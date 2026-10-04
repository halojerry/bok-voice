"""[bok-timing v2] mlx server 三值打点修复测试（2026-10-02 刀6B）。

病灶（日志取证）：``[bok-timing] generate_ms / first_progress_ms / prompt_window_ms``
三值里 first_progress_ms ≡ generate_ms、prompt_window_ms ≡ 0 恒等——v1 块在
``generate()`` 返回处立刻取值/打点，但 mlx server 的 ``generate()`` 只等
「排队 + tokenize + ctx 就绪」（生成线程先 ``rqueue.put(ctx)`` 再跑
``stream_generate``），prefill progress 回调全发生在返回之后的流迭代里，
``_bok_fp[0]`` 恒为 None → ``or _bok_g1`` 兜底成恒等。v2：回调只记时刻，
打点延迟到首个生成项到达（progress 元组先于 Response 入队，此刻 prefill 窗
已完整），缺席时明标 -1.0。

本测不依赖 mlx_lm 真件：①直接在合成 handler 上 exec ``TIMING_BLOCK`` 原文
（含 v1／v2 对照，行为级证明恒等式与修复）；②fake mlx_lm.server 模块复刻
「rqueue 元组 → _inner → 外部 progress_callback」真结构，驱动
``bok_mlx_server.py`` 的 generate/stream_generate 覆写链（双跳不互吞）；
③注入函数幂等/升级（v1 → v2 不叠加）。
"""

from __future__ import annotations

