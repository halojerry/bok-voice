# CUDA 部署包（阶段 D 开窗预置）

> 2026-09-25 预置、开窗实跑。目标：CUDA 服务器一键部署包——阶段 D
> （[verified-execution-plan](superpowers/plans/2026-09-26-verified-execution-plan.md) §4）
> 的部署工程全部提前做好，实机开窗时按本文跑脚本即可，不现场排坑。
> **本文所有「⚠未实测」条目汇总在 §8**——写包时没有 CUDA 机器，
> 未经真机验证的环节一律明标。

---

## 1. 包内容与拓扑

```
scripts/cuda/
├── bootstrap.sh          预检 + venv + sglang 安装 + 模型下载 + 单元渲染（不写 /etc）
├── models.env.example    模型清单（repo 全部 TODO 占位，开窗时按选型定稿）
├── env.example           /etc/bok-cuda/bok-cuda.env 模板（凭据/端口/车道指向，逐键注释）
├── smoke.sh              开窗验证清单（GPU/两车道探活/priority/radix/节点/可选 e2e）
├── uninstall.sh          对称回滚（模型与 env 缺省保留）
├── bok-node-start.sh     bok-node 单元的 ExecStart 包装（凭据不进单元文件）
└── units/
    ├── sglang-a.service      a_reply 车道 :18100（priority scheduling + radix）
    ├── sglang-judge.service  judge/settle/mining 车道 :18101（同族旗标）
    └── bok-node.service      节点守护（心跳/注册；Restart=on-failure 有讲究，勿改）
```

拓扑：CUDA 机 = **推理服务器**（两实例 sglang + 节点注册心跳）；worker/LiveKit/ASR/TTS
留在 Mac 或云节点，经云 CP 路由表把 a_reply/judge 车道跨机指到 CUDA 机
（§6）。本机全栈形态（node_agent cmd_up 拉全家）代码位已留（`BOK_NODE_FULL_STACK=1`）
但 ⚠未实测，首窗推荐 heartbeat-only。

端口与本地区分（Mac 栈车道 :1235/:1237 原样不动）：**:18100 = a_reply，:18101 = judge/settle**。

---

## 2. 问题清单（已知坑全列，开窗前先读）

### 2.1 driver / CUDA 版本门槛——**550 不够，要 580**

任务草案写「driver≥550、CUDA≥12.4」，**已按检索事实修正**：sglang 当前稳定版
0.5.20（2026-09-18）的 PyPI 车道只发 CUDA 13 轮子；0.5.19（2026-09-04）是最后一个带
CUDA 12（cu129）车道的版本，且其轮子索引已随 CUDA 12 退役。CUDA 13 是新的大版本族，
最低 driver 即 **580.65**（r580 分支；550 只够 CUDA 12.x 小版本兼容）。
bootstrap 预检按此钉：driver < 580 直接人话报错退出。

- 升级 driver 到 ≥580 是正解；「SGLANG_VERSION=0.5.19 保旧 driver」理论上存在
  （最后 cu12 车道），但轮子索引已退役，**不要指望**，文档不提供自动化路径。
- 预检同时核 `nvidia-smi` 表头 CUDA Version（= driver 支持的最高 toolkit）≥ 13.0。

### 2.2 sglang 旗标随版本漂移——实机首跑以 `--help` 为准

本包全部旗标逐一核对过 **v0.5.19 源码（python/sglang/srt/server_args.py、
schedule_policy.py、entrypoints/openai/protocol.py）** 与 **0.5.20 文档
（docs.sglang.io → advanced_features/server_arguments）**，两代旗标名一致。
开窗时装完先跑一句再 enable 单元：

```bash
~/bok-cuda/venv/bin/python -m sglang.launch_server --help | grep -E "schedule|priority|radix"
```

**任务草案旗标名勘误**（写包时逐一查证过）：

