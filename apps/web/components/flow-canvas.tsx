"use client";

// 流程画布（W2 T2）：话术步的场景泳道可视化 + 答法抽屉 + steps_json 保存。
// 三层范式照 qa-canvas-view：节点/抽屉纯渲染上抛,本组件（宿主）独占 draft 状态与写路径;
// 布局与 ref 拆装在 lib/flow-canvas（纯函数）。意图=只读 overlay（jump_step→跳转边、
// play_qa→「播快答」徽标、judge→「判据」徽标）,编辑深链问答画布,W2 不搬图写路径。
// 保存=serialize 全部步（含 scene）→ stepsToJson → updateTemplate({steps_json})
// （CP PUT exclude_unset 部分更新,只动 steps_json 不抹其余字段）。

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background, Controls, Handle, MiniMap, Position, ReactFlow,
  type Edge, type Node, type NodeChange, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { api } from "@/lib/api";
import { ErrorState } from "@/components/app-shell";
import { useSession } from "@/components/session-context";
import { jsonToSteps, stepsToJson, type FlowStep, type TemplateRow } from "@/components/template-editor";
import type { GraphDoc } from "@/lib/qa-canvas";
import {
  layoutFlow, parseStepRefParts, serializeStepRef,
  findUnparsedDirectives,
  type FlowNode, type StepRefParts, type StepBranch, UNGROUPED_LANE,
} from "@/lib/flow-canvas";

// 情绪下拉选项与 template-editor 表单同款（罐头物化烧进音频,实时回复不受影响）。
const EMOTIONS: [string, string][] = [
  ["", "自动(不下发,按文本匹配)"],
  ["calm", "平稳自然"],
  ["sad", "低沉柔和(致歉/安抚)"],
  ["happy", "轻快亲切"],
  ["surprised", "惊讶上扬"],
];
const EMOTION_LABEL: Record<string, string> = Object.fromEntries(EMOTIONS.filter(([v]) => v));

// 拖拽柄外观（qa-canvas-view 同款;本画布边全部由布局派生,柄恒不可手连）。
const HANDLE_STYLE = {
  width: 8, height: 8, background: "var(--live)", border: "1px solid var(--card-border)",
};

const inputCls =
  "w-full rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)";

// —— 纯渲染节点 ——
// 步节点：goal + 正稿首行截断 + 徽标（直念/情绪/意图跳入数）;点击开答法抽屉。
function FlowStepNode({ data }: NodeProps) {
  const d = data as Extract<FlowNode, { kind: "step" }> & { onOpen: (index: number) => void };
  return (
    <div
      className="w-[260px] cursor-pointer rounded-lg border border-(--live) bg-(--live-soft) p-3 text-xs hover:bg-accent"
      title="点击编辑这一步的答法"
      onClick={() => d.onOpen(d.index)}
    >
      <Handle type="target" position={Position.Top} id="t" isConnectable={false} style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Bottom} id="b" isConnectable={false} style={HANDLE_STYLE} />
      <p className="line-clamp-2 font-medium">
        第 {d.index + 1} 步 · {d.goal || "(无目标)"}
      </p>
      <p className="mt-1 line-clamp-1 muted">{d.scriptFirst || "(无正稿)"}</p>
      <div className="mt-2 flex flex-wrap items-center gap-1">
        {d.say && <span className="rounded-sm bg-sky-100 px-1 text-[10px] text-sky-700">直念</span>}
        {d.say && d.emotion && (
          <span className="rounded-sm bg-muted px-1 text-[10px]">{EMOTION_LABEL[d.emotion] ?? d.emotion}</span>
        )}
        {d.jumpIn > 0 && (
          <span className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700">{d.jumpIn} 意图跳入</span>
        )}
      </div>
    </div>
  );
}

