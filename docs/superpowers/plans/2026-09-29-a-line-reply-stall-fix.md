# A 线回复哑火修复（治本 v2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 A 线「连打后新会话只有垫音」——治会话生命周期四病灶（挂断不上报→看门狗复活空房→B线退出强杀→mlx cache 顶满）+ 两个正确性 bug + 两个产品行为，探针先行。

**Architecture:** P0 两个探针（生命周期审计=主力、慢速注入=诊断）→ P1 服务端治本（补派免疫 `ever_dispatched` 账本、B线退出守护、cache 参数定档）+ 客户端挂断兜底 → P2 正确性（guard cancel 蒸发、ladder 同级连发）→ P3 产品行为（unclear 推进、nudge 软化）。spec：`docs/superpowers/specs/2026-09-29-a-line-reply-stall-fix-design.md`（v2）。

**Tech Stack:** Python 3.12（FastAPI CP + livekit-agents 1.8.2 worker）、Next.js 静态导出 web、mlx_lm server（homebrew python，`--prompt-cache-bytes` 启动参数）、pytest。

## Global Constraints

- 粤语规范值恒小写 `cantonese`（全栈唯一拼写，`tests/test_cantonese_terminology.py` 门禁）。
- 新增运营 env 一律登记 `tools/bok.py` `_FORWARD_ENV` 表（`tests/test_forward_env.py` 扫描面，未登记即 CI 红）。
- kill-switch 读法与全仓同款 `os.environ.get(...) == "1"` 或 `!= "0"`（跟随各文件既有惯例）。
- 生产代码 Python：`from __future__ import annotations`、类型注解、PEP 8；编辑后 `python -m compileall -q`。
- 日志标记全 `flush=True` print（agent 侧）或 `control_log`（CP 侧），新标记先查 `references/markers.md` 惯例。
- 探针跑前必须 `BOK_LOCAL_TTS=1` 起栈（E2E 渲染客户音直打 :8788）。
- 分支 `fix/a-line-reply-stall-v2`；工作区有 09-28 各波未提交改动（~5100 行）——**不要动它们、不要 stash、不要提交它们**；每次 commit 只 add 本任务清单内的文件。
- 测试跑法：`.venv312/bin/python -m pytest <path> -x -q`；全量 `./scripts/test.sh`。

---

### Task 1: P0.1 会话生命周期审计探针

**Files:**
- Create: `scripts/probe_session_lifecycle.py`
- Create: `reports/session-lifecycle-2026-09-29/`（探针输出目录）

**Interfaces:**
- Consumes: CP REST（`POST /api/calls` 建单、`POST /api/calls/{id}/hangup`、`GET /api/calls/{id}`）、LiveKit ListRooms/ListParticipants（经 `livekit.api`，凭据 env `LIVEKIT_API_KEY/SECRET/URL`）、agent.log tail（`~/Library/Application Support/BokVoice/logs/agent.log`）、llm.log `Prompt Cache:` 行 tail。
- Produces: `reports/session-lifecycle-2026-09-29/report.md`（每通一行时序表 + 违规清单 + cache/GPU 走势）；探针自身退出码 0=全部达标 / 1=有违规（修后验收复用）。

- [ ] **Step 1: 写探针骨架与参数面**

探针结构（单文件，~400 行）：

```python
#!/usr/bin/env python3
"""会话生命周期审计探针（spec 2026-09-29 v2 P0.1）。

复刻真实使用：连打 N 通 simulation call，每通用 CP hangup 端点结束
（--mode hangup，按钮路径）或不断连接直接建下一通（--mode abandon，
关页/刷新路径——病灶复刻臂）。全程审计挂断→ended→room 删→job 退
的全链时延、同房 job 数、mlx cache 走势、GPU 内存。
产出 reports/session-lifecycle-<date>/report.md；退出码 0=达标 1=违规。
"""
import argparse, asyncio, json, os, re, subprocess, time, urllib.request
from datetime import datetime, timezone

LOG_DIR = os.path.expanduser("~/Library/Application Support/BokVoice/logs")
CP = "http://127.0.0.1:8000"

# 审计判据（修复目标，spec §3 表）
TARGETS = {"cp_ended_s": 1.0, "room_deleted_s": 3.0, "job_exit_s": 5.0, "jobs_per_room": 1}
```

参数：`--calls 10`（默认）、`--mode hangup|abandon`、`--interval 15`、`--out reports/session-lifecycle-2026-09-29`。凭据从 env 读（与 `tools/bok.py` 同源：`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET`/`LIVEKIT_URL`）。

- [ ] **Step 2: 实现三路采集器**

