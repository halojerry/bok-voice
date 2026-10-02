"""模型 JSON 输出的**保守补齐**（2026-09-21）——治「内容全对、少一个括号」这类坏法。

## 为什么需要它

`control_plane.summarize._parse` 对模型输出做 `re.search(r"\\{.*\\}")` 再 `json.loads`，
失败就静默退指标摘要。2026-09-21 真栈实测（本机 9B @1237，真实通话转写，6/6 复现）：

模型吐的 JSON **内容与结构全对**，只是**漏掉了最外层那个 `}`**——

```
{ "summary": "…", "new_topics": [ {…}, {…} ], "insight": { …, "language": "zh" } }
                                                                                 ↑ 少这个
```

`finish_reason=stop`、只用了 312/512 token，所以**不是截断**；是模型自身漏收尾。
后果是 `json.loads` 报 `Expecting ',' delimiter`（在外层对象上等逗号/闭合），
整通纪要退成「本场共 N 轮」的桩文本，而 `new_topics`/`insight` 全丢。

## 尺度：只补**未闭合的括号**，绝不猜内容

只做一件事：按字符串感知的括号栈扫一遍，把**欠着的闭括号按栈序补到末尾**。
不匹配的闭括号、未终止的字符串、以及「本来就平衡但依然解析失败」（例如漏逗号）
一律返 ``None``——那类是内容坏了，补不出来，交给调用方走原 fallback，
**不要在这里发明修复**（修坏的 JSON 会把错答案沉淀进知识库，比丢掉更糟）。
"""

from __future__ import annotations

import json

_CLOSERS = {"{": "}", "[": "]"}


def close_unclosed_json(text: str) -> str | None:
    """把未闭合的 `{`/`[` 补齐到末尾；无可补（或不可安全补）返 ``None``。

    不可安全补 = 闭括号与栈顶不匹配 / 字符串未终止——那说明不是「漏收尾」，
    是更深的坏，猜下去只会造出似是而非的对象。
    """
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in _CLOSERS:
            stack.append(_CLOSERS[ch])
        elif ch in ("}", "]"):
            if not stack or stack[-1] != ch:
                return None  # 错位闭括号：不是漏收尾，别猜
            stack.pop()
    if in_str or not stack:
        return None  # 未终止字符串 / 本来就平衡（另一种坏法）
    return text + "".join(reversed(stack))


def loads_lenient(text: str) -> dict | None:
    """严格解析优先；失败且是「漏收尾」型才补括号重试。返 dict 或 ``None``。"""
    src = (text or "").strip()
    if not src:
        return None
    try:
        data = json.loads(src)
    except Exception:  # noqa: BLE001 - 坏 JSON 是预期输入，走补齐路径
        data = None
    if isinstance(data, dict):
        return data
    repaired = close_unclosed_json(src)
    if repaired is None:
        return None
    try:
        data = json.loads(repaired)
    except Exception:  # noqa: BLE001 - 补不回来就是补不回来
        return None
    return data if isinstance(data, dict) else None
