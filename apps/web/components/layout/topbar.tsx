"use client";

/**
 * P2 布局壳 · 细顶栏（Task 2 交付组件，Task 3 壳层接线）。
 *
 * 左 = 移动端汉堡（<md，开抽屉；状态由壳持有）+ 当前页标题（pathname 经
 * FLAT_NAV/GUARD_ONLY 最长前缀反查 label；首页给「工作台」，其余未命中显示空串）。
 * 右 = status slot（壳传 StatusBadge；undefined 不渲染容器）+ 账号区
 * （匿名=「本地模式」徽标+登录链接；登录=用户名+退出）+「进入工作台」
 * btn-primary CTA（入口非活状态 → 近黑，不用青）。
 * 折叠开关归 Sidebar 底部，不进顶栏。
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useMemo, type ReactNode } from "react";
import { Menu } from "lucide-react";
import { useSession, useSessionActions } from "@/components/session-context";
import { FLAT_NAV, GUARD_ONLY, matchesPath } from "@/lib/navigation";

export type TopbarProps = {
  /** 打开移动端导航抽屉（<md 汉堡按钮回调；抽屉状态由壳持有）。 */
  onMobileOpen: () => void;
  /** 右侧状态区（壳传 StatusBadge）；未提供时（undefined/null）不渲染容器。 */
  status?: ReactNode;
};

/** 路径 → 页标题：FLAT_NAV+GUARD_ONLY 内最长前缀命中项的 label。 */
function pageTitle(pathname: string): string {
  // 首页不在主导航（stage 工作台经 DashboardPage 内嵌壳内），但顶栏仍给页名
  // （与页面 h1 一致）；其余未命中路径显示空串。
  if (pathname === "/") return "工作台";
  const hits = [...FLAT_NAV, ...GUARD_ONLY].filter((n) =>
    matchesPath(pathname, n.href),
  );
  if (hits.length === 0) return "";
  // 最长前缀优先（前缀本互不重叠，此处按最长兜底 /calls/new 类子路径）。
  return hits.reduce((best, n) => (n.href.length > best.href.length ? n : best))
    .label;
}

export function Topbar({ onMobileOpen, status }: TopbarProps) {
  const pathname = usePathname();
  const session = useSession();
  const { logout } = useSessionActions();
  const title = useMemo(() => pageTitle(pathname), [pathname]);

  function handleLogout() {
    logout();
    window.location.href = "/login/";
  }

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-4 border-b border-border bg-background/85 px-4 backdrop-blur lg:px-6">
      <div className="flex min-w-0 items-center gap-3">
        <button
          type="button"
          aria-label="打开导航"
          onClick={onMobileOpen}
          className="flex size-9 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground md:hidden"
        >
          <Menu size={18} />
        </button>
        <span className="truncate text-sm font-medium">{title}</span>
      </div>

      <div className="flex shrink-0 items-center gap-4">
        {status != null && <div className="shrink-0">{status}</div>}
        {/* —— 账号区 —— */}
        {session?.anonymous && (
          <span className="flex items-center gap-2">
            <span className="rounded-sm border border-(--card-border) px-2 py-0.5 text-[11px] text-muted-foreground">
              本地模式
            </span>
            {/* 匿名=auth-off 单机形态的兜底会话，但登录入口必须可达（2026-09-15
                用户实测「看不到登录页」）——已配 BOK_JWT_SECRET 的部署点此进入账号态。 */}
            <Link
              href="/login/"
              className="transition text-xs text-muted-foreground hover:text-foreground"
            >
              登录
            </Link>
          </span>
        )}
        {session && !session.anonymous && (
          <span className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="max-w-24 truncate">
              {session.display_name || session.username}
            </span>
            <button
              type="button"
              className="transition hover:text-(--foreground)"
              onClick={handleLogout}
            >
              退出
            </button>
          </span>
        )}
        <Link href="/calls/new" className="btn-primary">
          进入工作台
        </Link>
      </div>
    </header>
  );
}
