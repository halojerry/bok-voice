"""ASR 转写受限润色层（三语，2026-09-27 落地）。

四路调研定案的架构（详见 AGENTS.md 同日条）：
- **两层纠错**：①确定性音近吸附（``bok_voice_core.asr_polish``，仓内静态变体表
  assets/asr_variants.json，~1ms，``BOK_ASR_POLISH`` 默认开）；②小模型层
  （csc-sidecar :8792，MacBERT4CSC，zh-only——粤语碰模型必漂移係实测定案，
  ``BOK_CSC_SIDECAR`` 默认关 opt-in，按句级置信度门控触发）。
- **原文单轨铁律**：润色副本**只喂 LLM 上下文**（ContextState 冻结点替换，
  见 ContextAwareLLM.chat）；FlowController/WA 收号/QA 快路/意图规则/账本一律
  吃 raw。数字零降级在层内守卫（asr_polish 冻结数字 span；CSC 结果回来再过
  一道本地位数字段比对），不靠 prompt。
- **KV-cache 安全**：polish 确定性（同 raw 恒同 polished）；在 ContextAwareLLM
  冻结点一次性定形进 _applied_tails 账本，此后原样重放——严格前缀契约不破。

env（均入 bok.py ``_FORWARD_ENV``）：
- ``BOK_ASR_POLISH``（默认 "1"）确定性吸附总闸；
- ``BOK_CSC_SIDECAR``（默认 "0"）小模型层 opt-in；
- ``BOK_CSC_URL``（默认 http://127.0.0.1:8792）；
- ``BOK_CSC_CONF_GATE``（默认 "0.6"）置信度门——句级 conf.mean 高于阈值
  （或拿不到置信度时按保守「不过门」处理=照调，见 _csc_should_call）不调模型。
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from bok_voice_core.asr_polish import (
    PolishResult,
    detect_lane,
    load_variant_table,
    polish_transcript,
)

# 数字 run 提取（CSC 结果本地皮带检查用）：阿拉伯串 + 中文数字词。
_DIGIT_WORD_RE = re.compile(r"[0-9]+|[一二三四五六七八九零俩廿两十百千万亿]+")


def polish_enabled() -> bool:
    """确定性吸附总闸（默认开；关=LLM 上下文恒吃 raw，零行为变化）。"""
    return os.environ.get("BOK_ASR_POLISH", "1") == "1"


def csc_enabled() -> bool:
    """小模型层 opt-in（默认关——粤语漂移风险与 torch 依赖都要求先实弹验收）。"""
    return os.environ.get("BOK_CSC_SIDECAR", "0") == "1"


def _csc_url() -> str:
    return os.environ.get("BOK_CSC_URL", "http://127.0.0.1:8792").rstrip("/")


def _csc_conf_gate() -> float:
    try:
        return float(os.environ.get("BOK_CSC_CONF_GATE", "0.6"))
    except Exception:  # noqa: BLE001 - 坏值回默认
        return 0.6


# 静态资产只读缓存（区别于 settings 类可变全局——本表进程内不变，与
# _ASR_HOTWORDS 常量同级；load 失败置 None 永久降级为直通，不每轮重试 IO）。
_TABLE_CACHE: dict[str, dict[str, dict[str, list[str]]]] | None = None
_TABLE_LOADED = False


def _table() -> dict[str, dict[str, dict[str, list[str]]]] | None:
    global _TABLE_CACHE, _TABLE_LOADED
    if not _TABLE_LOADED:
        _TABLE_LOADED = True
        try:
            _TABLE_CACHE = load_variant_table()
        except Exception as exc:  # noqa: BLE001 - 资产缺席/损坏=层直通,唔阻通话
            print(f"ASR_POLISH table_load_failed {exc!r}", flush=True)
            _TABLE_CACHE = None
    return _TABLE_CACHE


def _lane_for(lang: str | None, text: str) -> str:
    """车道判定：通话语言优先（A 线整通钉死，勿逐段 sniff 切道），缺省回 detect。"""
    if lang in ("zh", "cantonese", "en"):
        return lang
    return detect_lane(text)


def sync_polish(text: str, lang: str | None = None) -> str:
    """确定性音近吸附（同步、纯本地、~1ms）——ContextAwareLLM 冻结点的兜底路径。

    只在吸附命中时打日志（ASR_POLISH lane= edits= before→after），无命中静默
    （每轮打空行会把 agent.log 淹掉）。"""
    if not text or not polish_enabled():
        return text
    table = _table()
    if not table:
        return text
    try:
        lane = _lane_for(lang, text)
        pr: PolishResult = polish_transcript(text, lane, table)
    except Exception as exc:  # noqa: BLE001 - 吸附失败直通原文
        print(f"ASR_POLISH_ERROR {exc!r}", flush=True)
        return text
    if pr.edits:
        for _start, _end, before, after in pr.edits:
            print(f"ASR_POLISH lane={pr.lane} {before!r} -> {after!r}", flush=True)
    return pr.text


def _digit_runs(text: str) -> list[str]:
    return _DIGIT_WORD_RE.findall(text or "")


def _csc_should_call(lang: str | None, text: str, confidence: Any) -> bool:
    if not csc_enabled() or not text:
        return False
    lane = _lane_for(lang, text)
    if lane != "zh":
        return False  # 粤语漂移/英语无模型——三语架构里模型层只接 zh
    if not (4 <= len(text) <= 200):
        return False
    if isinstance(confidence, dict):
        try:
            mean = float(confidence.get("mean") or 0.0)
        except (TypeError, ValueError):
            mean = 0.0
        # 高置信轮不烧模型往返；低置信/拿不到数值(confidence:None 键在)=照调
        # （None 语义=sidecar 关档或回退,不能反过来把整层关死）。
        if mean > _csc_conf_gate():
            return False
    return True


async def _csc_post(text: str) -> dict | None:
    """csc-sidecar /correct 调用（250ms 总超时,任何异常 fail-open 返回 None）。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=0.25) as client:
            r = await client.post(
                f"{_csc_url()}/correct",
                json={"text": text, "lang": "zh"},
            )
            r.raise_for_status()
            return r.json()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 模型层缺席/超时=只用确定性吸附结果
        print(f"CSC_POLISH skipped {exc!r}", flush=True)
        return None


