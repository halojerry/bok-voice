// lib/step-canvas.ts（Scene Canvas v2 一步一画布派生层,2026-09-26）配套单测：
//   ① stepUniverse：第 N 步宇宙派生——anchors 动作/jump 派生、意图 scoped/global 分桶
//      （disabled 也出卡、停用绑定不收）、chips（其余步+收线+打铃）与 usedBy 记账、
//      ghost（非末步→下一步/末步→收线）、scriptFirst（40 字截断/纯分支退分支摘要）、
//      越界与 NaN、graph 缺省=空图;
//   ② layoutStepUniverse：固定三列布局——分支边只画有动作锚点（无标记/留本步不画线）、
//      意图边三路（jump_step→步 chip / play_qa→快答徽章 / notify_human→打铃）、
//      ghost 只读虚线（末步→收线 chip）、qaLabels 反查与「词条缺失」、
//      确定性（同输入两次调用 deepEqual）、节点 kinds 齐全;
//   ③ buildRail：branchCount / intentCount（scoped 口径,全程意图不计）/ say / scene 口径;
//   ④ setStepBranchAction / clearStepBranchAction：动作标记写回分支行 resp——
//      旧标记剥掉不叠、应答文字保留、整步 ref 逐字节 round-trip、其它步逐字节不变、
//      无效寻址原样返回同一引用（身份断言）、jump 步号钳 1..步数。
//
// 装配：lib/step-canvas.ts import "./flow-canvas"（相对路径）,不能单文件直载——
// 把 lib/flow-canvas.ts 与 lib/step-canvas.ts 两个文件用 tsc 转译进**同一个**临时 outDir
// （测的是真实生产纯函数,不在测试里复刻）,emit 的 require("./flow-canvas") 在同目录天然
// 解析到同一份编译产物。临时目录建在 WEB_ROOT（apps/web）下：转译/装载失败 try/finally
// 立即清场,正常路径 test.after 收尾清理。
//
// 已知契约疑点（测试按**实现现状**钉住并用「契约疑点」注释标位,审查时核）：
//   A. stepUniverse 意图分桶：任务书与 lib 源码字段注释都说 steps 空/缺=全程意图 →
//      globalIntents（不铺节点）,实现三元 (scope.length===0||scope.includes(n))
//      ? scoped : global 把两分支装反——全程意图被铺卡、别步意图进了 global 桶。
//   B. 非整数 stepNo（如 2.5）：任务书说「非整数 → null」,实现是 Math.round 后校验,
//      2.5 舍入成第 3 步并不拒绝。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdtempSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");
const TSC = path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc");

// ---- 装配：两文件同 outDir 转译,step-canvas.js 的 require("./flow-canvas") 同目录解析 ----
const TMP = mkdtempSync(path.join(WEB_ROOT, ".tmp-step-canvas-"));
let loaded = null;
try {
  execFileSync(
    process.execPath,
    [
      TSC,
      path.join(WEB_ROOT, "lib", "flow-canvas.ts"),
      path.join(WEB_ROOT, "lib", "step-canvas.ts"),
      "--outDir", TMP,
      "--module", "commonjs",
      "--target", "es2020",
      "--skipLibCheck",
    ],
    { stdio: "inherit" },
  );
  const require = createRequire(import.meta.url);
  const fc = require(path.join(TMP, "flow-canvas.js"));
  const sc = require(path.join(TMP, "step-canvas.js"));
  loaded = { fc, sc };
} finally {
  if (!loaded) rmSync(TMP, { recursive: true, force: true }); // 转译/装载失败不留临时目录
}
const fc = loaded.fc;
const sc = loaded.sc;

test.after(() => {
  rmSync(TMP, { recursive: true, force: true });
});

// ---- 测试数据工厂（每个用例现造,避免用例间串味;函数永不原地改输入,此处纯为可读性） ----

// 4 步模板：第 2 步 ref = 正稿 + 3 分支（【收线】/【跳第4步】/无标记）+ 1 注意行。
// 行序按 serializeStepRef 的产出行序（正稿段→分支段→注意段）,保 round-trip 逐字节可断言。
function makeSteps() {
  return [
    { goal: "身份确认", ref: "你好，请问係{姓名}小姐吗？我係快递客服。", scene: "开场" },
    {
      goal: "来电通知",
      ref: [
        "您有一个快递到了，需要跟您确认一下派送。",
        "如果客户骂人 → 【收线】唔好意思打搅咗，祝您生活愉快",
        "如果客户赶时间→【跳第4步】直接讲办理",
        "如果客户嫌慢 → 安抚并报时效",
        "注意：收线前必须道歉一次",
      ].join("\n"),
    },
    { goal: "问进度", ref: "正稿：您的快递正在派送中。\n如果客户问几时到 → 报当日时效", scene: "  物流查询  " },
    { goal: "收尾", ref: "多谢配合，祝您生活愉快，再见。", say: true },
  ];
}

