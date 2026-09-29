# A 线回复哑火修复——设计文档 v2（治本优先，探针先行）

- 日期：2026-09-29（v2，替代同日 v1「四波兜底」方案）
- 状态：待审
- 范围：**A 线**（B 线已结案：麦克风未开零输入是正常行为；「中文翻中文」方向配死记观察位另立）
- v1→v2 变更（Ethan 拍板「治标不治本不行，我们做的是 realtime」）：
  - **砍掉** v1 P1.1「LLM 停滞三窗口分级处置」（filler 补位/断流重发/兜底直念）——兜底层不建，治本后按数据再议；
  - **新增** P1「会话生命周期治本」——v1 只把「mlx 慢」当待定量产测，v2 把 09-29 生命周期审计发现的四个病灶列为修复主体；
  - 保留正确性 bug 修复（guard 蒸发 / ladder 同级连发）与产品行为（unclear 推进 / nudge 软化）。

## 1. 根因（2026-09-29 诊断 + 生命周期审计定案）

### 1.1 直接死因（已实锤）

mlx :1239 生成段极端慢：坏通 `call-ed6aa9b8` 轮 1 prefill 17:30:03.98 完成 → 请求 17:30:14.729 完成，**生成段 10.7s（28 token ≈ 2.6 tps，正常 20-24 tps）**。LLM 引擎无辜（TTFT 912ms、prefill 0.9s 正常）。agent 各层（早发/filler/watchdog/ladder）被慢速逐层放大成「只有垫音」。

### 1.2 会话生命周期四病灶（Ethan 假设「挂断后会话没退出→进程/资源累积→速率下来」的审计定案）

对 2026-09-29 17:07-17:31 全部 34 个 agent job 逐房审计（received/exiting 全量比对）：

| # | 病灶 | 证据 | 后果 |
|---|---|---|---|
| ① | **挂断后 job 退出延迟 1-3 分钟**：挂断按钮只改 CP 状态，LiveKit 房间与连接不关，job 靠 `close_on_disconnect`/回收器兜底收摊 | 8c25aa8a job 存活 200s；reaped 通话等回收器窗 | 房间空转期 ASR 会话/内存持续占用 |
| ② | **job1 退出后派发层 3 秒内补派 job2 到同一房间**：call 仍 active + 房间还在 + agent 不在房 → 看门狗判「丢 agent」补位 | 803e44ad（job1 58s 退→3s 后 job2 124s）、8c25aa8a（job1 153s 退→3s 后 job2 44s）、9f2ff7a3 同形——三例 `resuming=false` 全新 job | 补位 job 接手一个客户已放弃的会话，**双份 ASR+LLM 空转到回收器**；连续快打时多通交叠（17:10-17:13 三 job 并存） |
| ③ | **B 线 job 退出超时被强杀** | interp 两 worker 均出现 `process did not exit in time, killing process`（b8793951 job 2270/2276） | 退出路径挂死→占资源到 10s 强杀窗 |
| ④ | **mlx prompt cache 多通累积顶满**：通话结束不清本通序列，直到上限 | 坏通时段 cache 61→64 条 / 5.83→**6.27GB**（上限=启动参数 `--prompt-cache-bytes 6GB`）；第七波「6GB cache 与整机冻结关联」观察位第二次出现 | 统一内存架构下 GPU 可用内存被挤 → Metal 性能退化——**坏通生成段 2.6 tps 的头号嫌疑** |

**注意**：坏通时段（17:29:48-17:31:06）A 线 job 独占、无并发——「进程残留直接拖慢那 10.7 秒」不成立；但①②④构成「多通累积→资源不归位→新会话降速」的机制链，病灶④恰在坏通时刻打满。**治本=挂断即收+补派免疫+退出即净+cache 上限合理化。**

### 1.3 「流程不动」闭环

Ethan 当前模板（3 步、无 say 直念步）step2/step3 内容全靠 LLM 应答交付，LLM 哑火 ⇒ 内容出不来 ⇒ 体感流程死。修掉哑火流程自然恢复；P3.1 另满足「意图不明确时一步步往下走」。

## 2. 方案总览

