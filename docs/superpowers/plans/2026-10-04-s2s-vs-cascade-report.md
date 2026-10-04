# S2S 流控引擎 vs 级联引擎「同稿双跑」对照报告（2026-10-04，feat/s2s-spike）

> 任务：同一份四步粤语话术 + 同一份 corpus 语料，S2S 流控 worker（`bok-s2s-flow`）
> 与级联 FlowController 各真跑一遍，产出 A 线方案输入。**未改任何现有源码**；
> 只新增探针 `scripts/probe_s2s_vs_cascade.py`、本报告、经 CP API 建的模板/对象/通话。
>
> 一句话结论：**同稿同姿态下，粤语 A 线继续级联；S2S 的真实增量是「流控形态」
> （意图判定层溶解、步序零工程），不是延迟——本轮它每轮都比级联慢 1-3s，
> 且慢得有明确机制（无罐头腿、每轮 LLM、TTS 首包加固未迁）。**

---

## 1. 实验设置

### 1.1 同稿契约（两侧逐字同源）

| 步 | 正稿（S2S `FLOW_STEPS[*].script` = 级联 template 步 ref 首行） |
|---|---|
| 步1（开场核实） | 您好，請問係陳大文先生嗎？我係快捷快遞嘅客服，你有個包裹今日到咗我哋倉。 |
| 步2（通知） | 你件嘢係京東買嘅，而家喺我哋中轉倉，聽日可以派到。 |
| 步3（收号） | 麻煩您留個WhatsApp號碼，方便我哋發收貨確認俾您。（捕号后逐位复述确认） |
| 步4（收尾） | 多謝您嘅配合，我哋聽日準時送到，再見。 |

入站语料（`reports/asr-whisper-bench/corpus-v2/`，两轮同稿，探针真 WebRTC 进房推 16k PCM）：

| 用户轮 | 文件 | 语料文本 | 剧本角色 |
|---|---|---|---|
| 1 | `canto_12.wav` | 你哋幾時可以先送到我度 | 步1答 |
| 2 | `brand_canto_02.wav` | 我件貨係京東買的 | 步2答 |
| 3 | `digit_canto_04.wav` | 我WhatsApp號碼係六四三二一一零九 | 步3喂号（期望捕获 64321109） |
| 4 | `canto_11.wav` | 我件貨爛咗想投訴 | 步4答 |

### 1.2 两条引擎的装配（生产姿态，均未改代码）

| | 级联 | S2S |
|---|---|---|
| 入口 | CP `POST /api/templates`（id `08ade4374c40`，名「快遞通知四步-S2S對照-粵」，cantonese）→ 对象 `demo-陳大文-<ts>`（contact_channel=WhatsApp）→ `POST /api/calls`（live）→ `/api/token` 进房 | roomConfig 直派 `agent_name=bok-s2s-flow`（`apps/agent/agent_runtime/providers/s2s_flow_worker.py`，步序钉死 worker） |
| ASR | **豆包云**（CP settings `asr.provider=doubao`；agent.log `DOUBAO_ASR_TEXT ... ASR_MS=201/187/294/269(cloud)`） | **豆包云**（s2s serve `--stt doubao`，`DoubaoSaucSTTHandler`） |
| LLM | **DeepSeek**（model_routing `a_reply`=openai/deepseek-flash，前缀缓存实测 cached 2816-2944/2981-3293） | **DeepSeek**（serve `--llm-backend chat-completions --model_name deepseek-flash`） |
| TTS | **MiniMax 云**（persona voice = `Cantonese_crisp_news_anchor_vv2`，与 S2S 同声） | **MiniMax 云**（serve `--tts minimax`，bidi handler，voice/model 同 `Cantonese_crisp_news_anchor_vv2` / `speech-2.8-hd`） |
| 声音/home | 级联 worker :8081（常驻栈，未动） | serve :8795（本次起）+ worker :8086（本次起，跑完已 pkill） |

级联模板 `steps_json` 两个姿态：
- **say 版（主判据，canon）= 步2/3/4 `say:true`**（正稿罐头直念腿，合规内容的仓内生产惯例）；
- **llm 版（补充臂）= 剥 `say`**（同稿走 LLM 应答腿）——用来把「腿的选择」与
  「引擎开销」拆开。步3 带一条分支 `如果客户話件爛咗或者想投诉→【跳第4步】…`
  （理由与语法坑见 §4）。

