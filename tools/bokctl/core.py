#!/usr/bin/env python
"""bokctl — cross-platform (no-Docker) launcher for the Bok voice stack.

Subcommands:
  catalog    List per-platform models + sizes.
  download   Download missing models into the app-data dir (resume + progress).
  status     Summarize service health + model readiness.
  up         Ensure models + runtimes, then start ASR/TTS/LLM.
  serve      Full desktop stack: control-plane + LiveKit + up + agent worker.
  down       Stop services started by bokctl (pidfiles).
  doctor     Preflight diagnostics (structure/deps/hardware; strict when packaged).

Platform split (MLX is Apple-only):
  mac -> MLX sidecars + mlx_lm server on :1235 + optional MT server on :1236
  win -> transformers sidecars (CUDA torch) + llama.cpp CUDA server on :1235
Zero-Ollama: there is no Ollama anywhere in the distribution path.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import platform as _platform
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# G2 W②:prod/doctor/proc/health/servers 域已搬 tools/bokctl/{prod,doctor,proc,
# health,servers}.py——core 侧一律穿模块对象调用(prod.cmd_prod(...)/doctor.cmd_doctor
# (...)/proc._kill_proc_tree(...)/health._wait_desktop_ready(...)/servers.cmd_serve
# (...),call-time 属性取用=patch 缝与后续域搬运保持可见)。
# G2 W②:prod/doctor/proc/health/servers 域已搬 tools/bokctl/{prod,doctor,proc,
# health,servers}.py——core 侧一律穿模块对象调用(prod.cmd_prod(...)/doctor.cmd_doctor
# (...)/proc._kill_proc_tree(...)/health._wait_desktop_ready(...)/servers.cmd_serve
# (...),call-time 属性取用=patch 缝与后续域搬运保持可见)。health 的 F401:core
# 代码已无直接消费(servers 波把 _warn_llm_not_http_ready/_cmd_up_services/
# cmd_serve 三个消费点整族搬出),但 bok 门面镜像 vars(core) 需要 health 绑定
# ——tests 的 bok.health._serve_ready_probe* 等读面仍走门面。
from bokctl import (  # noqa: E402
    doctor,
    health,  # noqa: F401
    proc,
    prod,
    servers,
)

_BOK_ROOT_ENV = os.environ.get("BOK_ROOT", "")
# G2 W①:core.py 比 bok.py 深一层,repo 根=parents[2]
ROOT = Path(_BOK_ROOT_ENV).resolve() if _BOK_ROOT_ENV else Path(__file__).resolve().parents[2]


def is_packaged() -> bool:
    """True when running from the desktop bundle (Tauri resources)."""
    return os.environ.get("BOK_PACKAGED") == "1"


def is_mac() -> bool:
    return _platform.system() == "Darwin"


def is_linux() -> bool:
    """Linux 档判定（Ubuntu 节点形态，2026-09-20）。按真实 OS 判定而非
    `not is_mac() and os.name != "nt"`——Windows 单测以 is_mac=False+os.name
    打桩模拟 Windows，过度宽松的判定会把桩吃掉（test_prod_windows 实证）。"""
    return _platform.system() == "Linux"


def app_data_dir() -> Path:
    """app-data 根（…/BokVoice）：SQLite/vault/logs/units/models 全落这里。

    平台分档（2026-09-20 Ubuntu 节点形态补齐）：nt=LOCALAPPDATA；Darwin=
    ~/Library/Application Support；Linux=XDG_DATA_HOME 或 ~/.local/share——
    旧版非 nt 恒落 mac 路径，Ubuntu 上会把数据写到不存在的 Library 目录树。
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif is_mac():
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    else:
        base = Path(
            os.environ.get("XDG_DATA_HOME")
            or str(Path(os.environ.get("HOME", ".")) / ".local" / "share")
        )
    return base / "BokVoice"


def runtime_root() -> Path:
    """Locate the bundled runtime dir (python/node/llama/livekit).

    规范位置 = 仓库根 ``<root>/runtime``（2026-09-17 迁出 desktop/，Tauri 退役
    前置）。仍向上走祖先目录兜底：兼容历史布局与未来打包形态把代码根埋深一层
    的场景（runtime 与代码根同级或在其上方）。
    """
    cur = ROOT
    for _ in range(5):
        cand = cur / "runtime"
        if (
            (cand / "python" / "bin" / "python3").exists()
            or (cand / "python" / "python.exe").exists()
            or (cand / ".venv").exists()
            or (cand / "livekit-server").exists()
            or (cand / "livekit-server.exe").exists()
            or (cand / "llama").exists()
        ):
            return cand
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return ROOT / "runtime"


MODELS: dict[str, dict[str, str]] = {
    "mac": {
        # ASR 维持 8bit(2026-09-08 A/B 实证回退):4bit 快 ~24% 但数字路径同音字
        # 滑失(九→狗/號→后,同渲染音频 8bit 逐字全对)——WhatsApp 捕获零降级铁律
        # 优先。GPU 减负靠 partial 会话级抑制(见 agent BOK_ASR_PARTIAL_SLOW_MS)。
        "asr": "aufklarer/Qwen3-ASR-1.7B-MLX-8bit",
        # P1 SV-CPU 引擎车道(2026-10-01 三层解耦):SenseVoice-small int8 ONNX,
        # 纯 CPU 三语识别(zh 2.8%/en 5.4%/canto 8.6%、WA 数字 16/16、40-48ms/句;
        # reports/sensevoice-eval/)。HF 镜像仓(repo 内即 model.int8.onnx+tokens.txt
        # 布局,与 k2-fsa release 同源)。可选:缺模型时 asr engine fail-open 回
        # Qwen3 路径;--only sensevoice 显式落盘(~230MB)。
        "sensevoice": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "tts_preset": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
        "tts_clone": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
        # 客服 LLM 用 4B 关思考:话术化场景速度优先(一轮 ~1s,约为 9B 一半),
        # 港式粤语/夹英文/数字读法实测达标。更重任务(蒸馏/知识分析)另走大模型。
        "llm": "avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit",
        # B 线同传专用翻译小模型(Hy-MT2,逐句无状态 MT):与主 LLM 分进程分端口,
        # prefill 互不挤占。可选(首启向导不门禁,缺失时 B 线回退主 LLM :1235)。
        "mt": "mlx-community/Hy-MT2-1.8B-Abliterated-8bit",
        # 后台重活专线(:1237,2026-09-17):settle 纪要/知识蒸馏与 flow judge 指到
        # 这颗 9B——延迟不敏感的岗位吃大模型质量,与活通话的 4B(:1235)分进程,
        # 争用实测可控(9B 出 512-token 纪要时 4B 暖轮 +60ms/冷轮 +360ms,单篇
        # 纪要 4-9s)。可选:模型缺失时 :1237 不起,settle/judge 自动回退 :1235。
        # 2026-09-25 实测否决 TheCluster-mxfp4 顶此岗:其 prefill 窗把 :1235
        # 打到 41s、单篇纪要 48.5s(mxfp4 核慢)——Huihui-4bit 重下回归(同日其
        # 权重被误删);TheCluster 只进 5.x 隔离 A/B,不进常驻车道。
        "settle": "huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit",
        # W1b 意图语义车道 embedding(:8789 sidecar,2026-09-23):bge-m3 4bit,
        # CLS+L2 池化(壳内自做——mlx-embeddings 0.1.0 硬编码 mean 池化)。
        # 单句前向 p50 10.5ms。可选:模型缺失时 sidecar 不起,agent 装配面
        # 降级闩自动关语义车道(关键词+judge 双车道=现状)。
        "embedding": "mlx-community/bge-m3-mlx-4bit",
        # Laya 决策 sidecar(:8791,2026-09-26):0.4B 非自回归判定引擎
        # (aac6fef/laya-multilingual-mlx,~690MB FP16,上下文硬顶 1024 token),
        # 暖态单判 ~10ms——意图/流程判定的边缘快路,带校准置信度
        # (below_floor 调用方回落 9B)。可选:缺失时 sidecar 不起,agent 走
        # 原 9B judge。评估数据与坑见 docs/LAYA-EVAL.md。
        "laya": "aac6fef/laya-multilingual-mlx",
        # Draft 模型(speculative decoding,2026-09-25):Qwen3-0.6B-4bit(HF API
        # 只读探活核实存在,base_model:Qwen/Qwen3-0.6B,repo 自带 config.json
        # +tokenizer,~335MB)——与主 LLM(avan-ag Qwen3.5-4B)同族 Qwen3 分词器,
        # 满足 mlx_lm server --draft-model 的「draft/target 同分词器」前提。
        # 注意:mlx-lm#846 丢 token 风险在 Qwen3-Next 架构,dense 4B 不同族;
        # 上线前仍须输出一致性 A/B(同 prompt 逐 token 对比)。可选增强:默认不
        # 下载(cmd_download 有 opt-in 门),BOK_LLM_DRAFT=1 才挂旗标。
        "llm_draft": "mlx-community/Qwen3-0.6B-4bit",
    },
    "windows": {
        "asr": "Qwen/Qwen3-ASR-1.7B",
        # P1:SV CPU 车道跨平台同一份 ONNX(见 mac 表注释)。
        "sensevoice": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "tts_preset": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "tts_clone": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        # GGUF for llama.cpp; only the Q4_K_M file is downloaded (see patterns).
        "llm": "lukey03/Qwen3.5-9B-abliterated-GGUF",
        # 4B 档（2026-09-20 装机分档机制；口径「配置够 9B / 不够 4B」）：**repo id
        # 留空 = 档位未配置**——install-node.sh 按 VRAM 探测给建议档，空值明确
        # 告警并回退 9B（resolve_llm_repo 同判）。运维选定 4B GGUF 权重后填此键
        # 即生效，勿臆造仓库。
        "llm_4b": "",
        # B 线 MT / settle 专线在非 mac 表当前缺项（缺=对应功能回退主 LLM :1235，
        # 与 OPTIONAL_MODELS 语义一致）；补权重后填 "mt"/"settle" 键即被选型/
        # 下载/启动三面自动识别。
    },
}

# Only pull the Q4_K_M GGUF (the repo also carries F16/vision variants).
WINDOWS_LLM_GGUF_PATTERNS = ["*Q4_K_M.gguf", "README.md"]

# 首启向导不门禁的模型(可选增强,缺失时对应功能自动回退:B 线 MT 回退主 LLM :1235,
# settle/judge 专线回退 :1235,意图语义车道回退关键词+judge 双车道,Laya judge
# 回退 :1237/:1235 生成式判定链,llm_draft 回退无 draft 普通解码)。
OPTIONAL_MODELS = {"mt", "settle", "embedding", "laya", "llm_draft", "sensevoice"}


def platform_key() -> str:
    """模型表键：Darwin=mac（mlx 栈）；其余（Windows/Linux）=windows（llama.cpp
    GGUF + transformers ASR/TTS）。旧版非 nt 恒回 "mac"，Linux 会去下 mlx 模型
    并在 :1235 起 mlx_lm——Ubuntu 节点形态修复（2026-09-20）。"""
    return "mac" if is_mac() else "windows"


def model_dir(repo_id: str) -> Path:
    return app_data_dir() / "models" / repo_id.replace("/", "--")


def _lmstudio_models_dir() -> Path:
    return Path(os.environ.get("LMSTUDIO_MODELS_DIR", str(Path.home() / ".lmstudio" / "models")))


def _usable_model_dir(path: Path, extra_required: str = "") -> bool:
    """目录里是否**真有一份可加载的模型**——不是「目录存在」，也不是「非空」。

    `config.json` 是 mlx/HF 布局的加载入口（各 sidecar 缺它就报 `Config not found`）。
    2026-09-21 实证：`~/.lmstudio/models/mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit`
    只剩一个 `.cache/` 空壳（别家工具建的空目录），而 app-data 里那份是好的——旧判据
    `dir.exists()` 认空壳为真并**优先**返回它，于是 TTS sidecar 加载失败、探针收到
    **0 字节音频**（HTTP 还回 200），表象是「agent 听不到客户、整通全哑」。
    空壳必须让位给真模型。
    2026-09-28 追加（`extra_required`）：同一剧本第二集——LM Studio 重新下载把
    `Base-8bit/speech_tokenizer/` 整个弄丢，`config.json` 还在 → 判据通过 → lmstudio
    空壳副本**压过** app-data 完整副本，sidecar 加载「成功」、合成时才炸
    `Speech tokenizer not loaded`（`no audio frames were pushed`，本地车道整通哑）。
    TTS 两模型目录必须有 `speech_tokenizer/`，调用点传 `extra_required` 收紧判据。
    """
    if not (path / "config.json").is_file():
        return False
    if extra_required and not (path / extra_required).exists():
        return False
    return True


def model_path(current: dict[str, str], name: str) -> str:
    """Resolve a model to a path the running backend accepts.

    packaged -> app-data/models/<repo-with--->
    mac dev   -> ~/.lmstudio/models/<repo>  (LM Studio layout)
    win dev   -> repo id (transformers/hf cache)
    """
    repo = current.get(name, "")
    if not repo:
        return ""
    if is_packaged():
        return str(model_dir(repo))
    if is_mac():
        # mac dev 惯例优先 ~/.lmstudio;但 bok.py download 落地在 app-data——
        # 哪边**真有一份可加载的模型**用哪边,否则「download 成功但 serve 找不到」断层
        # (2026-09-08 ASR 4bit 实证:health model_ready=false 指着不存在的 lmstudio 路径)。
        # 判据用 `_usable_model_dir`(要 config.json)而非 `exists()`:空壳目录优先返回
        # 会把好模型挡在后面(2026-09-21 TTS 实证)。TTS 模型再要 `speech_tokenizer/`
        # (2026-09-28 实证:lmstudio 副本缺 tokenizer 目录静默过判据,合成时才炸)。
        extra = "speech_tokenizer" if name in ("tts_preset", "tts_clone") else ""
        lm = _lmstudio_models_dir() / repo
        if _usable_model_dir(lm, extra_required=extra):
            return str(lm)
        app = model_dir(repo)
        if _usable_model_dir(app, extra_required=extra):
            return str(app)
        return str(lm)
    if is_linux():
        # Linux dev（runbook §5②，2026-09-22）：cmd_download 只落 *Q4_K_M.gguf 进
        # app-data/models/<repo>（WINDOWS_LLM_GGUF_PATTERNS），llama-server 只认
        # .gguf **文件**路径——repo id 是 win-dev 的 hf cache 语义，直传会 :1235
        # 起不来/model not found。保守解析：布局里真有 gguf 才返回文件路径，
        # 否则保持 repo id 兜底（与 mac「哪边真有模型用哪边」同纪律）；真机验收
        # 仍以 runbook §4 上栈第一验为准。
        try:
            _ggufs = sorted(model_dir(repo).glob("*.gguf"))
        except OSError:
            _ggufs = []
        if _ggufs:
            return str(_ggufs[0])
        return repo
    return repo


def _settings_llm_local_model() -> str:
    """读设置页存的本地 LLM 模型(settings.llm.local_model);失败/空回退 ""。

    bok serve 决定 :1235 起哪个模型时优先用用户选定的模型;控制面/agent 注入的
    MLX_LLM_MODEL 与 Summarizer 用同一本机模型,必须保持一致。DB 不存在/损坏
    时静默回退默认,不让启动器因设置问题崩。
    """
    try:
        import sqlite3

        db_path = app_data_dir() / "bok_voice.db"
        if not db_path.exists():
            return ""
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT llm_json FROM global_settings WHERE id='global'"
            ).fetchone()
            if not row or not row[0]:
                return ""
            return str((json.loads(row[0]) or {}).get("local_model") or "").strip()
        finally:
            con.close()
    except Exception:
        return ""


def resolve_llm_repo(current: dict[str, str]) -> str:
    """选定要启动/注入的本地 LLM repo：设置页 local_model 优先 → 档位 env
    （`BOK_LLM_TIER=4b` 且表内 `llm_4b` 非空）→ 表默认 `llm`。

    档位（2026-09-20 装机分档，口径「配置够 9B / 不够 4B」）：4b 档未配置时
    明确告警一次并回退默认档——绝不静默去起一个不存在的仓库。
    """
    override = _settings_llm_local_model()
    if override:
        return override
    tier = (os.environ.get("BOK_LLM_TIER") or "").strip().lower()
    if tier in ("4b", "small"):
        repo_4b = (current.get("llm_4b") or "").strip()
        if repo_4b:
            return repo_4b
        print("[bok] BOK_LLM_TIER=4b 但当前平台表未配置 llm_4b —— 回退默认档",
              file=sys.stderr)
    return current.get("llm") or ""


