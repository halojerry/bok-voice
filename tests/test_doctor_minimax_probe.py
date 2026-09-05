"""doctor MiniMax 音色探针(get_voice 校验)分支回归。

不触网:urlopen 打桩。凭据只喺测试内假造,永不入码。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import bok


def _make_db(tmp: Path, tts: dict) -> Path:
    db = tmp / "bok_voice.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS global_settings (id TEXT PRIMARY KEY, tts_json TEXT)")
    conn.execute(
        "INSERT INTO global_settings VALUES ('global', ?)", (json.dumps(tts),)
    )
    conn.commit()
    conn.close()
    return db


def test_probe_skips_without_db(tmp_path):
    fails: list[str] = []
    bok._doctor_minimax_tts(tmp_path, fails)
    assert fails == []


def test_probe_skips_non_minimax_provider(tmp_path):
    _make_db(tmp_path, {"provider": "qwen3_tts"})
    fails: list[str] = []
    bok._doctor_minimax_tts(tmp_path, fails)
    assert fails == []


def test_probe_skips_when_key_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    _make_db(tmp_path, {"provider": "minimax", "speaker_cantonese": "Cantonese_X"})
    fails: list[str] = []
    bok._doctor_minimax_tts(tmp_path, fails)
    assert fails == []


def test_probe_fails_when_no_voice_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    _make_db(tmp_path, {"provider": "minimax", "api_key": "k"})
    fails: list[str] = []
    bok._doctor_minimax_tts(tmp_path, fails)
    assert len(fails) == 1 and "speaker_zh" in fails[0]


def test_probe_resolves_configured_voice(tmp_path, monkeypatch):
    """get_voice 列表里有已配音色 → ok 唔 fail。"""

    def fake_urlopen(req, timeout=8):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {
                        "base_resp": {"status_code": 0},
                        "data": {
                            "system_voice": [
                                {"voice_id": "Cantonese_Male_news_anchor_vv2"},
                                {"voice_id": "male-qn-qingse"},
                            ]
                        },
                    }
                ).encode()

        return _Resp()

    import bok as bok_mod

    monkeypatch.setattr(bok_mod.urllib.request, "urlopen", fake_urlopen)
    _make_db(
        tmp_path,
        {"provider": "minimax", "api_key": "k", "speaker_cantonese": "Cantonese_Male_news_anchor_vv2"},
    )
    fails: list[str] = []
    bok._doctor_minimax_tts(tmp_path, fails)
    assert fails == []


def test_probe_fails_when_voice_not_in_account_list(tmp_path, monkeypatch):
    """账号列表可达但已配音色全部唔喺列表 → 确定性错配,硬 fail。"""

    def fake_urlopen(req, timeout=8):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {
                        "base_resp": {"status_code": 0},
                        "data": {"system_voice": [{"voice_id": "male-qn-qingse"}]},
                    }
                ).encode()

        return _Resp()

    import bok as bok_mod

    monkeypatch.setattr(bok_mod.urllib.request, "urlopen", fake_urlopen)
    _make_db(
        tmp_path,
        {"provider": "minimax", "api_key": "k", "speaker_zh": "not_a_real_voice"},
    )
    fails: list[str] = []
    bok._doctor_minimax_tts(tmp_path, fails)
    assert len(fails) == 1 and "not_a_real_voice" in fails[0]
