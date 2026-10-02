"use client";

// 客户端数据层（2026-10-02 UX 根因修复·根因一「没有数据层」）：
// 此前 131 个 await api.* 全部裸 fetch-in-useEffect——零缓存零去重，每次导航全冷取、
// 六个页面各自重复拉同一端点（templates×5 / objects×6 / qa-entries×6）、mutation 后
// 全量 refetch。SWR 统一为 stale-while-revalidate：
//   - 缓存命中导航秒开（旧数据先上屏、后台静默换新）
//   - 同 key 并发请求自动去重
//   - revalidateOnFocus：切回标签页自动刷新（全局默认，app-shell 挂载）
//   - refreshInterval 轮询比裸 setInterval 多两件事：tab 隐藏自动暂停、
//     数据 identity 不变时零重渲染（稳定 key 比较）
// 渐进收编纪律：本文件只收编「跨页重复消费」的热端点（calls/objects/templates），
// 页面级一次性数据仍走各页自己的加载逻辑——不做大爆炸式重写。
import useSWR from "swr";
import { api } from "@/lib/api";

type Row = Record<string, unknown>;

/** 通话列表。limit>0=服务端分页（created_at 倒序）；refreshMs>0=轮询（tab 隐藏自停）。 */
export function useCallsList(accountId: string, status = "", limit = 0, refreshMs = 0) {
  return useSWR<Row[]>(
    ["calls", accountId, status, limit],
    () => api.listCalls(accountId, status, limit),
    { refreshInterval: refreshMs || 0 },
  );
}

/** 对象列表（objects 页 / calls 页名称映射 / campaigns / TemplateVars 共享同一缓存）。 */
export function useObjectsList(accountId: string) {
  return useSWR<Row[]>(["objects", accountId], () => api.listObjects(accountId));
}

/** 话术模板列表（studio / templates / qa 画布脊柱 / objects / campaigns 共享同一缓存）。 */
export function useTemplatesList(accountId: string) {
  return useSWR<Row[]>(["templates", accountId], () => api.listTemplates(accountId));
}
