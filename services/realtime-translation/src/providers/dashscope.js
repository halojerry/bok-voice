// Qwen-MT via Alibaba DashScope (cloud fallback translator for the B line).
// Requires DASHSCOPE_API_KEY; when absent the worker server falls back to
// the local Ollama translator.

const DEFAULT_OPTS = {
  apiKey: process.env.DASHSCOPE_API_KEY || "",
  model: "qwen-mt-turbo",
  endpoint: "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
  timeoutMs: 60000,
};

// Qwen-MT expects English full names; map our UI codes.
const LANG_NAME = {
  zh: "Chinese",
  yue: "Cantonese",
  en: "English",
  auto: "auto",
};

export class DashScopeTranslator {
  constructor(opts = {}) {
    this.opts = { ...DEFAULT_OPTS, ...opts };
    if (!this.opts.apiKey) {
      throw new Error("DashScopeTranslator requires DASHSCOPE_API_KEY");
    }
  }

  _lang(code) {
    return LANG_NAME[String(code || "").toLowerCase()] || code || "auto";
  }

  async translate(text, sourceLang, targetLang) {
    const res = await fetch(this.opts.endpoint, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.opts.apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: this.opts.model,
        input: { messages: [{ role: "user", content: [{ text }] }] },
        parameters: {
          translation_options: {
            source_lang: this._lang(sourceLang),
            target_lang: this._lang(targetLang),
          },
        },
      }),
      signal: AbortSignal.timeout(this.opts.timeoutMs),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`DashScope HTTP ${res.status}: ${detail.slice(0, 300)}`);
    }
    const data = await res.json();
    const choice = (data.output || {}).choices?.[0];
    const content = choice?.message?.content?.[0]?.text ?? choice?.message?.content?.[0] ?? "";
    const out = String(content).trim();
    if (!out) throw new Error("DashScope returned empty translation");
    return out;
  }
}
