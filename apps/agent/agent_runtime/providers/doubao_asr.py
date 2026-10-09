"""豆包（火山引擎）流式语音识别 —— LiveKit STT provider（2026-10-03 云 ASR 装线波）。

协议：openspeech V3 族 SAUC 双向流式（``wss://.../api/v3/sauc/bigmodel``，二进制帧
+ gzip；帧格式与 scripts/probes/probe_cloud_asr.py 同源——探针为取证台架，本文件为生产
实现，协议语义改动两处同步）。

设计定案（全部有 2026-10-03 实弹证据，见 probe 报告 reports/cloud-asr/）：

- **一段 VAD 语音 = 一个 WS 会话**：本地 VAD（min_silence 0.28-0.45s）驱动分段，
  START_OF_SPEECH 开会话（连接时延藏在用户说话期间）、说话中 200ms 分包喂入、
  END_OF_SPEECH 发末包（负 seq）强制服务端立即定稿——服务端自身 800ms
  end_window 判停在我们的分段节奏里结构性用不上（我们 0.28s 静音先到）。
- **result.text 全程单调累积**（两句话夹 1s 静音实弹：不重置、definite 只在末包
  出现）——FINAL 直接取「最长已见文本」，无需已提交坐标/拼接账本。
- **interim 高频**（~200ms/次、有重复）——按「文本变化」去重后发 INTERIM_TRANSCRIPT。
- **重试**：整段 PCM 全程留缓冲（上限 60s），live 路连接失败/超时/服务端错且无
  文本时，整段单发重试一次（探针同姿势）；重试仍败 → 返回空（EOS 已发，空转写由
  下游 EMPTY_TURN_DROPPED 兜底，绝不悬挂轮次）。
- **方言**：``enable_lid`` 常开（官方「启用中英文及方言识别」，含粤语）；语言不传
  （auto 三语实测全通，zh 0.0 / 粤 0.084 / en 0.073）。
- **热词**：``request.corpus.context`` 直传热词（100 token 上限，取前 40 词）。
- 收线/告别直念窗（F4）：``set_closing_say(True)`` 期间整段丢弃（不发 EOS/FINAL）。
- **说话中成句（clause_commit，B 线 W1 2026-10-08）**：interim ``result.text``
  （~400ms 更新=快车道）上跑 A 线句级闸标点档（``_find_clause_cut`` 单点
  import ``livekit_plugins`` 纯函数族，零第二份）——稳定子句前缀即发
  FINAL_TRANSCRIPT（说话中 MT 起跑）；committed-prefix 对齐剥已交前缀
  （partial 修订绝不回滚已发出的 FINAL：失配记日志+重置对齐）；VAD END 只补
  未提交尾巴。``clause_commit=False``（A 线缺省不传）=逐字节旧路。

总闸 ``BOK_DOUBAO_ASR``（缺省开，=0 时装配点回退本地 Qwen3-ASR；装配点读法见
agent.py / interpret.py 的 provider 分支）。凭据/端点由设置面（asr 段）经装配点
下发，本模块零 env 凭据、零模块级可变全局（worker 并发多通不串线）。
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import os
import struct
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

from livekit.agents import APIConnectOptions, stt, utils, vad

from ..speaker_lock import SegmentSpeakerGate

# ---- 常量 ----
DOUBAO_WS_DEFAULT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
# 官方推荐 2.0（seedasr）；1.0（bigasr）为历史版本——两者四臂矩阵实测同输出，
# 装线按 2.0，设置面可覆盖。
DOUBAO_RESOURCE_DEFAULT = "volc.seedasr.sauc.duration"

_PACKET_BYTES = 16000 * 2 * 200 // 1000  # 200ms@16k mono PCM16
_SEG_CAP_BYTES = 16000 * 2 * 60  # 重试缓冲上限 60s
_CONNECT_TIMEOUT_S = 8.0
_FINAL_TIMEOUT_S = 6.0
# R4-C 端窗看门狗（2026-10-09 W8-A2）：definite 到而本地迟迟不 END 的病理兜底
# 宽限（>2s 才收段）与轮询节拍。宽限常量不设 env（病理兜底不值得运营面；
# 单测 monkeypatch 本常量提速）。
_END_WINDOW_WATCHDOG_S = 2.0
_END_WINDOW_WATCHDOG_TICK_S = 0.25

# 火山 SAUC 二进制帧（V3 协议族；官方 demo protocol.py 语义）
MSG_FULL_CLIENT_REQ = 0b0001
MSG_AUDIO_ONLY_REQ = 0b0010
MSG_FULL_SERVER_RESP = 0b1001
MSG_SERVER_ACK = 0b1011
MSG_ERROR = 0b1111

FLAG_NO_SEQ = 0b0000
FLAG_POS_SEQ = 0b0001
FLAG_LAST_NO_SEQ = 0b0010
FLAG_NEG_SEQ = 0b0011


def doubao_asr_enabled() -> bool:
    """总闸（缺省开）：=0 时装配点强制回退本地 Qwen3-ASR。"""
    return os.environ.get("BOK_DOUBAO_ASR", "1") != "0"


def _header(msg_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes([
        (1 << 4) | 1,                      # version=1, header_size=1 → 4 字节头
        (msg_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    ])


def frame_full_client_request(payload: dict) -> bytes:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    buf = _header(MSG_FULL_CLIENT_REQ, FLAG_POS_SEQ, serialization=1, compression=1)
    buf += struct.pack(">i", 1)            # seq=1
    buf += struct.pack(">I", len(body)) + body
    return buf


def frame_audio(seq: int, chunk: bytes, *, last: bool) -> bytes:
    body = gzip.compress(chunk)
    if last:
        buf = _header(MSG_AUDIO_ONLY_REQ, FLAG_NEG_SEQ, serialization=0, compression=1)
        buf += struct.pack(">i", -seq)
    else:
        buf = _header(MSG_AUDIO_ONLY_REQ, FLAG_POS_SEQ, serialization=0, compression=1)
        buf += struct.pack(">i", seq)
    buf += struct.pack(">I", len(body)) + body
    return buf


def parse_server_frame(data: bytes) -> dict:
    """宽容解析（官方 demo 语义）：flags&1=带 seq、&2=末包、&4=带 event。"""
    b0, b1, b2 = data[0], data[1], data[2]
    msg_type = b1 >> 4
    flags = b1 & 0x0F
    compression = b2 & 0x0F
    pos = 4 * (b0 & 0x0F)
    out: dict = {"type": msg_type, "flags": flags, "is_last": bool(flags & 0x02),
                 "seq": None, "event": None, "payload": b"", "code": None}
    if flags & 0x01:
        out["seq"] = struct.unpack(">i", data[pos:pos + 4])[0]
        pos += 4
    if flags & 0x04:
        out["event"] = struct.unpack(">i", data[pos:pos + 4])[0]
        pos += 4
    if msg_type == MSG_ERROR:
        out["code"] = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
    if msg_type in (MSG_FULL_SERVER_RESP, MSG_SERVER_ACK, MSG_ERROR):
        if pos + 4 <= len(data):
            size = struct.unpack(">I", data[pos:pos + 4])[0]
            pos += 4
            payload = data[pos:pos + size]
            if compression == 1 and payload:
                try:
                    payload = gzip.decompress(payload)
                except Exception:  # noqa: BLE001 - 解析失败留原始
                    pass
            out["payload"] = payload
    return out


def _ws_host_ok(url: str) -> bool:
    """仅放行 wss/https 公网端点（SSRF 护栏：拒环回/私有/保留地址）。

    域名不在此处做 DNS 解析（解析发生在 websockets 连接层）；IP 字面量按
    ``ipaddress.is_global`` 判定——私有/环回/保留/链路本地一律拒绝。
    """
    try:
        parsed = urlsplit(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("wss", "https"):
        return False
    host = (parsed.hostname or "").lower().strip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith((".local", ".internal", ".lan", ".localhost")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名：放行（连接层再做解析）
    return bool(ip.is_global)


def _emit_spec_feed(stt_, feed: str) -> None:
    """B 线投机翻译原文挂点派发(镜像 stable_prefix_listener 纪律):回调异常
    绝不影响 interim 事件流;挂点缺席(None)或空文本=零动作。"""
    if not feed:
        return
    cb = getattr(stt_, "raw_interim_listener", None)
    if cb is None:
        return
    try:
        cb(feed)
    except Exception as exc:  # noqa: BLE001
        print(f"BOK_INTERP_SPEC listener error: {exc!r}", flush=True)


def _find_clause_cut(
    text: str,
    start: int,
    prev_full: str,
    *,
    last_commit_at: float,
    now: float,
) -> int | None:
    """说话中子句级提交闸（A 线 ``_sentence_boundary`` 标点档移植——纯函数，
    实现单点 import ``livekit_plugins`` 纯函数族，**禁止第二份**）。

    扫 ``[start:]`` 找第一个过全部门的标点边界（强句 。！？!? 或子句 ，、；,;），
    返回边界后坐标（排他）或 None。门（参数沿 A 线语义，缺一不可）：
    - 限速：距上次提交 < ``QWEN3_ASR_COMMIT_MIN_INTERVAL_S``（B 线 worker
      env=1.0；A 线缺省 1.5——本闸只在 clause_commit 流生效）不提交；
    - 字数：强句边界候选 ≥ ``_ASR_SENTENCE_MIN_CHARS``(6)；子句边界候选 ≥
      ``QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS``（B 线 worker env=6）；
    - 数字 run 保护：候选段含 ≥4 位连续 ASCII 字母/数字 run（单号/号码高危）
      → 该边界不切、继续往后扫同段必再败 → 整段留给 EOS 尾巴兜底；
    - 跨 interim 稳定：上一 interim 全文同坐标与当前逐字一致（首现不提交，
      防滑窗修订 flicker——A 线 prev_full 同款）。
    """
    from .livekit_plugins import (  # noqa: PLC0415 - 懒 import：本模块保持轻导入面
        _ASR_SENTENCE_MIN_CHARS,
        _SENTENCE_STRONG_PUNCT,
        _SENTENCE_WEAK_PUNCT,
        _clause_commit_min_chars,
        _has_latin_or_digit_run,
        _sentence_commit_min_interval_s,
    )

    if now - last_commit_at < _sentence_commit_min_interval_s():
        return None
    if start >= len(text):
        return None
    i = start
    while i < len(text):
        strong = text[i] in _SENTENCE_STRONG_PUNCT
        weak = text[i] in _SENTENCE_WEAK_PUNCT
        if strong or weak:
            punct = _SENTENCE_STRONG_PUNCT if strong else _SENTENCE_WEAK_PUNCT
            j = i + 1
            while j < len(text) and text[j] in punct:
                j += 1
            min_chars = _ASR_SENTENCE_MIN_CHARS if strong else _clause_commit_min_chars()
            sentence = text[start:j]
            if (
                len(sentence) >= min_chars
                and not _has_latin_or_digit_run(sentence, min_len=4)
                and prev_full[start:j] == sentence
            ):
                return j
            # 该边界不够格（太短/数字 run/未稳定）→ 继续扫下一边界（短句排队
            # 累积；未稳定边界下个 interim 自然变稳定）。
        i += 1
    return None


def _len_fuse_cut(text: str, start: int, min_chars: int) -> int | None:
    """长度保险丝切点（纯函数，2026-10-09 W6×interp-lite 合流；单测直喂）。

    标点档（``_find_clause_cut``）只在标点边界提交——无标点连续语流（长句/
    地址/一气呵成形）结构性等句号/EOS，句档下两句并成一单=译文轨饿出巨型
    天窗（实弹 call-d6704474：26.3s 窗，根因 119 字单 commit）。本函数在
    ``[start:]`` 攒够 ``min_chars`` **内容字**（非空白/标点）后找安全切点：
    切位后一个字符不得是 ASCII 字母数字（防劈单号/号码 run——``_find_clause_cut``
    的数字 run 纪律同源）；攒不够/只在 run 内= None（等下轮 interim）。
    稳定性与限速由调用方门控（与标点档同判据）。
    """
    n = 0
    i = start
    while i < len(text):
        ch = text[i]
        if not ch.isspace() and not unicodedata.category(ch).startswith("P"):
            n += 1
            if n >= min_chars:
                j = i + 1
                if (
                    j < len(text)
                    and ch.isascii() and ch.isalnum()
                    and text[j].isascii() and text[j].isalnum()
                ):
                    pass  # 切点两侧都在 ASCII 字母数字 run 内：后移到 run 尾（防劈单号）
                else:
                    return j
        i += 1
    return None


def _first_block_cut(text: str, prev_full: str) -> int | None:
    """快启动层首块切点（纯函数，2026-10-09 EVS 目标；单测直喂）。

    离散提交的内在矛盾：首块要小（快出声）↔ 后续块要大（不断流）——固定门槛
    两头不可兼得（call-b3e4e391 实弹：30 字档首声 ~6s=「大块迟到」；6 字档=天窗）。
    本函数只管 **utterance 首块**（`_cc_committed_len==0`）：小门槛（任意标点边界
    ≥5 字 / 无标点保险丝 8 字）抢首声（≈语流 1.2s+管线 1.1s≈**2.3s 出声**）；
    后续块回意群档（12/15）保连续。判据复用：数字 run 保护 + 跨 interim 稳定
    （prev_full 同坐标一致）。仅 interp_lite 装配（len_fuse 旗控）——旧线零变化。"""
    from .livekit_plugins import (  # noqa: PLC0415
        _SENTENCE_STRONG_PUNCT,
        _SENTENCE_WEAK_PUNCT,
        _has_latin_or_digit_run,
    )

    i = 0
    while i < len(text):
        if text[i] in _SENTENCE_STRONG_PUNCT or text[i] in _SENTENCE_WEAK_PUNCT:
            puncts = _SENTENCE_STRONG_PUNCT + _SENTENCE_WEAK_PUNCT
            j = i + 1
            while j < len(text) and text[j] in puncts:
                j += 1
            seg = text[:j]
            if (
                len(seg) >= 5
                and not _has_latin_or_digit_run(seg, min_len=4)
                and prev_full[:j] == seg
            ):
                return j
        i += 1
    fuse = _len_fuse_cut(text, 0, 8)
    if fuse is not None and prev_full[:fuse] == text[:fuse]:
        return fuse
    return None


class DoubaoSTT(stt.STT):
    """豆包 SAUC 流式 ASR（LiveKit STT；A/B 线共用）。

    capabilities.streaming=True——本类自带 VAD 骨架流（无离线包装层）；
    interim_results=True（服务端增量文本去重后发 INTERIM_TRANSCRIPT）。
    """

    model = "doubao-asr"
    provider = "doubao"

    # PrefillSpeculator 挂点（A 线对偶件 2026-10-07）：agent.py 按会话覆写——
    # 流层在 interim 更新时以「与上一 interim 的公共前缀」为稳定前缀回调
    # （语义镜像 Qwen3ASRLiveSTT 的 PREFLIGHT 挂点：稳定前缀=下一请求 user
    # 文本的保守前缀）。类级缺省 None=零行为。
    stable_prefix_listener = None

    # B 线投机翻译(SpecMt)原文挂点(2026-10-08 W1×spec 饥饿修复,call-21739d55
    # 定案):interpret.py 按会话覆写。背景=W1 clause-commit 在 interim 更新点
    # 先跑、会话级 interim 事件只带剥掉已提交前缀的「尾巴」→ spec 检测器候选的
    # 第二次目击永远到不了(候选首见于 interim k-1,k 时被 commit 剥走)=整通零
    # 开火。本挂点让流层直接喂「上一提交坐标之后的尾巴+本次刚提交的子句」——
    # 即与「下一个 FINAL(EOS 尾巴或下一子句)」同坐标系的投机视角,刚提交子句
    # 恰好构成候选的第二次目击。类级缺省 None=零行为(A 线不设,逐字节旧路)。
    raw_interim_listener = None

    def __init__(
        self,
        *,
        api_key: str = "",
        resource_id: str = DOUBAO_RESOURCE_DEFAULT,
        ws_url: str = DOUBAO_WS_DEFAULT,
        app_id: str = "",
        access_token: str = "",
        language_state=None,
        hotword_terms: list[str] | None = None,
        vad_=None,
        speaker_lock=None,
        connect_timeout: float = _CONNECT_TIMEOUT_S,
        final_timeout: float = _FINAL_TIMEOUT_S,
        utt_merge: bool = False,
        utt_wait_s: float = 0.45,
        clause_commit: bool = False,
        len_fuse: bool = False,
        server_utterances: bool = False,
        end_window_watchdog: bool = False,
    ):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                diarization=False,
                aligned_transcript=False,
                offline_recognize=True,
                keyterms=False,
                chat_context=False,
            )
        )
        self._api_key = str(api_key or "").strip()
        self._resource_id = str(resource_id or "").strip() or DOUBAO_RESOURCE_DEFAULT
        self._ws_url = str(ws_url or "").strip() or DOUBAO_WS_DEFAULT
        self._app_id = str(app_id or "").strip()
        self._access_token = str(access_token or "").strip()
        self._language_state = language_state
        # 热词（装配点已按当通 effective 词表解析好；100 token 上限内取前 40 词，
        # 保序去重防预算被重复词吃掉）。
        self._hotword_terms = list(
            dict.fromkeys(str(t).strip() for t in (hotword_terms or []) if str(t).strip())
        )
        self._vad = vad_
        # W5 声纹锁（BOK_SPEAKER_LOCK 默认关）：每通一把、装配点传入；None=零行为。
        self._speaker_lock = speaker_lock
        self._connect_timeout = float(connect_timeout)
        self._final_timeout = float(final_timeout)
        # 尾部续说观察窗（B 线 3b，2026-10-08）：END 不立刻负 seq 定稿，先等
        # utt_wait_s——窗口内续讲=同一 WS 会话续喂（服务端 result.text 单调
        # 累积=微停顿并段），静默到底才负 seq 定稿。**B 线专用**（interpret
        # 装配点传 True；A 线不传=逐字节旧路）。观察值=说话末音后总静默
        # (VAD min_silence + utt_wait_s) 才切句：0.45+0.45=0.9s 档。
        self._utt_merge = bool(utt_merge)
        self._utt_wait_s = min(max(float(utt_wait_s or 0.0), 0.0), 3.0)
        # 说话中成句（B 线 W1 clause-commit，2026-10-08）：interim 子句级闸命中
        # 即发 FINAL（说话中 MT 起跑）；False（A 线缺省不传）=逐字节旧路。
        self._clause_commit = bool(clause_commit)
        # 长度保险丝（2026-10-09 W6×interp-lite 合流）：无标点连续语流攒够
        # ``QWEN3_ASR_CLAUSE_LEN_CHARS`` 内容字就地切（``_len_fuse_cut``）——句档
        # （逗号档关）下没有它=长句结构性等句号/EOS，两句并一单饿出巨型天窗
        # （call-d6704474 实弹 26.3s 窗根因）。**默认 False=旧线逐字节**；仅
        # interp_lite 装配开（LiteDoubaoSTT）。限速/跨窗稳定与标点档同判据。
        self._len_fuse = bool(len_fuse)
        # 服务端分句消费（2026-10-09 官方优先翻案，Ethan 拍板）：True=消费
        # show_utterances 的 definite 分句（见新即发 FINAL），本地三层闸不跑；
        # False（缺省）=本地闸档=旧线逐字节零变化。仅 interp_lite 装配传 True。
        self._server_utterances = bool(server_utterances)
        # R4-C 端窗看门狗（2026-10-09 W8-A2）：True=definite 到而本地迟迟不 END
        # （>``_END_WINDOW_WATCHDOG_S``）→ 主动走既有收段路径的病理兜底（VAD 卡死
        # 时负 seq 永远发不出=段缓冲无界增长、未 definite 尾巴永不落稿）。缺省
        # False=旧线逐字节零变化（不观测、不建任务）；仅 interp_lite 装配传 True。
        self._end_window_watchdog = bool(end_window_watchdog)
        # 与 Qwen3ASRLiveSTT 同款公开面（agent 侧 duck 访问）：partial 档旋钮、
        # 回复在途旗、收线窗旗、本轮 partial 末稿。云档语义见各方法 docstring。
        self._partial_ms_override: int | None = None
        self._reply_busy = False
        self._closing_say = False
        self._turn_partial_text: str = ""
        # 无 token 置信度（云档无此物）——轮处理器的 CSC 门读它，None=门不触发。
        self.last_confidence: dict | None = None

    # ---- 凭据/请求构造（每次连接现取；零模块级状态）----
    def _headers(self) -> dict:
        headers = {
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
            # 官方文档列为必选（任务ID，随机 UUID）；社区协议只有 Connect-Id，双发无害。
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        else:
            # 旧版控制台鉴权（APP ID + Access Token）——设置面两套字段并存，
            # 新版单 Key 优先。
            headers["X-Api-App-Key"] = self._app_id
            headers["X-Api-Access-Key"] = self._access_token
        return headers

    def _config(self) -> dict:
        request: dict = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
            "result_type": "full",
            # 方言识别开关（官方「启用中英文及方言识别」，含粤语）。缺省 false 时
            # 粤语被按普通话音系硬转（2026-10-03 实测），故常开。
            "enable_lid": True,
        }
        terms = self._hotword_terms[:40]
        if terms:
            # 直传热词（官方 corpus.context，JSON 字符串；与热词表合计 100 token 上限）。
            request["corpus"] = {
                "context": json.dumps(
                    {"hotwords": [{"word": t} for t in terms]}, ensure_ascii=False
                )
            }
        # W3b 官方三臂（2026-10-08，huoshan SAUC 文档；全部默认关、A/B 耳测定档）：
        # ① enable_nonstream 二遍识别——句末服务端重识别换更准 final（官方推荐开；
        #   收益面=ASR 误听下降+spec 确认前缀更稳；改动 final/interim 一致性面，先臂后定）。
        if os.environ.get("BOK_DOUBAO_NONSTREAM", "") == "1":
            request["enable_nonstream"] = True
        # ② enable_ddc 语义顺滑——服务端删停顿词/语气词/重复词（结巴照译的官方版；
        #   会剥语气词，与 B 线语气标记 v2 冲突 → 只做 A/B 臂，绝不缺省开）。
        if os.environ.get("BOK_DOUBAO_DDC", "") == "1":
            request["enable_ddc"] = True
        # ③ 首字加速——ASR 首个 interim 提前（代价=首字准确率；accelerate_score
        #   文档「值越大首字越快」，取 3 温和档）。
        if os.environ.get("BOK_DOUBAO_FIRST_TOKEN_BOOST", "") == "1":
            request["enable_accelerate_text"] = True
            request["accelerate_score"] = 3
        return {
            "user": {"uid": "bok-agent"},
            "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
            "request": request,
        }

    def _lang(self) -> str:
        return str(getattr(self._language_state, "lang", "") or "")

    # ---- LiveKit STT 接口 ----
    def stream(self, *, language=None, conn_options=None):
        return _DoubaoLiveStream(self, conn_options=conn_options or APIConnectOptions())

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        pcm = bytes(getattr(buffer, "data", b""))
        text = await self._transcribe_once(pcm) if pcm else ""
        alternatives = (
            [stt.SpeechData(language=self._lang(), text=text)] if text else []
        )
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT, request_id="", alternatives=alternatives
        )

    # ---- 整段单发（重试路 + offline recognize 共用）----
    async def _transcribe_once(self, pcm: bytes, timeout: float | None = None) -> str:
        if not pcm:
            return ""
        import websockets

        timeout = float(timeout if timeout is not None else self._final_timeout + 4.0)
        try:
            ws = await websockets.connect(
                self._ws_url,
                additional_headers=self._headers(),
                open_timeout=self._connect_timeout,
                max_size=20_000_000,
            )
        except Exception as exc:  # noqa: BLE001
            print("DOUBAO_ASR_CONNECT_ERROR", repr(exc), flush=True)
            return ""
        try:
            await ws.send(frame_full_client_request(self._config()))
            packets = [pcm[i:i + _PACKET_BYTES] for i in range(0, len(pcm), _PACKET_BYTES)]
            for i, packet in enumerate(packets):
                await ws.send(frame_audio(i + 2, packet, last=(i == len(packets) - 1)))
            text = ""
            deadline = time.monotonic() + timeout
            try:
                while True:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.5, deadline - time.monotonic())
                    )
                    if isinstance(raw, str):
                        continue
                    fr = parse_server_frame(bytes(raw))
                    if fr["type"] == MSG_ERROR:
                        print("DOUBAO_ASR_SERVER_ERROR code=", fr.get("code"), flush=True)
                        break
                    if fr["payload"]:
                        try:
                            j = json.loads(fr["payload"])
                        except Exception:  # noqa: BLE001
                            continue
                        t = str(((j.get("result") or {}).get("text")) or "")
                        if len(t) >= len(text):
                            text = t
                    if fr["is_last"]:
                        break
            except asyncio.TimeoutError:
                print("DOUBAO_ASR_ONESHOT_TIMEOUT", flush=True)
            return text
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print("DOUBAO_ASR_STREAM_ERROR", repr(exc), flush=True)
            return ""
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- 与 Qwen3ASRLiveSTT 同款公开面（agent 侧 duck 访问，语义见各自 docstring）----
    def set_partial_ms(self, ms: int | None) -> None:
        """会话级 partial 解码档（本地侧 GPU 竞态旋钮）——云档无对应物，只记不用。"""
        self._partial_ms_override = ms

    def set_reply_busy(self, busy: bool) -> None:
        """回复在途旗（本地侧迟到 FINAL 护栏读它）——云档终稿来自服务端无重解，
        v1 只记不用（保留字段供后续云侧守卫扩展）。"""
        self._reply_busy = bool(busy)

    def set_closing_say(self, on: bool) -> None:
        """收线/告别直念窗：True 期间流整段丢弃（不发 EOS/FINAL，把告别说完）。"""
        self._closing_say = bool(on)

    def last_partial_text(self) -> str:
        """本轮 ASR partial 末稿（E2 热词泄漏清洗的 fallback_text 取口）。

        契约与 Qwen3 版同款：只反映本轮（新语音段开场即清；该轮无 FINAL 发出时
        为空）；读取恒安全（纯属性读）。云档写点=interim 文本更新 + FINAL 重贴。
        """
        return str(self._turn_partial_text or "")


class _DoubaoLiveStream(stt.RecognizeStream):
    """VAD 骨架 + 段内 WS 会话的豆包流式转写。

    生命周期：START_OF_SPEECH 开 WS（配置帧 seq=1）→ 说话中 200ms 分包喂音频
    → END_OF_SPEECH 发末包（负 seq）→ 等 is_last 收全文 → FINAL_TRANSCRIPT。
    收线窗（_closing_say）整段丢弃；live 路失败且无文本时整段单发重试一次。

    **尾部续说观察窗（utt 档，B 线 3b 2026-10-08，stt_._utt_merge=False=零行为）**：
    END_OF_SPEECH 不立刻负 seq 定稿——先观察 utt_wait_s：窗口内 START（续讲）
    =微停顿并段（同会话续喂，服务端 result.text 单调累积=天然并稿，不重开 WS）；
    静默到底=负 seq 定稿整段出 FINAL。有效切句边界=说话末音后
    (VAD min_silence + utt_wait_s) 总静默（B 线 0.45+0.45=0.9s 档），微停顿不再
    一停一句。等待期不喂帧（服务端不计静音、不提前 end_window，窗口主权在本地）。
    """

    def __init__(self, stt_: DoubaoSTT, *, conn_options):
        super().__init__(stt=stt_, conn_options=conn_options, sample_rate=16000)
        self._stt_ = stt_
        self._vad = stt_._vad
        # 段状态（_reset_segment 清）
        self._seg_pcm = bytearray()      # 整段 PCM（重试缓冲，上限 60s）
        self._last_server_text = ""      # 段内最长已见文本（result.text 单调）
        self._last_interim_emitted = ""  # INTERIM 去重
        self._session_error = ""
        self._finishing = False
        self._saw_last = False           # 本段是否收到服务端末包（正常定稿）
        # W5 声纹门（总闸关=零行为）：VAD 段 pre-ASR 把门，drop 段不发包不成轮。
        self._gate = SegmentSpeakerGate(getattr(stt_, "_speaker_lock", None))
        # 会话状态
        self._ws = None
        self._send_q: asyncio.Queue | None = None
        self._sender_task: asyncio.Task | None = None
        self._receiver_task: asyncio.Task | None = None
        self._final_evt: asyncio.Event | None = None
        self._session_alive = False
        # 尾部续说观察窗（B 线 3b；stt_._utt_merge=False=零行为逐字节旧路）
        self._utt = bool(getattr(stt_, "_utt_merge", False))
        self._utt_wait = float(getattr(stt_, "_utt_wait_s", 0.0) or 0.0)
        self._tailing = False            # END 已发、尾窗观察中（INFERENCE 喂入停）
        self._tail_task: asyncio.Task | None = None
        self._tail_finalizing = False    # 尾窗已到期、负 seq 定稿在途（START 让路）
        self._utt_merges = 0             # 本 utterance 内并掉的微停顿数（观测）
        # 说话中成句状态（clause_commit，B 线 W1）：已提交前缀坐标（全文char）、
        # 已提交前缀原文（对齐基线）、限速钟（跨段保留=A 线同款）、段锚钟与
        # 提交计数（观测；_reset_segment 清）。
        self._clause_commit = bool(getattr(stt_, "_clause_commit", False))
        self._cc_committed_len = 0
        self._cc_committed_text = ""
        self._cc_last_commit_at = 0.0
        self._cc_seg_t0 = 0.0
        self._cc_commits = 0
        # 服务端分句消费（2026-10-09 官方优先翻案，Ethan 拍板「让 ASR 自己切分
        # 然后给 LLM」）：show_utterances 的 definite 分句=厂商卖的语义分段，
        # 见新 definite 即发 FINAL——本地标点/保险丝/快启动三层闸全部让位
        # （它们是今天一天在手工重建厂商已有的能力，碎片/大块/吞字三病全由此生）。
        # stt_._server_utterances=False（缺省）=本地闸档=旧线逐字节零变化。
        self._server_utt = bool(getattr(stt_, "_server_utterances", False))
        self._su_emitted = 0  # 已发 FINAL 的 definite 分句计数（按序单调）
        # R4-C 端窗看门狗（W8-A2；stt_._end_window_watchdog=False=零行为）：
        # 段内 definite 计数/锚钟与 definite 合计文本（冻结判据=全文不再超出该
        # 合计——definite 常与全文增长同帧到达，钟面比较会假阳，按内容比）。
        self._wd_enabled = bool(getattr(stt_, "_end_window_watchdog", False))
        self._wd_task: asyncio.Task | None = None
        self._wd_definite = 0
        self._wd_definite_at = 0.0
        self._wd_definite_text = ""

    # ---- 会话管理 ----
    async def _open_session(self) -> None:
        if not _ws_host_ok(self._stt_._ws_url):
            self._session_error = "ws_url_rejected"
            print(f"DOUBAO_ASR_URL_REJECTED {self._stt_._ws_url!r}", flush=True)
            return
        import websockets

        try:
            ws = await websockets.connect(
                self._stt_._ws_url,
                additional_headers=self._stt_._headers(),
                open_timeout=self._stt_._connect_timeout,
                max_size=20_000_000,
            )
        except Exception as exc:  # noqa: BLE001
            self._session_error = f"connect:{exc!r}"
            print("DOUBAO_ASR_CONNECT_ERROR", repr(exc), flush=True)
            return
        try:
            await ws.send(frame_full_client_request(self._stt_._config()))
        except Exception as exc:  # noqa: BLE001
            self._session_error = f"config:{exc!r}"
            print("DOUBAO_ASR_CONFIG_ERROR", repr(exc), flush=True)
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            return
        self._ws = ws
        self._send_q = asyncio.Queue()
        self._final_evt = asyncio.Event()
        self._session_alive = True
        # 持引用任务（禁裸 create_task：异常回收 + 关闭时 cancel 需要句柄）。
        self._sender_task = asyncio.create_task(self._sender(ws))
        self._receiver_task = asyncio.create_task(self._receiver(ws))
        print("DOUBAO_ASR_CONNECT ok", flush=True)

    async def _close_session(self) -> None:
        self._session_alive = False
        tasks = [t for t in (self._sender_task, self._receiver_task) if t is not None]
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sender_task = None
        self._receiver_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None

    async def _sender(self, ws) -> None:
        """音频泵：队列收 PCM → 200ms 分包 → 正 seq 帧；None 哨兵 = 末包（负 seq）。"""
        seq = 2
        buf = bytearray()
        while True:
            item = await self._send_q.get()
            if item is None:
                # 末包：余量（可能为空）强制服务端立即定稿。
                await ws.send(frame_audio(seq, bytes(buf), last=True))
                return
            buf.extend(item)
            while len(buf) >= _PACKET_BYTES:
                packet = bytes(buf[:_PACKET_BYTES])
                del buf[:_PACKET_BYTES]
                await ws.send(frame_audio(seq, packet, last=False))
                seq += 1

    async def _receiver(self, ws) -> None:
        try:
            while True:
                raw = await ws.recv()
                if isinstance(raw, str):
                    continue
                fr = parse_server_frame(bytes(raw))
                if fr["type"] == MSG_ERROR:
                    self._session_error = f"server:{fr.get('code')}"
                    print(f"DOUBAO_ASR_SERVER_ERROR code={fr.get('code')}", flush=True)
                    break
                if fr["payload"]:
                    try:
                        j = json.loads(fr["payload"])
                    except Exception:  # noqa: BLE001
                        j = None
                    if isinstance(j, dict):
                        self._on_payload(j)
                if fr["is_last"]:
                    self._saw_last = True
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._session_error = self._session_error or f"recv:{exc!r}"
        finally:
            if self._final_evt is not None:
                self._final_evt.set()

    def _on_payload(self, j: dict) -> None:
        res = j.get("result") or {}
        if self._wd_enabled:
            self._wd_observe(res)
        if self._server_utt:
            self._server_definite_commits(res)
        text = str(res.get("text") or "")
        if text and len(text) >= len(self._last_server_text):
            # 单调累积（实弹取证）；防御性取最长，防服务端变体重置。
            self._last_server_text = text
            self._maybe_interim(text)

    def _wd_observe(self, res: dict) -> None:
        """端窗看门狗观测（R4-C，纯记账零副作用）：definite 分句计数只增不减
        （服务端 utterances 全量重发=按数取增量），增量时刻记锚钟、增量瞬间把
        definite 合计文本快照下来（冻结判据=全文不再超出该合计——definite 常与
        全文增长同帧到达，钟面比较会假阳，按内容比）。"""
        definite = [
            u for u in (res.get("utterances") or [])
            if u.get("definite") and str(u.get("text") or "").strip()
        ]
        if len(definite) > self._wd_definite:
            self._wd_definite = len(definite)
            self._wd_definite_at = time.monotonic()
            self._wd_definite_text = "".join(str(u.get("text") or "") for u in definite)

    def _wd_arm(self) -> None:
        """段开武装（START 分支调用）：记账清零 + 起看门狗任务；未启用=零行为。"""
        if not self._wd_enabled:
            return
        self._wd_cancel()
        self._wd_definite = 0
        self._wd_definite_at = 0.0
        self._wd_definite_text = ""
        self._wd_task = asyncio.create_task(self._end_window_watch())

    def _wd_cancel(self) -> None:
        """收看门狗任务（END/收线窗/声纹丢段/流关闭共用；未启/已停=无害 noop）。"""
        t, self._wd_task = self._wd_task, None
        if t is not None and not t.done():
            t.cancel()

    async def _end_window_watch(self) -> None:
        """端窗看门狗循环（R4-C「VAD 卡死」病理兜底，2026-10-09 W8-A2）。

        前提（P2 实证）：豆包 SAUC 不显式设 ``end_window_size`` 时缺省 ~3044ms 才
        definite（lite 显式下发后 definite 会先于本地 END 到达）。开火判据=**喂帧
        中**（会话活、非定稿在途、非尾窗）且 definite 增量出现过、且 definite 之后
        服务端再无任何新内容（全文冻结=服务端听到的是静音）、且超宽限本地仍未
        END——即「服务端已判停、本地 VAD 卡在 speaking」的病理段 → 主动走既有
        :meth:`_finalize_utterance` 收段（负 seq 定稿+FINAL+复位），复用既有生命
        周期不新造。真语音续行（definite 后 text 仍增长）恒让位——看门狗绝不拦腰
        切活语音。任务收尾姿势镜像 ``_tail_watch``（CancelledError 静默退、异常
        复位不阻后续段）。"""
        try:
            while True:
                await asyncio.sleep(_END_WINDOW_WATCHDOG_TICK_S)
                if not self._session_alive or self._finishing or self._tailing:
                    return
                if self._wd_definite <= 0 or self._wd_definite_at <= 0.0:
                    continue
                if str(self._last_server_text or "").strip() != self._wd_definite_text.strip():
                    continue  # 全文超出 definite 合计=仍有未提交内容（真语音续行）
                if time.monotonic() - self._wd_definite_at <= _END_WINDOW_WATCHDOG_S:
                    continue
                break
        except asyncio.CancelledError:
            return
        print(f"[doubao] END_WINDOW watchdog fired definite={self._wd_definite}", flush=True)
        # 单一收段者：若 VAD END 恰在此刻落地武装了尾窗，取消之（残留由本次
        # 收段一并负 seq 定稿，双 finalizer 不会同时飞行）。
        self._cancel_tail()
        self._finishing = True
        try:
            await self._finalize_utterance()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 看门狗收段失败不阻后续段
            print(f"DOUBAO_END_WINDOW_WATCH_ERROR {exc!r}", flush=True)
            self._finishing = False
            self._reset_segment()

    def _server_definite_commits(self, res: dict) -> None:
        """服务端 definite 分句消费（官方优先翻案 2026-10-09）。

        ``show_utterances`` 响应里 ``utterances[].definite=True`` 即服务端已定稿
        的语义分句（其 VAD+语义模型决定边界，``end_window_size`` 调灵敏度）——
        见新 definite 即发 FINAL 进翻译；committed 记账沿 ``_cc_committed_*``
        （display 剥除与 EOS 尾巴共用既有对齐梯）。本地三层闸（标点/保险丝/
        快启动）在此档全部不跑——那是手工重建厂商能力的三层劣化补丁。"""
        utts = res.get("utterances") or []
        definite = [u for u in utts if u.get("definite") and str(u.get("text") or "").strip()]
        new = definite[self._su_emitted:]
        if not new:
            return
        self._su_emitted = len(definite)
        for u in new:
            utext = str(u.get("text") or "").strip()
            if not utext:
                continue
            self._cc_committed_text = (self._cc_committed_text or "") + utext
            self._cc_committed_len = len(self._cc_committed_text)
            self._cc_commits += 1
            try:
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        alternatives=[stt.SpeechData(language=self._stt_._lang(), text=utext)],
                    )
                )
            except Exception:  # noqa: BLE001 - 流已关：迟到分句丢弃
                continue
            print(
                f"[doubao] UTTERANCE definite chars={len(utext)} "
                f"prefix_len={self._cc_committed_len} total={self._su_emitted}",
                flush=True,
            )

    def _maybe_interim(self, text: str) -> None:
        if not text or text == self._last_interim_emitted:
            return
        if self._gate.dropped:
            return  # 声纹门已判丢：interim 也不外泄（喂 LLM 抢跑/字幕全免）
        # PrefillSpeculator 稳定前缀喂点（A 线对偶件 2026-10-07）：与上一 interim
        # 的公共前缀=尚未被服务端修订的保守前缀（后续请求 user 文本的稳定头）。
        # 回调异常绝不影响 interim 事件流（镜像 Qwen3 侧挂点纪律）。
        _prev = self._last_interim_emitted
        self._last_interim_emitted = text
        _spec_cb = getattr(self._stt_, "stable_prefix_listener", None)
        if _spec_cb is not None and _prev:
            _common_len = 0
            for a, b in zip(_prev, text):
                if a != b:
                    break
                _common_len += 1
            if _common_len:
                try:
                    _spec_cb(_prev[:_common_len])
                except Exception as exc:  # noqa: BLE001
                    print(f"BOK_PREFILL_SPEC listener error: {exc!r}", flush=True)
        # 说话中成句（B 线 W1 clause-commit）：闸命中即发 FINAL（说话中 MT 起跑）。
        # 事件序=FINAL(已稳定子句) → INTERIM(未提交剩余)；关旗=零行为逐字节旧路。
        # spec 原文挂点(饥饿修复)喂「上一提交坐标之后的尾巴+本次刚提交子句」:
        # 本次 commit **之前**的坐标快照——候选第二次目击恰好在 commit 发生的
        # 那个 interim 可见(会话级 display 已剥走),而后续 interim 的 spec 视角
        # 自动回到「下一 FINAL 同坐标系」(余段视角),不会对已提交子句重复开火。
        _spec_feed = text
        if self._server_utt:
            # 服务端分句档：commit 由 _server_definite_commits 驱动（definite 即 FINAL），
            # 本地三层闸不跑；display=全文剥已发 definite 前缀（startswith 主路，
            # 失配显示全文——服务端驱动下罕见，对齐兜底在 EOS 路径）。
            if self._cc_committed_text and text.startswith(self._cc_committed_text):
                display = text[self._cc_committed_len:]
            else:
                display = text
        elif self._clause_commit:
            _spec_base = self._cc_committed_len
            self._maybe_clause_commit(text, _prev)
            display = self._clause_tail(text)
            _spec_feed = text[_spec_base:]
            if not display:
                _emit_spec_feed(self._stt_, _spec_feed)
                return  # 已见文本全部提交/对齐重置吞显：本轮无剩余可出
        else:
            display = text
        _emit_spec_feed(self._stt_, _spec_feed)
        try:
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language=self._stt_._lang(), text=display)],
                )
            )
        except Exception:  # noqa: BLE001 - 流已关：迟到 interim 丢弃
            pass

    def _maybe_clause_commit(self, text: str, prev_full: str) -> None:
        """interim 更新点的子句级提交：``_find_clause_cut`` 命中即发 FINAL。

        坐标系=服务端全文（result.text 单调累积）；已提交前缀记账在
        ``_cc_committed_len/_cc_committed_text``，后续 interim 展示与 EOS 尾巴
        都经 :meth:`_clause_tail` 对齐剥除。``lag_ms``=首个 interim 进闸到本次
        提交的毫秒（utterance 内提交时点观测）。"""
        now = time.monotonic()
        if self._cc_seg_t0 <= 0.0:
            self._cc_seg_t0 = now
        cut = _find_clause_cut(
            text,
            self._cc_committed_len,
            prev_full,
            last_commit_at=self._cc_last_commit_at,
            now=now,
        )
        if cut is None and self._stt_._len_fuse and self._cc_committed_len == 0 and self._cc_commits == 0:
            # 快启动层（EVS 目标，2026-10-09）：utterance 首块小门槛抢首声
            # （~2.3s vs 意群档 ~3s）；后续块回意群档。len_fuse 旗控=旧线零变化。
            fast = _first_block_cut(text, prev_full)
            if fast is not None:
                cut = fast
        if cut is None and self._stt_._len_fuse:
            # 长度保险丝（标点档未命中才问；限速/跨窗稳定与标点档同判据；稳定
            # 判据=**归一化**比较——ASR 回溯改标点不再废稳定性，call-ed6326a5
            # 实弹：raw 比较下保险丝在首火后即被标点改写持续打哑）。
            from .livekit_plugins import (  # noqa: PLC0415
                _sentence_commit_min_interval_s,
                _strip_punct_space,
            )

            try:
                min_chars = int(os.environ.get("QWEN3_ASR_CLAUSE_LEN_CHARS", "20") or 20)
            except ValueError:
                min_chars = 20
            fuse = _len_fuse_cut(text, self._cc_committed_len, max(1, min_chars))
            if (
                fuse is not None
                and now - self._cc_last_commit_at >= _sentence_commit_min_interval_s()
                and _strip_punct_space(prev_full[self._cc_committed_len:fuse])
                == _strip_punct_space(text[self._cc_committed_len:fuse])
            ):
                cut = fuse
        if cut is None:
            return
        clause = text[self._cc_committed_len:cut]
        self._cc_committed_len = cut
        self._cc_committed_text = text[:cut]
        self._cc_last_commit_at = now
        self._cc_commits += 1
        try:
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[
                        stt.SpeechData(language=self._stt_._lang(), text=clause)
                    ],
                )
            )
        except Exception:  # noqa: BLE001 - 流已关：迟到 commit 丢弃
            return
        print(
            f"[doubao] CLAUSE_COMMIT chars={len(clause)} "
            f"lag_ms={(now - self._cc_seg_t0) * 1000:.0f} prefix_len={self._cc_committed_len}",
            flush=True,
        )

    def _clause_tail(self, text: str) -> str:
        """committed-prefix 对齐：剥已提交前缀后的剩余尾巴（interim 展示与
        EOS 定稿共用同一坐标系）。

        台阶（A 线 ``_uncommitted`` 同思想；HF speech-to-speech snapshot 纪律
        ——partial 修订**绝不回滚已发出的 FINAL**）：
        ① 严格前缀 startswith（主路径：单调累积极少改写已稳定头）；
        ② 归一化前缀（去标点/空白逐字对齐——服务端标点改写不算新内容）；
        ③ 双失配 → **交差量再重置**（2026-10-09 修订，call-ed6326a5 实弹：旧版
           静默吞字——句档/保险丝下 ASR 回溯改字使双失配必现，每句吞 20-35 字）：
           找最长公共归一前缀 p，把 p 之后的新内容当尾巴交出去（发 FINAL 落账），
           然后重置领地 committed=全文。旧取舍「宁漏勿重」作废——同传漏句不可
           接受；修订重叠处的极小重复可接受且有观测行。
        """
        if not self._cc_committed_text:
            return text
        if text.startswith(self._cc_committed_text):
            return text[self._cc_committed_len:]
        from .livekit_plugins import (  # noqa: PLC0415 - 同 _find_clause_cut 懒 import
            _UNCOMMITTED_LEADING_WEAK_PUNCT,
            _strip_punct_space,
        )

        norm_c = _strip_punct_space(self._cc_committed_text)
        norm_t = _strip_punct_space(text)
        if norm_c and norm_t.startswith(norm_c):
            # 归一化对齐截尾：raw 里跳过标点/空白吃掉 norm_c 长度的正字，剩余是真尾巴。
            need = len(norm_c)
            for i, ch in enumerate(text):
                if ch.isspace() or unicodedata.category(ch).startswith("P"):
                    continue
                need -= 1
                if need == 0:
                    return text[i + 1:].lstrip(_UNCOMMITTED_LEADING_WEAK_PUNCT)
            return ""
        print(
            f"DOUBAO_CLAUSE_ALIGN_RESET committed={self._cc_committed_text[:40]!r} "
            f"text={text[:40]!r}",
            flush=True,
        )
        # 交差量：p=最长公共归一前缀；norm_t[p:] 是未交过的新内容 → **直接发
        # FINAL**（差量只在 INTERIM 显示=MT 仍吃不到，吞字没修透）；然后重置
        # 领地 committed=全文、display 空。旧取舍「宁漏勿重」作废——同传漏句
        # 不可接受；修订重叠处的极小重复可接受且有 DELTA 观测行。
        p = 0
        while p < min(len(norm_c), len(norm_t)) and norm_c[p] == norm_t[p]:
            p += 1
        delta = ""
        if p < len(norm_t):
            need = p
            raw_p = None
            for i, ch in enumerate(text):
                if ch.isspace() or unicodedata.category(ch).startswith("P"):
                    continue
                if need == 0:
                    raw_p = i
                    break
                need -= 1
            if raw_p is not None and raw_p < len(text):
                delta = text[raw_p:].lstrip(_UNCOMMITTED_LEADING_WEAK_PUNCT)
        self._cc_committed_text = text
        self._cc_committed_len = len(text)
        if delta:
            print(f"DOUBAO_CLAUSE_ALIGN_DELTA chars={len(delta)}", flush=True)
            try:
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        alternatives=[stt.SpeechData(language=self._stt_._lang(), text=delta)],
                    )
                )
            except Exception:  # noqa: BLE001 - 流已关：差量丢弃（下轮 EOS 兜底整段）
                pass
        return ""

    def _feed(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._seg_pcm.extend(pcm)
        if len(self._seg_pcm) > _SEG_CAP_BYTES:
            del self._seg_pcm[: len(self._seg_pcm) - _SEG_CAP_BYTES]
        if not self._gate.feed(pcm):
            return  # 声纹门判丢：不再喂 WS（服务端空闲，段末关会话整段抑制）
        if self._session_alive and self._send_q is not None:
            self._send_q.put_nowait(pcm)

    async def _finish_segment(self) -> str:
        """末包 → 等定稿 → （未见末包时）整段单发重试。返回全文（可能空串）。

        重试判据=``not _saw_last``（连接失败/中途死/超时/服务端错——此时手里的
        文本可能是半截稿）；正常末包（含合法空转写）不重试，绝不重复烧钱。
        """
        text = ""
        if self._session_alive and self._send_q is not None and self._final_evt is not None:
            self._send_q.put_nowait(None)
            try:
                await asyncio.wait_for(self._final_evt.wait(), timeout=self._stt_._final_timeout)
            except asyncio.TimeoutError:
                self._session_error = self._session_error or "final_timeout"
                print("DOUBAO_ASR_FINAL_TIMEOUT", flush=True)
            text = self._last_server_text
        await self._close_session()
        if self._seg_pcm and not self._saw_last:
            print(
                f"DOUBAO_ASR_RETRY cause={(self._session_error or 'no_last')[:80]} "
                f"seg_ms={len(self._seg_pcm) // 32}",
                flush=True,
            )
            retry_text = await self._stt_._transcribe_once(bytes(self._seg_pcm))
            if len(retry_text) >= len(text):
                text = retry_text
        return text

    def _reset_segment(self) -> None:
        self._seg_pcm.clear()
        self._last_server_text = ""
        self._last_interim_emitted = ""
        self._session_error = ""
        self._saw_last = False
        # 说话中成句坐标随段清零（限速钟 _cc_last_commit_at 跨段保留=A 线同款）。
        self._cc_committed_len = 0
        self._cc_committed_text = ""
        self._cc_seg_t0 = 0.0
        self._cc_commits = 0
        self._su_emitted = 0  # 服务端分句计数随段清（新 utterance 从头数）
        # 暴露位随段清零；FINAL 发出点按 pre-reset 快照重贴（与 Qwen3 版契约一致）。
        self._stt_._turn_partial_text = ""

    async def _finalize_utterance(self) -> None:
        """负 seq 定稿 → FINAL → 声纹登记 → 段复位（legacy 内联路=utt 尾窗到期路共用）。

        与旧 END 内联路径逐字节同序：快照 partial → `_finish_segment`（末包→等
        is_last→失败整段单发重试）→ FINAL 事件 → DOUBAO_ASR_TEXT 行（utt 档追加
        utt_merges 列）→ gate.segment_end 灰区复核 → 复位 → pre-reset 快照重贴。
        """
        t0 = time.monotonic()
        partial_snapshot = self._last_server_text
        text = await self._finish_segment()
        # 说话中成句（B 线 W1）：已提交子句不随定稿重复入历史——FINAL 只补
        # 未提交尾巴（committed-prefix 对齐；可能为空则不发）。关旗=全文逐字节旧路。
        tail = self._clause_tail(text) if (self._clause_commit and text) else text
        if tail:
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[
                        stt.SpeechData(language=self._stt_._lang(), text=tail)
                    ],
                )
            )
        suffix = f" utt_merges={self._utt_merges}" if self._utt else ""
        if self._clause_commit and self._cc_commits:
            suffix += f" clause_commits={self._cc_commits}"
        print(
            f"DOUBAO_ASR_TEXT {text[:120]!r} {self._stt_._lang()} "
            f"ASR_MS={(time.monotonic() - t0) * 1000:.0f}(cloud){suffix}",
            flush=True,
        )
        # 段末收口：灰区整段复核判丢→吞 FINAL（幻听轮不成）；登记钩照走。
        if not self._gate.segment_end(text):
            text = ""
        self._finishing = False
        self._utt_merges = 0
        self._reset_segment()
        if text and partial_snapshot:
            self._stt_._turn_partial_text = partial_snapshot

    async def _tail_watch(self) -> None:
        """尾部续说观察窗（B 线 3b）：睡满 utt_wait_s=真停顿→负 seq 定稿整段。

        被取消=窗口内续讲（START 分支 cancel）→ 同会话续喂，微停顿并段；
        服务端 ``result.text`` 单调累积（实弹定案），续段文本天然并进同一稿。
        """
        try:
            await asyncio.sleep(self._utt_wait)
        except asyncio.CancelledError:
            return
        # 尾窗到期：此后不再可并段——START 若撞上，让它等定稿走完再开新会话。
        self._tail_finalizing = True
        self._tail_task = None
        self._tailing = False
        self._finishing = True
        try:
            await self._finalize_utterance()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 尾窗定稿失败不阻后续段
            print(f"DOUBAO_UTT_TAIL_ERROR {exc!r}", flush=True)
            self._finishing = False
            self._reset_segment()

    # ---- 主循环（VAD 双任务骨架，与 _Qwen3ASRLiveStream 同构）----
    async def _run(self) -> None:
        vad_stream = self._vad.stream()

        async def _forward_input() -> None:
            async for input in self._input_ch:
                if isinstance(input, self._FlushSentinel):
                    vad_stream.flush()
                    continue
                vad_stream.push_frame(input)
            vad_stream.end_input()

        async def _recognize() -> None:
            started = False
            async for event in vad_stream:
                if event.type == vad.VADEventType.START_OF_SPEECH:
                    # 尾部续说观察窗内续讲=微停顿并段（B 线 3b）：取消尾窗、
                    # 同会话续喂（不重开 WS——服务端 text 单调累积天然并稿）。
                    # 尾窗已到期、定稿在途=不可并段：等定稿走完（会话已关），
                    # 本次 START 走新会话路（停顿≥窗口=真 utterance 边界）。
                    if self._tail_task is not None or self._tail_finalizing:
                        t, self._tail_task = self._tail_task, None
                        if self._tail_finalizing:
                            if t is not None:
                                try:
                                    await t
                                except asyncio.CancelledError:
                                    pass
                                except Exception:  # noqa: BLE001 - 定稿异常已由尾窗记录
                                    pass
                            self._tail_finalizing = False
                            self._tailing = False
                        else:
                            t.cancel()
                            try:
                                await t
                            except asyncio.CancelledError:
                                pass
                            except Exception:  # noqa: BLE001
                                pass
                            self._tailing = False
                            self._utt_merges += 1
                            print("DOUBAO_UTT_MERGE resume", flush=True)
                    started = True
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(stt.SpeechEventType.START_OF_SPEECH)
                    )
                    self._gate.segment_start()
                    # R4-C 端窗看门狗：段开武装（记账清零+起任务；未启用=零行为）。
                    self._wd_arm()
                    # legacy=每段必开新会话（段末已关）；utt=会话跨停顿存活，
                    # 仅在确无活会话时开（续说复用=并段的关键）。
                    if not self._session_alive:
                        await self._open_session()
                    if event.frames:
                        # 前导喂会话（silero prefix padding + min_speech 确认窗帧）
                        try:
                            self._feed(bytes(utils.merge_frames(event.frames).data))
                        except Exception:  # noqa: BLE001 - 合帧失败不致命
                            pass
                elif event.type == vad.VADEventType.INFERENCE_DONE:
                    if not started or self._finishing or self._tailing:
                        continue
                    try:
                        self._feed(bytes(utils.merge_frames(event.frames).data))
                    except Exception:  # noqa: BLE001
                        continue
                elif event.type == vad.VADEventType.END_OF_SPEECH:
                    if not started:
                        continue
                    # R4-C 端窗看门狗：本地 END 先到=段正常收口，看门狗下岗。
                    self._wd_cancel()
                    # 收线/告别直念窗：整段丢弃（不发 EOS/FINAL，把告别说完）。
                    if bool(getattr(self._stt_, "_closing_say", False)):
                        started = False
                        self._finishing = False
                        self._cancel_tail()
                        self._reset_segment()
                        print("DOUBAO_ASR_CLOSING_SAY_SUPPRESS src=segment_eos", flush=True)
                        continue
                    # ---- W5 声纹门：本段已判非通话对象 → 整段抑制（镜像收线窗语义：
                    # 不发 EOS、不 finish、不重试、不出 FINAL；关会话止损）。----
                    if self._gate.dropped:
                        started = False
                        self._finishing = False
                        self._cancel_tail()
                        await self._close_session()
                        self._gate.segment_end("")
                        self._reset_segment()
                        continue
                    self._finishing = True
                    speech_end_time = (
                        time.time() - event.silence_duration - event.inference_duration
                    )
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.END_OF_SPEECH,
                            speech_end_time=speech_end_time,
                        )
                    )
                    # ---- 尾部续说观察窗（B 线 3b）：END 不立刻定稿——窗口内续讲
                    # 并段（服务端单调累积），静默到底负 seq 定稿。等待期不喂帧
                    # （服务端不计静音、不提前 end_window，窗口主权在我们）。----
                    if self._utt and self._utt_wait > 0:
                        self._finishing = False
                        self._tailing = True
                        started = False
                        self._tail_task = asyncio.create_task(self._tail_watch())
                        continue
                    await self._finalize_utterance()
                    started = False

        try:
            await asyncio.gather(_forward_input(), _recognize())
        finally:
            self._cancel_tail()
            self._wd_cancel()
            await self._close_session()

    def _cancel_tail(self) -> None:
        """取消尾窗任务（续讲/收线/声纹丢段/流关闭共用；已到期=无害 noop）。"""
        t, self._tail_task = self._tail_task, None
        if t is not None and not t.done():
            t.cancel()
        self._tailing = False
