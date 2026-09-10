"""LLM 慢生成轮垫话(2026-09-09 PR-2;2026-09-10 资产化改版)。

垫话=随源码分发的固定资产:固定音色+固定参数(见 scripts/gen_filler_assets.py)
经 MiniMax 预生成 wav,连同 manifest.json 提交在 assets/fillers/——运行时只播
文件,绝不云合成,与人设音色/tts_cache 状态解耦(旧 cache.lookup 路径废弃:
人设一换或未 pregen 就静默 miss)。

万能话术原则(2026-09-10):垫话随机触发,语境永不匹配——只保留注意力应承
(好的/收到/明白/嗯)与等待邀请(稍等/我看下),零动作动词;改话术=改生成脚本
重新生成,一个 PR。

播放排序(2026-09-10 用户拍板):垫话一旦开播必须播完。真回复首音频到达时
不再掐垫话,而是 _RelaySynthesizeStream 向 hold_if_playing() 询问扣压时长
(垫话剩余+BOK_FILLER_GAP_MS 默认 300ms),帧缓冲到点再放——垫话→静默→回复
自然衔接。新用户轮(cancel)仍立即掐垫话:用户插话优先,放完旧垫话反而怪。

链发(2026-09-10):首条垫话播完、回复首音频仍未到 → 垫话后残余静默照旧,
BOK_FILLER_GAP_MS 呼吸后自动补第二发——挂「播完观察者」等官方
PlayHandle.wait_for_playout()(播完即醒,精确补位,不靠估时长)。每轮封顶
1 次(_chain_depth),链发消耗 BOK_FILLER_MAX 同一计数;BOK_FILLER_CHAIN=0 关。

通道铁律(不变):垫话走 BackgroundAudioPlayer out-of-band 音轨,绝不能走
session.say()——livekit 1.8 speech 队列严格串行,垫话必排回复后(实机实证)。
垫话不进 LLM 上下文(out-of-band 不入 chat_ctx,KV 前缀/回声锚零污染)。
每通限次 BOK_FILLER_MAX(默认 2)+随机不重样;BOK_FILLER=0 一键全关。

语言铁律(2026-09-10 实证修复):垫话语言=会话装配时钉死的通话语言,构造时
由调用方捕获传入——en 通话曾因运行时 lang 状态漂移落回 zh 池,英国腔通话里
放出普通话「我看一下哈」(用户实测怪声根因)。池缺失=明文日志跳过,绝不跨
语言发声。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import wave
from pathlib import Path

FILLER_ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "fillers"


def filler_enabled() -> bool:
    return os.environ.get("BOK_FILLER", "1") == "1"


def filler_delay_s() -> float:
    try:
        return max(0.0, int(os.environ.get("BOK_FILLER_DELAY_MS", "500")) / 1000)
    except ValueError:
        return 0.5


def filler_gap_s() -> float:
    """垫话播完 → 回复衔接前的静默间隔(用户定档 300ms)。"""
    try:
        return max(0.0, int(os.environ.get("BOK_FILLER_GAP_MS", "300")) / 1000)
    except ValueError:
        return 0.3


def filler_max_per_call() -> int:
    try:
        return max(0, int(os.environ.get("BOK_FILLER_MAX", "2")))
    except ValueError:
        return 2


def filler_chain_enabled() -> bool:
    """首条垫话播完回复仍未出声 → 自动补第二条(BOK_FILLER_CHAIN,默认开)。"""
    return os.environ.get("BOK_FILLER_CHAIN", "1") == "1"


def load_manifest(assets_dir: Path) -> dict[str, list[dict]]:
    """manifest.json → {lang: [{text, file, dur_s}, ...]}。"""
    p = Path(assets_dir) / "manifest.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    return {
        str(lang): [e for e in entries if e.get("file")]
        for lang, entries in data.items()
        if entries
    }


def load_wav_pcm(path: Path) -> tuple[bytes, int]:
    """stdlib wave → (pcm16 bytes, sample_rate)。只收 mono/16bit(生成端保证)。"""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError(f"unexpected wav layout: {path.name}")
        return w.readframes(w.getnframes()), w.getframerate()


class FillerDirector:
    """垫话编排:arm(轮提交后) → 定时器 → 资产命中即播;首音频回调 → 只作废
    定时器(垫话播完),回复帧由 tts_cache hold 到「垫话完+gap」。

    纯编排无业务判定——「该不该垫」的轮次资格由调用方决定(hook 正常返回才
    arm;closing/WA 步由 guards 回调在开火前最后一刻复核)。
    """

    def __init__(
        self,
        session,
        *,
        lang_resolver,
        player=None,
        guards=None,
        assets_dir: Path | None = None,
    ) -> None:
        self._session = session
        self._lang_resolver = lang_resolver
        # BackgroundAudioPlayer(livekit 官方 out-of-band 音轨):垫话唯一合法通道。
        # None=垫话整体失效。
        self._player = player
        self._guards = guards or (lambda: False)
        self._assets = Path(assets_dir) if assets_dir else FILLER_ASSETS_DIR
        self._manifest: dict[str, list[dict]] | None = None
        self._timer: asyncio.Task | None = None
        self._handle = None
        self._cur_dur = 0.0
        self._play_started = 0.0
        self._fired_lines: list[str] = []  # 已实际播放(审计/探针断言用)
        self._recent: list[str] = []  # 已选取(含未播出),防相邻重复
        self._count = 0
        self._chain_task: asyncio.Task | None = None
        self._chain_depth = 0  # 本轮已链发次数(每轮封顶 1)
        self._reply_audio_seen = False  # on_reply_first_audio 置位,新轮/arm 重置

    # ---- 生命周期 ----

    def arm(self) -> None:
        """轮提交、确认走 LLM 正常路径后调用;重复 arm 先作废旧定时器/链发。"""
        self._cancel_timer()
        self._cancel_chain()
        if not filler_enabled() or self._count >= filler_max_per_call():
            return
        if self._player is None:
            return
        delay = filler_delay_s()
        if delay <= 0:
            return
        self._timer = asyncio.create_task(self._fire(delay))

    def on_reply_first_audio(self) -> None:
        """真回复首音频(CachedTTS stream 回调):只作废定时器。

        在播垫话**不掐**——播放排序契约=垫话播完→gap→回复;扣压由
        tts_cache._RelaySynthesizeStream 向 hold_if_playing() 询时实现。
        置位 _reply_audio_seen:链发观察者醒来时据此放弃补第二发。
        """
        self._reply_audio_seen = True
        self._cancel_timer()

    def hold_if_playing(self) -> float:
        """回复首帧应扣压的秒数:在播垫话的剩余时长 + gap;没在播=0。"""
        handle = self._handle
        if handle is None:
            return 0.0
        done = getattr(handle, "done", None)
        if callable(done) and done():
            return 0.0
        elapsed = time.monotonic() - self._play_started if self._play_started else 0.0
        remaining = max(0.0, self._cur_dur - elapsed)
        return remaining + filler_gap_s()

    def cancel(self) -> None:
        """新用户轮到达等场景:作废定时器/链发并停掉在播垫话——用户插话优先,
        out-of-band 音轨不受框架打断机制管理,必须自己停。"""
        self._cancel_timer()
        self._cancel_chain()
        self._stop_playing()

    def reset_per_call(self) -> None:
        self._count = 0
        self._fired_lines.clear()
        self._recent.clear()
        self._cancel_timer()
        self._cancel_chain()
        self._stop_playing()
        self._handle = None

    # ---- 内部 ----

    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    def _cancel_chain(self) -> None:
        """取消链发观察者并复位本轮链发状态(arm/cancel/reset 三处共用)。"""
        if self._chain_task is not None and not self._chain_task.done():
            self._chain_task.cancel()
        self._chain_task = None
        self._chain_depth = 0
        self._reply_audio_seen = False

    def _stop_playing(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            done = getattr(handle, "done", None)
            if callable(done) and done():
                return
            handle.stop()
        except Exception:  # noqa: BLE001 - 停播失败让垫话自然播完(短语 ≤1.5s)
            pass

    def _spawn_chain(self) -> None:
        """首条起播即挂「播完观察者」——官方 PlayHandle.wait_for_playout 精确补位。"""
        if not filler_chain_enabled() or self._chain_depth > 0:
            return
        self._chain_task = asyncio.create_task(self._chain_wait())

    async def _chain_wait(self) -> None:
        try:
            handle = self._handle
            if handle is None:
                return
            wait = getattr(handle, "wait_for_playout", None)
            if callable(wait):
                await wait()  # 官方 API:播完即醒(async def,须调用后 await)
            else:  # 测试替身无官方 API:按已知时长等(少量过等由后续门复核兜住)
                await asyncio.sleep(self._cur_dur + 0.05)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - 观察者失败=退回单发行为,唔阻通话
            return
        self._handle = None  # 已播完:清档,放行 _fire 的「上一句还在播」门
        if self._reply_audio_seen:
            return  # 回复首音频已到,hold 契约自会衔接,唔使链发
        if not filler_enabled() or self._player is None:
            return
        if self._guards() or filler_max_per_call() <= self._count:
            return
        state = str(getattr(self._session, "agent_state", "") or "")
        if state not in ("listening", "thinking", ""):
            return
        await asyncio.sleep(filler_gap_s())  # 垫话→垫话同款呼吸
        if self._reply_audio_seen or self._handle is not None:
            return  # gap 中回复出声/新开火——让位,唔叠音
        self._chain_depth += 1
        await self._fire(0.0)  # 复用开火路径(门在 _fire 内再复核;计数同源)

    def _pools(self) -> dict[str, list[dict]]:
        if self._manifest is None:
            try:
                self._manifest = load_manifest(self._assets)
            except Exception as exc:  # noqa: BLE001 - 资产缺失=垫话停用(响亮降级)
                print(
                    f"BOK_FILLER manifest unavailable dir={self._assets} err={exc!r} — 垫话停用",
                    flush=True,
                )
                self._manifest = {}
        return self._manifest

    def _pick(self, lang: str) -> dict | None:
        pool = self._pools().get(lang)
        if not pool:
            # 语言铁律:宁可不垫,绝不跨语言发声(池缺失响亮日志,不落其他语言池)。
            print(f"BOK_FILLER no pool lang={lang!r} — 跳过", flush=True)
            return None
        # 随机不重样(同垫话连续两轮最刺耳):池里剔除上两句后随机,池小才允许重复。
        recent = set(self._recent[-2:])
        candidates = [e for e in pool if e["file"] not in recent] or list(pool)
        entry = random.choice(candidates)
        self._recent.append(entry["file"])
        return entry

    async def _fire(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._timer = None
        try:
            # 直调防御:kill-switch/player 缺失在 arm 已挡,这里再挡一次
            # (定时器任务与状态翻转存在竞态窗口)。
            if not filler_enabled() or self._player is None:
                return
            if self._guards() or filler_max_per_call() <= self._count:
                return
            state = str(getattr(self._session, "agent_state", "") or "")
            if state not in ("listening", "thinking", ""):
                return  # 别的东西在播/状态不明,唔叠音
            if self._handle is not None:
                return  # 上一句垫话还在播(理论到唔到:cancel 已清),唔叠音
            entry = self._pick(self._lang_resolver())
            if not entry:
                return
            pcm, rate = load_wav_pcm(self._assets / entry["file"])
            from .tts_cache import frames_aiter, pcm_to_frames

            frames = pcm_to_frames(pcm, rate)
            self._count += 1
            self._fired_lines.append(entry["text"])
            print(f"BOK_FILLER fired count={self._count} line={entry['text']!r}", flush=True)
            # out-of-band 播放:即刻出声,唔排 speech 队列。fade_in 防咔哒(同
            # MiniMax 首包修剪 15ms 姿势),fade_out 令 stop() 有 50ms 淡出。
            try:
                from livekit.agents import AudioConfig

                source = AudioConfig(source=frames_aiter(frames), fade_in=0.015, fade_out=0.05)
            except Exception:  # noqa: BLE001 - 无 livekit(测试替身)直接喂裸帧迭代
                source = frames_aiter(frames)
            self._cur_dur = float(entry.get("dur_s") or round(len(pcm) / 2 / rate, 2))
            self._play_started = time.monotonic()
            self._handle = self._player.play(source)
            self._spawn_chain()  # 挂播完观察者:回复没来就链发第二发
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 垫话失败唔阻通话
            print(f"BOK_FILLER error err={exc!r}", flush=True)
