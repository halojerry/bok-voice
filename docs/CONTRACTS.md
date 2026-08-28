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
| POST | `/api/token` | 获取 LiveKit token + room；若 call 存在则置为 `active` |
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
| POST | `/api/supervisor/{callId}/join` | 主管进房 token |
| POST | `/api/supervisor/{callId}/pause-agent` | 暂停 AI |
| POST | `/api/supervisor/{callId}/takeover` | 接管（置 `escalated_to_human`） |
| POST | `/api/supervisor/{callId}/transfer` | 转人工（置 `escalated_to_human` + `disposition=transferred`） |

## Agent ↔ Control Plane 内部契约

`ControlPlaneClient`：`get_call` / `get_object` / `get_persona` / `search_knowledge` / `add_turn` / `settle`。

## LiveKit 契约

- 前端：`LiveKitRoom` + `useVoiceAssistant` + `BarVisualizer` + `useTranscriptions` + `RoomAudioRenderer` + `VoiceAssistantControlBar`（官方 `@livekit/components-react`）。
- room 名 = call_id（`/api/token` 映射）；Agent 从 `ctx.room.name` 解析通话。
- 未来 SIP：dispatch rule / trunk，把 `sip.callID` 等映射到 `CallSession`。
