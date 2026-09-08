"""LLM 慢生成轮垫话(2026-09-09,PR-2;设计见 docs/superpowers/specs/2026-09-08-tts-cache-design.md)。

只垫「必须 LLM 临场生成」的轮次(话术缓存未命中、hook 正常返回走 LLM):
回复首音频 BOK_FILLER_DELAY_MS(默认 700ms)未到 → 播一句预合成应承语,
真回复首音频到达 → 定向 interrupt 垫话(CachedTTS 首音频回调驱动)。

铁律:
- 快轮永远不垫(定时器被首音频回调作废,零打扰);
- 垫话绝不触发云合成——缓存未命中直接跳过(垫话库靠 tts-pregen --fillers 预生成);
- add_to_chat_ctx=False:垫话不进 LLM 上下文(KV 前缀不变、4B 不被锚定、
  last_reply 回声锚不被污染);
- 每通限次 BOK_FILLER_MAX(默认 2)+轮换防机械;BOK_FILLER=0 一键全关。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os

# 默认垫话骨架(最终话术可经 BOK_FILLER_LINES JSON 覆盖;用户拍板后改这里也行)。
DEFAULT_FILLER_LINES: dict[str, tuple[str, ...]] = {
    "zh": ("好的，您稍等。", "我看一下哈。"),
    "cantonese": ("好，等我睇下。", "好，你等陣。"),
    "en": ("Sure, let me check.", "One moment please."),
}


def filler_enabled() -> bool:
    return os.environ.get("BOK_FILLER", "1") == "1"


def filler_delay_s() -> float:
    try:
        return max(0.0, int(os.environ.get("BOK_FILLER_DELAY_MS", "700")) / 1000)
    except ValueError:
        return 0.7


def filler_max_per_call() -> int:
    try:
        return max(0, int(os.environ.get("BOK_FILLER_MAX", "2")))
    except ValueError:
        return 2


def filler_lines() -> dict[str, tuple[str, ...]]:
    raw = os.environ.get("BOK_FILLER_LINES", "").strip()
    if not raw:
        return DEFAULT_FILLER_LINES
    try:
        data = json.loads(raw)
        return {str(k): tuple(str(x) for x in v) for k, v in data.items() if v}
    except Exception:  # noqa: BLE001 - 配置坏了回落默认
        return DEFAULT_FILLER_LINES


class FillerDirector:
    """垫话编排:arm(轮提交后) → 定时器 → 缓存命中即播;首音频回调 → 作废/打断。

    纯编排无业务判定——「该不该垫」的轮次资格由调用方决定(hook 正常返回才
    arm;closing/WA 步由 guards 回调在开火前最后一刻复核)。
    """

    def __init__(
        self,
        session,
        tts_provider,
        cache,
        *,
        lang_resolver,
        guards=None,
    ) -> None:
        self._session = session
        self._tts = tts_provider
        self._cache = cache
        self._lang_resolver = lang_resolver
        self._guards = guards or (lambda: False)
        self._timer: asyncio.Task | None = None
        self._handle = None
        self._count = 0
        self._idx = 0

    # ---- 生命周期 ----

    def arm(self) -> None:
        """轮提交、确认走 LLM 正常路径后调用;重复 arm 先作废旧定时器。"""
        self._cancel_timer()
        if not filler_enabled() or self._count >= filler_max_per_call():
            return
        if self._cache is None or self._tts is None:
            return
        delay = filler_delay_s()
        if delay <= 0:
            return
        self._timer = asyncio.create_task(self._fire(delay))

    def on_reply_first_audio(self) -> None:
        """真回复首音频(CachedTTS stream 回调):作废定时器 + 打断在播垫话。"""
        self._cancel_timer()
        self._interrupt_playing()

    def cancel(self) -> None:
        """新用户轮到达等场景:作废定时器(在播垫话交给官方打断机制处理)。"""
        self._cancel_timer()

    def reset_per_call(self) -> None:
        self._count = 0
        self._idx = 0
        self._cancel_timer()
        self._handle = None

    # ---- 内部 ----

    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    def _interrupt_playing(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            done = getattr(handle, "done", None)
            if callable(done) and done():
                return
            handle.interrupt()
        except Exception:  # noqa: BLE001 - 打断失败让垫话自然播完(短语 ≤1.2s)
            pass

    def _pick_line(self, lang: str) -> str:
        lines = filler_lines().get(lang) or filler_lines().get("zh") or ()
        if not lines:
            return ""
        line = lines[self._idx % len(lines)]
        self._idx += 1
        return line

    async def _fire(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._timer = None
        try:
            if self._guards() or filler_max_per_call() <= self._count:
                return
            state = str(getattr(self._session, "agent_state", "") or "")
            if state not in ("listening", "thinking", ""):
                return  # 别的东西在播/状态不明,唔叠音
            lang = self._lang_resolver()
            line = self._pick_line(lang)
            if not line:
                return
            voice = getattr(self._tts, "resolved_voice", lambda: "")()
            model = getattr(self._tts, "resolved_model", lambda: "")()
            pcm = self._cache.lookup(line, voice=voice, model=model)
            if pcm is None:
                return  # 垫话库未预生成/音色变更——跳过,绝不触发云合成
            from .tts_cache import frames_aiter, pcm_to_frames

            frames = pcm_to_frames(pcm, self._cache.sample_rate)
            self._count += 1
            print(f"BOK_FILLER fired count={self._count} line={line!r}", flush=True)
            # say() 同步返回 SpeechHandle(await 只係等播完)——必须同步先记句柄,
            # on_reply_first_audio 喺播报期间先至打断得到;晚一步句柄拿唔到。
            started = self._session.say(line, audio=frames_aiter(frames), add_to_chat_ctx=False)
            if inspect.isawaitable(started):  # 兼容测试替身/未来签名
                started = await started
            self._handle = started

            def _clear(_f, *, _self=self, _h=started) -> None:
                if _self._handle is _h:
                    _self._handle = None

            try:
                started.add_done_callback(_clear)
            except Exception:  # noqa: BLE001 - 替身句柄冇回调就等下一轮覆盖
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 垫话失败唔阻通话
            print(f"BOK_FILLER error err={exc!r}", flush=True)
