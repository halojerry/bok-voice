"use client";

// 客户管理（W① 2026-10-09，root 专属平台面）：SaaS 客户生命周期——
// 列表（管理员/员工数/有效期/到期态）、新建客户（名称+管理员账密+有效期）、
// 一键续费（30/90/365 天）、改显示名、设永久。到期执法在 CP identity_gate
// （W②）；本页只做生命周期 CRUD。引擎/模型配置恒不在此（/settings 平台控制台）。

import { useCallback, useEffect, useState } from "react";
import { api, type AccountRow } from "@/lib/api";
import { EmptyState, ErrorState, LoadingState } from "@/components/app-shell";
import { friendlyErrorText } from "@/lib/api-ready";

/** 续费快捷档（天）——按钮排一行，自定义天数走输入框。 */
const RENEW_DAYS = [30, 90, 365];

/** 与 users 页同款输入框类（仓内无全局 .input，各页局部声明）。 */
const input = "w-full rounded-lg border border-(--card-border) bg-transparent px-3 py-2 text-sm outline-hidden focus:border-(--live)";

const DURATION_OPTIONS: { value: number; label: string }[] = [
  { value: 0, label: "永久" },
  { value: 30, label: "30 天" },
  { value: 90, label: "3 个月" },
  { value: 180, label: "6 个月" },
  { value: 365, label: "12 个月" },
];

type CreateForm = {
  display_name: string;
  admin_username: string;
  admin_password: string;
  duration_days: number;
};

const EMPTY_CREATE: CreateForm = {
  display_name: "",
  admin_username: "",
  admin_password: "",
  duration_days: 90,
};

