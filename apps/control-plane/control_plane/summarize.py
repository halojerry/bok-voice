from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

# E7 离线润色接线（2026-09-21）：润色**只**作用于喂 LLM 的纪要 prompt 文本
# （``_render_transcript``），落盘的 ``transcript.md`` 原件（main._write_settlement_docs）
# 逐字不碰——那是原始证据面。kill-switch ``BOK_POLISH_OFFLINE`` 默认关（见
# ``polish_wiring`` 模块 docstring 的实测理由）。
from bok_voice_core.deepseek_llm import thinking_extra_body
from bok_voice_core.json_repair import loads_lenient
from bok_voice_core.polish_wiring import polish_offline_text


_SYSTEM = (
    "你是电话客服质检助手。给定一场对话的逐轮文本，提炼：\n"
    "1) 一段 3-5 句的对话总结；\n"
    "2) 你认为是值得沉淀的关键点/新话题（每个一句，含对象关注点、异议、需求）；\n"
    "3) 一条全局洞察（statement：反映该对象群共性的观察；confidence：0~1）。\n"
    "只输出 JSON，格式："
    '{"summary":"...","new_topics":[{"topic":"...","summary":"..."}],'
    '"insight":{"statement":"...","confidence":0.8,"language":"zh"}}'
)

# 同传会话(B 线,kind=interpret)专用:逐轮是「原文：/译文：」双语对照行,没有
# 客服对象——纪要框架对齐会议同传场景(Good-Interpreter 调研项:赛后总结喂
# 双语对照,2026-09-16 P1 落地)。总结用中文写,引用发言标注「我方/对方」。
_SYSTEM_INTERP = (
    "你是双语会议同传纪要助手。给定一场同传会话的逐轮文本（每轮含「原文：」与"
    "「译文：」两行，原文=说话人原话，译文=给另一方的翻译；说话人只分「我方/对方」），"
    "提炼：\n"
    "1) 一段 3-5 句的中文对话纪要（双方各说了什么、达成什么，引用时标注「我方/对方」）；\n"
    "2) 值得沉淀的关键点/待办（每个一句，含承诺、数字、时间等硬信息）；\n"
    "3) 一条全局洞察（statement：此类同传会话的共性观察；confidence：0~1）。\n"
    "只输出 JSON，格式："
    '{"summary":"...","new_topics":[{"topic":"...","summary":"..."}],'
    '"insight":{"statement":"...","confidence":0.8,"language":"zh"}}'
)


