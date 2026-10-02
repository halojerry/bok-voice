// 实时日志抽屉（DR 波契约 §5/§6）· 追加/截断与贴底判定契约测试。
// 钉两条验收硬判据：①上限最近 N 行（超出丢头部，id 只增不回退）；
// ②用户上滚=不贴底（暂停自动滚）、回底（≤eps）恢复。

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

const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-live-logs-"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(WEB_ROOT, "lib", "live-logs.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const { appendLogLines, isNearBottom } = require(path.join(TMP_OUT, "live-logs.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

test("追加：id 递增、文本原样（空行保留一位）", () => {
  const r1 = appendLogLines([], ["l1", "l2"], 0, 500);
  assert.deepEqual(r1.lines, [{ id: 1, text: "l1" }, { id: 2, text: "l2" }]);
  assert.equal(r1.nextId, 2);
  const r2 = appendLogLines(r1.lines, [""], r1.nextId, 500);
  assert.deepEqual(r2.lines.map((l) => l.text), ["l1", "l2", ""]);
  assert.equal(r2.nextId, 3);
});

test("上限最近 500 行：超出丢头部，id 继续递增不回退", () => {
  const batch = Array.from({ length: 300 }, (_, i) => `a${i}`);
  const r1 = appendLogLines([], batch, 0, 500);
  assert.equal(r1.lines.length, 300);
  const r2 = appendLogLines(r1.lines, Array.from({ length: 300 }, (_, i) => `b${i}`), r1.nextId, 500);
  assert.equal(r2.lines.length, 500);
  assert.equal(r2.lines[0].text, "a100"); // 头部丢 100 行
  assert.equal(r2.lines[499].text, "b299");
  assert.equal(r2.lines[499].id, 600);
  // 空批不改变内容也不改 nextId
  const r3 = appendLogLines(r2.lines, [], r2.nextId, 500);
  assert.equal(r3.lines.length, 500);
  assert.equal(r3.nextId, 600);
});

test("贴底判定：用户上滚=暂停自动滚，回底恢复", () => {
  // 视图 1000 高、内容 2000：滚到 1000（底）→ 贴底
  assert.equal(isNearBottom(2000, 1000, 1000), true);
  // 滚到 500（中部）→ 不贴底（暂停自动滚）
  assert.equal(isNearBottom(2000, 500, 1000), false);
  // 距底 24px 容差内仍算贴底（抖动保护）
  assert.equal(isNearBottom(2000, 976, 1000), true);
  assert.equal(isNearBottom(2000, 975, 1000), false);
  // 内容不足一屏（不滚动）=贴底
  assert.equal(isNearBottom(400, 0, 1000), true);
  // 非数字（jsdom/SSR 缺布局）=保守贴底
  assert.equal(isNearBottom(undefined, undefined, undefined), true);
});
