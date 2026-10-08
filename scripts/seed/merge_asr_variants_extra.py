"""curated 变体幂等并进 ASR 吸附资产(2026-10-08 B 线 P0)。

背景:``build_asr_variants.py`` 的音近生成器依赖 rime-cantonese/ToJyutping 等
离线数据与一次性 venv(本机已不在盘)——curated 实测变体走本件直并,不重跑
生成器。**builder 重建会覆盖资产,重建后必须重跑本件回填**(幂等:变体已在
表内=跳过)。

卫生规则(镜像 builder 尾段):
- 变体与同车道正确词撞形=丢(防运行时反查歧义);
- 同词变体去重、保序(curated 在后追加);
- meta.extra_merged 记录回填来源与计数,重建覆盖后可对账。

用法:
    python scripts/seed/merge_asr_variants_extra.py [extra.json …]
    缺省=scripts/seed/asr_variants_extra_bline.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# --- scripts import bootstrap (G1) ---
import pathlib as _pathlib

_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

ASSET = Path(__file__).resolve().parents[2] / "packages" / "core" / "bok_voice_core" / "assets" / "asr_variants.json"
DEFAULT_EXTRA = Path(__file__).resolve().parent / "asr_variants_extra_bline.json"


def merge(extra_path: Path) -> dict:
    data = json.loads(ASSET.read_text(encoding="utf-8"))
    raw = json.loads(extra_path.read_text(encoding="utf-8"))
    variants = data["variants"]
    report = {}
    for lang in ("zh", "cantonese", "en"):
        added = 0
        lane_words = set(variants.get(lang) or {})
        for word, vs in (raw.get(lang) or {}).items():
            if not word or not isinstance(vs, list):
                continue
            lane_words.add(word)  # 后到的词也参与「变体撞词形」判定
        for word, vs in (raw.get(lang) or {}).items():
            bucket = variants.setdefault(lang, {}).setdefault(word, [])
            for v in vs:
                v = str(v).strip()
                if not v or v == word or v in bucket or v in lane_words:
                    continue
                bucket.append(v)
                added += 1
        if added:
            report[lang] = added
    meta = data.setdefault("meta", {})
    merged = meta.setdefault("extra_merged", {})
    merged[extra_path.name] = report
    if report:
        ASSET.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return report


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv[1:]] or [DEFAULT_EXTRA]
    for p in paths:
        report = merge(p)
        print(f"[merge-asr-variants] {p.name}: {report or 'no-op(已并)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
