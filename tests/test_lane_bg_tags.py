"""I1（2026-10-03）bg 车道标记源级 pin：prewarm/warmup 出站请求带 X-Bok-Lane: bg。

:1237 前门闸（:1238）与 :1235 闸同判 X-Bok-Lane——交互回复（MlxLlmLLM 缺省
setdefault "reply"）优先，后台预热（prefix_prewarm / 投机 prefill / _prewarm_impl）
标 bg 排队。行为侧由 tests/test_llm_queue_proxy.py 的 lane 透传用例覆盖，
此处钉两处字面量与 bok.py 闸门接线防重构静默漂移。
"""

from pathlib import Path

from _bok_src import bok_source

ROOT = Path(__file__).resolve().parents[1]

PLUGINS = (
    ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
).read_text(encoding="utf-8")
BOK = bok_source()


def test_prewarm_and_warmup_tag_bg_lane():
    assert PLUGINS.count('"X-Bok-Lane": "bg"') >= 2
    # prefix_prewarm 的 req-id 与 lane 同头共存（取消即 abort 语义不丢）
    assert '_MLX_REQ_ID_HEADER: req_id, "X-Bok-Lane": "bg"' in PLUGINS
    # a_reply 缺省 reply 车道不受影响（setdefault 语义）
    assert 'headers.setdefault("X-Bok-Lane", "reply")' in PLUGINS


def test_bok_settle_gate_wiring_pins():
    # 前门闸端口与上游映射（:1238→:1237）在 bok.py 单点
    assert '"BOK_LLM_QUEUE_PORT": "1238"' in BOK
    assert '"BOK_LLM_QUEUE_UPSTREAM": "http://127.0.0.1:1237"' in BOK
    assert "settle-proxy.pid" in BOK
    assert "_settle_gate_url()" in BOK
