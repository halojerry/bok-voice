"""A 线语气管线（voice style）装配决策离线复现探针（2026-10-06 云档哑火查因）。

背景：2026-09-25 全量落地的语气/停顿标记管线（voice_style.py 六标记白名单 +
`<#x#>` 停顿 + NATURALNESS_BLOCK prompt 块 + TTS transform 双门控）在 2026-10-06
云档（MiniMax 2.8-hd + DeepSeek a_reply）实弹听感平淡。两条假设：
  H1 门解析：`voice_style_enabled_for_model` 在云档装配链拿到错模型字符串；
  H2 prompt：门过了但 LLM 不产标记（块按本地 4B 调的）。

本探针**离线复现装配决策**（默认零网络）：
  ① 读 app-data 设置库（只读 URI；key 只打掩码指纹，绝不回显）——
     tts_json.provider / persona tts_provider 覆写 / model_routing lanes.a_reply；
  ② 单点复算 resolved model：生产装配链 primary MiniMaxTTS **不传 model_override**
     （agent.py minimax 分支），`_model()` = env `MINIMAX_MODEL` 或缺省
     speech-2.8-hd——settings tts_json.model **不参与** A 线主档解析（填了也死）；
  ③ 门 verdict：`voice_style_enabled_for_model(model)`（voice_style.py 规范入口）；
  ④ prompt 注入：`ContextState.set_voice_style(verdict)` 后 NATURALNESS_BLOCK
     是否出现在 `render_instruction_prefix()`（与生产 ContextAwareLLM.chat 同源）；
  ⑤ 覆盖面事实（来自 turns 账本，只读）：窗口内 A 线轮按 gen 分布——脚本/罐头
     直念轮**结构性不带标记**（缓存键=text、资产预合成），管线只触达 gen=llm 轮；
     再对 llm 轮做句长 vs 换气注入地板（BOK_BREATH_SENT_CHARS 缺省 20、句尾
     pending 不落地）统计——短回复轮换气保底也难触发。
  ⑥ `--live N`（opt-in，真网络）：对 lanes.a_reply 同款 prompt 形状打 N 发，统计
     标记产出率（H2 直测；DeepSeek thinking 契约走 bok_voice_core 单点）。

用法：
  .venv312/bin/python scripts/probes/probe_voice_style_gate.py            # 离线
  .venv312/bin/python scripts/probes/probe_voice_style_gate.py --live 6   # +实弹
  .venv312/bin/python scripts/probes/probe_voice_style_gate.py --since 2026-10-06
退出码：0=探针跑完（门 verdict 无论开关都算成功）；2=前置缺失（DB 没库等）。
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
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# agent_runtime(voice_style/agent/livekit_plugins) 在 apps/agent 下；bok_voice_core 在 packages/core
for _p in (ROOT, ROOT / "apps" / "agent", ROOT / "packages" / "core"):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))

DEFAULT_DB = Path.home() / "Library" / "Application Support" / "BokVoice" / "bok_voice.db"

# --live 出站 host 白名单（SSRF 护栏，判例=cache_minimax_auditions._url_ok）：
# 本探针的测量对象=云 a_reply 车道（DeepSeek 产标记率），host 白名单只放官方域
# ——https、443/缺省口、无 userinfo、拒绝环回/私有/保留地址。DB 路由面可被写面
# 污染，不过形状校验=拒绝发请求。
_ALLOWED_LIVE_HOSTS = frozenset({"api.deepseek.com"})


def _url_ok(url: str) -> bool:
    parts = urllib.parse.urlsplit(str(url or ""))
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() in _ALLOWED_LIVE_HOSTS
        and parts.port in (None, 443)
        and not parts.username
        and not parts.password
    )

# 生产同款标记判定面（与 sanitize 白名单同源，外加 pause token 与候选扩容三件）
_MARK_RE = re.compile(
    r"\((?:breath|emm|inhale|exhale|clear-throat|coughs|sighs|chuckle|laughs)\)|<#[0-9.]+#>"
)
_SENT_SPLIT_RE = re.compile(r"[。！？!?]+")


def mask(value: str) -> str:
    """凭据指纹：只回 sha1 前 8 位 + 长度，绝不回显原值。"""
    if not value:
        return "<empty>"
    return f"<masked sha1={hashlib.sha1(value.encode()).hexdigest()[:8]} len={len(value)}>"


def load_settings(db: Path) -> dict:
    """只读打开设置库，取 tts_json / model_routing_json / persona 概况。

    key 类字段只保留 mask() 指纹；任何异常按保守语义报错退出（不猜）。"""
    if not db.exists():
        print(f"FAIL: 设置库不存在: {db}", file=sys.stderr)
        raise SystemExit(2)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT tts_json, model_routing_json FROM global_settings WHERE id='global'"
        ).fetchone()
        personas = con.execute(
            "SELECT name, tts_provider, language FROM persona_profiles ORDER BY name LIMIT 50"
        ).fetchall()
    finally:
        con.close()
    tts = json.loads(row["tts_json"] or "{}") if row else {}
    routing = json.loads(row["model_routing_json"] or "{}") if row else {}

    def _mask_json(d: dict) -> dict:
        out = {}
        for k, v in (d or {}).items():
            if isinstance(v, dict):
                out[k] = _mask_json(v)
            elif isinstance(v, str) and re.search(r"key|token|secret", k, re.I):
                out[k] = mask(v)
            else:
                out[k] = v
        return out

    return {
        "tts": _mask_json(tts),
        "routing": _mask_json(routing),
        "personas": [
            {"name": p["name"], "tts_provider": p["tts_provider"] or "", "language": p["language"] or ""}
            for p in personas
        ],
    }


def resolve_assembly(settings: dict) -> dict:
    """复现 agent.py 装配决策（单点语义，不猜）：

    - 有效 provider：effective_tts_provider(persona, tts_cfg)（F-11 单源）；
    - 主档模型：primary MiniMaxTTS 构造**不带 model_override** → `_model()` =
      env MINIMAX_MODEL 或缺省 speech-2.8-hd（settings tts_json.model 不入链）；
    - 门：voice_style_enabled_for_model(主档)（=env 闸 × "2.8" 判据）；
    - prompt：ContextState.set_voice_style(verdict) → NATURALNESS_BLOCK 在
      render_instruction_prefix() 里=与生产 ContextAwareLLM.chat 同源判定。
    """
    from agent_runtime.agent import effective_tts_provider  # F-11 单点判据
    from agent_runtime.voice_style import (
        NATURALNESS_BLOCK,
        breath_inject_enabled,
        voice_style_enabled_for_model,
    )

    tts_cfg = dict(settings["tts"])
    tts_cfg["provider"] = (tts_cfg.get("provider") or "").strip().lower()
    overrides = [p for p in settings["personas"] if (p.get("tts_provider") or "").strip()]

    # 生产装配链主实例构造参数里没有 model（agent.py minimax 分支逐字段核对），
    # 回退链 backup 才带 model_override=alt(hd→2.6-turbo)。主档即 env/缺省。
    resolved_model = (None or __import__("os").environ.get("MINIMAX_MODEL", "speech-2.8-hd")).strip()
    verdict = voice_style_enabled_for_model(resolved_model)

    block_injected = False
    prefix_chars = 0
    try:
        from agent_runtime.providers.livekit_plugins import ContextState

        cs = ContextState()
        cs.set_voice_style(verdict)
        prefix = cs.render_instruction_prefix()
        block_injected = NATURALNESS_BLOCK in prefix
        prefix_chars = len(prefix)
    except Exception as exc:  # pragma: no cover - livekit 装载失败时降级报告
        print(f"WARN: ContextState 渲染失败（{exc!r}）——prompt 注入判定不可用", file=sys.stderr)

    return {
        "effective_provider": effective_tts_provider(None, tts_cfg),
        "global_provider": tts_cfg.get("provider") or "",
        "persona_overrides": overrides,
        "resolved_model": resolved_model,
        "model_source": f"env MINIMAX_MODEL={__import__('os').environ.get('MINIMAX_MODEL', '')!r} or default",
        "settings_tts_model_field": tts_cfg.get("model") or "",
        "gate_verdict": verdict,
        "gate_env": "BOK_A_LINE_VOICE_TAGS=" + (__import__("os").environ.get("BOK_A_LINE_VOICE_TAGS", "1") or "1"),
        "breath_inject": breath_inject_enabled(),
        "block_injected": block_injected,
        "prefix_chars": prefix_chars,
    }


def turns_coverage(db: Path, since: str) -> dict:
    """覆盖面事实：窗口内 A 线 agent 轮按 gen 分布 + llm 轮句长 vs 换气地板。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT gen, transcript FROM turns "
            "WHERE created_at >= ? AND line='a' AND speaker='agent_ai'",
            (since,),
        ).fetchall()
    finally:
        con.close()
    by_gen: dict[str, int] = {}
    llm_stats = {"turns": 0, "would_breath": 0, "max_sent": 0}
    floor = 20
    try:
        floor = max(0, int(__import__("os").environ.get("BOK_BREATH_SENT_CHARS", "20")))
    except ValueError:
        pass
    for gen, text in rows:
        g = gen or "script"
        by_gen[g] = by_gen.get(g, 0) + 1
        if g == "llm" and text:
            llm_stats["turns"] += 1
            # 换气注入可达面模拟：句尾**非回复收尾**且句长≥floor 才注入
            # （流式实现里末句 pending 永不落地）；这里按完整文本近似——
            # 末句之外存在 ≥floor 的句子数。
            sents = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]
            if sents:
                for s in sents[:-1]:
                    llm_stats["max_sent"] = max(llm_stats["max_sent"], len(s.strip()))
                    if len(s.strip()) >= floor:
                        llm_stats["would_breath"] += 1
                        break
    return {"by_gen": by_gen, "total": sum(by_gen.values()), "llm": llm_stats, "breath_floor": floor}


