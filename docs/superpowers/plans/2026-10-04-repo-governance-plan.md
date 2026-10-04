# 仓库治理计划（REPO-GOVERNANCE）— 目录 / 契约 / 模块 / 索引

> 2026-10-04 · 分支 `feat/s2s-spike` · **只读取证产出，本文件是计划不是执行记录；成文时未改任何源码、未 commit。**
> 探针口径：全部数字来自当前工作区（含在途未提交清理，见 §1.4），命令可复现（附录 C）。
> 用户定调（原话）：「一堆屎山」，各板块应在自己位置；scripts/ 乱；bok.py 承载过多能力全耦合；
> 要目录管理 / 契约管理 / 模块管理 / 可维护管理 / map 体系 / 索引——**出问题第一时间知道去哪找，不在垃圾堆里翻**。

---

## 0. 一屏结论（抱怨 → 量化事实 → 提案 → 验收令）

| 抱怨 | 量化事实（本仓当前） | 提案 | 验收令 |
|---|---|---|---|
| scripts/ 乱 | **138 文件 / 43,835 行** 平铺一个目录；56 个 `probe_*` 与 build/e2e/seed/运维混放；10 个隐藏产物 JSON 丢在目录根（在途已迁 `artifacts/`）；`probe_interp×4 / probe_llm×4 / probe_minimax×3` 同目变体簇 | G1：按用途分 7 桶（probes 48/e2e 10/bench 15/seed 13/pipeline 11/ops 23/runtime 3）+ `lib/` 共享层 2 + `archive/` 13 | `scripts/README.md` 索引 138/138 覆盖；新增 `test_scripts_index` 绿；全量 pytest 绿 |
| scripts/ 搬家会踩雷 | **77/138 被外部引用；68 个文件需改；脚本间 import 边 56 条、44 个 importer**（`urlguard_gate` 被 22 个脚本 import）；3 个脚本是**产品运行时 exec**（CP 调 `pregen_tts.py`/`mock_callee.py`） | G1 迁移机械：`git mv` + 引用重写（代码/CI/测试）+ 历史档不改 + 4 行 import 引导头 + `check_doc_paths` CI | 见 §4 G1 验收令 |
| bok.py 全耦合 | **4,774 行 / 246KB / 138 个顶层 def|class / 14 子命令**；`_FORWARD_ENV` 单表 392 行 250 键；`_control_plane_env` 189 行；端口字面量散落（`:1235`×96、`:1237`×53、`:1236`×43…）；**182 个文件引用 bok.py 路径；28 个测试读它的源码文本；212 处 `setattr(bok, "X")` 打在它的命名空间上（19 个测试文件）** | G2：`tools/bokctl/` 包 + `tools/bok.py` 只留 15 行 launcher；**先建测试 patch 夹具，再搬域模块**；`_FORWARD_ENV`/`_control_plane_env`/端口表保持单源 | 每步全量 pytest 绿 + 新 `test_bok_module_contract`（单源表全仓唯一）+ `bok status` 冒烟 |
| map 找不到/找到了是错的 | `ORCHESTRATION-MAP.md` 写于 10-02（基线 78e17e1），**写完当天锚点全对**；其后 194 commits（27 次触 agent.py），agent.py 8014→8913 行，**锚点漂移中位 452 行 / 最大 722 行**；`REPO_MAP.md` 还指着已删除的 `desktop/`、`build_release.sh`、`verify_bundle.sh`、`stub_external_bin.sh`；README 也指 `verify_bundle.sh`；AGENTS.md 首行指已删除的 `AGENT.md`；`A_LINE_LOGIC.md` 自带「行号漂移校正表」手工补丁（自述漂移 +58~+498） | G3：**锚规约（符号锚，行号仅辅助）** + 单一 `docs/RUNBOOK.md`（症状→第一现场→探针→测试→回退开关）+ 两条 CI 校验（路径存在 / 行号锚不新增）；入口索引复用已有 `call-diagnosis` skill 三件套 | `check_doc_paths` 0 断链；RUNBOOK 覆盖 ≥12 条症状；live 文档新增 `file:line` 锚 = 0 |

**为什么是这三波**：目录（G1）解决「在哪」、模块（G2）解决「改哪里」、索引（G3）解决「怎么找」。
三者都不动运行时行为——D5 单点仲裁 / C 契约波该做的事，本计划只提供基础设施（见 §5）。

---

## 1. 现状数字（四路探针）

### 1.1 scripts/ 普查（卫生波后在途状态）

**规模**

| 指标 | 数字 |
|---|---|
| 受管文件 | **138**（`*.py` 124 / `*.sh` 10 / `*.ps1` 3 / `*.spec` 1） |
| 总行数 | **43,835** |
| 子目录 | `archive/`（3 件，在途刚迁入）、`artifacts/`（10 件隐藏 JSON/SQL，在途刚迁入）、`cuda/`（7 件 CUDA 部署包）、`data/`（1 件 `intent_catalog_v1.json`）、`__pycache__/`（209 项） |
| 最大单体 | `ab_slot_actor.py` 1933 行、`probe_flow_graph.py` 1722、`prepare_csc_data.py` 1418、`probe_intent_mine.py` 1323、`probe_branch_action.py` 1272 |

**年龄分桶**（`git log -1 --format=%cs` 相对 2026-10-04）

| ≤7 天 | 8-14 天 | 15-30 天 | 31-60 天 | >60 天 | 无 git 史 |
|---|---|---|---|---|---|
| 47 | 50 | 39 | 1 | **0** | 1 |

**结论：scripts/ 不是「死文件垃圾场」，是「活文件垃圾堆」**——97/138（70%）近两周有提交。
所以治理动作不能是「大扫除删除」，只能是「归位 + 索引 + 退役流程」。

**用途聚类（人工规则，见附录 A 全量映射）**

| 桶 | 数量 | 代表 |
|---|---|---|
| probes（真栈行为取证） | 48 | `probe_latency_soak.py` `probe_flow_20rounds.py` `probe_offscript_soak.py` |
| e2e（整通验收） | 10 | `e2e_real_customer.py` `e2e_trilingual_livekit.py` `e2e_barge_in.py` |
| bench（台架/压测/测量） | 15 | `ab_slot_actor.py` `bench_9b_direct.py` `measure_latency.py` `load_cp_concurrency.py` |
| pipeline（模型/数据管线） | 11 | `train_csc_model.py` `prepare_csc_data.py` `qa_bank.py` `snippet_seed_mining.py` |
| seed（资产/导入/种子） | 13 | `gen_filler_assets.py` `import_xkt_qa.py` `build_asr_variants.py` `load_intent_catalog.py` |
| ops（构建/安装/部署/门禁） | 23 | `bootstrap.sh` `install-node.*` `build_*_pkg.sh` `check_schema_drift.py` `smoke_postgres.py` |
| runtime（产品运行时 exec） | 3 | `pregen_tts.py`（CP `pregen.py` 调用）`mock_callee.py`（CP `main.py` 调用）`mine_qa.py`（`bok tts-mine`） |
| lib（被 import 的助手） | 2 | `urlguard_gate.py`（in-degree **22**）`probe_stimulus.py` |
| archive（零/软引用候选） | 13 | `t56r1_probe.py` `r1_build_gold.py` `probe_minimax_speed_ab.py` …（15 个软零引用里留 2 个在桶内：`e2e_http.py`、`pad_test_audio.py`） |

> 口径说明：目录里 `probe_*.py` 原始计数是 **56**；目标桶 `probes/` 收 48（另 7 个进 `archive/`、1 个 `probe_stimulus` 进 `lib/`）。
> 上表 9 行合计 = 138。

**同目的变体簇**（同名前缀 × N——「找不到哪个才是我要的」的直接来源）

- `probe_interp_*` ×4：`backlog` `continuous` `duplex` `late_mic`
- `probe_llm_*` ×4：`cache` `contention` `draft_ab` `stall`
- `probe_minimax_*` ×3：`bidi_cold` `emotion_tags` `speed_ab`
- `probe_reply_*` ×3：`latency` `parity` `quality`
- `probe_cloud_asr*` ×2、`probe_deepseek_*` ×2、`probe_flow_*` ×2、`probe_mt_*` ×2、`probe_qa_*` ×2、`load_*` ×5、`build_*` ×5

**引用面（决定搬迁成本的硬数字）**

| 消费者 | 引用条数 | 说明 |
|---|---|---|
| 文档（活的） | 175（18 个文件） | 改 |
| 文档（历史档 `docs/superpowers/plans/**`、`reports/**`） | 250（59 个文件） | **不改**（历史记录） |
| 脚本 → 脚本 | 175 | 见 import 图 |
| 测试 | 64 处（37 个文件） | 改 |
| 代码（apps/packages/tools/deploy/security） | 58 | 改 |
| CI | 12（5 个 workflow） | 改 |

