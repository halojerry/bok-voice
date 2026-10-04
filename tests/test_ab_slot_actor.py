"""D1 槽位化 actor A/B 台架纯逻辑单测（scripts/bench/ab_slot_actor.py）。

覆盖：臂 env 组装 / 安全闸拒绝 / 汇总聚合 / 尺寸表计算 / 模型路径解析 /
回包 sanity / live 执行清单。全部离线（不碰运行中的栈）；只有 simulate_call
冒烟测试 import 仓库内纯渲染函数（flow/livekit_plugins，零网络零进程）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import ab_slot_actor as aba  # noqa: E402


# ---------------------------------------------------------------------------
# 四臂 env 组装
# ---------------------------------------------------------------------------
def test_arm_env_assembly():
    # ARM1=现状：零 env 覆盖（任何键都会改变对照基线）。
    assert aba.arm_env(aba.ARM1) == {}
    assert aba.arm_shape(aba.ARM1) == "full_script"
    # ARM2=槽位+守卫关：SLOT 开 + 复读三代/stall 阶梯关。
    env2 = aba.arm_env(aba.ARM2)
    assert env2["BOK_SLOT_ACTOR"] == "1"
    assert env2["BOK_REPEAT_GUARD"] == "0"
    assert env2["BOK_REPEAT_CROSS_TURN"] == "0"
    assert env2["BOK_STALL_LADDER"] == "0"
    assert aba.arm_shape(aba.ARM2) == "slot"
    # ARM3=全剧本+守卫关：无 SLOT（测 9B 残余病理）。
    env3 = aba.arm_env(aba.ARM3)
    assert "BOK_SLOT_ACTOR" not in env3
    assert env3["BOK_REPEAT_GUARD"] == "0"
    assert aba.arm_shape(aba.ARM3) == "full_script"
    # ARM4=槽位+守卫关+云端：env 与 ARM2 同构（云在路由层，不进 env）。
    assert aba.arm_env(aba.ARM4) == env2
    assert aba.arm_shape(aba.ARM4) == "slot"
    # 臂 env 返回值必须可改（拷贝语义），不能改到规格表本身。
    env2["BOK_SLOT_ACTOR"] = "0"
    assert aba.arm_env(aba.ARM2)["BOK_SLOT_ACTOR"] == "1"


def test_slot_env_gate_name_locked():
    """env 闸名与 W-A 契约锁死（默认 "0" 由消费者侧保证，台架只写 "1"）。"""
    assert aba.SLOT_ENV == {"BOK_SLOT_ACTOR": "1"}


# ---------------------------------------------------------------------------
# 尺寸表计算
# ---------------------------------------------------------------------------
def test_est_tokens_calibration_and_monotonic():
    assert aba.est_tokens("") == 0
    # CJK≈1tok/字（校准系数 0.93）：100 个汉字 ≈ 93 token。
    assert aba.est_tokens("字" * 100) == 93
    # ASCII 走 ~4 字符/token 通道。
    assert aba.est_tokens("a" * 40) == 10
    assert aba.est_tokens("你好 hello") < aba.est_tokens("你好 hello world extra")


def test_serialize_and_request_size_deterministic():
    msgs = [
        {"role": "system", "content": "甲"},
        {"role": "user", "content": "乙"},
    ]
    a = aba.serialize_messages(msgs)
    b = aba.serialize_messages(msgs)
    assert a == b
    row = aba.request_size_row(msgs)
    assert row["n_messages"] == 2
    assert row["chars"] == len(a)
    assert row["est_tokens"] == aba.est_tokens(a)


def test_uncached_est_strict_prefix_and_reanchor():
    prev = "甲乙丙丁"
    # 严格前缀追加：uncached=增量部分（"戊己"）。
    cur = prev + "戊己"
    out = aba.uncached_est(prev, cur)
    assert out["lcp_chars"] == len(prev)
    assert out["reanchor"] is False
    assert out["uncached_est"] == aba.est_tokens(cur) - aba.est_tokens(prev)
    # 前缀断裂（历史截断重锚）：lcp < len(prev) → reanchor=True，按 lcp 之后全额计。
    cur2 = "甲乙" + "庚辛壬"
    out2 = aba.uncached_est(prev, cur2)
    assert out2["reanchor"] is True
    assert out2["lcp_chars"] == 2
    # 无上一请求（首轮冷）=全额。
    out3 = aba.uncached_est("", cur)
    assert out3["cached_est"] == 0
    assert out3["uncached_est"] == aba.est_tokens(cur)


def test_longest_common_prefix_len():
    assert aba.longest_common_prefix_len("abcd", "abce") == 3
    assert aba.longest_common_prefix_len("", "x") == 0
    assert aba.longest_common_prefix_len("same", "same") == 4


def test_percentile_nearest_rank():
    assert aba.percentile([], 50) is None
    assert aba.percentile([10, 20, 30, 40], 50) == 20
    assert aba.percentile([10, 20, 30, 40], 95) == 40


# ---------------------------------------------------------------------------
# 槽位 stub 渲染器
# ---------------------------------------------------------------------------
def test_slot_role_card_length_and_company():
    card = aba.slot_role_card("cantonese", company="測試倉")
    lo, hi = aba.SLOT_CARD_LIMITS
    assert lo <= len(card) <= hi
    assert "測試倉" in card
    # 未知语言回落 zh 卡（宽容）。
    assert len(aba.slot_role_card("klingon")) > 0


def test_slot_task_block_within_limits_and_deterministic():
    kw = dict(
        step_no=5,
        step_total=8,
        goal="辦理收號（傳截圖）",
        script="辦理理賠要用WhatsApp傳訂單截圖核對。唔該你留個WhatsApp號碼。",
        verdict="QUESTION",
        user_text="你哋係咪呃人？點解有我電話？",
        branch_resp="話傳截圖同理賠核對都係經WhatsApp由專員跟進",
        facts=["平台：拼多多", "第三條不進（上限 2 條）"],
        anchor="辦理理賠要用Wh",
    )
    block = aba.slot_task_block(**kw)
    lo, hi = aba.SLOT_TASK_LIMITS
    assert lo <= len(block) <= hi
    assert block == aba.slot_task_block(**kw)  # 确定性
    assert "辦理收號" in block
    # 超长输入必须硬截断到上限内（防单块撑爆尾部预算）。
    long_block = aba.slot_task_block(
        step_no=1, step_total=8, goal="目标", script="台" * 400, user_text="客" * 100
    )
    assert len(long_block) <= hi
    # 过薄输入补到下限之上（保证两形态对照里槽位块下界可达）。
    thin = aba.slot_task_block(step_no=1, step_total=8, goal="")
    assert len(thin) >= lo
    # 空 verdict 不渲染提示行。
    assert "回应类型" not in aba.slot_task_block(step_no=1, step_total=8, goal="目标")


def test_slot_stub_messages_shape():
    msgs = aba.slot_stub_messages(
        lang="cantonese",
        company="集運中轉倉",
        history=[{"role": "assistant", "content": "開場"}, {"role": "user", "content": "你好"}],
        user_text="我想問賠償",
        task_block="【本轮任务】答賠償",
    )
    assert msgs[0]["role"] == "system"
    assert len(msgs) == 4
    assert msgs[-1]["content"] == "我想問賠償\n\n【本轮任务】答賠償"


# ---------------------------------------------------------------------------
# offline 聚合
# ---------------------------------------------------------------------------
def _fake_row(round_no, lane, a1_tokens, a1_unc, sl_tokens, sl_unc, tag=""):
    return {
        "round": round_no,
        "lane": lane,
        "tag": tag,
        "shapes": {
            "ARM1": {"est_tokens": a1_tokens, "uncached_est": a1_unc},
            "slot": {"est_tokens": sl_tokens, "uncached_est": sl_unc},
        },
    }


def test_aggregate_offline_totals_and_peak():
    rows = [
        _fake_row(1, "say", 0, 0, 0, 0),
        _fake_row(2, "llm", 3432, 3432, 479, 479),
        _fake_row(3, "llm", 3881, 449, 726, 189),
        _fake_row(15, "llm", 7512, 248, 2200, 124),
    ]
    agg = aba.aggregate_offline(rows)
    a1, sl = agg["ARM1"], agg["slot"]
    assert a1["n_llm_rounds"] == 3 and a1["n_script_rounds"] == 1
    assert a1["total_uncached_tokens"] == 3432 + 449 + 248
    assert sl["total_uncached_tokens"] == 479 + 189 + 124
    assert a1["peak_request_tokens"] == 7512
    assert a1["peak_request_round"] == 15
    # 秒数=token/311（换算单点）。
    assert a1["total_uncached_seconds"] == round((3432 + 449 + 248) / aba.MODEL_PREFILL_TOK_S, 2)


def test_pick_round_and_representative_rounds():
    rows = [
        _fake_row(1, "say", 0, 0, 0, 0),
        _fake_row(2, "llm", 1, 1, 1, 1),
        _fake_row(3, "llm", 1, 1, 1, 1, tag="platform_question"),
        _fake_row(4, "llm", 1, 1, 1, 1, tag="post_jump"),
    ]
    assert aba.pick_round(rows, 3)["round"] == 3
    assert aba.pick_round(rows, 99) is None
    picked = aba.pick_representative_rounds(rows, ("post_jump", "platform_question"), limit=2)
    # tag 顺序=wanted 顺序；脚本轮（say）永不入选。
    assert [r["round"] for r in picked] == [4, 3]
    assert all(r["lane"] == "llm" for r in picked)


# ---------------------------------------------------------------------------
# live：安全闸 / 执行清单 / 汇总聚合
# ---------------------------------------------------------------------------
def _mk_db(path: Path, statuses: list[str], routing: str | None = None) -> None:
    # 同名重造（测试内多次调用）：先删旧库，保证建表幂等。
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE call_sessions (id TEXT, status TEXT, created_at TEXT)")
    for i, s in enumerate(statuses):
        con.execute("INSERT INTO call_sessions VALUES (?,?,?)", (f"call-{i}", s, "2026-10-01T00:00:00"))
    con.execute("CREATE TABLE global_settings (id TEXT PRIMARY KEY, model_routing_json TEXT)")
    if routing is not None:
        con.execute("INSERT INTO global_settings VALUES ('global', ?)", (routing,))
    con.commit()
    con.close()


def test_active_call_blockers_refuses_inflight(tmp_path):
    db = tmp_path / "bok.db"
    _mk_db(db, ["ended", "active", "failed"])
    blockers = aba.active_call_blockers(db)
    assert len(blockers) == 1
    assert blockers[0]["status"] == "active"
    # 只有终态=放行（空清单）。
    _mk_db(db, ["ended", "failed"])
    assert aba.active_call_blockers(db) == []
    # ringing/paused 同拦。
    _mk_db(db, ["ringing", "paused"])
    assert len(aba.active_call_blockers(db)) == 2


def test_active_call_blockers_fail_closed(tmp_path):
    # DB 不可读=返回 unknown 条目（live 是重启栈的破坏性操作，fail-closed）。
    blockers = aba.active_call_blockers(tmp_path / "missing.db")
    assert blockers and blockers[0]["status"] == "unknown"


def test_read_routing_a_reply(tmp_path):
    db = tmp_path / "bok.db"
    routing = json.dumps(
        {
            "lanes": {
                "a_reply": {
                    "provider": "local",
                    "base_url": "http://127.0.0.1:1237/v1",
                    "model": "huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit",
                }
            }
        }
    )
    _mk_db(db, ["ended"], routing=routing)
    cfg = aba.read_routing_a_reply(db)
    assert cfg["base_url"] == "http://127.0.0.1:1237/v1"
    assert cfg["model"].endswith("Huihui-Qwen3.5-9B-abliterated-mlx-4bit")
    # 缺行/坏库=空 dict（调用方回退 env，绝不炸）。
    _mk_db(db, ["ended"], routing=None)
    assert aba.read_routing_a_reply(db) == {}
    assert aba.read_routing_a_reply(tmp_path / "nope.db") == {}


def test_resolve_model_path_absolute_first(tmp_path):
    home = tmp_path
    # 已是绝对路径：原样。
    assert aba.resolve_model_path("/abs/m", [], home) == "/abs/m"
    # 服务端已注册绝对候选（后缀匹配）优先。
    ids = ["short/repo", "/Users/x/.lmstudio/models/short/repo"]
    assert aba.resolve_model_path("short/repo", ids, home) == "/Users/x/.lmstudio/models/short/repo"
    # 服务端认得的短 id（服务端已注册=无 HF 下载风险）。
    assert aba.resolve_model_path("short/repo", ["short/repo"], home) == "short/repo"
    # ~/.lmstudio/models 在盘 → 绝对化。
    d = home / ".lmstudio" / "models" / "org" / "name"
    d.mkdir(parents=True)
    assert aba.resolve_model_path("org/name", [], home) == str(d)
    # 解析不出=空串（调用方拒绝直发，绝不冒险发短 id）。
    assert aba.resolve_model_path("ghost/model", [], home) == ""
    assert aba.resolve_model_path("", [], home) == ""


def test_parse_llm_ttft_and_perceived():
    window = (
        "LLM_TTFT_MS 1203 (official) cached=4100/4650 prompt=4650 gen=42 tps=40.7\n"
        "PERCEIVED_MS total=2100 (eou=600 llm=900 tts=600)\n"
        "garbage line\n"
    )
    rows = aba.parse_llm_ttft(window)
    assert len(rows) == 1
    assert rows[0]["ttft_ms"] == 1203
    assert rows[0]["cached"] == 4100
    assert rows[0]["uncached"] == 550
    per = aba.parse_perceived(window)
    assert per == [{"total": 2100, "eou": 600, "llm": 900, "tts": 600}]


def test_count_markers():
    window = "REPEAT_CROSS_TURN_EMPTY suppressed=2\n[stall-ladder] step=3 level=degrade\n"
    got = aba.count_markers(window, aba.GUARD_MARKERS)
    assert got["REPEAT_CROSS_TURN_EMPTY"] == 1
    assert got["[stall-ladder]"] == 1
    assert got["REPEAT_SELF_SUPPRESSED"] == 0


def test_aggregate_live_arm_full_shape():
    window = (
        "LLM_TTFT_MS 1200 (official) cached=1000/1500 prompt=1500 gen=40 tps=40.0\n"
        "LLM_TTFT_MS 2400 (official) cached=0/5000 prompt=5000 gen=30 tps=38.0\n"
        "PERCEIVED_MS total=2100 (eou=600 llm=900 tts=600)\n"
        "REPEAT_CROSS_TURN_EMPTY suppressed=1\n"
        "[stall-ladder] step=3 level=degrade\n"
        "LLM_FALLBACK_TEXT 好的，我帮您核对资料，请稍等。\n"
    )
    soak_json = [
        {
            "perceived": [{"total": 1800, "eou": 500, "llm": 700, "tts": 600}],
            "measures": [{"first_audio_ms": 1300}, {"first_audio_ms": 2600}],
        }
    ]
    agg = aba.aggregate_live_arm(
        flow20_stdout="FLOW20 应答=20/20 步号面=[1, 8] 坏标记=0 → PASS",
        soak_stdout="LATENCY_SOAK ... → PASS",
        soak_json=soak_json,
        log_window=window,
    )
    assert agg["n_llm_requests"] == 2
    assert agg["ttft_ms"]["p50"] == 1200
    assert agg["ttft_ms"]["p95"] == 2400
    assert agg["uncached_tokens"]["total"] == 500 + 5000
    assert agg["prompt_tokens"]["max"] == 5000
    # PERCEIVED 合并日志 + soak JSON 两源（n=2）。
    assert agg["perceived_ms"]["n"] == 2
    assert agg["perceived_ms"]["p50"] == 1800
    assert agg["first_audio_ms"]["n"] == 2
    assert agg["first_audio_ms"]["p95"] == 2600
    assert agg["bad_markers"]["REPEAT_CROSS_TURN_EMPTY"] == 1
    assert agg["guard_markers"]["[stall-ladder]"] == 1
    assert agg["flow20_pass"] is True


def test_build_live_plan_per_arm_and_restore():
    plan = aba.build_live_plan(python="/py", repo=Path("/repo"), arms=(aba.ARM1, aba.ARM4))
    assert [p["arm"] for p in plan] == [aba.ARM1, aba.ARM4, "RESTORE"]
    # 探针要本地 TTS 合成刺激音频（E2E 姿势），缺省注入 BOK_LOCAL_TTS=1。
    assert plan[0]["env"]["BOK_LOCAL_TTS"] == "1"
    assert plan[0]["env"].get("BOK_SLOT_ACTOR") is None
    assert plan[1]["env"]["BOK_SLOT_ACTOR"] == "1"
    assert plan[1]["env"]["BOK_REPEAT_GUARD"] == "0"
    assert plan[1]["commands"]["arm4_routing_put"]
    # 每臂命令齐：down/serve/flow20/soak；RESTORE 无探针。
    assert plan[0]["commands"]["flow20"][-1].endswith("probe_flow_20rounds.py")
    assert any("probe_latency_soak.py" in c for c in plan[0]["commands"]["soak"])
    assert "flow20" not in plan[2]["commands"]


def test_arm4_route_payload_and_restore():
    """ARM4 路由 PUT 体 + 还原体（live 的云端臂凭据只走请求体，不落盘）。"""
    payload = aba.build_a_reply_route_payload(
        base_url="https://api.deepseek.com/v1", model="deepseek-chat", api_key="sk-test"
    )
    lane = payload["lanes"]["a_reply"]
    assert lane["provider"] == "openai"
    assert lane["model"] == "deepseek-chat"
    assert lane["extra"]["enable_thinking"] is False
    # 还原体：原始 local 档原样回写（空 api_key=CP 保留旧值语义）。
    original = json.dumps(
        {
            "lanes": {
                "a_reply": {
                    "provider": "local",
                    "base_url": "http://127.0.0.1:1237/v1",
                    "model": "huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit",
                    "api_key": "",
                }
            }
        }
    )
    restore = aba.build_restore_route_payload(original)
    assert restore["lanes"]["a_reply"]["provider"] == "local"
    assert restore["lanes"]["a_reply"]["base_url"] == "http://127.0.0.1:1237/v1"
    # 空串/坏串=空 lanes（no-op，不误清车道）。
    assert aba.build_restore_route_payload("") == {"lanes": {}}
    assert aba.build_restore_route_payload("{not json") == {"lanes": {}}


# ---------------------------------------------------------------------------
# 回包 sanity
# ---------------------------------------------------------------------------
def test_cantonese_like_short_reply_not_false_negative():
    # direct 首跑实弹：真粤语短回复「好嘅，收到。」只带 1 个强特征字——
    # 阈值必须 ≥1 才不假阴。
    assert aba.cantonese_like("好嘅，收到。")
    assert aba.cantonese_like("我哋會即刻提交資料，你留意通知就得。")
    assert not aba.cantonese_like("好的，收到您的号码，我们现在安排专员添加您。")
    # 系词「係」=粤语信号（direct 二跑实弹假阴）；繁体书面语 關係/聯繫 不算。
    assert aba.cantonese_like("原來係我剛才講錯，係專員會加你WhatsApp。")
    assert not aba.cantonese_like("關於這件事情的關係與聯繫方式，我們稍後通知您。")


def test_script_read_stats_verbatim_vs_free():
    script = "好嘅，唔該晒你今日嘅時間，再見！"
    stats = aba.script_read_stats(script, script)
    assert stats["whole_over_threshold"] is True
    assert stats["sentences_over_threshold"] >= 1
    free = aba.script_read_stats("明白你嘅情況，我幫你睇下訂單先。", script)
    assert free["whole_over_threshold"] is False
    assert free["sentences_over_threshold"] == 0


def test_sanity_check_shape():
    ok = aba.sanity_check("好嘅，我幫你 check 下。", lang="cantonese", script="唔該你留個號碼")
    assert ok["non_empty"] is True
    assert ok["cantonese_ok"] is True
    empty = aba.sanity_check("", lang="cantonese", error="HTTP 500")
    assert empty["non_empty"] is False
    assert empty["error"] == "HTTP 500"
    # 非粤语轮不做粤语判据（None=不适用，不是 False）。
    zh = aba.sanity_check("好的，收到。", lang="zh")
    assert zh["cantonese_ok"] is None


def test_aggregate_direct_excludes_closing_step_pathology():
    def _row(round_no, closing, reply, script, ttft, ptok, est):
        return {
            "round": round_no,
            "closing_step": closing,
            "results": {
                "ARM1": {
                    "ttft_ms": ttft,
                    "tps": 40.0,
                    "prompt_tokens": ptok,
                    "est_tokens": est,
                    "sanity": aba.sanity_check(reply, lang="cantonese", script=script),
                }
            },
        }

    rows = [
        _row(2, False, "好嘅，收到。", "唔該你留個號碼", 1000, 500, 520),
        # 收尾步念正稿：属正常业务行为，不计病理。
        _row(15, True, "好嘅，唔該晒你今日嘅時間，再見！", "好嘅，唔該晒你今日嘅時間，再見！", 900, 400, 380),
    ]
    agg = aba.aggregate_direct(rows)["ARM1"]
    assert agg["n"] == 2
    assert agg["pathology_rows"] == 1
    assert agg["script_whole_over"] == 0
    assert agg["sanity_pass"] == 2
    assert agg["est_real_ratio"] == round((520 / 500 + 380 / 400) / 2, 3)


# ---------------------------------------------------------------------------
# simulate_call 冒烟（仓库内纯渲染；不碰栈）
# ---------------------------------------------------------------------------
def test_simulate_call_smoke_two_shapes():
    template = aba.load_template_fixture(aba.TEMPLATE_ID)
    rounds = [
        {"text": "你好", "note": "开场"},
        {"text": "我係陳大文", "note": "身份确认"},
        {"text": "點解要問我邊個平台買嘅？", "tag": "platform_question", "note": "平台反问"},
        {"text": "拼多多買嘅", "note": "平台答"},
    ]
    sim = aba.simulate_call(
        template=template,
        rounds=rounds,
        persona=aba.FIXTURE_PERSONA,
        object_card=aba.FIXTURE_OBJECT,
        object_brief=aba.FIXTURE_OBJECT_BRIEF,
        lang="cantonese",
    )
    rows = sim["rows"]
    assert len(rows) == 4
    # 首轮=身份步推进进直念步（say 车道，零 LLM 请求）。
    assert rows[0]["lane"] == "say"
    # ARM1 system=现渲染函数（前缀+人设，3000+ 字符量级）；槽位=stub 角色卡。
    assert sim["system_chars"]["ARM1"] > 3000
    assert 300 <= sim["system_chars"]["slot"] <= 460
    llm_rows = [r for r in rows if r["lane"] == "llm"]
    assert llm_rows, "至少一轮 LLM 请求"
    for r in llm_rows:
        for shape in ("ARM1", "slot"):
            s = r["shapes"][shape]
            assert s["est_tokens"] > 0
            assert s["messages"][0]["role"] == "system"
            assert s["messages"][-1]["role"] == "user"
            assert s["uncached_est"] <= s["est_tokens"]
        lo, hi = aba.SLOT_TASK_LIMITS
        assert lo <= r["shapes"]["slot"]["extra_chars"] <= hi
        # ARM1 的易变尾部长于槽位任务块（本轮 A/B 的核心断言）。
        assert r["shapes"]["ARM1"]["extra_chars"] > r["shapes"]["slot"]["extra_chars"]
    # 聚合可用（R2 位在场）。
    agg = aba.aggregate_offline(rows)
    assert agg["ARM1"]["total_uncached_tokens"] > agg["slot"]["total_uncached_tokens"]
    assert aba.pick_round(rows, 2) is not None
