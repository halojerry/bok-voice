// Translation provider for the local OpenAI-compatible LLM (mlx_lm on macOS,
// llama-server on Windows, both on port 1235). The server runs with
// enable_thinking=false, so replies are fast and content-only.
//
// Zero-Ollama: there is no native /api/chat transport anymore; only the
// OpenAI-compatible /v1/chat/completions endpoint is used.

const DEFAULT_OPTS = {
  baseUrl: "http://127.0.0.1:1235/v1",
  model: "local",
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

export class LocalOpenAITranslator {
  constructor(opts = {}) {
    this.opts = { ...DEFAULT_OPTS, ...opts };
    this.baseUrl = this.opts.baseUrl.replace(/\/+$/, "");
    this.chatUrl = this.baseUrl.endsWith("/v1")
      ? `${this.baseUrl}/chat/completions`
      : `${this.baseUrl}/v1/chat/completions`;
  }

  async translate(text, sourceLang, targetLang) {
    const src = LANG_NAME[String(sourceLang || "").toLowerCase()] || sourceLang || "auto";
    const tgt = LANG_NAME[String(targetLang || "").toLowerCase()] || targetLang || "auto";
    const res = await fetch(this.chatUrl, {
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
        max_tokens: this.opts.numPredict,
        temperature: this.opts.temperature,
        stream: false,
      }),
      signal: AbortSignal.timeout(this.opts.timeoutMs),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`Translator HTTP ${res.status}: ${detail.slice(0, 200)}`);
    }
    const data = await res.json();
    const out = String((data.choices?.[0]?.message || {}).content || "").trim();
    if (!out) throw new Error("Translator returned empty translation");
    return out;
  }
}