- 有 code/CI/test 硬引用的脚本：**77/138**；迁到新路径必须同时编辑的文件：**68 个**（含 3 个 `apps/web/.next/**` 构建垃圾，应删/忽略；真实编辑面 ≈65 个文件，清单见附录 B）。
- 零外部引用（严格）= **12 个**；放宽到「只被文档提到」= **15 个**。这 15 个里取 **13 个**进 `archive/` 候选（`e2e_http.py` 与 `pad_test_audio.py` 留在 e2e/seed 桶——活文档仍在引用它们）；**只挪不删**，逐个由 Ethan 过目（部分近期探针仍在手边用）。

**脚本间 import 图（本次普查最关键的发现）**

- **56 条 `import <sibling>` 边，44 个 importer**；in-degree Top：`urlguard_gate` 22、`e2e_real_customer` 9、`e2e_interpret` 4、`e2e_barge_in` 3、`probe_latency_soak` 3、`probe_brand_words` 2、`probe_hotword_ab` 2、`measure_prompt` 2、`probe_interpret_latency` 2。
- **44 个 importer 全部会在裸搬家后断**：16 个没有任何 `sys.path` 引导；28 个有引导但写的是 `_SCRIPTS = Path(__file__).resolve().parent`（搬进桶后 parent 变成桶目录，兄弟模块仍在 scripts/ 根，照样断）。
  → 所以 G1a 要**先把 4 行引导头插进全部 124 个脚本**（同层时是行为不变的 no-op），搬家和 import 才能解耦。这决定了 G1 的机械形状（§3.1/§4 G1a）。

**运行时 exec 依赖（不是 dev 工具，别当垃圾）**

| 脚本 | 谁在运行时调用 | 路径字面量 |
|---|---|---|
| `scripts/pregen_tts.py` | CP `control_plane/pregen.py:147`、`main.py:5792`；`tools/bok.py:4670`（`tts-pregen`） | `_repo_root()/"scripts"/"pregen_tts.py"` |
| `scripts/mock_callee.py` | CP `main.py:3886`；`tools/bok.py:3378`（进程识别 marker 字符串） | `repo_root/"scripts"/"mock_callee.py"` |
| `scripts/mine_qa.py` | `tools/bok.py:4683`（`tts-mine`）；`qa_cluster` 同源注释 | `ROOT/"scripts"/"mine_qa.py"` |
| `scripts/mlx_lm_template_leak_fix.py` | `tools/bok.py:1612` 真路径 exec | `ROOT/"scripts"/"mlx_lm_template_leak_fix.py"` |

**在途清理（本次普查时工作区已有，53 条改动）**：`scripts/test_*.py|sh` 3 件 → `scripts/archive/`；10 个隐藏 JSON/SQL → `scripts/artifacts/`；`docs/*` 10 件 → `docs/archive/`；`AGENT.md` 删除（staged）；8 个根目录 `bok-architecture.*` 工件 staged 删除（磁盘仍在，`.gitignore` 已覆盖）；`REPO_MAP.md`/`AGENTS.md`/`README.md`/`DELIVERY-MANUAL.md` 有编辑。
→ **G1 必须基于这个状态执行，且不得与在途清理打架**（先让在途清理落地成一个 commit，再开 G 波）。

### 1.2 tools/bok.py 解剖

**总量**：4,774 行 / 246KB / 138 个顶层 `def|class` / **14 个子命令**（`catalog manifest status serve down doctor tts-mine clean-testdata monitor up download tts-pregen prod setup`）/ 57 个 `os.environ` 读取键（37 个 `BOK_*`）。

**关注点簇（行区间，可复现脚本见附录 C）**

| 簇 | 行区间 | 行数 | 备注 |
|---|---|---|---|
| 头部/imports/`ROOT` | 1–102 | 102 | `BOK_ROOT` 与 `Path(__file__).parents[1]` 钉死仓根 |
| `MODELS` 模型表（mac/windows） | 103–178 | 76 | 含大量决策注释（8bit 选择/9B 专线/可选模型） |
| 平台路径与运行时定位 | 179–727 | 549 | `platform_key/model_path/runtime_root/app_data_dir/bundled_*/_livekit_config_path` |
| 健康与探活 | 728–1088 | 361 | `healthy/_http_ok/_llm_http_ready/_worker_ports/_probe_llm/_desktop_stack_targets` |
| 模型目录/下载/状态 | 1090–1271 | 182 | `cmd_catalog/manifest/setup_models/cmd_download/cmd_status` |
| 进程原语 | 1272–1415 | 144 | `_rotate_log/_spawn_kwargs/_start_proc/_stop_pidfile/certifi` |
| **CP 面 env** | **1416–1604** | **189** | `_control_plane_env()` — CP 容器/单元环境单源（23 处 `env[...] =` 写入） |
| LLM/服务启动器 | 1605–2051 | 447 | `_apply_mlx_template_fix/_default_prompt_cache_bytes/_mac_llm_server_argv/_start_llm/_start_mt_llm/_start_settle_proxy/_start_settle_llm/_start_laya/_start_call_plane` |
| `cmd_up` / 通话面服务 | 2052–2256 | 205 | `cmd_up/_cmd_up_services`（CP/LiveKit/web 的拉起顺序） |
| judge env + **`_FORWARD_ENV`** | 2261–2689 | 429 | `_apply_judge_env` 37 行 + **`_FORWARD_ENV` 392 行 / 250 键**（全仓单源，`tests/test_forward_env.py` 静态扫描它的源码文本） |
| agent/interp worker env 与 worker 表 | 2690–2814 | 125 | `_apply_bok_passthrough_env`（别名 = `_FORWARD_ENV`）/`_apply_flow_graph_env`/`_agent_worker_env`/`_worker_specs` |
| pid/杀树/监视器 | 2815–3049 | 235 | `_pid_alive/_pidfile_alive_stamped/_pid_reused_stale/_kill_proc_tree/_kill_pidfile/_ensure_monitor` |
| `cmd_monitor` | 3050–3174 | 125 | 通话态看护（活性判定） |
| `cmd_serve` / `cmd_down` | 3175–3344 | 170 | serve 幂等（端口已监听跳过） |
| 孤儿清扫 | 3345–3654 | 310 | `_sweep_orphan_workers/_process_serve_root/_pid_origin_foreign/_sweep_stale_root_markers/_orphan_port_owners/_sweep_orphan_listeners` |
| doctor | 3655–4114 | 460 | GPU/内存/虚拟声卡/MiniMax TTS 探测/queue proxy lease |
| 生产常驻（prod） | 4115–4624 | 510 | `_agent_prod_env/_interp_env/_prod_units/systemd 暂存/cmd_prod_install/uninstall/status` |
| CLI 面（parse_args/main/小命令） | 4625–4774 | 150 | argparse 14 子命令 + `tts-pregen/tts-mine/clean-testdata` + `main` 分派 |

**端口字面量分布**（「端口表」事实上不止一张）：`:1235`×96、`:1237`×53、`:1236`×43、`:1239`×35、`:8788`×28、`:8791`×22、`:8000`×22、`:8787`×20、`:8789`×18、`:7880`×16、`:3000`×13、`:8084`×9。
集中定义的只有 `_worker_ports()`（L802–892）与 `_desktop_stack_targets()`。

**拆包的真实阻力（数字）**

| 约束 | 数字 | 影响 |
|---|---|---|
| 引用 `tools/bok.py` 路径的文件 | **182** | 入口路径不能改，`tools/bok.py` 必须是 launcher/shim |
| 测试 `import bok` 后使用的属性 | **112 个**（`bok._FORWARD_ENV` 39 次、`bok.ROOT` 22、`_monitor_kill_round` 17、`_control_plane_env` 16、`repo_python` 14、`_sweep_orphan_listeners` 14、`_agent_worker_env` 14、`_agent_prod_env` 14…） | shim 必须再导出全部（或测试迁移） |
| 测试读 `tools/bok.py` 源码文本做源级 pin | **28 个测试文件**（其中 23 处直接 `(ROOT/"tools"/"bok.py").read_text()`，另 5 处先存成 `_BOK_SRC` 常量；另有 22 个仅在注释/docstring 里提到 bok.py，合计 50 个文件提到） | 扫描面必须从「一个文件」改为「包目录」——**这是最大单点风险** |
| `monkeypatch.setattr(bok, "X", …)` 打在本模块命名空间 | **212 处 / 19 个测试文件**（`app_data_dir` 33、`is_mac` 22、`is_linux` 14、`healthy` 13、`repo_python` 10、`_start_proc` 7、`_embedded_livekit` 7…，共 64 个属性） | 直接拆分 = 这些 patch 全部失效（被调函数看不到） |
| `monkeypatch.setattr(bok.os/subprocess/urllib.request/time/socket/_platform, …)` | 72 处 | **不受影响**（打的是共享模块对象，拆包不破） |

→ 结论：**「先拆文件」必炸；正确顺序 = 先给 patch 面建间接层，再搬**（§4 G2 W①）。

### 1.3 map / 索引体系现状与陈旧度

