# 第一性原理重构：根因定案与执行计划（2026-10-01）

四路只读审计（发言权/上下文/守卫族/资源熵）证据定案。本档是唯一事实源，主线 AGENTS.md 不再复述。

## 零、已拍板的三个决策（Ethan，2026-10-01）

1. **本地与云端都允许，接口都要有。** 部署形态是路由决策，不是架构约束。
2. **Mac=开发演示（1-2 并发）；CUDA=生产，≥10 并发；并发数按硬件/实测容量动态化。**
3. **TTS：dev=本地（首声快、免费、离线）；生产=MiniMax（音色+吞吐）；FallbackAdapter 链已建成。** 实测纠正：本地 TTFA 167-374ms **快于** MiniMax 云端首包 600-900ms；MiniMax 赢在音色与吞吐，不赢在首声。欠的耳测（本地克隆粤语 vs MiniMax 2.8）只影响"本地能否进生产"，不阻塞架构。

## 一、根因树（证据版，全部 file:line 定案见审计输出）

- **RC-A（根）**：actor 任务里装了编排职责。prompt 协议占比实测 97.7%（第2轮）/93.9%（第15轮），真实对话仅 6.1%；槽位式任务 400-800 字符结构可行。守卫守的不是模型缺陷，是自己喂进去的复制素材。主干已翻本地 9B（DB 路由实锤），守卫栈是 4B 沉积、从未重基线。
- **RC-B**：发言权无单点仲裁。27 车道+暗道（filler off-band/框架回复/LLM 内芯 fallback/late-answer）群抢+事后清场；`_REPLY_LANES` 是死常量（零读取）。单守卫测试 ~300 条 vs 真交互演练 8-10 条。
- **RC-C**：资源静态错配+无预算准入。常驻 ~22-23GB（含 1.9GB 已弃用 ASR 死权重、6.3GB 闲置 4B）；reply/judge/settle 全挤 9B 单卡；唯一防爆闸=并发数 2；`_physical_mem_gib` 已写好零调用。
- **RC-D**：只加不减。env 转发 224 键；`BOK_SPEC_REOPEN` 纯蒸汽（零实现）；翻档不重基线、守卫不退役。
- 并列现实：粤语全栈低资源（数据飞轮路线不变）；Mac 硬件天花板（6 通 OOM 实测全栈崩）。

## 二、并发与部署的第一性原理

**准入不创造容量。** 并发上限由瓶颈资源决定，"按硬件配置"是代理变量——正确形状是**按实测可用容量**，硬件档案只给 floor/ceiling 初值：

```
max_active_calls = clamp(floor, (free_mem − reserved_footprint − headroom) / per_call_workset, ceiling)
```

- mac 档：ceiling=2（物理现实）。cuda 档：floor=10。
- 每次建单时现算（psutil/`_physical_mem_gib` 接线），无后台循环。
- 10 并发的组件真话表（谁真并行、谁串行、谁云化）：

| 组件 | Mac 1-2 通 | CUDA 10+ 通 | 路径 |
|---|---|---|---|
| LLM reply | MLX :1237 9B 单流+队列代理 | sglang 连续批处理单实例（实测聚合 ~105tps@2 流） | model_routing 双态✅ |
| judge/settle | 挤 9B（现状病灶） | 随 reply 批处理或独立实例 | lane routing✅ |
| ASR | SenseVoice CPU 40-55ms | 同（CPU 并行即够） | provider 面✅ |
| TTS | 本地 qwen3 clone | **MiniMax 云**（MPS 本地 10 并发必死） | FallbackAdapter✅ |
| embed/laya | 本地常驻 | 云化或 CUDA 驻留 | lane routing✅ |

结论：接口面基本已双态；缺的是 (a) 容量准入模块，(b) **部署档案**单选概念（dev-mac / prod-cuda / hybrid 一选全栈钉层）。

## 三、执行顺序（为什么是这个序）

1. **D2 四缺陷修复**（先行）：defer-ack 票据 / reask-cap 后 nudge 不重挂 / stall 阶梯先于 garbled-reask 倒置 / 复读全吞伪装死火。它们污染所有账本信号——不修，之后任何 A/B 归因不可信。
2. **容量准入模块**（capacity.py）：上述公式接线 `_physical_mem_gib`，env `BOK_MAX_CALLS_FLOOR/CEILING`，按档 floor/ceiling；Mac 实测校准一次。
3. **D1 槽位化 actor A/B（最高杠杆）**：四臂——①现状(9B+全剧本+全守卫) ②9B+槽位prompt+守卫族关 ③9B+全剧本+守卫族关(测 9B 残余病灶) ④云端 LLM+槽位prompt。悬架=现有 soak/FLOW20。预期：守卫死刑名单 ~60% 家族退役、4.5k 行前缀机制大部分注销、每轮 prefill 4k+ 字符级→<1k。
   - 槽位任务形状：`system: 角色卡(~250c) + user: 本轮任务(台词/命中分支+事实槽+8字锚) + 用户话` ≈ 300-700 token。编排职责（推进/收线/verdict/纪律/总览/禁讲）全部撤出 prompt，归 FlowController。
   - 跨步提问依赖总览事实 → 按轮检索槽位（knowledge/对象档案钩子已在）。
4. **D4 烧单**：即刻可烧 BOK_SPEC_REOPEN(蒸汽)/BOK_TAIL_SLIM/BOK_DEFER_ACK/BOK_REPEAT_GUARD/BOK_REPEAT_CROSS_TURN/QWEN3_ASR_CHUNK_KEEP+勘误族；D1 落地后整族再烧。烧=删代码删开关。
5. **D5 单点仲裁重构（排 D1 后）**：D1 先删一批车道（stall degrade/bypass、defer、fallback-ack、复读守卫族），先做 D5 等于收编即将不存在的车道。D1 后的收编清单=filler off-band/框架 generate_reply/LLM 内芯 fallback/late-answer 回调/17 处 `_say_script`。
6. **D3 资源真话**：卸 sensevoice 档下 Qwen3-ASR 死权重；4B 去留随 D1 结果定（若槽位化后 9B 单卡够，杀 4B 释放 6.3GB）。

## 四、守卫死刑名单（D1 落地即删）

整族删：复读三代+_released_head 拼合/D1 有界持有、TTS 首 chunk 早发·LLM 流内道、stall degrade/bypass、unclear 连续推进、LLM 首 token 道歉链（保留传输 timeout）、ack 锚豁免、lecture_guard、尾部锚剥离（数字顿号剥离视锚格式保留）、say 步长度、prompt 域纪律（禁讲/数字纪律/objection）。
保留：TTS 侧首子句/首 chunk（守 provider 时延）、watchdog+synth-ext（守引擎失败，重调阈值）、filler（减配额）、starve/storm/nudge（守用户行为）、garbled-reask（守 ASR）、late-final/热词回声/WA·digit 累积（守 ASR）、stall close 降级为业务出口。

## 五、验收纪律

- 每刀：聚焦测试 + `python -m compileall -q`；合并前全量 suite。
- A/B 归因依赖账本可信 → D2 先行是硬前置。
- 烧开关 = 删代码；验证窗口以 soak/FLOW20 一次绿为准。
