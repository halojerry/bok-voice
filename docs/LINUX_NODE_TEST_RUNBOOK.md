# Linux 实机测试手册（Ubuntu GPU 节点）

> 目标：在一台 Ubuntu + NVIDIA GPU 机器上把 Bok Voice 跑起来，并完成与 mac 主开发机
> **同尺度的健康面 + A 线真栈验收**。接线基线 = PR #137（2026-09-20 Ubuntu 节点形态，
> 拓扑见 [RUNTIME_TOPOLOGY.md](RUNTIME_TOPOLOGY.md) §「Ubuntu 节点形态」，回归钉
> `tests/test_linux_node_wiring.py`）。
>
> **前置结论（plan §33.2 遗留，动手前必读）**：Linux 走 **GGUF/llama.cpp + transformers**，
> mac 走 **MLX**——两侧推理栈不同，而本仓全部延迟基线（[LATENCY_BUDGETS.md](LATENCY_BUDGETS.md)）
> 都在 mlx 上测。**真机跑完延迟探针之前，不得假定两平台等价**；本手册的验收单就是
> 这条基线的采集面。

## 0. 形态判据与两档测试姿势

| 姿势 | 入口 | 适用 |
|---|---|---|
| **A. 源码 dev 档** | `scripts/bootstrap.sh` → `python tools/bok.py serve` | 本手册主用：探针/E2E/改码回归 |
| **B. 节点装机档** | `install-node.sh` 七步制（license/node_token 自举） | 客户真实形态；至少过一遍 |

两档共用同一套平台判定：`platform_key()` 非 Darwin 一律取 `MODELS["windows"]` 表
（llama.cpp GGUF + transformers 后端）；app-data 落 `$XDG_DATA_HOME`（缺省
`~/.local/share`）`/BokVoice`。

```bash
# 形态自检（第 1 分钟就该做的三件事）
python3 tools/bok.py catalog        # 判据：platform: windows（mac 才是 mac）
python3 tools/bok.py doctor         # 判据：platform: Linux (windows)；app-data 在 ~/.local/share/BokVoice
ls ~/.local/share/BokVoice          # 判据：目录可建（旧版 bug=错写 ~/Library）
```

## 1. mac vs Linux 形态差异表

