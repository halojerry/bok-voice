# 第一性原理重构：根因定案与执行计划（2026-10-01）

四路只读审计（发言权/上下文/守卫族/资源熵）证据定案。本档是唯一事实源。
**状态更新（第一波落地后）见 §六。**

## 零、已拍板的三个决策（Ethan，2026-10-01）

1. **本地与云端都允许，接口都要有。** 部署形态是路由决策，不是架构约束。
2. **Mac=开发演示（1-2 并发）；CUDA=生产，≥10 并发；并发数按实测容量动态化。**
3. **TTS：dev=本地（首声快、免费、离线）；生产=MiniMax（音色+吞吐）；FallbackAdapter 链已建成。** 实测纠正：本地 TTFA 167-374ms **快于** MiniMax 云端首包 600-900ms；MiniMax 赢在音色与吞吐，不赢在首声。

## 一、根因树（证据版）

- **RC-A（根）**：actor 任务里装了编排职责。prompt 协议占比实测 97.7%（R2）/93.9%（R15），真实对话仅 6.1%；槽位式任务 400-800 字符结构可行。守卫守的不是模型缺陷，是自己喂进去的复制素材。主干已翻本地 9B，守卫栈是 4B 沉积、从未重基线。
- **RC-B**：发言权无单点仲裁。27 车道+暗道群抢+事后清场；`_REPLY_LANES` 死常量（零读取）。单守卫测试 ~300 条 vs 真交互演练 8-10 条。
- **RC-C**：资源静态错配+无预算准入。常驻 ~22-23GB（含 1.9GB ASR 死权重、6.3GB 闲置 4B）；reply/judge/settle 全挤 9B 单卡。
- **RC-D**：只加不减。env 224 键；`BOK_SPEC_REOPEN` 纯蒸汽；翻档不重基线、守卫不退役。
- 并列现实：粤语全栈低资源（数据飞轮路线）；Mac 硬件天花板（6 通 OOM 实测）。
- **过程教训（2026-10-01 实锤）**：主仓 102 文件未提交、HEAD 内部不一致（committed 测试引用 untracked 模块）——测试绿的是工作区不是 git。已快照进 worktree 基线 e9be38a；**主仓欠一次正式 commit（Ethan 决定时机）**。

## 二、并发与部署

```
max_active_calls = clamp(floor, (可用内存 − 安全垫含常驻脚印) / 单通工作集, ceiling)
```

档案初值：mac {floor1, ceiling2（6 通 OOM 实测物理现实，公式只压不抬）, workset2.5G, headroom2G}；cuda {floor10, ceiling48, workset1.5G, headroom4G}。legacy `BOK_MAX_ACTIVE_CALLS` 显式设=钉死（零探测）。

| 组件 | Mac 1-2 通 | CUDA 10+ 通 |
|---|---|---|
| LLM | MLX 9B 单流+队列代理 | sglang 连续批处理（实测聚合 ~105tps@2 流） |
| ASR | SenseVoice CPU 40-55ms | 同，CPU 并行即够 |
| TTS | 本地克隆 | **MiniMax 云**（MPS 本地 10 并发必死） |
| judge/settle | 现挤 9B（病灶） | 随 reply 批处理或独立实例 |

## 三、执行序

1. ✅ **D2 四缺陷**（2026-10-01 第一波落地，§六）
2. ✅ **容量准入 + ASR 死权重**（同波落地，§六）
3. ⬜ **D1 槽位化 actor A/B（下一刀，最高杠杆）**：四臂——①现状(9B+全剧本+全守卫) ②9B+槽位prompt+守卫族关 ③9B+全剧本+守卫族关 ④云端 LLM+槽位prompt。悬架=现有 soak/FLOW20。槽位形状：`system 角色卡(~250c) + user 本轮任务(台词/命中分支+事实槽+8字锚) + 用户话` ≈300-700 token；编排职责（推进/收线/verdict/纪律/总览/禁讲）全撤出 prompt 归 FlowController；跨步提问→按轮检索槽位。
4. ⬜ **D4 烧单**：BOK_SPEC_REOPEN(蒸汽)/BOK_TAIL_SLIM/BOK_DEFER_ACK/BOK_REPEAT_GUARD/BOK_REPEAT_CROSS_TURN/QWEN3_ASR_CHUNK_KEEP+勘误族；D1 后整族再烧（死刑名单 §四）。
5. ⬜ **D5 单点仲裁重构（排 D1 后）**：D1 先删车道，收编清单=filler off-band/框架 generate_reply/LLM 内芯 fallback/late-answer 回调/_say_script 调用点。
6. ⬜ **D3 资源真话余项**：4B 去留随 D1 定（槽位化后 9B 单卡若够，杀 4B 释放 6.3GB）。

