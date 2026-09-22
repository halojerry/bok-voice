"""分支动作（A-②）+ 分支罐头快路（A-①）实弹验收探针（2026-09-20 路线 A）。

场景（真栈 + TTS 现场合成推流，骨架照 probe_flow_graph / e2e_real_customer）：

1. 经 CP API 建探针模板（6 步 zh，第 2 步 ref 带 5 条「如果客户X→【动作】Y」分支、
   第 5 步带 1 条同位自跳分支用于 jump_noop）→ 建模拟通话（建单快照）→ 等开场白。
2. 每腿一通真通话；轮次表统一以「warmup（身份确认，推 0→1）」起手——分支动作只认
   **当前步** ref 的分支，必须先把流程推进到第 2 步（分支步）触发语才打得到。
3. 六条腿（`--leg`，一腿一进程一通真通话）：
   - canned   「无动作分支」命中 → 罐头快路：前置 `pregen_tts.py --branches` 真物化
     （--branch-status 复核 ok=真落盘）→ 断言 `BRANCH_CANNED hit step=2` + 该轮
     assistant 行 `gen=script`/`provider=branch-canned` + 回复文本与分支应答逐字一致。
   - refuse   「【收线】」命中 → 断言 `BRANCH_ACTION refuse step=2` + 该轮
     gen=script/provider=branch-refuse 且文本=分支台词逐字 + 通话随后真收线
     （call ENDED、收线时刻 ≈14s 给宽容差）+ 该轮是全通最后一条 assistant 行
     （收线前无 LLM 生成）。
   - handoff  「【转人工】」命中 → 断言 `BRANCH_ACTION handoff step=2` +
     `call_sessions.assist_status == "notified"`（真 CP 落库）+ 该轮仍走 LLM
     （gen=llm/provider=branch-notify）+ 后续中性轮照常应答（打铃不中断通话）。
   - jump     「【跳第5步】」命中 → 断言 `BRANCH_ACTION jump step=5` + 该轮
     assistant 行 provider=branch-jump 且 template_step=5 + 随后在第 5 步推同位
     自跳触发语 → `BRANCH_ACTION jump_noop step=5` 且该窗口**零**位移宣告
     （kind=jump 不出现）。
   - hold     「【留本步】」命中 → 断言 `BRANCH_ACTION hold step=2` + 触发轮与
     验证轮窗口零推进行（rule=auto/rule=confirm/judge(bg)=confirm）+ 触发轮正常
     应答（罐头或 LLM，不哑）+ 验证轮（最后一轮）assistant 行 template_step 仍=2。
   - kill     `--leg kill`（**须先以 BOK_BRANCH_ACTION=0 重启 worker**，env 进程级
     定死）：同一 jump 触发语 → 断言全程窗口零 `BRANCH_ACTION`、零
     `BRANCH_CANNED hit`（miss 允许——A-① 回退语义）、零 branch-* 轮、无任何
     template_step=5 轮（不出现行为跳跃）。

测试句形铁律（AGENTS.md）：客户话音句不带逗号/大换气、≥10 字（QWEN3_ASR_PAUSE_
COMMIT_MIN_CHARS=10 的 pause 提交门）；对象名 E2E- 前缀=心跳豁免。分支条件的
命中面（verdict 家族 + 2-gram）已对真 flow.match_step_branch 离线校验
（tests/test_branch_action_probe.py 钉住，含近失转写容忍变体）。

物化（canned/hold 腿前置）：probe 内部先建模板+人设+通话（不连接），再起
`pregen_tts.py --branches --persona <探针人设>` 子进程（--texts-file 只物化目标
分支，烧真金云配额），随后 `--branch-status` 复核缓存真落盘；物化不上=腿 FAIL
（判据不放宽）。

用法：<python> scripts/probe_branch_action.py --leg canned|refuse|handoff|jump|hold|kill
      [--lang zh] [--persona-voice ...] [--keep-template] [--selftest]
前置：`python tools/bok.py serve`（CP 8000 / LiveKit 7880 / ASR 8787 / TTS 8788）；
kill 腿前置=以 BOK_BRANCH_ACTION=0 重启 agent worker（探针不代重启）。
报告：reports/branch-action/<ts>-<leg>.json。退出码：判据全过 0，否则 1。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
from livekit import rtc

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
sys.path.insert(0, str(_SCRIPTS))

import e2e_real_customer as erc  # noqa: E402  复用骨架:建通/推流/收音/turns/日志路径

REPORT_DIR = _ROOT / "reports" / "branch-action"

# auth-on 栈：CP 请求带机器通道 Bearer，未设 env 零变化（与 probe_flow_graph 同款）。
_CP_HEADERS: dict[str, str] = {}
if os.environ.get("BOK_CP_TOKEN", "").strip():
    _CP_HEADERS["Authorization"] = f"Bearer {os.environ['BOK_CP_TOKEN'].strip()}"

ACCOUNT_ID = os.environ.get("BOK_PROBE_ACCOUNT", "acc-001")
REFUSE_END_DELAY_S = 14.0  # agent._schedule_call_end 默认档（disposition=declined）
REFUSE_END_MIN_S = 5.0     # 收线时刻宽容差下界（告别念完 + 任务调度余量）
REFUSE_END_MAX_S = 30.0    # 上界（慢生成轮的告别合成也算内）
ASSIST_TIMEOUT_S = 25.0    # assist_status=notified 落库等候（fire-and-forget 上报）
ENDED_TIMEOUT_S = 45.0     # refuse 腿等 call ENDED 的上限（14s 档 + 余量）

# ---- 打点正则（jump_noop 必须排在 jump 前防前缀误吃；同 probe_flow_graph 口径）----
RE_BRANCH_ACTION = re.compile(r"BRANCH_ACTION\s+(refuse|handoff|jump_noop|jump|hold)\b(.*)$")
RE_BRANCH_CANNED = re.compile(r"BRANCH_CANNED\s+(hit|miss)\b(.*)$")
# 推进行（hold 腿「零推进」断言面）：规则路与背景 judge 路两族。
RE_ADVANCE = re.compile(r"\[flow\]\s+(rule=auto|rule=confirm|judge\(bg\)=confirm)\s+step=")
BRANCH_PROVIDERS = ("branch-canned", "branch-refuse", "branch-notify", "branch-jump")

# ---- 探针模板（6 步 zh；第 2 步=分支步 5 条动作分支；第 5 步=同位自跳 1 条）----
# 条件设计约束（已对真 match_step_branch 离线校验）：
# - 条件**不含「问」**：QUESTION 族词会以 0 bigram 抢走一切提问轮（家族优先）。
# - 每条条件与自己的触发语 2-gram ≥3、与其它腿触发语 =0（近失转写仍 ≥2）。
PROBE_STEPS: list[dict] = [
    {"goal": "确认身份", "ref": "你好，请问是{姓名}吗？"},
    {
        "goal": "说明来意核实签收",
        "ref": (
            "我这边系统显示这个包裹送达到的时候家里没有人签收，想跟您核实一下。\n"
            "如果客户说打错电话了→【收线】不好意思打扰了，我们再核对一下资料，再见\n"
            "如果客户要找真人客服→【转人工】好的，我帮您转接人工同事\n"
            "如果客户说包裹几时送到→【跳第5步】\n"
            "如果客户说等等先→【留本步】好的，您慢慢看\n"
            "如果客户查运单号是多少→这个包裹编号我帮您查过了"
        ),
    },
    {"goal": "询问购买平台", "ref": "请问这件商品是在哪个平台购买的呢？"},
    {"goal": "说明理赔方案", "ref": "如果确认丢件，我们会按平台规则赔付。"},
    {
        "goal": "引导办理登记",
        "ref": (
            "我这边帮您登记办理理赔事项，请您放心。\n"
            "如果客户催进度到哪一步了→【跳第5步】"
        ),
    },
    {"goal": "收尾确认", "ref": "好的，感谢您的时间，再见。"},
]
# 模板热词：ASR 最弱项的触发词保护（词表随 /api/start 下发 sidecar）。
PROBE_HOTWORDS = "打错电话,真人客服,运单号,包裹,几时送到,签收,理赔"

# ---- 轮次话术（≥10 字、无逗号；与分支条件的 2-gram 命中面见 selftest）----
WARMUP_TEXT = "对的我就是本人有话请讲"          # 身份确认 → confirm → 推进 0→1
TRIGGERS: dict[str, str] = {
    "canned":  "我的运单号码到底是多少呢",        # 命中「查运单号是多少」无动作分支
    "refuse":  "你是不是打错电话了我不是这个人",  # 命中「说打错电话了」→【收线】
    "handoff": "我要找真人客服帮我来处理这个事",  # 命中「要找真人客服」→【转人工】
    "jump":    "这个包裹几时才能送到我手上啊",    # 命中「说包裹几时送到」→【跳第5步】
    "hold":    "这个事情你等等先我还想再了解了解",  # 命中「说等等先」→【留本步】
}
NOOP_TEXT = "你们这个进度到哪一步了啊"            # 第 5 步同位自跳（jump_noop 腿）
NEUTRAL_TEXT = "我知道了那你说吧我在听"          # handoff 腿后续中性轮（打铃不中断）
VERIFY_TEXT = "那你继续说我在听着呢"             # hold 腿验证轮（步号仍=2 的载体）
# 转写归因关键词（信息位：转写都没吐出来时断言不可归因于分支引擎，是 ASR 侧问题）
TRIGGER_KEYWORDS: dict[str, list[str]] = {
    "canned":  ["运单", "多少"],
    "refuse":  ["打错", "不是"],
    "handoff": ["真人", "客服"],
    "jump":    ["包裹", "几时", "送到"],
    "hold":    ["等等", "想想"],
}
# 无动作分支应答（物化目标+逐字比对目标）；与 PROBE_STEPS 第 2 步分支逐字节同源。
CANNED_RESP_RAW = "这个包裹编号我帮您查过了"
HOLD_RESP_RAW = "【留本步】好的，您慢慢看"
CANNED_RESP_TEXT = "这个包裹编号我帮您查过了"     # 无标记=剥标记后原文
HOLD_RESP_TEXT = "好的，您慢慢看"
REFUSE_RESP_TEXT = "不好意思打扰了，我们再核对一下资料，再见"

# 每腿轮次表：[(窗口名, 话术)]。kill 腿复用 jump 触发语（其应答剥标记后为空 →
# BOK_BRANCH_ACTION=0 时罐头腿自然落 BRANCH_CANNED miss，不会播录音污染断言）。
LEG_ROUNDS: dict[str, list[tuple[str, str]]] = {
    "canned":  [("warmup", WARMUP_TEXT), ("trigger", TRIGGERS["canned"])],
    "refuse":  [("warmup", WARMUP_TEXT), ("trigger", TRIGGERS["refuse"])],
    "handoff": [("warmup", WARMUP_TEXT), ("trigger", TRIGGERS["handoff"]),
                ("neutral", NEUTRAL_TEXT)],
    "jump":    [("warmup", WARMUP_TEXT), ("trigger", TRIGGERS["jump"]),
                ("noop", NOOP_TEXT)],
    "hold":    [("warmup", WARMUP_TEXT), ("trigger", TRIGGERS["hold"]),
                ("verify", VERIFY_TEXT)],
    "kill":    [("warmup", WARMUP_TEXT), ("trigger", TRIGGERS["jump"])],
}
# 需要前置物化的腿 → 物化的分支 resp 原文（含动作标记，--texts-file 逐字节匹配面）
LEG_MATERIALIZE: dict[str, list[str]] = {
    "canned": [CANNED_RESP_RAW],
    "hold": [HOLD_RESP_RAW],
}


# ---------------------------------------------------------------------------
# 纯函数（tests/test_branch_action_probe.py 直测；--selftest 无栈自检）
# ---------------------------------------------------------------------------
def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def probe_evidence(*, log_exists: bool, marks: list[int], expected_rounds: int) -> dict:
    """观测面可信度（同 probe_flow_graph.probe_evidence）：absence 判据的前提。"""
    grew = [b > a for a, b in zip(marks, marks[1:])]
    rounds_complete = len(marks) == expected_rounds + 1
    grew_each_round = bool(grew) and all(grew) and len(grew) == expected_rounds
    ok = bool(log_exists and rounds_complete and grew_each_round)
    return {
        "ok": ok,
        "log_exists": bool(log_exists),
        "expected_rounds": int(expected_rounds),
        "marks": list(marks),
        "rounds_complete": rounds_complete,
        "grew_each_round": grew_each_round,
    }


def parse_branch_events(lines: list[str]) -> list[dict]:
    """抽 `BRANCH_ACTION <kind> …` / `BRANCH_CANNED <hit|miss> …` 为事件字典（保序）。"""
    events: list[dict] = []
    for raw in lines:
        m = RE_BRANCH_ACTION.search(str(raw))
        source = "action"
        if not m:
            m = RE_BRANCH_CANNED.search(str(raw))
            source = "canned"
        if not m:
            continue
        ev: dict = {"kind": m.group(1), "source": source}
        for token in m.group(2).split():
            if "=" in token:
                key, value = token.split("=", 1)
                ev[key] = value
        events.append(ev)
    return events


def advance_lines(lines: list[str]) -> list[str]:
    """流程推进行原文（rule=auto/rule=confirm/judge(bg)=confirm，保序）。"""
    return [ln for ln in lines if RE_ADVANCE.search(str(ln))]


def evaluate_hold_advance(raw_windows: dict[str, list[str]],
                          windows: tuple[str, ...] = ("trigger", "verify")) -> bool:
    """hold 腿「零推进」（纯函数）：指定窗口零推进行（原文行判定——推进行不属
    BRANCH_ACTION 事件族）。窗口缺失=没观测，保守判 False（不空过）。"""
    for w in windows:
        if w not in raw_windows:
            return False
        if advance_lines(list(raw_windows.get(w) or [])):
            return False
    return True


def assistant_rows(turns: list[dict]) -> list[dict]:
    """assistant 轮行（时间序保序），带归一字段。"""
    rows: list[dict] = []
    for t in turns:
        if str(t.get("role") or "") != "assistant":
            continue
        rows.append({
            "provider": str(t.get("provider") or "").strip(),
            "gen": str(t.get("gen") or "").strip(),
            "template_step": _as_int(t.get("template_step"), -1),
            "transcript": str(t.get("transcript") or ""),
        })
    return rows


def rows_after_last_user(turns: list[dict]) -> list[dict]:
    """最后一条真客户轮（机制行排除）之后的 assistant 行（保序）。"""
    last_idx = -1
    for i, t in enumerate(turns):
        if str(t.get("role") or "") == "user" and not str(t.get("provider") or "").strip():
            last_idx = i
    if last_idx < 0:
        return []
    return assistant_rows(turns[last_idx + 1:])


def find_user_idx(turns: list[dict], keywords: list[str], start: int = 0) -> int:
    """第一条转写含任一关键词的真客户轮下标（机制行排除；找不到 -1）。

    ASR 措辞会漂，故只锚关键词不锚全句；关键词取自各轮话术的实转写稳定词。
    """
    for i in range(max(0, start), len(turns)):
        t = turns[i]
        if str(t.get("role") or "") != "user" or str(t.get("provider") or "").strip():
            continue
        text = str(t.get("transcript") or "")
        if any(k in text for k in keywords):
            return i
    return -1


def user_understood(turns: list[dict], keywords: list[str]) -> bool:
    """客户轮转写里出现任一触发词（机制行排除）——ASR 归因信息位。"""
    for t in turns:
        if str(t.get("role") or "") != "user" or str(t.get("provider") or "").strip():
            continue
        text = str(t.get("transcript") or "").lower()
        if any(k.lower() in text for k in keywords if k.strip()):
            return True
    return False


def evaluate_leg(
    *,
    leg: str,
    evidence: dict,
    events_by_window: dict[str, list[dict]],
    turns: list[dict],
    call_row: dict | None = None,
    refuse_end_elapsed_s: float | None = None,
    answered_by_window: dict[str, bool] | None = None,
    materialized: dict[str, str] | None = None,
    hold_advance_ok: bool | None = None,
) -> dict:
    """主判据（纯函数）。leg→判据集见模块 docstring；全过才 PASS。

    absence 判据（kill 腿零打点、hold 腿零推进）在 evidence_ok=False / 观测缺席时
    恒 False（无观测不成立 PASS，review R1 假绿闸同款）。正向判据要真观测到事件。
    """
    ev_ok = bool((evidence or {}).get("ok"))
    rows = assistant_rows(turns)
    trigger_events = list(events_by_window.get("trigger") or [])
    answered = dict(answered_by_window or {})
    material = dict(materialized or {})
    info: dict = {
        "branch_turns": [r for r in rows if r["provider"] in BRANCH_PROVIDERS],
        "steps_seen": sorted({r["template_step"] for r in rows if r["template_step"] > 0}),
        "materialized": material,
    }

    def _kinds(window: str) -> list[str]:
        return [str(e.get("kind")) for e in (events_by_window.get(window) or [])]

    if leg == "canned":
        hit = [e for e in trigger_events
               if e.get("kind") == "hit" and _as_int(e.get("step"), -1) == 2]
        bc = [r for r in rows if r["provider"] == "branch-canned"]
        checks = {
            "evidence_ok": ev_ok,
            "canned_materialized": material.get(CANNED_RESP_RAW) == "ok",
            "canned_hit_logged": bool(hit),
            "turn_gen_script_provider": any(
                r["gen"] == "script" and r["provider"] == "branch-canned" for r in rows),
            "text_exact": bool(bc) and bc[-1]["transcript"].strip() == CANNED_RESP_TEXT,
        }
    elif leg == "refuse":
        logged = [e for e in trigger_events
                  if e.get("kind") == "refuse" and _as_int(e.get("step"), -1) == 2]
        br = [r for r in rows if r["provider"] == "branch-refuse"]
        status = str((call_row or {}).get("status") or "")
        elapsed = refuse_end_elapsed_s
        checks = {
            "evidence_ok": ev_ok,
            "refuse_logged": bool(logged),
            "turn_gen_script_text_exact": bool(br) and br[-1]["gen"] == "script"
            and br[-1]["transcript"].strip() == REFUSE_RESP_TEXT,
            "call_ended": status.lower() == "ended",
            "end_timing": elapsed is not None
            and REFUSE_END_MIN_S <= elapsed <= REFUSE_END_MAX_S,
            "no_llm_after_refuse": bool(rows) and rows[-1]["provider"] == "branch-refuse",
        }
    elif leg == "handoff":
        logged = [e for e in trigger_events
                  if e.get("kind") == "handoff" and _as_int(e.get("step"), -1) == 2]
        bn = [r for r in rows if r["provider"] == "branch-notify"]
        # 触发轮仍走 LLM：触发客户轮（真人/客服 词锚）之后存在 gen=llm 应答行。
        # （provider=branch-notify 只作信息位：LLM 慢到 late-answer 兜底抢先出声
        # 时，consume-once 的 provider 戳会被 late-answer 轮消费掉——2026-09-20
        # 实弹，引擎语义如此，判据不依赖它。）
        trig_idx = find_user_idx(turns, ("真人", "客服"))
        neu_idx = find_user_idx(turns, ("说吧", "在听", "听"), trig_idx + 1) \
            if trig_idx >= 0 else -1
        # 触发段=触发客户轮到中性客户轮之间（中性轮自己的 llm 行不算数）。
        trig_segment = turns[trig_idx + 1: neu_idx if neu_idx > trig_idx else len(turns)] \
            if trig_idx >= 0 else []
        still_llm = any(r["gen"] == "llm" for r in assistant_rows(trig_segment))
        # 中性轮应答（打铃不中断）：中性客户轮（说吧/听 词锚）之后仍有应答行；
        # ASR 未吐中性轮时退音频级证据（answered.neutral=该窗真听到回复声）。
        if neu_idx >= 0:
            followup = bool(answered.get("neutral")) \
                and bool(assistant_rows(turns[neu_idx + 1:]))
        else:
            followup = bool(answered.get("neutral"))
        info["trigger_user_found"] = trig_idx >= 0
        info["neutral_user_found"] = neu_idx >= 0
        checks = {
            "evidence_ok": ev_ok,
            "handoff_logged": bool(logged),
            "assist_notified": str((call_row or {}).get("assist_status") or "") == "notified",
            "turn_still_llm": still_llm,
            "followup_answered": followup,
        }
    elif leg == "jump":
        logged = [e for e in trigger_events
                  if e.get("kind") == "jump" and _as_int(e.get("step"), -1) == 5]
        noop_kinds = _kinds("noop")
        noop_logged = "jump_noop" in noop_kinds and "jump" not in noop_kinds
        checks = {
            "evidence_ok": ev_ok,
            "jump_logged": bool(logged),
            "jump_turn_provider": any(
                r["provider"] == "branch-jump" and r["template_step"] == 5 for r in rows),
            "noop_logged": noop_logged,
        }
    elif leg == "hold":
        logged = [e for e in trigger_events
                  if e.get("kind") == "hold" and _as_int(e.get("step"), -1) == 2]
        trig_idx = find_user_idx(turns, ("等等", "想想"))
        ver_idx = find_user_idx(turns, ("听着", "继续说", "在听", "听"),
                                trig_idx + 1 if trig_idx >= 0 else 0)
        trig_rows = assistant_rows(
            turns[trig_idx + 1: ver_idx if ver_idx > trig_idx else len(turns)]) \
            if trig_idx >= 0 else []
        ver_rows = assistant_rows(turns[ver_idx + 1:]) if ver_idx >= 0 else []
        trig_answered = bool(answered.get("trigger")) and bool(trig_rows)
        # 验证轮（最后一轮）应答仍停在第 2 步：验证轮真出声 + 其应答行步号全=2。
        stay = bool(answered.get("verify")) and bool(ver_rows) \
            and all(r["template_step"] == 2 for r in ver_rows)
        info["trigger_user_found"] = trig_idx >= 0
        info["verify_user_found"] = ver_idx >= 0
        checks = {
            "evidence_ok": ev_ok,
            "hold_logged": bool(logged),
            "no_advance": bool(hold_advance_ok),
            "trigger_answered": trig_answered,
            "stay_step2": stay,
        }
    elif leg == "kill":
        all_events = [e for evs in events_by_window.values() for e in evs]
        action_events = [e for e in all_events if e.get("source") == "action"]
        canned_hits = [e for e in all_events
                       if e.get("source") == "canned" and e.get("kind") == "hit"]
        checks = {
            "evidence_ok": ev_ok,
            "killswitch_no_action_logs": ev_ok and not action_events,
            "killswitch_no_canned_hit": ev_ok and not canned_hits,
            # 轮面 absence 的可观测前提：这通电话真跑出了 LLM 应答轮。
            "turns_present": bool(rows),
            "killswitch_no_branch_turns": bool(rows) and not info["branch_turns"],
            "killswitch_no_step_jump": bool(rows)
            and all(r["template_step"] != 5 for r in rows),
        }
    else:
        raise ValueError(f"unknown leg: {leg}")
    return {"checks": checks, "pass": all(checks.values()),
            "evidence": evidence or {}, "events": events_by_window, "info": info}


# ---------------------------------------------------------------------------
# CP 侧：模板 / 通话 / 日志窗口
# ---------------------------------------------------------------------------
def _cp(path: str, *, method: str = "GET", **kw) -> httpx.Response:
    kw.setdefault("timeout", 15)
    return httpx.request(method, f"{erc.CONTROL_PLANE_URL}{path}", headers=_CP_HEADERS, **kw)


def create_probe_template(lang: str) -> str:
    """经 CP 建探针模板（第 2 步分支动作步、第 5 步同位自跳步；名字含 probe）。"""
    resp = _cp("/api/templates", method="POST", json={
        "account_id": ACCOUNT_ID,
        "name": f"probe-branch-action-{lang}-{int(time.time())}",
        "language": lang,
        "steps_json": json.dumps(PROBE_STEPS, ensure_ascii=False),
        "hotwords": PROBE_HOTWORDS,
    })
    resp.raise_for_status()
    return str(resp.json().get("id") or "")


def create_probe_call(lang: str, template_id: str, voice: str) -> tuple[str, str, str]:
    """E2E- 前缀对象（心跳豁免）+ 人设 + 建单（显式 template_id 快照）。返回
    (call_id, persona_id, resolved_voice)——persona_id 供 --branches 物化对音色。"""
    ts = int(time.time() * 1000) % 100000
    obj = _cp("/api/objects", method="POST", params={"account_id": ACCOUNT_ID}, json={
        "display_name": f"E2E-陳小明-{ts}",
        "role_template": "buyer",
        "language": lang,
        "background": "branch action probe",
        "template_id": template_id,
    })
    obj.raise_for_status()
    obj = obj.json()
    persona = _cp("/api/personas", method="POST", json={
        "account_id": ACCOUNT_ID,
        "name": f"E2E分支动作{lang}",
        "language": lang,
        "tone": "礼貌专业",
        "reference_audio": voice,
    })
    persona.raise_for_status()
    persona = persona.json()
    call = _cp("/api/calls", method="POST", json={
        "account_id": ACCOUNT_ID,
        "object_id": obj["id"],
        "persona_id": persona["id"],
        "template_id": template_id,
        "mode": "live",
        "direction": "webrtc",
        "language": lang,
    })
    call.raise_for_status()
    return (str(call.json()["id"]), str(persona.get("id") or ""),
            str(persona.get("reference_audio") or voice))


def delete_template(template_id: str) -> None:
    try:
        _cp(f"/api/templates/{template_id}", method="DELETE")
    except Exception:  # noqa: BLE001 - 清理失败只留痕
        pass


def fetch_call_row(call_id: str) -> dict:
    try:
        row = _cp(f"/api/calls/{call_id}").json()
        return row if isinstance(row, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def fetch_turns_authed(call_id: str, settle_s: float = 12.0) -> list[dict]:
    """erc.fetch_turns 的 auth-on 档（同 probe_flow_graph.fetch_turns_authed）。"""
    last: list[dict] = []
    stable = 0
    deadline = time.perf_counter() + settle_s
    while time.perf_counter() < deadline:
        rows = httpx.get(
            f"{erc.CONTROL_PLANE_URL}/api/calls/{call_id}/turns",
            headers=_CP_HEADERS, timeout=10,
        ).json()
        if not isinstance(rows, list):
            rows = []
        if rows and len(rows) == len(last):
            stable += 1
            if stable >= 2:
                return rows
        else:
            stable = 0
        last = rows
        await asyncio.sleep(1.5)
    return last


async def wait_call_ended(call_id: str, timeout_s: float) -> tuple[bool, float | None]:
    """轮询 call 状态到 ENDED；返回 (是否 ENDED, 本函数内等待的秒数)。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout_s:
        row = fetch_call_row(call_id)
        if str(row.get("status") or "").lower() == "ended":
            return True, time.perf_counter() - t0
        await asyncio.sleep(1.0)
    return False, None


