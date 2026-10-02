"""W1b 意图语义车道:本地 embedding 检索对「关键词未中」轮的同步补位。

镜像 qa_gate 纪律:纯逻辑 + HTTP 客户端,**不 import flow.py**(运行时引擎);
日志不打在这里——返回结果对象(`SemMatch`),agent.py 统一打点。

**评分对 qa_gate 0.6/0.4 的刻意偏离(勿「改回一致」)**:qa_gate 的 0.90 阈值
工作在「子串≈1」的近逐字档——0.4×子串把近逐字命中顶过阈值,0.6×余弦只是
辅助;意图语义匹配工作在「关键词未中 → 子串≈0」的释义档,若沿用 0.6 缩放,
阈值 0.78 相当于要求余弦 ≥1.3,**结构性不可达**。故 w_cos=1.0(余弦直接对
阈值)、w_sub=0.10(关键词字面子串出现时的温和加分,奖励 ASR 字面贴近,
不参与释义判定)。

**SSRF 边界(刻意限制不是疏忽)**:`EmbedClient` 只放行环回地址——客户话语
是敏感数据,embedding 出网违反本仓数据政策。形状参照 scripts/load_intent_catalog.py
`_assert_safe_cp_url`:协议限 http/https + getaddrinfo 解析后逐 IP is_loopback
校验,非环回在构造期即拒绝。
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import socket
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass

from bok_voice_core.flow_graph import (
    ACTION_PLAY_QA,
    FlowGraphDoc,
    FlowIntent,
    normalize_graph_text,
)

# 意图语义快照缓存的容量(按素材文本集哈希;16 个模板热点绰绰有余)。
_SNAPSHOT_CACHE_CAPACITY = 16


def semantic_enabled() -> bool:
    """总闸(默认开;端点缺席时装配面自动降级——开关只是运营逃生门)。"""
    return os.environ.get("BOK_INTENT_SEMANTIC", "1") == "1"


def semantic_threshold() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("BOK_INTENT_SEM_THRESHOLD", "0.78"))))
    except ValueError:
        return 0.78


def semantic_base_url() -> str:
    raw = os.environ.get("BOK_INTENT_SEM_BASE_URL", "").strip()
    return raw or "http://127.0.0.1:8789"


def semantic_timeout_s() -> float:
    """毫秒→秒;坏值回默认 400ms(语义查询在热路径同步等,预算必须小)。"""
    try:
        return max(0.0, float(os.environ.get("BOK_INTENT_SEM_TIMEOUT_MS", "400"))) / 1000.0
    except ValueError:
        return 0.4


def semantic_weights() -> tuple[float, float]:
    """(w_cos, w_sub);偏离 qa_gate 0.6/0.4 的原因见模块 docstring。"""

    def _w(key: str, default: float) -> float:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    return _w("BOK_INTENT_SEM_COS_W", 1.0), _w("BOK_INTENT_SEM_SUB_W", 0.10)


def eligible_semantic_intents(
    doc: FlowGraphDoc,
    *,
    step_1based: int,
    fired: set[str],
    play_allowed: bool = True,
) -> list[FlowIntent]:
    """语义匹配的候选意图(纯函数):镜像 `eligible_judge_intents` 四闸。

    ①`enabled`;②有向量素材(label/keywords/judge.prompt 皆素材,**恒真**——
    空素材意图的 max-pool 恒 0 分,结构性过不了阈值,无需显式闸);③步 scope
    (空=全程);④至少一条可触发绑定(enabled + intent 对上 + once 未 fired)。

    `play_allowed=False`(规则推进轮:play 臂将被 advanced 守卫旁路)时滤掉
    「仅剩 play_qa 绑定可触发」的意图——命中也播不出,白 embed 一轮;混合绑定
    (play+jump/notify)保留,jump/notify 臂不受 advanced 守卫约束(蓝图定案)。
    """
    if not doc.intents:
        return []
    out: list[FlowIntent] = []
    for intent in doc.intents:
        if not intent.enabled:
            continue
        if intent.steps and step_1based not in intent.steps:
            continue
        live = [
            b
            for b in doc.bindings
            if b.enabled and b.intent == intent.id and not (b.once and b.id in fired)
        ]
        if not live:
            continue
        if not play_allowed and all(b.action == ACTION_PLAY_QA for b in live):
            continue
        out.append(intent)
    return out


def blend_score(cos: float, sub: float, *, w_cos: float, w_sub: float) -> float:
    """混合分(纯函数):w_cos×余弦 + w_sub×子串比;权重语义见模块 docstring。"""
    return w_cos * cos + w_sub * sub


@dataclass
class SemMatch:
    """语义匹配结果(空 intent_id=未中;日志由 agent.py 统一打)。

    `reason` ∈ ""|no_candidates|no_embedder|timeout|error:空串=正常评分但未过
    阈值;no_candidates=无可匹配意图(全部被闸滤掉/空话语,零 embed 调用);
    其余为 embed 不可用归因(本通降级闩上后恒 no_embedder)。
    """

    intent_id: str = ""
    score: float = 0.0
    best_text: str = ""
    reason: str = ""


def _assert_loopback_url(url: str) -> None:
    """SSRF 边界:协议限 http/https,主机解析后逐 IP 必须环回(见模块 docstring)。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported embedding url scheme: {parsed.scheme!r} (http/https only)")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("embedding url missing host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    if not infos:
        raise ValueError(f"embedding url host {host!r} resolved to nothing")
    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_loopback:
            raise ValueError(
                f"embedding url host {host} resolves to non-loopback {ip}; "
                "客户话语不出网(语义车道仅限本机 embedding 端点)"
            )


