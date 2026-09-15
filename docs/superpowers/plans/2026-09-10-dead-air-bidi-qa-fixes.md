# 零残余静默修复包（垫话链发 / bidi 冷启动根治 / QA 命中率）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消灭最差轮的垫话后残余静默（实测 1.43s）与 MiniMax bidi 冷连接离群（实测 TTS 首包 3170ms vs p50 350ms），并把 QA 快路命中率从 0/42 轮做成可度量、可增长的闭环。

**Architecture:** 三个独立子域并行推进——①垫话域：在已合并的「垫话资产化+播放排序契约」（origin/main 020a74c）之上加长句资产与自动链发第二垫话；②bidi 域：死亡即重预热 + ping 连失强断 + bidi 首包看门狗（classic 已验证的形状移植），探针先行归因冷启动构成；③QA 域：汇总打点 + 离线命中率探针 + 真实转写挖掘 runbook。全部改动 env 可回退，不动架构。

**Tech Stack:** Python 3.12 / livekit-agents 1.8.0（锁 <1.9，1.8.0 即最新）/ MiniMax t2a_v2_bidi WebSocket / pytest。

## 实测基线（2026-09-10，42 轮全三语口径）

| 段 | p50 | p90 | 最差 |
|---|---|---|---|
| ASR | 195ms | — | — |
| eou | 652ms | 1123ms | — |
| LLM TTFT（抢跑含提前量） | 474ms | 1086ms | 3566ms |
| TTS 首包 | 350ms | — | **3170ms（bidi 冷连接离群）** |
| perceived（用户讲完→AI出声） | 1550ms | 2336ms | 2826ms |
| 垫话轮残余静默 | 0ms | — | **1430ms**（慢LLM+bidi冷连接轮） |
| QA 快路命中 | 0/42 轮（48 条字面库） | — | — |

## Global Constraints

- **基线钉死**：一切实施分支从 `origin/main`（tip=020a74c）拉。主仓工作目录当前停在 `feat/kb-incremental-indexing`（KB PR#59 三提交，**不含** #60-62 栈）——不要在那儿实施；用 `git worktree add ../va-deadair origin/main -b feat/dead-air` 开新 worktree。`/Users/halo/Documents/bok/va-p0-thin-node` 是正在跑的栈（内容==origin/main），只读参照，勿改勿停。
- **KB PR#59 是独立事项**：它的 rebase/合并不属本包，实施者勿顺手处理。
- **垫话通道铁律**：走 `BackgroundAudioPlayer` out-of-band，绝不 `session.say()`；不进 LLM 上下文；垫话一旦开播必须播完（hold 契约已合并，`fillers.py hold_if_playing` + `tts_cache._RelaySynthesizeStream` 扣压）；新用户轮 cancel 立即掐。
- **万能话术原则（用户逐条过审）**：垫话只准「注意力应承（好的/收到/明白/嗯）+ 等待邀请（稍等/我看下）」，零动作动词、零语境依赖。改话术=改 `scripts/gen_filler_assets.py` 重新生成，一个 PR。
- **粤语规范值 = 小写 `cantonese`** 全栈唯一拼写；`tests/test_cantonese_terminology.py` 门禁。
- **绝不运行时云合成垫话**；QA 快路命中还需音频已物化（`tts-pregen --qa`）。
- **MiniMax 凭据**在设置 DB `tts.api_key`，勿提交仓库；探针脚本读 `MINIMAX_API_KEY` env，缺 key 明文报错退出。venv 无系统 CA，手起进程带 `SSL_CERT_FILE=certifi`。
- **A/B 前后殭尸 worker 门禁**：`ps aux | grep agent_runtime` 必须为 0 再 serve；`E2E 测试句形铁律`：话音句不带逗号/大换气，<10 字单口气句。
- 改完 Python 跑 `python -m compileall -q apps packages services tools scripts`。
- Merge gate：pytest 全绿 + `npm test` + `cargo test` + `scripts/verify_bundle.sh`（单模式）+ 三语 E2E / barge-in / edge 真栈通过；**never fake-green**。

## 文件结构总览

| 文件 | 职责 | 动作 |
|---|---|---|
| `scripts/probe_minimax_bidi_cold.py` | bidi 冷启动归因探针 | 新建 |
| `scripts/probe_qa_hit.py` | QA 词库离线命中率探针 | 新建 |
| `scripts/gen_filler_assets.py` | 垫话资产生成（长句 tier） | 修改 |
| `apps/agent/agent_runtime/assets/fillers/*` | 30→39 wav + manifest | 再生成 |
| `apps/agent/agent_runtime/fillers.py` | 链发编排 + 默认值 | 修改 |
| `apps/agent/agent_runtime/providers/livekit_plugins.py` | bidi 重预热/看门狗/预热重试 | 修改 |
| `apps/agent/agent_runtime/agent.py` | QA 汇总打点 + 读点归一 + pacing | 修改 |
| `tests/test_fillers.py` / `tests/test_minimax_bidi.py` / 新 `tests/test_probe_qa_hit.py` | 契约钉死 | 修改/新建 |
| `AGENTS.md` | 运行规约增补 | 修改 |

---

## Phase 0 探针先行（度量不落地不调参）

### Task 1: bidi 冷启动归因探针

**Files:**
- Create: `scripts/probe_minimax_bidi_cold.py`

**Interfaces:**
- Produces: 归因表输出（stdout），判定 3.17s 离群 ∈ {握手段, 服务端处女连接首合成预热, 2201 后惰性重连}。Task 8 的 `MINIMAX_BIDI_SYNTH_WARMUP` 默认值由本探针结论拍板。

- [ ] **Step 1: 写探针脚本**

协议字段抄 `livekit_plugins.py` 的 `_task_start_payload`（搜 `def _task_start_payload`）与 `_connect_and_start`（:2496）的时序：connect → 收 `connected_success` → `task_start` → 等 `task_started` → `task_continue` → `task_flush` → 收音频（hex）至 `task_flushed`。

