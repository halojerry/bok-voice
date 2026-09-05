#!/usr/bin/env python3
"""mlx_lm 生成提示边界归一补丁（幂等，BOK_TEMPLATE_LEAK_FIX=0 关闭）。

根因（2026-09-06 token 级探针实证，档案 .superpowers/sdd/2026-09-05-llm-cache/）：
聊天模板 endfor 后的注释块（"{# Final Generation Prompt #}"）未做左侧剥白，
生成模式提示在最后一条消息后多泄一个 "\n"（token ĊĊ vs 历史模式 Ċ）。
mlx_lm LRUPromptCache 只复用「严格前缀」，上一轮请求因此永远不是下一轮的
前缀，跨轮命中坍缩回 system 锚点（实测 25/78）。Qwen3.5 等混合架构模型的
ArraysCache 层不可裁剪（is_trimmable()=False），fetch 的「修剪长键」路径
是死路——唯一出路就是让跨轮 token 序列真正一致。

本补丁把该注释改为右剥白（{#- … #}），生成/历史边界 token 归一，纯空白
差异、零语义变化；实测命中 25/78 → 48/78（剩余未命中为生成头 <think>
不对称，属模板语义改动，另行决策）。模板无该注释模式时为 no-op。

runtime site-packages 不入 git：desktop/runtime 重建后由 tools/bok.py
启动 :1235/:1236 前自动重跑本脚本。
"""

from __future__ import annotations

import importlib.util
import os

MARK = "bok-cache-fix"
OLD = "{# Final Generation Prompt #}"
NEW = "{#- Final Generation Prompt #}"

ANCHOR = """        # Update the member variables
        self.model_key = (model_path, adapter_path, draft_model_path)
        self.model = model
        self.tokenizer = tokenizer"""

BLOCK = """        # [bok-cache-fix 2026-09-06] 生成提示边界归一（KV-cache 跨轮命中前提）：
        # 模板 endfor 后的注释块未剥白，生成模式提示尾部多泄一个 "\\n"
        # （ĊĊ vs 历史模式 Ċ）→ 上一轮请求永远不是下一轮的缓存前缀，mlx_lm
        # LRU（严格前缀匹配）命中坍缩回 system 锚点；混合架构模型
        # （Qwen3.5 ArraysCache 层不可裁剪）连 fetch 的修剪长键路径也是死路。
        # 此处把注释改为右剥白（{#-），生成/历史边界 token 归一，纯空白差异、
        # 零语义变化。模板无该注释模式时为 no-op；BOK_TEMPLATE_LEAK_FIX=0 回退。
        if os.environ.get("BOK_TEMPLATE_LEAK_FIX", "1") == "1":
            _ct = getattr(tokenizer, "chat_template", None)
            if _ct and OLD_PLACEHOLDER in _ct:
                tokenizer.chat_template = _ct.replace(
                    OLD_PLACEHOLDER,
                    NEW_PLACEHOLDER,
                    1,
                )
                logging.info(
                    "[bok-cache-fix] chat template generation-prompt whitespace leak normalized"
                )

"""


def _server_path() -> str | None:
    try:
        spec = importlib.util.find_spec("mlx_lm.server")
    except Exception:
        return None
    return spec.origin if spec and spec.origin else None


def main() -> int:
    if os.environ.get("BOK_TEMPLATE_LEAK_FIX", "1") != "1":
        print("[bok-cache-fix] disabled by env")
        return 0
    path = _server_path()
    if not path:
        print("[bok-cache-fix] mlx_lm.server not found, skip")
        return 0
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if MARK in src:
        print(f"[bok-cache-fix] already patched: {path}")
        return 0
    if ANCHOR not in src:
        print(f"[bok-cache-fix] anchor not present (upstream changed?), skip: {path}")
        return 0
    block = BLOCK.replace("OLD_PLACEHOLDER", repr(OLD)).replace("NEW_PLACEHOLDER", repr(NEW))
    patched = src.replace(ANCHOR, block + ANCHOR, 1)
    if "import os\n" not in patched.split("def ")[0]:
        patched = patched.replace("import logging\n", "import logging\nimport os\n", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    import py_compile

    py_compile.compile(path, doraise=True)
    print(f"[bok-cache-fix] patched: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
