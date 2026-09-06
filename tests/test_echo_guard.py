"""回声自听守卫：AI speaking 中收到与自己上一句高度相似的「用户轮」→ 拦下。

场景=外放+浏览器 AEC 失效（输出设备存档失效）时 AI 自闻自答、当场打断自己
再复读一遍。借鉴 KoljaB/RealtimeVoiceChat 的 barge-in 回声门思想（他们的
isTTSPlaying 客户端上报在服务端从未被消费——这条把守卫做在文本侧）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import _echo_guard_enabled, _is_echo_self_heard  # noqa: E402
from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402


def test_speaking_and_high_similarity_blocks():
    reply = "好的，我幫你查下張單嘅運輸狀況，請你稍等一下"
    heard = "好的，我幫你查下張單嘅運輸狀況，請你稍等一下。"
    assert _is_echo_self_heard(heard, reply, agent_speaking=True) is True


def test_not_speaking_passes():
    """AI 讲完之后客户复述/引用係正常行为，唔拦。"""
    reply = "好的，我幫你查下張單嘅運輸狀況，請你稍等一下"
    heard = "你啱啱係咪話幫我查下張單嘅運輸狀況？"
    assert _is_echo_self_heard(heard, reply, agent_speaking=False) is False


def test_low_similarity_passes():
    reply = "好的，我幫你查下張單嘅運輸狀況，請你稍等一下"
    heard = "唔該你快啲啦，我趕時間收貨㗎，麻煩幫幫手"
    assert _is_echo_self_heard(heard, reply, agent_speaking=True) is False


def test_short_texts_pass():
    """短应承（<6 归一字）永唔拦——「係」「好」係高频真实回复。"""
    assert _is_echo_self_heard("係", "係", agent_speaking=True) is False
    assert _is_echo_self_heard("", "好的我幫你查下", agent_speaking=True) is False
    assert _is_echo_self_heard("好的我幫你查下", "", agent_speaking=True) is False


def test_punct_space_normalized_comparison():
    """标点/空白归一后比对——回声转写的标点差异唔构成放行理由。"""
    reply = "你嘅單號尾號七八九零，我哋已經收到咗嘞"
    heard = "你嘅單號尾號 7890 ，我哋已經收到咗嘞？"  # 数字写法不同 → 相似度唔到 0.9，放行
    assert _is_echo_self_heard(heard, reply, agent_speaking=True) is False
    heard2 = "你嘅單號尾號七八九零我哋已經收到咗嘞"
    assert _is_echo_self_heard(heard2, reply, agent_speaking=True) is True


def test_env_gate_off(monkeypatch):
    monkeypatch.setenv("QWEN3_ECHO_GUARD", "0")
    assert _echo_guard_enabled() is False
    monkeypatch.delenv("QWEN3_ECHO_GUARD", raising=False)
    assert _echo_guard_enabled() is True


def test_context_state_last_reply_property():
    """只读出口=agent 守卫取锚；空串 set 忽略（保持旧锚，同旧行为）。"""
    st = ContextState(account_id="t")
    assert st.last_reply == ""
    st.set_last_reply("第一句回复")
    assert st.last_reply == "第一句回复"
    st.set_last_reply("")
    assert st.last_reply == "第一句回复"


def test_guard_raise_sits_outside_broad_try():
    """顺序约束（源码级）：守卫的 StopResponse raise 必须喺 WhatsApp try 的
    except-pass 之前——钩子里 WA 偵測/流程推进都包住 except Exception: pass，
    StopResponse（Exception 子类）喺 try 内会被吞，守卫失效。"""
    import agent_runtime.agent as ag

    src = Path(ag.__file__).read_text(encoding="utf-8")
    guard_pos = src.index("QWEN3_ECHO_SELF_HEARD_DROP")
    wa_except_pos = src.index("except Exception:  # pragma: no cover - WhatsApp 偵測失敗唔阻斷")
    assert guard_pos < wa_except_pos, "回声守卫必须喺 WhatsApp except-pass try 之前 raise"
