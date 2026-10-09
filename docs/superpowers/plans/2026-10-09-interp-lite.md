# interp-lite：薄 B 线重建（2026-10-09 立项）

> 会话：`session-20261009-090913-bffc`（worktree `work-session-20261009-090913-bffc`）。
> 状态：**进行中**。窗 B 排程见 §8。

## 1. 论点（为什么有这条线）

我们 = 云端模型厂商（豆包/DeepSeek/MiniMax）+ LiveKit 套件，本应该轻。实测对照：

| 参照 | 全 worker 体量 | 最大域文件 | 形态 |
|---|---|---|---|
| 金喜同传（商用出货，m芯片 recovered） | **71 模块 / 12,694 行** | translate-channel.ts 2,179 | 域目录 + 一 provider 一 client + `cloud_only` profile |
| xiyu-ai（开源陪伴框架） | ~70 域文件 / ~30k | db.mjs 5,153（存储域） | 一域一文件 + providers/ 六模态适配器 |
| LiveKit 官方 examples | 薄入口 | — | AgentSession + 三行模型装配 |
| **我们（旧 B 线）** | interpret.py 单文件 **3,276**（且一周翻倍） | entrypoint 810 | 补偿机制织进编排层闭包 |

诊断：**重的不是业务，是补偿机制**——它们存在的原因是本地 mlx/Qwen3 档没有这些能力。
三份官方文档证明：云厂商把每一项补偿都作为产品参数在卖（§2）。薄线 = 官方参数 + 薄胶水。

## 2. 官方能力替代表（docs-first 基线）

文档基线（本地官方副本，钉进各 provider 文件头）：

- 豆包：`/Users/halo/Documents/bok/huoshan-api_副本.md`（bigmodel_async 流式 ASR）
- DeepSeek：`/Users/halo/Documents/bok/deepseek-api_副本.md`（chat completions）
- MiniMax：`/Users/halo/Documents/bok/minimax-api_副本.md`（t2a_v2_bidi 双向流式）

| 旧线自建（本地档逼出来的） | 官方替代 | 文档出处 |
|---|---|---|
| ASR 提交闸全家（clause/len/pause/frag+限速） | `enable_nonstream` 二遍识别 + VAD 段会话 + 末包负 seq 定稿（在役 provider 已实现） | huoshan §request |
| 首字延迟 hack | `enable_accelerate_text` + `accelerate_score` | huoshan |
| 热词四层拼装（B 线只用术语表层） | `corpus.context` 直传热词（100 token 上限） | huoshan |
| asr_polish 部分 | `correct_table`/`regex_correct_table` + `enable_ddc`（与语气标记冲突，臂后定） | huoshan |
| 起始吃字护栏 | `force_to_speech_time`（官方推荐 1000ms） | huoshan |
| MT 思考烧预算 | `thinking:{"type":"disabled"}`（官方**默认 enabled+high**，显式关是必选项） | deepseek |
| MT 前缀缓存命中不可见 | `usage.prompt_cache_hit_tokens` 回读（stream_options.include_usage） | deepseek |
| TTS 客户端攒句/首段铁闸 | bidi **服务端攒句**（逐字粒度合法，句末标点立即合成） | minimax §攒句 |
| TTS 头段催产 | `task_flush`（插件内 head-flush 已实现，BOK_TTS_FIRST_CHUNK_CHARS） | minimax |
| TTS 打断/背压 force-interrupt | `task_cancel`（打断后可继续不重建） | minimax |
| 语气词 160 行确定性转换 | **官方 19 枚标签清单进 MT 提示词**（LLM 生成；guard 只校验） | minimax task_continue.text |
| 情绪参数 | 官方「模型自动匹配，一般无需手动指定」→ 不下发 | minimax voice_setting.emotion |
| 停顿注入 | `<#x#>` [0.01,99.99]s + `(breath)` 在官方 19 标签内 | minimax |

**立法（新线适用，随本波入 AGENTS.md）**：新线任何自建机制必须注明「官方无对应物」或
「官方对应物实测不行（附数据）」——官方优先铁律下沉到厂商参数层。

