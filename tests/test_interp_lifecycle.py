"""B 线生命周期韧性单测(2026-09-27):TTS 硬失败兜底 / MT 超时兜底 / 背压弃句观测。

背景三案(整通静默实证):
- call-b347f691 等:云端 MiniMax SSL 校验失败 / 「AudioEmitter isn't started」
  bidi 错误 → 整通 8 源句 0 译文出声。修=_build_tts_provider 按 A 线同款官方
  FallbackAdapter 包裹(主档 MiniMax、备档本地 Qwen3-TTS)。
- call-4fda36e0 等:MT wait_for(15s) 超时被 _mt_say_worker 静默吞(21 次)→ 改按
  目标语说中性请示句 + `MT_TIMEOUT_FALLBACK` 观测行。
- _PlaybackBacklog 弃句在 DB 与已播句不可分 → 弃句时打 `BACKLOG_DROP` 一行。

纯装配/纯函数直喂,唔启 worker;worker 分支用源码 pin + 超时异常可达性双证。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from agent_runtime import interpret


# ==================== TASK 1: TTS 硬失败兜底 ====================


def test_build_tts_provider_minimax_wraps_fallback(monkeypatch, capsys):
    """MiniMax 档 = 官方 FallbackAdapter(主档 MiniMax、备档本地 Qwen3-TTS)。

    2026-09-27:云端 SSL/bidi 硬失败曾整通零出声,现按 A 线同款回退姿势包裹。
    装配点打一行 `tts fallback armed primary=minimax backup=qwen3_local`。"""
    from livekit.agents import tts as agents_tts
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS, Qwen3TTSTTS

    monkeypatch.delenv("BOK_LOCAL_TTS", raising=False)
    provider = interpret._build_tts_provider(
        {"provider": "minimax", "api_key": "k-test", "speaker_zh": "v-zh"}, "zh"
    )
    assert isinstance(provider, agents_tts.FallbackAdapter)
    primary, backup = provider._tts_instances[0], provider._tts_instances[1]
    assert isinstance(primary, MiniMaxTTS)
    assert isinstance(backup, Qwen3TTSTTS)
    assert backup._base_url == "http://127.0.0.1:8788"
    out = capsys.readouterr().out
    assert "tts fallback armed primary=minimax backup=qwen3_local" in out


def test_build_tts_provider_bok_local_tts_zero_keeps_single_instance(monkeypatch):
    """BOK_LOCAL_TTS=0(bok.py 明确跳过本地 :8788 的语义)→ 裸主档,不装备档。"""
    from livekit.agents import tts as agents_tts
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS

    monkeypatch.setenv("BOK_LOCAL_TTS", "0")
    provider = interpret._build_tts_provider({"provider": "minimax", "api_key": "k"}, "zh")
    assert isinstance(provider, MiniMaxTTS)
    assert not isinstance(provider, agents_tts.FallbackAdapter)


def test_build_tts_provider_local_branch_not_wrapped(monkeypatch):
    """provider=qwen3_tts 本就是本地档 → 不包回退链(零变化)。"""
    from livekit.agents import tts as agents_tts
    from agent_runtime.providers.livekit_plugins import Qwen3TTSTTS

    monkeypatch.delenv("BOK_LOCAL_TTS", raising=False)
    provider = interpret._build_tts_provider({"provider": "qwen3_tts"}, "zh")
    assert isinstance(provider, Qwen3TTSTTS)
    assert not isinstance(provider, agents_tts.FallbackAdapter)


# ==================== TASK 2: MT 超时兜底 ====================


def test_mt_fail_line_by_target_lang():
    """按目标语出中性请示句;未知/空语言键回落 zh。zh/en 为任务硬要求。"""
    assert interpret._mt_fail_line("zh") == "抱歉，这句没听清，请再说一遍。"
    assert interpret._mt_fail_line("en") == "Sorry, I didn't catch that — could you repeat?"
    assert "聽唔清楚" in interpret._mt_fail_line("cantonese")
    assert interpret._mt_fail_line("") == interpret._mt_fail_line("zh")
    assert interpret._mt_fail_line("fr") == interpret._mt_fail_line("zh")
    # 绝不回放源文:兜底句只含目标语提示,唔含任何源句占位。
    assert "{text}" not in interpret._mt_fail_line("zh")


class _HangLLM:
    """聊天流永不产出——复现 wait_for 超时(worker except 分支的触发形态)。"""

    def chat(self, *, chat_ctx, conn_options):  # noqa: ARG002
        async def _gen():
            await asyncio.sleep(10)
            yield None  # pragma: no cover

        return _gen()


def test_mt_once_timeout_raises_timeout_error():
    """_mt_once 超时抛 asyncio.TimeoutError —— 正是 worker 超时分支捕获的类型。"""
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            interpret._mt_once(_HangLLM(), object(), timeout_s=0.05, target_lang="en")
        )


def test_mt_say_worker_timeout_branch_source_pinned():
    """worker 超时分支:打 MT_TIMEOUT_FALLBACK 行 + 走 session.say 出兜底句。

    worker 定义在 entrypoint 闭包内不可直调 → 源码级 pin(镜像仓内源级 pin 惯例):
    超时 except 分支必须(1)打印带 room/round 的观测行、(2)say(_mt_fail_line(...)),
    且必须排在泛 except Exception 之前(否则被吞)。
    """
    src = Path(interpret.__file__).read_text(encoding="utf-8")
    assert "MT_TIMEOUT_FALLBACK room=" in src
    assert "session.say(_mt_fail_line(target_lang))" in src
    # 超时分支(except asyncio.TimeoutError)必须先于泛 except Exception
    # ——限定在 _mt_say_worker 体内(offset 至 worker 定义之后)。
    worker = src[src.index("async def _mt_say_worker") :]
    i_timeout = worker.index("except asyncio.TimeoutError:")
    i_generic = worker.index("except Exception as exc:  # 单句失败不阻后续")
    assert i_timeout < i_generic


def test_mt_fail_line_wired_into_say_path_source_pinned():
    """兜底句经同一 session.say 出口(与译文同链),唔另起第二条输出通道。"""
    src = inspect.getsource(interpret)
    worker = src[src.index("async def _mt_say_worker") :]
    say_block = worker[worker.index("except asyncio.TimeoutError:") :]
    say_block = say_block[: say_block.index("except Exception as exc:  # 单句失败不阻后续")]
    assert "_mt_fail_line(target_lang)" in say_block
    # 不写假译文行:超时分支唔得出现 _add_turn / add_turn。
    assert "_add_turn" not in say_block
    assert "add_turn(" not in say_block


# ==================== TASK 3: 背压弃句观测 ====================


class _FakeItem:
    def __init__(self, text: str):
        self.text_content = text


class _FakeHandle:
    def __init__(self, text: str = "", done: bool = False):
        self._items = [_FakeItem(text)] if text else []
        self._done = done
        self.interrupt_calls: list[bool] = []

    def done(self) -> bool:
        return self._done

    @property
    def chat_items(self):
        return self._items

    def interrupt(self, *, force: bool = False):
        self.interrupt_calls.append(force)
        self._done = True


def test_backlog_drop_prints_observability(monkeypatch, capsys):
    """弃句时打 `BACKLOG_DROP room=… lang=… chars=…`(无 DB 句柄,打印即交付)。"""
    monkeypatch.setenv("BOK_INTERP_MAX_BACKLOG_S", "2.0")
    monkeypatch.delenv("BOK_INTERP_BACKLOG", raising=False)
    bl = interpret._PlaybackBacklog("zh", room="call-abc123")
    h1 = _FakeHandle("一二三四五六七八九十")  # 10 字 → 2.0s
    h2 = _FakeHandle("一二三四五六七八九十")
    h3 = _FakeHandle("一二三四五六七八九十")
    bl.on_speech_created(h1)
    bl.on_speech_created(h2)
    capsys.readouterr()  # 丢弃前两轮(无弃句)输出
    depth, est, dropped = bl.on_speech_created(h3)
    assert dropped == 1
    out = capsys.readouterr().out
    assert "BACKLOG_DROP room=call-abc123 lang=zh chars=10" in out


def test_backlog_no_drop_no_print(monkeypatch, capsys):
    """未弃句零输出(成本零)。"""
    monkeypatch.setenv("BOK_INTERP_MAX_BACKLOG_S", "6.0")
    monkeypatch.delenv("BOK_INTERP_BACKLOG", raising=False)
    bl = interpret._PlaybackBacklog("en", room="call-x")
    bl.on_speech_created(_FakeHandle("hello there"))
    out = capsys.readouterr().out
    assert "BACKLOG_DROP" not in out
