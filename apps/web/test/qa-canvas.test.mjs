// lib/qa-canvas.ts 纯函数单测（qa-canvas Phase 1 Task 4）：steps_json 解析、
// 画布图派生（步骤脊柱列 + 簇星形边 + 确定性布局）、连簇星形校验、布局 localStorage 键。
//
// 与 apiBase.test.mjs 同款装配：api.ts 是带 "use client" 消费方的 TS 源码且
// apps/web 无 JS 测试基建，采用单文件转译路线——tsc CLI 带显式文件入参时忽略
// tsconfig.json（noEmit 不碍事），转译成 CJS 产物后经 createRequire 加载——
// 不引新依赖（typescript 本就在 devDependencies，构建必需），不经 npx
// （直呼 node_modules 内 bin，确定性）。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");

// ---- 装配：lib/qa-canvas.ts → 临时 CJS 产物（模块加载期一次性完成，失败=整文件红）----
const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-qa-canvas-"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(WEB_ROOT, "lib", "qa-canvas.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const qa = require(path.join(TMP_OUT, "qa-canvas.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

const ROWS = [
  { id: "h", question_text: "怎么查物流", answer_text: "在小程序查", lang: "zh", scope: "global", step_index: -1, cluster_head_id: "", enabled: true, hit_count: 5, created_at: "2026-01-01" },
  { id: "v", question_text: "物流咋查", answer_text: "在小程序查", lang: "zh", scope: "step", step_index: 1, cluster_head_id: "h", enabled: true, hit_count: 1, created_at: "2026-01-02" },
  { id: "s", question_text: "多久到", answer_text: "三天内", lang: "zh", scope: "step", step_index: 0, cluster_head_id: "", enabled: false, hit_count: 0, created_at: "2026-01-03" },
];
const STEPS = [{ goal: "开场", ref: "你好" }, { goal: "通知", ref: "抱歉" }];

test("parseTemplateSteps 解析 steps_json", () => {
  const steps = qa.parseTemplateSteps(JSON.stringify(STEPS));
  assert.equal(steps.length, 2);
  assert.equal(qa.parseTemplateSteps("").length, 0);
});

test("deriveGraph 步骤脊柱+簇边+步骤边+顺序线", () => {
  const g = qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} });
  assert.equal(g.stepNodes.filter((n) => n.data.virtual !== true).length, 2);
  const kinds = g.edges.map((e) => e.data.kind).sort();
  assert.deepEqual(kinds, ["cluster", "spine", "step", "step"]); // spine=步骤间顺序线
  const cluster = g.edges.find((e) => e.data.kind === "cluster");
  assert.equal(cluster.source, "v"); assert.equal(cluster.target, "h");
  const spine = g.edges.find((e) => e.data.kind === "spine");
  assert.equal(spine.source, "step:0"); assert.equal(spine.target, "step:1");
  // 布局确定性:同输入两次全同
  assert.deepEqual(qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} }),
                   qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} }));
});

test("deriveGraph 布局 v1.1——节距/簇留白/global 左泳道", () => {
  // A(step0 独立) → B(step0, A 的变体) → C(step0 独立)：簇边界前应有额外留白。
  const rows = [
    { id: "A", question_text: "q1", answer_text: "a", lang: "zh", scope: "step", step_index: 0, cluster_head_id: "", enabled: true },
    { id: "B", question_text: "q2", answer_text: "a", lang: "zh", scope: "step", step_index: 0, cluster_head_id: "A", enabled: true },
    { id: "C", question_text: "q3", answer_text: "a", lang: "zh", scope: "step", step_index: 0, cluster_head_id: "", enabled: true },
  ];
  const g = qa.deriveGraph(rows, STEPS, { langFilter: "all", positions: {} });
  const byId = Object.fromEntries(g.qaNodes.map((n) => [n.id, n.position]));
  assert.equal(byId.B.y - byId.A.y, qa.ENTRY_PITCH_Y);          // 常规节距
  assert.ok(byId.C.y - byId.B.y >= qa.ENTRY_PITCH_Y + qa.CLUSTER_GAP_Y); // 簇边界留白
  // 变体缩进：B 在 A 右侧。
  assert.ok(byId.B.x > byId.A.x);
  // global 条目在脊柱左侧独立泳道。
  const gh = qa.deriveGraph([ROWS[0]], STEPS, { langFilter: "all", positions: {} });
  assert.ok(gh.qaNodes[0].position.x < 0);
});

