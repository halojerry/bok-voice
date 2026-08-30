import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PKG_DIR = dirname(fileURLToPath(import.meta.url)) + "/..";
export const REPO_ROOT = resolve(PKG_DIR, "../..");

const DEFAULTS = {
  asr: { provider: "qwen3_asr", base_url: "http://127.0.0.1:8787", sample_rate: 16000 },
  translator: {
    provider: "ollama",
    base_url: "http://127.0.0.1:1235/v1",
    model: "/Users/halo/.lmstudio/models/huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit",
    think: false,
  },
  tts: { provider: "qwen3_tts", base_url: "http://127.0.0.1:8788", sample_rate: 24000 },
  server: { host: "0.0.0.0", port: 8790, metrics_file: resolve(REPO_ROOT, "data/translation-metrics.jsonl") },
};

export function loadConfig(path = resolve(PKG_DIR, "config.json")) {
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
  return cfg;
}
