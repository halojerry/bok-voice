# CUDA 节点实装手册 + 部署问题台账（2026-09-24 首装实录）

实例：117.50.180.185（RTX 4090 48G 魔改 / 容器形态 PID1=tini 无 systemd / Ubuntu 22.04 / 16C94G）。
结论先行：**全栈三语 E2E 3/3 + B 线 8/8 + soak 零哑轮全过**；本台账是路上踩掉的雷与仍开口的项。

## 一、装机流水（复现用）

1. **SSH 免密**：`ssh-keygen -f ~/.ssh/bok_cuda` + `ssh-copy-id -p 23`（expect 喂一次密码）。
2. **源码**：Mac 中转 rsync（箱直连 GitHub 慢；**排除清单必须含 `.venv`**——见问题 #2）。
3. **python**：`conda create -n bok python=3.12`；栈 `pip install -e ... -i 清华`；主 env 补 `huggingface_hub[cli]`（#5）。
4. **llama.cpp 源编**（预编译二进制 GLIBC 2.38 > 22.04 的 2.35，#1）：
   - 工具链全走 pip wheel：`nvidia-cuda-nvcc-cu13 / cuda-runtime / cccl / cmake / ninja`（无 nvcc 机器的正路）。
   - **toolkit shim**（#3）：pip wheel 布局不合 CMake `FindCUDAToolkit`（cudart 在 `lib/` 且只有 `.so.13`）→ `/root/cuda/{bin,lib64,include,nvvm}` 符号链接 + 无版本号开发链接。
   - **版本嵌合体补丁**（#4）：cu13 wheel 自身 nvcc 13.4 + 头 13.0 → 摘 cccl `cuda_toolkit.h` 的一致性 `#error`（仅 minor 差）。
   - `cmake -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 -DCUDAToolkit_ROOT=/root/cuda`，16 并发 ~7 分钟。
   - **箱上成品脚本**：`/root/build_llamacpp_box.sh`、`/root/make_cuda_shim.sh`。
5. **双道起服**（`/root/launch_llm_lanes.sh`）：Q8 回道 :1235（fa on + cache-reuse 256 + -np 6 -c 30720）、Q4 判官道 :1237（-np 3 -c 12288）、**`-rea off` 必须**（#7）。bok serve 健康检查自动让位收编。
6. **sidecar venv**：`services/*-sidecar/.venv` → symlink `/root/bok-venv`（torch cu13 复用省 3G；代价见 #11 版本冲突）。
7. **bok serve**（`/root/serve_box.sh`）：自动下载 ASR/TTS 模型（HF 直连可用且快，~3min/模型）；B 线需先 `npm ci`（#6）。
8. **真库种子**：Mac `sqlite3 .backup` → 换入 `~/.local/share/BokVoice/bok_voice.db`（模板/对象/QA/凭据全套即得）。
9. **验证**：`E2E_ONLY=cantonese e2e_trilingual_livekit.py` → `e2e_interpret.py` → `probe_latency_soak.py`。

## 二、问题台账（已修）

| # | 问题 | 修法 | 落点 |
|---|---|---|---|
| 1 | 预编译 llama.cpp 要 GLIBC 2.38 | 源编（pip wheel 工具链） | 流程 |
| 2 | rsync 漏排 `.venv`——Mac 僵尸 venv 上箱骗死 sidecar 探测（bin/python 断链） | 排除清单补 `.venv`，箱上清掉重 symlink | 流程（本文档） |
| 3 | pip CUDA wheel 布局不合 FindCUDAToolkit | `/root/cuda` shim（lib64 开发链接） | `make_cuda_shim.sh` |
| 4 | cu13 wheel nvcc 13.4 + 头 13.0 嵌合体 | 摘 cccl 一致性 #error | 构建脚本注释 |
| 5 | 主 env 缺 huggingface_hub → cmd_download 死 | 补装（**Linux bootstrap 应自带**=开口项 A） | 流程 |
| 6 | serve 硬等 :8790 超时即退 → LiveKit/worker 没起 | 先 `npm ci`（**开口项 B：serve 对可选服务应软等待**） | 流程 |
| 7 | Qwen3.5 thinking 默认烧 reasoning_content | `-rea off`（bok.py Linux 分支已升级旗标：fa/cache-reuse/-rea/-np） | **tools/bok.py ✅** |
| 8 | TTS sidecar fa2 硬依赖（包缺席=起不来） | fa2 ImportError→SDPA 回落 | **services/qwen3-tts-sidecar/app.py ✅** |
| 9 | `pkill -f "pattern"` 自匹配杀自家 SSH shell（两次神秘断连根因） | `[p]attern` 正则避身——**远程 ops 铁律** | 流程 |
| 10 | FireRedTTS3 webui 音色库下拉服务端不刷新 + 空 transcript 不入列 | 文件对直喂 /voice_clone；转写可用自家 ASR 代办后回填 `voices/*.txt` | 流程 |
| 11 | bok-venv 三家版本冲突（qwen-asr 锁 4.57.6 / qwen-tts 4.57.3 / vllm 要 ≥5.10） | 当前 asr+tts 共存可用，vllm 让位（**开口项 C：正式分 env**） | 台账 |