①CP 轮询器：`GET /api/calls/{id}`（Bearer `BOK_CP_TOKEN` 或 auth-off 直打）至 `status == "ended"`，记录挂断→ended 秒数。
②LiveKit 轮询器：`livekit.api.LiveKitAPI`（`pip` 包已在 venv）`room.list_rooms`/`list_participants`，至房间消失，记录秒数；每通记录观测期内的 `job request` 计数与 `dispatch_id` 集合。
③日志 tail 采集：建单前记 `agent.log` 行偏移；结束后扫窗口统计每房 `received job request`/`process exiting`；`llm.log` 窗口内 `Prompt Cache: (\d+) sequences, ([\d.]+) GB` 峰值。
④GPU 采样：`subprocess` 起 `powermetrics --samplers gpu_power -i 1000`（无权限则降级 `memory_pressure` 快照，探针打一行 `gpu_sampler=fallback`）。

- [ ] **Step 3: 实现两模式主循环与判定**

`--mode hangup`：建单→等 agent session_started（agent.log 偏移窗口）→ 推 2 轮合成语音（复用 `probe_flow_20rounds.py` 的 erc 建单与 TTS 客户端函数，import 而非复制；如不可 import 则提取最小等价实现）→ `POST /api/calls/{id}/hangup` → 审计开始。
`--mode abandon`：建单→session_started→**不挂断**，直接 `await asyncio.sleep(3)` 建下一通（复刻关页/刷新）——本臂预期抓「job 退出→看门狗补派 job2」违规（修复前基线）。
判定表逐项对比 TARGETS，违规写 `report.md`（房号、项、实测值、目标值）。

- [ ] **Step 4: 手动实弹跑通（栈在跑）**

Run: `cd ~/Documents/bok/voice-assistant && .venv312/bin/python scripts/probe_session_lifecycle.py --calls 5 --mode hangup --out /tmp/lc_smoke`
Expected: 产出 report.md，5 通行齐全；hangup 臂各项 < 目标（既有链路应过）；`--calls 3 --mode abandon` 臂抓到 ≥1 个 `jobs_per_room=2` 违规（修复前基线实锤）。

- [ ] **Step 5: Commit**

```bash
git add scripts/probe_session_lifecycle.py
git commit -m "probe: 会话生命周期审计探针（P0.1，挂断/弃置两臂全链时延+资源归位）"
```

---

### Task 2: P0.2 慢速注入诊断探针

**Files:**
- Create: `scripts/probe_llm_stall.py`
- Create: `reports/llm-stall-replay/`（输出目录）

**Interfaces:**
- Consumes: 无外部接口依赖（内嵌限速代理 + Task 1 的建单/推音频基建函数 `build_call_and_push_rounds`——从 Task 1 探针 import）。
- Produces: `reports/llm-stall-replay/{timeline.md, pyspy_dump_<pid>.txt}`；`main() -> int`（0=复现并抓到栈 / 2=未复现=记录后正常退出）。

- [ ] **Step 1: 写限速 SSE 代理（探针内嵌函数）**

```python
async def throttled_proxy(listen_port: int, upstream: str, tps: float):
    """SSE 流式响应逐 event 注入 delay 的透明代理。

    delay = event 字符数 / (tps * 1.6)（中文 ~1.6 char/token 均摊）;
    非 /v1/chat/completions 路径与请求直通。aiohttp 双端。
    """
```

健康检查与模型列表直通（agent 装配期 `_fetch` 走 :1235 的 `/v1/models` 探测）。

- [ ] **Step 2: 写自动抓栈器**

```python
def pyspy_dump(tag: str) -> str | None:
    """ps 匹配 agent_runtime job 子进程（worker 父进程名含 agent_runtime.main），
    对其 job 子进程逐个 py-spy dump（--pid，homebrew 路径 /opt/homebrew/bin/py-spy），
    落 reports/llm-stall-replay/pyspy_dump_{tag}_{pid}.txt。"""
```

- [ ] **Step 3: 主循环与判据**

驱动 3 轮（身份→「吃什么包裹啊？」→「系什么包裹？」，复用 Task 1 基建）；日志 watcher 扫 `MINIMAX_BIDI_PERF sentences=0 canceled=1` 或「commit 后 6s 无 `TTS_FIRST_AUDIO_MS`」→ 立即 `pyspy_dump("stall")` + 时间线归因（该轮 agent.log 窗口原文落 timeline.md）。doctor 前置：检查 serve 进程 env 含 `MLX_LLM_BASE_URL=http://127.0.0.1:12399/v1`（本探针代理口），否则打印重启指引退出码 1。

- [ ] **Step 4: 实弹跑（栈需以代理为 LLM 入口重启）**

Run: `MLX_LLM_BASE_URL=http://127.0.0.1:12399/v1 BOK_LOCAL_TTS=1 python tools/bok.py serve` + 另终端 `.venv312/bin/python scripts/probe_llm_stall.py --tps 2.6`
Expected: 复现 sentences=0 形态 + pyspy 栈落盘（或未复现则 timeline.md 记录正常形态，退出 2——两种结果都是有效产出，写入报告）。

- [ ] **Step 5: Commit**

```bash
git add scripts/probe_llm_stall.py
git commit -m "probe: 慢速注入诊断探针（P0.2，2.6tps 限速代理+py-spy 自动抓栈）"
```

---

### Task 3: P1.b 补派免疫——ever_dispatched 账本（服务端根治）

**Files:**
- Modify: `apps/control-plane/control_plane/main.py:2532-2622`（`_dispatch_watchdog_attempt`）
- Modify: `apps/control-plane/control_plane/dispatch_utils.py`（新增账本函数）
- Test: `tests/test_dispatch_watchdog.py`（既有文件追加）

**Interfaces:**
- Consumes: 既有 `list_dispatches_safe(lkapi, room) -> list | None`、`dispatch_is_zombie(d, attempt, now_s)`、`_repo().get_call(room)`、`_TERMINAL_CALL_STATUSES`。
- Produces: `dispatch_utils.mark_dispatch_alive(room: str) -> None`、`dispatch_utils.has_dispatched_before(room: str) -> bool`（模块级 dict 账本，进程内存态；watchdog 每次 `create_dispatch` 成功后 mark，attempt 入口查）；观测行 `dispatch.watchdog.suppress_ever_dispatched`（control_log.warning 同款结构）。

**根因（写入实现注释）**：job 正常结束 → 其 dispatch 变「全终态空壳」→ `dispatch_is_zombie` 清扫删除 → 存在性判据变 False → 看门狗补派 job2 进空房（2026-09-29 三例：803e44ad/8c25aa8a/9f2ff7a3，job1 退→3s 后新 dispatch_id 补位，挂到回收器）。语义修正：**每通通话至多一个 agent 生命周期——看门狗只救冷启动窗（从未入房），不复活已结束的**。

- [ ] **Step 1: 写失败测试**

```python
def test_watchdog_suppresses_revive_after_normal_exit():
    """job 正常结束（dispatch 全终态被清扫）后，真人仍在房 → 不补派。
    2026-09-29 三例 job2 复活空房的回归钉。"""
    # 替身 lkapi：list_participants 返回真人一个；list_dispatches 返回
    # 全终态空壳一条（zombie 可清）；create_dispatch 计数。
    mark_dispatch_alive("call-x")  # 曾派过（冷启动窗内 agent 已入房）
    code = asyncio.run(_dispatch_watchdog_attempt(fake_lkapi, "call-x", attempt=1))
    assert code == "suppress_ever"  # 新结局码
    assert fake_lkapi.created == 0

def test_watchdog_still_saves_cold_start_never_joined():
    """从未入房（无 alive 标记）+ 真人在场 → 照常补派（冷启动窗语义保留）。"""
    code = asyncio.run(_dispatch_watchdog_attempt(fake_lkapi, "call-y", attempt=1))
    assert code == "created"
    assert fake_lkapi.created == 1

def test_ever_dispatched_book_is_per_room():
    mark_dispatch_alive("call-a"); assert has_dispatched_before("call-a")
    assert not has_dispatched_before("call-b")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv312/bin/python -m pytest tests/test_dispatch_watchdog.py -x -q`
Expected: FAIL（`mark_dispatch_alive` 不存在）

- [ ] **Step 3: 实现**

`dispatch_utils.py` 追加：

```python
# ever_dispatched 账本（2026-09-29 生命周期治本）：room → agent 曾真实入房。
# 看门狗只救「从未入房」的冷启动窗；job 入房过再退出（正常结束/崩溃）=本通
# agent 生命周期已尽，永不复活——复活=接手客户已放弃的空房空转到回收器
# （2026-09-29 三例 job1 退→3s 补 job2 实证）。进程内存态：CP 重启清零
# 保守（重启后 watchdog 对在途通话失去记忆，最坏回到旧补派行为，不劣化）。
_EVER_DISPATCHED: set[str] = set()


def mark_dispatch_alive(room: str) -> None:
    if room:
        _EVER_DISPATCHED.add(room)


def has_dispatched_before(room: str) -> bool:
    return room in _EVER_DISPATCHED
```

`main.py` `_dispatch_watchdog_attempt` 改两处：
①入锁前（list_participants 判 agent 在场处）：agent 在场时 `mark_dispatch_alive(room)`（复用现有 `recovered` 分支前）；
②清扫后存在性判据的 else 支路（即将 `create_dispatch` 前）加：

```python
            if has_dispatched_before(room):
                control_log.warning(
                    "dispatch_watchdog_suppress_ever_dispatched",
                    extra={"event": "dispatch.watchdog.suppress_ever_dispatched",
                           "data": {"room": room, "attempt": attempt}},
                )
                return "suppress_ever"
```

`dispatch_utils.__all__` 追加两个名字（文件头 `34: "has_dispatch_record"` 惯例）。
另：`_TERMINAL_CALL_STATUSES` 判定保持在前（ended 恒 terminal，双保险不变）。

- [ ] **Step 4: 跑测试通过 + 全文件回归**

Run: `.venv312/bin/python -m pytest tests/test_dispatch_watchdog.py -q`（新旧全绿）+ `python -m compileall -q apps/control-plane/control_plane/`

- [ ] **Step 5: 实弹验证（Task 1 探针 abandon 臂复跑）**

Run: `.venv312/bin/python scripts/probe_session_lifecycle.py --calls 3 --mode abandon --out /tmp/lc_after_t3`
Expected: `jobs_per_room` 恒 1；agent.log 出现 `dispatch.watchdog.suppress_ever_dispatched`（CP 日志 control-plane.log）。

- [ ] **Step 6: Commit**

```bash
git add apps/control-plane/control_plane/main.py apps/control-plane/control_plane/dispatch_utils.py tests/test_dispatch_watchdog.py
git commit -m "fix: 补派免疫——ever_dispatched 账本，看门狗不复活已结束通话（P1.b）"
```

---

### Task 4: P1.a 客户端挂断兜底（beforeunload beacon）+ 挂断链打点

**Files:**
- Modify: `apps/web/components/CallStudio.tsx:941-981`（`leave()` 函数区）
- Test: 无独立测试文件（web 无 CI 测试面）；验证=build + 手动实弹

**Interfaces:**
- Consumes: 既有 `api.hangup(id)`（`lib/api.ts:260`）。
- Produces: `hangupBeacon(callId)`（组件内函数）——`navigator.sendBeacon` 发 `POST /api/calls/{id}/hangup`（Blob + `application/json` + Bearer 头不可用——beacon 无自定义头；**CP hangup 已有 `_gate_page` auth-off 本机直通路径**，beacon 走无头档；auth-on 部署 beacon 不达=接受，服务端 Task 3 已兜底）。

- [ ] **Step 1: 实现 beacon 兜底**

`CallStudio.tsx` `leave()` 上方新增并挂载：

```tsx
// 挂断兜底（spec v2 P1.a）：关页/刷新不经过 leave() 的路径，用 sendBeacon
// 补发 hangup。beacon 无法带 auth 头——auth-off 本机/局域网档直达；auth-on
// 部署 beacon 可能 401，此路径只作 best-effort，服务端 ever_dispatched
// （P1.b）才是根治。重复 hangup 幂等（CP update_call 已 ENDED 短路）。
function hangupBeacon(callId: string) {
  if (!callId || typeof navigator.sendBeacon !== "function") return;
  try {
    const base = new URL(apiBaseUrl(), window.location.origin).toString().replace(/\/$/, "");
    navigator.sendBeacon(`${base}/api/calls/${callId}/hangup`, new Blob(["{}"], { type: "application/json" }));
  } catch { /* best-effort */ }
}
useEffect(() => {
  const onHide = () => hangupBeacon(callIdRef.current);
  window.addEventListener("pagehide", onHide);
  return () => window.removeEventListener("pagehide", onHide);
}, []);
```

`apiBaseUrl` 从 `lib/api.ts` 既有 base 解析函数复用（若无则从 `api` 模块的 request 实现里取常量——**实现时先读 api.ts 的 base 解析，用现成的，不新造**）。

- [ ] **Step 2: build 验证**

Run: `cd apps/web && npm run build`
Expected: 零 error（tsc 过）。

- [ ] **Step 3: 实弹验证**

栈在跑 + web dev 静态导出服务；浏览器开 CallStudio 接通一通 → **直接关标签页** → 10s 内查 `control-plane.log` 出现 `call.hangup` 审计、LiveKit 房间消失、agent job `process exiting`。

- [ ] **Step 4: Commit**

```bash
git add apps/web/components/CallStudio.tsx
git commit -m "fix: 关页/刷新 sendBeacon 兜底 hangup（P1.a 客户端臂）"
```

---

### Task 5: P1.c B 线 job 退出路径守护

**Files:**
- Modify: `apps/agent/agent_runtime/interpret.py:1270-1275`（`closed.wait()` 区）+ shutdown 清理链各 await 点
- Test: `tests/test_interpret_exit_guard.py`（新文件）

**Interfaces:**
- Consumes: 无。
- Produces: `_guarded_await(coro, stage: str, timeout: float = 3.0) -> None`（interpret.py 模块级助手——`asyncio.wait_for` 包装，超时打 `interp.exit_slow stage=<stage> ms=N` 后放弃该步继续）；shutdown 链逐 stage 包裹。

- [ ] **Step 1: 写失败测试**

```python
def test_guarded_await_times_out_and_logs(capsys):
    async def hang(): await asyncio.sleep(30)
    async def main():
        await _guarded_await(hang(), stage="tts_close", timeout=0.05)
    asyncio.run(main())  # 不抛
    assert "interp.exit_slow stage=tts_close" in capsys.readouterr().out

def test_guarded_await_passes_through():
    async def ok(): return 7
    assert asyncio.run(_guarded_await(ok(), stage="x")) in (None, 7)
```

- [ ] **Step 2: 跑失败** → Run: `.venv312/bin/python -m pytest tests/test_interpret_exit_guard.py -x -q` → FAIL（`_guarded_await` 未定义）

- [ ] **Step 3: 实现并接入**

`_guarded_await` 落 interpret.py；**先读 `entrypoint` 的 session 关闭与 shutdown 回调现状**（`session.on("close")` 后的资源清理：TTS bidi 会话、ASR 流、ws 协程），对每个裸 `await` 清理调用包 `_guarded_await(..., stage="<具体资源名>")`。以实际代码为准逐点包——探针/实弹观测 `process did not exit in time` 的 stage 即为修复点（修前 unknown，修后打点定位慢点并守护）。

- [ ] **Step 4: 跑过 + compileall**

- [ ] **Step 5: 实弹验证**：B 线连打 3 通挂断，`grep "did not exit in time" interp-*.log` 零新增、`interp.exit_slow` 打点（若有慢点）可见。

- [ ] **Step 6: Commit**

```bash
git add apps/agent/agent_runtime/interpret.py tests/test_interpret_exit_guard.py
git commit -m "fix: B线 job 退出路径逐点 wait_for 守护（P1.c，killing process 根治）"
```

---

### Task 6: P1.d mlx prompt cache 参数定档（A/B 探针裁定）

**Files:**
- Modify: `tools/bok.py:1370-1420`（`--prompt-cache-bytes` 档位函数——**先读现状**：三级优先级 env `BOK_LLM_PROMPT_CACHE_BYTES` > preset > 内存分档）
- Test: `tests/test_prompt_cache_tier.py`（若既有档位测试文件则追加；先 grep `prompt_cache` tests/ 定位）

**Interfaces:**
- Consumes: 既有档位函数（`_prompt_cache_bytes_tier` 类命名以实际为准）。
- Produces: 单通档默认 6GB→**4GB**、`--prompt-cache-size` 128→**48**（`tools/bok.py:1415` 参数行）。

- [ ] **Step 1: A/B 数据采集（Task 1 探针跑两档）**

6GB 档（现状）跑 Task 1 `--calls 10 --mode hangup` 记 cache 峰值/走势与各通 LLM_TTFT；改参数重启栈同跑 4GB 档对比。
判据：4GB 档 cache 峰值 ≤4GB 且 TTFT 无单调回归（`cached=/` 前缀命中率不明显掉）→ 定 4GB；命中率显著掉 → 5GB 复测；仍掉 → 保持 6GB 且 report 记录「LRU 换出抖动」结论。

- [ ] **Step 2: 改档位默认值 + 测试**

测试钉档位函数返回 4GB 默认（env 覆盖路径不变）。

- [ ] **Step 3: 跑过 + compileall + Commit**

```bash
git add tools/bok.py tests/test_prompt_cache_tier.py
git commit -m "tune: mlx prompt cache 6GB→4GB/128→48 条（P1.d，A/B 探针定档）"
```

---

### Task 7: P2.a guard cancel 缓冲蒸发修复

**Files:**
- Modify: `apps/agent/agent_runtime/providers/livekit_plugins.py:1492-1640`（`_RepeatSelfGuardStream`）
- Modify: `apps/agent/agent_runtime/agent.py:7111-7115`（`_watch` 补账点读缓冲）
- Test: `tests/test_repeat_guard_cancel.py`（新文件）

**Interfaces:**
- Consumes: `_RepeatSelfGuardStream(plugin, inner, last_reply, *, bypass, ledger, threshold, allow_repeat)` 既有签名不变。
- Produces: `_RepeatSelfGuardStream.pending_buffer` 属性（`str`，cancel 时残留缓冲）；打点 `REPEAT_GUARD_CANCEL_DROP chars=N`；`_watch` 补账 transcript = `_reply_partial["text"]` + `pending_buffer`（取自当轮 stream 实例——agent 侧经 `ContextAwareLLM` 暴露最近流实例的引用 `last_guard_stream`，chat() 时置位，`_watch` 读；`None` 安全）。

- [ ] **Step 1: 写失败测试**

```python
def test_cancel_preserves_pending_buffer(capsys):
    """流中途 cancel：缓冲文本进 pending_buffer + 打点；正常流完 pending_buffer 空。"""
    # 假 inner：yield 3 个 chunk（无句界）后挂起；外层 cancel
    guard = _RepeatSelfGuardStream(fake_plugin, inner, last_reply="你好", bypass=False)
    task = asyncio.create_task(guard._run())
    await asyncio.sleep(0.01); task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert guard.pending_buffer != "" or "REPEAT_GUARD_CANCEL_DROP" in capsys.readouterr().out
```

（具体 fake_plugin/inner 替身形状参考既有 `tests/test_repeat_cross_turn.py` 的替身写法——实现时先读该文件复用其 fixture 风格。）

- [ ] **Step 2: 跑失败** → FAIL

- [ ] **Step 3: 实现**

`_run` 的 `async for` 外包 `try/except asyncio.CancelledError`：

```python
    async def _run(self):
        try:
            async for ev in self._inner:
                ...（现状体不动）
            tail = self._flush_at_end()
            ...（现状体不动）
        except asyncio.CancelledError:
            if self._buf:
                print(f"REPEAT_GUARD_CANCEL_DROP chars={len(self._buf)}", flush=True)
            raise
```

`__init__` 加 `self.pending_buffer = ""`；`_feed`/`_flush_at_end` 更新处同步维护（简单做法：`pending_buffer` 做成 property 返回 `self._buf`——零维护成本，取消用该写法）。ContextAwareLLM 持最近 guard 流引用 + agent `_watch` 拼接（见 Interfaces）。

- [ ] **Step 4: 跑过 + 既有回归**（`test_repeat_cross_turn.py`/`test_repeat_self_guard*.py` 全绿——**先 grep 实际文件名**）

- [ ] **Step 5: Commit**

```bash
git add apps/agent/agent_runtime/providers/livekit_plugins.py apps/agent/agent_runtime/agent.py tests/test_repeat_guard_cancel.py
git commit -m "fix: guard cancel 缓冲不再蒸发——pending_buffer 落账本+打点（P2.a）"
```

---

### Task 8: P2.b stall ladder 同级连发修复

**Files:**
- Modify: `apps/agent/agent_runtime/flow.py`（`FlowController`——`__init__`/`advance()`/`jump_to()` 加 `ladder_fired` 维护）
- Modify: `apps/agent/agent_runtime/agent.py:6006-6045`（发射条件与退役顶 streak）
- Test: `tests/test_stall_ladder.py`（既有文件追加）

**Interfaces:**
- Consumes: 既有 `stall_ladder_level(streak)`、`STALL_DEGRADE_N/STALL_BYPASS_N/STALL_CLOSE_N`、`flow_ctrl.step_streak`。
- Produces: `FlowController.ladder_fired: set[str]`（`advance()`/`jump_to()` 清空——两函数现状体末尾加 `self.ladder_fired.clear()`）。

- [ ] **Step 1: 写失败测试（钉 2026-09-29 形状）**

```python
def test_same_level_not_fired_twice_across_user_turns():
    """streak=3 发 degrade 后 streak=4 轮不得再发 degrade（call-ed6aa9b8
    17:30:37/17:30:46 同句两发回归钉）；streak=5 发 bypass；换步后同级别可再发。"""
    fc = FlowController.from_template(_tpl_3steps())  # 复用既有测试的模板 fixture
    fc.step_streak[0] = 3; fc.ladder_fired.add("degrade")
    # agent 侧发射条件的纯函数化判定（若发射在 agent.py 内联，则测 flow 侧账本 +
    # agent 侧条件表达式提取为 flow.py 纯函数 ladder_should_fire(fired, level)->bool）
    assert ladder_should_fire(fc.ladder_fired, "degrade") is False
    assert ladder_should_fire(fc.ladder_fired, "bypass") is True
    fc.advance(); assert fc.ladder_fired == set()
```

- [ ] **Step 2: 跑失败** → FAIL（`ladder_fired` 不存在）

- [ ] **Step 3: 实现**

`flow.py`：`FlowController.__init__` 加 `self.ladder_fired: set[str] = set()`；`advance()`/`jump_to()` 末尾 `self.ladder_fired.clear()`；新增纯函数：

```python
def ladder_should_fire(fired: set[str], level: str) -> bool:
    """同级不连发（2026-09-29 v2 P2.b）：本步已发射过的级别不再发——
    旧「顶 streak 到下一级门槛-1」数值魔术有洞（degrade 后 streak=4 仍判
    degrade，call-ed6aa9b8 同句两发实证）。"""
    return level not in fired
```

`agent.py` 发射段：`if _lvl and ladder_should_fire(flow_ctrl.ladder_fired, _lvl):` 包住现有发射体；发射成功路径 `flow_ctrl.ladder_fired.add(_lvl)`；**删除**「发射后顶 streak」两行（`flow_ctrl.step_streak[...] = STALL_CLOSE_N - 1 if ...` 段）。close 级不加同级门（`enter_closing()` 天然幂等，块外 `not flow_ctrl.closing` 已拦）。

- [ ] **Step 4: 跑过 + 既有 stall 测试全绿**

- [ ] **Step 5: Commit**

```bash
git add apps/agent/agent_runtime/flow.py apps/agent/agent_runtime/agent.py tests/test_stall_ladder.py
git commit -m "fix: stall ladder 同级连发——ladder_fired 账本替顶 streak 数值魔术（P2.b）"
```

---

### Task 9: P2.c LLM 慢速观测行（纯打点）

**Files:**
- Modify: `apps/agent/agent_runtime/providers/livekit_plugins.py`（MlxLlmLLM 流结束处——TTFT 总结行同点）
- Modify: `tools/bok.py`（`_FORWARD_ENV` 表加 `BOK_LLM_STALL_OBS_TPS`）
- Test: `tests/test_llm_stall_obs.py`（新文件）

**Interfaces:**
- Consumes: MlxLlmLLM 流内既有 token 计时（TTFT/tps 计算点）。
- Produces: 流完成时若平均 tps < 阈值打 `LLM_STALL_OBS tps=<n> gen=<n> window_s=<n>`；env `BOK_LLM_STALL_OBS_TPS`（默认 `"5"`，`"0"`=关）。

- [ ] **Step 1: 失败测试**（FakeLLM 流注入慢 chunk 间隔，断言打点；快流断言无打点；env 0 断言无打点）
- [ ] **Step 2: 跑失败** → FAIL
- [ ] **Step 3: 实现**（TTFT 总结行旁加判定——复用其 tps/gen 变量；`_FORWARD_ENV` 加行）
- [ ] **Step 4: 跑过 + `tests/test_forward_env.py` 全绿**
- [ ] **Step 5: Commit** `git add ... && git commit -m "obs: LLM_STALL_OBS 慢速观测行（P2.c，纯打点不处置）"`

---

### Task 10: P3.1 unclear 连续推进

**Files:**
- Modify: `apps/agent/agent_runtime/flow.py`（`FlowController`：`unclear_streak` 账本 + `advance()`/`jump_to()` 清零 + `relieve_unclear_streak()`）
- Modify: `apps/agent/agent_runtime/agent.py`（judge `route=keep` 消费点——`judge(bg)=` 行区，先 grep 定位现状分支结构）
- Modify: `tools/bok.py`（`_FORWARD_ENV` 加 `BOK_UNCLEAR_ADVANCE`/`BOK_UNCLEAR_ADVANCE_N`）
- Test: `tests/test_unclear_advance.py`（新文件）

**Interfaces:**
- Consumes: judge 消费点既有结构（`jv`/`route` 变量、`_route_log` 惯例、`flow_ctrl` 实例）。
- Produces: `FlowController.unclear_streak: dict[int, int]`（per-step，同 `step_streak` 形态）；`relieve_unclear_streak()`（实答轮挂点=`relieve_stall_streak()` 同一调用处旁边）；日志 `[flow] unclear-advance step=N streak=N`；审计 `flow.unclear_advance`（detail: `{"streak": n, "step": new}`）。

- [ ] **Step 1: 写失败测试**

```python
def test_unclear_streak_bumps_on_keep_and_advances_at_threshold():
    fc = FlowController.from_template(_tpl_3steps())
    fc.bump_unclear_streak()          # 轮1 keep
    fc.bump_unclear_streak()          # 轮2 keep
    assert fc.unclear_at(0) == 2
    fc.bump_unclear_streak()          # 轮3 keep → 消费点判推进
    assert unclear_should_advance(fc, threshold=3) is True

def test_unclear_relieved_by_real_answer():
    fc.bump_unclear_streak(); fc.relieve_unclear_streak()
    assert fc.unclear_at(0) == 0  # 抵销（0 下限）

def test_unclear_cleared_on_advance():
    fc.bump_unclear_streak(); fc.advance()
    assert fc.unclear_at(0) == 0 and fc.unclear_at(1) == 0

def test_unclear_not_advance_on_last_step_or_closing():
    # 最后一步（current == len-1）与 closing 时不推进（消费点门）
    ...
```

（`bump_unclear_streak`/`unclear_at`/`unclear_should_advance` 的最终命名以 flow.py 既有 `step_streak` 家族风格对齐——实现时读 `bump_stall` 的形态复刻，保持同款 API 形状；测试与实现同步定名。）

- [ ] **Step 2: 跑失败** → FAIL
- [ ] **Step 3: 实现 flow 侧 + agent 消费点**

agent judge `route=keep` 分支（现状打 `judge(bg)=... route=keep` 行处）追加：

```python
                    if route == "keep":
                        flow_ctrl.bump_unclear_streak()
                        _n = int(os.environ.get("BOK_UNCLEAR_ADVANCE_N", "3") or 3)
                        if (
                            os.environ.get("BOK_UNCLEAR_ADVANCE", "1") == "1"
                            and unclear_should_advance(flow_ctrl, threshold=_n)
                            and flow_ctrl.has_steps
                            and flow_ctrl.current < len(flow_ctrl.steps) - 1  # 非最后一步
                            and not flow_ctrl.closing and not flow_ctrl.done
                        ):
                            flow_ctrl.advance()
                            flow_ctrl.unclear_clear_all()
                            _invalidate_stale_preemptive("unclear 连续 → 推进")
                            context_state.set_flow_current(flow_ctrl)
                            print(f"[flow] unclear-advance step={flow_ctrl.current + 1} streak={_n} (call {room_name})", flush=True)
                            _audit_flow("flow.unclear_advance", {"streak": _n, "step": flow_ctrl.current + 1})
```

（`set_flow_current`/`_invalidate_stale_preemptive`/审计调用形态——**实现时对照同文件 `rule=auto` 推进段的既有三件套写法逐字对齐**，含 WA 步门：推进前若 `_looks_like_whatsapp_step` 且未 captured 则不推，照抄 auto 段的对应守卫。）`relieve_unclear_streak()` 挂 `relieve_stall_streak()` 的调用点旁（`_report_assistant_turn` 内 gen∈{llm,qa_fastpath} 同判据）。

- [ ] **Step 4: 跑过 + `_FORWARD_ENV` 测试绿 + compileall**
- [ ] **Step 5: Commit** `git commit -m "feat: unclear 连续 N 轮推进（P3.1，BOK_UNCLEAR_ADVANCE）"`

---

### Task 11: P3.2 心跳 nudge 软化

**Files:**
- Modify: `apps/agent/agent_runtime/agent.py:460-483`（`_nudge_line`）与 `:6979`（`SILENCE_NUDGE_SECONDS` 默认值）
- Modify: `scripts/pregen_tts.py`（`--nudge` 批量预合成臂——**先读现状参数结构**追加）
- Test: `tests/test_nudge_lines.py`（新文件）

**Interfaces:**
- Consumes: `_nudge_line(name, lang, count)` 签名不变。
- Produces: 新三语文案（spec §6 表逐字）；`SILENCE_NUDGE_SECONDS` 默认 `"12"`；pregen `--nudge`（按账号 roster 对象 display_name 渲染三句变体入缓存）。

- [ ] **Step 1: 写失败测试**（pin 新文案三语三句、`count` 轮换序、默认间隔 `12`——读 env 默认值的既有读法断言）
- [ ] **Step 2: 跑失败** → FAIL
- [ ] **Step 3: 实现文案+间隔**（spec §6 表逐字入 `_nudge_line`；`6979` 行 `"8"`→`"12"`）
- [ ] **Step 4: 跑过** + pregen `--nudge` 实现（读 `scripts/pregen_tts.py` 现有 `--qa`/`--branch-status` 臂形态复刻：拉账号对象 → 渲染 `_nudge_line(name, lang, i)` → 逐条入 tts-cache）+ 手动跑 `python tools/bok.py tts-pregen --nudge` 冒烟
- [ ] **Step 5: 耳测样本**：新旧文案各三语两音色合成落 `~/Desktop/nudge_ab/`（新 = `mcp minimax text_to_audio` 或 pregen 产物；旧 = tts-cache 里既有条目导出）——**留给 Ethan 拍板，不阻塞本任务 commit**
- [ ] **Step 6: Commit** `git commit -m "feat: 心跳 nudge 三语软文案+间隔 8→12s+pregen --nudge（P3.2）"`

---

### Task 12: 全量验收

**Files:** 无新文件（验证任务）

- [ ] **Step 1: 全量测试**：`./scripts/test.sh` → 全绿（基线 3589+ 新增 ~25）
- [ ] **Step 2: FLOW20**：`BOK_LOCAL_TTS=1 .venv312/bin/python scripts/probe_flow_20rounds.py` → 20/20 零坏标记
- [ ] **Step 3: 生命周期复跑**：Task 1 探针 hangup+abandon 两臂 → 全项达标（job 恒 1、job_exit <5s、suppress_ever_dispatched 打点在、cache 峰值 ≤ 定档值）
- [ ] **Step 4: soak 双语**：`probe_latency_soak` canto/zh → PERCEIVED p50 ≤3000ms 无回归
- [ ] **Step 5: 产出归档**：`reports/session-lifecycle-2026-09-29/`（修前基线+修后对照）、`reports/llm-stall-replay/`、A/B 定档结论写回 spec 文件尾部「验收记录」节
- [ ] **Step 6: Commit** `git commit -m "test: v2 治本全量验收（生命周期达标+FLOW20+soak）"`

---

## Self-Review 记录

- **Spec 覆盖**：P0.1→T1、P0.2→T2、P1.a→T4、P1.b→T3、P1.c→T5、P1.d→T6、P2.a→T7、P2.b→T8、P2.c→T9、P3.1→T10、P3.2→T11、验收→T12。spec §8 回滚表各 kill-switch/参数在对应 task 落地。✓
- **占位扫描**：T5 Step 3「以实际代码为准逐点包」与 T10「对照 auto 段逐字对齐」是**实现时读码对齐指令**（含明确的读哪个文件哪段），非 TBD。✓
- **类型一致**：`mark_dispatch_alive`/`has_dispatched_before`（T3 定义，T3 消费）；`pending_buffer`（T7 定义消费同 task）；`ladder_fired`/`ladder_should_fire`（T8）；`unclear_streak` 家族（T10 flow↔agent↔test 同名）。✓
- **顺序依赖**：T1→T2（基建 import）、T1→T3 Step 5（abandon 臂验收）、T3 先于 T4（服务端根治优先，客户端兜底后置）、T6 依赖 T1 探针数据。其余独立。
