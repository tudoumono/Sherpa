"""Codex `--output-schema`（構造化最終応答での完了判定・docs/proposals/2026-09-08-Codex出力スキーマ.md）。

`providers/codex/provider.py::CodexProvider._run_authoring` は、Codex(OpenAI) 構成かつ env
`SHERPA_CODEX_OUTPUT_SCHEMA` が "0" でない（既定 ON）とき `--output-schema <固定スキーマファイル>` を
付け、最終応答を `{"status": "final"|"in_progress", "answer": str, "next_step": str|null}` の3キー
JSON に固定する。継続判定・見出し選択は、この構造化応答（`_parse_structured` で検証したもの）だけを
根拠にし、平文の語尾ヒューリスティック（`_needs_continuation`/`_pick_codex_headline`・
tests/unit/test_codex_auto_continue.py の契約）へは戻らない——不正な最終出力（構文エラー・途中で
切れた JSON・未知の status・必須欠落・型違い・余分なキー）も「未完了」として扱う。

偽 codex は test_codex_auto_continue.py の多段階版（呼び出し回数ごとに応答計画を JSON で渡す・
`agent_messages`/`last_message`/`extra_events` を持つ）をそのまま再利用する（tests/unit は
`__init__.py` を持たない rootless パッケージのため、同じ流儀の
docs/notes/2026-09-08-Codex出力スキーマ-RV再現.py が実証済みの「`import test_codex_auto_continue as
helper`」で読み込む）。JSON 文字列を `agent_messages`/`last_message` に渡すだけで足りるため、
偽 codex 自体（`helper._write_fake_codex`）は無改修で再利用する。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import test_codex_auto_continue as helper  # noqa: E402

from sherpa import agents as A  # noqa: E402
from sherpa.providers.codex import provider as PV  # noqa: E402


def _sj(status: str, answer: str, next_step: str | None = None) -> str:
    """構造化最終応答の JSON 文字列（偽 codex の `agent_messages`/`last_message` にそのまま渡す）。"""
    return json.dumps({"status": status, "answer": answer, "next_step": next_step}, ensure_ascii=False)


def _setup(tmp_path: Path, monkeypatch, steps: list, users_dirname: str,
          schema_env: str | None = None) -> Path:
    """`helper._setup` と同じ偽 codex 方式だが、出力スキーマは既定 ON のまま呼び出す（本ファイルは
    スキーマ契約そのものの検証が目的のため、`helper._setup` の env 0 固定は使わない）。`schema_env`
    を渡すと `SHERPA_CODEX_OUTPUT_SCHEMA` を明示的に上書きする（4番: 無効化の確認用）。"""
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


# ===== 1. in_progress → final で2回のうちに結論に届く =====

def test_in_progress_then_final_concludes_in_two_calls(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-SCHEMA-1",
         "agent_messages": [_sj("in_progress", "まず資料を確認します。", "税率マスタを読む")],
         "usage": helper._usage()},
        {"thread_id": "TH-SCHEMA-1",
         "agent_messages": [_sj("final", "確認した結果、影響はありません。")],
         "usage": helper._usage(30, 2, 13, 3)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_final")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-final", conversation_id=20001)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "確認した結果、影響はありません。"
    assert not env.get("codex_stopped_early")
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2, f"in_progress→final で2回のはず: {calls!r}"


# ===== 2. status=final なら宣言調の文面でも継続しない =====

def test_final_status_does_not_continue_even_with_declarative_wording(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-SCHEMA-2",
         "agent_messages": [_sj("final", "まず確認します。次に調べます。")],
         "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_final_wording")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-final-wording", conversation_id=20002)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "まず確認します。次に調べます。"
    assert not env.get("codex_stopped_early")
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1, f"status=final なのに語尾ヒューリスティックで継続している: {calls!r}"


# ===== 3. -o だけに構造化 JSON（--json ストリームに agent_message が無い） =====

def test_last_message_file_only_json_final(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-SCHEMA-3",
         "last_message": _sj("final", "資料を確認した結果、対象はありません。"),
         "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_lastmsg_only")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-lastmsg-only", conversation_id=20003)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "資料を確認した結果、対象はありません。"
    assert not env.get("codex_stopped_early")
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1


# ===== 4. 無効化（env 0 / Codex(Ollama) 構成）: --output-schema が付かず現行ヒューリスティックのまま =====

def test_env_disabled_skips_output_schema_and_uses_heuristic(tmp_path, monkeypatch):
    """env 0 のときは平文の語尾ヒューリスティック契約（test_codex_auto_continue.py と同じ判定）に
    戻る——作業宣言→結論の2回で継続が終わることを確認する。"""
    steps = [
        {"thread_id": "TH-SCHEMA-OFF", "agent_messages": ["まず資料を確認します。"], "usage": helper._usage()},
        {"thread_id": "TH-SCHEMA-OFF", "agent_messages": ["資料を確認した結果、影響はありません。"],
         "usage": helper._usage(30, 2, 13, 3)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_env_off", schema_env="0")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-env-off", conversation_id=20004)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "資料を確認した結果、影響はありません。"
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2, f"env 無効時は現行ヒューリスティックのはず: {calls!r}"
    assert not any("--output-schema" in c for c in calls), "env 無効なのに --output-schema が付いている"


def test_ollama_construct_skips_output_schema(tmp_path, monkeypatch):
    """Codex(Ollama) 構成（`ollama_base_url` あり）は未確認のため対象外（§2-8）。"""
    steps = [{"thread_id": "TH-SCHEMA-OLLAMA", "agent_messages": ["確認した結果、対象はありません。"],
              "usage": helper._usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_ollama")
    prov = A.CodexProvider(ollama_base_url="http://localhost:11434")
    ctx = helper._ctx(uid="schema-ollama", conversation_id=None)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "確認した結果、対象はありません。"
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1
    assert "--output-schema" not in calls[0], "Codex(Ollama) 構成なのに --output-schema が付いている"


# ===== 5. 不正な最終出力は通常完了にしない（継続し、尽くせば codex_stopped_early・生 JSON を出さない） =====

_BROKEN_PAYLOADS = [
    ("truncated", '{"status":"in_progress","answer":"これから資料を確認します。","next_step":'),
    ("unknown_status", json.dumps({"status": "done", "answer": "終わりました。", "next_step": None})),
    ("missing_next_step", json.dumps({"status": "final", "answer": "終わりました。"})),
    ("wrong_type_answer", json.dumps({"status": "final", "answer": 123, "next_step": None})),
    ("extra_key", json.dumps(
        {"status": "final", "answer": "終わりました。", "next_step": None, "extra": "x"})),
]


@pytest.mark.parametrize("label,payload", _BROKEN_PAYLOADS)
def test_invalid_final_output_is_not_treated_as_complete(tmp_path, monkeypatch, label, payload):
    steps = [{"thread_id": f"TH-SCHEMA-BAD-{label}", "agent_messages": [payload], "usage": helper._usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname=f"users_schema_bad_{label}")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "0")   # 尽くさせて即 codex_stopped_early を確認する
    prov = A.CodexProvider()
    ctx = helper._ctx(uid=f"schema-bad-{label}", conversation_id=20100)

    env = helper._result_env(helper._run(prov, ctx))

    assert env.get("codex_stopped_early") is True, f"{label}: 不正な最終出力が完了扱いになっている: {env!r}"
    assert '"status"' not in env["headline"], f"{label}: 生 JSON が headline に出ている: {env['headline']!r}"
    assert env["headline"] == "回答を取り出せませんでした。もう一度お試しください。"
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1


def test_invalid_final_output_triggers_continuation_when_limit_allows(tmp_path, monkeypatch):
    """不正な最終出力は「未完了」として扱われ、上限が許せば実際に継続 attempt が呼ばれる
    （5番の「継続し、尽くせば」の前半＝継続そのものを確認する）。"""
    truncated = '{"status":"in_progress","answer":"これから資料を確認します。","next_step":'
    steps = [
        {"thread_id": "TH-SCHEMA-BAD-CONT", "agent_messages": [truncated], "usage": helper._usage()},
        {"thread_id": "TH-SCHEMA-BAD-CONT", "agent_messages": [truncated], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_bad_cont")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "1")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-bad-cont", conversation_id=20101)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2, f"不正な出力は継続するはず: {calls!r}"
    assert env.get("codex_stopped_early") is True
    assert '"status"' not in env["headline"]


# ===== 6. 平文 commentary ＋有効な最終 JSON は正常処理／-o とストリーム末尾の食い違いは -o を採る =====

def test_plain_commentary_mixed_with_valid_final_json_is_handled(tmp_path, monkeypatch):
    final = _sj("final", "依存先を確認しました。")
    steps = [
        {"thread_id": "TH-SCHEMA-MIX", "agent_messages": ["資料を確認しています…", final],
         "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_mix")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-mix", conversation_id=20200)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "依存先を確認しました。"
    assert not env.get("codex_stopped_early")
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1


def test_last_message_file_wins_over_stream_tail_mismatch(tmp_path, monkeypatch):
    stream_tail = _sj("in_progress", "まだ調べています。", "続きを見る")
    last_message = _sj("final", "対象は3件でした。")
    steps = [
        {"thread_id": "TH-SCHEMA-MISMATCH", "agent_messages": [stream_tail], "last_message": last_message,
         "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_mismatch")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-mismatch", conversation_id=20201)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "対象は3件でした。", "-o とストリーム末尾が食い違うとき -o を採っていない"
    assert not env.get("codex_stopped_early")
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1


# ===== 7. argv に --output-schema <絶対パス> が付く（OpenAI 系・既定） =====

def test_argv_includes_output_schema_flag_by_default(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-SCHEMA-ARGV", "agent_messages": [_sj("final", "対象はありません。")],
              "usage": helper._usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_argv")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-argv", conversation_id=20300)

    helper._run(prov, ctx)

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1
    argv = calls[0]
    assert "--output-schema" in argv
    schema_path = argv[argv.index("--output-schema") + 1]
    assert schema_path.endswith("output_schema.json")
    assert Path(schema_path).is_absolute()
    assert Path(schema_path).is_file()


# ===== 8. turn.failed（agent_message 無し・終了コード1）は明示的な失敗として返す =====

def test_turn_failed_without_agent_message_is_explicit_failure(tmp_path, monkeypatch):
    """既存の「stdout に JSON が1行も無い」無出力失敗（test_codex_silent_output_failure.py）とは別
    ケース——JSON イベント自体は複数行読めている（got_any_line=True）が、agent_message が1つも無い
    まま `turn.failed` で終わる。"""
    steps = [
        {"exit_code": 1,
         "extra_events": [
             {"type": "turn.started"},
             {"type": "error", "message": "invalid_json_schema"},
             {"type": "turn.failed", "error": {"message": "invalid_json_schema"}},
         ]},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_turnfailed")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-turnfailed", conversation_id=None,
                      message="偽 codex turn.failed テスト")

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] != "dispatch-headline", (
        f"turn.failed なのに決定的回答をそのまま返している: {env!r}")
    assert "接続できません" in env["headline"]
    assert "回答を返せずに終了しました" in env["headline"]
    assert not env.get("codex_stopped_early")
    assert not env.get("codex_timed_out")
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1


# ===== 9. headline に生 JSON が出ない（1〜6 の assert に加え、明示的にも固定する） =====

def test_headline_never_contains_raw_structured_json(tmp_path, monkeypatch):
    truncated = '{"status":"in_progress","answer":"これから確認します。","next_step":'
    steps = [{"thread_id": "TH-SCHEMA-RAWCHECK", "agent_messages": [truncated], "usage": helper._usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_rawcheck")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "0")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-rawcheck", conversation_id=20400)

    env = helper._result_env(helper._run(prov, ctx))

    assert "{" not in env["headline"], f"headline に生 JSON らしき文字列が出ている: {env['headline']!r}"
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1


# ===== 10. RV是正: AGENTS.md の構造化段落・継続プロンプトの語彙はスキーマ有効時だけ =====

def test_write_agents_md_structured_paragraph_only_when_schema_on(tmp_path):
    """`write_agents_md` は `output_schema=True` のときだけ `status`/`answer`/`next_step` の3項目段落を
    足す（既定 False＝従来どおりその段落は無い）。"""
    from sherpa import codex_agents_md
    d = tmp_path / "authoring"
    d.mkdir()
    codex_agents_md.write_agents_md(d)
    off_txt = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert "next_step" not in off_txt, "output_schema 省略（既定 False）なのに構造化応答の段落がある"

    codex_agents_md.write_agents_md(d, output_schema=True)
    on_txt = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert "next_step" in on_txt and "`status`" in on_txt, "output_schema=True なのに構造化応答の段落が無い"


def test_continue_prompt_uses_schema_wording_when_schema_on(tmp_path, monkeypatch):
    """既定 ON（スキーマ有効）の継続プロンプト（argv 末尾）は `status`/`final` の語彙を使う。"""
    steps = [
        {"thread_id": "TH-SCHEMA-CONT-PROMPT",
         "agent_messages": [_sj("in_progress", "まず資料を確認します。", "続きを見る")],
         "usage": helper._usage()},
        {"thread_id": "TH-SCHEMA-CONT-PROMPT",
         "agent_messages": [_sj("final", "確認しました。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_cont_prompt")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-cont-prompt", conversation_id=20500)

    helper._run(prov, ctx)

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2
    assert calls[1][-1] == PV._CONTINUE_PROMPT_SCHEMA, (
        f"スキーマ有効時の継続プロンプトが専用文言になっていない: {calls[1][-1]!r}")
    assert "status" in calls[1][-1] and "final" in calls[1][-1]


def test_continue_prompt_plain_wording_when_schema_off(tmp_path, monkeypatch):
    """env 0（スキーマ無効）の継続プロンプトは従来文言のまま——`status` の語彙を含まない。"""
    steps = [
        {"thread_id": "TH-SCHEMA-CONT-OFF", "agent_messages": ["まず資料を確認します。"], "usage": helper._usage()},
        {"thread_id": "TH-SCHEMA-CONT-OFF", "agent_messages": ["確認した結果、影響はありません。"],
         "usage": helper._usage(30, 2, 13, 3)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_cont_off", schema_env="0")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-cont-off", conversation_id=20501)

    helper._run(prov, ctx)

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2
    assert calls[1][-1] == PV._CONTINUE_PROMPT
    assert "status" not in calls[1][-1], f"スキーマ無効時の継続プロンプトに status が混入: {calls[1][-1]!r}"


# ===== 11. RV是正: 同一 attempt 内で先に得た有効な final を、後続の壊れた JSON で失わない =====

def test_valid_final_earlier_in_same_attempt_is_not_lost_by_later_broken_message(tmp_path, monkeypatch):
    """1回の attempt に agent_message が複数届き、先に有効な `final` JSON・後に壊れた JSON が続く場合、
    見出しはその先に得た `final` の answer になる（`_structured_answers` が最終候補（末尾）だけでなく
    attempt 内の全 message を検証して積むようになったことの回帰確認・§2-3）。"""
    valid_final = _sj("final", "先に得た結論です。")
    broken = '{"status":"in_progress","answer":"これから確認します。","next_step":'
    steps = [{"thread_id": "TH-SCHEMA-KEEP-FINAL",
              "agent_messages": [valid_final, broken], "usage": helper._usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_keep_final")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "0")   # 尽くさせて1 attempt だけで確定させる
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-keep-final", conversation_id=20600)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "先に得た結論です。", f"先に得た final が失われている: {env!r}"
    assert env.get("codex_stopped_early") is True, "最新出力（壊れた JSON）は未完了のはずなのに立っていない"
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1


# ===== 12. RV是正: resume フォールバックは古いセッションの構造化状態を引き継がない =====

def test_resume_fallback_resets_structured_state(tmp_path, monkeypatch):
    """resume 先セッションが無出力で失敗（`-o` にだけ古い `final` が残る）→ 新規セッションへ
    フォールバック→ フレッシュ attempt は `in_progress` のまま尽きる、というターンで、headline が
    フォールバック前の古い `final` に戻らず、フレッシュ側の最後の `in_progress` の answer になる
    （`_structured_answers`/`_latest_structured` をフォールバック時にクリアする・§2-3）。
    DB 不要（`test_codex_resume.py` と同じ流儀で `Ctx.codex_session_id` を直接渡す）。"""
    steps = [
        # resume attempt: thread_id 無し＝stdout 出力ゼロ（got_any_line=False）で resume 失敗と
        # みなされる。-o にだけ古いセッションの final を残す（フォールバック前に吸収され得る）。
        {"last_message": _sj("final", "古いセッションの結論です。"), "exit_code": 1},
        # フォールバック後のフレッシュ attempt: in_progress のまま（継続は env で止める）。
        {"thread_id": "TH-SCHEMA-RESET-FRESH",
         "agent_messages": [_sj("in_progress", "フレッシュ側の途中経過です。", "続きを見る")],
         "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_resume_reset")
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "0")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-resume-reset", conversation_id=20700, codex_session_id="SID-SCHEMA-STALE")

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "フレッシュ側の途中経過です。", (
        f"resume 失敗前の古い final が見出しに残っている: {env!r}")
    assert env.get("codex_stopped_early") is True
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2, f"resume 失敗→フレッシュの2回のはず: {calls!r}"
    assert "resume" in calls[0] and "SID-SCHEMA-STALE" in calls[0]
    assert "resume" not in calls[1], "フォールバック後は resume を試みないはず"


# ===== 12b. RV是正: 継続 attempt の turn.failed（agent_message 無し）は古い回答に戻らず明示失敗 =====

def test_continuation_turn_failed_without_new_message_is_explicit_failure(tmp_path, monkeypatch, caplog):
    """初回 attempt は正常な `in_progress`（継続対象）→ その続き（resume）attempt が agent_message を
    1つも出さないまま `turn.failed`・exit 1 で終わる場合、`_pick_structured_headline` が拾う古い
    `in_progress` の answer をそのまま見出しにせず、既存の silent failure 分岐（固定失敗文言）へ入る
    （§2-10 追加是正）。"""
    import logging
    steps = [
        {"thread_id": "TH-SCHEMA-CONT-TURNFAILED",
         "agent_messages": [_sj("in_progress", "まず資料を確認します。", "続きを見る")],
         "usage": helper._usage()},
        {"exit_code": 1,
         "extra_events": [
             {"type": "turn.started"},
             {"type": "error", "message": "invalid_json_schema"},
             {"type": "turn.failed", "error": {"message": "invalid_json_schema"}},
         ]},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_cont_turnfailed")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-cont-turnfailed", conversation_id=20800)

    with caplog.at_level(logging.WARNING, logger="sherpa"):
        env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] != "まず資料を確認します。", (
        f"継続先の turn.failed なのに古い in_progress の answer が見出しに残っている: {env!r}")
    assert "接続できません" in env["headline"] and "回答を返せずに終了しました" in env["headline"]
    assert not env.get("codex_stopped_early"), "turn.failed による明示失敗のはずが codex_stopped_early も立っている"
    assert any("turn_failed=True" in r.message for r in caplog.records), \
        "warning ログに turn_failed=True が記録されていない"
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2, f"in_progress→続き（turn.failed）の2回のはず: {calls!r}"


# ===== 13. RV是正: `_parse_structured` は status が非 str でも例外にならない =====

def test_parse_structured_rejects_non_string_status_list():
    assert PV._parse_structured(json.dumps({"status": [], "answer": "x", "next_step": None})) is None


def test_parse_structured_rejects_non_string_status_dict():
    assert PV._parse_structured(json.dumps({"status": {}, "answer": "x", "next_step": None})) is None


# ===== 出力スキーマファイル自体の内容（§1・strict 契約） =====

def test_output_schema_file_matches_contract():
    schema = json.loads(PV._OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"status", "answer", "next_step"}
    assert schema["properties"]["status"]["enum"] == ["final", "in_progress"]
    assert schema["properties"]["next_step"]["type"] == ["string", "null"]


# ===== `_parse_structured`（純関数） =====

def test_parse_structured_accepts_well_formed_final():
    obj = PV._parse_structured(json.dumps({"status": "final", "answer": "ok", "next_step": None}))
    assert obj == {"status": "final", "answer": "ok", "next_step": None}


def test_parse_structured_accepts_in_progress_with_next_step():
    obj = PV._parse_structured(json.dumps({"status": "in_progress", "answer": "調べます", "next_step": "続き"}))
    assert obj == {"status": "in_progress", "answer": "調べます", "next_step": "続き"}


def test_parse_structured_rejects_empty_or_none():
    assert PV._parse_structured("") is None
    assert PV._parse_structured(None) is None


def test_parse_structured_rejects_non_json():
    assert PV._parse_structured("これは JSON ではありません") is None


def test_parse_structured_rejects_truncated_json():
    assert PV._parse_structured('{"status":"final","answer":"x","next_step":') is None


def test_parse_structured_rejects_non_object_json():
    assert PV._parse_structured(json.dumps(["final", "x", None])) is None


def test_parse_structured_rejects_unknown_status():
    assert PV._parse_structured(json.dumps({"status": "done", "answer": "x", "next_step": None})) is None


def test_parse_structured_rejects_missing_key():
    assert PV._parse_structured(json.dumps({"status": "final", "answer": "x"})) is None


def test_parse_structured_rejects_extra_key():
    assert PV._parse_structured(json.dumps(
        {"status": "final", "answer": "x", "next_step": None, "extra": 1})) is None


def test_parse_structured_rejects_non_string_answer():
    assert PV._parse_structured(json.dumps({"status": "final", "answer": 1, "next_step": None})) is None


def test_parse_structured_rejects_non_string_next_step():
    assert PV._parse_structured(json.dumps({"status": "in_progress", "answer": "x", "next_step": 1})) is None


def test_empty_final_answer_falls_to_fixed_message_not_dispatch(tmp_path, monkeypatch):
    """`{"status":"final","answer":"","next_step":null}` は本文なし＝固定文言。dispatch の決定的見出しを
    正常回答として返さない（継続はしない＝final の宣言は尊重）。"""
    steps = [{"thread_id": "TH-EMPTY", "agent_messages": [
        json.dumps({"status": "final", "answer": "", "next_step": None}, ensure_ascii=False)],
        "usage": helper._usage()}]
    argv_log = helper._setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_empty_final")
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "1")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-empty-final", conversation_id=930)
    env = helper._result_env(helper._run(prov, ctx))
    assert len(helper._read_argv_log(argv_log)) == 1
    assert env["headline"] == "回答を取り出せませんでした。もう一度お試しください。"
    assert env["headline"] != "dispatch-headline"


# ===== DEPTH-2 S1（出力スキーマ v2・主張配列・docs/proposals/2026-09-17-深さの再定義とレビュー巡.md §2.5） =====

def _sj2(status: str, answer: str, claims: list, next_step: str | None = None) -> str:
    return json.dumps({"status": status, "answer": answer, "next_step": next_step, "claims": claims},
                      ensure_ascii=False)


def test_parse_structured_v2_reads_claims_array():
    payload = _sj2("final", "回答", [
        {"id": "c1", "status": "confirmed", "text": "t1", "evidence_refs": ["ev-1"],
         "reason": "", "reason_code": ""},
        {"id": "c2", "status": "unknown", "text": "t2", "evidence_refs": [],
         "reason": "", "reason_code": "budget"}])
    parsed = PV._parse_structured_v2(payload)
    assert parsed is not None
    assert parsed["answer"] == "回答"
    assert len(parsed["claims"]) == 2
    assert parsed["claims"][1]["reason_code"] == "budget"


def test_parse_structured_v2_backward_compatible_with_v1_three_keys():
    """v1 の3キー形（`claims` 無し）も後方互換で読める——`claims` は空配列を補う。"""
    payload = json.dumps({"status": "final", "answer": "回答", "next_step": None})
    assert PV._parse_structured_v2(payload) == {
        "status": "final", "answer": "回答", "next_step": None, "claims": []}


def test_parse_structured_v2_rejects_truncated_json():
    truncated = '{"status":"final","answer":"回答","next_step":null,"claims":[{"id":"c1"'
    assert PV._parse_structured_v2(truncated) is None


def test_parse_structured_v2_falls_back_to_empty_claims_on_invalid_claim_reason_code():
    """不正な主張要素は主張構造だけを不採用にする——`status`/`answer` は保持し `claims` を
    空にする（RV #15: 応答全体を捨てて固定文言に落とさない）。"""
    payload = _sj2("final", "回答", [{"id": "c1", "status": "unknown", "text": "t",
                                     "evidence_refs": [], "reason": "", "reason_code": "not_a_real_code"}])
    parsed = PV._parse_structured_v2(payload)
    assert parsed == {"status": "final", "answer": "回答", "next_step": None, "claims": []}


