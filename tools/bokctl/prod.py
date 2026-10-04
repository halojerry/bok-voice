#!/usr/bin/env python
"""prod 域(launchd/systemd 常驻安装、prod install/uninstall/status;G2 W② 从 core 搬出,
搬运纪律=穿模块对象调用)。

- 本模块只 `from bokctl import core`(及 servers 域)拿模块对象:凡仍住在 core 的
  名字(PROD_HTTP_CHECKS/_agent_prod_env/healthy/_cp_bind_host/
  _control_plane_env 等)一律 `core.X` 调用时取——patch 与后续域搬运在 core 侧
  保持可见(patch 缝=模块属性)。`_realtime_demo_enabled` 属 serve 装配门,
  W②-servers 波搬入 bokctl.servers,本域穿 `servers.X` 取(servers 波新例:
  域间消费=改穿所属域,core 不做值转发);路径/平台锚(ROOT/app_data_dir/
  repo_python/is_mac/is_linux/_embedded_livekit/_livekit_config_path)
  paths 波(2026-10-04)后穿 `paths.X` 取。
- 本域自有函数(_prod_units/_systemd_staging_dir/cmd_prod_install/uninstall/status/
  cmd_prod)域内裸名互调(同模块全局=call-time 可 patch)。
- 测试面:patch 一律走 tests/_bokpatch.py(patch_bok;PATCH_TARGETS 已把 "cmd_prod"
  改道 bokctl.prod);facade 读用 bok.prod.X。
"""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from bokctl import core, paths, servers


def _prod_units(with_model_plane: bool = False) -> list[tuple[str, list[str], dict[str, str], str]]:
    """生产常驻单元清单（mac plists 与 Windows Task Scheduler 任务共用同一份定义，
    含完整 env（SSL_CERT_FILE 烘焙后），防两平台定义漂移）。

    ``with_model_plane=True``（opt-in，`prod install --with-model-plane`，2026-10-02
    审计）：追加 `bok-model-plane` 单元跑 `bok up --models-only`（RunAtLoad 开机
    补拉模型面 + KeepAlive 幂等重扫；重启后 :8787/:1235/:1236/:1237/:1239 不再
    等人工）。默认 False——既有装机渲染逐字节零变化。"""
    agent_env = core._agent_prod_env()
    livekit_bin = str(paths._embedded_livekit() or "livekit-server")
    py = paths.repo_python()
    # unit 定义:name → (args, 附加 env)。agent/interp 共用 agent_env。
    units = [
        (
            "bok-control-plane",
            [str(py), "-m", "uvicorn", "control_plane.main:app", "--host", core._cp_bind_host(), "--port", "8000"],
            core._control_plane_env((paths.app_data_dir() / "bok_voice.db").as_posix()),
            "Bok 控制面 API",
        ),
        ("bok-livekit", [livekit_bin, "--config", str(paths._livekit_config_path())], {}, "实时语音信令/媒体"),
        ("bok-agent", [str(py), "-m", "agent_runtime.main"], agent_env, "A 线客服 agent worker"),
        (
            "bok-interp-fwd",
            [str(py), "-m", "agent_runtime.interpret"],
            {**core._interp_env(agent_env), "BOK_SERVICE": "interp-fwd", "INTERP_DIRECTION": "fwd"},
            "B 线同传 fwd",
        ),
        (
            "bok-interp-rev",
            [str(py), "-m", "agent_runtime.interpret"],
            {**core._interp_env(agent_env), "BOK_SERVICE": "interp-rev", "INTERP_DIRECTION": "rev"},
            "B 线同传 rev",
        ),
    ]
    if with_model_plane:
        # 模型面常驻（opt-in）：`bok up --models-only` 幂等（health 门防叠进程）
        # ——RunAtLoad 开机补拉；KeepAlive 让 launchd 周期重扫（进程退出即被
        # 10s 节流重启），模型面掉线不等人。env 用 agent 面（BOK_CP_TOKEN 等
        # passthrough 在内，download 与 CP 探测同源）。
        units.append(
            (
                "bok-model-plane",
                [str(py), str(paths.ROOT / "tools" / "bok.py"), "up", "--models-only"],  # G2 W①:同上
                agent_env,
                "模型面常驻（asr/llm/mt/settle/tts；bok up --models-only 幂等重扫）",
            )
        )
    if servers._realtime_demo_enabled():
        # 演示档常驻单元（opt-in，同 _worker_specs 门）：BOK_QWEN_REALTIME=1 才
        # 生成 launchd/schtasks/systemd 单元——健康面 WORKER_PORTS 不收 :8084
        # （默认栈不跑演示档，常列会令 prod status 对未启用部署恒 DEGRADED）。
        units.append(
            (
                "bok-realtime",
                [str(py), "-m", "agent_runtime.realtime_demo"],
                {**agent_env, "BOK_SERVICE": "realtime-demo"},
                "云端 Realtime S2S 演示档",
            )
        )
    return units


