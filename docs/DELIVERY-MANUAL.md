# Bok Voice 交付说明书 + 功能使用手册

| 项 | 值 |
|---|---|
| 文档版本 | v2026-09-26 |
| 适用 commit | main 分支（基线 `ac286c4` 起，含同轮落库的 qa_bank.py / prep_tts_dataset.py / 演示档话风收紧） |
| 读者 | ①交付/部署工程师 ②客户侧运营（话务主管）与运维（admin/root） |
| 配套手册 | `docs/USER-GUIDE.md`（使用说明书·按日常场景）｜`docs/UI-GUIDE.md`（UI 使用说明书·逐页图解，含 19 张真机截图 docs/assets/ui/） |
| 事实来源 | 全部参数/命令/权限取自仓库代码与文档；标注路径处**以该路径代码为准** |

> 两句话认识产品：**A 线**=本地优先的实时语音客服（话术分步推进 → ASR 转写 → 本地 LLM 回复 → 云端 TTS 出声）；
> **B 线**=实时同声传译工作台（双语进房、源语言→目标语言句级字幕+译音）。
> 拓扑权威文档：`docs/RUNTIME_TOPOLOGY.md`（安装后能否正常使用的唯一验收基准）。

---

# 第一部 交付（给工程师）

## 1. 产品与拓扑概览

**两种部署形态**（`docs/RUNTIME_TOPOLOGY.md` §0）：

| 形态 | 组成 | 适用 |
|---|---|---|
| 单机全家桶 | 一台 Mac/Win/Linux 机器跑全部组件（`python tools/bok.py serve`） | 单站点/演示/小型客户 |
| 分发型 | 云端 CP（同一 control-plane 代码，`DATABASE_URL` 指 Supabase Postgres，托管管理台静态 UI）+ 客户节点（`tools/node_agent.py` 心跳/commands/UI :3000，拉起本机全栈） | 多节点 SaaS 交付 |

单机组件与端口（常量表单点 `CORE_PORTS`/`WORKER_PORTS`/`PROD_HTTP_CHECKS`，tools/bok.py:507-531）：

| 组件 | 端口 | 职责 |
|---|---|---|
| control-plane | :8000 | 业务 API/设置/审计/token（apps/control-plane） |
| ASR sidecar | :8787 | 三语转写 zh/cantonese/en（services/） |
| TTS sidecar | :8788 | 本地合成/克隆（当前生产线 TTS=MiniMax 云，:8788 为回退/bench） |
| LLM | :1235 | A 线对话主模型（Mac=mlx_lm；Win/Linux=llama.cpp GGUF） |
| MT LLM（可选） | :1236 | B 线同传专用翻译（Hy-MT2；缺失自动回退 :1235） |
| settle-llm（可选） | :1237 | 后台重活 9B（纪要/judge；缺盘回退 :1235） |
| embedding（可选） | :8789 | 意图语义车道（bge-m3；缺失回退关键词双车道） |
| LiveKit | :7880 | RTC 信令与媒体 |
| agent worker | :8081 | A 线智能体（健康端点 `GET /worker`） |
| interp worker ×2 | :8082 / :8083 | B 线 fwd/rev 同传 worker |
| realtime 演示 worker（可选） | :8084 | 云端 Realtime S2S 演示档（`BOK_QWEN_REALTIME=1` 才拉起） |
| node-agent UI | :3000 | 坐席浏览器 UI（node_agent `--ui-dir` 自动托管） |

**常见坑**：三个 livekit-agents worker 端口必须显式分拆（8081/8082/8083），混绑会 `Errno 48` 崩溃（tools/bok.py `_worker_specs`；AGENTS.md「agent worker 端口必须显式分」）。

## 2. 环境要求

（README.md「硬件要求」表 + docs/RUNTIME_TOPOLOGY.md）

| 平台 | 要求 |
|---|---|
| macOS | Apple Silicon（M 系列），建议内存 ≥32GB |
| Windows | NVIDIA GPU（CUDA 12.4、驱动 ≥550、显存 ≥8GB），无 CPU 兜底 |
| Linux/Ubuntu 节点 | systemd 常驻；VRAM 分档选型（<10GB=min / 10-19GB=a / ≥20GB=all，`scripts/install-node.sh`） |

模型体积（README.md）：首启下载约 13GB（全家桶档）/ 5.6GB（基础档）。数据全部落本机 app-data（见 §7）。

**单机并发梯队（2026-09-25 实测，M4 Pro/48G，AGENTS.md「并发/边界回归资产」条）**：
A 线 2 路≈免费；4 路=悬崖（回复 p50 明显劣化）；≥4A+2B 混跑曾触发 WindowServer GPU 饿死（屏幕冻结）。
**演示场景建议 A 线 ≤2-3 路**；扩容=第二台 Mac 起同栈或 CUDA 服务器（Mac 本机 vLLM 跑不了）。
LM Studio(:1234) 驻留模型是额外 GPU 租户，演示前卸载。

**平台模型表速览**（tools/bok.py `MODELS`，repo id 以代码为准）：

| 键 | Mac（mlx 栈） | Windows/Linux（GGUF/transformers） |
|---|---|---|
| asr | aufklarer/Qwen3-ASR-1.7B-MLX-8bit | Qwen/Qwen3-ASR-1.7B |
| tts_preset / tts_clone | Qwen3-TTS-12Hz-1.7B CustomVoice/Base 8bit | Qwen/Qwen3-TTS-12Hz-1.7B CustomVoice/Base |
| llm | avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit | lukey03/Qwen3.5-9B-abliterated-GGUF（只下 Q4_K_M） |
| llm_4b | —（Linux 节点档位键，空=未配置） | 运维选定 GGUF 权重后填此键生效 |
| mt / settle / embedding | Hy-MT2-1.8B / Huihui-Qwen3.5-9B / bge-m3（均可选档） | 非空才被选型/下载/启动识别 |

> ASR 维持 8bit 是刻意决策：4bit 快 ~24% 但数字路径同音字滑失（九→狗/號→后），触碰「WhatsApp 捕获数字零降级」铁律（tools/bok.py MODELS 注释）。

## 3. 安装

```bash
# 1) Python 依赖（建 .venv312，Python ≥3.11）
./scripts/bootstrap.sh

# 2) 模型下载（幂等、断点续传；Mac 开发机也可用 ~/.lmstudio/models 既有模型）
python tools/bok.py download            # 全量（按平台表 tools/bok.py MODELS）
python tools/bok.py download --only asr llm   # 子集（键见命令帮助：asr/tts_preset/tts_clone/llm/llm_4b/mt/settle）
python tools/bok.py setup status        # 查看在盘/缺失（first-run 向导同一通道）

# 3) Web 管理台构建（静态导出）
cd apps/web && npm ci && npm run build
```

模型表要点（tools/bok.py `MODELS`，以代码为准）：
- Mac（mlx 栈）：ASR=Qwen3-ASR-1.7B-MLX-8bit；LLM=Qwen3.5-4B（4bit）；MT=Hy-MT2-1.8B；settle=Huihui-Qwen3.5-9B；embedding=bge-m3。
- Win/Linux（`platform_key()` 仅 Darwin=mac）：ASR/TTS 走 transformers，LLM=GGUF（只下 Q4_K_M）；`llm_4b` 档位键留空=未配置，`BOK_LLM_TIER=4b` 且表内非空才生效。
- 可选模型 `OPTIONAL_MODELS = {mt, settle, embedding}`：缺失不门禁，对应功能自动回退（B 线 MT 回退 :1235、judge/纪要回退 :1235、语义车道回退关键词）。
- Mac 模型解析顺序（tools/bok.py `model_path`）：打包模式=app-data/models；开发机先 `~/.lmstudio/models`、后 app-data——两边哪边「真有 config.json 的可用模型」用哪边。

