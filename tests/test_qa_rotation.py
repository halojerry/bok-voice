"""多答案轮换(Phase 3.2):折组/组分/轮换/kill/零变化(spec §2)。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "packages" / "core"))


def _e(qid, q, head="", **kw):
    return {"id": qid, "question_text": q, "answer_text": f"ans-{qid}",
            "lang": "zh", "scope": "global", "cluster_head_id": head, **kw}


def test_match_folds_variant_into_head(monkeypatch):
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from agent_runtime import qa_gate

    entries = [_e("head", "怎么退款"), _e("v1", "怎么退款啊", head="head"), _e("v2", "退款咋弄", head="head")]
    idx = qa_gate.QaIndex(entries)
    hit, score = idx.match("怎么退款啊")   # v1 分最高
    assert hit["id"] == "head"             # 折组:变体命中 → head 代表出场
    assert score > 0                        # 组分=成员最高分
    assert [m["id"] for m in idx.cluster_members("head")] == ["head", "v1", "v2"]


def test_orphan_variant_stays_standalone(monkeypatch):
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from agent_runtime import qa_gate

    entries = [_e("orphan", "怎么退款", head="ghost")]  # head 不在场
    hit, _ = qa_gate.QaIndex(entries).match("怎么退款")
    assert hit["id"] == "orphan"  # 孤儿退独立条目(旧行为)


def test_pick_rotation_least_played_then_order():
    from agent_runtime.qa_gate import pick_rotation_member

    ms = [_e("head", "q"), _e("v1", "q", head="head"), _e("v2", "q", head="head")]
    assert pick_rotation_member(ms, [])["id"] == "head"        # 零账本 → 插入序首
    assert pick_rotation_member(ms, ["head"])["id"] == "v1"    # head 播过 1 次 → v1
    assert pick_rotation_member(ms, ["head", "v1"])["id"] == "v2"
    assert pick_rotation_member(ms, ["head", "v1", "v2"])["id"] == "head"  # 全 1 次 → 回队首


def test_killswitch_naked_variants(monkeypatch):
    monkeypatch.setenv("BOK_QA_ROTATION", "0")
    from agent_runtime import qa_gate

    entries = [_e("head", "怎么退款"), _e("v1", "怎么退款啊", head="head")]
    hit, _ = qa_gate.QaIndex(entries).match("怎么退款啊")
    assert hit["id"] == "v1"  # 旧档:变体自己赢、播自己


def test_no_clusters_byte_identical(monkeypatch):
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from agent_runtime import qa_gate

    # 无簇配置(Phase 1 之前的世界)→ 折组零效应
    entries = [_e("a", "怎么退款", priority=10), _e("b", "怎么退款", priority=1)]
    assert qa_gate.QaIndex(entries).match("怎么退款")[0]["id"] == "b"  # 3.1 优先级键不受影响


# ---- kill-switch 开关本体 ----

def test_qa_rotation_enabled_default_on_and_kill(monkeypatch):
    from agent_runtime.qa_gate import qa_rotation_enabled

    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    assert qa_rotation_enabled() is True          # 默认开(Phase 3.2 翻档,spec §2)
    monkeypatch.setenv("BOK_QA_ROTATION", "1")
    assert os.environ.get("BOK_QA_ROTATION") == "1"   # 防 monkeypatch 假绿
    assert qa_rotation_enabled() is True
    monkeypatch.setenv("BOK_QA_ROTATION", "0")
    assert qa_rotation_enabled() is False
    monkeypatch.setenv("BOK_QA_ROTATION", "yes")  # 脏值=关(严格 "1",与其余开关同款)
    assert qa_rotation_enabled() is False


# ---- 簇成员表(cluster_members) ----

def test_cluster_members_resolves_variant_id_and_returns_copy(monkeypatch):
    """接口契约:传 head id 得整簇;传变体 id 归一- 到簇头(by_id 等调用面可能
    传变体);无簇条目 → 单元素表(调用方按 len>1 判);陌生 id → []。返回值是
    副本——调用方 mutate 唔可以污染索引(否则下一轮轮换序漂移)。"""
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from agent_runtime.qa_gate import QaIndex

    entries = [_e("head", "怎么退款"), _e("v1", "怎么退款啊", head="head"), _e("solo", "运费多少")]
    idx = QaIndex(entries)
    assert [m["id"] for m in idx.cluster_members("head")] == ["head", "v1"]
    assert [m["id"] for m in idx.cluster_members("v1")] == ["head", "v1"]
    assert [m["id"] for m in idx.cluster_members("solo")] == ["solo"]
    assert idx.cluster_members("ghost") == []
    assert idx.cluster_members("") == []
    members = idx.cluster_members("head")
    members.append(_e("evil", "怎么退款"))
    assert [m["id"] for m in idx.cluster_members("head")] == ["head", "v1"]  # 索引未被污染


def test_head_unusable_or_selfref_leaves_variant_standalone(monkeypatch):
    """head 在库但不可命中(空问法→不进索引)/ 自指 id → 不建组,变体退孤儿。"""
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from agent_runtime.qa_gate import QaIndex

    # head 空问法:索引装配时被跳过 → 变体无组可折
    entries = [{"id": "head", "question_text": "", "answer_text": "h", "lang": "zh",
                "scope": "global", "cluster_head_id": ""},
               _e("v1", "怎么退款", head="head")]
    idx = QaIndex(entries)
    assert idx.cluster_members("head") == []
    assert idx.match("怎么退款")[0]["id"] == "v1"

    # 自指 cluster_head_id(病态数据)→ 不建组、不折组
    solo = _e("solo", "怎么退款", head="solo")
    idx2 = QaIndex([solo])
    assert [m["id"] for m in idx2.cluster_members("solo")] == ["solo"]
    assert idx2.match("怎么退款")[0]["id"] == "solo"


# ---- 轮换取员纯函数契约 ----

def test_pick_rotation_member_empty_raises_and_is_pure():
    from agent_runtime.qa_gate import pick_rotation_member

    with pytest.raises(ValueError):      # 空表=契约违约(上游 len>1 门已挡),响亮不静默
        pick_rotation_member([], [])
    ms = [_e("head", "q"), _e("v1", "q", head="head")]
    played = ["head"]
    before = list(played)
    assert pick_rotation_member(ms, played)["id"] == "v1"
    assert played == before                          # 只读账本,绝不 mutate
    assert [m["id"] for m in ms] == ["head", "v1"]   # 不动成员序
    # 账本里的陌生 id(异簇历史/清理后残留)不参与本簇计数
    assert pick_rotation_member(ms, ["zzz", "zzz"])["id"] == "head"


# ---- 与 3.1 优先级键的合成:折组只换「出场代表」 ----

def test_fold_keeps_winner_key_and_score(monkeypatch):
    """3.1 组合保证:胜者键(priority asc, -score, 插入序)逐字节不变,折组只把
    变体胜者的返回换到 head——分数仍是变体的分;kill 档回变体自身。"""
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from agent_runtime.qa_gate import QaIndex

    # A 档:同优先级 → 高分变体胜(v1 问法=原话,分 1.0;head 问法异形,分低)
    # → 折组返回 head,分=变体分
    low_head = [_e("head", "退款要怎么弄啊"), _e("v1", "怎么退款", head="head")]
    idx = QaIndex(low_head)
    hit, score = idx.match("怎么退款", threshold=0.3)
    assert hit["id"] == "head" and score > 0.99
    monkeypatch.setenv("BOK_QA_ROTATION", "0")
    hit_kill, score_kill = idx.match("怎么退款", threshold=0.3)
    assert hit_kill["id"] == "v1" and score_kill == score   # 同分:折组不改分
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)

    # B 档:head priority 更小(3.1 胜者键)→ head 自己胜,返回原样、分=head 低分
    # (高分变体唔会经轮换被拖出来:折组只在变体已胜时换代表)
    pinned_head = [_e("head", "退款要怎么弄啊", priority=1), _e("v1", "怎么退款", head="head", priority=10)]
    hit_h, score_h = QaIndex(pinned_head).match("怎么退款", threshold=0.3)
    assert hit_h["id"] == "head" and score_h < 0.99


def test_no_clusters_rotation_on_off_identical(monkeypatch):
    """零变化铁律(逐字节口径):无簇配置下 rotation 开/关胜者与分数全同。"""
    from agent_runtime.qa_gate import QaIndex

    entries = [
        _e("a", "怎么退款", priority=10),
        _e("b", "怎么退款", priority=1),
        {"id": "c", "question_text": "运费多少", "answer_text": "8元", "lang": "zh", "scope": "global"},
    ]
    queries = ["怎么退款", "运费多少", "运费多少啊", "今天天气怎么样", ""]
    monkeypatch.setenv("BOK_QA_ROTATION", "0")
    off = [(h["id"] if h else None, s) for h, s in (QaIndex(entries).match(q) for q in queries)]
    monkeypatch.setenv("BOK_QA_ROTATION", "1")
    on = [(h["id"] if h else None, s) for h, s in (QaIndex(entries).match(q) for q in queries)]
    assert on == off


# ---- C1(终审 Critical):折组/轮换必须遵守 match 的 lang/scope 过滤 ----

def test_fold_and_rotation_obey_step_scope(monkeypatch):
    """C1(a) 跨 scope:head(global)+变体(scope=step,step_index=3)。

    第 5 步时变体必须被滤出折组/轮换池(否则第 3 步答案在第 5 步播);
    第 3 步才纳入。无步骤上下文(过滤档 step_index=None)=步作用域成员滤出。
    """
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from agent_runtime.qa_gate import QaIndex

    idx = QaIndex([
        _e("head", "怎么退款"),
        _e("v3", "怎么退款啊", head="head", scope="step", step_index=3),
    ])
    assert [m["id"] for m in idx.cluster_members("head", step_index=5)] == ["head"]
    assert [m["id"] for m in idx.cluster_members("head", step_index=3)] == ["head", "v3"]
    assert [m["id"] for m in idx.cluster_members("head", lang="zh", step_index=None)] == ["head"]

    hit5, _ = idx.match("怎么退款啊", lang="zh", step_index=5, threshold=0.3)  # 变体不在命中面
    assert hit5["id"] == "head"
    assert [m["id"] for m in idx.cluster_members(hit5["id"], lang="zh", step_index=5)] == ["head"]

    hit3, _ = idx.match("怎么退款啊", lang="zh", step_index=3, threshold=0.3)  # 变体满分胜 → 折组
    assert hit3["id"] == "head"
    assert [m["id"] for m in idx.cluster_members(hit3["id"], lang="zh", step_index=3)] == ["head", "v3"]


def test_fold_obeys_lang_filter(monkeypatch):
    """C1(b) 跨语言:head(en)+变体(zh)——zh 通话折到 zh 变体(非 en head);
    en 通话折到 en head(zh 变体被语言闸滤出)。"""
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from agent_runtime.qa_gate import QaIndex

    idx = QaIndex([
        _e("head_en", "how to refund", lang="en"),
        _e("v_zh", "怎么退款", lang="zh", head="head_en"),
    ])
    hit_zh, _ = idx.match("怎么退款", lang="zh")
    assert hit_zh["id"] == "v_zh"        # en head 被语言闸滤掉 → 代表=zh 变体
    assert [m["id"] for m in idx.cluster_members("head_en", lang="zh")] == ["v_zh"]
    hit_en, _ = idx.match("how to refund", lang="en")
    assert hit_en["id"] == "head_en"     # en 通话:head 幸存居首
    assert [m["id"] for m in idx.cluster_members("head_en", lang="en")] == ["head_en"]


def test_all_members_filtered_returns_naked_winner(monkeypatch):
    """C1(c) 全滤光 → 裸胜者(防御契约)。

    合法流中胜者必是成员(它自己就幸存),「簇全滤光」只在过滤面/命中面分家
    时出现;压 _team_head=None 验证 match 的兜底守卫——裸胜者原样返回,绝不
    静默丢弃。附带真实过滤路径佐证:全成员异语 → 过滤后空簇。
    """
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from agent_runtime.qa_gate import QaIndex

    idx = QaIndex([_e("head", "怎么退款"), _e("v1", "怎么退款啊", head="head")])
    monkeypatch.setattr(QaIndex, "_team_head", lambda self, *a, **k: None)
    hit, score = idx.match("怎么退款啊")
    assert hit["id"] == "v1" and score > 0

    idx2 = QaIndex([_e("h2", "怎么退款", lang="en"), _e("v2", "怎么退款啊", lang="en", head="h2")])
    assert idx2.cluster_members("h2", lang="zh") == []


# ---- 轮换序(命中恒 head,出场成员轮换)——spec §2 验收的纯函数版 ----

def test_rotation_sequence_head_stable_members_rotate(monkeypatch):
    monkeypatch.delenv("BOK_QA_ROTATION", raising=False)
    from agent_runtime.qa_gate import QaIndex, pick_rotation_member

    idx = QaIndex([
        _e("head", "怎么退款"),
        _e("v1", "怎么退款啊", head="head"),
        _e("v2", "退款咋弄", head="head"),
    ])
    played: list[str] = []
    seq: list[str] = []
    for _ in range(4):
        hit, score = idx.match("怎么退款啊")
        assert hit["id"] == "head" and score > 0          # 折组:恒 head 代表出场
        member = pick_rotation_member(idx.cluster_members(hit["id"]), played)
        seq.append(member["id"])
        played.append(member["id"])                        # 模拟真播出后记账
    assert seq == ["head", "v1", "v2", "head"]
    assert len(set(seq)) >= 2                              # 去重 ≥2(同答不再连播)


# ---- agent 装配点纯函数缝:出场序 + 账本 ----

def test_qa_rotation_plan_single_member_is_legacy_path():
    """无簇/单成员(=kill-switch 档调用点传 [])→ [entry] 原路逐字节同旧。"""
    from agent_runtime.agent import _qa_rotation_plan

    head = _e("head", "怎么退款")
    v1 = _e("v1", "怎么退款啊", head="head")
    assert _qa_rotation_plan(head, [], []) == [head]        # kill-switch:簇根本唔查
    assert _qa_rotation_plan(head, [head], []) == [head]    # 无簇条目
    assert _qa_rotation_plan(v1, [v1], ["v1"]) == [v1]      # 孤儿变体(旧行为)
    assert _qa_rotation_plan(head, [head], ["head", "head"]) == [head]


def test_qa_rotation_plan_multi_member_walks_rotation_order():
    """多成员簇 → 轮换序(反复取 pick_rotation_member = 同单一序源,防两处漂移),
    供「首员无 PCM 沿序下移」用。"""
    from agent_runtime.agent import _qa_rotation_plan
    from agent_runtime.qa_gate import pick_rotation_member

    ms = [_e("head", "q"), _e("v1", "q", head="head"), _e("v2", "q", head="head")]

    def ids(played: list[str]) -> list[str]:
        return [m["id"] for m in _qa_rotation_plan(ms[0], ms, played)]

    assert ids([]) == ["head", "v1", "v2"]
    assert ids(["head"]) == ["v1", "v2", "head"]           # 已播在末位(下移兜底最後才重播)
    assert ids(["head", "head"]) == ["v1", "v2", "head"]
    assert ids(["v1"]) == ["head", "v2", "v1"]
    # 首员与接口逐字一致(单序源)
    for played in ([], ["head"], ["head", "v1"], ["v1", "v2"]):
        assert _qa_rotation_plan(ms[0], ms, played)[0] is pick_rotation_member(ms, played)


def test_qa_note_played_ledger_cap_and_guards():
    """账本记账:真播出才调(调用点在装配处),容量 64 截断防无界;坏 id 归一空串。"""
    from agent_runtime.agent import _QA_PLAYED_CAP, _qa_note_played

    played: list[str] = []
    _qa_note_played(played, "head")
    _qa_note_played(played, None)
    assert played == ["head", ""]
    for i in range(_QA_PLAYED_CAP * 2):
        _qa_note_played(played, f"v{i}")
    assert len(played) == _QA_PLAYED_CAP
    assert played[-1] == f"v{_QA_PLAYED_CAP * 2 - 1}"      # 近况保留(队首丢最老)


def test_agent_wiring_gates_rotation_and_leaves_graph_branch_alone():
    """装配点契约(源码级,防静默回退):
    ①快路分支必须带 qa_rotation_enabled 闸 + 出场序/记账调用——漏闸即无簇也轮换;
    ②graph by_id 分支(运营钉死条目 id)必须零轮换调用(spec §2:轮换只作用 match 命中)。
    """
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    fastpath = src.split("# ---- Q→A 检索快路", 1)[1]
    assert "qa_rotation_enabled()" in fastpath
    assert "_qa_rotation_plan(_qa_entry" in fastpath
    assert "_qa_note_played(flow_ctrl.qa_played" in fastpath
    graph_block = src.split("# ---- 话术图引擎", 1)[1].split("# ---- Q→A 检索快路", 1)[0]
    assert "cluster_members" not in graph_block
    assert "_qa_rotation_plan" not in graph_block
    # 2026-09-19 审计 P2-12:记账解禁——图 by_id 播放真出声后补记 qa_played
    # (旧版连记账一并禁入,同簇条目再被快路轮换选中=同通同段罐头播两遍);
    # 取员/出场序仍禁入,轮换选取语义(spec §2 graph 分支零轮换)原样钉死。
    assert "pick_rotation_member" not in graph_block
