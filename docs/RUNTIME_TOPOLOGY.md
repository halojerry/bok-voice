# Bok Voice 运行时关系拓扑（RUNTIME TOPOLOGY）

> 本文件是"安装后的 App 能不能正常使用"的唯一验收基准。任何改动必须保证
> 按这张图跑起来：组件齐全、端口可达、数据落在 app-data、bundle 只读。

## 0. 分发型拓扑（P0 起双形态，spec=2026-09-10-thin-node-saas-design.md）

单机形态（本文其余部分描述的 dev/打包形态）不变。分发货形态新增：
- **云 CP**：同一 control-plane 代码，`DATABASE_URL` 指 Supabase Postgres；托管管理台静态 UI
- **节点包**：LiveKit + ASR/LLM sidecar + agent/interp worker + node-agent（tools/node_agent.py，
  心跳 :8000/api/nodes/heartbeat，commands 通道 P3）+ 节点本地托管坐席 UI（runtime-config.js 注入
  cpUrl/livekitUrl，spec §8 纯内网档）
- 新数据列：turns.org_id/line/speaker/gen/template_step/started_ms/ended_ms/perceived_ms（分析账本，spec §6.1）
- 新表：orgs/nodes（org 缝 + 节点注册表，node_token 只存 sha256）
- 节点远程停机开关（kill-switch）与 Windows 无头常驻契约：见 §3 对应小节

## 1. 组件与端口

