"""authoring-time 语气标记 pass(2026-10-07 拍板)——LLM 在**写话术时**给步文案
拼白名单语气标记(不是通话时),产出交给模板编辑器表单(未保存态),人类审阅
后才走既有 PUT 保存。本模块是 CP 侧纯函数+编排单点;端点只回 draft JSON,
**零 DB 写**(镜像「检测本地端点」按钮的 draft-only 先例)。

管道(已落地,本模块只产出原料):步文案可带白名单标记 → agent 侧
`script_line_speech_text` 规范形(ASCII 小写 `(marker)`,单源在
apps/agent/agent_runtime/voice_style.py)→ pregen 把标记烤进缓存音频、运行时
同键命中。CP **不 import agent 包**——白名单口径由测试源级 parity pin 钉住
(VOICE_TAG_ALLOWED ⊆ voice_style.VOICE_TAG_WHITELIST,镜像
test_branch_syntax_parity 读源模式)。

纪律(钉在 apply 的守卫里,LLM 不听话就拒):
- 允许标记=VOICE_TAG_ALLOWED(白名单子集,ASCII 小写括号形);全角/大写归一
  成规范形后再判;未知括号词只在句首/句读后剥(舞台指示「(停顿两秒)」主发位),
  句中当内容保留——镜像 voice_style.sanitize 的未知 token 政策。
- 每条可标记面(一步正稿/开场白/收尾)至多 2 枚标记,超出剥掉留前 2。
- **禁改写原句**:剥净标记/停顿后的骨架文本必须与原文骨架逐字一致,不等=
  整行拒用回原文(结构性保证,不靠 prompt 自觉)。
- 只标记「会被念出来」的面:每步只取正稿头(首个分支行/注意行之前的段),
  分支/注意行逐字节保留(分支语法零风险);opening/closing 整体一条。

LLM 车道=mining(2026-09-25 模型路由;后台创作重活车道,复用 qa_cluster 的
`_llm_chat`/`_discover_model` 单一实现,temperature 0/max_tokens 2048/
timeout 60——交互式按钮,比挖掘批处理的 120s 收紧)。LLM 网络/解析失败=
VoiceTagPassError,端点转 502 带 stage,**绝不 500**。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata

import httpx
from bok_voice_core.branch_syntax import BRANCH_LINE_RE, NOTE_LINE_RE
from bok_voice_core.model_routes import PROVIDER_OPENAI, resolve_route

from .deps import read_model_routing_raw

# LLM 调用/模型发现复用 qa_cluster 的 mining 车道单一实现(本模块测试
# monkeypatch voice_tag_pass._llm_chat/_discover_model,与 test_qa_cluster_cp
# 同款假 LLM 姿势;qa 侧 monkeypatch 不受影响——两处各持自己的名字绑定)。
from .qa_cluster import _discover_model, _llm_chat  # noqa: F401  (测试 monkeypatch 面)

# ---------------------------------------------------------------------------
# 允许标记(CP 侧口径单点):白名单子集。取通话线 NATURALNESS_BLOCK 鼓励主动
# 使用的五件表现力标记(breath/sighs/chuckle/laughs/emm);clear-throat/inhale/
# exhale/coughs 四件=通话线 prompt 明示「不要主动使用」的族,authoring 线同样
# 不放(罐头会被反复播放,咳/清嗓类标记在话术稿里语义更差)。parity pin 在
# tests/test_voice_tag_pass.py:VOICE_TAG_ALLOWED ⊆ voice_style.VOICE_TAG_WHITELIST。
# ---------------------------------------------------------------------------
VOICE_TAG_ALLOWED: frozenset[str] = frozenset(
    {"breath", "sighs", "chuckle", "laughs", "emm"}
)

# 每条可标记面(一步正稿/开场白/收尾)标记上限(2026-10-07 拍板 ≤2 枚/步)。
MAX_TAGS_PER_LINE = 2
# 停顿 token 上限(标记之外的独立预算;值域 0.05-0.80 钳制在 agent 侧规范形做,
# CP 只防 LLM 灌一排停顿)。
MAX_PAUSES_PER_LINE = 3

# 括号 token(ASCII+全角括号,同 voice_style._PAREN_RE 形状)
_PAREN_RE = re.compile(r"[（(]([^（）()]{1,24})[）)]")
# 停顿 token:任意形态先收(坏格式剥),数值合法性按 voice_style 同款判
_PAUSE_ANY_RE = re.compile(r"<#\s*([^#<>]{0,24}?)\s*#>")
_PAUSE_NUM_RE = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,3})?$")
# 句读边界(未知括号词剥除许可位,镜像 voice_style._SENT_BOUNDARY_RE)
_SENT_BOUNDARY_RE = re.compile(r"[。！？!?.；;\n]")


class VoiceTagPassError(RuntimeError):
    """LLM 网络/发现/解析失败——端点转 502 带 stage(绝不 500)。"""


# ---------------------------------------------------------------------------
# 纯函数:收集可标记面 → prompt → 解析回包 → 守卫+组装 draft
# ---------------------------------------------------------------------------


def _norm_tag(inner: str) -> str:
    """标记归一(镜像 voice_style._norm_tag 语义):NFKC 全角归一+小写+去空白/连字
    变体。归一后落 VOICE_TAG_ALLOWED 才算合法标记;方向安全——归一实现若与
    agent 侧漂移,最坏=标记在合成层被剥(文本无损),不会出现「当文本念出来」。"""
    return (
        unicodedata.normalize("NFKC", inner)
        .strip()
        .lower()
        .replace("－", "-")
        .replace(" ", "")
        .replace("\u3000", "")
    )


def _split_script_head(ref: str) -> tuple[str, str]:
    """把步 ref 拆成(正稿头, 尾段):首个分支行/注意行起逐字节归尾段。

    分支行语法单源=bok_voice_core.branch_syntax(C1 立法,与 flow.py 同一对象),
    这里不立第二份正则。尾段(分支/注意)逐字节保留——authoring 线不碰分支,
    分支罐头/语法闸零风险。"""
    head_lines: list[str] = []
    tail: list[str] = []
    for line in str(ref or "").splitlines():
        if not tail and not (
            BRANCH_LINE_RE.match(line.strip()) or NOTE_LINE_RE.match(line.strip())
        ):
            head_lines.append(line)
            continue
        tail.append(line)
    return "\n".join(head_lines), "\n".join(tail)


def collect_lines(template_dict: dict) -> list[dict]:
    """从模板 dict 收集可标记面:opening/closing 整体一条;每步正稿头一条。

    返回 [{id, text, kind, step_index}];空文案不出条。id 稳定(重放/审计对账)。
    """
    out: list[dict] = []
    opening = str(template_dict.get("opening") or "").strip()
    if opening:
        out.append({"id": "opening", "text": opening, "kind": "opening", "step_index": -1})
    closing = str(template_dict.get("closing") or "").strip()
    if closing:
        out.append({"id": "closing", "text": closing, "kind": "closing", "step_index": -1})
    try:
        steps = json.loads(str(template_dict.get("steps_json") or "") or "[]")
    except (ValueError, RecursionError):
        steps = []
    if not isinstance(steps, list):
        steps = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        ref = str(step.get("ref") or "")
        head, _tail = _split_script_head(ref)
        head = head.strip()
        if not head:
            continue
        out.append({"id": f"s{idx + 1}", "text": head, "kind": "step", "step_index": idx})
    return out


def build_voice_tag_prompt(steps_text: str) -> tuple[str, str]:
    """构建(system, user)双段 prompt。

    steps_text=collect_lines 产出的 JSON 串(逐条 {id,text});输出契约=严格
    JSON {"lines":[{id,text}]} 且**所有 id 都要回**(没改的原文返回)。纪律与
    CP 侧守卫同款(白名单子集/≤2 枚/禁改写/停顿少量),LLM 违反会被守卫拒,
    prompt 只是第一道。
    """
    allowed = " ".join(f"({t})" for t in sorted(VOICE_TAG_ALLOWED))
    system = (
        "【语气标记插入】你给客服话术稿插入语音语气标记,让合成语音更像真人。纪律:\n"
        f"- 只允许这些标记:{allowed};停顿标记形如 <#0.3#>(秒,0.05-0.80)可少量使用。\n"
        "- 绝不改写、增删、调换任何文字,只插入标记;原文的{变量}、标点、空行一字不动。\n"
        "- 每条至多 2 个标记;短句(10 字以内)不放;拿不准就不放;不是每条都要加。\n"
        "- 标记不放行首;自然位置=句读之后/换气点(如「您好。(breath)请问是{姓名}吗?」)。\n"
        "- 按语境选:安抚/致歉用 (sighs) 或 (breath);查询/思考用 (emm);轻快满意用 "
        "(chuckle);客户讲到有趣处可 (laughs)。\n"
        '- 输出严格 JSON:{"lines":[{"id":"原id","text":"加了标记的整条文案"}]},'
        "所有 id 都必须返回,没加标记的条目原文返回,不要输出任何其他文字。"
    )
    user = str(steps_text or "")
    return system, user


def parse_marked_lines(raw: str) -> dict[str, str]:
    """解析 LLM 回包 → {id: text}。宽容形态:剥 ``` 围栏、顶层 dict 或裸数组;
    解析不了/空回包=空 dict(调用方按「无改动」处理,不炸)。"""
    text = str(raw or "").strip()
    if not text:
        return {}
    if text.startswith("```"):  # llm 偶发 ```json … ```
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    if isinstance(data, dict):
        data = data.get("lines")
    if not isinstance(data, list):
        return {}
    out: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or "").strip()
        val = item.get("text")
        if key and isinstance(val, str):
            out[key] = val
    return out


