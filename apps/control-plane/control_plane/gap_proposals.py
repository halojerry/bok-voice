"""话术分支/意图词提案(L-②,2026-09-20,防 main.py 膨胀;与 gap_mining 同分工)。

L-①(gap_mining)把「LLM 漏网轮」采集成快答词条;本模块把同一批漏网轮对着
**目标模板的 steps_json / graph_json** 提另外两条数据面杠杆的「人工确认制提案」:

- branch(分支提案):漏网轮反复发生在某套模板的某个已知步 → 提议在该步 ref 里
  追加一条分支行「如果客户{条件}→{应答}」。运行时 FlowController.parse_step_ref
  本来就从 ref 解析这种行(apps/agent/agent_runtime/flow.py:102 `_BRANCH_LINE_RE`
  `^(?:如果客户|(?:If|When)\\s+the\\s+customer)\\s*(?P<cond>.{1,120}?)\\s*→\\s*
  (?P<resp>\\S.*)$`,flow.py:190 parse_step_ref 拆三件)——本模块只生成该语法的
  **规范中文锚形**(「如果客户{cond}→{resp}」,与画布 serializeStepRef 的规范形
  一致,apps/web/lib/flow-canvas.ts:112),不发明语法。
- intent_keyword(意图词提案):漏网轮的提法不在模板 graph_json 任何意图的
  关键词覆盖内 → 提议给「词面最相关」的意图追加一个关键词。运行时命中=
  **双侧 normalize_graph_text(剥空白中英标点→casefold)子串**
  (packages/core/bok_voice_core/flow_graph.py:56,pick_graph_action :341-373);
  意图要有至少一条 enabled 绑定边关键词才有出口,故只提有绑定的意图。

两条都是**提案**:GET 只读展示,写入必须走 POST adopt 人工确认,且最终落库走
**既有模板写链**(repo.update_template + 版本快照 + 审计,与 PUT /api/templates
同闸链),published_json 永不触碰(发布两态 W2:发布端点是它唯一写入口)。

## 端点契约(main.py 薄壳,闸链与 L-① 对齐)

- GET /api/stats/template-proposals →
  `{"coverage": <L-① 同款>, "gaps": <L-① 同款 gap 行>,
    "proposals": [proposal...], "generated_at": int}`——coverage/gaps 逐字节
  复用 gap_mining.build_llm_gap_report(驾驶舱一张画面);proposals 每 gap 行
  至多两条(branch 先、intent_keyword 后),字段:
    key(稳定去重键,见下)/kind("branch"|"intent_keyword")/template_id/
    template_name/customer_text(代表原话)/norm(归一客户话,即运行时匹配面)/
    count/calls/lang/sample_answer/sample_call_id/step(1-based,0=未知)/
    step_goal/branch_cond/branch_resp(可编辑默认应答)/branch_line(将追加的整行)/
    intent_id/intent_label/keyword(可编辑默认关键词)/
    available(bool)/blocked_reason/blocked_label(人话原因)。
  **去重即「不可采纳」而非从列表消失**:等价分支/已覆盖关键词照常出 Proposal
  但 available=false + 人话原因(驾驶舱「看得见为什么不行」),adopt 对这些键
  走幂等 created=false,绝不重复写。
- POST /api/stats/template-proposals/adopt → body `{"items": [
    {key, kind, template_id, norm, step, cond, text, intent_id}...]}`
  (web 把 GET 的 proposal 字段原样回传,text 是人工改后的应答/关键词)。
  逐项返回 `{"results": [{key, kind, template_id, id, created, detail}...]}`、
  顶层 adopted 计数;HTTP 201=至少一项真实写入,200=全部幂等/无写入。
  幂等:分支等价已存在 / 关键词已被覆盖 → created=false 不重复写不重复审计;
  键校验:服务端按 item 自带字段重算 key(hash(norm)),不一致 400——「改了
  内容还拿旧键提交」与伪造键都被拒;模板/步号/意图失配(过期提案)→
  404/400 + 人话 detail,绝不静默 no-op。
- key 形态:`branch|{template_id}|{step}|{sha1(norm)[:10]}` /
  `intent|{template_id}|{intent_id}|{sha1(norm)[:10]}`——norm 是内容完整性
  材料(web 原样回传),服务端重算比对。

## 关键词/分支冗余判定口径(纯函数,tests/test_template_proposals.py 钉住)

- 分支等价:目标步 ref 里已有一条分支行,其条件与提案条件归一
  (normalize_graph_text)相等或互为子串 → branch_exists。
- 关键词冗余(模板全域):提案关键词归一后与任一意图既有关键词相等/被其包含
  → keyword_present / keyword_covered(提案词包含既有词=该词命中面已是既有词
  的子集,加了也不改行为);或任一既有关键词已能命中本次漏网原话 →
  keyword_covered(保守:即便那条意图停用/步 scope 不含本步,也不代运营猜)。
- 意图匹配:候选=enabled 且有 enabled 绑定的意图;优先步 scope 含漏网步者;
  词面亲和度=label+keywords 归一后与漏网话的(整词包含 + 2-gram 重叠)计分,
  并列取 id 字典序(确定性);亲和 0=不猜,提 no_matching_intent。

纯函数面(build_proposals_for_template/写助手/键派生)零 repo 零 I/O;查询面
build_template_proposal_report 只走 BusinessRepository 公开方法
(list_calls/list_objects/get_turns 经 gap_mining、get_template),零手写 SQL,
不新增表/列/迁移。CP 侧**不 import agent_runtime**(云端镜像不含 apps/agent)——
分支行语法以本模块 `_BRANCH_COND_RE` 镜像 flow.py:102(同款镜像先例:
apps/web/lib/flow-canvas.ts BRANCH_RE);镜像与原件的一致性由
tests/test_template_proposals.py 的真 parse_step_ref round-trip 钉住。
"""

