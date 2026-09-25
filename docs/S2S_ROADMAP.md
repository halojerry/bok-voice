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

## 增补（2026-09-26，第二轮调研：HF speech-to-speech / MiniMax ASR / realtime 演示档）

1. **huggingface/speech-to-speech（13.3k★，活跃）**：正经框架非 demo——Silero VAD +
   Parakeet TDT（25 欧洲语）/Whisper/Paraformer(中)/Qwen3-ASR-0.6B 做 STT + 可换 LLM
   （OpenAI 兼容/mlx-lm/vLLM）+ Qwen3-TTS-12Hz-1.7B/Kokoro 做 TTS；WebSocket/WebRTC
   服务面**实现 OpenAI Realtime GA 事件集**（对外可调用的 realtime 服务形态）。
   **判定：不引入**——①省不了钱（我们现金支出=MiniMax TTS，VAD/ASR/LLM 本地=固定
   硬件成本，打包进程数≠降云费）；②四件套同进程=共置最大化，恰是我们刚治好的 G1；
   ③粤语 STT 依赖 Whisper/Paraformer、TTS 无粤语位，与我们的三语面错配；④其漏斗是
   通用语音聊天，无我们文本面七层漏斗（话术/快答/捕号/意向）。**可偷**：Realtime 事件
   集作互操作标准；Qwen3-ASR-0.6B（小杯兄弟，低成本 listening 档候选，暂不动）。
2. **MiniMax ASR（/v1/speech_to_text, asr-1.0）**：文件式离线转写（multipart，≤500s/
   ≤50MB；json/verbose_json/srt/vtt；说话人分离+词级时间戳；SSE 只流响应不流音频；
   输入语言含 zh/yue/en——`yue` 属外部系统枚举字面量，同 Volcano dialect 白名单类）。
   **不能上实时车道**（无流式音频输入）；**定位=离线审计线**：录音 verbose_json vs 在线
   Qwen3-ASR turns 逐通 diff——把碎裂/数字滑失从轶事变数据，喂 ASR 补课清单；兼作
   QA 挖掘第二信源（捞回碎裂句）。凭据=既有 MiniMax key（同账户）。
3. **云端流式 ASR 新候选**：Qwen-Audio-3.x-**ASR-Flash-Streaming**（DashScope
   WebSocket 实时转写）——比文件式 API 更对路，升为 CUDA 窗口云端 ASR 对照腿首选
   （原 Qwen3-ASR-Flash 试点位）。
4. **realtime 演示档（独立 demo agent，不进 A 线）**：Qwen-Audio-3.0-Realtime /
   Qwen3-Omni-Flash realtime（DashScope WS，Plus/Flash，全双工，粤语**输出**仍✗→
   zh/en demo 腿）；ChatGPT realtime（gpt-realtime）走 LiveKit 官方 openai 插件
   （含 GPT-Live 插件与 **realtime 模型+自家 TTS 混合模式**——实时听 + MiniMax 出
   粤语声，是粤语 demo 的潜在中间态）。计费=会话时长/调用计费，演示对象必须用假数据。
5. **open_asr_leaderboard**（HF，Apache-2.0，活跃）：WER(+RTFx) 榜——英语短/长形 +
   多语 FLEURS/MCV/MLS（FLEURS 含 yue/cmn）；H200 docker 逐家族评测、PR 提交、私有
   集防过拟。**用法=下一代本地 ASR 候选短名单源**，不直接当结论：批式离线榜 ≠ 我们
   流式 sidecar（句级提交/热词偏置/数字纪律/MLX 皆不在榜内），终裁走我们自己的
   碎裂语料 A/B 探针纪律。
