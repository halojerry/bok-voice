"""E3 输出后验 Guard 纯函数矩阵（core，2026-09-21）。

契约：``bok_voice_core.output_guard``（type4me ``IntelliSenseOutputValidator`` ＋
``ProtectedFactExtractor`` 移植；§26.2-E3 / §26.3 参数表）。本文件只测纯函数面，
不碰接线。

术语：语言相关字面量只用 zh / cantonese / en（AGENTS.md 术语铁律）。
"""

from __future__ import annotations

import pytest

from bok_voice_core.output_guard import (
    NEGATION_HARD_KEYS,
    NEGATION_SOFT_KEYS,
    REASON_EMPTY,
    REASON_EXPANSION,
    REASON_FABRICATED_FACT,
    REASON_FENCE,
    REASON_LANGUAGE_DRIFT,
    REASON_NEGATION_DRIFT,
    REASON_PROTECTED_TOKEN_LOST,
    REASON_SENSITIVE_ADDITION,
    REASON_TOOL_CALL,
    GuardPolicy,
    apply_guard,
    contains_sensitive_content,
    guard_output,
    protected_tokens,
)


# ---- 0. 通过基线 ----


@pytest.mark.parametrize(
    "original,candidate",
    [
        ("你好，请问是张三吗？", "你好，请问是张三吗？"),
        ("我的单号是12345", "我的单号是12345"),  # 数字原样保留 = 通过
        ("咱们约周三下午", "咱们约周三下午"),  # 星期 token 原样
        ("我买了两千三百块", "我买了2300块"),  # 无原数字 token → 不触发编造
        ("不是A，是B", "B"),  # contradiction 只告警 → 通过
        ("见 https://a.example.com/x", "见 https://a.example.com/x"),
    ],
)
def test_accepts_clean_transformations(original, candidate):
    result = guard_output(original, candidate)
    assert result.accepted is True
    assert result.reason == ""
    assert result.final_text == candidate
    assert bool(result) is True


def test_result_shape_distinguishes_reject_from_accept():
    ok = guard_output("你好", "您好")
    bad = guard_output("你好", "")
    assert (ok.accepted, ok.reason) == (True, "")
    assert bad.accepted is False and bad.reason
    # candidate 永远留档（拒绝也不丢）
    assert bad.candidate == ""
    assert bad.original == "你好"
    assert bad.rejected is True


def test_apply_guard_returns_candidate_or_original():
    assert apply_guard("你好", "您好") == "您好"
    assert apply_guard("你好", "") == "你好"


# ---- 1. 空输出 / 围栏 / tool_call（结构卫生） ----


@pytest.mark.parametrize("candidate", ["", "   ", "\n\t", "\u3000"])
def test_empty_candidate_rejected(candidate):
    assert guard_output("你好", candidate).reason == REASON_EMPTY


@pytest.mark.parametrize("candidate", ["```x```", "看这个 ```python", "```"])
def test_fence_rejected(candidate):
    assert guard_output("你好", candidate).reason == REASON_FENCE


@pytest.mark.parametrize(
    "candidate", ["<tool_call>", "</tool_call>", "< tool_calls >", "<function_call>"]
)
def test_tool_call_marker_rejected(candidate):
    assert guard_output("你好", candidate).reason == REASON_TOOL_CALL


# ---- 2. 扩张上限 max(3N, N+120)（§26.3） ----


def test_expansion_boundary_small_n():
    original = "原" * 10
    cap = max(3 * 10, 10 + 120)  # = 130
    assert guard_output(original, "原" * cap).accepted is True
    rejected = guard_output(original, "原" * (cap + 1))
    assert rejected.reason == REASON_EXPANSION


def test_expansion_boundary_large_n_uses_3n():
    original = "原" * 200
    cap = max(3 * 200, 200 + 120)  # = 600（3N 分支胜出）
    assert guard_output(original, "原" * cap).accepted is True
    assert guard_output(original, "原" * (cap + 1)).reason == REASON_EXPANSION


def test_expansion_pivot_at_n_60():
    # N=60：3N=180 与 N+120=180 相等（两分支交点）。
    original = "原" * 60
    assert guard_output(original, "原" * 180).accepted is True
    assert guard_output(original, "原" * 181).reason == REASON_EXPANSION


