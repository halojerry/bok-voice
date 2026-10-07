"""I1c（2026-10-03）host 门：云端车道不发 prefix prewarm / warmup。

2026-10-06 修订（A 线对偶件）：投机预热（PrefillSpeculator）云车道放行——
DeepSeek 端点 + BOK_PREFILL_SPEC_CLOUD=1（缺省）判 lane="cloud"（DeepSeek
服务端自动前缀缓存吃同款 max_tokens=1 预热；成本护栏=
prefill_speculator.cloud_budget_verdict 云臂专属）；BOK_PREFILL_SPEC_CLOUD=0
回旧 host 门逐字节（云档零发射）。会话首轮真实前缀预热（LLM_PREFIX_PREWARM）
与 warmup 仍本地专属（`_llm_prewarm_local(_raw_llm)` pin 不变）。

routing openai 档 = 同款 MlxLlmLLM + 云 base_url。行为面：agent
`_llm_prewarm_local`（首轮预热本地门）+ `prefill_speculator.lane_for_llm_provider`
（投机预热车道判定）+ plugins `_is_local_mlx_url`（纯 host 谓词，与 abort env
解耦）；源级 pin 钉三个消费点（首轮 prewarm 条件 / speculator 车道判定 /
`_prewarm_impl` 门）。
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
    # 首轮真实前缀预热仍本地专属（I1c 原判,云放行不扩到此件）;
    # 投机预热已换车道判定单点（prefill_speculator.lane_for_llm_provider）。
    assert "_llm_prewarm_local(_raw_llm)" in AGENT_SRC
    assert "lane_for_llm_provider(llm_provider)" in AGENT_SRC
    assert "_is_local_mlx_url(self._bok_abort_base)" in PLUGINS_SRC


def test_prefill_lane_for_llm_provider(monkeypatch):
    """投机预热车道判定：本机=local 恒定;DeepSeek 缺省=cloud / =0 回旧门;
    非 DeepSeek 云端点两档都不放;读不到底=保守 local。"""
    from agent_runtime.prefill_speculator import lane_for_llm_provider

    local = SimpleNamespace(_client=SimpleNamespace(base_url="http://127.0.0.1:1235/v1/"))
    deepseek = SimpleNamespace(_client=SimpleNamespace(base_url="https://api.deepseek.com/v1"))
    other = SimpleNamespace(_client=SimpleNamespace(base_url="https://api.openai.com/v1"))
    monkeypatch.delenv("BOK_PREFILL_SPEC_CLOUD", raising=False)
    assert lane_for_llm_provider(local) == "local"
    assert lane_for_llm_provider(deepseek) == "cloud", "缺省(未设)=云档放行"
    assert lane_for_llm_provider(other) == ""
    monkeypatch.setenv("BOK_PREFILL_SPEC_CLOUD", "0")
    assert lane_for_llm_provider(deepseek) == "", "=0 回旧 host 门(云档零发射)"
    assert lane_for_llm_provider(local) == "local"
    # 读不到底（替身/嵌入方）=保守 local（旧 _llm_prewarm_local True 同款）
    assert lane_for_llm_provider(SimpleNamespace()) == "local"
    assert lane_for_llm_provider(object()) == "local"