### 1.3 量尺（两侧同一实现，探针内共用函数）

- **墙钟首声** = 探针推完用户 wav 最后一帧（真时 10ms 帧）→ agent **主音频轨**首段
  有声（16k 重采样、20ms 帧 RMS≥200、累计≥0.12s）的毫秒差。垫话音轨单列（两跑
  `bg=None`=级联垫话 0 发，headline 未被垫话污染）。
- 轮间：等 agent 说完（累计有声≥0.3s 后尾静默≥2s）+ 0.8s 定息。
- 轮账本：级联读 CP `/api/calls/{id}/turns`（`template_step`/`provider`/`gen`/文本）；
  S2S 读 worker stdout `S2S_FLOW`/`S2S_FLOW_FULL` 行（step/wa_captured/asst_text）。

---

## 2. 双跑数据表

### 2.1 主跑（canonical，每侧 1 跑）

**级联 `call-876da64e`（say 罐头直念姿态；本轮 **0 次 LLM**，全部罐头/确定性车道）**

| 轮 | 语料 | agent 回复（全文） | 车道（provider/gen） | 步 | 墙钟首声 |
|---|---|---|---|---|---|
| 开场 | — | 您好，請問係陳大文先生嗎？我係快捷快遞嘅客服，你有個包裹今日到咗我哋倉。 | opening/script（直念） | 1 | 101ms（join 后出声） |
| 1 | canto_12 | 你件嘢係京東買嘅，而家喺我哋中轉倉，聽日可以派到。 | flow-say/script（直念 step2） | 1→2 | **961ms** |
| 2 | brand_canto_02 | 麻煩您留個WhatsApp號碼，方便我哋發收貨確認俾您。 | flow-say/script（直念 step3） | 2→3 | **705ms** |
| 3 | digit_canto_04 | 好，收到，你嘅WhatsApp號碼係六四三二一一零九，啱嘅話我哋而家安排專員加你。 | wa-confirm/script（直捕罐头复述） | 3 | **812ms** |
| 4 | canto_11 | 多謝您嘅配合，我哋聽日準時送到，再見。 | flow-say+branch-jump/script | 3→4 | **813ms** |

**S2S `call-s2sflow-9c9ef4`（步序钉死；4 轮全 LLM 生成，serve llm 腿 1.05/1.65/1.39/2.41s）**

| 轮 | 语料 | worker 账本回复（asst_text 全文） | 账本 step | 墙钟首声 | serve LLM 腿 |
|---|---|---|---|---|---|
| 开场 | — | 您好，請問係陳大文先生嗎？我係快捷快遞嘅客服，你有個包裹今日到咗我哋倉。 | 1 | 1113ms（join 后出声） | 0.84s |
| 1 | canto_12 | 你件嘢係京東買嘅，而家喺我哋中轉倉，聽日可以派到。 | 2 | **2631ms** | 1.05s |
| 2 | brand_canto_02 | 送貨安排我哋稍後會通知您㗎。麻煩您留個WhatsApp號碼，方便我哋發收貨確認俾您。 | 3 | **3187ms** | 1.65s |
| 3 | digit_canto_04 | 好嘅，您嘅WhatsApp號碼係六四三二一一零九，啱唔啱呀？ | 3（wa_captured=64321109） | **2844ms** | 1.39s |
| 4 | canto_11 | 多謝您嘅配合，我哋聽日準時送到，再見 | 4 | **3912ms** | 2.41s |

### 2.2 两跑汇总（每侧 2 跑，主跑 + 复跑；判定 ①-④ 全绿）

