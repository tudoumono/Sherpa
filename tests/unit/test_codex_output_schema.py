"""Codex `--output-schema`（構造化最終応答での完了判定）の契約。

継続判定・見出し選択は構造化応答（`_parse_structured`/`_parse_structured_v2` で検証したもの）だけを
根拠にし、不正な最終出力は「未完了」として扱う。偽 codex は test_codex_auto_continue.py の多段階版を
`helper` として再利用する（出力スキーマは既定 ON のまま呼ぶ）。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import test_codex_auto_continue as helper  # noqa: E402

from sherpa import agents as A  # noqa: E402
from sherpa import codex_agents_md  # noqa: E402
from sherpa.env_int import env_int  # noqa: E402
from sherpa import investigation_state as INV  # noqa: E402
from sherpa.providers.codex import continuation as CONT  # noqa: E402
from sherpa.providers.codex import structured as STRUCT  # noqa: E402
from sherpa.providers.codex import turn_consts as TC  # noqa: E402

_FIXED_NO_ANSWER = "回答を取り出せませんでした。もう一度お試しください。"
_TRUNCATED = '{"status":"in_progress","answer":"これから資料を確認します。","next_step":'


def _sj(status: str, answer: str, next_step: str | None = None) -> str:
    return json.dumps({"status": status, "answer": answer, "next_step": next_step}, ensure_ascii=False)


def _sj2(status: str, answer: str, claims: list, next_step: str | None = None) -> str:
    return json.dumps({"status": status, "answer": answer, "next_step": next_step, "claims": claims},
                      ensure_ascii=False)


def _setup(tmp_path: Path, monkeypatch, steps: list, users_dirname: str,
          schema_env: str | None = None) -> Path:
    """`helper._setup` と同じ偽 codex だが出力スキーマは既定 ON のまま（`schema_env` で明示上書き）。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"steps": steps}), encoding="utf-8")
    helper._write_fake_codex(bin_dir, argv_log, plan_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    if schema_env is not None:
        monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", schema_env)
    return argv_log


def _go(tmp_path, monkeypatch, steps, uid, cid, *, schema_env=None, env=None, prov=None, ctx=None, caplog=None):
    """1 ターン実行して (env, calls) を返す。"""
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname=f"users_{uid}", schema_env=schema_env)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    events = helper._run(prov or A.CodexProvider(), ctx or helper._ctx(uid=uid, conversation_id=cid))
    return helper._result_env(events), helper._read_argv_log(argv_log)


# 台帳ゲートが manifest 無しを検知して 1 回だけ催促するため、final 受理の呼び出し回数は +1 になる。

# ===== 構造化応答での完了判定 =====

def test_in_progress_then_final_concludes_with_ledger_nudge(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-SCHEMA-1",
         "agent_messages": [_sj("in_progress", "まず資料を確認します。", "税率マスタを読む")],
         "usage": helper._usage()},
        {"thread_id": "TH-SCHEMA-1", "agent_messages": [_sj("final", "確認した結果、影響はありません。")],
         "usage": helper._usage(30, 2, 13, 3)},
    ]
    env, calls = _go(tmp_path, monkeypatch, steps, "schema-final", 20001)
    assert env["headline"] == "確認した結果、影響はありません。"
    assert not env.get("codex_stopped_early")
    assert len(calls) == 3   # in_progress → final → 台帳催促 1 回


@pytest.mark.parametrize("label,step", [
    ("declarative_wording", {"agent_messages": [_sj("final", "まず確認します。次に調べます。")]}),
    ("last_message_only", {"last_message": _sj("final", "まず確認します。次に調べます。")}),
    ("plain_commentary_plus_final", {"agent_messages": ["資料を確認しています…",
                                                        _sj("final", "まず確認します。次に調べます。")]}),
    ("last_message_wins_over_stream_tail", {
        "agent_messages": [_sj("in_progress", "まだ調べています。", "続きを見る")],
        "last_message": _sj("final", "まず確認します。次に調べます。")}),
])
def test_status_final_is_accepted_without_heuristic_continuation(tmp_path, monkeypatch, label, step):
    """status=final は宣言調の文面でも継続しない。-o のみ・平文混在・ストリーム末尾との食い違い（-o を採る）でも同じ。"""
    steps = [{"thread_id": f"TH-{label}", "usage": helper._usage(), **step}]
    env, calls = _go(tmp_path, monkeypatch, steps, f"schema-{label}", 20002)
    assert env["headline"] == "まず確認します。次に調べます。"
    assert not env.get("codex_stopped_early")
    assert len(calls) == 2


# ===== 無効化（env 0 / Codex(Ollama)） =====

