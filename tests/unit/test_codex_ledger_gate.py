"""調査台帳ゲート（`CodexProvider._run_authoring` の出力スキーマ v2 時の台帳確認）の契約。

台帳（`run_dir/.tmp/investigation/`）が complete でなければ final を受理せず、台帳から組んだ継続
プロンプトで追加 attempt を発行する（上限・無進捗・manifest 欠落で打ち切り）。未完了台帳は
`.codex-sessions/{conversation_id}/investigation/` へ退避し、「続き」系の依頼でだけ復元する。
complete に届いた後は見直しの一巡（最大 2 回）が入るため、complete 受理は +1 attempt になる。
偽 codex は台帳ファイルを書く拡張版（`step["ledger"]`）。`_ctx`/`_usage`/`_run`/`_result_env`/
`_read_argv_log` は test_codex_auto_continue.py を `helper` として再利用する。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import test_codex_auto_continue as helper  # noqa: E402

from sherpa import agents as A  # noqa: E402
from sherpa import investigation_ledger as IL  # noqa: E402
from sherpa.providers.codex import ledger_gate as ledger_gate_mod  # noqa: E402
from sherpa.providers.codex import structured as structured_mod  # noqa: E402
from sherpa.providers.codex import turn_prepare as turn_prepare_mod  # noqa: E402

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
if args and args[-1] == "-":   # プロンプトは argv でなく標準入力から渡される（fake codex 側も同じ規約に合わせる）
    args = args[:-1] + [sys.stdin.read()]
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
    coverage = ledger.get("coverage")
    if coverage:
        # COD-16: coverage.jsonl（項目ごとの記録）をテストが直接置けるようにする——実際の呼出しは
        # `sherpa/mcp_server.py::_record_item_coverage` が同じ形（item/tool/outcome/ts の4キー）で
        # 書く（この拡張はそれを模す・本文/引数は含めない契約はテスト側でも守る）。
        with (inv_dir / "coverage.jsonl").open("a", encoding="utf-8") as f:
            for entry in coverage:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    reviews = ledger.get("reviews")
    if reviews:
        # COD-18 ⑤: reviews.jsonl（中間の見直し）も coverage と同じ追記専用——テストが直接
        # 置ける（実際の呼出しは `ledger_review_put` が `ts` を足して書く・この拡張はそれを模す）。
        with (inv_dir / "reviews.jsonl").open("a", encoding="utf-8") as f:
            for entry in reviews:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
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

# 道具ゼロの促しを出さないよう、1 回目の実行は既定で道具を 1 回使う。
if call_index == 1 and not step.get("no_tools"):
    print(json.dumps({"type": "item.completed",
                       "item": {"id": "t0-default", "type": "command_execution",
                                "command": "ls", "status": "completed", "exit_code": 0}}), flush=True)

for event in step.get("extra_events", []):
    print(json.dumps(event), flush=True)

usage = step.get("usage")
if usage:
    print(json.dumps({"type": "turn.completed", "usage": usage}))
    sys.stdout.flush()

sys.exit(step.get("exit_code", 0))
'''
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


def _write_fake_codex(bin_dir: Path, argv_log: Path, plan_path: Path) -> None:
    script = bin_dir / "codex"
    script.write_text(
        _FAKE_CODEX_LEDGER_PY.replace("__ARGV_LOG__", str(argv_log))
                             .replace("__PLAN_PATH__", str(plan_path)))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp_path: Path, monkeypatch, steps: list, users_dirname: str,
          argv_log_name: str = "argv.log") -> Path:
    """出力スキーマは既定 ON（v2）のまま。`argv_log_name` は同一 tmp_path で複数ターンを呼ぶ用。"""
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


def _inprog(answer: str, next_step: str = "続けます") -> str:
    return json.dumps({"status": "in_progress", "answer": answer, "next_step": next_step, "claims": []},
                      ensure_ascii=False)


def _final_with_claims(answer: str, claims: list, next_step: str | None = None) -> str:
    return json.dumps({"status": "final", "answer": answer, "next_step": next_step, "claims": claims},
                      ensure_ascii=False)


def _manifest(items: list, question_kind: str = "list", created_at: str = "2026-09-21T00:00:00Z") -> dict:
    return {"question_kind": question_kind, "created_at": created_at, "items": items}


def _item(item_id: str, status: str, reason: str = "", evidence: list | None = None,
         owner: str = "parent", required_checks: list | None = None) -> dict:
    # required_checks が空配列だと item 自体が無効（語彙閉包契約）なので既定は ["source"]。
    return {"id": item_id, "kind": "row", "subject": "対象",
            "required_checks": required_checks if required_checks is not None else ["source"],
            "evidence": evidence or [], "status": status, "reason": reason, "owner": owner}


_EVIDENCE = [{"kind": "source", "path": "src/a.py", "line": 1}]


def _ok(item_id: str) -> dict:
    return _item(item_id, "source_confirmed", evidence=_EVIDENCE)


def _pend(item_id: str) -> dict:
    return _item(item_id, "pending")


def _review(verdict: str = "mostly_answered", added_items: list | None = None,
           removed_items: list | None = None, extra_perspectives: list | None = None,
           ts: float = 1.0, terminal_count: int = 1) -> dict:
    """正規形を満たす最小の中間の見直し（reviews.jsonl へ直接書く用）。"""
    return {"purpose": "依頼の目的を確認した", "perspectives": ["観点1"],
            "summary": "ここまでの調査で分かったことの要約", "added_items": added_items or [],
            "removed_items": removed_items or [], "verdict": verdict,
            "extra_perspectives": extra_perspectives or [], "ts": ts,
            "terminal_count": terminal_count}


def _complete(ids=("a",), **review_kw) -> dict:
    """ids が全て終端＋有効な見直し 1 件の台帳。"""
    return {"manifest": _manifest(list(ids)), "items": {i: _ok(i) for i in ids},
            "reviews": [_review(**review_kw)]}


def _open(ids=("a",)) -> dict:
    """ids が全て pending の台帳。"""
    return {"manifest": _manifest(list(ids)), "items": {i: _pend(i) for i in ids}}


def _st(tid: str, *msgs: str, ledger: dict | None = None, **kw) -> dict:
    step = {"thread_id": tid, "agent_messages": list(msgs), "usage": helper._usage(), **kw}
    if ledger is not None:
        step["ledger"] = ledger
    return step


def _users(uid: str) -> str:
    return f"users_{uid}"


def _retired(tmp_path: Path, uid: str, cid) -> Path:
    return tmp_path / _users(uid) / uid / "workspace" / ".codex-sessions" / str(cid) / "investigation"


def _t(tmp_path, monkeypatch, steps, uid, cid, *, message=None, log="argv.log", env=None, prov=None):
    """1 ターン実行して (events, env, calls) を返す。同一 uid の複数ターンは同じ users dir を共有する。"""
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname=_users(uid), argv_log_name=log)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    kw = {} if message is None else {"message": message}
    events = helper._run(prov or A.CodexProvider(), helper._ctx(uid=uid, conversation_id=cid, **kw))
    return events, helper._result_env(events), helper._read_argv_log(argv_log)


def _review_nodes(events) -> list:
    return [e["id"] for e in events if isinstance(e, dict) and e.get("type") == "node"
            and str(e.get("id", "")).startswith("ledger-review-")]


def _growth_steps(tid: str, n: int) -> list:
    """毎回 1 件終端化しつつ新しい item を足す（母集団が伸び続け無進捗にならない）台帳の n 段。"""
    steps = [_st(tid, _final("初期報告です。"), ledger=_open(["a0"]))]
    for i in range(1, n + 1):
        steps.append(_st(tid, _final(f"途中{i}です。"), ledger={
            "manifest": _manifest([f"a{j}" for j in range(i + 1)]),
            "items": {f"a{i - 1}": _ok(f"a{i - 1}"), f"a{i}": _pend(f"a{i}")}}))
    return steps


# ===== MCP 経由の台帳作成・必須種別 =====

def test_ledger_written_through_mcp_accepts_final(tmp_path, monkeypatch):
    """偽 Codex から本物の stdio MCP サーバへ接続し、台帳をツールだけで作成して受理される。"""
    log = tmp_path / "mcp.jsonl"
    steps = [_st("TH-MCP-LEDGER", _final("MCPで登録しました。"), mcp_log=str(log), ledger_tools=[
                 {"name": "ledger_manifest_set", "arguments": {"question_kind": "list", "items": ["a"]}},
                 {"name": "ledger_item_put", "arguments": _ok("a")},
                 {"name": "ledger_review_put", "arguments": _review()}]),
             # ledger_tools を持たない別 step（mcp_log に 2 行目を足さない）
             _st("TH-MCP-LEDGER", _final("見直しましたが変更ありません。"))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "mcp-ledger", 31001)
    assert len(calls) == 2
    results = json.loads(log.read_text())
    assert results[0]["manifest_invalid"] is True and results[0]["items"] == 0
    assert results[1] == {"ok": True}
    assert results[2] == {"ok": True, "id": "a"}
    assert results[3] == {"ok": True}
    assert results[4]["complete"] is True
    inv = env["investigation"]
    assert inv["complete"] is True and inv["continuations"] == 0
    assert inv["review"] == {"attempted": True, "rounds": 0, "items_added": 0}
    assert inv["mid_review"] == {"count": 1, "missing": False, "pending": []}


def test_ledger_mcp_unsatisfied_kinds_in_continuation_prompt(tmp_path, monkeypatch):
    log = tmp_path / "mcp.jsonl"
    partial = _item("sel1_z", "spec_only", evidence=[{"kind": "spec_doc", "path": "docs/spec.md", "line": 8}])
    partial.update(required_checks=["source", "spec_doc"], subject="本文をプロンプトへ戻さない")
    complete = {**partial, "status": "source_confirmed", "evidence": [*partial["evidence"], *_EVIDENCE]}
    steps = [
        _st("TH-MCP-UNSATISFIED", _final("資料の確認が済みました。"), mcp_log=str(log), ledger_tools=[
            {"name": "ledger_manifest_set", "arguments": {"question_kind": "list", "items": ["sel1_z"]}},
            {"name": "ledger_item_put", "arguments": partial}]),
        _st("TH-MCP-UNSATISFIED", _final("ソースも確認しました。"), mcp_log=str(log), ledger_tools=[
            {"name": "ledger_item_put", "arguments": complete},
            {"name": "ledger_review_put", "arguments": _review()}]),
    ]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "mcp-unsatisfied", 31002)
    assert len(calls) == 3
    assert "未充足: sel1_z（source が未確認）" in calls[1][-1]
    assert partial["subject"] not in calls[1][-1]
    assert "docs/spec.md" not in calls[1][-1] and "src/a.py" not in calls[1][-1]
    results = [json.loads(line) for line in log.read_text().splitlines()]
    assert results[0][-1]["unsatisfied"] == {"sel1_z": ["source"]}
    assert results[1][-1]["complete"] is True
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["continuations"] == 1


def test_ledger_source_required_by_scope_even_when_item_omits_it(tmp_path, monkeypatch):
    """範囲にソースがあれば item が required_checks に source を含めなくても spec_only だけでは完了にならない。"""
    spec = [{"kind": "spec_doc", "path": "docs/x.md", "line": 1}]
    partial = _item("a", "spec_only", evidence=spec, required_checks=["spec_doc"])
    complete = {**partial, "status": "source_confirmed", "required_checks": ["spec_doc", "source"],
                "evidence": [*spec, *_EVIDENCE]}
    steps = [_st("TH-SRC-REQ", _final("資料の確認が済みました。"),
                 ledger={"manifest": _manifest(["a"]), "items": {"a": partial}}),
             _st("TH-SRC-REQ", _final("ソースも確認しました。"),
                 ledger={"items": {"a": complete}, "reviews": [_review()]})]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "src-required", 31101)
    assert len(calls) == 3
    assert "未充足: a（source が未確認）" in calls[1][-1]
    assert env["investigation"]["complete"] is True


def test_ledger_spec_only_does_not_complete_when_layer_excludes_source_but_scope_has_source(tmp_path, monkeypatch):
    """層を docs に絞っても範囲にソースがあるなら source は必須のまま（spec_only だけでは完了しない）。"""
    item = _item("a", "spec_only", evidence=[{"kind": "spec_doc", "path": "docs/x.md", "line": 1}],
                 required_checks=["spec_doc"])
    steps = [_st("TH-DOCS-ONLY", _final("資料で確認しました。"),
                 ledger={"manifest": _manifest(["a"]), "items": {"a": item}, "reviews": [_review()]})]
    argv_log = _setup(tmp_path, monkeypatch, steps, users_dirname=_users("docs-only-layer"))
    ctx = helper._ctx("docs-only-layer", 31102)
    ctx.scope_meta = {"layer": "docs"}
    env = helper._result_env(helper._run(A.CodexProvider(), ctx))
    assert len(helper._read_argv_log(argv_log)) > 2
    assert env["investigation"]["complete"] is False


def test_ledger_source_required_extra_ignores_layer(monkeypatch):
    """ソースの必須は探す対象（層）に依存しない。走査が判定不能なら安全側（必須）へ倒す。"""
    assert ledger_gate_mod._ledger_source_required_extra("v1", [], None) == ("source",)
    assert ledger_gate_mod._ledger_source_required_extra("v1", [], "docs") == ("source",)
    monkeypatch.setattr(ledger_gate_mod, "_scope_evidence_kinds", lambda *a, **k: None)
    assert ledger_gate_mod._ledger_source_required_extra("v1", [], "docs") == ("source",)


# ===== 受理・継続・打ち切り =====

def test_ledger_complete_on_first_attempt_accepts_with_review_round(tmp_path, monkeypatch):
    steps = [_st("TH-L1", _final("結論です。"), ledger=_complete())]
    uid, cid = "ledger-complete", 30001
    _, env, calls = _t(tmp_path, monkeypatch, steps, uid, cid)
    assert len(calls) == 2   # 最初から complete でも見直しの一巡で 2 回
    assert env["headline"] == "結論です。"
    inv = env["investigation"]
    assert inv["complete"] is True and inv["continuations"] == 0 and inv["stopped_reason"] == "complete"
    assert env["limits"]["ledger_incomplete"] is False
    assert inv["review"] == {"attempted": True, "rounds": 0, "items_added": 0}
    assert inv["retained"] is False
    assert not _retired(tmp_path, uid, cid).exists()   # complete な台帳は退避しない


def test_ledger_incomplete_item_triggers_one_ledger_continuation(tmp_path, monkeypatch):
    steps = [_st("TH-L2", _final("途中です。"), ledger=_open()),
             _st("TH-L2", _final("確定しました。"),
                 ledger={"items": {"a": _ok("a")}, "reviews": [_review()]})]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-one-continue", 30002)
    assert len(calls) == 3
    assert "a" in calls[1][-1]
    assert "resume" in calls[1] and "TH-L2" in calls[1]
    assert env["headline"] == "確定しました。"
    inv = env["investigation"]
    assert inv["complete"] is True and inv["continuations"] == 1 and inv["stopped_reason"] == "complete"


def test_ledger_no_progress_twice_stops_and_accepts(tmp_path, monkeypatch):
    step = _st("TH-L3", _final("途中です。"), ledger=_open())
    _, env, calls = _t(tmp_path, monkeypatch, [step, dict(step), dict(step)], "ledger-no-progress", 30004)
    assert len(calls) == 3
    inv = env["investigation"]
    assert inv["complete"] is False and inv["stopped_reason"] == "no_progress" and inv["continuations"] == 2
    assert env["limits"]["ledger_incomplete"] is True


def test_ledger_cap_after_ten_continuations(tmp_path, monkeypatch):
    """毎回進捗があっても母集団が伸び続ければ、無進捗ではなく cap（10）で打ち切る。"""
    _, env, calls = _t(tmp_path, monkeypatch, _growth_steps("TH-L4", 10), "ledger-cap", 30005)
    assert len(calls) == 11
    inv = env["investigation"]
    assert inv["complete"] is False and inv["stopped_reason"] == "cap" and inv["continuations"] == 10
    assert env["limits"]["ledger_incomplete"] is True


def test_ledger_missing_manifest_retries_once_then_accepts(tmp_path, monkeypatch):
    steps = [_st("TH-L5", _final("結論A。")), _st("TH-L5", _final("結論A再。"))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-missing", 30006)
    assert len(calls) == 2
    assert calls[1][-1] == ledger_gate_mod._LEDGER_MANIFEST_MISSING_PROMPT
    inv = env["investigation"]
    assert inv["complete"] is False and inv["manifest_invalid"] is True
    assert inv["stopped_reason"] == "ledger_missing" and inv["continuations"] == 1


# ===== 退避・復元 =====

@pytest.mark.parametrize("cid,retained", [(None, False), (30007, True)])
def test_retire_only_with_conversation_id(tmp_path, monkeypatch, cid, retained):
    uid = f"ledger-retire-{retained}"
    _, env, _ = _t(tmp_path, monkeypatch, [_st("TH-L6", _final("途中です。"), ledger=_open())], uid, cid)
    assert env["investigation"]["complete"] is False
    assert env["investigation"]["retained"] is retained
    if retained:
        retire_dir = _retired(tmp_path, uid, cid)
        assert (retire_dir / "manifest.json").is_file()
        assert (retire_dir / "items" / "a.json").is_file()
    else:
        assert not (tmp_path / _users(uid) / uid / "workspace" / ".codex-sessions").exists()


def test_restore_ledger_when_message_starts_with_continue_prefix(tmp_path, monkeypatch):
    uid, cid = "ledger-restore", 30009
    _, env1, _ = _t(tmp_path, monkeypatch, [_st("TH-L7A", _final("途中です。"), ledger=_open())],
                    uid, cid, message="通常の依頼です")
    assert env1["investigation"]["retained"] is True
    # manifest を渡さず item だけ終端化: 復元されていなければ manifest_invalid のまま区別できる
    steps2 = [_st("TH-L7B", _final("完了しました。"), ledger={"items": {"a": _ok("a")}, "reviews": [_review()]})]
    _, env2, calls2 = _t(tmp_path, monkeypatch, steps2, uid, cid, message="続きをお願いします", log="argv2.log")
    assert len(calls2) == 2
    assert env2["investigation"]["restored"] is True and env2["investigation"]["complete"] is True


def test_retired_ledger_deleted_when_message_does_not_start_with_continue_prefix(tmp_path, monkeypatch):
    uid, cid = "ledger-no-restore", 30010
    _, env1, _ = _t(tmp_path, monkeypatch, [_st("TH-L7C", _final("途中です。"), ledger=_open())],
                    uid, cid, message="通常の依頼です")
    assert env1["investigation"]["retained"] is True
    steps2 = [_st("TH-L7D", _final("別件、完了しました。"), ledger=_complete(["b"]))]
    _, env2, _ = _t(tmp_path, monkeypatch, steps2, uid, cid, message="別件をお願いします", log="argv2.log")
    assert env2["investigation"]["restored"] is False
    assert env2["investigation"]["complete"] is True
    assert not _retired(tmp_path, uid, cid).exists()


def test_gate_inactive_when_schema_disabled(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-L8", "ledger": _open(),
              "agent_messages": ["確認した結果、影響はありません。"], "usage": helper._usage()}]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-schema-off", 30011,
                       env={"SHERPA_CODEX_OUTPUT_SCHEMA": "0"})
    assert len(calls) == 1
    assert env["headline"] == "確認した結果、影響はありません。"
    assert not any("--output-schema" in c for c in calls)


# ===== 台帳ゲートの順序・進捗判定（敵対 RV の再現） =====

def test_stale_rejected_final_is_not_returned_after_ledger_continuation_goes_in_progress(tmp_path, monkeypatch):
    """ゲートが拒否した最初の final は、継続が in_progress で終わっても採用されない（最後の final を採る）。"""
    steps = [_st("TH-STALE", _final("第一final・まだ未完了です。"), ledger=_open()),
             _st("TH-STALE", _inprog("作業中です。")),
             _st("TH-STALE", _final("最終結論です。"), ledger={"items": {"a": _ok("a")}, "reviews": [_review()]})]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-stale-final", 30012)
    assert len(calls) == 4
    assert env["headline"] == "最終結論です。"
    assert env["investigation"]["complete"] is True and env["investigation"]["continuations"] == 1


def test_ledger_progress_resets_streak_when_any_item_terminalizes_each_round(tmp_path, monkeypatch):
    """1 件ずつ終端化する継続は、残りの非終端集合が変わらなくても無進捗と誤検知されない。"""
    steps = [_st("TH-PROGRESS", _final("初期報告です。"), ledger=_open(["a", "b", "c"])),
             _st("TH-PROGRESS", _final("aを確認しました。"), ledger={"items": {"a": _ok("a")}}),
             _st("TH-PROGRESS", _final("bを確認しました。"), ledger={"items": {"b": _ok("b")}}),
             _st("TH-PROGRESS", _final("全て確認しました。"),
                 ledger={"items": {"c": _ok("c")}, "reviews": [_review()]})]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-progress", 30013)
    assert len(calls) == 5
    assert env["headline"] == "全て確認しました。"
    inv = env["investigation"]
    assert inv["complete"] is True and inv["stopped_reason"] == "complete" and inv["continuations"] == 3


def test_generator_close_retires_incomplete_ledger_without_relying_on_verdict(tmp_path, monkeypatch):
    """`_attempt()` 内の yield で generator を close しても、未完了台帳は outer finally が退避する。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(_FAKE_CODEX_CLOSE_PY)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    uid, cid = "ledger-close-u1", 30014
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / _users(uid)))

    gen = A.CodexProvider().run(helper._ctx(uid=uid, conversation_id=cid))
    seen: list = []
    for _ in range(20):
        ev = next(gen)
        seen.append(ev)
        if isinstance(ev, dict) and str(ev.get("id", "")).startswith("cx-"):
            break
    else:
        raise AssertionError(f"command_execution node（cx-*）に到達しなかった。seen={seen!r}")
    gen.close()

    retire_dir = _retired(tmp_path, uid, cid)
    assert (retire_dir / "manifest.json").is_file()
    assert (retire_dir / "items" / "a.json").is_file()


