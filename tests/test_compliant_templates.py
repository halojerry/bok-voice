"""合规版三语模板（data/templates/hegui-*.json）结构守卫（2026-10-03 批次3）。

B 稿 → 模板化产物：7 步 × 三语、分支行/注意行走 flow 同源正则解析、未识别
「→」指令零容忍（否则引擎静默丢弃=分支悄悄失效）、命名不含 e2e/probe
（探针自动选模依赖）。装载面 scripts/load_compliant_templates.py。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.flow import _BRANCH_LINE_RE, parse_step_ref, parse_steps  # noqa: E402

FILES = ("hegui-zh.json", "hegui-cantonese.json", "hegui-en.json")


def _load(name: str):
    data = json.loads((ROOT / "data" / "templates" / name).read_text(encoding="utf-8"))
    return data, json.dumps(data["steps_json"], ensure_ascii=False)


def test_templates_parse_and_branches_wellformed():
    for name in FILES:
        data, steps_json = _load(name)
        steps = parse_steps(steps_json)
        assert len(steps) == 7, f"{name}: 期望 7 步,实得 {len(steps)}"
        for st in steps:
            parts = parse_step_ref(st.ref)
            assert parts.script, f"{name}: step {st.goal!r} 缺正稿"
            assert parts.notes, f"{name}: step {st.goal!r} 缺注意行"
            for cond, resp in parts.branches:
                assert 1 <= len(cond) <= 120, (name, cond)
                assert resp.strip(), (name, cond)
            # 未识别指令行零容忍：ref 里除正稿/已识别行外的「→」行 = 引擎会静默丢弃
            for raw in st.ref.splitlines():
                line = raw.strip()
                if "→" in line:
                    assert _BRANCH_LINE_RE.match(line), f"{name}: 未识别指令行 {line[:44]!r}"


def test_templates_language_names_and_no_probe_markers():
    names = set()
    for name in FILES:
        data, _ = _load(name)
        assert data["language"] in ("zh", "cantonese", "en")
        assert "e2e" not in data["name"]
        assert "probe" not in data["name"].lower()
        assert data["steps_json"] and isinstance(data["steps_json"], list)
        names.add(data["name"])
    assert len(names) == 3
