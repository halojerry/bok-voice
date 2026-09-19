// 统一链路 Logger 单测（node --test，与 apiBase.test.mjs 同款单文件转译路线）：
//   ① info/warn/error 输出 JSON 行（traceId/时间戳/上下文）
//   ② 同 trace 环形缓存 ≤50 条、5 分钟 TTL 清理、trace 总量上限（防内存膨胀）
//   ③ 敏感信息脱敏（键掩码 + Bearer/token= scrub、非变异、循环/深度/bigint 安全）
//   ④ logger.error → 异步非阻塞自动上报（整条链路 + error stack + 环境/版本/git + 用户/操作）
//   ⑤ 上报失败只本地警告：不递归、不抛错、不阻塞（req7）
//   ⑥ Node 全局兜底：uncaughtException / unhandledRejection → 日志+上报；安装幂等、卸载恢复
//   ⑦ bind/child：异步回调异常记入同 trace 并原样 rethrow；上下文合并覆盖
//
// lib/logger.ts 是 TS 源码，node 不能直跑：tsc CLI 显式文件入参（忽略 tsconfig）转译成
// CJS 产物后经 createRequire 加载——不引新依赖（typescript 本就在 devDependencies）。

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

// ---- 装配：lib/logger.ts → 临时 CJS 产物（模块加载期一次性完成，失败=整文件红）----
const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-logger-"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(WEB_ROOT, "lib", "logger.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const {
  startTrace,
  configureLogger,
  configureReporting,
  createReporter,
  installGlobalHandlers,
  getTraceLogs,
  sweepExpiredTraces,
  redact,
  resetForTests,
  _globalHandlers,
} = require(path.join(TMP_OUT, "logger.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

const flush = () => new Promise((r) => setTimeout(r, 5));

/** 每例隔离：重置单例（store/config/已装处理器/在途上报定时器）+ 注入捕获型 write。 */
function captureWrite() {
  const lines = [];
  configureLogger({ write: (level, line) => lines.push({ level, line }) });
  return lines;
}

test.beforeEach(() => {
  resetForTests();
});

test.afterEach(() => {
  resetForTests();
});

// ---- ① JSON 行输出 ----

test("info 输出 JSON 行，携带 traceId/ISO 时间戳/自定义上下文", () => {
  const lines = captureWrite();
  const log = startTrace({ operation: "checkout", user: { userId: "u-1" }, context: { cart: 3 } });
  log.info("hello", { orderId: 42 });

  assert.equal(lines.length, 1);
  assert.equal(lines[0].level, "info");
  const e = JSON.parse(lines[0].line);
  assert.equal(e.level, "info");
  assert.equal(e.traceId, log.traceId);
  assert.ok(e.traceId.length > 0);
  assert.ok(!Number.isNaN(Date.parse(e.ts)), "ts 必须是 ISO 时间戳");
  assert.equal(e.message, "hello");
  assert.equal(e.context.orderId, 42);
  assert.equal(e.context.cart, 3);
  assert.equal(e.context.userId, "u-1");
});

test("warn/error 同格式；error 条目序列化完整 stack", () => {
  const lines = captureWrite();
  const log = startTrace({ operation: "op" });
  log.warn("w1");
  log.error("boom", new Error("kaboom"), { step: 2 });

  const w = JSON.parse(lines[0].line);
  assert.equal(w.level, "warn");
  const e = JSON.parse(lines[1].line);
  assert.equal(e.level, "error");
  assert.equal(e.error.name, "Error");
  assert.equal(e.error.message, "kaboom");
  assert.match(e.error.stack, /kaboom/);
  assert.equal(e.context.step, 2);
});

test("error 未传 err 时以 message 兜底序列化，不抛错", () => {
  const lines = captureWrite();
  const log = startTrace({ operation: "op" });
  log.error("only-message");
  const e = JSON.parse(lines[0].line);
  assert.equal(e.error.message, "only-message");
});

// ---- ② 环形缓存 / TTL / 总量上限 ----

test("同一 trace 环形缓存上限 50 条：丢最旧、保最新", () => {
  captureWrite();
  const log = startTrace({ operation: "ring" });
  for (let i = 0; i < 60; i++) log.info(`m-${i}`);

  const logs = getTraceLogs(log.traceId);
  assert.equal(logs.length, 50);
  assert.equal(logs[0].message, "m-10");
  assert.equal(logs[49].message, "m-59");
});

test("TTL 过期清理：未过期保留，超 5 分钟清除", () => {
  captureWrite();
  const log = startTrace({ operation: "ttl" });
  log.info("keep");

  assert.equal(sweepExpiredTraces(Date.now()), 0, "刚创建不清理");
  assert.equal(getTraceLogs(log.traceId).length, 1);

  assert.ok(sweepExpiredTraces(Date.now() + 6 * 60 * 1000) >= 1, "超 TTL 清除");
  assert.equal(getTraceLogs(log.traceId), undefined);
});

test("trace 总量上限：挤出最旧 trace，防多链路内存膨胀", () => {
  captureWrite();
  configureLogger({ maxTraces: 2 });
  const a = startTrace({ operation: "a" });
  const b = startTrace({ operation: "b" });
  const c = startTrace({ operation: "c" });

  assert.equal(getTraceLogs(a.traceId), undefined, "最旧被挤出");
  assert.ok(getTraceLogs(b.traceId).length === 0, "b 仍在");
  assert.ok(getTraceLogs(c.traceId).length === 0, "c 仍在");
});

test("getTraceLogs 返回快照：外部改动不影响缓存", () => {
  captureWrite();
  const log = startTrace({ operation: "snap" });
  log.info("one");
  const logs = getTraceLogs(log.traceId);
  assert.equal(logs.length, 1);
  logs.pop();
  assert.equal(getTraceLogs(log.traceId).length, 1, "快照隔离");
});

// ---- ③ 脱敏 ----

test("redact：敏感键掩码、Bearer/token= scrub、非变异", () => {
  const input = {
    password: "hunter2",
    nested: { apiKey: "k-123", keep: 1 },
    arr: [{ token: "t-9" }],
    header: "Bearer abc.def.ghi",
    note: "call api with token=qqq123 now",
    ok: "fine",
  };
  const out = redact(input);

  assert.equal(out.password, "[REDACTED]");
  assert.equal(out.nested.apiKey, "[REDACTED]");
  assert.equal(out.nested.keep, 1);
  assert.equal(out.arr[0].token, "[REDACTED]");
  assert.equal(out.header, "Bearer [REDACTED]");
  assert.ok(out.note.includes("token=[REDACTED]"), `scrub 串联: ${out.note}`);
  assert.ok(!out.note.includes("qqq123"));
  assert.equal(out.ok, "fine");
  // 非变异：调用方对象保持原样
  assert.equal(input.password, "hunter2");
  assert.equal(input.header, "Bearer abc.def.ghi");
});

test("redact：循环引用/超深嵌套/bigint 不炸不漏", () => {
  const cyc = {};
  cyc.self = cyc;
  assert.equal(redact(cyc).self, "[CIRCULAR]");

  assert.equal(redact(1n), "1", "bigint 转 string（JSON.stringify 安全）");
  assert.equal(redact(undefined), undefined);
  assert.equal(redact(null), null);

  let deep = { leaf: "x" };
  for (let i = 0; i < 20; i++) deep = { child: deep };
  const out = redact(deep);
  assert.ok(JSON.stringify(out).includes("TRUNCATED"), "超深截断且可序列化");
});

test("日志上下文进缓存前已脱敏（缓存行与输出行一致）", () => {
  const lines = captureWrite();
  const log = startTrace({ operation: "sec" });
  log.info("m", { password: "p", note: "Bearer zzz.yyy" });

  const cached = getTraceLogs(log.traceId)[0];
  assert.equal(cached.context.password, "[REDACTED]");
  assert.equal(cached.context.note, "Bearer [REDACTED]");
  assert.equal(JSON.parse(lines[0].line).context.password, "[REDACTED]");
});

// ---- ④ error 自动上报 ----

test("logger.error 触发异步上报：traceId+整条链路+stack+环境+版本/git+用户/操作", async () => {
  const lines = captureWrite();
  const reports = [];
  configureLogger({
    version: "v-test",
    gitHash: "abc1234",
    environment: "node",
    reporter: { report: (p) => reports.push(p) },
  });

  const log = startTrace({ traceId: "trc-fixed", operation: "pay", user: { userId: "u9" } });
  log.info("step-1", { qty: 2 });
  log.error("boom", new Error("kaboom"), { step: 2 });
  await flush();

  assert.equal(reports.length, 1, "异步（setTimeout 0）非阻塞上报一次");
  const p = reports[0];
  assert.equal(p.traceId, "trc-fixed");
  assert.equal(p.environment, "node");
  assert.equal(p.version, "v-test");
  assert.equal(p.gitHash, "abc1234");
  assert.deepEqual(p.user, { userId: "u9" });
  assert.equal(p.operation, "pay");
  assert.ok(p.occurredAt);
  assert.ok(p.runtime && typeof p.runtime === "object");

  const errEntry = p.logs.find((e) => e.level === "error");
  assert.ok(errEntry, "链路里含本条 error");
  assert.match(errEntry.error.stack, /kaboom/);
  assert.ok(p.logs.some((e) => e.message === "step-1"), "链路含先前 info（整条链路）");
  assert.equal(p.logs.length, 2);
  assert.equal(lines.length, 2, "上报不替代本地输出");
});

test("createReporter：无 endpoint/transport 时只警告一次不抛错", () => {
  const warns = [];
  const origWarn = console.warn;
  console.warn = (...a) => warns.push(a.map(String).join(" "));
  try {
    const reporter = createReporter({});
    reporter.report({
      traceId: "t", occurredAt: new Date().toISOString(), environment: "node",
      version: "v", gitHash: "h", runtime: {}, error: { name: "Error", message: "m" }, logs: [],
    });
    assert.equal(warns.filter((w) => w.includes("log-reporter")).length, 1, "恰好警告一次（不刷屏）");
  } finally {
    console.warn = origWarn;
  }
});

test("createReporter：默认 transport 走 fetch POST JSON", async () => {
  const calls = [];
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, status: 200 };
  };
  try {
    const reporter = createReporter({ endpoint: "https://cp.example.com/api/logs" });
    reporter.report({
      traceId: "t", occurredAt: "now", environment: "node", version: "v", gitHash: "h",
      runtime: {}, error: { name: "Error", message: "m" }, logs: [{ ts: "t", level: "error", traceId: "t", message: "m" }],
    });
    await flush();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "https://cp.example.com/api/logs");
    assert.equal(calls[0].init.method, "POST");
    const body = JSON.parse(calls[0].init.body);
    assert.equal(body.traceId, "t");
    assert.equal(body.logs.length, 1);
  } finally {
    globalThis.fetch = origFetch;
  }
});

