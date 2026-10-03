"""Bok patch: 豆包 SAUC STT 参数（官方协议凭据与开关；字段带 doubao_stt_ 前缀，
config_prefix 剥前缀后传 handler.setup）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DoubaoSTTHandlerArguments:
    doubao_stt_api_key: str = field(
        default="",
        metadata={"help": "火山引擎豆包新版单 Key（X-Api-Key）。留空回退 app_id+access_token 或 env DOUBAO_STT_API_KEY。"},
    )
    doubao_stt_app_id: str = field(
        default="",
        metadata={"help": "旧版控制台 APP ID（X-Api-App-Key）。"},
    )
    doubao_stt_access_token: str = field(
        default="",
        metadata={"help": "旧版控制台 Access Token（X-Api-Access-Key）。"},
    )
    doubao_stt_resource_id: str = field(
        default="volc.seedasr.sauc.duration",
        metadata={"help": "SAUC 资源 ID：volc.seedasr.sauc.duration（2.0 推荐）/ volc.bigasr.sauc.duration（1.0）。"},
    )
    doubao_stt_ws_url: str = field(
        default="wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
        metadata={"help": "SAUC 端点（仅公网 wss/https，SSRF 护栏校验）。"},
    )
    doubao_stt_hotwords: str = field(
        default="",
        metadata={"help": "热词（空格/逗号分隔，取前 40；走官方 request.corpus.context）。"},
    )
    doubao_stt_connect_timeout_s: float = field(
        default=8.0, metadata={"help": "WS 连接超时（秒）。"},
    )
    doubao_stt_final_timeout_s: float = field(
        default=6.0, metadata={"help": "末包定稿等待（秒）。"},
    )
    doubao_stt_packet_ms: int = field(
        default=200, metadata={"help": "音频分包粒度（毫秒）。"},
    )
