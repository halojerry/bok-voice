# 2026-09-24 · Mac-First 语音质量收敛计划

**背景**：CUDA 盒（117.50.180.185，RTX 4090 48GB）按小时计费，已关机。所有开发与测试转 Mac；全部 CUDA 相关验证收敛到**一次开机窗口**跑完。A 机（43.167.209.254）SSH 仍被卡（sshd 疑似 MaxStartups 洪水），不受本计划阻塞。

**Mac 测试边界**（先讲清楚什么能测什么不能）：

| 能在 Mac 测 | 留给 CUDA 窗口 |
|---|---|
| 三语话术 / 标记与停顿 / MiniMax 云 TTS（2.8）/ 垫话梯子 / TurnDetector v1-mini（本地 CPU ~108MB）/ 9B judge 纪律 / 全部离线单测与三语 E2E / barge-in / offscript soak | **并发负载**（Mac GPU 争抢：ASR partials 拖 LLM TTFT +24%，单腿顺序测试可信、并发结论不可信）/ ASR CUDA 档 / llama `-np 3` 多槽 / 打包与节点拓扑实测 |

**已定决策**（本计划的前提）：TTS 统一 MiniMax（音色一致）；LLM 本地（**Mac 表默认=4B avan-ag，2026-09-25 核实：设置页从未设过 9B、运行车道一直 4B；「9B」=CUDA/llama.cpp 车道默认+质量目标，对话车道三方 A/B 见 5.x**）；EOT 官方无粤语档（v1 云=隐私否决，v1-mini 本地=无粤语档校准、回落英文 0.36）；标记集 = (breath)(inhale)(exhale)(clear-throat)(emm)（砍 (coughs)）；回复头部禁标记/停顿；`<#x#>` 停顿无模型限制。

---

## 阶段 0 · 收口与基线（半天）

- [ ] 0.1 commit 本地 17+ 文件改动（livekit_plugins / agent.py / bok.py / e2e_real_customer / probe_latency_soak / seed_invite_templates / mm_llm_shim / gpu_contention_probe / bringup 文档 / 2 个测试文件，17+ 测试绿）——**等 Ethan 一声**
- [ ] 0.2 Mac `bok.py serve` 起栈；三语邀约话术 reseed（2026-09-24 改写稿：zh -38% / 粤 -36% / en -24%）；TTS 渲染三语耳测（人感+标记禁入 ref 核对）
- [ ] 0.3 基线 soak：invite 三语 + pause 场景，记 p50/p90 落盘——后续每阶段对照这张表
- [ ] 0.4 核对 livekit-agents 实装 1.8.2 vs 锁版/AGENTS.md 记录的 1.8.0（子代理在 venv 实测为 1.8.2），不一致就更新锁版与文档
- [x] 0.5 **核实 ASR device=cuda 的固化点**（2026-09-24 结案）：**已固化，无缺口**——bok.py 启动层 `_cuda()` → `QWEN3_ASR_DEVICE=cuda` 自动钉（:1335，非本会话引入、仓库既有）+ sidecar `_resolve_device()` 双保险（env 覆写 > torch.cuda 可用即 cuda > cpu）。箱上当时跑 cpu 是孤儿栈手工启动姿势的产物，非仓库缺口；窗口 6.2 干净 bring-up 即自动 cuda，一键电池加一条断言：ASR 日志 `device=cuda`（app.py:236/252 会打）

## 阶段 1 · P1 TTS 人感包

