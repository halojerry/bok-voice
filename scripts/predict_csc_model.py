#!/usr/bin/env python3
"""CSC 离线预测 harness（配合 ``scripts/eval_csc_model.py``，2026-09-27）。

加载一个 CSC 检查点（自训产物 ``scripts/train_csc_model.py`` 或基座
``shibing624/macbert4csc-base-chinese``），对评测集逐条做**等长逐字贪心改写**，
输出 ``eval_csc_model.py`` 直接消费的预测 JSONL（``{"id": ..., "src": ..., "pred": ...}``）。

## 与 CSC sidecar 的关系

推理核心（offset_mapping 对齐、多字符/续接 token 跳过、阈值门）镜像
``services/csc-sidecar/app.py`` 的 ``_model_rewrite``。区别只在**语言门**：
sidecar 的 ``/correct`` 对 ``lang != "zh"`` 一律拒（粤语宁可漏纠也不许漂移）；
本 harness 是**离线评测**，**接受全部语言（含粤语）**，不做任何语言拒绝——
评测要如实反映模型在粤语面上的行为。

## 无结构违约 + 粤语不漂移的保证（贴合 eval 成功判据）

等长逐字改写天然保证 ``structural_edits == 0``（永不增删）。另有与 sidecar
同源的保守守卫（**编辑守卫，不是语言门**）：

- 冻结位回填：数字串（阿拉伯 + 中文数字词）、拉丁 run、非汉字标点/空白、
  以及**粤语特征字**（``bok_voice_core.asr_polish._CANTONESE_MARKERS``，单源
  import 不复制）在推理前替换成占位符、输出后强制回填原文 → 这些字符
  **物理上不可能被改**，故 ``yue_marker_loss == 0``。
- 等长断言：回填后长度不符 → 整条返回原文。
- 编辑预算：实际改动处数 > ``--max-edits`` → 整条返回原文（防一口改多，
  也是 ``keep`` 负样本不被破坏的兜底）。
- 阈值门：逐字 softmax 概率低于 ``--threshold``（默认 0.9，与 sidecar 标定同档）
  的预测丢弃、保留原字符。

## 用法

    python scripts/predict_csc_model.py --ckpt data/csc/macbert4csc-ft \\
        --testset data/csc/csc_testset.json --out /tmp/csc-smoke/preds.jsonl --device cpu
    python scripts/eval_csc_model.py --predictions /tmp/csc-smoke/preds.jsonl \\
        --json-out /tmp/csc-smoke/report.json
"""

from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_CORE = ROOT / "packages" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

# 粤语特征字单源（与 eval_csc_model.py 同一份集合，不复制）。
from bok_voice_core.asr_polish import _CANTONESE_MARKERS  # noqa: E402

DEFAULT_BASE = "shibing624/macbert4csc-base-chinese"
DEFAULT_TESTSET = ROOT / "data" / "csc" / "csc_testset.json"

# 占位符：私有区字符在 BERT 词表里映射成 [UNK]，逐字符替换保证等长、回填即还原。
PLACEHOLDER = "\ue000"
# 中文数字词（含大写），与 sidecar app.py 同集合。
_CN_NUMERALS = frozenset("零一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟")


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def frozen_positions(text: str) -> set[int]:
    """守卫：返回**不可改**字符下标集合（数字/中文数字/拉丁/标点空白/粤语特征字）。

    镜像 sidecar ``freeze_spans``，并额外冻 ``_CANTONESE_MARKERS``（harness 接受
    粤语，但要保证特征字零丢失）。
    """
    frozen: set[int] = set()
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch.isdigit() or ch in _CN_NUMERALS:
            while i < n and (text[i].isdigit() or text[i] in _CN_NUMERALS):
                frozen.add(i)
                i += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            while i < n and (("a" <= text[i] <= "z") or ("A" <= text[i] <= "Z")):
                frozen.add(i)
                i += 1
        elif ch in _CANTONESE_MARKERS:
            frozen.add(i)
            i += 1
        elif not _is_cjk(ch):
            frozen.add(i)
            i += 1
        else:
            i += 1
    return frozen


def mask_text(text: str, frozen: set[int]) -> str:
    return "".join(PLACEHOLDER if k in frozen else text[k] for k in range(len(text)))