| 组件 | 端口/协议 | 职责 | 运行时 | 数据落点 |
|---|---|---|---|---|
| `bok.py serve` | — | 编排：启动顺序、健康、模型下载 | 打包 Python | pid 文件 → app-data/run；日志 → app-data/logs |
| node-agent | 心跳出站 + UI :3000 HTTP | 薄节点守护（心跳/commands/全栈拉起/**坐席 UI 静态托管**，2026-09-17 Tauri 退役后 `--ui-dir` 即自动起 ：3000） | 打包 Python（stdlib-only 面） | node-state → ~/.bok/node-state.json |
| control-plane | :8000 HTTP | 业务 API、知识、设置、审计、LiveKit token | 打包 Python | SQLite → app-data/bok_voice.db |
| ASR sidecar | :8787 HTTP | 三语转写（zh/cantonese/en） | Mac=mlx_audio；Win=qwen-asr+CUDA | 模型 → app-data/models |
| TTS sidecar | :8788 HTTP | 合成 / 克隆 / 试听 | Mac=mlx_audio；Win=qwen-tts | 模型 → app-data/models |
| LLM | :1235 OpenAI 兼容 | A 线对话（flow judge、CP 摘要同源）；B 线翻译回退 | Mac=mlx_lm；Win=llama-server CUDA | 模型 → app-data/models |
| MT LLM（可选） | :1236 OpenAI 兼容 | B 线同传专用翻译（Hy-MT2 小模型，逐句无状态；模型缺失自动跳过 → B 线回退 :1235） | Mac=mlx_lm | 模型 → app-data/models |
| B-line worker | :8790 WS | 同传通道：ASR→翻译→TTS 队列 / 背压 | 内嵌 Node | 指标 → app-data/translation-metrics.jsonl |
| LiveKit server | :7880 WS/WebRTC | RTC 信令与媒体（7881/7882 RTC 端口） | 内嵌二进制 | keys → 内嵌 livekit.yaml |
| agent worker | 进程（健康 :8081/worker） | A 线智能体（VAD/对话/情绪/打断） | 打包 Python | 调 8787/8788/1235/8000；TTS=MiniMax 云（`tts_cache` 本地音频缓存叠加） |
| interpreter worker ×2 | 进程（健康 :8082 fwd / :8083 rev） | B 线双 AgentSession 同传（`bok-interp-fwd/rev` 显式分发） | 打包 Python | 调 8787/8788/1236(MT,回退 1235)/8000；TTS=MiniMax 云(或本地 8788) |

> 健康探针（2026-09-17）：三 worker 的 `/worker` 是 livekit-agents 内建真端点
> （agent_name/worker_load/sdk_version），`bok.py status/doctor/prod status`
> 统一读端点本体而非 TCP UP；LLM 另有 max_tokens=1 功能探针（端口 UP ≠ 能用，
> `BOK_DOCTOR_LLM_PROBE_TIMEOUT_S` 默认 10s）；b-line :8790 无明文 /health，
> 非 upgrade 请求恒 426=本体作答判活。常量单点 `CORE_PORTS`/`WORKER_PORTS`/
> `PROD_HTTP_CHECKS`（tools/bok.py），契约钉在 `tests/test_health_surface.py`。

### 本地 TTS 音频缓存 + 垫话 + Q→A 快路（2026-09-09，`docs/superpowers/specs/2026-09-08-*-design.md`）

- **缓存**：`agent_runtime/tts_cache.py`——`CachedTTS` 包装 MiniMaxTTS（仅拦
  `synthesize()` 整句路径，stream 透传），key=sha1(归一化文本+音色+模型档+采样率)，
  PCM 存 **app-data/tts-cache/**（LRU 500 条）。脚本直念线（开场白/心跳/收线/WA 确认）
  经 `_say_script`：命中 ~0ms 出声，未命中边播边落盘。开关 `BOK_TTS_CACHE=0`。
- **预生成**：`bok.py tts-pregen`（--greetings 无变量脚本线 / --objects 逐对象
  开场白收线心跳 / --qa 快答库启用条目应答 / --fillers 按人设音色物化垫话，
  2026-09-10 双层出声复活）——离线批量合成，需 CP 或 MINIMAX_API_KEY。
  greetings/fillers/qa 落盘**打钉不逐出**（逐对象开场白与运行时 tee 不钉，
  LRU 500 只淘汰未钉条目）。**人设保存点自动物化（W3）**：CP POST/PUT
  /api/personas 音色变化 → 后台 detached 子进程跑 `scripts/pregen_tts.py
  --greetings --fillers --qa --persona <id>`（新会话不被栈重启打断，单飞，
  `BOK_PERSONA_AUTO_PREGEN=0` 关），日志 **app-data/logs/tts-pregen.log**，
  响应 `tts_pregen.status` 即提醒面。
- **垫话**：`agent_runtime/fillers.py`——LLM 慢轮回复首音频 500ms 未到播预合成
  应承语。**2026-09-10 资产化改版**：垫话=随源码分发 wav 资产（`assets/fillers/`
  +manifest，`scripts/gen_filler_assets.py` 固定音色/参数预生成，三语各 10 短句
  万能话术 1.0-1.5s），运行时只播文件绝不云合成；**播放排序=垫话播完→300ms
  （`BOK_FILLER_GAP_MS`）→回复**（不再掐垫话，回复首帧经 `_RelaySynthesizeStream`
  hold 扣压）；**链发**（2026-09-10）：首条播完回复仍未出声 → gap 后自动补第二发
  （`BOK_FILLER_CHAIN` 默认 1，每轮封顶 1 次、与主动播共享 `BOK_FILLER_MAX=3`
  计数；池 39 条=每语言 10 短 1.0-1.5s+3 长 1.7-2.3s）；语言=装配时钉死的通话语言，
  池缺失跳过绝不跨语言。垫话绝不进
  LLM 上下文。开关 `BOK_FILLER=0`。
- **抢跑防抖 + PrefillSpeculator**（2026-09-10）：框架抢跑默认关
  （`PREEMPTIVE_GENERATION=0`，命中在本仓 STT 架构下结构性不可能——PREFLIGHT
  只发稳定前缀而提交是全句 FINAL，843 失效/0 命中实测）。替代预热=
  `prefill_speculator.py`：说话中按稳定前缀发 max_tokens=1 out-of-band 请求
  （严格前缀=上次真实请求快照+回复历史原文+user 前缀），真轮只 prefill 分叉
  尾巴；`BOK_PREFILL_SPEC=0` 关。诊断 `BOK_PREEMPTIVE_DEBUG=1` +
  `scripts/probe_preemptive.py`。
- **turns 分析账本**：A 线 `_on_conversation_item` 每轮上报
  line/speaker/gen/template_step/started_ms/ended_ms/perceived_ms（B 线
  `line=b`、speaker=me/other）；`gen`=llm/script/qa_fastpath 生成源；
  perceived_ms=eou+llm+tts 三段（时序：item_added 时 pending 已就位即取）；
  上报任务挂断 flush 防 teardown 丢轮。
- **Q→A 快路**：`agent_runtime/qa_gate.py` + CP `/api/qa-entries`、
  `/api/reports/qa-pairs`——四道闸（作用域/关键信号旁路/推进收线让位/阈值 0.90）
  全过且应答音频已缓存才跳过 LLM；挖掘入库 `bok.py tts-mine --apply N`。
  开关 `BOK_QA_FASTPATH=0`。B3：装配取数钉本通账号+`owner_scope=created_by`
  （共享+建单人个人条目；战役等无主通话='' 仅共享），不再硬编码 acc-001。

### 音频设备（设置页，2026-09-17 Tauri 退役后=纯浏览器）

- **输入（麦克风）**：`enumerateDevices`（授权后出 label）；采集走 WebRTC deviceId。
- **输出（扬声器）**：仅 Chromium 内核可靠（livekit `switchActiveDevice("audiooutput")`
  =`setSinkId`；一体台 B 线走 `AudioContext.setSinkId` router）；Safari/WKWebView
  **不支持**网页切输出——跟随系统默认（原 Tauri/CoreAudio 系统级切换随壳退役）。
- 选择持久化在 localStorage（`bok.audio.mic` / `bok.audio.out` 按角色 `.me`/`.other`
  变体），接通/开始采集时自动应用；浏览器权限走标准 getUserMedia 流程。

## 2. 数据流

### A 线（客服语音助手）

```text
浏览器/客户端 (WebRTC)
  → LiveKit :7880（信令/媒体）
  → agent worker（VAD 切句 → ASR :8787 转写 → LLM :1235 生成 → TTS :8788 合成）
  → 音频轨回放
同时：通话/转写/结算/审计 → control-plane :8000 → SQLite（对象、人设、知识、模板、设置、审计）
```

> **前端就绪自愈**：服务未就绪时先开页面（节点 node_agent 拉起全栈有秒级时差）。
> 前端 `lib/api-ready.ts` 的 `useControlPlaneReady` 轮询 `/health`，Control Plane 就绪后
> 自动重拉对象/人设等数据；`TypeError: Load failed` 不再直接上屏，而是映射为
> 中文提示「本地服务启动中/无法连接」。
>
> **URL 归一**：前端 API 基址、B 线 WS、LiveKit、agent 的 CONTROL_PLANE_URL 一律
> 默认 `127.0.0.1`（服务只绑 IPv4；避免 macOS localhost 优先解析 ::1 导致
> fetch 恒定失败）。节点拓扑由 `runtime-config.js` 注入 cpUrl/livekitUrl；
> `scripts/probe_thin_client_static.py` 校验 `out/` 不含烤死的 `http://localhost:8000`。

### Supervisor（主管台）

- 暂停/接管：control-plane 把通话置 `paused` 或 `escalated_to_human`；agent 的
  `_supervisor_watch` 每 2s 轮询通话状态 → 暂停自动回复（`on_user_turn_completed`
  抛 `StopResponse`）并 `interrupt(force)` 打断当前发言，人工接管会话。
- 恢复：`POST /api/supervisor/{id}/resume-agent` 把状态置回 `active` 并清
  `escalated_to_human`；agent 恢复自动回复。
- 转人工：置 `ended` + `disposition=transferred`，agent 退出并触发结算。
- 拒绝收线：客户明确拒绝/告别（`flow.py` REFUSE 判定）→ agent 注入收尾话术讲一句
  礼貌再见，随后 `POST /api/supervisor/{id}/end` 置 `ended` + `disposition=declined`
  并断房，结算由 agent `_on_close` 幂等触发。
- **静默旁听（2026-09-14 路线 A）**：`POST /api/supervisor/{id}/listen` 签发
  `purpose=listen` token（can_publish/can_publish_data=False、can_subscribe=True），
  **不挂 RoomConfiguration、不翻通话状态**（听一通 paused 不得把它恢复 active）；
  被听方无任何提示（产品拍板），`supervisor.listen.start`（签发即记）+
  `/listen/stop`（补时长）双审计。web 入口 `/supervisor?listen=<id>`，
  `ListenPanel` 用官方 LiveKitRoom 只订阅、绝不发布麦克风。
- **主管台真实化（2026-09-14）**：通话卡片直接操作（暂停/恢复/接管/转人工/挂断，均 confirm），
  卡片展示 对象/语言/当前话术步/已进行时长/最近一句客户话（`/api/calls/{id}/turns` 的
  template_step/speaker，3–4s 轮询只跑在途通话）；「进入工作台」深链 `/calls?call=<id>`；
  原「质量监控/纪律控制」占位卡已删。
- **登录与权限分面（B4，2026-09-14）**：`/login` 登录（localStorage `bok_token`，
  api.ts 自动附 Bearer、401 跳登录页）；**无 token=匿名本地模式**（全部页面+acc-001，
  单机 auth-off 现状零变化）。SessionProvider 拉 `/api/auth/me`（含 `permissions`
  有效集）；导航按权限过滤（user=8 键目录 ∩ 本人权限，主管专属面 admin 可见），
  路由守卫 `gateForPath`（无权面板）。`/users` 员工管理（admin 建号/启停/重置密码/
  按人勾选 8 权限键）；`/qa` 快答库页（B3 owner 分档）；话术页归属徽标。
  后端 `_gate_page` 逐请求查库——主管改权限对在线 token 即时生效。

### 外呼战役（mock 档，spec 2026-09-12-outbound-campaign-roster）

```text
web /campaigns（建波/启停/进度表）
  → CP POST /api/campaigns（object_ids 名单 + scenarios/scripts mock 剧本钩子）
    + POST /api/campaigns/{id}/start
  → CP 常驻 campaign loop（campaign.py `_campaign_loop`，5s 巡检 POLL_S）
      ①收割：dialing/in_call 的 item 其通话已终态 → item 落结果（幂等）
      ②串行：无进行中 item 且有 pending → 建通话 + explicit agent dispatch
        （metadata 带 `dial` 块），item 置 dialing；**任意时刻至多 1 路在跑**
      ③名单尽 → campaign done
      起拨前 gap 冷却：最近终态 item 距今 < gap_seconds 不起下一通（首通不受门控）
  → agent 收 metadata `dial` 块 → dial_outbound（dialer.py，四态出口）
      real 档：官方 CreateSIPParticipant(wait_until_answered) + SipCallError 码映射
               （486/603 拒接、408/480 无人接、5xx trunk 故障）；需 Redis + 公网
               reachable 的 trunk，本地 mock 档无需
      mock 档：CP `POST /api/sip/mock/callee` 派生 scripts/mock_callee.py 子进程
               （真 TTS 客户语音进房；answer 逐句轮播 / no_answer 不入房 /
               reject 进房即离 / hangup_mid 说一句就走）
        · 台词从 dial 块 `script` 下发，空台词按语言默认 2 句兜底
        · `speak_interval_s` 控句间隔；子进程会等 AI 讲完（对端音轨能量）
          再出声，避免与开场白撞轮
      → wait_for_participant：超时=no_answer、进房 1.5s 内离房零音频=rejected
  → agent `POST /api/calls/{id}/dial-result`（answered→ACTIVE，三失败态→ENDED+
    disposition）→ CP 按 call_id 反查 campaign item 同步状态（只认 dialing/in_call）
  → 接通后走正常 A 线装配（开场白=话术第 1 步直念）；captured 号码自动入名册
  → web /roster（认领池：unclaimed → claimed → handled）
```

- `BOK_SIP_MODE` 是 dial 后端 kill-switch（有值即终局：`mock`/`real`，非法值
  回落 mock）；缺省读设置 DB `sip.mode`（设置页 SIP 卡片）。agent 侧
  `resolve_dial_mode` 与 CP 侧 `_dial_mode` 同语义双实现（跨包分层，CP 不 import agent）。
- mock 客户子进程日志落 `runtime/logs/mock-callee.log`（`MOCK_CALLEE event=…`
  结构化行，E2E 断言素材）；房间断开立即收尾，CP 起子进程后起 daemon reaper 防僵尸。
- campaign 名单由 `POST /api/campaigns` 一次建仓：对象无电话 → item 直接 `skipped`
  （且不作 gap 冷却锚）。
- mock 剧本钩子（`scenarios`/`scripts`/`mock_speak_interval_s`）只服务演练与 E2E；
  campaign 级存 `campaigns.scripts_json`（无独立列，`__` 前缀键放 campaign 级参数），
  起拨时按 object_id 取台词塞进 dial 块 `script`。真实 SIP 拨号恒为空。
- **话术快照（2026-09-14）**：`campaigns.template_id` 由 `_start_call` 写入建单
  （`POST /api/calls` 的 `template_id`），agent 装配读 `call_sessions.template_id`
  优先、回落对象卡绑定——此前该字段只存不读，运营在战役里选的话术被静默忽略。
- **删除战役**：`DELETE /api/campaigns/{id}`——running 拒删（409，先停止），
  删除连名单项一起清并审计 `campaign.delete`。
- 全链路 E2E：`python scripts/e2e_campaign.py`（3 对象战役——1 接通走完话术+captured
  入名册 / 1 无人接 / 1 接通即挂；断言串行、终态三态、名册入册与 handled 回写）。

### 外呼战役（mock 档，spec 2026-09-12-outbound-campaign-roster）

```text
web /campaigns（建波/启停/进度表）
  → CP POST /api/campaigns（object_ids 名单 + scenarios/scripts mock 剧本钩子）
    + POST /api/campaigns/{id}/start
  → CP 常驻 campaign loop（campaign.py `_campaign_loop`，5s 巡检 POLL_S）
      ①收割：dialing/in_call 的 item 其通话已终态 → item 落结果（幂等）
      ②串行：无进行中 item 且有 pending → 建通话 + explicit agent dispatch
        （metadata 带 `dial` 块），item 置 dialing；**任意时刻至多 1 路在跑**
      ③名单尽 → campaign done
      起拨前 gap 冷却：最近终态 item 距今 < gap_seconds 不起下一通（首通不受门控）
  → agent 收 metadata `dial` 块 → dial_outbound（dialer.py，四态出口）
      real 档：官方 CreateSIPParticipant(wait_until_answered) + SipCallError 码映射
               （486/603 拒接、408/480 无人接、5xx trunk 故障）；需 Redis + 公网
               reachable 的 trunk，本地 mock 档无需
      mock 档：CP `POST /api/sip/mock/callee` 派生 scripts/mock_callee.py 子进程
               （真 TTS 客户语音进房；answer 逐句轮播 / no_answer 不入房 /
               reject 进房即离 / hangup_mid 说一句就走）
        · 台词从 dial 块 `script` 下发，空台词按语言默认 2 句兜底
        · `speak_interval_s` 控句间隔；子进程会等 AI 讲完（对端音轨能量）
          再出声，避免与开场白撞轮
      → wait_for_participant：超时=no_answer、进房 1.5s 内离房零音频=rejected
  → agent `POST /api/calls/{id}/dial-result`（answered→ACTIVE，三失败态→ENDED+
    disposition）→ CP 按 call_id 反查 campaign item 同步状态（只认 dialing/in_call）
  → 接通后走正常 A 线装配（开场白=话术第 1 步直念）；captured 号码自动入名册
  → web /roster（认领池：unclaimed → claimed → handled）
```

- `BOK_SIP_MODE` 是 dial 后端 kill-switch（有值即终局：`mock`/`real`，非法值
  回落 mock）；缺省读设置 DB `sip.mode`（设置页 SIP 卡片）。agent 侧
  `resolve_dial_mode` 与 CP 侧 `_dial_mode` 同语义双实现（跨包分层，CP 不 import agent）。
- mock 客户子进程日志落 `runtime/logs/mock-callee.log`（`MOCK_CALLEE event=…`
  结构化行，E2E 断言素材）；房间断开立即收尾，CP 起子进程后起 daemon reaper 防僵尸。
- campaign 名单由 `POST /api/campaigns` 一次建仓：对象无电话 → item 直接 `skipped`
  （且不作 gap 冷却锚）。
- mock 剧本钩子（`scenarios`/`scripts`/`mock_speak_interval_s`）只服务演练与 E2E；
  campaign 级存 `campaigns.scripts_json`（无独立列，`__` 前缀键放 campaign 级参数），
  起拨时按 object_id 取台词塞进 dial 块 `script`。真实 SIP 拨号恒为空。
- 全链路 E2E：`python scripts/e2e_campaign.py`（3 对象战役——1 接通走完话术+captured
  入名册 / 1 无人接 / 1 接通即挂；断言串行、终态三态、名册入册与 handled 回写）。
  **C4 号码容差（2026-09-15 T7 定责）**：本 E2E 验「captured→名册」链路，不验逐位
  ASR 精度——live 链路里号码句**头段**会被多解一个音（实证：`六四三二零一一一` →
  `六六四三二零一一一`/`八六四三二零一一一`，TTS 渲染与 sidecar 流式路径均无锅，
  照 agent 插件「VAD 前导帧并 `_pending`」喂法可 6/6 复现），故按「捕获串**含**脚本
  号码的 ≥7 位连续子串」判定；逐位精度归 `probe_cantonese_digits`/`probe_8khz_asr`。

### 电话边缘站点（VPS，spec 2026-09-13-sip-edge-thin-node-v2 §7 P1.5）

战役真中继档（`mode=real`）需要一个有公网口的 SIP 边缘。自用期（形态 2 单租户）
= 香港/同城小 VPS 上跑 **Redis + livekit-sip + 独立 LiveKit 站点**；GPU 仍在
Mac/客户机房侧，worker 只**出站**连站点 LiveKit —— 无任何入站端口需求。

```text
①VPS 部署（一次性，root/sudo）：
    sudo scripts/deploy_sip_edge.sh --api-key K --api-secret S \
         --livekit-url ws://127.0.0.1:7880 [--redis-url redis://127.0.0.1:6379]
    apt 依赖: redis-server + Go>=1.21 + pkg-config libopus-dev libopusfile-dev
    libsoxr-dev + **build-essential**（cgo 必须：media-sdk→amrwb-cgo 的 dec/enc
    全靠 #cgo，缺 C 编译器 = "build constraints exclude all Go files"；运行库
    libopus0/libopusfile0/libsoxr0）
      → git clone https://github.com/livekit/sip 到 /opt/livekit-sip → mage build
        （CGO_ENABLED=1，同上游 Dockerfile）
      → 产物装 /usr/local/bin/livekit-sip（幂等：已在则跳过，--force 重编）
      → 渲染 /etc/bok/livekit-sip.yaml（0640 root:livekit-sip）
      → systemd bok-livekit-sip.service（Restart=always、User=livekit-sip 非 root；
        Redis 依赖按 `--redis-url` 分支：本机档 `Requires=redis-server.service`，
        远端档 `Wants=`+注释——远端 Redis 与本机 redis.service 状态无关，别被拖停/拖起重启）
      → 结尾打印防火墙/健康检查提示：5060/UDP + 10000-20000/UDP **只打印不代开**
    配置键（上游 pkg/config/config.go 核实）：api_key / api_secret / ws_url /
    redis.address / sip_port: 5060 / rtp_port: "10000-20000"（只认字符串形态）/
    use_external_ip: true（SDP 通告公网 IP）/ logging.level

②CP 建站点行（sip_sites 表）：
    POST /api/sip/sites {name, livekit_url, sip_edge?, trunk_id?, numbers?, region?}
    → 建行（**幂等**：同 account+name 已存在返回既有行、不重复建不改写；审计
    `sip.site_created`；name 空/sip_edge 越界=400）
    GET /api/sip/sites（列表，面板下拉数据源）、
    POST /api/sip/sites/{id}/trunk（注册 trunk）。
    面板「设置 → 外呼（SIP）」站点下拉旁「+ 新建站点」最小表单（name +
    livekit_url 两字段）直连 POST 建行——旧版空库只能提示「请先在后端登记站点」。

③面板「设置 → 外呼（SIP）」切 real 档 → 选站点 → 填 trunk 商（如 Telnyx）
    地址/主叫号/鉴权 → 「注册 trunk」= POST /api/sip/sites/{id}/trunk：
    CP 用 LIVEKIT_URL/LIVEKIT_API_KEY(env 或 app.state) 调官方
    CreateSIPOutboundTrunk（address/numbers 必填，auth_* 空=IP 白名单模式）→
    返回 ST_... 回填 site.trunk_id（密码只进不出；失败 502 且不写 trunk_id）

④战役挂站点：POST /api/campaigns 带 site_id → campaign 起拨的 dial 块
    trunk 按站点优先（站点无 trunk/未挂站点 → 回退 settings `sip.trunk_id`，
    单站点旧行为零变化）

⑤Mac worker 出站注册到远端站点（无入站端口）：
    LIVEKIT_URL=wss://<vps> LIVEKIT_API_KEY=<站点 key> LIVEKIT_API_SECRET=<站点 secret> \
      python tools/bok.py serve
    bok.py 原样透传 LIVEKIT_URL/凭据给 agent/interp worker（tools/bok.py）。
    **P1.5 现状**：CP 与 worker 的 LiveKit 地址由 env `LIVEKIT_URL` 单点决定，
    `site.livekit_url` 只是登记字段（`get_default_site` 的 `site-local` 恒合成、
    不入库）——token/dispatch 尚未按 site 逐站点路由
```

- **Redis 是硬依赖**：livekit-sip 经 Redis（psrpc）与站点 LiveKit 耦合，两边必须
  指同一个 Redis，否则 trunk 注册/房间调度互不可见（trunk 注册会 502）。
- 端口面：5060/UDP（SIP 信令）+ 10000-20000/UDP（RTP 媒体）必须公网可达（VPS
  安全组人工放行）；VPS 其余端口不对公网。
- dial 后端开关语义不变：`BOK_SIP_MODE`（env，终局）> 设置 DB `sip.mode`。
- 真中继启用前置门（spec §6，2026-09-15 审查定案）：mock 档 8kHz 窄带门禁已过
  （`scripts/probe_8khz_asr.py`），仍需闭环「带前缀粤语报号窄带复测」+
  「真 G.711 样本回填」两项，缺一不放行。

### B 线（同声传译 v2，LiveKit 双端）

```text
web /interpret 两端（me/other，各自选麦克风/扬声器）
  → CP /api/calls(kind=interpret) + /api/token(role=me|other)
  → LiveKit 房间（me-<room> / other-<room> + 两个 interpreter agent）
  → interpreter(AgentSession 全托管):silero VAD → Qwen3-ASR(源语言钉死,
    cantonese 走 hint) → MT 翻译模型(:1236 Hy-MT2,官方模板逐句无状态、
    历史不累积;未部署自动回退 :1235 主 LLM) → MiniMax 云 TTS(默认 turbo 档,
    按 target_lang 三键换音色 + language_boost;设置非 minimax 时本地 Qwen3-TTS 回退)
  → 译文轨 trans-<lang> 只授权对方订阅(set_track_subscription_permissions);
    字幕 lk.transcription 全量广播(原文+译文,前端 useTranscriptions 渲染)
  → 译文句 add_turn(原文：…\n译文：…) → 房间断开 settle → 总结/知识蒸馏/vault
agent worker:A 线 `agent_name="bok-voice"` 显式分发(官方推荐;隐式 dispatch 已废除,
杜绝同传房被客服 agent 隐式抢派)。operator/supervisor token 由 CP /api/token 挂
RoomAgentDispatch(metadata={call_id})精确派发;CP /api/token 即官方 TokenSource
endpoint 契约({serverUrl, participantToken},201),官方 SDK 可直连。
interpreter worker:bok serve 起 2 个常驻进程(interp-fwd/rev,agent_name
bok-interp-fwd/rev 显式分发,方向语言对+精确 identity 由 me 端 token 的
RoomAgentDispatch metadata 下发;无房间时空闲,job 到达才拉管线)。
三个 livekit-agents worker 健康端口显式分拆(A 线 8081/interp 8082·8083,
WorkerOptions.port)——默认同为 8081 会竞态,后绑者 Errno 48 即崩
("Agent did not join the room" 根因,2026-09-06)。
旧 v1(/translate + WS :8790)冻结保留作 POC,不再迭代。
```

## 3. 生命周期

### 启动顺序（`bok.py serve`）

1. 确保 app-data 目录（run/logs/models/vault）存在
2. control-plane :8000（注入 `DATABASE_URL=sqlite:///<app-data>/bok_voice.db`、`VAULT_ROOT=<app-data>/vault`）
3. LiveKit :7880（内嵌二进制 + livekit.yaml；不依赖 Docker）
4. ASR :8787、TTS :8788、LLM :1235（并行拉起）
5. B-line :8790（注入 app-data 配置文件）
6. agent worker（注册到 :7880）
7. 轮询全部端口 UP → 前端可用

### 关闭（`bok.py down`）

按 pid 文件逐个 SIGTERM（run/*.pid）；node_agent full 模式退出/收到 shutdown 指令时经 cmd_down 收栈。

### 失败处理

- 任一服务超时未 UP：`bok.py serve` 返回非零，日志在 app-data/logs，不静默继续
- 模型缺失：首启向导 `setup status/download`，幂等 + 断点续传
- 硬件不满足（Windows 无 NVIDIA GPU）：`doctor --packaged` 阻止 LLM 启动并给文案

### 节点远程停机开关（kill-switch，2026-09-16 site-delivery M1/M2）

root 在 web `/nodes` 页（`POST /api/nodes/{id}/revoke`，`/unrevoke` 解除）熔断
一台节点的云端供给：

- **窒息点=通话面、认证模式无关（不是整 CP 封锁）**：`POST /api/calls` 与
  `POST /api/token` 两个端点上，任何 Bearer 解析到已吊销节点的请求一律
  403 `node revoked`（`node_revoked_gate` 中间件注册在最外层，auth-off 开放流
  同样拦）；携带 node_id 的建单在建单时 403（未知节点 404）、该通话取 token
  时同样 403（覆盖坐席 JWT 通道）。revoked 节点打其余端点仍走各自原有门禁，
  行为零变化。
- **node_id 绑定的生产者边界（诚实边界，fixwave 记录）**：`call_sessions.node_id`
  目前只由**直连 API 的调用方**写入（`POST /api/calls` 显式携带）——外呼战役
  （`control_plane/campaign.py`）与 web UI 建单尚未接线，node_id 绑定窒息通道
  对这两条路径暂为潜在防线；已交付的真实 choke 是 node_token 通道（上面的窒息
  点中间件）。campaign/UI 建单接线为 tracked follow-up。
- **root 吊销=sticky**：永久生效，唯一恢复路径 `POST /api/nodes/{id}/unrevoke`
  （root 专属；解除后节点须重注册换发 token / 心跳成功才回 online）；克隆检出
  自动吊销（revoked_source=auto_clone）保留同指纹重注册复活路径——root 吊销
  不存在注册端点自愈的出路，恢复必须经 root 显式操作。
- **心跳 401 行动指令**：仅 root-revoked 的心跳 401 detail 携带机器可执行的
  `{"reason": "…", "action": "shutdown"}`（license 类 401 仍是纯文本）。
- **node-agent 服从语义**（`tools/node_agent.py`）：`BOK_NODE_KILL_ON_REVOKE`
  默认 1 = 收到 shutdown 指令即 cmd_down 停栈 + exit 0（绝不 self-heal、
  不重注册）；=0 观察档（只逐轮大声记录，不停栈不退出——心跳持续 401、失联
  REFUSE_JOBS 照旧）。license 吊销=永久：node-agent 连续 3 次心跳确认后退避
  停栈（无 self-heal 出路）；token 失效/auto_clone 吊销仍走 license 流幂等
  重注册自愈（复活路径保留）。
- **launchd/KeepAlive 复活环**：mac 生产档（launchd KeepAlive）下被吊销节点
  会被重新拉起，但只会循环在「注册探测→心跳 401→停栈→exit 0」——栈不复活，
  只余每次探测的烧耗；彻底止息等 root `unrevoke` 或卸载节点。
- 验收量尺：`scripts/probe_killswitch.py`（CI `node-handshake.yml` linux job
  对真 CP 实跑 吊销→窒息点→unrevoke 复活 全链）。

### Windows 无头常驻（Task Scheduler，`bok.py prod install`）

Windows 站点机的常驻等价物（对照 mac launchd RunAtLoad + KeepAlive）：

- `bok.py prod install` 在 Windows 经 `tools/schtasks_units.py` 逐 unit 注册
  Task Scheduler 任务（任务名 `bok-<unit>`；XML 落盘必须带 BOM 的 UTF-16）：
  BootTrigger（开机自起）+ RestartOnFailure（PT1M × 3 次）+ SYSTEM principal。
  **诚实边界**：RestartOnFailure 是有限次拉回（3 次），不等价 launchd
  KeepAlive 的无限 KeepAlive。
- **`--node-agent` 单任务模式**（节点包拓扑）：只注册 `bok-node-agent` 一个
  任务，node_agent 内部经 cmd_up 拉全栈，心跳/凭据参数原样透传
  （`bok.py prod install --node-agent --cp-url <url> --license-key bokn_…`）。
- Task Scheduler XML 没有 env 元素：action 用 cmd.exe 前缀链
  （`cd /d … && set "K=V" && … && "exe" args`）注入 env，env dict 与 mac plist
  同源同 dict（`cmd_prod_install` 单点组装，含 SSL_CERT_FILE 烘焙）——凭据
  存放在任务 XML 与 plist env 中等价。
- `prod uninstall` 对称卸载（mac launchd bootout + 删 plist / Windows
  `schtasks /delete`；装过 `--node-agent` 的机器连 bok-node-agent 一把清）。
- `--open-firewall`（netsh 放行 :8000/:7880 TCP+UDP）**默认只打印计划不
  执行**，显式 flag + 管理员权限才落防火墙。**:8000 规则只有 CP 以
  `BOK_BIND_HOST=0.0.0.0` 显式 opt-in 对外监听时才有意义**——CP 缺省恒绑
  127.0.0.1（`bok.py` `_cp_bind_host()`：serve 与 prod install 单元定义共用，
  M2.3 补课的 env 开关），:7880 由 livekit.yaml 决定。
- **`schtasks /end` 子树边界**：/end 只终止任务的 Exec 动作进程（本仓恒为
  cmd.exe），链式子进程（python/livekit 等 payload）存活——
  `scripts/probe_windows_lifecycle.py` B5b 在真 Windows 实跑断言；依赖 /end
  停栈的 `prod uninstall` 据此按 pidfile 精确补杀（只杀自己 pid 记录的进程，
  绝不按镜像名杀共享镜像），无 pidfile 的任务树成员（如 node_agent 自身）
  WARNING 提示手工处理。
- `scripts/install-node.ps1 -InstallService`：装完即注册常驻服务（内部执行的
  就是上面的 `prod install --node-agent`）；当前为 **token 模式**
  （`--node-token` 直传），license 模式节点直接用 bok.py 注册；任务 XML 经
  env 前缀链存凭据，与 plist env 等价。
- 生命周期实跑量尺：`scripts/probe_windows_lifecycle.py`（A 段 down 树杀
  全平台执行、B 段 schtasks 契约仅 Windows 实跑，runner 无提权时 B 段按
  access-denied 优雅 [skip]；CI windows job 实跑）。

## 4. 路径约定

| 路径 | 可写 | 用途 |
|---|---|---|
| 节点安装树（`~/bok-voice` / `%USERPROFILE%\bok-voice`） | 代码可更新 | 代码、runtime/（Python/二进制复用）、静态前端 |
| `~/Library/Application Support/BokVoice`（win `%LOCALAPPDATA%\BokVoice`） | 是 | models / vault / logs / run / bok_voice.db / audit / bline.json |
| `~/.lmstudio/models` | 只读引用 | 本机开发/软链复用（`--` 目录名映射） |

## 5. 默认配置与环境变量

打包模式（`BOK_PACKAGED=1`）由 `bok.py serve` 注入：

| 变量 | 值 | 作用 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///<app-data>/bok_voice.db` | 业务数据持久化 |
| `VAULT_ROOT` | `<app-data>/vault` | 知识库 markdown 落盘 |
| `LIVEKIT_URL` | `ws://127.0.0.1:7880` | control-plane 签 token 时下发的服务器地址 |
| `LIVEKIT_API_KEY` | `devkey` | control-plane `/api/token` 签发真实 JWT（缺失会 503） |
| `LIVEKIT_API_SECRET` | `devsecret` | 同上；与 livekit.yaml `keys` 一致 |
| `BOK_BLINE_CONFIG` | `<app-data>/bline.json` | B 线通道配置（ASR/TTS/翻译/指标路径） |
| `QWEN3_TTS_DATA_DIR` | `<app-data>/tts-data` | TTS 语音克隆注册数据（registry + 参考音频），bundle 只读/可升级 |
| LLM 默认 | `provider=local_openai` + `http://127.0.0.1:1235/v1` | A/B 线共用本地 LLM |
| 服务绑定 | 127.0.0.1 | 仅本机可访问 |

### 设置（`/api/settings`，Agent 运行时会真实消费）

> web 设置页（2026-09-14 路线 A 瘦身）：主视图只留「语音与凭据 / 音频设备 / 外呼 SIP /
> 罐头试听 / 本机桌面服务」；ASR/LLM/VAD/运行策略收进底部「开发者参数」折叠区。
> 默认值不变、PUT 载荷形状不变（CP 零改动）。

- `asr.provider`：`qwen3_asr`（本地 sidecar）/ `fake`（仅测试）。语言值统一 `zh/cantonese/en`（粤语全时空唯一拼写 `cantonese`）；agent 在会话语言为粤语时给 sidecar 传 `language=cantonese` 强制模型按粤语转写，避免 auto 误判成普通话。
- `llm.provider`：`local_openai`/`mlx`（本地）/ `deepseek`（云端，缺 `api_key` 显式告警并回退本地）/ `fake`。
- `tts.provider`：`qwen3_tts` / `volcano_streaming`（需 `VOLC_*` 环境变量）/ `fake`（静音测试音，非火山 beep）。
  音色兜底按语言 `speaker_zh/speaker_cantonese/en`（旧拼写键已由启动迁移改写）；persona 绑定 `reference_audio` 优先。
- `vad`：`provider` + `max_buffered_speech` / `min_speech_duration` / `min_silence_duration` / `interruption`  —— 直接构造 `inference.VAD` 与打断开关（环境变量 `VAD_*` 仅作部署覆盖）。
  基线默认（2026-09-05 句号级提交落地后）：`min_silence_duration=0.45`、`min_speech_duration=0.15`；
  A 线 turn_detection=`stt`（STT 句末 END_OF_SPEECH 提交，句级 FINAL→EOS，说话中即提交，
  数字串/短句/1.5s 限流保护；续接可能句——归一后 ≥2 位数字或系词收尾——会在句末被扣住
  `QWEN3_ASR_JOIN_HOLD_MS`（默认 800，0=关）等续段并入同一 sidecar 会话、一条 FINAL
  覆盖全段，超时由 flush 补发（该轮多等 ≤HOLD ms；flush 与正常停嘴同一套短尾规则并带
  会话纪元守卫，finish 等待期续讲开新会话唔会被 reset 清轮）），
  endpointing `min_delay=0.25`/`max_delay=0.6`。
- `sip`（Wave2）：`mode`（`mock`/`real`，外呼拨号后端；env `BOK_SIP_MODE` 优先且
  有值即终局）/ `trunk_id` / `address` / `auth_username` / `auth_password`（secret 掩码：
  GET 回空串 + `has_auth_password`，PUT 传空=保留旧值）/ `numbers`（本端号码池）/
  `ringing_timeout_s`（默认 30）/ `max_call_duration_s`（默认 600，mock 档作 agent 侧时长
  保险丝）。切 `real` 的前提：已注册 SIP trunk + Redis（LiveKit 的 SIP 服务依赖）+
  公网可达；未满足时保留 `mock`。
  语言钉定 + 热词：每通对话语言钉死随 `/api/start?language=` 下发（Chinese/English/Cantonese
  规范名）；热词 context（官方 customizable context，system message 词汇表软偏置）随
  `/api/start?context=` 下发——话术模板 hotwords 字段 + 话术领域词 + 对象文字字段
  （`BOK_ASR_HOTWORDS=0` / `QWEN3_ASR_CONTEXT=0` 两级回退）。
  （历史警戒已失效条件化：当年压端点致哑火=轮次在离线 ASR final 前提交；现提交结构性等待
  STT FINAL 且句级路径 FINAL 即句文，三语 E2E 实证 0 丢转写。回退开关：`TURN_DETECTION=`
  置空回 EOT 模型档 + `QWEN3_ASR_SENTENCE_COMMIT=0`（两者须一起关，否则句级 FINAL 会
  叠进停嘴 FINAL 重复转写）；**kill-switch 档 endpointing min_delay 自动回 ≥0.35**
  （`_endpointing_delays_from_env` 强制，无需手动——未校准 EOT 配 0.25 早提交截断粤语，
  p6 实证）；B 线 interp 2026-09-16 起与 A 线同默认开（`_turn_handling_opts` 单源
  切 stt + 打断默认关，见 AGENTS.md B 线同传条）。真实音频滑窗句间只出逗号，
  VAD 停嘴微停顿（≥0.45s）为第二句边界源（`QWEN3_ASR_SENTENCE_PAUSE_TRIGGER` 默认 1）。）
- `policy`：`offline_first`/`cloud_first`；建通话（`POST /api/calls`）时写入 manifest。

### LLM prompt 结构与多客服并发容量

- **每轮 system 顺序**：稳定指令前缀（用户语言规则 / 回复节奏 / 应答准则 / 话术总览 / 当前步）
  + 人设 base（`_instructions`+facts） + 易变参考尾部（知识库 / 联网 / 对话记忆）。
  目的：让 token0 起的公共前缀逐轮字节不变，命中 mlx_lm 的 prompt KV-cache——
  实测同 system + 不同 user 轮次 `prompt cached=1413/1434`，第二轮 2571ms→502ms。
- **绑话术的对象默认不做 RAG**（`has_steps` 门控，`CONTEXT_RAG=1` 强制开）：单对象只上话术，
  减少每轮 prefill；知识单条截断 350 字。无模板的开放咨询才检索知识库+联网。
- **多客服并发容量**（单机 M4 Pro 48GB，Qwen3.5-4B-MLX-4bit）：mlx_lm 的 prefill 是
  ~0.6k token/s 的架构硬墙（Qwen3.5 混合线性注意力，批多宽同速）；KV-cache 命中则绕过它。
  前置做足后同机约 **4-8 路交互客服**（warm 前缀每路≈decode+小尾 prefill）；冷启动同撞
  大 prompt 时受 prefill 墙限（2-4 路亚秒）。bok.py 已给 mlx server 加 `--prompt-cache-size 128`
  （默认 10 会被 4-6 路并发打穿）。超过此容量 → 第二台 Mac 起同栈 / GPU(CUDA) 服务器 vLLM
  （Mac 本机 vLLM 跑不了，且那是另一套架构）。

### 数据快照与清理

- `call_sessions.template_id`：建通话时把对象绑定的模板 id 快照到通话记录（审计"这场用了哪版话术"）。
- 删除话术模板会同步清空引用该模板的对象卡（`object_profiles.template_id`）。
- 删除知识文档会同时移除 vault 源文件，重启重建索引后不再"复活"。

## 6. 故障排查

1. `python tools/bok.py status` — 七项端口 UP/DOWN
2. `python tools/bok.py doctor --packaged` — 结构/依赖/硬件体检
3. app-data/logs/*.log — 各服务日志；app-data/audit/*.jsonl — 审计
4. bundle 只读：任何试图写 bundle 的路径都要改到 app-data

## 云端部署（P1，宝塔/Docker 主机）

`deploy/cloud/` 是云侧唯一部署物：单容器（GHCR `ghcr.io/halojerry/bok-voice:latest`，CI 产出）+ Supabase 业务库 + vault 命名卷。操作 runbook（含宝塔导入/升级/HTTPS 反代/备份）见 `deploy/cloud/README.md`；首次启动 `build_engine()` 幂等迁移自动建表（无需手工 SQL）。CI 门禁：`compose-rehearsal.yml`（宝塔导入路径永远有效）+ `schema-drift.yml`（schema 产物与代码零漂移）。节点接入见 `scripts/install-node.sh`（license 流）与 `docs/NODE_PACKAGING.md`（node-agent 二进制）。
