"""P1b 增量 /api/finish 单测:新鲜 partial → 只解码尾巴拼接;各安全阀回退整句。

不依赖真实模型:importlib 载 sidecar(同 test_wave_b_llm_asr_usage 的 fake-model
模式),按输入长度返回不同文本,记录每次 generate 喂进的样本数。
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
import types
from pathlib import Path
from threading import Lock

os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")
os.environ.setdefault("QWEN3_ASR_STREAM", "1")

ROOT = Path(__file__).resolve().parents[1]


def _load_sidecar_app():
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_incr", ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PARTIAL_TEXT = "有冇人知道灣仔活道"  # CJK 结尾:接缝安全
TAIL_TEXT = "係點去㗎"
VOICED = b"\x00\x19"  # int16 6400(≈0.19 振幅):RMS 高于静音门限,trim 唔会裁


class _FakeModel:
    """按样本数分流:长输入(整段/partial)回 PARTIAL_TEXT,短输入(尾巴)回 TAIL_TEXT。
    digit 模式下短输入回含数字文本,模拟 WhatsApp 号码落在尾段。"""

    def __init__(self, digit_tail: bool = False):
        self.calls: list[dict] = []
        self.digit_tail = digit_tail

    def generate(self, wav, language=None, max_tokens=256):
        self.calls.append({"samples": len(wav), "language": language})
        if len(wav) <= 16000:  # ≤1s:增量尾巴窗
            text = "單號 12345" if self.digit_tail else TAIL_TEXT
        else:
            text = PARTIAL_TEXT
        return types.SimpleNamespace(text=text, language=["Cantonese"])


def _make_svc(mod, model: _FakeModel):
    svc = mod.ASRService()
    svc._model = model
    return svc


def _run_partial(mod, svc, sid: str, pcm: bytes) -> None:
    """喂一窗音频并强制 partial 立即推理(节流窗口清零)。"""
    svc._sessions[sid]["last_partial_at"] = 0.0
    out = svc.chunk(sid, pcm)
    assert out["partial"] is True and out["text"] == PARTIAL_TEXT


def test_fresh_partial_finish_decodes_tail_only():
    """新鲜 partial 已覆盖 buffer 主体 → finish 只解码尾部小段,FINAL=拼接结果。"""
    mod = _load_sidecar_app()
    model = _FakeModel()
    svc = _make_svc(mod, model)

    sid = svc.start(language="cantonese")
    body = VOICED * (16000 * 2)  # 2s(全零静音:trim 不裁整段静音)
    _run_partial(mod, svc, sid, body)
    assert model.calls[0]["samples"] == 32000  # partial 覆盖整段

    # 尾巴 0.5s:模拟 END_OF_SPEECH 后 agent 补传的 finish body。
    tail = VOICED * 8000
    svc._sessions[sid]["chunks"].extend(tail)
    final = svc.finish(sid)

    assert final["partial"] is False
    assert final["text"] == PARTIAL_TEXT + TAIL_TEXT  # CJK 边界直接拼接
    assert final["language"] == "Cantonese"
    # 关键断言:第二次 generate 只喂 0.5s 尾巴(8000 样本),唔重解码整段。
    assert len(model.calls) == 2
    assert model.calls[1]["samples"] == 8000
    assert model.calls[1]["language"] == "cantonese"


def test_fresh_partial_zero_tail_returns_partial_directly():
    """尾巴为零(最后一窗已覆盖全部音频)→ partial 直接转正,零额外解码。"""
    mod = _load_sidecar_app()
    model = _FakeModel()
    svc = _make_svc(mod, model)

    sid = svc.start(language="cantonese")
    _run_partial(mod, svc, sid, VOICED * (16000 * 2))
    final = svc.finish(sid)
    assert final["partial"] is False and final["text"] == PARTIAL_TEXT
    assert len(model.calls) == 1  # 只有 partial 那一窗


def test_stale_partial_finish_falls_back_to_whole_decode():
    """partial 不新鲜(> FINISH_PARTIAL_FRESH_SEC)→ 整句重解码兜底。"""
    mod = _load_sidecar_app()
    model = _FakeModel()
    svc = _make_svc(mod, model)

    sid = svc.start(language="cantonese")
    body = VOICED * (16000 * 2)
    _run_partial(mod, svc, sid, body)
    # 假装 partial 是 10s 前跑的:接缝早已过期。
    svc._sessions[sid]["last_partial_at"] = time.monotonic() - 10
    svc._sessions[sid]["chunks"].extend(VOICED * 8000)

    final = svc.finish(sid)
    assert final["partial"] is False and final["text"] == PARTIAL_TEXT
    assert len(model.calls) == 2
    assert model.calls[1]["samples"] == 40000  # 整段 2.5s = 40000 样本


def test_digit_in_tail_falls_back_to_whole_decode():
    """尾段文本含数字(WhatsApp 号码高危)→ 丢弃尾段结果,整句高精度兜底。"""
    mod = _load_sidecar_app()
    model = _FakeModel(digit_tail=True)
    svc = _make_svc(mod, model)

    sid = svc.start(language="cantonese")
    body = VOICED * (16000 * 2)
    _run_partial(mod, svc, sid, body)
    svc._sessions[sid]["chunks"].extend(VOICED * 8000)

    final = svc.finish(sid)
    # FINAL = 整句解码结果(唔拼接带数字的尾段)。尾段探测 + 整句兜底 = 3 次调用,
    # 最后一次必须喂整段(2.5s=40000 样本)。
    assert final["partial"] is False and final["text"] == PARTIAL_TEXT
    assert len(model.calls) == 3
    assert model.calls[-1]["samples"] == 40000


def test_seam_after_latin_char_falls_back_to_whole_decode():
    """接缝劈在 latin 词上(partial 尾字符是字母/数字)→ 不可验证,整句兜底。"""
    mod = _load_sidecar_app()
    model = _FakeModel()
    svc = _make_svc(mod, model)

    sid = svc.start(language="cantonese")
    body = VOICED * (16000 * 2)
    _run_partial(mod, svc, sid, body)
    # partial 尾字符是 latin(o)→ 词可能被缝劈开,文本侧无法验证 → 整句兜底。
    svc._sessions[sid]["partial_text"] = "確認訂單 no"
    svc._sessions[sid]["chunks"].extend(VOICED * 8000)
    final = svc.finish(sid)
    assert final["text"] == PARTIAL_TEXT  # 整句解码结果
    assert model.calls[1]["samples"] == 40000


def test_lock_busy_finish_falls_back_to_whole_decode():
    """inf_lock 忙超过小等窗口(还有 partial 在飞)→ 唔等,整句兜底。"""
    mod = _load_sidecar_app()
    mod.FINISH_LOCK_WAIT_SEC = 0.05  # 测试加速
    model = _FakeModel()
    svc = _make_svc(mod, model)

    sid = svc.start(language="cantonese")
    body = VOICED * (16000 * 2)
    _run_partial(mod, svc, sid, body)
    svc._sessions[sid]["chunks"].extend(VOICED * 8000)
    # 占住推理锁:模拟另一个 partial 窗还在 GPU 上跑。
    # (finish 会 pop 会话,先攞住锁引用再 finish。)
    busy_lock = Lock()
    busy_lock.acquire()
    svc._sessions[sid]["inf_lock"] = busy_lock
    try:
        final = svc.finish(sid)
    finally:
        busy_lock.release()
    assert final["partial"] is False and final["text"] == PARTIAL_TEXT
    assert model.calls[1]["samples"] == 40000  # 整句


def test_inc_finish_disabled_goes_whole_decode():
    """QWEN3_ASR_INC_FINISH=0 → 一键回退整句重解码(快速回退档)。"""
    mod = _load_sidecar_app()
    mod.INC_FINISH = False
    model = _FakeModel()
    svc = _make_svc(mod, model)

    sid = svc.start(language="cantonese")
    body = VOICED * (16000 * 2)
    _run_partial(mod, svc, sid, body)
    svc._sessions[sid]["chunks"].extend(VOICED * 8000)
    final = svc.finish(sid)
    assert final["text"] == PARTIAL_TEXT
    assert model.calls[1]["samples"] == 40000


def test_trailing_silence_trimmed_before_decode():
    """EOT 卫生:finish buffer 尾部静音先裁掉再解码(整句路径同样受益)。"""
    mod = _load_sidecar_app()
    model = _FakeModel()
    svc = _make_svc(mod, model)

    sid = svc.start(language="cantonese")
    loud = (b"\x00\x19" * 16000)[: 16000 * 2]  # 1s 恒定振幅「语音」
    silence = b"\x00\x00" * 16000  # 1s 静音尾巴
    svc.chunk(sid, loud + silence)
    final = svc.finish(sid)
    assert final["partial"] is False
    # generate 只喂 1s 语音段(16000 样本),1s 静音尾巴被裁。
    assert model.calls[-1]["samples"] == 16000


def test_finish_body_arriving_mid_partial_does_not_lose_tail():
    """竞态回归:finish body 喺 partial 解码期间(锁外)追加进 chunks。

    covered 必须记「解码快照长度」(generate 前拍照)而非解码完成后的当刻
    buffer 长度——否则多认覆盖 → finish 见 covered==len(pcm) → 尾巴被当已解码
    直接转正 partial,尾段语音(可能含号码)静默丢失。
    """
    mod = _load_sidecar_app()
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    class _BlockingModel:
        def generate(self, wav, language=None, max_tokens=256):
            calls.append(len(wav))
            if len(wav) > 16000:  # partial 窗:解码中途卡住,模拟 GPU 在烧
                started.set()
                release.wait(timeout=5)
                return types.SimpleNamespace(text=PARTIAL_TEXT, language=["Cantonese"])
            return types.SimpleNamespace(text=TAIL_TEXT, language=["Cantonese"])

    svc = mod.ASRService()
    svc._model = _BlockingModel()

    sid = svc.start(language="cantonese")
    worker = threading.Thread(
        target=svc.chunk, args=(sid, VOICED * (16000 * 2)), daemon=True
    )
    worker.start()
    assert started.wait(timeout=5), "partial 解码应已开始并被卡住"
    # /api/finish 的 PCM body 喺端点锁外追加(partial 仲持住 inf_lock)。
    svc._sessions[sid]["chunks"].extend(VOICED * 8000)
    release.set()
    worker.join(timeout=5)

    # 关键断言:covered=快照 2s(64000B),唔系追加后的 80000B。
    assert svc._sessions[sid]["partial_covered"] == 64000

    final = svc.finish(sid)
    assert final["partial"] is False
    assert final["text"] == PARTIAL_TEXT + TAIL_TEXT  # 尾巴有解码、有拼接
    assert calls[-1] == 8000  # 尾段 0.5s 有真实送去解码


def test_trimmed_window_partial_keeps_covered_none():
    """>PARTIAL_MAX_SEC 裁剪窗:partial 只解尾部 25s,covered 必须保持 None
    (增量让位整句兜底),解码完成后的赋值唔得翻案。"""
    mod = _load_sidecar_app()
    mod.PARTIAL_MAX_SEC = 5.0  # 收紧便于构造
    calls: list[dict] = []

    class _Model:
        def generate(self, wav, language=None, max_tokens=256):
            calls.append(len(wav))
            return types.SimpleNamespace(text=PARTIAL_TEXT, language=["Cantonese"])

    svc = mod.ASRService()
    svc._model = _Model()
    sid = svc.start(language="cantonese")
    svc._sessions[sid]["last_partial_at"] = 0.0
    svc.chunk(sid, VOICED * (16000 * 6))  # 6s > 5s 上限 → 裁剪窗
    assert svc._sessions[sid]["partial_covered"] is None
    final = svc.finish(sid)
    assert final["text"] == PARTIAL_TEXT  # 整句兜底结果
    assert calls[-1] == 16000 * 6  # 整句解码喂全 buffer(头段 partial 从未见过)
