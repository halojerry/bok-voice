"""metrics kind 枚举三面单源（2026-10-03 C3）。

背景：DR 容灾+可观测波（``docs/DR-WAVE-CONTRACT.md`` §1）的四 kind 枚举曾在
三处各持一份字面量拷贝——worker 上报
（``apps/agent/agent_runtime/metrics_report.py``）、CP 滚动窗
（``apps/control-plane/control_plane/ops_metrics.py``）、上报端点 schema
（``apps/control-plane/control_plane/main.py`` ``AgentMetricSample``）——改一处
漏两处=静默分叉（新 kind 能上报但被 CP 滤掉、或 schema 422）。

本模块是唯一真源（纯函数零依赖层纪律：只 import 标准库）：

- ``METRICS_KINDS``：四 kind 元组（值/顺序与历史三副本逐字一致）；
- ``METRICS_KIND_LANES``：kind → provider 灯面名映射（``asr/llm/tts/vad``，
  键序=CP ``providers`` 响应行序，原 ``ops_metrics.KIND_TO_PROVIDER``）;
- ``METRICS_KINDS_LITERAL``：``typing.Literal`` 形态（Pydantic 请求模型注解
  用——``main.py`` 直接 import 本对象，不再抄字面量）。

**改一处必须同步本文件**（三面消费方 import 本模块；parity 由
``tests/test_metrics_kinds_parity.py`` 钉死对象/值相等 + 消费点源级 pin）。
"""
from __future__ import annotations

from typing import Literal

__all__ = ["METRICS_KINDS", "METRICS_KIND_LANES", "METRICS_KINDS_LITERAL"]

# 契约 §1 冻结四 kind（worker 上报 / CP 滚动窗 / 端点 schema 共用）。
METRICS_KINDS: tuple[str, ...] = (
    "llm_ttft",
    "asr_transcribe",
    "tts_first_audio",
    "vad_infer",
)

# kind → provider 灯面名（§2 providers 响应行序；消费方 ops_metrics.KIND_TO_PROVIDER）。
METRICS_KIND_LANES: dict[str, str] = {
    "asr_transcribe": "asr",
    "llm_ttft": "llm",
    "tts_first_audio": "tts",
    "vad_infer": "vad",
}

# 端点 schema 的 Literal 形态（typing.Literal 运行时即字面量集合对象，可被
# import 作 Pydantic 字段注解：main.py `kind: METRICS_KINDS_LITERAL`）。
METRICS_KINDS_LITERAL = Literal[
    "llm_ttft", "asr_transcribe", "tts_first_audio", "vad_infer"
]

# 单源一致性断言：Literal 与元组分叉=import 即炸（改一处忘另一处的硬拦；
# tests/test_metrics_kinds_parity.py 另有显式 pin）。
assert set(METRICS_KINDS_LITERAL.__args__) == set(METRICS_KINDS), (
    "METRICS_KINDS_LITERAL 与 METRICS_KINDS 分叉：两处必须同步"
)
