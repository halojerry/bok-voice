"""W8-B 热修单测（2026-10-09；只覆盖本会话所有权两文件，零网络）。

- **H1 合并档**（stash 抢救件回归钉，call-fb236e61/call-585ab71d 实弹形状）：
  lite（server_utt+clause_commit 同开）下本地意群档在未提交余段继续切句——
  server-utt 单档实证连续语流 definite 只在停嘴出现（72 字单段单 definite=
  「等说完才出声」）；服务端 definite 消费做坐标协调+差量 FINAL（61a2ff3：
  不吞不重）。
- **H2 段切换互斥硬化**（三通实弹「第 3 轮起翻译侧全哑」进行性断流根治）：
  START/END 撞在途收段先等落地；``_wd_cancel`` 对已进收段的看门狗放行；
  收段被 cancel 复位 finish 旗+段坐标再重抛；互斥等待带 shield+超时兜底
  （楔死强制复位放行，绝不带悬挂状态继续哑）。
- **W8-B 切口质量**（call-fa95543a「mini Max 开」劈词）：拉丁/数字 run 计 1 词
  （``_clause_content_units``）；词界铁闸（``_len_fuse_cut`` 切点结构性不落
  run 中间）；顿号（、）权重低于逗号（攒 12 内容单位）；长度保险丝防饿死化
  （余段有真标点在望绝不硬剁）。

注：前 agent 的 H3（pipeline 播放解耦）用例不随本文件——pipeline.py 非本会话
所有权、stash 只取回本两文件，H3 归其所有者随其文件回归。

fake WS/VAD/造帧单源复用 ``tests.test_doubao_asr``。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_runtime.providers import doubao_asr as da  # noqa: E402
from agent_runtime.providers.doubao_asr import (  # noqa: E402
    DoubaoSTT,
    _DoubaoLiveStream,
)

import tests.test_doubao_asr as base  # noqa: E402  (fake WS/VAD/造帧单源)

_REPO = Path(__file__).resolve().parents[1]

# 段A（17 字）/段B（6 字）——镜像 call-fb236e61 的「浴巾/空调」实弹形状。
_SEG_A = "我有蓝色的预警，还有个白色的空调。"
_SEG_B = "你想不想要？"


def _merged_stream() -> tuple[_DoubaoLiveStream, list[tuple[str, str]]]:
    """合并档流（server_utt+clause_commit+len_fuse=lite 装配旗组）+事件收集器。"""
    stt = DoubaoSTT(
        api_key="k", vad_=base._FakeVad(),
        clause_commit=True, server_utterances=True, len_fuse=True, utt_merge=True,
    )
    stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
    events: list[tuple[str, str]] = []

    class _FakeCh:
        def send_nowait(self, ev):
            t = ev.alternatives[0].text if ev.alternatives else ""
            events.append((str(ev.type).split(".")[-1], t))

        def close(self):  # 流收尾回调（aclose 时框架关事件道）；测试收集器 no-op
            pass

    stream._event_ch = _FakeCh()
    return stream, events


def _finals(events) -> list[str]:
    return [t for n, t in events if n == "FINAL_TRANSCRIPT"]


def _interims(events) -> list[str]:
    return [t for n, t in events if n == "INTERIM_TRANSCRIPT"]


# ---- W8-B 切口质量：拉丁/数字按词计 ------------------------------------------------


def test_content_units_latin_run_counts_one_word():
    """内容单位纯函数：连续 ASCII 字母/数字 run=1 词、CJK 逐字、标点/空白不计。

    旧 len 口径下 ``mini Max 开``=10——拉丁逐字计权让字数闸形同虚设（实弹切出
    「mini Max 开|放平台」劈词）。"""
    assert da._clause_content_units("mini Max 开") == 3  # 旧 len 口径=10
    assert da._clause_content_units("ABC123") == 1
    assert da._clause_content_units("你好，世界。") == 4  # 标点不计
    assert da._clause_content_units("a b c") == 3
    assert da._clause_content_units("") == 0


def test_len_fuse_word_boundary_iron_gate():
    """词界铁闸：切点结构性不落 ASCII run 中间；拉丁词计下旧病形状不再提前硬剁。"""
    # 两 run 各计 1 词 → 切点=第二 run 尾之后（绝不劈 abcd1234/efgh）。
    # 词界铁闸直接钉：切点只在 run 尾之后，永不落 run 中间（两 run 由空格分隔）。
    assert da._len_fuse_cut("abc 1234 efgh", 0, 2) == 8  # abc=1、1234=2 → 切第二 run 尾
    # 实弹病形状（call-fa95543a）：旧逐字口径在「接口」词中间剁；词计下 13 单位
    # < 20 → None（等更多内容/EOS）。
    assert da._len_fuse_cut("你可以试试 MiniMax 开放平台的接口", 0, 20) is None
    # 攒够 20 单位时切点恒在完整词之后（MiniMax 整词在前缀内、切点不落 run 内）。
    cut = da._len_fuse_cut("你可以试试 MiniMax 开放平台的接口能力概览说明书文档", 0, 20)
    assert cut is not None
    prefix = "你可以试试 MiniMax 开放平台的接口能力概览说明书文档"[:cut]
    assert "MiniMax" in prefix  # 词完整在前缀内（旧逐字口径会把 20 字门槛落进词邻域）


def test_dunhao_tier_needs_more_units_than_comma(monkeypatch):
    """顿号权重低于逗号：同形状 9 单位逗号即切、顿号不切；攒满 12 单位顿号才切。"""
    monkeypatch.delenv("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", raising=False)
    monkeypatch.delenv("QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS", raising=False)  # 缺省 8
    now = 100.0
    # 逗号：9 内容单位 ≥ 8 → 切（prev 含标点=跨 interim 稳定姿势）。
    prev_c = "我们看一下苹果香蕉，"
    assert da._find_clause_cut("我们看一下苹果香蕉，帮我", 0, prev_c, last_commit_at=0.0, now=now) == 10
    # 顿号：列举符不是句界，9 单位 < 12 → 不切（列举不碎切）。
    prev_d = "我们看一下苹果香蕉、"
    assert da._find_clause_cut("我们看一下苹果香蕉、帮我", 0, prev_d, last_commit_at=0.0, now=now) is None
    # 攒满 12 单位：顿号照切（防饿死仍有出口）。
    long_prev = "今天下午三点我们在大会议室开产品评审会、"
    assert da._find_clause_cut(
        "今天下午三点我们在大会议室开产品评审会、然后", 0, long_prev,
        last_commit_at=0.0, now=now,
    ) == 20


def test_len_fuse_yields_when_punct_in_sight(monkeypatch, capsys):
    """防饿死化（有标点优先等标点）：余段有真标点在望时保险丝绝不硬剁；
    标点稳定一拍后由标点档接手。"""
    monkeypatch.setenv("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS", "6")

    async def scenario():
        stream, events = _merged_stream()
        t1 = "价格方面我们可以再帮你申请一下更大的优惠折扣额度，请您稍等一下"
        stream._on_payload({"result": {"text": t1, "utterances": []}})
        assert _finals(events) == []  # 首现不稳定；且逗号前 24 单位够格=在望 → 保险丝不硬剁
        # 稳定一拍（interim 去重按文本变化，第二拍必须增长）：逗号档接手（24 单位 ≥ 6）。
        stream._on_payload({"result": {"text": t1 + "啊", "utterances": []}})
        assert _finals(events) == ["价格方面我们可以再帮你申请一下更大的优惠折扣额度，"]
        await stream.aclose()

    asyncio.run(scenario())


def test_len_fuse_still_fires_without_punct(monkeypatch):
    """防饿死本职保留：无标点长流攒满门槛（快启动首块 8 单位）照切。"""
    monkeypatch.setenv("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS", "6")

    async def scenario():
        stream, events = _merged_stream()
        t = "这是一个完全没有任何标点的超长连续语流我们一直在往下说个不停"
        stream._on_payload({"result": {"text": t, "utterances": []}})
        assert _finals(events) == []  # 首现不稳定
        stream._on_payload({"result": {"text": t + "啊", "utterances": []}})
        assert _finals(events) == [t[:8]]  # 无标点 → 快启动保险丝 8 单位即切
        await stream.aclose()

    asyncio.run(scenario())


# ---- H1：合并档——本地意群档在 server-utt 下继续切句 -----------------------------


def test_h1_local_comma_cut_fires_in_server_utt_mode(monkeypatch, capsys):
    """连续语流（无 definite）本地逗号档照切：稳定子句→CLAUSE_COMMIT FINAL。"""
    monkeypatch.setenv("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS", "6")

    async def scenario():
        stream, events = _merged_stream()
        t1 = "今天天气很好，我们出去玩"  # 逗号候选（跨 interim 未稳定）
        stream._on_payload({"result": {"text": t1, "utterances": []}})
        assert _finals(events) == []  # 首现不稳定：不提交（滑窗修订防抖）
        t2 = t1 + "吧"
        stream._on_payload({"result": {"text": t2, "utterances": []}})
        # 稳定命中：逗号档 FINAL（server-utt 单档下此 FINAL 结构性不存在=72 字句病）。
        assert _finals(events) == ["今天天气很好，"]
        assert _interims(events)[-1] == "我们出去玩吧"  # display=已提交后尾巴
        await stream.aclose()

    asyncio.run(scenario())
    assert "CLAUSE_COMMIT" in capsys.readouterr().out


def test_h1_definite_covers_local_cut_differential_tail(monkeypatch, capsys):
    """本地切点在前→服务端 definite 覆盖：committed 收口、FINAL 只发差量尾巴。"""
    monkeypatch.setenv("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS", "6")

    async def scenario():
        stream, events = _merged_stream()
        stream._on_payload({"result": {"text": "今天天气很好，我们出去玩", "utterances": []}})
        stream._on_payload({"result": {"text": "今天天气很好，我们出去玩吧", "utterances": []}})
        assert _finals(events) == ["今天天气很好，"]  # 本地逗号档已提交 7 字
        # 服务端 definite 覆盖整句（含已交 7 字）：差量尾巴=未交的 7 字。
        whole = "今天天气很好，我们出去玩吧。"
        stream._on_payload({"result": {"text": whole, "utterances": [{"text": whole, "definite": True}]}})
        assert _finals(events) == ["今天天气很好，", "我们出去玩吧。"]  # 无重复
        assert stream._cc_committed_len == len(whole)
        await stream.aclose()

    asyncio.run(scenario())
    assert "UTTERANCE definite chars=14" in capsys.readouterr().out


def test_h1_definite_already_covered_no_duplicate_final(monkeypatch, capsys):
    """本地切点更远（definite⊆已提交）：covered 只记账、绝不重发 FINAL（防双译）。"""

    async def scenario():
        stream, events = _merged_stream()
        # 直接构造「本地已提交 14 字」姿势（两个逗号子句已各发 FINAL）。
        stream._cc_committed_text = "今天天气很好，我们出去玩吧。"
        stream._cc_committed_len = 14
        stream._on_payload({"result": {"text": "今天天气很好，我们出去玩吧", "utterances": [
            {"text": "今天天气很好，", "definite": True},
        ]}})
        assert _finals(events) == []  # covered：零 FINAL（旧累积语义会重发 7 字=双译）
        assert stream._cc_committed_len == 14  # 本地更远坐标保持
        await stream.aclose()

    asyncio.run(scenario())
    assert "covered" in capsys.readouterr().out


def test_h1_multi_definite_payload_after_local_cut(monkeypatch, capsys):
    """一帧多 definite（服务端全量重发形态）在本地切点之后：逐枚 covered、零重发。"""

    async def scenario():
        stream, events = _merged_stream()
        stream._cc_committed_text = "今天天气很好，我们出去玩吧。"
        stream._cc_committed_len = 14
        stream._on_payload({"result": {"text": "今天天气很好，我们出去玩吧。还想再待一会", "utterances": [
            {"text": "今天天气很好，", "definite": True},
            {"text": "我们出去玩吧。", "definite": True},
        ]}})
        assert _finals(events) == []  # 两枚都在已提交域内：零重发
        assert stream._cc_committed_len == 14
        # 后续真新内容照常差量。
        stream._on_payload({"result": {"text": "今天天气很好，我们出去玩吧。还想再待一会。", "utterances": [
            {"text": "今天天气很好，", "definite": True},
            {"text": "我们出去玩吧。还想再待一会。", "definite": True},
        ]}})
        assert _finals(events) == ["还想再待一会。"]
        await stream.aclose()

    asyncio.run(scenario())


# ---- H2：段切换坐标（跨段 definite/差量 FINAL/空 final） ------------------------


def test_h2_cross_segment_definite_b_full_text(monkeypatch, capsys):
    """段A definite→收段复位→段B definite：FINAL=段B 6 字全文（非空/非错位）。

    旧病理（坐标跨段携带）：段B 的 definite 被 ``_su_emitted`` 计数跳过、或
    committed 前缀残留把段B 当已提交吞掉——断言钉死新段 committed 从零起算
    （段 B 首字完整）。"""
    monkeypatch.delenv("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", raising=False)

    async def scenario():
        stream, events = _merged_stream()
        # 段A：一枚 definite（17 字）→ FINAL(A)、committed=17。
        stream._on_payload({"result": {"text": _SEG_A, "utterances": [{"text": _SEG_A, "definite": True}]}})
        assert _finals(events) == [_SEG_A]
        assert stream._cc_committed_len == len(_SEG_A)
        # 段收口（_finalize_utterance 同款坐标副作用）：EOS 尾巴空 + 复位。
        assert stream._clause_tail(_SEG_A) == ""  # 已全部提交：EOS 无尾巴（零空 final）
        stream._reset_segment()
        assert stream._cc_committed_len == 0 and stream._su_emitted == 0
        assert stream._su_text == ""
        # 段B（新会话）：说 6 字→definite：FINAL 必须=段B 全文（含首字）。
        stream._on_payload({"result": {"text": _SEG_B, "utterances": [{"text": _SEG_B, "definite": True}]}})
        assert _finals(events) == [_SEG_A, _SEG_B]  # 段B final=6 字全文（非空/非差量错位）
        assert stream._cc_committed_len == len(_SEG_B)
        assert stream._su_emitted == 1
        await stream.aclose()

    asyncio.run(scenario())


def test_h2_same_session_continuation_b_first_char_kept(monkeypatch, capsys):
    """段A definite→同段无缝续说段B：段B final 必含段B 首字（force_to_speech_time
    姿势下 pre-roll 叉点摄入的句头不被 committed 前缀吞掉）。"""
    monkeypatch.delenv("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", raising=False)

    async def scenario():
        stream, events = _merged_stream()
        stream._on_payload({"result": {"text": _SEG_A, "utterances": [{"text": _SEG_A, "definite": True}]}})
        assert _finals(events) == [_SEG_A]
        # 无缝续说（同 WS 会话，text 单调累积；段B 尚未 definite）：
        # interim display=段B 尾巴，必须以段B 首字开头。
        grown = _SEG_A + _SEG_B
        stream._on_payload({"result": {"text": grown, "utterances": [{"text": _SEG_A, "definite": True}]}})
        interims = _interims(events)
        assert interims and interims[-1].startswith(_SEG_B[0])  # 段B 首字未被吞
        # 段B 转 definite：FINAL=差量尾巴=段B 全文（不重发段A、不吞段B）。
        stream._on_payload({
            "result": {
                "text": grown,
                "utterances": [{"text": _SEG_A, "definite": True}, {"text": _SEG_B, "definite": True}],
            }
        })
        assert _finals(events) == [_SEG_A, _SEG_B]
        # EOS：全部已提交，尾巴空（不重发、零空 final）。
        assert stream._clause_tail(grown) == ""
        await stream.aclose()

    asyncio.run(scenario())


def test_h2_finalize_empty_text_emits_no_final(monkeypatch):
    """空 final 不再出现：全已提交/无文本收段 → 零 FINAL 事件。"""

    async def scenario():
        stream, events = _merged_stream()
        # 会话已死+段缓冲空：_finish_segment 无文本 → tail 空 → 不发事件。
        await asyncio.wait_for(stream._finalize_utterance(), timeout=2)
        assert _finals(events) == [] and _interims(events) == []
        await stream.aclose()

    asyncio.run(scenario())


# ---- H2：互斥硬化（等待落地/超时兜底/取消复位/看门狗放行） ------------------------


def test_h2_start_awaits_inflight_finalize(monkeypatch):
    """在途收段（尾窗到期/看门狗 detached finalize）未落地：START 护栏必须等
    ``_reset_segment``（坐标/段缓冲清零）完成才放行——旧版竞态=新段连锅端。"""

    async def scenario():
        stt = DoubaoSTT(api_key="k", vad_=base._FakeVad(), clause_commit=True, server_utterances=True)
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
        # 造「旧段已提交」姿势：收段落地应把它清零。
        stream._cc_committed_text = _SEG_A
        stream._cc_committed_len = len(_SEG_A)
        stream._su_emitted = 1
        entered, release, landed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def _inflight_finalize():
            entered.set()
            await release.wait()
            stream._reset_segment()  # 收段的关键副作用
            landed.set()

        t = asyncio.create_task(_inflight_finalize())
        await entered.wait()
        stream._fin_task = t
        stream._tail_finalizing = True  # 尾窗到期姿势（_tail_task 已被 watcher 置 None）

        async def _guarded():
            await stream._await_inflight_finalize()

        waiter = asyncio.create_task(_guarded())
        await asyncio.sleep(0.05)
        assert not waiter.done()  # 收段未落地：护栏拦着（旧版此处已放行=竞态）
        release.set()
        await asyncio.wait_for(waiter, timeout=2)
        assert landed.is_set()  # 护栏等到收段落地才返回
        assert stream._cc_committed_len == 0 and stream._su_emitted == 0
        # 无在途收段：零等待直接返回。
        stream._fin_task = None
        await asyncio.wait_for(stream._await_inflight_finalize(), timeout=0.1)

    asyncio.run(scenario())


def test_fin_await_timeout_backstop_resets_without_cancelling(monkeypatch, capsys):
    """超时兜底（W8-B）：收段楔死永不落地→护栏按帽放行+强制复位段状态；
    shield 保证楔死任务本身**不被拦腰 cancel**（拦腰=旧竞态坐标连锅端）。"""
    monkeypatch.setattr(da, "_FIN_AWAIT_TIMEOUT_S", 0.05)

    async def scenario():
        stt = DoubaoSTT(api_key="k", vad_=base._FakeVad(), clause_commit=True, server_utterances=True)
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
        stream._cc_committed_text = _SEG_A
        stream._cc_committed_len = len(_SEG_A)
        stream._finishing = True
        wedged = asyncio.create_task(asyncio.Event().wait())  # 永不落地
        await asyncio.sleep(0.01)
        stream._fin_task = wedged
        await asyncio.wait_for(stream._await_inflight_finalize(), timeout=2)
        # 放行+复位：新段从干净坐标起步（绝不带悬挂 finish 旗继续哑）。
        assert stream._finishing is False
        assert stream._cc_committed_len == 0 and stream._cc_committed_text == ""
        assert stream._fin_task is None  # 登记位让位（迟到的 finally 不再认领）
        # shield：楔死任务原样飞行（未被 cancel）。
        assert not wedged.done() and not wedged.cancelled()
        wedged.cancel()  # 清理
        try:
            await wedged
        except asyncio.CancelledError:
            pass
        assert "DOUBAO_FIN_AWAIT_TIMEOUT" in capsys.readouterr().out

    asyncio.run(scenario())


def test_fin_await_zero_wait_when_done():
    """已落地（done）的收段：零等待直接返回。"""

    async def scenario():
        stt = DoubaoSTT(api_key="k", vad_=base._FakeVad(), clause_commit=True, server_utterances=True)
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        stream._fin_task = done
        await asyncio.wait_for(stream._await_inflight_finalize(), timeout=0.1)

    asyncio.run(scenario())


def test_wd_cancel_spares_inflight_finalize():
    """``_wd_cancel`` 对已进收段（=_fin_task 本尊）的看门狗放行不 cancel——
    拦腰取消收段=跳过 _reset_segment 留悬挂 finish 旗（进行性断流次根因）。"""

    async def scenario():
        stt = DoubaoSTT(api_key="k", vad_=base._FakeVad(), clause_commit=True, server_utterances=True)
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())

        async def _wedge():
            await asyncio.Event().wait()

        # 收段相：放行（不 cancel）。
        t = asyncio.create_task(_wedge())
        await asyncio.sleep(0.01)
        stream._fin_task = t
        stream._wd_task = t
        stream._wd_cancel()
        assert stream._wd_task is None
        assert not t.done()  # 未被取消
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        # 循环相（未进收段）：照常取消。
        t2 = asyncio.create_task(_wedge())
        await asyncio.sleep(0.01)
        stream._fin_task = None
        stream._wd_task = t2
        stream._wd_cancel()
        await asyncio.sleep(0.01)
        assert t2.cancelled()

    asyncio.run(scenario())


def test_cancelled_finalize_resets_finishing_and_coords(monkeypatch):
    """收段任务被取消（收线窗/丢段/流关闭）：finish 旗+段坐标必须复位再重抛——
    悬挂 _finishing 把后续 INFERENCE_DONE 喂帧全拦=进行性断流次根因。"""

    async def scenario():
        stt = DoubaoSTT(api_key="k", vad_=base._FakeVad(), clause_commit=True, server_utterances=True, utt_merge=True)
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
        stream._cc_committed_text = _SEG_A  # 旧段坐标残留
        stream._cc_committed_len = len(_SEG_A)
        stream._su_emitted = 1

        async def _hang():
            await asyncio.Event().wait()

        monkeypatch.setattr(stream, "_finalize_utterance", _hang)  # 收段悬挂姿势
        t = asyncio.create_task(stream._tail_watch())
        await asyncio.sleep(0.6)  # 过尾窗（utt_wait 0.45）→ 进收段（_finishing=True）
        assert stream._finishing is True and stream._fin_task is t
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        # 复位完成：无悬挂旗、坐标清零、登记位清空。
        assert stream._finishing is False
        assert stream._cc_committed_len == 0 and stream._su_emitted == 0
        assert stream._fin_task is None

    asyncio.run(scenario())


def test_h2_mutex_wiring_pin():
    """接线 pin：START+END 两处互斥护栏、看门狗收段放行、超时兜底、两路 watcher
    取消复位（防未来重构悄然旁路）。"""
    src = (_REPO / "apps/agent/agent_runtime/providers/doubao_asr.py").read_text()
    assert src.count("await self._await_inflight_finalize()") == 2  # START+END
    assert "self._fin_task = asyncio.current_task()" in src  # 两路 detached 收段都登记
    assert "t is self._fin_task" in src  # _wd_cancel 收段相放行
    assert "asyncio.shield(fin)" in src  # 超时兜底不拦腰 cancel
    assert "DOUBAO_FIN_AWAIT_TIMEOUT" in src
    # 两路 watcher 取消复位（cancel 途中收段=悬挂 finish 旗根因，复位后重抛）。
    cancel_heal = "self._finishing = False\n            self._reset_segment()\n            raise"
    assert src.count(cancel_heal) == 2


# ---- 播放期摄入无闸（防复发钉） ----------------------------------------------------


def test_ingestion_feed_unconditional_under_busy_flags():
    """_feed 音频帧转发与播放/pipeline 状态零耦合：回复在途/播放中旗全开照样进队。

    摄入侧唯一合法门=声纹锁（_gate）；本测把一切「播报时暂停识别」类开关位
    置真（含 A 线旋钮），断言帧照常转发 WS 发送队列。"""

    async def scenario():
        vad = base._FakeVad()
        stt = DoubaoSTT(api_key="k", vad_=vad, clause_commit=True, server_utterances=True)
        stream = _DoubaoLiveStream(stt, conn_options=da.APIConnectOptions())
        stream._session_alive = True
        stream._send_q = asyncio.Queue()
        # 一切可疑闸位置真：回复在途/partial 旋钮/最近 FINAL 稿非空。
        stt._reply_busy = True
        stt._partial_ms_override = 3000
        stt._turn_partial_text = "上一轮的话"
        pcm = b"\x11\x22" * 160  # 10ms@16k
        stream._feed(pcm)
        assert stream._send_q.qsize() == 1  # 播放/回复状态不改摄入
        stream._feed(pcm)
        assert stream._send_q.qsize() == 2
        await stream.aclose()

    asyncio.run(scenario())


def test_ingestion_no_gate_ast_pin():
    """AST 钉：``_feed`` 函数体零播放/pipeline 状态引用（未来加闸即红）。"""
    import ast

    src = (_REPO / "apps/agent/agent_runtime/providers/doubao_asr.py").read_text()
    tree = ast.parse(src)
    feeds = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_feed"]
    assert len(feeds) == 1
    deny = {"reply", "busy", "say", "speak", "speaking", "playback", "playout", "pause"}
    tokens: set[str] = set()
    for node in ast.walk(feeds[0]):
        if isinstance(node, ast.Name):
            tokens.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            tokens.add(node.attr.lower())
    hit = tokens & deny
    assert not hit, f"_feed 出现播放/回复状态引用: {sorted(hit)}"


def test_lite_worker_never_touches_ingestion_gates_source_scan():
    """源扫描钉：interp_lite 全树不触碰任何 ASR 摄入闸旋钮（grep 现码零命中的
    回归钉——防止未来有人给薄线加「播报时暂停识别」类门）。"""
    lite_dir = _REPO / "apps/agent/agent_runtime/interp_lite"
    deny = ("set_reply_busy", "set_partial_ms", "set_closing_say", "_reply_busy")
    for py in sorted(lite_dir.rglob("*.py")):
        src = py.read_text()
        for knob in deny:
            assert knob not in src, f"{py.name} 出现摄入闸旋钮 {knob}"


# ---- 合并档装配接线（lite 缺省形状不回归） ---------------------------------------


def test_lite_wiring_flags_unchanged(monkeypatch):
    """lite 装配旗组 pin：合并档=server_utt+clause_commit+len_fuse 同开（缺省）。"""
    from agent_runtime.interp_lite.providers.asr_doubao import LiteDoubaoSTT

    for k in ("BOK_INTERP_SERVER_UTT", "BOK_INTERP_UTT_MERGE", "BOK_INTERP_UTT_WAIT_S"):
        monkeypatch.delenv(k, raising=False)
    lite = LiteDoubaoSTT(api_key="k")
    assert lite._server_utterances is True
    assert lite._clause_commit is True
    assert lite._len_fuse is True
    # kill-switch 保留：SERVER_UTT=0 回纯本地闸档（合并档关闭）。
    monkeypatch.setenv("BOK_INTERP_SERVER_UTT", "0")
    off = LiteDoubaoSTT(api_key="k")
    assert off._server_utterances is False and off._clause_commit is True