# ---- --live：对 a_reply 车道同形状实弹（默认关） ----

_LIVE_USERS = [
    ("zh", "我有个包裹到仓了吗？帮我看下。"),
    ("zh", "哎呀我的包裹是不是丢了啊，等了好几天了！"),
    ("canto", "我件包裹到咗未呀？帮我睇下。"),
    ("canto", "哎呀我隻包裹係咪唔見咗呀，等咗幾日喇！"),
]
_LIVE_TAIL = "\n\n【当前步骤】第2步：通知客户包裹到仓。\n【通话中客户已讲】无"
_LIVE_PERSONA = "你是集运仓库的客服专员小助手，称呼客户为您。语气亲切自然，像熟人聊天。"


def live_fire(settings: dict, prefix: str, n: int, timeout_s: float) -> int:
    """对 lanes.a_reply 打 n 发（轮转取句），打印标记产出率。key 绝不回显。

    SSRF 护栏（判例=cache_minimax_auditions._url_ok）：base_url 来自 settings DB
    （可被写面污染），发请求前过 _url_ok 形状校验——仅 https://api.deepseek.com
    （本探针测量对象=云车道 DeepSeek 产标记率），拒绝环回/私有/保留地址。"""
    from bok_voice_core.deepseek_llm import thinking_extra_body

    # routing 里 key 已被 mask——真 key 从 DB 重读（load_settings 只用于展示面）。
    lane_raw = _raw_lane(settings)
    if not lane_raw:
        print("FAIL: --live 需要 model_routing lanes.a_reply（openai 档）", file=sys.stderr)
        return 2
    base_url, model, key = lane_raw
    if not _url_ok(base_url):
        print(
            f"FAIL: base_url 未过 SSRF 白名单: {base_url}（仅 https://api.deepseek.com）",
            file=sys.stderr,
        )
        return 2
    print(f"[live] base_url={base_url} model={model} key={mask(key)} thinking=disabled(单点契约)")
    body_extra = thinking_extra_body(base_url)  # DeepSeek 端点缺省关思考（单点）
    system = prefix + "\n" + _LIVE_PERSONA
    marked = 0
    for i in range(n):
        lang, user = _LIVE_USERS[i % len(_LIVE_USERS)]
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user + _LIVE_TAIL},
            ],
            "max_tokens": 512,
            **body_extra,
        }
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode())
            text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as exc:
            print(f"[live] {i + 1}/{n} {lang} FAIL {exc!r}", file=sys.stderr)
            continue
        hits = _MARK_RE.findall(text)
        if hits:
            marked += 1
        print(f"[live] {i + 1}/{n} {lang} markers={hits or 'NONE'} | {text[:80]!r}")
        time.sleep(0.2)
    print(f"[live] SUMMARY marked={marked}/{n}")
    return 0


