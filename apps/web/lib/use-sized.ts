"use client";

// 容器尺寸就绪 hook（2026-09-26，React Flow error#004/#015 修法）：
// React Flow 挂载瞬间若父容器宽高任一为 0（tab 切换/首帧布局未定型/key 重挂竞态），
// 会打「parent container needs a width and height」（#004）且节点量不到尺寸——紧接着
// 拖拽再打「drag a node that is not initialized」（#015）。官方推荐姿势=容器量到
// 非零尺寸后才渲染 <ReactFlow>；量到即放行并断开观察（后续不再产生 RO 回调——
// 不引入新的布局反馈循环噪音）。
//
// 实现=回调 ref + node 状态（2026-09-26 二修，真栈实弹）：元素被 React 换节点
// （重挂/结构变化）时，旧实现闭包 `el` 变尸体——RO 盯着已 detach 的节点永不回调，
// 占位符「画布加载中」卡死（直链进画布视图实测）。回调 ref 把当前节点写进 state，
// effect 以 node 为依赖：换节点即拆旧 RO、对新节点重闸，永远盯活元素。
import { useCallback, useEffect, useState } from "react";

export function useSized<T extends HTMLElement>() {
  const [node, setNode] = useState<T | null>(null);
  const [ready, setReady] = useState(false);
  const ref = useCallback((el: T | null) => {
    setNode(el);
    if (!el) setReady(false);
  }, []);

  useEffect(() => {
    if (!node) return;
    if (node.clientWidth > 0 && node.clientHeight > 0) {
      setReady(true);
      return;
    }
    let ro: ResizeObserver | null = null;
    try {
      ro = new ResizeObserver(() => {
        if (node.clientWidth > 0 && node.clientHeight > 0) {
          setReady(true);
          ro?.disconnect();
        }
      });
      ro.observe(node);
    } catch {
      // RO 不可用（极老浏览器）：直接放行，退回旧行为（至多重现 dev 警告，不阻塞功能）。
      setReady(true);
    }
    return () => ro?.disconnect();
  }, [node]);

  return { ref, ready };
}
