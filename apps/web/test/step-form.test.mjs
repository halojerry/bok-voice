// components/step-form.tsx（列表编辑卡片结构化,2026-09-25）配套单测：
//   ① round-trip 逐字节无损：含 分支+动作标记【收线】【跳第3步】【留本步】+注意行+未知行
//      的 ref 原文 → parseStepRefParts → serializeStepRef → 与原文逐字节一致（未知行不丢）;
//   ② 分支行编辑流（组件同款三件拆装）：改条件/改应答后 serialize 产出合法 如果…→… 行;
//   ③ 动作下拉切换语义：切动作不丢字、jump 默认步号、应答换行折叠成空格;
//   ④ clampJumpStepInRange：jump 步号输入 1..步数 钳制（列表卡片口径,与画布抽屉 999 上限的差异位）;
//   ⑤ 「＋加一条应对」空分支不污染 ref;填一半残行降级普通行文字不丢。
//
// 装配照 csv.test.mjs/flow-canvas.test.mjs：
//   - lib/flow-canvas.ts 零 import,单文件 tsc 转译直载（真实生产纯函数,不在测试里复刻）;
//   - components/step-form.tsx 带 "use client" 与 "@/" 别名依赖,noResolve 单文件转译
//     （类型错误不阻断 emit）+ Module._resolveFilename 钩子把 "@/lib/flow-canvas" 桩到
//     装配一的真实转译产物（组件吃到的拆装函数=生产同一份）、把 "@/components/var-insert"
//     桩到最小 stub;临时目录落在 WEB_ROOT 下,require("react")/"react/jsx-runtime"
//     沿目录树上溯 apps/web/node_modules。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import Module from "node:module";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");
const TSC = path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc");

// ---- 装配一：lib/flow-canvas.ts（零 import,直载） ----
const TMP_FC = mkdtempSync(path.join(WEB_ROOT, ".tmp-stepform-fc-"));
execFileSync(
  process.execPath,
  [TSC, path.join(WEB_ROOT, "lib", "flow-canvas.ts"), "--outDir", TMP_FC, "--module", "commonjs", "--target", "es2020", "--skipLibCheck"],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const fc = require(path.join(TMP_FC, "flow-canvas.js"));

// ---- 装配二：components/step-form.tsx（真实生产代码,@/ 依赖钩子重定向） ----
// noResolve：不解析 import（"@/..." 无 paths 映射必报 TS2307,jsx-runtime 报 TS2875）
// ——类型错误不阻断 emit,发射产物里的 require("@/...") 在装载期被钩子重定向。
const TMP_SF = mkdtempSync(path.join(WEB_ROOT, ".tmp-stepform-sf-"));
spawnSync(
  process.execPath,
  [
    TSC, path.join(WEB_ROOT, "components", "step-form.tsx"),
    "--outDir", TMP_SF, "--module", "commonjs", "--target", "es2020",
    "--skipLibCheck", "--noResolve", "--jsx", "react-jsx",
  ],
  { stdio: "ignore" },
);
const sfPath = path.join(TMP_SF, "step-form.js");
assert.ok(
  (() => { try { return readFileSync(sfPath).length > 0; } catch { return false; } })(),
  "step-form.tsx 转译产物缺失——测试装配失败",
);
const stubPath = path.join(TMP_SF, "stub.js");
writeFileSync(stubPath, "exports.VarTextarea = () => null;\n");
const prevResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === "@/lib/flow-canvas") {
    return prevResolve.call(this, path.join(TMP_FC, "flow-canvas.js"), ...rest);
  }
  if (request === "@/components/var-insert") {
    return prevResolve.call(this, stubPath, ...rest);
  }
  return prevResolve.call(this, request, ...rest);
};
const stepForm = require(sfPath);

test.after(() => {
  Module._resolveFilename = prevResolve;
  rmSync(TMP_FC, { recursive: true, force: true });
  rmSync(TMP_SF, { recursive: true, force: true });
});

// 断言助手：round-trip 不变量 = parse(serialize(parse(x))) 与 parse(x) 逐件相等。
function assertRoundTrip(ref) {
  const once = fc.parseStepRefParts(ref);
  const twice = fc.parseStepRefParts(fc.serializeStepRef(once));
  assert.deepEqual(twice, once);
  return once;
}