async def wait_assist_notified(call_id: str, timeout_s: float) -> str:
    """轮询 assist_status 直到非空（notified/done）；超时返回当前值。"""
    deadline = time.perf_counter() + timeout_s
    status = ""
    while time.perf_counter() < deadline:
        status = str(fetch_call_row(call_id).get("assist_status") or "")
        if status:
            return status
        await asyncio.sleep(1.0)
    return status


def log_windows(marks: list[int]) -> list[list[str]]:
    """按字节偏移切 agent.log（erc.log_windows 共享件，保留本地名免散改调用点）。"""
    return erc.log_windows(marks)


# ---------------------------------------------------------------------------
# 物化（pregen_tts.py --branches 子进程 + --branch-status 落盘复核）
# ---------------------------------------------------------------------------
def _pregen_env() -> dict:
    env = dict(os.environ)
    env.setdefault("MINIMAX_MODEL", "speech-2.8-hd")  # 与 worker 默认档同源
    if _CP_HEADERS.get("Authorization"):
        env["BOK_CP_TOKEN"] = _CP_HEADERS["Authorization"].removeprefix("Bearer ")
    if not env.get("SSL_CERT_FILE"):
        with contextlib.suppress(Exception):
            import certifi
            env["SSL_CERT_FILE"] = certifi.where()
    return env


