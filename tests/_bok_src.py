"""bok 源码扫描面单点(G2 W①):源级 pin 测试统一从这里拿码,不再直读 bok.py。

W①=core.py 全文;W② 起域模块搬出后自动跟上(bokctl/*.py 全目录拼接,
稳定序=文件名排序),断言字符串跨搬运不失效。
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BOKCTL = _ROOT / "tools" / "bokctl"


def bok_source() -> str:
    """tools/bokctl/*.py 全量拼接(文件名排序,带文件头分隔注释)。"""
    parts = []
    for p in sorted(_BOKCTL.glob("*.py")):
        parts.append(f"# ==== {p.name} ====")
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)
