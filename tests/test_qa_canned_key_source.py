"""F-11（2026-09-23 生产就绪修复波）：罐头缓存键推导单源化。

实弹证据（T3 报告 F-11）：branch-canned 与 graph play 的 `_qa_pcm_for` 恒 miss
（BRANCH_CANNED miss / FLOW_GRAPH play_miss），而 CP canned-status=ok、
`tts-pregen --qa` skipped=33、缓存文件在场——物化面与运行时查询键不同源。

根因：pregen 物化/状态面**无条件按 MiniMax** 推键（MiniMaxTTS+voice map+
MINIMAX_MODEL+minimax_speed_for），运行时罐头缓存只挂在 MiniMax 链上
（`_tts_primary` 非空才建 `_tts_cache`）——有效 TTS provider 是 qwen3_tts
（全局默认，设置 provider 空）时运行时根本没有缓存链，`_qa_pcm_for` 结构性
查空，状态面却照报 ok。修法=有效 provider 判据单源化（`effective_tts_provider`
/`canned_cache_supported`），pregen 物化与状态面同闸；回归测试钉「物化→运行时
查询命中」round-trip。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in ("apps/agent", "packages/core", "scripts"):
    sp = str(ROOT / p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from agent_runtime.agent import (  # noqa: E402
    _assemble_minimax_voice_map,
    _resolve_tts_voice_mode,
    canned_cache_supported,
    effective_tts_provider,
)
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    LanguageState,
    MiniMaxTTS,
    minimax_speed_for,
)
from agent_runtime.tts_cache import CachedTTS, TtsAudioCache  # noqa: E402
import pregen_tts  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_minimax_key_env(monkeypatch):
    """M-4(2026-09-23 评审返工):钉键基线——round-trip 两侧取值口
    (`minimax_speed_for` 读 MINIMAX_SPEED、`MiniMaxTTS._model` 读
    MINIMAX_MODEL)不吃部署机 env 覆盖,物化↔查询同键断言才稳定。"""
    monkeypatch.delenv("MINIMAX_MODEL", raising=False)
    monkeypatch.delenv("MINIMAX_SPEED", raising=False)
    yield


# ---- 有效 provider 判据（单源，与 agent 装配同一条规则） ----

def test_effective_tts_provider_rule():
    # persona 覆盖 > 全局；全局空缺省 qwen3_tts（与 agent.py 装配逐字节同规则）。
    assert effective_tts_provider({"tts_provider": "minimax"}, {"provider": "qwen3_tts"}) == "minimax"
    assert effective_tts_provider({"tts_provider": ""}, {"provider": "minimax"}) == "minimax"
    assert effective_tts_provider(None, {"provider": "minimax_streaming"}) == "minimax_streaming"
    assert effective_tts_provider(None, {}) == "qwen3_tts"
    assert effective_tts_provider({}, {"provider": ""}) == "qwen3_tts"
    assert effective_tts_provider({"tts_provider": " QWEN3_TTS "}, {}) == "qwen3_tts"


def test_canned_cache_supported_only_minimax_family():
    # 罐头缓存只挂 MiniMax 链：minimax 族 True，其余（含缺省 qwen3_tts）False。
    assert canned_cache_supported(None, {"provider": "minimax"})
    assert canned_cache_supported({"tts_provider": "minimax_streaming"}, {})
    assert not canned_cache_supported(None, {})
    assert not canned_cache_supported(None, {"provider": "qwen3_tts"})
    assert not canned_cache_supported({"tts_provider": "volcano"}, {"provider": "minimax"})
    assert not canned_cache_supported({"tts_provider": "fake"}, {})


# ---- 物化→运行时查询 round-trip（键单源回归） ----

_MINIMAX_CFG = {"provider": "minimax", "sample_rate": 24000, "api_key": "k"}
_PERSONA = {
    "id": "p1",
    "language": "zh",
    "tts_provider": "minimax",
    "reference_audio": "Chinese_crisp_podcaster_nv1",
}


def test_materialize_then_runtime_lookup_hit(tmp_path):
    """pregen 物化键与运行时 `_qa_pcm_for` 取值口（resolved_voice/model/speed）
    必须同键——这条就是 F-11 的回归钉。"""
    lang, text = "zh", "您的包裹已经到驿站了。"
    cache = TtsAudioCache(tmp_path, sample_rate=24000)
    voice_mode = _resolve_tts_voice_mode(_MINIMAX_CFG)

    # 物化侧：与 pregen_tts._materialize 同一条推导（voice map 同源函数+同速规则）。
    voice = pregen_tts._persona_resolved_voice(_PERSONA, lang, _MINIMAX_CFG, voice_mode)
    assert voice == "Chinese_crisp_podcaster_nv1"
    model = "speech-2.8-hd"
    speed = minimax_speed_for(lang)
    key = cache.key_for(text, voice=voice, model=model, speed=speed)
    assert cache.store(key, b"\x01\x02" * 4800, text=text, voice=voice,
                       model=model, pin=True, speed=speed)

    # 运行时侧：复刻 agent.py 装配（MiniMaxTTS + CachedTTS 包裹取值口）后查询。
    voice_map = _assemble_minimax_voice_map(
        persona=_PERSONA, tts_cfg=_MINIMAX_CFG, greet_lang=lang, voice_mode=voice_mode
    )
    primary = MiniMaxTTS(
        voice=voice_map, language_state=LanguageState(lang=lang),
        sample_rate=24000, api_key="k",
    )
    cached = CachedTTS(
        primary, cache=cache,
        voice_provider=primary.resolved_voice,
        model_provider=primary.resolved_model,
        speed_provider=primary.resolved_speed,
    )
    # _qa_pcm_for 同款取值口（agent.py :4807-4815）。
    pcm = cache.lookup(
        text, voice=cached.resolved_voice(), model=cached.resolved_model(),
        speed=cached.resolved_speed(),
    )
    assert pcm is not None, "物化键与运行时查询键必须同源命中（F-11）"


def test_status_and_jobs_gate_non_minimax_provider(tmp_path):
    """有效 provider 非 minimax 族：状态面不得报 ok（报 provider_off），
    物化计划不出该 persona 的 job（物化了运行时也查不到）。"""
    rows = [{"id": "qa:1", "answer_text": "您好，包裹已到驿站。", "lang": "zh", "enabled": True}]
    qwen_persona = {"id": "p2", "language": "zh", "tts_provider": "", "reference_audio": "clone-x"}

    # --qa-status：qwen3 persona → missing + reason=provider_off（不是假 ok）。
    plan = pregen_tts._qa_status(
        rows, persona_pool=[qwen_persona], lang_personas={"zh": qwen_persona},
        all_personas=False, tts_cfg={}, voice_mode="single",
        model="speech-2.8-hd", sample_rate=24000, cache=TtsAudioCache(tmp_path),
    )
    assert plan["qa:1"]["state"] == "missing"
    assert plan["qa:1"]["reason"] == "provider_off"
    assert plan["qa:1"]["voice"] == "" and plan["qa:1"]["key"] == ""

    # --qa 物化计划：qwen3 persona 的 job 全跳过。
    assert pregen_tts._qa_jobs(
        rows, [qwen_persona], {"zh": qwen_persona},
        all_personas=True, tts_cfg={}, voice_mode="single",
    ) == []
    jobs = pregen_tts._qa_jobs(
        rows, [_PERSONA], {"zh": _PERSONA},
        all_personas=True, tts_cfg=_MINIMAX_CFG, voice_mode="single",
    )
    assert len(jobs) == 1 and jobs[0][2] == "您好，包裹已到驿站。"

    # minimax persona 照常出计划与状态（无 provider 问题的条目不带 reason 键）。
    plan_ok = pregen_tts._qa_status(
        rows, persona_pool=[_PERSONA], lang_personas={"zh": _PERSONA},
        all_personas=False, tts_cfg=_MINIMAX_CFG, voice_mode="single",
        model="speech-2.8-hd", sample_rate=24000, cache=TtsAudioCache(tmp_path),
    )
    assert plan_ok["qa:1"]["state"] == "missing"
    assert "reason" not in plan_ok["qa:1"]
    assert plan_ok["qa:1"]["voice"] == "Chinese_crisp_podcaster_nv1"


def test_branch_status_skips_provider_off_context(tmp_path):
    """--branch-status：非 minimax 上下文不算可合成上下文（qwen3 栈同一份缓存
    文件在场也绝不报 ok）。"""
    tpl = {
        "language": "zh",
        "steps_json": json.dumps(
            [{"goal": "查单号", "ref": "请问单号是多少？\n如果客户不肯给→好的先帮您登记。"}]
        ),
    }
    # 状态键=parse_step_ref 产出的 resp 原文(_branch_status 同源),非整行分支行。
    raw = "好的先帮您登记。"
    base_kw = dict(texts=None, tts_cfg=_MINIMAX_CFG, voice_mode="single",
                   model="speech-2.8-hd", cache=TtsAudioCache(tmp_path))

    # minimax + 已物化 → ok（与 _materialize 同键落盘后查询）。
    rendered = pregen_tts._strip_branch_action(raw)
    voice = pregen_tts._persona_resolved_voice(_PERSONA, "zh", _MINIMAX_CFG, "single")
    speed = minimax_speed_for("zh")
    key = TtsAudioCache(tmp_path).key_for(rendered, voice=voice, model="speech-2.8-hd", speed=speed)
    TtsAudioCache(tmp_path).store(key, b"\x00\x01" * 100, text=rendered, voice=voice,
                                  model="speech-2.8-hd", pin=True, speed=speed)
    status = pregen_tts._branch_status([tpl], {"zh": _PERSONA}, **base_kw)
    assert status[raw] == "ok"

    # qwen3 栈（同一份缓存文件在场）→ 上下文不可合成，绝不报 ok。
    qwen_persona = {"id": "p2", "language": "zh", "tts_provider": "", "reference_audio": "clone-x"}
    status_q = pregen_tts._branch_status(
        [tpl], {"zh": qwen_persona},
        texts=None, tts_cfg={}, voice_mode="single",
        model="speech-2.8-hd", cache=TtsAudioCache(tmp_path),
    )
    assert status_q[raw] != "ok"
