"use client";

// QA 画布视图(spec §4.3/§4.4,2026-09-17 Phase1)。渲染+编辑交互在本文件:
// 连线(onConnect)/右键/双击/断线点击一律上抛,page.tsx 持数据与 PATCH。
// 布局=lib/qa-canvas.deriveGraph(确定性);拖动位置 localStorage;MiniMap 常开。

import {
  useCallback, useEffect, useMemo, useState,
  type CSSProperties, type MouseEvent as ReactMouseEvent,
} from "react";
import {
  Background, Controls, Handle, MiniMap, Position, ReactFlow,
  type Edge, type Node, type NodeChange, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  deriveGraph, LOCAL_POS_KEY, parseTemplateSteps,
  type FlowStep, type Pt, type QaRow,
} from "@/lib/qa-canvas";

export type TemplateRow = { id: string; name?: string; steps_json?: string; language?: string };

const LANG_LABEL: Record<string, string> = { zh: "普", cantonese: "粤", en: "EN" };
const SOURCE_LABEL: Record<string, string> = { curated: "精选", mined: "挖掘", imported: "导入" };

// 拖拽柄外观(源=左/目标=右;边由柄锚定,无柄时 v12 报 008 且边不渲染)。
const HANDLE_STYLE: CSSProperties = {
  width: 8, height: 8, background: "var(--accent)", border: "1px solid var(--card-border)",
};

function QaEntryNode({ data }: NodeProps) {
  const d = data as QaRow & { isHead: boolean; canned?: "ok" | "missing"; canEdit: boolean };
  const dim = d.enabled === false;
  return (
    <div
      className={`w-[260px] rounded-lg border bg-white/5 p-3 text-xs ${dim ? "opacity-50" : ""} ${d.isHead ? "border-(--accent)" : "border-(--card-border)"}`}
      title={d.canEdit ? undefined : "共享条目由主管维护"}
    >
      {/* 柄始终渲染(v12 边锚定柄,缺柄=008 且簇/步骤边整体消失)。只读条目双向闸:
          接进=落点校验查 isConnectable;拖出=onPointerDown 只查 isConnectableStart
          (v12 不看 isConnectable,反向从 target 柄拖出同理),两枚柄都钉 canEdit。
          共享行写权限仍由 CP 兜底。 */}
      <Handle
        type="target" position={Position.Right}
        isConnectable={d.canEdit} isConnectableStart={d.canEdit} style={HANDLE_STYLE}
      />
      <Handle
        type="source" position={Position.Left}
        isConnectable={d.canEdit} isConnectableStart={d.canEdit} style={HANDLE_STYLE}
      />
      <p className="line-clamp-2 font-medium">{String(d.question_text ?? "(无问法)")}</p>
      <p className="mt-1 line-clamp-1 muted">{String(d.answer_text ?? "")}</p>
      <div className="mt-2 flex flex-wrap items-center gap-1">
        <span className="rounded-sm bg-white/10 px-1 text-[10px]">{LANG_LABEL[String(d.lang ?? "zh")] ?? d.lang}</span>
        <span className="rounded-sm bg-white/10 px-1 text-[10px]">命中 {Number(d.hit_count ?? 0)}</span>
        {d.source && <span className="rounded-sm bg-white/10 px-1 text-[10px]">{SOURCE_LABEL[d.source] ?? d.source}</span>}
        {d.canned === "missing" && <span className="rounded-sm bg-amber-400/20 px-1 text-[10px] text-amber-300">缺料</span>}
        {d.canned === "ok" && <span className="rounded-sm bg-emerald-400/20 px-1 text-[10px] text-emerald-300">罐头✓</span>}
        {d.enabled === false && <span className="rounded-sm bg-white/10 px-1 text-[10px]">停用</span>}
        {!d.canEdit && <span className="rounded-sm bg-white/10 px-1 text-[10px]" title="共享只读">🔒</span>}
      </div>
    </div>
  );
}

function QaStepNode({ data }: NodeProps) {
  const d = data as { index: number; goal: string; refFirstLine: string; virtual: boolean };
  return (
    <div className={`w-[200px] rounded-lg border p-3 text-xs ${d.virtual ? "border-dashed border-(--card-border) muted" : "border-(--accent) bg-(--accent)/5"}`}>
      {/* 只作连线落点(挂步骤),不给 source 柄(连线只准从条目拖出);虚拟「全程通用」不接。 */}
      <Handle type="target" position={Position.Right} isConnectable={!d.virtual} style={HANDLE_STYLE} />
      <p className="font-medium">{d.virtual ? "全程通用" : `第 ${d.index + 1} 步 · ${d.goal}`}</p>
      {!d.virtual && <p className="mt-1 line-clamp-2 muted">{d.refFirstLine}</p>}
    </div>
  );
}

const NODE_TYPES = { qaEntry: QaEntryNode, qaStep: QaStepNode };

