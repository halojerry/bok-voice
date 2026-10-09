# scripts/ 索引（G1；G1c 起顶层 .py 清零）

> 本索引由 `tests/test_scripts_index.py` 钉住：新增/删除/改名 `scripts/` 下任何 .py 必须同步本表，否则测试红；「最近证据」= 最近一次实质提交（G1a 引导头机械提交 69f89fa 不计）。
> 覆盖 `scripts/` 下全部受管 .py（不含 `cuda/`、`artifacts/`、`__pycache__/`；含 `archive/`）——当前 135 个：`probes/` 51 + `bench/` 19 + `e2e/` 10 + `seed/` 15 + `pipeline/` 11 + `ops/` 11 + `runtime/` 3 + `lib/` 3 + `archive/` 12（计数由 `test_scripts_index` 双向钉，以表为准）。**顶层只剩 `.sh`/`.ps1`/`.spec` 安装与构建入口**——发行管线（release.yml 断言/deploy 脚本）引用且无 PR 期验证，搬迁需发行干跑先行，故留顶层。`cuda/` 是独立交付包、`artifacts/` 是生成物（CI 钉路径），均不在本表。**桶语义**：probes=真栈行为取证 / bench=台架压测测量 / e2e=整通验收 / pipeline=模型与数据管线 / seed=资产导入种子 / ops=构建安装门禁与仓治理 / runtime=产品运行时 exec（CP/bok 契约）/ lib=被跨桶 import 的助手（引导头桶集合成员）。
> 每个受管 .py 头部含 G1 引导头（marker `# --- scripts import bootstrap (G1) ---`），见 `docs/superpowers/plans/2026-10-04-repo-governance-plan.md` §3.1；`tests/test_script_bootstrap.py` 收口；`scripts/ops/import_smoke.py` 逐件装载冒烟（CI ci.yml 常驻步）。