**文档存量**：`docs/` 97 个 md（顶层 29）；`docs/superpowers/plans/` 38 份计划（探针时，含 1 未跟踪；本文件为第 39）；仓库 md 共 6,181 个（探针时，含 tests/reports）。

| 文档 | 行数 | 最后实质提交 | 陈旧度证据 |
|---|---|---|---|
| `docs/ORCHESTRATION-MAP.md` | 295 | 2026-10-02（**只提交过 1 次**，a9c248a） | 基线 78e17e1 上 agent.py 的 9 个锚**逐点全对**；此后 194 commits（27 次触 agent.py），agent.py 8014→**8913** 行，**锚漂移中位 452 / 最大 722 行**；livekit_plugins 锚（8414→8815 行）漂移 +214~+547，且该段锚按「未提交 worktree 终态」标定，与 agent.py 的「已提交 HEAD」口径混用 |
| `docs/REPO_MAP.md` | 124 | 2026-10-02（23 次提交） | 指向已不存在的 `desktop/`、`scripts/build_release.sh`、`scripts/verify_bundle.sh`、`scripts/stub_external_bin.sh`；`CONTRACTS/DEV_TOOLS` 已迁 `docs/archive/` 仍在指路 |
| `AGENTS.md` | 224 行 / 50KB（在途清理后） | 2026-10-03（110 次提交） | 首行链 `AGENT.md`——**已在途 staged 删除**（断链）；波次纪要占 25% 字符；321 个反引号符号引用 / **0 个 `file:line` 锚**（这个姿势是对的） |
| `README.md` | — | 在途编辑 | 引用 `scripts/verify_bundle.sh`（不存在） |
| `docs/A_LINE_LOGIC.md` | 735 | 2026-09-20 之后 | 自述「行号已漂移 +58~+498，以 §10.5 行号漂移校正表为准」——**本仓已经付过一次行号锚的债** |

**已有的对症资产（G3 应站在它肩上，不另起一份）**：`.agents/skills/call-diagnosis/`——`SKILL.md`（症状速查 + turns/日志/离线复算三步法）+ `references/code-map.md`（53 行：机制→符号→回退开关）、`markers.md`（69 行：日志标记词表）、`probes.md`（50 行：探针命令）。这是仓库里**唯一**「症状→去处」形态的索引，但它是 agent skill 私有引用，人（和新人）进的入口是 `AGENTS.md`/`REPO_MAP.md`，两者都不含症状维度。

**机器校验**：`.github/workflows/` 8 个 workflow，**零**文档路径/链接检查；`pytest` 365 个测试文件（85,838 行）里也没有 doc-path 检查。

### 1.4 执行前提（在途变更）

当前工作区有 53 条未提交改动（清理中的迁移）。G 波启动前置：

1. 在途清理先落一个 commit（否则 G1 的 `git mv` 会与 staged rename 混淆，`git status` 双写）；
2. `docs/COMMIT` 口径确认：G 波每步一个 commit，可 `git revert` 回滚；
3. 本计划文成时**没有**执行任何 G 动作。

---

## 2. 外部先例（可抄的具体做法）

| 案例 | 事实（结构/数字） | 抄什么 |
|---|---|---|
| **livekit/agents**（https://github.com/livekit/agents） | 顶层 = `livekit-agents/`（核心包）+ `livekit-plugins/`（**76 个**插件目录，每个自带 `pyproject.toml`+`README`+同构子包：`llm.py/stt.py/tts.py/realtime/`）+ `examples/` + `tests/` + **`scripts/` 只有 2 个文件**（`check_types.py`、`generate_test_summary.py`）；核心包按关注点分子包（`llm/ stt/ tts/ vad/ voice/ cli/ ipc/ metrics/ utils/ evals/`）；`cli/` 自成子包（`cli.py` 17KB + `discover.py/log.py/watcher.py/readchar.py`）；`tests/` 平铺 + 共享 fake（`fake_llm.py/fake_tts.py/fake_vad.py`）+ pytest 类别 marker（unit/audio_eot/plugin/…） | ① **scripts/ 极薄**：构建/检查入口进 Makefile，dev 工具放 tests fakes 或包内；② CLI 独立成子包；③ 插件/模块各自带 README+pyproject 的「一个板块一个家」；④ 测试类别 marker 让「跑哪套」可寻址 |
| **huggingface/speech-to-speech**（https://github.com/huggingface/speech-to-speech） | `src/speech_to_speech/{LLM,STT,TTS,VAD,api,pipeline,diarization,utils,arguments_classes}`——**按角色分目录**；`archive/` 带自己的 README 收退役代码；`scripts/` 只有 3 个（2 个 benchmark + 1 个合成对话客户端）；tests 平铺一事一文件 | ① 顶层按**角色**（不是按技术层）分；② `archive/` 是**一等目录**（退役有去处，不必删）；③ scripts/ 只放「一次性跑完就走的台架」 |
| **openai/openai-agents-python**（https://github.com/openai/openai-agents-python） | **没有 `tools/`、没有 `scripts/`**；根 = `Makefile` + `PLANS.md`（ExecPlan 规范）+ `AGENTS.md`+`CLAUDE.md`；`src/agents/` 平铺 + 子包（`mcp/ memory/ handoffs/ extensions/`），私有模块 `_*.py` 前缀；`examples/`+`integration_tests/`+`tests/` 分家；docs 走 mkdocs | ① 当「入口脚本」只剩 Makefile 时，脚本目录可以彻底消失；② `PLANS.md` 把「计划怎么写」也变成仓库契约（我们的 `docs/superpowers/plans/` 缺一份这样的规约）；③ examples 与集成测试分家 |
| **astral-sh/uv**（https://github.com/astral-sh/uv，Rust） | `crates/uv/src/bin/{uv.rs,uvw.rs,uvx.rs}` 三个**薄 bin**；`crates/uv/src/commands/` **25 个模块**（一命令一模块，`commands/mod.rs` 只做 re-export）；**CLI 参数定义单独成 crate**（`crates/uv-cli/src/lib.rs` 305KB + `options.rs`）；共享设置 `settings.rs` | ① 「一命令一模块 + mod.rs 汇总」；② **参数定义与实现分离**（bok 的 `parse_args` 应该独居 `cli.py`）；③ 薄 bin = `tools/bok.py` 15 行 launcher |
| **pypa/pip**（https://github.com/pypa/pip，Python） | `src/pip/_internal/cli/{main.py,main_parser.py,parser.py,base_command.py,cmdoptions.py}` + `src/pip/_internal/commands/` **19 个命令模块**，命令注册表集中一处 | Python 版本的同款：**parser 单点 + 一命令一文件 + 命令注册表**；bok 完全对齐这个形状即可 |
| **modal-labs/modal-client**（https://github.com/modal-labs/modal-client） | 多语言 monorepo：`py/ go/ js/ modal_proto/ test-support/`；任务入口 = `tasks.py`+`invoke.yaml`（invoke 任务运行器）；无 scripts/ | ① 语言/板块在顶层直接分家；② 「怎么跑」用任务运行器声明（我们对应 `tools/bok.py <subcommand>`） |

**共性结论**：成熟仓的 `scripts/` 都是**个位数**；所有「多能力」都被拆成包/子包 + 一个薄入口；
`archive/`/`examples/` 都是一等目录；「怎么跑」集中在 Makefile / tasks.py / CLI 一个入口。

---

## 3. 目标布局（目录树草案）

### 3.1 scripts/ 目标树

```
scripts/
├── README.md          # 脚本索引单源：文件 → 一句话用途 → 消费者（CI/CP/人）→ 需真栈？→ 上次证据
├── lib/         (2)   # 被 import 的共享助手（不是入口）：urlguard_gate.py（in-degree 22）、probe_stimulus.py
├── runtime/     (3)   # 产品运行时 exec 的（CP 路径契约面）：pregen_tts.py、mine_qa.py、mock_callee.py
├── probes/     (48)   # 真栈行为取证：probe_*.py
├── e2e/        (10)   # 整通验收：e2e_*.py（含被 9 个探针复用的 e2e_real_customer）
├── bench/      (15)   # 台架/压测/测量：bench_*/ab_*/measure_*/load*/soak/gpu_*/e4_*/redteam
├── pipeline/   (11)   # 模型与数据管线：train_/eval_/predict_/prepare_/prep_/build_asr_variants/r2_/qa_*/snippet
├── seed/       (13)   # 资产/导入/种子：gen_*/seed_*/import_*/load_*templates|catalog/migrate_*/render_*/pad_*/cache_*
├── ops/        (23)   # 构建/安装/部署/CI 门禁：build_*/bootstrap*/install-node*/setup-*/deploy_sip_edge/check_schema_drift/dump_postgres_ddl/smoke_*/node_handshake_smoke/mm_*/node_agent.spec
├── archive/    (13+)  # 退役（现有 3 + 候选 13；只挪不删，逐个过目）
└── artifacts/         # 生成物（已存在，勿手改）
```