// 场景泳道头：名字行内可编辑（受控上抛 onChangeScene(prev,new);逐键重映射同名步）。
function LaneHeaderNode({ data }: NodeProps) {
  const d = data as Extract<FlowNode, { kind: "lane" }> & {
    canEdit: boolean;
    onChangeScene: (prev: string, next: string) => void;
  };
  return (
    <div className="w-[180px] rounded-lg border border-dashed border-(--card-border) bg-muted/60 p-2">
      <p className="mb-1 text-[10px] muted">场景</p>
      <input
        className={`${inputCls} nodrag font-medium`}
        value={d.name}
        disabled={!d.canEdit}
        placeholder="未分组"
        title={d.canEdit ? "改场景名：该泳道全部步随之改名" : undefined}
        onChange={(e) => d.onChangeScene(d.name, e.target.value)}
      />
    </div>
  );
}

// 意图节点（只读 overlay）：跳转边在布局层派生;徽标=播快答/判据/停用。
function FlowIntentNode({ data }: NodeProps) {
  const d = data as Extract<FlowNode, { kind: "intent" }>;
  return (
    <div
      className={`w-[240px] rounded-lg border px-3 py-2 text-xs shadow-sm ${
        d.enabled ? "border-amber-300 bg-amber-50" : "border-(--card-border) bg-muted/60 opacity-50"
      }`}
    >
      <Handle type="target" position={Position.Left} id="l" isConnectable={false} style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Right} id="r" isConnectable={false} style={HANDLE_STYLE} />
      <p className="flex items-center gap-1.5 font-medium">
        <span aria-hidden>🎯</span>
        <span className="min-w-0 truncate">{d.label}</span>
      </p>
      <p className="mt-1 truncate muted">{d.keywords.join(" / ")}</p>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {d.playQa && <span className="rounded-sm bg-emerald-100 px-1 text-[10px] text-emerald-700">播快答</span>}
        {d.judge && (
          <span className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700" title="关键词未中时由后台 LLM 按判据评估">
            判据
          </span>
        )}
        {!d.enabled && <span className="rounded-sm bg-muted px-1 text-[10px]">已停用</span>}
      </div>
    </div>
  );
}

const NODE_TYPES = { flowStep: FlowStepNode, laneHeader: LaneHeaderNode, flowIntent: FlowIntentNode };

