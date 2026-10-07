// lib/device-roles.ts 纯函数单测（2026-10-08 虚拟设备票，call-933945a5 实证）：
//  · isVirtualAudioDevice：BlackHole/Oray/(Virtual) 后缀/Soundflower/Loopback 族命中，
//    真实设备（AirPods/AB13X USB/Mac mini 扬声器/外置麦克风）零误伤；
//  · deviceRoleIssues：虚拟设备进任一角色槽=warn（不拦死，用户可能刻意路由实验）；
//    同麦 fatal 语义不变（该通的原始病灶之一，不回退）；
//  · 装配 pin：interpret-console 的 realMic/realOut 过滤必须带 isVirtualAudioDevice
//    ——auto-assign 挑中 BlackHole 的回归防线（call-933945a5：AirPods 失联 → stale
//    回落默认与对方同麦 → auto-assign 按枚举序把 BlackHole 塞给我方麦）。
//
// 装配照 intent-table.test.mjs：lib/device-roles.ts → tsc → CJS → require。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");

const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-device-roles-"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(WEB_ROOT, "lib", "device-roles.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const { isVirtualAudioDevice, deviceRoleIssues } = require(path.join(TMP_OUT, "device-roles.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

// call-933945a5 web-client.log 的真实枚举表（16:53:00 devices 事件原样）。
const MIC_NAMES_933945A5 = [
  "外置麦克风 (Built-in)",
  "BlackHole 2ch (Virtual)",
  "OrayVirtualAudioDevice (Virtual)",
  "默认 - 外置麦克风 (Built-in)",
  "AB13X USB Audio (12d1:3a07)",
];

test("isVirtualAudioDevice：虚拟/回环族命中（该通实测设备名）", () => {
  assert.equal(isVirtualAudioDevice("BlackHole 2ch (Virtual)"), true);
  assert.equal(isVirtualAudioDevice("OrayVirtualAudioDevice (Virtual)"), true);
  assert.equal(isVirtualAudioDevice("BlackHole 64ch"), true);
  assert.equal(isVirtualAudioDevice("Soundflower (2ch)"), true);
  assert.equal(isVirtualAudioDevice("Loopback Audio"), true);
  assert.equal(isVirtualAudioDevice("VB-Audio Virtual Cable"), true);
  assert.equal(isVirtualAudioDevice("Voicemeeter Input"), true);
});

test("isVirtualAudioDevice：真实设备零误伤（含该通全部真实件）", () => {
  for (const n of ["AirPods (Bluetooth)", "AB13X USB Audio (12d1:3a07)", "外置麦克风 (Built-in)", "Mac mini扬声器 (Built-in)", "外置耳机 (Built-in)"]) {
    assert.equal(isVirtualAudioDevice(n), false, n);
  }
  assert.equal(isVirtualAudioDevice(""), false);
});

test("auto-assign 候选还原：该通枚举表过滤后只剩真实麦（BlackHole/Oray 出局）", () => {
  const real = MIC_NAMES_933945A5.filter((n) => !n.startsWith("默认") && !isVirtualAudioDevice(n));
  assert.deepEqual(real, ["外置麦克风 (Built-in)", "AB13X USB Audio (12d1:3a07)"]);
  // 对方麦已占 外置麦克风 → 我方麦补位=AB13X(真实件)，不再轮到 BlackHole
  const oth = "外置麦克风 (Built-in)";
  const me = real.find((n) => n !== oth);
  assert.equal(me, "AB13X USB Audio (12d1:3a07)");
});

test("deviceRoleIssues：虚拟设备进角色槽=warn（不拦死）；同麦 fatal 语义不变", () => {
  const r = deviceRoleIssues([
    { role: "meMic", label: "我方麦克风", name: "BlackHole 2ch (Virtual)", id: "idA", groupId: "gA" },
    { role: "othMic", label: "对方麦克风", name: "外置麦克风 (Built-in)", id: "idB", groupId: "gB" },
  ]);
  assert.equal(r.fatal.length, 0);
  assert.equal(r.warn.length, 1);
  assert.match(r.warn[0], /虚拟音频设备/);
  // 同一支物理麦挂两个角色（同 id+groupId，call-933945a5 的第一病灶形态）= fatal 原语义
  const f = deviceRoleIssues([
    { role: "meMic", label: "我方麦克风", name: "外置麦克风 (Built-in)", id: "idB", groupId: "gB" },
    { role: "othMic", label: "对方麦克风", name: "外置麦克风 (Built-in)", id: "idB", groupId: "gB" },
  ]);
  assert.equal(f.fatal.length, 1);
  assert.match(f.fatal[0], /两侧麦克风是同一支/);
});

test("装配 pin：interpret-console realMic/realOut 过滤带 isVirtualAudioDevice", () => {
  const src = readFileSync(path.join(WEB_ROOT, "components", "interpret-console.tsx"), "utf8");
  assert.match(src, /realMic = mics\.filter\(\(d\) => !d\.is_default && !isVirtualAudioDevice\(d\.name\)\)/);
  assert.match(src, /realOut = outs\.filter\(\(d\) => !d\.is_default && !isVirtualAudioDevice\(d\.name\)\)/);
  assert.match(src, /isVirtualAudioDevice,\s*\n\s*scriptMismatch/);
});

test("装配 pin：saved 存量虚拟毒值清洗（call-e1cd7550——#211 只挡新分配没清存量）", () => {
  const src = readFileSync(path.join(WEB_ROOT, "components", "interpret-console.tsx"), "utf8");
  // 麦侧：saved 命中虚拟设备并入 badMe/badOth（stale 同款清理路径）
  assert.match(src, /const badMe = micStale\(savedMicDevice\("me"\)\) \|\| micVirtual\(savedMicDevice\("me"\)\);/);
  assert.match(src, /const curMe = badMe \? "" : savedMicDevice\("me"\);/);
  // 输出侧：saved 虚拟清洗 + wlog 可观测
  assert.match(src, /wlog\("out_virtual_reset"/);
  assert.match(src, /saveOutputDevice\("", "me"\)/);
});

test("装配 pin：OverconstrainedError 毒 id 清理（清 saved + 房间 exact 约束回默认）", () => {
  const src = readFileSync(path.join(WEB_ROOT, "components", "interpret-console.tsx"), "utf8");
  // me 侧连接错误分支：overconstrained → 清 saved + setMeMicId("") + 房间回默认
  assert.match(src, /meRoom\.switchActiveDevice\("audioinput", ""\)/);
  assert.match(src, /room\.switchActiveDevice\("audioinput", ""\)/);
  assert.match(src, /otherRoomRef\.current\?\.switchActiveDevice\("audioinput", ""\)/);
  assert.match(src, /已清掉记住的设备/);
});
