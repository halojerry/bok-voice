# QA 画布编辑器 + 罐头状态面 + 惜客通导入器 — 设计文档

日期：2026-09-17
状态：已与用户逐段确认（画布编辑器/簇边+步骤边/同页切换/惜客通吸收项/罐头面/导入器均获拍板）

## 1. 背景与目标

快答库（`/qa`）现形态为「左卡片列表 + 右表单」。用户希望以画布连线方式管理与查看，并参考了惜客通PLUS
话术管理页（React Flow 画布 + 表格式问答库）及其导出数据（`docs/AI自销话术.tar.gz`、
`docs/海外仓话术演示.tar.gz`）。

三个子特性：

- **A. QA 画布**：同页「列表 | 画布」切换；节点=条目与话术步骤；边=同义簇与步骤挂载；拖拽连线可编辑。
- **B. 罐头状态面**：节点徽标显示应答音频物化状态（对齐惜客通「录音管理」的等价物），试听、单条/批量重新物化。
- **C. 惜客通导入器**：把惜客通导出的 `tbl_ai_knowledge.json` 搬进 `qa_entries`。

## 2. 非目标

- 不改 `qa_gate` 匹配逻辑、不改 agent 装配、不改 TTS 预热物化管线（罐头 key 规划只复用不改行为）。
- 不做追问链/意图规则引擎（惜客通 `AfterAnswerMode` 链式跳转、`tbl_dialog_intention_rules` 的可配置意向体系）——独立项目。
- 不做节点位置的服务端持久化（v1 存 localStorage；惜客通存 X/Y 于服务端，多机协同需求出现后再议）。
- 不抄「优先级」与「关键词库」（我们的匹配面=HybridLexical，`question_text` 即匹配面；变体行=关键词的更强形态）。
- 不做多答案轮换（他们 `Answer2-5`）：导入时仅取 `Answer1`，其余在导入报告中列示。

## 3. 事实依据（设计锚点）

- `qa_gate.match`（`apps/agent/agent_runtime/qa_gate.py`）过滤维度=**lang + scope + step_index**（+既有四道闸），
  **`template_id` 不参与运行时匹配**——步骤边派生只需 `scope/step_index`，`template_id` 仅决定画布上挂在哪张模板图。
- `qa_entries`（`packages/business-db/bok_voice_business_db/models.py`）现有列：question_text/answer_text/lang/
  scope/step_index/voice_id/enabled/hit_count/source(curated|mined)/template_id/owner_user_id/account_id。
- CP 端点：`GET/POST /api/qa-entries`、`PATCH/DELETE /api/qa-entries/{id}`、`POST /api/qa-entries/{id}/hit`、
  `POST /api/tts/preview`（试听已存在）；`control_plane/pregen.py` 已有 key 规划与单飞逻辑（人设保存点自动物化复用）。
- `tts_cache.py`：`cache_key()`（text+voice+model 精确匹配）、meta 带 `pinned`、`default_cache_dir()`；
  本地形态 CP 与 agent 同盘（pregen 日志即写同一 app-data 基目录），罐头状态可直读。
- 权限（B3/B4）：user=自己的+共享（共享只读）、admin/root/匿名本地=全量管理；owner 转移=root/admin。
- 惜客通数据结构（已验证）：`tbl_ai_knowledge.Question` 为 `&` 分隔多问法串；`Answer/Answer2-5` 多答案；
  `AfterAnswerMode/AfterAnswerSceneId/SceneStep` 答后跳转；`tbl_dialog_actions_local_v2` 画布节点含 X/Y；
  画布库同为 React Flow。

## 4. 子特性 A：QA 画布

### 4.1 数据模型（唯一 schema 变更）

- `QaEntry` 新增 `cluster_head_id: str = ""`（String(64)）。语义：非空=本条是 `cluster_head_id` 指向条目的
  同义变体（一层星形：head 不得再指向他条）。
- 迁移：`control_plane/deps.py build_engine()` 幂等补列（`_ensure_column`，inspector 版）；仓库层
  `create/patch` 透传该字段（权限盖章规则不变）；`delete_qa_entry` 级联
  `UPDATE qa_entries SET cluster_head_id='' WHERE cluster_head_id=:id`（防孤儿引用）。
- `scripts/dump_postgres_ddl.py` 重跑 → 应用 `scripts/.p0_supabase_schema.sql`（Supabase MCP migration）。
- `source` 字段新增取值 `imported`（展示徽标用，运行时不过滤）。

### 4.2 页面结构

