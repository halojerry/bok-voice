"""Sentry 接线单点(规范十条 R3,2026-10-04)——CP 与 agent worker 共用。

定位(治理计划):企业试点「线上出错你不在场」的真需求,~2h 量级。
纪律:
  - **DSN 只走 env**(`SENTRY_DSN`):不进 settings DB(免一次 DDL/掩码面),
    不进源码(gitleaks 全历史 CI 在守)。
  - SDK 缺席/DSN 空=完整 no-op(离线企业部署 pip 不可达也绝不炸启动);
    `capture()` 任何异常自吞——观测件把业务链路打挂=负资产。
  - traces_sample_rate=0.2(定案);`send_default_pii` 由 env `SENTRY_SEND_PII`
    控制(缺省 0=保守;Ethan 2026-10-04 拍板 dev 档开——Sentry 官方推荐开,
    开了请求头/IP 进事件;CP 请求面带联系方式,prod 档自行权衡)。
  - 组件标签 component=control-plane|agent-worker:一个 DSN 下分面聚合。
"""
from __future__ import annotations

import os
from typing import Any

TRACES_SAMPLE_RATE = 0.2

_initialized = False


def init_sentry(component: str) -> bool:
    """按 env 初始化 Sentry(幂等)。返回是否真的启用了。

    在进程入口调用一次:CP=main.py app 创建后;worker=agent entrypoint 早段。
    """
    global _initialized
    if _initialized:
        return True
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:  # 离线部署未装 SDK——DSN 设了也只告警,不炸启动
        print(f"[sentry] SENTRY_DSN 已设但 sentry-sdk 未安装,观测关闭 ({component})", flush=True)
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=(os.environ.get("SENTRY_ENVIRONMENT") or "").strip() or None,
            traces_sample_rate=TRACES_SAMPLE_RATE,
            send_default_pii=os.environ.get("SENTRY_SEND_PII", "").strip() in ("1", "true", "yes"),
        )
        sentry_sdk.set_tag("component", component)
        _initialized = True
        print(f"[sentry] enabled component={component} traces={TRACES_SAMPLE_RATE}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001 - 初始化失败=观测缺失,绝不阻启动
        print(f"[sentry] init failed ({component}): {exc!r}", flush=True)
        return False


def capture(exc: BaseException, **context: Any) -> None:
    """关键路径异常上报(未初始化=静默 no-op;SDK 异常自吞)。"""
    if not _initialized:
        return
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                scope.set_tag(key, str(value)[:200])
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 - 观测件永不外抛
        pass


def is_enabled() -> bool:
    return _initialized