def test_env_disabled_skips_output_schema_and_uses_heuristic(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-SCHEMA-OFF", "agent_messages": ["まず資料を確認します。"], "usage": helper._usage()},
        {"thread_id": "TH-SCHEMA-OFF", "agent_messages": ["資料を確認した結果、影響はありません。"],
         "usage": helper._usage(30, 2, 13, 3)},
    ]
    env, calls = _go(tmp_path, monkeypatch, steps, "schema-env-off", 20004, schema_env="0")
    assert env["headline"] == "資料を確認した結果、影響はありません。"
    assert len(calls) == 2
    assert not any("--output-schema" in c for c in calls)
    assert calls[1][-1] == CONT._CONTINUE_PROMPT
    assert "status" not in calls[1][-1]


def test_ollama_construct_skips_output_schema(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-SCHEMA-OLLAMA", "agent_messages": ["確認した結果、対象はありません。"],
              "usage": helper._usage()}]
    env, calls = _go(tmp_path, monkeypatch, steps, "schema-ollama", None,
                     prov=A.CodexProvider(ollama_base_url="http://localhost:11434"))
    assert env["headline"] == "確認した結果、対象はありません。"
    assert len(calls) == 1
    assert "--output-schema" not in calls[0]


# ===== 不正な最終出力は完了扱いにしない =====

_BROKEN_PAYLOADS = [
    ("truncated", _TRUNCATED),
    ("unknown_status", json.dumps({"status": "done", "answer": "終わりました。", "next_step": None})),
    ("missing_next_step", json.dumps({"status": "final", "answer": "終わりました。"})),
    ("wrong_type_answer", json.dumps({"status": "final", "answer": 123, "next_step": None})),
    ("extra_key", json.dumps({"status": "final", "answer": "終わりました。", "next_step": None, "extra": "x"})),
]


@pytest.mark.parametrize("label,payload", _BROKEN_PAYLOADS)
def test_invalid_final_output_is_not_treated_as_complete(tmp_path, monkeypatch, label, payload):
    steps = [{"thread_id": f"TH-SCHEMA-BAD-{label}", "agent_messages": [payload], "usage": helper._usage()}]
    env, calls = _go(tmp_path, monkeypatch, steps, f"schema-bad-{label}", 20100,
                     env={"SHERPA_CODEX_AUTO_CONTINUE": "0"})
    assert env.get("codex_stopped_early") is True
    assert '"status"' not in env["headline"] and "{" not in env["headline"]
    if label == "wrong_type_answer":
        assert env["body"] == _FIXED_NO_ANSWER
    else:
        # 読み取れた answer の本文は固定文言に置き換えず、途中で切れた可能性の注記つきで返す。
        _body = "これから資料を確認します。" if label == "truncated" else "終わりました。"
        assert env["body"] == _body and "回答が途中で切れた可能性があります。" in env["headline"]
    assert len(calls) == 1


def test_invalid_final_output_triggers_continuation_when_limit_allows(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-SCHEMA-BAD-CONT", "agent_messages": [_TRUNCATED], "usage": helper._usage()}] * 2
    env, calls = _go(tmp_path, monkeypatch, steps, "schema-bad-cont", 20101,
                     env={"SHERPA_CODEX_AUTO_CONTINUE": "1"})
    assert len(calls) == 2
    assert env.get("codex_stopped_early") is True
    assert '"status"' not in env["headline"]


def test_valid_final_earlier_in_same_attempt_is_not_lost_by_later_broken_message(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-SCHEMA-KEEP-FINAL",
              "agent_messages": [_sj("final", "先に得た結論です。"), _TRUNCATED], "usage": helper._usage()}]
    env, calls = _go(tmp_path, monkeypatch, steps, "schema-keep-final", 20600,
                     env={"SHERPA_CODEX_AUTO_CONTINUE": "0"})
    assert env["body"] == "先に得た結論です。"
    assert env.get("codex_stopped_early") is True
    assert len(calls) == 2   # 確定候補にも台帳催促が 1 回入る


def test_short_final_after_notification_does_not_replace_answer_with_sources(tmp_path, monkeypatch):
    """出典付きの final の後に、完了通知への短い final が届いても見出しは出典付きの回答にする。"""
    steps = [{"thread_id": "TH-SCHEMA-FOLLOWUP", "usage": helper._usage(), "agent_messages": [
        _sj("final", "区分は 1〜7 です。\n\n参照した資料:\n- a/b.doc"),
        _sj("final", "追加通知の内容は結論と整合していました。先ほどの回答内容は変更ありません。")]}]
    env, _ = _go(tmp_path, monkeypatch, steps, "schema-followup", 20650,
                 env={"SHERPA_CODEX_AUTO_CONTINUE": "0"})
    assert "区分は 1〜7 です。" in env["headline"]
    assert "追加通知" not in env["headline"]