- `/qa` 页头加「列表 | 画布」分段切换；两视图共享同一份 `rows` 数据与右侧表单状态。
- 画布组件 `next/dynamic` 懒加载（`@xyflow/react` v12 仅进画布 chunk，列表视图零负担；静态导出/Tauri 兼容）。
- 画布顶部工具条：模板选择下拉（默认第一个模板）、语言过滤 chips（三语）、「重置布局」、「一键补料」（见 B）。

### 4.3 图模型（`apps/web/lib/qa-canvas.ts`，纯函数，node --test 覆盖）

- 节点两类：
  - **步骤节点**：选中模板的 1..N 步 + 首列虚拟「全程通用」节点。
  - **QA 节点**：可见条目（manager=全部；user=自己的+共享）。
- 边两类：
  - **簇边**（实线）：`child.cluster_head_id → head`。
  - **步骤边**（虚线）：`scope=step` 条目 → 第 `step_index+1` 步节点；`scope=global` → 通用节点（不画线，归位首列即可）。
- 布局：确定性网格——列=步骤（通用/未挂步在首列），列内按簇分组（head 带头、变体随行），无簇条目按创建序排布。
  纯函数 `(rows, template, positions) => nodes/edges`；用户拖动过的位置存
  `localStorage["qa-canvas-pos:{accountId}:{templateId}"]`，未拖动的走自动布局；「重置布局」清键。
- 节点卡片：问法（≤2 行截断）+ 答案首行 + 徽标条（语言色 zh/粤/en、命中数、停用灰显、共享锁、来源
  curated/mined/imported、罐头状态徽标见 B）。节点尺寸随命中数微调。

### 4.4 交互

- **拖连线**（React Flow handle）：
  - 条目→条目 = 挂簇：被拖者 `cluster_head_id=目标id`。校验（纯函数）：同一簇内禁连；一层星形约束
    （目标自身已是变体 → 改挂其 head 或拒绝，实现取「重挂到目标的 head」）；禁止自连。
  - 条目→步骤节点 = `PATCH {scope:"step", step_index:N-1, template_id:当前模板}`；拖到通用节点 =
    `PATCH {scope:"global", step_index:-1}`。
- **断边**：点选边 → Delete 键或右键「解除」；簇边=清 `cluster_head_id`，步骤边=回 global。
- **编辑**：单击节点 → 右侧滑出与列表视图同款表单（复用现有 `QaForm` 状态与保存逻辑）；双击空白 = 新建
  （落点处插入，保存后按自动布局归位）；节点右键菜单 = 编辑/启停/删除/试听/重新物化（+manager: 归属转移在侧栏表单）。
- **权限**：与列表逐字一致——user 对共享条目不渲染拖拽柄与右键编辑项（只读+试听）；403 错误照常展示错误条。
- **保存与回滚**：每次连线/断线即时 PATCH（与列表行为一致）；失败顶部错误条 + 本地图状态回滚到
  `refresh()` 后的派生态。

## 5. 子特性 B：罐头状态面

- CP 新增 `GET /api/qa/canned-status?account_id=`（鉴权/可见性与 `/api/qa-entries` 同规）：
  复用 `pregen.py` 的 key 规划逻辑（必要时抽公共函数进 `packages/`，禁止 CP/agent 两份拷贝）逐条算
  `cache_key` → 查 tts-cache meta 存在性，返回 `{entry_id: {state: "ok"|"missing", voice}}`。
  文本/音色变更后 key 必然不同=missing，无需独立「过期」态。
- CP 新增 `POST /api/qa/pregen {ids?: string[]}`：触发 `pregen --qa`（带 ids 时限定条目；复用现有人设保存点的
  单飞锁与 `tts_pregen.status` 提醒面语义），返回 queued/already_running 等。
- Web：
  - 节点徽标：✓ 已物化（绿）/ 缺料（灰，注「走 LLM 兜底」）；侧栏表单内「试听」按钮直连 `POST /api/tts/preview`。
  - 工具条「一键补料」= `POST /api/qa/pregen`（全量幂等，重跑零云调用）；节点右键「重新物化」= 带 id 调用。
- 分布式形态（CP 与 agent 异机）不在本期：届时状态端点改走 agent 上报，接口形状不变。

## 6. 子特性 C：惜客通导入器

- `scripts/import_xkt_qa.py --input tbl_ai_knowledge.json --account acc-001 [--lang auto|zh|cantonese|en] [--owner ""] [--apply]`
  （默认 dry-run 打印计划，`--apply` 落地——对齐 `tts-mine` 交互惯例）。
