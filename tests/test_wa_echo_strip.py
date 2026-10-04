"""M-23(2026-09-23 修复波#4):en 报号首位数字回声 → 错号被确认,双闸修复。

task-4 M2 实弹三例(en 场景通话,原值钉死):
- call-960bd5bd:刺激 "one two three four five six"(123456)→ ASR 听成
  「One, one, two, three, four, five, six.」= 1123456 → 复述确认错号;
- call-c35453f1:刺激 34567 → 334567 → 复述确认错号;
- call-f2dc0e7a:刺激 6432543 → 66432543(8位)→ 直接捕获+复述确认错号。

双闸:
1. **号长期望 en 收紧**(复述确认闸,不挡捕获):en 原本 None(宽松不校验)
   → 8 位港号(852+8=11 亦收,与粤线同款)。1123456/334567 两例在确认层被
   拦下改请重讲;66432543(8位,长度合法)确认层拦不住=如实残余。
2. **首位回声剥离**(`_strip_number_echo`,累积边界):复述确认后的新数字串
   以「上一轮已确认串/AI last_reply 数字串」为**严格前缀且更长**(回声+续号
   形状)→ 剥前缀再累积/侦测;相等或更短不动(客户重申同一号码是合法行为,
   绝不毁真内容)。首位「首位数字双叠」本身 ASR 文本层无可靠判据(真号可双叠
   开头,如 6643…)——不盲剥,如实残余,靠确认闸+复述措辞兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("apps/agent", "packages/core"):
    sp = str(ROOT / p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from _bok_src import bok_source  # noqa: E402

from agent_runtime.agent import (  # noqa: E402
    _strip_number_echo,
    _wa_confirm_or_reask,
    _wa_len_expected,
)

# ---- 闸 1:en 号长期望(task-4 三例原值钉) ----


def test_en_len_expectation_now_8():
    assert _wa_len_expected("en") == 8


def test_t4_case_1123456_reask():
    # task-4 call-960bd5bd 原值:7 位错号不再被复述确认
    out = _wa_confirm_or_reask("en", "1123456")
    assert "1123456" not in out
    assert "again" in out.lower()


def test_t4_case_334567_reask():
    # task-4 call-c35453f1 原值:6 位错号不再被复述确认
    out = _wa_confirm_or_reask("en", "334567")
    assert "334567" not in out
    assert "again" in out.lower()


def test_t4_case_66432543_len_plausible_confirmed():
    # task-4 call-f2dc0e7a 原值:8 位长度合法 → 确认层放行(复述措辞自带核对
    # 出口 "If that's right");长度闸管不了内容错,如实残余
    out = _wa_confirm_or_reask("en", "66432543")
    assert "66432543" in out


def test_en_852_prefixed_11_digits_accepted():
    # 与粤线同款:852+8=11 位照收
    out = _wa_confirm_or_reask("en", "85298765432")
    assert "85298765432" in out


# ---- 闸 2:首位回声剥离(纯函数) ----


def test_strip_reported_number_prefix_extension():
    # 已确认 1123456 后,新段数字以其为前缀且更长(回声+续号)→ 剥前缀
    out = _strip_number_echo(
        "one one two three four five six seven eight",
        reported={"1123456"},
        last_reply="",
    )
    from agent_runtime.flow import _digit_normalize

    assert "112345678" not in _digit_normalize(out).replace(" ", "")
    assert "78" == "".join(ch for ch in _digit_normalize(out) if ch.isdigit())


def test_strip_last_reply_readback_echo():
    # AI 复述「I've noted your number 6432543」被麦克风回听,混进客户新报号首位
    out = _strip_number_echo(
        "Six, four, three, two, five, four, three, two.",
        reported=set(),
        last_reply="Got it — I've noted your number 6432543. If that's right…",
    )
    from agent_runtime.flow import _digit_normalize

    digits = "".join(ch for ch in _digit_normalize(out) if ch.isdigit())
    assert digits == "2"


def test_equal_or_shorter_run_untouched():
    from agent_runtime.flow import _digit_normalize

    # 客户重申同一号码(相等)→ 不剥(合法行为,绝不毁真内容)
    out = _strip_number_echo(
        "six four three two five four three",
        reported={"6432543"},
        last_reply="",
    )
    assert out == "six four three two five four three"  # 原样保留(词形未动)
    assert "6432543" == "".join(ch for ch in _digit_normalize(out) if ch.isdigit())
    # 新段比已确认串更短(碎片)→ 不剥
    out2 = _strip_number_echo("six four three", reported={"6432543"}, last_reply="")
    assert "6432543" not in out2  # 原样保留(碎片走既有 <8 累积/4 位门语义)


def test_no_echo_sources_untouched():
    from agent_runtime.flow import _digit_normalize

    # 双叠开头真号(6643…)无回声源 → 原样(不盲剥首位双叠的诚实边界)
    out = _strip_number_echo(
        "Six six four three two five four three.",
        reported=set(),
        last_reply="",
    )
    assert "66432543" == "".join(ch for ch in _digit_normalize(out) if ch.isdigit())


def test_strip_normal_digit_text_untouched():
    from agent_runtime.flow import _digit_normalize

    # 开头即号码、但不含任何回声源前缀 → 原样保留全部数字
    out = _strip_number_echo(
        "Nine eight seven six five four three two",
        reported={"1123456"},
        last_reply="",
    )
    assert "98765432" == "".join(ch for ch in _digit_normalize(out) if ch.isdigit())


# ---- 接线(env 立法/边界位次,源级断言) ----


def test_wiring_and_forward_env():
    src = (ROOT / "apps/agent/agent_runtime/agent.py").read_text(encoding="utf-8")
    assert 'os.environ.get("BOK_WA_ECHO_STRIP", "1") == "1"' in src  # 默认开
    bok_src = bok_source()
    assert '"BOK_WA_ECHO_STRIP"' in bok_src  # env 立法:进 _FORWARD_ENV
    # 剥离点在 WA 累积块之前(累积与侦测都吃干净文本)——打真调用点
    # (`_echo_free = …` 唯一赋值位);裸 `_strip_number_echo(` index 会命中
    # 模块级 def 行=恒真空断言(M-8,2026-09-24 评审返工)。
    assert src.count("_echo_free = _strip_number_echo(") == 1  # 调用点唯一性自证
    i_call = src.index("_echo_free = _strip_number_echo(")
    i_accum = src.index("if _WA_ACCUM_ENABLED and flow_ctrl.has_steps:")
    assert i_call < i_accum
