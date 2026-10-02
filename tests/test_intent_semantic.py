"""W1b 意图语义车道单元面:blend/env 钳制 / eligible 四闸 / 索引匹配 /
pick_graph_action(semantic_hit) / EmbedClient 降级闩与 SSRF 边界 / 快照缓存。

镜像 test_flow_graph_judge 姿势:纯函数离线测;HTTP 面以 fake transport 计数,
零真网络(async 用 asyncio.run,仓内无 pytest-asyncio)。模块纪律见
agent_runtime/intent_semantic.py docstring(评分对 qa_gate 0.6/0.4 的刻意
偏离亦有钉)。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for part in ("packages/core", "apps/agent"):
    p = ROOT / part
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import bok_voice_core.flow_graph as fg  # noqa: E402
from agent_runtime import intent_semantic as isem  # noqa: E402
from agent_runtime.intent_semantic import (  # noqa: E402
    EmbedClient,
    IntentSemanticIndex,
    SemMatch,
    blend_score,
    eligible_semantic_intents,
    intent_material_texts,
    semantic_base_url,
    semantic_enabled,
    semantic_threshold,
    semantic_timeout_s,
    semantic_weights,
)

_ID_A = "int_1a2b3c4d"
_ID_B = "int_2b3c4d5e"
_ID_C = "int_3c4d5e6f"
_BND_A = "bnd_7e8f9a0b"
_BND_B = "bnd_c1d2e3f4"
_BND_C = "bnd_d4e5f6a7"

_ENV_KEYS = (
    "BOK_INTENT_SEMANTIC",
    "BOK_INTENT_SEM_THRESHOLD",
    "BOK_INTENT_SEM_BASE_URL",
    "BOK_INTENT_SEM_TIMEOUT_MS",
    "BOK_INTENT_SEM_COS_W",
    "BOK_INTENT_SEM_SUB_W",
)


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    """每测:六枚 env 回未设档(默认值),语义快照缓存清空(防跨测污染)。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    isem.SEMANTIC_VECTOR_CACHE.clear()
    yield
    isem.SEMANTIC_VECTOR_CACHE.clear()


def _graph(*, intents: list[dict], bindings: list[dict] | None = None) -> str:
    return json.dumps({"version": 1, "intents": intents, "bindings": bindings or []})


def _intent(intent_id: str = _ID_A, *, keywords=None, steps=None, judge=None, **over):
    item: dict = {
        "id": intent_id,
        "label": f"label-{intent_id}",
        "keywords": keywords if keywords is not None else ["kw-" + intent_id[-4:]],
        "enabled": True,
    }
    if steps is not None:
        item["steps"] = steps
    if judge is not None:
        item["judge"] = {"prompt": judge}
    item.update(over)
    return item


def _binding(
    binding_id: str = _BND_A,
    *,
    intent: str = _ID_A,
    action: str = "jump_step",
    **over: object,
) -> dict:
    item: dict = {"id": binding_id, "intent": intent, "action": action, "enabled": True}
    if action == "jump_step":
        item["step"] = 4
    else:
        item["qa_id"] = "qa-1"
    item.update(over)
    return item


def _doc_from(intents: list[dict], bindings: list[dict] | None = None) -> fg.FlowGraphDoc:
    return fg.parse_flow_graph(_graph(intents=intents, bindings=bindings))


def pick(doc, text, **kw):
    return fg.pick_graph_action(
        doc, text, step_1based=kw.pop("step_1based", 1), fired=kw.pop("fired", set()), **kw
    )


# ---- blend_score 数学与 env 钳制 ----


def test_blend_score_math():
    assert blend_score(0.8, 0.5, w_cos=1.0, w_sub=0.10) == pytest.approx(0.85)
    assert blend_score(1.0, 0.0, w_cos=1.0, w_sub=0.10) == pytest.approx(1.0)
    assert blend_score(0.6, 1.0, w_cos=0.6, w_sub=0.4) == pytest.approx(0.76)
    assert blend_score(0.0, 0.0, w_cos=1.0, w_sub=0.10) == 0.0


