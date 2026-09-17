from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from bok_voice_core.types import CallMode, CallStatus, Role, SettlementStatus


class TokenRequest(BaseModel):
    account_id: str = "acc-001"
    object_id: str = ""
    call_id: str = ""
    # 参与者角色:operator(默认,客服操作端) / me(同传我方端,创建房间并挂 agent 分发) /
    # other(同传对方端) / supervisor(主管旁听)。官方契约路径(participant_identity
    # 前缀)优先于本字段。
    role: str = "operator"
    # 签发用途:""=正常入房 / "listen"=主管静默旁听专线(can_publish 全关,
    # 不加 RoomConfiguration——主管不是房间创建者,不得建房/拉起 agent)。
    purpose: str = ""
    # ---- LiveKit 官方 TokenSource endpoint 契约(livekit_token_source.proto,
    # snake_case 请求体)。room_name/participant_identity 提供时优先于旧字段。
    room_name: str = ""
    participant_identity: str = ""
    participant_name: str = ""
    participant_metadata: str = ""


class TokenResponse(BaseModel):
    # LiveKit 官方 TokenSourceResponse 契约字段(proto JSON camelCase——官方
    # development token server 同款;客户端 fromJson 双向兼容 snake/camel)。
    # TokenSource.endpoint/custom 自此直连本端点,不再需要键名映射层。
    serverUrl: str = "ws://127.0.0.1:7880"
    participantToken: str = ""


class ListenStopRequest(BaseModel):
    """旁听结束回执：seconds=本次旁听时长（前端尽力而为，取不到传 0）。"""

    seconds: int = 0


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    # 最低 8 位，校验在端点侧（scrypt hash 见 control_plane/auth.py）。
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"  # root/admin/user
    org_id: str = ""
    account_id: str = ""
    display_name: str = ""
    # B4 页面权限（仅 role=user 目标）：键 ⊆ permissions.GRANTABLE_PERMISSIONS；
    # None=不写（存 '' → 读侧默认集），list 含 '[]' 时=全关。
    permissions: list[str] | None = None


class UpdateUserRequest(BaseModel):
    password: str = ""  # 非空=重置密码
    status: str = ""  # active/disabled
    display_name: str = ""
    role: str = ""  # 仅 root 可改
    # B4：None=不改权限（与 '' 区分——空 list 是「全关」这一显式意图）。
    permissions: list[str] | None = None


class CreateCallRequest(BaseModel):
    account_id: str
    # 同传会话(kind=interpret)没有客服对象,允许空。
    object_id: str = ""
    persona_id: str = ""
    # 显式话术(建单即快照):外呼战役/话务员自选话术走这里;空=回落对象卡绑定。
    template_id: str = ""
    mode: CallMode = CallMode.SIMULATION
    direction: str = "webrtc"
    language: str = "zh"
    tts_reference_voice: str = ""
    # 会话种类:""=客服通话 / interpret=双端同传(B 线 v2)。target_lang=对方语言。
    kind: str = ""
    target_lang: str = ""
    # 通话绑定节点(site-delivery M1,2026-09-16 thin-node 拓扑):建单钉死承载节点,
    # /api/token 签发前校验其未吊销。''=无绑定(单机全栈形态零变化);未知节点 404、
    # revoked 节点 403 在 create_call 端点校验。
    node_id: str = ""
    # B 线同传术语表(P0-2):「源=译」或纯词条,逗号/分号/换行分隔;1000 字上限
    # 在 _create_call_in 截断(agent 侧另有 400 字 prompt 护栏)。客服通话忽略。
    glossary: str = ""
    # B 线会话级音色(2026-09-17):同传页建单我方/对方语言各选一把 MiniMax 音色,
    # JSON map `{"zh":"voice_id",...}`;512 字上限在 _create_call_in 截断。空=跟随设置。
    voices_json: str = ""


class CreateObjectRequest(BaseModel):
    display_name: str
    role_template: str = "customer"
    language: str = "zh"
    background: str = ""
    phone: str = ""
    tracking_no: str = ""
    courier: str = ""
    address: str = ""
    contact_channel: str = ""
    template_id: str = ""


class ImportRequest(BaseModel):
    account_id: str
    path: str
    content: str


class UpdateObjectRequest(BaseModel):
    display_name: str = ""
    role_template: str = "customer"
    language: str = "zh"
    background: str = ""
    phone: str = ""
    tracking_no: str = ""
    courier: str = ""
    address: str = ""
    contact_channel: str = ""
    template_id: str = ""
    status: str = "active"


