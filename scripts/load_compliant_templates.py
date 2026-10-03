#!/usr/bin/env python3
"""装载三语合规话术模板（data/templates/hegui-*.json → CP /api/templates）。

- 默认 dry-run：只打印将要创建/更新的摘要，不发请求；`--apply` 才写。
- 幂等：按模板 name 匹配（GET /api/templates?account_id=…）——存在 → PUT
  （部分更新，只发本文件的键）；不存在 → POST 新建。**绝不删除/修改同名以外
  的任何模板**（旧生产模板保持不动，本波只「并存」）。
- `--publish`：对落库后的模板调 POST /api/templates/{id}/publish（冻结九键+
  触发罐头 pregen）。**注意 publish 的 pregen 需要 TTS 可用**（MiniMax 恢复后
  再开）；默认关闭。
- 鉴权：BOK_CP_TOKEN 在场自动带机器通道 Bearer（scripts/e2e_real_customer.py
  同款）；auth-off 栈直通。
- steps_json 在文件里存 JSON 数组（人可编辑），装载时 json.dumps 成字符串契约。

用法：
    ./pkgruntime-aside/python/bin/python3.12 scripts/load_compliant_templates.py            # dry-run
    ./pkgruntime-aside/python/bin/python3.12 scripts/load_compliant_templates.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = ROOT / "data" / "templates"
FILES = ("hegui-zh.json", "hegui-cantonese.json", "hegui-en.json")
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000").rstrip("/")

CP_HEADERS: dict[str, str] = {}
if os.environ.get("BOK_CP_TOKEN", "").strip():
    CP_HEADERS["Authorization"] = f"Bearer {os.environ['BOK_CP_TOKEN'].strip()}"


def load_payload(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = data.get("steps_json")
    if not isinstance(steps, list):
        raise SystemExit(f"{path.name}: steps_json 必须是数组（人可编辑形态）")
    payload = dict(data)
    payload["steps_json"] = json.dumps(steps, ensure_ascii=False)
    return payload


def find_existing(client: httpx.Client, account_id: str, name: str) -> dict | None:
    r = client.get(f"{CONTROL_PLANE_URL}/api/templates", params={"account_id": account_id},
                   headers=CP_HEADERS)
    r.raise_for_status()
    for t in r.json():
        if str(t.get("name") or "") == name:
            return t
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（缺省 dry-run）")
    ap.add_argument("--publish", action="store_true", help="落库后顺带 publish（pregen 需 TTS 可用）")
    args = ap.parse_args()

    payloads = [load_payload(TEMPLATES_DIR / f) for f in FILES]
    with httpx.Client(timeout=20) as client:
        for payload in payloads:
            name, lang = payload["name"], payload["language"]
            account = payload.get("account_id") or "acc-001"
            existing = find_existing(client, account, name)
            action = "PUT " if existing else "POST"
            n_steps = len(json.loads(payload["steps_json"]))
            print(f"[plan] {action} name={name!r} lang={lang} steps={n_steps} "
                  f"id={(existing or {}).get('id', '(new)')}")
            if not args.apply:
                continue
            if existing:
                body = {k: v for k, v in payload.items() if k != "account_id"}
                r = client.put(f"{CONTROL_PLANE_URL}/api/templates/{existing['id']}",
                               json=body, headers=CP_HEADERS)
            else:
                r = client.post(f"{CONTROL_PLANE_URL}/api/templates",
                                json=payload, headers=CP_HEADERS)
            r.raise_for_status()
            saved = r.json()
            tid = saved.get("id") or (existing or {}).get("id", "")
            print(f"[ok]   {action} {name!r} id={tid}")
            if args.publish and tid:
                pr = client.post(f"{CONTROL_PLANE_URL}/api/templates/{tid}/publish",
                                 headers=CP_HEADERS)
                pr.raise_for_status()
                print(f"[ok]   publish {name!r} -> {pr.json().get('published')}")
    if not args.apply:
        print("\n(dry-run——加 --apply 落库；publish 另加 --publish，需 TTS 可用)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