def _skeleton(text: str) -> str:
    """骨架化(禁改写校验的比对面,对 original/guarded 同一侧应用):剥合法标记、
    停顿 token、舞台指示位的未知括号词,收敛空白——「剥掉这些之后两边必须逐字
    一致」。与 _guard_line 的 token 政策同源:守卫对合法标记换规范形/舞台位剥除
    /停顿清理,骨架对同类 token 一律剥净,两边口径天然对齐。"""
    text = str(text or "")

    def _paren(m: re.Match) -> str:
        norm = _norm_tag(m.group(1))
        if norm in VOICE_TAG_ALLOWED:
            return ""
        prefix = text[: m.start()]
        if not prefix.strip() or _SENT_BOUNDARY_RE.search(prefix[-2:]):
            return ""  # 舞台指示位剥除(与守卫同判)
        return m.group(0)  # 句中括号=内容,保留

    out = _PAREN_RE.sub(_paren, text)
    out = _PAUSE_ANY_RE.sub("", out)
    return re.sub(r"\s+", " ", out).strip()


def _guard_line(original: str, returned: str) -> str:
    """单条守卫:白名单子集归一/未知括号句首剥/≤2 标记/停顿清理/禁改写校验。

    返回守卫后的文本;违反「禁改写原句」或产出空串 → 原样返回(拒用)。守卫是
    **人审前的质量闸**,不是合成规范形——最终规范形单源仍是 agent 侧
    script_line_speech_text(pregen/运行时同调),这里不复制那份实现。

    实现序:括号 pass 必须先于停顿 pass——_paren_repl 的句读边界判定引用原文
    returned 的下标,若停顿替换(变长)先行,下标错位;停顿 repl 自包含无此依赖。"""
    original = str(original or "")
    returned = str(returned or "")
    kept_markers = 0
    kept_pauses = 0

    def _paren_repl(m: re.Match) -> str:
        nonlocal kept_markers
        norm = _norm_tag(m.group(1))
        if norm in VOICE_TAG_ALLOWED:
            if kept_markers >= MAX_TAGS_PER_LINE:
                return ""  # 超预算剥除(留前 2)
            kept_markers += 1
            return f"({norm})"  # 规范形(全角/大写归一)
        prefix = returned[: m.start()]
        if not prefix.strip() or _SENT_BOUNDARY_RE.search(prefix[-2:]):
            return ""  # 舞台指示位剥除(「(停顿两秒)」类,合成层反正会剥)
        return m.group(0)  # 句中括号=可能的内容,保留

    def _pause_repl(m: re.Match) -> str:
        nonlocal kept_pauses
        raw = (m.group(1) or "").strip()
        if not _PAUSE_NUM_RE.fullmatch(raw):
            return ""  # 坏格式(<#abc#>/<# #>)剥除
        if kept_pauses >= MAX_PAUSES_PER_LINE:
            return ""  # 超预算剥除
        kept_pauses += 1
        return m.group(0)

    buf = _PAREN_RE.sub(_paren_repl, returned)
    buf = _PAUSE_ANY_RE.sub(_pause_repl, buf)
    buf = re.sub(r"^(?:\s*<#[^#<>]{0,24}#>\s*)+", "", buf)  # 行首停顿剥
    buf = re.sub(r"(?:\s*<#[^#<>]{0,24}#>\s*)+$", "", buf)  # 行尾停顿剥
    buf = re.sub(r"(<#[^#<>]{0,24}#>)\s*(<#[^#<>]{0,24}#>)", r"\1", buf)  # 相邻停顿合一
    buf = re.sub(r"[ \t]{2,}", " ", buf).strip()
    if not buf:
        return original
    # 禁改写校验:骨架不一致=LLM 动了原句(改字/换标点/吞句) → 整行拒用回原文。
    if _skeleton(buf) != _skeleton(original):
        return original
    return buf