// ---- ① 模块面 + round-trip 逐字节无损（任务书验收 3 的第一问） ----
test("step-form 模块面：BranchRowEditor/StepRefForm/clampJumpStepInRange 导出在场", () => {
  assert.equal(typeof stepForm.BranchRowEditor, "function");
  assert.equal(typeof stepForm.StepRefForm, "function");
  assert.equal(typeof stepForm.clampJumpStepInRange, "function");
});

test("round-trip 逐字节：分支+动作标记【收线】【跳第3步】【留本步】+注意行+未知行原样回写", () => {
  // 注意：serialize 输出顺序=正稿段（含未知行）→ 分支行 → 注意行,故未知行排在分支前。
  const ref = [
    "正稿第一行，含{姓名}变量。",
    "客户报出号码(数字串)→复述确认",
    "如果客户骂人 → 【收线】唔好意思打搅咗，祝您生活愉快",
    "如果客户赶时间→【跳第3步】直接讲办理",
    "如果客户重听  →  【留本步】好嘅，我哋慢慢嚟",
    "注意：收线前必须道歉一次",
    "注意：赔付只能进微信零钱",
  ].join("\n");
  const parts = fc.parseStepRefParts(ref);
  assert.equal(parts.script, "正稿第一行，含{姓名}变量。\n客户报出号码(数字串)→复述确认");
  assert.deepEqual(parts.branches.map((b) => b.resp), [
    "【收线】唔好意思打搅咗，祝您生活愉快",
    "【跳第3步】直接讲办理",
    "【留本步】好嘅，我哋慢慢嚟",
  ]);
  assert.equal(parts.notes, "收线前必须道歉一次\n赔付只能进微信零钱");
  // 逐字节一致：未知行不丢、三种箭头空格形态保真、标记一次不丢一次不重。
  assert.equal(fc.serializeStepRef(parts), ref);
  assertRoundTrip(ref);
});

// ---- ② 分支行编辑流（组件 BranchRowEditor 同款三件拆装） ----
test("分支行编辑：改条件后 serialize 产出合法 如果…→… 行（arrow 分隔保真）", () => {
  const parts = fc.parseStepRefParts("正稿\n如果客户问为什么赔 → 说明是运输途中遗失");
  const condEdited = { ...parts, branches: [{ ...parts.branches[0], cond: "嫌赔得少" }] };
  const out = fc.serializeStepRef(condEdited);
  assert.equal(out, "正稿\n如果客户嫌赔得少 → 说明是运输途中遗失");
  // 重解析仍是一条合法分支行。
  assert.deepEqual(fc.parseStepRefParts(out).branches, [
    { cond: "嫌赔得少", resp: "说明是运输途中遗失", arrow: " → " },
  ]);
});

test("分支行编辑：改应答（composeBranchResp 重组带标记）后 serialize 产出合法行,动作可再拆出", () => {
  const parts = fc.parseStepRefParts("正稿\n如果客户骂人 → 【收线】唔好意思");
  const info = fc.parseBranchAction(parts.branches[0].resp);
  assert.deepEqual(info, { action: "refuse", step: 0, text: "唔好意思" });
  const respEdited = {
    ...parts,
    branches: [{ ...parts.branches[0], resp: fc.composeBranchResp("refuse", 0, info.text + "，拜拜") }],
  };
  const out = fc.serializeStepRef(respEdited);
  assert.equal(out, "正稿\n如果客户骂人 → 【收线】唔好意思，拜拜");
  const back = fc.parseStepRefParts(out);
  assert.equal(back.branches.length, 1);
  assert.deepEqual(fc.parseBranchAction(back.branches[0].resp), {
    action: "refuse", step: 0, text: "唔好意思，拜拜",
  });
});