## 3. Clean-room 红线

m芯片 `recovered/`（金喜还原源码）**永不入库、永不转录**——只允许行为描述与量化参数进文档。
我们自己 repo 的代码（interpret.py/doubao_asr.py/voice_style.py/MiniMaxTTS…）随意复用。

## 4. 目标树与预算

```
apps/agent/agent_runtime/interp_lite/     # CI 硬预算：Σ.py ≤3000（目标 ~1500），单文件 ≤600
├── __init__.py
├── worker.py            # entrypoint(JobContext) + run_interpreter（镜像旧线 worker 契约）
├── pipeline.py          # MT FIFO say worker + _LagLedger 配对 + turns 上报 + shutdown
├── voice_tags.py        # 官方 19 标签白名单 + TagGate（括号 token 跨 delta 缓冲校验）+ 字幕剥除
├── config.py            # 装配：instructions（19 标签版）/voices/boost/STT 参数档/语言归一
└── providers/
    ├── asr_doubao.py    # LiteDoubaoSTT(DoubaoSTT)：官方参数档（nonstream/force_to_speech_time）
    ├── mt_deepseek.py   # DeepSeekMT：httpx SSE 直连（无插件包装），thinking 显式 disabled，
    │                    #   stream_options.include_usage → prompt_cache_hit_tokens 入账本
    └── tts_minimax.py   # 复用 MiniMaxTTS（已是官方 bidi 契约实现）——见 §6 偏差说明
```

worker 契约（与旧线**同槽位**，CP/前端零感知）：agent_name=`bok-interp-fwd/rev`、
端口 8082/8083、`INTERP_DIRECTION`、`num_idle_processes=1`、端口单例守卫、
`GET :port/worker` 健康面、dispatch metadata 键（listen_identity/deliver_identity/
source_lang/target_lang/glossary/voices/persona_id）、`trans-<目标语言>` 具名轨、
订阅权限白名单（deliver 端 + fwd 侧 listen 端补授权）、turns line=b 分行落库、
settle 半场闸、SessionReport 带 worker 标识。

## 5. 语气词定案（prompt 生成 + 瘦 guard）

- MT system prompt 枚举**官方 19 标签**（`(laughs) (chuckle) (coughs) (clear-throat)
  (groans) (breath) (pant) (inhale) (exhale) (gasps) (sniffs) (sighs) (snorts) (burps)
  (lip-smacking) (humming) (hissing) (emm) (sneezes)`，speech-2.8 专属），规则=「每句至多
  一枚、只标真实发声、语境自然嵌入」。DeepSeek 指令遵循强；静态前缀命中官方上下文缓存。
- `TagGate`（~60 行）：括号 token 跨 delta 缓冲（`(`/（ 开口挂起 ≤8 字符），inner 经
  `voice_style.norm_voice_tag` 归一后对官方 19 集校验——命中→ASCII 规范形，未命中→
  原样透传（可能是内容括号）。字幕/落库剥除复用旧线 `_caption_text`/`_strip_voice_tags`。
- parity 测试：官方 19 集常量 == 文档派生字面；A 线 `VOICE_TAG_WHITELIST`(9) ⊆ 官方 19。
- 旧线 160 行确定性转换层（`_apply_voice_tags`）**不进新线**——其存在理由=本地 Hy-MT2
  对模板外指示无视（实测留档）。若漂移率高（TagGate 违约计数可观），证据触发回调。

## 6. 实现偏差记录（对批准计划的修正）

1. **包位置**：`apps/agent/agent_runtime/interp_lite/`（非顶层 `apps/interp/`）——零
   PYTHONPATH 手术（`_repo_pythonpath` 已含 apps/agent），预算测试扫该子树，隔离目标不变。
2. **TTS 复用不重写**：原计划「薄客户端全新实现 bidi」；读旧插件后确认 `MiniMaxTTS` +
   `_MiniMaxBidiStream` 已按官方 bidi 契约实现（持久连接/task_cancel/task_flush 头段
   催产/2205 原样重发/30s ping/region 端点），重写=重复造轮子（正是 10-09 审计否决项）。
   `tts_minimax.py`=装配层（2.8-turbo 档/voice map/boost）+ 文件头钉文档。