def _exc_type_names(exc: BaseException) -> set[str]:
    """异常 MRO 名集(含 cause/context 链)——免硬依赖 httpx 的传输层分类。"""
    names: set[str] = set()
    cur: BaseException | None = exc
    while cur is not None:
        names |= {cls.__name__ for cls in type(cur).__mro__}
        cur = cur.__cause__ or cur.__context__
    return names


class EmbedClient:
    """OpenAI 形 /v1/embeddings 客户端(loopback-only + 本通降级闩)。

    降级闩:连接拒绝(端点不在)→ 立即 dead;其它错误连续 2 次 → dead。dead 后
    `embed()` 零 HTTP、恒 None 且 `last_reason="no_embedder"`——整通不再碰端点。
    `last_reason` ∈ ""|no_embedder|timeout|error,供调用方归因(本模块不打日志)。
    """

    def __init__(self, base_url: str = "", *, timeout_s: float | None = None) -> None:
        self.base_url = (base_url or semantic_base_url()).rstrip("/")
        self.timeout_s = semantic_timeout_s() if timeout_s is None else timeout_s
        self.dead = False
        self.last_reason = ""
        self._consecutive_errors = 0
        _assert_loopback_url(self.base_url)

    async def _post(self, url: str, payload: dict, timeout_s: float) -> object:
        """HTTP 传输缝(测试以子类覆写注入 fake transport 计数)。"""
        import httpx  # 延迟导入:fake transport 测试路径零 httpx 依赖

        async with httpx.AsyncClient(timeout=timeout_s) as http:
            resp = await http.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()

    def _register_error(self, reason: str) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= 2:
            self.dead = True
        self.last_reason = reason

    async def embed(self, texts: list[str], *, timeout_s: float | None = None) -> list[list[float]] | None:
        """批量向量(OpenAI 形);失败/闩上 → None(理由落 last_reason,绝不抛)。

        `timeout_s` 覆盖:装配期批量索引(一次几百条,~500ms+)与每轮单句查询
        (~20ms)预算不同——真模型实测 178 条 514ms,默认 400ms 查询预算下批量
        必超时(2026-09-23 真栈发现,离线假 embedder 零延迟抓不到)。build 用
        分块+`_BUILD_EMBED_TIMEOUT_S`,查询保持构造期默认。
        """
        if self.dead:
            self.last_reason = "no_embedder"
            return None
        clean = [str(t or "") for t in (texts or []) if str(t or "").strip()]
        if not clean:
            return []
        url = f"{self.base_url}/v1/embeddings"
        try:
            data = await self._post(url, {"input": clean}, self.timeout_s if timeout_s is None else timeout_s)
        except Exception as exc:  # noqa: BLE001 - 分类见下;CancelledError 属 BaseException 直穿
            names = _exc_type_names(exc)
            if isinstance(exc, TimeoutError) or "TimeoutException" in names:
                self._register_error("timeout")
            elif isinstance(exc, ConnectionRefusedError) or "ConnectError" in names:
                # 连接拒绝=端点不在,本通重试无意义 → 立即判死
                self.dead = True
                self.last_reason = "error"
            else:
                self._register_error("error")
            return None
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            self._register_error("error")
            return None
        try:
            ordered = sorted(rows, key=lambda r: int(r.get("index", 0)) if isinstance(r, dict) else 0)
            vecs = [[float(x) for x in r["embedding"]] for r in ordered]
        except (KeyError, TypeError, ValueError):
            self._register_error("error")
            return None
        if len(vecs) != len(clean) or any(not v for v in vecs):
            self._register_error("error")
            return None
        self._consecutive_errors = 0
        self.last_reason = ""
        return vecs