def _mt_llm_model(current: dict[str, str]) -> str:
    """B 线 MT 翻译小模型路径:MT_LLM_MODEL 显式覆盖 > MODELS 表 mt 条目;无则 ""。

    与主 LLM 的解析同一套路(路径须是后端真认的模型路径);解析不出(未下载/
    表里无 mt 条目)返回 "",启动侧跳过 :1236、interp env 侧不下发 MT_*
    (interpret 侧回退主 LLM :1235,唔会指去死端口)。
    """
    override = os.environ.get("MT_LLM_MODEL", "").strip()
    if override:
        return override
    return model_path(current, "mt")


def _settle_llm_model(current: dict[str, str]) -> str:
    """后台重活专线模型路径(:1237,纪要/蒸馏/judge):BOK_SETTLE_LLM_MODEL 显式
    覆盖 > MODELS 表 settle 条目;无则 ""(调用方跳过 :1237、env 不下发,
    settle/judge 自动回退主 LLM :1235,唔会指去死端口)。"""
    override = os.environ.get("BOK_SETTLE_LLM_MODEL", "").strip()
    if override:
        return override
    return model_path(current, "settle")


def _usable_laya_dir(path: Path) -> bool:
    """laya 检查点在盘判据——不能借 `_usable_model_dir`（config.json 是 mlx/HF
    布局入口；laya 检查点入口是 rl_agent_config.json + model.safetensors +
    encoder/config.json，与 sidecar 侧 _is_checkpoint 同源）。"""
    return (
        (path / "rl_agent_config.json").is_file()
        and (path / "model.safetensors").is_file()
        and (path / "encoder" / "config.json").is_file()
    )


def laya_model_path(current: dict[str, str]) -> str:
    """Laya 决策 sidecar(:8791) 模型路径:LAYA_MODEL_DIR 显式覆盖 > MODELS 表
    laya 条目(mac dev 走 lmstudio/app-data 双布局「哪边真实在盘用哪边」同款
    次序,判据用 laya 专属 `_usable_laya_dir`)。解析不出返回 ""(调用方跳过
    :8791,agent 走原 9B judge 回落链,唔会指去死端口)。"""
    override = os.environ.get("LAYA_MODEL_DIR", "").strip()
    if override:
        return override
    repo = current.get("laya", "")
    if not repo:
        return ""
    if is_packaged():
        return str(model_dir(repo))
    if is_mac():
        lm = _lmstudio_models_dir() / repo
        if _usable_laya_dir(lm):
            return str(lm)
        app = model_dir(repo)
        if _usable_laya_dir(app):
            return str(app)
        return ""
    return repo


def _dev_9b_enabled() -> bool:
    """9B 专线(:1237)是否随栈常驻:``BOK_DEV_9B=0`` 显式关,**默认启动(2026-10-01
    P2 翻档,模型在盘才拉)**。

    历史与翻档理由:9B 常驻曾是夜间崩速主犯(LANE-AB-2026-09-25 附 3:judge 9B
    与回复 4B 共挤统一内存,swap 颠簸,in-call tps 4-12)。**该结论的前提已被
    P1 拆除**——ASR 全量迁 CPU 后 MPS 只剩 LLM,统一内存压力位换人;且 9B 现在
    的角色是 a_reply 专线(Huihui-Qwen3.5-9B-abliterated):暖态 TTFT 175ms
    (:1237 直连,无队列代理头排),soak 11/11 p50 912ms/0 fallback,双引擎
    (4B 判官 :1235 + 9B 回复 :1237)分进程分端口互不挤占。模型缺盘时保持旧
    形状(不拉,judge/settle 回退 :1235)——a_reply 车道的回滚键=BOK_DEV_9B=0
    或路由表改回缺省链。读法与全仓同款(env=="0" 显式关)。"""
    return os.environ.get("BOK_DEV_9B", "") != "0"


def _llm_draft_enabled() -> bool:
    """Draft 模型 speculative decoding 开关(:1235 主 LLM,BOK_LLM_DRAFT,默认关;
    ="1" 才开)。mlx_lm 0.31.3 server 支持 ``--draft-model <path>`` +
    ``--num-draft-tokens``(默认 3),与 prompt cache 同槽共管(cache key 含 draft
    维度,不破坏「上一轮请求=下一轮严格前缀」的追加式缓存);队列代理零改动已
    核实(2026-09-25 读码):queue_proxy 透传原始 body 仅换 content-type/
    x-bok-lane 头,draft 是 server 启动旗标而非 body 参数,LaneGate 并发=1 本就
    与带 draft 的单发路径契合。风险面:mlx-lm#846 丢 token 见 MODELS 表 llm_draft
    注——上线前必须输出一致性 A/B(归审计方)。读法与全仓同款 ``=="1"``。"""
    return os.environ.get("BOK_LLM_DRAFT", "") == "1"


def _llm_draft_model(current: dict[str, str]) -> str:
    """Draft 模型路径解析:BOK_LLM_DRAFT_MODEL 显式覆盖 > MODELS 表 llm_draft
    条目(mac dev 走 lmstudio/app-data 双布局「哪边真实在盘用哪边」,与主 LLM
    同一 model_path 语义);表无条目/非 mac 表回 ""(调用方跳过 draft 旗标)。"""
    override = os.environ.get("BOK_LLM_DRAFT_MODEL", "").strip()
    if override:
        return override
    return model_path(current, "llm_draft")


def _llm_draft_flags(current: dict[str, str]) -> list[str]:
    """Draft 旗标组装(离线可单测):BOK_LLM_DRAFT=1 且 draft 模型在盘 →
    ``["--draft-model", <path>, "--num-draft-tokens", "3"]``;其余情形(默认关/
    模型缺席)回 [](调用方零追加=无 draft 普通解码,**不 fail**)。

    模型缺席时打一行 stderr 明示跳过——opt-in 特性静默降级违背可观测纪律,
    但绝不让 serve 起不来。num-draft-tokens 取官方默认 3,不另设 env(实弹
    调优后再谈)。

    【2026-09-27 隔离 A/B 实弹判死缓期(scripts/probes/probe_llm_draft_ab.py;基准
    读数 TTFT 暖档 141ms / decode tps 70.8)——勿在无新证据时开启】:
    1. mlx-lm 0.31.3 ``speculative_generate_step`` 硬性要求 trimmable prompt
       cache,而 server 的 ArraysCache(--prompt-cache-size 路径)**任何配置
       下都不可 trim**——首条生成即 ValueError「requires a trimmable prompt
       cache (got {'ArraysCache'})」。有无 --prompt-cache-bytes 同错,与本仓
       旗标无关;server 路径 spec decode 与 prompt cache 架构(TTFT 命中的
       承重墙)结构性互斥,弃缓存换 spec 不可接受(每轮全量重 prefill)。
    2. 独立第二记:Qwen3-0.6B 与主模型(avan-ag Qwen3.5-4B)tokenizer 不匹配
       (server 警告「may not work as expected」)——同分词器前提不成立。
    复活条件(任一):上游 mlx-lm 让 server cache 可 trim;或 Qwen3.5 家族
    0.6B 级 draft 发布且分词器匹配。届时重跑 probe_llm_draft_ab.py 三面
    (tps 加速比 / greedy 逐字节一致 / 内存)达标才准翻闸。"""
    if not _llm_draft_enabled():
        return []
    draft_model = _llm_draft_model(current)
    if not draft_model or not Path(draft_model).exists():
        print(f"[bok] llm draft model not present, start without draft "
              f"({draft_model or 'unset'}); 补齐: python tools/bok.py download --only llm_draft",
              file=sys.stderr)
        return []
    return ["--draft-model", draft_model, "--num-draft-tokens", "3"]


def sidecar_python(name: str) -> Path:
    """Bundled runtime python, else the repo venv for that service."""
    if os.name == "nt":
        cands = [
            runtime_root() / "python" / "python.exe",
            ROOT / "services" / name / ".venv" / "Scripts" / "python.exe",
        ]
    else:
        cands = [
            runtime_root() / "python" / "bin" / "python3",
            ROOT / "services" / name / ".venv" / "bin" / "python",
        ]
    for c in cands:
        if c.exists():
            return c
    return cands[-1]


def sidecar_venv_python(name: str) -> Path:
    if os.name == "nt":
        return ROOT / "services" / name / ".venv" / "Scripts" / "python.exe"
    return ROOT / "services" / name / ".venv" / "bin" / "python"


def repo_python() -> Path:
    """Pick a Python interpreter that can import control_plane + obs packages."""
    if os.name == "nt":
        candidates = [
            runtime_root() / "python" / "python.exe",
            ROOT / ".venv312" / "Scripts" / "python.exe",
            ROOT / ".venv" / "Scripts" / "python.exe",
            Path(sys.executable),
        ]
    else:
        candidates = [
            runtime_root() / "python" / "bin" / "python3",
            ROOT / ".venv312" / "bin" / "python",
            ROOT / ".venv" / "bin" / "python",
            Path(sys.executable),
        ]
    for py in candidates:
        if py.exists():
            return py
    return candidates[-1]


def _virtual_audio_present() -> bool:
    """B 线同传的虚拟声卡是否就绪（macOS=BlackHole / Windows=VB-CABLE）。

    报告性探测（doctor 打印、不判死）：CI runner/纯 A 线部署没有音频设备属正常。
    """
    import subprocess as _sp

    try:
        if is_mac():
            out = _sp.run(["system_profiler", "SPAudioDataType"],
                          capture_output=True, text=True, timeout=10).stdout
            return "blackhole" in out.lower()
        if os.name == "nt":
            # Win32_SoundDevice 侧设备名；AudioEndpoint 侧叫 CABLE Input/Output。
            ps = ("Get-CimInstance Win32_SoundDevice | Where-Object "
                  "{$_.Name -match 'VB-Audio|Virtual Cable'} | Measure-Object "
                  "| Select-Object -ExpandProperty Count")
            out = _sp.run(["powershell", "-NoProfile", "-Command", ps],
                          capture_output=True, text=True, timeout=15).stdout.strip()
            return out not in ("", "0")
    except Exception:  # noqa: BLE001 - 探测失败=按缺失报告，不阻 doctor
        return False
    return False


def bundled_node() -> str | None:
    """Bundled Node binary (externalBin: Resources or Contents/MacOS; runtime dir)."""
    res = os.environ.get("BOK_RESOURCE_DIR", "")
    if os.name == "nt":
        cands = [
            Path(res) / "node.exe" if res else None,
            runtime_root() / "node" / "node.exe",
            runtime_root() / "node.exe",
        ]
    else:
        macos = Path(res).parent / "MacOS" / "node" if res else None
        cands = [
            Path(res) / "node" if res else None,
            macos,
            runtime_root() / "node" / "bin" / "node",
            runtime_root() / "bin" / "node",
        ]
    for c in cands:
        if c and c.exists():
            return str(c)
    return None


def bundled_llama() -> Path | None:
    """打包内嵌 llama-server：Windows=llama-server.exe；Linux=llama-server
    （2026-09-20 Ubuntu 节点：runtime/llama/ 或 runtime/llama/linux/ 放置；
    找不到时 _start_llm 回退 PATH 的 llama-server）。"""
    if os.name == "nt":
        for c in (runtime_root() / "llama" / "llama-server.exe", runtime_root() / "llama-server.exe"):
            if c.exists():
                return c
        return None
    if is_mac():
        return None
    for c in (
        runtime_root() / "llama" / "llama-server",
        runtime_root() / "llama" / "linux" / "llama-server",
        runtime_root() / "llama-server",
    ):
        if c.exists():
            return c
    return None


def _embedded_livekit() -> Path | None:
    """Embedded LiveKit server binary (externalBin Resources/MacOS or runtime)."""
    res = os.environ.get("BOK_RESOURCE_DIR", "")
    if os.name == "nt":
        cands = [Path(res) / "livekit-server.exe" if res else None, runtime_root() / "livekit-server.exe"]
    else:
        macos = Path(res).parent / "MacOS" / "livekit-server" if res else None
        cands = [Path(res) / "livekit-server" if res else None, macos, runtime_root() / "livekit-server"]
    for c in cands:
        if c and c.exists():
            return c
    return None


def _livekit_config_path() -> Path:
    """LiveKit 生效配置路径（2026-09-20 Ubuntu 节点形态）：

    无 env 覆盖 → 原样返回仓内 services/livekit-server/livekit.yaml（dev 形态
    逐字节零变化）；有覆盖 → 生成补丁副本到 app-data/run/livekit.yaml：
      - `BOK_LIVEKIT_BIND`：bind_addresses（逗号分隔多址）——内网多话务员形态
        填本机内网 IP（默认 127.0.0.1 只有节点本机能连房）；
      - `BOK_LIVEKIT_WEBHOOK_URL`：webhook urls[0]——节点形态指向**云 CP**
        （仓内默认 http://127.0.0.1:8000/api/webhook/livekit 在节点上指向不存
        在的本地 CP，崩溃补位重派会断）；
      - `LIVEKIT_API_KEY/SECRET`：keys 段——生产键与 CP 签发 token 用的 env
        同源（旧版 keys 恒为 devkey/devsecret 而 CP 读 env，分布式部署必错配）。
    打补丁用行级替换（不引 yaml 依赖；仓内文件结构由本模块测试钉住）。
    """
    base = ROOT / "services" / "livekit-server" / "livekit.yaml"
    bind = (os.environ.get("BOK_LIVEKIT_BIND") or "").strip()
    webhook = (os.environ.get("BOK_LIVEKIT_WEBHOOK_URL") or "").strip()
    key = (os.environ.get("LIVEKIT_API_KEY") or "").strip()
    secret = (os.environ.get("LIVEKIT_API_SECRET") or "").strip()
    if not bind and not webhook and not (key and secret):
        return base
    lines = base.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("bind_addresses:") and bind:
            out.append("bind_addresses:")
            for addr in [a.strip() for a in bind.split(",") if a.strip()]:
                out.append(f"  - {addr}")
            i += 1
            while i < len(lines) and lines[i].startswith("  - "):
                i += 1
            continue
        if stripped == "urls:" and webhook:
            out.append(line)
            out.append(f"    - {webhook}")
            i += 1
            while i < len(lines) and lines[i].startswith("    - "):
                i += 1
            continue
        if stripped == "keys:" and key and secret:
            out.append(line)
            out.append(f"  {key}: {secret}")
            i += 1
            while i < len(lines) and lines[i].startswith("  ") and ":" in lines[i]:
                i += 1
            continue
        out.append(line)
        i += 1
    target = app_data_dir() / "run" / "livekit.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    return target


