"""QA 自沉淀引擎（CP 闲时循环，2026-09-25）。

人不需要按钮：引擎在 CP 进程内自主跑「挖掘 → 聚类 → 分档采纳 → drift 禁用 →
同音学习 → 补录音」的完整闭环，把 L-①/②/③ 家族的人工确认面升级成带保守闸的
自动面。**默认关**（``BOK_QA_AUTO_DIGEST=1`` 才开）——自动写库必须显式 opt-in。

## 八步管线（每步独立 try/except，一步失败不阻其余，错误汇进 error 列与返回值）

① 闲时闸：``_live_call_count()>0`` → ``{"skipped":"busy"}``——**不落 run 行、
   不推水位**（什么都没处理，水位不得前进）。
② 候选：``mine_qa_pairs`` 过最近通话（水位=上次执行轮的 finished_at；首轮=全部；
   mine 输出天然按问法聚合，重复喂幂等）。产出带 count 的问法行，供 ⑥ 取 miss。
③ 聚类：复用 ``qa_cluster._compute_plan`` 内核（本就 headless——无 Request 依赖、
   不触碰 HTTP dry 缓存，既有端点行为零改动）。LLM 不可用（ClusterError）→
   该轮跳过聚类采纳（④ 无计划可分档），同音/drift 步照跑。
④ 分档采纳：策略层 ``classify_candidates``（bok_voice_core.qa_digest_policy）
   把「聚类计划+挖掘复现计数」折成的候选列表分档 → auto_variant/auto_fresh 入库
   （variant：question=候选原话、answer 继承目标词条、cluster_head_id=目标；
   fresh：answer=挖掘面 AI 实答众数，策略层已过敏感/语言闸）。盖章与
   /api/qa/cluster apply 同款（机器口径：账号=本账号、无 owner、priority 钳制）；
   source='auto-digest'；幂等=find_existing_qa_entry 同 question+lang 已存在即跳过；
   审计逐条 qa_entry.create + 一条 qa.auto_adopt 汇总；采纳成功作废该账号 dry
   计划缓存（与 apply_cluster 同纪律）。
⑤ drift 反馈（2026-09-25 VectorQ 化，语义从「只禁用」升级为每词条阈值）：复用
   ``qa_drift.build_qa_drift_report`` 纯计算（零 SQL 复制）——
   - never_asked（龄≥14d + 终身零命中守卫）/ digits_bypass →
     ``enabled=False``（**禁用可逆，不删**）；never_asked 龄不足不自动；
   - repeat_after_play → **一升二禁**：``hit_threshold`` 步升 +0.03（缺省按
     全局档 0.80 起算，顶格 0.95）；顶格仍 repeat → 才 ``enabled=False``
     （原语义=首犯即禁，现在给词条一次自证机会，保守度只升不降）；
   - 回落：被升过的词条（hit_threshold 非 NULL）窗口内 fired>0（清白命中=
     快答真播出去且没被复问）→ 步降 -0.02，回到全局档 0.80 写 NULL（回全局）。
   审计 qa_entry.update detail.source=auto-digest，阈值动作带 old/new；
   已禁用行不重复；当轮刚升过的词条不参与回落（同窗既有 repeat 证据又衰减
   自相矛盾）。
⑥ 同音表（一期词面 + 二期语义锚定 + 三期语料自聚类）：miss 问法（count≥2，且
   与现有词条词面互不包含=「不沾」）× 现有词条 → 一期 ``mine_homophones``
   （整句距离=1）+ 二期 ``mine_homophones_semantic``（语义同族 cos≥0.75 前提下
   按逐字拼音序列块对齐取「裴/赔」族，sims 由本模块 embed 客户端批量算——
   ``BOK_EMBED_BASE_URL`` 覆盖端点、默认本机 :8789 bge 侧车；**不可达/失败
   → 二期跳过记 run error，一期词面路径照跑**；词条向量按 (id, question)
   进程内缓存）+ 三期 ``mine_homophones_corpus``（matched_corpus=挖掘全量
   问法−misses 归一化对齐去重；孪生句=等长+恰一处同音差异，**方向由命中侧
   给出**——不依赖 embedding 也不依赖词条问法形状）→ 三路按 (wrong,right)
   合并（support 取大）→ support≥2 的对子 UPSERT 进 qa_homophones（support
   取 max(旧,新)）；**source 分轨：三期 corpus 独有的对子 source='corpus'，
   一/二期已产出的对子保持默认 'auto'**（按 key 互斥切分，同 key 恰写一次，
   written 计数不翻倍）。审计 qa.homophone_learn 一条汇总。
⑦ pregen：本轮有采纳 → 复用 ``pregen.qa_pregen_spawn`` 子进程姿势（与
   POST /api/qa/pregen 同一入口）；失败只记 error 不回滚采纳。
⑧ 落 qa_digest_runs 行（各步骤计数；finished_at=下一次挖掘的水位）。

## 策略层联动

``bok_voice_core.qa_digest_policy`` 并行开发：缺席（未合并/依赖缺失）时 ④⑥
优雅跳过（error 记原因），②③⑤⑦⑧ 照跑。策略解析走惰性缓存（首用时 import），
``reset_policy_cache`` 供测试在 sys.modules 注入桩后重判。

## 循环与单飞

``digest_loop``：while True——env 关=空转零成本（每周期只读一次 env）；env 开=
try run_digest_once 后 sleep 间隔。间隔 ``BOK_QA_DIGEST_INTERVAL_S`` 可覆盖
（坏值/非正值回 DIGEST_INTERVAL_S）。单飞=模块级 asyncio.Lock 包住 run 全程
（与 settle 壳同姿势：上一轮没完，后来的串行等待不叠跑）。

## 存储

两表 DDL 在 ``deps.build_engine`` 幂等段（``QA_DIGEST_TABLE_DDL``，方言可移植）；
SQL 会话工厂经 ``bind_storage`` 绑定（main startup 调用，与 bind_routing_storage
同姿势），None（内存仓形态/tests）回落模块级内存表。本模块对 main 只做函数内
延迟 import（campaign 先例——模块级 import 成环）。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from bok_voice_core.qa_text import mine_qa_pairs, normalize_question
from bok_voice_core.testdata import is_test_object_name

from . import gap_mining
from . import pregen as pregen_mod
from . import qa_cluster as qa_cluster_mod
from . import qa_drift

# ---- 常量 ----

AUTO_SOURCE = "auto-digest"  # 引擎采纳行的 source 盖章（运行时匹配不筛 source）

DIGEST_INTERVAL_S = 600.0  # 循环间隔；BOK_QA_DIGEST_INTERVAL_S 可覆盖
MIN_HOMOPHONE_SUPPORT = 2  # 同音对子入库的支持数下限
NEVER_ASKED_MIN_AGE_DAYS = 14  # never_asked 自动禁用的词条龄下限

# ---- VectorQ 每词条自适应阈值（2026-09-25，生产端；消费端=agent qa_gate 逐
# 条目读 entry["hit_threshold"]，NULL=用全局默认）。误差驱动的单调调参：
# 误命中证据（repeat_after_play=播了快答客户又问）单调上调，顶格仍犯才禁用；
# 清白命中（窗口 fired>0 且无 repeat 提案）步降回落，回到全局档写 NULL。
HIT_THRESHOLD_DEFAULT = 0.80  # 全局默认档（QA_SEM 语义补位档）
HIT_THRESHOLD_STEP = 0.03  # repeat_after_play 一次升幅
HIT_THRESHOLD_CAP = 0.95  # 顶格；顶格仍 repeat → 禁用
HIT_THRESHOLD_DECAY = 0.02  # 清白命中一次降幅
HIT_THRESHOLD_FLOOR = 0.80  # 回落下限（=全局档；触底写 NULL）

# ---- 二期语义锚定 embed 客户端（services/bge-embed-sidecar，OpenAI 兼容）----
# POST {base}/v1/embeddings {"input": [texts...]} → {"data":[{"index","embedding"}]}
# GET {base}/health。CP 不 import agent_runtime，httpx 直调；失败/不可达 →
# 二期跳过（一期词面路径照跑），错误记 run error 不炸轮。
ENV_EMBED_BASE_URL = "BOK_EMBED_BASE_URL"
_EMBED_DEFAULT_BASE = "http://127.0.0.1:8789"
_EMBED_TIMEOUT_S = 4.0  # 闲时循环：慢侧车不值得等
_EMBED_MAX_CACHE = 4096  # 词条向量进程内缓存上限（超限整体清空，防无界）

# 聚类挖掘参数（与 HTTP dry 档独立）：min_calls=2 让同音步拿得到 count≥2 的
# miss 问法；limit 与端点同钳 100。
_MIN_CALLS = 2
_LIMIT = 100

# 同音对子文本入库截断（VARCHAR(255) 主键，UTF-8 余量内再截一手）。
_MAX_HOMOPHONE_TEXT = 200

# env 读法（全仓同款 ==，无 truthy 宽容）
ENV_DIGEST = "BOK_QA_AUTO_DIGEST"
ENV_INTERVAL = "BOK_QA_DIGEST_INTERVAL_S"


def digest_enabled() -> bool:
    """引擎总闸：默认关，``BOK_QA_AUTO_DIGEST=1`` 才开（每周期重读，翻转即时生效）。"""
    return os.environ.get(ENV_DIGEST, "") == "1"


def digest_interval_s() -> float:
    """循环间隔解析：坏值/非正值回默认（配错不炸循环也不许 0 值忙转）。"""
    raw = os.environ.get(ENV_INTERVAL, "").strip()
    if not raw:
        return DIGEST_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        return DIGEST_INTERVAL_S
    return value if value > 0 else DIGEST_INTERVAL_S


def _utcnow_iso() -> str:
    """UTC 墙钟 naive ISO 串（与 main._utcnow_iso 同形：落库/字典序比较统一口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


