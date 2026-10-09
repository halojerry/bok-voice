/**
 * 全局未保存（dirty）信号（2026-10-08 侧栏拦截刀）：app-shell 级离开拦截的数据源。
 *
 * 背景：编辑器的 dirty 快照防线原来是孤岛——组件私有（TemplateEditor 的
 * onDirtyChange 快照对比）/页面私有（studio 步骤草稿 stepsDirty），侧栏/顶栏的
 * 站内 Link 点击不拦、直接导航丢改动（AGENTS.md 2026-10-02 web UX 三刀遗留）。
 * 本模块把孤岛状态汇成一份全局布尔：编辑器进入 dirty 时上报 true、保存/重置/
 * 卸载时上报 false；壳层消费点在事件时刻命令式查询——
 *  · Sidebar/Topbar 站内 Link onClick → guardDirtyNavClick（confirm 拦截）；
 *  · beforeunload（关标签/刷新）→ installDirtyUnloadGuard（app-shell 挂一次）。
 *
 * 选型：模块级 Map（key=编辑器实例）+ 订阅者集合——不引状态库（与 lib/ 纯函数
 * 模块同口味）；同组件多实例用 nextDirtyKey 取唯一键防互相覆盖，同 key 同值
 * 上报幂等。纯逻辑零 DOM（window 只在 guardDirtyNavClick/installDirtyUnloadGuard
 * 函数体内触碰），node 单测可直跑（test/dirty-signal.test.mjs）。
 */

export type DirtyListener = (dirty: boolean) => void;

/** 各编辑器实例上报的 dirty 状态；聚合语义=任一为 true。 */
const sources = new Map<string, boolean>();
const listeners = new Set<DirtyListener>();
/** 上次对外广播的聚合值（null=尚未广播过；同值不重复广播）。 */
let lastEmitted: boolean | null = null;

function aggregate(): boolean {
  for (const dirty of sources.values()) {
    if (dirty) return true;
  }
  return false;
}

function emit(): void {
  const dirty = aggregate();
  if (lastEmitted === dirty) return;
  lastEmitted = dirty;
  for (const listener of listeners) listener(dirty);
}

/** 编辑器实例键：自增序号，防同组件多实例共用一个键互相覆盖上报。 */
let keySeq = 0;
export function nextDirtyKey(prefix: string): string {
  keySeq += 1;
  return `${prefix}#${keySeq}`;
}

/** 上报某编辑器实例的 dirty 变化（同值幂等；保存/重置/卸载须报 false）。 */
export function setDirty(key: string, dirty: boolean): void {
  if (sources.get(key) === dirty) return;
  sources.set(key, dirty);
  emit();
}

/** 当前全局 dirty（命令式查询：Link onClick / beforeunload 事件时刻用）。 */
export function isGlobalDirty(): boolean {
  return aggregate();
}

/** 订阅全局 dirty 变化（订阅即以当前值回调一次）；返回退订函数。 */
export function subscribeDirty(listener: DirtyListener): () => void {
  listeners.add(listener);
  listener(aggregate());
  return () => {
    listeners.delete(listener);
  };
}

/** 站内 Link onClick 守卫：全局 dirty 时原生 confirm，取消=preventDefault 停留。
 *  修饰键/中键点击（新开标签，本页不走导航）不拦。参数按结构类型收窄，
 *  React 的 MouseEvent 结构兼容——本模块不 import React。 */
export function guardDirtyNavClick(e: {
  preventDefault(): void;
  button?: number;
  metaKey?: boolean;
  ctrlKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
}): void {
  if ((e.button ?? 0) !== 0) return;
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  if (!isGlobalDirty()) return;
  if (!window.confirm("当前页面有未保存的修改，离开会丢失。确定离开？")) {
    e.preventDefault();
  }
}

/** 关标签/刷新守卫：常驻监听、事件时刻查全局 dirty（仅 dirty 时拦，干净即放行）。
 *  app-shell mount 挂一次、返回卸载函数（壳卸载即摘）。与 TemplateEditor/studio
 *  页自带的 beforeunload 并存无害——浏览器对多 handler 只弹一次原生确认框。 */
export function installDirtyUnloadGuard(): () => void {
  const onBeforeUnload = (e: BeforeUnloadEvent) => {
    if (!isGlobalDirty()) return;
    e.preventDefault();
    e.returnValue = "";
  };
  window.addEventListener("beforeunload", onBeforeUnload);
  return () => window.removeEventListener("beforeunload", onBeforeUnload);
}
