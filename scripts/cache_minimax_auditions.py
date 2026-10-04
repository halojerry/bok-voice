#!/usr/bin/env python3
"""MiniMax 官方试听缓存（2026-10-02，音色目录换血配套）。

对 apps/web/lib/minimax-voices.ts 目录里的每只音色，调 MiniMax 官方合成
t2a_v2 生成一句固定试听文本，落 assets/minimax-auditions/<voice_id 安全化>.mp3
（同一次批量跑顺带当 2054 voice-not-exist 的体检——新增音色 ID 先跑本脚本）。

- 试听文本按音色语言固定三句（粤/普/英各一句，见 LANG_TEXT），语速与生产
  同规则（zh/粤 1.2、en 1.0，与 CP /api/tts/preview、gen_filler_assets 同源）；
- 目录来源=TS 文件本身（正则解析数组组 + 导出块 group→lang 映射），不复制
  清单，杜绝两处漂移——改 TS 数组语法（`["id", "label"],`）两处必须同步；
- 凭据运行期从 settings DB 读（global_settings.tts_json.api_key，只读打开），
  环境变量 MINIMAX_API_KEY 可覆盖；代码零密钥、零明文；
- SSRF 护栏：端点协议 https + host 白名单 {api.minimax.cn, api.minimax.chat}
  （MINIMAX_BASE_URL 被污染/内网重定向一律拒发），每请求前过 _url_ok；
- 幂等：目标文件已存在即跳过（--force 重生成；写盘走 .tmp + os.replace 原子落盘）；
- 限频(1002)/传输抖动退避重试 ×3，2054/鉴权错立即失败不空转。

用法：
  .venv312/bin/python scripts/cache_minimax_auditions.py            # 全量（幂等）
  .venv312/bin/python scripts/cache_minimax_auditions.py --lang zh  # 只跑普通话
  .venv312/bin/python scripts/cache_minimax_auditions.py --voice GentleLady
  .venv312/bin/python scripts/cache_minimax_auditions.py --dry-run  # 只列清单
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_TS = ROOT / "apps" / "web" / "lib" / "minimax-voices.ts"
OUT_DIR = ROOT / "assets" / "minimax-auditions"
SETTINGS_DB = Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db"

MODEL = "speech-2.8-hd"
SAMPLE_RATE = 32000  # mp3 合法档（8000/16000/22050/24000/32000/44100）
TIMEOUT_S = 60
RETRIES = 3
PACE_S = 0.6  # 逐只之间小睡，给 RPM 限频留余量

CN_BASE = "https://api.minimax.cn"
INTL_BASE = "https://api.minimax.chat"
T2A_PATH = "/v1/t2a_v2"
ALLOWED_HOSTS = frozenset({"api.minimax.cn", "api.minimax.chat"})

LANGS = ("cantonese", "zh", "en")

# 每语种一句固定试听文本（同一份音频供 web/运维批量试听，免每次现烧云配额）。
LANG_TEXT = {
    "cantonese": "你好，我係 Bok 客服。唔該想問下，我件貨而家到咗未呀？可以幫我查下進度嘛？",
    "zh": "你好，我是 Bok 客服。请问有什么可以帮您？",
    "en": "Hello, this is the Bok assistant. How can I help you today?",
}


# ---- SSRF 护栏（与 gen_filler_assets/mm_voice 同姿势：云端目标钉死字面量，
# env 覆盖也要过白名单，绝不带凭据发向非 MiniMax 主机） ----
def _url_ok(url: str) -> bool:
    parts = urllib.parse.urlsplit(str(url or ""))
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() in ALLOWED_HOSTS
        and parts.port in (None, 443)
        and not parts.username
        and not parts.password
    )


def endpoint() -> str:
    base = os.environ.get("MINIMAX_BASE_URL", "").strip().rstrip("/")
    if not base:
        region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
        base = INTL_BASE if region in {"intl", "global", "chat"} else CN_BASE
    if base.endswith(T2A_PATH):
        return base
    return f"{base}{T2A_PATH}"


def load_api_key() -> tuple[str, str]:
    """返回 (key, 来源说明)；key 绝不回显。env 优先，缺省读 settings DB。"""
    raw = os.environ.get("MINIMAX_API_KEY", "").strip()
    if raw:
        return raw, "env MINIMAX_API_KEY"
    if not SETTINGS_DB.exists():
        print(f"FAIL: 找不到设置库 {SETTINGS_DB}，也没有 MINIMAX_API_KEY env", file=sys.stderr)
        sys.exit(2)
    try:
        con = sqlite3.connect(f"file:{SETTINGS_DB}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT tts_json FROM global_settings").fetchone()
        finally:
            con.close()
        key = str((json.loads(row[0]) if row else {}).get("api_key") or "").strip()
    except (sqlite3.Error, ValueError, TypeError) as exc:
        print(f"FAIL: 读设置库失败（{exc}）；可改用 MINIMAX_API_KEY env", file=sys.stderr)
        sys.exit(2)
    if not key:
        print("FAIL: 设置库 tts_json.api_key 为空（设置 → TTS 语音合成 填 Key）", file=sys.stderr)
        sys.exit(2)
    return key, "settings DB tts.api_key"


# ---- 目录解析（TS 文件唯一数据源；语法契约见文件头注释） ----
_GROUP_HEAD_RE = re.compile(r"^const ([A-Z][A-Z0-9_]*): Array<\[string, string\]> = \[\s*$")
_ROW_RE = re.compile(r'^\s*\["([^"]+)",\s*"([^"]+)"\],\s*$')
_EXPORT_RE = re.compile(
    r"\.\.\.([A-Z][A-Z0-9_]*)\.map\(\(\[id, label\]\) => \(\{ id, label, lang: \"([a-z]+)\" as const \}\)\),"
)


def parse_catalog(path: Path = CATALOG_TS) -> list[dict]:
    groups: dict[str, list[tuple[str, str]]] = {}
    export_langs: dict[str, str] = {}
    order: list[str] = []
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        head = _GROUP_HEAD_RE.match(line)
        if head:
            current = head.group(1)
            groups[current] = []
            continue
        if current is not None:
            if line.strip() == "];":
                current = None
                continue
            row = _ROW_RE.match(line)
            if row:
                groups[current].append((row.group(1), row.group(2)))
        export = _EXPORT_RE.search(line)
        if export:
            name, lang = export.group(1), export.group(2)
            if name not in groups:
                print(f"FAIL: 导出块引用了未解析的组 {name}（TS 语法变了？）", file=sys.stderr)
                sys.exit(2)
            export_langs[name] = lang
            order.append(name)

    entries: list[dict] = []
    seen: set[str] = set()
    for name in order:
        for voice_id, label in groups.get(name, []):
            if voice_id in seen:
                continue
            seen.add(voice_id)
            entries.append({"id": voice_id, "label": label, "lang": export_langs[name]})
    if not entries:
        print(f"FAIL: 从 {path} 解析出 0 只音色（数组语法改过？本脚本与 TS 有同步契约）", file=sys.stderr)
        sys.exit(2)
    return entries


def safe_name(voice_id: str) -> str:
    """voice_id → 文件名安全化（保留 [A-Za-z0-9._-]，其余折叠为 _）。"""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", voice_id).strip("_") or "voice"


def pick_voices(entries: list[dict], voice: str, lang: str) -> list[dict]:
    out = entries
    if lang:
        out = [e for e in out if e["lang"] == lang]
    if voice:
        needle = voice.strip().lower()
        exact = [e for e in out if e["id"].lower() == needle]
        if exact:
            out = exact
        else:
            prefixed = [e for e in out if e["id"].lower().startswith(needle)]
            out = prefixed or [e for e in out if needle in e["id"].lower()]
        if not out:
            print(f"FAIL: --voice {voice!r} 未匹配到目录条目（--dry-run 可看全量清单）", file=sys.stderr)
            sys.exit(2)
        if len(out) > 1:
            names = "\n  ".join(e["id"] for e in out)
            print(f"FAIL: --voice {voice!r} 匹配多只，请给更完整的 ID:\n  {names}", file=sys.stderr)
            sys.exit(2)
    if not out:
        print("FAIL: --lang/--voice 过滤后无可跑条目", file=sys.stderr)
        sys.exit(2)
    return out


def _ssl_context() -> ssl.SSLContext:
    """CA 走 certifi（仓库铁律）；certifi 缺失回落系统默认。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover - venv 恒有 certifi
        return ssl.create_default_context()


