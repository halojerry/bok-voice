"use client";

/**
 * 统一链路 Logger（2026-09-18）：浏览器 / Node 双端通用，零运行时依赖。
 *
 * 能力：
 *  - info/warn/error：每条日志携带 traceId + ISO 时间戳 + 自定义上下文，输出 JSON 行。
 *  - startTrace()：生成 traceId 并返回绑定该链路的 logger 实例；业务入口统一从此开始。
 *  - 同 trace 环形缓存 ≤50 条（丢最旧）；trace 空闲 5 分钟自动清理；trace 总量上限，
 *    防长时间运行内存持续上涨。
 *  - 输出前敏感信息脱敏：敏感键掩码 + Bearer/token= 字符串 scrub；非变异、循环/超深/bigint 安全。
 *  - 全局兜底异常捕获：浏览器 window.onerror / unhandledrejection；
 *    Node uncaughtException / unhandledRejection——异常不静默，必有日志 + 堆栈。
 *  - logger.error 触发异步非阻塞自动上报：traceId、整条链路日志、error message+stack、
 *    环境标识、版本/git hash、用户与操作上下文。
 *  - 上报逻辑独立 try-catch：失败只本地警告（带堆栈），禁止递归触发上报，不阻塞主业务。
 *
 * 业务侧契约：
 *  - 业务入口一律 `const log = startTrace({ operation, user })`；
 *  - 异步回调 / 定时器 / Promise 手动传递 logger（闭包捕获），或经 `log.bind(fn)` 包装——
 *    bind 会把回调内同步/异步异常记入同 trace 并**原样 rethrow**（记日志但不吞异常）；
 *  - 重要分支、提前 return 必须打日志，不允许静默返回；
 *  - catch 一律 `log.error(message, err, ctx)` 记录完整 stack，需要上层感知时继续 throw。
 *
 * 内部自保（防递归上报，需求 7 的唯一豁免口）：logger/reporter 自身故障只走
 * console.warn 显式 JSON 打点（channel=log-reporter，含堆栈——非静默吞）。此路径绝不回调
 * logger.error，否则上报器故障会再次触发上报形成递归。
 */

export type LogLevel = "info" | "warn" | "error";

export type LogContext = Record<string, unknown>;

export interface SerializedError {
  name: string;
  message: string;
  stack?: string;
  cause?: SerializedError;
}

export interface LogEntry {
  ts: string;
  level: LogLevel;
  traceId: string;
  message: string;
  context?: LogContext;
  error?: SerializedError;
}

export interface ErrorReportPayload {
  traceId: string;
  occurredAt: string;
  environment: string;
  version: string;
  gitHash: string;
  user?: LogContext;
  operation?: string;
  runtime: Record<string, unknown>;
  error: SerializedError;
  /** 当前 trace 整条链路日志（含触发上报的这条 error）。 */
  logs: LogEntry[];
}

export interface LoggerReporter {
  report(payload: ErrorReportPayload): unknown;
}

// ---- 常量 ----

const DEFAULT_MAX_LOGS_PER_TRACE = 50;
const DEFAULT_TRACE_TTL_MS = 5 * 60 * 1000;
const SWEEP_INTERVAL_MS = 60 * 1000;
const DEFAULT_MAX_TRACES = 500;
const DEFAULT_MAX_REPORTS_PER_TRACE = 5;
const MAX_REDACT_DEPTH = 6;
const MAX_REDACT_ARRAY = 100;
const MAX_ERROR_CAUSE_DEPTH = 3;
const REDACTED = "[REDACTED]";
const GLOBAL_TRACE_ID = "trc-global-fallback";

/** 命中即整值掩码的敏感键（大小写不敏感，子串匹配）。 */
const SENSITIVE_KEY_RE =
  /(pass(word|wd)?|pwd|secret|token|api[-_]?key|apikey|authorization|auth|cookie|credential|private[-_]?key|access[-_]?key|cvv|cvc|card[-_]?num(ber)?|credit[-_]?card|ssn|id[-_]?number|phone|mobile|email)/i;

const BEARER_RE = /\b(bearer|basic)\s+[\w.~+/=-]{6,}/gi;
const KEY_VALUE_SECRET_RE =
  /\b(api[-_]?key|token|access[-_]?token|secret)(\s*[:=]\s*)[\w.~+/=-]{6,}/gi;

