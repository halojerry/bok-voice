// lib/dirty-signal.ts 纯逻辑单测（2026-10-08 侧栏 dirty 拦截刀）：
//  · 聚合语义：任一源 dirty=全局 dirty；全部清零=干净；
//  · 幂等：同 key 同值重复上报只广播一次（多实例/重复接线不漂移）；
//  · 订阅：订阅即收到当前值、退订后不再收；
//  · 卸载守卫：dirty 时 beforeunload 拦（preventDefault+returnValue）、干净时放行、
//    卸载函数摘掉同一监听。
//
// 装配照 intent-table.test.mjs：lib/dirty-signal.ts 零 import，单文件 tsc 转译到
// 临时目录，createRequire 加载即可（零新依赖）。window 只在守卫函数体内触碰，
// 单测里用 globalThis.window 桩替身。

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

const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-dirty-signal-"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(WEB_ROOT, "lib", "dirty-signal.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const ds = require(path.join(TMP_OUT, "dirty-signal.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

test("聚合：任一源 dirty=全局 dirty；全部清零=干净", () => {
  const k1 = ds.nextDirtyKey("editor");
  const k2 = ds.nextDirtyKey("editor");
  assert.notEqual(k1, k2, "实例键唯一（同组件多实例防串）");
  assert.equal(ds.isGlobalDirty(), false);
  ds.setDirty(k1, true);
  assert.equal(ds.isGlobalDirty(), true);
  ds.setDirty(k2, true);
  assert.equal(ds.isGlobalDirty(), true);
  ds.setDirty(k1, false);
  assert.equal(ds.isGlobalDirty(), true, "k2 还 dirty，全局仍 dirty");
  ds.setDirty(k2, false);
  assert.equal(ds.isGlobalDirty(), false);
});

test("幂等：同值重复上报只广播一次", () => {
  const k = ds.nextDirtyKey("editor");
  const seen = [];
  const off = ds.subscribeDirty((d) => seen.push(d));
  assert.deepEqual(seen, [false], "订阅即以当前值回调一次");
  ds.setDirty(k, true);
  ds.setDirty(k, true);
  ds.setDirty(k, true);
  assert.deepEqual(seen, [false, true], "同值重复上报不重复广播");
  ds.setDirty(k, false);
  ds.setDirty(k, false);
  assert.deepEqual(seen, [false, true, false]);
  off();
  ds.setDirty(k, true);
  assert.deepEqual(seen, [false, true, false], "退订后不再收");
  ds.setDirty(k, false); // 收尾归零：模块态是单例，别把 dirty 串给后续测试
});

test("卸载守卫：dirty 拦、干净放行、卸载摘同一监听", () => {
  const added = [];
  const removed = [];
  globalThis.window = {
    addEventListener: (type, fn) => added.push([type, fn]),
    removeEventListener: (type, fn) => removed.push([type, fn]),
  };
  try {
    const k = ds.nextDirtyKey("editor");
    ds.setDirty(k, false); // 归零起点（前序测试的模块态不串场）
    const uninstall = ds.installDirtyUnloadGuard();
    assert.equal(added.length, 1);
    assert.equal(added[0][0], "beforeunload");
    const handler = added[0][1];
    const evt = {
      calls: 0,
      returnValue: undefined,
      preventDefault() {
        this.calls += 1;
      },
    };
    ds.setDirty(k, true);
    handler(evt);
    assert.equal(evt.calls, 1, "dirty 时拦");
    assert.equal(evt.returnValue, "");
    ds.setDirty(k, false);
    handler(evt);
    assert.equal(evt.calls, 1, "干净时放行（仅 dirty 生效）");
    uninstall();
    assert.deepEqual(removed, added, "卸载摘掉同一监听");
  } finally {
    delete globalThis.window;
  }
});
