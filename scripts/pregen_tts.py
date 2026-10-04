"""离线批量预合成 TTS 本地缓存(bok.py tts-pregen 的执行体,2026-09-08;task-14b 按人设物化)。

用法(bok.py 负责带好 PYTHONPATH 与 SSL_CERT_FILE):
  python scripts/pregen_tts.py --greetings            # 无变量脚本线全量
  python scripts/pregen_tts.py --objects              # 逐对象渲染开场白/收线/心跳
  python scripts/pregen_tts.py --greetings --objects  # 一次跑齐
  python scripts/pregen_tts.py --greetings --object X # 直念步按对象 X 的变量渲染(缺省=账号最近更新对象)
  python scripts/pregen_tts.py --fillers              # 垫话按人设音色物化(全部启用人设)
  python scripts/pregen_tts.py --fillers --persona X  # 只给一个人设补物化垫话
  python scripts/pregen_tts.py --qa --all-personas    # QA 罐头 × 全部启用人设
  python scripts/pregen_tts.py --branches             # 话术分支应答物化(步骤 ref「如果客户X→就Y」)
  python scripts/pregen_tts.py --branches --texts-file t.txt  # 只物化指定分支应答(CP 按条补录;一行一条,resp 原文含动作标记)
  python scripts/pregen_tts.py --qa-status            # 逐条目物化状态 JSON(stdout,不合成不碰云)
  python scripts/pregen_tts.py --branch-status        # 逐分支应答物化状态 JSON(stdout,不合成;键=resp 原文含动作标记,值 ok/missing/ph)
  python scripts/pregen_tts.py --qa --entry-id X      # 只物化指定 qa 条目 id(--entry-id 可重复)
  任意组合 + --dry-run                                # 只打印 (persona,lang,voice,条数) 计划清单

语音解析与运行时同源:CP /api/settings(internal=true 拿明文 api_key)+
/api/personas → _assemble_minimax_voice_map(人设 reference_audio 三态经
_parse_voice_map,single 模式 collapse,全空回落默认音色)→ _resolve_voice_map
(MiniMaxTTS._resolve_voice 同款:lang 键 → zh 键回落)。
key 不匹配只会 miss(慢但不错),绝不播错音频。已存在的 key 自动跳过——
音色/模型档变更后旧条目自然失效,重跑即重建。
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
import asyncio
import contextlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

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
    canned_cache_supported,
    effective_tts_provider,
)
from agent_runtime.fillers import FILLER_ASSETS_DIR, load_manifest  # noqa: E402
from agent_runtime.flow import (  # noqa: E402
    object_vars,
    parse_branch_action,
    parse_step_ref,
    parse_steps,
    render_template_text,
)
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    LanguageState,
    MiniMaxTTS,
    minimax_speed_for,
)
from agent_runtime.tts_cache import TtsAudioCache, default_cache_dir  # noqa: E402

# 物化 job=(persona, lang, text);persona=None=无对应人设(回落设置默认音色)。
Job = tuple[dict | None, str, str, str]  # (persona, lang, text, emotion)
# 物化结果记录=(persona, lang, voice, "new"|"skip"|"fail"),逐 job 一条。
Record = tuple[dict | None, str, str, str]


# 瞬断重试间隔(秒):CP 重启/uvicorn 瞬时拒连曾令保存点自动物化 4 连崩
# (RemoteDisconnected,2026-09-11 实证)——物化窗口常跨 CP 生命周期,必须扛抖。
_CP_RETRY_DELAYS = (1.0, 3.0)


def _cp_get(base: str, path: str, token: str, *, opener=None, channel: bool = False) -> object:
    url = f"{base.rstrip('/')}{path}"
    opener = opener or urllib.request.urlopen
    last_exc: Exception | None = None
    attempts = 1 + len(_CP_RETRY_DELAYS)
    for i in range(attempts):
        if i:
            time.sleep(_CP_RETRY_DELAYS[i - 1])
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if channel:
            # 机器通道自报(与运行时 agent ControlPlaneClient 同款头):
            # GET /api/templates/{id} 据此吃 published_json 冻结 overlay
            # (auth-off 单机形态;auth-on 下凭 token 已判机器通道,头无害)。
            req.add_header("X-Bok-Channel", "agent")
        try:
            with opener(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            # http.client.RemoteDisconnected 是 ConnectionError 子类;HTTPError 是
            # URLError 子类但代表服务端明确答复(4xx/5xx),重试无益——放行抛出。
            last_exc = exc
            continue
    raise last_exc  # type: ignore[misc]


def _template_detail_rows(base: str, token: str, listed: list) -> list:
    """列表行 → 逐条详情行(冻结 overlay),修「分文物化吃 live 草稿」漂移。

    列表端点 GET /api/templates **不**应用 published_json overlay(见 main.py
    ``_template_machine_overlay`` docstring「列表端点不 overlay」),而运行时 agent
    装配走详情端点 GET /api/templates/{id} 的机器通道 overlay。分支/直念步物化若
    读列表行的 steps_json,就会用**编辑中的 live 草稿**算缓存键,与运行时冻结版
    错位——物化出的音频运行时查不到(永远 miss)。故列表只用于发现 id,steps_json
    一律取详情冻结版;单条详情拉取失败退列表行(不阻物化,旧行为)。
    """
    out: list = []
    for tpl in listed or []:
        tid = str((tpl or {}).get("id") or "")
        if not tid:
            out.append(tpl)
            continue
        try:
            detail = _cp_get(base, f"/api/templates/{quote(tid)}", token, channel=True)
        except Exception:  # noqa: BLE001 - 详情不可读退列表行(不阻物化;旧行为)
            detail = None
        out.append(detail if isinstance(detail, dict) and detail else tpl)
    return out


def _fetch_cp(base: str, token: str, *, account_id: str = "") -> tuple[dict, list, list, list]:
    settings = _cp_get(base, "/api/settings?internal=true", token)
    if not isinstance(settings, dict):
        settings = {}
    try:
        # F1(2026-09-20):account_id 显式置空=拉全部账号人设(CP 端点缺省 acc-001
        # 会滤掉其它账号的人设)。--persona 的自动物化随人设保存触发,人设可能任
        # 意账号;按人设物化/查找必须跨账号,单账号部署零变化(键去重天然幂等)。
        personas = _cp_get(base, "/api/personas?account_id=", token) or []
    except Exception:
        personas = []
    try:
        # 2026-09-27:account_id 给定时同步收窄对象列表——直念步变量渲染的缺省
        # 对象要求是「该账号最近更新的对象」,不按账号会误取 acc-001(CP 端点缺省)。
        obj_path = "/api/objects"
        if account_id:
            obj_path = f"/api/objects?account_id={quote(account_id)}"
        objects = _cp_get(base, obj_path, token) or []
    except Exception:
        objects = []
    try:
        # 列表端点只用于发现模板 id;steps_json 逐条取详情端点(冻结 overlay)——
        # 见 _template_detail_rows(2026-09-27 修「物化吃 live 草稿」漂移)。
        tpl_path = "/api/templates"
        if account_id:
            tpl_path = f"/api/templates?account_id={quote(account_id)}"
        templates = _template_detail_rows(base, token, _cp_get(base, tpl_path, token) or [])
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


def _say_step_lines(tpl: dict | None, obj: dict | None = None) -> list[tuple[str, str]]:
    """直念步(say=1)ref 首行(2026-09-12 开场白三段拆分):通知/道歉类文本,
    agent 走 _say_script 脚本线——与本脚本同一条缓存线物化后即点即播。
    与 FlowController.step_say_text 同首行规则(同 parse_steps/同 `ref or goal`/
    同 render_template_text/同 residual-`{}` 跳过)。返回 (text, emotion):
    emotion=步级行级情绪(模板 steps_json `emotion` 字段,2026-09-16 罐头带
    情绪——物化时经 MINIMAX_EMOTION 烧进音频;空=不下发自动匹配)。

    obj=变量渲染所用对象卡(2026-09-27):必须与运行时 **同一个对象** 才等价——
    运行时 FlowController.vars_map=object_vars(object_card)(flow.py:1183),这里
    走同一个 object_vars()+render_template_text(),故对同一 obj 产出的文本逐字节
    等于 step_say_text(),缓存键(文本+音色+模型)才对得上。obj=None=空变量(旧
    行为:含 {占位} 的行渲染后仍有残留 → 跳过,永不物化)。"""
    if not tpl:
        return []
    vars_map = object_vars(obj) if obj else {}
    out: list[tuple[str, str]] = []
    for s in parse_steps(str(tpl.get("steps_json") or "")):
        if not s.say:
            continue
        rendered = render_template_text(s.ref or s.goal, vars_map)
        for line in rendered.splitlines():
            line = line.strip()
            if line and not re.search(r"\{[^{}]+\}", line):
                out.append((line, s.emotion))
                break
    return out


def _resolve_say_object(objects: list[dict], *, object_id: str) -> dict | None:
    """直念步变量渲染所用对象(2026-09-27)。

    - 显式 `--object <id>` 命中即用;显式指定但不在列表=None(**绝不静默换对象**
      ——换对象会算出错误的缓存键,宁退回空变量旧行为)。
    - 未给:该账号对象列表末条。CP /api/objects 无 updated_at 列,返回序≈插入/
      更新序,末条作「最近更新对象」代理;空表=None(空变量旧行为)。
    """
    oid = str(object_id or "").strip()
    if oid:
        return next((o for o in objects if str((o or {}).get("id") or "") == oid), None)
    return objects[-1] if objects else None


def _strip_branch_action(resp: str) -> str:
    """剥分支应答首部的动作标记(纯函数);无标记原样返回。

    单源复用官方解析器 flow.parse_branch_action(2026-09-20 A-② 已落定):
    (action, step, text) 的 text 即剥标记后的应答文本——与运行时出声文本
    逐字节同源(含【 收线 】内空白容错/【跳第0步】退默认的边缘语义),缓存
    键不会与运行时错位。标记语法只在 flow.py 一处定义。
    """
    return parse_branch_action(str(resp or ""))[2]


def _load_texts_file(path: str) -> set[str] | None:
    """--texts-file 读入(一行一条,空白行忽略);未给=None=不过滤。"""
    p = str(path or "").strip()
    if not p:
        return None
    lines = Path(p).read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip()}


def _branch_jobs(
    templates: list[dict],
    lang_personas: dict[str, dict | None],
    *,
    texts: set[str] | None = None,
) -> list[Job]:
    """话术分支应答物化计划(2026-09-20 路线 A-① 分支罐头快路配套)。

    逐模板逐步 parse_step_ref 解析 ref 的「如果客户X→就Y」分支(与运行时
    agent.branch_canned_pick 同一解析器),取每个分支 resp 剥动作标记后渲染文本。
    渲染后仍有 {占位} 残留的条目跳过——运行时分支快路同规则不认(查不到
    同文缓存,宁落 LLM 不念占位符)。texts 给定时只做 raw resp(含标记,逐字节)
    命中的分支(CP branch-pregen 按条补录通道)。同 (persona, lang, text) 重复
    resp 由 _materialize 的 seen 去重,不重复合成。分支快路闸门只认缓存有音频的
    应答——这里物化即开闸,键与 _say_script 同源(text+voice+model+speed,
    emotion 空)。
    """
    jobs: list[Job] = []
    for tpl in templates or []:
        lang = _normalize_lang((tpl or {}).get("language"), default="") or ""
        if not lang:
            continue
        for s in parse_steps(str(tpl.get("steps_json") or "")):
            for _cond, resp in parse_step_ref(s.ref or "").branches:
                raw = str(resp or "").strip()
                if not raw or (texts is not None and raw not in texts):
                    continue
                rendered = render_template_text(_strip_branch_action(raw), {})
                if not rendered.strip() or re.search(r"\{[^{}]+\}", rendered):
                    continue
                jobs.append((lang_personas.get(lang), lang, rendered, ""))
    return jobs


def _fillers_jobs(
    persona_pool: list[dict],
    manifest: dict[str, list[dict]],
    tts_cfg: dict,
    voice_mode: str,
    canned: dict[str, list[str]] | None = None,
) -> list[Job]:
    """垫话物化计划:每个启用人设取其语言对应池的整池句(39 句 manifest,每语言 13)
    + 垫话罐头库该语言的条目文本(2026-09-13 乙节:filler_entries 确定性命中层,
    同 (text, voice) 键去重——罐头文本与 manifest 文本撞车时只物化一次)。

    无该语言池 / 解析不出音色(如 per_language 模式下该人设缺此语言键)的人设
    响亮跳过——运行时该人设本来就查不到缓存(音色为空不查),物化也无意义。
    """
    canned = canned or {}
    jobs: list[Job] = []
    for persona in persona_pool:
        lang = _normalize_lang((persona or {}).get("language"), default="zh") or "zh"
        entries = manifest.get(lang) or []
        texts = [str(e.get("text") or "") for e in entries]
        for t in canned.get(lang) or []:
            if t and t not in texts:
                texts.append(t)  # 罐头文本(manifest 无对应资产,纯 cache 物化层)
        voice = _persona_resolved_voice(persona, lang, tts_cfg, voice_mode)
        if (not entries and not texts) or not voice:
            print(
                f"fillers: persona={_persona_key(persona)} lang={lang} "
                f"voice={voice or '-'} skipped (no pool or no voice)",
                flush=True,
            )
            continue
        for t in texts:
            # text 原样透传(含 MiniMax <#x#> 停顿标记)——运行时 lookup/backfill
            # 都用 manifest 原文,key 必须同文。
            jobs.append((persona, lang, t, ""))
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
                # F-11 provider 闸:非 minimax 族的版本运行时查不到,不进计划。
                if not canned_cache_supported(persona, tts_cfg):
                    continue
                if not _persona_resolved_voice(persona, lang, tts_cfg, voice_mode):
                    continue
                jobs.append((persona, lang, text, ""))
        else:
            persona = lang_personas.get(lang)
            # F-11 provider 闸(同上):计划期剔除,物化零烧云。
            if not canned_cache_supported(persona, tts_cfg):
                continue
            jobs.append((lang_personas.get(lang), lang, text, ""))
    return jobs


def _qa_status(
    qa_rows: list[dict],
    persona_pool: list[dict],
    lang_personas: dict[str, dict | None],
    *,
    all_personas: bool,
    tts_cfg: dict,
    voice_mode: str,
    model: str,
    sample_rate: int,
    cache: TtsAudioCache,
    entry_ids: set[str] | None = None,
) -> dict[str, dict]:
    """--qa-status 可测核心:与 _materialize 同口径逐条目算 key、查缓存。

    条目状态:任一计划音色版本在缓存=ok,否则 missing。停用/空答案不进结果。
    音色解析走 _persona_resolved_voice(_assemble_minimax_voice_map+
    _resolve_voice_map 运行时同源)、语速走 minimax_speed_for、键走
    cache.key_for(text, voice, model, speed, emotion="")——与 _materialize 逐项
    同一口径,零第二套解析;不合成故无人设 map 备忘(逐条目现算,纯查询)。
    sample_rate 入参仅与 _materialize 签名对齐(键的采样率维度经 cache.key_for
    取 cache.sample_rate,调用方以同一 sample_rate 构造 cache)。
    """
    out: dict[str, dict] = {}
    for e in qa_rows:
        eid = str(e.get("id") or "")
        if not eid or (entry_ids and eid not in entry_ids):
            continue
        if not bool(e.get("enabled", True)):
            continue
        text = str(e.get("answer_text") or "").strip()
        if not text:
            continue
        lang = _normalize_lang((e or {}).get("lang"), default="zh") or "zh"
        voices: list[str] = []
        provider_off = False
        if all_personas:
            for persona in persona_pool:
                # F-11 provider 闸:非 minimax 族的 persona 版本运行时查不到,不入候选。
                if not canned_cache_supported(persona, tts_cfg):
                    provider_off = True
                    continue
                v = _persona_resolved_voice(persona, lang, tts_cfg, voice_mode)
                if v:
                    voices.append(v)
        else:
            persona = lang_personas.get(lang)
            if not canned_cache_supported(persona, tts_cfg):
                provider_off = True
            else:
                v = _persona_resolved_voice(persona, lang, tts_cfg, voice_mode)
                if v:
                    voices.append(v)
        if not voices:
            entry = {"state": "missing", "voice": "", "key": ""}
            # F-11 信息位:缺料原因是有效 provider 非 minimax 族(运行时无缓存链,
            # 物化/补录都无效),与「缺录音」区分——运营先改 provider 再谈补录。
            if provider_off:
                entry["reason"] = "provider_off"
            out[eid] = entry
            continue
        speed = minimax_speed_for(lang)
        state = "missing"
        voice_used = ""
        key_used = ""
        for voice in voices:
            key = cache.key_for(text, voice=voice, model=model, speed=speed, emotion="")
            if cache.get(key) is not None:
                state, voice_used, key_used = "ok", voice, key
                break
            state, voice_used, key_used = "missing", voice_used or voice, key_used or key
        out[eid] = {"state": state, "voice": voice_used, "key": key_used}
    return out


def _branch_status(
    templates: list[dict],
    lang_personas: dict[str, dict | None],
    *,
    tts_cfg: dict,
    voice_mode: str,
    model: str,
    cache: TtsAudioCache,
    texts: set[str] | None = None,
) -> dict[str, str]:
    """--branch-status 可测核心:逐分支应答产出 ok/missing/ph 三态(纯查询零合成)。

    键=分支 resp 原文(含动作标记,逐字节——CP/web 拿它对画布答法抽屉);要物化
    的音频文本=剥标记后渲染。渲染与 --branches 物化同口径(render_template_text+
    空变量表):渲染后仍有 {占位} 残留 → "ph"——变量缺失时空变量渲染与运行时
    对象变量渲染必不同文,缓存永远打不中,报 missing 会误导运营补录一条永远
    无效的录音(应先改话术)。同一 raw resp 跨模板/跨语言出现时按语言集合逐上下文
    探测:任一上下文缓存命中=ok;否则存在可合成上下文(渲染干净且解析出音色)=
    missing;全部上下文都是占位残留/空文本=ph。音色/语速/键与 _branch_jobs 逐项
    同源(_persona_resolved_voice+minimax_speed_for+cache.key_for)。
    """
    ctx_langs: dict[str, set[str]] = {}
    for tpl in templates or []:
        lang = _normalize_lang((tpl or {}).get("language"), default="") or ""
        if not lang:
            continue
        for s in parse_steps(str(tpl.get("steps_json") or "")):
            for _cond, resp in parse_step_ref(s.ref or "").branches:
                raw = str(resp or "").strip()
                if not raw or (texts is not None and raw not in texts):
                    continue
                ctx_langs.setdefault(raw, set()).add(lang)
    out: dict[str, str] = {}
    for raw, langs in ctx_langs.items():
        hit = False
        synthable = False
        for lang in sorted(langs):
            rendered = render_template_text(_strip_branch_action(raw), {})
            if not rendered.strip() or re.search(r"\{[^{}]+\}", rendered):
                continue  # ph 上下文:补录无效
            persona = lang_personas.get(lang)
            # F-11 provider 闸:非 minimax 族上下文运行时无缓存链,查不到任何键
            # ——不算可合成上下文(qwen3 栈同一份 MiniMax 缓存在场也绝不报 ok)。
            if not canned_cache_supported(persona, tts_cfg):
                continue
            voice = _persona_resolved_voice(persona, lang, tts_cfg, voice_mode)
            if not voice:
                continue  # 无音色:运行时同样不查缓存
            synthable = True
            key = cache.key_for(
                rendered, voice=voice, model=model,
                speed=minimax_speed_for(lang), emotion="",
            )
            if cache.get(key) is not None:
                hit = True
                break
        out[raw] = "ok" if hit else ("missing" if synthable else "ph")
    return out


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
    pin: bool = False,
) -> tuple[int, int, int, list[Record]]:
    """合成+落盘主循环(greetings/objects/fillers/qa 四条物化线共用)。

    voice 按 (persona, lang) 运行时同源解析(map 组装带备忘,同人设同语言只算
    一次);(persona, lang, text, emotion) 四元组去重;已在缓存(同 text+voice+
    model+emotion)的条目 skip 不重合成。dry_run=True 只按同口径分类计数,绝不
    碰云 API。

    pin=True 落盘打钉不逐出(罐头集:静态直念线/垫话/QA——无界的逐对象
    开场白别传,否则 LRU 失去意义)。

    返回 (generated, skipped, failed, records)。
    """
    seen: set[tuple[str, str, str, str]] = set()
    uniq: list[Job] = []
    for persona, lang, text, emotion in jobs:
        text = str(text or "").strip()
        emotion = str(emotion or "").strip().lower()
        if not text:
            continue
        k = (_persona_key(persona), lang, text, emotion)
        if k in seen:
            continue
        seen.add(k)
        uniq.append((persona, lang, text, emotion))

    map_cache: dict[tuple[str, str], dict] = {}
    ok = skip = fail = 0
    records: list[Record] = []
    for persona, lang, text, emotion in uniq:
        pk = (_persona_key(persona), lang)
        voice_map = map_cache.get(pk)
        if voice_map is None:
            voice_map = _assemble_minimax_voice_map(
                persona=persona, tts_cfg=tts_cfg, greet_lang=lang, voice_mode=voice_mode
            )
            map_cache[pk] = voice_map
        voice = _resolve_voice_map(voice_map, lang)
        # F-11(2026-09-23)provider 闸:罐头缓存只挂 MiniMax 链——非 minimax 族
        # 运行时(_tts_cache=None)结构性查不到任何键,物化=白烧云+状态面假 ok。
        # 与 agent.canned_cache_supported 同一判据(单源),skip 不计 fail(非故障)。
        if not canned_cache_supported(persona, tts_cfg):
            skip += 1
            records.append((persona, lang, voice, "skip"))
            print(
                f"SKIP_PROVIDER_OFF persona={pk[0]} lang={lang} "
                f"provider={effective_tts_provider(persona, tts_cfg)} — "
                "罐头缓存只挂 MiniMax 链,该 provider 下运行时查不到键,不物化不烧云",
                flush=True,
            )
            continue
        if not voice:
            fail += 1
            records.append((persona, lang, "", "fail"))
            print(f"NO_VOICE persona={pk[0]} lang={lang} — 跳过", flush=True)
            continue
        # 语速维度(W2):与运行时同一条语言档规则(zh/粤 1.2)——合成(provider 的
        # language_state 已按 lang 构造)与缓存键同步带 speed,速度烧在音频里。
        speed = minimax_speed_for(lang)
        key = cache.key_for(text, voice=voice, model=model, speed=speed, emotion=emotion)
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
            # 情绪维度(2026-09-16 罐头带情绪):行级 emotion 经 MINIMAX_EMOTION
            # 注入,_resolve_emotion 在 task_start 时直透。批量线单进程串行、
            # 逐 job 建 provider——env 注入无并发竞态;空=不下发(模型按文本
            # 自动匹配,与运行时实时线同语义,轮间语气稳定)。
            if emotion:
                os.environ["MINIMAX_EMOTION"] = emotion
            try:
                pcm = await _synth(provider, text)
            finally:
                if emotion:
                    os.environ.pop("MINIMAX_EMOTION", None)
        except Exception as exc:  # noqa: BLE001 - 单条失败唔阻整体
            fail += 1
            records.append((persona, lang, voice, "fail"))
            print(f"FAIL lang={lang} chars={len(text)} err={exc!r}", flush=True)
            continue
        stored = cache.store(
            key, pcm, text=text, voice=voice, model=model, pin=pin, speed=speed, emotion=emotion
        )
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
    ap.add_argument("--branches", action="store_true", help="话术分支应答预合成(步骤 ref「如果客户X→就Y」的 Y,剥动作标记渲染后无占位才物化;分支罐头快路只认缓存有音频)")
    ap.add_argument("--qa-status", action="store_true", help="不合成:逐条目输出物化状态 JSON(stdout),供 CP canned-status 端点消费")
    ap.add_argument("--branch-status", action="store_true", help="不合成:逐分支应答输出物化状态 JSON(stdout,键=resp 原文含动作标记,值 ok/missing/ph),供 CP branch-canned-status 端点消费")
    ap.add_argument("--entry-id", action="append", default=[], help="只处理指定 qa 条目 id(可重复;--qa/--qa-status 共用过滤)")
    ap.add_argument("--texts-file", default="", help="只处理指定文本(一行一条,--branches/--branch-status 共用过滤;供 CP 按分支补录/查询)")
    ap.add_argument("--all-personas", action="store_true", help="--qa 配套:条目 × 每个启用人设各物化一版(缺省每语言只取该语言人设)")
    ap.add_argument("--dry-run", action="store_true", help="只打印将物化的 (persona,lang,voice,条数) 计划清单,不合成")
    ap.add_argument("--cp", default=os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--account-id", default="", help="分支模式(--branches/--branch-status)按账号过滤 CP 模板(缺省不带参,走 CP 端点默认账号)")
    ap.add_argument("--persona", default="", help="人设范围:指定 persona id(fillers/qa-all 只物化该人设;缺省按语言取该语言的 persona/全部人设)")
    ap.add_argument("--object-id", default="", help="只为指定对象预生成开场白/收线/心跳(配合 --objects)")
    ap.add_argument("--object", default="", help="直念步(--greetings 线)变量渲染所用对象 id:缺省=该账号最近更新的对象;无对象=保留 {占位} 的行不物化(--object-id 缺省时作同义回退)")
    ap.add_argument("--model", default="", help="MINIMAX_MODEL 覆盖(默认 env/2.8-hd,须与运行时一致)")
    args = ap.parse_args()
    if not (args.greetings or args.objects or args.fillers or args.qa or args.branches
            or args.qa_status or args.branch_status):
        args.greetings = True

    if os.environ.get("MINIMAX_API_KEY", ""):
        api_key = os.environ["MINIMAX_API_KEY"]
    else:
        api_key = ""
    token = os.environ.get("BOK_CP_TOKEN", "")
    settings, personas, objects, templates = _fetch_cp(
        args.cp, token,
        account_id=args.account_id if (args.branches or args.branch_status) else "",
    )
    tts_cfg = settings.get("tts") or {}
    api_key = api_key or str(tts_cfg.get("api_key") or "")
    # --qa-status/--branch-status 纯状态面零合成(dry-run 同理):无 key 也放行。
    if not api_key and not (args.dry_run or args.qa_status or args.branch_status):
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
    # F1(2026-09-20)信息位:状态面所用音色的逐语言来源——"persona:<id>"=该语言
    # 有人设供音色(与绑定该人设的运行时同源);"personas_default"=该语言无人设,
    # 落设置页 speaker_*/内置默认音色(与绑定了人设的运行时不同源,判 ok 可能是
    # 对错键的「假 ok」)。CP 端点顶层透传,不改 statuses 三态语义。
    voice_source: dict[str, str] = {
        lang: (
            f"persona:{str((lang_personas.get(lang) or {}).get('id') or '')}"
            if lang_personas.get(lang)
            else "personas_default"
        )
        for lang in ("zh", "cantonese", "en")
    }
    # F-11(2026-09-23)信息位:逐语言有效 TTS provider(与运行时 agent 装配同源
    # 判据)。非 minimax 族=运行时无罐头缓存链,状态面的一切 ok/missing 都只是
    # MiniMax 键位的读数,CP 端点顶层透传。
    tts_provider_map: dict[str, str] = {
        lang: effective_tts_provider(lang_personas.get(lang), tts_cfg)
        for lang in ("zh", "cantonese", "en")
    }
    # 按人设物化的人设池(fillers / qa --all-personas 共用):--persona 限单人人设,
    # 缺省=CP 全部人设(人设无 enabled 字段,在册即启用;无音色/无池者在计划期跳过)。
    if args.persona:
        persona_pool = [x for x in personas if str(x.get("id")) == args.persona]
        if not persona_pool:
            # stderr：--qa-status 的 stdout 是纯 JSON 契约（CP canned-status 逐行解析）。
            print(f"persona {args.persona} not found in CP /api/personas", file=sys.stderr, flush=True)
            # F1(2026-09-20):指定人设找不到时绝不静默回落默认音色——回落物化出的
            # 缓存键与运行时人设音色错位(永远 miss),状态面还会对错键报「假 ok」。
            # 响亮失败+非零退出(3),此刻零 provider 构造零缓存写,一个字节不落盘。
            print(
                "PERSONA_MISSING abort (no default-voice fallback, nothing written): "
                f"--persona {args.persona}",
                file=sys.stderr,
                flush=True,
            )
            return 3
    else:
        persona_pool = list(personas)

    cache = TtsAudioCache(root=default_cache_dir(), sample_rate=sample_rate)
    model = os.environ.get("MINIMAX_MODEL", "speech-2.8-hd")

    # ---- Q→A 条目拉取(--qa 物化与 --qa-status 共用)+ --entry-id 过滤 ----
    # 提前到一切物化段之前:--qa-status 在任何合成开始前 return,绝不建 provider。
    qa_rows: list[dict] = []
    if args.qa or args.qa_status:
        # Q→A 快路启用条目:应答文本按条目语言取对应 persona 音色物化——运行时
        # 闸门按 (answer_text, resolved_voice, model) 查缓存,音色必须同源。
        # 空表/拉取失败静默(库未建=无物化需求)。
        try:
            qa_rows = _cp_get(args.cp, "/api/qa-entries?enabled=1", token) or []
        except Exception as exc:  # noqa: BLE001 - 库未建/CP 不可达唔阻其他预合成
            # stderr：--qa-status 的 stdout 是纯 JSON 契约（CP canned-status 逐行解析）。
            print(f"qa entries fetch failed: {exc!r}", file=sys.stderr, flush=True)
            qa_rows = []
        if args.entry_id:
            _want = {str(x) for x in args.entry_id}
            qa_rows = [r for r in qa_rows if str((r or {}).get("id") or "") in _want]

    if args.qa_status:
        # 状态面:零合成零云——逐条目按物化同口径算 key、查缓存,stdout 单行 JSON
        # 供 CP canned-status 端点(Task 3)消费。音色解析链上的偶发诊断打印
        # ([agent] minimax default/skip local)重定向到 stderr,保 stdout 纯 JSON。
        with contextlib.redirect_stdout(sys.stderr):
            status = _qa_status(
                qa_rows, persona_pool, lang_personas,
                all_personas=args.all_personas, tts_cfg=tts_cfg, voice_mode=voice_mode,
                model=model, sample_rate=sample_rate, cache=cache,
                entry_ids=set(args.entry_id) if args.entry_id else None,
            )
        print(json.dumps({"qa_status": status, "voice_source": voice_source,
                          "tts_provider": tts_provider_map}, ensure_ascii=False), flush=True)
        return 0

    if args.branch_status:
        # 分支状态面:零合成零云——逐分支按物化同口径算 key、查缓存,stdout 单行
        # JSON 供 CP branch-canned-status 端点消费。stdout 纯 JSON 契约与
        # --qa-status 同款(诊断打印重定向 stderr)。
        with contextlib.redirect_stdout(sys.stderr):
            status = _branch_status(
                templates, lang_personas,
                tts_cfg=tts_cfg, voice_mode=voice_mode, model=model, cache=cache,
                texts=_load_texts_file(args.texts_file),
            )
        print(json.dumps({"branch_status": status, "voice_source": voice_source,
                          "tts_provider": tts_provider_map}, ensure_ascii=False), flush=True)
        return 0

    # 缓存目录须与运行时同根:BOK_TTS_CACHE_DIR 由 bok.py/调用方透传。
    print(f"cache dir={cache.root} model={model} sample_rate={sample_rate}", flush=True)

    total = gen = skip = fail = 0

    # ---- 无变量脚本线(静态有限集=钉住)+ 逐对象线(开场白无界=不钉) ----
    # 每语言 lang_personas 人设;两条线 pin 语义不同,分开物化。
    greet_jobs: list[Job] = []
    object_jobs: list[Job] = []
    if args.greetings:
        for lang, text in GENERIC_GREETINGS.items():
            greet_jobs.append((lang_personas.get(lang), lang, text, ""))
        for lang in ("zh", "cantonese", "en"):
            for i in range(3):
                greet_jobs.append((lang_personas.get(lang), lang, _nudge_line("", lang, i), ""))
            greet_jobs.append((lang_personas.get(lang), lang, _farewell_line("", lang), ""))
            greet_jobs.append((lang_personas.get(lang), lang, _wa_number_line(lang, ""), ""))
        # 直念步(say=1)文本线:通知/道歉类合规内容,agent 走 _say_script 脚本线
        # ——同一条缓存线物化(钉住)。按语言去重(同语言模板共用同一段通知)。
        # 变量渲染对象(2026-09-27):--object 显式 / 回退 --object-id / 缺省=该
        # 账号最近更新对象;无对象=None → 空变量(含占位符的行不物化,旧行为)。
        _say_obj = _resolve_say_object(objects, object_id=args.object or args.object_id)
        _seen_notice: set[tuple[str, str]] = set()
        for tpl in templates or []:
            _tlang = _normalize_lang((tpl or {}).get("language"), default="") or ""
            if not _tlang:
                continue
            for text, emotion in _say_step_lines(tpl, _say_obj):
                if (_tlang, text) in _seen_notice:
                    continue
                _seen_notice.add((_tlang, text))
                greet_jobs.append((lang_personas.get(_tlang), _tlang, text, emotion))

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
                object_jobs.append((lang_personas.get(lang), lang, opening, ""))
            if name:
                object_jobs.append((lang_personas.get(lang), lang, _farewell_line(name, lang), ""))
                for i in range(3):
                    object_jobs.append(
                        (lang_personas.get(lang), lang, _nudge_line(name, lang, i), "")
                    )

    for jobs, pin in ((greet_jobs, True), (object_jobs, False)):
        if not jobs:
            continue
        ok, sk, fl, records = await _materialize(
            cache, model, jobs,
            api_key=api_key, sample_rate=sample_rate, tts_cfg=tts_cfg,
            voice_mode=voice_mode, dry_run=args.dry_run, pin=pin,
        )
        gen += ok
        skip += sk
        fail += fl
        total += ok + sk + fl
        if args.dry_run:
            _print_group_counts("plan", records, with_total=True)

    # ---- 垫话按人设物化(task-14b 复活;2026-09-13 含罐头条目) ----
    # 运行时 FillerDirector 双层选源:先查 (text, voice, model) 人设物化版
    # (命中=与通话完全同人声),miss 播源码资产兜底+后台补物化。这里即批量
    # 补物化入口:新人设上线跑一次 --fillers,垫话即全程人设音色。罐头库
    # (filler_entries)条目文本一并物化——确定性命中层只有 cache 有音频才出声。
    if args.fillers:
        try:
            manifest = load_manifest(FILLER_ASSETS_DIR)
        except Exception as exc:  # noqa: BLE001 - 资产缺失=垫话物化停用(响亮降级)
            print(f"fillers manifest unavailable dir={FILLER_ASSETS_DIR} err={exc!r}", flush=True)
            manifest = {}
        canned: dict[str, list[str]] = {}
        try:
            for row in _cp_get(args.cp, "/api/fillers?enabled=1", token) or []:
                _lang = _normalize_lang((row or {}).get("lang"), default="") or ""
                _text = str((row or {}).get("text") or "").strip()
                if _lang and _text:
                    canned.setdefault(_lang, []).append(_text)
            if canned:
                print(f"fillers canned entries: { {k: len(v) for k, v in canned.items()} }", flush=True)
        except Exception as exc:  # noqa: BLE001 - CP 不可达=只物化 manifest 句
            print(f"fillers canned fetch failed: {exc!r} — 只物化 manifest 句", flush=True)
        fillers_jobs = _fillers_jobs(persona_pool, manifest, tts_cfg, voice_mode, canned=canned)
        if fillers_jobs:
            ok, sk, fl, records = await _materialize(
                cache, model, fillers_jobs,
                api_key=api_key, sample_rate=sample_rate, tts_cfg=tts_cfg,
                voice_mode=voice_mode, dry_run=args.dry_run, pin=True,
            )
            gen += ok
            skip += sk
            fail += fl
            total += ok + sk + fl
            _print_group_counts("fillers", records, with_total=args.dry_run)

    # ---- Q→A 快路条目物化(条目已在上方拉取并过 --entry-id 过滤) ----
    if args.qa:
        qa_job_list = _qa_jobs(
            qa_rows, persona_pool, lang_personas,
            all_personas=args.all_personas, tts_cfg=tts_cfg, voice_mode=voice_mode,
        )
        if qa_job_list:
            ok, sk, fl, records = await _materialize(
                cache, model, qa_job_list,
                api_key=api_key, sample_rate=sample_rate, tts_cfg=tts_cfg,
                voice_mode=voice_mode, dry_run=args.dry_run, pin=True,
            )
            gen += ok
            skip += sk
            fail += fl
            total += ok + sk + fl
            if args.all_personas:
                _print_group_counts("qa", records, with_total=args.dry_run)

    # ---- 话术分支应答物化(2026-09-20 路线 A-①;模板已在上方拉取) ----
    # 分支罐头快路闸门只认缓存有音频——物化即开闸;pin=True 罐头集永不逐出
    # (与 qa/greetings 直念线同纪律)。重复 resp 由 _materialize seen 去重。
    if args.branches:
        branch_job_list = _branch_jobs(
            templates, lang_personas, texts=_load_texts_file(args.texts_file)
        )
        if branch_job_list:
            ok, sk, fl, records = await _materialize(
                cache, model, branch_job_list,
                api_key=api_key, sample_rate=sample_rate, tts_cfg=tts_cfg,
                voice_mode=voice_mode, dry_run=args.dry_run, pin=True,
            )
            gen += ok
            skip += sk
            fail += fl
            total += ok + sk + fl
            _print_group_counts("branches", records, with_total=args.dry_run)

    print(f"pregen done total={total} generated={gen} skipped={skip} failed={fail}", flush=True)
    return 0 if fail == 0 else 2


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
