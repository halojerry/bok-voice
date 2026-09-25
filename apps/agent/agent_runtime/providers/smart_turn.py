"""smart-turn 语义闸（V1，2026-09-26）：VAD 停嘴处判「客户说完了没」。

pipecat-ai/smart-turn-v3 的 ``smart-turn-v3.2-cpu.onnx``（int8 ~8.7MB，BSD-2）
直接吃音频语义——16kHz 波形 → Whisper log-mel（见 smart_turn_features.py）→
ONNX → sigmoid 概率：p ≥ 0.5=说完（complete）、p < 0.5=未说完（incomplete）。
官方基准 CPU 单线程 ~12ms/次，是 KoljaB/RealtimeVoiceChat「语义端点」思想的
模型化版本（livekit_plugins._pause_commit_min_chars 注释里的「官方 audio turn
detector 另评估」就是本件的前置调研——v1-mini 无粤语校准，先试社区件）。

接线点（livekit_plugins._run 的 VAD END_OF_SPEECH 分支）：VAD 停嘴 → 取会话
最近 ≤8s 尾部 PCM 喂模型 → p<0.5 复用既有 join-hold（不新造机制）等下一段
并入；p≥0.5 照旧 pause-commit/finish。判定只影响「停嘴是不是真停嘴」，不碰
句级标点/长度档提交。

铁律：
- **kill-switch ``BOK_SMART_TURN``（默认 "0"=关）**——未验收特性不默认开，
  读法全仓同款 ``os.environ.get(...) == "1"``；进 tools/bok.py ``_FORWARD_ENV``。
- **fail-open**：onnxruntime 缺位/模型缺位/推理异常一律返回 None，调用方照旧
  pause-commit（语义闸坏了不许比没有闸更差）。
- 推理放 ``asyncio.to_thread``（ONNX int8 CPU 推理是同步阻塞调用，不阻塞
  事件循环）；模型进程级懒加载单例，加载失败缓存住不重试（防每段语音重复
  吃加载异常）。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

# 模型资产随源码分发（与 fillers assets 同惯例）：agent_runtime/assets/smart-turn/
_MODEL_PATH = Path(__file__).resolve().parent.parent / "assets" / "smart-turn" / "smart-turn-v3.2-cpu.onnx"

# 模型期望 16kHz 单声道 int16 PCM；喂 8s（128000 样本），不足头部补零、超出留尾。
_MODEL_SAMPLE_RATE = 16000
_MODEL_SAMPLES_8S = 16000 * 8
PCM_BYTES_8S = _MODEL_SAMPLES_8S * 2  # 滚动尾部缓冲上限（livekit_plugins 取口）
# 尾部 PCM 少于这个长度不喂模型（信号不足，判定=噪声）：0.4s。
_MIN_PCM_BYTES = int(16000 * 2 * 0.4)
# 概率阈值（官方语义）：p ≥ 0.5 = complete。
_THRESHOLD = 0.5


def smart_turn_enabled() -> bool:
    """kill-switch：BOK_SMART_TURN=="1" 才启用（默认关，未验收特性）。"""
    return os.environ.get("BOK_SMART_TURN", "0") == "1"


def _pcm16_to_float(pcm: bytes | bytearray):
    """int16 LE PCM → [-1,1] float32（模型/特征件的输入域）。"""
    import numpy as np

    return np.frombuffer(bytes(pcm), dtype="<i2").astype("float32") / 32768.0


def truncate_or_pad_to_8s(wave):
    """留尾截到 8s，不足头部补零（与上游 local_smart_turn_v3 同款语义）。"""
    import numpy as np

    if wave.size > _MODEL_SAMPLES_8S:
        return wave[-_MODEL_SAMPLES_8S:]
    if wave.size < _MODEL_SAMPLES_8S:
        return np.pad(wave, (_MODEL_SAMPLES_8S - wave.size, 0), mode="constant", constant_values=0)
    return wave


def smart_turn_decide(prob: float | None) -> str:
    """概率 → 判定（纯函数，离线可测）：'hold'（未说完）/ 'commit'（说完）/
    'pass'（不可判——fail-open 走旧路径）。"""
    if prob is None:
        return "pass"
    return "hold" if prob < _THRESHOLD else "commit"


class SmartTurnAnalyzer:
    """ONNX 会话包装：predict(pcm_bytes) → 概率（同步，调用方放线程池）。"""

    def __init__(self, model_path: str | Path | None = None) -> None:
        import onnxruntime as ort

        path = str(model_path or _MODEL_PATH)
        if not Path(path).is_file():
            raise FileNotFoundError(f"smart-turn model missing: {path}")
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 1  # 官方基准档（~12ms/次）
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(path, sess_options=so)
        from .smart_turn_features import compute_whisper_log_mel_features

        self._features = compute_whisper_log_mel_features

    def predict(self, pcm: bytes | bytearray) -> float:
        """int16 PCM 尾部 → 「说完」概率。纯同步 CPU 调用（~12ms）。"""
        import numpy as np

        wave = truncate_or_pad_to_8s(_pcm16_to_float(pcm))
        log_mel = self._features(wave, do_normalize=True)  # (80, 800) float32
        input_features = np.expand_dims(log_mel, axis=0)  # batch 维
        outputs = self._session.run(None, {"input_features": input_features})
        return float(np.asarray(outputs[0]).reshape(-1)[0])


_analyzer: SmartTurnAnalyzer | None = None
_analyzer_failed = False
_analyzer_lock = asyncio.Lock()


async def _get_analyzer() -> SmartTurnAnalyzer | None:
    """进程级懒加载单例；失败缓存（模型缺位/ort 缺位不反复重试刷日志）。"""
    global _analyzer, _analyzer_failed
    if _analyzer is not None:
        return _analyzer
    if _analyzer_failed:
        return None
    async with _analyzer_lock:
        if _analyzer is not None:
            return _analyzer
        if _analyzer_failed:
            return None
        try:
            t0 = time.monotonic()
            _analyzer = await asyncio.to_thread(SmartTurnAnalyzer)
            print(
                f"SMART_TURN model loaded path={_MODEL_PATH.name} "
                f"load_ms={int((time.monotonic() - t0) * 1000)}",
                flush=True,
            )
            return _analyzer
        except Exception as exc:  # noqa: BLE001 - fail-open
            _analyzer_failed = True
            print(f"SMART_TURN verdict=failopen reason=model_load err={exc!r}", flush=True)
            return None


async def smart_turn_prob(pcm: bytes | bytearray) -> float | None:
    """尾部 PCM → 概率；任何不可判（闸关/太短/加载失败/推理异常）→ None。

    None 语义=「不干预，照旧旧路径」——调用方（vad-pause 判定点）拿到 None
    必须原样放行。skip/failopen 场景在此打点（闸关时零日志零成本）。
    """
    if not smart_turn_enabled():
        return None
    if len(pcm) < _MIN_PCM_BYTES:
        print(f"SMART_TURN verdict=skip reason=short_audio samples={len(pcm) // 2}", flush=True)
        return None
    analyzer = await _get_analyzer()
    if analyzer is None:
        return None
    try:
        return await asyncio.to_thread(analyzer.predict, bytes(pcm))
    except Exception as exc:  # noqa: BLE001 - fail-open
        print(f"SMART_TURN verdict=failopen reason=inference err={exc!r}", flush=True)
        return None