def _systemd_staging_dir(explicit: str = "") -> Path:
    """systemd 单元暂存目录解析（2026-09-24 appliance 档）：旗标 > env > 缺省
    `<repo>/release-artifacts/systemd/`（与 build_node_pkg/build_runtime_pkg 的
    产物目录同根，不打包脚本排除面之外另立家）。

    防线：解析结果落在 /etc 下（含 /etc/systemd/system）直接拒——本工具是
    generate-not-execute 姿态，装载归操作员 root；staging 指到系统目录等于
    变相代写。注意 /etc 在 macOS 是 /private/etc 的符号链接，两侧都要 resolve
    后再比，守卫才在双平台都成立。"""
    raw = (explicit or os.environ.get("BOK_SYSTEMD_STAGING_DIR") or "").strip()
    path = Path(raw).expanduser() if raw else paths.ROOT / "release-artifacts" / "systemd"
    resolved = path.resolve()
    etc = Path("/etc").resolve()
    if resolved == etc or etc in resolved.parents:
        raise SystemExit(
            f"[prod] staging 目录不得落在 /etc 下（{resolved}）——装载归操作员 root，"
            "本工具只生成（换个目录或用 --staging-dir/BOK_SYSTEMD_STAGING_DIR 覆盖）")
    return path


def cmd_prod_install(node_agent: bool = False, node_args: list[str] | None = None,
                     open_firewall: bool = False, staging_dir: str = "",
                     with_model_plane: bool = False) -> int:
    """生成生产常驻单元（mac launchd plist / Windows Task Scheduler / Linux
    systemd 暂存），不启动。

    dev 栈用 `bok.py serve`（前台 + run/*.pid）；生产档把常驻进程交给 OS 守护
    （launchd KeepAlive / schtasks RestartOnFailure / systemd Restart=always），
    补上桌面形态天然缺的 watchdog。首次需配 livekit.yaml 生产键（services/livekit-server/livekit.yaml）。

    Windows（无头部署，tools/schtasks_units.py 单点生成）：
      - 缺省真注册 5 个 stack 任务（单机全栈模式；需要管理员 PowerShell）；
      - `--node-agent [node_agent 参数...]` 改注册单个 bok-node-agent 任务
        （节点包拓扑：node_agent 内部经 cmd_up 拉全栈，心跳/凭据参数原样透传），
        例：bok.py prod install --node-agent --cp-url http://cp:8000 --license-key bokn_xxx；
      - `--open-firewall` 执行 netsh 放行（:8000/:7880 TCP+UDP，需管理员；
        缺省只打印计划）。
    Linux（Ubuntu appliance 全栈，tools/systemd_units.py 单点生成）：
      - 只生成到暂存目录（--staging-dir / BOK_SYSTEMD_STAGING_DIR 可覆盖，缺省
        <repo>/release-artifacts/systemd/），**永不写 /etc/systemd/system**；
      - 输出打印 cp + daemon-reload + enable --now 逐字装载指引（root 执行）；
      - /etc/bok/bok.env 由操作员维护（示例文本随输出打印，本工具不代写）。
    --with-model-plane（opt-in，默认 OFF；mac/Windows 档）：追加
      `bok-model-plane` 常驻单元跑 `bok up --models-only`——重启后模型面
      （:8787/:1235/:1236/:1237/:1239）由 launchd/schtasks 补拉，不再等人工。
      默认不传 = 既有装机渲染逐字节零变化；Linux systemd 档暂不接（单元面
      由 systemd_units 模块单点，另窗收编）。
    """
    if paths.is_mac() and node_agent:
        print("[prod] --node-agent 是 Windows 节点拓扑模式；mac 全栈机用标准 5 单元安装",
              file=sys.stderr)
        return 2

    if paths.is_mac():
        unit_dir = paths.app_data_dir() / "units"
        unit_dir.mkdir(parents=True, exist_ok=True)
        log_dir = paths.app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        if open_firewall:
            print("[prod] --open-firewall 是 Windows(netsh) 专用；mac 走系统防火墙应用签名规则，忽略")
        for name, args, env, comment in _prod_units(with_model_plane=with_model_plane):
            label = f"com.bokvoice.{name}"
            arg_xml = "\n".join(f"    <string>{a}</string>" for a in args)
            env_xml = "\n".join(f"      <key>{k}</key>\n      <string>{v}</string>" for k, v in sorted(env.items()))
            plist = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n<dict>\n'
                f"  <key>Label</key><string>{label}</string>\n"
                "  <key>ProgramArguments</key>\n  <array>\n" + arg_xml + "\n  </array>\n"
                f"  <key>WorkingDirectory</key><string>{paths.ROOT}</string>\n"
                "  <key>EnvironmentVariables</key>\n  <dict>\n" + env_xml + "\n  </dict>\n"
                "  <key>RunAtLoad</key><true/>\n"
                "  <key>KeepAlive</key><true/>\n"
                f"  <key>StandardOutPath</key><string>{log_dir / (name + '.log')}</string>\n"
                f"  <key>StandardErrorPath</key><string>{log_dir / (name + '.err.log')}</string>\n"
                "</dict>\n</plist>\n"
            )
            out = unit_dir / f"{label}.plist"
            out.write_text(plist)
            print(f"generated {out.relative_to(paths.app_data_dir())}  ({comment})")
        print(f"\nunits 目录: {unit_dir}")
        print("mac 装载(KeepAlive 自动拉起):  launchctl bootstrap gui/$(id -u) " + str(unit_dir) + "/*.plist")
        print(
            "mac 卸载:                      bok.py prod uninstall"
            "（或 launchctl bootout gui/$(id -u)/com.bokvoice.bok-control-plane 等）"
        )
        print("livekit 生产键/端口见 services/livekit-server/livekit.yaml")
        return 0

    if paths.is_linux() and os.name != "nt":
        # Linux（Ubuntu appliance 全栈，2026-09-24 重构）：systemd 单元只**生成**
        # 到暂存目录（generate-not-execute，同 schtasks 档姿态且更保守——连注册
        # 动作都不代跑）：/etc/systemd/system 与 /etc/bok/bok.env 永不代写，装载
        # 归操作员 root（cp → daemon-reload → enable --now，逐字指引见输出）。
        # Restart=always/RestartSec=3 语义边界见 systemd_units 模块 docstring。
        # `os.name != "nt"` 与 paths.is_linux() 双条件：test_prod_windows 以
        # os.name="nt" 打桩模拟 Windows，而 CI 跑在 ubuntu-latest（paths.is_linux 真）——
        # 只看 paths.is_linux 会在 Linux 上把模拟 Windows 的用例截胡（容器实测 6 红）。
        import systemd_units as _sd

        staging = _systemd_staging_dir(staging_dir)
        if node_agent:
            node_args = list(node_args or [])
            if "--cp-url" not in node_args:
                print("[prod] --node-agent 需要 node_agent 参数（--cp-url 必填）："
                      "bok.py prod install --node-agent --cp-url <url> "
                      "[--license-key KEY] [--ui-dir DIR] ...",
                      file=sys.stderr)
                return 2
            rendered = [_sd.render_node_agent_unit(str(paths.ROOT), str(paths.repo_python()), node_args)]
        else:
            rendered = _sd.render_all_units(str(paths.ROOT), str(paths.repo_python()),
                                            cp_bind_host=core._cp_bind_host())
        staging.mkdir(parents=True, exist_ok=True)
        for fname, text in rendered:
            (staging / fname).write_text(text, encoding="utf-8")
            print(f"generated {staging / fname}")
        print(f"\nstaging 目录: {staging}")
        concrete = [n for n, _t in rendered if n != _sd.SIDECAR_UNIT]
        print("装载（root 逐字执行；先备好 /etc/bok/bok.env——缺文件单元拒绝启动）：")
        print(f"  cp {staging}/*.service /etc/systemd/system/")
        print("  systemctl daemon-reload")
        print("  systemctl enable --now " + " ".join(concrete))
        print("sidecar 模板 bok-sidecar@.service：按文件内注释把对应实例（asr :8787 /")
        print("  tts :8788 / embed :8789）的 ExecStart 替换占位行后再")
        print("  systemctl enable --now bok-sidecar@asr.service（tts/embed 同式）")
        print("日志面: journalctl -u bok-cp.service -f（各单元同式）")
        print("env 文件 /etc/bok/bok.env 示例（本工具不代写；照抄建文件后按需增删）：")
        print(_sd.env_file_sample(str(paths.ROOT)), end="")
        print("livekit 生产键/端口见 services/livekit-server/livekit.yaml")
        return 0

    # Windows 前置的 app-data 目录（units 存 xml 副本 / logs 给 cmd 前缀链重定向；
    # mac 分支已各自创建，Linux 暂存档不落 app-data）。
    unit_dir = paths.app_data_dir() / "units"
    unit_dir.mkdir(parents=True, exist_ok=True)
    log_dir = paths.app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Windows：Task Scheduler 真注册（BootTrigger=开机自起 + RestartOnFailure=
    # 崩溃拉回 + SYSTEM principal，与 mac plists 同信息量；XML/argv 组装见
    # tools/schtasks_units.py，元素顺序/env 前缀链约束见其模块 docstring）。
    import schtasks_units as _sch

    if node_agent:
        node_args = list(node_args or [])
        if "--cp-url" not in node_args:
            print("[prod] --node-agent 需要 node_agent 参数（--cp-url 必填）："
                  "bok.py prod install --node-agent --cp-url <url> "
                  "[--node-token TOK | --license-key KEY] [--ui-dir DIR] ...",
                  file=sys.stderr)
            return 2
        py = paths.repo_python()
        units = [(
            "node-agent",
            [str(py), str(paths.ROOT / "tools" / "node_agent.py"), *node_args],
            {"PYTHONUNBUFFERED": "1"},
            "薄节点守护（cmd_up 拉全栈 + 心跳；参数原样透传 node_agent）",
        )]
    else:
        units = _prod_units(with_model_plane=with_model_plane)

    comspec = os.environ.get("ComSpec", "cmd.exe")
    install_ok = True
    for name, args, env, comment in units:
        tname = _sch.task_name(name)
        arguments = _sch.build_cmd_arguments(env, args, str(paths.ROOT),
                                             log_dir / f"{name}.log",
                                             log_dir / f"{name}.err.log")
        xml = _sch.build_unit_task_xml(name, comspec, arguments, str(paths.ROOT))
        xml_path = _sch.write_task_xml(unit_dir / f"{tname}.xml", xml)
        r = _sch.run_schtasks(_sch.schtasks_create_argv(tname, xml_path))
        if r.returncode == 0:
            print(f"registered {tname}  ({comment})")
            print(f"  xml: {xml_path.relative_to(paths.app_data_dir())}")
            print(f"  action: {comspec} {arguments}")
        else:
            install_ok = False
            tail = ((r.stderr or "").strip() or (r.stdout or "").strip()).splitlines()
            detail = tail[-1][:200] if tail else ""
            print(f"FAILED {tname}: schtasks rc={r.returncode} {detail}"
                  "（需要管理员 PowerShell？）", file=sys.stderr)

    fw_rc = _sch.apply_firewall_rules(open_firewall)
    print(f"\nunits 目录: {unit_dir}")
    print("windows 查询: schtasks /query /tn bok-control-plane 等（开机自起+RestartOnFailure 拉回）")
    print("windows 卸载: bok.py prod uninstall")
    print("livekit 生产键/端口见 services/livekit-server/livekit.yaml")
    return 0 if (install_ok and fw_rc == 0) else 1


