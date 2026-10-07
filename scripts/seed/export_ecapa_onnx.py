# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
"""ECAPA-TDNN 声纹 ONNX 自导出工具（2026-10-07 v2 认人票；一次性、非 CI 面）。

产物=单文件 ``ecapa_tdnn_voxceleb.onnx``（~84MB；wav→Fbank80→CMVN→ECAPA→192 维
整管线进图，特征面在 torch 内闭合——无 numpy 重实现 fbank 的错配面），放置=
``<app-data>/models/``，激活=worker env ``BOK_SPEAKER_LOCK_MODEL`` 指路。

来源与许可：官方 ``speechbrain/spkrec-ecapa-voxceleb``（Apache-2.0，ungated）
自导出——**不用**第三方 0 下载量 re-upload（供给链不可信）。torch/onnxscript
依赖**不在生产 venv**：临时 venv 跑本脚本
（``python -m venv /tmp/ecapa_venv && pip install torch --index-url
https://download.pytorch.org/whl/cpu && pip install speechbrain onnx onnxscript``）。
导出后校验：parity(torch vs onnx) cos≥0.999×三时长；单文件 sha256=
fa492b67df396c8ac297938e98e809ea5a9727547e68828ebc54c4b4163574e4。
"""

from __future__ import annotations

import sys as _sys
import pathlib as _pathlib

_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))

import sys
from pathlib import Path


def main() -> int:
    import numpy as np  # noqa: F401
    import onnx
    import torch
    from speechbrain.inference.speaker import SpeakerRecognition

    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/ecapa")
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "ecapa_raw.onnx"
    final = out_dir / "ecapa_tdnn_voxceleb.onnx"

    class Wrap(torch.nn.Module):
        def __init__(self, sb):
            super().__init__()
            self.feat = sb.mods.compute_features
            self.mvn = sb.mods.mean_var_norm
            self.enc = sb.mods.embedding_model

        def forward(self, wav: torch.Tensor) -> torch.Tensor:
            lens = torch.ones(1, dtype=torch.float32)
            return self.enc(self.mvn(self.feat(wav), lens), lens)

    sb = SpeakerRecognition.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(out_dir / "sb_ckpt")
    )
    wrap = Wrap(sb).eval()
    torch.onnx.export(
        wrap, torch.randn(1, 16000), str(raw),
        input_names=["wav"], output_names=["emb"],
        dynamic_axes={"wav": {1: "T"}, "emb": {}},
        opset_version=17, do_constant_folding=True,
    )
    # 新版导出器把权重外置成 .onnx.data——内联合并成单文件（部署只搬一个文件）
    m = onnx.load(str(raw), load_external_data=True)
    onnx.save_model(m, str(final), save_as_external_data=False)
    print("exported", final)

    import onnxruntime as ort

    sess = ort.InferenceSession(str(final), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(0)
    worst = 1.0
    for dur in (1.0, 2.5, 4.0):
        wav = (rng.standard_normal(int(16000 * dur)) * 0.05).astype(np.float32)[None, :]
        with torch.no_grad():
            t = wrap(torch.from_numpy(wav)).numpy().ravel()
        o = sess.run(None, {"wav": wav})[0].ravel()
        cos = float(np.dot(t, o) / (np.linalg.norm(t) * np.linalg.norm(o)))
        worst = min(worst, cos)
    assert worst >= 0.999, f"parity FAIL {worst}"
    print(f"PARITY_OK {worst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
