// lib/voice-map.ts 纯函数单测（2026-10-08 音色选择器统一波）：人设
// reference_audio 解析（坏形状=空 map）+ 整场主音色收敛（主语言槽 > zh >
// cantonese > 首个非空——与 agent 侧 collapse_voice_map 同规则）。
//
// 装配照 voice-options.test.mjs：lib/voice-map.ts 零 import，单文件转译直载。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdtempSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");
const TSC = path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc");

const TMP = mkdtempSync(path.join(WEB_ROOT, ".tmp-voice-map-"));
execFileSync(
  process.execPath,
  [TSC, path.join(WEB_ROOT, "lib", "voice-map.ts"), "--outDir", TMP, "--module", "commonjs", "--target", "es2020", "--skipLibCheck"],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const { parseVoiceMap, primaryVoiceFor } = require(path.join(TMP, "voice-map.js"));
rmSync(TMP, { recursive: true, force: true });

test("parseVoiceMap: 合法 JSON → map；非对象/坏 JSON/非字符串 → 空 map", () => {
  assert.deepEqual(parseVoiceMap('{"zh":"Chinese_wenrou"}'), { zh: "Chinese_wenrou" });
  assert.deepEqual(parseVoiceMap(""), {});
  assert.deepEqual(parseVoiceMap(null), {});
  assert.deepEqual(parseVoiceMap("not-json"), {});
  assert.deepEqual(parseVoiceMap('["v1"]'), {});
  assert.deepEqual(parseVoiceMap('{"zh":123}'), { zh: 123 }); // 形状宽容:值原样(空值由收敛链挡)
});

test("primaryVoiceFor: 主语言槽优先 → zh → cantonese → 首个非空", () => {
  const map = { en: "English_ryan", zh: "Chinese_wenrou", cantonese: "Cantonese_crisp" };
  assert.equal(primaryVoiceFor("zh", map), "Chinese_wenrou");
  assert.equal(primaryVoiceFor("cantonese", map), "Cantonese_crisp");
  assert.equal(primaryVoiceFor("en", map), "English_ryan");
  // 主语言槽缺席(de 等四语人设)→ zh 回落
  assert.equal(primaryVoiceFor("de", map), "Chinese_wenrou");
  // 三键全缺 → 首个非空值
  assert.equal(primaryVoiceFor("zh", { fr: "French_anna" }), "French_anna");
  assert.equal(primaryVoiceFor("zh", {}), "");
});