| 维度 | mac | Linux（Ubuntu 节点） | 实测注意 |
|---|---|---|---|
| 模型表键 | `MODELS["mac"]`（MLX 4bit/8bit） | `MODELS["windows"]`：ASR/TTS=HF 原生，LLM=GGUF **只拉 Q4_K_M**（`WINDOWS_LLM_GGUF_PATTERNS`） | `catalog` 输出为准；`llm_4b` 键留空=4B 档未配置，`BOK_LLM_TIER=4b` 会告警回退默认档 |
| LLM :1235 | `mlx_lm.server`（prompt-cache-size/bytes、prefill-step-size 512） | `llama-server --jinja --chat-template-kwargs '{"enable_thinking":false}' --n-gpu-layers all --cache-type-k/v q8_0 --ctx-size 8192` | 启动 flags 硬编码在 `bok.py _start_llm` 非 mac 分支；VRAM 不足时 `--n-gpu-layers all` 会 OOM，看 `logs/llm.log` |
| llama-server 来源 | （无此组件） | 放置顺序：`runtime/llama/llama-server` → `runtime/llama/linux/llama-server` → `runtime/llama-server` → **PATH 兜底**（`bundled_llama()` + `shutil_which`） | 运行时包自带（`build_runtime.sh` Linux 档拉 llama.cpp b10733 ubuntu-x64）；自装档 `apt`/官方 release 装进 PATH 也可，缺任一则 :1235 不起、doctor 如实报 DOWN |
| ASR :8787 | qwen3-ASR MLX 8bit（backend=mlx） | qwen-asr transformers（backend=transformers，`QWEN3_ASR_DEVICE=cuda`（`torch.cuda` 探测）或 cpu） | CUDA 栈由装机第 2 步装 `requirements-runtime-linux-cuda.txt`（已能 import torch 即跳过） |
| TTS :8788 | qwen3-TTS MLX | 表内仍带 tts_preset/tts_clone（transformers 档）**但 `qwen-tts` pip 包不在 Linux 运行时面**（cuda requirements 头注明示「TTS 走 MiniMax 云端」） | 见 §5 坑⑤：先验 :8788 真出声，不可用则 settings `tts.provider=minimax` |
| MT :1236 / settle :1237 | Hy-MT2 / 9B 专线（MLX 专属模型） | **缺**（表无 mt/settle 条目）→ 端口不起，B 线翻译/纪要/flow judge 自动回退 :1235 | 意图挖掘探针见 §4 T6；探针勿打 :1235 与活通话争用 |
| 模型在盘判定 `_model_present` | app-data 布局 + `~/.lmstudio/models` 双布局 | **仅 app-data 布局**（`~/.local/share/BokVoice/models/<repo---id>`） | `doctor` 报 MISSING 先看这里；判据是「目录非空」（`config.json` 语义只用于 mac 布局） |
| app-data 路径 | `~/Library/Application Support/BokVoice` | `$XDG_DATA_HOME`（缺省 `~/.local/share`）`/BokVoice` | 日志=`logs/`、pid=`run/`、模型=`models/` |
| prod 常驻 | launchd plist（RunAtLoad+KeepAlive 无限拉回） | **systemd system unit**（`WantedBy=multi-user.target` + `Restart=on-failure`，RestartSec=10） | 语义精确复刻：退出 75（更新）→拉回上新版；`exit(0)`（root 吊销熔断）→**保持死亡**；勿改 `Restart=always`（会把熔断节点拉活）。`bok.py prod install` 只生成落盘零特权，装载由装机脚本 root 执行（或手工 cp→daemon-reload→enable --now） |
| 运营 env 面 | `_FORWARD_ENV` → plist EnvironmentVariables | 同一张表 → systemd `Environment=` 逐条 | 新增 env 开关的立法动作=在 `_FORWARD_ENV` 加一行（`tests/test_forward_env.py` 门禁）；封闭 env 下不进表=死开关 |
| doctor NVIDIA 门禁 | 无（mac 无此检查） | **无**（门禁只挂 `os.name=="nt"` 的 Windows） | Linux 无 GPU 不会被 doctor 拦——靠装机第 1 步 `nvidia-smi` 报告与 :1235 DOWN 暴露 |
| monitor/孤儿清扫 | ps/lsof + killpg | 同款全可用（POSIX 分支） | A/B 前后 `ps aux | grep agent_runtime` 必须为 0（殭尸 worker 纪律与 mac 相同） |
| 端口面 | 同一张表 | **同一张表**：CP 8000 / ASR 8787 / TTS 8788 / LLM 1235 / b-line 8790 / LiveKit 7880(+7881/7882 RTC) / worker 8081-8083 / web 3000；mt 1236、settle 1237 可选 | 常量单点 `CORE_PORTS`/`WORKER_PORTS`/`PROD_HTTP_CHECKS`（tools/bok.py），装机第 1 步端口预检同清单 |

## 2. 上栈步骤

### 2.0 机器前置

```bash
lsb_release -ds                          # Ubuntu 20.04/22.04/24.04 x86_64（aarch64 无 llama 构建面，勿用）
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader   # 判据：驱动>=550、显存按档（<10GB=min 档）
python3 --version                        # 判据：3.12.x（bootstrap.sh 建 .venv312；无 3.12 先装 deadsnakes 或用系统包）
node --version                           # 判据：>=18（B 线 worker + web dev；运行时包自带可跳）
df -h "$HOME"                            # 判据：模型权重 ~30-60GB 余量
ss -ltn | grep -E ':(3000|7880|7881|7882|8081|8082|8083|8787|8788|8790|1235|1236|1237)\b'   # 判据：空（预期端口全空闲）
```

NVIDIA 驱动 + CUDA 运行库装法（装机脚本不代装）：`sudo apt install nvidia-driver-550-server` 后重启，
`nvidia-smi` 出表即可；torch 用 PyPI cu12x 轮子（`requirements-runtime-linux-cuda.txt` 头注）。

### 2.1 姿势 A：源码 dev 档

```bash
# 国内网络可先导出 PIP_INDEX_URL/PIP_EXTRA_INDEX_URL、HF_ENDPOINT 镜像（脚本原样透传）
./scripts/bootstrap.sh                   # 判据：.venv312 建成、requirements 装完、exit 0
cd services/realtime-translation && npm ci && npm test && cd -   # 判据：node --test 全绿（B 线面）

python3 tools/bok.py download --only asr tts_preset tts_clone llm   # 判据：四项 [ok]；llm 只落 *.Q4_K_M.gguf
python3 tools/bok.py serve               # 判据：`desktop ready: control-plane=8000 asr=8787 tts=8788 llm=1235 b-line=8790`（120s 内）
```

serve 前的两枚 Linux 特有检查（顺序敏感）：

