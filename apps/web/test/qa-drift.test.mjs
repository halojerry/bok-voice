// lib/qa-drift.ts 纯函数单测（L-③ 在库快答词条体检，2026-09-20）：
// 提案键（与 CP proposal_key 同式）、种类徽标人话、报告摘要容错、采纳项组装
// （字段逐字回传供服务端键校验）、补录音失败判定、采纳结果四态人话文案
// （幂等 / 待管理员补录 / 补录音失败 / 成功）。
//
// 装配照 gap-proposals.test.mjs：lib/qa-drift.ts 零 import，用真 tsc 单文件转译直载。

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

const TMP = mkdtempSync(path.join(WEB_ROOT, ".tmp-qa-drift-"));
execFileSync(
  process.execPath,
  [TSC, path.join(WEB_ROOT, "lib", "qa-drift.ts"), "--outDir", TMP, "--module", "commonjs", "--target", "es2020", "--skipLibCheck"],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const qd = require(path.join(TMP, "qa-drift.js"));

test.after(() => {
  rmSync(TMP, { recursive: true, force: true });
});

const P = {
  key: "reanswer|qa:1",
  kind: "reanswer",
  reason: "repeat_after_play",
  qa_id: "qa:1",
  question_text: "幾時送到",
  current_answer: "兩到三日。",
  suggested_answer: "大概三日內到。",
  lang: "zh",
  scope: "global",
  occurrences: 3,
  fired: 3,
  repeats: 2,
  hits: 5,
  headline: "这段话客户问过 3 次，其中 2 次是听完快速回答又问了一遍。",
  detail: "说明那句回答没答到点上。",
};

test("driftKey 镜像 CP：kind|qa_id", () => {
  assert.equal(qd.driftKey("reanswer", "qa:1"), "reanswer|qa:1");
  assert.equal(qd.driftKey("retire", "qa:abc"), "retire|qa:abc");
  assert.equal(qd.driftKey("retire", ""), "retire|");
});

test("driftKindLabel 用运营看得懂的话，不出现术语", () => {
  assert.equal(qd.driftKindLabel("reanswer"), "建议改答案");
  assert.equal(qd.driftKindLabel("retire"), "建议删掉");
  for (const s of [qd.driftKindLabel("reanswer"), qd.driftKindLabel("retire")]) {
    assert.ok(!/漂移|失效|drift/i.test(s), `不该出现术语：${s}`);
  }
});

test("driftSummary 容错缺字段/空报告", () => {
  const s = qd.driftSummary({ window_calls: 12, entries_scanned: 8, counts: { reanswer: 2, retire: 1 } });
  assert.deepEqual(s, { windowCalls: 12, scanned: 8, reanswer: 2, retire: 1, total: 3 });
  assert.deepEqual(qd.driftSummary(null), { windowCalls: 0, scanned: 0, reanswer: 0, retire: 0, total: 0 });
  assert.equal(qd.driftSummary({}).total, 0);
});

test("buildReanswerItem：text 取人工改后的值并 trim，键与 id 原样回传", () => {
  const item = qd.buildReanswerItem(P, "  三日內會到。  ");
  assert.deepEqual(item, {
    key: "reanswer|qa:1", kind: "reanswer", qa_id: "qa:1", text: "三日內會到。",
  });
  // 空文本不静默兜底（交服务端 400 拒，避免悄悄写空答案）
  assert.equal(qd.buildReanswerItem(P, "").text, "");
});

test("buildRetireItem：无文本，键按 retire 前缀（与提案键一致才过服务端键校验）", () => {
  const retire = { ...P, kind: "retire", key: "retire|qa:1" };
  assert.deepEqual(qd.buildRetireItem(retire), {
    key: "retire|qa:1", kind: "retire", qa_id: "qa:1", text: "",
  });
});

test("pregenFailed：failed/error 为真，queued/缺失为假", () => {
  assert.equal(qd.pregenFailed({ status: "failed" }), true);
  assert.equal(qd.pregenFailed({ status: "error" }), true);
  assert.equal(qd.pregenFailed({ status: "queued" }), false);
  assert.equal(qd.pregenFailed({}), false);
  assert.equal(qd.pregenFailed(null), false);
});

test("driftResultMessage 四态互斥：幂等 / 待管理员补录 / 补录失败 / 成功", () => {
  const idle = qd.driftResultMessage({ kind: "reanswer", created: false, needs_pregen: false, pregen: null, detail: "" });
  assert.match(idle, /没有重复改/);
  assert.match(
    qd.driftResultMessage({ kind: "retire", created: false, needs_pregen: false, pregen: null, detail: "" }),
    /已经删过/,
  );
  const waiting = qd.driftResultMessage({ kind: "reanswer", created: true, needs_pregen: true, pregen: null, detail: "" });
  assert.match(waiting, /管理员/);
  assert.match(waiting, /录音/);
  const failed = qd.driftResultMessage({ kind: "reanswer", created: true, needs_pregen: false, pregen: { status: "failed" }, detail: "" });
  assert.match(failed, /没成功/);
  assert.ok(!/正在重新录音/.test(failed), "补录音失败不能说成正在录音");
  assert.match(
    qd.driftResultMessage({ kind: "reanswer", created: true, needs_pregen: false, pregen: { status: "queued" }, detail: "" }),
    /正在重新录音/,
  );
  assert.match(
    qd.driftResultMessage({ kind: "retire", created: true, needs_pregen: false, pregen: null, detail: "" }),
    /已删掉/,
  );
  assert.equal(qd.driftResultMessage(undefined), "已处理。");
  assert.equal(qd.driftResultMessage(null), "已处理。");
});

test("driftResultMessage 优先级：needs_pregen 先于 pregen 字段", () => {
  // 非 admin 分支拿不到 pregen(恒 null)，但即便给了也以 needs_pregen 为准
  const msg = qd.driftResultMessage({
    kind: "reanswer", created: true, needs_pregen: true, pregen: { status: "queued" }, detail: "",
  });
  assert.match(msg, /管理员/);
});
