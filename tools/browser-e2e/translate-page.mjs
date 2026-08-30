// 验证 /translate 页面能渲染并连上 B 线 worker（ws://localhost:8790）。
// 页面加载后应显示 "WS OPEN"，且无页面报错。
import { chromium } from "playwright";

const WEB_URL = process.env.WEB_URL || "http://localhost:3000/translate";

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(`PAGEERROR ${e.message}`));
page.on("console", (m) => {
  if (m.type() === "error") errors.push(`CONSOLE ${m.text()}`);
});

try {
  await page.goto(WEB_URL, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForFunction(() => document.body.innerText.includes("同声传译"), undefined, { timeout: 15000 });
  await page.waitForFunction(() => document.body.innerText.includes("WS OPEN"), undefined, { timeout: 15000 });
  const hasChannelForm = await page.locator("text=添加通道").count();
  const wsState = await page.evaluate(() => {
    const m = document.body.innerText.match(/WS (OPEN|CONNECTING|CLOSED)/i);
    return m ? m[1] : "UNKNOWN";
  });
  console.log(`[PASS] page rendered with 同声传译 + channel form (${hasChannelForm})`);
  console.log(`[PASS] WS state: ${wsState.toUpperCase()}`);
  const realErrors = errors.filter((e) => !e.includes("Autoplay") && !e.includes("webkit"));
  console.log(realErrors.length === 0 ? "[PASS] no page/console errors" : `[FAIL] errors: ${realErrors.join(" | ").slice(0, 300)}`);
  if (wsState.toUpperCase() !== "OPEN" || realErrors.length > 0) process.exitCode = 1;
} catch (err) {
  console.log("[FAIL]", err?.message?.slice(0, 300));
  process.exitCode = 1;
} finally {
  await browser.close();
}
