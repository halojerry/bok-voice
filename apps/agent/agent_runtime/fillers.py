"""LLM 慢生成轮垫话(2026-09-09,PR-2;设计见 docs/superpowers/specs/2026-09-08-tts-cache-design.md)。

只垫「必须 LLM 临场生成」的轮次(话术缓存未命中、hook 正常返回走 LLM):
回复首音频 BOK_FILLER_DELAY_MS(默认 700ms)未到 → 播一句预合成应承语,
真回复首音频到达 → 停掉垫话(CachedTTS 首音频回调驱动)。

通道铁律(2026-09-09 实证改版):垫话必须走 BackgroundAudioPlayer out-of-band
音轨,**绝不能走 session.say()**——livekit 1.8 speech 队列严格串行(同优先级按
插入序 pop,且对前一个 speech await 整个 generation 完成),回复 speech 在 commit
时已入队,700ms 后 say() 的垫话必然排在回复后面:多数被首音频回调静默吞掉
(慢轮照样纯静音),事件循环拥塞时竞态漏出=垫话在回复播完后才响(实机实证)。
out-of-band 音不受框架打断机制管理 → 新用户轮/收线由 cancel()/reset 主动 stop。

铁律:
- 快轮永远不垫(定时器被首音频回调作废,零打扰);
- 垫话绝不触发云合成——缓存未命中直接跳过(垫话库靠 tts-pregen --fillers 预生成);
- 垫话不进 LLM 上下文(out-of-band 根本不入 chat_ctx,KV 前缀不变、4B 不被锚定、
  last_reply 回声锚不被污染);
- 每通限次 BOK_FILLER_MAX(默认 2)+轮换防机械;BOK_FILLER=0 一键全关;
- stop() 后音源队列最多残留 ~400ms 尾音(官方 _AUDIO_SOURCE_BUFFER_MS)——与
  回复首句自然重叠,属应承感不算 bug。
"""

from __future__ import annotations

import asyncio
import json
import os
import random

# 默认垫话骨架(最终话术可经 BOK_FILLER_LINES JSON 覆盖;用户拍板后改这里也行)。
# 2026-09-09 扩容:按语言口头禅+客服常用语起池(粤语繁体、zh 书面普通话、en
# 口语),每通限次内随机不重样(见 _pick_line)——旧版每通固定从第 0 句起轮换,
# 每通第一句垫话千篇一律。
DEFAULT_FILLER_LINES: dict[str, tuple[str, ...]] = {
    "zh": (
        "好的，您稍等。",
        "我看一下哈。",
        "马上帮您查。",
        "收到，您别急。",
        "明白，您等等啊。",
    ),
    "cantonese": (
        "好，等我睇下。",
        "好，你等陣。",
        "好嘅，幫你跟緊。",
        "收到，冇問題。",
        "明白，等我一陣。",
    ),
    "en": (
        "Sure, let me check.",
        "One moment please.",
        "Got it, checking now.",
        "Of course, one sec.",
        "Right away, let me see.",
    ),
}


def filler_enabled() -> bool:
    return os.environ.get("BOK_FILLER", "1") == "1"


def filler_delay_s() -> float:
    try:
        return max(0.0, int(os.environ.get("BOK_FILLER_DELAY_MS", "700")) / 1000)
    except ValueError:
        return 0.7


def filler_max_per_call() -> int:
    try:
        return max(0, int(os.environ.get("BOK_FILLER_MAX", "2")))
    except ValueError:
        return 2


def filler_lines() -> dict[str, tuple[str, ...]]:
    raw = os.environ.get("BOK_FILLER_LINES", "").strip()
    if not raw:
        return DEFAULT_FILLER_LINES
    try:
        data = json.loads(raw)
        return {str(k): tuple(str(x) for x in v) for k, v in data.items() if v}
    except Exception:  # noqa: BLE001 - 配置坏了回落默认
        return DEFAULT_FILLER_LINES


