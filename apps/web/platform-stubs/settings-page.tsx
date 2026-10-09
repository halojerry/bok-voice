"use client";

// 客户构建档 /settings 桩页（W④ 2026-10-09 双构建）。
// 真身=platform-console/settings/page.tsx（引擎/模型路由/克隆/诊断等平台机密面，
// 含厂商名与端点提示文案）——仅打进平台构建（scripts/build-variants.mjs 换装）。
// 本桩在客户 bundle 中替代真身：零厂商字面量、零平台机密 import。

import { Settings } from "lucide-react";

export default function SettingsStubPage() {
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col items-center gap-4 p-12 text-center">
      <Settings className="size-10 muted" />
      <h1 className="text-lg font-semibold">平台设置</h1>
      <p className="muted max-w-md text-sm leading-relaxed">
        引擎与平台配置由服务方统一管理。如需调整语音引擎、模型或集成配置，
        请联系平台管理员。
      </p>
    </div>
  );
}