| 引擎 | 跑次 | 轮首声 ms（4 轮） | p50 | ①步序（账本） | ②WA 捕获+复述 | ③每轮首声 | ④轮数=4 |
|---|---|---|---|---|---|---|---|
| 级联 | 跑A `call-f6a0528f` | 957 / 757 / 761 / 761 | 761 | [1,2,3,3,4] ✅ | ✅ `whatsapp_status=captured` / `customer_whatsapp=64321109` / 逐位复述 | 0.76-0.96s | ✅ |
| 级联 | 跑B `call-876da64e` | 961 / 705 / 812 / 813 | 812 | [1,2,3,3,4] ✅ | 同上 ✅ | 0.71-0.96s | ✅ |
| S2S | 跑A `call-s2sflow-6dd204` | 2780 / 2925 / 2728 / 2522 | 2780 | [1,2,3,3,4] ✅ | ✅ `wa_captured=64321109` / 逐位复述（六四三二一一零九） | 2.52-2.93s | ✅ |
| S2S | 跑B `call-s2sflow-9c9ef4` | 2631 / 3187 / 2844 / 3912 | 2844 | [1,2,3,3,4] ✅ | 同上 ✅（`fabricated=[]` 零编造号） | 2.63-3.91s | ✅ |

同轮对照（跑B）：**级联每轮快 1.7-3.1s**（0.96 vs 2.63；0.71 vs 3.19；0.81 vs 2.84；
0.81 vs 3.91）。两条引擎的步序/捕号/复述/轮数**四项判据全绿**，差异全在延迟与
「谁在生成这轮话」。

### 2.3 补充臂：级联去 `say`（纯 LLM 应答腿，`call-ac0520e3`）

| 轮 | 语料 | 车道 | 墙钟首声 | PERCEIVED（eou/llm/tts） | 步 |
|---|---|---|---|---|---|
| 1 | canto_12 | llm（DeepSeek） | 1668ms | 1162（436/515/211） | 1→2 |
| 2 | brand_canto_02 | llm | 1717ms | 1228（518/399/311） | 2（**未推进**，见 §3.2） |
| 3 | digit_canto_04 | wa-confirm（罐头） | 756ms | —（零 LLM） | 2（捕号照常+复述） |
| 4 | canto_11 | llm | 1970ms | 1464（489/763/212） | 2（对客应答投诉） |

即：**级联即使每轮都走 LLM，轮首声 1.67-1.97s 仍快于 S2S 的 2.52-3.91s**
（级联 LLM 腿=DeepSeek TTFT 0.40-0.76s + 前缀缓存命中 95%；S2S 侧
`ChatCompletionsApiModelHandler` 的 1.05-2.41s 是**整条首句/回复生成**且无罐头腿）。

---

## 3. 判据逐条核对与机制归因

### 3.1 ①四步步序完整性：两侧 ✅

- 级联：turns 助手行 `template_step` 序列 `[1,2,3,3,4]`（开场+4 回复共 5 行），
  换步证据 `[flow] rule=auto step=2/3` + 步4 由分支跳转进入（`flow-say+branch-jump`）。
- S2S：worker 账本 `step=[1,2,3,3,4]` 单调、事件面 `step_advance 2→3→4` +
  `wa_captured@step3`。
- **两侧都需「第 3 步停一轮」**（捕号轮不推进，下一轮才走 4）——步序形态同构。

### 3.2 ②WA 捕号与复述：两侧 ✅，机制不同

- 级联：第 3 轮 **直捕罐头确认**（`wa-confirm` 车道，零 LLM/零 TTFT）：
  「好，收到，你嘅WhatsApp號碼係六四三二一一零九，啱嘅話我哋而家安排專員加你。」
  CP 账：`whatsapp_status=captured`、`customer_whatsapp=64321109`。
- S2S：worker 正则捕号（阿拉伯/全角/中文数字统一，≥8 位）→ 捕获块进
  instructions → 「六四三二一一零九」逐位复述；`fabricated=[]`（零编造号）。
- **注意一个级联账本缺行**：WA 直捕轮的 **user 转写行缺失**（turns 里 4 轮用户话
  只有 3 行；级联 2 跑都如此）。捕号/复述本身不受影响（探针按助手行判），但
  「用户说了什么」在这一轮账上不可追。

### 3.3 ③每轮墙钟首声：级联 0.71-0.96s，S2S 2.52-3.91s

