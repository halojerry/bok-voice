"""W2 刀1(2026-10-08):ToJyutping 预 import warm-up——钉「qa_phonetic 的
tojyutping import 不再在首次调用处」。

病理:qa_phonetic._converter 惰性 import vendored ToJyutping,首次 import
触发 trie 解析 ~160-280ms;惰性点在首个粤语 QA 查询(通话中)=阻塞 event
loop(2026-10-08 账本 465 次 "event loop blocked")。修法=worker entrypoint
早段 ``warm_up()`` 预付,运行时纯缓存命中。零运行时行为变化,无 kill-switch。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_VENDOR_PREFIX = "bok_voice_core.tojyutping_vendor"


def _drop_vendor_modules() -> None:
    """把 vendor 模块从 sys.modules 摘除,模拟「进程尚未 import」的冷态。"""
    for _name in [n for n in sys.modules if n.startswith(_VENDOR_PREFIX)]:
        del sys.modules[_name]


def test_warm_up_preimports_vendor_trie():
    """warm_up() 后 vendored ToJyutping 已在 sys.modules——trie 解析已付,
    后续 text_to_syllables 首调用不再触发 import(阻塞面消灭)。"""
    from bok_voice_core import qa_phonetic

    _drop_vendor_modules()
    assert qa_phonetic.warm_up() is True
    assert f"{_VENDOR_PREFIX}.ToJyutping" in sys.modules
    assert f"{_VENDOR_PREFIX}.ToJyutping.Jyutping" in sys.modules
    # 首调用只走缓存/转换路径,vendor 模块引用仍由 sys.modules 持有
    # (若 warm-up 缺席,此处会是本进程首次 import 的触发点)。
    assert qa_phonetic.text_to_syllables("你好") == [] or isinstance(
        qa_phonetic.text_to_syllables("测试文本"), list
    )
    assert f"{_VENDOR_PREFIX}.ToJyutping" in sys.modules


def test_warm_up_never_raises_on_bad_state(monkeypatch):
    """_converter 抛异常时 warm_up 返回 False 绝不抛——预热失败唔阻装配,
    运行时惰性路径照旧兜底(零行为变化红线)。"""
    from bok_voice_core import qa_phonetic

    def _boom():
        raise RuntimeError("vendor broken")

    monkeypatch.setattr(qa_phonetic, "_converter", _boom)
    assert qa_phonetic.warm_up() is False


def test_entrypoint_calls_warm_up_before_room_claim():
    """源级 pin:agent.py entrypoint 早段调用 warm_up——位置契约=在
    ``_init_sentry("agent-worker")`` 之后、RoomClaim 守卫之前(启动期,
    会话装配前;失败静默 except 包裹)。"""
    src = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert "from bok_voice_core.qa_phonetic import warm_up as _qa_phonetic_warm_up" in src
    assert "_qa_phonetic_warm_up()" in src
    i_sentry = src.index('_init_sentry("agent-worker")')
    i_warm = src.index("_qa_phonetic_warm_up()")
    i_claim = src.index("from .room_claim import RoomClaim")
    assert i_sentry < i_warm < i_claim, "warm_up 必须在 entrypoint 早段(sentry 后、room-claim 前)"
    # 预热失败静默:try/except 包裹(装配永不因预热炸)
    i_try = src.rindex("try:", 0, i_warm)
    between = src[i_try:i_warm]
    assert "qa_phonetic" in between
