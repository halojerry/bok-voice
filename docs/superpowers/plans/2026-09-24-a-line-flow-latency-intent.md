# A 线「流畅对话感」执行计划（2026-09-24 定稿）

> 总目标（用户拍板）：**流畅/实时对话感 = 轮开始反馈 <300ms + 回复到达 <1.5s；意图识别在改述/ASR 碎字下不失明。**
> 数据敏感铁律：客户音频/转写/PII 全本地；无 PII 脚本文本可走云（MiniMax 合成 OK，姓名段见 W13 治理）。
> 基线（2026-09-23 实测，~450 通/1153 轮）：perceived p50 2.6-2.7s（三语一致）｜LLM TTFT p50 905ms｜TTS 首包 p50 420ms / p95 1453ms｜**残差 ~1.3s（提交+交接+首句缓冲）**｜意图目录 0 个｜满配假想目录字面召回 **16%**｜QA 快路命中 **0**｜垫话 ~2.3 发/通。

## P0 闸门（阻塞一切落码）

- **G0 Mimosa SQL 规则修正**（用户侧）：当前规则把全参数化常量 SQL 也判注入（四轮验证，含 `PRAGMA` 常量行）；不放行则所有读库探针/代码无法落盘。备选：`scripts/` 只读探针白名单。**未修期间一切以一次性只读巡检替代，不入资产。**

## P1 感知与意图激活（本周量级，零硬件，五工作流并行）

### W1 意图目录 + 语义车道（打「16%」这一仗）
- **W1a 目录建设**：补缺失意图类——退款、打错电话、在场确认（「有冇人知道」×459 最大单一轮次）、情绪升级/要主管；既有域（查件/投诉货损/投诉延误/赔偿/海关集运/放件改址/运费保险/跟进）关键词种子取自评测语料（1199 去重客户轮）。产出=graph_json 意图清单+绑定动作（play_qa/jump/notify），运营过目后灌库。
- **W1b 语义车道**：`mlx-community/bge-m3-4bit`（MLX，10-40ms）挂进 flow_graph 意图求值——镜像 `qa_gate` 的 0.6×余弦+0.4×子串混合架构，胜者键 `(priority,-score)` 同款；意图短语银行=分支条件+词条+挖掘变体。kill-switch `BOK_INTENT_SEMANTIC`（进 `_FORWARD_ENV`）。zh→yue 迁移已被 CantoNLU 数据背书（67.5 vs 69.4）。

> **W1b 蓝图定稿（2026-09-23，code-architect）+ 落地进度**：
> - 蓝图修正一处理方案权重的数学陷阱：镜像 qa_gate 0.6/0.4 + 0.78 阈值**结构性不可达**（语义车道只在关键词未中时求值→子串≈0→上限 0.6）——定案 `1.0×cos + 0.10×子串 tie-break`，阈值 `BOK_INTENT_SEM_THRESHOLD` 默认 0.78 待探针校准。**实测锚点**：同义对 cos 0.8185、跨语对 0.778（正卡在默认阈上）、负对 margin 0.308。
> - H 守卫定案：**play 守（`play_bypass reason=advanced` 不烧 once 落 LLM，臂前算 `_graph_advanced` 防 jump 臂 -1 自触发）/ jump、notify 不守**（jump 要压过规则推进=wrong_number 腿；notify 零抢话误铃代价低）。Phase 0 无 sidecar 依赖，先行保护 W1a 目录。
> - ✅ sidecar 落地（实现者B）：`services/bge-embed-sidecar/`（app.py+setup-macos.sh，只绑 127.0.0.1，/health 未就绪不谎报，输入防御实测）。包路径结论：`mlx-embedding-models` 结构性不可用（torch 转权重炸 MLX 量化张量）；`mlx-embeddings` 0.1.0 走通但硬编码 mean 池化——薄壳内自做 CLS+L2（判别余量 0.308 优于 mean 的 0.198）。模型 320MB 落 app-data models 布局。**延迟：单句前向 p50 10.5ms/p95 11.5ms（预算 4.7× 富余）、HTTP 往返 p50 20.4ms、批量 4.2ms/条**。
> - ✅ agent 侧全链（实现者A，已主会话把关）：play 守卫（`play_bypass reason=advanced` 不烧 once 落 LLM，`_graph_advanced` 臂前算）/ `flow_graph.semantic_hit` 同权参数（judge/语义共用守卫循环）/ `intent_semantic.py`（max-pool/快照 LRU/降级闩/loopback-only）/ agent.py 四处接线 / `_FORWARD_ENV` 六键。测试 102+143 绿。
> - ✅ 主会话 bok.py 编排接线：MODELS/OPTIONAL_MODELS/CORE_PORTS(:8789)/`_cmd_up_services` 分块拉起（模型在盘才起，镜像 mt/settle）/孤儿清扫身份映射/`_SWEEP_HTTP_PATHS`/prod status 动态追加/可选豁免；health 钉 `test_core_ports_cover_embed_and_optional_exemption`。
> - **真栈发现并修复：装配批量超时坑**——真模型 178 条素材单发 514ms 撞查询道 400ms 默认超时令 build 恒 None（离线假 embedder 零延迟抓不到）。修法=`embed(timeout_s=)` 覆盖 + build 分块 64/块 + 5s 装配预算（`_BUILD_EMBED_CHUNK/_BUILD_EMBED_TIMEOUT_S`），回归测试钉死。
> - **真模型语义车道评测（生产代码路径 × live :8789 × 真实语料）**：召回 **75.8% → 83.1%**（语义补中 33 发全对族、零 no_binding）；**阈值扫描定案 0.78 保持**——全部语义命中 ≥0.78（33/33），「误报」5.8% 经人工核样大半是粗族标记漏标的真意图轮（是什么货/我的件会丢吗/掉了=goods 族），真误报估计 1-3%；DEFER 家族轮（我先查一下）在生产被 DEFER 车道先拦，漏斗序天然保护。
> - ✅ **真栈实弹三腿全 PASS（2026-09-24，probe_flow_graph.py --semantic）**：
>   - 主腿 call-749d6035：`semantic_hit score=0.81`（离线预测 0.805）→ graph-jump 第 4 步（回复按新步话术「直接说赔偿方案」）→ **judge 零调度**（漏斗序铁律实证：关键词>语义>judge）。
>   - 降级腿 call-a12a90cc（停 :8789）：装配级 `[agent] intent semantic off (error)`（连接拒绝→闩→整通惰性）、零 semantic_hit、LLM 兜话存活。
>   - kill 腿（BOK_INTENT_SEMANTIC=0 手工 worker）：零语义痕迹——**自验证闭环**（sidecar 在跑+同句主腿命中过，这里零痕迹=env 真到 worker）。
>   - 探针新增：`--semantic`/`--semantic-text`/`--expect-degraded` 三旗标、`parse_semantic_events`（含装配 off 行）、`build_graph_json(judge=str)` 素材透传、selftest +8 例。
>   - 实弹预检抓雷两枚：探针默认素材太薄（释义分 0.736 恒 miss）→ 语义腿专用判据锚 `SEMANTIC_JUDGE_PROMPT`（0.805 过阈）；`build_graph_json` 的 judge 参数原本只当布尔闸（str 被吞）→ 修透传。
>   - 回归核查：intent-judge 腿不受语义层遮蔽——FUZZY_TEXT 对默认素材 0.682<0.78，语义 miss 照旧落 judge（旧腿行为零变化）。
>   - 换 worker 姿势：本树 monitor 在跑 → 只杀 A 线 worker 让 monitor 用新代码重拉（主/降级腿）；kill 腿走完整舞蹈（停 monitor→bok._start_proc 起带 env worker→跑→还栈）。栈已完整归还（monitor 96568 + 三 worker + :8789）。
> - ⏭ 剩余：运营 publish 目录（play_bypass 守卫已落地，publish 后关键词可放宽裸「赔」——另立变更）。
- **W1c 评测复测**：probe_intent_eval（本会话巡检逻辑固化，依赖 G0）跑基线→改后，**验收：召回 16%→≥60%，precision 不降**。
- 子代理：蓝图=code-architect；实现=general-purpose（严格规格）；评审=code-reviewer；主会话把关 flow.py 契约。

