# Qwen3-TTS SFT 真人感微调操作手册（2026-09-26 全链实测验证）

> 验证基线：Mac(M3/48G) 上把「仓内真粤语/中文 wav → JSONL → audio_codes → SFT(MPS) →
> checkpoint → mlx-audio convert 8bit → sidecar 同款 load_model → custom_voice 合成 →
> 本机 ASR 回听可懂」**每一跳实弹打通**（V4 复核，2026-09-26）。唯一必须 CUDA 的只有
> 训练本身。产物与现役生产模型逐项同构，**sidecar 零代码改动**。
> 出处：QwenLM/Qwen3-TTS `finetuning/`（qwen-tts 0.1.1）；VoxCPM2 对照腿见文末。

## 0. 全链一图

```
真客服录音 → ①升采样24k → ②diarization剔客户侧 → ③降噪(可选) → ④VAD切3-30s
→ ⑤ASR粗转写+⑥人工校对 → ⑦固定ref.wav → train_raw.jsonl
→ prepare_data.py(编码, Mac可) → sft_12hz.py(CUDA必) → checkpoint(逐epoch存档)
→ mlx_audio.convert 8bit(Mac可) → 换模型目录 → 重跑 tts-pregen → 上线
```

## 1. 数据契约（官方硬约束）

- JSONL 三字段：`{"audio": <wav>, "text": <转写>, "ref_audio": <同一参考wav>}`
- **audio 与 ref_audio 必须 24kHz 单声道**（`dataset.py` `assert sr == 24000`）
- **全库同一条 ref_audio**（官方强烈建议，音色一致性）；ref 目标 8-10s
- 单说话人 SFT（官方当前版本；多说话人 future）
- 量级共识：**每语言/每音色 10-30 分钟干净、情绪丰富的录音**（最低 5 分钟；
  「10 分钟有情绪的干净录音胜过 1 小时噪声」——EasyFinetune 实践）

## 2. 数据准备（全部 Mac 可做，详见 docs/TTS-SFT-DATA-PREP.md）

工具链七步：ffmpeg 升采样 → pyannote 3.1 diarization（**先查录音系统是否分轨，
分轨=这步白送**；坐席锚定用「开场白说话人=坐席」）→ DeepFilterNet3 降噪（可选，
**宁可轻微噪声不要降噪伪影**）→ silero VAD 切 3-30s → 复用 :8787 Qwen3-ASR 粗转写
→ label-studio/audacity 人工校对（数字/单号必复核——错转写会把错误读法烙进音色）
→ 固定 ref.wav。

## 3. CUDA 窗口训练命令（可复制）

```bash
# 0) 环境（一次）
git clone https://github.com/QwenLM/Qwen3-TTS && cd Qwen3-TTS/finetuning
pip install qwen-tts accelerate tensorboard
pip install flash-attn --no-build-isolation   # 必须与 torch/CUDA 匹配
#   装不上 flash-attn：sed 's/flash_attention_2/sdpa/' sft_12hz.py > sft_sdpa.py
#   （Mac MPS 实测 sdpa 可训通；4090 实测先例 sidecar 路径 SDPA 前向足够）

# 1) 编码（Mac 或 CUDA）
python prepare_data.py --device cuda:0 \
  --tokenizer_model_path Qwen/Qwen3-TTS-Tokenizer-12Hz \
  --input_jsonl train_raw.jsonl --output_jsonl train_with_codes.jsonl

# 2) SFT——保守档
python sft_12hz.py \
  --init_model_path Qwen/Qwen3-TTS-12Hz-1.7B-Base \
  --output_model_path output \
  --train_jsonl train_with_codes.jsonl \
  --batch_size 32 --lr 2e-6 --num_epochs 10 \
  --speaker_name boc_agent
#   24GB 卡（4090）改 --batch_size 6（grad_accum 硬编码 4，等效 batch 24）
#   显存账（全参 AdamW bf16）：batch32≈40-60GB；batch6≈14-20GB；
#   数据少时 0.6B-Base 更稳（显存约砍 1/3）
```

**epoch 存档纪律（必守）**：每 checkpoint = copytree(Base ~3.3G) + 权重 3.6G ≈ **7GB/epoch**，
10 epoch ≈ 70GB——磁盘预留 ≥100G 或训完即移走非保留档；**每 epoch 对 checkpoint 快速
合成试听选档，不盲信末档**（ComfyUI 社区有「越训越差」退化报告）。

## 4. 合并→部署（全部 Mac 可做，已实测）

```bash
# 3) 转 8bit（SFT checkpoint 即标准 HF custom_voice 模型——「合并」就是存档本身）
python -m mlx_audio.convert --hf-path output/checkpoint-epoch-<best> \
  --mlx-path boc-qwen3-tts-sft-8bit --quantize --q-bits 8 --q-group-size 64

# 4) 部署：产物放 "Application Support/BokVoice/models/"，
#    bok.py tts_preset 指新目录（或 env QWEN3_TTS_PRESET_MODEL 覆盖）→ 重启 sidecar
# 5) 验证：GET :8788/health model_ready=true；:8788/v1/speakers 含 boc_agent；
#    试听合成。
# ⚠️ tts_cache 缓存键含 model——换模型后罐头音频全部失效，必须重跑
#    tts-pregen --greetings --fillers --qa，否则运行时优雅降级走逐句合成。
```

实测细节：`spk_id={speaker_name:3000}` 与 `tts_model_type=custom_voice` 完整穿透
8bit 转换；`get_supported_speakers()` 返回的 SFT 音色名即运行时 voice 名。

## 5. 已知风险与红线

- 训练 lr 过大/轮数过多伤 backbone——lr 2e-6 起步、逐 epoch 试听。
- 粤语：Qwen3-TTS 官方评测表**无粤语行**（音色表只有北京/四川方言位）——粤语 SFT 属
  契约外扩展，社区有先例（印地语 FT）；发音正确性不达标时的 B 计划=VoxCPM2。
- `spk_id` 官方两侧类型不一致（int vs List[int]），当前实测 int 路径正常，升级留意。
- MPS 训练数值未与 CUDA 对齐校准——Mac 只作链路验证，训练去 CUDA。

## 6. VoxCPM2 对照腿（Mac 实测 2026-09-26）

- Mac MPS **可用且超预期**：零微调粤语零样本可懂（本机 ASR 回听逐字一致）、48kHz、
  RTF≈0.95（快过实时）；中文 3.5s/句。内存 10.4GB。
- 短板：体量 2B/内存 10.4G（现役 1.7B-8bit 只要 2.9G）、无 MLX 量化路径（省内存要
  走未验证的 GGUF-Metal）。官方 LoRA 工具链齐（`voxcpm_finetune_lora.yaml`，
  5-10 分钟音频适配说话人，r=32≈全参 98% 相似度）、官方方言列表明列粤语。
- 定位：**粤语零样本兜底/克隆质量对照组**，暂不动生产栈；环境留在 /tmp/VoxCPM 可复测。
