"""Bok CSC sidecar — MacBERT4CSC 中文（zh-only）受限纠错层。

定位：三语纠错架构里的「模型层」。ASR 出来的普通话转写里偶发近音/形近错字
（方按→方案、赔尝→赔偿、京冬→京东），本服务用 shibing624/macbert4csc-base-chinese
（102M，Apache-2.0，BERT 掩码语言模型，逐字等长改写）做一次保守纠错。

为什么是「受限」——上一轮全量实测（/tmp/csc harness，见 docs 记录）结论：

- 普通话面：正确句 **0/4 误改**，p50 15ms / p90 17ms（MPS，fp32），可用。
- 粤语面：模型**碰粤语必漂移**（係→系、哋→跌…），且该漂移在治理上不可接受
  （粤语是 B 线/A 线一等的通话语言，纠错层不准改它一个字）。
- 召回本来就低（阈 0.9 下普通话可纠错句只挑得动高置信的），所以收益是「锦上添花」，
  宁可漏纠也不许误改 —— 服务只接 `lang=="zh"`，粤语在服务端直接拒。

守卫链（每一条都是显式代码，任一条不过 = 放弃纠错、返回原文，绝不冒险）：

a. 冻结 span 掩码：推理前把连续数字串（阿拉伯 + 中文数字词）、拉丁 run、既有标点
   逐字符替换成占位符并记录位置；模型对这些 span 的输出被强制回填原文 ——
   数字/标点/拉丁物理上不可能被改（数字零降级是仓内铁律）。
b. 等长断言：掩码回填后字符数不等（理论上不会，但显式兜底）→ 整条放弃返回原文。
c. 编辑预算：实际改动处数 > max_edits（默认 2）→ 放弃返回原文（防模型一口改多）。
d. 粤语特征字防漂移：输入含 哋/嘅/喺/唔/係 等粤语独有字 → skip（zh 车道判定交给
   调用方，但服务端再防一手，杜绝「调用方判错语言 → 粤语被漂移」）。

端口 8792（8787 ASR / 8788 TTS / 8789 embed / 8790 mt / 8791 laya 已占用）。
只绑 127.0.0.1（通话转写不出本机）。懒加载：首个请求才拉模型（MPS 优先/CPU 回退），
启动不被 102M 权重阻塞。并发小，单 worker + 一把锁串行即可。

kill-switch：`CSC_DISABLE_LOAD=1` 时 /correct 一律 503（进程/health 仍在，供离线冒烟）。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

VERSION = "0.1.0"
PORT = int(os.environ.get("CSC_PORT", "8792"))
# 检查点（102M，Apache-2.0）。MacBERT 掩码语言模型，逐字符等长改写。
REPO = "shibing624/macbert4csc-base-chinese"

MAX_CHARS = int(os.environ.get("CSC_MAX_CHARS", "200"))
# 实测 0.9 是「零误改」档（普通话正确句 0/4 被动），下调会迅速引入假纠。
DEFAULT_THRESHOLD = float(os.environ.get("CSC_THRESHOLD", "0.9"))
DEFAULT_MAX_EDITS = int(os.environ.get("CSC_MAX_EDITS", "2"))
# 离线冒烟/health-only 用：跳过真实模型加载（/health 诚实报 loaded=false）。
LOAD_DISABLED = os.environ.get("CSC_DISABLE_LOAD", "") == "1"

# 占位符：私有区字符在 BERT 词表里必然映射成 [UNK]，模型读不到真实数字/拉丁/标点，
# 且逐字符替换保证冻结 span 与原文等长（回填即还原）——「物理上不可能被改」。
PLACEHOLDER = "\ue000"

# 粤语特征字（只收粤语独有 / 普通话罕见字形）。
# 刻意不收「地 / 既」：「地址」「既然」等普通话常用词会被误伤；而粤语里把「哋/嘅」
# 误写成「地/既」正是我们最怕的漂移源 —— 这类漂移只出现在真粤语文本里，由 哋/嘅
# 本身兜住（输入里只要有 哋/嘅/喺/唔/係 之一就整个 skip）。
YUE_MARKERS = frozenset("哋嘅喺唔係咁冇嚟咗㗎瞓俾睇啲嘢")

# 中文数字词（含大写），与阿拉伯数字同列为冻结 span：数字零降级铁律。
CN_NUMERALS = frozenset("零一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟")


# --------------------------------------------------------------------------- 路径

def app_data_dir() -> Path:
    """Mirror tools/bok.py app_data_dir()（sidecar 独立 venv，不 import 编排器）。"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif sys.platform == "darwin":
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    else:
        base = Path(
            os.environ.get("XDG_DATA_HOME")
            or str(Path(os.environ.get("HOME", ".")) / ".local" / "share")
        )
    return base / "BokVoice"


