"""ASR 受限润色层接线测试（2026-09-27，三语纠错层）。

两层：单元（asr_polish_runtime 的门控/吸附/CSC 皮带）+ 源级 pin（agent.py
预计算点、ContextAwareLLM 冻结点、live STT 置信度暴露、_FORWARD_ENV 四键）。
冻结点的请求侧行为由源级 pin 钉住（ContextAwareLLM.chat 构造完整会话过重，
仓里 prompt-cache 相关测试同款先例）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 源级 pin

def test_agent_prewires_polish_in_turn_handler():
    src = (_REPO / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert "from .asr_polish_runtime import polish_turn" in src
    assert "await polish_turn(" in src
    assert "context_state.set_polished(user_text, _polished)" in src


def test_contextaware_freeze_applies_polish():
    src = (
        _REPO / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
    ).read_text(encoding="utf-8")
    # 冻结点三处都走 _polish_body（新消息首拼尾部 / 重锚定 / revision 重渲染）
    assert src.count("_polish_body(") >= 4  # 定义 1 + 消费 3
    assert "self._ctx.polished_of(raw)" in src
    # 原文单轨：账本键恒为 raw（record/rewrite 传 orig/actual，不是 polished）
    assert "self._ctx.record_applied_tail(orig, final)" in src
    assert "self._ctx.rewrite_last_applied_tail(actual, rebased)" in src


def test_live_stt_exposes_confidence():
    src = (
        _REPO / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
    ).read_text(encoding="utf-8")
    assert "self._stt_.last_confidence = conf if isinstance(conf, dict) else None" in src
    assert "self.last_confidence: dict | None = None" in src


def test_forward_env_carries_polish_keys():
    src = (_REPO / "tools" / "bok.py").read_text(encoding="utf-8")
    for key in ("BOK_ASR_POLISH", "BOK_CSC_SIDECAR", "BOK_CSC_URL", "BOK_CSC_CONF_GATE"):
        assert f'"{key}"' in src, key


def test_hotword_context_label_removed():
    """「Vocabulary:」标签 2026-09-27 A/B 砍除（裸 join 7/10 vs 带标签 6/10、
    三处证据同向）——源级 pin 防回潮。回声守卫兼容两形态（先 replace 再切词），
    旧带标签串作守卫输入仍可解析。"""
    src = (_REPO / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert 'prefix = "Vocabulary: "' not in src


# ------------------------------------------------------------ 单元：门控

def test_polish_env_master_switch(monkeypatch):
    from agent_runtime import asr_polish_runtime as rt

    monkeypatch.delenv("BOK_ASR_POLISH", raising=False)
    assert rt.polish_enabled() is True
    monkeypatch.setenv("BOK_ASR_POLISH", "0")
    assert rt.polish_enabled() is False
    # 关档=直通，表命中也不动
    assert rt.sync_polish("我俾乍骗咗", "zh") == "我俾乍骗咗"


def test_sync_polish_snaps_and_freezes_digits(monkeypatch):
    from agent_runtime import asr_polish_runtime as rt

    monkeypatch.delenv("BOK_ASR_POLISH", raising=False)
    # zh 表：乍骗 → 诈骗（资产抽样已验证的映射）
    out = rt.sync_polish("我俾乍骗咗", "zh")
    assert "诈骗" in out
    # 中文数字词零改动（数字零降级）
    out2 = rt.sync_polish("乍骗单号三七七八九零", "zh")
    assert "三七七八九零" in out2
    # 无命中原样返回
    assert rt.sync_polish("你地公司係唔係香港既", "zh") == "你地公司係唔係香港既"


def test_csc_gate_matrix(monkeypatch):
    from agent_runtime import asr_polish_runtime as rt

    monkeypatch.setenv("BOK_CSC_SIDECAR", "0")
    assert rt._csc_should_call("zh", "这个方按需要我承担运费吗", None) is False
    monkeypatch.setenv("BOK_CSC_SIDECAR", "1")
    assert rt._csc_should_call("zh", "这个方按需要我承担运费吗", None) is True
    # 高置信不过门；低置信过门
    assert rt._csc_should_call("zh", "这个方按需要我承担运费吗", {"mean": 0.9}) is False
    assert rt._csc_should_call("zh", "这个方按需要我承担运费吗", {"mean": 0.2}) is True
    # 粤语/英语车道不进模型层；过短/过长拒
    assert rt._csc_should_call("cantonese", "你地公司係唔係香港既", None) is False
    assert rt._csc_should_call("en", "hello there", None) is False
    assert rt._csc_should_call("zh", "好的", None) is False
    assert rt._csc_should_call("zh", "x" * 201, None) is False


# ------------------------------------------------------ 单元：polish_turn

def _fake_csc(payload):
    async def _call(text: str):
        return payload

    return _call


def test_polish_turn_applies_csc_when_gated(monkeypatch):
    from agent_runtime import asr_polish_runtime as rt

    monkeypatch.delenv("BOK_ASR_POLISH", raising=False)
    monkeypatch.setenv("BOK_CSC_SIDECAR", "1")
    monkeypatch.setattr(
        rt, "_csc_post",
        _fake_csc({"text": "这个方案需要我承担运费吗", "edits": [{"pos": 2, "from": "按", "to": "案"}]}),
    )
    out = asyncio.run(rt.polish_turn("这个方按需要我承担运费吗", "zh", None))
    assert out == "这个方案需要我承担运费吗"


def test_polish_turn_drops_csc_digit_drift(monkeypatch):
    from agent_runtime import asr_polish_runtime as rt

    monkeypatch.delenv("BOK_ASR_POLISH", raising=False)
    monkeypatch.setenv("BOK_CSC_SIDECAR", "1")
    # CSC 结果动了数字（三七七八九零 → 三七七八九一）→ 皮带丢弃整条
    monkeypatch.setattr(
        rt, "_csc_post",
        _fake_csc({"text": "单号三七七八九一", "edits": [{"pos": 0, "from": "零", "to": "一"}]}),
    )
    out = asyncio.run(rt.polish_turn("单号三七七八九零", "zh", None))
    assert out == "单号三七七八九零"


def test_polish_turn_csc_failure_fail_open(monkeypatch):
    from agent_runtime import asr_polish_runtime as rt

    monkeypatch.delenv("BOK_ASR_POLISH", raising=False)
    monkeypatch.setenv("BOK_CSC_SIDECAR", "1")

    async def _boom(text: str):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(rt, "_csc_post", _boom)
    out = asyncio.run(rt.polish_turn("这个方按需要我承担运费吗", "zh", None))
    assert out == "这个方按需要我承担运费吗"


# ------------------------------------------------- 单元：ContextState 映射

def test_context_state_polished_map():
    from agent_runtime.providers.livekit_plugins import ContextState

    cs = ContextState()
    cs.set_user_language("zh")
    assert cs.user_language == "zh"
    assert cs.polished_of("乍骗") is None
    cs.set_polished("乍骗", "诈骗")
    assert cs.polished_of("乍骗") == "诈骗"
    # 同 raw 不覆盖（确定性契约）；相同文本不进 map
    cs.set_polished("乍骗", "别的")
    assert cs.polished_of("乍骗") == "诈骗"
    cs.set_polished("一样", "一样")
    assert cs.polished_of("一样") is None
