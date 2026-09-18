"use client";

// 快答库（B4）：Q→A 检索快路的运营配置面。
// 话务员（user）=「我的 / 共享」两 tab，只能改自己的；主管（admin/root/本地匿名）=全部列表
// + 归属列与归属转移。命中判定按字面措辞，所以条目的问题文本要按客户实际说法写。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { api, authHeaders, type UserRow } from "@/lib/api";
import {
  parseGraphDoc, parseTemplateSteps, resolveClusterTarget, revertCluster,
  type FlowStep, type GraphBinding, type GraphDoc, type GraphIntent,
} from "@/lib/qa-canvas";
import type { TemplateRow } from "@/components/qa-canvas-view";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useSession } from "@/components/session-context";
import { useAccount } from "@/components/account-context";

// 画布视图懒加载(@xyflow/react 仅进画布 chunk,列表视图零负担;spec §4.2)。
const QaCanvasView = dynamic(() => import("@/components/qa-canvas-view"), {
  ssr: false,
  loading: () => <LoadingState />,
});

/** 画布边右键/Delete 上抛的边载荷(与组件 CanvasEdgeHit 同构)。 */
type CanvasEdgeHit = { id: string; source: string; target: string; data?: { kind?: string } };

/** 播放一段音频 blob(录音回放/现场合成共用);开始播放即返回,结束后回收 objectURL。 */
async function playBlob(blob: Blob): Promise<void> {
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  try {
    await audio.play();
    audio.onended = () => URL.revokeObjectURL(url);
    audio.onerror = () => URL.revokeObjectURL(url);
  } catch (e) {
    URL.revokeObjectURL(url);
    throw e;
  }
}

const LANGS = [
  ["zh", "普通话"],
  ["cantonese", "粤语"],
  ["en", "English"],
] as const;

const SCOPE_LABEL: Record<string, string> = { global: "全程通用", step: "指定步骤" };

type QaRow = {
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
  /** 同义簇(qa-canvas Phase1):""=独立条目,非空=挂在 head 行之下。 */
  cluster_head_id?: string;
  /** 匹配优先级(Phase 3.1):小者先,默认 10;≠10 时列表/画布出徽标。 */
  priority?: number;
  created_at?: string;
};

type QaForm = {
  question_text: string;
  answer_text: string;
  lang: string;
  scope: string;
  /** 界面从 1 开始数步骤，入库转成 0 基 step_index（agent 端按步骤下标比对）。 */
  step: number;
  voice_id: string;
  enabled: boolean;
  /** 匹配优先级（Phase 3.1）：小者先，[0,1000]，默认 10。 */
  priority: string;
};

const EMPTY_FORM: QaForm = {
  question_text: "",
  answer_text: "",
  lang: "zh",
  scope: "global",
  step: 1,
  voice_id: "",
  enabled: true,
  priority: "10",
};

/** step_index → 界面步骤号（-1/未设 = 第 1 步）。 */
function displayStep(row: QaRow): number {
  const idx = Number(row.step_index ?? -1);
  return idx >= 0 ? idx + 1 : 1;
}

// ---- 话术图（qa-flow-graph Phase 2 Task 8）：意图/绑定编辑的纯工具 + 编辑器模态 ----
// 契约来源：lib/qa-canvas.ts 的 GraphDoc/GraphIntent/GraphBinding（画布派生）与 CP
// packages/core/flow_graph.py validate_flow_graph（保存严格校验）——前端必须在**保存前**
// 满足同一组约束（label 1-64 字、keywords 非空 ≤32 项且每项 ≤64 字、step 1-999、
// priority 0-1000、play_qa 必带 qa_id、id 形如 int_/bnd_+8 位小写 hex），否则 PUT 必 400。

/** 模板行（画布脊柱输入 + 意图图归属判定）：owner_user_id 由 CP 列表带出（B3 owner 语义）。 */
type QaTemplateRow = TemplateRow & { owner_user_id?: string };

/** 未配置话术图的模板以此起步（与 parseGraphDoc 的坏数据兜底同形）。 */
const EMPTY_GRAPH: GraphDoc = { version: 1, intents: [], bindings: [] };

/**
 * 原文是否**形状合法**的话术图（空串=未启用，合法）。
 * 用于把「合法空图」与「坏 JSON / 版本不符」区分开：两者经 parseGraphDoc 都得到空图，
 * 但后者意味着库里存着一份我们读不懂的配置——此时必须**禁止写入**（否则第一次编辑就把
 * 那份配置覆盖成空 doc），并给出可解释的警告。合法空 doc（`{"version":1,"intents":[],…}`）
 * 不在此列，清空全部意图后仍可继续编辑（不能把操作者锁死）。
 */
function graphRawValid(raw: string): boolean {
  const text = String(raw ?? "").trim();
  if (!text) return true;
  try {
    const data = JSON.parse(text) as Partial<GraphDoc>;
    return Boolean(data) && data.version === 1 && Array.isArray(data.intents) && Array.isArray(data.bindings);
  } catch {
    return false;
  }
}

