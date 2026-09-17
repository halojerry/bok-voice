"""术语门禁：粤语值全时空统一小写 cantonese（AGENTS.md「Language / Terminology Rules」）。

全仓跟踪的源文件不得出现旧拼写 yue（大小写不敏感），唯以下单点豁免：
- deps.py / test_control_plane.py：DB 存量迁移（唯一兼容点）及其回归夹具
- web_search.py / test_web_search.py：维基百科外部域名 zh-yue.wikipedia.org
- interpret.py / test_interpret_tts_provider.py / agent.py / test_fixed_language_call.py：
  MiniMax language_boost 外部枚举（TTS 供应商 API 真字面量，粤=普+粤标记；
  B 线 interpret 与 A 线 entrypoint per-call 注入同源同值，值经 env 单点透传）
- test_volcano_v3.py / ARCHITECTURE.md：Volcano API dialect 枚举（外部接口字面量）
- docs/archive/**、AGENTS.md、AGENT.md、docs/CONTRACTS.md：历史档案与政策文档

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
    # MiniMax language_boost 外部枚举(API 真字面量,粤=普+粤标记);只豁免带该
    # 枚举值的行,语言字段本身仍一律 cantonese。A 线 agent.py 同源注入
    # (per-call 固定语言,B 线 interpret 同值)。
    "apps/agent/agent_runtime/interpret.py": re.compile(r"Chinese,Yue"),
    # 0913 验收探针的 MM 合成话音线同枚举(t2a_v2 language_boost 外部字面量)。
    "scripts/acceptance_0913_scenarios.py": re.compile(r"Chinese,Yue"),
    "scripts/mm_voice.py": re.compile(r"Chinese,Yue"),
    "apps/agent/agent_runtime/agent.py": re.compile(r"Chinese,Yue"),
    "tests/test_interpret_tts_provider.py": re.compile(r"Chinese,Yue"),
    "tests/test_fixed_language_call.py": re.compile(r"Chinese,Yue"),
    "scripts/test_volcano_v3.py": None,
    # 计划文档里的粤语文本检测示例代码（_YUE_MARKS 正则只是「识别粤语字」的
    # 变量名，非语言字段赋值）——4b0dd6b 存量，按门禁政策文档白名单收口。
    "docs/superpowers/plans/2026-09-17-qa-canvas-phase1.md": re.compile(r"_YUE_MARKS"),
    # MiniMax ASR BCP-47 语言头外部枚举(粤语=yue,asr-1.0 /v1/speech_to_text 真字面量,
    # 同 language_boost 政策):只豁免带该枚举值的行,探针内部语言字段一律 cantonese。
    "scripts/probe_cloud_asr_ab.py": re.compile(r'"yue"'),
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
    "scripts/probe_smart_turn.py": re.compile(r"yue"),
    # P0 真库烟测做 yue→cantonese 数据迁移演练(铺旧值行验证 build_engine 改写)，
    # 同 deps.py 类：旧拼写是演练夹具非运行时语言字段，按行豁免。
    "scripts/smoke_postgres.py": re.compile(r"yue"),
    "docs/ARCHITECTURE.md": re.compile(r"VOLC_DIALECT"),
    "AGENTS.md": None,
    "AGENT.md": None,
    "docs/CONTRACTS.md": None,
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
