"""离线批量预合成 TTS 本地缓存(bok.py tts-pregen 的执行体,2026-09-08)。

用法(bok.py 负责带好 PYTHONPATH 与 SSL_CERT_FILE):
  python scripts/pregen_tts.py --greetings            # 无变量脚本线全量
  python scripts/pregen_tts.py --objects              # 逐对象渲染开场白/收线/心跳
  python scripts/pregen_tts.py --greetings --objects  # 一次跑齐

语音解析与运行时同源:CP /api/settings(internal=true 拿明文 api_key)+
/api/personas → _assemble_minimax_voice_map → MiniMaxTTS.resolved_voice()。
key 不匹配只会 miss(慢但不错),绝不播错音频。已存在的 key 自动跳过——
音色/模型档变更后旧条目自然失效,重跑即重建。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in ("apps/agent", "packages/core"):
    _path = str(ROOT / _p)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from agent_runtime.agent import (  # noqa: E402
    GENERIC_GREETINGS,
    _assemble_minimax_voice_map,
    _farewell_line,
    _normalize_lang,
    _nudge_line,
    _resolve_tts_voice_mode,
    _wa_number_line,
)
from agent_runtime.flow import object_vars, render_template_text  # noqa: E402
from agent_runtime.providers.livekit_plugins import LanguageState, MiniMaxTTS  # noqa: E402
from agent_runtime.tts_cache import TtsAudioCache, default_cache_dir  # noqa: E402


def _cp_get(base: str, path: str, token: str) -> object:
    url = f"{base.rstrip('/')}{path}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_cp(base: str, token: str) -> tuple[dict, list, list, list]:
    settings = _cp_get(base, "/api/settings?internal=true", token)
    if not isinstance(settings, dict):
        settings = {}
    try:
        personas = _cp_get(base, "/api/personas", token) or []
    except Exception:
        personas = []
    try:
        objects = _cp_get(base, "/api/objects", token) or []
    except Exception:
        objects = []
    try:
        templates = _cp_get(base, "/api/templates", token) or []
    except Exception:
        templates = []
    return settings, list(personas), list(objects), list(templates)


async def _synth(provider: MiniMaxTTS, text: str) -> bytes:
    buf = bytearray()
    async with provider.synthesize(text) as stream:
        async for ev in stream:
            data = ev.frame.data
            buf.extend(data.tobytes() if isinstance(data, memoryview) else bytes(data))
    if getattr(stream, "_emitted_beep", False):
        raise RuntimeError("synth failed (beep emitted)")
    return bytes(buf)


def _provider_for(lang: str, voice_map: dict, api_key: str, sample_rate: int) -> MiniMaxTTS:
    return MiniMaxTTS(
        voice=voice_map,
        language_state=LanguageState(lang=lang),
        sample_rate=sample_rate,
        api_key=api_key,
    )


def _template_for(templates: list[dict], obj: dict) -> dict | None:
    tid = str(obj.get("template_id") or "")
    if tid:
        for tpl in templates:
            if str(tpl.get("id") or "") == tid:
                return tpl
    return templates[0] if templates else None


def _opening_line(tpl: dict | None, obj: dict, lang: str) -> str:
    """话术第 1 步 ref 首行渲染(与 FlowController.opening_text 同逻辑):
    模板语言≠通话语言或变量缺失 → 空串(运行时会退通用语,预生成跳过)。"""
    if not tpl or str(tpl.get("language") or "") != lang:
        return ""
    steps = tpl.get("steps") or []
    if not steps:
        return ""
    first = steps[0] or {}
    raw = str(first.get("ref") or first.get("goal") or "")
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        rendered = render_template_text(line, object_vars(obj))
        if "{" in rendered and "}" in rendered:
            return ""  # 仍有未填占位
        return rendered
    return ""


async def main_async() -> int:
    ap = argparse.ArgumentParser(description="TTS 本地缓存离线预合成")
    ap.add_argument("--greetings", action="store_true", help="无变量脚本线全量(兜底问候/心跳/收线/WA)")
    ap.add_argument("--objects", action="store_true", help="逐对象渲染开场白/收线/心跳并预合成")
    ap.add_argument("--fillers", action="store_true", help="垫话短语库预合成(PR-2 垫话用,绝不运行时合成)")
    ap.add_argument("--cp", default=os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--model", default="", help="MINIMAX_MODEL 覆盖(默认 env/2.8-hd,须与运行时一致)")
    args = ap.parse_args()
    if not (args.greetings or args.objects or args.fillers):
        args.greetings = True

    if os.environ.get("MINIMAX_API_KEY", ""):
        api_key = os.environ["MINIMAX_API_KEY"]
    else:
        api_key = ""
    token = os.environ.get("BOK_CP_TOKEN", "")
    settings, personas, objects, templates = _fetch_cp(args.cp, token)
    tts_cfg = settings.get("tts") or {}
    api_key = api_key or str(tts_cfg.get("api_key") or "")
    if not api_key:
        print("MINIMAX_API_KEY missing (settings tts.api_key/env) — cannot synthesize", flush=True)
        return 1
    if args.model:
        os.environ["MINIMAX_MODEL"] = args.model
    sample_rate = int(tts_cfg.get("sample_rate") or 24000)
    persona = personas[0] if personas else None
    voice_mode = _resolve_tts_voice_mode(tts_cfg)

    cache = TtsAudioCache(root=default_cache_dir(), sample_rate=sample_rate)
    model = os.environ.get("MINIMAX_MODEL", "speech-2.8-hd")
    # 缓存目录须与运行时同根:BOK_TTS_CACHE_DIR 由 bok.py/调用方透传。
    print(f"cache dir={cache.root} model={model} sample_rate={sample_rate}", flush=True)

    # (lang, text) 去重集合
    jobs: list[tuple[str, str]] = []

    if args.greetings:
        for lang, text in GENERIC_GREETINGS.items():
            jobs.append((lang, text))
        for lang in ("zh", "cantonese", "en"):
            for i in range(3):
                jobs.append((lang, _nudge_line("", lang, i)))
            jobs.append((lang, _farewell_line("", lang)))
            jobs.append((lang, _wa_number_line(lang, "")))

    if args.objects:
        for obj in objects:
            lang = _normalize_lang((obj or {}).get("language"), default="zh") or "zh"
            name = str((obj or {}).get("display_name") or "").strip()
            tpl = _template_for(templates, obj)
            opening = _opening_line(tpl, obj, lang)
            if opening:
                jobs.append((lang, opening))
            if name:
                jobs.append((lang, _farewell_line(name, lang)))
                for i in range(3):
                    jobs.append((lang, _nudge_line(name, lang, i)))

    if args.fillers:
        from agent_runtime.fillers import filler_lines

        for lang, lines in filler_lines().items():
            for line in lines:
                jobs.append((lang, line))

    # 去重(同文本同语言只合成一次;key 已含 voice,不同 lang 同 voice 也会分开算)
    seen: set[tuple[str, str]] = set()
    uniq: list[tuple[str, str]] = []
    for lang, text in jobs:
        text = str(text or "").strip()
        if not text or (lang, text) in seen:
            continue
        seen.add((lang, text))
        uniq.append((lang, text))

    ok = skip = fail = 0
    for lang, text in uniq:
        provider = _provider_for(lang, _assemble_minimax_voice_map(
            persona=persona, tts_cfg=tts_cfg, greet_lang=lang, voice_mode=voice_mode
        ), api_key, sample_rate)
        voice = provider.resolved_voice()
        key = cache.key_for(text, voice=voice, model=model)
        if cache.get(key) is not None:
            skip += 1
            continue
        try:
            pcm = await _synth(provider, text)
        except Exception as exc:  # noqa: BLE001 - 单条失败唔阻整体
            fail += 1
            print(f"FAIL lang={lang} chars={len(text)} err={exc!r}", flush=True)
            continue
        stored = cache.store(key, pcm, text=text, voice=voice, model=model)
        if stored:
            ok += 1
            print(f"OK lang={lang} chars={len(text)} bytes={len(pcm)} key={key[:10]}", flush=True)
        else:
            fail += 1
            print(f"STORE_FAIL lang={lang} key={key[:10]}", flush=True)

    print(f"pregen done total={len(uniq)} generated={ok} skipped={skip} failed={fail}", flush=True)
    return 0 if fail == 0 else 2


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