## 4. 启动与常驻

**开发/演示前台**：

```bash
python tools/bok.py serve     # 拉起全栈（启动顺序见 docs/RUNTIME_TOPOLOGY.md §3）
python tools/bok.py status    # 七项端口 UP/DOWN + worker /worker 真探针
python tools/bok.py down      # 按 pid 文件收栈
python tools/bok.py doctor    # 体检（打包形态 --packaged 为硬门禁；节点装在树上用 doctor --packaged）
```

`serve` 启动顺序（docs/RUNTIME_TOPOLOGY.md §3，失败即非零退出不静默继续）：
1. 确保 app-data 目录（run/logs/models/vault）
2. control-plane :8000（注入 `DATABASE_URL=sqlite:///<app-data>/bok_voice.db`、`VAULT_ROOT`）
3. LiveKit :7880（内嵌二进制 + livekit.yaml，不依赖 Docker）
4. ASR :8787 / TTS :8788 / LLM :1235 并行拉起
5. agent worker（注册到 :7880）→ 轮询全部端口 UP

**macOS 生产常驻（launchd，KeepAlive 崩溃自拉起）**（docs/archive/DEV_TOOLS.md §6）：

```bash
python tools/bok.py prod install    # 生成 launchd plist 单元
launchctl bootstrap gui/$(id -u) "$HOME/Library/Application Support/BokVoice/units"/*.plist
python tools/bok.py prod status     # 官方健康面汇总
python tools/bok.py prod uninstall  # 对称卸载
```

**Windows 常驻**：`prod install` 经 `tools/schtasks_units.py` 注册 Task Scheduler（BootTrigger + RestartOnFailure 有限 3 次，不等价 launchd 无限 KeepAlive）；`--node-agent` 单任务模式注册 `bok-node-agent` 一个任务（`docs/RUNTIME_TOPOLOGY.md` §3）。

**Ubuntu 节点**：`tools/systemd_units.py`（`Restart=on-failure`：更新退出码 75 拉回、熔断退出码 0 保持停止）；装机走 `scripts/install-node.sh` 七步（体检→venv+CUDA→license 注册→模型→systemd→拉起+六点硬自检→摘要）。

**健康三表（单点）**（tools/bok.py:507-531）：

| 表 | 内容 | 探针方式 |
|---|---|---|
| `CORE_PORTS` | 8000/8787/8788/1235/1236/1237/8789/7880 | HTTP 面（`PROD_HTTP_CHECKS`）优先，无 HTTP 面退 TCP 判活 |
| `WORKER_PORTS` | agent-worker 8081 / interp-fwd 8082 / interp-rev 8083 | `GET :port/worker` 真端点（agent_name/load/sdk_version；TCP UP 对「进程在、没注册」不可见） |
| `PROD_HTTP_CHECKS` | 8000/health、8787/health、8788/health、1235/v1/models、7880/ | mt/settle(1236/1237)「起了才查」可选线 |

LLM 另有功能探针 `_probe_llm`（max_tokens=1 真往返；预算 env `BOK_DOCTOR_LLM_PROBE_TIMEOUT_S` 默认 10s；空闲 >2h 权重页入 ~40s 会一次假警，重跑区分）。契约钉在 `tests/test_health_surface.py`。

**常见坑**：Windows 交付节点优先用 `prod install --node-agent` 单任务模式（`docs/RUNTIME_TOPOLOGY.md` §3）；`schtasks /end` 只杀 Exec 动作进程，停栈靠 pidfile 精确补杀（`scripts/probes/probe_windows_lifecycle.py` 实跑契约）。

## 5. 鉴权开闸（auth-on 标准姿势）

单机本机自用可以不开鉴权（auth-off，全放行）。**只要暴露到局域网/云，必须开**（AGENTS.md「auth-on 开发栈标准姿势」+「CP 可选鉴权」）：

```bash
# 两把密钥本机生成，0600，绝不提交仓库
openssl rand -hex 32 > ~/.bok_dev_jwt_secret && chmod 600 ~/.bok_dev_jwt_secret
openssl rand -hex 32 > ~/.bok_dev_cp_token  && chmod 600 ~/.bok_dev_cp_token

BOK_AUTH_REQUIRED=1 \
BOK_JWT_SECRET="$(cat ~/.bok_dev_jwt_secret)" \
BOK_CP_TOKEN="$(cat ~/.bok_dev_cp_token)" \
BOK_ROOT_USERNAME=root \
BOK_ROOT_PASSWORD='<换成客户自己的强口令>' \
python tools/bok.py serve
```

铁律（`apps/control-plane/control_plane/auth.py`；startup fail-closed）：
- **密钥分离**：`BOK_CP_TOKEN`（agent worker 机器通道凭据）与 `BOK_JWT_SECRET`（用户 JWT 签名密钥）**必须异值**，JWT secret ≥32 字节；同值/缺失/过短 CP 拒启。
- `BOK_CP_TOKEN` 与 `BOK_AUTH_REQUIRED` **同设**：缺 CP token 时 auth-on 下 agent worker 的 turns/QA/设置上报全 401，doctor 的 token 探针同源失败。
- JWT 豁免路径只有：/health、/api/auth/login、/api/nodes/heartbeat、/api/nodes/register、/api/webhook/livekit；非 `/api` 的 GET/HEAD（静态站）两道门禁一律放行。
- 节点加固模式（auth-on 或 CP token 任一）：`POST /api/nodes/register` 必须带 license_key（root 经 `POST /api/nodes/licenses` 签发 `bokn_*`，明文只出现一次）+ 机器指纹。

**收窄语义（有身份就按身份收紧）**（`apps/control-plane/control_plane/auth.py` / `main.py`）：

| 机制 | 语义 |
|---|---|
| `scoped_account` | 列表类：user/admin 强制本账号；空账号 user fail-closed 403 |
| `deny_cross_account` | by-ID 类：越权一律 404（不泄露存在性） |
| `deny_foreign_owner` | 话术/QA 归属闸：user 只改自己的；共享条目改动=admin/root（403）、别人的=404 |
| 机器通道 | `BOK_CP_TOKEN`（`state.machine`）恒直通——agent worker 调 CP（turns/whatsapp/QA/垫话/设置）不被角色闸误杀 |
| 资源盖章 | user 建模板/QA 自动盖本人 `owner_user_id`；`call_sessions.created_by` 盖建单身份，agent 装配按 `owner_scope=created_by` 收窄 QA 可见集 |

## 6. 模型配置

**设置页「模型路由」卡（root 专属）**：登录后 `/settings` 页 `apps/web/components/settings-model-routing.tsx`。五车道（`packages/core/bok_voice_core/model_routes.py` 契约）：

| 车道 | 用途 |
|---|---|
| `a_reply` | A 线通话回复主 LLM |
| `judge` | 流程 judge + 意图判据 |
| `mt` | B 线同传翻译 |
| `settle` | 纪要/蒸馏/润色 |
| `mining` | CP 挖掘/聚类/学习驾驶舱 |

