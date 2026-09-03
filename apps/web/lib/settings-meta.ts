"use client";

/**
 * 设置页字段元数据：每个设置项的 provider 下拉、字段级中文 label/说明/输入类型。
 * 只暴露 agent 运行时会真实消费的字段；误导性死配置不再展示。
 */

export type ProviderKind = "asr" | "llm" | "tts" | "vad";
export type FieldType = "text" | "number" | "select" | "secret";

export interface ProviderOption {
  value: string;
  label: string;
  hint?: string;
}

export interface FieldMeta {
  key: string;
  label: string;
  hint?: string;
  type: FieldType;
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  options?: ProviderOption[];
  /** 音色类字段：设置页可一键试听当前选中音色。 */
  preview?: boolean;
}

export interface ProviderMeta {
  kind: ProviderKind;
  title: string;
  desc: string;
  providers: ProviderOption[];
  fields: FieldMeta[];
}

export const SETTING_CARDS: ProviderMeta[] = [
  {
    kind: "asr",
    title: "ASR 语音识别",
    desc: "把客户语音转成文字。qwen3_asr 走本地 sidecar；sherpa_sensevoice 走 sherpa-onnx；fake 仅用于无模型测试。",
    providers: [
      { value: "qwen3_asr", label: "Qwen3-ASR（本地）" },
      { value: "sherpa_sensevoice", label: "SenseVoice（sherpa-onnx）" },
      { value: "fake", label: "Fake（仅测试）", hint: "无模型时用固定文本模拟识别。" },
    ],
    fields: [
      { key: "base_url", label: "服务地址", type: "text", hint: "ASR sidecar 地址；agent 运行时会优先读环境变量 QWEN3_ASR_BASE_URL。", placeholder: "http://127.0.0.1:8787" },
    ],
  },
  {
    kind: "llm",
    title: "LLM 大模型",
    desc: "对话生成引擎。本地（mlx / local_openai）开箱即用；deepseek 需填云端 API Key。",
    providers: [
      { value: "local_openai", label: "本地 LLM（mlx_lm / llama-server）" },
      { value: "mlx", label: "MLX（macOS）" },
      { value: "deepseek", label: "DeepSeek（云端）", hint: "需在下方填写 API Key；不填会自动回退本地 LLM。" },
      { value: "fake", label: "Fake（仅测试）", hint: "固定脚本回复。" },
    ],
    fields: [
      { key: "model", label: "模型名", type: "text", hint: "本地默认走环境变量注入的真实模型路径，可留空；deepseek 填如 deepseek-chat。", placeholder: "deepseek-chat" },
      { key: "base_url", label: "服务地址", type: "text", hint: "本地可留空（启动器已注入 http://127.0.0.1:1235/v1）；deepseek 填 https://api.deepseek.com/v1。", placeholder: "http://127.0.0.1:1235/v1" },
      { key: "api_key", label: "API Key", type: "secret", hint: "仅 deepseek 需要；已保存的 Key 不会回显。", placeholder: "sk-…" },
    ],
  },
  {
    kind: "tts",
    title: "TTS 语音合成",
    desc: "把 AI 回复念出来。qwen3_tts 走本地 sidecar；volcano 走火山引擎；minimax 走 MiniMax 云端（粤语地道、情感自然）；fake 出静音测试音。",
    providers: [
      { value: "qwen3_tts", label: "Qwen3-TTS（本地）" },
      { value: "volcano_streaming", label: "火山引擎（云端）", hint: "凭据读 VOLC_APP_ID / VOLC_ACCESS_TOKEN 环境变量。" },
      { value: "minimax", label: "MiniMax（云端）", hint: "在下方「API Key」填入持久化凭据（保存后重启 agent 生效）；也可用环境变量 MINIMAX_API_KEY 覆盖。国内 api.minimax.cn / 海外 api.minimax.chat。" },
      { value: "fake", label: "Fake（仅测试）", hint: "静音测试音，无模型也可跑通链路。" },
    ],
    fields: [
      { key: "api_key", label: "API Key", type: "secret", hint: "MiniMax / DeepSeek 等云端凭据，持久化保存（重启不丢）；已保存的 Key 不回显。", placeholder: "sk-…" },
      { key: "base_url", label: "服务地址", type: "text", hint: "Qwen3-TTS sidecar 地址；agent 运行时会优先读 QWEN3_TTS_BASE_URL。", placeholder: "http://127.0.0.1:8788" },
      { key: "speaker_zh", label: "普通话音色", type: "text", preview: true, hint: "persona 未绑音色时的默认音色；MiniMax 如 zhiyan_meet_feminine。", placeholder: "如 zhiyan_meet_feminine" },
      { key: "speaker_yue", label: "粤语音色", type: "select", preview: true, hint: "客户切粤语时使用。MiniMax 粤语音色（实测可用）；选「跟随普通话」则粤语轮用普通话音色。", options: [
        { value: "", label: "跟随普通话（不单独设粤语）" },
        { value: "Cantonese_Male_news_anchor_vv2", label: "男主播 news_anchor_vv2" },
        { value: "Cantonese_ProfessionalHost（F)", label: "专业女主持（F）" },
        { value: "Cantonese_ProfessionalHost（M)", label: "专业男主持（M）" },
        { value: "Cantonese_GentleLady", label: "温柔女声 GentleLady" },
        { value: "Cantonese_PlayfulMan", label: "活泼男声 PlayfulMan" },
        { value: "Cantonese_CuteGirl", label: "可爱女孩 CuteGirl" },
        { value: "Cantonese_KindWoman", label: "善良女声 KindWoman" },
      ] },
      { key: "speaker_en", label: "英语音色", type: "text", hint: "客户切英语时使用；留空则沿用普通话音色。" },
      { key: "instruct", label: "语气指令（可选）", type: "text", hint: "附加到每次合成的情绪指令之前。", placeholder: "如：温和、耐心" },
      { key: "sample_rate", label: "采样率", type: "number", hint: "输出 PCM 采样率，通常保持 24000。", min: 8000, max: 48000, step: 1000 },
    ],
  },
  {
    kind: "vad",
    title: "VAD 语音活动检测与打断",
    desc: "判断客户何时开始/结束说话，以及 AI 说话时能否被打断。修改后在下一次通话生效。",
    providers: [
      { value: "silero", label: "Silero VAD（本地）" },
      { value: "fake", label: "Fake（仅测试）", hint: "无模型时按固定节奏模拟说话/静音。" },
    ],
    fields: [
      { key: "max_buffered_speech", label: "单句最长缓冲（秒）", type: "number", hint: "客户连续说话超过该时长会强制切句，避免缓冲溢出。", min: 1, max: 120, step: 0.5 },
      { key: "min_speech_duration", label: "最短说话时长（秒）", type: "number", hint: "短于此的杂音不当作一句话。", min: 0.05, max: 2, step: 0.05 },
      { key: "min_silence_duration", label: "判定结束的静音时长（秒）", type: "number", hint: "客户停顿超过该时长视为一句话说完。调小更灵敏，调大可减少误切。", min: 0.05, max: 3, step: 0.05 },
      { key: "interruption", label: "允许打断 AI 说话", type: "select", hint: "关闭后 AI 说话期间客户无法打断（适合朗读类场景）。", options: [
        { value: "true", label: "开启" },
        { value: "false", label: "关闭" },
      ] },
    ],
  },
];

export const POLICY_META = {
  title: "运行策略",
  desc: "默认本地优先；选择云端优先需要已配置云端 provider（如 DeepSeek / 火山引擎）。",
  options: [
    { value: "offline_first", label: "本地优先（推荐）", hint: "优先使用本地 ASR/LLM/TTS；未配置云端时全部走本地。" },
    { value: "cloud_first", label: "云端优先", hint: "优先使用云端 provider；缺凭据时回退本地。" },
  ],
};

export const CARD_TITLE: Record<ProviderKind, string> = {
  asr: "ASR 语音识别",
  llm: "LLM 大模型",
  tts: "TTS 语音合成",
  vad: "VAD 与打断",
};

export const DEFAULT_PROVIDER: Record<ProviderKind, string> = {
  asr: "qwen3_asr",
  llm: "local_openai",
  tts: "qwen3_tts",
  vad: "silero",
};
