"""Linux systemd 常驻单元生成（2026-09-20 Ubuntu 节点形态）。

平台对应物：mac = launchd plist（RunAtLoad + KeepAlive）；Windows = Task
Scheduler（BootTrigger + RestartOnFailure）；Linux = systemd system unit：

  WantedBy=multi-user.target     ←→ RunAtLoad / BootTrigger（开机自起）
  Restart=on-failure             ←→ KeepAlive / RestartOnFailure，**且精确复刻
      kill-switch 语义**——本仓退出码有含义（tools/node_agent.py
      `_RESTART_EXIT_CODE=75` = 更新/重启，交守护拉回上新版；`exit(0)` = root
      吊销熔断，必须保持死亡）。Restart=on-failure 只拉非零退出：更新 75 拉回、
      熔断 0 不拉回；`Restart=always` 会把熔断节点拉活，禁止使用。

Env 注入 = Environment= 逐条（systemd 原生支持，无需 Windows 的 cmd 前缀链）。
ExecStart 按空格分词 → 含空格/花括号/引号的参数必须引号包裹（本仓
`--chat-template-kwargs '{"enable_thinking":false}'` 正落此例），规则见
quote_arg；落盘后可在真机 `systemd-analyze verify` 复核。

**本模块零 subprocess**：只产出 unit 文本与落盘（app-data/units），不做任何
systemctl 调用——装载/停用由安装脚本或操作者显式以 root 执行（职责边界与
schtasks_units.py「唯一副作用出口在 bok.py prod」同族；systemd 档更进一步，
连 bok.py 也不代跑特权命令）。单元名只经白名单校验的 slug（_safe_unit_name）。
"""
from __future__ import annotations

import re
from pathlib import Path

SYSTEMD_DIR = Path("/etc/systemd/system")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _safe_unit_name(name: str) -> str:
    """单元名白名单：仅 [a-z0-9-]（源是代码内常量清单，校验是纵深防御——
    名字永不来自网络/用户输入；不合规直接拒绝，不进路径拼接）。"""
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"unsafe systemd unit name: {name!r}")
    return f"bok-{name}.service"


def unit_name(name: str) -> str:
    """单元文件名（与 Windows `bok-<unit>` 任务名、mac `com.bokvoice.<unit>` 同槽位）。"""
    return _safe_unit_name(name)


def quote_arg(arg: str) -> str:
    """ExecStart 参数引用：含空格/`"`/`{}`/`;` 等一律双引号包裹并转义内部引号。

    systemd 词法：双引号内保留空格、支持 `\\"` 与 `\\\\` 转义；单引号是保留字面
    量但不可转义（本仓 JSON 片段含双引号，统一走双引号路径）。
    """
    text = str(arg)
    if text and all(ch.isalnum() or ch in "-_./:=,+@%" for ch in text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_unit(
    name: str,
    args: list[str],
    env: dict[str, str],
    root: str,
    log_dir: str,
    comment: str = "",
) -> str:
    """渲染一个 systemd system unit（Restart=on-failure 语义见模块 docstring）。"""
    slug = _safe_unit_name(name)
    env_lines = "\n".join(
        f"Environment={quote_arg(f'{k}={v}')}" for k, v in sorted(env.items())
    )
    exec_line = " ".join(quote_arg(a) for a in args)
    header = f"# Bok 常驻单元（由 tools/bok.py prod install 生成）— {comment}\n"
    return (
        header
        + "[Unit]\n"
        + f"Description=Bok {name} — {comment or name}\n"
        + "After=network-online.target\n"
        + "Wants=network-online.target\n"
        + "\n[Service]\n"
        + "Type=simple\n"
        + f"WorkingDirectory={quote_arg(root)}\n"
        + (env_lines + "\n" if env_lines else "")
        + f"ExecStart={exec_line}\n"
        # 更新(75)拉回、熔断(0)保持死亡——见模块 docstring；勿改 always。
        + "Restart=on-failure\n"
        + "RestartSec=10\n"
        + f"StandardOutput=append:{log_dir}/{name}.log\n"
        + f"StandardError=append:{log_dir}/{name}.err.log\n"
        + "\n[Install]\n"
        + "WantedBy=multi-user.target\n"
    )


def write_units(
    units: list[tuple[str, list[str], dict[str, str], str]],
    unit_dir: Path,
    root: str,
    log_dir: str,
) -> list[str]:
    """把单元清单落盘到 unit_dir，返回文件名列表（纯写盘，零特权动作）。"""
    unit_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, args, env, comment in units:
        slug = _safe_unit_name(name)
        text = build_unit(name, args, env, root, log_dir, comment)
        path = unit_dir / slug
        path.write_text(text, encoding="utf-8")
        written.append(slug)
        print(f"generated {path}  ({comment})")
    return written


def remove_units(names: list[str], unit_dir: Path) -> list[str]:
    """删除 unit_dir 下的单元副本，返回已删文件名（幂等；系统目录由操作者清理）。"""
    removed: list[str] = []
    for name in names:
        slug = _safe_unit_name(name)
        path = unit_dir / slug
        if path.exists():
            path.unlink()
            removed.append(slug)
            print(f"[uninstall] removed {path}")
    return removed
