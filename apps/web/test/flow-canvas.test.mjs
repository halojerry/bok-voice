// lib/flow-canvas.ts 纯函数单测（W2 T2 流程画布）：step.ref 三件拆装 round-trip
// （语义镜像 agent flow.py _BRANCH_LINE_RE/_NOTE_LINE_RE/parse_step_ref）、flow.py
// 边角语法、scene 经 jsonToSteps/stepsToJson 往返不丢、场景泳道分桶、布局确定性。
//
// 装配照 qa-canvas.test.mjs：lib/flow-canvas.ts 零 import,单文件转译直载。
// 另加一份 components/template-editor.tsx 的真实代码装载（scene 往返守卫要跑
// **生产** stepsToJson/jsonToSteps,不许在测试里复刻）——该文件带 "use client" 与
// "@/" 别名组件依赖,node 装配走 noResolve 单文件转译（类型错误不阻断 emit）+
// Module._resolveFilename 钩子把 "@/..." 桩到最小 stub;临时目录落在 WEB_ROOT 下,
// 使 require("react")/"react/jsx-runtime" 能沿目录树上溯到 apps/web/node_modules。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import Module from "node:module";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");
const TSC = path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc");

// ---- 装配一：lib/flow-canvas.ts（零 import,qa-canvas.test.mjs 同款） ----
const TMP_FC = mkdtempSync(path.join(WEB_ROOT, ".tmp-flow-canvas-"));
execFileSync(
  process.execPath,
  [TSC, path.join(WEB_ROOT, "lib", "flow-canvas.ts"), "--outDir", TMP_FC, "--module", "commonjs", "--target", "es2020", "--skipLibCheck"],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const fc = require(path.join(TMP_FC, "flow-canvas.js"));

// ---- 装配二：components/template-editor.tsx（真实生产代码,stub 掉 "@/" 别名依赖） ----
const TMP_TE = mkdtempSync(path.join(WEB_ROOT, ".tmp-template-editor-"));
writeFileSync(
  path.join(TMP_TE, "stub.js"),
  [
    "// 测试桩：只补齐 template-editor 的模块级导入面（api/ErrorState/useAccount/useSession）,",
    "// 纯函数 stepsToJson/jsonToSteps 不经它们。",
    "exports.api = {};",
    "exports.ErrorState = () => null;",
    "exports.useAccount = () => ({ accountId: 'acc-001' });",
    "exports.useSession = () => null;",
    "",
  ].join("\n"),
);
// noResolve：不解析 import（"@/..." 无 paths 映射必报 TS2307）——错误不阻断 emit,
// 发射产物里的 require("@/...") 在装载期被钩子重定向到 stub。
spawnSync(
  process.execPath,
  [
    TSC, path.join(WEB_ROOT, "components", "template-editor.tsx"),
    "--outDir", TMP_TE, "--module", "commonjs", "--target", "es2020",
    "--skipLibCheck", "--noResolve", "--jsx", "react-jsx",
  ],
  { stdio: "ignore" },
);
const tePath = path.join(TMP_TE, "template-editor.js");
assert.ok(
  (() => { try { return readFileSync(tePath).length > 0; } catch { return false; } })(),
  "template-editor.tsx 转译产物缺失——测试装配失败",
);
const resolveFilename = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (String(request).startsWith("@/")) {
    return resolveFilename.call(this, path.join(TMP_TE, "stub.js"), ...rest);
  }
  return resolveFilename.call(this, request, ...rest);
};
const te = require(tePath);

test.after(() => {
  rmSync(TMP_FC, { recursive: true, force: true });
  rmSync(TMP_TE, { recursive: true, force: true });
});

// 断言助手：round-trip 不变量 = parse(serialize(parse(x))) 与 parse(x) 逐件相等。
function assertRoundTrip(ref) {
  const once = fc.parseStepRefParts(ref);
  const twice = fc.parseStepRefParts(fc.serializeStepRef(once));
  assert.deepEqual(twice, once);
  return once;
}

// ---- ① 真实样例 round-trip（语料取自 template-editor STEPS_EXAMPLES / flow.py 分支语法） ----
const REF_ZH = [
  "这是我们的责任，我们有购买运费保险，会以一赔二赔付给您，不用自己贴钱。",
  "如果客户问为什么赔 → 说明是运输途中遗失，我方全责",
  "如果客户担心不到账 → 说明赔付会直接到微信零钱/钱包",
  "如果客户说要重新买 → 说明可以用赔付抵扣，不用自己再贴钱",
].join("\n");