def test_env_defaults_when_unset():
    assert semantic_enabled() is True
    assert semantic_threshold() == pytest.approx(0.78)
    assert semantic_base_url() == "http://127.0.0.1:8789"
    assert semantic_timeout_s() == pytest.approx(0.4)
    assert semantic_weights() == (pytest.approx(1.0), pytest.approx(0.10))


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("abc", 0.78),  # 坏值回默认
        ("", 0.78),
        ("1.5", 1.0),  # 钳 [0,1]
        ("-0.2", 0.0),
        ("0.5", 0.5),
    ],
)
def test_env_threshold_clamp(monkeypatch, raw, expect):
    monkeypatch.setenv("BOK_INTENT_SEM_THRESHOLD", raw)
    assert semantic_threshold() == pytest.approx(expect)


@pytest.mark.parametrize(
    ("raw", "expect"),
    [("abc", 0.4), ("", 0.4), ("0", 0.0), ("1000", 1.0), ("-5", 0.0)],
)
def test_env_timeout_ms(monkeypatch, raw, expect):
    monkeypatch.setenv("BOK_INTENT_SEM_TIMEOUT_MS", raw)
    assert semantic_timeout_s() == pytest.approx(expect)


def test_env_weights_bad_values_fall_back(monkeypatch):
    monkeypatch.setenv("BOK_INTENT_SEM_COS_W", "not-a-float")
    monkeypatch.setenv("BOK_INTENT_SEM_SUB_W", "")
    assert semantic_weights() == (pytest.approx(1.0), pytest.approx(0.10))
    monkeypatch.setenv("BOK_INTENT_SEM_COS_W", "0.7")
    monkeypatch.setenv("BOK_INTENT_SEM_SUB_W", "0.3")
    assert semantic_weights() == (pytest.approx(0.7), pytest.approx(0.3))


def test_env_kill_switch(monkeypatch):
    monkeypatch.setenv("BOK_INTENT_SEMANTIC", "0")
    assert semantic_enabled() is False


# ---- eligible_semantic_intents:四闸镜像 + play_allowed 过滤 ----


def test_eligible_excludes_disabled_and_out_of_scope_and_dead_bindings():
    doc = _doc_from([_intent(steps=[3])], [_binding()])
    assert [i.id for i in eligible_semantic_intents(doc, step_1based=1, fired=set())] == []
    assert [i.id for i in eligible_semantic_intents(doc, step_1based=3, fired=set())] == [_ID_A]
    doc.intents[0].enabled = False
    assert eligible_semantic_intents(doc, step_1based=3, fired=set()) == []
    doc.intents[0].enabled = True
    doc.bindings[0].enabled = False
    assert eligible_semantic_intents(doc, step_1based=3, fired=set()) == []
    doc.bindings[0].enabled = True
    doc.bindings[0].once = True
    assert [i.id for i in eligible_semantic_intents(doc, step_1based=3, fired=set())] == [_ID_A]
    assert eligible_semantic_intents(doc, step_1based=3, fired={_BND_A}) == []


def test_eligible_play_allowed_filters_play_only_keeps_mixed():
    doc = _doc_from(
        [_intent(_ID_A), _intent(_ID_B)],
        [
            _binding(_BND_A, intent=_ID_A, action="play_qa"),
            _binding(_BND_B, intent=_ID_B, action="play_qa"),
            _binding(_BND_C, intent=_ID_B, action="jump_step", step=5),
        ],
    )
    # play 允许档:两个都合格
    assert [
        i.id
        for i in eligible_semantic_intents(doc, step_1based=1, fired=set(), play_allowed=True)
    ] == [_ID_A, _ID_B]
    # advanced 轮(play 臂将被守卫旁路):仅剩 play 绑定的 A 被滤,混合绑定的 B 保留
    assert [
        i.id
        for i in eligible_semantic_intents(doc, step_1based=1, fired=set(), play_allowed=False)
    ] == [_ID_B]


# ---- IntentSemanticIndex.match:FakeEmbedder 定向量 ----


class FakeEmbedder:
    """固定向量表 fake embedder(接口=EmbedClient;零 HTTP、零闩)。"""

    def __init__(self, table: dict[str, list[float]]):
        self.table = dict(table)
        self.calls = 0
        self.dead = False
        self.last_reason = ""

    async def embed(self, texts: list[str], *, timeout_s: float | None = None) -> list[list[float]]:
        # timeout_s:与生产 EmbedClient.embed 同形(build 分块传装配期预算,真模型
        # 178 条 514ms>查询道 400ms——2026-09-23 真栈发现,假 embedder 必须接 kwarg)。
        self.calls += 1
        return [list(self.table[t]) for t in texts]