// —— 答法抽屉（右侧固定面板,非 modal）：全部字段受控,写路径归宿主 ——
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
        <span className="label">第 {props.index + 1} 步 · 答法</span>
        <button className="btn-ghost px-2 py-0.5 text-xs" onClick={props.onClose}>收起 ✕</button>
      </div>
      <p className="text-[11px] muted">改动在画布顶部「保存」后落库。</p>
      <label className="block">
        <span className="text-xs muted">目标</span>
        <input
          className={`mt-0.5 ${inputCls}`}
          value={props.step.goal}
          disabled={readOnly}
          placeholder="这一步要达成的目标"
          onChange={(e) => props.onGoal(e.target.value)}
        />
      </label>
      <label className="block">
        <span className="text-xs muted">场景（纯分组,不影响推进顺序）</span>
        <select
          className={`mt-0.5 ${inputCls}`}
          value={props.step.scene ?? ""}
          disabled={readOnly}
          onChange={(e) => props.onScene(e.target.value)}
        >
          <option value="">{UNGROUPED_LANE}</option>
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
        直念(进入该步的当轮逐字念正稿首行)
      </label>
      {props.step.say === true && (
        <label className="flex items-center gap-1.5 text-[11px] muted">
          情绪
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
          <span className="text-[10px]">罐头物化时烧进音频</span>
        </label>
      )}
      <label className="block">
        <span className="text-xs muted">正稿（首个非空行=本步首轮说的话;未知格式行也留在这里,保存原样回写）</span>
        <textarea
          className={`mt-0.5 h-24 resize-none ${inputCls}`}
          value={parts.script}
          disabled={readOnly}
          onChange={(e) => props.onParts({ ...parts, script: e.target.value })}
        />
      </label>
      {findUnparsedDirectives(parts.script).length > 0 && (
        <div className="rounded border border-amber-500/40 bg-amber-500/10 p-1.5 text-[11px] text-amber-300">
          {findUnparsedDirectives(parts.script).length} 行指令引擎不会执行
          （只认「如果客户…→…」与「注意：」；这些行会原样保存，但运行时忽略）：
          <ul className="mt-0.5 list-disc pl-4">
            {findUnparsedDirectives(parts.script).map((l, i) => (
              <li key={i} className="line-clamp-1">{l}</li>
            ))}
          </ul>
        </div>
      )}
      <div>
        <span className="text-xs muted">分支（如果客户…→应答;运行时按客户回应只命中一条）</span>
        <div className="mt-1 space-y-1">
          {parts.branches.map((b, i) => (
            <div key={i} className="flex items-center gap-1">
              <input
                className={inputCls}
                value={b.cond}
                disabled={readOnly}
                placeholder="如果客户…"
                onChange={(e) => setBranch(i, { cond: e.target.value })}
              />
              <input
                className={inputCls}
                value={b.resp}
                disabled={readOnly}
                placeholder="应答…"
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
            ＋ 加分支
          </button>
        </div>
      </div>
      <label className="block">
        <span className="text-xs muted">注意（操作事实,每轮注入;多条每行一条）</span>
        <textarea
          className={`mt-0.5 h-16 resize-none ${inputCls}`}
          value={parts.notes}
          disabled={readOnly}
          placeholder="注意：…"
          onChange={(e) => props.onParts({ ...parts, notes: e.target.value })}
        />
      </label>
    </aside>
  );
}

export default function FlowCanvas(props: {
  /** 编辑的模板行（steps_json/graph_json/owner 判定源）;null=无可编辑内容。 */
  tpl: TemplateRow | null;
  /** 话术图（parseGraphDoc 结果）,只读 overlay 数据源。 */
  graph: GraphDoc;
  /** 保存成功后回调：外层重拉模板行（徽标/各 tab 随之取权威数据）。 */
  onSaved?: () => void;
}) {
  const { graph } = props;
  const session = useSession();
  const tplId = String(props.tpl?.id ?? "");
  // B4 owner 只读兜底（与 TemplateEditor 同款口径;CP PUT/publish 闸兜底）。
  const isManager = Boolean(
    session && (session.anonymous || session.role === "admin" || session.role === "root"),
  );
  const uid = session?.user_id ?? "";
  const readOnly = Boolean(tplId) && !isManager && !(uid !== "" && String(props.tpl?.owner_user_id ?? "") === uid);

  // 草稿步（含 scene）：tpl.id 变化才重锚（保存后外层重拉同 id 不冲掉编辑中状态）。
  const [draft, setDraft] = useState<FlowStep[]>([]);
  const [drawerIdx, setDrawerIdx] = useState<number | null>(null);
  const [parts, setParts] = useState<StepRefParts | null>(null);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft(jsonToSteps(props.tpl?.steps_json));
    setDrawerIdx(null);
    setPositions({});
    setErr(null);
    setOk(false);
    // 只跟 tpl.id 走（TemplateEditor 同款锚定语义）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tplId]);

  // 抽屉打开时把该步 ref 拆成正稿/分支/注意（之后逐键受控编辑,保存前实时序列化回 draft）。
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

  // —— 写路径（宿主独占） ——
  const updateStep = useCallback(
    (idx: number, patch: Partial<FlowStep>) =>
      setDraft((d) => d.map((s, i) => (i === idx ? { ...s, ...patch } : s))),
    [],
  );
  // 抽屉三件编辑：parts 即时回显 + 序列化写回该步 ref（保存按钮永远拿到最新 draft）。
  // 普通函数（非 useCallback）：闭包持当轮 drawerIdx,不在状态更新器里做副作用。
  function updateParts(next: StepRefParts) {
    setParts(next);
    if (drawerIdx === null) return;
    const ref = serializeStepRef(next);
    setDraft((d) => d.map((s, i) => (i === drawerIdx ? { ...s, ref } : s)));
  }
  // 泳道头改名：该场景全部步随之重映射（scene 空的「未分组」泳道头不可改,canEdit 已闸）。
  const renameScene = useCallback((prev: string, next: string) => {
    const name = next.trim();
    if (!name || name === prev) return;
    setDraft((d) => d.map((s) => ((s.scene ?? "") === prev ? { ...s, scene: name } : s)));
  }, []);
  // 新建场景：落一个带场景的空白步并直接开抽屉（空白步保存时被 stepsToJson 过滤,不产生垃圾行）。
  const addScene = useCallback(() => {
    const name = (window.prompt("新场景名称（如：开场 / 谈赔偿 / 收尾）") ?? "").trim();
    if (!name) return;
    if (draft.some((s) => (s.scene ?? "") === name)) return;
    setDraft((d) => (d.some((s) => (s.scene ?? "") === name) ? d : [...d, { goal: "", ref: "", scene: name }]));
    setDrawerIdx(draft.length);
  }, [draft]);

  async function save() {
    if (!tplId) return;
    setSaving(true);
    setErr(null);
    setOk(false);
    try {
      await api.updateTemplate(tplId, { steps_json: stepsToJson(draft) });
      // 空白步（goal+ref 全空）被 stepsToJson 静默过滤——计数提示,防「画了几步存出来少几步」困惑。
      const dropped = draft.filter((s) => !s.goal.trim() && !s.ref.trim()).length;
      if (dropped > 0) setErr(`已忽略 ${dropped} 个空白步（目标与参考说法都为空）。`);
      setOk(true);
      props.onSaved?.();
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  // —— RF 受控图 ——
  const openDrawer = useCallback((index: number) => setDrawerIdx(index), []);
  const nodes: Node[] = useMemo(
    () =>
      layout.nodes.map((n) => {
        const position = positions[n.id] ?? { x: n.x, y: n.y };
        if (n.kind === "lane") {
          return {
            id: n.id, type: "laneHeader" as const, position, deletable: false,
            data: { ...n, canEdit: !readOnly && n.name !== UNGROUPED_LANE, onChangeScene: renameScene },
          };
        }
        if (n.kind === "step") {
          return {
            id: n.id, type: "flowStep" as const, position, deletable: false,
            data: { ...n, onOpen: openDrawer },
          };
        }
        return { id: n.id, type: "flowIntent" as const, position, deletable: false, data: { ...n } };
      }),
    [layout, positions, readOnly, renameScene, openDrawer],
  );
  const edges: Edge[] = useMemo(
    () =>
      layout.edges.map((e) => {
        if (e.kind === "spine") {
          return {
            id: e.id, source: e.source, target: e.target,
            sourceHandle: "b", targetHandle: "t",
            selectable: false, deletable: false,
            style: { stroke: "var(--muted-foreground)", strokeWidth: 2 },
          };
        }
        return {
          id: e.id, source: e.source, target: e.target,
          sourceHandle: "r", targetHandle: "t",
          selectable: false, deletable: false,
          label: "跳转",
          style: { stroke: "#d97706", strokeWidth: 1.6, strokeDasharray: "6 4" },
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
        <span className="text-[11px] muted">点步节点编辑答法；场景=泳道纯分组，不改推进顺序</span>
        <div className="ml-auto flex items-center gap-2">
          {!readOnly && (
            <button className="btn-ghost text-xs" disabled={!tplId} onClick={addScene}>
              ＋ 新建场景
            </button>
          )}
          {!readOnly && (
            <button className="btn-primary px-3 py-1 text-xs" disabled={saving || !tplId} onClick={save}>
              {saving ? "保存中…" : "保存"}
            </button>
          )}
        </div>
      </div>
      {readOnly && (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
          共享话术由主管维护；你可以查看但不能修改。
        </p>
      )}
      {err && <ErrorState message={err} />}
      {ok && !err && <span className="text-sm text-emerald-600">已保存。</span>}
      <div className="flex items-stretch gap-3">
        <div className="h-[560px] min-w-0 flex-1 rounded-lg border border-(--card-border)">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            fitView
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
        <p className="text-xs muted">该模板还没有步骤——回「表单」页签配置或填入示例后，这里会按场景分泳道展示。</p>
      )}
    </section>
  );
}
