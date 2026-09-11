"""离线批量预合成 TTS 本地缓存(bok.py tts-pregen 的执行体,2026-09-08;task-14b 按人设物化)。

用法(bok.py 负责带好 PYTHONPATH 与 SSL_CERT_FILE):
  python scripts/pregen_tts.py --greetings            # 无变量脚本线全量
  python scripts/pregen_tts.py --objects              # 逐对象渲染开场白/收线/心跳
  python scripts/pregen_tts.py --greetings --objects  # 一次跑齐
  python scripts/pregen_tts.py --fillers              # 垫话按人设音色物化(全部启用人设)
  python scripts/pregen_tts.py --fillers --persona X  # 只给一个人设补物化垫话
  python scripts/pregen_tts.py --qa --all-personas    # QA 罐头 × 全部启用人设
  任意组合 + --dry-run                                # 只打印 (persona,lang,voice,条数) 计划清单

语音解析与运行时同源:CP /api/settings(internal=true 拿明文 api_key)+
/api/personas → _assemble_minimax_voice_map(人设 reference_audio 三态经
_parse_voice_map,single 模式 collapse,全空回落默认音色)→ _resolve_voice_map
(MiniMaxTTS._resolve_voice 同款:lang 键 → zh 键回落)。
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
from agent_runtime.fillers import FILLER_ASSETS_DIR, load_manifest  # noqa: E402
from agent_runtime.flow import object_vars, render_template_text  # noqa: E402
from agent_runtime.providers.livekit_plugins import LanguageState, MiniMaxTTS  # noqa: E402
from agent_runtime.tts_cache import TtsAudioCache, default_cache_dir  # noqa: E402

# 物化 job=(persona, lang, text);persona=None=无对应人设(回落设置默认音色)。
Job = tuple[dict | None, str, str]
# 物化结果记录=(persona, lang, voice, "new"|"skip"|"fail"),逐 job 一条。
Record = tuple[dict | None, str, str, str]


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


def _persona_key(persona: dict | None) -> str:
    """打印/去重用人设标识(无人设=「-」)。"""
    return str((persona or {}).get("id") or "-")


def _resolve_voice_map(raw, lang: str) -> str:
    """音色三态解析(纯函数)——与运行时 MiniMaxTTS._resolve_voice 同款语义。

    dict / JSON 串 / 单音色 id 串:取 lang 键,空缺回落 zh 键(MiniMax/Qwen3
    两家插件同款回退);全缺=空串(调用方按「该语言无音色」跳过,运行时同样
    不查缓存直接走兜底)。抽成本地纯函数是为了 --dry-run 零 provider 构造。
    """
    if isinstance(raw, dict):
        return str(raw.get(lang) or raw.get("zh") or "")
    text = str(raw or "")
    if text.startswith("{"):
        try:
            mapping = json.loads(text)
        except Exception:
            return text
        if isinstance(mapping, dict):
            return str(mapping.get(lang) or mapping.get("zh") or "")
        return text
    return text


def _persona_resolved_voice(persona: dict | None, lang: str, tts_cfg: dict, voice_mode: str) -> str:
    """(人设, 语言) → 该语言锚定音色 id——与运行时合成时点完全同源。

    _assemble_minimax_voice_map(人设 reference_audio 三态/collapse/默认音色兜底)
    组 map 后按 _resolve_voice_map 取 lang 键(缺省回落 zh)。无 provider 构造,
    dry-run 可安全批量调用。
    """
    voice_map = _assemble_minimax_voice_map(
        persona=persona, tts_cfg=tts_cfg, greet_lang=lang, voice_mode=voice_mode
    )
    return _resolve_voice_map(voice_map, lang)


def _template_for(templates: list[dict], obj: dict) -> dict | None:
    tid = str(obj.get("template_id") or "")
    if tid:
        for tpl in templates:
            if str(tpl.get("id") or "") == tid:
                return tpl
    return templates[0] if templates else None


def _template_steps(tpl: dict | None) -> list[dict]:
    """CP 模板的步骤在 steps_json 字符串字段(列表/详情都不带解析过的 steps 数组)。"""
    if not tpl:
        return []
    raw = tpl.get("steps_json") or tpl.get("steps") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return raw if isinstance(raw, list) else []


def _opening_line(tpl: dict | None, obj: dict, lang: str) -> str:
    """话术第 1 步 ref 首行渲染(与 FlowController.opening_text 同逻辑):
    模板语言≠通话语言或变量缺失 → 空串(运行时会退通用语,预生成跳过)。"""
    if not tpl or str(tpl.get("language") or "") != lang:
        return ""
    steps = _template_steps(tpl)
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


def _fillers_jobs(
    persona_pool: list[dict], manifest: dict[str, list[dict]], tts_cfg: dict, voice_mode: str
) -> list[Job]:
    """垫话物化计划:每个启用人设取其语言对应池的整池句(39 句 manifest,每语言 13)。

    无该语言池 / 解析不出音色(如 per_language 模式下该人设缺此语言键)的人设
    响亮跳过——运行时该人设本来就查不到缓存(音色为空不查),物化也无意义。
    """
    jobs: list[Job] = []
    for persona in persona_pool:
        lang = _normalize_lang((persona or {}).get("language"), default="zh") or "zh"
        entries = manifest.get(lang) or []
        voice = _persona_resolved_voice(persona, lang, tts_cfg, voice_mode)
        if not entries or not voice:
            print(
                f"fillers: persona={_persona_key(persona)} lang={lang} "
                f"voice={voice or '-'} skipped (no pool or no voice)",
                flush=True,
            )
            continue
        for e in entries:
            # text 原样透传(含 MiniMax <#x#> 停顿标记)——运行时 lookup/backfill
            # 都用 manifest 原文,key 必须同文。
            jobs.append((persona, lang, str(e.get("text") or "")))
    return jobs


def _qa_jobs(
    qa_rows: list[dict],
    persona_pool: list[dict],
    lang_personas: dict[str, dict | None],
    *,
    all_personas: bool,
    tts_cfg: dict,
    voice_mode: str,
) -> list[Job]:
    """Q→A 快路启用条目的物化计划。

    默认:每条目按条目语言取该语言一个人设(旧行为)。--all-personas:条目 ×
    每个启用人设都物化一版(新人设上线/换人设即全量命中,task-14b);解析不出
    该条目语言音色的人设跳过(运行时音色为空同样不查缓存)。
    """
    jobs: list[Job] = []
    for e in qa_rows:
        if not bool(e.get("enabled", True)):
            continue
        text = str(e.get("answer_text") or "").strip()
        lang = _normalize_lang((e or {}).get("lang"), default="zh") or "zh"
        if not text:
            continue
        if all_personas:
            for persona in persona_pool:
                if not _persona_resolved_voice(persona, lang, tts_cfg, voice_mode):
                    continue
                jobs.append((persona, lang, text))
        else:
            jobs.append((lang_personas.get(lang), lang, text))
    return jobs


async def _materialize(
    cache: TtsAudioCache,
    model: str,
    jobs: list[Job],
    *,
    api_key: str,
    sample_rate: int,
    tts_cfg: dict,
    voice_mode: str,
    dry_run: bool = False,
) -> tuple[int, int, int, list[Record]]:
    """合成+落盘主循环(greetings/objects/fillers/qa 四条物化线共用)。

    voice 按 (persona, lang) 运行时同源解析(map 组装带备忘,同人设同语言只算
    一次);(persona, lang, text) 三元组去重;已在缓存(同 text+voice+model)的
    条目 skip 不重合成。dry_run=True 只按同口径分类计数,绝不碰云 API。

    返回 (generated, skipped, failed, records)。
    """
    seen: set[tuple[str, str, str]] = set()
    uniq: list[Job] = []
    for persona, lang, text in jobs:
        text = str(text or "").strip()
        if not text:
            continue
        k = (_persona_key(persona), lang, text)
        if k in seen:
            continue
        seen.add(k)
        uniq.append((persona, lang, text))

    map_cache: dict[tuple[str, str], dict] = {}
    ok = skip = fail = 0
    records: list[Record] = []
    for persona, lang, text in uniq:
        pk = (_persona_key(persona), lang)
        voice_map = map_cache.get(pk)
        if voice_map is None:
            voice_map = _assemble_minimax_voice_map(
                persona=persona, tts_cfg=tts_cfg, greet_lang=lang, voice_mode=voice_mode
            )
            map_cache[pk] = voice_map
        voice = _resolve_voice_map(voice_map, lang)
        if not voice:
            fail += 1
            records.append((persona, lang, "", "fail"))
            print(f"NO_VOICE persona={pk[0]} lang={lang} — 跳过", flush=True)
            continue
        key = cache.key_for(text, voice=voice, model=model)
        if cache.get(key) is not None:
            skip += 1
            records.append((persona, lang, voice, "skip"))
            continue
        if dry_run:
            ok += 1
            records.append((persona, lang, voice, "new"))
            continue
        provider = _provider_for(lang, voice_map, api_key, sample_rate)
        try:
            pcm = await _synth(provider, text)
        except Exception as exc:  # noqa: BLE001 - 单条失败唔阻整体
            fail += 1
            records.append((persona, lang, voice, "fail"))
            print(f"FAIL lang={lang} chars={len(text)} err={exc!r}", flush=True)
            continue
        stored = cache.store(key, pcm, text=text, voice=voice, model=model)
        if stored:
            ok += 1
            records.append((persona, lang, voice, "new"))
            print(f"OK lang={lang} chars={len(text)} bytes={len(pcm)} key={key[:10]}", flush=True)
        else:
            fail += 1
            records.append((persona, lang, voice, "fail"))
            print(f"STORE_FAIL lang={lang} key={key[:10]}", flush=True)
    return ok, skip, fail, records


def _print_group_counts(prefix: str, records: list[Record], *, with_total: bool = False) -> None:
    """records 按 (persona, lang, voice) 聚合打印计数线。

    fillers/qa 按人设物化的统一口径:fillers: persona=X lang=Y voice=Z new=N skip=M
    (dry-run 附 total=条数,即计划清单)。
    """
    groups: dict[tuple[str, str, str], list[str]] = {}
    order: list[tuple[str, str, str]] = []
    for persona, lang, voice, outcome in records:
        gk = (_persona_key(persona), lang, voice)
        if gk not in groups:
            groups[gk] = []
            order.append(gk)
        groups[gk].append(outcome)
    for pid, lang, voice in order:
        outs = groups[(pid, lang, voice)]
        total = f" total={len(outs)}" if with_total else ""
        print(
            f"{prefix}: persona={pid} lang={lang} voice={voice}{total} "
            f"new={outs.count('new')} skip={outs.count('skip')}",
            flush=True,
        )


async def main_async() -> int:
    ap = argparse.ArgumentParser(description="TTS 本地缓存离线预合成")
    ap.add_argument("--greetings", action="store_true", help="无变量脚本线全量(兜底问候/心跳/收线/WA)")
    ap.add_argument("--objects", action="store_true", help="逐对象渲染开场白/收线/心跳并预合成")
    ap.add_argument("--fillers", action="store_true", help="垫话 manifest 按人设音色物化进 tts-cache(默认全部启用人设;--persona 限单人人设)")
    ap.add_argument("--qa", action="store_true", help="Q→A 快路启用条目的应答预合成(闸门只认缓存有音频的条目)")
    ap.add_argument("--all-personas", action="store_true", help="--qa 配套:条目 × 每个启用人设各物化一版(缺省每语言只取该语言人设)")
    ap.add_argument("--dry-run", action="store_true", help="只打印将物化的 (persona,lang,voice,条数) 计划清单,不合成")
    ap.add_argument("--cp", default=os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--persona", default="", help="人设范围:指定 persona id(fillers/qa-all 只物化该人设;缺省按语言取该语言的 persona/全部人设)")
    ap.add_argument("--object-id", default="", help="只为指定对象预生成开场白/收线/心跳(配合 --objects)")
    ap.add_argument("--model", default="", help="MINIMAX_MODEL 覆盖(默认 env/2.8-hd,须与运行时一致)")
    args = ap.parse_args()
    if not (args.greetings or args.objects or args.fillers or args.qa):
        args.greetings = True

    if os.environ.get("MINIMAX_API_KEY", ""):
        api_key = os.environ["MINIMAX_API_KEY"]
    else:
        api_key = ""
    token = os.environ.get("BOK_CP_TOKEN", "")
    settings, personas, objects, templates = _fetch_cp(args.cp, token)
    tts_cfg = settings.get("tts") or {}
    api_key = api_key or str(tts_cfg.get("api_key") or "")
    if not api_key and not args.dry_run:
        print("MINIMAX_API_KEY missing (settings tts.api_key/env) — cannot synthesize", flush=True)
        return 1
    if args.model:
        os.environ["MINIMAX_MODEL"] = args.model
    sample_rate = int(tts_cfg.get("sample_rate") or 24000)
    # 语言→该语言的 persona(运行时按语言用人设,音色随人设;每语言一套罐头,
    # 音色与运行时同源)。--persona 显式覆盖时全部语言用同一 persona。
    lang_personas: dict[str, dict | None] = {}
    for lang in ("zh", "cantonese", "en"):
        if args.persona:
            lang_personas[lang] = next((x for x in personas if str(x.get("id")) == args.persona), None)
        else:
            lang_personas[lang] = next(
                (x for x in personas if _normalize_lang(x.get("language"), default="") == lang), None
            )
    voice_mode = _resolve_tts_voice_mode(tts_cfg)
    # 按人设物化的人设池(fillers / qa --all-personas 共用):--persona 限单人人设,
    # 缺省=CP 全部人设(人设无 enabled 字段,在册即启用;无音色/无池者在计划期跳过)。
    if args.persona:
        persona_pool = [x for x in personas if str(x.get("id")) == args.persona]
        if not persona_pool:
            print(f"persona {args.persona} not found in CP /api/personas", flush=True)
    else:
        persona_pool = list(personas)

    cache = TtsAudioCache(root=default_cache_dir(), sample_rate=sample_rate)
    model = os.environ.get("MINIMAX_MODEL", "speech-2.8-hd")
    # 缓存目录须与运行时同根:BOK_TTS_CACHE_DIR 由 bok.py/调用方透传。
    print(f"cache dir={cache.root} model={model} sample_rate={sample_rate}", flush=True)

    total = gen = skip = fail = 0

    # ---- 无变量脚本线 + 逐对象线(每语言 lang_personas 人设) ----
    legacy_jobs: list[Job] = []
    if args.greetings:
        for lang, text in GENERIC_GREETINGS.items():
            legacy_jobs.append((lang_personas.get(lang), lang, text))
        for lang in ("zh", "cantonese", "en"):
            for i in range(3):
                legacy_jobs.append((lang_personas.get(lang), lang, _nudge_line("", lang, i)))
            legacy_jobs.append((lang_personas.get(lang), lang, _farewell_line("", lang)))
            legacy_jobs.append((lang_personas.get(lang), lang, _wa_number_line(lang, "")))

    if args.objects:
        only_id = str(args.object_id or "").strip()
        for obj in objects:
            if only_id and str(obj.get("id") or "") != only_id:
                continue
            lang = _normalize_lang((obj or {}).get("language"), default="zh") or "zh"
            name = str((obj or {}).get("display_name") or "").strip()
            tpl = _template_for(templates, obj)
            opening = _opening_line(tpl, obj, lang)
            if opening:
                legacy_jobs.append((lang_personas.get(lang), lang, opening))
            if name:
                legacy_jobs.append((lang_personas.get(lang), lang, _farewell_line(name, lang)))
                for i in range(3):
                    legacy_jobs.append((lang_personas.get(lang), lang, _nudge_line(name, lang, i)))

    if legacy_jobs:
        ok, sk, fl, records = await _materialize(
            cache, model, legacy_jobs,
            api_key=api_key, sample_rate=sample_rate, tts_cfg=tts_cfg,
            voice_mode=voice_mode, dry_run=args.dry_run,
        )
        gen += ok
        skip += sk
        fail += fl
        total += ok + sk + fl
        if args.dry_run:
            _print_group_counts("plan", records, with_total=True)

    # ---- 垫话按人设物化(task-14b 复活) ----
    # 运行时 FillerDirector 双层选源:先查 (text, voice, model) 人设物化版
    # (命中=与通话完全同人声),miss 播源码资产兜底+后台补物化。这里即批量
    # 补物化入口:新人设上线跑一次 --fillers,垫话即全程人设音色。
    if args.fillers:
        try:
            manifest = load_manifest(FILLER_ASSETS_DIR)
        except Exception as exc:  # noqa: BLE001 - 资产缺失=垫话物化停用(响亮降级)
            print(f"fillers manifest unavailable dir={FILLER_ASSETS_DIR} err={exc!r}", flush=True)
            manifest = {}
        fillers_jobs = _fillers_jobs(persona_pool, manifest, tts_cfg, voice_mode)
        if fillers_jobs:
            ok, sk, fl, records = await _materialize(
                cache, model, fillers_jobs,
                api_key=api_key, sample_rate=sample_rate, tts_cfg=tts_cfg,
                voice_mode=voice_mode, dry_run=args.dry_run,
            )
            gen += ok
            skip += sk
            fail += fl
            total += ok + sk + fl
            _print_group_counts("fillers", records, with_total=args.dry_run)

    # ---- Q→A 快路条目物化 ----
    if args.qa:
        # Q→A 快路启用条目:应答文本按条目语言取对应 persona 音色物化——运行时
        # 闸门按 (answer_text, resolved_voice, model) 查缓存,音色必须同源。
        # 空表/拉取失败静默(库未建=无物化需求)。
        try:
            qa_rows = _cp_get(args.cp, "/api/qa-entries?enabled=1", token) or []
        except Exception as exc:  # noqa: BLE001 - 库未建/CP 不可达唔阻其他预合成
            print(f"qa entries fetch failed: {exc!r}", flush=True)
            qa_rows = []
        qa_job_list = _qa_jobs(
            qa_rows, persona_pool, lang_personas,
            all_personas=args.all_personas, tts_cfg=tts_cfg, voice_mode=voice_mode,
        )
        if qa_job_list:
            ok, sk, fl, records = await _materialize(
                cache, model, qa_job_list,
                api_key=api_key, sample_rate=sample_rate, tts_cfg=tts_cfg,
                voice_mode=voice_mode, dry_run=args.dry_run,
            )
            gen += ok
            skip += sk
            fail += fl
            total += ok + sk + fl
            if args.all_personas:
                _print_group_counts("qa", records, with_total=args.dry_run)

    print(f"pregen done total={total} generated={gen} skipped={skip} failed={fail}", flush=True)
    return 0 if fail == 0 else 2


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