**每桶里的「变体簇」用索引化解**（不强制合并代码——合并 4 个 `probe_interp_*` 是另一个议题，先让 README 一眼说清哪个是哪个）：

```
probes/README.md（或 scripts/README.md 的 probes 段）
  probe_interp_backlog.py    B 线积压计数（读 CP /api/interpret/backlog）→ 消费：人
  probe_interp_continuous.py 连续说话不断流（复用 e2e_interpret）→ 人
  probe_interp_duplex.py     双向同时说话（复用 e2e_interpret+probe_interpret_latency）
  probe_interp_late_mic.py   迟到麦克风首句（独立）
```

**迁移机械（G1 执行时逐字照做）**

1. **先索引后搬家**：生成 `scripts/README.md`（138 行表），零风险、立即可用；再动文件。
2. `git mv` 保历史；一次一个桶，一桶一个 commit。
3. 引用重写（脚本按文件列表 sed，逐个仓内 grep 复核）：
   - **硬引用 68 个文件 = CI 5 + 代码 22（含 3 个 `.next` 构建垃圾，顺手删）+ 测试 37 + 部署/安全 4** → 真实编辑面 65；
   - **活文档 18 个 + `.agents/skills/call-diagnosis/references/probes.md`**（175 + 10 处）；
   - 历史文档（`docs/superpowers/plans/**`、`reports/**`，59 文件 250 处）**不改**——它们是历史记录，改了反而失真；CI 白名单排除。
4. **import 引导头（4 行，先于一切搬家、一次机械插入全部 124 个 `scripts/**/*.py`）**：
   ```python
   # --- scripts import bootstrap (G1) ---
   import sys as _sys, pathlib as _pathlib
   _S = _pathlib.Path(__file__).resolve().parents[1]
   for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
       if str(_d) not in _sys.path:
           _sys.path.insert(0, str(_d))
   ```
   **为什么先插再搬**：此刻所有文件同层，`sys.path[0]` 本来就是 `scripts/`，插入是**行为不变的 no-op**（可先单独提交并跑全量验证）；此后任何文件挪进任何桶，56 条裸 `import <sibling>`（`urlguard_gate` 等）继续解析，**搬家与 import 解耦**。
   CI lint `tests/test_script_bootstrap.py`：`scripts/**/*.py` 含 marker（纯库 `lib/` 除外/或同规则），缺 marker 即红。
   *（终态可选：`lib/` 升级成真包 + 显式 `from lib.x import y`，但那是一次额外重写；G1 先不承诺。）*
5. **CI paths 改**：`.github/workflows/{ci,release,node-handshake,postgres-smoke,schema-drift}.yml` 里的 `paths:` 与 `run:` 逐条改（附录 B）。
6. **AGENTS.md / REPO_MAP.md / DELIVERY-MANUAL.md / call-diagnosis 三件套**的 scripts 段同步改；`REPO_MAP.md` 顺手清 3 条历史断链。

**不动的东西（红线）**：`scripts/artifacts/.p0_supabase_schema.sql` 被 `schema-drift.yml` 的 `paths:` 与 `check_schema_drift.py` 双向钉住——**路径不变**（在途已把它从 scripts 根挪到 `artifacts/`，CI 已随之改；G1 不再动它）；`scripts/cuda/` 是独立交付包（CUDA 一键部署），**整目录原样保留**。

### 3.2 tools/ 目标树（bok 拆包）

```
tools/
├── bok.py                 # 15 行 launcher：把 tools/ 加进 sys.path，from bokctl.cli import main
├── bokctl/                # 实现包
│   ├── __init__.py        # 兼容再导出（tests 的 112 个属性名；文档里所有 bok.X 都还能用）
│   ├── cli.py             # argparse 单点（14 子命令）+ main 分派   [现 L4625–4774/150 行 → 分派逻辑 ≤120]
│   ├── paths.py           # ROOT/app_data_dir/runtime_root/is_mac/is_linux/sidecar_python/bundled_*/livekit 配置
│   ├── models.py          # MODELS 表 + model_dir/model_path/resolve_llm_repo/_local_tts_needed/(settle|mt|draft|laya) 选择
│   ├── env.py             # ★ 单源：_FORWARD_ENV(250) / _control_plane_env / _agent_worker_env / _agent_prod_env / _interp_env / _apply_*  — 禁止任何模块复制表
│   ├── ports.py           # ★ 单源：ASR=8787 TTS=8788 CP=8000 LK=7880 LLM=1235 MT=1236 SETTLE=1237 LLM_RAW=1239 EMBED=8789 LAYA=8791 WEB=3000 REALTIME=8084（活跃代码路径逐步收编，见 G2 W④）
│   ├── health.py          # healthy/_http_ok/_llm_http_ready/_worker_ports/_probe_llm/_desktop_stack_targets
│   ├── proc.py            # _start_proc/_kill_proc_tree/pid 原语/monitor spawn/sweeps
│   ├── servers.py         # _start_llm/_start_mt_llm/_start_settle_*/_start_laya/_start_call_plane/_cmd_up_services
│   ├── prod.py            # _prod_units + cmd_prod_install/uninstall/status（消费 tools/systemd_units.py + schtasks_units.py）
│   ├── doctor.py          # doctor 全部
│   └── commands/          # 一命令一模块（uv/pip 形状）
│       ├── serve.py down.py status.py up.py monitor.py download.py setup.py misc.py（tts-pregen/tts-mine/clean-testdata）
│       └── ...
├── node_agent.py          # （不动）节点 agent
├── schtasks_units.py      # （不动）Windows 单元纯函数生成器
├── systemd_units.py       # （不动）Linux 单元纯函数生成器
└── browser-e2e/           # （不动）
```

**三条拆包纪律（写成 lint，不靠人记）**

1. **单源表红线**：全仓 `_FORWARD_ENV = (` 与 `def _control_plane_env` **各自只允许出现 1 次**（`tests/test_bok_module_contract.py` 扫 `tools/`）；`_FORWARD_ENV` 的键集合与拆分前**逐字节相同**（快照对比）。
2. **可 patch 缝 = 穿模块对象调用**：`bokctl/` 内部一律 `from bokctl import paths` 后 `paths.app_data_dir()`，**禁止** `from bokctl.paths import app_data_dir`（后者会把名字绑死，test patch 失效）。
3. **源扫描面改目录**：28 个读 `tools/bok.py` 源码文本的测试，统一改走 `tests/_bok_src.py::bok_source()`（`Path("tools/bokctl").rglob("*.py")` 拼接 + launcher）；**函数签名不变，测试用例内的断言不动**。

**为什么不让 `tools/bok.py` 变成包**：182 个文件按路径调用它，28 个测试按文件读它；`import bok` 与包 `bok/` 同名会互相遮蔽（tests 用 `sys.path.insert(tools); import bok`）。**launcher + 异名包 `bokctl/` 是唯一零参考断链的形状**。

### 3.3 docs/ 索引体系目标态

```
docs/
├── RUNBOOK.md          # ★ 新增：唯一「问题→去处」入口（人 + agent 都从这里进）
├── REPO_MAP.md         # 目录地图（改：清断链 + 每个板块一行「遇到 X 看这里」）
├── RUNTIME_TOPOLOGY.md # 端口/数据流（不动）
├── ORCHESTRATION-MAP.md# 编排时序（改：锚规约；内容由 C 波拥有）
├── LATENCY_BUDGETS.md  # 超时政策（不动）
├── TURN-PIPELINE.md / A_LINE_LOGIC.md / ARCHITECTURE-MAP.md / DELIVERY-MANUAL.md / …（不动）
└── superpowers/plans/**  # 历史（不改引用）
```

**`docs/RUNBOOK.md` 形状（人手维护，起步 12–20 行，每条带证据字段）**

