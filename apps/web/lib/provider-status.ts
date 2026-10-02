/**
 * 容灾可观测波（feat/dr-observability，契约 §6）· Provider 读数 → 灯色/文案的
 * 纯函数单点。两处消费同一判定，杜绝颜色口径漂移：
 *   ① CallStudio 右栏「Provider 服务状态」卡（3s 轮询 GET /api/metrics/providers）
 *   ② root 容灾面板（3s 轮询 GET /api/ops/disaster-status，providers 同形）
 *
 * 阈值（Ethan 定稿，逐字）：
 * - LLM：last/p95 **任一** >2500ms → 红（SLO 违约）；1000-2500 → 黄；<1000 → 绿。
 * - asr/tts/vad：last > 2×p50 → 黄；> 4×p50 → 红（p50 缺失/非正 = 无基线，按绿）。
 * - 无数据（last_ms 非有限数或 n<=0）→ 灰（沿用既有「已连接」灯语义）。
 *
 * 零依赖、零 React——test/provider-status.test.mjs 走单文件转译（仓内既有路线）。
 */

/** 四级灯色（grey=无数据）。 */
export type ProviderLevel = "green" | "yellow" | "red" | "grey";

/** metrics/disaster 两端点 providers 行的结构形状（数字可缺省，缺失不炸）。 */
export type ProviderStatLike = {
  last_ms?: number | null;
  p50?: number | null;
  p95?: number | null;
  n?: number | null;
};

/** 契约 §2/§4 famine 对象形状（字段全宽容，坏数据只影响展示不抛错）。 */
export type FamineLike = {
  level?: string | null;
  ema_s?: number | null;
  since?: string | null;
  downgraded?: boolean | null;
  dialing_paused?: boolean | null;
  manual_override?: string | null;
};

/** 有限数字守卫（string/null/NaN 一律 null——契约数字可能缺席）。 */
export function finiteMs(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** kind→灯色（kind 用契约单词 asr/llm/tts/vad；未知 kind 走「其余 provider」分支）。 */
export function providerLevel(kind: string, stat?: ProviderStatLike | null): ProviderLevel {
  if (!stat) return "grey";
  const last = finiteMs(stat.last_ms);
  if (last === null) return "grey";
  const n = finiteMs(stat.n ?? null);
  if (stat.n !== undefined && stat.n !== null && n !== null && n <= 0) return "grey";
  if (kind === "llm") {
    const p95 = finiteMs(stat.p95);
    const worst = p95 === null ? last : Math.max(last, p95);
    if (worst > 2500) return "red";
    if (worst >= 1000) return "yellow";
    return "green";
  }
  const p50 = finiteMs(stat.p50);
  if (p50 === null || p50 <= 0) return "green";
  if (last > 4 * p50) return "red";
  if (last > 2 * p50) return "yellow";
  return "green";
}

/** 灯面 emoji（Ethan 定稿格式的圆点；grey 用 ⚪ 表示无数据）。 */
export const LEVEL_DOT: Record<ProviderLevel, string> = {
  green: "🟢",
  yellow: "🟡",
  red: "🔴",
  grey: "⚪",
};

/** 灯面文字色（与 emoji 同档；grey 复用全局 muted 工具类）。 */
export const LEVEL_TEXT_CLASS: Record<ProviderLevel, string> = {
  green: "text-emerald-600",
  yellow: "text-amber-600",
  red: "text-red-600",
  grey: "muted",
};

/** 毫秒读数（整数化；「412ms」形）。 */
export function formatMs(v: number): string {
  return `${Math.round(v)}ms`;
}

/**
 * Provider 行取值文案（label 由消费方给；此处只拼值）：
 *   有读数 → 「已连接 · 412ms (p95 490)」；p95 缺失（如 vad 端点）→ 「已连接 · 18ms」；
 *   无数据 → 「已连接」（灰灯，沿用原卡文案）。
 */
export function providerValueText(stat?: ProviderStatLike | null): string {
  const last = stat ? finiteMs(stat.last_ms) : null;
  if (last === null) return "已连接";
  const p95 = stat ? finiteMs(stat.p95) : null;
  return p95 === null
    ? `已连接 · ${formatMs(last)}`
    : `已连接 · ${formatMs(last)} (p95 ${Math.round(p95)})`;
}

export type FamineLine = { dot: string; text: string; level: ProviderLevel };

/**
 * 状态行（严格照 Ethan 定稿）：healthy=🟢 健康 (EMA 0.6s) / famine=🟡 饥荒中 /
 * downgraded=🔴 已降档·停拨 / dialing_paused=🔴 停拨。EMA 秒=1 位小数，
 * 有读数才带括号（无 ema_s 不编数字）。
 */
export function famineLine(famine?: FamineLike | null): FamineLine {
  if (!famine) return { dot: LEVEL_DOT.grey, text: "未知", level: "grey" };
  const ema = finiteMs(famine.ema_s);
  const suffix = ema === null ? "" : ` (EMA ${ema.toFixed(1)}s)`;
  if (famine.downgraded) return { dot: "🔴", text: `已降档·停拨${suffix}`, level: "red" };
  if (famine.dialing_paused) return { dot: "🔴", text: `停拨${suffix}`, level: "red" };
  const level = String(famine.level ?? "");
  if (level === "famine") return { dot: "🟡", text: `饥荒中${suffix}`, level: "yellow" };
  if (level === "healthy") return { dot: "🟢", text: `健康${suffix}`, level: "green" };
  return { dot: LEVEL_DOT.grey, text: `${level || "未知"}${suffix}`, level: "grey" };
}

/** ISO 时刻 → 粗粒度持续时长（「2 分 13 秒」；坏值/空=空串）。 */
export function sinceDuration(since?: string | null, now: number = Date.now()): string {
  if (!since) return "";
  const t = Date.parse(since);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.floor((now - t) / 1000));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${s % 60} 秒`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时 ${m % 60} 分`;
  return `${Math.floor(h / 24)} 天 ${h % 24} 小时`;
}

/** swap 行灯色：用量 > 阈值（默认 8GB）→ 红；无数据 → 灰。 */
export function swapLevel(used?: number | null, threshold?: number | null): ProviderLevel {
  const u = finiteMs(used);
  if (u === null) return "grey";
  const th = finiteMs(threshold ?? null) ?? 8;
  return u > th ? "red" : "green";
}