def test_retire_directory_is_either_absent_or_complete_and_lock_released_after_run(tmp_path, monkeypatch):
    """退避は一時ディレクトリ→原子置換で中間状態を残さず、ターン終了後に会話ロックが解放されている。"""
    uid, cid = "ledger-retire-atomic", 30015
    _, env, _ = _t(tmp_path, monkeypatch, [_st("TH-RETIRE-ATOMIC", _final("途中です。"), ledger=_open(["a", "b"]))],
                   uid, cid)
    assert env["investigation"]["complete"] is False and env["investigation"]["retained"] is True
    retire_dir = _retired(tmp_path, uid, cid)
    assert (retire_dir / "manifest.json").is_file()
    assert (retire_dir / "items" / "a.json").is_file() and (retire_dir / "items" / "b.json").is_file()
    assert list(retire_dir.parent.glob(".investigation.tmp-*")) == []
    lk = turn_prepare_mod._conversation_lock(cid)
    assert lk.acquire(blocking=False)
    lk.release()


def test_unevaluated_final_in_same_attempt_is_not_adopted_when_ledger_incomplete(tmp_path, monkeypatch):
    """台帳未完了の final は完了回答にせず、後続の途中本文とともに部分回答として残す。"""
    steps = [_st("TH-UNEVAL", _final("第一final・まだ未完了です。"), _inprog("これから確認します。")),
             _st("TH-UNEVAL", _inprog("台帳の催促後も作業中です。")),
             _st("TH-UNEVAL", _inprog("自動継続後も作業中です。"))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-unevaluated-final", 30017)
    assert len(calls) == 3
    assert "第一final・まだ未完了です。" in env["headline"]
    assert "台帳の確認が終わる前" in env["headline"]
    assert "続きの調査で得た途中の内容" in env["headline"]
    for text in ("これから確認します。", "台帳の催促後も作業中です。", "自動継続後も作業中です。"):
        assert env["headline"].count(text) == 1
    assert env["completion"] == "partial"
    assert env.get("codex_stopped_early") is True
    inv = env["investigation"]
    assert inv["complete"] is False and inv["manifest_invalid"] is True and inv["stopped_reason"] == "ledger_missing"


def test_final_in_auto_continue_without_tools_still_routes_to_ledger_gate(tmp_path, monkeypatch):
    """自動継続 attempt がツール未実行で final→in_progress を出しても、final 候補があれば打ち切らず台帳ゲートへ戻す。"""
    steps = [_st("TH-AC-UNEVAL", _inprog("まず確認します。")),
             _st("TH-AC-UNEVAL", _final("第一final・まだ未完了です。"), _inprog("やっぱり続けます。"), ledger=_open()),
             _st("TH-AC-UNEVAL", _inprog("台帳継続後も作業中です。"))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-ac-uneval", 30019,
                       env={"SHERPA_CODEX_AUTO_CONTINUE": "1"})
    assert len(calls) == 3
    assert "a" in calls[2][-1]   # 3 回目は台帳継続プロンプト
    assert env["headline"] != "第一final・まだ未完了です。"
    assert env["investigation"]["complete"] is False
    assert env["investigation"]["continuations"] >= 1


def test_manifest_invalid_content_is_repaired_via_cap_budget_then_succeeds(tmp_path, monkeypatch):
    """manifest の内容不正は「未作成」とは別の催促で、cap 枠内で修復（MCP の台帳ツール経由）すれば受理される。"""
    log = tmp_path / "mcp.jsonl"
    steps = [_st("TH-REPAIR-OK", _final("第一final・まだ未完了です。"),
                 ledger={"manifest": {"question_kind": "list", "items": ["a"]}}),   # created_at 欠落
             _st("TH-REPAIR-OK", _final("修復して完了しました。"), mcp_log=str(log), ledger_tools=[
                 {"name": "ledger_manifest_set", "arguments": {"question_kind": "list", "items": ["a"]}},
                 {"name": "ledger_item_put", "arguments": _ok("a")},
                 {"name": "ledger_review_put", "arguments": _review()}])]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-manifest-repair-ok", 30020)
    assert len(calls) == 3
    assert calls[1][-1] == ledger_gate_mod._LEDGER_MANIFEST_INVALID_PROMPT
    assert env["headline"] == "修復して完了しました。"
    assert env["investigation"]["complete"] is True and env["investigation"]["continuations"] == 1


def test_manifest_invalid_content_never_repaired_stops_at_cap(tmp_path, monkeypatch):
    steps = [_st("TH-REPAIR-CAP", _final("第一final・まだ未完了です。"),
                 ledger={"manifest": {"question_kind": "list", "items": ["a"]}}),
             _st("TH-REPAIR-CAP", _final("まだ修復していません。"))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-manifest-repair-cap", 30021)
    assert len(calls) == 11
    inv = env["investigation"]
    assert inv["complete"] is False and inv["stopped_reason"] == "cap" and inv["continuations"] == 10
    assert env["limits"]["ledger_incomplete"] is True


def test_progress_during_auto_continue_resets_no_progress_streak(tmp_path, monkeypatch):
    """自動継続中に生じた進捗（a の終端化）も streak をリセットし、無進捗 2 回と誤検知されない。"""
    steps = [_st("TH-PROGRESS-AC", _final("初期報告です。"), ledger=_open(["a", "b"])),
             _st("TH-PROGRESS-AC", _inprog("まだ確認中です。")),
             _st("TH-PROGRESS-AC", _final("aを確認した結果、影響ありません。"), ledger={"items": {"a": _ok("a")}}),
             _st("TH-PROGRESS-AC", _inprog("またまだです。")),
             _st("TH-PROGRESS-AC", _final("全て確認しました。"),
                 ledger={"items": {"b": _ok("b")}, "reviews": [_review()]})]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-progress-ac", 30022)
    assert len(calls) == 6
    assert env["headline"] == "全て確認しました。"
    assert env["investigation"]["complete"] is True and env["investigation"]["stopped_reason"] == "complete"


def test_manifest_missing_at_cap_boundary_stops_at_cap_without_extra_prompt(tmp_path, monkeypatch):
    """進捗つきの台帳継続を 10 回重ねた末に manifest が消えても、「1 回だけの催促」は cap を無視して発行されない。"""
    steps = _growth_steps("TH-CAP-MISSING", 9)
    steps.append(_st("TH-CAP-MISSING", _final("最後の項目も確認しましたが、まだ残りがあるかもしれません。"),
                     ledger={"items": {"a9": _ok("a9")}, "delete_manifest": True}))
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-cap-missing-manifest", 30023)
    assert len(calls) == 11
    inv = env["investigation"]
    assert inv["complete"] is False and inv["stopped_reason"] == "cap" and inv["continuations"] == 10
    assert env["limits"]["ledger_incomplete"] is True


def test_field_level_progress_without_terminalizing_resets_no_progress_streak(tmp_path, monkeypatch):
    """pending→in_progress→evidence 追加と、非終端集合が縮まなくても status/evidence が変われば無進捗と誤検知しない。"""
    steps = [_st("TH-FIELD-PROGRESS", _final("初期報告です。"), ledger=_open()),
             _st("TH-FIELD-PROGRESS", _final("状態を更新しました。"),
                 ledger={"items": {"a": _item("a", "in_progress")}}),
             _st("TH-FIELD-PROGRESS", _final("根拠を追加しました。"),
                 ledger={"items": {"a": _item("a", "in_progress", evidence=_EVIDENCE)}}),
             _st("TH-FIELD-PROGRESS", _final("確認完了です。"),
                 ledger={"items": {"a": _ok("a")}, "reviews": [_review()]})]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-field-progress", 30026)
    assert len(calls) == 5
    assert env["headline"] == "確認完了です。"
    assert env["investigation"]["complete"] is True and env["investigation"]["continuations"] == 3


# ===== 退避・復元の純関数（symlink 拒否・規約外ファイル・原子性） =====

def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _mk_inv(base: Path, *, manifest=True, items=("a",)) -> Path:
    """manifest.json（manifest=True のとき）と items/{id}.json（pending）を持つ台帳ディレクトリ。"""
    (base / "items").mkdir(parents=True)
    if manifest:
        _write_json(base / "manifest.json", _manifest(list(items)))
    for i in items:
        _write_json(base / "items" / f"{i}.json", _pend(i))
    return base


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "ledger_home"
    h.mkdir()
    return h


def test_retire_moves_incomplete_ledger_with_broken_manifest_but_items_present(tmp_path):
    """manifest 無しでも items 配下にファイルがあれば退避する（manifest_invalid だけで判定しない）。"""
    inv = _mk_inv(tmp_path / "investigation", manifest=False)
    home = _home(tmp_path)
    assert ledger_gate_mod._retire_investigation_ledger(inv, home) is True
    assert (home / "investigation" / "items" / "a.json").is_file()
    assert not (home / "investigation" / "manifest.json").exists()


def test_retire_skips_truly_unused_empty_investigation_dir(tmp_path):
    inv = _mk_inv(tmp_path / "investigation", manifest=False, items=())
    home = _home(tmp_path)
    assert ledger_gate_mod._retire_investigation_ledger(inv, home) is False
    assert not (home / "investigation").exists()


def test_retire_refuses_when_items_dir_has_symlink(tmp_path):
    """items/ 配下に symlink が 1 つでもあれば退避しない（model-shell が仕込んだホスト側ファイルを実体化しない）。"""
    inv = _mk_inv(tmp_path / "investigation")
    secret = tmp_path / "secret.txt"
    secret.write_text("model からは読めないはずの内容", encoding="utf-8")
    (inv / "items" / "leak.json").symlink_to(secret)
    home = _home(tmp_path)
    assert ledger_gate_mod._retire_investigation_ledger(inv, home) is False
    assert not (home / "investigation").exists()


def test_retire_ignores_non_contract_files(tmp_path):
    """退避対象は manifest.json と items/*.json だけ。規約外ファイル・ディレクトリは含めない。"""
    inv = _mk_inv(tmp_path / "investigation")
    (inv / "items" / "note.txt").write_text("本文混入テスト", encoding="utf-8")
    (inv / "stray_dir").mkdir()
    (inv / "stray_dir" / "x.json").write_text("{}", encoding="utf-8")
    home = _home(tmp_path)
    assert ledger_gate_mod._retire_investigation_ledger(inv, home) is True
    retire_dir = home / "investigation"
    assert (retire_dir / "manifest.json").is_file() and (retire_dir / "items" / "a.json").is_file()
    assert not (retire_dir / "items" / "note.txt").exists()
    assert not (retire_dir / "stray_dir").exists()


def test_retire_refuses_when_investigation_dir_unlistable_and_manifest_is_symlink(tmp_path):
    """investigation/ を chmod 0o111（列挙不可）にして manifest.json を symlink にしても、列挙失敗を握りつぶさず拒否する。"""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root では chmod によるディレクトリ列挙禁止が効かない")
    inv = tmp_path / "investigation"
    (inv / "items").mkdir(parents=True)
    _write_json(inv / "items" / "a.json", _pend("a"))
    secret = tmp_path / "secret3.txt"
    secret.write_text("model からは読めないはずの内容3", encoding="utf-8")
    (inv / "manifest.json").symlink_to(secret)
    home = _home(tmp_path)
    mode_before = inv.stat().st_mode
    inv.chmod(0o111)
    try:
        retained = ledger_gate_mod._retire_investigation_ledger(inv, home)
    finally:
        inv.chmod(mode_before)
    assert retained is False
    assert not (home / "investigation").exists()


def test_retained_reflects_actual_copy_failure_in_envelope(tmp_path, monkeypatch):
    """コピーに失敗したら retained は実際の成否（False）を記録する。"""
    def _boom_copy2(*_a, **_k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(shutil, "copy2", _boom_copy2)
    _, env, _ = _t(tmp_path, monkeypatch, [_st("TH-RETAIN-FAIL", _final("途中です。"), ledger=_open())],
                   "ledger-retain-fail", 30018)
    assert env["investigation"]["complete"] is False
    assert env["investigation"]["retained"] is False


@pytest.mark.parametrize("name", ["manifest.json", "coverage.jsonl"])
def test_copy_investigation_contract_files_refuses_symlink(tmp_path, name):
    """コピー直前に symlink を個別確認し（木の走査・列挙権限に依存せず）OSError で中止する。"""
    src = tmp_path / "src"
    src.mkdir()
    if name != "manifest.json":
        _write_json(src / "manifest.json", {"question_kind": "list", "created_at": "t", "items": ["a"]})
    secret = tmp_path / "secret4.txt"
    secret.write_text("漏れてはいけない内容", encoding="utf-8")
    (src / name).symlink_to(secret)
    dst = tmp_path / "dst"
    with pytest.raises(OSError):
        ledger_gate_mod._copy_investigation_contract_files(src, dst)
    assert not (dst / name).exists()


def test_copy_investigation_contract_files_carries_coverage_jsonl(tmp_path):
    """coverage.jsonl も退避・復元で持ち越す（無いと前ターンの検索記録が消え、確定済みの not_found_in_scope が降格しうる）。"""
    src = tmp_path / "src"
    src.mkdir()
    _write_json(src / "manifest.json", {"question_kind": "list", "created_at": "t", "items": ["a"]})
    (src / "coverage.jsonl").write_text(
        json.dumps({"item": "a", "tool": "ripgrep_search", "outcome": "no_hits", "ts": 1.0}) + "\n",
        encoding="utf-8")
    dst = tmp_path / "dst"
    ledger_gate_mod._copy_investigation_contract_files(src, dst)
    assert (dst / "coverage.jsonl").read_text(encoding="utf-8") == (src / "coverage.jsonl").read_text(encoding="utf-8")
    assert IL.load_coverage(dst) == {"a": ("no_hits",)}


@pytest.mark.parametrize("poison", ["items_leak", "manifest"])
def test_restore_refuses_and_deletes_retired_dir_with_symlink(tmp_path, monkeypatch, poison):
    """退避先に symlink（items/ 配下・manifest.json 自身）があれば、「続き」宣言でも復元せず退避先ごと削除する。"""
    uid, cid = f"ledger-symlink-restore-{poison}", 30025
    retire_dir = _retired(tmp_path, uid, cid)
    (retire_dir / "items").mkdir(parents=True)
    secret = tmp_path / "secret2.txt"
    secret.write_text("model からは読めないはずの内容2", encoding="utf-8")
    if poison == "items_leak":
        _write_json(retire_dir / "manifest.json", _manifest(["a"]))
        (retire_dir / "items" / "leak.json").symlink_to(secret)
    else:
        (retire_dir / "manifest.json").symlink_to(secret)
    # このターン自身は 1 回で complete させる
    _, env, _ = _t(tmp_path, monkeypatch, [_st("TH-SYMLINK-RESTORE", _final("続きの調査です。"), ledger=_complete(["b"]))],
                   uid, cid, message="続きをお願いします")
    assert env["investigation"]["restored"] is False
    assert env["investigation"]["complete"] is True
    assert not retire_dir.exists()


def test_manifest_symlink_to_other_directory_does_not_leak_ids_in_continue_prompt(tmp_path, monkeypatch):
    """manifest.json が他調査への symlink でも load_ledger がリンク先を読まず、継続プロンプトに他調査の登録 id が漏れない。"""
    other_manifest = tmp_path / "other_investigation" / "manifest.json"
    _write_json(other_manifest, {"question_kind": "list", "created_at": "2026-09-21T00:00:00Z",
                                 "items": ["other-secret-1", "other-secret-2"]})
    steps = [_st("TH-MANIFEST-SYMLINK", _final("結論です。"), ledger={"manifest_symlink_to": str(other_manifest)}),
             _st("TH-MANIFEST-SYMLINK", _final("再度の結論です。"))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "ledger-manifest-symlink", 30028)
    assert len(calls) == 11   # 内容不正扱いで cap（10）まで催促
    for call in calls:
        assert "other-secret-1" not in call[-1] and "other-secret-2" not in call[-1]
    inv = env["investigation"]
    assert inv["manifest_invalid"] is True and inv["stopped_reason"] == "cap"
    assert inv["continuations"] == 10 and inv["missing"] == []


def _mk_retired_ab(base: Path) -> Path:
    (base / "items").mkdir(parents=True)
    _write_json(base / "manifest.json", _manifest(["a", "b"]))
    _write_json(base / "items" / "a.json", _ok("a"))
    _write_json(base / "items" / "b.json", _pend("b"))
    return base


def _copy2_boom_on_b(monkeypatch):
    orig = shutil.copy2

    def _boom(src, dst, *a, **k):
        if str(src).endswith("b.json"):
            raise OSError(5, "I/O error")
        return orig(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "copy2", _boom)


def test_restore_investigation_ledger_partial_failure_leaves_investigation_dir_empty(tmp_path, monkeypatch):
    """復元中（2 件目のコピー）に OSError が起きても、部分復元・ステージングを残さず退避元も変えない。"""
    retired = _mk_retired_ab(tmp_path / "retired")
    inv = tmp_path / "investigation"
    (inv / "items").mkdir(parents=True)
    tmp_root = tmp_path / "tmp_root"
    tmp_root.mkdir()
    _copy2_boom_on_b(monkeypatch)
    assert ledger_gate_mod._restore_investigation_ledger(retired, inv, tmp_root) is False
    assert not (inv / "manifest.json").exists()
    assert not (inv / "items" / "a.json").exists()
    assert (inv / "items").is_dir()
    assert list(tmp_root.glob(".investigation.restore-*")) == []
    assert (retired / "items" / "a.json").is_file() and (retired / "items" / "b.json").is_file()


def test_partial_restore_failure_preserves_original_retired_ledger(tmp_path, monkeypatch):
    """ターン開始時の復元が部分失敗しても、run 終了時に元の退避台帳（manifest+a+b）を削除・置換しない。"""
    uid, cid = "ledger-partial-restore-fail", 30029
    retire_dir = _mk_retired_ab(_retired(tmp_path, uid, cid))
    _copy2_boom_on_b(monkeypatch)
    _, env, _ = _t(tmp_path, monkeypatch, [_st("TH-PARTIAL-RESTORE", _final("続きの調査です。"))],
                   uid, cid, message="続きをお願いします")
    assert env["investigation"]["restored"] is False
    assert (retire_dir / "manifest.json").is_file()
    assert (retire_dir / "items" / "a.json").is_file() and (retire_dir / "items" / "b.json").is_file()
    assert json.loads((retire_dir / "manifest.json").read_text(encoding="utf-8"))["items"] == ["a", "b"]


# ===== `_ledger_progressed`（純関数） =====

def _snapshot(manifest_items: list, items: dict, invalid_ids: tuple = ()) -> "IL.LedgerSnapshot":
    return IL.LedgerSnapshot(manifest=_manifest(manifest_items) if manifest_items is not None else None,
                             items=items, invalid_ids=invalid_ids)


@pytest.mark.parametrize("prev,curr,expected", [
    # 登録済みだがファイル未作成（missing）だった item が作られた
    (_snapshot(["a", "b", "c", "d"], {}), _snapshot(["a", "b", "c", "d"], {"a": _ok("a")}), True),
    # 未登録 item が pending のまま混在していても、登録済みに変化が無ければ進捗なし
    (_snapshot(["a"], {"a": _pend("a"), "b": _pend("b")}), _snapshot(["a"], {"a": _pend("a"), "b": _pend("b")}), False),
    # 登録済み item が 1 件終端化
    (_snapshot(["a", "b"], {"a": _pend("a"), "b": _pend("b")}),
     _snapshot(["a", "b"], {"a": _ok("a"), "b": _pend("b")}), True),
    # 終端化しなくても status が変われば進捗あり
    (_snapshot(["a"], {"a": _pend("a")}), _snapshot(["a"], {"a": _item("a", "in_progress")}), True),
    # 完全に無変化
    (_snapshot(["a"], {"a": _pend("a")}), _snapshot(["a"], {"a": _pend("a")}), False),
])
def test_ledger_progressed(prev, curr, expected):
    assert ledger_gate_mod._ledger_progressed(prev, curr) is expected


def test_ledger_progressed_false_with_required_extra_when_nothing_changes():
    """required_extra で未充足になった無変化の item は、`ledger_complete()` と `_ledger_progressed` の両方へ同じ
    required_extra を渡す限り進捗なしと判定する。"""
    spec_only = _item("s", "spec_only", evidence=[{"kind": "spec_doc", "path": "docs/x.md", "line": 1}],
                      required_checks=["spec_doc"])
    prev, curr = _snapshot(["s"], {"s": spec_only}), _snapshot(["s"], {"s": dict(spec_only)})
    assert IL.ledger_complete(curr, required_extra=("source",)).unsatisfied == {"s": ("source",)}
    assert ledger_gate_mod._ledger_progressed(prev, curr, required_extra=("source",)) is False


# ===== claims の台帳突合 =====

def test_confirmed_claim_with_unmatched_ledger_refs_is_downgraded_to_inferred(tmp_path, monkeypatch):
    """台帳に無い参照だけを挙げた confirmed は推定へ格下げされ、limits.claims_unmatched と headline 注記が付く。"""
    claim = {"id": "c1", "status": "confirmed", "text": "対象の値は42です。",
             "evidence_refs": ["src/other.py:99"], "reason": "", "reason_code": ""}
    steps = [_st("TH-CLAIMS-LEDGER", _final_with_claims("対象の値は42です。", [claim]), ledger=_complete())]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "claims-ledger-unmatched", 30030)
    assert len(calls) == 2
    assert env["investigation"]["complete"] is True
    out_claim = env["data"]["claims"][0]
    assert out_claim["status"] == "inferred"
    assert "根拠が調査台帳に無い" in out_claim["reason"]
    assert env["investigation"]["claims_check"] == {
        "ledger": True, "checked": 1, "downgraded": 1, "unmatched_refs": 1, "manifest_state": "valid"}
    assert env["limits"]["claims_unmatched"] is True
    assert env["headline"].startswith(structured_mod._DEMOTED_CLAIMS_NOTE)


def test_confirmed_claim_with_matched_ledger_ref_stays_confirmed(tmp_path, monkeypatch):
    claim = {"id": "c1", "status": "confirmed", "text": "対象の値は42です。",
             "evidence_refs": ["src/a.py:1"], "reason": "", "reason_code": ""}
    ledger = {"manifest": _manifest(["a"]), "items": {"a": _ok("a")}}
    steps = [_st("TH-CLAIMS-LEDGER-OK", _final_with_claims("対象の値は42です。", [claim]), ledger=ledger)]
    _, env, _ = _t(tmp_path, monkeypatch, steps, "claims-ledger-matched", 30031)
    assert env["data"]["claims"][0]["status"] == "confirmed"
    assert env["investigation"]["claims_check"]["downgraded"] == 0
    assert env["limits"]["claims_unmatched"] is False
    assert not env["headline"].startswith(structured_mod._DEMOTED_CLAIMS_NOTE)


# ===== 素の Codex（plain）では台帳ゲートが効かず、退避台帳に触れない =====

def test_plain_mode_turn_keeps_retained_ledger_of_standard(tmp_path, monkeypatch):
    uid, cid = "ledger-plain-keep", 30032
    _, env1, _ = _t(tmp_path, monkeypatch, [_st("TH-LP1", _final("途中です。"), ledger=_open())],
                    uid, cid, message="通常の依頼です")
    assert env1["investigation"]["retained"] is True
    retired = _retired(tmp_path, uid, cid)
    assert retired.is_dir()
    steps2 = [{"thread_id": "TH-LP2", "agent_messages": ["素の回答です。"], "usage": helper._usage()}]
    _t(tmp_path, monkeypatch, steps2, uid, cid, message="別の依頼です", log="argv2.log",
       prov=A.CodexProvider(system_settings={"codex_mode": "plain"}))
    assert retired.is_dir()


# ===== 確認できなかった項目（coverage.jsonl による降格・回答末尾の節） =====

@pytest.mark.parametrize("label,coverage,counts,present,absent", [
    ("truncated", [{"item": "a", "tool": "ripgrep_search", "outcome": "truncated", "ts": 1.0}],
     {"unverified": 1}, "対象（検索が上限に達し、途中までしか確認できませんでした）", "探したが無かった"),
    ("no_hits", [{"item": "a", "tool": "ripgrep_search", "outcome": "no_hits", "ts": 1.0}],
     {"not_found_in_scope": 1}, "対象（登録範囲内では見つかりませんでした）", None),
    ("no_coverage", None, {"unverified": 1}, "対象（この項目を調べた記録がありません）", None),
    ("error", [{"item": "a", "tool": "es_search", "outcome": "error", "ts": 1.0}],
     {"unverified": 1}, "対象（検索が失敗し、確認できませんでした）", "上限に達し"),
])
def test_not_found_in_scope_is_downgraded_by_coverage_and_listed_in_section(
        tmp_path, monkeypatch, label, coverage, counts, present, absent):
    """検索の記録（切り詰め・0 件・記録なし・道具の失敗）に応じて not_found_in_scope を降格し、
    「確認できなかった項目」節に定型文で載せる（モデルの自由記述の reason は転記しない）。"""
    ledger = {"manifest": _manifest(["a"]),
              "items": {"a": _item("a", "not_found_in_scope", reason="探したが無かった")}}
    if coverage:
        ledger["coverage"] = coverage
    if label == "truncated":
        ledger["reviews"] = [_review()]
    _, env, _ = _t(tmp_path, monkeypatch, [_st(f"TH-COD16-{label}", _final("対象は見つかりませんでした。"), ledger=ledger)],
                   f"cod16-{label}", 31110)
    if label == "truncated":
        assert env["investigation"]["complete"] is True
        assert "確認できなかった項目:" in env["headline"]
    assert env["investigation"]["counts"] == counts
    assert present in env["headline"]
    if absent:
        assert absent not in env["headline"]


def test_all_confirmed_items_do_not_append_unconfirmed_section(tmp_path, monkeypatch):
    _, env, _ = _t(tmp_path, monkeypatch, [_st("TH-COD16-D", _final("確認できました。"), ledger=_complete())],
                   "cod16-d", 31104)
    assert env["investigation"]["complete"] is True
    assert "確認できなかった項目" not in env["headline"]


def test_unconfirmed_section_includes_non_terminal_and_missing_items():
    """未完了のまま受理されたターンでは、非終端（pending/in_progress）と item ファイル無し（missing）も
    「調べ終わっていません」で節に載る。"""
    items = {"a": _item("a", "not_found_in_scope", reason="探したが無かった"),
             "b": _pend("b"), "c": _item("c", "in_progress")}   # "d" は item ファイル無し
    section = ledger_gate_mod._unconfirmed_items_section(
        IL.LedgerSnapshot(manifest=_manifest(["a", "b", "c", "d"]), items=items, invalid_ids=()))
    assert "対象（登録範囲内では見つかりませんでした）" in section
    assert "- 対象（調べ終わっていません）" in section
    assert section.count("調べ終わっていません") == 3
    assert "- d（調べ終わっていません）" in section


# ===== 見直しの一巡（complete 直後に最大 2 回・目録が増えたら台帳継続へ戻る） =====

def test_review_round_asked_once_when_complete_and_no_growth(tmp_path, monkeypatch):
    steps = [_st("TH-REV1", _final("初回の結論です。"), ledger=_complete()),
             _st("TH-REV1", _final("見直しましたが変更ありません。"))]
    events, env, calls = _t(tmp_path, monkeypatch, steps, "review-once", 32001)
    assert len(calls) == 2
    assert _review_nodes(events) == ["ledger-review-1"]
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["review"] == {"attempted": True, "rounds": 0, "items_added": 0}
    assert env["headline"] == "見直しましたが変更ありません。"


def test_review_round_growth_returns_to_investigation_then_reviews_again(tmp_path, monkeypatch):
    steps = [_st("TH-REV2", _final("初回の結論です。"), ledger=_complete()),
             _st("TH-REV2", _final("見直したところ b が必要でした。"),
                 ledger={"manifest": _manifest(["a", "b"]), "items": {"b": _pend("b")}}),
             _st("TH-REV2", _final("bも確認しました。"), ledger={"items": {"b": _ok("b")}}),
             _st("TH-REV2", _final("これ以上の見直しはありません。"))]
    events, env, calls = _t(tmp_path, monkeypatch, steps, "review-growth", 32002)
    assert len(calls) == 4
    assert _review_nodes(events) == ["ledger-review-1", "ledger-review-2"]
    inv = env["investigation"]
    assert inv["review"] == {"attempted": True, "rounds": 1, "items_added": 1}
    assert inv["continuations"] == 1 and inv["complete"] is True


def test_review_round_stops_asking_after_cap_reached(tmp_path, monkeypatch):
    """目録が増えた見直しが上限（2）に達したら、以後は見直しの一巡を頼まない。"""
    steps = [_st("TH-REV3", _final("初回の結論です。"), ledger=_complete()),
             _st("TH-REV3", _final("bが必要でした。"), ledger={"manifest": _manifest(["a", "b"]), "items": {"b": _pend("b")}}),
             _st("TH-REV3", _final("bも確認しました。"), ledger={"items": {"b": _ok("b")}}),
             _st("TH-REV3", _final("cも必要でした。"),
                 ledger={"manifest": _manifest(["a", "b", "c"]), "items": {"c": _pend("c")}}),
             _st("TH-REV3", _final("cも確認しました。"), ledger={"items": {"c": _ok("c")}})]
    events, env, calls = _t(tmp_path, monkeypatch, steps, "review-cap", 32003)
    assert len(calls) == 5
    assert _review_nodes(events) == ["ledger-review-1", "ledger-review-2"]
    assert env["investigation"]["review"] == {"attempted": True, "rounds": 2, "items_added": 2}
    assert env["investigation"]["complete"] is True


def test_review_round_reverts_to_pre_review_candidate_when_answer_gets_worse(tmp_path, monkeypatch):
    """見直し後の回答が空に悪化したら、見直し前の候補を使う（claims が空で優先ヒューリスティックが効かないケース）。"""
    steps = [_st("TH-REV4", _final("対象は Y です。"), ledger=_complete()), _st("TH-REV4", _final(""))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "review-revert", 32004)
    assert len(calls) == 2
    assert "対象は Y です。" in env["headline"]
    assert "見直し前の回答に戻しました" in env["headline"]
    assert env["investigation"]["review"] == {"attempted": True, "rounds": 0, "items_added": 0}


def test_review_round_does_nothing_in_plain_mode(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-REV-PLAIN", "agent_messages": ["素の回答です。"], "usage": helper._usage()}]
    events, env, calls = _t(tmp_path, monkeypatch, steps, "review-plain", 32005,
                            prov=A.CodexProvider(system_settings={"codex_mode": "plain"}))
    assert len(calls) == 1
    assert _review_nodes(events) == []
    assert env.get("investigation") is None


def test_review_round_broken_json_does_not_mark_stopped_early(tmp_path, monkeypatch):
    """見直しが構造化スキーマに合わない平文で終わり目録も増えなければ、実行前の状態へ戻す（途中停止扱いにしない）。"""
    steps = [_st("TH-REV5", _final("初回の結論です。"), ledger=_complete()),
             {"thread_id": "TH-REV5", "agent_messages": ["これは構造化出力のスキーマに合わない平文です。"],
              "usage": helper._usage()}]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "review-broken-json", 32006)
    assert len(calls) == 2
    assert "初回の結論です。" in env["headline"]
    assert "見直し前の回答に戻しました" in env["headline"]
    assert not env.get("codex_stopped_early") and not env.get("codex_silent_failure")


def test_review_round_reopened_ledger_without_growth_returns_to_gate_not_cap(tmp_path, monkeypatch):
    """見直しが目録を増やさず既存 item を差し戻したら通常の台帳ゲートへ戻る（打ち切り理由を cap にしない）。"""
    steps = [_st("TH-REV6", _final("初回の結論です。"), ledger=_complete()),
             _st("TH-REV6", _final("見直し中に確認が必要になりました。"), ledger={"items": {"a": _item("a", "in_progress")}}),
             _st("TH-REV6", _final("確認できました。"), ledger={"items": {"a": _ok("a")}}),
             _st("TH-REV6", _final("これ以上の見直しはありません。"))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "review-reopen", 32007)
    assert len(calls) == 4
    inv = env["investigation"]
    assert inv["complete"] is True and inv["stopped_reason"] == "complete" and inv["continuations"] == 1
    assert inv["review"] == {"attempted": True, "rounds": 0, "items_added": 0}


def test_review_round_growth_reverts_to_pre_review_candidate_when_followup_stalls(tmp_path, monkeypatch):
    """見直しで目録が増えた後の台帳継続が無進捗で打ち切られたら、見直し前の完成回答へ切り戻す。"""
    steps = [_st("TH-REV7", _final("初回の結論です。"), ledger=_complete()),
             _st("TH-REV7", _final("見直したところ b が必要でした。"),
                 ledger={"manifest": _manifest(["a", "b"]), "items": {"b": _pend("b")}}),
             _st("TH-REV7", _final("")), _st("TH-REV7", _final(""))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "review-growth-stall", 32008)
    assert len(calls) == 4
    assert env["investigation"]["stopped_reason"] == "no_progress"
    assert env["investigation"]["review"] == {"attempted": True, "rounds": 1, "items_added": 1}
    assert "初回の結論です。" in env["headline"]
    assert "見直し前の回答に戻しました" in env["headline"]
    assert "対象（調べ終わっていません）" in env["headline"]


def test_review_round_reopening_every_time_is_capped_by_requests(tmp_path, monkeypatch):
    """見直しのたびに台帳を未完了へ戻しても、見直しを頼むのは上限（2 回）まで。"""
    reopen = {"items": {"a": _item("a", "in_progress")}}
    close = {"items": {"a": _ok("a")}, "reviews": [_review()]}
    steps = [_st("TH-REV8", _final("初回の結論です。"), ledger={"manifest": _manifest(["a"]), **close}),
             _st("TH-REV8", _final("見直し1。"), ledger=reopen), _st("TH-REV8", _final("確認1。"), ledger=close),
             _st("TH-REV8", _final("見直し2。"), ledger=reopen), _st("TH-REV8", _final("確認2。"), ledger=close),
             _st("TH-REV8", _final("頼まれていない見直し。"), ledger=reopen)]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "review-reopen-cap", 32009)
    assert len(calls) == 5
    assert env["investigation"]["complete"] is True and env["investigation"]["continuations"] == 2


def test_review_round_growth_reverts_when_followup_ends_in_progress(tmp_path, monkeypatch):
    steps = [_st("TH-REV9", _final("初回の結論です。"), ledger=_complete()),
             _st("TH-REV9", _final("b が必要でした。"),
                 ledger={"manifest": _manifest(["a", "b"]), "items": {"b": _pend("b")}}),
             _st("TH-REV9", _inprog("b を調べています。")), _st("TH-REV9", _inprog("b を調べています。"))]
    _, env, _ = _t(tmp_path, monkeypatch, steps, "review-growth-inprog", 32010)
    assert "初回の結論です。" in env["headline"]
    assert "見直し前の回答に戻しました" in env["headline"]
    assert not env.get("codex_stopped_early")


def test_review_round_growth_then_worse_followup_reverts_before_second_review(tmp_path, monkeypatch):
    """見直しで目録が増えた後の台帳継続が主張の減った回答で完了したら、2 回目の見直しを頼まず見直し直前の回答へ戻す。"""
    def claim(cid, text):
        return {"id": cid, "status": "confirmed", "text": text, "evidence_refs": ["src/a.py:1"],
                "reason": "", "reason_code": "", "evidence_kinds": ["source"]}
    three = [claim("c1", "対象は X"), claim("c2", "対象は Y"), claim("c3", "対象は Z")]
    steps = [_st("TH-REV10", _final_with_claims("初回の結論です（X・Y・Z）。", three), ledger=_complete()),
             _st("TH-REV10", _final_with_claims("b が必要でした（X・Y・Z）。", three),
                 ledger={"manifest": _manifest(["a", "b"]), "items": {"b": _pend("b")}}),
             _st("TH-REV10", _final_with_claims("b だけの回答です。", [claim("c9", "b は W")]),
                 ledger={"items": {"b": _ok("b")}}),
             _st("TH-REV10", _final_with_claims("頼まれていない見直し。", [claim("c9", "b は W")]))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "review-growth-worse", 32011)
    assert len(calls) == 3
    assert "初回の結論です（X・Y・Z）。" in env["headline"]
    assert "b だけの回答です。" in env["headline"]
    assert "見直し前の回答に戻しました" in env["headline"]
    assert "b が必要でした（X・Y・Z）。" in env["headline"]
    assert env["completion"] == "partial"


def test_review_round_growth_ending_in_progress_does_not_ask_second_review(tmp_path, monkeypatch):
    steps = [_st("TH-REV11", _final("初回の結論です。"), ledger=_complete()),
             _st("TH-REV11", _inprog("b を足しました。", "答え直します"),
                 ledger={"manifest": _manifest(["a", "b"]), "items": {"b": _ok("b")}}),
             _st("TH-REV11", _inprog("頼まれていない見直し。"))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "review-growth-inprog2", 32012)
    assert len(calls) == 2
    assert "初回の結論です。" in env["headline"]
    assert not env.get("codex_stopped_early")


# ===== 中間の見直し（`ledger_review_put`）の台帳ゲートへの組み込み =====

def _extra_review_ledger(extra=("帳票",)) -> dict:
    return {**_complete(), "reviews": [_review(verdict="mostly_answered", extra_perspectives=list(extra))]}


def _retire_with_extra_perspectives(tmp_path, monkeypatch, uid, cid, tid):
    """前ターン: mostly_answered＋extra_perspectives の見直しで complete になり、台帳が退避された状態を作る。"""
    steps = [_st(tid, _final("本体の結論です。"), ledger=_extra_review_ledger()),
             _st(tid, _final("最終点検も変更ありません。"))]
    _, env, _ = _t(tmp_path, monkeypatch, steps, uid, cid)
    assert env["investigation"]["retained"] is True


def test_ledger_review_missing_blocks_completion_until_review_put(tmp_path, monkeypatch):
    """全 item が終端でも見直しが無ければ complete にならず、継続プロンプトに見直しを促す一文が載る。"""
    steps = [_st("TH-MIDREV-1", _final("全項目は確認済みです。"),
                 ledger={"manifest": _manifest(["a"]), "items": {"a": _ok("a")}}),
             _st("TH-MIDREV-1", _final("見直しも書きました。"), ledger={"reviews": [_review()]}),
             _st("TH-MIDREV-1", _final("最終点検も変更ありません。"))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "midreview-missing", 32101)
    assert len(calls) == 3
    assert "ledger_review_put" in calls[1][-1]
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["mid_review"] == {"count": 1, "missing": False, "pending": []}


def test_ledger_review_added_item_not_terminal_keeps_incomplete(tmp_path, monkeypatch):
    """見直しが added_items に挙げた item が未終端なら complete にならず、終端化した attempt で受理される。"""
    steps = [_st("TH-MIDREV-2", _final("b を見つけました。"), ledger={
                 "manifest": _manifest(["a"]), "items": {"a": _ok("a"), "b": _pend("b")},
                 "reviews": [_review(added_items=[{"id": "b", "reason": "見直しで発見"}])]}),
             _st("TH-MIDREV-2", _final("b も確認しました。"),
                 ledger={"manifest": _manifest(["a", "b"]), "items": {"b": _ok("b")}}),
             _st("TH-MIDREV-2", _final("見直し済みです。"))]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "midreview-pending", 32102)
    assert len(calls) == 3
    assert "b" in calls[1][-1]
    assert env["investigation"]["complete"] is True


