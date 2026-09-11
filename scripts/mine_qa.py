"""高频问答对挖掘报告(bok.py tts-mine 的执行体,PR-3;2026-09-11 加 --sync)。

从 CP /api/reports/qa-pairs 取报告(归一化聚类、按出现通话数排序)打印;
--apply N 把前 N 条入库为 qa_entries(source=mined,答案取各报告条目的
众数答案)。入库后跑 `bok.py tts-pregen` 为新条目合成应答音频——闸门只认
「缓存有音频」的条目。

--sync 自动学习闭环:挖掘→质量闸→入库→按语言物化 TTS→汇报,一条命令。
设计立场:保守自动+人工否决(2026-09-11)——错答案一旦罐头化就是复读机,
闸(bok_voice_core.qa_text.auto_apply_verdict)必须严;闸外条目按原因码
打印给人看,人可用 DELETE /api/qa-entries/{id} 否决。--dry-run 只打印
auto/skip 两列表不写库(单独用同义)。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "packages" / "core") not in sys.path:
    sys.path.insert(0, str(ROOT / "packages" / "core"))

from bok_voice_core.qa_text import (  # noqa: E402
    AUTO_MAX_Q_LEN,
    AUTO_MIN_CALLS,
    AUTO_MIN_Q_LEN,
    AUTO_MIN_VOTE_RATIO,
    auto_apply_verdict,
    normalize_question,
)


def _cp_request(base: str, path: str, token: str, *, method: str = "GET", payload: dict | None = None) -> object:
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    with urllib.request.urlopen(req, data=data, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def plan_sync(pairs: list[dict], existing_rows: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """--sync 主循环的可测核心:逐条过 auto_apply_verdict,分 auto/skip 两列。

    existing_rows 形如 GET /api/qa-entries 的行;question_text 归一后建查重
    集合(与闸同源 normalize_question)。skipped 元素为 (报告行, 原因码)。
    """
    existing = {
        normalize_question(str(e.get("question_text") or ""))
        for e in existing_rows or []
        if str(e.get("question_text") or "").strip()
    }
    auto: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    for r in pairs or []:
        ok, reason = auto_apply_verdict(r, existing)
        if ok:
            auto.append(r)
        else:
            skipped.append((r, reason))
    return auto, skipped


def _entry_payload(r: dict, account: str) -> dict:
    """入库 payload(与 --apply 同姿势):source=mined/scope=global/enabled=true。"""
    return {
        "question_text": r["question"],
        "answer_text": r["answer"],
        "lang": r["lang"],
        "scope": "global",
        "account_id": account,
        "source": "mined",
        "enabled": True,
    }


def _run_pregen(cp: str) -> bool:
    """跑 scripts/pregen_tts.py --qa 物化(继承 env:BOK_CP_URL/TOKEN、
    MINIMAX_API_KEY、SSL_CERT_FILE 均由 bok.py/调用方透传)。返回是否成功。"""
    pregen = Path(__file__).resolve().parent / "pregen_tts.py"
    try:
        proc = subprocess.run([sys.executable, str(pregen), "--qa", "--cp", cp])
    except OSError as exc:
        print(f"pregen spawn failed: {exc!r}", flush=True)
        return False
    return proc.returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Q→A 高频问答对挖掘")
    ap.add_argument("--cp", default=os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--account", default="acc-001")
    ap.add_argument("--min-calls", type=int, default=5, help="至少出现在 N 通电话才进报告")
    ap.add_argument("--apply", type=int, default=0, help="把报告前 N 条入库为 qa_entries(source=mined)")
    ap.add_argument("--sync", action="store_true", help="自动学习闭环:质量闸→入库→tts-pregen --qa 按语言物化")
    ap.add_argument("--dry-run", action="store_true", help="只打印 auto/skip 两列表,不写库不物化(单独用同义)")
    ap.add_argument("--no-pregen", action="store_true", help="跳过入库后的 tts-pregen --qa 物化")
    args = ap.parse_args(argv)
    token = os.environ.get("BOK_CP_TOKEN", "")

    if args.sync and args.apply:
        print("--sync 与 --apply 互斥(一条命令各干各的)", flush=True)
        return 2

    try:
        rows = _cp_request(
            args.cp,
            f"/api/reports/qa-pairs?min_calls={args.min_calls}&account_id={args.account}&limit=100&exclude_test=true",
            token,
        )
    except urllib.error.HTTPError as exc:
        print(f"report failed: HTTP {exc.code}", flush=True)
        return 1
    if not rows:
        print(f"no qa pairs >= {args.min_calls} calls (account {args.account})", flush=True)
        return 0

    print(f"{'calls':>5}  {'lang':<9} question -> answer")
    for r in rows:
        print(f"{r['calls']:>5}  {r['lang']:<9} {r['question']} -> {r['answer'][:48]} ({r['answer_votes']} votes)")
    print(f"total {len(rows)} pairs (threshold >= {args.min_calls} calls)", flush=True)

    if not (args.sync or args.dry_run):
        return _apply_top_n(args, rows, token)
    return _sync(args, rows, token)


def _apply_top_n(args: argparse.Namespace, rows: list[dict], token: str) -> int:
    """--apply 旧路径(保留不动):前 N 条无脑入库。"""
    applied = 0
    if args.apply:
        for r in rows[: max(0, args.apply)]:
            try:
                _cp_request(args.cp, "/api/qa-entries", token, method="POST", payload=_entry_payload(r, args.account))
                applied += 1
            except urllib.error.HTTPError as exc:
                print(f"apply failed for {r['question'][:32]!r}: HTTP {exc.code}", flush=True)
        print(f"applied {applied} entries — 记得跑 `bok.py tts-pregen` 物化应答音频", flush=True)
    return 0


def _sync(args: argparse.Namespace, rows: list[dict], token: str) -> int:
    """--sync / --dry-run:质量闸分列 → 打印 → (--dry-run 止步)入库 → 物化。"""
    try:
        existing_rows = _cp_request(args.cp, f"/api/qa-entries?account_id={args.account}", token) or []
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        # 查重集合拿不到就保守中止——宁可漏收,不可重复入库
        print(f"list qa entries failed: {exc!r} — 中止(保守:查重失败不入库)", flush=True)
        return 1

    auto, skipped = plan_sync(rows, existing_rows)
    print(
        f"[sync] auto {len(auto)} / skipped {len(skipped)}"
        f" (闸:calls>={AUTO_MIN_CALLS} 票比>={AUTO_MIN_VOTE_RATIO}"
        f" 问法 {AUTO_MIN_Q_LEN}-{AUTO_MAX_Q_LEN}字 无4位数字 不与现有重复)",
        flush=True,
    )
    for r in auto:
        print(f"  AUTO {r['calls']:>5}  {r['lang']:<9} {r['question']} -> {r['answer'][:48]}")
    by_reason: dict[str, list[dict]] = {}
    for r, reason in skipped:
        by_reason.setdefault(reason, []).append(r)
    for reason in sorted(by_reason):
        rs = by_reason[reason]
        samples = "; ".join(f"{r['question'][:24]}({r['calls']})" for r in rs[:3])
        print(f"  SKIP {reason} x{len(rs)}: {samples}")
    if args.dry_run:
        print("[sync] dry-run:未写库(人工复核后 `bok.py tts-mine --sync` 落地)", flush=True)
        return 0

    applied = 0
    for r in auto:
        try:
            _cp_request(args.cp, "/api/qa-entries", token, method="POST", payload=_entry_payload(r, args.account))
            applied += 1
        except urllib.error.HTTPError as exc:
            print(f"apply failed for {r['question'][:32]!r}: HTTP {exc.code}", flush=True)
    print(f"[sync] 入库 {applied} 条", flush=True)
    if args.no_pregen or applied == 0:
        return 0
    if not _run_pregen(args.cp):
        # pregen 自带 CP/SSL 处理;缺 key 等问题它自己已报错——入库成功不算失败
        print("词条已入库,稍后跑 `bok.py tts-pregen --qa` 物化", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
