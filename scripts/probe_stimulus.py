"""探针/E2E 客户话音刺激源单点开关(2026-09-21)。

是什么
------
E2E 与各类 probe 脚本都要把「客户说的话」合成成音频再推进 LiveKit 房间。
历史上这唯一走本地 TTS sidecar（`http://127.0.0.1:8788` 的
`/v1/audio/speech`，预设音色 `Vivian`，16kHz PCM）。本模块把这层合成收成
单点：`stimulus_pcm(text, lang)`，外加一个纯选择器
`resolve_stimulus_backend(env)`。

为什么要有它
------------
计划中的一次收尾改动是**退役本地 TTS 轨**（见
`docs/superpowers/plans/2026-09-21-a-line-speed-asr-decision-verification.md`
§24.1 / §29.2）。但该改动一直被挡住——本地 TTS sidecar 是全部 E2E/probe
台架**唯一的客户话音来源**，砍掉它等于砍掉真栈验收能力。本模块补上缺失的
那一环：让台架既能走本地（默认，逐字节不变），也能按环境变量改走**云端**
`scripts/mm_voice.mm_pcm`（t2a_v2，生产音色，与线上同源）。有了这条云端
腿，本地 TTS 的退役才不再等于丢掉真栈验证。**真正的切换是之后一次单独的、
刻意的决定**，不是本模块自动完成的。

切换是换基线（重要）
--------------------
改变客户话音的来源 = 改变刺激信号 = 改变探针读数。`mm_voice.py` 自己就
记录过：本地 Qwen3-TTS 的粤语短词 ASR 可懂度差（实测「拼多多」→「二。二。」），
云端 MM 合成回读才好。所以**换后端前后的 probe/E2E 数字不可直接对比**——
先把旧后端读数当基线记下来，再切，再重新取基线。默认值恒为 `local`，
任何未知/空值都安全回落到 `local`，绝不静默改基线。

怎么刻意切换
------------
    BOK_PROBE_STIMULUS=cloud python scripts/e2e_real_customer.py ...
    BOK_PROBE_STIMULUS=local python scripts/e2e_real_customer.py ...   # 显式回默认

未设该 env = `local`（与历史行为逐字节一致）。`cloud` 需要设置 DB 里配好
MiniMax key（mm_voice 自会读，不落明文）。
"""
from __future__ import annotations

import os
from typing import Mapping

ENV_SWITCH = "BOK_PROBE_STIMULUS"

DEFAULT_TTS_URL = "http://127.0.0.1:8788"
DEFAULT_VOICE = "Vivian"

# 云端腿：复用 scripts/mm_voice.py 的 mm_pcm（不复制其实现）。
# 两种导入姿势：直接跑脚本时 sys.path[0]=scripts/（flat），
# 从仓库根/包内导入时走 scripts.mm_voice。import 本身无网络、无副作用
# （certifi/urllib 都在 mm_pcm 调用时才用）。
try:  # scripts/ 直接执行
    from mm_voice import mm_pcm as _mm_pcm
except ImportError:  # 仓库根/包内导入
    from scripts.mm_voice import mm_pcm as _mm_pcm  # type: ignore


def resolve_stimulus_backend(env: Mapping[str, str] | None = None) -> str:
    """纯函数：按 `BOK_PROBE_STIMULUS` 选后端，只认 `cloud`，其余一律 `local`。

    `env` 缺省读 `os.environ`；显式传入便于离线测试。
    """
    src = os.environ if env is None else env
    value = (src.get(ENV_SWITCH) or "").strip().lower()
    return "cloud" if value == "cloud" else "local"


def _local_pcm(
    text: str, lang: str, voice: str, tts_url: str, sample_rate: int, timeout: float
) -> bytes:
    """本地 TTS sidecar 合成 16k PCM——与各脚本历史 payload 逐字节同款。"""
    import httpx

    with httpx.Client(timeout=timeout) as client:
        r = client.post(
            f"{tts_url}/v1/audio/speech",
            json={"input": text, "language": lang, "voice": voice, "sample_rate": sample_rate},
        )
        r.raise_for_status()
        return r.content


def stimulus_pcm(
    text: str,
    lang: str,
    *,
    voice: str | None = None,
    tts_url: str | None = None,
    sample_rate: int = 16000,
    timeout: float = 60.0,
) -> bytes:
    """合成一段客户话音 PCM（16k, mono, int16）。

    后端由 `BOK_PROBE_STIMULUS` 决定（见 `resolve_stimulus_backend`）：

    - `local`（默认）：POST `{tts_url}/v1/audio/speech`，payload 与历史完全一致；
      `voice` 缺省 `"Vivian"`、`tts_url` 缺省读 `TTS_URL` env（再回退 8788）。
    - `cloud`：委托 `scripts/mm_voice.mm_pcm(text, lang)`（生产音色）。此模式下
      `voice`/`tts_url`/`sample_rate`/`timeout` 不参与（云端音色按 lang 映射）。
    """
    if resolve_stimulus_backend() == "cloud":
        return _mm_pcm(text, lang)

    url = tts_url if tts_url is not None else os.environ.get("TTS_URL", DEFAULT_TTS_URL)
    resolved_voice = voice if voice is not None else DEFAULT_VOICE
    return _local_pcm(text, lang, resolved_voice, url, sample_rate, timeout)
