# QA 多答案轮换（Phase 3.2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 变体不再裸竞争——命中落在簇上（head 代表），簇内按「本通最少播放」轮换取条目播；`BOK_QA_ROTATION=0` 一键回旧档。

**Architecture:** 折组发生在 `QaIndex` 装配（变体并入 head 候选组、组分=成员最高分、孤儿变体退独立）；轮换是纯函数（ledger 驱动）；`FlowController.qa_played` 是本通账本（graph_fired 同款纪律：真播出才记）。graph `by_id` 路径（运营钉死条目 id）**不轮换**。设计契约见 [spec §2](../specs/2026-09-18-flow-graph-phase3.md)。

**Tech Stack:** Python 3.12 / pytest。无 DB 改动、无新列。

## Global Constraints

- **零变化铁律**：无簇配置（全部 cluster_head_id 空）的账号，命中/播放与今逐字节同——含 priority 胜者键（3.1 刚立）不变。
- kill-switch：`os.environ.get("BOK_QA_ROTATION", "1") == "1"`；=0 时 match 返回裸胜者（变体可自己赢、播自己的答案=旧行为）、无轮换。
- 记账纪律：member id 只在**实际播出**后 append 进 `qa_played`（`_qa_canned_say` 成功返回后）。
- 轮换只作用于 **match 命中**（QA 快路 + 探针）；`by_id`（graph play_qa 绑定）不轮换。
- 无 PCM 的成员跳过顺位下移；全组无 PCM → 照旧 `no_audio` 落 LLM。
- 术语门禁（唯一合法拼写 `cantonese`）；conventional commits scope `qa-rotation`。
- 工作树 `/Users/halo/Documents/bok/voice-assistant-worktrees/flow-graph-phase3`，分支 `qa-rotation-phase32`。解释器用主树 `.venv312`（工作树 venv 因网络未建，conftest 隔离已哨兵验证）。

---

### Task 1: 运行时 — 折组 + 轮换 + 账本

**Files:**
- Modify: `apps/agent/agent_runtime/qa_gate.py`（`QaIndex`：装配折组 + `cluster_members()`；模块级 `qa_rotation_enabled()`；纯函数 `pick_rotation_member()`）
- Modify: `apps/agent/agent_runtime/flow.py`（`FlowController.qa_played: list[str] = field(default_factory=list)`，注释同 graph_fired 风格）
- Modify: `apps/agent/agent_runtime/agent.py`（QA 快路命中分支：match 胜者经簇解析→轮换取 member→用 member 的 answer/PCM 播→成功才记账；graph by_id 分支不动）
- Test: `tests/test_qa_rotation.py`

**Interfaces（T2/审查依赖，逐字）:**
- `qa_rotation_enabled() -> bool`
- `QaIndex.cluster_members(head_id: str) -> list[dict]`（head 自身+其在场变体，按插入序；无簇/未知 id → `[entry] or []`）
- `pick_rotation_member(members: list[dict], played: list[str]) -> dict`（纯函数：按 `(played.count(id) asc, 插入序)` 取首；空列表 → ValueError 不准发生——调用方保证非空）
- `match()` 语义变更（kill-switch=1 时）：胜者是变体 → 返回 `(head_entry, score)`（组分不变，仍是该变体的分）；胜者本就是 head → 原样。=0 时逐字节同旧。

- [ ] **Step 1: 失败测试**（新文件，节选核心——完整形状由 TDD 现场补齐）

```python
"""多答案轮换(Phase 3.2):折组/组分/轮换/kill/零变化(spec §2)。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

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
```

- [ ] **Step 2: 红** — Run: `pytest tests/test_qa_rotation.py -v` → FAIL（无 cluster_members/pick_rotation_member，match 不折组）
- [ ] **Step 3: 实现** —
  - qa_gate：`qa_rotation_enabled()`；`QaIndex.__init__` 里 `self._clusters: dict[str, list[dict]]`（head_id → 成员表：head 在索引前提成立才建组；变体 head 不在 → 独立）；`match()` 胜者出线后：若 rotation 开且胜者 cluster_head_id 非空且其 head 在 `_items` → 返回 head（分数不变）；`cluster_members()`；`pick_rotation_member()`（纯函数放模块级，`played.count`）。
  - flow.py：`qa_played` 字段（graph_fired 注释风格）。
  - agent.py QA 快路命中分支（`_qa_entry is not None` 后、`_qa_pcm_for` 前）：`rotation 开 且 _qa_index 有该 entry 的簇（len>1）` → `members = _qa_index.cluster_members(...)`，`member = pick_rotation_member(members, flow_ctrl.qa_played)`，`_qa_answer/_qa_pcm` 按 member 解析，member 无 PCM 则沿轮换序下移尝试，全组无 PCM → 原样 no_audio 落 LLM；`_qa_canned_say` 成功（含 StopResponse 抛出前的 True 返回）后 `flow_ctrl.qa_played.append(member_id)`；`cp.qa_hit` 记 member id。graph by_id 分支零改动。
- [ ] **Step 4: 绿 + 零回归** — 新文件全绿 + `pytest tests -q` 全量（1477 基线）+ compileall
- [ ] **Step 5: Commit** — `feat(qa-rotation): cluster folding + least-played rotation + per-call ledger (BOK_QA_ROTATION)`

---

### Task 2: 探针腿 + 文档 + 验收

**Files:**
- Modify: `scripts/probe_qa_hit.py`（`--rotation-duel`：合成 head+2 变体，断言①首轮 match→head、pick→head ②账本 [head] 后 pick→v1 ③kill 档变体裸胜——纯离线）
- Modify: `AGENTS.md`（QA 快路段 3.1 注后追加 3.2 半句：折组语义/轮换账本/kill-switch/探针名）
- 验收：全量 pytest + web 不动（无改动免跑）+ duel 实跑 + Supabase 零改动（无新列）+ PR

- [ ] 红测 `tests/test_probe_qa_rotation.py`（duel 纯函数三断言）→ 实现 → 绿
- [ ] `--rotation-duel` 实跑 PASS
- [ ] AGENTS.md 半句 + plan 勘误（如有）
- [ ] 全量验收 + push + PR（base main）

---

## Self-Review

- Spec §2 覆盖：折组→T1、孤儿→T1、轮换账本→T1、kill→T1、by_id 不轮换→T1 agent 分支约束、探针→T2。零变化有专测（无簇+3.1 键交互）。无 DB 改动与 spec 一致。占位符无。