test("parseStepRefParts：正稿/分支/注意三件拆装（flow.py 真实语法）", () => {
  const parts = fc.parseStepRefParts(REF_ZH);
  assert.equal(parts.script, "这是我们的责任，我们有购买运费保险，会以一赔二赔付给您，不用自己贴钱。");
  assert.deepEqual(parts.branches, [
    { cond: "问为什么赔", resp: "说明是运输途中遗失，我方全责" },
    { cond: "担心不到账", resp: "说明赔付会直接到微信零钱/钱包" },
    { cond: "说要重新买", resp: "说明可以用赔付抵扣，不用自己再贴钱" },
  ]);
  assert.equal(parts.notes, "");
  // 条件=锚词与箭头之间、应答=箭头后（flow.py:96 语义）：箭头后首个非空白起,原样保留「就」头。
  const jiu = fc.parseStepRefParts("正稿一行\n如果客户嫌慢 → 就安抚并报时效");
  assert.deepEqual(jiu.branches, [{ cond: "嫌慢", resp: "就安抚并报时效" }]);
});

test("round-trip ①：parse→serialize→parse 逐件相等（正稿多行/两分支/注意行）", () => {
  const ref = [
    "係我哋责任,我哋有买运费保险,会以一赔二赔俾你,唔使自己蚀钱。",
    "如果客户问点解要赔 → 讲係运输途中遗失,顺丰全责",
    "如果客户担心唔到账 → 讲赔付会直接落微信零钱/钱包",
    "注意：赔付会直接落微信零钱",
  ].join("\n");
  const parts = assertRoundTrip(ref);
  assert.equal(parts.notes, "赔付会直接落微信零钱");
  assert.equal(parts.branches.length, 2);
  // 序列化规范形：正稿行 + 每分支「如果客户{cond}→{resp}」+「注意：{note}」；
  // 箭头两侧空白规范掉（解析层空白容差,flow.py:96 \s*→\s* 同款）。
  assert.equal(
    fc.serializeStepRef(parts),
    "係我哋责任,我哋有买运费保险,会以一赔二赔俾你,唔使自己蚀钱。\n"
      + "如果客户问点解要赔→讲係运输途中遗失,顺丰全责\n"
      + "如果客户担心唔到账→讲赔付会直接落微信零钱/钱包\n"
      + "注意：赔付会直接落微信零钱",
  );
});

test("round-trip ①b：多条注意行/空行/首行即分支/无正稿", () => {
  // 两条注意行 → notes \n 连接,序列化逐行还原「注意：」头。
  const two = assertRoundTrip("注意：先报单号\n如果客户报了 → 复述确认\n注意：再问平台");
  assert.equal(two.notes, "先报单号\n再问平台");
  assert.equal(two.script, "");
  // ref 为空/纯空白：三件全空,序列化空串。
  assert.deepEqual(fc.parseStepRefParts(""), { script: "", branches: [], notes: "" });
  assert.equal(fc.serializeStepRef({ script: "", branches: [], notes: "" }), "");
});

// ---- ② flow.py 边角语法（箭头空白变体/EN 锚词/注意冒号变体/解析不了→script） ----
test("flow.py 边角：箭头两侧空白容差逐字对齐 _BRANCH_LINE_RE（flow.py:96）", () => {
  const cases = [
    ["如果客户唔记得→提佢下单填嘅地址", "唔记得", "提佢下单填嘅地址"],
    ["如果客户唔记得 → 提佢下单填嘅地址", "唔记得", "提佢下单填嘅地址"],
    ["如果客户唔记得  →  提佢下单填嘅地址", "唔记得", "提佢下单填嘅地址"],
    ["如果客户 唔记得→提佢", "唔记得", "提佢"],
    // 应答含第二个箭头：非贪婪条件停在首箭头,余量全归应答。
    ["如果客户A→B→C", "A", "B→C"],
  ];
  for (const [line, cond, resp] of cases) {
    const parts = fc.parseStepRefParts(line);
    assert.deepEqual(parts.branches, [{ cond, resp }], line);
  }
});

test("flow.py 边角：EN 锚词（If/When the customer,IGNORECASE）与 Note 冒号变体", () => {
  const en = fc.parseStepRefParts(
    "Hello, is this {name}?\nIf the customer is busy right now → ask when or how works best\nwhen the customer worries → reassure them\nNote: keep it short\nnote：不用自己贴钱",
  );
  assert.equal(en.script, "Hello, is this {name}?");
  assert.deepEqual(en.branches, [
    { cond: "is busy right now", resp: "ask when or how works best" },
    { cond: "worries", resp: "reassure them" },
  ]);
  assert.equal(en.notes, "keep it short\n不用自己贴钱");
});