// ---- ⑤ 上报失败隔离 ----

test("上报同步抛错：只本地警告（带堆栈），不递归、不阻塞主流程", async () => {
  let calls = 0;
  const warns = [];
  const origWarn = console.warn;
  console.warn = (...a) => warns.push(a.map(String).join(" "));
  try {
    captureWrite();
    configureLogger({
      reporter: {
        report: () => {
          calls += 1;
          throw new Error("transport-down");
        },
      },
    });
    const log = startTrace({ operation: "f" });
    log.error("e1", new Error("x"));
    await flush();
    await flush();

    assert.equal(calls, 1, "失败不再触发第二次上报（禁递归）");
    assert.ok(warns.some((w) => w.includes("log-reporter")), "本地警告打点");
    assert.ok(warns.some((w) => w.includes("transport-down")), "警告含故障原因");
    // 主业务不受影响：后续日志照常输出
    log.info("still-alive");
    assert.equal(getTraceLogs(log.traceId).some((e) => e.message === "still-alive"), true);
  } finally {
    console.warn = origWarn;
  }
});

test("上报 Promise 拒绝：同样只警告，无 unhandledRejection 逃逸", async () => {
  let calls = 0;
  const warns = [];
  const origWarn = console.warn;
  console.warn = (...a) => warns.push(a.map(String).join(" "));
  try {
    captureWrite();
    configureLogger({
      reporter: {
        report: () => {
          calls += 1;
          return Promise.reject(new Error("async-down"));
        },
      },
    });
    const log = startTrace({ operation: "f2" });
    log.error("e2", new Error("y"));
    await flush();
    await flush();
    assert.equal(calls, 1);
    assert.ok(warns.some((w) => w.includes("async-down")));
  } finally {
    console.warn = origWarn;
  }
});

