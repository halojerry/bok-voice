"""文档卫生门禁（G1a 治理，2026-10-04）:活文档仓内路径断链检查器。

四面:
  ① 真仓实跑 scripts/ops/check_doc_paths.py——验收令=断链清单为空（本次已修平
     README / REPO_MAP / ARCHITECTURE / CI_CD_PLAN 对已退役 desktop/、
     build_release.sh、verify_bundle.sh、stub_external_bin.sh 的残留引用）;
  ② --json 机读面=空数组（CI 可直接消费）;
  ③ tmp 夹断链夹具——证明检查器逻辑真在抓（不是恒绿;import 模块级函数,非子进程）;
  ④ 检查器自身带 scripts 引导头 marker（与其他 scripts 一致,防引导头 lint 漏检）。

约定:纯本地文件检查,零网络零真栈;真仓用例走子进程（验退出码契约）。

G3b 追加（2026-10-05,锚棘轮面,scripts/ops/check_doc_anchors.py）:
  ⑤ 真仓实跑锚检查器退出 0（当前计数 ≤ 基线,门在位）;
  ⑥ --json 机读面形状有效（count/per_file 自洽、ok=True）;
  ⑦ tmp 夹具证增长真被抓:2 锚 vs 基线 1 → 退出 1 且点名超基线文档;
     基线抬平后回 0（棘轮语义:降容易涨要显式 --update）;
  ⑧ 锚检查器自带 scripts 引导头 marker（同 ④ 纪律）;
  ⑨ 基线文件形状钉死（{"count","as_of","note"}）,防棘轮状态静默漂移。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "ops" / "check_doc_paths.py"
ANCHOR_CHECKER = ROOT / "scripts" / "ops" / "check_doc_anchors.py"
ANCHOR_BASELINE = ROOT / "scripts" / "ops" / "doc_anchor_baseline.json"
BOOTSTRAP_MARKER = "# --- scripts import bootstrap (G1) ---"


def _load_checker():
    spec = importlib.util.spec_from_file_location("_doc_path_checker", CHECKER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_checker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def _load_anchor_checker():
    spec = importlib.util.spec_from_file_location("_doc_anchor_checker", ANCHOR_CHECKER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_anchor_checker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ANCHOR_CHECKER), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_live_docs_no_broken_paths() -> None:
    proc = _run_checker()
    assert proc.returncode == 0, f"活文档断链:\n{proc.stdout}{proc.stderr}"


def test_live_docs_json_empty_array() -> None:
    proc = _run_checker("--json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == []


def test_fixture_detects_broken_refs(tmp_path: Path) -> None:
    # 夹具根系要有 docs/ 与 scripts/ 顶层,断链引用才落在检查器的 scope 内
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "live.py").write_text("", encoding="utf-8")
    (tmp_path / "docs" / "real.md").write_text("ok\n", encoding="utf-8")
    (tmp_path / "docs" / "guide.md").write_text(
        "见 `scripts/ghost.py` 与 [设计](docs/missing.md);参照 `scripts/live.py`。\n",
        encoding="utf-8",
    )
    broken = _load_checker().check_doc_paths(tmp_path)
    assert {b["ref"] for b in broken} == {"scripts/ghost.py", "docs/missing.md"}, broken
    assert {b["file"] for b in broken} == {"docs/guide.md"}
    assert all(b["line"] == 1 for b in broken), broken


def test_fixture_detects_broken_yml_run_ref(tmp_path: Path) -> None:
    # workflow run: 行里的裸命令路径（python scripts/x.py）也要抓——曾漏报的一条面
    (tmp_path / "scripts").mkdir()
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n  t:\n    steps:\n      - run: python scripts/ghost.py\n",
        encoding="utf-8",
    )
    broken = _load_checker().check_doc_paths(tmp_path)
    assert [(b["file"], b["line"], b["ref"]) for b in broken] == [
        (".github/workflows/ci.yml", 4, "scripts/ghost.py")
    ], broken


def test_checker_carries_bootstrap_marker() -> None:
    lines = CHECKER.read_text(encoding="utf-8").splitlines()
    # 窗口不设上限:本文件 docstring 37 行,marker 恒在 import 区之后(合规位置);
    # 契约=marker 在 + 紧随的引导 import 行,不钉绝对行号。
    assert BOOTSTRAP_MARKER in lines, "检查器缺 scripts 引导头 marker（G1 治理 §3.1）"
    idx = lines.index(BOOTSTRAP_MARKER)
    assert any(
        ln.startswith("import sys as _sys, pathlib as _pathlib")
        for ln in lines[idx + 1 : idx + 9]
    )


# --- G3b 锚棘轮面（check_doc_anchors.py） -------------------------------------


def test_anchor_checker_real_repo_within_baseline() -> None:
    proc = _run_anchor_checker()
    assert proc.returncode == 0, f"活文档锚超基线:\n{proc.stdout}{proc.stderr}"


def test_anchor_checker_json_face_valid() -> None:
    proc = _run_anchor_checker("--json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert isinstance(payload["count"], int) and payload["count"] >= 0
    assert payload["ok"] is True
    assert isinstance(payload["per_file"], dict)
    # per_file 与总量自洽;逐件计数非负
    assert payload["count"] == sum(payload["per_file"].values())
    assert all(n > 0 for n in payload["per_file"].values())


def test_anchor_fixture_catches_growth(tmp_path: Path, capsys) -> None:
    mod = _load_anchor_checker()
    # 夹具根系:docs/ 放带锚文档,scripts/ops/ 放自备基线（基线相对被扫根解析）
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts" / "ops").mkdir(parents=True)
    (tmp_path / "docs" / "map.md").write_text(
        "见 `apps/agent/agent_runtime/agent.py:100` 与 `agent.py:200`。\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "ops" / "doc_anchor_baseline.json").write_text(
        json.dumps({"count": 1, "as_of": "fixture", "note": "test"}),
        encoding="utf-8",
    )
    # 计数面:2 锚 > 基线 1 → delta +1、文档被点名
    res = mod.check_anchors(tmp_path)
    assert res["count"] == 2 and res["per_file"] == {"docs/map.md": 2}, res
    assert res["baseline"] == 1 and res["delta"] == 1 and res["ok"] is False, res
    # 退出码契约:main() 退出 1 且人类可读面点名超基线文档
    assert mod.main(["--root", str(tmp_path)]) == 1
    assert "docs/map.md" in capsys.readouterr().out
    # 棘轮抬平（--update 等价的人工动作）→ 回 0
    mod.write_baseline(tmp_path, 2, "fixture")
    assert mod.main(["--root", str(tmp_path)]) == 0
    capsys.readouterr()


def test_anchor_checker_carries_bootstrap_marker() -> None:
    lines = ANCHOR_CHECKER.read_text(encoding="utf-8").splitlines()
    assert BOOTSTRAP_MARKER in lines, "锚检查器缺 scripts 引导头 marker（G1 治理 §3.1）"
    idx = lines.index(BOOTSTRAP_MARKER)
    assert any(
        ln.startswith("import sys as _sys, pathlib as _pathlib")
        for ln in lines[idx + 1 : idx + 9]
    )


def test_anchor_baseline_seeded_and_shaped() -> None:
    # 棘轮状态文件必须在场且形状钉死:缺位/坏形状会让锚门退化成「恒 1」假红
    data = json.loads(ANCHOR_BASELINE.read_text(encoding="utf-8"))
    assert set(data) == {"count", "as_of", "note"}, data
    assert isinstance(data["count"], int) and data["count"] >= 0
    assert data["as_of"] and data["note"]