class TemplateRequest(BaseModel):
    account_id: str = ""
    # 话务员级归属(B3):''=账号共享 / user_id=话务员个人;user 建的 CP 强制盖章本人。
    owner_user_id: str = ""
    name: str = ""
    opening: str = ""
    core: str = ""
    objection: str = ""
    closing: str = ""
    tone_override: str = ""
    language: str = "zh"
    steps_json: str = ""
    hotwords: str = ""


class UpdateTemplateRequest(BaseModel):
    account_id: str = ""
    # 所有权转移只归 admin/root(user 的 payload 由 CP 剥掉);非 root 的 account_id 冻结。
    owner_user_id: str = ""
    name: str = ""
    opening: str = ""
    core: str = ""
    objection: str = ""
    closing: str = ""
    tone_override: str = ""
    language: str = "zh"
    steps_json: str = ""
    hotwords: str = ""


class PersonaRequest(BaseModel):
    account_id: str = ""
    name: str = ""
    company: str = ""
    tone: str = ""
    language: str = "zh"
    reference_audio: str = ""
    tts_provider: str = ""


class UpdatePersonaRequest(BaseModel):
    account_id: str = ""
    name: str = ""
    company: str = ""
    tone: str = ""
    language: str = "zh"
    reference_audio: str = ""
    tts_provider: str = ""


class ProviderSettings(BaseModel):
    provider: str = ""
    model: str = ""
    base_url: str = ""
    backend: str = ""
    api_key: str = ""
    endpoint: str = ""
    language: str = ""
    # 语言/音色模式（agent 运行时读取；设置以 JSON blob 存储，旧档缺键=默认值，
    # 无需 DB 迁移）：tts.voice_mode: single=整场同声(collapse 成主音色,默认) |
    # per_language=按语言分音色(speaker_zh/speaker_cantonese/speaker_en 逐轮切换)；
    # asr.language_mode: auto=锚定+滞回跟随(默认) | fixed=钉死 language 指定语言。
    voice_mode: str = "single"
    language_mode: str = "auto"
    speaker: str = ""
    speaker_zh: str = ""
    speaker_cantonese: str = ""
    speaker_en: str = ""
    instruct: str = ""
    resource_id: str = ""
    app_id: str = ""
    access_token: str = ""
    # 本地 LLM 模型选择(ML Studio repo,如 avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit):
    # 由 bok serve 读取,决定 :1235 起哪个模型;空则用 bok 默认。model 字段是请求体里的
    # 模型名,cloud 用;本地模型切换存这里,避免与 mlx 需要的全路径模型名冲突。
    local_model: str = ""
    # VAD / 打断（agent 运行时会读取这些值；sensitivity = Silero 激活阈值 0~1，
    # 越高越抗噪，越低越灵敏，agent 侧传给 inference.VAD activation_threshold）
    max_buffered_speech: float = 15.0
    min_speech_duration: float = 0.15
    min_silence_duration: float = 0.45
    interruption: bool = True
    sensitivity: float = 0.6
    sample_rate: int = 24000


class SipSettingsModel(BaseModel):
    """外呼（SIP）配置段（spec 2026-09-12 Wave2）。

    mode=mock|real：mock=CP 派生真语音被叫（本地演示/E2E），real=官方
    CreateSIPParticipant 走真 trunk。env `BOK_SIP_MODE` 是 kill-switch
    （有值即终局，见 agent dialer.resolve_dial_mode），settings 只在 env
    缺省时生效。auth_password 走 secret 掩码（GET 返回空串+has_ 标记，
    PUT 传空=保留旧值）。
    """

    mode: str = "mock"
    trunk_id: str = ""
    address: str = ""
    auth_username: str = ""
    auth_password: str = ""
    numbers: list[str] = []
    ringing_timeout_s: int = 30
    max_call_duration_s: int = 600


class CampaignSettingsModel(BaseModel):
    """全局外呼时段窗段（2026-09-17 T3b）：settings.campaign。

    call_windows 形状 [{"days":[1..7],"start":"HH:MM","end":"HH:MM"}]（≤3 组，
    归一见 campaign.parse_call_windows，非法项静默丢弃）；空=不限时段。与任务级
    call_windows 取交集（campaign._tick_campaign 双层都过才起拨）。
    """

    call_windows: list[dict] = []


class SettingsRequest(BaseModel):
    asr: ProviderSettings = ProviderSettings()
    llm: ProviderSettings = ProviderSettings()
    tts: ProviderSettings = ProviderSettings()
    vad: ProviderSettings = ProviderSettings()
    sip: SipSettingsModel = SipSettingsModel()
    # None=请求未带 campaign 键 → 保留既有段（不清运营已配的全局窗）；
    # 传 {} / call_windows=[] = 清空（不限时段）。
    campaign: CampaignSettingsModel | None = None
    policy: str = "offline_first"


