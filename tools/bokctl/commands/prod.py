"""bokctl.commands.prod —— `bok prod` 适配器（G2 W③）。

实现住 bokctl.prod 域（与命令适配器同名共存）；本模块内一律以 ``_prod``
别名引用域模块，参数塑形逐字节镜像原 core.main 的 prod 分派支。"""
from __future__ import annotations

from bokctl import prod as _prod


def run(args) -> int:
    return _prod.cmd_prod(args.action,
                      node_agent=getattr(args, "node_agent", False),
                      node_args=getattr(args, "extra", None),
                      open_firewall=getattr(args, "open_firewall", False),
                      staging_dir=getattr(args, "staging_dir", ""),
                      with_model_plane=getattr(args, "with_model_plane", False))
