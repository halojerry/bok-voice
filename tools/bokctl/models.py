#!/usr/bin/env python
"""models 域(模型目录/路径/下载/选型:MODELS 平台模型表、model_dir/model_path
解析、resolve_llm_repo 档位选型、_mt/_settle/laya/_llm_draft 专线模型解析、
setup_models/cmd_setup/cmd_download/cmd_catalog/cmd_manifest 装机面;
G2 W② 从 core 搬出,搬运纪律=穿模块对象调用)。

- 本模块只 `from bokctl import core` 拿模块对象:凡仍住在 core 的名字(platform_key/
  is_packaged/is_mac/is_linux/app_data_dir 等一律 `core.X` 调用时取——patch 与
  后续域搬运在 core 侧保持可见(patch 缝=模块属性)。
- 本域自有名件(MODELS/WINDOWS_LLM_GGUF_PATTERNS/OPTIONAL_MODELS/model_dir/
  _lmstudio_models_dir/_usable_model_dir/model_path/_settings_llm_local_model/
  resolve_llm_repo/_mt_llm_model/_settle_llm_model/_usable_laya_dir/
  laya_model_path/_llm_draft_enabled/_llm_draft_model/_llm_draft_flags/
  cmd_catalog/cmd_manifest/setup_models/_all_models_present/cmd_setup/
  _dir_sha256/_enable_hf_transfer/cmd_download)域内裸名互调(同模块全局=
  call-time 可 patch)。
- 留守 core 的近邻(边界记录,2026-10-04):MLX_SERVER_WRAPPER 是 ROOT 派生路径
  常量(paths 域候选;core 的域 import 行先于 ROOT 定义,域模块 import 期取不到
  core.ROOT——常量必须留 core,消费者 servers 穿 core.MLX_SERVER_WRAPPER);
  platform_key/app_data_dir/is_packaged/is_mac/is_linux 属 paths 域(后批);
  _dev_9b_enabled/_settle_gate_url 是 env 组装面闸键(env 域最后一批)——本域
  专线解析(_settle_llm_model/_mt_llm_model)只管「模型路径是甚么」,起不起
  对应端口是 env/servers 域的决策。
- 测试面:patch 一律走 tests/_bokpatch.py(patch_bok;PATCH_TARGETS 已把 model_dir/
  _lmstudio_models_dir/model_path/_settings_llm_local_model/_settle_llm_model/
  _enable_hf_transfer/cmd_download 改道 bokctl.models);facade 读用 bok.models.X。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from bokctl import core

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


def model_dir(repo_id: str) -> Path:
    return core.app_data_dir() / "models" / repo_id.replace("/", "--")


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
    if core.is_packaged():
        return str(model_dir(repo))
    if core.is_mac():
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
    if core.is_linux():
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

        db_path = core.app_data_dir() / "bok_voice.db"
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
    if core.is_packaged():
        return str(model_dir(repo))
    if core.is_mac():
        lm = _lmstudio_models_dir() / repo
        if _usable_laya_dir(lm):
            return str(lm)
        app = model_dir(repo)
        if _usable_laya_dir(app):
            return str(app)
        return ""
    return repo


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


def cmd_catalog() -> int:
    key = core.platform_key()
    print(f"platform: {key}")
    for name, repo in MODELS[key].items():
        if repo:
            print(f"  {name:<12} {repo}")
    print(f"  ~download into {core.app_data_dir() / 'models'}")
    return 0


def cmd_manifest() -> int:
    """Emit a JSON manifest for the desktop shell / CI release pipeline."""
    key = core.platform_key()
    data: dict = {
        "platform": key,
        "app_data_dir": str(core.app_data_dir()),
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
    key = core.platform_key()
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
    key = core.platform_key()
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
        if core.is_mac():
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
