// Translation provider for the local LLM. Supports two transports:
//  - OpenAI-compatible /v1/chat/completions (our bundled mlx_lm server, port 1235)
//  - Ollama native /api/chat (base_url without /v1)
// The mlx_lm server runs with enable_thinking=false, so replies are ~1s and
// content-only (same reasoning as the A-line fix).

const DEFAULT_OPTS = {
  baseUrl: "http://127.0.0.1:1235/v1",
  model: "/Users/halo/.lmstudio/models/huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit",
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
    this.baseUrl = this.opts.baseUrl.replace(/\/$/, "");
    this.openaiCompat = /\/v1$/.test(this.baseUrl);
  }

  async translate(text, sourceLang, targetLang) {
    const src = LANG_NAME[String(sourceLang || "").toLowerCase()] || sourceLang || "auto";
    const tgt = LANG_NAME[String(targetLang || "").toLowerCase()] || targetLang || "auto";
    const url = this.openaiCompat
      ? `${this.baseUrl}/chat/completions`
      : `${this.baseUrl}/api/chat`;
    const body = this.openaiCompat
      ? {
          model: this.opts.model,
          messages: [
            { role: "system", content: SYSTEM },
            {
              role: "user",
              content: `Source language: ${src}\nTarget language: ${tgt}\nText: ${text}`,
            },
          ],
          max_tokens: this.opts.numPredict,
          temperature: this.opts.temperature,
          stream: false,
        }
      : {
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
        };
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.opts.timeoutMs),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`Translator HTTP ${res.status}: ${detail.slice(0, 200)}`);
    }
    const data = await res.json();
    const out = this.openaiCompat
      ? String((data.choices?.[0]?.message || {}).content || "").trim()
      : String((data.message || {}).content || "").trim();
    if (!out) throw new Error("Translator returned empty translation");
    return out;
  }
}
