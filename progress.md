## fixed-language

A 线每通对话语言固定（产品决策，取代逐轮语言跟随）：粤语通话全程粤语、中文全程中文、英文全程英文，中途不切换。会话装配时一次钉死 ASR/LLM/TTS 三方（`apps/agent/agent_runtime/agent.py`）：

- `_call_language(persona, object_card)`：人设(AI)语言 → 对象语言 → zh，装配时一次决定，ASR/LLM/TTS 共用。
- ASR 恒钉定：`PinnedLanguageState(lang=_call_asr_pin_language(asr_cfg, greet_lang))` + `pin_language=True`，hint 整场=通话语言（zh 也下发 Chinese）；设置 `asr.language_mode=fixed`+显式 `language` 仍优先，mode=auto 在 A 线=钉到本通语言（滞回跟随退役，B 线不变）。en 会话：sidecar `/api/start language=English`（partial 与整句两路同源 `_asr_language_hint`），finish hint 随开语言，无需额外接线。
- 共享 `language_state` 改用 `PinnedLanguageState(greet_lang)`：TTS `_resolve_voice`/`_speech_lang`/lecture_guard/联网语言整通恒定。
- LLM：【用户语言】规则装配时 `context_state.set_user_language(greet_lang)` 一次（P4-C 沿革），逐轮钩子不再写——前缀从第一声起字节静态。
- 逐轮钩子 `on_user_turn_completed`：删除 `_sticky_reply_language` 调用块 + `set_user_language` + 「回复语言切换」抢跑标记；流程推进/REFUSE/WhatsApp 标记原样保留。`_sticky_reply_language` 定义保留但标废弃（无调用点）。
- TTS：MiniMax 构造前按通话语言注入 `MINIMAX_LANGUAGE_BOOST`（zh→Chinese、cantonese→Chinese,Yue、en→English，与 B 线 interpret 同源同值）；每 job 赋值，同进程并发多 job 覆盖局限已在注释留档（livekit 默认每 job 一进程）。
- 术语门禁：`tests/test_cantonese_terminology.py` `_ALLOWLIST` 为 agent.py 与 test_fixed_language_call.py 加 `Chinese,Yue` 行级豁免（MiniMax API 真字面量，与 interpret.py 同条目）。
- 文档：AGENTS.md 语言铁律条改为 per-call 固定政策（auto 语义变化、滞回退役、B 线不变），推进条目去掉逐轮语言锚定/语言切换标记。
- 测试：`tests/test_agent_language_follow.py` 撤下 3 个 sticky 跟随测试（新政策回归点移至新文件）；新增 `tests/test_fixed_language_call.py`（装配解析优先级、ASR 钉定+显式覆盖、hint 三语全钉、TTS 钉死、boost 映射/逐 job 注入、逐轮钩子零语言处理源码门禁、混语言轮次前缀字节稳定）。
- 门禁：`python -m compileall` 全绿；pytest tests/ 285 passed（含并行 agent 正在改的 livekit_plugins MiniMax 区域当日状态）。
- 未动（本轮范围外）：web 设置页 `asr.language_mode` hint 文案仍写「auto=跟随」旧语义（auto 在 A 线现为钉到通话语言），下轮 web 侧补文案。

## fixedlang-bidi fix round

Review findings 三修（基于 fixed-language + bidi 未提交工作之上）：

- **FIX 1（bidi 残留音频泄漏，Important）**：`_MiniMaxBidiSession` 增加 per-stream 纪元号（`active_epoch`/`alloc_epoch()`）——流在首个 `task_continue`（含 lecture 罐头路径）才认领纪元；`_recv_loop` 里本流纪元未认领时收到的一切 `audio` 一律丢弃并打点 `MINIMAX_BIDI_DROP_STALE`（首条）+ `MINIMAX_BIDI_DROP_STALE_TOTAL`（收摊汇总）。堵住 cancel 超时（`MINIMAX_TTS_BIDI_CANCEL_TIMEOUT`）连接保留复用后，被打断那句的迟到音频漏进下一轮 stream 的泄漏。新 fake-WS 测试 `test_cancel_timeout_stale_audio_gated_for_next_stream`（`tests/test_minimax_bidi.py`，`_QueueWS` 可注入服务端消息）：cancel 超时 → 旧流残留（0x11 哨兵）丢弃、新流 continue 后音频（0x22）照常转发、两流一条连接；mutation 验证（禁用门禁 → 测试红）。
- **FIX 2a（zh 钉定 live 实证）**：TTS sidecar :8788（serena，language=Auto）合成混英句「你好，我哋幫你 check 下 WhatsApp 同埋 order status，唔該稍等。」→ /tmp/zh_mix.wav（16k PCM 4.8s）→ ASR sidecar :8787 两次整句转写：`language=Chinese` → 「你好，我得帮你check下WhatsApp同埋order status。你该稍等。」；无 hint（auto）→ 「你好，我得帮你 check ha WhatsApp for my order status。你该稍等。」（lang 判成 English）。**结论：Chinese hint 下英文词全部保住（check/WhatsApp/order status 原样），auto 反而烂（下→ha、同埋→for my）且误判 English**——维持 zh→Chinese hint，不改 `_call_asr_pin_language`。
- **FIX 2b（陈旧注释三处改 per-call-fixed 口径）**：`services/qwen3-asr-sidecar/app.py` `_finish_language_hint` docstring（zh 原样透传=Chinese hint，实测夹英文保得住；空才 auto）+ `/api/start` 行注；`livekit_plugins.py` `_ASR_LANG_HINTS` 块与 `_post_audio` 块（生产两处 agent.py/interpret.py 都 pin=True 三语全钉，pin=False 只係构造缺省逃生口）。
- **FIX 3（boost 部署覆盖，Minor）**：`_apply_minimax_language_boost`（agent.py）改为仅在 env 缺失时写入 `MINIMAX_LANGUAGE_BOOST`——部署显式预设（含空串=有意禁用）不覆盖；同进程多 job 首写留存局限已注释留档。`tests/test_fixed_language_call.py` 拆 `test_apply_boost_env_preset_wins`（English 预设/空串预设都赢）并修 per-call 注入测试（每段 delenv 模拟新 job 进程）。
- 门禁：`python -m compileall -q apps services` 绿；`pytest tests/ -q` **295 passed**（含新 bidi 门禁测试 9/9）。