def load_testset(path: str | Path) -> list[dict]:
    """载入评测集：``{"items": [...]}`` 或裸 list（与 eval_csc_model.py 同解析）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    return list(items or [])


def _rewrite_batch(
    masked_texts: list[str], tok: Any, model: Any, device: str, threshold: float
) -> list[str]:
    """对一批掩码文本做逐字等长贪心改写（镜像 sidecar ``_model_rewrite``）。"""
    import torch

    enc = tok(
        masked_texts,
        return_tensors="pt",
        padding=True,
        return_offsets_mapping=True,
    )
    offsets = enc["offset_mapping"].tolist()
    with torch.no_grad():
        logits = model(
            input_ids=enc["input_ids"].to(device),
            attention_mask=enc["attention_mask"].to(device),
        ).logits
    probs = torch.softmax(logits, dim=-1)
    max_p, arg = torch.max(probs, dim=-1)
    max_p = max_p.cpu().tolist()
    ids = arg.cpu().tolist()

    out_texts: list[str] = []
    for bi, masked in enumerate(masked_texts):
        out = list(masked)
        for t, (s, e) in enumerate(offsets[bi]):
            if e <= s or s >= len(masked):
                continue  # [CLS]/[SEP]/padding 等特殊 token
            piece = tok.convert_ids_to_tokens(ids[bi][t])
            if piece.startswith("##") or len(piece) != 1:
                continue  # 多字符/续接 token：跳过（保留原字符）
            if e - s != 1:
                continue
            if max_p[bi][t] >= threshold:
                out[s] = piece
        out_texts.append("".join(out))
    return out_texts


def _restore_and_guard(
    original: str, masked_out: str, frozen: set[int], max_edits: int
) -> str:
    """冻结位回填 + 等长断言 + 编辑预算；任一守卫不过 → 返回原文。"""
    if len(masked_out) != len(original):
        return original
    restored = "".join(
        original[i] if i in frozen else masked_out[i] for i in range(len(original))
    )
    n_edits = sum(1 for i in range(len(original)) if restored[i] != original[i])
    if n_edits > max_edits:
        return original
    return restored


def predict(args: argparse.Namespace) -> int:
    import torch
    from transformers import BertForMaskedLM, BertTokenizerFast

    device = (args.device or "cpu").strip().lower()
    if device == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    if device == "mps" and not torch.backends.mps.is_available():
        print("[predict_csc_model] mps 不可用，回退 cpu")
        device = "cpu"

    items = load_testset(args.testset)
    print(f"[predict_csc_model] ckpt={args.ckpt} testset={args.testset} items={len(items)} device={device}")

    tok = BertTokenizerFast.from_pretrained(args.ckpt)
    model = BertForMaskedLM.from_pretrained(args.ckpt)
    model.to(device).eval()

    records: list[dict] = []
    batch = max(1, int(args.batch))
    for start in range(0, max(1, len(items)), batch):
        chunk = items[start : start + batch]
        originals = [str(it.get("input", "")) for it in chunk]
        frozen = [frozen_positions(t) for t in originals]
        masked = [mask_text(t, f) for t, f in zip(originals, frozen)]
        masked_out = _rewrite_batch(masked, tok, model, device, args.threshold)
        for it, orig, fz, mo in zip(chunk, originals, frozen, masked_out):
            pred = _restore_and_guard(orig, mo, fz, args.max_edits)
            iid = it.get("id")
            rec = {"src": orig, "pred": pred}
            if iid is not None:
                rec["id"] = iid
            rec["lang"] = it.get("lang", "")
            records.append(rec)

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[predict_csc_model] wrote {len(records)} predictions → {out_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CSC 离线预测 harness（等长逐字贪心）")
    ap.add_argument("--ckpt", default=DEFAULT_BASE, help="检查点目录或 HF 基座（默认 MacBERT4CSC）")
    ap.add_argument("--testset", default=str(DEFAULT_TESTSET), help="评测集 JSON")
    ap.add_argument("--out", required=True, help="预测 JSONL 输出路径")
    ap.add_argument("--device", default="cpu", help="cpu / mps / auto（默认 cpu）")
    ap.add_argument("--batch", type=int, default=16, help="批大小（默认 16）")
    ap.add_argument("--threshold", type=float, default=0.9, help="逐字概率门（默认 0.9，sidecar 标定档）")
    ap.add_argument("--max-edits", type=int, default=2, help="单条编辑预算（默认 2；超出整条放弃）")
    args = ap.parse_args(argv)
    return predict(args)


if __name__ == "__main__":
    raise SystemExit(main())