**S2S 的 2.5-3.9s 有可分解的机制账**（跑B serve 日志时间线）：
`停嘴 →(VAD 软结束)→ ASR 定稿 0.34-0.39s → LLM 整条生成 1.05-2.41s → 首包/传输 ~0.3s`。
- LLM 腿（DeepSeek，served 云）是最大单项且**逐轮变长**（1.05→1.65→1.39→2.41s，
  与回复长度正相关：90→162→197→403 output tokens）；
- 首包腿 ~0.2-0.4s（MiniMax 云地板 240ms 量级）；
- 残余是 VAD 收口窗与 karaoke 传输。**S2S 没有罐头腿**：开场/四步正稿/复述确认
  全部经 LLM 生成（口径上「每轮一次云 LLM」）。

**级联的 0.71-0.96s** 是「罐头皮」。级联并非天生快——补充臂的 LLM 轮 1.67-1.97s
（PERCEIVED 1.16-1.46s）显示：级联的 LLM 轮也只要 1.2-1.5s 感知延迟（前缀缓存 +
首段早发/头段催产 + MiniMax ttfb 0.21-0.31s），**仍比 S2S 的同轮快 1-2s**。

### 3.4 ④轮总数：两侧 ✅ 4 用户轮（+1 开场）

级联 4/4 有答；S2S 4/4 有答（账本 5 行含开场）。探针推流真时、帧长一致。

---

## 4. 本轮暴露的仓库级硬细节（两个，都是工兵级但真）

### 4.1 分支行只认**简体**行头「如果客户」，繁体整行被静默丢弃

`apps/agent/agent_runtime/flow.py:107-111` 的 `_BRANCH_LINE_RE` 行头字面量=
`如果客户|If the customer|When the customer`，注意行=`注意|Note`。实测：

```
[flow] ref_directive_unparsed line='如果客戶話件爛咗或者想投诉→【跳第4步】…'
       —— 行内含「→」但行头不被识别 … 该行已被忽略
```

- 影响面：**粤语（繁体）模板写「如果客戶…」的分支行全部是死行**——仓内种子
  `scripts/seed_invite_templates.py` 的 canto 条目（`如果客戶唔係本人→…`、
  `如果客戶唔願意留→…`）就在此列（离线实测 `parse_step_ref(...).branches == []`）。
  画布/表格导入侧如果统一落简体行头可绕开，但没有任何提示告诉运营。
- 本轮因此必须把步3 的分支写成 `如果客户話件爛咗或者想投诉→…`（行头简体、
  条件体可繁体）才生效。
- 另：家族词表 `_BRANCH_FAMILY[OBJECTION]` 是**简体**「投诉」；豆包 ASR 输出简体
  （实测 `我建议咗，想投诉。`），所以条件体写繁体「投訴」会连 bigram 兜底一起打空。

### 4.2 级联步序是**内容闸**驱动，对 ASR 退化结构性敏感（S2S 是步序钉死，免疫）

补充臂实证：轮2 用户话说「我件貨係京東買的」，豆包转写成 **「我建议京东买的。」**
（丢了「係」）→ 规则判 UNCLEAR → 后台 judge 也判 unclear(conf=0.00) → 级联**卡在
step2 不推进**（后续轮全停在 step2，靠 LLM 自由应对）。而 say 主跑因通知步
`say:true` 走「非提问即推进」的宽松语义（`should_auto_advance` 的 say_step 分支）
照常通过——**生产模板给通知步标 say 不只是音色问题，它是推进鲁棒性的一部分**；
去 say 的同稿等于是把推进押在 ASR 正确性上。S2S 側 step machine 对内容不敏感
（任何回应即推），复现 4/4 步序，代价是轮4 的投诉被机械带进告别（无对客应答）。

---

## 5. 既有事实定案（本报告引用，不重复验证）

1. **S2S 姿势矩阵**（此前验收跑，同探针口径墙钟首声）：全本地 1952ms /
   DeepSeek+本地音频 ≈1800ms / 豆包+DeepSeek+MiniMax 1807ms；CER 豆包带热词
   数字品牌全对、qwen3 本地 0.040。（本次同姿势两跑 p50 2780/2844ms——高于前
   验收单跑：宿主共享机负载可见（load avg ~11.9、另一 checkout 的 s2s 实例同机
   在跑），云 LLM 腿 1.05-2.41s 波动是主要变量；量级不改变结论。）