// ---- 脱敏 ----

function scrubString(s: string): string {
  return s
    .replace(BEARER_RE, (_m, scheme: string) => `${scheme} ${REDACTED}`)
    .replace(KEY_VALUE_SECRET_RE, (_m, key: string, sep: string) => `${key}${sep}${REDACTED}`);
}

/**
 * 深度脱敏：敏感键整值掩码、字符串 scrub；非变异（返回新结构）；
 * 循环引用 → [CIRCULAR]，超深 → 截断标记，数组超长截断，bigint 转字符串（JSON 安全）。
 */
export function redact(value: unknown, depth = 0, seen?: WeakSet<object>): unknown {
  if (value === null || value === undefined) return value;
  const t = typeof value;
  if (t === "string") return scrubString(value as string);
  if (t === "number" || t === "boolean") return value;
  if (t === "bigint") return String(value);
  if (t === "function" || t === "symbol") return `[${t}]`;
  if (value instanceof Error) return serializeError(value);
  if (depth >= MAX_REDACT_DEPTH) return "[TRUNCATED_MAX_DEPTH]";
  const obj = value as Record<string, unknown>;
  const seenSet = seen ?? new WeakSet<object>();
  if (seenSet.has(obj)) return "[CIRCULAR]";
  seenSet.add(obj);
  if (Array.isArray(value)) {
    const out: unknown[] = value
      .slice(0, MAX_REDACT_ARRAY)
      .map((v) => redact(v, depth + 1, seenSet));
    if (value.length > MAX_REDACT_ARRAY) out.push(`[+${value.length - MAX_REDACT_ARRAY} more]`);
    return out;
  }
  const out: LogContext = {};
  for (const [k, v] of Object.entries(obj)) {
    out[k] = SENSITIVE_KEY_RE.test(k) ? REDACTED : redact(v, depth + 1, seenSet);
  }
  return out;
}

/** 错误对象 → 可序列化结构（name/message/scrub 后的 stack/cause 链限深）。 */
export function serializeError(err: unknown, depth = 0): SerializedError {
  if (err === null || err === undefined || typeof err !== "object") {
    return { name: "Error", message: String(err) };
  }
  const e = err as { name?: unknown; message?: unknown; stack?: unknown; cause?: unknown };
  const out: SerializedError = {
    name: typeof e.name === "string" ? e.name : "Error",
    message: typeof e.message === "string" ? e.message : String(err),
    stack: typeof e.stack === "string" ? scrubString(e.stack) : undefined,
  };
  if (depth < MAX_ERROR_CAUSE_DEPTH && e.cause !== undefined && e.cause !== null) {
    out.cause = serializeError(e.cause, depth + 1);
  }
  return out;
}

// ---- 配置与存储 ----

interface LoggerConfig {
  environment: string;
  version: string;
  gitHash: string;
  maxLogsPerTrace: number;
  traceTtlMs: number;
  maxTraces: number;
  maxReportsPerTrace: number;
  write: (level: LogLevel, line: string) => void;
  reporter: LoggerReporter | null;
}

export interface ConfigureLoggerOptions {
  environment?: string;
  version?: string;
  gitHash?: string;
  maxLogsPerTrace?: number;
  traceTtlMs?: number;
  maxTraces?: number;
  maxReportsPerTrace?: number;
  write?: (level: LogLevel, line: string) => void;
  /** null = 显式清空上报器；undefined = 保持现状。 */
  reporter?: LoggerReporter | null;
}

function defaultWrite(level: LogLevel, line: string): void {
  if (level === "error") console.error(line);
  else if (level === "warn") console.warn(line);
  else console.log(line);
}

function detectEnvironment(): string {
  const g = globalThis as { window?: { document?: unknown } };
  return g.window && typeof g.window.document !== "undefined" ? "browser" : "node";
}

function defaultVersion(): string {
  const g = globalThis as { __BOK_CONFIG__?: { version?: string } };
  if (g.__BOK_CONFIG__?.version) return g.__BOK_CONFIG__.version;
  const p = globalThis as { process?: { env?: Record<string, string | undefined> } };
  if (p.process?.env) return p.process.env.BOK_APP_VERSION || "unknown";
  return "unknown";
}