_E0 = [1.0, 0.0, 0.0, 0.0]
_E1 = [0.0, 1.0, 0.0, 0.0]
_E2 = [0.0, 0.0, 1.0, 0.0]
_E3 = [0.0, 0.0, 0.0, 1.0]


def _semantic_doc() -> fg.FlowGraphDoc:
    return _doc_from(
        [_intent(_ID_A, keywords=["kwA1", "kwA2"]), _intent(_ID_B, keywords=["kwB1"])],
        [
            _binding(_BND_A, intent=_ID_A, action="jump_step", step=4),
            _binding(_BND_B, intent=_ID_B, action="jump_step", step=5),
        ],
    )


def _fake_table() -> dict[str, list[float]]:
    return {
        # A 意图素材:label / 两关键词各占一根正交轴
        "label-int_1a2b3c4d": _E0,
        "kwA1": _E1,
        "kwA2": _E2,
        # B 意图素材
        "label-int_2b3c4d5e": _E3,
        "kwB1": _E0,
        # 默认关键词形态(int_1a2b3c4d[-4:]="3c4d";play_allowed 过滤用例的素材)
        "kw-3c4d": _E0,
        # 用户话语向量
        "随便说点什么": _E2,  # 与 A 的 kwA2 同轴 → cos=1.0(max-pool 命中)
        "完全无关的话语": _E3,  # 与 B 的 label 同轴
        "正交话语": [0.7, 0.7, 0.0, 0.0],  # 与全部素材 cos=0
        "我要投诉呀": _E3,
    }


def test_match_max_pool_threshold_top1_and_best_text():
    async def _run():
        doc = _semantic_doc()
        fake = FakeEmbedder(_fake_table())
        idx = await IntentSemanticIndex.build(fake, doc)
        assert idx is not None and len(idx) == 2
        build_calls = fake.calls
        assert build_calls == 1  # 素材一次批量 embed

        # max-pool:A 的素材里 kwA2 同轴 → cos=1.0 胜过 label/kwA1 的 0 分
        m = await idx.match("随便说点什么", doc, step_1based=1, fired=set())
        assert m.reason == "" and m.intent_id == _ID_A
        assert m.score == pytest.approx(1.0)
        assert m.best_text == "kwA2"
        assert fake.calls == build_calls + 1  # 每轮至多 1 次 query embed

        # top-1:另一话语与 B 素材同轴 → B 胜(doc 序在前的 A 不抢)
        m2 = await idx.match("完全无关的话语", doc, step_1based=1, fired=set())
        assert m2.intent_id == _ID_B and m2.score == pytest.approx(1.0)

    asyncio.run(_run())


def test_match_below_threshold_returns_empty_with_blank_reason():
    async def _run():
        doc = _semantic_doc()
        idx = await IntentSemanticIndex.build(FakeEmbedder(_fake_table()), doc)
        m = await idx.match("正交话语", doc, step_1based=1, fired=set())
        assert m.intent_id == "" and m.reason == ""  # 正常评分未过阈值
        # 未过关返回**全场最高分**(诊断位,镜像 qa_gate 旧档语义):
        # [0.7,0.7,0,0] 与 E0/E1 的 cos=0.7/0.9899≈0.7071 < 0.78
        assert m.score == pytest.approx(0.7071067811865476, abs=1e-9)

    asyncio.run(_run())


def test_match_threshold_env_gates(monkeypatch):
    """阈值是 match 时读 env:同分在默认阈 0.78 过、抬到 0.9 不过(阈值先行)。"""

    async def _run(match_expect_hit: bool):
        if not match_expect_hit:
            monkeypatch.setenv("BOK_INTENT_SEM_THRESHOLD", "0.9")
        doc = _doc_from(
            [_intent(_ID_A, keywords=["kwA1"])],
            [_binding(_BND_A, intent=_ID_A, action="jump_step", step=4)],
        )
        table = {"label-int_1a2b3c4d": _E0, "kwA1": _E1, "半同轴话语": [0.6, 0.8, 0.0, 0.0]}
        idx = await IntentSemanticIndex.build(FakeEmbedder(table), doc)
        # cos(半同轴, kwA1)=0.8
        m = await idx.match("半同轴话语", doc, step_1based=1, fired=set())
        if match_expect_hit:
            assert m.intent_id == _ID_A and m.score == pytest.approx(0.8)
        else:
            assert m.intent_id == "" and m.reason == "" and m.score == pytest.approx(0.8)

    asyncio.run(_run(True))
    asyncio.run(_run(False))


