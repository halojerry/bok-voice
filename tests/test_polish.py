"""E7 离线润色面纯函数矩阵（core，2026-09-21）。

契约：``bok_voice_core.polish``（type4me ``formalWritingPromptTemplate`` 的**确定性**
子面 + E3 Guard 出口；§26.2-E7 / §26.3）。本文件只测纯函数面，不碰接线。

两条被测试钉住的裁决（交付报告会写明）：
1. **数字铁律优先**：报号串**整句短路**（``is_number_reporting`` 主动进流水，逐字返回原文）。
   ⚠️ 2026-09-21 修正：这句先前写作「结构防线 + 语境防线」，但真库实测（3063 条真实
   客户轮）证明**折叠重复能绕过那两道防线** —— ``四三二零一一一。→ 四三二零一。``
   （69 轮数字面被改，含 WhatsApp 报号串，且 ``is_number_reporting`` 当时是**无人调用
   的死保险**）。现改为整句短路这**唯一**一条保险，判例见文件末尾 ``_REAL_NUMBER_TURNS``。
   连「不是12345，是67890」这类数字改口也因 Guard 硬保护 token 检查而**回退原文**
   ——宁可退原文，不丢报号数字。
2. **Guard/polish 参数化调解**：润色面走 ``GuardPolicy(exempt_digit_addition=True)``
   ——豁免数字新增（2300/15%/3:30），保留硬保护/语言/硬否定/敏感新增。

术语：语言相关字面量只用 zh / cantonese / en（AGENTS.md 术语铁律）。
"""

from __future__ import annotations

import pytest

from bok_voice_core.output_guard import (
    REASON_EMPTY,
    REASON_FABRICATED_FACT,
    REASON_PROTECTED_TOKEN_LOST,
    GuardPolicy,
    guard_output,
)
from bok_voice_core.polish import (
    PolishPolicy,
    collapse_repetitions,
    fold_self_correction,
    is_number_reporting,
    normalize_numbers,
    remove_fillers,
    polish_text,
)


# ---- 1. 删口水词（独立段才删） ----


@pytest.mark.parametrize(
    "original,expected",
    [
        ("呃，我要回复什么？", "我要回复什么？"),
        ("嗯，对的", "对的"),
        ("啊，原来是这样", "原来是这样"),
        ("哦，可以啊", "可以啊"),
        ("唔该帮我查下", "唔该帮我查下"),  # 「唔」嵌在词内（后接 CJK）→ 不删
        ("好啊，就这样", "好啊，就这样"),  # 「啊」嵌在词内 → 不删
        ("um, I think so", "I think so"),
        ("hmm, okay", "okay"),
        ("I am, uh, not sure", "I am, not sure"),
    ],
)
def test_remove_fillers(original, expected):
    # 经 ``polish_text`` 出口断言（remove_fillers 单步会留标点残渣，收尾整理在出口）。
    assert polish_text(original).text == expected


def test_filler_run_removed_as_unit():
    assert polish_text("呃呃呃，你好").text == "你好"


def test_remove_fillers_raw_leaves_punctuation():
    # 单步函数只删词素、不整标点（职责分离；残标点由 ``_tidy`` 收尾）。
    assert remove_fillers("嗯，对的") == "，对的"
    assert remove_fillers("我 呃 说") == "我  说"


# ---- 2. 折叠重复（强调与构词不折） ----


@pytest.mark.parametrize(
    "original,expected",
    [
        ("对的，对的", "对的"),
        ("这个这个单号", "这个单号"),
        ("没事没事", "没事"),
        ("谢谢谢谢", "谢谢"),
        ("签字！签字！签字！", "签字！签字！签字！"),  # 感叹号强调 → 不折
        ("看看这个", "看看这个"),  # 汉语双字叠词（构词）→ 不折
        ("我想想", "我想想"),
        ("我我我", "我"),  # ≥3 连同一单字 → 折
        ("哈哈哈哈", "哈哈哈哈"),  # 笑声 → 不折
    ],
)
def test_collapse_repetitions(original, expected):
    assert collapse_repetitions(original) == expected


# ---- 3. 改口取后值 / 丢弃废弃半句（Polish 口径） ----


@pytest.mark.parametrize(
    "original,expected",
    [
        ("不是明天，是后天", "后天"),
        ("不是A，而是B", "B"),
        ("不对，是张三", "张三"),  # 标记折叠 + 剥连接词「是」
        ("算了，我明天再说", "我明天再说"),
        ("我今天不是来投诉的", "我今天不是来投诉的"),  # 合法否定（无对比结构）→ 不动
        ("我的地址不是这里，是前面那栋", "前面那栋"),
    ],
)
def test_fold_self_correction(original, expected):
    assert fold_self_correction(original) == expected


