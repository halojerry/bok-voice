"""bokctl.commands.doctor —— `bok doctor` 适配器（G2 W③）。

实现住 bokctl.doctor 域（与命令适配器同名共存）；本模块内一律以
``_doctor`` 别名引用域模块，只做分派薄适配。"""
from __future__ import annotations

from bokctl import doctor as _doctor


def run(args) -> int:
    return _doctor.cmd_doctor()
