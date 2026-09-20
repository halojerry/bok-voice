// lib/gap-proposals.ts 纯函数单测（L-② 话术分支/意图词提案，2026-09-20）：
// 漏网轮分组键与提案归组（proposalGapKey/proposalsForGap）、三条教法路由的
// 可用性与人话原因兜底（branchRouteHint/intentRouteHint）、分支行预览
// （branchLinePreview 与 CP gap_proposals.branch_line 同形）、采纳请求体组装
// （buildBranchAdoptItem/buildIntentAdoptItem：字段逐字回传供服务端键校验）。
//
// 装配照 flow-canvas.test.mjs：lib/gap-proposals.ts 零 import,单文件转译直载。

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

const TMP = mkdtempSync(path.join(WEB_ROOT, ".tmp-gap-proposals-"));
execFileSync(
  process.execPath,
  [TSC, path.join(WEB_ROOT, "lib", "gap-proposals.ts"), "--outDir", TMP, "--module", "commonjs", "--target", "es2020", "--skipLibCheck"],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const gp = require(path.join(TMP, "gap-proposals.js"));

test.after(() => {
  try {
    rmSync(TMP, { recursive: true, force: true });
  } catch {
    /* 临时目录清理失败不影响结论 */
  }
});

// ---- 夹具 ----

function proposal(over = {}) {
  return {
    key: over.key ?? "branch|tpl-1|3|abc",
    kind: over.kind ?? "branch",
    template_id: over.template_id ?? "tpl-1",
    template_name: over.template_name ?? "粤语集运",
    customer_text: over.customer_text ?? "幾時可以送到呀",
    norm: over.norm ?? "幾時可以送到呀",
    count: over.count ?? 4,
    calls: over.calls ?? 3,
    lang: over.lang ?? "cantonese",
    sample_answer: over.sample_answer ?? "一般兩到三日到。",
    sample_call_id: over.sample_call_id ?? "call-1",
    step: over.step ?? 3,
    step_goal: over.step_goal ?? "平台",
    branch_cond: over.branch_cond ?? "幾時可以送到呀",
    branch_resp: over.branch_resp ?? "一般兩到三日到。",
    branch_line: over.branch_line ?? "如果客户幾時可以送到呀→一般兩到三日到。",
    intent_id: over.intent_id ?? "",
    intent_label: over.intent_label ?? "",
    keyword: over.keyword ?? "",
    available: over.available ?? true,
    blocked_reason: over.blocked_reason ?? "",
    blocked_label: over.blocked_label ?? "",
  };
}

// ---- 分组 ----

test("proposalGapKey 与 gap-mining rowKey 同式", () => {
  assert.equal(gp.proposalGapKey("tpl-1", "幾時送到"), "tpl-1|幾時送到");
  assert.equal(gp.proposalGapKey("", "幾時送到"), "|幾時送到");
});

test("proposalsForGap 按(模板|原话)归组且每类取第一条", () => {
  const proposals = [
    proposal({ key: "k1", kind: "branch" }),
    proposal({ key: "k2", kind: "intent_keyword", intent_id: "int_a", keyword: "幾時到" }),
    proposal({ key: "k3", kind: "branch", customer_text: "另一句", branch_cond: "另一句" }),
    proposal({ key: "k4", kind: "intent_keyword", customer_text: "另一句", intent_id: "int_b", keyword: "x" }),
  ];
  const routes = gp.proposalsForGap(proposals, "tpl-1", "幾時可以送到呀");
  assert.equal(routes.branch.key, "k1");
  assert.equal(routes.intent.key, "k2");
  // 其它漏网轮的提案不串组
  const other = gp.proposalsForGap(proposals, "tpl-1", "另一句");
  assert.equal(other.branch.key, "k3");
  // 无提案 → 双 null（UI 走本地兜底文案）
  assert.deepEqual(gp.proposalsForGap([], "tpl-1", "x"), { branch: null, intent: null });
});

// ---- 人话原因兜底 ----

test("branchRouteHint 服务端 blocked_label 优先", () => {
  const blocked = proposal({ available: false, blocked_reason: "branch_exists", blocked_label: "这句话已经在第 3 步的分支里了，不用重复加。" });
  assert.equal(gp.branchRouteHint("tpl-1", blocked), "这句话已经在第 3 步的分支里了，不用重复加。");
});

test("branchRouteHint 无提案时按是否绑模板兜底", () => {
  assert.equal(gp.branchRouteHint("", null), "这通通话没有绑定话术模板，先去「话术」页给这类通话绑定模板。");
  assert.match(gp.branchRouteHint("tpl-1", null), /暂时做不了话术分支/);
});

test("intentRouteHint 同款兜底", () => {
  const blocked = proposal({ kind: "intent_keyword", available: false, blocked_reason: "keyword_covered", blocked_label: "现有意图的关键词已经能接住这句话，不用再加。" });
  assert.equal(gp.intentRouteHint("tpl-1", blocked), "现有意图的关键词已经能接住这句话，不用再加。");
  assert.equal(gp.intentRouteHint("", null), "这通通话没有绑定话术模板，先去「话术」页给这类通话绑定模板。");
});

// ---- 分支行预览与采纳请求体 ----

test("branchLinePreview 与 CP branch_line 同形", () => {
  assert.equal(gp.branchLinePreview("幾時送到", "兩到三日。"), "如果客户幾時送到→兩到三日。");
  assert.equal(gp.branchLinePreview(" 幾時送到 ", " 兩到三日。 "), "如果客户幾時送到→兩到三日。");
});

test("buildBranchAdoptItem 回传键校验材料,text 为人工改后应答", () => {
  const p = proposal();
  const item = gp.buildBranchAdoptItem(p, "  人工改過嘅回答。  ");
  assert.deepEqual(item, {
    key: p.key,
    kind: "branch",
    template_id: "tpl-1",
    norm: p.norm,
    step: 3,
    cond: p.branch_cond,
    text: "人工改過嘅回答。",
    intent_id: "",
  });
});

test("buildIntentAdoptItem 回传意图上下文,text 为人工改后关键词", () => {
  const p = proposal({ kind: "intent_keyword", key: "intent|tpl-1|int_a|abc", intent_id: "int_a", intent_label: "查询时效", keyword: "幾時可以送到呀", step: 0 });
  const item = gp.buildIntentAdoptItem(p, " 幾時送到 ");
  assert.deepEqual(item, {
    key: p.key,
    kind: "intent_keyword",
    template_id: "tpl-1",
    norm: p.norm,
    step: 0,
    cond: "",
    text: "幾時送到",
    intent_id: "int_a",
  });
});

test("step 缺失/非数值时按 0 组装(服务端 400 拦,不在前端伪造)", () => {
  const p = proposal();
  p.step = undefined;
  assert.equal(gp.buildBranchAdoptItem(p, "x").step, 0);
});