def test_match_substring_bonus_can_lift_over_threshold(monkeypatch):
    """子串 bonus(0.1 档只是温和加分;释义档 0.78 结构性不吃它——偏离 qa_gate 的原因)。"""

    async def _run(sub_w: str, thr: str, expect_hit: bool):
        monkeypatch.setenv("BOK_INTENT_SEM_SUB_W", sub_w)
        monkeypatch.setenv("BOK_INTENT_SEM_THRESHOLD", thr)
        doc = _doc_from(
            [_intent(_ID_A, keywords=["投诉"])],
            [_binding(_BND_A, intent=_ID_A, action="jump_step", step=4)],
        )
        table = {"label-int_1a2b3c4d": _E0, "投诉": _E1, "我要投诉呀": _E3}  # cos=0
        idx = await IntentSemanticIndex.build(FakeEmbedder(table), doc)
        # 归一「我要投诉呀」len 5,kw len 2 → sub=0.4
        m = await idx.match("我要投诉呀", doc, step_1based=1, fired=set())
        if expect_hit:
            assert m.intent_id == _ID_A and m.score == pytest.approx(0.4)
        else:
            assert m.intent_id == ""

    asyncio.run(_run("1.0", "0.3", True))  # 纯子串加分过关
    asyncio.run(_run("0.10", "0.78", False))  # 默认权重下 0.04 远不到阈(缩放偏离的必要性)


def test_match_scope_and_once_and_play_allowed_gates():
    async def _run():
        # 步 scope:A 只在步 3 生效 → 步 1 时 A 被滤,无候选
        doc_a = _doc_from(
            [_intent(_ID_A, keywords=["kwA1"], steps=[3])],
            [_binding(_BND_A, intent=_ID_A, action="jump_step", step=4, once=True)],
        )
        fake2 = FakeEmbedder(_fake_table())
        idx2 = await IntentSemanticIndex.build(fake2, doc_a)
        m = await idx2.match("随便说点什么", doc_a, step_1based=1, fired=set())
        assert m.intent_id == "" and m.reason == "no_candidates"
        assert fake2.calls == 1  # build 那次;match 预筛空 → 零 query embed
        # once 已烧 → 同样无可候选
        m2 = await idx2.match("随便说点什么", doc_a, step_1based=3, fired={_BND_A})
        assert m2.reason == "no_candidates"
        # play_allowed=False + 仅剩 play 绑定 → 预筛滤光,零 query embed
        doc_play = _doc_from([_intent(_ID_A)], [_binding(_BND_A, intent=_ID_A, action="play_qa")])
        fake3 = FakeEmbedder(_fake_table())
        idx3 = await IntentSemanticIndex.build(fake3, doc_play)
        m3 = await idx3.match("随便说点什么", doc_play, step_1based=1, fired=set(), play_allowed=False)
        assert m3.reason == "no_candidates"
        assert fake3.calls == 1

    asyncio.run(_run())


def test_match_empty_utterance_makes_zero_calls():
    async def _run():
        doc = _semantic_doc()
        fake = FakeEmbedder(_fake_table())
        idx = await IntentSemanticIndex.build(fake, doc)
        build_calls = fake.calls
        for empty in ("", "   "):
            m = await idx.match(empty, doc, step_1based=1, fired=set())
            assert m.reason == "no_candidates" and m.intent_id == ""
        assert fake.calls == build_calls  # 空话语零调用

    asyncio.run(_run())


def test_build_empty_material_or_dead_embedder_returns_none():
    async def _run():
        assert await IntentSemanticIndex.build(FakeEmbedder({}), fg.FlowGraphDoc()) is None
        # 端点缺席(连接拒绝)→ build None,闩已上
        dead = _failing_client(ConnectionRefusedError("refused"))
        doc = _semantic_doc()
        assert await IntentSemanticIndex.build(dead, doc) is None
        assert dead.dead is True

    asyncio.run(_run())


# ---- pick_graph_action(semantic_hit):与 judge 同权同守卫 ----


def test_semantic_hit_fires_when_keywords_miss():
    doc = _doc_from([_intent(_ID_A)], [_binding(_BND_A)])
    text = "随便一句模糊话"
    assert pick(doc, text) is None
    hit = pick(doc, text, semantic_hit=_ID_A)
    assert hit is not None and hit.id == _BND_A