1. **llama-server 可达**：`runtime/llama/llama-server --version` 或 `command -v llama-server`。
   两者皆无 → `_start_llm` 只打一行 stderr 后跳过，:1235 永远 DOWN，serve 120s 超时退出
   （stderr `timeout waiting … still down: [1235]`）。
2. **GGUF 真文件路径**：llama-server `-m` 吃**单个 .gguf 文件**。非打包档 `model_path` 对
   Linux 返回的是 **repo id 字符串**（如 `lukey03/Qwen3.5-9B-abliterated-GGUF`），
   llama-server 不认——上栈第一验（§5 坑②）：起服务前在 web 设置页把「本地 LLM 模型」
   （`settings.llm.local_model`，`resolve_llm_repo` 的最高优先级）填成
   `~/.local/share/BokVoice/models/lukey03--Qwen3.5-9B-abliterated-GGUF/…Q4_K_M.gguf` 绝对路径；
   或临时 `BOK_PACKAGED=1`（走 app-data 目录布局）。**若 :1235 起了但日志报 model not found，
   九成是这条。**

停栈 `python3 tools/bok.py down`；改码重验前先 down 再 serve（殭尸纪律）。

### 2.2 姿势 B：节点装机档（七步制，客户真实形态）

```bash
# 云 CP 已 publish 工件时（交付链零 GitHub；--dry-run 先跑一遍看计划）
curl -fsSL -H "Authorization: Bearer bokn_<key>" \
  https://<云域名>/api/nodes/downloads/bootstrap/latest/bootstrap-node.sh | bash -s -- \
  --cp-url https://<云域名> --license-key bokn_<key> \
  --livekit-url ws://<本机内网IP>:7880 --dry-run     # 零副作用
# 去 --dry-run 正式装；内网多话务员必填 --livekit-url=本机内网 IP（默认 127.0.0.1 只有本机能连房）
```

七步判据（任一步失败即停并报第几步，日志 `~/.local/share/BokVoice/logs/`）：

| 步 | 内容 | 通过判据 |
|---|---|---|
| 1/7 | 体检：GPU/磁盘/端口/CP 可达 | note 无红色；CP `/health` ok |
| 2/7 | venv + CUDA 栈 | `runtime python 已有 torch` 或 pip 装完 exit 0 |
| 3/7 | license 注册 + 心跳探活 | `注册/心跳通过`（节点已在云台可见；坏 key 此步秒失败） |
| 4/7 | 模型选型 + 下载 | 按 VRAM 给档（<10GB=min/10-19=a/≥20=all）；`download --only` 全 [ok] |
| 5/7 | systemd 常驻 | root：`systemctl status bok-node-agent` active (running)；非 root：打印手工装载命令 |
| 6/7 | 拉起全栈 + 六点硬自检 | 控制面/主模型:1235/识别:8787/合成:8788/媒体:7880/工作进程:8081 全过（**不过=装机判失败**） |
| 7/7 | 成功摘要 | 打印话务员入口 `http://<内网IP>:3000` + 管理入口，不含凭据 |

节点媒体/回调寻址（进 systemd 单元 env）：`BOK_LIVEKIT_BIND`（默认=探测内网 IP）、
`BOK_LIVEKIT_WEBHOOK_URL`（默认 `<CP_URL>/api/webhook/livekit`——节点形态必须指云 CP）。
自检/诊断：

```bash
systemctl status bok-node-agent && journalctl -u bok-node-agent -n 50
python3 tools/bok.py status && python3 tools/bok.py doctor
python3 tools/bok.py prod status          # 判据：prod: OK（worker 三件读真 /worker 端点）
```

## 3. 健康面（映射 bok.py 三表，先于一切功能验收）

```bash
curl -fsS http://127.0.0.1:8000/health                             # 判据：含 "ok":true
curl -fsS http://127.0.0.1:8081/worker | python3 -m json.tool       # 判据：agent_name=bok-voice、worker_load 数值（TCP UP≠真注册）
curl -fsS http://127.0.0.1:8082/worker && curl -fsS http://127.0.0.1:8083/worker   # interp-fwd/rev
curl -fsS http://127.0.0.1:1235/v1/models                          # 判据：返回 GGUF 模型 id 列表
curl -fsS http://127.0.0.1:8787/health && curl -fsS http://127.0.0.1:8788/health
python3 tools/bok.py doctor            # 判据：doctor: OK（dev 模式 warnings 不阻断，逐条读）
```

- doctor 的 LLM 功能探针（max_tokens=1 真 prefill）在 Linux 同样适用——`/v1/models` 200 但
  prefill 超时=llama-server wedge/冷页入，重跑一次区分（`BOK_DOCTOR_LLM_PROBE_TIMEOUT_S` 默认 10s）。
