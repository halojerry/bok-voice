"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import {
  hasPage,
  isManager,
  useSession,
  useSessionActions,
  type PageKey,
  type Session,
} from "@/components/session-context";

type NavItem = {
  href: string;
  label: string;
  /** 页面权限键（契约 §1；user 需有效集含该键才可见）。 */
  key?: PageKey;
  /** 主管专属（不可授予 user，见契约 §1）；管理员/匿名本地模式可见。 */
  admin?: boolean;
};

/** 主导航（user 项在前、主管管理区在后；顺序即契约 §4 的最终顺序）。 */
const NAV: NavItem[] = [
  { href: "/calls", label: "会话", key: "calls" },
  { href: "/roster", label: "名册", key: "roster" },
  { href: "/campaigns", label: "外呼", key: "campaigns" },
  { href: "/objects", label: "对象", key: "objects" },
  { href: "/qa", label: "快答库", key: "qa" },
  { href: "/templates", label: "话术", key: "templates" },
  { href: "/interpret", label: "同传", key: "interpret" },
  { href: "/reports", label: "报表", key: "reports" },
  { href: "/supervisor", label: "主管台", admin: true },
  { href: "/users", label: "员工", admin: true },
  { href: "/knowledge", label: "知识库", admin: true },
  { href: "/personas", label: "人设", admin: true },
  { href: "/audit", label: "审计", admin: true },
  { href: "/settings", label: "设置", admin: true },
];

/** 不在主导航中但需路由守卫的页面：/translate（与同传同键）。 */
const GUARD_ONLY: NavItem[] = [
  { href: "/translate", label: "同传", key: "interpret" },
];

/** 路由访问门（契约 §4）：open=放行；page=按权限键；manager=主管专属。 */
export type RouteGate = { kind: "open" } | { kind: "page"; key: PageKey } | { kind: "manager" };

/** 前缀匹配：/calls 命中 /calls 与 /calls/**（静态导出尾斜杠兼容），不命中 /callsXYZ。 */
function matchesPath(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

/** 路径 → 访问门；未匹配任何前缀的路径一律放行（如 "/"、"/setup"）。 */
export function gateForPath(pathname: string): RouteGate {
  const hit = [...NAV, ...GUARD_ONLY].find((n) => matchesPath(pathname, n.href));
  if (!hit) return { kind: "open" };
  if (hit.admin) return { kind: "manager" };
  return hit.key ? { kind: "page", key: hit.key } : { kind: "open" };
}

/** 导航项可见性：匿名本地模式/admin/root=全部；user=权限键 ∩ 有效集（主管项隐藏）。 */
function navVisible(item: NavItem, session: Session | null): boolean {
  if (!session) return false;
  if (isManager(session)) return true;
  if (item.admin) return false;
  return item.key ? hasPage(session, item.key) : true;
}

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
        <span className="flex h-9 w-9 items-center justify-center rounded-sm bg-(--stage-value) font-mono text-base font-bold text-[#01191c]">
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