def intent_material_texts(intent: FlowIntent) -> list[str]:
    """意图语义素材(纯函数):label + keywords + judge.prompt,去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (intent.label, intent.judge_prompt, *intent.keywords):
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


class _SnapshotVectorCache:
    """模块级 LRU(容量 16):sha256(素材文本集) → {素材文本: 向量}。

    同模板反复外呼零重复 embed;改模板 → 哈希变 → 重算,零失效逻辑。
    """

    def __init__(self, capacity: int = _SNAPSHOT_CACHE_CAPACITY) -> None:
        self._capacity = capacity
        self._data: OrderedDict[str, dict[str, list[float]]] = OrderedDict()

    def get(self, key: str) -> dict[str, list[float]] | None:
        hit = self._data.get(key)
        if hit is not None:
            self._data.move_to_end(key)
        return hit

    def put(self, key: str, value: dict[str, list[float]]) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


SEMANTIC_VECTOR_CACHE = _SnapshotVectorCache()

# 装配期批量索引的分块与预算(2026-09-23 真栈校准):单块 64 条 ~150-250ms,
# 178 条素材 3 块 ~520ms——查询道 400ms 预算下不分块必超时;块预算 5s 远离
# 每轮路径(session.start 前一次性完成),亦低于服务端 256 条批量上限。
_BUILD_EMBED_CHUNK = 64
_BUILD_EMBED_TIMEOUT_S = 5.0


def _cosines(query: list[float], rows: list[list[float]]) -> list[float]:
    """query 与各素材行的余弦(numpy 软依赖:在则点积矩阵化,缺则纯 Python,结果一致)。"""
    if not rows:
        return []
    try:
        import numpy as np

        q = np.asarray(query, dtype=float)
        m = np.asarray(rows, dtype=float)
        qn = float(np.linalg.norm(q))
        rn = np.linalg.norm(m, axis=1)
        denom = qn * rn
        denom[denom == 0.0] = 1e-9
        return [float(x) for x in (m @ q) / denom]
    except Exception:  # noqa: BLE001 - numpy 缺席/形状怪 → 纯 Python 正确降级
        qn = math.sqrt(sum(x * x for x in query)) or 1e-9
        out: list[float] = []
        for row in rows:
            rn = math.sqrt(sum(x * x for x in row)) or 1e-9
            out.append(sum(x * y for x, y in zip(query, row)) / (qn * rn))
        return out


class IntentSemanticIndex:
    """意图语义索引:装配构建一次(素材向量经模块 LRU 快照缓存),每轮至多 1 次 query embed。"""

    def __init__(
        self,
        client: EmbedClient,
        by_intent: dict[str, list[tuple[str, list[float]]]],
    ) -> None:
        self._client = client
        self._by_intent = by_intent

    def __len__(self) -> int:
        return len(self._by_intent)

    @classmethod
    async def build(
        cls, client: EmbedClient, doc: FlowGraphDoc
    ) -> IntentSemanticIndex | None:
        """素材空/全部失败 → None(整通惰性);同素材第二次 build 走缓存零 HTTP。"""
        by_intent: dict[str, list[str]] = {}
        all_texts: list[str] = []
        seen: set[str] = set()
        for intent in doc.intents:
            if not intent.enabled:
                continue
            texts = intent_material_texts(intent)
            if not texts:
                continue
            by_intent[intent.id] = texts
            for t in texts:
                if t not in seen:
                    seen.add(t)
                    all_texts.append(t)
        if not all_texts:
            return None
        key = hashlib.sha256("\x1f".join(all_texts).encode("utf-8")).hexdigest()
        table = SEMANTIC_VECTOR_CACHE.get(key)
        if table is None:
            # 分块 + 装配期专属预算:单块 64 条(~150-250ms 实测)远低于服务端
            # 256 上限,也压平批量尾延迟;块预算 _BUILD_EMBED_TIMEOUT_S(5s,非
            # 每轮查询的 400ms——装配窗一次性开销,session.start 前完成)。
            vecs: list[list[float]] = []
            for start in range(0, len(all_texts), _BUILD_EMBED_CHUNK):
                chunk = all_texts[start : start + _BUILD_EMBED_CHUNK]
                part = await client.embed(chunk, timeout_s=_BUILD_EMBED_TIMEOUT_S)
                if part is None or len(part) != len(chunk):
                    return None
                vecs.extend(part)
            table = dict(zip(all_texts, vecs))
            SEMANTIC_VECTOR_CACHE.put(key, table)
        snapshot: dict[str, list[tuple[str, list[float]]]] = {}
        for intent_id, texts in by_intent.items():
            items = [(t, table[t]) for t in texts if t in table]
            if items:
                snapshot[intent_id] = items
        if not snapshot:
            return None
        return cls(client, snapshot)

    async def match(
        self,
        user_text: str,
        doc: FlowGraphDoc,
        *,
        step_1based: int,
        fired: set[str],
        play_allowed: bool = True,
    ) -> SemMatch:
        """语义匹配:eligible 预筛 → 1 次 query embed → max-pool + 子串 bonus
        → blend → 阈值门 → top-1;未中/不可用返回带 reason 的空 `SemMatch`。"""
        utt = str(user_text or "")
        if not utt.strip():
            return SemMatch(reason="no_candidates")  # 空话语零调用
        eligible = [
            i
            for i in eligible_semantic_intents(
                doc, step_1based=step_1based, fired=fired, play_allowed=play_allowed
            )
            if i.id in self._by_intent
        ]
        if not eligible:
            return SemMatch(reason="no_candidates")
        qvecs = await self._client.embed([utt])
        if qvecs is None:
            return SemMatch(reason=self._client.last_reason or "no_embedder")
        qvec = qvecs[0]
        norm_utt = normalize_graph_text(utt)
        thr = semantic_threshold()
        w_cos, w_sub = semantic_weights()
        best_id, best_score, best_text = "", 0.0, ""
        top_score = 0.0  # 全场最高分(未过关时的诊断返回,镜像 qa_gate 旧档语义)
        for intent in eligible:
            items = self._by_intent.get(intent.id) or []
            cos_list = _cosines(qvec, [v for _t, v in items])
            cos, arg_text = 0.0, ""
            for (text, _vec), c in zip(items, cos_list):
                if c > cos:
                    cos, arg_text = c, text
            # 子串 bonus(只认关键词字面:双侧归一出现 → len(kw)/len(utt))
            sub = 0.0
            for kw in intent.keywords:
                nk = normalize_graph_text(kw)
                if nk and nk in norm_utt:
                    sub = max(sub, len(nk) / max(1, len(norm_utt)))
            score = blend_score(cos, sub, w_cos=w_cos, w_sub=w_sub)
            if score > top_score:
                top_score = score
            # 阈值门镜像 qa_gate(score < thr 不过;平分保 doc 序=先到意图优先)
            if score >= thr and score > best_score:
                best_id, best_score = intent.id, score
                best_text = arg_text or items[0][0]
        if best_id:
            return SemMatch(intent_id=best_id, score=best_score, best_text=best_text)
        return SemMatch(score=top_score)  # 未过阈值:reason="" + 全场最高分供日志归因
