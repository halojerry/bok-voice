"use client";

// 流程画布（2026-09-20 易用性改版）：完全受控（draft/onDraftChange）——步骤草稿归
// 工作站页面层，「未保存/应用」全局按钮是唯一保存入口；与步骤列表编辑同一份草稿，
// 视图来回切不丢修改。画布=纵向步骤工作流：脊柱瀑布（实线=讲完默认进下一步）+
// 左栏意图卡（虚线=听到关键词跳到对应步骤）+ 点步开「AI 怎么说」抽屉。
// 意图=只读 overlay（编辑在「意图管理」tab）。

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background, Controls, Handle, MiniMap, Position, ReactFlow,
  type Edge, type Node, type NodeChange, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useSession } from "@/components/session-context";
import type { FlowStep, TemplateRow } from "@/components/template-editor";
import { VarTextarea } from "@/components/var-insert";
import type { GraphDoc } from "@/lib/qa-canvas";
import {
  layoutFlow, parseStepRefParts, serializeStepRef, UNGROUPED_LANE,
  type FlowNode, type StepRefParts, type StepBranch,
} from "@/lib/flow-canvas";

// 情绪下拉选项与 template-editor 表单同款（罐头物化烧进音频,实时回复不受影响）。
const EMOTIONS: [string, string][] = [
  ["", "自动"],
  ["calm", "平稳自然"],
  ["sad", "低沉柔和（致歉/安抚）"],
  ["happy", "轻快亲切"],
  ["surprised", "惊讶上扬"],
];
const EMOTION_LABEL: Record<string, string> = {
  calm: "平稳", sad: "柔和", happy: "亲切", surprised: "上扬",
};

// 拖拽柄外观（qa-canvas-view 同款;本画布边全部由布局派生,柄恒不可手连）。
const HANDLE_STYLE = {
  width: 8, height: 8, background: "var(--live)", border: "1px solid var(--card-border)",
};

const inputCls =
  "w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)";

