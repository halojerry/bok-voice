// W4a 双栏字幕成组纯函数契约(pairSubtitles,2026-10-07 demo-quality-wave):
// 纯函数住在 components/interpret-console.tsx 的标记注释区(本文件外的唯一真身),
// 测试按标记切片 → 项目自带 tsc 转译成 CJS → require 加载(weblog.test.mjs 同
// 路线,零新依赖)。钉住的算法契约:
//   ① src→紧随其后的同侧 dst 成对成组(原文在上、翻译紧随);
//   ② 同侧连到的多条 dst 全归同组(逐句拆译不散组);
//   ③ 在途原文(无翻译)冲账后单独成组,不丢;
//   ④ 孤儿译文(窗口裁剪切掉原文/清空残留)单独成组,不丢;
//   ⑤ rev/fwd 双流交错各自归位,绝不串流(流向+双侧独立开口槽);
//   ⑥ 输出保同列内时间序。
// 另钉 LANG_SHORT 七语表(zh/cantonese/en + W2 四语 de/fr/ja/pt)——防裸语言串
// 回归(语言对行/列头都吃这张表)。
// W4c(2026-10-08)新增 whoIs 契约:列=说话方——译文归「被译那句话的说话方」的
// 列(fwd→右/我方,rev→左/对方),不按译文语言分列;原文+译文同列成组。

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.resolve(HERE, "..");
const SRC_TSX = path.join(WEB_ROOT, "components", "interpret-console.tsx");

const BEGIN = "// ==== interp-subtitles (pure; extracted & node-tested by test/interp-subtitles.test.mjs) ====";
const END = "// ==== end interp-subtitles ====";

const source = readFileSync(SRC_TSX, "utf8");
const beginAt = source.indexOf(BEGIN);
const endAt = source.indexOf(END);
if (beginAt < 0 || endAt < 0 || endAt < beginAt) {
  throw new Error(
    "interp-subtitles 标记区在 interpret-console.tsx 缺失——成组纯函数必须保持在标记内供 node 测试提取",
  );
}
const region = source.slice(beginAt + BEGIN.length, endAt);

// ---- 装配:标记区 → 临时 .ts → 项目自带 tsc → CJS → require(加载期一次性)----
const TMP_OUT = mkdtempSync(path.join(tmpdir(), "bok-interp-sub-"));
mkdirSync(TMP_OUT, { recursive: true });
writeFileSync(path.join(TMP_OUT, "interp-subtitles.ts"), region);
execFileSync(
  path.join(WEB_ROOT, "node_modules", "typescript", "bin", "tsc"),
  [
    path.join(TMP_OUT, "interp-subtitles.ts"),
    "--outDir", TMP_OUT,
    "--module", "commonjs",
    "--target", "es2020",
    "--skipLibCheck",
  ],
  { stdio: "inherit" },
);
const require = createRequire(import.meta.url);
const { pairSubtitles, whoIs } = require(path.join(TMP_OUT, "interp-subtitles.js"));

test.after(() => {
  rmSync(TMP_OUT, { recursive: true, force: true });
});

const src = (side, flow, text, idx) => ({ who: { kind: "src", side, flow }, text, idx });
const dst = (side, flow, text, idx) => ({ who: { kind: "dst", side, flow }, text, idx });

// ---- whoIs 列=说话方(W4c 2026-10-08)----
/** agent 转写行的最小替身:transcribed_track_id 指向 agent 的 trans-<lang> 轨。 */
const agtRow = (trackSid) => ({
  streamInfo: { attributes: { "lk.transcribed_track_id": trackSid } },
});
/** 最小 Room 替身:远端只有一个 agent 参与者(既非 me- 也非 other-),挂 trans- 轨;
 * trackPublications 按 livekit 真形给 Map 形(值迭代面)——Object.values(Map)=恒空
 * 是 W4c 前译文归属全落 fallback 的运行时根因,替身必须长成真形才测得出。 */
const mkRoom = (trackSid, trackName) => ({
  remoteParticipants: { values: () => [{ identity: "agent-bok-interp", trackPublications: new Map([["t1", { trackSid, trackName }]]) }] },
  localParticipant: {},
});

test("whoIs:人端 identity 直判原文侧(me=右/我方,other=左/对方)", () => {
  assert.deepEqual(
    { side: whoIs({ participantInfo: { identity: "me-room1" } }, mkRoom("", ""), "zh", "en").side, kind: whoIs({ participantInfo: { identity: "me-room1" } }, mkRoom("", ""), "zh", "en").kind },
    { side: "right", kind: "src" },
  );
  const other = whoIs({ participantInfo: { identity: "other-room1" } }, mkRoom("", ""), "zh", "en");
  assert.equal(other.side, "left");
  assert.equal(other.kind, "src");
  assert.equal(other.flow, "rev");
});

test("whoIs W4c:译文归「被译那句话的说话方」的列,不按译文语言分列", () => {
  // 我说 zh,译成 en(fwd):旧版 en 译文进左列(对方列)=原文译文分家;现进右列。
  const fwd = whoIs(agtRow("TR_FWD"), mkRoom("TR_FWD", "trans-en"), "zh", "en");
  assert.equal(fwd.kind, "dst");
  assert.equal(fwd.flow, "fwd");
  assert.equal(fwd.side, "right");
  // 对方说 en,译成 zh(rev):zh 译文进左列(对方列),与对方原文同列。
  const rev = whoIs(agtRow("TR_REV"), mkRoom("TR_REV", "trans-zh"), "zh", "en");
  assert.equal(rev.kind, "dst");
  assert.equal(rev.flow, "rev");
  assert.equal(rev.side, "left");
});

