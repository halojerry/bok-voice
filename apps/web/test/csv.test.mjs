// lib/csv.ts 纯函数单测（表格导入 2026-09-25）：BOM 剥除、引号转义/字段内逗号换行、
// CRLF/LF/CR 行界、全空行丢弃、注释行判定、escape/toCsv 往返、多值格与布尔格解析。
//
// 装配照 flow-canvas.test.mjs：lib/csv.ts 零 import，单文件 tsc 转译直载。

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

const TMP = mkdtempSync(path.join(WEB_ROOT, ".tmp-csv-"));
execFileSync(
  process.execPath,
  [TSC, path.join(WEB_ROOT, "lib", "csv.ts"), "--outDir", TMP, "--module", "commonjs", "--target", "es2020", "--skipLibCheck"],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const csv = require(path.join(TMP, "csv.js"));

test("parseCsv：基础逗号分隔 + 末行无换行照收", () => {
  assert.deepEqual(csv.parseCsv("a,b,c\n1,2,3"), [["a", "b", "c"], ["1", "2", "3"]]);
});

test("parseCsv：UTF-8 BOM（Excel 导出）剥除", () => {
  assert.deepEqual(csv.parseCsv("\uFEFF问法,回答\n甲,乙"), [["问法", "回答"], ["甲", "乙"]]);
});

test("parseCsv：引号字段内逗号/换行/转义双引号", () => {
  const text = '问法,回答\n"含,逗号","第一行\n第二行"\n"说""你好""" ,尾随';
  assert.deepEqual(csv.parseCsv(text), [
    ["问法", "回答"],
    ["含,逗号", "第一行\n第二行"],
    ['说"你好"', "尾随"],
  ]);
});

test("parseCsv：CRLF / LF / CR 行界一致", () => {
  const lf = csv.parseCsv("a,b\nc,d");
  assert.deepEqual(csv.parseCsv("a,b\r\nc,d"), lf);
  assert.deepEqual(csv.parseCsv("a,b\rc,d"), lf);
});

test("parseCsv：全空行丢弃；空字段保留", () => {
  assert.deepEqual(csv.parseCsv("a,,c\n\n  \nd,,"), [["a", "", "c"], ["d", "", ""]]);
});

test("parseCsv：空输入返回空表", () => {
  assert.deepEqual(csv.parseCsv(""), []);
  assert.deepEqual(csv.parseCsv("\n\n"), []);
});

test("isCommentRow：首格 # 开头为注释，字段内 # 不算", () => {
  assert.equal(csv.isCommentRow(["# 列说明：问法,回答"]), true);
  assert.equal(csv.isCommentRow(["  #缩进也算"]), true);
  assert.equal(csv.isCommentRow(["问法", "#备注"]), false);
  assert.equal(csv.isCommentRow(["", "#第二格"]), false);
});

test("csvEscape/toCsv：含逗号引号换行才加引号，CRLF 行界 + BOM 可选", () => {
  assert.equal(csv.csvEscape("plain"), "plain");
  assert.equal(csv.csvEscape('a,b"c\nd'), '"a,b""c\nd"');
  const out = csv.toCsv([["问法", "回,答"], ["x", "y"]], { bom: true });
  assert.equal(out.charCodeAt(0), 0xfeff);
  assert.ok(out.endsWith("x,y\r\n"));
  assert.ok(out.includes('"回,答"'));
});

test("parseCsv ↔ toCsv 往返：引号/逗号/换行字段无损", () => {
  const rows = [["问法", "回答"], ['说"你好",吗', "第一行\n第二行"]];
  assert.deepEqual(csv.parseCsv(csv.toCsv(rows)), rows);
});

test("splitListCell：逗号/顿号/分号/换行分隔，去空去重保序", () => {
  assert.deepEqual(csv.splitListCell("投诉;我要投诉，退款、\n找主管;;投诉"), [
    "投诉",
    "我要投诉",
    "退款",
    "找主管",
  ]);
  assert.deepEqual(csv.splitListCell(""), []);
});

test("parseBoolCell：是/否/true/false/1/0/空档，其它报 null", () => {
  assert.equal(csv.parseBoolCell("是", false), true);
  assert.equal(csv.parseBoolCell("否", true), false);
  assert.equal(csv.parseBoolCell("", true), true);
  assert.equal(csv.parseBoolCell("YES", false), true);
  assert.equal(csv.parseBoolCell("0", true), false);
  assert.equal(csv.parseBoolCell("也许", true), null);
});

// 清理临时转译目录
import { rmSync as rm } from "node:fs";
rm(TMP, { recursive: true, force: true });
