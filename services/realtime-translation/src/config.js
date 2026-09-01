import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PKG_DIR = dirname(fileURLToPath(import.meta.url)) + "/..";
export const REPO_ROOT = resolve(PKG_DIR, "../..");

const DEFAULTS = {
  asr: { provider: "qwen3_asr", base_url: "http://127.0.0.1:8787", sample_rate: 16000 },
  translator: {
    provider: "local_openai",
    base_url: "http://127.0.0.1:1235/v1",
    model: "",
  },
  tts: { provider: "qwen3_tts", base_url: "http://127.0.0.1:8788", sample_rate: 24000 },
  server: { host: "127.0.0.1", port: 8790, metrics_file: "" },
};

export function loadConfig(path = resolve(PKG_DIR, "config.json")) {
  // Packaged app: bok.py writes a fully-resolved config to app-data and points
  // BOK_BLINE_CONFIG at it so nothing is written into the read-only bundle.
  const envPath = process.env.BOK_BLINE_CONFIG;
  if (envPath) path = envPath;
  let file = {};
  try {
    file = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    // defaults
  }
  const cfg = {
    ...DEFAULTS,
    ...file,
    server: { ...DEFAULTS.server, ...(file.server || {}) },
    asr: { ...DEFAULTS.asr, ...(file.asr || {}) },
    translator: { ...DEFAULTS.translator, ...(file.translator || {}) },
    tts: { ...DEFAULTS.tts, ...(file.tts || {}) },
  };
  if (!cfg.server.metrics_file) {
    cfg.server.metrics_file = resolve(REPO_ROOT, "data/translation-metrics.jsonl");
  }
  return cfg;
}
