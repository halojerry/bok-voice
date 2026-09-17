// QA 画布纯函数（qa-canvas Phase 1 Task 4，spec §4.3/§4.4）：解析 steps、派生节点/边、
// 簇校验、布局契约。Task 5/6/7 的画布组件唯一数据面——函数签名与返回形态勿动。
// 布局契约（archify 原则）：步骤脊柱=唯一主路径；QA 簇=最近步骤列的星形侧分支
// （head 行首、变体缩进挂其右下）；确定性网格（同输入同输出，无随机/时间源）；
// 语义边标签（cluster=变体归属，step=跳步入口）；位置覆盖走 localStorage（LOCAL_POS_KEY）。

export type QaRow = {
  id: string;
  owner_user_id?: string;
  question_text?: string;
  answer_text?: string;
  lang?: string;
  scope?: string;
  step_index?: number;
  voice_id?: string;
  enabled?: boolean;
  hit_count?: number;
  source?: string;
  cluster_head_id?: string;
  created_at?: string;
};

export type FlowStep = { goal: string; ref: string };
export type Pt = { x: number; y: number };

export type CanvasStepNode = {
  id: string; type: "qaStep";
  position: Pt;
  data: { index: number; goal: string; refFirstLine: string; virtual: boolean };
};
export type CanvasQaNode = {
  id: string; type: "qaEntry";
  position: Pt;
  data: QaRow & { isHead: boolean; canned?: "ok" | "missing" };
};
export type CanvasEdge = {
  id: string;
  source: string; target: string;
  sourceHandle: string | null; targetHandle: string | null;
  data: { kind: "cluster" | "step" };
  animated?: boolean;
  label?: string;
};

export const COL_W = 300;
export const ROW_H = 150;
// global（col0）条目的独立 x 泳道间距：col0 与 col1 的 y 基准同为 40（global 节点
// y=0+40 偏移、step:0 节点 y=40），无独立泳道时两列条目像素级全同重叠——global 列
// 右移让出独立车道，确定性契约不变（纯常量偏移，同输入同输出）。
const GLOBAL_COL_GAP = 80;

export function parseTemplateSteps(stepsJson: string): FlowStep[] {
  try {
    const raw = JSON.parse(stepsJson || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

export function LOCAL_POS_KEY(accountId: string, templateId: string): string {
  return `qa-canvas-pos:${accountId}:${templateId}`;
}

/** 列 x 坐标：0=通用/未挂步，1..N=步骤。 */
function colOf(row: QaRow, stepCount: number): number {
  if (String(row.scope ?? "") === "step") {
    const idx = Number(row.step_index ?? -1);
    if (idx >= 0 && idx < stepCount) return idx + 1;
  }
  return 0;
}

export function deriveGraph(
  rows: QaRow[],
  steps: FlowStep[],
  opts: { langFilter: string; positions: Record<string, Pt> },
): { qaNodes: CanvasQaNode[]; stepNodes: CanvasStepNode[]; edges: CanvasEdge[] } {
  const filtered = (rows ?? []).filter(
    (r) => opts.langFilter === "all" || String(r.lang ?? "zh") === opts.langFilter,
  );
  const stepNodes: CanvasStepNode[] = steps.map((s, i) => ({
    id: `step:${i}`,
    type: "qaStep" as const,
    position: { x: 0, y: 40 + i * (ROW_H + 60) },
    data: {
      index: i,
      goal: String(s.goal || ""),
      refFirstLine: String(s.ref || "").split("\n")[0].slice(0, 40),
      virtual: false,
    },
  }));
  // 虚拟「全程通用」节点在列首上方。
  stepNodes.unshift({
    id: "step:global",
    type: "qaStep",
    position: { x: 0, y: 0 },
    data: { index: -1, goal: "全程通用", refFirstLine: "不挂步骤的条目归此列", virtual: true },
  });

  const heads = new Set(filtered.map((r) => String(r.cluster_head_id || "")).filter(Boolean));
  const qaNodes: CanvasQaNode[] = [];
  const cursor = new Map<number, number>(); // 列内游标（确定性行序：创建序=输入序）
  for (const r of filtered) {
    const col = colOf(r, steps.length);
    const isHead = heads.has(String(r.id));
    // 星形侧分支：head 行首，变体缩进挂其右下。
    const headId = String(r.cluster_head_id || "");
    const indent = headId ? 120 : 0;
    const rowIdx = cursor.get(col) ?? 0;
    const base = stepNodes.find((n) => n.id === `step:${col === 0 ? "global" : col - 1}`)!.position;
    const pos = opts.positions[String(r.id)] ?? {
      x: base.x + COL_W + indent + (col === 0 ? GLOBAL_COL_GAP : 0),
      y: base.y + rowIdx * (ROW_H - 30) + (col === 0 ? 40 : 0),
    };
    cursor.set(col, rowIdx + 1);
    qaNodes.push({
      id: String(r.id),
      type: "qaEntry",
      position: pos,
      data: { ...r, isHead },
    });
  }
  const edges: CanvasEdge[] = [];
  for (const r of filtered) {
    const headId = String(r.cluster_head_id || "");
    if (headId && filtered.some((x) => String(x.id) === headId)) {
      edges.push({
        id: `c:${r.id}:${headId}`,
        source: String(r.id), target: headId,
        sourceHandle: null, targetHandle: null,
        data: { kind: "cluster" },
      });
    }
    if (String(r.scope ?? "") === "step") {
      const idx = Number(r.step_index ?? -1);
      if (idx >= 0 && idx < steps.length) {
        edges.push({
          id: `s:${r.id}:step:${idx}`,
          source: String(r.id), target: `step:${idx}`,
          sourceHandle: null, targetHandle: null,
          data: { kind: "step" },
          animated: false,
          label: `进入第 ${idx + 1} 步`,
        });
      }
    }
  }
  return { qaNodes, stepNodes, edges };
}

/**
 * 连簇校验（spec §4.4）：一层星形；目标是变体→重定向其 head；自连拒绝。
 * source 侧：已带变体的主条目整类拒绝（H1→H2 / H1→V2 / H1→S 都会令其变体链
 * V1→H1→H2 变两层）——一层星形是数据不变量，CP 侧零校验后盾，纯函数层把死。
 * target 侧：主条目挂到自己簇内变体被 targetHead===fromId 拦截。
 * 变体重挂自己已挂的 head=幂等成功（headId 仍返回该 head，调用方落库为 no-op）。
 * （brief 实现草稿的「两条目已在同一簇」分支会误杀幂等重挂，已按测试语义收窄。）
 */
export function resolveClusterTarget(
  rows: QaRow[], fromId: string, toId: string,
): { ok: boolean; headId: string; reason: string } {
  if (fromId === toId) return { ok: false, headId: "", reason: "不能连接到自己" };
  if (rows.some((x) => String(x.cluster_head_id || "") === fromId)) {
    return { ok: false, headId: "", reason: "主条目已带变体，先解除其变体再挂" };
  }
  const headOf = (id: string): string => {
    const r = rows.find((x) => String(x.id) === id);
    return String(r?.cluster_head_id || "") || id;
  };
  const targetHead = headOf(toId);
  if (targetHead === fromId) return { ok: false, headId: "", reason: "主条目不能挂到自己的变体" };
  return { ok: true, headId: targetHead, reason: "" };
}
