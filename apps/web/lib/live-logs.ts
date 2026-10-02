/**
 * 实时日志抽屉（DR 波契约 §5/§6）· 纯函数单点：追加/截断（上限最近 N 行，超出
 * 丢头部）与「是否贴底」判定（决定自动滚底 vs 用户上滚暂停）。零依赖、零 React
 * ——test/live-logs.test.mjs 走单文件转译（仓内既有路线），交互壳在
 * components/CallStudio.tsx 的 CallLogDrawer。
 */

/** 抽屉内一行：自增 id（头部丢行后 index 位移不撞 React key）+ 原始文本。 */
export type LogLine = { id: number; text: string };

export type AppendResult = { lines: LogLine[]; nextId: number };

/**
 * 追加一批原始行：id 从 nextId+1 递增分配；总行数超 max 丢头部（保留最近 max 行）。
 * 返回新 nextId（只增不减——即使头部被丢，id 也不回退）。
 */
export function appendLogLines(
  prev: LogLine[],
  fresh: readonly unknown[],
  nextId: number,
  max: number,
): AppendResult {
  const cap = Number.isFinite(max) && max > 0 ? Math.floor(max) : 0;
  let id = nextId;
  const added: LogLine[] = [];
  for (const raw of fresh ?? []) {
    id += 1;
    added.push({ id, text: String(raw ?? "") });
  }
  let lines = added.length ? [...prev, ...added] : [...prev];
  if (cap > 0 && lines.length > cap) lines = lines.slice(lines.length - cap);
  return { lines, nextId: id };
}

/** 是否贴底（自动滚底窗口；默认 24px 容差）。 */
export function isNearBottom(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
  eps = 24,
): boolean {
  const h = Number(scrollHeight);
  const t = Number(scrollTop);
  const c = Number(clientHeight);
  if (!Number.isFinite(h) || !Number.isFinite(t) || !Number.isFinite(c)) return true;
  return h - t - c <= eps;
}