function fmtExpires(iso: string): string {
  if (!iso) return "永久";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export default function CustomersPage() {
  const [rows, setRows] = useState<AccountRow[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [form, setForm] = useState<CreateForm>(EMPTY_CREATE);
  const [formError, setFormError] = useState("");
  const [creating, setCreating] = useState(false);
  const [renewDays, setRenewDays] = useState<Record<string, string>>({});

  const reload = useCallback(async () => {
    try {
      setRows(await api.listAccounts());
      setError("");
    } catch (e) {
      setError(friendlyErrorText(e));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const create = useCallback(async () => {
    setFormError("");
    if (!form.admin_username.trim()) {
      setFormError("请填写客户管理员用户名");
      return;
    }
    if (form.admin_password.length < 12) {
      setFormError("客户管理员密码至少 12 位");
      return;
    }
    setCreating(true);
    try {
      await api.createAccount({
        display_name: form.display_name.trim(),
        admin_username: form.admin_username.trim(),
        admin_password: form.admin_password,
        duration_days: form.duration_days,
      });
      setForm(EMPTY_CREATE);
      await reload();
    } catch (e) {
      setFormError(friendlyErrorText(e));
    } finally {
      setCreating(false);
    }
  }, [form, reload]);

  const renew = useCallback(async (id: string, days: number) => {
    setBusy(`${id}:${days}`);
    try {
      await api.renewAccount(id, days);
      await reload();
    } catch (e) {
      setError(friendlyErrorText(e));
    } finally {
      setBusy("");
    }
  }, [reload]);

  const makePermanent = useCallback(async (id: string) => {
    if (!window.confirm("确认设为永久？该客户将不再受订阅到期限制。")) return;
    setBusy(`${id}:perm`);
    try {
      await api.patchAccount(id, { expires_at: "permanent" });
      await reload();
    } catch (e) {
      setError(friendlyErrorText(e));
    } finally {
      setBusy("");
    }
  }, [reload]);

  const renewCustom = useCallback(async (id: string) => {
    const raw = (renewDays[id] || "").trim();
    if (!raw) return;
    const days = Number(raw);
    if (!Number.isInteger(days) || days <= 0 || days > 3650) {
      setError("自定义天数需为 1~3650 的整数");
      return;
    }
    await renew(id, days);
    setRenewDays((m) => ({ ...m, [id]: "" }));
  }, [renew, renewDays]);

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6 p-6">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold">客户管理</h1>
        <p className="muted text-xs">
          一个客户=一个独立账号域（数据互相隔离）。到期后客户侧整站只显示续费提示；
          续费按剩余时长顺延，已过期则从当下起算。
        </p>
      </header>

      {error ? <ErrorState message={error} /> : null}

      <section className="rounded-lg border p-4 space-y-3">
        <span className="label">新建客户</span>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1 text-xs">
            <span className="muted">客户名称（缺省=管理员用户名）</span>
            <input
              className={input}
              value={form.display_name}
              onChange={(e) => setForm((f) => ({ ...f, display_name: e.target.value }))}
              placeholder="例如：某某贸易有限公司"
            />
          </label>
          <label className="space-y-1 text-xs">
            <span className="muted">客户管理员用户名</span>
            <input
              className={input}
              value={form.admin_username}
              onChange={(e) => setForm((f) => ({ ...f, admin_username: e.target.value }))}
              placeholder="客户登录账号"
            />
          </label>
          <label className="space-y-1 text-xs">
            <span className="muted">初始密码（≥12 位）</span>
            <input
              type="password"
              className={input}
              value={form.admin_password}
              onChange={(e) => setForm((f) => ({ ...f, admin_password: e.target.value }))}
            />
          </label>
          <label className="space-y-1 text-xs">
            <span className="muted">使用有效期</span>
            <select
              className={input}
              value={form.duration_days}
              onChange={(e) => setForm((f) => ({ ...f, duration_days: Number(e.target.value) }))}
            >
              {DURATION_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
        </div>
        {formError ? <p className="text-xs text-red-600">{formError}</p> : null}
        <div className="flex items-center gap-3">
          <button className="btn-primary text-xs" onClick={() => void create()} disabled={creating}>
            {creating ? "创建中…" : "创建客户"}
          </button>
          <p className="muted text-xs">管理员开箱即可自建员工（默认授予「员工管理」）。</p>
        </div>
      </section>

      <section className="space-y-3">
        <span className="label">客户列表</span>
        {rows === null ? (
          <LoadingState />
        ) : rows.length === 0 ? (
          <EmptyState label="暂无客户——上方新建第一个客户。" />
        ) : (
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full text-xs">
              <thead className="bg-muted/50 text-left">
                <tr>
                  <th className="px-3 py-2">客户</th>
                  <th className="px-3 py-2">账号 ID</th>
                  <th className="px-3 py-2">管理员</th>
                  <th className="px-3 py-2">员工数</th>
                  <th className="px-3 py-2">有效期至</th>
                  <th className="px-3 py-2">状态</th>
                  <th className="px-3 py-2">续费</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-t align-top">
                    <td className="px-3 py-2 font-medium">{r.display_name || r.id}</td>
                    <td className="px-3 py-2 muted font-mono">{r.id}</td>
                    <td className="px-3 py-2">{r.admin_username || "—"}</td>
                    <td className="px-3 py-2">{r.user_count}</td>
                    <td className="px-3 py-2">{fmtExpires(r.expires_at)}</td>
                    <td className="px-3 py-2">
                      {r.expires_at === "" ? (
                        <span className="muted">永久</span>
                      ) : r.expired ? (
                        <span className="rounded bg-red-100 px-1.5 py-0.5 text-red-700">已到期</span>
                      ) : (
                        <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-700">生效中</span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {RENEW_DAYS.map((d) => (
                          <button
                            key={d}
                            className="btn-ghost text-xs"
                            disabled={busy !== ""}
                            onClick={() => void renew(r.id, d)}
                          >
                            {busy === `${r.id}:${d}` ? "…" : `+${d}天`}
                          </button>
                        ))}
                        <input
                          className={`w-16 rounded-lg border border-(--card-border) bg-transparent px-2 py-1 text-xs outline-hidden focus:border-(--live)}`}
                          placeholder="天数"
                          value={renewDays[r.id] || ""}
                          onChange={(e) => setRenewDays((m) => ({ ...m, [r.id]: e.target.value }))}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") void renewCustom(r.id);
                          }}
                        />
                        {r.expires_at !== "" ? (
                          <button
                            className="btn-ghost text-xs"
                            disabled={busy !== ""}
                            onClick={() => void makePermanent(r.id)}
                          >
                            {busy === `${r.id}:perm` ? "…" : "设永久"}
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
