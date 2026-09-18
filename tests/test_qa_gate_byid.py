"""QaIndex.by_id:话术图 play_qa 绑定按条目 id 直取(spec §4.1)。"""
from agent_runtime.qa_gate import QaIndex


def test_by_id_hits_and_misses():
    entries = [
        {"id": "qa-1", "question_text": "怎么退款", "answer_text": "稍等帮您查"},
        {"id": "", "question_text": "无id", "answer_text": "x"},
        {"id": "qa-3", "question_text": "", "answer_text": "空问题不入索引"},
    ]
    idx = QaIndex(entries)
    hit = idx.by_id("qa-1")
    assert hit is not None and hit["answer_text"] == "稍等帮您查"
    assert idx.by_id("qa-missing") is None
    assert idx.by_id("") is None
