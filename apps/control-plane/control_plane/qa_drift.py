"""问答词条漂移提案(L-③,2026-09-20):「改答案 / 删词条」以通知形式送达,人工确认后自动填入。

L-①(gap_mining)把「LLM 漏网轮」采集成**新**词条;L-②(gap_proposals)把漏网轮升级成话术
分支/意图词;L-③ 反过来体检**已经在库里的快答词条**——哪条播了客户不认、哪条压根没人问,
把结论做成「通知」摆给运营,人工确认后才动库(POST /api/stats/qa-drift/adopt)。

## 三条判据(全部来自 turns 分析账本,零 LLM 零猜测)

同通内按 created_at 排序,逐轮配对「客户问 → AI 答」(与 gap_mining 同假设):
- `occurrences[n]`:归一问法 n 在窗口内被问过几次;
- `fired[n]`:紧接其后的 AI 轮 `gen=="qa_fastpath"`(罐头出口统一置,见 gap_mining
  模块 docstring 的 gen 取值面)——即那条快答真的被播出声了;
- `repeats[n]`:n 被问 → 播了快答 → 客户**又问同一条** 的次数(快答没答到);
- `llm_answer[n]`:紧接其后的 AI 轮走了 LLM 时它实际说的那句(改答案时的默认值)。

## 提案(每条词条至多一条,互斥,按序取首个命中)

| kind | reason | 触发 | 结论 |
|---|---|---|---|
| retire | never_asked | occurrences==0 | 窗口内没人这么问过 |
| retire | digits_bypass | 问法含 ≥4 位数字 run | 带数字的轮运行时被四道闸旁路,词条永远命中不了 |
| reanswer | repeat_after_play | repeats>0 | 播了快答客户又问一遍 |
| reanswer | never_fired | occurrences≥3 且 fired==0 | 这句话反复出现,快答一次都没用上 |

`occurrences>0 且 fired==occurrences` → 健康词条,不出提案(不打扰运营)。

## 两条保守闸(2026-09-20 真栈 150 通实弹后补,防误报)

- **建议答案必须同语言**:llm_answer 记的是「当时 AI 自己答的那句」,可能来自别的语言
  的通话(实弹:中文词条被建议成粤语答案,罐头化后会让词条播出错语言的录音)。建议
  答案与词条 lang 不同一律**不采用**(留空交运营自己写)——宁可少一条建议,不出错语言。
- **never_fired 要求 occurrences≥NEVER_FIRED_MIN_OCCURRENCES(3)**:只出现过一两次就
  断言「快答没用上」是噪声(实弹窗口里 occ=1 的两条正是如此);反复出现才算证据。
- 建议答案不可用(跨语言/为空)时,**repeat_after_play 照常出提案**(客户复问本身就是
  铁证),suggested_answer 留空、human detail 提示自己写;never_fired 则退化为
  「先补录音」的提示,同样照常出(它本来就不是「改答案」,而是「这条没生效」)。

## 口径与已知边界(如实标注,不假装判据能分得更细)

- 归一用 `qa_text.normalize_question`(与运行时匹配同源),**不是** `candidate_norm`
  ——后者滤应承/数字是「采新词条」的场景口径,体检要看见全部被问过的话;
- 窗口=该账号最近 max_calls 通**非测试对象**通话(默认 200),报告出 `window_calls`
  让运营知道结论基于多少通——「没人问过」是这个口径下的结论,不是「历史上从没问过」;
- `never_fired` 的**两个可能成因判据分不清**:罐头缺料(没录音 → 快路闸过不去)与
  被更高优先级词条/同义簇代表抢了出场(Phase 3.1/3.2 的 priority/轮换语义)。
  报告不猜,两个可能都写进 detail 让运营判断;`hits` 列(entry 自带计数)只作旁证;
- 只体检 `enabled=True` 的词条(停用词条是运营的明确意图,不去打扰);
- 零手写 SQL(只走 BusinessRepository 公开方法 list_calls/list_objects/get_turns/
  list_qa_entries),不新增表/列/迁移;写入走**既有 qa 写链**(update_qa_entry /
  delete_qa_entry + 审计),本模块只产提案。
"""

from __future__ import annotations

import re
import time
from collections import Counter

from bok_voice_core.qa_text import normalize_question
from bok_voice_core.testdata import is_test_object_name

from . import gap_mining

# ---- kind 与 reason(出仓值,web 逐字消费) ----

