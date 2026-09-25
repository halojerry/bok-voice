# 生产就绪全栈验收测试计划 v2（Production Readiness Test Plan）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**v2 变更（对标大厂发布门禁后的补齐）：** v1 只有功能/压测/RBAC 六维；v2 按业界标准（Google SRE Ch.27 launch checklist + 阿里/美团全链路压测·混沌·灰度实践）补六块：**SLO 基线表（T2）、AI 输出质量评测批（T4）、长稳 soak（T8）、混沌工程矩阵（T9）、数据完整性与灾备演练（T10）、发布工程与灰度方案（T11/T14）**；静态门禁加依赖 CVE+密钥扫描（T1）；安全加 fuzz/限流（T12）；新增观测就绪审计（T13）。

**Goal:** 对当前工作树做一次生产级全栈验收，产出十维记分卡（含 SLO 达标表、容量结论、故障自愈证据、回退矩阵、灰度方案）与「可否上生产」判定。

**Architecture:** 十五个 Task 串行：前置 → 静态门禁 → SLO 立尺 → A 线功能 → AI 质量批 → B 线 → WebUI → 容量 → 长稳 → 混沌 → 灾备 → 发布工程 → 安全深化 → 观测审计 → 判定。全部真栈真语音真浏览器，证据落 `reports/prod-readiness/`。

**Tech Stack:** pytest（tests/ 203 文件）/ node --test / 仓内 E2E·probe 脚针 / browser-use / curl+fuzz / gitleaks+pip-audit+npm audit / sqlite3 演练。

## Global Constraints（全程约束，违反=测试无效）

- **Never fake-green**：A 线 E2E 必须走真实 `/api/token` 建单；禁用 `E2E_SELF_TOKEN=1` 凑绿。
- **防殭尸 worker**：每次换栈/重启前后 `ps aux | grep -E "agent_runtime|control_plane" | grep -v grep` 必须归零。
- **auth-on 标准姿势**（全程 auth-on，测的就是生产形态）：
  ```bash
  BOK_AUTH_REQUIRED=1 BOK_JWT_SECRET="$(cat ~/.bok_dev_jwt_secret)" \
  BOK_CP_TOKEN="$(cat ~/.bok_dev_cp_token)" \
  BOK_ROOT_USERNAME=root BOK_ROOT_PASSWORD="Root-Bok2026!" \
  python tools/bok.py serve
  ```
  doctor 同 env 跑。
- **E2E 话音句铁律**：测试句不带逗号/大换气（>0.45s 停顿劈轮）；<10 字单口气句结构性免疫。
- **测试对象=当前工作树**（用户已拍板 2026-09-23：5 个未提交改动文件纳入验收范围）。
- **用户授权记录（2026-09-23）**：在跑 runtime 栈可 `down`；本地构建产物可清理并**从源码重建**（Step 0.2a-0.2e）；模型不重下载——本地模型用 ~/.lmstudio 软链进 app-data/models（doctor 双布局核验），TTS 云端走 MiniMax 凭据。
- **执行方式（用户拍板）**：Subagent-Driven，A/B 线多并发腿重点由 subagent 执行。
- **共享栈协议**：runtime 栈已授权 down（2026-09-23）；down 前仍须确认无在途通话；测完是否恢复 runtime 栈听用户指示。
- **环境保真度声明（写入 VERDICT）**：本计划测的是本机回环拓扑；生产真实 SIP 中继/公网抖动/终端设备差异不在本计划覆盖内 → **最终上线 gate 必须包含灰度试点期真实网络验证（T14）**，本计划给出的是「可进入灰度」的判定。
- **证据落盘**：每 Task 输出写 `reports/prod-readiness/<task>-*.{log,csv,md,png}`。
- **GPU 预算**：全量 6-10 小时 GPU 时间（长稳 2h+混沌 1h+评测批 2h+功能腿 3h+），分批执行；每批之间确认栈健康再继续。

---

### Task 0: 前置与栈主权（Preflight）

**Files:** 产出 `reports/prod-readiness/env.sh`（后续所有 Task source 的环境变量）、`00-preflight.log`

**Interfaces:**
- Produces: dev 树 auth-on 全栈、`DB`/`CP` 变量、QA 罐头预物化、真库副本（T10 灾备用）。

- [ ] **Step 0.1: 固化工作树状态并确认范围**

```bash
cd /Users/halo/Documents/bok/voice-assistant
mkdir -p reports/prod-readiness
git status --short | tee reports/prod-readiness/00-git-status.txt
git diff --stat
```

**已拍板（2026-09-23）**：未提交改动（agent.py/measure_prompt.py/sidecar app.py/bok.py/next-env.d.ts）**纳入验收范围**。

- [ ] **Step 0.2: 与在跑 runtime 栈交接**

```bash
curl -s -H "Authorization: Bearer $(cat ~/.bok_dev_cp_token)" http://127.0.0.1:8000/api/calls | python3 -c "import sys,json; rows=json.load(sys.stdin); items=rows if isinstance(rows,list) else rows.get('items',[]); print('active:', sum(1 for r in items if r.get('status')=='ACTIVE'))"
python tools/bok.py status
python tools/bok.py down
ps aux | grep -E "agent_runtime|control_plane" | grep -v grep   # 必须为空，有残留记录 PID 手动 kill
```

- [ ] **Step 0.2a: 清理前先备份 DB（清理动作的前置保险）**