def apply_voice_tag_draft(template_dict: dict, marked_lines: dict[str, str]) -> dict:
    """组装 draft(纯函数,零 DB 写):marked_lines=parse_marked_lines 的产出。

    逐条过守卫;守卫后与原文相同(没加/被拒)不计入 changed。返回 draft dict:
    {template_id, language, draft_only, steps_json, opening, closing, changed,
    lines}。steps_json 重组只改被标记步的 ref 正稿头,其余键(goal/say/emotion/
    scene)与尾段逐字节保留。"""
    lines = collect_lines(template_dict)
    try:
        steps = json.loads(str(template_dict.get("steps_json") or "") or "[]")
    except (ValueError, RecursionError):
        steps = []
    if not isinstance(steps, list):
        steps = []
    steps = [s if isinstance(s, dict) else {"goal": "", "ref": str(s)} for s in steps]
    draft_steps = list(steps)
    opening = str(template_dict.get("opening") or "")
    closing = str(template_dict.get("closing") or "")
    changed = 0
    for item in lines:
        marked = marked_lines.get(item["id"])
        if not marked or not str(marked).strip():
            continue
        guarded = _guard_line(item["text"], marked)
        if guarded == item["text"]:
            continue  # 没加标记/被拒:不计改动
        changed += 1
        if item["kind"] == "step":
            idx = int(item["step_index"])
            _head, tail = _split_script_head(str(steps[idx].get("ref") or ""))
            new_ref = guarded
            if tail:
                # 尾段逐字节保留(含原有换行);guarded 只替换正稿头。
                sep = "" if tail.startswith("\n") else "\n"
                new_ref = f"{guarded}{sep}{tail}"
            new_step = dict(steps[idx])
            new_step["ref"] = new_ref
            draft_steps[idx] = new_step
        elif item["kind"] == "opening":
            opening = guarded
        elif item["kind"] == "closing":
            closing = guarded
    return {
        "template_id": str(template_dict.get("id") or ""),
        "language": str(template_dict.get("language") or ""),
        "draft_only": True,
        "steps_json": json.dumps(draft_steps, ensure_ascii=False),
        "opening": opening,
        "closing": closing,
        "changed": changed,
        "lines": len(lines),
    }


