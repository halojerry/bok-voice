"""W6 全程场景底噪(2026-10-06 demo-quality-wave):低音量无缝循环铺底 + AI 说话 duck。

需求(Ethan 2026-10-06):全程循环环境音(office/callcenter/car 三档),可拔插、
绝不抢道——底噪发布在 agent 出站 out-of-band 音轨(与垫话同一套
BackgroundAudioPlayer 轨道基建),电平恒定低(manifest gain_db,缺省 -28dB 播放
衰减 × 资产 -28dBFS 峰值 ≈ -56dBFS 有效铺底),AI 说话时 duck(-12dB 额外衰减,
~200ms 平滑渐变)、停嘴回涨。总闸=env `BOK_AMBIENT_SCENE`(none 缺省=零行为,
已登记 tools/bokctl/env.py _FORWARD_ENV 2026-10-06)。

轨道复用(fillers.py 同款铁律):
- livekit BackgroundAudioPlayer 内部音轨固定 48k(AudioSource(48000)+AudioMixer
  (48000),mixer 无重采样)——24k 资产 wav 播放前必须 resample 对齐,否则 2 倍速
  升调「机器人声」(2026-09-11 实机实证,fillers.BACKGROUND_PLAYER_RATE 同源)。
- 官方 play(loop=True) 只支持文件路径(内部 `_loop_audio_frames`),AsyncIterator
  明确不支持 loop;且 AudioConfig.volume 是 play 时点固定值,运行中不可变。
  **duck 需要运行中渐变增益** → 官方两路都不覆盖,故自写小循环播放器:
  轨道获取方式照抄 fillers(player.play(AsyncIterator) 单次调用、out-of-band
  混音器自行叠加),循环=无限 AsyncIterator(官方文档明示「use your own
  'infinite' AsyncIterator with loop=False」),增益/duck=生成器内逐帧 dB 域
  平滑轨迹(线性 dB 斜率,满 12dB 恰好走完 DUCK_FADE_S)。
- out-of-band 不入 chat_ctx(零 LLM/转写污染),框架打断机制不管理该轨——
  stop() 必须自己停(镜像 fillers 通道铁律)。

宽容原则(镜像 bok_voice_core.flow_graph parse 宽容档):manifest 缺失/坏 JSON/
形状不对 → 空目录零行为,运行时绝不因底噪炸(宁缺毋炸,底噪是纯锦上添花)。

资产=scripts/seed/gen_ambience.py 种子化确定性合成(numpy,license 记 manifest),
40s 无缝循环(尾→首 2s crossfade)24kHz mono 16-bit wav,随源码分发
assets/ambience/——同 fillers 分发模式,改场景=改生成脚本重跑,一个 PR。

装配接线(审计员,本模块零 agent.py 改动):见模块尾 AmbienceLoopPlayer 文档。
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import numpy as np

from .fillers import BACKGROUND_PLAYER_RATE, load_wav_pcm, resample_pcm

AMBIENCE_ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "ambience"
AMBIENCE_SCENE_ENV = "BOK_AMBIENT_SCENE"

# 场景目录:运行时合法名单(资产生成参数/seed 单源在 scripts/seed/gen_ambience.py,
# test_ambience 钉两者键集一致)。新增场景=目录加一行 + 生成脚本加配方 + 重跑。
SCENES: dict[str, str] = {
    "office": "写字楼底噪:褐噪声底 + 120Hz HVAC 嗡 + 极低幅度慢起伏",
    "callcenter": "客服中心:office 底 + 稀疏远场人声嗡 + 偶发轻键盘敲击",
    "car": "车内:低频路噪隆隆(<120Hz 为主) + 缓慢幅度起伏 + 轻微路噪嘶声",
}

# 播放增益缺省 -28dB(底噪是铺底不是前景;资产本身已归一到约 -28dBFS 峰值,
# 叠加后有效峰值 ≈ -56dBFS——远低于人声,恒不触发我方 VAD/回声守卫)。
DEFAULT_GAIN_DB = -28.0
# duck 额外衰减(AI 说话时避让;停嘴回涨=同一轨迹反向走回)。
DUCK_ATTEN_DB = 12.0
# duck 渐变时长:满 12dB 走完恰好 0.2s(平滑,无爆音无抽动)。
DUCK_FADE_S = 0.2
# 播放增益钳制窗:坏值/越界 manifest 收进此窗(防手滑把底噪写成前景音量)。
GAIN_DB_FLOOR = -60.0
GAIN_DB_CEIL = -12.0
# 循环帧块粒度(增益步进分辨率):20ms @48k=960 样本——mixer 实时拉取,
# 每块一次 numpy 向量化,零压力;粒度越细 duck 渐变台阶越密。
LOOP_CHUNK_MS = 20


# ---- 纯函数层(单测直喂,零 livekit 零 IO 副作用) ----


def load_ambience_manifest(assets_dir: Path | str | None = None) -> dict[str, dict]:
    """assets/ambience/manifest.json → {scene: {file, loop_s, gain_db, ...}}。

    宽容解析(镜像 flow_graph parse 档):文件缺失/坏 JSON/形状不对/条目缺
    scene 或 file → 逐条跳过,整体失败 → 空目录({})永不炸。接受三种形状:
    {"scenes":[...]}(生成端规范形)/ 顶层 list / {scene: {...}} 手写 dict。
    loop_s/gain_db 数值坏 → 丢该键(消费端回落缺省),不影响其他条目。
    """
    root = Path(assets_dir) if assets_dir is not None else AMBIENCE_ASSETS_DIR
    try:
        data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 缺文件/坏 JSON=空目录,零行为
        return {}
    entries: list[dict] = []
    if isinstance(data, dict):
        raw = data.get("scenes")
        if isinstance(raw, list):
            entries = [e for e in raw if isinstance(e, dict)]
        else:
            entries = [
                {**v, "scene": k} for k, v in data.items()
                if isinstance(v, dict) and k != "scenes"
            ]
    elif isinstance(data, list):
        entries = [e for e in data if isinstance(e, dict)]
    out: dict[str, dict] = {}
    for e in entries:
        name = str(e.get("scene") or "").strip().lower()
        file = str(e.get("file") or "").strip()
        if not name or not file or name in out:
            continue
        entry = dict(e)
        entry["scene"] = name
        entry["file"] = file
        try:
            entry["loop_s"] = float(e.get("loop_s"))
        except (TypeError, ValueError):
            entry.pop("loop_s", None)
        try:
            entry["gain_db"] = clamp_gain_db(float(e.get("gain_db")))
        except (TypeError, ValueError):
            entry["gain_db"] = DEFAULT_GAIN_DB
        out[name] = entry
    return out


def clamp_gain_db(value: float, default: float = DEFAULT_GAIN_DB) -> float:
    """播放增益钳制:坏值(NaN/inf/非数)→ default;越界收进 [FLOOR, CEIL]。

    底噪电平是「绝不抢道」的硬边界——manifest 手滑写 0dB 也被钳回 -12dB 封顶。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(v) or math.isinf(v):
        return float(default)
    return float(min(max(v, GAIN_DB_FLOOR), GAIN_DB_CEIL))