```bash
DB=$(.venv312/bin/python -c "import sys; sys.path.insert(0,'tools'); import bok; print(bok.app_data_dir() / 'bok_voice.db')")
sqlite3 "$DB" ".backup /tmp/bok-pre-clean-backup.db" && echo "BACKUP_OK -> /tmp/bok-pre-clean-backup.db"
```

- [ ] **Step 0.2b: 清理构建产物（用户授权；清单封闭，禁用 git clean）**

```bash
rm -rf .venv312 apps/web/node_modules apps/web/.next apps/web/out services/realtime-translation/node_modules
find apps packages services tools scripts -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null; echo CLEAN_OK
```

**红线**：不动 `app-data/`（DB/vault/日志/tts cache/models 软链目标）、不动 `runtime/`（T11 升级叙事的旧版打包树）、不动 `work-session-*/`、`bok-architecture.*`、`.superpowers/`、`reports/` 等未跟踪内容。

- [ ] **Step 0.2c: 从源码重建 Python 环境**

```bash
./scripts/bootstrap.sh 2>&1 | tee reports/prod-readiness/00-bootstrap.log | tail -8
.venv312/bin/python --version && .venv312/bin/pip list 2>/dev/null | wc -l
```

- [ ] **Step 0.2d: 从源码重建 Node 面**

```bash
cd services/realtime-translation && npm ci 2>&1 | tail -3 && cd ../..
cd apps/web && npm ci 2>&1 | tail -3 && npm run build 2>&1 | tee ../../reports/prod-readiness/00-webbuild.log | tail -6 && cd ../..
```

- [ ] **Step 0.2e: 模型核对（不重下载；lmstudio 软链策略）**

```bash
find ~/.lmstudio/models -maxdepth 2 -mindepth 2 -type d 2>/dev/null | head -20   # 盘点本地已有模型
python3 - <<'EOF'
import sys; sys.path.insert(0, 'tools'); import bok
for k, v in getattr(bok, 'MODELS', {}).items(): print(k, '->', v)
EOF
```

`doctor` 报 MISSING 的本地模型，按 `MODELS` 声明的 `owner--name` 软链：`ln -s ~/.lmstudio/models/<owner>/<name> "<app-data>/models/<owner--name>"`（`_model_present` 双布局核验）；MiniMax 为云 API 无本地件。

- [ ] **Step 0.3: doctor + 起栈**

```bash
BOK_AUTH_REQUIRED=1 BOK_JWT_SECRET="$(cat ~/.bok_dev_jwt_secret)" \
BOK_CP_TOKEN="$(cat ~/.bok_dev_cp_token)" \
BOK_ROOT_USERNAME=root BOK_ROOT_PASSWORD="Root-Bok2026!" \
python tools/bok.py doctor --packaged 2>&1 | tee reports/prod-readiness/00-doctor.log
# 然后同 env `python tools/bok.py serve`（后台/独立终端），再：
python tools/bok.py status 2>&1 | tee reports/prod-readiness/00-status.log
for p in 8000 7880 8787 8788 8081 8082 8083 8790; do printf "%s: " $p; nc -z -w1 127.0.0.1 $p && echo UP || echo DOWN; done | tee -a reports/prod-readiness/00-status.log
curl -s http://127.0.0.1:8081/worker | head -c 200; echo
```

- [ ] **Step 0.4: 解析路径并写 env.sh**

```bash
DB=$(.venv312/bin/python -c "import sys; sys.path.insert(0,'tools'); import bok; print(bok.app_data_dir() / 'bok_voice.db')")
cat > reports/prod-readiness/env.sh <<EOF
export DB="$DB"
export CP=http://127.0.0.1:8000
export MACHINE_AUTH="Authorization: Bearer \$(cat ~/.bok_dev_cp_token)"
EOF
cat reports/prod-readiness/env.sh
```

- [ ] **Step 0.5: 罐头预物化 + 真库备份（T10 灾备素材）**

```bash
python tools/bok.py tts-pregen --qa 2>&1 | tail -5          # 退出码 3=人设缺账号，先建人设再重跑
sqlite3 "$DB" ".backup reports/prod-readiness/10-restore-source.db" && echo BACKUP_OK
```

---

### Task 1: 静态门禁 + 依赖与密钥安全

**Files:** 产出 `reports/prod-readiness/01-static.*`

- [ ] **Step 1.1: pytest 全量**

```bash
./scripts/test.sh 2>&1 | tee reports/prod-readiness/01-pytest.log | tail -20
```

- [ ] **Step 1.2: Node + Web + 编译面**

```bash
cd services/realtime-translation && npm ci && npm test 2>&1 | tee ../../reports/prod-readiness/01-node.log | tail -10
cd ../../apps/web && npx tsc --noEmit 2>&1 | tee ../../reports/prod-readiness/01-tsc.log && npm run build 2>&1 | tee ../../reports/prod-readiness/01-webbuild.log | tail -15
cd .. && .. && python -m compileall -q apps packages services tools scripts && echo COMPILE_OK
```

- [ ] **Step 1.3: 依赖 CVE 扫描**

```bash
.venv312/bin/pip install -q pip-audit
.venv312/bin/pip-audit 2>&1 | tee reports/prod-readiness/01-pip-audit.log | tail -15
cd apps/web && npm audit --omit=dev 2>&1 | tee ../../reports/prod-readiness/01-npm-audit-web.log | tail -10
cd ../../services/realtime-translation && npm audit --omit=dev 2>&1 | tee ../../reports/prod-readiness/01-npm-audit-rt.log | tail -10; cd ../..
```