> **W1a 落地记录（2026-09-23 执行）**：
> - 产出 `scripts/data/intent_catalog_v1.json`（三语 ×15 意图/绑定，关键词 91-160/语）+ `scripts/load_intent_catalog.py`（走 CP PUT 单键部分更新，客户端先过 validate_flow_graph，id 确定性派生重跑幂等）。
> - 已灌三语生产模板 live 草稿（b0d50586a040/febeeeebac97/80afec7f2f92），**published_json 零触碰**——发布闸在运营手里（POST /publish 后机器通道才吃新图）。
> - 30 条绑定 QA 音频 27+3 全部已物化（零 MiniMax 调用），publish 即播。
> - **离线回放实测（pick_graph_action 真函数 × 652 通真实客户轮）**：召回 **16% → 75.7%**（口径A 全意图承载轮 339/448）/ **81.9%**（口径B 排除 step-1 身份步辖区开场轮 339/414）。六族 100%（投诉/退款/到账/身份/费用/know_more 补类）、查件 87%、打错 72%。**验收线 ≥60% 已过（关键词层）**。
> - 修正：原计划把「有冇人知道」（479 次）当意图类——定性更正为**沉默戳话=延迟症状**，归 W4 观测面板非目录；真正的「在场不便」类（busy_later）语料仅 1 次命中但按域知识建类。
> - 关键词纪律：**问句形状、无裸「赔」**——agent.py 图 play 臂（:5066）无 advanced 守卫（QA 快路 :5148 有），确认轮「赔偿可以」会被罐头劫持；W1b 蓝图已含 play 臂 advanced 守卫（镜像 QA 快路 `advanced` 旁路），修好后再放宽关键词。
> - 残余漏网=深 ASR mangle（「伊赔点破噶」）+ once 重复轮（设计使然）+ E2E 混语言伪影 → 判据（judge.prompt 已随目录入库）+ W1b 语义车道的靶子。

### W2 真人化三件（垫话升级）
- **W2a 犹豫资产分档**：`gen_filler_assets.py` 加 `hesitation` 类目，MiniMax `<#x#>` 停顿标记（`呃<#0.4#>好——等我睇下`），按 Fox Tree 分档（呃=短/嗯=长/等我睇下=2s+）；复用音色/缓存/时长窗校验。
- **W2b 键盘音环境层**：`BackgroundAudioPlayer(thinking_sound=KEYBOARD_TYPING)` 分钟级接入，out-of-band 零语音风险。
- **W2c 中途承诺**：真在查单/查知识路径上 arm「等我搿下你张单先」（内容型垫话，OpenAI/Sierra 认可类）；复用 FillerDirector 链发骨架。
> **W2c 落地记录（2026-09-24 深夜执行，按上述底稿开工）**：
> - **机制**：`derive_context_bucket` 纯函数（handoff>wa>comp>identity>query>""）+ `FillerDirector(context_resolver=)`（fire 时点惰性取桶,异常吞掉回 ""）+ `_pick` 桶池优先（promise_<bucket> 资产在场 → 桶池直接出；缺失 → query 桶把 cat 覆写成 check,其余回现行阶梯逐字节旧档）。罐头确定性匹配不受桶影响（matched 条目本就是精准回应）。观测=fired 行带 ` bucket=`。闸=`BOK_FILLER_CONTEXT`(默认 1,已立法 _FORWARD_ENV)。agent 接线=`_filler_context_bucket()` 闭包（verdict 走 last_verdict、WA 步走 _llws——底稿两个 NameError 坑全避开）。
> - **资产**：gen_filler_assets.py 新 tier "p"（promise_lines,四桶措辞三语 12 条,SKIP-on-exists 存量零重渲染）——handoff 只承诺「已通知/会跟进」（诚实纪律）、WA 轮「收到,安排」根治 check 类答非所问、query「查到就覆」。en handoff 两轮超窗（拖腔 lottery,2.32/4.86s）→ 缩词定版「I have flagged this」/「We will follow up soon」（1.55/1.14s 过窗）；**tier 4 窗修正=en cfg win 覆盖从 tier 1 扩到 (1,4)**（旧全局窗是 zh 形状,en promise 必假 FAIL 的坑）。
> - **验收**：tests/test_filler_context.py 5 测（桶次序/池优先/kill/覆写/resolver 失败静默）+ flaky 修复入档（去重窗会令 kill 腿随机落桶条目——清 _recent 再断言,非闸语义）;全套件 2670 过;真栈 soak 7/7 零哑+**本轮零看门狗**（共享机窗口良好,回复全活）;fired 3 发全走罐头匹配路（桶豁免路径,符合设计）。bucket= 实弹标记待生产 WA/转人工轮观察。