def cmd_prod_uninstall(staging_dir: str = "") -> int:
    """卸载生产常驻单元（mac launchd bootout+删 plist / Windows schtasks /delete /
    Linux 清暂存副本）。

    幂等：未安装的单元记 informational 不算失败；真失败（权限/删除被拒）返回 1。
    Windows 卸载面含 bok-node-agent（装过 --node-agent 的机器一把清）。
    Windows 停栈顺序（schtasks /end 只杀 Exec 动作进程 cmd.exe，链式子进程
    存活——probe_windows_lifecycle B5b 断言）：/end 全部 → 按 pidfile 补杀
    幸存子进程（core.cmd_down，ours-only）→ /delete 全部。
    Linux（2026-09-24 暂存档）：只删暂存目录 bok-*.service 副本 + 兼容清扫
    2026-09-20 档落在 app-data/units 的旧副本；系统面（/etc/systemd/system +
    enable 状态）归操作员 root 停用删除（逐字指引见输出，本函数零特权动作）。
    """
    unit_dir = paths.app_data_dir() / "units"
    failures = 0
    if paths.is_mac():
        names = [u[0] for u in _prod_units()]
        # opt-in 模型面单元（--with-model-plane，2026-10-02）：默认清单不含它——
        # 装过的机器卸载不能残留（bootout+删 plist）；未装=plist 不在,静默
        # （零输出变化，不骗 not installed）。
        if (unit_dir / "com.bokvoice.bok-model-plane.plist").exists():
            names.append("bok-model-plane")
        for name in names:
            label = f"com.bokvoice.{name}"
            plist = unit_dir / f"{label}.plist"
            if not plist.exists():
                print(f"[uninstall] {label}: not installed")
                continue
            r = subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
                capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                print(f"[uninstall] {label}: bootout ok")
            else:
                # 未加载（No such process）是幂等卸载的常态，不算失败。
                tail = ((r.stderr or "").strip().splitlines() or [""])[0][:120]
                print(f"[uninstall] {label}: bootout rc={r.returncode} {tail}")
            plist.unlink(missing_ok=True)
            print(f"[uninstall] removed {plist.relative_to(paths.app_data_dir())}")
        return 0

    if paths.is_linux() and os.name != "nt":
        # Linux（2026-09-24 暂存目录档）：删暂存 bok-*.service 副本 + 兼容清扫
        # 2026-09-20 档落在 app-data/units 的旧副本；系统面需操作员 root 停用
        # 删除后 daemon-reload（本函数不代跑特权命令，双条件同 install 的
        # 「模拟 Windows 用例不被 Linux 截胡」判例）。
        staging = _systemd_staging_dir(staging_dir)
        for path in sorted(staging.glob("bok-*.service")):
            path.unlink()
            print(f"[uninstall] removed {path}")
        for path in sorted(unit_dir.glob("bok-*.service")):
            path.unlink()
            print(f"[uninstall] removed legacy {path}")
        print("[uninstall] 系统面（/etc/systemd/system）需以 root 逐字执行：")
        print("  systemctl disable --now bok-cp.service bok-agent-worker.service"
              " bok-interp-fwd.service bok-interp-rev.service bok-node-agent.service")
        print("  rm -f /etc/systemd/system/bok-*.service")
        print("  systemctl daemon-reload")
        return 0

    import schtasks_units as _sch

    names = [u[0] for u in _prod_units()] + ["node-agent"]
    # opt-in 模型面任务（--with-model-plane，2026-10-02）：装过才补进卸载面
    # （xml 副本是「装过」的廉价判据；默认 OFF 的既有装机零输出变化）。
    if (unit_dir / f"{_sch.task_name('bok-model-plane')}.xml").exists():
        names.append("bok-model-plane")
    # /end 只终止任务实例的 Exec 动作进程（本仓恒为 cmd.exe），cmd_up 拉起的
    # 链式子进程（ASR/TTS/LLM/LiveKit/CP/worker）会存活——scripts/probes/
    # probe_windows_lifecycle.py B5b 在真 Windows 上断言这一点。顺序：
    # ①逐任务 /end（停动作进程）→ ②按 pidfile 精确清幸存子进程（ours-only；
    # 勿按镜像名杀——python.exe/livekit-server.exe 是共享镜像，会误杀无关
    # 进程；无 pidfile 的任务树成员如 node_agent 自身无法廉价归因，见 WARNING
    # 尾注）→ ③/delete /f 卸载注册。
    for name in names:
        _sch.run_schtasks(_sch.schtasks_end_argv(_sch.task_name(name)))
    alive = [pf for pf in sorted((paths.app_data_dir() / "run").glob("*.pid"))
             if core._pid_alive(pf)]
    if alive:
        stems = ", ".join(pf.stem for pf in alive)
        print(f"[uninstall] WARNING: schtasks /end 杀不到链式子进程，仍在运行: {stems}"
              " —— best-effort taskkill /T /F（按 pidfile，逐树收割）", file=sys.stderr)
        core.cmd_down()
        print("[uninstall] note: 无 pidfile 记录的任务树成员（如 node_agent 自身）"
              "若仍存活，请按 PID 手工 taskkill——无法按镜像名安全归因")
    for name in names:
        tname = _sch.task_name(name)
        # /delete /f 卸载注册（未安装 rc!=0 + "does not exist" 属幂等常态）。
        r = _sch.run_schtasks(_sch.schtasks_delete_argv(tname))
        combined = ((r.stdout or "") + (r.stderr or "")).lower()
        if r.returncode == 0:
            print(f"[uninstall] {tname}: deleted")
        elif "does not exist" in combined:
            print(f"[uninstall] {tname}: not installed")
        else:
            failures += 1
            tail = ((r.stderr or "").strip() or (r.stdout or "").strip()).splitlines()
            detail = tail[-1][:200] if tail else ""
            print(f"[uninstall] FAILED {tname}: schtasks rc={r.returncode} {detail}",
                  file=sys.stderr)
        xml_path = unit_dir / f"{tname}.xml"
        if xml_path.exists():
            xml_path.unlink()
            print(f"[uninstall] removed {xml_path.relative_to(paths.app_data_dir())}")
    return 1 if failures else 0