test("flow.py 边角：空条件行判定与解析不了的行兜底（逐字对齐 flow.py 正则行为）", () => {
  // 「如果客户 → 就xxx」：锚后空白被 lazy cond 吃成「 」,正则仍命中=空条件分支——
  // JS 与 Python 正则回溯序一致,flow.py 同样存 (cond="",resp="就xxx")。
  const emptyCond = fc.parseStepRefParts("如果客户 → 就xxx");
  assert.deepEqual(emptyCond.branches, [{ cond: "", resp: "就xxx" }]);
  // 序列化侧：空条件不成合法分支语法,降级普通行「就xxx」（文字不丢,重解析落 script）。
  assert.equal(fc.serializeStepRef(emptyCond), "就xxx");
  // 「客户报出号码(数字串)→复述确认」=flow.py:104 注释里的真实未知指令行：
  // 运行时只告警丢弃,画布保守保留进 script（任务书硬约束：解析不了的行不丢）。
  const unknown = fc.parseStepRefParts("正稿\n客户报出号码(数字串)→复述确认");
  assert.equal(unknown.branches.length, 0);
  assert.equal(unknown.script, "正稿\n客户报出号码(数字串)→复述确认");
});

// ---- ⑤ 未知行保留 round-trip ----
test("round-trip ⑤：未知形态行原样保留（序列化回写,再解析逐件相等）", () => {
  const ref = [
    "你好，请问係咪{姓名}？我哋係{物流公司}，有个包裹单号尾号{快递尾号}运输途中唔见咗。",
    "如果客户唔记得 → 提佢下单时填嘅地址帮佢回忆",
    "客户报出号码(数字串)→复述确认",
    "备注：本步要复核电话尾号",
  ].join("\n");
  const parts = assertRoundTrip(ref);
  // 三行全在 script（首行+并入的未知行+普通行）,逐行原样（剥行尾空白/空行的解析层规范外不动）。
  assert.equal(parts.script.split("\n").length, 3);
  assert.equal(parts.script.split("\n")[1], "客户报出号码(数字串)→复述确认");
  assert.equal(parts.branches.length, 1);
  // serialize 输出里未知行逐字在场。
  assert.ok(fc.serializeStepRef(parts).includes("客户报出号码(数字串)→复述确认"));
});

test("serialize 退化分支：缺条件/缺应答降级普通行,文字不丢", () => {
  const out = fc.serializeStepRef({
    script: "正稿",
    branches: [
      { cond: "", resp: "" },        // 全空:不产行
      { cond: "", resp: "孤应答" },  // 缺条件:降级普通行
      { cond: "孤条件", resp: "" },  // 缺应答:降级普通行
      { cond: "好", resp: "应答" },  // 完整:分支行
    ],
    notes: "",
  });
  assert.equal(out, "正稿\n孤应答\n孤条件\n如果客户好→应答");
});

// ---- ③ scene 经 jsonToSteps→stepsToJson 往返不丢（真实生产代码） ----
test("scene 往返：jsonToSteps→stepsToJson 不丢 scene；空 scene 不写键（旧数据零变化）", () => {
  const steps = [
    { goal: "开场", ref: "你好", scene: "开场" },
    { goal: "谈赔", ref: "係我哋责任", say: true, emotion: "sad", scene: "谈赔偿" },
    { goal: "收尾", ref: "拜拜" }, // 旧数据无 scene 键
  ];
  const json = te.stepsToJson(steps);
  const back = te.jsonToSteps(json);
  assert.equal(back[0].scene, "开场");
  assert.equal(back[1].scene, "谈赔偿");
  assert.equal(back[1].say, true);
  assert.equal(back[1].emotion, "sad");
  assert.equal(back[2].scene, ""); // 缺失 → ""（任务书口径）
  // 无 scene 的步落库 JSON 不带该键（W2 前数据逐字节同形）。
  const parsed = JSON.parse(json);
  assert.equal("scene" in parsed[2], false);
  assert.equal("scene" in parsed[1], true);
  // 非 say 步也带 scene（scene 不依附 say）。
  assert.equal(parsed[0].scene, "开场");
  // 再走一轮（保存→重开）仍不丢。
  assert.equal(te.jsonToSteps(te.stepsToJson(back))[0].scene, "开场");
});