def test_semantic_hit_unknown_or_disabled_or_out_of_scope_is_noop():
    doc = _doc_from([_intent(_ID_A)], [_binding(_BND_A)])
    text = "随便一句模糊话"
    assert pick(doc, text, semantic_hit="int_deadbeef") is None
    doc.intents[0].enabled = False
    assert pick(doc, text, semantic_hit=_ID_A) is None
    doc.intents[0].enabled = True
    doc.intents[0].steps = [3]
    assert pick(doc, text, semantic_hit=_ID_A) is None
    assert pick(doc, text, semantic_hit=_ID_A, step_1based=3) is not None


def test_semantic_hit_binding_disabled_and_once_fired_noop():
    doc = _doc_from([_intent(_ID_A)], [_binding(_BND_A)])
    text = "随便一句模糊话"
    doc.bindings[0].enabled = False
    assert pick(doc, text, semantic_hit=_ID_A) is None
    doc.bindings[0].enabled = True
    doc.bindings[0].once = True
    assert pick(doc, text, semantic_hit=_ID_A, fired=set()) is not None
    assert pick(doc, text, semantic_hit=_ID_A, fired={_BND_A}) is None


def test_semantic_hit_empty_text_noop_and_default_byte_same():
    doc = _doc_from([_intent(_ID_A)], [_binding(_BND_A)])
    assert pick(doc, "", semantic_hit=_ID_A) is None
    # 默认参数(不带 semantic_hit)=旧档:关键词未中恒 None
    assert pick(doc, "随便一句模糊话") is None
    assert pick(doc, "kw-3c4d") is not None  # 关键词路照旧


def test_semantic_hit_shares_candidate_pool_and_sort_with_keywords():
    """关键词命中(prio 30 的 C) 与语义命中(prio 5 的 A) 同轮 → 同池排序 A 胜
    (语义命中只是「多了一个命中意图」,唔係特权通道)。"""
    doc = _doc_from(
        [_intent(_ID_A), _intent(_ID_C)],
        [
            _binding(_BND_A, intent=_ID_A, action="jump_step", step=4, priority=5),
            _binding(_BND_C, intent=_ID_C, action="jump_step", step=6, priority=30),
        ],
    )
    # 默认关键词:A="kw-3c4d"、C="kw-5e6f"
    only_kw_a = pick(doc, "kw-3c4d")
    assert only_kw_a is not None and only_kw_a.id == _BND_A
    only_kw_c = pick(doc, "kw-5e6f")
    assert only_kw_c is not None and only_kw_c.id == _BND_C
    only_sem = pick(doc, "随便一句模糊话", semantic_hit=_ID_C)
    assert only_sem is not None and only_sem.id == _BND_C
    # 关键词(C, prio 30) + 语义(A, prio 5) 同轮:同一池 → priority 5 的 A 胜
    both = pick(doc, "kw-5e6f", semantic_hit=_ID_A)
    assert both is not None and both.id == _BND_A


def test_semantic_and_judge_hits_coexist_single_winner():
    """judge + semantic 并存:多路命中进同一池,只出**一个**绑定 (priority,id) 排序。"""
    doc = _doc_from(
        [_intent(_ID_A), _intent(_ID_B), _intent(_ID_C)],
        [
            _binding(_BND_A, intent=_ID_A, action="jump_step", step=4, priority=20),
            _binding(_BND_B, intent=_ID_B, action="jump_step", step=5, priority=8),
            _binding(_BND_C, intent=_ID_C, action="jump_step", step=6, priority=12),
        ],
    )
    text = "随便一句模糊话"  # 关键词全不中
    hit = fg.pick_graph_action(
        doc, text, step_1based=1, fired=set(), judge_hit=_ID_B, semantic_hit=_ID_C
    )
    assert hit is not None and hit.id == _BND_B  # prio 8 < 12
    # 同一意图双路同时命中(judge=semantic=C)→ 集合去重,仍单绑定
    hit2 = fg.pick_graph_action(
        doc, text, step_1based=1, fired=set(), judge_hit=_ID_C, semantic_hit=_ID_C
    )
    assert hit2 is not None and hit2.id == _BND_C


def test_keyword_and_semantic_same_intent_single_binding():
    doc = _doc_from([_intent(_ID_A)], [_binding(_BND_A)])
    hit = pick(doc, "kw-3c4d", semantic_hit=_ID_A)
    assert hit is not None and hit.id == _BND_A  # 去重后照常单绑定