// —— 纯渲染节点 ——
// 步节点（工作流卡）：目的 + AI 说的话预览 + 徽标 + 「客户这样说」chip；点击开抽屉。
function FlowStepNode({ data }: NodeProps) {
  const d = data as Extract<FlowNode, { kind: "step" }> & { onOpen: (index: number) => void };
  return (
    <div
      className="w-[300px] cursor-pointer rounded-lg border border-(--live) bg-(--live-soft) p-3 text-xs hover:bg-accent"
      title="点这一步，编辑 AI 怎么说、怎么应对"
      onClick={() => d.onOpen(d.index)}
    >
      {/* 脊柱入边（上）+ 意图跳转入边（左）+ 脊柱出边（下） */}
      <Handle type="target" position={Position.Top} id="t" isConnectable={false} style={HANDLE_STYLE} />
      <Handle type="target" position={Position.Left} id="l" isConnectable={false} style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Bottom} id="b" isConnectable={false} style={HANDLE_STYLE} />
      <p className="flex items-center gap-1.5 font-medium">
        <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-(--live) px-1 text-[10px] font-bold text-white">
          {d.index + 1}
        </span>
        <span className="min-w-0 flex-1 truncate">{d.goal || "(没写目的)"}</span>
      </p>
      {d.index === 0 && (
        <p className="mt-0.5 text-[10px] text-(--live-ink)">● 电话接通先讲这一步</p>
      )}
      <p className="mt-1 line-clamp-2 muted">{d.scriptFirst || "(还没写内容)"}</p>
      <div className="mt-2 flex flex-wrap items-center gap-1">
        {d.say && <span className="rounded-sm bg-sky-100 px-1 text-[10px] text-sky-700">逐字照念</span>}
        {d.say && d.emotion && (
          <span className="rounded-sm bg-muted px-1 text-[10px]">{EMOTION_LABEL[d.emotion] ?? d.emotion}</span>
        )}
        {d.scene && <span className="rounded-sm bg-violet-100 px-1 text-[10px] text-violet-700">{d.scene}</span>}
        {d.jumpIn > 0 && (
          <span className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700">{d.jumpIn} 个意图会跳到这</span>
        )}
      </div>
      {(d.branches.length > 0 || d.branchMore > 0) && (
        <div className="mt-1.5 border-t border-(--card-border) pt-1.5">
          <p className="text-[9px] muted">客户这样说时有专门应对</p>
          <div className="mt-0.5 flex flex-wrap gap-1">
            {d.branches.map((b, i) => (
              <span
                key={i}
                className="rounded-full border border-(--card-border) bg-background px-1.5 py-0.5 text-[10px]"
                title={`客户${b.cond} → ${b.resp}`}
              >
                {b.cond || "(空)"}
              </span>
            ))}
            {d.branchMore > 0 && (
              <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] muted">+{d.branchMore}</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// 意图节点（只读 overlay,左栏）：跳转边在布局层派生;编辑在「意图管理」tab。
function FlowIntentNode({ data }: NodeProps) {
  const d = data as Extract<FlowNode, { kind: "intent" }>;
  return (
    <div
      className={`w-[240px] rounded-lg border px-3 py-2 text-xs shadow-sm ${
        d.enabled ? "border-amber-300 bg-amber-50" : "border-(--card-border) bg-muted/60 opacity-50"
      }`}
    >
      <Handle type="source" position={Position.Right} id="r" isConnectable={false} style={HANDLE_STYLE} />
      <p className="flex items-center gap-1.5 font-medium">
        <span aria-hidden>🎯</span>
        <span className="min-w-0 truncate">{d.label}</span>
      </p>
      <p className="mt-1 truncate muted">听到：{d.keywords.join(" / ") || "—"}</p>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {d.playQa && <span className="rounded-sm bg-emerald-100 px-1 text-[10px] text-emerald-700">播快答</span>}
        {d.judge && (
          <span
            className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700"
            title="关键词没听清时，AI 会按这段描述再判断一次"
          >
            智能判定
          </span>
        )}
        {!d.enabled && <span className="rounded-sm bg-muted px-1 text-[10px]">已停用</span>}
      </div>
    </div>
  );
}

const NODE_TYPES = { flowStep: FlowStepNode, flowIntent: FlowIntentNode };

// —— 步骤编辑抽屉（右侧固定面板,非 modal）：全部字段受控,写路径归宿主 ——
function AnswerDrawer(props: {
  index: number;
  step: FlowStep;
  scenes: string[];
  parts: StepRefParts;
  readOnly: boolean;
  onGoal: (v: string) => void;
  onScene: (v: string) => void;
  onSay: (v: boolean) => void;
  onEmotion: (v: string) => void;
  onParts: (next: StepRefParts) => void;
  onClose: () => void;
}) {
  const { parts, readOnly } = props;
  const setBranch = (i: number, patch: Partial<StepBranch>) =>
    props.onParts({ ...parts, branches: parts.branches.map((b, j) => (j === i ? { ...b, ...patch } : b)) });
  return (
    <aside className="w-[340px] shrink-0 space-y-2 self-start rounded-lg border border-(--card-border) bg-muted/40 p-3">
      <div className="flex items-center justify-between">
        <span className="label">第 {props.index + 1} 步：AI 怎么说</span>
        <button className="btn-ghost px-2 py-0.5 text-xs" onClick={props.onClose}>收起 ✕</button>
      </div>
      <p className="text-[11px] muted">改动记入右上角「未保存」，点「应用」才会生效。</p>
      <label className="block">
        <span className="text-xs muted">这一步的目的（给你自己看的备注）</span>
        <input
          className={`mt-0.5 ${inputCls}`}
          value={props.step.goal}
          disabled={readOnly}
          placeholder="如：确认对方身份"
          onChange={(e) => props.onGoal(e.target.value)}
        />
      </label>
      <label className="block">
        <span className="text-xs muted">分组标签（可选，只影响画布上的颜色归类）</span>
        <select
          className={`mt-0.5 ${inputCls}`}
          value={props.step.scene ?? ""}
          disabled={readOnly}
          onChange={(e) => props.onScene(e.target.value)}
        >
          <option value="">不分组</option>
          {props.scenes.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-1.5 text-[11px] muted">
        <input
          type="checkbox"
          className="size-3 accent-(--live)"
          checked={props.step.say === true}
          disabled={readOnly}
          onChange={(e) => props.onSay(e.target.checked)}
        />
        逐字照念（AI 一字不差念下面第一行，不自由发挥）
      </label>
      {props.step.say === true && (
        <label className="flex items-center gap-1.5 text-[11px] muted">
          念的语气
          <select
            className="rounded-lg border border-(--card-border) bg-transparent px-1.5 py-0.5 text-xs outline-hidden focus:border-(--live)"
            value={props.step.emotion ?? ""}
            disabled={readOnly}
            onChange={(e) => props.onEmotion(e.target.value)}
          >
            {EMOTIONS.map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </select>
          <span className="text-[10px]">录音时用这个语气</span>
        </label>
      )}
      <div>
        <span className="text-xs muted">AI 主要说的话（写要点即可，AI 用自己的语气讲）</span>
        <div className="mt-0.5">
          <VarTextarea
            className={`h-24 resize-none ${inputCls}`}
            value={parts.script}
            disabled={readOnly}
            onChange={(v) => props.onParts({ ...parts, script: v })}
          />
        </div>
      </div>
      <div>
        <span className="text-xs muted">客户如果这样说 → 就这样答（每条一行）</span>
        <div className="mt-1 space-y-1">
          {parts.branches.map((b, i) => (
            <div key={i} className="flex items-center gap-1">
              <input
                className={inputCls}
                value={b.cond}
                disabled={readOnly}
                placeholder="客户说…（如：问为什么赔）"
                onChange={(e) => setBranch(i, { cond: e.target.value })}
              />
              <input
                className={inputCls}
                value={b.resp}
                disabled={readOnly}
                placeholder="AI 就答…"
                onChange={(e) => setBranch(i, { resp: e.target.value })}
              />
              <button
                className="btn-ghost shrink-0 px-1.5 py-0 text-xs text-red-600"
                disabled={readOnly}
                onClick={() => props.onParts({ ...parts, branches: parts.branches.filter((_, j) => j !== i) })}
              >
                删
              </button>
            </div>
          ))}
          <button
            className="btn-ghost px-2 py-0.5 text-xs"
            disabled={readOnly}
            onClick={() => props.onParts({ ...parts, branches: [...parts.branches, { cond: "", resp: "" }] })}
          >
            ＋ 加一条应对
          </button>
        </div>
      </div>
      <label className="block">
        <span className="text-xs muted">给 AI 的补充提醒（每次都记住的事实，如必讲的号码）</span>
        <textarea
          className={`mt-0.5 h-16 resize-none ${inputCls}`}
          value={parts.notes}
          disabled={readOnly}
          placeholder="如：赔付只能进微信零钱"
          onChange={(e) => props.onParts({ ...parts, notes: e.target.value })}
        />
      </label>
    </aside>
  );
}

export default function FlowCanvas(props: {
  /** 编辑的模板行（owner 判定源）;null=无可编辑内容。 */
  tpl: TemplateRow | null;
  /** 话术图（parseGraphDoc 结果）,只读 overlay 数据源。 */
  graph: GraphDoc;
  /** 步骤草稿（工作站层持有）;本组件只上报变更,不做保存。 */
  draft: FlowStep[];
  onDraftChange: (next: FlowStep[]) => void;
}) {
  const { graph, draft } = props;
  const session = useSession();
  const tplId = String(props.tpl?.id ?? "");
  // B4 owner 只读兜底（与 TemplateEditor 同款口径;CP PUT/publish 闸兜底）。
  const isManager = Boolean(
    session && (session.anonymous || session.role === "admin" || session.role === "root"),
  );
  const uid = session?.user_id ?? "";
  const readOnly = Boolean(tplId) && !isManager && !(uid !== "" && String(props.tpl?.owner_user_id ?? "") === uid);

  const [drawerIdx, setDrawerIdx] = useState<number | null>(null);
  const [parts, setParts] = useState<StepRefParts | null>(null);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});

  useEffect(() => {
    setDrawerIdx(null);
    setPositions({});
    // 只跟 tpl.id 走（应用保存后外层重拉同 id,画布布局重锚但草稿已=落库值）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tplId]);

  // 抽屉打开时把该步 ref 拆成 主要内容/应对/提醒 三件（受控编辑实时序列化回草稿）。
  useEffect(() => {
    if (drawerIdx === null) {
      setParts(null);
      return;
    }
    const s = draft[drawerIdx];
    setParts(s ? parseStepRefParts(s.ref) : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawerIdx]);

  const layout = useMemo(() => layoutFlow(draft, graph), [draft, graph]);
  const scenes = useMemo(
    () => [...new Set(draft.map((s) => (s.scene ?? "").trim()).filter(Boolean))],
    [draft],
  );

  // —— 写路径（全部上报宿主草稿;保存=工作站层「应用」） ——
  const updateStep = useCallback(
    (idx: number, patch: Partial<FlowStep>) =>
      props.onDraftChange(draft.map((s, i) => (i === idx ? { ...s, ...patch } : s))),
    [draft, props.onDraftChange],
  );
  // 抽屉三件编辑：parts 即时回显 + 序列化写回该步 ref（草稿永远拿到最新值）。
  // 普通函数（非 useCallback）：闭包持当轮 drawerIdx,不在状态更新器里做副作用。
  function updateParts(next: StepRefParts) {
    setParts(next);
    if (drawerIdx === null) return;
    const ref = serializeStepRef(next);
    props.onDraftChange(draft.map((s, i) => (i === drawerIdx ? { ...s, ref } : s)));
  }

  // —— RF 受控图 ——
  const openDrawer = useCallback((index: number) => setDrawerIdx(index), []);
  const nodes: Node[] = useMemo(
    () =>
      layout.nodes.map((n) => {
        const position = positions[n.id] ?? { x: n.x, y: n.y };
        if (n.kind === "step") {
          return {
            id: n.id, type: "flowStep" as const, position, deletable: false,
            data: { ...n, onOpen: openDrawer },
          };
        }
        return { id: n.id, type: "flowIntent" as const, position, deletable: false, data: { ...n } };
      }),
    [layout, positions, openDrawer],
  );
  const edges: Edge[] = useMemo(
    () =>
      layout.edges.map((e) => {
        if (e.kind === "spine") {
          return {
            id: e.id, source: e.source, target: e.target,
            sourceHandle: "b", targetHandle: "t",
            selectable: false, deletable: false,
            label: e.label,
            style: { stroke: "var(--muted-foreground)", strokeWidth: 2 },
            labelStyle: { fontSize: 10 },
          };
        }
        if (e.kind === "thenjump") {
          return {
            id: e.id, source: e.source, target: e.target,
            sourceHandle: "r", targetHandle: "l",
            selectable: false, deletable: false,
            label: e.label,
            style: { stroke: "#059669", strokeWidth: 1.6, strokeDasharray: "2 4" },
            labelStyle: { fill: "#065f46", fontSize: 10 },
            labelBgStyle: { fill: "#d1fae5" },
          };
        }
        return {
          id: e.id, source: e.source, target: e.target,
          sourceHandle: "r", targetHandle: "l",
          selectable: false, deletable: false,
          label: e.label,
          style: { stroke: "#d97706", strokeWidth: 1.8, strokeDasharray: "6 4" },
          labelStyle: { fill: "#92400e", fontSize: 10 },
          labelBgStyle: { fill: "#fef3c7" },
        };
      }),
    [layout],
  );
  // 受控图：只消化拖动位置（选择/删除一律吞——结构改动全走抽屉/宿主）。
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

  const drawerStep = drawerIdx !== null ? draft[drawerIdx] : undefined;

  return (
    <section className="card space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="label">流程画布</span>
        <span className="text-[11px] muted">
          从上到下=通话顺序；左边意图卡=听到某些话就跳到箭头指的步骤；点步骤卡即可编辑
        </span>
      </div>
      {readOnly && (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
          共享话术由主管维护；你可以查看但不能修改。
        </p>
      )}
      <div className="flex items-stretch gap-3">
        <div className="h-[560px] min-w-0 flex-1 rounded-lg border border-(--card-border)">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            fitView
            fitViewOptions={{ padding: 0.15 }}
            minZoom={0.2}
            deleteKeyCode={null}
            onNodesChange={onNodesChange}
            onEdgesChange={() => {}}
          >
            <Background gap={24} />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>
        </div>
        {drawerStep && parts && drawerIdx !== null && (
          <AnswerDrawer
            index={drawerIdx}
            step={drawerStep}
            scenes={scenes}
            parts={parts}
            readOnly={readOnly}
            onGoal={(v) => updateStep(drawerIdx, { goal: v })}
            onScene={(v) => updateStep(drawerIdx, { scene: v })}
            onSay={(v) => updateStep(drawerIdx, { say: v })}
            onEmotion={(v) => updateStep(drawerIdx, { emotion: v })}
            onParts={updateParts}
            onClose={() => setDrawerIdx(null)}
          />
        )}
      </div>
      {draft.length === 0 && (
        <p className="text-xs muted">这套话术还没有步骤——切到「列表编辑」点「+ 加一步」或「填示例」。</p>
      )}
    </section>
  );
}