| 症状 | 第一现场 | 探针/复算 | 测试 | 回退开关 |
|---|---|---|---|---|
| TTS 首声慢 | `providers/livekit_plugins.py` MiniMaxTTS/bidi 头段 flush | `scripts/probes/probe_latency_soak.py` → `reports/latency-soak/` | `tests/test_bidi_head_flush.py` | `MINIMAX_BIDI_HEAD_FLUSH=0` |
| env 改了不生效（prod） | `tools/bokctl/env.py` `_FORWARD_ENV` + `_control_plane_env` | `tests/test_forward_env.py` 静态扫描 | 同左 | — |
| 客户吃字/重问 | ASR 流 `_Qwen3ASRLiveStream` + 热词表 | `scripts/probes/probe_fast_speech.py`、`probe_8khz_asr.py` | `tests/test_sentence_commit.py` | `QWEN3_ASR_*=0` |
| 通话卡死/不回话 | 出口 chokepoint `_register_reply_lane`；watchdog | `scripts/probes/probe_latency_soak.py`（哑轮哨兵） | `tests/test_reply_chokepoint_lint.py` | `BOK_RESPONSE_WATCHDOG_S` |
| 流程不推进/乱推进 | `flow.py` verdict/`should_auto_advance` | `scripts/probes/probe_flow_graph.py`、`probe_flow_20rounds.py` | `tests/test_flow_controller.py` | 图/步开关 |
| WA 号码错读 | 收号链 + 数字热词；`guard_fabricated_number` | `scripts/probes/probe_cantonese_digits.py` | `tests/test_number_guard.py` | `BOK_NUMBER_GUARD=0` |
| 复读 | `_RepeatSelfGuardStream` + 跨轮账本 | `soak` 实弹 → turns `gen` 列 | `tests/test_repeat_cross_turn.py` | `BOK_REPEAT_CROSS_TURN=0` |
| 意图判错 | `intent_rules.py` 12 键 + judge 车道 | LAYA `:8791` / `probe_judge_parity.py` | `tests/test_intent_rules.py` | `BOK_LAYA_JUDGE=0` |
| LLM 饥荒/慢 | 路由表 + `:1234` 队列代理 + 饥荒 EMA | `bok status` / `/disaster` | `tests/test_llm_famine.py` | `BOK_MODEL_ROUTING=0` |
| 双派发/双开场白 | `room_claim.py` flock 互斥 | agent.log `[room-claim]` | `tests/test_room_claim.py` | `BOK_ROOM_CLAIM=0` |
| 跑的是哪份代码 | worktree 指针 | `ps aux \| grep agent_runtime` | — | — |
| 文档里的路径死了 | `scripts/ops/check_doc_paths.py` | CI | 同左 | — |

**机制（简单，不过度工程）**：
1. `docs/RUNBOOK.md` = 人手维护的症状表；**只链接**已有 `call-diagnosis` 三件套与各专题文档，不复制内容（避免第 N 份 map 漂移）。
2. 两条 CI 校验（放 `scripts/ops/`，pytest 包装成 `tests/test_doc_hygiene.py`，避免新人记不住脚本名）：
   - `check_doc_paths`：活文档里的仓内路径（`scripts/…`、`docs/…`、`tools/…`、`apps/…`）必须存在；白名单 = `docs/superpowers/**`、`reports/**`、`docs/archive/**`（历史）；
   - `check_doc_anchors`：统计 live 文档 `\.py:\d+` 出现数，**只准降不准升**（基线落一个 JSON）。
3. `scripts/README.md` 由 `tests/test_scripts_index.py` 收口：`scripts/**/*.{py,sh,ps1,spec}` 每个文件必须出现在索引表里（新增孤儿脚本 CI 即红）——**「目录即索引」**。

---

## 4. 分波执行

> 每波独立可交付；每步一个 commit；回滚 = `git revert`。禁止跨波并行改同一文件（参照 ORCH-CLEANUP-CONTRACT 的文件所有权纪律）。

### G1 scripts 迁移（三小步，按风险升序）

**G1a 索引先上 + 引导头预插（零风险，半天）**
- 动作：生成 `scripts/README.md` 138 行表（用途/消费者/是否需真栈/最近证据）；新增 `tests/test_scripts_index.py`（覆盖检查）；新增 `scripts/ops/check_doc_paths.py` + `tests/test_doc_hygiene.py`（含 `check_doc_anchors` 基线）；**一次机械插入 bootstrap 头到 124 个 py，并单独提交验证 no-op**。
- 验收令：`python3 -m pytest -q tests/test_scripts_index.py tests/test_doc_hygiene.py tests/test_script_bootstrap.py` 绿；**bootstrap 头提交后全量 pytest 绿（证明 no-op）**；`check_doc_paths` 输出断链清单 = **本次已知 4 条**（`desktop/`、`build_release.sh`、`verify_bundle.sh`、`stub_external_bin.sh` + README 的 verify_bundle）——先修平再开 G1b。
- 风险：无（只增文件 + no-op 头；头插入若有语法错，全量 pytest 立刻红）。

**G1b 叶子文件归位（中风险，1–2 天）**
- 动作：搬 **74 个无 import 边的叶子**（脚本互 import 的连通分量是 50 个文件：44 个 importer ∪ 16 个被依赖，见 §1.1）：`probes/`（除被 import 的 6 个）、`bench/`、`seed/`、`ops/`、`archive/` 候选 13 个；`runtime/` 的 3 个单独一个 commit（**CP 契约面**）。
- 迁移机械：`git mv` → 按附录 B 的 65 文件清单改引用 → 跑全量。**不再需要动任何 import**（G1a 已铺好）。
- 验收令：
  - `python3 -m pytest -q` **全量绿**（重点：`tests/test_cantonese_terminology.py` 13 处、`test_probe_stimulus.py` 12 处、`test_probe_8khz_asr.py`、`test_branch_action_probe.py`、`test_flow_graph_probe.py`、`test_ab_slot_actor.py`、`test_publish_pregen.py` 等）；
  - `python tools/bok.py tts-pregen --help` 与 `tts-mine --help` 能起（runtime 路径）；
  - 对 `runtime/` 三个脚本：`grep -rn "scripts/pregen_tts.py\|scripts/mock_callee.py\|scripts/mine_qa.py"` 全仓 0 命中（全部改到新路径）；
  - CI 五件（ci/release/node-handshake/postgres-smoke/schema-drift）的 `paths:`/`run:` 更新，`gh workflow` 本地 `bash -n` 校验 sh；release smoke 断言 `smoke/bok-node-*/scripts/install-node.sh` 同步改。
- 风险与对策：
  - release 打包会按 `scripts/install-node.sh` 断言 → `install-node.sh|ps1` **留在 ops/ 且同 commit 改打包脚本与断言**；若怕发行事故，可给 `install-node.sh` 保留 `scripts/` 顶层（豁免一个文件）——推荐前者（一次改净）。
  - CP 运行时 exec → `runtime/` 三件与 CP/`bok.py` 路径同 commit；`tools/bok.py:3378` 的进程识别 marker 字符串 `"scripts/mock_callee.py"` 必须同步（否则孤儿清扫不认 mock 被叫）。

**G1c 连通分量归位（中风险，1 天）**
- 动作：搬剩余 **50 个参与 import 的文件**——16 个被依赖模块（`e2e_real_customer`、`e2e_interpret`、`e2e_barge_in`、`probe_latency_soak`、`probe_brand_words`、`probe_hotword_ab`、`measure_prompt`、`probe_interpret_latency`、`probe_asr_digits_ab`、`asr_whisper_bench`、`e2e_edge_cases`、`probe_offscript_soak`、`smoke_postgres`、`dump_postgres_ddl`、`probe_stimulus`→`lib/`、`urlguard_gate`→`lib/`）+ 其余 importer 归入各自语义桶。import 侧由 G1a 的引导头兜住，本步只改引用。
- 验收令：
  - `python3 -m pytest -q tests/test_script_bootstrap.py`（新）绿；
  - **逐脚本 import 冒烟**：对 50 个参与 import 的模块跑 `python -c "import <module>"`（带引导头）——CI 化；
  - `scripts/ops/check_doc_paths.py` 0 断链；全量 pytest 绿。
- 风险：跨桶 import 在「跑脚本」时解析失败（本地静态检查不报、真跑才炸）→ 用上面的 import 冒烟兜住；若某 importer 残留 → 它是 `ImportError` 而非静默，定位成本低。
- **回滚**：G1c 单独 commit，回滚不影响 G1a/G1b。

### G2 bok 拆包（四步）

**W① 骨架 + patch 夹具（零行为变化，1 天）**
- 动作：`tools/bokctl/{__init__.py,core.py}`——`core.py` = 今天 bok.py 的全部内容（0 行改动搬运）；`tools/bok.py` → 15 行 launcher + `from bokctl import *` 兼容再导出；新增 `tests/_bokpatch.py`：`patch_bok(monkeypatch, "app_data_dir", value)` + `PATCH_TARGETS` 映射表（此刻全部指向 `bokctl.core`），并把 19 个测试文件里的 **212 处 `setattr(bok, "X", …)` 机械改写成 `patch_bok(...)`**；`tests/_bok_src.py::bok_source()` 上线（扫描面改目录）。
- 验收令：全量 pytest 绿（212 处改写 + 28 个源扫描改道后行为等价）；`python tools/bok.py status` 输出与改前逐字节同；`python tools/bok.py --help` 同。
- 风险：改写引入 typo → 用「一次改一个测试文件 + 跑该文件」的节奏；夹具先行让后续每步只改**一张映射表**而不是 212 处。

