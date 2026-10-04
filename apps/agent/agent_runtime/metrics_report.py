"""A 线 worker → CP 指标批量上报（DR 容灾+可观测波契约 §1，2026-10-01）。

契约（docs/DR-WAVE-CONTRACT.md §1，冻结）::

    POST /api/metrics/agent-report          （auth-off/机器通道直通）
    {"call_id": "call-x", "account_id": "acc-001", "worker": "a-line",
     "samples": [{"kind": "llm_ttft", "ms": 680.0, "ts": "2026-10-01T15:00:00Z"}, ...]}

kind 枚举固定四种：llm_ttft / asr_transcribe / tts_first_audio / vad_infer
（单源=``packages/core/bok_voice_core/metrics_kinds.py``——worker 上报/CP 滚动窗/
端点 schema 三面共用，本模块只 import 别名）。

形状纪律：
- ``add(kind, ms)`` 同步入队（纯内存 append，无 await/IO，绝不阻塞通话链路）；
- 后台 task 每 ``interval_s``（默认 2s）把整队批量 POST 一次；批满
  ``max_batch``（默认 50）提前发——「≤2s 或队列攒够即批量发」，空队 tick
  零请求；
- **全部吞错**：任何异常/非 2xx 只丢当前批，不重试、不阻塞、不堆积
  （失败批已出队即弃；队列 maxlen 溢出丢最旧，``dropped`` 计数留观测）；
- 成功批打一行 ``METRICS_REPORT sent=n``（失败不打，防日志风暴）；
- ``close()`` 收尾：停后台批 → 尽力 flush 余样 → 关客户端（幂等，全吞错）。

调用点（agent.py）：会话装配时构造（CP 地址/请求头复用既有 ControlPlaneClient
同源，见 ``cp.request_headers``）、``_on_metrics`` 四 kind 采样、``_close`` 收尾。
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from datetime import datetime, timezone

import httpx

from bok_voice_core.metrics_kinds import METRICS_KINDS

# 契约 §1 冻结枚举（单源=packages/core/bok_voice_core/metrics_kinds.py；CP 侧
# kind→provider 映射：asr/llm/tts/vad）。模块级别名=现有调用方零改动。
KINDS = METRICS_KINDS

_DEFAULT_INTERVAL_S = 2.0
_DEFAULT_MAX_BATCH = 50
_DEFAULT_QUEUE_MAX = 200
_HTTP_TIMEOUT_S = 2.0
_ENDPOINT = "/api/metrics/agent-report"


def _utc_now_iso() -> str:
    """契约样例口径：UTC ISO8601 秒精度（``2026-10-01T15:00:00Z``）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VadInferTracker:
    """vad_metrics → 单次推理均耗（ms）。增量差分，首样本只建基线。

    口径（livekit-agents 源码核实，``livekit/agents/vad.py``
    ``VADStream._metrics_monitor_task``）：事件携带的 ``inference_duration_total``
    / ``inference_count`` 是**自上一个事件以来**的批次量，emit 后计数器清零——
    即线上每个事件的批总量本身就是「增量」。仍按契约的增量差分公式
    ``Δdur/Δcnt`` 实现（兼容累计型生产者）：

    - 两计数器相对上一事件同增 → 取真增量（累计型生产者口径）；
    - 计数器回退/零增量（=逐批清零形态，本仓 livekit 实测）→ 当批总量即增量，
      并**永久切到当批口径**（防后续「计数巧合 +1」被差分公式算成噪声值）；
    - 首样本只建基线（契约：首样本跳过）；空批/除零/负值/坏值直接滤除
      （不进基线、不参与差分）。
    """

    def __init__(self) -> None:
        self._prev: tuple[float, int] | None = None
        # None=未定档；True=增量口径；False=当批口径（计数器回退/零增量后锁死）。
        self._delta_mode: bool | None = None

    def feed(self, duration_total_s: float, count: int) -> float | None:
        """喂一个 vad_metrics 样本；返回单次推理均耗 ms，跳过时 None。"""
        try:
            dur = float(duration_total_s)
            cnt = int(count)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(dur) or dur <= 0 or cnt <= 0:
            return None  # 空批/除零/负值=无信息，不进基线
        prev, self._prev = self._prev, (dur, cnt)
        if prev is None:
            return None  # 首样本：只建基线（增量差分无基准，契约跳过）
        d_dur, d_cnt = dur - prev[0], cnt - prev[1]
        if self._delta_mode is None:
            # 第二样本定档：计数器同增=累计型；否则=逐批清零型。
            self._delta_mode = d_cnt > 0 and d_dur > 0
        if self._delta_mode:
            if d_cnt > 0 and d_dur > 0:
                return round(d_dur / d_cnt * 1000.0, 1)
            self._delta_mode = False  # 计数器回退/零增量=逐批清零型，永久当批口径
        return round(dur / cnt * 1000.0, 1)