# ---- 策略层惰性解析（并行开发：缺席优雅降级；测试可注入 sys.modules 桩） ----

_policy_cache: dict[str, Any] = {"checked": False, "mod": None}


def reset_policy_cache() -> None:
    """清策略解析缓存（测试在 sys.modules 注入/拔除桩模块后调用重判）。"""
    _policy_cache["checked"] = False
    _policy_cache["mod"] = None


def policy_module() -> Any | None:
    """策略模块惰性解析：首用时 import 一次并缓存；缺席返回 None。"""
    if not _policy_cache["checked"]:
        _policy_cache["checked"] = True
        try:
            from bok_voice_core import qa_digest_policy as mod

            _policy_cache["mod"] = mod
        except Exception:  # noqa: BLE001 - 未合并/依赖缺失都算缺席
            _policy_cache["mod"] = None
    return _policy_cache["mod"]


# ---- 存储：SQL 会话工厂绑定 + 内存回落（bind_routing_storage 同姿势） ----

_STORAGE: dict[str, Any] = {"factory": None}
_MEM_HOMOPHONES: dict[tuple[str, str], dict] = {}
_MEM_RUNS: list[dict] = []


def bind_storage(session_factory: Any) -> None:
    """main startup 调用：绑定与 repo 同 engine 的 session factory（None=内存态）。"""
    _STORAGE["factory"] = session_factory


def upsert_homophones(pairs: list[dict], *, source: str = "auto") -> int:
    """同音对子 UPSERT（support 取 max(旧,新)）→ 实际落库对子数。

    引擎侧双闸：wrong/right 非空且不同、support≥MIN_HOMOPHONE_SUPPORT——策略层
    也应滤，这里兜底（坏对子静默丢弃）。读后写实现（SELECT→INSERT/UPDATE），
    不依赖方言特有 ON CONFLICT，SQLite/PG 同一条代码路径。
    """
    rows: list[dict] = []
    for pair in pairs or []:
        wrong = str((pair or {}).get("wrong") or "").strip()[:_MAX_HOMOPHONE_TEXT]
        right = str((pair or {}).get("right") or "").strip()[:_MAX_HOMOPHONE_TEXT]
        if not wrong or not right or wrong == right:
            continue
        try:
            support = int((pair or {}).get("support") or 0)
        except (TypeError, ValueError):
            continue
        if support < MIN_HOMOPHONE_SUPPORT:
            continue
        rows.append({"wrong": wrong, "right": right, "support": support})
    if not rows:
        return 0

    factory = _STORAGE["factory"]
    if factory is None:
        for row in rows:
            key = (row["wrong"], row["right"])
            old = _MEM_HOMOPHONES.get(key)
            if old is None:
                _MEM_HOMOPHONES[key] = {
                    "wrong": row["wrong"],
                    "right": row["right"],
                    "support": row["support"],
                    "source": source,
                    "created_at": _utcnow_iso(),
                }
            else:
                old["support"] = max(int(old.get("support") or 0), row["support"])
        return len(rows)

    from sqlalchemy import text

    written = 0
    with factory() as session:
        for row in rows:
            found = session.execute(
                text('SELECT support FROM qa_homophones WHERE wrong = :w AND "right" = :r'),
                {"w": row["wrong"], "r": row["right"]},
            ).fetchone()
            if found is None:
                session.execute(
                    text(
                        'INSERT INTO qa_homophones (wrong, "right", support, source, created_at)'
                        " VALUES (:w, :r, :s, :src, :ts)"
                    ),
                    {"w": row["wrong"], "r": row["right"], "s": row["support"],
                     "src": source, "ts": _utcnow_iso()},
                )
            else:
                session.execute(
                    text(
                        'UPDATE qa_homophones SET support = :s'
                        ' WHERE wrong = :w AND "right" = :r'
                    ),
                    {"s": max(int(found[0] or 0), row["support"]),
                     "w": row["wrong"], "r": row["right"]},
                )
            written += 1
        session.commit()
    return written