// ---- ③ 动作下拉切换语义（BranchRowEditor onChange 同款） ----
test("动作切换：无标记→refuse 切动作不丢字;jump 默认步号 1", () => {
  // 无标记分支切「礼貌收线」：文本保留、标记加在首部（组件 step= info.action==="jump" ? info.step : 1）。
  const p = fc.parseStepRefParts("正稿\n如果客户嫌慢 → 就安抚并报时效");
  const b = p.branches[0];
  const info = fc.parseBranchAction(b.resp);
  const switched = {
    ...p,
    branches: [{ ...b, resp: fc.composeBranchResp("refuse", info.action === "jump" ? info.step : 1, info.text) }],
  };
  assert.equal(fc.serializeStepRef(switched), "正稿\n如果客户嫌慢 → 【收线】就安抚并报时效");
  // 无标记分支切「跳到第 N 步」：默认步号 1。
  const switchedJump = {
    ...p,
    branches: [{ ...b, resp: fc.composeBranchResp("jump", info.action === "jump" ? info.step : 1, info.text) }],
  };
  const outJump = fc.serializeStepRef(switchedJump);
  assert.equal(outJump, "正稿\n如果客户嫌慢 → 【跳第1步】就安抚并报时效");
  assert.equal(fc.parseBranchAction(fc.parseStepRefParts(outJump).branches[0].resp).step, 1);
});

test("应答 textarea 换行折叠（组件 onChange replace）→ serialize 仍单行合法分支", () => {
  const p = fc.parseStepRefParts("正稿\n如果客户嫌慢 → 就安抚");
  const folded = {
    ...p,
    branches: [{
      ...p.branches[0],
      resp: fc.composeBranchResp("", 0, "第一句\r\n第二句".replace(/[\r\n]+/g, " ")),
    }],
  };
  const out = fc.serializeStepRef(folded);
  assert.equal(out, "正稿\n如果客户嫌慢 → 第一句 第二句");
  assert.equal(fc.parseStepRefParts(out).branches.length, 1);
});

// ---- ④ jump 步号输入钳制（列表卡片口径 1..步数） ----
test("clampJumpStepInRange：1..stepCount 钳制;非法回落 1;步数未知/非法=只按 1..999 兜底", () => {
  const cj = stepForm.clampJumpStepInRange;
  assert.equal(cj(3, 4), 3);        // 正常在界内
  assert.equal(cj(1, 4), 1);
  assert.equal(cj(9, 4), 4);        // 超步数钳到末步
  assert.equal(cj(0, 4), 1);        // 非法回落 1（与 lib clampJumpStep 同底）
  assert.equal(cj(-2, 4), 1);
  assert.equal(cj(2.5, 4), 1);      // 非整数回落 1
  assert.equal(cj(2, 1), 1);        // 单步模板
  assert.equal(cj(5, undefined), 5);  // 步数未知=不额外钳
  assert.equal(cj(999, undefined), 999);
  assert.equal(cj(5000, undefined), 1); // >999 与 lib 同底回落 1
  assert.equal(cj(5, 0), 5);          // 非法步数=不钳
});

test("jump 步号输入流（组件同款）：clamp 后 compose 产出【跳第N步】,重拆动作可还原", () => {
  const p = fc.parseStepRefParts("正稿\n如果客户赶时间 → 直接讲办理");
  const b = p.branches[0];
  const n = stepForm.clampJumpStepInRange(Number("9"), 4); // 4 步模板里敲 9 → 钳到 4
  const edited = {
    ...p,
    branches: [{ ...b, resp: fc.composeBranchResp("jump", n, fc.parseBranchAction(b.resp).text) }],
  };
  const out = fc.serializeStepRef(edited);
  assert.equal(out, "正稿\n如果客户赶时间 → 【跳第4步】直接讲办理");
  assert.deepEqual(fc.parseBranchAction(fc.parseStepRefParts(out).branches[0].resp), {
    action: "jump", step: 4, text: "直接讲办理",
  });
});

// ---- ⑤ 空分支/残行：ref 不被污染、文字不丢 ----
test("加一条应对（空分支）不污染 ref;填一半残行降级普通行文字不丢", () => {
  const parts = fc.parseStepRefParts("正稿");
  // 全空分支（点了「＋加一条应对」还没填）：serialize 略过,ref 不变——空行只活在组件态。
  const withEmpty = { ...parts, branches: [...parts.branches, { cond: "", resp: "" }] };
  assert.equal(fc.serializeStepRef(withEmpty), "正稿");
  // 只填条件未填应答：残行降级普通行（文字不丢,重解析落 script 段）。
  const condOnly = { ...parts, branches: [{ cond: "怕麻烦", resp: "" }] };
  assert.equal(fc.serializeStepRef(condOnly), "正稿\n怕麻烦");
  // round-trip 仍稳定：parse(serialize(parse(x))) 逐件相等。
  assertRoundTrip("正稿");
});
