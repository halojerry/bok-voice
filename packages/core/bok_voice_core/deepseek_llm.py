"""DeepSeek 端点请求体契约的**单一落点**（2026-09-21）——官方文档事实 + 我们的预算约束。

官方事实（``api-docs.deepseek.com/api/create-chat-completion``，2026-09-21 查证）：

- ``thinking`` 是 ``/chat/completions`` 的**请求体字段**，``{"type": "enabled"|"disabled"}``，
  **默认 enabled**；用 OpenAI SDK 时**必须放进 ``extra_body``**（SDK 不认识该字段）。
- ``reasoning_effort`` 同一开关的另一入口：``none``=关，``low``/``high``/``max``=开（默认 ``high``）。
- 旧模型名 ``deepseek-chat`` / ``deepseek-reasoner`` **2026-07-24 停用**，过渡期分别
  指向 ``deepseek-v4-flash`` 的非思考 / 思考模式；当前在线的名字是 ``deepseek-flash`` /
  ``deepseek-v4-pro``（实测 ``GET /models``，2026-09-21）。
- 磁盘前缀缓存自动生效（**从第 0 个 token 起严格前缀**才算命中），命中价比未命中低一个
  数量级，``usage.prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens`` 可读。

**为什么要在这层兜**：我们**所有**调用点的 max_tokens 都很小（对话 160 / 判据 8-32 /
纪要 512），而思考默认开——预算会被整段烧在 reasoning 上、正文出**空串**。实测
``deepseek-flash`` 默认档 max_tokens=300：``reasoning_tokens=300``、``content`` 长度 0
（``finish_reason`` 照样是正常值，故上游看不出错）。通话侧的表现是**静默哑火**
（2026-09-21 判据换云首轮 6/6 ``unclear conf=0.00`` + 一通哑声，就是踩的这个坑）。

故本模块的规矩：**端点是 DeepSeek → 缺省关思考**；沉淀/纪要这类非实时后台任务由调用方
显式传 ``enabled``。非 DeepSeek 端点（本地 MLX :1235/:1236/:1237）一律返空 dict——它们
不认识 ``thinking`` 字段，多发一个未知键是纯风险。

纯函数、零 I/O、零 env（env 读取留在调用点，与 ``polish_wiring`` 的分层同款）。
"""

from __future__ import annotations

from urllib.parse import urlsplit

DEEPSEEK_HOST = "api.deepseek.com"

# 允许的档位；其余值（含空）按缺省关思考处理。
VALID_MODES = ("enabled", "disabled")


def is_deepseek_endpoint(base_url: str) -> bool:
    """base_url 是否指向 DeepSeek 官方端点（含子域，排除前缀伪装的伪域名）。

    ``evilapi.deepseek.com`` 是真子域（放行），``api.deepseek.com.attacker.tld``
    是伪域名（拒绝）——故用「等于或点号后缀」判定，不用裸 ``endswith``。
    """
    host = (urlsplit((base_url or "").strip()).hostname or "").lower().strip(".")
    if not host:
        return False
    return host == DEEPSEEK_HOST or host.endswith("." + DEEPSEEK_HOST)


def thinking_extra_body(base_url: str, mode: str = "") -> dict:
    """该端点该带的 ``thinking`` 请求体片段；非 DeepSeek 端点返 ``{}``。

    ``mode`` 空/非法 = 缺省关思考（见模块 docstring 的预算理由）；``"enabled"`` 交给
    澄清思考的调用点（挂断后纪要、离线蒸馏）。
    """
    if not is_deepseek_endpoint(base_url):
        return {}
    clean = (mode or "").strip().lower()
    return {"thinking": {"type": clean if clean in VALID_MODES else "disabled"}}