**W② 域模块搬运（每域一 commit，1–2 天）**
- 顺序（按 patch 面从薄到厚）：`prod.py` → `doctor.py` → `proc.py` → `health.py` → `servers.py` → `models.py` → `paths.py` → `env.py`（env 最后，因为它被 33 个测试引用，动它收益/风险比最差）。
- 每搬一域：把 `PATCH_TARGETS` 对应行从 `bokctl.core` 改到新模块 → 跑全量。
- 纪律：搬动的函数**穿模块对象调用**（纪律 2）；`_FORWARD_ENV`/`_control_plane_env` 在 `env.py` 内**原样搬运**，键序不变。
- 验收令：全量 pytest 绿 + 新 `tests/test_bok_module_contract.py`：①`_FORWARD_ENV = (` 在 `tools/` 唯一；②`_FORWARD_ENV` 键集合 == 快照（250 键）；③`def _control_plane_env` 唯一；④`tools/bokctl/**` 无「`from bokctl.X import f` 再调用可 patch 缝」的违规（AST 扫描白名单）。
- 风险：源扫描测试（28 个）——W① 已把扫描面改到 `tests/_bok_src.py::bok_source()`（目录拼接），此步只需确认它生效；若某测试直接 `read_text(ROOT/"tools"/"bok.py")` 漏改 → 断言失败会点名。

**W③ commands/ 子命令化（1 天）**
- `cli.py` 只留 argparse 定义 + 分派表；`cmd_serve/cmd_down/cmd_monitor/cmd_up/cmd_download/cmd_status` 等入 `commands/`。
- 验收令：`python tools/bok.py <14 子命令> --help` 全部可解析；`parse_args` 的 10 个测试使用点绿；全量绿。

**W④ ports.py 收编（可选，独立评估）**
- 只收「活跃代码路径」里的端口字面量（`_worker_ports`/sweep/health/serve/doctor/启动器），**不动注释与文档字符串**（96 处 `:1235` 大多在解释性注释里，全改会把 diff 撕碎且无收益）。
- 验收令：`grep -n "127.0.0.1:1235" tools/bokctl/` 的活跃代码路径 0 命中（注释除外）；全量绿。
- 风险：低；收益也是四步里最小——**如果时间紧，砍掉 W④ 不影响本计划目标**。

### G3 map / 索引（1–2 天）

- **G3a 修现存断链（零风险）**：REPO_MAP 的 `desktop/`、三个 build 脚本、`CONTRACTS/DEV_TOOLS` 指向 → 改；README 的 `verify_bundle.sh` → 改（或标注「已退役」）；AGENTS.md 首行 `AGENT.md` 链 → 删/改；ORCHESTRATION-MAP 头部补「最近实质更新 commit + 基线」字段。
- **G3b 锚规约立法**：新增 `docs/RUNBOOK.md`（§3.3 表）+ `scripts/ops/check_doc_anchors.py`（计数基线 JSON）；在 ORCHESTRATION-MAP 头部写明「`file:line` 只作辅助，权威=符号名；新增段落禁止只用行号」。**只动头部与锚格式，时序正文归 C 波**。
- **G3c RUNBOOK 与 call-diagnosis 合流**：RUNBOOK 每条「探针」链接 `scripts/probes/*`，「测试」链接 `tests/*`，「回退开关」链接 `tools/bokctl/env.py` 的键（G2 完成后路径稳定）；`call-diagnosis/SKILL.md` 加一行「人类入口 = docs/RUNBOOK.md」。
- 验收令：`check_doc_paths` 0（白名单外）；`check_doc_anchors` 计数不增；RUNBOOK ≥12 条症状且每条 4 列齐全；`tests/test_doc_hygiene.py` 绿。
- 风险：RUNBOOK 变成第三份漂移 map → 用「只链接不复写」纪律 + 路径校验兜住。

### 批次 R · 开发规范收编（2026-10-04 Ethan 规范十条 → 落地映射）

> 原则（与 G 波同源，用户定调原话：「能被工具强制的才是规范，不能的只是建议」「mimosa 的作用就是防止你乱来」）：
> 每条要么落 pre-commit / CI / hook，要么不写。先对照仓库现状，已有等价物的**不重造**，不适配的**写明理由**。