// 图：2 张 scoped 意图（steps:[2] / [2,3]）+ 1 张全程意图（steps:[]）+ 1 张别步意图（steps:[1]）。
// 绑定三路各一：jump_step / play_qa（qa_id 反查标签）/ notify_human——notify 挂在 scoped 意图
// （i-price）上,使「notify_human→打铃」这条意图边不依赖全程意图的铺卡与否（见契约疑点 A）。
function makeGraph() {
  return {
    intents: [
      { id: "i-cancel", label: "取消订单", keywords: ["取消", "唔要"], steps: [2], judge: { prompt: "客户是否想取消？" } },
      { id: "i-price", label: "问赔偿", keywords: ["赔偿"], steps: [2, 3] },
      { id: "i-global", label: "转人工", keywords: ["人工"], steps: [] },
      { id: "i-other", label: "开场确认", steps: [1] },
    ],
    bindings: [
      { id: "b-jump", intent: "i-cancel", action: "jump_step", step: 4 },
      { id: "b-qa", intent: "i-price", action: "play_qa", qa_id: "qa-1" },
      { id: "b-notify", intent: "i-price", action: "notify_human" },
    ],
  };
}

const QA_LABELS = { "qa-1": "点解咁耐仲未到？" };

// ---- ① 模块面：五件套导出在场 + flow-canvas 再导出同源（同一模块实例） ----

test("step-canvas 模块面：五件套导出在场,flow-canvas 再导出与直载产物同源", () => {
  for (const fn of ["stepUniverse", "layoutStepUniverse", "buildRail", "setStepBranchAction", "clearStepBranchAction"]) {
    assert.equal(typeof sc[fn], "function", `${fn} 导出在场`);
  }
  for (const fn of ["parseStepRefParts", "serializeStepRef", "parseBranchAction", "composeBranchResp"]) {
    assert.equal(typeof sc[fn], "function", `再导出 ${fn} 在场`);
  }
  // step-canvas emit 的 require("./flow-canvas") 解析到同一份编译产物——函数是同一个引用。
  assert.strictEqual(sc.parseStepRefParts, fc.parseStepRefParts);
  assert.strictEqual(sc.parseBranchAction, fc.parseBranchAction);
});

// ---- ② stepUniverse 基本盘（第 2 步） ----