def test_parse_structured_v2_falls_back_to_empty_claims_on_extra_claim_key():
    payload = _sj2("final", "回答", [{"id": "c1", "status": "confirmed", "text": "t",
                                     "evidence_refs": [], "reason": "", "reason_code": "", "extra": 1}])
    parsed = PV._parse_structured_v2(payload)
    assert parsed == {"status": "final", "answer": "回答", "next_step": None, "claims": []}


def test_env_2_selects_v2_schema_file_and_surfaces_claims(tmp_path, monkeypatch):
    """env `SHERPA_CODEX_OUTPUT_SCHEMA=2` は v2 スキーマファイルを argv に付け、応答の `claims`
    が envelope（`data.claims`）へ現れる。"""
    steps = [{"thread_id": "TH-SCHEMA-V2",
             "agent_messages": [_sj2("final", "確認した結果、標準税率は10%です。", [
                 {"id": "c1", "status": "confirmed", "text": "標準税率は10%。",
                  "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""},
                 {"id": "c2", "status": "unknown", "text": "軽減税率の適用開始日は不明。",
                  "evidence_refs": [], "reason": "", "reason_code": "unexplored"}])],
             "usage": helper._usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_v2", schema_env="2")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-v2", conversation_id=20700)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "確認した結果、標準税率は10%です。"
    claims = env["data"]["claims"]
    assert {c["status"] for c in claims} == {"confirmed", "unknown"}
    unknown = next(c for c in claims if c["status"] == "unknown")
    assert unknown["reason_code"] == "unexplored"
    calls = helper._read_argv_log(argv_log)
    argv = calls[0]
    schema_path = argv[argv.index("--output-schema") + 1]
    assert schema_path.endswith("output_schema_v2.json")


def test_env_1_default_still_uses_v1_schema_file_without_claims():
    """既定（env 未設定＝1）は従来どおり v1 スキーマ——`data.claims` は付かない
    （既存テスト7番と同じ argv 契約・回帰確認）。"""
    assert PV._env_int("SHERPA_CODEX_OUTPUT_SCHEMA", 1, 0, 2) == 1


# ===== RV C1/C4（docs/rv/2026-09-17-DEPTH-2.md）: `_parse_claim` の裏付け・理由必須 =====

def test_parse_claim_rejects_confirmed_with_empty_evidence_refs():
    """Codex は state の ev_id を持たないため実在チェックまではできないが、confirmed が
    根拠参照を1件も挙げない（裏付けゼロの確定）主張は拒否する。"""
    item = {"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": [],
            "reason": "", "reason_code": ""}
    assert PV._parse_claim(item) is None


def test_parse_claim_rejects_confirmed_with_whitespace_only_evidence_refs():
    """`evidence_refs` が空白のみの文字列だけだと、非空リストでも裏付けゼロ扱いで拒否する
    （API 経路 `investigation_state.InvestigationState.set_claims` と同じ規約）。"""
    item = {"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": [""],
            "reason": "", "reason_code": ""}
    assert PV._parse_claim(item) is None


def test_parse_claim_accepts_confirmed_with_evidence_refs():
    item = {"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["4期/01_標準/消費税法.md"],
            "reason": "", "reason_code": ""}
    assert PV._parse_claim(item) == item


def test_parse_claim_rejects_inferred_with_empty_reason():
    item = {"id": "c1", "status": "inferred", "text": "t", "evidence_refs": [],
            "reason": "", "reason_code": ""}
    assert PV._parse_claim(item) is None


def test_parse_claim_rejects_inferred_with_whitespace_only_reason():
    item = {"id": "c1", "status": "inferred", "text": "t", "evidence_refs": [],
            "reason": "   ", "reason_code": ""}
    assert PV._parse_claim(item) is None


def test_parse_structured_v2_falls_back_to_empty_claims_on_confirmed_without_evidence_refs():
    """`_parse_structured_v2` は `claims` の各要素を `_parse_claim` で検証する——1件でも
    裏付けゼロの confirmed があれば主張配列だけを空にし、`status`/`answer` は保持する。"""
    payload = _sj2("final", "回答", [{"id": "c1", "status": "confirmed", "text": "t",
                                     "evidence_refs": [], "reason": "", "reason_code": ""}])
    parsed = PV._parse_structured_v2(payload)
    assert parsed == {"status": "final", "answer": "回答", "next_step": None, "claims": []}


def test_env_2_invalid_claim_keeps_final_answer_without_extra_continuation(tmp_path, monkeypatch):
    """RV #15: status=final・answer 有りの応答で claims に不正要素が1件混じっていても、
    answer/status は保持されて claims だけが空になり、不要な continuation attempt を消費せず
    固定文言「回答を取り出せませんでした」にも落ちない。"""
    steps = [{"thread_id": "TH-SCHEMA-V2-BADCLAIM",
             "agent_messages": [_sj2("final", "確認した結果、標準税率は10%です。", [
                 {"id": "c1", "status": "unknown", "text": "軽減税率の適用開始日は不明。",
                  "evidence_refs": [], "reason_code": "not_a_real_code"}])],
             "usage": helper._usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_v2_badclaim", schema_env="2")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-v2-badclaim", conversation_id=20701)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["headline"] == "確認した結果、標準税率は10%です。"
    assert "回答を取り出せませんでした" not in env["headline"]
    assert not env.get("data", {}).get("claims")
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1, f"不正な claims 要素だけで応答全体が捨てられ継続してしまっている: {calls!r}"


# ===== RV C5: v2 選択時は AGENTS.md に claims の意味・区分・理由コード語彙を伝える =====

def test_write_agents_md_v2_paragraph_has_claims_vocabulary_only_when_schema_v2(tmp_path):
    """`output_schema_v2=True` のときだけ `claims`／confirmed/inferred/unknown の語彙を含む
    4項目段落を書く。`output_schema_v2=False`（既定）は従来どおり3項目段落のまま。"""
    from sherpa import codex_agents_md
    d = tmp_path / "authoring"
    d.mkdir()
    codex_agents_md.write_agents_md(d, output_schema=True, output_schema_v2=False)
    v1_txt = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert "claims" not in v1_txt, "output_schema_v2=False なのに claims の段落が混入している"

    codex_agents_md.write_agents_md(d, output_schema=True, output_schema_v2=True)
    v2_txt = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert "claims" in v2_txt and "confirmed" in v2_txt and "inferred" in v2_txt and "unknown" in v2_txt
    assert "not_found_in_scope" in v2_txt   # 閉じた理由コード語彙も明示する


def test_write_agents_md_v2_paragraph_has_final_in_progress_wording(tmp_path):
    """RV是正2巡目 指摘(2): v2 段落は元々「3項目版と同じ意味」と v1 段落を参照していたが、
    output_schema_v2=True のときは v1 段落自体が出力されず参照先が無かった——v2 段落自身に
    final／in_progress／next_step の意味と中断時（全件確認前・予算到達）の記述条件が明記され、
    v1 段落の該当記述は変わらないことを確認する。"""
    from sherpa import codex_agents_md
    d = tmp_path / "authoring"
    d.mkdir()

    codex_agents_md.write_agents_md(d, output_schema=True, output_schema_v2=False)
    v1_txt = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert codex_agents_md._STATUS_FIELD_MEANING in v1_txt

    codex_agents_md.write_agents_md(d, output_schema=True, output_schema_v2=True)
    v2_txt = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert codex_agents_md._STATUS_FIELD_MEANING in v2_txt, (
        "v2 段落に final/in_progress/next_step と中断時の記述条件が明記されていない")
    assert "final" in v2_txt and "in_progress" in v2_txt and "next_step" in v2_txt
    assert "予算到達" in v2_txt   # 中断条件（全件確認前・予算到達）が v2 段落自身に残っている


def test_write_agents_md_v2_paragraph_requires_output_schema_on(tmp_path):
    """`output_schema=False` なら `output_schema_v2=True` を渡しても段落を足さない
    （`--output-schema` 自体が無効なターンへ CLI が強制しない構造化応答を約束させない）。"""
    from sherpa import codex_agents_md
    d = tmp_path / "authoring"
    d.mkdir()
    codex_agents_md.write_agents_md(d, output_schema=False, output_schema_v2=True)
    txt = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert "claims" not in txt and "next_step" not in txt


def test_env_2_run_writes_v2_agents_md_paragraph(tmp_path, monkeypatch):
    """`SHERPA_CODEX_OUTPUT_SCHEMA=2` の実行は、実際に生成した run_dir の AGENTS.md へ
    v2 段落（claims の意味・区分・理由コード語彙）を書く——スキーマだけ v2 を選んでも
    Codex 側への指示が3項目のままだと `data.claims` が無言で空になる、という実害の再現。"""
    from sherpa import codex_agents_md
    captured: dict = {}
    orig_write = codex_agents_md.write_agents_md

    def _spy(authoring, output_schema=False, direct_read=True, output_schema_v2=False, **kw):
        captured["output_schema_v2"] = output_schema_v2
        return orig_write(authoring, output_schema=output_schema, direct_read=direct_read,
                          output_schema_v2=output_schema_v2, **kw)
    monkeypatch.setattr(PV.codex_agents_md, "write_agents_md", _spy)

    steps = [{"thread_id": "TH-SCHEMA-V2-AGENTS",
             "agent_messages": [_sj2("final", "確認しました。", [])], "usage": helper._usage()}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_v2_agents", schema_env="2")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-v2-agents", conversation_id=20701)

    helper._run(prov, ctx)

    assert captured.get("output_schema_v2") is True


def test_env_1_run_writes_v1_agents_md_paragraph(tmp_path, monkeypatch):
    """既定（env 未設定＝v1）の実行は v2 段落を渡さない（回帰確認）。"""
    from sherpa import codex_agents_md
    captured: dict = {}
    orig_write = codex_agents_md.write_agents_md

    def _spy(authoring, output_schema=False, direct_read=True, output_schema_v2=False, **kw):
        captured["output_schema_v2"] = output_schema_v2
        return orig_write(authoring, output_schema=output_schema, direct_read=direct_read,
                          output_schema_v2=output_schema_v2, **kw)
    monkeypatch.setattr(PV.codex_agents_md, "write_agents_md", _spy)

    steps = [{"thread_id": "TH-SCHEMA-V1-AGENTS",
             "agent_messages": [_sj("final", "確認しました。")], "usage": helper._usage()}]
    _setup(tmp_path, monkeypatch, steps, users_dirname="users_schema_v1_agents")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="schema-v1-agents", conversation_id=20702)

    helper._run(prov, ctx)

    assert captured.get("output_schema_v2") is False
