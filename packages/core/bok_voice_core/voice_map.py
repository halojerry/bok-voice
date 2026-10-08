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


def collapse_voice_map(raw_map: dict, persona_lang: str) -> dict:
    """整场同声：把 {zh,cantonese,en} 分语言 map 收敛成单一主音色（纯函数）。

    主音色取人设主语言（persona.language）对应键，缺则按 zh→cantonese→en→首个非空
    取；最终统一放进 zh 键（MiniMax/Qwen3 的 _resolve_voice 语言缺省都回落 zh），
    使整场无论讲粤/普/英都用同一把声。回退点：若想恢复「按语言分音色」，
    删掉本函数调用、直接传 raw_map 即可。语言值全时空统一 cantonese（旧值
    已由 CP 启动迁移清零，这里不再兜别名）。

    2026-10-08 上收 core（原住 agent.py）：B 线同传「三语言=A 线同款人设音色」
    （用户拍板整场同声语义）与 A 线消费同一对象。
    """
    if not raw_map:
        return {}
    lang = (persona_lang or "").strip().lower()
    if lang not in {"zh", "cantonese", "en"}:
        lang = ""
    picked = ""
    for key in ([lang] if lang else []) + ["zh", "cantonese", "en"]:
        if raw_map.get(key):
            picked = str(raw_map[key])
            break
    if not picked:
        for v in raw_map.values():
            if v:
                picked = str(v)
                break
    return {"zh": picked} if picked else {}
