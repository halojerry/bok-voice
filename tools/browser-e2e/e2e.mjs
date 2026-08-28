import { chromium } from "playwright";

const WEB_URL = process.env.WEB_URL || "http://localhost:3000/calls/new";
const WAIT_MS = Number(process.env.WAIT_MS || 20000);
const chrome = await chromium.launch({
  headless: true,
  args: [
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
  ],
});

const context = await chrome.newContext({ permissions: ["microphone"] });
const page = await context.newPage();
const logs = [];
page.on("console", (m) => logs.push(m.text()));

await page.goto(WEB_URL, { waitUntil: "networkidle" });
// The Connect button is disabled until an object is selected.
await page.waitForSelector('button:has-text("接通")', { state: "visible", timeout: 15000 });
await page.getByRole("button", { name: "接通" }).click();

await page.waitForFunction(
  () => document.body.innerText.includes("实时转写"),
  { timeout: 30000 },
);

const body = await page.locator("body").innerText();
const roomMatch = body.match(/call-([a-z0-9]+)/i);
const roomName = roomMatch?.[1];
if (!roomName) throw new Error("room name missing");

console.log("ROOM_JOINED", roomName);

// 松手前先让 Agent 有机会被 LiveKit 派发
await page.waitForTimeout(WAIT_MS);
await page.getByRole("button", { name: "挂断" }).click();
await page.waitForFunction(
  () => !document.body.innerText.includes("实时转写"),
  { timeout: 10000 },
);

console.log("BROWSER_E2E_PASSED");
console.log("console_logs:", logs.slice(-8));
await chrome.close();