def test_mostly_answered_extra_perspectives_appends_note_and_force_retains_ledger(tmp_path, monkeypatch):
    """最後の見直しが mostly_answered＋extra_perspectives なら、回答末尾に定型文が付き complete でも台帳を退避する。"""
    uid, cid = "midreview-extra", 32103
    steps = [_st("TH-MIDREV-3", _final("本体の結論です。"), ledger=_extra_review_ledger()),
             _st("TH-MIDREV-3", _final("最終点検も変更ありません。"))]
    _, env, _ = _t(tmp_path, monkeypatch, steps, uid, cid)
    assert "追加で調べられる観点: 帳票。続けて調べる場合は『続き』と送ってください。" in env["headline"]
    assert env["investigation"]["complete"] is True
    assert env["investigation"]["retained"] is True
    assert (_retired(tmp_path, uid, cid) / "reviews.jsonl").is_file()


def test_continue_prefix_injects_prior_extra_perspectives_into_next_prompt(tmp_path, monkeypatch):
    uid, cid = "midreview-continue", 32104
    _retire_with_extra_perspectives(tmp_path, monkeypatch, uid, cid, "TH-MIDREV-4A")
    steps2 = [_st("TH-MIDREV-4B", _final("続きの結論です。")), _st("TH-MIDREV-4B", _final("続きの見直しも変更ありません。"))]
    _, env2, calls2 = _t(tmp_path, monkeypatch, steps2, uid, cid, message="続きをお願いします", log="argv2.log")
    assert "前回の見直しで追加に調べられるとした観点: 帳票" in calls2[0][-1]
    assert "ledger_manifest_set に足してから調べてください" in calls2[0][-1]
    assert env2["investigation"]["restored"] is True


