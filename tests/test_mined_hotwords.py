"""开采热词(第四来源)装配面单测(EX-H2)。

被验对象:
1. ``agent_runtime.agent._fetch_mined_hotwords`` —— 装配期一次性 GET
   ``/api/asr/hotwords?account_id=&lang=`` 的 fail-open 矩阵(404/超时/坏 JSON/
   空 words/形状错 → ""),成功路径按拆分纪律 join,BOK_MINED_HOTWORDS=0 零 HTTP。
2. A 线装配调用点 source pin(传 mined_hotwords;B 线 interpret 路径不接)。
3. tools/bok.py ``_FORWARD_ENV`` 登记 BOK_MINED_HOTWORDS。

零网络:httpx.AsyncClient 全替换为进程内假件;``asyncio.run`` 直跑协程。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "tools"))

import httpx  # noqa: E402

import agent_runtime.agent as ag  # noqa: E402
from agent_runtime.agent import _fetch_mined_hotwords, asr_hotword_context  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, *, json_error: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("invalid json")
        return self._payload


class _FakeAsyncClient:
    """httpx.AsyncClient 替身:记录构造参数与 GET,按 behavior 返回/抛。"""

    behavior = None  # _FakeResponse 实例 或 要抛出的 Exception 实例
    instances: list["_FakeAsyncClient"] = []

    def __init__(self, **kw):
        self.kw = kw
        self.gets: list[tuple[str, dict]] = []
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        self.gets.append((url, params))
        if isinstance(type(self).behavior, Exception):
            raise type(self).behavior
        return type(self).behavior


def _install(monkeypatch, behavior) -> type[_FakeAsyncClient]:
    _FakeAsyncClient.behavior = behavior
    _FakeAsyncClient.instances = []
    monkeypatch.setattr(ag, "httpx", types.SimpleNamespace(AsyncClient=_FakeAsyncClient))
    return _FakeAsyncClient


def _run(account_id: str = "acc-9", lang: str = "cantonese") -> str:
    return asyncio.run(_fetch_mined_hotwords(account_id, lang))


# ---- 成功路径 ----


def test_success_joins_and_calls_contract(monkeypatch):
    fake = _install(
        monkeypatch,
        _FakeResponse(200, {"words": ["開採甲", "開採乙", "開採丙"]}),
    )
    out = _run("acc-9", "cantonese")
    assert out == "開採甲,開採乙,開採丙"
    inst = fake.instances[0]
    assert inst.gets == [
        ("/api/asr/hotwords", {"account_id": "acc-9", "lang": "cantonese"})
    ]
    # 契约遵守:base_url 归一 + 短发超时 + 机器通道头
    assert inst.kw["base_url"] == "http://127.0.0.1:8000"
    assert inst.kw["timeout"] == 2.0
    assert inst.kw["headers"]["X-Bok-Channel"] == "agent"


def test_success_result_flows_into_context_order(monkeypatch):
    """拉取产物交给 asr_hotword_context:开采段整段排在行业段之前。"""
    _install(monkeypatch, _FakeResponse(200, {"words": ["開採甲", "開採乙"]}))
    mined = _run()
    ctx = asr_hotword_context(
        "cantonese", None, extra_hotwords="模板一", mined_hotwords=mined
    )
    assert ctx.index("模板一") < ctx.index("開採甲") < ctx.index("開採乙") < ctx.index("單號")


def test_auth_header_uses_machine_token(monkeypatch):
    monkeypatch.setenv("BOK_CP_TOKEN", "secret-tok")
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp.local:9000/")
    fake = _install(monkeypatch, _FakeResponse(200, {"words": ["甲"]}))
    assert _run() == "甲"
    inst = fake.instances[0]
    assert inst.kw["base_url"] == "http://cp.local:9000"
    assert inst.kw["headers"]["Authorization"] == "Bearer secret-tok"


# ---- fail-open 矩阵 ----


def test_404_fail_open(monkeypatch):
    _install(monkeypatch, _FakeResponse(404, {"detail": "not found"}))
    assert _run() == ""


def test_500_fail_open(monkeypatch):
    _install(monkeypatch, _FakeResponse(500, None))
    assert _run() == ""


def test_timeout_fail_open(monkeypatch):
    _install(monkeypatch, httpx.TimeoutException("timeout"))
    assert _run() == ""


def test_connection_error_fail_open(monkeypatch):
    _install(monkeypatch, httpx.ConnectError("refused"))
    assert _run() == ""


def test_invalid_json_fail_open(monkeypatch):
    _install(monkeypatch, _FakeResponse(200, json_error=True))
    assert _run() == ""


def test_empty_words_fail_open(monkeypatch):
    _install(monkeypatch, _FakeResponse(200, {"words": []}))
    assert _run() == ""


def test_words_not_list_fail_open(monkeypatch):
    _install(monkeypatch, _FakeResponse(200, {"words": {"a": 1}}))
    assert _run() == ""


def test_payload_not_dict_fail_open(monkeypatch):
    _install(monkeypatch, _FakeResponse(200, ["甲"]))
    assert _run() == ""


def test_blank_words_filtered(monkeypatch):
    _install(monkeypatch, _FakeResponse(200, {"words": ["甲", "  ", "", "乙"]}))
    assert _run() == "甲,乙"


# ---- kill-switch ----


def test_kill_switch_zero_skips_http(monkeypatch):
    monkeypatch.setenv("BOK_MINED_HOTWORDS", "0")
    fake = _install(monkeypatch, _FakeResponse(200, {"words": ["甲"]}))
    assert _run() == ""
    assert fake.instances == []  # 零 HTTP 调用(连 client 都唔构造)


def test_default_on_fetches(monkeypatch):
    monkeypatch.delenv("BOK_MINED_HOTWORDS", raising=False)
    fake = _install(monkeypatch, _FakeResponse(200, {"words": ["甲"]}))
    assert _run() == "甲"
    assert len(fake.instances) == 1


# ---- source pin ----


def test_a_line_assembly_passes_mined_hotwords():
    """A 线装配调用点:一次性拉取 + 传 mined_hotwords(装配期,非逐轮)。"""
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert "await _fetch_mined_hotwords(" in src
    assert "mined_hotwords=_mined_hotwords" in src
    # 单一调用点:装配路径只此一处(worker 并发下无逐轮读取)
    assert src.count("await _fetch_mined_hotwords(") == 1


def test_b_line_interpret_does_not_receive_mined():
    """B 线同传路径不得接开采词(同传域与开采用户话术不同)。"""
    src = (
        ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py"
    ).read_text(encoding="utf-8")
    assert "mined_hotwords" not in src
    assert "_fetch_mined_hotwords" not in src


def test_forward_env_contains_mined_hotwords():
    import bok  # noqa: E402  (sys.path 已注入 tools/)

    assert "BOK_MINED_HOTWORDS" in bok._FORWARD_ENV