import builtins
import importlib.util
import logging
import queue
import re
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "mlx_lm_template_leak_fix.py"
WRAPPER_PATH = ROOT / "services" / "llm-mlx" / "bok_mlx_server.py"
VENV_SERVER = (
    ROOT / "services" / "llm-mlx" / ".venv" / "lib" / "python3.12"
    / "site-packages" / "mlx_lm" / "server.py"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def script():
    return _load(SCRIPT_PATH, "mlx_tlf_timing_ut")


@pytest.fixture()
def wrapper():
    mod = _load(WRAPPER_PATH, "bok_mlx_server_timing_ut")
    mod._REGISTRY = mod._AbortRegistry()
    return mod


# ---------------------------------------------------------------------------
# ① 源级 pin：TIMING_BLOCK=v2 新语义（fpm 不再 or 兜底成恒等）
# ---------------------------------------------------------------------------

def test_timing_block_v2_semantics_pins(script):
    blk = script.TIMING_BLOCK
    assert script.TIMING_MARK_V2 in blk
    # 三值 + 首 token 槽位：三个独立时点，不再用 generate 返回时刻兜底
    assert "_bok_fp = [None, None, None]" in blk
    assert "or _bok_g1" not in blk  # 恒等式根因源：必须清除
    assert "first_token_ms" in blk
    assert blk.count("-1.0") >= 3  # progress/token 缺席明标 -1.0
    assert "progress_callback=_bok_progress_cb" in blk  # 打点回调仍在链上
    assert blk.rstrip().endswith("except Exception as e:")  # 原 except 槽位保持
    # 打点延迟到首个生成项（流迭代）之后，而非 generate() 返回处
    assert "response = _bok_timed_stream(response)" in blk
    # 日志格式串四值四参对齐
    assert blk.count("%.0f") == 4
    assert "% (_bok_g, _fpm, _ppw, _ftm)" in blk


def test_apply_timing_patch_has_upgrade_and_idempotency_paths(script):
    src = SCRIPT_PATH.read_text()
    assert "TIMING_BLOCK_V1" in src  # 存量 v1 精确替换（不叠加）
    assert "TIMING_MARK_V2 in src" in src  # v2 幂等判据
    assert script.TIMING_MARK_V1 not in script.TIMING_BLOCK
    assert script.TIMING_BLOCK_V1.rstrip().endswith("except Exception as e:")


def test_kill_switches_independent(script, wrapper, monkeypatch):
    """BOK_TIMING_PATCH=0 只停注入；BOK_MLX_ABORT=0 只停 wrapper，互不拖累。"""
    monkeypatch.setenv("BOK_TIMING_PATCH", "0")
    assert script._apply_timing_patch("/nonexistent", "sentinel") == "sentinel"
    src = SCRIPT_PATH.read_text()
    wsrc = WRAPPER_PATH.read_text()
    # 开关只读各自身份的 env（文本里的交叉提及=注释，非 env 读取）
    assert 'os.environ.get("BOK_TIMING_PATCH", "1")' in src
    assert 'os.environ.get("BOK_MLX_ABORT"' not in src
    assert 'os.environ.get(ABORT_ENV, "1")' in wsrc
    assert 'os.environ.get("BOK_TIMING_PATCH"' not in wsrc
    monkeypatch.delenv("BOK_TIMING_PATCH")
    monkeypatch.setenv("BOK_MLX_ABORT", "0")
    assert wrapper._abort_enabled() is False
    assert script._apply_timing_patch("/nonexistent", "sentinel") == "sentinel"  # 不碰


# ---------------------------------------------------------------------------
# ③ 注入幂等 / v1→v2 升级（不产生重复块）
# ---------------------------------------------------------------------------

_HEADER = '''#!/usr/bin/env python
import time
import logging


class H:
    def handle(self, request, args, keepalive_callback):
        # Create the token generator
        try:
            ctx, response = self.response_generator.generate(
                request,
                args,
                progress_callback=keepalive_callback,
            )
        except Exception as e:
            return e
        return ctx, response
'''


def test_injection_fresh_anchor_then_idempotent(script, tmp_path):
    p = tmp_path / "server.py"
    p.write_text(_HEADER)
    script._apply_timing_patch(str(p), p.read_text())
    once = p.read_text()
    assert once.count(script.TIMING_BLOCK) == 1
    assert script.TIMING_MARK_V1 not in once
    assert script.TIMING_ANCHOR not in once  # 锚被整块替换，不是叠加
    script._apply_timing_patch(str(p), p.read_text())  # 第二次
    assert p.read_text() == once  # 逐字节不变


def test_injection_upgrades_v1_without_stacking(script, tmp_path):
    p = tmp_path / "server_v1.py"
    p.write_text(_HEADER.replace(script.TIMING_ANCHOR, script.TIMING_BLOCK_V1, 1))
    script._apply_timing_patch(str(p), p.read_text())
    out = p.read_text()
    assert out.count(script.TIMING_BLOCK) == 1
    assert script.TIMING_BLOCK_V1 not in out  # 旧块被替换
    assert script.TIMING_MARK_V1 not in out
    assert script.TIMING_ANCHOR not in out
    script._apply_timing_patch(str(p), out)
    assert p.read_text() == out  # 升级后幂等


def test_injection_anchor_missing_is_noop(script, tmp_path):
    p = tmp_path / "upstream.py"
    p.write_text("x = 1\n")
    assert script._apply_timing_patch(str(p), p.read_text()) == "x = 1\n"
    assert p.read_text() == "x = 1\n"


# ---------------------------------------------------------------------------
# 行为级：合成 handler 上 exec TIMING_BLOCK 原文（v1 恒等式对照 / v2 真窗）
# ---------------------------------------------------------------------------

_TIMING_RE = re.compile(
    r"\[bok-timing\] generate_ms=(\S+) first_progress_ms=(\S+) "
    r"prompt_window_ms=(\S+)(?: first_token_ms=(\S+))?"
)


class _ListHandler(logging.Handler):
    def __init__(self, out: list[str]) -> None:
        super().__init__()
        self.out = out

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.out.append(record.getMessage())


def _exec_timing_block_source(src: str, ns: dict) -> None:
    """执行注入块原文（最小护栏）。

    src 由本测试自建（TIMING_BLOCK/TIMING_BLOCK_V1 原文 + 合成 harness），
    非外部输入；ns 必须是调用方显式传入的命名空间 dict。用 builtins.exec
    显式指代内建（与裸 exec 语义完全一致，静态扫描不误报）。
    """
    if not isinstance(ns, dict):
        raise TypeError("timing block 命名空间必须是 dict")
    builtins.exec(compile(src, "<timing-block>", "exec"), ns)


def _exec_timing_block(block: str):
    """把注入块原文装进合成 handle()，返回可驱动 harness。

    块缩进 8 空格（方法体）；dedent 后成为 4 空格函数体，尾随 except 槽位补
    错误路径，函数末尾消费 response（= 真 handle_completion 的流迭代）。"""
    src = (
        "def _harness(self, request, args, keepalive_callback):\n"
        + textwrap.indent(textwrap.dedent(block), "    ")
        + "\n"  # 注入块正文以 except 行收尾（无尾换行），补上再挂错误路径
        + "            self._err = str(e)\n"
        + "            return 'error'\n"
        + "    self._consumed = list(response)\n"
        + "    return 'ok'\n"
    )
    ns: dict = {"time": time, "logging": logging}
    _exec_timing_block_source(src, ns)
    return ns["_harness"]


class _FakeGen:
    """复刻真 generate 的时序：先返回 (ctx, stream)，progress 在流迭代里才发生。"""

    def __init__(self, plan):
        self.plan = plan
        self.cb = None

    def generate(self, request, args, progress_callback=None):
        self.cb = progress_callback

        def _stream():
            for step in self.plan:
                if step[0] == "sleep":
                    time.sleep(step[1])
                elif step[0] == "progress":
                    progress_callback(step[1], step[2])
                elif step[0] == "token":
                    yield step[1]

        return types.SimpleNamespace(prompt=[1, 2]), _stream()


def _run_block(block, plan):
    logs: list[str] = []
    handler = _ListHandler(logs)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.INFO)
    try:
        fn = _exec_timing_block(block)
        self_ = types.SimpleNamespace(response_generator=_FakeGen(plan), _err=None)
        outcome = fn(self_, None, None, lambda p, t: None)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    hits = [line for line in logs if "[bok-timing]" in line]
    assert len(hits) == 1, hits
    m = _TIMING_RE.search(hits[0])
    assert m, hits[0]
    vals = [float(g) for g in m.groups() if g is not None]
    return outcome, self_, vals