2. **意图层溶解结论**：S2S 形态下独立意图判定层退化为 worker 侧确定性流控
   （`FlowMachine.advance_after_assistant` + 正则捕号 + instructions 钉步），
   **零额外判定延迟**——本轮 S2S 全程没有 judge 类调用；对照级联每轮还有
   规则 verdict + 后台 judge/推进注入。
3. **S2S 机制验尸结论**：`session.update` instructions 在 **generation time** 被
   消费（真被吃）；「贴脸竞态」（response.create 后立刻 update 会输）由
   「预推+追捕推」双时序 + latest-wins 串行器**结构化免疫**（commit 69f9522
   worker 文档 1/2/3 条 + 本轮实弹步序无误）。
4. **MiniMax TTS handler 加固件待迁清单**（`services/s2s/.../minimax_bidi_handler.py`
   文档头 + 代码核实）：
   - 已随迁：①60s 自管 ping（`MINIMAX_BIDI_PING_S/_MAX_MISS`）；②连接/握手失败
     静默重试 1 次（`MINIMAX_BIDI_CONNECT_RETRY`）；③RPM 限流熔断（1002/1039，
     ≥2 轮，`MINIMAX_TTS_BIDI_GUARD`）；④陈旧音频纪元门禁（`MINIMAX_BIDI_DROP_STALE`）。
   - **未随迁**：⑤合成级预热（宿主无装配期钩子；生产实测 `first_continue_to_audio_ms
     257-655ms` 复用整连）；⑥classic 回退车道+连接池（宿主无 classic 线）；⑦**首段
     早发 `BOK_TTS_FIRST_CHUNK_CHARS`（≥10 字即放、数字/拉丁不劈）**；⑧**头段
     `task_flush` 催产 `MINIMAX_BIDI_HEAD_FLUSH`（第九波：云首声 637-1017→233-454ms）**
     ——现 handler 只在 EndOfResponse 发 flush（`minimax_bidi_handler.py:567-570`）。
   - 文本侧加固（生产插件有、S2S 宿主无）：数字逐位槽位化、发音词典、voice style
     标记剥离/换气注入、复读/编造号码出口守卫、复述环——这些是 agent 层件，S2S
     宿主结构性缺位。
5. **级联侧已知争用病灶**（提速主刀）：云档下 6 个无人调用的本地模型服务驻
   GPU（:1235/:1236/:1237/:1239/:8789/:8791 常驻；-876da64e 这跑 a_reply 全走云
   DeepSeek，本地 LLM 无请求），ASR sidecar 无条件拉起；治争用=级联侧下一刀
   （本机实测本跑期间 load avg 曾 11.9、内存压缩器 21G）。

---

## 6. 定位建议（按本轮数据）

1. **粤语 A 线：继续级联，S2S 不接生产**。同稿同姿态级联全面领先：脚本轮
   0.71-0.96s vs S2S 2.52-3.91s；即使级联走纯 LLM 腿（1.67-1.97s）也快于 S2S
   同轮 1-2s；级联的四项判据全绿且账本/审计/结算/UI 面完整，S2S 侧现在只有
   worker 账本。S2S 质检结论与 S2S_ROADMAP「S2S 收益本质=砍级联三段排队」在
   本栈**不成立**——因为级联的快腿（罐头/前缀缓存/早发）把三段排队已经砍掉，
   而 S2S 把「排队的可预测性」换成了「每轮 LLM 必须跑完首句 + 服务端攒句」。
2. **S2S 的真实增量在形态不在延迟**：①意图/推进判定层溶解——本轮复述、步序、
   捕号全对且零判定调用，模板侧**零分支工程**（不需要写 `如果客户…` 分支）；
   ②对「内容驱动的鲁棒性」是反命题：S2S 对 ASR 退化免疫（级联在 4.2 卡步），
   但代价是钝（轮4 投诉被机械带进告别，级联给出「唔好意思…認真跟進」的对客
   应答）。若产品接受「合规脚本按步念、不做语境适应」，S2S 形态是干净的。