function defaultGitHash(): string {
  const g = globalThis as { __BOK_CONFIG__?: { gitHash?: string } };
  if (g.__BOK_CONFIG__?.gitHash) return g.__BOK_CONFIG__.gitHash;
  const p = globalThis as { process?: { env?: Record<string, string | undefined> } };
  if (p.process?.env) return p.process.env.BOK_GIT_HASH || p.process.env.GIT_COMMIT || "unknown";
  return "unknown";
}

function defaultConfig(): LoggerConfig {
  return {
    environment: detectEnvironment(),
    version: defaultVersion(),
    gitHash: defaultGitHash(),
    maxLogsPerTrace: DEFAULT_MAX_LOGS_PER_TRACE,
    traceTtlMs: DEFAULT_TRACE_TTL_MS,
    maxTraces: DEFAULT_MAX_TRACES,
    maxReportsPerTrace: DEFAULT_MAX_REPORTS_PER_TRACE,
    write: defaultWrite,
    reporter: null,
  };
}

interface TraceRecord {
  traceId: string;
  logs: LogEntry[];
  createdAt: number;
  lastAt: number;
  /** 本 trace 已调度（含失败）的上报次数——限次防错误风暴打爆上报通道。 */
  reports: number;
  reportCapWarned: boolean;
  operation?: string;
  user?: LogContext;
}

class TraceStore {
  private traces = new Map<string, TraceRecord>();
  private maxLogsPerTrace = DEFAULT_MAX_LOGS_PER_TRACE;
  private traceTtlMs = DEFAULT_TRACE_TTL_MS;
  private maxTraces = DEFAULT_MAX_TRACES;

  configure(opts: { maxLogsPerTrace: number; traceTtlMs: number; maxTraces: number }): void {
    this.maxLogsPerTrace = opts.maxLogsPerTrace;
    this.traceTtlMs = opts.traceTtlMs;
    this.maxTraces = opts.maxTraces;
    for (const rec of this.traces.values()) {
      if (rec.logs.length > this.maxLogsPerTrace) {
        rec.logs.splice(0, rec.logs.length - this.maxLogsPerTrace);
      }
    }
  }

  create(traceId: string, operation?: string, user?: LogContext): TraceRecord {
    if (this.traces.size >= this.maxTraces) {
      // 淘汰最旧 trace（createdAt 最小）——多链路并发的内存上界。
      let oldestKey = "";
      let oldestAt = Infinity;
      for (const [k, v] of this.traces) {
        if (v.createdAt < oldestAt) {
          oldestAt = v.createdAt;
          oldestKey = k;
        }
      }
      if (oldestKey) this.traces.delete(oldestKey);
    }
    const now = Date.now();
    const rec: TraceRecord = {
      traceId,
      logs: [],
      createdAt: now,
      lastAt: now,
      reports: 0,
      reportCapWarned: false,
      operation,
      user,
    };
    this.traces.set(traceId, rec);
    return rec;
  }

  get(traceId: string): TraceRecord | undefined {
    return this.traces.get(traceId);
  }

  touch(rec: TraceRecord): void {
    rec.lastAt = Date.now();
  }

  push(rec: TraceRecord, entry: LogEntry): void {
    rec.logs.push(entry);
    if (rec.logs.length > this.maxLogsPerTrace) {
      rec.logs.splice(0, rec.logs.length - this.maxLogsPerTrace);
    }
  }

  /** 清理空闲超过 TTL 的 trace，返回清除数量。now 可注入（测试）。 */
  sweep(now: number = Date.now()): number {
    let removed = 0;
    for (const [k, v] of this.traces) {
      if (now - v.lastAt > this.traceTtlMs) {
        this.traces.delete(k);
        removed += 1;
      }
    }
    return removed;
  }

  clear(): void {
    this.traces.clear();
  }
}

let config: LoggerConfig = defaultConfig();
const store = new TraceStore();
let sweepTimer: ReturnType<typeof setInterval> | null = null;
const pendingReportTimers = new Set<ReturnType<typeof setTimeout>>();
let currentUser: LogContext | undefined;

/**
 * logger / reporter 自身故障的显式打点通道：raw console.warn + JSON（含堆栈）。
 * 刻意不走 logger.error——上报子系统故障若再触发上报即递归（需求 7 明令禁止）。
 */