def test_empty_final_answer_falls_to_fixed_message_not_dispatch(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-EMPTY", "agent_messages": [_sj("final", "")], "usage": helper._usage()}]
    env, calls = _go(tmp_path, monkeypatch, steps, "schema-empty-final", 930, schema_env="1")
    assert len(calls) == 1
    assert env["headline"] == _FIXED_NO_ANSWER


# ===== argv・スキーマファイル =====

@pytest.mark.parametrize("schema_env,suffix,n_calls", [
    (None, "output_schema_v2.json", 2),
    ("1", "output_schema.json", 1),
])
def test_argv_includes_output_schema_flag(tmp_path, monkeypatch, schema_env, suffix, n_calls):
    """env 未設定は v2、`1` は v1 のスキーマファイル（絶対パス）を付ける。"""
    steps = [{"thread_id": "TH-SCHEMA-ARGV", "agent_messages": [_sj("final", "対象はありません。")],
              "usage": helper._usage()}]
    _, calls = _go(tmp_path, monkeypatch, steps, f"schema-argv-{schema_env}", 20300, schema_env=schema_env)
    assert len(calls) == n_calls
    argv = calls[0]
    schema_path = argv[argv.index("--output-schema") + 1]
    assert schema_path.endswith(suffix)
    assert Path(schema_path).is_absolute() and Path(schema_path).is_file()


def test_output_schema_file_matches_contract():
    schema = json.loads(TC._OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"status", "answer", "next_step"}
    assert schema["properties"]["status"]["enum"] == ["final", "in_progress"]
    assert schema["properties"]["next_step"]["type"] == ["string", "null"]


def test_output_schema_v2_claim_requires_eight_keys_including_item_ids():
    schema = json.loads(TC._OUTPUT_SCHEMA_PATH_V2.read_text(encoding="utf-8"))
    claim_schema = schema["properties"]["claims"]["items"]
    assert set(claim_schema["required"]) == STRUCT._CLAIM_KEYS
    assert claim_schema["properties"]["evidence_kinds"]["items"]["enum"] == list(INV.EVIDENCE_KINDS)
    assert "item_ids" in claim_schema["required"]


def test_parse_claim_accepts_item_ids_and_keeps_seven_key_form_without_them():
    base = {"id": "c1", "status": "inferred", "text": "t", "evidence_refs": [], "reason": "r",
            "reason_code": "", "evidence_kinds": ["source"]}
    assert STRUCT._parse_claim({**base, "item_ids": ["i1"]})["item_ids"] == ["i1"]
    assert "item_ids" not in STRUCT._parse_claim(base)
    assert STRUCT._parse_claim({**base, "item_ids": "i1"}) is None


def test_env_default_still_resolves_to_2_matching_provider_default(monkeypatch):
    monkeypatch.delenv("SHERPA_CODEX_OUTPUT_SCHEMA", raising=False)
    assert env_int("SHERPA_CODEX_OUTPUT_SCHEMA", 2, 0, 2) == 2


# ===== turn.failed =====

_TURN_FAILED_EVENTS = [
    {"type": "turn.started"},
    {"type": "error", "message": "invalid_json_schema"},
    {"type": "turn.failed", "error": {"message": "invalid_json_schema"}},
]


def test_turn_failed_without_agent_message_is_explicit_failure(tmp_path, monkeypatch):
    steps = [{"exit_code": 1, "extra_events": _TURN_FAILED_EVENTS}]
    env, calls = _go(tmp_path, monkeypatch, steps, "schema-turnfailed", None,
                     ctx=helper._ctx(uid="schema-turnfailed", conversation_id=None,
                                     message="偽 codex turn.failed テスト"))
    assert env["headline"] != "dispatch-headline"
    assert "接続できません" in env["headline"] and "回答を返せずに終了しました" in env["headline"]
    assert not env.get("codex_stopped_early") and not env.get("codex_timed_out")
    assert len(calls) == 1


def test_continuation_turn_failed_without_new_message_is_explicit_failure(tmp_path, monkeypatch, caplog):
    """継続が無回答で失敗しても、前の回答と失敗の注記を返す。"""
    steps = [
        {"thread_id": "TH-SCHEMA-CONT-TURNFAILED",
         "agent_messages": [_sj("in_progress", "まず資料を確認します。", "続きを見る")],
         "usage": helper._usage()},
        {"exit_code": 1, "extra_events": _TURN_FAILED_EVENTS},
    ]
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        env, calls = _go(tmp_path, monkeypatch, steps, "schema-cont-turnfailed", 20800)
    assert "まず資料を確認します。" in env["headline"]
    assert "失敗" in env["headline"]
    assert env["completion"] == "partial"
    assert not env.get("codex_silent_failure")
    assert len(calls) == 2


# ===== 継続プロンプト・resume フォールバック =====

def test_continue_prompt_uses_schema_wording_when_schema_on(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-SCHEMA-CONT-PROMPT",
         "agent_messages": [_sj("in_progress", "まず資料を確認します。", "続きを見る")], "usage": helper._usage()},
        {"thread_id": "TH-SCHEMA-CONT-PROMPT", "agent_messages": [_sj("final", "確認しました。")],
         "usage": helper._usage()},
    ]
    _, calls = _go(tmp_path, monkeypatch, steps, "schema-cont-prompt", 20500)
    assert len(calls) == 3
    assert calls[1][-1] == CONT._CONTINUE_PROMPT_SCHEMA
    assert "status" in calls[1][-1] and "final" in calls[1][-1]