# ---- 3. 语言漂移（§26.3） ----


@pytest.mark.parametrize(
    "original,candidate",
    [
        ("你好请问在吗", "hello hi there"),  # 原 CJK≥4 → 候选 CJK=0
        ("这是中文的内容", "abcdefgh"),  # 双侧 total≥8 且 1.0↔0.0 交叉
        ("abcdefgh", "这是中文的内容啊"),  # 反向交叉（拉丁→CJK）
    ],
)
def test_language_drift_boundaries(original, candidate):
    assert guard_output(original, candidate).reason == REASON_LANGUAGE_DRIFT


def test_same_language_no_drift():
    assert guard_output("一二三四五六七八", "一二三四五六七八九十").accepted is True


def test_short_cjk_below_threshold_not_drift():
    # 原 CJK=3 <4 且双侧 total <8 → 不判漂移（阈值边界）。
    assert guard_output("你好吗", "hi").accepted is True


def test_language_drift_ratio_crossing_needs_both_totals_8():
    # 原 total=3 <8 → 两个分支都不触发（第一分支 CJK=3<4）。
    assert guard_output("你好吗", "hi").accepted is True


# ---- 4. 硬否定：仅 prohibition / never 计数相等（§26.3） ----


def test_hard_negation_keys_are_prohibition_and_never():
    assert NEGATION_HARD_KEYS == ("prohibition", "never")
    assert set(NEGATION_SOFT_KEYS) == {
        "inability",
        "absence",
        "contradiction",
        "general",
    }


@pytest.mark.parametrize(
    "original,candidate",
    [
        ("你不要告诉他", "你告诉他"),  # prohibition 1→0
        ("请勿触碰", "触碰"),  # prohibition 1→0
        ("你从不迟到", "你迟到"),  # never 1→0
        ("他不知道", "他不知道，不要告诉他"),  # prohibition 0→1（新增也拒）
    ],
)
def test_hard_negation_drift_rejected(original, candidate):
    assert guard_output(original, candidate).reason == REASON_NEGATION_DRIFT


@pytest.mark.parametrize(
    "original,candidate",
    [
        ("不是A，是B", "B"),  # contradiction（软类）
        ("我没有购买", "我购买了"),  # absence（软类）
        ("我不能去", "我可以去"),  # inability（软类）
        ("不记得了", "忘了"),  # general（软类）
    ],
)
def test_soft_negation_drift_only_warns(original, candidate):
    result = guard_output(original, candidate)
    assert result.accepted is True
    assert any(w.startswith("soft_negation_drift") for w in result.warnings)


# ---- 5. 硬保护 token 丢失（整 token 完全匹配） ----


def test_url_loss_rejected():
    result = guard_output("见 https://a.example.com/x 页面", "见某网站页面")
    assert result.reason == REASON_PROTECTED_TOKEN_LOST


@pytest.mark.parametrize(
    "original,candidate",
    [
        ("我的单号是12345", "我的单号是123456"),  # 整 token 不匹配 → 丢
        ("手机号13800138000", "手机号一三八零零一三八零零零"),
        ("约在周三", "约在明天"),  # 星期
        ("邮箱 a@b.com", "邮箱 a-at-b.com"),
        ("路径 /var/log/app", "路径某目录"),
    ],
)
def test_protected_token_loss_matrix(original, candidate):
    assert guard_output(original, candidate).reason == REASON_PROTECTED_TOKEN_LOST


def test_protected_tokens_extraction_shape():
    toks = protected_tokens("见 https://a.com/b 邮件 a@b.co 路径 /x/y 数字 12 周三")
    assert "https://a.com/b" in toks
    assert "a@b.co" in toks
    assert "12" in toks
    assert "周三" in toks


def test_protected_token_present_when_candidate_keeps_it():
    assert guard_output("我的单号是12345", "好的，我的单号是12345").accepted is True