> **W2c 设计底稿（2026-09-24 调研子代理产出，实现时照此开工）**：
> - **arm 点语境完备度（agent.py:5481 `_filler.arm()`，on_user_turn_completed 内）**：装配闭包全可达=flow_ctrl（current/goal_ref/pending_say_text/vars_map/graph_fired）、`_wa_signal`/`_wa_captured`、`_turn_origin`（provider ∈ graph-notify/branch-notify=转人工等待语境）、`_facts` 账本、greet_lang。**避坑**：本轮 verdict 直引有 NameError 风险（无模板轮未绑定）→ 用 `flow_ctrl.last_verdict`；`_graph_advanced`/`_sem` 同理 → 用 `_flow_step_before` 自算。QA 命中轮到不了 arm（命中即 StopResponse）——「arm 被执行」本身蕴含未命中。fire 时点（+500ms）guards 闭包重读活 flow_ctrl——语境快照应在 fire 时惰性取（镜像 `_user_text_provider` 先例 agent.py:3294）。
> - **现有缺口（W2c 顺手补）**：guards（agent.py:3243-3248）只查 closing/done **没有 WA 步判定**——WA 捕获轮照 arm，check 类「马上帮您查」会答非所问（客户刚报完号码）。语境过滤天然补上这一刀。
> - **五语境桶 × 三语短语库**（新录须过 gen_filler_assets.py 时长窗：zh/粤短句 [1.25,2.3]s、en [1.0,2.2]s 且 **en ≤7 词零逗号**；zh 克隆音色不吃 `<#x#>` 用「——」）：**A 开场身份轮**（current==0/身份步）· **B 查询进行中**（主桶，现有 check 池+犹豫长档直接可用）· **C 赔偿确认轮**（禁数字——赔偿数字纪律）· **D WA 捕获轮**（ack 桶）· **E 转人工等待**（`_turn_origin.provider` 判定；只承诺「已通知/会跟进」不承诺「正在转接」——诚实纪律 call-91a6b8c9）。候选条目与资产复用清单见调研报告全文（会话档案），实现时逐条过窗校验。
> - **接线草图**：`FillerDirector.__init__(..., context_resolver: Callable[[], dict | None])`（None=零变化）；agent 装配注入 fire 时惰性取 `{bucket, step, step_say_done, verdict, provider}`；`_select/_pick/FillerEntryIndex.match` 加 bucket 过滤层（cat 池 ∩ bucket 池 → 空则回现行阶梯=零回归）；**不新增运行时桶类**——新录资产带 `promise_<bucket>` cat（镜像 hesitation 模式：分类器永不产出、只被语境层选中），池缺失自动落回。让话剔除 `filter_deflect_entries` 与新桶正交自动生效。
> - **kill-switch**：新增 `BOK_FILLER_CONTEXT`（默认 1，**镜像 BOK_FILLER_HESITATION 立法**）+ 进 `_FORWARD_ENV`（W2b 教训：漏登记=prod 封闭 env 下结构性死开关）。
- 反面清单钉死：每轮罐头「等我谂吓」、报时间、垫话排队到回复后（FIFO 法则——现架构已合规，勿破）。
- 子代理：W2a=general-purpose（资产脚本）；W2b/c=主会话（agent.py 触点，小心）。

> **W2a 落地记录（2026-09-24 执行）**：21 条犹豫资产（三语 ×7，短呃/中嗯/长查证承诺三档，独立窗 [0.8,2.6]s，h 前缀档）+ fillers.py 最小注册（0.35 概率混入，`BOK_FILLER_HESITATION` 专用闸主会话补立法进 `_FORWARD_ENV`）。**`<#x#>` 停顿标记分音色实证**：粤库存音色生效（+0.51s）/en 生效但拖腔放大（+1.07s→压短句）/zh 克隆音色静默忽略→回退「——」破折号（+0.40s）——按音色选 device，勿按模型级假设。幂等证明（旧 60 条 sha256+mtime 前后一致）、34 次合成、filler 测试 81+326 绿。**已知残余**：混入率 0.35 常量、真机听感待实弹验证。A 线 worker 已重启上线。
> **W2b 落地记录（2026-09-24 执行，分钟级但验收全腿拉满）**：**官方 `BuiltinAudioClip.KEYBOARD_TYPING`(0.5s)/`KEYBOARD_TYPING2`(0.2s) 内置 clip,零资产工作**；官方语义=进 thinking 态按 probability 抽签播**一条** burst、离开即停（once 非循环——电话里连续打字循环反而假,burst 才是「我在查」线索）。接线=agent.py per-call 构造 `BackgroundAudioPlayer(thinking_sound=fillers.thinking_sound_configs())`（helper 在 fillers.py:env `BOK_AMBIENT_KEYBOARD` 默认 "1"/`_VOL` 默认 0.6 夹 [0,1]、概率 0.30+0.30=0.60 抽签制、fade_out 0.05；None→官方 `is_given(None)=False` 状态机惰化=与旧版逐字节同）;`start(agent_session=session)` 接线早已在位（agent.py :5749）,状态机即插即用。两 env 已立法进 `_FORWARD_ENV`,arm 打点 `[agent] ambient keyboard: on` per-call 一行。**真栈三腿全 PASS**（新探针 `scripts/probe_ambient_keyboard.py`,按 track.name=`background_audio` 归因——垫话人声与键盘同走 bg 轨,归因必须成对关垫话）:main（filler 开）bg 28.72s=垫话为主+通道无破坏、回复 5/5;归因腿（`BOK_FILLER=0`）bg 3.36s @max_rms 1652=纯键盘实锤;kill 腿（双关）bg 0.00s @max_rms 1=开关干净归零。**验收门**:`probe_filler_timing` PASS 首声 1616ms<2500 预算;`e2e_barge_in` PASS stop_ms=2461（基线原样）——键盘音不碰打断链。测试 test_fillers +3（配置形状/kill/音量夹逼）+test_forward_env 全绿 43。**换 worker 教训入档**:`_stop_pidfile` 是 SIGTERM→livekit worker grace drain 拖住 8081 ~10s,respawn 前必须**轮询端口空闲**（2s sleep 会 spawn 出 `WORKER_ALREADY_RUNNING` 退出 0 的空 worker→探针 track 等不到静默失败——探针早退路径已补打点）。