def test_fold_no_cut_when_marker_is_sentence_final():
    # 标记在句末、后面没内容 → 不折（否则整句被清空）。
    assert fold_self_correction("那就算了") == "那就算了"


# ---- 4. 口语数字规范化（含成语/惯用形黑名单） ----


@pytest.mark.parametrize(
    "original,expected",
    [
        ("两千三百", "2300"),
        ("两万三千", "23000"),
        ("一千零二十", "1020"),
        ("十五分钟", "15分钟"),
        ("百分之十五", "15%"),
        ("百分之一", "1%"),
        ("三点半", "3:30"),
        ("三点一刻", "3:15"),
        ("三点十五分", "3:15"),
        ("三点五分", "3:05"),
        # 惯用形/成语黑名单 → 不动
        ("十万火急", "十万火急"),
        ("一五一十地说", "一五一十地说"),
        ("十分感谢", "十分感谢"),
        ("十分开心", "十分开心"),
        ("万一他来了", "万一他来了"),
        ("千万别这样", "千万别这样"),
    ],
)
def test_normalize_numbers(original, expected):
    assert normalize_numbers(original) == expected


def test_normalize_numbers_ignores_bare_single_digit_runs():
    # 无级量词 = 不成数值式（单字连读天然不被匹配）。
    assert normalize_numbers("三七七八九零") == "三七七八九零"
    assert normalize_numbers("一二三") == "一二三"


# ---- 5. 数字铁律（结构性 + 语境性双防线） ----


@pytest.mark.parametrize(
    "original",
    [
        "我个单号系三七七八九零，唔该帮我查下",
        "我的单号是三千七百块",  # 号码关键词前窗 → 数词不动
        "单号12345",  # ASCII 数字串结构性不匹配
        "我的手机是13800138000",
        "我的WhatsApp係六四三二零一一一",
        "订单号是１２３４５６",  # 全角数字串
    ],
)
def test_digit_iron_rule_untouched(original):
    assert normalize_numbers(original) == original


def test_mixed_utterance_normalizes_value_but_keeps_report():
    out = normalize_numbers("我买了两千三百块，单号12345")
    assert out == "我买了2300块，单号12345"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我的单号是12345", True),
        ("我个单号系三七七八九零", True),
        ("我的手机号13800138000", True),
        ("我买了两千三百块", False),
        ("你好，请问是张三吗", False),
    ],
)
def test_is_number_reporting(text, expected):
    assert is_number_reporting(text) is expected


# ---- 6. Guard 出口：豁免数字新增、保留硬保护（参数化调解的被测属性） ----


def test_guard_reconciliation_exemption_is_load_bearing():
    source = "单号12345，买了两千三百块"
    polished = normalize_numbers(source)
    assert polished == "单号12345，买了2300块"
    # 不豁免：新增 2300 被判编造事实 → 拒（这就是「必须参数化」的证据）。
    assert guard_output(source, polished).reason == REASON_FABRICATED_FACT
    # 豁免：同一个候选通过（保留硬保护/语言/硬否定等其余闸）。
    assert (
        guard_output(
            source, polished, policy=GuardPolicy(exempt_digit_addition=True)
        ).accepted
        is True
    )


def test_polish_end_to_end_accepts_digit_normalization():
    """数量词归一仍生效——但它**只在不含报号串的轮上**发生。

    含 4 位以上数字串/号码关键词的轮走整句短路（下面一条判例），故这条改用纯数量句：
    口语量词照归一到阿拉伯数字（2300），这是设计内的授权改写（Guard 豁免数字新增）。
    """
    result = polish_text("呃，我买了两千三百块")
    assert result.guard.accepted is True
    assert result.text == "我买了2300块"
    assert "numbers" in result.applied
    # 混着报号的轮 → 整句短路，数量词也一并让位（宁可少洗，不可改号）
    mixed = polish_text("单号12345，我买了两千三百块")
    assert mixed.text == "单号12345，我买了两千三百块"
    assert mixed.guard.reason == "number-reporting"


def test_polish_number_self_correction_falls_back_to_original():
    """数字改口：落地恒为原文（铁律：不丢数字）——**由报号短路保证**。

    短路是第一道也是唯一一道（2026-09-21 起）：``不是12345，是67890`` 含 4 位数字 run
    → 整句短路直接返回原文。第二层「Guard 硬保护 token」仍在，供**没有**触发短路的
    改口式句子兜底——用 ``skip_number_reporting=False`` 把那一层单独钉住。
    """
    result = polish_text("不是12345，是67890")
    assert result.text == "不是12345，是67890"
    assert result.guard.reason == "number-reporting"
    assert result.applied == ()

    # 第二层单独验：关掉短路，折叠仍会丢原报号串 → Guard 硬保护 token 检查拒 → 回退原文。
    layered = polish_text("不是12345，是67890", policy=PolishPolicy(skip_number_reporting=False))
    assert layered.guard.rejected is True
    assert layered.guard.reason == REASON_PROTECTED_TOKEN_LOST
    assert layered.polished == "67890"  # 润色稿留档
    assert layered.text == "不是12345，是67890"