每车道可切 `local`（本机 OpenAI 兼容端点）↔ `openai`（云端，必填 base_url+model+api_key）；支持预置档（保存/套用/删除，套档不清密钥）与逐车道测连（max_tokens=1 探活）。**热切换：云端/端点切换下一通电话生效，零重启**（本机引擎内换 model 仍需该 server 重启）；admin/话务员不允许配置（后端 root+机器通道专属，403 兜底）。kill-switch `BOK_MODEL_ROUTING=0` 忽略全表。

路由端点组（apps/control-plane/control_plane/main.py，root+机器通道专属）：

| 端点 | 用途 |
|---|---|
| `GET /api/model-routing` | 读配置（api_key 恒掩码 `has_api_key`；`?internal=1` 明文回源，root 专属） |
| `PUT /api/model-routing` | 写配置（openai 档必填 base_url+model；**api_key 空=保留旧值**） |
| `POST /api/model-routing/presets` / `…/apply` / `DELETE …/{name}` | 预置档存/套/删 |
| `POST /api/model-routing/test` | 单车道探活（绝不 500） |

审计 `model_routing.*`，detail 零密钥材料。

**演示/单机降档**：`BOK_DEMO_PRESET=1` 把 LLM prompt-cache 压到 6GB（tools/bok.py:1149-1181；显式 `BOK_LLM_PROMPT_CACHE_BYTES` 优先）。9B 后端（:1237）默认不再随栈常驻，`BOK_DEV_9B=1` 显式才拉。

**云端 Realtime S2S 演示档**（§16 详述）：`BOK_QWEN_REALTIME=1` + `QWEN_REALTIME_KEY=<云端凭据>` 随栈拉起 :8084 worker（两键均已入 `_FORWARD_ENV` 白名单，tools/bok.py:1625-1631）。

**常见坑**：路由改动保存在 `global_settings.model_routing_json`（迁移唯一落点 `deps.build_engine()`）；本地档 base_url 留空仅 mt 车道=「回退 :1235」信号，其他车道留空=不生效（`packages/core/bok_voice_core/model_routes.py` 为准）。

## 7. 数据面：位置与备份

app-data 根（tools/bok.py `app_data_dir()`）：

| 平台 | 路径 |
|---|---|
| macOS | `~/Library/Application Support/BokVoice` |
| Windows | `%LOCALAPPDATA%\BokVoice` |
| Linux | `$XDG_DATA_HOME/BokVoice`（缺省 `~/.local/share/BokVoice`） |

| 子路径 | 内容 |
|---|---|
| `bok_voice.db` | SQLite 业务库（对象/人设/模板/QA/通话/审计/设置） |
| `vault/` | 知识库 markdown（`VAULT_ROOT`） |
| `models/` | 平台模型（打包模式） |
| `tts-cache/` | 本地 TTS 音频缓存（罐头 pin=True 不逐出） |
| `logs/`、`run/`、`audit/`、`units/` | 日志 / pid / 审计 jsonl / launchd 单元 |

**备份**：仓库未提供 CP 一键导出端点（端点全集以 `apps/control-plane/control_plane/main.py` 路由表为准）。推荐：
```bash
sqlite3 "$HOME/Library/Application Support/BokVoice/bok_voice.db" ".backup /backup/bok_voice-$(date +%F).db"
```
（同款 `.backup` 手法是本仓验收惯例，AGENTS.md「CP 侧新端点用真库副本验」。）
数据导入通道：对象 `POST /api/objects/import`、知识 `POST /api/knowledge/import`（main.py:4448/4486）。

**数据快照与清理语义**（docs/RUNTIME_TOPOLOGY.md §5）：
- 建通话时把对象绑定模板快照到 `call_sessions.template_id`（审计「这通用了哪版话术」）。
- 删除话术模板会同步清空引用它的对象卡 `object_profiles.template_id`。
- 删除知识文档会同时移除 vault 源文件。
- 测试残留清理：`python tools/bok.py clean-testdata`（默认 dry-run 打印，`--apply` 才经 CP API 真删，审计可追溯）。
**Supabase 镜像**（分发型云 CP）：`scripts/ops/dump_postgres_ddl.py` 生成引导件、`scripts/ops/smoke_postgres.py` 真库冒烟；CP→Supabase 必须走 Supavisor session pooler（IPv4 :5432，直连域名是 IPv6-only）；云侧 runbook=`deploy/cloud/README.md`。

## 8. 交付前检查清单

- [ ] `python tools/bok.py status` 全绿（含三 worker `GET /worker` 真探针，非仅 TCP）
- [ ] `python tools/bok.py doctor` 通过；打包形态 `doctor --packaged` 全硬门禁过
- [ ] 模型在盘：`python tools/bok.py setup status`（Mac 双布局 app-data/lmstudio 均认）
- [ ] auth-on：`BOK_AUTH_REQUIRED=1` + 两把密钥**异值**、≥32 字节、文件 0600、未提交仓库
- [ ] root 口令非文档示例值；web `/login` 登录、退出、匿名态（无 token）行为符合预期
- [ ] 账号与权限：root/admin/user 三角色各建一枚，按 §9 矩阵抽查 403 面
- [ ] 罐头物化：`python tools/bok.py tts-pregen --greetings --fillers --qa` 跑过（QA 快路只认有音频的词条）
- [ ] 一通真 E2E 通话（普通话+粤语各一）：`E2E_ONLY=cantonese .venv312/bin/python scripts/e2e/e2e_trilingual_livekit.py`
- [ ] 演示档状态确认：默认栈 `BOK_QWEN_REALTIME` 未设（:8084 不起）；若客户已购云端演示授权再按 §16 开
- [ ] `BOK_CP_TOKEN` 已设置（只要机器暴露在局域网）；CP 默认只绑 127.0.0.1（对外监听须显式 `BOK_BIND_HOST=0.0.0.0`）
- [ ] 术语门禁通过：`.venv312/bin/python -m pytest tests/test_cantonese_terminology.py -q`
- [ ] 交付物内无任何密钥材料（CI 有 gitleaks 全历史门禁，本地也自查一遍）

---

# 第二部 功能使用（按角色走查）

> 每节格式：入口路径 → 操作步骤 → 生效条件 → 常见坑。
> 页面文件都在 `apps/web/app/(app)/<页面>/page.tsx`；权限键目录=`apps/control-plane/control_plane/permissions.py`（web 侧镜像 `apps/web/components/session-context.tsx`，两表逐字对齐）。

## 9. 登录与角色

入口：`/login`（localStorage `bok_token`；**无 token=匿名本地模式**，全部页面+acc-001，单机 auth-off 零变化）。

权限矩阵（单一事实源 `apps/control-plane/control_plane/permissions.py`）：

| 键 | 标签 | user 默认 | 说明 |
|---|---|---|---|
| calls | 工作台 | 开 | 建单/通话日志/意向规则 |
| roster | 名册 | 开 | 捕获号码认领池 |
| campaigns | 外呼 | 开 | 外呼战役 |
| objects | 对象 | 开 | 对象名册（只读） |
| interpret | 同传 | 开 | B 线工作台 |
| templates | 话术 | 开 | 话术模板（可编辑） |
| qa | 快答库 | 开 | QA 词条（可编辑） |
| reports | 报表 | **关（可授予）** | 聚合报表/场景学习 tab |

管理键目录 6 键（**root 逐键下发给 admin**，「root 没下发就用不了」）：settings / knowledge / personas / audit / users / supervisor。
**root 专属、永不进任何目录**：nodes（节点）、licenses（授权）、模型路由（§6）、`GET /api/settings?internal=1` 明文回源。