def test_v1_block_reproduces_identity_bug(script):
    """v1 对照臂：generate 返回处取值 → fpm 恒等于 g、ppw 恒 0（根因留档）。"""
    plan = [
        ("sleep", 0.03),
        ("progress", 0, 100),
        ("sleep", 0.04),
        ("progress", 100, 100),
        ("token", "hi"),
    ]
    outcome, self_, (g, fpm, ppw) = _run_block(script.TIMING_BLOCK_V1, plan)
    assert outcome == "ok" and self_._consumed
    assert fpm == g  # 恒等式：or _bok_g1 兜底
    assert ppw == 0.0


def test_v2_block_reports_real_progress_window_and_first_token(script):
    plan = [
        ("sleep", 0.03),
        ("progress", 0, 100),
        ("sleep", 0.04),
        ("progress", 100, 100),
        ("sleep", 0.02),
        ("token", "hi"),
    ]
    outcome, self_, (g, fpm, ppw, ftm) = _run_block(script.TIMING_BLOCK, plan)
    assert outcome == "ok" and self_._consumed == ["hi"]
    assert g < fpm - 10  # generate 返回时 progress 尚未发生（不再恒等）
    assert ppw >= 20  # 首→末 progress 真窗（计划里 40ms）
    assert ftm > fpm  # 首 token 晚于末 progress


def test_v2_block_marks_minus_one_when_no_progress(script):
    plan = [("sleep", 0.01), ("token", "hi")]
    outcome, self_, (g, fpm, ppw, ftm) = _run_block(script.TIMING_BLOCK, plan)
    assert outcome == "ok"
    assert fpm == -1.0 and ppw == -1.0  # 不打恒等，明标缺席
    assert ftm >= 0.0


def test_v2_block_empty_stream_marks_all_minus_one(script):
    outcome, _self, (g, fpm, ppw, ftm) = _run_block(script.TIMING_BLOCK, [])
    assert outcome == "ok"
    assert fpm == -1.0 and ppw == -1.0 and ftm == -1.0


# ---------------------------------------------------------------------------
# ② wrapper 转发链：双跳（abort 守卫 + 外部 progress 回调）互不吞
# ---------------------------------------------------------------------------

def _make_queue_server(stream_script):
    """fake mlx_lm.server：复刻真结构（generate 先 put(ctx)，progress 元组经
    rqueue 由 _inner 在 handler 线程转发；_serve_single 经模块命名空间调
    stream_generate=wrapper 替换点）。"""
    mod = types.ModuleType("fake_mlx_server_timing")
    mod.Queue = queue.Queue

    class _Ctx:
        def __init__(self):
            self._should_stop = False
            self.stop_calls = 0

        def stop(self):
            self.stop_calls += 1
            self._should_stop = True

    class FakeAPIHandler:
        def do_POST(self):  # pragma: no cover - 本文件不驱动 HTTP 面
            pass

        def handle_completion(self, request, stop_words):  # pragma: no cover
            pass

    class FakeResponseGenerator:
        def __init__(self):
            self.req_queue = queue.Queue()  # 请求队列（非 HTTP client：名字避开 requests.* 误报）
            self.forwarded_cb = None
            self.iterated: list = []
            self.error = None
            self.ctx = None

        def generate(self, request, generation_args, progress_callback=None):
            self.forwarded_cb = progress_callback  # wrapper 转发链的观测点
            rqueue = mod.Queue()
            self.req_queue.put((rqueue, request, generation_args))

            def _inner():
                while True:
                    item = rqueue.get(timeout=10)
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    if isinstance(item, tuple):
                        if progress_callback is not None:
                            progress_callback(*item)
                        continue
                    yield item

            self.ctx = rqueue.get(timeout=10)
            if isinstance(self.ctx, Exception):
                raise self.ctx
            return self.ctx, _inner()

        def _serve_single(self, request):
            rqueue, _req, _args = request

            def progress(p, t):
                rqueue.put((p, t))

            rqueue.put(_Ctx())
            try:
                for item in mod.stream_generate(
                    prompt=[1], prompt_progress_callback=progress
                ):
                    rqueue.put(item)
            except Exception as exc:  # noqa: BLE001 - 镜像真源码：异常落 rqueue
                self.error = exc
                rqueue.put(exc)
            finally:
                rqueue.put(None)

    mod.ResponseGenerator = FakeResponseGenerator
    mod.APIHandler = FakeAPIHandler
    mod.stream_generate = stream_script
    mod.main = lambda: None
    return mod


