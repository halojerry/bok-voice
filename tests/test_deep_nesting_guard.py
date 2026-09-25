"""M-6（fix-wave-3，task-12 F-Major-1）：validation error input echo 深嵌套爆栈护栏。

task-12 fuzz 实测：1000 层深嵌套 JSON → `RequestValidationError`（缺 account_id）
→ FastAPI 缺省 handler 对 `exc.errors()` 的 `input` echo 跑 `jsonable_encoder`
→ **RecursionError 500**（fastapi/encoders.py，CP log 5323–5443 行 traceback 佐证）。
服务本身无状态损伤，但任意持 calls 键身份（user JWT 同样可达）可反复触发 =
一次性 DoS 面，违反 fuzz「零 5xx」判据。

修法：自定义 RequestValidationError handler——错误明细（type/loc/msg）保留，
`input` 字段过 `_safe_validation_input` 有界化（深度 ≤4、每层 ≤16 键/项、
叶子 repr ≤200 字），畸形输入恒 422 不再 500。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from control_plane.main import _safe_validation_input


def test_safe_validation_input_shallow_value_kept():
    assert _safe_validation_input("acc-001") == "acc-001"
    assert _safe_validation_input(42) == 42
    assert _safe_validation_input({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}


def test_safe_validation_input_deep_nesting_truncated():
    deep = current = {}
    for _ in range(1000):
        current["a"] = {}
        current = current["a"]
    out = _safe_validation_input(deep)
    # 结果必须可 JSON 序列化（这是 handler 的全部意义）且深度有限
    import json

    json.dumps(out)  # 不得 RecursionError
    assert isinstance(out, dict)


def test_safe_validation_input_wide_and_long_truncated():
    wide = {f"k{i}": "y" * 500 for i in range(50)}
    out = _safe_validation_input(wide)
    import json

    json.dumps(out)
    assert len(out) <= 16  # 每层键数封顶
    for v in out.values():
        assert len(str(v)) <= 200 + 8  # 叶子 repr 截断

    long_list = list(range(100))
    assert len(_safe_validation_input(long_list)) <= 16


def test_safe_validation_input_nested_mixed_shapes():
    val = {"a": [{"b": ({"c": ["x" * 300]},)}]}
    out = _safe_validation_input(val)
    import json

    json.dumps(out)


def test_deep_nested_body_returns_422_not_500(monkeypatch):
    """task-12 F9 实弹形状：1000 层 `{"a":` 嵌套打 /api/calls → 422（原 500）。"""
    from fastapi.testclient import TestClient

    import control_plane.main as cp_main
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    monkeypatch.setattr(cp_main, "_token_issue_times", {})
    client = TestClient(cp_main.app)

    payload = '{"a":' * 1000 + "1" + "}" * 1000
    r = client.post(
        "/api/calls",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert "detail" in body


def test_normal_validation_error_still_informative(monkeypatch):
    """普通 422 的可诊断性保留：loc/type/msg 在场（修的是 500 不是语义）。"""
    from fastapi.testclient import TestClient

    import control_plane.main as cp_main
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    monkeypatch.setattr(cp_main, "_token_issue_times", {})
    client = TestClient(cp_main.app)

    r = client.post("/api/calls", json={})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail
    err = detail[0]
    assert err.get("type") and err.get("loc") and err.get("msg")