test("stepUniverse 基本盘：anchors 动作/jump 派生、意图分桶、chips+usedBy 记账、ghost 非末步", () => {
  const u = sc.stepUniverse(makeSteps(), makeGraph(), 2, { qaLabels: QA_LABELS });
  assert.ok(u, "第 2 步宇宙非 null");
  assert.equal(u.stepNo, 2);
  assert.equal(u.index, 1); // 0-based
  assert.equal(u.goal, "来电通知");
  assert.equal(u.say, false);
  assert.equal(u.emotion, "");
  assert.equal(u.scene, "");
  assert.equal(u.scriptFirst, "您有一个快递到了，需要跟您确认一下派送。");

  // anchors：parseStepRefParts().branches 逐条 + parseBranchAction 派生;resp 恒为原文含标记。
  assert.deepEqual(
    u.anchors.map((a) => [a.branchIndex, a.cond, a.action, a.jump]),
    [
      [0, "骂人", "refuse", 0],
      [1, "赶时间", "jump", 4],
      [2, "嫌慢", "", 0],
    ],
  );
  assert.equal(u.anchors[0].resp, "【收线】唔好意思打搅咗，祝您生活愉快");

  // 意图分桶（2026-09-26 审查已修,按契约面钉死）：steps 空=全程 → globalIntents（横条,不铺节点）;
  // 非空含本步 → scopedIntents;非空不含本步（i-other）两桶都不出现——本画布不出现,换步再看。
  assert.deepEqual(u.scopedIntents.map((c) => c.intentId), ["i-cancel", "i-price"]);
  assert.deepEqual(u.globalIntents.map((c) => c.intentId), ["i-global"]);
  assert.ok(!u.scopedIntents.some((c) => c.intentId === "i-other"), "别步意图不进触发列");
  assert.ok(!u.globalIntents.some((c) => c.intentId === "i-other"), "别步意图也不进横条桶");

  // 意图卡字段：judge 徽标 / keywords / 绑定视图（qa_id 反查 qaLabels）。
  const cancel = u.scopedIntents[0];
  assert.equal(cancel.label, "取消订单");
  assert.equal(cancel.enabled, true);
  assert.equal(cancel.judge, true);
  assert.deepEqual(cancel.keywords, ["取消", "唔要"]);
  assert.equal(cancel.bindings.length, 1);
  assert.equal(cancel.bindings[0].bindingId, "b-jump");
  assert.equal(cancel.bindings[0].action, "jump_step");
  assert.equal(cancel.bindings[0].step, 4);
  const price = u.scopedIntents[1];
  assert.equal(price.judge, false);
  assert.equal(price.bindings.length, 2);
  assert.equal(price.bindings[0].bindingId, "b-qa");
  assert.equal(price.bindings[0].action, "play_qa");
  assert.equal(price.bindings[0].qaId, "qa-1");
  assert.equal(price.bindings[0].qaLabel, "点解咁耐仲未到？");
  assert.equal(price.bindings[1].bindingId, "b-notify");
  assert.equal(price.bindings[1].action, "notify_human");

  // chips = 其余 3 步 + 收线 + 打铃;usedBy 记账与 layout 边 id 同规则
  // （分支边 "sc:br:<i>" / 意图边 "sc:ib:<bindingId>";无标记分支不记;play_qa 不入 chip——它去快答徽章）。
  assert.deepEqual(u.chips.map((c) => c.id), [
    "sc:tgt:step:1",
    "sc:tgt:step:3",
    "sc:tgt:step:4",
    "sc:tgt:refuse",
    "sc:tgt:bell",
  ]);
  assert.deepEqual(u.chips.map((c) => c.usedBy), [
    [],
    [],
    ["sc:br:1", "sc:ib:b-jump"], // 第 4 步：分支【跳第4步】+ 意图 jump_step 两路都指它
    ["sc:br:0"], // 收线：分支【收线】
    ["sc:ib:b-notify"], // 打铃：scoped 意图（i-price）的 notify_human——全程意图不铺卡不记账
  ]);
  assert.equal(u.chips[0].label, "第1步 · 身份确认");
  assert.equal(u.chips[3].label, "收线");
  assert.equal(u.chips[4].label, "转人工 / 打铃");

  // ghost：4 步模板的第 2 步=非末步 → 下一步。
  assert.deepEqual(u.ghost, { targetStepNo: 3, closing: false, label: "默认推进 → 第3步（引擎固有，不可编辑）" });
});

test("scriptFirst：正稿首行 40 字截断;纯分支 ref 退首分支摘要（标记剥掉、箭头规范形）", () => {
  const longRef = "你好，请问係{姓名}小姐吗？我係快递客服，想同您确认一下今日嘅派送安排，唔使一分钟。";
  const u1 = sc.stepUniverse([{ goal: "开场", ref: longRef }], null, 1);
  assert.equal(u1.scriptFirst.length, 40, "正稿首行超 40 字截断");
  assert.ok(longRef.startsWith(u1.scriptFirst), "截断保留首行前缀");

  // 无正稿（纯分支 ref）：退「如果客户<cond>→<剥标记文本>」,动作标记不进摘要。
  const up = sc.stepUniverse([{ goal: "g", ref: "如果客户骂人 → 【收线】拜拜" }], null, 1);
  assert.equal(up.scriptFirst, "如果客户骂人→拜拜");
});

test("末步 ghost：closing=true、targetStepNo=null;layout 的 sc:ghost 边指向收线 chip", () => {
  const u4 = sc.stepUniverse(makeSteps(), makeGraph(), 4);
  assert.ok(u4);
  assert.deepEqual(u4.ghost, { targetStepNo: null, closing: true, label: "末步讲完自动收线（引擎固有）" });
  // 末步 chips：其余 3 步 + 两个固定终点。
  assert.deepEqual(u4.chips.map((c) => c.id), [
    "sc:tgt:step:1",
    "sc:tgt:step:2",
    "sc:tgt:step:3",
    "sc:tgt:refuse",
    "sc:tgt:bell",
  ]);
  const l4 = sc.layoutStepUniverse(u4);
  const ge = l4.edges.find((e) => e.id === "sc:ghost");
  assert.ok(ge, "ghost 边在场");
  assert.equal(ge.kind, "ghost");
  assert.equal(ge.target, "sc:tgt:refuse", "末步 ghost 指向收线 chip");
  assert.equal(ge.label, u4.ghost.label);
});