- [ ] 1.1 A 线 `MINIMAX_MODEL` 钉 2.8 系（settings 默认 + 非 2.8 回退档标记自动熄火，B 线 `_voice_tags_supported` 双门控同款）
- [ ] 1.2 标记管线移植 A 线：白名单正则（五标记 + `<#x#>`，小写归一、未知括号词剥掉防 4B 自创 `(微笑)`）；出口过滤挂 LLM/TTS 接缝；字幕 / turns 落库 / LLM 历史三下游剥离；tts_cache 键含标记文本 → pregen 同步
- [ ] 1.3 prompt「说话自然度」块进静态前缀（标准书面中文、KV 静态）：五标记字符级示例、`<#0.3#>` 停顿示例、(emm) 只准查询轮、每轮≤1 标记每通≤3、开场词轮换、半途换句不道歉；收编官方 `tts_text_transforms`（markdown/emoji 过滤）
- [ ] 1.4 验收：离线单测（白名单/剥离/熄火三族）+ 渲染耳测 + soak 无回归

## 阶段 2 · P2 垫音梯子（三级，覆盖 TTS 尖刺）

- [ ] 2.1 键盘音床资产：numpy 程序化合成（随机间隔滤波按键声），无缝 loop 4-6s ×3 变体，零版权零依赖
- [ ] 2.2 `FillersDirector` T3：~2.8s 起床循环 −10dB 随机起点；回复首帧 150ms 淡出；每通≤2 次；无语言属性三语通用；走 `BackgroundAudioPlayer` out-of-band 音轨（不进 speech 队列不进 LLM 上下文）
- [ ] 2.3 验收：`probe_filler_timing` 首声 <2.5s 不破；soak 加尖刺覆盖统计（漏点计数——TTS>1s 轮中床/垫话盖到最后一帧的比例）

## 阶段 3 · 轮次检测定档（Mac 全可测）

- [ ] 3.1 粤语 detector 阈值扫描三腿：`BOK_TURN_DETECTOR_THRESHOLD` 0.45/0.50 × `ENDPOINT_MAX_DELAY` 0.6/1.2（`TURN_DETECTION=detector` 重启 worker，注意零殭尸 worker 铁律）；判据 = pause-canto 劈轮数 × invite-canto 正常轮 p50 的 Pareto
- [ ] 3.2 zh / en 急档复核（官方校准值 zh 0.355 / en 0.36 × max 0.6），对照 stt 基线 −340ms 是否复现
- [ ] 3.3 定案按语言分流表（zh/en/canto 各自 detector 或 stt），阈值进 `repository.default_settings`；找不到粤语甜点就 canto 留 stt
- [ ] 3.4 盯官方 `languages.py`：粤语语言档上线即换按语言 dict 校准（env 钩子已就位）

## 阶段 4 · P3 judge 纪律 + 人工交接

- [ ] 4.1 背景 judge 补三件官方纪律：同题去重（同参数重复轮不重烧 9B）、客户换话题主动取消在途判定、迟到结果必送达（`judge_pending_expired` 静默丢弃改显式送达语义）
- [ ] 4.2 notify_human 附第一人称交接 briefing（warm-transfer 官方样板）：turns + `_intent_facts_snapshot` → 100-200 字摘要给主管接收面
- [ ] 4.3 验收：judge 单测族 + 打铃链路手测

## 阶段 5 · Mac 总回归

