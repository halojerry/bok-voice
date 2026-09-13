---
name: call-diagnosis
description: 通话质量诊断 playbook。当用户给出 call id（如 call-46b94ebd）抱怨通话问题——吃字/不回话/复读/乱推进/回声/卡死/延迟——用本 skill 的三件套取证法快速定位根因。先读本文件的症状速查表，再按需读 references/ 下的标记词汇表、代码地图、probe 用法。
---

# 通话质量诊断

诊断任何"这通电话有问题"的请求。核心方法：**turns 账本 → agent.log 标记窗口 → 离线复算**，三面证据对齐再下结论，不猜。

## 第 0 步：确认运行代码来源

```bash
ps aux | grep agent_runtime | grep -v grep   # 看 worker 进程路径属于哪个 worktree
```

日志行为与当前源码对不上时，先怀疑跑的是旧代码/别的 worktree（殭尸 worker 见 markers.md 尾部）。

## 第 1 步：turns 账本（对话形状）

```bash
DB=~/Library/Application\ Support/BokVoice/bok_voice.db
sqlite3 -separator '|' "$DB" "SELECT created_at,role,speaker,gen,template_step,latency_ms,perceived_ms,replace(replace(transcript,char(10),' '),char(13),'') FROM turns WHERE call_id='call-XXXX' ORDER BY created_at;"
```

读什么：gen 列（llm/script/filler/qa_fastpath——只剩 filler=回复饿死）；template_step 跳变（假推进）；perceived_ms（>3000 即超标）；user 轮文本完整性（吃字/词表污染）。

## 第 2 步：agent.log 标记窗口

```bash
grep -n 'call-XXXX' ~/Library/Application\ Support/BokVoice/logs/agent.log | head -3   # 拿行号窗口
sed -n 'START,ENDp' ~/Library/Application\ Support/BokVoice/logs/agent.log | grep -vE '"name": "livekit.agents"|deprecat|adaptive'
```

日志时间戳是 UTC（本地=+8）。各标记的含义查 **references/markers.md**。

## 第 3 步：离线复算（决策层复现）

用真实模板+对象复现 flow 决策，钉死"规则当时为什么这么判"：

```bash
cd <worktree> && PYTHONPATH=apps/agent:packages/core:packages/business-db ../voice-assistant/.venv312/bin/python -c "
import json, sqlite3
from agent_runtime.flow import FlowController, decide_advance, should_auto_advance
db = sqlite3.connect('$HOME/Library/Application Support/BokVoice/bok_voice.db')
# ... 取 template steps_json + object 卡,FlowController.from_template,设 current,
# 逐轮回放 rule_verdict / should_auto_advance 对照日志
"
```

## 症状速查表

| 症状 | 首查标记 | 常见根因（按概率） |
|---|---|---|
| 回复被吃/没答上 | `MINIMAX_BIDI_PERF sentences=0 canceled=1` | 碎片轮打断在途回复（REDECODE/犹豫门已治，查新形态） |
| 用户话被吃字（头） | 转写首字 vs 实际口述 | START pre-roll 丢失（已修 2026-09-12，回归则查 `_recognize` START 分支） |
| 用户话被吃字（尾） | `QWEN3_ASR_REDECODE_DROP` | 重解丢弃门（长度感知取尾已修，查 `_uncommitted`） |
| 真答案被丢 | `QWEN3_HOTWORD_ECHO_DROP/STRIP` | 词表回声守卫误杀（剥尾保头已修，查 `_echo_filter`） |
| AI 傻念话术/复读 | 回复 vs 步正稿相似度 | 尾部注入形态（渐进披露已修，查 `current_step_text`/`match_step_branch`） |
| 两轮回复一字不差 | `REPEAT_SELF_SUPPRESSED` 是否触发 | 出口复读防线漏网（查 `_RepeatSelfGuardStream`） |
| 流程乱跳步 | `[flow] rule=auto/judge(bg)=` 行 | 假推进（say_step/judge 门槛已修，对照复算） |
| 只剩垫话不回正事 | `BOK_FILLER fired` 连发 + 无 LLM 轮 | 碎片饿死/DEFER 误拦 |
| 延迟超标 | `PERCEIVED_MS`（eou+llm+tts 三段） | 三段拆账：LLM_TTFT 含 cached=N/M、TTS_FIRST_AUDIO、eou |
| 卡死/哑火 | `draining worker` / 进程消失 | worker 死/serve 竞态（bok.py 已修，`bok.py status` 看 worker 三行） |

按需深入：**references/markers.md**（全部日志标记词汇表）、**references/code-map.md**（症状→文件/函数地图）、**references/probes.md**（探针与 E2E 用法+GPU 竞态规矩）。

## 诊断产出格式

每例给：时间线表（turns+日志对齐）、根因链（带代码位置 file:line）、修复方向 + 预期收益。分析归分析——**动代码前先向用户呈根因**（本仓惯例）。