| 草案写的 | 实际旗标 | 说明 |
|---|---|---|
| `--schedule-priority` | `--enable-priority-scheduling` | 请求级优先级调度总开关；请求体 `"priority": N` 整数，**高值先调度**；另配 `--priority-scheduling-preemption-threshold`（默认 10，抢占所需最小优先级差）、`--schedule-low-priority-values-first`（反向，勿开）、`--default-priority-value` |
| `--load-balance-method lpm` | `--schedule-policy lpm` | `--load-balance-method` 是 **sgl-router**（独立负载均衡路由器）的旗标，不是引擎 serve 的；单实例调度用 `--schedule-policy`（取值 lpm/fcfs/lof/…，默认 fcfs） |
| `--prefix-cache-radar radix` | （无需旗标） | RadixAttention **默认开启**；没有这个旗标。关闭口是 `--disable-radix-cache`（勿加）；逐出策略 `--radix-eviction-policy`（默认 lru，可选 lfu/slru/priority） |

**lpm 与 priority 的取舍（源码核实，schedule_policy.py `calc_priority`）**：
`fcfs` 档排队 =「priority 再先到先到」——priority 排队真生效（G5 主姿势）；
`lpm` 档排队 = 最长前缀匹配优先，**priority 在排队序里不生效**、只管抢占与缩退。
radix 缓存命中两种档下都工作（lpm 只改排队顺序，不改命中）。env 文件里
`SGLANG_SCHEDULE_POLICY` / `SGLANG_SCHEDULE_POLICY_JUDGE` 两实例可各自取值：
首窗双 fcfs，战役并发档再试 lpm 对比。

**请求 priority 的客户端接线现状**：sglang OpenAI 兼容口接受 `priority` 整数字段
（protocol.py ChatCompletionRequest 已核实）。但 agent 侧「回复轮带高优、
speculator 预热/judge 带低优」的逐请求接线是**开窗待办**（apps/agent 改动，不在本包
范围）——服务端旗标就位后，全体请求默认 priority=0，priority 调度处于「上膛未击发」
状态。§5 验收的 priority A/B 用 smoke.sh 的 curl 探针（显式带 priority 字段）先证明
通道，真收益判定等接线。

### 2.3 HF 网络与镜像

- 一切 HF 下载走 `HF_ENDPOINT=https://hf-mirror.com`（env 缺省，可覆盖回官方）；
  国内机**直连 huggingface.co 会卡死**，这不是玄学是既定事实。
- pip 同理：`PIP_INDEX_URL` 缺省清华镜像，可覆盖。
- 下载用 huggingface_hub 的 `snapshot_download`（bootstrap 内嵌），不用 CLI 子命令
  （`huggingface-cli`→`hf` 改名漂移，API 稳）。gated 仓库才需要 `HF_TOKEN`。

### 2.4 sglang 吃 HF safetensors，**不吃 MLX / GGUF**

仓内模型表（tools/bok.py `MODELS`）mac 档是 MLX 4bit、windows 档是 llama.cpp GGUF
——都不能给 sglang 用。models.env.example 的 repo 全部留 TODO 占位，开窗时按当时
选型定稿（9B 级 Qwen3.5 系 safetensors；Mac 现役 Huihui-9B 要找它的 HF 格式版），
**勿照抄仓内 repo id，勿臆造仓库名**。量化（fp8/awq/gptq）经 `--quantization` 旗标
声明（单元文件里留了注释行）。

### 2.5 显存预算表（⚠估算，实机以 sglang 启动日志实际分配为准）

预算池 = `--mem-fraction-static` × 卡总显存（权重 + KV cache 共池；OOM 第一道
下调钮就是把这个值从 0.85 往下拧）。

| 配置 | 权重 | 余给 KV/激活 | 适用卡 |
|---|---|---|---|
| 9B × 1 bf16 | ~18G | 视卡而定 | 24G 卡可跑（KV 薄，单并发余量） |
| 9B × 1 fp8 | ~9-10G | 充裕 | 24G 卡舒适 |
| 9B × 2 bf16（本包双实例） | ~36G | 极薄 | **48G 卡勉强**（A6000 / RTX 6000 Ada / L40S） |
| 9B × 2 fp8（首窗推荐） | ~20G | ~20G+ | 48G 卡舒适；80G 卡（A100/H100）宽裕 |

