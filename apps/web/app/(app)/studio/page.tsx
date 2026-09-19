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
import { parseGraphDoc, parseTemplateSteps } from "@/lib/qa-canvas";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { hasPage, useSession } from "@/components/session-context";
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
  // 主流程 tab 双视图：画布=推荐默认（普通人视角：看到流程再点步骤）；列表=批量编辑。
  const [flowView, setFlowView] = useState<"form" | "canvas">("canvas");
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
      {tplLoading && <LoadingState />}

      {!tplLoading && tplRow && (
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

          {/* 1. 主流程：画布（默认）/ 列表编辑——同一份工作站层草稿,右上角「应用」统一保存 */}
          {tab === "flow" && (
            <div className="space-y-2">
              <div className="flex items-center gap-1">
                {([["canvas", "画布（推荐）"], ["form", "列表编辑"]] as const).map(([k, label]) => (
                  <button
                    key={k}
                    className={`btn-ghost text-xs ${flowView === k ? "border-(--live) text-(--live-ink)" : "muted"}`}
                    onClick={() => setFlowView(k)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {flowView === "canvas" ? (
                <FlowCanvas tpl={tplRow} graph={graph} draft={stepsDraft} onDraftChange={changeSteps} />
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

          {/* 6. 学习报告（reports 键可见）：话术优化分析 + 高频问答对 + AI 聚类采纳 */}
          {tab === "reports" && canReports && <StudyTab />}
        </>
      )}
    </div>
  );
}
