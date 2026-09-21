"""`scripts/load_cp_concurrency.py` 临时库名的**预留**语义（CWE-377 收口）。

来源：2026-09-21 Mimosa 分诊 ⑥-⑨ 复核确认的**唯一**一条（计划档 §32.3 ⑧）——
旧写法只产出名字、不留占位，「拿到名字 → sqlite 打开」之间该路径可被他人抢占
（TOCTOU）。修法=`mkstemp`（原子建 0600 文件）后关 fd；sqlite 把零长文件当新库，
故对压测语义零变化。

钉住两层：
- **结构**：CWE-377 的「先取名字、后打开」调用不得回潮（扫描面**排除 tests/**——
  本文件自己要写出该判据；排除 `runtime/`＝第三方 vendored）。
- **行为**：`_start_cp` 交给子进程的 `DATABASE_URL` 指向的路径，**在 Popen 被调用的
  那一刻已经是既存文件**——这正是「预留」与「先取名字」的区别所在。

判据字面量**拼接构造**（`_MKTEMP_NEEDLE`）而非写死：安全门禁自身不该在扫描器眼里
长得像它要禁的东西，否则每个静态扫描器都会在本文件上重复报同一条误报（同 §31.6
「以结构规避而非放宽门禁」的惯例）。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "load_cp_concurrency", _ROOT / "scripts" / "load_cp_concurrency.py")
lcc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lcc)

# "tempfile." + "mk" + "temp(" —— 见模块 docstring 的拼接理由。
_MKTEMP_NEEDLE = "tempfile." + "mk" + "temp("
_SCAN_ROOTS = ("scripts", "tools", "apps", "packages", "services")


class _Resp:
    status_code = 200


def test_no_mktemp_pattern_in_source():
    """CWE-377 的名字-再-open 模式禁止回潮（tests/ 自身除外，见模块 docstring）。"""
    hits: list[str] = []
    for top in _SCAN_ROOTS:
        for path in (_ROOT / top).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            body = path.read_text(encoding="utf-8", errors="ignore")
            if _MKTEMP_NEEDLE in body:
                hits.append(str(path.relative_to(_ROOT)))
    assert hits == []


def test_start_cp_hands_over_a_precreated_db_file(monkeypatch):
    """Popen 时刻该库文件已存在（原子预留），且确是 mkstemp 产物。"""
    seen: dict = {}

    def fake_get(url, **kw):
        # 第一枪＝启动前的健康探测（要失败才会走启动分支），其后一律 200 收口。
        if "probe" not in seen:
            seen["probe"] = True
            raise lcc.httpx.ConnectError("refused")
        return _Resp()

    class _Proc:
        pid = 4321

        def kill(self):  # pragma: no cover - 成功路径不会走到
            seen["killed"] = True

    def fake_popen(cmd, **kw):
        db_path = Path(kw["env"]["DATABASE_URL"].replace("sqlite:///", ""))
        seen["exists_at_spawn"] = db_path.exists()
        seen["is_file_at_spawn"] = db_path.is_file()
        seen["size_at_spawn"] = db_path.stat().st_size
        seen["name"] = db_path.name
        seen["db_path"] = db_path
        return _Proc()

    monkeypatch.setattr(lcc.httpx, "get", fake_get)
    monkeypatch.setattr(lcc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(lcc.time, "sleep", lambda _s: None)

    proc = lcc._start_cp()
    assert isinstance(proc, _Proc)
    assert seen["exists_at_spawn"] is True
    assert seen["is_file_at_spawn"] is True
    assert seen["size_at_spawn"] == 0          # 零长＝sqlite 认作新库
    assert seen["name"].startswith("bok-load-cp-") and seen["name"].endswith(".db")
    assert "killed" not in seen
    os.unlink(seen["db_path"])                 # 用例自己清场（脚本本体不删，与旧行为同）


def test_start_cp_reuses_healthy_cp_without_touching_tempdb(monkeypatch):
    """复用分支（:8001 已在跑）不得再建库——预留只发生在真要起进程时。"""
    spawned: list = []
    monkeypatch.setattr(lcc.httpx, "get", lambda url, **kw: _Resp())
    monkeypatch.setattr(lcc.subprocess, "Popen", lambda *a, **kw: spawned.append(kw))
    assert lcc._start_cp() is None
    assert spawned == []
