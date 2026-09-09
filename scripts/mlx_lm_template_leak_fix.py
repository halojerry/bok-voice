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



# ---- S5 排队定罪计时补丁（2026-09-09,独立于模板归一补丁,幂等）----
TIMING_MARK = "bok-timing"
TIMING_ANCHOR = """        # Create the token generator
        try:
            ctx, response = self.response_generator.generate(
                request,
                args,
                progress_callback=keepalive_callback,
            )
        except Exception as e:"""
TIMING_BLOCK = """        # [bok-timing 2026-09-09] generate() 内含「模型锁等待 + prompt
        # processing」——首行 progress 前的耗时=锁等待+首步 prefill,行间=prompt
        # 处理窗。缺这条没法把暖轮 TTFT 的排队常数定罪到具体环节。BOK_TIMING_PATCH=0 关。
        _bok_t0 = time.perf_counter()
        _bok_fp = [None, None]
        _bok_orig_cb = keepalive_callback

        def _bok_progress_cb(_p, _t):
            _now = time.perf_counter()
            if _bok_fp[0] is None:
                _bok_fp[0] = _now
            _bok_fp[1] = _now
            _bok_orig_cb(_p, _t)

        try:
            ctx, response = self.response_generator.generate(
                request,
                args,
                progress_callback=_bok_progress_cb,
            )
            _bok_g1 = time.perf_counter()
            _bok_g = (_bok_g1 - _bok_t0) * 1000
            _bok_fpm = ((_bok_fp[0] or _bok_g1) - _bok_t0) * 1000
            _bok_ppw = ((_bok_fp[1] - _bok_fp[0]) * 1000) if _bok_fp[0] else 0.0
            logging.info(
                "[bok-timing] generate_ms=%.0f first_progress_ms=%.0f prompt_window_ms=%.0f"
                % (_bok_g, _bok_fpm, _bok_ppw)
            )
        except Exception as e:"""


def _apply_timing_patch(path: str, src: str) -> str:
    if os.environ.get("BOK_TIMING_PATCH", "1") != "1":
        print("[bok-timing] disabled by env")
        return src
    if TIMING_MARK in src:
        print(f"[bok-timing] already patched: {path}")
        return src
    if TIMING_ANCHOR not in src:
        print(f"[bok-timing] anchor not present (upstream changed?), skip: {path}")
        return src
    import py_compile

    patched = src.replace(TIMING_ANCHOR, TIMING_BLOCK, 1)
    compile(patched, path, "exec")  # 语法校验,写坏 server 会炸整个栈
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    py_compile.compile(path, doraise=True)
    print(f"[bok-timing] patched: {path}")
    return patched


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
        _apply_timing_patch(path, src)
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
    # 计时补丁在模板补丁落盘后基于新内容再打（幂等各自 MARK）
    _apply_timing_patch(path, patched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