function safeWarn(message: string, err: unknown): void {
  console.warn(
    JSON.stringify({
      ts: new Date().toISOString(),
      level: "warn",
      channel: "log-reporter",
      message,
      error: serializeError(err),
    }),
  );
}

function ensureSweepTimer(): void {
  if (sweepTimer) return;
  sweepTimer = setInterval(() => {
    store.sweep();
  }, SWEEP_INTERVAL_MS);
  // Node 下不阻止进程退出；浏览器无 unref（类型守卫）。
  const t = sweepTimer as { unref?: () => void };
  if (typeof t.unref === "function") t.unref();
}

// ---- 上报 ----

function runtimeInfo(): Record<string, unknown> {
  const g = globalThis as {
    window?: { location?: { href: string }; navigator?: { userAgent?: string } };
    process?: { version?: string; pid?: number };
  };
  if (g.window?.location) {
    return {
      href: g.window.location.href,
      userAgent: g.window.navigator?.userAgent ?? "",
    };
  }
  if (g.process) {
    return { node: g.process.version ?? "", pid: g.process.pid ?? 0 };
  }
  return {};
}

function dispatchReport(
  traceId: string,
  rec: TraceRecord | undefined,
  entry: LogEntry,
): void {
  // 上报独立 try-catch：失败只本地警告，绝不向上抛、绝不阻塞主业务（需求 7）。
  try {
    const payload: ErrorReportPayload = {
      traceId,
      occurredAt: entry.ts,
      environment: config.environment,
      version: config.version,
      gitHash: config.gitHash,
      operation: rec?.operation,
      user: rec?.user ? (redact(rec.user) as LogContext) : currentUser ? (redact(currentUser) as LogContext) : undefined,
      runtime: runtimeInfo(),
      error: entry.error ?? serializeError(entry.message),
      logs: rec ? rec.logs.slice() : [entry],
    };
    const result = config.reporter ? config.reporter.report(payload) : undefined;
    if (result instanceof Promise) {
      result.catch((err: unknown) => safeWarn("log report async failed", err));
    }
  } catch (err) {
    safeWarn("log report failed", err);
  }
}

function scheduleReport(traceId: string, rec: TraceRecord | undefined, entry: LogEntry): void {
  if (!config.reporter) return;
  if (rec) {
    if (rec.reports >= config.maxReportsPerTrace) {
      if (!rec.reportCapWarned) {
        rec.reportCapWarned = true;
        safeWarn(
          "log report cap reached for trace",
          new Error(`traceId=${traceId} cap=${config.maxReportsPerTrace}`),
        );
      }
      return;
    }
    rec.reports += 1;
  }
  // 异步非阻塞：setTimeout(0) 让出主流程；定时器登记在册，reset 时可回收。
  const timer = setTimeout(() => {
    pendingReportTimers.delete(timer);
    dispatchReport(traceId, rec, entry);
  }, 0);
  pendingReportTimers.add(timer);
}

// ---- TraceLogger ----

export class TraceLogger {
  readonly traceId: string;
  private readonly base: LogContext;

  constructor(traceId: string, base: LogContext) {
    this.traceId = traceId;
    this.base = base;
  }

  info(message: string, context?: LogContext): void {
    this.log("info", message, undefined, context);
  }

  warn(message: string, context?: LogContext): void {
    this.log("warn", message, undefined, context);
  }

  error(message: string, err?: unknown, context?: LogContext): void {
    this.log("error", message, err, context);
  }

  /** 派生 logger：同 traceId，上下文合并且子覆盖父。 */
  child(context: LogContext): TraceLogger {
    return new TraceLogger(this.traceId, { ...this.base, ...context });
  }

  /**
   * 包装交给第三方执行的回调（定时器 / 事件 / Promise 链）：保 trace 活性，
   * 同步与异步异常都记入本 trace 后**原样 rethrow**——记日志但不吞异常。
   */
  bind<F extends (...args: never[]) => unknown>(fn: F): F {
    const logger = this;
    const label = fn.name || "anonymous";
    const wrapped = function wrappedFn(this: unknown, ...args: unknown[]): unknown {
      const rec = store.get(logger.traceId);
      if (rec) store.touch(rec);
      try {
        const result = (fn as (...a: unknown[]) => unknown).apply(this, args);
        if (result instanceof Promise) {
          return result.catch((err: unknown) => {
            logger.error("bound async callback failed", err, { bind: label });
            throw err;
          });
        }
        return result;
      } catch (err) {
        logger.error("bound callback failed", err, { bind: label });
        throw err;
      }
    };
    return wrapped as F;
  }