| 角色 | 能做什么 |
|---|---|
| root | 全域；签发节点 license；下发/收回 admin 管理键；配模型路由 |
| admin | 本账号运营 + 被下发的管理键（存量 admin 行 `permissions_json` 空=全量，显式 JSON=root 裁定集）；授不出自己没有的键 |
| user | 8 页面键 ∩ 本人权限；模板/QA 只改自己的（共享条目改动=403，别人的=404） |

生效条件：**JWT 只装身份、权限逐请求查库**——主管改权限对在线 token 即时生效（`_gate_page` 每请求查库）。改密：顶栏自助改密对话框（全角色）。

web 导航三形态（`apps/web/components/session-context.tsx`）：

| 角色 | 导航面 |
|---|---|
| root | 纯平台面（含 /nodes 节点管理、模型路由） |
| admin | 运营页按有效集 + 管理页按下发键（/nodes 仅 root） |
| user | 8 页面键 ∩ 本人权限（路由守卫 `gateForPath` 拦无权页面） |

401 处理：api.ts `request()` 自动附 Bearer，401 清 token 跳 `/login`；服务未就绪时页面先开、`useControlPlaneReady` 轮询 /health 自愈重拉（`apps/web/lib/api-ready.ts`）。

**常见坑**：新建 admin 盖默认章=运营默认集+管理键全关，root 记得去 /users 下发面板勾选；user 建的模板/QA 自动盖本人章，body 传别的无效。

## 10. 话术模板与流程画布

入口：`/templates`（列表/表单编辑）与 `/studio`（AI 工作站，模板列表 → `/studio/?t=<id>` 工作台）。

工作站七个 tab（`apps/web/app/(app)/studio/page.tsx`）：话术流程（画布=推荐默认/列表双视图）· 意图管理 · 变量设定 · 客户意向 · 录音沉淀（罐头）· 通话日志 · 场景学习（需 reports 键）。

**步骤模型**（引擎=apps/agent/agent_runtime/flow.py `FlowController`，画布改的=引擎跑的）：
- 每步=目标 goal + 话稿 ref。ref 首行=正稿（进该步首轮渲染），「如果客户X→就Y」行=分支，「注意：」行=注意事项。
- **直念步**：步字段 `say:1`（表单「直念」复选框）——进入该步当轮 AI 直接念 ref 首行，零 LLM、零 TTFT，适合通知/赔偿承诺等合规内容。
- **分支动作**：分支应答首部可加动作前缀 `【收线】`（别名`【挂断】`）/`【转人工】`/`【跳第N步】`/`【留本步】`（`flow.py parse_branch_action`）。执行在每轮漏斗早段：`【收线】`=念完该句直接礼貌收线；`【转人工】`=打铃通知主管但不抢话；`【跳第N步】`=流程跳转（N 从 1 计）。第 1 步只放【收线】+【转人工】。
- **热词**：模板 `hotwords` 字段（表单可编辑，逗号/顿号/分号/换行分隔）注入 ASR 词汇表软偏置；总长护栏 ≤200 字符，数字串不进。
- **变量**：`{姓名}` `{单号}` `{聯絡方式}/{联系方式}/{contact}` 等（`apps/web/lib/var-panel.ts` 镜像 flow.py `object_vars`；联系渠道按对象语言缺省 zh→微信 / 粤/英→WhatsApp，换渠道不用改模板）。

**发布两态（重点）**（AGENTS.md W2 条）：
- 编辑永远只改 **live 草稿**（`steps_json` 等）；`POST /api/templates/{id}/publish` 是**唯一**把草稿冻结进 `published_json` 的写入口；PUT 永不触碰发布版。
- **AI 通话恒吃已发布冻结版**；人类通道（列表/详情）看的是 live 草稿。改完不发布=通话行为不变；模板列表会显示「已发布 / 有未发布改动」徽标。
- 建单时快照 `call_sessions.template_id`，中途改模板/发布都不动在途通话。

**流程画布**：场景泳道+步节点+答法抽屉（正稿/分支/注意三件结构化编辑；分支=条件+动作下拉+应答文本+录音状态点+补录按钮）。意图（graph_json）：关键词命中→绑定动作 `play_qa`（播指定 QA 罐头，可带 `then_jump` 播完跳步）/`jump_step`（跳步）/通知人工（打铃）；可配 `judge` 判据文本（1-400 字）补关键词未中的模糊轮。意图编辑深链 `/qa/?template=<id>&view=canvas`。保存=单键部分更新（`updateTemplate({steps_json})`），静态导出深链一律 query 参数。

**话术图语义细则**（`packages/core/bok_voice_core/flow_graph.py`；严格校验→CP 保存 400，运行时宽容解析→坏数据=空图不炸通话）：

| 规则 | 值 |
|---|---|
| 意图关键词 | 确定性子串（双侧 casefold，无正则——运营面安全） |
| `priority` | 小者先；同轮多命中只执行一个 |
| `once=true` | 每通至多触发一次（成功动作才烧账） |
| `intents[].steps` | 空=全程生效；非空=仅这些步生效 |
| `then_jump` | 仅 `play_qa` 合法，1-based，播完当场跳步；不另立 once 账 |
| 每轮优先级 | 拒绝收线 > 短应承 > 直念步 > 话术图 > QA 快路 > 自由 LLM |
| kill-switch | `BOK_FLOW_GRAPH=0` 整闸（judge 子闸 `BOK_FLOW_GRAPH_JUDGE=0`） |

模板版本：`GET /api/templates/{id}/revisions` 可查历史版本快照（发布/采纳动作会落快照）。

**WhatsApp 步方向铁律**：话术里收号码步必须是「向客户索取客户自己的 WhatsApp/微信号码」（等客户报数字串）；写成「加我们微信」客户永远不报号、步锁死。

**常见坑**：①改了话术客户侧没变化=忘了点发布；②分支条件写得太泛会在错误轮触发；`【收线】`类破坏性分支有双护栏（条件须字面命中或明确拒绝判定+热词幻觉否决），但运营仍应把条件写具体；③改分支文本后要补录分支罐头（录音状态点会显示 missing）。

## 11. QA 快答库（重点）

入口：`/qa`（列表+画布双视图，`apps/web/components/qa-library.tsx` / `apps/web/components/qa-canvas-view.tsx`）。

### 11.1 命中逻辑白话版（apps/agent/agent_runtime/qa_gate.py）

- 通话中客户每说一句，先拿**问法文本**和 QA 库比对：分数=0.6×语义余弦+0.4×子串覆盖，**阈值 0.90，只认字面措辞**——同义改写命中不了，必须按客户真实说法补词条（或用聚类把变体挂进同义簇）。
- **四道闸先于匹配**（`qa_gate.py qa_exclude_reason`，优先级从高到低）：

| 闸 | 触发 | 白话 |
|---|---|---|
| advanced / closing / done | 本轮已推进或已进收尾 | 收线了不再答 |
| wa_signal / wa_step_locked | 客户在报 WhatsApp 号码 | 收号步锁定，QA 不抢 |
| digits | 问法含 ≥4 位数字 run | 数字要走流程捕获 |
| refuse | 拒绝语气 | 不答只收 |
| verdict 白名单 | 仅确认/模糊/提问三类轮可进 QA | 其余语气不快路 |
- 命中且**应答录音已物化** → 跳过 LLM 直接播罐头（约 50ms）；**没录音 → 落回 LLM 正常回复**（日志 no_audio）。所以「录了没」直接决定省钱和响应速度。
- **优先级**：`priority` 小者先（[0,1000]，默认 10；web 表单可编辑，≠10 时列表/画布出 P 徽标）。
- **同义簇轮换**：把同一问题的多种问法（变体）拖挂到同一主词条（`cluster_head_id`），命中任意变体由主词条代表出场，簇内按本通最少播放轮换取成员播答案；在画布用运营钉死条目 id 的绑定不参与轮换。kill-switch：`BOK_QA_FASTPATH=0`/`BOK_QA_PRIORITY=0`/`BOK_QA_ROTATION=0`。

