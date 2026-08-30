// Trilingual browser E2E (A-line): inject zh / yue / en WAV files as the fake
// microphone capture and assert that the LiveKit room shows a user transcript
// and an agent reply for each language.
import { chromium } from "playwright";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

const WEB_URL = process.env.WEB_URL || "http://localhost:3000/calls/new";
const ROOT = resolve(import.meta.dirname, "../../data/test-audio");
const PER_LANG_TIMEOUT_MS = Number(process.env.PER_LANG_TIMEOUT_MS || 240000);
const JOIN_TIMEOUT_MS = 45000;

const LANGS = [
  { lang: "zh", file: "zh.wav" },
  { lang: "yue", file: "yue.wav" },
  { lang: "en", file: "en.wav" },
];

// 支持 ONLY_LANG=zh 只跑单语，便于调试（勿用 LANG：与系统 locale 冲突）。
const ONLY = process.env.ONLY_LANG;
const RUN = ONLY ? LANGS.filter((l) => l.lang === ONLY) : LANGS;

for (const item of RUN) {
  const p = resolve(ROOT, item.file);
  if (!existsSync(p)) throw new Error(`missing test audio: ${p}`);
  item.path = p;
}

async function runLanguage(item) {
  const chrome = await chromium.launch({
    headless: true,
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
      "--autoplay-policy=no-user-gesture-required",
      `--use-file-for-fake-audio-capture=${item.path}`,
    ],
  });
  const context = await chrome.newContext({ permissions: ["microphone"] });
  const page = await context.newPage();
  const logs = [];
  page.on("console", (m) => logs.push(m.text()));
  page.on("pageerror", (e) => logs.push(`PAGEERROR ${e.message}`));

  const result = { lang: item.lang, youText: "", agentText: "", error: "" };
  try {
    await page.goto(WEB_URL, { waitUntil: "networkidle" });
    await page.waitForSelector('button:has-text("接通")', {
      state: "visible",
      timeout: 30000,
    });
    await page.getByRole("button", { name: "接通" }).click();

    // Wait until the room is live (the connect button flips to 挂断).
    await page.waitForFunction(
      () => document.body.innerText.includes("挂断"),
      undefined,
      { timeout: JOIN_TIMEOUT_MS },
    );

    // A user (YOU) transcript from the injected audio.
    await page.waitForFunction(
      () => {
        const els = [...document.querySelectorAll("div")];
        return els.some(
          (d) => d.innerText.startsWith("YOU") && d.innerText.trim().length > 4,
        );
      },
      undefined,
      { timeout: PER_LANG_TIMEOUT_MS },
    );
    result.youText = await page
      .locator("div", { hasText: /^YOU/ })
      .last()
      .innerText()
      .catch(() => "");

    // An agent (AGENT) reply after ASR -> LLM -> TTS.
    await page.waitForFunction(
      () => {
        const els = [...document.querySelectorAll("div")];
        return els.some(
          (d) => d.innerText.startsWith("AGENT") && d.innerText.trim().length > 4,
        );
      },
      undefined,
      { timeout: PER_LANG_TIMEOUT_MS },
    );
    result.agentText = await page
      .locator("div", { hasText: /^AGENT/ })
      .last()
      .innerText()
      .catch(() => "");

    await page.getByRole("button", { name: "挂断" }).click().catch(() => {});
    await page.waitForTimeout(3000);
  } catch (err) {
    result.error = String(err?.message || err);
    result.bodySnippet = await page.locator("body").innerText().catch(() => "").then((t) => t.slice(0, 1200));
  }

  result.console = logs.slice(-6);
  await context.close().catch(() => {});
  await chrome.close().catch(() => {});
  return result;
}

const results = [];
for (const item of RUN) {
  const r = await runLanguage(item);
  results.push(r);
  const ok = !r.error && r.youText.trim().length > 0 && r.agentText.trim().length > 0;
  console.log(
    `\n[${r.lang}] ${ok ? "PASS" : "FAIL"}  YOU="${(r.youText || "").slice(0, 60)}"  AGENT="${(r.agentText || "").slice(0, 60)}"`,
  );
  if (r.error) console.log("  error:", r.error);
  if (r.bodySnippet) console.log("  body:", JSON.stringify(r.bodySnippet));
  console.log("  console:", r.console);
}

const passed = results.filter((r) => !r.error && r.youText && r.agentText).length;
console.log(`\nTRILINGUAL_E2E ${passed}/${results.length} PASSED`);
if (passed !== results.length) process.exitCode = 1;
