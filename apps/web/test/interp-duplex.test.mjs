// W8-A3(2026-10-09, Ethan 拍板全双工拓扑) 闭麦闸缺省档契约:
//  · HALF_DUPLEX_DEFAULT === false —— meHeld/othHeld 暂让+声源仲裁是「两人同机
//    一体台+共享扬声器」历史遗产,远程耳机拓扑(双方各戴耳机各自设备)下纯伤害
//    (对方说话被暂让白丢),缺省必须=互不闭麦(真全双工);
//  · halfDuplexInitial:query 显式值优先——?halfDuplex=1 强开(同机演示档逃生门,
//    外放同桌不开=译文被对向麦拾回再译=串译死循环) / ?halfDuplex=0 强关 /
//    缺席或坏值=编译期常量;坏 search 串绝不抛;
//  · 装配 pin:interpret-console 必须经 halfDuplexInitial 初始化(禁止手写
//    useState(true) 把旧半双工缺省档带回来)。
//
// 装配照 device-roles.test.mjs:lib/interp-duplex.ts → tsc → CJS → require。

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

const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-interp-duplex-"));
execFileSync(
  process.execPath,
  [
    path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
    path.join(WEB_ROOT, "lib", "interp-duplex.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "pipe" },
);

const require = createRequire(import.meta.url);
const m = require(path.join(TMP_OUT, "interp-duplex.js"));

test("half-duplex default is OFF (full duplex topology)", () => {
  assert.equal(m.HALF_DUPLEX_DEFAULT, false);
});

test("halfDuplexInitial: absent/garbage search falls back to constant", () => {
  assert.equal(m.halfDuplexInitial(""), false);
  assert.equal(m.halfDuplexInitial("?"), false);
  assert.equal(m.halfDuplexInitial("?foo=1&bar=2"), false);
  assert.equal(m.halfDuplexInitial("not-a-query"), false);
});

test("halfDuplexInitial: explicit query override wins", () => {
  assert.equal(m.halfDuplexInitial("?halfDuplex=1"), true);
  assert.equal(m.halfDuplexInitial("?halfDuplex=0"), false);
  assert.equal(m.halfDuplexInitial("?halfDuplex=1&x=y"), true);
  assert.equal(m.halfDuplexInitial("?x=y&halfDuplex=0"), false);
});

test("halfDuplexInitial: only literal '1'/'0' are honored", () => {
  assert.equal(m.halfDuplexInitial("?halfDuplex=true"), false);
  assert.equal(m.halfDuplexInitial("?halfDuplex=on"), false);
  assert.equal(m.halfDuplexInitial("?halfDuplex="), false);
});

test("assembly pin: interpret-console initializes via halfDuplexInitial", () => {
  const src = readFileSync(path.join(WEB_ROOT, "components", "interpret-console.tsx"), "utf8");
  assert.match(src, /halfDuplexInitial\(/, "console must derive initial state from lib/interp-duplex");
  assert.doesNotMatch(
    src,
    /useState\(true\);\s*\n\s*const \[meHeld/,
    "halfDuplex must not default to true (full duplex is the default)",
  );
});

test("cleanup", () => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});