def test_polish_hard_protection_keeps_url():
    # 人为构造：折叠把 URL 丢了 → Guard 拒 → 回退原文。
    result = polish_text("不是 https://a.example.com/x，是别的")
    assert result.guard.rejected is True
    assert result.text == "不是 https://a.example.com/x，是别的"


# ---- 7. 出口契约 / 策略开关 / 纯函数 ----


def test_polish_result_shape():
    result = polish_text("呃，我要回复什么？")
    assert result.text == result.polished == "我要回复什么？"
    assert "fillers" in result.applied
    assert result.guard.accepted is True


def test_policy_can_disable_steps():
    off = PolishPolicy(normalize_numbers=False, drop_fillers=False, collapse_repeats=False,
                       fold_corrections=False)
    assert polish_text("呃，两千三百，对的，对的", policy=off).text.startswith("呃，两千三百")


def test_policy_guard_off_skips_output_guard():
    # guard=False：不做后验，直接采纳润色稿（供调用方自行裁决；默认永远挂 Guard）。
    # ⚠️ 报号短路是**前置安全条件**、不是 Guard：不受 guard 开关影响，故本判例要显式
    # 关掉它才测得到「无后验」这一层（用含数字 run 的句子正好说明这个次序）。
    off = PolishPolicy(guard=False, skip_number_reporting=False)
    result = polish_text("不是12345，是67890", policy=off)
    assert result.text == "67890"
    assert result.guard.detail == "guard disabled"
    # 短路照常先行（guard=False 也挡不住）
    assert polish_text("不是12345，是67890", policy=PolishPolicy(guard=False)).guard.reason == (
        "number-reporting"
    )


def test_empty_after_polish_falls_back_to_original():
    # 「嗯。」被口水词轨清空 → Guard 空输出拒 → 回退原文（不产出空稿）。
    result = polish_text("嗯。")
    assert result.guard.reason == REASON_EMPTY
    assert result.text == "嗯。"


def test_polish_is_pure_and_deterministic():
    text = "呃，我买了两千三百块，单号12345，对的，对的"
    first = polish_text(text)
    second = polish_text(text)
    assert first == second
    assert text == "呃，我买了两千三百块，单号12345，对的，对的"  # 入参不改写


def test_polish_idempotent_on_clean_text():
    clean = "你好，请问是张三吗"
    assert polish_text(clean).text == clean


# ---- 报号短路（2026-09-21 真库实测回归） ----------------------------------------
# 下面这 6 条**逐字取自真库**（turns: line='a', role='user'），是 3063 条真实客户轮里
# 被「折叠重复」改掉数字的那一批。前 4 条是客户在念号码（含 WhatsApp 报号），
# 旧档会把中间重复的数字吃掉——`四三二零一一一。→ 四三二零一。` 在真库里出现 4 次。
_REAL_NUMBER_TURNS = (
    "四三二零一一一。",
    "六四三二零一一一。",
    "四五六五六五六七。",
    "一三四四四三五是。",
    "我的微信是一三四五五七七八。",
    "四五六五六五六七。好的",
)


@pytest.mark.parametrize("text", _REAL_NUMBER_TURNS)
def test_number_reporting_turn_returned_verbatim(text):
    """报号串整句短路：逐字返回原文、零步生效、原因码可归因。"""
    result = polish_text(text)
    assert result.text == text
    assert result.applied == ()
    assert result.guard.reason == "number-reporting"
    assert result.polished == text


@pytest.mark.parametrize("text", _REAL_NUMBER_TURNS)
def test_without_short_circuit_the_digits_are_eaten(text):
    """A/B 归因（关掉短路=回旧档）：真库那批破坏确实来自「折叠重复」。

    这是**缺陷留档**而非正确性断言——它钉住「旧档会吃号码」，所以短路不能被删。
    不吞数字的样本（如「我的微信是…」）在旧档下本来就不变，跳过。
    """
    old = polish_text(text, policy=PolishPolicy(skip_number_reporting=False))
    if old.text != text:
        assert "repeats" in old.applied
        assert old.text != text


def test_short_circuit_does_not_over_block_ordinary_cleaning():
    """短路只挡报号串：普通客户话照洗，数量词归一照做（不是一刀切停摆）。"""
    assert polish_text("啊，你好，听得到我声音吗？").text == "你好，听得到我声音吗？"
    # 数量型词（非报号）仍归一——`is_number_reporting` 对「两百块」为假。
    assert polish_text("啊，两百块。").text == "200块。"
    assert is_number_reporting("两百块。") is False
