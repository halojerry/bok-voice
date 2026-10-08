#!/usr/bin/env python
"""env 域(worker/CP 子进程 env 组装 + `_FORWARD_ENV` 立法单点表;G2 W② 最后一批
从 core 搬出,搬运纪律=穿模块对象调用)。

- 本模块 `from bokctl import core, models, paths` 拿模块对象:models 域件
  (MODELS/model_path/resolve_llm_repo/_mt_llm_model/_settle_llm_model)穿
  `models.X`、路径/平台锚(is_mac/_repo_pythonpath/app_data_dir/repo_python)
  穿 `paths.X`、core 仍住的名字(healthy——_interp_env 的 :1236 探活)穿
  `core.X` 调用时取——patch 与域间消费在属主模块侧保持可见(patch 缝=模块
  属性)。
- 本域自有名件(_dev_9b_enabled/_certifi_bundle/_bake_ssl_cert_file/
  _control_plane_env/_llm_queue_proxy_on/_settle_gate_url/_apply_judge_env/
  _FORWARD_ENV/_BOK_PASSTHROUGH_KEYS/_apply_bok_passthrough_env/
  _apply_flow_graph_env/_agent_worker_env/_apply_interp_direction_env/
  _agent_prod_env/_interp_env)域内裸名互调(同模块全局=call-time 可 patch)。
- 单源红线(W② 计划):`_FORWARD_ENV` 表键序不变、原样搬运——全仓
  `_FORWARD_ENV` 表字面量与 `def _control_plane_env` 各只允许出现一次,键集合
  冻结快照钉在 tests/test_bok_module_contract.py;表本体唯一,别名
  `_BOK_PASSTHROUGH_KEYS` 防 2026-09-18 旧调用面散抄。
- 留守 core 的近邻(边界记录,2026-10-04):_cp_bind_host(serve/prod 的 bind
  接线,非 env 组装)判留 core;_llm_raw_expected/_llm_raw_status_check_expected
  等健康可选线判据仍在 core(穿 `env._llm_queue_proxy_on` 取拓扑闸)。
- 测试面:patch 一律走 tests/_bokpatch.py(patch_bok;PATCH_TARGETS 已把
  _control_plane_env/_agent_prod_env/_agent_worker_env/_interp_env 改道
  bokctl.env);facade 读用 bok.env.X(_FORWARD_ENV 立法门禁读面同)。
"""
from __future__ import annotations

import os
from pathlib import Path

from bokctl import core, models, paths


def _dev_9b_enabled() -> bool:
    """9B 专线(:1237)是否随栈常驻:``BOK_DEV_9B=0`` 显式关,**默认启动(2026-10-01
    P2 翻档,模型在盘才拉)**。

    历史与翻档理由:9B 常驻曾是夜间崩速主犯(LANE-AB-2026-09-25 附 3:judge 9B
    与回复 4B 共挤统一内存,swap 颠簸,in-call tps 4-12)。**该结论的前提已被
    P1 拆除**——ASR 全量迁 CPU 后 MPS 只剩 LLM,统一内存压力位换人;且 9B 现在
    的角色是 a_reply 专线(Huihui-Qwen3.5-9B-abliterated):暖态 TTFT 175ms
    (:1237 直连,无队列代理头排),soak 11/11 p50 912ms/0 fallback,双引擎
    (4B 判官 :1235 + 9B 回复 :1237)分进程分端口互不挤占。模型缺盘时保持旧
    形状(不拉,judge/settle 回退 :1235)——a_reply 车道的回滚键=BOK_DEV_9B=0
    或路由表改回缺省链。读法与全仓同款(env=="0" 显式关)。"""
    return os.environ.get("BOK_DEV_9B", "") != "0"


def _certifi_bundle(py: Path | None = None) -> str:
    """定位 worker 解释器可用的 certifi CA 束；找不到返回 ""。

    .venv312（homebrew python@3.12 + OpenSSL 3.6）无默认 CA 束——certifi 已装
    但 OpenSSL 唔自动读，裸连 https://api.minimax.cn 必炸 SSLCertVerificationError
    （P5 验收实测：MiniMax TTS 每轮全灭）。worker env 注 SSL_CERT_FILE=<cacert.pem>
    云端 TLS 先打得通。

    顺序：① 先路径探测【目标解释器】的 site-packages（env 係为子进程构建，
    bok.py 自己的解释器未必同款）；② 回退 importlib 探当前解释器（SSL_CERT_FILE
    只係一条普通 PEM 路径，文件在盘子进程就食得）。两者都失败 → 返回 ""，
    调用方保持 env 原样（绝不阻塞启动）。
    """
    if py is not None:
        try:
            prefix = Path(py).resolve().parent.parent  # <venv>/bin/python → <venv>
            pats = [prefix.glob("lib/python3*/site-packages/certifi/cacert.pem")]
            if os.name == "nt":
                pats.append(prefix.glob("Lib/site-packages/certifi/cacert.pem"))
            for pat in pats:
                for cand in sorted(pat):
                    if cand.is_file():
                        return str(cand)
        except Exception:  # noqa: BLE001 - 探测失败就走 importlib 兜底
            pass
    try:
        import certifi

        cand = certifi.where()
        if cand and Path(cand).is_file():
            return str(cand)
    except Exception:  # noqa: BLE001 - 无 certifi = 无默认束，维持现状
        pass
    return ""


def _bake_ssl_cert_file(env: dict[str, str], py: Path | None = None) -> dict[str, str]:
    """worker env 固化 SSL_CERT_FILE：仅在未设且 certifi 在盘时注入。

    用户/部署显式设置的 SSL_CERT_FILE（env dict 或启动 bok 的 shell）永远优先，
    唔覆盖；shell 有值时抄进 env dict（launchd plist 由此生成，唔会漏）。
    找不到束就唔注入（保持旧行为，注入失败零副作用）。
    """
    if env.get("SSL_CERT_FILE"):
        return env
    shell_val = os.environ.get("SSL_CERT_FILE", "")
    if shell_val:
        env["SSL_CERT_FILE"] = shell_val
        return env
    bundle = _certifi_bundle(py)
    if bundle:
        env["SSL_CERT_FILE"] = bundle
    return env


