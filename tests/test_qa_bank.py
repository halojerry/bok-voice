"""qa_bank.py(种子包导入/导出 CLI)离线单测(2026-09-25)。

只测无网络部分:JSONL 解析/序列化 round-trip、幂等比对(归一单点=
packages/core normalize_question;同问法同语言跳过、不同语言不误跳)、
dry-run 计数、坏行宽容跳过、簇两段式重映射/悬空降级、假客户端顶替的
import 主流程与退出码、鉴权 401/403 人话报错退出码 2。
夹具一律 canto 前缀;零密钥样字面量。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

import qa_bank as qb  # noqa: E402


# ---- 夹具(canto 前缀) ----


def _entry(eid: str, question: str, answer: str, lang: str = "cantonese", **extra) -> dict:
    row = {
        "id": eid,
        "account_id": "canto-seed",
        "owner_user_id": "",
        "question_text": question,
        "answer_text": answer,
        "lang": lang,
        "scope": "global",
        "step_index": -1,
        "voice_id": "",
        "enabled": True,
        "hit_count": 0,
        "source": "curated",
        "cluster_head_id": "",
        "priority": 10,
        "template_id": "",
        "created_at": "2026-09-25T00:00:00",
    }
    row.update(extra)
    return row


class FakeCpClient:
    """duck-typing 假 CP(qb.CpClient 的方法面);fail_on=按问法文本制造 POST 失败。"""

    def __init__(self, entries=None, fail_on=(), auth_error=None):
        self.entries = list(entries or [])
        self.fail_on = set(fail_on)
        self.auth_error = auth_error
        self.created: list[dict] = []
        self.pregen_calls: list[list[str]] = []
        self.status_calls: list[str] = []

    def list_qa_entries(self, account_id: str) -> list:
        if self.auth_error is not None:
            raise self.auth_error
        return [dict(e) for e in self.entries]

    def create_qa_entry(self, payload: dict) -> dict:
        if payload.get("question_text") in self.fail_on:
            raise qb.CpError("boom:模拟写入失败")
        row = dict(payload)
        row["id"] = f"qa:canto-new{len(self.created) + 1}"
        self.created.append(row)
        return dict(row)

    def qa_pregen(self, entry_ids: list[str]) -> dict:
        self.pregen_calls.append([str(x) for x in entry_ids])
        return {"status": "queued", "pid": 424242}

    def qa_canned_status(self, account_id: str) -> dict:
        self.status_calls.append(account_id)
        return {"available": True, "statuses": {}, "generated_at": 1727200000}


def _import_args(file_path: str, **kw) -> argparse.Namespace:
    defaults = dict(account="canto-seed", file=file_path, dry_run=False, owner_user_id="", pregen=False)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ---- JSONL 行解析/序列化 round-trip ----


def test_seed_row_roundtrip_via_jsonl_line():
    entry = _entry(
        "qa:canto-1",
        "幾時送到？",
        "今天之內送到。",
        lang="cantonese",
        scope="step",
        step_index=2,
        priority=5,
        enabled=False,
        cluster_head_id="qa:canto-head",
    )
    line = json.dumps(qb.seed_row_from_entry(entry), ensure_ascii=False)
    row, reason = qb.parse_seed_line(line)
    assert reason == ""
    assert row == qb.seed_row_from_entry(entry)
    # 字段序=契约序(src_id 在前),便于人读 diff。
    assert list(json.loads(line).keys()) == list(qb.SEED_FIELDS)


def test_parse_seed_line_defaults_and_lang_normalization():
    row, reason = qb.parse_seed_line(json.dumps({"question_text": "多久到", "answer_text": "两天"}))
    assert reason == ""
    assert row["lang"] == "zh" and row["scope"] == "global" and row["step_index"] == -1
    assert row["priority"] == 10 and row["enabled"] is True and row["cluster_head_id"] == ""
    row2, _ = qb.parse_seed_line(json.dumps({"question_text": "几时到", "answer_text": "听日", "lang": "CANTONESE"}))
    assert row2["lang"] == "cantonese"


def test_parse_seed_line_bad_lines_toleration():
    cases = {
        "not json at all {": "bad_json",
        json.dumps([1, 2]): "not_object",
        json.dumps({"answer_text": "有"}): "missing_question",
        json.dumps({"question_text": "问", "answer_text": ""}): "missing_answer",
        json.dumps({"question_text": "q", "answer_text": "a", "lang": "jp"}): "bad_lang",
        json.dumps({"question_text": "q", "answer_text": "a", "lang": "zh", "scope": "weird"}): "bad_scope",
        json.dumps({"question_text": "q", "answer_text": "a", "priority": "很多"}): "bad_priority",
        json.dumps({"question_text": "q", "answer_text": "a", "enabled": "也许"}): "bad_enabled",
        json.dumps({"question_text": "q", "answer_text": "a", "step_index": "第3步"}): "bad_step_index",
    }
    for raw, expected in cases.items():
        row, reason = qb.parse_seed_line(raw)
        assert row is None and reason == expected, (raw, reason)


def test_parse_seed_line_priority_clamped_and_enabled_coercion():
    row, _ = qb.parse_seed_line(json.dumps({"question_text": "q", "answer_text": "a", "priority": 9999, "enabled": "true"}))
    assert row["priority"] == 1000 and row["enabled"] is True
    row2, _ = qb.parse_seed_line(json.dumps({"question_text": "q", "answer_text": "a", "enabled": 0}))
    assert row2["enabled"] is False


# ---- 幂等比对(归一单点 normalize_question) ----


def test_entry_key_same_normalized_question_diff_punctuation():
    # 归一只剥标点/符号/空白,语尾词(啊)是文字、照旧保留——变体只做宽度/标点差。
    k1 = qb.entry_key("幾時送到?", "cantonese")
    k2 = qb.entry_key("　幾時送到？ ", "cantonese")
    k3 = qb.entry_key("幾時送到,啊。", "en")
    assert k1 == k2
    assert k1 != k3  # 语言不同=不同键(幂等不误跳跨语言)
    assert k1 == qb.entry_key("幾時送到", "cantonese")
    assert qb.entry_key("幾時送到", "") != k1


def test_plan_import_skips_same_lang_but_not_cross_lang():
    existing = [_entry("qa:canto-old", "幾時送到", "聽日到", lang="cantonese")]
    rows = [
        dict(qb.parse_seed_line(json.dumps({"src_id": "s1", "question_text": "幾時送到?", "answer_text": "聽日到", "lang": "cantonese"}))[0]),
        dict(qb.parse_seed_line(json.dumps({"src_id": "s2", "question_text": "幾時送到", "answer_text": "明天到", "lang": "zh"}))[0]),
    ]
    plan = qb.plan_import(rows, existing)
    # 同问法同语言 → 跳过;同问法不同语言 → 两个键,不误跳。
    assert [s["reason"] for s in plan["skipped"]] == ["duplicate"]
    assert len(plan["creates"]) == 1
    assert plan["creates"][0]["row"]["lang"] == "zh"


def test_plan_import_inbatch_duplicate_second_skipped():
    rows = []
    for src in ("s1", "s2"):
        rows.append(
            qb.parse_seed_line(json.dumps({"src_id": src, "question_text": "可以退貨嗎", "answer_text": "可以", "lang": "cantonese"}))[0]
        )
    plan = qb.plan_import(rows, [])
    assert len(plan["creates"]) == 1 and len(plan["skipped"]) == 1
    assert plan["skipped"][0]["reason"] == "duplicate" and plan["skipped"][0]["line_no"] == 2


# ---- 簇两段式重映射/悬空降级(跨环境迁移不断链) ----


def test_plan_import_variant_before_head_orders_two_phase():
    # 文件序:变体在前、head 在后 → 执行序仍是 phase1(head)先行。
    variant = json.dumps({"src_id": "s-var", "question_text": "運費幾多", "answer_text": "首重另計", "lang": "cantonese", "cluster_head_id": "s-head"})
    head = json.dumps({"src_id": "s-head", "question_text": "運費怎麼算", "answer_text": "按重量計", "lang": "zh"})
    rows = [qb.parse_seed_line(variant)[0], qb.parse_seed_line(head)[0]]
    plan = qb.plan_import(rows, [])
    assert [c["phase"] for c in plan["creates"]] == [1, 2]
    assert plan["creates"][0]["row"]["src_id"] == "s-head"
    assert plan["creates"][1]["head_src_id"] == "s-head"
    assert plan["creates"][1]["head_key"] == qb.entry_key("運費怎麼算", "zh")


def test_plan_import_dangling_head_stripped_to_standalone():
    row = qb.parse_seed_line(
        json.dumps({"src_id": "s1", "question_text": "點退件", "answer_text": "門市交收", "lang": "cantonese", "cluster_head_id": "qa:ghost"})
    )[0]
    plan = qb.plan_import([row], [])
    assert len(plan["creates"]) == 1
    assert plan["creates"][0]["row"]["cluster_head_id"] == ""  # 绝不写断链引用
    assert plan["dangling"] == [{"line_no": 1, "head_src_id": "qa:ghost"}]


def test_run_import_remaps_cluster_head_to_new_env_id():
    seed = (
        json.dumps({"src_id": "s-head", "question_text": "運費怎麼算", "answer_text": "按重量計", "lang": "zh"}) + "\n"
        + json.dumps({"src_id": "s-var", "question_text": "運費幾多", "answer_text": "按重量計", "lang": "cantonese", "cluster_head_id": "s-head"})
        + "\n"
    )
    client = FakeCpClient(entries=[_entry("qa:canto-existing", "舊問題", "舊答案")])
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write(seed)
        path = f.name
    try:
        rc = qb.cmd_import(client, _import_args(path))
        assert rc == 0
        assert len(client.created) == 2
        head_row, var_row = client.created[0], client.created[1]
        assert head_row["cluster_head_id"] == ""  # head 先建
        # 变体重映射到本环境新 id,不断链。
        assert var_row["cluster_head_id"] == head_row["id"]
        assert var_row["account_id"] == "canto-seed" and var_row["owner_user_id"] == ""
    finally:
        Path(path).unlink(missing_ok=True)


def test_run_import_idempotent_rerun_all_skipped():
    line = json.dumps({"src_id": "s1", "question_text": "可以退貨嗎", "answer_text": "可以,七日內。", "lang": "cantonese"})
    client = FakeCpClient(entries=[_entry("qa:canto-have", "可以退貨嗎？", "可以,七日內。", lang="cantonese")])
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write(line + "\n")
        path = f.name
    try:
        rc = qb.cmd_import(client, _import_args(path))
        assert rc == 0
        assert client.created == []  # 归一后同问法同语言 → 全跳过
    finally:
        Path(path).unlink(missing_ok=True)


# ---- dry-run 零写入 + 计数 ----


def test_dry_run_counts_and_zero_writes(capsys):
    lines = [
        json.dumps({"src_id": "s1", "question_text": "幾時送到", "answer_text": "聽日", "lang": "cantonese"}),
        json.dumps({"src_id": "s2", "question_text": "幾時送到", "answer_text": "聽日", "lang": "cantonese"}),  # 批内重复
        json.dumps({"src_id": "s3", "question_text": "壞行", "answer_text": "", "lang": "cantonese"}),  # missing_answer
        "garbage line",
    ]
    client = FakeCpClient()
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        path = f.name
    try:
        rc = qb.cmd_import(client, _import_args(path, dry_run=True))
        assert rc == 0
        assert client.created == [] and client.pregen_calls == []  # 零写入
        err = capsys.readouterr().err
        assert "将导入 1 跳过 3" in err
        assert "duplicate=1" in err and "missing_answer=1" in err and "bad_json=1" in err
    finally:
        Path(path).unlink(missing_ok=True)


# ---- 主流程:成功/跳过/失败计数与退出码(--pregen) ----


def test_run_import_counts_exit_codes_and_pregen_ids(capsys):
    lines = [
        json.dumps({"src_id": "s1", "question_text": "問一", "answer_text": "答一", "lang": "zh"}),
        json.dumps({"src_id": "s2", "question_text": "爆的問法", "answer_text": "答二", "lang": "zh"}),  # 制造 POST 失败
        json.dumps({"src_id": "s3", "question_text": "問三", "answer_text": "答三", "lang": "zh"}),
    ]
    client = FakeCpClient(entries=[_entry("qa:canto-have3", "問三", "已有答案", lang="zh")], fail_on={"爆的問法"})
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        path = f.name
    try:
        args = _import_args(path, pregen=True)
        rc = qb.cmd_import(client, args)
        assert rc == 1  # 有失败行 → 1
        assert len(client.created) == 1  # 問一建成;問三与库内同键跳过;爆的問法失败
        assert client.pregen_calls == [[client.created[0]["id"]]]  # 只物化新建 id
        err = capsys.readouterr().err
        assert "新建 1 跳过 1 失败 1" in err
        assert "罐头物化已排队" in err

        # 全成功 → 0
        client2 = FakeCpClient()
        rc2 = qb.cmd_import(client2, _import_args(path))
        assert rc2 == 0 and len(client2.created) == 3
    finally:
        Path(path).unlink(missing_ok=True)


def test_run_import_missing_file_exit_1(capsys):
    rc = qb.cmd_import(FakeCpClient(), _import_args("/nonexistent/canto-seed.jsonl"))
    assert rc == 1
    assert "读不到种子文件" in capsys.readouterr().err


def test_main_import_flow_with_fake_client(monkeypatch, tmp_path):
    seed = tmp_path / "canto-seed.jsonl"
    seed.write_text(
        json.dumps({"src_id": "s1", "question_text": "幾時送到", "answer_text": "聽日到", "lang": "cantonese"}) + "\n",
        encoding="utf-8",
    )
    holder = FakeCpClient()
    monkeypatch.setattr(qb, "CpClient", lambda base, headers=None: holder)
    rc = qb.main(["import", "--account", "canto-seed", "--file", str(seed)])
    assert rc == 0
    assert len(holder.created) == 1
    assert holder.created[0]["lang"] == "cantonese"


def test_main_auth_error_exit_2_messages(monkeypatch, capsys):
    for token_set, hint in ((False, "BOK_CP_TOKEN 未设置"), (True, "BOK_CP_TOKEN 无效")):
        holder = FakeCpClient(auth_error=qb.AuthError(401, token_set=token_set))
        monkeypatch.setattr(qb, "CpClient", lambda base, headers=None: holder)
        rc = qb.main(["status", "--account", "canto-seed"])
        assert rc == 2
        err = capsys.readouterr().err
        assert hint in err and "401" in err


def test_cp_headers_and_base_url_from_env(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://127.0.0.1:8010/")
    monkeypatch.setenv("BOK_CP_TOKEN", "dummy-machine-token")
    assert qb.cp_base_url() == "http://127.0.0.1:8010"  # 尾斜杠剥掉
    assert qb.cp_headers() == {"Authorization": "Bearer dummy-machine-token"}
    monkeypatch.delenv("BOK_CP_TOKEN")
    assert qb.cp_headers() == {}  # token 缺失=不带头,401/403 时由 AuthError 报人话


# ---- 导出选行 + 簇断链防护 ----


def test_select_export_rows_shared_pool_and_include_shared():
    entries = [
        _entry("qa:canto-shared", "共享問", "共享答"),
        _entry("qa:canto-personal", "个人問", "个人答", owner_user_id="user-canto-9"),
    ]
    only_shared, _ = qb.select_export_rows(entries)
    assert [e["id"] for e in only_shared] == ["qa:canto-shared"]
    everything, _ = qb.select_export_rows(entries, include_shared=True)
    assert {e["id"] for e in everything} == {"qa:canto-shared", "qa:canto-personal"}


def test_select_export_rows_auto_includes_filtered_cluster_head():
    # head 是 zh、变体是 cantonese:--lang cantonese 时 head 被自动补带,簇不断链。
    entries = [
        _entry("qa:canto-head-zh", "運費怎麼算", "按重量計", lang="zh"),
        _entry("qa:canto-var", "運費幾多", "按重量計", lang="cantonese", cluster_head_id="qa:canto-head-zh"),
        _entry("qa:canto-other", "其他問", "其他答", lang="zh"),  # zh 条目被语言过滤排除
    ]
    chosen, auto_heads = qb.select_export_rows(entries, lang="cantonese")
    ids = [e["id"] for e in chosen]
    assert auto_heads == 1
    assert "qa:canto-head-zh" in ids and "qa:canto-var" in ids
    assert "qa:canto-other" not in ids


def test_cmd_export_writes_jsonl_and_reimports_clean(tmp_path, capsys):
    entries = [
        _entry("qa:canto-h", "幾時送到", "聽日到", lang="cantonese"),
        _entry("qa:canto-v", "幾時到貨", "聽日到", lang="cantonese", cluster_head_id="qa:canto-h"),
    ]
    client = FakeCpClient(entries=entries)
    out = tmp_path / "canto-out.jsonl"
    args = argparse.Namespace(account="canto-seed", out=str(out), lang="", include_shared=False)
    rc = qb.cmd_export(client, args)
    assert rc == 0
    written = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert {w["src_id"] for w in written} == {"qa:canto-h", "qa:canto-v"}
    # 导出包喂回 plan_import(空库) → 全部可导入,变体 head_key 指向包内 head。
    plan = qb.plan_import([qb.parse_seed_line(l)[0] for l in out.read_text(encoding="utf-8").splitlines()], [])
    assert len(plan["creates"]) == 2 and plan["dangling"] == []
    assert {c["phase"] for c in plan["creates"]} == {1, 2}
    assert "导出 2 条" in capsys.readouterr().err
