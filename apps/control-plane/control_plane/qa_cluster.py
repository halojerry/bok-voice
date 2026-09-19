"""QA 自学习聚类 runner(2026-09-19 W3-T1,防 main.py 膨胀,端点瘦逻辑全在此)。

把 scripts/mine_qa.py --cluster 的 LLM 编排提为 CP 进程内能力:
mine_qa_pairs(与 /api/reports/qa-pairs 同源,进程内调用零重复)→ 按语言分批
问本地 LLM(纯函数在 bok_voice_core.qa_cluster,CLI 与 CP 共用)→ 三列计划
(variant/fresh/junk)→ apply 按 select 逐行盖章入库(与 create_qa_entry 同款:
账号/owner/priority)。

LLM 端点=MLX_LLM_BASE_URL(CP env 面已有,勿用 CLI 的 BOK_LLM_PORT);
模型经 /v1/models 发现(路径型 id、含 4b 优先),env BOK_QA_CLUSTER_MODEL 直覆盖;
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

from bok_voice_core.qa_cluster import (  # noqa: F401  (_CLUSTER_SYSTEM_PROMPT re-export)
    _CLUSTER_SYSTEM_PROMPT,
    build_cluster_messages,
    parse_llm_decisions,
    plan_cluster,
)
from bok_voice_core.qa_text import mine_qa_pairs

from .auth import current_identity

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


def _base_url() -> str:
    return os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1").rstrip("/")


def _discover_model(base_url: str) -> str:
    """取本地 server 已加载模型的真实路径(仓规:request model 必须填真实路径;
    含 4b 优先——4B 是主对话模型,1.8B 是 B 线翻译专才)。"""
    r = httpx.get(f"{base_url}/models", timeout=10)
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


def _llm_chat(base_url: str, model: str, system: str, user: str, *, timeout: float = 120.0) -> str:
    """OpenAI 兼容 /v1/chat/completions(httpx 同步,Summarizer 先例)。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": 4096,
        "stream": False,
    }
    r = httpx.post(f"{base_url}/chat/completions", json=payload, timeout=timeout)
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


def _compute_plan(repo: Any, account_id: str, min_calls: int, limit: int) -> dict:
    """挖掘→分语言 LLM 聚类→三列计划(junk 转 {row,reason} 便于 JSON 出仓)。

    词条清单=账号全量(owner_scope=None 无 owner 过滤,机器/管理口径)——variant
    只拷贝 answer_text,对目标词条无运行时依赖(任务书探针结论)。
    """
    conversations = repo.iter_call_conversations(account_id, exclude_test_objects=True)
    pairs = mine_qa_pairs(conversations, min_calls=min_calls, limit=limit)
    existing_rows = repo.list_qa_entries(account_id, owner_scope=None)
    base_url = _base_url()
    try:
        model = _resolve_model(base_url)
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
            text = _llm_chat(base_url, model, _CLUSTER_SYSTEM_PROMPT, batch["message"])
        except Exception as exc:  # noqa: BLE001 - 网络/解析失败统一 503 带原因
            raise ClusterError(f"llm 请求失败(lang={batch['lang']}): {exc!r}") from exc
        decisions = parse_llm_decisions(text)
        v, f, j = plan_cluster(rows, existing_rows, decisions)
        variants.extend(v)
        fresh.extend(f)
        junk.extend(j)
    return {
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


def run_cluster(repo: Any, account_id: str, min_calls: int = 5, limit: int = 60) -> dict:
    """dry 主入口:缓存命中直接回(不二次 LLM),缺席单飞重算并回填缓存。"""
    hit = _plan_cache_get(account_id, min_calls, limit)
    if hit is not None:
        return hit
    with _SingleFlight():
        # 双检:等锁期间另一请求可能刚算完回填
        hit = _plan_cache_get(account_id, min_calls, limit)
        if hit is not None:
            return hit
        plan = _compute_plan(repo, account_id, min_calls, limit)
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
    *,
    audit: Callable[..., dict] | None = None,
) -> dict:
    """apply 主入口:select 过滤 → 逐行盖章入库 → 审计。

    盖章与 main.create_qa_entry 同款:identity 存在且 role≠root → account 强制
    本账号;role==user → owner 强制本人;priority 钳制。每行审计 qa_entry.create,
    汇总审计 qa.cluster。采纳成功(created>0)后作废该账号 dry 缓存——已入库问法
    下次重算即 dup-existing,不再 offered(防 TTL 内重复采纳双写)。
    """
    sel_v, sel_f = _select_rows(plan, select)
    with _SingleFlight():  # 创建段也单飞:并发 apply 双写 / dry 撞正在落库的计划都 409
        audit_fn = audit or (lambda **kw: {})
        ident = current_identity(request)
        created = 0
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
        audit_fn(
            "qa.cluster",
            subject_type="qa_entry",
            account_id=account_id,
            detail={"variants": len(sel_v), "fresh": len(sel_f), "junk": len(plan.get("junk") or [])},
        )
    if created > 0:
        _plan_cache_pop_account(account_id)
    return {"created": created, "plan": plan, "model": str(plan.get("model") or "")}
