"""E7 离线润色面的**接线单点**（2026-09-21）：kill-switch + fail-soft。

``polish.polish_text`` 本体是纯函数（零 I/O、零 env，模块 docstring 的纯函数承诺
不动）；本模块是那层「脏」的：**唯一**一处读 ``os.environ``、**唯一**一处
try/except，供三个**延迟不敏感**的离线消费者共用（§31.6 队列 ③ 的三处落点）：

1. 挂断后纪要输入——``control_plane.summarize.Summarizer._render_transcript``
   （喂 LLM 的 prompt 文本；**不是** ``_write_settlement_docs`` 落盘的
   ``transcript.md`` 原件，那是原始证据面，逐字不碰）；
2. QA 挖掘输入——``bok_voice_core.qa_text.mine_qa_pairs``（CP
   ``/api/reports/qa-pairs`` 与 ``control_plane/qa_cluster.py`` 共用同一份纯函数，
   故这是该面的单点）；
3. L-① 漏网轮挖掘输入——``control_plane.gap_mining.build_llm_gap_report``。

**绝不进实时轮**（§26.2-E7 明文）：A 线 agent worker **不 import 本模块**
（``tests/test_polish_wiring.py`` 结构化锚钉住），``BOK_POLISH_OFFLINE`` 也因此
**不进** ``tools/bok.py::_FORWARD_ENV``——那张表是 **A 线 agent worker 的封闭 env
面**。它走 ``bok._control_plane_env``（CP 面，与 ``BOK_SETTLE_LLM_*`` 同款显式
注入）：不注入的话，prod launchd/schtasks 的封闭 env 面里这枚开关是**死门**
（AGENTS.md 记过两次同款实弹教训）。

## kill-switch 与默认值（默认**关**）

``BOK_POLISH_OFFLINE`` 读作 ``== "1"``（仓规），未设/其它值 = 关。为什么默认关，
2026-09-21 实测（数字见 ``tests/test_polish_wiring.py`` 的文件头与交付报告）：

- 它改的是**喂进挖掘与纪要的文本**，而挖掘答案一旦被人工采纳就**罐头化**，
  下一通真实通话照播——默认开 = 未经真栈 A/B 就改生产话术面，与「保守自动」
  纪律相左（``qa_text`` 的自动入库闸「错答案一旦罐头化就是复读机」同一立场）。
- **这是「先量再判」抓到的**：首轮真库实测（7744 条 A 线转写）发现
  ``collapse_repetitions`` 会吃掉**粘着字母或连成一串的数字**（``MT3000 → MT30``、
  ``soak111 → soak1``）与英文词尾（``exceeded → exceed``），而 Guard 的硬保护
  token 面（``output_guard._NUMBER_RE`` 只认「两侧皆非字母数字」的整数串）**静默
  放行**这类破坏。该项**已修**（三枚重复正则加 ASCII 两侧边界，见 ``polish.py``
  与回归判例 ``test_ascii_runs_never_collapsed``）：修后真库改动 632 → **625 条
  （8.1%）**，R1 标注语料 152 轮改动 **48 轮（31.6%）**不变。
  —— 即「默认关」的现行理由回到上一条（未做真栈 A/B），不再是连带风险。
- 结论：**默认关 = 现状逐字节零变化**（未设时不进任何一步）；要看效果就
  ``BOK_POLISH_OFFLINE=1`` 起 CP，在真实挖掘/纪要上比对一轮再决定要不要转默认
  ——旋钮已就位，转默认不需要改码。

``polish_text`` 出口自带 E3 Guard（``GuardPolicy(exempt_digit_addition=True)``）：
拒绝即回退原文（``PolishResult.text`` 契约）；本层再叠一层 try/except——**两层
fail-soft**，消费者永远拿到「可用的字符串」，清洗绝不炸纪要/挖掘。
"""

from __future__ import annotations

import os

from .polish import polish_text

__all__ = ["POLISH_OFFLINE_ENV", "polish_offline_enabled", "polish_offline_text"]

#: kill-switch 键名（CP 面 env；见模块 docstring「绝不进实时轮」）。
POLISH_OFFLINE_ENV = "BOK_POLISH_OFFLINE"


def polish_offline_enabled() -> bool:
    """kill-switch：只有显式 ``BOK_POLISH_OFFLINE=1`` 才开（未设/其它值 = 关）。"""
    return os.environ.get(POLISH_OFFLINE_ENV) == "1"


def polish_offline_text(text: str) -> str:
    """离线面润色出口（唯一接线入口）：关 → 逐字原样；开 → 确定性润色；异常 → 原文。

    入参宽容（``None`` / 非 str 照 ``polish._as_text`` 口径收敛成 str），返回恒为
    ``str``。**纯读**：不改写入参、无落库、无网络。
    """
    body = "" if text is None else str(text)
    if not body or not polish_offline_enabled():
        return body
    try:
        return polish_text(body).text
    except Exception:  # noqa: BLE001 - 离线清洗绝不允许炸纪要/挖掘（fail-soft 同邻码纪律）
        return body
