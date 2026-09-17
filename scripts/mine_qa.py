"""高频问答对挖掘报告(bok.py tts-mine 的执行体,PR-3;2026-09-11 加 --sync;
2026-09-16 加 --cluster LLM 同义聚类)。

从 CP /api/reports/qa-pairs 取报告(归一化聚类、按出现通话数排序)打印;
--apply N 把前 N 条入库为 qa_entries(source=mined,答案取各报告条目的
众数答案)。入库后跑 `bok.py tts-pregen` 为新条目合成应答音频——闸门只认
「缓存有音频」的条目。

--sync 自动学习闭环:挖掘→质量闸→入库→按语言物化 TTS→汇报,一条命令。
设计立场:保守自动+人工否决(2026-09-11)——错答案一旦罐头化就是复读机,
闸(bok_voice_core.qa_text.auto_apply_verdict)必须严;闸外条目按原因码
打印给人看,人可用 DELETE /api/qa-entries/{id} 否决。--dry-run 只打印
auto/skip 两列表不写库(单独用同义)。

--cluster LLM 同义聚类(2026-09-16,治「0.90 只认字面措辞」):本地 LLM 把
挖掘候选对着现有词条判 variant/new/junk——variant=同义同答 → 以候选原话为
question、继承目标词条 answer_text 入库(真实用户措辞即 qa_gate 匹配面;
同文同音色 → TTS 缓存键同条目,零新增合成)。默认只打印计划,--apply 才入库;
new 类不自动入库(留给 --sync 质量闸),junk 只报原因。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
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


# ---- --cluster:LLM 同义聚类(2026-09-16,治「0.90 只认字面措辞」) ----
# LLM 端点固定本机回环 mlx_lm server:主机硬编码、端口 int 校验(无动态 URL,
# 同 probe_minimax_emotion_tags 先例);model 取 /v1/models 真实路径(仓规)。
_LLM_HOST = "127.0.0.1"
_LLM_PORT = int(os.environ.get("BOK_LLM_PORT", "1235"))

_CLUSTER_SYSTEM_PROMPT = (
    "你是客服快答词库的管理员。输入是现有词条列表和新挖掘的问答候选。"
    "对每个候选独立判断:\n"
    '1. "variant":候选问题与某条现有词条意图相同,且候选答案与该词条答案语义一致'
    " → 给出该词条 id 作 target;question 字段必须原样抄候选的问题(不要改写,"
    "真实用户措辞就是匹配面)。\n"
    '2. "new":全新问答,现有库里没有同义词条 → target 留空字符串。\n'
    '3. "junk":寒暄/语气词/与业务无关/答案与相关词条语义冲突。\n'
    '只输出 JSON 数组,不要任何解释或代码块标记:'
    '[{"i":候选序号,"decision":"variant|new|junk","target":"词条id或空串","note":"不超过8字的理由"}]'
)


def _llm_chat(model: str, system: str, user: str, *, timeout: int = 180) -> str:
    """OpenAI 兼容 /v1/chat/completions(本机回环,路径常量)。"""
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 4096,
            "stream": False,
        }
    ).encode("utf-8")
    conn = http.client.HTTPConnection(_LLM_HOST, _LLM_PORT, timeout=timeout)
    try:
        conn.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
        data = json.loads(conn.getresponse().read())
    finally:
        conn.close()
    return str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")


def _llm_model() -> str:
    """取本地 server 已加载模型的真实路径(仓规:request model 必须填真实路径)。"""
    conn = http.client.HTTPConnection(_LLM_HOST, _LLM_PORT, timeout=10)
    try:
        conn.request("GET", "/v1/models")
        data = json.loads(conn.getresponse().read())
    finally:
        conn.close()
    ids = [str(d.get("id") or "") for d in (data.get("data") or [])]
    paths = [i for i in ids if i.startswith("/")]
    for p in paths:  # 4B 主对话模型优先(1.8B 是 B 线翻译专才)
        if "4b" in p.lower():
            return p
    return paths[0] if paths else (ids[0] if ids else "")


def _parse_llm_decisions(text: str) -> dict[int, dict]:
    """宽松解析 LLM 输出 → {候选序号: decision dict};非 JSON/缺字段静默跳过。"""
    t = re.sub(r"```(?:json)?|```", "", str(text or "")).strip()
    start = t.find("[")
    end = t.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        arr = json.loads(t[start : end + 1])
    except ValueError:
        return {}
    out: dict[int, dict] = {}
    if not isinstance(arr, list):
        return out
    for d in arr:
        if not isinstance(d, dict) or isinstance(d.get("i"), bool):
            continue
        try:
            i = int(d.get("i"))
        except (TypeError, ValueError):
            continue
        decision = str(d.get("decision") or "").strip().lower()
        if decision not in ("variant", "new", "junk"):
            continue
        out[i] = {
            "decision": decision,
            "target": str(d.get("target") or "").strip(),
            "note": str(d.get("note") or "")[:16],
        }
    return out


def plan_cluster(
    pairs: list[dict],
    existing_rows: list[dict],
    decisions: dict[int, dict],
) -> tuple[list[dict], list[dict], list[tuple[dict, str]]]:
    """决策 → 三列(可测核心):variants 入库 payload / new 交回 --sync 闸 / junk。

    variant 入库 payload:question=候选原话(真实措辞即匹配面),answer=继承目标
    词条 answer_text(答案以既有为准,防答案漂移)。保守门:目标词条必须存在
    且同语言;归一后与现有词条重复 → 丢。
    """
    by_id = {str(e.get("id") or ""): e for e in existing_rows or []}
    existing_norms = {
        normalize_question(str(e.get("question_text") or ""))
        for e in existing_rows or []
        if str(e.get("question_text") or "").strip()
    }
    variants: list[dict] = []
    fresh: list[dict] = []
    junk: list[tuple[dict, str]] = []
    for i, r in enumerate(pairs or []):
        d = decisions.get(i)
        if d is None:
            junk.append((r, "no-decision"))
            continue
        if d["decision"] == "variant":
            target = by_id.get(d["target"])
            if target is None:
                junk.append((r, "target-missing"))
                continue
            if str(target.get("lang") or "") != str(r.get("lang") or ""):
                junk.append((r, "lang-mismatch"))
                continue
            q = str(r.get("question") or "").strip()
            if not q:
                junk.append((r, "empty-question"))
                continue
            # 问法长度门(与 --sync 闸同源 AUTO_MIN/MAX_Q_LEN):4B judge 对
            # 「啊」「多多」类碎片会误判 variant——LLM 提议,字数门处决。
            if not (AUTO_MIN_Q_LEN <= len(q) <= AUTO_MAX_Q_LEN):
                junk.append((r, "q-len-out-of-range"))
                continue
            if normalize_question(q) in existing_norms:
                junk.append((r, "dup-existing"))
                continue
            variants.append(
                {
                    "question_text": q,
                    "answer_text": str(target.get("answer_text") or ""),
                    "lang": r["lang"],
                    "scope": "global",
                    "source": "mined",
                    "enabled": True,
                    "account_id": str(target.get("account_id") or "acc-001"),
                }
            )
            existing_norms.add(normalize_question(q))
        elif d["decision"] == "new":
            fresh.append(r)
        else:
            junk.append((r, d["note"] or "junk"))
    return variants, fresh, junk


def _cluster(args: argparse.Namespace, rows: list[dict], token: str) -> int:
    """--cluster 主循环:取词条 → 分语言批量问 LLM → 计划打印 → (--apply)入库。"""
    try:
        existing_rows = _cp_request(args.cp, f"/api/qa-entries?account_id={args.account}", token) or []
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"list qa entries failed: {exc!r} — 中止", flush=True)
        return 1
    try:
        model = args.llm_model or _llm_model()
    except Exception as exc:
        print(f"llm models 探测失败({_LLM_HOST}:{_LLM_PORT}): {exc!r} — 中止", flush=True)
        return 1
    if not model:
        print("llm 无可用模型 — 中止", flush=True)
        return 1

    variants: list[dict] = []
    fresh: list[dict] = []
    junk: list[tuple[dict, str]] = []
    langs = sorted({str(r.get("lang") or "") for r in rows} - {""})
    for lang in langs:
        lang_rows = [r for r in rows if str(r.get("lang")) == lang]
        entries = [
            {
                "id": str(e.get("id") or ""),
                "question": str(e.get("question_text") or ""),
                "answer": str(e.get("answer_text") or "")[:160],
            }
            for e in existing_rows
            if str(e.get("lang")) == lang and str(e.get("question_text") or "").strip()
        ][:60]
        if not entries:
            fresh.extend(lang_rows)  # 该语言还没有词条 → 全部交回 --sync 质量闸
            continue
        cands = [
            {"i": i, "question": str(r.get("question") or ""), "answer": str(r.get("answer") or "")[:120]}
            for i, r in enumerate(lang_rows)
        ]
        user = (
            f"现有词条(lang={lang}):{json.dumps(entries, ensure_ascii=False)}\n"
            f"候选:{json.dumps(cands, ensure_ascii=False)}"
        )
        try:
            text = _llm_chat(model, _CLUSTER_SYSTEM_PROMPT, user)
        except Exception as exc:
            print(f"[cluster:{lang}] llm 请求失败: {exc!r} — 该语言跳过", flush=True)
            continue
        decisions = _parse_llm_decisions(text)
        # 候选序号是分语言局部 i → plan_cluster 按 (lang_rows, 局部 decisions) 跑
        v, f, j = plan_cluster(lang_rows, existing_rows, decisions)
        variants.extend(v)
        fresh.extend(f)
        junk.extend(j)

    print(
        f"[cluster] variant {len(variants)} / new {len(fresh)} / junk {len(junk)}"
        f" (llm={model.rsplit('/', 1)[-1]})",
        flush=True,
    )
    for v in variants:
        print(f"  VARIANT [{v['lang']}] {v['question_text']!r} -> 继承答案 {v['answer_text'][:36]!r}")
    for r in fresh:
        print(f"  NEW     [{r['lang']}] {r['question']!r} (留待 tts-mine --sync 质量闸)")
    for r, reason in junk:
        print(f"  JUNK    [{r.get('lang')}] {str(r.get('question'))[:32]!r} ({reason})")
    if not args.apply:
        print("[cluster] dry:未写库(--cluster 配 --apply 落地 variant)", flush=True)
        return 0

    applied = 0
    for v in variants:
        try:
            _cp_request(args.cp, "/api/qa-entries", token, method="POST", payload=v)
            applied += 1
        except urllib.error.HTTPError as exc:
            print(f"apply failed for {v['question_text'][:32]!r}: HTTP {exc.code}", flush=True)
    print(
        f"[cluster] 入库 variant {applied} 条(答案同文同音色 → 缓存键已存在零重合成;"
        f"换新人设/新音色后跑 `bok.py tts-pregen --qa`)",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Q→A 高频问答对挖掘")
    ap.add_argument("--cp", default=os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--account", default="acc-001")
    ap.add_argument("--min-calls", type=int, default=5, help="至少出现在 N 通电话才进报告")
    ap.add_argument("--apply", type=int, default=0, help="把报告前 N 条入库为 qa_entries(source=mined)")
    ap.add_argument("--sync", action="store_true", help="自动学习闭环:质量闸→入库→tts-pregen --qa 按语言物化")
    ap.add_argument("--dry-run", action="store_true", help="只打印 auto/skip 两列表,不写库不物化(单独用同义)")
    ap.add_argument("--no-pregen", action="store_true", help="跳过入库后的 tts-pregen --qa 物化")
    ap.add_argument("--cluster", action="store_true", help="LLM 同义聚类:候选对着现有词条判 variant/new/junk")
    ap.add_argument("--llm-model", default=os.environ.get("BOK_LLM_CLUSTER_MODEL", ""), help="覆盖聚类用模型(默认 /v1/models 自动取 4B 路径)")
    args = ap.parse_args(argv)
    token = os.environ.get("BOK_CP_TOKEN", "")

    if args.sync and args.apply:
        print("--sync 与 --apply 互斥(一条命令各干各的)", flush=True)
        return 2
    if args.cluster and args.sync:
        print("--cluster 与 --sync 互斥(--cluster 配 --apply 落地 variant)", flush=True)
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

    if args.cluster:
        return _cluster(args, rows, token)
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
