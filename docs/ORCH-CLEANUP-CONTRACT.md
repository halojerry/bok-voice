# 编排梳理波·冻结契约（2026-10-02，worktree chore/orch-cleanup）

来源：两路只读探针（编排时序图 + 车道/守卫/常数陈旧度）。十七波已落：打断即 abandon/judge 挪 4B（a_reply=:1237 直连、judge=:1235 代理→4B，DB 路由表已核）/垫话让路 hold=0/补答去重/TTS 全云/load_threshold 0.99。

## 文件所有权（铁律）
- **S1**：`apps/agent/agent_runtime/agent.py`、`apps/agent/agent_runtime/tts_cache.py` + 其测试。
- **S2**：`apps/agent/agent_runtime/providers/livekit_plugins.py`、`apps/agent/agent_runtime/fillers.py`、`docs/ORCHESTRATION-MAP.md`、`tests/test_reply_chokepoint_lint.py` + 其测试。
- 禁碰对方文件；禁重启/端口；各自测试绿后报告，主线合流。

## P0（S1）刀1 时序门取样点错位——bug
`agent.py:7754` `now = time.monotonic()` 在 `await handle` 之后；框架打断有 5s 宽限 → 宽限>下一轮流创建（~0.4-1s）时新流被误 abandon。修：`_watch` 入口（speech_created 时刻）取样传入；测试补「handle 晚归时新流不被弃」。

## P1（S1）
- 双 tts 样本：`tts_cache.py:526-527 _forward_metric` 转发内芯真样本 + Relay 基座监视器自己 emit（`_mark_started` 在 400-402 首帧转发锚→ttfb 近零）→ Provider 卡 p50 拉低/n 翻倍。修：Relay 层不再 emit（监视器排空或删 `_mark_started` 调用），单样本走内芯真锚。
- late-answer 双写：`agent.py:4060` gen=llm 登记 → `record_reply`×2（4700-4703+4988-4989）+ stall 双扣（4691-4698+4884-4885）。修：late-answer 登记 `relieve=False`（先例 qa-fastpath 6746-6748）+ ledger 同文去重。

## P2
- （S1）judge 让路链复标：`_JUDGE_REPLY_WAIT_S=15`(1495)、FLOW_JUDGE_DELAY=3/IDLE_CAP=6(1457-1472)、capped-skip(1475-1485)、`_judge_yield`(5304-5309)。前提已变：judge 与 reply 不同端点，互斥消失只剩 GPU 错峰（实弹 474/504 capped、359 skip=judge 饿死）。修：等待帽 15→4s、允许用户话中窗直接跑、capped 阈值放宽；**保留饥荒 overlay 姿态下的闸语义**（overlay 把 a_reply 拉回 :1235 代理时同槽互斥复活——用 env 可调的帽）。
- （S1）watchdog hold 撑高项已死：`agent.py:3664-3667` 删 hold 读取（恒 max(2,1.5)=2s）；`fillers.py` 的 `FILLER_YIELD hold=0 (legacy…)` 噪音行降频（仅 legacy>500ms 才打）。
- （S2）reshot 窗口：`fillers.py:895-913/1120-1124`——pending>2s（bidi 卡顿窗）裸静默时放行第二发；skip 文案同步新语义。

## P3
- （S2）`_flush_at_end` 漏拼 `_released_head`：`livekit_plugins.py:2069-2091` 复用 1966 的组合单元判定。
- （S1）pause-ack 空转：`agent.py:7568-7575/4667-4669` → turns 直记一行（保留语音行为）。
- （S2）AST lint 补面：`_reply_done_event` 的 set 站点清单入 `tests/test_reply_chokepoint_lint.py`（新车道漏 set=judge 白等 15s）。
- 注释同步：drain「mlx 无断连中止」等已被 W-ABORT 推翻的陈旧注释。

## P4（S2）
`docs/ORCHESTRATION-MAP.md`：编排时序地图固化（阶段 0-3 序列/等待链/FF 任务谱/车道表/常数表——探针输出为准）。

## 验收
worktree 全量 pytest 绿（2 例 429 限流 flaky 单跑绿即可）；合流后主线重启跑 soak canto + FLOW20（另行窗口）。
