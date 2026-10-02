# 真客服录音 → Qwen3-TTS SFT 数据集工具链（2026-09-26，Mac 可行性标注）

> 配套 docs/TTS-SFT-PLAYBOOK.md（全链手册）。官方契约：`{"audio","text","ref_audio"}`，
> 24kHz 硬约束、全库同一 ref、单说话人。

## 流水线（每步标注 Mac 可做性）

```
原始录音 (8kHz 电话落盘 / 双声道混音)
  ├─ ① 升采样重排  8kHz → 24kHz mono 16bit wav        [Mac ✅ CPU]
  ├─ ② 说话人分离  diarization 剔除客户侧             [Mac ✅ CPU 慢 / 精度注意]
  ├─ ③ 降噪（可选） 电话线路噪声                      [Mac ✅ CPU]
  ├─ ④ VAD 切句    切 3-30s 片段                      [Mac ✅ CPU]
  ├─ ⑤ ASR 粗转写  whisper/qwen3-asr 出初稿           [Mac ✅ 本地现役 ASR 可复用]
  ├─ ⑥ 人工校对    粤语别字/数字/专名逐条校对          [人工，工具辅助]
  └─ ⑦ 固定 ref    选最长最干净一条作全库 ref_audio    [Mac ✅]
```

## 各步工具与命令

### ① 升采样（ffmpeg；勿走 audioread——本机有 segfault 前科）
```bash
ffmpeg -i call_001.wav -ac 1 -ar 24000 -c:a pcm_s16le out/utt_full_001.wav
```

### ② diarization（剔客户侧保坐席）
- **先查录音系统是否分轨——分轨=这步白送**（强烈建议）。
- pyannote.audio 3.1（Mac CPU/MPS 可跑，慢）：`Pipeline.from_pretrained("pyannote/speaker-diarization-3.1")`；
  **「坐席=开场白说话人」锚定**（我们通话固定坐席先开口），不要只按说话时长猜。
- 备选 whisperX（对齐+分离一体）/ NeMo（CUDA 档）。8kHz 源分离精度天然偏低。

### ③ 降噪（可选；宁可轻微噪声不要处理伪影——伪影会烙进音色）
- DeepFilterNet3（Mac CPU）：`deepFilter input.wav -o denoised/`；或 noisereduce 谱减法。

### ④ VAD 切句（3-30s，目标中位 ~8s；<1s 碎片丢；句间静音留 ≤0.3s 自然呼吸）
- silero-vad：`get_speech_timestamps(wav16k, model, sampling_rate=16000)`；
  或 ffmpeg silencedetect 粗切后合并。

### ⑤ ASR 粗转写
- 复用现役 :8787 Qwen3-ASR（三语已校准）；交叉校验 whisper-large-v3 再过一遍，
  两稿差异大的片段标人工优先复核。

### ⑥ 人工校对（label-studio 或 audacity 逐条听改）
- 粤语书面化统一（嘅/咗/唔 按模型输出习惯）；标点真实反映句界（影响 TTS 停顿）；
  长犹豫（呃/嗯）删；**数字/单号必复核——SFT 学到错转写=把错误读法烙进音色**。

### ⑦ 固定 ref_audio（8-10s 最干净最具代表性一条，全库共用）

## Mac 不可做/受限
- 训练本身（flash_attention_2 硬编码；sdpa 补丁 Mac 可跑但数值未校准，只作链路验证）。
- 大批量 diarization/降噪 CPU 慢（~0.3-1× 实时），几百小时量级建议 CUDA 跑预处理，
  Mac 做小批量与质检。
- VoxCPM2 LoRA 微调声称支持 MPS（training/accelerator.py 有分支），未实测训练收敛。
