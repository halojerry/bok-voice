"use client";

// 员工管理（B4）：主管专属面——建号 / 启停 / 重置密码 / 8 键页面权限勾选。
// 权限目录与后端 permissions.py 逐字对齐（见 components/session-context.tsx PAGE_KEYS）。
// root 账号只读展示（仅 root 可管理 root/admin），本页不提供跨主管操作。

import { Fragment, useCallback, useEffect, useState } from "react";
import { api, type UserRow } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { PAGE_KEYS, PAGE_LABELS, useSession, type PageKey } from "@/components/session-context";

/** 新话务员默认页面集：目录 8 键去掉「报表」（报表默认关，主管可按需开）。 */
const DEFAULT_PERMISSIONS: string[] = PAGE_KEYS.filter((k) => k !== "reports");

const ROLE_LABEL: Record<string, string> = { root: "超级管理员", admin: "管理员", user: "话务员" };
const ROLE_BADGE: Record<string, string> = {
  root: "bg-amber-400/15 text-amber-300",
  admin: "bg-sky-400/15 text-sky-300",
  user: "bg-white/10 muted",
};

type CreateForm = {
  username: string;
  display_name: string;
  password: string;
  role: string;
  permissions: string[];
};

const EMPTY_CREATE: CreateForm = {
  username: "",
  display_name: "",
  password: "",
  role: "user",
  permissions: [...DEFAULT_PERMISSIONS],
};

/** 权限勾选组：8 键目录，标签取 PAGE_LABELS。 */
function PermissionChecks({
  value,
  onChange,
  disabled = false,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1.5">
      {PAGE_KEYS.map((k) => (
        <label key={k} className={`flex items-center gap-1.5 text-xs ${disabled ? "muted" : ""}`}>
          <input
            type="checkbox"
            className="size-3 accent-(--accent)"
            checked={value.includes(k)}
            disabled={disabled}
            onChange={(e) => onChange(e.target.checked ? [...value, k] : value.filter((x) => x !== k))}
          />
          {PAGE_LABELS[k]}
        </label>
      ))}
    </div>
  );
}

/** 行权限（服务端返回有效集；缺省/'' 语义=默认集，[]=全关；admin/root=全部 grantable 键）。 */
function rowPermissions(u: UserRow): string[] {
  if (Array.isArray(u.permissions)) return u.permissions;
  if (u.role === "admin" || u.role === "root") return [...PAGE_KEYS];
  return DEFAULT_PERMISSIONS;
}

