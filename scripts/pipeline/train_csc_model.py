#!/usr/bin/env python3
"""CSC 自训训练入口（char-level MacBERT4CSC 微调，2026-09-27）。

配套：``scripts/seed/prepare_csc_data.py``（数据管道）与 ``scripts/pipeline/eval_csc_model.py``
（位置感知判分）。本脚本只做**训练**，产出检查点给 ``scripts/pipeline/predict_csc_model.py``
与 CSC sidecar（``CSC_MODEL_DIR``）加载。

## 模型与加载姿势（镜像 ``services/csc-sidecar/app.py``）

- 基座 ``shibing624/macbert4csc-base-chinese``（102M，Apache-2.0）。
- 加载/保存一律走 ``BertTokenizerFast`` + ``BertForMaskedLM``，与 sidecar 逐字
  同款；``save_pretrained`` 产出的目录因此可被 sidecar 的
  ``BertForMaskedLM.from_pretrained(CSC_MODEL_DIR)`` **直接加载**（也兼容
  ``AutoModelForMaskedLM``）。CSC sidecar 判断模型族做的是逐字符等长改写，
  故训练侧也必须保持等长契约（见下）。

## 输入（与 prepare_csc_data.py 落盘 schema 一致）

``--train`` 读 JSONL，每行 ``{"src": 错句, "tgt": 正句, "lane": ..., "origin": ...}``
（``prepare_csc_data.py`` 的实际字段名是 ``src``/``tgt``；本脚本额外容忍
``text``/``target`` 别名以适配手写数据）。

## 对齐与损失（等长契约 + teacher forcing）

- **等长字符对齐**：只保留 ``len(src) == len(tgt)`` 的行；长度不等的行（词级变体
  替换偶发增删、回灌脏数据）**跳过并计数**——等长是 CSC 输出契约（sidecar 的
  等长断言 / eval 的 structural_edits 都以它为准）。
- **teacher forcing**：输入 = ``src`` 原字（含错误字，与推理时喂 raw 转写同分布），
  标签 = ``tgt`` 原字；逐位置交叉熵。**本脚本采用「全序列 MLM-on-target」**：
  在所有非特殊/非 padding 位置算 CE（身份对位置 label==input，模型学到「照抄 =
  正确」；错误位置学到「改对」）。这是 smoke 档的刻意简化——更贴近 MacBERT4CSC
  原法的「只在改变位置算损失 + 检测头」是后续生产目标，不在本轮范围。
- 分词用 ``is_split_into_words=True`` 把 ``src`` 拆成单字词，保证「一字符 =
  一 token」；标签经 ``word_ids()`` 映射回字符下标，多字符/续接 token 自然对齐。

## 用法

    # smoke（需 torch+transformers；模型未缓存会尝试联网拉取，离线请先备缓存）
    python scripts/pipeline/train_csc_model.py --train /tmp/csc-smoke/csc_train.jsonl \
        --out /tmp/csc-smoke/ckpt --epochs 1 --batch 8 --limit 300 --device cpu

    # 正式（≥5 万对，Apple Silicon 建议 --device mps）
    python scripts/pipeline/train_csc_model.py --train data/csc/csc_train.jsonl \
        --out data/csc/macbert4csc-ft --epochs 3 --batch 16 --device mps
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
import random
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = "shibing624/macbert4csc-base-chinese"
DEFAULT_TRAIN = ROOT / "data" / "csc" / "csc_train.jsonl"
DEFAULT_OUT = ROOT / "data" / "csc" / "macbert4csc-ft"


def load_pairs(path: str | Path, limit: int | None = None) -> tuple[list[tuple[str, str]], int]:
    """读训练 JSONL，返回 (等长对, 因长度不等被跳过的行数)。

    字段名以 ``prepare_csc_data.py`` 的 ``src``/``tgt`` 为准，兼容 ``text``/``target``
    别名。空行/缺字段/空串跳过（不计入长度不等）。
    """
    rows: list[tuple[str, str]] = []
    skipped_len = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        src = rec.get("src")
        if src is None:
            src = rec.get("text")
        tgt = rec.get("tgt")
        if tgt is None:
            tgt = rec.get("target")
        if not isinstance(src, str) or not isinstance(tgt, str):
            continue
        src, tgt = src.strip(), tgt.strip()
        if not src or not tgt:
            continue
        if len(src) != len(tgt):
            skipped_len += 1
            continue
        rows.append((src, tgt))
        if limit is not None and len(rows) >= max(1, int(limit)):
            break
    return rows, skipped_len


def resolve_device(name: str) -> str:
    """解析设备：auto → mps（可用时）否则 cpu；显式 mps 不可用则回退 cpu 并告警。"""
    import torch

    name = (name or "cpu").strip().lower()
    if name == "auto":
        return "mps" if torch.backends.mps.is_available() else "cpu"
    if name == "mps" and not torch.backends.mps.is_available():
        print("[train_csc_model] mps 不可用，回退 cpu")
        return "cpu"
    return name


class _CharDataset:
    """把 (src, tgt) 逐条转成等长字符 MLM 训练的 (input_ids, attention_mask, labels)。"""

    def __init__(self, pairs: list[tuple[str, str]], tok: Any, max_len: int) -> None:
        self._pairs = pairs
        self._tok = tok
        self._max_len = max(8, int(max_len))

    def __len__(self) -> int:
        return len(self._pairs)

    def __getitem__(self, idx: int) -> dict[str, Any]:
        import torch

        src, tgt = self._pairs[idx]
        enc = self._tok(
            list(src),
            is_split_into_words=True,
            add_special_tokens=True,
            truncation=True,
            max_length=self._max_len,
            padding="max_length",
            return_tensors="pt",
        )
        input_ids = enc["input_ids"][0]
        attention_mask = enc["attention_mask"][0]
        word_ids = enc.word_ids(0)  # 单字词 → 一词一号；特殊/填充位为 None
        char_ids = self._tok.convert_tokens_to_ids(list(tgt))
        labels = torch.full_like(input_ids, -100)
        for pos, wid in enumerate(word_ids):
            if wid is None:
                continue
            if wid < len(char_ids):
                labels[pos] = char_ids[wid]
        return {"input_ids": input_ids, "attention_mask": attention_mask, "labels": labels}


def _collate(batch: list[dict[str, Any]]) -> dict[str, Any]:
    import torch

    return {k: torch.stack([b[k] for b in batch]) for k in batch[0]}


def train(args: argparse.Namespace) -> int:
    import torch
    from torch.utils.data import DataLoader

    from transformers import BertForMaskedLM, BertTokenizerFast

    random.seed(args.seed)
    torch.manual_seed(args.seed)

    device = resolve_device(args.device)
    pairs, skipped_len = load_pairs(args.train, limit=args.limit)
    print(f"[train_csc_model] train={args.train}")
    print(f"  pairs={len(pairs)} skipped_len_mismatch={skipped_len} device={device} base={args.base}")
    if not pairs:
        print("[train_csc_model] 无可训练行，退出")
        return 2

    tok = BertTokenizerFast.from_pretrained(args.base)
    model = BertForMaskedLM.from_pretrained(args.base)
    model.to(device)

    ds = _CharDataset(pairs, tok, args.max_len)
    loader = DataLoader(
        ds,
        batch_size=max(1, int(args.batch)),
        shuffle=True,
        collate_fn=_collate,
        generator=torch.Generator().manual_seed(args.seed),
    )
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)

    model.train()
    step = 0
    last_loss = float("nan")
    for epoch in range(max(1, int(args.epochs))):
        for batch in loader:
            batch = {k: v.to(device) for k, v in batch.items()}
            out = model(**batch)
            loss = out.loss
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            step += 1
            last_loss = float(loss.detach().cpu())
            if step % args.log_every == 0:
                print(f"[train_csc_model] epoch={epoch + 1} step={step} loss={last_loss:.4f}")

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    # save_pretrained 双件 → sidecar CSC_MODEL_DIR 直接可加载（BertForMaskedLM 同款）。
    model.save_pretrained(str(out_dir))
    tok.save_pretrained(str(out_dir))
    meta = {
        "base": args.base,
        "epochs": int(args.epochs),
        "batch": int(args.batch),
        "max_len": int(args.max_len),
        "lr": float(args.lr),
        "seed": int(args.seed),
        "pairs": len(pairs),
        "skipped_len_mismatch": skipped_len,
        "device": device,
        "last_loss": None if last_loss != last_loss else round(last_loss, 6),
    }
    (out_dir / "train_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[train_csc_model] saved checkpoint → {out_dir}  last_loss={meta['last_loss']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="char-level MacBERT4CSC 自训入口")
    ap.add_argument("--train", default=str(DEFAULT_TRAIN), help="训练 JSONL(src/tgt)")
    ap.add_argument("--base", default=DEFAULT_BASE, help="基座模型/本地检查点（默认 MacBERT4CSC）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="检查点输出目录")
    ap.add_argument("--epochs", type=int, default=1, help="训练轮数（默认 1）")
    ap.add_argument("--batch", type=int, default=8, help="批大小（默认 8）")
    ap.add_argument("--max-len", type=int, default=64, help="最大字符长度（默认 64）")
    ap.add_argument("--device", default="cpu", help="cpu / mps / auto（默认 cpu）")
    ap.add_argument("--lr", type=float, default=5e-5, help="学习率（默认 5e-5）")
    ap.add_argument("--seed", type=int, default=20260927, help="随机种子")
    ap.add_argument("--limit", type=int, default=None, help="仅取前 N 行（smoke 用）")
    ap.add_argument("--log-every", type=int, default=20, help="每 N 步打印一次 loss")
    args = ap.parse_args(argv)
    return train(args)


if __name__ == "__main__":
    raise SystemExit(main())
