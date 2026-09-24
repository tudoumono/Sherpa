"""調査台帳ゲート（`docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md` §2/§4/§6 の provider.py
統合・`sherpa/investigation_ledger.py` は純関数のみ担当）。

`CodexProvider._run_authoring` は、出力スキーマ v2（`_schema_v2`）が有効なとき、モデルが返した
`status=final` をそのまま信じず、`run_dir/.tmp/investigation/` の台帳（`investigation_ledger.
load_ledger`/`ledger_complete`）が完了しているかを確認してから受理する。未完了なら台帳の状態から
組み立てた継続プロンプトで `_attempt(True, prompt_text=...)` を追加発行し（既存の自動継続
`SHERPA_CODEX_AUTO_CONTINUE` とは別枠の上限 `_LEDGER_CONTINUE_CAP`）、2 attempt 連続で無進捗・
上限到達・manifest 欠落が1回の催促でも解消しない、のいずれかで打ち切って受理する。完了せずターンが
終わった台帳は `{workspace}/.codex-sessions/{conversation_id}/investigation/` へ退避し、次ターンの
依頼文が「続き」系で始まるときだけ復元する。

既存 tests/unit/test_codex_auto_continue.py と同じ「偽 codex 実行ファイルを PATH に差し込む」流儀
（実 codex は一切呼ばない）。台帳ファイルの書込みは偽 codex 側で行う（`step["ledger"]` に
manifest/items を指定する独自拡張）。`_ctx`/`_usage`/`_run`/`_result_env`/`_read_argv_log` は
`test_codex_auto_continue.py`（tests/unit は rootless パッケージのため `import test_codex_auto_continue
as helper` で読み込む・test_codex_output_schema.py と同じ流儀）をそのまま再利用する。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import test_codex_auto_continue as helper  # noqa: E402

from sherpa import agents as A  # noqa: E402
from sherpa import investigation_ledger as IL  # noqa: E402
from sherpa.providers.codex import provider as PV  # noqa: E402

# ===== 偽 codex（台帳ファイルの書込みに対応した拡張版） =====

_FAKE_CODEX_LEDGER_PY = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys
import tomllib

argv_log = pathlib.Path(r"__ARGV_LOG__")
plan_path = pathlib.Path(r"__PLAN_PATH__")
args = sys.argv[1:]
with argv_log.open("a", encoding="utf-8") as f:
    f.write(repr(args) + "\n")
    f.flush()

call_index = len(argv_log.read_text(encoding="utf-8").splitlines())
steps = json.loads(plan_path.read_text(encoding="utf-8"))["steps"]
step = steps[call_index - 1] if call_index - 1 < len(steps) else steps[-1]

run_dir = pathlib.Path(args[args.index("-C") + 1])
if "ledger_tools" in step:
    config = tomllib.loads((pathlib.Path(os.environ["CODEX_HOME"]) / "config.toml").read_text())
    server = config["mcp_servers"]["sherpa"]
    assert server["env"]["SHERPA_MCP_LEDGER_DIR"] == str(run_dir / ".tmp/investigation")
    calls = [{"name": "ledger_status", "arguments": {}}, *step["ledger_tools"],
             {"name": "ledger_status", "arguments": {}}]
    messages = [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": call}
                for i, call in enumerate(calls)]
    process = subprocess.run([server["command"], *server["args"]],
                             input="".join(json.dumps(msg) + "\n" for msg in messages),
                             text=True, capture_output=True, check=True,
                             env={**os.environ, **server["env"]}, cwd=run_dir)
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(responses) == len(messages)
    results = []
    for response in responses:
        assert not response["result"]["isError"], response
        results.append(json.loads(response["result"]["content"][0]["text"]))
    with pathlib.Path(step["mcp_log"]).open("a", encoding="utf-8") as output:
        output.write(json.dumps(results) + "\n")
ledger = step.get("ledger")
if ledger:
    inv_dir = run_dir / ".tmp" / "investigation"
    items_dir = inv_dir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    manifest = ledger.get("manifest")
    if manifest is not None:
        (inv_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for item_id, item in (ledger.get("items") or {}).items():
        (items_dir / f"{item_id}.json").write_text(
            json.dumps(item, ensure_ascii=False), encoding="utf-8")
    if ledger.get("delete_manifest"):
        mf = inv_dir / "manifest.json"
        if mf.exists():
            mf.unlink()
    manifest_symlink_to = ledger.get("manifest_symlink_to")
    if manifest_symlink_to:
        mf = inv_dir / "manifest.json"
        if mf.exists() or mf.is_symlink():
            mf.unlink()
        mf.symlink_to(pathlib.Path(manifest_symlink_to))

tid = step.get("thread_id")
if tid:
    print(json.dumps({"type": "thread.started", "thread_id": tid}))
    sys.stdout.flush()

for i, text in enumerate(step.get("agent_messages", [])):
    print(json.dumps({"type": "item.completed",
                       "item": {"id": f"m{i}", "type": "agent_message", "text": text}}))
    sys.stdout.flush()

usage = step.get("usage")
if usage:
    print(json.dumps({"type": "turn.completed", "usage": usage}))
    sys.stdout.flush()

sys.exit(step.get("exit_code", 0))
'''


def _write_fake_codex(bin_dir: Path, argv_log: Path, plan_path: Path) -> None:
    script = bin_dir / "codex"
    script.write_text(
        _FAKE_CODEX_LEDGER_PY.replace("__ARGV_LOG__", str(argv_log))
                             .replace("__PLAN_PATH__", str(plan_path)))
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp_path: Path, monkeypatch, steps: list, users_dirname: str,
          argv_log_name: str = "argv.log") -> Path:
    """出力スキーマは既定 ON（v2）のまま呼ぶ——台帳ゲートは `_schema_v2` が有効なときだけ効く
    契約そのものを検証するため、`helper._setup` のようなスキーマ無効化はしない。`argv_log_name`
    は同一 `tmp_path` で複数ターン（同一会話の継続）をまたいで呼ぶテスト用に、呼び出し回数の
    集計先を分けられるようにする（既定は単一ターンのテストと同じ固定名）。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    argv_log = tmp_path / argv_log_name
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"steps": steps}), encoding="utf-8")
    _write_fake_codex(bin_dir, argv_log, plan_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    return argv_log


def _final(answer: str, next_step: str | None = None) -> str:
    return json.dumps({"status": "final", "answer": answer, "next_step": next_step, "claims": []},
                      ensure_ascii=False)


def _manifest(items: list, question_kind: str = "list",
             created_at: str = "2026-09-21T00:00:00Z") -> dict:
    return {"question_kind": question_kind, "created_at": created_at, "items": items}


def _item(item_id: str, status: str, reason: str = "", evidence: list | None = None,
         owner: str = "parent", required_checks: list | None = None) -> dict:
    # required_checks は空配列だと item 自体が無効になる（investigation_ledger.validate_item の
    # 語彙閉包契約）——このファイルの item は全て source の evidence（_EVIDENCE）で終端化するため
    # 既定を ["source"] にする。
    return {"id": item_id, "kind": "row", "subject": "対象",
            "required_checks": required_checks if required_checks is not None else ["source"],
            "evidence": evidence or [], "status": status, "reason": reason, "owner": owner}


_EVIDENCE = [{"kind": "source", "path": "src/a.py", "line": 1}]


def test_ledger_written_through_mcp_accepts_final(tmp_path, monkeypatch):
    """偽Codexから本物のstdio MCPサーバへ接続し、台帳をツールだけで作成する。"""
    log = tmp_path / "mcp.jsonl"
    steps = [{"thread_id": "TH-MCP-LEDGER", "mcp_log": str(log),
              "ledger_tools": [
                  {"name": "ledger_manifest_set", "arguments": {"question_kind": "list", "items": ["a"]}},
                  {"name": "ledger_item_put", "arguments": _item("a", "source_confirmed", evidence=_EVIDENCE)},
              ], "agent_messages": [_final("MCPで登録しました。")], "usage": helper._usage()}]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_mcp_ledger")
    env = helper._result_env(helper._run(A.CodexProvider(), helper._ctx("mcp-ledger", 31001)))
    assert len(helper._read_argv_log(argv_log)) == 1
    results = json.loads(log.read_text())
    assert results[0]["manifest_invalid"] is True and results[0]["items"] == 0
    assert results[1] == {"ok": True}
    assert results[2] == {"ok": True, "id": "a"}
    assert results[3]["complete"] is True
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["continuations"] == 0


def test_ledger_mcp_unsatisfied_kinds_in_continuation_prompt(tmp_path, monkeypatch):
    log = tmp_path / "mcp.jsonl"
    partial = _item("sel1_z", "spec_only", evidence=[{"kind": "spec_doc", "path": "docs/spec.md", "line": 8}])
    partial.update(required_checks=["source", "spec_doc"], subject="本文をプロンプトへ戻さない")
    complete = {**partial, "status": "source_confirmed", "evidence": [*partial["evidence"], *_EVIDENCE]}
    steps = [
        {"thread_id": "TH-MCP-UNSATISFIED", "mcp_log": str(log), "ledger_tools": [
            {"name": "ledger_manifest_set", "arguments": {"question_kind": "list", "items": ["sel1_z"]}},
            {"name": "ledger_item_put", "arguments": partial},
        ], "agent_messages": [_final("資料の確認が済みました。")], "usage": helper._usage()},
        {"thread_id": "TH-MCP-UNSATISFIED", "mcp_log": str(log), "ledger_tools": [
            {"name": "ledger_item_put", "arguments": complete},
        ], "agent_messages": [_final("ソースも確認しました。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_mcp_unsatisfied")
    env = helper._result_env(helper._run(A.CodexProvider(), helper._ctx("mcp-unsatisfied", 31002)))
    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2
    assert "未充足: sel1_z（source が未確認）" in calls[1][-1]
    assert partial["subject"] not in calls[1][-1]
    assert "docs/spec.md" not in calls[1][-1] and "src/a.py" not in calls[1][-1]
    results = [json.loads(line) for line in log.read_text().splitlines()]
    assert results[0][-1]["unsatisfied"] == {"sel1_z": ["source"]}
    assert results[1][-1]["complete"] is True
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["continuations"] == 1


# ===== 範囲にソースがあれば required_checks の宣言に関わらず source を必須にする =====
#
# モデル自身が item の required_checks に source を含めなくても、範囲にソースがある調査は Sherpa
# が完了判定へ source を機械的に足す（`_ledger_source_required_extra`）。範囲にソースが無い（層が
# docs）ターンは今までどおり。

def test_ledger_source_required_by_scope_even_when_item_omits_it(tmp_path, monkeypatch):
    """既定 world（v1・layer=both＝ソースが範囲内）では、item 自身が required_checks に source を
    含めていなくても spec_only だけの台帳は完了にならず、継続の催促に「source が未確認」が載る。"""
    partial = _item("a", "spec_only", evidence=[{"kind": "spec_doc", "path": "docs/x.md", "line": 1}],
                    required_checks=["spec_doc"])
    complete = {**partial, "status": "source_confirmed", "required_checks": ["spec_doc", "source"],
               "evidence": [*partial["evidence"], *_EVIDENCE]}
    steps = [
        {"thread_id": "TH-SRC-REQ",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": partial}},
         "agent_messages": [_final("資料の確認が済みました。")], "usage": helper._usage()},
        {"thread_id": "TH-SRC-REQ",
         "ledger": {"items": {"a": complete}},
         "agent_messages": [_final("ソースも確認しました。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_src_required")
    env = helper._result_env(helper._run(A.CodexProvider(), helper._ctx("src-required", 31101)))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2, f"範囲にソースがあるので spec_only だけでは受理しないはず: {calls!r}"
    assert "未充足: a（source が未確認）" in calls[1][-1]
    assert env["investigation"]["complete"] is True


def test_ledger_spec_only_completes_when_layer_excludes_source(tmp_path, monkeypatch):
    """探す対象の層を docs に絞ったターンは、範囲に本当はソースがあっても Sherpa は source を
    完了判定の必須種別に足さない——spec_only のまま1 attempt で受理する。"""
    steps = [
        {"thread_id": "TH-DOCS-ONLY",
         "ledger": {"manifest": _manifest(["a"]),
                    "items": {"a": _item("a", "spec_only",
                                         evidence=[{"kind": "spec_doc", "path": "docs/x.md", "line": 1}],
                                         required_checks=["spec_doc"])}},
         "agent_messages": [_final("資料で確認しました。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_docs_only_layer")
    ctx = helper._ctx("docs-only-layer", 31102)
    ctx.scope_meta = {"layer": "docs"}

    env = helper._result_env(helper._run(A.CodexProvider(), ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1, f"層が docs のターンは source を要求せず1回で受理するはず: {calls!r}"
    assert env["investigation"]["complete"] is True


def test_ledger_source_required_extra_follows_effective_layer_not_scope_meta_layer(monkeypatch):
    """`_ledger_source_required_extra` は MCP へ実際に渡す実効の層（呼び出し側の `_layer`・qa 以外
    は層なし＝`None`）を見る——author レンズは scope_meta.layer に関わらず層なしで MCP 探索する
    ため、scope_meta.layer="docs" な author 依頼でも実際にはソースを探索できる＝必須にする。
    層そのものが docs（qa・層=docs）なら、走査が判定不能（`None`）でも安全側（必須）へは倒さない
    ——MCP のソース読取自体が層で拒否されるため、判定不能を理由に解決できない催促を出し続けない。"""
    assert PV._ledger_source_required_extra("v1", [], None) == ("source",)
    monkeypatch.setattr(PV, "_scope_evidence_kinds", lambda *a, **k: None)
    assert PV._ledger_source_required_extra("v1", [], "docs") == ()


# ===== 1. 台帳が complete で final → 1 attempt で受理 =====

def test_ledger_complete_on_first_attempt_accepts_without_extra_continuation(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-L1",
         "ledger": {"manifest": _manifest(["a"]),
                    "items": {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("結論です。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_complete")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-complete", conversation_id=30001)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1, f"台帳が最初から complete なら1回で受理のはず: {calls!r}"
    assert env["headline"] == "結論です。"
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["continuations"] == 0
    assert env["investigation"]["stopped_reason"] == "complete"
    assert env["limits"]["ledger_incomplete"] is False


# ===== 2. 1 attempt目は非終端1件 → 継続プロンプトに id が含まれる → 2 attempt目で終端 → 受理 =====

def test_ledger_incomplete_item_triggers_one_ledger_continuation(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-L2",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [_final("途中です。")], "usage": helper._usage()},
        {"thread_id": "TH-L2",
         "ledger": {"items": {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("確定しました。")], "usage": helper._usage(30, 2, 13, 3)},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_one_continue")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-one-continue", conversation_id=30002)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2, f"非終端1件→終端で2回のはず: {calls!r}"
    assert "a" in calls[1][-1], f"継続プロンプトに未完了 id が含まれない: {calls[1][-1]!r}"
    assert "resume" in calls[1] and "TH-L2" in calls[1]
    assert env["headline"] == "確定しました。"
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["continuations"] == 1
    assert env["investigation"]["stopped_reason"] == "complete"


# ===== 3. 非終端が2 attempt続けて変化なし → stopped_reason=no_progress で受理 =====

def test_ledger_no_progress_twice_stops_and_accepts(tmp_path, monkeypatch):
    stalled_step = {"thread_id": "TH-L3",
                    "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
                    "agent_messages": [_final("途中です。")], "usage": helper._usage()}
    steps = [stalled_step, dict(stalled_step), dict(stalled_step)]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_no_progress")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-no-progress", conversation_id=30004)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 3, f"初回＋無進捗2回で打ち切るはず: {calls!r}"
    assert env["investigation"]["complete"] is False
    assert env["investigation"]["stopped_reason"] == "no_progress"
    assert env["investigation"]["continuations"] == 2
    assert env["limits"]["ledger_incomplete"] is True


# ===== 4. 継続10回で stopped_reason=cap（各回で進捗があっても母集団が伸び続ければ cap で止まる） =====

def test_ledger_cap_after_ten_continuations(tmp_path, monkeypatch):
    """RV 中-1（5巡目是正）で進捗判定を「非終端集合が縮んだか」に統一したため、`reason` だけを
    変えて非終端のまま据え置く旧方式は2回で no_progress になる——この cap テストは毎回1件
    終端化しつつ新しい item を追加する（母集団が伸び続けて never complete）設計で、無進捗にせず
    cap（10）で打ち切られることを固定する。"""
    steps = [{"thread_id": "TH-L4",
             "ledger": {"manifest": _manifest(["a0"]), "items": {"a0": _item("a0", "pending")}},
             "agent_messages": [_final("初期報告です。")], "usage": helper._usage()}]
    for i in range(1, 11):
        steps.append({
            "thread_id": "TH-L4",
            "ledger": {"manifest": _manifest([f"a{j}" for j in range(i + 1)]),
                      "items": {f"a{i - 1}": _item(f"a{i - 1}", "source_confirmed", evidence=_EVIDENCE),
                                f"a{i}": _item(f"a{i}", "pending")}},
            "agent_messages": [_final(f"途中{i}です。")], "usage": helper._usage()})
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_cap")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-cap", conversation_id=30005)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 11, f"初回＋台帳継続10回（各回で進捗があっても cap で止まる）はず: {calls!r}"
    assert env["investigation"]["complete"] is False
    assert env["investigation"]["stopped_reason"] == "cap"
    assert env["investigation"]["continuations"] == 10
    assert env["limits"]["ledger_incomplete"] is True


# ===== 5. manifest 無し → 1回継続 → まだ無し → 受理・stopped_reason=ledger_missing =====

def test_ledger_missing_manifest_retries_once_then_accepts(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-L5", "agent_messages": [_final("結論A。")], "usage": helper._usage()},
        {"thread_id": "TH-L5", "agent_messages": [_final("結論A再。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_missing")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-missing", conversation_id=30006)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2, f"manifest 無しは1回だけ継続して受理するはず: {calls!r}"
    assert calls[1][-1] == PV._LEDGER_MANIFEST_MISSING_PROMPT
    assert env["investigation"]["complete"] is False
    assert env["investigation"]["manifest_invalid"] is True
    assert env["investigation"]["stopped_reason"] == "ledger_missing"
    assert env["investigation"]["continuations"] == 1


# ===== 6. 退避（非永続では退避しない・永続で incomplete/complete） =====

def test_no_retire_without_conversation_id(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-L6A",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [_final("途中です。")], "usage": helper._usage()},
    ]
    users_dirname = "users_ledger_no_conv"
    _setup(tmp_path, monkeypatch, steps, users_dirname=users_dirname)
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-no-conv", conversation_id=None)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["investigation"]["complete"] is False
    assert env["investigation"]["retained"] is False
    sessions_root = tmp_path / users_dirname / "ledger-no-conv" / "workspace" / ".codex-sessions"
    assert not sessions_root.exists(), "conversation_id 無しなのに .codex-sessions が作られている"


def test_retire_creates_backup_when_incomplete(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-L6B",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [_final("途中です。")], "usage": helper._usage()},
    ]
    users_dirname = "users_ledger_retain"
    _setup(tmp_path, monkeypatch, steps, users_dirname=users_dirname)
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-retain", conversation_id=30007)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["investigation"]["complete"] is False
    assert env["investigation"]["retained"] is True
    retire_dir = (tmp_path / users_dirname / "ledger-retain" / "workspace"
                 / ".codex-sessions" / "30007" / "investigation")
    assert (retire_dir / "manifest.json").is_file(), "未完了台帳が退避先へコピーされていない"
    assert (retire_dir / "items" / "a.json").is_file()


def test_no_retire_backup_when_complete(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-L6C",
         "ledger": {"manifest": _manifest(["a"]),
                    "items": {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("結論です。")], "usage": helper._usage()},
    ]
    users_dirname = "users_ledger_no_retain"
    _setup(tmp_path, monkeypatch, steps, users_dirname=users_dirname)
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-no-retain", conversation_id=30008)

    env = helper._result_env(helper._run(prov, ctx))

    assert env["investigation"]["complete"] is True
    assert env["investigation"]["retained"] is False
    retire_dir = (tmp_path / users_dirname / "ledger-no-retain" / "workspace"
                 / ".codex-sessions" / "30008" / "investigation")
    assert not retire_dir.exists(), "complete な台帳が退避されてしまった"


# ===== 7. 「続き」で復元・それ以外で削除 =====

def test_restore_ledger_when_message_starts_with_continue_prefix(tmp_path, monkeypatch):
    users_dirname = "users_ledger_restore"
    uid = "ledger-restore"
    conv_id = 30009
    steps_run1 = [
        {"thread_id": "TH-L7A",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [_final("途中です。")], "usage": helper._usage()},
    ]
    _setup(tmp_path, monkeypatch, steps_run1, users_dirname=users_dirname)
    prov = A.CodexProvider()
    env1 = helper._result_env(helper._run(prov, helper._ctx(
        uid=uid, conversation_id=conv_id, message="通常の依頼です")))
    assert env1["investigation"]["complete"] is False
    assert env1["investigation"]["retained"] is True

    # 2ターン目: manifest を渡さず item だけ終端化する——「続き」で復元されていなければ
    # manifest_invalid のまま（1回継続して ledger_missing）になり、区別できる。
    steps_run2 = [
        {"thread_id": "TH-L7B",
         "ledger": {"items": {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("完了しました。")], "usage": helper._usage()},
    ]
    argv_log2 = _setup(tmp_path, monkeypatch, steps_run2, users_dirname=users_dirname,
                       argv_log_name="argv2.log")
    env2 = helper._result_env(helper._run(prov, helper._ctx(
        uid=uid, conversation_id=conv_id, message="続きをお願いします")))

    calls2 = helper._read_argv_log(argv_log2)
    assert len(calls2) == 1, f"復元されていれば manifest 既存＝1回で complete のはず: {calls2!r}"
    assert env2["investigation"]["restored"] is True
    assert env2["investigation"]["complete"] is True


def test_retired_ledger_deleted_when_message_does_not_start_with_continue_prefix(tmp_path, monkeypatch):
    users_dirname = "users_ledger_no_restore"
    uid = "ledger-no-restore"
    conv_id = 30010
    steps_run1 = [
        {"thread_id": "TH-L7C",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [_final("途中です。")], "usage": helper._usage()},
    ]
    _setup(tmp_path, monkeypatch, steps_run1, users_dirname=users_dirname)
    prov = A.CodexProvider()
    env1 = helper._result_env(helper._run(prov, helper._ctx(
        uid=uid, conversation_id=conv_id, message="通常の依頼です")))
    assert env1["investigation"]["retained"] is True

    # 2ターン目: 「続き」で始まらない＝退避台帳は復元されず削除される。新規の完結した台帳
    # （別 item id）を渡し、1回で complete するかどうかで復元有無を判別する。
    steps_run2 = [
        {"thread_id": "TH-L7D",
         "ledger": {"manifest": _manifest(["b"]),
                    "items": {"b": _item("b", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("別件、完了しました。")], "usage": helper._usage()},
    ]
    _setup(tmp_path, monkeypatch, steps_run2, users_dirname=users_dirname, argv_log_name="argv2.log")
    env2 = helper._result_env(helper._run(prov, helper._ctx(
        uid=uid, conversation_id=conv_id, message="別件をお願いします")))

    assert env2["investigation"]["restored"] is False
    assert env2["investigation"]["complete"] is True
    retire_dir = (tmp_path / users_dirname / uid / "workspace"
                 / ".codex-sessions" / str(conv_id) / "investigation")
    assert not retire_dir.exists(), "「続き」で始まらないのに退避台帳が残っている（削除も complete 退避もされていない）"


# ===== 8. _schema_v2 無効ではゲートが効かない（final をそのまま受理） =====

def test_gate_inactive_when_schema_disabled(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-L8",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": ["確認した結果、影響はありません。"], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_schema_off")
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-schema-off", conversation_id=30011)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1, f"schema_v2 無効なら台帳が未完了でもゲートは効かないはず: {calls!r}"
    assert env["headline"] == "確認した結果、影響はありません。"
    assert not any("--output-schema" in c for c in calls)


# ===== RV是正（2026-09-22）: 台帳ゲート統合の敵対レビュー3件 =====
#
# [高-1] 台帳ゲートが拒否した final の後に in_progress が返ると、拒否したはずの古い final が
#        採用されてしまうバグ（自動継続の判定ループへ戻さず、`_structured_answers` から拾い直す
#        `_pick_structured_headline`/`_pick_structured_claims` が古い final を再選出していた）。
# [中-2] 項目が1件ずつ着実に終端化していても「残った非終端が毎回一致」を無進捗と誤検知し、
#        2 attempt で打ち切っていたバグ。
# [高-3] `_attempt()` 内の yield で generator が close されると判定行を通らず、未完了台帳が
#        退避されないまま run_dir ごと削除されるバグ。

def test_stale_rejected_final_is_not_returned_after_ledger_continuation_goes_in_progress(
        tmp_path, monkeypatch):
    """[高-1] 台帳ゲートが1回目の final を拒否して継続させた attempt が `in_progress` で終わっても、
    受理される最終回答は最後の final（台帳が完了した時点の final）であり、拒否済みの最初の final
    ではない。ツールを1つも呼ばない自動継続 attempt が `final` に届いた場合でも打ち切らず台帳ゲート
    へ制御を戻すことも合わせて検証する（前段の自動継続分岐の副作用）。"""
    steps = [
        {"thread_id": "TH-STALE",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [_final("第一final・まだ未完了です。")], "usage": helper._usage()},
        {"thread_id": "TH-STALE",
         "agent_messages": [json.dumps(
             {"status": "in_progress", "answer": "作業中です。", "next_step": "続けます", "claims": []},
             ensure_ascii=False)],
         "usage": helper._usage()},
        {"thread_id": "TH-STALE",
         "ledger": {"items": {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("最終結論です。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_stale_final")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-stale-final", conversation_id=30012)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 3, f"final(拒否)→in_progress→final(受理)で3回のはず: {calls!r}"
    assert env["headline"] == "最終結論です。", (
        f"拒否済みの古い final が受理されている（RV高-1 未是正）: {env['headline']!r}")
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["continuations"] == 1


def test_ledger_progress_resets_streak_when_any_item_terminalizes_each_round(tmp_path, monkeypatch):
    """[中-2] a→b→c と1件ずつ終端化していく3回の継続では、残った非終端集合が毎回変わらなくても
    （＝旧判定は無進捗と誤検知していた）打ち切られず、全件終端化した時点で complete として受理する。"""
    steps = [
        {"thread_id": "TH-PROGRESS",
         "ledger": {"manifest": _manifest(["a", "b", "c"]),
                    "items": {"a": _item("a", "pending"), "b": _item("b", "pending"),
                              "c": _item("c", "pending")}},
         "agent_messages": [_final("初期報告です。")], "usage": helper._usage()},
        {"thread_id": "TH-PROGRESS",
         "ledger": {"items": {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("aを確認しました。")], "usage": helper._usage()},
        {"thread_id": "TH-PROGRESS",
         "ledger": {"items": {"b": _item("b", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("bを確認しました。")], "usage": helper._usage()},
        {"thread_id": "TH-PROGRESS",
         "ledger": {"items": {"c": _item("c", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("全て確認しました。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_progress")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-progress", conversation_id=30013)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 4, (
        f"1件ずつ終端化する3回の継続（初回+3）で complete に届くはず（無進捗誤検知で打ち切られて"
        f"いないか）: {calls!r}")
    assert env["headline"] == "全て確認しました。"
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["stopped_reason"] == "complete"
    assert env["investigation"]["continuations"] == 3


# ---- [高-3] generator close で未完了台帳が失われず退避される ----
# `tests/unit/test_codex_kill_timeout.py` の「同じスレッド・yield 点で close する」流儀を踏襲する
# （実行中の generator を別スレッドから close する未定義動作は踏まない）。

_FAKE_CODEX_CLOSE_PY = r'''#!/usr/bin/env python3
import json
import pathlib
import sys
import time

args = sys.argv[1:]
run_dir = pathlib.Path(args[args.index("-C") + 1])
inv_dir = run_dir / ".tmp" / "investigation"
items_dir = inv_dir / "items"
items_dir.mkdir(parents=True, exist_ok=True)
(inv_dir / "manifest.json").write_text(json.dumps(
    {"question_kind": "list", "created_at": "2026-09-22T00:00:00Z", "items": ["a"]},
    ensure_ascii=False), encoding="utf-8")
(items_dir / "a.json").write_text(json.dumps(
    {"id": "a", "kind": "row", "subject": "対象", "required_checks": [],
     "evidence": [], "status": "pending", "reason": "", "owner": "parent"},
    ensure_ascii=False), encoding="utf-8")

print(json.dumps({"type": "thread.started", "thread_id": "TH-CLOSE"}))
sys.stdout.flush()
print(json.dumps({"type": "item.completed", "item": {"id": "c1", "type": "command_execution",
                   "command": "ls", "status": "completed", "exit_code": 0}}))
sys.stdout.flush()
time.sleep(120)
'''


def test_generator_close_retires_incomplete_ledger_without_relying_on_verdict(tmp_path, monkeypatch):
    """[高-3] 永続会話で台帳作成後、`_attempt()` 内の yield（command_execution node）で generator
    を close すると、本体ループの判定行（`_investigation_verdict` を埋める箇所）には一度も到達
    しない。それでも outer finally が台帳ディレクトリを都度読み直して退避することを確認する。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(_FAKE_CODEX_CLOSE_PY)
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    users_dirname = "users_ledger_close"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))

    prov = A.CodexProvider()
    uid = "ledger-close-u1"
    conv_id = 30014
    ctx = helper._ctx(uid=uid, conversation_id=conv_id)

    gen = prov.run(ctx)
    seen: list = []
    for _ in range(20):
        ev = next(gen)
        seen.append(ev)
        if isinstance(ev, dict) and str(ev.get("id", "")).startswith("cx-"):
            break
    else:
        raise AssertionError(f"command_execution node（cx-*）に到達しなかった。seen={seen!r}")

    gen.close()   # クライアント切断相当（本体の finally が判定行を経由せず走る）

    retire_dir = (tmp_path / users_dirname / uid / "workspace"
                 / ".codex-sessions" / str(conv_id) / "investigation")
    assert (retire_dir / "manifest.json").is_file(), (
        "generator close で未完了台帳が退避されていない（RV高-3 未是正）")
    assert (retire_dir / "items" / "a.json").is_file()


# ===== RV是正（2026-09-22 2巡目）: 退避が会話ロックの外にある =====

def test_retire_directory_is_either_absent_or_complete_and_lock_released_after_run(
        tmp_path, monkeypatch):
    """[中-1] 台帳の退避・削除が終わるまで会話ロックを保持し、退避のコピーは一時ディレクトリ→
    `os.replace` の原子置換にした。(a) 退避先が存在するなら manifest と全 item が揃っている
    （コピー途中の中間状態が外から観測されない・一時ディレクトリも残らない）。(b) ターン終了直後に
    会話ロックが解放されている（別スレッドが `blocking=False` で取得できる＝退避処理をロックの
    内側へ移したことで新たにデッドロックしていないことの確認も兼ねる）。"""
    from sherpa.providers.codex import provider as PV

    steps = [
        {"thread_id": "TH-RETIRE-ATOMIC",
         "ledger": {"manifest": _manifest(["a", "b"]),
                    "items": {"a": _item("a", "pending"), "b": _item("b", "pending")}},
         "agent_messages": [_final("途中です。")], "usage": helper._usage()},
    ]
    users_dirname = "users_ledger_retire_atomic"
    _setup(tmp_path, monkeypatch, steps, users_dirname=users_dirname)
    prov = A.CodexProvider()
    conv_id = 30015
    env = helper._result_env(helper._run(prov, helper._ctx(
        uid="ledger-retire-atomic", conversation_id=conv_id)))

    assert env["investigation"]["complete"] is False
    assert env["investigation"]["retained"] is True

    retire_dir = (tmp_path / users_dirname / "ledger-retire-atomic" / "workspace"
                 / ".codex-sessions" / str(conv_id) / "investigation")
    # (a) 退避先が存在するなら manifest と全 item が揃っている（途中状態が観測されない）。
    assert retire_dir.is_dir()
    assert (retire_dir / "manifest.json").is_file()
    assert (retire_dir / "items" / "a.json").is_file()
    assert (retire_dir / "items" / "b.json").is_file()
    # 原子置換用の一時ディレクトリが残っていない（swap 後は消えているはず）。
    leftover_tmp = list(retire_dir.parent.glob(".investigation.tmp-*"))
    assert leftover_tmp == [], f"退避の一時ディレクトリが残っている: {leftover_tmp!r}"

    # (b) ターン終了直後に会話ロックが解放されている。
    lk = PV._conversation_lock(conv_id)
    assert lk.acquire(blocking=False), "ターン終了後も会話ロックが解放されていない（RV中-1 未是正）"
    lk.release()


# ===== RV是正（2026-09-22 3巡目）: ゲート未通過final・manifest破損台帳の消失・retainedの実態不一致 =====

def test_unevaluated_final_in_same_attempt_is_not_adopted_when_ledger_incomplete(tmp_path, monkeypatch):
    """[高-1] 同一 attempt が final の直後に in_progress を出すと `_latest_structured` は
    in_progress になるが、その final は台帳ゲートを一度も通っていない（`_latest_structured` だけを
    見る旧トリガーでは見逃す）。その後の台帳継続・自動継続がいずれも in_progress かつツール未実行の
    まま終わっても、未評価の final が受理されないことを固定する。"""
    steps = [
        {"thread_id": "TH-UNEVAL",
         "agent_messages": [
             _final("第一final・まだ未完了です。"),
             json.dumps({"status": "in_progress", "answer": "これから確認します。",
                        "next_step": "続けます", "claims": []}, ensure_ascii=False),
         ],
         "usage": helper._usage()},
        {"thread_id": "TH-UNEVAL",
         "agent_messages": [json.dumps(
             {"status": "in_progress", "answer": "台帳の催促後も作業中です。",
              "next_step": "続けます", "claims": []}, ensure_ascii=False)],
         "usage": helper._usage()},
        {"thread_id": "TH-UNEVAL",
         "agent_messages": [json.dumps(
             {"status": "in_progress", "answer": "自動継続後も作業中です。",
              "next_step": "続けます", "claims": []}, ensure_ascii=False)],
         "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_unevaluated_final")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-unevaluated-final", conversation_id=30017)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 3, (
        f"初回（final→in_progress）→台帳催促→自動継続（ツール未実行）で打ち切るはず: {calls!r}")
    assert env["headline"] != "第一final・まだ未完了です。", (
        f"台帳ゲートを一度も通っていない final が採用されている（RV高-1 3巡目 未是正）: {env!r}")
    assert env["headline"] == "自動継続後も作業中です。"
    assert env.get("codex_stopped_early") is True
    assert env["investigation"]["complete"] is False
    assert env["investigation"]["manifest_invalid"] is True
    assert env["investigation"]["stopped_reason"] == "ledger_missing"


def test_retire_moves_incomplete_ledger_with_broken_manifest_but_items_present(tmp_path):
    """[高-2] manifest ファイルが無くても items 配下に1件でもファイルがあれば退避する
    （`ledger_complete()` の `manifest_invalid` だけで判定すると、manifest が壊れている・欠落して
    いるだけで items にデータがある台帳が退避されず失われていた）。直接 `_retire_investigation_ledger`
    を呼ぶ単体テスト。"""
    from sherpa.providers.codex import provider as PV

    investigation_dir = tmp_path / "investigation"
    (investigation_dir / "items").mkdir(parents=True)
    (investigation_dir / "items" / "a.json").write_text(
        json.dumps(_item("a", "pending"), ensure_ascii=False), encoding="utf-8")
    ledger_home = tmp_path / "ledger_home"
    ledger_home.mkdir()

    retained = PV._retire_investigation_ledger(investigation_dir, ledger_home)

    assert retained is True, "manifest 無し・items ありの台帳が退避されていない"
    retire_dir = ledger_home / "investigation"
    assert (retire_dir / "items" / "a.json").is_file()
    assert not (retire_dir / "manifest.json").exists()


def test_retire_skips_truly_unused_empty_investigation_dir(tmp_path):
    """[高-2] manifest ファイルも items ファイルも無い（一度も使われていない）空ディレクトリは
    退避しない。"""
    from sherpa.providers.codex import provider as PV

    investigation_dir = tmp_path / "investigation"
    (investigation_dir / "items").mkdir(parents=True)
    ledger_home = tmp_path / "ledger_home"
    ledger_home.mkdir()

    retained = PV._retire_investigation_ledger(investigation_dir, ledger_home)

    assert retained is False
    assert not (ledger_home / "investigation").exists()


def test_retained_reflects_actual_copy_failure_in_envelope(tmp_path, monkeypatch):
    """[中-3] コピーに失敗した場合、`env["investigation"]["retained"]` は実際の成否（False）を
    反映する——退避前に式で予測した値ではなく、退避処理を先に実行してから実態を記録する。"""
    from sherpa.providers.codex import provider as PV

    steps = [
        {"thread_id": "TH-RETAIN-FAIL",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [_final("途中です。")], "usage": helper._usage()},
    ]
    users_dirname = "users_ledger_retain_fail"
    _setup(tmp_path, monkeypatch, steps, users_dirname=users_dirname)

    # RV高-1（6巡目）でコピー実装を copytree から選択的コピー（copy2 ベース）へ変更したため、
    # ここも copy2 を落とす（`_copy_investigation_contract_files` が実際に使う関数）。
    def _boom_copy2(*_a, **_k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(PV.shutil, "copy2", _boom_copy2)

    prov = A.CodexProvider()
    conv_id = 30018
    env = helper._result_env(helper._run(prov, helper._ctx(
        uid="ledger-retain-fail", conversation_id=conv_id)))

    assert env["investigation"]["complete"] is False
    assert env["investigation"]["retained"] is False, (
        "コピー失敗なのに retained=True と記録されている（RV中-3 未是正）")


# ===== RV是正（2026-09-22 4巡目）: 自動継続内の未評価final・manifest内容不正の扱い =====

def test_final_in_auto_continue_without_tools_still_routes_to_ledger_gate(tmp_path, monkeypatch):
    """[高-1] 初回 in_progress → 自動継続の attempt がツール未実行のまま final→in_progress を
    同一 attempt 内で出しても、final 候補（`_candidate_final()`）がある限り「ツール未実行なら
    打ち切る」を適用せず、ループ先頭の台帳ゲートへ戻す——未評価の final が採用されず、台帳継続が
    少なくとも1回発行されることを固定する。"""
    steps = [
        {"thread_id": "TH-AC-UNEVAL",
         "agent_messages": [json.dumps(
             {"status": "in_progress", "answer": "まず確認します。", "next_step": "続けます",
              "claims": []}, ensure_ascii=False)],
         "usage": helper._usage()},
        {"thread_id": "TH-AC-UNEVAL",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [
             _final("第一final・まだ未完了です。"),
             json.dumps({"status": "in_progress", "answer": "やっぱり続けます。",
                        "next_step": "続けます", "claims": []}, ensure_ascii=False),
         ],
         "usage": helper._usage()},
        {"thread_id": "TH-AC-UNEVAL",
         "agent_messages": [json.dumps(
             {"status": "in_progress", "answer": "台帳継続後も作業中です。", "next_step": "続けます",
              "claims": []}, ensure_ascii=False)],
         "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_ac_uneval")
    # 自動継続の枠を1に固定する——枠が残っていると、台帳継続（3回目）の後に無進捗のまま
    # もう1回自動継続が発行されてしまい（それ自体は正しい挙動）、本テストが確認したい
    # 「final候補があるうちは打ち切らない」の検証点（3回目が台帳継続であること）がぼやける。
    monkeypatch.setenv("SHERPA_CODEX_AUTO_CONTINUE", "1")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-ac-uneval", conversation_id=30019)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 3, f"初回→自動継続(final混入)→台帳継続で3回のはず: {calls!r}"
    assert "a" in calls[2][-1], f"3回目が台帳継続プロンプトになっていない: {calls[2][-1]!r}"
    assert env["headline"] != "第一final・まだ未完了です。", (
        f"ゲート未評価の final が採用されている（RV高-1 4巡目 未是正）: {env!r}")
    assert env["investigation"]["complete"] is False
    assert env["investigation"]["continuations"] >= 1


def test_manifest_invalid_content_is_repaired_via_cap_budget_then_succeeds(tmp_path, monkeypatch):
    """[中-2] manifest.json が存在するが必須キー（`created_at`）を欠く場合、「未作成」の1回だけの
    催促ではなく、通常の台帳継続と同じ枠（cap）で修復を促す。修復（有効な manifest への書き直し）
    後の final で受理されることを固定する。"""
    log = tmp_path / "mcp.jsonl"
    steps = [
        {"thread_id": "TH-REPAIR-OK",
         "ledger": {"manifest": {"question_kind": "list", "items": ["a"]}},   # created_at 欠落＝内容不正
         "agent_messages": [_final("第一final・まだ未完了です。")], "usage": helper._usage()},
        # 修復は MCP の台帳ツール経由で行う（モデルに許された唯一の書込手段＝直接ファイルを書く形の
        # テストでは「内容不正の既存 manifest をツールが拒否する」回帰を検知できない）。
        {"thread_id": "TH-REPAIR-OK", "mcp_log": str(log),
         "ledger_tools": [
             {"name": "ledger_manifest_set", "arguments": {"question_kind": "list", "items": ["a"]}},
             {"name": "ledger_item_put", "arguments": _item("a", "source_confirmed", evidence=_EVIDENCE)},
         ],
         "agent_messages": [_final("修復して完了しました。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_manifest_repair_ok")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-manifest-repair-ok", conversation_id=30020)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 2, f"内容不正の manifest は修復催促1回で受理されるはず: {calls!r}"
    assert calls[1][-1] == PV._LEDGER_MANIFEST_INVALID_PROMPT, (
        f"内容不正の manifest への催促文言が「未作成」用と区別されていない: {calls[1][-1]!r}")
    assert env["headline"] == "修復して完了しました。"
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["continuations"] == 1


def test_manifest_invalid_content_never_repaired_stops_at_cap(tmp_path, monkeypatch):
    """[中-2] manifest.json が存在するが内容が規約に合わないまま修復されない場合、通常の台帳継続と
    同じ枠（cap=10）で打ち切り、`stopped_reason="cap"`・`limits.ledger_incomplete=True` を記録する
    （「未作成なら1回催促後に受理」と同じ扱いにはしない）。"""
    steps = [
        {"thread_id": "TH-REPAIR-CAP",
         "ledger": {"manifest": {"question_kind": "list", "items": ["a"]}},   # created_at 欠落＝内容不正
         "agent_messages": [_final("第一final・まだ未完了です。")], "usage": helper._usage()},
        {"thread_id": "TH-REPAIR-CAP",
         "agent_messages": [_final("まだ修復していません。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_manifest_repair_cap")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-manifest-repair-cap", conversation_id=30021)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 11, f"初回＋修復催促10回（cap）のはず: {calls!r}"
    assert env["investigation"]["complete"] is False
    assert env["investigation"]["stopped_reason"] == "cap"
    assert env["investigation"]["continuations"] == 10
    assert env["limits"]["ledger_incomplete"] is True


# ===== RV是正（2026-09-22 5巡目）: 自動継続中の進捗が streak に反映されない・cap 未判定の11回目 =====

def test_progress_during_auto_continue_resets_no_progress_streak(tmp_path, monkeypatch):
    """[中-1] 台帳継続（無更新・in_progress）→自動継続（a を終端化して final）→台帳継続（無更新）
    の順でも、自動継続中に生じた進捗（a の終端化）が streak をリセットするため、2回連続無進捗と
    誤検知されて打ち切られない——最終的に残りの項目（b）も終端化されて complete で受理されることを
    固定する。"""
    steps = [
        {"thread_id": "TH-PROGRESS-AC",
         "ledger": {"manifest": _manifest(["a", "b"]),
                    "items": {"a": _item("a", "pending"), "b": _item("b", "pending")}},
         "agent_messages": [_final("初期報告です。")], "usage": helper._usage()},
        {"thread_id": "TH-PROGRESS-AC",
         "agent_messages": [json.dumps({"status": "in_progress", "answer": "まだ確認中です。",
                                        "next_step": "続けます", "claims": []}, ensure_ascii=False)],
         "usage": helper._usage()},
        {"thread_id": "TH-PROGRESS-AC",
         "ledger": {"items": {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("aを確認した結果、影響ありません。")], "usage": helper._usage()},
        {"thread_id": "TH-PROGRESS-AC",
         "agent_messages": [json.dumps({"status": "in_progress", "answer": "またまだです。",
                                        "next_step": "続けます", "claims": []}, ensure_ascii=False)],
         "usage": helper._usage()},
        {"thread_id": "TH-PROGRESS-AC",
         "ledger": {"items": {"b": _item("b", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("全て確認しました。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_progress_ac")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-progress-ac", conversation_id=30022)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 5, (
        f"台帳継続→自動継続(進捗)→台帳継続→自動継続(進捗)→complete で5回のはず"
        f"（自動継続中の進捗が streak に反映されず無進捗2回と誤検知されていないか・RV中-1 "
        f"5巡目 未是正）: {calls!r}")
    assert env["headline"] == "全て確認しました。"
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["stopped_reason"] == "complete"


def test_manifest_missing_at_cap_boundary_stops_at_cap_without_extra_prompt(tmp_path, monkeypatch):
    """[中-2] 進捗を伴う台帳継続を10回（cap）重ねた末に manifest が消えて final が返っても、
    不存在分岐の「1回だけ催促」は発行されない——台帳起因の催促は発行前に共通の cap 判定を
    経由するため、continuations が既に10に達していれば manifest の状態にかかわらず cap で
    受理する。"""
    steps = [{"thread_id": "TH-CAP-MISSING",
             "ledger": {"manifest": _manifest(["a0"]), "items": {"a0": _item("a0", "pending")}},
             "agent_messages": [_final("初期報告です。")], "usage": helper._usage()}]
    for i in range(1, 10):
        steps.append({
            "thread_id": "TH-CAP-MISSING",
            "ledger": {"manifest": _manifest([f"a{j}" for j in range(i + 1)]),
                      "items": {f"a{i - 1}": _item(f"a{i - 1}", "source_confirmed", evidence=_EVIDENCE),
                                f"a{i}": _item(f"a{i}", "pending")}},
            "agent_messages": [_final(f"途中{i}です。")], "usage": helper._usage()})
    # 10回目の継続（このステップの発行で continuations は10に到達する）: 前項目を終端化しつつ
    # manifest 自体を消す——次の判定（11回目の可否）の時点で manifest_invalid（不存在）になる。
    steps.append({
        "thread_id": "TH-CAP-MISSING",
        "ledger": {"items": {"a9": _item("a9", "source_confirmed", evidence=_EVIDENCE)},
                  "delete_manifest": True},
        "agent_messages": [_final("最後の項目も確認しましたが、まだ残りがあるかもしれません。")],
        "usage": helper._usage()})
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_cap_missing_manifest")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-cap-missing-manifest", conversation_id=30023)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 11, (
        f"初回＋台帳継続10回で cap のはず（manifest 不存在の1回だけの催促が cap を無視して"
        f"11回目を発行していないか・RV中-2 5巡目 未是正）: {calls!r}")
    assert env["investigation"]["complete"] is False
    assert env["investigation"]["stopped_reason"] == "cap"
    assert env["investigation"]["continuations"] == 10
    assert env["limits"]["ledger_incomplete"] is True


# ===== RV是正（2026-09-22 6巡目）: symlink 経由の情報漏洩・終端化を伴わない進捗の無視 =====

def test_retire_refuses_when_items_dir_has_symlink(tmp_path):
    """[高-1] items/ 配下に symlink が1つでもあれば退避しない——model-shell は cwd
    （`.tmp/investigation/` 配下）に書けるため、model からは不可視のホスト側ファイルへの symlink
    を仕込める。Sherpa（親権限）がそれを辿って退避すると、model からは読めないファイル本文が
    実体化してしまう。直接 `_retire_investigation_ledger` を呼ぶ単体テスト。"""
    investigation_dir = tmp_path / "investigation"
    (investigation_dir / "items").mkdir(parents=True)
    (investigation_dir / "manifest.json").write_text(
        json.dumps(_manifest(["a"]), ensure_ascii=False), encoding="utf-8")
    (investigation_dir / "items" / "a.json").write_text(
        json.dumps(_item("a", "pending"), ensure_ascii=False), encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("model からは読めないはずの内容", encoding="utf-8")
    (investigation_dir / "items" / "leak.json").symlink_to(secret)
    ledger_home = tmp_path / "ledger_home"
    ledger_home.mkdir()

    retained = PV._retire_investigation_ledger(investigation_dir, ledger_home)

    assert retained is False, "symlink があるのに退避されている（RV高-1 6巡目 未是正）"
    assert not (ledger_home / "investigation").exists()


def test_retire_ignores_non_contract_files(tmp_path):
    """[高-1] items/ 配下の規約外ファイル（例: note.txt）・台帳ルート直下の規約外ディレクトリは
    退避に含まれない——退避対象は manifest.json と items/*.json だけに限定する。"""
    investigation_dir = tmp_path / "investigation"
    (investigation_dir / "items").mkdir(parents=True)
    (investigation_dir / "manifest.json").write_text(
        json.dumps(_manifest(["a"]), ensure_ascii=False), encoding="utf-8")
    (investigation_dir / "items" / "a.json").write_text(
        json.dumps(_item("a", "pending"), ensure_ascii=False), encoding="utf-8")
    (investigation_dir / "items" / "note.txt").write_text("本文混入テスト", encoding="utf-8")
    (investigation_dir / "stray_dir").mkdir()
    (investigation_dir / "stray_dir" / "x.json").write_text("{}", encoding="utf-8")
    ledger_home = tmp_path / "ledger_home"
    ledger_home.mkdir()

    retained = PV._retire_investigation_ledger(investigation_dir, ledger_home)

    assert retained is True
    retire_dir = ledger_home / "investigation"
    assert (retire_dir / "manifest.json").is_file()
    assert (retire_dir / "items" / "a.json").is_file()
    assert not (retire_dir / "items" / "note.txt").exists(), "規約外ファイルが退避に含まれている"
    assert not (retire_dir / "stray_dir").exists(), "規約外ディレクトリが退避に含まれている"


def test_restore_refuses_and_deletes_retired_dir_with_symlink(tmp_path, monkeypatch):
    """[高-1] 退避先（前ターンの退避）に symlink が仕込まれていた場合、「続き」宣言でも復元せず、
    汚染された退避先ごと削除する。"""
    users_dirname = "users_ledger_symlink_restore"
    uid = "ledger-symlink-restore"
    conv_id = 30025
    retire_dir = (tmp_path / users_dirname / uid / "workspace"
                 / ".codex-sessions" / str(conv_id) / "investigation")
    (retire_dir / "items").mkdir(parents=True)
    (retire_dir / "manifest.json").write_text(
        json.dumps(_manifest(["a"]), ensure_ascii=False), encoding="utf-8")
    secret = tmp_path / "secret2.txt"
    secret.write_text("model からは読めないはずの内容2", encoding="utf-8")
    (retire_dir / "items" / "leak.json").symlink_to(secret)

    # このターン自身は1回で complete させる（無関係な新規退避で assert が汚れないようにする）。
    steps = [
        {"thread_id": "TH-SYMLINK-RESTORE",
         "ledger": {"manifest": _manifest(["b"]),
                    "items": {"b": _item("b", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("続きの調査です。")], "usage": helper._usage()},
    ]
    _setup(tmp_path, monkeypatch, steps, users_dirname=users_dirname)
    prov = A.CodexProvider()
    env = helper._result_env(helper._run(prov, helper._ctx(
        uid=uid, conversation_id=conv_id, message="続きをお願いします")))

    assert env["investigation"]["restored"] is False, (
        "symlink 入り退避先が復元されている（RV高-1 6巡目 未是正）")
    assert env["investigation"]["complete"] is True
    assert not retire_dir.exists(), "symlink 入り退避先が削除されずに残っている"


def test_field_level_progress_without_terminalizing_resets_no_progress_streak(tmp_path, monkeypatch):
    """[中-2] 単一 item が pending→in_progress→evidence 追加と進む（非終端集合自体は縮まない）
    3 attempt でも、`no_progress()` が非終端の全 id を返さない（＝status/evidence が変わった）
    限り streak がリセットされ、無進捗2回と誤検知されて打ち切られないことを固定する。"""
    steps = [
        {"thread_id": "TH-FIELD-PROGRESS",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [_final("初期報告です。")], "usage": helper._usage()},
        {"thread_id": "TH-FIELD-PROGRESS",
         "ledger": {"items": {"a": _item("a", "in_progress")}},
         "agent_messages": [_final("状態を更新しました。")], "usage": helper._usage()},
        {"thread_id": "TH-FIELD-PROGRESS",
         "ledger": {"items": {"a": _item("a", "in_progress", evidence=_EVIDENCE)}},
         "agent_messages": [_final("根拠を追加しました。")], "usage": helper._usage()},
        {"thread_id": "TH-FIELD-PROGRESS",
         "ledger": {"items": {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("確認完了です。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_field_progress")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-field-progress", conversation_id=30026)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 4, (
        f"pending→in_progress→evidence追加→terminal の4回で complete のはず（field-level進捗が"
        f"streak に反映されず無進捗2回と誤検知されていないか・RV中-2 6巡目 未是正）: {calls!r}")
    assert env["headline"] == "確認完了です。"
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["continuations"] == 3


# ===== RV是正（2026-09-22 7巡目）: `_ledger_progressed` 純関数の直接テスト =====
#
# [中-1] 欠落（missing・登録済みだがファイル未作成）item が終端化しても、非終端集合だけの比較
#        では「縮んだ」と判定されない——未解決集合（非終端∪欠落∪無効）で比較する。
# [中-2] 未登録 item が no_progress() の戻りに混ざると、登録済み item が無進捗でも「一致しない」
#        と誤認して常に進捗ありと判定してしまう——no_progress() の結果を登録済み id に限定する。

def _snapshot(manifest_items: list, items: dict, invalid_ids: tuple = ()) -> "IL.LedgerSnapshot":
    return IL.LedgerSnapshot(
        manifest=_manifest(manifest_items) if manifest_items is not None else None,
        items=items, invalid_ids=invalid_ids)


def test_ledger_progressed_true_when_missing_item_gets_created():
    """[中-1] 登録済みだがファイル未作成（missing）だった item にファイルが作られると
    （一度も非終端集合に含まれないまま解決した場合でも）進捗ありと判定する。"""
    prev = _snapshot(["a", "b", "c", "d"], {})
    curr = _snapshot(["a", "b", "c", "d"], {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)})
    assert PV._ledger_progressed(prev, curr) is True


def test_ledger_progressed_false_when_only_unregistered_item_changes():
    """[中-2] manifest に登録されていない item が pending のまま混在していても、登録済み item に
    変化が無ければ進捗なしと判定する（no_progress() の戻りが未登録 item でノイズにならない）。"""
    prev = _snapshot(["a"], {"a": _item("a", "pending"), "b": _item("b", "pending")})
    curr = _snapshot(["a"], {"a": _item("a", "pending"), "b": _item("b", "pending")})
    assert PV._ledger_progressed(prev, curr) is False


def test_ledger_progressed_true_when_one_registered_item_terminalizes():
    """既存シナリオ（1件ずつ終端化）の純関数レベルの固定: 登録済み item が1件終端化すれば
    進捗ありと判定する。"""
    prev = _snapshot(["a", "b"], {"a": _item("a", "pending"), "b": _item("b", "pending")})
    curr = _snapshot(["a", "b"], {"a": _item("a", "source_confirmed", evidence=_EVIDENCE),
                                  "b": _item("b", "pending")})
    assert PV._ledger_progressed(prev, curr) is True


def test_ledger_progressed_true_when_field_updates_without_terminalizing():
    """既存シナリオ（field-level 更新）の純関数レベルの固定: 終端化しなくても status が変われば
    進捗ありと判定する。"""
    prev = _snapshot(["a"], {"a": _item("a", "pending")})
    curr = _snapshot(["a"], {"a": _item("a", "in_progress")})
    assert PV._ledger_progressed(prev, curr) is True


def test_ledger_progressed_false_when_nothing_changes():
    """基準ケース: 登録済み item が完全に無変化なら進捗なしと判定する。"""
    prev = _snapshot(["a"], {"a": _item("a", "pending")})
    curr = _snapshot(["a"], {"a": _item("a", "pending")})
    assert PV._ledger_progressed(prev, curr) is False

    # required_extra で追加した種別（item 自身は required_checks に宣言していない）が理由で
    # 未充足になった item でも、`ledger_complete()`/`no_progress()` の両方へ同じ required_extra を
    # 渡せば無変化のまま進捗なしと判定する——片方にだけ足すと、無変化の item を「進捗あり」と
    # 誤判定する（無充足判定の基準が前後の比較でずれるため）。
    spec_only = _item("s", "spec_only", evidence=[{"kind": "spec_doc", "path": "docs/x.md", "line": 1}],
                      required_checks=["spec_doc"])
    prev_extra = _snapshot(["s"], {"s": spec_only})
    curr_extra = _snapshot(["s"], {"s": dict(spec_only)})
    assert IL.ledger_complete(curr_extra, required_extra=("source",)).unsatisfied == {"s": ("source",)}
    assert PV._ledger_progressed(prev_extra, curr_extra, required_extra=("source",)) is False


# ===== RV是正（2026-09-22 8巡目）: symlink 検査の抜け（列挙失敗の握りつぶし） =====
#
# 6巡目の `_investigation_tree_has_symlink` は `os.walk` の既定 `onerror=None`（列挙エラーを
# 無視する）に依存しており、`investigation/` を chmod 0o111（実行のみ・listdir 不可）にすると
# 列挙が黙って空になり、symlink を検出できずに退避・復元してしまっていた。

def test_retire_refuses_when_investigation_dir_unlistable_and_manifest_is_symlink(tmp_path):
    """[高-1] investigation/ を chmod 0o111（列挙不可・既知パスへの到達は可能）にして
    manifest.json を symlink にしても、`os.walk` の列挙失敗を握りつぶさず拒否する
    （retained=False）。root では chmod による列挙禁止が効かないため skip する。"""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root では chmod によるディレクトリ列挙禁止が効かない")
    investigation_dir = tmp_path / "investigation"
    (investigation_dir / "items").mkdir(parents=True)
    (investigation_dir / "items" / "a.json").write_text(
        json.dumps(_item("a", "pending"), ensure_ascii=False), encoding="utf-8")
    secret = tmp_path / "secret3.txt"
    secret.write_text("model からは読めないはずの内容3", encoding="utf-8")
    (investigation_dir / "manifest.json").symlink_to(secret)
    ledger_home = tmp_path / "ledger_home"
    ledger_home.mkdir()

    mode_before = investigation_dir.stat().st_mode
    investigation_dir.chmod(0o111)
    try:
        retained = PV._retire_investigation_ledger(investigation_dir, ledger_home)
    finally:
        investigation_dir.chmod(mode_before)   # cleanup で tmp_path の削除が権限エラーにならないよう戻す

    assert retained is False, (
        "列挙不可（chmod 0o111）でも symlink 拒否できていない（RV高-1 8巡目 未是正）")
    assert not (ledger_home / "investigation").exists()


def test_copy_investigation_contract_files_refuses_symlinked_manifest(tmp_path):
    """[高-1] `_copy_investigation_contract_files` はコピー直前に symlink を個別確認する——木の
    走査（`_investigation_tree_has_symlink`）に頼らず、この関数単体でも symlink を拒否する
    （多層防御・列挙権限に依存しない既知パスへの `lstat` だけで判定する）。"""
    src = tmp_path / "src"
    src.mkdir()
    secret = tmp_path / "secret4.txt"
    secret.write_text("漏れてはいけない内容", encoding="utf-8")
    (src / "manifest.json").symlink_to(secret)
    dst = tmp_path / "dst"

    with pytest.raises(OSError):
        PV._copy_investigation_contract_files(src, dst)

    assert not (dst / "manifest.json").exists(), "symlink の中身がコピーされてしまっている"


def test_restore_refuses_and_deletes_when_retired_manifest_is_symlink(tmp_path, monkeypatch):
    """[高-1] 退避先の manifest.json（items/ 配下のファイルではなく manifest 自身）が symlink の
    場合も、「続き」宣言で復元せず、汚染された退避先ごと削除する。"""
    users_dirname = "users_ledger_symlink_manifest_restore"
    uid = "ledger-symlink-manifest-restore"
    conv_id = 30027
    retire_dir = (tmp_path / users_dirname / uid / "workspace"
                 / ".codex-sessions" / str(conv_id) / "investigation")
    (retire_dir / "items").mkdir(parents=True)
    secret = tmp_path / "secret5.txt"
    secret.write_text("model からは読めないはずの内容5", encoding="utf-8")
    (retire_dir / "manifest.json").symlink_to(secret)

    # このターン自身は1回で complete させる（無関係な新規退避で assert が汚れないようにする）。
    steps = [
        {"thread_id": "TH-SYMLINK-MANIFEST-RESTORE",
         "ledger": {"manifest": _manifest(["b"]),
                    "items": {"b": _item("b", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final("続きの調査です。")], "usage": helper._usage()},
    ]
    _setup(tmp_path, monkeypatch, steps, users_dirname=users_dirname)
    prov = A.CodexProvider()
    env = helper._result_env(helper._run(prov, helper._ctx(
        uid=uid, conversation_id=conv_id, message="続きをお願いします")))

    assert env["investigation"]["restored"] is False, (
        "manifest.json が symlink の退避先が復元されている（RV高-1 8巡目 未是正）")
    assert env["investigation"]["complete"] is True
    assert not retire_dir.exists(), "symlink 入り退避先が削除されずに残っている"


# ===== RV是正（2026-09-22 9巡目）: ゲートの読み込み自体（load_ledger）がリンク先を辿る漏洩 =====

def test_manifest_symlink_to_other_directory_does_not_leak_ids_in_continue_prompt(tmp_path, monkeypatch):
    """[高-1] manifest.json を他ディレクトリ（他会話相当）への symlink にして final を返しても、
    `investigation_ledger.load_ledger()` がリンク先を読まないため、台帳ゲートの継続プロンプトに
    リンク先の登録 id が含まれない——退避・復元時の symlink 検査（6/8巡目）はライブ読み込み経路
    （毎 attempt の `load_ledger` 呼び出し）を防げないため、`load_ledger` 自体で拒否する。"""
    other_dir = tmp_path / "other_investigation"
    other_dir.mkdir()
    other_manifest = other_dir / "manifest.json"
    other_manifest.write_text(json.dumps(
        {"question_kind": "list", "created_at": "2026-09-21T00:00:00Z",
         "items": ["other-secret-1", "other-secret-2"]}, ensure_ascii=False), encoding="utf-8")

    steps = [
        {"thread_id": "TH-MANIFEST-SYMLINK",
         "ledger": {"manifest_symlink_to": str(other_manifest)},
         "agent_messages": [_final("結論です。")], "usage": helper._usage()},
        # 以降は台帳に触れない（symlink の manifest.json がそのまま残る）——`Path.is_file()` は
        # symlink を追従してリンク先の実体を見るため、provider.py 側は「manifest ファイルは
        # 存在するが内容が規約に合わない」（4巡目是正の repair 分岐・cap 枠）と判定し続ける。
        # load_ledger() 自体はリンク先を読まない（symlink を無効化する）ので、内容不正の状態が
        # 毎回続き、cap（10）まで催促してから受理する——このテストの主眼はその全プロンプトに
        # リンク先の登録 id が一度も現れないことの確認。
        {"thread_id": "TH-MANIFEST-SYMLINK",
         "agent_messages": [_final("再度の結論です。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_manifest_symlink")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-manifest-symlink", conversation_id=30028)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 11, (
        f"symlink の manifest.json は「内容不正」扱いで cap（10）まで催促し続けるはず: {calls!r}")
    for call in calls:
        prompt_text = call[-1]
        assert "other-secret-1" not in prompt_text and "other-secret-2" not in prompt_text, (
            f"symlink 先の他調査の登録 id がプロンプトに漏れている（RV高-1 9巡目 未是正）: "
            f"{prompt_text!r}")
    assert env["investigation"]["manifest_invalid"] is True
    assert env["investigation"]["stopped_reason"] == "cap"
    assert env["investigation"]["continuations"] == 10
    assert env["investigation"]["missing"] == []


# ===== RV是正（2026-09-22 10巡目）: 復元途中の失敗が元の退避台帳を部分台帳で破壊する =====

def test_restore_investigation_ledger_partial_failure_leaves_investigation_dir_empty(tmp_path, monkeypatch):
    """[中-1] items が複数件ある退避台帳の復元中（2件目のコピー）に OSError が起きても、
    investigation_dir に部分復元（manifest＋1件目だけ）が残らない——空の状態（新規台帳として
    扱える状態）に戻ることを、`_restore_investigation_ledger` を直接呼んで固定する。"""
    retired_dir = tmp_path / "retired"
    (retired_dir / "items").mkdir(parents=True)
    (retired_dir / "manifest.json").write_text(
        json.dumps(_manifest(["a", "b"]), ensure_ascii=False), encoding="utf-8")
    (retired_dir / "items" / "a.json").write_text(
        json.dumps(_item("a", "source_confirmed", evidence=_EVIDENCE), ensure_ascii=False),
        encoding="utf-8")
    (retired_dir / "items" / "b.json").write_text(
        json.dumps(_item("b", "pending"), ensure_ascii=False), encoding="utf-8")
    investigation_dir = tmp_path / "investigation"
    (investigation_dir / "items").mkdir(parents=True)
    tmp_root = tmp_path / "tmp_root"
    tmp_root.mkdir()

    orig_copy2 = PV.shutil.copy2

    def _boom_on_b(src, dst, *a, **k):
        if str(src).endswith("b.json"):
            raise OSError(5, "I/O error")
        return orig_copy2(src, dst, *a, **k)
    monkeypatch.setattr(PV.shutil, "copy2", _boom_on_b)

    restored = PV._restore_investigation_ledger(retired_dir, investigation_dir, tmp_root)

    assert restored is False
    assert not (investigation_dir / "manifest.json").exists(), (
        "部分復元（manifest だけ）が investigation_dir に残っている（RV中-1 10巡目 未是正）")
    assert not (investigation_dir / "items" / "a.json").exists(), (
        "部分復元（item a だけ）が investigation_dir に残っている（RV中-1 10巡目 未是正）")
    assert (investigation_dir / "items").is_dir()
    # 一時ステージングディレクトリも残らない。
    assert list(tmp_root.glob(".investigation.restore-*")) == []
    # 退避元（retired_dir）はこの関数が触らない——無変化のまま。
    assert (retired_dir / "items" / "a.json").is_file()
    assert (retired_dir / "items" / "b.json").is_file()


def test_partial_restore_failure_preserves_original_retired_ledger(tmp_path, monkeypatch):
    """[中-1] ターン開始時の復元中に OSError が起きても、その run の終了時（通常終了・finally の
    両方）で元の退避台帳（manifest＋a＋b）を削除・置換しない——`restored=False`・退避先は
    無変化のまま残ることを、実際のターンを通して固定する。"""
    users_dirname = "users_ledger_partial_restore_fail"
    uid = "ledger-partial-restore-fail"
    conv_id = 30029
    retire_dir = (tmp_path / users_dirname / uid / "workspace"
                 / ".codex-sessions" / str(conv_id) / "investigation")
    (retire_dir / "items").mkdir(parents=True)
    (retire_dir / "manifest.json").write_text(
        json.dumps(_manifest(["a", "b"]), ensure_ascii=False), encoding="utf-8")
    (retire_dir / "items" / "a.json").write_text(
        json.dumps(_item("a", "source_confirmed", evidence=_EVIDENCE), ensure_ascii=False),
        encoding="utf-8")
    (retire_dir / "items" / "b.json").write_text(
        json.dumps(_item("b", "pending"), ensure_ascii=False), encoding="utf-8")

    orig_copy2 = PV.shutil.copy2

    def _boom_on_b(src, dst, *a, **k):
        if str(src).endswith("b.json"):
            raise OSError(5, "I/O error")
        return orig_copy2(src, dst, *a, **k)
    monkeypatch.setattr(PV.shutil, "copy2", _boom_on_b)

    steps = [
        {"thread_id": "TH-PARTIAL-RESTORE",
         "agent_messages": [_final("続きの調査です。")], "usage": helper._usage()},
    ]
    _setup(tmp_path, monkeypatch, steps, users_dirname=users_dirname)
    prov = A.CodexProvider()
    env = helper._result_env(helper._run(prov, helper._ctx(
        uid=uid, conversation_id=conv_id, message="続きをお願いします")))

    assert env["investigation"]["restored"] is False, (
        "復元が部分的に失敗したのに restored=True になっている（RV中-1 10巡目 未是正）")
    # 元の退避台帳は失われていない（manifest+a+b のまま）。
    assert (retire_dir / "manifest.json").is_file(), (
        "復元失敗後に元の退避台帳（manifest.json）が失われている（RV中-1 10巡目 未是正）")
    assert (retire_dir / "items" / "a.json").is_file(), (
        "復元失敗後に元の退避台帳（item a）が失われている（RV中-1 10巡目 未是正）")
    assert (retire_dir / "items" / "b.json").is_file(), (
        "復元失敗後に元の退避台帳（item b）が失われている（RV中-1 10巡目 未是正）")
    with (retire_dir / "manifest.json").open(encoding="utf-8") as f:
        assert json.load(f)["items"] == ["a", "b"], "元の退避台帳の manifest 内容が変わっている"


# ===== claims の台帳突合（`PV._claims_vs_ledger`・docs/proposals/2026-09-21-調査台帳を文脈の外に
#       置く.md §3「claims は台帳からの投影」）: 実際のターンを通した統合確認 =====

def _final_with_claims(answer: str, claims: list, next_step: str | None = None) -> str:
    return json.dumps({"status": "final", "answer": answer, "next_step": next_step, "claims": claims},
                      ensure_ascii=False)


def test_confirmed_claim_with_unmatched_ledger_refs_is_downgraded_to_inferred(tmp_path, monkeypatch):
    """台帳（item "a"・evidence=`src/a.py:1`）が complete な状態で、最終応答の confirmed 主張が
    別の（台帳に無い）参照 `src/other.py:99` だけを挙げている——根拠種別ゲートは6キー形
    （`evidence_kinds` 無し＝未申告）のため素通りするが、台帳突合が確定を推定へ格下げし、
    `limits.claims_unmatched=True` になる。"""
    claim = {"id": "c1", "status": "confirmed", "text": "対象の値は42です。",
             "evidence_refs": ["src/other.py:99"], "reason": "", "reason_code": ""}
    steps = [
        {"thread_id": "TH-CLAIMS-LEDGER",
         "ledger": {"manifest": _manifest(["a"]),
                    "items": {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final_with_claims("対象の値は42です。", [claim])],
         "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_claims_ledger_unmatched")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="claims-ledger-unmatched", conversation_id=30030)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1, f"台帳が最初から complete なら1回で受理のはず: {calls!r}"
    assert env["investigation"]["complete"] is True
    out_claim = env["data"]["claims"][0]
    assert out_claim["status"] == "inferred"
    assert "根拠が調査台帳に無い" in out_claim["reason"]
    assert env["investigation"]["claims_check"] == {
        "ledger": True, "checked": 1, "downgraded": 1, "unmatched_refs": 1,
        "manifest_state": "valid"}
    assert env["limits"]["claims_unmatched"] is True
    # 台帳突合による格下げも、根拠種別ゲートと同じ headline 注記（同じ文言・同じ位置）を出す
    # ——内部の claims だけでなく画面・共有が読む本文側にも断定を残さない。
    assert env["headline"].startswith(PV._DEMOTED_CLAIMS_NOTE)


def test_confirmed_claim_with_matched_ledger_ref_stays_confirmed(tmp_path, monkeypatch):
    """台帳の evidence に実在する参照 `src/a.py:1` を挙げた confirmed 主張は維持され、
    `limits.claims_unmatched=False` になる（前の逆ケースと対にして固定する）。"""
    claim = {"id": "c1", "status": "confirmed", "text": "対象の値は42です。",
             "evidence_refs": ["src/a.py:1"], "reason": "", "reason_code": ""}
    steps = [
        {"thread_id": "TH-CLAIMS-LEDGER-OK",
         "ledger": {"manifest": _manifest(["a"]),
                    "items": {"a": _item("a", "source_confirmed", evidence=_EVIDENCE)}},
         "agent_messages": [_final_with_claims("対象の値は42です。", [claim])],
         "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_claims_ledger_matched")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="claims-ledger-matched", conversation_id=30031)

    env = helper._result_env(helper._run(prov, ctx))

    out_claim = env["data"]["claims"][0]
    assert out_claim["status"] == "confirmed"
    assert env["investigation"]["claims_check"]["downgraded"] == 0
    assert env["limits"]["claims_unmatched"] is False
    # 格下げが無いターンでは注記を付けない（前の逆ケースと対で固定する）。
    assert not env["headline"].startswith(PV._DEMOTED_CLAIMS_NOTE)


# ===== MCP 無効の構成では台帳ゲートも無効（台帳はツールでしか書けない） =====

def test_ledger_gate_inactive_when_mcp_disabled(tmp_path, monkeypatch):
    steps = [
        {"thread_id": "TH-L-NOMCP", "agent_messages": [_final("結論A。")], "usage": helper._usage()},
    ]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname="users_ledger_nomcp")
    monkeypatch.setenv("SHERPA_CODEX_MCP", "0")
    prov = A.CodexProvider()
    ctx = helper._ctx(uid="ledger-nomcp", conversation_id=30007)

    env = helper._result_env(helper._run(prov, ctx))

    calls = helper._read_argv_log(argv_log)
    assert len(calls) == 1, f"MCP 無効では台帳の催促をしないはず: {calls!r}"
    assert env.get("investigation") is None


def test_retired_ledger_kept_and_not_restored_when_mcp_disabled(tmp_path, monkeypatch):
    """MCP 無効の run では退避台帳を復元も削除もしない——復元すると更新手段の無い旧台帳が
    claims の突合だけに使われる（台帳 #3 の是正漏れ）。退避先は次の MCP 有効ターンまで残る。"""
    users_dirname = "users_ledger_nomcp_restore"
    uid = "ledger-nomcp-restore"
    conv_id = 30011
    steps_run1 = [
        {"thread_id": "TH-L-NM1",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [_final("途中です。")], "usage": helper._usage()},
    ]
    _setup(tmp_path, monkeypatch, steps_run1, users_dirname=users_dirname)
    prov = A.CodexProvider()
    env1 = helper._result_env(helper._run(prov, helper._ctx(
        uid=uid, conversation_id=conv_id, message="通常の依頼です")))
    assert env1["investigation"]["retained"] is True
    retired = tmp_path / users_dirname / uid / "workspace" / ".codex-sessions" / str(conv_id) / "investigation"
    assert (retired / "manifest.json").is_file()

    steps_run2 = [
        {"thread_id": "TH-L-NM2", "agent_messages": [_final("続きの結論。")], "usage": helper._usage()},
    ]
    argv_log2 = _setup(tmp_path, monkeypatch, steps_run2, users_dirname=users_dirname,
                       argv_log_name="argv2.log")
    monkeypatch.setenv("SHERPA_CODEX_MCP", "0")
    env2 = helper._result_env(helper._run(prov, helper._ctx(
        uid=uid, conversation_id=conv_id, message="続きをお願いします")))

    assert len(helper._read_argv_log(argv_log2)) == 1
    assert env2.get("investigation") is None
    assert (retired / "manifest.json").is_file(), "MCP 無効の run が退避台帳を消してはいけない"
    assert env2.get("data", {}).get("claims_check", {}).get("manifest_state", "absent") == "absent"


def test_plain_mode_turn_keeps_retained_ledger_of_standard(tmp_path, monkeypatch):
    """standard が「続き」のために退避した未完了の台帳を、素の Codex（plain）のターンは消さない。"""
    users_dirname = "users_ledger_plain_keep"
    uid = "ledger-plain-keep"
    conv_id = 30031
    steps_run1 = [
        {"thread_id": "TH-LP1",
         "ledger": {"manifest": _manifest(["a"]), "items": {"a": _item("a", "pending")}},
         "agent_messages": [_final("途中です。")], "usage": helper._usage()},
    ]
    _setup(tmp_path, monkeypatch, steps_run1, users_dirname=users_dirname)
    env1 = helper._result_env(helper._run(A.CodexProvider(), helper._ctx(
        uid=uid, conversation_id=conv_id, message="通常の依頼です")))
    assert env1["investigation"]["retained"] is True
    retired = tmp_path / users_dirname / uid / "workspace" / ".codex-sessions" / str(conv_id) / "investigation"
    assert retired.is_dir()

    steps_run2 = [{"thread_id": "TH-LP2", "agent_messages": ["素の回答です。"], "usage": helper._usage()}]
    _setup(tmp_path, monkeypatch, steps_run2, users_dirname=users_dirname, argv_log_name="argv2.log")
    helper._result_env(helper._run(A.CodexProvider(system_settings={"codex_mode": "plain"}), helper._ctx(
        uid=uid, conversation_id=conv_id, message="別の依頼です")))
    assert retired.is_dir(), "plain のターンが standard の退避台帳を消した"