- [ ] 全 pytest + 三语 E2E（真 `/api/token`）+ `e2e_barge_in` + `probe_offscript_soak` + `probe_filler_timing` + interpret 延迟探针
- [ ] soak p50/p90 对照阶段 0 基线出对比表（人感包+梯子+轮次档的净收益）
- [ ] 5.v **Mac 实机并发电池（2026-09-24 Ethan 定案：多组多轮多并发 + A/B 线同开）**：`load_audio_concurrency.py`（已补 `LOAD_TEMPLATE_ID` 钉邀约模板）阶梯 A2/A4/A6 路 × 3 轮（每级一跑，逐轮首声 + 哑轮率）；B 腿=`e2e_interpret.py` ×2 并行（每通=fwd+rev 双 worker + MT :1236）；**AB 同开腿**=A4 + B×2 同时开火，看 A 线 TTFT 相对 A4 单独跑的增量与 B 线退化。段落归因走 turns 表（worker 已带 token，PERCEIVED 账本恢复）。已知口径：旧"TTS sidecar 全局锁"约束已随 TTS 上云失效，本电池实测新的天花板在哪（嫌疑=mlx :1235 请求串行）；A/B 同开前必须确认基线 soak 已收（互污染）。报告落 `reports/mac-concurrency-2026-09-24/`
- [ ] 5.x（可选 A/B）LLM 候选：`qwen3.5-9b-uncensored`（mlx 8bit）经现有 :1235 sidecar 接入（bok.py MODELS 加条目），对照现 9B——判据四件：soak TTFT/cached 命中、`probe_reply_quality`、offscript soak、jump-speech 探针（abliterated 重点盯指令遵循/复制引力）。**不换 LM Studio 引擎**：同 mlx Metal kernel 无量级增益，丢 `cached=N/M` 观测面（KV 架构验证命脉），.lmstudio 路径断层有前科；可选 30 分钟背靠背基准（同模型同前缀重放）拿数据；连带决策：ASR 不换 Whisper/ANE/ExecuTorch（Qwen3-ASR 无 CoreML 路，换引擎=放弃热词/句级提交/join-hold全家+Mac 与 CUDA 分叉）
- [ ] 5.y **演示档（Mac demo posture，2026-09-24 新增——Mac 要给人演示，流畅度一等公民）**：①`scripts/demo_preflight.sh` 一键预热体检：起栈→LLM 前缀预热→罐头/垫话物化核对→开场白 TTS 缓存温热→零殭尸核查→出体检单；②演示配置：单通话、背景意图 judge 关（`BOK_FLOW_GRAPH_JUDGE=0`，除非演示点就是图意图）、ASR partial 抑制确认开、垫话物化优先命中；③演示腿判据：invite-zh/canto 单通话 p50 ≤1.2s、零哑轮、尖刺轮全被梯子盖住；④演示走生产架构本身，不经 LM Studio/ExecuTorch 中转换活线（换引擎救不了链路结构；ExecuTorch ASR 0.0.8 = Whisper 系无热词无句级契约，观察项）
- [ ] 5.z（bench）**Whisper vs Qwen3-ASR 三腿对照（2026-09-24 定案，bench 层活线零改动）**：腿1 转写质量 A/B（同语料：16 碎裂句+三语 e2e wav+数字串，盯数字/品牌词/粤语三维）；腿2 单发延迟（whisper.cpp 墙钟 vs sidecar finish）；腿3 GPU 自由度探针（:1235 固定前缀 LLM TTFT × {空闲 / Qwen3-ASR 循环 / whisper-Metal 循环 / whisper-CoreML(ANE encoder) 循环}——第四格≈第一格=分流假设成立）。工具：whisper.cpp 源码编译（`WHISPER_COREML=ON`，先查 Xcode CLT）+ `ggml-large-v3-turbo`(1.6GB)（+large-v3 3.1GB 可选）+ LM Studio ExecuTorch ASR 对照。**判据预先押定**：活线换=腿1三维全不输 AND 腿3 ANE 格≈基线 AND 接受句级提交重写；演示档局部采纳=仅腿3成立；全不满足=对账表记"Whisper/ANE 实测否决"闭题。产出 `scripts/asr_whisper_bench.py` + 报告落盘 `reports/asr-whisper-bench/`
  - **执行进度（2026-09-24）**：ANE 腿已完成（`reports/asr-whisper-bench/ane-leg.md`，whisper.cpp d09f61a 双构建 + encoder-only CoreML）——LLM TTFT：9B 空闲 328.6 / Metal 循环 +21% / **CoreML(ANE) 循环 +1%（≈归零，假设成立）**；4B 档 +53%→+19%（残余=decoder 仍走 Metal）；ANE 档转写吞吐 -21%（M4 Pro GPU 强，价值在让 GPU 不在提速）。
  - **终裁（2026-09-24 两腿合齐，闭题）**：LM Studio 1.1.0 无头 STT 死路（无 HTTP 路由/`lms load` 不认，实测定案）→ 改道 mlx-whisper 同权重对打。**腿1 质量三维 whisper 全输**（数字 0.807<0.812 / 品牌 5<7 / 粤 CER 0.849 灾难+复读幻觉）→ 活线换 ❌；**腿3 我们自家 ASR 连续解码下 LLM TTFT ≈零差**（233.5 vs 空闲 233.8）→ 演示档 ANE 无病可治 ❌。记对账表「Whisper/ANE 实测否决」。**意外收获=真 bug**：Qwen3 把逐位口播「三七七八九零」改写成「三千七百九零」（数词化）——WhatsApp 捕获命门风险，见停车场新条目。老结论「partial +24% TTFT」与今测不符，以今日为准。

