#!/usr/bin/env python3
"""ASR 评测语料·四语版（德/法/日/葡，W2c 2026-10-06）：MiniMax 云 TTS 渲染豆包实测语料。

背景：W2 已把 de/fr/ja/pt 放进 B 线语言下拉，但豆包 SAUC（seedasr 2.0）对四语
「宣发≠实测」。本脚本渲染客服场景小语料（每语 6 句：问候/问运单/确认身份/道歉/
数字串/长句，数字句 8-12 位运单号按语种书写），供 scripts/probes/probe_cloud_asr.py
--corpus 直测豆包 lane，产出放行/灰区/限缩判定。

- 渲染=MiniMax 云 TTS（speech-2.8-hd，t2a_v2 非流式）：凭据运行期从 settings DB
  （global_settings.tts_json.api_key，只读打开）读，env MINIMAX_API_KEY 可覆盖；
  代码零密钥、零明文。SSRF 护栏与目录/凭据加载复用 seed/cache_minimax_auditions.py
  同一实现（_url_ok + ALLOWED_HOSTS + parse_catalog 正则解析 TS 目录，单源不复制）；
- 音色=minimax-voices.ts 对应语种组首只非克隆条目（moss_audio_ 前缀=用户克隆资产
  跳过）：de→German_SweetLady / fr→French_Female_News Anchor / ja→Japanese_efficient_
  reporter_vv1 / pt→Portuguese_UpsetGirl；语速 1.0（非 zh/粤生产规则）；四语各带
  官方 language_boost（German/French/Japanese/Portuguese）提升发音地道度；
- 产物=reports/asr-4lang-corpus/<lang>/NN.wav（mp3 合成后 afconvert 转 16k mono
  PCM16，与 corpus v2 同规格；仅 macOS）+ 根 manifest.json（id/lang/text/digits/
  dur_s/file，probe_cloud_asr 直接可吃）。reports/ 已 gitignore，产物不进仓；
- 幂等：目标 wav 已存在即跳过（--force 重渲）；逐句限频小睡防 RPM 限频。

用法：
  .venv312/bin/python scripts/seed/render_asr_corpus_4lang.py             # 全量（幂等）
  .venv312/bin/python scripts/seed/render_asr_corpus_4lang.py --lang ja   # 只跑日语
  .venv312/bin/python scripts/seed/render_asr_corpus_4lang.py --force
  .venv312/bin/python scripts/seed/render_asr_corpus_4lang.py --dry-run   # 只列清单
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
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from cache_minimax_auditions import (
    _url_ok,
    endpoint,
    load_api_key,
    parse_catalog,
)

ROOT = Path(__file__).resolve().parents[2]
OUT_DEFAULT = ROOT / "reports" / "asr-4lang-corpus"

MODEL = "speech-2.8-hd"  # 与 cache_minimax_auditions 同款
SAMPLE_RATE = 32000  # mp3 合法档（8000/16000/22050/24000/32000/44100）
TIMEOUT_S = 60
RETRIES = 3
PACE_S = 0.6  # 逐句小睡，给 RPM 限频留余量
SPEED = 1.0  # 非 zh/粤语速规则（生产唯一速度源同款）

LANGS = ("de", "fr", "ja", "pt")
# MiniMax 官方 language_boost 合法值（文档枚举单点映射；四语各挂本语种 boost）。
LANG_BOOST = {"de": "German", "fr": "French", "ja": "Japanese", "pt": "Portuguese"}


# ---- 语料定义（客服场景六句型：问候/问运单/确认身份/道歉/数字串/长句） ----
# 数字句 8-12 位运单号按语种书写：de/fr/pt 数字词逐位、ja 阿拉伯数字顿号逐位
# （TTS 按顿号逐位读；ASR ITN 回数字/数字词均可，probe 侧归一后比对）。
CORPUS: dict[str, list[dict]] = {
    "de": [
        {"id": "de_01", "text": "Guten Tag, kann mir jemand bei meiner Bestellung helfen?"},
        {"id": "de_02", "text": "Ich möchte wissen, wo mein Paket gerade ist, es war schon längst überfällig."},
        {"id": "de_03",
         "text": "Mein Name ist Thomas Berger, ich wohne in der Lindenstraße und bin Kunde seit zwei Jahren."},
        {"id": "de_04", "text": "Entschuldigen Sie bitte die Störung, ich weiß, Sie haben viel zu tun."},
        {"id": "de_05",
         "text": "Meine Sendungsnummer ist acht vier eins sechs null zwei sieben drei, bitte prüfen Sie die.",
         "digits": "84160273"},
        {"id": "de_06",
         "text": "Der Zusteller hat angeblich gestern Nachmittag geklingelt, aber ich war den ganzen Tag zu Hause "
                 "und niemand ist gekommen, deshalb möchte ich eine Nachforschung beantragen."},
    ],
    "fr": [
        {"id": "fr_01", "text": "Bonjour, j'appelle parce que ma commande n'est toujours pas arrivée."},
        {"id": "fr_02", "text": "Pouvez-vous vérifier où se trouve mon colis, s'il vous plaît ?"},
        {"id": "fr_03", "text": "Je m'appelle Claire Moreau, je confirme que c'est bien moi, la titulaire du compte."},
        {"id": "fr_04", "text": "Désolé pour le dérangement, la connexion a coupé au mauvais moment."},
        {"id": "fr_05", "text": "Mon numéro de suivi est sept quatre neuf deux un six zéro cinq trois.",
         "digits": "749216053"},
        {"id": "fr_06",
         "text": "Le livreur a prétendu avoir déposé le paquet devant ma porte hier après-midi, "
                 "mais je n'ai rien reçu et personne n'a sonné, je voudrais une explication."},
    ],
    "ja": [
        {"id": "ja_01", "text": "こんにちは、先週注文した荷物について、お伺いしたいことがあります。"},
        {"id": "ja_02", "text": "すみません、私の荷物がまだ届かないのですが、確認していただけますか。"},
        {"id": "ja_03", "text": "私は田中花子と申します。ご本人確認をお願いします。"},
        {"id": "ja_04", "text": "申し訳ありません、電波の状態が悪くて、もう一度お願いしてもいいですか。"},
        {"id": "ja_05", "text": "追跡番号は、4、5、1、2、8、0、3、7です。",
         "digits": "45128037"},
        {"id": "ja_06",
         "text": "昨日の午後に配達完了の通知が届いたのですが、実際には荷物が届いていませんので、"
                 "調査していただけますでしょうか。"},
    ],
    "pt": [
        {"id": "pt_01", "text": "Olá, boa tarde, eu ligo porque a minha encomenda ainda não chegou."},
        {"id": "pt_02", "text": "Pode verificar o estado da minha encomenda, por favor ?"},
        {"id": "pt_03", "text": "O meu nome é Maria Fernandes, confirmo que sou eu a titular da conta."},
        {"id": "pt_04", "text": "Desculpe incomodar, a chamada caiu no pior momento."},
        {"id": "pt_05", "text": "O meu número de rastreio é sete, dois, oito, quatro, um, nove, zero, seis, três.",
         "digits": "728419063"},
        {"id": "pt_06",
         "text": "O estafeta disse que deixou o pacote à porta ontem à tarde, mas eu estive em casa o dia todo "
                 "e ninguém apareceu, quero abrir uma reclamação."},
    ],
}


def pick_voice(entries: list[dict], lang: str) -> dict:
    """语种组内首只非克隆条目（moss_audio_ 前缀=用户克隆资产，语料只用官方目录音色）。"""
    for e in entries:
        if e["lang"] == lang and not e["id"].startswith("moss_audio_"):
            return e
    raise SystemExit(f"FAIL: 语种 {lang} 在 minimax-voices.ts 目录无非克隆条目")


def synth_mp3_boost(key: str, url: str, voice: str, text: str, *, language_boost: str) -> bytes:
    """t2a_v2 非流式 → mp3 字节（cache_minimax_auditions.synth_mp3 同款重试语义 +
    四语 language_boost 槽；SSRF 每请求前过 _url_ok，2054/401 立即失败不空转）。"""
    body = {
        "model": MODEL,
        "text": text,
        "stream": False,
        "language_boost": language_boost,
        "voice_setting": {"voice_id": voice, "speed": float(SPEED), "vol": 1, "pitch": 0},
        "audio_setting": {"sample_rate": SAMPLE_RATE, "format": "mp3", "channel": 1},
    }
    last = ""
    for attempt in range(RETRIES):
        if not _url_ok(url):
            raise RuntimeError(f"SSRF 护栏拒绝非白名单目标: {url}")
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
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
            raise RuntimeError(f"MiniMax 传输失败: {last}") from exc

        base_resp = payload.get("base_resp") or {}
        code = int(base_resp.get("status_code", -1))
        if code == 0:
            audio_hex = (payload.get("data") or {}).get("audio") or ""
            if not audio_hex:
                raise RuntimeError(f"MiniMax 返回空音频: {str(base_resp)[:200]}")
            try:
                data = bytes.fromhex(audio_hex)
            except ValueError as exc:
                raise RuntimeError("MiniMax 音频非 hex 编码") from exc
            if len(data) < 512:
                raise RuntimeError(f"音频过短（{len(data)} 字节），疑非有效 mp3")
            return data
        last = str(base_resp)
        if code == 2054:
            raise RuntimeError(f"MiniMax 2054 voice-not-exist（音色 {voice} 无效）")
        if code == 1002 and attempt < RETRIES - 1:  # RPM 限频
            time.sleep(10 * (attempt + 1))
            continue
        break
    raise RuntimeError(f"MiniMax base_resp={last}")


def mp3_to_wav16k(mp3_path: Path, wav_path: Path) -> None:
    """mp3 → 16k mono PCM16 wav（afconvert，corpus v2 同规格；仅 macOS）。"""
    subprocess.run(
        ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
         str(mp3_path), str(wav_path)],
        check=True,
    )


def dur_s(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as w:
        assert w.getframerate() == 16000 and w.getsampwidth() == 2 and w.getnchannels() == 1, (
            f"规格漂移（须 16k/16bit/mono）: {wav_path}")
        return round(w.getnframes() / w.getframerate(), 2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="四语（de/fr/ja/pt）豆包 ASR 实测语料渲染（MiniMax 云 TTS）")
    ap.add_argument("--lang", default="", choices=list(LANGS), help="只跑某语种")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--force", action="store_true", help="已存在也重渲（默认幂等跳过）")
    ap.add_argument("--dry-run", action="store_true", help="只列清单，不调 API")
    args = ap.parse_args(argv)

    out = Path(args.out)
    langs: tuple[str, ...] = (args.lang,) if args.lang else LANGS
    selected = {lg: CORPUS[lg] for lg in langs}

    entries = parse_catalog()
    voices = {lg: pick_voice(entries, lg) for lg in selected}
    if args.dry_run:
        for lg, items in selected.items():
            v = voices[lg]
            print(f"[{lg}] voice={v['id']} ({v['label']}) boost={LANG_BOOST[lg]} 共 {len(items)} 句")
            for it in items:
                print(f"  {it['id']}  {it['text'][:60]}")
        print(f"[dry-run] 目标目录 {out}")
        return 0

    url = endpoint()
    if not _url_ok(url):
        print(f"FAIL: 端点未过 SSRF 白名单: {url}", file=sys.stderr)
        return 2
    key, key_src = load_api_key()
    print(f"[env] endpoint={urllib.parse.urlsplit(url).netloc} key={key_src} "
          f"model={MODEL} 句数={sum(len(v) for v in selected.values())}")

    manifest: list[dict] = []
    old_manifest = out / "manifest.json"
    if old_manifest.is_file():
        # 保留全部旧行（--lang 单语重跑不得丢其它语种行），选中项随后按 id 替换。
        manifest = list(json.loads(old_manifest.read_text(encoding="utf-8")))

    ok = skipped = failed = 0
    failures: list[str] = []
    for lg in selected:
        voice = voices[lg]["id"]
        lang_dir = out / lg
        lang_dir.mkdir(parents=True, exist_ok=True)
        for n, it in enumerate(selected[lg], 1):
            wav_path = lang_dir / f"{n:02d}.wav"
            row = {"id": it["id"], "lang": lg, "text": it["text"],
                   "digits": it.get("digits"), "file": f"{lg}/{n:02d}.wav",
                   "voice": voice}
            if wav_path.is_file() and not args.force:
                row["dur_s"] = dur_s(wav_path)
                skipped += 1
            else:
                t0 = time.perf_counter()
                try:
                    data = synth_mp3_boost(key, url, voice, it["text"],
                                           language_boost=LANG_BOOST[lg])
                except RuntimeError as exc:
                    print(f"[fail] {it['id']}: {exc}", file=sys.stderr)
                    failures.append(it["id"])
                    failed += 1
                    continue
                tmp_mp3 = wav_path.with_suffix(".mp3.tmp")
                tmp_mp3.write_bytes(data)
                try:
                    mp3_to_wav16k(tmp_mp3, wav_path)
                finally:
                    tmp_mp3.unlink(missing_ok=True)
                row["dur_s"] = dur_s(wav_path)
                dt = time.perf_counter() - t0
                print(f"[ok] {it['id']} {row['dur_s']:4.1f}s {len(data)}B mp3 {dt:4.1f}s "
                      f"-> {wav_path.relative_to(out)}")
                ok += 1
                time.sleep(PACE_S)
            manifest = [m for m in manifest if m["id"] != it["id"]] + [row]

    manifest.sort(key=lambda m: (LANGS.index(m["lang"]), m["file"]))
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] ok={ok} skip={skipped} fail={failed} -> {out}（manifest.json {len(manifest)} 条）")
    if failures:
        print("[failures] " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
