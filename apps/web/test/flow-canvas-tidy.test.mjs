// components/flow-canvas.tsx 一键整理（dagre 自动布局）纯函数单测（2026-09-25）：
// tidyPositions 产出显示层覆盖位置——步骤=脊柱链按步号逐级向下（rankdir TB 由 spine
// 边决定）;意图卡=贴近 jump 目标步;确定性（同输入同输出）;全节点覆盖（含空图/无边单节点）。
// estimateNodeSize：盒估计恒为正、step 宽=STEP_NODE_W、intent 宽=INTENT_NODE_W。
//
// 装配照 flow-canvas.test.mjs 装配二（template-editor）同款：组件带 "use client" 与
// react/@xyflow/dagre 依赖,noResolve 单文件转译（类型错误不阻断 emit）+
// Module._resolveFilename 钩子——"@/lib/flow-canvas" 重定向到**真实转译产物**
// （tidyPositions 消费的 FlowNode/FlowEdge 形状必须与生产布局同源,不许桩）,
// 其余 "@/..." 与 css 桩掉;react/@xyflow/react/@dagrejs/dagre 沿目录树解析到
// apps/web/node_modules 真包（三包 CJS require 均 node 可载）。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import Module from "node:module";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");
const TSC = path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc");

// ---- 装配一：lib/flow-canvas.ts（真实布局纯函数,tidy 的输入形状来源） ----
const TMP_FC = mkdtempSync(path.join(WEB_ROOT, ".tmp-flow-canvas-"));
execFileSync(
  process.execPath,
  [TSC, path.join(WEB_ROOT, "lib", "flow-canvas.ts"), "--outDir", TMP_FC, "--module", "commonjs", "--target", "es2020", "--skipLibCheck"],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const fc = require(path.join(TMP_FC, "flow-canvas.js"));

// ---- 装配二：components/flow-canvas.tsx（真实生产代码,@/ 依赖按需重定向） ----
const TMP_FCX = mkdtempSync(path.join(WEB_ROOT, ".tmp-flow-canvas-tsx-"));
writeFileSync(
  path.join(TMP_FCX, "stub.js"),
  [
    "// 测试桩：组件的模块级导入面里只桩 UI/会话依赖（useSession/VarTextarea/css）,",
    "// tidyPositions/estimateNodeSize 不经它们。",
    "exports.useSession = () => null;",
    "exports.VarTextarea = () => null;",
    "",
  ].join("\n"),
);
// noResolve：不解析 import——错误不阻断 emit（tsc 对外部包签名缺退出码 2,spawnSync 不抛,
// 只看产物存在与否）,产物里的 require 在装载期被钩子重定向。
spawnSync(
  process.execPath,
  [
    TSC, path.join(WEB_ROOT, "components", "flow-canvas.tsx"),
    "--outDir", TMP_FCX, "--module", "commonjs", "--target", "es2020",
    "--skipLibCheck", "--noResolve", "--jsx", "react-jsx",
  ],
  { stdio: "ignore" },
);
const fcxJs = path.join(TMP_FCX, "flow-canvas.js");
assert.ok(
  (() => { try { return require("node:fs").readFileSync(fcxJs).length > 0; } catch { return false; } })(),
  "flow-canvas.tsx 转译产物缺失——测试装配失败",
);
const resolveFilename = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  const req = String(request);
  if (req === "@/lib/flow-canvas") {
    // 真实 lib：tidy 消费的节点/边形状与生产 layoutFlow 同源。
    return resolveFilename.call(this, path.join(TMP_FC, "flow-canvas.js"), ...rest);
  }
  if (req.startsWith("@/") || req.endsWith(".css")) {
    return resolveFilename.call(this, path.join(TMP_FCX, "stub.js"), ...rest);
  }
  return resolveFilename.call(this, request, ...rest);
};
const fcx = require(fcxJs);

test.after(() => {
  rmSync(TMP_FC, { recursive: true, force: true });
  rmSync(TMP_FCX, { recursive: true, force: true });
});

// 步骤 4 步（含分支/直念/场景）+ 2 个意图（其一 jump 绑定）——tidy 的典型输入。
const STEPS = [
  { goal: "开场", ref: "你好，请问係{姓名}？\n如果客户唔记得 → 提佢单里的地址", scene: "开场" },
  { goal: "通知", ref: "同你讲声唔好意思。", say: true },
  { goal: "谈赔", ref: "会按一赔二赔俾你。\n如果客户问点解 → 讲係运输遗失我方全责\n如果客户担心 → 讲直接落微信零钱" },
  { goal: "收尾", ref: "唔该晒你时间,拜拜！" },
];
const GRAPH = {
  version: 1,
  intents: [
    { id: "int_a", label: "投诉", keywords: ["投诉"], steps: [], enabled: true },
    { id: "int_b", label: "退款", keywords: ["退款"], steps: [], enabled: true },
  ],
  bindings: [
    { id: "bnd_1", intent: "int_a", action: "jump_step", step: 3, enabled: true },
    { id: "bnd_2", intent: "int_b", action: "jump_step", step: 4, enabled: true },
  ],
};

