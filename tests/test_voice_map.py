"""core voice_map 单测（2026-10-08 B 线人设音色复用波上收）。

单源纪律：``parse_voice_map`` 从 agent.py 上收 bok_voice_core——A 线
（agent._parse_voice_map 别名）/B 线（interpret 音色链人设层）消费同一对象。
语义零变化（历史用例随迁）。
"""

from __future__ import annotations

from bok_voice_core.voice_map import parse_voice_map


def test_parse_voice_map_shapes():
    assert parse_voice_map(None) == {"zh": ""}
    assert parse_voice_map("") == {"zh": ""}
    # 空白原样保留（零漂移：剥空格是下游 _cloud_voice 过滤层的事）。
    assert parse_voice_map("  ") == {"zh": "  "}
    assert parse_voice_map("single-voice-id") == {"zh": "single-voice-id"}
    # JSON map 直通
    assert parse_voice_map('{"zh": "a", "cantonese": "b", "en": "c"}') == {
        "zh": "a",
        "cantonese": "b",
        "en": "c",
    }
    # dict 直通
    assert parse_voice_map({"en": "x"}) == {"en": "x"}
    # 坏 JSON 回落单音色档
    assert parse_voice_map('{"zh": ') == {"zh": '{"zh": '}
    # JSON 但非 dict（数组/标量）回落单音色档
    assert parse_voice_map('["a","b"]') == {"zh": '["a","b"]'}
    assert parse_voice_map('"quoted-id"') == {"zh": '"quoted-id"'}
