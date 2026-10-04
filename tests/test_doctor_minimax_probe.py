"""doctor MiniMax 音色探针(get_voice 校验)分支回归。

不触网:urlopen 打桩。凭据只喺测试内假造,永不入码。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402
from _bokpatch import patch_bok  # noqa: E402


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
    bok.doctor._doctor_minimax_tts(tmp_path, fails)
    assert fails == []


def test_probe_skips_non_minimax_provider(tmp_path):
    _make_db(tmp_path, {"provider": "qwen3_tts"})
    fails: list[str] = []
    bok.doctor._doctor_minimax_tts(tmp_path, fails)
    assert fails == []


def test_probe_skips_when_key_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    _make_db(tmp_path, {"provider": "minimax", "speaker_cantonese": "Cantonese_X"})
    fails: list[str] = []
    bok.doctor._doctor_minimax_tts(tmp_path, fails)
    assert fails == []


def test_probe_warns_when_no_voice_configured(tmp_path, monkeypatch, capsys):
    """漏配=warning 唔阻断发布:运行时有默认音色兜底(空音色防 beep),
    「全空=静音」係 e20ed7a 时代的过期前提。确定性错配仍係硬 fail。"""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    _make_db(tmp_path, {"provider": "minimax", "api_key": "k"})
    fails: list[str] = []
    bok.doctor._doctor_minimax_tts(tmp_path, fails)
    assert fails == []
    assert "warning" in capsys.readouterr().out


def test_probe_resolves_configured_voice(tmp_path, monkeypatch):
    """get_voice 列表里有已配音色 → ok 唔 fail。"""

    def fake_http_call(url, method="GET", *, body=None, headers=None, timeout_s=10.0):
        return 200, json.dumps(
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

    import bok as bok_mod  # noqa: F401  (门面在读面仍可用;补丁走权威模块)

    patch_bok(monkeypatch, "_http_call", fake_http_call)
    _make_db(
        tmp_path,
        {"provider": "minimax", "api_key": "k", "speaker_cantonese": "Cantonese_Male_news_anchor_vv2"},
    )
    fails: list[str] = []
    bok.doctor._doctor_minimax_tts(tmp_path, fails)
    assert fails == []


def test_probe_fails_when_voice_not_in_account_list(tmp_path, monkeypatch):
    """账号列表可达但已配音色全部唔喺列表 → 确定性错配,硬 fail。"""

    def fake_http_call(url, method="GET", *, body=None, headers=None, timeout_s=10.0):
        return 200, json.dumps(
            {
                "base_resp": {"status_code": 0},
                "data": {"system_voice": [{"voice_id": "male-qn-qingse"}]},
            }
        ).encode()

    import bok as bok_mod  # noqa: F401  (门面在读面仍可用;补丁走权威模块)

    patch_bok(monkeypatch, "_http_call", fake_http_call)
    _make_db(
        tmp_path,
        {"provider": "minimax", "api_key": "k", "speaker_zh": "not_a_real_voice"},
    )
    fails: list[str] = []
    bok.doctor._doctor_minimax_tts(tmp_path, fails)
    assert len(fails) == 1 and "not_a_real_voice" in fails[0]
