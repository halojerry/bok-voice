"use client";

// AI 工作站（W1；2026-09-20 重设计）：以话术模板为入口的集中工作台。
// 列表态（无 ?t=）：全部模板概览 + 新建话术（话术/快答内容全部整合在此，
// /templates、/qa 移出主导航后这里成为唯一内容入口）；
// 工作台态（?t=<id>）：话术流程 + 意图管理 + 变量 + 客户意向 + 录音沉淀 +
// 通话日志 + 学习报告 tab（对齐参考产品的主流程/意图管理/变量设定/客户意向/录音管理 tab 面）。
// （学习报告/聚类采纳在 components/study-tab.tsx,变量目录与预览在 components/template-vars.tsx）。
// 深链先例：/calls?call= / /supervisor?listen= —— 静态导出用 query，不开动态路由。

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, type UserRow } from "@/lib/api";
import { parseGraphDoc, parseTemplateSteps } from "@/lib/qa-canvas";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { hasPage, useSession } from "@/components/session-context";
import TemplateEditor, {
  LANGS,
  PublishBadge,
  toTemplateRow,
  type TemplateRow,
} from "@/components/template-editor";
import FlowCanvas from "@/components/flow-canvas";
import StudyTab from "@/components/study-tab";
import TemplateVarsTab from "@/components/template-vars";
import CannedAuditionCard from "@/components/canned-audition";
import IntentRulesCard from "@/components/intent-rules-card";

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

  // ---- tab 切换（qa 页 view chips 同款写法）；学习报告 tab 仅对有 reports 键的人出现 ----
  const [tab, setTab] = useState("flow");
  // 话术流程 tab 双视图（W2 流程画布）：表单=共用编辑器,画布=步骤工作流+答法抽屉。
  const [flowView, setFlowView] = useState<"form" | "canvas">("form");
  const tabs: [string, string][] = [
    ["flow", "话术流程"],
    ["intent", "意图管理"],
    ["vars", "变量"],
    ["disposition", "客户意向"],
    ["canned", "录音沉淀"],
    ["calls", "通话日志"],
  ];
  if (canReports) tabs.push(["reports", "学习报告"]);

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
        <button className="btn-ghost text-xs" onClick={() => window.location.assign("/studio/")}>
          ← 返回列表
        </button>
        <h1 className="page-title">{String(tplRow?.name ?? selId)}</h1>
        <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px]">
          {langText(String(tplRow?.language ?? "zh"))}
        </span>
      </div>

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

          {/* 1. 话术流程：表单=共用编辑器 / 画布=场景泳道+答法抽屉（保存成功重拉模板行,各 tab 随之取到权威数据） */}
          {tab === "flow" && (
            <div className="space-y-2">
              <div className="flex items-center gap-1">
                {([["form", "表单"], ["canvas", "画布"]] as const).map(([k, label]) => (
                  <button
                    key={k}
                    className={`btn-ghost text-xs ${flowView === k ? "border-(--live) text-(--live-ink)" : "muted"}`}
                    onClick={() => setFlowView(k)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {flowView === "form" ? (
                <TemplateEditor tpl={tplRow} onSaved={() => setTplRev((v) => v + 1)} />
              ) : (
                <FlowCanvas tpl={tplRow} graph={graph} onSaved={() => setTplRev((v) => v + 1)} />
              )}
            </div>
          )}

          {/* 2. 意图管理（第一层识别：关键词/judge 判据/绑定动作；编辑进问答画布） */}
          {tab === "intent" && (
            <section className="card space-y-4">
              {graph.intents.length === 0 ? (
                <EmptyState label="该模板未配置话术图意图。" />
              ) : (
                <div className="space-y-3">
                  {graph.intents.map((it) => {
                    const binds = graph.bindings.filter((b) => b.intent === it.id);
                    return (
                      <div key={it.id} className="rounded-lg bg-muted/60 p-3">
                        <p className="flex flex-wrap items-center gap-2 font-medium">
                          {String(it.label || "(未命名意图)")}
                          {it.enabled === false && (
                            <span className="rounded-sm bg-muted px-1 text-[10px]">已停用</span>
                          )}
                          {it.judge?.prompt && (
                            <span
                              className="rounded-sm bg-amber-100 px-1 text-[10px] text-amber-700"
                              title={it.judge.prompt}
                            >
                              LLM 判据
                            </span>
                          )}
                        </p>
                        <p className="mt-1 text-xs muted">关键词：{it.keywords.join(", ") || "-"}</p>
                        <p className="mt-0.5 text-xs muted">
                          生效范围：{it.steps.length === 0 ? "全程" : `第 ${it.steps.join("、")} 步`}
                        </p>
                        {binds.length > 0 ? (
                          <div className="mt-2 space-y-1">
                            {binds.map((b) => (
                              <p key={b.id} className="text-xs muted">
                                {b.action === "play_qa"
                                  ? `播快答 ${String(b.qa_id ?? "-")}`
                                  : b.action === "notify_human"
                                    ? "通知人工"
                                    : `跳到第 ${Number(b.step ?? 1)} 步`}
                                {b.action === "play_qa" && Number(b.then_jump ?? 0) > 0 && ` · 播完跳第 ${Number(b.then_jump)} 步`}
                                {` · P${Number(b.priority ?? 10)}`}
                                {b.once && " · 只执行一次"}
                                {b.enabled === false && " · 已停用"}
                              </p>
                            ))}
                          </div>
                        ) : (
                          <p className="mt-2 text-xs muted">无绑定动作</p>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
              <div className="rounded-lg border border-(--card-border) p-3">
                <span className="label">步骤脊柱（{stepsList.length} 步）</span>
                {stepsList.length === 0 ? (
                  <p className="mt-1 text-xs muted">尚无步骤。</p>
                ) : (
                  <div className="mt-1 space-y-1">
                    {stepsList.map((s, i) => (
                      <p key={i} className="text-xs muted">
                        <span className="font-bold text-(--live-ink)">第 {i + 1} 步</span> · {String(s.goal || "(无目标)")}
                      </p>
                    ))}
                  </div>
                )}
              </div>
              <button
                className="btn-primary"
                onClick={() => window.location.assign(`/qa/?template=${encodeURIComponent(selId)}&view=canvas`)}
              >
                打开意图画布编辑（关键词 / 判据 / 绑定）
              </button>
            </section>
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