KIND_REANSWER = "reanswer"  # 改答案
KIND_RETIRE = "retire"  # 删词条(停用)
KINDS = (KIND_REANSWER, KIND_RETIRE)

RS_NEVER_ASKED = "never_asked"
RS_DIGITS_BYPASS = "digits_bypass"
RS_REPEAT_AFTER_PLAY = "repeat_after_play"
RS_NEVER_FIRED = "never_fired"

# 窗口默认最近多少通(报告出 window_calls,结论口径随之透明)。
DEFAULT_MAX_CALLS = 200

# adopt 单批上限(人工确认动作,批量是给「全采纳」留的口子,不是导入通道)。
ADOPT_MAX_ITEMS = 20

# never_fired 的最少出现次数:只出现一两次就断言「快答没用上」是噪声(真栈实弹
# 窗口里 occ=1 的两条正是不足据),反复出现才算证据。
NEVER_FIRED_MIN_OCCURRENCES = 3

# 带数字的轮运行时被四道闸旁路(flow.py 数字闸/QA 快路 digits 闸),词条是死重
# ——与 qa_text.auto_apply_verdict 的 digits 判据同口径(中文数字一并归一后判)。
_DIGIT_RUN_RE = re.compile(r"\d{4,}")
_CJK_DIGIT_TRANS = str.maketrans(
    {"零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
     "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
)


def has_digit_run(text: str) -> bool:
    """≥4 位连续数字 run(ASCII 或中文数字归一后)——运行时数字轮不走快路。"""
    t = str(text or "")
    return bool(_DIGIT_RUN_RE.search(t) or _DIGIT_RUN_RE.search(t.translate(_CJK_DIGIT_TRANS)))


def qa_norm(entry: dict) -> str:
    """词条问法的归一键(与运行时 qa_gate 匹配同源)。"""
    return normalize_question(str((entry or {}).get("question_text") or ""))


def proposal_key(kind: str, qa_id: str) -> str:
    """提案去重键(动作直接作用于既有行,身份=行 id,无需内容哈希)。"""
    return f"{kind}|{qa_id}"


def drift_counters() -> dict:
    """逐问法累加器(纯数据,便于测试直接构造)。"""
    return {
        "occurrences": Counter(),
        "fired": Counter(),
        "repeats": Counter(),
        "llm_answer": {},
        "llm_answer_lang": {},
    }


def build_drift_proposal(
    entry: dict, counters: dict, *, window_calls: int
) -> dict | None:
    """单条词条 → 提案 dict(None=健康,不出提案)。纯函数,零 I/O。

    互斥按序: never_asked → digits_bypass → repeat_after_play → never_fired。
    两条保守闸(见模块 docstring):建议答案必须与词条同语言;never_fired 需
    occurrences≥NEVER_FIRED_MIN_OCCURRENCES。
    """
    qa_id = str((entry or {}).get("id") or "")
    norm = qa_norm(entry)
    if not qa_id or not norm:
        return None
    occ = int(counters["occurrences"].get(norm, 0))
    fired = int(counters["fired"].get(norm, 0))
    reps = int(counters["repeats"].get(norm, 0))
    entry_lang = str((entry or {}).get("lang") or "zh")
    llm_ans = _same_lang_answer(counters, norm, entry_lang)
    current = str((entry or {}).get("answer_text") or "")

    kind = reason = ""
    if occ == 0:
        kind, reason = KIND_RETIRE, RS_NEVER_ASKED
    elif has_digit_run(norm):
        kind, reason = KIND_RETIRE, RS_DIGITS_BYPASS
    elif reps > 0:
        kind, reason = KIND_REANSWER, RS_REPEAT_AFTER_PLAY
    elif fired == 0 and occ >= NEVER_FIRED_MIN_OCCURRENCES:
        kind, reason = KIND_REANSWER, RS_NEVER_FIRED
    else:
        return None  # 出现过且每次都播了快答(或次数不足以断言)=不打扰

    suggested = llm_ans.strip() if kind == KIND_REANSWER else ""
    if suggested and suggested == current.strip():
        # 建议与现状逐字相同=没有可填的内容(LLM 那句就是从这条答案来的),
        # 出提案只会让运营白点一次 → 当健康处理。
        # 建议为空(跨语言/当时没答)不算健康:repeat_after_play 是铁证,
        # never_fired 仍值一条「先补录音」的提示——留空交运营自己写。
        return None

    headline, detail = _human_note(reason, occ=occ, fired=fired, reps=reps,
                                   window_calls=window_calls, has_suggestion=bool(suggested))
    return {
        "key": proposal_key(kind, qa_id),
        "kind": kind,
        "reason": reason,
        "qa_id": qa_id,
        "question_text": str((entry or {}).get("question_text") or ""),
        "current_answer": current,
        "suggested_answer": suggested,
        "lang": entry_lang,
        "scope": str((entry or {}).get("scope") or "global"),
        "occurrences": occ,
        "fired": fired,
        "repeats": reps,
        "hits": int((entry or {}).get("hits") or 0),
        "headline": headline,
        "detail": detail,
    }


def _same_lang_answer(counters: dict, norm: str, entry_lang: str) -> str:
    """取该问法「当时 AI 自己答的那句」,但**只在语言与词条一致时**采用。

    跨语言建议是错料:中文词条被建议成粤语答案,罐头化后会播出错语言的录音
    (2026-09-20 真栈 150 通实弹:两条中文词条的建议答案都是粤语)。语言取轮上
    language 列(与 gap_mining 取 gap lang 同源);缺失按 zh(仓内缺省)。
    """
    text = str(counters.get("llm_answer", {}).get(norm) or "")
    if not text:
        return ""
    lang = str(counters.get("llm_answer_lang", {}).get(norm) or "zh")
    if lang != str(entry_lang or "zh"):
        return ""
    return text


def _human_note(
    reason: str, *, occ: int, fired: int, reps: int, window_calls: int, has_suggestion: bool
) -> tuple[str, str]:
    """reason → (结论一句话, 建议动作)。人话面向非技术运营,零术语。"""
    if reason == RS_NEVER_ASKED:
        return (
            f"最近 {window_calls} 通电话里，没有客户这么问过。",
            "这条快速回答一直没派上用场，可以删掉，免得以后误播。",
        )
    if reason == RS_DIGITS_BYPASS:
        return (
            "这条问题里带号码/数字，电话里这类问题走不了快速回答（系统会先当号码处理）。",
            "这条快速回答永远不会生效，建议删掉，改用话术里的其他说法来接。",
        )
    if reason == RS_REPEAT_AFTER_PLAY:
        head = f"这段话客户问过 {occ} 次，其中 {reps} 次是听完快速回答又问了一遍。"
        if has_suggestion:
            return (head, "说明那句回答没答到点上。建议改成下面这句（当时 AI 自己答的），或者自己重写。")
        return (head, "说明那句回答没答到点上。请自己写一句更贴题的，确认后替换。")
    if reason == RS_NEVER_FIRED:
        head = f"这段话客户问过 {occ} 次，快速回答一次都没用上。"
        if has_suggestion:
            return (
                head,
                "两种可能：①这句还没录好音（补录音就能用）；②被别的同义回答抢了出场。"
                "先补录音；补录音之后还是没用上，再考虑改成下面这句（当时 AI 自己答的）。",
            )
        return (
            head,
            "多半是这句还没录好音。先补录音；补录音之后还是没用上，再回来改答案。",
        )
    return ("这条快速回答可能有问题。", "建议人工确认后再改。")


# ---- 查询面(仓库公开方法取数,零手写 SQL) ----


def _walk_turns(turns: list, counters: dict) -> None:
    """同通内逐轮配对,把 occurrences/fired/repeats/llm_answer 累加进 counters。

    状态机:客户轮记 prev;AI 回复轮结算 prev。repeats 需要「问 → 播快答 → 又问
    同一条」三连,故另记 last_norm/last_reply_canned。
    """
    ordered = sorted(turns, key=lambda t: str(getattr(t, "created_at", "") or ""))
    prev = ""
    last_norm = ""
    last_reply_canned = False
    for t in ordered:
        if str(getattr(t, "line", "") or "") != "a":
            continue
        role = str(getattr(t, "role", "") or "")
        speaker = str(getattr(t, "speaker", "") or "")
        if role == "user" and speaker == "customer":
            n = normalize_question(str(getattr(t, "transcript", "") or ""))
            if not n:
                continue
            counters["occurrences"][n] += 1
            if n == last_norm and last_reply_canned:
                counters["repeats"][n] += 1
            last_norm = n
            prev = n
            last_reply_canned = False
            continue
        if role != "assistant" or speaker != "agent_ai":
            continue
        gen = str(getattr(t, "gen", "") or "")
        if not gap_mining.is_reply_gen(gen):
            # 垫话/打断账本行不是「对这条的回复」:不记 fired、不消费待配对客户轮
            # (垫话恰好在客户问句与真快答回复之间落地,吃掉配对会把真快答记成 miss)。
            continue
        if not prev:
            continue
        if gen == gap_mining.GEN_QA_FASTPATH:
            counters["fired"][prev] += 1
            last_reply_canned = True
        elif gen == gap_mining.GEN_LLM:
            text = str(getattr(t, "transcript", "") or "").strip()
            if text:
                counters["llm_answer"][prev] = text[:gap_mining.ANSWER_MAX_CHARS]
                # 记语言:建议答案要跟词条同语言才可用(见 _same_lang_answer)。
                counters["llm_answer_lang"][prev] = str(getattr(t, "language", "") or "zh")
            last_reply_canned = False
        else:
            # 脚本直念/别族快路(开场白/心跳/直念步/意图播放)不是这条快答的出口,
            # 既不记 fired 也不打断 repeats 判定(它不算「答了这条」)。
            last_reply_canned = False
        prev = ""


def build_qa_drift_report(
    repo,
    *,
    account_id: str,
    max_calls: int = DEFAULT_MAX_CALLS,
    limit: int = 30,
    exclude_test_objects: bool = True,
) -> dict:
    """体检主入口:窗口内 turns → 逐词条提案。

    「通知」的形状:每条提案自带 headline(发现了什么)+ detail(建议做什么),
    运营看得懂再决定;`counts` 给出两类各多少条(驾驶舱徽标/铃铛数字用)。
    """
    calls = repo.list_calls(account_id)
    obj_names: dict[str, str | None] = {}
    try:
        obj_names = {
            str(o.get("id") or ""): o.get("display_name")
            for o in (repo.list_objects(account_id) or [])
        }
    except Exception:  # noqa: BLE001 - 对象面不可读只损测试过滤,不炸报表
        obj_names = {}

    window: list[dict] = []
    for call in calls:
        if exclude_test_objects:
            name = obj_names.get(str(call.get("object_id") or ""))
            # obj 名缺失(None)=无对象通话,与测试前缀族一并滤(同 gap_mining 口径,
            # 两张报表的分析面必须一致,否则同一通在一张里算数另一张不算)。
            if name is None or is_test_object_name(name):
                continue
        window.append(call)
    # 窗口=最近 max_calls 通。排序键必须带 id 兜底:created_at 缺失/并列时(内存仓
    # 恒 None、SQL 侧同刻秒级并列)若只按 created_at,稳定排序会按 list_calls 原序
    # 截断=把**最旧**的 N 通当「最近 N 通」(方向反了且不可复现)。
    window.sort(key=lambda c: (str(c.get("created_at") or ""), str(c.get("id") or "")), reverse=True)
    window = window[: max(1, int(max_calls))]

    counters = drift_counters()
    for call in window:
        call_id = str(call.get("id") or "")
        try:
            turns = repo.get_turns(call_id)
        except Exception:  # noqa: BLE001 - 单通取数失败不炸整表
            continue
        _walk_turns(turns, counters)

    entries = [
        e for e in (repo.list_qa_entries(account_id, enabled=True, owner_scope=None) or [])
        if str(e.get("id") or "")
    ]
    window_calls = len(window)
    proposals: list[dict] = []
    for entry in entries:
        p = build_drift_proposal(entry, counters, window_calls=window_calls)
        if p:
            proposals.append(p)

    # 排序:先改答案后删词条(改=更紧急,错了每通都在答错),同组按出现次数降序,
    # 再按 id 保证确定性。
    rank = {KIND_REANSWER: 0, KIND_RETIRE: 1}
    proposals.sort(key=lambda p: (rank.get(p["kind"], 9), -p["occurrences"], p["qa_id"]))
    proposals = proposals[: max(1, min(int(limit), 200))]
    counts = Counter(p["kind"] for p in proposals)
    return {
        "window_calls": window_calls,
        "entries_scanned": len(entries),
        "counts": {KIND_REANSWER: counts.get(KIND_REANSWER, 0), KIND_RETIRE: counts.get(KIND_RETIRE, 0)},
        "proposals": proposals,
        "generated_at": int(time.time()),
    }
