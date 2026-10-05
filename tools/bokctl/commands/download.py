"""bokctl.commands.download —— `bok download`（G2 W③ 自 bokctl.models 搬入）。

模型域共享件（MODELS/WINDOWS_LLM_GGUF_PATTERNS/model_dir/
_lmstudio_models_dir/_enable_hf_transfer/_llm_draft_enabled）仍住
bokctl.models，一律穿 ``models.X`` call-time 取（patch 缝=模块属性）。"""
from __future__ import annotations

import os
import sys

from bokctl import models, paths


def cmd_download(only: set[str] | None = None) -> int:
    """下载平台模型表里的模型（幂等：已在盘跳过；`only` 限定子集——装机选型用）。

    `only` 提到表内不存在的键（如非 mac 表的 mt/settle）→ 逐项说明「未配置，
    对应功能回退主 LLM」，不算失败（与 OPTIONAL_MODELS 语义一致）。
    """
    key = paths.platform_key()
    table = models.MODELS[key]
    requested = set(only) if only else None
    if requested:
        for name in sorted(requested - set(table)):
            print(f"  [skip] {name} 当前平台表未配置（可选档；对应功能回退主 LLM :1235）")
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:  # pragma: no cover
        print(f"[download] huggingface_hub missing: {exc}", file=sys.stderr)
        return 2
    models._enable_hf_transfer()
    for name, repo in table.items():
        if not repo:
            continue
        # draft 权重 opt-in(2026-09-25,BOK_LLM_DRAFT 默认关):全量下载/serve
        # ensure 不拉 0.6B(~335MB)——默认档零下载零驻留(全栈 47/48G 内存压力
        # 线上,没人用的权重不占盘不占内存)。显式 --only llm_draft 或
        # BOK_LLM_DRAFT=1 才落盘。
        if (name == "llm_draft" and not models._llm_draft_enabled()
                and (requested is None or "llm_draft" not in requested)):
            print("  [skip] llm_draft (BOK_LLM_DRAFT!=1 默认不下载;补齐: download --only llm_draft)")
            continue
        if requested is not None and name not in requested:
            continue
        target = models.model_dir(repo)
        if target.exists() and any(target.iterdir()):
            print(f"  [ok]   {name} present  {target}")
            continue
        # mac dev 的 lmstudio 布局同样算「已在盘」——与 model_path 的「哪边真实
        # 存在用哪边」同语义;不认的话 lmstudio 已有的模型会被重复下载 5.5GB
        # (2026-09-17 settle 9B 实证:serve 在 ensure 步静默拉 HF)。
        if paths.is_mac():
            lm = models._lmstudio_models_dir() / repo
            if lm.exists() and any(lm.iterdir()):
                print(f"  [ok]   {name} present (lmstudio)  {lm}")
                continue
        print(f"  [down] {name}  {repo}")
        kwargs: dict = {}
        if key == "windows" and name == "llm":
            kwargs["allow_patterns"] = models.WINDOWS_LLM_GGUF_PATTERNS
        # hf_hub 1.x 自动断点续传，无需显式 resume_download。
        snapshot_download(repo_id=repo, local_dir=str(target), **kwargs)
        print(f"  [ok]   {name} downloaded")
    # hf_transfer 只用于下载加速；下载完成后摘掉，避免泄漏到 sidecar/LLM 进程，
    # 防止模型加载阶段偶发阻塞（观察：TTS 首启卡死与 HF_HUB_ENABLE_HF_TRANSFER 同现）。
    os.environ.pop("HF_HUB_ENABLE_HF_TRANSFER", None)
    return 0


def run(args) -> int:
    only = set(getattr(args, "only", None) or []) or None
    return cmd_download(only=only)
