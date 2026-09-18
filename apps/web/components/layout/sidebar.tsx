"use client";

/**
 * P2 布局壳 · 分组侧边栏（Task 2 交付组件，Task 3 壳层接线）。
 *
 * - 桌面（≥md）恒驻：展开 `w-60` / 折叠 `w-[3.5rem]` 图标轨。折叠偏好存
 *   localStorage `bok_sidebar_collapsed`（"1"=折叠）；初渲染恒按展开渲染、
 *   mount 后再读存储——static export 的服务端 HTML 与客户端首帧保持一致，
 *   防水合 mismatch。舞台路由（/calls/new、/interpret、/translate，判定走
 *   lib/navigation isStageRoute 单一事实源）自动折叠——临时态**不写偏好键**，
 *   离开舞台路由恢复存储偏好；舞台页上的手动切换仅当页临时生效。
 * - 移动（<md）：受控 off-canvas 抽屉 + 常挂半透明遮罩（opacity/pointer-events
 *   过渡渐隐，点击/Esc 关闭），translate-x 过渡；抽屉开关状态由壳持有
 *   （props 受控），路由变化自动收起，打开期间锁 body 滚动。抽屉恒展开
 *   （折叠偏好只作用于桌面恒驻栏），内容与桌面共用 SidebarContent 渲染函数。
 * - 分组/权限判定全走 lib/navigation 单一事实源：组内条目经 navVisible 过滤
 *   后为空则整组（含 eyebrow 组头）不渲染；第一组无上边框，组间 border-t。
 * - 激活态（matchesPath）= 品牌青 --live 族（bg-(--live-soft) /
 *   text-(--live-ink)），折叠态另加左缘 2px --live 竖条；非激活 hover =
 *   shadcn hover 灰（bg-accent）。青色只作活信号，其余中性。
 * - 折叠态条目用 Tooltip 显示 label（TooltipProvider 不在此挂——壳层挂一次）。
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useSession } from "@/components/session-context";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  NAV_GROUPS,
  isStageRoute,
  matchesPath,
  navVisible,
  type NavItem,
} from "@/lib/navigation";
import { cn } from "cn";

/** 折叠偏好存储键（"1"=折叠）。 */
const SIDEBAR_COLLAPSED_KEY = "bok_sidebar_collapsed";

export type SidebarProps = {
  /** 移动端抽屉开关（<md 生效；状态由壳持有，本组件不自管）。 */
  mobileOpen: boolean;
  /** 请求收起移动端抽屉（遮罩点击 / 路由变化时回调壳）。 */
  onMobileClose: () => void;
};

/** 单条导航链接：激活=青底青字，折叠=图标居中+Tooltip+左缘竖条。 */
function SidebarNavLink({
  item,
  collapsed,
  active,
}: {
  item: NavItem;
  collapsed: boolean;
  active: boolean;
}) {
  const Icon = item.icon;

  const link = (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "relative flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm transition-colors",
        collapsed && "justify-center px-0",
        active
          ? "bg-(--live-soft) font-medium text-(--live-ink)"
          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
      )}
    >
      {collapsed && active && (
        <span
          aria-hidden
          className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-(--live)"
        />
      )}
      <Icon size={18} className="shrink-0" />
      <span className={cn("truncate", collapsed && "sr-only")}>{item.label}</span>
    </Link>
  );

  if (!collapsed) return link;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{item.label}</TooltipContent>
    </Tooltip>
  );
}

/** 抽屉与桌面共用的侧栏内容：分组导航 + 底部折叠开关（抽屉不显示开关）。 */
function SidebarContent({
  collapsed,
  onToggleCollapsed,
  pathname,
  showToggle = true,
}: {
  collapsed: boolean;
  onToggleCollapsed: () => void;
  pathname: string;
  /** 移动端抽屉恒展开、关闭即走，不提供折叠开关。 */
  showToggle?: boolean;
}) {
  const session = useSession();

  // 组内条目全被权限过滤掉则整组不渲染（含组头）。
  const visibleGroups = NAV_GROUPS.map((group) => ({
    label: group.label,
    items: group.items.filter((item) => navVisible(item, session)),
  })).filter((group) => group.items.length > 0);

  return (
    <>
      <nav aria-label="主导航" className="min-h-0 flex-1 overflow-y-auto py-2">
        {visibleGroups.map((group, index) => (
          <div
            key={group.label}
            className={cn(
              "flex flex-col gap-0.5 py-2",
              // 第一组无上边框；组间 border-t 分隔。
              index > 0 && "border-t border-border",
            )}
          >
            <span className={cn("eyebrow px-2.5 pb-1", collapsed && "sr-only")}>
              {group.label}
            </span>
            {group.items.map((item) => (
              <SidebarNavLink
                key={item.href}
                item={item}
                collapsed={collapsed}
                active={matchesPath(pathname, item.href)}
              />
            ))}
          </div>
        ))}
      </nav>

      <div className={cn("mt-auto shrink-0 p-2", !showToggle && "hidden")}>
        <button
          type="button"
          onClick={onToggleCollapsed}
          aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
          title={collapsed ? "展开侧边栏" : "收起侧边栏"}
          className={cn(
            "flex w-full items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground",
            collapsed && "justify-center px-0",
          )}
        >
          {collapsed ? (
            <PanelLeftOpen size={18} className="shrink-0" />
          ) : (
            <PanelLeftClose size={18} className="shrink-0" />
          )}
          {!collapsed && <span className="truncate">收起导航</span>}
        </button>
      </div>
    </>
  );
}