def _control_plane_env(db: Path | str) -> dict[str, str]:
    """Env for the control-plane child. MUST include LiveKit credentials so
    /api/token issues a real JWT instead of the old sha256 dev fallback."""
    # 结算摘要/蒸馏（Summarizer）用同一本机 MLX：settings 里的 llm 卡片可能是空 base_url /
    # 占位 model="local"，真实地址由这里注入（与 agent worker L667 同源）。
    _cur = models.MODELS["mac"] if paths.is_mac() else models.MODELS["windows"]
    llm_model = models.model_path({**_cur, "llm": models.resolve_llm_repo(_cur)}, "llm")
    env = {
        "PYTHONPATH": paths._repo_pythonpath(),
        "BOK_SERVICE": "control-plane",
        "DATABASE_URL": f"sqlite:///{Path(db).as_posix()}",
        "VAULT_ROOT": str(paths.app_data_dir() / "vault"),
        "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
        "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
        "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
        "MLX_LLM_BASE_URL": os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        "MLX_LLM_MODEL": llm_model,
        # mt 车道缺省(:1236,与 _interp_env 同源)：CP 自身不翻译,但模型路由测连
        # 端点在 CP 进程内 resolve——缺这行 root 点 mt 测连恒 400「未配置显式
        # 端点」而 MT 其实活着(2026-09-26 实弹发现)。
        "MT_LLM_BASE_URL": os.environ.get("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1"),
    }
    # settle 专线(:1237,9B):Summarizer 优先吃这条——纪要/蒸馏係延迟不敏感的
    # 后台重活,大模型质量↑且与活通话的 :1235 隔离;模型不在盘不下发(注了会
    # 打死端口),Summarizer 走原链路回退 :1235。
    # 9B 后端化（2026-09-25，LANE-AB-2026-09-25.md 附3：9B 常驻=夜间崩速主犯
    # 之一——judge 二号驻留与回复车道共挤统一内存/swap 颠簸）：默认不随栈拉起
    # 也不注入该键（Summarizer 回退 MLX 已核实）；外部显式设了 BOK_SETTLE_LLM_*
    # 照传（云端纪要端点不受开关误伤）。BOK_DEV_9B=1 时行为与改造前逐字节相同。
    _settle = models._settle_llm_model(_cur)
    if _dev_9b_enabled():
        if _settle and Path(_settle).exists():
            # 消费口=前门闸（2026-10-03 I1）：queue 拓扑下 :1238（reply 插队+可
            # 观测），关=裸 :1237。与 _start_settle_proxy 同判据。
            env["BOK_SETTLE_LLM_BASE_URL"] = os.environ.get("BOK_SETTLE_LLM_BASE_URL", _settle_gate_url())
            env["BOK_SETTLE_LLM_MODEL"] = _settle
    else:
        _ext_settle_url = os.environ.get("BOK_SETTLE_LLM_BASE_URL", "").strip()
        if _ext_settle_url:
            env["BOK_SETTLE_LLM_BASE_URL"] = _ext_settle_url
            _ext_settle_model = os.environ.get("BOK_SETTLE_LLM_MODEL", "").strip()
            if _ext_settle_model:
                env["BOK_SETTLE_LLM_MODEL"] = _ext_settle_model
    # settle 专线指向**云端**（DeepSeek 等）时的凭据：Summarizer 现在会带
    # `Authorization: Bearer`（2026-09-21——先前不带，云端点一律 401，即「纪要换云」
    # 结构上走不通）。凭据只走 env、不落盘；未设=空串，payload/行为与本地档逐字节同旧。
    if os.environ.get("BOK_SETTLE_LLM_API_KEY", "").strip():
        env["BOK_SETTLE_LLM_API_KEY"] = os.environ["BOK_SETTLE_LLM_API_KEY"]
    # 云端思考档下纪要单次要 9-14s 起，Summarizer 缺省 15s 会 ReadTimeout（实测
    # v4-pro 5/5 全超时）——这枚开关是那档的超时口。同款 CP 面注入，未设=不上抬。
    if os.environ.get("BOK_SETTLE_THINKING_TIMEOUT_S", "").strip():
        env["BOK_SETTLE_THINKING_TIMEOUT_S"] = os.environ["BOK_SETTLE_THINKING_TIMEOUT_S"]
    # E7 离线润色面 kill-switch（2026-09-21）：唯一消费者是 **CP**（挂断后纪要输入 /
    # QA 挖掘 / L-① 漏网轮），故走这张 CP 面表显式下发（同 BOK_SETTLE_LLM_* 先例）——
    # prod launchd/schtasks 封闭 env 面不注入即死门。**不进 _FORWARD_ENV**：那张表是
    # A 线 agent worker 面，而润色绝不进实时轮（agent_runtime 不 import
    # polish_wiring，tests/test_polish_wiring.py 结构化锚钉住）。未设/空串不注入
    # （默认档=现状逐字节不变；开关默认关，见 polish_wiring 模块 docstring）。
    _polish_offline = os.environ.get("BOK_POLISH_OFFLINE", "").strip()
    if _polish_offline:
        env["BOK_POLISH_OFFLINE"] = _polish_offline
    # M-27 派发黑洞看门狗 kill-switch（2026-09-23 修复波#2）：唯一消费者是 **CP**
    # （token 签发后 A 线 agent 回房看门狗，control_plane.main），同 BOK_POLISH_OFFLINE
    # 判例走这张 CP 面表显式下发（**不进 _FORWARD_ENV**——agent worker 面）。未设/
    # 空串不注入（默认档=开；"0"=关，见 control_plane.main 看门狗块 docstring）。
    _dispatch_retry = os.environ.get("BOK_DISPATCH_RETRY", "").strip()
    if _dispatch_retry:
        env["BOK_DISPATCH_RETRY"] = _dispatch_retry
    # 并发准入上限（2026-09-27；2026-10-01 容量模块化）：唯一消费者是 **CP**
    # （_create_call_in 建单闸，control_plane.main），同 BOK_DISPATCH_RETRY 判例
    # 走这张 CP 面表显式下发（不进 _FORWARD_ENV——agent worker 面）。显式设了
    # =legacy 钉死（旧语义逐字节；"0"=不限）；未设/空串不注入=CP 侧 capacity.py
    # 动态档（mac 档 ceiling=2，准入不创造容量）。
    _max_active = os.environ.get("BOK_MAX_ACTIVE_CALLS", "").strip()
    if _max_active:
        env["BOK_MAX_ACTIVE_CALLS"] = _max_active
    # 容量准入档案（2026-10-01 第一性重写）：CP 面三键——部署档案选择 + floor/
    # ceiling 覆盖（capacity.py 消费；auto=macOS→mac、Linux+nvidia-smi→cuda）。
    # 未设/空串不注入=CP 侧 auto 探测，零迁移（同 BOK_DISPATCH_RETRY 判例）。
    for _k in ("BOK_DEPLOY_PROFILE", "BOK_MAX_CALLS_FLOOR", "BOK_MAX_CALLS_CEILING"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # M-7 login 频控 kill-switch（2026-09-23 修复波#3）：唯一消费者是 **CP**
    # （/api/auth/login per-username 滑窗，control_plane.main），同 BOK_DISPATCH_RETRY
    # 判例走这张 CP 面表显式下发（不进 _FORWARD_ENV）。未设/空串不注入（默认档=开；
    # "0"=关）。
    _login_rl = os.environ.get("BOK_LOGIN_RATE_LIMIT", "").strip()
    if _login_rl:
        env["BOK_LOGIN_RATE_LIMIT"] = _login_rl
    # 沉淀引擎（2026-09-28 env 面审计归位）：AUTO_DIGEST/HOMOPHONE 的唯一消费者
    # 是 CP 进程（qa_digest 循环）——prod launchd CP 的封闭 env 面此前结构性收不到
    # 这两键（BOK_FLOW_GRAPH 同款教训；dev 靠 _start_proc merge 才活着）。显式设了
    # 才透传，缺省=CP 侧默认（AUTO_DIGEST 关/HOMOPHONE 开）零变化。
    for _k in ("BOK_QA_AUTO_DIGEST", "BOK_QA_HOMOPHONE", "BOK_REQUIRE_TEMPLATE", "BOK_PUBLISH_AUTO_PREGEN"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # 容灾波（2026-10-02 DR-WAVE-CONTRACT §3）：CP 饥荒监视器的迟滞键——
    # 消费者是 CP 进程（ops_metrics 状态机），prod 封闭 env 面在此透传；
    # BOK_LLM_FAMINE_TTFT_S 消费者双面（agent worker 走 _FORWARD_ENV 已登记，
    # CP 复用同键）故这里也透传。
    for _k in ("BOK_LLM_FAMINE_TTFT_S", "BOK_LLM_FAMINE_HOLD_S", "BOK_LLM_FAMINE_RELEASE_S"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # 容灾波配套（ops_metrics 日志尾读/swap 阈值标注）：不透传=prod 封闭面死门
    # （日志端点按平台默认路径找日志、阈值恒 8——功能在但不可调）。
    for _k in ("BOK_AGENT_LOG", "BOK_SWAP_THRESHOLD_GB"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ══ 2026-10-02 编排审计第二波 · CP env 面收编 ══════════════════════════
    # prod（mac launchd / Windows schtasks）CP 单元的 env 是**封闭白名单**
    # （prod._prod_units → _control_plane_env），dev 靠 _start_proc merge 才能活着——
    # 凡 CP 会读而这张表没登记的键，在 prod 都是结构性死门（BOK_FLOW_GRAPH /
    # BOK_QA_AUTO_DIGEST 两次同款教训）。下面按消费者分组显式透传；**显式设了
    # 才下发（未设/空串/纯空白不注入）**=CP 侧缺省档零变化，先例=
    # BOK_POLISH_OFFLINE/BOK_DISPATCH_RETRY 块。
    # ① 认证三键（control_plane/auth.py）：BOK_AUTH_REQUIRED（auth_required()
    #    判据 auth.py:75）、BOK_JWT_SECRET（jwt_secret() 签名键 auth.py:84，
    #    main 启动闸缺它=BOK_AUTH_REQUIRED=1 直接拒启 main.py:461）、
    #    BOK_CP_TOKEN（机器通道同值直通 auth.py:293 + main 多处）。**事故形状**：
    #    prod 封闭面收不到 BOK_AUTH_REQUIRED=1 → CP 静默 auth-off（对外 bind 时
    #    /api/* 全裸放行；main._unsafe_open_bind 只挡非回环+双关的极端档）。
    #    密钥类走 BOK_SETTLE_LLM_API_KEY 先例（strip 判空 + 原值下发，不吃空格）。
    for _k in ("BOK_AUTH_REQUIRED", "BOK_JWT_SECRET", "BOK_CP_TOKEN"):
        if os.environ.get(_k, "").strip():
            env[_k] = os.environ[_k]
    # ② ops 面：BOK_LOG_LEVEL（CP 日志档 main.py:428）、BOK_CORS_ORIGINS（跨域
    #    白名单 main.py:229）、BOK_ROOT_USERNAME/BOK_ROOT_PASSWORD（root 幂等种子
    #    main.py:400-401，operator/机器赋权后的自助入口；密码原值下发不 strip）、
    #    BOK_SIP_MODE（dial.mode env 覆盖 campaign.py:61）、BOK_CP_PUBLIC_URL
    #    （云托管管理台/托管节点写 runtime-config main.py:444 + qa_digest.py:394）、
    #    SENTRY_DSN（R3 2026-10-04：CP init_sentry + worker 关键路径上报;
    #    worker 面同键另走 _FORWARD_ENV,双面同源 env）。
    if os.environ.get("BOK_ROOT_PASSWORD", "").strip():
        env["BOK_ROOT_PASSWORD"] = os.environ["BOK_ROOT_PASSWORD"]
    for _k in ("BOK_LOG_LEVEL", "BOK_CORS_ORIGINS", "BOK_ROOT_USERNAME",
               "BOK_SIP_MODE", "BOK_CP_PUBLIC_URL", "SENTRY_DSN",
               "SENTRY_ENVIRONMENT", "SENTRY_SEND_PII"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ③ node / 静态面：BOK_NODE_ARTIFACTS_DIR（节点制品目录 main.py:4189,4208）、
    #    BOK_NODE_LOG_TTL_DAYS（节点日志清扫窗 main.py:4250）、
    #    BOK_WEB_STATIC_DIR（CP 托管的静态 UI 根 main.py:7532）、
    #    BOK_APP_DATA（日志尾读/app-data 解析 main.py:7225——prod 的 app-data
    #    与 dev 默认路径不同，不注入则 ops 日志面指向错目录）。
    for _k in ("BOK_NODE_ARTIFACTS_DIR", "BOK_NODE_LOG_TTL_DAYS",
               "BOK_WEB_STATIC_DIR", "BOK_APP_DATA"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ④ settle 闲时轮（main.py:4623/4627 两窗；QA 挖掘/reports 后台作业在活通话
    #    窗口的让路姿势——不注入则 prod 恒吃缺省 15s/300s）。
    for _k in ("BOK_SETTLE_IDLE_POLL_S", "BOK_SETTLE_IDLE_WAIT_S"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ⑤ pregen/qa/embed：BOK_PERSONA_AUTO_PREGEN（发布即预热闸 pregen.py:135）、
    #    BOK_TTS_CACHE_DIR（罐头缓存目录 pregen.py:251）、BOK_QA_CLUSTER_MODEL
    #    （聚类计划 LLM 覆盖 qa_cluster.py:104）、BOK_QA_DIGEST_INTERVAL_S（沉淀
    #    循环间隔 qa_digest.py:137）、BOK_EMBED_BASE_URL（bge 侧车端点
    #    qa_digest.py:489）。
    for _k in ("BOK_PERSONA_AUTO_PREGEN", "BOK_TTS_CACHE_DIR", "BOK_QA_CLUSTER_MODEL",
               "BOK_QA_DIGEST_INTERVAL_S", "BOK_EMBED_BASE_URL"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ⑥ MiniMax（CP 侧 main.py:1247-1486）：MINIMAX_API_KEY（TTS 设置无 key 时的
    #    回落）+ BASE_URL/REGION（_minimax_clone_base 端点域）+ MODEL（合成档，
    #    缺省 speech-2.8-hd）。_FORWARD_ENV 先例已把 MINIMAX_API_KEY 发给 agent
    #    单元，CP 面同权（机器通道/门诊探针同源凭据；只走 env、不落盘）。key 原值
    #    下发不 strip（BOK_SETTLE_LLM_API_KEY 先例）。
    if os.environ.get("MINIMAX_API_KEY", "").strip():
        env["MINIMAX_API_KEY"] = os.environ["MINIMAX_API_KEY"]
    for _k in ("MINIMAX_BASE_URL", "MINIMAX_REGION", "MINIMAX_MODEL"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # ⑦ ops 端点覆盖（ops_metrics.py:545-551 server_registry——容灾面板/Provider
    #    卡/分节点部署把 ASR/TTS/laya/csc 指向非缺省 host:port 时的唯一入口）。
    for _k in ("BOK_LAYA_URL", "BOK_CSC_URL", "QWEN3_ASR_BASE_URL", "QWEN3_TTS_BASE_URL"):
        _v = os.environ.get(_k, "").strip()
        if _v:
            env[_k] = _v
    # .venv312 OpenSSL 无默认 CA 束：固化 SSL_CERT_FILE（P5 遗留项；CP 的
    # Summarizer/联网探针同食 TLS，注入失败零副作用）。
    return _bake_ssl_cert_file(env, paths.repo_python())


def _llm_queue_proxy_on() -> bool:
    """:1235 优先级队列代理开关(2026-09-26 根治 mlx 解码争用,默认开;
    BOK_LLM_QUEUE_PROXY=0 回旧拓扑=mlx 直跑 :1235 无代理)。开=mlx_lm 挪
    内部 :1239,services/llm-mlx/queue_proxy.py 占公网口 :1235:生成请求
    单并发排队、agent 回复(X-Bok-Lane: reply)插队,后台(settle/qa-cluster/
    judge)不再与活通话首轮互抢 GPU 时间片。"""
    return os.environ.get("BOK_LLM_QUEUE_PROXY", "1") == "1"


def _settle_gate_url() -> str:
    """9B 专线（a_reply/settle/prewarm）的**消费口**（2026-10-03 I1 前门闸）。

    queue proxy 拓扑（默认开）下 = :1238 前门（reply 插队 + GATE 观测；上游
    裸口 :1237，_start_settle_proxy 同条件拉起）；关 = 裸 :1237 旧形状。
    与 _start_settle_proxy 同一判据（同 env 开关），不会出现「指了闸却没起」。
    """
    if _llm_queue_proxy_on():
        return "http://127.0.0.1:1238/v1"
    return "http://127.0.0.1:1237/v1"


def _apply_judge_env(env: dict[str, str], _cur: dict[str, str]) -> None:
    """flow judge 专线 env(:1237 9B):模糊轮判定係后台重活(fire-and-forget),
    大模型判定质量↑且与活通话回复的 :1235 完全隔离;模型缺失(不在盘)不下发,
    judge 走原链路 :1235(agent.py 的 FLOW_JUDGE_* 优先级空头自动回退)。
    存在性检查必须有——注了 env 而 :1237 没起,judge 请求会打上死端口。

    9B 后端化(2026-09-25,LANE-AB-2026-09-25.md 附3):9B 常驻=夜间崩速主犯之一
    ——judge 9B 二号驻留与回复车道共挤统一内存,swap 颠簸下 in-call tps 4-12。
    默认(:1237 不拉)不注入该键,judge 回退链落 MLX :1235(agent.py
    ``FLOW_JUDGE_LLM_BASE_URL or MLX_LLM_BASE_URL`` 已核实);外部显式设了照传
    (云端 judge 钩子不受开关误伤)。BOK_DEV_9B=1 时行为与改造前逐字节相同。"""
    if not _dev_9b_enabled():
        _ext_judge_url = os.environ.get("FLOW_JUDGE_LLM_BASE_URL", "").strip()
        if _ext_judge_url:
            env["FLOW_JUDGE_LLM_BASE_URL"] = _ext_judge_url
            _ext_judge_model = os.environ.get("FLOW_JUDGE_LLM_MODEL", "").strip()
            if _ext_judge_model:
                env["FLOW_JUDGE_LLM_MODEL"] = _ext_judge_model
        return
    _settle = models._settle_llm_model(_cur)
    if _settle and Path(_settle).exists():
        env["FLOW_JUDGE_LLM_BASE_URL"] = os.environ.get("FLOW_JUDGE_LLM_BASE_URL", "http://127.0.0.1:1237/v1")
        # 外部显式 model 照传(docstring「外部显式设了照传」全分支一致化,2026-10-01
        # 翻档后此分支成为缺省路径;云端 judge 钩子不受开关误伤)。
        env["FLOW_JUDGE_LLM_MODEL"] = os.environ.get("FLOW_JUDGE_LLM_MODEL", "").strip() or _settle


# ---------------------------------------------------------------------------
# _FORWARD_ENV 立法（2026-09-19，spec §5 卫生项）：agent/interp worker 运行时
# 读的**运营可调 env** 全集单点表。此前 agent 实读 87 键只有 5 键进表——dev
# 靠 `_start_proc` merge `os.environ` 全活着，prod（launchd/schtasks 封闭白名单）
# 69 键全死（BOK_FLOW_GRAPH prod kill-switch、BOK_CP_TOKEN auth-on worker 上报
# 两次实弹同病）。**新增 env 开关的立法动作 = 在此表加一行**：
# `tests/test_forward_env.py` 扫 agent_runtime 全部 `os.environ` 读取面，未登记
# （本表/bok 既有注入/豁免清单）即测试失败——死门在 CI 层根治，唔靠人记。
# 豁免（测试侧 `_EXEMPT`）：SCRIPTED_LLM*/USE_FAKE_MEDIA/FAKE_STT_TEXT（E2E 测试腿
# 专用）、LOCALAPPDATA（Windows OS 变量，tts_cache 有 home 回退）。
_FORWARD_ENV = (
    # —— 话术图引擎 + QA 命中语义 ——
    "BOK_FLOW_GRAPH",
    "BOK_FLOW_GRAPH_JUDGE",
    # —— W1b 意图语义车道(2026-09-23:关键词/judge 之外第三条命中路,本地
    #    embedding 检索同步补位;总闸默认开,端点缺席时装配面自动降级) ——
    "BOK_INTENT_SEMANTIC",
    "BOK_INTENT_SEM_THRESHOLD",
    "BOK_INTENT_SEM_BASE_URL",
    "BOK_INTENT_SEM_TIMEOUT_MS",
    "BOK_INTENT_SEM_COS_W",
    "BOK_INTENT_SEM_SUB_W",
    # W3b 解锁·快答库语义补位(2026-09-24):词面 0.90 未中轮的释义档,端点同
    # :8789 本机 embedding(键独立,预算同 400ms)。
    "BOK_QA_SEMANTIC",
    "BOK_QA_SEM_THRESHOLD",
    "BOK_QA_SEM_BASE_URL",
    "BOK_QA_SEM_TIMEOUT_MS",
    # 匹配端根治（2026-09-25 三刀）：召回通道（双向子串+拼音）与 Laya QA 验证车道。
    # 全部默认保守：PINYIN=1 只影响召回排序（词面 0.90 快道字节不变）；LAYA_QA 默认 0
    # =整条车道零调用零变化。
    "BOK_QA_PINYIN",
    "BOK_QA_RECALL_K",
    "BOK_QA_RECALL_FLOOR",
    "BOK_LAYA_QA",
    "BOK_LAYA_QA_TIMEOUT_MS",
    "BOK_LAYA_QA_P",
    # 沉淀引擎 v1（2026-09-25）：闲时自动消化（挖→聚→分档采纳→退休→学同音→
    # pregen）。AUTO_DIGEST 默认 0（CP 读；自主写库行为先 opt-in 实弹再谈默认）；
    # HOMOPHONE 默认 1（表空=行为逐字节同旧，学到对子才生效，golden 负样本守门）。
    # AUTO_DIGEST 消费者是 CP 不是 agent worker——但 dev serve 靠 _start_proc
    # merge os.environ 活着、prod launchd CP 封闭 env 面走 _control_plane_env
    # （那里另有同名透传，2026-09-28 env 面审计归位；此处保留无害冗余）。
    "BOK_QA_AUTO_DIGEST",
    "BOK_QA_HOMOPHONE",
    # 匹配端闸松绑+死区填补（2026-09-25 四路并行轮预埋）：WA 步放行 question 类
    # （真实数据 wa_step_locked 431 次 bypass 的半数是错杀——答赔法与收号不冲突）；
    # 垫话按需第二发（治载荷轮 2.3s 后裸静默，仅真慢轮触发非固定双发）。
    "BOK_QA_WA_STEP_QUESTION",
    "BOK_FILLER_RESHOT",
    # —— Laya 决策 sidecar(:8791,2026-09-26):意图/流程判定 10ms 快路。总闸
    #    BOK_LAYA_JUDGE(serve 默认 "1" 随栈拉起,模型在盘才起;="0" 显式关;
    #    sidecar 侧同闸双保险,"0" 时 /v1/decide 一律 503)与端点覆盖(缺省
    #    127.0.0.1:8791;8789 是 embed sidecar 既定端口,勿混)。
    "BOK_LAYA_JUDGE",
    "BOK_LAYA_SIDECAR_URL",
    # —— 意向规则挂断评估(W4-T2,2026-09-19:0=关,挂断走原 disposition) ——
    "BOK_INTENT_RULES",
    # —— 意图喂下游(P2.4,2026-09-21:0=关;默认 1——当轮意图进 LLM 尾部
    #    【客户意图】行 + 垫话类别提示;0=set no-op/行消失,字节同旧) ——
    "BOK_INTENT_CONTEXT",
    "BOK_QA_ROTATION",
    "BOK_QA_PRIORITY",
    "BOK_QA_FASTPATH",
    # —— 粤语音系补位层(2026-09-22:字面 miss 后粤拼槽位对齐;zh 线无此档) ——
    "BOK_QA_PHONETIC",
    "BOK_QA_PHONETIC_THRESHOLD",
    # —— 分支罐头快路+分支动作(2026-09-20 路线 A-①/A-②:分支命中→物化录音跳
    #    LLM;应答首部【收线】/【转人工】/【跳第N步】/【留本步】动作前缀=引擎一等出口) ——
    "BOK_BRANCH_ACTION",
    "BOK_BRANCH_CANNED",
    # —— F4 破坏性动作双护栏(2026-09-20:refuse 派发前条件核心词须字面命中;
    #    整轮/末子句剥词表词后过短=ASR 抄词表不收线) ——
    "BOK_BRANCH_REFUSE_CONFIRM",
    "BOK_BRANCH_REFUSE_HOTWORD_GUARD",
    # —— F2 迟到 FINAL 尾巴护栏(2026-09-20:AI 生成/播报中相对已提交文本的
    #    极短追加 finish 尾巴=重解幻听,不成轮不打断快路/直念回复) ——
    "BOK_LATE_FINAL_GUARD",
    "BOK_LATE_FINAL_MAX_TAIL_CHARS",
    # —— hotword_only 否决层(2026-09-25:AI 忙时停嘴整窗重解把词表热词抄成独立
    #    迟到 FINAL 掐断在播罐头;按词表贪心剥离后严格为空才否决;0=整层不评估,
    #    行为回 F2 现状) ——
    "BOK_LATE_FINAL_HOTWORD_GUARD",
    "BOK_QA_MATCH_THRESHOLD",
    # —— 垫话/罐头/TTS 缓存 ——
    "BOK_FILLER",
    "BOK_FILLER_DELAY_MS",
    # 垫话连发冷却时间窗(2026-09-29):上次真垫话 Ns 内跳过本发(0=关窗)。
    # 旧「相邻轮歇一轮」seq 冷却把慢轮覆盖打穿,改时间窗后真实通话轮间隔
    # (>10s)普遍出窗=慢轮全覆盖,急连发段仍有阻尼。
    "BOK_FILLER_COOLDOWN_S",
    # 垫话让路(第十七波 2026-10-02,call-4e8d58c1):真答案首音频就绪即停在播
    # 垫话+hold 归零+reshot 查 reply 在途;="0" 一键回 09-10「垫话必须播完」。
    "BOK_FILLER_YIELD",
    # 晚到补答去重(第十七波):交付前与已交付文本比相似度(阈值沿用
    # BOK_REPEAT_CROSS_TURN_SIM);="0" 跳过比对回旧行为。
    "BOK_LATE_ANSWER_DEDUP",
    # worker 容量阈值(第十七波 FLOW20 全哑根修):livekit load=整机 psutil
    # cpu_percent,共享机桌面噪音过 0.7 线=拒派空房全哑;钉 0.99 仅近全饱和才拒,
    # 生产专用节点想保守可设回 0.7。
    "BOK_WORKER_LOAD_THRESHOLD",
    # SIP 拨号模式覆盖（dialer.py:51 resolve_dial_mode：有值即显式覆盖 settings
    # sip.mode，合法 mock/real、非法回落 mock）；CP 面同键另走 _control_plane_env
    # （campaign.py:61 消费），本行补 agent worker 面——不登记则 prod 封闭 env 面
    # agent 侧恒读空串，env 覆盖结构性死门（2026-10-03 C2）。
    "BOK_SIP_MODE",
    # Sentry 接线（R3 2026-10-04）：worker 面 init_sentry("agent-worker") +
    # 看门狗真火/背景 judge 失败关键路径上报；DSN 缺席=完整 no-op。
    # CP 面同键另走 _control_plane_env（main.py init_sentry）。
    # SENTRY_SEND_PII（2026-10-04 Ethan 拍板 dev 档开）:1=请求头/IP 进事件。
    "SENTRY_DSN",
    "SENTRY_ENVIRONMENT",
    "SENTRY_SEND_PII",
    "BOK_FILLER_GAP_MS",
    "BOK_FILLER_CHAIN",
    "BOK_FILLER_MAX",
    "BOK_FILLER_MAX_DUR_S",
    "BOK_FILLER_CUT_AFTER_S",
    "BOK_CONTEXT_MEM_LEGACY",
    "BOK_FILLER_MATCH",
    # W2a 犹豫混入专用闸(2026-09-24):0 只关犹豫池混入,罐头五类与上游门不动。
    "BOK_FILLER_HESITATION",
    # W2c 语境化过渡承诺(2026-09-24):垫话语境桶 promise_* 池优先,0=回现行阶梯。
    "BOK_FILLER_CONTEXT",
    # W2b 思考态键盘环境音(2026-09-24):官方 thinking_sound 抽签 burst,关=构造不带。
    "BOK_AMBIENT_KEYBOARD",
    "BOK_AMBIENT_KEYBOARD_VOL",
    "BOK_FILLER_MATCH_THRESHOLD",
    "BOK_FILLER_BACKFILL",
    "BOK_TTS_FALLBACK",
    # W8 首子句起播(2026-09-24):TTS 首送快车道——首个 task_continue/首段 POST
    # ≥N 字即送(默认 6,旧 overlap 档 12 字在慢生成轮把首送推后 ~0.7-0.9s)。
    "BOK_TTS_FIRST_CLAUSE",
    "BOK_TTS_FIRST_CLAUSE_CHARS",
    # W-TTS bidi 首 chunk 提前切(2026-09-28):首个 task_continue 句内 ≥N 字即发
    # (默认 10,切点避数字/拉丁 run),后续 continue 仍按句界;0=旧行为逐字节同。
    "BOK_TTS_FIRST_CHUNK_CHARS",
    # bidi 头段催产(2026-09-29):早切头段后立刻 task_flush——服务端对无句末标点
    # 缓冲不起合成(兜底窗 2.4s),不催=早发空转;台架首声 918-962→210-343ms。
    "MINIMAX_BIDI_HEAD_FLUSH",
    # —— LLM 生成链（兜底/投机/预热/超时预算） ——
    "BOK_LLM_FALLBACK",
    # mlx 生成中止（W-ABORT，2026-10-01）：agent worker 侧 MlxLlmLLM 读；="0"
    # 时不带 X-Bok-Req-Id、不发 POST /v1/abort（字节面同旧）。服务端 wrapper
    # 同键（dev serve 走 _start_proc merge；prod 侧 wrapper 由 bok 拉起时继承）。
    "BOK_MLX_ABORT",
    "BOK_PREFILL_SPEC",
    "BOK_PREFILL_SPEC_DEBUG",
    "BOK_PREFILL_SPEC_FINAL_QUIET_MS",
    "BOK_PREEMPTIVE_DEBUG",
    "LLM_PREFIX_PREWARM",
    "PREEMPTIVE_GENERATION",
    "PREEMPTIVE_TTS",
    "PREEMPTIVE_MAX_RETRIES",
    "PREEMPTIVE_DISABLE_ON_MARKER",
    "FLOW_JUDGE_DELAY",
    "FLOW_JUDGE_IDLE_CAP",
    "BOK_JUDGE_CAPPED_SKIP",
    "FLOW_JUDGE_LLM_API_KEY",
    # —— 模型路由统一 kill-switch（2026-09-25 阶段 0：packages/core/model_routes.py
    #    契约在读，="0" 忽略路由表字节同旧；进表=dev/prod 双面都可达） ——
    "BOK_MODEL_ROUTING",
    # —— 云端 Realtime S2S 演示档（2026-09-25 阶段 B：realtime_demo.py +
    #    providers/qwen_realtime.py 适配器读面。BOK_QWEN_REALTIME="1" 才随栈
    #    拉起 bok-realtime worker（:8084，opt-in 不动默认栈）；="0" worker 拒接
    #    一切 job 且适配器构造即 raise；QWEN_REALTIME_KEY=云端凭据（worker 端
    #    读好后**构造参数**传入，适配器自身零 key env 读取）；BOK_REALTIME_DEMO_
    #    MAX_S=会话时长熔断秒数（缺省 300）；QWEN_REALTIME_BASE_URL=WS 端点
    #    覆盖（缺省=适配器模块常量 QWEN_REALTIME_WS_BASE） ——
    "BOK_QWEN_REALTIME",
    "BOK_REALTIME_DEMO_MAX_S",
    "QWEN_REALTIME_KEY",
    # WS 端点覆盖：适配器现读 QWEN_REALTIME_WS_BASE（缺省=同名模块常量）；
    # QWEN_REALTIME_BASE_URL 是该槽的历史/别名登记，防适配器改名时门禁闪红。
    "QWEN_REALTIME_WS_BASE",
    "QWEN_REALTIME_BASE_URL",
    "FLOW_LLM_ADVANCE",
    "BOK_PERCEIVED_BUDGET_MS",
    "BOK_MAX_CALL_DURATION_S",
    # 结算 gather 等待窗(D7):0/缺省=自适应档(无慢任务 10s/有意图判据 25s)。
    "BOK_SETTLE_WAIT_S",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_API_KEY",
    # DeepSeek 思考档位：官方默认 enabled，而我们的 max_tokens 都很小（对话 160 /
    # 判据 8-32）——思考会把预算烧光、正文出空串（通话侧=静默哑火）。故 DeepSeek
    # 端点缺省关思考（契约见 bok_voice_core.deepseek_llm），这两枚是显式开/覆盖口。
    "DEEPSEEK_THINKING",
    "FLOW_JUDGE_LLM_THINKING",
    # —— 轮次/打断/心跳 ——
    "TURN_DETECTION",
    "BOK_TURN_DETECTOR_THRESHOLD",
    "BOK_TURN_DETECTOR_THRESHOLDS",
    # smart-turn 语义闸（V1，2026-09-26：VAD 停嘴处 ONNX 判「说完没」，p<0.5 复用
    # join-hold 等续段；providers/smart_turn.py。默认 "0"=关——未验收特性不默认开）
    "BOK_SMART_TURN",
    # —— FireRedVAD 试点适配层（2026-09-28：providers/firered_vad.py，0.6M DFSMN
    #    流式 ONNX 包成 livekit vad.VAD。BOK_VAD_PROVIDER 默认 "silero"=零变化，
    #    "firered" 才替换且缺依赖/缺模型回退 silero；MODEL_DIR 指模型目录覆盖资产；
    #    THRESHOLD/SMOOTH 为 FireRed 独立档默认 0.5/5，不照抄 silero 0.75） ——
    "BOK_VAD_PROVIDER",
    "BOK_FIRERED_MODEL_DIR",
    "BOK_FIRERED_THRESHOLD",
    "BOK_FIRERED_SMOOTH",
    "ENDPOINT_MIN_DELAY",
    "ENDPOINT_MAX_DELAY",
    "INTERRUPT_MIN_DURATION",
    "RESUME_FALSE_INTERRUPTION",
    "FALSE_INTERRUPTION_TIMEOUT",
    # —— M-30 turns 断窗重放(2026-09-23 修复波#2:CP 断窗轮次本地暂存 CP 恢复补交;
    #    0=回旧行为失败即弃——task-9 腿 9.6 实证 ~14 轮永久丢) ——
    "BOK_TURNS_REPLAY",
    "BOK_INTERRUPT_STORM_BACKOFF",
    "BOK_INTERRUPT_STORM_WINDOW_S",
    "BOK_INTERRUPT_STORM_THRESHOLD",
    "BOK_INTERRUPT_STORM_QUIET_S",
    "BOK_INTERRUPT_STORM_MAX_ROUNDS",
    "BOK_INTERRUPT_STORM_EXPIRY_RESUME",
    "BOK_INTERRUPT_LEDGER",
    "BOK_INTERRUPT_REAP",
    "BOK_REPEAT_HEAD_MAX_HOLD",
    "BOK_PREFIX_PREWARM_YIELD",
    "BOK_ACTIVE_CALLS_DIR",
    "BOK_ASR_ENGINE",
    "BOK_FACT_CORRECTION",
    "SILENCE_NUDGE_SECONDS",
    "SILENCE_NUDGE_MAX",
    "BOK_E2E_NUDGE_IMMUNE",
    "BOK_STARVE_ACK",
    "BOK_PAUSE_ACK",
    "BOK_DEFER_ACK",
    "BOK_SAY_STEP_LIMIT",
    # —— 看门狗/流程守卫 ——
    "BOK_RESPONSE_WATCHDOG_S",
    "BOK_RESPONSE_WATCHDOG_FILLER_EXT_S",
    "BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S",
    "BOK_DIGIT_ACCUMULATE",
    "BOK_WA_ACCUMULATE",
    "BOK_WA_ACCUM_TIMEOUT_S",
    "BOK_WA_LEN_CHECK",
    # —— M-23 首位数字回声剥离(2026-09-23 修复波#4:AI 复述/last_reply 回声混进
    #    客户报号首位 → 错号被确认;0=关) ——
    "BOK_WA_ECHO_STRIP",
    # —— M-22① 罐头/分支出声前内部指令守卫(2026-09-23 修复波#4:教练文案被
    #    罐头车道逐字念给客户;命中拒出声落 LLM;0=关) ——
    "BOK_CANNED_TEXT_GUARD",
    "BOK_WORKER_PORT_GUARD",
    # 单机多栈并存（并行会话/多 worktree 验收）错开 A 线 worker 端口，默认 8081 零漂移。
    "BOK_WORKER_PORT",
    # —— 漏斗 v2（stall 升级阶梯/judge route 路由/跟进工单；合入默认全开，0=回退） ——
    "BOK_STALL_LADDER",
    "BOK_UNCLEAR_ADVANCE",
    "BOK_UNCLEAR_ADVANCE_N",
    "BOK_LLM_STALL_OBS_TPS",
    "BOK_QA_CANNED_COOLDOWN_S",
    "BOK_ROUTE_JUDGE",
    "BOK_TOOLS_FOLLOWUP",
    # —— 双派发守卫（2026-09-28 call-0105a539 实证：同房双 job 并跑整通=双开场
    #    白+双份回答+fallback 道歉风暴；flock 同房互斥，后到 job 让位；0=关，
    #    DIR=锁目录覆盖供测试/多栈隔离） ——
    "BOK_ROOM_CLAIM",
    "BOK_ROOM_CLAIM_DIR",
    # —— ASR（agent 侧读的运维档；sidecar 专属键走 asr_env 另注入） ——
    # 云 ASR 装线波(2026-10-03):豆包 SAUC 总闸,=0 装配点回退本地 Qwen3-ASR
    # (A/B 线共用;凭据走设置面 asr 段,不经 env)。
    "BOK_DOUBAO_ASR",
    "BOK_ASR_HOTWORDS",
    "BOK_ASR_PARTIAL_SLOW_MS",
    # B 线正压臂波(2026-10-02):ASR 帧级调试观测(掉帧/水位打点)——
    # 运维键,prod 不透传=死门(test_forward_env 钉)。
    "BOK_ASR_FRAME_DEBUG",
    # 开采热词(第四来源,2026-09-28):agent 装配期 GET /api/asr/hotwords 一次;
    # 默认 "1"(端点缺席 fail-open 空串),="0" 跳过零 HTTP 调用。
    "BOK_MINED_HOTWORDS",
    # chunk POST 失败保留(D4):0=回退旧「先清后发」档。
    "QWEN3_ASR_CHUNK_KEEP",
    "QWEN3_ASR_STREAM",
    "QWEN3_ECHO_GUARD",
    "QWEN3_HOTWORD_ECHO_GUARD",
    # —— ASR 终稿后置轨(2026-09-21 E1/E2 接线:先热词泄漏清洗、后 snippet 词级
    #    替换;两枚 kill-switch 默认 "1",=0 各自回退零变化) ——
    "BOK_HOTWORD_LEAK_SANITIZE",
    "BOK_SNIPPETS",
    # —— 知识/检索/TTS 语言 ——
    "CONTEXT_RAG",
    "WEB_SEARCH",
    "MINIMAX_LANGUAGE_BOOST",
    "EMOTION_TAG_PILOT",
    # —— 机器通道凭据（auth-on 部署 worker 上报 turns/QA/设置全靠它） ——
    "BOK_CP_TOKEN",
    # —— 日志面 ——
    "BOK_LOG_LEVEL",
    # —— providers/ 子包收编（2026-09-20 D14：test_forward_env 扫描改 rglob 递归，
    #    providers/livekit_plugins.py 61 键此前整包漏扫——60 运营键进表，
    #    FAKE_STT_TEXT 测试腿 shim 进豁免）。未设不注入，默认档零变化。 ——
    # LLM 生成链调参/诊断（livekit_plugins.py MlxLlmLLM/ContextAwareLLM 读面）：
    "BOK_LLM_MSG_DEBUG",
    "BOK_LLM_REGEN",
    "BOK_A_LINE_VOICE_TAGS",
    # 换气注入（voice_style.py transform 长句句界补 (breath)，2026-09-27）：
    # 总闸默认开、阈值默认 20 字。同上未设不注入、默认档零变化。 ——
    "BOK_BREATH_INJECT",
    "BOK_BREATH_SENT_CHARS",
    # ASR 受限润色层（agent_runtime/asr_polish_runtime.py，2026-09-27）：
    # 确定性音近吸附默认开 + CSC 小模型二道 opt-in（zh-only，:8792）。
    "BOK_ASR_POLISH",
    "BOK_CSC_SIDECAR",
    "BOK_CSC_URL",
    "BOK_CSC_CONF_GATE",
    "BOK_REPEAT_GUARD",
    "BOK_REPEAT_CROSS_TURN",
    "BOK_REPEAT_CROSS_TURN_SIM",
    # 编造号码输出守卫（2026-10-01，call-231aa92a）：LLM 流出口逐句校验号码
    # 确认句——数字须来自 {捕获账本, 本轮客户原话}，编造者改写/替换（默认 "1"，
    # "0"=关=恒等返回；实现 packages/core/bok_voice_core/output_guard.py）。
    "BOK_NUMBER_GUARD",
    "BOK_TAIL_SLIM",
    "BOK_MEMORY_CHARS",
    # 尾部节食（第十一波 2026-09-29）：slim 轮记忆块降频——距上次带过 ≥N 条
    # 账本项才带（=1 旧行为每轮带）。尾部骑在新 user 消息后=每轮全新 uncached，
    # 记忆块(~250 字≈170 tok)是 slim 轮尾部最大件。STABLE_SPAN=稳定段重发回看
    # 窗口条数（默认 max(2, LLM_HISTORY_TURNS-2)；修隔轮意外重发 uncached 交替）。
    "BOK_TAIL_MEMORY_EVERY",
    "BOK_TAIL_STABLE_SPAN",
    # D1 槽位化 actor（2026-10-01,第一性原理重构）：A 线回复 LLM 从「整本剧本+
    # 全规则」切「角色卡+任务块」（默认 "0"=旧路径逐字节不变；B 线不接）。
    "BOK_SLOT_ACTOR",
    "EMOTION_TAG_PROMPT",
    "LLM_FIRST_TOKEN_TIMEOUT_S",
    # LLM 饥荒自适应（第十五波 2026-10-01,call-dc54f542）：机器级首 token 慢
    # （swap/GPU 争用）时拉长首 token 超时与 drain、禁 regen——等原流优于重来。
    "BOK_LLM_FAMINE",
    "BOK_LLM_FAMINE_TTFT_S",
    "BOK_LLM_FAMINE_FIRST_S",
    "BOK_LLM_FAMINE_DRAIN_S",
    "LLM_HISTORY_TURNS",
    "LLM_LATE_ANSWER_DEADLINE_S",
    "LLM_MAX_TOKENS",
    "LLM_REQUEST_RETRIES",
    "LLM_REQUEST_TIMEOUT_S",
    "LLM_TEMPERATURE",
    "LLM_WARMUP",
    "REPLY_MEMORY_LINES",
    # MiniMax TTS：凭据/端点 + bidi 自愈 + 语速/音调/音量 + 叠句增量（运营键全集）：
    "MINIMAX_API_KEY",
    "MINIMAX_BASE_URL",
    "MINIMAX_WS_URL",
    "MINIMAX_REGION",
    # —— F10 bidi 限流守卫(2026-09-23:限流族 task_failed 关连接+退避重试+回落
    #    HTTP+同通连续 3 轮熔断;0=回旧行为) ——
    "BOK_MINIMAX_BIDI_GUARD",
    "MINIMAX_BIDI_AUTO_REWARM",
    "MINIMAX_BIDI_CANCEL_WAIT_S",
    "MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S",
    "MINIMAX_BIDI_PING_MAX_MISS",
    "MINIMAX_BIDI_PING_S",
    "MINIMAX_BIDI_PREWARM_RETRY",
    "MINIMAX_BIDI_STALL_MAX_HEALS",
    "MINIMAX_BIDI_SYNTH_WARMUP",
    "MINIMAX_CONTINUOUS_SOUND",
    "MINIMAX_EMOTION",
    "MINIMAX_FIRST_AUDIO_TIMEOUT_S",
    "MINIMAX_PAUSE",
    "MINIMAX_PAUSE_SECS",
    "MINIMAX_PITCH",
    "MINIMAX_SPEED",
    "MINIMAX_TTS_OVERLAP",
    "MINIMAX_TTS_OVERLAP_CHARS",
    "MINIMAX_TTS_OVERLAP_MS",
    "MINIMAX_VOL",
    "MINIMAX_WS",
    "MINIMAX_WS_MODE",
    "MINIMAX_WS_POOL",
    "BOK_TTS_PREWARM",
    # Qwen3-ASR：agent 侧插件读面（sidecar 进程专属键另走 asr_env，不在此表）：
    "QWEN3_ASR_CHUNK_MS",
    # B 线提交边界三键(2026-10-06 延迟压刀起显式登记:逗号档字数/长度档字数/
    # 句级限速——operator 显式 export 须经 serve 面转发才能触达 B worker)。
    "QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS",
    "QWEN3_ASR_CLAUSE_LEN_CHARS",
    "QWEN3_ASR_COMMIT_MIN_INTERVAL_S",
    "QWEN3_ASR_HESITATION_GATE",
    "QWEN3_ASR_JOIN_HOLD_MS",
    "QWEN3_ASR_JOIN_HOLD_VOCAB",
    "QWEN3_ASR_PAUSE_COMMIT_MIN_CHARS",
    "QWEN3_ASR_PREFLIGHT_LANG_GATE",
    "QWEN3_ASR_SENTENCE_PAUSE_TRIGGER",
    # Qwen3-TTS sidecar 客户端（插件侧）调参：
    "QWEN3_TTS_MAX_TASK_AUDIO_SEC",
    "QWEN3_TTS_OVERLAP",
    "QWEN3_TTS_OVERLAP_CHARS",
    "QWEN3_TTS_OVERLAP_MS",
    # Volcano TTS：凭据/端点/voice 调参：
    "VOLC_ACCESS_TOKEN",
    "VOLC_APP_ID",
    "VOLC_DIALECT",
    "VOLC_LANGUAGE",
    "VOLC_LOUDNESS_RATE",
    "VOLC_RESOURCE_ID",
    "VOLC_SPEAKER",
    "VOLC_SPEECH_RATE",
    "VOLC_TTS_ENDPOINT",
    # —— 碎片轮重问车道（EX-2，2026-09-28）：碎裂/碎片转写 canned 重问（零 TTFT
    #    替掉 LLM 轮）总闸与四闸；默认开/0.45/0.5/2/2，未设不注入=默认档零变化 ——
    "BOK_GARBLED_REASK",
    "BOK_REASK_CONF_MEAN",
    "BOK_REASK_LOW_RATIO",
    "BOK_REASK_MIN_CONTENT_CHARS",
    "BOK_REASK_MAX_CONSEC",
    # —— B 线第一性原理波（2026-10-02）：装配期 MT 探活 kill-switch（缺省开） ——
    "BOK_INTERP_MT_PROBE",
    # —— 编排审计第二波 PR-B（2026-10-02）：A 线 a_reply 车道装配期探活 kill-switch
    #    （缺省开；agent.py 读注入 env Mapping 故静态扫描不强制，prod 转发靠此登记——
    #    BOK_INTERP_MT_PROBE 同款判例；tests/test_a_reply_probe.py 钉 membership） ——
    "BOK_A_REPLY_PROBE",
    # —— B 线 interim 投机翻译（2026-10-06）：prewarm-and-confirm 总闸（缺省开，
    #    0=旧路径逐字节；B 线 worker 专属，_interp_env 透传白名单同键） ——
    "BOK_INTERP_SPEC_MT",
    # —— B 线云端 MT 指令化（2026-10-08 Wave 1「MT 全量切 DeepSeek 试」）：mt 车道
    #    openai 档去 StatelessMTLLM 模板包裹（system 指令曾被丢=ASR 纠错锁死），
    #    缺省开；0=回旧模板包裹（试验逃生口；_interp_env 透传白名单同键） ——
    "BOK_INTERP_MT_CLOUD_INSTRUCT",
    # —— B 线流式交付（2026-10-08 Wave 2「B 线 A 线化」）：MT 流逐子句喂
    #    say(AsyncIterable) 框架自切句首子句即合成；字幕先出（sync_transcription
    #    同闸）；缺省开，0=回旧整句排干+同步字幕档（_interp_env 透传白名单同键） ——
    "BOK_INTERP_MT_STREAM_SAY",
    # —— B 线碎片闸（2026-10-08 Wave 3a）：豆包 FINAL 纯应承碎片（「啊。」「No.」
    #    ~30% finals 独烧全链）hold ≤0.6s 并入下段一次翻译；0=旧路径逐字节；
    #    BOK_INTERP_FRAG_HOLD_S 调窗（坏值回 0.6/负钳 0/上限 2.0；
    #    _interp_env 透传白名单同键） ——
    "BOK_INTERP_FRAG_MERGE",
    "BOK_INTERP_FRAG_HOLD_S",
    # —— demo 质量波（2026-10-06，docs/superpowers/plans/2026-10-06-demo-quality-wave.md）：
    #    W1c 垫话云车道解禁（默认 1=cloud a_reply 车道也 arm 垫话；0 回旧 auto-off）——
    "BOK_FILLER_CLOUD",
    #    W1f 打断四分法逃生口（默认 0=打断确证才弃流；1 回旧「打断瞬间即 abandon」档）——
    "BOK_INTERRUPT_INSTANT_ABANDON",
    #    W6 全程场景底噪（none 缺省 / office / callcenter / car，可拔插循环音轨）——
    "BOK_AMBIENT_SCENE",
    #    W6+ 自定义底噪（2026-10-08 用户拍板「内置合成底噪像电流,自己上传音频」）：
    #    BOK_AMBIENT_SCENE=custom + BOK_AMBIENT_FILE=/abs/room.wav（真房间录音循环），
    #    BOK_AMBIENT_GAIN_DB 可选覆盖播放衰减（缺省 -28dB 同内置轨）——
    "BOK_AMBIENT_FILE",
    "BOK_AMBIENT_GAIN_DB",
    #    W5 声纹锁通话对象（默认 0 关；enrollment+VAD 段相似度 pre-ASR 门）——
    "BOK_SPEAKER_LOCK",
    "BOK_SPEAKER_LOCK_MODEL",
    # —— A 线对偶件（2026-10-07，demo-quality-wave 第二窗）：PrefillSpeculator
    #    云车道放行门（默认 1=DeepSeek openai 档也发 interim 前缀预热
    #    max_tokens=1 预热，吃服务端前缀缓存；0=回旧「仅本地 mlx 端点」host 门） ——
    "BOK_PREFILL_SPEC_CLOUD",
    # —— B 线四语 ASR 车道（2026-10-07）：de/fr/ja/pt 源语走 MiniMax 伪流式
    #    （豆包四语 24/24 幻听实测；MiniMax 24/24 CER≤0.08）总闸（缺省开，
    #    0=回旧装配链逐字节；B 线 worker 专属，_interp_env 透传白名单同键） ——
    "BOK_MINIMAX_ASR",
)
# 历史名（2026-09-18 终审 I1 起的既有调用面/单测锚）：表本体唯一，别名防散。
_BOK_PASSTHROUGH_KEYS = _FORWARD_ENV


def _apply_bok_passthrough_env(env: dict[str, str]) -> None:
    """运营 env 透传（2026-09-18 实弹发现；2026-09-19 `_FORWARD_ENV` 立法收编全集）。

    `_agent_worker_env`/`_agent_prod_env` 都是**白名单 env**（dict 里没写的键一律
    不带 `os.environ`）——`BOK_FLOW_GRAPH=0 python tools/bok.py serve` 写在命令行上
    **到不了 agent worker**，worker 按默认 `"1"` 跑：kill 腿「全程零 FLOW_GRAPH」
    结构性测不出（实弹：worker pid env 只有 2 枚 BOK_ 键、无 BOK_FLOW_GRAPH，
    jump/play 照发，探针如实报 FAIL）。dev 靠 `_start_proc` merge `os.environ` 一直
    全活、prod 封闭白名单全死——表见 `_FORWARD_ENV`；未设/空串不注入（默认档
    逐字节不变）。"""
    for key in _BOK_PASSTHROUGH_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value


def _apply_flow_graph_env(env: dict[str, str]) -> None:
    """历史名（单测/旧调用面）：passthrough 透传的薄别名。"""
    _apply_bok_passthrough_env(env)


def _agent_worker_env(py) -> dict[str, str]:
    """A 线 main worker 的 env(serve 与 monitor 同源单点)。"""
    _cur = models.MODELS["mac"] if paths.is_mac() else models.MODELS["windows"]
    env: dict[str, str] = {
        "PYTHONPATH": paths._repo_pythonpath(),
        "BOK_SERVICE": "agent",
        "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
        "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
        "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
        "CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"),
        "MLX_LLM_BASE_URL": os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        "MLX_LLM_MODEL": models.model_path({**_cur, "llm": models.resolve_llm_repo(_cur)}, "llm"),
    }
    _apply_judge_env(env, _cur)
    _apply_bok_passthrough_env(env)
    # .venv312 OpenSSL 无默认 CA 束 → MiniMax WSS 必炸;固化 SSL_CERT_FILE。
    _bake_ssl_cert_file(env, py)
    return env


def _apply_interp_direction_env(env: dict, direction: str) -> dict:
    """方向级服务覆盖钩子（全双工分 GPU / 云端混合的接线点，2026-09-16）。

    B 线 fwd/rev 两个 worker 共享 _interp_env，两路同时说话时 ASR/MT 请求在
    同一块 GPU 上排队。REV 方向可用 `*_REV` env 把 ASR/MT 指到第二套端点
    （第二实例/第二台机/云端适配器），fwd 保持默认——排队问题的第一指定解。
    只认 REV 方向的覆盖；fwd 恒走默认（单向说话是主场景，零配置零变化）。"""
    if direction != "rev":
        return env
    for base in (
        "QWEN3_ASR_BASE_URL",
        "QWEN3_ASR_MODEL",
        "MT_LLM_BASE_URL",
        "MT_LLM_MODEL",
    ):
        v = os.environ.get(f"{base}_REV")
        if v:
            env[base] = v
    return env


def _agent_prod_env() -> dict[str, str]:
    """agent/interp worker 生产环境（与 cmd_serve 同源）。"""
    _cur = models.MODELS["mac"] if paths.is_mac() else models.MODELS["windows"]
    env = {
        "PYTHONPATH": paths._repo_pythonpath(),
        "BOK_SERVICE": "agent",
        "LIVEKIT_URL": os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"),
        "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY", "devkey"),
        "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET", "devsecret"),
        "CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"),
        "MLX_LLM_BASE_URL": os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        "MLX_LLM_MODEL": models.model_path({**_cur, "llm": models.resolve_llm_repo(_cur)}, "llm"),
    }
    _apply_judge_env(env, _cur)
    _apply_bok_passthrough_env(env)
    # .venv312 OpenSSL 无默认 CA 束 → MiniMax WSS 必炸 SSLCertVerificationError；
    # 固化 SSL_CERT_FILE（P5 遗留项），interp 经 _interp_env 的 dict 拷贝继承。
    return _bake_ssl_cert_file(env, paths.repo_python())


def _interp_env(agent_env: dict[str, str]) -> dict[str, str]:
    """B 线 interpreter 追加 env:MT 翻译小模型(:1236,可选增强)。

    MT_LLM_MODEL 必须是后端真认的模型路径(与主 LLM 同一解析);MT_* 只在
    模型目录在盘或 :1236 已健康(与 _start_mt_llm 同判)时下发——模型缺失/
    Windows 无 mt 条目时完全不注入,interpret 侧按 env 有无回退主 LLM
    :1235/DeepSeek,避免 worker 指去死端口整场无声。MINIMAX_* 不在这里注入
    ——TTS 供应商/音色由 interpret.py 按设置与方向解析。
    """
    env = dict(agent_env)
    # B 线句级提交(2026-09-16 起)与 A 线同档默认开：interpret.py 的
    # turn_handling 已切 turn_detection=stt（与句级 FINAL 成对，_turn_handling_opts
    # 单源复用 A 线 _turn_detection_mode_from_env/_endpointing_delays_from_env），
    # STT 说话中按句 FINAL+EOS 成轮 → 翻译+TTS 与源语音重叠，同传粒度从
    # 「停嘴整段」提前到句级。kill-switch 配对全自动：TURN_DETECTION≠stt
    # (空串/vad/EOT) 时 livekit_plugins.sentence_commit_enabled() 自行熄火 +
    # endpointing min_delay 自动回 ≥0.35 地板，无需动这里。setdefault 不抢用户
    # 显式 env（QWEN3_ASR_SENTENCE_COMMIT=0 仍是应急逃生口）。
    env.setdefault("QWEN3_ASR_SENTENCE_COMMIT", "1")
    # B 线子句级提交(2026-09-16):逗号/顿号/分号也作提交边界——译员按子句跟,
    # 长句唔使等整句讲完才出译文(「说话时间+生成时间」体感长的主刀)。默认 1,
    # 显式 0 逃生;A 线 worker 唔带此 env,客服轮次仍按句。
    env.setdefault("QWEN3_ASR_CLAUSE_COMMIT", "1")
    # B 线长度触发子句提交(2026-09-17 边说边译档):连续语流无逗号无 0.45s 停顿
    # 时,滑窗未提交前缀攒够字数(默认 10)且跨窗稳定即就地切句——标点档/停顿档
    # 的第三事件源,译出声不等人讲完。默认 1,显式 0 逃生;A 线唔带此 env。
    env.setdefault("QWEN3_ASR_CLAUSE_LEN_COMMIT", "1")
    # B 线延迟压刀(2026-10-06):提交边界三收紧——demo-cloud 实弹分段账
    # (perceived_ms 838→3878 同输入方差)定位大头=提交闸排队等待而非模型腿
    # (mt_ms=260 恒定/ASR_MS 190-320/TTS 首音频 270-460≈地板 1s)。B 线专属:
    # ①逗号档字数 8→6(自然短语组提前半拍);②长度档字数 10→8(地板即 8);
    # ③句级提交限速 1.5→1.0s(连珠句排队窗缩短)。A 线 worker 唔带这些 env,
    # 客服线逐字节零变化;三键均显式 env 可覆盖(经下方透传白名单)。
    env.setdefault("QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS", "6")
    env.setdefault("QWEN3_ASR_CLAUSE_LEN_CHARS", "8")
    env.setdefault("QWEN3_ASR_COMMIT_MIN_INTERVAL_S", "1.0")
    # VAD 停嘴门槛(2026-10-02 收编):旧版在此 setdefault 0.35(2026-09-17 B 线
    # 专属调参,当时 A 线 0.45)——但 env 优先级压过设置面,设置页对 B 线永久
    # 说谎(改了不生效)。现拆 setdefault:B 线与 A 线同读设置面 vad 段
    # (interpret _cfg_float / agent _vad_float 同一序:显式 env 部署覆盖 >
    # 设置页 > 缺省 0.35),显式 env 仍经下方透传白名单下发。当前设置值 0.35=
    # 拆钉零行为变化;后续调门槛只动设置页,两线同源。
    # B 线开关透传(_agent_worker_env 是白名单 env,不透传 os.environ——
    # 不显式带上的话文档里的逃生门在 dev/prod 栈都是死的,2026-09-16 实证)。
    for _k in (
        "BOK_INTERP_MT_CONTEXT",
        # E5 增补 2026-09-21:MT 出口确定性语言校验+单次强化重试总闸。默认开
        # ——正常轮零额外延迟,仅错语言轮多一次往返;=0 回退旧「出口不校验」档。
        "BOK_INTERP_MT_LANGGUARD",
        # B 线 interim 投机翻译 2026-10-06：prewarm-and-confirm 总闸，缺省开，
        # 0=旧路径逐字节（_FORWARD_ENV 已登记，B 线 worker 专属）。
        "BOK_INTERP_SPEC_MT",
        # B 线四语 ASR 车道 2026-10-07：de/fr/ja/pt 源语走 MiniMax 伪流式总闸，
        # 缺省开，0=回旧装配链逐字节（_FORWARD_ENV 已登记，B 线 worker 专属）。
        "BOK_MINIMAX_ASR",
        "BOK_INTERP_REV_AUDIO",
        "BOK_INTERP_BACKLOG",
        "BOK_INTERP_MAX_BACKLOG_S",
        "BOK_INTERP_VOICE_TAGS",
        # B 线云端 MT 指令化 2026-10-08 Wave 1：mt 车道 openai 档去 StatelessMTLLM
        # 模板包裹（system 指令曾被丢=纠错能力锁死），缺省开；0=回旧模板包裹
        # （「MT 全量切 DeepSeek 试」的逃生口，B 线 worker 专属）。
        "BOK_INTERP_MT_CLOUD_INSTRUCT",
        # B 线流式交付 2026-10-08 Wave 2：MT 流逐子句喂 say(AsyncIterable)——框架
        # 自切句首子句即合成;字幕先出(sync_transcription=False 同闸);缺省开,
        # 0=回旧整句排干+同步字幕档（B 线 worker 专属）。
        "BOK_INTERP_MT_STREAM_SAY",
        # B 线碎片闸 2026-10-08 Wave 3a：纯应承碎片 hold-and-merge（总闸缺省开，
        # 0=旧路径逐字节；FRAG_HOLD_S 调窗，B 线 worker 专属）。
        "BOK_INTERP_FRAG_MERGE",
        "BOK_INTERP_FRAG_HOLD_S",
        # B 线缺源遥测 2026-09-30：fwd 订阅空挂零痕迹(call-72112fd7)——看护
        # 每 N 秒分辨「对端没发麦」vs「发了订不上」打观测行;=0 关。
        "BOK_INTERP_SRC_TELEMETRY",
        "BOK_INTERP_SRC_TELEMETRY_S",
        # B 线订阅自愈 2026-09-30：set_subscribed 官方手动订阅口(对账定案)
        # ——检测到已发布未订上即重发订阅;=0 回纯观测档。
        "BOK_INTERP_SRC_HEAL",
        # B 线 MiniMax 硬失败兜底 2026-09-27：主档云端 MiniMax 失败时 FallbackAdapter
        # 备档=本地 Qwen3-TTS 是否装备。=0 显式跳过本地 TTS 时不装备，与 bok.py
        # _local_tts_needed 同键语义；未设=装备，本地 sidecar 未跑时逐请求穿透。
        "BOK_LOCAL_TTS",
        "MINIMAX_MODEL",
        "QWEN3_ASR_SENTENCE_COMMIT",
        "QWEN3_ASR_CLAUSE_COMMIT",
        "QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS",
        "QWEN3_ASR_CLAUSE_LEN_COMMIT",
        "QWEN3_ASR_CLAUSE_LEN_CHARS",
        "QWEN3_ASR_COMMIT_MIN_INTERVAL_S",
        "VAD_MIN_SILENCE_DURATION",
    ):
        if os.environ.get(_k):
            env[_k] = os.environ[_k]
    mt_model = models._mt_llm_model(models.MODELS["mac"] if paths.is_mac() else models.MODELS["windows"])
    if (mt_model and Path(mt_model).exists()) or core.healthy(1236):
        env["MT_LLM_BASE_URL"] = os.environ.get("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
        if mt_model:
            env["MT_LLM_MODEL"] = mt_model
    return env