```
P0 探针波（零生产代码改动）
   ├─ P0.1 会话生命周期审计探针（主力）：模拟真实使用连打+挂断，量化全链时延与资源归位
   └─ P0.2 慢速注入诊断探针：2.6 tps 注入钉死 agent 侧未闭案环（纯诊断，不导向兜底）
        ↓ 产出过目后动 P1
P1 治本——会话生命周期四件
   ├─ P1.a 挂断即收链路（按钮→CP end→DeleteRoom→job 即退）
   ├─ P1.b 派发补位判据（ended 永不补派）
   ├─ P1.c B 线 job 退出路径修复
   └─ P1.d mlx prompt cache 上限治理
P2 正确性修复（bug，非兜底）
   ├─ P2.a guard cancel 缓冲蒸发（账本完整性）
   ├─ P2.b stall ladder 同级连发
   └─ P2.c LLM 慢速观测行（纯打点，不处置）
P3 产品行为
   ├─ P3.1 unclear 连续推进
   └─ P3.2 心跳 nudge 软化
        ↓
全量验收 + P0.1 复跑（生命周期指标达标）
```

## 3. P0 探针波设计

### P0.1 会话生命周期审计探针 `scripts/probe_session_lifecycle.py`（新，主力）

**目的**：在修复前量化「挂断后每个环节该退没退、拖了多久、资源归位没有」，修复后同探针验收。

**形态**：复刻 Ethan 真实使用——连续建 10 通 simulation call，每通用 **CP 挂断端点**结束（**不手动断 LiveKit 连接**，与真实挂断按钮行为一致），间隔 ~15s 开下一通。全程审计：

| 审计项 | 采集 | 判据（修复目标） |
|---|---|---|
| 挂断→CP status=ended | CP API 轮询 | <1s |
| 挂断→LiveKit room 删除 | LiveKit server API ListRooms 轮询 | <3s |
| 挂断→agent job `process exiting` | agent.log tail | <5s |
| 同房 job 数 | log 全量比对 | 恒 1 |
| mlx cache 序列数/GB | llm.log `Prompt Cache:` 行走势 | 挂断后不单调爬升；峰值 < 设定上限 |
| ASR sidecar 会话回收 | asr.log（会话生命周期行） | 挂断后会话关闭 |
| 全程 GPU 内存 | powermetrics / `memory_pressure` 采样 | 连打 10 通后无单调爬升 |

**产出**：`reports/session-lifecycle-2026-09-29/`——每通一行时序表 + 违规项清单。**P1 各项的 go/no-go 与验收基线。**

### P0.2 慢速注入诊断探针 `scripts/probe_llm_stall.py`（降级：纯诊断）

保留 v1 设计的限速 SSE 代理（:12399 转发 :1235，`--tps 2.6`）+ 3 轮通话驱动 + py-spy 自动抓栈——**唯一目的**：钉死「tee 捕到 10 字、bidi 零 task_continue」的未闭案环（理解系统，非建兜底）。产出 `reports/llm-stall-replay/`。修复处置逻辑一律不做（v1 P1.1 已砍）；若 P1 治本后慢速场景仍有该形态，凭栈报告再议。

## 4. P1 治本——会话生命周期四件

### P1.a 挂断即收链路

**现状**：挂断按钮 → CP 状态改 ended → LiveKit 房间/浏览器连接原样 → job 等回收器。

**修法**（CP `POST /api/calls/{id}/end` 扩链，端点既有）：
1. CP 置 call status=ended（现状）；
2. **新增：LiveKit server API `DeleteRoom`**（CP 已持 LiveKit admin 凭据，`livekit-api` 客户端既有）——房间删除即断所有连接、房间空 → agent job `close_on_disconnect` 立即收摊；
3. 打点 `call.hangup_chain`：挂断→ended→room_deleted→job_exit 各段毫秒，进 P0.1 探针断言面。

**边界**：interpret（B 线）房间同链适用；回收器保留为兜底（room 删除失败时 5 分钟窗仍收）——但**删除成功后回收器应见 room 缺席即秒收，不再等满窗**（对齐 09-28 tri-state 修法）。

### P1.b 派发补位判据（ended 永不补派）

**现状**：job1 退出 + 房间在 + call active → 看门狗判「丢 agent」3 秒补 job2（三例实证）。

