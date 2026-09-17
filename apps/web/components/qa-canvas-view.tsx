"use client";

// QA 画布视图(spec §4.3/§4.4,2026-09-17 Phase1)。只读渲染在本文件;
// 连线/断线编辑回调经 props 上抛(page.tsx 持数据与 PATCH)。
// 布局=lib/qa-canvas.deriveGraph(确定性);拖动位置 localStorage;MiniMap 常开。

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background, Controls, MiniMap, ReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  deriveGraph, LOCAL_POS_KEY, parseTemplateSteps,
  type FlowStep, type Pt, type QaRow,
} from "@/lib/qa-canvas";

export type TemplateRow = { id: string; name?: string; steps_json?: string; language?: string };

const LANG_LABEL: Record<string, string> = { zh: "普", cantonese: "粤", en: "EN" };
const SOURCE_LABEL: Record<string, string> = { curated: "精选", mined: "挖掘", imported: "导入" };

function QaEntryNode({ data }: NodeProps) {
  const d = data as QaRow & { isHead: boolean; canned?: "ok" | "missing"; canEdit: boolean };
  const dim = d.enabled === false;
  return (
    <div
      className={`w-[260px] rounded-lg border bg-white/5 p-3 text-xs ${dim ? "opacity-50" : ""} ${d.isHead ? "border-(--accent)" : "border-(--card-border)"}`}
      title={d.canEdit ? undefined : "共享条目由主管维护"}
    >
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
  onConnectCluster: (fromId: string, toId: string) => void;
  onDisconnect: (edge: { id: string; data?: { kind?: string }; source: string; target: string }) => void;
  onStepConnect: (entryId: string, stepIndex: number) => void;
}) {
  const { rows, templates, templateId, canned } = props;
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
        data: { ...n.data, canned: canned[String(n.id)]?.state, canEdit: props.canEditRow(n.data) },
      })),
    ],
    [graph, canned, props],
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
      <div className="h-[600px] rounded-lg border border-(--card-border)">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          fitView
          minZoom={0.2}
          onNodeDragStop={onNodeDragStop}
          onNodeClick={(_, node) => {
            const row = rows.find((r) => String(r.id) === node.id);
            if (row) props.onNodeClick(row);
          }}
          onEdgeClick={(_, edge) => {
            if (confirm("解除这条连线？")) props.onDisconnect(edge as never);
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