def test_continue_without_new_review_is_not_accepted_until_added_item_terminalizes(tmp_path, monkeypatch):
    """「続き」ターンは、新しい見直しの added_items が終端になるまで complete にしない（何も調べず final を返しても受理しない）。"""
    uid, cid = "midreview-continue-gate", 32105
    _retire_with_extra_perspectives(tmp_path, monkeypatch, uid, cid, "TH-MIDREV-GATE-1")
    _, env2, _ = _t(tmp_path, monkeypatch, [_st("TH-MIDREV-GATE-2", _final("調べずに結論します。"))],
                    uid, cid, message="続きをお願いします", log="argv2.log")
    assert env2["investigation"]["restored"] is True
    assert env2["investigation"]["complete"] is False
    assert env2["investigation"]["mid_review"]["missing"] is True

    steps3 = [_st("TH-MIDREV-GATE-3", _final("帳票も確認しました。"), ledger={
                  "manifest": _manifest(["a", "b"]), "items": {"b": _ok("b")},
                  "reviews": [_review(verdict="mostly_answered", added_items=[{"id": "b", "reason": "帳票を調べた"}])]}),
              _st("TH-MIDREV-GATE-3", _final("最終点検も変更ありません。"))]
    _, env3, _ = _t(tmp_path, monkeypatch, steps3, uid, cid, message="続きをお願いします", log="argv3.log")
    assert env3["investigation"]["complete"] is True