def test_resume_fallback_resets_structured_state(tmp_path, monkeypatch):
    """resume 失敗（-o にだけ古い final）→新規セッションにフォールバックしたとき、古い final を見出しに戻さない。"""
    steps = [
        {"last_message": _sj("final", "古いセッションの結論です。"), "exit_code": 1},
        {"thread_id": "TH-SCHEMA-RESET-FRESH",
         "agent_messages": [_sj("in_progress", "フレッシュ側の途中経過です。", "続きを見る")],
         "usage": helper._usage()},
    ]
    ctx = helper._ctx(uid="schema-resume-reset", conversation_id=20700, codex_session_id="SID-SCHEMA-STALE")
    env, calls = _go(tmp_path, monkeypatch, steps, "schema-resume-reset", 20700, ctx=ctx,
                     env={"SHERPA_CODEX_AUTO_CONTINUE": "0"})
    assert env["body"] == "フレッシュ側の途中経過です。"
    assert env.get("codex_stopped_early") is True
    assert len(calls) == 2
    assert "resume" in calls[0] and "SID-SCHEMA-STALE" in calls[0]
    assert "resume" not in calls[1]


# ===== `_parse_structured`（純関数） =====

@pytest.mark.parametrize("obj", [
    {"status": "final", "answer": "ok", "next_step": None},
    {"status": "in_progress", "answer": "調べます", "next_step": "続き"},
])
def test_parse_structured_accepts_well_formed(obj):
    assert STRUCT._parse_structured(json.dumps(obj)) == obj


@pytest.mark.parametrize("raw", [
    "", None, "これは JSON ではありません", '{"status":"final","answer":"x","next_step":',
    json.dumps(["final", "x", None]),
    json.dumps({"status": "done", "answer": "x", "next_step": None}),
    json.dumps({"status": "final", "answer": "x"}),
    json.dumps({"status": "final", "answer": "x", "next_step": None, "extra": 1}),
    json.dumps({"status": "final", "answer": 1, "next_step": None}),
    json.dumps({"status": "in_progress", "answer": "x", "next_step": 1}),
    json.dumps({"status": [], "answer": "x", "next_step": None}),
    json.dumps({"status": {}, "answer": "x", "next_step": None}),
])
def test_parse_structured_rejects_invalid(raw):
    assert STRUCT._parse_structured(raw) is None


# ===== v2（主張配列） =====

_V2_EMPTY_CLAIMS = {"status": "final", "answer": "回答", "next_step": None, "claims": [], "claims_invalid": 0}


