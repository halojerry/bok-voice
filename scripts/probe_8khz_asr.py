#!/usr/bin/env python3
"""8kHz 窄带重验探针（spec 2026-09-13 §6 前置门；P1.5 Task 5）。

真中继上线前必须过这道门：运营商音频是 PCMU/PCMA 8kHz（电话频带 ~3.4kHz），
我们全套栈按 16kHz 调过——句级提交/热词/数字保护/拆句组装都要在窄带话音下
重测一遍。本探针把 mock_callee 的窄带档（`downsample_to_narrowband`）接进
**真链路**跑一遍：

  CP 建最小战役（单对象单腿）→ campaign loop 起拨 → agent 读 dial 块的
  `narrowband` → mock_callee 窄带档把 TTS 客户话音过 16k→8k→16k 推流 →
  agent 的真 ASR 转写 → `GET /api/calls/{id}/turns` 取 customer 转写 →
  difflib 字符准确率 + 号码句逐位比对。

宽档（窄带关）= 基线（既有产品行为）；窄档（窄带开）= 运营商 8kHz 线段的表现。
同一批台词、同一音色、同一话术，只差窄带开关，逐腿对照。ASR 有解码随机性，
默认跑 2 轮**取较好值**并在报告里注明轮次（单腿失败重试由轮次覆盖，不调台词凑分）。

结论行：`8KHZ_PROBE wide_acc=… narrow_acc=… digit_ok=… gate=PASS|FAIL`
（wide_acc/narrow_acc = 三语句腿的字符准确率均值；gate 判据 = 号码句窄带逐位
全对 + 三语句准确率 ≥90%）。退出码 0=门禁过。

前置：`python tools/bok.py serve`（CP :8000 / agent :8081 / LiveKit :7880 /
TTS :8788 / ASR :8787）；`ps aux | grep agent_runtime` 必须 0（殭尸铁律）。
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEAK_INTERVAL_S = 8.0   # 客户句间隔（mock callee 还会等 AI 讲完再出声）
DEFAULT_CALL_TIMEOUT_S = 150.0   # 单腿通话墙钟上限（含响铃/开场/台词/收线）
DEFAULT_FUSE_S = 90              # 窄带探针期临时把 mock 时长保险丝压到 90s
POLL_INTERVAL_S = 2.0
RESULT_PREFIX = "8KHZ_PROBE"
# 对象名前缀命中 agent 心跳豁免族（`^(E2E-|soak\d*-?|并发|LOAD-|边角-|多轮-|probe)`）
# ——心跳/收线音频会插进客户话音之间，污染转写断言窗。
OBJECT_PREFIX = "probe8k"
TEMPLATE_PREFIX = "8K-PROBE"


# ── 真链路骨架复用 e2e_campaign.py（起栈前置/建战役/轮询姿势同源，避免漂移）──

def _load_e2e() -> Any:
    path = Path(__file__).with_name("e2e_campaign.py")
    spec = importlib.util.spec_from_file_location("e2e_campaign_probe", path)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件缺失即环境坏
        raise SystemExit(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ec = _load_e2e()


# ── 探针台词集（宽/窄同词；每句单口气 <10 字，防 vad-pause 劈轮）───────────

@dataclass(frozen=True)
class Leg:
    """一条腿 = 一个对象 + 一通 mock 通话 + 一组台词。"""

    name: str
    language: str            # zh|cantonese|en（TTS/ASR 同一通话语言）
    lines: list[str]
    digits: str = ""         # 非空=该腿含号码句，逐位比对目标
    hotwords: str = ""       # 话术模板热词（ASR 软偏置，随模板下发）
    sentence: bool = True    # 计入「三语句」门禁的句子腿


# 台词集（brief Task 5 指定）：zh 短句 / cantonese 号码句拆两句 / en 短句 /
# 热词句；每腿末尾补一句客户道别——agent 收到道别走收尾链，通话正常收线
# （不靠 mock 时长保险丝兜底，否则每腿要干等 60s）。
DEFAULT_LEGS: list[Leg] = [
    Leg("zh-sentence", "zh", ["你好我是快递公司的专员", "好的再见"]),
    Leg("cantonese-number", "cantonese", ["我WhatsApp係", "六四三二零一一一", "好嘅再見"],
        digits="64320111", sentence=False),
    Leg("en-sentence", "en", ["Let me check that for you", "okay bye bye"]),
    Leg("hotword", "zh", ["拼多多京东下单", "好的再见"], hotwords="拼多多,京东,下单"),
]

# 探针话术（每语言一套最小 2 步）：开场步刻意**不自报「快递公司专员」**——zh 腿的
# 客户台词就是这句，开场若同句会撞回声自听守卫（AI 讲嘢中收到 ≥0.9 相似的客户轮
# 整轮丢弃）→ 首句结构性消失。开场只问「方便唔方便」，与台词无重叠。
PROBE_STEPS: dict[str, list[dict[str, Any]]] = {
    "zh": [
        {"goal": "开场：确认对方现在方便讲电话", "ref": "你好，请问现在方便讲电话吗？", "say": True},
        {"goal": "向客户索取他自己的WhatsApp号码，方便专员对接",
         "ref": "方便的话，可以读一下你的WhatsApp号码吗？", "say": True},
    ],
    "cantonese": [
        {"goal": "開場：確認對方而家方便講電話", "ref": "你好，請問而家方便講電話嗎？", "say": True},
        {"goal": "向客戶索取佢自己嘅WhatsApp號碼，方便專員對接",
         "ref": "方便嘅話，可以講一下你嘅WhatsApp號碼嗎？", "say": True},
    ],
    "en": [
        {"goal": "Opening: check if now is a good time to talk",
         "ref": "Hello, is now a good time to talk?", "say": True},
        {"goal": "Ask the customer for their own WhatsApp number",
         "ref": "Could you read out your WhatsApp number, please?", "say": True},
    ],
}


# ── 转写 → 准确率（纯函数）────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[\s。，,．.！!？?～~—\-、；;：:'\"“”‘’()（）\[\]{}<>《》/\\|]+")
_EN_DIGIT_WORDS_RE = re.compile(
    r"\b(zero|oh|one|two|three|four|five|six|seven|eight|nine)\b", re.IGNORECASE)
_EN_DIGIT_MAP = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3",
                 "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
                 "nine": "9"}
_CN_DIGIT_MAP = {"零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3",
                 "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
_FULLWIDTH_DIGITS = {chr(0xFF10 + i): str(i) for i in range(10)}


def normalize_text(text: str) -> str:
    """去空白与标点，只留可比对内容（ASR 输出无标点，台词有）。"""
    return _PUNCT_RE.sub("", str(text or ""))


def digits_of(text: str) -> str:
    """汉字/英文数字词/全角数字 → 数字串（照 flow._digit_normalize 语义）。"""
    lowered = _EN_DIGIT_WORDS_RE.sub(lambda m: _EN_DIGIT_MAP[m.group(1).lower()],
                                     str(text or "").lower())
    out = []
    for ch in lowered:
        if ch in _CN_DIGIT_MAP:
            out.append(_CN_DIGIT_MAP[ch])
        elif ch in _FULLWIDTH_DIGITS:
            out.append(_FULLWIDTH_DIGITS[ch])
        elif ch.isdigit():  # ASCII 数字（含 ASR 直出阿拉伯数字）
            out.append(ch)
    return "".join(out)


def char_accuracy(transcript: str, script: str) -> float:
    """difflib 字符准确率（0-1）；台词典为空视为 0（防除零假绿）。"""
    want = normalize_text(script)
    if not want:
        return 0.0
    return difflib.SequenceMatcher(None, normalize_text(transcript), want).ratio()


def script_text(lines: list[str]) -> str:
    """台词集拼成一段（与「客户轮转写拼接」同口径比对）。"""
    return "".join(str(x) for x in lines)


def customer_transcripts(turns: list[dict]) -> list[str]:
    """该通通话的 customer 轮转写（按时序，非空）。"""
    out = []
    for t in turns:
        if str(t.get("speaker") or "") != "customer":
            continue
        text = str(t.get("transcript") or "").strip()
        if text:
            out.append(text)
    return out


# ── CP 操作 ──────────────────────────────────────────────────────────────

_SEQ = [0]  # 对象电话尾号序号（同一秒内建多个对象时仍唯一）


def ensure_template(language: str, hotwords: str) -> str:
    """取/建该语言探针话术（同名复用，不重复建）。"""
    name = f"{TEMPLATE_PREFIX}-{language}"
    try:
        for tpl in ec._api("GET", f"/api/templates?account_id={ec.ACCOUNT_ID}",
                           timeout=15).json():
            if str(tpl.get("name") or "") == name:
                return str(tpl.get("id") or "")
    except Exception:  # noqa: BLE001 - 查询失败退直接建
        pass
    tpl = ec._api("POST", "/api/templates", json={
        "account_id": ec.ACCOUNT_ID, "name": name, "language": language,
        "opening": "", "core": "", "objection": "", "closing": "",
        "steps_json": json.dumps(PROBE_STEPS[language], ensure_ascii=False),
        "hotwords": hotwords,
    }, timeout=15).json()
    return str(tpl.get("id") or "")


def create_object(leg: Leg, template_id: str, tag: str, seq: int) -> tuple[str, str]:
    """建探针对象；返回 `(object_id, phone)`——phone 用来算 mock 客户的 identity。"""
    ts = int(time.time() * 1000) % 10000
    phone = f"+8529{ts:04d}{seq:02d}"
    obj = ec._api("POST", f"/api/objects?account_id={ec.ACCOUNT_ID}", json={
        "display_name": f"{OBJECT_PREFIX}-{leg.name}-{tag}-{ts}",
        "language": leg.language,
        "phone": phone,
        "template_id": template_id,
        "contact_channel": "whatsapp",
    }, timeout=15).json()
    return str(obj.get("id") or ""), phone


def start_campaign(leg: Leg, object_id: str, template_id: str,
                   *, narrowband: bool, speak_interval_s: float, tag: str) -> str:
    camp = ec._api("POST", "/api/campaigns", json={
        "account_id": ec.ACCOUNT_ID,
        "name": f"{OBJECT_PREFIX}-{leg.name}-{tag}",
        "object_ids": [object_id], "template_id": template_id, "persona_id": "",
        "language": leg.language, "gap_seconds": 0,
        "scenarios": {object_id: "answer"},
        "scripts": {object_id: list(leg.lines)},
        "mock_speak_interval_s": speak_interval_s,
        "narrowband": narrowband,
    }, timeout=20).json()
    campaign_id = str(camp.get("id") or "")
    if campaign_id:
        ec._api("POST", f"/api/campaigns/{campaign_id}/start", timeout=15)
    return campaign_id


def wait_call(campaign_id: str, timeout_s: float) -> dict:
    """轮询该单腿战役至终态，返回 item（含 call_id/status/last_error）。"""
    deadline = time.perf_counter() + timeout_s
    item: dict = {}
    while time.perf_counter() < deadline:
        try:
            detail = ec._api("GET", f"/api/campaigns/{campaign_id}", timeout=15).json()
        except Exception:  # noqa: BLE001 - CP 抖动重试
            time.sleep(POLL_INTERVAL_S)
            continue
        items = detail.get("items") or []
        if items:
            item = items[0]
        if str(detail.get("status") or "") == "done" or (
                item and str(item.get("status") or "") not in ("pending", "dialing", "in_call")):
            return item
        time.sleep(POLL_INTERVAL_S)
    return item


# ── mock callee 窄带档真伪核验（不许假绿：窄腿必须真的走了窄带档）─────────

def _mock_callee_log_path() -> Path:
    return ROOT / "runtime" / "logs" / "mock-callee.log"


def _log_size() -> int:
    try:
        return _mock_callee_log_path().stat().st_size
    except OSError:
        return 0


def _log_new_text(offset: int) -> str:
    path = _mock_callee_log_path()
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def narrowband_evidence(offset: int, identity: str) -> bool:
    """该通通话窗内是否出现**本腿 identity** 的窄带档自报（`narrowband=1 identity=…`）。

    强绑定：`identity` 是 mock 客户在房间里的唯一身份（`sip-mock-<号码>`，agent 侧
    按同一 identity 认它）。只按时间窗找 `narrowband=1` 会被并发/错位归属骗过——
    「本腿窄带档真的跑了」必须由本腿自己的日志行作证。
    """
    if not identity:
        return False
    return any(
        "narrowband=1" in line and f"identity={identity}" in line
        for line in _log_new_text(offset).splitlines()
    )


# ── 一轮（宽/窄各一遍全部台词腿）────────────────────────────────────────

@dataclass
class Record:
    leg: str
    language: str
    mode: str            # wide|narrow
    rnd: int
    lines: list[str]
    transcript: str
    acc: float
    sentence: bool = True   # 计入「三语句」门禁分母（号码腿单列逐位比对）
    digits_want: str = ""
    digits_got: str = ""
    digit_ok: bool = False
    call_id: str = ""
    turn_count: int = 0
    status: str = ""
    narrowband_marker: bool = False
    notes: list[str] = field(default_factory=list)


def run_leg(leg: Leg, mode: str, rnd: int, *, speak_interval_s: float,
            call_timeout_s: float) -> Record:
    template_id = ensure_template(leg.language, leg.hotwords)
    tag = f"{mode}-r{rnd}"
    object_id, phone = create_object(leg, template_id, tag, seq=_SEQ[0])
    _SEQ[0] += 1
    # mock 客户在房间里的 identity（agent 侧 `_dial_mock` 按同一 identity 认它）——
    # 窄带档核验按它逐腿强绑定。
    mock_identity = f"sip-mock-{phone}"
    rec = Record(leg=leg.name, language=leg.language, mode=mode, rnd=rnd,
                 lines=list(leg.lines), transcript="", acc=0.0,
                 sentence=leg.sentence, digits_want=leg.digits)
    if not object_id:
        rec.notes.append("建对象失败")
        return rec
    log_offset = _log_size()
    campaign_id = start_campaign(leg, object_id, template_id, narrowband=(mode == "narrow"),
                                 speak_interval_s=speak_interval_s, tag=tag)
    if not campaign_id:
        rec.notes.append("建战役失败")
        return rec
    t0 = time.perf_counter()
    item = wait_call(campaign_id, call_timeout_s)
    rec.status = str(item.get("status") or "")
    rec.call_id = str(item.get("call_id") or "")
    rec.notes.append(f"wall={time.perf_counter() - t0:.1f}s")
    if item.get("last_error"):
        rec.notes.append(f"last_error={item['last_error']}")
    rec.narrowband_marker = narrowband_evidence(log_offset, mock_identity)
    if mode == "narrow" and not rec.narrowband_marker:
        rec.notes.append(f"窄带档未见本腿自报（identity={mock_identity}）")
    if rec.call_id:
        try:
            turns = ec._api("GET", f"/api/calls/{rec.call_id}/turns", timeout=15).json()
        except Exception as exc:  # noqa: BLE001 - 取转写失败照记 0 分
            turns = []
            rec.notes.append(f"turns 拉取失败 {type(exc).__name__}")
        cust = customer_transcripts(turns if isinstance(turns, list) else [])
        rec.turn_count = len(cust)
        rec.transcript = "".join(cust)
        rec.acc = char_accuracy(rec.transcript, script_text(leg.lines))
        rec.digits_got = digits_of(rec.transcript)
        if leg.digits:
            rec.digit_ok = leg.digits in rec.digits_got
    else:
        rec.notes.append("无 call_id")
    try:
        ec._api("DELETE", f"/api/objects/{object_id}", timeout=15)
    except Exception:  # noqa: BLE001 - 清理失败不影响判定
        pass
    return rec


def print_record(rec: Record) -> None:
    print(f"  [{rec.mode:6s}] {rec.leg:17s} acc={rec.acc:.3f} turns={rec.turn_count} "
          f"status={rec.status or '?':8s} "
          + (f"digits={rec.digits_got or '-'} "
             f"digit_ok={'Y' if rec.digit_ok else 'N'} " if rec.digits_want else "")
          + f"tr={rec.transcript[:60]!r}"
          + (f"  ({'; '.join(rec.notes)})" if rec.notes else ""), flush=True)


# ── 汇总 ─────────────────────────────────────────────────────────────────

def sentence_mean(records: list[Record], mode: str, rnd: int) -> float:
    accs = [r.acc for r in records if r.mode == mode and r.rnd == rnd and r.sentence]
    return sum(accs) / len(accs) if accs else 0.0


def digit_ok_any(records: list[Record], mode: str) -> bool:
    hits = [r for r in records if r.mode == mode and r.digits_want]
    return any(r.digit_ok for r in hits)


def best_round(records: list[Record], mode: str, rounds: int) -> int:
    """该档取句准确率最高的轮次（并列取先跑的轮次）。"""
    return max(range(1, rounds + 1), key=lambda r: (sentence_mean(records, mode, r), -r))


def summary_table(records: list[Record], rounds: int) -> None:
    print("\n=== 对照表（宽档 vs 窄档）===", flush=True)
    header = (f"{'leg':18s} {'lang':10s} {'wide_acc':>8s} {'narrow_acc':>10s} "
              f"{'Δ':>7s}  {'wide_tr':32s} {'narrow_tr':32s}")
    print(header, flush=True)
    for leg in {r.leg: r for r in records}.values():
        w = [r for r in records if r.leg == leg.leg and r.mode == "wide"]
        n = [r for r in records if r.leg == leg.leg and r.mode == "narrow"]
        if not w or not n:
            continue
        wr = max(w, key=lambda r: r.acc)
        nr = max(n, key=lambda r: r.acc)
        print(f"{leg.leg:18s} {leg.language:10s} {wr.acc:8.3f} {nr.acc:10.3f} "
              f"{nr.acc - wr.acc:+7.3f}  {wr.transcript[:32]:32s} {nr.transcript[:32]:32s}",
              flush=True)


def conclude(records: list[Record], rounds: int) -> tuple[int, dict[str, Any]]:
    wide_r = best_round(records, "wide", rounds)
    narrow_r = best_round(records, "narrow", rounds)
    wide_acc = sentence_mean(records, "wide", wide_r)
    narrow_acc = sentence_mean(records, "narrow", narrow_r)
    digit_ok = digit_ok_any(records, "narrow")
    digit_want = next((r.digits_want for r in records if r.digits_want), "")
    digit_got = ""
    for r in records:
        if r.mode == "narrow" and r.digits_want and r.digit_ok:
            digit_got = r.digits_got
            break
    if not digit_got:
        got = [r.digits_got for r in records if r.mode == "narrow" and r.digits_want]
        digit_got = max(got, key=len) if got else ""
    # 真伪核验是硬条件：窄腿必须每腿都有 mock_callee 的窄带档日志自报，否则测得
    # 的根本不是窄带话音（实测踩过——栈里混进旧 CP 时窄带字段被静默忽略，宽档数据
    # 会被当成窄档结论）。
    narrow_legs = [r for r in records if r.mode == "narrow"]
    marker_ok = bool(narrow_legs) and all(r.narrowband_marker for r in narrow_legs)
    # 号码腿没跑（--legs 子集）时只按句准确率判——别把「没测」当「不过」。
    accuracy_ok = narrow_acc >= 0.90
    gate = bool(marker_ok and accuracy_ok and (digit_ok if digit_want else True))
    print(f"\n=== 结论（宽档第 {wide_r} 轮 / 窄档第 {narrow_r} 轮取较好值）===", flush=True)
    if digit_want:
        print(f"  号码句窄带逐位：want={digit_want} got={digit_got or '-'} "
              f"digit_ok={'PASS' if digit_ok else 'FAIL'}", flush=True)
    print(f"  三语句窄带字符准确率 = {narrow_acc:.3f}（门禁 ≥0.90）", flush=True)
    print(f"  窄带档真伪核验：narrow 腿日志自报 = "
          f"{sum(1 for r in narrow_legs if r.narrowband_marker)}/{len(narrow_legs)}"
          f"{'' if marker_ok else ' ← 无效：窄带档没真跑，数据当废'}", flush=True)
    print(f"{RESULT_PREFIX} wide_acc={wide_acc:.3f} narrow_acc={narrow_acc:.3f} "
          f"digit_ok={1 if digit_ok else 0} gate={'PASS' if gate else 'FAIL'}", flush=True)
    payload = {
        "wide_acc": round(wide_acc, 4), "narrow_acc": round(narrow_acc, 4),
        "wide_round": wide_r, "narrow_round": narrow_r,
        "digit_ok": digit_ok, "digit_want": digit_want, "digit_got": digit_got,
        "narrowband_marker_ok": marker_ok,
        "gate": "PASS" if gate else "FAIL",
        "rounds": rounds,
        "records": [vars(r) for r in records],
    }
    return (0 if gate else 1), payload


# ── main ─────────────────────────────────────────────────────────────────

def _snapshot_settings() -> dict:
    try:
        return ec._api("GET", "/api/settings", timeout=10).json()
    except Exception:  # noqa: BLE001
        return {}


def _put_settings(full: dict) -> None:
    """PUT 全量覆盖语义：body 需带齐五段（照 e2e_campaign 姿势）。"""
    body = {k: full.get(k) for k in ("asr", "llm", "tts", "vad", "policy")}
    body["sip"] = dict(full.get("sip") or {})
    ec._api("PUT", "/api/settings", json=body, timeout=15)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="8kHz 窄带重验探针（真链路）")
    parser.add_argument("--rounds", type=int, default=2, help="跑几轮取较好值（默认 2）")
    parser.add_argument("--legs", default="",
                        help="只跑指定腿（逗号分隔，默认全部）")
    parser.add_argument("--speak-interval", type=float, default=DEFAULT_SPEAK_INTERVAL_S)
    parser.add_argument("--call-timeout", type=float, default=DEFAULT_CALL_TIMEOUT_S)
    parser.add_argument("--json-out", default="", help="把原始数据写到该 JSON 路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    legs = DEFAULT_LEGS
    if args.legs.strip():
        want = {x.strip() for x in args.legs.split(",") if x.strip()}
        legs = [leg for leg in DEFAULT_LEGS if leg.name in want]
        if not legs:
            print(f"未知腿名：{args.legs}", flush=True)
            return 2

    if not ec.preflight():
        print("\n[preflight] 前置失败——栈未起/非 mock 档/有殭尸 worker，直接退出",
              flush=True)
        return 1

    print(f"\n[probe] legs={[leg.name for leg in legs]} rounds={args.rounds} "
          f"speak_interval={args.speak_interval}s", flush=True)
    original = _snapshot_settings()
    # 单腿通话墙钟上限：mock 档靠 agent 侧时长保险丝兜底收线（不设则 600s）。
    # 探针期压到 60s，跑完恢复原值（E2E 台词集长，改动不外溢）。
    try:
        sip = dict(original.get("sip") or {})
        sip["mode"] = "mock"
        sip["max_call_duration_s"] = DEFAULT_FUSE_S
        tuned = dict(original)
        tuned["sip"] = sip
        _put_settings(tuned)
        print(f"[setup] sip.mode=mock max_call_duration_s={DEFAULT_FUSE_S}s（跑完恢复）",
              flush=True)
    except Exception as exc:  # noqa: BLE001 - 设置写不进则照跑（有 farewell 收线）
        print(f"[setup] settings 调整跳过：{type(exc).__name__}: {exc}", flush=True)

    records: list[Record] = []
    try:
        for rnd in range(1, max(1, args.rounds) + 1):
            for mode in ("wide", "narrow"):
                print(f"\n--- 第 {rnd} 轮 {mode} 档 ---", flush=True)
                for leg in legs:
                    rec = run_leg(leg, mode, rnd, speak_interval_s=args.speak_interval,
                                  call_timeout_s=args.call_timeout)
                    records.append(rec)
                    print_record(rec)
    finally:
        if original:
            try:
                _put_settings(original)
                print("\n[teardown] settings 已恢复", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"\n[teardown] settings 恢复失败：{exc!r}", flush=True)

    summary_table(records, max(1, args.rounds))
    code, payload = conclude(records, max(1, args.rounds))
    if args.json_out:
        try:
            Path(args.json_out).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[out] 原始数据 → {args.json_out}", flush=True)
        except OSError as exc:
            print(f"[out] 写 JSON 失败：{exc!r}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
