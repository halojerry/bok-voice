"""LLM 漏网轮挖掘 + 快路覆盖率驾驶舱(2026-09-20 L-①,防 main.py 膨胀)。

A 线每轮回复走一条「快路漏斗」:开场白/直念步/QA 罐头快路/话术图动作/分支罐头
(命中即播录音或直念脚本,零 LLM)全部未命中才落本地 LLM(4B,慢且措辞不稳定)。
本模块把 turns 分析账本翻成运营可读的两块面:
- coverage:A 线回复轮里多少是脚本/罐头(快路)直接播的、多少走了 LLM 现场组织;
- gaps:走了 LLM 的轮,取其前一条客户原话,按归一化文本聚合出反复出现的问法,
  供运营人工确认后采集为问答词条(POST /api/stats/llm-gaps/adopt)。

只读驾驶舱 + 人工确认采集——无 LLM、无自动写入(同义聚类已有 POST /api/qa/cluster,
本模块不复用)。纯函数面(归一/排除判定/覆盖率/聚合)与查询面
(build_llm_gap_report)分离;查询面只走 BusinessRepository 公开方法
(list_calls/list_objects/get_turns),零手写 SQL——test_db_portability 方言
门禁天然无涉,也不新增表/列/迁移。

## gen/provider 取值面(2026-09-20 grep apps/agent/agent_runtime/agent.py 全部写入点实证)

gen 是唯一权威快路判定源(_turn_origin consume-once 账本,:3015 默认 "llm"、
:3065-3068 消费并重置):
- "llm"          :3015/:3067 —— LLM 现场组织。**同轮可能带 provider**:
                   "graph-jump"(:4609)/"branch-jump"(:4222,「本轮 LLM 按新步答」)/
                   "branch-notify"(:4198,notify_human 不暂停照常兜话)/
                   "stall-{N}"(:4385,超时降级重试)——这些 provider 是跳步/通知/
                   看门狗**副作用标记,回复文本仍係 LLM 生成**,不得按 provider
                   判快路(任务书提示的「provider 属 graph-*/branch-* 即快路」
                   会把这三类错算成罐头,以代码实证为准推翻);
- "script"       脚本直念:开场白(:5106)/心跳(:4916)/收线(:4906)/收线告别/
                   直念步(:4456 provider=flow-say)/WA 复述确认(:2197/:2250)/
                   watchdog-ack(:2295)/late-answer(:2642)/starve-ack(:3790/:3845)/
                   defer-ack(:4412)/storm-ack(:5070)/branch-refuse 直念收线
                   (:4115 provider=branch-refuse)/branch-canned 分支录音
                   (:4510 provider=branch-canned);
- "qa_fastpath"  :4441(_qa_canned_say 内统一置)——QA 罐头快路
                   (:4768 provider="qa-fastpath")与话术图 play_qa
                   (:4642 provider="graph-play")共用同一条罐头出口。
- "filler"       :2819 垫话 out-of-band 音轨账本(speaker=agent_ai)——不是回复轮;
- "interrupted"  :5027 打断残文本补记——不是完整回复轮;
客户侧 gen("paused" :3749 等)与 B 线(line="b",interpret.py :791 provider="interpret")
不在 A 线 AI 轮口径内,天然被 line/speaker 过滤掉。

## 口径(钉死,tests/test_gap_mining.py 钉住)

- 只统计 A 线(line=="a")、AI 轮(role=="assistant" 且 speaker=="agent_ai");
  分析账本三列 2026-09-10 起才有,旧行 speaker 为空不计(报告偏保守,不误归因);
- **回复轮**=AI 轮再排除 gen ∈ NON_REPLY_GENS(垫话/打断账本残行)——垫话每通
  最多 BOK_FILLER_MAX(3-6)行,若进分母会把覆盖率实质拉低 10-30%;
  coverage.turns=回复轮数,fastpath(gen ∈ FASTPATH_GENS)+llm(gen=="llm")
  为其两个子集;gen 为空/未知(旧数据、未来新值)留在分母、不归两边(by_gen
  可见),让比率偏保守而不误归因;
- gaps=gen=="llm" 的回复轮,取同通内**前一条**客户轮(role=="user" 且
  speaker=="customer")transcript 作候选;一条客户轮只配一条 LLM 回复
  (消费后即清,防一路客户话配多条回复重复计数);
- 候选排除:归一后 <3 字、纯数字/数字主导、应承语族、测试对象通话
  (clean-testdata 前缀族 is_test_object_name,连「无对象通话」一并滤——
  照 iter_call_conversations 的既有先例,不另造过滤);
- 归一化复用 bok_voice_core.qa_text.normalize_question(与运行时 QA 快路匹配
  同源——报告聚出来的问法运行时才对得上),聚合键=(归一文本, template_id),
  template_id 为空的通话合并成一组(calls 累计);
- 结果按 count 降序,门槛 count >= min_calls,截 limit。
"""