```python
#!/usr/bin/env python3
"""bidi 冷启动归因探针(2026-09-10)。

回答:实测 3.17s TTS 首包离群的构成——
A) 握手段(connect+connected_success+task_start/task_started RTT)
B) 服务端处女连接首次合成预热(同连接第1 vs 第2次合成对比)
C) 空闲后/2201 断连后重建连接的合成首包回升
用法: MINIMAX_API_KEY=... .venv312/bin/python scripts/probe_minimax_bidi_cold.py
"""
from __future__ import annotations
import asyncio, json, os, sys, time

import websockets  # 仓库运行时已有

MODEL = os.environ.get("MINIMAX_MODEL", "speech-2.8-hd")
VOICE = os.environ.get("MINIMAX_BIDI_PROBE_VOICE", "Cantonese_crisp_news_anchor_vv2")
ENDPOINT = os.environ.get(
    "MINIMAX_WS_URL",
    f"wss://api.{'minimax.io' if os.environ.get('MINIMAX_REGION','cn')=='intl' else 'minimax.cn'}"
    "/ws/v1/t2a_v2_bidi",
)
TEXT = os.environ.get("MINIMAX_BIDI_PROBE_TEXT", "好，我而家就幫你睇下。")

async def connect_start(key: str):
    t0 = time.monotonic()
    ws = await websockets.connect(
        ENDPOINT, additional_headers={"Authorization": f"Bearer {key}"},
        open_timeout=10, max_size=20_000_000, ping_interval=None,
    )
    t_connect = time.monotonic()
    try:
        await asyncio.wait_for(ws.recv(), timeout=10)  # connected_success
    except Exception:
        pass
    # 字段与 livekit_plugins._task_start_payload 同源;audio_setting pcm/24k/mono
    await ws.send(json.dumps({
        "event": "task_start", "model": MODEL,
        "voice_setting": {"voice_id": VOICE, "speed": 1.2, "vol": 1.0, "pitch": 0},
        "audio_setting": {"sample_rate": 24000, "bitrate": 128000, "format": "pcm", "channel": 1},
        "stream_options": {"exclude_aggregated_audio": True},
    }))
    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    assert resp.get("event") == "task_started", resp
    return ws, (time.monotonic() - t0) * 1000, (time.monotonic() - t_connect) * 1000

async def synth_once(ws, tag: str) -> float:
    """一次合成:发文本+flush,量首音频与全部收完耗时(ms)。"""
    t0 = time.monotonic(); first = None
    await ws.send(json.dumps({"event": "task_continue", "data": {"text": TEXT}}))
    await ws.send(json.dumps({"event": "task_flush"}))
    t_text = time.monotonic()
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        msg = json.loads(raw)
        ev = msg.get("event"); data = msg.get("data") or {}
        if (data.get("audio") or "") and first is None:
            first = (time.monotonic() - t_text) * 1000
        if ev == "task_flushed":
            break
    print(f"[{tag}] text→first_audio={first and round(first)}ms total={(time.monotonic()-t0)*1000:.0f}ms")
    return first or -1

async def main() -> int:
    key = os.environ.get("MINIMAX_API_KEY", "")
    if not key:
        print("缺 MINIMAX_API_KEY(设置 DB tts.api_key 的值)", file=sys.stderr); return 2
    ws, total_ms, post_ms = await connect_start(key)
    print(f"[connect] total={total_ms:.0f}ms (ws握手后段={post_ms:.0f}ms)")
    await synth_once(ws, "处女连接·第1次合成")
    await synth_once(ws, "同连接·第2次合成")
    for idle_s in (30, 60, 110):
        t0 = time.monotonic()
        while time.monotonic() - t0 < idle_s:  # 60s 自管 ping,同生产
            await asyncio.sleep(min(60, idle_s)); await asyncio.wait_for(ws.ping(), timeout=10)
        await synth_once(ws, f"ping 保活空闲{idle_s}s 后")
    # 不 ping 挂 130s:等 2201(官方 ~120s 空闲断连)
    print("[2201 观察] 停 ping 130s …", flush=True)
    t0 = time.monotonic(); closed = None
    try:
        while time.monotonic() - t0 < 140:
            await asyncio.wait_for(ws.recv(), timeout=10)
    except Exception as exc:
        closed = f"{time.monotonic()-t0:.0f}s {exc!r}"
    print(f"[2201 观察] 结果: {closed or '未断(140s)'}")
    ws2, t2, _ = await connect_start(key)
    print(f"[2201 重建] connect={t2:.0f}ms")
    await synth_once(ws2, "重建后·第1次合成")
    await ws2.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: 真栈空跑验证**

Run: `MINIMAX_API_KEY=$(sqlite3 "$HOME/Library/Application Support/BokVoice/bok_voice.db" "select value from settings where key='tts.api_key'") .venv312/bin/python scripts/probe_minimax_bidi_cold.py`
Expected: 归因表完整输出。**把结论写进本文件 Task 8 的默认值拍板行**（处女连接第1次 vs 第2次合成差值 = 服务端预热成分）。

- [ ] **Step 3: Commit**

```bash
git add scripts/probe_minimax_bidi_cold.py
git commit -m "test(probe): bidi 冷启动归因探针——处女连接/保活空闲/2201重建三路径首包实测"
```

### Task 2: QA 词库命中率探针

**Files:**
- Create: `scripts/probe_qa_hit.py`
- Test: `tests/test_probe_qa_hit.py`（纯函数部分）

**Interfaces:**
- Consumes: CP 现有端点 `GET /api/qa-entries`（enabled=1）、`GET /api/reports/qa-pairs?min_calls=1&limit=200`（服务端 `mine_qa_pairs`，见 `packages/core/bok_voice_core/qa_text.py:47`）；打分器 `agent_runtime.qa_gate.QaIndex`（构造吃 `cp.list_qa_entries()` 行列表，`match(text, lang, step_index)`）。
- Produces: `summarize(rows, pairs) -> dict`（would_hit_rate、top_misses），Task 10 复测用同一函数。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_probe_qa_hit.py
from scripts_probe_shim import summarize  # 见 Step 2 说明:直接 import probe 模块

def test_summarize_counts_would_hit_and_misses():
    rows = [{"id": 1, "question_text": "你们几点上班", "answer_text": "九点",
             "lang": "zh", "scope": "global", "step_index": None, "enabled": 1}]
    pairs = [{"question": "你们几点上班", "lang": "zh", "calls": 5},
             {"question": "我想改收货地址", "lang": "zh", "calls": 3}]
    out = summarize(rows, pairs)
    assert out["pairs_total"] == 2
    assert out["would_hit"] == 1
    assert out["would_hit_rate"] == 0.5
    assert out["top_misses"][0]["question"] == "我想改收货地址"
```