- node 心跳面（云 CP）：`curl -H "Authorization: Bearer <root JWT>" $CP_URL/api/nodes`，
  判据：本机节点 online、last_seen 推进。

## 4. 验收清单（映射 plan §48 真栈验收项）

> 全部探针吃 `CONTROL_PLANE_URL` env（缺省 `http://127.0.0.1:8000`）；节点档打云 CP 时
> `CONTROL_PLANE_URL=https://<云域名>` + `BOK_CP_TOKEN=<serve 同值>`。话音刺激合成默认走
> 本地 TTS :8788，本地合成不可用时统一 `BOK_PROBE_STIMULUS=cloud`（云端 MiniMax 单点开关）。
> 离线单测不算验收——以下每条都要真栈出数。

| # | 项 | 命令 | 判据 |
|---|---|---|---|
| T0 | 健康面 | §3 全部命令 | 全绿；`prod: OK` |
| T1 | 打断回归（**两条护栏不得挡真插话**） | `CONTROL_PLANE_URL=… python3 scripts/e2e_barge_in.py` | `interrupted=yes`、stop_ms≈2.4-2.6s、`resumed=yes`（mac 基线 stop 2.4s） |
| T2 | A 线三语真通话 | `E2E_ONLY=cantonese CONTROL_PLANE_URL=… python3 scripts/e2e_trilingual_livekit.py`（再跑 zh/en 腿） | 三语转写/回复语言正确、`sentences>0 canceled=0`；粤语话音句用脚本内置 9 字短句（勿改长句） |
| T3 | 话术分支六腿（A-①② 真栈入口） | `CONTROL_PLANE_URL=… python3 scripts/probe_branch_action.py`（六腿 canned/refuse/handoff/jump/hold/kill） | 逐腿 PASS；kill 腿先 `BOK_BRANCH_ACTION=0` 重启 serve |
| T4 | 缓存纪律（TTFT 归因，**llama-server 档**） | `MLX_URL=http://127.0.0.1:1235/v1 MLX_MODEL=<GGUF 文件名> python3 scripts/probe_cache_discipline.py --turns 8` | 定性三段成立：A 增长尾 TTFT 逐轮变差 / B 恒定尾持平 / C 截断尖峰。探针只允许环回地址（SSRF 白名单），llama-server 本机跑即合规；**`cached_tokens` 读数在 llama-server 的可用性先验一次**，不在则以 llama-server 日志 `promptscached/n_past` 为替代判据 |
| T5 | 延迟 soak（长通话逐轮预算） | `CONTROL_PLANE_URL=… python3 scripts/probe_latency_soak.py` | 正常轮哑 ≥2 = FAIL；p50/p95 对照 [LATENCY_BUDGETS.md](LATENCY_BUDGETS.md) 记录**本平台基线**（mlx 数字勿套用） |
| T6 | 意图挖掘（后台重活专线档） | `BOK_INTENT_MINE_URL=http://127.0.0.1:1237 python3 scripts/probe_intent_mine.py --db <真库副本>` | Linux 现**无 :1237**（settle 专线 MLX 专属）→ 二选一：①临时第二只 llama-server：`runtime/llama/llama-server --port 1237 -m <9B GGUF> --host 127.0.0.1 --n-gpu-layers all`，探针照跑；②本腿记 SKIP。**勿指 :1235**（与活通话争用，即 9B 抢 4B 的实弹事故形态） |
| T7 | B 线同传（可选） | `CONTROL_PLANE_URL=… python3 scripts/e2e_interpret.py` | fwd/rev 译文落库、零丢句；MT :1236 缺 → 回退 :1235 可通但感知 lag 必涨，**只记数不套 3.5s 预算** |
| T8 | offscript 质量（prompt/兜底改动后必跑） | `CONTROL_PLANE_URL=… python3 scripts/probe_offscript_soak.py` | 哑轮 0、质量旗（空答/整句复读）0；实录逐轮人工抽读 |

回归门槛句（与 AGENTS.md 同款）：改动 turns/audit/并发相关后 T1-T3 必跑；垫话/体感改动跑
`probe_filler_timing`（首声 <2.5s 预算）；结果回填 §5 尾部与 plan §33.3 队列
「Linux 节点延迟/质量基线」。

## 5. 已知 Linux 专项坑（按踩中概率排序）