from __future__ import annotations

import re
import time
from collections import Counter

from bok_voice_core.qa_text import normalize_question
from bok_voice_core.testdata import is_test_object_name

# E7 离线润色接线（2026-09-21）：L-① 漏网轮挖掘的输入预处理——脏转写是聚类
# 不纯的主因之一。kill-switch ``BOK_POLISH_OFFLINE`` 默认关（理由见
# ``polish_wiring`` 模块 docstring）；只作用于**派生报告行**（customer_text /
# sample_answer），turns 账本原件不动。
from bok_voice_core.polish_wiring import polish_offline_text

# ---- gen 取值面(模块级常量,来源行号见模块 docstring;改 agent 写入点须同步) ----

GEN_LLM = "llm"
GEN_SCRIPT = "script"
GEN_QA_FASTPATH = "qa_fastpath"
GEN_FILLER = "filler"  # 垫话 out-of-band 账本,非回复(agent.py:2819)
GEN_INTERRUPTED = "interrupted"  # 打断残文本补记,非完整回复(agent.py:5027)

# 快路(回复=脚本直念/罐头录音,零 LLM);graph-play 复用 qa_fastpath(agent.py:4642)。
FASTPATH_GENS = frozenset({GEN_SCRIPT, GEN_QA_FASTPATH})
LLM_GENS = frozenset({GEN_LLM})
# AI 侧非回复账本行:不进驾驶舱分母(见模块 docstring「回复轮」段)。
NON_REPLY_GENS = frozenset({GEN_FILLER, GEN_INTERRUPTED})

# ---- 候选文本排除判定(纯函数) ----

# 归一后最小长度:「嗯/好/係」类单双字应承一并拦在长度门。
MIN_NORM_LEN = 3

# 应承/语气语族(归一后全串命中才排除;≥3 字的「好好好/okay/yes」等长度门拦不住的补拦)。
_ACK_TEXTS = frozenset(
    {
        "ok", "okay", "yes", "yeah", "no", "haha", "lol",
        "嗯", "嗯嗯", "嗯嗯嗯", "哦", "哦哦", "噢", "喔", "啊", "哎", "诶",
        "好", "好好", "好好好", "好的", "好嘅", "好呀", "好啦", "好嘅好嘅",
        "係", "係係", "係係係", "系", "是", "是的", "对", "对对", "对对对",
        "对的", "行", "好的好的", "是的是的", "係咁", "好嘅唔該", "唔該",
    }
)