（`scripts/` 非 package——probe 文件顶部加 `sys.path` 自引用太绕，直接把 `summarize` 写成模块级纯函数，测试里 `sys.path.insert(0, "scripts"); import probe_qa_hit`。）

- [ ] **Step 2: 写探针**

```python
#!/usr/bin/env python3
"""QA 快路词库命中率探针:真实转写挖出的问法 vs 现库,报 would-hit 率与 top 未命中。"""
from __future__ import annotations
import json, os, sys, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

CP = os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000")

def _get(path: str):
    req = urllib.request.Request(CP + path)
    tok = os.environ.get("BOK_CP_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def summarize(rows: list[dict], pairs: list[dict]) -> dict:
    from agent_runtime.qa_gate import QaIndex  # 延迟 import(需 PYTHONPATH 指向 apps/agent)

    idx = QaIndex(rows)
    hit = 0; misses = []
    for p in pairs:
        lang = p.get("lang") or "zh"
        step = p.get("step_index")
        m = idx.match(p["question"], lang, step)
        if m and m.get("entry_id") is not None:
            hit += 1
        else:
            misses.append({"question": p["question"], "calls": p.get("calls", 0), "lang": lang})
    misses.sort(key=lambda e: -e["calls"])
    total = len(pairs)
    return {"pairs_total": total, "would_hit": hit,
            "would_hit_rate": (hit / total) if total else 0.0,
            "top_misses": misses[:20]}

def main() -> int:
    data = _get("/api/qa-entries")
    rows = data.get("items", data) if isinstance(data, dict) else data
    rows = [r for r in rows if r.get("enabled", 1)]
    rep = _get("/api/reports/qa-pairs?min_calls=1&limit=200")
    pairs = rep.get("pairs", rep) if isinstance(rep, dict) else rep
    out = summarize(list(rows), list(pairs))
    print(f"库={len(rows)} 条 | 真实问法={out['pairs_total']} | would-hit {out['would_hit']}"
          f" ({out['would_hit_rate']:.0%})")
    for m in out["top_misses"]:
        print(f"  MISS[{m['calls']}通] ({m['lang']}) {m['question']}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

注意：`QaIndex.match` 的返回结构以 `apps/agent/agent_runtime/qa_gate.py:97-128` 实读为准——命中判定用与 agent.py 命中块相同的字段（score ≥ 阈值返回 entry），探针里对返回 None 或无 entry 都计 miss。**跑之前先实读该函数对齐返回形状，测试同步。**

- [ ] **Step 3: 跑测试 + 真栈空跑**

Run: `.venv312/bin/pytest tests/test_probe_qa_hit.py -v`（先验证测试失败→实现→通过）
Run: `BOK_CP_URL=http://127.0.0.1:8000 .venv312/bin/python scripts/probe_qa_hit.py`
Expected: 输出基线命中率（当前预期 ≈0%）与 top 未命中问法清单——这份清单就是 Task 10 的补词条工单。

- [ ] **Step 4: Commit**

```bash
git add scripts/probe_qa_hit.py tests/test_probe_qa_hit.py
git commit -m "test(probe): QA 词库命中率探针——真实问法 would-hit 率+top 未命中工单"
```

---

## Phase 1 垫话覆盖强化

### Task 3: 长句资产扩池（30→39 条）

**Files:**
- Modify: `scripts/gen_filler_assets.py:42-97`（FILLERS 增 `long_lines` tier + 独立时长窗）
- Modify: `scripts/gen_filler_assets.py:197-247`（main 循环遍历两 tier）
- Create: `apps/agent/agent_runtime/assets/fillers/{zh,cantonese,en}-11..13.wav` + manifest.json（生成产物）
- Test: `tests/test_fillers.py`（manifest 断言）

**Interfaces:**
- Produces: manifest 每语言 13 条（10 短 + 3 长），条目 schema 不变 `{text, file, dur_s}`——`fillers.py _pools/_pick` 零改动即消费；长句 dur_s 目标 1.7-2.3s。

- [ ] **Step 1: 加长句清单与独立时长窗**

`FILLERS` 每语言 dict 增 `"long_lines": [...]`（守万能话术原则——只有应承+等待邀请，无动作动词之外的内容；参照已过审短句的动词面：「看下/查一下」属等待邀请，允许）：

```python
    "zh": {
        ...,
        "lines": [...],  # 原样不动
        "long_lines": [
            "好的，您稍等啊，我马上帮您看一下。",
            "收到收到，您别急，我这就帮您查一下。",
            "好的，麻烦您稍等一下，我马上看一下。",
        ],
    },
    "cantonese": {
        ...,
        "long_lines": [
            "好嘅，你稍等陣，我而家就幫你睇下。",
            "收到，唔好急，等我幫你睇下先。",
            "明白，麻煩你稍等多一陣，我即刻睇。",
        ],
    },
    "en": {
        ...,
        "long_lines": [  # en 社媒女声逗号拖腔(脚本头注释实证)——每句最多一个逗号
            "Sure, one moment please. Let me check that.",
            "Of course. Just a moment while I look into it.",
            "Got it. Please give me just a moment here.",
        ],
    },
```

时长窗分层（替换 `WIN_WARN/WIN_FAIL` 常量段）：

```python
WIN_WARN = (0.9, 1.6)
WIN_FAIL = (0.8, 1.8)
LONG_WIN_WARN = (1.6, 2.4)   # 长句 tier:覆盖窗目标 1.7-2.3s
LONG_WIN_FAIL = (1.5, 2.6)
```

- [ ] **Step 2: main 循环遍历两 tier**

`main()` 里现有逐 line 循环改为对 `[(cfg["lines"], 1, WIN_WARN, WIN_FAIL), (cfg.get("long_lines", []), 2, LONG_WIN_WARN, LONG_WIN_FAIL)]` 两轮遍历：tier 1 文件名 `f"{lang}-{i:02d}.wav"`（现有，不动，跳过已存在），tier 2 从 `11` 起编号 `f"{lang}-{10+i}.wav"`，时长窗取该 tier 的 warn/fail。其余逻辑（SKIP 已存在、`--force`、manifest 落盘）原样复用。