def resolve_scene(
    env_value: str | None,
    manifest: dict[str, dict] | None = None,
    *,
    assets_dir: Path | str | None = None,
    custom_file: str | None = None,
    custom_gain_db: float | None = None,
) -> dict | None:
    """BOK_AMBIENT_SCENE 值 → 场景条目;none/空/未知/清单缺失 → None(零行为)。

    双门:①名字必须在 SCENES 目录(运行时合法名单);②必须在 manifest 且有
    资产文件(运行时真相)——目录认得但资产没生成 = None,绝不空转播。
    返回条目为 manifest 条目的拷贝(含 scene/file/gain_db/loop_s)。

    **custom 档(2026-10-08,用户拍板「底噪像电流,自己上传音频」)**:内置三场景
    是种子化程序合成(褐噪声+HVAC 嗡——合成味的来源);真房间录音走本分支:
    ``BOK_AMBIENT_SCENE=custom`` + ``BOK_AMBIENT_FILE=/abs/path/room.wav``
    (可选 ``BOK_AMBIENT_GAIN_DB`` 覆盖播放衰减,缺省 -28dB 与内置同轨)。文件
    不在/非文件 → None 零行为(宁缺宁炸纪律同 manifest);loop_s 由播放器按实际
    PCM 长度计,条目不预填。env 分支不进 SCENES/manifest(test_ambience 的
    目录↔清单 parity 钉零影响)。"""
    name = str(env_value or "").strip().lower()
    if not name or name in ("none", "off", "0"):
        return None
    if name == "custom":
        import os

        path = str(custom_file if custom_file is not None else os.environ.get("BOK_AMBIENT_FILE", "")).strip()
        p = Path(path).expanduser()
        if not path or not p.is_absolute() or not p.is_file():
            print(f"BOK_AMBIENT custom: BOK_AMBIENT_FILE 未设/非绝对路径/不在盘({path[:60]}) — 零行为", flush=True)
            return None
        gain = custom_gain_db
        if gain is None:
            import os as _os

            try:
                gain = clamp_gain_db(float(_os.environ.get("BOK_AMBIENT_GAIN_DB", "")))
            except (TypeError, ValueError):
                gain = DEFAULT_GAIN_DB
        return {
            "scene": "custom",
            "file": str(p),
            "gain_db": clamp_gain_db(gain),
            "license": "user-provided",
        }
    if name not in SCENES:
        return None
    if manifest is None:
        manifest = load_ambience_manifest(assets_dir)
    entry = manifest.get(name)
    if not entry or not entry.get("file"):
        return None
    out = dict(entry)
    out["scene"] = name
    return out