export default function UsersPage() {
  const session = useSession();
  const [rows, setRows] = useState<UserRow[] | null>(null);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [cf, setCf] = useState<CreateForm>(EMPTY_CREATE);
  const [createBusy, setCreateBusy] = useState(false);
  const [createErr, setCreateErr] = useState("");
  const [permEditId, setPermEditId] = useState("");
  const [permDraft, setPermDraft] = useState<string[]>([]);
  const [pwEditId, setPwEditId] = useState("");
  const [pwDraft, setPwDraft] = useState("");
  const [pwErr, setPwErr] = useState("");

  /** 主管面：匿名本地会话（auth-off 单机形态）与 admin/root 可用。 */
  const isManager = Boolean(
    session && (session.anonymous || session.role === "admin" || session.role === "root"),
  );

  const refresh = useCallback(async () => {
    try {
      const raw = (await api.listUsers()) as unknown;
      // 兼容裸数组与 {users:[...]} 包裹两种响应形态。
      const list = Array.isArray(raw)
        ? (raw as UserRow[])
        : Array.isArray((raw as { users?: UserRow[] })?.users)
          ? (raw as { users: UserRow[] }).users
          : [];
      setRows(list);
      setErr("");
    } catch (e) {
      setErr(String(e));
      setRows((prev) => prev ?? []);
    }
  }, []);

  useEffect(() => {
    if (!isManager) return;
    void refresh();
  }, [isManager, refresh]);

  async function create() {
    const username = cf.username.trim();
    if (!username) {
      setCreateErr("请填写用户名。");
      return;
    }
    if (cf.password.length < 8) {
      setCreateErr("初始密码至少 8 位。");
      return;
    }
    setCreateBusy(true);
    setCreateErr("");
    try {
      const body: {
        username: string;
        password: string;
        role?: string;
        display_name?: string;
        permissions?: string[];
      } = {
        username,
        password: cf.password,
        role: cf.role,
        display_name: cf.display_name.trim(),
      };
      // permissions 仅对 role=user 目标有效（对 admin 目标传会被后端 400）。
      if (cf.role === "user") body.permissions = cf.permissions;
      await api.createUser(body);
      setCf(EMPTY_CREATE);
      setCreateOpen(false);
      setNotice(`已创建「${username}」。`);
      await refresh();
    } catch (e) {
      setCreateErr(String(e));
    } finally {
      setCreateBusy(false);
    }
  }

  async function toggleStatus(u: UserRow) {
    const next = u.status === "disabled" ? "active" : "disabled";
    if (next === "disabled" && !window.confirm(`确认停用「${u.username}」？停用后该成员无法登录。`)) return;
    setBusy(u.id);
    setErr("");
    setNotice("");
    try {
      await api.updateUser(u.id, { status: next });
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  async function savePassword(u: UserRow) {
    if (pwDraft.length < 8) {
      setPwErr("新密码至少 8 位。");
      return;
    }
    if (!window.confirm(`确认重置「${u.username}」的登录密码？旧密码立即失效。`)) return;
    setBusy(u.id);
    setErr("");
    setNotice("");
    try {
      await api.updateUser(u.id, { password: pwDraft });
      setPwEditId("");
      setPwDraft("");
      setPwErr("");
      setNotice(`已重置「${u.username}」的密码。`);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  async function savePermissions(u: UserRow) {
    if (!window.confirm(`确认保存「${u.username}」的页面权限？保存后立即生效。`)) return;
    setBusy(u.id);
    setErr("");
    setNotice("");
    try {
      await api.updateUser(u.id, { permissions: permDraft });
      setPermEditId("");
      setNotice(`已更新「${u.username}」的页面权限。`);
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy("");
    }
  }

  if (!session) return <LoadingState label="正在读取会话…" />;
  if (!isManager) {
    return (
      <div className="card space-y-2">
        <span className="label">无权限</span>
        <p className="text-sm">员工管理仅主管（管理员 / 超级管理员）可用，请使用主管账号登录。</p>
        <p className="text-xs muted">话务员如需调整账号信息，请联系主管。</p>
      </div>
    );
  }

  const input = "w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--accent)";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="page-title">员工管理</h1>
          <p className="page-sub">账号 · 角色 · 启停 · 页面权限（话务员可用的页面范围）</p>
        </div>
        {!createOpen && (
          <button
            className="btn-primary"
            onClick={() => {
              setCreateOpen(true);
              setCreateErr("");
              setNotice("");
            }}
          >
            + 新建员工
          </button>
        )}
      </div>

      {err && <ErrorState message={err} />}
      {notice && <p className="text-sm text-emerald-400">{notice}</p>}

      {createOpen && (
        <section className="card space-y-3">
          <div className="flex items-center justify-between">
            <span className="label">新建员工</span>
            <button
              className="btn-ghost px-2 py-0.5 text-xs"
              onClick={() => {
                setCreateOpen(false);
                setCf(EMPTY_CREATE);
                setCreateErr("");
              }}
            >
              收起
            </button>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block">
              <span className="text-xs text-(--stage-muted)">用户名（登录名）</span>
              <input
                className={`mt-1 ${input}`}
                value={cf.username}
                onChange={(e) => setCf({ ...cf, username: e.target.value })}
                placeholder="如：xiaowang"
              />
            </label>
            <label className="block">
              <span className="text-xs text-(--stage-muted)">姓名</span>
              <input
                className={`mt-1 ${input}`}
                value={cf.display_name}
                onChange={(e) => setCf({ ...cf, display_name: e.target.value })}
                placeholder="如：小王"
              />
            </label>
            <label className="block">
              <span className="text-xs text-(--stage-muted)">初始密码</span>
              <input
                type="password"
                className={`mt-1 ${input}`}
                value={cf.password}
                onChange={(e) => setCf({ ...cf, password: e.target.value })}
                placeholder="至少 8 位"
              />
            </label>
            <label className="block">
              <span className="text-xs text-(--stage-muted)">角色</span>
              <select
                className={`mt-1 ${input}`}
                value={cf.role}
                onChange={(e) => setCf({ ...cf, role: e.target.value })}
              >
                <option value="user">话务员</option>
                <option value="admin">管理员</option>
              </select>
            </label>
          </div>
          <div>
            <span className="label">页面权限</span>
            <div className="mt-1.5">
              <PermissionChecks
                value={cf.permissions}
                onChange={(next) => setCf({ ...cf, permissions: next })}
                disabled={cf.role !== "user"}
              />
            </div>
            <p className="mt-1 text-[11px] leading-relaxed muted">
              {cf.role === "user"
                ? "勾选的页面才会出现在该话务员的导航里；报表默认关闭，按需勾选。"
                : "管理员默认拥有全部页面权限，无需勾选。"}
            </p>
          </div>
          {createErr && <p className="text-xs text-red-300">{createErr}</p>}
          <div className="flex items-center gap-2">
            <button className="btn-primary text-xs" disabled={createBusy} onClick={() => void create()}>
              {createBusy ? "创建中…" : "创建员工"}
            </button>
          </div>
        </section>
      )}

      <section className="card">
        {rows === null ? (
          <LoadingState />
        ) : rows.length === 0 ? (
          <EmptyState label="暂无成员账号，点右上角「新建员工」开始。" />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left muted">
                <th className="py-1">用户名</th>
                <th>姓名</th>
                <th>角色</th>
                <th>状态</th>
                <th>页面权限</th>
                <th>创建时间</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((u) => {
                const perms = rowPermissions(u);
                const rowBusy = busy === u.id;
                const isUser = u.role === "user";
                return (
                  <Fragment key={u.id}>
                    <tr className="border-t border-(--card-border) align-top">
                      <td className="py-1.5 font-mono text-xs">{u.username}</td>
                      <td>{u.display_name || "—"}</td>
                      <td>
                        <span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${ROLE_BADGE[u.role] ?? "bg-white/10 muted"}`}>
                          {ROLE_LABEL[u.role] ?? u.role}
                        </span>
                      </td>
                      <td>
                        <span className={u.status === "disabled" ? "text-xs text-amber-300/80" : "text-xs muted"}>
                          {u.status === "disabled" ? "停用" : "正常"}
                        </span>
                      </td>
                      <td className="max-w-72">
                        <div className="flex flex-wrap gap-1">
                          {perms.length === 0 ? (
                            <span className="text-xs muted">无</span>
                          ) : (
                            perms.map((k) => (
                              <span key={k} className="rounded-sm bg-white/10 px-1.5 py-0.5 text-[10px] muted">
                                {PAGE_LABELS[k as PageKey] ?? k}
                              </span>
                            ))
                          )}
                        </div>
                      </td>
                      <td className="whitespace-nowrap text-xs muted">
                        {String(u.created_at ?? "").slice(0, 10) || "—"}
                      </td>
                      <td className="whitespace-nowrap">
                        {isUser ? (
                          <div className="flex flex-wrap items-center gap-2">
                            <button className="btn-ghost text-xs" disabled={rowBusy} onClick={() => void toggleStatus(u)}>
                              {u.status === "disabled" ? "启用" : "停用"}
                            </button>
                            <button
                              className="btn-ghost text-xs"
                              onClick={() => {
                                setPermEditId(permEditId === u.id ? "" : u.id);
                                setPermDraft([...perms]);
                                setPwEditId("");
                                setNotice("");
                              }}
                            >
                              {permEditId === u.id ? "收起权限" : "编辑权限"}
                            </button>
                            <button
                              className="btn-ghost text-xs"
                              onClick={() => {
                                setPwEditId(pwEditId === u.id ? "" : u.id);
                                setPwDraft("");
                                setPwErr("");
                                setPermEditId("");
                                setNotice("");
                              }}
                            >
                              {pwEditId === u.id ? "收起密码" : "重置密码"}
                            </button>
                          </div>
                        ) : (
                          <span className="text-xs muted">仅 root 可管理</span>
                        )}
                      </td>
                    </tr>
                    {permEditId === u.id && (
                      <tr className="border-t border-(--card-border) bg-white/5">
                        <td colSpan={7} className="py-2">
                          <span className="label">页面权限 · {u.username}</span>
                          <div className="mt-1.5">
                            <PermissionChecks value={permDraft} onChange={setPermDraft} />
                          </div>
                          <div className="mt-2 flex items-center gap-2">
                            <button className="btn-primary text-xs" disabled={rowBusy} onClick={() => void savePermissions(u)}>
                              保存权限
                            </button>
                            <button className="btn-ghost text-xs" onClick={() => setPermEditId("")}>取消</button>
                          </div>
                        </td>
                      </tr>
                    )}
                    {pwEditId === u.id && (
                      <tr className="border-t border-(--card-border) bg-white/5">
                        <td colSpan={7} className="py-2">
                          <span className="label">重置密码 · {u.username}</span>
                          <div className="mt-1.5 flex flex-wrap items-center gap-2">
                            <input
                              type="password"
                              className="w-56 rounded-lg border border-(--card-border) bg-transparent px-3 py-1.5 text-sm outline-hidden focus:border-(--accent)"
                              value={pwDraft}
                              onChange={(e) => {
                                setPwDraft(e.target.value);
                                setPwErr("");
                              }}
                              placeholder="新密码（至少 8 位）"
                            />
                            <button className="btn-primary text-xs" disabled={rowBusy} onClick={() => void savePassword(u)}>
                              确认重置
                            </button>
                            <button className="btn-ghost text-xs" onClick={() => { setPwEditId(""); setPwDraft(""); setPwErr(""); }}>
                              取消
                            </button>
                            {pwErr && <span className="text-xs text-red-300">{pwErr}</span>}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <p className="text-xs muted">
        角色说明：话务员只见被勾选的页面并维护自己的话术/快答；管理员可管理本账号全部资源；超级管理员权限不在本页调整。
      </p>
    </div>
  );
}