test("resolveClusterTarget 星形校验", () => {
  assert.equal(qa.resolveClusterTarget(ROWS, "v", "h").ok, true);       // 变体重挂主条目
  assert.equal(qa.resolveClusterTarget(ROWS, "h", "v").ok, false);      // head 不可挂到自己变体
  assert.equal(qa.resolveClusterTarget(ROWS, "h", "h").ok, false);      // 自连
  assert.equal(qa.resolveClusterTarget(ROWS, "s", "v").headId, "h");    // 目标是变体→重定向其 head
});

test("resolveClusterTarget 已带变体的主条目作 source 整类拒绝（防两层链）", () => {
  // h 已带变体 v：H1→H2（重挂别的 head）会令 V1→H1→H2 两层链，spec §4.4 一层星形不变量。
  const R2 = [
    ...ROWS,
    { id: "h2", question_text: "退款多久", answer_text: "七个工作日", lang: "zh", scope: "global", step_index: -1, cluster_head_id: "", enabled: true, hit_count: 0, created_at: "2026-01-04" },
  ];
  assert.equal(qa.resolveClusterTarget(R2, "h", "h2").ok, false);   // H1→H2
  assert.equal(qa.resolveClusterTarget(R2, "h", "v").ok, false);    // H1→自家变体（同族方向）
  assert.notEqual(qa.resolveClusterTarget(R2, "h", "h2").reason, "");
  // 不带变体的条目仍可作 source（回归锚）。
  assert.equal(qa.resolveClusterTarget(R2, "h2", "v").ok, true);
});

test("deriveGraph col0 与 col1 条目位置不重合（global 左泳道 vs 卫星右道）", () => {
  // 曾有缺陷：col0（global）与 col1（step 0）条目 x、y 全同像素级重叠。
  // v1.1 后 global 泳道在脊柱左侧（负 x），卫星道在右侧（+SAT_X）。
  const g = qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} });
  const byId = Object.fromEntries(g.qaNodes.map((n) => [n.id, n.position]));
  assert.notDeepEqual(byId.h, byId.s);        // h=global 泳道, s=step0 泳道
  assert.ok(byId.h.x < 0);                    // global 甩到脊柱左侧
  assert.ok(byId.s.x > 0);                    // 卫星在脊柱右侧走廊
});

test("revertCluster 回滚断簇", () => {
  const rows = [{ id: "v", cluster_head_id: "h2" }];
  assert.equal(qa.revertCluster(rows, "v", "h1")[0].cluster_head_id, "h1");
});

test("LOCAL_POS_KEY 形态", () => {
  assert.equal(qa.LOCAL_POS_KEY("acc-001", "tpl-1"), "qa-canvas-pos:acc-001:tpl-1");
});

// ---- 话术图 Phase 2:意图节点/绑定边派生 ----
const GRAPH_DOC = {
  version: 1,
  intents: [
    { id: "int_1a2b3c4d", label: "投诉", keywords: ["投诉"], steps: [], enabled: true },
    { id: "int_2b3c4d5e", label: "退款", keywords: ["退款"], steps: [3], enabled: false },
  ],
  bindings: [
    { id: "bnd_7e8f9a0b", intent: "int_1a2b3c4d", action: "jump_step", step: 4, priority: 10, once: false, enabled: true },
    { id: "bnd_c1d2e3f4", intent: "int_2b3c4d5e", action: "play_qa", qa_id: "nope", priority: 5, once: true, enabled: true },
    { id: "bnd_d2e3f4a5", intent: "int_nope", action: "jump_step", step: 2, priority: 9, once: false, enabled: true }, // 悬空→不画
  ],
};