def test_parse_structured_v2_reads_claims_array():
    payload = _sj2("final", "回答", [
        {"id": "c1", "status": "confirmed", "text": "t1", "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""},
        {"id": "c2", "status": "unknown", "text": "t2", "evidence_refs": [], "reason": "", "reason_code": "budget"}])
    parsed = STRUCT._parse_structured_v2(payload)
    assert parsed["answer"] == "回答"
    assert len(parsed["claims"]) == 2
    assert parsed["claims"][1]["reason_code"] == "budget"


def test_parse_structured_v2_backward_compatible_with_v1_three_keys():
    payload = json.dumps({"status": "final", "answer": "回答", "next_step": None})
    assert STRUCT._parse_structured_v2(payload) == _V2_EMPTY_CLAIMS


def test_parse_structured_v2_rejects_truncated_json():
    assert STRUCT._parse_structured_v2('{"status":"final","answer":"回答","next_step":null,"claims":[{"id":"c1"') is None


@pytest.mark.parametrize("bad_claim", [
    {"id": "c1", "status": "unknown", "text": "t", "evidence_refs": [], "reason": "", "reason_code": "not_a_real_code"},
    {"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": [], "reason": "", "reason_code": "", "extra": 1},
    {"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": [], "reason": "", "reason_code": ""},
])
def test_parse_structured_v2_drops_only_invalid_claims(bad_claim):
    """不正な主張要素だけを除き、正しい要素と status/answer は保持する。除いた件数を `claims_invalid` に残す。"""
    good = {"id": "c2", "status": "unknown", "text": "t2", "evidence_refs": [], "reason": "", "reason_code": "budget"}
    parsed = STRUCT._parse_structured_v2(_sj2("final", "回答", [bad_claim, good]))
    assert parsed["answer"] == "回答"
    assert [c["id"] for c in parsed["claims"]] == ["c2"]
    assert parsed["claims_invalid"] == 1


def test_env_2_selects_v2_schema_file_and_surfaces_claims(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-SCHEMA-V2",
              "agent_messages": [_sj2("final", "確認した結果、標準税率は10%です。", [
                  {"id": "c1", "status": "confirmed", "text": "標準税率は10%。",
                   "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""},
                  {"id": "c2", "status": "unknown", "text": "軽減税率の適用開始日は不明。",
                   "evidence_refs": [], "reason": "", "reason_code": "unexplored"}])],
              "usage": helper._usage()}]
    env, calls = _go(tmp_path, monkeypatch, steps, "schema-v2", 20700, schema_env="2")
    assert env["headline"] == "確認した結果、標準税率は10%です。"
    claims = env["data"]["claims"]
    assert {c["status"] for c in claims} == {"confirmed", "unknown"}
    assert next(c for c in claims if c["status"] == "unknown")["reason_code"] == "unexplored"
    argv = calls[0]
    assert argv[argv.index("--output-schema") + 1].endswith("output_schema_v2.json")


def test_env_2_invalid_claim_keeps_final_answer_without_extra_continuation(tmp_path, monkeypatch):
    """final の応答に不正な claim が混じっても answer は保持され、継続も固定文言化も起きない。"""
    steps = [{"thread_id": "TH-SCHEMA-V2-BADCLAIM",
              "agent_messages": [_sj2("final", "確認した結果、標準税率は10%です。", [
                  {"id": "c1", "status": "unknown", "text": "軽減税率の適用開始日は不明。",
                   "evidence_refs": [], "reason_code": "not_a_real_code"}])],
              "usage": helper._usage()}]
    env, calls = _go(tmp_path, monkeypatch, steps, "schema-v2-badclaim", 20701, schema_env="2")
    assert env["headline"].startswith(STRUCT._INVALID_CLAIMS_NOTE)
    assert env["headline"].endswith("確認した結果、標準税率は10%です。")
    assert not env.get("data", {}).get("claims")
    assert env["data"]["claims_invalid"] == 1
    assert len(calls) == 2   # 台帳催促の 1 回だけ


# ===== `_parse_claim` =====

def _item(**kw):
    base = {"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["src/A.cbl"],
            "reason": "", "reason_code": ""}
    return {**base, **kw}


@pytest.mark.parametrize("item", [
    _item(evidence_refs=[]),                                         # 裏付けゼロの確定
    _item(evidence_refs=[""]),                                       # 空白のみの根拠参照
    _item(evidence_kinds=["source", "not_a_real_kind"]),             # 閉語彙外
    _item(evidence_kinds="source"),                                  # 非リスト
    _item(status="inferred", evidence_refs=[], reason=""),           # 推定は理由必須
    _item(status="inferred", evidence_refs=[], reason="   "),
])
def test_parse_claim_rejects(item):
    assert STRUCT._parse_claim(item) is None


@pytest.mark.parametrize("item,expected_extra", [
    (_item(evidence_refs=["4期/01_標準/消費税法.md"]), {"evidence_kinds": None}),   # 6 キー形は未申告を補う
    (_item(evidence_kinds=["source", "callgraph"]), {}),
    (_item(status="inferred", evidence_refs=[], reason="根拠不十分", evidence_kinds=[]), {}),  # 空申告は未申告と区別
])
def test_parse_claim_accepts(item, expected_extra):
    assert STRUCT._parse_claim(item) == {**item, **expected_extra}


# ===== AGENTS.md の構造化応答の段落 =====

def _agents_md(tmp_path, **kw) -> str:
    d = tmp_path / "authoring"
    d.mkdir(exist_ok=True)
    codex_agents_md.write_agents_md(d, **kw)
    return (d / "AGENTS.md").read_text(encoding="utf-8")


def test_write_agents_md_structured_paragraphs(tmp_path):
    assert "next_step" not in _agents_md(tmp_path)
    on = _agents_md(tmp_path, output_schema=True)
    assert "next_step" in on and "`status`" in on

    v1 = _agents_md(tmp_path, output_schema=True, output_schema_v2=False)
    assert "claims" not in v1
    assert codex_agents_md._STATUS_FIELD_MEANING in v1

    v2 = _agents_md(tmp_path, output_schema=True, output_schema_v2=True)
    assert "claims" in v2 and "confirmed" in v2 and "inferred" in v2 and "unknown" in v2
    assert "not_found_in_scope" in v2
    assert codex_agents_md._STATUS_FIELD_MEANING in v2
    assert "final" in v2 and "in_progress" in v2 and "next_step" in v2 and "予算到達" in v2
    assert "item" in v2
    for tool in ("ripgrep_search", "es_search", "read_doc", "read_around", "file_head", "graph_neighbors"):
        assert tool in v2, tool

    off = _agents_md(tmp_path, output_schema=False, output_schema_v2=True)
    assert "claims" not in off and "next_step" not in off


@pytest.mark.parametrize("schema_env,expected_v2,msg", [
    ("2", True, _sj2("final", "確認しました。", [])),
    ("1", False, _sj("final", "確認しました。")),
    (None, True, _sj("final", "確認しました。")),
])
def test_run_passes_schema_version_to_agents_md(tmp_path, monkeypatch, schema_env, expected_v2, msg):
    """env 2／未設定は v2 段落、env 1 は v2 を渡さない（スキーマだけ v2 でも指示が 3 項目のままだと claims が空になる）。"""
    captured: dict = {}
    orig_write = codex_agents_md.write_agents_md

    def _spy(authoring, output_schema=False, direct_read=True, output_schema_v2=False, **kw):
        captured["output_schema_v2"] = output_schema_v2
        return orig_write(authoring, output_schema=output_schema, direct_read=direct_read,
                          output_schema_v2=output_schema_v2, **kw)

    monkeypatch.setattr(codex_agents_md, "write_agents_md", _spy)
    steps = [{"thread_id": "TH-SCHEMA-AGENTS", "agent_messages": [msg], "usage": helper._usage()}]
    _go(tmp_path, monkeypatch, steps, f"schema-agents-{schema_env}", 20703, schema_env=schema_env)
    assert captured.get("output_schema_v2") is expected_v2


# ===== `_apply_codex_evidence_gate`（Codex 経路の最終ゲート） =====
# 範囲判定は差し替えず、tmp の実コーパスを走査させる（偽にするのは Codex CLI 境界だけ）。

def _isolate_world_kb(monkeypatch, tmp_path: Path, world: str, files: dict) -> None:
    from sherpa import store
    from sherpa.providers import base as PB
    kb = tmp_path / "kb"
    wd = kb / world
    for rel, content in files.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    monkeypatch.setenv("SHERPA_KB_DIR", str(kb))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    for env in ("SHERPA_MCP_WORLD", "SHERPA_MCP_WORLD_ROOT"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(store, "get_world", lambda world_id: None)
    PB._scope_kinds_cache.clear()


_SRC_AND_SPEC = {"src/PROG1.cbl": "       IDENTIFICATION DIVISION.\n", "設計/仕様書.xlsx": b"dummy xlsx bytes"}
_SRC_ONLY = {"src/PROG1.cbl": "       IDENTIFICATION DIVISION.\n"}


def _claim(cid, status, kinds, refs=("ev-1",), reason="", reason_code="") -> dict:
    return {"id": cid, "status": status, "text": "t", "evidence_refs": list(refs),
            "reason": reason, "reason_code": reason_code, "evidence_kinds": kinds}


def _gate(monkeypatch, tmp_path, world, files, claims, lens="qa", personal_facts=""):
    _isolate_world_kb(monkeypatch, tmp_path, world, files)
    return STRUCT._apply_codex_evidence_gate(
        claims, lens=lens, world=world, scope_paths=[], layer="both", personal_facts=personal_facts)


def test_evidence_gate_demotes_confirmed_claim_missing_required_kind(monkeypatch, tmp_path):
    out, meta, missing, unavailable = _gate(monkeypatch, tmp_path, "gw1", _SRC_AND_SPEC,
                                            [_claim("c1", "confirmed", ["source"])])
    assert out[0]["status"] == "inferred"
    assert "設計書" in out[0]["reason"]
    assert out[0]["reason_code"] == ""
    assert unavailable == ()
    assert meta["demoted"] == 1


def test_evidence_gate_leaves_undeclared_legacy_claim_untouched(monkeypatch, tmp_path):
    out, meta, _, _ = _gate(monkeypatch, tmp_path, "gw2", _SRC_AND_SPEC, [_claim("c1", "confirmed", None)])
    assert out[0]["status"] == "confirmed"
    assert meta["applied"] is False
    assert meta["missing_codes"] == []


def test_evidence_gate_scope_absent_kind_is_not_counted_as_missing(monkeypatch, tmp_path):
    out, meta, missing, unavailable = _gate(monkeypatch, tmp_path, "gw3", _SRC_ONLY,
                                            [_claim("c1", "confirmed", ["source"])])
    assert out[0]["status"] == "confirmed"
    assert missing == ()
    assert unavailable == ("spec_doc",)
    assert meta["unavailable"] == ["spec_doc"]
    assert meta["missing_codes"] == []


def test_evidence_gate_personal_hits_satisfy_log_config(monkeypatch, tmp_path):
    """個人ファイルの grep ヒットがあれば、範囲にログ・設定が無くても充足済みとして格下げ・不足計上しない。"""
    out, meta, missing, unavailable = _gate(monkeypatch, tmp_path, "gw4", _SRC_ONLY,
                                            [_claim("c1", "confirmed", ["source"])],
                                            lens="troubleshoot", personal_facts="grep ヒットあり")
    assert unavailable == () and missing == ()
    assert meta["missing_codes"] == []
    assert out[0]["status"] == "confirmed"


def test_evidence_gate_log_config_missing_without_personal_hits(monkeypatch, tmp_path):
    out, meta, missing, _ = _gate(monkeypatch, tmp_path, "gw5", {**_SRC_ONLY, "logs/batch.log": "ERROR x\n"},
                                  [_claim("c1", "confirmed", ["source"])], lens="troubleshoot")
    assert "log_config" in missing
    assert "log_missing" in meta["missing_codes"]
    assert out[0]["status"] == "inferred"


def _gate_run(tmp_path, monkeypatch, *, claims, answer, uid, cid, files, lens=None):
    claims_json = json.dumps({"status": "final", "answer": answer, "next_step": None, "claims": claims})
    steps = [{"thread_id": f"TH-{uid}", "agent_messages": [claims_json], "usage": helper._usage()}]
    _setup(tmp_path, monkeypatch, steps, users_dirname=uid, schema_env="2")
    _isolate_world_kb(monkeypatch, tmp_path, "v1", files)   # `_setup` の後＝fixtures 指定を上書きする
    ctx = helper._ctx(uid=uid, conversation_id=cid)
    if lens:
        ctx = dataclasses.replace(
            ctx, route=lambda msg: {"lens": lens, "input": msg, "reason": "test", "confident": True})
    return helper._result_env(helper._run(A.CodexProvider(), ctx))


def test_evidence_gate_headline_gets_note_prefix_for_qa_lens(tmp_path, monkeypatch):
    env = _gate_run(tmp_path, monkeypatch, claims=[_claim("c1", "confirmed", ["source"])],
                    answer="標準税率は10%です。", uid="schema-v2-gate", cid=20705, files=_SRC_AND_SPEC)
    assert env["headline"].startswith("設計書を確認できていないため、この点は確定できません。")
    assert env["headline"].endswith("標準税率は10%です。")
    assert env["data"]["evidence_gate"]["missing_codes"] == ["spec_missing"]
    assert env["data"]["claims"][0]["status"] == "inferred"


def test_evidence_gate_headline_notes_demotion_when_turn_kinds_are_complete(tmp_path, monkeypatch):
    """申告の和集合は必須種別を満たすが個々の主張は欠いて格下げされたターンは、推定扱いの旨を前置する。"""
    env = _gate_run(tmp_path, monkeypatch,
                    claims=[_claim("c1", "confirmed", ["source"]), _claim("c2", "confirmed", ["spec_doc"])],
                    answer="標準税率は10%です。", uid="schema-v2-demoted", cid=20707, files=_SRC_AND_SPEC)
    assert env["headline"].startswith(STRUCT._DEMOTED_CLAIMS_NOTE)
    assert env["data"]["evidence_gate"]["missing_codes"] == []
    assert env["data"]["evidence_gate"]["demoted"] == 2
    assert all(c["status"] == "inferred" for c in env["data"]["claims"])


def test_evidence_gate_author_lens_skips_headline_note(tmp_path, monkeypatch):
    """作成系は本文が成果物の中身になるため注記を混ぜない（格下げ自体は効く）。"""
    env = _gate_run(tmp_path, monkeypatch, claims=[_claim("c1", "confirmed", ["source"])],
                    answer="資料を作成しました。", uid="schema-v2-author", cid=20706,
                    files=_SRC_AND_SPEC, lens="author")
    assert env["headline"] == "資料を作成しました。"
    assert env["data"]["claims"][0]["status"] == "inferred"


_BAD_CLAIM = {"id": "cx", "status": "confirmed", "text": "t", "evidence_refs": [], "reason": "", "reason_code": "", "extra": 1}


def test_invalid_claim_does_not_skip_gate_for_valid_claims(tmp_path, monkeypatch):
    """不正な主張が混じっても、正しい主張は根拠種別ゲートにかかり、不正があった旨が前置される。"""
    env = _gate_run(tmp_path, monkeypatch, claims=[_claim("c1", "confirmed", ["source"]), _BAD_CLAIM],
                    answer="標準税率は10%です。", uid="schema-v2-mixed", cid=20708, files=_SRC_AND_SPEC)
    assert [c["status"] for c in env["data"]["claims"]] == ["inferred"]
    assert env["data"]["claims_invalid"] == 1
    assert env["headline"].startswith(STRUCT._INVALID_CLAIMS_NOTE)
    assert env["headline"].endswith("標準税率は10%です。")


def test_all_invalid_claims_leave_no_confirmed_and_note(tmp_path, monkeypatch):
    env = _gate_run(tmp_path, monkeypatch, claims=[_BAD_CLAIM, {**_BAD_CLAIM, "id": "cy"}],
                    answer="標準税率は10%です。", uid="schema-v2-allbad", cid=20709, files=_SRC_AND_SPEC)
    assert env["data"]["claims"] == []
    assert env["data"]["claims_invalid"] == 2
    assert env["headline"].startswith(STRUCT._INVALID_CLAIMS_NOTE)


def test_legitimately_empty_claims_get_no_note(tmp_path, monkeypatch):
    env = _gate_run(tmp_path, monkeypatch, claims=[], answer="こんにちは。",
                    uid="schema-v2-empty", cid=20710, files=_SRC_AND_SPEC)
    assert env["headline"] == "こんにちは。"
    assert "claims_invalid" not in env.get("data", {})


def _headline_state(**kw):
    from types import SimpleNamespace
    base = dict(_structured_answers=[], _structured_answers_valid_from=0, _schema_v2=False,
                _agent_msgs=[], _agent_partial="", _attempt_msgs_start=0, _answer_notices=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_salvage_uses_only_top_level_answer_of_latest_attempt():
    from sherpa.providers.codex.turn_candidates import _pick_structured_headline as pick
    for msg in ('{"meta":{"answer":"入れ子の値"},"status":"in_pro', "[]"):
        assert pick(_headline_state(_agent_msgs=[msg])) == _FIXED_NO_ANSWER
    # 最新の試行に本文がないときは、前の試行から回収した本文を残す。
    assert "前の試行の本文です。" in pick(
        _headline_state(_agent_msgs=["前の試行の本文です。"], _attempt_msgs_start=1))


def test_ledger_rejected_final_is_shown_with_its_own_note_not_the_cut_off_note():
    from sherpa.providers.codex.turn_candidates import _pick_structured_headline as pick
    rejected = {"status": "final", "answer": "台帳確認前の回答です。", "next_step": None}
    st = _headline_state(_structured_answers=[rejected], _structured_answers_valid_from=1,
                         _agent_msgs=["x"], _attempt_msgs_start=1)
    out = pick(st)
    # 本文は差し戻された回答だけ。台帳の注記は本文に混ぜず notices に足す。
    assert out == "台帳確認前の回答です。"
    assert [k for k, _ in st._answer_notices] == ["ledger_unfinished"]
    assert "調査台帳の確認が終わる前の回答です" in st._answer_notices[0][1]
    assert "途中で切れた" not in out


def test_resume_fallback_drops_old_resume_sid(monkeypatch):
    from types import SimpleNamespace
    from sherpa.providers.codex import turn_loop
    st = _headline_state(
        uid="u", mcp_neighbors=[], mcp_graph_results=[], _wall_clock_state={"hit": False},
        _all_parent_thread_ids=[], got_any_line=False, attempt_returncode=1, resume_sid="OLD",
        codex_question=None, _stream_error=False, codex_usage=None, ran=False, thread_id=None,
        _latest_structured=None, _resume_fallback_happened=False, prompt="p", prompt_with_history="ph")
    monkeypatch.setattr(turn_loop, "_attempt", lambda *a, **k: iter(()))
    monkeypatch.setattr(turn_loop, "_absorb_last_message_fallback", lambda st: None)
    monkeypatch.setattr(turn_loop, "_update_structured_state", lambda st: None)
    monkeypatch.setattr(turn_loop, "_absorb_mcp_sidecar", lambda st: None)
    ctx = SimpleNamespace(stop_event=None, conversation_id=1)
    list(turn_loop._resume_fallback(None, ctx, st, {}))
    assert st.resume_sid is None and st.prompt == "ph"