预期：记录全部已知漏洞；无 fix 的记入 Minor/观察项（本地优先架构下多数 CVE 不在攻击面，逐条判读）。

- [ ] **Step 1.4: 密钥扫描**

```bash
gitleaks detect --source . --no-git -v 2>&1 | tee reports/prod-readiness/01-gitleaks.log | tail -20 || true
```

预期：零真实密钥（测试文件里的 fake token 记录豁免理由）。

---

### Task 2: SLO 基线表（先立尺，后续全按它判）

**Files:** 产出 `reports/prod-readiness/02-slo.md`

**Interfaces:**
- Produces: SLO 表（T3-T9 的判定标尺）。

- [ ] **Step 2.1: 写定 SLO 表（数值来自仓内既有实测锚点，禁止现场放宽）**

| # | SLI | SLO | 数据来源 |
|---|-----|-----|---------|
| 1 | A 线开场白首声 | p95 ≤3.0s | e2e 日志 greeting_playout |
| 2 | A 线轮首声（感知延迟） | p95 ≤2.5s | turns.perceived_ms |
| 3 | 打断停声 | p95 ≤3.0s（基线 2.4s） | e2e_barge_in |
| 4 | 哑轮率（正常轮零回复） | <1% | soak/load turns 统计 |
| 5 | 数字捕获准确率（报号轮） | 100%（零容错铁律） | WER 批报号句 |
| 6 | 整句复读 | 0 | 质量批+offscript 旗 |
| 7 | B 线感知 lag | p95 ≤3.5s | probe_interpret_latency |
| 8 | B 线丢句 | 0 | e2e_interpret/duplex |
| 9 | 故障自愈时间 | ≤60s（monitor 拉起） | 混沌 T9 |
| 10 | 长稳资源增幅 | 2h RSS 增幅 <10%、零 worker 重启 | T8 遥测 |

- [ ] **Step 2.2: 当前库基线读数（测前快照）**

```bash
source reports/prod-readiness/env.sh
.venv312/bin/python - <<'EOF' 2>&1 | tee reports/prod-readiness/02-baseline-turns.txt
import os, sqlite3
db = sqlite3.connect(os.environ["DB"])
rows = sorted(r[0] for r in db.execute(
    "SELECT perceived_ms FROM turns WHERE line='a' AND speaker='agent_ai' AND perceived_ms>0"))
if rows:
    print("n=", len(rows), "p50=", rows[len(rows)//2], "p95=", rows[int(len(rows)*.95)], "max=", rows[-1])
else:
    print("no perceived_ms rows yet")
EOF
```

---

### Task 3: A 线真栈功能（多轮话术·三语·打断·分支·图·快答）

**Files:** 产出 `reports/prod-readiness/03-a-line/*.log`

判据基线：三语 E2E 自判 PASS；barge-in `interrupted=yes` `stop_ms≈2.4s` `resumed=yes`；快语速两判据过；垫话首声 <2.5s；LLM 缓存 cached 逐轮增长、TTFT 压平。

- [ ] **Step 3.1: 三语 E2E（三跑）**

```bash
mkdir -p reports/prod-readiness/03-a-line
for L in cantonese zh en; do
  E2E_ONLY=$L .venv312/bin/python scripts/e2e_trilingual_livekit.py 2>&1 | tee reports/prod-readiness/03-a-line/e2e-$L.log | tail -5
done
```

- [ ] **Step 3.2: 多轮话术对话（不同模板轮换，用户核心诉求）**

```bash
.venv312/bin/python scripts/e2e_multi_turn.py 2>&1 | tee reports/prod-readiness/03-a-line/multi-turn.log | tail -8
.venv312/bin/python scripts/e2e_flow_scenario.py 2>&1 | tee reports/prod-readiness/03-a-line/flow-scenario.log | tail -8
```

人工判读：抽 turns 表逐轮对照步推进/直念步/say 锁（多轮真实对话的核心证据，不是脚本自判就算数）。

- [ ] **Step 3.3: 打断回归（用户核心诉求）**

```bash
.venv312/bin/python scripts/e2e_barge_in.py 2>&1 | tee reports/prod-readiness/03-a-line/barge-in.log | tail -5
```

- [ ] **Step 3.4: 边角+快语速**

```bash
.venv312/bin/python scripts/e2e_edge_cases.py 2>&1 | tee reports/prod-readiness/03-a-line/edge.log | tail -5
.venv312/bin/python scripts/probe_fast_speech.py 2>&1 | tee reports/prod-readiness/03-a-line/fast-speech.log | tail -5
```

- [ ] **Step 3.5: 分支六腿+图三腿（先 `--help` 核对腿名参数）**

```bash
.venv312/bin/python scripts/probe_branch_action.py --selftest
for LEG in canned refuse handoff jump hold; do
  .venv312/bin/python scripts/probe_branch_action.py --leg $LEG 2>&1 | tee reports/prod-readiness/03-a-line/branch-$LEG.log | tail -3
done
.venv312/bin/python scripts/probe_flow_graph.py --then-jump   2>&1 | tee reports/prod-readiness/03-a-line/graph-then-jump.log  | tail -3
.venv312/bin/python scripts/probe_flow_graph.py --intent-judge 2>&1 | tee reports/prod-readiness/03-a-line/graph-intent-judge.log | tail -3
.venv312/bin/python scripts/probe_flow_graph.py --jump-speech  2>&1 | tee reports/prod-readiness/03-a-line/graph-jump-speech.log  | tail -3
```

