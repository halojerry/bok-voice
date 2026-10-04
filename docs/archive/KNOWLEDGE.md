# Bok Voice — 知识库落盘、检索注入与全局蒸馏

本文说明知识从「导入」到「对话中被使用」再到「结算后沉淀/蒸馏」的完整数据流，以及各环节落盘在哪里。

## 一、知识库：落盘与检索注入

### 链路

```text
知识页「导入 Markdown」/ 直接调 POST /api/knowledge/import
  → 整篇落盘为 Markdown：<vault>/accounts/{account_id}/knowledge/{path}.md
  → 建立检索引擎索引（当前为字符匹配，重启从 vault 全量重建）
  → 通话中：客户每说完一句，Agent 用该句检索账号知识 top-5
  → 命中的片段随 system 指令注入 LLM（每轮渐进披露，最多 3 条 + 联网补充）
```

- 删除知识会**同步清理 vault 文件与索引**，不留僵尸（`/api/knowledge/{id}`）。
- 账号隔离：检索强制按 `account_id` 过滤；A 账号查不到 B 账号的知识。
- vault 根目录：启动时由 `VAULT_ROOT` 决定；桌面/源码统一为 App 数据目录下的 `vault/`（如 `~/Library/Application Support/BokVoice/vault/`）。

### 对话中如何被使用

接通对象前，Agent 会：
1. `get_call` → `get_object`（含 `template_id` → 话术模板）→ `get_persona`；
2. 用对象 `background` 预检索一次知识；
3. 每轮用户说完，用**用户原话**再检索并覆盖（`ContextState.set_knowledge`），同时把整场对话摘要维护在内存（最多 800 字）。

最终注入大模型的 system 形如：

```text
【用户语言】当前用户正在使用：粤语（广东话）。用地道粤语口语回复…
【实时检索到的资料（知识库）】
- <命中片段1>
- <命中片段2>
【联网检索到的资料（来源：Wikipedia/即时答案…）】   （可用 WEB_SEARCH=0 关闭）
【本通对话记忆】
user: …
assistant: …
```

### 已知边界

- 当前 `EmbeddingService` 用确定性 `CharHashEmbedding`（字符哈希 384 维），SQLite 单机下检索实际是**子串/字符匹配**，非语义向量；生产级语义检索（BGE/ONNX + pgvector / SQLite 向量扩展）为后续项。
- 导入是「整篇一条 chunk」，未做分块；长文档建议按主题拆成多段分别导入。

## 二、结算落盘

挂断（或 supervisor 端挂断 / room 关闭）→ `POST /api/calls/{id}/settle`（幂等）：
1. 汇总本场 turns，用**本机 LLM** 生成 `summary` + `new_topics` + `insight`（`control_plane/summarize.py`；失败回退纯指标摘要，不影响结算）；
2. 写 `settlements` 表（含 `summary`、`new_topics_json`、`global_insight_id`）；
3. `new_topics` → 追加到该对象的 `object_topics`；
4. `insight` → 追加到全局 `global_insights`；
5. 转写与结算文档落盘到 vault：

```text
<vault>/accounts/{account_id}/objects/{object_id}/calls/{call_id}/transcript.md
<vault>/accounts/{account_id}/objects/{object_id}/calls/{call_id}/settlement.md
```

> 这两类 call 文档不会被当作知识重建索引（索引只收 `accounts/*/knowledge/*`）。

## 三、全局蒸馏（谁在观察、沉淀在哪）

- 每场结算的 `insight`（statement + confidence + language）追加进 `global_insights` —— 这是「跨对象的共性观察」；
- 每场结算的 `new_topics` 追加进对应对象的 `object_topics` —— 这是「该客户的历史话题/关注点」；
- 界面：**报表 → 全局洞察**；**通话台 → 左栏历史主题**（对象维度）；通话详情右栏结算卡显示 summary 与本轮新话题。

### 为什么以前蒸馏表一直是空的（已修）

结算用的本机 LLM 地址此前只从 `settings.llm`（DB）读，而设置页保存的是空 `base_url` + 占位 `model="local"` → Summarizer 打到空地址/`model=local`（mlx_lm 会 404）→ 每次回退纯指标 fallback → `new_topics=[]`、`insight=None` → 两张蒸馏表永不写入。

修复：`summarize.py` 在 settings 缺省/占位时回退读 `MLX_LLM_BASE_URL`（默认 `http://127.0.0.1:1235/v1`）+ `MLX_LLM_MODEL`（真实模型路径）；`tools/bok.py` 启动 control-plane 时注入与 agent 一致的这两个 env。

## 四、快速验证

```bash
# 结算后查蒸馏是否落库
sqlite3 ~/Library/Application\ Support/BokVoice/bok_voice.db \
  "select id,statement,confidence from global_insights order by rowid desc limit 5;"
sqlite3 ~/Library/Application\ Support/BokVoice/bok_voice.db \
  "select object_id,topic from object_topics order by rowid desc limit 10;"

# 读端点（新增）
curl -s http://127.0.0.1:8000/api/insights
curl -s http://127.0.0.1:8000/api/objects/{object_id}/topics

# vault 落盘核对
find ~/Library/Application\ Support/BokVoice/vault/accounts -name "*.md" | head
```

## 相关文档

- 话术模板如何编辑与进入 prompt：见 [TALK_SCRIPT.md](TALK_SCRIPT.md)。
- 运行时拓扑（端口/目录/env）：见 `docs/RUNTIME_TOPOLOGY.md`。