## 四、守卫死刑名单（D1 落地即删）

整族删：复读三代/_released_head/D1 有界持有、TTS 首 chunk 早发·LLM 流内道、stall degrade/bypass、unclear 连续推进、LLM 首 token 道歉链（留传输 timeout）、ack 锚豁免、lecture_guard、尾部锚剥离（数字顿号剥离视锚格式）、say 步长度、prompt 域纪律。
保留：TTS 侧首子句/首 chunk、watchdog+synth-ext（重调阈值）、filler（减配额）、starve/storm/nudge、garbled-reask、late-final/热词回声/WA·digit 累积、stall close 降级为业务出口。

## 五、验收纪律

每刀聚焦测试+compileall；合并前全量；A/B 归因依赖账本可信（D2 是硬前置）；烧开关=删代码。

## 六、第一波落地记录（2026-10-01，全量 3779 绿）

| commit | 内容 |
|---|---|
| e9be38a | baseline：主仓工作区实况快照（102 文件） |
| 1193eea | D2-1 defer-ack 票据劈叉：注册补 history=False + hygiene 逐车道断言+配对不变量（旧计数被 docstring 喂绿） |
| a72fe51 | D2-2 nudge 护窗不过重挂 2s 复查（`_nudge_next_action` 纯函数；cap 静默轮死气堵住） |
| 404ddff | D2-3 garbled-reask 求值上移至 stall 阶梯前 + garbled 轮不喂 streak（ASR 病不再算模型头上；守卫=整笔跳过写点，噪声误判 confirm 不抹真 stall 证据）。**已知副效应**：reask 现先于 DEFER/直念步/图/QA（命中即消费），挂起 say 步在连续烂转写+cap 期间可推迟——观察位 |
| 2ee0a02 | D2-4 复读全吞上抛：on_full_swallow 回调→拆 watchdog+放行 W-GATE+turns provider=repeat-suppressed（被吞全文证据保全）+REPEAT_SUPPRESSED 标记；不改 starve/_assistant_out（客户耳中确是静默，starve-ack 是正确可闻兜底） |
| 52355ba | 容量准入 capacity.py（vm_stat//proc/meminfo/nvidia-smi 零依赖探测、30s 缓存、fail-open 回档案 ceiling、409 detail 带计算明细、legacy 钉死）+ sensevoice 档 Qwen3 权重跳载（缺省跳、懒加载保 qwen3 回滚免重启、ASR_QWEN3_SKIPPED） |
| (本档) | 术语门禁 CSC 族白名单收口（HF Common Voice 数据集 lane 常量/指标字段/变量名，IGNORECASE） |

审计通过项：所有权互斥干净、车道顺序实证（reask 6540 < stall 6642 < DEFER 6692 < say 6721 < graph 6866 < QA 7231）、全量 3778+1 独立复跑、本机容量实测 mac 档内存紧时自动压到 1（只压不抬验证）。

## 七、吸收外部定案（主仓 631f80a，2026-10-01 9B 直连 A/B 台架）

另一会话在主仓落的 bench 结论，已收编（脚本 `scripts/bench_9b_direct.py` 带入本 worktree，AGENTS.md 条目留待合并时对齐）：

1. **模型定案，不重造**：hauhaucs mxfp4 ≡ huihui affine-4bit 全等（暖 TTFT 171-184ms / tps 38-42 / 冷 prefill 5888tok=18.9s 双同）。**D1 四臂全部用 huihui 现接线，零模型 bake-off；hauhaucs=验证过的热备。**
2. **prefill 算术有了硬地基**：M4 Pro 9B prefill 天花板 **311 tok/s**（暖 cache 187-195ms）。FLOW20 首 token 3-4s 尾巴定案=真前缀 miss 的秒级代价。**D1 的收益从"应该会快"变成算术必然**：现状峰值 prompt ~9.5k tok 冷 miss=18.9s、每轮 ~2000 uncached≈6.4s；槽位化后全冷 ~700tok≈2.2s、每轮增量 100-300tok≈0.3-1s。
3. **勘误吸收**：旧结论「hauhaucs 模板拒 assistant-first」是 LM Studio 模板层，非模型——直连 `[s,a,u]` 双 200。唯一共享限制=无尾 user 消息 404；槽位请求恒带尾 user=构造上安全。
4. **陷阱钉死**：mlx_lm server 请求 `model` 字段必须**绝对路径**，短 repo id 触发 HF 在线下载（实测卡死 300s）——model_routing detect/preset 填值面与 CUDA 部署面都要遵守；LM Studio 残值=模型仓库。