def test_continue_with_empty_added_items_review_does_not_satisfy_the_gate(tmp_path, monkeypatch):
    """「続き」で新しい見直しを書いても added_items が空のままなら要件を満たさない（件数だけ増やして回避できない）。"""
    uid, cid = "midreview-empty-added", 32106
    _retire_with_extra_perspectives(tmp_path, monkeypatch, uid, cid, "TH-MIDREV-EMPTY-1")
    steps2 = [_st("TH-MIDREV-EMPTY-2", _final("見直しましたが追加は見つかりませんでした。"),
                  ledger={"reviews": [_review(verdict="insufficient")]})]
    _, env2, _ = _t(tmp_path, monkeypatch, steps2, uid, cid, message="続きをお願いします", log="argv2.log")
    assert env2["investigation"]["mid_review"]["count"] >= 2
    assert env2["investigation"]["complete"] is False
    assert env2["investigation"]["mid_review"]["missing"] is True


def test_continue_obligation_survives_an_intervening_insufficient_review(tmp_path, monkeypatch):
    """「続き」の途中で insufficient＋added_items 空の見直しを挟んでも、元の義務（追加の観点）は「最後の見直し」に
    埋もれず、次の「続き」の注入文に残り、added_items を持つ見直しを終端化すれば解除される。"""
    uid, cid = "midreview-obligation-survives", 32107
    _retire_with_extra_perspectives(tmp_path, monkeypatch, uid, cid, "TH-OBLIGATION-0")
    steps1 = [_st("TH-OBLIGATION-1", _final("見直しましたが追加は見つかりませんでした。"),
                  ledger={"reviews": [_review(verdict="insufficient")]})]
    _, env1, _ = _t(tmp_path, monkeypatch, steps1, uid, cid, message="続きをお願いします", log="argv1.log")
    assert env1["investigation"]["complete"] is False

    steps2 = [_st("TH-OBLIGATION-2A", _final("まだ調べていません。")),
              _st("TH-OBLIGATION-2B", _final("帳票も確認しました。"), ledger={
                  "manifest": _manifest(["a", "b"]), "items": {"b": _ok("b")},
                  "reviews": [_review(verdict="insufficient", added_items=[{"id": "b", "reason": "帳票を調べた"}])]})]
    _, env2, calls2 = _t(tmp_path, monkeypatch, steps2, uid, cid, message="続きをお願いします", log="argv2.log")
    assert "前回の見直しで追加に調べられるとした観点: 帳票" in calls2[0][-1]
    assert env2["investigation"]["complete"] is True


