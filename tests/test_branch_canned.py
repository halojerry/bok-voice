"""分支罐头快路(2026-09-20 路线 A-①)单测:闸门/命中/落穿/pregen 分支物化。

快路=当前步 ref 的「如果客户X→就Y」分支命中(match_step_branch 与提示词
注入同源)且应答已物化录音 → 直接播录音跳过 LLM;未物化照旧落穿(注入
提示词走 LLM,现状不变)。可测面三层:
- `branch_canned_pick` 纯函数(闸门全量:开关/closing/paused/REFUSE/
  FAREWELL/WA 步未捕获/无分支/占位残留/不推进);
- 漏斗接线源级断言(块位次在 say 直念门后、graph 意图块前;provider=
  branch-canned;命中 raise StopResponse、miss 落穿)——hook 闭包离线起不了,
  同 test_flow_graph_runtime 源级断言姿势;
- pregen `--branches` 计划与物化(解析/占位跳过/重复 resp 不重复合成/pin 落盘)。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("apps/agent", "packages/core", "scripts"):
    sp = str(ROOT / p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from agent_runtime.agent import branch_canned_pick  # noqa: E402
from agent_runtime.flow import (  # noqa: E402
    FAREWELL,
    OBJECTION,
    QUESTION,
    REFUSE,
    UNCLEAR,
    FlowController,
    parse_steps,
)
from agent_runtime.tts_cache import TtsAudioCache  # noqa: E402
import pregen_tts  # noqa: E402

# 与 test_cached_tts 同款:振幅 1000(> trim 静音门限),避免被首包静音修剪清零。
_LOUD = (1000).to_bytes(2, "little", signed=True) * 4800  # 0.2s@24k

_REF_PLATFORM = (
    "咁您係喺邊個平台買？\n"
    "如果客户嫌赔偿少→我哋會按平台規則盡量幫您爭取。\n"
    "如果客户说不知道哪个平台→唔緊要，打開訂單看看就有平台名。"
)


def _pick(**kw):
    base = dict(
        enabled=True,
        closing=False,
        paused=False,
        user_text="为什么赔这么少",
        goal="核实购买平台",
        ref=_REF_PLATFORM,
        wa_captured=False,
        verdict=OBJECTION,
        vars_map={},
    )
    base.update(kw)
    return branch_canned_pick(**base)


# ---- branch_canned_pick:命中与闸门 ----


def test_pick_hit_by_verdict_family():
    out = _pick()
    assert out is not None
    resp, cond = out
    assert resp == "我哋會按平台規則盡量幫您爭取。"
    assert cond == "嫌赔偿少"


def test_pick_hit_unclear_family_and_kept_on_step():
    out = _pick(
        user_text="我唔记得係边个平台买咯",
        verdict=UNCLEAR,
    )
    assert out == ("唔緊要，打開訂單看看就有平台名。", "说不知道哪个平台")


def test_pick_gates_switch_closing_paused_empty_text():
    assert _pick(enabled=False) is None  # BOK_BRANCH_CANNED=0 → 不走
    assert _pick(closing=True) is None
    assert _pick(paused=True) is None
    assert _pick(user_text="   ") is None


def test_pick_refuse_farewell_yield_to_closing_lane():
    assert _pick(verdict=REFUSE) is None
    assert _pick(verdict=FAREWELL) is None


def test_pick_wa_step_not_captured_skips():
    wa_ref = "請問您嘅WhatsApp號碼係幾多？\n如果客户问为什么→平台流程需要，纯记录用途。"
    # WA 步未捕获 → 跳过(与 QA 快路 wa_step_locked 同语义),唔可以截断收号轮
    assert _pick(goal="加客戶WhatsApp", ref=wa_ref, user_text="为什么要加我", verdict=QUESTION) is None
    # 已捕获 → 照常命中
    out = _pick(
        goal="加客戶WhatsApp", ref=wa_ref, user_text="为什么要加我",
        verdict=QUESTION, wa_captured=True,
    )
    assert out is not None and out[0] == "平台流程需要，纯记录用途。"


def test_pick_no_branch_step_zero_cost():
    assert _pick(ref="我哋係集運中轉倉，通知您件貨到咗。") is None
    assert _pick(ref="") is None  # 无流程/行完(current_goal_ref 返回空)
    assert _pick(user_text="随便讲句", verdict=UNCLEAR, ref="普通正稿没有分支行") is None


def test_pick_renders_vars_and_skips_placeholder_residual():
    ref = "正稿\n如果客户问运费→您的运费是{金额}元。"
    hit = _pick(ref=ref, user_text="运费是多少", verdict=QUESTION, vars_map={"金额": "三十"})
    assert hit == ("您的运费是三十元。", "问运费")
    # 变量缺失 → 占位残留 → 落穿 LLM(pregen 同规则不会物化此条)
    assert _pick(ref=ref, user_text="运费是多少", verdict=QUESTION, vars_map={}) is None


def test_pick_is_pure_and_does_not_advance_flow():
    steps = parse_steps(json.dumps(
        [{"goal": "平台", "ref": _REF_PLATFORM}, {"goal": "办理", "ref": "第2步"}],
        ensure_ascii=False,
    ))
    fc = FlowController(steps=steps)
    before = fc.current
    out1 = _pick()
    out2 = _pick()
    assert out1 == out2 is not None
    assert fc.current == before and not fc.done  # 不推进:分支应答留本步


# ---- 漏斗接线(源级断言,hook 闭包离线不可起) ----


def _agent_src() -> str:
    return (ROOT / "apps/agent/agent_runtime/agent.py").read_text(encoding="utf-8")


def test_funnel_block_position_and_wiring():
    src = _agent_src()
    i_bc = src.index("# ---- 分支罐头快路")
    i_graph = src.index("# ---- 话术图引擎")
    assert i_bc < i_graph  # 插在 say 直念门之后、graph 意图块之前
    assert src.index("pending_say_text()") < i_bc  # say 直念步让位(在前)
    region = src[i_bc:i_graph]
    assert "BOK_BRANCH_CANNED" in region  # 总开关在闸里
    assert 'os.environ.get("BOK_BRANCH_CANNED", "1") == "1"' in region  # 默认开
    assert '_turn_origin["gen"] = "script"' in region
    assert '_turn_origin["provider"] = "branch-canned"' in region
    assert "BRANCH_CANNED hit" in region
    assert "BRANCH_CANNED miss" in region  # 未物化落穿有日志
    assert "raise StopResponse()" in region
    assert "flow_ctrl.advance" not in region  # 不推进流程


# ---- pregen --branches:计划与物化 ----


def _tpl(lang: str, steps: list[dict]) -> dict:
    return {"language": lang, "steps_json": json.dumps(steps, ensure_ascii=False)}


def test_branch_jobs_parse_and_placeholder_skip():
    templates = [
        _tpl("zh", [
            {"goal": "平台", "ref": _REF_PLATFORM},
            {"goal": "运费", "ref": "正稿\n如果客户问运费→您的运费是{金额}元。"},
        ]),
        # 跨模板同文 resp(zh+粤各一条,跨语言不去重)
        _tpl("cantonese", [{"goal": "收尾", "ref": "多謝來電。\n如果客户嫌赔偿少→我哋會按平台規則盡量幫您爭取。"}]),
        _tpl("", [{"goal": "x", "ref": "如果客户问→无语言模板整条跳过"}]),
    ]
    lang_personas = {"zh": None, "cantonese": None, "en": None}
    jobs = pregen_tts._branch_jobs(templates, lang_personas)
    texts = [t for _p, _l, t, _e in jobs]
    assert "我哋會按平台規則盡量幫您爭取。" in texts
    assert texts.count("我哋會按平台規則盡量幫您爭取。") == 2  # zh + cantonese
    assert "唔緊要，打開訂單看看就有平台名。" in texts
    assert not any("{" in t for t in texts)  # 占位残留条目跳过(运行时同规则不认)
    assert not any(t == "无语言模板整条跳过" for t in texts)
    assert all(lang in ("zh", "cantonese") for _p, lang, _t, _e in jobs)


def test_materialize_branches_dedup_pin_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("MINIMAX_SPEED", raising=False)  # 语速档走语言默认,防开发 env 串档
    cache = TtsAudioCache(tmp_path, sample_rate=24000)
    jobs = [
        (None, "zh", "我哋會按平台規則盡量幫您爭取。", ""),
        (None, "zh", "我哋會按平台規則盡量幫您爭取。", ""),  # 同文重复 → 只合成一次
        (None, "zh", "唔緊要，打開訂單看看就有平台名。", ""),
    ]
    calls: list[str] = []

    async def _fake_synth(provider, text):
        calls.append(text)
        return _LOUD

    monkeypatch.setattr(pregen_tts, "_synth", _fake_synth)
    monkeypatch.setattr(pregen_tts, "_provider_for", lambda *a, **k: object())
    kwargs = dict(
        api_key="", sample_rate=24000, tts_cfg={}, voice_mode="single", pin=True,
    )
    ok, skip, fail, _records = asyncio.run(
        pregen_tts._materialize(cache, "speech-2.8-hd", jobs, **kwargs)
    )
    assert (ok, skip, fail) == (2, 0, 0)
    assert len(calls) == 2  # 重复 resp 不重复合成
    # 键与运行时同源:persona=None 走默认音色映射,语速走语言档;pin=True 落盘。
    voice = pregen_tts._persona_resolved_voice(None, "zh", {}, "single")
    from agent_runtime.providers.livekit_plugins import minimax_speed_for

    key = cache.key_for(
        "我哋會按平台規則盡量幫您爭取。", voice=voice, model="speech-2.8-hd",
        speed=minimax_speed_for("zh"), emotion="",
    )
    assert cache.get(key) is not None
    assert cache._is_pinned(key)  # 罐头集不逐出
    # 幂等重跑:全 skip 零合成(零重复云调用)
    ok2, skip2, fail2, _r2 = asyncio.run(
        pregen_tts._materialize(cache, "speech-2.8-hd", jobs, **kwargs)
    )
    assert (ok2, skip2, fail2) == (0, 2, 0)
    assert len(calls) == 2
