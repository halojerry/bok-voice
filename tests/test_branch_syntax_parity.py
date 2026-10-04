"""分支语法四实现 parity（2026-10-04 C1）——「如果客戶」繁体锚并收的契约钉。

背景:分支行正则曾有四份拷贝各自漂移——运行时 flow.py 只认简体锚,种子粤语
模板的「如果客戶」分支整层静默失效(probe_s2s_vs_cascade 实弹记录),而训练料
拷贝先一步收了繁体=训练/运行口径劈叉。本测试族钉三件事:

1. 单源:gap_proposals / prepare_csc_data / flow 三处 Python 消费方吃同一份
   bok_voice_core.branch_syntax(对象同一,不是字面镜像);
2. 行为 parity:简/繁/EN 分支行与简/繁动作标记在 flow.parse_step_ref /
   parse_branch_action 上行为一致(繁体分支从静默失效变生效=拍板的行为变化);
3. TS 镜像钉:lib/flow-canvas.ts 无法 import Python,由源级 pin 钉住锚词与
   动作标记字面集与本模块一致(改一处不改另一处即红)。

种子回归:scripts/seed/seed_invite_templates.py 的三语模板里每条分支行都必须被
parse_step_ref 认出(命中 100%,不再有静默丢弃)。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from agent_runtime import flow
from bok_voice_core import branch_syntax

ROOT = Path(__file__).resolve().parents[1]

# (行, 期望(cond,resp) 或 None=不认)
BRANCH_LINE_FIXTURES = [
    # 简体锚(既有行为,零漂移)
    ("如果客户不是本人→麻烦您转告他", ("不是本人", "麻烦您转告他")),
    ("如果客户问要收费吗→对,全程免费", ("问要收费吗", "对,全程免费")),
    # 繁体锚(2026-10-04 并收:此前整行被静默丢弃)
    ("如果客戶唔係本人→麻煩您話返俾佢聽", ("唔係本人", "麻煩您話返俾佢聽")),
    ("如果客戶問要唔要收費→係,全程免費", ("問要唔要收費", "係,全程免費")),
    ("如果客戶話最近都冇時間→唔緊要", ("話最近都冇時間", "唔緊要")),
    # EN 锚(大小写不敏感;箭头恒为「→」)
    ("if the customer asks about fees → yes, free", ("asks about fees", "yes, free")),
    ("When the customer refuses → ok", ("refuses", "ok")),
    # 不认形态
    ("客户说打错电话→复述确认", None),  # 无锚词行头
    ("如果客户→", None),  # 条件/应答空
]

# (resp, 期望(action, step, text))
BRANCH_ACTION_FIXTURES = [
    ("【收线】唔好意思打搅咗", ("refuse", 0, "唔好意思打搅咗")),
    ("【收線】唔好意思打搅咗", ("refuse", 0, "唔好意思打搅咗")),  # 繁体并收
    ("【挂断】拜拜", ("refuse", 0, "拜拜")),
    ("【掛斷】拜拜", ("refuse", 0, "拜拜")),
    ("【转人工】我帮您转接", ("handoff", 0, "我帮您转接")),
    ("【轉人工】我帮您转接", ("handoff", 0, "我帮您转接")),
    ("【留本步】好嘅", ("hold", 0, "好嘅")),
    ("【跳第3步】先办这个", ("jump", 3, "先办这个")),
    ("【跳第0步】x", ("", 0, "x")),
    ("无标记原样", ("", 0, "无标记原样")),
    ("", ("", 0, "")),
]


# ---- 1. 单源钉:三处 Python 消费方吃同一份 branch_syntax ----

def test_gap_proposals_eats_single_source():
    from control_plane import gap_proposals

    assert gap_proposals._BRANCH_COND_RE is branch_syntax.BRANCH_LINE_RE


def test_prepare_csc_data_eats_single_source():
    spec = importlib.util.spec_from_file_location(
        "_pinned_prepare_csc", ROOT / "scripts" / "seed" / "prepare_csc_data.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._BRANCH_LINE_RE is branch_syntax.BRANCH_LINE_RE
    assert mod._BRANCH_ACTION_RE is branch_syntax.BRANCH_ACTION_RE


def test_flow_reexports_are_single_source():
    assert flow._BRANCH_LINE_RE is branch_syntax.BRANCH_LINE_RE
    assert flow._NOTE_LINE_RE is branch_syntax.NOTE_LINE_RE
    assert flow._BRANCH_ACTION_RE is branch_syntax.BRANCH_ACTION_RE
    assert flow.parse_branch_action is branch_syntax.parse_branch_action


# ---- 2. 行为 parity:简/繁/EN 同判 ----

@pytest.mark.parametrize("line,expected", BRANCH_LINE_FIXTURES)
def test_branch_line_anchor_parity(line, expected):
    m = branch_syntax.BRANCH_LINE_RE.match(line)
    if expected is None:
        assert m is None
    else:
        assert m is not None, f"锚未命中(静默丢弃回归): {line}"
        assert (m.group("cond").strip(), m.group("resp").strip()) == expected


@pytest.mark.parametrize("resp,expected", BRANCH_ACTION_FIXTURES)
def test_branch_action_script_variant_parity(resp, expected):
    assert branch_syntax.parse_branch_action(resp) == expected


def test_parse_step_ref_extracts_traditional_branches():
    ref = "您好,請問係陳小姐嗎?\n如果客戶唔係本人→麻煩您話返俾佢聽。\n注意:一次只問一件事"
    parts = flow.parse_step_ref(ref)
    assert parts.branches == [("唔係本人", "麻煩您話返俾佢聽。")]
    assert parts.notes == ["一次只問一件事"]


def test_no_unparsed_directive_warning_for_traditional(capsys):
    # 繁体分支行不得再触发 ref_directive_unparsed 告警(此前=静默失效的观测面)。
    flow._UNPARSED_DIRECTIVE_WARNED.clear()
    flow.parse_step_ref("如果客戶唔係本人→麻煩您話返俾佢聽")
    assert "ref_directive_unparsed" not in capsys.readouterr().out


# ---- 3. TS 镜像源级 pin:锚词/动作标记字面集与 branch_syntax 一致 ----

def _ts_source() -> str:
    return (ROOT / "apps" / "web" / "lib" / "flow-canvas.ts").read_text(encoding="utf-8")


def test_ts_branch_re_contains_traditional_anchor():
    for rel in ("apps/web/lib/flow-canvas.ts", "apps/web/lib/var-panel.ts"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "如果客戶" in src, f"{rel} 缺繁体锚(与 branch_syntax 单源漂移)"
        assert "如果客户" in src


def test_ts_action_re_contains_traditional_kinds():
    src = _ts_source()
    for kind in ("收線", "掛斷", "轉人工"):
        assert kind in src, f"TS BRANCH_ACTION_RE 缺繁体标记 {kind}(与 branch_syntax 单源漂移)"


def test_no_local_branch_regex_copies_remain():
    # 权威源外不允许再出现自持拷贝(防第四/第五份拷贝回潮)。
    python_consumers = [
        ROOT / "apps" / "control-plane" / "control_plane" / "gap_proposals.py",
        ROOT / "scripts" / "seed" / "prepare_csc_data.py",
    ]
    literal = "如果客戶|(?:If|When)"
    for p in python_consumers:
        assert literal not in p.read_text(encoding="utf-8"), f"{p.name} 仍有自持分支正则拷贝"


# ---- 4. 种子模板离线回归:分支命中 100% ----

def _seed_templates():
    spec = importlib.util.spec_from_file_location(
        "_pinned_seed_templates", ROOT / "scripts" / "seed" / "seed_invite_templates.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TEMPLATES


def test_seed_templates_every_branch_line_recognized():
    total_branch_lines = 0
    for tpl in _seed_templates():
        for step in tpl.get("steps", []):
            for raw in str(step.get("ref") or "").splitlines():
                line = raw.strip()
                if not line or not line.startswith("如果客"):
                    continue
                total_branch_lines += 1
                assert branch_syntax.BRANCH_LINE_RE.match(line), (
                    f"种子分支行未被认出(静默失效回归): {tpl.get('language')}: {line}"
                )
    # 三语种子至少 10 条分支行(简体 zh + 繁体 canto + EN 模板各若干)。
    assert total_branch_lines >= 10


def test_seed_cantonese_traditional_branches_now_live():
    canto_branches: list[str] = []
    for tpl in _seed_templates():
        if str(tpl.get("language")) != "cantonese":
            continue
        for step in tpl.get("steps", []):
            parts = flow.parse_step_ref(str(step.get("ref") or ""))
            canto_branches.extend(parts.branches)
    # 粤语种子 4 条繁体分支从静默失效变生效(C1 行为变化的正面钉)。
    assert len(canto_branches) >= 4
