"""人设音色 map 解析（单源，A/B 线共用；2026-10-08 B 线人设音色复用波上收）。

历史：``_parse_voice_map`` 原住 agent.py（A 线专用）；B 线同传复用人设音色时
上收到 core——A/B 两线与 CP 侧消费同一对象，杜绝第二份拷贝漂移（分桶立法
同款纪律）。语义零变化：dict 直通；``{``开头字符串尝试 JSON（坏 JSON/非 dict
回落 ``{"zh": raw}``）；裸字符串=单音色进 zh 键；空=空 map。

分语言键只有 ``zh/cantonese/en``（A 线语言三态立法）；B 线四语目标
（de/fr/ja/pt）不在人设 map 内，由 B 线音色链回落设置/默认兜底。
"""

from __future__ import annotations

import json


def parse_voice_map(raw) -> dict:
    """人设 ``reference_audio`` → 分语言音色 map（纯函数）。

    - dict：原样直通（键=zh/cantonese/en）。
    - ``{"..."} `` 开头字符串：JSON 解析；坏 JSON/非 dict 回落 ``{"zh": raw}``。
    - 裸字符串（单音色 ID）：``{"zh": raw}``（下游语言缺省回落 zh=整场同声）。
    - None/空：空 map（消费方走自己的回退链）。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {"zh": raw}
        except Exception:  # noqa: BLE001 - 坏 JSON 回落单音色档
            return {"zh": raw}
    return {"zh": str(raw or "")}