@pytest.mark.parametrize("followup", [
    {"exit_code": 1, "extra_events": [{"type": "turn.failed", "error": {"code": "test_failure"}}]},
    {"exit_code": 1},
    {"agent_messages": [_final("")], "exit_code": 1},
])
def test_rejected_final_survives_followup_failure(tmp_path, monkeypatch, followup):
    steps = [_st("TH-PARTIAL", _final("回収済みの結論です。"), ledger=_open()),
             followup]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "partial-ledger", 33001)
    assert "resume" in calls[1]
    assert "回収済みの結論です。" in env["headline"]
    assert "台帳の確認が終わる前" in env["headline"]
    assert "失敗" in env["headline"]
    assert env["completion"] == "partial"
    assert not env["investigation"]["complete"]


def test_rejected_final_keeps_progress_and_broken_followup_as_notices(tmp_path, monkeypatch):
    original = "回収済みの結論です。対象はサンプル処理です。"
    progress = "続きで確認した補足です。詳細は未確認です。"
    broken = "続きで見つけた候補です。確認は終わっていません。"
    steps = [_st("TH-PARTIAL-CONTENT", _final(original), ledger=_open()),
             _st("TH-PARTIAL-CONTENT", _inprog(progress),
                 '{"answer": ' + json.dumps(broken, ensure_ascii=False) + ', "claims": [',
                 exit_code=1, extra_events=[{"type": "turn.failed", "error": {"code": "test_failure"}}])]
    _, env, calls = _t(tmp_path, monkeypatch, steps, "partial-content", 33002)
    assert len(calls) == 2
    headline = env["headline"]
    # 本文は差し戻された回答のまま・注記は別の欄（headline は注記＋本文の投影）。
    assert env["body"] == original and env["answer_schema"] == 2
    kinds = {n["kind"] for n in env["notices"]}
    assert {"ledger_unfinished", "partial_followup", "answer_recovery"} <= kinds
    assert "続きの調査で得た途中の内容" in headline and "失敗" in headline
    assert progress in headline and broken in headline
    assert headline.count(progress) == headline.count(broken) == 1
    assert headline.endswith(original)
    assert env["completion"] == "partial" and not env["investigation"]["complete"]