test("whoIs:track 元数据缺席回落 rev/左列(保守=对方说的话的译文)", () => {
  const fb = whoIs({}, mkRoom("", ""), "zh", "en");
  assert.equal(fb.kind, "dst");
  assert.equal(fb.flow, "rev");
  assert.equal(fb.side, "left");
});

test("W4c 端到端:我说的话的原文+译文落同一列并成组(不再孤儿泡)", () => {
  const room = mkRoom("TR_FWD", "trans-en");
  const rows = [
    { who: whoIs({ participantInfo: { identity: "me-room1" } }, room, "zh", "en"), text: "听得到我声音吗？", idx: 1 },
    { who: whoIs(agtRow("TR_FWD"), room, "zh", "en"), text: "Can you hear my voice?", idx: 2 },
  ];
  const groups = pairSubtitles(rows);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].side, "right");
  assert.equal(groups[0].src.text, "听得到我声音吗？");
  assert.deepEqual(groups[0].dsts.map((d) => d.text), ["Can you hear my voice?"]);
});

test("LANG_SHORT 七语表:三基语 + W2 四语(de/fr/ja/pt),别再出裸语言串", () => {
  const m = source.match(/const LANG_SHORT(?::[^=]*)?= \{([^}]*)\}/);
  assert.ok(m, "LANG_SHORT 声明没找到");
  const keys = [...m[1].matchAll(/([A-Za-z]+)\s*:/g)].map((x) => x[1]);
  assert.deepEqual([...keys].sort(), ["cantonese", "de", "en", "fr", "ja", "pt", "zh"]);
});

test("src→紧随 dst 同侧成对成组", () => {
  const groups = pairSubtitles([
    src("left", "rev", "你好", 1),
    dst("left", "rev", "Hello", 2),
  ]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].side, "left");
  assert.equal(groups[0].flow, "rev");
  assert.equal(groups[0].src.text, "你好");
  assert.deepEqual(groups[0].dsts.map((d) => d.text), ["Hello"]);
});

test("双流交错不串流:rev/fwd 各自归位、同列保序", () => {
  const groups = pairSubtitles([
    src("left", "rev", "早上好", 1),
    src("right", "fwd", "早上好,请问", 2),
    dst("left", "rev", "Good morning", 3),
    dst("right", "fwd", "Good morning, may I ask", 4),
  ]);
  assert.equal(groups.length, 2);
  const left = groups.filter((g) => g.side === "left");
  const right = groups.filter((g) => g.side === "right");
  assert.equal(left.length, 1);
  assert.equal(right.length, 1);
  assert.equal(left[0].src.text, "早上好");
  assert.deepEqual(left[0].dsts.map((d) => d.text), ["Good morning"]);
  assert.equal(right[0].src.text, "早上好,请问");
  assert.deepEqual(right[0].dsts.map((d) => d.text), ["Good morning, may I ask"]);
});

test("同侧连到多条 dst 全归同组(逐句拆译不散组)", () => {
  const groups = pairSubtitles([
    src("right", "fwd", "我们这边会补偿", 1),
    dst("right", "fwd", "We will compensate", 2),
    dst("right", "fwd", "you for this.", 3),
  ]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].dsts.length, 2);
});

test("在途原文(还没等到翻译)单独成组不丢", () => {
  const groups = pairSubtitles([src("left", "rev", "还在说", 1)]);
  assert.equal(groups.length, 1);
  assert.ok(groups[0].src);
  assert.equal(groups[0].dsts.length, 0);
});

test("新 src 冲账未配对的开口组:孤儿原文照渲染", () => {
  const groups = pairSubtitles([
    src("left", "rev", "第一句", 1),
    src("left", "rev", "第二句", 2),
    dst("left", "rev", "second sentence", 3),
  ]);
  assert.equal(groups.length, 2);
  assert.ok(groups[0].src);
  assert.equal(groups[0].dsts.length, 0); // 孤儿原文(被新句冲账)
  assert.equal(groups[1].src.text, "第二句");
  assert.deepEqual(groups[1].dsts.map((d) => d.text), ["second sentence"]);
});

test("孤儿译文(窗口裁剪切掉原文)单独成组不丢", () => {
  const groups = pairSubtitles([dst("left", "rev", "orphan translation", 9)]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].src, null);
  assert.deepEqual(groups[0].dsts.map((d) => d.text), ["orphan translation"]);
});

test("dst 流向与开口组不符=孤儿组(防御:筛选前置后本不该发生)", () => {
  const groups = pairSubtitles([
    src("left", "rev", "A", 1),
    dst("left", "fwd", "x", 2),
  ]);
  // 循环期孤儿 dst 先落、末尾开口组冲账后落
  assert.equal(groups.length, 2);
  assert.equal(groups[0].src, null);
  assert.deepEqual(groups[0].dsts.map((d) => d.text), ["x"]);
  assert.ok(groups[1].src);
  assert.equal(groups[1].dsts.length, 0);
});

test("末尾开口组冲账:双列各自收尾、列内保序", () => {
  const groups = pairSubtitles([
    src("left", "rev", "A", 1),
    dst("left", "rev", "a", 2),
    src("right", "fwd", "B", 3),
    src("left", "rev", "C", 4),
    src("right", "fwd", "D", 5),
    dst("right", "fwd", "d", 6),
  ]);
  // left 列:A(成对) → C(开口冲账,无译);right 列:B(开口冲账,无译) → D(成对)
  const left = groups.filter((g) => g.side === "left").map((g) => g.src.text);
  const right = groups.filter((g) => g.side === "right").map((g) => g.src.text);
  assert.deepEqual(left, ["A", "C"]);
  assert.deepEqual(right, ["B", "D"]);
});

test("空输入=空组", () => {
  assert.deepEqual(pairSubtitles([]), []);
});