  /** 本 trace 日志快照（浅拷贝；条目视为不可变）。 */
  getLogs(): LogEntry[] {
    const rec = store.get(this.traceId);
    return rec ? rec.logs.slice() : [];
  }

  private log(
    level: LogLevel,
    message: string,
    err?: unknown,
    context?: LogContext,
  ): void {
    const rec = store.get(this.traceId);
    const entry: LogEntry = {
      ts: new Date().toISOString(),
      level,
      traceId: this.traceId,
      message: typeof message === "string" ? message : String(message),
    };
    const merged: LogContext = { ...this.base, ...(context ?? {}) };
    if (Object.keys(merged).length > 0) {
      entry.context = redact(merged) as LogContext;
    }
    if (level === "error") {
      entry.error = serializeError(err !== undefined ? err : message);
    }
    // 先输出（本地行永远优先于任何上报/缓存故障），再入缓存，再调度上报。
    config.write(level, JSON.stringify(entry));
    if (rec) {
      store.push(rec, entry);
      store.touch(rec);
    }
    if (level === "error") scheduleReport(this.traceId, rec, entry);
  }
}

// ---- 公共 API ----

let idCounter = 0;

function newTraceId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  idCounter += 1;
  return `trc-${Date.now().toString(36)}-${idCounter.toString(36)}-${Math.random()
    .toString(36)
    .slice(2, 10)}`;
}

export interface StartTraceOptions {
  /** 显式指定 traceId（跨服务/续链场景）；缺省自动生成。 */
  traceId?: string;
  /** 当前业务操作（进入上报 payload 的「操作上下文」）。 */
  operation?: string;
  /** 当前用户（进入每条日志 context 与上报 payload）。 */
  user?: LogContext;
  /** 额外静态上下文，随本链路每条日志携带。 */
  context?: LogContext;
}

/** 业务入口统一从这里开始：生成/续接 traceId 并返回绑定该链路的 logger。 */
export function startTrace(options: StartTraceOptions = {}): TraceLogger {
  const traceId =
    options.traceId && options.traceId.length > 0 ? options.traceId : newTraceId();
  const existing = store.get(traceId);
  if (existing) {
    store.touch(existing); // 续链：保留既有日志
  } else {
    store.create(traceId, options.operation, options.user ?? currentUser);
  }
  ensureSweepTimer();
  const base: LogContext = {
    ...(options.user ?? {}),
    ...(options.context ?? {}),
  };
  return new TraceLogger(traceId, base);
}

export function getLogger(traceId: string): TraceLogger | undefined {
  return store.get(traceId) ? new TraceLogger(traceId, {}) : undefined;
}

export function getTraceLogs(traceId: string): LogEntry[] | undefined {
  const rec = store.get(traceId);
  return rec ? rec.logs.slice() : undefined;
}

export function sweepExpiredTraces(now: number = Date.now()): number {
  return store.sweep(now);
}

/** 会话装配后绑定当前用户：影响后续新建 trace 与上报 payload 的用户上下文。 */
export function setTraceUser(user: LogContext | undefined): void {
  currentUser = user;
}

/** 局部覆盖配置（undefined 项保持现状；reporter 传 null 显式关闭上报）。 */
export function configureLogger(options: ConfigureLoggerOptions = {}): void {
  if (options.environment !== undefined) config.environment = options.environment;
  if (options.version !== undefined) config.version = options.version;
  if (options.gitHash !== undefined) config.gitHash = options.gitHash;
  if (options.maxLogsPerTrace !== undefined) config.maxLogsPerTrace = options.maxLogsPerTrace;
  if (options.traceTtlMs !== undefined) config.traceTtlMs = options.traceTtlMs;
  if (options.maxTraces !== undefined) config.maxTraces = options.maxTraces;
  if (options.maxReportsPerTrace !== undefined) {
    config.maxReportsPerTrace = options.maxReportsPerTrace;
  }
  if (options.write !== undefined) config.write = options.write;
  if (options.reporter !== undefined) config.reporter = options.reporter;
  store.configure({
    maxLogsPerTrace: config.maxLogsPerTrace,
    traceTtlMs: config.traceTtlMs,
    maxTraces: config.maxTraces,
  });
}

