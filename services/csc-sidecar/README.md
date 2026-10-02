# CSC sidecar（中文受限纠错，:8792）

本地 **zh-only** 纠错 sidecar：`shibing624/macbert4csc-base-chinese`
（102M，Apache-2.0，MacBERT 掩码语言模型，逐字符等长改写）。FastAPI 单文件服务，
与 ASR(:8787)/TTS(:8788)/embed(:8789)/mt(:8790)/laya(:8791) 同族拓扑，只绑
`127.0.0.1`（通话转写不出本机）。

## 定位

三语纠错架构里的**模型层**。ASR 出来的普通话转写里偶发近音/形近错字
（方按→方案、赔尝→赔偿、京冬→京东），本服务做一次保守纠错，**只为 zh 锦上添花**。

**粤语 = 服务端直接拒**。上一轮全量实测（`/tmp/csc` harness）结论：

| 面 | 结论 |
|---|---|
| 普通话正确句（4 条） | **0/4 误改** —— 可用 |
| 普通话可纠错句 | 命中高置信错字（0.9 档下 `方按→方案` 等），**召回低**（只挑得动高置信的） |
| 粤语 | **碰粤语必漂移**（`係→系`、`哋→跌`），且治理上不可接受（粤语是一等通话语言，纠错层不准改它一个字）|

因此收益是「锦上添花」：宁可漏纠，也绝不误改。`lang != "zh"` 一律 200 直接返回原文；
即使调用方错判成 zh，服务端还会用粤语特征字再拦一道（见守卫 d）。

## 端点

### `GET /health`

```json
{"ok": true, "version": "0.1.0", "model": "shibing624/macbert4csc-base-chinese",
 "model_loaded": false, "device": null, "loaded_ms": null, "load_error": null,
 "threshold": 0.9, "max_edits": 2, "max_chars": 200}
```

懒加载：起服务**不**拉模型（不被 102M 权重阻塞），首个 `/correct` 才加载；
加载完成前 `model_loaded=false`。加载失败 `load_error` 带人话，进程不退（`/correct` 503）。

### `POST /correct`

请求：`{"text": "...", "lang": "zh", "max_edits": 2, "threshold": 0.9}`
（`max_edits`/`threshold` 可选，缺省取 env；`lang` 必传语义上只认 `"zh"`。）

```json
{"text": "这个方案需要我承担运费吗，单号AB1234",
 "edits": [{"pos": 3, "from": "按", "to": "案"}],
 "changed": true, "skipped_reason": null,
 "latency_ms": 11.1, "threshold": 0.9}
```

跳过时 `text` = 原文、`edits` = `[]`、`skipped_reason` 为人话原因：

| `skipped_reason` | 触发 |
|---|---|
| `lang_not_zh` | `lang` 不是 `"zh"`（含 `cantonese`/`en`/缺省） |
| `empty` | 空串 |
| `cantonese_markers:<字>` | 输入含粤语特征字（守卫 d） |
| `length_mismatch` | 掩码回填后字符数不等（守卫 b，理论不触发） |
| `over_budget` | 改动处数 > `max_edits`（守卫 c） |

`text` > 200 字 → **400**（拒；本服务只处理短转写片段）。参数非法（`text` 非串 /
`max_edits` 非 ≥0 整数 / `threshold` 非数）→ 400。

## 守卫链（每条都是显式代码，不过 = 放弃纠错返回原文）

| 守卫 | 做法 | 为什么 |
|---|---|---|
| **a. 冻结 span 掩码** | 推理前把连续数字串（阿拉伯 + 中文数字词）、拉丁 run、既有标点**逐字符**替换成占位符（`\uE000`→BERT `[UNK]`）并记录位置；推理后强制回填原文 | 数字/标点/拉丁**物理上不可能被改**（数字零降级铁律）；逐字符替换保持等长、原位可回填 |
| **b. 等长断言** | 掩码回填后字符数 ≠ 原文 → 整条放弃返回原文 | 对齐失败绝不冒险 |
| **c. 编辑预算** | 改动处数 > `max_edits`（默认 2）→ 放弃返回原文 | 防模型一口改多、乱猜 |
| **d. 粤语特征字防漂移** | 输入含 `哋嘅喺唔係咁冇嚟咗㗎瞓俾睇啲嘢` 之一 → skip | 调用方判错语言时服务端再防一手，杜绝粤语被漂移 |

> 对齐用 **offset_mapping**（不是 `decode().split(' ')`）：中文 BERT 词表会把
> `AB1234`、`1,234.56`、`〇〇` 并成多字符 token、把占位符并成 `[UNK]`，朴素 decode
> 长度对不上会整条放弃。offset 方案能把每个单字符 token 精确定位；多字符/`##`
> 续接 token 一律跳过（保留原字符）——保守即安全。
>
> 保持 **fp32**：阈值 0.9 是在 fp32 下标定的，转 fp16 会动分布、破坏「零误改」标定。

## 实测数字（MPS，fp32，thr=0.9）

- 单句延迟：**p50 ~11ms / p90 ~13ms**（selftest n=9；首句含 MPS 编译 ~167ms）。
- 模型加载：HF 缓存命中 ~4s，首次冷 ~14s；权重 102M。
- harness 20 条（`/tmp/csc/testset.json`）thr0.9：exact 6、edit_recall 2/16、
  全部误改落在粤语句上；**普通话正确句 4/4 零改动**。
- 自检：`selftest.py` **34/34 全绿**（守卫链纯函数 + skip 闸 + 真模型 8 句 + combo）。

## 运行

```bash
services/csc-sidecar/setup-macos.sh          # 建 .venv（python3.12；torch ~2GB 磁盘）
# 手动起：
services/csc-sidecar/.venv/bin/uvicorn app:app \
  --app-dir services/csc-sidecar --host 127.0.0.1 --port 8792
```

自检：

```bash
# 真模型（首次会从 HF 下载到 app-data 缓存；HF_HOME 指到已有缓存可离线复用）
services/csc-sidecar/.venv/bin/python services/csc-sidecar/selftest.py
# 只验守卫链纯函数 + health（不加载/下载模型）
CSC_DISABLE_LOAD=1 services/csc-sidecar/.venv/bin/python services/csc-sidecar/selftest.py
```

## 配置与环境变量

| env | 默认 | 说明 |
|---|---|---|
| `CSC_PORT` | `8792` | 端口 |
| `CSC_THRESHOLD` | `0.9` | 逐字接受阈（实测 0.9 为零误改档；下调迅速引入假纠）|
| `CSC_MAX_EDITS` | `2` | 编辑预算（守卫 c）|
| `CSC_MAX_CHARS` | `200` | 超长 400 阈值 |
| `CSC_HF_HOME` / `HF_HOME` | `~/Library/Application Support/BokVoice/models/csc-macbert` | HF 缓存根（首启从 `shibing624/macbert4csc-base-chinese` 下载）|
| `CSC_MODEL_DIR` | 空 | 显式本地检查点目录（离线/打包，优先于 HF）|
| `CSC_DISABLE_LOAD` | `0` | `1`=不加载模型，`/correct` 503、`/health` 诚实报 `model_loaded=false` |

## 粤语？自训后话

粤语纠错不在本服务能力内（模型漂移不可控），路线是**自训专用纠错模型**
（仓内 TTS-SFT / CUDA 部署链已有先例），本服务是 zh 面的第一步。接线、端口注册、
`bok.py` 编排由主线后续做——本目录只负责模型服务本身。
