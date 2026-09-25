# 复核后可信执行计划（2026-09-26，四路 subagent 实弹复核定稿）

> 复核方式：四路并行长任务（栈主实弹 A/B / TTS 链离线实测 / 云端真实凭据实捅 / 漏斗
> 兼容性代码级走查），主会话统一审查后成文。基线 commit 3ed8c6a；本轮 smart-turn
> 实现已入库（默认关）。**每条结论都有实弹证据，不是调研推测。**

## 0. 复核结论总表

| 项 | 复核结论（证据级） | 对计划的影响 |
|---|---|---|
| V1 smart-turn 现模型 | **粤语退化实锤**：47% 真停嘴被判未说完、held 概率中位 0.019（系统性低估）、23 次 held 全部 800ms 超时 flush 零真并入=纯 +810ms 首声（offscript 50 轮实弹）；普通话腿 p 0.24-0.35 正常 | 不能用于粤语 A 线；zh/en 可用 |
| V1 smart-turn 工程面 | 接入完成（kill-switch `BOK_SMART_TURN` 默认关，fail-open 三层，onnxruntime 35-130ms 不阻环，A/B barge_in 打断安全无损） | 代码已就位，开闸即用 |
| V3 LiveKit 内置 detector | 1.8.2 在位（14 语含 zh 无 yue）；杀句级重叠 | 只作对照，不主用 |
| V4 TTS SFT 全链 | **全链打通**（含最险的 checkpoint→8bit→sidecar 跳，产物与生产同构、sidecar 零改动）；手册=TTS-SFT-PLAYBOOK.md | 主路径放行，只差 CUDA 训练 |
| V5 VoxCPM2 | Mac 可用、粤语零样本可懂、RTF 0.95、但 10.4G 内存无 MLX 量化 | 粤语 B 计划/对照组 |
| V6 speculative reopen | 兼容矩阵+集成设计+回归清单齐；两硬冲突（say 先烧账本/已出声召回状态双烧）均有绕开姿势；我们已有半套（join-hold 同构） | M 量级，排在 smart-turn 粤语结论后 |
| V7 Qwen realtime | **key 全通**（4 次真会话）；协议高兼容；**qwen3-omni-flash-realtime 跟输入语言回粤语**；¥0.22-0.3/3 分钟通；适配器 3-4 人日 | realtime 演示档放行，粤语腿不用混合模式 |
| V8 MiniMax ASR | CER≈30%（粤语），不可作裁判 | 审计线不立项，降级可选旁证 |
| V10 prompt-cache 6GB | 单通无损（cached 0.906→0.901、TTFT 噪声级）、省 6G | 单通/演示档用 6GB，并发档留 12GB |
| V17 画布使用 | 审计无入口面字段，不可判定 | 最小埋点（两处审计 keys/fields）先上，数据说话再收缩 |

## 1. 阶段 A —— Mac 本周（零 CUDA、零新凭据）

1. **画布收缩+埋点**：`PUT /api/templates` 审计 detail 加 `keys=sorted(payload.keys())`、
   `PATCH /api/qa-entries` 加 `fields`（零 schema/零 web 改动，V17 方案）→ 观察一周期
   → 导航撤画布（代码保留开发者开关）。运营面收敛=话术表单+QA 表单+学习 tab。
2. **smart-turn 车道化**：按通话语言条件开闸（zh/en 通话 `BOK_SMART_TURN=1` 生效、
   cantonese 恒关）——接线处已有 per-call 语言，一行判定+offscript zh 腿复跑。
3. **prompt-cache 分档**：单通/演示预置 6GB（V10 无损实证），并发档 12GB 不动。
4. **QA 词库喂库**（G7）：L-① 采纳+聚类+pregen 进日常；would-hit 进 soak 常设段+驾驶舱铃铛。
5. **双路演示档验收**：`load_audio_concurrency=2`（判据 p50 感知 ≤2.5s、零 swap 爬升）。

## 2. 阶段 B —— realtime 演示档（A 线内，sk-ws key 现成）

1. **QwenRealtimeModel 适配器**（livekit openai realtime 插件薄子类，3-4 人日）：
   差异点已实锤——`session.finish` 私有事件、转写 delta 带 stash、音色按模型族白名单
   （audio-3.0=longan 系/omni=Cherry 系，错音色**整条 update 被拒**）、无 FC。
2. **建单 mode 档**：`call.mode=realtime_demo` → dispatch realtime worker（CP/审计/UI
   全保留；话术漏斗该档不生效，演示对象恒假数据=出境红线）。**粤语腿直接
   qwen3-omni-flash-realtime（实测跟随输入语言回粤语），不需要混合模式**。
3. 计费护栏：会话时长上限+usage 落账（response.done 自带 usage，实测 token 率
   13.5-26 tok/s）。

## 3. 阶段 C —— TTS 真人感（数据先行，Mac 无 GPU 依赖）

1. **数据准备现在开工**：工具链=TTS-SFT-DATA-PREP.md（七步全 Mac；先查录音分轨）。
   每语言 10-30 分钟/音色，人工校对纪律（数字必复核）。
2. CUDA 窗口跑 SFT（命令=PLAYBOOK §3；epoch 存档纪律+7GB/epoch 磁盘账）。
3. 部署链已验证：convert 8bit → 换模型目录 → **重跑 tts-pregen**（缓存键含 model）→
   上线试听。粤语发音不达标 → VoxCPM2 对照腿（PLAYBOOK §6）。

## 4. 阶段 D —— CUDA 一次性窗口（打包清单，一次开窗全做）

SGLang（G5 根修：priority/lpm/radix）｜Huihui-9B offscript 切档判据（4B/9B 质量差
值不值得速度差）｜MT-9B+润色（enable_thinking 车道旗）｜云端 ASR 流式对照
（Qwen3-ASR-Flash-Streaming）｜TTS SFT（阶段 C 数据就绪即并窗）。
（smart-turn 粤语微调可选腿：pipecat 训练管线开源+27 万条数据+我们录音——
排在 zh/en 车道化收益评估后。）

## 5. 阶段 E —— speculative reopen（M 量级，后置）

集成设计已定稿（tracker 放 STT 层/处置放漏斗层/`BOK_SPEC_REOPEN` 默认关进
_FORWARD_ENV/只召回未出声轮起步/say 轮修订窗内禁召回只丢续讲/judge 掺 revision 号）。
**触发条件**：smart-turn zh/en 车道化后仍残留的劈轮残差 → 才值得做。
回归门=11 单测+7 探针清单（V6 报告原文已存档）。

## 6. 否决/退役项（终局）

fork speech-to-speech 当底盘（3 个月+、无监督、粤语零验证）｜MiniMax ASR 审计线
（CER 30%）｜LiveKit detector 主用（杀句级重叠）｜9B 常驻（已退场）｜
CosyVoice「315 条 LoRA」说法（查证不实）。

## 7. 验收矩阵

| 阶段 | 探针/判据 |
|---|---|
| A | 画布埋点上线+一周期数据；zh 腿 offscript 劈轮下降；cache 6GB 档 soak 无回归；would-hit ≥30% |
| B | realtime 演示通话三语各一（粤=omni-flash-realtime）；打断/收线正常；usage 落账 |
| C | SFT 音色试听过审（粤语发音+客服腔）；pregen 全绿 |
| D | p50 感知 ≤1.2s；优先级 A/B 显效；9B 切档判据出数 |
| E | 11 单测+7 探针全绿（含 barge_in 硬门槛） |
