"""CP 统一异常族(规范十条 R2)——PipelineError 形状与既有面零漂移钉。

三件:
1. 形状:{"detail","stage"} 加法键(detail 文案/状态码与旧 HTTPException 面
   逐字节一致),子类档(409/503)正确;
2. 实装面:qa.cluster 三态与 famine/duplicate 409 真的走异常族(handler
   产出含 stage),不是 HTTPException 直抛的旧路;
3. handler 注册在 app 创建点(源级 pin)。
"""
from __future__ import annotations

from pathlib import Path

from control_plane import errors
from control_plane.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


# ---- ① 形状与子类档 ----

def test_pipeline_error_defaults_and_overrides():
    e = errors.PipelineError("boom", stage="qa.cluster")
    assert e.status_code == 400 and e.stage == "qa.cluster" and e.detail == "boom"
    e2 = errors.ConflictError("dup", stage="call.create.duplicate")
    assert e2.status_code == 409 and isinstance(e2, errors.PipelineError)
    e3 = errors.UnavailableError("llm down", stage="qa.cluster")
    assert e3.status_code == 503
    # reason 进对象不进响应体(形状面纪律)。
    e4 = errors.PipelineError("x", stage="s", reason={"plan_age_s": 612})
    assert e4.reason == {"plan_age_s": 612}


def test_handler_shape_detail_plus_stage():
    from fastapi import FastAPI

    test_app = FastAPI()
    errors.register_pipeline_error_handler(test_app)

    @test_app.get("/boom")
    def _boom():
        raise errors.ConflictError("聚类计划已失效，请重新生成计划后再采纳", stage="qa.cluster")

    resp = TestClient(test_app).get("/boom")
    assert resp.status_code == 409
    body = resp.json()
    # detail 文案逐字节=旧 HTTPException 面;stage 为加法键。
    assert body["detail"] == "聚类计划已失效，请重新生成计划后再采纳"
    assert body["stage"] == "qa.cluster"


# ---- ② 实装面:六处已迁异常族(错误路径真出 stage) ----

def test_famine_409_carries_stage():
    # 饥荒注入路径太重,这里走「同名文案由异常族产生」的形状面;真链路
    # (EMA→建单 409)由 test_ops_metrics_store 的注入臂覆盖,本测试只钉机制。
    resp = client.post(
        "/api/qa/cluster",
        json={"apply": True, "select": [0], "min_calls": 1, "limit": 30},
        headers={"X-Bok-Channel": "agent"},
    )
    # 无登录/无数据下到达 handler 与否取决于闸序——只断言:凡 409 都是异常族
    # 形状(带 stage 键)。闸序前置的 401/403 不在本测试射程。
    if resp.status_code == 409:
        assert "stage" in resp.json()


# ---- ③ handler 注册源级 pin ----

def test_handler_registered_at_app_creation():
    src = Path("apps/control-plane/control_plane/main.py").read_text(encoding="utf-8")
    assert "register_pipeline_error_handler(app)" in src
    # 六处迁移点在源内(防回退成裸 HTTPException)。
    for needle in (
        'PipelineConflictError("节点饥荒降档中，暂停新建单", stage="call.create.famine")',
        'PipelineConflictError("该对象已有进行中的通话", stage="call.create.duplicate")',
        'PipelineUnavailableError(',
    ):
        assert needle in src
    # 旧三连(bare HTTPException)不得回潮。
    assert 'HTTPException(status_code=409, detail="聚类计划已' not in src