@contextlib.contextmanager
def _texts_file(texts: list[str]):
    """--texts-file 临时文件（一行一条 resp 原文，逐字节）。"""
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        for t in texts:
            f.write(str(t).strip() + "\n")
        f.close()
        yield Path(f.name)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(f.name)


def materialize_branches(persona_id: str, raw_texts: list[str]) -> dict[str, str]:
    """真物化 + 落盘复核。返回 {resp 原文: ok|missing|ph}（--branch-status 的 stdout
    JSON）。物化走真云配额；复核走真缓存目录磁盘读——两者任一失败=腿 FAIL。"""
    if not raw_texts or not persona_id:
        return {}
    py = sys.executable
    script = str(_ROOT / "scripts" / "pregen_tts.py")
    with _texts_file(raw_texts) as tf:
        cmd = [py, script, "--branches", "--persona", persona_id,
               "--texts-file", str(tf), "--cp", erc.CONTROL_PLANE_URL]
        print(f"[branch-action] 物化子进程：{' '.join(cmd)}", flush=True)
        r1 = subprocess.run(cmd, cwd=str(_ROOT), env=_pregen_env(),
                            capture_output=True, text=True, timeout=600)
        tail = [ln for ln in (r1.stdout or "").strip().splitlines() if ln.strip()][-8:]
        print("[branch-action] 物化输出尾：\n  " + "\n  ".join(tail), flush=True)
        if r1.returncode != 0:
            print(f"[branch-action] 物化 FAILED rc={r1.returncode} "
                  f"stderr={(r1.stderr or '')[-500:]}", flush=True)
        cmd2 = [py, script, "--branch-status", "--persona", persona_id,
                "--texts-file", str(tf), "--cp", erc.CONTROL_PLANE_URL]
        r2 = subprocess.run(cmd2, cwd=str(_ROOT), env=_pregen_env(),
                            capture_output=True, text=True, timeout=120)
        for line in (r2.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    status = json.loads(line).get("branch_status")
                    if isinstance(status, dict):
                        return {str(k): str(v) for k, v in status.items()}
                except Exception:  # noqa: BLE001
                    pass
        print(f"[branch-action] branch-status 解析失败 rc={r2.returncode} "
              f"stdout={(r2.stdout or '')[-300:]} stderr={(r2.stderr or '')[-300:]}",
              flush=True)
    return {}


# ---------------------------------------------------------------------------
# 跑一腿
# ---------------------------------------------------------------------------
# wait_log_stable/log_windows 已收编 erc 共享件（2026-09-22 三探针单点）；
# 本地别名免散改调用点，语义=「marks[0]=通话前大小、窗口 k=第 k 轮」。
wait_log_stable = erc.wait_log_stable


async def run_leg(*, leg: str, lang: str, voice: str, keep_template: bool) -> dict:
    rounds = LEG_ROUNDS[leg]
    print(f"\n[branch-action] 腿={leg} lang={lang} rounds={[n for n, _ in rounds]}",
          flush=True)
    template_id = create_probe_template(lang)
    print(f"[branch-action] template={template_id}", flush=True)
    try:
        call_id, persona_id, resolved_voice = create_probe_call(lang, template_id, voice)
        print(f"[branch-action] call={call_id} persona={persona_id} voice={resolved_voice!r}",
              flush=True)
        materialized: dict[str, str] = {}
        want = LEG_MATERIALIZE.get(leg) or []
        if want and persona_id:
            materialized = materialize_branches(persona_id, want)
            print(f"[branch-action] 物化复核：{materialized}", flush=True)
            missing = [t for t in want if materialized.get(t) != "ok"]
            if missing:
                print(f"[branch-action] 物化未落盘：{missing}（判据不放宽，腿将 FAIL）",
                      flush=True)
        return await _run_leg_with_stack(
            leg=leg, lang=lang, template_id=template_id, call_id=call_id,
            rounds=rounds, materialized=materialized)
    finally:
        if not keep_template and template_id:
            delete_template(template_id)


async def _run_leg_with_stack(*, leg: str, lang: str, template_id: str, call_id: str,
                              rounds: list[tuple[str, str]],
                              materialized: dict[str, str]) -> dict:
    # 推尾静音（AGENTS「注意推尾静音」）：裸句尾直接断流会让 sidecar 整窗重解在
    # 句尾幻听出「那。」类残词 → 迟到 FINAL 修正轮 interrupt 掉在播回复（罐头/直念
    # 被 0.4s 掐断、assistant item 不落库，2026-09-20 canned 腿实弹）。句尾垫 0.8s
    # 静音让整窗解码与已提交文本等价、修正被丢，回复播完不被打断。
    pcms = {name: erc.tts_pcm(text, lang) + b"\x00" * int(TRAILING_SILENCE_S * PCM_BYTES_PER_S)
            for name, text in rounds}
    room = rtc.Room()
    agent_audio = bytearray()
    read_tasks: list[asyncio.Task] = []

    def attach(track) -> None:
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        if getattr(track, "name", "") not in ("roomio_audio", "background_audio"):
            return

        async def _read() -> None:
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            try:
                async for event in stream:
                    frame = getattr(event, "frame", event)
                    agent_audio.extend(bytes(frame.data))
            except Exception:  # noqa: BLE001 - 断轨收尾
                pass
            finally:
                with contextlib.suppress(Exception):
                    await stream.aclose()

        read_tasks.append(asyncio.get_running_loop().create_task(_read()))

    room.on("track_subscribed", lambda track, _p, _pt: attach(track))
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                attach(track)

    measures: list[dict] = []
    setup_ok = False
    refused = False
    # refuse 收线时刻口径：用户话音推完（≈turn 提交、分支命中、排程起点）→ call
    # ENDED 的墙钟秒。t_commit_est=触发轮起推时刻+音频时长（提交窗口）。
    t_commit_est: float | None = None
    t_wait_started: float | None = None
    ended_wait_s: float | None = None
    marks: list[int] = [erc.LOG_PATH.stat().st_size if erc.LOG_PATH.exists() else 0]
    try:
        data = _cp("/api/token", method="POST",
                   json={"account_id": ACCOUNT_ID, "call_id": call_id}).json()
        await room.connect(data["serverUrl"], data["participantToken"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("customer-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        setup_ok = await erc.wait_greeting(agent_audio)
        if not setup_ok:
            print("[branch-action] WARN 开场白 45s×3 未出声，照常推进", flush=True)
        greeting_wait, greeting_quiet = await wait_playout_end(agent_audio)
        print(f"[branch-action] 开场白播完等候 {greeting_wait:.2f}s quiet_ok={greeting_quiet}",
              flush=True)
        await asyncio.sleep(0.3)

        for name, text in rounds:
            pre_wait, pre_quiet = await wait_playout_end(agent_audio)
            print(f"    {name:>8} 起推前等候 {pre_wait:.2f}s quiet_ok={pre_quiet}", flush=True)
            t_push0 = time.perf_counter()
            m = await erc.play_and_listen(audio_source, agent_audio, pcms[name])
            m.update({"name": name, "text": text,
                      "pre_push_wait_s": round(pre_wait, 2), "pre_push_quiet": pre_quiet})
            measures.append(m)
            # 切窗前等日志落盘稳定（见 wait_log_stable）：上一轮的推进/分支日志
            # 必须落在本轮窗口内，防跨窗串行误判。
            marks.append(await wait_log_stable())
            first = f"{m['first_audio_ms'] / 1000:.2f}s" if m.get("first_audio_ms") is not None else "-"
            print(f"    {name:>8} 「{text}」 → 首声 {first} · 语音 {m.get('speech_s', 0):.1f}s · "
                  f"{'✓' if m.get('answered') else '✗哑'} · log+{marks[-1] - marks[-2]}B",
                  flush=True)
            if name == "trigger":
                t_commit_est = t_push0 + len(pcms[name]) / 32000.0
                if leg == "refuse":
                    # 收线=agent 侧 14s 延时任务；告别真播完后等 ENDED（不推下一轮）。
                    t_wait_started = time.perf_counter()
                    refused, ended_wait_s = await wait_call_ended(call_id, ENDED_TIMEOUT_S)
                    print(f"    {'ended':>8} call ENDED={refused} "
                          f"等候 {ended_wait_s}s", flush=True)
                if leg == "handoff":
                    assist = await wait_assist_notified(call_id, ASSIST_TIMEOUT_S)
                    print(f"    {'assist':>8} assist_status={assist or '(空)'}", flush=True)
            await asyncio.sleep(0.3 + ROUND_SETTLE_S)
    except Exception as exc:  # noqa: BLE001 - 单腿异常照常收尾并出报告
        print(f"[branch-action] 腿 {leg} 异常中断: {exc!r}", flush=True)
    finally:
        with contextlib.suppress(Exception):
            await room.disconnect()
        for t in read_tasks:
            t.cancel()
        try:
            _cp(f"/api/calls/{call_id}/hangup", method="POST")
            _cp(f"/api/calls/{call_id}/settle", method="POST", timeout=60)
        except Exception:  # noqa: BLE001
            pass

    turns = await fetch_turns_authed(call_id)
    evidence = probe_evidence(
        log_exists=erc.LOG_PATH.exists(), marks=marks, expected_rounds=len(rounds))
    windows = log_windows(marks)
    names = [n for n, _ in rounds]
    events_by_window = {name: parse_branch_events(windows[i])
                        for i, name in enumerate(names) if i < len(windows)}
    raw_windows = {name: windows[i] for i, name in enumerate(names) if i < len(windows)}
    if not evidence["ok"]:
        print(f"[branch-action] 观测面不足 → absence 判据不计 PASS：{evidence}", flush=True)

    # refuse 收线时刻（秒）=「话音推完(提交/排程起点)」→「ENDED 判定时刻」。
    refuse_elapsed: float | None = None
    if leg == "refuse" and refused and t_commit_est and t_wait_started \
            and ended_wait_s is not None:
        refuse_elapsed = (t_wait_started - t_commit_est) + ended_wait_s

    call_row = fetch_call_row(call_id) if leg in ("refuse", "handoff") else {}
    answered_by_window = {m["name"]: bool(m.get("answered")) for m in measures}

    res = evaluate_leg(
        leg=leg, evidence=evidence, events_by_window=events_by_window, turns=turns,
        call_row=call_row, refuse_end_elapsed_s=refuse_elapsed,
        answered_by_window=answered_by_window, materialized=materialized,
        hold_advance_ok=evaluate_hold_advance(raw_windows) if leg == "hold" else None)

    result = {
        "leg": leg,
        "lang": lang,
        "template_id": template_id,
        "call_id": call_id,
        "rounds": names,
        "texts": dict(rounds),
        "setup_ok": setup_ok,
        "evidence": evidence,
        "pacing": {"per_round": [
            {"name": m["name"], "wait_s": m.get("pre_push_wait_s"),
             "quiet_ok": m.get("pre_push_quiet")} for m in measures]},
        "trigger_understood": user_understood(
            turns, TRIGGER_KEYWORDS.get("jump" if leg == "kill" else leg, [])),
        "call_row": {k: call_row.get(k) for k in
                     ("status", "assist_status", "disposition", "ended_at",
                      "duration_s") if k in call_row},
        "measures": [{k: m.get(k) for k in ("name", "text", "first_audio_ms",
                                            "speech_s", "answered")} for m in measures],
        "refuse_end_elapsed_s": refuse_elapsed,
        "turn_rows": [{
            "role": str(t.get("role") or ""),
            "provider": str(t.get("provider") or ""),
            "gen": str(t.get("gen") or ""),
            "template_step": _as_int(t.get("template_step"), -1),
            "transcript": str(t.get("transcript") or "")[:120],
        } for t in turns],
        "verdict": res,
        "ts": int(time.time()),
    }
    print_leg(result)
    return result


def print_leg(res: dict) -> None:
    v = res["verdict"]
    print("\n" + "═" * 72, flush=True)
    print(f"腿 {res['leg']}  call={res['call_id']}  template={res['template_id']}",
          flush=True)
    print("═" * 72, flush=True)
    for m in res["measures"]:
        first = (f"{m['first_audio_ms'] / 1000:.2f}s"
                 if m.get("first_audio_ms") is not None else "无")
        print(f"  {m['name']:>8} 首声={first:>7} 语音={m.get('speech_s', 0):.1f}s "
              f"{'' if m.get('answered') else '✗哑'}  「{m['text']}」", flush=True)
    for name, evs in (v.get("events") or {}).items():
        print(f"  {name:>8} BRANCH 打点：{evs if evs else '（零）'}", flush=True)
    print(f"  branch 轮：{v['info']['branch_turns'] or '（无）'}", flush=True)
    print(f"  步号集合：{v['info']['steps_seen']}  "
          f"物化复核：{v['info']['materialized'] or '（本腿无需）'}", flush=True)
    print(f"  触发转写可辨={res['trigger_understood']}", flush=True)
    ev = res.get("evidence") or {}
    print(f"  观测面 evidence_ok={ev.get('ok')}（log_exists={ev.get('log_exists')} "
          f"rounds_complete={ev.get('rounds_complete')} "
          f"grew_each_round={ev.get('grew_each_round')}）", flush=True)
    if res.get("call_row"):
        print(f"  call 终态：{res['call_row']}  收线时刻≈{res.get('refuse_end_elapsed_s')}s",
              flush=True)
    for name, ok in v["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}", flush=True)
    print(f"  腿结论：{'PASS' if v['pass'] else 'FAIL'}", flush=True)


# ---------------------------------------------------------------------------
# 起推前「播完」等候（照 probe_flow_graph.wait_playout_end，含防早返护栏）
# ---------------------------------------------------------------------------
PCM_FRAME_BYTES = 640  # 20ms @ 16kHz/16bit 单声道
PCM_BYTES_PER_S = 32000
TRAILING_SILENCE_S = 1.2  # 每轮推流句尾静音垫（防 ASR 整窗重解幻听尾词→迟到修正打断回复）
ROUND_SETTLE_S = 1.0      # 轮间额外安定窗（上一轮迟到修正/残尾落定后再推下一轮）
QUIET_GAP_S = float(os.environ.get("BOK_PROBE_QUIET_GAP_S", "1.5"))
PLAYOUT_TIMEOUT_S = float(os.environ.get("BOK_PROBE_PLAYOUT_TIMEOUT_S", "25"))
PLAYOUT_MIN_SPEECH_S = float(os.environ.get("BOK_PROBE_PLAYOUT_MIN_SPEECH_S", "0.5"))
PLAYOUT_STABLE_WINDOWS = 3
PLAYOUT_POLL_S = 0.2


def trailing_silence_s(pcm: bytes, *, step: int = PCM_FRAME_BYTES,
                       threshold: float = 200.0) -> float:
    frames = len(pcm) // step
    silent = 0
    for i in range(frames - 1, -1, -1):
        if erc.frame_rms(pcm[i * step:(i + 1) * step]) >= threshold:
            break
        silent += 1
    return silent * (step / PCM_BYTES_PER_S)


def speech_seconds(pcm: bytes, processed: int = 0, *, step: int = PCM_FRAME_BYTES,
                   threshold: float = 200.0) -> tuple[float, int]:
    frames = len(pcm) // step
    speech = 0
    i = max(0, processed // step)
    while i < frames:
        if erc.frame_rms(pcm[i * step:(i + 1) * step]) >= threshold:
            speech += 1
        i += 1
    return speech * (step / PCM_BYTES_PER_S), i * step


async def wait_playout_end(agent_audio: bytearray, *, quiet_s: float = QUIET_GAP_S,
                           min_speech_s: float = PLAYOUT_MIN_SPEECH_S,
                           timeout_s: float = PLAYOUT_TIMEOUT_S) -> tuple[float, bool]:
    t0 = time.perf_counter()
    state = {"processed": 0, "speech": 0.0}
    stable = 0
    while time.perf_counter() - t0 < timeout_s:
        buf = bytes(agent_audio)
        speech, state["processed"] = speech_seconds(buf, state["processed"])
        state["speech"] += speech
        tail = trailing_silence_s(buf)
        if state["speech"] >= min_speech_s and tail >= quiet_s:
            stable += 1
            if stable >= PLAYOUT_STABLE_WINDOWS:
                return time.perf_counter() - t0, True
        else:
            stable = 0
        await asyncio.sleep(PLAYOUT_POLL_S)
    return time.perf_counter() - t0, False


# ---------------------------------------------------------------------------
# 无栈自检（--selftest）：纯函数正/反例
# ---------------------------------------------------------------------------
def _ev(*, log: bool = True, marks: list[int] | None = None, expected: int = 2) -> dict:
    _marks = [100 * (i + 1) for i in range(expected + 1)] if marks is None else marks
    return probe_evidence(log_exists=log, marks=_marks, expected_rounds=expected)


def _hold_inputs(*, advance: bool, answered_trigger: bool, answered_verify: bool,
                 turns: list[dict], evidence: dict | None = None) -> dict:
    """hold 腿 evaluate_leg 输入组装（no_advance 用真窗口原文判定）。"""
    raw_windows = {
        "warmup": ["[flow] rule=auto step=2 (call c1)"],
        "trigger": (["[flow] rule=auto step=3 (call c1)"] if advance
                    else ["BRANCH_ACTION hold step=2", "TTS_CACHE hit=1"]),
        "verify": ["[flow] say-step verbatim step=2"],
    }
    return evaluate_leg(
        leg="hold",
        evidence=evidence or _ev(expected=3),
        events_by_window={"trigger": parse_branch_events(raw_windows["trigger"])},
        turns=turns,
        answered_by_window={"trigger": answered_trigger, "verify": answered_verify},
        materialized={HOLD_RESP_RAW: "ok"},
        hold_advance_ok=evaluate_hold_advance(raw_windows))


def selftest() -> int:
    hit_line = "2026-09-20 10:00:00 BRANCH_CANNED hit step=2 branch_len=13 (call probe-1)"
    miss_line = "BRANCH_CANNED miss step=2 branch_len=13 (call probe-1)"
    hold_line = "BRANCH_ACTION hold step=2"
    refuse_line = "BRANCH_ACTION refuse step=2 text_len=20"
    handoff_line = "BRANCH_ACTION handoff step=2"
    jump_line = "BRANCH_ACTION jump step=5"
    noop_line = "BRANCH_ACTION jump_noop step=5"
    parsed = parse_branch_events([hit_line, miss_line, hold_line, refuse_line,
                                  handoff_line, jump_line, noop_line, "噪声行"])

    canned_pass_turns = [
        {"role": "user", "transcript": TRIGGERS["canned"], "provider": ""},
        {"role": "assistant", "provider": "branch-canned", "gen": "script",
         "template_step": 2, "transcript": CANNED_RESP_TEXT},
    ]
    refuse_pass_turns = [
        {"role": "user", "transcript": TRIGGERS["refuse"], "provider": ""},
        {"role": "assistant", "provider": "branch-refuse", "gen": "script",
         "template_step": 2, "transcript": REFUSE_RESP_TEXT},
    ]
    handoff_pass_turns = [
        {"role": "user", "transcript": TRIGGERS["handoff"], "provider": ""},
        {"role": "assistant", "provider": "branch-notify", "gen": "llm",
         "template_step": 2, "transcript": "好的帮您转接"},
        {"role": "user", "transcript": NEUTRAL_TEXT, "provider": ""},
        {"role": "assistant", "provider": "", "gen": "llm",
         "template_step": 3, "transcript": "好的您说"},
    ]
    jump_pass_turns = [
        {"role": "assistant", "provider": "branch-jump", "gen": "llm",
         "template_step": 5, "transcript": "包裹几时到相关应答"},
        {"role": "assistant", "provider": "", "gen": "llm",
         "template_step": 5, "transcript": "第 5 步应答"},
    ]
    hold_pass_turns = [
        {"role": "user", "transcript": TRIGGERS["hold"], "provider": ""},
        {"role": "assistant", "provider": "branch-canned", "gen": "script",
         "template_step": 2, "transcript": HOLD_RESP_TEXT},
        {"role": "user", "transcript": VERIFY_TEXT, "provider": ""},
        {"role": "assistant", "provider": "", "gen": "llm",
         "template_step": 2, "transcript": "好的那我继续给您说明"},
    ]

    def _canned(**kw) -> dict:
        base = dict(evidence=_ev(), events_by_window={
            "trigger": parse_branch_events([hit_line])}, turns=canned_pass_turns,
            materialized={CANNED_RESP_RAW: "ok"})
        base.update(kw)
        return evaluate_leg(leg="canned", **base)

    def _refuse(**kw) -> dict:
        base = dict(evidence=_ev(), events_by_window={
            "trigger": parse_branch_events([refuse_line])}, turns=refuse_pass_turns,
            call_row={"status": "ended", "assist_status": ""},
            refuse_end_elapsed_s=14.5)
        base.update(kw)
        return evaluate_leg(leg="refuse", **base)

    def _handoff(**kw) -> dict:
        base = dict(evidence=_ev(expected=3), events_by_window={
            "trigger": parse_branch_events([handoff_line])}, turns=handoff_pass_turns,
            call_row={"assist_status": "notified"},
            answered_by_window={"neutral": True})
        base.update(kw)
        return evaluate_leg(leg="handoff", **base)

    def _jump(**kw) -> dict:
        base = dict(evidence=_ev(expected=3), events_by_window={
            "trigger": parse_branch_events([jump_line]),
            "noop": parse_branch_events([noop_line])}, turns=jump_pass_turns)
        base.update(kw)
        return evaluate_leg(leg="jump", **base)

    def _kill(**kw) -> dict:
        base = dict(evidence=_ev(), events_by_window={
            "trigger": parse_branch_events([miss_line])},
            turns=[{"role": "assistant", "provider": "", "gen": "llm",
                    "template_step": 2, "transcript": "按流程答"}])
        base.update(kw)
        return evaluate_leg(leg="kill", **base)

    cases: list[tuple[str, bool, bool]] = [
        ("parse：7 行事件 + 噪声不收", len(parsed) == 7, True),
        ("parse：jump_noop 不被 jump 前缀误吃",
         [e["kind"] for e in parsed][-2:] == ["jump", "jump_noop"], True),
        ("parse：canned hit/miss 带 source=canned",
         parsed[0].get("source") == "canned" and parsed[1].get("kind") == "miss", True),

        # ---- canned 腿 ----
        ("canned 正例（物化 ok+hit+script 轮+逐字）", _canned()["pass"], True),
        ("canned 反例：未物化", _canned(materialized={CANNED_RESP_RAW: "missing"})["pass"],
         False),
        ("canned 反例：罐头未命中（走 LLM）", _canned(
            events_by_window={"trigger": []},
            turns=[{"role": "assistant", "provider": "", "gen": "llm",
                    "template_step": 2, "transcript": "随便答"}])["pass"], False),
        ("canned 反例：文本不一致（LLM 改写）", _canned(
            turns=[canned_pass_turns[0],
                   {"role": "assistant", "provider": "branch-canned", "gen": "script",
                    "template_step": 2, "transcript": CANNED_RESP_TEXT + "啊"}])["pass"],
         False),

        # ---- refuse 腿 ----
        ("refuse 正例（日志+逐字+ENDED+14s 内+最后一条）", _refuse()["pass"], True),
        ("refuse 反例：走了 LLM（gen=llm）", _refuse(
            turns=[refuse_pass_turns[0],
                   {"role": "assistant", "provider": "branch-refuse", "gen": "llm",
                    "template_step": 2, "transcript": REFUSE_RESP_TEXT}])["pass"], False),
        ("refuse 反例：没收线（status=active）", _refuse(call_row={"status": "active"})["pass"],
         False),
        ("refuse 反例：收线时刻超窗（45s）", _refuse(refuse_end_elapsed_s=45.0)["pass"],
         False),
        ("refuse 反例：收线后还有 LLM 轮", _refuse(
            turns=refuse_pass_turns + [{"role": "assistant", "provider": "",
                                        "gen": "llm", "template_step": 2,
                                        "transcript": "还在吗"}])["pass"], False),

        # ---- handoff 腿 ----
        ("handoff 正例（日志+notified+仍 LLM+后续应答）", _handoff()["pass"], True),
        ("handoff 反例：assist 未落库", _handoff(call_row={"assist_status": ""})["pass"],
         False),
        ("handoff 反例：本轮没走 LLM（gen=script）", _handoff(
            turns=[handoff_pass_turns[0],
                   {"role": "assistant", "provider": "branch-notify", "gen": "script",
                    "template_step": 2, "transcript": "好的帮您转接"}]
            + handoff_pass_turns[2:])["pass"], False),
        ("handoff 反例：后续轮哑了", _handoff(
            turns=handoff_pass_turns[:2],
            answered_by_window={"neutral": False})["pass"], False),

        # ---- jump 腿 ----
        ("jump 正例（jump@5+provider 轮@5+noop 无位移）", _jump()["pass"], True),
        ("jump 反例：noop 窗口出现位移宣告（jump）", evaluate_leg(
            leg="jump", evidence=_ev(expected=3),
            events_by_window={"trigger": parse_branch_events([jump_line]),
                              "noop": parse_branch_events([jump_line, noop_line])},
            turns=jump_pass_turns)["pass"], False),
        ("jump 反例：无 jump 日志", evaluate_leg(
            leg="jump", evidence=_ev(expected=3),
            events_by_window={"trigger": [], "noop": parse_branch_events([noop_line])},
            turns=jump_pass_turns)["pass"], False),
        ("jump 反例：provider 轮步号没到 5", evaluate_leg(
            leg="jump", evidence=_ev(expected=3),
            events_by_window={"trigger": parse_branch_events([jump_line]),
                              "noop": parse_branch_events([noop_line])},
            turns=[{"role": "assistant", "provider": "branch-jump", "gen": "llm",
                    "template_step": 2, "transcript": "x"}])["pass"], False),

        # ---- hold 腿 ----
        ("hold 正例（hold@2+应答+验证轮步号=2+零推进）", _hold_inputs(
            advance=False, answered_trigger=True, answered_verify=True,
            turns=hold_pass_turns)["pass"], True),
        ("hold 反例：窗口出现 rule=auto 推进", _hold_inputs(
            advance=True, answered_trigger=True, answered_verify=True,
            turns=hold_pass_turns)["pass"], False),
        ("hold 反例：触发轮哑了", _hold_inputs(
            advance=False, answered_trigger=False, answered_verify=True,
            turns=[hold_pass_turns[0], hold_pass_turns[2], hold_pass_turns[3]])["pass"],
         False),
        ("hold 反例：验证轮步号漂走（推进了）", _hold_inputs(
            advance=False, answered_trigger=True, answered_verify=True,
            turns=[hold_pass_turns[0], hold_pass_turns[1], hold_pass_turns[2],
                   {"role": "assistant", "provider": "", "gen": "llm",
                    "template_step": 3, "transcript": "下一步内容"}])["pass"], False),

        # ---- kill 腿 ----
        ("kill 正例（零动作打点+零罐头 hit+零 branch 轮+无步 5）", _kill()["pass"], True),
        ("kill 反例：仍有 BRANCH_ACTION 打点", _kill(
            events_by_window={"trigger": parse_branch_events([jump_line])})["pass"],
         False),
        ("kill 反例：罐头 hit（录音照播）", _kill(
            events_by_window={"trigger": parse_branch_events([hit_line])})["pass"], False),
        ("kill 反例：行为跳跃（出现步 5 轮）", _kill(
            turns=[{"role": "assistant", "provider": "branch-jump", "gen": "llm",
                    "template_step": 5, "transcript": "x"}])["pass"], False),
        ("kill 反例：branch 轮在场", _kill(
            turns=[{"role": "assistant", "provider": "branch-canned", "gen": "script",
                    "template_step": 2, "transcript": CANNED_RESP_TEXT}])["pass"], False),
        ("kill 假绿闸：日志缺失 → 零打点不成立", _kill(evidence=_ev(log=False))["pass"],
         False),
        ("kill 假绿闸：marks 塌成一个 → 不成立", _kill(evidence=_ev(marks=[100]))["pass"],
         False),
        ("kill 假绿闸：零 turns（没观测到轮）→ 不成立", _kill(turns=[])["pass"], False),

        # ---- 通用面 ----
        ("probe_evidence 诚实路径 ok", _ev()["ok"], True),
        ("probe_evidence 某轮零字节 → ok=False", _ev(marks=[100, 100, 300])["ok"], False),
        ("advance_lines 逮三族推进、不误吃无关行",
         len(advance_lines(["[flow] rule=auto step=3 (call c1)",
                            "[flow] judge(bg)=confirm step=3 (call c1)",
                            "[flow] rule=confirm step=2 (call c1)",
                            "BRANCH_ACTION hold step=2",
                            "[flow] defer-ack (call c1)"])) == 3, True),
        ("evaluate_hold_advance 缺窗口=保守 False",
         evaluate_hold_advance({"trigger": []}), False),
        ("模板契约：第 2 步 5 条分支+动作齐全、第 5 步同位自跳", _template_contract(), True),
        ("触发语铁律：全部 ≥10 字且无逗号", _sentences_contract(), True),
        ("物化目标=剥标记渲染后与逐字比对目标同文", _materialize_contract(), True),
        ("触发语与分支条件命中面（真 match_step_branch 离线复验）",
         _matching_contract(), True),
    ]

    failed = 0
    for label, got, want in cases:
        if bool(got) != bool(want):
            failed += 1
        print(f"  [{'PASS' if bool(got) == bool(want) else 'FAIL'}] {label}"
              f"（got={got} 期望={want}）", flush=True)
    print(f"BRANCH_ACTION_PROBE SELFTEST {'PASS' if not failed else f'FAIL({failed})'}",
          flush=True)
    return 1 if failed else 0


def _load_flow():
    sys.path.insert(0, str(_ROOT / "apps" / "agent"))
    sys.path.insert(0, str(_ROOT / "packages" / "core"))
    from agent_runtime import flow as flow_mod
    return flow_mod


def _template_contract() -> bool:
    """模板分支结构与引擎解析契约（离线用真 parse_step_ref/parse_branch_action）。"""
    try:
        flow_mod = _load_flow()
    except Exception:  # noqa: BLE001 - 无栈环境缺依赖时自检降级
        return True
    parts2 = flow_mod.parse_step_ref(str(PROBE_STEPS[1]["ref"]))
    parts5 = flow_mod.parse_step_ref(str(PROBE_STEPS[4]["ref"]))
    if len(parts2.branches) != 5 or len(parts5.branches) != 1:
        return False
    acts = [flow_mod.parse_branch_action(r)[0] for _c, r in parts2.branches]
    if acts != ["refuse", "handoff", "jump", "hold", ""]:
        return False
    if flow_mod.parse_branch_action(parts2.branches[2][1])[1] != 5:
        return False
    return flow_mod.parse_branch_action(parts5.branches[0][1])[:2] == ("jump", 5)


def _sentences_contract() -> bool:
    texts = [WARMUP_TEXT, *TRIGGERS.values(), NOOP_TEXT, NEUTRAL_TEXT, VERIFY_TEXT]
    return all(len(t) >= 10 and "," not in t and "，" not in t for t in texts)


def _materialize_contract() -> bool:
    """物化目标（resp 原文）与逐字比对目标（剥标记渲染后）经真解析器闭合。"""
    try:
        flow_mod = _load_flow()
    except Exception:  # noqa: BLE001
        return True
    for raw, want in ((CANNED_RESP_RAW, CANNED_RESP_TEXT),
                      (HOLD_RESP_RAW, HOLD_RESP_TEXT),
                      ("【收线】" + REFUSE_RESP_TEXT, REFUSE_RESP_TEXT)):
        got = flow_mod.render_template_text(flow_mod.parse_branch_action(raw)[2], {})
        if got != want:
            return False
    return True


def _matching_contract() -> bool:
    """每条触发语在真 match_step_branch 下唯一命中自己的分支（verdict 家族+2-gram）。"""
    try:
        flow_mod = _load_flow()
    except Exception:  # noqa: BLE001
        return True
    parts = flow_mod.parse_step_ref(str(PROBE_STEPS[1]["ref"]))
    expect = {
        "canned": ("查运单号是多少", ""),
        "refuse": ("说打错电话了", "refuse"),
        "handoff": ("要找真人客服", "handoff"),
        "jump": ("说包裹几时送到", "jump"),
        "hold": ("说等等先", "hold"),
    }
    for leg, (want_cond, want_act) in expect.items():
        text = TRIGGERS[leg]
        verdict = flow_mod.decide_advance(text, facts=None)
        m = flow_mod.match_step_branch(parts, text, verdict)
        if m is None:
            return False
        cond, resp = m
        act = flow_mod.parse_branch_action(resp)[0]
        if cond != want_cond or act != want_act:
            return False
    # 近失转写容忍：每腿给 2 条变体仍须命中同分支。
    tol = {
        "canned": ["我的运单号码到底是多少", "我运单号码到底是多少呢"],
        "refuse": ["你是不是打错电话了我不是这个人啊", "你是不是打错电话了偶不是这个人"],
        "handoff": ["我要找真人客服帮我来处理这个事情", "我要找真人客服帮我处理这个事"],
        "jump": ["这个包裹几时才能送到我手上", "这个包裹几时才能送到我的手上啊"],
        "hold": ["这个事情你等等先我还想再了解了解吧", "这个事情你等等先我还想再了解一下"],
    }
    for leg, variants in tol.items():
        want_cond = expect[leg][0]
        for t in variants:
            verdict = flow_mod.decide_advance(t, facts=None)
            m = flow_mod.match_step_branch(parts, t, verdict)
            if m is None or m[0] != want_cond:
                return False
    return True


# ---------------------------------------------------------------------------
async def main() -> int:
    parser = argparse.ArgumentParser(description="分支动作+分支罐头快路实弹验收探针")
    parser.add_argument("--leg", default="",
                        choices=["", "canned", "refuse", "handoff", "jump", "hold", "kill"],
                        help="一腿一通真通话；kill 腿须先以 BOK_BRANCH_ACTION=0 重启 agent worker")
    parser.add_argument("--lang", default="zh", choices=["zh", "cantonese", "en"])
    parser.add_argument("--persona-voice", default="",
                        help="空=按语言默认音色（erc.SCENARIOS）")
    parser.add_argument("--keep-template", action="store_true", help="保留探针模板")
    parser.add_argument("--selftest", action="store_true", help="无栈纯函数自检后退出")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if not args.leg:
        parser.error("--leg is required (unless --selftest)")

    voice = args.persona_voice or erc.SCENARIOS[args.lang]["persona_voice"]
    res = await run_leg(leg=args.leg, lang=args.lang, voice=voice,
                        keep_template=args.keep_template)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{int(time.time())}-{res['leg']}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[branch-action] JSON 报告 → {out}", flush=True)
    checks = res["verdict"]["checks"]
    print("BRANCH_ACTION_PROBE leg=" + res["leg"] + " " +
          " ".join(f"{k}={'1' if v else '0'}" for k, v in checks.items()) +
          f" → {'PASS' if res['verdict']['pass'] else 'FAIL'}", flush=True)
    if args.leg == "kill":
        print("（kill-switch 腿：须以 BOK_BRANCH_ACTION=0 重启 worker；跑完请用不带该 env "
              "的配方起回默认档并确认 :8081/worker=agent_name bok-voice）", flush=True)
    return 0 if res["verdict"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
