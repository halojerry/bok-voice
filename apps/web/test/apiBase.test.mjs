// apiBase() 三档解析契约（node --test 单测，任务 site-delivery Task 3）：
//   节点注入 window.__BOK_CONFIG__.cpUrl > 构建期 env NEXT_PUBLIC_CONTROL_PLANE_URL
//   > 默认 http://127.0.0.1:8000。
// 瘦客户端铁律：节点本地托管形态的 CP 地址必须在运行时可注入——任何一档写死
// localhost 即失效。本测试钉住 api.ts 的实际分支（?: 真值判断），不虚构行为。
//
// api.ts 是带 "use client" 消费方的 TS 源码且 apps/web 无 JS 测试基建，采用
// 单文件转译路线：tsc CLI 带显式文件入参时忽略 tsconfig.json（noEmit 不碍事），
// 转译成 CJS 产物后经 createRequire 加载——不引新依赖（typescript 本就在
// devDependencies，构建必需），不经 npx（直呼 node_modules 内 bin，确定性）。

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

// ---- 装配：lib/api.ts → 临时 CJS 产物（模块加载期一次性完成，失败=整文件红）----
const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-apibase-"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(WEB_ROOT, "lib", "api.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const { apiBase } = require(path.join(TMP_OUT, "api.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

// ---- 每例隔离：清 env、摘 window（Node 本无 window，测试内按例挂载）----
const ENV_KEY = "NEXT_PUBLIC_CONTROL_PLANE_URL";

test.beforeEach(() => {
  delete process.env[ENV_KEY];
  delete globalThis.window;
});

test.afterEach(() => {
  delete process.env[ENV_KEY];
  delete globalThis.window;
});

test("节点注入的 runtime-config cpUrl 优先于构建期 env", () => {
  process.env[ENV_KEY] = "http://build-time-cp:8000";
  // 与 tools/node_agent.py write_ui_config 注入后同形（含 livekitUrl）。
  globalThis.window = {
    __BOK_CONFIG__: { cpUrl: "http://192.168.8.9:8000", livekitUrl: "ws://192.168.8.9:7880" },
  };
  assert.equal(apiBase(), "http://192.168.8.9:8000");
});

test("无 runtime 配置（无 window）时用构建期 env", () => {
  process.env[ENV_KEY] = "https://cp.example.com";
  assert.equal(apiBase(), "https://cp.example.com");
});

test("window 在场但 __BOK_CONFIG__ 为空对象（出厂 stub 形态）时用构建期 env", () => {
  // public/runtime-config.js 的云端托管形态：window.__BOK_CONFIG__ = {} || {}
  process.env[ENV_KEY] = "https://cp.example.com";
  globalThis.window = { __BOK_CONFIG__: {} };
  assert.equal(apiBase(), "https://cp.example.com");
});

test("runtime cpUrl 为空串时按真值判断回落 env（源码分支）", () => {
  process.env[ENV_KEY] = "https://cp.example.com";
  globalThis.window = { __BOK_CONFIG__: { cpUrl: "" } };
  assert.equal(apiBase(), "https://cp.example.com");
});

test("env 与 runtime 配置双缺省回落 127.0.0.1:8000", () => {
  assert.equal(apiBase(), "http://127.0.0.1:8000");
});

test("window 在场、env 缺省、cpUrl 空串 → 仍回落 127.0.0.1:8000", () => {
  globalThis.window = { __BOK_CONFIG__: { cpUrl: "" } };
  assert.equal(apiBase(), "http://127.0.0.1:8000");
});
