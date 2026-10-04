"""术语门禁：粤语值全时空统一小写 cantonese（AGENTS.md「Language / Terminology Rules」）。

全仓跟踪的源文件不得出现旧拼写 yue（大小写不敏感），唯以下单点豁免：
- deps.py / test_control_plane.py：DB 存量迁移（唯一兼容点）及其回归夹具
- web_search.py / test_web_search.py：维基百科外部域名 zh-yue.wikipedia.org
- interpret.py / test_interpret_tts_provider.py / agent.py / test_fixed_language_call.py：
  MiniMax language_boost 外部枚举（TTS 供应商 API 真字面量，粤=普+粤标记；
  B 线 interpret 与 A 线 entrypoint per-call 注入同源同值，值经 env 单点透传）
- test_volcano_v3.py / ARCHITECTURE.md：Volcano API dialect 枚举（外部接口字面量）
- docs/superpowers/plans/2026-09-21-a-line-speed-asr-decision-verification.md：
  ASR 官方卡引述的外部数据集专名（Fleurs-yue / WenetSpeech-Yue / CV-yue）
- docs/archive/**、AGENTS.md：历史档案与政策文档

新增 yue 字面量 = 本测试失败。这是字段单轨化的防复发门禁：旧拼写只允许存在于
「别人的接口」和「改写它的迁移」里，我们自己的命名/字段/键/值一律 cantonese。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_ARCHIVE_PREFIX = "docs/archive/"

# 相对路径 → 该文件内允许包含旧拼写的行正则；None = 整文件豁免；未列名 = 全禁
_ALLOWLIST: dict[str, re.Pattern[str] | None] = {
    "apps/control-plane/control_plane/deps.py": None,
    "tests/test_control_plane.py": None,
    "apps/agent/agent_runtime/web_search.py": re.compile(r"zh-yue"),
    "tests/test_web_search.py": re.compile(r"zh-yue"),
    # whisper 语言码外部枚举（OpenAI/whisper 粤语=language "yue"，与 zh/en
    # 同族 API 真字面量）——asr_whisper_bench.py 是 bench 脚本，yue 只出现在
    # 语言码映射/转写调用/报告行，语言字段本身仍一律 cantonese。
    "scripts/bench/asr_whisper_bench.py": re.compile(r"yue"),
    # MiniMax language_boost 外部枚举(API 真字面量,粤=普+粤标记);只豁免带该
    # 枚举值的行,语言字段本身仍一律 cantonese。A 线 agent.py 同源注入
    # (per-call 固定语言,B 线 interpret 同值)。
    "apps/agent/agent_runtime/interpret.py": re.compile(r"Chinese,Yue"),
    # 0913 验收探针的 MM 合成话音线同枚举(t2a_v2 language_boost 外部字面量)。
    "scripts/probes/acceptance_0913_scenarios.py": re.compile(r"Chinese,Yue"),
    "scripts/lib/mm_voice.py": re.compile(r"Chinese,Yue"),
    "apps/agent/agent_runtime/agent.py": re.compile(r"Chinese,Yue"),
    # 5a(2026-09-30) AB 首 chunk 三臂样本脚本入库——同 MM language_boost 外部字面量。
    "scripts/bench/ab_tts_first_chunk.py": re.compile(r"Chinese,Yue"),
    "scripts/bench/bench_minimax_bidi.py": re.compile(r"Chinese,Yue"),
    "tests/test_interpret_tts_provider.py": re.compile(r"Chinese,Yue"),
    # 2026-10-04 CI 真 PG 回归钉:yue→cantonese 迁移的 LIKE 通配语义测试——
    # 「%yue%」是 DB 存量行匹配的迁移真值(deps.py 迁移同串,该文件已整文件
    # 豁免),这里只豁免带该通配串/迁移名的行;其余行出现 yue 照样红。
    "tests/test_db_portability.py": re.compile(r"%yue%|yue→cantonese"),
    "tests/test_fixed_language_call.py": re.compile(r"Chinese,Yue"),
    # P1(2026-10-01) SV-CPU 引擎车道:SenseVoice 外部语言枚举(yue,与 zh/en 同族
    # sherpa API 真字面量)+ HF repo id(zh-en-ja-ko-yue=仓库名 opaque 标识)。
    # 语言字段本身(session/通话级)仍一律小写 cantonese——_sv_lang_label 即收口
    # 单点(外部枚举→内部规范值的边界映射)。
    "services/qwen3-asr-sidecar/app.py": re.compile(r"yue"),
    "scripts/pipeline/eval_sensevoice.py": re.compile(r"yue"),
    "tools/bok.py": re.compile(r"zh-en-ja-ko-yue"),
    "tests/test_asr_engine_routing.py": re.compile(r"yue"),
    "scripts/archive/test_volcano_v3.py": None,
    # CSC 管线(2026-10-02 入库):yue 只作**车道标识**(LANE_YUE 常量收口单点,
    # 值="yue")与特征字计量(yue_markers_lost)——语言字段一律 cantonese
    # (lang 归一边界映射 in ("cantonese","yue","zh-yue") 同 whisper 先例)。
    # prepare_csc_data.py 车道语义密度最高(yue_pool/id_yue/yue_marker_pool/
    # 车道语义注释 60+ 处),token 枚举不成句——按 qwen3-asr-sidecar 先例整
    # 文件豁免;其余文件按 token 族窄匹配,裸 language="yue" 赋值不含这些
    # token 仍会被拦。
    "scripts/seed/prepare_csc_data.py": re.compile(r"yue", re.IGNORECASE),
    "scripts/pipeline/eval_csc_model.py": re.compile(r"yue_marker|yue_loss|yue_markers|\"yue\", \"zh-yue\"", re.IGNORECASE),
    "scripts/pipeline/predict_csc_model.py": re.compile(r"yue_marker_loss", re.IGNORECASE),
    "tests/test_csc_data.py": re.compile(
        r"_ITEM_YUE|yue_marker|EMBEDDED_SEED_YUE|yue_ratio|yue_id\b|\"yue\"|test_yue_|yue identity|yue 句|yue-keep",
        re.IGNORECASE,
    ),
    "tests/test_probe_stimulus_tools.py": re.compile(r"LANE_YUE|\"yue\"", re.IGNORECASE),
    "services/csc-sidecar/app.py": re.compile(
        r"YUE_MARKERS|yue_markers|cantonese_markers", re.IGNORECASE
    ),
    "services/csc-sidecar/selftest.py": re.compile(
        r"yue =|\"text\": yue|== yue|YUE_MARKERS|yue_markers|cantonese_markers", re.IGNORECASE
    ),
    # CSC 种子测试集(数据文件):"yue-fix-N"/"yue-keep-N" 是**用例 id 分组前缀**
    # (修复组/保持组),非语言字段——数据里语言字段一律 "cantonese"。窄豁免 id 形状。
    "data/csc/csc_testset.json": re.compile(r"yue-(fix|keep)"),
    # 计划文档里的粤语文本检测示例代码（_YUE_MARKS 正则只是「识别粤语字」的
    # 变量名，非语言字段赋值）——4b0dd6b 存量，按门禁政策文档白名单收口。
    "docs/superpowers/plans/2026-09-17-qa-canvas-phase1.md": re.compile(r"_YUE_MARKS"),
    # MiniMax ASR BCP-47 语言头外部枚举(粤语=yue,asr-1.0 /v1/speech_to_text 真字面量,
    # 同 language_boost 政策):只豁免带该枚举值的行,探针内部语言字段一律 cantonese。
    "scripts/probes/probe_cloud_asr_ab.py": re.compile(r'"yue"'),
    # 同族扩展(2026-10-03):probe_cloud_asr.py 的厂商标签已收口单点
    # _VENDOR_LANG——MiniMax BCP-47(yue)与火山 SAUC language(yue-CN)两枚举
    # 同宿一行;只豁免带该枚举值的行,内部语言字段一律 cantonese。
    "scripts/probes/probe_cloud_asr.py": re.compile(r'"yue"|yue-CN'),
    # vendored s2s（Apache-2.0 上游镜像 @81b688b4）：Whisper/SenseVoice 的 BCP-47
    # 语言枚举（"yue" token 表/解码选项/`"yue"→"cantonese"` 上游映射）是
    # 「别人的接口」类不透明标识符——上游镜像不改写；我方接线
    # （s2s_realtime/s2s_flow_worker）的语言字段仍一律 cantonese。
    "services/s2s/src/speech_to_speech/LLM/utils.py": re.compile(r'"yue": "cantonese"'),
    "services/s2s/src/speech_to_speech/STT/faster_whisper_handler.py": re.compile(r'"yue"'),
    "services/s2s/src/speech_to_speech/STT/whisper_stt_handler.py": re.compile(r"<\|yue\|>|yue"),
    "services/s2s/src/speech_to_speech/STT/README.md": re.compile(r"`yue`"),
    "services/s2s/src/speech_to_speech/arguments_classes/sense_voice_stt_arguments.py": re.compile(r"auto, zh, en, yue, ja, or ko"),
    # 0e2ccad 归档件（0910-0913 历史 plan 文档/探针）：引述旧拼写均为决策记录与
    # 遗留 fixture 名匹配，非运行时语言字段——按行豁免，新文件仍全禁。
    "docs/superpowers/plans/2026-09-09-official-first.md": re.compile(r"yue"),
    "docs/superpowers/plans/2026-09-10-kb-incremental-indexing.md": re.compile(r"yue"),
    "docs/superpowers/plans/2026-09-10-smart-turn-cantonese-spike.md": re.compile(r"yue"),
    "docs/superpowers/plans/2026-09-16-security-remediation.md": re.compile(r"yue"),
    # qa-canvas phase1 plan（4b0dd6b）：粤语文本识别代码片段的 `_YUE_MARKS` 变量名
    # （大写，IGNORECASE 才罩得住），决策记录非运行时语言字段——按行豁免，新文件仍全禁。
    "docs/superpowers/plans/2026-09-17-qa-canvas-phase1.md": re.compile(r"yue", re.IGNORECASE),
    # 战役调度+仪表盘实施计划（b5e77d1）：全局约束段引用「yue 字面量」门禁本身的
    # 决策记录非运行时语言字段——按行豁免，新文件仍全禁。
    "docs/superpowers/plans/2026-09-17-campaign-scheduling-dashboard.md": re.compile(r"yue"),
    "scripts/probes/probe_smart_turn.py": re.compile(r"yue"),
    # 2026-09-26 复核轮入库的文档：S2S_ROADMAP 增补引 MiniMax ASR BCP-47 `yue`
    # 外部枚举与 FLEURS 多语数据集位、2026-09-24 计划引 CantoNLU zh→yue 迁移
    # 结论/LiveKit detector 语言表缺席/sherpa zh-yue-en 三语——均为「别人的接口/
    # 数据集」类决策记录引述，非我方语言字段（政策同 zh-yue.wikipedia.org）。
    "docs/S2S_ROADMAP.md": re.compile(r"yue"),
    "docs/superpowers/plans/2026-09-24-a-line-flow-latency-intent.md": re.compile(r"yue"),
    # P0 真库烟测做 yue→cantonese 数据迁移演练(铺旧值行验证 build_engine 改写)，
    # 同 deps.py 类：旧拼写是演练夹具非运行时语言字段，按行豁免。
    "scripts/ops/smoke_postgres.py": re.compile(r"yue"),
    "docs/ARCHITECTURE.md": re.compile(r"VOLC_DIALECT"),
    # 零代码验证计划（2026-09-21）引述 Qwen3-ASR 官方卡的外部**数据集专名**
    # （Fleurs-yue / WenetSpeech-Yue / CV-yue）——「别人的接口」类不透明标识符，
    # 同 zh-yue.wikipedia.org 政策。只豁免点名这三个数据集的行；文档里描述我们
    # 自己的语言字段仍一律 cantonese。
    "docs/superpowers/plans/2026-09-21-a-line-speed-asr-decision-verification.md": re.compile(
        r"Fleurs-yue|WenetSpeech-Yue|CV-yue"
    ),
    "AGENTS.md": None,
    "tests/test_cantonese_terminology.py": None,
}

# 生成的锁文件是依赖清单（base64 哈希可能随机撞出子串），不属术语范畴
_SKIP_SUFFIX = ("package-lock.json", "pnpm-lock.yaml", "poetry.lock")


def _tracked_source_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    exts = (".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".sh", ".md", ".json", ".yaml", ".yml", ".rs")
    return [
        REPO / line
        for line in out.splitlines()
        if line.endswith(exts)
        and not line.endswith(_SKIP_SUFFIX)
        and not line.startswith(_ARCHIVE_PREFIX)
    ]


def test_no_legacy_cantonese_spelling_outside_allowlist() -> None:
    offenders: list[str] = []
    for path in _tracked_source_files():
        rel = path.relative_to(REPO).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):  # pragma: no cover - 非文本跟踪文件
            continue
        if "yue" not in text.lower():
            continue
        # 注意:豁免值是 None（整文件豁免），不能用 `get(rel) or 兜底`（None 是 falsy）。
        pattern = re.compile(r"(?!x)x") if rel not in _ALLOWLIST else _ALLOWLIST[rel]
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "yue" not in line.lower():
                continue
            if pattern is None or pattern.search(line):
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    assert not offenders, (
        "发现旧粤语拼写 yue（规范值=cantonese，见 AGENTS.md 术语铁律）。"
        "只允许出现在边界映射/DB 迁移/政策文档白名单；若确属外部接口字面量，"
        "请把它收口到单点并加入 _ALLOWLIST：\n" + "\n".join(offenders)
    )
