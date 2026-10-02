"""D6 纯 ack 族不进史（2026-09-30 多轮上下文计划 Phase 3）。

病灶：`_say_script`→`session.say` 默认 add_to_chat_ctx=True——watchdog-ack/
nudge/storm/starve/defer/followup/garbled-reask 七条纯 ack 车道全部落 LLM
历史，占 8 对史窗、喂 4B 自己的 ack 刷屏（道歉毒性的史内残留；ack-anchor-
exempt 只挡了锚/摘要面）。官方 fast-filler 模式明确 fillers 用
add_to_chat_ctx=False。

契约（Ethan 拍板「仅纯 ack 族」）：
- 排除集 = {watchdog-ack, nudge, storm-ack, starve-ack×2, defer-ack,
  followup-ack, garbled-reask}：history=False 注册（不推票据+置 A3 旗）
  + say(add_to_chat_ctx=False) + _ledger_ack_line 手工补 turns 行；
- 保留集 = wa-flush/digit-flush/wa-confirm/stall-*/opening/flow-say/
  late-answer/branch-refuse/farewell/qa、branch 罐头——默认行为零变化；
- KV 前缀安全：史只增不删（add_to_chat_ctx=False=从不插入），无回溯删改。

闭包内代码按仓惯例源级 pin。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)

_EXCLUDED_LANES = (
    "watchdog-ack",
    "nudge",
    "storm-ack",
    "starve-ack",
    "defer-ack",
    "followup-ack",
    "garbled-reask",
)
# 排除车道出现次数（starve-ack 两处发射点）
_EXPECTED_HISTORY_FALSE = {**{ln: 1 for ln in _EXCLUDED_LANES}, "starve-ack": 2}


def test_say_script_threads_add_to_chat_ctx():
    """_say_script 形参 + 全部 4 处 session.say 穿透。"""
    assert "add_to_chat_ctx: bool = True" in AGENT_SRC
    assert "return await session.say(text, add_to_chat_ctx=add_to_chat_ctx)" in AGENT_SRC
    assert (
        "audio=frames_aiter(pcm_to_frames(pcm, tts_provider.sample_rate)),\n"
        "            add_to_chat_ctx=add_to_chat_ctx,"
    ) in AGENT_SRC
    assert (
        "return await session.say(text, audio=_synth_and_play(), add_to_chat_ctx=add_to_chat_ctx)"
    ) in AGENT_SRC


def test_register_reply_lane_history_param():
    """history=False：不推票据 + 置 A3 旗（item_added 不再发生）。"""
    assert "history: bool = True," in AGENT_SRC
    assert "if not history:" in AGENT_SRC
    assert '_assistant_out["on"] = True' in AGENT_SRC


def _lane_registration_sites(src: str):
    """解析全部 _register_reply_lane 调用点。

    返回 [(lane 名原始表达式, 调用文本, 本车道块窗口到下一注册点)]。
    调用点 kwargs 内无嵌套括号冲突,深度扫描取平衡右括号。
    """
    import re

    key = "_register_reply_lane("
    # 排除 def 定义本身(2026-10-02 批3 合流校准):main 的 def 跨行形参
    # (`async def _register_reply_lane(\n    *,\n    lane...`)同样含本串,
    # 误收为「调用点」后其 1200 字符窗口(def 体+邻函数)会触发假阳性劈叉断言。
    starts = [
        m.start()
        for m in re.finditer(re.escape(key), src)
        if not re.search(r"def\s*$", src[max(0, m.start() - 40) : m.start()])
    ]
    sites = []
    for i, s in enumerate(starts):
        depth = 0
        j = s + len(key) - 1  # 指向 "("
        while j < len(src):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        call = src[s : j + 1]
        m_lane = re.search(r'lane=("[^"]+"|f"[^"]+")', call)
        lane_raw = m_lane.group(1) if m_lane else "?"
        window_end = starts[i + 1] if i + 1 < len(starts) else len(src)
        sites.append((lane_raw, call, src[j + 1 : window_end]))
    return sites


def test_excluded_lanes_registered_history_false():
    """八发射位七车道逐个 history=False 注册（逐车道断言,非总数）。"""
    sites = _lane_registration_sites(AGENT_SRC)
    assert sites, "应解析到 _register_reply_lane 调用点"
    for lane, n in _EXPECTED_HISTORY_FALSE.items():
        regs = [c for raw, c, _ in sites if lane.strip('"') in raw]
        assert len(regs) >= n, f"lane={lane} 注册点少於预期 {n}（实际 {len(regs)}）"
        # 逐注册点断言：排除车道的注册**必须**带 history=False——
        # 2026-10-01 实锤:defer-ack 漏带,靠全文件计数 >=8 被 docstring 里的
        # history=False 字样喂绿,票据无人消费被纯 LLM item 兜底误领。
        for c in regs:
            assert "history=False" in c, f"lane={lane} 注册缺 history=False:{c[:120]}"
    # 手工补账调用逐车道在场
    for lane in _EXCLUDED_LANES:
        assert f'await _ledger_ack_line("{lane}"' in AGENT_SRC, f"缺 {lane} 手工补账"


def test_add_to_chat_ctx_false_paired_with_history_false():
    """配对不变量：凡以 add_to_chat_ctx=False 方式出声,其注册必带 history=False。

    反向判定(2026-10-02 批3 合流校准)：对每个 `add_to_chat_ctx=False` 的
    出声调用,回溯**最近的前置注册点**——正向块窗口(注册→下一注册)在相邻
    车道块紧挨时(heartbeat farewell→nudge<1200 字符)会误把邻块的 False 出声
    记到本块头上(farewell/digit-flush 双假阳性);反向锚定把 False 出声唯一
    归属给紧邻它之前的注册,配对关系精确。违反即票据/出声方式劈叉
    (票据推了 item 永不发生→5s 兜底误领)。
    """
    import re as _re

    for m in _re.finditer(r"_say_script\((?:[^()]|\([^()]*\))*?add_to_chat_ctx=False", AGENT_SRC, _re.S):
        idx = m.start()
        prev = [
            (raw, call)
            for raw, call, _ in _lane_registration_sites(AGENT_SRC)
            if AGENT_SRC.index(call) < idx
        ]
        if not prev:
            continue
        lane_raw, call = prev[-1]
        if "notify=True" in call:
            continue
        assert "history=False" in call, (
            f"lane={lane_raw} 出声 add_to_chat_ctx=False 但最近注册未带 "
            f"history=False（票据劈叉）:{call[:120]}"
        )


def test_ledger_ack_line_helper_shape():
    """_ledger_ack_line：assistant 行 + provider=lane + gen=script，失败唔阻。"""
    assert "async def _ledger_ack_line(lane: str, text: str) -> None:" in AGENT_SRC
    assert 'provider=lane, gen="script",' in AGENT_SRC
    assert "ack lane ledger failed lane=" in AGENT_SRC


def test_kept_lanes_untouched():
    """保留车道零变化：注册不带 history、say 不带 add_to_chat_ctx=False。"""
    for lane in ("wa-flush", "digit-flush", "wa-confirm", "flow-say", "late-answer",
                 "branch-refuse", "farewell", "branch-canned"):
        assert f'lane="{lane}"' in AGENT_SRC, f"保留车道 {lane} 应仍在"
    # flow-say(直念步)的 say 调用不带 add_to_chat_ctx=False：定位其调用行
    import re

    m = re.search(
        r'_register_reply_lane\(\s*lane="flow-say"[^)]*\)(?!\s*\n\s*history)',
        AGENT_SRC,
    )
    assert m, "flow-say 注册应在场"
    # opening 轨注释仍钉进史语义
    assert "turn-1 前缀命中不变" in AGENT_SRC or "前缀命中不变" in AGENT_SRC