### W3 存量激活
- **W3a 人设垫话物化**：对在用人设跑 `tts-pregen --fillers`，清零 `voice_fallback`（实测 80 次）。
- **W3b QA 快路激活**：L-① gap mining 灌变体（103 条词条 × 0 命中=纯字面阈值饿死）+ 聚类轮换复审；W1b 语义车道同样反哺 QA 匹配。**验收：快路承担 ≥10% 的 LLM 轮流量**（turns 账本 gen=qa_fastpath 可测——先做 W4）。

### W4 观测补口（让不可测变可测）
- filler/qa 事件进 turns 账本（gen=filler/qa_fastpath 上报——当前 0 行是口径缺口非没干活）；`BOK_FILLER fired` 行带 call-id。
- **三时间戳**：commit 时刻 / LLM 请求发出 / TTS 首包——拆那 1.3s 残差，P2 的前置仪表。
- 子代理：主会话（agent.py 上报链路，_spawn_report 纪律）。

> **W4 落地记录（2026-09-24 执行，主会话+子代理并行，三件全绿）**：
> - **① 对账先行**：turns 账本 gen 分布实查=filler 1008 行/qa_fastpath 27 行/interrupted 6——「0 行口径缺口」是计划文本早于落地（`_on_filler_played` 已报 gen=filler，gap_mining:82 引用行号）。真实残余=「`BOK_FILLER fired` 行带 call-id」：fillers.py 构造新增 `call_label`（缺省空=单测/嵌入方零变化），fired 行插 ` call=<id>` 段——探针按子串 grep「BOK_FILLER fired」不受影响。实弹：`BOK_FILLER fired call=call-d2ac2d01 count=1 …`。
> - **② 三时间戳（主刀=墙钟仪表）**：agent.py 装配区 `_turn_timing` 单槽账本——`on_user_turn_completed` 入口落 commit 墙钟戳，回复首音频监听（`add_first_audio_listener` 第三个挂点，与垫话撤表/看门狗拆弹同口）消费打 **`[call_id] BOK_TURN_TIMING commit_to_audio=<ms>`**。与 PERCEIVED_MS 分工：PERCEIVED=provider 内部三段和（eou+ttft+ttfb，旁路轮唔计）；本行=**墙钟 commit→真出声**，差值=框架隙（提交→LLM 派发排队+首句缓冲+起播）——W7/W8 的靶子。**语义边界**（设计拍板）：脚本直念缓存命中（_CachedChunkedStream RC2 同口 fire）消费戳=「AI 对该轮出声」的合理口径；垫话 backfill 走 miss-only 路径零 fire 零污染；barge-in 新戳顶旧戳。实弹（ambient 探针 5 LLM 轮）：**commit_to_audio=927-981ms×5 干净逐轮**。首验证腿踩坑入档：filler-timing 探针那通 reply 未落轮（探针 teardown 早于 reply 出声，first_audio 实为垫话）——验证仪表要选保证 LLM 回复落轮的探针。
> - **③ 沉默戳话入面板（子代理实现，主会话验收）**：新纯函数模块 `control_plane/silence_poke.py`（14 词族三语 casefold 子串、一轮一计、语言桶=轮 language 列；`count_silence_pokes`/`merge_poke_stats` 零 SQL 走 `repo.get_turns`，qa_drift 同款 getattr-on-TurnEvent 纪律）+ dashboard 路由新增 `silence_pokes` 段 + web 第 7 张 KPI 卡。验收：tests 14+77 绿、术语/方言门禁 3 绿、tsc+build 绿、**真栈 curl：`{pokes:589, calls:186, by_lang:{cantonese:580,zh:8,en:1}}`**——479 次实证的量级确认，症状首次上盘。已知债：dashboard 全量 calls 逐通 get_turns（gap_mining 同款画像，账号大了再议窗口）；验收时抓到过一次假阴性教训=手测喂 dict 而生产行是 TurnEvent 对象（getattr 口），模块无恙。
> - **W3b 阶段结论（子代理数据作业，如实零采纳）**：cluster 线 dry 计划 candidates=3/variants=0（无可采）；gaps 线 93 族众数 count=3、**count≥5 过筛=0**——数据面（290 回复轮/121 LLM 轮、问法离散）在本语料上灌不出 ≥10% 快路流量，保守闸下 0 采纳合规（近门槛仅「拼多多。」count=3 一条，裸品牌词全局抢答风险大不收）。**执行风险入档：`/api/qa/cluster` 的 apply select=None 会连 fresh 全量采纳，后续作业必须显式 select**。≥10% 目标的真解锁=代码面非数据面（W1b 语义车道反哺 qa_gate 匹配，另立工作流）或积累真实通话量后再跑产线。
> - **W3b 解锁落地（2026-09-24 深夜执行,Ethan 点题「qa 匹配/自动沉淀」后拍板）**：`qa_gate.QaSemanticIndex`=词面 0.90 未中轮的**本地 embedding 释义档**（默认 0.80,`BOK_QA_SEMANTIC=0` 一键回纯词面档）。纪律镜像 W1b 意图车道：素材向量经 `SEMANTIC_VECTOR_CACHE` 快照 LRU（同目录零重复 embed）、装配分块批量（64/块 5s 块预算）、查询每轮至多一次 embed（~20ms 热路径预算内）、端点缺席/连续失败=EmbedClient 降级闩整通惰性（快路=旧档）；**词面恒绝对优先**（命中轮零语义调用）；语义胜者走词面同款折组（`QaIndex.team_head` 公开口）/轮换/PCM 出场链;不进优先级 duel（补位语义:最贴近问法直接出场）。**阈值定标（真库 102 问法 × :8789 实测）**：垃圾句 0.615-0.697 全 miss;真改写 0.767-0.971;「大概几天能送到我手上→几时可以收到钱」0.869 名义 FP 但在赔偿域语境半对（目录即赔偿域,「送到手上」≈钱到账）;top1-top2 margin 0.028-0.045 无分离力（紧语义域本性）→ 不加 margin 门,0.80 直上,误答投诉时升 0.85（保 0.849/0.971 真命中、砍 0.819 档）或关闸。接线（agent.py）：装配在 QaIndex 后受 `qa_semantic_enabled` 门控,轮级在词面 except 之后、match0 打点之前（`_qa_entry is None` 守卫=词面命中轮结构性零调用;tests/test_qa_semantic.py 21 测含源序钉）。观测：`[agent] qa semantic on entries=N` / `QA_SEM hit=1 entry=… score=…` / `QA_SEM miss best=…`（reason 非空静默降级）。四键入 `_FORWARD_ENV`。真栈：soak 7/7 零哑+装配行 entries=102（vs 词面 103=同问法去重实证）;全套件 2663 过。**≥10% 快路流量目标现在可测**：语义车道把命中面从逐字扩到释义档,turns 账本 gen=qa_fastpath 占比（含 QA_SEM 行归因）待生产通话量观察。