| 脚本 | 用途 | 消费者 | 真栈 | 最近证据 |
| --- | --- | --- | --- | --- |
| `scripts/archive/acceptance_0913_ghost.py` | 0913 实机验收·幽灵 job 探针(C2)。 | 无(手动) | 是 | 2026-10-05 |
| `scripts/archive/acceptance_0913_scenarios.py` | 0913 实机验收·场景驱动(C1 暂停黑洞 / C3+C4 号长闸与拜拜分流)。 | test_cantonese_terminology.py | 是 | 2026-10-05 |
| `scripts/archive/asr_whisper_ane_leg.py` | Whisper CoreML(ANE) 档决定性腿：LLM TTFT 三格对照（idle / Metal 循环 / CoreML 循环）。 | 无(手动) | 是 | 2026-10-05 |
| `scripts/archive/probe_e4_timing.py` | E4 时序探针：复现「回复中打断」场景并逐步打点用户轮提交/回复语音时延。 | 无(手动) | 是 | 2026-10-05 |
| `scripts/archive/probe_minimax_emotion_tags.py` | MiniMax 拟声标记 + emotion 探针(2026-09-16):A 线「更像真人」两问实弹验收。 | 无(手动) | 是 | 2026-10-05 |
| `scripts/archive/probe_minimax_speed_ab.py` | MiniMax 语速 A/B 探针(2026-09-12):classic HTTP vs bidi WS 是否真吃 speed。 | 无(手动) | 是 | 2026-10-05 |
| `scripts/archive/probe_mt_lang_validator.py` | B 线 MT 语言校验器**实效探针**(E5 增补验收:量化「它到底多久才响一次」)。 | 无(手动) | 是 | 2026-10-05 |
| `scripts/archive/probe_qa_phonetic.py` | 粤语音系补位层实弹验收探针(2026-09-22)。 | 无(手动) | 是 | 2026-10-05 |
| `scripts/archive/r1_build_gold.py` | R1 真实标注集构建（2026-09-21）。 | 无(手动) | 是 | 2026-10-05 |
| `scripts/archive/t56r1_probe.py` | T5/T6/R1 联合探针（2026-09-21）。 | 无(手动) | 是 | 2026-10-05 |
| `scripts/archive/test_deepseek.py` | 经 LiveKit LLM 插件打 DeepSeek 验 key（归档）。 | 无(手动) | 是 | 2026-10-04 |
| `scripts/archive/test_volcano_v3.py` | 火山 TTS V3 单向流式手动验证（不泄漏密钥，归档）。 | test_cantonese_terminology.py | 是 | 2026-10-04 |
| `scripts/bench/ab_slot_actor.py` | D1 槽位化 actor 四臂 A/B 台架（2026-10-01，第一性原理重构计划 §三 D1）。 | test_ab_slot_actor.py import / test_ab_slot_actor.py | 是 | 2026-10-04 |
| `scripts/bench/ab_tts_first_chunk.py` | MiniMax bidi 首 chunk 提前切 —— 耳朵 A/B 样本生成器（2026-09-28）。 | test_cantonese_terminology.py | 是 | 2026-10-04 |
| `scripts/bench/ab_vad_ten_vs_silero.py` | TEN VAD vs inference.VAD(silero) 语音端点 A/B。 | 无(手动) | 是 | 2026-09-15 |
| `scripts/bench/asr_whisper_bench.py` | Whisper vs Qwen3-ASR 对照 bench（腿1 质量 / 腿2 延迟 / 腿3 GPU 争抢）。 | probe_cloud_asr.py import / test_cantonese_terminology.py | 是 | 2026-09-25 |
| `scripts/bench/bench_9b_direct.py` | 9B 直连 A/B 台架(2026-09-29):同栈 mlx_lm server、同参、独占 GPU、逐模型顺序测。 | 无(手动) | 是 | 2026-10-01 |
| `scripts/bench/bench_judge_contention.py` | judge 跨进程争用台架（批次0.2 余项，2026-10-03）。 | 无(手动) | 是 | 2026-10-03 |
| `scripts/bench/bench_minimax_bidi.py` | MiniMax bidi 直连台架(2026-09-29,Ethan「查官方文档,是不是我们配置有问题」)。 | test_cantonese_terminology.py / test_bidi_head_flush.py | 是 | 2026-10-03 |
| `scripts/bench/bench_voice_tags_ab.py` | A 线语气标记 ±对照真合成台架（2026-10-06）——3 语×2 句×5 变体(plain/breath/emm/sighs/pause)×2 档,人耳 A/B 用,sighs 扩白名单听证。 | 无(手动) / test_scripts_index.py | 是 | 2026-10-06 |
| `scripts/bench/e4_frequency_scan.py` | E4 改口检测真实频次扫描（只读、可重跑、纯 stdlib）。 | 无(手动) | 否 | 2026-10-04 |
| `scripts/bench/gpu_contention_probe.py` | GPU 同卡争抢微基准（B 机 CUDA 节点，2026-09-24 D 项归因）。 | asr_whisper_bench.py / asr_whisper_ane_leg.py | 是 | 2026-09-25 |
| `scripts/bench/load_audio_concurrency.py` | 音频链路并发压测：4 路真实通话（各自 call/token/房间）同时进行 3 轮对话。 | security/mimosa/suppressions.json / AGENTS.md | 是 | 2026-10-04 |
| `scripts/bench/load_cp_concurrency.py` | CP API 并发压测：独立 CP 实例(:8001)+临时 DB，零污染真实数据。 | CP main.py / test_load_cp_db_reservation.py | 是 | 2026-10-04 |
| `scripts/bench/loadtest_calls.py` | A 线并发负载 E2E（2026-09-07 QA 新增）。 | 无(手动) | 是 | 2026-09-23 |
| `scripts/bench/measure_latency.py` | 逐阶段延迟实测：ASR / LLM 回复 / 翻译 / TTS / 总链路估算。 | test_probe_stimulus.py / LATENCY_BUDGETS.md | 是 | 2026-09-05 |
| `scripts/bench/measure_prompt.py` | 逐段量度实际注入 LLM 的 system 体积（KV-cache 前置的验收工具）。 | 3 脚本 import / probe_judge_parity.py | 否 | 2026-09-22 |
| `scripts/bench/probe_deepseek_cloud.py` | DeepSeek 云端探活（A线优化总计划 · 批次0.5）。 | 无(手动) | 是 | 2026-10-03 |
| `scripts/bench/probe_minimax_bidi_cold.py` | bidi 冷启动归因探针(2026-09-10)。 | 无(手动) | 是 | 2026-09-11 |
| `scripts/bench/redteam_probes.py` | Bok CP 认证/节点面红队探针（防御性自检，docs/SECURITY_REDTEAM.md [probe-1..9]）。 | SECURITY_REDTEAM.md | 是 | 2026-10-04 |
| `scripts/bench/soak_test.py` | A 线长稳 soak（2026-09-07 QA 新增）——渐进泄漏检测。 | 无(手动) | 是 | 2026-09-23 |
| `scripts/e2e/e2e_barge_in.py` | A 线打断（barge-in）E2E：AI 播报中插话 → 断言打断生效、不哑火、无崩溃。 | 3 脚本 import / test_garbled_reask.py | 是 | 2026-10-02 |
| `scripts/e2e/e2e_campaign.py` | 外呼战役 E2E（mock 档全链路）：3 对象——1 接通走完话术 / 1 无人接 / 1 接通即挂。 | probe_vad_head_syllable.py / probe_8khz_asr.py | 是 | 2026-10-04 |
| `scripts/e2e/e2e_edge_cases.py` | A 线边角 E2E（2026-09-07 全链路回归新增）。 | probe_interp_backlog.py import / test_probe_stimulus.py | 是 | 2026-09-23 |
| `scripts/e2e/e2e_flow_scenario.py` | 理赔分步场景多轮粤语 E2E：10+ 轮真实对话评估。 | 无(手动) | 是 | 2026-09-23 |
| `scripts/e2e/e2e_http.py` | 对运行中的 CP（:8000）做端到端 HTTP 冒烟（旧档）。 | 无(手动) | 是 | 2026-08-30 |
| `scripts/e2e/e2e_interpret.py` | B 线同传 E2E（2026-09-07 全链路回归新增）——v2 双 AgentSession 解释器首次端到端。 | 4 脚本 import / probe_interp_backlog.py | 是 | 2026-10-02 |
| `scripts/e2e/e2e_multi_turn.py` | A 线多轮三语 E2E：同一场通话内连续切换 普通话→粤语→英语。 | 无(手动) | 是 | 2026-09-23 |
| `scripts/e2e/e2e_pipeline.py` | 对 Docker 全栈跑确定性端到端管线冒烟。 | ARCHITECTURE.md | 是 | 2026-09-07 |
| `scripts/e2e/e2e_real_customer.py` | 真实客户多轮对话 E2E：模拟真人客户拿真问题跟 AI 连续聊一整场。 | 10 脚本 import / test_probe_stimulus.py | 是 | 2026-10-04 |
| `scripts/e2e/e2e_trilingual_livekit.py` | A 线三语 E2E：真实 LiveKit 房间 + 宿主机 sidecar（ASR/TTS）+ agent。 | test_probe_stimulus.py / asr_whisper_bench.py | 是 | 2026-10-02 |
| `scripts/lib/mm_voice.py` | MiniMax 云合成 16k PCM 探针话音线(2026-09-13)。 | probe_stimulus.py import / test_cantonese_terminology.py | 是 | 2026-10-04 |
| `scripts/lib/probe_stimulus.py` | 探针/E2E 客户话音刺激源单点开关(2026-09-21)。 | 13 脚本 import / test_probe_stimulus.py | 是 | 2026-10-02 |
| `scripts/lib/urlguard_gate.py` | 脚本面出站守卫单点（Mimosa SSRF 修复；被大量脚本 import）。 | 24 脚本 import / e2e_edge_cases.py | 否 | 2026-09-23 |
| `scripts/ops/check_doc_paths.py` | 活文档仓内路径断链检查（G1a 治理，2026-10-04）。 | 无(手动) | 否 | 2026-10-04 |
| `scripts/ops/check_doc_anchors.py` | 活文档 `.py:行号` 锚计数棘轮（G3b 治理，2026-10-05）：总量只准降不准升，降基线/放行新增锚须显式 `--update`；基线 `doc_anchor_baseline.json`。 | tests/test_doc_hygiene.py | 否 | 2026-10-05 |
| `scripts/ops/check_schema_drift.py` | Supabase schema 漂移门禁:scripts/artifacts/.p0_supabase_schema.sql 产物 ≡ build_engine() 代码? | CI schema-drift.yml / test_schema_reverse_parity.py | 否 | 2026-10-04 |
| `scripts/ops/dump_postgres_ddl.py` | 把 control-plane 的库结构从"代码跑出来"固化成可应用的 SQL(P0 Supabase 引导件)。 | check_schema_drift.py import / CI schema-drift.yml | 否 | 2026-10-04 |
| `scripts/ops/import_smoke.py` | 逐脚本 import 冒烟(G1c 验收件):每个受管脚本按直接运行姿势进程内装载,搬错桶/漏改 import/头丢失现形;SKIP-DEP=精简环境缺训练栈。 | CI ci.yml / 无(手动) | 否 | 2026-10-04 |
| `scripts/ops/llm_cache_report.py` | LLM 前缀缓存命中率报告（W0 验收工具）。 | LATENCY_BUDGETS.md / DELIVERY-MANUAL.md | 否 | 2026-09-06 |
| `scripts/ops/mimosa_triage.py` | Mimosa 扫描 triage：findings.json vs 仓库审定清单 → 只打印未审定增量。 | security/mimosa/suppressions.json / SHAPES.md | 否 | 2026-09-23 |
| `scripts/ops/mm_llm_shim.py` | MiniMax chatcompletion_v2 → OpenAI /v1/chat/completions 代理（测试腿）。 | cuda-node-bringup.md | 是 | 2026-10-04 |
| `scripts/ops/node_handshake_smoke.py` | 节点握手链路冒烟（P1）：对一个已起 CP 全验 register → heartbeat → 鉴权 → 列表。 | CI node-handshake.yml / redteam_probes.py | 是 | 2026-10-04 |
| `scripts/ops/smoke_postgres.py` | 真实 Postgres 冒烟（P0 分发型拓扑回归闸门，2026-09-15）。 | check_schema_drift.py import / CI postgres-smoke.yml | 是 | 2026-10-04 |
| `scripts/ops/smoke_sidecars.py` | Qwen3-ASR + Qwen3-TTS 本地 sidecar 冒烟（A 线本地验证）。 | test_probe_stimulus.py | 是 | 2026-10-01 |
| `scripts/pipeline/eval_csc_model.py` | CSC 模型统一评测入口（位置感知判分，2026-09-27）。 | test_cantonese_terminology.py / test_csc_data.py | 否 | 2026-10-02 |
| `scripts/pipeline/eval_sensevoice.py` | SenseVoice-small ONNX 评估:粤语 CER + WA 数字 + 8k 窄带,双引擎对照。 | test_cantonese_terminology.py | 否 | 2026-10-01 |
| `scripts/pipeline/judge_group_eval.py` | JudgeGroup prompt 规则回归(W9,2026-09-24)——静态用例库+本地 4B/9B 双判官同卷。 | test_judge_group.py import / test_judge_group.py | 是 | 2026-09-26 |
| `scripts/pipeline/mlx_lm_template_leak_fix.py` | mlx_lm 生成提示边界归一补丁（幂等，BOK_TEMPLATE_LEAK_FIX=0 关闭）。 | bok.py / test_mlx_timing_patch.py | 否 | 2026-10-04 |
| `scripts/pipeline/predict_csc_model.py` | CSC 离线预测 harness（配合 eval_csc_model，2026-09-27）。 | test_cantonese_terminology.py / train_csc_model.py | 否 | 2026-10-02 |
| `scripts/pipeline/qa_bank.py` | QA 快答词库「种子包」导入/导出 CLI(2026-09-25)。 | test_qa_bank.py import / test_qa_bank.py | 是 | 2026-10-04 |
| `scripts/pipeline/qa_laya_calibrate.py` | Laya QA 车道 floor 标定（golden 集上画 coverage-accuracy，2026-09-25）。 | agent laya_judge.py | 是 | 2026-09-27 |
| `scripts/pipeline/qa_match_report.py` | QA 匹配器离线标定 harness（golden 集驱动,零网络零真库零 LLM）。 | agent qa_gate.py / qa_golden_set.json | 否 | 2026-09-27 |
| `scripts/pipeline/r2_threshold_refit.py` | V-5 澄清闸阈值 leave-out 重拟合（离线，纯 stdlib）。 | security/mimosa/suppressions.json | 否 | 2026-10-04 |
| `scripts/pipeline/snippet_seed_mining.py` | E1 snippet 轨首批词表候选挖掘（2026-09-21，vocab-skill 范式）。 | test_snippet_mining_helpers.py / probe_mt_lang_validator.py | 否 | 2026-10-04 |
| `scripts/pipeline/train_csc_model.py` | CSC 自训训练入口（char-level MacBERT4CSC 微调，2026-09-27）。 | predict_csc_model.py | 否 | 2026-10-02 |
| `scripts/probes/probe_8khz_asr.py` | 8kHz 窄带重验探针（spec 2026-09-13 §6 前置门；P1.5 Task 5）。 | test_probe_8khz_asr.py / RUNTIME_TOPOLOGY.md | 是 | 2026-09-15 |
| `scripts/probes/probe_ambient_keyboard.py` | W2b 思考态键盘环境音探针(2026-09-24)。 | 无(手动) | 是 | 2026-09-26 |
| `scripts/probes/probe_asr_digits_ab.py` | bf16 vs 8bit 数字回归检查（2026-09-17 ASR 模型档位 A/B 辅助）。 | probe_cloud_asr_ab.py import / probe_cloud_asr_ab.py | 是 | 2026-09-17 |
| `scripts/probes/probe_branch_action.py` | 分支动作（A-②）+ 分支罐头快路（A-①）实弹验收探针（2026-09-20 路线 A）。 | test_branch_action_probe.py / security/mimosa/suppressions.json | 是 | 2026-10-04 |
| `scripts/probes/probe_brand_words.py` | 品牌词轮次完整性探针（S3 拼多多漏字专项）。 | 2 脚本 import / test_probe_stimulus.py | 是 | 2026-10-04 |
| `scripts/probes/probe_cache_discipline.py` | 缓存纪律探针（2026-09-21 讨论稿 §46 配套）：只打本机诊断端点，核对每轮 LLM 请求的 cached 占比与尾部字节。 | LINUX_NODE_TEST_RUNBOOK.md / AGENTS.md | 是 | 2026-09-22 |
| `scripts/probes/probe_campaign_schedule.py` | 战役调度循环集成探针（2026-09-17 campaign-scheduling-dashboard Task 3）。 | 无(手动) | 是 | 2026-09-18 |
| `scripts/probes/probe_cantonese_digits.py` | TTS 粤语数字读音探针：合成并(可选)ASR 回读,定位 0-9 / 尾号读法错误。 | 无(手动) | 是 | 2026-09-05 |
| `scripts/probes/probe_cloud_asr.py` | 云 ASR 直连探针（A线优化总计划 · 批次0.6b；W2c 加 de/fr/ja/pt 四语折叠判定+数字序列分，三语口径不动）。 | agent doubao_asr.py / test_doubao_asr.py | 是 | 2026-10-06 |
| `scripts/probes/probe_cloud_asr_ab.py` | 云端 Qwen3-ASR-Flash（阿里云百炼 DashScope）vs 本地 sidecar 同音频 A/B 探针。 | test_cantonese_terminology.py | 是 | 2026-09-17 |
| `scripts/probes/probe_ctx_decode.py` | 上下文长度→decode 吞吐成本微基准(2026-09-24):prompt 1.7k vs 3.2k 的 decode tps。 | 无(手动) | 是 | 2026-09-26 |
| `scripts/probes/probe_cuda_baseline.py` | CUDA 原型延迟基线探针（spec §9 门禁）：在 CUDA 节点跑与 Mac 侧同口径的 TTFT/ASR 采样出 JSON 基线（Mac 上 --dry-run 只校验参数）。 | SHAPES.md | 是 | 2026-10-04 |
| `scripts/probes/probe_deepseek_thinking.py` | DeepSeek 官方「思考开关」实测（2026-09-21）。 | 无(手动) | 是 | 2026-09-22 |
| `scripts/probes/probe_doubao_utterances.py` | 豆包 SAUC definite utterance 时序探针（2026-10-08 协议定案取证）：say 合成普通话五臂（0.4s/0.8s 停顿/连续/长会话>30s/尾部静音喂入），200ms 实时分包喂入不提前发末包，逐帧落 utterances（text/definite/时间戳）原样 JSONL，定案「说话中云侧是否增量下发 definite / 静音多久自发 end_window」。 | probe_cloud_asr.py import（协议同源）/ agent doubao_asr.py | 是 | 2026-10-08 |
| `scripts/probes/probe_fast_speech.py` | 快语速吃字/回声守卫探针（2026-09-12 Task 2 验收）。 | 3 脚本 import / test_probe_stimulus_tools.py | 是 | 2026-10-01 |
| `scripts/probes/probe_filler_timing.py` | 垫话时序探针:用户讲完一句后,agent 出声(垫话或回复)必须 <2s。 | LATENCY_BUDGETS.md / AGENTS.md | 是 | 2026-09-23 |
| `scripts/probes/probe_flow_20rounds.py` | A 线 20 轮粤语全流程实弹探针（FLOW20 验收电池）。 | test_settle_state_assertion.py / test_ab_slot_actor.py | 是 | 2026-09-30 |
| `scripts/probes/probe_flow_graph.py` | 话术图（flow graph）实弹验收（spec 2026-09-18-qa-flow-graph.md §8）。 | 4 脚本 import / test_flow_graph_probe_intent_judge.py | 是 | 2026-10-02 |
| `scripts/probes/probe_gpu_contention.py` | GPU 争用探针：谁在抢 4B 的 prefill（2026-09-22，TTFT 归因用）。 | test_judge_idle_yield.py | 是 | 2026-09-22 |
| `scripts/probes/probe_hotword_ab.py` | ASR 热词 context 扩容 A/B 探针（粤语异议域词碎裂专项）。 | 2 脚本 import / test_probe_stimulus.py | 是 | 2026-10-02 |
| `scripts/probes/probe_intent_mine.py` | 意图候选挖掘 dry-run 探针（执行计划 §48 P2.1，纯只读 + 本地 LLM）。 | test_probe_intent_mine.py import / flow_graph.py | 是 | 2026-09-22 |
| `scripts/probes/probe_interp_backlog.py` | B 线播放背压实弹探针（P2 `_PlaybackBacklog`，2026-09-16）。 | test_probe_backlog_count.py import / test_probe_backlog_count.py | 是 | 2026-10-02 |
| `scripts/probes/probe_interp_continuous.py` | B 线「边说边译」连续语流探针（2026-09-17 长度触发子句提交验收）。 | test_probe_stimulus.py / security/mimosa/suppressions.json | 是 | 2026-09-21 |
| `scripts/probes/probe_interp_duplex.py` | B 线全双工争用探针（2026-09-16）——两向同时说话，量双向延迟与零丢句。 | 无(手动) | 是 | 2026-09-19 |
| `scripts/probes/probe_mt_matrix.py` | B 线 MT 车道选型矩阵探针（2026-10-08 W1）：deepseek官方/qwen-mt-flash(translation_options.terms)/方舟v4.1-flash/opencode zen 四臂逐 delta 计时+译文质量+术语落地;晚峰复测定车道。 | test_scripts_index.py | 是 | 2026-10-08 |
| `scripts/probes/probe_deepseek_stream.py` | DeepSeek 流式真伪归因探针（2026-10-08）：chat 流式/Responses API/非流式三形状逐 delta 计时+TTFB+缓存臂;Responses 思考开关=reasoning.effort "none"。 | test_scripts_index.py | 是 | 2026-10-08 |
| `scripts/probes/probe_interp_spec_live.py` | B 线投机翻译实弹探针（2026-10-08 spec 复活验收）：多子句刺激（TTS 逗号停顿挤压到 VAD 静音线内=真人形状）→fire/CLAUSE_COMMIT/defer 日志判据。 | test_scripts_index.py | 是 | 2026-10-08 |
| `scripts/probes/probe_interp_late_mic.py` | B 线差分探针:延迟麦克风发布 vs 即时发布(call-72112fd7 复现器)。 | 无(手动) | 是 | 2026-09-30 |
| `scripts/probes/probe_interp_pause_commit.py` | B 线 vad-pause 提交字数门槛 A/B 探针(2026-10-06 `_pause_commit_min_chars` 评估)——子句+0.7s 真停顿刺激,量译文首声对停顿起点 lag。 | 无(手动) | 是 | 2026-10-06 |
| `scripts/probes/probe_interpret_latency.py` | B 线同传延迟探针（P1 评测体系,2026-09-16）——逐句感知 lag,版本回归用。 | 2 脚本 import / agent interpret.py | 是 | 2026-09-19 |
| `scripts/probes/probe_judge_parity.py` | 意图识别（judge）的**三方对照**（2026-09-21）：本机 9B vs DeepSeek flash vs v4-pro。 | 无(手动) | 是 | 2026-09-22 |
| `scripts/probes/probe_killswitch.py` | 远程停机开关（killswitch）目标语义探针：把「吊销节点=即刻断供云端」的目标契约钉成可跑的红绿基线。 | CI node-handshake.yml / node_agent.py | 是 | 2026-10-04 |
| `scripts/probes/probe_speaker_lock.py` | W5 声纹锁真实语音阈值探针（2026-10-07 接线波）：四把 macOS 嗓音×office 底噪三档 SNR 四臂，钉 0.78/0.65 双阈的「零误杀+纯环境音全判丢」——v1 不分人（C 臂 informational）。 | speaker_lock.py | 是 | 2026-10-07 |
| `scripts/probes/probe_latency_soak.py` | 多轮多样话术延迟测试台（2026-09-17）：真实客户多轮×每轮不同措辞，量「讲到出声」。 | 4 脚本 import / test_latency_soak_report.py | 是 | 2026-10-03 |
| `scripts/probes/probe_llm_cache.py` | LLM 缓存命中探针：一通电话内连续多轮真实对话，逐消息指纹定位 cached 分叉（TTFT 回归调查工具）。 | DELIVERY-MANUAL.md | 是 | 2026-09-23 |
| `scripts/probes/probe_llm_contention.py` | LLM 同卡争用微基准(2026-09-24):4B prefill 单飞 vs 9B 并行时的往返膨胀。 | 无(手动) | 是 | 2026-09-26 |
| `scripts/probes/probe_llm_draft_ab.py` | spec decode 隔离 A/B 探针（2026-09-27，llm_draft 实弹验收）——纯客户端，零进程管理。 | bok.py | 是 | 2026-09-27 |
| `scripts/probes/probe_llm_stall.py` | 慢速注入诊断探针（2026-09-29 v2 P0.2，纯诊断）。 | 无(手动) | 是 | 2026-09-30 |
| `scripts/probes/probe_storm_expiry.py` | 风暴到期自清 timer 实弹探针(2026-10-07 死气窗票验收件):连发打断→engage→静默 15s→三关(expiry 打点/回收线出声/尾问真答)——第一版抓出自毁守卫。 | agent storm 路径 | 是 | 2026-10-07 |
| `scripts/probes/probe_mt_glossary_ab.py` | B 线术语表 A/B/C 三臂实证探针(E5 定案用)。 | 无(手动) | 是 | 2026-09-21 |
| `scripts/probes/probe_offscript_soak.py` | 话术外问题集锦测试台（2026-09-17）：客户不讲「剧本里的话」时 A 线怎么接。 | 2 脚本 import / test_offscript_report.py | 是 | 2026-10-02 |
| `scripts/probes/probe_offtopic_recovery.py` | 跑题拉回探针（2026-09-25）：客户中途问流程完全无关的问题，A 线会不会被带飞。 | 无(手动) | 是 | 2026-10-03 |
| `scripts/probes/probe_polish_model.py` | E7 离线润色面「生成模型待定」A/B 探针（2026-09-21，plan §26.2-E7）。 | polish.py | 是 | 2026-10-04 |
| `scripts/probes/probe_preemptive.py` | 抢跑（preemptive generation）诊断探针——推长句喂饱 speculative 链。 | RUNTIME_TOPOLOGY.md | 是 | 2026-09-23 |
| `scripts/probes/probe_qa_hit.py` | QA 快路词库命中率探针:真实转写挖出的问法 vs 现库,报 would-hit 率与 top 未命中。 | 3 脚本 import / test_probe_qa_rotation.py | 是 | 2026-10-04 |
| `scripts/probes/probe_reply_latency.py` | 真实通话延迟探针：**遮羞布盖住了多少**（2026-09-21）。 | 无(手动) | 是 | 2026-10-03 |
| `scripts/probes/probe_reply_parity.py` | 回复质量的**三方对照**回放（2026-09-21）：本机 4B vs DeepSeek flash vs v4-pro。 | 无(手动) | 是 | 2026-09-22 |
| `scripts/probes/probe_reply_quality.py` | 回复质量离线回放探针（2026-09-12 P0「会说话」验收门）。 | 无(手动) | 是 | 2026-09-23 |
| `scripts/probes/probe_s2s_vs_cascade.py` | S2S 流控引擎 vs 级联引擎「同稿双跑」对照探针（2026-10-04，feat/s2s-spike）。 | branch_syntax.py | 是 | 2026-10-04 |
| `scripts/probes/probe_session_lifecycle.py` | 会话生命周期审计探针（2026-09-29 v2 P0.1）。 | 无(手动) | 是 | 2026-09-30 |
| `scripts/probes/probe_settle_parity.py` | 沉淀/纪要质量的**三方对照**（2026-09-21）：本机 9B vs DeepSeek flash vs v4-pro。 | 无(手动) | 是 | 2026-09-22 |
| `scripts/probes/probe_smart_turn.py` | smart-turn v3.2 (CPU int8 ONNX, 8.7MB) 三语 finished/unfinished 探针。 | test_cantonese_terminology.py | 是 | 2026-09-15 |
| `scripts/probes/probe_thin_client_static.py` | 瘦客户端静态探针：钉死「节点托管 UI 的 CP 地址必须在运行时可注入」。 | CI ci.yml / RUNTIME_TOPOLOGY.md | 否 | 2026-10-04 |
| `scripts/probes/probe_vad_head_syllable.py` | VAD START 前导帧并入会话缓冲 → 号码句头段多解一音（复现探针）。 | test_probe_stimulus.py / e2e_campaign.py | 是 | 2026-09-23 |
| `scripts/probes/probe_voice_style_gate.py` | A 线语气管线装配决策离线复现（2026-10-06 云档哑火查因）——resolved model/门 verdict/NATURALNESS_BLOCK 注入+turns 覆盖面;`--live N` 实弹测标记率。 | 无(手动) / test_scripts_index.py | 是 | 2026-10-06 |
| `scripts/probes/probe_windows_lifecycle.py` | Windows 无头部署生命周期探针（site-delivery Task 2）:down 停止语义 + schtasks 契约。 | schtasks_units.py / bok.py | 是 | 2026-09-17 |
| `scripts/runtime/mine_qa.py` | 高频问答对挖掘（bok.py tts-mine 执行体；--cluster 走 LLM 同义聚类，与 CP qa_cluster 同源）。 | ★ CP qa_cluster.py 同源 / bok.py tts-mine | 是 | 2026-10-04 |
| `scripts/runtime/mock_callee.py` | mock SIP 客户(模拟联调档):CP 派生的真语音被叫。 | ★ CP main.py exec / bok.py（进程识别 marker） | 是 | 2026-09-21 |
| `scripts/runtime/pregen_tts.py` | 离线批量预合成 TTS 本地缓存(bok.py tts-pregen 的执行体,2026-09-08;task-14b 按人设物化)。 | ★ CP pregen.py exec / bok.py tts-pregen | 是 | 2026-10-02 |
| `scripts/seed/build_asr_variants.py` | 离线构建 ASR 音近变体词表资产(2026-09-27)。 | asr_polish.py | 否 | 2026-10-04 |
| `scripts/seed/merge_asr_variants_extra.py` | curated 变体幂等并进吸附资产(2026-10-08 B 线 P0;builder 重建后须重跑回填,extra=asr_variants_extra_bline.json)。 | asr_polish.py / asr_variants.json | 否 | 2026-10-08 |
| `scripts/seed/export_ecapa_onnx.py` | v2 认人票：官方 speechbrain ECAPA ckpt 自导出单文件 ONNX（临时 venv 跑，torch 懒导入非生产依赖；parity 断言+sha256 存档）。 | speaker_lock.py ECAPA 档 | 否 | 2026-10-07 |
| `scripts/seed/cache_minimax_auditions.py` | MiniMax 官方试听缓存（2026-10-02，音色目录换血配套）。 | minimax-voices.ts | 是 | 2026-10-03 |
| `scripts/seed/gen_filler_assets.py` | 垫话音频资产生成器(2026-09-10 拍板,spec 讨论见会话)。 | agent fillers.py / test_speed_unification.py | 是 | 2026-10-04 |
| `scripts/seed/gen_ambience.py` | W6 场景底噪资产生成器(2026-10-06):三场景无缝循环 wav+manifest,种子化确定性合成。 | agent ambience.py / test_ambience.py | 是 | 2026-10-06 |
| `scripts/seed/gen_route_gates.py` | 生成 security/route-gates.json：枚举 CP 全部 FastAPI 路由 + 源码闸标记提取。 | test_route_gate_coverage.py | 否 | 2026-09-23 |
| `scripts/seed/import_xkt_qa.py` | 惜客通 tbl_ai_knowledge.json → Bok qa_entries 导入器(spec §6,2026-09-17)。 | test_import_xkt_qa.py import / test_import_xkt_qa.py | 是 | 2026-10-04 |
| `scripts/seed/load_compliant_templates.py` | 装载三语合规话术（data/templates/hegui-*.json → CP /api/templates）。 | test_compliant_templates.py | 是 | 2026-10-03 |
| `scripts/seed/load_intent_catalog.py` | 意图目录加载器(W1a,2026-09-23):把 scripts/data/intent_catalog_v1.json 灌进模板 graph_json。 | agent intent_semantic.py | 是 | 2026-10-04 |
| `scripts/seed/migrate_templates_8step_0913.py` | 三语模板 6 步 → 8 步迁移(2026-09-13 甲.1 拆步终稿)。 | test_slot_rendering.py import / test_slot_rendering.py | 否 | 2026-10-04 |
| `scripts/seed/pad_test_audio.py` | 给测试 WAV 前后补静音（Silero VAD 用）。 | 无(手动) | 否 | 2026-09-05 |
| `scripts/seed/prep_tts_dataset.py` | 原始客服录音 → Qwen3-TTS SFT 数据集流水线（docs/TTS-SFT-DATA-PREP.md 阶段 C 数据先行）。 | test_prep_tts_dataset.py import / test_prep_tts_dataset.py | 否 | 2026-10-04 |
| `scripts/seed/prepare_csc_data.py` | 粤语/普通话 CSC（拼写纠错）自训数据挖掘管道（2026-09-27）。 | test_probe_stimulus_tools.py import / CP hotword_mining.py | 否 | 2026-10-04 |
| `scripts/seed/render_asr_corpus_v2.py` | ASR 评测语料 v2：粤语条目重渲（好音频版，2026-10-03）。 | AGENTS.md | 否 | 2026-10-03 |
| `scripts/seed/render_asr_corpus_4lang.py` | 四语（de/fr/ja/pt）豆包 ASR 实测语料渲染（W2c，2026-10-06）：MiniMax 云 TTS×客服六句型/语→16k wav+manifest（reports/asr-4lang-corpus，gitignored），供 probe_cloud_asr --corpus 放行/限缩判定。 | probe_cloud_asr.py / cache_minimax_auditions.py（护栏+目录单源 import） | 是 | 2026-10-06 |
| `scripts/seed/seed_invite_templates.py` | 三语「客服邀约」话术种子（2026-09-24 精炼版）。 | test_branch_syntax_parity.py / prepare_csc_data.py | 是 | 2026-09-26 |