test("同一 trace 上报限次（默认 5）：错误风暴不打爆上报通道", async () => {
  const reports = [];
  const origWarn = console.warn;
  console.warn = () => {}; // cap 警告走 safeWarn（真实通道），测试内静音保输出干净
  try {
    captureWrite();
    configureLogger({ reporter: { report: (p) => reports.push(p) } });
    const log = startTrace({ operation: "storm" });
    for (let i = 0; i < 7; i++) log.error(`e${i}`, new Error("x"));
    await flush();
    assert.equal(reports.length, 5);
  } finally {
    console.warn = origWarn;
  }
});

// ---- ⑥ 全局兜底 ----

test("configureReporting 一站式装配：上报器 + 全局兜底同时就位", async () => {
  const reports = [];
  captureWrite();
  const beforeU = process.listenerCount("uncaughtException");
  configureReporting({ transport: (p) => reports.push(p) });
  assert.equal(process.listenerCount("uncaughtException"), beforeU + 1, "兜底已挂");

  const log = startTrace({ operation: "boot" });
  log.error("boot-err", new Error("boot"));
  await flush();
  assert.ok(reports.some((p) => p.logs.some((e) => e.error && e.error.message === "boot")), "error 已走上报");
});

test("Node 全局兜底：uncaughtException/unhandledRejection 记日志并上报", async () => {
  const reports = [];
  captureWrite();
  configureLogger({ reporter: { report: (p) => reports.push(p) } });

  const beforeU = process.listenerCount("uncaughtException");
  const beforeR = process.listenerCount("unhandledRejection");
  const uninstall = installGlobalHandlers();
  assert.equal(process.listenerCount("uncaughtException"), beforeU + 1);
  assert.equal(process.listenerCount("unhandledRejection"), beforeR + 1);

  const h = _globalHandlers();
  assert.ok(h && typeof h.uncaughtException === "function" && typeof h.unhandledRejection === "function");
  h.uncaughtException(new Error("fatal-uncaught"));
  h.unhandledRejection(new Error("rejected-promise"));
  await flush();

  assert.ok(reports.some((p) => p.logs.some((e) => e.error && e.error.message === "fatal-uncaught")));
  assert.ok(reports.some((p) => p.logs.some((e) => e.error && e.error.message === "rejected-promise")));
  assert.ok(process.listenerCount("uncaughtException") === beforeU + 1, "兜底捕获后进程不被它带走");

  uninstall();
  assert.equal(process.listenerCount("uncaughtException"), beforeU, "卸载恢复");
  assert.equal(process.listenerCount("unhandledRejection"), beforeR);
});

