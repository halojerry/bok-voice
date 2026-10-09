#!/usr/bin/env python3
"""意图目录加载器(W1a,2026-09-23):把 scripts/data/intent_catalog_v1.json 灌进模板 graph_json。

走 CP sanctioned 写入口(PUT /api/templates/{id} 单键部分更新,exclude_unset 语义),
服务端 validate_flow_graph 校验、revision 快照、审计 template.update 全部天然在手——
**绝不直写 DB**。加载只改 live 草稿;发布(/publish)留给运营拍板,加载器只提醒。

用法:
  python scripts/seed/load_intent_catalog.py                # dry:解析目录+校验+对账现状
  python scripts/seed/load_intent_catalog.py --apply        # 写入 live 草稿(可经 revision 回滚)
  python scripts/seed/load_intent_catalog.py --ids b0d50586a040  # 只灌指定模板
  python scripts/seed/load_intent_catalog.py --lang zh      # 只灌指定语言

意图/绑定 id 确定性派生(sha256(class+lang)[:8])——重跑幂等,同目录数据恒得同 id,
不会累积重复意图。绑定 QA 音频须物化:加载后按打印出的 pregen 命令补录音。
网络面:默认只允许环回地址(本机 CP);远程 CP 须显式 --allow-remote-host(携带
Bearer token 出网是人工拍板动作)。
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
import hashlib
import json
import os
import sys
import urllib.parse

import cp_outbound  # noqa: E402  CP 出站共享单点（G1 引导头后可裸 import scripts/lib）

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_PATH = os.path.join(REPO_ROOT, "scripts", "data", "intent_catalog_v1.json")


def _intent_id(lang: str, cls: str) -> str:
    return "int_" + hashlib.sha256(f"intent:{lang}:{cls}".encode()).hexdigest()[:8]


def _binding_id(lang: str, cls: str) -> str:
    return "bnd_" + hashlib.sha256(f"binding:{lang}:{cls}".encode()).hexdigest()[:8]


def build_graph(lang_doc: dict, lang: str) -> dict:
    """目录 lang 段 → flow_graph 契约形状(version=1, intents, bindings)。"""
    intents, bindings = [], []
    for it in lang_doc.get("intents", []):
        cls = str(it.get("class") or "").strip()
        if not cls:
            continue
        iid = _intent_id(lang, cls)
        entry: dict = {
            "id": iid,
            "label": str(it.get("label") or cls)[:64],
            "keywords": [str(k) for k in it.get("keywords", []) if str(k).strip()],
            "steps": list(it.get("steps") or []),
            "enabled": True,
        }
        judge = it.get("judge")
        prompt = str(judge.get("prompt") or "").strip() if isinstance(judge, dict) else ""
        if prompt:
            entry["judge"] = {"prompt": prompt}
        intents.append(entry)
        b = it.get("binding") or {}
        action = str(b.get("action") or "")
        if action:
            bind: dict = {
                "id": _binding_id(lang, cls),
                "intent": iid,
                "action": action,
                "priority": int(b.get("priority", 10)),
                "once": bool(b.get("once", False)),
                "enabled": True,
            }
            if action == "play_qa":
                bind["qa_id"] = str(b.get("qa_id") or "")
            if action == "jump_step":
                bind["step"] = int(b.get("step", 1))
            bindings.append(bind)
    return {"version": 1, "intents": intents, "bindings": bindings}


def _validate_client(graph: dict) -> list[str]:
    """客户端先过 CP 同款校验器(packages/core 单源),错误不出门。"""
    sys.path.insert(0, os.path.join(REPO_ROOT, "packages", "core"))
    from bok_voice_core.flow_graph import validate_flow_graph  # noqa: PLC0415

    return validate_flow_graph(json.dumps(graph, ensure_ascii=False))


def _allow_hosts(cp: str, allow_remote: bool) -> tuple[str, ...]:
    """--allow-remote-host 语义映射：显式拍板放行的 CP host 进共享闸 extra_hosts。

    （带 Bearer token 出网是人工拍板动作——旗标开=把该 CP host 显式声明进
    白名单；共享闸另认 env BOK_PROBE_EXTRA_HOSTS 扩展口。）"""
    if not allow_remote:
        return ()
    host = (urllib.parse.urlsplit(cp).hostname or "").strip().lower()
    return (host,) if host else ()


def _cp_request(cp: str, path: str, *, method: str = "GET", body: dict | None = None,
                _allow_remote: bool = False) -> tuple[int, object]:
    """CP sanctioned 写入口——出站闸与 urlopen sink 在共享单点
    scripts/lib/cp_outbound.cp_request_status（2026-10-09 L3 收敛；HTTPError
    内捕返回 (status, parsed-or-raw) 的旧形状原样）。每次请求前过闸=边界
    校验与 sink 同函数体（main 入口校验保留为 fail-fast 第一道，此处为
    sink 级第二道）。"""
    return cp_outbound.cp_request_status(
        cp, path,
        token=os.environ.get("BOK_CP_TOKEN", "").strip(),
        method=method, payload=body,
        extra_hosts=_allow_hosts(cp, _allow_remote),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", default=CATALOG_PATH, help="目录 JSON 路径")
    ap.add_argument("--cp", default=os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--apply", action="store_true", help="真正写入(缺省 dry 只对账)")
    ap.add_argument("--ids", default="", help="逗号分隔只灌这些模板 id")
    ap.add_argument("--lang", default="", help="只灌指定语言(zh/cantonese/en)")
    ap.add_argument("--allow-remote-host", action="store_true", help="放行非环回 CP 地址(带 token 出网须人工拍板)")
    args = ap.parse_args()

    try:
        cp_outbound.guard_url(args.cp, extra_hosts=_allow_hosts(args.cp, args.allow_remote_host))
    except PermissionError as exc:
        print(f"FAIL: {exc}（远程 CP：--allow-remote-host 或 env BOK_PROBE_EXTRA_HOSTS 显式放行）")
        return 1

    with open(args.catalog, encoding="utf-8") as fh:
        catalog = json.load(fh)

    want_ids = {s.strip() for s in args.ids.split(",") if s.strip()}
    want_lang = args.lang.strip()

    status, tpls = _cp_request(args.cp, "/api/templates", _allow_remote=args.allow_remote_host)
    if status != 200:
        print(f"FAIL: GET /api/templates -> {status} {tpls}")
        return 1
    tpl_list = tpls.get("items", tpls) if isinstance(tpls, dict) else tpls

    catalog_templates = {t["id"]: t for t in catalog.get("templates", [])}
    exit_code = 0
    for tpl in tpl_list:
        tid = tpl.get("id")
        meta = catalog_templates.get(tid)
        if meta is None:
            continue
        if want_ids and tid not in want_ids:
            continue
        lang = str(tpl.get("language") or "")
        if lang != meta.get("lang"):
            print(f"SKIP {tid}: 目录语言 {meta.get('lang')} != 模板语言 {lang}(防错灌)")
            continue
        if want_lang and lang != want_lang:
            continue
        lang_doc = catalog["langs"].get(lang)
        if not lang_doc:
            print(f"SKIP {tid}: 目录无 {lang} 段")
            continue
        graph = build_graph(lang_doc, lang)
        errs = _validate_client(graph)
        if errs:
            print(f"FAIL {tid} [{lang}]: 目录非法:\n  " + "\n  ".join(errs[:6]))
            exit_code = 1
            continue

        cur = str(tpl.get("graph_json") or "")
        cur_intents = 0
        try:
            cur_intents = len(json.loads(cur).get("intents", [])) if cur.strip() else 0
        except Exception:  # noqa: BLE001
            cur_intents = -1
        n_kw = sum(len(i["keywords"]) for i in graph["intents"])
        qa_ids = sorted({b["qa_id"] for b in graph["bindings"] if b.get("qa_id")})
        print(
            f"{'APPLY' if args.apply else 'DRY '} {tid} [{lang}] {str(tpl.get('name'))[:24]}: "
            f"intents {cur_intents} -> {len(graph['intents'])} (keywords={n_kw}, bindings={len(graph['bindings'])}, play_qa={len(qa_ids)})"
        )
        if not args.apply:
            continue
        st, resp = _cp_request(args.cp, f"/api/templates/{tid}", method="PUT", body={"graph_json": json.dumps(graph, ensure_ascii=False)}, _allow_remote=args.allow_remote_host)
        if st != 200:
            print(f"FAIL {tid}: PUT -> {st} {resp}")
            exit_code = 1
        else:
            print(f"  ok revision={resp.get('revision') if isinstance(resp, dict) else '?'} — live 草稿已更新")

    if exit_code == 0:
        print(
            "\n下一步(运营面):1) /templates 过目 → POST /api/templates/{id}/publish 才对机器通道生效;"
            "\n2) 绑定 QA 补录音(play_miss 只放行不消耗 once):"
        )
        qa_all = set()
        for lang, doc in catalog["langs"].items():
            for it in doc.get("intents", []):
                qa = str((it.get("binding") or {}).get("qa_id") or "")
                if qa:
                    qa_all.add(qa)
        print("  python scripts/runtime/pregen_tts.py --qa " + " ".join(f"--entry-id {q}" for q in sorted(qa_all)))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