from __future__ import annotations

import hashlib
import json
import re

from bok_voice_core.flow_graph import (
    CATCHALL_INTENT_ID,
    KEYWORD_MAX_CHARS,
    MAX_KEYWORDS,
    FlowGraphDoc,
    FlowIntent,
    normalize_graph_text,
    parse_flow_graph,
    validate_flow_graph,
)

from . import gap_mining

# ---- kind 与 blocked reason 常量(出仓值,web 逐字消费) ----

KIND_BRANCH = "branch"
KIND_INTENT_KEYWORD = "intent_keyword"
KINDS = (KIND_BRANCH, KIND_INTENT_KEYWORD)

BR_NO_TEMPLATE = "no_template"
BR_NO_STEP = "no_step"
BR_STEP_RANGE = "step_out_of_range"
BR_EMPTY_TEXT = "empty_text"
BR_NO_ANSWER = "no_answer"
BR_BRANCH_EXISTS = "branch_exists"
BR_NO_GRAPH = "no_graph"
BR_NO_ACTIVE_INTENT = "no_active_intent"
BR_NO_MATCHING_INTENT = "no_matching_intent"
BR_KEYWORD_PRESENT = "keyword_present"
BR_KEYWORD_COVERED = "keyword_covered"
BR_KEYWORDS_FULL = "keywords_full"

# adopt 单批上限(人工确认动作,批量是给「全采纳」留的口子,不是导入通道)。
ADOPT_MAX_ITEMS = 20

# 分支条件长度上溯 flow.py `_BRANCH_LINE_RE` 的 cond 窗口(`.{1,120}?`)。
COND_MAX_CHARS = 120


class ProposalError(ValueError):
    """写助手拒绝(模板/图数据与提案失配或会产出非法数据)。端点映射 400/409。"""


# ---- 文本卫生(纯函数) ----

_WS_RE = re.compile(r"\s+")


def sanitize_branch_text(text: str, *, max_chars: int = 0) -> str:
    """条件/应答/关键词统一卫生:剥首尾、换行与「→」折成空格再并拢(分支行是
    行级语法,换行会把一条分支劈成不认账的两行;「→」是条件/应答分隔符,混入
    即提前劈开)。max_chars>0 时截断(条件窗 120=flow.py:103、关键词 64=
    flow_graph.py:27 的保存校验窗)。"""
    s = str(text or "").strip().replace("→", " ").replace("\r", " ").replace("\n", " ")
    s = _WS_RE.sub(" ", s).strip()
    if max_chars and len(s) > max_chars:
        s = s[:max_chars].strip()
    return s