_RAW_LANE_CACHE: list = []


def _raw_lane(settings: dict) -> list:
    """从设置库重读 a_reply 车道真值（routing 展示副本里 key 已 mask）。"""
    if _RAW_LANE_CACHE:
        return _RAW_LANE_CACHE
    db = Path(_DB_ARG)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT model_routing_json FROM global_settings WHERE id='global'").fetchone()
    finally:
        con.close()
    lane = ((json.loads(row[0] or "{}").get("lanes") or {}).get("a_reply") or {}) if row else {}
    if (lane.get("provider") or "") != "openai" or not (lane.get("api_key") or "").strip():
        return []
    _RAW_LANE_CACHE.extend([lane["base_url"], lane["model"], lane["api_key"]])
    return _RAW_LANE_CACHE


_DB_ARG = DEFAULT_DB


def main(argv: list[str] | None = None) -> int:
    global _DB_ARG
    ap = argparse.ArgumentParser(description="A 线语气管线装配决策离线复现（H1 门解析 / H2 prompt 查因）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="设置库路径（缺省 app-data bok_voice.db）")
    ap.add_argument("--since", default=date.today().isoformat(), help="覆盖面统计窗口起点（YYYY-MM-DD）")
    ap.add_argument("--live", type=int, default=0, metavar="N", help="对 a_reply 车道实弹 N 发测标记率（默认 0=离线）")
    ap.add_argument("--timeout", type=float, default=30.0, help="--live 单发超时秒")
    args = ap.parse_args(argv)
    _DB_ARG = args.db
    db = Path(args.db)

    settings = load_settings(db)
    print("== settings（key 全掩码）==")
    print(f"tts.provider={settings['tts'].get('provider')!r} tts.model(field)={settings['tts'].get('model')!r}")
    lane = ((settings["routing"].get("lanes") or {}).get("a_reply") or {})
    print(
        "routing.a_reply: "
        + json.dumps({k: lane.get(k) for k in ("provider", "base_url", "model")}, ensure_ascii=False)
    )
    for p in settings["personas"]:
        if p["tts_provider"]:
            print(f"persona override: name={p['name']!r} tts_provider={p['tts_provider']!r} lang={p['language']!r}")

    asm = resolve_assembly(settings)
    print("\n== 装配决策复现（生产同源语义）==")
    print(f"effective tts provider = {asm['effective_provider']!r} (global={asm['global_provider']!r})")
    print(f"resolved primary model = {asm['resolved_model']!r}  [{asm['model_source']}]")
    if asm["settings_tts_model_field"]:
        print(
            f"NOTE: settings tts.model={asm['settings_tts_model_field']!r} 存在但不参与 A 线主档解析"
            "（primary MiniMaxTTS 不读该字段）"
        )
    print(f"gate = voice_style_enabled_for_model(...) -> {asm['gate_verdict']}  ({asm['gate_env']})")
    print(
        f"prompt: NATURALNESS_BLOCK injected={asm['block_injected']} prefix_chars={asm['prefix_chars']}"
        f"  breath_inject={asm['breath_inject']}"
    )
    _ok = asm["gate_verdict"] and asm["block_injected"]
    verdict = "H1 排除（门开、块注入）" if _ok else "H1 疑似（门关或块缺席）"
    print(f"VERDICT: {verdict}")

    cov = turns_coverage(db, args.since)
    print(f"\n== 覆盖面事实（turns 账本 {args.since} 起，只读）==")
    print(
        f"A 线 agent 轮 total={cov['total']} by_gen={json.dumps(cov['by_gen'], ensure_ascii=False)}"
        f"  → 管线只触达 gen=llm 轮（脚本/罐头直念结构性不带标记）"
    )
    ls = cov["llm"]
    print(
        f"llm 轮={ls['turns']} 非末句≥换气地板({cov['breath_floor']}字)的轮={ls['would_breath']}"
        f" 非末句最长={ls['max_sent']}字（地板上=换气保底也难触发的量化）"
    )

    if args.live > 0:
        from agent_runtime.providers.livekit_plugins import ContextState
        from agent_runtime.voice_style import NATURALNESS_BLOCK

        cs = ContextState()
        cs.set_voice_style(True)
        prefix = cs.render_instruction_prefix()
        assert NATURALNESS_BLOCK in prefix
        return live_fire(settings, prefix, args.live, args.timeout)
    print("\n(提示: --live N 可对 a_reply 车道实弹测标记产出率)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