`play_miss` 出现=先 `tts-pregen --qa` 再重跑该腿。

- [ ] **Step 3.6: kill-switch 合并重启窗（一次重启跑全部 kill 腿）**

停栈 → 以 `BOK_FLOW_GRAPH=0 BOK_BRANCH_ACTION=0` 前缀重起 serve →：

```bash
.venv312/bin/python scripts/probe_branch_action.py --leg kill --expect-off 2>&1 | tee reports/prod-readiness/03-a-line/branch-kill.log | tail -3
.venv312/bin/python scripts/probe_flow_graph.py --then-jump --expect-off 2>&1 | tee reports/prod-readiness/03-a-line/graph-kill.log | tail -3
.venv312/bin/python scripts/probe_killswitch.py --help   # 有真栈模式则照跑
```

恢复正常 env 重启栈，ps 确认零殭尸。

- [ ] **Step 3.7: QA 快答优先级/轮换**

```bash
.venv312/bin/python scripts/probe_qa_hit.py --priority-duel 2>&1 | tail -4
.venv312/bin/python scripts/probe_qa_hit.py --rotation-duel  2>&1 | tail -4
```

- [ ] **Step 3.8: offscript 风暴+回复质量+战役链路**

```bash
.venv312/bin/python scripts/probe_offscript_soak.py 2>&1 | tee reports/prod-readiness/03-a-line/offscript.log | tail -10
.venv312/bin/python scripts/probe_reply_quality.py 2>&1 | tee reports/prod-readiness/03-a-line/reply-quality.log | tail -5
.venv312/bin/python scripts/e2e_campaign.py 2>&1 | tee reports/prod-readiness/03-a-line/campaign.log | tail -5
.venv312/bin/python scripts/probe_campaign_schedule.py 2>&1 | tail -5
```

- [ ] **Step 3.9: 垫话+LLM 缓存健康**

```bash
FILLER_REQUIRE=1 .venv312/bin/python scripts/probe_filler_timing.py 2>&1 | tee reports/prod-readiness/03-a-line/filler.log | tail -5
.venv312/bin/python scripts/probe_llm_cache.py 2>&1 | tee reports/prod-readiness/03-a-line/llm-cache.log | tail -5
```

---

### Task 4: A 线 AI 输出质量评测批（大厂 gate 的「业务质量」维度）

**Files:** 产出 `reports/prod-readiness/04-quality/scoresheet.csv`、`04-wer.md`

**Interfaces:**
- Consumes: Task 0 的栈与罐头。
- Produces: 24 通对话评分 + 三语 WER 批 + 胡编哨兵结果（SLO #5/#6 证据）。

- [ ] **Step 4.1: 24 通对话评测集（3 语 × 8 场景）**

场景配比/通：正向跟进×2、异议处理×2、明确拒绝×1、offscript（防诈质疑/转人工/推搪）×2、WhatsApp 报号×1。话术模板轮换三套（6 步三语模板 / 带 graph / 带 branch）。执行复用既有腿：

```bash
# 每通走 e2e_multi_turn / e2e_flow_scenario / probe_offscript_soak 的主题参数（--help 核对），逐通落 transcript
# 产物：turns 导出 + 逐通 transcript 存 reports/prod-readiness/04-quality/calls/
.venv312/bin/python scripts/e2e_multi_turn.py --help
```

- [ ] **Step 4.2: 逐通评分 rubric（8 项 ×0-2 分，人工+LLM 辅助双评）**

评分项：①步推进正确性 ②合规内容念达（say 直念步）③赔偿数字纪律（非赔偿轮零报档位）④零整句复读 ⑤语言纯度（zh 不夹粤/粤不夹普）⑥身份与质疑应答（不反复追问平台）⑦WhatsApp 号码捕获与复述正确 ⑧收线礼貌。`scoresheet.csv` 列：`call_id,lang,scenario,item1..item8,total,notes`。**门槛：数字类（③⑦）任一通 0 分=Blocker；其余均分 ≥1.5。**

- [ ] **Step 4.3: WER 批（每语 30 句，数字句零容错）**

复用仓内「TTS 渲染已知文本→推流→ASR 回读比对」模式（参照 `probe_cantonese_digits.py`/`probe_asr_digits_ab.py`/`probe_fast_speech.py` 拼装驱动）：

- 句集：数字串句 10（单号/WhatsApp 号/金额）+ 领域词句 10（品牌/平台词）+ 一般句 10。
- 判据：**数字句 100% 逐字对（SLO #5 零容错）**；一般句记录 CER 参考值与错例清单（ASR 升级依据）。

- [ ] **Step 4.4: 胡编哨兵（4B 编造号码/热线已知风险）**

```bash
source reports/prod-readiness/env.sh
.venv312/bin/python - <<'EOF' 2>&1 | tee reports/prod-readiness/04-quality/hallucination-scan.txt
import os, re, sqlite3
db = sqlite3.connect(os.environ["DB"])
pat = re.compile(r"(85[0-9]{6,}|[0-9]{3,4}-[0-9]{3,4}-[0-9]{4}|\b[0-9]{8,}\b)")
rows = list(db.execute("SELECT call_id, text FROM turns WHERE speaker='agent_ai' AND created_at > datetime('now','-2 days')"))
whitelist = []  # 从模板/QA 词条导出的合法号码白名单，执行时补全
hits = [(c, t) for c, t in rows if pat.search(t or "") and t not in whitelist]
print("agent_ai rows:", len(rows), "hallucination-pattern hits:", len(hits))
for c, t in hits[:20]: print(c, t[:80])
EOF
```