def test_review_rollback_keeps_answer_read_only_from_last_message_file(tmp_path):
    from sherpa.providers.codex.turn_candidates import _pick_structured_headline, _update_structured_state
    from sherpa.providers.codex.turn_loop import _revert_review_if_worse
    from sherpa.providers.codex.turn_state import CodexTurnState

    st = CodexTurnState(helper._ctx("review-file", 33003), turn_t0=0, plain=False, skip_presearch=True)
    st._schema_on = st._schema_v2 = True
    st._last_message_path = tmp_path / "last-message.txt"
    original, review = "回収済みの結論です。", "見直しで別の確認点を見つけました。"
    st._last_message_path.write_text(_final(original), encoding="utf-8")
    _update_structured_state(st)
    st._ledger_review_pre_candidate = st._latest_structured
    st._ledger_review_pre_latest_structured = st._latest_structured
    st._ledger_review_pre_len = len(st._structured_answers)
    st._last_message_path.write_text(_final(review), encoding="utf-8")
    _update_structured_state(st)
    assert not st._agent_msgs and not st._agent_partial
    st._turn_failed = True
    _revert_review_if_worse(st)
    assert _pick_structured_headline(st) == original
    assert [k for k, _ in st._answer_notices] == ["review_reverted"]
    note = "\n\n".join(t for _, t in st._answer_notices)
    assert "見直し前の回答に戻しました" in note
    assert "採用しなかった見直しの内容（未確定）" in note
    assert note.count(review) == 1 and "回収できませんでした" not in note


def test_unconfirmed_items_are_returned_from_the_ledger_even_when_no_body_was_picked(tmp_path, monkeypatch):
    """回答の本文を選べなかったターンでも、台帳から作った未確認の一覧と「調べた範囲」を返す。"""
    ledger = {"manifest": _manifest(["a"]),
              "items": {"a": _item("a", "not_found_in_scope", reason="探したが無かった")}}
    events, env, _ = _t(tmp_path, monkeypatch, [_st("TH-COD25-NOBODY", ledger=ledger)], "cod25-nobody", 31125)
    assert env["investigation"]["unconfirmed_items"] == [
        {"item": "対象", "reason": "この項目を調べた記録がありません"}]
    assert any(n["kind"] == "unconfirmed_items" for n in env["notices"])
    assert any(i["label"] == "確認できなかった項目" for i in env["investigation_summary"]["items"])