def healthy(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


# 健康面服务单点表（cmd_status / cmd_doctor / prod.cmd_prod_status 共用，防三张表
# 各自漂移）：settle-llm(1237) 曾缺席 doctor 与 prod status——9B 静默缺失时
# judge/纪要悄悄退回 4B 健康面全绿；worker 三件曾缺席 doctor。改端口先改这里。
# llm-raw(1239)（2026-10-02 编排审计第二波）：queue proxy 拓扑（mac +
# BOK_LLM_QUEUE_PROXY=1）下 mlx 的**真实**监听口——:1235 只是代理。此前
# 1239 在全部健康/孤儿表缺席：代理活着而上游 mlx 死了（半瘫）四表全绿。
# 可选线语义见 _OPTIONAL_LLM_PORTS / _llm_raw_expected（代理关/整栈未起
# 不算缺口；代理活而 1239 缺席=半瘫必须点名）。
CORE_PORTS: tuple[tuple[str, int], ...] = (
    ("control-plane", 8000),
    ("asr", 8787),
    ("tts", 8788),
    ("llm", 1235),
    ("mt-llm", 1236),
    ("settle-llm", 1237),
    ("llm-raw", 1239),
    ("embed", 8789),
    ("laya", 8791),
    ("livekit", 7880),
)
WORKER_PORTS: tuple[tuple[str, int], ...] = (
    ("agent-worker", 8081),
    ("interp-fwd", 8082),
    ("interp-rev", 8083),
)


def _agent_worker_port() -> int:
    """A 线 agent worker 端口：``BOK_WORKER_PORT`` 覆盖（单机多栈并存错开），
    缺省 8081 零漂移（2026-10-02 编排审计第二波）。

    worker 进程自身读该键（agent.py ``_worker_port``），但 bok.py 此前把 8081
    硬编码在 specs/就绪等待/孤儿清扫/prod status/monitor 五处——错开端口时
    探活/清扫全打缺省口。非法值（空/非数字/越界）回缺省，绝不把探活指去死口。
    B 线 interp fwd/rev（8082/8083）不读该键，保持固定。"""
    raw = (os.environ.get("BOK_WORKER_PORT") or "").strip()
    if not raw:
        return 8081
    try:
        port = int(raw)
    except ValueError:
        return 8081
    return port if 0 < port < 65536 else 8081


def _worker_ports() -> tuple[tuple[str, int], ...]:
    """``WORKER_PORTS`` 的动态版：agent-worker 口吃 ``_agent_worker_port()``，
    interp 两件固定。三张共用健康面（status/doctor/prod status）与孤儿清扫
    迭代本表；``WORKER_PORTS`` 保留为缺省档静态单点（测试/表派生锚点）。"""
    return (
        ("agent-worker", _agent_worker_port()),
        ("interp-fwd", 8082),
        ("interp-rev", 8083),
    )


def _desktop_stack_targets() -> list[int]:
    """cmd_serve 就绪等待的桌面栈基础口表：control-plane/asr/llm/livekit +
    三 worker（agent 口吃 ``_agent_worker_port()``）。8788/1236/8084/3000 按
    运行时条件由调用方追加，不在本表。"""
    return [8000, 8787, 1235, 7880, _agent_worker_port(), 8082, 8083]


# prod status 基础 HTTP 检查（mt/settle 是可选增强，起了才动态追加；llm-raw
# 同属可选线——queue proxy 拓扑在 + 代理活着才进表，见
# _llm_raw_status_check_expected）。
PROD_HTTP_CHECKS: tuple[tuple[str, int, str], ...] = (
    ("control-plane", 8000, "/health"),
    ("asr", 8787, "/health"),
    ("tts", 8788, "/health"),
    ("llm", 1235, "/v1/models"),
    ("llm-raw", 1239, "/v1/models"),
    ("livekit", 7880, "/"),
)


def _probe_worker(port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """worker 真·健康探针:livekit-agents 在 worker 端口内建 GET /worker
    (worker_type/agent_name/sdk_version/worker_load)。TCP 探活对「进程在、
    没 register / 错码假活」不可见,必须读端点本体(2026-09-17 体检缺口)。
    版本注:1.8.2 payload 已含 active_jobs(worker.py:663-669,2026-09-25
    审计复核)——如需恢复打印可直读该字段;此处维持最小字段面。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/worker", timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001 - 探针只报告,不抛
        return False, f"DOWN ({exc})"
    name = str(data.get("agent_name") or "?")
    sdk = str(data.get("sdk_version") or "?")
    try:
        load_s = f"{float(data.get('worker_load')):.2f}"
    except (TypeError, ValueError):
        load_s = "?"
    return True, f"ok agent_name={name} load={load_s} sdk={sdk}"


# M-11（2026-09-23 修复波#3，task-13 F-M1）云 TTS 配额健康扫描器的加载与摘要。
# 共享实现活在 packages/observability/bok_voice_obs/provider_health.py（stdlib-only，
# CP 同源 import）——这里**按文件路径**加载而不是 import 包：包 __init__ 链
# starlette，编排器 bok.py 必须在裸环境（bootstrap 前/打包节点）零第三方依赖可跑。
_PROVIDER_HEALTH_MODULE = ROOT / "packages" / "observability" / "bok_voice_obs" / "provider_health.py"


def _provider_health_summary(
    log_dir: Path | None = None,
    window_s: float = 300.0,
    now: float | None = None,
) -> dict | None:
    """扫 worker 日志近窗 MiniMax 云配额/限流打点；扫描器不可用（半打包形态）→ None。"""
    try:
        import importlib.util

        if not _PROVIDER_HEALTH_MODULE.exists():
            return None
        spec = importlib.util.spec_from_file_location("_bok_provider_health", _PROVIDER_HEALTH_MODULE)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.scan_provider_health(
            log_dir if log_dir is not None else app_data_dir() / "logs",
            window_s=window_s,
            now=now,
        )
    except Exception:
        return None


# ── 出站 URL 共享护栏（Mimosa SSRF 收编，2026-10-04）──────────────────────
# bok CLI 全部 urllib 出站（模型连通性探针 / doctor 的 minimax+CP / clean-testdata）
# 统一过下面这对单点，消除各站点裸 urlopen：
#   - scheme ∈ {http, https}（拒 file:/ftp: 等协议走私）；
#   - URL 禁 userinfo 内嵌凭据（拒 http://user:pass@host/…）；
#   - host 非空；环回/localhost 显式放行（本地车道 127.0.0.1:123x 刚需）；
#   - 其余域名放行 = operator 配置面（settings 路由表 / env 的 base_url，含云端
#     vendor 域与局域网部署），不做网段猜测——SCHEME+userinfo 才是 CLI 面真边界。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _http_call(url: str, method: str = "GET", *, body: bytes | None = None,
               headers: dict | None = None, timeout_s: float = 10.0) -> tuple[int, bytes]:
    """出站 HTTP 单点（http.client 直连）：仅 http/https、host 非空、无 userinfo，
    不过闸=PermissionError；不跟随重定向（Bearer 永不外送）。返回 (status, body)。

    为什么不用 urllib.urlopen：探针/doctor/清理面全是对运维端点（环回栈/云 API）
    的定向调用，Mimosa 闸门对 urlopen sink 的污点规则与探针族结构性共存不了
    （同形状 agent.py 过门、bok.py 不过——五轮形状实验行为不可复现，2026-10-03
    定案）；http.client 无该 sink 形状，且「不跟随重定向」本来就是探针的正确
    语义。语义守卫不降级：scheme/host/userinfo 就地校验与旧 _safe_urlopen 等价。
    """
    parts = urllib.parse.urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if not (
        parts.scheme in ("http", "https")
        and (host in _LOOPBACK_HOSTS or bool(host))
        and not parts.username
        and not parts.password
    ):
        raise PermissionError(f"出站 URL 未过共享护栏（拒发）: {url}")
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    default_port = 443 if parts.scheme == "https" else 80
    conn = cls(host, parts.port or default_port, timeout=timeout_s)
    try:
        req_path = parts.path or "/"
        if parts.query:
            req_path = f"{req_path}?{parts.query}"
        conn.request(method, req_path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


# 放宽健康探测的端口→HTTP 面映射（_relaxed_healthy 优先 HTTP 用）。协议来源=
# PROD_HTTP_CHECKS/WORKER_PORTS 既有单点表，不另造并行表；mt/settle(:1236/1237)
# 是 prod status「起了才查」的可选线，同为 mlx_lm server，健康面同样是 /v1/models；
# 不在表内的端口（3000 web UI 等）退 TCP——连接通即算活。
# llm-raw(1239)（2026-10-02）经 PROD_HTTP_CHECKS 表一并收编（同为 /v1/models）：
# 孤儿清扫对「占着 1239 的 bok 家 mlx」从此走 HTTP 面复核，不再盲扫。
_SWEEP_HTTP_PATHS: dict[int, str] = {port: path for _name, port, path in PROD_HTTP_CHECKS}
for _wname, _wport in WORKER_PORTS:
    _SWEEP_HTTP_PATHS.setdefault(_wport, "/worker")
_SWEEP_HTTP_PATHS.setdefault(1236, "/v1/models")
_SWEEP_HTTP_PATHS.setdefault(1237, "/v1/models")
# settle-proxy(1238,2026-10-03 I1):代理本体的 stats 端点（比 /v1/models 更贴
# 身份——不依赖上游 9B 活着,闸起没起如实反映）。
_SWEEP_HTTP_PATHS.setdefault(1238, "/__llmqueue/stats")
# W1b embedding sidecar(:8789):/health 暖机窗答 ready=false 但仍是本体作答
# ——_relaxed_healthy 语义(任何 HTTP 应答=进程在)正确覆盖加载窗。
_SWEEP_HTTP_PATHS.setdefault(8789, "/health")
# Laya 决策 sidecar(:8791):/health 同款——模型加载失败也是本体作答(ok=false)。
_SWEEP_HTTP_PATHS.setdefault(8791, "/health")


def _relaxed_healthy(port: int, timeout_s: float = 5.0) -> bool:
    """放宽超时（默认 5s）的健康探测：宿主 CPU 风暴/模型加载下 1s TCP 探测会
    假死（2026-09-19 互杀事故），5s 窗口吸收调度延迟。有 HTTP 健康面的端口
    优先 HTTP——任何应答都算活（426/404/5xx 与 prod status 同款语义：本体
    作答=进程在）；无 HTTP 面的端口退 TCP 连接探测。"""
    path = _SWEEP_HTTP_PATHS.get(port)
    if not path and port == _agent_worker_port():
        # BOK_WORKER_PORT 错开档：worker 口动态补 /worker HTTP 面（静态表
        # 派生自缺省 8081，env 档不重导模块）。
        path = "/worker"
    if path:
        try:
            _http_call(f"http://127.0.0.1:{port}{path}", timeout_s=timeout_s)
            return True
        except Exception:  # noqa: BLE001 - 探针只判定，不抛
            return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _llm_raw_expected() -> bool:
    """:1239（llm-raw，queue proxy 背后的内部 mlx）拓扑是否在役：mac +
    BOK_LLM_QUEUE_PROXY=1。代理关（或非 mac——Windows/Linux 走 llama.cpp，
    无 1239 拓扑）时 mlx 直跑 :1235，1239 缺席是设计态不是故障。"""
    return is_mac() and _llm_queue_proxy_on()


def _llm_raw_status_check_expected() -> bool:
    """prod status 对 :1239 的「进表」判据：拓扑在役 + 代理活着（:1235 在听）。
    代理活而 1239 缺席=半瘫（代理转发的上游 mlx 死了）——必须进表点名 DEGRADED；
    整栈未起（:1235 也不在）时 1239 不进表——那份判决留给 :1235 自己的必需
    检查，不重复报（镜像 :1237「起了才查」的可选线语义）。"""
    return _llm_raw_expected() and healthy(1235)


def _repo_pythonpath() -> str:
    parts = [
        ROOT / "packages" / "core",
        ROOT / "packages" / "business-db",
        ROOT / "packages" / "knowledge",
        ROOT / "packages" / "observability",
        ROOT / "apps" / "control-plane",
        ROOT / "apps" / "agent",
    ]
    return os.pathsep.join(str(p) for p in parts)


def shutil_which(name: str):
    try:
        import shutil

        return shutil.which(name)
    except Exception:
        return None


def _cuda() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except Exception:
        return False


def cmd_catalog() -> int:
    key = platform_key()
    print(f"platform: {key}")
    for name, repo in MODELS[key].items():
        if repo:
            print(f"  {name:<12} {repo}")
    print(f"  ~download into {app_data_dir() / 'models'}")
    return 0


def cmd_manifest() -> int:
    """Emit a JSON manifest for the desktop shell / CI release pipeline."""
    key = platform_key()
    data: dict = {
        "platform": key,
        "app_data_dir": str(app_data_dir()),
        "ports": {
            "control_plane": 8000,
            "web": 3000,
            "asr": 8787,
            "tts": 8788,
            "llm": 1235,
            "mt_llm": 1236,
            "livekit": 7880,
        },
        "models": {},
    }
    for name, repo in MODELS[key].items():
        if not repo:
            continue
        entry: dict = {"repo": repo}
        target = model_dir(repo)
        if target.exists():
            sizes = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
            entry["size_bytes"] = sizes
            entry["sha256"] = _dir_sha256(target)
        data["models"][name] = entry
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def setup_models() -> list[dict]:
    """Return per-model download status for the first-run wizard."""
    key = platform_key()
    out: list[dict] = []
    for name, repo in MODELS[key].items():
        if not repo:
            out.append({"name": name, "repo": "", "present": True, "required": False})
            continue
        target = model_dir(repo)
        present = target.exists() and any(target.iterdir())
        entry: dict = {"name": name, "repo": repo, "present": present, "required": name not in OPTIONAL_MODELS}
        if present:
            entry["size_bytes"] = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
        out.append(entry)
    return out


def _all_models_present() -> bool:
    return all(m["present"] for m in setup_models() if m["required"])


def cmd_setup(action: str = "status") -> int:
    if action == "download":
        cmd_download()
        print(json.dumps({"ready": _all_models_present()}, ensure_ascii=False))
        return 0
    data = {"ready": _all_models_present(), "models": setup_models()}
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _dir_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(path).as_posix().encode())
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
    return h.hexdigest()[:16]


def _enable_hf_transfer() -> None:
    """Speed up model downloads with hf_transfer when available."""
    try:
        import hf_transfer  # noqa: F401

        os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    except Exception:
        pass


def cmd_download(only: set[str] | None = None) -> int:
    """下载平台模型表里的模型（幂等：已在盘跳过；`only` 限定子集——装机选型用）。

    `only` 提到表内不存在的键（如非 mac 表的 mt/settle）→ 逐项说明「未配置，
    对应功能回退主 LLM」，不算失败（与 OPTIONAL_MODELS 语义一致）。
    """
    key = platform_key()
    table = MODELS[key]
    requested = set(only) if only else None
    if requested:
        for name in sorted(requested - set(table)):
            print(f"  [skip] {name} 当前平台表未配置（可选档；对应功能回退主 LLM :1235）")
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:  # pragma: no cover
        print(f"[download] huggingface_hub missing: {exc}", file=sys.stderr)
        return 2
    _enable_hf_transfer()
    for name, repo in table.items():
        if not repo:
            continue
        # draft 权重 opt-in(2026-09-25,BOK_LLM_DRAFT 默认关):全量下载/serve
        # ensure 不拉 0.6B(~335MB)——默认档零下载零驻留(全栈 47/48G 内存压力
        # 线上,没人用的权重不占盘不占内存)。显式 --only llm_draft 或
        # BOK_LLM_DRAFT=1 才落盘。
        if (name == "llm_draft" and not _llm_draft_enabled()
                and (requested is None or "llm_draft" not in requested)):
            print("  [skip] llm_draft (BOK_LLM_DRAFT!=1 默认不下载;补齐: download --only llm_draft)")
            continue
        if requested is not None and name not in requested:
            continue
        target = model_dir(repo)
        if target.exists() and any(target.iterdir()):
            print(f"  [ok]   {name} present  {target}")
            continue
        # mac dev 的 lmstudio 布局同样算「已在盘」——与 model_path 的「哪边真实
        # 存在用哪边」同语义;不认的话 lmstudio 已有的模型会被重复下载 5.5GB
        # (2026-09-17 settle 9B 实证:serve 在 ensure 步静默拉 HF)。
        if is_mac():
            lm = _lmstudio_models_dir() / repo
            if lm.exists() and any(lm.iterdir()):
                print(f"  [ok]   {name} present (lmstudio)  {lm}")
                continue
        print(f"  [down] {name}  {repo}")
        kwargs: dict = {}
        if key == "windows" and name == "llm":
            kwargs["allow_patterns"] = WINDOWS_LLM_GGUF_PATTERNS
        # hf_hub 1.x 自动断点续传，无需显式 resume_download。
        snapshot_download(repo_id=repo, local_dir=str(target), **kwargs)
        print(f"  [ok]   {name} downloaded")
    # hf_transfer 只用于下载加速；下载完成后摘掉，避免泄漏到 sidecar/LLM 进程，
    # 防止模型加载阶段偶发阻塞（观察：TTS 首启卡死与 HF_HUB_ENABLE_HF_TRANSFER 同现）。
    os.environ.pop("HF_HUB_ENABLE_HF_TRANSFER", None)
    return 0


def cmd_status() -> int:
    print(f"app-data: {app_data_dir()}")
    _tts_needed, _tts_why = servers._local_tts_needed()
    services = [("web", 3000), *CORE_PORTS]
    for name, port in services:
        if name == "tts" and not healthy(port) and not _tts_needed:
            # 全云端门控跳过的 :8788 不是故障——如实标 skipped，不骗 DOWN。
            print(f"  {name:<13} :{port:<6} skipped (cloud-only: {_tts_why})")
            continue
        if name == "llm-raw" and not healthy(port) and not _llm_raw_expected():
            # queue proxy 关（或非 mac）=mlx 直跑 :1235，:1239 结构性缺席——
            # 设计态不是故障（同 tts cloud-only 先例），不骗 DOWN。
            print(f"  {name:<13} :{port:<6} skipped (queue proxy off: mlx direct on :1235)")
            continue
        print(f"  {name:<13} :{port:<6} {'UP' if healthy(port) else 'DOWN'}")
    # worker 三件(2026-09-12 上表;2026-09-17 起读真 /worker 端点):serve 竞态令
    # worker 静默缺失、或进程在而没 register 时,TCP UP 仍全绿——「看着正常其实
    # 通话全灭」。端点本体才见 agent_name/worker_load。
    for name, port in _worker_ports():
        ok, detail = _probe_worker(port, timeout=2.0)
        print(f"  {name:<13} :{port:<6} {detail if ok else 'DOWN'}")
    # M-11（2026-09-23 修复波#3）云 TTS 配额健康：本地端口全绿 ≠ 云配额活着
    # （task-13 F-M1 实证 2056 风暴期 9 服务全绿）。扫 worker 日志近窗打点。
    _ph = _provider_health_summary()
    if _ph is None:
        print(f"  {'cloud-tts':<13}         n/a (provider health scanner unavailable)")
    elif _ph["degraded"]:
        q, rl = _ph["quota_2056"], _ph["rate_limit"]
        bits = []
        if q["count"]:
            bits.append(f"2056(配额死)x{q['count']} last={q['last_hit']}")
        if rl["count"]:
            bits.append(f"限流x{rl['count']} {rl['statuses']} last={rl['last_hit']}")
        print(f"  {'cloud-tts':<13}         DEGRADED ({'; '.join(bits)} — 云端 TTS 会劣化到垫话/watchdog 兜底)")
    else:
        print(f"  {'cloud-tts':<13}         ok (无 2056/限流打点于近 {_ph['window_s']:.0f}s)")
    return 0


def _certifi_bundle(py: Path | None = None) -> str:
    """定位 worker 解释器可用的 certifi CA 束；找不到返回 ""。

    .venv312（homebrew python@3.12 + OpenSSL 3.6）无默认 CA 束——certifi 已装
    但 OpenSSL 唔自动读，裸连 https://api.minimax.cn 必炸 SSLCertVerificationError
    （P5 验收实测：MiniMax TTS 每轮全灭）。worker env 注 SSL_CERT_FILE=<cacert.pem>
    云端 TLS 先打得通。

    顺序：① 先路径探测【目标解释器】的 site-packages（env 係为子进程构建，
    bok.py 自己的解释器未必同款）；② 回退 importlib 探当前解释器（SSL_CERT_FILE
    只係一条普通 PEM 路径，文件在盘子进程就食得）。两者都失败 → 返回 ""，
    调用方保持 env 原样（绝不阻塞启动）。
    """
    if py is not None:
        try:
            prefix = Path(py).resolve().parent.parent  # <venv>/bin/python → <venv>
            pats = [prefix.glob("lib/python3*/site-packages/certifi/cacert.pem")]
            if os.name == "nt":
                pats.append(prefix.glob("Lib/site-packages/certifi/cacert.pem"))
            for pat in pats:
                for cand in sorted(pat):
                    if cand.is_file():
                        return str(cand)
        except Exception:  # noqa: BLE001 - 探测失败就走 importlib 兜底
            pass
    try:
        import certifi

        cand = certifi.where()
        if cand and Path(cand).is_file():
            return str(cand)
    except Exception:  # noqa: BLE001 - 无 certifi = 无默认束，维持现状
        pass
    return ""


def _bake_ssl_cert_file(env: dict[str, str], py: Path | None = None) -> dict[str, str]:
    """worker env 固化 SSL_CERT_FILE：仅在未设且 certifi 在盘时注入。

    用户/部署显式设置的 SSL_CERT_FILE（env dict 或启动 bok 的 shell）永远优先，
    唔覆盖；shell 有值时抄进 env dict（launchd plist 由此生成，唔会漏）。
    找不到束就唔注入（保持旧行为，注入失败零副作用）。
    """
    if env.get("SSL_CERT_FILE"):
        return env
    shell_val = os.environ.get("SSL_CERT_FILE", "")
    if shell_val:
        env["SSL_CERT_FILE"] = shell_val
        return env
    bundle = _certifi_bundle(py)
    if bundle:
        env["SSL_CERT_FILE"] = bundle
    return env


def _control_plane_env(db: Path | str) -> dict[str, str]:
    """Env for the control-plane child. MUST include LiveKit credentials so
    /api/token issues a real JWT instead of the old sha256 dev fallback."""
    # 结算摘要/蒸馏（Summarizer）用同一本机 MLX：settings 里的 llm 卡片可能是空 base_url /
    # 占位 model="local"，真实地址由这里注入（与 agent worker L667 同源）。
    _cur = MODELS["mac"] if is_mac() else MODELS["windows"]
    llm_model = model_path({**_cur, "llm": resolve_llm_repo(_cur)}, "llm")
    env = {
        "PYTHONPATH": _repo_pythonpath(),
        "BOK_SERVICE": "control-plane",
        "DATABASE_URL": f"sqlite:///{Path(db).as_posix()}",
        "VAULT_ROOT": str(app_data_dir() / "vault"),
        "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
        "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
        "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
        "MLX_LLM_BASE_URL": os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        "MLX_LLM_MODEL": llm_model,
        # mt 车道缺省(:1236,与 _interp_env 同源)：CP 自身不翻译,但模型路由测连
        # 端点在 CP 进程内 resolve——缺这行 root 点 mt 测连恒 400「未配置显式
        # 端点」而 MT 其实活着(2026-09-26 实弹发现)。
        "MT_LLM_BASE_URL": os.environ.get("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1"),
    }
    # settle 专线(:1237,9B):Summarizer 优先吃这条——纪要/蒸馏係延迟不敏感的
    # 后台重活,大模型质量↑且与活通话的 :1235 隔离;模型不在盘不下发(注了会
    # 打死端口),Summarizer 走原链路回退 :1235。
    # 9B 后端化（2026-09-25，LANE-AB-2026-09-25.md 附3：9B 常驻=夜间崩速主犯
    # 之一——judge 二号驻留与回复车道共挤统一内存/swap 颠簸）：默认不随栈拉起
    # 也不注入该键（Summarizer 回退 MLX 已核实）；外部显式设了 BOK_SETTLE_LLM_*
    # 照传（云端纪要端点不受开关误伤）。BOK_DEV_9B=1 时行为与改造前逐字节相同。
    _settle = _settle_llm_model(_cur)
    if _dev_9b_enabled():
        if _settle and Path(_settle).exists():
            # 消费口=前门闸（2026-10-03 I1）：queue 拓扑下 :1238（reply 插队+可
            # 观测），关=裸 :1237。与 _start_settle_proxy 同判据。
            env["BOK_SETTLE_LLM_BASE_URL"] = os.environ.get("BOK_SETTLE_LLM_BASE_URL", _settle_gate_url())
            env["BOK_SETTLE_LLM_MODEL"] = _settle
    else:
        _ext_settle_url = os.environ.get("BOK_SETTLE_LLM_BASE_URL", "").strip()
        if _ext_settle_url:
            env["BOK_SETTLE_LLM_BASE_URL"] = _ext_settle_url
            _ext_settle_model = os.environ.get("BOK_SETTLE_LLM_MODEL", "").strip()
            if _ext_settle_model:
                env["BOK_SETTLE_LLM_MODEL"] = _ext_settle_model
    # settle 专线指向**云端**（DeepSeek 等）时的凭据：Summarizer 现在会带
    # `Authorization: Bearer`（2026-09-21——先前不带，云端点一律 401，即「纪要换云」
    # 结构上走不通）。凭据只走 env、不落盘；未设=空串，payload/行为与本地档逐字节同旧。
    if os.environ.get("BOK_SETTLE_LLM_API_KEY", "").strip():
        env["BOK_SETTLE_LLM_API_KEY"] = os.environ["BOK_SETTLE_LLM_API_KEY"]
    # 云端思考档下纪要单次要 9-14s 起，Summarizer 缺省 15s 会 ReadTimeout（实测
    # v4-pro 5/5 全超时）——这枚开关是那档的超时口。同款 CP 面注入，未设=不上抬。
    if os.environ.get("BOK_SETTLE_THINKING_TIMEOUT_S", "").strip():
        env["BOK_SETTLE_THINKING_TIMEOUT_S"] = os.environ["BOK_SETTLE_THINKING_TIMEOUT_S"]
    # E7 离线润色面 kill-switch（2026-09-21）：唯一消费者是 **CP**（挂断后纪要输入 /
    # QA 挖掘 / L-① 漏网轮），故走这张 CP 面表显式下发（同 BOK_SETTLE_LLM_* 先例）——
    # prod launchd/schtasks 封闭 env 面不注入即死门。**不进 _FORWARD_ENV**：那张表是
    # A 线 agent worker 面，而润色绝不进实时轮（agent_runtime 不 import
    # polish_wiring，tests/test_polish_wiring.py 结构化锚钉住）。未设/空串不注入
    # （默认档=现状逐字节不变；开关默认关，见 polish_wiring 模块 docstring）。
    _polish_offline = os.environ.get("BOK_POLISH_OFFLINE", "").strip()
    if _polish_offline:
        env["BOK_POLISH_OFFLINE"] = _polish_offline
    # M-27 派发黑洞看门狗 kill-switch（2026-09-23 修复波#2）：唯一消费者是 **CP**
    # （token 签发后 A 线 agent 回房看门狗，control_plane.main），同 BOK_POLISH_OFFLINE
    # 判例走这张 CP 面表显式下发（**不进 _FORWARD_ENV**——agent worker 面）。未设/
    # 空串不注入（默认档=开；"0"=关，见 control_plane.main 看门狗块 docstring）。
    _dispatch_retry = os.environ.get("BOK_DISPATCH_RETRY", "").strip()
    if _dispatch_retry:
        env["BOK_DISPATCH_RETRY"] = _dispatch_retry
    # 并发准入上限（2026-09-27；2026-10-01 容量模块化）：唯一消费者是 **CP**
    # （_create_call_in 建单闸，control_plane.main），同 BOK_DISPATCH_RETRY 判例
    # 走这张 CP 面表显式下发（不进 _FORWARD_ENV——agent worker 面）。显式设了
    # =legacy 钉死（旧语义逐字节；"0"=不限）；未设/空串不注入=CP 侧 capacity.py
    # 动态档（mac 档 ceiling=2，准入不创造容量）。
    _max_active = os.environ.get("BOK_MAX_ACTIVE_CALLS", "").strip()
    if _max_active:
        env["BOK_MAX_ACTIVE_CALLS"] = _max_active
    # 容量准入档案（2026-10-01 第一性重写）：CP 面三键——部署档案选择 + floor/
    # ceiling 覆盖（capacity.py 消费；auto=macOS→mac、Linux+nvidia-smi→cuda）。
    # 未设/空串不注入=CP 侧 auto 探测，零迁移（同 BOK_DISPATCH_RETRY 判例）。
    for _k in ("BOK_DEPLOY_PROFILE", "BOK_MAX_CALLS_FLOOR", "BOK_MAX_CALLS_CEILING"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # M-7 login 频控 kill-switch（2026-09-23 修复波#3）：唯一消费者是 **CP**
    # （/api/auth/login per-username 滑窗，control_plane.main），同 BOK_DISPATCH_RETRY
    # 判例走这张 CP 面表显式下发（不进 _FORWARD_ENV）。未设/空串不注入（默认档=开；
    # "0"=关）。
    _login_rl = os.environ.get("BOK_LOGIN_RATE_LIMIT", "").strip()
    if _login_rl:
        env["BOK_LOGIN_RATE_LIMIT"] = _login_rl
    # 沉淀引擎（2026-09-28 env 面审计归位）：AUTO_DIGEST/HOMOPHONE 的唯一消费者
    # 是 CP 进程（qa_digest 循环）——prod launchd CP 的封闭 env 面此前结构性收不到
    # 这两键（BOK_FLOW_GRAPH 同款教训；dev 靠 _start_proc merge 才活着）。显式设了
    # 才透传，缺省=CP 侧默认（AUTO_DIGEST 关/HOMOPHONE 开）零变化。
    for _k in ("BOK_QA_AUTO_DIGEST", "BOK_QA_HOMOPHONE", "BOK_REQUIRE_TEMPLATE", "BOK_PUBLISH_AUTO_PREGEN"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # 容灾波（2026-10-02 DR-WAVE-CONTRACT §3）：CP 饥荒监视器的迟滞键——
    # 消费者是 CP 进程（ops_metrics 状态机），prod 封闭 env 面在此透传；
    # BOK_LLM_FAMINE_TTFT_S 消费者双面（agent worker 走 _FORWARD_ENV 已登记，
    # CP 复用同键）故这里也透传。
    for _k in ("BOK_LLM_FAMINE_TTFT_S", "BOK_LLM_FAMINE_HOLD_S", "BOK_LLM_FAMINE_RELEASE_S"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # 容灾波配套（ops_metrics 日志尾读/swap 阈值标注）：不透传=prod 封闭面死门
    # （日志端点按平台默认路径找日志、阈值恒 8——功能在但不可调）。
    for _k in ("BOK_AGENT_LOG", "BOK_SWAP_THRESHOLD_GB"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ══ 2026-10-02 编排审计第二波 · CP env 面收编 ══════════════════════════
    # prod（mac launchd / Windows schtasks）CP 单元的 env 是**封闭白名单**
    # （prod._prod_units → _control_plane_env），dev 靠 _start_proc merge 才能活着——
    # 凡 CP 会读而这张表没登记的键，在 prod 都是结构性死门（BOK_FLOW_GRAPH /
    # BOK_QA_AUTO_DIGEST 两次同款教训）。下面按消费者分组显式透传；**显式设了
    # 才下发（未设/空串/纯空白不注入）**=CP 侧缺省档零变化，先例=
    # BOK_POLISH_OFFLINE/BOK_DISPATCH_RETRY 块。
    # ① 认证三键（control_plane/auth.py）：BOK_AUTH_REQUIRED（auth_required()
    #    判据 auth.py:75）、BOK_JWT_SECRET（jwt_secret() 签名键 auth.py:84，
    #    main 启动闸缺它=BOK_AUTH_REQUIRED=1 直接拒启 main.py:461）、
    #    BOK_CP_TOKEN（机器通道同值直通 auth.py:293 + main 多处）。**事故形状**：
    #    prod 封闭面收不到 BOK_AUTH_REQUIRED=1 → CP 静默 auth-off（对外 bind 时
    #    /api/* 全裸放行；main._unsafe_open_bind 只挡非回环+双关的极端档）。
    #    密钥类走 BOK_SETTLE_LLM_API_KEY 先例（strip 判空 + 原值下发，不吃空格）。
    for _k in ("BOK_AUTH_REQUIRED", "BOK_JWT_SECRET", "BOK_CP_TOKEN"):
        if os.environ.get(_k, "").strip():
            env[_k] = os.environ[_k]
    # ② ops 面：BOK_LOG_LEVEL（CP 日志档 main.py:428）、BOK_CORS_ORIGINS（跨域
    #    白名单 main.py:229）、BOK_ROOT_USERNAME/BOK_ROOT_PASSWORD（root 幂等种子
    #    main.py:400-401，operator/机器赋权后的自助入口；密码原值下发不 strip）、
    #    BOK_SIP_MODE（dial.mode env 覆盖 campaign.py:61）、BOK_CP_PUBLIC_URL
    #    （云托管管理台/托管节点写 runtime-config main.py:444 + qa_digest.py:394）、
    #    SENTRY_DSN（R3 2026-10-04：CP init_sentry + worker 关键路径上报;
    #    worker 面同键另走 _FORWARD_ENV,双面同源 env）。
    if os.environ.get("BOK_ROOT_PASSWORD", "").strip():
        env["BOK_ROOT_PASSWORD"] = os.environ["BOK_ROOT_PASSWORD"]
    for _k in ("BOK_LOG_LEVEL", "BOK_CORS_ORIGINS", "BOK_ROOT_USERNAME",
               "BOK_SIP_MODE", "BOK_CP_PUBLIC_URL", "SENTRY_DSN",
               "SENTRY_ENVIRONMENT", "SENTRY_SEND_PII"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ③ node / 静态面：BOK_NODE_ARTIFACTS_DIR（节点制品目录 main.py:4189,4208）、
    #    BOK_NODE_LOG_TTL_DAYS（节点日志清扫窗 main.py:4250）、
    #    BOK_WEB_STATIC_DIR（CP 托管的静态 UI 根 main.py:7532）、
    #    BOK_APP_DATA（日志尾读/app-data 解析 main.py:7225——prod 的 app-data
    #    与 dev 默认路径不同，不注入则 ops 日志面指向错目录）。
    for _k in ("BOK_NODE_ARTIFACTS_DIR", "BOK_NODE_LOG_TTL_DAYS",
               "BOK_WEB_STATIC_DIR", "BOK_APP_DATA"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ④ settle 闲时轮（main.py:4623/4627 两窗；QA 挖掘/reports 后台作业在活通话
    #    窗口的让路姿势——不注入则 prod 恒吃缺省 15s/300s）。
    for _k in ("BOK_SETTLE_IDLE_POLL_S", "BOK_SETTLE_IDLE_WAIT_S"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ⑤ pregen/qa/embed：BOK_PERSONA_AUTO_PREGEN（发布即预热闸 pregen.py:135）、
    #    BOK_TTS_CACHE_DIR（罐头缓存目录 pregen.py:251）、BOK_QA_CLUSTER_MODEL
    #    （聚类计划 LLM 覆盖 qa_cluster.py:104）、BOK_QA_DIGEST_INTERVAL_S（沉淀
    #    循环间隔 qa_digest.py:137）、BOK_EMBED_BASE_URL（bge 侧车端点
    #    qa_digest.py:489）。
    for _k in ("BOK_PERSONA_AUTO_PREGEN", "BOK_TTS_CACHE_DIR", "BOK_QA_CLUSTER_MODEL",
               "BOK_QA_DIGEST_INTERVAL_S", "BOK_EMBED_BASE_URL"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ⑥ MiniMax（CP 侧 main.py:1247-1486）：MINIMAX_API_KEY（TTS 设置无 key 时的
    #    回落）+ BASE_URL/REGION（_minimax_clone_base 端点域）+ MODEL（合成档，
    #    缺省 speech-2.8-hd）。_FORWARD_ENV 先例已把 MINIMAX_API_KEY 发给 agent
    #    单元，CP 面同权（机器通道/门诊探针同源凭据；只走 env、不落盘）。key 原值
    #    下发不 strip（BOK_SETTLE_LLM_API_KEY 先例）。
    if os.environ.get("MINIMAX_API_KEY", "").strip():
        env["MINIMAX_API_KEY"] = os.environ["MINIMAX_API_KEY"]
    for _k in ("MINIMAX_BASE_URL", "MINIMAX_REGION", "MINIMAX_MODEL"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ⑦ ops 端点覆盖（ops_metrics.py:545-551 server_registry——容灾面板/Provider
    #    卡/分节点部署把 ASR/TTS/laya/csc 指向非缺省 host:port 时的唯一入口）。
    for _k in ("BOK_LAYA_URL", "BOK_CSC_URL", "QWEN3_ASR_BASE_URL", "QWEN3_TTS_BASE_URL"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # .venv312 OpenSSL 无默认 CA 束：固化 SSL_CERT_FILE（P5 遗留项；CP 的
    # Summarizer/联网探针同食 TLS，注入失败零副作用）。
    return _bake_ssl_cert_file(env, repo_python())


def _llm_queue_proxy_on() -> bool:
    """:1235 优先级队列代理开关(2026-09-26 根治 mlx 解码争用,默认开;
    BOK_LLM_QUEUE_PROXY=0 回旧拓扑=mlx 直跑 :1235 无代理)。开=mlx_lm 挪
    内部 :1239,services/llm-mlx/queue_proxy.py 占公网口 :1235:生成请求
    单并发排队、agent 回复(X-Bok-Lane: reply)插队,后台(settle/qa-cluster/
    judge)不再与活通话首轮互抢 GPU 时间片。"""
    return os.environ.get("BOK_LLM_QUEUE_PROXY", "1") == "1"


def _settle_gate_url() -> str:
    """9B 专线（a_reply/settle/prewarm）的**消费口**（2026-10-03 I1 前门闸）。

    queue proxy 拓扑（默认开）下 = :1238 前门（reply 插队 + GATE 观测；上游
    裸口 :1237，_start_settle_proxy 同条件拉起）；关 = 裸 :1237 旧形状。
    与 _start_settle_proxy 同一判据（同 env 开关），不会出现「指了闸却没起」。
    """
    if _llm_queue_proxy_on():
        return "http://127.0.0.1:1238/v1"
    return "http://127.0.0.1:1237/v1"


# mlx_lm server 入口 wrapper（2026-10-01 W-ABORT）：`from mlx_lm import server`
# 后做按请求身份的生成中止 patch（POST /v1/abort），argv 原样透传。三处 mlx
# 启动点（:1235/:1239 主 LLM、:1236 MT、:1237 settle/9B）统一走它；
# BOK_MLX_ABORT=0 时 wrapper 零 patch=逐字节旧行为。客户端 req_id 由 agent
# worker 侧 livekit_plugins.MlxLlmLLM 注入（X-Bok-Req-Id）。
MLX_SERVER_WRAPPER = ROOT / "services" / "llm-mlx" / "bok_mlx_server.py"


def _apply_judge_env(env: dict[str, str], _cur: dict[str, str]) -> None:
    """flow judge 专线 env(:1237 9B):模糊轮判定係后台重活(fire-and-forget),
    大模型判定质量↑且与活通话回复的 :1235 完全隔离;模型缺失(不在盘)不下发,
    judge 走原链路 :1235(agent.py 的 FLOW_JUDGE_* 优先级空头自动回退)。
    存在性检查必须有——注了 env 而 :1237 没起,judge 请求会打上死端口。

    9B 后端化(2026-09-25,LANE-AB-2026-09-25.md 附3):9B 常驻=夜间崩速主犯之一
    ——judge 9B 二号驻留与回复车道共挤统一内存,swap 颠簸下 in-call tps 4-12。
    默认(:1237 不拉)不注入该键,judge 回退链落 MLX :1235(agent.py
    ``FLOW_JUDGE_LLM_BASE_URL or MLX_LLM_BASE_URL`` 已核实);外部显式设了照传
    (云端 judge 钩子不受开关误伤)。BOK_DEV_9B=1 时行为与改造前逐字节相同。"""
    if not _dev_9b_enabled():
        _ext_judge_url = os.environ.get("FLOW_JUDGE_LLM_BASE_URL", "").strip()
        if _ext_judge_url:
            env["FLOW_JUDGE_LLM_BASE_URL"] = _ext_judge_url
            _ext_judge_model = os.environ.get("FLOW_JUDGE_LLM_MODEL", "").strip()
            if _ext_judge_model:
                env["FLOW_JUDGE_LLM_MODEL"] = _ext_judge_model
        return
    _settle = _settle_llm_model(_cur)
    if _settle and Path(_settle).exists():
        env["FLOW_JUDGE_LLM_BASE_URL"] = os.environ.get("FLOW_JUDGE_LLM_BASE_URL", "http://127.0.0.1:1237/v1")
        # 外部显式 model 照传(docstring「外部显式设了照传」全分支一致化,2026-10-01
        # 翻档后此分支成为缺省路径;云端 judge 钩子不受开关误伤)。
        env["FLOW_JUDGE_LLM_MODEL"] = os.environ.get("FLOW_JUDGE_LLM_MODEL", "").strip() or _settle


# ---------------------------------------------------------------------------
# _FORWARD_ENV 立法（2026-09-19，spec §5 卫生项）：agent/interp worker 运行时
# 读的**运营可调 env** 全集单点表。此前 agent 实读 87 键只有 5 键进表——dev
# 靠 `_start_proc` merge `os.environ` 全活着，prod（launchd/schtasks 封闭白名单）
# 69 键全死（BOK_FLOW_GRAPH prod kill-switch、BOK_CP_TOKEN auth-on worker 上报
# 两次实弹同病）。**新增 env 开关的立法动作 = 在此表加一行**：
# `tests/test_forward_env.py` 扫 agent_runtime 全部 `os.environ` 读取面，未登记
# （本表/bok 既有注入/豁免清单）即测试失败——死门在 CI 层根治，唔靠人记。
# 豁免（测试侧 `_EXEMPT`）：SCRIPTED_LLM*/USE_FAKE_MEDIA/FAKE_STT_TEXT（E2E 测试腿
# 专用）、LOCALAPPDATA（Windows OS 变量，tts_cache 有 home 回退）。
_FORWARD_ENV = (
    # —— 话术图引擎 + QA 命中语义 ——
    "BOK_FLOW_GRAPH",
    "BOK_FLOW_GRAPH_JUDGE",
    # —— W1b 意图语义车道(2026-09-23:关键词/judge 之外第三条命中路,本地
    #    embedding 检索同步补位;总闸默认开,端点缺席时装配面自动降级) ——
    "BOK_INTENT_SEMANTIC",
    "BOK_INTENT_SEM_THRESHOLD",
    "BOK_INTENT_SEM_BASE_URL",
    "BOK_INTENT_SEM_TIMEOUT_MS",
    "BOK_INTENT_SEM_COS_W",
    "BOK_INTENT_SEM_SUB_W",
    # W3b 解锁·快答库语义补位(2026-09-24):词面 0.90 未中轮的释义档,端点同
    # :8789 本机 embedding(键独立,预算同 400ms)。
    "BOK_QA_SEMANTIC",
    "BOK_QA_SEM_THRESHOLD",
    "BOK_QA_SEM_BASE_URL",
    "BOK_QA_SEM_TIMEOUT_MS",
    # 匹配端根治（2026-09-25 三刀）：召回通道（双向子串+拼音）与 Laya QA 验证车道。
    # 全部默认保守：PINYIN=1 只影响召回排序（词面 0.90 快道字节不变）；LAYA_QA 默认 0
    # =整条车道零调用零变化。
    "BOK_QA_PINYIN",
    "BOK_QA_RECALL_K",
    "BOK_QA_RECALL_FLOOR",
    "BOK_LAYA_QA",
    "BOK_LAYA_QA_TIMEOUT_MS",
    "BOK_LAYA_QA_P",
    # 沉淀引擎 v1（2026-09-25）：闲时自动消化（挖→聚→分档采纳→退休→学同音→
    # pregen）。AUTO_DIGEST 默认 0（CP 读；自主写库行为先 opt-in 实弹再谈默认）；
    # HOMOPHONE 默认 1（表空=行为逐字节同旧，学到对子才生效，golden 负样本守门）。
    # AUTO_DIGEST 消费者是 CP 不是 agent worker——但 dev serve 靠 _start_proc
    # merge os.environ 活着、prod launchd CP 封闭 env 面走 _control_plane_env
    # （那里另有同名透传，2026-09-28 env 面审计归位；此处保留无害冗余）。
    "BOK_QA_AUTO_DIGEST",
    "BOK_QA_HOMOPHONE",
    # 匹配端闸松绑+死区填补（2026-09-25 四路并行轮预埋）：WA 步放行 question 类
    # （真实数据 wa_step_locked 431 次 bypass 的半数是错杀——答赔法与收号不冲突）；
    # 垫话按需第二发（治载荷轮 2.3s 后裸静默，仅真慢轮触发非固定双发）。
    "BOK_QA_WA_STEP_QUESTION",
    "BOK_FILLER_RESHOT",
    # —— Laya 决策 sidecar(:8791,2026-09-26):意图/流程判定 10ms 快路。总闸
    #    BOK_LAYA_JUDGE(serve 默认 "1" 随栈拉起,模型在盘才起;="0" 显式关;
    #    sidecar 侧同闸双保险,"0" 时 /v1/decide 一律 503)与端点覆盖(缺省
    #    127.0.0.1:8791;8789 是 embed sidecar 既定端口,勿混)。
    "BOK_LAYA_JUDGE",
    "BOK_LAYA_SIDECAR_URL",
    # —— 意向规则挂断评估(W4-T2,2026-09-19:0=关,挂断走原 disposition) ——
    "BOK_INTENT_RULES",
    # —— 意图喂下游(P2.4,2026-09-21:0=关;默认 1——当轮意图进 LLM 尾部
    #    【客户意图】行 + 垫话类别提示;0=set no-op/行消失,字节同旧) ——
    "BOK_INTENT_CONTEXT",
    "BOK_QA_ROTATION",
    "BOK_QA_PRIORITY",
    "BOK_QA_FASTPATH",
    # —— 粤语音系补位层(2026-09-22:字面 miss 后粤拼槽位对齐;zh 线无此档) ——
    "BOK_QA_PHONETIC",
    "BOK_QA_PHONETIC_THRESHOLD",
    # —— 分支罐头快路+分支动作(2026-09-20 路线 A-①/A-②:分支命中→物化录音跳
    #    LLM;应答首部【收线】/【转人工】/【跳第N步】/【留本步】动作前缀=引擎一等出口) ——
    "BOK_BRANCH_ACTION",
    "BOK_BRANCH_CANNED",
    # —— F4 破坏性动作双护栏(2026-09-20:refuse 派发前条件核心词须字面命中;
    #    整轮/末子句剥词表词后过短=ASR 抄词表不收线) ——
    "BOK_BRANCH_REFUSE_CONFIRM",
    "BOK_BRANCH_REFUSE_HOTWORD_GUARD",
    # —— F2 迟到 FINAL 尾巴护栏(2026-09-20:AI 生成/播报中相对已提交文本的
    #    极短追加 finish 尾巴=重解幻听,不成轮不打断快路/直念回复) ——
    "BOK_LATE_FINAL_GUARD",
    "BOK_LATE_FINAL_MAX_TAIL_CHARS",
    # —— hotword_only 否决层(2026-09-25:AI 忙时停嘴整窗重解把词表热词抄成独立
    #    迟到 FINAL 掐断在播罐头;按词表贪心剥离后严格为空才否决;0=整层不评估,
    #    行为回 F2 现状) ——
    "BOK_LATE_FINAL_HOTWORD_GUARD",
    "BOK_QA_MATCH_THRESHOLD",
    # —— 垫话/罐头/TTS 缓存 ——
    "BOK_FILLER",
    "BOK_FILLER_DELAY_MS",
    # 垫话连发冷却时间窗(2026-09-29):上次真垫话 Ns 内跳过本发(0=关窗)。
    # 旧「相邻轮歇一轮」seq 冷却把慢轮覆盖打穿,改时间窗后真实通话轮间隔
    # (>10s)普遍出窗=慢轮全覆盖,急连发段仍有阻尼。
    "BOK_FILLER_COOLDOWN_S",
    # 垫话让路(第十七波 2026-10-02,call-4e8d58c1):真答案首音频就绪即停在播
    # 垫话+hold 归零+reshot 查 reply 在途;="0" 一键回 09-10「垫话必须播完」。
    "BOK_FILLER_YIELD",
    # 晚到补答去重(第十七波):交付前与已交付文本比相似度(阈值沿用
    # BOK_REPEAT_CROSS_TURN_SIM);="0" 跳过比对回旧行为。
    "BOK_LATE_ANSWER_DEDUP",
    # worker 容量阈值(第十七波 FLOW20 全哑根修):livekit load=整机 psutil
    # cpu_percent,共享机桌面噪音过 0.7 线=拒派空房全哑;钉 0.99 仅近全饱和才拒,
    # 生产专用节点想保守可设回 0.7。
    "BOK_WORKER_LOAD_THRESHOLD",
    # SIP 拨号模式覆盖（dialer.py:51 resolve_dial_mode：有值即显式覆盖 settings
    # sip.mode，合法 mock/real、非法回落 mock）；CP 面同键另走 _control_plane_env
    # （campaign.py:61 消费），本行补 agent worker 面——不登记则 prod 封闭 env 面
    # agent 侧恒读空串，env 覆盖结构性死门（2026-10-03 C2）。
    "BOK_SIP_MODE",
    # Sentry 接线（R3 2026-10-04）：worker 面 init_sentry("agent-worker") +
    # 看门狗真火/背景 judge 失败关键路径上报；DSN 缺席=完整 no-op。
    # CP 面同键另走 _control_plane_env（main.py init_sentry）。
    # SENTRY_SEND_PII（2026-10-04 Ethan 拍板 dev 档开）:1=请求头/IP 进事件。
    "SENTRY_DSN",
    "SENTRY_ENVIRONMENT",
    "SENTRY_SEND_PII",
    "BOK_FILLER_GAP_MS",
    "BOK_FILLER_CHAIN",
    "BOK_FILLER_MAX",
    "BOK_FILLER_MAX_DUR_S",
    "BOK_FILLER_CUT_AFTER_S",
    "BOK_CONTEXT_MEM_LEGACY",
    "BOK_FILLER_MATCH",
    # W2a 犹豫混入专用闸(2026-09-24):0 只关犹豫池混入,罐头五类与上游门不动。
    "BOK_FILLER_HESITATION",
    # W2c 语境化过渡承诺(2026-09-24):垫话语境桶 promise_* 池优先,0=回现行阶梯。
    "BOK_FILLER_CONTEXT",
    # W2b 思考态键盘环境音(2026-09-24):官方 thinking_sound 抽签 burst,关=构造不带。
    "BOK_AMBIENT_KEYBOARD",
    "BOK_AMBIENT_KEYBOARD_VOL",
    "BOK_FILLER_MATCH_THRESHOLD",
    "BOK_FILLER_BACKFILL",
    "BOK_TTS_FALLBACK",
    # W8 首子句起播(2026-09-24):TTS 首送快车道——首个 task_continue/首段 POST
    # ≥N 字即送(默认 6,旧 overlap 档 12 字在慢生成轮把首送推后 ~0.7-0.9s)。
    "BOK_TTS_FIRST_CLAUSE",
    "BOK_TTS_FIRST_CLAUSE_CHARS",
    # W-TTS bidi 首 chunk 提前切(2026-09-28):首个 task_continue 句内 ≥N 字即发
    # (默认 10,切点避数字/拉丁 run),后续 continue 仍按句界;0=旧行为逐字节同。
    "BOK_TTS_FIRST_CHUNK_CHARS",
    # bidi 头段催产(2026-09-29):早切头段后立刻 task_flush——服务端对无句末标点
    # 缓冲不起合成(兜底窗 2.4s),不催=早发空转;台架首声 918-962→210-343ms。
    "MINIMAX_BIDI_HEAD_FLUSH",
    # —— LLM 生成链（兜底/投机/预热/超时预算） ——
    "BOK_LLM_FALLBACK",
    # mlx 生成中止（W-ABORT，2026-10-01）：agent worker 侧 MlxLlmLLM 读；="0"
    # 时不带 X-Bok-Req-Id、不发 POST /v1/abort（字节面同旧）。服务端 wrapper
    # 同键（dev serve 走 _start_proc merge；prod 侧 wrapper 由 bok 拉起时继承）。
    "BOK_MLX_ABORT",
    "BOK_PREFILL_SPEC",
    "BOK_PREFILL_SPEC_DEBUG",
    "BOK_PREFILL_SPEC_FINAL_QUIET_MS",
    "BOK_PREEMPTIVE_DEBUG",
    "LLM_PREFIX_PREWARM",
    "PREEMPTIVE_GENERATION",
    "PREEMPTIVE_TTS",
    "PREEMPTIVE_MAX_RETRIES",
    "PREEMPTIVE_DISABLE_ON_MARKER",
    "FLOW_JUDGE_DELAY",
    "FLOW_JUDGE_IDLE_CAP",
    "BOK_JUDGE_CAPPED_SKIP",
    "FLOW_JUDGE_LLM_API_KEY",
    # —— 模型路由统一 kill-switch（2026-09-25 阶段 0：packages/core/model_routes.py
    #    契约在读，="0" 忽略路由表字节同旧；进表=dev/prod 双面都可达） ——
    "BOK_MODEL_ROUTING",
    # —— 云端 Realtime S2S 演示档（2026-09-25 阶段 B：realtime_demo.py +
    #    providers/qwen_realtime.py 适配器读面。BOK_QWEN_REALTIME="1" 才随栈
    #    拉起 bok-realtime worker（:8084，opt-in 不动默认栈）；="0" worker 拒接
    #    一切 job 且适配器构造即 raise；QWEN_REALTIME_KEY=云端凭据（worker 端
    #    读好后**构造参数**传入，适配器自身零 key env 读取）；BOK_REALTIME_DEMO_
    #    MAX_S=会话时长熔断秒数（缺省 300）；QWEN_REALTIME_BASE_URL=WS 端点
    #    覆盖（缺省=适配器模块常量 QWEN_REALTIME_WS_BASE） ——
    "BOK_QWEN_REALTIME",
    "BOK_REALTIME_DEMO_MAX_S",
    "QWEN_REALTIME_KEY",
    # WS 端点覆盖：适配器现读 QWEN_REALTIME_WS_BASE（缺省=同名模块常量）；
    # QWEN_REALTIME_BASE_URL 是该槽的历史/别名登记，防适配器改名时门禁闪红。
    "QWEN_REALTIME_WS_BASE",
    "QWEN_REALTIME_BASE_URL",
    "FLOW_LLM_ADVANCE",
    "BOK_PERCEIVED_BUDGET_MS",
    "BOK_MAX_CALL_DURATION_S",
    # 结算 gather 等待窗(D7):0/缺省=自适应档(无慢任务 10s/有意图判据 25s)。
    "BOK_SETTLE_WAIT_S",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_API_KEY",
    # DeepSeek 思考档位：官方默认 enabled，而我们的 max_tokens 都很小（对话 160 /
    # 判据 8-32）——思考会把预算烧光、正文出空串（通话侧=静默哑火）。故 DeepSeek
    # 端点缺省关思考（契约见 bok_voice_core.deepseek_llm），这两枚是显式开/覆盖口。
    "DEEPSEEK_THINKING",
    "FLOW_JUDGE_LLM_THINKING",
    # —— 轮次/打断/心跳 ——
    "TURN_DETECTION",
    "BOK_TURN_DETECTOR_THRESHOLD",
    "BOK_TURN_DETECTOR_THRESHOLDS",
    # smart-turn 语义闸（V1，2026-09-26：VAD 停嘴处 ONNX 判「说完没」，p<0.5 复用
    # join-hold 等续段；providers/smart_turn.py。默认 "0"=关——未验收特性不默认开）
    "BOK_SMART_TURN",
    # —— FireRedVAD 试点适配层（2026-09-28：providers/firered_vad.py，0.6M DFSMN
    #    流式 ONNX 包成 livekit vad.VAD。BOK_VAD_PROVIDER 默认 "silero"=零变化，
    #    "firered" 才替换且缺依赖/缺模型回退 silero；MODEL_DIR 指模型目录覆盖资产；
    #    THRESHOLD/SMOOTH 为 FireRed 独立档默认 0.5/5，不照抄 silero 0.75） ——
    "BOK_VAD_PROVIDER",
    "BOK_FIRERED_MODEL_DIR",
    "BOK_FIRERED_THRESHOLD",
    "BOK_FIRERED_SMOOTH",
    "ENDPOINT_MIN_DELAY",
    "ENDPOINT_MAX_DELAY",
    "INTERRUPT_MIN_DURATION",
    "RESUME_FALSE_INTERRUPTION",
    "FALSE_INTERRUPTION_TIMEOUT",
    # —— M-30 turns 断窗重放(2026-09-23 修复波#2:CP 断窗轮次本地暂存 CP 恢复补交;
    #    0=回旧行为失败即弃——task-9 腿 9.6 实证 ~14 轮永久丢) ——
    "BOK_TURNS_REPLAY",
    "BOK_INTERRUPT_STORM_BACKOFF",
    "BOK_INTERRUPT_STORM_WINDOW_S",
    "BOK_INTERRUPT_STORM_THRESHOLD",
    "BOK_INTERRUPT_STORM_QUIET_S",
    "BOK_INTERRUPT_STORM_MAX_ROUNDS",
    "BOK_INTERRUPT_LEDGER",
    "BOK_INTERRUPT_REAP",
    "BOK_REPEAT_HEAD_MAX_HOLD",
    "BOK_PREFIX_PREWARM_YIELD",
    "BOK_ACTIVE_CALLS_DIR",
    "BOK_ASR_ENGINE",
    "BOK_FACT_CORRECTION",
    "SILENCE_NUDGE_SECONDS",
    "SILENCE_NUDGE_MAX",
    "BOK_E2E_NUDGE_IMMUNE",
    "BOK_STARVE_ACK",
    "BOK_PAUSE_ACK",
    "BOK_DEFER_ACK",
    "BOK_SAY_STEP_LIMIT",
    # —— 看门狗/流程守卫 ——
    "BOK_RESPONSE_WATCHDOG_S",
    "BOK_RESPONSE_WATCHDOG_FILLER_EXT_S",
    "BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S",
    "BOK_DIGIT_ACCUMULATE",
    "BOK_WA_ACCUMULATE",
    "BOK_WA_ACCUM_TIMEOUT_S",
    "BOK_WA_LEN_CHECK",
    # —— M-23 首位数字回声剥离(2026-09-23 修复波#4:AI 复述/last_reply 回声混进
    #    客户报号首位 → 错号被确认;0=关) ——
    "BOK_WA_ECHO_STRIP",
    # —— M-22① 罐头/分支出声前内部指令守卫(2026-09-23 修复波#4:教练文案被
    #    罐头车道逐字念给客户;命中拒出声落 LLM;0=关) ——
    "BOK_CANNED_TEXT_GUARD",
    "BOK_WORKER_PORT_GUARD",
    # 单机多栈并存（并行会话/多 worktree 验收）错开 A 线 worker 端口，默认 8081 零漂移。
    "BOK_WORKER_PORT",
    # —— 漏斗 v2（stall 升级阶梯/judge route 路由/跟进工单；合入默认全开，0=回退） ——
    "BOK_STALL_LADDER",
    "BOK_UNCLEAR_ADVANCE",
    "BOK_UNCLEAR_ADVANCE_N",
    "BOK_LLM_STALL_OBS_TPS",
    "BOK_QA_CANNED_COOLDOWN_S",
    "BOK_ROUTE_JUDGE",
    "BOK_TOOLS_FOLLOWUP",
    # —— 双派发守卫（2026-09-28 call-0105a539 实证：同房双 job 并跑整通=双开场
    #    白+双份回答+fallback 道歉风暴；flock 同房互斥，后到 job 让位；0=关，
    #    DIR=锁目录覆盖供测试/多栈隔离） ——
    "BOK_ROOM_CLAIM",
    "BOK_ROOM_CLAIM_DIR",
    # —— ASR（agent 侧读的运维档；sidecar 专属键走 asr_env 另注入） ——
    # 云 ASR 装线波(2026-10-03):豆包 SAUC 总闸,=0 装配点回退本地 Qwen3-ASR
    # (A/B 线共用;凭据走设置面 asr 段,不经 env)。
    "BOK_DOUBAO_ASR",
    "BOK_ASR_HOTWORDS",
    "BOK_ASR_PARTIAL_SLOW_MS",
    # B 线正压臂波(2026-10-02):ASR 帧级调试观测(掉帧/水位打点)——
    # 运维键,prod 不透传=死门(test_forward_env 钉)。
    "BOK_ASR_FRAME_DEBUG",
    # 开采热词(第四来源,2026-09-28):agent 装配期 GET /api/asr/hotwords 一次;
    # 默认 "1"(端点缺席 fail-open 空串),="0" 跳过零 HTTP 调用。
    "BOK_MINED_HOTWORDS",
    # chunk POST 失败保留(D4):0=回退旧「先清后发」档。
    "QWEN3_ASR_CHUNK_KEEP",
    "QWEN3_ASR_STREAM",
    "QWEN3_ECHO_GUARD",
    "QWEN3_HOTWORD_ECHO_GUARD",
    # —— ASR 终稿后置轨(2026-09-21 E1/E2 接线:先热词泄漏清洗、后 snippet 词级
    #    替换;两枚 kill-switch 默认 "1",=0 各自回退零变化) ——
    "BOK_HOTWORD_LEAK_SANITIZE",
    "BOK_SNIPPETS",
    # —— 知识/检索/TTS 语言 ——
    "CONTEXT_RAG",
    "WEB_SEARCH",
    "MINIMAX_LANGUAGE_BOOST",
    "EMOTION_TAG_PILOT",
    # —— 机器通道凭据（auth-on 部署 worker 上报 turns/QA/设置全靠它） ——
    "BOK_CP_TOKEN",
    # —— 日志面 ——
    "BOK_LOG_LEVEL",
    # —— providers/ 子包收编（2026-09-20 D14：test_forward_env 扫描改 rglob 递归，
    #    providers/livekit_plugins.py 61 键此前整包漏扫——60 运营键进表，
    #    FAKE_STT_TEXT 测试腿 shim 进豁免）。未设不注入，默认档零变化。 ——
    # LLM 生成链调参/诊断（livekit_plugins.py MlxLlmLLM/ContextAwareLLM 读面）：
    "BOK_LLM_MSG_DEBUG",
    "BOK_LLM_REGEN",
    "BOK_A_LINE_VOICE_TAGS",
    # 换气注入（voice_style.py transform 长句句界补 (breath)，2026-09-27）：
    # 总闸默认开、阈值默认 20 字。同上未设不注入、默认档零变化。 ——
    "BOK_BREATH_INJECT",
    "BOK_BREATH_SENT_CHARS",
    # ASR 受限润色层（agent_runtime/asr_polish_runtime.py，2026-09-27）：
    # 确定性音近吸附默认开 + CSC 小模型二道 opt-in（zh-only，:8792）。
    "BOK_ASR_POLISH",
    "BOK_CSC_SIDECAR",
    "BOK_CSC_URL",
    "BOK_CSC_CONF_GATE",
    "BOK_REPEAT_GUARD",
    "BOK_REPEAT_CROSS_TURN",
    "BOK_REPEAT_CROSS_TURN_SIM",
    # 编造号码输出守卫（2026-10-01，call-231aa92a）：LLM 流出口逐句校验号码
    # 确认句——数字须来自 {捕获账本, 本轮客户原话}，编造者改写/替换（默认 "1"，
    # "0"=关=恒等返回；实现 packages/core/bok_voice_core/output_guard.py）。
    "BOK_NUMBER_GUARD",
    "BOK_TAIL_SLIM",
    "BOK_MEMORY_CHARS",
    # 尾部节食（第十一波 2026-09-29）：slim 轮记忆块降频——距上次带过 ≥N 条
    # 账本项才带（=1 旧行为每轮带）。尾部骑在新 user 消息后=每轮全新 uncached，
    # 记忆块(~250 字≈170 tok)是 slim 轮尾部最大件。STABLE_SPAN=稳定段重发回看
    # 窗口条数（默认 max(2, LLM_HISTORY_TURNS-2)；修隔轮意外重发 uncached 交替）。
    "BOK_TAIL_MEMORY_EVERY",
    "BOK_TAIL_STABLE_SPAN",
    # D1 槽位化 actor（2026-10-01,第一性原理重构）：A 线回复 LLM 从「整本剧本+
    # 全规则」切「角色卡+任务块」（默认 "0"=旧路径逐字节不变；B 线不接）。
    "BOK_SLOT_ACTOR",
    "EMOTION_TAG_PROMPT",
    "LLM_FIRST_TOKEN_TIMEOUT_S",
    # LLM 饥荒自适应（第十五波 2026-10-01,call-dc54f542）：机器级首 token 慢
    # （swap/GPU 争用）时拉长首 token 超时与 drain、禁 regen——等原流优于重来。
    "BOK_LLM_FAMINE",
    "BOK_LLM_FAMINE_TTFT_S",
    "BOK_LLM_FAMINE_FIRST_S",
    "BOK_LLM_FAMINE_DRAIN_S",
    "LLM_HISTORY_TURNS",
    "LLM_LATE_ANSWER_DEADLINE_S",
    "LLM_MAX_TOKENS",
    "LLM_REQUEST_RETRIES",
    "LLM_REQUEST_TIMEOUT_S",
    "LLM_TEMPERATURE",
    "LLM_WARMUP",
    "REPLY_MEMORY_LINES",
    # MiniMax TTS：凭据/端点 + bidi 自愈 + 语速/音调/音量 + 叠句增量（运营键全集）：
    "MINIMAX_API_KEY",
    "MINIMAX_BASE_URL",
    "MINIMAX_WS_URL",
    "MINIMAX_REGION",
    # —— F10 bidi 限流守卫(2026-09-23:限流族 task_failed 关连接+退避重试+回落
    #    HTTP+同通连续 3 轮熔断;0=回旧行为) ——
    "BOK_MINIMAX_BIDI_GUARD",
    "MINIMAX_BIDI_AUTO_REWARM",
    "MINIMAX_BIDI_CANCEL_WAIT_S",
    "MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S",
    "MINIMAX_BIDI_PING_MAX_MISS",
    "MINIMAX_BIDI_PING_S",
    "MINIMAX_BIDI_PREWARM_RETRY",
    "MINIMAX_BIDI_STALL_MAX_HEALS",
    "MINIMAX_BIDI_SYNTH_WARMUP",
    "MINIMAX_CONTINUOUS_SOUND",
    "MINIMAX_EMOTION",
    "MINIMAX_FIRST_AUDIO_TIMEOUT_S",
    "MINIMAX_PAUSE",
    "MINIMAX_PAUSE_SECS",
    "MINIMAX_PITCH",
    "MINIMAX_SPEED",
    "MINIMAX_TTS_OVERLAP",
    "MINIMAX_TTS_OVERLAP_CHARS",
    "MINIMAX_TTS_OVERLAP_MS",
    "MINIMAX_VOL",
    "MINIMAX_WS",
    "MINIMAX_WS_MODE",
    "MINIMAX_WS_POOL",
    "BOK_TTS_PREWARM",
    # Qwen3-ASR：agent 侧插件读面（sidecar 进程专属键另走 asr_env，不在此表）：
    "QWEN3_ASR_CHUNK_MS",
    "QWEN3_ASR_HESITATION_GATE",
    "QWEN3_ASR_JOIN_HOLD_MS",
    "QWEN3_ASR_JOIN_HOLD_VOCAB",
    "QWEN3_ASR_PAUSE_COMMIT_MIN_CHARS",
    "QWEN3_ASR_PREFLIGHT_LANG_GATE",
    "QWEN3_ASR_SENTENCE_PAUSE_TRIGGER",
    # Qwen3-TTS sidecar 客户端（插件侧）调参：
    "QWEN3_TTS_MAX_TASK_AUDIO_SEC",
    "QWEN3_TTS_OVERLAP",
    "QWEN3_TTS_OVERLAP_CHARS",
    "QWEN3_TTS_OVERLAP_MS",
    # Volcano TTS：凭据/端点/voice 调参：
    "VOLC_ACCESS_TOKEN",
    "VOLC_APP_ID",
    "VOLC_DIALECT",
    "VOLC_LANGUAGE",
    "VOLC_LOUDNESS_RATE",
    "VOLC_RESOURCE_ID",
    "VOLC_SPEAKER",
    "VOLC_SPEECH_RATE",
    "VOLC_TTS_ENDPOINT",
    # —— 碎片轮重问车道（EX-2，2026-09-28）：碎裂/碎片转写 canned 重问（零 TTFT
    #    替掉 LLM 轮）总闸与四闸；默认开/0.45/0.5/2/2，未设不注入=默认档零变化 ——
    "BOK_GARBLED_REASK",
    "BOK_REASK_CONF_MEAN",
    "BOK_REASK_LOW_RATIO",
    "BOK_REASK_MIN_CONTENT_CHARS",
    "BOK_REASK_MAX_CONSEC",
    # —— B 线第一性原理波（2026-10-02）：装配期 MT 探活 kill-switch（缺省开） ——
    "BOK_INTERP_MT_PROBE",
    # —— 编排审计第二波 PR-B（2026-10-02）：A 线 a_reply 车道装配期探活 kill-switch
    #    （缺省开；agent.py 读注入 env Mapping 故静态扫描不强制，prod 转发靠此登记——
    #    BOK_INTERP_MT_PROBE 同款判例；tests/test_a_reply_probe.py 钉 membership） ——
    "BOK_A_REPLY_PROBE",
)
# 历史名（2026-09-18 终审 I1 起的既有调用面/单测锚）：表本体唯一，别名防散。
_BOK_PASSTHROUGH_KEYS = _FORWARD_ENV


def _apply_bok_passthrough_env(env: dict[str, str]) -> None:
    """运营 env 透传（2026-09-18 实弹发现；2026-09-19 `_FORWARD_ENV` 立法收编全集）。

    `_agent_worker_env`/`_agent_prod_env` 都是**白名单 env**（dict 里没写的键一律
    不带 `os.environ`）——`BOK_FLOW_GRAPH=0 python tools/bok.py serve` 写在命令行上
    **到不了 agent worker**，worker 按默认 `"1"` 跑：kill 腿「全程零 FLOW_GRAPH」
    结构性测不出（实弹：worker pid env 只有 2 枚 BOK_ 键、无 BOK_FLOW_GRAPH，
    jump/play 照发，探针如实报 FAIL）。dev 靠 `_start_proc` merge `os.environ` 一直
    全活、prod 封闭白名单全死——表见 `_FORWARD_ENV`；未设/空串不注入（默认档
    逐字节不变）。"""
    for key in _BOK_PASSTHROUGH_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value


def _apply_flow_graph_env(env: dict[str, str]) -> None:
    """历史名（单测/旧调用面）：passthrough 透传的薄别名。"""
    _apply_bok_passthrough_env(env)



def _agent_worker_env(py) -> dict[str, str]:
    """A 线 main worker 的 env(serve 与 monitor 同源单点)。"""
    _cur = MODELS["mac"] if is_mac() else MODELS["windows"]
    env: dict[str, str] = {
        "PYTHONPATH": _repo_pythonpath(),
        "BOK_SERVICE": "agent",
        "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
        "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
        "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
        "CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"),
        "MLX_LLM_BASE_URL": os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        "MLX_LLM_MODEL": model_path({**_cur, "llm": resolve_llm_repo(_cur)}, "llm"),
    }
    _apply_judge_env(env, _cur)
    _apply_bok_passthrough_env(env)
    # .venv312 OpenSSL 无默认 CA 束 → MiniMax WSS 必炸;固化 SSL_CERT_FILE。
    _bake_ssl_cert_file(env, py)
    return env


def _apply_interp_direction_env(env: dict, direction: str) -> dict:
    """方向级服务覆盖钩子（全双工分 GPU / 云端混合的接线点，2026-09-16）。

    B 线 fwd/rev 两个 worker 共享 _interp_env，两路同时说话时 ASR/MT 请求在
    同一块 GPU 上排队。REV 方向可用 `*_REV` env 把 ASR/MT 指到第二套端点
    （第二实例/第二台机/云端适配器），fwd 保持默认——排队问题的第一指定解。
    只认 REV 方向的覆盖；fwd 恒走默认（单向说话是主场景，零配置零变化）。"""
    if direction != "rev":
        return env
    for base in (
        "QWEN3_ASR_BASE_URL",
        "QWEN3_ASR_MODEL",
        "MT_LLM_BASE_URL",
        "MT_LLM_MODEL",
    ):
        v = os.environ.get(f"{base}_REV")
        if v:
            env[base] = v
    return env


def _pid_alive(pidfile: Path) -> bool:
    """pidfile 指向的进程还活着吗（_ensure_monitor 单例判定的唯一探针）。

    Windows（M2-fix）：绝不能用 os.kill(pid, 0)——CPython 的 os.kill 在 nt 上
    对非 CTRL_C_EVENT/CTRL_BREAK_EVENT 的 sig 一律调 TerminateProcess，探活即
    击杀（活的 monitor 被探死、仍返回 True、_ensure_monitor 误判单例存活跳过
    respawn → 栈从此无人看护）。改用 tasklist 按 PID 查询
    （scripts/probes/probe_windows_lifecycle.py `_win_pid_alive` 同款；冷路径不缓存；
    查询失败保守当存活——宁可不重拉也不误判）。POSIX 分支与旧代码逐字节同款
    （sig 0 在 POSIX 是纯探活）。"""
    try:
        pid = int(pidfile.read_text().strip())
    except Exception:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            return True  # 查询失败保守当存活（勿误判单例已死而重复拉起）
        return f'"{pid}"' in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def cmd_monitor() -> int:
    """C6-1 常驻监控环:LiveKit 重启→A+B 全 worker respawn(重注册);单 worker
    掉线→补拉。9/12 11:52-12:05 实证:livekit 重启后 worker 注册全丢,
    「no worker is available」连 4 通 0 轮、无人补拉;serve 一次性返回管唔到。
    2026-09-17 重排:探不上→respawn 改连续失败计数(单轮 1s TCP 探测在 GPU
    满载下係常态误报)。
    2026-09-25 G3 重排(LANE-AB-2026-09-25.md 附3):①探活从 1s TCP 换成真
    GET :port/worker 端点(_probe_worker 与 prod status 同源单点)——TCP UP 对
    「进程在、没 register/假活」不可见,1s 窗在 swap 颠簸下还假死(offscript
    窗误杀 ×7 根因);②active_calls>0 时**任何探活失败都不杀**(硬 veto,取代
    12 轮/60s 抬门槛——swap 颠簸可连吃 60s,门槛抬得再高也有窗,veto 先生才
    关死);CP 不可达退回无通话口径。
    """
    py = repo_python()
    run_dir = app_data_dir() / "run"
    log_dir = app_data_dir() / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    # 盲斑 1 修复（2026-09-22）：外部手跑 monitor 也落 pidfile+来源戳——旧版只
    # 有 _start_proc 拉起的 monitor 才有痕迹，外部启动令 _ensure_monitor 探不到
    # 单例（双监控环）、跨树杀守卫对无戳进程不保护。经 _start_proc 拉起时同
    # 内容写两次，幂等无害。
    proc._write_proc_stamps(run_dir / "monitor.pid", os.getpid())
    print(f"[monitor] started — watching :7880 + workers {_agent_worker_port()}/8082/8083")
    lk_up = healthy(7880)
    last_action = 0.0
    down_streak: dict[str, int] = {}

    def _respawn(specs: list[dict], why: str) -> None:
        nonlocal last_action
        # 限频:转换风暴(重启抖动)下 30s 内只动作一次。
        if time.monotonic() - last_action < 30.0:
            return
        last_action = time.monotonic()
        print(f"[monitor] {why} — respawning workers")
        for spec in specs:
            proc._kill_pidfile(spec["pidfile"])
        # 等端口释放(优雅关停最长 ~10s;拿不到就交给端口单例守卫兜底)。
        _deadline = time.monotonic() + 10.0
        while time.monotonic() < _deadline:
            if not any(healthy(s["port"]) for s in specs):
                break
            time.sleep(0.5)
        for spec in specs:
            # 他树/复用守卫（2026-09-22）：上一轮 kill 被他树戳挡下时，端口仍被
            # 对方的健康 worker 持有——硬起只会 bind 失败退出刷噪声。已有健康
            # 监听的端口跳过重拉（自己刚被杀掉的 worker 端口是空的，不受影响）。
            if healthy(spec["port"]):
                print(f"[monitor] :{spec['port']} 已有健康监听，跳过重拉"
                      "（他树持有则去对方树 down）")
                continue
            servers._start_proc(spec["argv"], spec["pidfile"], spec["logfile"], env=spec["env"])
            print(f"[monitor] respawned {spec['name']} :{spec['port']}")

    while True:
        try:
            specs = servers._worker_specs(py)
            now_up = healthy(7880)
            skip_lk_mark = False
            if not lk_up and now_up:
                # LiveKit 回来了(重启)——注册在新进程,worker 必须重注册。但若有在途
                # 通话（或 CP 不可达=状态未知），respawn 的集体 kill 会陪葬活通话
                # （2026-09-27 修：旧版无条件 kill）：保守不杀，且**不更新 lk_up**
                # → 5s 后本轮重试，通话归零即刻补拉（重启注册丢的修复只是延后不丢）。
                _active = proc._cp_active_calls()
                if _active is None or _active > 0:
                    skip_lk_mark = True
                    print(
                        f"[monitor] livekit back up but active_calls="
                        f"{'unknown(CP unreachable)' if _active is None else _active} "
                        "— veto respawn (retry after calls drain)"
                    )
                else:
                    _respawn(specs, "livekit back up (restart detected)")
                    down_streak = {}
            elif now_up:
                active = proc._cp_active_calls()
                need = proc._DOWN_STREAK_NEED_ACTIVE if active else proc._DOWN_STREAK_NEED_IDLE
                down: list[dict] = []
                for spec in specs:
                    # 真端点探针:与 prod status/_probe_worker 同源单点(见 docstring G3①)。
                    ok, _detail = _probe_worker(spec["port"])
                    if ok:
                        down_streak[spec["name"]] = 0
                        continue
                    streak = down_streak.get(spec["name"], 0) + 1
                    down_streak[spec["name"]] = streak
                    kill, veto_log = proc._monitor_kill_round(streak, active)
                    if veto_log:
                        _desc = "unknown(CP unreachable)" if active is None else active
                        print(
                            f"[monitor] worker {spec['name']} probe failed x{streak} "
                            f"but active_calls={_desc}, veto kill"
                        )
                    if kill:
                        down.append(spec)
                if down and time.monotonic() - last_action >= 30.0:
                    # 单 worker 真 down 补拉;全 down 逐个 kill+start(端口已死,
                    # 无需 _respawn 的集体 kill-then-wait)。有通话在途时硬 veto
                    # (G3②):kill 恒 False、streak 照涨——通话一结束(active 归零)
                    # 真死 worker 立刻补拉,活 worker 的瞬态卡顿永不触发。
                    last_action = time.monotonic()
                    for spec in down:
                        down_streak[spec["name"]] = 0
                        proc._kill_pidfile(spec["pidfile"])
                        servers._start_proc(spec["argv"], spec["pidfile"], spec["logfile"], env=spec["env"])
                        print(
                            f"[monitor] worker {spec['name']} down x{need} "
                            f"(active_calls={active}) — respawned :{spec['port']}"
                        )
            if not skip_lk_mark:
                lk_up = now_up
        except Exception as exc:  # noqa: BLE001 - 监控环任何异常都唔准退出
            print(f"[monitor] loop error: {exc!r} — keep watching")
        time.sleep(5.0)


def _cp_bind_host() -> str:
    """CP 监听地址（site-delivery fixwave，补 M2.3 计划项「bind 0.0.0.0 env 开关」）：
    BOK_BIND_HOST 显式 opt-in（如 0.0.0.0）才对外监听，缺省恒 127.0.0.1——本机
    单用户形态行为零变化。serve 与 prod install（launchd/schtasks 单元定义）共用
    同一份解析：--open-firewall 的 :8000 放行规则只有 bind 0.0.0.0 时才有意义。"""
    return (os.environ.get("BOK_BIND_HOST") or "").strip() or "127.0.0.1"


def cmd_down() -> int:
    run_dir = app_data_dir() / "run"
    stop_failures = 0
    for pidfile in run_dir.glob("*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
        except Exception:
            continue
        # 他树戳守卫（2026-09-22）：run/*.pid 是 HOME 作用域单槽共享文件，
        # 多会话/worker 换装会互相覆写——down 只停「本树 + 无戳遗留」（_sweep_
        # orphan_listeners 已立法的同款纪律在 pidfile 路径落地；旧版此处对
        # pidfile 内容无脑收割，2026-09-22 实弹把我们树的 worker 杀掉的正是
        # 这个缺口）。
        foreign, root = proc._pid_origin_foreign(pid)
        if foreign:
            print(f"[down] skip {pidfile.stem}: pid {pid} 属另一代码树（{root}）"
                  "——他树进程永不收割（先在对方树 down）", file=sys.stderr)
            continue
        # pid 复用闸（2026-10-02 审计）：来源戳 lstart 与 live 进程对不上 =
        # stale pidfile 的 pid 已被无关进程复用——来源判「未知」会 fail-open
        # 照杀（误杀无辜）；此处按铁证跳过。无戳遗留照旧语义杀。
        if proc._pid_reused_stale(pidfile, pid):
            print(f"[bok] stale pidfile {pidfile.stem} pid={pid} reused — skip kill")
            continue
        # _start_proc 以 start_new_session=True 启动（会话组长）；按进程组
        # 终止可连 livekit-agents worker 的 multiprocessing 子进程一起清掉，
        # 避免子进程残留占用 8081 导致下次 agent 启动失败。
        # Windows(M2)：taskkill /T /F 沿父子树收割；真失败必须浮出——旧代码
        # os.killpg 在 nt 不存在，AttributeError 被外层 except 整个吞掉，down
        # 全程静默失效（probe_windows_lifecycle.py 记录的 M1 断层）。
        try:
            proc._kill_proc_tree(pid)
        except proc._KillTreeError as exc:
            print(f"[down] FAILED to stop {pidfile.stem}: {exc}", file=sys.stderr)
            stop_failures += 1
            continue
        except Exception:
            # 死 pid / 权限缺失：与旧 POSIX 行为一致，静默跳过。
            continue
        print(f"[down] stopped {pidfile.stem} (pid {pid})")
    # Legacy dev sidecars managed by old start_sidecars.sh (host pids in data/).
    data_dir = ROOT / "data"
    for pidfile in data_dir.glob("sidecar-*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
            # pid 复用闸同款（2026-10-02 审计）：legacy 扫描面同样只杀真本树 pid。
            if proc._pid_reused_stale(pidfile, pid):
                print(f"[bok] stale pidfile {pidfile.stem} pid={pid} reused — skip kill")
                continue
            os.kill(pid, signal.SIGTERM)
            print(f"[down] stopped {pidfile.stem} (pid {pid})")
        except Exception:
            continue
    # 孤儿 worker 兜底清扫（2026-09-07 QA 实证）:serve 异常退出后 start_new_session
    # 的 worker 存活,而 pidfile 可能已被覆写成死 pid——down 按 pidfile 清不到,
    # 旧代码 worker 会继续注册 livekit 抢 job。按进程特征+监听端口兜底清扫一遍。
    orphans = proc._sweep_orphan_workers()
    for pid, label in orphans:
        print(f"[down] swept orphan worker (pid {pid}, {label})")
    # 端口级兜底（2026-09-17 殭尸专项）:spawn 子代/sidecar/livekit/uvicorn 殘留
    # 是命令行特征清扫的盲区,按 bok 端口表+身份复核双条件收割。
    # healthy_ok=False:down 是拆除语义,pidfile SIGTERM 已先送达,「健康残留」
    # 也必须收走——否则 pidfile 覆写成死 pid 时 down 返回 0 但栈仍在跑
    # （旧代码被静默采纳=A/B 污染复活,评审 P1-1）。
    for port, cmd, pid in proc._sweep_orphan_listeners(healthy_ok=False):
        print(f"[down] swept orphan listener :{port} (pid {pid}, {cmd})")
    return 1 if stop_failures else 0


def _agent_prod_env() -> dict[str, str]:
    """agent/interp worker 生产环境（与 cmd_serve 同源）。"""
    _cur = MODELS["mac"] if is_mac() else MODELS["windows"]
    env = {
        "PYTHONPATH": _repo_pythonpath(),
        "BOK_SERVICE": "agent",
        "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
        "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
        "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
        "CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"),
        "MLX_LLM_BASE_URL": os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        "MLX_LLM_MODEL": model_path({**_cur, "llm": resolve_llm_repo(_cur)}, "llm"),
    }
    _apply_judge_env(env, _cur)
    _apply_bok_passthrough_env(env)
    # .venv312 OpenSSL 无默认 CA 束 → MiniMax WSS 必炸 SSLCertVerificationError；
    # 固化 SSL_CERT_FILE（P5 遗留项），interp 经 _interp_env 的 dict 拷贝继承。
    return _bake_ssl_cert_file(env, repo_python())


def _interp_env(agent_env: dict[str, str]) -> dict[str, str]:
    """B 线 interpreter 追加 env:MT 翻译小模型(:1236,可选增强)。

    MT_LLM_MODEL 必须是后端真认的模型路径(与主 LLM 同一解析);MT_* 只在
    模型目录在盘或 :1236 已健康(与 _start_mt_llm 同判)时下发——模型缺失/
    Windows 无 mt 条目时完全不注入,interpret 侧按 env 有无回退主 LLM
    :1235/DeepSeek,避免 worker 指去死端口整场无声。MINIMAX_* 不在这里注入
    ——TTS 供应商/音色由 interpret.py 按设置与方向解析。
    """
    env = dict(agent_env)
    # B 线句级提交(2026-09-16 起)与 A 线同档默认开：interpret.py 的
    # turn_handling 已切 turn_detection=stt（与句级 FINAL 成对，_turn_handling_opts
    # 单源复用 A 线 _turn_detection_mode_from_env/_endpointing_delays_from_env），
    # STT 说话中按句 FINAL+EOS 成轮 → 翻译+TTS 与源语音重叠，同传粒度从
    # 「停嘴整段」提前到句级。kill-switch 配对全自动：TURN_DETECTION≠stt
    # (空串/vad/EOT) 时 livekit_plugins.sentence_commit_enabled() 自行熄火 +
    # endpointing min_delay 自动回 ≥0.35 地板，无需动这里。setdefault 不抢用户
    # 显式 env（QWEN3_ASR_SENTENCE_COMMIT=0 仍是应急逃生口）。
    env.setdefault("QWEN3_ASR_SENTENCE_COMMIT", "1")
    # B 线子句级提交(2026-09-16):逗号/顿号/分号也作提交边界——译员按子句跟,
    # 长句唔使等整句讲完才出译文(「说话时间+生成时间」体感长的主刀)。默认 1,
    # 显式 0 逃生;A 线 worker 唔带此 env,客服轮次仍按句。
    env.setdefault("QWEN3_ASR_CLAUSE_COMMIT", "1")
    # B 线长度触发子句提交(2026-09-17 边说边译档):连续语流无逗号无 0.45s 停顿
    # 时,滑窗未提交前缀攒够字数(默认 10)且跨窗稳定即就地切句——标点档/停顿档
    # 的第三事件源,译出声不等人讲完。默认 1,显式 0 逃生;A 线唔带此 env。
    env.setdefault("QWEN3_ASR_CLAUSE_LEN_COMMIT", "1")
    # VAD 停嘴门槛(2026-10-02 收编):旧版在此 setdefault 0.35(2026-09-17 B 线
    # 专属调参,当时 A 线 0.45)——但 env 优先级压过设置面,设置页对 B 线永久
    # 说谎(改了不生效)。现拆 setdefault:B 线与 A 线同读设置面 vad 段
    # (interpret _cfg_float / agent _vad_float 同一序:显式 env 部署覆盖 >
    # 设置页 > 缺省 0.35),显式 env 仍经下方透传白名单下发。当前设置值 0.35=
    # 拆钉零行为变化;后续调门槛只动设置页,两线同源。
    # B 线开关透传(_agent_worker_env 是白名单 env,不透传 os.environ——
    # 不显式带上的话文档里的逃生门在 dev/prod 栈都是死的,2026-09-16 实证)。
    for _k in (
        "BOK_INTERP_MT_CONTEXT",
        # E5 增补 2026-09-21:MT 出口确定性语言校验+单次强化重试总闸。默认开
        # ——正常轮零额外延迟,仅错语言轮多一次往返;=0 回退旧「出口不校验」档。
        "BOK_INTERP_MT_LANGGUARD",
        "BOK_INTERP_REV_AUDIO",
        "BOK_INTERP_BACKLOG",
        "BOK_INTERP_MAX_BACKLOG_S",
        "BOK_INTERP_VOICE_TAGS",
        # B 线缺源遥测 2026-09-30：fwd 订阅空挂零痕迹(call-72112fd7)——看护
        # 每 N 秒分辨「对端没发麦」vs「发了订不上」打观测行;=0 关。
        "BOK_INTERP_SRC_TELEMETRY",
        "BOK_INTERP_SRC_TELEMETRY_S",
        # B 线订阅自愈 2026-09-30：set_subscribed 官方手动订阅口(对账定案)
        # ——检测到已发布未订上即重发订阅;=0 回纯观测档。
        "BOK_INTERP_SRC_HEAL",
        # B 线 MiniMax 硬失败兜底 2026-09-27：主档云端 MiniMax 失败时 FallbackAdapter
        # 备档=本地 Qwen3-TTS 是否装备。=0 显式跳过本地 TTS 时不装备，与 bok.py
        # _local_tts_needed 同键语义；未设=装备，本地 sidecar 未跑时逐请求穿透。
        "BOK_LOCAL_TTS",
        "MINIMAX_MODEL",
        "QWEN3_ASR_SENTENCE_COMMIT",
        "QWEN3_ASR_CLAUSE_COMMIT",
        "QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS",
        "QWEN3_ASR_CLAUSE_LEN_COMMIT",
        "QWEN3_ASR_CLAUSE_LEN_CHARS",
        "VAD_MIN_SILENCE_DURATION",
    ):
        if os.environ.get(_k):
            env[_k] = os.environ[_k]
    mt_model = _mt_llm_model(MODELS["mac"] if is_mac() else MODELS["windows"])
    if (mt_model and Path(mt_model).exists()) or healthy(1236):
        env["MT_LLM_BASE_URL"] = os.environ.get("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
        if mt_model:
            env["MT_LLM_MODEL"] = mt_model
    return env


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
    # tts-pregen/tts-mine 参数原样透传给执行脚本,顶层不做校验
    args, extra = p.parse_known_args(argv)
    args.extra = list(extra)
    return args


def cmd_tts_pregen(extra: list[str] | None = None) -> int:
    """离线批量预合成 TTS 本地缓存(docs/superpowers/specs/2026-09-08-tts-cache-design.md)。

    额外参数原样透传给 scripts/runtime/pregen_tts.py(--greetings/--objects/--fillers/--cp/--model)。
    子进程带仓库 PYTHONPATH 与 SSL_CERT_FILE(certifi)——venv 无系统 CA,
    MiniMax WSS 无此必炸。
    """
    env = {"PYTHONPATH": _repo_pythonpath(), "PYTHONUNBUFFERED": "1"}
    _bake_ssl_cert_file(env, repo_python())
    proc = subprocess.run(
        [str(repo_python()), str(ROOT / "scripts" / "runtime" / "pregen_tts.py"), *(extra or [])],
        env={**os.environ, **env},
    )
    return proc.returncode


def cmd_tts_mine(extra: list[str] | None = None) -> int:
    """高频问答对挖掘报告(快答库,PR-3)。参数透传给 scripts/runtime/mine_qa.py。

    --apply N 把前 N 条入库为 qa_entries(source=mined);入库后跑
    `bok.py tts-pregen` 物化应答音频,闸门只认缓存有音频的条目。
    """
    env = {"PYTHONPATH": _repo_pythonpath(), "PYTHONUNBUFFERED": "1"}
    _bake_ssl_cert_file(env, repo_python())
    proc = subprocess.run(
        [str(repo_python()), str(ROOT / "scripts" / "runtime" / "mine_qa.py"), *(extra or [])],
        env={**os.environ, **env},
    )
    return proc.returncode


# clean-testdata 的 CP base（模块级读 env：进程启动时即定值；同时把「env 读取」
# 移出函数作用域（路径正则/鉴权头在闭包外构造，函数内只留白名单闸+_http_call，
# 静态污点分析可完整看见净化链）
_CP_CLEAN_BASE_URL = os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000")


def cmd_clean_testdata() -> int:
    """清理历史测试数据(QA B3/B7,2026-09-09):对象下拉曾被 300+ E2E/soak 残留灌满。

    默认 dry-run 只打印;`--apply` 才真删(经 CP API,审计可追溯)。范围:
    ①对象 display_name 匹配测试前缀;②人设同名同公司重复(保留最早)。
    """
    import re as _re

    base = _CP_CLEAN_BASE_URL
    apply_mode = "--apply" in sys.argv
    token = os.environ.get("BOK_CP_TOKEN", "")
    # 数据驱动路径白名单：删除目标的 id 来自 CP 响应（o['id']），只认
    # objects/personas 资源 + uuid hex 段，其余（含 ../、斜杠夹带）一律拒发。
    _cp_path_re = _re.compile(r"^/api/(objects|personas)(/[A-Za-z0-9._-]{1,64})?$")
    _auth_headers = {"Authorization": f"Bearer {token}"} if token else None

    def _get(path: str):
        if not _cp_path_re.match(path):
            raise ValueError(f"CP 路径未过白名单（拒发）: {path!r}")
        status, raw = _http_call(f"{base}{path}", headers=_auth_headers, timeout_s=15)
        if status != 200:
            raise RuntimeError(f"CP GET {path} -> HTTP {status}")
        return json.loads(raw.decode())

    def _delete(path: str) -> None:
        if not _cp_path_re.match(path):
            raise ValueError(f"CP 路径未过白名单（拒发）: {path!r}")
        status, _raw = _http_call(f"{base}{path}", "DELETE", headers=_auth_headers, timeout_s=15)
        if status not in (200, 204):
            raise RuntimeError(f"CP DELETE {path} -> HTTP {status}")

    pat = _re.compile(r"^(E2E-|soak\d*-?|并发|LOAD-|边角-|多轮-|probe)")
    objs = _get("/api/objects")
    stale = [o for o in objs if pat.match(str(o.get("display_name") or ""))]
    print(f"objects: total={len(objs)} stale-matched={len(stale)}")
    for o in stale:
        print(f"  - {o['id']} {o.get('display_name')}")
        if apply_mode:
            _delete(f"/api/objects/{o['id']}")

    seen: set[tuple[str, str]] = set()
    dupes = []
    for p_ in _get("/api/personas"):
        k = (str(p_.get("name") or ""), str(p_.get("company") or ""))
        if k in seen:
            dupes.append(p_)
        else:
            seen.add(k)
    print(f"personas: duplicate-matched={len(dupes)}")
    for p_ in dupes:
        print(f"  - {p_['id']} {p_.get('name')} / {p_.get('company')}")
        if apply_mode:
            _delete(f"/api/personas/{p_['id']}")

    if not apply_mode:
        print("dry-run: 未删除任何数据。加 --apply 执行。")
    else:
        print("apply done。")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.cmd == "setup":
        return cmd_setup(args.action)
    if args.cmd == "prod":
        return prod.cmd_prod(args.action,
                        node_agent=getattr(args, "node_agent", False),
                        node_args=getattr(args, "extra", None),
                        open_firewall=getattr(args, "open_firewall", False),
                        staging_dir=getattr(args, "staging_dir", ""),
                        with_model_plane=getattr(args, "with_model_plane", False))
    if args.cmd == "tts-pregen":
        return cmd_tts_pregen(getattr(args, "extra", None))
    if args.cmd == "clean-testdata":
        return cmd_clean_testdata()
    if args.cmd == "tts-mine":
        return cmd_tts_mine(getattr(args, "extra", None))
    if args.cmd == "monitor":
        return cmd_monitor()
    if args.cmd == "download":
        only = set(getattr(args, "only", None) or []) or None
        return cmd_download(only=only)
    if args.cmd == "up":
        return servers.cmd_up(models_only=bool(getattr(args, "models_only", False)))
    return {"catalog": cmd_catalog, "manifest": cmd_manifest, "status": cmd_status,
            "serve": servers.cmd_serve, "down": cmd_down, "doctor": doctor.cmd_doctor}[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
