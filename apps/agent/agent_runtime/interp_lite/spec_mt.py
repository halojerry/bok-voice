"""interp_lite 投机翻译（prewarm-and-confirm 回归，W8-A1 核心延迟波）。

机器件**单源 import 旧线**（``interpret._SpecMtController`` 家族：稳定子句检测器/
held 槽/确认相似度 0.85/C2 必中体延迟交付——零复制，改动回旧线改）。本模块只有
lite 装配闭包（绑 pipeline/session/tts）与 lite 专属一刀：

- **右上下文门**（``BOK_INTERP_SPEC_RIGHT_CTX`` 缺省 2，0=关）：候选子句边界
  标点之后再收 ≥N 字才准开火——标点刚冒头就开火，ASR 回溯修订概率最高；
  实现为**喂前预滤**（不够 N 字整条 interim 不进检测器），不碰检测器内部
  状态，候选稳定性/预算记账语义与旧线逐字节同构。

装配差异（对照旧线 interpret 入口闭包）：无 backlog 摘译项；busy 闸=FIFO 深度
≥ ``BOK_INTERP_SPEC_BUSY_DEPTH``（复用旧线纯函数，缺省 2）∨ 真 MT 在途
（``pipeline.mt_busy``）∨ 死道（402/鉴权）；余段/兜底入队走 ``enqueue_raw``
（不过确认门，防 on_final 递归）。kill-switch 沿用 ``BOK_INTERP_SPEC_MT``
（缺省 1）；text-only 方向（无 TTS）整闸不开（无可预热音频，旧线同判）。
观测行格式照旧线：``INTERP_SPEC fire|hit|miss|abort chars=N``。
"""

from __future__ import annotations

import asyncio
import os

from ..interpret import (
    _SPEC_CONFIRM_SIM,  # noqa: F401 - 单源复出口（parity 钉：确认相似度=旧线同对象）
    _spec_busy_depth,
    _spec_clause_prefix,
    _spec_mt_enabled,
    _SpecMtController,
    _SpecMtDetector,
    _SpecMtHold,
)
from .providers.mt_deepseek import build_messages

__all__ = ["LiteSpecController", "build", "spec_right_ctx_chars"]

_SPEC_RIGHT_CTX_ENV = "BOK_INTERP_SPEC_RIGHT_CTX"
_SPEC_RIGHT_CTX_DEFAULT = 2


def spec_right_ctx_chars() -> int:
    """右上下文门字数（纯函数，单测直喂）：候选标点后再收 ≥N 字才开火。
    坏值回缺省 2；负数钳 0（=关，回旧线无右上下文档）。"""
    raw = os.environ.get(_SPEC_RIGHT_CTX_ENV, "")
    try:
        v = int(raw) if raw else _SPEC_RIGHT_CTX_DEFAULT
    except ValueError:
        return _SPEC_RIGHT_CTX_DEFAULT
    return max(0, v)


class LiteSpecController(_SpecMtController):
    """lite 控制器 = 旧线机器 + 右上下文门（喂前预滤）。"""

    def on_interim(self, text: str) -> None:
        n = spec_right_ctx_chars()
        if n > 0:
            cand = _spec_clause_prefix(str(text or ""))
            if cand is not None and len(text) - len(cand) < n:
                return  # 标点离尾太近：整条不喂（不开火、不烧目击/预算）
        super().on_interim(text)


async def _synth_pcm(tts_provider, text: str, log) -> bytes | None:
    """投机译文 TTS 全量排干成 PCM（不进 say 队列，零播放；旧线同纪律）。

    合成走会话同一个 tts_provider（音色/模型/语速按会话装配天然同源——
    confirm 播放的 audio 无需再对缓存 key，PCM 即真值）。失败=slot 永不
    ready，静默 MISS。"""
    buf = bytearray()
    try:
        stream = tts_provider.synthesize(text)
        async with stream:
            async for ev in stream:
                frame = getattr(ev, "frame", None)
                data = getattr(frame, "data", None)
                if data is not None:
                    buf.extend(data.tobytes() if isinstance(data, memoryview) else bytes(data))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 投机合成失败零影响
        log(f"[interp] INTERP_SPEC synth failed: {exc!r}")
        return None
    return bytes(buf) or None


def build(pipeline, *, tts_provider, run_mt, stats: dict | None = None, log=print):
    """装配 lite 投机控制器（返回 None=总闸关或 text-only——调用方零动作=旧路径）。

    ``run_mt``：async (span) -> str，MT 排干（pipeline 供与主路径同源实现）；
    空=投机未成。held PCM 经 ``tts_provider.synthesize`` 排干（会话同源）。"""
    if not _spec_mt_enabled() or tts_provider is None:
        return None
    if not hasattr(pipeline.session, "say"):
        return None

    async def _run_spec(span: str) -> tuple[str, bytes]:
        translated = await run_mt(span)
        if not translated:
            return "", b""
        pcm = await _synth_pcm(tts_provider, pipeline._final_text(translated), log)
        if not pcm:
            return "", b""
        return translated, pcm

    def _busy() -> bool:
        depth = _spec_busy_depth()
        busy = bool(
            pipeline.q.qsize() >= depth
            or pipeline.mt_busy["flag"]
            or pipeline.lane_dead["reason"]
        )
        st = stats
        if st is not None:
            if busy:
                st["blocked"] = st.get("blocked", 0) + 1
                if not st.get("was"):
                    st["was"] = True
                    log(
                        f"[interp] INTERP_SPEC busy depth={depth} fifo={pipeline.q.qsize()} "
                        f"mt={int(bool(pipeline.mt_busy['flag']))} "
                        f"dead={int(bool(pipeline.lane_dead['reason']))} "
                        f"fired={st.get('fired', 0)} blocked={st.get('blocked', 0)}"
                    )
            else:
                st["was"] = False
        return busy

    def _say_cached(final_src: str, text: str, pcm: bytes) -> None:
        """HIT 直播：held PCM 走 say(audio=frames) 零合成（qa_gate 罐头车同构）。
        先 say 后记账：say 失败不留 pending 孤儿；投机轮 mt_ms=0（旧线同口径）。"""
        from ..tts_cache import frames_aiter, pcm_to_frames

        pipeline.session.say(
            pipeline._final_text(text),
            audio=frames_aiter(pcm_to_frames(pcm, tts_provider.sample_rate)),
        )
        pipeline.last_ms["ms"] = 0
        pipeline.lag.note_src(final_src)
        pipeline.lag.done_mt(0)

    return LiteSpecController(
        enabled=True,
        detector=_SpecMtDetector(),
        hold=_SpecMtHold(),
        run_spec=_run_spec,
        busy_gate=_busy,
        say_cached=_say_cached,
        enqueue=pipeline.enqueue_raw,
        stats=stats,
        log=log,
    )


def run_mt_factory(mt, instructions: str, pairs, collect, timeout_s: float):
    """投机 MT 排干体（与主路径同源 ``_collect``/``build_messages``；滚动对共享）。

    返回 async (span) -> str；cancel 语义随流关闭（本地车道 aclose 即 abort，
    云端纯丢弃——旧线同注）。"""

    async def _run(span: str) -> str:
        msgs = build_messages(instructions, list(pairs), span)
        return await collect(mt, msgs, timeout_s)

    return _run
