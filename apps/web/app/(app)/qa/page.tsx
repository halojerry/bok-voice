"use client";

// 快答库（B4）：Q→A 检索快路的运营配置面。
// 话务员（user）=「我的 / 共享」两 tab，只能改自己的；主管（admin/root/本地匿名）=全部列表
// + 归属列与归属转移。命中判定按字面措辞，所以条目的问题文本要按客户实际说法写。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { api, authHeaders, type UserRow } from "@/lib/api";
import { resolveClusterTarget, revertCluster } from "@/lib/qa-canvas";
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
};

const EMPTY_FORM: QaForm = {
  question_text: "",
  answer_text: "",
  lang: "zh",
  scope: "global",
  step: 1,
  voice_id: "",
  enabled: true,
};

/** step_index → 界面步骤号（-1/未设 = 第 1 步）。 */
function displayStep(row: QaRow): number {
  const idx = Number(row.step_index ?? -1);
  return idx >= 0 ? idx + 1 : 1;
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
  const [templates, setTemplates] = useState<TemplateRow[]>([]);
  // 模板下拉只作画布步骤脊柱的派生输入(spec §4.2);已绑模板的显示/写入属 Phase 2,此处不做。
  const [templateId, setTemplateId] = useState("");
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
    const payload: Record<string, unknown> = {
      question_text: question,
      answer_text: answer,
      lang: form.lang,
      scope: form.scope,
      step_index: form.scope === "step" ? step - 1 : -1,
      voice_id: form.voice_id.trim(),
      enabled: form.enabled,
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
   *  脊柱顺序线(spine)是展示性元素,不可解除。 */
  async function disconnect(edge: { source: string; target: string; data?: { kind?: string } }) {
    const fromId = String(edge.source ?? "");
    if (!fromId) return;
    if (edge.data?.kind === "spine") return;
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
                  className={`btn-ghost text-xs ${tab === key ? "border-(--live) text-(--live)" : "muted"}`}
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
                className={`btn-ghost text-xs ${view === k ? "border-(--live) text-(--live)" : "muted"}`}
                onClick={() => setView(k)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {err && <ErrorState message={err} />}

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
            <button
              className={menuItemCls}
              onClick={() => { const hit = edgeMenu.edge; setEdgeMenu(null); void disconnect(hit); }}
            >
              {edgeMenu.edge.data?.kind === "cluster" ? "解除变体归属" : "解除步骤挂载"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