def branch_line(cond: str, resp: str) -> str:
    """规范分支行(运行时 parse_step_ref 与画布 serializeStepRef 都认账的形)。"""
    return f"如果客户{cond}→{resp}"


# 条件首尾句末标点剥除:漏网原话是转写原文(「拼多多。」「啊,我不记得了。」),
# 直接拼进条件会得到「如果客户拼多多。→…」——运营看着别扭,句号/问号还会给运行时
# 的 bigram 条件匹配添噪声。**只剥首尾**,内部逗号保留(运营原话里的停顿词对
# _cond_bigram_hits 是信号,不是噪点)。
_COND_EDGE_PUNCT_RE = re.compile(r"^[\s，。！？；：、,.!?;:~～…·]+|[\s，。！？；：、,.!?;:~～…·]+$")


def sanitize_branch_cond(text: str, *, max_chars: int = COND_MAX_CHARS) -> str:
    """分支条件卫生:在 sanitize_branch_text 之上再剥首尾句末标点。

    截断在剥标点之前(sanitize_branch_text 内),剥完可能略短于 max_chars——条件窗
    上限是 120(flow.py `.{1,120}?`),只会更安全,不会再超。
    """
    return _COND_EDGE_PUNCT_RE.sub("", sanitize_branch_text(text, max_chars=max_chars)).strip()


# flow.py:102 `_BRANCH_LINE_RE` 的镜像(只取条件组;CP 不 import agent_runtime,
# 云端镜像不含 apps/agent)。改 flow.py 语法须三处同步:flow.py / flow-canvas.ts / 本文件。
_BRANCH_COND_RE = re.compile(
    r"^(?:如果客户|(?:If|When)\s+the\s+customer)\s*(?P<cond>.{1,120}?)\s*→\s*(?P<resp>\S.*)$",
    re.IGNORECASE,
)


