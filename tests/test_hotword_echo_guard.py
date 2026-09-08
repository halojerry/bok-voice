"""ASR 热词幻听守卫单测:词表顺串判定(2026-09-08 call-feaf914c 实机回归)。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _is_hotword_echo  # noqa: E402

_CTX = (
    "Vocabulary: 單號, 運單, 賠償, 運費, 專員, 集運, 時效, 上門, 追蹤, 核實, "
    "WhatsApp, 微信, 顺丰速递"
)


def test_vocab_string_is_echo():
    # 实机回归原句:词表整串被抄成转写(标点任意)
    assert _is_hotword_echo(
        "單號，運單，賠償，運費，專員，集運，時效，上門，追蹤，核實，WhatsApp，微信，顺丰速递", _CTX
    )
    # 部分顺串也拦(≥6 字)
    assert _is_hotword_echo("單號運單賠償", _CTX)
    assert _is_hotword_echo("追蹤核實WhatsApp微信", _CTX)


def test_real_speech_not_echo():
    assert not _is_hotword_echo("我個單號係三七七八九零", _CTX)  # 真报单号(数字非词表词)
    assert not _is_hotword_echo("對對對，係我", _CTX)
    assert not _is_hotword_echo("你哋幾時送到呀", _CTX)
    assert not _is_hotword_echo("我唔記得咗囉，點算呀", _CTX)


def test_short_and_empty_not_echo():
    assert not _is_hotword_echo("好的", _CTX)  # <6 字唔拦
    assert not _is_hotword_echo("", _CTX)
    assert not _is_hotword_echo("單號運單", "Vocabulary: 單號, 運單")  # 凑不足 6 字


def test_empty_context_disabled():
    assert not _is_hotword_echo("單號運單賠償", "")


def test_guard_kill_switch(monkeypatch):
    from agent_runtime import agent as agent_mod

    monkeypatch.setenv("QWEN3_HOTWORD_ECHO_GUARD", "0")
    assert agent_mod._hotword_echo_guard_enabled() is False
    monkeypatch.setenv("QWEN3_HOTWORD_ECHO_GUARD", "1")
    assert agent_mod._hotword_echo_guard_enabled() is True
