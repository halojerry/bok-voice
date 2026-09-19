"use client";

// QA 画布视图(spec §4.3/§4.4,2026-09-17 Phase1)。渲染+编辑交互在本文件:
// 连线(onConnect)/右键/双击/断线(Delete 键或边右键)一律上抛,page.tsx 持数据与 PATCH。
// 布局=lib/qa-canvas.deriveGraph(确定性);拖动位置 localStorage;MiniMap 常开。
// 话术图 Phase2(spec §7,2026-09-18):意图节点(type="intent")+绑定边(kind="binding")
// 的渲染/点选/删除/调色盘也在此;图数据(graphDoc)与落库归 page(Task 8)。

import {
  memo, useCallback, useEffect, useMemo, useRef, useState,
  type CSSProperties, type MouseEvent as ReactMouseEvent, type RefObject,
} from "react";
import {
  Background, Controls, Handle, MiniMap, Position, ReactFlow, useReactFlow,
  type Edge, type EdgeChange, type Node, type NodeChange, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  deriveGraph, LOCAL_POS_KEY, parseTemplateSteps,
  type CanvasIntentNode, type FlowStep, type GraphDoc, type GraphIntent, type Pt, type QaRow,
} from "@/lib/qa-canvas";

export type TemplateRow = { id: string; name?: string; steps_json?: string; language?: string };

const LANG_LABEL: Record<string, string> = { zh: "普", cantonese: "粤", en: "EN" };
const SOURCE_LABEL: Record<string, string> = { curated: "精选", mined: "挖掘", imported: "导入" };

// 拖拽柄外观(源=左/目标=右;边由柄锚定,无柄时 v12 报 008 且边不渲染)。
const HANDLE_STYLE: CSSProperties = {
  width: 8, height: 8, background: "var(--live)", border: "1px solid var(--card-border)",
};

