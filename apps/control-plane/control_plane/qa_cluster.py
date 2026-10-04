"""QA 自学习聚类 runner(2026-09-19 W3-T1,防 main.py 膨胀,端点瘦逻辑全在此)。

把 scripts/mine_qa.py --cluster 的 LLM 编排提为 CP 进程内能力:
mine_qa_pairs(与 /api/reports/qa-pairs 同源,进程内调用零重复)→ 按语言分批
问本地 LLM(纯函数在 bok_voice_core.qa_cluster,CLI 与 CP 共用)→ 三列计划
(variant/fresh/junk)→ apply 按 select 逐行盖章入库(与 create_qa_entry 同款:
账号/owner/priority)。

LLM 端点=mining 车道（2026-09-25 模型路由：路由表命中吃 base_url/model/api_key；
未命中=MLX_LLM_BASE_URL env 链逐字节同旧，CP env 面已有，勿用 CLI 的 BOK_LLM_PORT）;
模型经 /v1/models 发现(路径型 id、含 4b 优先),路由 model 空/覆盖 env BOK_QA_CLUSTER_MODEL;
httpx 同步直打 OpenAI 兼容 /v1(temperature 0/max_tokens 4096/timeout 120,
照 Summarizer 姿势)。
单飞:模块级锁+运行标志,冲突 AlreadyRunning(端点转 409)。
dry 计划缓存:per-account {plan, ts} TTL 600s 读时惰性过期——apply 优先吃新鲜
缓存免二次 LLM,采纳成功后缓存作废(已入库问法下次重算即 dup-existing 不再 offered)。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

import httpx

from bok_voice_core.model_routes import PROVIDER_OPENAI, resolve_route


def _safe_httpx_get(url: str, **kwargs):
    """出站闸门（tools/bok.py _safe_urlopen 同形状）：仅 http/https、host 非空、
    无 userinfo；不过闸=PermissionError。挖掘 LLM 端点=MLX_LLM_BASE_URL env/
    路由表（运维配置面，可指云端，不锁环回）。"""
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if not (
        parts.scheme in ("http", "https")
        and bool(host)
        and not parts.username
        and not parts.password
    ):
        raise PermissionError(f"出站 URL 未过护栏（拒发）: {url}")
    return httpx.get(url, **kwargs)


def _safe_httpx_post(url: str, **kwargs):
    """同 _safe_httpx_get，post 面。"""
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if not (
        parts.scheme in ("http", "https")
        and bool(host)
        and not parts.username
        and not parts.password
    ):
        raise PermissionError(f"出站 URL 未过护栏（拒发）: {url}")
    return httpx.post(url, **kwargs)

from bok_voice_core.qa_cluster import (  # noqa: F401  (_CLUSTER_SYSTEM_PROMPT re-export)
    _CLUSTER_SYSTEM_PROMPT,
    build_cluster_messages,
    parse_llm_decisions,
    plan_cluster,
)
from bok_voice_core.qa_text import mine_qa_pairs, normalize_question

from . import hotword_mining
from .auth import current_identity
from .deps import read_model_routing_raw

# dry 计划缓存 TTL(秒):apply 带选择时吃缓存免二次 LLM。
# 键=(account, min_calls, limit)——不同参数=不同计划,apply 参数与 dry 不符时
# 不得命中旧计划(下标错位采错条目);apply 作废=清该账号全部键。
_plan_cache: dict[tuple[str, int, int], tuple[dict, float]] = {}
_LOCK = threading.Lock()
_RUNNING = False


class ClusterError(RuntimeError):
    """LLM 网络/发现/解析失败——端点转 503(文案带原因)。"""


class AlreadyRunning(RuntimeError):
    """单飞冲突——端点转 409。"""


class PlanStaleError(RuntimeError):
    """apply 带选择但新鲜缓存缺席(TOCTOU 二道闸,2026-10-02)——端点转 409。

    一道闸 has_fresh_plan 与取计划之间缓存可被并发作废(apply 成功/闲时引擎采纳
    都清账号键);此时 run_cluster 缺席重算会把**前端的旧下标**对到**新计划**上,
    边界内错位不报错、直接采错条目。fresh_only=True 令取计划与守卫同一份:命中=
    守卫验过的那份,缺席=409 让前端重新生成,永不静默重算。"""


def _base_url() -> str:
    return os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1").rstrip("/")


def _mining_lane() -> tuple[str, str, str, bool]:
    """mining 车道解析（2026-09-25 模型路由）→ (base_url, model_override, api_key, enable_thinking)。

    铁律——路由表未命中（空表/kill-switch → source=="env"）时返回 env 链现状
    （_base_url() + /models 发现），逐字节同旧。openai 云端档才真正携带 api_key
    （本地档 "mlx" 不塞请求头，契约 model_routes 注释）；enable_thinking 请求体
    扩展字段只随云端档下发（本地 mlx 走启动旗标 --chat-template-args，见计划 §2.2）。
    """
    route = resolve_route("mining", os.environ, read_model_routing_raw())
    if route.source != "routing":
        return _base_url(), "", "", False
    if route.provider == PROVIDER_OPENAI:
        return route.base_url, route.model, route.api_key, route.enable_thinking
    if route.base_url:
        # local 档显式改端点：model 空仍走 /models 发现，不带 thinking/body 扩展。
        return route.base_url, route.model, "", False
    # 坏数据（local 档无端点）=视同未命中，env 链兜底。
    return _base_url(), "", "", False


def _discover_model(base_url: str) -> str:
    """取本地 server 已加载模型的真实路径(仓规:request model 必须填真实路径;
    含 4b 优先——4B 是主对话模型,1.8B 是 B 线翻译专才)。"""
    r = _safe_httpx_get(f"{base_url}/models", timeout=10)
    r.raise_for_status()
    ids = [str(d.get("id") or "") for d in (r.json().get("data") or [])]
    paths = [i for i in ids if i.startswith("/")]
    for p in paths:
        if "4b" in p.lower():
            return p
    return paths[0] if paths else (ids[0] if ids else "")


def _resolve_model(base_url: str) -> str:
    override = os.environ.get("BOK_QA_CLUSTER_MODEL", "").strip()
    if override:
        return override
    model = _discover_model(base_url)
    if not model:
        raise ClusterError("llm 无可用模型(/v1/models 空)——先起 bok serve 的 LLM")
    return model


def _llm_chat(
    base_url: str,
    model: str,
    system: str,
    user: str,
    *,
    timeout: float = 120.0,
    api_key: str = "",
    enable_thinking: bool = False,
) -> str:
    """OpenAI 兼容 /v1/chat/completions(httpx 同步,Summarizer 先例)。

    api_key/enable_thinking 仅云端路由档携带（env 链请求=与旧版逐字节一致）。
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        # 4096→2048（2026-09-28 车道干扰审计）：决策输出实测数百 token,4096 只是
        # 头皮余量;这个调用是 :1235 单闸后台侧最长持有者,钳半最坏情况。截断的
        # 失败形态=JSON 解析失败→该账号 error 隔离（digest errors.append）,安全。
        "max_tokens": 2048,
        "stream": False,
    }
    if enable_thinking:
        payload["enable_thinking"] = True
    # api_key 仅云端档携带；env 链（api_key=""）保持与改造前同一调用形状
    # （不带 headers 参，Summarize 同款纪律——monkeypatch 窄签名不破）。
    if api_key:
        r = _safe_httpx_post(
            f"{base_url}/chat/completions",
            json=payload,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    else:
        r = _safe_httpx_post(f"{base_url}/chat/completions", json=payload, timeout=timeout)
    r.raise_for_status()
    return str((r.json().get("choices") or [{}])[0].get("message", {}).get("content") or "")


_PLAN_TTL_S = 600.0


def _plan_cache_get(account_id: str, min_calls: int, limit: int) -> dict | None:
    cached = _plan_cache.get((account_id, min_calls, limit))
    if cached is None:
        return None
    plan, ts = cached
    if time.time() - ts >= _PLAN_TTL_S:
        _plan_cache.pop((account_id, min_calls, limit), None)  # 读时惰性过期
        return None
    return plan


def _plan_cache_pop_account(account_id: str) -> None:
    for key in [k for k in _plan_cache if k[0] == account_id]:
        _plan_cache.pop(key, None)


def has_fresh_plan(account_id: str, min_calls: int, limit: int) -> bool:
    """apply 带勾选(select)的前置守卫:计划下标只在「与 dry 同参数的新鲜缓存」上有效。

    缓存缺席/TTL 过期/参数不符时 select 下标指向的可能是另一份重算计划——
    端点对 select≠None 的 apply 要求新鲜缓存,否则 409 让前端重新生成(select=None
    的「采纳全部」可安全重算,语义=采纳当前计划全量)。"""
    return _plan_cache_get(account_id, min_calls, limit) is not None


def _compute_hotwords(
    repo: Any,
    account_id: str,
    conversations: list[list[dict]],
    existing_rows: list[dict],
    base_url: str,
    model: str,
    api_key: str,
    thinking: bool,
) -> dict:
    """同一份对话扫描上挖 ASR 热词候选（零额外 I/O），同一 mining 车道 LLM 单次批量判定。

    existing_words=两级热词行（含停用）避重复提议；existing_q_norms=qa 词条归一后
    问法集（gap_ngram 覆盖排除，与 gap_mining 同源 normalize_question）。候选空=
    零 LLM 调用直接空段。LLM 失败照 qa 聚类口径 503（同车道同服务器，不静默吞）。
    """
    existing_words = [
        str(r.get("word") or "") for r in repo.list_hotword_entries(account_id, enabled=None)
    ]
    existing_q_norms = {
        normalize_question(str(r.get("question_text") or ""))
        for r in existing_rows or []
        if str(r.get("question_text") or "").strip()
    }
    candidates = hotword_mining.extract_hotword_candidates(
        conversations, existing_words, existing_q_norms
    )
    if not candidates:
        return hotword_mining.build_hotword_section([], {})
    message = hotword_mining.build_hotword_messages(candidates)
    try:
        text = _llm_chat(
            base_url,
            model,
            hotword_mining._HOTWORD_SYSTEM_PROMPT,
            message,
            api_key=api_key,
            enable_thinking=thinking,
        )
    except Exception as exc:  # noqa: BLE001 - 网络/解析失败统一 503 带原因
        raise ClusterError(f"hotword llm 请求失败: {exc!r}") from exc
    return hotword_mining.build_hotword_section(
        candidates, hotword_mining.parse_hotword_plan(text)
    )


def _compute_plan(
    repo: Any,
    account_id: str,
    min_calls: int,
    limit: int,
    *,
    with_hotwords: bool = False,
) -> dict:
    """挖掘→分语言 LLM 聚类→三列计划(junk 转 {row,reason} 便于 JSON 出仓)。

    词条清单=账号全量(owner_scope=None 无 owner 过滤,机器/管理口径)——variant
    只拷贝 answer_text,对目标词条无运行时依赖(任务书探针结论)。

    with_hotwords=True 时在**同一份 conversations** 上追加 hotwords 段（零额外
    扫描；供 POST /api/qa/cluster dry 计划用）。默认 False=qa_digest 闲时循环复用
    本内核时零行为/零 LLM 增量变化。
    """
    conversations = repo.iter_call_conversations(account_id, exclude_test_objects=True)
    pairs = mine_qa_pairs(conversations, min_calls=min_calls, limit=limit)
    existing_rows = repo.list_qa_entries(account_id, owner_scope=None)
    # mining 车道（模型路由）：env 链/本地显式端点走发现，云端档 model 必填直接用。
    base_url, model_override, api_key, thinking = _mining_lane()
    try:
        model = model_override or _resolve_model(base_url)
    except httpx.HTTPError as exc:
        raise ClusterError(f"llm models 探测失败({base_url}): {exc!r}") from exc
    variants: list[dict] = []
    fresh: list[dict] = []
    junk: list[tuple[dict, str]] = []
    for batch in build_cluster_messages(pairs, existing_rows):
        rows = batch["rows"]
        if not batch["message"]:
            # 该语言还没有词条 → 全部交回质量闸语义(CLI 同款:不出 LLM 请求)
            fresh.extend(rows)
            continue
        try:
            text = _llm_chat(
                base_url, model, _CLUSTER_SYSTEM_PROMPT, batch["message"],
                api_key=api_key, enable_thinking=thinking,
            )
        except Exception as exc:  # noqa: BLE001 - 网络/解析失败统一 503 带原因
            raise ClusterError(f"llm 请求失败(lang={batch['lang']}): {exc!r}") from exc
        decisions = parse_llm_decisions(text)
        v, f, j = plan_cluster(rows, existing_rows, decisions)
        variants.extend(v)
        fresh.extend(f)
        junk.extend(j)
    plan = {
        "account_id": account_id,
        "min_calls": min_calls,
        "limit": limit,
        "variants": variants,
        "fresh": fresh,
        "junk": [{"row": r, "reason": reason} for r, reason in junk],
        "model": model,
        "counts": {
            "variants": len(variants),
            "fresh": len(fresh),
            "junk": len(junk),
            "candidates": len(pairs),
        },
    }
    if with_hotwords:
        # 同一份 conversations 上追加热词段（零额外 I/O）；LLM 与 qa 聚类同车道。
        plan["hotwords"] = _compute_hotwords(
            repo, account_id, conversations, existing_rows,
            base_url, model, api_key, thinking,
        )
    return plan


class _SingleFlight:
    """模块级单飞:运行标志冲突即 AlreadyRunning(端点 409),不排队。"""

    def __enter__(self) -> None:
        global _RUNNING
        with _LOCK:
            if _RUNNING:
                raise AlreadyRunning("qa cluster already running")
            _RUNNING = True

    def __exit__(self, *exc_info: object) -> None:
        global _RUNNING
        with _LOCK:
            _RUNNING = False


def run_cluster(
    repo: Any,
    account_id: str,
    min_calls: int = 5,
    limit: int = 60,
    *,
    fresh_only: bool = False,
) -> dict:
    """dry 主入口:缓存命中直接回(不二次 LLM),缺席单飞重算并回填缓存。

    fresh_only=True(apply 带选择时):**只吃缓存不重算**——缺席抛 PlanStaleError
    (端点 409),保证 select 下标恒对到守卫验过的那份计划(TOCTOU 二道闸)。"""
    hit = _plan_cache_get(account_id, min_calls, limit)
    if hit is not None:
        return hit
    if fresh_only:
        raise PlanStaleError("聚类计划已失效，请重新生成计划后再采纳")
    with _SingleFlight():
        # 双检:等锁期间另一请求可能刚算完回填
        hit = _plan_cache_get(account_id, min_calls, limit)
        if hit is not None:
            return hit
        plan = _compute_plan(repo, account_id, min_calls, limit, with_hotwords=True)
    _plan_cache[(account_id, min_calls, limit)] = (plan, time.time())
    return plan


def _clamp_priority(value: int | None) -> int:
    """QA 优先级钳制,与 main.create_qa_entry 同款([0,1000],缺省 10)。"""
    if value is None:
        return 10
    return max(0, min(int(value), 1000))


def _fresh_payload(row: dict, account_id: str) -> dict:
    """fresh(全新问答)→ 入库 payload,与 mine_qa _entry_payload 同姿势。"""
    return {
        "question_text": str(row.get("question") or ""),
        "answer_text": str(row.get("answer") or ""),
        "lang": str(row.get("lang") or "zh"),
        "scope": "global",
        "source": "mined",
        "enabled": True,
        "account_id": account_id,
    }


def _select_rows(plan: dict, select: list[dict] | None) -> tuple[list[dict], list[dict]]:
    """select=[{kind,i}] 过滤(None=全部 variants+fresh);越界/未知 kind 静默忽略。"""
    variants = list(plan.get("variants") or [])
    fresh = list(plan.get("fresh") or [])
    if select is None:
        return variants, fresh
    sel_v: list[dict] = []
    sel_f: list[dict] = []
    for item in select or []:
        d = item if isinstance(item, dict) else {}
        kind = str(d.get("kind") or "")
        try:
            i = int(d.get("i"))
        except (TypeError, ValueError):
            continue
        if kind == "variant" and 0 <= i < len(variants):
            sel_v.append(variants[i])
        elif kind == "fresh" and 0 <= i < len(fresh):
            sel_f.append(fresh[i])
    return sel_v, sel_f


def apply_cluster(
    repo: Any,
    request: Any,
    account_id: str,
    plan: dict,
    select: list[dict] | None = None,
    hotword_select: list[int] | None = None,
    *,
    audit: Callable[..., dict] | None = None,
) -> dict:
    """apply 主入口:select 过滤 → 逐行盖章入库 → 审计。

    盖章与 main.create_qa_entry 同款:identity 存在且 role≠root → account 强制
    本账号;role==user → owner 强制本人;priority 钳制。每行审计 qa_entry.create,
    汇总审计 qa.cluster。采纳成功(created>0)后作废该账号 dry 缓存——已入库问法
    下次重算即 dup-existing,不再 offered(防 TTL 内重复采纳双写)。

    hotword_select(EX-H1):dry 计划 hotwords.candidates 的下标;选中候选 upsert 进
    `hotword_entries` 为**本账号行**(source=mined, freq 取计划值)——INSERT-or-UPDATE
    在 UNIQUE(account_id, lang, word) 上,已启用且 freq 未增=幂等 no-op 不审计;
    真写入(建行/复活/bump)每行审计 hotword.create(detail.source=qa-cluster,
    detail.freq=计划值)。热词采纳成功与 qa 采纳同款作废 dry 缓存。
    """
    sel_v, sel_f = _select_rows(plan, select)
    hw_rows = hotword_mining.select_hotword_rows(plan, hotword_select)
    has_selection = select is not None or hotword_select is not None
    created = 0
    hotwords_created = 0
    try:
        with _SingleFlight():  # 创建段也单飞:并发 apply 双写 / dry 撞正在落库的计划都 409
            audit_fn = audit or (lambda **kw: {})
            ident = current_identity(request)
            # 采纳归属:与 qa 行同款——user/admin 强制本账号;root/无身份按 cluster 账号。
            owner_account = ident.account_id if (ident is not None and ident.role != "root") else account_id
            rows_payloads = [("variant", dict(p)) for p in sel_v] + [
                ("fresh", _fresh_payload(p, account_id)) for p in sel_f
            ]
            for kind, payload in rows_payloads:
                if ident is not None and ident.role != "root":
                    payload["account_id"] = ident.account_id
                    if ident.role == "user":
                        payload["owner_user_id"] = ident.user_id
                payload["priority"] = _clamp_priority(payload.get("priority"))
                row = repo.create_qa_entry(payload)
                created += 1
                audit_fn(
                    "qa_entry.create",
                    subject_type="qa_entry",
                    subject_id=str(row.get("id") or ""),
                    account_id=str(payload.get("account_id") or account_id),
                    detail={"owner_user_id": str(row.get("owner_user_id") or ""), "kind": kind},
                )
            for cand in hw_rows:
                freq = int(cand.get("freq") or 0)
                res = repo.upsert_hotword_entry(
                    {
                        "account_id": owner_account,
                        "lang": str(cand.get("lang") or "zh"),
                        "word": str(cand.get("word") or ""),
                        "source": "mined",
                        "enabled": True,
                        "freq": freq,
                    }
                )
                if res.get("changed"):
                    hotwords_created += 1
                    audit_fn(
                        "hotword.create",
                        subject_type="hotword",
                        subject_id=str(res.get("id") or ""),
                        account_id=owner_account,
                        detail={"source": "qa-cluster", "freq": freq},
                    )
            audit_fn(
                "qa.cluster",
                subject_type="qa_entry",
                account_id=account_id,
                detail={"variants": len(sel_v), "fresh": len(sel_f), "junk": len(plan.get("junk") or [])},
            )
    finally:
        # 恒作废(2026-10-02 部分失败收口):带选择的下标只对生成它的那份计划有效——
        # 循环中途抛异常(部分行已入库)时旧计划下标同样不再可信用,缓存必须作废,
        # 重试走 409→重新生成(重算会以 existing_q_norms 排除已入库问法,不双写)。
        # 无选择路径保持旧语义:成功采纳(created/hotwords>0)才作废。
        if has_selection or created > 0 or hotwords_created > 0:
            _plan_cache_pop_account(account_id)
    return {
        "created": created,
        "hotwords_created": hotwords_created,
        "plan": plan,
        "model": str(plan.get("model") or ""),
    }