test("tidyPositions：步骤按步号纵向递增（spine 链决定 rank 序）,意图在场且不与步骤同位", () => {
  const { nodes, edges } = fc.layoutFlow(STEPS, GRAPH);
  const pos = fcx.tidyPositions(nodes, edges);

  // 全节点覆盖。
  assert.deepEqual(Object.keys(pos).sort(), nodes.map((n) => n.id).sort());

  // 步号纵向递增：fstep:0 在最上,fstep:3 在最下（「按步号纵向」硬判据）。
  const ys = [0, 1, 2, 3].map((i) => pos[`fstep:${i}`].y);
  for (let i = 1; i < ys.length; i++) {
    assert.ok(ys[i] > ys[i - 1], `fstep:${i} 应排在 fstep:${i - 1} 下方 (y ${ys[i]} <= ${ys[i - 1]})`);
  }
  // 脊柱同列不必强求（dagre 按边最短化排 x）,但横移幅度有限——步间水平距离不超 1.5 卡宽。
  const xs = [0, 1, 2, 3].map((i) => pos[`fstep:${i}`].x);
  assert.ok(Math.max(...xs) - Math.min(...xs) < 1.5 * fc.STEP_NODE_W, "步骤横向散开过大");

  // 意图卡在场、坐标有限（dagre 产出换算左上角后仍为有限数）。
  for (const id of ["fintent:int_a", "fintent:int_b"]) {
    assert.ok(Number.isFinite(pos[id].x) && Number.isFinite(pos[id].y), `${id} 坐标应有限`);
  }
  // 两个意图与四个步骤六盒互不重叠（粗盒估计的容差内——同一中心距 < 半宽和才算撞）。
  const boxes = nodes.map((n) => {
    const size = fcx.estimateNodeSize(n);
    return { id: n.id, x: pos[n.id].x, y: pos[n.id].y, w: size.width, h: size.height };
  });
  for (let a = 0; a < boxes.length; a++) {
    for (let b = a + 1; b < boxes.length; b++) {
      const A = boxes[a], B = boxes[b];
      const overlapX = Math.abs(A.x - B.x) < (A.w + B.w) / 2 - 8;
      const overlapY = Math.abs(A.y - B.y) < (A.h + B.h) / 2 - 8;
      assert.ok(!(overlapX && overlapY), `节点 ${A.id} 与 ${B.id} 在整理位上重叠`);
    }
  }
});

test("tidyPositions：确定性（同输入同输出）+ 空图/无边单节点/纯意图图不炸", () => {
  const { nodes, edges } = fc.layoutFlow(STEPS, GRAPH);
  const p1 = fcx.tidyPositions(nodes, edges);
  const p2 = fcx.tidyPositions(nodes, edges);
  assert.deepEqual(p1, p2);

  // 空图：无节点无边 → 空覆盖表。
  assert.deepEqual(fcx.tidyPositions([], []), {});

  // 无边单节点（单步模板）：dagre 自环无,产出有限坐标。
  const bare = fc.layoutFlow([{ goal: "s", ref: "只有一步" }]);
  const solo = fcx.tidyPositions(bare.nodes, bare.edges);
  assert.equal(Object.keys(solo).length, 1);
  assert.ok(Number.isFinite(solo["fstep:0"].x) && Number.isFinite(solo["fstep:0"].y));

  // 纯意图图（空步骤+意图绑定悬空钳到 0）：不炸、坐标有限。
  const intentOnly = fc.layoutFlow([], GRAPH);
  const ip = fcx.tidyPositions(intentOnly.nodes, intentOnly.edges);
  assert.equal(Object.keys(ip).length, intentOnly.nodes.length);
  for (const v of Object.values(ip)) {
    assert.ok(Number.isFinite(v.x) && Number.isFinite(v.y));
  }
});

test("estimateNodeSize：step 宽=STEP_NODE_W、intent 宽=INTENT_NODE_W、盒恒为正且分支越多越高", () => {
  const { nodes } = fc.layoutFlow(STEPS, GRAPH);
  for (const n of nodes) {
    const s = fcx.estimateNodeSize(n);
    assert.ok(s.width > 0 && s.height > 0, `${n.id} 盒应为正`);
    if (n.kind === "step") assert.equal(s.width, fc.STEP_NODE_W);
    else assert.equal(s.width, fc.INTENT_NODE_W);
  }
  // 分支行越多盒越高（谈赔步 2 分支 > 通知步 0 分支）。
  const talk = fcx.estimateNodeSize(nodes.find((n) => n.id === "fstep:2"));
  const tell = fcx.estimateNodeSize(nodes.find((n) => n.id === "fstep:1"));
  assert.ok(talk.height > tell.height, "有分支的步骤卡估值应更高");
});