class FillerDirector:
    """垫话编排:arm(轮提交后) → 定时器 → 缓存命中即播;首音频回调 → 作废/停播。

    纯编排无业务判定——「该不该垫」的轮次资格由调用方决定(hook 正常返回才
    arm;closing/WA 步由 guards 回调在开火前最后一刻复核)。
    """

    def __init__(
        self,
        session,
        tts_provider,
        cache,
        *,
        lang_resolver,
        player=None,
        guards=None,
    ) -> None:
        self._session = session
        self._tts = tts_provider
        self._cache = cache
        self._lang_resolver = lang_resolver
        # BackgroundAudioPlayer(livekit 官方 out-of-band 音轨):垫话唯一合法通道,
        # 见模块 docstring——speech 队列会令垫话排在回复后面。None=垫话整体失效。
        self._player = player
        self._guards = guards or (lambda: False)
        self._timer: asyncio.Task | None = None
        self._handle = None
        self._fired_lines: list[str] = []  # 已实际播放(审计/探针断言用)
        self._recent: list[str] = []  # 已选取(含未播出),防相邻重复
        self._count = 0

    # ---- 生命周期 ----

    def arm(self) -> None:
        """轮提交、确认走 LLM 正常路径后调用;重复 arm 先作废旧定时器。"""
        self._cancel_timer()
        if not filler_enabled() or self._count >= filler_max_per_call():
            return
        if self._cache is None or self._tts is None or self._player is None:
            return
        delay = filler_delay_s()
        if delay <= 0:
            return
        self._timer = asyncio.create_task(self._fire(delay))

    def on_reply_first_audio(self) -> None:
        """真回复首音频(CachedTTS stream 回调):作废定时器 + 停掉在播垫话。"""
        self._cancel_timer()
        self._stop_playing()

    def cancel(self) -> None:
        """新用户轮到达等场景:作废定时器并停掉在播垫话——out-of-band 音轨
        不受框架打断机制管理(框架只 interrupt speech 队列),必须自己停。"""
        self._cancel_timer()
        self._stop_playing()

    def reset_per_call(self) -> None:
        self._count = 0
        self._fired_lines.clear()
        self._recent.clear()
        self._cancel_timer()
        self._stop_playing()
        self._handle = None

    # ---- 内部 ----

    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None

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
        except Exception:  # noqa: BLE001 - 停播失败让垫话自然播完(短语 ≤1.2s)
            pass

    def _pick_line(self, lang: str) -> str:
        lines = filler_lines().get(lang) or filler_lines().get("zh") or ()
        if not lines:
            return ""
        # 随机不重样(同垫话连续两轮最刺耳):池里剔除上一句后随机,池=1 才允许重复。
        # 旧版顺序轮换 _idx 从 0 起——per-job 进程每通重建,每通第一句永远相同。
        recent = set(self._recent[-2:])
        pool = [x for x in lines if x not in recent] or list(lines)
        line = random.choice(pool)
        self._recent.append(line)
        return line

    async def _fire(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._timer = None
        try:
            if self._guards() or filler_max_per_call() <= self._count:
                return
            state = str(getattr(self._session, "agent_state", "") or "")
            if state not in ("listening", "thinking", ""):
                return  # 别的东西在播/状态不明,唔叠音
            if self._handle is not None:
                return  # 上一句垫话还在播(理论到唔到:cancel 已清),唔叠音
            lang = self._lang_resolver()
            line = self._pick_line(lang)
            if not line:
                return
            voice = getattr(self._tts, "resolved_voice", lambda: "")()
            model = getattr(self._tts, "resolved_model", lambda: "")()
            pcm = self._cache.lookup(line, voice=voice, model=model)
            if pcm is None:
                return  # 垫话库未预生成/音色变更——跳过,绝不触发云合成
            from .tts_cache import frames_aiter, pcm_to_frames

            frames = pcm_to_frames(pcm, self._cache.sample_rate)
            self._count += 1
            self._fired_lines.append(line)
            print(f"BOK_FILLER fired count={self._count} line={line!r}", flush=True)
            # out-of-band 播放:即刻出声,唔排 speech 队列。fade_in 防咔哒(同
            # MiniMax 首包修剪 15ms 姿势),fade_out 令 stop() 有 50ms 淡出。
            try:
                from livekit.agents import AudioConfig

                source = AudioConfig(source=frames_aiter(frames), fade_in=0.015, fade_out=0.05)
            except Exception:  # noqa: BLE001 - 无 livekit(测试替身)直接喂裸帧迭代
                source = frames_aiter(frames)
            self._handle = self._player.play(source)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 垫话失败唔阻通话
            print(f"BOK_FILLER error err={exc!r}", flush=True)