| 规范条 | 落地动作（并入哪个波） | 不采纳 / 适配理由 |
|---|---|---|
| Ruff 单工具链 | pyproject `[tool.ruff]` select=`["E","F","W","I"]` line-length=120；pre-commit 对 staged 文件 check；存量不整体 reformat（format 只对新文件）→ **R1** | AST lint 测试族（chokepoint/forward_env/terminology）保留——它们查 ruff 查不了的语义红线 |
| 契约层在前 | **已存在** = `packages/core/bok_voice_core`（model_routes / intent_rules / flow / qa_cluster 共享契约）；C 波 parity 测试就是执行面 | 不重构 greenfield `src/contracts` 布局——apps/* + packages/core 已实现同构原则，搬 = 纯 churn |
| 统一异常族 | CP 新 `errors.py`（`PipelineError` 带 stage 字段）只落 CP FastAPI 边界 → **R2** | agent worker 的 watchdog/degrade/饥荒链 = 既在的错误处理架构，重写无决策收益 |
| structlog | 不整体迁移 → **不立项** | 数十探针解析现有日志行格式（`MINIMAX_TTS_BIDI_PERF`/`[bok-timing]`/`S2S_FLOW`），行格式 = 已生效契约；新代码遵守 key=value 结构化 + 必带 call/turn 标识 |
| 三份文档纪律 | **已对齐**：AGENTS.md（唯一入口）/ RUNTIME_TOPOLOGY（≈protocol）/ superpowers/plans（=ADR 按日期） | G3 补 `docs/decisions.md` 一屏索引（只链接不复制）；不再造平行文档体系 |
| 预录音频回归 | **已存在** = corpus-v2 + E2E_ONLY/probe 电池 | 不重造；命名归一进 G3 RUNBOOK 索引 |
| 延迟断言进 CI | 不进 CI → **不立项** | 共享 Mac runner 方差 ±0.4s 需多跑分辨，CI 单跑断言必假红；probe 电池（soak/FLOW20 超标计数门槛）= 同功能真栈版 |
| Sentry | CP `main.py` init（traces 0.2）+ worker 关键路径 capture；DSN 走 env/settings DB → **R3** | 企业试点「线上出错你不在场」真需求，~2h，真增量 |
| Conventional Commits | commit-msg hook 校验 `type(scope):` 格式（既有习惯固化）→ **R1 同波** | — |
| 三脚本 + Runbook | 并入 **G3**；场景取自真实故障史（饥荒 / 双派发 / 槽位等待 / TTS 静默 / 换页冷启动 / 断链） | docker/CD 不采纳：部署 = bok serve + launchd，镜像化 = 新增故障面、零决策收益 |

**R1**（Ruff + commit-msg hook，半天）· **R2**（CP 异常族，2h）· **R3**（Sentry CP+worker，2h）——均排在 C 波之后、G2 之前落地（G2 拆包时新模块直接生在 Ruff 之下）。

**Mimosa 政策定案（2026-10-04 用户拍板：「防止你乱来」）**：提交门保持全量严检，**不设 vendor 豁免**——`services/s2s/**` 已含我们的补丁（base_openai 首段催产 / 豆包 handler / MiniMax bidi handler）= 自有 fork，按自有码修；扫描积压当窗清穿（方法 = 护栏迁移 agent 已验证的形态实验 → 真债修 / 误报改名 / 测试豁免化，**零 suppression 注释**）。

---

## 5. 与既有 C 契约波 / M 模块化波的接口关系

**现状**（取证）：仓库里的契约类文档只有两份——`docs/ORCH-CLEANUP-CONTRACT.md`（编排梳理波·冻结契约：文件所有权 S1=`agent.py`/`tts_cache.py`、S2=`livekit_plugins.py`/`fillers.py`/`ORCHESTRATION-MAP.md` + P0–P4 改动单）与 `docs/DR-WAVE-CONTRACT.md`（容灾可观测波）。**仓库内检索不到「M 模块化波」文档**（`grep 模块化 docs/` 零命中）；最接近的模块化纲领是 `docs/superpowers/plans/2026-10-01-first-principles-rework.md` 的 D5「单点仲裁重构」。

**划界（不重复的三条）**

| | C 契约波（ORCH-CLEANUP / DR-WAVE） | G 本计划 |
|---|---|---|
| 管什么 | **运行时行为契约**：时序、指标 kind、握手形状、冻结文件所有权 | **物理布局与可发现性**：文件放哪、模块怎么拆、索引怎么找 |
| 产物 | 契约文档（行为不变式）+ 代码改动单 | 目录树 + 薄入口 + 索引 + 3 条 lint |
| 触碰 | `apps/agent/**`（S1/S2 所有权） | `scripts/**`、`tools/**`、`docs/{RUNBOOK,REPO_MAP}`、`.github/**`；**不碰 apps/** |

**接缝规则**

1. **G 不改运行时语义**：G1/G2/G3 每步验收都要求全量 pytest 绿，任何行为变化 = 越界。
2. **`ORCHESTRATION-MAP.md` 是 C 的财产**：G3 只在头部加字段与锚规约，不改 §1–§7 的时序内容；若 C 波同期在改该文件 → G3 排到 C 波合流之后（文件所有权优先）。
3. **「contracts home」不重复建**：C 波若将来要把契约文档收进 `docs/contracts/`，**由 C 拍板路径**，G 只提供两件工具——`check_doc_paths`（迁移后断链检测）与 `git mv` 迁移机械（本计划 §3.1 第 2–3 条同一套）。G 本波**不搬任何契约文档**（搬 = 二次断链风险，收益为 0）。
4. **与 M/D5 的接口**：G2 的三条纪律（单源表 / 穿模块对象调用 / patch 夹具先行）就是 M 波拆 `agent.py`/`livekit_plugins.py`（8.9k/8.8k 行）时同样需要的前置设施。建议 M 波直接复用：①`tests/_bokpatch.py` 的同款夹具模式（先建间接层再搬模块）；②「单源表全仓唯一」lint 的写法；③`tests/_bok_src.py` 式的源码扫描面收敛（该仓有大量源级 pin 测试，M 拆 agent.py 会撞同一堵墙——本计划先把墙的位置量出来：28 个 bok 源扫描 + agent 侧的 `test_reply_chokepoint_lint` 族）。
5. **合并动作**：G2 完成后再开 M/D5 拆 agent.py，可避免两波同时改测试夹具；顺序建议 **G1 → G3a → G2 → G3b/c**（先索引止血，再动大件）。

---

## 6. 第一性原理：每条提案回答「解决哪个已量化的痛点」

| 提案 | 解决的量化痛点 | 不做的代价 |
|---|---|---|
| G1a 索引 + 索引 lint | 138 个文件平铺、56 个 probe 混放、变体簇 ×4；新人/agent 找脚本靠 `ls`+猜 | 每次排障平均多翻 1–3 分钟；「哪个 probe 验证过这个」不可回答 |
| G1b 叶子归位 | 同上 + 桶划分给 README 提供结构（无 import 边的文件搬动零风险） | 视觉垃圾堆继续膨胀（70% 文件是活的，不会自然消失） |
| G1c 连通分量 + bootstrap | **44 个 importer 全断**（16 个无引导 + 28 个引导写成「自身目录」）、56 条 import 边——不处理就搬不动 | 强行搬 → 44 个脚本 import 立即失败 |
| G1 历史档不改 | 250 处历史引用；改历史文档会失真且无收益 | 每次 diff 噪音 + 假历史 |
| G2 W① patch 夹具 | **212 处 `setattr(bok,"X")` / 19 文件**——直接拆包这类 patch 全失效 | 直接拆 = 19 个测试文件集体红，回滚成本高、信心崩 |
| G2 W② 域模块 | 4,774 行单文件；`_FORWARD_ENV` 392 行夹在 130 个函数之间；改 doctor 要在 4.7k 行里定位 | 每次改 bok 的认知成本 ≈ 全文件扫描；并行会话必撞车 |
| G2 单源表红线 | `_FORWARD_ENV` 是 prod 死门的唯一防线（历史上 69 键死门事故）；端口字面量已散落 96+53+43 处 | 拆包拆成两份表 = 重现「prod 静默死门」class 事故 |
| G2 W④ ports 只收活跃路径 | 端口只集中在 2 个函数定义、其余是注释——全收收益 ≈ 0，风险 ≠ 0 | 为美学重构付 diff 代价（违反本计划「不做美学重构」） |
| G3 锚规约 | **锚点 2 天漂移 452 行**（78e17e1→今，27 次 agent.py 提交）；A_LINE_LOGIC 已被迫手工维护校正表 | 「按旧图找错根因」的教训会复发（AGENTS.md 已记载多次） |
| G3 路径校验 | **6 处现存断链**（`desktop/`、3 个 build 脚本、README 的 `verify_bundle.sh`、AGENTS.md→`AGENT.md`）；0 个 CI 检查 | 断链只增不减，新人踩坑 |
| G3 RUNBOOK 单一入口 | 索引分散在 6 份文档 + 1 个 agent skill；症状维度（「TTS 慢去哪」）无处可查 | 「出问题第一时间知道去哪找」无法达成——这是用户原话的正面目标 |

---

## 7. 明确不做（防越界）

1. **不合并 probe 变体代码**（×4 那些）——先索引说清；合并是各专题的判断，不是治理波的事。
2. **不删任何脚本**——archive/ 只挪；零引用也只是「候选」，逐个过目。
3. **不搬契约文档、不新建 contracts/ 目录**（归 C 波）。
4. **不动 `apps/**`**（C 波所有权；`agent.py`/`livekit_plugins.py` 的模块化是 M/D5）。
5. **不动 `scripts/cuda/`、`scripts/artifacts/.p0_supabase_schema.sql`**（CI 钉住路径）。
6. **不做代码生成式索引**（不做 markdown 生成器/图谱服务）——两条校验 + 一份人手 RUNBOOK 足够；过度工程本身就是新的维护面。
7. **不改历史文档引用**（`docs/superpowers/**`、`reports/**`）。
8. **不 commit 本计划之外的任何东西**（本文件成文时也未 commit）。

---

## 附录 A：scripts/ → 桶 全量映射（138）

**runtime（3，产品运行时 exec，单 commit 优先）**
`pregen_tts.py` `mine_qa.py` `mock_callee.py`

**lib（2，被 import 的助手）**
`urlguard_gate.py` `probe_stimulus.py`

**probes（48）**
`probe_8khz_asr.py` `probe_ambient_keyboard.py` `probe_asr_digits_ab.py` `probe_branch_action.py` `probe_brand_words.py` `probe_cache_discipline.py` `probe_campaign_schedule.py` `probe_cantonese_digits.py` `probe_cloud_asr.py` `probe_cloud_asr_ab.py` `probe_ctx_decode.py` `probe_cuda_baseline.py` `probe_deepseek_thinking.py` `probe_fast_speech.py` `probe_filler_timing.py` `probe_flow_20rounds.py` `probe_flow_graph.py` `probe_gpu_contention.py` `probe_hotword_ab.py` `probe_intent_mine.py` `probe_interp_backlog.py` `probe_interp_continuous.py` `probe_interp_duplex.py` `probe_interpret_latency.py` `probe_judge_parity.py` `probe_killswitch.py` `probe_latency_soak.py` `probe_llm_cache.py` `probe_llm_contention.py` `probe_llm_draft_ab.py` `probe_llm_stall.py` `probe_minimax_bidi_cold.py` `probe_mt_glossary_ab.py` `probe_offscript_soak.py` `probe_offtopic_recovery.py` `probe_polish_model.py` `probe_preemptive.py` `probe_qa_hit.py` `probe_reply_latency.py` `probe_reply_parity.py` `probe_reply_quality.py` `probe_s2s_vs_cascade.py` `probe_session_lifecycle.py` `probe_settle_parity.py` `probe_smart_turn.py` `probe_thin_client_static.py` `probe_vad_head_syllable.py` `probe_windows_lifecycle.py`

**e2e（10）**
`e2e_barge_in.py` `e2e_campaign.py` `e2e_edge_cases.py` `e2e_flow_scenario.py` `e2e_http.py` `e2e_interpret.py` `e2e_multi_turn.py` `e2e_pipeline.py` `e2e_real_customer.py` `e2e_trilingual_livekit.py`

**bench（15）**
`ab_slot_actor.py` `ab_tts_first_chunk.py` `ab_vad_ten_vs_silero.py` `asr_whisper_bench.py` `bench_9b_direct.py` `bench_minimax_bidi.py` `e4_frequency_scan.py` `gpu_contention_probe.py` `load_audio_concurrency.py` `load_cp_concurrency.py` `loadtest_calls.py` `measure_latency.py` `measure_prompt.py` `redteam_probes.py` `soak_test.py`

**pipeline（11）**
`eval_csc_model.py` `eval_sensevoice.py` `judge_group_eval.py` `mlx_lm_template_leak_fix.py` `predict_csc_model.py` `qa_bank.py` `qa_laya_calibrate.py` `qa_match_report.py` `r2_threshold_refit.py` `snippet_seed_mining.py` `train_csc_model.py`

**seed（13）**
`build_asr_variants.py` `cache_minimax_auditions.py` `gen_filler_assets.py` `gen_route_gates.py` `import_xkt_qa.py` `load_compliant_templates.py` `load_intent_catalog.py` `migrate_templates_8step_0913.py` `pad_test_audio.py` `prep_tts_dataset.py` `prepare_csc_data.py` `render_asr_corpus_v2.py` `seed_invite_templates.py`

**ops（23）**
`bootstrap.sh` `bootstrap-node.sh` `build_livekit.sh` `build_node_agent.sh` `build_node_pkg.sh` `build_runtime.sh` `build_runtime_pkg.sh` `check_schema_drift.py` `deploy_sip_edge.sh` `dump_postgres_ddl.py` `install-node.sh` `install-node.ps1` `llm_cache_report.py` `mimosa_triage.py` `mm_llm_shim.py` `mm_voice.py` `node_agent.spec` `node_handshake_smoke.py` `setup-virtual-audio.sh` `setup-virtual-audio.ps1` `setup-windows.ps1` `smoke_postgres.py` `smoke_sidecars.py`

**archive（13 候选，只挪不删，逐个过目）**
`acceptance_0913_ghost.py` `acceptance_0913_scenarios.py` `asr_whisper_ane_leg.py` `bench_judge_contention.py` `probe_deepseek_cloud.py` `probe_e4_timing.py` `probe_interp_late_mic.py` `probe_minimax_emotion_tags.py` `probe_minimax_speed_ab.py` `probe_mt_lang_validator.py` `probe_qa_phonetic.py` `r1_build_gold.py` `t56r1_probe.py`

**不动**：`scripts/cuda/**`（独立交付包）、`scripts/artifacts/**`（生成物）、`scripts/data/**`（资产）、`scripts/archive/**`（已有人住）

---

## 附录 B：搬家断链清单（执行时逐条核对）

**B1 必须同 commit 编辑的引用文件（硬引用 68 = CI 5 + 代码 22 + 测试 37 + 部署/安全 4；真实编辑面 65，另 3 个是 `apps/web/.next/**` 构建垃圾）**

- **CI（5）**：`.github/workflows/ci.yml`（`probe_thin_client_static.py`）、`release.yml`（`build_runtime.sh`/`build_node_pkg.sh`/`build_runtime_pkg.sh` + `test -f smoke/bok-node-*/scripts/install-node.sh` 断言）、`node-handshake.yml`（`install-node.sh` 3 处 + `node_handshake_smoke.py` + `probe_killswitch.py`）、`postgres-smoke.yml`（`smoke_postgres.py`）、`schema-drift.yml`（`paths:` 3 条 + `check_schema_drift.py`）
- **代码（真实路径，7 处 + 12 处注释/构建垃圾）**：`apps/control-plane/control_plane/pregen.py:147`（`scripts/pregen_tts.py`）、`main.py:3886`（`scripts/mock_callee.py`）、`main.py:5792`（`scripts/pregen_tts.py`）、`tools/bok.py:1612/3378/4670/4683`、`apps/web/lib/minimax-voices.ts`（`cache_minimax_auditions.py`）；注释里的路径（非阻断但应同步）：`apps/agent/agent_runtime/{dialer,fillers,intent_semantic,qa_gate,laya_judge,interpret}.py`、`providers/doubao_asr.py`、`apps/control-plane/control_plane/{hotword_mining,qa_cluster}.py`、`packages/core/bok_voice_core/{asr_polish,flow_graph,polish,qa_cluster}.py`、`tools/{node_agent,schtasks_units}.py`、`packages/core/bok_voice_core/testdata.py`；垃圾 3：`apps/web/.next/**`（删/忽略）
- **测试（37 文件 / 64 处）**：`test_cantonese_terminology`(13) `test_probe_stimulus`(12) `test_ab_slot_actor`(3) `test_csc_data`(2) `test_qa_sync`(2) + 31 个单处：`test_bidi_head_flush` `test_garbled_reask` `test_route_gate_coverage` `test_import_xkt_qa` `test_judge_group` `test_compliant_templates` `test_load_cp_db_reservation` `test_token_dispatch` `test_slot_rendering` `test_qa_cluster` `test_mlx_timing_patch` `test_mock_callee` `test_canned_audio_fixes` `test_publish_pregen` `test_persona_account_scope` `test_learning_ledger_fixes` `test_probe_8khz_asr` `test_branch_action_safety` `test_branch_action_probe` `test_doubao_asr` `test_settle_state_assertion` `test_flow_graph_probe` `test_judge_idle_yield` `test_flow_graph_catchall` `test_probe_intent_mine` `test_probe_backlog_count` `test_node_agent_selfheal` `test_prod_windows` `test_qa_bank` `test_qa_golden` `test_snippet_mining_helpers` + `tests/qa_golden_set.json`
- **部署/安全（4）**：`deploy/cloud/publish_node_pkg.sh`(5) `deploy/cloud/install.sh`(4) `deploy/onprem/cuda-node-bringup.md`(7) `security/mimosa/suppressions.json`(8)
- **活文档（18 + skill，单独一类）**：`docs/REPO_MAP.md`(43) `docs/DELIVERY-MANUAL.md`(35) `AGENTS.md`(32) `docs/LINUX_NODE_TEST_RUNBOOK.md`(13) `docs/RUNTIME_TOPOLOGY.md`(13) `deploy/onprem/cuda-node-bringup.md`(7) `docs/ARCHITECTURE.md`(6) `docs/DEPLOY_SAAS_RUNBOOK.md`(5) `docs/LATENCY_BUDGETS.md`(4) `docs/A_LINE_LOGIC.md`(3) `docs/CUDA-DEPLOY.md`(3) `docs/NODE_PACKAGING.md`(3) `README.md`(2) `docs/OPERATOR_AUDIO_SETUP.md`(2) + 单处 `docs/SCENE_CANVAS.md` `docs/SECURITY_REDTEAM.md` `docs/WINDOWS_CHECKLIST.md` `data/templates/README.md` + `.agents/skills/call-diagnosis/references/probes.md`(10)
- **不改（白名单）**：`docs/superpowers/**`（59 文件 250 处）、`reports/**`、`docs/archive/**`、`.zcode/**`、`.mimosa/**`

**B2 运行时/发行高风险点（搬前先读）**

| 点 | 后果 | 对策 |
|---|---|---|
| `release.yml` smoke 断言 `scripts/install-node.sh` | 发行流水线红 | 同 commit 改断言 |
| `CP main.py` exec `scripts/mock_callee.py` | mock 外呼 404 | 同 commit 改路径 + 保留 404 提示文案里的新路径 |
| `bok.py:3378` marker `"scripts/mock_callee.py"` | 孤儿清扫不认 mock 进程 → 残留 | 同 commit 改（或改成正则匹配 basename） |
| `scripts/artifacts/.p0_supabase_schema.sql` | schema-drift 门禁失效 | **不动** |
| `scripts/cuda/**` | CUDA 部署包引用自身路径 | **整目录不动** |

---

## 附录 C：证据命令（可复现）

```bash
# scripts 普查
ls scripts/*.py scripts/*.sh scripts/*.ps1 scripts/*.spec | wc -l
find scripts -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' -o -name '*.ps1' -o -name '*.spec' \) | xargs wc -l | tail -1
for f in scripts/*.py scripts/*.sh scripts/*.ps1 scripts/*.spec; do \
  printf '%s|%s\n' "$(git log -1 --format=%cs -- "$f")" "$f"; done | sort | cut -d'|' -f1 | cut -c1-7 | uniq -c
# 引用面（每文件被谁引用）
grep -rIl --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=.venv312 --exclude-dir=.zcode <script-name> .
# import 图（in-degree）
python3 - <<'PY'  # 见本文件调研脚本：scripts/*.py 互相 ^(from|import) <name>
PY
# bok.py 解剖
wc -l tools/bok.py; grep -c '^def \|^class ' tools/bok.py
grep -n '^def \|^class ' tools/bok.py            # 簇边界
python3 -c "import re;src=open('tools/bok.py').read();print(len(set(re.findall(r'\"(BOK_[A-Z0-9_]+)\"',src))))"
# 引用 bok.py 的文件数 / 源扫描测试数 / patch 面
grep -rIl 'bok\.py' . --include='*.md' --include='*.py' --include='*.sh' --include='*.yml' | grep -v .git | wc -l
grep -rl '(ROOT / "tools" / "bok.py").read_text\|tools/bok.py").read_text' tests/*.py | wc -l
grep -rhoE 'setattr\(bok, "[A-Za-z_]+"' tests/ | wc -l
# map 陈旧度（基线锚 vs 今日）
git show 78e17e1:apps/agent/agent_runtime/agent.py | sed -n '4640p'
sed -n '5317p' apps/agent/agent_runtime/agent.py        # _register_reply_lane 今日实际位置
git log --oneline 78e17e1..HEAD -- apps/agent/agent_runtime/agent.py | wc -l
grep -oE '[a-z_]+\.py:[0-9]+' docs/ORCHESTRATION-MAP.md | sort -u | wc -l
# 断链
for f in build_release.sh verify_bundle.sh stub_external_bin.sh; do ls scripts/$f 2>/dev/null || echo "MISSING $f"; done
ls -d desktop AGENT.md 2>/dev/null || echo "MISSING desktop/ AGENT.md"
```

---

## 附：本计划成文时的在途状态（不要再踩）

- 工作区 53 条未提交改动（scripts/archive、scripts/artifacts、docs/archive、AGENT.md 删除、根目录 `bok-architecture.*` staged 删除）；
- 本文件是本次任务**唯一**的写入；
- 下一步动作（若批准）：**Mimosa 积压清穿（前置闸，2026-10-04 已拍板严格模式）→ 在途清理落 commit**，再按 **G1a → G3a → R1/R2/R3 → G1b → G1c → G2(W①→W②→W③) → G3b/c** 顺序开波。