/** 新增 id：CP `_ID_RE` 只收 `int_`/`bnd_` + 8 位小写 hex（crypto 随机，非 Math.random）。 */
function genGraphId(prefix: "int_" | "bnd_"): string {
  const bytes = crypto.getRandomValues(new Uint8Array(4));
  return prefix + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** 从图移除意图 + 其全部绑定（悬空绑定在 CP 侧必被拒：binding.intent 找不到意图）。 */
function dropIntentFromDoc(doc: GraphDoc, intentId: string): GraphDoc {
  return {
    ...doc,
    intents: doc.intents.filter((i) => i.id !== intentId),
    bindings: doc.bindings.filter((b) => b.intent !== intentId),
  };
}

/** 节点 id `intent:<intentId>` → 意图 id（Task 6 的 id 形态；非前缀形原样返回）。 */
function intentIdOfNodeId(nodeId: string): string {
  return nodeId.startsWith("intent:") ? nodeId.slice("intent:".length) : nodeId;
}

/** 绑定边 id `bind:<bindingId>` → 绑定 id（非该形态返回 ""，调用方按 no-op 处理）。 */
function bindingIdOfEdge(edgeId: string): string {
  return edgeId.startsWith("bind:") ? edgeId.slice("bind:".length) : "";
}

/** 关键词输入（逗号/顿号/分号/换行分隔）→ 数组：去空、去重、保序。 */
function splitKeywords(text: string): string[] {
  const out: string[] = [];
  for (const part of String(text ?? "").split(/[,，、;；\n\r]+/)) {
    const kw = part.trim();
    if (kw && !out.includes(kw)) out.push(kw);
  }
  return out;
}

/** [min,max] 整数钳位（priority/步号落库前统一收口）。 */
function clampInt(value: number, min: number, max: number): number {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return min;
  return Math.min(Math.max(n, min), max);
}

/** 绑定 → 编辑器行（review M28：jump 步号按当前话术真实步数钳位，越界落点会在画布上
 *  被 deriveGraph 钳到末步、显示成一条误导性的边，故进编辑器先归一到合法区间）。 */
type BindingDraft = {
  id: string;
  action: "play_qa" | "jump_step";
  qa_id: string;
  step: number;
  priority: number;
  once: boolean;
  enabled: boolean;
};

function bindingToDraft(b: GraphBinding, stepCount: number): BindingDraft {
  const action = b.action === "jump_step" ? "jump_step" : "play_qa";
  return {
    id: b.id,
    action,
    qa_id: String(b.qa_id ?? ""),
    step: clampInt(Number(b.step ?? 1), 1, Math.max(stepCount, 1)),
    priority: clampInt(Number(b.priority ?? 10), 0, 1000),
    once: b.once === true,
    enabled: b.enabled !== false,
  };
}

export default function QaPage() {
  const { accountId } = useAccount();
  const session = useSession();
  const [rows, setRows] = useState<QaRow[] | null>(null);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [tab, setTab] = useState<"mine" | "shared">("mine");
  const [form, setForm] = useState<QaForm>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [formErr, setFormErr] = useState("");
  const [ok, setOk] = useState(false);
  // 画布视图(qa-canvas Phase1 Task7):「列表|画布」切换 + 画布派生输入。
  const [view, setView] = useState<"list" | "canvas">("list");
  const [templates, setTemplates] = useState<QaTemplateRow[]>([]);
  // 模板下拉只作画布步骤脊柱的派生输入(spec §4.2);「画布选的哪个模板」同时决定意图图
  // (graph_json)读写目标与权限门(Task 8)。
  const [templateId, setTemplateId] = useState("");
  // 意图图(话术图 Phase 2 Task 8):选中模板的 graph_json 解析结果 + 编辑器开合。
  const [graphDoc, setGraphDoc] = useState<GraphDoc>(EMPTY_GRAPH);
  // 在手的 graphDoc **属于哪个模板**(review R1 Fault 2):""=未加载/加载失败/坏数据。
  // 只有 graphLoadedFor === templateId 才允许读写——切模板的当帧(旧图还在 state 里、新
  // GET 未回来)与加载失败档都因此结构性不可写,不会把 A 模板的图移植到 B、也不会把
  // 空图写回库覆盖已存配置。
  const [graphLoadedFor, setGraphLoadedFor] = useState("");
  // 图相关的独立错误面(与 QA 条目的 err 分开:err 会被 refresh/pregen 清掉,图警告必须留)。
  const [graphErr, setGraphErr] = useState("");
  // 在所有 graphDoc 数据路径上镜像同一份 doc(review R1 Fault 1):handler 不再读 render 期
  // 闭包里的 graphDoc,而是读写这个 ref——同一 tick 内的多次变更(多选 Delete 一批边/节点+边)
  // 逐个叠加,不会各自从同一份旧 doc 算,互相覆盖。
  const graphDocRef = useRef<GraphDoc>(EMPTY_GRAPH);
  const graphLoadedForRef = useRef("");
  // 写队列:同一 tick 的多笔变更按提交顺序**串行** PUT(浏览器多连接下并发 PUT 的到达顺序
  // 不受控,乱序会让服务端停在中间态=客户端显示已删、库里还在)。
  const writeChainRef = useRef<Promise<void>>(Promise.resolve());
  // null=关；非空=正在编辑的意图 id。
  const [editorIntentId, setEditorIntentId] = useState<string | null>(null);
  // 新建草稿意图:{id,pos}。草稿**不落库**(空 label/空 keywords 必被 CP 400 拒),只在
  // 本地图上存在;取消/放弃按 id 剔除。pos=调色盘落点(当前仅作「哪来的草稿」记录,
  // 布局仍由画布 deriveGraph 锚定首个生效步——画布 positions 状态归画布组件所有)。
  const [draftIntent, setDraftIntent] = useState<{ id: string; pos: { x: number; y: number } } | null>(
    null,
  );
  // 录音状态面(spec §5):entry_id → ok|missing;拉取失败=空表(徽标降级,§8)。
  const [canned, setCanned] = useState<Record<string, { state: "ok" | "missing" }>>({});
  const cannedPollRef = useRef<ReturnType<typeof setTimeout>[]>([]);
  // 右键菜单(节点/边):{x,y}=视口坐标,fixed 定位直用。
  const [menu, setMenu] = useState<{ row: QaRow; x: number; y: number } | null>(null);
  const [edgeMenu, setEdgeMenu] = useState<{ edge: CanvasEdgeHit; x: number; y: number } | null>(null);

  /** 主管模式：匿名本地会话（auth-off 单机形态）与 admin/root 一律全量管理。 */
  const isManager = Boolean(
    session && (session.anonymous || session.role === "admin" || session.role === "root"),
  );

  // 行编辑权(与列表逐字同规;useCallback 稳定引用——画布组件 nodes useMemo 依赖它,
  // 每 render 新建闭包会击穿 memo 全节点重建)。
  const canEditRow = useCallback(
    (row: QaRow) => {
      if (isManager) return true;
      const uid = session?.user_id ?? "";
      return uid !== "" && String(row.owner_user_id ?? "") === uid;
    },
    [isManager, session],
  );

  // 意图图编辑权(B3 口径，与 canEditRow 同规则、与 CP deny_foreign_owner(edit=True) 对齐)：
  // 主管(admin/root/匿名单机)=可改；user 只改自己的模板，共享模板(owner="")只读——CP 对共享
  // 模板的 PUT 直接 403，前端放开只会让操作者白画一张图。uid 必须非空(匿名 user_id 恒 ""，
  // 不加这条会让 "共享模板 owner=''" 与匿名 uid 相等而误判为可编辑)。
  const activeTemplate = templates.find((t) => String(t.id) === templateId);
  const canEditTemplateGraph = Boolean(activeTemplate) &&
    (isManager ||
      ((session?.user_id ?? "") !== "" &&
        String(activeTemplate?.owner_user_id ?? "") === session?.user_id));

  // 在手的图属于当前选中的模板(详情已取回且形状合法;review R1 Fault 2)。
  const graphReady = graphLoadedFor !== "" && graphLoadedFor === templateId;
  // **图可编辑** = 权限 × 在手图。缺任一即不可写:加载中/加载失败/坏数据档画布隐藏调色盘、
  // 意图节点不可删、编辑器只读,写路径(orchestrator)另有 ref 级同款门(双保险)。
  const canEditGraph = canEditTemplateGraph && graphReady;

  // 选中模板的步骤表：意图的生效步骤 chips 与 jump_step 目标下拉都按它的**真实步数**生成
  // (review M28:越界步号在画布上会被钳到末步、渲染成误导性落点)。
  const templateSteps: FlowStep[] = useMemo(
    () => parseTemplateSteps(String(activeTemplate?.steps_json ?? "")),
    [activeTemplate],
  );

  const refresh = useCallback(async () => {
    try {
      const data = await api.listQaAll(accountId);
      setRows(Array.isArray(data) ? (data as QaRow[]) : []);
      setErr("");
    } catch (e) {
      setErr(String(e));
      setRows((prev) => prev ?? []);
    }
  }, [accountId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 录音状态面:CP 侧 60s TTL(spec §5),补录音触发后经 scheduleCannedRefresh 轮询刷新。
  const refreshCanned = useCallback(async () => {
    try {
      const res = await api.qaCannedStatus(accountId);
      setCanned(res?.statuses ?? {});
    } catch {
      setCanned({}); // 拉取失败=无徽标降级(spec §8),不阻塞画布渲染
    }
  }, [accountId]);

  useEffect(() => {
    void refreshCanned();
  }, [refreshCanned]);

  // 补录音后轮询:生成是后台子进程(条目多时数十秒),且 CP 状态缓存 60s TTL——
  // 单次 setTimeout 3s 结构性看不见新料;改 3s/15s/40s/65s 四次间隔轮询,
  // 末次跨过 TTL 窗口;重复触发先清旧定时器。卸载清理防 setState-after-unmount。
  const scheduleCannedRefresh = useCallback(() => {
    for (const t of cannedPollRef.current) clearTimeout(t);
    cannedPollRef.current = [3000, 15000, 40000, 65000].map((ms) =>
      setTimeout(() => void refreshCanned(), ms),
    );
  }, [refreshCanned]);

  useEffect(
    () => () => {
      for (const t of cannedPollRef.current) clearTimeout(t);
    },
    [],
  );

  // 话术表(画布步骤脊柱输入):话务员 403/拉取失败=静默空表(脊柱消失仍可用);账号变化重拉。
  useEffect(() => {
    let alive = true;
    setTemplates([]);
    setTemplateId(""); // 旧账号选中不再有效
    void (async () => {
      try {
        const raw = (await api.listTemplates(accountId)) as Record<string, unknown>[];
        const list = (Array.isArray(raw) ? raw : [])
          .map((t) => ({
            id: String(t.id ?? ""),
            name: typeof t.name === "string" ? t.name : undefined,
            steps_json: typeof t.steps_json === "string" ? t.steps_json : undefined,
            language: typeof t.language === "string" ? t.language : undefined,
            // 意图图编辑权(B3):user 只改自己的,共享模板(owner="")只读。
            owner_user_id: typeof t.owner_user_id === "string" ? t.owner_user_id : "",
          }))
          .filter((t) => t.id);
        if (!alive) return;
        setTemplates(list);
        setTemplateId((prev) => prev || (list[0]?.id ?? "")); // spec §4.2 默认第一个模板
      } catch {
        if (alive) setTemplates([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, [accountId]);

  // 意图图加载(话术图 Phase 2 Task 8):选中模板变化 → 拉详情取 graph_json → parseGraphDoc。
  // 列表行虽带 graph_json,详情才是权威且最新(与写入同一端点)。**三步防误写**(review R1 Fault 2):
  // ①切模板的**当帧**就把 graphDoc 清空 + graphLoadedFor 置 ""——旧模板的图绝不留在手上被写进新模板;
  // ②只有详情取回且原文形状合法,才把 graphLoadedFor 置为**该模板 id**(=允许读写);
  // ③GET 失败 / 坏数据 → 保持 graphLoadedFor=""(写入被闸死)+ 独立 graphErr 警告,绝不静默放行
  //   (否则第一次编辑就会把库里那份读不懂的配置覆盖成空 doc)。
  useEffect(() => {
    let alive = true;
    setEditorIntentId(null);
    setDraftIntent(null);
    graphLoadedForRef.current = "";
    setGraphLoadedFor("");
    graphDocRef.current = EMPTY_GRAPH;
    setGraphDoc(EMPTY_GRAPH);
    setGraphErr("");
    if (!templateId) {
      return () => {
        alive = false;
      };
    }
    void (async () => {
      try {
        const tpl = (await api.getTemplate(templateId)) as Record<string, unknown>;
        if (!alive) return;
        const raw = typeof tpl.graph_json === "string" ? tpl.graph_json : "";
        if (!graphRawValid(raw)) {
          setGraphErr(
            "该话术的话术图数据无法解析（已存配置未被覆盖）。为避免误写，意图图编辑已暂停；请在话术里清空或重建后重试。",
          );
          return; // graphLoadedFor 保持 "":画布只读,写路径全闸死
        }
        const doc = parseGraphDoc(raw);
        graphDocRef.current = doc;
        setGraphDoc(doc);
        graphLoadedForRef.current = templateId;
        setGraphLoadedFor(templateId);
      } catch (e) {
        if (!alive) return;
        setGraphErr(`读取话术图失败：${String(e)}；为避免覆盖已存配置，该话术的意图图编辑已暂停（可刷新重试）。`);
      }
    })();
    return () => {
      alive = false;
    };
  }, [templateId]);

  // 成员表只服务主管面（归属列 + 转移下拉）；话务员无权访问 /api/users，别请求。
  useEffect(() => {
    if (!isManager) return;
    let alive = true;
    void (async () => {
      try {
        const raw = (await api.listUsers()) as unknown;
        // 兼容裸数组与 {users:[...]} 包裹两种响应形态。
        const list = Array.isArray(raw)
          ? (raw as UserRow[])
          : Array.isArray((raw as { users?: UserRow[] })?.users)
            ? (raw as { users: UserRow[] }).users
            : [];
        if (alive) setUsers(list);
      } catch {
        if (alive) setUsers([]); // 拉不到成员名时退化为显示 id，不阻断页面
      }
    })();
    return () => {
      alive = false;
    };
  }, [isManager]);

  const visibleRows = useMemo(() => {
    const all = rows ?? [];
    if (isManager || !session) return all;
    if (tab === "mine") {
      return all.filter((r) => String(r.owner_user_id ?? "") === session.user_id);
    }
    return all.filter((r) => String(r.owner_user_id ?? "") !== session.user_id);
  }, [rows, isManager, tab, session]);

  const mineCount = (rows ?? []).filter(
    (r) => String(r.owner_user_id ?? "") === (session?.user_id ?? ""),
  ).length;
  const sharedCount = (rows ?? []).length - mineCount;

  const userName = (id: string) => {
    const u = users.find((x) => x.id === id);
    return u?.display_name || u?.username || id;
  };

  /** 归属下拉选项：只列话务员（admin/root 不参与归属）；当前归属缺失时补一个占位项。 */
  const ownerChoices = (ownerId: string) => {
    const list = users
      .filter((u) => u.role === "user")
      .map((u) => ({ id: u.id, label: u.display_name || u.username || u.id }));
    if (ownerId && !list.some((o) => o.id === ownerId)) {
      list.push({ id: ownerId, label: `${ownerId}（未知成员）` });
    }
    return list;
  };

  function resetForm() {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setFormErr("");
    setOk(false);
  }

  function edit(row: QaRow) {
    setEditingId(String(row.id ?? ""));
    setForm({
      question_text: String(row.question_text ?? ""),
      answer_text: String(row.answer_text ?? ""),
      lang: String(row.lang ?? "zh"),
      scope: String(row.scope ?? "global"),
      step: displayStep(row),
      voice_id: String(row.voice_id ?? ""),
      enabled: row.enabled !== false,
      priority: String(row.priority ?? 10),
    });
    setFormErr("");
    setOk(false);
  }

  async function save() {
    const question = form.question_text.trim();
    const answer = form.answer_text.trim();
    if (!question || !answer) {
      setFormErr("客户问法和标准回答都不能为空。");
      return;
    }
    const step = Math.max(1, Math.round(Number(form.step) || 1));
    // 优先级钳 [0,1000]（CP 同款）；空串/NaN 回默认 10。0 是合法值。
    const prioRaw = form.priority.trim() === "" ? 10 : Math.round(Number(form.priority));
    const priority = Number.isFinite(prioRaw) ? Math.max(0, Math.min(prioRaw, 1000)) : 10;
    const payload: Record<string, unknown> = {
      question_text: question,
      answer_text: answer,
      lang: form.lang,
      scope: form.scope,
      step_index: form.scope === "step" ? step - 1 : -1,
      voice_id: form.voice_id.trim(),
      enabled: form.enabled,
      priority,
    };
    setBusy("save");
    setErr("");
    setFormErr("");
    try {
      if (editingId) {
        await api.patchQa(editingId, payload);
      } else {
        // 归属由服务端盖章（话务员建=本人、主管建=共享），前端不传 owner。
        await api.createQa({ ...payload, account_id: accountId });
      }
      setForm(EMPTY_FORM);
      setEditingId(null);
      setOk(true);
      // 新建/改完的条目归属本人，切回「我的」让用户立刻看到结果。
      if (!isManager) setTab("mine");
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  async function toggleEnabled(row: QaRow) {
    const id = String(row.id ?? "");
    setBusy(`${id}:toggle`);
    setErr("");
    try {
      await api.patchQa(id, { enabled: row.enabled === false });
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  async function remove(row: QaRow) {
    const id = String(row.id ?? "");
    const q = String(row.question_text ?? "").slice(0, 40);
    if (!window.confirm(`确认删除该快答条目？\n${q}\n删除后通话不再命中，此操作不可恢复。`)) return;
    setBusy(`${id}:delete`);
    setErr("");
    try {
      await api.deleteQa(id);
      if (editingId === id) resetForm();
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  async function transferOwner(row: QaRow, owner: string) {
    const id = String(row.id ?? "");
    setBusy(`${id}:owner`);
    setErr("");
    try {
      await api.patchQa(id, { owner_user_id: owner });
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  // ---- 画布录音面(qa-canvas Phase1 Task7):补录音/试听,随画布挂载接线 ----

  /** 补录音(一键=全量幂等,单条=右键「重新录音」):admin/root 闸,按钮仅主管渲染。
   *  already_running 静默(单飞进行中,轮询会自然取到结果);script_missing 才报错。 */
  async function pregenQa(ids: string[]) {
    setBusy("pregen");
    setErr("");
    try {
      const res = await api.pregenQa(ids);
      if (res?.status === "script_missing") setErr("重新录音失败：录音生成脚本缺失。");
      scheduleCannedRefresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  /** 试听(spec §5):先播录音缓存(零云费,auth-on 经 fetch-blob+Bearer,<audio> 带不了鉴权头);
   *  缺录音(404)/播放失败回退现场合成——烧云配额仅主管可用,user 见提示待生成。 */
  async function audition(row: QaRow) {
    const id = String(row.id ?? "");
    setBusy(`${id}:audition`);
    try {
      const res = await fetch(api.cannedAudioUrl(id), { headers: authHeaders() });
      if (!res.ok) throw new Error(String(res.status));
      await playBlob(await res.blob());
    } catch {
      if (isManager) {
        try {
          await playBlob(
            await api.previewTts({
              provider: "minimax",
              text: String(row.answer_text ?? ""),
              voice: String(row.voice_id ?? ""),
              language: String(row.lang ?? "zh"),
              sample_rate: 24000,
            }),
          );
        } catch (e) {
          setErr(`试听失败：${String(e)}`);
        }
      } else {
        window.alert("该条目还没有录音，请联系主管在画布上「重新录音」。");
      }
    } finally {
      setBusy("");
    }
  }

  // ---- 意图图编辑(话术图 Phase 2 Task 8):保存=PUT graph_json 单字段,乐观写+失败回滚 ----
  // 全模块只有两个图写入口:**本地提交** `setGraphLocal`(state+ref 同步)与**落库** `mutateGraph`
  // (读 ref 里的最新 doc → 算 next → 提交 → PUT)。handler 一律不许再直接读 render 期闭包里的
  // graphDoc——那是「同一 tick 多笔变更互相覆盖」的根因(review R1 Fault 1)。

  /** 本地提交图(state + ref 同步;不联网)。 */
  const setGraphLocal = useCallback((doc: GraphDoc) => {
    graphDocRef.current = doc;
    setGraphDoc(doc);
  }, []);

  /** 图变更的唯一落库入口:读**最新在手 doc**(ref)→ `fn` 算 next → 本地提交 → PUT。
   *
   *  - **同 tick 多笔**:画布多选 Delete 一批边会同步连调 N 次,每次都在**上一次的结果**上叠加
   *    (读 ref 而非旧闭包),N 笔一条不丢;
   *  - **写队列**:PUT 按提交顺序串行(浏览器多连接下并发 PUT 到达顺序不受控,乱序会让服务端
   *    停在中间态——客户端显示已删、库里还在);
   *  - **门**:只写**在手图属于当前模板**的那一份(graphLoadedForRef === templateId),加载中/
   *    失败/坏数据档与切模板的当帧结构性写不进去(review R1 Fault 2);
   *  - **回滚**:失败只回滚自己那次乐观写(期间若有更新的变更落地则 ref 已不是 next,不拿旧快照
   *    盖回去;后一笔的 next 是从这一版算出来的,服务端最终仍收敛到最新版)。 */
  const mutateGraph = useCallback(
    (fn: (doc: GraphDoc) => GraphDoc): Promise<boolean> => {
      const tid = templateId;
      if (!tid || graphLoadedForRef.current !== tid) return Promise.resolve(false);
      const prev = graphDocRef.current;
      const next = fn(prev);
      setGraphLocal(next); // 乐观
      const run = writeChainRef.current.then(async () => {
        try {
          await api.updateTemplate(tid, { graph_json: JSON.stringify(next) });
          setErr("");
          return true;
        } catch (e) {
          if (graphDocRef.current === next) setGraphLocal(prev); // 回滚
          setErr(`保存话术图失败：${String(e)}`);
          return false;
        }
      });
      // 链上吞错:某笔失败不能让后续写永久吊死(失败已单独上报/回滚)。
      writeChainRef.current = run.then(
        () => undefined,
        () => undefined,
      );
      return run;
    },
    [templateId, setGraphLocal],
  );

  /** 新建意图:先落**本地草稿**(不进库)+开编辑器。
   *  不落库的原因=CP validate_flow_graph 要求 label 1-64 字、keywords 非空——空草稿的 PUT
   *  必然 400(白跑一次请求+必触发回滚,还会让编辑器指向一条已被回滚掉的意图)。操作者点
   *  确认时整图一次 PUT 落地;取消/空草稿放弃=按 id 从本地图剔除,零请求。
   *  同时剔除上一条草稿:本地图里**只准存在一条**未落库空意图——孤儿空意图(无编辑器可改)
   *  会跟着下一次整图 PUT 一起上送 → 必被拒(模态覆盖层下其实点不出第二条,纯防御)。 */
  const addIntent = useCallback(
    (pos: { x: number; y: number }) => {
      if (!canEditGraph) return;
      const intent: GraphIntent = {
        id: genGraphId("int_"), label: "", keywords: [], steps: [], enabled: true,
      };
      const staleDraftId = draftIntent?.id ?? "";
      const doc = graphDocRef.current;
      setGraphLocal({
        ...doc,
        intents: [...doc.intents.filter((i) => i.id !== staleDraftId), intent],
      });
      setDraftIntent({ id: intent.id, pos });
      setEditorIntentId(intent.id);
    },
    [canEditGraph, draftIntent, setGraphLocal],
  );

  /** 删意图=连同它的绑定一起从图移除(悬空绑定在 CP 侧必被拒:binding.intent 找不到意图)。 */
  const deleteIntent = useCallback(
    (intentId: string) => {
      if (!canEditGraph) return;
      if (draftIntent?.id === intentId) {
        setGraphLocal(dropIntentFromDoc(graphDocRef.current, intentId)); // 草稿从未落库:纯本地剔除,零请求
        return;
      }
      void mutateGraph((doc) => dropIntentFromDoc(doc, intentId));
    },
    [canEditGraph, draftIntent, mutateGraph, setGraphLocal],
  );

  /** 删绑定(画布上 Delete 掉绑定边/或编辑器里删行后由 confirm 走整图 PUT)。 */
  const removeBinding = useCallback(
    (bindingId: string) => {
      if (!canEditGraph) return;
      void mutateGraph((doc) => ({
        ...doc,
        bindings: doc.bindings.filter((b) => b.id !== bindingId),
      }));
    },
    [canEditGraph, mutateGraph],
  );

  /** 编辑器确认:意图新值 + 该意图的绑定全集 → 整图 PUT。成功即关窗(失败留在窗内让操作者改)。 */
  const confirmIntentEditing = useCallback(
    async (nextIntent: GraphIntent, nextBindings: GraphBinding[]): Promise<boolean> => {
      const ok = await mutateGraph((doc) => ({
        version: doc.version,
        intents: doc.intents.some((i) => i.id === nextIntent.id)
          ? doc.intents.map((i) => (i.id === nextIntent.id ? nextIntent : i))
          : [...doc.intents, nextIntent],
        bindings: [
          ...doc.bindings.filter((b) => b.intent !== nextIntent.id),
          ...nextBindings,
        ],
      }));
      if (ok) {
        setEditorIntentId(null);
        setDraftIntent(null);
      }
      return ok;
    },
    [mutateGraph],
  );

  /** 关编辑器:草稿(未落库)连带从本地图剔除;存量意图仅关窗,零请求。 */
  const closeIntentEditor = useCallback(() => {
    const draftId = draftIntent?.id ?? "";
    if (draftId) {
      const doc = graphDocRef.current;
      setGraphLocal({ ...doc, intents: doc.intents.filter((i) => i.id !== draftId) });
    }
    setEditorIntentId(null);
    setDraftIntent(null);
  }, [draftIntent, setGraphLocal]);

  /** 绑定边右键 → 开其源意图的编辑器(绑定边 source 恒为 "intent:<id>")。 */
  const setEditingIntentForEdge = useCallback((edge: CanvasEdgeHit) => {
    const id = intentIdOfNodeId(String(edge.source ?? ""));
    if (id) setEditorIntentId(id);
  }, []);

  /** 正在编辑的意图(意图不在了→undefined,模态不渲染空窗)。 */
  const editingIntent = editorIntentId
    ? graphDoc.intents.find((i) => i.id === editorIntentId)
    : undefined;

  // ---- 连簇/挂步骤/断线处理器(Task6 落逻辑,Task7 随画布挂载接线);校验/回滚纯函数在 lib/qa-canvas.ts ----

  /** 连簇(spec §4.4):校验→乐观写 cluster_head_id→PATCH,失败 revertCluster 回滚后 refresh。 */
  async function connectCluster(fromId: string, toId: string) {
    const verdict = resolveClusterTarget(rows ?? [], fromId, toId);
    if (!verdict.ok) {
      setErr(verdict.reason);
      return;
    }
    const prev = String((rows ?? []).find((r) => String(r.id) === fromId)?.cluster_head_id ?? "");
    if (verdict.headId === prev) return; // 同簇兄弟变体/幂等重挂:no-op 静默,避免无意义 PATCH
    setRows((rs) =>
      (rs ?? []).map((r) => (String(r.id) === fromId ? { ...r, cluster_head_id: verdict.headId } : r)),
    ); // 乐观
    try {
      await api.patchQa(fromId, { cluster_head_id: verdict.headId });
      setErr("");
    } catch (e) {
      setErr(String(e));
      setRows((rs) => revertCluster(rs ?? [], fromId, prev)); // 回滚,spec §8
      await refresh();
    }
  }

  /** 挂步骤:stepIndex=-1=解挂回全程通用;失败 refresh 回真值(无乐观写)。 */
  async function stepConnect(entryId: string, stepIndex: number) {
    const patch =
      stepIndex < 0
        ? { scope: "global", step_index: -1, template_id: "" }
        : { scope: "step", step_index: stepIndex, template_id: templateId };
    try {
      await api.patchQa(entryId, patch);
      setErr("");
      await refresh();
    } catch (e) {
      setErr(String(e));
      await refresh();
    }
  }

  /** 断线:簇边=变体回归独立(乐观写+回滚,与连簇对称);步骤边=解挂回全程通用;
   *  绑定边=从图移除该绑定(Task 8);脊柱顺序线(spine)是展示性元素,不可解除。 */
  async function disconnect(edge: { id?: string; source: string; target: string; data?: { kind?: string } }) {
    const fromId = String(edge.source ?? "");
    if (!fromId) return;
    if (edge.data?.kind === "spine") return;
    // 绑定边(意图→目标,Task 8 必须闸):分支**必须先于**下面的 stepConnect 兜底——兜底会把
    // "intent:xxx" 当快答条目 id 去 PATCH,一个不存在的条目=404 假故障;binding 的删除语义是
    // 「从图移除该条绑定」(与簇边删除对称),Delete 键与右键两条路都收在这里。
    if (edge.data?.kind === "binding") {
      const bindingId = bindingIdOfEdge(String(edge.id ?? ""));
      if (bindingId) removeBinding(bindingId);
      return;
    }
    if (edge.data?.kind === "cluster") {
      const prev = String((rows ?? []).find((r) => String(r.id) === fromId)?.cluster_head_id ?? "");
      setRows((rs) =>
        (rs ?? []).map((r) => (String(r.id) === fromId ? { ...r, cluster_head_id: "" } : r)),
      ); // 乐观
      try {
        await api.patchQa(fromId, { cluster_head_id: "" });
        setErr("");
      } catch (e) {
        setErr(String(e));
        setRows((rs) => revertCluster(rs ?? [], fromId, prev));
        await refresh();
      }
      return;
    }
    await stepConnect(fromId, -1);
  }

  if (!session) return <LoadingState label="正在读取会话…" />;

  const emptyLabel = isManager
    ? "暂无快答条目，请在右侧新建。"
    : tab === "mine"
      ? "你还没有个人条目，可在右侧新建。"
      : "暂无共享条目。";
  const textarea = "w-full resize-none rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--live)";
  const selectCls = "w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--live)";
  const menuItemCls = "block w-full px-3 py-1.5 text-left text-xs hover:bg-accent";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="page-title">快答库</h1>
          <p className="page-sub">常见问法的即答条目 · 命中即播标准回答，跳过模型生成</p>
        </div>
        <div className="flex items-center gap-3">
          {/* 画布不套用 mine/shared 过滤(spec §4.3:画布=全部可见条目),画布视图藏归属 tab。 */}
          {!isManager && view === "list" && (
            <div className="flex items-center gap-1">
              {([
                ["mine", `我的（${mineCount}）`],
                ["shared", `共享（${sharedCount}）`],
              ] as const).map(([key, label]) => (
                <button
                  key={key}
                  className={`btn-ghost text-xs ${tab === key ? "border-(--live) text-(--live-ink)" : "muted"}`}
                  onClick={() => setTab(key)}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
          <div className="flex items-center gap-1">
            {([["list", "列表"], ["canvas", "画布"]] as const).map(([k, label]) => (
              <button
                key={k}
                className={`btn-ghost text-xs ${view === k ? "border-(--live) text-(--live-ink)" : "muted"}`}
                onClick={() => setView(k)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {err && <ErrorState message={err} />}
      {/* 图专用警告(review R1 Fault 2):加载失败/坏数据时写入被闸死,必须让操作者看见原因,
          不能静默退化成空图(否则下一笔编辑会把库里那份读不懂的配置覆盖掉)。 */}
      {graphErr && <ErrorState message={graphErr} />}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_420px]">
        {view === "canvas" ? (
          <section className="card">
            {rows === null ? (
              <LoadingState />
            ) : rows.length === 0 ? (
              <EmptyState label="暂无快答条目，可在右侧新建后回到画布。" />
            ) : (
              <QaCanvasView
                accountId={accountId}
                rows={rows}
                templates={templates}
                templateId={templateId}
                onTemplateChange={setTemplateId}
                canned={canned}
                canEditRow={canEditRow}
                onNodeClick={edit}
                onPaneDoubleClick={() => resetForm()}
                onNodeContextMenu={(row, e) => setMenu({ row, x: e.clientX, y: e.clientY })}
                onEdgeContextMenu={(edge, e) => setEdgeMenu({ edge, x: e.clientX, y: e.clientY })}
                onConnectCluster={connectCluster}
                onStepConnect={stepConnect}
                onDisconnect={disconnect}
                onPregenAll={isManager ? () => void pregenQa([]) : undefined}
                pregenBusy={busy === "pregen"}
                /* 话术图(Phase 2 Task 8):图数据 + 编辑权 + 意图增删/开编辑器。
                   无编辑权时画布自己隐藏调色盘、意图节点不可删。 */
                graphDoc={graphDoc}
                canEditGraph={canEditGraph}
                onAddIntent={addIntent}
                onOpenIntentEditor={(id) => setEditorIntentId(id)}
                onDeleteIntent={deleteIntent}
              />
            )}
          </section>
        ) : (
        <section className="card">
          {rows === null ? (
            <LoadingState />
          ) : visibleRows.length === 0 ? (
            <EmptyState label={emptyLabel} />
          ) : (
            <div className="space-y-3">
              {visibleRows.map((row) => {
                const id = String(row.id ?? "");
                const ownerId = String(row.owner_user_id ?? "");
                const mine = !isManager && session.user_id !== "" && ownerId === session.user_id;
                const canEdit = isManager || mine;
                const rowBusy = busy.startsWith(`${id}:`);
                return (
                  <div key={id} className="rounded-lg bg-muted/60 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="font-medium">{String(row.question_text ?? "(无问法)")}</p>
                        <p className="mt-1 whitespace-pre-line text-sm muted">
                          {String(row.answer_text ?? "")}
                        </p>
                      </div>
                      <div className="flex shrink-0 items-center gap-2" title={canEdit ? undefined : "共享条目由主管维护"}>
                        {canEdit ? (
                          <>
                            <button className="btn-ghost text-xs" onClick={() => edit(row)}>编辑</button>
                            <button
                              className="btn-ghost text-xs"
                              disabled={rowBusy}
                              onClick={() => void toggleEnabled(row)}
                            >
                              {row.enabled === false ? "启用" : "停用"}
                            </button>
                            <button
                              className="btn-ghost text-xs text-red-600"
                              disabled={rowBusy}
                              onClick={() => void remove(row)}
                            >
                              删除
                            </button>
                          </>
                        ) : (
                          <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px] muted">共享</span>
                        )}
                      </div>
                    </div>
                    <p className="mt-2 text-xs muted">
                      {LANGS.find((l) => l[0] === String(row.lang ?? "zh"))?.[1] ?? String(row.lang ?? "zh")}
                      {" · "}
                      {SCOPE_LABEL[String(row.scope ?? "global")] ?? String(row.scope ?? "global")}
                      {String(row.scope ?? "") === "step" && ` 第 ${displayStep(row)} 步`}
                      {Number(row.priority ?? 10) !== 10 && ` · P${Number(row.priority ?? 10)}`}
                      {String(row.voice_id ?? "") && ` · 音色 ${String(row.voice_id)}`}
                      {" · "}命中 {Number(row.hit_count ?? 0)} 次
                      {String(row.created_at ?? "") && ` · 创建 ${String(row.created_at).slice(0, 10)}`}
                      {row.enabled === false && <span className="ml-2 text-amber-600">已停用</span>}
                    </p>
                    {isManager && (
                      <div className="mt-2 flex items-center gap-2 text-xs">
                        <span className="label">归属</span>
                        <select
                          className="rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)"
                          value={ownerId}
                          disabled={rowBusy}
                          title="归属：共享=全账号可用；选成员=仅该话务员可见"
                          onChange={(e) => void transferOwner(row, e.target.value)}
                        >
                          <option value="">共享（全账号）</option>
                          {ownerChoices(ownerId).map((o) => (
                            <option key={o.id} value={o.id}>{o.label}</option>
                          ))}
                        </select>
                        <span className="muted">当前：{ownerId === "" ? "共享" : userName(ownerId)}</span>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </section>
        )}

        <section className="card space-y-3">
          <div className="flex items-center justify-between">
            <span className="label">{editingId ? "编辑快答条目" : "新建快答条目"}</span>
            {editingId && (
              <button className="btn-ghost px-2 py-0.5 text-xs" onClick={resetForm}>取消编辑</button>
            )}
          </div>

          <label className="block">
            <span className="text-xs text-muted-foreground">客户问法</span>
            <textarea
              className={`mt-1 h-20 ${textarea}`}
              value={form.question_text}
              onChange={(e) => setForm({ ...form, question_text: e.target.value })}
              placeholder="如：点样查物流？"
            />
            <span className="mt-1 block text-[11px] leading-relaxed muted">
              命中判定按字面措辞，客户说法不同就多建几条。
            </span>
          </label>

          <label className="block">
            <span className="text-xs text-muted-foreground">标准回答</span>
            <textarea
              className={`mt-1 h-24 ${textarea}`}
              value={form.answer_text}
              onChange={(e) => setForm({ ...form, answer_text: e.target.value })}
              placeholder="命中后直接播放的整句回答（按通话语言写）"
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-xs text-muted-foreground">语言</span>
              <select
                className={`mt-1 ${selectCls}`}
                value={form.lang}
                onChange={(e) => setForm({ ...form, lang: e.target.value })}
              >
                {LANGS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-muted-foreground">生效范围</span>
              <select
                className={`mt-1 ${selectCls}`}
                value={form.scope}
                onChange={(e) => setForm({ ...form, scope: e.target.value })}
              >
                <option value="global">全程通用</option>
                <option value="step">指定步骤</option>
              </select>
            </label>
          </div>

          {form.scope === "step" && (
            <label className="block">
              <span className="text-xs text-muted-foreground">第几步</span>
              <input
                type="number"
                min={1}
                className={`mt-1 ${selectCls}`}
                value={form.step}
                onChange={(e) => setForm({ ...form, step: Number(e.target.value) || 1 })}
              />
              <span className="mt-1 block text-[11px] leading-relaxed muted">
                从 1 开始数，对应话术的步骤顺序（第 1 步 = 开场确认）。
              </span>
            </label>
          )}

          <label className="block">
            <span className="text-xs text-muted-foreground">音色覆盖（可选）</span>
            <input
              className={`mt-1 ${selectCls}`}
              value={form.voice_id}
              onChange={(e) => setForm({ ...form, voice_id: e.target.value })}
              placeholder="留空 = 当前人设音色"
            />
          </label>

          <label className="block">
            <span className="text-xs text-muted-foreground">匹配优先级</span>
            <input
              type="number"
              min={0}
              max={1000}
              className={`mt-1 ${selectCls}`}
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: e.target.value })}
            />
            <span className="mt-1 block text-[11px] leading-relaxed muted">
              数字越小越优先（0-1000，默认 10）。只在同轮多条命中时决定谁答；不影响匹配阈值。
            </span>
          </label>

          <label className="flex items-center gap-1.5 text-[11px] muted">
            <input
              type="checkbox"
              className="size-3 accent-(--live)"
              checked={form.enabled}
              onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
            />
            启用（停用后通话不再命中该条目）
          </label>

          {formErr && <p className="text-xs text-red-600">{formErr}</p>}
          <div className="flex items-center gap-3">
            <button className="btn-primary" disabled={busy === "save"} onClick={() => void save()}>
              {editingId ? "保存修改" : "创建条目"}
            </button>
            {editingId && (
              <button
                className="btn-ghost"
                disabled={busy === `${editingId}:audition`}
                title="先播录音缓存；缺录音时主管侧现场合成"
                onClick={() =>
                  void audition({
                    id: editingId,
                    answer_text: form.answer_text,
                    lang: form.lang,
                    voice_id: form.voice_id,
                  })
                }
              >
                {busy === `${editingId}:audition` ? "合成中…" : "试听"}
              </button>
            )}
            {ok && <span className="text-sm text-emerald-600">已保存。</span>}
          </div>
          {!isManager && !editingId && (
            <p className="text-[11px] leading-relaxed muted">
              你创建的条目归属本人；共享条目由主管维护。
            </p>
          )}
        </section>
      </div>

      {/* 右键菜单(spec §4.4):节点=编辑/启停/试听/重新录音[主管]/删除(只读行仅试听);
          边=解除连线(簇边=散簇、步骤边=解挂,page.disconnect 按 kind 分路)。 */}
      {menu && (
        <div
          className="fixed inset-0 z-50"
          onClick={() => setMenu(null)}
          onContextMenu={(e) => { e.preventDefault(); setMenu(null); }}
        >
          <div
            className="absolute min-w-[112px] rounded-lg border border-(--card-border) bg-(--card) py-1 shadow-xl"
            style={{ left: menu.x, top: menu.y }}
          >
            {canEditRow(menu.row) && (
              <button className={menuItemCls} onClick={() => { setMenu(null); edit(menu.row); }}>编辑</button>
            )}
            {canEditRow(menu.row) && (
              <button className={menuItemCls} onClick={() => { setMenu(null); void toggleEnabled(menu.row); }}>
                {menu.row.enabled === false ? "启用" : "停用"}
              </button>
            )}
            <button className={menuItemCls} onClick={() => { setMenu(null); void audition(menu.row); }}>试听</button>
            {isManager && (
              <button className={menuItemCls} onClick={() => { setMenu(null); void pregenQa([String(menu.row.id)]); }}>
                重新录音
              </button>
            )}
            {canEditRow(menu.row) && (
              <button className={`${menuItemCls} text-red-600`} onClick={() => { setMenu(null); void remove(menu.row); }}>
                删除
              </button>
            )}
          </div>
        </div>
      )}
      {edgeMenu && (
        <div
          className="fixed inset-0 z-50"
          onClick={() => setEdgeMenu(null)}
          onContextMenu={(e) => { e.preventDefault(); setEdgeMenu(null); }}
        >
          <div
            className="absolute min-w-[112px] rounded-lg border border-(--card-border) bg-(--card) py-1 shadow-xl"
            style={{ left: edgeMenu.x, top: edgeMenu.y }}
          >
            {/* 绑定边(意图→目标)没有「解除」语义可走既有两个分支:右键菜单只给「编辑意图」
                (任务书允许的两种收口之一)——**绝不**落到 disconnect 的 patchQa 兜底
                (fromId="intent:xxx" 不是快答条目)。绑定的删除在意图编辑器行内,或选中
                绑定边按 Delete(走 onEdgesDelete → disconnect 的 binding 分支)。 */}
            {edgeMenu.edge.data?.kind === "binding" ? (
              canEditGraph ? (
                <button
                  className={menuItemCls}
                  onClick={() => {
                    const hit = edgeMenu.edge;
                    setEdgeMenu(null);
                    setEditingIntentForEdge(hit);
                  }}
                >
                  编辑意图
                </button>
              ) : (
                <span className={`${menuItemCls} muted cursor-default`}>绑定由主管维护</span>
              )
            ) : (
              <button
                className={menuItemCls}
                onClick={() => { const hit = edgeMenu.edge; setEdgeMenu(null); void disconnect(hit); }}
              >
                {edgeMenu.edge.data?.kind === "cluster" ? "解除变体归属" : "解除步骤挂载"}
              </button>
            )}
          </div>
        </div>
      )}

      {/* 意图编辑器(Task 8):key=intent id → 换意图即重挂,表单状态自然重置。
          意图必须还在当前图里(草稿被放弃/图被替换时不渲染空窗)。 */}
      {editingIntent && session && (
        <IntentEditorModal
          key={editingIntent.id}
          intent={editingIntent}
          bindings={graphDoc.bindings.filter((b) => b.intent === editingIntent.id)}
          isDraft={draftIntent?.id === editingIntent.id}
          readOnly={!canEditGraph}
          steps={templateSteps}
          qaRows={rows ?? []}
          onConfirm={confirmIntentEditing}
          onCancel={closeIntentEditor}
          onDelete={() => {
            if (!window.confirm(`确认删除意图「${editingIntent.label || "(未命名意图)"}」及其全部绑定？\n删除后该意图不再触发。`)) return;
            deleteIntent(editingIntent.id);
            setEditorIntentId(null);
            setDraftIntent(null);
          }}
        />
      )}
    </div>
  );
}

/**
 * 意图编辑器模态(话术图 Phase 2 Task 8)。
 *
 * 受控表单:名称 / 关键词(逗号分隔 ↔ 数组) / 生效步骤(全程 checkbox + 步号 chips,1-based) /
 * 绑定列表(动作 play_qa→选快答条目, jump_step→选步号；每行优先级/只执行一次/启用/删除) /
 * 意图级启用 / 删除意图。确认=A. 空草稿放弃(B. 校验失败留窗报错) / C. 整图 PUT 落库。
 *
 * **Backspace 契约(必须保持)**:ReactFlow 的 deleteKeyCode 是 ["Backspace","Delete"],
 * 其 useKeyPress 判定 `isInputDOMNode(event)`——事件源是 input/select/textarea/contentEditable
 * 或**其祖先带 `.nokey`** 时整键忽略(@xyflow/system esm index.js:915-922)。故这里双保险:
 * ①根覆盖层带 `.nokey`(焦点落到窗内任何位置都被吞);②`autoFocus` 把焦点直接落进名称输入框。
 * 二者缺一即「开着窗、画布上意图节点还选中着 → 一个退格静默删掉意图+绑定」。
 *
 * 校验口径与 CP validate_flow_graph 逐条对齐(label 1-64 / keywords 1-32 项且单项 ≤64 /
 * priority 0-1000 / jump_step 步号 1..真实步数 / play_qa 必带 qa_id):前端先拦,不让必 400 的
 * 请求上线。另有**第三态护栏**(review R1 Fault 3):「全程」未勾且一个步号都没勾 → 硬报错留窗,
 * 绝不落库 `steps: []`(运行时把空 steps 读成**全程**,与操作者「收窄」的预期正好相反)。
 */
function IntentEditorModal(props: {
  intent: GraphIntent;
  bindings: GraphBinding[];
  isDraft: boolean;
  /** 无编辑权(共享话术且非主管):全字段禁用,只留查看与关闭。 */
  readOnly: boolean;
  steps: FlowStep[];
  qaRows: QaRow[];
  onConfirm: (intent: GraphIntent, bindings: GraphBinding[]) => Promise<boolean>;
  onCancel: () => void;
  onDelete: () => void;
}) {
  const { intent, isDraft, readOnly, steps, qaRows } = props;
  const stepCount = steps.length;
  const [label, setLabel] = useState(intent.label);
  const [keywordsText, setKeywordsText] = useState(intent.keywords.join("，"));
  const [allSteps, setAllSteps] = useState(intent.steps.length === 0);
  // 越界步号(话术步数被改小后)不进编辑态:chips 只画真实步,保存时随之剔除(下方提示)。
  const [scopeSteps, setScopeSteps] = useState<number[]>(() =>
    [...new Set(intent.steps.filter((s) => s >= 1 && s <= stepCount))].sort((a, b) => a - b),
  );
  const [enabled, setEnabled] = useState(intent.enabled !== false);
  const [rows, setRows] = useState<BindingDraft[]>(() =>
    props.bindings.map((b) => bindingToDraft(b, stepCount)),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const droppedSteps = intent.steps.filter((s) => s < 1 || s > stepCount).length;
  // Backspace 契约的兜底(见组件注释):只读档全部 input disabled → autoFocus 落空,
  // 焦点可能还留在 body(此时 .nokey 祖先链不存在、退格不被吞)。故只读档把焦点主动
  // 收进覆盖层本体(tabIndex=-1);可编辑档 autoFocus 已把焦点落进名称输入框,不动。
  const rootRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (readOnly) rootRef.current?.focus();
  }, [readOnly]);

  function toggleStep(step: number) {
    setAllSteps(false);
    setScopeSteps((cur) =>
      cur.includes(step) ? cur.filter((s) => s !== step) : [...cur, step].sort((a, b) => a - b),
    );
  }

  function addBinding() {
    setRows((cur) => [
      ...cur,
      {
        id: genGraphId("bnd_"),
        action: "play_qa", // 默认动作=播快答(不依赖步数,零步骤话术也能建)
        qa_id: "",
        step: clampInt(1, 1, Math.max(stepCount, 1)),
        priority: 10, // spec:优先级默认 10(小者先)
        once: false,
        enabled: true,
      },
    ]);
  }

  function patchRow(id: string, patch: Partial<BindingDraft>) {
    setRows((cur) => cur.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  async function submit() {
    const keywords = splitKeywords(keywordsText);
    const name = label.trim();
    // 空草稿=放弃(新建后既没名字也没关键词):本地剔除,零请求。
    if (isDraft && !name && keywords.length === 0) {
      props.onCancel();
      return;
    }
    if (!name) return setError("意图名称不能为空。");
    if (keywords.length === 0) return setError("至少填一个触发关键词（客户话里会出现的说法）。");
    if (keywords.length > 32) return setError("触发关键词最多 32 个。");
    const overlong = keywords.find((k) => k.length > 64);
    if (overlong) return setError(`单个关键词最长 64 字，请改短：「${overlong.slice(0, 10)}…」`);
    // 生效步骤第三态护栏(review R1 Fault 3):「全程」未勾且一个步骤都没勾 → 落库 steps=[]
    // 会被运行时读成**全程**(pick_graph_action:空 steps=不限步),语义正好反了——操作者以为
    // 收窄了,实际放开了。越界步号档同理:chips 不画那个步号 → 选择为空 → 同一错误,绝不静默
    // 拓宽成全程(改个名字就把生效范围放大,是最隐蔽的一条)。
    if (!allSteps && scopeSteps.length === 0) {
      return setError("生效步骤一个都没选：请勾「全程」，或至少勾选一个步骤（空选择不生效）。");
    }
    const nextBindings: GraphBinding[] = [];
    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      const common = {
        id: row.id,
        intent: intent.id,
        priority: clampInt(row.priority, 0, 1000),
        once: row.once,
        enabled: row.enabled,
      };
      if (row.action === "play_qa") {
        if (!qaRows.some((q) => String(q.id) === row.qa_id)) {
          return setError(`第 ${i + 1} 条绑定还没有选择要播的快答条目。`);
        }
        nextBindings.push({ ...common, action: "play_qa", qa_id: row.qa_id });
      } else {
        if (stepCount < 1) return setError("该话术还没有步骤，无法使用「跳到某一步」。");
        nextBindings.push({ ...common, action: "jump_step", step: clampInt(row.step, 1, stepCount) });
      }
    }
    setSaving(true);
    const ok = await props.onConfirm(
      { ...intent, label: name.slice(0, 64), keywords, steps: allSteps ? [] : scopeSteps, enabled },
      nextBindings,
    );
    setSaving(false);
    if (!ok) setError("保存失败，未能写库（错误详情见页面顶部提示）。");
  }

  return (
    <div
      ref={rootRef}
      tabIndex={-1}
      className="nokey fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/40 p-4 pt-16 outline-hidden"
      onClick={props.onCancel}
      onContextMenu={(e) => e.preventDefault()}
    >
      <div
        className="w-full max-w-2xl rounded-xl border border-(--card-border) bg-(--card) p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <span className="label">{isDraft ? "新建意图" : "编辑意图"}</span>
            <p className="mt-1 text-[11px] leading-relaxed muted">
              客户话里命中任一关键词即触发，按优先级从小到大取第一条绑定执行。
            </p>
          </div>
          <button className="btn-ghost text-xs" onClick={props.onCancel}>关闭</button>
        </div>

        {readOnly && (
          <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
            这是共享话术的意图，由主管维护；你可以查看但不能修改。
          </p>
        )}

        <div className="mt-4 space-y-3">
          <label className="block">
            <span className="text-xs text-muted-foreground">意图名称</span>
            <input
              autoFocus
              className="input mt-1"
              maxLength={64}
              value={label}
              disabled={readOnly}
              onChange={(e) => { setLabel(e.target.value); setError(""); }}
              placeholder="如：客户投诉"
            />
          </label>

          <label className="block">
            <span className="text-xs text-muted-foreground">触发关键词（逗号分隔）</span>
            <input
              className="input mt-1"
              value={keywordsText}
              disabled={readOnly}
              onChange={(e) => { setKeywordsText(e.target.value); setError(""); }}
              placeholder="投诉，我要投诉，找主管"
            />
            <span className="mt-1 block text-[11px] leading-relaxed muted">
              客户原话里按字面出现任一关键词即命中（不认同义改写，按客户实际说法写）；标点与空格不影响匹配；最多 32 个、单个 ≤64 字。
            </span>
          </label>

          <div>
            <span className="text-xs text-muted-foreground">生效步骤</span>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-1.5 text-[11px] muted">
                <input
                  type="checkbox"
                  className="size-3 accent-(--live)"
                  checked={allSteps}
                  disabled={readOnly}
                  onChange={(e) => { setAllSteps(e.target.checked); setError(""); }}
                />
                全程
              </label>
              {Array.from({ length: stepCount }, (_, i) => i + 1).map((s) => (
                <button
                  key={s}
                  type="button"
                  disabled={readOnly}
                  className={`rounded-sm border px-2 py-0.5 text-[11px] ${
                    !allSteps && scopeSteps.includes(s)
                      ? "border-(--live) text-(--live-ink)"
                      : "border-(--card-border) muted"
                  }`}
                  onClick={() => { toggleStep(s); setError(""); }}
                >
                  第 {s} 步
                </button>
              ))}
            </div>
            <span className="mt-1 block text-[11px] leading-relaxed muted">
              {stepCount > 0
                ? "勾「全程」=整通通话都生效；否则只在勾选的步骤生效。"
                : "该话术还没有步骤，意图只能在「全程」生效。"}
            </span>
            {droppedSteps > 0 && (
              <span className="mt-1 block text-[11px] text-amber-600">
                原有 {droppedSteps} 个生效步号超出该话术的步数，已忽略（保存后移除）。
              </span>
            )}
            {/* 第三态提前显形(review R1 Fault 3):空选择保存必被拦(见 submit),在这里先说清,
                免操作者按了保存才知道——尤其越界步号档(看起来像"收窄过"的样子)。 */}
            {!allSteps && scopeSteps.length === 0 && (
              <span className="mt-1 block text-[11px] text-amber-600">
                当前一个步骤都没选：勾「全程」或至少选一个步骤，否则无法保存（空选择不生效，不等于全程）。
              </span>
            )}
          </div>

          <div className="rounded-lg border border-(--card-border) p-3">
            <div className="flex items-center justify-between">
              <span className="label">绑定动作（{rows.length}）</span>
              {!readOnly && (
                <button className="btn-ghost text-xs" onClick={addBinding}>添加绑定</button>
              )}
            </div>
            {rows.length === 0 && (
              <p className="mt-2 text-[11px] muted">还没有绑定：命中关键词后不会做任何事。</p>
            )}
            <div className="mt-2 space-y-2">
              {rows.map((row, i) => (
                <div key={row.id} className="rounded-lg bg-muted/60 p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[10px] muted">{i + 1}</span>
                    <select
                      className="select text-xs"
                      value={row.action}
                      disabled={readOnly}
                      onChange={(e) =>
                        patchRow(row.id, {
                          action: e.target.value === "jump_step" ? "jump_step" : "play_qa",
                        })
                      }
                    >
                      <option value="play_qa">播快答</option>
                      <option value="jump_step">跳到某步</option>
                    </select>
                    {row.action === "play_qa" ? (
                      <select
                        className="select min-w-[180px] flex-1 text-xs"
                        value={row.qa_id}
                        disabled={readOnly}
                        onChange={(e) => { patchRow(row.id, { qa_id: e.target.value }); setError(""); }}
                      >
                        <option value="">选择快答条目…</option>
                        {qaRows.map((q) => (
                          <option key={String(q.id)} value={String(q.id)}>
                            {String(q.question_text ?? "(无问法)")}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <select
                        className="select text-xs"
                        value={String(row.step)}
                        disabled={readOnly || stepCount < 1}
                        onChange={(e) => patchRow(row.id, { step: Number(e.target.value) || 1 })}
                      >
                        {stepCount < 1 ? (
                          <option value="1">（该话术没有步骤）</option>
                        ) : (
                          Array.from({ length: stepCount }, (_, k) => k + 1).map((s) => (
                            <option key={s} value={String(s)}>第 {s} 步</option>
                          ))
                        )}
                      </select>
                    )}
                    <label className="flex items-center gap-1 text-[11px] muted">
                      优先级
                      <input
                        type="number"
                        min={0}
                        max={1000}
                        className="input w-16 px-1.5 py-0.5 text-xs"
                        value={row.priority}
                        disabled={readOnly}
                        onChange={(e) => {
                          // 输入中途（空串/单减号）Number() 可能出 NaN——受控 number 收到 NaN 会
                          // 打 React 警告并清空显示，这里统一落回默认档 10，落库前再 clampInt。
                          const n = Number(e.target.value);
                          patchRow(row.id, { priority: Number.isFinite(n) ? n : 10 });
                        }}
                      />
                    </label>
                    <label className="flex items-center gap-1 text-[11px] muted">
                      <input
                        type="checkbox"
                        className="size-3 accent-(--live)"
                        checked={row.once}
                        disabled={readOnly}
                        onChange={(e) => patchRow(row.id, { once: e.target.checked })}
                      />
                      只执行一次
                    </label>
                    <label className="flex items-center gap-1 text-[11px] muted">
                      <input
                        type="checkbox"
                        className="size-3 accent-(--live)"
                        checked={row.enabled}
                        disabled={readOnly}
                        onChange={(e) => patchRow(row.id, { enabled: e.target.checked })}
                      />
                      启用
                    </label>
                    {!readOnly && (
                      <button
                        className="btn-ghost text-xs text-red-600"
                        onClick={() => setRows((cur) => cur.filter((r) => r.id !== row.id))}
                      >
                        删除
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <label className="flex items-center gap-1.5 text-xs muted">
            <input
              type="checkbox"
              className="size-3 accent-(--live)"
              checked={enabled}
              disabled={readOnly}
              onChange={(e) => { setEnabled(e.target.checked); setError(""); }}
            />
            启用该意图（停用后关键词不再触发，绑定的动作保留）
          </label>

          {error && <p className="text-xs text-red-600">{error}</p>}

          <div className="flex items-center justify-between gap-3 pt-1">
            <div>
              {!isDraft && !readOnly && (
                <button className="btn-ghost text-xs text-red-600" onClick={props.onDelete}>
                  删除意图
                </button>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button className="btn-ghost text-xs" onClick={props.onCancel}>
                {isDraft ? "放弃" : "取消"}
              </button>
              {!readOnly && (
                <button className="btn-primary" disabled={saving} onClick={() => void submit()}>
                  {saving ? "保存中…" : "保存"}
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