def existing_branch_conds(ref: str) -> list[str]:
    """step.ref 里已有分支行的条件列表(镜像 parse_step_ref 的行扫描)。"""
    out: list[str] = []
    for raw in str(ref or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _BRANCH_COND_RE.match(line)
        if m:
            out.append(m.group("cond").strip())
    return out


# ---- 去重键(稳定,纯派生) ----


def _norm_hash(norm: str) -> str:
    return hashlib.sha1(str(norm or "").encode("utf-8")).hexdigest()[:10]


def branch_proposal_key(template_id: str, step: int, norm: str) -> str:
    return f"{KIND_BRANCH}|{template_id}|{int(step)}|{_norm_hash(norm)}"


def intent_proposal_key(template_id: str, intent_id: str, norm: str) -> str:
    return f"{KIND_INTENT_KEYWORD}|{template_id}|{intent_id}|{_norm_hash(norm)}"


def _norm_of(gap_row: dict) -> str:
    """gap 行的归一键:aggregate_gap_groups 形状自带 norm;build_llm_gap_report
    出仓前 pop 掉——用 candidate_norm(代表原话)同值重算(聚组键本就是逐条
    candidate_norm 同值,测试钉住 round-trip)。"""
    norm = str(gap_row.get("norm") or "")
    if norm:
        return norm
    return gap_mining.candidate_norm(str(gap_row.get("customer_text") or ""))


# ---- 意图匹配(纯函数) ----


def _bigrams(s: str) -> set[str]:
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def intent_affinity(label: str, keywords: list[str], norm_text: str) -> int:
    """词面亲和度:整词包含(权重=长度+2)与 2-gram 重叠计数。0=无词面关系。"""
    score = 0
    for token in [label, *(keywords or [])]:
        t = normalize_graph_text(token)
        if not t:
            continue
        if t in norm_text:
            score += len(t) + 2
        score += len(_bigrams(t) & _bigrams(norm_text))
    return score


def match_intent_for_text(
    doc: FlowGraphDoc, *, step: int, norm_text: str
) -> tuple[FlowIntent | None, str]:
    """为漏网话挑「最相关意图」:(intent, "") 或 (None, blocked_reason)。

    候选=enabled 且有 enabled 绑定边的意图(关键词要有出口才有意义);优先步
    scope 含漏网步(空 scope=全程)的意图,没有再退全候选;亲和 >0 才算相关,
    并列 (亲和降序, id 升序) 取首个——确定性,不依赖 dict 序。P2.2 兜底意图
    `"*"` 永不进候选(它的形状约束=keywords 必须为空,给它加词会被保存期 400)。
    """
    bound = {b.intent for b in doc.bindings if b.enabled}
    candidates = [
        i for i in doc.intents if i.enabled and i.id in bound and i.id != CATCHALL_INTENT_ID
    ]
    if not candidates:
        return None, BR_NO_ACTIVE_INTENT
    scoped = [i for i in candidates if not i.steps or (step > 0 and step in i.steps)]
    pool = scoped or candidates
    scored = sorted(
        ((-intent_affinity(i.label, i.keywords, norm_text), i.id, i) for i in pool),
        key=lambda t: (t[0], t[1]),
    )
    best = scored[0]
    if -best[0] <= 0:
        return None, BR_NO_MATCHING_INTENT
    return best[2], ""


def keyword_conflict(doc: FlowGraphDoc, keyword: str, norm_text: str) -> str:
    """模板全域关键词冗余判定(纯函数):""=可加,否则 blocked_reason。

    - keyword_present:与既有关键词归一相等;
    - keyword_covered:提案词**包含**某既有词(命中面是既有词子集,加了不改行为)
      或任一既有词已能命中本次漏网原话(运行时子串匹配早已接住,提案是死重;
      保守不区分那条意图是否停用/步 scope 不含——不代运营猜)。
    """
    nk = normalize_graph_text(keyword)
    if not nk:
        return BR_EMPTY_TEXT
    existing = [
        w
        for i in doc.intents
        for k in (i.keywords or [])
        if (w := normalize_graph_text(k))
    ]
    if any(w == nk for w in existing):
        return BR_KEYWORD_PRESENT
    if any(w in nk for w in existing):
        return BR_KEYWORD_COVERED
    if norm_text and any(w in norm_text for w in existing):
        return BR_KEYWORD_COVERED
    return ""


# ---- 提案生成(纯函数;gap 行=aggregate_gap_groups 形状,norm 可缺省重算) ----


def _steps_array(steps_json: str) -> list:
    text = str(steps_json or "").strip()
    if not text:
        return []
    try:
        arr = json.loads(text)
    except ValueError:
        return []
    return arr if isinstance(arr, list) else []


def _step_entry(arr: list, step_1based: int) -> tuple[object, str, str]:
    """(entry, ref, goal);越界返回 (None, "", "")。"""
    idx = int(step_1based) - 1
    if idx < 0 or idx >= len(arr):
        return None, "", ""
    entry = arr[idx]
    if isinstance(entry, dict):
        return entry, str(entry.get("ref") or ""), str(entry.get("goal") or "")
    if isinstance(entry, str):
        return entry, entry, ""
    return None, "", ""


def _blocked(kind: str, gap: dict, norm: str, template_id: str, template_name: str,
             reason: str, label: str) -> dict:
    return _proposal_shape(
        kind=kind, gap=gap, norm=norm, template_id=template_id, template_name=template_name,
        available=False, reason=reason, label=label,
        branch={},
        intent={},
    )


def _proposal_shape(*, kind: str, gap: dict, norm: str, template_id: str,
                    template_name: str, available: bool, reason: str, label: str,
                    branch: dict, intent: dict) -> dict:
    step = int(gap.get("step") or 0)
    key = (
        branch_proposal_key(template_id, step, norm)
        if kind == KIND_BRANCH
        else intent_proposal_key(template_id, str(intent.get("intent_id") or ""), norm)
    )
    return {
        "key": key,
        "kind": kind,
        "template_id": template_id,
        "template_name": template_name,
        "customer_text": str(gap.get("customer_text") or ""),
        "norm": norm,
        "count": int(gap.get("count") or 0),
        "calls": int(gap.get("calls") or 0),
        "lang": str(gap.get("lang") or "zh"),
        "sample_answer": str(gap.get("sample_answer") or ""),
        "sample_call_id": str(gap.get("sample_call_id") or ""),
        "step": step,
        "step_goal": str(branch.get("step_goal") or ""),
        "branch_cond": str(branch.get("cond") or ""),
        "branch_resp": str(branch.get("resp") or ""),
        "branch_line": str(branch.get("line") or ""),
        "intent_id": str(intent.get("intent_id") or ""),
        "intent_label": str(intent.get("intent_label") or ""),
        "keyword": str(intent.get("keyword") or ""),
        "available": available,
        "blocked_reason": reason,
        "blocked_label": "" if available else label,
    }


def build_proposals_for_template(
    gap_rows: list[dict],
    *,
    template_id: str,
    steps_json: str,
    graph_json: str,
    template_name: str = "",
    template_found: bool = True,
) -> list[dict]:
    """一组同模板 gap 行 → 提案列表(每行 branch 一条 + intent_keyword 一条)。

    去重即「不可采纳」:等价分支已存在/关键词已被覆盖照常出提案,只是
    available=false + 人话 blocked_label(驾驶舱要「看得见为什么不行」,
    adopt 端点对这些状态走幂等 created=false)。纯函数,零 I/O,同输入同输出。
    """
    out: list[dict] = []
    doc = parse_flow_graph(graph_json)
    steps = _steps_array(steps_json)
    for gap in gap_rows:
        norm = _norm_of(gap)
        if not template_id or not template_found:
            label = (
                "这通通话没有绑定话术模板，先去「话术」页给这类通话绑定一套模板。"
                if not template_id
                else "这套话术模板不存在或已被删除，请刷新学习页后再试。"
            )
            out.append(_blocked(KIND_BRANCH, gap, norm, template_id, template_name, BR_NO_TEMPLATE, label))
            out.append(_blocked(KIND_INTENT_KEYWORD, gap, norm, template_id, template_name, BR_NO_TEMPLATE, label))
            continue

        # ---- branch 提案 ----
        step = int(gap.get("step") or 0)
        if step <= 0:
            out.append(_blocked(
                KIND_BRANCH, gap, norm, template_id, template_name, BR_NO_STEP,
                "这句话没记录到发生在第几步，做不了分支；可以先用「做成快答」采成词条。"))
        else:
            entry, ref, goal = _step_entry(steps, step)
            cond = sanitize_branch_cond(str(gap.get("customer_text") or ""))
            resp = sanitize_branch_text(str(gap.get("sample_answer") or ""))
            reason = label = ""
            if entry is None:
                reason, label = BR_STEP_RANGE, (
                    f"第 {step} 步已经不在这套话术里（现在共 {len(steps)} 步），请刷新学习页后再试。")
            elif not cond:
                reason, label = BR_EMPTY_TEXT, "这句话提不出可用的分支条件。"
            elif not resp:
                reason, label = BR_NO_ANSWER, "AI 当时没答上这句话；先点「做成快答」补上回答，再做分支。"
            else:
                norm_cond = normalize_graph_text(cond)
                for existing in existing_branch_conds(ref):
                    w = normalize_graph_text(existing)
                    if w and (w == norm_cond or w in norm_cond or norm_cond in w):
                        reason, label = BR_BRANCH_EXISTS, (
                            f"这句话已经在第 {step} 步的分支里了，不用重复加。")
                        break
            if reason:
                out.append(_blocked(KIND_BRANCH, gap, norm, template_id, template_name, reason, label))
            else:
                out.append(_proposal_shape(
                    kind=KIND_BRANCH, gap=gap, norm=norm, template_id=template_id,
                    template_name=template_name, available=True, reason="", label="",
                    branch={"cond": cond, "resp": resp, "line": branch_line(cond, resp), "step_goal": goal},
                    intent={},
                ))

        # ---- intent_keyword 提案 ----
        if not str(graph_json or "").strip():
            out.append(_blocked(
                KIND_INTENT_KEYWORD, gap, norm, template_id, template_name, BR_NO_GRAPH,
                "这套话术还没设置流程图意图；先去话术页的流程画布添加意图和连线。"))
            continue
        intent, reason = match_intent_for_text(doc, step=step, norm_text=norm)
        if intent is None:
            label = (
                "话术图里的意图都触发不了（意图要连上跳步或播快答才会生效），先去流程画布连线。"
                if reason == BR_NO_ACTIVE_INTENT
                else "没找到和这句话相关的意图；确有需要的话，去话术页流程画布手动加关键词。"
            )
            out.append(_blocked(KIND_INTENT_KEYWORD, gap, norm, template_id, template_name, reason, label))
            continue
        keyword = sanitize_branch_text(
            str(gap.get("customer_text") or ""), max_chars=KEYWORD_MAX_CHARS)
        intent_ctx = {"intent_id": intent.id, "intent_label": intent.label, "keyword": keyword}
        reason = label = ""
        if not keyword:
            reason, label = BR_EMPTY_TEXT, "这句话提不出可用的关键词。"
        elif len(intent.keywords) >= MAX_KEYWORDS:
            reason, label = BR_KEYWORDS_FULL, (
                f"这个意图的关键词已经满了（上限 {MAX_KEYWORDS} 条），先去话术页清理再加。")
        else:
            conflict = keyword_conflict(doc, keyword, norm)
            if conflict == BR_KEYWORD_PRESENT:
                reason, label = conflict, "这个关键词已经在这套话术的某个意图里了。"
            elif conflict == BR_KEYWORD_COVERED:
                reason, label = conflict, "现有意图的关键词已经能接住这句话，不用再加。"
            elif conflict:
                reason, label = conflict, "这句话提不出可用的关键词。"
        if reason:
            out.append(_proposal_shape(
                kind=KIND_INTENT_KEYWORD, gap=gap, norm=norm, template_id=template_id,
                template_name=template_name, available=False, reason=reason, label=label,
                branch={}, intent=intent_ctx,
            ))
        else:
            out.append(_proposal_shape(
                kind=KIND_INTENT_KEYWORD, gap=gap, norm=norm, template_id=template_id,
                template_name=template_name, available=True, reason="", label="",
                branch={},
                intent=intent_ctx,
            ))
    return out


# ---- 查询面(复用 gap_mining 聚合,不重走 turns) ----


def build_template_proposal_report(
    repo,
    *,
    account_id: str,
    template_id: str = "",
    min_calls: int = 3,
    limit: int = 30,
    exclude_test_objects: bool = True,
) -> dict:
    """查询主入口:coverage+gaps 逐字节来自 gap_mining.build_llm_gap_report;
    对每条 gap 行按其 template_id 取模板行(steps_json/graph_json)生成提案。

    模板按账号归属校验(跨账号/已删=按「模板不存在」出不可采纳提案,不炸、
    不跨账号泄露内容)。同模板行进程内缓存(一次报表至多每模板查一次)。
    """
    base = gap_mining.build_llm_gap_report(
        repo,
        account_id=account_id,
        template_id=template_id,
        min_calls=min_calls,
        limit=limit,
        exclude_test_objects=exclude_test_objects,
    )
    tpl_cache: dict[str, dict | None] = {}

    def _row(tid: str) -> dict | None:
        if tid not in tpl_cache:
            row = None
            try:
                row = repo.get_template(tid) or None
            except Exception:  # noqa: BLE001 - 模板面读失败按「不存在」降级,不炸报表
                row = None
            if row is not None and str(row.get("account_id") or "") != str(account_id):
                row = None  # 跨账号模板内容不进提案(归属校验,与 by-ID 闸同口径)
            tpl_cache[tid] = row
        return tpl_cache[tid]

    proposals: list[dict] = []
    for gap in base["gaps"]:
        tid = str(gap.get("template_id") or "")
        row = _row(tid) if tid else None
        proposals.extend(
            build_proposals_for_template(
                [gap],
                template_id=tid,
                steps_json=str((row or {}).get("steps_json") or ""),
                graph_json=str((row or {}).get("graph_json") or ""),
                template_name=str((row or {}).get("name") or ""),
                template_found=row is not None,
            )
        )
    return {
        "coverage": base["coverage"],
        "gaps": base["gaps"],
        "proposals": proposals,
        "generated_at": base["generated_at"],
    }


# ---- 写助手(纯函数:字符串进字符串出;published_json 不在面上,天然不触碰) ----


def append_branch_to_steps(
    steps_json: str, step_1based: int, cond: str, resp: str
) -> tuple[str, bool]:
    """把分支行追加到第 step_1based 步 ref 尾部 → (新 steps_json, changed)。

    - 幂等:该步已有逐字相同的行,或已有条件与提案条件归一等价(相等/互含)的
      分支 → 原样返回 changed=False;
    - 语法由构造保证(规范中文锚+卫生化条件/应答),round-trip 由测试对真
      parse_step_ref 钉住;
    - steps_json 非法/步号越界/条件或应答空 → ProposalError(端点映射 4xx,
      绝不静默写坏)。
    不产出 published_json;调用方拿返回值走既有 update_template 写链。
    """
    cond = sanitize_branch_cond(cond)
    resp = sanitize_branch_text(resp)
    if not cond or not resp:
        raise ProposalError("分支条件与应答都不能为空")
    line = branch_line(cond, resp)
    original = str(steps_json or "")
    text = original.strip()
    if not text:
        arr: list = []
    else:
        try:
            arr = json.loads(text)
        except ValueError as exc:
            raise ProposalError(f"steps_json 不是合法 JSON：{exc}") from exc
    if not isinstance(arr, list):
        raise ProposalError("steps_json 必须是 JSON 数组")
    idx = int(step_1based) - 1
    if idx < 0 or idx >= len(arr):
        raise ProposalError(f"第 {int(step_1based)} 步不存在（现在共 {len(arr)} 步）")
    entry = arr[idx]
    if isinstance(entry, dict):
        ref = str(entry.get("ref") or "")
    elif isinstance(entry, str):
        ref = entry
    else:
        raise ProposalError(f"第 {int(step_1based)} 步不是可编辑的话术步骤")
    if line in ref:
        return original, False
    norm_cond = normalize_graph_text(cond)
    for existing in existing_branch_conds(ref):
        w = normalize_graph_text(existing)
        if w and (w == norm_cond or w in norm_cond or norm_cond in w):
            return original, False
    new_ref = f"{ref.rstrip()}\n{line}" if ref.strip() else line
    if isinstance(entry, dict):
        entry["ref"] = new_ref
    else:
        arr[idx] = {"goal": "", "ref": new_ref}
    return json.dumps(arr, ensure_ascii=False), True


def append_keyword_to_graph(
    graph_json: str, intent_id: str, keyword: str
) -> tuple[str, str, bool]:
    """给指定意图追加关键词 → (新 graph_json, 新关键词, changed)。

    - 幂等:任一意图已有归一相等、或被提案词包含的既有关键词 → 原样返回
      changed=False(命中面不变,加了也是死重);
    - **严格校验在写入前**:追加后整图 validate_flow_graph(CP 保存同款),
      非法(关键词超限/条数超限/图超限/坏 JSON)→ ProposalError 携带校验器
      原文,绝不写坏数据;
    - 不产出 published_json。
    """
    keyword = sanitize_branch_text(keyword, max_chars=KEYWORD_MAX_CHARS)
    if not keyword:
        raise ProposalError("关键词不能为空")
    original = str(graph_json or "")
    text = original.strip()
    if not text:
        raise ProposalError("这套话术还没有 graph_json，先去流程画布添加意图")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProposalError(f"graph_json 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ProposalError("graph_json 必须是 JSON 对象")
    intents = data.get("intents")
    if not isinstance(intents, list):
        raise ProposalError("graph_json.intents 缺失或不是数组")
    target = next(
        (i for i in intents if isinstance(i, dict) and str(i.get("id") or "") == str(intent_id)),
        None,
    )
    if target is None:
        raise ProposalError(f"意图 {intent_id} 不存在（可能已被删除），请刷新学习页")
    nk = normalize_graph_text(keyword)
    for item in intents:
        if not isinstance(item, dict):
            continue
        for k in item.get("keywords") or []:
            w = normalize_graph_text(str(k or ""))
            if w and (w == nk or w in nk):
                return original, keyword, False
    kws = target.get("keywords")
    if not isinstance(kws, list):
        kws = []
    kws.append(keyword)
    target["keywords"] = kws
    new_text = json.dumps(data, ensure_ascii=False)
    errors = validate_flow_graph(new_text)
    if errors:
        raise ProposalError("；".join(errors[:5]))
    return new_text, keyword, True