## 三、开口项（待修/待验）

- **A. Linux bootstrap 脚本**：本手册第一节固化成 `scripts/setup-linux.sh`（含 #5 依赖、node deps、#9 排除清单 rsync 姿势）。
- **B. serve 对可选服务软等待**：:8790 不在时应降级继续起 A 线全家，不该整体退出。
- **C. sidecar 分 env**：asr/tts/vllm 各自 venv，消除版本三方拔河。
- **D. [W10] 首声 p50 2184ms 归因未完**：零哑全过但比 Mac 最佳窗（927ms）差。llama 侧已洗清（prefill 增量 3400tok/s、decode 50tps 满载）；头号嫌疑=**transformers ASR 与全家共卡争抢**。soak 的 `PERCEIVED 无样本` 是测量面缺口（turns perceived_ms 未落？），先修读数再动刀。
- **E. [W12] 热词 context 在 transformers ASR 是否生效未验**（E2E 过≠biasing 在）。
- **F. A 机（43.167.209.254）sshd 握手前掐线**：TCP 22 通、kex 前 close，非密码问题——疑似 fail2ban/安全组源 IP 白名单。**需控制台查**；我的出口 IP `67.159.52.225` 加白即可。
- **G. 平台 SSH 间歇断连**（两台都有）：长任务一律 nohup+日志轮询（已按此姿势执行）。

## 四、箱上常驻（当前）

```
:1235 Q8 回道 (fa/cache-reuse/6槽)   :1237 Q4 判官道 (3槽)
:8000 CP(真库种子) :3000 web :7880 LiveKit 1.9.4 :8081 worker
:8787 ASR transformers+CUDA :8788 TTS transformers+CUDA(SDPA)
:8790 B线 :9000 FireRedTTS3(Gradio)         显存 39.4/49G
```

运维脚本：`/root/serve_box.sh`（全家）、`/root/launch_llm_lanes.sh`（双道）、`/root/build_llamacpp_box.sh`（重编）。重启顺序：双道 → serve。

## 五、第二轮（2026-09-24 晚）：D 项归因闭环 + 云端腿

### D 项归因定案：不是 GPU 争抢，是 ASR sidecar 被 `QWEN3_ASR_DEVICE=cpu` 钉死在 CPU

- **微基准**（`scripts/gpu_contention_probe.py`，腿1/腿2 双向）：llama :1235 TTFT 空闲 p50=104ms、ASR 持续解码中 p50=94ms——**4090 上 ASR+LLM 同卡零争抢**（原假设否决）。
- 真凶：重启后的栈里 ASR sidecar env `QWEN3_ASR_DEVICE=cpu`（部署时被显式钉死），CPU fp32 解码 → `finish` p50=1330ms / p90=2450ms / max=4404ms（n=38，含当天真通话）。
- **修复=重启 ASR sidecar env 改 `cuda`**（模型 bf16 上卡 ~4.9GB；全家显存 44.6/49.1G）：finish 稳态 **237-285ms**（5.3 倍），冷首发 ~1.6s（CUDA warmup，可忽略）。
- **端到端实证**：soak-canto 本地腿首声 p50 2184ms → **1249ms**（p95 1472ms，零哑零超标），全改善来自此一改。对照 Mac：ASR finish 130ms（mlx 8bit）vs 箱 250ms——ASR 段 Mac 仍快 ~120ms；llama TTFT 箱 104ms（cached）完胜。Mac 整体最佳窗 927ms 仍领先，剩余差=ASR 段 + 管道零头。