def list_homophones() -> list[dict]:
    """当前同音对子全表（support 降序）——只读观测面（/api/stats/qa-digest）。"""
    factory = _STORAGE["factory"]
    if factory is None:
        rows = sorted(
            _MEM_HOMOPHONES.values(),
            key=lambda r: (-int(r.get("support") or 0), r["wrong"], r["right"]),
        )
        return [dict(r) for r in rows]

    from sqlalchemy import text

    try:
        with factory() as session:
            found = session.execute(
                text(
                    'SELECT wrong, "right", support, source, created_at FROM qa_homophones'
                    " ORDER BY support DESC, wrong, \"right\""
                )
            ).fetchall()
    except Exception:  # noqa: BLE001 - 观测面读失败回空表不炸端点
        return []
    return [
        {"wrong": r[0], "right": r[1], "support": int(r[2] or 0),
         "source": r[3], "created_at": r[4]}
        for r in found
    ]


def insert_run(row: dict) -> None:
    """落一轮 qa_digest_runs 行（id=uuid TEXT，仓库现有表风格）。"""
    record = {
        "id": str(row.get("id") or f"qadigest:{uuid.uuid4().hex[:12]}"),
        "started_at": str(row.get("started_at") or ""),
        "finished_at": str(row.get("finished_at") or ""),
        "adopted_variant": int(row.get("adopted_variant") or 0),
        "adopted_fresh": int(row.get("adopted_fresh") or 0),
        "disabled": int(row.get("disabled") or 0),
        "homophones": int(row.get("homophones") or 0),
        "pregen": int(row.get("pregen") or 0),
        "error": str(row.get("error") or ""),
    }
    factory = _STORAGE["factory"]
    if factory is None:
        _MEM_RUNS.append(record)
        return

    from sqlalchemy import text

    with factory() as session:
        session.execute(
            text(
                "INSERT INTO qa_digest_runs (id, started_at, finished_at, adopted_variant,"
                " adopted_fresh, disabled, homophones, pregen, error)"
                " VALUES (:id, :started_at, :finished_at, :adopted_variant,"
                " :adopted_fresh, :disabled, :homophones, :pregen, :error)"
            ),
            record,
        )
        session.commit()


def list_runs(limit: int = 20) -> list[dict]:
    """最近 N 轮 run 行（started_at 降序）——只读观测面。"""
    factory = _STORAGE["factory"]
    if factory is None:
        ordered = sorted(_MEM_RUNS, key=lambda r: str(r.get("started_at") or ""), reverse=True)
        return [dict(r) for r in ordered[: max(1, int(limit))]]

    from sqlalchemy import text

    try:
        with factory() as session:
            found = session.execute(
                text(
                    "SELECT id, started_at, finished_at, adopted_variant, adopted_fresh,"
                    " disabled, homophones, pregen, error FROM qa_digest_runs"
                    " ORDER BY started_at DESC LIMIT :lim"
                ),
                {"lim": max(1, int(limit))},
            ).fetchall()
    except Exception:  # noqa: BLE001 - 观测面读失败回空表不炸端点
        return []
    keys = ("id", "started_at", "finished_at", "adopted_variant", "adopted_fresh",
            "disabled", "homophones", "pregen", "error")
    return [dict(zip(keys, row)) for row in found]


def last_watermark() -> str:
    """水位=最近一次执行轮的 finished_at（无任何 run 时空串=首轮全量）。"""
    factory = _STORAGE["factory"]
    if factory is None:
        finished = [str(r.get("finished_at") or "") for r in _MEM_RUNS]
        finished = [t for t in finished if t]
        return max(finished) if finished else ""

    from sqlalchemy import text

    try:
        with factory() as session:
            row = session.execute(
                text(
                    "SELECT finished_at FROM qa_digest_runs"
                    " WHERE finished_at <> '' ORDER BY started_at DESC LIMIT 1"
                )
            ).fetchone()
    except Exception:  # noqa: BLE001 - 水位读失败退回全量（mine 幂等兜底）
        return ""
    return str(row[0] or "") if row else ""


# ---- main 反查（函数内延迟 import：模块级会成环，campaign 先例） ----


def _default_repo() -> Any:
    from .main import _repo

    return _repo()


def _default_audit() -> Callable[..., dict]:
    from .main import _audit

    return _audit


def _default_live_count() -> int:
    from .main import _live_call_count

    return _live_call_count()


def _default_base_url() -> str:
    """pregen 子进程回连 CP 的自地址：显式参 > 公网注入 env > 本机回环缺省。"""
    explicit = os.environ.get("BOK_CP_PUBLIC_URL", "").strip()
    return (explicit or "http://127.0.0.1:8000").rstrip("/")


# ---- ② 候选挖掘（水位过滤；测试对象/无对象通话滤与 iter_call_conversations 同款） ----


def _mine_recent_conversations(repo: Any, account_id: str, watermark: str) -> list[list[dict]]:
    """窗口内通话 → mine_qa_pairs 输入形（[[{role,text,lang},...],...]）。

    过滤三件与 iter_call_conversations 同口径：测试对象前缀族、无对象通话、
    created_at≤水位的旧通话（水位空=不滤）。逐通内按 created_at 排序。
    """
    calls = repo.list_calls(account_id)
    try:
        obj_names = {
            str(o.get("id") or ""): o.get("display_name")
            for o in (repo.list_objects(account_id) or [])
        }
    except Exception:  # noqa: BLE001 - 对象面不可读只损测试过滤
        obj_names = {}
    conversations: list[list[dict]] = []
    for call in calls:
        name = obj_names.get(str(call.get("object_id") or ""))
        if name is None or is_test_object_name(name):
            continue
        if watermark and str(call.get("created_at") or "") <= watermark:
            continue
        try:
            turns = repo.get_turns(str(call.get("id") or ""))
        except Exception:  # noqa: BLE001 - 单通取数失败不炸整轮
            continue
        ordered = sorted(turns, key=lambda t: str(getattr(t, "created_at", "") or ""))
        conversations.append(
            [
                {
                    "role": str(getattr(t, "role", "") or ""),
                    "text": str(getattr(t, "transcript", "") or ""),
                    "lang": str(getattr(t, "language", "") or ""),
                }
                for t in ordered
            ]
        )
    return conversations