def resolve_hf_home() -> Path:
    """HF 缓存根。CSC_HF_HOME（离线复用/测试）> 既有 HF_HOME > app-data 落点。"""
    for key in ("CSC_HF_HOME", "HF_HOME"):
        val = os.environ.get(key, "").strip()
        if val:
            return Path(val).expanduser()
    return app_data_dir() / "models" / "csc-macbert"


def resolve_model_source() -> str | None:
    """CSC_MODEL_DIR 显式本地检查点（离线/打包）> None=从 HF_REPO 下载/读缓存。"""
    val = os.environ.get("CSC_MODEL_DIR", "").strip()
    return val or None


# ------------------------------------------------------------------- 纯函数（守卫）

def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def freeze_spans(text: str) -> tuple[str, set[int]]:
    """守卫 a：逐字符冻结数字/拉丁/标点 span，返回 (掩码文本, 冻结下标集合)。

    逐字符替换而非整段替换 —— 保持字符位置一一对应，掩码文本与原文等长，
    模型输出可原位回填。模型看不到真实数字，因此不可能改数字。
    """
    frozen: set[int] = set()
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch.isdigit() or ch in CN_NUMERALS:
            while i < n and (text[i].isdigit() or text[i] in CN_NUMERALS):
                frozen.add(i)
                i += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            while i < n and (("a" <= text[i] <= "z") or ("A" <= text[i] <= "Z")):
                frozen.add(i)
                i += 1
        elif not _is_cjk(ch):
            # 标点、空白、符号、emoji 等一切非汉字非数字非拉丁字符。
            frozen.add(i)
            i += 1
        else:
            i += 1
    masked = "".join(PLACEHOLDER if k in frozen else text[k] for k in range(n))
    return masked, frozen


def find_yue_markers(text: str) -> list[str]:
    """守卫 d：返回输入里出现的粤语特征字（去重保序）。"""
    seen: list[str] = []
    for ch in text:
        if ch in YUE_MARKERS and ch not in seen:
            seen.append(ch)
    return seen


def restore_and_guard(
    original: str, masked_out: str, frozen: set[int], max_edits: int
) -> tuple[str, list[dict], str | None]:
    """守卫 a 回填 + 守卫 b 等长 + 守卫 c 编辑预算。

    返回 (最终文本, edits, skipped_reason)。任一守卫不过 → (原文, [], reason)。
    edits 是逐字符替换（等长保证），pos 为 0 基字符下标。
    """
    # 守卫 b：等长断言（掩码回填后字符数必须等于原文；不等说明对齐失败）。
    if len(masked_out) != len(original):
        return original, [], "length_mismatch"
    # 守卫 a 回填：冻结位置一律取原文，模型在那里预测什么都丢掉。
    restored = "".join(
        original[i] if i in frozen else masked_out[i] for i in range(len(original))
    )
    edits = [
        {"pos": i, "from": original[i], "to": restored[i]}
        for i in range(len(original))
        if restored[i] != original[i]
    ]
    # 守卫 c：编辑预算（改动太多说明模型在校正之外乱猜，整条放弃）。
    if len(edits) > max_edits:
        return original, [], "over_budget"
    return restored, edits, None


