# S2S 路线图（本地端到端语音模型评估）——2026-09-05

结论：**本地 S2S 当前无法覆盖粤语输出**，A 线主战场仍是「级联链路提速」（本仓
W0 KV-cache 命中率工程 + W1 打断自噬修复）；普通话/英语可在后续用 MiniCPM-o 4.5
（llama.cpp-omni）做 S2S 试点对照。

## 调研要点（出处见文末）

| 模型 | 粤语 | 流式/全双工 | Mac 可跑 | 备注 |
|---|---|---|---|---|
| Qwen3-Omni 30B-A3B | 粤语仅作输入/**输出✗** | 流式输出；本地全双工✗ | mlx-community 4-8bit 转换存在,实时引擎不成熟 | 输出 10 语无粤语 |
| Qwen2.5-Omni 3B/7B | 粤语 ASR WER 7.3/输出中英 | 分块输入+流式输出 | ✗(CUDA/vLLM/MNN) | |
| MiniCPM-o 4.5 (9B) | ✗(语音对话仅中/英) | 半双工✓;4.5 全双工(≈1Hz 决策) | ✓ llama.cpp-omni(M3/M4/M5 ≥16GB;全双工 M4 Max ≥24GB) | **首选试点** |
| GLM-4-Voice 9B | ✗(中/英) | 流式 | ✗ CUDA | |
| Step-Audio2-mini 8.3B | 输入✓(CER 含粤)/输出中英 | vLLM 流式 | ✗ CUDA | |
| Moshi 7B (kyutai) | 主英语 | 全双工(理论 ~200ms) | ✓ 官方 MLX(q4/q8) | 英语专用参考 |

## 关键判断
1. **粤语 S2S 输出=空白**：各家把粤语做「听得懂」，没有一家保证「用粤语出声」。
2. S2S 收益本质=砍掉级联三段排队与云端 TTS 首包；代价=更大模型、话术/知识/对象
   约束注入无成熟机制、打断需自研、Mac 无成熟实时引擎。
3. 因此路线：(a) 粤语线继续级联（当前 W0/W1 已把暖轮 TTFT 压到 ~0.6-1s 档）；
   (b) 普通话/英语试点 MiniCPM-o 4.5 半双工，实测「停嘴→出声」与打断质量再定；
   (c) Qwen3-Omni 4bit 作为未来自建 MLX 引擎的候补。

## 试点实验设计（MiniCPM-o 4.5）
1. llama.cpp-omni 起本地服务，录 20 句普通话/英文客服短句，测停嘴→首声 p50；
2. 话术可控性：能否稳定按 4 步话术推进（对照 FlowController）；
3. 打断：说话中插话让位与恢复；
4. 决策门：p50 ≤1.5s 且话术可控 → 再谈生产;否则维持级联。

## 出处
- github.com/QwenLM/Qwen3-Omni 、QwenLM/Qwen2.5-Omni
- github.com/OpenBMB/MiniCPM-o 、hf.co/openbmb/MiniCPM-o-4_5
- github.com/THUDM/GLM-4-Voice 、github.com/stepfun-ai/Step-Audio2
- github.com/kyutai-labs/moshi（moshi_mlx q4/q8）
- hf.co/mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit
