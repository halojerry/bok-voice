"""话术图（flow graph）实弹验收（spec 2026-09-18-qa-flow-graph.md §8）。

场景（真栈 + TTS 现场合成推流，骨架复用 e2e_real_customer / probe_latency_soak）：

1. 经 CP API 建带图模板（6 步 + `graph_json`）：意图「投诉」→ `jump_step 4`；
   意图「退款」→ `play_qa <现存 QA 条目 id>`（找不到条目则该腿跳过；罐头未物化时
   运行时走 `FLOW_GRAPH play_miss` 降级，**只记信息位**）。
2. 建模拟通话（建单快照该模板）→ 等开场白 → 推触发语「我要投诉」→ 断言：
   agent.log 窗口内出现 `FLOW_GRAPH jump`（step=4）且后续 assistant turns 行
   `provider=graph-jump` / `template_step=4`。
3. 推非触发语「好的好的」→ 断言该窗口内**零**新增 `FLOW_GRAPH` 行。
4. （有绑定 qa_id 时）推「我要退款」→ 信息位：`play` 或 `play_miss`，不进主判据。
4b. `--then-jump` 档（Phase 3.3 追问链）：轮次表换成「播」+「跳后」两轮，「我要退款」
   触发 `play_qa`（绑定带 then_jump=4）→ 罐头播完**当场**跳步；硬判据 =
   ① play 窗口内 `FLOW_GRAPH play`（`play_miss` → FAIL）② **OR**（同轮
   `FLOW_GRAPH jump … via=then_jump` 日志 · 跳后轮 assistant 行
   `template_step == 4`）。**为什么要两轮**（勘误预检 2）：播放轮本体行在**跳之前**
   落库（`_qa_canned_say` 内上报，步号=跳前步），跳后步号只能在**下一轮**的 turns 行
   上看到；跳后轮话术默认 `AFTER_TEXT`（刻意避开 `_CONFIRM_RE` 单字确认/收线/异议/
   提问七族词）只为信息位服务。**无 QA 条目 → 腿显式跳过**（报告
   `{"skipped": "no_qa_or_audio"}`、退出码 1，未评估 ≠ PASS）；**条目在场但音频未物化
   → 腿照跑**，运行时落 `FLOW_GRAPH play_miss`，play_logged 硬 FAIL、退出码 1
   （先 `python tools/bok.py tts-pregen --qa` 物化音频再来）。kill 腿同旧：
   `--then-jump --expect-off`，须先以 `BOK_FLOW_GRAPH=0` 重启 serve。
4c. `--intent-judge` 档（Phase 3.4 意图引擎，judge 判据补模糊轮）：轮次表 =
   「fuzzy」+「consume」两轮。fuzzy 轮推 `FUZZY_TEXT`（**刻意不含任何触发关键词**的
   强不满抱怨）→ 关键词未中 → 当轮同步打 `FLOW_GRAPH judge_scheduled`（落 fuzzy
   窗口，确定性）；背景任务经 FLOW_JUDGE_DELAY(3s)+9B 判定后打 `judge_hit`（落点
   跨窗口边界，**全腿日志尾全局扫**）。consume 轮（fuzzy 与 consume 之间插入
   `--judge-soak-s`（默认 6s）等候判定落账）推中性话 → 图块求值时 pop pending →
   `judge_pending_fired` + 同轮 `jump step=4` + turn 行 `graph-jump@4`。硬判据：
   judge_scheduled（fuzzy 窗口）+ judge_hit_logged（全局）+ judge_effective
   （`judge_pending_fired` 全局 **且** consume 窗口 jump@4 ∨ turn 行 graph-jump@4）。
   无 QA 依赖（jump_step 绑定）→ 无跳过路径。kill 腿：
   `--intent-judge --expect-off`，须先以 `BOK_FLOW_GRAPH_JUDGE=0` 重启 serve——
   断言全局零 judge_* 打点 + 零图轮 + 零图打点（judge 日志族不进 RE_FLOW_GRAPH，
   kill 断言面必须**单独**把 judge 事件并入，否则 judge 残留会逃过 no_logs）。
   A/B 口径：keyword 腿（默认档 trigger 直中，同轮 jump）vs judge 腿（fuzzy 下一轮
   jump）——同一图、同一目标步，只换触发话语与通路。
5. `--expect-off` 档 = kill-switch 腿：**须先以 `BOK_FLOW_GRAPH=0` 重启 serve**
   （env 在 worker 进程启动时定死，探针不能自己重启；A/B 前后 `ps aux | grep
   agent_runtime` 必须为 0 再 serve，防殭尸 worker 跑旧代码污染结论）→ 同表场景推
   同样触发语 → 断言全程零 `FLOW_GRAPH` 行、零 `graph-jump`/`graph-play` 轮。
   该 env 经 `bok.py` `_agent_worker_env` 白名单透传（2026-09-18 实弹发现并修复）：
   dev `serve` 本身就 merge `os.environ`，该白名单真正兜底的是 prod
   launchd/schtasks 的封闭 env 面——未入表时 prod 命令行 kill-switch 到不了 worker。
   腿 FAIL 时先核对 worker pid env 有没有这个键，再怀疑引擎。

退出码：主判据（②③，`--expect-off` 时⑤）全过 → 0；否则 1。哑轮/play_miss/首声预算
均为信息位（本探针判的是「图跑没跑」，不是延迟/质量）。

起推节奏（2026-09-18 实弹修复）：每轮起推前都等 agent 音轨**真播完**（末尾静音窗 +
累计语音下限；见 `wait_playout_end`）——旧版只等「首声」就推，触发器落进 9.2s 开场白
第 0.5s、麦克风收开场白尾巴 → 转写无触发词 → jump 不触发（首声 0.00s 伪值同源）。

用法：<python> scripts/probe_flow_graph.py [--expect-off] [--lang zh]
      [--trigger-text 我要投诉] [--no-play-round] [--keep-template]
      [--then-jump] [--after-text 我知道了，你说]
      [--intent-judge] [--fuzzy-text …] [--judge-soak-s 6]
      <python> scripts/probe_flow_graph.py --selftest   # 无栈纯函数自检
前置：`python tools/bok.py serve`（CP 8000 / LiveKit 7880 / ASR 8787 / TTS 8788）。
报告：reports/flow-graph/<ts>-<leg>.json。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from livekit import rtc

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

import e2e_real_customer as erc  # noqa: E402  复用骨架:建通/推流/收音/turns/日志路径
import probe_latency_soak as pls  # noqa: E402  复用:百分位/汇总/首声等待

REPORT_DIR = Path(__file__).resolve().parents[1] / "reports" / "flow-graph"

# auth-on 栈（2026-09-15 标准姿势）：CP 请求带机器通道 Bearer，未设 env 零变化。
_CP_HEADERS: dict[str, str] = {}
if os.environ.get("BOK_CP_TOKEN", "").strip():
    _CP_HEADERS["Authorization"] = f"Bearer {os.environ['BOK_CP_TOKEN'].strip()}"

ACCOUNT_ID = os.environ.get("BOK_PROBE_ACCOUNT", "acc-001")
TARGET_STEP = 4  # 1-based：投诉 jump 的目标步
TRIGGER_TEXT = "我要投诉"
NONTRIGGER_TEXT = "好的好的"
PLAY_TEXT = "我要退款"
# 追问链（Phase 3.3）跳后轮话术（勘误预检 2）：只为「跳后步号」信息位服务，刻意避开
# 规则判定的七族词——`_CONFIRM_RE` 单字（好/是/对/嗯/系/係，命中即规则推进 4→5，把
# after 轮步号判据污染成 5）、`_QUESTION_RE`（提问轮原地答不推进但语义漂移）、
# `_DEFER_RE`（社交拖延直念短应承）、`_REFUSE_RE`/`_FAREWELL_RE`/`_HANGUP_RE`（收线
# 冻结后跳步失效）、`_DENY_RE`（异议分支），以及图的两个触发意图词（退款/投诉——撞上
# 会再触发一次图动作）。`tests/test_flow_graph_probe_then_jump.py` 对着真 regex 逐族钉。
AFTER_TEXT = "我知道了，你说"
# ---- 起推前「播完」等候（2026-09-18 实弹修复）--------------------------------
# 根因：旧版只等 `erc.wait_greeting`（首声+累计静音采样，其 silent 从不清零，
# 开场白句间停顿会把采样凑满 → 早返）再 sleep 0.5s 就推触发语——9.2s 的 zh 开场白
# 才念到第 0.5s，麦克风收开场白尾巴 → ASR 无「投诉」→ jump 不触发（首声 0.00s 同源，
# 即开场白尾巴被当成本轮应答）。现在每轮起推前显式等「当前播出段真播完」。
# 判据=末尾静音窗（真实秒）+ 累计语音下限；见 `wait_playout_end` 文档
# （「无新帧」不可用：真栈空闲时音轨仍推帧，实测 ~2× 实时）。
QUIET_GAP_S = float(os.environ.get("BOK_PROBE_QUIET_GAP_S", "1.5"))  # 停嘴判定窗(真实秒)
PLAYOUT_TIMEOUT_S = float(os.environ.get("BOK_PROBE_PLAYOUT_TIMEOUT_S", "25"))
PLAYOUT_MIN_SPEECH_S = float(os.environ.get("BOK_PROBE_PLAYOUT_MIN_SPEECH_S", "0.5"))
PLAYOUT_STABLE_WINDOWS = 3  # 安静后还需连续 3 个采样窗（~0.6s）保持
PLAYOUT_POLL_S = 0.2
# 触发意图关键词：主词 + 同音/近形变体（ASR 是链路最弱环，触发起不来应归因
# 到转写而不是图引擎——见 trigger_transcribed 检查与诊断输出）。
TRIGGER_KEYWORDS = ["投诉", "投訴", "我要投诉", "举报", "索赔"]
PLAY_KEYWORDS = ["退款", "退钱", "退費", "退费", "Refund"]
# ---- 意图 judge 腿（Phase 3.4）------------------------------------------------
# fuzzy 轮话术：**不含任何触发/play 关键词**（图上关键词确定性命中恒先行——命中了就
# 走不到 judge，腿就白跑），但语义是强不满抱怨（判据要能贴上）。声明式短句，避开
# 提问词族（原地答不推进不影响本腿，但话面干净利于归因）。
FUZZY_TEXT = "你们拖了半个月都不处理，太不像话了，我都快气死了"
# 判据（进 graph_json intents[].judge.prompt）：正反例都写清——9B 判定器按此单选。
# 正例锚「强烈不满/抱怨/发脾气/要讨说法（无原词也算）」、反例锚「单纯询问进度/确认
# 信息不算」——fuzzy 转写只要保住抱怨语气即可命中（ASR 碎字容忍度高）。
JUDGE_PROMPT = (
    "客户表达强烈不满：在抱怨、发脾气、质问为什么拖着不处理、要讨个说法，"
    "话里没有说出「投诉」「举报」「索赔」这些原词也算命中；"
    "单纯询问进度、确认信息、客气地催一下不算命中。"
)
# fuzzy→consume 之间的等候（真实秒）：judge 任务 = FLOW_JUDGE_DELAY(3s) + 9B 判定
# 往返（中型判据集 1-3s）——round 的 reply 播放只保证 ~2-6s，判定可能晚于 consume 轮
# 起推才落账 → pending 未存 → consume 轮 pop 空。默认 6s 盖住 delay+往返，env 可调。
JUDGE_SOAK_S = float(os.environ.get("BOK_PROBE_JUDGE_SOAK_S", "6"))

# 探针模板（6 步，纯 goal/ref，无 say 直念步——直念步会在 graph 块之前抢走本轮）。
PROBE_STEPS: list[dict] = [
    {"goal": "确认身份", "ref": "你好，请问是{姓名}吗？"},
    {"goal": "说明来电原因", "ref": "我们这边有一件快递需要跟您确认一下。"},
    {"goal": "询问购买平台", "ref": "请问这件商品是在哪个平台购买的呢？"},
    {"goal": "说明理赔方案", "ref": "如果确认丢件，我们会按平台规则赔付。"},
    {"goal": "引导办理理赔", "ref": "我这边帮您登记办理。"},
    {"goal": "收尾确认", "ref": "好的，感谢您的时间，再见。"},
]

# FLOW_GRAPH 打点四词汇（长词在前防前缀误吃；\b 兜底）
RE_FLOW_GRAPH = re.compile(r"FLOW_GRAPH\s+(play_miss|jump_noop|jump|play)\b(.*)$")
# 意图 judge 打点族（Phase 3.4；**不在** RE_FLOW_GRAPH 里——默认/then-jump 腿的事件集
# 逐字节同旧，judge 事件只由 parse_judge_events 单独收，intent-judge 腿/其 kill 腿用）。
RE_FLOW_GRAPH_JUDGE = re.compile(
    r"FLOW_GRAPH\s+(judge_scheduled|judge_pending_fired|judge_pending_expired"
    r"|judge_hit|judge_miss|judge_skipped|judge\ failed)\b(.*)$"
)
JUDGE_EVENT_KINDS = ("judge_scheduled", "judge_pending_fired", "judge_pending_expired",
                     "judge_hit", "judge_miss", "judge_skipped", "judge failed")
GRAPH_PROVIDERS = ("graph-jump", "graph-play")


# ---------------------------------------------------------------------------
# 纯函数（tests/test_flow_graph_probe.py 直测；--selftest 无栈自检）
# ---------------------------------------------------------------------------
def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


PCM_FRAME_BYTES = 640  # 20ms @ 16kHz/16bit 单声道 = 16000×0.02×2
PCM_BYTES_PER_S = 32000


def trailing_silence_s(pcm: bytes, *, step: int = PCM_FRAME_BYTES, threshold: float = 200.0) -> float:
    """缓冲区**末尾连续静音的真实秒数**（纯函数；RMS≥threshold 判语音，同
    `erc.frame_rms` 口径）。从尾往前扫，撞到第一个语音帧即停——代价 O(尾静音帧数)；
    空缓冲回 0（**不**把「还没出声」误判成「已安静」）。

    注：`erc.speech_stats`/`e2e_edge_cases.wait_silence` 用 320B 步长却按 20ms/步
    计秒，其「秒」实为 ~2× 真实值；本函数用 640B=20ms 真实秒，阈值语义无歧义。
    """
    frames = len(pcm) // step
    silent = 0
    for i in range(frames - 1, -1, -1):
        if erc.frame_rms(pcm[i * step:(i + 1) * step]) >= threshold:
            break
        silent += 1
    return silent * (step / PCM_BYTES_PER_S)


def speech_seconds(pcm: bytes, processed: int = 0, *, step: int = PCM_FRAME_BYTES,
                   threshold: float = 200.0) -> tuple[float, int]:
    """增量统计累计语音秒数（纯函数）：只扫 `processed` 之后的新帧，返回 (新增秒, 新偏移)。"""
    frames = len(pcm) // step
    speech = 0
    i = max(0, processed // step)
    while i < frames:
        if erc.frame_rms(pcm[i * step:(i + 1) * step]) >= threshold:
            speech += 1
        i += 1
    return speech * (step / PCM_BYTES_PER_S), i * step


async def wait_playout_end(
    agent_audio: bytearray,
    *,
    quiet_s: float = QUIET_GAP_S,
    min_speech_s: float = PLAYOUT_MIN_SPEECH_S,
    timeout_s: float = PLAYOUT_TIMEOUT_S,
) -> tuple[float, bool]:
    """等 agent 音轨**当前播出段真播完**（不是「首声」）。返回 (实等秒数, 是否确认安静)。

    机制同 `scripts/e2e_edge_cases.py` 的 `wait_silence`（增量扫帧 + 末尾静音窗），
    外加两条防早返护栏：
    ① 缓冲累计语音 ≥ `min_speech_s`（排除空轨/TTS 前导静音直接凑满静音窗）；
    ② 末尾连续静音 ≥ `quiet_s`，且**连续 `PLAYOUT_STABLE_WINDOWS` 个采样窗保持**。

    **为什么不用「无新帧」判停嘴（2026-09-18 实弹诊断）**：真栈上 agent 音轨空闲时
    仍持续推帧（实测 ~2× 实时、纯静音，`background_audio`/`roomio_audio` 合成轨），
    「字节数不再增长」结构性不可达——旧版据此判停嘴，每次等候都空烧满 25s 超时。
    末尾静音窗是这里唯一可用的信号（E2E `wait_silence` 同款）。
    空缓冲 → 末尾静音 0，必等。超时回 `(timeout_s, False)`——调用方照推但显式打印。
    """
    t0 = time.perf_counter()
    state = {"processed": 0, "speech": 0.0}
    stable = 0
    while time.perf_counter() - t0 < timeout_s:
        buf = bytes(agent_audio)
        speech, state["processed"] = speech_seconds(buf, state["processed"])
        state["speech"] += speech
        tail = trailing_silence_s(buf)
        if state["speech"] >= min_speech_s and tail >= quiet_s:
            stable += 1
            if stable >= PLAYOUT_STABLE_WINDOWS:
                return time.perf_counter() - t0, True
        else:
            stable = 0
        await asyncio.sleep(PLAYOUT_POLL_S)
    return time.perf_counter() - t0, False


def parse_graph_events(lines: list[str]) -> list[dict]:
    """抽 `FLOW_GRAPH <kind> k=v …` 打点为事件字典（纯函数，保序）。"""
    events: list[dict] = []
    for raw in lines:
        m = RE_FLOW_GRAPH.search(str(raw))
        if not m:
            continue
        ev: dict = {"kind": m.group(1)}
        for token in m.group(2).split():
            if "=" in token:
                key, value = token.split("=", 1)
                ev[key] = value
        events.append(ev)
    return events


def parse_judge_events(lines: list[str]) -> list[dict]:
    """抽 `FLOW_GRAPH judge_*` 打点为事件字典（纯函数，保序；与 parse_graph_events
    同款 k=v 展开）。默认/then-jump 腿**不调用**（事件集零变化）。"""
    events: list[dict] = []
    for raw in lines:
        m = RE_FLOW_GRAPH_JUDGE.search(str(raw))
        if not m:
            continue
        ev: dict = {"kind": m.group(1)}
        for token in m.group(2).split():
            if "=" in token:
                key, value = token.split("=", 1)
                ev[key] = value
        events.append(ev)
    return events


def graph_turn_rows(turns: list[dict]) -> list[dict]:
    """assistant 轮里 provider 属图执行的两类（纯函数，时间序保序）。"""
    rows: list[dict] = []
    for t in turns:
        if str(t.get("role") or "") != "assistant":
            continue
        provider = str(t.get("provider") or "").strip()
        if provider in GRAPH_PROVIDERS:
            rows.append({
                "provider": provider,
                "template_step": _as_int(t.get("template_step")),
                "gen": str(t.get("gen") or ""),
                "transcript": str(t.get("transcript") or "")[:60],
            })
    return rows


def transcript_has_keyword(turns: list[dict], keywords: list[str]) -> bool:
    """客户轮（无 provider 标签）转写里出现任一触发词（纯函数）——归因用：
    转写都没吐出触发词时，jump 断言失败根因在 ASR 而非图引擎。"""
    wanted = [k.strip().lower() for k in keywords if k and k.strip()]
    if not wanted:
        return False
    for t in turns:
        if str(t.get("role") or "") != "user":
            continue
        if str(t.get("provider") or "").strip():
            continue  # 机制行（storm-listen/starve-ack 等）不是客户口
        text = str(t.get("transcript") or "").lower()
        if any(k in text for k in wanted):
            return True
    return False


def plan_rounds(
    *,
    then_jump: int | None,
    has_qa: bool,
    trigger_text: str,
    nontrigger_text: str,
    play_text: str,
    after_text: str,
    play_round: bool,
    intent_judge: bool = False,
    fuzzy_text: str = FUZZY_TEXT,
) -> list[tuple[str, str]]:
    """本腿轮次表 `[(窗口名, 话术)]`（纯函数）。

    - **then-jump 档**（`then_jump` 非 None，Phase 3.3 追问链）：`has_qa` 真 →
      `[("play", play_text), ("after", after_text)]`——「播」轮就是**触发轮**（推
      `play_text` 命中 play_qa 绑定）。跳后步号只能在**下一轮**的 turns 行上看到
      （勘误预检 2：播放轮本体行在跳前落库），故必须两轮。`has_qa` 假（**无 QA 条目**，
      即 `qa_id` 为空；条目在场而音频未物化不在此列——那种情况腿照跑、`play_miss` 由
      `play_logged` 硬 FAIL）→ **空表**：调用方须显式判「腿跳过」，绝不许以「没跑出
      东西」空过成 PASS。
      `play_round` 在本档**不适用**（「播」轮是触发轮，关掉即零触发语空跑）。
    - **intent-judge 档**（`intent_judge` 真，Phase 3.4）：`[("fuzzy", fuzzy_text),
      ("consume", after_text)]`——fuzzy 轮=无关键词的模糊抱怨（judge_scheduled 当轮
      同步落 fuzzy 窗口）；consume 轮复用 `after_text`（中性话，已避开七族词+触发词，
      只为「pop pending→绑定触发」服务）。无 QA 依赖（jump_step 绑定）→ 永不空表。
      `play_round` 在本档**不适用**。
    - **默认档**：`trigger`/`nontrigger`（+ `play` 信息位轮）逐字节同旧。
    """
    if intent_judge:
        return [("fuzzy", fuzzy_text), ("consume", after_text)]
    if then_jump is not None:
        if not has_qa:
            return []
        return [("play", play_text), ("after", after_text)]
    rounds: list[tuple[str, str]] = [("trigger", trigger_text), ("nontrigger", nontrigger_text)]
    if play_round and has_qa:
        rounds.append(("play", play_text))
    return rounds


def post_jump_step_seen(turns: list[dict], then_jump: int) -> bool:
    """跳后步号是否在**跳后轮**的转写行上出现（纯函数）。

    只看 assistant 转写行，且 `provider != "graph-play"`——**播放轮本体行在跳之前
    落库**（`_qa_canned_say` 内上报，步号=跳前步），哪怕步号撞上 `then_jump` 也恒不
    计入（否则「播了」会被当成「跳了」的假绿）。

    **无假正例不变量（review R1，M3）**：该判据把「after 轮出现 step=N」读成「链生效」，
    成立前提是 **N 只可能由本体位移到达**——本腿装配下它由三件事共同保证：① 起始步号
    0/1；② 每轮至多推进一次；③ play 轮是首轮、其本体行被排除，故触发前不存在把步号
    推到 N 的轮次。它**不是**结构不变量：若将来本腿在前面加轮次（pre-trigger rounds）、
    改目标步，或让规则推进能从别的步顺推到 N，判据就会退化成假正例面。届时应改为
    **只扫 after 窗口的 turns 切片**（现在传的是全量 turns），或加轮次锚；默认档
    `AFTER_TEXT` 的洁净性专测只钉默认话术，`--after-text` 自定义时责任在调用方。
    """
    target = _as_int(then_jump, -1)
    for t in turns:
        if str(t.get("role") or "") != "assistant":
            continue
        if str(t.get("provider") or "").strip() == "graph-play":
            continue
        if _as_int(t.get("template_step"), -1) == target:
            return True
    return False


def probe_evidence(*, log_exists: bool, marks: list[int], expected_rounds: int) -> dict:
    """观测面可信度（纯函数，review R1）——**没有真观测，就没有「零 FLOW_GRAPH」结论**。

    本探针两条主判据都是 absence-based（「窗口内零 `FLOW_GRAPH` 行」「零 graph 轮」）；
    没有观测前提时它们会**空过**（假绿）。三件前提：

    - `log_exists`：agent.log 在场。不在场=整通零可观测，「零命中」是空话。
    - `rounds_complete`：`len(marks) == expected_rounds + 1`。marks 首元素在进房前落、
      每跑完一轮追加一个 → 中途异常/提前退出必少 mark（mark 塌成一个=一轮未观测）。
    - `grew_each_round`：相邻 mark 严格递增。每轮都真往日志写了字节；若某轮窗口
      零字节，说明该轮根本没跑起来，该窗口的「零命中」不成立。

    `ok` = 三件全真；`evaluate_leg` 用它闸 absent 判据。
    """
    grew = [b > a for a, b in zip(marks, marks[1:])]
    rounds_complete = len(marks) == expected_rounds + 1
    grew_each_round = bool(grew) and all(grew) and len(grew) == expected_rounds
    ok = bool(log_exists and rounds_complete and grew_each_round)
    return {
        "ok": ok,
        "log_exists": bool(log_exists),
        "expected_rounds": int(expected_rounds),
        "marks": list(marks),
        "rounds_complete": rounds_complete,
        "grew_each_round": grew_each_round,
        "grew_flags": grew,
    }


def evaluate_leg(
    *,
    expect_off: bool,
    target_step: int,
    trigger_events: list[dict],
    nontrigger_events: list[dict],
    play_events: list[dict],
    turns: list[dict],
    evidence: dict,
    then_jump: int | None = None,
    after_events: list[dict] | None = None,
    intent_judge: bool = False,
    fuzzy_events: list[dict] | None = None,
    consume_events: list[dict] | None = None,
    judge_events: list[dict] | None = None,
) -> dict:
    """主判据（纯函数）。expect_off=False=图开启腿；True=kill-switch 腿。

    主判据（进退出码）：
      两腿共用  evidence_ok（`probe_evidence` 的 ok——absence 判据的前提）
      图开启腿  jump_logged / trigger_turn_provider / nontrigger_silent
      then_jump 腿  play_logged / then_jump_effective（Phase 3.3 追问链，见下）
      kill 腿   killswitch_no_logs / killswitch_no_graph_turns
    **absence-based 的三条（`nontrigger_silent`/`killswitch_no_logs`/
    `killswitch_no_graph_turns`）在 `evidence_ok=False` 时恒 False**——否则
    日志缺失/中途异常会以「窗口里什么都没有」空过成 PASS（review R1 假绿）。
    正向判据（`jump_logged`/`trigger_turn_provider`/`play_logged`/`then_jump_effective`）
    要真观测到事件才算，无需另加前提。
    信息位：play / play_miss / jump_noop 计数（罐头未物化是明确降级路径，不判 FAIL）。

    **then_jump 腿独立判据分支**（勘误预检 5：无非触发轮，走既有分支会结构性 FAIL）：
      - `play_logged`（硬）：play 窗口内有 `kind=="play"`——`play_miss`（罐头未物化）
        → FAIL，本腿主判据就是「播+跳」。
      - `then_jump_effective`（硬）：**OR** 语义（勘误预检 2）——同轮
        `jump` 且 `step == then_jump` 且 `via == "then_jump"`（确定性）**或** 跳后轮
        转写行步号命中（`post_jump_step_seen`，受 ASR 影响）。
      信息位：`then_jump_logged` / `next_turn_step`（两分量各自可读，便于归因）。
    **默认档（`then_jump is None`）判据集/事件键集逐字节同旧**（零变化铁律）。
    """
    after = list(after_events or [])
    all_events = trigger_events + nontrigger_events + play_events + after
    grows = graph_turn_rows(turns)
    play_kinds = [str(e.get("kind")) for e in play_events]
    ev_ok = bool((evidence or {}).get("ok"))
    info = {
        "play_kinds": play_kinds,
        "play_miss": sum(1 for k in play_kinds if k == "play_miss"),
        "jump_noop": sum(1 for e in all_events if str(e.get("kind")) == "jump_noop"),
        "graph_turns": grows,
    }
    events = {
        "trigger": trigger_events,
        "nontrigger": nontrigger_events,
        "play": play_events,
    }
    if expect_off and intent_judge:
        # kill 面（Phase 3.4）：judge 日志族**不在** RE_FLOW_GRAPH——不单独并入的话
        # judge 残留会逃过 no_logs（judge_scheduled 只须 env 关=零调度，absence 判据
        # 照旧吃 evidence 闸防假绿）。
        judges = list(judge_events or [])
        checks = {
            "evidence_ok": ev_ok,
            "killswitch_no_logs": ev_ok and not all_events,
            "killswitch_no_graph_turns": ev_ok and not grows,
            "killswitch_no_judge_logs": ev_ok and not judges,
        }
        events["judge"] = judges
    elif expect_off:
        checks = {
            "evidence_ok": ev_ok,
            "killswitch_no_logs": ev_ok and not all_events,
            "killswitch_no_graph_turns": ev_ok and not grows,
        }
    elif then_jump is not None:
        target = _as_int(then_jump, -1)
        # 跳日志落在**播放轮**窗口（T2 在 `_qa_canned_say` 返回后、本轮收尾 raise 之前
        # 同步打点；播报完成先于该轮 mark）。
        logged = any(
            str(e.get("kind")) == "jump"
            and _as_int(e.get("step"), -1) == target
            and str(e.get("via") or "").strip() == "then_jump"
            for e in play_events
        )
        next_seen = post_jump_step_seen(turns, target)
        # 信息位归因（M4）：`next_turn_step=False` = after 轮**无新步号**——可能是该轮被
        # QA 快路/规则吞掉（步号根本没重渲染）、ASR 没吐出该轮、或链没跳；此时归因落到
        # 同轮 `then_jump_logged` 分量（两分量各自可读，故不影响 OR 判据）。
        info.update({"then_jump": target, "then_jump_logged": logged,
                     "next_turn_step": next_seen})
        events["after"] = after
        checks = {
            "evidence_ok": ev_ok,
            "play_logged": any(k == "play" for k in play_kinds),
            "then_jump_effective": bool(logged or next_seen),
        }
    elif intent_judge:
        # Phase 3.4 意图 judge 腿：fuzzy 轮（无关键词）→ judge_scheduled 当轮同步落
        # fuzzy 窗口（确定性）；judge_hit/judge_pending_fired 落点跨窗口边界（3s 让路
        # delay + 9B 往返 vs reply 播放时长）→ **全局**日志尾扫。judge_effective 双锚：
        # pending_fired（消费+触发）**且**（consume 窗口 jump@target ∨ turn 行
        # graph-jump@target）——fired 日志在 pick 之后打，jump 分支同轮必跟。
        fuzzy = list(fuzzy_events or [])
        consume = list(consume_events or [])
        judges = list(judge_events or [])
        scheduled = any(str(e.get("kind")) == "judge_scheduled" for e in fuzzy)
        hit = any(str(e.get("kind")) == "judge_hit" for e in judges)
        fired = any(str(e.get("kind")) == "judge_pending_fired" for e in judges)
        consume_jump = any(
            str(e.get("kind")) == "jump" and _as_int(e.get("step"), -1) == target_step
            for e in consume
        ) or any(
            g["provider"] == "graph-jump" and g["template_step"] == target_step
            for g in grows
        )
        info.update({
            "judge_kinds": [str(e.get("kind")) for e in judges],
            "judge_scheduled": scheduled,
            "judge_hit": hit,
            "judge_pending_fired": fired,
            "consume_jump": consume_jump,
        })
        events.update({"fuzzy": fuzzy, "consume": consume, "judge": judges})
        checks = {
            "evidence_ok": ev_ok,
            "judge_scheduled": scheduled,
            "judge_hit_logged": hit,
            "judge_effective": bool(fired and consume_jump),
        }
    else:
        jumps = [e for e in trigger_events if str(e.get("kind")) == "jump"]
        checks = {
            "evidence_ok": ev_ok,
            "jump_logged": any(_as_int(e.get("step"), -1) == target_step for e in jumps),
            "trigger_turn_provider": any(
                g["provider"] == "graph-jump" and g["template_step"] == target_step
                for g in grows
            ),
            "nontrigger_silent": ev_ok and not nontrigger_events,
        }
    return {
        "checks": checks,
        "pass": all(checks.values()),
        "evidence": evidence or {},
        "events": events,
        "info": info,
    }


# ---------------------------------------------------------------------------
# CP 侧：模板 / QA 条目 / 通话
# ---------------------------------------------------------------------------
def _cp(path: str, *, method: str = "GET", **kw) -> httpx.Response:
    kw.setdefault("timeout", 15)
    return httpx.request(
        method, f"{erc.CONTROL_PLANE_URL}{path}", headers=_CP_HEADERS, **kw
    )


def build_graph_json(qa_id: str, *, then_jump: int | None = None, judge: bool = False) -> str:
    """图契约（spec §3）：投诉→jump_step 4；退款→play_qa（有 qa_id 才挂该腿）。

    `then_jump`（Phase 3.3 追问链，1-based）：非空且挂了 play_qa 绑定时给该绑定加
    `"then_jump": N`；默认 `None` 时输出与今逐字节同（旧腿/旧断言零变化）。
    `judge`（Phase 3.4 意图引擎）：真时给「投诉」意图挂 `judge.prompt=JUDGE_PROMPT`
    （关键词照旧必填——judge 只补关键词未中的模糊轮）；默认 False 逐字节同旧。
    """
    bindings = [{
        "id": "bnd_7e8f9a0b",
        "intent": "int_1a2b3c4d",
        "action": "jump_step",
        "step": TARGET_STEP,
        "priority": 10,
        "once": False,
        "enabled": True,
    }]
    intents = [{
        "id": "int_1a2b3c4d",
        "label": "投诉",
        "keywords": list(TRIGGER_KEYWORDS),
        "steps": [],
        "enabled": True,
        # 判据只在显式开 judge 腿时写键（CP 严格校验面：prompt 必须 1-400 字符串）。
        **({"judge": {"prompt": JUDGE_PROMPT}} if judge else {}),
    }]
    if qa_id:
        intents.append({
            "id": "int_2b3c4d5e",
            "label": "退款",
            "keywords": list(PLAY_KEYWORDS),
            "steps": [],
            "enabled": True,
        })
        bindings.append({
            "id": "bnd_c1d2e3f4",
            "intent": "int_2b3c4d5e",
            "action": "play_qa",
            "qa_id": qa_id,
            # 链只在显式给了目标步时写键（`then_jump=0`/None 都不写——CP 严格校验面
            # 拒非 [1,999] 值，写 0 会 400）。
            **({"then_jump": int(then_jump)} if then_jump else {}),
            "priority": 20,
            "once": False,
            "enabled": True,
        })
    return json.dumps({"version": 1, "intents": intents, "bindings": bindings},
                      ensure_ascii=False)


def pick_qa_id(lang: str) -> str:
    """现存 QA 条目（优先本语言、其次任意）——play 腿依赖罐头物化，缺失即信息位。"""
    try:
        rows = _cp(f"/api/qa-entries?account_id={ACCOUNT_ID}").json()
    except Exception:  # noqa: BLE001 - QA 库拉不到=跳过 play 腿
        return ""
    if not isinstance(rows, list):
        return ""
    picked = next(
        (r for r in rows if str(r.get("lang")) == lang and str(r.get("answer_text") or "").strip()),
        None,
    ) or next((r for r in rows if str(r.get("answer_text") or "").strip()), None)
    return str((picked or {}).get("id") or "")


def create_probe_template(lang: str, graph_json: str) -> str:
    """经 CP 建带图模板（顺带实弹 CP 保存校验路径）。名字含 probe=不被 E2E 自动挑中。"""
    resp = _cp("/api/templates", method="POST", json={
        "account_id": ACCOUNT_ID,
        "name": f"probe-flow-graph-{lang}-{int(time.time())}",
        "language": lang,
        "steps_json": json.dumps(PROBE_STEPS, ensure_ascii=False),
        "graph_json": graph_json,
    })
    resp.raise_for_status()
    return str(resp.json().get("id") or "")


def create_probe_call(lang: str, template_id: str, voice: str) -> tuple[str, str]:
    """E2E- 前缀对象（心跳豁免）+ 人设 + 建单（显式 template_id 快照该图模板）。"""
    ts = int(time.time() * 1000) % 100000
    obj = _cp("/api/objects", method="POST", params={"account_id": ACCOUNT_ID}, json={
        "display_name": f"E2E-陳小明-{ts}",
        "role_template": "buyer",
        "language": lang,
        "background": "flow graph probe",
        "template_id": template_id,
    })
    obj.raise_for_status()
    obj = obj.json()
    persona = _cp("/api/personas", method="POST", params={"account_id": ACCOUNT_ID}, json={
        "name": f"E2E话术图{lang}",
        "language": lang,
        "tone": "礼貌专业",
        "reference_audio": voice,
    })
    persona.raise_for_status()
    persona = persona.json()
    call = _cp("/api/calls", method="POST", json={
        "account_id": ACCOUNT_ID,
        "object_id": obj["id"],
        "persona_id": persona["id"],
        "template_id": template_id,
        "mode": "live",
        "direction": "webrtc",
        "language": lang,
    })
    call.raise_for_status()
    return str(call.json()["id"]), str(persona.get("reference_audio") or voice)


def delete_template(template_id: str) -> None:
    try:
        _cp(f"/api/templates/{template_id}", method="DELETE")
    except Exception:  # noqa: BLE001 - 清理失败只留痕，不影响判据
        pass


def log_windows(marks: list[int]) -> list[list[str]]:
    """按字节偏移切 agent.log，返回相邻偏移间的行窗口（纯读，越界/缺失回空）。"""
    try:
        data = erc.LOG_PATH.read_bytes()
    except Exception:  # noqa: BLE001 - 日志缺失=所有窗口空（断言会如实报零）
        return [[] for _ in range(max(0, len(marks) - 1))]
    out: list[list[str]] = []
    for start, end in zip(marks, marks[1:]):
        out.append([
            raw.decode("utf-8", errors="replace")
            for raw in data[max(0, start):max(0, end)].splitlines()
        ])
    return out


# ---------------------------------------------------------------------------
# 跑一腿
# ---------------------------------------------------------------------------
async def run_leg(*, expect_off: bool, lang: str, voice: str, trigger_text: str,
                  nontrigger_text: str, play_text: str, play_round: bool,
                  keep_template: bool, budgets: dict[str, float],
                  then_jump: int | None = None, after_text: str = AFTER_TEXT,
                  intent_judge: bool = False, fuzzy_text: str = FUZZY_TEXT,
                  judge_soak_s: float = JUDGE_SOAK_S) -> dict:
    leg_name = "killswitch-off" if expect_off else "graph-on"
    if then_jump is not None:
        leg_name = "then-jump-killswitch-off" if expect_off else "then-jump"
    if intent_judge:
        leg_name = "intent-judge-killswitch-off" if expect_off else "intent-judge"
    qa_id = pick_qa_id(lang)
    graph_json = build_graph_json(qa_id, then_jump=then_jump, judge=intent_judge)
    rounds = plan_rounds(
        then_jump=then_jump, has_qa=bool(qa_id), trigger_text=trigger_text,
        nontrigger_text=nontrigger_text, play_text=play_text, after_text=after_text,
        play_round=play_round, intent_judge=intent_judge, fuzzy_text=fuzzy_text,
    )
    print(f"\n[flow-graph] 腿={leg_name} lang={lang} qa_id={qa_id or '(无QA条目, play 腿跳过)'}"
          f" then_jump={then_jump} intent_judge={intent_judge} rounds={[n for n, _ in rounds]}",
          flush=True)
    if then_jump is not None and not rounds:
        # 未评估 ≠ PASS：**无 QA 条目**时链腿结构性跑不起来（触发语必然 play_miss），
        # 必须显式跳过并以退出码 1 收尾（空表跑出的「零痕迹」是没观测，不是证据）。
        # 注意：条目在场但音频未物化**不**走这里——腿照跑、`play_miss` 由 play_logged
        # 硬 FAIL。报告键 `no_qa_or_audio` 是历史 token（schema 稳定，不再新增含义）。
        print("[flow-graph] then-jump 腿跳过：无 QA 条目"
              "（先建 QA 条目，再 python tools/bok.py tts-pregen --qa 物化音频）", flush=True)
        return {
            "leg": leg_name,
            "skipped": "no_qa_or_audio",
            "expect_off": expect_off,
            "lang": lang,
            "qa_id": qa_id,
            "graph_json": graph_json,
            "then_jump": then_jump,
            "ts": int(time.time()),
        }
    template_id = create_probe_template(lang, graph_json)
    print(f"[flow-graph] template={template_id} graph={graph_json}", flush=True)
    try:
        return await _run_leg_with_stack(
            expect_off=expect_off, leg_name=leg_name, lang=lang, qa_id=qa_id,
            graph_json=graph_json, template_id=template_id, voice=voice,
            rounds=rounds, then_jump=then_jump, after_text=after_text,
            budgets=budgets, intent_judge=intent_judge, judge_soak_s=judge_soak_s,
        )
    finally:
        # 清理探针模板：名字含 probe=不会被 E2E 自动挑中，但跑完仍应不留痕（--keep-template 留档用）。
        if not keep_template and template_id:
            delete_template(template_id)


async def _run_leg_with_stack(*, expect_off: bool, leg_name: str, lang: str, qa_id: str,
                              graph_json: str, template_id: str, voice: str,
                              rounds: list[tuple[str, str]], then_jump: int | None,
                              after_text: str, budgets: dict[str, float],
                              intent_judge: bool = False,
                              judge_soak_s: float = JUDGE_SOAK_S) -> dict:
    # 轮次表由 `plan_rounds` 单点产出（默认档=触发/非触发/play 信息位轮；then-jump 档=
    # 播 + 跳后两轮），这里只负责跑表与按窗口切日志。
    pcms = {name: erc.tts_pcm(text, lang) for name, text in rounds}

    call_id, resolved_voice = create_probe_call(lang, template_id, voice)
    print(f"[flow-graph] call={call_id} voice={resolved_voice!r}", flush=True)

    room = rtc.Room()
    agent_audio = bytearray()
    read_tasks: list[asyncio.Task] = []

    def attach(track) -> None:
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        if getattr(track, "name", "") not in ("roomio_audio", "background_audio"):
            return

        async def _read() -> None:
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            try:
                async for event in stream:
                    frame = getattr(event, "frame", event)
                    agent_audio.extend(bytes(frame.data))
            except Exception:  # noqa: BLE001 - 断轨收尾
                pass
            finally:
                try:
                    await stream.aclose()
                except Exception:  # noqa: BLE001
                    pass

        read_tasks.append(asyncio.get_running_loop().create_task(_read()))

    room.on("track_subscribed", lambda track, _p, _pt: attach(track))
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                attach(track)

    measures: list[dict] = []
    setup_ok = False
    greeting_wait, greeting_quiet = 0.0, False
    marks: list[int] = [
        erc.LOG_PATH.stat().st_size if erc.LOG_PATH.exists() else 0
    ]
    try:
        data = _cp("/api/token", method="POST",
                   json={"account_id": ACCOUNT_ID, "call_id": call_id}).json()
        await room.connect(data["serverUrl"], data["participantToken"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("customer-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        setup_ok = await erc.wait_greeting(agent_audio)
        if not setup_ok:
            print("[flow-graph] WARN 开场白 45s×3 未出声，照常推进", flush=True)
        # wait_greeting 只保证「出过声」（其累计静音采样会因开场白句间停顿早返）→
        # 必须再等真播完，否则触发器落进开场白尾巴（实弹根因）。
        greeting_wait, greeting_quiet = await wait_playout_end(agent_audio)
        print(f"[flow-graph] 开场白播完等候 {greeting_wait:.2f}s quiet_ok={greeting_quiet}",
              flush=True)
        # 不 clear：`play_and_listen` 自按 `mark=len(agent_audio)` 只量本轮新帧，
        # 而累积语音保留才能让「安静」判据在空闲轮秒过（clear 后空闲轮无语音可累计
        # → floor 永不满足 → 每轮空烧满超时，2026-09-18 实弹第二轮踩到）。
        await asyncio.sleep(0.3)

        for name, text in rounds:
            # 每轮起推前都等「上一条真播完」——12.9s 级回复同样会吞掉下一轮
            # （首声 0.00s 伪值=上一条尾巴被当本轮应答）。
            pre_wait, pre_quiet = await wait_playout_end(agent_audio)
            print(f"    {name:>10} 起推前等候 {pre_wait:.2f}s quiet_ok={pre_quiet}", flush=True)
            m = await erc.play_and_listen(audio_source, agent_audio, pcms[name])
            m.update({"name": name, "text": text,
                      "pre_push_wait_s": round(pre_wait, 2), "pre_push_quiet": pre_quiet})
            measures.append(m)
            marks.append(erc.LOG_PATH.stat().st_size if erc.LOG_PATH.exists() else marks[-1])
            first = f"{m['first_audio_ms'] / 1000:.2f}s" if m.get("first_audio_ms") is not None else "-"
            print(f"    {name:>10} 「{text}」 → 首声 {first} · 语音 {m.get('speech_s', 0):.1f}s · "
                  f"{'✓' if m.get('answered') else '✗哑'} · log+{marks[-1] - marks[-2]}B", flush=True)
            await asyncio.sleep(0.3)
            if intent_judge and name == "fuzzy":
                # Phase 3.4:judge 任务 = FLOW_JUDGE_DELAY(3s) 让路 + 9B 往返——reply
                # 播放只盖 ~2-6s，判定可能晚于 consume 起推才落账（pending 未存 =
                # consume 轮 pop 空、judge_effective 假 FAIL）。显式浸泡等判定落账。
                print(f"    {'judge-soak':>10} 等候判定落账 {judge_soak_s:.1f}s", flush=True)
                await asyncio.sleep(judge_soak_s)
    except Exception as exc:  # noqa: BLE001 - 单腿异常照常收尾并出报告
        print(f"[flow-graph] 腿 {leg_name} 异常中断: {exc!r}", flush=True)
    finally:
        try:
            await room.disconnect()
        except Exception:  # noqa: BLE001
            pass
        for t in read_tasks:
            t.cancel()
        try:
            _cp(f"/api/calls/{call_id}/hangup", method="POST")
            _cp(f"/api/calls/{call_id}/settle", method="POST", timeout=60)
        except Exception:  # noqa: BLE001
            pass

    turns = await erc.fetch_turns(call_id)
    evidence = probe_evidence(
        log_exists=erc.LOG_PATH.exists(),
        marks=marks,
        expected_rounds=len(rounds),
    )
    windows = log_windows(marks)
    # 按**轮次名**通用切窗（默认档 trigger/nontrigger/play；then-jump 档 play/after）——
    # 不再硬编码窗口名，新增轮次只需在 `plan_rounds` 里加一行。
    name_to_window = {name: windows[i] for i, (name, _) in enumerate(rounds) if i < len(windows)}
    trigger_events = parse_graph_events(name_to_window.get("trigger", []))
    nontrigger_events = parse_graph_events(name_to_window.get("nontrigger", []))
    play_events = parse_graph_events(name_to_window.get("play", []))
    after_events = parse_graph_events(name_to_window.get("after", []))
    fuzzy_events = parse_graph_events(name_to_window.get("fuzzy", []))
    consume_events = parse_graph_events(name_to_window.get("consume", []))
    # judge 打点全局尾扫（Phase 3.4）：judge_hit/judge_pending_fired 的落点跨窗口边界
    # （3s 让路 + 9B 往返 vs reply 播放时长，可能落 fuzzy 窗、soak 间隙=consume 窗头、
    # 甚至 settle 后），窗口归属只作信息位——判据用 marks[0]:EOF 全量。
    judge_events: list[dict] = []
    if intent_judge:
        try:
            tail = erc.LOG_PATH.read_bytes()[max(0, marks[0]):]
            judge_events = parse_judge_events(
                [ln.decode("utf-8", errors="replace") for ln in tail.splitlines()]
            )
        except Exception:  # noqa: BLE001 - 日志缺失=零 judge 事件（判据如实报缺）
            judge_events = []
    if not evidence["ok"]:
        print(f"[flow-graph] 观测面不足 → absence 判据不计 PASS：{evidence}", flush=True)

    # 延迟/哑轮信息位（复用 soak 骨架，不进本探针退出码）。
    perceived: list[dict] = []
    counts = {k: 0 for k in pls.COUNT_MARKERS}
    flat = [line for w in windows for line in w]
    for line in flat:
        mt = pls.RE_PERCEIVED.search(line)
        if mt:
            perceived.append({
                "total": int(mt.group(1)), "eou": int(mt.group(2)),
                "llm": int(mt.group(3)), "tts": int(mt.group(4)),
            })
            continue
        for k in pls.COUNT_MARKERS:
            if k in line:
                counts[k] += 1
    summary = pls.summarize_report(measures, perceived, counts, budgets)

    verdict = evaluate_leg(
        expect_off=expect_off,
        target_step=TARGET_STEP,
        trigger_events=trigger_events,
        nontrigger_events=nontrigger_events,
        play_events=play_events,
        turns=turns,
        evidence=evidence,
        then_jump=then_jump,
        after_events=after_events,
        intent_judge=intent_judge,
        fuzzy_events=fuzzy_events,
        consume_events=consume_events,
        judge_events=judge_events,
    )
    # 归因用关键词：then-jump 腿的触发语是 `play_text`（退款 系），默认腿是「投诉」系。
    understood_keywords = PLAY_KEYWORDS if then_jump is not None else TRIGGER_KEYWORDS
    understood = transcript_has_keyword(turns, understood_keywords)
    round_text = dict(rounds)
    result = {
        "leg": leg_name,
        "expect_off": expect_off,
        "lang": lang,
        "template_id": template_id,
        "call_id": call_id,
        "qa_id": qa_id,
        "graph_json": graph_json,
        "then_jump": then_jump,
        "intent_judge": intent_judge,
        "rounds": [name for name, _ in rounds],
        "after_text": after_text if then_jump is not None else "",
        "setup_ok": setup_ok,
        "evidence": evidence,
        # 起推前「播完」等候（2026-09-18 实弹修复）：开场白 + 逐轮，供事后归因
        # 「首声 0.00s / 转写不含触发词」类伪值。
        "pacing": {
            "greeting_playout_s": round(greeting_wait, 2),
            "greeting_quiet_ok": greeting_quiet,
            "per_round": [
                {"name": m["name"], "wait_s": m.get("pre_push_wait_s"),
                 "quiet_ok": m.get("pre_push_quiet")}
                for m in measures
            ],
        },
        "trigger_text": round_text.get("trigger", ""),
        "nontrigger_text": round_text.get("nontrigger", ""),
        "play_text": round_text.get("play", ""),
        "trigger_understood": understood,
        "measures": measures,
        "summary": summary,
        "turn_rows": [
            {
                "role": str(t.get("role") or ""),
                "provider": str(t.get("provider") or ""),
                "gen": str(t.get("gen") or ""),
                "template_step": int(t.get("template_step") or 0),
                "transcript": str(t.get("transcript") or "")[:80],
            }
            for t in turns
        ],
        "verdict": verdict,
        "ts": int(time.time()),
    }
    print_leg(result, budgets)
    return result


def print_leg(res: dict, budgets: dict[str, float]) -> None:
    v = res["verdict"]
    print("\n" + "═" * 72, flush=True)
    print(f"腿 {res['leg']}  call={res['call_id']}  template={res['template_id']}", flush=True)
    print("═" * 72, flush=True)
    for m in res["measures"]:
        first = f"{m['first_audio_ms'] / 1000:.2f}s" if m.get("first_audio_ms") is not None else "无"
        print(f"  {m['name']:>10} 首声={first:>7} 语音={m.get('speech_s', 0):.1f}s "
              f"{'' if m.get('answered') else '✗哑'}  「{m['text']}」", flush=True)
    # 窗口行只渲染**本腿真跑过的轮次**（M5）：`evaluate_leg` 恒建 trigger/nontrigger/
    # play 三键，then-jump 档没跑 trigger/nontrigger——不按轮次表过滤就会打出
    # 「trigger FLOW_GRAPH 行：（零）」这种对不存在的轮做的空断言。
    ran_rounds = set(res.get("rounds") or [])
    for label in ("trigger", "nontrigger", "play", "after"):
        if label not in v["events"]:
            continue
        if ran_rounds and label not in ran_rounds:
            continue
        evs = v["events"][label]
        print(f"  {label:>10} FLOW_GRAPH 行：{evs if evs else '（零）'}", flush=True)
    print(f"  graph 轮：{v['info']['graph_turns'] or '（无）'}", flush=True)
    print(f"  信息位：play_kinds={v['info']['play_kinds'] or '（无）'} "
          f"play_miss={v['info']['play_miss']} jump_noop={v['info']['jump_noop']}", flush=True)
    if "then_jump_logged" in v["info"]:
        print(f"  追问链信息位：then_jump={v['info'].get('then_jump')} "
              f"同轮 jump 日志={v['info']['then_jump_logged']} "
              f"跳后轮步号命中={v['info']['next_turn_step']}", flush=True)
    print(f"  触发转写可辨={res['trigger_understood']}", flush=True)
    ev = res.get("evidence") or v.get("evidence") or {}
    print(f"  观测面 evidence_ok={ev.get('ok')}（log_exists={ev.get('log_exists')} "
          f"rounds_complete={ev.get('rounds_complete')} grew_each_round={ev.get('grew_each_round')} "
          f"marks={ev.get('marks')}/{ev.get('expected_rounds')}轮）", flush=True)
    fa, pd = res["summary"]["first_audio"], res["summary"]["perceived"]
    if fa["n"]:
        print(f"  墙钟首声 n={fa['n']} p50={fa['p50']:.0f}ms max={fa['max']:.0f}ms "
              f"（预算 {budgets['first_ms']:.0f}，信息位）", flush=True)
    if pd["n"]:
        print(f"  PERCEIVED n={pd['n']} p50={pd['p50']:.0f}ms（信息位）", flush=True)
    if not res["trigger_understood"]:
        what = "退款(play_qa)" if v["info"].get("then_jump") else "投诉"
        print(f"  ⚠ 转写未见 {what} 触发词：本腿断言不可归因于图引擎（ASR 侧问题）", flush=True)
    for name, ok in v["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}", flush=True)
    print(f"  腿结论：{'PASS' if v['pass'] else 'FAIL'}", flush=True)


# ---------------------------------------------------------------------------
# 无栈自检（--selftest）：纯函数正/反例
# ---------------------------------------------------------------------------
def selftest() -> int:
    def _turns(provider: str, step: int) -> list[dict]:
        return [
            {"role": "user", "transcript": "我要投诉", "provider": ""},
            {"role": "assistant", "transcript": "好的", "provider": provider,
             "gen": "llm", "template_step": step},
        ]

    def _ev(*, log: bool = True, marks: list[int] | None = None) -> dict:
        # 诚实路径：2 轮 → 3 个 mark（进房前 + 每轮后），每轮窗口都有新字节。
        return probe_evidence(
            log_exists=log,
            marks=[100, 200, 300] if marks is None else marks,
            expected_rounds=2,
        )

    jump_ev = {"kind": "jump", "binding": "bnd_7e8f9a0b", "step": str(TARGET_STEP)}
    parsed = parse_graph_events([
        "2026-09-18 10:00:00,000 [INFO] FLOW_GRAPH jump binding=bnd_7e8f9a0b step=4",
        "FLOW_GRAPH play_miss binding=bnd_c1d2e3f4 qa=qa-1",
        "其它噪声行",
    ])
    cases: list[tuple[str, bool, bool]] = [
        ("graph-on 正例（play_miss 仍 PASS）", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP,
            trigger_events=[jump_ev], nontrigger_events=[],
            play_events=[{"kind": "play_miss", "binding": "bnd_c1d2e3f4"}],
            turns=_turns("graph-jump", TARGET_STEP), evidence=_ev())["pass"], True),
        ("graph-on 反例：无 jump 日志/无 provider", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP,
            trigger_events=[], nontrigger_events=[], play_events=[],
            turns=_turns("", TARGET_STEP), evidence=_ev())["pass"], False),
        ("graph-on 反例：非触发轮出现 FLOW_GRAPH 行", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP,
            trigger_events=[jump_ev],
            nontrigger_events=[{"kind": "jump_noop", "binding": "x"}],
            play_events=[], turns=_turns("graph-jump", TARGET_STEP), evidence=_ev())["pass"], False),
        ("kill 腿正例：全程零图痕迹", evaluate_leg(
            expect_off=True, target_step=TARGET_STEP,
            trigger_events=[], nontrigger_events=[], play_events=[],
            turns=[{"role": "assistant", "provider": "", "gen": "llm",
                    "template_step": 2, "transcript": "好的"}], evidence=_ev())["pass"], True),
        ("kill 腿反例：仍有图痕迹", evaluate_leg(
            expect_off=True, target_step=TARGET_STEP,
            trigger_events=[jump_ev], nontrigger_events=[], play_events=[],
            turns=_turns("graph-jump", TARGET_STEP), evidence=_ev())["pass"], False),
        # ---- review R1：absence-based 判据的观测前提（无观测不成 PASS）----
        ("假绿闸：日志缺失 → kill 腿零痕迹不成立", evaluate_leg(
            expect_off=True, target_step=TARGET_STEP,
            trigger_events=[], nontrigger_events=[], play_events=[],
            turns=[], evidence=_ev(log=False))["pass"], False),
        ("假绿闸：marks 塌成一个（一轮未观测）→ kill 腿不成立", evaluate_leg(
            expect_off=True, target_step=TARGET_STEP,
            trigger_events=[], nontrigger_events=[], play_events=[],
            turns=[], evidence=_ev(marks=[100]))["pass"], False),
        ("假绿闸：中途异常少 mark → 非触发轮静默空过不成立", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP,
            trigger_events=[jump_ev], nontrigger_events=[],
            play_events=[], turns=_turns("graph-jump", TARGET_STEP),
            evidence=_ev(marks=[100, 200]))["pass"], False),
        ("probe_evidence 诚实路径 ok", _ev()["ok"], True),
        ("probe_evidence 某轮零字节 → ok=False",
         _ev(marks=[100, 100, 300])["ok"], False),
        ("parse_graph_events 拆词（play_miss 不误吃成 play）", parsed == [
            {"kind": "jump", "binding": "bnd_7e8f9a0b", "step": "4"},
            {"kind": "play_miss", "binding": "bnd_c1d2e3f4", "qa": "qa-1"},
        ], True),
        ("transcript_has_keyword 命中真客户轮", transcript_has_keyword(
            [{"role": "user", "transcript": "我要投诉", "provider": ""}],
            TRIGGER_KEYWORDS), True),
        ("transcript_has_keyword 忽略机制行", not transcript_has_keyword(
            [{"role": "user", "transcript": "我要投诉", "provider": "starve-ack"}],
            TRIGGER_KEYWORDS), True),
        # ---- Phase 3.3 追问链腿（play+跳 / 无跳 / 罐头未物化）----
        ("then-jump 正例：同轮 via=then_jump 日志", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP, then_jump=TARGET_STEP,
            trigger_events=[], nontrigger_events=[],
            play_events=[{"kind": "play", "binding": "bnd_c1d2e3f4", "qa": "qa-1"},
                         {"kind": "jump", "binding": "bnd_c1d2e3f4",
                          "step": str(TARGET_STEP), "via": "then_jump"}],
            after_events=[], turns=_turns("graph-play", 2), evidence=_ev())["pass"], True),
        ("then-jump 正例：跳后轮步号命中（无同轮日志）", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP, then_jump=TARGET_STEP,
            trigger_events=[], nontrigger_events=[],
            play_events=[{"kind": "play", "binding": "bnd_c1d2e3f4", "qa": "qa-1"}],
            after_events=[], turns=_turns("", TARGET_STEP), evidence=_ev())["pass"], True),
        ("then-jump 反例：罐头未物化（play_miss）→ 播都没播", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP, then_jump=TARGET_STEP,
            trigger_events=[], nontrigger_events=[],
            play_events=[{"kind": "play_miss", "binding": "bnd_c1d2e3f4"}],
            after_events=[], turns=_turns("graph-play", 2), evidence=_ev())["pass"], False),
        ("then-jump 反例：播了但没跳（无日志/无步号）", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP, then_jump=TARGET_STEP,
            trigger_events=[], nontrigger_events=[],
            play_events=[{"kind": "play", "binding": "bnd_c1d2e3f4", "qa": "qa-1"}],
            after_events=[], turns=_turns("graph-play", 2), evidence=_ev())["pass"], False),
        ("then-jump kill 腿：after 窗口图痕迹也算越闸", evaluate_leg(
            expect_off=True, target_step=TARGET_STEP, then_jump=TARGET_STEP,
            trigger_events=[], nontrigger_events=[], play_events=[],
            after_events=[{"kind": "jump", "binding": "bnd_c1d2e3f4",
                           "step": str(TARGET_STEP), "via": "then_jump"}],
            turns=[], evidence=_ev())["pass"], False),
        ("plan_rounds then-jump 档=播+跳后两轮", plan_rounds(
            then_jump=TARGET_STEP, has_qa=True, trigger_text=TRIGGER_TEXT,
            nontrigger_text=NONTRIGGER_TEXT, play_text=PLAY_TEXT, after_text=AFTER_TEXT,
            play_round=True) == [("play", PLAY_TEXT), ("after", AFTER_TEXT)], True),
        ("plan_rounds then-jump 档无 QA → 空表（腿跳过≠空过成 PASS）", plan_rounds(
            then_jump=TARGET_STEP, has_qa=False, trigger_text=TRIGGER_TEXT,
            nontrigger_text=NONTRIGGER_TEXT, play_text=PLAY_TEXT, after_text=AFTER_TEXT,
            play_round=True) == [], True),
        ("graph_json 默认档不带 then_jump 键",
         all("then_jump" not in b for b in json.loads(build_graph_json("qa-1"))["bindings"]), True),
        ("graph_json then-jump 档挂在 play_qa 绑定上",
         next(b for b in json.loads(build_graph_json("qa-1", then_jump=TARGET_STEP))["bindings"]
              if b["action"] == "play_qa")["then_jump"], TARGET_STEP),
        # ---- Phase 3.4 意图 judge 腿（fuzzy+consume / kill / 判定面）----
        ("intent-judge 正例：scheduled+hit+fired+consume jump", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP, intent_judge=True,
            trigger_events=[], nontrigger_events=[], play_events=[],
            fuzzy_events=[{"kind": "judge_scheduled", "intents": "1", "step": "1"}],
            consume_events=[{"kind": "jump", "binding": "bnd_7e8f9a0b", "step": "4"}],
            judge_events=[{"kind": "judge_hit", "intent": "int_1a2b3c4d", "step": "1"},
                          {"kind": "judge_pending_fired", "binding": "bnd_7e8f9a0b", "step": "1"}],
            turns=_turns("graph-jump", TARGET_STEP), evidence=_ev())["pass"], True),
        ("intent-judge 反例：judge_miss（9B 不贴合）", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP, intent_judge=True,
            trigger_events=[], nontrigger_events=[], play_events=[],
            fuzzy_events=[{"kind": "judge_scheduled", "intents": "1", "step": "1"}],
            consume_events=[], judge_events=[{"kind": "judge_miss", "step": "1"}],
            turns=_turns("", 2), evidence=_ev())["pass"], False),
        ("intent-judge 反例：hit 了但 pending 没消费（consume 轮缺失）", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP, intent_judge=True,
            trigger_events=[], nontrigger_events=[], play_events=[],
            fuzzy_events=[{"kind": "judge_scheduled", "intents": "1", "step": "1"}],
            consume_events=[], judge_events=[{"kind": "judge_hit", "intent": "x", "step": "1"}],
            turns=_turns("", 2), evidence=_ev())["pass"], False),
        ("intent-judge 反例：scheduled 都没打（关键词命中/图未开）", evaluate_leg(
            expect_off=False, target_step=TARGET_STEP, intent_judge=True,
            trigger_events=[], nontrigger_events=[], play_events=[],
            fuzzy_events=[], consume_events=[], judge_events=[],
            turns=_turns("", 2), evidence=_ev())["pass"], False),
        ("intent-judge kill 腿正例：零图+零 judge 痕迹", evaluate_leg(
            expect_off=True, target_step=TARGET_STEP, intent_judge=True,
            trigger_events=[], nontrigger_events=[], play_events=[],
            fuzzy_events=[], consume_events=[], judge_events=[],
            turns=[], evidence=_ev())["pass"], True),
        ("intent-judge kill 腿反例：judge_scheduled 残留（no_judge_logs 逮到）", evaluate_leg(
            expect_off=True, target_step=TARGET_STEP, intent_judge=True,
            trigger_events=[], nontrigger_events=[], play_events=[],
            fuzzy_events=[], consume_events=[],
            judge_events=[{"kind": "judge_scheduled", "intents": "1", "step": "1"}],
            turns=[], evidence=_ev())["pass"], False),
        ("intent-judge kill 腿假绿闸：日志缺失 → 零痕迹不成立", evaluate_leg(
            expect_off=True, target_step=TARGET_STEP, intent_judge=True,
            trigger_events=[], nontrigger_events=[], play_events=[],
            fuzzy_events=[], consume_events=[], judge_events=[],
            turns=[], evidence=_ev(log=False))["pass"], False),
        ("plan_rounds intent-judge 档=fuzzy+consume 两轮（无 QA 依赖）", plan_rounds(
            then_jump=None, has_qa=False, intent_judge=True,
            trigger_text=TRIGGER_TEXT, nontrigger_text=NONTRIGGER_TEXT,
            play_text=PLAY_TEXT, after_text=AFTER_TEXT, play_round=True
        ) == [("fuzzy", FUZZY_TEXT), ("consume", AFTER_TEXT)], True),
        ("graph_json 默认/then-jump 档不带 judge 键",
         all("judge" not in i for i in json.loads(build_graph_json("qa-1"))["intents"])
         and all("judge" not in i
                 for i in json.loads(build_graph_json("qa-1", then_jump=TARGET_STEP))["intents"]), True),
        ("graph_json intent-judge 档判据挂投诉意图（prompt 1-400）",
         1 <= len(next(i for i in json.loads(build_graph_json("qa-1", judge=True))["intents"]
                       if i["id"] == "int_1a2b3c4d")["judge"]["prompt"]) <= 400, True),
        ("parse_judge_events 拆词（含 reason/intent 字段）", parse_judge_events([
            "FLOW_GRAPH judge_scheduled intents=1 step=1 (call c-1)",
            "FLOW_GRAPH judge_skipped reason=stale (call c-1)",
            "FLOW_GRAPH jump binding=bnd_7e8f9a0b step=4",  # 非judge行必须不收
        ]) == [
            {"kind": "judge_scheduled", "intents": "1", "step": "1"},
            {"kind": "judge_skipped", "reason": "stale"},
        ], True),
        ("FUZZY_TEXT 不含任何图触发/play 关键词（腿前提）",
         not any(k.lower() in FUZZY_TEXT.lower()
                 for k in TRIGGER_KEYWORDS + PLAY_KEYWORDS), True),
    ]

    failed = 0
    for label, got, want in cases:
        if bool(got) != bool(want):
            failed += 1
        print(f"  [{'PASS' if bool(got) == bool(want) else 'FAIL'}] {label}"
              f"（got={got} 期望={want}）", flush=True)
    print(f"FLOW_GRAPH_PROBE SELFTEST {'PASS' if not failed else f'FAIL({failed})'}", flush=True)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
async def main() -> int:
    parser = argparse.ArgumentParser(description="话术图实弹验收探针")
    parser.add_argument("--expect-off", action="store_true",
                        help="kill-switch 腿：须先以 BOK_FLOW_GRAPH=0 重启 serve（env 进程级定死）")
    parser.add_argument("--lang", default="zh", choices=["zh", "cantonese", "en"])
    parser.add_argument("--persona-voice", default="", help="空=按语言默认音色")
    parser.add_argument("--trigger-text", default=TRIGGER_TEXT)
    parser.add_argument("--nontrigger-text", default=NONTRIGGER_TEXT)
    parser.add_argument("--play-text", default=PLAY_TEXT)
    parser.add_argument("--no-play-round", action="store_true", help="不推 play_qa 信息位轮")
    parser.add_argument("--then-jump", action="store_true",
                        help=f"追问链腿（Phase 3.3）：play_qa 绑定带 then_jump={TARGET_STEP}，"
                             "轮次表=播+跳后两轮；跳后步号与同轮 jump 日志取 OR 判据")
    parser.add_argument("--after-text", default=AFTER_TEXT,
                        help="--then-jump 的跳后轮话术（须避开确认/收线/异议/提问七族词与图触发词）")
    parser.add_argument("--intent-judge", action="store_true",
                        help="意图判据腿（Phase 3.4）：投诉意图挂 judge.prompt，轮次表=fuzzy+consume"
                             " 两轮（fuzzy 刻意不含关键词）；kill 腿须先以 BOK_FLOW_GRAPH_JUDGE=0 重启 serve")
    parser.add_argument("--fuzzy-text", default=FUZZY_TEXT,
                        help="--intent-judge 的模糊触发轮话术（须避开图全部触发/play 关键词，保持抱怨语义）")
    parser.add_argument("--judge-soak-s", type=float, default=JUDGE_SOAK_S,
                        help="fuzzy→consume 之间等候判定落账的真实秒数（盖 FLOW_JUDGE_DELAY+9B 往返）")
    parser.add_argument("--keep-template", action="store_true", help="保留探针模板（默认跑完删）")
    parser.add_argument("--budget-first-ms", type=float, default=2500.0)
    parser.add_argument("--budget-perceived-ms", type=float, default=3000.0)
    parser.add_argument("--selftest", action="store_true", help="无栈纯函数自检后退出")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    voice = args.persona_voice or erc.SCENARIOS[args.lang]["persona_voice"]
    budgets = {"first_ms": args.budget_first_ms, "perceived_ms": args.budget_perceived_ms}
    res = await run_leg(
        expect_off=args.expect_off, lang=args.lang, voice=voice,
        trigger_text=args.trigger_text, nontrigger_text=args.nontrigger_text,
        play_text=args.play_text, play_round=not args.no_play_round,
        keep_template=args.keep_template, budgets=budgets,
        then_jump=TARGET_STEP if args.then_jump else None,
        after_text=args.after_text,
        intent_judge=args.intent_judge, fuzzy_text=args.fuzzy_text,
        judge_soak_s=args.judge_soak_s,
    )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{int(time.time())}-{res['leg']}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[flow-graph] JSON 报告 → {out}", flush=True)
    if res.get("skipped"):
        # 未评估 ≠ PASS：无 QA 条目时链腿跑不起来，显式跳过并以退出码 1 收尾
        # （不许把「没跑出东西」读成「零痕迹=PASS」；音频未物化不走这条——见 run_leg）。
        print(f"FLOW_GRAPH_PROBE leg={res['leg']} SKIPPED ({res['skipped']}) → FAIL"
              "（腿未评估；无 QA 条目——先建条目，再 python tools/bok.py tts-pregen --qa）",
              flush=True)
        return 1
    checks = res["verdict"]["checks"]
    print("FLOW_GRAPH_PROBE leg=" + res["leg"] + " " +
          " ".join(f"{k}={'1' if v else '0'}" for k, v in checks.items()) +
          f" → {'PASS' if res['verdict']['pass'] else 'FAIL'}", flush=True)
    if args.expect_off:
        kill_env = "BOK_FLOW_GRAPH_JUDGE" if args.intent_judge else "BOK_FLOW_GRAPH"
        print(f"（kill-switch 腿：须以 {kill_env}=0 重启 serve，探针不代重启；env 经 bok.py "
              "_BOK_PASSTHROUGH_KEYS 白名单透传（dev serve merge os.environ，白名单真正兜底 "
              "prod 封闭 env 面）——若本腿仍 FAIL，先核对 worker 进程 env 里到底有没有 "
              f"{kill_env}，再怀疑引擎）", flush=True)
    return 0 if res["verdict"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