function QaEntryNode({ data }: NodeProps) {
  const d = data as QaRow & { isHead: boolean; canned?: "ok" | "missing"; canEdit: boolean };
  const dim = d.enabled === false;
  return (
    <div
      className={`w-[280px] rounded-lg border bg-muted/60 p-3 text-xs ${dim ? "opacity-50" : ""} ${d.isHead ? "border-(--live)" : "border-(--card-border)"}`}
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
        <span className="rounded-sm bg-muted px-1 text-[10px]">{LANG_LABEL[String(d.lang ?? "zh")] ?? d.lang}</span>
        <span className="rounded-sm bg-muted px-1 text-[10px]">命中 {Number(d.hit_count ?? 0)}</span>
        {d.source && <span className="rounded-sm bg-muted px-1 text-[10px]">{SOURCE_LABEL[d.source] ?? d.source}</span>}
        {Number(d.priority ?? 10) !== 10 && (
          <span className="rounded-sm bg-sky-100 px-1 text-[10px] text-sky-700" title="匹配优先级：小者先">P{Number(d.priority ?? 10)}</span>
        )}
        {d.canned === "missing" && <span className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700">缺录音</span>}
        {d.canned === "ok" && <span className="rounded-sm bg-emerald-100 px-1 text-[10px] text-emerald-700">录音✓</span>}
        {d.enabled === false && <span className="rounded-sm bg-muted px-1 text-[10px]">停用</span>}
        {!d.canEdit && <span className="rounded-sm bg-muted px-1 text-[10px]" title="共享只读">🔒</span>}
      </div>
    </div>
  );
}

function QaStepNode({ data }: NodeProps) {
  const d = data as { index: number; goal: string; refFirstLine: string; virtual: boolean };
  return (
    <div className={`w-[200px] rounded-lg border p-3 text-xs ${d.virtual ? "border-dashed border-(--card-border) muted" : "border-(--live) bg-(--live-soft)"}`}>
      {/* 只作连线落点(挂步骤),不给 source 柄(连线只准从条目拖出);虚拟「全程通用」不接。 */}
      <Handle type="target" position={Position.Right} isConnectable={!d.virtual} style={HANDLE_STYLE} />
      <p className="font-medium">{d.virtual ? "全程通用" : `第 ${d.index + 1} 步 · ${d.goal}`}</p>
      {!d.virtual && <p className="mt-1 line-clamp-2 muted">{d.refFirstLine}</p>}
    </div>
  );
}

/** 意图节点(话术图 Phase 2,spec §7):锚定话术步骤的意图卡,禁用态置灰照渲染。 */
const IntentNode = memo(function IntentNode({ data, selected }: NodeProps) {
  const d = data as { intent: GraphIntent };
  return (
    <div
      className={`w-[220px] rounded-lg border px-3 py-2 shadow-sm ${
        d.intent.enabled ? "border-amber-300 bg-amber-50" : "border-(--card-border) bg-muted/60 opacity-50"
      } ${selected ? "ring-2 ring-amber-400" : ""}`}
    >
      {/* 柄恒渲染(v12 解析边端点时两端都要取到 Handle,缺=008 且边整体不渲染),
          源柄在右(绑定边目标全是脊柱/卫星道,在意图道右侧)。
          绑定关系只在意图编辑器里建,故两枚柄双闸钉死不可连——否则从条目拖到意图节点
          会掉进 onConnect 的兜底分支、落成一条以 "intent:*" 为簇头的脏簇边。 */}
      <Handle
        type="target" position={Position.Left}
        isConnectable={false} isConnectableStart={false} style={HANDLE_STYLE}
      />
      <Handle
        type="source" position={Position.Right}
        isConnectable={false} isConnectableStart={false} style={HANDLE_STYLE}
      />
      <div className="flex items-center gap-1.5 text-[13px] font-medium">
        <span aria-hidden>🎯</span>
        {/* min-w-0:flex 子项默认 min-width:auto=内容宽,不加则 truncate 对长 label 不生效 */}
        <span className="min-w-0 truncate">{d.intent.label || "(未命名意图)"}</span>
        {!d.intent.enabled && <span className="ml-auto shrink-0 text-[10px] muted">已停用</span>}
      </div>
      <div className="mt-1 truncate text-[11px] muted">{d.intent.keywords.join(" / ")}</div>
    </div>
  );
});

const NODE_TYPES = { qaEntry: QaEntryNode, qaStep: QaStepNode, intent: IntentNode };

// 「＋ 意图」调色盘(spec §7):作为 <ReactFlow> 的子节点渲染——useReactFlow 的
// screenToFlowPosition 要 provider(v12 的 ReactFlow 自带 provider 且 children 落在
// 非变换层,与 MiniMap 同层),放外层 div 就得把整个画布包进 ReactFlowProvider。
// 左下角让开 Controls(默认 bottom-left,26px×3 叠层):同行右移到 left-16。
function IntentPalette({ wrapperRef, onAddIntent }: {
  wrapperRef: RefObject<HTMLDivElement | null>;
  onAddIntent?: (flowPos: Pt) => void;
}) {
  const { screenToFlowPosition } = useReactFlow();
  return (
    <div className="nodrag nopan absolute bottom-4 left-16 z-10 flex gap-2">
      <button
        type="button"
        className="rounded-md border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-100"
        onClick={() => {
          const bounds = wrapperRef.current?.getBoundingClientRect();
          onAddIntent?.(screenToFlowPosition({
            x: (bounds?.left ?? 0) + (bounds?.width ?? 800) / 2,
            y: (bounds?.top ?? 0) + (bounds?.height ?? 600) / 2,
          }));
        }}
      >
        ＋ 意图
      </button>
    </div>
  );
}

/** 节点 id → 意图 id(Task 6 的 id 形态 "intent:<intentId>")。 */
function intentIdOf(nodeId: string): string {
  return nodeId.startsWith("intent:") ? nodeId.slice("intent:".length) : nodeId;
}

// 上抛边载荷的结构收窄(仅取 page 关心的字段,不 as never 逃逸)。
export type CanvasEdgeHit = { id: string; source: string; target: string; data?: { kind?: string } };

export default function QaCanvasView(props: {
  accountId: string;
  rows: QaRow[];
  templates: TemplateRow[];
  templateId: string;
  onTemplateChange: (id: string) => void;
  canned: Record<string, { state: "ok" | "missing" }>;
  canEditRow: (row: QaRow) => boolean;
  onNodeClick: (row: QaRow) => void;
  onPaneDoubleClick: (pt: { x: number; y: number }) => void;
  /** 右键条目(page 提供菜单:编辑/启停/删除/试听/重新录音[manager]);不给时仅吞掉浏览器默认菜单。 */
  onNodeContextMenu?: (row: QaRow, e: ReactMouseEvent) => void;
  /** 右键边(page 提供菜单:解除连线,spec §4.4「右键解除」)。 */
  onEdgeContextMenu?: (edge: CanvasEdgeHit, e: ReactMouseEvent) => void;
  onConnectCluster: (fromId: string, toId: string) => void;
  onDisconnect: (edge: CanvasEdgeHit) => void;
  onStepConnect: (entryId: string, stepIndex: number) => void;
  /** 补齐录音(仅主管注入;不给=按钮不渲染,user 的 403 由闸兜底)。 */
  onPregenAll?: () => void;
  pregenBusy?: boolean;
  /** 话术图(spec §7):意图节点+绑定边的派生输入;缺省=无图,Phase1 视图零变化。 */
  graphDoc?: GraphDoc;
  /** 意图图可编辑(归属判定归 Task 8):缺省/false=调色盘不渲染、意图节点不可删。 */
  canEditGraph?: boolean;
  /** 新建意图:落点=视口中心流坐标(page 落库后回传新 graphDoc 重渲染)。 */
  onAddIntent?: (flowPos: Pt) => void;
  /** 点选意图节点=开意图编辑器(intentId 已剥 "intent:" 前缀)。 */
  onOpenIntentEditor?: (intentId: string) => void;
  /** Delete 键删意图(page 落库时连带其绑定;仅 canEditGraph 时意图节点进删除集)。 */
  onDeleteIntent?: (intentId: string) => void;
}) {
  const { rows, templates, templateId, canned, canEditRow, accountId, graphDoc } = props;
  // memo 依赖用的稳定布尔(直接依赖 props 对象会击穿 memo 全量重建)。
  const canEditGraph = Boolean(props.canEditGraph);
  // 画布外层 div:调色盘落点=该矩形中心(screenToFlowPosition 吃屏幕坐标)。
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [langFilter, setLangFilter] = useState("all");
  const [positions, setPositions] = useState<Record<string, Pt>>({});
  // 边选中态本地持有(spec §4.4:点选边→Delete 键或右键解除):受控图里 RF 的
  // select/remove 变更一律吞掉,删除必须经 page PATCH+refresh 落库。
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);

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
    () => deriveGraph(rows, steps, { langFilter, positions, graph: graphDoc }),
    [rows, steps, langFilter, positions, graphDoc],
  );

  const nodes: Node[] = useMemo(
    () => [
      ...graph.stepNodes.map((n) => ({ ...n, deletable: false, data: { ...n.data } })),
      ...graph.qaNodes.map((n) => ({
        ...n,
        // deletable=false:节点不是可删对象(删除走右键菜单→page),防 Backspace 误删
        // 连带把其连线拉进 getElementsToRemove 的删除集。
        deletable: false,
        data: { ...n.data, canned: canned[String(n.id)]?.state, canEdit: canEditRow(n.data) },
      })),
      // 意图节点:图的可删对象(删除=从图移除意图+其绑定),故 deletable 跟 canEditGraph
      // 走——不可编辑时 deletable=false,Backspace 结构性进不了删除集。
      ...graph.nodes
        .filter((n): n is CanvasIntentNode => n.type === "intent")
        .map((n) => ({ ...n, deletable: canEditGraph, data: { ...n.data } })),
    ],
    [graph, canned, canEditRow, canEditGraph],
  );
  const edges: Edge[] = useMemo(
    () =>
      graph.edges.map((e) => {
        const sel = e.id === selectedEdgeId;
        if (e.data.kind === "spine") {
          // 脊柱顺序连线（展示性）：细实线读流程走向，不可选/不可删/不响应右键。
          return {
            ...e,
            selectable: false,
            deletable: false,
            style: { stroke: "var(--muted-foreground)", strokeWidth: 2 },
          };
        }
        if (e.data.kind === "binding") {
          // 绑定边(意图→目标,spec §7):虚线 amber 与实线簇边/步骤边区分;可删跟 canEditGraph
          // (删除=从图移除绑定,沿既有簇边删除模式经 onEdgesDelete/右键菜单上抛 page)。
          return {
            ...e,
            selected: sel,
            selectable: true,
            deletable: canEditGraph,
            style: {
              stroke: sel ? "var(--live)" : "#d97706",
              strokeWidth: sel ? 2.4 : 1.6,
              strokeDasharray: "6 4",
            },
            labelStyle: { fill: "#92400e", fontSize: 11 },
            labelBgStyle: { fill: "#fef3c7" },
          };
        }
        return {
          ...e,
          selected: sel,
          animated: e.data.kind === "step",
          // 内联 style 会盖掉 RF 的 .selected 高亮,选中反馈在此显式给出。
          style: e.data.kind === "cluster"
            ? { stroke: "var(--live)", strokeWidth: sel ? 3 : 1.5 }
            : { stroke: sel ? "var(--live)" : "var(--muted-foreground)", strokeDasharray: "4 3", strokeWidth: sel ? 2 : 1 },
          labelStyle: { fontSize: 10 },
        };
      }),
    [graph, selectedEdgeId, canEditGraph],
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

  // 受控边:select/remove 变更一律不落内部状态(选中态=selectedEdgeId 本地持有,
  // 删除经 onEdgesDelete 上抛 page PATCH+refresh;静默吞掉防受控图分叉)。
  const onEdgesChange = useCallback((_changes: EdgeChange[]) => {}, []);

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <select
          className="rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)"
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
            className={`btn-ghost text-xs ${langFilter === k ? "border-(--live) text-(--live-ink)" : "muted"}`}
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
        {props.onPregenAll && (
          <button className="btn-ghost text-xs" disabled={props.pregenBusy} onClick={props.onPregenAll}>
            {props.pregenBusy ? "录音生成中…" : "补齐录音"}
          </button>
        )}
      </div>
      {/* 双击空白新建(spec §4.4):@xyflow 12.11 无 onPaneDoubleClick,
          用包裹层 onDoubleClick + 落点判 pane 兜出,并关掉双击缩放避免手势打架;
          坐标上抛 page,Phase1 仅开新建表单(落点插入待 Phase2)。 */}
      <div
        ref={wrapperRef}
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
          deleteKeyCode={["Backspace", "Delete"]}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeDragStop={onNodeDragStop}
          onPaneClick={() => setSelectedEdgeId(null)}
          onNodeClick={(_, node) => {
            setSelectedEdgeId(null); // 点节点=去选边,防 Backspace 误触发边解除
            if (node.type === "intent") {
              props.onOpenIntentEditor?.(intentIdOf(node.id)); // 点意图=开意图编辑器(spec §7)
              return;
            }
            const row = rows.find((r) => String(r.id) === node.id);
            if (row) props.onNodeClick(row);
          }}
          onNodeContextMenu={(e, node) => {
            e.preventDefault();
            const row = rows.find((r) => String(r.id) === node.id);
            row && props.onNodeContextMenu?.(row, e);
          }}
          onEdgeClick={(_, edge) => {
            // spec §4.4:点选边=仅选中,解除走 Delete 键或右键菜单(Task 6 的点击 confirm 已收口)。
            setSelectedEdgeId(edge.id);
          }}
          onEdgeContextMenu={(e, edge) => {
            e.preventDefault();
            if ((edge.data as { kind?: string } | undefined)?.kind === "spine") return; // 脊柱线不可解除
            const hit = edge as CanvasEdgeHit;
            setSelectedEdgeId(edge.id);
            props.onEdgeContextMenu?.(hit, e);
          }}
          onBeforeDelete={async ({ nodes: delNodes, edges: delEdges }) => {
            // 删意图节点:RF 会把挂在它身上的绑定边一并放进删除集(deleteElements 里
            // onEdgesDelete 先于 onNodesDelete 触发),那条绑定边就会既走 onDisconnect 又走
            // onDeleteIntent 的「连带绑定」=同一批上抛两次(page 两次读改写会互相打架)。
            // 意图删除语义已含连带绑定,故把源节点是本次被删意图的绑定边从删除集摘掉。
            const intentIds = new Set(delNodes.filter((n) => n.type === "intent").map((n) => n.id));
            // 半接线护栏(Task 8 review):没有 onDeleteIntent 时意图删除语义不存在,摘绑定边会
            // 让那批边既不走 onDisconnect 也不走 onDeleteIntent=静默丢;原样放行交给框架处理。
            if (intentIds.size === 0 || !props.onDeleteIntent) return { nodes: delNodes, edges: delEdges };
            return {
              nodes: delNodes,
              edges: delEdges.filter(
                (e) => !((e.data as { kind?: string } | undefined)?.kind === "binding" && intentIds.has(e.source)),
              ),
            };
          }}
          onNodesDelete={(deleted) => {
            // 意图节点删除(spec §7):deletable 已按 canEditGraph 钉死,能进删除集即已过闸。
            for (const node of deleted) {
              if (node.type !== "intent") continue;
              props.onDeleteIntent?.(intentIdOf(node.id));
            }
          }}
          onEdgesDelete={(deleted) => {
            for (const edge of deleted) {
              if ((edge.data as { kind?: string } | undefined)?.kind === "spine") continue;
              props.onDisconnect(edge as CanvasEdgeHit);
            }
            setSelectedEdgeId(null);
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
        >
          <Background gap={24} />
          <Controls />
          <MiniMap pannable zoomable />
          {/* 调色盘浮层(spec §7):不可编辑图时不渲染。 */}
          {canEditGraph && <IntentPalette wrapperRef={wrapperRef} onAddIntent={props.onAddIntent} />}
        </ReactFlow>
      </div>
    </div>
  );
}