test("parseGraphDoc tolerant", () => {
  assert.deepEqual(qa.parseGraphDoc(""), { version: 1, intents: [], bindings: [] });
  assert.deepEqual(qa.parseGraphDoc("garbage").intents, []);
  assert.equal(qa.parseGraphDoc(JSON.stringify(GRAPH_DOC)).intents.length, 2);
});

test("deriveGraph intent nodes anchored to scope step", () => {
  const steps = qa.parseTemplateSteps(JSON.stringify([{ goal: "g1", ref: "r1" }, { goal: "g2", ref: "r2" }, { goal: "g3", ref: "r3" }]));
  const graph = qa.deriveGraph([], steps, { graph: qa.parseGraphDoc(JSON.stringify(GRAPH_DOC)) });
  const intentNodes = graph.nodes.filter((n) => n.type === "intent");
  assert.equal(intentNodes.length, 2); // 禁用意图照渲染(视图置灰)
  const complain = intentNodes.find((n) => n.id === "intent:int_1a2b3c4d");
  assert.equal(complain.position.x, qa.INTENT_X);
  // 第 1 步 = step:0，其 y=80（脊柱基线 80+i*STEP_GAP_Y；y=0 属于 step:global 虚拟节点）。
  // [task-6 勘误] plan 原断言写 0，与本文件既有脊柱基线常量不符。
  assert.equal(complain.position.y, 80);
  const refund = intentNodes.find((n) => n.id === "intent:int_2b3c4d5e");
  assert.equal(refund.data.intent.enabled, false);
});

test("deriveGraph binding edges skip dangling and carry label", () => {
  // [task-6 勘误] plan 夹具 3 步（step:0..2）与断言「1-based 第 4 步 → step:3」不符：3 步下
  // step:4 越界被钳到 step:2。仅补第 4 步令原断言逐字成立，其余断言未改。
  const steps = qa.parseTemplateSteps(
    JSON.stringify([{ goal: "g1", ref: "r1" }, { goal: "g2", ref: "r2" }, { goal: "g3", ref: "r3" }, { goal: "g4", ref: "r4" }]),
  );
  const graph = qa.deriveGraph([], steps, { graph: qa.parseGraphDoc(JSON.stringify(GRAPH_DOC)) });
  const bindEdges = graph.edges.filter((e) => e.data?.kind === "binding");
  assert.equal(bindEdges.length, 1); // 悬空 intent 与悬空 qa_id 均不画
  assert.equal(bindEdges[0].id, "bind:bnd_7e8f9a0b");
  assert.equal(bindEdges[0].source, "intent:int_1a2b3c4d");
  assert.equal(bindEdges[0].target, "step:3"); // 1-based 第 4 步 → 0-based step:3
  assert.equal(bindEdges[0].label, "投诉");
});

test("graph absent = zero intent nodes/edges", () => {
  const steps = qa.parseTemplateSteps(JSON.stringify([{ goal: "g", ref: "r" }]));
  const graph = qa.deriveGraph([], steps, {});
  assert.equal(graph.nodes.filter((n) => n.type === "intent").length, 0);
  assert.equal(graph.edges.filter((e) => e.data?.kind === "binding").length, 0);
});

test("deriveGraph 三类节点 data.kind 判别面（step/qaEntry/intent 同面可判别）", () => {
  const graph = qa.deriveGraph(ROWS, STEPS, {
    langFilter: "all", positions: {}, graph: qa.parseGraphDoc(JSON.stringify(GRAPH_DOC)),
  });
  // 判别面必须每个节点都在（Task 7/8 靠 data.kind 分派渲染/交互，虚拟 step:global 不例外）。
  assert.ok(graph.nodes.every((n) => typeof n.data.kind === "string"));
  assert.deepEqual([...new Set(graph.nodes.map((n) => n.data.kind))].sort(), ["intent", "qaEntry", "step"]);
  assert.equal(graph.nodes.find((n) => n.id === "step:global").data.kind, "step");
  assert.equal(graph.nodes.find((n) => n.id === "h").data.kind, "qaEntry");
  assert.equal(graph.nodes.find((n) => n.id === "intent:int_1a2b3c4d").data.kind, "intent");
});

