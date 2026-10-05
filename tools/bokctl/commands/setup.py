"""bokctl.commands.setup —— `bok setup`（G2 W③ 自 bokctl.models 搬入）。

模型域共享件（_all_models_present/setup_models）仍住 bokctl.models，穿
``models.X`` call-time 取；download 子动作经 ``commands.download.cmd_download``
（owner 随 W③ 搬入 commands.download）。"""
from __future__ import annotations

import json

from bokctl import commands, models


def cmd_setup(action: str = "status") -> int:
    if action == "download":
        commands.download.cmd_download()
        print(json.dumps({"ready": models._all_models_present()}, ensure_ascii=False))
        return 0
    data = {"ready": models._all_models_present(), "models": models.setup_models()}
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def run(args) -> int:
    return cmd_setup(args.action)
