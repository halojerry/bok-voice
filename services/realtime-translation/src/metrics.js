import { appendFileSync } from "node:fs";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

export function appendMetrics(config, metric) {
  const file = config.server.metrics_file;
  if (!file) return;
  try {
    mkdirSync(dirname(file), { recursive: true });
    appendFileSync(file, `${JSON.stringify({ ts: Date.now(), ...metric })}\n`, "utf8");
  } catch {
    // metrics must never take down the worker
  }
}