test("越界/非法 stepNo → null;graph=undefined = 空图,chips/ghost 照常", () => {
  const steps = makeSteps();
  const graph = makeGraph();
  assert.equal(sc.stepUniverse(steps, graph, 0), null, "stepNo=0 越界");
  assert.equal(sc.stepUniverse(steps, graph, 5), null, "stepNo>步数越界");
  assert.equal(sc.stepUniverse(steps, graph, NaN), null, "NaN 非法");
  // 非整数拒绝（2026-09-26 审查收紧：严格整数契约,不再 Math.round 静默错步）。
  assert.equal(sc.stepUniverse(steps, graph, 2.5), null, "2.5 非整数拒绝");

  // graph 缺省 = 空图：意图两桶全空;chips/ghost 等「步自身」派生不受影响。
  const un = sc.stepUniverse(steps, undefined, 2);
  assert.ok(un);
  assert.deepEqual(un.scopedIntents, []);
  assert.deepEqual(un.globalIntents, []);
  assert.equal(un.chips.length, 5);
  assert.deepEqual(un.ghost, { targetStepNo: 3, closing: false, label: "默认推进 → 第3步（引擎固有，不可编辑）" });
  assert.equal(un.anchors.length, 3);
});

test("disabled 意图也出卡（enabled:false）,停用绑定不入 bindings/usedBy", () => {
  const graphOff = {
    intents: [
      { id: "i-off", label: "停用意图", steps: [2], enabled: false },
      { id: "i-on", label: "启用意图", steps: [2] },
    ],
    bindings: [
      { id: "bx", intent: "i-on", action: "notify_human", enabled: false }, // 停用绑定不收
      { id: "by", intent: "i-on", action: "jump_step", step: 3 },
    ],
  };
  const uo = sc.stepUniverse(makeSteps(), graphOff, 2);
  assert.deepEqual(
    uo.scopedIntents.map((c) => [c.intentId, c.enabled, c.bindings.length]),
    [
      ["i-off", false, 0],
      ["i-on", true, 1],
    ],
  );
  // usedBy 只记 enabled 绑定：停用的 notify_human 不上打铃 chip。
  assert.deepEqual(uo.chips.find((c) => c.id === "sc:tgt:bell").usedBy, []);
  assert.deepEqual(uo.chips.find((c) => c.id === "sc:tgt:step:3").usedBy, ["sc:ib:by"]);
});

// ---- ③ layoutStepUniverse：固定三列布局 ----

