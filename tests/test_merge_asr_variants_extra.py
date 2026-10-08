"""scripts/seed/merge_asr_variants_extra.py 契约测试(2026-10-08 B 线 P0):

- 幂等:同 extra 并两次=第二次 no-op;
- 卫生:变体撞同车道正确词形=丢(防反查歧义);同词变体去重;
- meta.extra_merged 记账(可对账 builder 重建覆盖);
- 产物形态:variants 键保留、curated 变体可被 load_variant_table 读回。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "seed" / "merge_asr_variants_extra.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("merge_asr_variants_extra", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["merge_asr_variants_extra"] = mod
    spec.loader.exec_module(mod)
    return mod


def _mk_asset(tmp_path: Path) -> Path:
    asset = tmp_path / "asr_variants.json"
    asset.write_text(
        json.dumps(
            {
                "meta": {"version": 1},
                "variants": {"zh": {"顺丰": ["顺风"]}, "cantonese": {"順豐": ["順風"]}, "en": {}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return asset


def _mk_extra(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "extra.json"
    p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return p


def test_merge_idempotent_and_hygiene(tmp_path, monkeypatch):
    mod = _load_module()
    asset = _mk_asset(tmp_path)
    monkeypatch.setattr(mod, "ASSET", asset)
    extra = _mk_extra(
        tmp_path,
        {
            "zh": {
                "了解": ["成鸟解", "成鳥解"],
                "撞词": ["顺丰"],  # 变体撞同车道正确词=必须丢
            }
        },
    )
    r1 = mod.merge(extra)
    assert r1 == {"zh": 2}  # 成鸟解+成鳥解 进表;撞词条目被卫生掉
    r2 = mod.merge(extra)
    assert r2 == {}  # 幂等 no-op
    data = json.loads(asset.read_text(encoding="utf-8"))
    assert data["variants"]["zh"]["了解"] == ["成鸟解", "成鳥解"]
    assert "撞词" not in data["variants"]["zh"] or "顺丰" not in data["variants"]["zh"].get("撞词", [])
    assert data["meta"]["extra_merged"]["extra.json"] == {"zh": 2}


def test_merge_output_loadable_by_runtime(tmp_path, monkeypatch):
    """合并产物可被 bok_voice_core.load_variant_table 读回且吸附生效(端到端半步)。"""
    sys.path.insert(0, str(ROOT / "packages" / "core"))
    from bok_voice_core.asr_polish import load_variant_table, polish_transcript

    mod = _load_module()
    asset = _mk_asset(tmp_path)
    monkeypatch.setattr(mod, "ASSET", asset)
    mod.merge(_mk_extra(tmp_path, {"zh": {"了解": ["成鸟解"]}}))

    table = load_variant_table(asset)
    pr = polish_transcript("你好，我成鸟解下你们的产品", "zh", table)
    assert pr.text == "你好，我了解下你们的产品"
    assert pr.edits and pr.edits[0][2] == "成鸟解"
