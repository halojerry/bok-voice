"""高频问答对挖掘报告(bok.py tts-mine 的执行体,PR-3)。

从 CP /api/reports/qa-pairs 取报告(归一化聚类、按出现通话数排序)打印;
--apply N 把前 N 条入库为 qa_entries(source=mined,答案取各报告条目的
众数答案)。入库后跑 `bok.py tts-pregen` 为新条目合成应答音频——闸门只认
「缓存有音频」的条目。
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request


def _cp_request(base: str, path: str, token: str, *, method: str = "GET", payload: dict | None = None) -> object:
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    with urllib.request.urlopen(req, data=data, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Q→A 高频问答对挖掘")
    ap.add_argument("--cp", default=os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--account", default="acc-001")
    ap.add_argument("--min-calls", type=int, default=5, help="至少出现在 N 通电话才进报告")
    ap.add_argument("--apply", type=int, default=0, help="把报告前 N 条入库为 qa_entries(source=mined)")
    args = ap.parse_args()
    token = os.environ.get("BOK_CP_TOKEN", "")

    try:
        rows = _cp_request(
            args.cp,
            f"/api/reports/qa-pairs?min_calls={args.min_calls}&account_id={args.account}&limit=100",
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

    applied = 0
    if args.apply:
        for r in rows[: max(0, args.apply)]:
            try:
                _cp_request(
                    args.cp,
                    "/api/qa-entries",
                    token,
                    method="POST",
                    payload={
                        "question_text": r["question"],
                        "answer_text": r["answer"],
                        "lang": r["lang"],
                        "scope": "global",
                        "account_id": args.account,
                        "source": "mined",
                        "enabled": True,
                    },
                )
                applied += 1
            except urllib.error.HTTPError as exc:
                print(f"apply failed for {r['question'][:32]!r}: HTTP {exc.code}", flush=True)
        print(f"applied {applied} entries — 记得跑 `bok.py tts-pregen` 物化应答音频", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
