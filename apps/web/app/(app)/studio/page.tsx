"use client";

// AI 工作站（W1；2026-09-20 重设计）：以话术模板为入口的集中工作台。
// 列表态（无 ?t=）：全部模板概览 + 新建话术（话术/快答内容全部整合在此，
// /templates、/qa 移出主导航后这里成为唯一内容入口）；
// 工作台态（?t=<id>）：话术流程 + 意图管理 + 变量 + 客户意向 + 录音沉淀 +
// 通话日志 + 学习报告 tab（对齐参考产品的主流程/意图管理/变量设定/客户意向/录音管理 tab 面）。
// （学习报告/聚类采纳在 components/study-tab.tsx,变量目录与预览在 components/template-vars.tsx）。
// 深链先例：/calls?call= / /supervisor?listen= —— 静态导出用 query，不开动态路由。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { api, type UserRow } from "@/lib/api";
import { downloadCsv, parseBoolCell } from "@/lib/csv";
import { serializeStepRef } from "@/lib/flow-canvas";
import { parseGraphDoc, parseTemplateSteps } from "@/lib/qa-canvas";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { hasPage, useSession } from "@/components/session-context";
import TableImport, {
  buildExampleCsvRows, rowOk, rowSkip, type ImportResult, type ParsedRow,
} from "@/components/table-import";
import TemplateEditor, {
  LANGS,
  PublishBadge,
  jsonToSteps,
  stepsToJson,
  toTemplateRow,
  type FlowStep,
  type TemplateRow,
} from "@/components/template-editor";
import FlowCanvas from "@/components/flow-canvas";
import StepsListEditor from "@/components/steps-list-editor";
import StudyTab from "@/components/study-tab";
import GapMining from "@/components/gap-mining";
import TemplateVarsTab from "@/components/template-vars";
import CannedAuditionCard from "@/components/canned-audition";
import IntentRulesCard from "@/components/intent-rules-card";
import IntentManager from "@/components/intent-manager";
import QaLibrary from "@/components/qa-library";
import { seedPackFor } from "@/lib/seed-pack";

// 通话行状态徽标（照 calls 页惯例搬一份,页面文件不可导入）。
const CALL_STATUS: Record<string, [string, string]> = {
  active: ["进行中", "bg-emerald-500"],
  ringing: ["振铃", "bg-amber-400"],
  paused: ["已暂停", "bg-blue-400"],
  ended: ["已结束", "bg-neutral-500"],
  failed: ["失败", "bg-red-400"],
};
const WA_PENDING = ["offered", "captured"];
const LANG_LABEL: Record<string, string> = {
  zh: "中文",
  cantonese: "粤语",
  en: "英语",
  vi: "越南语",
};

// 主流程 tab 双视图记忆（2026-09-25 用户拍板）：默认「列表编辑」（表单/卡片对运营更
// 易用;画布不能连线、整理弱,降为切换选项）,用户上次选择记 localStorage,首访=列表。
const FLOW_VIEW_STORAGE_KEY = "bok.flow.view";
function readStoredFlowView(): "form" | "canvas" {
  // 静态导出预渲染期无 window;存储值非法一律回落列表（首访默认）。
  if (typeof window === "undefined") return "form";
  try {
    return window.localStorage.getItem(FLOW_VIEW_STORAGE_KEY) === "canvas" ? "canvas" : "form";
  } catch {
    return "form"; // 存储不可用（隐私模式等）:仅不记忆选择,功能不受影响
  }
}

// ---- 表格导入契约（主流程 tab「导入话术」，2026-09-25）：列=步号,目的,AI主要说的话,
// 客户这样说→AI怎么做（分支行：`条件 :: 应答` 每格一条,多条用 | 分隔）,逐字照念(是/否),
// 补充提醒。组装 steps_json 走既有模板保存（单键 steps_json），整表替换前弹确认。----
const FLOW_IMPORT_COLUMNS = [
  { key: "no", label: "步号", hint: "必填；≥1 整数，决定顺序" },
  { key: "goal", label: "目的", hint: "这一步要达成什么（可空）" },
  { key: "script", label: "AI主要说的话", hint: "参考说法正文（可空）" },
  { key: "branch", label: "客户这样说→AI怎么做", hint: "格式「条件 :: 应答」，多条用 | 分隔" },
  { key: "say", label: "逐字照念", hint: "是/否，可空=否（通知/道歉等合规内容用）" },
  { key: "note", label: "补充提醒", hint: "可空；多条用 | 分隔" },
];
const FLOW_IMPORT_FILENAME = "flow-steps-example.csv";
const FLOW_IMPORT_EXAMPLE: string[][] = [
  ["1", "确认身份", "您好，请问是{姓名}本人吗？我是{物流公司}客服。", "", "否", ""],
  ["2", "说明来意并致歉", "您的包裹在运输途中丢失了，非常抱歉，我们按承诺给您办理理赔。", "现在没空 :: 好的，那您方便的时候我再给您来电|已经知道了 :: 好的，那我们直接进入理赔办理", "是", "赔偿档位只在这一步讲，其他步骤不报数字"],
  ["3", "索取联系方式", "麻烦把您的微信号报给我，理赔专员会加您办理。", "不方便 :: 问什么时候方便，约好时间再跟进", "否", ""],
  ["4", "收尾", "感谢您的配合，祝您生活愉快，再见。", "", "否", ""],
];