def _skip(text: str, reason: str) -> dict:
    return {"text": text, "edits": [], "changed": False, "skipped_reason": reason}


# ------------------------------------------------------------------------ 服务

class CscService:
    def __init__(self) -> None:
        # 一把锁同时护住「懒加载单飞」与「推理串行」（并发小，串行可接受）。
        self._lock = threading.Lock()
        self._tok: Any | None = None
        self._model: Any | None = None
        self._device: str | None = None
        self._loaded = False
        self._load_error: str | None = None
        self._load_ms: float | None = None
        self._model_source: str = ""

    # ---------------------------------------------------------------- load
    def load(self) -> None:
        """（首次请求触发）下载/加载模型。MPS 优先，CPU 回退。

        保持 fp32：阈值 0.9 的标定是在 fp32 下做的，转 fp16 会动分布、
        破坏「零误改」标定。
        """
        if LOAD_DISABLED:
            self._load_error = "model load disabled (CSC_DISABLE_LOAD=1)"
            return
        t0 = time.perf_counter()
        try:
            import torch
            from transformers import BertForMaskedLM, BertTokenizerFast

            hf_home = resolve_hf_home()
            hf_home.mkdir(parents=True, exist_ok=True)
            # 用 HF_HOME（而非 cache_dir 形参）定位缓存：与 harness /tmp/csc/models
            # 的 HF_HOME 布局同源，离线复用零重下。
            os.environ["HF_HOME"] = str(hf_home)

            src = resolve_model_source()
            if src:
                self._model_source = src
                tok = BertTokenizerFast.from_pretrained(src)
                model = BertForMaskedLM.from_pretrained(src)
            else:
                self._model_source = REPO
                tok = BertTokenizerFast.from_pretrained(REPO)
                model = BertForMaskedLM.from_pretrained(REPO)

            device = "mps" if torch.backends.mps.is_available() else "cpu"
            model.to(device).eval()
            self._tok = tok
            self._model = model
            self._device = device
            self._loaded = True
            self._load_error = None
            self._load_ms = (time.perf_counter() - t0) * 1000
        except Exception as exc:  # pragma: no cover - 下载/加载失败
            self._load_error = str(exc) or repr(exc)
            self._tok = self._model = None
            self._loaded = False
            self._load_ms = None

    def ensure_loaded(self) -> None:
        if LOAD_DISABLED:
            raise HTTPException(status_code=503, detail="model load disabled (CSC_DISABLE_LOAD=1)")
        if self._loaded:
            return
        if self._load_error:
            # 失败即定盘（与 laya 同款）：不每请求重试下载；恢复靠重启。
            raise HTTPException(status_code=503, detail=f"model not ready: {self._load_error}")
        with self._lock:
            if not self._loaded and not self._load_error:
                self.load()
        if self._load_error:
            raise HTTPException(status_code=503, detail=f"model not ready: {self._load_error}")

    # -------------------------------------------------------------- health
    def health(self) -> dict:
        return {
            "ok": self._load_error is None,
            "version": VERSION,
            "model": self._model_source or REPO,
            "model_loaded": self._loaded,
            "device": self._device,
            "loaded_ms": round(self._load_ms, 1) if self._load_ms is not None else None,
            "load_error": self._load_error,
            "threshold": DEFAULT_THRESHOLD,
            "max_edits": DEFAULT_MAX_EDITS,
            "max_chars": MAX_CHARS,
        }

    # ------------------------------------------------------------- inference
    def _model_rewrite(self, masked: str, threshold: float) -> str:
        """对掩码文本做一次逐字等长改写（阈值以下的字符保留掩码原字符）。

        对齐用 offset_mapping（而非 decode().split(' ')）——中文 BERT 词表会把
        「AB1234」「1,234.56」「〇〇」这类并成多字符 token、也会把占位符并成 [UNK]，
        朴素 decode 会长度对不上而整条放弃；offsets 能把每个单字符 token 精确定位。
        多字符 token / ## 续接 token 一律跳过（保留原字符）——保守即安全。
        """
        import torch

        tok, model, device = self._tok, self._model, self._device
        enc = tok([masked], return_tensors="pt", return_offsets_mapping=True)
        offsets = enc["offset_mapping"][0].tolist()
        with torch.no_grad():
            logits = model(
                input_ids=enc["input_ids"].to(device),
                attention_mask=enc["attention_mask"].to(device),
            ).logits[0]
        probs = torch.softmax(logits, dim=-1)
        max_p, arg = torch.max(probs, dim=-1)
        max_p = max_p.cpu().tolist()
        tokens = tok.convert_ids_to_tokens(arg.cpu().tolist())
        out = list(masked)
        for t, (s, e) in enumerate(offsets):
            if e <= s or s >= len(masked):
                continue  # [CLS]/[SEP]/padding 等特殊 token
            piece = tokens[t]
            if piece.startswith("##") or len(piece) != 1:
                continue  # 多字符/续接 token：跳过（保留原字符）
            if e - s != 1:
                continue
            if max_p[t] >= threshold:
                out[s] = piece
        return "".join(out)

    # --------------------------------------------------------------- correct
    def _run(self, text: str, lang: str, max_edits: int, threshold: float) -> dict:
        # 收费最便宜的两道门先走：语言 → 空串 → 粤语特征字。
        if lang != "zh":
            return _skip(text, "lang_not_zh")
        if text == "":
            return _skip(text, "empty")
        markers = find_yue_markers(text)
        if markers:
            # 守卫 d：调用方判成 zh 也拦（粤语宁可漏纠也不能漂移）。
            return _skip(text, "cantonese_markers:" + "".join(markers))

        self.ensure_loaded()

        masked, frozen = freeze_spans(text)  # 守卫 a（推理前）
        with self._lock:
            masked_out = self._model_rewrite(masked, threshold)
        # 守卫 a 回填 + b 等长 + c 预算
        restored, edits, reason = restore_and_guard(text, masked_out, frozen, max_edits)
        if reason:
            return _skip(text, reason)
        return {
            "text": restored,
            "edits": edits,
            "changed": bool(edits),
            "skipped_reason": None,
        }

    def correct(self, payload: dict[str, Any]) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        text = payload.get("text")
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail="text 必须是字符串")
        lang = payload.get("lang", "")
        if not isinstance(lang, str):
            raise HTTPException(status_code=400, detail="lang 必须是字符串（zh/cantonese/en）")
        max_edits = payload.get("max_edits", DEFAULT_MAX_EDITS)
        if isinstance(max_edits, bool) or not isinstance(max_edits, int) or max_edits < 0:
            raise HTTPException(status_code=400, detail="max_edits 必须是 >=0 的整数")
        threshold = payload.get("threshold", DEFAULT_THRESHOLD)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise HTTPException(status_code=400, detail="threshold 必须是数字（0-1）")
        threshold = min(1.0, max(0.0, float(threshold)))

        # 语言门先于长度门：粤语长文本也走 200 skip（不必 400）。
        if lang == "zh" and len(text) > MAX_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"text 超长：{len(text)} 字 > 上限 {MAX_CHARS} 字（本服务只处理短转写片段）",
            )

        t0 = time.perf_counter()
        result = self._run(text, lang, max_edits, threshold)
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        result["threshold"] = round(threshold, 4)
        return result


service = CscService()

app = FastAPI(title="Bok CSC Sidecar (MacBERT4CSC, zh-only)")


@app.get("/health")
def health() -> dict:
    return service.health()


@app.post("/correct")
def correct(payload: dict[str, Any]) -> dict:
    return service.correct(payload)


if __name__ == "__main__":  # pragma: no cover - manual run convenience
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
