# Bok Voice 仓库地图（REPO MAP）

> 目录 → 用途 → 归属（dev / packaged / both）→ 关键文件。与 `RUNTIME_TOPOLOGY.md`（端口/数据流）搭配读。
> 本文随结构/端口变化更新；删文件先查本图引用。

## 根目录

| 路径 | 用途 | 归属 |
|---|---|---|
| `tools/bok.py` | 编排/模型/健康/自检唯一入口（serve/status/down/doctor/prod/download） | both |
| `tools/schtasks_units.py` | Windows Task Scheduler 常驻单元生成（XML/argv 纯函数单点 + run_schtasks/防火墙副作用出口；`bok.py prod` 与 `tests/test_prod_windows.py` 消费） | both |
| `scripts/` | CI 构建、E2E、延迟测量、smoke、sidecar 启动（见下「脚本」） | dev/CI |
| `apps/agent/` | LiveKit agent 运行时（A 线客服 + B 线同传 worker） | both |
| `apps/control-plane/` | FastAPI 业务服务 :8000（对象/人设/知识/话术/通话/审计/token/webhook/名册/外呼战役） | both |
| `apps/web/` | Next.js 静态导出（纯浏览器 UI，节点 :3000 / 云 CP 托管：calls/interpret/supervisor/objects/personas/settings/roster/campaigns/nodes（root 停机开关）/qa（快答库：列表+画布双视图，画布新件见下「apps/web」节）/**studio（AI 工作站 W1：`/studio/?t=<id>` 五 tab=话术流程/意图与问答/罐头录音/通话日志/学习报告，编辑器与 /templates 共用 `components/template-editor.tsx`；主管台含捕获打铃 v0）**…；统一链路 logger：`lib/logger.ts`（traceId/环形缓存/脱敏/全局兜底，error 自动上报 CP `/api/web_logs`，装配点 `lib/log-bootstrap.ts` + `session-context`）；`test/*.test.mjs` node --test 单测，CI web job `npm test`） | both |
| `packages/core/` | 领域模型 + 策略（`bok_voice_core`：policies/types） | both |
| `packages/business-db/` | SQLAlchemy 仓库（`bok_voice_business_db`：global_settings 默认等） | both |
| `packages/knowledge/` | 知识服务 / Markdown / 向量（沉淀知识库） | both |
| `packages/observability/` | 结构化日志 + 审计（`bok_voice_obs`） | both |
| `services/qwen3-asr-sidecar/` | ASR HTTP sidecar :8787（`app.py`：start/chunk/finish、partial 滑窗 + 增量 finish） | both |
| `services/qwen3-tts-sidecar/` | TTS HTTP sidecar :8788（`app.py`：克隆音色/流式；本地回退档） | both |
| `services/llm-mlx/` | 仅 `.venv`（mlx_lm 0.31.3）；server 启动命令在 bok.py，无仓库代码 | dev |
| `services/realtime-translation/` | B 线 v1 同传 :8790（Node，冻结留 POC） | both(旧) |
| `services/livekit-server/` | `livekit.yaml`（self-host 配置，钉端口/prometheus/json 日志） | both |
| `desktop/` | Tauri 桌壳 + runtime 装配（src-tauri Rust / runtime symlink；前端经 `apps/web/lib/tauri.ts` 直连 invoke，`desktop/src/` 前端桥已删） | packaged |
| `tests/` | pytest 全量（含 `fixtures/audio/{zh,cantonese,en}.wav` E2E 音频 + 术语门禁 + `test_prod_windows.py` Windows prod 生命周期/安装器单测 + qa-canvas Phase 1 四件：`test_qa_cluster_field.py` 簇列数据层/级联、`test_pregen_qa_status.py` pregen --qa-status、`test_qa_canned_status.py` CP 罐头状态/试听/补料端点、`test_import_xkt_qa.py` 惜客通导入器纯函数） | dev/CI |
| `docs/` | RUNTIME_TOPOLOGY / REPO_MAP / CONTRACTS / DEV_TOOLS / archive 决策归档 | dev |
| `dev/docker/` | 可选 Docker 开发栈（归档，不进 CI） | dev-only |

## apps/agent（agent_runtime）

| 文件 | 职责 |
|---|---|
| `main.py` | worker 入口（A 线 `bok-voice`） |
| `agent.py` | A 线装配：语言钉定/ASR/LLM/抢跑/打断/话术推进 hook/心跳/收尾 |
| `interpret.py` | B 线同传 worker（fwd/rev，Hy-MT2 :1236 + MiniMax 三语音色） |
| `flow.py` | 话术分步推进引擎（FlowController/rule_verdict/should_auto_advance） |
| `dialer.py` | SIP 外播薄层（`dial_outbound` 四态出口：real=官方 CreateSIPParticipant / mock=CP 派生真语音被叫；`resolve_dial_mode` env→settings→mock） |
| `control_plane.py` | CP HTTP 客户端 |
| `web_search.py` | 联网检索（默认关） |
| `providers/livekit_plugins.py` | 本地模型插件：LanguageState/PinnedLanguageState/ContextState(前缀/尾/对象档案)/MlxLlmLLM/DeepSeekLLM/StatelessMTLLM/Qwen3ASR(STT/流式句级)/Qwen3TTS/MiniMaxTTS(classic 池+bidi 实验)/VolcanoTTS |
| `providers/registry.py` `fakes.py` `volc_v3_protocol.py` | 插件注册 / 测试 fake / Volcano 协议 |
| `plugins/` | 上下文/情绪/知识/结算子模块（context/emotion/knowledge/settlement） |

## 脚本（scripts/）

- 构建：`bootstrap.sh` `test.sh` `build_livekit.sh` `build_runtime.sh` `build_release.sh` `verify_bundle.sh`（--staging/--app/--doctor）`stub_external_bin.sh`
- E2E：`e2e_trilingual_livekit.py`（三语，真 /api/token，一案一通话）`e2e_flow_scenario.py` `e2e_multi_turn.py` `e2e_http.py` `e2e_pipeline.py`
- 测量/探针：`measure_latency.py`（需真栈）`measure_prompt.py`（本地）`probe_cantonese_digits.py` `smoke_sidecars.py` `pad_test_audio.py` `test_deepseek.py` `test_volcano_v3.py`
- 站点交付探针（2026-09-16）：`probe_killswitch.py`（kill-switch 目标语义：吊销 sticky→通话面 403 窒息→unrevoke 复活全链，CI `node-handshake.yml` linux 对真 CP 实跑）`probe_windows_lifecycle.py`（down 树杀语义 A 段全平台 + schtasks 契约 B 段仅 Windows 实跑、runner 无提权按 access-denied 优雅 skip，CI windows job）`probe_thin_client_static.py`（静态导出注入链形状 + 无烤死 localhost，CI web job，需先 `apps/web && npm run build`）
- TTS 缓存/快答库：`pregen_tts.py`（`bok.py tts-pregen` 执行体：--greetings/--objects/--fillers/--qa 离线预合成（--qa-status 出罐头物化状态 JSON），写 app-data/tts-cache；也被 CP 人设保存点自动触发，见 `apps/control-plane/control_plane/pregen.py`）`mine_qa.py`（`bok.py tts-mine` 执行体：高频问答对报告 + --apply 入库 / --sync 自动学习闭环：挖掘→质量闸→入库→按语言物化）`import_xkt_qa.py`（惜客通 `tbl_ai_knowledge.json` → qa_entries 导入器：Question 按 `&` 拆主条目+变体（`cluster_head_id` 指针）、lang 启发式、默认 dry-run、`--apply` 经 POST /api/qa-entries 落地；AfterAnswer* 等外部字段不搬、dry-run 报告列示）
- 真实客户多轮 E2E：`e2e_real_customer.py`（三语三音色多轮真问题连聊，模板绑定走对象 template_id）
- 延迟测试台：`probe_latency_soak.py`（多轮多样话术逐轮「推完→首声」+PERCEIVED 三段对照+拆轮/打断风暴/哑轮哨兵+p50/p95+JSON 报告 `reports/latency-soak/`；配套政策表 `docs/LATENCY_BUDGETS.md`）
- 话术外问题集锦：`probe_offscript_soak.py`（防诈/转人工/追问/推搪/普通话混合 5 套×10 轮实录对照+质量旗，`reports/offscript-soak/`）；热词 A/B：`probe_hotword_ab.py`（ASR sidecar 直打 none/current/extended 三档量词表收益与 prefill 成本）
- 外呼战役 E2E：`e2e_campaign.py`（mock 档全链路：3 对象战役串行自动下一通 + 终态三态 + captured 入名册；C4 号码容差=「含脚本号码的 ≥7 位**连续子串**」——live 链路号码句**头段**会被 ASR 多解一个音，定责与证据见脚本内注释）
- 8kHz 窄带重验：`probe_8khz_asr.py`（宽/窄对照 + 号码逐位 + 窄带档真伪核验，真栈探针；前置门见 spec §6.1）
- mock SIP 被叫：`mock_callee.py`（CP 派生的真语音被叫子进程：answer/no_answer/reject/hangup_mid 四剧本；台词/句间隔由 dial 块下发，会等 AI 讲完再出声）
- 并发/边界：`e2e_barge_in.py` `e2e_edge_cases.py` `e2e_interpret.py` `load_cp_concurrency.py` `load_audio_concurrency.py` `probe_filler_timing.py`
- 云端部署三件套：`deploy/cloud/`（docker-compose.yml + .env.example + 宝塔 runbook README——云 CP 单容器接 Supabase，vault 落命名卷；CI `compose-rehearsal.yml` 每次改动真跑 compose 排练：config 校验→本地构建→up→/health+静态站+root 登录断言）
- Supabase 漂移门禁：`check_schema_drift.py`（产物应用→build_engine→双 dump+代码基线四向比对，死对象方向也逮；CI `schema-drift.yml`）+ 节点握手 smoke `node_handshake_smoke.py`（license 流 9 步）+ kill-switch 探针（CI `node-handshake.yml`：Linux 真 CP 实跑握手+停机开关全链 / Windows ps1 干跑 + 生命周期探针）
- 话务员机虚拟声卡：`setup-virtual-audio.sh|ps1`（B 线同传路由；mac=BlackHole GPL/win=VB-CABLE donationware **下载即装不随包分发**——再分发限制；doctor 有检测行；指南 `docs/OPERATOR_AUDIO_SETUP.md`）
- 节点安装/打包：`install-node.sh|ps1`（步骤计划器 fail-fast，`--node-token|--license-key` 双流，dry-run 零副作用；ps1 `-Fetch` 冷装机自举（CP 拉包）+ `-InstallService` 注册常驻（token/license 双流透传）；`bootstrap-node.sh`=bash 自举入口）+ `build_node_pkg.sh`/`build_runtime_pkg.sh`（发版工件：代码包+运行时包，`deploy/cloud/publish_node_pkg.sh` 推云 CP）+ `build_node_agent.sh`/`node_agent.spec`（PyInstaller onefile node-agent 二进制，`docs/NODE_PACKAGING.md`）
- 电话边缘站点部署：`deploy_sip_edge.sh`（Ubuntu 22.04+ VPS，root/sudo：apt 依赖 + livekit-sip 原生编译装 `/usr/local/bin/livekit-sip` + `/etc/bok/livekit-sip.yaml` + systemd `bok-livekit-sip.service`（Redis 依赖按 `--redis-url` 分支：本机档 `Requires=`、远端档 `Wants=`）；幂等，`--force` 重编；周期=脚本部署→CP 建站→面板注册 trunk→战役挂 site_id，见 RUNTIME_TOPOLOGY「电话边缘站点」）
- 平台：`setup-windows.ps1`

## 关键入口

| 场景 | 入口 |
|---|---|
| 本机开发拉起全栈 | `python tools/bok.py serve`（无 Docker） |
| 节点发版打包 | `scripts/build_node_pkg.sh <版本>` + `build_runtime_pkg.sh <版本>`（CI release.yml） |
| 模型首启下载 | `python tools/bok.py setup download` |
| 节点自检 | `bok.py doctor`（安装树：`BOK_ROOT` 指安装根） |
| 生产常驻（launchd） | `bok.py prod install` / `prod status` |
| A 线通话 | 前端 /calls → LiveKit :7880 → agent worker（每通语言固定） |
| B 线同传 v2 | 前端 /interpret → LiveKit :7880 → interp worker ×2（:1236 MT + MiniMax） |
| B 线同传 v1(冻结) | 前端 /translate → ws://127.0.0.1:8790 |

## apps/control-plane（control_plane）

| 文件 | 职责 |
|---|---|
| `main.py` | 全部 API 端点 + 启动装配（含 `/api/roster*`、`/api/campaigns*`、`/api/objects/{id}/dial-now`（单发外呼）、`/api/sip/sites`（GET 列表 / **POST 建站，同 account+name 幂等**）、`/api/sip/sites/{id}/trunk`（注册 outbound trunk）、`/api/sip/mock/callee`） |
| `campaign.py` | 外呼战役串行循环（5s 巡检：终态收割 / 串行起下一通 / 名单尽判 done；gap 冷却；dispatcher 可注入） |
| `deps.py` | 引擎装配 + 幂等 DB 迁移唯一入口（新建列/数据迁移都在 `build_engine()`） |
| `schemas.py` | 请求/响应模型（含 `SipSettingsModel`） |
| `pregen.py` | 人设保存点自动物化（detached 子进程跑 pregen_tts.py）+ QA 罐头状态面（`qa_canned_status`/`cache_root`，供 `GET /api/qa/canned-status`·`GET /api/qa/{id}/canned-audio`·`POST /api/qa/pregen`） |

## apps/web（快答库画布 Phase 1 新件）

| 文件 | 职责 |
|---|---|
| `components/qa-canvas-view.tsx` | React Flow 画布（步骤脊柱+QA 卫星簇渲染、拖线挂簇/挂步与断边、乐观回滚、右键菜单回调、罐头徽标✓/缺料；只读条目双向闸不出柄） |
| `lib/qa-canvas.ts` | 画布纯函数唯一数据面（parseTemplateSteps/deriveGraph 布局契约、resolveClusterTarget 簇校验、revertCluster、localStorage 位置键；签名勿动，`test/qa-canvas.test.mjs` 钉住） |
| `components/template-editor.tsx` | 话术模板编辑表单（2026-09-19 W1 自 /templates 原样提取：分步 goal/ref/直念/情绪/TSV 导入/热词/保存逻辑；/templates 列表页与 /studio 工作台「话术流程」tab 双页共用，提取前后渲染输出一致） |
| `app/(app)/studio/page.tsx` | AI 工作站（列表态+`?t=<id>` 工作台态五 tab；静态导出零动态段，深链 query 参数形态） |

## 数据表（packages/business-db，新表须方言可移植）

| 表 | 用途 |
|---|---|
| `call_sessions` / `turns` | 通话主记录 / 逐轮分析账本（speaker/gen/template_step/perceived_ms） |
| `roster_entries` | 名册认领池（captured 号码自动入册；unclaimed→claimed→handled） |
| `campaigns` | 外呼战役（status draft/running/paused/done/stopped、site_id 电话边缘站点、gap_seconds、scripts_json mock 台词钩子） |
| `campaign_items` | 战役名单项（seq/phone/status/call_id/scenario，无电话对象直接 skipped） |
| `sip_sites` | 电话边缘站点（livekit_url/sip_edge none·local·cloud/trunk_id/numbers_json；`site-local` 是 repo 合成的虚拟默认站点，不入库） |

## 运行时装配（packaged）

```text
runtime/python/                独立 CPython（依赖 requirements-runtime-<平台>.txt；2026-09-17 迁出 desktop/）
runtime/llama/                 Windows llama-server.exe + cudart DLL
runtime/bline-node_modules/    B-line worker 依赖
```

## 已清理的遗留

- Ollama：已从编排/默认配置/B 线翻译移除（docs/archive 留存说明）
- Docker：开发可选栈归档 `dev/docker/`，CI 不构建
- CosyVoice：无运行时引用
- `.superpowers/`：subagent 工作台（gitignored，实测档案留档）不入 gh 推送