/** 主流程表格一行解析出的数据（预览/导入共用）。 */
type FlowImportRow = {
  /** 步号（≥1；决定步骤顺序，允许跳号）。 */
  no: number;
  goal: string;
  script: string;
  branches: { cond: string; resp: string }[];
  say: boolean;
  /** 注意行内容（多条 \n 连接，序列化时逐行还原「注意：」头）。 */
  notes: string;
};

/** 分支格一条「条件 :: 应答」→ (cond, resp)；缺分隔/缺件=null（预览标跳过）。 */
function parseBranchCell(piece: string): { cond: string; resp: string } | null {
  const m = piece.match(/^(.+?)\s*(?:::|：：)\s*(.+)$/);
  if (!m) return null;
  const cond = m[1].trim();
  const resp = m[2].trim();
  return cond && resp ? { cond, resp } : null;
}

/** 单行解析（纯函数；prev=先前各行，做步号查重——预览期即标跳过）。 */
function parseFlowImportRow(row: string[], prev: ParsedRow<FlowImportRow>[]): ParsedRow<FlowImportRow> {
  const noRaw = String(row[0] ?? "").trim();
  const no = Math.round(Number(noRaw));
  if (!noRaw || !Number.isFinite(no) || no < 1) {
    return rowSkip(`步号须为 ≥1 的整数（当前「${noRaw || "空"}」）`);
  }
  const seenNos = new Set<number>();
  for (const p of prev) if (p.ok) seenNos.add(p.data.no);
  if (seenNos.has(no)) return rowSkip(`步号 ${no} 重复`);
  const goal = String(row[1] ?? "").trim();
  const script = String(row[2] ?? "").trim();
  const branchCell = String(row[3] ?? "").trim();
  const branches: { cond: string; resp: string }[] = [];
  if (branchCell) {
    for (const piece of branchCell.split("|")) {
      const t = piece.trim();
      if (!t) continue;
      const b = parseBranchCell(t);
      if (!b) return rowSkip(`分支「${t.slice(0, 20)}」缺 :: 分隔（格式：条件 :: 应答）`);
      branches.push(b);
    }
  }
  const say = parseBoolCell(String(row[4] ?? ""), false);
  if (say === null) return rowSkip(`逐字照念「${String(row[4]).trim()}」须为 是/否`);
  const noteCell = String(row[5] ?? "").trim();
  const notes = noteCell
    ? noteCell.split(/\s*\|\s*|\r?\n\s*/).map((n) => n.trim()).filter(Boolean)
    : [];
  if (!goal && !script && branches.length === 0 && notes.length === 0) {
    return rowSkip("该行没有任何内容");
  }
  return rowOk({ no, goal, script, branches, say, notes: notes.join("\n") });
}

