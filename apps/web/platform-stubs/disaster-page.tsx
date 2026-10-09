"use client";

// 客户构建档 /disaster 桩页（W④ 2026-10-09 双构建）。
// 真身=platform-console/disaster/page.tsx（容灾处置面：服务器清单/端口/饥荒态，
// root 专属）。仅打进平台构建；本桩零机密信息。

import { ShieldAlert } from "lucide-react";

export default function DisasterStubPage() {
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col items-center gap-4 p-12 text-center">
      <ShieldAlert className="size-10 muted" />
      <h1 className="text-lg font-semibold">平台运维</h1>
      <p className="muted max-w-md text-sm leading-relaxed">
        容灾与运维处置由服务方平台台统一执行。如遇服务异常，请联系平台管理员。
      </p>
    </div>
  );
}
