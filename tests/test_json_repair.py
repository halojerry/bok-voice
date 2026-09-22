"""`bok_voice_core.json_repair` 的保守补齐契约（2026-09-21）。

背景见模块 docstring：`summarize._parse` 的模型输出有**内容全对、只漏最外层一个 `}`**
的坏法（本机 9B 真实转写 6/6 复现），静默退桩文本把整通沉淀丢掉。
本文件的边界与收益都钉死：**只补未闭合的括号**，且补不回来时宁可不救。
"""

from __future__ import annotations

import json

from bok_voice_core.json_repair import close_unclosed_json, loads_lenient

GOOD = '{"summary":"s","new_topics":[{"topic":"t","summary":"x"}],"insight":{"statement":"i","confidence":0.8}}'


def test_valid_json_passes_strict_path():
    assert loads_lenient(GOOD)["summary"] == "s"


def test_missing_outer_brace_is_repaired():
    """本机 9B 的**主要坏法**：漏掉最外层 `}`（内容与结构全对）。"""
    broken = GOOD[:-1]
    assert close_unclosed_json(broken) == GOOD
    data = loads_lenient(broken)
    assert data is not None
    assert data["summary"] == "s"
    assert len(data["new_topics"]) == 1
    assert data["insight"]["statement"] == "i"


def test_missing_two_closers_is_repaired():
    """数组与对象同时欠闭合时按栈序补齐。"""
    broken = '{"summary":"s","new_topics":[{"topic":"t"}'
    data = loads_lenient(broken)
    assert data is not None and data["summary"] == "s"


def test_balanced_but_invalid_is_not_guessed():
    """漏逗号 = 另一种坏法——不猜（修坏的 JSON 会沉淀错答案，比丢掉更糟）。"""
    broken = '{"summary":"s" "new_topics":[]}'
    assert close_unclosed_json(broken) is None
    assert loads_lenient(broken) is None


def test_mismatched_closer_is_not_guessed():
    """错位闭括号（`[` 被 `}` 关）不是「漏收尾」，不猜。"""
    assert close_unclosed_json('{"a":[1,2}') is None
    assert loads_lenient('{"a":[1,2}') is None


def test_inner_closed_outer_open_is_repaired():
    """数组自己闭合了、只剩外层对象欠闭合——这种是能补的。"""
    assert close_unclosed_json('{"a":[1,2]') == '{"a":[1,2]}'


def test_unterminated_string_is_not_guessed():
    """字符串没结束 = 内容被切了，补括号只会造出半个值的假对象。"""
    assert close_unclosed_json('{"summary":"写到一半') is None
    assert loads_lenient('{"summary":"写到一半') is None


def test_braces_inside_strings_do_not_confuse_scanner():
    broken = '{"summary":"他话「{你好}」跟住走咗"'  # 字符串内含大括号，且漏外层 }
    data = loads_lenient(broken)
    assert data is not None
    assert data["summary"] == "他话「{你好}」跟住走咗"


def test_non_dict_json_is_refused():
    """顶层不是对象（数组/标量）不算纪要——返 None 让调用方走 fallback。"""
    assert loads_lenient("[1,2,3]") is None
    assert loads_lenient('"just a string"') is None
    assert loads_lenient("") is None


def test_already_balanced_returns_none_from_repair():
    """平衡但解析失败的输入，补齐函数必须返 None（它没有可补的东西）。"""
    assert close_unclosed_json(GOOD) is None


def test_repaired_output_is_json_loadable():
    """补齐产物必须是**真 JSON**（不是碰巧能跑的对象）。"""
    broken = '{"a":1,"b":{"c":[1,2'
    fixed = close_unclosed_json(broken)
    assert fixed is not None
    assert json.loads(fixed) == {"a": 1, "b": {"c": [1, 2]}}
