"""Linux systemd 常驻单元生成（2026-09-24 appliance 全栈形态）。

平台对应物：mac = launchd plist（RunAtLoad + KeepAlive）；Windows = Task
Scheduler 任务（BootTrigger + RestartOnFailure，tools/schtasks_units.py）；
Linux = systemd system unit：

  WantedBy=multi-user.target     ←→ RunAtLoad / BootTrigger（开机自起）
  Restart=always + RestartSec=3  ←→ KeepAlive / RestartOnFailure（崩溃自动拉回）
  EnvironmentFile=/etc/bok/bok.env ←→ launchd EnvironmentVariables dict /
      schtasks cmd set 前缀链（运行时 env 注入面；systemd 原生支持，无需引链）

**本模块纯函数本位**：只返回单元文本字符串，零落盘零 subprocess——写暂存目录
由 tools/bok.py prod install 做（唯一副作用出口，与 schtasks_units「副作用只在
bok.py prod」同族且更严）。**/etc/systemd/system 与 /etc/bok/bok.env 永不由本仓
代写**：装载与 env 文件都归操作员以 root 执行（安装输出打印逐字命令）。

语义边界（动 Restart 前先读）：`Restart=always` 按 2026-09-24 appliance 规格
钉死——**唯独 bok-node-agent 例外用 `on-failure`**：tools/node_agent.py 的吊销
熔断正是 exit(0)（更新/重启走 75），`always` 会把已吊销节点每 3s 拉回=授权
吊销失效（2026-09-20 契约「勿改 always」）；on-failure 令熔断后单元保持 dead，
真崩溃照样拉回。其余单元恒 always（clean stop 由 systemctl stop 天然不重启，
always 只兜崩溃/掉电）。若未来再动这两档，同步点：本模块 RESTART_MODE /
render_node_agent_unit 的 restart= 与 tests/test_systemd_units.py 的形状断言。

单元清单（appliance 全栈，2026-09-24 规格）：
  bok-cp.service            控制面 API :8000（uvicorn，--port 进 ExecStart）
  bok-agent-worker.service  A 线 agent worker（健康口 :8081 是 BOK_WORKER_PORT
                            代码缺省、不在 argv——端口记号写进 Description）
  bok-interp-fwd.service    B 线同传 fwd（:8082；INTERP_DIRECTION=fwd 单元内联
                            ——两个 interp 单元共用同一份 EnvironmentFile，只有
                            方向键相异，故逐单元 Environment= 承载）
  bok-interp-rev.service    B 线同传 rev（:8083；INTERP_DIRECTION=rev 同上）
  bok-node-agent.service    薄节点守护（UI :3000 + 心跳 + 全栈拉起）
  bok-sidecar@.service      模型 sidecar 模板（asr :8787 / tts :8788 / embed
                            :8789）——ExecStart 是占位符，逐实例真命令以注释
                            给出，装载前必须替换（systemd 模板无法按 %i 分派
                            不同 --app-dir/端口；占位 = 忘了替换就显式失败，
                            不静默空转）

LiveKit 信令/媒体(:7880) 与 LLM 服务(:1235/1236/1237) 不进 systemd 清单：
前者随发行包走独立守护面；后者由 node_agent cmd_up / bok.py up 按「模型在盘才
起」拉起——模型缺失即跳过的可选线不适合 Restart=always 常驻。
"""
from __future__ import annotations

import re

# 引用面（永不创建/写）：systemd 读它注入运行时 env；文件由操作员 root 维护。
ENV_FILE = "/etc/bok/bok.env"

RESTART_MODE = "always"  # 见模块 docstring「语义边界」——与测试形状断言成对
RESTART_SEC = "3"

