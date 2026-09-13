# 外呼战役 + 名册认领 + 官方 SIP 外播（模拟联调档）· 设计

日期：2026-09-12 · 状态：已批准（方案 1） · worktree：`va-campaign`（base = origin/main c0fd5b3）

## 1. 背景与目标

A 线客服通话目前只能从 web 手动创建（浏览器麦克风当客户）。本设计补齐「AI 主动打电话给客户」的完整闭环：

1. **自动挂断**——通话终态（话术走完 / 客户挂断 / 无人接听 / 拒接 / 拨号失败）全部自动收线，零人工。
2. **微信 / WhatsApp 自动落盘**——通话中捕获的客户号码带渠道自动进名册。
3. **名册认领**——专员在名册页认领 → 复制号码去加 → 标记已对接。
4. **自动下一通**——campaign 串行外呼战役：名单逐个打、终态 + 5s 自动下一通。
5. **官方 SIP 外播对接**——按 LiveKit 官方外呼两段式接入；本周期跑 **mock 档模拟联调**（真语音客户），real 档（真运营商 trunk）代码就位、即插即用。

## 2. 决策记录（用户拍板 2026-09-12）

| 决策点 | 拍板 |
|---|---|
| SIP 落地路径 | **先做模拟联调**（系统侧按官方 SIP 协议全建好，运营商后补即插即用） |
| 外呼目标地区 | 香港（+852）——影响未来中继选型，不影响本周期 |
| 名册工作方式 | **纯人工认领**（认领→复制→标已对接；自动发 WhatsApp 消息留待下期） |
| campaign 首版节奏 | **串行最简**（同时 1 路、终态 +5s 下一通、无人接听不重拨只标记） |
| 方案 | 方案 1：官方姿势 + 双后端拨号层 + CP 战役循环（方案 2 直连 Twilio Media Streams / 方案 3 web 假外呼均否决） |
| mock 客户真度 | 真语音（TTS sidecar 现场合成客户台词进房） |
| 名册入册方式 | captured 自动入册 + 现有通话内爆闪横幅保留 |

## 3. 官方 SIP 调研结论（docs MCP + GitHub 源码交叉验证，2026-09-12）

- `livekit/sip` 自部署完整支持外呼，Apache-2.0，**原生编译可跑（`mage build`，非 Docker-only，不违「Never reintroduce Docker」铁律）**；无官方预编译二进制。
- 外呼 API：`CreateSIPParticipant`（Twirp），关键参数 `sip_trunk_id` / `sip_call_to` / `room_name` / `wait_until_answered=True` / `ringing_timeout`（硬上限 80s）/ `max_call_duration` / `play_dialtone`。
- 接通状态：participant attribute `sip.callStatus`（dialing→ringing→active→hangup）；失败：`SipCallError`（486/603→拒接、408/480→无人接、5xx→trunk 故障）。
- **官方集成姿势（外呼两段式）**：`lk dispatch create --new-room --agent-name ... --metadata '{"phone_number": ...}'` 先派 agent 进房 → agent job 内 `ctx.api.sip.create_sip_participant(...)` → `ctx.wait_for_participant(...)` → **接通后才 `session.start`**（官方 🔥 警告：响铃期 start 会让开场白播进空房，客户接起只听到尾巴）。dispatch rule 是入站专用，外呼不用。
- **挂断收尾铁律**：`CLIENT_INITIATED`/`ROOM_DELETED`/`USER_REJECTED` RoomIO 自动收 session；**`USER_UNAVAILABLE`/`SIP_TRUNK_FAILURE` 必须手动 `ctx.shutdown()`**（不接会漏 job 占满 worker）。agent 主叫挂断用 `ctx.delete_room()`（房间不删对方会一直听静音）。
- 真实联调部署硬前提：**Redis（新依赖，livekit-server 与 sip 共享总线）+ 公网 IP + 5060 与 10000-20000/UDP 公网可达**。macOS 开发机做不了真 PSTN——mock 档先行的依据。
- trunk 是长生命周期对象，建一次复用；不支持 SIP REGISTER（排除软终端式接入）；已验证运营商 Twilio/Telnyx/Plivo/Sinch 等。香港 +852 落地是运营商/合规问题，LiveKit 侧零障碍。
- 语音信箱会伪装成「接通」（200 OK）——AMD 自部署需自带 LLM/STT，本周期不做（香港场景语音信箱渗透低）。

