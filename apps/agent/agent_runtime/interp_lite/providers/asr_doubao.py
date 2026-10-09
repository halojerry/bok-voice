"""interp_lite 豆包 ASR：官方参数档子类（在役 provider 之上的 docs-first 薄层）。

官方文档基线（本地副本 ``/Users/halo/Documents/bok/huoshan-api_副本.md``，2026-10-09 核对）：
- 端点：``wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async``（协议帧/鉴权/
  VAD 段会话模型由在役 ``providers/doubao_asr.py`` 实现——2026-10-03 实弹装线件，
  帧格式与官方 demo protocol.py 语义同源，本子类零协议改动）。
- 本层开的官方参数（旧线默认关、各自臂后定的 W3b 三臂，见计划档 §2 替代表）：
  - ``enable_nonstream=True`` 二遍识别——官方推荐开：句末对整段重识别，分句最终
    结果更准（实时上屏不变）。提交边界仍=「我们 VAD 段会话 + 末包负 seq 定稿」
    （实弹定案：0.28-0.45s 本地静音先于服务端 800ms end_window，结构性用不上它）。
  - ``force_to_speech_time=1000``——官方推荐值：起始静音/弱音不再过早判停（对译
    起始吃字，官方护栏替代旧线的手搓起始守卫）。
  - ``enable_accelerate_text``/``accelerate_score``——首字加速官方臂，沿用既有键
    ``BOK_DOUBAO_FIRST_TOKEN_BOOST``（不新设键；旧线同键同语义）。
- 明确**不开**：``enable_ddc`` 语义顺滑——官方删语气词，与语气标记 v2 冲突
  （旧线臂注同判）；``end_window_size`` 不设（段会话模型下不参战）。

旧 provider 文件零改动（子类覆写 ``_config()``）；``clause_commit``/``utt_merge``
恒 False——提交闸全家由官方参数+段会话边界替代（计划档 §8 不带项审计表）。
"""

from __future__ import annotations

import os

from ...providers.doubao_asr import DOUBAO_RESOURCE_DEFAULT, DOUBAO_WS_DEFAULT, DoubaoSTT


class LiteDoubaoSTT(DoubaoSTT):
    """薄线豆包 STT = 在役类 + 官方参数档（model 名区分观测行）。"""

    model = "doubao-asr-lite"

    def __init__(self, **kwargs):
        # 提交闸全家显式关死：这些是本地 ASR 时代的补偿，薄线由官方参数接手。
        kwargs.setdefault("clause_commit", False)
        kwargs.setdefault("utt_merge", False)
        super().__init__(**kwargs)

    def _config(self) -> dict:
        cfg = super()._config()
        req = cfg.setdefault("request", {})
        req["enable_nonstream"] = True
        req.setdefault("force_to_speech_time", 1000)
        # 首字加速官方臂沿用既有键（缺省关；旧线同键，跨线零新键）。
        if os.environ.get("BOK_DOUBAO_FIRST_TOKEN_BOOST", "") == "1":
            req["enable_accelerate_text"] = True
            req.setdefault("accelerate_score", 3)
        return cfg


def build(asr_cfg: dict, *, language_state, hotword_terms: list[str], vad_) -> LiteDoubaoSTT | None:
    """装配（凭据/端点=设置面 asr 段；缺凭据返回 None=调用方按 cloud-only 拒装）。"""
    api_key = str(asr_cfg.get("api_key") or "").strip()
    old_auth = bool(str(asr_cfg.get("app_id") or "").strip()) and bool(
        str(asr_cfg.get("access_token") or "").strip()
    )
    if not api_key and not old_auth:
        return None
    return LiteDoubaoSTT(
        api_key=api_key,
        resource_id=str(asr_cfg.get("resource_id") or "").strip() or DOUBAO_RESOURCE_DEFAULT,
        ws_url=str(asr_cfg.get("endpoint") or "").strip() or DOUBAO_WS_DEFAULT,
        app_id=str(asr_cfg.get("app_id") or "").strip(),
        access_token=str(asr_cfg.get("access_token") or "").strip(),
        language_state=language_state,
        hotword_terms=hotword_terms,
        vad_=vad_,
    )