- 字段映射：
  - `Question` 按 `&` 拆分：第一个=主条目 `question_text`，其余=变体行（`cluster_head_id` 指向主条目，同答案同语言）。
  - `Answer` → `answer_text`；`Answer2-5` 不导入，dry-run 报告列示条数。
  - `Status!=1` → `enabled=false` 照导（画布灰显，运营决定启停）。
  - `lang` 缺省 `auto`：粤语特征字（嘅係唔咗哋啲冇乜嚟）启发式判 cantonese，否则 zh；显式 flag 覆盖全部。
  - `source="imported"`、`hit_count=0`、`voice_id=""`、`owner_user_id` 按 flag（默认共享）。
  - `AfterAnswer*`（答后跳转）、`Priority`、`Trigger*`、`Interrupt*`、`LabelId`、`Intention`：不导入，
    dry-run 报告附「未搬字段」清单——步骤挂载留给画布拖线完成（导入器+画布正好闭环）。
- 去重：归一化问法（去空白/标点）与账号内现有条目重复 → 跳过并计数。
- `--apply` 走仓库层直写（与 `tts-mine --sync` 同层），结束后提示跑 `tts-pregen --qa` 物化。
- 输入兼容两张导出包（schema 相同）；JSON 为顶层数组。

## 7. API 变更汇总

| 变更 | 类型 | 说明 |
|---|---|---|
| `qa_entries.cluster_head_id` | schema | deps 幂等补列 + Supabase schema 重跑应用 |
| `PATCH /api/qa-entries/{id}` | 扩展 | 接受 `cluster_head_id`（权限规则不变） |
| `DELETE /api/qa-entries/{id}` | 行为 | 级联清引用 |
| `GET /api/qa/canned-status` | 新增 | 罐头物化状态 |
| `POST /api/qa/pregen` | 新增 | 触发物化（单飞） |
| `GET /api/qa-entries` | 不变 | 画布与列表同源 |

## 8. 错误处理

- PATCH 失败：错误条 + 图状态回滚（派生自最新 refresh 数据，不乐观保留脏边）。
- 罐头状态端点失败/超时：徽标降级为「未知」灰点，不阻塞画布渲染。
- pregen 已在跑：返回 `already_running`（沿用现语义），前端提示稍后。
- 导入器：dry-run 报告含 created/variants/skipped_dup/disabled/未搬字段统计；非法行（缺 Question/Answer）列明细不入库。
- 删除 head 条目：仓库层级联清引用，画布随后 refresh 自然散簇。

## 9. 测试计划

- Python（pytest）：
  - 仓库层：迁移补列、`patch cluster_head_id` 透传、删除级联清引用、B3 owner 规则不受影响。
  - 导入器纯函数：`&` 拆分、去重、lang 启发式、dry-run 计划、非法行报告（`tests/test_import_xkt_qa.py`）。
  - canned-status：tmp 缓存目录夹具下 ok/missing 两态（`tests/test_qa_canned_status.py`）。
  - `test_db_portability` 门禁照过；`test_cantonese_terminology` 照过（`cantonese` 为规范拼写）。
- Web：
  - `node --test`：`lib/qa-canvas.ts` 纯函数（布局确定性、边派生、簇校验/环拒绝、localStorage 键构造）。
  - `npx tsc --noEmit && npm run build` 门禁。
- Merge gate 全绿；E2E 不动（纯 UI/CP 配套，运行时零变化）。

## 10. 惜客通借鉴清单（已收编）

1. React Flow 选型（业界同款）+ MiniMap（一行配置）。
2. 节点底部徽标条 ← 他们的分支芯片。
3. 试听 ← 他们的「试听」列。
4. 罐头状态面 ← 他们的「录音管理」（等价物收编，范围收窄到 QA）。
5. 「答后进入第 N 步」心智 ← 他们的 `AfterAnswerSceneStep`（对应步骤边标注）。
6. 导入器 ← 他们的导出格式。

## 11. 交付顺序

1. schema + 仓库层 + 迁移（含 Supabase 重跑）与 Python 测试。
2. 画布只读渲染（布局/节点/边）→ 编辑交互（连线/断线/侧栏）→ 视图切换收口。
3. 罐头状态面（CP 端点 + 徽标 + 补料）。
4. 导入器（dry-run → --apply）。
5. `docs/REPO_MAP.md` 补新文件条目；`docs/RUNTIME_TOPOLOGY.md` 无端口/数据流变化不动。
