"""LLM 饥荒自适应单测（第十五波,2026-10-01,call-dc54f542 根修）。

物理账（实弹 call-dc54f542 23:01 窗）：机器级首 token 慢（swap 26GB 挤压,
实测 3-25s,健康档 0.2-0.6s）时,健康档常数全是负贡献——原流 7.9s 本有答案,
被 3s 超时+8s drain 判死→abort→regen 全量重 prefill（负载×2）→25.3s>22s
传输超时→客户 37s 零答案;干等原流 10s 即有答案。

自适应=TTFT EMA（worker 级跨通话共享）≥BOK_LLM_FAMINE_TTFT_S(默认 4s)时:
首 token 超时拉长（默认 15s）、drain 拉长（默认 15s）、禁 regen。快样本把
EMA 拉回=自动复原。kill-switch BOK_LLM_FAMINE=0 整闸关。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "apps" / "agent"))

from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    MlxLlmLLM,
    _famine_reset_for_tests,
    _late_answer_deadline_s,
    llm_famine_active,
    record_llm_first_token,
)


def _first_token_timeout_s() -> float:
    return MlxLlmLLM._first_token_timeout_s()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("BOK_LLM_FAMINE", raising=False)
    monkeypatch.delenv("BOK_LLM_FAMINE_TTFT_S", raising=False)
    monkeypatch.delenv("BOK_LLM_FAMINE_FIRST_S", raising=False)
    monkeypatch.delenv("BOK_LLM_FAMINE_DRAIN_S", raising=False)
    monkeypatch.delenv("LLM_FIRST_TOKEN_TIMEOUT_S", raising=False)
    monkeypatch.delenv("LLM_LATE_ANSWER_DEADLINE_S", raising=False)
    _famine_reset_for_tests()
    yield
    _famine_reset_for_tests()


# ------------------------------------------------------------------ 信号面


def test_ema_activates_after_two_slow_samples():
    record_llm_first_token(6.0)
    assert llm_famine_active() is False, "单样本不判定（防孤例）"
    record_llm_first_token(6.0)
    assert llm_famine_active() is True


def test_ema_recovery_via_fast_samples():
    record_llm_first_token(8.0)
    record_llm_first_token(8.0)
    assert llm_famine_active() is True
    for _ in range(6):
        record_llm_first_token(0.3)  # 快样本把 EMA 拉回
    assert llm_famine_active() is False, "健康样本应自动复原"


def test_timeout_sample_counts_as_famine_signal():
    """超时轮喂 timeout×2 深饥荒样本(2026-10-02 审计修)——两轮超时即进饥荒。

    旧版喂 timeout 本身(3.0),EMA 上限=3.0 < 4.0 阈值,饥荒**数学上永不
    激活**(十五波修复自始是死代码——call-dc54f542 的病态链:3s 超时→drain
    8s→abort→regen 同参全量重 prefill 负载×2,在 swap 抖动机上循环)。
    修后:超时轮=6.0 样本,两轮 EMA=6.0 ≥ 4.0 → 饥荒激活(拉长超时/drain、
    禁 regen,等原流优于重来);快样本仍可自动复原。"""
    record_llm_first_token(6.0)  # 超时轮的真实样本(timeout 3.0 × 2)
    assert llm_famine_active() is False, "单样本未达 n≥2"
    record_llm_first_token(6.0)
    assert llm_famine_active() is True, "两轮首 token 超时=机器级病态,必须进饥荒"
    for _ in range(6):
        record_llm_first_token(0.3)  # 快样本把 EMA 拉回
    assert llm_famine_active() is False, "健康样本应自动复原"


# ------------------------------------------------------------------ 常数面


def test_first_token_timeout_stretched_under_famine():
    record_llm_first_token(9.0)
    record_llm_first_token(9.0)
    assert _first_token_timeout_s() == 15.0


def test_drain_deadline_stretched_under_famine():
    record_llm_first_token(9.0)
    record_llm_first_token(9.0)
    assert _late_answer_deadline_s() == 15.0


def test_healthy_state_keeps_stock_constants():
    record_llm_first_token(0.3)
    record_llm_first_token(0.3)
    assert _first_token_timeout_s() == 3.0
    assert _late_answer_deadline_s() == 8.0


def test_kill_switch_disables_everything(monkeypatch):
    record_llm_first_token(9.0)
    record_llm_first_token(9.0)
    assert llm_famine_active() is True
    monkeypatch.setenv("BOK_LLM_FAMINE", "0")
    assert llm_famine_active() is False
    assert _first_token_timeout_s() == 3.0
    assert _late_answer_deadline_s() == 8.0


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("BOK_LLM_FAMINE_TTFT_S", "2")
    monkeypatch.setenv("BOK_LLM_FAMINE_FIRST_S", "20")
    monkeypatch.setenv("BOK_LLM_FAMINE_DRAIN_S", "25")
    record_llm_first_token(2.5)
    record_llm_first_token(2.5)
    assert llm_famine_active() is True
    assert _first_token_timeout_s() == 20.0
    assert _late_answer_deadline_s() == 25.0


def test_explicit_config_beats_famine_floor(monkeypatch):
    """显式配了更大的健康档常数 → 饥荒档不缩短它（max 语义）。"""
    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT_S", "18")
    record_llm_first_token(9.0)
    record_llm_first_token(9.0)
    assert _first_token_timeout_s() == 18.0


# ------------------------------------------------------------------ regen 禁令


def test_regen_skipped_under_famine(capsys):
    from agent_runtime.providers.livekit_plugins import _LlmFallbackStream

    record_llm_first_token(9.0)
    record_llm_first_token(9.0)
    st = _LlmFallbackStream.__new__(_LlmFallbackStream)
    st._spawn_regen()
    out = capsys.readouterr().out
    assert "LLM_FAMINE regen_skipped" in out


def test_regen_runs_when_healthy(capsys):
    from agent_runtime.providers.livekit_plugins import _LlmFallbackStream

    record_llm_first_token(0.3)
    record_llm_first_token(0.3)
    spawned: list = []
    st = _LlmFallbackStream.__new__(_LlmFallbackStream)
    st._spawn_attached = lambda coro: (spawned.append(coro), coro.close())
    st._spawn_regen()
    assert len(spawned) == 1, "健康态照常重生"
    assert "regen_skipped" not in capsys.readouterr().out


# ------------------------------------------------------------------ 立法 pin


def test_famine_env_keys_in_forward_env():
    import tools.bok as bok

    for k in ("BOK_LLM_FAMINE", "BOK_LLM_FAMINE_TTFT_S",
              "BOK_LLM_FAMINE_FIRST_S", "BOK_LLM_FAMINE_DRAIN_S"):
        assert k in bok.env._FORWARD_ENV, k


def test_source_pin_timeout_branch_records_sample():
    """超时分支必须喂**深饥荒样本**(timeout×2)——源级 pin 防重构丢。

    2026-10-02 审计修:旧版喂 timeout 本身(3.0),EMA 上限=3.0 < 激活线 4.0,
    十五波饥荒自适应从未激活过(纯死代码)。×2 语义=首 token 超 3s 的轮代表
    ≥2×deadline 的机器级病态;两轮超时即应进饥荒(见行为测试)。"""
    src = (_REPO / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text(encoding="utf-8")
    i = src.index("if first_task not in done:")
    j = src.index("self._late_deadline > 0", i)
    assert "record_llm_first_token(timeout * 2.0)" in src[i:j]
