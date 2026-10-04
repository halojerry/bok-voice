# 知识库专项复盘评审（2026-09-07 QA T4）

> 范围：会话蒸馏总结 / 原始聊天记录存储 / 全局知识库总结能力。本文只评审定位，不含代码改动。

## 一、现状链路（已逐段行为验证）

```
通话结束(hangup/收线) → agent 上报 SessionReport(含权威 chat_history)
  → CP POST /api/calls/{id}/settle（幂等,重复调用返回同一结果）
    → SettlementTrigger.build_result（情绪/表达率指标）
    → Summarizer().build(turns, call, settings)——本机 LLM 生成 {summary, new_topics[], insight}
       └ 失败/超时 → 静默回退「纯指标摘要」（本轮回退产物=空串）
    → vault 落盘 accounts/{acc}/objects/{obj}/calls/{call}/transcript.md + settlement.md
    → _write_distill_knowledge: accounts/{acc}/knowledge/{object}/{call}.md + 向量索引 upsert
    → insight → global_insights；new_topics → object_topics
检索侧：CONTEXT_RAG 默认关；开时每轮 cp.search_knowledge → ContextState 尾部（2×150 字）
```

验证结果（2026-09-07 实测）：
- settle 幂等 ✓（两次调用同结果）；vault transcript.md/settlement.md 落盘 ✓；知识库累计 112 条蒸馏文件 ✓
- ✗ LLM 蒸馏在 :1235 饱和（soak 压测中）时静默回退为**空摘要**，且空蒸馏**连知识文件都不写**（`no summary & no topics → skip`）——蒸馏成败完全不可观测
- ✗ 强挂断/打断收线的通话 turns 可能整批缺失（见 QA 报告 E6/P2-3），导致 transcript.md 只有标题、蒸馏无米

## ⚠️ 定位原则（2026-09-07 用户拍板）
**对象通话默认不用知识库检索（RAG 保持默认关，不做对象绑定自动开）——通话只做沉淀；知识库 = 全局分析 + 话术优化的数据层。** 本评审原 P1-5「RAG 分级开」正式否决；检索升级只服务分析/报表场景。

## 二、问题清单（按严重度）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| P0-1 | **检索是子串匹配，非语义**：SQLite 路径 ILIKE 全表扫 + 内存路径 `query_low in text`；embedding 是 CharHash 哈希（CI 用途,非语义） | vector_store.py:24-34; embeddings.py:12-32 | 知识检索「命中=字面包含」，同义/转述全漏——RAG 开了也基本不可用 |
| P0-2 | **导入整文档单 chunk**（✅ 已修,#23 分块导入+重启 rebuild 对齐）：import_document 全文一个向量 | knowledge.py:71-77 | 长文档检索粒度=整篇，命中即整篇注入，截断 150 字后信息损失 |
| P1-1 | **RAG 默认全关 + 通话内知识不可用**：单对象只上对象档案+话术；`CONTEXT_RAG=1` 是逃生口 | agent.py:639-644 | 「知识库问答」产品能力事实上不存在，只有静态档案 |
| P1-2 | **空蒸馏静默丢弃**：总结失败→空 summary→不写知识文件、无告警无重试 | main.py:763-805 实证 | 蒸馏覆盖率不可知，知识库悄然漏通话 |
| P1-3 | **打断/强挂通话轮次缺失** → transcript.md 空壳、蒸馏无输入 | QA E6 实证（call-ab9f3e96 turns=0） | 最有价值的投诉型通话反而留不下记录 |
| P2-1 | **无全局汇总**：global_insights 只逐 call 追加，无对象级/周期性聚合视图 | models.py:141-148 | 「全局知识库总结」能力=列表，非总结 |
| P2-2 | **模板/设置无版本历史**：update 直接覆盖行；audit detail 无 before/after | models.py:57-71 | 「这场用了哪版话术」只在 call.template_id 快照了 id，内容不可回溯 |
| P2-3 | **observe() 是 no-op**：实时学习通道是占位 | knowledge.py:67-69 | — |
| P2-4 | **检索面单点**：`/api/knowledge/search` 每轮同步调用（RAG 开时）在 prefill 关键路径上 | agent.py:1330-1334 | 知识多后延迟会进通话关键路径 |

## 三、优化方向（分级路线）

**P0（检索可用性）**
1. 换语义向量：SQLite 内嵌 `sqlite-vec`/` LanceDB` 起步，embedding 用多语（粤/普/英）小模型（如 bge-m3 量化，mlx 可跑）；检索质量用「同义改写命中率」验收。
2. 导入分块：按标题/段落 300-500 token chunk + 重叠 50，metadata 带来源 path/段落号；`_write_distill_knowledge` 同步分块。

**P1（蒸馏可信 + 通话内可用）**
3. 蒸馏可观测：失败重试 1 次；空产物写 `distill_failures` 审计事件 + CP 报表面板；空摘要不得静默。
4. 轮次回填：settle 时若 turns 缺失，从 SessionReport.chat_history 回填（治 P1-3 审计缺口）。
5. RAG 分级开：默认仍关，但对象绑定知识库时自动开「该对象 scope」检索（账户隔离不变）。

**P2（总结能力）**
6. 全局汇总：新增周期任务把 global_insights 聚合为对象级/周报级 digest（LLM 摘要+指标），报表页展示。
7. 模板版本化：conversation_templates 加 revision 表，audit detail 记 before/after diff。
8. 检索移出关键路径：改为会话装配时一次性按对象拉知识入静态前缀（对象知识本来就小），或后台预取。

## 四、验收口径建议
- 检索：三语各 20 条改写问句命中率 ≥80%（子串基线约 30%）。
- 蒸馏：7 天蒸馏覆盖率 ≥99%，空摘要告警=0 静默。
- 回放：任意 call_id 5 分钟内可还原「轮次文本+每轮延迟+当版话术+蒸馏产物」。