export default function StudioPage() {
  const { accountId } = useAccount();
  const session = useSession();
  const isManager = Boolean(
    session && (session.anonymous || session.role === "admin" || session.role === "root"),
  );
  const uid = session?.user_id ?? "";
  // 学习报告 = reports 权限键（B4 页面矩阵；会话未加载时不出该 tab）。
  const canReports = hasPage(session, "reports");

  // ---- 深链：?t=<模板 id> 进工作台态 ----
  const [selId, setSelId] = useState("");
  useEffect(() => {
    const m = window.location.search.match(/[?&]t=([^&]+)/);
    if (m) setSelId(decodeURIComponent(m[1]));
  }, []);

  // ---- 列表态数据 ----
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [userNames, setUserNames] = useState<Record<string, string>>({});
  // 新建话术面板（2026-09-20：/templates 移出主导航，这里成为唯一内容入口）。
  const [creating, setCreating] = useState(false);
  // 列表刷新序号：新建保存成功后 +1 重拉列表。
  const [listRev, setListRev] = useState(0);

  useEffect(() => {
    if (selId) return; // 工作台态不拉列表
    let alive = true;
    setLoading(true);
    api.listTemplates(accountId)
      .then((data) => {
        if (!alive) return;
        setRows(Array.isArray(data) ? data : []);
        setErr("");
      })
      .catch((e) => {
        if (alive) setErr(String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [selId, accountId, listRev]);

  // 主管面归属徽标要显示成员姓名（话务员无权访问 /api/users，不请求；照 templates 页惯例）。
  useEffect(() => {
    if (!isManager || selId) return;
    let alive = true;
    void (async () => {
      try {
        const raw = (await api.listUsers()) as unknown;
        const list = Array.isArray(raw)
          ? (raw as UserRow[])
          : Array.isArray((raw as { users?: UserRow[] })?.users)
            ? (raw as { users: UserRow[] }).users
            : [];
        if (!alive) return;
        const map: Record<string, string> = {};
        list.forEach((u) => {
          map[u.id] = u.display_name || u.username || u.id;
        });
        setUserNames(map);
      } catch {
        if (alive) setUserNames({});
      }
    })();
    return () => {
      alive = false;
    };
  }, [isManager, selId]);

  // ---- 工作台态：模板详情（权威行；tplRev 供保存成功后重拉） ----
  const [tpl, setTpl] = useState<Record<string, unknown> | null>(null);
  const [tplLoading, setTplLoading] = useState(false);
  const [tplErr, setTplErr] = useState("");
  const [tplRev, setTplRev] = useState(0);

  useEffect(() => {
    if (!selId) {
      setTpl(null);
      return;
    }
    let alive = true;
    setTplLoading(true);
    api.getTemplate(selId)
      .then((row) => {
        if (!alive) return;
        setTpl((row ?? null) as Record<string, unknown>);
        setTplErr("");
      })
      .catch((e) => {
        if (!alive) return;
        setTpl(null);
        setTplErr(String(e));
      })
      .finally(() => {
        if (alive) setTplLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [selId, tplRev]);

  const tplRow: TemplateRow | null = useMemo(() => (tpl ? toTemplateRow(tpl) : null), [tpl]);
  // 意图图与步骤脊柱（graph_json 解析一律走 lib/qa-canvas.parseGraphDoc 现成实现）。
  const graph = useMemo(() => parseGraphDoc(tplRow?.graph_json ?? ""), [tplRow]);
  const stepsList = useMemo(() => parseTemplateSteps(tplRow?.steps_json ?? ""), [tplRow]);
  // 内容只读判定（B4 owner 口径，与 FlowCanvas/TemplateEditor 同款）：共享话术非主管=只读。
  const contentReadOnly = Boolean(selId) && !isManager && !(uid !== "" && String(tplRow?.owner_user_id ?? "") === uid);

  // ---- 步骤草稿（工作站层唯一持有；画布与列表编辑同一份——切换视图不丢修改） ----
  const [stepsDraft, setStepsDraft] = useState<FlowStep[]>([]);
  const [stepsDirty, setStepsDirty] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applyErr, setApplyErr] = useState("");
  const [applyNote, setApplyNote] = useState("");
  const [publishing, setPublishing] = useState(false);

  // 锚定：模板行变化（进入工作台/应用成功重拉/保存设置重拉）→ 草稿=落库值、清脏。
  // **脏修改在途时模板行重拉不重锚**（保存「模板设置」会触发重拉——重置草稿=冲掉
  // 未应用的步骤修改,正是本次改版要消灭的丢编辑陷阱）；换模板必重锚。
  const anchoredSelRef = useRef(selId);
  useEffect(() => {
    const changedTemplate = anchoredSelRef.current !== selId;
    anchoredSelRef.current = selId;
    if (!changedTemplate && stepsDirty) return;
    setStepsDraft(jsonToSteps(tpl?.steps_json));
    setStepsDirty(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selId, tpl]);

  const changeSteps = useCallback((next: FlowStep[]) => {
    setStepsDraft(next);
    setStepsDirty(true);
    setApplyNote("");
  }, []);

  // ---- 主流程表格导入（整表替换 steps_json）----
  const [flowImportOpen, setFlowImportOpen] = useState(false);

  /** 组装并保存：按步号升序成步，ref=serializeStepRef（正稿+分支+注意三件，
   * 语法与 lib/flow-canvas 对 flow.py _BRANCH_LINE_RE/_NOTE_LINE_RE 的镜像一致）。
   * 保存后草稿直接重锚为导入结果（不等重拉），再 tplRev+1 取权威行。 */
  async function importFlowRows(rows: FlowImportRow[]): Promise<ImportResult> {
    const ordered = [...rows].sort((a, b) => a.no - b.no);
    const steps: FlowStep[] = ordered.map((r) => ({
      goal: r.goal,
      ref: serializeStepRef({ script: r.script, branches: r.branches, notes: r.notes }),
      ...(r.say ? { say: true } : {}),
    }));
    await api.updateTemplate(selId, { steps_json: stepsToJson(steps) });
    setStepsDraft(steps);
    setStepsDirty(false);
    setApplyNote(`已从表格导入 ${steps.length} 步（整表替换）`);
    setTplRev((v) => v + 1);
    return { done: steps.length, failed: 0, errors: [] };
  }

  /** 全局「应用」：唯一保存入口（只写 steps_json 单键,PUT exclude_unset 部分更新）。 */
  async function applySteps() {
    if (!selId || applying) return;
    setApplying(true);
    setApplyErr("");
    try {
      await api.updateTemplate(selId, { steps_json: stepsToJson(stepsDraft) });
      const dropped = stepsDraft.filter((s) => !s.goal.trim() && !s.ref.trim()).length;
      setApplyNote(dropped > 0 ? `已应用（忽略了 ${dropped} 个空白步）` : "已应用");
      // 应用后即清脏（重拉到达前用户可见状态正确;锚定效应随后重锚为同一份落库值）。
      setStepsDirty(false);
      setTplRev((v) => v + 1); // 重拉模板行（发布徽标等随之取权威数据）
    } catch (e) {
      setApplyErr(String(e));
    } finally {
      setApplying(false);
    }
  }

  /** 发布（新通话用冻结版）：有未应用修改先提醒——发布的是已保存版本。 */
  async function publishNow() {
    if (!selId) return;
    if (stepsDirty && !window.confirm("还有未应用的修改：发布的是「已保存」的版本。建议先点「应用」。\n\n仍要直接发布？")) return;
    if (!window.confirm("发布后，之后拨出的电话都按这个版本讲。确定发布？")) return;
    setPublishing(true);
    try {
      await api.publishTemplate(selId);
      setTplRev((v) => v + 1);
    } catch (e) {
      setApplyErr(String(e));
    } finally {
      setPublishing(false);
    }
  }

  // 有未应用修改时拦浏览器关闭/刷新（站点内导航走「返回列表」的确认）。
  useEffect(() => {
    if (!stepsDirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [stepsDirty]);

  /** 返回列表：脏=确认（未应用的修改会丢——页面态不跨模板保留）。 */
  function backToList() {
    if (stepsDirty && !window.confirm("主流程的修改还没应用，返回会丢失。仍要返回？")) return;
    window.location.assign("/studio/");
  }

  // ---- tab 切换（qa 页 view chips 同款写法）；学习报告 tab 仅对有 reports 键的人出现 ----
  const [tab, setTab] = useState("flow");
  // 主流程 tab 双视图：默认「列表编辑」,画布为切换选项;选择记 localStorage（见文件头注释）。
  const [flowView, setFlowView] = useState<"form" | "canvas">(readStoredFlowView);
  const switchFlowView = useCallback((v: "form" | "canvas") => {
    setFlowView(v);
    try {
      window.localStorage.setItem(FLOW_VIEW_STORAGE_KEY, v);
    } catch {
      // 存储不可用:选择只在本次会话生效,功能不受影响。
    }
  }, []);
  const tabs: [string, string][] = [
    ["flow", "主流程"],
    ["intent", "意图管理"],
    ["qa", "问答库"],
    ["vars", "变量设定"],
    ["disposition", "客户意向"],
    ["canned", "录音沉淀"],
    ["calls", "通话日志"],
  ];
  if (canReports) tabs.push(["reports", "场景学习"]);

  // ---- 罐头录音 tab：挂该模板步骤的 QA 词条 + 罐头物化状态 ----
  const [qaRows, setQaRows] = useState<Record<string, unknown>[]>([]);
  const [canned, setCanned] = useState<Record<string, { state: "ok" | "missing" }>>({});
  const [cannedLoading, setCannedLoading] = useState(false);
  const [cannedErr, setCannedErr] = useState("");
  useEffect(() => {
    if (tab !== "canned" || !selId) return;
    let alive = true;
    setCannedLoading(true);
    void (async () => {
      try {
        const data = await api.listQaAll(accountId);
        if (!alive) return;
        setQaRows(Array.isArray(data) ? data : []);
        setCannedErr("");
      } catch (e) {
        if (!alive) return;
        setQaRows([]);
        setCannedErr(String(e));
      } finally {
        if (alive) setCannedLoading(false);
      }
      // 罐头状态面拉取失败=无徽标降级（qa 页同款口径），不阻塞列表渲染。
      try {
        const res = await api.qaCannedStatus(accountId);
        if (alive) setCanned(res?.statuses ?? {});
      } catch {
        if (alive) setCanned({});
      }
    })();
    return () => {
      alive = false;
    };
  }, [tab, selId, accountId]);

  const stepQaRows = useMemo(
    () =>
      qaRows.filter(
        (r) => String(r.template_id ?? "") === selId && String(r.scope ?? "") === "step",
      ),
    [qaRows, selId],
  );

  // ---- 分支罐头状态（主流程画布答法抽屉，2026-09-20）：statuses 键=分支 resp
  // 原文（含动作标记）。进主流程 tab / 换模板 / 换账号拉一次（TTL 缓存在 CP 侧，
  // 补录后 branchRev+1 定点刷一次，不轮询）。拉取失败=无徽标降级，不阻塞画布。 ----
  const [branchCanned, setBranchCanned] = useState<Record<string, "ok" | "missing" | "ph">>({});
  const [branchRev, setBranchRev] = useState(0);
  const [branchNote, setBranchNote] = useState("");
  useEffect(() => {
    if (tab !== "flow" || !selId) return;
    let alive = true;
    api.branchCannedStatus(accountId)
      .then((res) => {
        if (alive) setBranchCanned(res?.statuses ?? {});
      })
      .catch(() => {
        if (alive) setBranchCanned({});
      });
    return () => {
      alive = false;
    };
  }, [tab, selId, accountId, branchRev]);

  /** 分支一键补录（答法抽屉「补录这条」）：queued→人话提示+5s 后刷一次状态。 */
  async function pregenBranch(resp: string) {
    setBranchNote("");
    try {
      const res = await api.branchPregen(accountId, [resp]);
      if (res?.status === "queued") {
        setBranchNote("补录任务已排队，AI 正在生成这条录音，稍后自动刷新状态。");
        window.setTimeout(() => {
          setBranchNote("");
          setBranchRev((v) => v + 1);
        }, 5000);
      } else if (res?.status === "already_running") {
        setBranchNote("上一批补录还在进行中，等它跑完再试。");
      } else {
        setBranchNote(`补录没有启动（${res?.status ?? "unknown"}）。`);
      }
    } catch (e) {
      setBranchNote(`补录失败：${String(e)}`);
    }
  }

  // ---- 通话日志 tab：全量拉取后客户端按模板过滤（照 calls 页惯例） ----
  const [callRows, setCallRows] = useState<Record<string, unknown>[]>([]);
  const [callsLoading, setCallsLoading] = useState(false);
  const [callsErr, setCallsErr] = useState("");
  useEffect(() => {
    if (tab !== "calls" || !selId) return;
    let alive = true;
    setCallsLoading(true);
    api.listCalls(accountId, "")
      .then((data) => {
        if (!alive) return;
        setCallRows(Array.isArray(data) ? data : []);
        setCallsErr("");
      })
      .catch((e) => {
        if (alive) setCallsErr(String(e));
      })
      .finally(() => {
        if (alive) setCallsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [tab, selId, accountId]);

  const tplCalls = useMemo(() => {
    const filtered = callRows.filter((c) => String(c.template_id ?? "") === selId);
    filtered.sort((a, b) => String(b.created_at ?? "").localeCompare(String(a.created_at ?? "")));
    return filtered;
  }, [callRows, selId]);
  const visibleCalls = tplCalls.slice(0, 50);

  // ---- 学习报告 tab：渲染迁入 components/study-tab.tsx（W3：+AI 聚类采纳面板） ----

  const langText = (raw: string) => LANGS.find((l) => l[0] === raw)?.[1] ?? raw;

  // ---- 列表态渲染 ----
  if (!selId) {
    return (
      <div>
        <div className="mb-8 flex items-start justify-between gap-3">
          <div>
            <h1 className="page-title">AI 工作站</h1>
            <p className="page-sub">按话术模板集中作业：话术流程 · 意图管理 · 变量 · 客户意向 · 录音沉淀 · 通话日志 · 学习报告</p>
          </div>
          {!creating && (
            <button className="btn-primary shrink-0" onClick={() => setCreating(true)}>
              ＋ 新建话术
            </button>
          )}
        </div>

        {creating && (
          <section className="mb-6 space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">新建话术模板</span>
              <button className="btn-ghost text-xs" onClick={() => setCreating(false)}>
                收起 ✕
              </button>
            </div>
            <TemplateEditor
              tpl={null}
              onSaved={() => {
                setCreating(false);
                setListRev((v) => v + 1);
              }}
            />
          </section>
        )}

        <section className="card">
          {err && <ErrorState message={err} />}
          {loading ? (
            <LoadingState />
          ) : rows.length === 0 ? (
            <EmptyState label="暂无话术模板，点右上角「新建话术」创建。" />
          ) : (
            <div className="space-y-3">
              {rows.map((row) => {
                const id = String(row.id ?? "");
                const ownerId = String(row.owner_user_id ?? "");
                const isMine = uid !== "" && ownerId === uid;
                const ownerLabel =
                  ownerId === ""
                    ? "共享"
                    : isManager
                      ? userNames[ownerId] || "个人"
                      : isMine
                        ? "我的"
                        : "他人";
                const langRaw = String(row.language ?? "zh");
                const stepCount = parseTemplateSteps(String(row.steps_json ?? "")).length;
                // 意图数=列表行携带的 graph_json 现算（计数仅展示、权威以详情为准）。
                const graphRaw = typeof row.graph_json === "string" ? row.graph_json : "";
                const intentCount = graphRaw.trim() !== "" ? parseGraphDoc(graphRaw).intents.length : null;
                return (
                  <button
                    key={id}
                    type="button"
                    onClick={() => window.location.assign(`/studio/?t=${encodeURIComponent(id)}`)}
                    className="w-full rounded-lg bg-muted/60 p-4 text-left transition hover:bg-accent"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="flex flex-wrap items-center gap-2 font-medium">
                          {String(row.name ?? "-")}
                          <PublishBadge row={row} />
                          <span
                            className={`rounded-sm px-1.5 py-0.5 text-[10px] font-normal ${
                              ownerId === "" ? "bg-muted muted" : "bg-sky-100 text-sky-700"
                            }`}
                          >
                            {ownerLabel}
                          </span>
                        </p>
                        <p className="mt-1 text-xs muted">
                          {langText(langRaw)}
                          {` · ${stepCount} 步`}
                          {intentCount !== null && ` · ${intentCount} 个意图`}
                        </p>
                      </div>
                      <span className="shrink-0 text-xs text-(--live)">打开 →</span>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </section>
      </div>
    );
  }

  // ---- 工作台态渲染 ----
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button className="btn-ghost text-xs" onClick={backToList}>
          ← 返回列表
        </button>
        <h1 className="page-title">{String(tplRow?.name ?? selId)}</h1>
        <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px]">
          {langText(String(tplRow?.language ?? "zh"))}
        </span>
        {/* 全局状态（右上角）：主流程步骤草稿的未保存/应用/发布——唯一保存入口 */}
        <div className="ml-auto flex items-center gap-2">
          {stepsDirty ? (
            <span className="flex items-center gap-1.5 text-xs text-amber-700">
              <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
              未保存
            </span>
          ) : (
            <span className="text-xs text-emerald-600">已保存 ✓</span>
          )}
          {applyNote && !stepsDirty && <span className="text-xs text-emerald-600">{applyNote}</span>}
          {!contentReadOnly && (
            <>
              <button
                className="btn-primary px-3 py-1 text-xs"
                disabled={!stepsDirty || applying || !selId}
                onClick={() => void applySteps()}
              >
                {applying ? "应用中…" : "应用"}
              </button>
              <button
                className="btn-ghost px-2 py-1 text-xs"
                disabled={publishing || !selId}
                onClick={() => void publishNow()}
              >
                {publishing ? "发布中…" : "发布"}
              </button>
            </>
          )}
        </div>
      </div>
      {applyErr && <ErrorState message={applyErr} />}

      {tplErr && <ErrorState message={tplErr} />}
      {/* 首次加载才出转圈;点「应用」后的重拉沿用已在屏内容——整块卸载会把画布抽屉/
          滚动位置一并冲掉（F7：保存后答法抽屉自动收起的根因）。 */}
      {tplLoading && !tplRow && <LoadingState />}

      {tplRow && (
        <>
          <div className="flex items-center gap-1">
            {tabs.map(([k, label]) => (
              <button
                key={k}
                className={`btn-ghost text-xs ${tab === k ? "border-(--live) text-(--live-ink)" : "muted"}`}
                onClick={() => setTab(k)}
              >
                {label}
              </button>
            ))}
          </div>

          {/* 1. 主流程：列表编辑（默认）/ 画布——同一份工作站层草稿,右上角「应用」统一保存 */}
          {tab === "flow" && (
            <div className="space-y-2">
              <div className="flex items-center gap-1">
                {([["form", "列表编辑"], ["canvas", "画布"]] as const).map(([k, label]) => (
                  <button
                    key={k}
                    className={`btn-ghost text-xs ${flowView === k ? "border-(--live) text-(--live-ink)" : "muted"}`}
                    onClick={() => switchFlowView(k)}
                  >
                    {label}
                  </button>
                ))}
                {/* 表格导入（整表替换 steps_json）+ 示例模板下载 */}
                {!contentReadOnly && (
                  <>
                    <button className="btn-ghost text-xs" onClick={() => setFlowImportOpen(true)}>
                      导入话术
                    </button>
                    <button
                      className="btn-ghost text-xs"
                      title="下载示例 CSV（含列说明注释行，导入时自动忽略）"
                      onClick={() =>
                        downloadCsv(FLOW_IMPORT_FILENAME, buildExampleCsvRows(FLOW_IMPORT_COLUMNS, FLOW_IMPORT_EXAMPLE))
                      }
                    >
                      下载示例模板
                    </button>
                  </>
                )}
                {branchNote && <span className="ml-auto text-xs text-amber-700">{branchNote}</span>}
              </div>
              {/* 重派生保险（2026-09-25「列表与画布对不上」排查收尾）：两视图本就同吃页面层
                  stepsDraft、切换视图=条件渲染卸载重挂,天然拿到最新草稿;key 再钉到 模板+步数
                  ——保存后步数变化（空白步被剔除/列表加删步后应用重锚）时画布整树重建,拖动
                  覆盖与抽屉步号不再按旧下标错位。步数不变的同模板保存不换 key,F7（答法抽屉
                  保存后保持打开）不受影响。刻意不用内容哈希——逐字编辑会频繁换 key 反复重挂。 */}
              {flowView === "canvas" ? (
                <FlowCanvas
                  key={`${selId}:${stepsDraft.length}`}
                  tpl={tplRow}
                  graph={graph}
                  draft={stepsDraft}
                  onDraftChange={changeSteps}
                  branchCanned={branchCanned}
                  onPregenBranch={pregenBranch}
                  onOpenIntents={() => setTab("intent")}
                />
              ) : (
                <>
                  <StepsListEditor
                    value={stepsDraft}
                    onChange={changeSteps}
                    readOnly={contentReadOnly}
                    lang={String(tplRow?.language ?? "zh")}
                  />
                  {/* 模板设置（名称/语言/语气/热词）：独立保存,不碰步骤草稿 */}
                  <details className="card">
                    <summary className="cursor-pointer text-sm font-medium">
                      模板设置 <span className="ml-1 text-xs muted">（名称 / 语言 / 语气 / 热词——本块有独立保存按钮）</span>
                    </summary>
                    <div className="mt-3">
                      <TemplateEditor tpl={tplRow} variant="meta" onSaved={() => setTplRev((v) => v + 1)} />
                    </div>
                  </details>
                </>
              )}
            </div>
          )}

          {/* 2. 意图管理（PRD 3.3）：第一层识别——表格+弹窗直编 graph_json；种子包一键导入 */}
          {tab === "intent" && tplRow && (
            <IntentManager
              tpl={tplRow}
              accountId={accountId}
              readOnly={contentReadOnly}
              onSaved={() => setTplRev((v) => v + 1)}
              seedIntents={seedPackFor(String(tplRow.language ?? "zh")).intents}
            />
          )}

          {/* 2b. 问答库（PRD 3.4）：表格+弹窗；多轮行为（播完跳转/通知人工）在意图管理挂 play_qa 绑定 */}
          {tab === "qa" && tplRow && (
            <QaLibrary
              accountId={accountId}
              templateId={selId}
              lang={String(tplRow.language ?? "zh")}
              stepCount={Math.max(stepsList.length, 1)}
              readOnly={contentReadOnly}
              seedPack={seedPackFor(String(tplRow.language ?? "zh")).qa}
            />
          )}

          {/* 3. 客户意向（挂断判定 intent_rules）：与 /calls 页同一张卡（账号级规则面） */}
          {tab === "disposition" && (
            <section className="space-y-2">
              <p className="text-xs muted">
                挂断时按通话事实（时长/轮数/到达步数/是否捕获号码等）判定客户意向码与处置，命中即写进通话记录。
              </p>
              <IntentRulesCard accountId={accountId} />
            </section>
          )}

          {/* 4. 录音沉淀：罐头音资产试听（垫话/QA 罐头） + 挂步骤词条的物化状态面 */}
          {tab === "canned" && (
            <section className="space-y-3">
              <CannedAuditionCard />
              <div className="card space-y-3">
              {cannedErr && <ErrorState message={cannedErr} />}
              {cannedLoading ? (
                <LoadingState />
              ) : stepQaRows.length === 0 ? (
                <EmptyState label="该模板暂无挂步骤的问答条目。" />
              ) : (
                <div className="space-y-2">
                  {stepQaRows.map((r) => {
                    const qid = String(r.id ?? "");
                    const st = canned[qid]?.state;
                    const idx = Number(r.step_index ?? -1);
                    return (
                      <div key={qid} className="flex items-center justify-between gap-3 rounded-lg bg-muted/60 p-3">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium">{String(r.question_text ?? "(无问法)")}</p>
                          <p className="mt-0.5 text-xs muted">
                            第 {idx >= 0 ? idx + 1 : 1} 步 · {langText(String(r.lang ?? "zh"))}
                          </p>
                        </div>
                        {st === "ok" && (
                          <span className="shrink-0 rounded-sm bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-700">录音✓</span>
                        )}
                        {st === "missing" && (
                          <span className="shrink-0 rounded-sm bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700">缺录音</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
              <div className="rounded-lg border border-dashed border-(--card-border) p-3 text-xs muted">
                补录 / 重新录音请前往
                <Link href="/qa/" className="text-(--live)">问答画布</Link>
                （画布视图可右键条目重新录音）。
                分支录音（话术步的「如果客户…→就…」应对）在主流程画布的答法抽屉里查看/补录。
              </div>
              </div>
            </section>
          )}

          {/* 4. 通话日志：按模板过滤的最新通话（最多 50 条） */}
          {tab === "calls" && (
            <section className="card space-y-2">
              {callsErr && <ErrorState message={callsErr} />}
              {callsLoading ? (
                <LoadingState />
              ) : tplCalls.length === 0 ? (
                <EmptyState label="该模板还没有通话记录。" />
              ) : (
                <>
                  <p className="px-2 text-xs muted">
                    共 {tplCalls.length} 通 · 显示最近 {visibleCalls.length} 通
                  </p>
                  <div className="space-y-2">
                    {visibleCalls.map((c) => {
                      const cid = String(c.id ?? c.call_id ?? "");
                      const status = String(c.status ?? "idle");
                      const [label, color] = CALL_STATUS[status] ?? [status, "bg-neutral-500"];
                      const wa = String(c.whatsapp_status ?? "");
                      const waNum = String(c.customer_whatsapp ?? "");
                      const langRaw = String(c.language ?? "");
                      return (
                        <button
                          key={cid}
                          type="button"
                          onClick={() => window.location.assign(`/calls/?call=${encodeURIComponent(cid)}`)}
                          className="w-full rounded-lg bg-muted/60 px-3 py-2 text-left transition hover:bg-accent"
                        >
                          <div className="flex items-center justify-between gap-3">
                            <div className="min-w-0">
                              <p className="flex flex-wrap items-center gap-2 truncate text-sm font-medium">
                                <span className={`h-2 w-2 shrink-0 rounded-full ${color}`} />
                                {label}
                                <span className="muted">{LANG_LABEL[langRaw] ?? (langRaw || "-")}</span>
                                <span className="muted">处置 {String(c.disposition ?? "") || "-"}</span>
                                {WA_PENDING.includes(wa) && (
                                  <span className="rounded-sm bg-(--live-soft) px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-(--live-ink)">
                                    WhatsApp {wa === "captured" && waNum ? waNum : "待对接"}
                                  </span>
                                )}
                              </p>
                              <p className="mt-0.5 truncate text-xs muted">
                                {cid} · {String(c.created_at ?? "").slice(0, 19).replace("T", " ")}
                              </p>
                            </div>
                            <span className="shrink-0 text-xs text-(--live)">查看 →</span>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                </>
              )}
            </section>
          )}

          {/* 5. 变量：占位符目录 + 无效占位告警 + 对象预览（渲染语义 lib/var-panel.ts） */}
          {tab === "vars" && <TemplateVarsTab tpl={tplRow} accountId={accountId} />}

          {/* 6. 学习报告（reports 键可见）：话术优化分析 + 高频问答对 + AI 聚类采纳 + 快路覆盖率/漏网轮采集 */}
          {tab === "reports" && canReports && (
            <>
              <StudyTab />
              <GapMining templateId={selId} />
            </>
          )}
        </>
      )}

      {/* 主流程表格导入（2026-09-25）：CSV 上传→预览（N 行将导入/M 行跳过及原因）→
          确认（整表替换 steps_json）→保存→摘要 */}
      {Boolean(selId) && (
        <TableImport<FlowImportRow>
          open={flowImportOpen}
          title="导入话术（表格）"
          description="整表替换：导入将覆盖当前主流程的全部步骤（按步号排序），保存后立即生效为草稿。"
          columns={FLOW_IMPORT_COLUMNS}
          exampleRows={FLOW_IMPORT_EXAMPLE}
          exampleFilename={FLOW_IMPORT_FILENAME}
          parseRow={parseFlowImportRow}
          onImport={importFlowRows}
          confirmText={(n) => {
            const lines = [
              `导入将【整表替换】当前主流程的全部步骤：现有 ${stepsList.length} 步 → 表格的 ${n} 步。`,
            ];
            if (stepsDirty) lines.push("注意：还有未应用的修改，将被这次导入覆盖。");
            lines.push("替换后立即保存，无需再点「应用」；已发布版本不受影响。确定继续？");
            return lines.join("\n");
          }}
          onClose={() => setFlowImportOpen(false)}
        />
      )}
    </div>
  );
}