## 阶段 6 · CUDA 一次性验证窗口（开机即跑，目标 ≤3h）

- [ ] 6.1 **预备（Mac 上先做好）**：`scripts/cuda_revalidate.sh` 一键电池——健康检查 → reseed → probe 全家（soak/latency/offscript/barge-in）→ 并发 `load_audio_concurrency` → 报告落盘 `reports/cuda-revalidate/`。开机窗口按小时计费，一切能脚本化的不许手跑
- [ ] 6.2 窗口内容：干净 bring-up（git pull、ASR CUDA、llama `-np 3 -c 30720`、FireRed 保持停、无殭尸 worker 核查；**根因核实 `bok.py serve` 在箱上等 :3000 卡死那次**——正确启动序/node_agent 托管 UI 写进 bringup 文档）→ 一键电池 → soak 对照 Mac 基线（重点看并发与 TTS 尖刺分布是否随平台变化）
- [ ] 6.2b 一键电池内置两条箱上踩过的坑的规避：探针**不读 `/api/settings` 拿 MiniMax key**（匿名访问被掩码出假 1004，直读 DB 列）；远程脚本只允许双引号嵌套（单引号 heredoc 互炸的教训）
- [ ] 6.3 收尾：箱上重复模板清理（d6c2cca17257 / 61ee6bb71cec——届时找 Ethan CONFIRM）→ `build_node_pkg.sh` 打包验证 → 关机

## 阶段 7 · A 机部署（被 SSH 卡，独立轨道）

- [ ] sshd MaxStartups 修复或 SSH 挪离 22 端口（Ethan 侧 journalctl 排查）
- [ ] CP + MiniMax relay 部署（`deploy/onprem/nginx-a-relay.conf`）
- [ ] 两机拓扑联调（B 机呼 A 机 relay）

---

## 停车场（记录在案、不阻塞主线）

| 项 | 触发条件 |
|---|---|
| TTS 对冲请求（hedge，700ms 同声重发赛跑） | 梯子上线后尖刺漏点 >5% 才启用 |
| ~~qwen3-tts-flash 罐头线~~ | **已放弃（2026-09-24 Ethan 拍板弃 DashScope/qwen TTS）**——TTS 终局单一 MiniMax，本地 Qwen3-TTS sidecar 仅剩 bench 用途 |
| A 线 ASR finish 深调（250→130ms） | 阶段 0 基线若 eou 段占比仍 >45% 再动 |
| 官方 aec_warmup_duration / metrics_collected 遥测 | 阶段 1 顺手项之外的下一次触碰 audio 配置时 |
| MiniMax 横评补测（Cartesia/VoxCPM 已否决，不回头） | — |
| **数字口播数词化 bug（whisper bench 意外捕获）**：Qwen3-ASR 把逐位口播「三七七八九零」转写成「三千七百九零」，`_valid_digit_runs` 会从错误转写抽错号码——WhatsApp 捕获域。复现证据在 `reports/asr-whisper-bench/results.json`（数字串句逐条） | 修法候选：ASR 侧逐位转写引导（customizable context 是否吃非词表指令待验）vs agent 侧 WA 轮数词→逐位还原；先加一条 e2e 边界用例钉住行为再动手 |