def test_module_docstring_pins_qa_gate_deviation_rationale():
    """评分偏离 qa_gate 0.6/0.4 的原因必须钉在 docstring(防后人「改回一致」)。"""
    doc = isem.__doc__ or ""
    assert "0.6" in doc and "0.78" in doc and "结构性不可达" in doc


# ---- EmbedClient:SSRF 边界 + 降级闩 + OpenAI 形解析 ----


def _failing_client(error: BaseException, calls: list | None = None) -> EmbedClient:
    class _Flaky(EmbedClient):
        async def _post(self, url: str, payload: dict, timeout_s: float) -> object:
            if calls is not None:
                calls.append((url, payload))
            raise error

    return _Flaky("http://127.0.0.1:8789")


def test_embed_client_rejects_non_loopback_and_bad_scheme(monkeypatch):
    # 非环回解析 → 构造期拒绝(monkeypatch getaddrinfo,零真 DNS)
    def _fake_getaddrinfo(host, port, proto=0, **_kw):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(isem.socket, "getaddrinfo", _fake_getaddrinfo)
    with pytest.raises(ValueError, match="non-loopback"):
        EmbedClient("http://example.com")

    def _loopback_getaddrinfo(host, port, proto=0, **_kw):
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(isem.socket, "getaddrinfo", _loopback_getaddrinfo)
    c = EmbedClient("http://embed.local:8789")
    assert c.base_url == "http://embed.local:8789"
    # 坏协议 / 缺 host → 拒绝
    with pytest.raises(ValueError, match="scheme"):
        EmbedClient("ftp://127.0.0.1:8789")
    with pytest.raises(ValueError, match="missing host"):
        EmbedClient("http://")


def test_embed_client_openai_shape_parse_and_payload():
    async def _run():
        captured: list = []

        class _Ok(EmbedClient):
            async def _post(self, url: str, payload: dict, timeout_s: float) -> object:
                captured.append((url, payload))
                return {
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                }

        client = _Ok("http://127.0.0.1:8789")
        vecs = await client.embed(["甲", "乙"])
        assert vecs == [[1.0, 0.0], [0.0, 1.0]]  # 按 index 归位
        url, payload = captured[0]
        assert url == "http://127.0.0.1:8789/v1/embeddings"
        assert payload == {"input": ["甲", "乙"]}
        assert client.last_reason == ""

    asyncio.run(_run())


def test_embed_client_latch_connection_refused_first_error_kills():
    async def _run():
        calls: list = []
        client = _failing_client(ConnectionRefusedError("refused"), calls)
        assert await client.embed(["x"]) is None
        assert client.dead is True and client.last_reason == "error"
        assert await client.embed(["x"]) is None  # 闩上:零 HTTP
        assert client.last_reason == "no_embedder"
        assert len(calls) == 1  # 后续零调用

    asyncio.run(_run())


def test_embed_client_latch_two_consecutive_generic_errors():
    async def _run():
        calls: list = []
        client = _failing_client(RuntimeError("boom"), calls)
        assert await client.embed(["x"]) is None
        assert client.dead is False and client.last_reason == "error"  # 首错给重试机会
        assert await client.embed(["x"]) is None
        assert client.dead is True  # 连续 2 次 → 判死
        assert await client.embed(["x"]) is None
        assert len(calls) == 2  # 第三次零 HTTP

    asyncio.run(_run())


def test_embed_client_success_resets_consecutive_counter():
    async def _run():
        state = {"fail": True}

        class _Half(EmbedClient):
            async def _post(self, url: str, payload: dict, timeout_s: float) -> object:
                if state["fail"]:
                    raise RuntimeError("boom")
                return {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}

        client = _Half("http://127.0.0.1:8789")
        assert await client.embed(["x"]) is None  # 错 1 次
        assert client.dead is False
        state["fail"] = False
        assert await client.embed(["x"]) == [[1.0, 0.0]]  # 成功重置计数
        state["fail"] = True
        assert await client.embed(["x"]) is None  # 又错 1 次:仍不死(计数已重置)
        assert client.dead is False

    asyncio.run(_run())


def test_embed_client_timeout_classification():
    async def _run():
        calls: list = []
        client = _failing_client(asyncio.TimeoutError(), calls)
        assert await client.embed(["x"]) is None
        assert client.last_reason == "timeout" and client.dead is False
        assert await client.embed(["x"]) is None
        assert client.dead is True and client.last_reason == "timeout"

    asyncio.run(_run())


