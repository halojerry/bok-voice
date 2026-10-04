#!/usr/bin/env python3
"""意图目录加载器(W1a,2026-09-23):把 scripts/data/intent_catalog_v1.json 灌进模板 graph_json。

走 CP sanctioned 写入口(PUT /api/templates/{id} 单键部分更新,exclude_unset 语义),
服务端 validate_flow_graph 校验、revision 快照、审计 template.update 全部天然在手——
**绝不直写 DB**。加载只改 live 草稿;发布(/publish)留给运营拍板,加载器只提醒。

用法:
  python scripts/load_intent_catalog.py                # dry:解析目录+校验+对账现状
  python scripts/load_intent_catalog.py --apply        # 写入 live 草稿(可经 revision 回滚)
  python scripts/load_intent_catalog.py --ids b0d50586a040  # 只灌指定模板
  python scripts/load_intent_catalog.py --lang zh      # 只灌指定语言

意图/绑定 id 确定性派生(sha256(class+lang)[:8])——重跑幂等,同目录数据恒得同 id,
不会累积重复意图。绑定 QA 音频须物化:加载后按打印出的 pregen 命令补录音。
网络面:默认只允许环回地址(本机 CP);远程 CP 须显式 --allow-remote-host(携带
Bearer token 出网是人工拍板动作)。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

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


def _assert_safe_cp_url(cp: str, *, allow_remote: bool) -> None:
    """SSRF 边界:协议限 http/https,主机解析后逐 IP 校验。

    默认档只放行环回(本机 CP 是这个工具的唯一常态目标);--allow-remote-host
    才放行解析到的非环回地址(带 Bearer token 出网=人工拍板动作)。解析失败、
    缺主机、非常规协议一律拒绝。
    """
    parsed = urllib.parse.urlparse(cp)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported CP url scheme: {parsed.scheme!r} (http/https only)")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("CP url missing host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    for _, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_loopback:
            continue
        if allow_remote:
            continue
        raise ValueError(
            f"CP url host {host} resolves to non-loopback {ip}; pass --allow-remote-host if intended"
        )


def _cp_request(cp: str, path: str, *, method: str = "GET", body: dict | None = None,
                _allow_remote: bool = False) -> tuple[int, object]:
    # 出站闸与 sink 同函数体（Mimosa L3 污点纪律）：每次请求前就地过环回/DNS
    # 边界校验——main 的入口校验保留为 fail-fast 第一道，此处为 sink 级第二道。
    _assert_safe_cp_url(cp, allow_remote=_allow_remote)
    url = f"{cp.rstrip('/')}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    token = os.environ.get("BOK_CP_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return exc.code, raw


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
        _assert_safe_cp_url(args.cp, allow_remote=args.allow_remote_host)
    except ValueError as exc:
        print(f"FAIL: {exc}")
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
        print("  python scripts/pregen_tts.py --qa " + " ".join(f"--entry-id {q}" for q in sorted(qa_all)))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