class Summarizer:
    """Runs conversation → summary/topics/insight through the configured LLM.

    Best-effort: any LLM failure falls back to a deterministic metrics-only
    summary so settlement never blocks on the model.
    """

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def build(self, turns: list[Any], call: dict, settings: dict) -> dict:
        """Return {summary, new_topics, insight} derived from ``turns``."""
        transcript = self._render_transcript(turns)
        if not transcript:
            return {"summary": "", "new_topics": [], "insight": None}
        llm_cfg = settings.get("llm", {}) or {}
        base_url = (llm_cfg.get("base_url") or "").rstrip("/")
        model = (llm_cfg.get("model") or "").strip()
        # settle 专线优先(BOK_SETTLE_*,bok.py 注入指向 :1237 9B):纪要/蒸馏係
        # 延迟不敏感的后台重活,大模型质量↑且与活通话的 :1235 完全隔离;env
        # 缺席回退原链路(settings llm 卡 > MLX_LLM_* env,语义同旧)。
        _settle_base = os.environ.get("BOK_SETTLE_LLM_BASE_URL", "").strip()
        _settle_model = os.environ.get("BOK_SETTLE_LLM_MODEL", "").strip()
        api_key = (llm_cfg.get("api_key") or "").strip()
        if _settle_base and _settle_model:
            base_url, model = _settle_base.rstrip("/"), _settle_model
            # 专线若指向云端（DeepSeek 等）必须有凭据——本地 MLX 不校验时这是个空串，
            # 语义不变。凭据只走 env（与 BOK_SETTLE_LLM_* 同款 CP 面注入），不落盘。
            api_key = os.environ.get("BOK_SETTLE_LLM_API_KEY", "").strip() or api_key
        # `"mlx"` 是本仓既有的「本地端点不校验凭据」哨兵（与 agent `_llm_judge` 的
        # api_key 缺省同值）——设置页本地卡就存这个字面量，它**不是**凭据，不许变成
        # Authorization 头（否则本地档的请求形状也变了）。
        if api_key == "mlx":
            api_key = ""
        # 设置页 LLM 卡片可存空 base_url / 占位 model="local"；本机 MLX 的真实地址
        # 由启动器经 env 注入（与 agent 的 MlxLlmLLM 同一来源）。只读 settings 会打到
        # 空 URL / model=local → mlx_lm 404 → 蒸馏表（new_topics/insight）永不写入。
        if not base_url or not model or model == "local":
            env_base = os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1").rstrip("/")
            env_model = (os.environ.get("MLX_LLM_MODEL") or "").strip()
            if env_base and env_model:
                base_url, model = env_base, env_model
        if not base_url or not model:
            return self._fallback(turns)
        try:
            system = _SYSTEM_INTERP if str(call.get("kind") or "") == "interpret" else _SYSTEM
            return self._via_llm(base_url, model, transcript, call, system, api_key)
        except Exception as exc:  # pragma: no cover - model/network failure
            print(f"[summarize] LLM summary failed, falling back: {exc!r}", flush=True)
            return self._fallback(turns)

    def _via_llm(
        self,
        base_url: str,
        model: str,
        transcript: str,
        call: dict,
        system: str = _SYSTEM,
        api_key: str = "",
    ) -> dict:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"通话对象：{call.get('object_id','')}\n对话：\n{transcript}",
                },
            ],
            "max_tokens": 512,
            "temperature": 0.2,
            "stream": False,
        }
        # 沉淀/纪要是**非实时**后台重活：思考开着更准（2026-09-21 口径——对话侧关思考
        # 换首字延迟，纪要侧不动它）。但思考与正文**共用** max_tokens 预算，512 不够时
        # 会整段烧在 reasoning 上、content 出空串，而这里落地是静默 ``_fallback``
        # （指标摘要，质量无声降级）——故思考档下把预算抬到容得下「思考 + JSON 正文」。
        # 本地 MLX 端点该片段为空 dict，payload 逐字节同旧。
        #
        # **超时也得跟着抬**（2026-09-21 实测）：思考档下真跑一次要 9-14s 起，而
        # ``self.timeout`` 缺省 15s——云端 v4-pro 档实测 5/5 全部 ReadTimeout
        # （`ReadTimeout('The read operation timed out')`），即「纪要换云」光抬预算
        # 不抬超时**结构上跑不通**。纪要本来就离线，放宽无代价。
        thinking_body = thinking_extra_body(base_url, "enabled")
        timeout = self.timeout
        if thinking_body:
            payload.update(thinking_body)
            payload["max_tokens"] = 2048
            timeout = max(timeout, float(os.environ.get("BOK_SETTLE_THINKING_TIMEOUT_S", "90")))
        # 无凭据时不传 `headers` kwarg（而非传 None）：本地档的调用形状逐字节同旧，
        # 既有以三参签名桩 httpx.post 的测试/调用方零改动。
        post_kwargs: dict[str, Any] = {"json": payload, "timeout": timeout}
        if api_key:
            post_kwargs["headers"] = {"Authorization": f"Bearer {api_key}"}
        r = httpx.post(f"{base_url}/chat/completions", **post_kwargs)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"].get("content", "")
        return self._parse(content)

    def _parse(self, content: str) -> dict:
        text = content.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            # 可观测性（2026-09-21）：这里以前是**静默** `_fallback`，所以「settle 模型
            # 换了以后沉淀成片丢」在观测面完全看不见——本次实测（本机 9B @1237）就是
            # 靠这个盲区藏了很久。空稿要留痕，别只留结果。
            print(f"[summarize] 模型没吐 JSON（content {len(text)} 字，头部：{text[:80]!r}）→ 退指标摘要", flush=True)
            return self._fallback([])
        try:
            data = json.loads(m.group(0))
        except Exception as exc:  # noqa: BLE001 - 坏 JSON 是模型输出问题，可见即可
            # 「内容全对、只漏了最外层一个 `}`」是本机 9B 的**主要坏法**（2026-09-21
            # 真实转写 6/6 复现，见 bok_voice_core.json_repair 模块 docstring）。
            # 先试保守补括号再判失败——不然这些**内容完好**的纪要会整批退成桩文本。
            repaired = loads_lenient(m.group(0))
            if repaired is not None:
                print(
                    f"[summarize] 模型 JSON 漏收尾 → 补括号救回（content {len(text)} 字，"
                    f"原错：{exc}）",
                    flush=True,
                )
                data = repaired
            else:
                print(
                    f"[summarize] 模型吐的 JSON 解析失败且补不回来（content {len(text)} 字）：{exc} "
                    f"→ 退指标摘要（new_topics/insight 全丢）",
                    flush=True,
                )
                return self._fallback([])
        return {
            "summary": str(data.get("summary", "")),
            "new_topics": list(data.get("new_topics", [])),
            "insight": data.get("insight") or None,
        }

    @staticmethod
    def _render_transcript(turns: list[Any], max_chars: int = 6000) -> str:
        """turns → 纪要 prompt 文本；每轮文本过 E7 离线润色（唯一接线点）。

        只润色这一份**派生**文本（本地变量，喂 LLM）；原始转写仍在 turns 账本与
        ``transcript.md`` 原件里逐字保留。kill-switch 关/润色异常时逐字原样。
        """
        lines: list[str] = []
        total = 0
        for t in turns:
            role = getattr(t, "role", None) or getattr(t, "role", "?")
            text = getattr(t, "transcript", "") or getattr(t, "text", "")
            line = f"{role}: {polish_offline_text(text)}"
            lines.append(line)
            total += len(line)
            if total >= max_chars:
                break
        return "\n".join(lines)

    @staticmethod
    def _fallback(turns: list[Any]) -> dict:
        texts = [getattr(t, "transcript", "") for t in turns if getattr(t, "transcript", "")]
        if not texts:
            return {"summary": "", "new_topics": [], "insight": None}
        # deterministic fallback: first/last key quote + user-turn count
        user = [t for t in texts]
        heads = [x[:60] for x in user[:2]]
        return {
            "summary": f"本场共 {len(user)} 轮。主要内容：{'；'.join(heads)}",
            "new_topics": [],
            "insight": None,
        }