- [ ] **Step 3: 生成 + 校验**

Run: `.venv312/bin/python scripts/gen_filler_assets.py`
Expected: 新增 9 条 wav 全在 LONG 窗内（超窗按脚本报 FAIL，调 speed 微调后重跑；`--force` 只在拍板豁免时用）。`python3 -c` 验 manifest：每语言 13 条、长句 dur_s 均值 ≥1.7s。

- [ ] **Step 4: 写 manifest 契约测试**

`tests/test_fillers.py` 增（读真实资产，防再生成时 schema/池子回退）：

```python
def test_filler_assets_manifest_shape():
    from agent_runtime.fillers import FILLER_ASSETS_DIR, load_manifest
    m = load_manifest(FILLER_ASSETS_DIR)
    assert set(m) == {"zh", "cantonese", "en"}
    for lang, entries in m.items():
        assert len(entries) >= 13, f"{lang} 池应含 10 短 + 3 长"
        durs = sorted(e["dur_s"] for e in entries)
        assert durs[-1] >= 1.6, f"{lang} 应含 ≥1.6s 长句"
        for e in entries:
            assert (FILLER_ASSETS_DIR / e["file"]).exists()
```

- [ ] **Step 5: 跑测试 + Commit**

Run: `.venv312/bin/pytest tests/test_fillers.py -v` → PASS

```bash
git add scripts/gen_filler_assets.py apps/agent/agent_runtime/assets/fillers tests/test_fillers.py
git commit -m "feat(agent): 垫话池长句 tier 30→39 条——覆盖窗上限 1.5s→2.3s,残余静默窗口收窄"
```

### Task 4: 自动链发第二垫话

**Files:**
- Modify: `apps/agent/agent_runtime/fillers.py`（`_fire` :250 后挂观察者 + 新方法）
- Test: `tests/test_fillers.py`（fake player 的 PlayHandle 补 `wait_for_playout`）

**Interfaces:**
- Consumes: livekit 官方 `PlayHandle.wait_for_playout()`（`background_audio.py:472`，播完即醒；本仓 `.venv312` 内 1.8.0 逐行核验存在）。
- Produces: `BOK_FILLER_CHAIN`（默认 `"1"`，0 关）；每轮至多链发 1 次（`_chain_depth` 门）；链发消耗 `BOK_FILLER_MAX` 同一计数。

- [ ] **Step 1: fake PlayHandle 补 wait_for_playout，写失败测试**

`tests/test_fillers.py` 的 `_FakePlayer`（:35-46）handle 增加：

```python
class _FakeHandle:
    def __init__(self):
        self._done = asyncio.get_running_loop().create_future()
    def done(self):
        return self._done.done()
    def stop(self):
        if not self._done.done():
            self._done.set_result(True)
    async def wait_for_playout(self):
        await self._done
```

测试用例：

```python
def test_chain_fires_second_filler_when_reply_late(monkeypatch, ...):
    # MAX=3, CHAIN=1:arm→首发起播;不置 reply_audio;播完(handle._done.set_result)
    # → 复核门全过 → gap 后第二发起播。断言 fired_lines==2、count==2、两次不重样。

def test_chain_skipped_when_reply_first_audio_arrived(...):
    # 首发起播后调 on_reply_first_audio() 再完成 handle → 不发第二条。

def test_chain_depth_capped_at_one_per_turn(...):
    # 第二条播完仍无 reply → 不发第三条(_chain_depth 门),即使 MAX 未耗尽。

def test_chain_cancelled_on_new_turn(...):
    # 观察者挂起中调 cancel() → 任务取消,无第二发。
```

（沿用现有用例的事件循环驱动姿势——`asyncio.run`/`loop.run_until_complete` 与现有 `test_fillers.py` 一致。）

- [ ] **Step 2: 跑测试验证失败**

Run: `.venv312/bin/pytest tests/test_fillers.py -k chain -v`
Expected: FAIL（`wait_for_playout` 属性缺失 / 无链发行为）

- [ ] **Step 3: 实现**

`fillers.py` 改动（基于现行 :94-254 结构）：

```python
def filler_chain_enabled() -> bool:
    """首条垫话播完回复仍未出声 → 自动补第二条(BOK_FILLER_CHAIN,默认开)。"""
    return os.environ.get("BOK_FILLER_CHAIN", "1") == "1"
```

`__init__` 增字段：

```python
        self._chain_task: asyncio.Task | None = None
        self._chain_depth = 0          # 本轮已链发次数(每轮封顶 1)
        self._reply_audio_seen = False  # on_reply_first_audio 置位,arm() 重置
```

`arm()` 开头（`self._cancel_timer()` 之后）增：

```python
        if self._chain_task is not None and not self._chain_task.done():
            self._chain_task.cancel()
        self._chain_task = None
        self._chain_depth = 0
        self._reply_audio_seen = False
```

`cancel()` 与 `reset_per_call()` 同款加链发任务取消（三行同上，抽 `_cancel_chain()` 私有方法三处复用）。

`on_reply_first_audio()` 增一行 `self._reply_audio_seen = True`。

`_fire()` 在 `self._handle = self._player.play(source)`（:250）之后增：

```python
            self._spawn_chain()
```

新方法：

```python
    def _spawn_chain(self) -> None:
        """首条起播即挂「播完观察者」——官方 PlayHandle.wait_for_playout 精确补位。"""
        if not filler_chain_enabled() or self._chain_depth > 0:
            return
        self._chain_task = asyncio.create_task(self._chain_wait())

    async def _chain_wait(self) -> None:
        try:
            handle = self._handle
            if handle is None:
                return
            wait = getattr(handle, "wait_for_playout", None)
            if callable(wait):
                await wait
            else:  # 测试替身无官方 API:按已知时长等(少量过等由后续门复核兜住)
                await asyncio.sleep(self._cur_dur + 0.05)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return
        self._handle = None  # 已播完:清档,放行 _fire 的「上一句还在播」门
        if self._reply_audio_seen:
            return  # 回复首音频已到,hold 契约自会衔接,唔使链发
        if not filler_enabled() or self._player is None:
            return
        if self._guards() or filler_max_per_call() <= self._count:
            return
        state = str(getattr(self._session, "agent_state", "") or "")
        if state not in ("listening", "thinking", ""):
            return
        await asyncio.sleep(filler_gap_s())  # 垫话→垫话同款呼吸
        if self._reply_audio_seen or self._handle is not None:
            return
        self._chain_depth += 1
        await self._fire(0.0)  # 复用开火路径(门在 _fire 内再复核;计数同源)
```