test("layoutStepUniverse：分支边只画有动作、意图边三路、qabadge、ghost、kinds 齐全、确定性", () => {
  const u = sc.stepUniverse(makeSteps(), makeGraph(), 2, { qaLabels: QA_LABELS });
  const l = sc.layoutStepUniverse(u);

  // 节点面：步 + scoped 意图卡 + 1 枚快答徽章 + 5 枚 chip。
  const ids = l.nodes.map((n) => n.id);
  assert.equal(ids.filter((x) => x === "sc:step").length, 1);
  assert.ok(ids.includes("sc:int:i-cancel"));
  assert.ok(ids.includes("sc:int:i-price"));
  assert.ok(ids.includes("sc:qa:b-qa"));
  // 全程意图不铺节点（分桶修复后按契约面钉死）——只活在画布顶部横条。
  assert.ok(!ids.includes("sc:int:i-global"), "全程意图不铺节点");
  assert.ok(!ids.includes("sc:int:i-other"), "别步意图不铺节点");
  assert.equal(l.nodes.filter((n) => n.kind === "chip").length, 5);

  // kinds 齐全：step / intent / chip / qabadge 四种。
  assert.deepEqual([...new Set(l.nodes.map((n) => n.kind))].sort(), ["chip", "intent", "qabadge", "step"]);

  // 快答徽章内容：bindingId/qaId/标签（qaLabels 反查）。
  const badge = l.nodes.find((n) => n.id === "sc:qa:b-qa");
  assert.equal(badge.kind, "qabadge");
  assert.equal(badge.bindingId, "b-qa");
  assert.equal(badge.qaId, "qa-1");
  assert.equal(badge.label, "快答：点解咁耐仲未到？");

  const byId = new Map(l.edges.map((e) => [e.id, e]));

  // 分支边：只画有动作锚点的——br:0【收线】→收线 chip;br:1【跳第4步】→第 4 步 chip;
  // br:2 无标记（留本步语义）→ 无边。
  const br0 = byId.get("sc:br:0");
  assert.equal(br0.kind, "branch");
  assert.equal(br0.source, "sc:step");
  assert.equal(br0.branchIndex, 0);
  assert.equal(br0.target, "sc:tgt:refuse");
  const br1 = byId.get("sc:br:1");
  assert.equal(br1.kind, "branch");
  assert.equal(br1.target, "sc:tgt:step:4");
  assert.ok(!byId.has("sc:br:2"), "无标记分支不画线");
  // jump 边 target chip 在场。
  assert.ok(l.nodes.some((n) => n.id === "sc:tgt:step:4" && n.kind === "chip"));

  // 意图边三路：jump_step→步 chip / play_qa→快答徽章 / notify_human→打铃。
  const ibJump = byId.get("sc:ib:b-jump");
  assert.equal(ibJump.kind, "intent");
  assert.equal(ibJump.source, "sc:int:i-cancel");
  assert.equal(ibJump.target, "sc:tgt:step:4");
  assert.equal(ibJump.label, "取消订单");
  const ibQa = byId.get("sc:ib:b-qa");
  assert.equal(ibQa.kind, "intent");
  assert.equal(ibQa.source, "sc:int:i-price");
  assert.equal(ibQa.target, "sc:qa:b-qa");
  assert.equal(ibQa.label, "播快答");
  // notify_human → 打铃（挂在 scoped 意图 i-price 上,不依赖全程意图的铺卡口径）。
  const ibNotify = byId.get("sc:ib:b-notify");
  assert.equal(ibNotify.kind, "intent");
  assert.equal(ibNotify.source, "sc:int:i-price");
  assert.equal(ibNotify.target, "sc:tgt:bell");
  assert.equal(ibNotify.label, "问赔偿（打铃）");

  // ghost 只读虚线：非末步 → 下一步 chip,label 同宇宙。
  const ge = byId.get("sc:ghost");
  assert.equal(ge.kind, "ghost");
  assert.equal(ge.target, "sc:tgt:step:3");
  assert.equal(ge.label, u.ghost.label);

  // 确定性：同输入两次调用 deepEqual。
  assert.deepEqual(sc.layoutStepUniverse(u), l);
});

test("layoutStepUniverse：qaLabels 缺失 → 快答徽章 label 含「词条缺失」", () => {
  const uNoQa = sc.stepUniverse(makeSteps(), makeGraph(), 2);
  const bq = uNoQa.scopedIntents.find((c) => c.intentId === "i-price").bindings[0];
  assert.equal(bq.qaLabel, "", "无 opts.qaLabels 反查为空串");
  const lNoQa = sc.layoutStepUniverse(uNoQa);
  const badge = lNoQa.nodes.find((n) => n.id === "sc:qa:b-qa");
  assert.ok(badge, "快答徽章仍产出");
  assert.ok(badge.label.includes("词条缺失"));
});

// ---- ④ setStepBranchAction / clearStepBranchAction：锚点连线写回 ----

test("setStepBranchAction：无标记分支设 jump(4)——行首出【跳第4步】、应答保留、整步 ref 逐字节、其它步不变", () => {
  const steps = makeSteps();
  const ref2Before = steps[1].ref;
  const out = sc.setStepBranchAction(steps, 2, 2, "jump", 4);
  assert.notStrictEqual(out, steps, "返回新数组");
  assert.strictEqual(steps[1].ref, ref2Before, "输入数组未被原地改");
  // 整步 ref 逐字节：只有目标分支行多了标记,正稿/其它分支/注意行一字不动（arrow 分隔保真）。
  assert.equal(out[1].ref, [
    "您有一个快递到了，需要跟您确认一下派送。",
    "如果客户骂人 → 【收线】唔好意思打搅咗，祝您生活愉快",
    "如果客户赶时间→【跳第4步】直接讲办理",
    "如果客户嫌慢 → 【跳第4步】安抚并报时效",
    "注意：收线前必须道歉一次",
  ].join("\n"));
  // 其它三步 ref 逐字节不变。
  assert.equal(out[0].ref, steps[0].ref);
  assert.equal(out[2].ref, steps[2].ref);
  assert.equal(out[3].ref, steps[3].ref);
  // 写回结果可再拆出：parseBranchAction 还原 action/step/text。
  assert.deepEqual(sc.parseBranchAction(sc.parseStepRefParts(out[1].ref).branches[2].resp), {
    action: "jump",
    step: 4,
    text: "安抚并报时效",
  });
});