def _digest_accounts(repo: Any) -> list[str]:
    """引擎巡检的账号集：qa_entries ∪ calls 的去重账号（空库=空表→本轮空转）。"""
    accounts: set[str] = set()
    try:
        accounts |= {
            str(e.get("account_id") or "")
            for e in (repo.list_qa_entries("", owner_scope=None) or [])
        }
    except Exception:  # noqa: BLE001 - 单面失败不阻另一面
        pass
    try:
        accounts |= {str(c.get("account_id") or "") for c in (repo.list_calls("") or [])}
    except Exception:  # noqa: BLE001
        pass
    accounts.discard("")
    return sorted(accounts)


def _is_miss(question_norm: str, entry_norms: set[str]) -> bool:
    """候选问法是否「与现有词条不沾」：词面相等/互相包含都算沾（保守少学）。"""
    for norm in entry_norms:
        if not norm:
            continue
        if norm == question_norm or norm in question_norm or question_norm in norm:
            return False
    return True


def _entry_age_days(row: dict, now: datetime | None = None) -> float:
    """词条龄（天）：created_at 解析失败/缺失按 0 处理——never_asked 自动禁用
    需要「确定够老」，证据不足时保守不动。"""
    raw = str((row or {}).get("created_at") or "").strip()
    if not raw:
        return 0.0
    try:
        created = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - created).total_seconds() / 86400.0)


# ---- ⑥ 二期 embed 客户端（bge 侧车 :8789，OpenAI 兼容 /v1/embeddings）----


def embed_base_url() -> str:
    """embed 侧车基址：``BOK_EMBED_BASE_URL`` 覆盖，缺省本机 :8789。"""
    raw = os.environ.get(ENV_EMBED_BASE_URL, "").strip()
    return (raw or _EMBED_DEFAULT_BASE).rstrip("/")