test("全局兜底重复安装幂等；全局 trace 同样限次上报", async () => {
  const reports = [];
  const origWarn = console.warn;
  console.warn = () => {}; // 同上：cap 警告静音
  try {
    captureWrite();
    configureLogger({ reporter: { report: (p) => reports.push(p) } });
    const beforeU = process.listenerCount("uncaughtException");

    const uninstall1 = installGlobalHandlers();
    const uninstall2 = installGlobalHandlers();
    assert.equal(process.listenerCount("uncaughtException"), beforeU + 1, "幂等：只装一份");

    const h = _globalHandlers();
    for (let i = 0; i < 9; i++) h.uncaughtException(new Error(`fatal-${i}`));
    await flush();
    assert.equal(reports.length, 5, "全局 trace 也吃每-trace 上报限次");

    uninstall2();
    assert.equal(process.listenerCount("uncaughtException"), beforeU, "卸载恢复");
    uninstall1(); // 重复卸载无副作用
    assert.equal(process.listenerCount("uncaughtException"), beforeU);
  } finally {
    console.warn = origWarn;
  }
});

// ---- ⑦ bind / child ----

test("bind：同步+异步回调异常记入同 trace 并原样 rethrow（异常不静默）", async () => {
  const reports = [];
  captureWrite();
  configureLogger({ reporter: { report: (p) => reports.push(p) } });

  const log = startTrace({ operation: "bind" });
  const syncBoom = () => {
    throw new Error("sync-boom");
  };
  assert.throws(log.bind(syncBoom), /sync-boom/);

  const asyncBoom = async () => {
    throw new Error("async-boom");
  };
  await assert.rejects(log.bind(asyncBoom), /async-boom/);

  const errs = getTraceLogs(log.traceId).filter((e) => e.level === "error");
  assert.ok(errs.some((e) => e.error.message === "sync-boom"));
  assert.ok(errs.some((e) => e.error.message === "async-boom"));
  await flush();
  assert.ok(reports.length >= 1, "bind 捕获的异常同样触发上报");
});

test("bind 透传 this/参数与返回值", () => {
  captureWrite();
  const log = startTrace({ operation: "bind2" });
  const fn = function (a, b) {
    return `${this.prefix}:${a + b}`;
  };
  const bound = log.bind(fn);
  assert.equal(bound.call({ prefix: "R" }, 2, 3), "R:5");
});

test("child：继承 traceId，上下文合并且子覆盖父", () => {
  const lines = captureWrite();
  const root = startTrace({ operation: "c", context: { a: 1 } });
  const kid = root.child({ a: 9, b: 2 });
  assert.equal(kid.traceId, root.traceId);
  kid.info("from-kid");

  const e = JSON.parse(lines[0].line);
  assert.equal(e.context.a, 9);
  assert.equal(e.context.b, 2);
  // 父不受影响
  root.info("from-root");
  const e2 = JSON.parse(lines[1].line);
  assert.equal(e2.context.a, 1);
  assert.equal(e2.context.b, undefined);
});

test("startTrace 复用已有 traceId 不清空链路", () => {
  captureWrite();
  const a = startTrace({ traceId: "trc-same", operation: "one" });
  a.info("first");
  const b = startTrace({ traceId: "trc-same", operation: "two" });
  b.info("second");
  const logs = getTraceLogs("trc-same");
  assert.equal(logs.length, 2, "同 traceId 二次 startTrace 续链不清零");
});