---

## 附 · 2026-09-24 CUDA 箱问题对账表（一条不丢）

### 环境层（箱特有，窗口/固化处置）

| # | 问题 | 处置 | 归属 |
|---|---|---|---|
| E1 | ASR 被钉 CPU（finish p50 1330ms） | 切 CUDA 250ms，soak 2184→1249ms（-43%）；**固化点待核实** | 0.5 / 6.2 |
| E2 | llama `-np 6 -c 30720`=5120/slot，prompt 5131→400→LLM_FALLBACK | `-np 3 -c 30720`=10240/slot（/props 验证） | 已修 bok.py，待 commit 0.1 |
| E3 | FireRed 15GB 占 VRAM 拖累 | 停掉（14.2GB→30.4GB used），窗口保持停 | 6.2 |
| E4 | `bok.py serve` 等 :3000 卡死→孤儿栈 | 窗口干净 bring-up + 启动序根因核实 | 6.2 |
| E5 | SSH 密码时好时坏 | ed25519 key 已装（authorized_keys 持久） | ✅ 完成 |
| E6 | 殭尸 worker 跑旧代码污染 A/B | 零殭尸核查进一键电池 | 6.2 |
| E7 | 箱上重复模板 d6c2cca17257 / 61ee6bb71cec | 届时 CONFIRM 后删 | 6.3 |

### 工具/代码层（已修，全在待 commit 的 17 文件里）

| # | 问题 | 处置 |
|---|---|---|
| C1 | PERCEIVED 探针日志路径 mac-only | `_default_log_dir()` 跨平台（env > Darwin > Linux XDG） |
| C2 | 探针 wav 找不到 / `language=zh`→`"Zh"` 500 | `PROBE_WAV` env + 免 language 参数 |
| C3 | MiniMax 假 1004（`/api/settings` 匿名掩码） | 探针直读 DB 列；规避写进一键电池 6.2b |
| C4 | shim `/v3/...` 404 | 正确路径 `/v1/text/chatcompletion_v2` |
| C5 | Mimosa SSRF 拦截 ×2（shim+探针） | host 白名单 + ipaddress 校验守卫 |
| C6 | 本地 TTS `Unsupported speakers` 崩 5/7 轮 | `_resolve_local_voice` 音色解析器 |
| C7 | 本地 TTS 交替 12-13.8s 轮（RTF 1.52 + `_gen_lock` 串行） | 本地活线判死，罐头线专用；活线还原 minimax |
| C8 | EN 模板 400 `invalid_branch_text` | 分支改客户视角口语 |
| C9 | ssh heredoc 单引号互炸 | 远程脚本双引号纪律（6.2b） |

### 未解/结论层

| # | 项 | 状态 |
|---|---|---|
| U1 | ~~DashScope key#1 401×12~~ | **已关闭（2026-09-24）**——弃 DashScope/qwen TTS，key 作废，无需排查；key#2 流式全系 ModelNotFound（batch-only）一并作废 |
| U2 | MiniMax M2.x 无法关思考；abab6.5s 非思考 447ms 无前缀缓存 | 结论——MiniMax 云 LLM 弃，本地 9B 留 |
| U3 | MiniMax TTS 首响尖刺 25%（p90 2395ms） | P2 梯子治；hedge 停车场 |
| U4 | 4090 无 GPU 争抢（微基准双向：llama TTFT 104→94ms under ASR） | 结论——争抢是 Mac 特有，测量边界进计划 |
| U5 | livekit-agents 1.8.0（文档）vs 1.8.2（venv） | 0.4 核对锁版 |
| U6 | A 机 SSH kex_exchange_identification（TCP 通） | 独立轨道 阶段 7 |