### D 项测量缺口根因（PERCEIVED 无样本）：erc LOG_PATH 平台硬编码

`scripts/e2e_real_customer.py` LOG_PATH 硬编码 `~/Library/Application Support/...`（macOS），Linux 上恒不存在 → soak 日志窗口整段空转（PERCEIVED/哨兵全空）。**已修**：`_default_log_dir()`（env `BOK_LOG_DIR` > Darwin 库目录 > Linux XDG vault）。修复后 canto 云腿 PERCEIVED n=6 p50 1697ms p95 2494ms 正常出数。turns 表 perceived_ms 本来就落（1328/1307/726/1292ms 实查）——不是 agent 侧缺口。

### 云端腿（MiniMax LLM+TTS 全云，本地 ASR/VAD）

- **端点定案**：`https://api.minimax.cn/v1/text/chatcompletion_v2`（同平台 key，DB tts_json.api_key 直用；`/api/settings?internal=1` 对匿名会掩 key——拿真 key 读 DB 列）。M2/M2.5 系思考关不掉（reasoning_content 恒先流，三种参数拼写无效）→ **abab6.5s-chat**（非思考，首 content token 447ms，SSE chunk OpenAI-delta 兼容）。
- **接线**：`scripts/mm_llm_shim.py` :1236（OpenAI `/v1/chat/completions` → chatcompletion_v2 透传，SSE 补 [DONE]，mlx 专属字段剥除，SSRF 护栏钉死上游域名）；A 线 worker env `MLX_LLM_BASE_URL=http://127.0.0.1:1236/v1` + `MLX_LLM_MODEL=abab6.5s-chat` + `BOK_PREFILL_SPEC=0 LLM_PREFIX_PREWARM=0`（**预热线上云=烧钱，必关**）。
- **数字**（修复 ASR 后同一栈，soak 三语云腿）：首声 p50 zh 1450 / canto 1561 / en 1389ms；**云 TTFT 675-853ms 且 `cached=0`**（无前缀缓存，每轮全量 prompt 重算）vs 本地 104ms cached。
- **邀约本体腿**（invite-zh/canto/en，钉邀约模板，全云）：p50 1145/1472/1520ms，零哑零缺答，三语 WhatsApp/微信捕获全中；但每通 1-3 发超标（最高 6.1s）。
- **胖尾真凶=TTS 不是 LLM**：PERCEIVED 分段实锤——本地腿慢轮 `total=4327 (eou=730 llm=327 tts=3270)`、en 云腿 `total=3525 (llm=1046 tts=1896)`；**MiniMax t2a 首包偶发 1.9-3.3s**（典型轮 tts=0-870ms），LLM 段本地 179-392ms 稳如老狗。**下一个延迟杠杆=TTS 供应商/本地化 A/B**（正是 TTS 三路决策的输入）。邀约腿六步未走满（template_step 最高 4，7 轮体量只推进到中段；TTFT 测量不受影响）。
- **邀约话术本体腿**：三语「延保服务回访邀约」模板（`scripts/seed_invite_templates.py`，中性域无赔偿；EN 分支须客户面话术，coach 祈使句会被 CP `en_coach_head` 验证拒 400）+ soak 新场景 `invite-zh/canto/en`（配合型六步）+ `--template-id` 直通口（erc.create_call 新参，跳过自动挑模板）。

### 箱上状态更新

- 14:07 起全家=孤儿进程（bok.py serve 等 :3000 超时退出=开口项 B 又添一证），无 monitor；ASR 已 CUDA、其余不动。VAULT_ROOT 生产姿态（DB 全量在 `~/.local/share/BokVoice/`，**key 在 DB settings 列**）。
- 本轮测试后 **worker 仍指云腿 shim :1236**——回本地腿需重启 worker（env 快照在 /proc，MLX_LLM_BASE_URL 还原 :1235 即可）。
- 双发种子模板清理欠账：`d6c2cca17257`/`61ee6bb71cec`（seed 脚本首跑建、复用判断失效建的重复邀约模板）待删。
