"use client";

// 话术库页（W1 AI 工作站）：列表/归属过滤/删除留在本页；编辑表单（含保存逻辑与
// 纯函数助手）已原样提取到 components/template-editor.tsx 供 /studio 工作台共用。
// 提取前后渲染输出逐字一致（字段、默认值、占位文案不动）。

import { useCallback, useEffect, useState } from "react";
import { api, type UserRow } from "@/lib/api";
import { useTemplatesList } from "@/lib/swr";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { useAccount } from "@/components/account-context";
import { useSession } from "@/components/session-context";
import TemplateEditor, {
  FIELD_LABELS, LANGS, TEMPLATE_FIELDS, jsonToSteps, toTemplateRow,
  type TemplateRow,
} from "@/components/template-editor";

export default function TemplatesPage() {
  const { accountId } = useAccount();
  // 数据层（2026-10-02）：模板列表走 SWR 共享缓存（key=["templates",accountId]）——
  // 与 /studio 列表同缓存，跨页导航秒开不重拉；保存/删除后 mutate 重验。
  const { data: tplData, isLoading: loading, error: tplErr, mutate: mutateTemplates } =
    useTemplatesList(accountId);
  const rows = tplData ?? [];
  const fetchErr = tplErr ? String(tplErr) : null;
  const [editingRow, setEditingRow] = useState<Record<string, unknown> | null>(null);
  // 编辑器未保存状态（onDirtyChange 上抛）：切行/取消编辑前做丢失确认。
  const [editorDirty, setEditorDirty] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // B4：归属徽标与编辑权——话务员（user）只能改自己的条目，共享/他人只读（服务端 403 兜底）。
  const session = useSession();
  const [userNames, setUserNames] = useState<Record<string, string>>({});
  const [scopeTab, setScopeTab] = useState<"all" | "mine" | "shared">("all");
  const isManager = Boolean(
    session && (session.anonymous || session.role === "admin" || session.role === "root"),
  );

  const refresh = useCallback(async () => {
    const next = await mutateTemplates().catch(() => undefined);
    // 编辑行快照回填（2026-10-02 交互逻辑）：重拉后用最新行数据替换选中时的陈旧快照——
    // 编辑器只跟 tplId 重锚（同 id 换对象不覆写正在编辑的表单），但保存 payload 应以
    // 最新行为准（修复「保存后仍拿旧名称/旧字段覆盖」的陈旧快照源头）。
    setEditingRow((prev) => {
      if (!prev) return prev;
      const id = String(prev.id ?? "");
      const latest = (next ?? []).find((r) => String(r.id ?? "") === id);
      return latest ?? prev;
    });
  }, [mutateTemplates]);

  /** 丢弃未保存修改的确认（切行/取消编辑共用）。返回 true=可以切走。 */
  const confirmDiscardEdits = useCallback(() => {
    if (!editorDirty) return true;
    return window.confirm("当前编辑未保存，切换会丢失。仍要切换？");
  }, [editorDirty]);

  // 主管面：拉成员表把归属 user_id 显示成姓名（话务员无权访问 /api/users，不请求）。
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
        if (!alive) return;
        const map: Record<string, string> = {};
        list.forEach((u) => {
          map[u.id] = u.display_name || u.username || u.id;
        });
        setUserNames(map);
      } catch {
        if (alive) setUserNames({}); // 拉不到成员名时退化为「个人」徽标
      }
    })();
    return () => {
      alive = false;
    };
  }, [isManager]);

  async function remove(id: string) {
    if (!window.confirm("确认删除该话术模板？")) return;
    try {
      await api.deleteTemplate(id);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  if (!session) return <LoadingState label="正在读取会话…" />;

  const ownerIdOf = (row: Record<string, unknown>) => String(row.owner_user_id ?? "");
  const mineCount = rows.filter((r) => session.user_id !== "" && ownerIdOf(r) === session.user_id).length;
  const sharedCount = rows.filter((r) => ownerIdOf(r) === "").length;
  // 话务员视图是服务端已过滤的「共享+本人」，这里只做客户端分组。
  const visibleRows =
    isManager || scopeTab === "all"
      ? rows
      : rows.filter((r) => (scopeTab === "mine" ? ownerIdOf(r) === session.user_id : ownerIdOf(r) === ""));

  // 编辑器的模板行收窄（防御式，见 toTemplateRow）。
  const editingTpl: TemplateRow | null = editingRow ? toTemplateRow(editingRow) : null;

  return (
    <div>
      <div className="mb-8 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="page-title">话术库</h1>
          <p className="page-sub">可复用的对话模板 · 开场白 / 核心话术 / 异议应对 / 收尾，对象卡可绑定</p>
        </div>
        {!isManager && (
          <div className="flex items-center gap-1">
            {([
              ["all", `全部（${rows.length}）`],
              ["mine", `我的（${mineCount}）`],
              ["shared", `共享（${sharedCount}）`],
            ] as const).map(([key, label]) => (
              <button
                key={key}
                className={`btn-ghost text-xs ${scopeTab === key ? "border-(--live) text-(--live-ink)" : "muted"}`}
                onClick={() => setScopeTab(key)}
              >
                {label}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_440px]">
        <section className="card">
          {(err || fetchErr) && <ErrorState message={err || fetchErr} />}
          {loading ? (
            <LoadingState />
          ) : rows.length === 0 ? (
            <EmptyState label="暂无话术模板，请在右侧新建。" />
          ) : visibleRows.length === 0 ? (
            <EmptyState label="该分组下暂无话术模板。" />
          ) : (
            <div className="space-y-3">
              {visibleRows.map((row) => {
                const id = String(row.id ?? "");
                const ownerId = ownerIdOf(row);
                const isMine = session.user_id !== "" && ownerId === session.user_id;
                const canEdit = isManager || isMine;
                const ownerLabel =
                  ownerId === ""
                    ? "共享"
                    : isManager
                      ? userNames[ownerId] || "个人"
                      : isMine
                        ? "我的"
                        : "他人";
                return (
                  <div key={id} className="rounded-lg bg-muted/60 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="flex flex-wrap items-center gap-2 font-medium">
                          {String(row.name ?? "-")}
                          <span
                            className={`rounded-sm px-1.5 py-0.5 text-[10px] font-normal ${
                              ownerId === "" ? "bg-muted muted" : "bg-sky-100 text-sky-700"
                            }`}
                          >
                            {ownerLabel}
                          </span>
                        </p>
                        <p className="mt-1 text-xs muted">
                          {LANGS.find((l) => l[0] === String(row.language ?? "zh"))?.[1] ?? String(row.language ?? "zh")}
                          {String(row.tone_override ?? "") && ` · 语气 ${String(row.tone_override)}`}
                          {String(row.hotwords ?? "") && ` · 热词 ${String(row.hotwords)}`}
                        </p>
                      </div>
                      <div
                        className="flex shrink-0 gap-2"
                        title={canEdit ? undefined : "共享话术由主管维护"}
                      >
                        <button
                          className="btn-ghost text-xs"
                          disabled={!canEdit}
                          title={canEdit ? undefined : "共享话术由主管维护"}
                          onClick={() => {
                            const nextId = String(row.id ?? "");
                            // 换目标行且编辑器有未保存修改 → 先确认（同 id 点开不重锚表单，无需拦）。
                            if (nextId !== String(editingTpl?.id ?? "") && !confirmDiscardEdits()) return;
                            setEditingRow(row);
                          }}
                        >
                          编辑
                        </button>
                        <button
                          className="btn-ghost text-xs text-red-600"
                          disabled={!canEdit}
                          title={canEdit ? undefined : "共享话术由主管维护"}
                          onClick={() => remove(id)}
                        >
                          删除
                        </button>
                      </div>
                    </div>
                    <div className="mt-2 text-xs">
                      {(() => {
                        const s = jsonToSteps(row.steps_json);
                        if (s.length > 0) {
                          return (
                            <div className="space-y-1">
                              {s.map((st, i) => (
                                <p key={i} className="muted">
                                  <span className="font-bold text-(--live-ink)">{i + 1}.</span>{" "}
                                  {st.goal || "(无目标)"}
                                </p>
                              ))}
                            </div>
                          );
                        }
                        return (
                          <div className="grid grid-cols-2 gap-2">
                            {TEMPLATE_FIELDS.map((k) => (
                              <div key={k}>
                                <span className="label">{FIELD_LABELS[k]}</span>
                                <p className="mt-0.5 line-clamp-3 muted">{String(row[k] ?? "")}</p>
                              </div>
                            ))}
                          </div>
                        );
                      })()}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        {/* 编辑表单（原内联面板整体提取；保存成功=退回新建态+刷新列表，
            话务员新建归属本人 → 切回「全部」保证刚保存的条目可见）。 */}
        <TemplateEditor
          tpl={editingTpl}
          onDirtyChange={setEditorDirty}
          onSaved={() => {
            setEditingRow(null);
            if (!isManager) setScopeTab("all");
            void refresh();
          }}
          onCancel={() => {
            // 取消编辑=丢弃草稿：有未保存修改时先确认（取消则留在编辑态）。
            if (!confirmDiscardEdits()) return;
            setEditingRow(null);
          }}
        />
      </div>
    </div>
  );
}