## 4. Wave 1：名册（roster）+ 渠道化落盘

### 4.1 数据模型（`packages/business-db/bok_voice_business_db/models.py` + `deps.py build_engine()` 幂等迁移）

```python
class RosterEntry(Base):
    __tablename__ = "roster_entries"
    id: str PK                      # roster-<hex8>
    account_id: str, index
    call_id: str, index             # 来源通话（最近一次捕获）
    object_id: str, index
    channel: str                    # whatsapp | wechat
    number: str                     # 客户号码
    display_name: str               # 对象名快照
    summary: str                    # 通话摘要快照
    status: str                     # unclaimed | claimed | handled
    claimed_by: str                 # 认领人（账号名，单账号形态记 acc 标识）
    claimed_at: datetime nullable
    created_at: datetime
```

- **去重键 = object_id + channel + number**：已存在且 status != handled 的重复捕获只更新 `call_id`/`summary`/`display_name`，不重复入册；handled 后新捕获另起新行。
- SQL 可移植（过 `tests/test_db_portability.py` 门禁，禁 sqlite 专有语法）。

### 4.2 channel 数据源

`flow.py` 捕获正则的捕获组本来就区分渠道词（`whatsapp|whats app|wechat|wa|微信`）。`detect_whatsapp_signal` 结果带出 matched channel；agent 上报 `POST /api/calls/{id}/whatsapp` 的 `WhatsAppCaptureRequest` 加可选 `channel` 字段（`whatsapp|wechat`），缺省按对象 `contact_channel` 推断（含「微信」→ wechat，否则 whatsapp）。

### 4.3 API

- `POST /api/calls/{call_id}/whatsapp`（现有）：captured 时**自动 upsert 名册行**；offered 不入册（还没号码）。通话内爆闪横幅行为不变。
- `GET /api/roster?status=&channel=&account_id=`：名册列表（新到前）。
- `POST /api/roster/{id}/claim` / `POST /api/roster/{id}/unclaim`：认领/释放（claimed_by/claimed_at）。
- `POST /api/roster/{id}/handled`：标记已对接——**同步把来源 call 的 `whatsapp_status` 置 handled**（与横幅停止逻辑同源）；unclaim 后回 unclaimed。
- 摘要来源：settlement digest（`GET /api/calls/{id}/settlement` 的 digest 字段），空则退末轮客户转写。audit 全程。

### 4.4 UI（`apps/web`）

新页 `/roster`：状态/渠道筛选、认领/取消认领、一键复制号码、标记已对接、跳来源通话（CallStudio）。 nav 进「名册」入口。

## 5. Wave 2：SIP 外播编排（模拟联调档）

### 5.1 dialer 薄层（`apps/agent/agent_runtime/dialer.py`）

统一返回四态：`ANSWERED(participant) / NO_ANSWER / REJECTED / TRUNK_FAILED`。

- **real 后端**：`ctx.api.sip.create_sip_participant(CreateSIPParticipantRequest(sip_trunk_id=…, sip_call_to=number, room_name=…, participant_identity=f"sip-{number}", wait_until_answered=True, ringing_timeout=…, max_call_duration=…))`；`api.SipCallError` 按 SIP 码映射（486/603→REJECTED、408/480→NO_ANSWER、5xx→TRUNK_FAILED）。本周期代码就位、默认不启用。
- **mock 后端**：agent POST CP `POST /api/sip/mock/callee`（body：room、number、scenario、script）→ CP 派生 `scripts/mock_callee.py` 子进程（detached，同 `pregen.py` 子进程先例：单飞防叠、失败不阻主链路）→ agent `ctx.wait_for_participant(identity=f"sip-mock-{number}", timeout=响铃窗)`；超时=NO_ANSWER；participant 进房后极短窗（<1.5s）内离房且零音频=REJECTED（对齐 real 档 486 语义）。
- **拨号期不 arm 心跳**：session 未 start，结构性满足（钩子注册先于 start 的 P1 规则保持）。

### 5.2 agent 外呼模式（`agent.py` entry 分支）