class MetricsReporter:
    """worker → CP 四 kind 指标批量上报器（fire-and-forget，全吞错）。

    ``transport`` 是测试缝（httpx.MockTransport）；生产走默认真实传输。
    """

    def __init__(
        self,
        cp_base_url: str,
        headers: dict[str, str] | None = None,
        *,
        call_id: str = "",
        account_id: str = "",
        worker: str = "a-line",
        interval_s: float = _DEFAULT_INTERVAL_S,
        max_batch: int = _DEFAULT_MAX_BATCH,
        queue_max: int = _DEFAULT_QUEUE_MAX,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=str(cp_base_url or "").rstrip("/"),
            timeout=_HTTP_TIMEOUT_S,
            headers=dict(headers or {}),
            transport=transport,
        )
        self._call_id = call_id
        self._account_id = account_id
        self._worker = worker
        # 地板 0.05s 防测试/误配 0 值把 tick 打成忙轮询。
        self._interval_s = max(0.05, float(interval_s))
        self._max_batch = max(1, int(max_batch))
        self._queue: deque[dict] = deque(maxlen=max(1, int(queue_max)))
        self._dropped = 0  # 队列溢出被动丢最旧（观测用，无日志防风暴）
        self._wake = asyncio.Event()  # 批满提前发信号
        self._task: asyncio.Task | None = None
        self._closed = False

    # ---- 入队面 ----

    @property
    def pending(self) -> int:
        """当前未上报样本数（测试/调试观测）。"""
        return len(self._queue)

    @property
    def dropped(self) -> int:
        """队列溢出丢弃数（测试/调试观测）。"""
        return self._dropped

    def add(self, kind: str, ms: float) -> None:
        """入队一个样本（同步非阻塞、绝不上抛）。

        kind 不在契约枚举 / 数值非有限 / 负值（如 livekit TTSMetrics.ttfb 取消
        哨兵 -1.0）一律静默丢——脏样本不进 CP 统计。
        """
        if self._closed or kind not in KINDS:
            return
        try:
            value = float(ms)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value) or value < 0:
            return
        if len(self._queue) == self._queue.maxlen:
            self._dropped += 1
        self._queue.append({"kind": kind, "ms": round(value, 1), "ts": _utc_now_iso()})
        if len(self._queue) >= self._max_batch:
            self._wake.set()
        self._ensure_task()

    def _ensure_task(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无事件循环（纯同步环境）——close() 仍可手动 flush
        self._task = loop.create_task(self._run())

    # ---- 后台批量面 ----

    async def _run(self) -> None:
        while not self._closed:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval_s)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass
            if self._closed:
                return
            await self._flush_once()

    async def _flush_once(self) -> None:
        """整队出队发一批；空队零请求；任何失败静默丢批（不重试）。"""
        n = len(self._queue)
        if n == 0:
            return
        samples = [self._queue.popleft() for _ in range(n)]
        payload = {
            "call_id": self._call_id,
            "account_id": self._account_id,
            "worker": self._worker,
            "samples": samples,
        }
        try:
            resp = await self._client.post(_ENDPOINT, json=payload)
            resp.raise_for_status()
        except Exception:  # noqa: BLE001 - 上报失败静默丢，绝不影响通话链路
            return
        print(f"METRICS_REPORT sent={n}", flush=True)

    # ---- 收尾面 ----

    async def close(self) -> None:
        """收尾：停后台批 → 扫尾 flush 余样 → 关客户端（幂等，全吞错）。

        尽力而为：任何一步失败都只牺牲残余样本，绝不向上抛。
        """
        if self._closed:
            return
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾吞错
                pass
        await self._flush_once()
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001 - 关连接失败零影响
            pass