### 11.2 /qa 页与画布操作

| 操作 | 入口 | 说明 |
|---|---|---|
| 新建/编辑词条 | /qa 列表 | 问法/回答/优先级/语言/挂步/启用 |
| 挂簇（同义变体） | /qa 画布拖线 | PATCH `cluster_head_id` |
| 挂到话术步（仅该步生效） | /qa 画布拖到步节点 | PATCH `scope+step_index` |
| 罐头状态/试听/补录 | 列表状态面 | `GET /api/qa/canned-status` + `GET /api/qa/{id}/canned-audio` + `POST /api/qa/pregen` |
| owner 分档 | user 只见自己的+共享 | 共享条目改动=admin/root |

### 11.3 生产喂库三阶段

**① 冷启动=种子包导入（幂等）**，两把工具按来源选：

```bash
# A. 通用种子包（推荐——跨环境迁移/交付基线包；2026-09-26 落地并实弹验证）
python scripts/pipeline/qa_bank.py export --account acc-001 --out seed.jsonl   # 既有环境导出（默认共享池，--include-shared 连个人词条）
python scripts/pipeline/qa_bank.py import --account acc-001 --file seed.jsonl --dry-run   # 先看计划（零写入）
python scripts/pipeline/qa_bank.py import --account acc-001 --file seed.jsonl --pregen    # 导入+顺手补录罐头
```
幂等键=（归一问法, 语言），归一单点复用 `packages/core` 的 `normalize_question`——重复导整包不炸；同义簇两段式导入自动重映射 `cluster_head_id`（源 id→新环境 id，head 被跳过时回退存量 id，断链自动降级独立条并告警，绝不写悬空引用）；`status` 子命令看词条数/语言分布/簇数/罐头物化三态。

```bash
# B. 惜客通报表专用（存量素材：docs/海外仓话术演示.tar.gz / docs/AI自销话术.tar.gz）
python scripts/seed/import_xkt_qa.py --input tbl_ai_knowledge.json --account acc-001 --apply
```
默认 dry-run 打印计划；`--apply` 才入库（source=imported；Question 按 `&` 拆主条目+变体并回填 `cluster_head_id`；两层去重幂等，重跑安全）。步骤挂载留给画布拖线。以 `scripts/seed/import_xkt_qa.py` 参数面为准。

**② 学习循环=studio「场景学习」tab**（`apps/web/components/gap-mining.tsx`；需 reports 键）：
- **覆盖率+漏网轮采集**：顶部大数=快路覆盖率；漏网轮=「AI 走了 LLM」的客户原话聚合，勾选→改好答案→采纳入库（人工确认才入库，同问法+语言幂等）。
- **would-hit 铃铛**：漏网问法里已有词条能接住的比例汇总条（提醒你先补录音再采新条）。
- **AI 聚类**（`apps/web/components/study-tab.tsx`）：从真实通话挖掘候选，本地 LLM 判 变体/新/垃圾 三列，勾选采纳；dry 和 apply **必须同参**（页面已固定 limit=30，别手改）。
- **模板提案**：把漏网轮对目标话术提「分支行追加」或「意图关键词追加」两类提案；采纳只改 live 草稿，**记得去话术页发布才生效**（页面有提示）。
- **词条体检**：反向体检在库词条，四类通知——没人问过/带数字 bypass→建议删（retire）；播了又问/反复出现没用上→建议改答案（reanswer）。采纳时若调用者是 admin/root 会顺带触发一次补录音；**非 admin 返回 needs_pregen=true，提示找管理员补录**。

**③ 物化=补录音**：词条必须录了音才走快路。
```bash
python tools/bok.py tts-pregen --qa        # 全量补录（admin/root；云配额闸在 POST /api/qa/pregen）
```
**改答案=旧音频作废，必须重录**（缓存键=文本）。罐头落盘 pin=True 永不被 LRU 逐出。

**常见坑**：①「明明录了却没快」——多半是改过答案没重录，或词条被更高优先级/同义簇代表抢了出场（体检 detail 会写两种可能）；②用户自建词条属 user 时，admin 补录前确认账号与归属；③同义改写命中不了不是故障，补变体进簇。

## 12. 对象名册与人设

- `/objects`：对象卡（显示名/语言/快递平台/联系渠道/绑定话术模板/文字字段）。对象是变量与热词的数据源；删除话术模板会同步清空对象卡上的模板绑定。导入走 `POST /api/objects/import`。
- `/roster`：名册认领池——通话中捕获到客户 WhatsApp/微信号码自动入册（unclaimed→claimed→handled），话务员认领跟进（`POST /api/roster/{id}/claim|unclaim|handled`）。
- `/personas`：人设（公司/自我介绍/音色）。**音色三源**（`apps/web/lib/voice-options.ts` 统一）：MiniMax 静态目录、云端克隆（设置页「克隆我的声音」面板，`POST /api/tts/minimax-voices`）、本地预置/克隆音色。人设绑定 `reference_audio` 优先；按语言选择并与 TTS `language_boost` 联动。
- **人设保存点自动物化**：保存人设且音色相关字段变化 → CP 后台自动跑 `pregen_tts.py --greetings --fillers --qa --persona <id>`（单飞、幂等；`BOK_PERSONA_AUTO_PREGEN=0` 关）；响应 `tts_pregen.status` 是提醒面（queued/no_voice/…），日志 `app-data/logs/tts-pregen.log`。

**常见坑**：人设缺音色=开场白/垫话回落默认音色（日志 `BOK_FILLER voice_fallback` 是缺料提醒）；克隆音色 7 天内未用于合成会被云商删除、首用收激活费（以 CP 端点实现为准）。

## 13. 外呼战役

入口：`/campaigns`（建波/启停/进度表）。建单时选对象名单+话术（`campaigns.template_id` 会写入通话快照，AI 真用它）。

| 配置 | 语义 | 来源 |
|---|---|---|
| 时段窗 | 全局 `settings.campaign.call_windows` × 任务 `call_windows` 双层取交集，各 ≤3 组，本地时域；**不支持跨零点窗**，非法项静默丢弃 | apps/control-plane/control_plane/campaign.py |
| 并发 | `max_concurrency`（0=不限；一轮至多补一通温和爬升） | 同上 |
| 重拨 | 未接通自动回队：PUT 三态——显式 `{}`=清除 / 缺键=保留旧值 / 合法策略=覆盖（**页面开关关闭必须传 {}**，CP 侧幂等） | main.py PUT /api/campaigns/{id} |
| 拨号后端 | `sip.mode`：mock（演练，派脚本化虚拟客户）/ real（官方 CreateSIPParticipant，需 Redis+公网 trunk）；env `BOK_SIP_MODE` 优先 | 设置页「外呼（SIP）」卡 |
| 站点/trunk | real 档先建 SIP 站点（`POST /api/sip/sites`）再注册 trunk | 同上+docs/RUNTIME_TOPOLOGY.md §2 |
| 删除 | running 拒删（409，先停止）；删除连名单项并审计 | DELETE /api/campaigns/{id} |

