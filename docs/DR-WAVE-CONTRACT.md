# 容灾+可观测波 · 冻结契约（feat/dr-observability）

一条管道四个消费面：worker 指标上报 → CP 滚动窗口 → {Provider 卡, 饥荒监视器, Root 面板, 告警}。
三路并行文件所有权：A=apps/agent · B=apps/control-plane · C=apps/web。契约以本文为准，改契约=主线拍板。

## 1. 指标上报（worker → CP）

`POST /api/metrics/agent-report`（auth-off/机器通道直通，与既有 worker 上报同姿势）

```json
{
  "call_id": "call-xxx", "account_id": "acc-001", "worker": "a-line",
  "samples": [
    {"kind": "llm_ttft", "ms": 680.0, "ts": "2026-10-01T15:00:00Z"},
    {"kind": "asr_transcribe", "ms": 412.0, "ts": "..."},
    {"kind": "tts_first_audio", "ms": 310.0, "ts": "..."},
    {"kind": "vad_infer", "ms": 18.0, "ts": "..."}
  ]
}
```

kind 枚举固定四种。worker 批量发（≤2s 或 ≥1 样本触发），fire-and-forget 全吞错，绝不能阻塞通话链路。

## 2. Provider 卡读面

`GET /api/metrics/providers?call_id=<可选>`

```json
{
  "window_s": 300,
  "providers": {
    "asr":  {"last_ms": 412, "p50": 400, "p95": 490, "n": 31},
    "llm":  {"last_ms": 680, "p50": 710, "p95": 1240, "n": 28},
    "tts":  {"last_ms": 310, "p50": 300, "p95": 450, "n": 40},
    "vad":  {"last_ms": 18,  "p50": 18,  "p95": 22,  "n": 900}
  },
  "famine": {"level": "healthy", "ema_s": 0.6, "since": null, "downgraded": false, "dialing_paused": false}
}
```

kind→provider 映射：asr_transcribe→asr / llm_ttft→llm / tts_first_audio→tts / vad_infer→vad。
滚动窗=最近 300s 每 kind deque(maxlen=500)。

## 3. 饥荒监视器（CP 内，单一真源）

- 消费 llm_ttft 样本，EMA α=0.4（与 worker 端第十五波同款），≥2 样本且 EMA ≥ `BOK_LLM_FAMINE_TTFT_S`（默认 4000ms）→ `famine`。
- 持续 ≥10s（`BOK_LLM_FAMINE_HOLD_S`）→ **downgraded**：CP 在给 agent 的 settings 热读响应里把 a_reply 车道 overlay 成 4B（不改用户 model_routing_json 原文——GET /api/settings 的 agent 通道响应叠加 runtime override；overlay 后新通话下一通生效，与既有热切换机制同一条路）。
- EMA 回落 < 阈值持续 ≥30s（`BOK_LLM_FAMINE_RELEASE_S`）→ 回 healthy。
- **downgraded 状态 → 准入闸**：`_create_call_in`（live+非 interpret）→ 409 `{"detail":"节点饥荒降档中，暂停新建单"}` + 审计 `call.reject_famine`；在途通话不受影响；campaign 改期由既有 redispatch 承担。
- 状态转换全部审计 `ops.famine`（detail: from/to/ema_ms）。
- worker 端第十五波 EMA 降级为 CP 失联时的本地兜底，不动。
- 手动覆盖（root）：`force_downgrade` / `force_healthy` / `pause_dialing` / `resume_dialing`，覆盖优先于自动，打审计 `ops.famine_override`。

## 4. Root 容灾面板

`GET /api/ops/disaster-status`（require_role root，机器通道直通）

```json
{
  "famine": {"level": "healthy", "ema_s": 0.6, "since": null, "downgraded": false,
             "dialing_paused": false, "manual_override": null},
  "providers": {…同 §2…},
  "memory": {"swap_used_gb": 24.7, "threshold_gb": 8},
  "servers": [{"name": "llm-9b", "port": 1237, "up": true}, …],
  "active_calls": 2,
  "recent_events": [{"ts": "...", "event": "ops.famine", "detail": {...}}, …最近 20 条]
}
```

`POST /api/ops/disaster-override`（root）body `{"action": "force_downgrade|force_healthy|pause_dialing|resume_dialing"}`。

## 5. 实时日志

`GET /api/calls/{id}/logs?after=<byte_offset>&limit=200`

```json
{"lines": ["…原始行…"], "next_offset": 48213, "eof": true}
```

- 数据源：`app_data/logs/agent.log`；过滤=行内含 `call_id` 字样（结构行天然带）+ 原始 print 行按「最近一次结构行归属」带入（同一 call 窗口的原始行跟结构行走）。
- 权限：calls 页角色可读（`_gate_page("calls")`）；root 可经既有键授权面放给 admin/用户（不新增权限模型）。
- 抽屉轮询 2s，`after` 游标续读。

## 6. Web 消费（C 路所有权）

- **Provider 卡**（CallStudio 现有「Provider 服务状态」卡内改造，Ethan 定稿格式）：
  ```
  ASR   🟢 已连接 · 412ms (p95 490)
  LLM   🟢 已连接 · 680ms (p95 1240)
  TTS   🟢 已连接 · 310ms (p95 450)
  VAD   🟢 已连接 · 18ms
  ─────────────────────────
  状态: 🟢 健康 (EMA 0.6s)
  ```
  颜色阈值：LLM last/p95 <1000 绿 / 1000-2500 黄 / >2500 红（红=SLO 违约）；其余 provider 绿/黄/红按 2×p50 基线。状态行：healthy 绿 / famine 黄 / downgraded+停拨 红。轮询 3s `GET /api/metrics/providers`。
- **Root 容灾面板**：/nodes 或 /settings 旁新 rootOnly 卡片（navigation 先例），读 §4 端点，含手动覆盖四钮（二次确认）。
- **实时日志抽屉**：CallStudio 加「实时日志」折叠抽屉（通话页角色可见），轮询 §5。

## 7. 测试与验收

- B：`test_ops_metrics_store.py`（滚动窗/EMA/状态机/overlay 优先级/准入 409/审计）+ `test_call_logs_endpoint.py`（游标/过滤/权限）
- A：`test_agent_metrics_report.py`（样本采集映射/批量节流/吞错/源级 pin）
- C：tsc+build 零错；组件可 mock 数据渲染
- 端到端实弹验证=合并回主树后主线做（本 worktree 不起服务，防端口冲突）