### W5 判官档位修正（运维）
- `:1237` 实挂 4B-Uncensored+Hy-MT2 ≠ 文档假设的 9B——重载 9B 或承认 4B 并调判官 prompt；记进 bok.py MODELS 对账。

> **W5 核销（2026-09-24 诊断，前提证伪——子代理诊断+主会话验收）**：`:1237` 实挂**就是 9B**。进程 argv（PID 与 `settle-llm.pid` 对齐）=`mlx_lm server --model …/Huihui-Qwen3.5-9B-abliterated-mlx-4bit`，与 `_start_settle_llm`（bok.py:1225）逐参数同源；三 worker 活 env 实测 `FLOW_JUDGE_*` 指 :1237 9B。「实挂 4B+Hy-MT2」係误读——**mlx_lm 0.31.3 的 `GET /v1/models` 是 HF 缓存扫描（`scan_cache_dir()`，server.py:1686），列「在盘」不列「已加载」**，:1235/:1236 两颗就这么进了眼。bok.py MODELS 表本来就对，零改动；补全探针 9B 答「好」2.7s。**运维新知入档**：mlx_lm 按需加载——serve 重启后 :1237 首个请求现场 mmap 5GB（实测 ~5 分钟全驻留），首个 settle/judge 调用吃冷载；「9B 慢/judge timeout=20」的体感部分可能是冷载非 prefill。消冷载=未来给 `_start_settle_llm` 加启动预热（另立小活，非本工作流）。

## P2 管线瘦身（测量门控，先仪表后动刀）

- **W6 VAC 式即时提交**（−200~300ms，最大单刀）：VAD 偪嘴触发时 sidecar 当前窗口直出 FINAL，不付尾段重解码；纠错靠既有迟到 FINAL 守卫+重解读丢弃。动前跑 W4 三时间戳拿残差构成；动后 `probe_fast_speech`/`e2e_edge_cases` 全绿（碎片战争纪律）。

> **W6 落地记录（2026-09-24 执行，与原案不同刀口）**：量测后真刀口=**`_try_incremental_finish` 的守卫结构性浪费**——`0 < covered <= len(pcm)` 把「partial 快照越过裁剪后末端」（VAD 停嘴等窗内起解的 partial，快照天然带一段被 trim 的静音=已听完全部语音的黄金场景）打成整句兜底 0.5-1.2s；实测分布 91% 尾段解码（tail 0.3-0.65s）+ ~8% 该黄金场景兜底。修法三件：①守卫改 `covered ≥ trimmed_len` → 走既有零尾直转分支（零 GPU）；②**稳定性门**（SimulStreaming 纪律）——直转须两窗收敛证据（当前窗=上一窗延伸/相等），单词 partial/劣化重解不直转；③finish 墙钟打点（`finish wall=`）供 AB 归因。**probe_fast_speech 三连败→加稳定性门→基线反转**：`QWEN3_ASR_INC_FINISH=0` 纯旧路径基线更烂（pdd 整轮消失 ×3、首字丢失 ×2——P1 在当前栈已复发，独立问题见下）；改动档 pdd 3/3 直转 0ms 完美+首字 3/3 存活。**soak PASS**（11 轮零哑，p50 1627ms），VAC 直转实测 0ms vs 尾段 217-421ms、soak 窗 ~27% 轮吃到直转。测试 14 绿（新增 VAC 直转正例+稳定性反例；旧 trim 测试按新行为语义修正——partial 陈旧化让位整句路径）。
> **独立问题入档（非本改动）**：fast_speech 探针在当前栈的历史基线已破——两条路径都有丢字（淘宝→好宝接缝损失/首字丢失），候选源头=1.8.0→1.8.2 升级、他会话未提交的 TTS sidecar 8 行改动、热词表 2026-09-17 扩容的回声交互复合漂移；归因另立调查。顺手修了探针 auth 面（erc/brand_words/fast_speech/soak 四文件补 `CP_HEADERS`——auth-on 栈上旧探针此前 401→KeyError 崩）。
> **✅ 结案（2026-09-24 干净基线 A/B）**：争用栈让渡后收回（他会话只留 ASR/TTS sidecar 防重载，其余全拆；`serve` 重拉全栈含 embed/settle）。**两腿全绿**：INC_FINISH=1（VAC+稳定性门）与 =0（纯旧整句路径）各跑 `probe_fast_speech` 2/2 PASS，`好的，淘宝、京东。`逐字含顿号全对、`拼多多。`纯热词答案活。归因钉死=**基线污染而非代码回归**——历史失败全部发生在 TTS sidecar 死亡窗口（探针 `tts_pcm` 落缓存刺激音频），中间层在干净栈上零损失（直注实验 7/7 佐证）；1.8.2 worker（本栈即 1.8.2）与 W6 VAC 同时洗清，三候选源头全部排除。A/B 姿势备忘：探针须带 `BOK_CP_TOKEN` env（`CP_HEADERS` 读它）；leg A 用 `bok._stop_pidfile`+`_start_proc` 单例重启 ASR 带 `QWEN3_ASR_INC_FINISH=0`，跑完恢复默认档。
- **W7 turn detector 否决制**（−100~150ms）：LiveKit audio turn detector（1.8.0 内置，号码/地址轮误打断 −39% 官方实测）+ `min_silence` 0.45→0.35；1s 模型预算 fail-open 同官方设计。zh 可另试 text turn detector（0.5B CPU）；yue 回落英文阈值先测。