3. **若继续 S2S 试点，按 ROI 排**：①迁待迁清单 ⑦⑧（首段早发+头段催产）——
   这是把 S2S 首声压进 2s 内的必要条件（按第九波实证：云首声 637→233ms 档）；
   ②给 worker 加**罐头/直念腿**（合规句不烧 LLM；预计把脚本轮拉到级联同档，
   并让复述确认也免 DeepSeek）；③迁 ⑤⑥（预热/连接池/回退）做可用性；
   ④产品面（QA/热词/结算/账本落库/意图图）当前全无，是真正的工程量。
4. **级联侧本轮顺手暴露两个可修小件**（都不大、都有测试位）：①WA 直捕轮 user
   转写行缺账（4.2 关联的是一致性缺口）；②分支行头简繁字面坑——建议
   `_BRANCH_LINE_RE` 收 `如果客戶/如果客户` 两种行头（同步 `lib/flow-canvas.ts`
   与 `_validate_steps_branch_text` 的镜像），或在保存期给繁体分支行一条明确
   告警（现状是运行期一次性 warning，运营看不见）。

---

## 7. 复现与工件

```bash
# 级联栈常驻（勿动）；S2S 侧前置（本报告跑法）：
#   serve :8795 = /tmp/s2s-venv/bin/speech-to-speech serve --host 127.0.0.1 --port 8795 \
#     --stt doubao --llm-backend chat-completions --model_name deepseek-flash \
#     --responses_api_base_url https://api.deepseek.com/v1 --responses_api_api_key <k> \
#     --tts minimax --minimax_tts_region cn --detect_llm_output_language \
#     --stream_batch_sentences 1 --no_smart_turn --speculative_reopen_ms 400 --num_pipelines 1
#   worker :8086 = .venv312/bin/python -m agent_runtime.providers.s2s_flow_worker start
#     (env: LIVEKIT_URL/API_KEY/API_SECRET, PYTHONPATH=apps/agent；stdout→/tmp/s2s_flow_worker.log)

# 双跑（模板/对象/通话都是探针经 API 自建，幂等）
.venv312/bin/python scripts/probe_s2s_vs_cascade.py --engine both --runs 1 \
    --out /tmp/s2s_vs_cascade_results.json
# 补充臂（级联去 say；跑完自动把模板还原成 say 版 canon）
.venv312/bin/python scripts/probe_s2s_vs_cascade.py --engine cascade \
    --cascade-steps llm --out /tmp/s2s_vs_cascade_results_llmarm.json
```

| 工件 | 位置 |
|---|---|
| 探针（两侧驱动+同一量尺+判定+JSON） | `scripts/probe_s2s_vs_cascade.py` |
| 模板 | CP 模板 id `08ade4374c40`「快遞通知四步-S2S對照-粵」（cantonese，say 版 canon） |
| 对象（每跑一个） | `demo-陳大文-23748` / `demo-陳大文-34122` / `demo-陳大文-43952` …（demo- 测试族） |
| 数据 JSON | `/tmp/s2s_vs_cascade_results.json`（双跑）+ `/tmp/s2s_vs_cascade_results_llmarm.json`（补充臂） |
| 级联账本 | `/api/calls/{call-876da64e,call-f6a0528f,call-ac0520e3}/turns` + `~/Library/Application Support/BokVoice/logs/agent.log` |
| S2S 账本 | worker stdout `/tmp/s2s_flow_worker.log`（本跑两侧已加长）；serve 日志 `/tmp/s2s_vc_serve.log` |

## 8. 约束遵守与遗留

- 未改任何现有源码；未 commit；只新增探针与本报两份文件 + CP API 数据。
- 本次起的进程已清理：:8795 serve（PID 49040）与 :8086 worker（PID 51378 及其
  子进程）已 pkill，端口无残留；级联常驻栈（:7880/:8001... :8081 worker）未动。
- 遗留观察位：①级联 WA 直捕轮 user 行缺账；②分支行头简繁坑（§4.1）；③本机共享
  负载导致 dispatch 偶发晚到（首跑 25s 未见 agent，探针已加 75s 等待+重试）；
  ④S2S `--min_silence_ms`/VAD 档未做 A/B（本轮沿用服务默认），若继续 S2S 试点，
  这一项与「首段早发」应同窗一起调。
