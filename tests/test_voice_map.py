"""core voice_map 单测（2026-10-08 B 线人设音色复用波上收）。

单源纪律：``parse_voice_map``/``collapse_voice_map`` 从 agent.py 上收
bok_voice_core——A 线（agent 别名）/B 线（interpret 音色链人设层）消费同一
对象。语义零变化（历史用例随迁）。
"""

from __future__ import annotations

from bok_voice_core.voice_map import collapse_voice_map, parse_voice_map


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


def test_collapse_voice_map_picks_main_language():
    """整场同声收敛：主语言键优先，缺则 zh→canto→en 链，统一落 zh 键。"""
    m = {"zh": "v-zh", "cantonese": "v-canto", "en": "v-en"}
    assert collapse_voice_map(m, "cantonese") == {"zh": "v-canto"}
    assert collapse_voice_map(m, "en") == {"zh": "v-en"}
    assert collapse_voice_map(m, "zh") == {"zh": "v-zh"}
    assert collapse_voice_map(m, "") == {"zh": "v-zh"}  # 无主语言=zh 链首
    assert collapse_voice_map(m, "de") == {"zh": "v-zh"}  # 非法主语言=回落链
    # 主语言键空 → 链上下一键
    assert collapse_voice_map({"cantonese": "v-canto", "en": "v-en"}, "zh") == {"zh": "v-canto"}
    # 全空/空 map → {}（纯空白原样保留——剥空白是下游过滤层的事,零漂移）
    assert collapse_voice_map({}, "zh") == {}
    assert collapse_voice_map({"zh": "", "en": ""}, "zh") == {}
