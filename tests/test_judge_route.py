"""judge 路由字段:route/conf 防御式解析 + prompt 扩展开关(漏斗 v2 P0)。"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import JUDGE_ROUTES, build_judge_messages, parse_judge_output, parse_judge_route  # noqa: E402


def test_parse_route_full_line():
    route, conf = parse_judge_route("stay route=register_followup conf=0.8")
    assert route == "register_followup" and conf == 0.8


def test_parse_route_missing_defaults_keep():
    assert parse_judge_route("advance") == ("keep", 0.0)
    assert parse_judge_route("") == ("keep", 0.0)


def test_parse_route_invalid_falls_back():
    assert parse_judge_route("stay route=nonsense conf=0.5")[0] == "keep"
    # conf 越界夹取;垃圾值回落 0.0
    assert parse_judge_route("stay route=keep conf=9")[1] == 1.0
    assert parse_judge_route("stay route=keep conf=abc")[1] == 0.0


def test_route_vocab():
    assert JUDGE_ROUTES == frozenset({"keep", "degrade_question", "capture_contact", "register_followup", "transfer_human"})


def test_prompt_route_enabled():
    kw = dict(current_index=1, total=6, overview_lines=[], goal="g", ref="r", next_goal="n", user_text="u", facts=None)
    assert "route=" in build_judge_messages(**kw, route_enabled=True)[0]["content"]
    assert "route=" not in build_judge_messages(**kw)[0]["content"]


def test_parse_judge_output_backcompat():
    assert parse_judge_output("advance") == "confirm"
    assert parse_judge_output("objection") == "objection"
    assert parse_judge_output("stay") == "unclear"