- job metadata 带 `{"dial": {"to": "+852…", "mode": "mock"|"real", "call_id": …, "campaign_item_id": …, "scenario": …}}`（dispatch 时由 CP 注入）。
- 流程：进房 → `dial_outbound(...)` → **ANSWERED 才 `session.start`** → 现有开场白/心跳/话术/打断自愈全复用（A 线外呼= AI 打给客户，接通后 AI 开场白是自然时序，与官方「不向空房 greeting」警告一致）。
- 失败终态 → 上报 CP（campaign item 结果 + call disposition=no_answer/rejected/failed）→ **`ctx.shutdown()`**（官方铁律）。
- 通话中客户挂断（CLIENT_INITIATED）→ RoomIO 自动收 → 现有 `on_close` 结算链路复用，campaign 循环靠 call 终态轮询衔接。
- **mock 档 max_call_duration 保险丝**：agent 侧定时器（real 档由 CreateSIPParticipant 承担）到点 `end_call(completed)` + `ctx.delete_room()`。

### 5.3 mock 客户（`scripts/mock_callee.py`，真语音）

- CP 派生，参数：`--room --token --identity --scenario --ring-delay --script-file`。
- 进房身份 `sip-mock-<number>`；剧本四型：
  - `answer`：ring-delay（2-4s 随机）后进房，按剧本台词用 TTS sidecar（`tts_pcm`，语言随战役）轮播客户语音（复用 `e2e_real_customer` 的台词与合成姿势），订阅 agent 音轨做断言素材；
  - `no_answer`：永不进房，进程睡到响铃超时退出（对 agent 表现为 wait_for_participant 超时）；
  - `reject`：进房后 <1s 即离房且零音频——dialer 判「接通前离房」= REJECTED（对齐 real 档 486/603 语义）；
  - `hangup_mid`：聊 N 轮（或 T 秒）后离房。
- 结束打结构化 JSON 行日志（`MOCK_CALLEE done scenario=… rounds=…`）供 E2E 断言。
- 进程生命周期：房间结束/agent 离开 → 自动退场；CP 侧记录派生 pid 便于 `bok.py down` 清理。

### 5.4 SIP 配置面（真实联调预留骨架）

- settings DB `sip` 段：`{mode: "mock"|"real", trunk_id, address, auth_username, auth_password, numbers: [], ringing_timeout_s: 30, max_call_duration_s: 600}`；`repository.default_settings` 同步默认值。
- env kill-switch：`BOK_SIP_MODE`（缺省 mock，优先于 settings）。
- web 设置页 SIP 卡片：mock/real 切换 + trunk 字段表单；real 档凭据加密存储（同 `tts.api_key` 姿势，勿提交仓库）。

## 6. Wave 3：campaign 自动下一通

### 6.1 数据模型

```python
class Campaign(Base):
    __tablename__ = "campaigns"
    id, account_id, name
    template_id, persona_id, language   # 整战役统一（三语钉死一通一语言的现有规则不变）
    status: draft | running | paused | done | stopped
    gap_seconds: int = 5                # 终态→下一通间隔
    concurrency: int = 1                # 首版钉死串行
    created_at, finished_at

class CampaignItem(Base):
    __tablename__ = "campaign_items"
    id, campaign_id, index
    object_id, phone                    # 对象 phone 带出，可覆盖
    status: pending | dialing | in_call | done | no_answer | rejected | failed | skipped
    call_id, attempts = 1, last_error
    updated_at
```

### 6.2 循环（CP 后台 asyncio 任务，reaper 同款生命周期）

- 启动扫一遍 + 短周期轮询（5s）：`running` 且无 pending → 置 `done` + `finished_at`。
- 取下一 pending item → 置 `dialing` → 现有 `create_call` 链路（object→template/persona/language、contact_phone=phone）→ `create_dispatch`（metadata 注入 dial 块 + campaign_item_id）。
- call 进入 active（agent 上报拨号结果）→ item `in_call`；call 终态（ENDED/FAILED）→ 按 disposition 落 item 结果（completed→done / no_answer / rejected / failed）。
- item 结果落定 → `gap_seconds` → 下一通。
- `pause`：当前通话跑完不再起下一通；`stop`：不掐进行中通话，标记 `stopped`。状态机迁移全部可单测（纯函数 + fake repo）。

### 6.3 API + audit