// ---- Phase 3.3 追问链 then_jump：加载/保存往返 + 默认关（勘误预检 4 的守门测试）----
// 勘误预检 4：page.tsx 的保存是**逐字段重建**——不进 BindingDraft 的键编辑一次即蒸发。
// 故往返测试必须跑**真代码**（qa.bindingFromDraft=submit 对两种动作的唯一重建入口），
// 不得在测试里复刻一份投影（复刻=并行实现，改坏生产却照样绿：review R1 I1 的变异实验）。
// 加载侧 bindingThenJumpToDraft 进草稿，保存侧 bindingFromDraft/bindingThenJumpField 出键。

/** 夹具按 bindingFromDraft 的**字段发射顺序**书写（id/intent/priority/once/enabled →
 *  action → qa_id → then_jump），故可用整串比对断言「保存后逐字节不变」，而非仅比值。 */
const CHAIN_DOC = {
  version: 1,
  intents: [{ id: "int_1a2b3c4d", label: "退款", keywords: ["退款"], steps: [], enabled: true }],
  bindings: [
    {
      id: "bnd_7e8f9a0b", intent: "int_1a2b3c4d", priority: 10, once: false, enabled: true,
      action: "play_qa", qa_id: "qa-1", then_jump: 4,
    },
    { id: "bnd_c1d2e3f4", intent: "int_1a2b3c4d", action: "jump_step", step: 2, priority: 10, once: false, enabled: true },
  ],
};

test("then_jump 往返：打开既有追问链→改名保存→链逐字节存活（勘误预检 4）", () => {
  const doc = qa.parseGraphDoc(JSON.stringify(CHAIN_DOC));
  const original = doc.bindings[0];
  assert.equal(original.then_jump, 4);
  // 加载：then_jump 进草稿（缺这一步=打开即丢链）
  const draft = qa.bindingThenJumpToDraft(original.then_jump, 5);
  assert.equal(draft, 4);
  // 保存：走 page.tsx submit 的真实重建函数（改优先级，其余原样）
  const saved = qa.bindingFromDraft({ ...original, then_jump: draft, priority: 3 }, original.intent, 5);
  // 链存活且值不变；再经一次 parse 仍读得回（落库文本→运行时解析同路）
  assert.equal(saved.then_jump, 4);
  assert.equal(saved.priority, 3);
  assert.equal(qa.parseGraphDoc(JSON.stringify({ version: 1, intents: CHAIN_DOC.intents, bindings: [saved] }))
    .bindings[0].then_jump, 4);
  // 未改字段时逐字节同（含键序）——真代码不许顺手重排/丢键
  const untouched = qa.bindingFromDraft(original, original.intent, 5);
  assert.equal(JSON.stringify(untouched), JSON.stringify(original));
});

test("then_jump 保存侧：0 / jump_step / 坏值绝不写键（CP 严格校验会 400）", () => {
  const base = CHAIN_DOC.bindings[0];
  // 清空→不写键（键缺席=无链）；经真实重建函数同样不带键
  assert.deepEqual(qa.bindingThenJumpField("play_qa", 0, 5), {});
  assert.deepEqual(qa.bindingFromDraft({ ...base, then_jump: 0 }, base.intent, 5), {
    id: base.id, intent: base.intent, priority: 10, once: false, enabled: true, action: "play_qa", qa_id: "qa-1",
  });
  // jump_step 行带 then_jump → 恒 {}
  assert.deepEqual(qa.bindingThenJumpField("jump_step", 4, 5), {});
  // …且草稿行即使带着 then_jump，重建出的 jump_step 绑定也不许出现该键
  assert.deepEqual(
    qa.bindingFromDraft(
      { id: "bnd_x", action: "jump_step", step: 2, then_jump: 4, priority: 10, once: false, enabled: true },
      "int_1a2b3c4d", 5,
    ),
    { id: "bnd_x", intent: "int_1a2b3c4d", priority: 10, once: false, enabled: true, action: "jump_step", step: 2 },
  );
  for (const bad of [NaN, Infinity, -1, 0, null, undefined, ""]) {
    assert.deepEqual(qa.bindingThenJumpField("play_qa", bad, 5), {}, String(bad));
  }
  // 越界按真实步数收口（同 step 的 M28 姿势）
  assert.deepEqual(qa.bindingThenJumpField("play_qa", 99, 5), { then_jump: 5 });
  // review R1 M2：上界再压 CP 合同 999（>999 步的话术也不吐越界键）
  assert.deepEqual(qa.bindingThenJumpField("play_qa", 4, 5000), { then_jump: 4 });
  assert.deepEqual(qa.bindingThenJumpField("play_qa", 5000, 10000), { then_jump: 999 });
  assert.equal(qa.bindingFromDraft({ ...base, then_jump: 5000 }, base.intent, 10000).then_jump, 999);
});