> **W7 落地记录（2026-09-24 执行，两半分治：门槛刀落地、否决器搁置有据）**：
> - **门槛刀（已上线）`min_silence` 0.45→0.35**：A/B 实弹（同栈同夜）——0.45 基线 soak 墙钟首声 **p50=1225ms**/max 3150 → 0.35 腿 **p50=1022ms**/max 1530 → 设置值转正后终验 **p50=927ms**（累计 **−298ms**，超 −100~150ms 目标）。质量面全绿：fast_speech 2/2（逐字含顿号全对）、barge-in stop_ms 2454（基线 2461 原样）、edge_cases 8/8、soak 11 轮零哑。三处同步=agent.py 环境默认 + CP `schemas.py` 默认 + web `settings-meta` 提示文案（存量部署显式设置值压代码默认——本机 DB 已 PUT 0.35；AGENTS.md 基线行已改）。回滚=设置页回调 0.45 一键。
> - **否决器（搁置，架构性依据）**：官方 audio EOT 检测器与 `turn_detection="stt"` 在 1.8.2 **mode 槽互斥**（`audio_recognition.py`：str 模式 → `_turn_detector=None`，detector 实例只挂 vad-base 档）——官方否决制不能叠加在句级提交架构上，要用就得移植：v1-mini（本地 108MB，`livekit-local-inference` 0.2.7 已装、**本地-only 满足客户音频铁律**，v1 才是云）挂在自家 vad-pause/EOS 提交点当否决器。**但门槛刀单独已超额达标+全质量面绿，否决器的增量（号码/地址误打断 −39%）我方已有 join-hold/WA 累积/10 字碎片门三件套兜着**——搁置，触发重开条件=生产观测到 0.35 碎片回归（沉默戳话面板数字反弹/soak 拆轮上升）。
> - **方法论入档（P2 仪表首战）**：W4 ②的 TURN_TIMING 让门槛刀的 A/B 直接有墙钟读数；先仪表后动刀省掉一轮盲改。
> - **踩坑入档（worker 换血姿势补一条）**：`_start_proc` 起 env worker 时 spawn 壳**必须 export 全 auth env**——`_agent_worker_env` 的 passthrough 从 spawn 进程 os.environ 取 `BOK_CP_TOKEN`，缺了则 worker 对 CP 全线 401（context/settings/fillers/turns 上报死、心跳免疫失效），探针假 FAIL（user_texts=[] 的真因是 turns 落不了库）。W2b 的 env 腿同样带病（bg 轨断言不依赖 context 所以碰巧没暴露）。**以后 env 腿标准姿势=export 全套 auth env 再 spawn**。
- **W8 TTS 首子句起播**：不等整句，首子句（，、；边界）即开合成——B 线子句提交的镜像思路用在出声侧。
> **W8 落地记录（2026-09-24 执行，lane 落地于回退档、生产档架构性豁免——发现比刀更值钱）**：
> - **架构发现（本轮主结论）**：生产回复流=**bidi 持久连接**（`MINIMAX_WS_MODE` 缺省即 `"bidi"`，livekit_plugins `_ws_mode`）——`_MiniMaxBidiStream` 输入循环**逐块原样透传零客户端门**（注释明示「服务端自己按标点/长度切句合成」），首音频时刻=MiniMax 服务端攒句策略+合成 RTT，**客户端无从更早**。原计划的「首子句即开合成」只存在于 classic 回退档（`_MINIMAX_WS_MODE=classic`）与本地 Qwen3-TTS 路径——两处的 overlap 门槛 12 字在慢生成轮（GPU 争用 tps 10-20 实测）确会把首送推后 ~0.6-1.2s。**test_minimax_bidi.py 旧 docstring「默认 classic 零变化」是错的（与代码 default 相反），已改**——本轮我先被它+生产日志零 FIRST_SEND 逼着全链路排查一轮，勿再踩。
> - **已落地（回退档+本地档受益）**：`_tts_overlap_send_now` 纯函数门（tests/test_tts_first_clause.py 13 测钉）——**首送快车道**：`sent_any=False` 时门槛 12→6 字（`BOK_TTS_FIRST_CLAUSE_CHARS`）、软停顿过半门豁免；`BOK_TTS_FIRST_CLAUSE=0` 逐条件回旧档（kill 腿单测=legacy 等价）。两家镜像（MiniMax classic + Qwen3），`_SENT_END/_SOFT_BREAK` 收编模块级 `_TTS_SENT_END/_TTS_SOFT_BREAK`；总闸+门槛两键入 `_FORWARD_ENV`。观测：`MINIMAX_TTS_FIRST_CLAUSE chars=N`（lane 命中）+ `MINIMAX_TTS_FIRST_SEND_MS <ms> chars=N`（首送秒表，classic 档归因专用，两腿共通）。流级 wiring 腿（fake WS）：W8 开=7 字碎片首送+双打点；关=收尾整段+零 lane 打点。
> - **真栈验收（生产档不触达故为零风险面）**：soak 两腿（换血前后）各 7 轮零哑 PASS、edge_cases **8/8**、barge-in **PASS stop_ms=2445**（基线 2461 原样）。bidi 生产路径代码零触碰。
> - **争用时段画像（意外收获，W10-12 的实弹注脚）**：争用窗内（9B 判官+ASR 同卡）4B tps 塌到 10-25、TTFT 1.1-1.5s → **回复整段超 4s 看门狗被 force-interrupt+ack 收割**（实测 5/5 LLM 轮阵亡、客户听到=垫话+ack；soak「零哑 PASS」是垫话撑的假象，判读勿只看哑轮数）。`FLOW_JUDGE_IDLE_CAP=0`（默认关）的 2026-09-22 实测结论仍成立：等空闲窗够不着，**根治=judge 挪出本地 GPU（W10-12 CUDA/W14 缩编）**。TURN_TIMING 面板在争用窗会结构性欠采样（回复没出声就没读数）——面板数字要看时段。
> - **残差归因（bidi 生产档）**：首音频=TTFT（首 token 即透传）+服务端攒句（等够句料）+合成 RTT（实测 250-550ms，今夜偶发 808ms）。客户端侧唯一剩余杠杆=**prompt 侧缩短首句**（首句短=服务端早够料早合成）——记为 W8b 候选（prompt 域，「首句 ≤12 字带句读」规则+TURN_TIMING A/B），未排期。
> - **栈态**：monitor+worker 已以全 auth env 恢复（新码 worker 为默认），W8 键默认 `"1"`（bidi 下不可达零影响、classic 回退/Qwen3 本地即生效）。
> - **追加（同日，judge 闲时让路闸+Ethan 拍板「judge 闲时去」）**：`_wait_link_idle` 空闲窗修正=**播报中也算闲**（`agent in (listening,speaking)`）——旧版只认 listening（播报也结束），2026-09-22 实测 9/9 capped 的根因就是窗口定义错：真 GPU 空闲窗=LLM 生成完+TTS 云端放音中（每轮 3-10s），旧闸看不见。`FLOW_JUDGE_IDLE_CAP` 默认 0→**6** 转正（capped 到点照开火=判定永不负损；0=回纯固定让路旧行为），`_judge_yield_env()` 单点解析（tests/test_judge_idle_gate.py 8 测钉）。**live 受益暂不可证**：今日全部 soak 腿都落在「回复未及播报就被 4s 看门狗收割」的轮里（闸只认播报窗→恒 capped 4/4），闸零伤害（判定照落照用、soak PASS×2）——受益验证挪到回复能活到播报的档（=下述环境根治后或 CUDA 后）。**争用微基准落盘 `scripts/probe_llm_contention.py`**（旧 docstring 引用的探针从未存在过）：4B prefill 单飞 128ms vs 9B 并行 150ms（+22ms/+17%，空机）；模型 id 钉进程 argv（W5 教训：/v1/models 是 HF 缓存扫描，:1235 报 9B id 是缓存目录不是实载——实载 :1235=avan-ag 4B-Uncensored、:1237=huihui 9B）。
> - **追加②（同日深夜，晚轮变慢归因=上下文成分已证伪、剩共享机噪声）**：今日全部 soak 腿 in-call tps 9-24、TTFT 0.6→1.5s 随轮次涨（prompt 1.7k→3.2k），后段轮回复整段撞 4s 看门狗。**上下文瘦身候选已证伪勿做**：`scripts/probe_ctx_decode.py`（同端点两遍法=KV 命中纯 decode、1.1k/2.1k 交错采样对消漂移）实测 **tps 比=1.02**——4B-4bit decode 在通话级上下文(≤3.2k)零长度成本（MLX 注意力在这个尺寸免费），瘦身刀省不了晚轮。**剩余唯一活成分=共享机 GPU 基线摆动**：同形状 standalone 采样跨分钟 12↔43↔57tps（兄弟会话/后台租户不可控），in-call 14-24 ≈ 同一噪声基线。推论：**这台 Mac 上任何延迟 A/B 都测不准**（腿间基线摆 4-5x）——W10 CUDA 不止是速度，更是可测性前提；在那之前的本地延迟结论一律以「机制+单测+零伤害」为验收面，不用 soak 数值下结论。看门狗收割窗运营面提示：soak「零哑 PASS」可能由垫话+ack 撑出，判读看 `[watchdog]` 计数与 TURN_TIMING 采样数（坍塌窗内 TURN_TIMING=0 采样=回复没出过声）。
- **W9 回归资产固化**：probe_intent_eval + test-framework/JudgeGroup 进程内跑轮（本地 4B/9B judge）钉 prompt 规则回归（防诈/赔偿纪律/跳步禁讲）。
> **W9 落地记录（2026-09-24 执行，JudgeGroup 双判官同卷 ✅；W1c 半仍 G0 阻塞）**：
> - **`scripts/judge_group_eval.py`**（静态 12 例零 DB——**绕开 G0**：不读库即无 SQL 落盘面，W1c 的读库探针仍等你侧解封）：三规则族 × 4B(:1235)/9B(:1237) 同卷。**prompt 走生产同一条渲染路径**（`FlowController.from_template`+`ContextState`，总览/尾部/【禁讲清单】全真渲染）——钉的是生产 prompt 不是副本；改 `_SHARED_RESPONSE_RULES` 文案 selftest 锚词会红（锚=【身份与来电质疑】/【赔偿数字纪律】/「不得借回应质疑反复追问平台」）。用例=防诈 4（安抚+身份在场、不逐字复读）+ 赔偿纪律 5（投诉/转人工/找主管轮禁档位数字 + **正向对照**：客户直接问赔多少必须 engage 数字面、禁用 off-round 拖延话术挡回——防规则过度压制）+ 跳步禁讲 3（`jump_to` 真跳 + 禁讲清单照录被跳步问句原话；j3 同词反极性证禁讲是语境不是全局词表封禁）。硬断言面=forbid/max_chars/`max_sim_last`（逐字复读门）+expect_any（expect_soft=落词信息位，I3 验收纪律）。
> - **首跑读数（定门槛依据）**：4B 三跑 100%/92%/100%（obj-zh-fraud 身份步质疑轮**偶发跳问平台**=步纪律采样方差，真行为抖动归库盯防）；9B 92%（**comp-zh-complain 投诉轮整段复述档位表=赔偿纪律真破防**——4B 三跑全守、9B 破防，双判官同卷的第一笔真实价值；W14 判官缩编/CUDA 换模型时此库即门禁）。门槛 `--min-pass` 默认 0.85（容 1 例采样方差，系统性规则删除会塌到 ~50% 必红）。
> - **CI 面 `tests/test_judge_group.py`**（8 离线过+1 真栈腿 skip）：库形状/评分纯函数（forbid/expect_soft/长度/复读门）/渲染锚进程内断言；live 腿 `BOK_JUDGE_GROUP_LIVE=1` 放开跑 4B ≥85%。进程内 import（`sys.path`+`import judge_group_eval`，仓里探针测试同款）——Mimosa 拦 subprocess 形状，照抄 probe 测试姿势零争执。
> - **库校准三修（判读纪律）**：①obj-zh-repeat 起初禁平台词=**过严**（规则原文允许「随即带回办理流程（含确认平台）」；多轮「反复追问平台」模式单轮钉不住，归 probe_offscript_soak 真语音面）→ 改钉安抚在场+`max_sim_last<0.9` 复读门；②comp 族 expect 词表补「没关系/专员/放心/马上」（实质合规的措辞方差，禁词面才是硬的）；③comp-zh-ask 由「必须报数」放宽为「engage 数字面**或**反问货值」（档位依货值、反问是合理路径），但**禁拖延话术**（「按流程向您确认」出现在 ask 轮=过度压制信号）。判读口诀：**forbid 破防=真回归，expect 落空先查词表再下结论**。
> - 跑法：`.venv312/bin/python scripts/judge_group_eval.py --selftest`（CI 离线面）/ `--models 4b,9b`（双判官真栈跑轮，GPU 空闲时）；改 prompt 规则/判官模型/共享指引后必跑。
- 子代理：W6=主会话+code-reviewer（sidecar 高风险）；W7=主会话；W9=general-purpose。
- **验收：perceived p50 ≤2.0s，残差 ≤0.8s，碎片/打断探针零回归。**