**仪表盘口径**（`GET /api/stats/dashboard`，首页 `apps/web/components/dashboard-page.tsx`）：接通只认 `status==ENDED` 且 disposition 不在 {no_answer, rejected, failed}；「今日」=本地午夜对应 UTC 边界起算。

**战役循环内部**（campaign.py `_campaign_loop`，5s 巡检；mock 档演练与 E2E 全链 `scripts/e2e/e2e_campaign.py`）：
① 收割：dialing/in_call 的名单项其通话已终态 → 落结果（幂等）；
② 串行补位：无进行中项且有 pending → 建通话+显式派单（metadata 带 dial 块）；
③ 名单尽 → done。起拨前 gap 冷却：最近终态项距今不足冷却秒不起下一通（首通不受门控）。
mock 档派生 `scripts/runtime/mock_callee.py` 子进程当虚拟客户（answer 逐句轮播 / no_answer 不入房 / reject 进房即离 / 说一句就走四态）；real 档走官方 CreateSIPParticipant + SipCallError 码映射（486/603 拒接、408/480 无人接、5xx trunk 故障），需 Redis + 公网可达 trunk。

**常见坑**：对象没填电话=名单项直接 skipped 且不占冷却；「开关关不掉」=旧版本只改了表单没传 `{}`，现版本已堵，升级后仍建议点完开关保存后复查一次。

## 14. 通话工作台与主管台

- `/calls`：建单（选对象/语言/话术；演示档选项见 §16，root 专属）、通话列表、通话详情（轮次日志 `template_step`/speaker/gen）、**补结算**按钮（ended 行，CP 幂等兜底）。页面顶部折叠卡=**意向规则**（`apps/web/components/intent-rules-card.tsx`）。
- **WhatsApp 捕获状态**：通话的 whatsapp_status（捕获/待提供等）在列表与主管台呈现；捕获成功号码自动入名册。
- **意向规则（挂断归因）**（`packages/core/bok_voice_core/intent_rules.py`）：两级规则表（全局行 admin/root 写、账号行话务员写，读=两级行合并）。条件=事实键+比较符+数值，全部 AND 才命中，按 (priority,id) 取首条。**12 个事实键**（`INTENT_FACTS`，端点组挂 calls 键，web 折叠卡条件行=中文下拉）：

| 事实键 | 含义 |
|---|---|
| duration_s | 通话时长秒 |
| nudge_fired | 沉默心跳已发次数 |
| watchdog_fired | 响应看门狗触发次数 |
| storm_rounds | 打断风暴静听轮数 |
| repeat_count / refuse_count / objection_count / confirm_count / question_count | 各 verdict 累计 |
| step_max | 到达的最大话术步（1-based） |
| wa_captured | WhatsApp/微信已捕获（布尔） |
| graph_notifies | notify_human 已触发次数 |

评估在 agent 挂断单点，命中可覆盖 disposition 与 intent_code（落 `call_sessions.intent_code`，`/end?intent_code=`）；kill-switch `BOK_INTENT_RULES=0`。新意向 disposition 对工作台/外呼天然安全（非三个坏值=算接通、不重拨）。
- **打铃（notify_human）**：话术图意图或分支动作【转人工】触发 `POST /api/calls/{id}/assist` → `assist_status='notified'`；主管台横幅/徽标/WebAudio 蜂鸣（WA 捕获跳变同一条呈现链）；接管后置 done。**打铃不暂停 AI**——AI 照常兜话，零空档。
- `/supervisor` 主管台：在途通话卡片（对象/语言/当前话术步/时长/客户最近一句，3-4s 轮询），卡片直操作：暂停/恢复/接管/转人工/挂断（均有确认）。**静默旁听**：`ListenPanel`（入口 `/supervisor?listen=<callId>`）只订阅不发布，被听方无提示，start/stop 双审计。**SIP 转接**：`POST /api/supervisor/{id}/transfer-sip`——settings `sip.mode` 非 "real" 一律短路返回 `{"mocked": true}`（试点骨架，trunk REFER 未验证）。

**主管台操作端点**（apps/control-plane/control_plane/main.py；pause/ takeover /transfer /end 均审计，听一通 paused 通话不会被旁听悄悄恢复）：

| 操作 | 端点 |
|---|---|
| 暂停 AI / 恢复 AI | `POST /api/supervisor/{id}/pause-agent` / `resume-agent` |
| 接管 | `POST /api/supervisor/{id}/takeover`（置 escalated_to_human，agent 每 2s 轮询让位） |
| 转人工收线 | `POST /api/supervisor/{id}/transfer`（置 ended+disposition=transferred） |
| 拒绝收线 | `POST /api/supervisor/{id}/end`（置 ended+disposition=declined） |
| 静默旁听 | `POST /api/supervisor/{id}/listen` / `listen/stop`（purpose=listen token，只订阅） |
| SIP 转接 | `POST /api/supervisor/{id}/transfer-sip`（sip.mode 非 real 一律 `{"mocked":true}`） |

**常见坑**：旁听/接管都是 admin/root 专属闸；web 蜂鸣要等用户与页面交互后才会响（浏览器自动播放策略）；接管是「暂停 AI+人工上」，不是语音桥接。

## 15. B 线同传

入口：`/interpret`（旧 `/translate` 为 v1 POC 冻结保留，不再迭代）。

**建单**：我方语言/对方语言各选一头 + 各选一把 MiniMax 音色（会话级音色优先于设置页三键；空=跟随设置；选本地 Qwen3 音色会被闸过滤回落防报错）+ **术语表**（「源=译」或纯词条，逗号/分号/换行分隔，1000 字截断）→ 建单（kind=interpret）→ 坐席在一台 `/interpret` 一体台页里同时看双方双通道（me/other 视角，无需双方各自进房）。

| 会话级参数 | 落点/上限 |
|---|---|
| 术语表 | `call_sessions.glossary`（1000 字硬截）→ ASR 热词 + MT 术语槽双路注入 |
| 会话音色 | `voices_json`（512 字硬截）→ fwd/rev dispatch metadata 同份透传 |
| 音色优先级 | 会话级 > 设置页三键 > 硬编码默认（`tests/test_interpret_tts_provider.py`） |

**工作台**（`apps/web/components/interpret-console.tsx`）：
- 字幕区默认译员视角（只看「对方→我」流）；大字幕浮窗（三档字号/拖动/全屏/追帧）。
- 逐句翻译延迟面板（原文行→译文字幕到达差+近 8 句均值）。延迟体感物理下限约 1.2-1.5s；标准预算 3.5s（`BOK_PROBE_LAG_BUDGET_MS`）。
- 「听对方原声」开关（默认开像直接通话；关=纯字幕）；「译员耳语」开关（默认开：对方说的话翻成我方语言念给你听）；env `BOK_INTERP_REV_AUDIO=0` 可回退「rev 纯字幕」单向化档。
- 术语表双路注入：ASR 热词 + 翻译模型术语槽（会话级常量）。

**常见坑**：en 译文播放期变速不可行（浏览器 WebAudio 无保_pitch 变速）；全双工形态需虚拟声卡/耳机物理隔离并关自动半双工；B 线 worker 缺 MT 模型(:1236)自动回退主 LLM，质量略降（建议交付时把 `mt` 模型下载齐）。

