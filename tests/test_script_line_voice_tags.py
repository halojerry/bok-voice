"""罐头线（脚本直念线）语气标记票离线单测（2026-10-06）。

背景：W3 人感波只给了 gen=llm 轮标记能力（prompt 块 + tts_text_transforms）；
say 步/开场白等罐头线占实弹可听轮的大头、结构上不带标记。本票让**步骤文案**
可以直接携带白名单语气/停顿标记——pregen 物化时随文本烧进缓存音频，运行时
同键命中即点即播。

被测面（全部离线，零网络）：
- `voice_style.script_line_speech_text`：罐头线规范形单源——白名单标记归一
  （全角/大小写 → 小写 ASCII）、句首未知括号剥除、停顿钳制、kill-switch 全剥、
  marker-free 快路径逐字节零漂移。
- 键行为：缓存键折入标记文本（标记变体与无标记变体不同键不撞车；全角/ASCII
  变体归一同键=去重非碰撞）。
- pregen `_say_step_lines`/`_opening_line` 与运行时 `step_say_text`/`opening_text`
  对标记文案仍逐字节同源（缓存键 text 维度对齐的前提，test_publish_pregen
  契约在标记形态下的延伸）。
- `_materialize` 物化路径（fake 合成）：规范形文本进合成、缓存键=规范形键、
  重跑幂等 skip。
- demo 模板（data/templates/hegui-cantonese.json 开场白 (breath)）端到端同文。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in ("apps/agent", "scripts", "scripts/runtime"):
    _sp = str(ROOT / _p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

import pytest  # noqa: E402
from agent_runtime.flow import FlowController  # noqa: E402
from agent_runtime.tts_cache import TtsAudioCache, cache_key  # noqa: E402
from agent_runtime.voice_style import (  # noqa: E402
    VOICE_TAG_WHITELIST,
    script_line_speech_text,
)


@pytest.fixture(autouse=True)
def _default_gate(monkeypatch):
    """钉缺省门档：kill-switch 未设=开（规范形保留白名单标记）。"""
    monkeypatch.delenv("BOK_A_LINE_VOICE_TAGS", raising=False)


# ---------------------------------------------------------------- 规范形


def test_marker_free_fast_path_byte_identical():
    """无括号/停顿 token 的文案逐字节原样返回（含英文双空格——sanitize 的空白
    收敛对快路径不发生，罐头零漂移铁律）。"""
    for text in (
        "你好，打擾你。我係集運中轉倉嘅客服，打俾你係關於你一件包裹嘅異常情況。",
        "Hello  world,  double spaces survive!",
        "单号 SF7890 已到仓。",
        "",
    ):
        assert script_line_speech_text(text) == text


def test_content_paren_mid_sentence_untouched():
    """句中内容括号（非标记语义）原样保留——与 sanitize 既有契约一致。"""
    text = "我们支持粤语（广东话）服务"
    assert script_line_speech_text(text) == text


def test_whitelist_marker_normalized_to_ascii():
    """作者写成全角/大写形态的白名单标记 → 归一成小写 ASCII（MiniMax 只认
    ASCII 括号形态，全角原样会被当文本念出来并烧进缓存）。"""
    out = script_line_speech_text("你好，打擾你。（BREATH）我係集運中轉倉嘅客服。")
    assert out == "你好，打擾你。(breath)我係集運中轉倉嘅客服。"
    # W3 扩容三件同受白名单
    assert "(chuckle)" in script_line_speech_text("好的（Chuckle）冇問題。")


def test_stage_direction_and_bad_pause_cleaned():
    """句首舞台指示括号剥除 + 超限停顿钳制——人工文案最常见两类脏形态。"""
    out = script_line_speech_text("（停顿两秒）您好<#3#>我帮您查")
    assert "停顿" not in out and out.startswith("您好")
    assert "<#0.8#>" in out


def test_kill_switch_strips_script_line_markers(monkeypatch):
    """env 总闸关 → 规范形全剥：kill-switch 对 LLM 线与罐头线语义一致。"""
    monkeypatch.setenv("BOK_A_LINE_VOICE_TAGS", "0")
    out = script_line_speech_text("(breath)您好<#0.3#>在吗")
    assert "(breath)" not in out and "<#" not in out and "您好" in out and "在吗" in out
    # marker-free 文案在关闸档同样快路径原样返回（零漂移不看门档）
    assert script_line_speech_text("您好在吗") == "您好在吗"


def test_whitelist_reuse_single_source():
    """规范形与 LLM 线同一白名单（reuse VOICE_TAG_WHITELIST，不立第二份词表）。"""
    assert "breath" in VOICE_TAG_WHITELIST and "chuckle" in VOICE_TAG_WHITELIST


# ---------------------------------------------------------------- 运行时 flow


def _fc(steps: list[dict], obj: dict | None = None) -> FlowController:
    tpl = {"id": "tpl-1", "language": "cantonese", "steps_json": json.dumps(steps, ensure_ascii=False)}
    return FlowController.from_template(tpl, obj)


def test_flow_step_say_text_marker_free_byte_identical():
    """无标记直念步返回值与旧实现逐字节一致（快路径）。"""
    line = "我哋會經WhatsApp聯絡您，麻煩你留意查收。"
    fc = _fc([{"goal": "通知", "ref": line, "say": 1}])
    assert fc.pending_say_text() == line
    assert fc.step_say_text(0) == line


def test_flow_step_say_text_canonicalizes_author_markers():
    """文案带全角标记 → 直念文本输出规范形（pregen 同调，见下方 parity）。"""
    fc = _fc([{"goal": "安抚", "ref": "多謝你嘅耐心。（sighs）我哋會盡快處理。", "say": 1}])
    assert fc.step_say_text(0) == "多謝你嘅耐心。(sighs)我哋會盡快處理。"
    assert fc.pending_say_text() == "多謝你嘅耐心。(sighs)我哋會盡快處理。"


def test_flow_opening_text_canonicalizes_markers():
    """开场白（step 0 force 直念）同过规范形。"""
    fc = _fc([{"goal": "开场", "ref": "你好，打擾你。（breath）我係集運中轉倉嘅客服。\n如果客户唔信 → 就咁話"}, {}])
    assert fc.opening_text() == "你好，打擾你。(breath)我係集運中轉倉嘅客服。"


# ---------------------------------------------------------------- 键行为


def test_cache_key_folds_markers_no_collision():
    """缓存键折入标记文本：标记变体与无标记变体**不同键**（不会撞车播错音频）。"""
    base = dict(voice_id="v", model="speech-2.8-hd", sample_rate=24000)
    assert cache_key("(breath)你好", **base) != cache_key("你好", **base)
    assert cache_key("(sighs)抱歉", **base) != cache_key("抱歉", **base)


def test_cache_key_marker_variants_dedup_not_collide():
    """全角/ASCII/宽空白变体在键层归一同键=**去重**（同一规范形同一段音频），
    与「标记 vs 无标记不同键」的正交性质一起构成无碰撞键面。"""
    base = dict(voice_id="v", model="speech-2.8-hd", sample_rate=24000)
    ascii_key = cache_key("(breath)你好", **base)
    assert cache_key("（breath）你好", **base) == ascii_key


# ------------------------------------------------- pregen parity（键对齐前提）


def _runtime_say_lines(tpl: dict, obj: dict | None) -> list[tuple[str, str]]:
    fc = FlowController.from_template(tpl, obj)
    out: list[tuple[str, str]] = []
    for i, s in enumerate(fc.steps):
        if not s.say:
            continue
        text = fc.step_say_text(i)
        if text:
            out.append((text, s.emotion))
    return out


def test_pregen_say_step_lines_parity_with_markers():
    """pregen `_say_step_lines` 与运行时 `step_say_text` 对标记文案逐字节同源
    ——test_publish_pregen 字节同源契约在标记形态下的延伸（缓存键 text 维度
    两侧一致的前提）。"""
    import pregen_tts

    tpl = {
        "id": "tpl-1",
        "language": "cantonese",
        "steps_json": json.dumps(
            [
                {"goal": "通知", "ref": "你好{姓名}，我哋會經{聯絡方式}聯絡您。（chuckle）請留意。", "say": 1},
                {"goal": "收尾", "ref": "再見。（breath）多謝你。", "say": 1},
            ],
            ensure_ascii=False,
        ),
    }
    obj = {"id": "obj-1", "display_name": "陳大文", "language": "cantonese"}
    actual = pregen_tts._say_step_lines(tpl, obj)
    expected = _runtime_say_lines(tpl, obj)
    assert actual == expected
    # 归一真的发生（全角 → ASCII），变量照常渲染
    assert any("(chuckle)" in t for t, _ in actual)
    assert any("陳大文" in t and "(chuckle)" in t for t, _ in actual)
    assert any(t.endswith("再見。(breath)多謝你。") or t == "再見。(breath)多謝你。" for t, _ in actual)


def test_pregen_opening_line_parity_demo_template():
    """demo 模板端到端（hegui-cantonese 开场白 (breath)）：运行时 opening_text
    与 pregen `_opening_line` 同文同规范形——物化键=运行时查找键。"""
    import pregen_tts

    raw = json.loads((ROOT / "data" / "templates" / "hegui-cantonese.json").read_text(encoding="utf-8"))
    tpl = dict(raw)
    tpl["steps_json"] = json.dumps(raw["steps_json"], ensure_ascii=False)  # CP 存储形态
    opening = FlowController.from_template(tpl, {}).opening_text()
    assert "(breath)" in opening
    assert opening.startswith("你好，打擾你。(breath)")
    # pregen 侧（--objects 逐对象开场白线）同文
    assert pregen_tts._opening_line(tpl, {}, "cantonese") == opening


# ------------------------------------------------- 物化路径（fake 合成零网络）


def test_materialize_bakes_canonical_marker_text_into_cache(monkeypatch, tmp_path):
    """pregen 物化全链（计划器→物化器）：规范形文本交给合成（情绪烧进音频的
    前提）、缓存键=规范形键、标记变体与无标记变体各占一键、重跑幂等 skip。
    fake 合成零网络。"""
    import pregen_tts
    from agent_runtime.providers.livekit_plugins import minimax_speed_for

    seen: list[str] = []

    async def _fake_synth(provider, text):
        seen.append(text)
        return b"\x01\x02\x03"

    monkeypatch.setattr(pregen_tts, "_synth", _fake_synth)
    monkeypatch.setattr(pregen_tts, "_provider_for", lambda *a, **kw: object())
    cache = TtsAudioCache(root=tmp_path, sample_rate=24000)
    persona = {"id": "p1", "language": "cantonese", "reference_audio": '{"cantonese":"Vcanto"}'}
    raw_marked = "你好，打擾你。（BREATH）我係客服。"  # 作者写脏形态
    plain = "再見。"
    tpl = {
        "language": "cantonese",
        "steps_json": json.dumps(
            [{"goal": "开场", "ref": raw_marked, "say": 1},
             {"goal": "收尾", "ref": plain, "say": 1}],
            ensure_ascii=False,
        ),
    }
    jobs = [
        (persona, "cantonese", text, emotion)
        for text, emotion in pregen_tts._say_step_lines(tpl, None)  # 计划器：规范形在这里发生
    ]

    kw = dict(
        api_key="k", sample_rate=24000, tts_cfg={"provider": "minimax"},
        voice_mode="single", pin=True,
    )
    ok, skip, fail, _records = asyncio.run(
        pregen_tts._materialize(cache, "speech-2.8-hd", jobs, **kw)
    )
    assert (ok, skip, fail) == (2, 0, 0)
    # 合成收到规范形（全角 BREATH → ASCII breath），不把脏形态烧进音频
    assert raw_marked not in seen
    assert "你好，打擾你。(breath)我係客服。" in seen

    voice = pregen_tts._persona_resolved_voice(persona, "cantonese", {"provider": "minimax"}, "single")
    assert voice == "Vcanto"
    speed = minimax_speed_for("cantonese")
    model = "speech-2.8-hd"
    # 缓存键 = 规范形键；物化即点即播（运行时 step_say_text 同规范形 → 同键命中）
    canon = "你好，打擾你。(breath)我係客服。"
    assert cache.get(cache.key_for(canon, voice=voice, model=model, speed=speed)) is not None
    assert cache.get(cache.key_for(plain, voice=voice, model=model, speed=speed)) is not None
    # 无标记变体是另一条键（不撞车）；作者脏形态原文不是键
    assert cache.get(cache.key_for("你好，打擾你。我係客服。", voice=voice, model=model, speed=speed)) is None
    assert cache.get(cache.key_for(raw_marked, voice=voice, model=model, speed=speed)) is None

    # 重跑幂等：同 jobs 全 skip（已物化不重合成）
    ok2, skip2, fail2, _r2 = asyncio.run(
        pregen_tts._materialize(cache, "speech-2.8-hd", jobs, **kw)
    )
    assert (ok2, skip2, fail2) == (0, 2, 0)
    assert len(seen) == 2  # 合成只发生首轮


def test_say_step_lines_emotion_dimension_untouched():
    """步级行级情绪与标记正交：emotion 原样透传（缓存键 emotion 维度既有语义）。"""
    import pregen_tts

    tpl = {
        "language": "cantonese",
        "steps_json": json.dumps(
            [{"goal": "通知", "ref": "好抱歉。（sighs）包裹搵唔到。", "say": 1, "emotion": "sad"}],
            ensure_ascii=False,
        ),
    }
    assert pregen_tts._say_step_lines(tpl, None) == [("好抱歉。(sighs)包裹搵唔到。", "sad")]


def test_runtime_lookup_key_matches_pregen_store_key():
    """闭环钉：runtime `_say_script` 的查找键（text=规范形 + voice/model/speed）
    与 pregen `store` 的键逐项同参 → 同一 sha1（demo 文案全链演练，零 I/O）。"""
    import pregen_tts
    from agent_runtime.providers.livekit_plugins import minimax_speed_for

    raw = json.loads((ROOT / "data" / "templates" / "hegui-cantonese.json").read_text(encoding="utf-8"))
    tpl = dict(raw)
    tpl["steps_json"] = json.dumps(raw["steps_json"], ensure_ascii=False)
    runtime_text = FlowController.from_template(tpl, {}).opening_text()
    pregen_text = pregen_tts._opening_line(tpl, {}, "cantonese")
    assert runtime_text == pregen_text

    persona = {"id": "p1", "language": "cantonese", "reference_audio": '{"cantonese":"Vcanto"}'}
    voice = pregen_tts._persona_resolved_voice(persona, "cantonese", {"provider": "minimax"}, "single")
    speed = minimax_speed_for("cantonese")
    model = os.environ.get("MINIMAX_MODEL", "speech-2.8-hd")  # 与 pregen main 同缺省档
    cache = TtsAudioCache(root=Path("/nonexistent"), sample_rate=24000)
    lookup_key = cache.key_for(runtime_text, voice=voice, model=model, speed=speed, emotion="")
    store_key = cache.key_for(pregen_text, voice=voice, model=model, speed=speed, emotion="")
    assert lookup_key == store_key