时序语义：垫话1播完 → gap → （回复仍未出声才）垫话2；若回复首音频在垫话1期间到达，`on_reply_first_audio` 只作废定时器（现行契约），hold 扣压衔接回复——链发条件 `_reply_audio_seen` 恰好把这两种结局分开。第二发起播后 `_cur_dur/_play_started/_handle` 更新，`hold_if_playing` 对垫话2照常生效。

- [ ] **Step 4: 跑测试通过**

Run: `.venv312/bin/pytest tests/test_fillers.py -v` → 全 PASS（含既有 11 用例零回归）

- [ ] **Step 5: Commit**

```bash
git add apps/agent/agent_runtime/fillers.py tests/test_fillers.py
git commit -m "feat(agent): 垫话自动链发——wait_for_playout 精确补位第二发,残余静默窗口再砍半"
```

### Task 5: 限次默认 2→3 + 队列连发呼吸

**Files:**
- Modify: `apps/agent/agent_runtime/fillers.py:60-64`（默认值）
- Modify: `apps/agent/agent_runtime/agent.py:1588`（`AgentSession(` 构造加参）
- Test: `tests/test_fillers.py`（MAX 默认断言）、`tests/test_p1_orchestration.py`（若钉了 session 参数集合同步）

**Interfaces:**
- Consumes: 官方 `min_consecutive_speech_delay`（AgentSession 参数，默认 0.0；语义=speech 队列相邻两条播报间的最小间隔，垫话 out-of-band 不经此队列零影响——livekit `agent_activity.py:1838-1858` 核验）。
- Produces: `BOK_FILLER_MAX` 默认 3；A 线 `min_consecutive_speech_delay=0.3`。**B 线（interpret.py:288 的 AgentSession）不加**——同传节奏独立。

- [ ] **Step 1: 改默认值 + 测试**

`filler_max_per_call()` 默认 `"2"`→`"3"`，`except ValueError: return 3`。`test_fillers.py` 既有 MAX 用例（:190-210）显式设 `BOK_FILLER_MAX=2` 的保持不变；补一条默认值断言：

```python
def test_filler_max_default_is_three(monkeypatch):
    monkeypatch.delenv("BOK_FILLER_MAX", raising=False)
    from agent_runtime.fillers import filler_max_per_call
    assert filler_max_per_call() == 3
```

理由注释写在默认值行：链发与主动 arm 共享计数，3 = 单轮至多「1 主动+1 链发」仍给后续轮留 1 发。

- [ ] **Step 2: AgentSession 加 pacing**

`agent.py:1588` 的 `AgentSession(...)` 构造参数加 `min_consecutive_speech_delay=0.3,`（紧挨现有 `interruption=...` 参数块；勿动 B 线）。

- [ ] **Step 3: 跑测试 + Commit**

Run: `.venv312/bin/pytest tests/test_fillers.py tests/test_p1_orchestration.py -v`

```bash
git add apps/agent/agent_runtime/fillers.py apps/agent/agent_runtime/agent.py tests/test_fillers.py
git commit -m "feat(agent): 垫话限次默认2→3(链发共享计数)+min_consecutive_speech_delay 0.3队列呼吸"
```

---

## Phase 2 MiniMax bidi 冷启动根治

> 现状（真基线 `livekit_plugins.py`）：会话级预热已有（`prewarm` :2567，会话装配时 agent.py 调），60s 自管 ping（:2466），2201/异常惰性重连（`ensure_ready` :2528）。三个缺口：**①连接死亡要等真实合成轮才发现，那一轮吃全程冷启动**；**②bidi 无首包看门狗**（classic 专属 :2181，bidi 只有 30s recv 超时）；**③prewarm 失败静默**（首段照吃冷启动）。
> 明确否决：worker 常驻全局热连接池——`language_boost` 不在参数指纹（:1670-1684 区）跨 call 串带旧语言、音色跨 call 不匹配、一连接一会话并发 2206 风险。收益增量小正确性坑多。

### Task 6: 死亡即重预热 + ping 连失强断

**Files:**
- Modify: `apps/agent/agent_runtime/providers/livekit_plugins.py`（`_MiniMaxBidiSession`：`invalidate` :2553、`_ping_loop` :2466、`_recv_loop` 死亡分支 :2717-2722）
- Test: `tests/test_minimax_bidi.py`（fake websockets 既有基建）

**Interfaces:**
- Produces: `invalidate(*, reprewarm: bool = False)`；env `MINIMAX_BIDI_AUTO_REWARM`（默认 `"1"`）、`MINIMAX_BIDI_PING_MAX_MISS`（默认 `"2"`）。Task 7 的看门狗复用 `reprewarm=False` 语义（看门狗自持锁重连，不走 prewarm）。

- [ ] **Step 1: 写失败测试**

```python
def test_invalidate_reprewarm_schedules_background_prewarm(...):
    # session.invalidate(reprewarm=True) 后:_ws=None 且 _prewarm_task 在飞(fake connect 成功)
    # env MINIMAX_BIDI_AUTO_REWARM=0 时不排。

def test_ping_consecutive_misses_force_invalidate(...):
    # fake ws 的 ping 抛 TimeoutError 两次 → invalidate 被调 + 日志 MINIMAX_TTS_BIDI_DEAD
    # 单次 miss 不断,pong 恢复清零计数。
```

- [ ] **Step 2: 实现**

`__init__` 增 `self._ping_misses = 0`。`invalidate` 改签名：

```python
    async def invalidate(self, *, reprewarm: bool = False) -> None:
        """弃置当前连接(2201/异常关闭/收尾);下个 ensure_ready 自动全新重连。
        reprewarm=True:死亡即后台重预热——唔等下一个真实轮先撞冷启动。"""
        ws, self._ws = self._ws, None
        self._params = None
        self._ping_misses = 0
        self._stop_ping()
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        if reprewarm and os.environ.get("MINIMAX_BIDI_AUTO_REWARM", "1") == "1":
            self.prewarm()
```

