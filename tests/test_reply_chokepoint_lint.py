"""回复车道 chokepoint lint（EX-2，2026-09-28）。

背景：每通共享状态（账本 gen/provider、重复锚、看门狗、stall 抵销）原先由
~26 条出口车道各自手写 `_turn_origin[...]`（17 处）/`set_last_reply`（9 处）/
`_cancel_response_watchdog`（15 处）——框架只串行 turn 钩子，分支打铃/跳步这类
「只写 provider、等下一个 LLM item」的车道在 item 被打断时 provider 泄漏进下一轮
（call-35adfa90）。EX-2 统一为 `_register_reply_lane` chokepoint + FIFO 票据消费。

本测试用 AST 全量扫描 `apps/agent/agent_runtime/**/*.py`，把上述手工写限制在
显式 (file, enclosing-function) 白名单内——新车道若绕过 chokepoint 直接手写即红。
同仓源级 pin 风格见 tests/test_agent_turn_timing.py / test_intent_judge_wiring.py。
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "apps" / "agent" / "agent_runtime"
AGENT_SRC = (AGENT_DIR / "agent.py").read_text(encoding="utf-8")

# ---- 白名单（显式数据，每条带理由）----
# 判据 = (所在文件名, 最近的 enclosing function 名)。"<module>" = 模块级代码。
_ALLOW_TURN_ORIGIN: set[tuple[str, str]] = set()  # EX-2 后单槽 _turn_origin 已删除

_ALLOW_SET_LAST_REPLY: dict[tuple[str, str], str] = {
    # chokepoint 本体：预写重复锚（anchor=True 车道；中央 sink 再以 lecture_guard 精修锚）
    ("agent.py", "_register_reply_lane"): "chokepoint 预锚",
    # 中央锚 sink：item_added 到达时以 lecture_guard/strip_voice_style 版精修锚
    ("agent.py", "_on_item_for_context"): "中央锚 sink",
    # ContextState.set_last_reply 定义处（livekit_plugins）
    ("livekit_plugins.py", "set_last_reply"): "ContextState 锚定义",
}

_ALLOW_CANCEL_WATCHDOG: dict[tuple[str, str], str] = {
    # chokepoint 本体：cancel_watchdog=True 车道统一拆弹
    ("agent.py", "_register_reply_lane"): "chokepoint 拆弹",
    # on_user_turn_completed 内的「有意静默」拆弹（自听回声/纯回声轮/暂停/风暴
    # 静听/暂存等续段）——这些不是回复车道，保留直调（EX-2 明确允许）。
    ("agent.py", "on_user_turn_completed"): "有意静默路径（非回复车道）",
    # FIX-3(D2-4)：复读防线全吞收尾回调——响应发生过，静默是刻意决定；
    # 拆看门狗防 6s 兜底强断念道歉（非回复车道：无 item/无出声）。
    ("agent.py", "_repeat_full_swallow"): "全吞有意静默（非回复车道）",
    # 看门狗武装点：arm 前先拆旧 timer（watchdog 定义/arm site 豁免）
    ("agent.py", "_arm_response_watchdog"): "武装点拆旧 timer",
}

_ALLOW_REPEAT_REQUESTED: dict[tuple[str, str], str] = {
    # 复问/REPEAT 放行闸唯一写入点（EX-2 re-ask gate 所在函数）
    ("agent.py", "on_user_turn_completed"): "re-ask gate",
}

# `_reply_done_event.set()` 站点清单（P3，2026-10-02 补面）。
# 语义：judge 让路链（第七波）靠该事件感知「本轮回复已交付/放弃」，站点散落
# 各出口车道——20 站点逐一审计后固化于此。**双向钉**：新增站点（新车道出口）
# 或删除既有站点（漏 set=judge 白等等待帽）都会使清单失配 → 红，逼一次显式
# 修订（新出口必须在交付/静默放弃两条边上都 set，否则 judge 被饥饿）。
# 判据 = (文件名, 最近的 enclosing function 名) → 该函数内 `.set()` 次数。
# 基线=HEAD 78e17e1（S1 的 P0-P3 未提交改动合流后按实际重核一次）。
_ALLOW_REPLY_DONE_SET: dict[tuple[str, str], int] = {
    # 中央交付点：assistant turn 上报（框架 speech 交付）后置位
    ("agent.py", "_report_assistant_turn"): 1,
    # 会话收尾：on_close 全量解除等待
    ("agent.py", "_on_close"): 1,
    # turn 钩子内的出口车道（10 处：打断/风暴/暂停/静默放弃等，EX-2 审计集）
    # W2 刀4（2026-10-08）+1=11：burst-merge 连发窗——合并补答在途（窗尾
    # generate_reply 一次性应答窗内全部轮），本轮 StopResponse=「交付在途的
    # 放弃边」，judge 须放行。
    ("agent.py", "on_user_turn_completed"): 11,
    # 打断 watcher（speech_created 触发的放弃边；第七波审计漏网、十二波补洞）
    ("agent.py", "_watch"): 1,
    # FIX-3（2026-10-02 批3 合流）：复读全吞=有意静默的放弃边——judge 不等
    # 一条被吞的复读（全吞上抛处置完即解除等待）。
    ("agent.py", "_repeat_full_swallow"): 1,
}


def _iter_trees():
    for path in sorted(AGENT_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path.name, ast.parse(path.read_text(encoding="utf-8"))


def _walk_with_func(tree: ast.AST, funcname: str = "<module>"):
    """yield (node, nearest_enclosing_function_name)。"""
    for child in ast.iter_child_nodes(tree):
        name = funcname
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = child.name
        yield child, name
        yield from _walk_with_func(child, name)


def _sites():
    """收集五类受限写入/调用的 (kind, filename, funcname)。"""
    out: list[tuple[str, str, str]] = []
    for fname, tree in _iter_trees():
        for node, func in _walk_with_func(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "_turn_origin"
                    ):
                        out.append(("turn_origin", fname, func))
                    if isinstance(tgt, ast.Attribute) and tgt.attr == "repeat_requested":
                        out.append(("repeat_requested", fname, func))
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == "set_last_reply":
                    out.append(("set_last_reply", fname, func))
                if isinstance(f, ast.Name) and f.id == "_cancel_response_watchdog":
                    out.append(("cancel_watchdog", fname, func))
                if (
                    isinstance(f, ast.Attribute)
                    and f.attr == "set"
                    and (
                        (isinstance(f.value, ast.Name) and f.value.id == "_reply_done_event")
                        or (
                            isinstance(f.value, ast.Attribute)
                            and f.value.attr == "_reply_done_event"
                        )
                    )
                ):
                    out.append(("reply_done_set", fname, func))
    return out


def test_no_stray_turn_origin_writes():
    bad = [(f, fn) for (k, f, fn) in _sites() if k == "turn_origin"]
    assert not bad, f"EX-2 后 _turn_origin 单槽已删除，仍有手写：{bad}"


def test_set_last_reply_only_in_allowlist():
    allow = set(_ALLOW_SET_LAST_REPLY)
    bad = [(f, fn) for (k, f, fn) in _sites() if k == "set_last_reply" and (f, fn) not in allow]
    assert not bad, f"set_last_reply 只准在 chokepoint/中央 sink 写（EX-2 统一锚），越权点：{bad}"


def test_cancel_watchdog_only_in_allowlist():
    allow = set(_ALLOW_CANCEL_WATCHDOG)
    bad = [(f, fn) for (k, f, fn) in _sites() if k == "cancel_watchdog" and (f, fn) not in allow]
    assert not bad, f"_cancel_response_watchdog 只准在 chokepoint/豁免点调，越权点：{bad}"


def test_repeat_requested_only_in_allowlist():
    allow = set(_ALLOW_REPEAT_REQUESTED)
    bad = [(f, fn) for (k, f, fn) in _sites() if k == "repeat_requested" and (f, fn) not in allow]
    assert not bad, f"repeat_requested 只准在 re-ask gate 写，越权点：{bad}"


def test_reply_done_set_sites_match_manifest():
    """`_reply_done_event.set()` 站点清单双向钉（P3）。

    方向一（新增即红）：新出口车道加了 set 站点而不改清单 → 此处报未知站点；
    方向二（删除即红）：既有 set 被误删（judge 白等等待帽/饿死）→ 计数失配。
    修订姿势：动清单必须同时说明该出口的交付/静默放弃语义（本文件注释）。
    """
    live = Counter((f, fn) for (k, f, fn) in _sites() if k == "reply_done_set")
    expected = Counter(_ALLOW_REPLY_DONE_SET)
    assert live == expected, (
        "reply_done_event.set() 站点与清单不符——新增站点(新车道出口)或删除既有"
        f"站点均需显式修订清单。live={dict(live)} expected={dict(expected)}"
    )


def test_reply_done_set_declaration_and_clear_are_single():
    """声明点与 clear 点各恰一处（协议面锚：跨函数泄漏/双 Event 即红）。"""
    declared = 0
    cleared: list[tuple[str, str]] = []
    for fname, tree in _iter_trees():
        for node, func in _walk_with_func(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "_reply_done_event":
                        declared += 1
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "clear"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_reply_done_event"
            ):
                cleared.append((fname, func))
    assert declared == 1, f"_reply_done_event 声明点应恰 1 处：{declared}"
    assert cleared == [("agent.py", "on_user_turn_completed")], cleared