export default function QaCanvasView(props: {
  rows: QaRow[];
  templates: TemplateRow[];
  templateId: string;
  onTemplateChange: (id: string) => void;
  canned: Record<string, { state: "ok" | "missing" }>;
  canEditRow: (row: QaRow) => boolean;
  onNodeClick: (row: QaRow) => void;
  onPaneDoubleClick: (pt: { x: number; y: number }) => void;
  /** 右键条目(Task 7 提供菜单);不给时仅吞掉浏览器默认菜单。 */
  onNodeContextMenu?: (row: QaRow, e: ReactMouseEvent) => void;
  onConnectCluster: (fromId: string, toId: string) => void;
  onDisconnect: (edge: { id: string; data?: { kind?: string }; source: string; target: string }) => void;
  onStepConnect: (entryId: string, stepIndex: number) => void;
}) {
  const { rows, templates, templateId, canned, canEditRow } = props;
  const [langFilter, setLangFilter] = useState("all");
  const [positions, setPositions] = useState<Record<string, Pt>>({});
  const accountId = "acc-001"; // 与页面 useAccount 同源,Task 7 接线时由 props 传入替换。

  useEffect(() => {
    try {
      const raw = localStorage.getItem(LOCAL_POS_KEY(accountId, templateId));
      setPositions(raw ? (JSON.parse(raw) as Record<string, Pt>) : {});
    } catch {
      setPositions({});
    }
  }, [accountId, templateId]);

  const steps: FlowStep[] = useMemo(() => {
    const tpl = templates.find((t) => String(t.id) === templateId);
    return parseTemplateSteps(String(tpl?.steps_json ?? ""));
  }, [templates, templateId]);

  const graph = useMemo(
    () => deriveGraph(rows, steps, { langFilter, positions }),
    [rows, steps, langFilter, positions],
  );

  const nodes: Node[] = useMemo(
    () => [
      ...graph.stepNodes.map((n) => ({ ...n, data: { ...n.data } })),
      ...graph.qaNodes.map((n) => ({
        ...n,
        data: { ...n.data, canned: canned[String(n.id)]?.state, canEdit: canEditRow(n.data) },
      })),
    ],
    [graph, canned, canEditRow],
  );
  const edges: Edge[] = useMemo(
    () =>
      graph.edges.map((e) => ({
        ...e,
        animated: e.data.kind === "step",
        style: e.data.kind === "cluster"
          ? { stroke: "var(--accent)", strokeWidth: 1.5 }
          : { stroke: "#888", strokeDasharray: "4 3" },
        labelStyle: { fontSize: 10 },
      })),
    [graph],
  );

  const onNodeDragStop = useCallback(
    (_: unknown, node: Node) => {
      setPositions((prev) => {
        const next = { ...prev, [node.id]: node.position };
        try {
          localStorage.setItem(LOCAL_POS_KEY(accountId, templateId), JSON.stringify(next));
        } catch { /* 隐私模式丢弃,不阻塞 */ }
        return next;
      });
    },
    [accountId, templateId],
  );

  // 受控模式必须消化 position 变更(v12):拖动实时跟手走 deriveGraph 同一条
  // positions 覆盖路径(batch 合并一次 setPositions);onNodeDragStop 只做落盘。
  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setPositions((prev) => {
      const next = { ...prev };
      let moved = false;
      for (const c of changes) {
        if (c.type === "position" && c.position) {
          next[c.id] = c.position;
          moved = true;
        }
      }
      return moved ? next : prev;
    });
  }, []);

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <select
          className="rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--accent)"
          value={templateId}
          onChange={(e) => props.onTemplateChange(e.target.value)}
        >
          {templates.map((t) => (
            <option key={String(t.id)} value={String(t.id)}>{String(t.name || t.id)}</option>
          ))}
        </select>
        {(["all", "zh", "cantonese", "en"] as const).map((k) => (
          <button
            key={k}
            className={`btn-ghost text-xs ${langFilter === k ? "border-(--accent) text-accent" : "muted"}`}
            onClick={() => setLangFilter(k)}
          >
            {k === "all" ? "全部" : LANG_LABEL[k]}
          </button>
        ))}
        <button
          className="btn-ghost text-xs"
          onClick={() => {
            localStorage.removeItem(LOCAL_POS_KEY(accountId, templateId));
            setPositions({});
          }}
        >
          重置布局
        </button>
      </div>
      {/* 双击空白新建入口(Task 7 消费坐标):@xyflow 12.11 无 onPaneDoubleClick,
          用包裹层 onDoubleClick + 落点判 pane 兜出,并关掉双击缩放避免手势打架。 */}
      <div
        className="h-[600px] rounded-lg border border-(--card-border)"
        onDoubleClick={(e) => {
          const t = e.target as HTMLElement | null;
          if (t?.classList?.contains("react-flow__pane")) {
            props.onPaneDoubleClick({ x: e.clientX, y: e.clientY });
          }
        }}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          fitView
          minZoom={0.2}
          zoomOnDoubleClick={false}
          onNodesChange={onNodesChange}
          onNodeDragStop={onNodeDragStop}
          onNodeClick={(_, node) => {
            const row = rows.find((r) => String(r.id) === node.id);
            if (row) props.onNodeClick(row);
          }}
          onNodeContextMenu={(e, node) => {
            e.preventDefault();
            const row = rows.find((r) => String(r.id) === node.id);
            row && props.onNodeContextMenu?.(row, e);
          }}
          onConnect={(conn) => {
            if (!conn.source || !conn.target) return;
            const srcIsQa = conn.source.indexOf("step:") !== 0;
            if (!srcIsQa) return; // 只允许从条目拖出
            const target = conn.target;
            if (target.startsWith("step:")) {
              const idx = Number(target.slice(5));
              if (!Number.isNaN(idx)) props.onStepConnect(conn.source, idx === -1 ? -1 : idx);
              return;
            }
            props.onConnectCluster(conn.source, target);
          }}
          onEdgeClick={(_, edge) => {
            // Edge→上抛载荷的结构收窄(仅取本页关心的字段,不再 as never 逃逸)。
            const hit = edge as { id: string; data?: { kind?: string }; source: string; target: string };
            if (confirm("解除这条连线？")) props.onDisconnect(hit);
          }}
        >
          <Background gap={24} />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
      </div>
    </div>
  );
}