**修法**：`dispatch_utils.py` 补派判定入口加一道硬门——**CP 查 call_sessions status，非 `ringing/active/paused` 一律不补**（ended/cancelled 永不补派）。与既有 `has_dispatch_record` 存在性判据叠加（存在性判「已派」，状态判「还该不该有人」）。审计 `dispatch.suppressed_ended`。

### P1.c B 线 job 退出路径修复

**现状**：interp job `process did not exit in time → killing process`（10s 强杀窗）。

**修法**：`interpret.py` 关闭链审计——`session.on("close")` 后的 `closed.wait()` + shutdown 回调里定位挂点（候选：TTS bidi 会话未 aclose / ASR 流未停 / ws 收发协程未 join），逐项加超时守护（`asyncio.wait_for(..., 3s)` + 打点 `interp.exit_slow stage=<name>`）。**以复现定位为准，P0.1 探针跑 B 线臂触发。**

### P1.d mlx prompt cache 上限治理

**现状**：`--prompt-cache-bytes 6GB`（启动参数）+ 多通累积不清理 → 64 条/6.27GB 顶满挤统一内存。

**修法**（参数级，零代码）：`services/llm-mlx` launcher `--prompt-cache-bytes 6GB → 4GB`、`--prompt-cache-size 128 → 48`（≈十几通对话工作集，LRU 自然换出）。**P0.1 探针 A/B**：4GB 档 vs 6GB 档的 cache 走势 + 生成段 tps + 命中率（`cached=/` 前缀命中不降级为判据——若 4GB 令命中显著掉、TTFT 涨，回 5GB 再测）。mlx_lm 无 per-sequence 清理 API，不做通话级清理的过度设计；**挂断重启清空**列为备选（仅当 A/B 证明 LRU 换出本身产生抖动）。

**验收**：连打 10 通 cache 峰值 ≤4GB 且无一通 TTFT 显著回归。

## 5. P2 正确性修复（bug，非兜底）

### P2.a guard cancel 缓冲蒸发

`_RepeatSelfGuardStream` 被 cancel 时 `_buf` 随协程蒸发（坏通三轮 `chars=10` 只有 tee 部分入账）。修：暴露 `pending_buffer`；`_run` 的 `CancelledError` 分支打 `REPEAT_GUARD_CANCEL_DROP chars=N`；agent.py `_on_speech_created._watch` 补账时拼入——账本完整，行为零变化。测试 `test_repeat_guard_cancel.py`。

### P2.b stall ladder 同级连发

「发射后 streak 顶到下一级门槛-1」的数值魔术有洞：degrade 发射后 streak=4，`stall_ladder_level(4)` 仍判 degrade → 同句再发（call-ed6aa9b8 17:30:37/46 两发实证）。修：`FlowController.ladder_fired: set[str]` 显式账本（advance/jump 清空），发射条件加 `and _lvl not in ladder_fired`，退役顶 streak 写法；bypass→close 直升保留。测试 `test_stall_ladder.py` 补 streak=4 二次 degrade 必拦回归。

### P2.c LLM 慢速观测行（纯打点，不处置）

`MlxLlmLLM` 流层：流完成时若平均 tps < `BOK_LLM_STALL_OBS_TPS`（默认 5）打 `LLM_STALL_OBS tps=N gen=N`。**治本后凭此行判断慢速是否绝迹；不绝迹再议处置**（v1 三窗口方案留档备查）。

## 6. P3 产品行为

### P3.1 unclear 连续推进

- `FlowController.unclear_streak`（per-step）：judge `route=keep` 消费点 bump；实答轮 -1 抵销（`relieve_stall_streak` 同挂点）；advance/jump 清零。
- 消费：`unclear_streak >= N` 且非最后一步且非 closing/done 且 WA 门通过 → 推进三件套（与 rule=auto 同构）。打点 `[flow] unclear-advance step=N streak=N` + 审计 `flow.unclear_advance`。
- env：`BOK_UNCLEAR_ADVANCE`（默认 `"1"`）/ `BOK_UNCLEAR_ADVANCE_N`（默认 `"3"`），入 `_FORWARD_ENV`。
- 测试 `test_unclear_advance.py`。

### P3.2 心跳 nudge 软化

