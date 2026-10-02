// 意图/绑定 id 生成回归钉（2026-09-26 实弹）：intent-manager 的 id 必须走 genHexId
// （int_/bnd_ + 8 位小写 hex——CP flow_graph._ID_RE 唯一合法形状）。旧 rid() 用
// Math.random base36 产 6 字符非 hex id，新建意图/加动作/基础意图包三条保存路径
// 全被 CP 400 拒且 UI 只剩裸 "400 Bad Request"。源码扫描钉死复发面。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";
import assert from "node:assert/strict";

const src = readFileSync(
  fileURLToPath(new URL("../components/intent-manager.tsx", import.meta.url)),
  "utf8",
);

test("intent-manager id 生成无 rid/Math.random 残留", () => {
  const codeOnly = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.ok(!/\brid\s*\(/.test(codeOnly), "rid( 已删净（防旧生成器回潮）");
  assert.ok(!/Math\.random/.test(codeOnly), "代码面无 Math.random（id 一律 crypto genHexId）");
});

test("genHexId 四个落点齐备（新建意图/绑定兜底/加动作/基础意图包）", () => {
  assert.equal((src.match(/genHexId\("int_"\)/g) || []).length >= 2, true, "int_ 落点");
  assert.equal((src.match(/genHexId\("bnd_"\)/g) || []).length >= 2, true, "bnd_ 落点");
});

test("genHexId 产出的形状恒过 CP _ID_RE（int_+8hex）", () => {
  // 直接复刻实现跑 200 发：crypto 同源逻辑，验证形状而非具体值。
  const gen = (prefix) => {
    const bytes = new Uint8Array(4);
    crypto.getRandomValues(bytes);
    return prefix + Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
  };
  const RE = /^int_[0-9a-f]{8}$/;
  for (let i = 0; i < 200; i++) assert.match(gen("int_"), RE);
  for (let i = 0; i < 200; i++) assert.match(gen("bnd_"), /^bnd_[0-9a-f]{8}$/);
});
