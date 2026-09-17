"""惜客通 tbl_ai_knowledge.json → Bok qa_entries 导入器(spec §6,2026-09-17)。

用法:
  python scripts/import_xkt_qa.py --input tbl_ai_knowledge.json [--account acc-001]
      [--lang auto|zh|cantonese|en] [--owner ""] [--apply]
默认 dry-run 打印计划;--apply 走 POST /api/qa-entries(source=imported),
主条目先建、变体回填 cluster_head_id。AfterAnswer*/Priority/Trigger* 等
外部字段不搬,dry-run 报告列示——步骤挂载留给画布拖线。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

_CANTO_MARKS = re.compile(r"[唔該係嘅咗哋啲冇乜嚟]")

# 惜客通未搬运字段前缀(外部真字面量,报告列示用)。
_SKIPPED_PREFIXES = ("AfterAnswer", "Priority", "Trigger", "Interrupt", "LabelId", "Intention", "Force")


def normalize_q(text: str) -> str:
    return re.sub(r"[\s，。？！、,.\?!]+", "", str(text or "")).strip()


def detect_lang(question: str) -> str:
    return "cantonese" if _CANTO_MARKS.search(question or "") else "zh"


def plan_import(
    rows: list[dict],
    existing: set[str],
    lang_mode: str,
) -> tuple[list[dict], dict]:
    """dry-run 可测核心。creates 变体行带 _head_question(apply 时解析为真实 id)。"""
    creates: list[dict] = []
    report = {"created": 0, "variants": 0, "skipped_dup": 0, "disabled": 0,
              "alt_answers_seen": 0, "invalid": 0, "skipped_fields": set()}
    for row in rows or []:
        raw_q = str(row.get("Question") or "").strip()
        answer = str(row.get("Answer") or "").strip()
        if not raw_q or not answer:
            report["invalid"] += 1
            continue
        variants = [v.strip() for v in raw_q.split("&") if v.strip()]
        if not variants:
            report["invalid"] += 1
            continue
        head_q = variants[0]
        if normalize_q(head_q) in existing:
            report["skipped_dup"] += 1
            continue
        # Status=0=停用(0 是 falsy,不能用 or 1 兜缺省——会把停用行翻成启用)。
        raw_status = row.get("Status")
        enabled = int(raw_status if raw_status is not None else 1) == 1
        base = {
            "answer_text": answer,
            "lang": lang_mode if lang_mode != "auto" else detect_lang(head_q),
            "scope": "global",
            "step_index": -1,
            "voice_id": "",
            "source": "imported",
            "enabled": enabled,
        }
        if not enabled:
            report["disabled"] += 1
        creates.append({"question_text": head_q, "cluster_head_id": "", **base})
        report["created"] += 1
        for v in variants[1:]:
            if normalize_q(v) in existing:
                report["skipped_dup"] += 1
                continue
            creates.append({**base, "question_text": v, "cluster_head_id": None,
                            "_head_question": head_q})
            report["variants"] += 1
            report["created"] += 1
        alt = sum(1 for k in row.keys() if re.fullmatch(r"Answer[2-5]", k) and str(row.get(k) or "").strip())
        report["alt_answers_seen"] += alt
        for k in row.keys():
            if k.startswith(_SKIPPED_PREFIXES):
                report["skipped_fields"].add(k)
    return creates, report


def _cp_request(base: str, path: str, token: str, *, method: str = "GET", payload: dict | None = None) -> object:
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    with urllib.request.urlopen(req, data=data, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--account", default="acc-001")
    ap.add_argument("--cp", default=os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--lang", default="auto", choices=["auto", "zh", "cantonese", "en"])
    ap.add_argument("--owner", default="")
    ap.add_argument("--apply", action="store_true", help="缺省 dry-run")
    args = ap.parse_args()

    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        print("输入必须是 JSON 数组(惜客通 tbl_ai_knowledge.json)")
        return 2
    token = os.environ.get("BOK_CP_TOKEN", "")
    existing_rows = _cp_request(args.cp, f"/api/qa-entries?account_id={args.account}", token) or []
    existing = {normalize_q(str(e.get("question_text") or "")) for e in existing_rows}
    creates, report = plan_import(rows, existing, args.lang)
    for c in creates:
        c.setdefault("account_id", args.account)
        c.setdefault("owner_user_id", args.owner)
    # created=计划总条数(主条目+变体,与 len(creates) 同步);variants 是其中变体子集。
    print(f"计划:新建 {report['created']} 条(变体 {report['variants']}、含停用 {report['disabled']})"
          f" | 去重跳过 {report['skipped_dup']} | 非法行 {report['invalid']}"
          f" | 备选答案计数(不搬) {report['alt_answers_seen']}")
    if report["skipped_fields"]:
        print(f"未搬字段: {sorted(report['skipped_fields'])}")
    if not args.apply:
        for c in creates[:20]:
            print(f"  [{'头' if not c.get('_head_question') else '变体'}] {c['lang']} {c['question_text']}")
        print("dry-run 结束(--apply 落地)")
        return 0
    head_ids: dict[str, str] = {}
    done = 0
    for c in creates:
        head_q = c.pop("_head_question", None)
        if head_q:
            c["cluster_head_id"] = head_ids.get(head_q, "")
        row = _cp_request(args.cp, "/api/qa-entries", token, method="POST", payload=c)
        if not head_q:
            head_ids[str(c["question_text"])] = str(row.get("id"))
        done += 1
    print(f"已入库 {done} 条。下一步: python scripts/pregen_tts.py --qa 物化罐头；步骤挂载请在画布拖线。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
