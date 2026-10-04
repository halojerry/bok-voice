"""CP 统一异常族(规范十条 R2,2026-10-04)——PipelineError 带 stage 机读标识。

定位(治理计划定案):**只落 CP FastAPI 边界**——agent worker 的
watchdog/degrade/饥荒自适应链=既在的错误处理架构,重写无决策收益,不碰。

形状契约:
  - `PipelineError(message, stage=..., status_code=..., reason=...)`
  - stage=稳定机读标识(点分层:"qa.cluster"/"call.create"/"ops.famine"),
    供日志/告警/前端分流消费;**不得**把面向人的文案塞进 stage。
  - 响应体=FastAPI HTTPException 同款 {"detail": <文案>} **外加**兄弟键
    "stage"(加法变更:既有客户端读 detail 的路径逐字节不变)。
  - status_code 缺省 400;子类可覆写(见下)。

使用纪律:
  - 既有端点的 detail 文案**逐字节保持**(测试与 web 消费面钉着);
    迁移=机制换 PipelineError,文案/状态码零漂移。
  - 新端点优先 raise PipelineError(带 stage),不再裸 HTTPException——
    审计/日志才有可聚合的阶段维度。
"""
from __future__ import annotations

from typing import Any


class PipelineError(Exception):
    """CP pipeline 阶段失败的统一形状(FastAPI 边界层)。

    例:``raise PipelineError("聚类计划已失效…", stage="qa.cluster", status_code=409)``
    """

    status_code: int = 400
    stage: str = "cp"

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        status_code: int | None = None,
        reason: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if stage is not None:
            self.stage = stage
        if status_code is not None:
            self.status_code = status_code
        # reason=结构化补充(如 {"plan_age_s": 612}),进日志不进响应体——
        # 响应体只带 detail/stage 两个键,形状面越小越稳。
        self.reason: dict[str, Any] = dict(reason or {})

    @property
    def detail(self) -> str:
        """与 HTTPException.detail 同名同义——handler 直接取。"""
        return str(self.args[0]) if self.args else ""


def register_pipeline_error_handler(app: Any) -> None:
    """把 PipelineError 挂到 FastAPI app(响应体={"detail","stage"})。

    在 app 创建后、路由注册前后均可调用;Starlette 按 MRO 找 handler,
    子类(ConflictError 等)一并被接住。
    """

    from fastapi.responses import JSONResponse

    async def _handler(_request: Any, exc: PipelineError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "stage": exc.stage},
        )

    app.add_exception_handler(PipelineError, _handler)


# ---- 常用档子类(status 语义钉死,调用点只给 stage 与文案) ----


class ConflictError(PipelineError):
    """409:并发/状态冲突(单飞锁、计划过期、重复建单)。"""

    status_code = 409


class UnavailableError(PipelineError):
    """503:依赖(本地 LLM/网关)不可用——可重试,不是参数错。"""

    status_code = 503
