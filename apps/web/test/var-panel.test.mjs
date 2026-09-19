// lib/var-panel.ts 纯函数单测（W3 T2 变量 tab）：占位符目录匹配（别名/trim/繁简同值）、
// 渲染语义（空串/缺失保留占位）、数字双轨（汉字逐位 vs ASCII 尾号）、联系渠道语言缺省、
// 占位符扫描（未知 key 告警/分步归并/legacy 四段兜底）、直念行 dropped 判定。
//
// 语义权威=agent flow.py：object_vars(:252-289)/render_template_text(:291-298)/
// _CANTONESE_DIGITS(:207-221)/step_say_text(:1031-1045)——断言注释逐条引行号。
//
// 装配照 flow-canvas.test.mjs：lib/var-panel.ts 零 import,单文件转译直载。

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

const TMP = mkdtempSync(path.join(WEB_ROOT, ".tmp-var-panel-"));
execFileSync(
  process.execPath,
  [TSC, path.join(WEB_ROOT, "lib", "var-panel.ts"), "--outDir", TMP, "--module", "commonjs", "--target", "es2020", "--skipLibCheck"],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const vp = require(path.join(TMP, "var-panel.js"));

test.after(() => {
  rmSync(TMP, { recursive: true, force: true });
});

// ---- ① 目录匹配：别名组 / trim / 繁简不同 key 同值 ----
test("目录：别名组同值（姓名/名字/name ← display_name,flow.py:276-278）", () => {
  const obj = { display_name: "王小明" };
  for (const key of ["姓名", "名字", "name"]) {
    assert.equal(vp.renderVarsText(`你好{${key}}`, obj), "你好王小明", key);
  }
  // 物流公司/快递公司/courier 三别名（flow.py:280-282）；收货地址/地址（:284-285）。
  const oc = { courier: "顺丰", address: "九龙城区" };
  assert.equal(vp.renderVarsText("{物流公司}/{快递公司}/{courier}", oc), "顺丰/顺丰/顺丰");
  assert.equal(vp.renderVarsText("{收货地址}{地址}", oc), "九龙城区九龙城区");
});

test("目录：占位键 trim 匹配（flow.py:296 key=m.group(1).strip()）", () => {
  const obj = { display_name: "王小明" };
  assert.equal(vp.renderVarsText("请问係咪{ 姓名 }？", obj), "请问係咪王小明？");
  // 花括号内含空白：trim 后命中目录 → 照常替换。
  assert.equal(vp.renderVarsText("{\t快递单号}", { tracking_no: "7890" }), "七八九零");
  // trim 后仍不在目录 → 原样保留（含空白整体回写）。
  assert.equal(vp.renderVarsText("{ 客户名 }", { display_name: "王小明" }), "{ 客户名 }");
});

test("目录：繁简是两个 key、同指一值（联系方式≠聯絡方式,flow.py:286-288）", () => {
  const obj = { contact_channel: "WhatsApp: abc" };
  assert.equal(vp.renderVarsText("{聯絡方式}", obj), "WhatsApp: abc");
  assert.equal(vp.renderVarsText("{联系方式}", obj), "WhatsApp: abc");
  assert.equal(vp.renderVarsText("{contact}", obj), "WhatsApp: abc");
  // 目录里两组键各自在册：扫描不报未知。
  const hits = vp.scanPlaceholders([{ goal: "", ref: "{聯絡方式}/{联系方式}" }]);
  assert.equal(hits.filter((h) => h.unknown).length, 0);
  assert.equal(hits.length, 2);
});

// ---- ② 渲染：空串/缺失保留占位（flow.py:291-298 val 为空回写 m.group(0)） ----
test("渲染：对象字段空串保留原占位；未知 key 原样", () => {
  assert.equal(vp.renderVarsText("你好{姓名}", { display_name: "" }), "你好{姓名}");
  assert.equal(vp.renderVarsText("你好{姓名}", {}), "你好{姓名}");
  assert.equal(vp.renderVarsText("你好{姓名}", null), "你好{姓名}");
  // 不在目录的 key 一律保留（flow.py vars_map.get(key,"") 同款）。
  assert.equal(vp.renderVarsText("{客户名}", { display_name: "王小明" }), "{客户名}");
  // 空文本原样;相邻花括号 {} 不算占位（正则要求 ≥1 字符）。
  assert.equal(vp.renderVarsText("", { display_name: "x" }), "");
  assert.equal(vp.renderVarsText("a{}b", { display_name: "x" }), "a{}b");
});

// ---- ③ 数字双轨：快递尾号汉字逐位 vs tracking_tail ASCII（flow.py:273-282） ----
test("digitsCn 双轨：快递尾号=末4位汉字,tracking_tail=末4位 ASCII", () => {
  const obj = { tracking_no: "SF7890" };
  // 快递单号整体逐位转（flow.py:275 digits_to_cantonese(tracking)）。
  assert.equal(vp.renderVarsText("{快递单号}", obj), "SF七八九零");
  // 尾号：末 4 位汉字（粤语 TTS 逐字读）。
  assert.equal(vp.renderVarsText("{快递尾号}", obj), "七八九零");
  // EN 模板：末 4 位保留 ASCII（flow.py:282 注释——英文 TTS 直读,汉字会读错）。
  assert.equal(vp.renderVarsText("{tracking_tail}", obj), "7890");
  // 短于 4 位：tail=整串（flow.py:262 tail=tracking[-4:] if len>=4 else tracking）。
  const short = { tracking_no: "78" };
  assert.equal(vp.renderVarsText("{快递尾号}", short), "七八");
  assert.equal(vp.renderVarsText("{tracking_tail}", short), "78");
  // 电话逐位汉字（flow.py:287）。
  assert.equal(vp.renderVarsText("{电话}", { phone: "13800138000" }), "一三八零零一三八零零零");
  // digitsToCn 只转数字、其它字符原样（flow.py:221 _CANTONESE_DIGITS.get(ch,ch)）。
  assert.equal(vp.digitsToCn("SF7890号"), "SF七八九零号");
  assert.equal(vp.digitsToCn(""), "");
});

// ---- ④ 联系渠道语言缺省三分支（flow.py:268-271） ----
test("contact 缺省：zh→微信,cantonese/en→WhatsApp;显式 contact_channel 优先", () => {
  assert.equal(vp.renderVarsText("{聯絡方式}", { language: "zh" }), "微信");
  assert.equal(vp.renderVarsText("{联系方式}", { language: "cantonese" }), "WhatsApp");
  assert.equal(vp.renderVarsText("{contact}", { language: "en" }), "WhatsApp");
  // 语言大小写归一（flow.py:268 .strip().lower()）。
  assert.equal(vp.renderVarsText("{contact}", { language: "ZH" }), "微信");
  // 未知语言落 WhatsApp 兜底（.get(lang,"WhatsApp")）。
  assert.equal(vp.renderVarsText("{contact}", { language: "vi" }), "WhatsApp");
  // 对象显式指定优先、语言不参与。
  assert.equal(
    vp.renderVarsText("{聯絡方式}", { language: "zh", contact_channel: " WhatsApp " }),
    "WhatsApp",
  );
});

// ---- ⑤ 扫描：未知 key 告警 / 分步归并 / 三段行分类 / legacy 兜底 ----
test("scanPlaceholders：未知 key=unknown 告警,goal 与 ref 都扫", () => {
  const steps = [
    { goal: "确认包裹是不是{姓名}本人的", ref: "你好，请问係咪{姓名}？尾号{快递尾号}。" },
    { goal: "谈赔", ref: "单号{快递单号}丢了\n如果客户问{地址}在哪 → 按对象地址答复\n注意：先核对{电话}" },
  ];
  const hits = vp.scanPlaceholders(steps);
  const byKey = Object.fromEntries(hits.map((h) => [h.key.trim(), h]));
  // goal+ref 累计：姓名 2 次、跨两步。
  assert.equal(byKey["姓名"].count, 2);
  assert.deepEqual(byKey["姓名"].stepIdxs, [0]);
  assert.equal(byKey["快递单号"].count, 1);
  assert.deepEqual(byKey["快递单号"].stepIdxs, [1]);
  // 分支行/注意行里的占位也扫到（三段拆分同 parseStepRefParts 语义）。
  assert.equal(byKey["地址"].count, 1);
  assert.equal(byKey["电话"].count, 1);
  assert.equal(byKey["电话"].section, "note");
  assert.equal(byKey["姓名"].section, "goal");
  // 全部命中目录,无未知。
  assert.equal(hits.filter((h) => h.unknown).length, 0);
});

test("scanPlaceholders：未知 key 告警（trim 后不在目录）+ 键原样保留", () => {
  const hits = vp.scanPlaceholders([{ goal: "", ref: "您的{ 会员等级 }是黄金" }]);
  assert.equal(hits.length, 1);
  assert.equal(hits[0].key, " 会员等级 "); // 原样（展示用）
  assert.equal(hits[0].unknown, true);
  assert.deepEqual(hits[0].stepIdxs, [0]);
  // 已知+未知混合。
  const mixed = vp.scanPlaceholders([{ goal: "{姓名}/{單號}", ref: "" }]);
  assert.deepEqual(mixed.map((h) => [h.key.trim(), h.unknown]), [["姓名", false], ["單號", true]]);
});

test("scanPlaceholders：steps 空时扫 legacy 四段（section=四段名）", () => {
  const legacy = {
    opening: "您好，我是{物流公司}的",
    core: "",
    objection: "如果客户拒绝 → 提{姓名}的包裹",
    closing: "再见",
  };
  const hits = vp.scanPlaceholders([], legacy);
  const byKey = Object.fromEntries(hits.map((h) => [h.key.trim(), h]));
  assert.equal(byKey["物流公司"].section, "opening");
  assert.equal(byKey["姓名"].section, "objection");
  assert.deepEqual(byKey["物流公司"].stepIdxs, []); // legacy 无步下标
  // 空段不产生命中；无 legacy 也不炸。
  assert.equal(vp.scanPlaceholders([], {}).length, 0);
  assert.equal(vp.scanPlaceholders([{ goal: "", ref: "{姓名}" }]).length, 1);
});

// ---- ⑥ 直念行清单与 dropped 判定（flow.py:1031-1045 step_say_text 行级语义） ----
test("renderableLines：第 1 步恒取（opening force）,say 步取首行,非 say 步跳过", () => {
  const steps = [
    { goal: "g0", ref: "你好，请问係咪{姓名}？\n如果客户唔记得 → 提地址" },
    { goal: "g1", ref: "非直念步首行不预览" },
    { goal: "g2", ref: "通知正文第一行\n第二行", say: true },
  ];
  const obj = { display_name: "王小明" };
  const lines = vp.renderableLines(steps, obj);
  assert.deepEqual(lines.map((l) => [l.stepIdx, l.kind]), [[0, "opening"], [2, "say"]]);
  // 首个非空行=开场行;变量已替换。
  assert.equal(lines[0].raw, "你好，请问係咪{姓名}？");
  assert.equal(lines[0].rendered, "你好，请问係咪王小明？");
  assert.equal(lines[0].dropped, false);
  // say 步首行。
  assert.equal(lines[1].rendered, "通知正文第一行");
});

test("renderableLines：渲染后仍含 {占位}=dropped（变量缺失,通话中跳过/回退）", () => {
  const steps = [
    { goal: "g", ref: "请问係咪{姓名}？" },
    { goal: "g", ref: "缺单号{快递单号}丢了", say: true },
  ];
  const lines = vp.renderableLines(steps, { display_name: "" });
  assert.equal(lines[0].dropped, true); // 开场行含缺失变量 → 回退通用开场白
  assert.equal(lines[0].rendered, "请问係咪{姓名}？");
  assert.equal(lines[1].dropped, true); // 直念行含缺失变量 → 运行时跳过该行
  // 对象字段齐全 → 不 dropped。
  const ok = vp.renderableLines(
    [{ goal: "g", ref: "单号{快递单号}丢了", say: true }],
    { tracking_no: "SF7890" },
  );
  assert.equal(ok[0].dropped, false);
  assert.equal(ok[0].rendered, "单号SF七八九零丢了");
});

test("renderableLines：ref 空退 goal（flow.py:1038 s.ref or s.goal）,全空行跳过", () => {
  const lines = vp.renderableLines(
    [{ goal: "确认{姓名}的包裹", ref: "\n  \n如果客户问 → 答" }],
    { display_name: "王小明" },
  );
  assert.equal(lines.length, 1);
  assert.equal(lines[0].raw, "如果客户问 → 答"); // 首个非空行（空行跳过）
  // ref 整段空白 → 源=goal。
  const fromGoal = vp.renderableLines([{ goal: "第{快递尾号}步", ref: "  " }], { tracking_no: "1234" });
  assert.equal(fromGoal[0].raw, "第{快递尾号}步");
  assert.equal(fromGoal[0].dropped, false);
  assert.equal(fromGoal[0].rendered, "第一二三四步");
});
