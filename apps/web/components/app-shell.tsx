"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useRef, useState } from "react";
import { AlertCircle, Inbox } from "lucide-react";
import { AccountProvider, useAccount } from "@/components/account-context";
import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";
import { Skeleton } from "@/components/ui/skeleton";
import { TooltipProvider } from "@/components/ui/tooltip";
import { gateForPath } from "@/lib/navigation";
import {
  SessionProvider,
  hasPage,
  isManager,
  useSession,
} from "@/components/session-context";
import { friendlyErrorText } from "@/lib/api-ready";

function StatusBadge() {
  const { health, settingsLoading } = useAccount();
  if (settingsLoading) return <span className="font-mono">loading</span>;
  if (health === false) return <span className="text-xs text-red-600">控制面离线</span>;
  return (
    <span className="hidden items-center gap-2 text-xs text-muted-foreground sm:inline-flex">
      <span className="h-2 w-2 rounded-full bg-(--live)" />
      <span className="font-mono">v0.1.0</span>
    </span>
  );
}

/** 会话加载中的轻量占位：不渲染导航，避免权限面闪跳。 */
function SessionLoading() {
  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-background">
      <p className="text-sm muted">正在加载会话…</p>
    </div>
  );
}

/** 无权限拦截面板：仍留在壳内，用户可经导航离开当前页。 */
function NoPermission() {
  return (
    <div className="card mx-auto mt-16 max-w-xl text-center">
      <p className="text-sm">无权访问此页面，请联系主管开通权限。</p>
      {/* 回首页（路由门 open）：calls 可能同样被收回，钉 / 兜底防循环撞墙。 */}
      <Link href="/" className="stage-btn-ghost mt-4">
        返回首页
      </Link>
    </div>
  );
}

/** 会话就绪门：session 加载完成前只显示占位（防导航/权限闪跳）。 */
function SessionReady({ children }: { children: React.ReactNode }) {
  const session = useSession();
  if (!session) return <SessionLoading />;
  return <>{children}</>;
}

/** 路由守卫（契约 §4）：路径前缀 → 权限键 / 主管专属 / root 专属；未匹配前缀放行。 */
function RouteGuard({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const session = useSession();
  if (!session) return <SessionLoading />;
  const gate = gateForPath(pathname);
  if (gate.kind === "page" && !hasPage(session, gate.key)) return <NoPermission />;
  if (gate.kind === "manager" && !isManager(session)) return <NoPermission />;
  // root 专属（/nodes 平台面）：匿名本地会话不算 root（匿名=acc-001 工作台形态）。
  if (gate.kind === "root" && !(session.role === "root" && !session.anonymous)) {
    return <NoPermission />;
  }
  return <>{children}</>;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  // 移动端导航抽屉开关：壳持有（Task 2 契约），Sidebar 受控、Topbar 汉堡触发。
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  // 汉堡按钮 ref：抽屉（dialog）关闭后 Sidebar 把焦点还到打开者。
  const mobileTriggerRef = useRef<HTMLButtonElement | null>(null);
  // 会话在最外层：AccountProvider（账号归属）与导航/守卫都读 SessionProvider。
  return (
    <SessionProvider>
      <SessionReady>
        <AccountProvider>
          {/* TooltipProvider 全站单例：折叠态导航 Tooltip 等都在此伞下。 */}
          <TooltipProvider>
            <div className="flex min-h-screen w-full bg-background">
              <Sidebar
                mobileOpen={mobileNavOpen}
                onMobileClose={() => setMobileNavOpen(false)}
                mobileTriggerRef={mobileTriggerRef}
              />
              <div className="flex min-w-0 flex-1 flex-col">
                <Topbar
                  onMobileOpen={() => setMobileNavOpen(true)}
                  mobileTriggerRef={mobileTriggerRef}
                  status={<StatusBadge />}
                />
                <main className="mx-auto w-full max-w-6xl flex-1 px-6 pb-12 pt-6 lg:px-8">
                  <RouteGuard>{children}</RouteGuard>
                </main>
              </div>
            </div>
          </TooltipProvider>
        </AccountProvider>
      </SessionReady>
    </SessionProvider>
  );
}

/** 加载态：Skeleton 骨架行 + 原文案（P4 浅色改版；文案一字不动，只换视觉）。 */
export function LoadingState({
  label = "加载中…",
  skeletonLines = 3,
}: {
  label?: string;
  skeletonLines?: number;
}) {
  const lines = Math.max(1, skeletonLines);
  return (
    <div className="space-y-2" role="status">
      <div aria-hidden="true" className="space-y-2">
        {Array.from({ length: lines }, (_, i) => (
          <Skeleton key={i} className={i === lines - 1 ? "h-4 w-2/3" : "h-4 w-full"} />
        ))}
      </div>
      <p className="text-sm muted">{label}</p>
    </div>
  );
}

/** 空态：lucide 图标 + 原文案（文案一字不动）。 */
export function EmptyState({ label = "暂无数据" }: { label?: string }) {
  return (
    <div className="flex flex-col items-center gap-2 py-10 text-muted-foreground">
      <Inbox className="h-8 w-8" aria-hidden="true" />
      <p className="text-sm muted">{label}</p>
    </div>
  );
}

/** 错误态：lucide 图标 + 友好文案；红底红字样式保留。 */
export function ErrorState({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg bg-red-500/10 p-3 text-sm text-red-600">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <p>{friendlyErrorText(message)}</p>
    </div>
  );
}
