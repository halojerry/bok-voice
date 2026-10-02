#!/usr/bin/env python3
"""CSC sidecar 自检 —— 不依赖 pytest。

跑三类断言：
  1) 守卫链纯函数（冻结 span / 等长 / 编辑预算 / 粤语特征字）；
  2) 服务级 skip 闸（lang!=zh、空串、粤语、超长 400、参数校验）——不需要模型；
  3) 真模型 8 条固定句（4 该纠 / 4 不该动）+ 冻结 combo + 预算闸，打印逐条延迟。

用法：
  services/csc-sidecar/.venv/bin/python services/csc-sidecar/selftest.py
  # 只验守卫链（不加载/下载模型）：
  CSC_DISABLE_LOAD=1 services/csc-sidecar/.venv/bin/python services/csc-sidecar/selftest.py
  # 复用 harness 缓存加速（/tmp/csc/models）：
  HF_HOME=/tmp/csc/models services/csc-sidecar/.venv/bin/python services/csc-sidecar/selftest.py

退出码：0=全绿（或显式 CSC_DISABLE_LOAD=1 的守卫链档），1=有 FAIL。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 复用上一轮 harness 的 HF 缓存（若存在且用户没显式指定缓存）。
if not os.environ.get("HF_HOME") and not os.environ.get("CSC_HF_HOME"):
    if Path("/tmp/csc/models").is_dir():
        os.environ["HF_HOME"] = "/tmp/csc/models"

from fastapi import HTTPException  # noqa: E402

import app as csc  # noqa: E402
from app import (  # noqa: E402
    CscService,
    find_yue_markers,
    freeze_spans,
    restore_and_guard,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ------------------------------------------------------------------ 1) 守卫纯函数
def test_guards() -> None:
    section("守卫链纯函数（无模型）")

    # 守卫 a：冻结 span（数字 + 标点），掩码等长、占位符落位。
    text = "订单377890，谢谢"
    masked, frozen = freeze_spans(text)
    check("a: 掩码等长", len(masked) == len(text), f"{len(masked)}!={len(text)}")
    check("a: 数字全冻结", {2, 3, 4, 5, 6, 7} <= frozen, str(sorted(frozen)))
    check("a: 标点冻结", 8 in frozen, str(sorted(frozen)))
    check("a: 汉字不冻结", frozen.isdisjoint({0, 1, 9, 10}), str(sorted(frozen)))
    check("a: 冻结位置=占位符", masked[2] == csc.PLACEHOLDER and masked[8] == csc.PLACEHOLDER)

    # 守卫 a 回填：冻结位置取原文（模型在那里输出什么都不认）。
    out, edits, reason = restore_and_guard("A1", "B2", {0, 1}, 5)
    check("a: 冻结回填=原文", out == "A1" and edits == [] and reason is None)

    # 守卫 b：等长断言。
    out, edits, reason = restore_and_guard("abc", "ab", set(), 5)
    check("b: 长度不等→放弃", out == "abc" and edits == [] and reason == "length_mismatch")

    # 守卫 c：编辑预算。
    out, edits, reason = restore_and_guard("abc", "xyz", set(), 2)
    check("c: 3 改 > 预算 2→放弃", out == "abc" and edits == [] and reason == "over_budget")
    out, edits, reason = restore_and_guard("abc", "ayc", set(), 2)
    check("c: 1 改 ≤ 预算→放行", out == "ayc" and reason is None and edits == [{"pos": 1, "from": "b", "to": "y"}])

    # 守卫 d：粤语特征字。
    check("d: 检出 哋/嘅", find_yue_markers("你哋公司嘅") == ["哋", "嘅"])
    check("d: 普通话常用字不误报", find_yue_markers("地址既然") == [])


# ------------------------------------------------------------- 2) 服务级 skip 闸
def test_skip_gates(svc: CscService) -> None:
    section("服务级 skip 闸（不需要模型）")

    r = svc.correct({"text": "你哋公司", "lang": "cantonese"})
    check("lang=cantonese→lang_not_zh", r["skipped_reason"] == "lang_not_zh" and r["text"] == "你哋公司")

    r = svc.correct({"text": "hello", "lang": "en"})
    check("lang=en→lang_not_zh", r["skipped_reason"] == "lang_not_zh")

    r = svc.correct({"text": "hello", "lang": ""})
    check("lang 缺省→lang_not_zh（保守）", r["skipped_reason"] == "lang_not_zh")

    r = svc.correct({"text": "", "lang": "zh"})
    check("空串→empty", r["skipped_reason"] == "empty")

    # 守卫 d 服务端兜底：调用方误判成 zh，粤语也不许被漂移。
    yue = "你哋公司喺香港，我係用顺丰嘅"
    r = svc.correct({"text": yue, "lang": "zh"})
    check("守卫 d: z h+粤语→cantonese_markers", r["skipped_reason"].startswith("cantonese_markers"))
    check("守卫 d: 粤语原文零改动（无漂移）", r["text"] == yue and "係" in r["text"])

    # 超长 → 400（拒）。
    try:
        svc.correct({"text": "好" * (csc.MAX_CHARS + 1), "lang": "zh"})
        check("超长→400", False, "未抛 HTTPException")
    except HTTPException as e:
        check("超长→400", e.status_code == 400)

    # 参数校验。
    for bad, label in [
        ({"text": 1, "lang": "zh"}, "text 非串"),
        ({"text": "好的", "lang": "zh", "max_edits": True}, "max_edits=bool"),
        ({"text": "好的", "lang": "zh", "max_edits": -1}, "max_edits<0"),
        ({"text": "好的", "lang": "zh", "threshold": "x"}, "threshold 非数"),
    ]:
        try:
            svc.correct(bad)
            check(f"参数校验: {label}→400", False, "未抛")
        except HTTPException as e:
            check(f"参数校验: {label}→400", e.status_code == 400)


# --------------------------------------------------------------- 3) 真模型推理
FIX_CASES = [
    ("这个方按需要我承担运费吗", "这个方案需要我承担运费吗"),
    ("请帮我察一下单号", "请帮我查一下单号"),
    ("赔尝方案是什么", "赔偿方案是什么"),
    ("京冬物流", "京东物流"),
]
KEEP_CASES = [
    ("我的订单号是三七七八九零", "数字串+正确句"),
    ("拼多多和京东都可以发货", "专名+正确句"),
    ("这个方案我同意，运费我来承担", "正确句"),
]


def test_model(svc: CscService) -> None:
    section("真模型推理（8 条固定句 + 守卫 combo）")
    if csc.LOAD_DISABLED:
        print("  skip （CSC_DISABLE_LOAD=1：仅守卫链档）")
        return
    svc.ensure_loaded()
    if svc._load_error:
        check("模型加载", False, svc._load_error)
        return
    check("模型加载", True, f"device={svc._device} source={svc._model_source}")

    lat: list[float] = []

    def run(text, **kw):
        r = svc.correct({"text": text, "lang": "zh", **kw})
        lat.append(r["latency_ms"])
        return r

    print("  -- 该纠（4） --")
    for src, want in FIX_CASES:
        r = run(src)
        ok = r["text"] == want and len(r["edits"]) == 1
        check(f"纠: {src} → {want}", ok, f"got {r['text']!r} edits={r['edits']}")
        print(f"       {r['latency_ms']}ms")

    print("  -- 不该动（4） --")
    for src, note in KEEP_CASES:
        r = run(src)
        check(f"保持: {src} ({note})", r["text"] == src and r["edits"] == [], f"got {r['text']!r}")
        print(f"       {r['latency_ms']}ms")
    # 第 4 条「不该动」= 粤语特征字句，走守卫 d（已在 skip 闸测过，这里再钉一次原文）。
    yue = "你哋公司喺香港，我係用顺丰嘅"
    r = svc.correct({"text": yue, "lang": "zh"})
    check("保持: 粤语特征字句不漂移", r["text"] == yue and "cantonese_markers" in (r["skipped_reason"] or ""))

    print("  -- 守卫 combo --")
    # a 冻结 span：数字/拉丁/标点原样保留，同时仍纠正汉字错字。
    combo_in = "这个方按需要我承担运费吗，运单号AB1234"
    r = run(combo_in)
    check(
        "a: 纠汉字同时保护 AB1234/标点",
        r["text"] == "这个方案需要我承担运费吗，运单号AB1234" and r["edits"] == [{"pos": 3, "from": "按", "to": "案"}],
        f"got {r['text']!r}",
    )
    # c 预算：同一句 max_edits=0 → 放弃返回原文。
    r = run("这个方按需要我承担运费吗", max_edits=0)
    check("c: max_edits=0→over_budget 返回原文", r["skipped_reason"] == "over_budget" and r["text"] == "这个方按需要我承担运费吗")

    if lat:
        s = sorted(lat)
        p50 = s[len(s) // 2]
        p90 = s[min(len(s) - 1, int(round((len(s) - 1) * 0.9)))]
        print(f"\n  延迟: n={len(s)} p50={p50}ms p90={p90}ms max={s[-1]}ms mean={round(sum(s)/len(s),1)}ms")


def main() -> int:
    print(f"CSC sidecar selftest — version {csc.VERSION}")
    print(f"HF_HOME={os.environ.get('HF_HOME', '(unset)')} CSC_DISABLE_LOAD={csc.LOAD_DISABLED}")
    svc = CscService()

    test_guards()
    test_skip_gates(svc)
    test_model(svc)

    # /health 面（懒加载，不触发下载）。
    section("/health")
    h = svc.health()
    print("  " + str(h))
    check("health 结构完整", {"ok", "version", "model", "model_loaded", "threshold"} <= set(h))

    print(f"\n=== PASS {PASS} / FAIL {FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
