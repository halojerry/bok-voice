// lib/intent-table.ts 纯函数单测（P2.5 意图表单化，2026-09-21）：
//  · 圆整不变量：手工 graph_json（1 常规意图 + 1 "*" 兜底）→ 行 → 保存 = **字节级不变**；
//  · 有改动保存：未变项原样复用（键序/字节不动）、兜底绑定 once 强制 false、删意图连绑定丢；
//  · 契约助手：genGraphId 形状（CP `_ID_RE`）、关键词/步号解析、id 合法性、判据长度。
//
// 装配照 qa-canvas.test.mjs：lib/intent-table.ts 相对 import ./qa-canvas，单次 tsc 转译
// 会把两个文件一并发射到临时目录，createRequire 加载即可（零新依赖）。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");

// ---- 装配：lib/intent-table.ts（连带 ./qa-canvas）→ 临时 CJS 产物 ----
const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-intent-table-"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(WEB_ROOT, "lib", "intent-table.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const it = require(path.join(TMP_OUT, "intent-table.js"));
const qa = require(path.join(TMP_OUT, "qa-canvas.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

// 手工构造的规范图：1 常规意图（带判据）+ 1 兜底意图（keywords 空、once false）。
const FIXTURE = JSON.stringify({
  version: 1,
  intents: [
    {
      id: "session_refuse",
      label: "拒绝",
      keywords: ["不用了", "唔使"],
      steps: [],
      enabled: true,
      judge: { prompt: "客户明确拒绝才算" },
    },
    { id: "*", label: "兜底", keywords: [], steps: [], enabled: true },
  ],
  bindings: [
    { id: "bnd_0a1b2c3d", intent: "session_refuse", priority: 10, once: true, enabled: true, action: "notify_human" },
    { id: "bnd_1a2b3c4d", intent: "*", priority: 10, once: false, enabled: true, action: "jump_step", step: 2 },
  ],
});

test("round-trip：未改动 → 原字节返回（规范串）", () => {
  const rows = it.rowsFromDoc(qa.parseGraphDoc(FIXTURE));
  assert.equal(it.saveIntentDoc(FIXTURE, rows), FIXTURE);
});

test("round-trip：未改动 → 原字节返回（缩进/空白也保真）", () => {
  const pretty = JSON.stringify(JSON.parse(FIXTURE), null, 2);
  const rows = it.rowsFromDoc(qa.parseGraphDoc(pretty));
  assert.equal(it.saveIntentDoc(pretty, rows), pretty);
});

test("round-trip：行对象换新但内容不变（弹窗「打开即保存」路径）→ 原字节", () => {
  const rows = it.rowsFromDoc(qa.parseGraphDoc(FIXTURE));
  // 模拟 UI：浅拷贝整表 + 被编辑行「重建为内容相同的新对象」。
  const copied = rows.map((r) => ({ ...r, bindings: r.bindings.map((b) => ({ ...b })) }));
  assert.equal(it.saveIntentDoc(FIXTURE, copied), FIXTURE);
});

test("rowsFromDoc：兜底行 keywords 空、绑定 once=false（引擎形状）", () => {
  const rows = it.rowsFromDoc(qa.parseGraphDoc(FIXTURE));
  assert.deepEqual(rows.map((r) => r.id), ["session_refuse", "*"]);
  const catchall = rows.find((r) => r.id === "*");
  assert.deepEqual(catchall.keywords, []);
  assert.equal(catchall.bindings[0].once, false);
  assert.equal(catchall.bindings[0].intent, undefined); // 行不带 intent（归属由行 id 决定）
  const normal = rows.find((r) => r.id === "session_refuse");
  assert.deepEqual(normal.keywords, ["不用了", "唔使"]);
  assert.equal(normal.judge, "客户明确拒绝才算");
  assert.equal(normal.bindings[0].action, "notify_human");
});

test("有改动保存：未变项对象逐字节复用、顺序保持", () => {
  const doc = qa.parseGraphDoc(FIXTURE);
  const rows = it.rowsFromDoc(doc);
  const idx = rows.findIndex((r) => r.id === "session_refuse");
  rows[idx] = { ...rows[idx], label: "明确拒绝" };
  const out = it.saveIntentDoc(FIXTURE, rows);
  const parsed = JSON.parse(out);
  assert.equal(parsed.intents[0].label, "明确拒绝");
  // 未改的兜底意图逐字节同原串（键序/内容不动）。
  assert.equal(JSON.stringify(parsed.intents[1]), JSON.stringify(JSON.parse(FIXTURE).intents[1]));
  // 未改的绑定逐字节同原串。
  assert.equal(JSON.stringify(parsed.bindings[1]), JSON.stringify(JSON.parse(FIXTURE).bindings[1]));
});

test("兜底绑定 once 强制 false（即使行里写 true）", () => {
  const doc = qa.parseGraphDoc(FIXTURE);
  const rows = it.rowsFromDoc(doc);
  const c = rows.find((r) => r.id === "*");
  c.bindings[0] = { ...c.bindings[0], once: true }; // 脏值
  const parsed = JSON.parse(it.saveIntentDoc(FIXTURE, rows));
  const b = parsed.bindings.find((x) => x.intent === "*");
  assert.equal(b.once, false);
});

test("新增兜底绑定：追加且 intent=* / once=false", () => {
  const doc = qa.parseGraphDoc(FIXTURE);
  const rows = it.rowsFromDoc(doc);
  const c = rows.find((r) => r.id === "*");
  c.bindings = [...c.bindings, { id: it.genGraphId("bnd_"), action: "notify_human", qa_id: "", step: 1, then_jump: 0, priority: 10, once: false, enabled: true }];
  const parsed = JSON.parse(it.saveIntentDoc(FIXTURE, rows));
  const added = parsed.bindings.filter((x) => x.intent === "*");
  assert.equal(added.length, 2);
  assert.equal(added[1].action, "notify_human");
  assert.equal(added[1].once, false);
  assert.match(added[1].id, /^bnd_[0-9a-f]{8}$/);
});

test("删除意图连同其绑定一并消失（不留悬空）", () => {
  const doc = qa.parseGraphDoc(FIXTURE);
  const rows = it.rowsFromDoc(doc).filter((r) => r.id !== "session_refuse");
  const parsed = JSON.parse(it.saveIntentDoc(FIXTURE, rows));
  assert.deepEqual(parsed.intents.map((i) => i.id), ["*"]);
  assert.deepEqual(parsed.bindings.map((b) => b.intent), ["*"]);
});

test("新增常规意图：追加在末尾、字段规范化", () => {
  const doc = qa.parseGraphDoc(FIXTURE);
  const rows = it.rowsFromDoc(doc);
  rows.push({
    id: "whatsapp_contact",
    label: "给微信",
    keywords: ["微信", "加我"],
    keywordsText: "微信，加我",
    steps: [4],
    stepsText: "4",
    judge: "",
    enabled: true,
    bindings: [
      { id: it.genGraphId("bnd_"), action: "play_qa", qa_id: "qa-1", step: 1, then_jump: 4, priority: 10, once: true, enabled: true },
    ],
  });
  const parsed = JSON.parse(it.saveIntentDoc(FIXTURE, rows));
  assert.equal(parsed.intents.length, 3);
  assert.equal(parsed.intents[2].id, "whatsapp_contact");
  assert.deepEqual(parsed.intents[2].keywords, ["微信", "加我"]);
  assert.deepEqual(parsed.intents[2].steps, [4]);
  const b = parsed.bindings.find((x) => x.intent === "whatsapp_contact");
  assert.equal(b.action, "play_qa");
  assert.equal(b.qa_id, "qa-1");
  assert.equal(b.then_jump, 4);
});

test("契约助手：genGraphId 形状 / parseKeywords / parseStepsText / id 合法性 / 判据长度", () => {
  assert.match(it.genGraphId("int_"), /^int_[0-9a-f]{8}$/);
  assert.match(it.genGraphId("bnd_"), /^bnd_[0-9a-f]{8}$/);
  assert.deepEqual(it.parseKeywords("不用了, 唔使、别打了\n再想想"), ["不用了", "唔使", "别打了", "再想想"]);
  assert.deepEqual(it.parseKeywords("重复，重复"), ["重复"]);
  assert.deepEqual(it.parseStepsText("1,3"), [1, 3]);
  assert.deepEqual(it.parseStepsText(""), []);
  assert.deepEqual(it.parseStepsText("abc"), []);
  assert.equal(it.isIntentIdValid("whatsapp_contact"), true);
  assert.equal(it.isIntentIdValid("Session-Refuse"), false);
  assert.equal(it.isIntentIdValid("1abc"), false);
  assert.equal(it.isCatchallId("*"), true);
  assert.equal(it.isCatchallId("session_refuse"), false);
  assert.equal(it.isJudgeTooLong("x".repeat(it.JUDGE_PROMPT_MAX_CHARS)), false);
  assert.equal(it.isJudgeTooLong("x".repeat(it.JUDGE_PROMPT_MAX_CHARS + 1)), true);
});
