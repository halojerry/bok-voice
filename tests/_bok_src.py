"""bok 源码扫描面单点(G2 W①):源级 pin 测试统一从这里拿码,不再直读 bok.py。

W①=core.py 全文;W② 起域模块搬出后自动跟上(bokctl/ 全目录拼接),断言字符串
跨搬运不失效。W③(2026-10-05)起命令实现住 bokctl/commands/ 子包——扫描面
改 **rglob 递归**(原 glob 只扫顶层,命令体搬入子包会静默离开拼接面=源级
pin 假红),子包内容自动入面。
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BOKCTL = _ROOT / "tools" / "bokctl"


def bok_source() -> str:
    """tools/bokctl/ 全量拼接(rglob 递归,路径名排序,带文件头分隔注释)。"""
    parts = []
    for p in sorted(_BOKCTL.rglob("*.py")):
        parts.append(f"# ==== {p.relative_to(_BOKCTL).as_posix()} ====")
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)