`POST /api/campaigns`（object_ids + template/persona/language/gap）、`POST /api/campaigns/{id}/start|pause|stop`、`GET /api/campaigns`、`GET /api/campaigns/{id}`（items + 进度 + 汇总）。agent→CP 上报：`POST /api/campaigns/items/{item_id}/dial-result`（answered/no_answer/rejected/failed + sip 细节）。

### 6.4 UI（`/campaigns` 页）

新建（对象多选 + 话术 + 人设 + 语言 + gap）、启动/暂停/停止、进度表（每对象状态 + 跳 CallStudio 监听当前通话）、结果汇总（接通率 / 号码捕获数 / 平均时长）。进行中通话可直接进 supervisor 视图旁听。

### 6.5 自动挂断终态口径（收口清单）

| 终态 | 触发 | 已有/新增 |
|---|---|---|
| 话术走完 | farewell 念完 → 14s end_call | 已有，复用 |
| 静默超时 | 12s 沉默上限收线 | 已有，复用 |
| 客户挂断 | SIP participant 离房（CLIENT_INITIATED）→ RoomIO 收 → on_close 结算 | 已有链路，外呼下复用 |
| 无人接听 | ringing_timeout（默认 30s，≤80s 硬上限） | 新增（dialer 超时 + disposition=no_answer） |
| 拒接 | real: SipCallError 486/603；mock: reject 剧本（接通前离房） | 新增（dialer 映射 REJECTED） |
| trunk 故障 | SipCallError 5xx | 新增（real 档） |
| 时长保险丝 | max_call_duration_s（默认 600s） | 新增（mock=agent 定时器 / real=API 参数） |
| 拨号失败收尾 | REJECTED/NO_ANSWER/TRUNK_FAILED → ctx.shutdown() | 新增（官方铁律：后两态 RoomIO 不自动收） |

## 7. 测试与验收

- **单测**：roster upsert 去重/状态机/handled 联动；channel 推断（含 contact_channel 缺省）；dialer SipCallError→四态映射；campaign 状态机迁移（fake repo）；mock_callee 参数解析。
- **门禁**：`tests/test_db_portability.py`（新表新列可移植）、`tests/test_cantonese_terminology.py`、pytest 全绿、`python -m compileall -q`、web `npx tsc --noEmit && npm run build`。
- **E2E（mock 档全链路）**：`scripts/e2e_campaign.py`——建 3 对象战役（1 接通走完话术自动挂断 / 1 无人接 / 1 接通即挂）→ 断言：自动下一通按序发生、终态 disposition 正确、接通轮 captured 后名册自动入册、campaign 汇总正确。真语音 mock 客户（TTS sidecar），E2E 时段错峰跑（GPU 竞态现有约束）。
- 验收口径：串行战役跑完 3 对象全程零人工干预；名册从捕获到认领到已对接闭环可操作。

## 8. 风险与边界

- **GPU 竞态**：mock 客户 TTS 合成与 agent 同卡——E2E/演示错峰（现有约束同款）；mock 客户合成完缓存 wav 再进房可减压（实现时取简）。
- **真 SIP 切换**（下期）：Redis 新依赖 + 公网 IP + 大段 UDP 属部署项；dialer real 后端 + trunk 配置面本周期就位，切真只改配置。
- **multi-worker 端口**：mock_callee 是 rtc 客户端不是 worker，无端口冲突；但 `bok.py down` 清理需覆盖派生进程（pid 登记）。
- **单账号形态**：claimed_by 记账号标识；多坐席/RBAC 留给 thin-node SaaS 分期（spec 2026-09-10 已有 turns 说话人账本与两级管理员设计）。
- **CDP/AEC**：外呼客户=电话侧，无浏览器回声问题；CallStudio 旁听沿用现有 supervisor token。

## 9. 明确不做（YAGNI）

- 自动发 WhatsApp/微信消息（需 WhatsApp Business API + 模板审核，下期）。
- 无人接听重拨、并发多路、时段限制（首版串行最简拍板；状态机预留 attempts 字段）。
- AMD 语音信箱检测（自部署需自带 LLM/STT 验质量，香港场景渗透低）。
- DTMF/IVR 导航、冷热转接（A 线全流程 AI 拍板不变）。
- 大陆 +86 线路合规调研（目标香港；落地时独立调研）。