判据：白名单外命中=Major（记录每例；真号码必须走 QA 词条而非模型生成）。

---

### Task 5: B 线真栈功能（同传·延迟·连续·积压·全双工）

**Files:** 产出 `reports/prod-readiness/05-b-line/*.log`

- [ ] **Step 5.1: 同传 E2E（5 腿）**

```bash
mkdir -p reports/prod-readiness/05-b-line
.venv312/bin/python scripts/e2e_interpret.py 2>&1 | tee reports/prod-readiness/05-b-line/e2e-interpret.log | tail -8
```

- [ ] **Step 5.2: 延迟（SLO #7/#8 证据）**

```bash
.venv312/bin/python scripts/probe_interpret_latency.py 2>&1 | tee reports/prod-readiness/05-b-line/latency.log | tail -6
```

- [ ] **Step 5.3: 连续语流+积压+全双工**

```bash
.venv312/bin/python scripts/probe_interp_continuous.py 2>&1 | tee reports/prod-readiness/05-b-line/continuous.log | tail -6
.venv312/bin/python scripts/probe_interp_backlog.py    2>&1 | tee reports/prod-readiness/05-b-line/backlog.log    | tail -6
.venv312/bin/python scripts/probe_interp_duplex.py     2>&1 | tee reports/prod-readiness/05-b-line/duplex.log     | tail -6
```

---

### Task 6: WebUI 全功能 UI/UX + 前后端连通性（真浏览器）

**Files:** 产出 `reports/prod-readiness/06-webui/*`（走查记录+截图+console 清单）

**执行方式：browser-use 插件（`browser-use:web-gui-tester` skill）。**

- [ ] **Step 6.1: 构建产物伺服（注入本栈 cpUrl）**

```bash
cd apps/web
cat > out/runtime-config.js <<EOF
window.__BOK_CONFIG__ = {"cpUrl": "http://127.0.0.1:8000", "livekitUrl": "ws://127.0.0.1:7880"};
EOF
npx serve out -l 3010 &
curl -s http://127.0.0.1:3010/ | head -c 100
```

- [ ] **Step 6.2: 三角色登录分面**

root/admin/user 各登录，断言导航形态（root=纯平台面含 /nodes；admin=运营页+按下发键的管理页；user=工作台面；匿名=本地模式）。

- [ ] **Step 6.3: 19 页面逐页走查（每页两件事：零 console error + 核心 CRUD 真打 CP）**

