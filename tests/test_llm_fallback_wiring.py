"""RC1 兜底文本注入断线单测(2026-09-17)。

实锤根因:装配区曾对包装后的 ContextAwareLLM getattr set_fallback_text——
包装层无此方法亦无 __getattr__ 代理,getattr 恒 None → 注入静默跳过 →
MlxLlmLLM._fallback_text 恒空 → 2.0s 首-token 闸壳包裹条件恒 False,
整场休眠(5 通×50 轮实测 LLM_FIRST_TOKEN_TIMEOUT 哨兵 0 次)。

修法:_wire_llm_fallback(raw_llm, lang) 模块级纯函数,只认 raw 内芯;装配区
改打 _raw_llm(与 set_late_answer_cb 同姿势)并打装配日志 LLM_FALLBACK wired=1/0。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _llm_fallback_line, _wire_llm_fallback  # noqa: E402


class _RawWithFallback:
    """最小 raw 内芯替身:与 MlxLlmLLM 同形(只看 set_fallback_text 通道)。"""

    def __init__(self):
        self.fallback_text = None

    def set_fallback_text(self, text: str) -> None:
        self.fallback_text = text


def test_wire_injects_per_language_and_returns_true():
    for lang in ("cantonese", "zh", "en"):
        raw = _RawWithFallback()
        assert _wire_llm_fallback(raw, lang) is True
        assert raw.fallback_text == _llm_fallback_line(lang), (lang, raw.fallback_text)


def test_wire_returns_false_without_method():
    """包装层形状(无该方法亦无代理)→ False,装配日志 wired=0 可见。"""

    class _Wrapped:
        def chat(self, *a, **k):  # 故意不代理 set_fallback_text
            raise NotImplementedError

    assert _wire_llm_fallback(_Wrapped(), "cantonese") is False
    assert _wire_llm_fallback(None, "zh") is False


def test_wire_survives_raising_setter():
    class _Boom:
        def set_fallback_text(self, text):
            raise RuntimeError("setter down")

    assert _wire_llm_fallback(_Boom(), "cantonese") is False


def test_fallback_lines_nonempty_all_langs():
    for lang in ("cantonese", "zh", "en"):
        assert _llm_fallback_line(lang)
    # 未知语言回落 zh 档(函数尾分支)
    assert _llm_fallback_line("zz") == _llm_fallback_line("zh")
