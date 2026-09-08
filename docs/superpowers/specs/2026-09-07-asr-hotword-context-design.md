# ASR 热词/Context 偏置设计（Qwen3-ASR 官方 context 通道）

日期：2026-09-07 ｜ 状态：已按推荐选项定稿（用户未逐题作答，采用标注的推荐项）

## 背景与目标

E2E 实证（edge-cases E2）领域词误听：fixture「我個**單號**係三七七八九零」的前导「我個單號係」被稳定误听（main 基线同样失败）。Qwen3-ASR 官方支持 customizable context——HF 模型卡「Context / hotwords」节：free-form 词汇表以 **system message** 喂入（例：`Vocabulary: Quilter, apostle, gospel.`），与 language 强制可叠加。本机 mlx_audio 的 `generate(..., system_prompt=...)` 就是这条通道（`_build_prompt` 把 system_prompt 拼进 system turn，language 前缀照旧）。

目标：把话术模板与对象信息中的领域词汇喂给 ASR，降低「單號/賠償/平台名」类误听；延迟增量有护栏；随时可关。

## 方案对比

- **A 只静态领域词表**：零数据依赖，但对象差异（平台名）吃不到。
- **B 静态词表 + 对象字段派生（选定）**：覆盖「对话+话术」主要误听源，零 DB 迁移。
- **C 再加 templates.hotwords 字段**（DB 迁移 + CP API + web 表单）：话术级定制最强，二期再做。

## 设计

### 1. sidecar（services/qwen3-asr-sidecar/app.py）

- `/api/start` 新增 query 参数 `context: str = ""` → 存 `session["context"]`。
- mlx 后端三个 generate 调用点（`_partial_mlx`、finish 整句、`_try_incremental_finish`）统一传 `system_prompt=session.get("context") or None`。
- kill-switch：`QWEN3_ASR_CONTEXT=0` 时忽略 context（防御）。
- transformers/vllm 后端 v1 不接（生产是 mlx）；official `qwen_asr` 的对应参数留注释口，不报错。

### 2. agent（agent.py + livekit_plugins.py）

- **热词组装**：agent.py 顶层纯函数 `asr_hotword_context(lang: str, object_card: dict | None) -> str`：
  - 静态表 `_ASR_HOTWORDS`（三语各一套，行业词：單號/運單/賠償/運費/專員/集運/時效/上門/WhatsApp/微信…zh/en 对应简体/英文）；
  - 对象派生：`object_card` 的 `platform`/`contact_channel` 等文字字段值（淘寶/京東/Temu…）；
  - **数字串不进热词**（幻听数字风险；下游已有 known-number 过滤兜底）——digit-dominant token 过滤；
  - 拼官方示例风格 `Vocabulary: w1, w2, …`；总长护栏 ≤120 字符（超长截断），防 partial 解码 prefill 膨胀。
- **传递**：`Qwen3ASRSTT.__init__` 加 `hotword_context: str = ""`；`_start_session`/`/api/start` 请求带 `context` 参数。装配点 agent.py:1096 传入 `asr_hotword_context(...)`（`flow_ctrl`/`object_card` 均在作用域）。
- **kill-switch**：`BOK_ASR_HOTWORDS=0` 时装配传空。
- B 线 interpret v1 不动（范围收敛）。

### 3. 延迟预算

system_prompt 进每次解码的 prefill；partial 每 ~700ms 一次全量 generate。60 token 级 context 在 1.7B mlx 上估 +30-80ms/次——验收以 ASR_MS 实测 p50 增量 ≤80ms 为护栏，超了减词/关开关。

### 4. 验收

- 单元：`asr_hotword_context`（语言分表/对象注入/数字过滤/长度截断/kill-switch）+ sidecar context 存储与 generate 透传（fake model 断言 kwargs）。
- E2E：edge-cases E2 场景重跑——软偏置不保证修复误听，验收线=**不劣化 + ASR_MS 达标**，误听改善为 bonus。
- 文档：AGENTS.md 加热词条目；RUNTIME_TOPOLOGY ASR 段补 context 通道。

## 错误处理

- sidecar context 缺失/空 → system_prompt=None（行为同现状）。
- agent 侧对象字段异常 → try 包裹回退纯静态表。
- 全链路两级 kill-switch（agent `BOK_ASR_HOTWORDS` / sidecar `QWEN3_ASR_CONTEXT`）。
