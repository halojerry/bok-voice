# Laya decision sidecar (:8791)

本地意图/流程判定 sidecar：`aac6fef/laya-multilingual-mlx`（0.4B 双向编码器，
非自回归，一次前向出一判定）。FastAPI 单文件服务，与 ASR(:8787)/TTS(:8788)
同族拓扑。评估数据与全部坑见 `docs/LAYA-EVAL.md`。

**端口是 8791 不是 8789**：8789 是 W1b bge-embed sidecar 的既定端口（健康面、
serve 接线、语义车道），2026-09-26 用户拍板 Laya 让位改用家族下一个空位。

## 端点

- `GET /health` → `{ok, model, loaded_ms, load_error, kill_switch}`。
  模型加载失败 `ok=false` 但进程不退（调用方回落自己的 LLM 判定链）。
- `POST /v1/decide`：

  ```json
  {
    "state": "<任意长文本，判定上下文>",
    "questions": {
      "<qid>": {"type": "choice", "instructions": "...", "criteria": ["A", "B", "C"]}
    },
    "confidence_floor": 0.5
  }
  ```

  ```json
  {
    "answers": {"<qid>": {"choice": "A", "probabilities": {"A": 0.9}, "confidence": 0.71, "below_floor": false}},
    "state_truncated": false,
    "state_tokens": 128,
    "latency_ms": 9.7
  }
  ```

  - 多 question 一次 batch（实测 8 问 ~57ms）；单问异常该 qid 返 `{"error": "..."}` 不炸批。
  - 只支持 `type=choice`（score/noul 实测不可用，收到 400 人话）。
  - **截断纪律在本服务单点执行**：state 超 `LAYA_STATE_TOKEN_BUDGET`（默认 800
    token）保头截断、置 `state_truncated=true`、日志 warn——上游 1024 硬顶是
    静默截尾（客户原话在尾部会整段消失），调用方免心智。`state_tokens` 恒报
    入参原文的真实 token 数。**判定关键原文放 state 头部**。
  - `below_floor = confidence < confidence_floor`：低置信轮调用方回落 9B 判定链。
  - 并发不串行化（实测 4 线程 199 q/s 单检查点共享）。

## 路径解析与开关

- 模型：`LAYA_MODEL_DIR`（显式检查点目录）> app-data `models/aac6fef--laya-multilingual-mlx`
  （`python tools/bok.py download --only laya` 落点）。两处皆无 → `/health ok=false`
  带人话。
- `BOK_LAYA_JUDGE=0`：`/v1/decide` 一律 503（进程可起但不服务；agent 侧同闸双保险）。
- `LAYA_STATE_TOKEN_BUDGET`（默认 800）/ `LAYA_OPTIONS_WARN`（默认 10，选项多于
  此打 warn，评估结论 3-8 选项可靠）/ `LAYA_WARMUP`（默认 1，启动时吃掉首调编译）。

## 运行

```bash
services/laya-sidecar/setup-macos.sh        # 建 services/laya-sidecar/.venv
python tools/bok.py download --only laya    # 权重 ~690MB FP16
# 随栈（opt-in，默认不拉起）：BOK_LAYA_JUDGE=1 python tools/bok.py serve
# 手动：
services/laya-sidecar/.venv/bin/uvicorn app:app \
  --app-dir services/laya-sidecar --host 127.0.0.1 --port 8791
```

离线测试（无 laya-mlx 依赖，mock 模型）：

```bash
services/laya-sidecar/.venv/bin/python -m pytest services/laya-sidecar/test_laya_sidecar.py -q
```
