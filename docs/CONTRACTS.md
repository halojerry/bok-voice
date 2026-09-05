# Bok Voice 契约

## 核心类型

见 `packages/core/types.py`：`SessionManifest`、`ContextBundle`、`TurnEvent`、`CallSession`、`SettlementResult`、`UsageRecord`、`CallMode`、`CallStatus`、`SettlementStatus`、`Role`、`ObjectProfile`、`ObjectTopic`、`PersonaProfile`、`GlobalInsight`。

## Provider / 服务协议

见 `packages/core/providers.py`：

- `VADProvider`、`ASRProvider`、`LLMProvider`、`TTSProvider`（实时媒体）。
- `KnowledgeService`、`VectorStore`、`EmbeddingService`、`MarkdownSource`（知识库）。
- `BusinessRepository`（含 `create_call/get_call/update_call/list_calls/create_turn/get_turns/get_settlement/append_settlement/get_object/get_persona/list_personas`）。
- `SettlementWorker`、`ProviderRegistryProtocol`。

## Control Plane REST

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/token` | **LiveKit 官方 TokenSource endpoint 契约**：请求兼容官方 `TokenSourceRequest`（snake_case：`room_name`/`participant_identity`）与业务字段（`call_id`/`role`）；响应即官方 `TokenSourceResponse`（`{serverUrl, participantToken}`，201）。任何官方 SDK/playground 可直连。call 存在则置 `active` |
| POST | `/api/calls` | 创建通话 |
| GET | `/api/calls?status=active` | 活跃通话 |
| GET | `/api/calls/{id}` | 通话详情 |
| GET | `/api/calls/{id}/turns` | 通话轮次 |
| POST | `/api/calls/{id}/hangup` | 挂断（持久化 `ended`） |
| POST | `/api/calls/{id}/settle` | 结算（幂等 upsert） |
| GET | `/api/calls/{id}/settlement` | 查询结算 |
| GET/POST | `/api/objects` | 对象列表/新建 |
| GET | `/api/objects/{id}` | 对象详情 |
| POST | `/api/objects/import` | 批量导入 |
| GET/POST | `/api/knowledge/search` / `/api/knowledge/import` | 知识检索/导入 |
| GET/PUT | `/api/personas` | 人设列表/更新 |
| GET | `/api/personas/{id}` | 人设详情 |
| GET | `/api/supervisor/active-calls` | 活跃通话列表 |
| POST | `/api/supervisor/{callId}/join` | 主管进房：校验通话存在并签发 supervisor token（官方契约字段，`identity=supervisor-<room>`） |
| POST | `/api/supervisor/{callId}/pause-agent` | 暂停 AI |
| POST | `/api/supervisor/{callId}/takeover` | 接管（置 `escalated_to_human`） |
| POST | `/api/supervisor/{callId}/transfer` | 转人工（置 `escalated_to_human` + `disposition=transferred`） |

## Agent ↔ Control Plane 内部契约

`ControlPlaneClient`：`get_call` / `get_object` / `get_persona` / `search_knowledge` / `add_turn` / `settle`。

## LiveKit 契约

- 前端：`useSession` + `TokenSource`（官方 Session API）+ `useAgent` + `useAgentExpression` + `useTranscriptions` + `StartAudio` + `VoiceAssistantControlBar`（官方 `@livekit/components-react`）。
- **Token 契约**：CP `/api/token` 即官方 endpoint——请求体 `room_name`/`participant_identity`/`participant_metadata`（proto JSON snake_case）或业务字段 `call_id`/`role`；响应 `{serverUrl, participantToken}`（201）。`room_name`/`call_id` 缺失显式 400（不再造随机房）。
- **身份约定**：`me-<room>`（同传我方端）/`other-<room>`（对方端）/`operator-<account>-<room>`（客服操作端）/`supervisor-<room>`（主管）。角色同时写入 participant attributes `bok.role`（`me/other/operator/supervisor`）与 `bok.account_id`——代码按属性判定，字符串前缀仅作签发约定。
- **Agent 显式分发**（官方推荐，无隐式 dispatch）：A 线 worker `agent_name="bok-voice"`，operator/supervisor token 挂 `RoomAgentDispatch(agent_name="bok-voice", metadata={"call_id": room})`；B 线同传 `bok-interp-fwd`/`bok-interp-rev`，me 端 token 携带 metadata `{listen_identity, deliver_identity, source_lang, target_lang}`（精确 identity，agent 不再拼前缀）。call_id 走 job metadata，无 env 旁路。
- room 名 = call_id；Agent 从 `ctx.room.name` / job metadata 解析通话。
- **译文轨命名**：同传 agent 输出音轨名 `trans-<lang>`（`cantonese/zh/en`），承担官方属性没有的「译文语言」语义；字幕归属用官方 `lk.transcribed_track_id`。
- 未来 SIP：dispatch rule / trunk，把 `sip.callID` 等映射到 `CallSession`（attributes 通道已预留）。
