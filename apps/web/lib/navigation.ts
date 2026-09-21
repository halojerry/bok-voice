/**
 * 导航单一事实源（P2 布局壳）：分组结构 + 权限门判定。
 *
 * 侧边栏/顶栏/路由守卫都从这里取数。权限判定语义
 * （matchesPath/gateForPath/navVisible/RouteGate）自旧顶部导航组件 **逐字搬家**
 * ——零判定改动；只把扁平 NAV 重组为分组 NAV_GROUPS（工作台→运营→内容→管理，
 * 扁平顺序变化是产品预期）。分组前缀互不重叠，gateForPath 首匹配语义不受影响
 * （/calls/new 仍命中 /calls 的 calls 键）。
 */

import type { LucideIcon } from "lucide-react";
import {
  AudioLines,
  BookUser,
  ChartColumn,
  FileText,
  FolderOpen,
  Headphones,
  Library,
  MessageCircleQuestion,
  PhoneCall,
  Radar,
  ScrollText,
  Server,
  Settings,
  UserRound,
  Users,
  Workflow,
} from "lucide-react";
import {
  hasManagement,
  hasPage,
  type ManagementKey,
  type PageKey,
  type Session,
} from "@/components/session-context";

export type NavItem = {
  href: string;
  label: string;
  /** 页面权限键（契约 §1；user 需有效集含该键才可见）。 */
  key?: PageKey;
  /** 管理权限键（下发制 2026-09-20；admin 需被下发该键才可见，root 恒可见）。 */
  mkey?: ManagementKey;
  /** 主管专属（不可授予 user，见契约 §1）；可见性按下发制逐键判。 */
  admin?: boolean;
  /** root 专属（平台面，如 /nodes 节点治理）；仅 root 可见。 */
  rootOnly?: boolean;
  /** lucide-react 字形（侧边栏图标轨/折叠态提示用）。 */
  icon: LucideIcon;
};

/** 主导航分组（顺序即侧边栏顺序；分组序取代旧扁平 NAV 序，权限语义不受顺序影响）。 */
export const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "工作台",
    items: [
      { href: "/calls", label: "会话", key: "calls", icon: PhoneCall },
      { href: "/interpret", label: "同传", key: "interpret", icon: AudioLines },
    ],
  },
  {
    label: "运营",
    items: [
      { href: "/roster", label: "名册", key: "roster", icon: BookUser },
      { href: "/campaigns", label: "外呼", key: "campaigns", icon: Radar },
      { href: "/objects", label: "对象", key: "objects", icon: FolderOpen },
      { href: "/reports", label: "报表", key: "reports", icon: ChartColumn },
    ],
  },
  {
    // 2026-09-20 重组：话术+问答+意图已全部整合进 AI 工作站（/studio），
    // 旧「内容」组的 /templates、/qa 导航项移除（路由保留守卫，见 GUARD_ONLY）。
    label: "AI 设置",
    items: [
      { href: "/studio", label: "AI 工作站", key: "templates", icon: Workflow },
      { href: "/knowledge", label: "知识库", admin: true, mkey: "knowledge", icon: Library },
      { href: "/personas", label: "人设", admin: true, mkey: "personas", icon: UserRound },
    ],
  },
  {
    label: "管理",
    items: [
      { href: "/supervisor", label: "主管台", admin: true, mkey: "supervisor", icon: Headphones },
      { href: "/users", label: "员工", admin: true, mkey: "users", icon: Users },
      { href: "/nodes", label: "节点", rootOnly: true, icon: Server },
      { href: "/audit", label: "审计", admin: true, mkey: "audit", icon: ScrollText },
      { href: "/settings", label: "设置", admin: true, mkey: "settings", icon: Settings },
    ],
  },
];

/** 扁平导航（派生，不手写）：工作台→运营→AI 设置→管理。 */
export const FLAT_NAV: NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

/** 不在主导航中但需路由守卫/顶栏标题的页面（深链可达）：
 *  /translate（同传同键）；/templates、/qa（2026-09-20 移出导航——内容整合进 AI 工作站，
 *  工作站「意图管理」tab 与问答画布深链仍落到这两页，守卫键原样保留）。 */
export const GUARD_ONLY: NavItem[] = [
  { href: "/translate", label: "同传", key: "interpret", icon: AudioLines },
  { href: "/templates", label: "话术", key: "templates", icon: FileText },
  { href: "/qa", label: "快答库", key: "qa", icon: MessageCircleQuestion },
];

/**
 * 舞台路由（沉浸工作面）：进入时桌面侧栏自动收成图标轨——**临时态**，不写
 * `bok_sidebar_collapsed` 用户偏好键（离开舞台路由即恢复存储偏好；舞台页上的
 * 手动切换仅当轮路由生效）。/translate 取自 GUARD_ONLY 单一事实源，本表不重复拼写。
 */
export const STAGE_ROUTE_PREFIXES = [
  "/calls/new",
  "/interpret",
  ...GUARD_ONLY.map((item) => item.href),
];

/** 舞台路由判定（前缀语义同 matchesPath：/calls/new 命中 /calls/new/**，不命中 /calls）。 */
export function isStageRoute(pathname: string): boolean {
  return STAGE_ROUTE_PREFIXES.some((prefix) => matchesPath(pathname, prefix));
}

/** 路由访问门（契约 §4）：open=放行；page=按权限键；manager=主管专属；root=root 专属。 */
export type RouteGate =
  | { kind: "open" }
  | { kind: "page"; key: PageKey }
  | { kind: "manager" }
  | { kind: "root" };

/** 前缀匹配：/calls 命中 /calls 与 /calls/**（静态导出尾斜杠兼容），不命中 /callsXYZ。 */
export function matchesPath(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

/** 路径 → 访问门；未匹配任何前缀的路径一律放行（如 "/"、"/setup"）。 */
export function gateForPath(pathname: string): RouteGate {
  const hit = [...FLAT_NAV, ...GUARD_ONLY].find((n) => matchesPath(pathname, n.href));
  if (!hit) return { kind: "open" };
  if (hit.rootOnly) return { kind: "root" };
  if (hit.admin) return { kind: "manager" };
  return hit.key ? { kind: "page", key: hit.key } : { kind: "open" };
}

/** 导航项可见性（2026-09-20 三形态导航）：
 * root=平台控制台——只出管理面/平台面（运营页对 root 隐藏；服务端仍全域放行，作排障通道）；
 * admin=客户主管台——运营页按有效集、管理页按下发键（/nodes 平台资产恒不可见）；
 * user=话务员台——权限键 ∩ 有效集；匿名本地模式=单机全权。 */
export function navVisible(item: NavItem, session: Session | null): boolean {
  if (!session) return false;
  if (item.rootOnly) return !session.anonymous && session.role === "root";
  if (session.anonymous) return true;
  if (session.role === "root") return Boolean(item.admin) || item.rootOnly === true;
  if (session.role === "admin") {
    if (item.admin) return item.mkey ? hasManagement(session, item.mkey) : true;
    return item.key ? hasPage(session, item.key) : true;
  }
  if (item.admin) return false;
  return item.key ? hasPage(session, item.key) : true;
}
