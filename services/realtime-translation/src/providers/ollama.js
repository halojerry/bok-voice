// Translation provider via the local Ollama native /api/chat endpoint.
// Uses think=false + a bounded num_predict (same reasoning as the A-line fix).

const DEFAULT_OPTS = {
  baseUrl: "http://127.0.0.1:11434",
  model: "huihui_ai/qwen3.5-abliterated:9b",
  think: false,
  numPredict: 512,
  temperature: 0.2,
  timeoutMs: 60000,
};

const SYSTEM = `You are a professional simultaneous-interpretation engine.
Translate the user's text from the source language to the target language.
Rules:
- Output ONLY the translation, no explanations, no quotes, no extra text.
- Output ONLY in the target language, never in the source language.
- Keep names, numbers, and technical terms where sensible.
- Preserve the original tone (casual/courteous).
- If the source is already in the target language, return it unchanged.`;

const LANG_NAME = {
  zh: "Chinese (Simplified)",
  yue: "Cantonese",
  en: "English",
  auto: "auto",
};

export class OllamaTranslator {
  constructor(opts = {}) {
    this.opts = { ...DEFAULT_OPTS, ...opts };
    this.baseUrl = this.opts.baseUrl.replace(/\/$/, "").replace(/\/v1$/, "");
  }

  async translate(text, sourceLang, targetLang) {
    const src = LANG_NAME[String(sourceLang || "").toLowerCase()] || sourceLang || "auto";
    const tgt = LANG_NAME[String(targetLang || "").toLowerCase()] || targetLang || "auto";
    const res = await fetch(`${this.baseUrl}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: this.opts.model,
        messages: [
          { role: "system", content: SYSTEM },
          {
            role: "user",
            content: `Source language: ${src}\nTarget language: ${tgt}\nText: ${text}`,
          },
        ],
        stream: false,
        think: this.opts.think,
        options: { num_predict: this.opts.numPredict, temperature: this.opts.temperature },
      }),
      signal: AbortSignal.timeout(this.opts.timeoutMs),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`Ollama HTTP ${res.status}: ${detail.slice(0, 200)}`);
    }
    const data = await res.json();
    const out = String((data.message || {}).content || "").trim();
    if (!out) throw new Error("Ollama returned empty translation");
    return out;
  }
}
