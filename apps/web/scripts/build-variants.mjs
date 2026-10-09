#!/usr/bin/env node
/**
 * 双构建编排（W④ 2026-10-09）：customer（SaaS 交付档，bundle 零厂商字面量）
 * 与 platform（平台控制台，全功能真身）两个静态产物。
 *
 * 仓库默认态=真身在 app/(app)/settings·disaster（dev/local 形态零摩擦）。
 * - `build`（customer）：备份真页 → 换 platform-stubs 桩页 → next build →
 *   产物改名 out → customer-out → 还原真页。桩页零厂商字面量/零机密 import，
 *   lib/settings-meta.ts 与 components/settings-model-routing.tsx 仅被真页
 *   import，随换桩一起脱离引用面（DCE + 无入口=不进客户 bundle）。
 * - `build:platform`：直接 next build（真身照旧）→ 产物改名 platform-out。
 *
 * 防呆：换桩前若 .next-variant-backup 已存在即中止（上次异常中断未还原）。
 */
import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const BACKUP = path.join(ROOT, ".next-variant-backup");

const SWAPS = [
  {
    real: "app/(app)/settings/page.tsx",
    stub: "platform-stubs/settings-page.tsx",
  },
  {
    real: "app/(app)/disaster/page.tsx",
    stub: "platform-stubs/disaster-page.tsx",
  },
];

/** 备份键=路径斜线折叠（防 basename 撞名——settings/disaster 两页都叫
 * page.tsx，同名备份会互相覆盖、restore 错配（2026-10-09 实弹翻车钉死）。 */
function backupName(real) {
  return real.replace(/[\\/]+/g, "__");
}

function swapInStubs() {
  if (fs.existsSync(BACKUP)) {
    die(`残留备份 ${BACKUP}（上次构建异常中断未还原）——先 npm run build:restore`);
  }
  fs.mkdirSync(BACKUP, { recursive: true });
  for (const { real, stub } of SWAPS) {
    const realPath = path.join(ROOT, real);
    if (!fs.existsSync(realPath)) die(`真身缺失: ${real}`);
    fs.copyFileSync(realPath, path.join(BACKUP, backupName(real)));
    fs.copyFileSync(path.join(ROOT, stub), realPath);
  }
  console.log("[build-variants] 桩页已换装（真身备份于 .next-variant-backup/）");
}

function restore() {
  if (!fs.existsSync(BACKUP)) {
    console.log("[build-variants] 无备份需要还原");
    return;
  }
  for (const { real } of SWAPS) {
    const bak = path.join(BACKUP, backupName(real));
    if (fs.existsSync(bak)) fs.copyFileSync(bak, path.join(ROOT, real));
  }
  fs.rmSync(BACKUP, { recursive: true, force: true });
  console.log("[build-variants] 真身已还原");
}

function build(outDirName, env) {
  execSync("npx next build", { stdio: "inherit", cwd: ROOT, env: { ...process.env, ...env } });
  if (!outDirName) {
    // 客户档=交付主产物，保持 next 原生 out/（CI probe_thin_client_static、
    // CP BOK_WEB_STATIC_DIR、node_agent :3000 托管全部吃这个路径，零改）。
    console.log("[build-variants] 产物落 out/（客户档=交付主产物）");
    return;
  }
  const out = path.join(ROOT, "out");
  const dest = path.join(ROOT, outDirName);
  if (fs.existsSync(dest)) fs.rmSync(dest, { recursive: true, force: true });
  // 复制不挪窝（CI 实弹翻车钉死：rename 会把 out/ 抢走，后续 thin-client
  // 探针/产物 grep 扑空）——platform-out 是对照档，out/ 原位保留。
  fs.cpSync(out, dest, { recursive: true });
  console.log(`[build-variants] 产物复制到 ${outDirName}/（out/ 原位保留）`);
}

const mode = process.argv[2] || "customer";
if (mode === "restore") {
  restore();
} else if (mode === "platform") {
  build("platform-out", { NEXT_PUBLIC_PLATFORM_CONSOLE: "1" });
} else if (mode === "customer") {
  try {
    swapInStubs();
    build("", { NEXT_PUBLIC_PLATFORM_CONSOLE: "0" });
  } finally {
    restore();
  }
} else {
  die(`未知模式 ${mode}（customer | platform | restore）`);
}
