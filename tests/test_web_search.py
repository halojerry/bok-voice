"""web_search 纯函数单测（不联网，确定性）。"""

from agent_runtime.web_search import _shorten_query, _wiki_domain, _trim


def test_wiki_domain_maps_language():
    assert _wiki_domain("zh") == "zh.wikipedia.org"
    assert _wiki_domain("yue") == "zh-yue.wikipedia.org"
    assert _wiki_domain("cantonese") == "zh-yue.wikipedia.org"
    assert _wiki_domain("en") == "en.wikipedia.org"
    assert _wiki_domain("") == "zh.wikipedia.org"


def test_shorten_query_strips_question_words():
    # “多少”被剥，保留实义词“米/高度”供搜索。
    assert _shorten_query("广州塔高度多少米？") == "广州塔高度米"
    assert _shorten_query("湾仔活道係邊度啊？") == "湾仔活道係邊度"
    # 纯关键词原样返回（长度内）
    assert _shorten_query("粤菜") == "粤菜"


def test_trim_respects_limit():
    assert _trim("你好") == "你好"
    long = "x" * 1000
    assert len(_trim(long)) <= 600
    assert _trim(long).endswith("…")
