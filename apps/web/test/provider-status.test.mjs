// 容灾可观测波（feat/dr-observability）web 消费面 · Provider 读数判定契约测试。
// 输入=契约 §2/§4 的 mock 样本（逐字取自 docs/DR-WAVE-CONTRACT.md 示例），
// 断言=灯色档位与行文案（Ethan 定稿格式「🟢 已连接 · 412ms (p95 490)」）。
// 纯函数单点 lib/provider-status.ts，经 tsc 单文件转译成 CJS 加载（仓内既有路线：
// test/apiBase.test.mjs，不引新依赖）。

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

const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-provider-status-"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(WEB_ROOT, "lib", "provider-status.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const {
  providerLevel,
  providerValueText,
  famineLine,
  sinceDuration,
  swapLevel,
  LEVEL_DOT,
} = require(path.join(TMP_OUT, "provider-status.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

// 契约 §2 mock（逐字）：window_s/providers/famine 三键
const MOCK = {
  window_s: 300,
  providers: {
    asr: { last_ms: 412, p50: 400, p95: 490, n: 31 },
    llm: { last_ms: 680, p50: 710, p95: 1240, n: 28 },
    tts: { last_ms: 310, p50: 300, p95: 450, n: 40 },
    vad: { last_ms: 18, p50: 18, p95: 22, n: 900 },
  },
  famine: { level: "healthy", ema_s: 0.6, since: null, downgraded: false, dialing_paused: false },
};

test("契约 mock：行文案逐字对齐 Ethan 定稿格式", () => {
  assert.equal(providerValueText(MOCK.providers.asr), "已连接 · 412ms (p95 490)");
  assert.equal(providerValueText(MOCK.providers.llm), "已连接 · 680ms (p95 1240)");
  assert.equal(providerValueText(MOCK.providers.tts), "已连接 · 310ms (p95 450)");
  // VAD 无 p95 段 → 「已连接 · 18ms」（定稿格式同款）
  assert.equal(providerValueText({ last_ms: 18, p50: 18, n: 900 }), "已连接 · 18ms");
});

test("非 LLM：2×p50 黄 / 4×p50 红（严格大于）", () => {
  // 契约 mock 样本：412 vs 2×400=800 → 绿
  assert.equal(providerLevel("asr", MOCK.providers.asr), "green");
  assert.equal(providerLevel("tts", MOCK.providers.tts), "green");
  assert.equal(providerLevel("vad", MOCK.providers.vad), "green");
  // 恰 2× 不黄（> 才黄）；801 → 黄；恰 4× 不红；1601 → 红
  assert.equal(providerLevel("asr", { last_ms: 800, p50: 400 }), "green");
  assert.equal(providerLevel("asr", { last_ms: 801, p50: 400 }), "yellow");
  assert.equal(providerLevel("asr", { last_ms: 1600, p50: 400 }), "yellow");
  assert.equal(providerLevel("asr", { last_ms: 1601, p50: 400 }), "red");
  // p50 缺失/非正=无基线 → 绿（有读数就不灰）
  assert.equal(providerLevel("tts", { last_ms: 9999 }), "green");
  assert.equal(providerLevel("tts", { last_ms: 9999, p50: 0 }), "green");
});

test("LLM：last/p95 任一 >2500 红、1000-2500 黄、<1000 绿", () => {
  // 契约 mock 样本 p95=1240 → 黄（阈值优先于定稿示例的装饰色）
  assert.equal(providerLevel("llm", MOCK.providers.llm), "yellow");
  assert.equal(providerLevel("llm", { last_ms: 680, p95: 950 }), "green");
  assert.equal(providerLevel("llm", { last_ms: 1000, p95: 900 }), "yellow"); // 下边界含
  assert.equal(providerLevel("llm", { last_ms: 2500, p95: 2500 }), "yellow"); // 上边界含
  assert.equal(providerLevel("llm", { last_ms: 900, p95: 2501 }), "red"); // p95 单独越线
  assert.equal(providerLevel("llm", { last_ms: 2600, p95: 900 }), "red"); // last 单独越线
});

test("无数据=灰（n<=0 / last_ms 缺失 / 无 stat）", () => {
  assert.equal(providerLevel("asr", null), "grey");
  assert.equal(providerLevel("asr", undefined), "grey");
  assert.equal(providerLevel("asr", {}), "grey");
  assert.equal(providerLevel("asr", { last_ms: null, p50: 400 }), "grey");
  assert.equal(providerLevel("asr", { last_ms: 412, p50: 400, n: 0 }), "grey");
  assert.equal(providerValueText(null), "已连接");
  assert.equal(LEVEL_DOT.grey, "⚪");
});

test("状态行：healthy/famine/downgraded/dialing_paused 四态 + EMA 1 位小数", () => {
  const healthy = famineLine(MOCK.famine);
  assert.deepEqual(healthy, { dot: "🟢", text: "健康 (EMA 0.6s)", level: "green" });
  const famine = famineLine({ level: "famine", ema_s: 5.13, downgraded: false, dialing_paused: false });
  assert.deepEqual(famine, { dot: "🟡", text: "饥荒中 (EMA 5.1s)", level: "yellow" });
  const down = famineLine({ level: "famine", ema_s: 5.13, downgraded: true, dialing_paused: true });
  assert.deepEqual(down, { dot: "🔴", text: "已降档·停拨 (EMA 5.1s)", level: "red" });
  const paused = famineLine({ level: "healthy", ema_s: 0.2, downgraded: false, dialing_paused: true });
  assert.deepEqual(paused, { dot: "🔴", text: "停拨 (EMA 0.2s)", level: "red" });
  // 无读数不编数字；无 famine 对象=灰「未知」
  assert.equal(famineLine({ level: "healthy" }).text, "健康");
  assert.deepEqual(famineLine(null), { dot: "⚪", text: "未知", level: "grey" });
});

test("持续时长与 swap 档", () => {
  const now = Date.parse("2026-10-01T15:10:00Z");
  assert.equal(sinceDuration("2026-10-01T15:07:47Z", now), "2 分 13 秒");
  assert.equal(sinceDuration("2026-10-01T15:09:30Z", now), "30 秒");
  assert.equal(sinceDuration(null, now), "");
  assert.equal(sinceDuration("not-a-date", now), "");
  // 契约 §4 mock：swap 24.7GB / 阈值 8GB → 红
  assert.equal(swapLevel(24.7, 8), "red");
  assert.equal(swapLevel(3, 8), "green");
  assert.equal(swapLevel(8, 8), "green"); // 严格大于才红
  assert.equal(swapLevel(null, 8), "grey");
});