// ---- 上报器工厂 ----

export interface CreateReporterOptions {
  endpoint?: string;
  /** 自定义传输（默认 fetch POST JSON）；返回 Promise 时拒绝会被隔离为本地警告。 */
  transport?: (payload: ErrorReportPayload) => unknown;
  environment?: string;
  version?: string;
  gitHash?: string;
  getUser?: () => LogContext | undefined;
  getOperation?: () => string | undefined;
}

function defaultTransport(
  endpoint?: string,
): ((payload: ErrorReportPayload) => unknown) | undefined {
  if (!endpoint) return undefined;
  if (typeof fetch !== "function") {
    safeWarn("log reporting unavailable: no fetch in this runtime", new Error("createReporter"));
    return undefined;
  }
  return (payload: ErrorReportPayload) =>
    fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      keepalive: true,
    }).then((res: Response) => {
      if (!res.ok) throw new Error(`log report endpoint ${res.status}`);
      return res;
    });
}

export function createReporter(options: CreateReporterOptions = {}): LoggerReporter {
  let disabledWarned = false;
  return {
    report(payload: ErrorReportPayload): unknown {
      const transport = options.transport ?? defaultTransport(options.endpoint);
      if (!transport) {
        if (!disabledWarned) {
          disabledWarned = true;
          safeWarn(
            "log reporting disabled: no endpoint/transport configured",
            new Error("createReporter"),
          );
        }
        return undefined;
      }
      const merged: ErrorReportPayload = {
        ...payload,
        environment: options.environment ?? payload.environment,
        version: options.version ?? payload.version,
        gitHash: options.gitHash ?? payload.gitHash,
      };
      const user = options.getUser?.();
      if (user) merged.user = redact(user) as LogContext;
      const op = options.getOperation?.();
      if (op) merged.operation = op;
      return transport(merged);
    },
  };
}

// ---- 全局兜底异常捕获 ----

/**
 * 一站式装配：注册上报器 + 安装全局兜底异常捕获。
 * 应用入口（web 端见 lib/log-bootstrap.ts 的 setupClientLogging）调用一次即可。
 */
export function configureReporting(
  options: CreateReporterOptions & InstallGlobalHandlersOptions & { maxReportsPerTrace?: number } = {},
): void {
  configureLogger({
    environment: options.environment,
    version: options.version,
    gitHash: options.gitHash,
    maxReportsPerTrace: options.maxReportsPerTrace,
    reporter: createReporter(options),
  });
  installGlobalHandlers({ exitOnUncaught: options.exitOnUncaught, exitGraceMs: options.exitGraceMs });
}

interface InstalledHandlers {
  onWindowError?: (event: ErrorEvent) => void;
  onUnhandledRejectionDom?: (event: PromiseRejectionEvent) => void;
  onUncaughtException?: (err: unknown) => void;
  onUnhandledRejectionNode?: (reason: unknown) => void;
}

let installed: InstalledHandlers | null = null;
let exitOnUncaught = false;
let exitGraceMs = 500;

function globalFallbackLogger(): TraceLogger {
  const existing = store.get(GLOBAL_TRACE_ID);
  if (!existing) store.create(GLOBAL_TRACE_ID, "global-fallback", currentUser);
  return new TraceLogger(GLOBAL_TRACE_ID, { source: "global-handler" });
}

export interface InstallGlobalHandlersOptions {
  /** Node uncaughtException 后是否延迟退出（默认 false：只记录，交由守护进程决策）。 */
  exitOnUncaught?: boolean;
  /** 退出前给上报留的宽限毫秒。 */
  exitGraceMs?: number;
}

/**
 * 安装全局兜底：浏览器 window.onerror / unhandledrejection；
 * Node uncaughtException / unhandledRejection。幂等（重复安装只装一份），
 * 返回卸载函数（可重复调用）。
 */