# ≥4 位连续数字 run(ASCII 或中文数字归一后)——号码/单号轮运行时被四道闸旁路,
# 做成词条是死重(与 qa_text.auto_apply_verdict 同判据,数字归一只为判定)。
_DIGIT_RUN_RE = re.compile(r"\d{4,}")
_CJK_DIGIT_TRANS = str.maketrans(
    {"零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
     "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
)

# 答案截断(与 mine_qa_pairs 的 answers[:120] 同宽)。
ANSWER_MAX_CHARS = 120


def is_fastpath_gen(gen: str) -> bool:
    """快路判定:只认 gen(权威源);provider 不参与(graph-jump/branch-jump/
    branch-notify/stall-N 与 gen=llm 同轮,见模块 docstring)。"""
    return str(gen or "") in FASTPATH_GENS


def is_llm_gen(gen: str) -> bool:
    return str(gen or "") in LLM_GENS


def is_reply_gen(gen: str) -> bool:
    """回复轮=非垫话/打断账本行。gen 空串(旧数据)按回复计(保守留分母)。"""
    return str(gen or "") not in NON_REPLY_GENS


def candidate_norm(text: str) -> str:
    """客户原话 → 归一化候选键;不过闸返回空串(排除)。

    归一=normalize_question(全半角统一+剥标点空白,与运行时快路匹配同源)。
    闸依次:长度 <MIN_NORM_LEN / 应承语族 / ≥4 位数字 run(含中文数字)/
    数字主导(数字字符占归一串一半以上,「单号係12345678」类)。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    norm = normalize_question(raw)
    if len(norm) < MIN_NORM_LEN:
        return ""
    if norm in _ACK_TEXTS:
        return ""
    translated = norm.translate(_CJK_DIGIT_TRANS)
    if _DIGIT_RUN_RE.search(translated):
        return ""
    digit_chars = sum(ch.isdigit() for ch in norm) + sum(norm.count(c) for c in "零〇一二两三四五六七八九")
    if digit_chars * 2 >= len(norm):
        return ""
    return norm


# ---- E7 离线润色接线(单点,2026-09-21) ----


def _turn_text(turn) -> str:
    """turns 账本一行 → 派生报告用的文本(客户轮=候选问法 / AI 轮=样例答案)。

    **E7 润色单点**:这里是本模块唯一取 transcript 的入口,润色只落在这份
    **派生**文本上(kill-switch 默认关/润色异常时逐字原样——见
    ``polish_wiring.polish_offline_text`` 的两层 fail-soft)。turns 账本原件
    与 ``repo.get_turns`` 返回的行都不被改写;采纳后落库的是**新** qa_entry
    (人工确认面),不是原话账本。
    """
    return polish_offline_text(str(getattr(turn, "transcript", "") or "").strip())


# ---- 覆盖率(纯函数) ----


def compute_coverage(rows: list[dict]) -> dict:
    """AI 回复轮行 [{"gen","provider"}] → coverage 段。

    分母=回复轮(gen 不在 NON_REPLY_GENS);fastpath_ratio 分母为 0 → 0.0。
    by_gen/by_provider 只统计回复轮(垫话/打断行不是回复,不进细分)。
    """
    replies = [r for r in rows or [] if is_reply_gen(str(r.get("gen") or ""))]
    total = len(replies)
    fast = sum(1 for r in replies if is_fastpath_gen(str(r.get("gen") or "")))
    llm = sum(1 for r in replies if is_llm_gen(str(r.get("gen") or "")))
    by_gen = Counter(str(r.get("gen") or "") for r in replies)
    by_provider = Counter(str(r.get("provider") or "") for r in replies if str(r.get("provider") or ""))
    return {
        "turns": total,
        "fastpath": fast,
        "llm": llm,
        "fastpath_ratio": round(fast / total, 4) if total else 0.0,
        "by_gen": dict(by_gen),
        "by_provider": dict(by_provider),
    }


# ---- 漏网轮聚合(纯函数) ----


def _top(counter: Counter) -> tuple[str, int]:
    """众数并列取字典序最小者(确定性,不依赖插入序)。空 Counter 返回 ("", 0)。"""
    if not counter:
        return "", 0
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def aggregate_gap_groups(groups: dict[tuple[str, str], dict]) -> list[dict]:
    """聚合中间组 → gaps 行(排序在调用方;这里只做众数与形状收窄)。"""
    out: list[dict] = []
    for g in groups.values():
        raw_text, _n = _top(g["raws"])
        answer, _v = _top(g["answers"])
        step_text, _s = _top(g["steps"])
        lang, _l = _top(g["langs"])
        out.append(
            {
                "norm": g["norm"],
                "customer_text": raw_text,
                "count": g["count"],
                "calls": len(g["call_ids"]),
                "template_id": g["template_id"],
                "step": int(step_text) if step_text else 0,
                "lang": lang or "zh",
                "sample_answer": answer,
                "sample_call_id": g["answer_calls"].get(answer, ""),
            }
        )
    return out


def build_llm_gap_report(
    repo,
    *,
    account_id: str,
    template_id: str = "",
    min_calls: int = 3,
    limit: int = 30,
    exclude_test_objects: bool = True,
) -> dict:
    """查询主入口:仓库公开方法取数 → coverage + gaps。

    min_calls 门槛按「出现轮数」(count)判,照任务契约;limit 截断在排序后。
    测试对象过滤与 iter_call_conversations 同款:测试前缀族与无对象通话一并滤。
    """
    calls = repo.list_calls(account_id)
    if template_id:
        calls = [c for c in calls if str(c.get("template_id") or "") == template_id]
    obj_names: dict[str, str | None] = {}
    try:
        obj_names = {
            str(o.get("id") or ""): o.get("display_name")
            for o in (repo.list_objects(account_id) or [])
        }
    except Exception:  # noqa: BLE001 - 对象面不可读只损测试过滤,不炸报表
        obj_names = {}

    ai_rows: list[dict] = []
    groups: dict[tuple[str, str], dict] = {}
    for call in calls:
        call_id = str(call.get("id") or "")
        tpl = str(call.get("template_id") or "")
        if exclude_test_objects:
            name = obj_names.get(str(call.get("object_id") or ""))
            # obj 名缺失(None)=无对象通话(outerjoin 未命中同款语义),与测试前缀族一并滤
            if name is None or is_test_object_name(name):
                continue
        try:
            turns = repo.get_turns(call_id)
        except Exception:  # noqa: BLE001 - 单通取数失败不炸整表
            continue
        # 同通内按 created_at 排序(turns 无 seq 列,与 iter_call_conversations 同假设)
        ordered = sorted(turns, key=lambda t: str(getattr(t, "created_at", "") or ""))
        prev_customer = None
        for t in ordered:
            if str(getattr(t, "line", "") or "") != "a":
                continue
            role = str(getattr(t, "role", "") or "")
            speaker = str(getattr(t, "speaker", "") or "")
            if role == "user" and speaker == "customer":
                prev_customer = t
                continue
            if role != "assistant" or speaker != "agent_ai":
                continue
            gen = str(getattr(t, "gen", "") or "")
            ai_rows.append({"gen": gen, "provider": str(getattr(t, "provider", "") or "")})
            if not is_reply_gen(gen):
                continue  # 垫话/打断账本行:不配对、不进覆盖分母
            if is_llm_gen(gen) and prev_customer is not None:
                # E7：候选问法与样例答案都走 _turn_text（润色单点），归一/聚合/
                # 展示/采纳 payload 全吃同一份文本——键与显示不劈叉。
                customer_text = _turn_text(prev_customer)
                norm = candidate_norm(customer_text)
                if norm:
                    g = groups.setdefault(
                        (norm, tpl),
                        {
                            "norm": norm,
                            "template_id": tpl,
                            "count": 0,
                            "call_ids": set(),
                            "raws": Counter(),
                            "answers": Counter(),
                            "answer_calls": {},
                            "steps": Counter(),
                            "langs": Counter(),
                        },
                    )
                    g["count"] += 1
                    g["call_ids"].add(call_id)
                    g["raws"][customer_text] += 1
                    answer = _turn_text(t)[:ANSWER_MAX_CHARS]
                    if answer:
                        g["answers"][answer] += 1
                        g["answer_calls"].setdefault(answer, call_id)
                    step = int(getattr(t, "template_step", 0) or 0)
                    if step > 0:
                        g["steps"][step] += 1
                    # 通话语言随轮落库——词条要带对语言,运行时快路按 lang 过滤
                    # (lang 不符词条永不命中),粤语/英语通话的候选不能默认 zh。
                    if str(getattr(t, "language", "") or ""):
                        g["langs"][str(getattr(t, "language", "") or "")] += 1
                prev_customer = None  # 一条客户话只配一条 LLM 回复
            elif not is_llm_gen(gen):
                prev_customer = None  # 快路回复已接住该客户轮,不留给下一条

    coverage = compute_coverage(ai_rows)
    rows = [r for r in aggregate_gap_groups(groups) if r["count"] >= max(1, int(min_calls))]
    rows.sort(key=lambda r: (-r["count"], r["norm"]))
    rows = rows[: max(1, min(int(limit), 200))]
    for r in rows:
        r.pop("norm", None)  # 内部聚合键不出仓
    return {"coverage": coverage, "gaps": rows, "generated_at": int(time.time())}


# ---- 采集幂等查找(纯查询;入库与审计在 main.adopt 端点,与 POST /api/qa-entries 同链) ----


def find_existing_qa_entry(repo, account_id: str, question_text: str, lang: str) -> dict | None:
    """同账号同 lang 归一同问法 → 返回既有词条行(None=无)。

    owner_scope=None 全账号查(管理口径,与 qa_cluster.apply 的 existing_norms
    同款)——幂等去重看内容归属账号,不看人。
    """
    norm = normalize_question(str(question_text or ""))
    if not norm:
        return None
    for row in repo.list_qa_entries(account_id, owner_scope=None):
        if (
            str(row.get("lang") or "") == str(lang or "")
            and normalize_question(str(row.get("question_text") or "")) == norm
        ):
            return row
    return None
