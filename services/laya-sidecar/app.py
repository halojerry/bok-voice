"""Bok Laya decision sidecar — local aac6fef/laya-multilingual-mlx (MLX) intent/flow judge.

POST /v1/decide + GET /health. Binds 127.0.0.1 only (host decided by the uvicorn
command line; bok.py wiring passes --host 127.0.0.1): call transcripts never
leave the machine.

Laya is a 0.4B bidirectional (non-autoregressive) decision engine: one forward
pass per question, whole request batched, ~10ms warm per judge shape (see
docs/LAYA-EVAL.md for the local eval: latency/accuracy/truncation pitfalls).
Position vs the 4B/9B generative LLMs: a fast judge WITH calibrated confidence,
not a smarter one — callers gate on `below_floor` and fall back to their LLM.

Contract decisions baked in here (do not re-litigate at call sites):

- Port 8791. The task brief said 8789, but 8789 is the W1b bge-embed sidecar's
  canonical port (CORE_PORTS ("embed", 8789), serve wiring, live stack) — two
  services cannot share it; 2026-09-26 user decision moved laya to the next
  free family slot 8791 (8787/8788/8790 were taken at the time; the v1 worker
  on :8790 has since been retired, 2026-10-02).
- Truncation discipline lives HERE, server-side, single point. The upstream
  hard cap is 1024 tokens with SILENT tail truncation — a 2000-char state and
  a 4500-char state produce byte-identical outputs while the customer's actual
  words (usually at the tail) quietly vanish. We count state tokens with laya's
  own tokenizer and, over budget (LAYA_STATE_TOKEN_BUDGET, default 800), KEEP
  THE HEAD, set state_truncated=true and log a warn. Callers stay dumb.
- choice-only. score/noul measured unusable in the eval (score ranking
  distorted; noul follows option labels); requests carrying them get a 400 in
  plain language instead of a garbage distribution.
- One bad question must not sink the batch: the batch runs first; if it raises,
  each question is retried alone and the failing qid gets {"error": "..."}.
- Concurrent forwards are NOT serialized: the eval measured 199 q/s across 4
  threads on one shared checkpoint, and laya_mlx's own router leaves inference
  outside its lock for the same reason.
- Kill-switch BOK_LAYA_JUDGE=0 → every /v1/decide answers 503 (process stays
  up for the health face). Belt-and-suspenders with the agent-side gate.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException

APP_DIR = Path(__file__).resolve().parent

# 检查点（bok.py MODELS["mac"]["laya"] 同源；owner--name 是 app-data 落盘惯例）。
DEFAULT_REPO = "aac6fef/laya-multilingual-mlx"
PORT = int(os.environ.get("LAYA_PORT", "8791"))
STATE_TOKEN_BUDGET = int(os.environ.get("LAYA_STATE_TOKEN_BUDGET", "800"))
# LAYA-EVAL 实测：3-8 选项安全；20 选项时每选项描述被上游截到 ~10 字（题面照收，
# 只打 warn——上游 build_prefix 自会压缩，运营侧多数判定是 2-4 选）。
OPTION_COUNT_WARN = int(os.environ.get("LAYA_OPTIONS_WARN", "10"))
DEFAULT_CONFIDENCE_FLOOR = 0.5

# 离线测试 affordance：跳过真实模型加载（/health 诚实报 ok=false）。
LOAD_DISABLED = os.environ.get("LAYA_DISABLE_LOAD", "") == "1"

app = FastAPI(title="Bok Laya Decision Sidecar")


def app_data_dir() -> Path:
    """Mirror bok.py app_data_dir()（sidecar 独立 venv，不 import 编排器）。"""
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


def _is_checkpoint(path: Path) -> bool:
    """laya 检查点判据（不是「目录存在」）：Agent.resolve_model 的三个入口文件。"""
    return (
        (path / "model.safetensors").is_file()
        and (path / "rl_agent_config.json").is_file()
        and (path / "encoder" / "config.json").is_file()
        and (path / "tokenizer" / "tokenizer.json").is_file()
    )


def resolve_model_dir() -> Path:
    """LAYA_MODEL_DIR 显式 > app-data models 下 owner--name 目录。

    返回「应该在哪」的路径（存在性/完整性由加载步判，/health 带人话）。
    """
    override = os.environ.get("LAYA_MODEL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return app_data_dir() / "models" / DEFAULT_REPO.replace("/", "--")


def truncate_state_head(
    state: str,
    count_ids: Callable[[str], list[int]],
    decode: Callable[[list[int]], str],
    budget: int,
) -> tuple[str, int, bool]:
    """纯函数：state 超预算时保头截断。

    返回 (feed_text, original_tokens, truncated)。original_tokens 恒为入参
    state 的真实 token 数（截断时调用方才知道超了多少）；feed_text 是实际
    进模型的文本（截断时 = 前 budget 个 token 的解码，边界可能有字节级损耗，
    宁保守不少断——上游 1024 硬顶静默截尾的坑绝不外溢给调用方）。
    """
    ids = count_ids(state)
    n = len(ids)
    if budget <= 0 or n <= budget:
        return state, n, False
    return decode(ids[:budget]), n, True


def _validate_choice_question(qid: str, q: Any) -> str | None:
    """逐问结构校验（choice-only 已在请求级闸过）。返回 None=可进批。"""
    if not isinstance(q, dict):
        return f"question 必须是对象 {{type,instructions,criteria}}"
    instructions = q.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        return "instructions 必须是非空字符串"
    criteria = q.get("criteria")
    if isinstance(criteria, dict):
        labels = list(criteria.keys())
    elif isinstance(criteria, list):
        labels = criteria
    else:
        return "criteria 必须是非空选项数组（或 {标签: 描述} 对象）"
    if not labels:
        return "criteria 不能为空"
    if not all(isinstance(c, str) and c for c in labels):
        return "criteria 选项必须是非空字符串"
    if len(set(labels)) != len(labels):
        return "criteria 选项标签必须唯一"
    # 选项数不设硬顶：超预算时上游 prepare 自会 raise（经批失败→逐问隔离落
    # {"error"}），这里只留 warn（见 decide 尾部 LAYA_OPTIONS）。
    return None


class LayaService:
    def __init__(self) -> None:
        self._agent: Any | None = None
        self._tok: Any | None = None
        self._load_error: str | None = None
        self._load_ms: float | None = None
        self.model_dir: Path = resolve_model_dir()

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        if LOAD_DISABLED:
            self._load_error = "model load disabled (LAYA_DISABLE_LOAD=1)"
            return
        t0 = time.perf_counter()
        try:
            self.model_dir = resolve_model_dir()
            if not self.model_dir.is_dir():
                raise FileNotFoundError(
                    f"Laya 模型不在盘：{self.model_dir} 不存在。"
                    "设 LAYA_MODEL_DIR 指向检查点目录，或先运行 python tools/bok.py download --only laya"
                )
            if not _is_checkpoint(self.model_dir):
                raise FileNotFoundError(
                    f"{self.model_dir} 不是完整的 Laya 检查点"
                    "（缺 model.safetensors / rl_agent_config.json / encoder/config.json / tokenizer/）。"
                    "重新运行 python tools/bok.py download --only laya"
                )
            # 延迟 import：无 laya-mlx 的环境（离线单测/打包扫描）仍可 import 本模块。
            from laya_mlx.agent import Agent
            from laya_mlx.tokenizer import Tokenizer

            agent = Agent(str(self.model_dir), dtype="float16")
            tok = Tokenizer(self.model_dir / "tokenizer")
            # 暖机：吸收首调编译（实测首判 ~0.7s vs 暖态 ~10ms），启动不被阻塞
            # 的代价换首个真判定不吃冷启动（LAYA_WARMUP=0 可关）。
            if os.environ.get("LAYA_WARMUP", "1") == "1":
                agent.predict(
                    "warmup",
                    {"_warm": {"type": "choice", "instructions": "warmup", "criteria": ["A", "B"]}},
                )
            self._agent = agent
            self._tok = tok
            self._load_ms = (time.perf_counter() - t0) * 1000
            self._load_error = None
        except Exception as exc:  # pragma: no cover - model download/load can fail
            self._load_error = str(exc) or repr(exc)
            self._agent = None
            self._tok = None
            self._load_ms = None

    def ensure_loaded(self) -> None:
        if self._load_error:
            raise HTTPException(status_code=503, detail=f"model not ready: {self._load_error}")
        if self._agent is None or self._tok is None:
            raise HTTPException(status_code=503, detail="model not loaded")

    # ----------------------------------------------------------------- health
    def health(self) -> dict:
        return {
            "ok": self._agent is not None and self._load_error is None,
            "model": str(self.model_dir) if self.model_dir else DEFAULT_REPO,
            "loaded_ms": round(self._load_ms, 1) if self._load_ms is not None else None,
            "load_error": self._load_error,
            "kill_switch": os.environ.get("BOK_LAYA_JUDGE", "") == "0",
        }

    # ----------------------------------------------------------------- decide
    @staticmethod
    def _kill_switch_off() -> bool:
        # 读法与全仓同款：显式 "0" 才关（unset=服务——进程被人手动拉起就是要用）。
        return os.environ.get("BOK_LAYA_JUDGE", "") == "0"

    def _count_state_ids(self, state: str) -> list[int]:
        assert self._tok is not None
        return list(self._tok(state, add_special_tokens=False)["input_ids"])

    def _decode_ids(self, ids: list[int]) -> str:
        assert self._tok is not None
        return self._tok.backend.decode(ids, skip_special_tokens=True)

    @staticmethod
    def _map_answer(ans: dict, floor: float) -> dict:
        conf = float(ans.get("confidence") or 0.0)
        return {
            "choice": ans.get("choice"),
            "probabilities": ans.get("probabilities") or {},
            "confidence": round(conf, 4),
            "below_floor": conf < floor,
        }

    def decide(self, payload: dict[str, Any]) -> dict:
        if self._kill_switch_off():
            raise HTTPException(
                status_code=503,
                detail="laya judge disabled by BOK_LAYA_JUDGE=0",
            )
        self.ensure_loaded()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        state = payload.get("state")
        if not isinstance(state, str):
            raise HTTPException(status_code=400, detail="state 必须是字符串（判定上下文/对话原文）")
        questions = payload.get("questions")
        if not isinstance(questions, dict) or not questions:
            raise HTTPException(
                status_code=400,
                detail="questions 必须是非空对象：{qid: {type: 'choice', instructions, criteria[]}}",
            )
        # choice-only 是请求级 400（score/noul 评估结论不可用，收到即人话拒绝）。
        for qid, q in questions.items():
            got = q.get("type") if isinstance(q, dict) else type(q).__name__
            if got != "choice":
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"questions['{qid}'].type={got!r} 不受支持：本服务只支持 type=choice"
                        "（score/noul 判定实测不可用，见 docs/LAYA-EVAL.md）"
                    ),
                )
        floor = payload.get("confidence_floor", DEFAULT_CONFIDENCE_FLOOR)
        if isinstance(floor, bool) or not isinstance(floor, (int, float)):
            raise HTTPException(status_code=400, detail="confidence_floor 必须是数字（0-1）")
        floor = min(1.0, max(0.0, float(floor)))

        # 截断纪律（服务端单点，见模块 docstring）：保头、置旗、打 warn。
        feed_text, state_tokens, truncated = truncate_state_head(
            state, self._count_state_ids, self._decode_ids, STATE_TOKEN_BUDGET
        )
        if truncated:
            print(
                f"LAYA_TRUNCATE state_tokens={state_tokens} budget={STATE_TOKEN_BUDGET} "
                "keep=head（客户原话若在尾部已被截，调用方请把关键原文放头部）",
                flush=True,
            )

        # 逐问结构校验：坏问不进批也不炸批。
        answers: dict[str, dict] = {}
        valid: dict[str, dict] = {}
        for qid, q in questions.items():
            err = _validate_choice_question(str(qid), q)
            if err:
                answers[str(qid)] = {"error": err}
            else:
                valid[str(qid)] = q

        t0 = time.perf_counter()
        if valid:
            agent = self._agent
            try:
                result = agent.predict(feed_text, valid)
                for qid in valid:
                    answers[qid] = self._map_answer(result["answers"][qid], floor)
            except Exception:
                # 批级失败 → 逐问重试隔离：坏 qid 带 error，好 qid 照出答案
                # （prepare 阶段的逐问异常，如选项超出 token 预算，都走这里）。
                for qid, q in valid.items():
                    try:
                        r = agent.predict(feed_text, {qid: q})
                        answers[qid] = self._map_answer(r["answers"][qid], floor)
                    except Exception as exc:
                        answers[qid] = {"error": (str(exc) or repr(exc))[:300]}
        latency_ms = (time.perf_counter() - t0) * 1000
        for qid, q in valid.items():
            if isinstance(q.get("criteria"), list) and len(q["criteria"]) > OPTION_COUNT_WARN:
                print(
                    f"LAYA_OPTIONS questions['{qid}']={len(q['criteria'])} 个选项"
                    f"（>{OPTION_COUNT_WARN} 评估结论不可靠）",
                    flush=True,
                )
        print(
            f"LAYA_DECIDE questions={len(questions)} state_tokens={state_tokens} "
            f"truncated={truncated} ms={latency_ms:.1f}",
            flush=True,
        )
        return {
            "answers": answers,
            "state_truncated": truncated,
            "state_tokens": state_tokens,
            "latency_ms": round(latency_ms, 1),
        }


service = LayaService()


@app.on_event("startup")
def _startup() -> None:
    service.load()


@app.get("/health")
def health() -> dict:
    return service.health()


@app.post("/v1/decide")
def decide(payload: dict[str, Any]) -> dict:
    return service.decide(payload)


if __name__ == "__main__":  # pragma: no cover - manual run convenience
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