test("then_jump 加载侧：缺省/坏值落 0（不跳），越界按真实步数钳位", () => {
  assert.equal(qa.bindingThenJumpToDraft(undefined, 5), 0); // 存量图无键
  assert.equal(qa.bindingThenJumpToDraft(null, 5), 0);
  assert.equal(qa.bindingThenJumpToDraft("x", 5), 0);
  assert.equal(qa.bindingThenJumpToDraft(0, 5), 0);
  assert.equal(qa.bindingThenJumpToDraft(4, 5), 4);
  assert.equal(qa.bindingThenJumpToDraft(9, 5), 5); // 步数被改小后越界 → 收口
  assert.equal(qa.bindingThenJumpToDraft(4, 0), 1); // 零步骤话术不产生 <1 值
});

test("then_jump 画布第二边默认关：带链的图不多出任何边（零视觉变化）", () => {
  const steps = qa.parseTemplateSteps(JSON.stringify(
    [{ goal: "g1", ref: "r1" }, { goal: "g2", ref: "r2" }, { goal: "g3", ref: "r3" }, { goal: "g4", ref: "r4" }],
  ));
  // 目标快答条目在位（否则 play_qa 边悬空不画，证明不了「不额外画边」）。
  const rows = [{ id: "qa-1", question_text: "怎么退", answer_text: "点这里", lang: "zh", scope: "global", step_index: -1, cluster_head_id: "", enabled: true }];
  const graph = qa.deriveGraph(rows, steps, {
    langFilter: "all", graph: qa.parseGraphDoc(JSON.stringify(CHAIN_DOC)),
  });
  const bindEdges = graph.edges.filter((e) => e.data?.kind === "binding").map((e) => e.id).sort();
  assert.deepEqual(bindEdges, ["bind:bnd_7e8f9a0b", "bind:bnd_c1d2e3f4"]); // 链图仍只有一对绑定边
  assert.equal(graph.edges.filter((e) => String(e.id).startsWith("thenjump:")).length, 0);
});