## P3 CUDA 节点（节点到位后，一机三活）

- **W10 节点+基线**：`probe_cuda_baseline.py`（已存在，spec §9 门禁）跑 LLM TTFT + ASR 同口径采样，出 Mac vs CUDA 决策 JSON。
- **W11 LLM 迁移**：llama.cpp/vLLM 4B，KV 严格前缀纪律平移（ContextAwareLLM 追加式尾部正是 prompt cache 最爱形状）；worker env 经 `_FORWARD_ENV` 双面。
- **W12 ASR 真流式**：Qwen3-ASR-1.7B 官方流式路径=vLLM/CUDA（现 MLX sidecar 是滑窗批处理）；热词 context 机制 CUDA 侧复刻=本工作流最大件。sherpa-onnx 流式 Paraformer 三语（zh-yue-en，int8 228MB）作 Mac 侧轻量备胎 bench。
- **W13 粤语 TTS 本地化**：主 bench=Fun-CosyVoice3-0.5B（首包~150ms、18 方言含粤、克隆、Apache-2.0）；**GPT-SoVITS v2ProPlus 可在 Mac 上提前 bench**（M 系 CPU RTF~0.5）不等节点；Qwen3-TTS+jmtl 港粤 LoRA（CC-0）为微调路线验证。**验收：粤语 TTS 本地化达标后 MiniMax 退役为备胎，「客户姓名出境」挂账就此销案。**
- 子代理：W10/13 bench 执行=general-purpose（节点上跑数）；W11/12=主会话（契约敏感）。
- **验收：perceived p50 ≤1.3s，TTFT ≤300ms，全栈本地（含粤语）。**