class SupervisorCommand(BaseModel):
    call_id: str


class WhatsAppCaptureRequest(BaseModel):
    """Agent 偵測到客戶俾 WhatsApp:number 有值=captured(客戶讀出號碼),空=offered(應承加專員)。"""

    number: str = ""
    channel: str = ""  # whatsapp | wechat,缺省由 CP 按对象 contact_channel 推断


class WhatsAppHandledRequest(BaseModel):
    """專員喺操作台標記已對接。"""

    handled: bool = True


class DialResultRequest(BaseModel):
    """Agent 外呼拨号结果上报（spec Wave2）：answered→ACTIVE；三失败态→ENDED+disposition。

    status 空/未知 = no-op（幂等，不误伤尚在 RINGING 的通话）。
    """

    status: str = ""  # answered | no_answer | rejected | failed
    detail: str = ""


class SiteCreateRequest(BaseModel):
    """建站入参（spec 2026-09-13 P1.5 Task 7 收尾：面板「+ 新建站点」数据源）。

    **幂等**：同 `account_id` + `name` 已有站点时端点直接返回既有行（不重复建、
    不改写）——建站是站点生命周期的一次性引导动作，重复提交（面板双击/脚本重跑）
    不应产生同名重复站点。要改字段走 `update_site`（后续端点），不靠重名行堆叠。
    `numbers` 严格 `list[str]`（T1 审查定案，与 `TrunkRegisterRequest` 同款防线）。
    `sip_edge` 值域 `none|local|cloud`（空=local，与 repo 默认一致；越界=400）。
    """

    name: str
    livekit_url: str = ""
    sip_edge: str = "local"
    trunk_id: str = ""
    numbers: list[str] = []
    region: str = ""
    account_id: str = "acc-001"


class TrunkRegisterRequest(BaseModel):
    """站点注册 SIP outbound trunk 入参（spec 2026-09-13 P1.5 Task 3，一次性引导）。

    `numbers` 严格 `list[str]`（T1 审查定案）：CP 层 Pydantic 类型是唯一防线——
    repository 层的 `_normalize_site_numbers` 对畸形入参静默归一会把号码池清空。
    `auth_username`/`auth_password` 留空 = IP 白名单模式（LiveKit 官方同款语义）；
    **密码只进不出**，端点响应不回显（凭据安全，T8 掩码先例）。
    """

    address: str
    auth_username: str = ""
    auth_password: str = ""
    numbers: list[str] = []


class DialNowRequest(BaseModel):
    """对象页「立即外呼」入参（spec 2026-09-13 P1.5 Task 4：单发外呼）。

    全部可省：`template_id` 空=用对象绑定话术（显式给则覆盖通话快照）、
    `persona_id` 空=agent 按对象/默认人设解析、`language` 空=对象语言（再兜 zh）、
    `site_id` 空=不挂站点（dial 块 trunk 回退 settings `sip.trunk_id`）。
    """

    template_id: str = ""
    persona_id: str = ""
    language: str = ""
    site_id: str = ""


class RosterClaimRequest(BaseModel):
    """名册认领：claimed_by 缺省 acc-001（本机单账号形态）。"""

    claimed_by: str = "acc-001"


class RosterHandledRequest(BaseModel):
    """名册「已对接」标记：true=handled 并同步来源通话横幅；false=撤销认领。"""

    handled: bool = True


class QaEntryCreate(BaseModel):
    """快答库条目(Q→A 检索快路,2026-09-09)。scope=global | step(配合 step_index)。"""

    question_text: str
    answer_text: str
    lang: str = "zh"
    scope: str = "global"
    step_index: int = -1
    voice_id: str = ""
    template_id: str = ""
    account_id: str = "acc-001"
    # 话务员级归属(B3):''=账号共享 / user_id=话务员个人;user 建的 CP 强制盖章本人。
    owner_user_id: str = ""
    source: str = "curated"
    enabled: bool = True


class QaEntryPatch(BaseModel):
    question_text: Optional[str] = None
    answer_text: Optional[str] = None
    lang: Optional[str] = None
    scope: Optional[str] = None
    step_index: Optional[int] = None
    voice_id: Optional[str] = None
    enabled: Optional[bool] = None
    # 所有权转移只归 admin/root(user 的 patch 由 CP 剥掉)。
    owner_user_id: Optional[str] = None
