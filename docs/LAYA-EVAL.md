# Laya 决策引擎调查与落位评估（2026-09-26，本机实跑验证）

> 调查方式：web 检索真身 + laya-mlx 本机实装（/tmp 隔离 venv，未动仓库环境）+ 三套真实判定形状
> 人工金标实测（延迟/准确率/截断）。执行：subagent 长任务，主会话审查归档。
> 背景：GitHub 趋势上的 0.4B 非自回归决策引擎，评估放进 Bok Voice 哪个节点。

## 1. 真身档案

- **上游**：Convai Innovations（HF `convaiinnovations/laya*`）；**MLX 运行时 = 社区移植
  [mizorewww/laya-mlx](https://github.com/mizorewww/laya-mlx)**（~6.4k 星，Apache-2.0，PyPI
  `laya-mlx` 0.2.0）——非官方，权重逐参数校验转 MLX FP16。
- **架构**：双向编码器（非自回归）——multilingual 档=mmBERT-base 322M + 2 层 decision
  transformer + 逐选项 [MASK] 打分头。单次前向出判定，batch 内每问题一行。
- **三问型契约**：`choice`=选项概率分布；`score`=量级概率+期望分（最弱原语）；`noul`=P(true)。
  返回含 `confidence`（1-归一化熵）。
- **体积/上下文**：multilingual ~690MB FP16（无 4/8bit 量化档）；**上下文硬顶 1024 token**
  （multilingual 档），超长=静默截尾。
- **许可**：Apache-2.0 商用可。训练仅 RLCD ~5 GPU 小时，官方明说 zero-shot 接近随机、
  **微调才是预期用法**（与我们判定形状零样本已可用并不矛盾，见下）。
- 自报对比 Jev：typed-decisions 0.766 vs 0.727，p50 32.8ms vs 236-276ms（Jev 数字为第三方发表值）。

## 2. 本机实测（Mac 48GB / MLX 0.32.2 / laya-multilingual-mlx）

### 延迟（judge 形状单问题）

| 指标 | Laya | 本地 4B :1235 | 9B judge :1237 |
|---|---|---|---|
| 暖态 p50/p95 | **9.7 / 10.2 ms** | ~290ms | 0.6-2s |
| 批量 8 问一次调用 | 57.6ms（7.2ms/问） | 需 8 次生成 | 需 8 次生成 |
| 4 线程并发 | 199 q/s 无串行化 | 单实例互斥 | 单实例互斥 |

### 准确率（人工金标，4B 同题在线对照）

| 判定形状 | Laya | 4B 对照 | 备注 |
|---|---|---|---|
| A. flow judge 三选一 ×8 | 6/8 | 6/8 | 各错一题；A1「他这个就过来了。」两代同错 CONFIRM；**miss 的置信度 0.30-0.38、命中最高 0.91——置信度天然可作回落门** |
| B. intent judge 多选一/NONE ×8 | **8/8** | — | 概率极锐（0.99+）；「你哋係咪呃人嘅」0.999 命中 |
| C. 挖掘聚类 variant/new/junk ×8 | 4/8 | 4B 现役 | variant 全灭；碎片「啊」→new(0.997)=碎片直接入库污染，不可接受 |

### 上下文截断（关键坑）

state 放 600 字（384 tok）内正常；1200 字（729 tok）判读开始翻转；**2000/3000/4500 字三种输入
输出逐字节相同**——硬顶 1024 且截尾（客户原话通常在尾部=整段静默消失，无警告）。
我们 judge prompt 1-3k token 不能直接投喂，必须裁剪到 ≤800 token 且把客户原话放头部/近端。

其它实证坑：选项多到 20 个时每选项描述被截到 ~10 字（3-8 选项安全）；`noul` 跟选项标签走；
`score` 排序失真不可作相似度；英文档对非拉丁文字 0.000 且高置信（必须用 multilingual 档）。

## 3. 逐节点落位判定

| 节点 | 判定 | 理由 |
|---|---|---|
| **intent judge**（模糊轮背景批判） | **✅ 第一落位，首推** | choice 原语原生形状、实测 8/8、概率锐利；9B 批量判（预算 20s、与主回复抢 GPU）→10ms 边缘判定，judge 窗口 miss 大幅收窄；错判后果轻（关键词层兜底仍在） |
| **flow judge**（A 线每轮） | ⚠️ 第二落位·双级判定 | 快 30-300 倍但准确率与 4B 打平（各错一题）；姿势=Laya 首判+置信门（conf≥0.5 采纳，低置信回落 9B）——置信度与对错强相关是实测事实；须先过 offscript/reply_quality 实弹 |
| 挖掘聚类 | ❌ 保持 4B | 4/8，碎片误入库直接伤 QA 快路 |
| offscript 质量旗/回声/热词等 | ❌ 不引入 | 已有确定性方案，ML 反而引入不确定性 |

**集成形态**：独立决策 sidecar（:8791 与 ASR/TTS sidecar 同族拓扑），不进 A 线 worker 进程
（0.2.0 早期依赖不进关键路径、崩溃可隔离）；模型路由 judge 车道加第三种 kind=`laya`；agent 薄契约
`POST :8791/v1/decide {state(≤800tok), questions{type,instructions,criteria}, confidence_floor}` →
answers 带 `below_floor` 旗，低置信回落现有 9B 链路=纯加速旁路零行为回归；kill-switch
`BOK_LAYA_JUDGE`（入 `_FORWARD_ENV`——D14 教训）。

## 4. LLaSA 快评（名字撞车防混淆）

HKUSTAudio Llasa-1B/3B/8B（LLaMA+XCodec2 TTS）：**CC BY-NC 4.0 禁商用**直接排除；无粤语支持
声明、16kHz、无 MLX 运行时——TTS 节点无一席之地，粤语结论与 S2S_ROADMAP 一致。

## 5. 一句话结论

Laya 不是更聪明的判官（0.4B 在模糊轮与 4B 同错、聚类完败），是**快 30-300 倍且带校准置信度的
判官**——价值=把判定从「占生成式 GPU 队列 0.6-2s」变成「10ms 边缘判定+置信门」，第一落位
intent judge，第二落位双级 flow judge，其余不动。