def synth_mp3(key: str, url: str, voice: str, text: str, speed: float) -> bytes:
    """t2a_v2 HTTP 非流式 → mp3 字节。返回可写盘音频；失败抛 RuntimeError。

    重试策略：传输层抖动/HTTP 429/1002(RPM 限频) 退避重试；
    2054(voice-not-exist)/401 等确定性错误立即失败，不空转烧时间。
    """
    body = {
        "model": MODEL,
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": voice, "speed": float(speed), "vol": 1, "pitch": 0},
        "audio_setting": {"sample_rate": SAMPLE_RATE, "format": "mp3", "channel": 1},
    }
    ctx = _ssl_context()
    last = ""
    for attempt in range(RETRIES):
        if not _url_ok(url):
            raise RuntimeError(f"SSRF 护栏拒绝非白名单目标: {url}")
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ctx) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                last = f"HTTP {exc.code}"
                time.sleep(3 * (attempt + 1))
                continue
            if exc.code == 401:
                raise RuntimeError("MiniMax 鉴权失败（key 过期/无效，检查设置页 TTS API Key）") from exc
            raise RuntimeError(f"MiniMax HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = f"transport {exc!r}"
            if attempt < RETRIES - 1:
                time.sleep(3 * (attempt + 1))
                continue
            break

        base_resp = payload.get("base_resp") or {}
        code = int(base_resp.get("status_code", -1))
        if code == 0:
            audio_hex = (payload.get("data") or {}).get("audio") or ""
            if not audio_hex:
                raise RuntimeError(f"MiniMax 返回空音频: {str(base_resp)[:200]}")
            try:
                data = bytes.fromhex(audio_hex)
            except ValueError as exc:
                raise RuntimeError(f"MiniMax 音频非 hex 编码: {str(payload.get('data'))[:120]}") from exc
            if len(data) < 512:
                raise RuntimeError(f"音频过短（{len(data)} 字节），疑非有效 mp3")
            if not (data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)):
                raise RuntimeError(f"音频头非 mp3 帧（前 4 字节 {data[:4].hex()}）")
            return data
        last = str(base_resp)
        if code == 2054:
            raise RuntimeError("MiniMax 2054 voice-not-exist（音色 ID 无效，勿入目录）")
        if code == 1002 and attempt < RETRIES - 1:  # RPM 限频
            time.sleep(10 * (attempt + 1))
            continue
        break
    raise RuntimeError(f"MiniMax base_resp={last}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MiniMax 官方试听缓存（目录全量 → assets/minimax-auditions/*.mp3）")
    ap.add_argument("--voice", default="", help="只跑单只音色（完整 ID，或唯一前缀/子串）")
    ap.add_argument("--lang", default="", choices=list(LANGS), help="只跑某语种")
    ap.add_argument("--force", action="store_true", help="已存在也重生成（默认幂等跳过）")
    ap.add_argument("--dry-run", action="store_true", help="只列将处理的音色，不调 API")
    args = ap.parse_args(argv)

    entries = parse_catalog()
    selected = pick_voices(entries, args.voice, args.lang)

    # 文件名碰撞守卫（安全化后两只音色同名=落盘互相覆盖）
    names: dict[str, str] = {}
    for e in selected:
        safe = safe_name(e["id"])
        if safe in names and names[safe] != e["id"]:
            print(f"FAIL: 文件名碰撞 {names[safe]} vs {e['id']} → {safe}.mp3", file=sys.stderr)
            return 2
        names[safe] = e["id"]

    if args.dry_run:
        for e in selected:
            print(f"{e['lang']:>9}  {e['id']}  ({e['label']})")
        print(f"[dry-run] {len(selected)} 只，目标目录 {OUT_DIR}")
        return 0

    url = endpoint()
    if not _url_ok(url):
        print(f"FAIL: 端点未过 SSRF 白名单: {url}（host 白名单 {'/'.join(sorted(ALLOWED_HOSTS))}）", file=sys.stderr)
        return 2
    key, key_src = load_api_key()
    print(f"[env] endpoint={urllib.parse.urlsplit(url).netloc}{T2A_PATH} key={key_src} 目录条目={len(entries)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok = skipped = failed = 0
    failures: list[str] = []
    for e in selected:
        path = OUT_DIR / f"{safe_name(e['id'])}.mp3"
        if path.exists() and not args.force:
            print(f"[skip] {e['id']} 已存在 {path.relative_to(ROOT)}")
            skipped += 1
            continue
        speed = 1.2 if e["lang"] in ("zh", "cantonese") else 1.0
        t0 = time.perf_counter()
        try:
            data = synth_mp3(key, url, e["id"], LANG_TEXT[e["lang"]], speed)
        except RuntimeError as exc:
            print(f"[fail] {e['id']} ({e['lang']}): {exc}", file=sys.stderr)
            failures.append(e["id"])
            failed += 1
            continue
        tmp = path.with_suffix(".mp3.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        dt = time.perf_counter() - t0
        print(f"[ok]   {e['id']} ({e['lang']}) -> {path.relative_to(ROOT)} ({len(data)} bytes, {dt:.1f}s)")
        ok += 1
        if e is not selected[-1]:
            time.sleep(PACE_S)

    print(f"[done] ok={ok} skip={skipped} fail={failed} 目录={OUT_DIR.relative_to(ROOT)}")
    if failures:
        print("[failures] " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
