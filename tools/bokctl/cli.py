#!/usr/bin/env python
"""bokctl.cli —— 子命令注册与分派表（pip/uv registry 形态；G2 W③，2026-10-05）。

W③ 把「argparse 装配 + cmd 分派」从 core 拆出：core 退为域根（健康面/HTTP
护栏/pid 探针等共享件），命令实现住 bokctl.commands.<cmd>（一命令一模块），
本模块以 ``_COMMANDS``（name → module，模块对象 only）+ ``run(args)`` 协议
分派。``parse_args`` 逐字节自 core 搬入（help 字符串是契约——pre/post 搬运
--help 输出必须 byte-equal，验证基线 /tmp/bok_w3_baseline）；core 以
``parse_args = cli.parse_args / main = cli.main`` 续读门面（tests/脚本
``bok.parse_args``/``bok.main`` 读面零变化）。

分派缝纪律：_COMMANDS 只装模块对象（禁函数值 import）；命令模块内不得在
模块级读 core/commands 属性（环安全），一切互引 call-time 穿模块对象取
（patch 缝=模块属性）。
"""
from __future__ import annotations

import argparse

from bokctl import commands

# 显式 import 即加载：子模块由此注册进 commands 包属性
# （_COMMANDS 的 commands.X 引用依赖这些绑定）。
from bokctl.commands import (  # noqa: F401
    catalog,  # noqa: F401
    demo_setup,  # noqa: F401
    doctor,  # noqa: F401
    down,  # noqa: F401
    download,  # noqa: F401
    manifest,  # noqa: F401
    misc,  # noqa: F401
    monitor,  # noqa: F401
    prod,  # noqa: F401
    serve,  # noqa: F401
    setup,  # noqa: F401
    status,  # noqa: F401
    up,  # noqa: F401
)

# 15 个子命令 → 实现模块（tts-pregen/tts-mine/clean-testdata 三件共用 misc）。
_COMMANDS: dict = {
    "catalog": commands.catalog,
    "manifest": commands.manifest,
    "status": commands.status,
    "serve": commands.serve,
    "down": commands.down,
    "doctor": commands.doctor,
    "tts-mine": commands.misc,
    "clean-testdata": commands.misc,
    "monitor": commands.monitor,
    "up": commands.up,
    "download": commands.download,
    "tts-pregen": commands.misc,
    "prod": commands.prod,
    "setup": commands.setup,
    "demo-setup": commands.demo_setup,
}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bok", description="Bok voice stack launcher (no Docker)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("catalog", "manifest", "status", "serve", "down", "doctor", "tts-mine", "clean-testdata", "monitor"):
        sub.add_parser(name)
    p_up = sub.add_parser("up", help="拉起全栈；--models-only=只拉模型面（跳过通话面）")
    p_up.add_argument("--models-only", action="store_true",
                      help="只起模型面（asr/llm/mt/settle/tts+proxy；云 TTS 档跳过 :8788），"
                           "不拉 LiveKit/worker/monitor（prod 常驻单元 bok-model-plane 用）")
    p_dl = sub.add_parser("download", help="下载平台模型表（--only 子集=装机选型）")
    p_dl.add_argument("--only", nargs="*", default=None,
                      help="只下载指定模型键（asr tts_preset tts_clone llm llm_4b mt settle embedding laya）")
    sub.add_parser("tts-pregen", help="离线预合成 TTS 本地缓存(参数透传:--greetings/--objects/--fillers/--cp/--model)")
    p_prod = sub.add_parser("prod", help="生产常驻单元与健康面")
    p_prod.add_argument("action", nargs="?", default="status",
                        choices=["install", "status", "uninstall"])
    p_prod.add_argument("--node-agent", action="store_true",
                        help="[install;Windows] 注册单个 node_agent 任务，其余参数原样透传")
    p_prod.add_argument("--open-firewall", action="store_true",
                        help="[install;Windows] 执行 netsh 防火墙放行(需管理员；缺省只打印计划)")
    p_prod.add_argument("--staging-dir", default="",
                        help="[install/uninstall;Linux] systemd 单元暂存目录"
                             "（缺省 <repo>/release-artifacts/systemd；"
                             "BOK_SYSTEMD_STAGING_DIR 同义，旗标优先）")
    p_prod.add_argument("--with-model-plane", action="store_true",
                        help="[install;mac/Windows] 追加 opt-in 常驻单元 "
                             "bok-model-plane（bok up --models-only，重启补拉模型面；"
                             "缺省 OFF=既有装机零变化）")
    p_setup = sub.add_parser("setup", help="First-run model readiness / download")
    p_setup.add_argument("action", nargs="?", default="status", choices=["status", "download"])
    p_demo = sub.add_parser("demo-setup", help="一键配置演示档(云 ASR/云 LLM/云 TTS/本地 MT)")
    p_demo.add_argument("--asr-key", default="",
                        help="豆包新式单 Key(argv 优先;env BOK_DEMO_ASR_KEY;必填)")
    p_demo.add_argument("--asr-resource-id", default="",
                        help="豆包资源 ID(env BOK_DEMO_ASR_RESOURCE_ID;缺省=服务端默认)")
    p_demo.add_argument("--tts-key", default="",
                        help="MiniMax API Key(argv 优先;env BOK_DEMO_TTS_KEY;必填)")
    p_demo.add_argument("--deepseek-key", default="",
                        help="DeepSeek API Key(argv 优先;env BOK_DEMO_DEEPSEEK_KEY;必填)")
    p_demo.add_argument("--deepseek-base-url", default="",
                        help="DeepSeek 端点(env BOK_DEMO_DEEPSEEK_BASE_URL;"
                             "缺省 https://api.deepseek.com/v1)")
    p_demo.add_argument("--mt-model", default="",
                        help="B 线 MT 本地模型显式路径(env BOK_DEMO_MT_MODEL;"
                             "缺省=空走 env 缺省链 :1236 MT2)")
    # tts-pregen/tts-mine 参数原样透传给执行脚本,顶层不做校验
    args, extra = p.parse_known_args(argv)
    args.extra = list(extra)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    return _COMMANDS[args.cmd].run(args)


if __name__ == "__main__":
    raise SystemExit(main())
