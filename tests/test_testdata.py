"""测试对象命名族单源(bok_voice_core.testdata)——正则族与边界用例。

同一份前缀族两处消费:agent 心跳豁免(alias import)与 CP qa-pairs 挖掘过滤。
"""

from __future__ import annotations

from bok_voice_core.testdata import TEST_OBJECT_NAME_RE, is_test_object_name


def test_test_object_name_family_hits():
    # 与 tools/bok.py clean-testdata / E2E·压测脚本命名约定同族
    assert is_test_object_name("E2E-zh-1788952794")
    assert is_test_object_name("E2E-cantonese-1")
    assert is_test_object_name("soak10")
    assert is_test_object_name("soak2-road3")
    assert is_test_object_name("并发-4路-1")
    assert is_test_object_name("LOAD-20260910")
    assert is_test_object_name("边角-e1")
    assert is_test_object_name("多轮-对话3")
    assert is_test_object_name("probe-filler")


def test_real_names_and_none_empty_miss():
    assert not is_test_object_name("陳大文")
    assert not is_test_object_name("Bok客戶")
    assert not is_test_object_name("E2E客服")  # 前缀门:E2E- 带连字符才算
    assert not is_test_object_name("")
    assert not is_test_object_name(None)


def test_regex_pattern_is_stable():
    # 前缀族改动必须同步 scripts/e2e_* 与 AGENTS.md「测试对象心跳豁免」段
    assert TEST_OBJECT_NAME_RE.pattern == r"^(E2E-|soak\d*-?|并发|LOAD-|边角-|多轮-|probe)"