KV 单价参考（Qwen3 系 9B 级 GQA：36 层 × 8 KV 头，bf16）：≈0.3MB/token，32k 上下文
≈10G/**并发路**——多路并发时 KV 池是第一瓶颈。磁盘账：9B bf16 单模型 ~18-20G，
双模型 ~40G；加 sglang 轮子 ~10G、缓存日志、TTS SFT（§2.7，~7G/epoch）后
200G 预检线是底线。

### 2.6 与 Mac 栈并存：端口 / 身份 / 凭据分离

- **端口**：CUDA 包用 :18100/:18101，与 Mac 的 :1235/:1237 有意错开——两台机器
  同时在线时排查互不干扰，云 CP 路由表里也一眼可辨。
- **节点身份**：节点指纹 = sha256(/etc/machine-id)（Linux），与 Mac 的
  IOPlatformSerialNumber 指纹天然不同——在云台 nodes 页**单独注册/绑定一个节点身份**，
  不与 Mac 节点混用。加固模式（auth-on 云 CP）需要 root 先按指纹预绑定：
  `sha256sum /etc/machine-id` 预取。license 流重装/重启幂等复用 node_id
  （token 落 ~/.bok/node-state.json，0600）。
- **凭据**：BOK_CP_TOKEN / license key 只进 /etc/bok-cuda/bok-cuda.env（600），
  零密钥字面量进单元文件或脚本（包装脚本 bok-node-start.sh 负责把 env 翻译成 argv）。
- sglang 自身暴露到不可信网络时启用 `--api-key`（单元注释行 + `SGLANG_API_KEY`）；
  带 key 部署的消费方走云 CP 路由卡的 api_key 字段（env 缺省链只有
  FLOW_JUDGE_LLM_API_KEY 一个 key 通道，a_reply 走 env 链时塞不了 key）。

### 2.7 TTS SFT 依赖（阶段 C 并窗）

CUDA 窗口的 TTS SFT（真人感音色微调）**不随本包携带**——命令与纪律在
docs/TTS-SFT-PLAYBOOK.md §3，前置是阶段 C 的数据就绪（每语言 10-30 分钟/音色、
人工校对完）。本包只管把磁盘余量留够（7G/epoch）和预检线（200G）算进账。
SFT 产物回部署链：convert 8bit → 换模型目录 → 重跑 tts-pregen → 上线试听
（Mac 侧动作，与 sglang 无关）。

### 2.8 两实例 vs 单实例合并档

本包按任务定案出**两实例**（a_reply :18100 / judge:settle :18101）：进程分边界 =
权重显存隔离（同 Mac 姿势），但 **priority 只在实例内生效**——跨车道的调度公平性
靠进程隔离近似。替代形态「单实例合并档」（一颗 9B 同时服两车道、请求 priority
真正跨车道分层，才是 G5 的完整解）等双实例数据出来后按显存/质量账另立单元，
本包 units/ 命名与端口已为此留好位（复用同一份 env 文件即可加实例）。

---

## 3. 一键流程（开窗当日按序执行）

```bash
# 0) 前提（一次性）：Ubuntu 22.04/24.04 + NVIDIA driver ≥580 + 仓库检出/解包
#    git clone <repo> ~/bok-voice   # 或发行包；bootstrap 只要 scripts/cuda/ 在场

# 1) 选型定稿（唯一需要人工决策的环节）
cp ~/bok-voice/scripts/cuda/models.env.example ~/bok-voice/scripts/cuda/models.env
$EDITOR ~/bok-voice/scripts/cuda/models.env    # 填 MODEL_*_REPO（见 §2.4）

# 2) 一键安装（预检 → venv → sglang 0.5.20 → 渲染单元 → 下模型 → 打印指引）
cd ~/bok-voice/scripts/cuda && ./bootstrap.sh
#    只补模型：./bootstrap.sh --models-only；跳过模型：--skip-models

# 3) env 填值 + 单元装载（root；bootstrap 永不写 /etc）
sudo install -d -m 700 /etc/bok-cuda
sudo cp ~/bok-cuda/env.rendered.example /etc/bok-cuda/bok-cuda.env
sudo $EDITOR /etc/bok-cuda/bok-cuda.env        # BOK_CP_URL / license / 节点名必填
sudo chmod 600 /etc/bok-cuda/bok-cuda.env
sudo cp ~/bok-cuda/units/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sglang-a.service sglang-judge.service bok-node.service
#    首启 = 权重加载，journalctl -u sglang-a -f 看到监听端口才算就绪（⚠首启时长未实测，
#    预期几十秒到几分钟量级，取决于模型尺寸与盘速）

# 4) 开窗验证清单
cd ~/bok-voice/scripts/cuda && ./smoke.sh            # 六步硬门禁 + 两个信息位
#    全栈在跑时追加端到端一通：./smoke.sh --e2e

# 5) 接入云 CP（root，web 设置页路由卡）
#    a_reply  → provider=openai, base_url=http://<CUDA内网IP>:18100/v1, model=a-reply
#    judge    → provider=openai, base_url=http://<CUDA内网IP>:18101/v1, model=judge-9b
#    settle/mining 想并车指 :18101 同式。改道下一通生效、零重启（§6）。

# 6) 验收判据表（§5）跑数；不达标回退 = 路由卡切回 local 档（§7）
```

回滚：`./uninstall.sh`（缺省保留模型与 env；`--purge-models --purge-env` 全清）。
Mac 栈零依赖本包——路由不切回，CUDA 机下线只影响指过去的车道（见 §7 回退链）。

---

## 4. 模型路由热切换链路（为什么是「下一通生效」）

消费点在 agent 装配（apps/agent/agent_runtime/agent.py，`_routing_raw` 当通局部值）：
worker 每通建单时 `get_settings()` 拉 CP 设置，`model_routing_json` 顶层键经
`resolve_route(lane, env, routing_raw)` 解析五车道（a_reply/judge/mt/settle/mining），
**worker 并发多通不串线**；路由表 openai 档的下发字段 = base_url + model + api_key +
enable_thinking。所以：

1. root 在路由卡改道保存 → CP settings 即时可见；
2. 在途通话不受影响（车道解析在装配时一次钉死，话术快照同款隔离哲学）；
3. 下一通建单起，新车道生效，**零重启**——这是回归 Mac 的回退通道（§7）；
4. agent 侧 env 缺省链（`MLX_LLM_BASE_URL` 等）是路由表缺省/kill-switch
   （`BOK_MODEL_ROUTING=0` 全表失效）时的兜底，**env 文件不要设 BOK_MODEL_ROUTING**。

## 5. 验收判据表（阶段 D 口径）

| # | 判据 | 量法 | 线 |
|---|---|---|---|
| A1 | 端到端感知延迟 | `smoke.sh --e2e`（probe_latency_soak，headline=PERCEIVED p50） | **p50 感知 ≤1.2s**（阶段 D 目标；探针缺省预算 3000ms 须显式收紧） |
| A2 | priority A/B 显效 | smoke.sh [5/6] 信息位起步；接线后跑饱和压制对照 | 压制中高优请求时延 ≈ 无竞争基线（显著劣化=调度未分层） |
| A3 | 9B 切档判据 | probe_offscript_soak（真栈 50 轮）双车道对照 4B/9B | 质量差值（offscript 空答/复读旗）值得速度差的结论出数 |
| A4 | radix 命中 | smoke.sh [4/6] + sglang 日志 cached_tokens；多路并发用 load_audio_concurrency | 同模板跨通 KV 复用生效（日志 cached 增长） |
| A5 | 节点在册 | 云台 nodes 页看到 cuda 节点 + 心跳；smoke.sh [6/6] | 节点 active、心跳 60s 周期无断 |
| A6 | 全栈回归 | `scripts/e2e/e2e_barge_in.py`（真打断硬门槛）+ `e2e/e2e_trilingual_livekit.py` | 与 Mac 基线同绿（车道换了，漏斗语义不许变） |

## 6. sglang 旗标依据（版本存档）

- **版本钉**：`SGLANG_VERSION=0.5.20`（当前稳定，2026-09-18；0.5.19 = 2026-09-04，
  最后的 CUDA 12 车道）。
- **文档**：docs.sglang.io → advanced_features/server_arguments（0.5.20 线；
  priority scheduling 家族、schedule-policy 取值、radix eviction）。
- **源码核对**（v0.5.19 tag，与 0.5.20 文档一致）：`server_args.py`
  （schedule_policy/enable_priority_scheduling/priority_scheduling_preemption_threshold/
  retraction_policy/radix_eviction_policy/disable_radix_cache/mem_fraction_static/
  chunked_prefill_size）、`schedule_policy.py`（fcfs+lpm 与 priority 的排队语义）、
  `entrypoints/openai/protocol.py`（ChatCompletionRequest.priority 字段）、
  `pyproject.toml`（extras 清单——**不存在 `[sampler]`**，裸装 sglang 即运行时；
  Python ≥3.10；torch 2.13）。
- **平台事实**：0.5.20 起 PyPI 车道 = CUDA 13；cu129 轮子/镜像退役（0.5.19 最后一班）；
  CUDA 13.0 最低 driver 580.65。

## 7. 回退链（出问题往回退的顺序）

1. **车道回退**：云 CP 路由卡把 a_reply/judge 切回 local（或清空条目）→ 下一通生效
   零重启；CUDA 机不用动。
2. **实例回退**：`systemctl stop sglang-judge`（judge/settle 自动回退主车道，与 Mac
   :1237 缺失同语义）；A 车道实例停了路由必须先回退，否则回复轮打空。
3. **整机下线**：`./uninstall.sh`（模型与 env 保留，下次开窗复用）。
4. **kill-switch 底牌**：worker env `BOK_MODEL_ROUTING=0` 全表失效退 env 缺省链
   （与路由卡无关的最后一道，正常用 §7.1 即可）。

## 8. ⚠未实测清单（本包全部未经真机验证的环节）

写包环境无 CUDA 机器，以下每一条都是**首次开窗必须现场核对的点**：

1. **bootstrap 预检解析**：`nvidia-smi --query-gpu=driver_version` 与表头
   `CUDA Version` 的 sed 提取在真表头上的输出形状（写包时按文档格式推演，未跑真机）。
2. **pip 装 sglang==0.5.20 全链**：torch 2.13 + flashinfer 等依赖解析时长与磁盘峰值
   （~10G+）、镜像站同步延迟（清华镜像缺新轮子时须切官方 index——脚本参数已留）。
3. **模型下载量级**：models.env 的 repo 定稿后 snapshot_download 实际体积/时长
   （hf-mirror 在真机上的吞吐）。
4. **两实例首启时长与显存实测**：权重加载墙钟、`--mem-fraction-static 0.85/0.80`
   在目标卡上是否 OOM（尤其 48G 卡跑 bf16×2）、双实例同卡是否互相挤兑。
5. **systemd 单元装载**：单元文件的 `$VAR` 运行时展开（EnvironmentFile → ExecStart
   参数位）在目标 systemd 版本上的行为（已按 systemd 文档写，未在真机 enable 过）；
   包括 sglang 对空参/缺参时的报错形态。
6. **旗标组合实跑**：`--enable-priority-scheduling + --schedule-policy fcfs +
   --retraction-policy priority` 的实跑日志形态、`priority` 字段在
   /v1/chat/completions 上的接受性（源码核实过字段存在，未实弹 POST 过）。
7. **smoke.sh [4/6][5/6]**：radix 双发测时与饱和压制 A/B 的数值形状（信息位判读
   文案是否够用，真机上可能要调 sleep 与 max_tokens）。
8. **bok-node 注册链**：heartbeat-only 形态对加固模式云 CP（auth-on + 指纹预绑定）
   的注册成功率；license 流 token 状态文件落盘与心跳周期。
9. **--e2e 端到端**：probe_latency_soak 在「CUDA 车道 + Mac worker」跨机拓扑下的
   PERCEIVED 口径（跨机 RTT 计入 llm 段，判据线可能要按内网 RTT 重校）。
10. **TTS SFT 并窗**：数据就绪前的命令未排进本包（PLAYBOOK §3 拥有真值），
    磁盘余量按 7G/epoch 预检，实跑未验。
11. **全栈节点形态**：`BOK_NODE_FULL_STACK=1`（cmd_up 拉全家）在本包 env 面上的
    完整跑通——首窗明确不做，代码位留好。
12. **卸载对称性**：uninstall.sh 在真机上 disable/remove/daemon-reload 的干净程度
    （rmdir 非空目录的保留分支）。

> 开窗纪律：每跑完一步把「实际现象 vs 本文写法」的差异记回来改文档——
> 本包的价值在第二次开窗时零排坑。
