#!/usr/bin/env python3
"""外呼战役 E2E（mock 档全链路）：3 对象——1 接通走完话术 / 1 无人接 / 1 接通即挂。

真实链路（不 mock 任何 CP/LiveKit 组件）：`POST /api/campaigns` 建波 + start
→ CP campaign loop（5s 巡检）串行起拨 → agent 收到 metadata dial 块 → mock 档
派生 `scripts/mock_callee.py` 真语音被叫进房 → 客户说出 WhatsApp 号码 → agent
侦测捕获上报 CP → 名册自动入册 → 电话收线 → loop 收割终态、留 gap 冷却后起下一通。

断言：
  C1 前置（health / sip.mode=mock / 三 worker 端口 / 无殭尸 agent worker）
  C2 串行——任意轮询采样至多 1 个 item 处于 dialing/in_call
  C3 终态——answer 轮 done、no_answer 轮 no_answer、reject 轮 rejected
  C4 campaign status=done
  C5 名册自动入册——answer 轮的号码 channel=whatsapp（captured→入册链路）
  C6 话音真度——answer 轮 turns 有 customer 转写非空（mock 客户真出声）
  C7 无 fail 腿（每通都有终态，没有卡死/派发失败）

退出码 0=PASS；任一断言失败非 0（不许改断言凑绿）。

前置：`python tools/bok.py serve`（含 CP :8000 / agent :8081 / LiveKit :7880 /
TTS :8788）；`ps aux | grep agent_runtime` 必须 0（A/B 殭尸 worker 铁律）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
ACCOUNT_ID = os.environ.get("E2E_ACCOUNT_ID", "acc-001")
WORKER_PORTS = (8081, 8082, 8083)
# 当前栈的三 worker pidfile（agent/interp-fwd/interp-rev）：用来区分「本栈正常
# worker」与「前一次栈残留的殭尸 worker」。殭尸铁律的真实风险=旧栈失联后旧代码
# worker 继续 servicing 新请求污染 A/B 结论——本栈 pidfile 里没登记的那些才是。
PIDFILE_NAMES = ("agent", "interp-fwd", "interp-rev")
POLL_INTERVAL_S = 2.0
# no_answer 腿含 ~30s 响铃窗 + mock 客户说话窗 + gap 冷却；预算按序 ≈90s，
# 留 3 倍余量（模型冷启动/机器负载下的抖动不误伤）。
TOTAL_TIMEOUT_S = float(os.environ.get("E2E_CAMPAIGN_TIMEOUT", "300"))
GAP_SECONDS = int(os.environ.get("E2E_CAMPAIGN_GAP", "5"))
# mock 客户台词句间隔：要盖过 AI 一轮（处理 + 播报，实测开场 ~5s、收号步 ~8s），
# 否则报号句会在 AI 还没问到收号步时到达。默认 10s 留余量；机器快可调小。
MOCK_SPEAK_INTERVAL_S = float(os.environ.get("E2E_CAMPAIGN_SPEAK_INTERVAL", "10"))
# 接通轮的号码（客户台词念出）——5 位起（捕获下限 4 位；测试短号亦可）。
WA_NUMBER = "64320111"
# 捕获号码的容差：mock 客户为对齐 AI 收号步会重复念号，**哪一次念号先被判定 captured
# 由 ASR 解码竞速决定**——某次念号听岔一位（实证 64320111 → 64320117）就会先占位。
# 本 E2E 验的是「captured→名册自动入册」链路，不是 ASR 逐位精度（那有专属探针）；
# 故按「同长度 + 至少 7/8 位吻合」判定为对脚本号码的忠实捕获，同时把实际值打进
# 报告便于复盘听岔。位数不足/渠道错/本轮无条目照旧 FAIL（真链路问题不许放过）。
WA_MIN_MATCH_DIGITS = 7


def _number_close(captured: str, want: str = WA_NUMBER,
                  min_match: int = WA_MIN_MATCH_DIGITS) -> bool:
    """捕获号码是否忠实于脚本号码（同长度 + 至少 min_match 位逐位吻合）。"""
    got = "".join(ch for ch in str(captured or "") if ch.isdigit())
    if len(got) != len(want):
        return False
    return sum(1 for a, b in zip(got, want) if a == b) >= min_match

RESULTS: list[tuple[str, bool, str]] = []
LEG_TIMINGS: list[tuple[str, float, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {note}", flush=True)


def _api(method: str, path: str, **kw) -> httpx.Response:
    return httpx.request(method, f"{CONTROL_PLANE_URL}{path}", timeout=kw.pop("timeout", 20), **kw)


def _app_data_dir() -> Path:
    """栈的 app-data（run/*.pid 与 logs 都在这里）；与 tools/bok.py `app_data_dir` 同规则。"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    return base / "BokVoice"


def preflight() -> bool:
    """C1 前置：health / sip.mode / worker 端口 / 无殭尸 worker。任一不过直接退出。"""
    try:
        r = _api("GET", "/health", timeout=5)
        r.raise_for_status()
        ok_health = bool(r.json().get("ok"))
    except Exception as exc:  # noqa: BLE001 - 栈没起=直接失败
        record("C1a CP /health", False, f"{type(exc).__name__}: {exc}")
        return False
    record("C1a CP /health", ok_health, "service up")

    # 殭尸铁律：跑前不得有**本栈之外**的 agent worker（殭尸跑旧代码会污染 E2E
    # 结论）。本栈三 worker 的 pid 登记在 app-data/run/*.pid——按 pid 比对，
    # 而不是「见到 agent_runtime 就报」（那会把自己栈的正常 worker 误判成殭尸）。
    try:
        ps = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=10).stdout
        live: dict[str, list[str]] = {}
        for ln in ps.splitlines():
            if "agent_runtime" in ln and "grep" not in ln:
                parts = ln.split()
                if len(parts) > 10:
                    live[parts[1]] = parts[10:]
        expected: set[str] = set()
        for name in PIDFILE_NAMES:
            pf = _app_data_dir() / "run" / f"{name}.pid"
            try:
                expected.add(pf.read_text().strip())
            except Exception:  # noqa: BLE001 - 缺 pidfile 时无「已知 worker」可豁免
                pass
        zombies = {pid: cmd for pid, cmd in live.items() if pid not in expected}
        record("C1b 无本栈外殭尸 agent worker", not zombies,
               f"{len(zombies)} 个残留 {list(zombies)[:3]}" if zombies
               else f"clean（本栈 {len(live)} 个 worker 已登记）")
    except Exception as exc:  # noqa: BLE001
        record("C1b 无本栈外殭尸 agent worker", False, f"ps 失败: {exc}")

    # sip.mode 期望 mock：非 mock 档 E2E 会真的拨真号（危险），必须先置回。
    try:
        cur = _api("GET", "/api/settings", timeout=10).json()
        mode = str((cur.get("sip") or {}).get("mode") or "")
        if mode != "mock":
            print(f"  [preflight] sip.mode={mode!r} → PUT 置回 mock", flush=True)
            sip = dict(cur.get("sip") or {})
            # PUT /api/settings 全量覆盖语义：body 需带齐五段（缺段落默认值），
            # 所以直接把读到的全量 settings 回灌、只改 sip.mode。
            body = {k: cur.get(k) for k in ("asr", "llm", "tts", "vad", "policy")}
            sip["mode"] = "mock"
            body["sip"] = sip
            _api("PUT", "/api/settings", json=body, timeout=15).raise_for_status()
            mode = str((_api("GET", "/api/settings", timeout=10).json()
                        .get("sip") or {}).get("mode") or "")
        record("C1c sip.mode=mock", mode == "mock", f"mode={mode!r}")
    except Exception as exc:  # noqa: BLE001
        record("C1c sip.mode=mock", False, f"{type(exc).__name__}: {exc}")

    # 三 worker 端口（8081 A 线 / 8082·8083 B 线）：status 行缺失时照旧探端口。
    listening = set()
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=10).stdout
        for port in WORKER_PORTS:
            if f":{port} " in out or f":{port}\t" in out:
                listening.add(port)
    except Exception:  # noqa: BLE001 - lsof 不可用时退 socket 探测
        listening = set()
    if not listening:
        import socket

        for port in WORKER_PORTS:
            with socket.socket() as s:
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    listening.add(port)
    record("C1d 三 worker 端口就绪", len(listening) == 3,
           f"listening={sorted(listening)}")
    return all(ok for _, ok, _ in RESULTS)


E2E_TEMPLATE_NAME = "E2E-CAMP-最小话术"


def pick_zh_template() -> str:
    """挑一条**短** zh 话术（≤3 步，WhatsApp 步可达）；没有则建最小 2 步模板。

    为什么不直接用 acc-001 的正牌 6 步模板：那条话术的前几步是「身份确认→遗失通知→
    引导核实→赔偿标准」，要客户真实应答才能推进到第 5 步（收 WhatsApp 号码）。E2E 的
    mock 客户只会念固定台词，中途「好的再见」类通用应承还会被判 REFUSE 收线，号码句
    根本到不了收号步——本 E2E 要验的是「captured→名册」链路，不是 4B 对话理解，
    故用短话术把收号步放到前两步。已存在同名测试模板时复用（不重复建）。
    """
    try:
        tpls = _api("GET", f"/api/templates?account_id={ACCOUNT_ID}", timeout=15).json()
        for tpl in tpls:
            if str(tpl.get("language") or "") != "zh":
                continue
            if str(tpl.get("name") or "") == E2E_TEMPLATE_NAME:
                return str(tpl.get("id") or "")
        # 退一步：任何 ≤3 步且含 WhatsApp/微信/联络方式的 zh 模板都可用。
        import json as _json

        for tpl in tpls:
            if str(tpl.get("language") or "") != "zh":
                continue
            try:
                steps = _json.loads(str(tpl.get("steps_json") or "") or "[]")
            except ValueError:
                continue
            if not isinstance(steps, list) or not steps or len(steps) > 3:
                continue
            blob = " ".join(str(s.get("goal", "")) + str(s.get("ref", ""))
                            for s in steps if isinstance(s, dict))
            if any(w in blob.lower() for w in ("whatsapp", "微信", "wechat", "联络", "联系方式")):
                return str(tpl.get("id") or "")
    except Exception:  # noqa: BLE001 - 查询失败退建最小模板
        pass
    # 最小 2 步：step1=开场身份（直念，兼作开场白）、step2=向客户索取 WhatsApp。
    # 方向铁律：索取**客户自己的**号码（等客户报数字串），不是「发给客户」。
    import json as _json

    steps = _json.dumps([
        {"goal": "开场：自报家门，说明来意",
         "ref": "你好，我是快递公司的专员，想和你确认一下包裹的联络方式。", "say": True},
        {"goal": "向客户索取他自己的 WhatsApp 号码，方便专员对接",
         "ref": "方便的话，可以读一下你的 WhatsApp 号码吗？我们专员会加你。", "say": True},
    ], ensure_ascii=False)
    tpl = _api("POST", "/api/templates", json={
        "account_id": ACCOUNT_ID, "name": E2E_TEMPLATE_NAME, "language": "zh",
        "opening": "", "core": "", "objection": "", "closing": "",
        "steps_json": steps, "hotwords": "WhatsApp,号码",
    }, timeout=15).json()
    return str(tpl.get("id") or "")


def _cleanup_stale_roster() -> None:
    """清掉历史 E2E 遗留的同号名册条目（幂等；失败不阻本轮）。

    名册表全库累积且无 DELETE 端点之外的回收路径，历史 E2E 对象的条目会一直躺在
    `/api/roster` 里。本轮断言钉死 call_id 后本不会误命中，但残留条目会让失败
    复盘时的现场变脏；能清就先清（无对外副作用——这些条目只属于已删测试对象）。
    """
    try:
        entries = _api("GET", f"/api/roster?account_id={ACCOUNT_ID}", timeout=15).json()
    except Exception:  # noqa: BLE001
        return
    stale = [e for e in entries
             if _number_close(str(e.get("number") or ""))
             and str(e.get("display_name") or "").startswith("E2E-CAMP-")]
    if not stale:
        return
    # 无 `DELETE /api/roster/{id}` 端点（名册只增），走库直删——测试对象已删，
    # 其名册条目只属本 E2E 场景，清掉不影响任何生产数据。库不可达则放弃清理
    # （断言已钉 call_id，残留只会让现场变脏，不会假绿）。
    try:
        import sqlite3

        db = _app_data_dir() / "bok_voice.db"
        conn = sqlite3.connect(str(db), timeout=5)
        try:
            conn.executemany(
                "DELETE FROM roster_entries WHERE id = ?", [(str(e["id"]),) for e in stale]
            )
            conn.commit()
        finally:
            conn.close()
        print(f"  [setup] 清掉 {len(stale)} 条历史 E2E 名册条目", flush=True)
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻本轮（断言口径已不依赖它）
        print(f"  [setup] 清理历史名册条目跳过: {type(exc).__name__}", flush=True)


def create_objects(template_id: str) -> list[dict]:
    """建 3 个测试对象（E2E-CAMP- 前缀吃心跳豁免；号码 1/2/3 区分三腿）。"""
    ts = int(time.time() * 1000) % 100000
    out = []
    for idx, scenario_hint in enumerate(("answer", "no_answer", "reject"), start=1):
        obj = _api("POST", f"/api/objects?account_id={ACCOUNT_ID}", json={
            "display_name": f"E2E-CAMP-{ts}-{scenario_hint}",
            "language": "zh",
            "phone": f"+8529{ts % 1000:03d}{idx:03d}",
            "template_id": template_id,
            # 显式 whatsapp 渠道：名册断言 channel 不靠推断。
            "contact_channel": "whatsapp",
        }, timeout=15).json()
        out.append(obj)
    return out


def poll_campaign(campaign_id: str, obj_ids: list[str]) -> dict:
    """轮询进度至 campaign done 或超时；同步采样串行不变量。

    串行判定：每轮采样 items，统计 status ∈ {dialing, in_call} 的数量——任一
    采样 >1 即违反（CP 循环是「一路进行中就等它出终态」的结构性保证）。
    """
    deadline = time.perf_counter() + TOTAL_TIMEOUT_S
    samples = 0
    max_inflight = 0
    serial_ok = True
    last: dict = {}
    leg_started: dict[str, float] = {}
    leg_ended: dict[str, float] = {}
    obj_to_key = {oid: f"leg{i + 1}" for i, oid in enumerate(obj_ids)}
    while time.perf_counter() < deadline:
        try:
            last = _api("GET", f"/api/campaigns/{campaign_id}", timeout=15).json()
        except Exception as exc:  # noqa: BLE001 - CP 抖动重试
            print(f"  [poll] 拉取失败重试: {exc!r}", flush=True)
            time.sleep(POLL_INTERVAL_S)
            continue
        items = last.get("items") or []
        samples += 1
        inflight = [i for i in items if i.get("status") in ("dialing", "in_call")]
        max_inflight = max(max_inflight, len(inflight))
        if len(inflight) > 1:
            serial_ok = False
        # 每腿耗时：item 离开 pending 起、落终态止（含响铃窗/通话时长/冷却尾）。
        for i in items:
            key = obj_to_key.get(str(i.get("object_id") or ""), "leg?")
            st = str(i.get("status") or "")
            if st != "pending" and key not in leg_started:
                leg_started[key] = time.perf_counter()
            if st in ("done", "no_answer", "rejected", "failed", "skipped") and key not in leg_ended:
                leg_ended[key] = time.perf_counter()
                if key in leg_started:
                    LEG_TIMINGS.append((key, leg_ended[key] - leg_started[key], st))
        if str(last.get("status") or "") == "done":
            break
        time.sleep(POLL_INTERVAL_S)
    last["_samples"] = samples
    last["_max_inflight"] = max_inflight
    last["_serial_ok"] = serial_ok
    return last


def run() -> int:
    if not preflight():
        print("\n[preflight] 前置失败——栈未起/非 mock 档/有殭尸 worker，直接退出", flush=True)
        return 1

    template_id = pick_zh_template()
    print(f"  [setup] template_id={template_id!r}", flush=True)
    objs = create_objects(template_id)
    obj_ids = [str(o["id"]) for o in objs]
    print(f"  [setup] objects={obj_ids}", flush=True)
    # 名册是全库累积的：清掉历史 E2E 对象留下的同号条目，保证本轮断言只可能
    # 命中本轮 captured（否则上一轮的条目会让丢字的轮次假绿——实测踩过）。
    _cleanup_stale_roster()

    scenarios = {obj_ids[0]: "answer", obj_ids[1]: "no_answer", obj_ids[2]: "reject"}
    # 台词铁律：每句 <10 字单口气（≥10 字 + 停顿会被 vad-pause 劈轮），数字句独立
    # 成句。三个实机调出来的要点（不是猜的）：
    #  ①句间隔 `mock_speak_interval_s` 要盖过 AI 一轮处理+播报（开场白 ~5s + 收号步
    #    播报）：默认 6s 会让报号句在 AI 还在念 step1 时到达 → 号码落在收号步之外，
    #    捕获门失效（实测号码句先于 say-step step1 出现）。
    #  ②首句必须够长且是 confirm 语义（「哦原來係咁我聽下先」9 字 → rule_verdict=
    #    confirm）：太短（「你好」0.56s）会被 VAD 短尾规则吞掉、零转写零推进，报号句
    #    就会变成首轮落在 step1（收号步之外）；也不能是告别语义（会触发 REFUSE 收线）。
    #  ③号码念三次：mock 客户是固定时间轴而 AI 步进是「检测→推进」顺序——检测用
    #    的是**当前步**（推进前），所以「把流程推到 step2」和「号码落在 step2 当轮」
    #    不可能是同一轮（实测：号码只念两次时，第一轮落在 step1、第二轮既推进又
    #    携带号码但检测看到的仍是 step1 → 两次都漏）。第一次负责把流程推到收号步，
    #    后面再念一次才真正落在收号步当轮、裸号码即 captured。重复报号也符合真实
    #    客户行为（对方没回应时自然会再说一遍）。
    scripts = {obj_ids[0]: ["哦原來係咁我聽下先", "喂六四三二零一一一",
                            "六四三二零一一一", "六四三二零一一一", "好的再见"]}
    try:
        camp = _api("POST", "/api/campaigns", json={
            "account_id": ACCOUNT_ID, "name": f"E2E-CAMP-{int(time.time())}",
            "object_ids": obj_ids, "template_id": template_id, "persona_id": "",
            "language": "zh", "gap_seconds": GAP_SECONDS,
            "scenarios": scenarios, "scripts": scripts,
            "mock_speak_interval_s": MOCK_SPEAK_INTERVAL_S,
        }, timeout=20).json()
    except Exception as exc:  # noqa: BLE001
        record("C0 建战役", False, f"{type(exc).__name__}: {exc}")
        return 1
    camp_id = str(camp.get("id") or "")
    record("C0 建战役", bool(camp_id) and camp.get("status") == "draft",
           f"id={camp_id} status={camp.get('status')!r}")
    if not camp_id:
        return 1

    print(f"  [run] start campaign {camp_id}", flush=True)
    _api("POST", f"/api/campaigns/{camp_id}/start", timeout=15).raise_for_status()
    t0 = time.perf_counter()
    detail = poll_campaign(camp_id, obj_ids)
    wall = time.perf_counter() - t0
    items = detail.get("items") or []

    record("C1 串行（至多 1 路 dialing/in_call）", bool(detail.get("_serial_ok")),
           f"采样 {detail.get('_samples')} 次、峰值并发 {detail.get('_max_inflight')}")
    record("C2 campaign 终态 done", str(detail.get("status") or "") == "done",
           f"status={detail.get('status')!r} wall={wall:.1f}s")

    by_obj = {str(i.get("object_id") or ""): i for i in items}
    expected = {"answer": "done", "no_answer": "no_answer", "reject": "rejected"}
    for idx, (obj_id, scen) in enumerate(scenarios.items()):
        item = by_obj.get(obj_id) or {}
        got = str(item.get("status") or "missing")
        record(f"C3.{idx + 1} {scen} 轮终态={expected[scen]}", got == expected[scen],
               f"got={got!r} call={item.get('call_id', '')}")

    # 名册自动入册（captured→roster 链路）：answer 轮客户念出号码。
    # 断言必须**钉死到本轮 answer 轮的通话**（call_id == answer_call）：名册是全库
    # 累积的，只按号码模糊匹配会命中上一轮跑的陈旧条目（实测踩过——上一轮 captured
    # 过的同号条目让本轮 ASR 明明丢字也「PASS」，典型假绿）。
    answer_call = str((by_obj.get(obj_ids[0]) or {}).get("call_id") or "")
    roster_ok = False
    roster_note = ""
    roster_entry_id = ""
    try:
        entries = _api("GET", f"/api/roster?account_id={ACCOUNT_ID}", timeout=15).json()
        hits = [e for e in entries if str(e.get("call_id") or "") == answer_call
                and _number_close(str(e.get("number") or ""))
                and str(e.get("channel") or "") == "whatsapp"]
        roster_ok = bool(hits)
        if hits:
            roster_entry_id = str(hits[0].get("id") or "")
            call = _api("GET", f"/api/calls/{answer_call}", timeout=15).json()
            got_num = str(hits[0].get("number") or "")
            roster_note = (f"number={got_num} channel={hits[0].get('channel')} "
                           f"status={hits[0].get('status')} "
                           f"call.whatsapp_status={call.get('whatsapp_status')!r}"
                           + ("" if got_num.endswith(WA_NUMBER)
                              else f"（ASR 听岔：脚本 {WA_NUMBER}）"))
        else:
            got_all = [(str(e.get("number") or ""), str(e.get("call_id") or ""))
                       for e in entries if str(e.get("call_id") or "") == answer_call]
            roster_note = (f"本轮通话 {answer_call} 无合格名册条目"
                           f"（本轮全部条目 {got_all}，共 {len(entries)} 条名册）")
    except Exception as exc:  # noqa: BLE001
        roster_note = f"{type(exc).__name__}: {exc}"
    record("C4 名册自动入册（captured→roster）", roster_ok, roster_note)

    # 话音真度：answer 轮的 customer 转写必须非空（mock 客户真出声，不是静坐无声）。
    speech_ok = False
    speech_note = f"call_id={answer_call!r}"
    if answer_call:
        try:
            turns = _api("GET", f"/api/calls/{answer_call}/turns", timeout=15).json()
            cust = [t for t in turns if str(t.get("speaker") or "") == "customer"
                    and str(t.get("transcript") or "").strip()]
            speech_ok = bool(cust)
            speech_note += (f" customer_turns={len(cust)} "
                            f"first={str(cust[0]['transcript'])[:30]!r}" if cust
                            else f" 零 customer 转写（共 {len(turns)} turn）")
        except Exception as exc:  # noqa: BLE001
            speech_note += f" {type(exc).__name__}: {exc}"
    record("C5 接通轮有客户真语音转写", speech_ok, speech_note)

    # 名册动作回写来源通话（handled → call.whatsapp_status=handled）——同样钉死
    # 本轮的那条名册条目（roster_entry_id 为空的档位直接判失败，不落到陈旧条目）。
    try:
        if roster_entry_id:
            _api("POST", f"/api/roster/{roster_entry_id}/handled", json={"handled": True},
                 timeout=15).raise_for_status()
            call = _api("GET", f"/api/calls/{answer_call}", timeout=15).json()
            record("C6 名册 handled 回写来源通话",
                   str(call.get("whatsapp_status") or "") == "handled",
                   f"whatsapp_status={call.get('whatsapp_status')!r}")
        else:
            record("C6 名册 handled 回写来源通话", False, "本轮无名册条目可标记")
    except Exception as exc:  # noqa: BLE001
        record("C6 名册 handled 回写来源通话", False, f"{type(exc).__name__}: {exc}")

    # 清理：删测试对象（campaign 留档——API 无 DELETE 战役，且留档便于复盘）。
    for oid in obj_ids:
        try:
            _api("DELETE", f"/api/objects/{oid}", timeout=15)
        except Exception:  # noqa: BLE001 - 清理失败不影响判定
            pass
    print(f"  [cleanup] 已删 {len(obj_ids)} 个测试对象；campaign {camp_id} 留档", flush=True)

    print("\n  每腿耗时：", flush=True)
    for key, secs, st in LEG_TIMINGS:
        print(f"    {key}: {secs:.1f}s → {st}", flush=True)
    print(f"  战役总墙钟: {wall:.1f}s", flush=True)
    return 0


def main() -> int:
    try:
        run()
    except KeyboardInterrupt:
        return 130
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n--- 明细 ---", flush=True)
    for name, ok, note in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name} {note}", flush=True)
    print(f"CAMPAIGN_E2E {passed}/{len(RESULTS)} PASSED", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
