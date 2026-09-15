# smart-turn 粤语可行性 spike 计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实测 pipecat smart-turn v3（8M 参数音频原生 EOT 模型）在本机 Mac CPU 上的推理开销与三语 finished/unfinished 判定准确率，并评估「用粤语通话资产微调出粤语 EOT 头」的数据可行性。

**Architecture:** 探针脚本构造受控测试集（完整句=finished、同音频按时长截断=unfinished），喂 ONNX 模型统计准确率与延迟；再读 train.py/数据 schema 给出微调数据清单与成本估计。不碰运行栈。

**Tech Stack:** onnxruntime（CPU）、huggingface_hub（已装，下模型 `pipecat-ai/smart-turn-v3`）、tests/fixtures 三语音频。

## Global Constraints

- 本 spike 为只读评估；不改 apps/、services/。
- v3.2 官方口径：23 语言、**无粤语**（只有 "Chinese"）——spike 的一切结论都围绕「微调补粤语」展开。
- 决策门目标：若本机推理 p95 ≤100ms 且三语准确率 ≥80%，微调立项有戏；推理超预算则无论数据如何都不立项。

---

### Task 1: 安装与模型就位

- [ ] **Step 1:** `pip install smart-turn`（若 PyPI 无此包则 `pip install onnxruntime` + `huggingface_hub.snapshot_download("pipecat-ai/smart-turn-v3")` 直接取 ONNX 权重，推理代码照仓库 `smart_turn/` 目录 ~百行自行搬运到探针脚本内）。
- [ ] **Step 2:** 记录实际安装方式、模型文件清单与体积进 `.superpowers/sdd/2026-09-10-smart-turn-spike/FINDINGS.md`。

### Task 2: 探针脚本与三语实测

**Files:**
- Create: `scripts/probe_smart_turn.py`

- [ ] **Step 1: 写脚本**（核心结构）

```python
#!/usr/bin/env python3
"""smart-turn 三语 finished/unfinished 探针：准确率 + CPU 推理延迟。"""
from __future__ import annotations
import wave, time
import numpy as np

SR = 16000

def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return (pcm.astype(np.float32) / 32768.0)[: SR * 8]  # 官方上限 8s

def build_cases() -> list[tuple[str, np.ndarray, int]]:
    """(标签, 音频, 截断比例) — finished=完整句, unfinished=60%/75% 截断。"""
    cases = []
    for lang in ("zh", "cantonese", "en"):
        full = load_wav(f"tests/fixtures/audio/{lang}.wav")
        cases.append((lang, full, 1))          # finished
        cases.append((lang, full[: int(len(full) * 0.6)], 0))  # unfinished
        cases.append((lang, full[: int(len(full) * 0.75)], 0))
    return cases

def main(predict) -> None:  # predict(pcm)->(prob_finished, ms)
    hit = total = 0
    lats = []
    for lang, pcm, label in build_cases():
        prob, ms = predict(pcm)
        lats.append(ms)
        pred = int(prob >= 0.5)
        hit += pred == label
        total += 1
        print(f"{lang:9s} label={label} pred={pred} p={prob:.3f} {ms:.0f}ms")
    lats = sorted(lats)
    print(f"\naccuracy={hit}/{total}  p50={lats[len(lats)//2]:.0f}ms  p95={lats[int(len(lats)*0.95)]:.0f}ms")
```

`predict` 由 Task 1 选定的推理路径实现（onnx session 输入 16k float32 → sigmoid 概率）。

- [ ] **Step 2:** 跑三语 fixtures 全集（单句 3 条 + `data/smoke-out/preset-*.wav` 3 条 + e2e_multi 首段），记录准确率矩阵与 p50/p95 进 FINDINGS.md。**重点看粤语列**：官方未训过粤语，若粤语准确率显著低于中英（<70%），恰恰证明「需要微调」成立，spike 目的是量化这个 gap。

### Task 3: 粤语微调数据可行性盘点

- [ ] **Step 1:** 读 `smart-turn` 仓库 `train.py` + HF 数据集 `smart-turn-data-v3.2-train` 的 schema（字段/时长分布/每语言样本量），原样记进 FINDINGS.md。
- [ ] **Step 2:** 盘点本仓可用的粤语音频资产：`grep -rn "record\|\.wav\|ogg" apps/control-plane apps/agent --include="*.py"` 确认**生产是否落盘通话音频**（当前判断：turns 只存转写文本，音频不持久化——若证实，微调数据来源=E2E 真化合成音频+外部粤语语料如 Common Voice yue，须写明）。
- [ ] **Step 3:** 给出数据清单：目标样本量（对照官方每语言量级）、标注规则（finished=句末完整轮；unfinished=截断/句中停顿轮）、制作管线（从 E2E 渲染管线复用 tts_pcm 截断批量生成）。
- [ ] **Step 4:** 结论：三档之一——「A：准确率达标+数据管线清晰 → 立项微调 PR 计划」「B：推理达标但需先补数据 → 列 S5 后排期」「C：推理超预算 → 归档不立项」。