## 16. 演示档（root 专属，云端 Realtime S2S）

用途：给客户演示「端到端云端语音模型」的独立档，**不进 A 线生产链**（无话术漏斗/QA/心跳/垫话，全部旁路）。

| 项 | 值 | 来源 |
|---|---|---|
| 开关 | 启动环境 `BOK_QWEN_REALTIME=1` 才随栈拉起 :8084 worker（默认栈零变化） | tools/bok.py:1618-1631、1849- |
| 云端凭据 | `QWEN_REALTIME_KEY` 必填，缺失 worker 人话拒接 | apps/agent/agent_runtime/realtime_demo.py |
| 建单 | `/calls` 建单 mode=realtime_demo 选项，**仅 root 可见可建**；非 root 建单 403（出境红线闸） | main.py:2160-2171 |
| 假对象红线 | 演示对象名必须命中测试前缀族或 demo- 前缀，真名拒接 | realtime_demo.py `DEMO_OBJECT_NAME_RE` |
| 时长熔断 | `BOK_REALTIME_DEMO_MAX_S` 默认 300s，到点自动告别收线；**护栏不许关死**（≤0 回落缺省） | realtime_demo.py `demo_max_seconds()` |
| 费用口径 | 云端按会话计费，usage 落账（metrics）；实测某 omni 档约 ¥0.22-0.3/通（2026-09-26 实测口径，随云商价目浮动） | docs/S2S_ROADMAP.md 增补 |

**常见坑**：忘了设 `BOK_QWEN_REALTIME=1` 时 web 建单能建但无人接（worker 未起）；演示对象用真名会被拒接——这是红线不是故障；`BOK_REALTIME_DEMO_MAX_S=0` 不是关闭而是回落 300s。

## 17. 报表 / 审计 / 洞察

| 页面/端点 | 内容 | 闸 |
|---|---|---|
| `/reports`（键=reports） | 通话汇总 `GET /api/reports/summary`、明细 `/api/reports/calls`、用量 `/api/reports/usage`；洞察卡 `GET /api/insights`（管理面，无权用户降级不显示） | `_gate_page("reports")` |
| studio 场景学习 tab | 覆盖率/漏网轮/聚类/提案/体检（§11.3） | reports 键 + 各采纳端点自身闸 |
| `/audit` | 审计流水（登录/发布/采纳/短信/节点/主管操作…`app-data/audit/*.jsonl`） | 管理键 audit |
| 云 TTS 健康 | `GET /api/stats/provider-health`（近窗配额/限流打点汇总） | 管理面 |

**常见坑**：reports 键默认关，话务员看不到是预期；审计查询是管理面（下发 audit 键的 admin 也可看）。

---

# 第三部 运维

## 18. 日志位置与探针清单

日志根=`<app-data>/logs/`（tools/bok.py 各 `_start_proc` 调用；下表以文件名真实产出为准）：

| 文件 | 内容 | 高频关键字 |
|---|---|---|
| `agent.log` | A 线 worker | `LLM_TTFT_MS(cached=N/M)`、`PERCEIVED_MS`、`FLOW_GRAPH jump|play|judge_*`、`BRANCH_ACTION refuse|handoff|jump|hold`、`QA_FASTPATH`、`[watchdog]`、`BOK_FILLER`、`MINIMAX_TTS_STALL`、`QWEN3_ECHO_SELF_HEARD_DROP` |
| `interp-fwd.log` / `interp-rev.log` | B 线双 worker | 句级提交 `source=partial-punct|partial-len|vad-pause`、`MINIMAX_BIDI_PERF` |
| `realtime-demo.log` | 演示档 worker | `REALTIME_DEMO usage` |
| `control-plane.log` | CP API | 403/审计、迁移 `_ensure_column` |
| `asr.log` / `tts.log` / `llm.log` / `mt-llm.log` / `settle-llm.log` / `embed.log` / `livekit.log` | 各 sidecar/服务 | 模型加载、/v1/models |
| `monitor.log` | dev 监护 | `down xN (active_calls=M)` |
| `tts-pregen.log` | 罐头物化 | 预合成进度/失败 |
| `runtime/logs/mock-callee.log` | 外呼 mock 虚拟客户 | `MOCK_CALLEE event=…` |
| `<app-data>/audit/*.jsonl` | 审计账本 | 逐操作一行 |

通话质量诊断技能：`.agents/skills/call-diagnosis/SKILL.md`（症状速查+标记词汇表+probe 用法）。延迟预算单一事实源：`docs/LATENCY_BUDGETS.md`。

**探针清单**（全部在 `scripts/`；真栈探针需 `python tools/bok.py serve` 后跑，离线单测不算验收）：

| 探针 | 验什么 | 主判据 |
|---|---|---|
| `e2e/e2e_trilingual_livekit.py` | A 线三语端到端（E2E_ONLY=zh/cantonese/en） | 三语全绿、0 丢转写 |
| `e2e/e2e_barge_in.py` | 打断：AI 播报中插话 | 停声+恢复不哑火（interrupted=yes resumed=yes） |
| `e2e/e2e_edge_cases.py` | 静音/超短音频不毒化链路 | 全绿 |
| `e2e/e2e_interpret.py` | B 线双向同传 E2E | 双向译文落库+启停×3 |
| `e2e/e2e_campaign.py` | 外呼战役全链（mock） | 接通/无人接/即挂三态+名册回写 |
| `probes/probe_branch_action.py` | 分支动作六腿真通话 | 每腿日志+turns 对账（kill 腿先以 `BOK_BRANCH_ACTION=0` 重启 serve） |
| `probes/probe_flow_graph.py` | 话术图 jump/play/then_jump/判据/跳步话面 | 日志族+turns（kill 腿先 `BOK_FLOW_GRAPH=0` 重启） |
| `probes/probe_qa_hit.py` | QA 快路（`--priority-duel`/`--rotation-duel` 离线） | 三档对照断言 |
| `probes/probe_interpret_latency.py` | B 线逐句感知延迟 | avg/逐句 ≤3500ms 预算 |
| `probes/probe_interp_backlog.py` / `probes/probe_interp_continuous.py` / `probes/probe_interp_duplex.py` | B 线背压丢句/边说边译/全双工 | drop≥1 且原文零丢等（见脚本头） |
| `probes/probe_latency_soak.py` | 延迟/竞争态（拆轮/风暴/兜底） | 正常轮哑 ≥2 FAIL + p50/p95 预算计数（首指标 PERCEIVED） |
| `probes/probe_offscript_soak.py` | 话术外问题 5 主题×10 轮 | 哑轮/兜底哨兵+质量旗（改 prompt/兜底后必跑） |
| `probes/probe_filler_timing.py` | 垫话/首声 | 首声 <2.5s 预算 |
| `probes/probe_fast_speech.py` / `probes/probe_brand_words.py` / `probes/probe_hotword_ab.py` | 快语速吃字/品牌词切轮/热词 A/B | 见脚本头 |
| `probes/probe_reply_quality.py` | 真实轮次回放打本地 LLM | 三断言 |
| `probes/probe_killswitch.py` | 节点吊销→窒息→复活全链 | CI node-handshake 实跑 |
| `ops/node_handshake_smoke.py` | 节点 license/指纹握手 | 加固流 9 步 |
| `bench/load_audio_concurrency.py` / `bench/load_cp_concurrency.py` | 通话并发/CP 并发 | 见脚本头（load_cp 需裸跑勿带 auth env） |
| `probes/probe_windows_lifecycle.py` | Windows 常驻生命周期 | B 段仅 Windows 实跑 |