`_ping_loop` 的 `except asyncio.TimeoutError` 分支（:2478-2479）改：

```python
                except asyncio.TimeoutError:
                    self._ping_misses += 1
                    print(f"MINIMAX_TTS_BIDI_PING_TIMEOUT miss={self._ping_misses}", flush=True)
                    if self._ping_misses >= self._ping_max_miss():
                        print("MINIMAX_TTS_BIDI_DEAD ping连失 — 强断重预热", flush=True)
                        await self.invalidate(reprewarm=True)
                        return
                else:
                    self._ping_misses = 0
```

配套 `_ping_max_miss()`（读 `MINIMAX_BIDI_PING_MAX_MISS` 默认 2，配错回 2）。`_recv_loop` 死亡分支（:2719）`await session.invalidate()` → `await session.invalidate(reprewarm=True)`。

`aclose()` 防 teardown 期重预热——先取消在飞预热任务（官方 #7050 形状）：

```python
    async def aclose(self) -> None:
        task = self._prewarm_task
        self._prewarm_task = None
        if task is not None and not task.done():
            task.cancel()
        await self.invalidate()
```

- [ ] **Step 3: 跑测试 + Commit**

Run: `.venv312/bin/pytest tests/test_minimax_bidi.py -v` → 全 PASS（既有 448 行用例零回归）

```bash
git add apps/agent/agent_runtime/providers/livekit_plugins.py tests/test_minimax_bidi.py
git commit -m "fix(tts): bidi 死亡即重预热+ping连失强断——死亡连接不再等真实轮才发现,冷启动离群源①根治"
```

### Task 7: bidi 首包看门狗（重连+重发）

**Files:**
- Modify: `apps/agent/agent_runtime/providers/livekit_plugins.py`（`_MiniMaxBidiStream._run` :2653 起）
- Test: `tests/test_minimax_bidi.py`

**Interfaces:**
- Consumes: Task 6 的 `invalidate`；classic 看门狗语义参照（:2181-2220：首段文本发出起计时、超时断开重连+重发已发文本、重发闸防并发）。
- Produces: env `MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S`（默认 `"6"`，`"0"` 关）。bidi 攒句比 classic 按句宽，起步 6s；Task 1 探针数据回来再校。

- [ ] **Step 1: 写失败测试**

```python
def test_bidi_stall_watchdog_reconnects_and_resends(...):
    # MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S=1 + fake ws:task_continue 发出后首条音频
    # 永不到达 → 1s 后:日志 MINIMAX_TTS_BIDI_STALL、连接被 invalidate、
    # 新连接上重发已发文本(单条合并 task_continue)、新纪元认领、音频恢复推送。
def test_bidi_stall_watchdog_off(...):
    # env=0:挂起不重连(行为同旧)。
```

- [ ] **Step 2: 实现**

`_run` 内：①发送侧每发一条 `task_continue`（搜 `state["t_first_continue"]` 的赋值点所在发送分支）把文本 append 进 `self._sent_text_parts: list[str]`（`__init__` 置 `[]`）；②首条 `task_continue` 发出后起看门狗任务、`state["first_pushed"]` 置位或流收尾时 cancel：

```python
async def _stall_watch(self, session, state) -> None:
    """首段文本发出后 N 秒无首包 → 判连接僵死:弃连接重连+重发已发文本。
    classic MINIMAX_FIRST_AUDIO_TIMEOUT_S 同语义移植;bidi 无此看门狗时只能
    干等 30s recv 超时(整轮回复静默)。"""
    timeout = self._first_audio_timeout_s()
    if timeout <= 0:
        return
    await asyncio.sleep(timeout)
    if state["first_pushed"] or self._flushed_evt.is_set():
        return
    print(f"MINIMAX_TTS_BIDI_STALL timeout={timeout}s — 重连重发", flush=True)
    await session.invalidate()          # 不 reprewarm:本流自持锁紧接着 ensure_ready
    ws = await session.ensure_ready()   # 全新连接(我们仍持 session.lock)
    text = "".join(self._sent_text_parts)[:10000]  # 官方单条 ≤10k
    if text:
        await ws.send(json.dumps({"event": "task_continue", "data": {"text": text}}))
    state["t_first_continue"] = time.monotonic()
```

配套 `_first_audio_timeout_s()`（env 读，配错回 6.0）。**收流侧重建**：`_recv_loop` 闭包引用的 `ws` 变量在旧连接 `invalidate` 后其 `recv()` 抛错自然退出（现行为）；看门狗重连后需要一条新 recv 消费路径——把 `_recv_loop` 的启动抽成 `_run` 作用域的 `_start_recv(ws)` 小函数，看门狗重连成功后重建 recv 任务并把新 `ws` 重新绑定到闭包变量（`nonlocal ws`）。纪元语义保持：重发即本流首个 task_continue → 按现行认领路径认领 `my_epoch`（实读认领点，发送分支里 `session.active_epoch = my_epoch` 一行同款）。若实现中发现 recv 任务结构与闭包重绑过于缠绕，允许把 recv 循环改为参数传 ws 的独立协程（行为等价、测试同过即可）。

- [ ] **Step 3: 跑测试 + Commit**

Run: `.venv312/bin/pytest tests/test_minimax_bidi.py tests/test_minimax_stream.py -v`

```bash
git add apps/agent/agent_runtime/providers/livekit_plugins.py tests/test_minimax_bidi.py
git commit -m "fix(tts): bidi 首包看门狗——6s 无首包重连+重发已发文本,30s干等recv超时时代结束"
```

### Task 8: prewarm 失败重试 +（探针定夺的）合成级预热

**Files:**
- Modify: `apps/agent/agent_runtime/providers/livekit_plugins.py`（`_prewarm_run` :2579-2592、`__init__`、`_connect_and_start` 成功尾）
- Test: `tests/test_minimax_bidi.py`

