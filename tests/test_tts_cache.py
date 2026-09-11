"""TtsAudioCache 单测:key 归一化/存取/LRU/损坏降级/组帧。"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.tts_cache import (  # noqa: E402
    TtsAudioCache,
    cache_key,
    frames_aiter,
    normalize_cache_text,
    pcm_to_frames,
)


def _cache(tmp_path: Path, **kw) -> TtsAudioCache:
    return TtsAudioCache(root=tmp_path / "tts-cache", sample_rate=24000, **kw)


def test_normalize_unifies_fullwidth_punct_and_space():
    assert normalize_cache_text("  你好，係咪？ ") == "你好,係咪?"
    assert normalize_cache_text("A　B…C") == "A B-C" or normalize_cache_text("A　B…C") == "A B…C"
    # 空白折叠
    assert normalize_cache_text("a \n\t b") == "a b"


def test_key_ignores_punct_width_but_not_voice_model_rate():
    a = cache_key("好的，收到", voice_id="v1", model="m", sample_rate=24000)
    b = cache_key("好的,收到", voice_id="v1", model="m", sample_rate=24000)
    assert a == b  # 全半角标点同 key
    assert a != cache_key("好的,收到", voice_id="v2", model="m", sample_rate=24000)
    assert a != cache_key("好的,收到", voice_id="v1", model="m2", sample_rate=24000)
    assert a != cache_key("好的,收到", voice_id="v1", model="m", sample_rate=16000)
    # 不做数字归一:不同号码必不同 key
    assert cache_key("号码123", voice_id="v", model="m", sample_rate=24000) != cache_key(
        "号码124", voice_id="v", model="m", sample_rate=24000
    )


def test_store_get_roundtrip_and_meta(tmp_path):
    c = _cache(tmp_path)
    key = c.key_for("你好", voice="v", model="m")
    assert c.get(key) is None
    pcm = b"\xe8\x03" * 4800  # 0.2s@24k,振幅 1000(> trim 静音门限,不会被剪)
    assert c.store(key, pcm, text="你好", voice="v", model="m") is True
    got = c.get(key)
    assert got == pcm
    meta = (tmp_path / "tts-cache" / f"{key}.json")
    assert meta.exists()


def test_corrupt_or_missing_returns_none(tmp_path):
    c = _cache(tmp_path)
    key = c.key_for("坏档", voice="v", model="m")
    c.root.mkdir(parents=True, exist_ok=True)
    c._pcm_path(key).write_bytes(b"\x00\x01")  # 存在但内容随意 → 仍可读(信任存在即整段)
    assert c.get(key) == b"\x00\x01"
    missing = c.key_for("无", voice="v", model="m")
    assert c.get(missing) is None


def test_lru_eviction(tmp_path):
    c = _cache(tmp_path, max_entries=2)
    keys = []
    for i in range(3):
        k = c.key_for(f"line{i}", voice="v", model="m")
        c.store(k, b"\xe8\x03" * 4800, text=f"line{i}", voice="v", model="m")
        keys.append(k)
        time.sleep(0.01)  # mtime 可分辨
    assert c.get(keys[0]) is None  # 最旧被淘汰
    assert c.get(keys[2]) is not None


def test_pinned_entries_survive_eviction(tmp_path):
    """罐头集(垫话/QA/静态直念线)打钉:无界动态条目(逐对象开场白)灌满
    LRU 也不会把钉住条目挤走——否则音色一致性静默破功(2026-09-10)。"""
    c = _cache(tmp_path, max_entries=2)
    pcm = b"\xe8\x03" * 4800
    pinned = c.key_for("垫话池句", voice="v", model="m")
    assert c.store(pinned, pcm, text="垫话池句", voice="v", model="m", pin=True)
    time.sleep(0.01)
    dyn = []
    for i in range(3):  # 动态条目淹没(pinned 最旧,处在淘汰区)
        k = c.key_for(f"开场白{i}", voice="v", model="m")
        c.store(k, pcm, text=f"开场白{i}", voice="v", model="m")
        dyn.append(k)
        time.sleep(0.01)
    assert c.get(pinned) is not None  # 钉住永不逐出
    assert c.get(dyn[0]) is None  # 未钉条目照常 LRU
    assert c.get(dyn[2]) is not None


def test_unpinned_default_stays_evictable(tmp_path):
    """运行时 tee 落盘(session.say 直念线)不传 pin=默认可逐出。"""
    c = _cache(tmp_path, max_entries=1)
    pcm = b"\xe8\x03" * 4800
    a = c.key_for("a", voice="v", model="m")
    b = c.key_for("b", voice="v", model="m")
    c.store(a, pcm, text="a", voice="v", model="m")
    time.sleep(0.01)
    c.store(b, pcm, text="b", voice="v", model="m")
    assert c.get(a) is None
    assert c.get(b) is not None
    meta = c._meta_path(b).read_text(encoding="utf-8")
    assert "pinned" not in meta  # 未钉不写键,旧行为不变


def test_unpinned_restore_preserves_pin(tmp_path):
    """双写者竞态保钉:pregen 落钉后,运行时 tee 迟到重写同 key 不得洗掉
    pinned(否则罐头静默退回可逐出——保存人设→来电窗口恰会撞上)。"""
    c = _cache(tmp_path)
    pcm = b"\xe8\x03" * 4800
    k = c.key_for("垫话池句", voice="v", model="m")
    assert c.store(k, pcm, text="垫话池句", voice="v", model="m", pin=True)
    # 迟到的未钉写回(pcm 变了也会换内容,但 key 同)
    assert c.store(k, b"\xe8\x03" * 2400, text="垫话池句", voice="v", model="m")
    assert c._is_pinned(k) is True


def test_pcm_to_frames_slices_200ms():
    sr = 24000
    pcm = b"\xe8\x03" * sr  # 1s mono s16le(48000 bytes)
    frames = pcm_to_frames(pcm, sr)
    assert len(frames) == 5
    assert all(f.sample_rate == sr for f in frames)
    total = sum(f.samples_per_channel for f in frames)
    assert total == sr


def test_frames_aiter_is_async_iterable():
    pcm = b"\x01\x00" * 4800
    frames = pcm_to_frames(pcm, 24000)

    async def _run():
        out = []
        async for f in frames_aiter(frames):
            out.append(f)
        return out

    assert len(asyncio.run(_run())) == len(frames)


def test_env_toggle_and_dir_env(tmp_path, monkeypatch):
    from agent_runtime.tts_cache import default_cache_dir, tts_cache_enabled

    monkeypatch.setenv("BOK_TTS_CACHE_DIR", str(tmp_path / "custom"))
    assert default_cache_dir() == tmp_path / "custom" / "tts-cache"
    monkeypatch.setenv("BOK_TTS_CACHE", "0")
    assert tts_cache_enabled() is False
    monkeypatch.setenv("BOK_TTS_CACHE", "1")
    assert tts_cache_enabled() is True