① **两侧推理栈不等价（§33.2 遗留，本手册存在的原因）**：prefill 吞吐/前缀缓存行为/
KV 量化（llama-server 档 `--cache-type-k/v q8_0`）与 mlx 完全不同；`LLM_TTFT_MS cached=N/M`
读数来自 mlx usage 面，llama-server 未必给同名字段——所有延迟结论必须 Linux 实测重采。

② **llama-server `-m` 需要真 GGUF 文件路径**：非打包档 `model_path` 对 Linux 返回 repo id
字符串（`model_path` 的 win dev 档语义），llama-server 不认 repo id——:1235 起不来或
`model not found`。修复姿势：settings `llm.local_model` 填绝对路径（最高优先级）或
`BOK_PACKAGED=1`。**上栈第一验。**

③ **prod env 封闭面三平台同病**：dev `serve` 靠 `_start_proc` merge `os.environ` 全活；
prod（mac=launchd / Windows=schtasks / **Linux=systemd Environment=**）只带白名单。
新增运营 env 的立法动作=在 `tools/bok.py _FORWARD_ENV` 加一行（CI 门禁
`tests/test_forward_env.py` 扫全读取面）。`bok.py prod install --node-agent` 的 systemd
单元 env 透传面当前只有 `BOK_LIVEKIT_BIND`/`BOK_LIVEKIT_WEBHOOK_URL`/`BOK_LLM_TIER` 三枚。

④ **云 CP 节点形态的 worker→CP 断链（接线缺口，测试前必验）**：node_agent 拿 `--cp-url`
只用于心跳/UI 注入，**不翻进 worker env**；`_agent_worker_env` 的 `CONTROL_PLANE_URL`
缺省 `http://127.0.0.1:8000`，而 `cmd_up` 不起本地 CP——云 CP 节点上 turns/QA/设置/主管
上报会打不存在的本地 CP。本机 dev 档（serve 含 CP）不受影响。**验收姿势**：T2/T3 跑完看
CP 侧 turns 行是否落库；不落=踩中。修复候选（待接线，勿在测试机手改）：node_agent
`cmd_up` 前 `os.environ.setdefault("CONTROL_PLANE_URL", args.cp_url)`，或 prod install
`--node-agent` passthrough 表加 `CONTROL_PLANE_URL`/`BOK_CP_TOKEN`。

⑤ **TTS 本地面口径不一**：模型表仍带 tts_preset/tts_clone 且 `cmd_up` 照拉 :8788，但
Linux 运行时未装 `qwen-tts` pip 包（cuda requirements 头注明示「TTS 走 MiniMax 云端」）。
验收：`curl -s http://127.0.0.1:8788/…合成请求` 真出声才可作 T2 的本地刺激源；否则
settings `tts.provider=minimax` + 探针 `BOK_PROBE_STIMULUS=cloud`，并把 :8788 本地合成档
记为 Linux 待补项。

⑥ **doctor 的 NVIDIA 门禁只挂 Windows**（`_doctor_gpu_gate` 见 `os.name=="nt"` 即返）——
Linux 无 GPU/驱动过旧 doctor 不拦；GPU 健康判定以装机第 1 步 + `nvidia-smi` + llm.log 为准。

⑦ **Supabase IPv6（云 CP 侧，节点打云 CP 时的前置）**：pooler 域名 IPv6-only、宿主/容器
无 v6 出网 → CP crash-loop。重启/重拉一律走 `deploy/cloud/up.sh`（池器域名→IPv4 现场解析
双覆盖）；手工姿势见 [DEPLOY_SAAS_RUNBOOK.md](DEPLOY_SAAS_RUNBOOK.md) §重启/重拉铁律。

⑧ **殭尸 worker 纪律与 mac 相同**：`bok.py down` 收 pidfile+ps/lsof 双兜底，但 A/B 前后
仍须 `ps aux | grep agent_runtime` 为 0 再起新栈；他树 stamp（`BOK_SERVE_ROOT`）的进程
永不误杀。

⑨ SIP 边缘（livekit-sip VPS）另有专门脚本 `scripts/deploy_sip_edge.sh` + systemd
单元，不在本手册范围；GPU 节点只做出站连站点，无入站端口需求。

## 6. 结果回填

- 本文件 §4 表格逐行补「Linux 实测值 vs mac 基线」两列再归档；延迟数字进
  [LATENCY_BUDGETS.md](LATENCY_BUDGETS.md) 时**单独开 Linux 档，勿覆盖 mlx 数**。
- 队列对应：plan §33.3「Linux 节点延迟/质量基线」；§4 T6 的 :1237 专线与坑④⑤的接线缺口
  各立一张 ticket 回填 commit 号。