def test_match_reports_no_embedder_when_client_dead():
    async def _run():
        doc = _semantic_doc()
        client = _failing_client(ConnectionRefusedError("refused"))
        idx = await IntentSemanticIndex.build(client, doc)
        assert idx is None  # build 期就判死 → 整通惰性,match 永不被调

    asyncio.run(_run())


# ---- 快照缓存:同素材第二次 build 零 HTTP ----


def test_snapshot_cache_second_build_zero_http():
    async def _run():
        doc = _semantic_doc()
        first = FakeEmbedder(_fake_table())
        idx1 = await IntentSemanticIndex.build(first, doc)
        assert first.calls == 1
        second = FakeEmbedder(_fake_table())
        idx2 = await IntentSemanticIndex.build(second, doc)
        assert second.calls == 0  # 同素材哈希命中 LRU,零 embed
        m1 = await idx1.match("随便说点什么", doc, step_1based=1, fired=set())
        m2 = await idx2.match("随便说点什么", doc, step_1based=1, fired=set())
        assert m1.intent_id == m2.intent_id == _ID_A  # 向量等价,行为一致
        # 改模板(素材集变)→ 哈希变 → 重算
        doc2 = _doc_from(
            [_intent(_ID_A, keywords=["kwA1", "kwA2", "kwA9"])],
            [_binding(_BND_A, intent=_ID_A, action="jump_step", step=4)],
        )
        third = FakeEmbedder({**_fake_table(), "kwA9": _E1})
        await IntentSemanticIndex.build(third, doc2)
        assert third.calls == 1

    asyncio.run(_run())


def test_snapshot_cache_capacity_bounded():
    isem.SEMANTIC_VECTOR_CACHE.clear()
    for i in range(isem._SNAPSHOT_CACHE_CAPACITY + 3):
        isem.SEMANTIC_VECTOR_CACHE.put(f"k{i}", {"t": [float(i)]})
    assert len(isem.SEMANTIC_VECTOR_CACHE._data) == isem._SNAPSHOT_CACHE_CAPACITY
    assert "k0" not in isem.SEMANTIC_VECTOR_CACHE._data  # 最旧被逐出
    isem.SEMANTIC_VECTOR_CACHE.clear()


def test_build_chunks_large_material_sets():
    """分块回归(2026-09-23 真栈):素材 >64 条时按块多次 embed——真模型 178 条
    单发 514ms 撞查询道 400ms 超时令 build 恒 None;分块后每块 ~200ms 走装配
    期专属预算。同时钉死:总向量数与素材数一致(块序不丢不重)。"""
    import math

    n_kw = isem._BUILD_EMBED_CHUNK + 10  # 74 关键词 + label = 75 素材 → 2 块(64+11)
    kws = [f"kw{i:03d}" for i in range(n_kw)]
    label = f"label-{_ID_A}"  # _intent helper 的固定 label 形状
    doc = _doc_from(
        [_intent(_ID_A, keywords=kws)],
        [_binding(_BND_A, intent=_ID_A, action="jump_step", step=4)],
    )
    fake = FakeEmbedder({**{k: _E1 for k in kws}, label: _E1})

    async def _run() -> None:
        isem.SEMANTIC_VECTOR_CACHE.clear()
        idx = await IntentSemanticIndex.build(fake, doc)
        assert idx is not None
        import math

        assert fake.calls == math.ceil((n_kw + 1) / isem._BUILD_EMBED_CHUNK)
        m = await idx.match(kws[0], doc, step_1based=1, fired=set())
        assert m.intent_id == _ID_A  # 向量齐全,匹配照常
        isem.SEMANTIC_VECTOR_CACHE.clear()

    asyncio.run(_run())


# ---- 素材收集:label+keywords+judge.prompt 去重保序 ----


def test_material_texts_dedupe_and_keep_order():
    doc = _doc_from(
        [_intent(_ID_A, keywords=["同词", "kwA2"], judge="同词")],
        [_binding()],
    )
    texts = intent_material_texts(doc.intents[0])
    assert texts == ["label-int_1a2b3c4d", "同词", "kwA2"]


def test_sem_match_dataclass_defaults():
    s = SemMatch()
    assert s.intent_id == "" and s.score == 0.0 and s.best_text == "" and s.reason == ""
