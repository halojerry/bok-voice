// lib/qa-canvas.ts 纯函数单测（qa-canvas Phase 1 Task 4）：steps_json 解析、
// 画布图派生（步骤脊柱列 + 簇星形边 + 确定性布局）、连簇星形校验、布局 localStorage 键。
//
// 与 apiBase.test.mjs 同款装配：api.ts 是带 "use client" 消费方的 TS 源码且
// apps/web 无 JS 测试基建，采用单文件转译路线——tsc CLI 带显式文件入参时忽略
// tsconfig.json（noEmit 不碍事），转译成 CJS 产物后经 createRequire 加载——
// 不引新依赖（typescript 本就在 devDependencies，构建必需），不经 npx
// （直呼 node_modules 内 bin，确定性）。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");

// ---- 装配：lib/qa-canvas.ts → 临时 CJS 产物（模块加载期一次性完成，失败=整文件红）----
const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-qa-canvas-"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(WEB_ROOT, "lib", "qa-canvas.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const qa = require(path.join(TMP_OUT, "qa-canvas.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

const ROWS = [
  { id: "h", question_text: "怎么查物流", answer_text: "在小程序查", lang: "zh", scope: "global", step_index: -1, cluster_head_id: "", enabled: true, hit_count: 5, created_at: "2026-01-01" },
  { id: "v", question_text: "物流咋查", answer_text: "在小程序查", lang: "zh", scope: "step", step_index: 1, cluster_head_id: "h", enabled: true, hit_count: 1, created_at: "2026-01-02" },
  { id: "s", question_text: "多久到", answer_text: "三天内", lang: "zh", scope: "step", step_index: 0, cluster_head_id: "", enabled: false, hit_count: 0, created_at: "2026-01-03" },
];
const STEPS = [{ goal: "开场", ref: "你好" }, { goal: "通知", ref: "抱歉" }];

test("parseTemplateSteps 解析 steps_json", () => {
  const steps = qa.parseTemplateSteps(JSON.stringify(STEPS));
  assert.equal(steps.length, 2);
  assert.equal(qa.parseTemplateSteps("").length, 0);
});

test("deriveGraph 步骤脊柱+簇边+步骤边+顺序线", () => {
  const g = qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} });
  assert.equal(g.stepNodes.filter((n) => n.data.virtual !== true).length, 2);
  const kinds = g.edges.map((e) => e.data.kind).sort();
  assert.deepEqual(kinds, ["cluster", "spine", "step", "step"]); // spine=步骤间顺序线
  const cluster = g.edges.find((e) => e.data.kind === "cluster");
  assert.equal(cluster.source, "v"); assert.equal(cluster.target, "h");
  const spine = g.edges.find((e) => e.data.kind === "spine");
  assert.equal(spine.source, "step:0"); assert.equal(spine.target, "step:1");
  // 布局确定性:同输入两次全同
  assert.deepEqual(qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} }),
                   qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} }));
});

test("deriveGraph 布局 v1.1——节距/簇留白/global 左泳道", () => {
  // A(step0 独立) → B(step0, A 的变体) → C(step0 独立)：簇边界前应有额外留白。
  const rows = [
    { id: "A", question_text: "q1", answer_text: "a", lang: "zh", scope: "step", step_index: 0, cluster_head_id: "", enabled: true },
    { id: "B", question_text: "q2", answer_text: "a", lang: "zh", scope: "step", step_index: 0, cluster_head_id: "A", enabled: true },
    { id: "C", question_text: "q3", answer_text: "a", lang: "zh", scope: "step", step_index: 0, cluster_head_id: "", enabled: true },
  ];
  const g = qa.deriveGraph(rows, STEPS, { langFilter: "all", positions: {} });
  const byId = Object.fromEntries(g.qaNodes.map((n) => [n.id, n.position]));
  assert.equal(byId.B.y - byId.A.y, qa.ENTRY_PITCH_Y);          // 常规节距
  assert.ok(byId.C.y - byId.B.y >= qa.ENTRY_PITCH_Y + qa.CLUSTER_GAP_Y); // 簇边界留白
  // 变体缩进：B 在 A 右侧。
  assert.ok(byId.B.x > byId.A.x);
  // global 条目在脊柱左侧独立泳道。
  const gh = qa.deriveGraph([ROWS[0]], STEPS, { langFilter: "all", positions: {} });
  assert.ok(gh.qaNodes[0].position.x < 0);
});

test("resolveClusterTarget 星形校验", () => {
  assert.equal(qa.resolveClusterTarget(ROWS, "v", "h").ok, true);       // 变体重挂主条目
  assert.equal(qa.resolveClusterTarget(ROWS, "h", "v").ok, false);      // head 不可挂到自己变体
  assert.equal(qa.resolveClusterTarget(ROWS, "h", "h").ok, false);      // 自连
  assert.equal(qa.resolveClusterTarget(ROWS, "s", "v").headId, "h");    // 目标是变体→重定向其 head
});

test("resolveClusterTarget 已带变体的主条目作 source 整类拒绝（防两层链）", () => {
  // h 已带变体 v：H1→H2（重挂别的 head）会令 V1→H1→H2 两层链，spec §4.4 一层星形不变量。
  const R2 = [
    ...ROWS,
    { id: "h2", question_text: "退款多久", answer_text: "七个工作日", lang: "zh", scope: "global", step_index: -1, cluster_head_id: "", enabled: true, hit_count: 0, created_at: "2026-01-04" },
  ];
  assert.equal(qa.resolveClusterTarget(R2, "h", "h2").ok, false);   // H1→H2
  assert.equal(qa.resolveClusterTarget(R2, "h", "v").ok, false);    // H1→自家变体（同族方向）
  assert.notEqual(qa.resolveClusterTarget(R2, "h", "h2").reason, "");
  // 不带变体的条目仍可作 source（回归锚）。
  assert.equal(qa.resolveClusterTarget(R2, "h2", "v").ok, true);
});

test("deriveGraph col0 与 col1 条目位置不重合（global 左泳道 vs 卫星右道）", () => {
  // 曾有缺陷：col0（global）与 col1（step 0）条目 x、y 全同像素级重叠。
  // v1.1 后 global 泳道在脊柱左侧（负 x），卫星道在右侧（+SAT_X）。
  const g = qa.deriveGraph(ROWS, STEPS, { langFilter: "all", positions: {} });
  const byId = Object.fromEntries(g.qaNodes.map((n) => [n.id, n.position]));
  assert.notDeepEqual(byId.h, byId.s);        // h=global 泳道, s=step0 泳道
  assert.ok(byId.h.x < 0);                    // global 甩到脊柱左侧
  assert.ok(byId.s.x > 0);                    // 卫星在脊柱右侧走廊
});

test("revertCluster 回滚断簇", () => {
  const rows = [{ id: "v", cluster_head_id: "h2" }];
  assert.equal(qa.revertCluster(rows, "v", "h1")[0].cluster_head_id, "h1");
});

test("LOCAL_POS_KEY 形态", () => {
  assert.equal(qa.LOCAL_POS_KEY("acc-001", "tpl-1"), "qa-canvas-pos:acc-001:tpl-1");
});