def _embed_vectors(
    texts: list[str], *, base_url: str = "", timeout: float = _EMBED_TIMEOUT_S
) -> list[list[float]] | None:
    """批量取向量（对齐入参序）；任何失败/不可达/形状不对 → None（不抛）。

    侧车响应 OpenAI 兼容：``{"data": [{"index": i, "embedding": [...]}]}``；
    逐条按 index 回填，缺位/空向量都算失败（宁可跳过二期不出错对子）。
    测试经 monkeypatch 本函数桩化（零网络）。
    """
    clean = [str(t or "") for t in (texts or [])]
    if not clean:
        return []
    import httpx

    try:
        resp = httpx.post(
            f"{(base_url or embed_base_url())}/v1/embeddings",
            json={"input": clean},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
    except Exception:  # noqa: BLE001 - 不可达/超时/坏 JSON 统一按缺席处理
        return None
    vecs: list[list[float] | None] = [None] * len(clean)
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index", -1))
        except (TypeError, ValueError):
            continue
        vec = item.get("embedding")
        if 0 <= idx < len(clean) and isinstance(vec, list) and vec:
            try:
                vecs[idx] = [float(x) for x in vec]
            except (TypeError, ValueError):
                vecs[idx] = None
    if any(v is None for v in vecs):
        return None
    return [v for v in vecs if v is not None]


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（零向量按 0 处理——不相似，绝不做 NaN）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


# 词条向量进程内缓存（键=(id, question_text)——问题改了键就变，天然失效）。
_ENTRY_VEC_CACHE: dict[tuple[str, str], list[float]] = {}


def reset_embed_cache() -> None:
    """清词条向量缓存（测试隔离用；生产进程内常驻，容量封顶整体清空）。"""
    _ENTRY_VEC_CACHE.clear()


def _entry_vectors(
    entries: list[dict],
) -> dict[tuple[str, str], list[float]] | None:
    """enabled 词条 → {(id, question_text): 向量}；embed 不可达 → None。

    缓存命中不重复请求；miss 的键批量取向量，**失败值不进缓存**（下轮重试，
    不把「侧车临时挂了」钉死成永久缺席）。
    """
    wanted: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for e in entries or []:
        key = (str((e or {}).get("id") or ""), str((e or {}).get("question_text") or ""))
        if key[0] and key[1] and key not in seen:
            seen.add(key)
            wanted.append(key)
    missing = [k for k in wanted if k not in _ENTRY_VEC_CACHE]
    if missing:
        vecs = _embed_vectors([k[1] for k in missing])
        if vecs is None:
            return None
        for key, vec in zip(missing, vecs):
            if len(_ENTRY_VEC_CACHE) >= _EMBED_MAX_CACHE:
                _ENTRY_VEC_CACHE.clear()
            _ENTRY_VEC_CACHE[key] = vec
    return {k: _ENTRY_VEC_CACHE[k] for k in wanted}


def _mine_semantic_homophones(
    policy: Any, misses: list[dict], entries: list[dict]
) -> tuple[list[dict], str]:
    """二期语义锚定挖掘 → (对子, error)。零网络失败面：embed 不可达/策略层
    无此函数（旧版并行开发兼容）→ ([], 因)——一期词面路径照跑。"""
    harvest = getattr(policy, "mine_homophones_semantic", None)
    if not callable(harvest):
        return [], ""
    miss_qs: list[str] = []
    for m in misses or []:
        q = str((m or {}).get("question") or "")
        if q and q not in miss_qs:
            miss_qs.append(q)
    if not miss_qs:
        return [], ""
    entry_vecs = _entry_vectors(entries)
    if entry_vecs is None:
        return [], "embed unavailable"
    miss_vecs = _embed_vectors(miss_qs)
    if miss_vecs is None:
        return [], "embed unavailable"
    q2vec = dict(zip(miss_qs, miss_vecs))
    sims: dict[tuple[str, str], float] = {}
    for m in misses or []:
        raw_q = str((m or {}).get("question") or "")
        mv = q2vec.get(raw_q)
        if not mv:
            continue
        for e in entries or []:
            eid = str((e or {}).get("id") or "")
            key = (eid, str((e or {}).get("question_text") or ""))
            ev = entry_vecs.get(key)
            if eid and ev:
                sims[(raw_q, eid)] = _cosine(mv, ev)
    try:
        return list(harvest(misses, entries, sims) or []), ""
    except Exception as exc:  # noqa: BLE001 - 策略层异常降级为跳过不炸轮
        return [], repr(exc)


def _mine_corpus_homophones(
    policy: Any, misses: list[dict], matched_corpus: list[dict]
) -> tuple[list[dict], str]:
    """三期语料自聚类挖掘 → (对子, error)。策略层无此函数（旧版并行开发
    兼容）→ ([], "")——同二期姿势，一期/二期路径照跑不炸轮。"""
    harvest = getattr(policy, "mine_homophones_corpus", None)
    if not callable(harvest):
        return [], ""
    try:
        return list(harvest(misses, matched_corpus) or []), ""
    except Exception as exc:  # noqa: BLE001 - 策略层异常降级为跳过不炸轮
        return [], repr(exc)


def _matched_corpus_rows(
    candidates: list[dict], account_id: str, misses: list[dict]
) -> list[dict]:
    """三期 matched 语料：本账号挖掘全量问法 − misses（归一化对齐去重）。

    mine_qa_pairs 的问法键本就是归一形，这里再过一遍 ``normalize_question``
    对齐（幂等）；misses 的归一形从 matched 侧剔除——matched=「方向证据源」，
    只要不是 miss 就收（词面沾词条的高频问法天然在列，count 不设门槛）。
    同归一形去重保留首个（mine 输出按 (-calls, question) 排序，首个=计数
    最高的行）。
    """
    miss_norms = {
        normalize_question(str(m.get("question") or "")) for m in misses or []
    }
    seen: set[str] = set()
    out: list[dict] = []
    for row in candidates or []:
        if row.get("account_id") != account_id:
            continue
        q = str(row.get("question") or "")
        norm = normalize_question(q)
        if not norm or norm in miss_norms or norm in seen:
            continue
        seen.add(norm)
        out.append({"question": q, "count": int(row.get("calls") or 0)})
    return out


def _merge_homophone_pairs(*groups: list[dict]) -> list[dict]:
    """多路挖掘结果按 (wrong,right) 合并：support 取大，example 取先到。

    一期/二期常对同一 (wrong,right) 各出一条（「钟/仲」距离=1 与拼音对齐
    双路都命中）——不去重会让 UPSERT written 计数翻倍、审计虚高。输出按
    (-support, wrong, right) 排序保证确定性。
    """
    merged: dict[tuple[str, str], dict] = {}
    for group in groups:
        for pair in group or []:
            if not isinstance(pair, dict):
                continue
            key = (str(pair.get("wrong") or ""), str(pair.get("right") or ""))
            if not key[0] or not key[1]:
                continue
            try:
                sup = int(pair.get("support") or 0)
            except (TypeError, ValueError):
                sup = 0
            cur = merged.get(key)
            if cur is None:
                merged[key] = {
                    "wrong": key[0],
                    "right": key[1],
                    "support": sup,
                    "example": str(pair.get("example") or ""),
                }
            else:
                cur["support"] = max(cur["support"], sup)
    out = sorted(
        merged.values(), key=lambda d: (-d["support"], d["wrong"], d["right"])
    )
    return out


# ---- 主入口 ----

# 单飞锁（loop-aware）：模块级锁但按当前事件循环惰性（重）建——CP 服务全程单
# loop 天然复用；tests 经 asyncio.run 每次新 loop，裸 asyncio.Lock 会在第二个
# loop 上抛「bound to a different event loop」。check-then-set 之间无 await，
# 单线程事件循环内天然原子（与 settle 锁 setdefault 同款纪律）。
_DIGEST_LOCK: asyncio.Lock | None = None
_DIGEST_LOCK_LOOP: asyncio.AbstractEventLoop | None = None


def _digest_lock() -> asyncio.Lock:
    global _DIGEST_LOCK, _DIGEST_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _DIGEST_LOCK is None or _DIGEST_LOCK_LOOP is not loop:
        _DIGEST_LOCK = asyncio.Lock()
        _DIGEST_LOCK_LOOP = loop
    return _DIGEST_LOCK


_TASKS: set[asyncio.Task] = set()


async def run_digest_once(
    *,
    repo: Any | None = None,
    audit: Callable[..., dict] | None = None,
    live_count: Callable[[], int] | None = None,
    base_url: str = "",
) -> dict:
    """单轮沉淀（八步管线）。repo/audit/live_count 可注入（tests 离线直调），
    缺省反查 main。返回各步骤计数+错误清单；busy 跳过轮不落 run 行不推水位。"""
    async with _digest_lock():  # 单飞：循环与外部调用上一轮没完不叠
        return await _run_once(
            repo=repo, audit=audit, live_count=live_count, base_url=base_url
        )


async def _run_once(
    *,
    repo: Any | None,
    audit: Callable[..., dict] | None,
    live_count: Callable[[], int] | None,
    base_url: str,
) -> dict:
    started = _utcnow_iso()
    out: dict[str, Any] = {
        "started_at": started,
        "skipped": "",
        "accounts": [],
        "candidates": 0,
        "plan": {},
        "adopted_variant": 0,
        "adopted_fresh": 0,
        "pending": 0,
        "disabled": 0,
        "threshold_raised": 0,
        "threshold_decayed": 0,
        "homophones": 0,
        "pregen": "",
        "errors": [],
    }

    # ① 闲时闸：有活通话（含振铃）让路——挖掘/LLM/写库都不与通话争抢。
    try:
        live = (live_count or _default_live_count)()
    except Exception as exc:  # noqa: BLE001 - 闸失联按忙处理（宁可不跑）
        out["skipped"] = f"busy({exc!r})"
        return out
    if live > 0:
        out["skipped"] = "busy"
        return out

    repo = repo if repo is not None else _default_repo()
    audit_fn = audit or _default_audit()
    errors: list[str] = out["errors"]

    # ② 候选：水位过滤挖掘，逐账号带 count 的问法行。
    accounts = _digest_accounts(repo)
    out["accounts"] = accounts
    if not accounts:
        out["skipped"] = "empty"  # 空库：不落 run 行（防空闲安装无限攒 run 行）
        return out
    watermark = last_watermark()
    candidates: list[dict] = []
    for acc in accounts:
        try:
            pairs = mine_qa_pairs(
                _mine_recent_conversations(repo, acc, watermark),
                min_calls=_MIN_CALLS,
                limit=_LIMIT,
            )
            candidates.extend([{"account_id": acc, **row} for row in pairs])
        except Exception as exc:  # noqa: BLE001 - 步骤隔离
            errors.append(f"mine({acc}): {exc!r}")
    out["candidates"] = len(candidates)

    # ③ 聚类：复用 qa_cluster 内核（headless）；LLM 不可用→本轮跳过聚类采纳。
    # 中途闲时复查（2026-09-28）：①的闸只在 run 起点看一次——挖掘/写库耗时窗内
    # 来了通话，聚类 LLM 是 :1235 单闸的最长持有者（实测一次 digest 距真实通话
    # 开跑仅 0.85s），顶住实时回复=通话侧 TTFT 抖刺。失联按忙处理（宁可不跑）。
    try:
        if (live_count or _default_live_count)() > 0:
            out["skipped"] = "busy-midway"
            return out
    except Exception as exc:  # noqa: BLE001
        out["skipped"] = f"busy({exc!r})"
        return out
    plans: dict[str, dict] = {}
    for acc in accounts:
        try:
            if (live_count or _default_live_count)() > 0:
                out["skipped"] = "busy-midway"
                return out
            # 下线程(2026-10-02 审计修):_compute_plan 内含 httpx.post(timeout=120)
            # 的同步 LLM 调用——直跑在事件循环上=整个 CP 停摆(/health/turns 上报/
            # token/挂断/webhook 全冻;settle 已修同款「/health 59.4s 停摆」);闲时门
            # 只在调用前后查活通话,不救循环本身。
            plans[acc] = await asyncio.to_thread(
                qa_cluster_mod._compute_plan, repo, acc, _MIN_CALLS, _LIMIT
            )
        except Exception as exc:  # noqa: BLE001 - ClusterError(LLM 不可用)等一并列错误
            errors.append(f"cluster({acc}): {exc!r}")
    out["plan"] = {
        "variants": sum(len(p.get("variants") or []) for p in plans.values()),
        "fresh": sum(len(p.get("fresh") or []) for p in plans.values()),
        "junk": sum(len(p.get("junk") or []) for p in plans.values()),
    }

    # ④ 分档采纳（策略层缺席→跳过记因）：plan+挖掘计数 → 候选形状 → 分档 → 入库。
    adopted_ids: list[str] = []
    policy = policy_module()
    for acc, plan in plans.items():
        if policy is None:
            errors.append("adopt: policy module missing")
            break
        try:
            buckets = policy.classify_candidates(
                _plan_to_candidates(acc, plan, candidates)
            )
            n_v = _adopt_variants(
                repo, audit_fn, acc, buckets.get("auto_variant") or [], adopted_ids
            )
            n_f = _adopt_fresh(
                repo, audit_fn, acc, buckets.get("auto_fresh") or [], adopted_ids
            )
            out["pending"] += len(buckets.get("pending") or [])
            out["adopted_variant"] += n_v
            out["adopted_fresh"] += n_f
            if n_v + n_f > 0:
                audit_fn(
                    "qa.auto_adopt",
                    subject_type="qa_entry",
                    account_id=acc,
                    detail={
                        "variant": n_v,
                        "fresh": n_f,
                        "ids": [i for i in adopted_ids],
                        "source": AUTO_SOURCE,
                    },
                )
                # 采纳成功作废该账号 dry 计划缓存（apply_cluster 同纪律）。
                qa_cluster_mod._plan_cache_pop_account(acc)
        except Exception as exc:  # noqa: BLE001 - 步骤隔离
            errors.append(f"adopt({acc}): {exc!r}")

    # ⑤ drift 反馈（VectorQ 一升二禁 + 清白回落；never_asked/digits 仍直接禁用）。
    for acc in accounts:
        try:
            report = qa_drift.build_qa_drift_report(
                repo, account_id=acc, include_fired=True
            )
            raised_ids: set[str] = set()
            for proposal in report.get("proposals") or []:
                reason = str(proposal.get("reason") or "")
                if reason == qa_drift.RS_NEVER_FIRED:
                    continue  # 缺录音/被抢出场两成因分不清，不自动
                if reason not in (
                    qa_drift.RS_NEVER_ASKED,
                    qa_drift.RS_DIGITS_BYPASS,
                    qa_drift.RS_REPEAT_AFTER_PLAY,
                ):
                    continue
                qa_id = str(proposal.get("qa_id") or "")
                row = repo.get_qa_entry(qa_id)
                if not row or not row.get("enabled"):
                    continue  # 已禁用/已删：幂等不重复
                if reason == qa_drift.RS_REPEAT_AFTER_PLAY:
                    # VectorQ 一升二禁：首犯=该词条在当前阈值下仍有误命中证据
                    # → 阈值步升（给一次自证机会）；顶格 0.95 仍 repeat → 禁用
                    # （原语义=首犯即禁；保守度只升不降——禁用仍可逆不删）。
                    try:
                        _old = row.get("hit_threshold")
                        _base = HIT_THRESHOLD_DEFAULT if _old is None else float(_old)
                    except (TypeError, ValueError):
                        _base = HIT_THRESHOLD_DEFAULT
                    if _base >= HIT_THRESHOLD_CAP - 1e-9:
                        repo.update_qa_entry(qa_id, {"enabled": False})
                        audit_fn(
                            "qa_entry.update",
                            subject_type="qa_entry",
                            subject_id=qa_id,
                            account_id=acc,
                            detail={
                                "source": AUTO_SOURCE,
                                "reason": reason,
                                "enabled": False,
                                "question": str(proposal.get("question_text") or "")[:60],
                                "hit_threshold": {"old": _base, "new": _base},
                            },
                        )
                        out["disabled"] += 1
                        continue
                    _new = round(min(HIT_THRESHOLD_CAP, _base + HIT_THRESHOLD_STEP), 6)
                    repo.update_qa_entry(qa_id, {"hit_threshold": _new})
                    audit_fn(
                        "qa_entry.update",
                        subject_type="qa_entry",
                        subject_id=qa_id,
                        account_id=acc,
                        detail={
                            "source": AUTO_SOURCE,
                            "reason": reason,
                            "question": str(proposal.get("question_text") or "")[:60],
                            "hit_threshold": {"old": _base, "new": _new},
                        },
                    )
                    raised_ids.add(qa_id)
                    out["threshold_raised"] += 1
                    continue
                if reason == qa_drift.RS_NEVER_ASKED:
                    if _entry_age_days(row) < NEVER_ASKED_MIN_AGE_DAYS:
                        continue  # 龄不足：可能只是窗口小，不自动
                    # 终身命中守卫(2026-09-25 首轮实弹补):窗口 occ==0 ≠ 没用——
                    # 实弹把终身 hit=17/5/3 的词条按「近窗没被问」禁了。命中过
                    # 一次就是被证明有用的问法,自动退休只碰零终身命中的。
                    try:
                        _lifetime_hits = int(row.get("hit_count") or 0)
                    except (TypeError, ValueError):
                        _lifetime_hits = 0
                    if _lifetime_hits > 0:
                        continue
                repo.update_qa_entry(qa_id, {"enabled": False})
                audit_fn(
                    "qa_entry.update",
                    subject_type="qa_entry",
                    subject_id=qa_id,
                    account_id=acc,
                    detail={
                        "source": AUTO_SOURCE,
                        "reason": reason,
                        "enabled": False,
                        "question": str(proposal.get("question_text") or "")[:60],
                    },
                )
                out["disabled"] += 1
            # 回落 pass：被升过的词条（hit_threshold 非 NULL）本窗 fired>0
            # （清白命中=快答真播出去且没被复问）→ 步降；回到全局档写 NULL。
            # 当轮刚升过的词条跳过——同窗既有 repeat 证据又衰减自相矛盾；
            # never_asked/digits 禁用行已不在 enabled 列表，天然不参与。
            fired_by_norm = report.get("fired_by_norm") or {}
            for entry in repo.list_qa_entries(acc, enabled=True, owner_scope=None) or []:
                eid = str(entry.get("id") or "")
                ht = entry.get("hit_threshold")
                if not eid or ht is None or eid in raised_ids:
                    continue
                try:
                    _base = float(ht)
                except (TypeError, ValueError):
                    continue
                if _base <= HIT_THRESHOLD_FLOOR + 1e-9:
                    continue  # 已在全局档（残留数据），无可回落
                try:
                    _fired = int(fired_by_norm.get(qa_drift.qa_norm(entry), 0) or 0)
                except (TypeError, ValueError):
                    _fired = 0
                if _fired <= 0:
                    continue
                _new = round(_base - HIT_THRESHOLD_DECAY, 6)
                _back = _new <= HIT_THRESHOLD_FLOOR + 1e-9
                repo.update_qa_entry(eid, {"hit_threshold": None if _back else _new})
                audit_fn(
                    "qa_entry.update",
                    subject_type="qa_entry",
                    subject_id=eid,
                    account_id=acc,
                    detail={
                        "source": AUTO_SOURCE,
                        "reason": "clean_hit_decay",
                        "fired": _fired,
                        "hit_threshold": {"old": _base, "new": None if _back else _new},
                    },
                )
                out["threshold_decayed"] += 1
        except Exception as exc:  # noqa: BLE001 - 步骤隔离
            errors.append(f"drift({acc}): {exc!r}")

    # ⑥ 同音学习（策略层缺席→跳过）：miss×词条 → 一期词面 + 二期语义锚定
    # + 三期语料自聚类，三路合并（support 取大）后 support≥2 对子 UPSERT。
    if policy is not None:
        for acc in accounts:
            try:
                entries = [
                    e
                    for e in (repo.list_qa_entries(acc, owner_scope=None) or [])
                    if e.get("enabled")
                ]
                if not entries:
                    continue
                entry_norms = {
                    normalize_question(str(e.get("question_text") or "")) for e in entries
                }
                # miss=未命中任何词条且复现≥2 的问法；策略层只吃 {question, count}。
                misses = [
                    {"question": str(row.get("question") or ""), "count": int(row.get("calls") or 0)}
                    for row in candidates
                    if row.get("account_id") == acc
                    and int(row.get("calls") or 0) >= MIN_HOMOPHONE_SUPPORT
                    and _is_miss(str(row.get("question") or ""), entry_norms)
                ]
                if not misses:
                    continue
                mined = policy.mine_homophones(misses, entries)
                # 二期语义锚定：embed 侧车批量算 sims（词条向量进程内缓存）；
                # 不可达/失败 → 跳过记 error，一期词面路径照跑不炸轮。
                sem_pairs, sem_err = _mine_semantic_homophones(policy, misses, entries)
                if sem_err:
                    errors.append(f"homophone-semantic({acc}): {sem_err}")
                # 三期语料自聚类：matched_corpus=挖掘全量问法−misses（归一化
                # 对齐去重）；孪生句对方向由命中侧给出。策略层旧版无此函数
                # → 跳过不炸（同二期姿势）。
                matched_corpus = _matched_corpus_rows(candidates, acc, misses)
                corpus_pairs, corpus_err = _mine_corpus_homophones(
                    policy, misses, matched_corpus
                )
                if corpus_err:
                    errors.append(f"homophone-corpus({acc}): {corpus_err}")
                # 三路合并（support 取大）；source 分轨——三期 corpus 独有的
                # 对子盖 'corpus' 章，一/二期已产出的对子保持默认 'auto'
                # （按 key 互斥切分，同 key 恰写一次，written 计数不翻倍）。
                leg12 = _merge_homophone_pairs(mined, sem_pairs)
                merged = _merge_homophone_pairs(leg12, corpus_pairs)
                corpus_only_keys = {
                    (str(p.get("wrong") or ""), str(p.get("right") or ""))
                    for p in corpus_pairs
                } - {
                    (str(p.get("wrong") or ""), str(p.get("right") or ""))
                    for p in leg12
                }
                auto_rows: list[dict] = []
                corpus_rows: list[dict] = []
                for pair in merged:
                    key = (str(pair.get("wrong") or ""), str(pair.get("right") or ""))
                    (corpus_rows if key in corpus_only_keys else auto_rows).append(pair)
                written = upsert_homophones(auto_rows)
                written += upsert_homophones(corpus_rows, source="corpus")
                items = [
                    {"wrong": str(p.get("wrong") or "")[:60],
                     "right": str(p.get("right") or "")[:60],
                     "support": int(p.get("support") or 0)}
                    for p in (merged or [])
                ]
                if written:
                    audit_fn(
                        "qa.homophone_learn",
                        subject_type="qa_homophone",
                        account_id=acc,
                        detail={"pairs": written, "items": items[:20]},
                    )
                out["homophones"] += written
            except Exception as exc:  # noqa: BLE001 - 步骤隔离
                errors.append(f"homophone({acc}): {exc!r}")
    elif adopted_ids or plans:
        errors.append("homophone: policy module missing")

    # ⑦ pregen：本轮有采纳才触发（与 POST /api/qa/pregen 同一入口/子进程姿势）；
    # 失败只记 error，绝不回滚已采纳的词条。
    if adopted_ids:
        try:
            spawn = pregen_mod.qa_pregen_spawn(
                (base_url or _default_base_url()).rstrip("/"), adopted_ids
            )
            out["pregen"] = str(spawn.get("status") or "")
        except Exception as exc:  # noqa: BLE001 - 物化失败不回滚采纳
            errors.append(f"pregen: {exc!r}")

    # ⑧ 落 run 行（审计与水位共用；finished_at=下一次挖掘的水位）。
    out["finished_at"] = _utcnow_iso()
    try:
        insert_run(
            {
                "started_at": started,
                "finished_at": out["finished_at"],
                "adopted_variant": out["adopted_variant"],
                "adopted_fresh": out["adopted_fresh"],
                "disabled": out["disabled"],
                "homophones": out["homophones"],
                "pregen": len(adopted_ids) if str(out["pregen"]) == "queued" else 0,
                "error": "; ".join(errors)[:500],
            }
        )
    except Exception as exc:  # noqa: BLE001 - 记账失败不影响本轮已落库成果
        errors.append(f"record: {exc!r}")
    out["errors"] = errors
    return out


def _plan_to_candidates(account_id: str, plan: dict, mined: list[dict]) -> list[dict]:
    """qa_cluster 计划+挖掘计数 → 策略层候选形状（classify_candidates 输入契约）。

    形状：{question, lang, count, cluster_verdict, target_entry_id,
    sample_answer, sample_lang}。variant 行的 question_text 与 mine 行的
    question 同一归一源（normalize_question），(question, lang) 精确配对取
    count；fresh 行本就是 mine 行（count=calls 直接带）。junk 不喂（策略层
    也只会 skip 它）。额外的 account_id 键策略层不读，回程定位账号用。
    """
    by_q: dict[tuple[str, str], dict] = {
        (str(r.get("question") or ""), str(r.get("lang") or "")): r
        for r in mined or []
        if r.get("account_id") == account_id
    }
    out: list[dict] = []
    for v in plan.get("variants") or []:
        question = str(v.get("question_text") or "")
        lang = str(v.get("lang") or "zh")
        row = by_q.get((question, lang)) or {}
        out.append({
            "account_id": account_id,
            "question": question,
            "lang": lang,
            "count": int(row.get("calls") or 0),
            "cluster_verdict": "variant",
            "target_entry_id": str(v.get("cluster_head_id") or ""),
            "sample_answer": str(v.get("answer_text") or ""),
            "sample_lang": lang,
        })
    for f in plan.get("fresh") or []:
        question = str(f.get("question") or "")
        lang = str(f.get("lang") or "zh")
        out.append({
            "account_id": account_id,
            "question": question,
            "lang": lang,
            "count": int(f.get("calls") or 0),
            "cluster_verdict": "fresh",
            "target_entry_id": "",
            "sample_answer": str(f.get("answer") or ""),
            "sample_lang": lang,
        })
    return out


def _audit_create(
    audit_fn: Callable[..., dict],
    account_id: str,
    entry: dict,
    kind: str,
) -> None:
    """qa_entry.create 审计（与 /api/qa/cluster apply 同款形状 + source 位）。"""
    audit_fn(
        "qa_entry.create",
        subject_type="qa_entry",
        subject_id=str(entry.get("id") or ""),
        account_id=account_id,
        detail={
            "owner_user_id": str(entry.get("owner_user_id") or ""),
            "kind": kind,
            "source": AUTO_SOURCE,
        },
    )


def _adopt_variants(
    repo: Any,
    audit_fn: Callable[..., dict],
    account_id: str,
    cands: list[dict],
    adopted_ids: list[str],
) -> int:
    """auto_variant 批入库（与 /api/qa/cluster apply 的 variant 同款语义）→ 条数。

    question=候选原话（真实措辞即匹配面）、answer=继承目标词条 answer_text
    （答案以既有为准防漂移）、cluster_head_id=目标词条 id。幂等：find_existing
    同 question+lang 已在库即跳过。目标词条丢失/答案空=不落坏词条。
    """
    created = 0
    for cand in cands or []:
        cand = cand if isinstance(cand, dict) else {}
        question = str(cand.get("question") or "").strip()
        lang = str(cand.get("lang") or "zh")
        target_id = str(cand.get("target_entry_id") or "")
        if not question or not target_id:
            continue
        if gap_mining.find_existing_qa_entry(repo, account_id, question, lang):
            continue  # 幂等：同 question+lang 已在库
        target = repo.get_qa_entry(target_id) or {}
        answer = str(target.get("answer_text") or "").strip()
        if not answer:
            continue  # 目标词条答案空：不继承空答案（宁缺勿错）
        payload = {
            "question_text": question,
            "answer_text": answer,
            "lang": lang,
            "scope": "global",
            "source": AUTO_SOURCE,
            "enabled": True,
            "account_id": account_id,
            "cluster_head_id": target_id,
            "priority": qa_cluster_mod._clamp_priority(None),
        }
        entry = repo.create_qa_entry(payload)
        created += 1
        adopted_ids.append(str(entry.get("id") or ""))
        _audit_create(audit_fn, account_id, entry, "auto_variant")
    return created


def _adopt_fresh(
    repo: Any,
    audit_fn: Callable[..., dict],
    account_id: str,
    cands: list[dict],
    adopted_ids: list[str],
) -> int:
    """auto_fresh 批入库（复用 qa_cluster._fresh_payload 写路径）→ 条数。

    answer=挖掘面 AI 当时实答众数（策略层已过敏感闸+语言一致闸）；
    source 覆写为 auto-digest。幂等同 variant。
    """
    created = 0
    for cand in cands or []:
        cand = cand if isinstance(cand, dict) else {}
        question = str(cand.get("question") or "").strip()
        lang = str(cand.get("lang") or "zh")
        if not question:
            continue
        if gap_mining.find_existing_qa_entry(repo, account_id, question, lang):
            continue  # 幂等：同 question+lang 已在库
        payload = qa_cluster_mod._fresh_payload(
            {
                "question": question,
                "answer": str(cand.get("sample_answer") or ""),
                "lang": lang,
            },
            account_id,
        )
        payload["source"] = AUTO_SOURCE
        payload["priority"] = qa_cluster_mod._clamp_priority(payload.get("priority"))
        entry = repo.create_qa_entry(payload)
        created += 1
        adopted_ids.append(str(entry.get("id") or ""))
        _audit_create(audit_fn, account_id, entry, "auto_fresh")
    return created


# ---- 循环与启动 ----


async def digest_loop() -> None:
    """循环体：env 关=空转零成本（只读一次 env）；env 开=单轮沉淀后睡间隔。

    永不因单轮失败而亡（外层 try/except 全包）；单飞锁在 run_digest_once 内。
    """
    while True:
        if digest_enabled():
            try:
                out = await run_digest_once()
                if out.get("skipped"):
                    print(f"[qa-digest] skipped: {out['skipped']}", flush=True)
                else:
                    print(
                        "[qa-digest] run"
                        f" adopted(v={out.get('adopted_variant')},"
                        f"f={out.get('adopted_fresh')})"
                        f" disabled={out.get('disabled')}"
                        f" threshold(+{out.get('threshold_raised') or 0}"
                        f"/-{out.get('threshold_decayed') or 0})"
                        f" homophones={out.get('homophones')}"
                        f" errors={len(out.get('errors') or [])}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001 - 循环韧性
                print(f"[qa-digest] run error {exc!r}", flush=True)
        await asyncio.sleep(digest_interval_s())


def start_digest_task() -> None:
    """main startup 钩子注册：强引用持 task 防 GC（同 _disconnect_room_tasks 姿势）。

    env 默认关时任务照样挂上（空转零成本）——翻转 env 后无需重启即生效。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    task = loop.create_task(digest_loop())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