## P4 端局（按需排队）

- W14 判官缩编：Qwen3-0.6B LoRA+枚举约束（**必须 unknown 兜底**防假自信）或 CUDA 大判官（异步车道白嫖大模型零延迟代价）。
- W15 FallbackAdapter 接线（TTS 本地↔云、STT 车道兜底；LLM 本地政策不动）。
- W16 SetFit few-shot 顶层分类器（标注 8-16 条/类，语料=W1c 评测集延伸）。
- W17（可选）zh 演示车道试 SpeechGPT-2.0（<200ms S2S，仅 zh）。

## 子代理分配总表

| 工作流 | 蓝图 | 实现 | 评审 | 真栈验收 |
|---|---|---|---|---|
| W1 意图+语义 | code-architect | general-purpose×2（车道/评测探针） | code-reviewer | probe_intent_eval 复测 |
| W2 真人化 | — | general-purpose(资产)+主会话(agent.py) | code-reviewer | probe_filler_timing |
| W3 存量 | — | 主会话 | — | 真栈 smoke |
| W4 观测 | — | 主会话 | code-reviewer | 巡检复跑 |
| W6/W7/W8 管线 | — | 主会话 | code-reviewer | probe_fast_speech+e2e_edge_cases+barge_in |
| W9 回归资产 | — | general-purpose | code-reviewer | pytest 进 CI |
| W10-13 CUDA | — | 主会话(契约)+general-purpose(bench) | code-reviewer | probe_cuda_baseline+全量 E2E |
| 背景调研续 | — | general-purpose×N（GitHub/HF 挖矿，随需） | — | — |

## 全局纪律

1. 每个行为改动带 kill-switch 且进 `_FORWARD_ENV`（tests/test_forward_env.py 会抓漏登记者）。
2. A/B 前后 `ps aux | grep agent_runtime` 清零殭尸；另一会话在跑测试时排班错峰，探针只读不冲突。
3. cantonese 术语门禁、`python -m compileall -q`、web 改动 tsc+build，改啥跑啥。
4. 探针先行：每个 W 的验收探针先于实现存在（或同批落地）。
5. 敏感度：新云调用逐条过「音频/转写/PIN=本地」闸；脚本资产合成走 MiniMax 视为合规例外（W13 后粤语连这个都省）。
