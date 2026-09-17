"use client";

// B4 登录页（契约 §4）：用户名+密码 → /api/auth/login → 存 token → 硬跳首页。
// 静态导出兼容：纯客户端表单，无任何服务端能力。
// 登录页在 (app) 分组之外，不带 AppShell（无导航/无会话守卫）。

import { useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { friendlyApiError } from "@/lib/api-ready";

const TOKEN_KEY = "bok_token";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setError("");
    setSubmitting(true);
    try {
      const res = await api.login(username.trim(), password);
      window.localStorage.setItem(TOKEN_KEY, res.token);
      // 硬跳（而非 router.push）：让 AppShell 的 SessionProvider 带新 token 重新挂载。
      window.location.href = "/";
    } catch (err) {
      const raw = err instanceof Error ? err.message : String(err ?? "");
      setError(/\b401\b/.test(raw) ? "用户名或密码错误" : friendlyApiError(err));
      setSubmitting(false);
    }
  }

  const canSubmit = username.trim().length > 0 && password.length > 0 && !submitting;

  return (
    <main className="stage-shell flex min-h-screen w-full items-center justify-center px-6">
      <div className="w-full max-w-sm">
        <div className="mb-5 flex items-center gap-2.5">
          <span className="flex h-9 w-9 items-center justify-center rounded-sm bg-(--stage-value) font-mono text-base font-bold text-[#01191c]">
            B
          </span>
          <span className="text-[15px] font-medium tracking-tight">Bok Voice</span>
        </div>

        <div className="rounded-2xl border border-(--card-border) bg-(--card) p-6">
          <h1 className="text-xl font-semibold">Bok Voice 登录</h1>
          <p className="mt-1.5 mb-5 text-sm muted">使用主管分配的账号登录工作台。</p>

          <form className="flex flex-col gap-3" onSubmit={handleSubmit}>
            <label className="flex flex-col gap-1.5">
              <span className="label">用户名</span>
              <input
                className="input"
                autoComplete="username"
                autoFocus
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1.5">
              <span className="label">密码</span>
              <input
                className="input"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>

            {error && (
              <p className="rounded-lg bg-red-500/10 p-2.5 text-sm text-red-600">{error}</p>
            )}

            <button
              className="stage-btn-primary mt-1 h-9 w-full disabled:opacity-50"
              type="submit"
              disabled={!canSubmit}
            >
              {submitting ? "登录中…" : "登录"}
            </button>
          </form>
        </div>

        <p className="mt-4 text-center text-xs muted">
          本机单用户模式无需登录，
          <Link href="/" className="text-accent hover:underline">
            直接返回首页
          </Link>
          。
        </p>
      </div>
    </main>
  );
}
