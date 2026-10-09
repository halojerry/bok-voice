# W6 B 线流畅度（不拆碎 · 整句流式）——2026-10-09 立项

## 1. 实弹证据（call-107f3f67, 2026-10-09 08:26 本地, zh→cantonese, solo 测试）

fwd 方向轨迹（interp-fwd.log slice @46983）：

```
INTERP_SPEC fire chars=15 → CLAUSE_COMMIT chars=15 lag_ms=4175 → defer wait 2s → defer-fallback
INTERP_LAG src_chars=15 mt_ms=634  first_ms=621  perceived_ms=5718   ← spec 兜住 MT(634ms)
CLAUSE_COMMIT chars=12 lag_ms=6953（busy: mt=1 → spec 被闸）
INTERP_LAG src_chars=12 mt_ms=4947 first_ms=4945 perceived_ms=8483   ← 全款 DeepSeek 早高峰
INTERP_LAG src_chars=6  mt_ms=3540 first_ms=3538 perceived_ms=9958   ← 排队复利
```

rev 方向（cantonese→zh）**整通零提交零翻译**（solo 测试对方侧没说话，非 bug）。

近 60 条 INTERP_LAG 聚合（跨近日全部会话）：

| 指标 | 值 |
|---|---|
| src_chars | p50 **10** / max 53（碎片极小） |
| mt_ms | p50 **872** / p90 1168 / max 5450（MT 腿整体健康, >2s 仅 3/60） |
| perceived_ms | p50 **4027** / p90 6277 / max 8715（**75% 超 3s**） |

`lag_ms` 语义 = 首个 interim 进闸→提交（doubao_asr.py:730），4175ms 里 ~3.5s 是说话本身，闸尾 ~0.7s。

## 2. 归因（碎片化是病，不是 MT）

1. **提交粒度太细**：逗号档（6 字）+ 限速 1.0s → p50 10 字碎片 = 每段 TTS 只出 ~2s 声。
2. **管道填不满**：每碎片播 2s，下一碎片 MT 要 1-3.5s → 段间开天窗；连续说话时 perceived 复利攀升（5.7→8.5→10s）。**碎片→各自 MT RTT→串行→天窗**，这就是「断断续续、讲不完一句」。
3. **spec 2s 确认窗 < 提交滞后**：fire 了但 defer-fallback，等 FINAL 的机制本身把 spec 的收益吃掉一半（MT 634ms 已到手，确认却等到 4.2s 后）。
4. MT 峰值（早/晚高峰 3.5-5s）是**放大器不是主因**（p50 872ms）。

## 3. 参照系（为什么别人顺）

| 系统 | 切分 | 顺的机制 |
|---|---|---|
| **LiveKit 官方 pipeline_translator**（docs recipes） | **不拆句**——Deepgram endpointing 整轮进 LLM | `preemptive_generation=True` 在 interim 上就开翻，LLM token 流直灌 TTS，一轮一条连续语音 |
| **interpret-live**（github, sub-second） | 终端标点或 max 24-token cap | **LocalAgreement-n 稳定前缀**（连续 n 个 partial 不变才 commit，前缀单调不回收）+ 有界队列并发 + 本地 NLLB MT（RTT≈0） |
| **SeamlessStreaming / StreamSpeech / FASST**（学术） | 模型级 wait-k/单调 | speech-to-speech 模型内流式，不级联 |
| **人类同传** | 意群（phrase） | EVS 2-4s 是常态，靠意群边界+预判 |

共性：**翻译单元=自然停顿/句末，不是逗号；一旦出声就是连续长段**。Ethan 的「不要自己拆分」与参照系一致。

## 4. W6 刀具（全部 env 门控、默认保守、B 线专属不改 A 线）

- **刀1 提交升句档** `BOK_INTERP_SENTENCE_COMMIT`（默认 0=旧行为）：B 线 `_interp_env` 下 clause-commit 只认句末标点（。！？；?!），逗号档关闭；长度保险丝 8→**20** 字（连续语流超 20 内容字就地切，防憋死）；限速 1.0→2.0s。复用现有 `QWEN3_ASR_CLAUSE_*` 三键、只改 B 线注入缺省——**A 线零变化**（test_a_lane_env_untouched 钉）。
- **刀2 spec 稳定前缀确认** `BOK_INTERP_SPEC_CONFIRM_STABLE`（默认 0）：确认从「等 FINAL/2s 窗」改为「held PCM 在 (投机 MT 完成 ∧ interim 前缀跨 2 次稳定) 即放」，官方 commit 只做余段对账（HIT 余段机制不变）。把 4.2s 确认尾砍到 ~0.5s。
- **刀3 管道保满观测**：句级单元下 MT(≤3.5s) < TTS 播放(6-8s) → 管道自然满；INTERP_LAG 增加 queue_wait_ms 分解列（commit_lag/mt/tts/queue 四段账），验收用。
- **MT 车道**：维持 DeepSeek（p50 872 健康）；晚峰 18-22h 复测若 p90>4s 再议峰时臂（qwen-mt RPM60 不适合主车道，留 spec 预热臂候选）。

## 5. 验收

- 真人连续语流（≥40 字长句 ×3 通）：perceived p50 ≤2.5s、段间天窗（TTS 静音 >800ms）计数 ≤1/句、单段出声时长 p50 ≥4s（=不再 2s 碎片）。
- `e2e_interpret` 8/8 不回归；`probe_interp_duplex`/`probe_interp_backlog` 绿；A 线 env 面零变化。
- INTERP_SPEC fire/hit/block 计数对账（hit 率目标 ≥30%）。

## 6. 关联未完票（2026-10-09 盘点快照）

晚峰 18-22h 终判 / 官方三臂耳测（BOK_DOUBAO_NONSTREAM/DDC/FIRST_TOKEN_BOOST）/ W4 追帧（OLA+speed 两层）/ W5 TranscriptSynchronizer / A 线 REPEAT_GUARD_CANCEL_DROP+CP famine Sentry / 真跨向回声去重（CP relay）/ carry buffer（doubao VAD START 层独立票）/ subagent 配额 10-10 恢复。