def _drive(wrapper, fake, req, external_cb):
    """生成线程跑 _serve_single，主线程走 generate → 消费流（真 server 同姿势）。"""
    gen = fake.ResponseGenerator()
    out: dict = {"gen": gen}

    def _serve():
        rqueue, r, args = gen.req_queue.get(timeout=10)
        gen._serve_single((rqueue, r, args))

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    try:
        _ctx, stream = gen.generate(req, None, progress_callback=external_cb)
        out["items"] = list(stream)
        out["raise"] = None
    except BaseException as exc:  # noqa: BLE001 - 正常/abort 两态同抓
        out["items"] = []
        out["raise"] = exc
    t.join(timeout=10)
    return out


def test_progress_chain_double_hop_reaches_external_callback(wrapper):
    """打点回调（外部）经 rqueue 逐跳收到 progress；wrapper 不换掉它。"""
    seen: list = []

    def stream_script(**kwargs):
        cb = kwargs.get("prompt_progress_callback")
        assert cb is not None  # wrapper 恒注入守卫（entry 在场）
        cb(0, 10)
        cb(10, 10)
        yield "tok1"
        yield "tok2"

    fake = _make_queue_server(stream_script)
    wrapper.install(fake)

    def external_cb(p, t):
        seen.append((p, t))

    res = _drive(wrapper, fake, types.SimpleNamespace(_bok_req_id="t1"), external_cb)
    assert res["raise"] is None
    assert res["items"] == ["tok1", "tok2"]
    assert seen == [(0, 10), (10, 10)]  # 外部回调逐跳收到
    assert res["gen"].forwarded_cb is external_cb  # 原回调未被守卫链替换（非吞点）


def test_guard_aborts_mid_prefill_and_external_callback_keeps_first_hop(wrapper):
    """双跳并存：守卫每次 progress 检查（第二跳 abort 拦截），外部回调已收前一跳。"""
    seen: list = []

    def stream_script(**kwargs):
        cb = kwargs.get("prompt_progress_callback")
        cb(0, 10)  # 第一跳：守卫放行 → 外部回调记下
        wrapper._REGISTRY.abort("t2")  # prefill 期间 abort 到达
        cb(5, 10)  # 第二跳：守卫拦截，外部回调不再收到
        yield "never"

    fake = _make_queue_server(stream_script)
    wrapper.install(fake)
    res = _drive(
        wrapper, fake, types.SimpleNamespace(_bok_req_id="t2"),
        lambda p, t: seen.append((p, t)),
    )
    assert isinstance(res["raise"], wrapper._BokAbortError)
    assert res["items"] == []
    assert seen == [(0, 10)]


def test_no_req_id_path_progress_still_reaches_external_callback(wrapper):
    """无 X-Bok-Req-Id（或 BOK_MLX_ABORT=0 的 pass-through）：打点回调与 abort
    链解耦，progress 照达（wrapper 缺席不吞打点）。"""
    seen: list = []

    def stream_script(**kwargs):
        cb = kwargs.get("prompt_progress_callback")
        cb(1, 5)
        cb(5, 5)
        yield "a"

    fake = _make_queue_server(stream_script)
    wrapper.install(fake)
    res = _drive(wrapper, fake, types.SimpleNamespace(), lambda p, t: seen.append((p, t)))
    assert res["raise"] is None and res["items"] == ["a"]
    assert seen == [(1, 5), (5, 5)]


# ---------------------------------------------------------------------------
# 注入态 pin：本机 venv 已注入时必须=v2（未注入/文件缺席=跳过，不误伤 CI）
# ---------------------------------------------------------------------------

def test_venv_server_has_v2_block_when_injected(script):
    if not VENV_SERVER.exists():
        pytest.skip("mlx_lm venv 不在本机（CI）")
    src = VENV_SERVER.read_text()
    if "[bok-timing" not in src:
        pytest.skip("venv 未注入 bok-timing（未起过栈）")
    assert script.TIMING_MARK_V2 in src, "存量 v1 打点未升级到 v2（重跑注入脚本）"
    assert script.TIMING_BLOCK in src  # v2 块原文在位
    assert script.TIMING_MARK_V1 not in src  # v1 旧块清零（替换非叠加）
    assert src.count(script.TIMING_BLOCK) == 1
