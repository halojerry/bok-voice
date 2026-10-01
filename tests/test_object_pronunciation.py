"""对象发音词典数据面与 A 线装配接线回归（2026-09-27）。

三块：
1. CP objects 端点读写往返（pronunciation 字段）+ ≤500 字截断；
2. 旧库启动补列（deps `_ensure_column` 幂等，缺 pronunciation 的 object_profiles）；
3. agent.py 装配源级 pin：解析 helper 存在且 minimax 分支把值传进 MiniMaxTTS。
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from fastapi.testclient import TestClient  # noqa: E402

from control_plane.main import app  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_object_pronunciation_round_trip():
    """POST 建档带 pronunciation → GET/PATCH 往返一致；空=存空串。"""
    with TestClient(app) as client:
        obj = client.post(
            "/api/objects",
            params={"account_id": "acc-001"},
            json={
                "display_name": "陳大文",
                "language": "cantonese",
                "pronunciation": "陳大文/(can4)(daai6)(man4)",
            },
        ).json()
        obj_id = obj["id"]
        assert obj["pronunciation"] == "陳大文/(can4)(daai6)(man4)"

        listed = client.get("/api/objects", params={"account_id": "acc-001"}).json()
        row = next(o for o in listed if o["id"] == obj_id)
        assert row["pronunciation"] == "陳大文/(can4)(daai6)(man4)"

        # PATCH 改读法
        updated = client.patch(
            f"/api/objects/{obj_id}",
            json={"display_name": "陳大文", "pronunciation": "陳大文/(can4)(daai6)(man4)\n张伟/(zhang1)(wei3)"},
        ).json()
        assert updated["pronunciation"] == "陳大文/(can4)(daai6)(man4)\n张伟/(zhang1)(wei3)"

        # 空串=普通文本字段，存空（无 sms secret 式保留旧值语义）
        cleared = client.patch(
            f"/api/objects/{obj_id}",
            json={"display_name": "陳大文", "pronunciation": ""},
        ).json()
        assert cleared["pronunciation"] == ""


def test_object_pronunciation_truncated_to_500():
    with TestClient(app) as client:
        long = "张/" + "a" * 600
        obj = client.post(
            "/api/objects",
            params={"account_id": "acc-001"},
            json={"display_name": "长词条", "pronunciation": long},
        ).json()
        assert len(obj["pronunciation"]) == 500


def test_migration_adds_pronunciation_column(tmp_path, monkeypatch):
    """存量库 object_profiles 无 pronunciation → 启动补列，且二启幂等。"""
    import sqlite3

    db = tmp_path / "old_pron.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE object_profiles (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) DEFAULT '', "
        "display_name VARCHAR(255) DEFAULT '', role_template VARCHAR(64) DEFAULT '', "
        "language VARCHAR(16) DEFAULT 'zh', background TEXT DEFAULT '', phone VARCHAR(64) DEFAULT '', "
        "tracking_no VARCHAR(64) DEFAULT '', courier VARCHAR(64) DEFAULT '', address VARCHAR(255) DEFAULT '', "
        "template_id VARCHAR(64) DEFAULT '', status VARCHAR(32) DEFAULT 'active');"
    )
    conn.close()

    from control_plane.deps import build_engine

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    build_engine()
    build_engine()  # 幂等

    c = sqlite3.connect(db)
    cols = [r[1] for r in c.execute("PRAGMA table_info(object_profiles)")]
    c.close()
    assert "pronunciation" in cols


def test_agent_assembly_wires_pronunciation_source_pin():
    """源级 pin：agent.py 有解析 helper + minimax 分支解析/传参/打日志。"""
    src = (_REPO_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert "def _parse_pronunciation_entries(" in src
    assert 'get("pronunciation")' in src
    assert "pronunciation=_pronunciation or None" in src
    assert "[agent] minimax pronunciation" in src


def test_parse_pronunciation_entries_filters_and_cleans():
    """纯函数：非法行（无 `/`、任一段空）丢弃；合法行清理首尾空白后回写。"""
    import sys

    sys.path.insert(0, str(_REPO_ROOT / "apps" / "agent"))
    from agent_runtime.agent import _parse_pronunciation_entries

    raw = "\n".join([
        "陳大文/(can4)(daai6)(man4)",
        "没有斜杠读法",       # 无 `/` → 丢弃
        "/只有读法",           # 原词空 → 丢弃
        "张伟/",               # 读法空 → 丢弃
        "  李四 / (lei5)(sei3)  ",  # 首尾空白清理
        "",                     # 空行 → 丢弃
    ])
    assert _parse_pronunciation_entries(raw) == [
        "陳大文/(can4)(daai6)(man4)",
        "李四/(lei5)(sei3)",
    ]
    # None / 空 → []
    assert _parse_pronunciation_entries(None) == []
    assert _parse_pronunciation_entries("") == []