# ---------------------------------------------------------------------------
# 编排:车道取数 → LLM → 解析 → 组装。端点薄壳只调这里。
# ---------------------------------------------------------------------------


def _base_url() -> str:
    return os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1").rstrip("/")


def _lane() -> tuple[str, str, str, bool]:
    """mining 车道解析(镜像 qa_cluster._mining_lane):路由表命中吃 base_url/
    model/api_key;未命中=MLX_LLM_BASE_URL env 链逐字节同旧。云端档才携带
    api_key/enable_thinking(本地档 "mlx" 不塞请求头)。"""
    route = resolve_route("mining", os.environ, read_model_routing_raw())
    if route.source != "routing":
        return _base_url(), "", "", False
    if route.provider == PROVIDER_OPENAI:
        return route.base_url, route.model, route.api_key, route.enable_thinking
    if route.base_url:
        return route.base_url, route.model, "", False
    return _base_url(), "", "", False


def _resolve_model(base_url: str) -> str:
    model = _discover_model(base_url)
    if not model:
        raise VoiceTagPassError("llm 无可用模型(/v1/models 空)——先起 bok serve 的 LLM")
    return model


def generate_draft(template_dict: dict, *, timeout: float = 60.0) -> dict:
    """全链:收集→prompt→LLM(mining 车道)→解析→守卫组装 draft。

    LLM 网络/HTTP/发现/解析失败统一 VoiceTagPassError(端点转 502)。零 DB 写。
    """
    lines = collect_lines(template_dict)
    if not lines:
        return apply_voice_tag_draft(template_dict, {})
    base_url, model_override, api_key, thinking = _lane()
    try:
        model = model_override or _resolve_model(base_url)
    except httpx.HTTPError as exc:
        raise VoiceTagPassError(f"llm models 探测失败({base_url}): {exc!r}") from exc
    steps_text = json.dumps(
        [{"id": it["id"], "text": it["text"]} for it in lines], ensure_ascii=False
    )
    system, user = build_voice_tag_prompt(steps_text)
    try:
        text = _llm_chat(
            base_url,
            model,
            system,
            user,
            timeout=timeout,
            api_key=api_key,
            enable_thinking=thinking,
        )
    except Exception as exc:  # noqa: BLE001 - 网络/协议失败统一 502 口径
        raise VoiceTagPassError(f"llm 请求失败: {exc!r}") from exc
    marked = parse_marked_lines(text)
    return apply_voice_tag_draft(template_dict, marked)