async def polish_turn(text: str, lang: str | None = None, confidence: Any = None) -> str:
    """轮级润色入口（agent.py on_user_turn_completed 预计算调用）：

    确定性吸附 → （门控命中时）CSC 小模型二道 → 皮带检查（数字段比对，防
    服务端守卫之外的任何数字漂移）→ 返回最终润色文本。任何一层失败都只损失
    该层增益，绝不劣化原文。"""
    base = sync_polish(text, lang)
    if base == text and not _csc_should_call(lang, text, confidence):
        return base
    if not _csc_should_call(lang, base, confidence):
        return base
    try:
        resp = await _csc_post(base)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 双保险:_csc_post 内部已兜,monkeypatch
        # /非常规实现路径的异常也不能冒泡出润色层(轮钩子另有兜底,这里再一道)。
        print(f"CSC_POLISH skipped {exc!r}", flush=True)
        return base
    if not isinstance(resp, dict):
        return base
    out = str(resp.get("text") or "")
    edits = resp.get("edits") or []
    if not out or out == base or not edits:
        return base
    # 本地数字皮带：输入与输出的数字 run 序列必须逐一致——不一致整条弃用
    # （数字零降级是层内铁律,不信任任何远端守卫的完备性）。
    if _digit_runs(base) != _digit_runs(out):
        print(f"CSC_POLISH dropped digit_drift {base!r} -> {out!r}", flush=True)
        return base
    for e in edits[:3]:
        try:
            print(f"CSC_POLISH pos={e.get('pos')} {e.get('from')!r} -> {e.get('to')!r}", flush=True)
        except Exception:  # noqa: BLE001
            pass
    return out