| 页面 | 核心动作 |
|---|---|
| `/`(stage) | dashboard 数字加载 |
| `/objects` `/roster` | 建对象→编辑→筛选 |
| `/templates` | 建模板→编辑→**发布**（published/has_changes 翻转）→画布→答法抽屉 |
| `/studio?t=` | 五 tab 全走（话术/意图问答/罐头状态/通话日志/学习报告） |
| `/qa` | 列表+画布、拖线挂簇、罐头状态、试听、补料 |
| `/calls` `/calls/new` | 建单（template_id 快照）→意向规则卡→补结算按钮 |
| `/campaigns` | 建任务（调度窗/并发/**重拨关=真传 {}**）→启动→名单状态 |
| `/interpret` | 建同传单（双向语言+音色+术语表）→同传台（字幕/浮窗/延迟面板） |
| `/supervisor` | 捕获打铃横幅/徽标、ListenPanel |
| `/users` | root 下发面板、新建 admin 管理段、改密对话框 |
| `/settings` | 引擎四卡+开发者参数、密钥掩码、sms 段保存往返 |
| `/personas` | 建人设+参考音频上传→pregen 状态提示 |
| `/knowledge` `/reports` `/audit` `/translate` | CRUD/加载 |
| `/nodes` | 仅 root 可见可开 |
| `/login` `/setup` | 401 回路 |

- [ ] **Step 6.4: 连通性专项**

401→清 token 跳登录；user 访问管理面=403 呈现非白屏；QA 试听/音频上传/previewVoice 三条音频链路；两处历史裸 fetch 点（canned-audition/interpret-console）重点看带 auth。

- [ ] **Step 6.5: UI/UX 质量清单**

加载态/空态/错误提示人话化/布局破损/console error 全量清单 → `06-webui/console-errors.md`。

---

### Task 7: 容量压测 + 资源遥测（容量结论=生产容量规划输入）

**Files:** 产出 `reports/prod-readiness/07-load/*`、`telemetry/load-*.log`

- [ ] **Step 7.1: 资源遥测采样器（本 Task 与 T8 共用）**

```bash
mkdir -p reports/prod-readiness/telemetry
nohup bash -c 'while true; do echo "=== $(date +%FT%T)"; ps -eo pid,rss,%cpu,command | grep -E "agent_runtime|control_plane|qwen3|mlx|uvicorn" | grep -v grep; df -k . | tail -1; sleep 30; done' >> reports/prod-readiness/telemetry/resources.log 2>&1 &
echo $! > reports/prod-readiness/telemetry/sampler.pid
```

- [ ] **Step 7.2: CP 层并发（隔离栈）**

```bash
.venv312/bin/python scripts/load_cp_concurrency.py 2>&1 | tee reports/prod-readiness/07-load/cp.log | tail -10
```

判据：turns 30/30 零丢。

- [ ] **Step 7.3: 真通话梯度 2→4→6 路**

```bash
for N in 2 4 6; do
  LOAD_ROADS=$N LOAD_TURNS=4 .venv312/bin/python scripts/load_audio_concurrency.py 2>&1 | tee reports/prod-readiness/07-load/audio-$N.log | tail -8
done
```

产出容量结论格式：**「最大稳态路数=拐点值，安全余量=拐点−1 路」+ 每梯度 TTFT/丢轮/哑轮/RSS 曲线**（写 VERDICT 容量节）。

- [ ] **Step 7.4: 延迟 soak + A/B 混跑**

```bash
.venv312/bin/python scripts/probe_latency_soak.py 2>&1 | tee reports/prod-readiness/07-load/soak.log | tail -12
```

混跑：A 线 2 路通话进行中同时跑 `e2e_interpret.py`（B 线 1 路），记录两侧 TTFT/lag 是否预算内（真实生产形态）。判据：soak 正常轮哑 ≥2=红线 FAIL。

---

### Task 8: 长稳测试（≥2h 稳态运行，泄漏检测）

**Files:** 产出 `reports/prod-readiness/08-soak/`

- [ ] **Step 8.1: 2h 循环负载**

```bash
# 循环跑 4 路真通话直到 2h（bash 驱动 load_audio_concurrency 循环；每轮间隔 60s）
end=$((SECONDS+7200))
n=0
while [ $SECONDS -lt $end ]; do
  n=$((n+1)); echo "=== round $n $(date +%FT%T)"
  LOAD_ROADS=4 LOAD_TURNS=4 .venv312/bin/python scripts/load_audio_concurrency.py 2>&1 | tail -4
  sleep 60
done 2>&1 | tee reports/prod-readiness/08-soak/rounds.log
```

- [ ] **Step 8.2: 判据（SLO #10）**

- 零 worker 崩溃/零 monitor 误杀（agent.log 无 parent process shutdown 归因于误判）；
- RSS：首末轮对比增幅 <10%（telemetry/resources.log 首尾取同进程采样）；
- 累计哑轮率 <1%（turns 统计：正常轮中 gen 完成且零音频零转写的比例）；
- 磁盘：TTS cache/日志增速记录（容量规划输入）。

- [ ] **Step 8.3: 停采样器**

```bash
kill $(cat reports/prod-readiness/telemetry/sampler.pid)
```

---

### Task 9: 混沌工程矩阵（故障注入，验证容错/降级/自愈）

**Files:** 产出 `reports/prod-readiness/09-chaos/*.log`

**总判据（每项同构）：系统不崩、自愈 ≤60s 或有明确人工面、turns/audit 完整、发现项分级记录。**

- [ ] **Step 9.1: A 线 worker 通话中 kill -9**

```bash
# 终端A：一路通话进行中（e2e_multi_turn 或 LOAD_ROADS=1）
WORKER_PID=$(lsof -ti :8081 -sTCP:LISTEN); kill -9 $WORKER_PID; date +%FT%T.%N
# 观察：serve monitor 自动拉起耗时；该通 call_sessions 状态流转（卡 ACTIVE=发现项）；
# 已落 turns 零丢；CP 审计；新通话可正常建立
```

- [ ] **Step 9.2: TTS sidecar 通话中 kill**

```bash
kill $(lsof -ti :8788 -sTCP:LISTEN)
# 观察：该轮回复失败面（报错不炸通话？watchdog？）、恢复后后续轮正常
```

- [ ] **Step 9.3: ASR sidecar kill（同 9.2，:8787）**——用户轮丢失面+恢复。

- [ ] **Step 9.4: LLM 侧车断 30s**

```bash
LLM_PID=$(lsof -ti :1235 -sTCP:LISTEN); kill $LLM_PID
sleep 30
# 观察：2s fallback 句触发、watchdog 行为；monitor 拉起后新轮恢复正常（无殭尸旧进程）
ps aux | grep -E "mlx|1235" | grep -v grep
```

- [ ] **Step 9.5: MiniMax 云端不可达（重启窗注入）**

停栈 → `MINIMAX_BASE_URL=http://127.0.0.1:9` 前缀重起 → 跑一轮 e2e：观察本地 TTS 回退或明确失败面 + 日志/审计 → 恢复正常 env 重启。

- [ ] **Step 9.6: CP 重启（通话中）**

```bash
kill $(lsof -ti :8000 -sTCP:LISTEN); date +%FT%T.%N
# 观察：agent 上报重试面、通话续命或结束面、CP 起回后链路恢复
```

- [ ] **Step 9.7: DB 写锁 60s**

```bash
source reports/prod-readiness/env.sh
.venv312/bin/python -c "import sqlite3,time; c=sqlite3.connect('$DB',timeout=1); c.execute('BEGIN EXCLUSIVE'); time.sleep(60)" &
# 期间建单/通话：CP 5xx/排队面不崩进程，解锁后自愈
```

- [ ] **Step 9.8: GPU 争用（monitor 误杀防线验证）**

通话进行中并行跑重解码任务（如 `scripts/measure_prompt.py` 或大 prompt 直打 :1235）拉满 GPU → 观察 monitor 不误杀在途通话（`down xN (active_calls=M)` 日志形态）、TTFT 劣化曲线记录。

- [ ] **Step 9.9: B 线 worker kill（:8082 通话中，同 9.1 观察 B 线面）**

---

### Task 10: 数据完整性与灾备演练

**Files:** 产出 `reports/prod-readiness/10-recovery/*`

- [ ] **Step 10.1: 通话中 kill -9 ×5 通的账本完整性**

逐通：起通话→中途 kill worker→对账「turns 已报轮零丢、call_sessions 状态、审计行齐全」；对 ended 会话走补结算（calls 行补结算按钮或 CP settle 幂等）验证不双记。

- [ ] **Step 10.2: 备份恢复 + 迁移幂等**

```bash
source reports/prod-readiness/env.sh
cp reports/prod-readiness/10-restore-source.db /tmp/restore-drill.db
# 隔离 CP（HOME/VAULT_ROOT 指 /tmp）挂副本连起两次（验证 build_engine 迁移幂等重跑）
HOME=/tmp/bok-restore VAULT_ROOT=/tmp/bok-restore BOK_DB_PATH=/tmp/restore-drill.db \
  .venv312/bin/python -m uvicorn control_plane.main:app --port 8010 &
sleep 8; kill %1   # 再起第二次
HOME=/tmp/bok-restore VAULT_ROOT=/tmp/bok-restore BOK_DB_PATH=/tmp/restore-drill.db \
  .venv312/bin/python -m uvicorn control_plane.main:app --port 8010 &
curl -s http://127.0.0.1:8010/health
```

（`BOK_DB_PATH` 等变量名以 `control_plane/deps.py` 实际读取为准，先 grep 核对再跑。）

- [ ] **Step 10.3: schema 漂移检查**

```bash
.venv312/bin/python scripts/check_schema_drift.py 2>&1 | tee reports/prod-readiness/10-recovery/schema-drift.log | tail -10
```

- [ ] **Step 10.4: 混沌时段审计完整性抽查**

T9 全程的破坏性动作（kill/重启/锁）在 audit 表逐条可查；缺失=发现项。

---

### Task 11: 发布工程与回退（release engineering）

**Files:** 产出 `reports/prod-readiness/11-release/*`

- [ ] **Step 11.1: 版本包构建冒烟**

```bash
scripts/build_node_pkg.sh 0.0.0-rc1 2>&1 | tee reports/prod-readiness/11-release/node-pkg.log | tail -8
scripts/build_runtime_pkg.sh 0.0.0-rc1 2>&1 | tee reports/prod-readiness/11-release/runtime-pkg.log | tail -8
```

- [ ] **Step 11.2: 升级演练路径确认**

「旧栈→新代码」升级=T0 已天然演练（runtime 树旧栈→dev 树新栈），DB 迁移幂等=T10.2 已验；本步记录为 VERDICT 引用+抽 1 腿 e2e 作升级后 smoke。

- [ ] **Step 11.3: kill-switch 回退矩阵（回滚预案的核心）**

grep 全部 `BOK_*` 开关（`_FORWARD_ENV` 表+agent_runtime 读取面），产出矩阵表：`开关 → 关掉后退回什么行为 → 验证探针`。至少逐一行抽查 3 个高危开关（FLOW_GRAPH/BRANCH_ACTION/QA_FASTPATH）真关过（T3.6 已关两个，QA_FASTPATH 补一腿 `BOK_QA_FASTPATH=0` 重启窗跑 probe_qa_hit）。

- [ ] **Step 11.4: 回滚预案文档化（写进 VERDICT 附页）**

路径：git revert 代码 + DB 迁移只增列向前兼容（不回滚数据）+ kill-switch 即时行为回退（无需重启的项标注）。哪些开关需重启、哪些 env 需重注入 prod 封闭面（`_FORWARD_ENV` 表）逐条列明。

---

### Task 12: 安全深化（RBAC 矩阵 + fuzz + 限流 + 已知缓项核验）

**Files:** 产出 `reports/prod-readiness/12-security.log`

- [ ] **Step 12.1: 角色矩阵 curl 抽测**

```bash
source reports/prod-readiness/env.sh
curl -s -o /dev/null -w 'anon /health: %{http_code}\n' $CP/health                       # 200
curl -s -o /dev/null -w 'anon /api/objects: %{http_code}\n' $CP/api/objects             # 401
# user_jwt/admin_jwt 由 Task 6.2 建的账号登录获取
curl -s -H "Authorization: Bearer $USER_JWT"  -o /dev/null -w 'user objects: %{http_code}\n'  $CP/api/objects  # 200
curl -s -H "Authorization: Bearer $USER_JWT"  -o /dev/null -w 'user settings: %{http_code}\n'  $CP/api/settings # 403
curl -s -H "$MACHINE_AUTH" -o /dev/null -w 'machine settings: %{http_code}\n' $CP/api/settings                    # 200
```

补：user 改他人对象=404、user PUT 共享模板=403、admin 自提权=403、`GET /api/settings?internal=1` admin=403/root=200、机器通道铸 root=403。

- [ ] **Step 12.2: API fuzz（畸形输入不 500）**

```bash
source reports/prod-readiness/env.sh
for BODY in '{' '{"a":' '{}'.repeat(100)' "$(python3 -c 'print("{\"text\":\""+"A"*100000+"\"}")')" '{"name":"الصين 拼多多\"); DROP TABLE calls;--"}'; do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Content-Type: application/json" -H "$MACHINE_AUTH" -d "$BODY" $CP/api/calls
done | tee -a reports/prod-readiness/12-security.log
# 判据：全部 4xx，零 5xx；CP 日志 grep Traceback 零新增
```

- [ ] **Step 12.3: 限流面探测**

`/api/auth/login` 与 `/api/token` 各 100 连发（curl 循环）：记录行为（有限流=通过；无限流=发现项 Major，公网暴露前必须补）。

- [ ] **Step 12.4: 已知缓项核验（AGENTS.md 在档未修）**

webhook 无 secret fail-open、diag 路由无角色闸、`/api/token` 无记录房间可铸 publish token——逐项验证现状并写入 VERDICT 风险节（公网部署前必修清单）。

---

### Task 13: 观测就绪审计（出事了能不能发现）

**Files:** 产出 `reports/prod-readiness/13-observability.log`

- [ ] **Step 13.1: 故障可探测性**

kill TTS sidecar 30s → `python tools/bok.py status`/`doctor` 如实报 DOWN → 重启恢复（dev 面验证；launchd `prod status` DEGRADED 面记录 dev/prod 差异说明）。

- [ ] **Step 13.2: 打铃链**

DB 直改一通 `call_sessions.whatsapp_status` 模拟跳变 → `/supervisor` 页（browser-use）横幅/徽标/蜂鸣出现；`assist_status=notified` 同链合流验证。

- [ ] **Step 13.3: 日志可判读性**

抽 T9 任一混沌事件：用 agent.log+CP 日志+audit 三面拼出完整时间线（谁/何时/什么故障/如何恢复）。runbook 存在性：`docs/RUNTIME_TOPOLOGY.md`、`.agents/skills/call-diagnosis/`、AGENTS.md 逃生口清单——链接齐则通过，缺=发现项。

---

### Task 14: 汇总判定 + 灰度上线方案

**Files:**
- Create: `reports/prod-readiness/VERDICT.md`

- [ ] **Step 14.1: 十维记分卡**

| 维度 | 对应 Task | 门槛 |
|---|---|---|
| 静态+依赖+密钥 | T1 | 全绿+CVE 判读完+零真实密钥 |
| SLO 达标 | T2 表×T3-T8 证据 | 10 项 SLO 全达标或带书面豁免 |
| A 线功能 | T3 | 全腿 PASS |
| AI 输出质量 | T4 | 数字类零错、rubric 均分 ≥1.5、胡编零白名单外命中 |
| B 线功能 | T5 | 五腿 PASS |
| WebUI/连通 | T6 | 零 Blocker 级 UI 缺陷、CRUD 真连通 |
| 容量 | T7 | 拐点明确、安全余量 ≥1 路 |
| 稳定性 | T8 | 2h 零崩溃、RSS <10% |
| 容错/灾备 | T9/T10 | 自愈 ≤60s、账本零丢、恢复演练过 |
| 安全/观测/回退 | T12/T13/T11 | 零越权、故障可探测、回退矩阵齐 |

缺陷分级：**Blocker**（阻断灰度）/ **Major**（灰度前修或带预案）/ **Minor**（观察项）。

- [ ] **Step 14.2: 灰度上线方案（大厂标准：不搞 binary go/no-go）**

- **gate 判定改为「可否进入灰度」**；全量上线需灰度期指标达标。
- 灰度设计：1 个账号 × 真实外呼 20 通/日起，观察 3 项 SLO（哑轮率/数字捕获/接通 disposition 分布）+ 客诉/打铃人工面。
- 回退触发：任一核心 SLO 击穿或 Blocker 级缺陷 → kill-switch 回退矩阵路径（T11.3）执行。
- 环境保真度声明附上：本机回环结论 + 灰度期补真实 SIP/公网验证。

- [ ] **Step 14.3: 栈恢复与汇报**

```bash
python tools/bok.py down
# T0.2 之前若有 runtime 常驻栈：按原姿势恢复并 ps 复核
```

向用户交付 VERDICT.md 摘要：「可/不可进入灰度」+ Blocker/Major 清单 + 建议修复顺序 + 灰度方案。

---

## Self-Review 记录（v2）

- **业界框架覆盖对照**：Google SRE launch checklist（SLO/监控/容量/回滚/oncall→T2/T13/T7/T11/T13）、全链路压测（T7 含资源遥测）、混沌（T9 九项）、长稳（T8）、灾备（T10）、灰度（T14.2）、依赖与密钥安全（T1.3/1.4）、业务质量 gate（T4）——v1 缺的六块全部落位。
- **用户原始诉求对照**：A/B 线多轮不同话术（T3.2/T4.1）、打断（T3.3）、WebUI 全功能（T6）、连通性（T6.4）、并发（T7）、响应（T2 SLO 化+T3.9/T5.2/T7.4）、生产判定（T14）。
- **命令核对**：全部脚本名来自 `scripts/` 实际清单；DB 路径经 `bok.app_data_dir()` 解析（T0.4）；gitleaks 已确认安装；pip-audit 需安装（T1.3 内含）；两处标「先 --help/grep 核对」的参数（branch --leg、BOK_DB_PATH 变量名）已标明核对动作。
- **执行顺序依赖**：T2 先于 T3-T9（立尺）；T0.5 的备份先于 T9 混沌（灾备素材+防止真库被混沌搞脏——**混沌只打主栈、灾备演练只打副本**）。
