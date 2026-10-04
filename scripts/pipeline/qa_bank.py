"""QA 快答词库「种子包」导入/导出 CLI(2026-09-25)。

客户生产部署冷启动的唯一喂库口:新环境没有通话历史,挖掘闭环采不出料,
必须把预置词条包(JSONL)灌进 /api/qa-entries。此前只能逐条手 POST。

子命令:
  export  经 GET /api/qa-entries 拉词条写 JSONL(每行含 src_id+九个业务字段;
          cluster 导出时 head/变体关系靠 src_id/cluster_head_id 原样保留,
          语言/归属过滤会把被滤掉的簇 head 自动带上——跨环境迁移簇不断链)。
  import  逐行 POST /api/qa-entries;幂等=归一化问法+语言同键已存在即跳过
          (归一单点用 packages/core 的 normalize_question,不另造第二套);
          变体行的 cluster_head_id 指向源环境 id,导入时两段式先建 head/独立条、
          再重映射到本环境新 id(或复用已存在的同键词条 id),悬空引用剥掉降级
          独立条并告警——绝不把断链的 cluster_head_id 写进库。
  status  词条数/按语言分布/簇数/罐头物化状态(GET /api/qa/canned-status)。

鉴权:CONTROL_PLANE_URL env(缺省 http://127.0.0.1:8000)+ BOK_CP_TOKEN env 带
Bearer(机器通道,与 agent worker / load_audio_concurrency 同姿势)。token 缺失
且 CP 非 auth-off(请求 401/403)→ 人话报错,退出码 2。

用法:
  python scripts/pipeline/qa_bank.py export --account acc-001 --out seed.jsonl [--lang cantonese] [--include-shared]
  python scripts/pipeline/qa_bank.py import --account acc-001 --file seed.jsonl [--dry-run] [--owner-user-id ""] [--pregen]
  python scripts/pipeline/qa_bank.py status --account acc-001

退出码:0 成功;1 操作失败(连不上/文件缺失/部分行导入失败);2 鉴权(401/403)。
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
import os
import sys
from collections import Counter
from pathlib import Path

import httpx

_SCRIPTS = Path(__file__).resolve().parents[1]  # G1c 入桶后 scripts/ 根=parents[1]
_ROOT = _SCRIPTS.parent
for _p in (str(_ROOT), str(_ROOT / "packages" / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bok_voice_core.qa_text import normalize_question  # noqa: E402

DEFAULT_CP_URL = "http://127.0.0.1:8000"
# 语言三态规范值(仓规:粤语规范值=小写 cantonese,全栈唯一拼写)。
CANON_LANGS = ("zh", "cantonese", "en")
CANON_SCOPES = ("global", "step")
PRIORITY_MIN, PRIORITY_MAX = 0, 1000

# JSONL 行字段(导出/导入共用契约;src_id 仅服务簇重映射,不进 CP body)。
SEED_FIELDS = (
    "src_id",
    "question_text",
    "answer_text",
    "lang",
    "scope",
    "step_index",
    "priority",
    "enabled",
    "cluster_head_id",
)


class CpError(RuntimeError):
    """CP 请求失败(连接/非 2xx)。main 捕获 → 退出码 1。"""


class AuthError(CpError):
    """CP 401/403。main 捕获 → 人话报错,退出码 2。"""

    def __init__(self, status: int, token_set: bool):
        self.status = status
        self.token_set = token_set
        super().__init__(f"CP {status}")


def cp_base_url() -> str:
    return os.environ.get("CONTROL_PLANE_URL", DEFAULT_CP_URL).rstrip("/")


def cp_headers() -> dict:
    headers: dict[str, str] = {}
    token = os.environ.get("BOK_CP_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _log(msg: str) -> None:
    """进度/汇总走 stderr——export 不带 --out 时 stdout 必须是纯 JSONL。"""
    print(msg, file=sys.stderr)


class CpClient:
    """httpx 薄封装;测试用 duck-typing 假客户端顶替(方法面同这四个)。"""

    def __init__(self, base_url: str, headers: dict | None = None, timeout: float = 30.0):
        self._base = base_url.rstrip("/")
        self._headers = headers or {}
        self._timeout = timeout

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict | list:
        token_set = bool(self._headers.get("Authorization"))
        try:
            resp = httpx.request(
                method,
                f"{self._base}{path}",
                params=params,
                json=json_body,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise CpError(f"无法连接控制面 {self._base}:{exc}") from exc
        if resp.status_code in (401, 403):
            raise AuthError(resp.status_code, token_set)
        if resp.status_code >= 400:
            raise CpError(f"CP {method} {path} → {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not isinstance(data, (dict, list)):
            raise CpError(f"CP {method} {path} 响应不是 JSON 对象/数组")
        return data

    def list_qa_entries(self, account_id: str) -> list:
        data = self._request("GET", "/api/qa-entries", params={"account_id": account_id})
        return data if isinstance(data, list) else []

    def create_qa_entry(self, payload: dict) -> dict:
        data = self._request("POST", "/api/qa-entries", json_body=payload)
        if not isinstance(data, dict):
            raise CpError("POST /api/qa-entries 响应不是 JSON 对象")
        return data

    def qa_pregen(self, entry_ids: list[str]) -> dict:
        data = self._request("POST", "/api/qa/pregen", json_body={"ids": [str(x) for x in entry_ids]})
        if not isinstance(data, dict):
            raise CpError("POST /api/qa/pregen 响应不是 JSON 对象")
        return data

    def qa_canned_status(self, account_id: str) -> dict:
        data = self._request("GET", "/api/qa/canned-status", params={"account_id": account_id})
        if not isinstance(data, dict):
            raise CpError("GET /api/qa/canned-status 响应不是 JSON 对象")
        return data


# ---- 纯函数面(离线单测钉住:tests/test_qa_bank.py) ----


def _int_or(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def entry_key(question_text: str, lang: str) -> tuple[str, str]:
    """幂等键=归一化问法+语言。归一单点=packages/core normalize_question。"""
    return (normalize_question(str(question_text or "")), str(lang or "").strip().lower())


def parse_seed_line(raw: str) -> tuple[dict | None, str]:
    """解析一行 JSONL → (row, reason)。row=None 时 reason=跳过原因码(坏行宽容)。

    校验:JSON 可解析/是对象/问答非空/lang 缺省回落 zh(与 CP QaEntryCreate 默认
    同口径)、显式给值必须是三态规范值/scope ∈ {global,step}/step_index 与
    priority 可转 int(priority 钳 [0,1000])/enabled 可转 bool。
    cluster_head_id 原样保留(源环境 id),重映射在导入执行期做。
    """
    try:
        obj = json.loads(raw)
    except ValueError:
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "not_object"
    question = str(obj.get("question_text") or "").strip()
    answer = str(obj.get("answer_text") or "").strip()
    if not question:
        return None, "missing_question"
    if not answer:
        return None, "missing_answer"
    lang = str(obj.get("lang") or "").strip().lower() or "zh"
    if lang not in CANON_LANGS:
        return None, "bad_lang"
    scope = str(obj.get("scope") or "global").strip().lower() or "global"
    if scope not in CANON_SCOPES:
        return None, "bad_scope"
    try:
        step_index = int(obj.get("step_index", -1))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, "bad_step_index"
    try:
        priority = int(obj.get("priority", 10))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, "bad_priority"
    priority = max(PRIORITY_MIN, min(priority, PRIORITY_MAX))
    enabled_raw = obj.get("enabled", True)
    if isinstance(enabled_raw, bool):
        enabled = enabled_raw
    elif isinstance(enabled_raw, (int, float)):
        enabled = bool(enabled_raw)
    elif isinstance(enabled_raw, str):
        flag = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}.get(
            enabled_raw.strip().lower()
        )
        if flag is None:
            return None, "bad_enabled"
        enabled = flag
    else:
        return None, "bad_enabled"
    row = {
        "src_id": str(obj.get("src_id") or "").strip(),
        "question_text": question,
        "answer_text": answer,
        "lang": lang,
        "scope": scope,
        "step_index": step_index,
        "priority": priority,
        "enabled": enabled,
        "cluster_head_id": str(obj.get("cluster_head_id") or "").strip(),
    }
    return row, ""


def seed_row_from_entry(entry: dict) -> dict:
    """CP 词条行 → JSONL 行(导出侧 shaping;字段序=SEED_FIELDS)。"""
    return {
        "src_id": str(entry.get("id") or ""),
        "question_text": str(entry.get("question_text") or ""),
        "answer_text": str(entry.get("answer_text") or ""),
        "lang": str(entry.get("lang") or ""),
        "scope": str(entry.get("scope") or "global"),
        "step_index": _int_or(entry.get("step_index"), -1),
        "priority": _int_or(entry.get("priority"), 10),
        "enabled": bool(entry.get("enabled", True)),
        "cluster_head_id": str(entry.get("cluster_head_id") or ""),
    }


def select_export_rows(entries: list[dict], lang: str = "", include_shared: bool = False) -> tuple[list[dict], int]:
    """导出选行 + 簇断链防护。

    默认只导账号共享池(owner_user_id=='',种子基线);--include-shared 连话务员
    个人词条一起导(全量快照)。--lang 按语言过滤。被过滤行若有变体入选而其
    head 不在选中集 → head 自动补带(哪怕语言/归属不同)——导出包必须自洽,
    否则导入端无从重映射,簇必断链。返回 (行列表, 自动补带的 head 数)。
    """
    by_id = {str(e.get("id") or ""): e for e in entries}
    chosen: list[dict] = []
    chosen_ids: set[str] = set()
    for e in entries:
        if not include_shared and str(e.get("owner_user_id") or "") != "":
            continue
        if lang and str(e.get("lang") or "") != lang:
            continue
        chosen.append(e)
        chosen_ids.add(str(e.get("id") or ""))
    auto_heads = 0
    for e in list(chosen):
        head_id = str(e.get("cluster_head_id") or "")
        if head_id and head_id not in chosen_ids and head_id in by_id:
            chosen.append(by_id[head_id])
            chosen_ids.add(head_id)
            auto_heads += 1
    return chosen, auto_heads


def plan_import(rows: list[dict], existing: list[dict], owner_user_id: str = "") -> dict:
    """导入计划(纯函数;dry-run 与真跑共用)。

    幂等口径:同 (normalize_question(问法), lang) 键已在目标库 **或本批先到**
    → skip(duplicate);不同语言同问法是两个键,互不误跳。
    两段式建单:phase 1=无簇引用行(head/独立条),phase 2=变体行(带
    head_src_id+head_key 供执行期重映射);head 不在文件内=悬空,降级独立条
    (cluster_head_id 剥空)并记 dangling 告警,绝不写断链引用。

    返回 {"creates": [...], "skipped": [...], "dangling": [...]}:
      creates[i] = {"line_no", "phase", "row"(归一后行), "head_src_id", "head_key"}
      skipped[i] = {"line_no", "reason", "question_text", "lang"}
      dangling[i]= {"line_no", "head_src_id"}
    """
    key2id: dict[tuple[str, str], str] = {}
    for e in existing or []:
        k = entry_key(str(e.get("question_text") or ""), str(e.get("lang") or ""))
        eid = str(e.get("id") or "")
        if k not in key2id and eid:
            key2id[k] = eid
    src2row: dict[str, dict] = {}
    for r in rows or []:
        sid = str(r.get("src_id") or "")
        if sid and sid not in src2row:
            src2row[sid] = r
    creates: list[dict] = []
    skipped: list[dict] = []
    dangling: list[dict] = []
    seen: set[tuple[str, str]] = set(key2id.keys())
    for i, r in enumerate(rows or []):
        line_no = i + 1
        key = entry_key(r["question_text"], r["lang"])
        if key in seen:
            skipped.append(
                {
                    "line_no": line_no,
                    "reason": "duplicate",
                    "question_text": r["question_text"],
                    "lang": r["lang"],
                }
            )
            continue
        seen.add(key)
        head_src = str(r.get("cluster_head_id") or "")
        if head_src and head_src in src2row:
            head_row = src2row[head_src]
            creates.append(
                {
                    "line_no": line_no,
                    "phase": 2,
                    "row": dict(r),
                    "head_src_id": head_src,
                    "head_key": entry_key(head_row["question_text"], head_row["lang"]),
                }
            )
            continue
        row = dict(r)
        if head_src:
            # 悬空引用:head 不随包(手工裁剪/旧版导出)。剥链降级独立条,不断链入库。
            row["cluster_head_id"] = ""
            dangling.append({"line_no": line_no, "head_src_id": head_src})
        creates.append({"line_no": line_no, "phase": 1, "row": row, "head_src_id": "", "head_key": ("", "")})
    creates.sort(key=lambda c: (c["phase"], c["line_no"]))
    return {"creates": creates, "skipped": skipped, "dangling": dangling}


def create_payload(row: dict, head_id: str, account_id: str, owner_user_id: str) -> dict:
    """JSONL 行 → POST /api/qa-entries body(src_id 不进 body;source 走默认 curated)。"""
    return {
        "question_text": row["question_text"],
        "answer_text": row["answer_text"],
        "lang": row["lang"],
        "scope": row["scope"],
        "step_index": int(row["step_index"]),
        "priority": int(row["priority"]),
        "enabled": bool(row["enabled"]),
        "cluster_head_id": head_id,
        "account_id": account_id,
        "owner_user_id": owner_user_id,
    }


def read_seed_rows(file_path: str | Path) -> tuple[list[dict], list[tuple[int, str]]]:
    """读 JSONL → (有效行, 坏行[(行号, 原因)])。空行忽略不计。"""
    text = Path(file_path).read_text(encoding="utf-8")
    rows: list[dict] = []
    bad: list[tuple[int, str]] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        row, reason = parse_seed_line(raw)
        if row is None:
            bad.append((line_no, reason))
        else:
            rows.append(row)
    return rows, bad


def _reason_tally(items: list[dict] | list[tuple[int, str]], key_of) -> str:
    counter = Counter(key_of(x) for x in items)
    return ", ".join(f"{k}={v}" for k, v in sorted(counter.items())) or "(无)"


# ---- 子命令 ----


def cmd_export(client: CpClient, args: argparse.Namespace) -> int:
    entries = client.list_qa_entries(args.account)
    chosen, auto_heads = select_export_rows(entries, lang=str(args.lang or ""), include_shared=bool(args.include_shared))
    lines = [json.dumps(seed_row_from_entry(e), ensure_ascii=False) for e in chosen]
    if args.out:
        # 输出产物写文件是允许的(JSONL 种子包,不是源码);路径穿越拒绝(../)。
        _out = Path(args.out)
        if ".." in _out.parts:
            raise ValueError(f"refusing traversal write target: {_out}")
        with _out.resolve().open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        _log(f"导出 {len(lines)} 条 → {args.out}(account={args.account} lang={args.lang or '全部'} include_shared={args.include_shared})")
    else:
        sys.stdout.write("".join(line + "\n" for line in lines))
        _log(f"导出 {len(lines)} 条 → stdout(account={args.account} lang={args.lang or '全部'} include_shared={args.include_shared})")
    if auto_heads:
        _log(f"注:自动补带 {auto_heads} 条被过滤掉的簇 head(防断链,随包导出)")
    return 0


def cmd_import(client: CpClient, args: argparse.Namespace) -> int:
    try:
        rows, bad = read_seed_rows(args.file)
    except (OSError, ValueError) as exc:  # ValueError 覆盖非 UTF-8(UnicodeDecodeError)
        _log(f"读不到种子文件 {args.file}:{exc}")
        return 1
    if not rows and not bad:
        _log(f"种子文件 {args.file} 无有效行(空文件?)")
        return 1
    existing = client.list_qa_entries(args.account)
    plan = plan_import(rows, existing, owner_user_id=str(args.owner_user_id or ""))

    if args.dry_run:
        skip_total = len(plan["skipped"]) + len(bad)
        _log(f"dry-run:将导入 {len(plan['creates'])} 跳过 {skip_total}(零写入)")
        _log(f"  跳过原因: {_reason_tally(plan['skipped'], lambda s: s['reason'])}"
             f"{'; 坏行: ' + _reason_tally(bad, lambda b: b[1]) if bad else ''}")
        for item in plan["creates"]:
            head_note = f" (变体 ← {item['head_src_id']})" if item["phase"] == 2 else ""
            _log(f"  将导入 L{item['line_no']}: [{item['row']['lang']}] {item['row']['question_text'][:40]}{head_note}")
        for s in plan["skipped"]:
            _log(f"  跳过 L{s['line_no']} ({s['reason']}): [{s['lang']}] {s['question_text'][:40]}")
        for line_no, reason in bad:
            _log(f"  坏行 L{line_no} ({reason})")
        for d in plan["dangling"]:
            _log(f"  告警 L{d['line_no']}: head {d['head_src_id']} 不在包内,该行将降级为独立条")
        return 0

    key2id: dict[tuple[str, str], str] = {}
    for e in existing:
        k = entry_key(str(e.get("question_text") or ""), str(e.get("lang") or ""))
        eid = str(e.get("id") or "")
        if k not in key2id and eid:
            key2id[k] = eid
    src2id: dict[str, str] = {}
    created_ids: list[str] = []
    created = failed = 0
    for item in plan["creates"]:
        row = item["row"]
        head_id = ""
        if item["phase"] == 2:
            head_id = src2id.get(item["head_src_id"], "") or key2id.get(item["head_key"], "")
            if not head_id:
                _log(f"告警 L{item['line_no']}: head 解析不到(建失败且库内无同键),降级独立条")
        try:
            out = client.create_qa_entry(create_payload(row, head_id, args.account, str(args.owner_user_id or "")))
        except CpError as exc:
            failed += 1
            _log(f"失败 L{item['line_no']}: [{row['lang']}] {row['question_text'][:40]} → {exc}")
            continue
        new_id = str(out.get("id") or "")
        created += 1
        created_ids.append(new_id)
        key2id.setdefault(entry_key(row["question_text"], row["lang"]), new_id)
        if row.get("src_id"):
            src2id.setdefault(str(row["src_id"]), new_id)
        _log(f"导入 L{item['line_no']}: [{row['lang']}] {row['question_text'][:40]} → {new_id}"
             + (f"(簇 ← {head_id})" if head_id else ""))
    for s in plan["skipped"]:
        _log(f"跳过 L{s['line_no']} ({s['reason']}): [{s['lang']}] {s['question_text'][:40]}")
    for line_no, reason in bad:
        _log(f"坏行 L{line_no} ({reason})")
    for d in plan["dangling"]:
        _log(f"告警 L{d['line_no']}: head {d['head_src_id']} 不在包内,已降级独立条")
    _log(f"导入完成:新建 {created} 跳过 {len(plan['skipped']) + len(bad)} 失败 {failed}")
    if args.pregen:
        if not created_ids:
            _log("--pregen:本批无新建词条,跳过物化")
        else:
            out = client.qa_pregen(created_ids)
            status = str(out.get("status") or "")
            if status == "queued":
                _log(f"罐头物化已排队(pid={out.get('pid')}),日志 {out.get('log', '')}")
            elif status == "already_running":
                _log("罐头物化已在进行中(单飞锁),未重复触发")
            elif status == "script_missing":
                _log("CP 侧 pregen 脚本缺失,未触发物化(答案未罐头化,快路不会命中)")
            else:
                _log(f"罐头物化响应: {json.dumps(out, ensure_ascii=False)}")
    return 1 if failed else 0


def cmd_status(client: CpClient, args: argparse.Namespace) -> int:
    entries = client.list_qa_entries(args.account)
    per_lang = Counter(str(e.get("lang") or "?") for e in entries)
    variants = [e for e in entries if str(e.get("cluster_head_id") or "")]
    clusters = len({str(e.get("cluster_head_id") or "") for e in variants})
    print(f"账号 {args.account}:词条 {len(entries)} 条")
    print("  语言分布: " + (", ".join(f"{k}={v}" for k, v in sorted(per_lang.items())) or "(无)"))
    print(f"  同义簇: {clusters} 个簇 / {len(variants)} 条变体")
    st = client.qa_canned_status(args.account)
    statuses = st.get("statuses") or {}
    tally = Counter(str((v or {}).get("state") or "?") for v in statuses.values())
    if st.get("available") is False:
        print("  罐头状态面不可用(CP 侧 pregen 探测失败,详情看 CP 日志)")
    print(
        "  罐头物化: "
        + (", ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "(无记录)")
        + f"(共 {len(statuses)} 条状态,generated_at={st.get('generated_at', '')})"
    )
    tts_provider = st.get("tts_provider") or {}
    if tts_provider:
        print("  逐语言 TTS provider: " + json.dumps(tts_provider, ensure_ascii=False))
    voice_source = st.get("voice_source") or {}
    if voice_source:
        print("  逐语言音色来源: " + json.dumps(voice_source, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qa_bank.py",
        description="QA 快答词库种子包导入/导出 CLI(冷启动喂库口;归一/幂等/簇链与 CP 同源)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_export = sub.add_parser("export", help="经 GET /api/qa-entries 导出 JSONL 种子包")
    p_export.add_argument("--account", default="acc-001", help="目标账号(缺省 acc-001)")
    p_export.add_argument("--out", default="", help="输出 JSONL 路径;缺省写 stdout")
    p_export.add_argument("--lang", default="", choices=CANON_LANGS, help="按语言过滤(缺省全语言)")
    p_export.add_argument(
        "--include-shared",
        action="store_true",
        help="连话务员个人词条一起导(全量快照);缺省只导账号共享池 owner_user_id=''",
    )
    p_export.set_defaults(handler=cmd_export)

    p_import = sub.add_parser("import", help="逐行 POST /api/qa-entries 导入种子包(幂等)")
    p_import.add_argument("--account", default="acc-001", help="目标账号(缺省 acc-001)")
    p_import.add_argument("--file", required=True, help="种子 JSONL 路径")
    p_import.add_argument("--dry-run", action="store_true", help="只报「将导入 N 跳过 M(原因)」,零写入")
    p_import.add_argument(
        "--owner-user-id",
        default="",
        help="导入词条归属:''=账号共享(缺省);仅 root/admin/机器通道可指派,user 身份 CP 强制归本人",
    )
    p_import.add_argument(
        "--pregen",
        action="store_true",
        help="导入后调 POST /api/qa/pregen 把新建词条答案物化成罐头(机器通道过配额闸)",
    )
    p_import.set_defaults(handler=cmd_import)

    p_status = sub.add_parser("status", help="词条数/语言分布/簇数/罐头物化状态")
    p_status.add_argument("--account", default="acc-001", help="目标账号(缺省 acc-001)")
    p_status.set_defaults(handler=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = CpClient(cp_base_url(), cp_headers())
    try:
        return args.handler(client, args)
    except AuthError as exc:
        if exc.token_set:
            _log(
                f"CP 返回 {exc.status}:BOK_CP_TOKEN 无效,或该身份没有 qa 页面权限。\n"
                "检查 BOK_CP_TOKEN 值,或换用具备 qa 权限的身份(root/admin/机器通道)。"
            )
        else:
            _log(
                f"CP 返回 {exc.status}:控制面开启了鉴权(auth-on),而 BOK_CP_TOKEN 未设置。\n"
                "请先 export BOK_CP_TOKEN=<机器通道令牌> 再跑(与 agent worker 同一通道;参考 AGENTS.md auth-on 标准姿势)。"
            )
        return 2
    except CpError as exc:
        _log(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