- `_nudge_line` 三语软陪伴型文案（去「喂」开头；**待 Ethan 耳测拍板**）：
  - canto：`{name}，唔急，我等你，你聽到就應我一聲。` / `{name}，我仲喺度，你有咩想問隨時講。` / `{name}，唔好意思，你可能喺度谂紧，我等你。`
  - zh / en 同构（v1 表）。
- `SILENCE_NUDGE_SECONDS` 默认 `"8"`→`"12"`。
- 新文案=新缓存 key 旧条目自然失效；`tts-pregen --nudge` 按账号 roster 预合成；新旧对照样本落 `~/Desktop/nudge_ab/` 耳测后定稿。
- 测试 `test_nudge_lines.py` pin 文案+默认值+轮换。

## 7. 测试与验收

| 项 | 标准 |
|---|---|
| 全量 pytest | `./scripts/test.sh` 全绿（预计 +20~28 条新测试） |
| **P0.1 修后复跑** | 挂断→job 退出 <5s；同房 job 恒 1；**补派 suppressed_ended 打点在**；cache 峰值 ≤4GB；连打 10 通 TTFT 无单调回归 |
| FLOW20 | 20/20 零坏标记（含 `--narrowband` 臂） |
| soak 双语 | canto/zh PERCEIVED p50 ≤3000ms 无回归 |
| P0.2 | 未闭案环栈报告落 `reports/llm-stall-replay/`（诊断档案，不导向修复） |
| P1.c | B 线 10 通挂断零 `killing process`，`interp.exit_slow` 打点可观测 |
| P3.1 | 连续 unclear 场景推进可达最后一步 |
| P3.2 | 新文案耳测通过（Ethan）；运行时 `TTS_CACHE hit=1` 命中新条目 |

## 8. 风险与回滚

| 风险 | 缓解 |
|---|---|
| P1.a DeleteRoom 误删在途房间（挂断与在途通话竞态） | 只删 call_id 对应房间；删除前查 call status=ended；失败不重试只打点（回收器兜底仍在） |
| P1.b 补派抑制误杀真丢 agent 的场景 | 仅 ended/cancelled 抑制；ringing/active/paused 照旧补派——与现有判据叠加非替换 |
| P1.d cache 收紧令命中率掉、TTFT 涨 | P0.1 A/B 定档（4GB→5GB→6GB 阶梯）；launcher 参数回滚零代码 |
| P1.c 退出路径改动破坏 B 线 | 每项 wait_for 超时守护，`interp.exit_slow` 打点可观测；杀进程兜底保留 |
| kill-switch 总表 | `BOK_UNCLEAR_ADVANCE`/`BOK_UNCLEAR_ADVANCE_N`/`BOK_LLM_STALL_OBS_TPS` 入 `_FORWARD_ENV`；P1.a/b 属修复无开关（走测试钉），P1.d 走 launcher 参数 |

## 9. 执行序与文件所有权

1. **P0.1 + P0.2**（新探针脚本，零生产代码）→ 产出呈 Ethan 过目。
2. **P1.a/P1.b**（control-plane main + dispatch_utils，CP 侧）∥ **P1.c**（interpret.py）∥ **P1.d**（launcher 参数，P0.1 A/B 后落）——三路文件互斥。
3. **P2.a**（livekit_plugins.py guard + agent.py watcher）∥ **P2.b**（flow.py + agent.py 阶梯段）——P2 内同文件波内串行。
4. **P3.1**（flow.py + agent.py judge 段）∥ **P3.2**（agent.py `_nudge_line` + pregen）——与 P2 动同文件，按波串行。
5. 全量验收 + P0.1 复跑。

## 10. 明确不做（本波）

- **v1 P1.1 LLM 停滞三窗口分级处置（filler 补位/断流重发/兜底直念）**——兜底层不建；P2.c 观测行留数据口，治本后凭 `LLM_STALL_OBS` 再议。
- filler 拦截观测补齐（v1 P0.3）——随三窗口处置一起砍（无消费者）。
- B 线方向自适应/自动换向（观察位另立）。
- `PERCEIVED_MS` 修表（A3）、尾部瘦身/前缀断裂（A1/A2）、out-of-band 道歉交付——既有排队项。
- mlx 通话级 cache 清理 API——包无此能力，不过度设计。