**Interfaces:**
- Produces: env `MINIMAX_BIDI_PREWARM_RETRY`（默认 `"1"`，失败 1s 后重试一次——官方 #6969 姿势）、`MINIMAX_BIDI_SYNTH_WARMUP`（**默认值由 Task 1 探针结论拍板**：处女连接第1次合成首包 − 同连接第2次 ≥500ms → 默认 `"1"`，否则 `"0"`）。
**T1 实测结论**：处女连接第1次合成 274 ms vs 第2次 252 ms（差值=服务端预热成分 ~22ms，可忽略；两次复跑一致 310/314ms）；ping 保活空闲 30/60/110s 后 250/331/262 ms 无回升；2201 实测 ~127s 服务端断连，重建 connect ~511ms + 首合成 301 ms（重建全路径 ≈0.8s，均远低于 3.17s 离群）→ SYNTH_WARMUP 默认应设 "0"（握手预连已覆盖大头，合成级预热零收益）。

- [ ] **Step 1: 失败测试**

```python
def test_prewarm_fail_retries_once(...):
    # fake connect 第一次抛错第二次成功 → 日志先 FAIL 后成功重试;RETRY=0 只试一次。
def test_synth_warmup_sends_and_discards(...):
    # SYNTH_WARMUP=1:prewarm 连接后发一条 task_continue+task_flush,收音频丢弃,
    # 连接仍活、无参数指纹变化;=0 不发。
```

- [ ] **Step 2: 实现**

`__init__` 增 `self._prewarm_retries = 0`；`_connect_and_start` 成功尾（`self._start_ping(ws)` 后）重置 `self._prewarm_retries = 0`。`_prewarm_run` 失败分支改：

```python
        except Exception as exc:  # noqa: BLE001
            print(f"MINIMAX_TTS_BIDI_PREWARM_FAIL {exc!r}", flush=True)
            await self.invalidate()
            if (
                self._prewarm_retries < 1
                and os.environ.get("MINIMAX_BIDI_PREWARM_RETRY", "1") == "1"
            ):
                self._prewarm_retries += 1
                await asyncio.sleep(1.0)
                self.prewarm()
```

合成级预热（连接建好后立刻做一次真实合成把服务端会话焐热、音频全丢）：

```python
    async def _synth_warmup(self, ws) -> None:
        t0 = time.monotonic()
        await ws.send(json.dumps({"event": "task_continue",
                                  "data": {"text": "好的，您稍等。"}}))
        await ws.send(json.dumps({"event": "task_flush"}))
        while True:  # 丢弃音频至 task_flushed(上限 8s);纪元门禁由首个真实流兜底
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
            if json.loads(raw).get("event") == "task_flushed":
                break
        print(f"MINIMAX_BIDI_SYNTH_WARMUP ms={(time.monotonic()-t0)*1000:.0f}", flush=True)
```

在 `_prewarm_run` 成功路径（`_connect_and_start` 之后、打点之前）按 env 调用；`_synth_warmup` 自身异常只打 `MINIMAX_BIDI_SYNTH_WARMUP_FAIL` 不 invalidate（预热文本合成失败≠连接坏）。计费代价：每通 ~8 个字符（`extra_info.usage_characters` 口径），量级可忽略。

- [ ] **Step 3: 跑测试 + Commit**

Run: `.venv312/bin/pytest tests/test_minimax_bidi.py -v`

```bash
git add apps/agent/agent_runtime/providers/livekit_plugins.py tests/test_minimax_bidi.py
git commit -m "fix(tts): bidi prewarm 失败重试(官方#6969姿势)+可选合成级预热——首段合成不再吃冷启动"
```

---

## Phase 3 QA 快路命中率

### Task 9: 汇总打点 + 读点归一

**Files:**
- Modify: `apps/agent/agent_runtime/agent.py`（QA 块 :2240-2320 区，搜 `QA_FASTPATH`；通话收尾 `_close`）
- Test: `tests/test_p1_orchestration.py` 或新 `tests/test_qa_summary.py`

**Interfaces:**
- Produces: 每通收尾一行 `QA_FASTPATH_SUMMARY hit=N hit_audio=N no_audio=N bypass digits=N refuse=N wa=N advanced=N verdict=N disabled=N match0=N`（stdout，PERF 风格）；`agent.py` 改调 `qa_gate.qa_fastpath_enabled()`（消除 `os.environ` 内联双读点，`qa_gate.py:23` 现成无人调用）。

- [ ] **Step 1: 写失败测试**——装配期注入 env/装配产物，跑一轮收尾，断言 summary 行各计数正确（fake cp + 直接调 `_close` 路径，参照 test_fillers 的模块级函数测试姿势；若 `_close` 闭包不可达，把计数器做成 `agent.py` 模块级 `QA_COUNTERS` dict + `print` 纯函数 `format_qa_summary(counters)`，测纯函数）。
- [ ] **Step 2: 实现**——QA 块三处 print 点（bypass :~2279 / hit=1 :2312 / hit=0 no_audio）各增计数器自增；`_close()`（通话结算处，搜 `_close` / `call_id` settle 逻辑）末尾打印 summary 行。读点归一：`agent.py:1608` 附近的 `os.environ.get("BOK_QA_FASTPATH", "1") == "1"` 改 `qa_fastpath_enabled()`（import 自 `.qa_gate`）。
- [ ] **Step 3: 跑测试 + Commit**

```bash
git add apps/agent/agent_runtime/agent.py tests/
git commit -m "feat(agent): QA_FASTPATH_SUMMARY 每通汇总+BOK_QA_FASTPATH 读点归一——命中率从打点散值变可聚合"
```

### Task 10: 词库增长执行（操作 runbook，非代码）

- [ ] **Step 1**: 跑 Task 2 探针拿 top 未命中工单（真栈、真实通话转写）。
- [ ] **Step 2**: 按工单补词条——每高频意图 ≥3 种字面说法（HybridLexical 只认字面措辞，同义改写必须按说法补条目；答案文案用话术原文）；`source=curated`、`scope=global`、`enabled=1`，经 `POST /api/qa-entries`（或 web 话术表单）。
- [ ] **Step 3**: `python tools/bok.py tts-pregen --qa` 物化音频（命中还需音频已缓存）。
- [ ] **Step 4**: 复跑 Task 2 探针——目标 top-20 挖掘问法 would-hit ≥30% 且逐条有音频。不达标的问法回 Step 2 补说法。
- [ ] **Step 5**: 无代码提交（词条在 DB）；把前后命中率数字记入本文件末尾「验收记录」节。

---

## Phase 4 验收与文档

### Task 11: 全量回归 + 文档 + PR