def gain_slew_db_per_sample(duck_atten_db: float, fade_s: float, rate: int) -> float:
    """dB 域斜率:满 |duck_atten_db| 恰好走完 fade_s 秒。fade_s<=0 = 瞬时。"""
    span = max(0.0, float(fade_s)) * max(1, int(rate))
    if span <= 0 or duck_atten_db <= 0:
        return 1e6  # 瞬时(逐块直达目标)
    return float(duck_atten_db) / span


def gain_trajectory(
    cur_db: float, target_db: float, n: int, slew_db_per_sample: float
) -> tuple[float, "np.ndarray"]:
    """增益轨迹(纯函数):从 cur_db 向 target_db 以恒定 dB 斜率走 n 步。

    返回 (新 cur_db, 长度 n 的 gains 数组[dB])——线性 dB 域渐变,听感平滑
    (噪声底对波形线性不敏感,dB 域恒速=响度恒速)。到点即停,不过冲。
    """
    if n <= 0:
        return float(cur_db), np.empty(0, dtype=np.float64)
    diff = float(target_db) - float(cur_db)
    if diff == 0.0:
        return float(cur_db), np.full(n, float(cur_db), dtype=np.float64)
    max_move = float(slew_db_per_sample) * n
    step = max(-max_move, min(max_move, diff))
    ramp = float(cur_db) + np.sign(step) * float(slew_db_per_sample) * np.arange(n)
    lo, hi = min(float(cur_db), float(target_db)), max(float(cur_db), float(target_db))
    gains = np.clip(ramp, lo, hi)
    return float(cur_db) + step, gains


def mix_chunk(seg: "np.ndarray", gains_db: "np.ndarray") -> bytes:
    """int16 段 × dB 增益轨迹 → int16 bytes(纯函数;削波钳位)。"""
    out = seg.astype(np.float32) * np.power(10.0, gains_db.astype(np.float32) / 20.0)
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


# ---- 播放器(轨道获取照抄 fillers;循环+duck 官方不覆盖,自写最小实现) ----


