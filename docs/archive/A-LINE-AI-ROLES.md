# A 线 AI 职能图（实时应答 / 挂断分析 / 沉淀学习）

> 一页纸口径：AI 在 A 线做什么、数据流向哪、落在哪张表。
> 方向定调：A 线是 **AI 主动外呼**（campaign 名册派发 → SIP 拨出 → LiveKit 实时对话），不是坐等客户发消息。
> 详档：[RUNTIME_TOPOLOGY.md](RUNTIME_TOPOLOGY.md) / [ARCHITECTURE-MAP.md](ARCHITECTURE-MAP.md)。

## ① 实时应答（通话中，每轮）

```
客户说话 → VAD 判停 → ASR（四层热词+音近润色）→ 六层漏斗（命中即短路）→ TTS 流式出声
```

漏斗优先级（上到下）：

| 层 | 内容 | 为什么存在 |
|---|---|---|
| REFUSE 收线 / DEFER 短应承 | 拒绝即停、社交拖延不推进 | 不硬推 |
| say 直念步 | 合规内容（通知/赔偿档）逐字播出，不过 LLM | 数字零降级、措辞钉死 |
| 分支动作 | 运营配「这句话就照这样做」：收线/转人工/跳步/留本步 | 画布可配 |
| 意图图 graph | 关键词/判据触发跳步、播罐头、打铃转人工 | 运营可控 |
| QA 快路 | 词条命中播预生成罐头音频 | 零 TTFT、同音色 |
| **自由 LLM** | 4B 本地流式生成（五车道可路由云端） | **接住其余一切** |

占比（真实流量）：罐头/直念命中 10-40%，**其余 60-90% 是 LLM 现场生成**——LLM 是承重地板，罐头是加速层，不是备选关系。

人感工程（都在这条链上）：墙钟首声 p95 1.7s / PERCEIVED p50 ~1.9s（预算 3000ms）；首 chunk 早发（攒 10 字即送不等整句）、换气注入、(emm)/停顿标记、跨轮复读守卫；每通语言钉死（zh/cantonese/en，中途不切换）。

读写面：
- **读**：`conversation_templates`（agent 恒吃 published_json 冻结版）、`qa_entries` + 罐头音频、`intent_rules`、`object_profiles`（对象变量/发音标注）
- **写**：`turns`（每轮 gen/provider=车道溯源、template_step）、`call_sessions.assist_status`（打铃）、通话中 WA 收号 → `call_sessions.whatsapp_status/customer_whatsapp`

## ② 挂断分析（挂断单点）

```
挂断 → 意向事实账本（12 键：verdict 计数/nudge/storm/graph_notifies…）
      × intent_rules（两级规则表，确定性比对，无 LLM）
      → disposition 覆盖 + intent_code
settle 车道（9B）：通话纪要 + 用量结算；SMS 挂断钩子（骨架，enabled 才发）
```

- **落库**：`call_sessions.disposition / intent_code`、纪要、`usage_records`、`settlements`
- **去向**：dashboard/campaign——disposition 驱动重拨（`no_answer/rejected/failed` 三坏值不重拨，其余=接通、item=done）
- **跟进工单（通话中就在发生）**：judge 判到查单/投诉/跟进 → `call_followups` 建单（kind=complaint/track_order/followup，status open→人工办结）+ agent 播确认语——这是「下一步跟进」的现行实物，AI 在通话里登记、人工在后台跟办
- **人工协作面**：notify_human 打铃不暂停（AI 零空档兜话）→ supervisor 台横幅/徽标/蜂鸣 → takeover 接管

## ③ 沉淀学习（离线，全部人工勾选采纳才入库）

三路挖掘同窗同锁同缓存（qa_cluster 一键三带）：

| 路 | 产物 | 落库 |
|---|---|---|
| QA 聚类 | variants/fresh/junk 三列计划 | `qa_entries`（盖章 owner 语义） |
| 热词沉淀 | ASR 劫字根治（「你講多次」族实证） | `hotword_entries`，下一通生效 |
| LLM gap 挖掘 | LLM 轮前一条客户话语聚合 | `/api/stats/llm-gaps` → 人工 adopt → `qa_entries` |

**边界要说清**：沉淀的是**组织级话术资产**（词条/热词/变体），不是客户级画像。`object_profiles` 是外呼前的静态输入，通话挖到的客户事实（平台、顾虑、号码变更）没有回写管道。

## 现状缺口（叙述 vs 实现）

| 叙述 | 现状 | 补法 |
|---|---|---|
| 「AI 同步更新 CRM」 | 无外部 CRM 连接器；对象档案静态 | A. settle 时 9B 顺手抽结构化字段回写 `object_profiles`（车道现成，最值） |
| 「给出下一步跟进建议」 | 有实物但形态是**工单登记**（通话中）+ disposition 驱动重拨（挂断后），无挂断后生成的「建议」文本 | B. 纪要尾部 next_action 字段化，dashboard 呈现（小活） |
| 「素材库回复」 | 罐头音频 + QA 词条 | 已落地 |
| 外部 CRM 集成 | 不存在 | C. webhook/outbound 同步层，短信 provider 骨架是先例；有真实客户需求再动 |

## 一句话分工

- **AI** = 实时语音应答（LLM 主体 + 罐头/直念加速与合规）+ 挂断分析（disposition/纪要/工单）+ 离线学习（挖话术资产）
- **对象库/账本** = 沉淀面（现状全本地）；真「CRM 回写」= 缺口 A 待排期