// 接线守门（review R1 I1 的补强）：纯函数测得到重建逻辑，却测不到「page.tsx 有没有调它、
// 调用结果有没有被 push 进落库数组」。逐字段重建的丢键风险恰在这两行，故源码扫描钉死。
test("page.tsx 接线：草稿进 then_jump、submit 用真实重建函数且结果直接入 nextBindings", () => {
  const src = readFileSync(path.join(WEB_ROOT, "app", "(app)", "qa", "page.tsx"), "utf8");
  assert.match(src, /then_jump:\s*number;/); // 草稿类型带 then_jump（不进草稿=保存即蒸发）
  assert.match(src, /then_jump:\s*bindingThenJumpToDraft\(/); // 加载侧走纯函数
  // 保存侧：重建结果必须直接进落库数组（不许再自拼 { ...common, ... }）
  assert.match(src, /nextBindings\.push\(\s*bindingFromDraft\(row,\s*intent\.id,\s*stepCount\)/);
  assert.doesNotMatch(src, /nextBindings\.push\(\s*\{\s*\.\.\.common/);
});

// —— Phase 3.4 判据（judge.prompt）——
// 空判据必须**字节级**同旧 JSON：靠 `intentJudgeField` 空值返 undefined + JSON.stringify
// 省键，故下面用真 JSON.stringify 对照（不是 deepEqual——键在场值 undefined 也该消失）。
test("判据投影：空/纯空白省键（JSON 逐字节同旧文档），非空 trim 后落 prompt", () => {
  assert.equal(qa.JUDGE_PROMPT_MAX_CHARS, 400);
  assert.equal(qa.intentJudgeField(""), undefined);
  assert.equal(qa.intentJudgeField("   \n\t "), undefined);
  assert.equal(qa.intentJudgeField(undefined), undefined);
  assert.deepEqual(qa.intentJudgeField("  客户表达不满时算命中  "), { prompt: "客户表达不满时算命中" });

  // 保存路径投影形状（page.tsx onConfirm 字面量同形：...intent + judge: intentJudgeField(...)）。
  const base = { id: "int_1", label: "投诉", keywords: ["投诉"], steps: [], enabled: true };
  const projected = {
    ...base, label: "投诉", keywords: ["投诉"], steps: [], enabled: true,
    judge: qa.intentJudgeField("  "),
  };
  assert.equal(JSON.stringify(projected), JSON.stringify(base)); // 空判据：零字节变化
  // 清空既有判据（编辑旧意图删掉判据后保存）：也回到旧文档字节。
  const hadJudge = { ...base, judge: { prompt: "旧判据" } };
  assert.equal(
    JSON.stringify({ ...hadJudge, judge: qa.intentJudgeField("") }),
    JSON.stringify(base),
  );
  // 非空判据：键在场且只有 prompt（形状与 CP 契约一致）。
  const withJudge = { ...base, judge: qa.intentJudgeField("客户要求赔偿但没说出关键词") };
  assert.equal(
    JSON.stringify(withJudge),
    '{"id":"int_1","label":"投诉","keywords":["投诉"],"steps":[],"enabled":true,'
      + '"judge":{"prompt":"客户要求赔偿但没说出关键词"}}',
  );
});

// 接线守门（同 Phase 3.3 bindingFromDraft 的源码扫描）：纯函数测得到投影，却测不到「草稿有没有
// 进编辑器状态、保存字面量有没有折入 judge」。judge 只有唯一保存路径投影，源码扫描钉死。
test("page.tsx 接线：判据草稿进状态、判据文本域在场、保存字面量折入唯一投影", () => {
  const canvasSrc = readFileSync(path.join(WEB_ROOT, "lib", "qa-canvas.ts"), "utf8");
  assert.match(canvasSrc, /judge\?:\s*\{\s*prompt:\s*string\s*\}/); // 模型面
  const src = readFileSync(path.join(WEB_ROOT, "app", "(app)", "qa", "page.tsx"), "utf8");
  assert.match(src, /useState\(intent\.judge\?\.prompt \?\? ""\)/); // 加载侧进草稿
  assert.match(src, /judge:\s*intentJudgeField\(judgeText\)/); // 保存侧唯一投影
  // I1 修正(review)：裸 JUDGE_PROMPT_MAX_CHARS 会被 import 行喂饱(恒绿)——钉比较本身，
  // 删掉整段超长检查块此断言必红。
  assert.match(src, /judgePrompt\.length\s*>\s*JUDGE_PROMPT_MAX_CHARS/); // 超长可见报错（不静默截断）
  assert.match(src, /判据最长 /); // 报错文案在场（与上一条双锚，防只留比较删报错）
  assert.match(src, /判据（可选）/); // 文本域标签
  assert.match(
    src,
    /判据即 prompt 片段：写清什么算命中、什么不算（正反例）。留空=仅关键词确定性命中。关键词未中时由后台大模型按判据评估，命中下一轮生效。/,
  );
  assert.doesNotMatch(src, /judge:\s*\{\s*prompt/); // 不许第二处自拼 judge 对象
  // N1 修正(review)：形状特定负守卫测不到 judge: intentJudgeField(other) 类第二投影——
  // count 锚直钉「全文件 judge: 字面量恰一处」。
  assert.equal((src.match(/judge:/g) ?? []).length, 1); // 唯一保存路径投影（count 锚）
});