3. **ASR 子类化**：`LiteDoubaoSTT(DoubaoSTT)` 覆写 `_config()` 开官方参数档
   （enable_nonstream/force_to_speech_time/可选 accelerate），旧 provider 文件零改动；
   提交边界=在役「VAD 段会话+负 seq 定稿」设计（实弹定案），clause_commit/utt_merge
   双双 False（官方参数替代）。

## 7. 验收判据

- LOC 预算门绿（`tests/test_interp_lite_budget.py`：Σ≤3000、单文件≤600、常量调整=评审可见）。
- 全量 pytest 绿；ruff 全树真 bug 门绿 + 新文件门（本波扩 select）绿。
- 真栈（`BOK_INTERP_LITE=1 python tools/bok.py serve`）：`scripts/e2e/e2e_interpret.py`
  **8/8 PASS**（I1/I2/I5/I6 双向双语言对出声、I1b/I5b 分行落库、I3 启停×3、I4 长流 45s）。
- `scripts/probes/probe_interpret_latency.py` avg ≤3500ms（对照旧线 2223 基线；差距逐项
  归因进本档，按证据决定补带哪条机制）。
- 僵尸 worker 纪律：serve 前后 `ps aux | grep agent_runtime` 归零；探针逐条跑。
- serve 开关：`BOK_INTERP_LITE=1`（默认 0=旧线逐字节）；不进 `_FORWARD_ENV`（serve 侧
  开关，BOK_LOCAL_TTS 先例）；proc.py 僵尸扫描 marker 补 `agent_runtime.interp_lite`。

## 8. 不带项审计表（证据触发才补）

| 机制 | 为什么可以不带 | 触发补带条件 |
|---|---|---|
| spec-mt 投机翻译 | 旧线 MT 腿 ~260ms（15 字句），投机的收益窗已被流式 say 吃掉大半 | 延迟探针 first_ms 显著劣于旧线且归因到 MT 等待 |
| 碎片闸（frag hold-and-merge） | 豆包 VAD 段会话=天然提交边界；碎片形态源于本地 ASR 提交 | e2e I4 长流出现碎片轮 |
| backlog 背压/摘译 | 试点演示档并发低；say 队列串行本身就是背压 | 探针/实弹出现 lag 雪球（连续语流 >6s 积压） |
| ~~clause-commit~~ **已开（2026-10-09 用户拍板「必须边讲边出声」，当日触发当日开）** | 试点首版误判「可不带」——真连续无停顿长讲会整段等讲完才翻；现 `LiteDoubaoSTT(clause_commit=True, utt_merge=True)` 与旧线 B 档同旗同源，说话中子句边界（~1s 一拍）提前交 FINAL、MT 说话中起跑、对方滞后 1-2s 跟读 | 窗 B 评估官方 `fixed_prefix_result`/`end_window_size` 替代自家闸门（官方替代表 §2 该行的真身） |
| echo-dedup/回声守卫 | 双端各自订阅定向轨，串音面小 | 实弹出现本向回声轮 |
| asr_polish/stutter fold | DeepSeek prompt 已含 ASR 噪声纠错规则；`correct_table` 官方位待接 | e2e 误听率明显高于旧线 |
| 抢跑/preemptive | DeepSeek 首 token ~350ms + 流式 say 已流水线化 | first_ms 归因到 prefill 等待 |

## 9. 窗 B 排程（本窗不做）

1. provider 搬移共源化（doubao/MiniMax 从旧包提出独立薄文件，新旧线同源）。
2. 旧线 interpret.py 冻结（只修 P0 bug）；新线达标后 serve 默认翻转。
3. A 线（agent.py 9,582）同法评估——巨石棘轮基线待其对应窗口。
4. P0 文档欠账（REV_AUDIO 入 _FORWARD_ENV、LATENCY_BUDGETS/decisions/RUNBOOK 刷新、
   遗留 worktree 清理）=独立小窗。