test("setStepBranchAction：refuse→handoff 标记替换不叠;clear 后动作空、原始应答逐字节还原", () => {
  const steps = makeSteps();
  // 【收线】→【转人工】：旧标记剥掉、应答文字保留、恰一个【】标记不叠加。
  const s1 = sc.setStepBranchAction(steps, 2, 0, "handoff");
  const resp1 = sc.parseStepRefParts(s1[1].ref).branches[0].resp;
  assert.equal(resp1, "【转人工】唔好意思打搅咗，祝您生活愉快");
  assert.equal(resp1.split("【").length - 1, 1, "标记替换不叠——恰一个【】");
  // clear：分支保留为普通答法,动作拆出为空、text 等于原始应答。
  const s2 = sc.clearStepBranchAction(s1, 2, 0);
  const parts2 = sc.parseStepRefParts(s2[1].ref);
  assert.deepEqual(sc.parseBranchAction(parts2.branches[0].resp), {
    action: "",
    step: 0,
    text: "唔好意思打搅咗，祝您生活愉快",
  });
  // 分支条数与注意行不丢（serialize round-trip 既有纪律）。
  assert.equal(parts2.branches.length, 3);
  assert.equal(parts2.notes, "收线前必须道歉一次");
  assert.equal(parts2.script, "您有一个快递到了，需要跟您确认一下派送。");
});

test("setStepBranchAction：无效寻址原样返回同一引用（身份断言）;jump 钳制 99→步数、0→1", () => {
  const sa = makeSteps();
  const before = sa.map((s) => s.ref);
  assert.strictEqual(sc.setStepBranchAction(sa, 0, 0, "refuse"), sa, "步号 0 越界→原样返回");
  assert.strictEqual(sc.setStepBranchAction(sa, 9, 0, "refuse"), sa, "步号>步数→原样返回");
  assert.strictEqual(sc.setStepBranchAction(sa, 2, 99, "refuse"), sa, "分支下标越界→原样返回");
  assert.strictEqual(sc.setStepBranchAction(sa, 2, -1, "refuse"), sa, "分支下标为负→原样返回");
  assert.deepEqual(sa.map((s) => s.ref), before, "无效寻址不产生任何改动");

  // jump 步号钳 1..步数：4 步模板敲 99 → 钳到 4。
  const s99 = sc.setStepBranchAction(makeSteps(), 2, 2, "jump", 99);
  assert.equal(sc.parseBranchAction(sc.parseStepRefParts(s99[1].ref).branches[2].resp).step, 4);
  // jumpStep 0/缺省 → 钳回 1。
  const s0 = sc.setStepBranchAction(makeSteps(), 2, 2, "jump", 0);
  assert.equal(sc.parseBranchAction(sc.parseStepRefParts(s0[1].ref).branches[2].resp).step, 1);
});

// ---- ⑤ buildRail：步列口径 ----

test("buildRail：branchCount/intentCount（scoped 口径,全程意图不计）/say/scene;graph 缺省=零意图", () => {
  const rail = sc.buildRail(makeSteps(), makeGraph());
  assert.equal(rail.length, 4);
  assert.deepEqual(rail.map((r) => r.stepNo), [1, 2, 3, 4]);
  // 第 2 步 3 分支;第 3 步 1 分支;第 1/4 步纯正稿。
  assert.deepEqual(rail.map((r) => r.branchCount), [0, 3, 1, 0]);
  // intentCount 只数 steps 非空且含本步的意图：第 1 步=i-other,第 2 步=i-cancel+i-price,
  // 第 3 步=i-price,第 4 步=0;全程意图（steps:[]）任何步都不计。
  assert.deepEqual(rail.map((r) => r.intentCount), [1, 2, 1, 0]);
  assert.equal(rail[0].goal, "身份确认");
  assert.equal(rail[2].scene, "物流查询", "scene trim");
  assert.equal(rail[3].say, true);
  assert.equal(rail[1].say, false);

  // graph 缺省（null/undefined）= 空图：intentCount 全 0,branchCount 不受影响。
  for (const graph of [null, undefined]) {
    const railBare = sc.buildRail(makeSteps(), graph);
    assert.ok(railBare.every((r) => r.intentCount === 0));
    assert.deepEqual(railBare.map((r) => r.branchCount), [0, 3, 1, 0]);
  }
});