export function Sidebar({ mobileOpen, onMobileClose }: SidebarProps) {
  const pathname = usePathname();
  const onStage = isStageRoute(pathname);
  // 存储偏好：初渲染恒展开（与 static export 服务端 HTML 一致），mount 后应用存储值。
  const [storedCollapsed, setStoredCollapsed] = useState(false);
  // 舞台路由上的临时手动切换（null=跟随自动折叠）；换路由即清零，
  // 重新进入舞台页重新自动折叠、离开即恢复存储偏好。
  const [stageOverride, setStageOverride] = useState<boolean | null>(null);

  useEffect(() => {
    try {
      setStoredCollapsed(
        window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1",
      );
    } catch {
      /* localStorage 不可用（隐私模式等）——保持展开 */
    }
  }, []);

  // 换路由清舞台临时态：自动折叠是「进舞台页」的一次性行为，不跨路由续命。
  useEffect(() => {
    setStageOverride(null);
  }, [pathname]);

  // 生效折叠态：舞台路由=自动折叠（用户当页临时切换可覆写）；其余路由=存储偏好。
  // 舞台判定只依赖 pathname（prerender 与客户端同源），不引入水合 mismatch。
  const collapsed = onStage ? (stageOverride ?? true) : storedCollapsed;

  // 路由变化自动收起移动端抽屉。回调经 effect 写入 ref（不在渲染期改 ref，
  // 并发渲染下丢弃的渲染不带副作用）；记录上一个 pathname，mount 首帧不触发。
  const closeRef = useRef(onMobileClose);
  useEffect(() => {
    closeRef.current = onMobileClose;
  }, [onMobileClose]);
  const pathnameRef = useRef(pathname);
  useEffect(() => {
    if (pathnameRef.current !== pathname) {
      pathnameRef.current = pathname;
      closeRef.current();
    }
  }, [pathname]);

  // 抽屉打开时：Esc 关闭 + 锁 body 滚动（遮罩外的背景页不跟滚）。
  useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [mobileOpen]);

  const toggleCollapsed = () => {
    if (onStage) {
      // 舞台路由上的手动切换仅临时生效——不写用户偏好键，离开路由即失效。
      setStageOverride((prev) => !(prev ?? true));
      return;
    }
    setStoredCollapsed((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        /* 持久化失败——折叠态仅在当前会话生效 */
      }
      return next;
    });
  };

  const widthClass = collapsed ? "w-[3.5rem]" : "w-60";

  return (
    <>
      {/* 桌面恒驻（≥md）：文档流内 sticky 左栏，不遮内容。 */}
      <aside
        className={cn(
          "sticky top-0 hidden h-screen shrink-0 flex-col border-r border-border bg-background md:flex",
          widthClass,
        )}
      >
        <SidebarContent
          collapsed={collapsed}
          onToggleCollapsed={toggleCollapsed}
          pathname={pathname}
        />
      </aside>

      {/* 移动端抽屉（<md）：off-canvas + 半透明遮罩，translate-x / opacity 过渡。
          抽屉恒展开（折叠只属桌面恒驻栏），关闭走遮罩/Esc/路由变化。
          遮罩常挂（opacity + pointer-events 过渡）：关闭时抽屉 200ms 滑出期间
          遮罩同步渐隐，不再条件渲染瞬拆闪断；关闭态 inert + 透明不可交互。 */}
      <div className="md:hidden">
        <button
          type="button"
          aria-label="关闭导航"
          onClick={onMobileClose}
          inert={!mobileOpen}
          className={cn(
            "fixed inset-0 z-40 bg-foreground/10 transition-opacity duration-200",
            mobileOpen ? "opacity-100" : "pointer-events-none opacity-0",
          )}
        />
        <aside
          inert={!mobileOpen}
          className={cn(
            "fixed inset-y-0 left-0 z-50 flex w-60 flex-col border-r border-border bg-background transition-transform duration-200",
            mobileOpen ? "translate-x-0" : "-translate-x-full",
          )}
        >
          <SidebarContent
            collapsed={false}
            onToggleCollapsed={toggleCollapsed}
            pathname={pathname}
            showToggle={false}
          />
        </aside>
      </div>
    </>
  );
}
