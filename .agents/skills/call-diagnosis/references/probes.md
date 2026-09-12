# 探针与验收用法

**铁律：探针只可在无通话时跑**（与真实通话抢 GPU，实测拖慢 TTFT/ASR）。跑前 `python tools/bok.py status` 确认栈在、无进行中通话。

## 回复质量（话术渐进披露验收）

```bash
.venv312/bin/python scripts/probe_reply_quality.py [--call call-8fa17d2b]
```
用真实通话轮次回放新 prompt 打本地 LLM，断言 ≤80 字/不照念(<0.7)/不复读(<0.9)。结果 JSON：`scripts/.probe_reply_quality.json`。

## 快语速吃字/回声守卫

```bash
.venv312/bin/python scripts/probe_fast_speech.py
```
1.4× 速 TTS 推流验首字存活 + courier=拼多多 验真答案不被词表守卫丢。**注意探针要推尾静音**（VAD END 依赖静音帧）。

## 品牌/热词

```bash
.venv312/bin/python scripts/probe_brand_words.py
```

## 延迟/缓存

- `scripts/measure_latency.py` / `measure_prompt.py`：PERCEIVED_MS 口径复测。
- `BOK_LLM_MSG_DEBUG=1`：逐请求逐消息 sha1 指纹（定位前缀分叉消息）。
- `scripts/llm_cache_report.py <worker.log>`：cached 分布汇总。
- 探针打 :1235 的 model 字段**必须用本地路径**（repo id 触发 HF hub 解析）。

## E2E（真机，需全栈+模型）

```bash
E2E_ONLY=cantonese .venv312/bin/python scripts/e2e_trilingual_livekit.py
.venv312/bin/python scripts/e2e_barge_in.py      # 打断恢复
.venv312/bin/python scripts/e2e_edge_cases.py    # 静音/超短音频
.venv312/bin/python scripts/e2e_real_customer.py # 真客户式多轮（用户验收首选）
```

E2E 句形铁律：话音句不带逗号/大换气（>0.45s 停顿劈轮）；<10 字单口气句结构性免疫劈轮。

## 栈操作

```bash
python tools/bok.py serve    # 分离式：desktop ready 后退出属正常
python tools/bok.py status   # 11 行含 worker 三行——worker 缺=不能用
python tools/bok.py down     # 后跑 ps aux | grep agent_runtime 确认 0
```
启动验证：`lsof -nP -iTCP:8081 -iTCP:8082 -iTCP:8083 -sTCP:LISTEN` 应见 3 个 python。
