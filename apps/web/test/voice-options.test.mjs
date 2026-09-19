// lib/voice-options.ts 纯函数单测（W5-T2 卫生收编）：下拉装配（合并去重/语言过滤
// 回落/克隆置顶/两种标签风格）、试听语言判定（ID 正则三分 + fieldKey/fallback 链）、
// 示例文本三语、罐头音试听路由三态（cache/live/blocked）。
//
// 装配照 var-panel.test.mjs：lib/voice-options.ts 零 import，单文件转译直载。

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

const TMP = mkdtempSync(path.join(WEB_ROOT, ".tmp-voice-options-"));
execFileSync(
  process.execPath,
  [TSC, path.join(WEB_ROOT, "lib", "voice-options.ts"), "--outDir", TMP, "--module", "commonjs", "--target", "es2020", "--skipLibCheck"],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const vo = require(path.join(TMP, "voice-options.js"));

test.after(() => {
  rmSync(TMP, { recursive: true, force: true });
});

const CATALOG = [
  { id: "canto-a", label: "粤甲", lang: "cantonese" },
  { id: "zh-a", label: "普甲", lang: "zh" },
  { id: "en-a", label: "英甲", lang: "en" },
];

// ---- ① 合并 + Set 去重（目录/预置先、克隆后；先到先得） ----
test("合并去重：firstOption→目录→克隆，目录与克隆同 id 只留先到者", () => {
  const opts = vo.buildVoiceSelectOptions({
    catalog: CATALOG,
    slotLang: "zh",
    minimaxClones: [
      { voice_id: "zh-a", label: "重复目录 id" }, // 与目录条目同 id → 丢弃
      { voice_id: "clone-1", label: "我的声" },
      { voice_id: "clone-1", label: "重复克隆" }, // 克隆内部重复 → 丢弃
      { voice_id: "", label: "空 id" }, // 空 id 不产出
    ],
    firstOption: { value: "", label: "（默认，跟随设置）" },
  });
  assert.deepEqual(opts, [
    { value: "", label: "（默认，跟随设置）" },
    { value: "zh-a", label: "普甲" },
    { value: "clone-1", label: "克隆 · 我的声" },
  ]);
});

// ---- ② slotLang 过滤 + 未知语言回落全量；克隆不吃过滤 ----
test("语言过滤与回落：已知语言滤目录，未知（vi/缺省）回落全量，克隆恒在", () => {
  const zh = vo.buildVoiceSelectOptions({ catalog: CATALOG, slotLang: "zh", minimaxClones: [{ voice_id: "c1", label: "x" }] });
  assert.deepEqual(zh.map((o) => o.value), ["zh-a", "c1"]);
  for (const slot of ["vi", "", undefined]) {
    const full = vo.buildVoiceSelectOptions({ catalog: CATALOG, slotLang: slot, minimaxClones: [{ voice_id: "c1", label: "x" }] });
    assert.deepEqual(full.map((o) => o.value), ["canto-a", "zh-a", "en-a", "c1"], `slotLang=${String(slot)} 回落全量`);
  }
});

// ---- ③ 匹配语言克隆置顶（云端克隆先、本地克隆后；其余克隆垫底） ----
test("克隆置顶：匹配 slotLang 的克隆排目录与预置之前", () => {
  const opts = vo.buildVoiceSelectOptions({
    catalog: CATALOG,
    slotLang: "cantonese",
    minimaxClones: [
      { voice_id: "clone-en", label: "英克隆", sample_lang: "en" },
      { voice_id: "clone-canto", label: "粤克隆", sample_lang: "cantonese" },
    ],
    localClones: [{ id: "local-canto", lang: "cantonese" }],
    localSpeakers: ["serena", "vivian"],
  });
  assert.deepEqual(opts.map((o) => o.value), [
    "clone-canto", "local-canto", // 匹配语言克隆置顶（云端→本地）
    "canto-a", // 目录过滤到粤语
    "serena", "vivian", // 预置 speaker（不过滤）
    "clone-en", // 其余克隆垫底
  ]);
});

// ---- ④ 两种标签风格：前缀（settings/interpret，默认「克隆 · 」）与 personas 后缀 ----
test("标签风格：前缀风格默认「克隆 · 」可参数化；后缀风格=personas 手法", () => {
  const prefix = vo.buildVoiceSelectOptions({
    catalog: CATALOG,
    slotLang: "zh",
    minimaxClones: [{ voice_id: "cv1", label: "我的声" }, { voice_id: "cv2" }],
  });
  assert.deepEqual(prefix.map((o) => o.label), ["普甲", "克隆 · 我的声", "克隆 · cv2"]);

  const customPrefix = vo.buildVoiceSelectOptions({
    minimaxClones: [{ voice_id: "cv1", label: "我的声" }],
    cloneLabelPrefix: "云克隆：",
  });
  assert.equal(customPrefix[0].label, "云克隆：我的声");

  const suffix = vo.buildVoiceSelectOptions({
    localClones: [{ id: "lv1", lang: "cantonese" }, { id: "lv2" }],
    localSpeakers: ["serena", "vivian"],
    slotLang: "cantonese",
    cloneSuffix: true,
    presetLabels: { serena: "塞蕾娜" },
    presetSuffix: "（预置音色）",
  });
  assert.deepEqual(suffix.map((o) => `${o.value}=${o.label}`), [
    "lv1=lv1（克隆 · 粤语）", // 匹配语言克隆置顶
    "serena=塞蕾娜（预置音色）", // 预置显示名映射 + 后缀
    "vivian=vivian（预置音色）", // 不在映射回落 id
    "lv2=lv2（克隆）", // 语言未知不带语言标注
  ]);
});

// ---- ⑤ decideAuditionPath 三态 ----
test("decideAuditionPath：有录音=cache，缺录音主管=live/非主管=blocked，未知回落 cache", () => {
  assert.equal(vo.decideAuditionPath({ state: "ok" }, false), "cache");
  assert.equal(vo.decideAuditionPath({ state: "ok" }, true), "cache");
  assert.equal(vo.decideAuditionPath({ state: "missing" }, true), "live");
  assert.equal(vo.decideAuditionPath({ state: "missing" }, false), "blocked");
  // 状态面未加载/TTL 滞后：先按 cache 路径试取，页面取播失败仍按角色降级。
  assert.equal(vo.decideAuditionPath(undefined, true), "cache");
  assert.equal(vo.decideAuditionPath(undefined, false), "cache");
});

// ---- ⑥ resolvePreviewLang：ID 正则三分 + fieldKey/fallback 链 ----
test("resolvePreviewLang：Cantonese_→粤、English_/socialmedia_→英、其余按链回落", () => {
  assert.equal(vo.resolvePreviewLang("Cantonese_crisp_news_anchor_vv2"), "cantonese");
  assert.equal(vo.resolvePreviewLang("English_magnetic_voiced_man"), "en");
  assert.equal(vo.resolvePreviewLang("socialmedia_female_2_v1"), "en");
  assert.equal(vo.resolvePreviewLang("Chinese_wenrounvxing"), "zh");
  // 本地 Qwen 多语音色：按字段键链（settings 本地音色路）。
  assert.equal(vo.resolvePreviewLang("serena", { fieldKey: "speaker_cantonese" }), "cantonese");
  assert.equal(vo.resolvePreviewLang("serena", { fieldKey: "speaker_en" }), "en");
  assert.equal(vo.resolvePreviewLang("serena", { fieldKey: "speaker_zh" }), "zh");
  // 显式 fallback（如人设主语言）→ 三态外忽略 → 缺省普通话。
  assert.equal(vo.resolvePreviewLang("serena", { fallback: "cantonese" }), "cantonese");
  assert.equal(vo.resolvePreviewLang("serena", { fallback: "vi" }), "zh");
  assert.equal(vo.resolvePreviewLang(""), "zh");
});

// ---- ⑦ previewSampleText：三语含粤语，人设名可注入 ----
test("previewSampleText：三语齐全含粤语示例，name 缺省回落「Bok 客服」", () => {
  assert.match(vo.previewSampleText("cantonese"), /我係Bok 客服/);
  assert.match(vo.previewSampleText("cantonese", "小博"), /我係小博/);
  assert.match(vo.previewSampleText("zh", "小博"), /我是小博/);
  assert.match(vo.previewSampleText("en", "小博"), /this is 小博/);
  assert.match(vo.previewSampleText("en"), /this is Bok 客服/);
  assert.match(vo.previewSampleText("unknown"), /我是Bok 客服/); // 未知语言回落普通话
});
