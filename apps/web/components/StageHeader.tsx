"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useSession, useSessionActions } from "@/components/session-context";
import { FLAT_NAV as NAV, matchesPath, navVisible } from "@/lib/navigation";

// 导航事实源已迁 @/lib/navigation（P2 Task 1，分组侧边栏地基）；本组件渲染行为
// 零变化。gateForPath/RouteGate 原地转出口——app-shell 等既有 import 面不变
// （Task 3 退役本组件时随壳一并改源）。
export { gateForPath } from "@/lib/navigation";
export type { RouteGate } from "@/lib/navigation";

/**
 * 全站统一顶部导航（取自首页舞台顶栏）。所有页面共用，保证整套版式一致。
 * 导航按会话权限过滤；右侧显示账号（匿名=本地模式徽标，登录=用户名+退出）。
 * @param status 右侧状态徽标；默认显示版本号。
 */
export function StageHeader({ status }: { status?: ReactNode }) {
  const pathname = usePathname();
  const session = useSession();
  const { logout } = useSessionActions();
  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : matchesPath(pathname, href);

  function handleLogout() {
    logout();
    window.location.href = "/login/";
  }

  return (
    <header className="mx-auto flex h-16 w-full max-w-7xl items-center justify-between gap-4 px-6 lg:px-10">
      <Link href="/" className="flex shrink-0 items-center gap-2.5">
        <span className="flex h-9 w-9 items-center justify-center rounded-sm bg-(--stage-value) font-mono text-base font-bold text-white">
          B
        </span>
        <span className="text-[15px] font-medium tracking-tight">Bok Voice</span>
      </Link>

      <nav className="hidden items-center gap-7 text-sm text-(--stage-muted) md:flex">
        {NAV.filter((n) => navVisible(n, session)).map((n) => (
          <Link
            key={n.href}
            href={n.href}
            className={`transition hover:text-(--foreground) ${
              isActive(n.href) ? "text-(--stage-value)" : ""
            }`}
          >
            {n.label}
          </Link>
        ))}
      </nav>

      <div className="flex shrink-0 items-center gap-4">
        <span className="hidden items-center gap-2 text-xs text-(--stage-muted) sm:inline-flex">
          {status ?? (
            <>
              <span className="h-2 w-2 rounded-full bg-(--stage-value)" />
              <span className="font-mono">v0.1.0</span>
            </>
          )}
        </span>
        {session?.anonymous && (
          <span className="flex items-center gap-2">
            <span className="rounded-sm border border-(--card-border) px-2 py-0.5 text-[11px] text-(--stage-muted)">
              本地模式
            </span>
            {/* 匿名=auth-off 单机形态的兜底会话，但登录入口必须可达（2026-09-15
                用户实测「看不到登录页」）——已配 BOK_JWT_SECRET 的部署点此进入账号态。 */}
            <Link
              href="/login/"
              className="transition hover:text-(--foreground) text-xs text-(--stage-muted)"
            >
              登录
            </Link>
          </span>
        )}
        {session && !session.anonymous && (
          <span className="flex items-center gap-2 text-xs text-(--stage-muted)">
            <span className="max-w-24 truncate">{session.display_name || session.username}</span>
            <button
              type="button"
              className="transition hover:text-(--foreground)"
              onClick={handleLogout}
            >
              退出
            </button>
          </span>
        )}
        <Link href="/calls/new" className="stage-btn-primary">
          进入工作台
        </Link>
      </div>
    </header>
  );
}
