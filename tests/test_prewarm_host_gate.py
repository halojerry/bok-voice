"""I1c（2026-10-03）host 门：云端车道不发 prefix prewarm / 投机预热 / warmup。

routing openai 档 = 同款 MlxLlmLLM + 云 base_url——isinstance/hasattr 门放行，
但云端无本地 KV 语义。行为面：agent `_llm_prewarm_local`（内芯 base_url 判据）
+ plugins `_is_local_mlx_url`（纯 host 谓词，与 abort env 解耦）；源级 pin 钉
三个消费点（prefix prewarm 主题条件 / speculator 条件 / `_prewarm_impl` 门）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _llm_prewarm_local  # noqa: E402
from agent_runtime.providers.livekit_plugins import _is_local_mlx_url  # noqa: E402

AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
PLUGINS_SRC = (
    ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
).read_text(encoding="utf-8")


def test_llm_prewarm_local_judges_client_base_url():
    local = SimpleNamespace(_client=SimpleNamespace(base_url="http://127.0.0.1:1235/v1/"))
    cloud = SimpleNamespace(_client=SimpleNamespace(base_url="https://api.deepseek.com/v1"))
    assert _llm_prewarm_local(local)
    assert not _llm_prewarm_local(cloud)
    # 读不到底（替身/嵌入方）=保守 True（本地为主，零行为变化）
    assert _llm_prewarm_local(SimpleNamespace())
    assert _llm_prewarm_local(object())


def test_is_local_mlx_url_pure_host_predicate():
    assert _is_local_mlx_url("http://127.0.0.1:1237/v1")
    assert _is_local_mlx_url("http://localhost:1238/v1")
    assert not _is_local_mlx_url("https://api.deepseek.com/v1")
    assert not _is_local_mlx_url("")
    assert not _is_local_mlx_url("garbage")


def test_prewarm_host_gate_source_pins():
    assert "_llm_prewarm_local(_raw_llm)" in AGENT_SRC
    assert "_llm_prewarm_local(llm_provider)" in AGENT_SRC
    assert "_is_local_mlx_url(self._bok_abort_base)" in PLUGINS_SRC
