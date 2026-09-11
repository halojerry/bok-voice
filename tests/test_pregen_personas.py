"""tts-pregen 按人设物化纯函数测试(task-14b:音色三态解析+计划组装+计数口径)。

音色解析与运行时 MiniMaxTTS._resolve_voice 同款:persona 音色字段(dict/JSON 串/
单音色串三态)经运行时同源 _assemble_minimax_voice_map 组 map 后,取 lang 键、
缺省回落 zh 键,全缺=空串(调用方按「该语言无音色」跳过)。全部纯函数,零网络
零 provider 构造。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pregen_tts  # noqa: E402


# ---- _resolve_voice_map:三态解析(MiniMaxTTS._resolve_voice 同款) ----


def test_resolve_voice_map_dict_takes_lang_key():
    m = {"zh": "v_zh", "cantonese": "v_canto", "en": "v_en"}
    assert pregen_tts._resolve_voice_map(m, "zh") == "v_zh"
    assert pregen_tts._resolve_voice_map(m, "cantonese") == "v_canto"
    assert pregen_tts._resolve_voice_map(m, "en") == "v_en"


def test_resolve_voice_map_dict_missing_lang_falls_back_zh():
    # 缺 lang 键回落 zh 键(运行时同款);zh 也缺=空串(无该语言音色)。
    assert pregen_tts._resolve_voice_map({"zh": "v_zh"}, "cantonese") == "v_zh"
    assert pregen_tts._resolve_voice_map({"cantonese": "v_canto"}, "zh") == ""
    assert pregen_tts._resolve_voice_map({"en": "v_en"}, "cantonese") == ""


def test_resolve_voice_map_json_string_same_as_dict():
    raw = '{"zh":"Chinese_wenrounvxing","cantonese":"Cantonese_crisp_news_anchor_vv2"}'
    assert pregen_tts._resolve_voice_map(raw, "zh") == "Chinese_wenrounvxing"
    assert pregen_tts._resolve_voice_map(raw, "cantonese") == "Cantonese_crisp_news_anchor_vv2"
    # en 缺键回落 zh。
    assert pregen_tts._resolve_voice_map(raw, "en") == "Chinese_wenrounvxing"


def test_resolve_voice_map_plain_id_passthrough():
    # 单音色 id 串(非 JSON):任何语言都解析到它(运行时 _resolve_voice 姿势)。
    assert pregen_tts._resolve_voice_map("Cantonese_GentleLady", "cantonese") == "Cantonese_GentleLady"
    assert pregen_tts._resolve_voice_map("Cantonese_GentleLady", "en") == "Cantonese_GentleLady"


def test_resolve_voice_map_empty_and_garbage():
    assert pregen_tts._resolve_voice_map("", "zh") == ""
    assert pregen_tts._resolve_voice_map(None, "zh") == ""
    # 坏 JSON 与运行时一致:原样返回(交给下游兜底,不吞成空串)。
    assert pregen_tts._resolve_voice_map("{not json", "zh") == "{not json"


def test_persona_resolved_voice_full_chain_same_source():
    # 全链:_parse_voice_map(JSON 串)→ single collapse(人设主语言)→ 解析。
    persona = {"id": "p1", "language": "cantonese", "reference_audio": '{"zh":"Vzh","cantonese":"Vcanto"}'}
    # single 模式(默认):collapse 成人设主语言(粤)一把声放 zh 键,任何语言都它。
    assert (
        pregen_tts._persona_resolved_voice(persona, "cantonese", {}, "single") == "Vcanto"
    )
    assert pregen_tts._persona_resolved_voice(persona, "zh", {}, "single") == "Vcanto"
    # per_language 模式:按请求语言取键,zh 请求拿 Vzh。
    assert pregen_tts._persona_resolved_voice(persona, "zh", {}, "per_language") == "Vzh"
    assert pregen_tts._persona_resolved_voice(persona, "en", {}, "per_language") == "Vzh"


# ---- _fillers_jobs:人设 × 本语言整池 ----

_MANIFEST = {
    "zh": [{"text": "好的，您稍等。", "file": "zh-01.wav"}, {"text": "嗯<#0.3#>让我看下。", "file": "zh-06.wav"}],
    "en": [{"text": "Sure, one moment.", "file": "en-01.wav"}],
}


def test_fillers_jobs_per_persona_own_language_pool():
    personas = [
        {"id": "pzh", "language": "zh", "reference_audio": '{"zh":"Vzh"}'},
        {"id": "pen", "language": "en", "reference_audio": '{"en":"Ven"}'},
    ]
    jobs = pregen_tts._fillers_jobs(personas, _MANIFEST, {}, "single")
    # 每人设只取其语言对应池;text 原样透传(含 <#x#> 停顿标记)。
    assert jobs == [
        (personas[0], "zh", "好的，您稍等。"),
        (personas[0], "zh", "嗯<#0.3#>让我看下。"),
        (personas[1], "en", "Sure, one moment."),
    ]


def test_fillers_jobs_skips_persona_without_voice_for_its_lang(capsys):
    # per_language 模式下 en 人设只有 cantonese 键 → en 解析为空 → 跳过(响亮打印)。
    personas = [{"id": "pbad", "language": "en", "reference_audio": '{"cantonese":"Vcanto"}'}]
    jobs = pregen_tts._fillers_jobs(personas, _MANIFEST, {}, "per_language")
    assert jobs == []
    out = capsys.readouterr().out
    assert "persona=pbad" in out and "skipped" in out


def test_fillers_jobs_skips_lang_without_pool(capsys):
    personas = [{"id": "pcanto", "language": "cantonese", "reference_audio": '{"zh":"Vcanto"}'}]
    jobs = pregen_tts._fillers_jobs(personas, _MANIFEST, {}, "single")
    assert jobs == []  # manifest 无 cantonese 池
    out = capsys.readouterr().out
    assert "persona=pcanto" in out and "skipped" in out


# ---- _qa_jobs:默认每语言一人设 / --all-personas 全人设覆盖 ----

_QA_ROWS = [
    {"answer_text": "我们九点上班。", "lang": "zh", "enabled": True},
    {"answer_text": "我哋九點開工。", "lang": "cantonese", "enabled": True},
    {"answer_text": "", "lang": "zh", "enabled": True},  # 空应答跳过
    {"answer_text": "已停用", "lang": "zh", "enabled": False},  # 停用跳过
]


def test_qa_jobs_default_one_persona_per_lang():
    lang_personas = {
        "zh": {"id": "pzh", "language": "zh", "reference_audio": '{"zh":"Vzh"}'},
        "cantonese": {"id": "pcanto", "language": "cantonese", "reference_audio": '{"zh":"Vcanto"}'},
        "en": None,
    }
    jobs = pregen_tts._qa_jobs(
        _QA_ROWS, [], lang_personas, all_personas=False, tts_cfg={}, voice_mode="single"
    )
    assert jobs == [
        (lang_personas["zh"], "zh", "我们九点上班。"),
        (lang_personas["cantonese"], "cantonese", "我哋九點開工。"),
    ]


def test_qa_jobs_all_personas_covers_every_persona_with_voice():
    personas = [
        {"id": "pzh", "language": "zh", "reference_audio": '{"zh":"Vzh"}'},
        {"id": "pcanto", "language": "cantonese", "reference_audio": '{"cantonese":"Vcanto"}'},
    ]
    jobs = pregen_tts._qa_jobs(
        _QA_ROWS, personas, {}, all_personas=True, tts_cfg={}, voice_mode="per_language"
    )
    # zh 条目:pzh zh 键直取;pcanto 只有 cantonese 键、zh 请求解析为空 → 跳过
    # (「音色匹配条目语言」门:运行时音色为空同样不查缓存,物化无意义)。
    # cantonese 条目:pcanto 直取;pzh 缺 cantonese 键回落 zh 键=运行时同款回落
    # (运行时同人设同语言解析出同一音色,该组合物化后照样命中)。
    assert jobs == [
        (personas[0], "zh", "我们九点上班。"),
        (personas[0], "cantonese", "我哋九點開工。"),
        (personas[1], "cantonese", "我哋九點開工。"),
    ]


# ---- _print_group_counts:聚合计数线(dry-run 计划清单/真跑计数同口径) ----


def test_print_group_counts_real_run_format(capsys):
    records: list[pregen_tts.Record] = [
        ({"id": "p9"}, "en", "vE", "new"),
        ({"id": "p9"}, "en", "vE", "new"),
        ({"id": "p9"}, "en", "vE", "skip"),
    ]
    pregen_tts._print_group_counts("fillers", records)
    assert capsys.readouterr().out.strip() == "fillers: persona=p9 lang=en voice=vE new=2 skip=1"


def test_print_group_counts_dry_run_includes_total(capsys):
    records: list[pregen_tts.Record] = [
        ({"id": "p1"}, "zh", "v1", "new"),
        ({"id": "p1"}, "zh", "v1", "skip"),
        ({"id": "p1"}, "zh", "v2", "new"),
        (None, "zh", "v3", "new"),
    ]
    pregen_tts._print_group_counts("fillers", records, with_total=True)
    out = capsys.readouterr().out.splitlines()
    assert "fillers: persona=p1 lang=zh voice=v1 total=2 new=1 skip=1" in out
    assert "fillers: persona=p1 lang=zh voice=v2 total=1 new=1 skip=0" in out
    assert "fillers: persona=- lang=zh voice=v3 total=1 new=1 skip=0" in out