export function installGlobalHandlers(
  options: InstallGlobalHandlersOptions = {},
): () => void {
  if (installed) return uninstallGlobalHandlers;
  installed = {};
  exitOnUncaught = options.exitOnUncaught ?? false;
  exitGraceMs = options.exitGraceMs ?? 500;

  const g = (globalThis as unknown as {
    window?: {
      addEventListener?: (type: string, fn: (ev: never) => void) => void;
      removeEventListener?: (type: string, fn: (ev: never) => void) => void;
    };
    process?: {
      on?: (event: string, fn: (...args: never[]) => void) => unknown;
      removeListener?: (event: string, fn: (...args: never[]) => void) => unknown;
      exit?: (code: number) => never;
    };
  });

  if (g.window && typeof g.window.addEventListener === "function") {
    const win = g.window;
    installed.onWindowError = (event: ErrorEvent): void => {
      const err =
        event.error instanceof Error
          ? event.error
          : new Error(event.message || "window error");
      globalFallbackLogger().error("window.onerror", err, {
        filename: event.filename,
        lineno: event.lineno,
        colno: event.colno,
      });
    };
    installed.onUnhandledRejectionDom = (event: PromiseRejectionEvent): void => {
      globalFallbackLogger().error("unhandledrejection", event.reason ?? new Error("unknown rejection"));
    };
    win.addEventListener("error", installed.onWindowError as (ev: never) => void);
    win.addEventListener("unhandledrejection", installed.onUnhandledRejectionDom as (ev: never) => void);
  } else if (g.process && typeof g.process.on === "function") {
    const proc = g.process;
    installed.onUncaughtException = (err: unknown): void => {
      globalFallbackLogger().error("uncaughtException", err);
      if (exitOnUncaught && typeof proc.exit === "function") {
        // 给异步上报留宽限窗后退出（由守护进程/launchd 拉起）。
        setTimeout(() => proc.exit?.(1), exitGraceMs);
      }
    };
    installed.onUnhandledRejectionNode = (reason: unknown): void => {
      globalFallbackLogger().error("unhandledRejection", reason);
    };
    proc.on("uncaughtException", installed.onUncaughtException as (...args: never[]) => void);
    proc.on("unhandledRejection", installed.onUnhandledRejectionNode as (...args: never[]) => void);
  }

  return uninstallGlobalHandlers;
}

export function uninstallGlobalHandlers(): void {
  if (!installed) return;
  const g = (globalThis as unknown as {
    window?: {
      removeEventListener?: (type: string, fn: (ev: never) => void) => void;
    };
    process?: {
      removeListener?: (event: string, fn: (...args: never[]) => void) => unknown;
    };
  });
  if (g.window && typeof g.window.removeEventListener === "function") {
    if (installed.onWindowError) {
      g.window.removeEventListener("error", installed.onWindowError as (ev: never) => void);
    }
    if (installed.onUnhandledRejectionDom) {
      g.window.removeEventListener(
        "unhandledrejection",
        installed.onUnhandledRejectionDom as (ev: never) => void,
      );
    }
  }
  if (g.process && typeof g.process.removeListener === "function") {
    if (installed.onUncaughtException) {
      g.process.removeListener(
        "uncaughtException",
        installed.onUncaughtException as (...args: never[]) => void,
      );
    }
    if (installed.onUnhandledRejectionNode) {
      g.process.removeListener(
        "unhandledRejection",
        installed.onUnhandledRejectionNode as (...args: never[]) => void,
      );
    }
  }
  installed = null;
}

/** 测试钩子：当前已安装的全局兜底处理器（未安装返回 null）。 */
export function _globalHandlers(): {
  uncaughtException?: (err: unknown) => void;
  unhandledRejection?: (reason: unknown) => void;
} | null {
  if (!installed) return null;
  return {
    uncaughtException: installed.onUncaughtException,
    unhandledRejection: installed.onUnhandledRejectionNode,
  };
}

/** 测试钩子：完整重置单例（配置/缓存/已装处理器/在途上报定时器/清理定时器）。 */
export function resetForTests(): void {
  uninstallGlobalHandlers();
  for (const t of pendingReportTimers) clearTimeout(t);
  pendingReportTimers.clear();
  if (sweepTimer) {
    clearInterval(sweepTimer);
    sweepTimer = null;
  }
  store.clear();
  config = defaultConfig();
  currentUser = undefined;
  exitOnUncaught = false;
}