// ---- ④ sceneLanes 泳道分桶 ----
test("sceneLanes：空 scene=未分组、连续同 scene 归并、交错按首次出现归并保序", () => {
  const steps = [
    { goal: "a", ref: "r", scene: "开场" },
    { goal: "b", ref: "r" }, // 空 scene
    { goal: "c", ref: "r", scene: "开场" }, // 交错:与 a 同泳道
    { goal: "d", ref: "r", scene: "谈赔" },
  ];
  const lanes = fc.sceneLanes(steps);
  assert.deepEqual(
    lanes.map((l) => l.name),
    ["开场", "未分组", "谈赔"], // 首次出现序;空 scene 殿后于「开场」、先于「谈赔」（保持原位）
  );
  assert.deepEqual(lanes[0].steps.map((s) => s.goal), ["a", "c"]);
  assert.deepEqual(lanes[1].steps.map((s) => s.goal), ["b"]);
  assert.deepEqual(lanes[2].steps.map((s) => s.goal), ["d"]);
  // 全空 scene → 单条未分组泳道。
  assert.deepEqual(
    fc.sceneLanes([{ goal: "x", ref: "r" }, { goal: "y", ref: "r" }]).map((l) => [l.name, l.steps.length]),
    [[fc.UNGROUPED_LANE, 2]],
  );
  assert.deepEqual(fc.sceneLanes([]), []);
});

// ---- 布局（layoutFlow）：确定性/泳道几何/意图 overlay ----
const GRAPH = {
  version: 1,
  intents: [
    { id: "int_a", label: "投诉", keywords: ["投诉"], steps: [], enabled: true, judge: { prompt: "客户表达不满" } },
    { id: "int_b", label: "退款", keywords: ["退款"], steps: [3], enabled: false },
  ],
  bindings: [
    { id: "bnd_1", intent: "int_a", action: "jump_step", step: 3, priority: 10, once: false, enabled: true },
    { id: "bnd_2", intent: "int_a", action: "play_qa", qa_id: "qa-1", priority: 10, once: true, enabled: true },
    { id: "bnd_3", intent: "int_b", action: "jump_step", step: 99, priority: 10, once: false, enabled: false }, // 停用:不画
    { id: "bnd_4", intent: "int_ghost", action: "jump_step", step: 1, priority: 10, once: false, enabled: true }, // 悬空意图:不画
  ],
};

test("layoutFlow：泳道纵向、步脊柱、意图侧栏/jumpIn/徽标、确定性", () => {
  const steps = [
    { goal: "s1", ref: "正稿一", scene: "开场" },
    { goal: "s2", ref: "如果客户问 → 答", scene: "开场" },
    { goal: "s3", ref: "正稿三" },
  ];
  const g1 = fc.layoutFlow(steps, GRAPH);
  const g2 = fc.layoutFlow(steps, GRAPH);
  assert.deepEqual(g1, g2); // 确定性:同输入同输出

  const lanes = g1.nodes.filter((n) => n.kind === "lane");
  assert.deepEqual(lanes.map((n) => n.name), ["开场", fc.UNGROUPED_LANE]);
  const laneY = Object.fromEntries(lanes.map((n) => [n.name, n.y]));
  assert.ok(laneY[fc.UNGROUPED_LANE] > laneY["开场"]); // 泳道纵向排布

  const stepNodes = g1.nodes.filter((n) => n.kind === "step");
  assert.equal(stepNodes.length, 3);
  const byId = Object.fromEntries(stepNodes.map((n) => [n.id, n]));
  assert.ok(byId["fstep:1"].scriptFirst.includes("如果客户问")); // 正稿首行（无正稿步显示分支首行）
  assert.equal(byId["fstep:2"].x, byId["fstep:0"].x); // 同一脊柱列
  assert.equal(byId["fstep:2"].jumpIn, 1); // int_a jump_step→第 3 步

  // 意图节点：jump 边（启用的）1 条,目标=fstep:2;play_qa/judge/停用徽标进 data。
  const intents = g1.nodes.filter((n) => n.kind === "intent");
  assert.equal(intents.length, 2); // 停用意图照渲染
  const jumps = g1.edges.filter((e) => e.kind === "jump");
  assert.equal(jumps.length, 1);
  assert.equal(jumps[0].source, "fintent:int_a");
  assert.equal(jumps[0].target, "fstep:2");
  assert.equal(intents.find((n) => n.intentId === "int_a").playQa, true);
  assert.equal(intents.find((n) => n.intentId === "int_a").judge, true);
  assert.equal(intents.find((n) => n.intentId === "int_b").enabled, false);

  // 泳道内脊柱线（开场两条步之间）；跨泳道不拉线。
  const spines = g1.edges.filter((e) => e.kind === "spine");
  assert.deepEqual(spines.map((e) => [e.source, e.target]), [["fstep:0", "fstep:1"]]);

  // 无图:零意图零 jump 边。
  const bare = fc.layoutFlow(steps);
  assert.equal(bare.nodes.filter((n) => n.kind === "intent").length, 0);
  assert.equal(bare.edges.filter((e) => e.kind === "jump").length, 0);
});