def cmd_prod_status() -> int:
    """生产健康面汇总:官方健康端点(livekit GET /、worker GET :port/worker)+ sidecar /health。

    worker 三件(8081-8083)是 _prod_units 的常驻单元,失联必须 DEGRADED——
    旧版只探 :8081,B 线 fwd/rev 双 worker 静默缺失健康面照绿(2026-09-17 补盲)。
    可选增强(mt/settle)起了才纳入,缺模型环境不算降级；llm-raw(:1239)同属可选线
    ——queue proxy 关/整栈未起不点名,代理活而 1239 死=半瘫必须点名（见
    core._llm_raw_status_check_expected）。
    """
    print("bok prod status:")
    # 可选线 :1239（llm-raw）「预期在场才查」：queue proxy 拓扑（mac+开）下
    # mlx 的真实监听口——代理活着而它死了，生成会全灭而旧四表全绿。
    checks = [
        (name, port, path) for (name, port, path) in core.PROD_HTTP_CHECKS
        if port != 1239 or core._llm_raw_status_check_expected()
    ]
    if core.healthy(1236):
        checks.append(("mt-llm", 1236, "/v1/models"))
    if core.healthy(1237):
        checks.append(("settle-llm", 1237, "/v1/models"))
    if core.healthy(1238):
        # :1237 前门闸（2026-10-03 I1）：「起了才查」同款——queue 关的栈没有它。
        checks.append(("settle-proxy", 1238, "/__llmqueue/stats"))
    if core.healthy(8789):
        # W1b embedding sidecar:/health 本体答 ready(未就绪答 ready=false 但
        # 200——下面 "Not Ready" 同款行检不出,暖机窗极短可接受;DOWN 才是缺席)。
        checks.append(("embed", 8789, "/health"))
    if core.healthy(8791):
        # Laya 决策 sidecar:同款「起了才查」——sidecar 未起的部署(模型/venv
        # 缺席或 BOK_LAYA_JUDGE=0)不进表,健康面不假 DEGRADED;/health ok=false
        # (模型加载失败)仍是本体作答算活,真 DOWN 才缺席。
        checks.append(("laya", 8791, "/health"))
    all_ok = True
    for name, port, path in checks:
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3)
            body = r.read(200).decode()[:200]
            ok = (r.status == 200 or r.status == 426) and "Not Ready" not in body
            print(f"  {name:<13} :{port}  {'ok' if ok else 'NON-200'}")
            all_ok = all_ok and ok
        except urllib.error.HTTPError as exc:
            # 426 Upgrade Required = WS 服务本体作答（非升级请求一律 426,
            # urlopen 以 HTTPError 抛出）——比 TCP 探活证据更强,视为活。
            if exc.code == 426:
                print(f"  {name:<13} :{port}  ok (ws worker, 426 upgrade)")
            else:
                print(f"  {name:<13} :{port}  HTTP {exc.code}")
                all_ok = False
        except Exception as exc:
            print(f"  {name:<13} :{port}  DOWN ({exc})")
            all_ok = False
    for name, port in core._worker_ports():
        ok, detail = core._probe_worker(port)
        print(f"  {name:<13} :{port}  {detail}")
        all_ok = all_ok and ok
    print("prod: OK" if all_ok else "prod: DEGRADED")
    return 0 if all_ok else 1


def cmd_prod(cmd: str, node_agent: bool = False,
             node_args: list[str] | None = None,
             open_firewall: bool = False, staging_dir: str = "",
             with_model_plane: bool = False) -> int:
    if cmd == "install":
        return cmd_prod_install(node_agent=node_agent, node_args=node_args,
                                open_firewall=open_firewall,
                                staging_dir=staging_dir,
                                with_model_plane=with_model_plane)
    if cmd == "uninstall":
        return cmd_prod_uninstall(staging_dir=staging_dir)
    return cmd_prod_status()