- [ ] **Step 1**: `python -m compileall -q apps packages services tools scripts`
- [ ] **Step 2**: `.venv312/bin/python scripts/test.sh` 全绿（含术语门禁、新增 chain/watchdog/summary 用例）
- [ ] **Step 3**: 真栈 E2E（殭尸门禁：前后 `ps aux | grep agent_runtime` 为 0 再 `bok.py serve`）：三语 `E2E_ONLY=... scripts/e2e_trilingual_livekit.py` 3/3、`scripts/e2e_barge_in.py` PASS、`scripts/e2e_edge_cases.py` 8/8、`FILLER_REQUIRE=1 .venv312/bin/python scripts/probe_filler_timing.py`（主判据首声 <2.5s 且垫话通道必发）
- [ ] **Step 4**: 42 轮口径复测（与基线表同法：三语 E2E 全 turn 的 PERCEIVED_MS/各段 metrics 落日志后汇总）。**验收目标**（达标线，非硬门）：perceived p90 ≤ 2.0s；垫话轮残余静默 max ≤ 0.6s；`TTS_FIRST_AUDIO_MS` 无 >1.5s 离群；QA 快路（补库后）真实命中 ≥1 轮/通话样本。
- [ ] **Step 5**: 文档：`AGENTS.md` 罐头音三件套段补 `BOK_FILLER_CHAIN/BOK_FILLER_MAX=3`、MiniMax 运行规约段补 `MINIMAX_BIDI_AUTO_REWARM/PING_MAX_MISS/FIRST_AUDIO_TIMEOUT_S/PREWARM_RETRY/SYNTH_WARMUP`；`docs/RUNTIME_TOPOLOGY.md` 垫话链发一笔。env 表同步。
- [ ] **Step 6**: PR（squash），body 写根因（残余静默 1.43s 构成 / bidi 三缺口 / QA 零命中）+ 验收证据（Step 4 数字 vs 基线表）。

## Self-Review 记录

- 覆盖检查：三问题（残余静默→T3/T4/T5；bidi 离群→T1/T6/T7/T8；QA 零命中→T2/T9/T10）均有任务；白捡项 min_consecutive_speech_delay 落 T5。
- 占位符扫描：T7 Step 2 的 recv 重绑给了两条等效实现路径与行为约束（测试同过即可），非 TBD；T8 SYNTH_WARMUP 默认值显式挂 T1 结论拍板线，非延后实现。
- 类型/命名一致性：`invalidate(*, reprewarm)` 在 T6 定义、T7 消费同签名；`_spawn_chain/_chain_wait/_chain_depth` T4 内自洽；env 名全表见 Task 11 Step 5。

## 验收记录

### Phase 3 词库增长实测（Task 10 Step 5，2026-09-10）

- QA 库规模：**48 → 63 条**（`reports/qa-pairs` 真实转写挖掘 + 按高频意图补字面说法，`source=mined/curated` 并存，应答音频经 `tts-pregen --qa` 全量物化）。
- 探针 would-hit 率（`scripts/probe_qa_hit.py`，200 条真实问法）：**0% → 6%**；按通话加权口径 **8.7%**。
- 可补问法口径 **4/4 收编**（每条补齐 ≥3 种字面说法且音频在位）。原 Task 10 Step 4 「top-20 would-hit ≥30%」目标修订：top-20 未命中中 **16/20 为设计性排除项**（数字串/WhatsApp 信号/推进/收线等四道闸旁路问法，快路结构性不接——按设计不该命中，非词库缺口），余 4 条可补问法已全部收编。

## 追加：真实通话采集试点手册（2026-09-11，PR#63 追加提交 25ba314/188ff33）

**已落地前置**：qa-pairs 挖掘默认滤「测试对象前缀族 + 无对象通话」（实测本机 DB 779→76 通，top 高频从 E2E fixture 族变为真实场景句）；`exclude_test=false` 可看全量；正则与 agent 心跳豁免单源化（`bok_voice_core/testdata.py`）。

**Phase 0·本地试点就绪（现有形态，本周可跑）**
1. 通道 v1=网页进线：真实客户经 CallStudio 页 WebRTC 进线（网站内嵌/WhatsApp 发链接均可）；SIP 电话线二期，试点无需号码。
2. 规模红线：单机同时在线 ≤4 通（prompt-cache 4-6 路上限 + TTS sidecar 全局串行）。
3. 三件套：`bok.py prod install`（launchd 常驻 KeepAlive）；`BOK_CP_TOKEN`（CP 鉴权，agent/web 同步携带）；app-data 每日异地备份。
4. 铁律：**进线必建对象**（散客 ad-hoc 建档）——无对象通话会被挖掘过滤，等于白采集。
5. 上线前复扫 0909 QA 轮 3 个 P0（web 挂断不结算/模板编辑丢字段/对象下拉 300+）修没修。
6. turns 已全量落库（STT 必存口径）；音频录音（LiveKit egress）试点期默认不开，开时注意客户告知义务。

**Phase 1·每周 30 分钟挖矿循环**
`probe_qa_hit`（命中率+未命中工单）→ `GET /api/reports/qa-pairs`（已滤干净）→ 人工过目 → 补词条（避易丢头韵/按 heard 变体补键）→ `bok.py tts-pregen --qa` → 复测。同一份真实转写喂三引擎：QA 词库、ASR 热词（话术表单）、话术优化（script-insights 视图）。攒 50 通首跑，之后每周。

**Phase 2·真实数据反哺 eou（smart-turn B 档）**
50-100 通后用 turns 里现成的边界做「这句完没完」标注 → 评估官方 TurnDetector（0909 调研：8ms 延迟达标、合成集失真待真集）→ 采纳则 eou ~650ms→~400ms、感知 p50 再砍 ~250ms。

**Phase 3·规模化再上薄节点 SaaS**（P1-P4 已有 spec=2026-09-10-thin-node-saas-design.md，采集期不需要。）

**Phase 1 更新（2026-09-11 qa-sync 落地）**：每周循环从四条命令缩为一条——`python tools/bok.py tts-mine --sync`（挖掘→质量闸→入库→按词条语言物化 TTS→汇报）。质量闸保守（calls≥5/票比≥0.8/问法 4-24 字/无数字串/归一不重复），闸外条目打印供人工否决（DELETE 端点）。首月建议人工过目每次 sync 的入库清单；稳定后可 launchd 周跑全自动。
