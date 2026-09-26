"use client";

// 容器尺寸就绪 hook（2026-09-26，React Flow error#004/#015 修法）：
// React Flow 挂载瞬间若父容器宽高任一为 0（tab 切换/首帧布局未定型/key 重挂竞态），
// 会打「parent container needs a width and height」（#004）且节点量不到尺寸——紧接着
// 拖拽再打「drag a node that is not initialized」（#015）。官方推荐姿势=容器量到
// 非零尺寸后才渲染 <ReactFlow>；本 hook 用 ResizeObserver 一次性守门，量到即放行
// 并断开观察（后续不再产生 RO 回调——不引入新的布局反馈循环噪音）。
import { useEffect, useRef, useState } from "react";

export function useSized<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (el.clientWidth > 0 && el.clientHeight > 0) {
      setReady(true);
      return;
    }
    let ro: ResizeObserver | null = null;
    try {
      ro = new ResizeObserver(() => {
        if (el.clientWidth > 0 && el.clientHeight > 0) {
          setReady(true);
          ro?.disconnect();
        }
      });
      ro.observe(el);
    } catch {
      // RO 不可用（极老浏览器）：直接放行，退回旧行为（至多重现 dev 警告，不阻塞功能）。
      setReady(true);
    }
    return () => ro?.disconnect();
  }, []);
  return { ref, ready };
}