# ---- 6. 敏感新增（只在「引入」时拒） ----
# 注：本节夹具含凭据形状的串（`api_key: …` 等）——这是**功能使然**：本模块的职责就是
# 检出这类 token，负向判例必须真的给一个。`sk-abcdef123456` 因此被 gitleaks 的
# generic-api-key 命中，已按 .gitleaks.toml 的既定程序**逐条精确登记**（该文件前言第三条
# 有说明）。**勿为躲门禁把它改成「更高熵的形状」**——那条路只会让夹具更像真凭据。


@pytest.mark.parametrize(
    "addition",
    [
        "api_key: sk-abcdef123456",
        "api-key=abcdef1234",
        "secret: hunter2hunter2",
        "access_token: abcdefghijkl",
        "password=MyPassw0rd!",
        "Authorization: Bearer abcdefghijklmnop",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_sensitive_addition_rejected(addition):
    # 原始用纯拉丁（CJK=0）避免先被语言漂移拦下，专测敏感新增这一条。
    original = "please send me the details"
    assert guard_output(original, original + " " + addition).reason == (
        REASON_SENSITIVE_ADDITION
    )


def test_sensitive_not_rejected_when_already_present():
    original = "配置里写 api_key: sk-abcdef123456 对吧"
    assert guard_output(original, original).accepted is True
    assert contains_sensitive_content(original) is True


def test_contains_sensitive_content_negative():
    assert contains_sensitive_content("普通文本没有任何凭据") is False


# ---- 7. 编造数字事实 + E7 豁免（§26.2-E3 末条 / §26.2-E7 参数化） ----


def test_fabricated_numeric_token_rejected():
    original = "单号12345"
    candidate = "单号12345，共2300元"
    assert guard_output(original, candidate).reason == REASON_FABRICATED_FACT


def test_fabricated_requires_original_protected_token():
    # 输入本无保护 token → 不判编造（§26.2-E3「且输入本有保护 token」）。
    assert guard_output("我买了两千三百块", "我买了2300块").accepted is True


def test_exempt_digit_addition_accepts_normalization():
    policy = GuardPolicy(exempt_digit_addition=True)
    result = guard_output("单号12345，买了两千三百块", "单号12345，买了2300块", policy=policy)
    assert result.accepted is True


def test_exempt_digit_addition_still_protects_loss():
    # 豁免的只是「新增数字」，**丢数字照拒**（硬保护 token 检查保留）。
    policy = GuardPolicy(exempt_digit_addition=True)
    result = guard_output("单号12345，买了两千三百块", "买了2300块", policy=policy)
    assert result.reason == REASON_PROTECTED_TOKEN_LOST


def test_exempt_digit_addition_relaxes_expansion_budget():
    # 豁免时两侧按「剥数字后长度」计长：纯数字新增不吃长度预算。
    original = "原" * 10  # 剥数字后仍是 10
    candidate = "原" * 130 + "9" * 300  # 剥数字后 130，不超 max(30,130)
    policy = GuardPolicy(exempt_digit_addition=True)
    assert guard_output(original, candidate, policy=policy).accepted is True
    # 不豁免时 430 > 130 → 扩张拒。
    assert guard_output(original, candidate).reason == REASON_EXPANSION


def test_default_policy_is_no_exemption():
    assert GuardPolicy().exempt_digit_addition is False


def test_list_marker_not_fabricated():
    # 行首列表标记 "1. " 不算编造（§26.2-E3「非行首列表标记」）。
    original = "单号12345"
    candidate = "单号12345\n1. 明天联系\n2. 后天联系"
    assert guard_output(original, candidate).accepted is True


# ---- 8. 纯函数性质 ----


def test_pure_and_deterministic():
    original = "我的单号是12345，周二送达"
    candidate = "我的单号是12345，周三送达"
    first = guard_output(original, candidate)
    second = guard_output(original, candidate)
    assert first == second
    assert original == "我的单号是12345，周二送达"  # 入参不被改写


def test_policy_does_not_leak_between_calls():
    a = guard_output("单号12345，两千三百块", "单号12345，2300块")
    b = guard_output(
        "单号12345，两千三百块",
        "单号12345，2300块",
        policy=GuardPolicy(exempt_digit_addition=True),
    )
    assert a.rejected and b.accepted
