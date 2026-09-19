// weblog 自保打点契约（2026-09-18 静默 catch 收编）：
//   weblog 是诊断上报通道，失败走 console.warn 的 JSON 行（channel=weblog），
//   **绝不 import logger**——日志上报器的失败再走 logger.error 会递归上报
//   （lib/logger.ts safeWarn 同款自保语义）。本测试钉住三条路径：
//   ① fetch 拒绝（网络断/CP 下线）→ 打点一次且不向外抛；② fetch 同步抛错
//   （SSR/离线极端场景）→ 同上；③ 上报成功 → 零打点、call_id 绑定正确。
//
// weblog.ts 引用 "@/lib/api"，tsc CLI 显式文件入参不读 tsconfig paths——
// 沿用 test/apiBase.test.mjs 的单文件转译路线：把别名改写为 "./api" 后连
// api.ts 一起复制进临时目录转译成 CJS，再 createRequire 加载（不引新依赖）。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");

// ---- 装配：weblog.ts（别名改写）+ api.ts → 临时 CJS 产物（加载期一次性）----
const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-weblog-"));
const SRC = path.join(TMP_OUT, "src");
mkdirSync(SRC);
writeFileSync(
  path.join(SRC, "weblog.ts"),
  readFileSync(path.join(WEB_ROOT, "lib", "weblog.ts"), "utf8").replace('"@/lib/api"', '"./api"'),
);
writeFileSync(path.join(SRC, "api.ts"), readFileSync(path.join(WEB_ROOT, "lib", "api.ts"), "utf8"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(SRC, "weblog.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const { wlog, wlogBindCall } = require(path.join(TMP_OUT, "weblog.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

// ---- 每例隔离：console.warn spy + fetch stub 还原 + 摘 window ----
const origWarn = console.warn;
const origFetch = globalThis.fetch;
let warns = [];

test.beforeEach(() => {
  warns = [];
  console.warn = (...args) => {
    warns.push(args.map(String).join(" "));
  };
  delete globalThis.window;
});

test.afterEach(() => {
  console.warn = origWarn;
  globalThis.fetch = origFetch;
  delete globalThis.window;
});

test("fetch 拒绝 → console.warn 打点一次（channel=weblog），wlog 不向外抛", async () => {
  globalThis.fetch = () => Promise.reject(new TypeError("load failed"));
  assert.doesNotThrow(() => wlog("sink_test", { ok: true }));
  await new Promise((r) => setTimeout(r, 20)); // 让 rejection 走完 .catch
  assert.equal(warns.length, 1, `期望恰好一条打点，实得 ${warns.length}`);
  const line = JSON.parse(warns[0]);
  assert.equal(line.channel, "weblog");
  assert.equal(line.level, "warn");
  assert.equal(line.event, "sink_test");
  assert.match(line.message, /weblog post failed/);
  assert.equal(line.error.name, "TypeError");
  assert.equal(line.error.message, "load failed");
});

test("fetch 同步抛错（SSR/离线极端场景）→ console.warn 打点一次，wlog 不向外抛", () => {
  globalThis.fetch = () => {
    throw new Error("no fetch in this runtime");
  };
  assert.doesNotThrow(() => wlog("devices"));
  assert.equal(warns.length, 1);
  const line = JSON.parse(warns[0]);
  assert.equal(line.channel, "weblog");
  assert.match(line.message, /weblog post threw/);
  assert.equal(line.error.name, "Error");
});

test("上报成功 → 零打点；call_id 绑定随事件与失败行一致携带", async () => {
  wlogBindCall("call-weblog-test");
  let posted = null;
  globalThis.fetch = (_url, init) => {
    posted = JSON.parse(init.body);
    return Promise.resolve({ ok: true });
  };
  wlog("me_connected");
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(warns.length, 0, "成功路径不允许打点");
  assert.equal(posted.event, "me_connected");
  assert.equal(posted.call_id, "call-weblog-test");

  // 失败行携带同一 call_id（排障时能把失败对回会话）
  globalThis.fetch = () => Promise.reject(new Error("boom"));
  wlog("held", { on: true });
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(warns.length, 1);
  assert.equal(JSON.parse(warns[0]).call_id, "call-weblog-test");
});
