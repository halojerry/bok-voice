"""judge 的 **token 预算**契约（2026-09-21 实测修复，漏斗 v2 建单从未触发过）。

## 病灶

route 模式下 judge 的输出契约是**两行**（`flow.build_judge_messages`）：
第一行 `advance/stay/objection`，第二行 `route=X conf=0.0~1.0`。而
`agent._llm_judge` 的 **max_tokens 缺省 8**，route 调用点又没显式放宽——8 token
只够第一行加半个 route 行。2026-09-21 对三个后端逐一实测（同一组真 judge
messages、`user_text='我要投诉件货延误啊，你哋搞乜㗎'`）：

| 后端 | max_tokens=8 | max_tokens=24 |
| --- | --- | --- |
| 本机 9B（:1237） | `'stay\\nroute=register_followup conf'` → conf **0.00** | `conf=0.8` |
| deepseek-flash | `'stay\\nroute=register_followup'` → conf **0.00** | `conf=0.8` |
| deepseek-v4-pro | `'stay\\nroute=register_followup'` → conf **0.00** | `conf=0.8` |

`parse_judge_route` 在 conf 缺失时保守返 0.0（2026-09-20 的加固，本身是对的），
于是 `conf < FOLLOWUP_CONF_MIN(0.7)` **恒成立**——`register_followup` 建单动作
**在生产里从未触发过**，`degrade_boost` 同时失去置信信号。`BOK_ROUTE_JUDGE`
缺省就是 `"1"`，所以这是活着的缺陷，不是潜在缺陷。

## 为什么既有测试没抓到

`test_judge_route.py` 全部把**完整字符串**喂给解析器（`"stay route=register_followup
conf=0.8"`），测的是解析器这一侧；从来没有人问「运行时的模型真的会吐出这一行吗」。
所以本文件补的是**缝到线的距离**：预算要撑得住契约。

## 边界

本文件不测模型行为（那要真端点），只钉两件事：①预算常量够大（按实测的 24 钉住，
含余量理由）；②route 调用点真的把预算传下去了（结构化锚，防有人改回缺省 8）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.flow import (  # noqa: E402
    FOLLOWUP_CONF_MIN,
    JUDGE_MAX_TOKENS,
    JUDGE_ROUTE_MAX_TOKENS,
    JUDGE_ROUTES,
    parse_judge_route,
)


def _route_line(route: str) -> str:
    """route 模式下模型要吐的那两行（用最长的 route id 算最坏情况）。"""
    return f"stay\nroute={route} conf=0.8"


def test_route_budget_is_larger_than_single_line_budget():
    assert JUDGE_ROUTE_MAX_TOKENS > JUDGE_MAX_TOKENS


def test_route_budget_survives_the_two_line_contract():
    """8 token 下三后端都被截在 conf 之前——预算必须留得下整条 route 行。

    实测判据：本机 9B 在 8 token 下正好吐到 `...conf`（第 8 个 token 落在 conf 上），
    24 token 三后端都完整。这里用「最坏 route id 的字符面」做下界，再压掉实测的
    8-token 截断点，避免有人把常量改小回去。
    """
    worst = max(_route_line(r) for r in JUDGE_ROUTES)
    # 8 token 只够覆盖到 conf 之前（实测 `route=register_followup` 处即耗尽），
    # 故预算必须显著大于「8 token 能覆盖的字符数」——这里用最坏行的字符数/3
    # 做保守下界（中英混排 BPE 实测 ≳3 字符/token 是过乐观的，故只当下界用）。
    assert JUDGE_ROUTE_MAX_TOKENS >= 16, "预算回到 8 档会让 conf 再次结构性解析不出来"
    assert len(worst) / 3 <= JUDGE_ROUTE_MAX_TOKENS


def test_truncated_route_line_cannot_pass_the_followup_gate():
    """把实测的截断面固化成回归证据：截断 → conf 0.00 → 过不了建单闸。"""
    truncated_9b = "stay\nroute=register_followup conf"
    truncated_cloud = "stay\nroute=register_followup"
    for raw in (truncated_9b, truncated_cloud):
        route, conf = parse_judge_route(raw)
        assert route == "register_followup"  # route 解析得到（所以日志看起来「正常」）
        assert conf == 0.0
        assert conf < FOLLOWUP_CONF_MIN  # 建单闸恒不触发
    # 完整行才够格
    route, conf = parse_judge_route("stay\nroute=register_followup conf=0.8")
    assert route == "register_followup" and conf >= FOLLOWUP_CONF_MIN


def test_flow_judge_call_site_passes_route_budget():
    """结构化锚：route 调用点必须按 route_enabled 选档，不得回退缺省 8。"""
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert re.search(
        r"max_tokens=JUDGE_ROUTE_MAX_TOKENS if route_enabled else JUDGE_MAX_TOKENS", src
    ), "flow judge 调用点的预算选档被改掉了——conf 会重新变回恒 0.00"
    # 常量必须真从 flow 导入（agent.py 有多处 `from .flow import`，按块匹配）。
    assert re.search(r"from \.flow import \([^)]*JUDGE_ROUTE_MAX_TOKENS", src), (
        "flow judge 的预算常量没从 flow 导入"
    )


def test_route_prompt_asks_for_two_lines():
    """契约本身是两行——预算是为它留的，不是为单行 verdict 留的。"""
    from agent_runtime.flow import build_judge_messages

    msgs = build_judge_messages(
        current_index=3,
        total=6,
        overview_lines=["1", "2", "3"],
        goal="g",
        ref="r",
        next_goal="n",
        user_text="u",
        facts={},
        route_enabled=True,
    )
    blob = "\n".join(m["content"] for m in msgs)
    assert "route=X conf=0.0~1.0" in blob
    # 非 route 模式仍是一行契约（预算 8 不动）
    msgs2 = build_judge_messages(
        current_index=3,
        total=6,
        overview_lines=["1", "2", "3"],
        goal="g",
        ref="r",
        next_goal="n",
        user_text="u",
        facts={},
        route_enabled=False,
    )
    assert "conf" not in "\n".join(m["content"] for m in msgs2)
