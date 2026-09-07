# 语音体验评估批次报告（2026-09-07，专项 C）

> 栈：va-qa @ main；每相位 10 通真实粤语 E2E（真实 token），指标=agent.log 的 TTS_FIRST_AUDIO_MS（含等 LLM 首句文本时间，跨相位口径一致可比）。

## C1 MiniMax turbo A/B（speech-2.8-hd vs speech-2.8-turbo）

| 相位 | 样本 | p50 | p95 | min | max | E2E |
|---|---|---|---|---|---|---|
| turbo | 46 | **224ms** | **475ms** | 190 | 564 | 10/10 PASS |
| hd | 47 | 309ms | 535ms | 216 | 643 | 10/10 PASS |

- turbo 首包快 **~85ms（p50，-27%）**；p95 快 60ms。差距真实但比预期小——bidi 暖连接池已把握手摊薄，剩余差异是模型推理本身。
- **结论建议**：默认保持 hd（音质优先），turbo 作为 `MINIMAX_MODEL` 环境一键选项保留；若业务要极致首包可切 turbo，需人工盲听对比音质后定。

## C2 dev sidecar 监督

`bok.py monitor` 子命令已实现并随 serve 自动拉起（BOK_MONITOR=0 关）：每 10s 探测 8787/8788/1235/8790，失活即重跑幂等 `up`（60s 冷却）；down 按 pidfile 清除。QA 期间「sidecar 被杀→通话硬失败」不再需要人工干预。

## C4 情绪标签试点：**止损**（判据触发）

设计：prompt 要求 4B 每轮开头输出白名单标签（[关切][抱歉][耐心][开心][严肃]），TTS 侧流→流剥除并打点。
实测（5 通，24 条回复）：**白名单标签 0 次出现**；模型自造集外标签（如「内心」），未被剥除→被 TTS 念出（音频转写出现「内心好明白」「nội tâm」）——标签外溢污染音频。
**处置**：试点代码保留但 `EMOTION_TAG_PILOT` 默认关（生产零影响）；止损成立——4B 在粤语客服语境下无法稳定输出受限标签集。后续若重启此方向，先解决受限生成（grammar/少样本微调）再接 voice_setting。

## C3 长稳 soak（拉长批）

另起 120 通后台运行中（结果与 RSS 趋势见 /tmp/qa-soak-samples.tsv；40 通基线：全平面无泄漏迹象）。

## C5 官方 audio turn detector 对比

HF 当前不可达（v1-mini 模型需下载）——暂缓，网络恢复后补。

## 工程注记
- TextTransforms 契约=AsyncIterable[str]→AsyncIterable[str]：text→text「函数」会把流对象当文本，整条 TTS 静音（sentences=0 canceled=1）——C4 踩坑已文档化，transform 必须流→流生成器。