class AmbienceLoopPlayer:
    """单场景无缝循环底噪:一个 play() 提交无限 AsyncIterator,常驻混合器。

    用法(agent.py 装配点):
        scene = resolve_scene(os.environ.get("BOK_AMBIENT_SCENE"))
        if scene and _bg_audio is not None:
            _amb = AmbienceLoopPlayer(_bg_audio, entry=scene)
            _amb.start()
        # agent_state=speaking → _amb.set_ducked(True);listening → set_ducked(False)
        # 挂断/收线 → _amb.stop()

    状态机约定:ducked 是目标态(set_ducked 只翻旗),增益实际走 ~200ms 平滑
    轨迹——speaking/listening 高频翻转无害,轨迹自动收敛到最后一个目标。
    player=None / 资产缺失 / start 失败 = 整体零行为(响亮日志,不阻通话)。
    """

    def __init__(
        self,
        player,
        *,
        entry: dict,
        assets_dir: Path | str | None = None,
        rate: int = BACKGROUND_PLAYER_RATE,
        duck_atten_db: float = DUCK_ATTEN_DB,
        fade_s: float = DUCK_FADE_S,
        call_label: str = "",
    ) -> None:
        self._player = player
        self._scene = str(entry.get("scene") or "")
        self._call_label = str(call_label or "")
        self._rate = int(rate)
        self._chunk_n = max(1, self._rate * LOOP_CHUNK_MS // 1000)
        self._duck_atten_db = max(0.0, float(duck_atten_db))
        self._fade_s = max(0.0, float(fade_s))
        self._slew = gain_slew_db_per_sample(self._duck_atten_db, self._fade_s, self._rate)
        base = clamp_gain_db(entry.get("gain_db", DEFAULT_GAIN_DB))
        self._gain_db = base
        self._cur_db = base
        self._target_db = base
        self._ducked = False
        self._task: asyncio.Task | None = None
        self._handle = None
        self._pcm: "np.ndarray | None" = None
        try:
            wav_path = Path(assets_dir if assets_dir is not None else AMBIENCE_ASSETS_DIR) / str(entry["file"])
            pcm, wav_rate = load_wav_pcm(wav_path)
            self._pcm = np.frombuffer(resample_pcm(pcm, wav_rate, self._rate), dtype=np.int16)
        except Exception as exc:  # noqa: BLE001 - 资产缺失=底噪停用,唔阻通话
            print(
                f"BOK_AMBIENT wav unavailable scene={self._scene!r} err={exc!r} — 底噪停用",
                flush=True,
            )
            self._pcm = None

    # ---- 状态 ----

    @property
    def available(self) -> bool:
        """资产已装载且上游 player 在场——False 时 start() 零行为。"""
        return self._player is not None and self._pcm is not None and len(self._pcm) > 0

    @property
    def ducked(self) -> bool:
        return self._ducked

    @property
    def gain_db(self) -> float:
        return self._gain_db

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- 生命周期 ----

    def start(self) -> None:
        """提交循环帧流(单次 play,常驻混合器;幂等)。"""
        if self._task is not None:
            return
        if not self.available:
            return
        self._task = asyncio.create_task(self._run())

    def set_ducked(self, ducked: bool) -> None:
        """AI 说话=True(避让 -12dB,~200ms 渐下);停嘴=False(回涨)。

        只翻目标旗——增益实际走 gain_trajectory 平滑轨迹;未 start 时翻旗
        无害(轨迹从 start 起点生效)。
        """
        ducked = bool(ducked)
        if ducked == self._ducked:
            return
        self._ducked = ducked
        self._target_db = self._gain_db - (self._duck_atten_db if ducked else 0.0)

    def stop(self) -> None:
        """收线/关闭:停播+撤流(幂等;out-of-band 轨框架不管,必须自己停)。"""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.stop()
            except Exception:  # noqa: BLE001 - 停播失败交给任务取消兜底
                pass
        self._cur_db = self._target_db = self._gain_db
        self._ducked = False

    # ---- 内部 ----

    async def _run(self) -> None:
        """提交 play 并驻留到 stop()/取消。play 失败=底噪停用(不阻通话)。"""
        label = f" call={self._call_label}" if self._call_label else ""
        try:
            from livekit import rtc
        except Exception as exc:  # noqa: BLE001 - 无 livekit(测试/异常环境)
            print(f"BOK_AMBIENT livekit unavailable scene={self._scene!r} err={exc!r}", flush=True)
            return
        try:
            self._handle = self._player.play(self._frames_gen(rtc))
        except Exception as exc:  # noqa: BLE001 - mixer 未启动等=底噪停用
            print(f"BOK_AMBIENT play failed scene={self._scene!r} err={exc!r}{label}", flush=True)
            self._handle = None
            return
        print(
            f"BOK_AMBIENT started scene={self._scene!r} gain_db={self._gain_db:.0f}"
            f" loop_s={len(self._pcm) / self._rate:.1f}{label}",
            flush=True,
        )
        try:
            await self._handle.wait_for_playout()
        except asyncio.CancelledError:
            raise
        finally:
            # 正常 stop() 路径 _handle 已在 stop() 清档;此处兜 cancel 路径。
            self._handle = None

    async def _frames_gen(self, rtc):
        """无限循环帧流:资产 PCM 环形取块 × duck 增益轨迹 → rtc.AudioFrame。

        停止=上层 handle.stop() → mixer remove_stream → 本生成器被 aclose()
        (GeneratorExit 在 yield 点抛出,finally 静默收尾)。
        """
        pcm = self._pcm
        total = len(pcm)
        pos = 0
        try:
            while True:
                n = min(self._chunk_n, total)
                idx = np.arange(pos, pos + n) % total
                seg = pcm[idx]
                self._cur_db, gains = gain_trajectory(
                    self._cur_db, self._target_db, n, self._slew
                )
                data = mix_chunk(seg, gains)
                pos = int((pos + n) % total)
                yield rtc.AudioFrame(
                    data=data,
                    sample_rate=self._rate,
                    num_channels=1,
                    samples_per_channel=n,
                )
                await asyncio.sleep(0)
        finally:
            self._cur_db = self._target_db = self._gain_db
            self._ducked = False
