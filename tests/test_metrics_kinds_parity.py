"""metrics kind 枚举三面单源 parity 门禁（2026-10-03 C3）。

契约 §1 的四 kind 字面量曾三处各持一份：worker 上报
（``agent_runtime/metrics_report.py``）、CP 滚动窗
（``control_plane/ops_metrics.py``）、上报端点 schema
（``control_plane/main.py`` ``AgentMetricSample``）。2026-10-03 收编进
``packages/core/bok_voice_core/metrics_kinds.py`` 单源，本测试钉死：

① 三处消费方 import 后与共享模块对象/值相等（含 lane 映射键序=§2 行序）；
② 三处消费点源级 pin（import+派生态——防未来把字面量抄回来，值相等也红）；
③ web 侧无 kind 字面量（grep 实证：四 kind 名在 apps/web 全树零命中），但
   Provider 卡四行 provider 名（``components/provider-status.tsx``
   ``PROVIDER_FIELDS``）是跨语言等值面——钉其与 ``METRICS_KIND_LANES`` 值序
   一致（TS 无法 import Python，读源钉字面）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# main.py 导入前置（照 test_ops_metrics_store.py 姿势：强制内存仓 + 假 LiveKit）。
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

from typing import get_args  # noqa: E402

from agent_runtime import metrics_report  # noqa: E402
from bok_voice_core.metrics_kinds import (  # noqa: E402
    METRICS_KIND_LANES,
    METRICS_KINDS,
    METRICS_KINDS_LITERAL,
)
from control_plane import main as cp_main  # noqa: E402
from control_plane import ops_metrics  # noqa: E402
from pydantic import ValidationError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

_AGENT_SRC = ROOT / "apps" / "agent" / "agent_runtime" / "metrics_report.py"
_CP_STORE_SRC = ROOT / "apps" / "control-plane" / "control_plane" / "ops_metrics.py"
_CP_MAIN_SRC = ROOT / "apps" / "control-plane" / "control_plane" / "main.py"
_WEB_PROVIDER_SRC = ROOT / "apps" / "web" / "components" / "provider-status.tsx"


def test_shared_module_literal_matches_kinds():
    """共享模块内部自洽：Literal __args__ 与 METRICS_KINDS 逐字（含顺序）一致。"""
    assert tuple(get_args(METRICS_KINDS_LITERAL)) == METRICS_KINDS
    assert set(METRICS_KIND_LANES) == set(METRICS_KINDS)


def test_worker_reporter_kinds_is_shared():
    """worker 上报面：metrics_report.KINDS 与共享元组相等（别名保调用面零改动）。"""
    assert metrics_report.KINDS == METRICS_KINDS


def test_cp_store_kinds_and_lanes_are_shared():
    """CP 滚动窗：KINDS 相等；KIND_TO_PROVIDER 值相等且键序一致（§2 行序）。"""
    assert ops_metrics.KINDS == METRICS_KINDS
    assert ops_metrics.KIND_TO_PROVIDER == METRICS_KIND_LANES
    assert list(ops_metrics.KIND_TO_PROVIDER) == list(METRICS_KIND_LANES)


def test_endpoint_schema_literal_is_shared():
    """上报端点 schema：Pydantic 注解解析出的 Literal 与共享枚举一致。"""
    field = cp_main.AgentMetricSample.model_fields["kind"]
    assert set(get_args(field.annotation)) == set(METRICS_KINDS)
    for kind in METRICS_KINDS:
        assert cp_main.AgentMetricSample(kind=kind, ms=1.0).kind == kind
    with pytest.raises(ValidationError):
        cp_main.AgentMetricSample(kind="llm_ttf", ms=1.0)


def test_consumer_source_pins_alias_shape():
    """源级 pin：三消费点必须是「import 共享模块 + 派生态」，防回抄字面量。"""
    agent_src = _AGENT_SRC.read_text(encoding="utf-8")
    assert "from bok_voice_core.metrics_kinds import METRICS_KINDS" in agent_src
    assert "KINDS = METRICS_KINDS" in agent_src

    store_src = _CP_STORE_SRC.read_text(encoding="utf-8")
    assert (
        "from bok_voice_core.metrics_kinds import METRICS_KIND_LANES, METRICS_KINDS"
        in store_src
    )
    assert "KINDS: tuple[str, ...] = METRICS_KINDS" in store_src
    assert "KIND_TO_PROVIDER: dict[str, str] = METRICS_KIND_LANES" in store_src

    main_src = _CP_MAIN_SRC.read_text(encoding="utf-8")
    assert "from bok_voice_core.metrics_kinds import METRICS_KINDS_LITERAL" in main_src
    assert "kind: METRICS_KINDS_LITERAL" in main_src
    # 端点 schema 不应再出现 kind 字面量（旧三副本形状）。
    assert "llm_ttft" not in main_src


def test_web_provider_rows_match_shared_lane_values():
    """web 跨语言 pin：Provider 卡四行（顺序即行序）= METRICS_KIND_LANES 值序。

    web 侧不存在 kind 字面量（四 kind 名在 apps/web 全树零命中），可 pin 的
    等值面=provider 灯面名；TS 无法 import Python，读源钉字面。
    """
    src = _WEB_PROVIDER_SRC.read_text(encoding="utf-8")
    rows = re.findall(r'\[\s*"(asr|llm|tts|vad)"\s*,', src)
    assert rows == list(METRICS_KIND_LANES.values())
