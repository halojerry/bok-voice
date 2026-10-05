"""bokctl.commands.manifest —— `bok manifest` 适配器（G2 W③）。

实现住 bokctl.models（模型域未随命令分家）；本模块只做分派薄适配。"""
from __future__ import annotations

from bokctl import models


def run(args) -> int:
    return models.cmd_manifest()
