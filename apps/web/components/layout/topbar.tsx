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
import { useMemo, useState, type ReactNode, type Ref } from "react";
import { Menu } from "lucide-react";
import { useSession, useSessionActions } from "@/components/session-context";
import { api } from "@/lib/api";
import { FLAT_NAV, GUARD_ONLY, matchesPath } from "@/lib/navigation";

export type TopbarProps = {
  /** 打开移动端导航抽屉（<md 汉堡按钮回调；抽屉状态由壳持有）。 */
  onMobileOpen: () => void;
  /** 汉堡按钮 ref（壳层接线）：抽屉关闭后 Sidebar 把焦点还到这里。 */
  mobileTriggerRef?: Ref<HTMLButtonElement>;
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

/**
 * 改密对话框（W5-T2 孤儿 API 收编：api.changeMyPassword 此前无任何消费点）。
 * 自绘覆盖层照 qa 页 IntentEditorModal 手法：fixed 全屏遮罩点击关窗、内层
 * stopPropagation；新密码 ≥8 位前端先拦；成功=关窗+logout+跳登录页重新登录。
 */
function ChangePasswordDialog({ onClose }: { onClose: () => void }) {
  const { logout } = useSessionActions();
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit() {
    if (newPw.length < 8) {
      setErr("新密码至少 8 位。");
      return;
    }
    if (newPw !== confirmPw) {
      setErr("两次输入的新密码不一致。");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      await api.changeMyPassword(oldPw, newPw);
      // 成功即失效旧会话：关框 → 登出 → 回登录页重新登录。
      onClose();
      logout();
      window.location.href = "/login/";
    } catch (e) {
      setErr(String(e));
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
      onContextMenu={(e) => e.preventDefault()}
    >
      <div
        className="w-full max-w-sm rounded-xl border border-(--card-border) bg-(--card) p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3">
          <span className="label">修改密码</span>
          <button type="button" className="btn-ghost text-xs" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="mt-3 space-y-3">
          <label className="block">
            <span className="text-xs text-muted-foreground">当前密码</span>
            <input
              autoFocus
              type="password"
              className="input mt-1"
              value={oldPw}
              onChange={(e) => {
                setOldPw(e.target.value);
                setErr("");
              }}
            />
          </label>
          <label className="block">
            <span className="text-xs text-muted-foreground">新密码（至少 8 位）</span>
            <input
              type="password"
              className="input mt-1"
              value={newPw}
              onChange={(e) => {
                setNewPw(e.target.value);
                setErr("");
              }}
            />
          </label>
          <label className="block">
            <span className="text-xs text-muted-foreground">确认新密码</span>
            <input
              type="password"
              className="input mt-1"
              value={confirmPw}
              onChange={(e) => {
                setConfirmPw(e.target.value);
                setErr("");
              }}
            />
          </label>
          {err && <p className="text-sm text-red-600">{err}</p>}
          <button
            type="button"
            className="btn-primary w-full"
            disabled={busy || !oldPw || !newPw}
            onClick={() => void submit()}
          >
            {busy ? "提交中…" : "修改并重新登录"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function Topbar({ onMobileOpen, mobileTriggerRef, status }: TopbarProps) {
  const pathname = usePathname();
  const session = useSession();
  const { logout } = useSessionActions();
  const title = useMemo(() => pageTitle(pathname), [pathname]);
  const [pwOpen, setPwOpen] = useState(false);

  function handleLogout() {
    logout();
    window.location.href = "/login/";
  }

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-4 border-b border-border bg-background/85 px-4 backdrop-blur lg:px-6">
      <div className="flex min-w-0 items-center gap-3">
        <button
          ref={mobileTriggerRef}
          type="button"
          aria-label="打开导航"
          aria-haspopup="dialog"
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
              onClick={() => setPwOpen(true)}
            >
              改密
            </button>
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
      {pwOpen && <ChangePasswordDialog onClose={() => setPwOpen(false)} />}
    </header>
  );
}