# 模板单元定名（spec 2026-09-24；实例化即 bok-sidecar@asr.service 等）。
SIDECAR_UNIT = "bok-sidecar@.service"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# sidecar 三实例（实例名, 端口, --app-dir）——与 bok.py cmd_up 的拉起命令同源
# （sidecar venv python + uvicorn app:app --app-dir … --host 127.0.0.1 --port …）。
_SIDECAR_INSTANCES: tuple[tuple[str, str, str], ...] = (
    ("asr", "8787", "services/qwen3-asr-sidecar"),
    ("tts", "8788", "services/qwen3-tts-sidecar"),
    ("embed", "8789", "services/bge-embed-sidecar"),
)


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
    """ExecStart/Environment 参数引用：含空格/`"`/`{}`/`;` 等一律双引号包裹并
    转义内部引号（systemd 词法：双引号内保留空格、支持 `\\"` 与 `\\\\` 转义；
    单引号是保留字面量但不可转义，本仓 JSON 片段含双引号统一走双引号路径）。"""
    text = str(arg)
    if text and all(ch.isalnum() or ch in "-_./:=,+@%" for ch in text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_unit(
    name: str,
    exec_start: list[str],
    working_dir: str,
    description: str,
    extra_env: dict[str, str] | None = None,
    restart: str = "",
) -> str:
    """渲染一个具体 systemd system unit（纯函数，返回文本不落盘）。

    形状（2026-09-24 appliance 规格）：[Unit] Description + After/Wants=
    network-online.target；[Service] Type=simple + WorkingDirectory +
    EnvironmentFile（只引用，缺文件=单元拒绝启动，逼操作员先建）+ ExecStart +
    Restart=always + RestartSec=3；[Install] WantedBy=multi-user.target。
    extra_env 只放**逐单元相异**的键（如 INTERP_DIRECTION）——共用运行时配置
    一律走 EnvironmentFile，不烘进单元。日志走 journald 缺省（journalctl -u
    <unit>），不写 StandardOutput= 路径。"""
    env_lines = "".join(
        f"Environment={quote_arg(f'{k}={v}')}\n"
        for k, v in sorted((extra_env or {}).items())
    )
    exec_line = " ".join(quote_arg(a) for a in exec_start)
    # restart 覆盖口：仅 node-agent 用 on-failure（2026-09-24 修正）。node_agent
    # 的吊销熔断是 exit(0)——Restart=always 会把它每 3s 复活，授权吊销失效；
    # 其余单元恒 always（clean stop 由 systemctl stop 天然不重启，always 只兜
    # 崩溃/掉电）。见模块 docstring「语义边界」。
    restart = restart or RESTART_MODE
    return (
        f"# Bok 常驻单元（由 tools/bok.py prod install 生成）— {description}\n"
        + "[Unit]\n"
        + f"Description={description}\n"
        + "After=network-online.target\n"
        + "Wants=network-online.target\n"
        + "\n[Service]\n"
        + "Type=simple\n"
        + f"WorkingDirectory={quote_arg(working_dir)}\n"
        + f"EnvironmentFile={ENV_FILE}\n"
        + env_lines
        + f"ExecStart={exec_line}\n"
        + f"Restart={restart}\n"
        + f"RestartSec={RESTART_SEC}\n"
        + "\n[Install]\n"
        + "WantedBy=multi-user.target\n"
    )


def build_sidecar_template(working_dir: str) -> str:
    """渲染 bok-sidecar@.service 模板（纯函数）。

    ExecStart 是**占位符**（/bin/false：忘了替换就 enable=显式失败+journal
    可见，绝不静默空转）；逐实例真命令以注释给出——二进制路径已按 working_dir
    烘焙为绝对路径（systemd 要求 ExecStart 二进制绝对路径），操作员照抄一行到
    ExecStart 即可装载对应实例。模板与具体单元同形状（形状断言共用）。"""
    docs: list[str] = []
    for inst, port, app_dir in _SIDECAR_INSTANCES:
        py = f"{working_dir}/{app_dir}/.venv/bin/python"
        docs.append(
            f"#   {inst:<5} (:{port}): "
            f"ExecStart={quote_arg(py)} -m uvicorn app:app"
            f" --app-dir {app_dir} --host 127.0.0.1 --port {port}\n"
            f"#     启用：systemctl enable --now bok-sidecar@{inst}.service\n"
        )
    return (
        f"# Bok 常驻单元（由 tools/bok.py prod install 生成）— 模型 sidecar 模板"
        f"（asr :8787 / tts :8788 / embed :8789）\n"
        + "# !! 占位 ExecStart：装载每个实例前，把下方对应实例的 ExecStart 行替换掉\n"
        + "# !! 本占位行（/bin/false + Restart=always = 忘了替换时 journal 里可见的\n"
        + "# !! 显式失败循环，绝不静默）。\n"
        + "[Unit]\n"
        + "Description=Bok model sidecar %i (asr :8787 / tts :8788 / embed :8789)\n"
        + "After=network-online.target\n"
        + "Wants=network-online.target\n"
        + "\n[Service]\n"
        + "Type=simple\n"
        + f"WorkingDirectory={quote_arg(working_dir)}\n"
        + f"EnvironmentFile={ENV_FILE}\n"
        + "ExecStart=/bin/false\n"
        + f"Restart={RESTART_MODE}\n"
        + f"RestartSec={RESTART_SEC}\n"
        + "\n# 逐实例 ExecStart（路径已按仓库根烘焙为绝对路径）：\n"
        + "".join(docs)
        + "\n[Install]\n"
        + "WantedBy=multi-user.target\n"
    )


def render_node_agent_unit(
    repo_root: str,
    python_bin: str,
    node_args: list[str],
) -> tuple[str, str]:
    """渲染单个 bok-node-agent 单元（节点包拓扑：node_agent 经 cmd_up 自拉全栈，
    不与其余单元并装）。node_args 原样透传（--cp-url 必填的校验归调用方 bok.py）。"""
    args = [str(python_bin), f"{repo_root}/tools/node_agent.py", *node_args]
    return (
        unit_name("node-agent"),
        build_unit(
            "node-agent",
            args,
            repo_root,
            "Bok node-agent — 薄节点守护（UI :3000 + 心跳 + 全栈拉起）",
            extra_env={"PYTHONUNBUFFERED": "1"},
            # 吊销熔断=exit(0)（license-revocation fuse）——on-failure 令吊销后
            # 单元保持 dead（2026-09-20 契约「勿改 always」在 2026-09-24 重写时
            # 曾一度丢失，本行是它的复归，语义见模块 docstring）。
            restart="on-failure",
        ),
    )


def render_all_units(
    repo_root: str,
    python_bin: str,
    cp_bind_host: str = "127.0.0.1",
    node_agent_args: list[str] | None = None,
) -> list[tuple[str, str]]:
    """渲染 appliance 全栈单元清单（纯函数）：返回 [(文件名, 文本), ...]。

    名单见模块 docstring；ExecStart 的二进制一律 python_bin（装机 venv 的绝对
    路径），WorkingDirectory=repo_root；CP 监听地址由 cp_bind_host 下发（与
    bok.py _cp_bind_host 同源语义：缺省 127.0.0.1，BOK_BIND_HOST 显式才对外）。"""
    root = str(repo_root)
    py = str(python_bin)
    node_args = list(node_agent_args or ["--cp-url", "http://127.0.0.1:8000"])
    return [
        (
            unit_name("cp"),
            build_unit(
                "cp",
                [py, "-m", "uvicorn", "control_plane.main:app",
                 "--host", cp_bind_host, "--port", "8000"],
                root,
                "Bok CP — control plane API (:8000)",
            ),
        ),
        (
            unit_name("agent-worker"),
            build_unit(
                "agent-worker",
                [py, "-m", "agent_runtime.main"],
                root,
                "Bok agent-worker — A 线客服 worker (:8081)",
            ),
        ),
        (
            unit_name("interp-fwd"),
            build_unit(
                "interp-fwd",
                [py, "-m", "agent_runtime.interpret"],
                root,
                "Bok interp-fwd — B 线同传 fwd (:8082)",
                extra_env={"BOK_SERVICE": "interp-fwd", "INTERP_DIRECTION": "fwd"},
            ),
        ),
        (
            unit_name("interp-rev"),
            build_unit(
                "interp-rev",
                [py, "-m", "agent_runtime.interpret"],
                root,
                "Bok interp-rev — B 线同传 rev (:8083)",
                extra_env={"BOK_SERVICE": "interp-rev", "INTERP_DIRECTION": "rev"},
            ),
        ),
        render_node_agent_unit(root, py, node_args),
        (SIDECAR_UNIT, build_sidecar_template(root)),
    ]


def env_file_sample(repo_root: str) -> str:
    """/etc/bok/bok.env 的**示例文本**（打印用——本仓只引用该文件，永不创建/写）。

    至少需要 PYTHONPATH（WorkingDirectory 只有仓库根，可 import 的包全在
    apps/packages 子目录）与 PYTHONUNBUFFERED；凭据/拓扑键按需追加。装载顺序
    提醒写在文本里：先建 env 文件再 enable，缺文件=单元拒绝启动。"""
    root = str(repo_root).rstrip("/")
    parts = [
        f"{root}/packages/core",
        f"{root}/packages/business-db",
        f"{root}/packages/knowledge",
        f"{root}/packages/observability",
        f"{root}/apps/control-plane",
        f"{root}/apps/agent",
    ]
    return (
        f"# ---- {ENV_FILE} 示例（由 bok.py prod install 打印；文件本身由操作员创建：\n"
        "# ---- install -d -m 700 /etc/bok 然后 $EDITOR 放入以下内容并 chmod 600）\n"
        f"PYTHONPATH={':'.join(parts)}\n"
        "PYTHONUNBUFFERED=1\n"
        "# ---- 按需追加（systemd env-file 语法：一行 KEY=value，# 注释；值含空白用引号）----\n"
        "# BOK_BIND_HOST=0.0.0.0              # CP 对外监听（缺省 127.0.0.1）\n"
        "# LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET   # 缺省 ws://127.0.0.1:7880 + devkey\n"
        "# DATABASE_URL / VAULT_ROOT          # 不设走 CP 内建缺省；appliance 建议显式钉库/知识库位置\n"
        "# BOK_CP_TOKEN / BOK_AUTH_REQUIRED / BOK_JWT_SECRET   # auth-on 三件（agent 机器通道同 BOK_CP_TOKEN）\n"
        "# MLX_LLM_BASE_URL / BOK_WORKER_PORT / BOK_LIVEKIT_BIND / BOK_LLM_TIER / MINIMAX_* 等\n"
    )