## 19. 常见故障速查

| 症状 | 首查 | 依据 |
|---|---|---|
| worker 显示 UP 但不接单/「Agent did not join」 | `GET :8081/worker`（等端点）是否 200；三 worker 端口是否互抢（8081/8082/8083 必须分开） | tools/bok.py `_probe_worker`；AGENTS.md 端口显式分 |
| auth-on 下 agent 上报全 401 / doctor token 探针失败 | worker env 是否带 `BOK_CP_TOKEN`（prod 封闭 env 由 `_FORWARD_ENV` 白名单透传） | tools/bok.py `_FORWARD_ENV`；tests/test_forward_env.py |
| 新加 env 开关「不生效」 | dev serve 合并 os.environ 一般没问题；prod launchd/schtasks 是封闭白名单——**必须在 `_FORWARD_ENV` 表加一行** | AGENTS.md `_FORWARD_ENV` 立法 |
| 客户答案词被吞（答「拼多多」没反应） | 词表回声闸剥离（设计行为）；真答案恰在词表时有豁免（分隔符短答案），升级后复查 `_ASR_HOTWORDS` | AGENTS.md ASR 吃字四件套 |
| TTS 报 2054 | 音色 id 无效：EN 旧音色 id 已实测 2054 移除；本地 Qwen3 音色误配云端通道同闸过滤回落 | apps/web/lib/minimax-voices.ts；AGENTS.md MiniMax 运行规约 |
| 云 TTS 静默劣化（只剩垫话/兜底） | `GET /api/stats/provider-health`：2056=配额死、429=限流；看门狗重连日志 `MINIMAX_TTS_STALL` | packages/observability/bok_voice_obs/provider_health.py |
| 回复突然全哑只剩心跳（打断后） | 旧版本 playout pause 泄漏（livekit-agents 1.7.1）；确认 1.8.x 并升级 | AGENTS.md livekit-agents 版本条 |
| dev monitor 杀掉在途通话 | 新版有 active_calls 否决（active_calls>0 恒不杀）；确认跑的是现行代码 | tools/bok.py `cmd_monitor` |
| 换了代码 A/B 结果反了/行为怪 | **殭尸 worker**：`bok.py down` 杀不掉失联旧 worker——A/B 前后 `ps aux | grep agent_runtime` 必须为 0 再 serve | AGENTS.md「stack A/B 须防殭尸 worker」 |
| GPU 满载屏幕冻结（无头栈还活着） | WindowServer GPU 饿死（≥4A+2B 混跑）；演示档压到 A≤2-3 路 | AGENTS.md 并发梯队 |
| TTFT 随轮次越来越慢 | KV 前缀断裂；`BOK_LLM_MSG_DEBUG=1`+`scripts/ops/llm_cache_report.py` 看 cached 是否逐轮增长 | docs/RUNTIME_TOPOLOGY.md §6；scripts/probes/probe_llm_cache.py |
| 粤语通话全通 VAD 判定不介入且首声慢 | `BOK_SMART_TURN` 开了——**粤语通话禁开**（默认就是 0，别设 1） | AGENTS.md smart-turn 条（2026-09-26 定案） |
| 「AI 没走快答」但查库词条在 | 词条无物化音频（no_audio 落 LLM）或被四道闸旁路（数字/收号/收线）；`/qa` 状态面 + 体检 detail 双确认 | §11.1；control_plane/qa_drift.py |
| 开场白/垫话音色和通话不一致 | 人设没配音色（回落默认）；看 `tts-pregen.log` 与 agent.log `BOK_FILLER voice_fallback`；补跑 `tts-pregen --greetings --fillers` | docs/RUNTIME_TOPOLOGY.md 本地 TTS 缓存节 |
| A/B 测试脚本直打 :1235 出怪结果 | mlx `/v1/models` 是懒注册表——脚本必须显式带当前车道模型 env，否则会热载第二个模型污染数字 | AGENTS.md「车道换模两陷阱」 |
| 外呼「没重拨」 | 回队判定收口在收割更新单点；确认用的是现行代码（旧版 dial-result 直写终态曾令重拨静默 no-op）；PUT 策略三态是否传对 | AGENTS.md 战役调度条 |

## 20. 安全红线清单

1. **LLM 本地默认**：A 线回复/知识检索/联网默认全本地全关（`CONTEXT_RAG=1`/`WEB_SEARCH=1` 才开）；切云端车道=客户数据出境，须客户书面授权（模型路由卡 a_reply 云端档有出境警示）。
2. **云端演示档红线**：演示档产生出境云端费用——仅 root 建单、仅假对象（测试前缀族/demo- 前缀）、300s 熔断不许关死（§16）。
3. **凭据纪律**：两把密钥（JWT secret / CP token）必须异值、≥32 字节、文件 0600、绝不入库（CI gitleaks 全历史门禁兜底）；MiniMax key 存设置 DB（`tts.api_key`），`GET /api/settings?internal=1` 明文回源只给 root+机器通道（被盗 admin 凭据拉不走云凭据）。
4. **CP token 暴露前必设**：`BOK_CP_TOKEN` 未设=全端点放行（本机单用户形态）；CP 缺省恒绑 127.0.0.1，对外监听须显式 `BOK_BIND_HOST=0.0.0.0` 并自行加反代/防火墙。
5. **节点与 license**：加固模式注册必须 license+指纹；root 吊销 sticky（恢复必须 root `unrevoke`）；指纹是威慑不是 DRM，防线在服务端签发/配额/吊销/克隆检出。
6. **高危动作默认安全**：短信 webhook 默认 `enabled=False`（开了才发、HMAC 签名、烧真金走 admin/root 判据）；SIP 转接 settings `sip.mode` 非 "real" 一律 mocked；审计全操作留痕（`app-data/audit/`）。
7. **账号卫生**：交付后立即改 root 口令；测试数据清理 `python tools/bok.py clean-testdata`（默认 dry-run，`--apply` 才删）。
8. **旁听合规**：静默旁听是产品拍板行为（被听方无提示），start/stop 双审计——客户侧合同里要写明。

---

## 附：一页速查

```bash
# 起/停/看
python tools/bok.py serve | status | down | doctor
python tools/bok.py prod install | prod status | prod uninstall
# 模型/罐头
python tools/bok.py download [--only asr llm ...]   python tools/bok.py tts-pregen --qa
# QA 种子导入
python scripts/seed/import_xkt_qa.py --input tbl_ai_knowledge.json --apply
# auth-on 标准姿势（见 §5）
BOK_AUTH_REQUIRED=1 BOK_JWT_SECRET=... BOK_CP_TOKEN=... BOK_ROOT_USERNAME=root BOK_ROOT_PASSWORD=... python tools/bok.py serve
# 验收三件套（需全栈）
E2E_ONLY=cantonese .venv312/bin/python scripts/e2e/e2e_trilingual_livekit.py
.venv312/bin/python scripts/e2e/e2e_barge_in.py
.venv312/bin/python scripts/probes/probe_offscript_soak.py
```

> 本手册未尽处以代码为准：命令面=`tools/bok.py`；端点与闸=`apps/control-plane/control_plane/main.py`；
> 权限=`apps/control-plane/control_plane/permissions.py`；拓扑=`docs/RUNTIME_TOPOLOGY.md`；
> A 线逻辑=`docs/A_LINE_LOGIC.md`；工程军规=`AGENTS.md`。
