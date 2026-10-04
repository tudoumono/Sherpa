"""Codex の 1 ターン（`CodexProvider.run`）を偽 codex で実際に動かして確かめる契約テスト。

起動の形（引数・作業ディレクトリ・標準入力・設定の書き出し順）・成果物の台帳登録・回答の選び方・
ask_user の確認カード・利用統計・表示ラベル・グラフ世代不一致の扱いを、起動した偽 codex が見た状態と
`run()` が返したイベントで検証する。偽 codex は起動時に自分の見た状態（引数・cwd・標準入力・設定・AGENTS.md・
配備済みスキル）を JSON 1 行で記録する。
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import test_codex_workspace_authoring as H  # noqa: E402
from sherpa import agents as A  # noqa: E402
from sherpa import codex_agents_md, codex_skills  # noqa: E402

_PRELUDE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
_args = sys.argv[1:]
_prompt = sys.stdin.read() if _args[-1:] == ["-"] else ""
_cwd = pathlib.Path.cwd()
_ch = os.environ.get("CODEX_HOME")
_cfg = pathlib.Path(_ch) / "config.toml" if _ch else None
_agents = _cwd / "AGENTS.md"
_skills = _cwd / ".agents" / "skills"
_rec = {
    "argv": _args, "prompt": _prompt, "cwd": str(_cwd),
    "session_leader": os.getsid(0) == os.getpid(),
    "env_keys": sorted(os.environ), "codex_home": _ch,
    "config": _cfg.read_text() if _cfg is not None and _cfg.is_file() else None,
    "agents_md": _agents.read_text() if _agents.is_file() else None,
    "skill_dirs": sorted(p.name for p in _skills.iterdir()) if _skills.is_dir() else [],
    "sid": os.getsid(0),
}
with open(r"__REC__", "a", encoding="utf-8") as _f:
    _f.write(json.dumps(_rec, ensure_ascii=False) + "\n")
'''


def _o_path() -> str:
    """偽 codex の本文側で `-o` の出力先を得る式。"""
    return "_args[_args.index('-o') + 1]"


def _run(tmp_path, monkeypatch, body: str, uid: str, *, lens="qa", sandbox=None,
         prov=None, **ctx_kw):
    """偽 codex（記録つき）で 1 ターン実行して (events, env, 起動記録のリスト) を返す。"""
    rec_path = tmp_path / "launch.jsonl"
    script = _PRELUDE.replace("__REC__", str(rec_path)) + body
    H._install_codex(tmp_path, monkeypatch, script, sandbox=sandbox)
    events = list((prov or A.CodexProvider()).run(H._ctx(uid, lens=lens, **ctx_kw)))
    recs = ([json.loads(ln) for ln in rec_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if rec_path.exists() else [])
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    env = results[0]["env"] if results else None
    return events, env, recs


@pytest.fixture
def registry(monkeypatch):
    """成果物の台帳登録（DB）を、呼び出しを記録する偽へ差し替える。戻り値は登録行のリスト。"""
    from sherpa import store
    rows: list = []

    @contextlib.contextmanager
    def _lock(_uid, _rel):
        yield

    def _record(uid, rel, path, size, sha, expires_at=None):
        row = {"id": 100 + len(rows), "rel_path": rel}
        rows.append((uid, rel, path))
        return row

    monkeypatch.setattr(store, "workspace_file_lock", _lock)
    monkeypatch.setattr(store, "no_live_upload_for_path", lambda *a, **k: True)
    monkeypatch.setattr(store, "record_workspace_file", _record)
    return rows


# ===== 起動の形 =====

def test_codex_is_launched_in_run_dir_with_config_and_prompt_on_stdin(tmp_path, monkeypatch):
    """作業領域 `authoring/run-*` を cwd にして独立セッションで起動し、プロンプトは標準入力で渡す。
    設定（config.toml）と AGENTS.md は起動時点で書き出し済みで、親の環境変数は子へ渡らない。"""
    monkeypatch.setenv("SHERPA_PARENT_ENV_PROBE", "leak-me")
    events, env, recs = _run(tmp_path, monkeypatch, H._msg("確認しました。"), "launch-u1",
                             message="税率の仕様を教えて")
    assert env["headline"] == "確認しました。"
    assert len(recs) == 1
    r = recs[0]
    argv = r["argv"]
    assert argv[:2] == ["exec", "--json"] and "--strict-config" in argv and "--skip-git-repo-check" in argv
    assert "--ephemeral" in argv                      # 会話 id が無いターンはセッションを残さない
    assert not any("read-only" in a for a in argv)    # 読取の封じ込めは permission profile（引数の -s read-only ではない）
    assert argv[-1] == "-" and "税率の仕様を教えて" in r["prompt"]
    assert not any("税率の仕様を教えて" in a for a in argv)   # 本文は argv（ps）に載せない
    assert argv[argv.index("-C") + 1] == r["cwd"]
    run_dir = pathlib.Path(r["cwd"])
    users = tmp_path / "users"
    assert run_dir.parent == users.resolve() / "launch-u1" / "workspace" / "authoring"
    assert run_dir.name.startswith("run-") and run_dir.name != "files"
    assert pathlib.Path(argv[argv.index("-o") + 1]).is_relative_to(run_dir)
    assert (users / "launch-u1" / "workspace" / "files").is_dir()
    assert r["session_leader"] is True
    assert r["config"] and "[mcp_servers.sherpa]" in r["config"]   # 起動前に書き出し済み
    assert r["codex_home"] and not pathlib.Path(r["codex_home"]).is_relative_to(run_dir)
    assert r["agents_md"]
    assert "SHERPA_PARENT_ENV_PROBE" not in r["env_keys"] and "CODEX_HOME" in r["env_keys"]
    assert not run_dir.exists()                       # 実行後に作業領域を削除する


def test_codex_launch_without_sandbox_uses_workspace_write_and_no_config(tmp_path, monkeypatch):
    """サンドボックス無効のフォールバックは `-s workspace-write`・使い捨てセッションで、CODEX_HOME の設定を書かない。"""
    _, env, recs = _run(tmp_path, monkeypatch, H._msg("確認しました。"), "launch-fb-u1", sandbox="0")
    assert env["headline"] == "確認しました。"
    argv = recs[0]["argv"]
    assert argv[argv.index("-s") + 1] == "workspace-write" and "--ephemeral" in argv
    assert "--strict-config" not in argv
    assert recs[0]["config"] is None and recs[0]["agents_md"]


def test_last_message_file_is_used_when_no_agent_message_and_removed_after(tmp_path, monkeypatch):
    """`--json` に agent_message が無ければ `-o` の最終メッセージを回答にし、使い終えたファイルは残さない。"""
    body = f"import pathlib\npathlib.Path({_o_path()}).write_text('最終メッセージの本文')\n"
    _, env, recs = _run(tmp_path, monkeypatch, body, "lastmsg-u1")
    assert env["headline"] == "最終メッセージの本文"
    assert not list((tmp_path / "users").rglob("last-message-*.txt"))


def test_layer_filter_is_passed_to_mcp_only_for_qa(tmp_path, monkeypatch):
    """探す対象（層）の限定は qa のターンだけ MCP へ渡す（作成・トラブルシュートには渡さない）。"""
    sm = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    _, _, qa = _run(tmp_path, monkeypatch, H._msg("回答です。"), "layer-qa-u1", lens="qa", scope_meta=sm)
    assert 'SHERPA_MCP_LAYER = "code"' in qa[0]["config"]
    for lens in ("troubleshoot", "author"):
        sub = tmp_path / lens
        sub.mkdir()
        _, _, other = _run(sub, monkeypatch, H._msg("回答です。"), f"layer-{lens}-u1", lens=lens, scope_meta=sm)
        assert other[0]["config"] and "SHERPA_MCP_LAYER" not in other[0]["config"], lens


@pytest.mark.parametrize("schema_env,expected", [("0", False), ("2", True)])
def test_agents_md_and_argv_follow_output_schema_setting(tmp_path, monkeypatch, schema_env, expected):
    """出力スキーマの有無は `--output-schema` と AGENTS.md の生成引数で一致し、AGENTS.md は作業領域に書かれる。"""
    captured: dict = {}
    orig = codex_agents_md.write_agents_md

    def _spy(authoring, **kw):
        captured["authoring"] = str(authoring)
        captured.update(kw)
        return orig(authoring, **kw)

    monkeypatch.setattr(codex_agents_md, "write_agents_md", _spy)
    H._install_codex(tmp_path, monkeypatch, _PRELUDE.replace("__REC__", str(tmp_path / "launch.jsonl")) + H._msg("確認しました。"))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", schema_env)
    list(A.CodexProvider().run(H._ctx("schema-u1")))
    rec = json.loads((tmp_path / "launch.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert ("--output-schema" in rec["argv"]) is expected
    assert captured["output_schema"] is expected
    assert captured["authoring"] == rec["cwd"] and rec["agents_md"]


def test_skills_are_deployed_before_codex_starts(tmp_path, monkeypatch):
    """スキルの配備は codex の起動より前に完了している。"""
    rec_path = tmp_path / "launch.jsonl"
    seen_at_deploy: list = []
    orig = codex_skills.deploy_skills

    def _spy(*a, **kw):
        seen_at_deploy.append(rec_path.exists())      # この時点で偽 codex は未起動
        return orig(*a, **kw)

    monkeypatch.setattr(codex_skills, "deploy_skills", _spy)
    _, _, recs = _run(tmp_path, monkeypatch, H._msg("確認しました。"), "skills-u1")
    assert seen_at_deploy == [False]
    assert "xlsx" in recs[0]["skill_dirs"]


# ===== 成果物（作成したファイル）の台帳登録 =====

_WRITE_REPORT = "import pathlib\npathlib.Path('report.md').write_text('作成した本文')\n"


def test_created_file_is_registered_and_surfaced_as_card(tmp_path, monkeypatch, registry):
    """作成の依頼で作られたファイルは台帳へ登録（登録関数の戻り値＝行）され、env の作成ファイルカードが
    その行の rel_path と DL エンドポイントの形で組まれ、個人由来の印（codex_wrote_files）も立つ。"""
    _, env, _ = _run(tmp_path, monkeypatch, _WRITE_REPORT + H._msg("作成しました。"), "created-u1", lens="author")
    assert [(r[0], r[1]) for r in registry] == [("created-u1", "report.md")]
    assert env["created_files"] == [{"name": "report.md", "download_url": "/workspace/files/100/download"}]
    assert env["codex_wrote_files"] == ["report.md"]
    files_dir = tmp_path / "users" / "created-u1" / "workspace" / "files"
    assert (files_dir / "report.md").read_text(encoding="utf-8") == "作成した本文"   # files/ へ移されている


def test_no_created_files_card_when_nothing_registered(tmp_path, monkeypatch, registry):
    """何も作られなかった作成ターンでは、作成ファイルカードも個人由来の印も付かない。"""
    _, env, _ = _run(tmp_path, monkeypatch, H._msg("作成するものがありませんでした。"), "created-none-u1",
                     lens="author")
    assert registry == []
    assert "created_files" not in env and "codex_wrote_files" not in env


def test_agents_md_sidecar_and_skills_in_run_dir_are_not_registered_as_created_files(
        tmp_path, monkeypatch, registry):
    """作業領域にある AGENTS.md・MCP サイドカー・配備済みスキル（Codex が `.agents/` 配下へ書いた物も）は成果物にならず、
    同じターンで本当に作られたファイルだけが登録される。"""
    body = ("import pathlib\n"
            "pathlib.Path('.mcp_sidecar.jsonl').write_text('{}')\n"
            "pathlib.Path('.agents/skills/zzz').mkdir(parents=True, exist_ok=True)\n"
            "pathlib.Path('.agents/skills/zzz/leftover.txt').write_text('x')\n"
            "pathlib.Path('real.md').write_text('本物')\n")
    _, env, recs = _run(tmp_path, monkeypatch, body + H._msg("作成しました。"), "created-excl-u1", lens="author")
    assert recs[0]["agents_md"] and "xlsx" in recs[0]["skill_dirs"]    # 除外対象が実際に作業領域にあった
    assert [r[1] for r in registry] == ["real.md"]
    assert env["codex_wrote_files"] == ["real.md"]


# ===== 回答（headline）の選び方 =====

_CONCLUSION = ("税率テーブル(TAX_RATE)は夜間バッチ NIGHTLY.jcl から呼ばれる BATCH01 が参照しています。"
               "したがって税率変更は夜間バッチに波及します。")
_TRAILING_WORK = "念のため他の経路も洗い出します。根拠の有無を切り分けます。"


def test_headline_is_the_conclusion_not_the_last_agent_message(tmp_path, monkeypatch):
    """最後に届いた作業宣言で結論を上書きせず、結論を含む message を回答に選ぶ。"""
    body = H._msg(_CONCLUSION, "m1") + H._msg(_TRAILING_WORK, "m2")
    _, env, _ = _run(tmp_path, monkeypatch, body, "headline-u1")
    assert "波及します" in env["headline"]
    assert "根拠の有無を切り分けます" not in env["headline"]


_FILE_MESSAGE = "次に影響範囲を確認します。根拠の有無を切り分けます。"
_STREAM_BODY = (H._msg("税率変更は夜間バッチに波及します。", "m1")
                + f"import pathlib\npathlib.Path({_o_path()}).write_text({_FILE_MESSAGE!r})\n")


def test_stream_read_error_prefers_last_message_file_over_picked_message(tmp_path, monkeypatch):
    """出力の読取が途中例外で終わったターンは、message からの選択より `-o` の最終メッセージを先に採る
    （完全版が入り得るため）。読取が正常に終わったターンは逆に、message からの選択が主で `-o` は従。"""
    # message を読み終えた後に、UTF-8 として読めない出力を流して読取を例外終了させる（間を空けて別の読取にする）。
    broken = (_STREAM_BODY + "import sys, time\nsys.stdout.flush()\ntime.sleep(0.5)\n"
              "sys.stdout.buffer.write(b'\\xff\\xfe\\n')\nsys.stdout.buffer.flush()\n")
    _, env, _ = _run(tmp_path, monkeypatch, broken, "stream-err-u1")
    assert env["headline"] == _FILE_MESSAGE
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    _, env_ok, _ = _run(ok_dir, monkeypatch, _STREAM_BODY, "stream-ok-u1")
    assert env_ok["headline"] == "税率変更は夜間バッチに波及します。"


# ===== 推論レベル =====

def _reasoning_arg(recs) -> str:
    argv = recs[0]["argv"]
    return next(a.split("=", 1)[1] for a in argv if a.startswith("model_reasoning_effort="))


@pytest.mark.parametrize("profile,expected", [("quick", "medium"), ("standard", "high"), ("deep", "high"), ("max", "high")])
def test_reasoning_follows_admin_base_and_only_quick_steps_down(tmp_path, monkeypatch, profile, expected):
    """推論レベルは管理画面の基準値（ここでは high）で固定し、調べる深さではクイックだけ 1 段下げる。"""
    prov = A.CodexProvider(system_settings={"depth_base_codex_reasoning": "high"})
    sm = {"world": "v1", "scope_paths": [], "source": "all", "depth_profile": profile}
    _, _, recs = _run(tmp_path, monkeypatch, H._msg("確認しました。"), f"reason-{profile}-u1", prov=prov, scope_meta=sm)
    assert _reasoning_arg(recs) == expected


# ===== ask_user（確認カード） =====

_ASK_ARGS = {"mode": "single", "prompt": "対象範囲は？",
             "options": [{"id": "a", "label": "A案"}, {"id": "b", "label": "B案"}]}


def _ask_started(iid="q1") -> str:
    return H._emit({"type": "item.started", "item": {
        "id": iid, "type": "mcp_tool_call", "tool": "ask_user", "status": "in_progress", "arguments": _ASK_ARGS}})


def _nodes(events, node_id):
    return [e for e in events if isinstance(e, dict) and e.get("type") == "node" and e.get("id") == node_id]


def test_ask_user_ends_turn_with_question_before_any_result_or_file_registration(tmp_path, monkeypatch, registry):
    """ask_user を捕まえたターンは、その場で出力の読取を打ち切り（以降の出力は処理しない）、親ノードと確認ノードを
    done に確定してから確認カードだけを返して終わる。回答（_result）も成果物の台帳登録も出さず、`-o` の一時ファイルも残さない。"""
    body = (_WRITE_REPORT
            + f"import pathlib\npathlib.Path({_o_path()}).write_text('途中の本文')\n"
            + _ask_started()
            + H._emit({"type": "item.completed", "item": {
                "id": "after", "type": "command_execution", "command": "ls", "status": "completed", "exit_code": 0}})
            + H._msg("質問の後の本文です。"))
    events, env, _ = _run(tmp_path, monkeypatch, body, "ask-u1", lens="author")
    questions = [e for e in events if isinstance(e, dict) and e.get("type") == "question"]
    assert len(questions) == 1 and questions[0]["prompt"] == "対象範囲は？"
    assert [o["label"] for o in questions[0]["options"]] == ["A案", "B案"]
    assert env is None and registry == []                         # _result なし・台帳登録なし
    assert events[-1] is questions[0]
    ask_nodes = _nodes(events, "cx-q1")
    assert ask_nodes and all(n["label"] == "ユーザに確認" and n["status"] == "done" for n in ask_nodes)
    assert _nodes(events, "cx-after") == []                        # 質問の後の出力は読まない
    parent = [e for e in events if isinstance(e, dict) and e.get("type") == "node" and e.get("id") == "codex"]
    assert parent[-1]["status"] == "done"
    assert events.index(parent[-1]) < events.index(questions[0])
    assert not list((tmp_path / "users").rglob("last-message-*.txt"))


def test_ask_user_is_ignored_when_message_is_a_confirm_id_resend(tmp_path, monkeypatch):
    """前の確認への回答の再送（依頼に「確認ID:」）では ask_user を無視して調査を続け、MCP 側でもツールを隠す。"""
    msg = "選択: 対象範囲\n確認ID: ask-0011\n元の依頼: 消費税率の仕様"
    body = _ask_started() + H._msg("再質問せずに調べました。")
    events, env, recs = _run(tmp_path, monkeypatch, body, "ask-resend-u1", message=msg)
    assert not [e for e in events if isinstance(e, dict) and e.get("type") == "question"]
    assert env["headline"] == "再質問せずに調べました。"
    assert 'SHERPA_MCP_ASK_DISABLED = "1"' in recs[0]["config"]
    ctrl = tmp_path / "ctrl"
    ctrl.mkdir()
    _, _, recs2 = _run(ctrl, monkeypatch, H._msg("回答です。"), "ask-first-u1")
    assert "SHERPA_MCP_ASK_DISABLED" not in recs2[0]["config"]


# ===== 利用統計（usage） =====

_USAGE_EVENT = {"type": "turn.completed", "usage": {"input_tokens": 341026, "cached_input_tokens": 244864,
                                                    "output_tokens": 12318, "reasoning_output_tokens": 9392}}


def test_turn_completed_usage_lands_in_env_with_locality_by_construct(tmp_path, monkeypatch):
    """`turn.completed` の usage を回答の usage にし、担当バッジ（is_local）は接続先で決まる（OpenAI は cloud・Ollama は local）。"""
    body = H._msg("確認しました。") + H._emit(_USAGE_EVENT)
    _, env, _ = _run(tmp_path, monkeypatch, body, "usage-u1")
    u = env["usage"]
    assert (u["provider"], u["model"], u["is_local"]) == ("codex", "gpt-5.5", "cloud")
    assert (u["input_tokens"], u["cached_input_tokens"], u["output_tokens"], u["reasoning_output_tokens"]) == (
        341026, 244864, 12318, 9392)
    ol = tmp_path / "ollama"
    ol.mkdir()
    _, env2, _ = _run(ol, monkeypatch, body, "usage-ollama-u1",
                      prov=A.CodexProvider(ollama_base_url="http://127.0.0.1:11500/", model="gpt-oss:20b"))
    assert env2["usage"]["is_local"] == "local" and env2["usage"]["model"] == "gpt-oss:20b"


# ===== MCP ツール呼びの表示ノード =====

def _tool_item(tool: str, args: dict, iid: str, **extra) -> str:
    return H._mcp_call(tool, args, id=iid, **extra)


def test_tool_labels_for_folder_tree_compare_and_ledger_tools(tmp_path, monkeypatch):
    """folder_tree/compare_documents と調査台帳の 3 ツールは「その他の処理」に丸めず専用ラベルで出す。
    台帳ツールの補足は件数と状態語彙（閉集合）だけで、モデル生成の文字列は出さない。"""
    body = (_tool_item("folder_tree", {"path_prefix": "a"}, "t1")
            + _tool_item("compare_documents", {"doc_id_a": "a.md", "doc_id_b": "b.md"}, "t2")
            + _tool_item("ledger_manifest_set", {"question_kind": "list", "items": ["a", "b"]}, "t3")
            + _tool_item("ledger_item_put", {"id": "sel1", "subject": "秘密の本文", "status": "source_confirmed"}, "t4")
            + _tool_item("ledger_item_put", {"id": "sel2", "status": "<script>"}, "t5")
            + _tool_item("ledger_status", {}, "t6")
            + H._msg("確認しました。"))
    events, _, _ = _run(tmp_path, monkeypatch, body, "labels-u1")

    def last(iid):
        return _nodes(events, f"cx-{iid}")[-1]
    assert last("t1")["label"] == "フォルダ構成を確認"
    assert last("t2")["label"] == "世代間の差分を比較"
    assert (last("t3")["label"], last("t3")["detail"]) == ("調査台帳に項目を登録", "2件")
    assert (last("t4")["label"], last("t4")["detail"]) == ("調査台帳の項目を更新", "状態: source_confirmed")
    assert (last("t5")["label"], last("t5")["detail"]) == ("調査台帳の項目を更新", "")
    assert (last("t6")["label"], last("t6")["detail"]) == ("調査台帳の状態を確認", "")
    assert not any("秘密の本文" in json.dumps(e, ensure_ascii=False) for e in events)


def test_ledger_tool_details_are_closed_vocabulary_and_labels_are_counted_in_improvement_log():
    """台帳ツールの補足は件数と状態語彙だけ（不正な型・語彙外は空）。表示ラベルは改善ログの集計対象にも入っている。"""
    from sherpa import improvement_log
    from sherpa.providers.codex.ledger_gate import _LEDGER_TOOL_DETAILS as D
    assert D["ledger_manifest_set"]({"question_kind": "list", "items": ["a", "b"]}) == "2件"
    assert D["ledger_item_put"]({"id": "sel1", "subject": "秘密の本文", "status": "source_confirmed"}) == "状態: source_confirmed"
    assert D["ledger_item_put"]({"status": "<script>"}) == ""
    for bad in ([], {}, ["source_confirmed"], {"k": "v"}, 1, None):
        assert D["ledger_item_put"]({"status": bad}) == ""
    assert D["ledger_status"]({}) == ""
    assert {"調査台帳に項目を登録", "調査台帳の項目を更新", "調査台帳の状態を確認"} <= improvement_log._TOOL_CALL_LABELS


# ===== グラフ世代不一致 =====

def _graph_item(result: dict, iid: str) -> str:
    return _tool_item("graph_neighbors", {"name": "X"}, iid, result=result)


def test_graph_schema_era_mismatch_degrades_but_does_not_end_the_run(tmp_path, monkeypatch):
    """世代不一致（graph_reingest_required）は近傍として読まず、run を終端せずに調査を続け、縮退の印を env へ渡す。
    通常の結果は従来どおり近傍として読む（縮退の印は付かない）。"""
    from sherpa.providers.codex import codex_attempt
    calls: list = []
    orig = codex_attempt._mcp_neighbors_from

    def _spy(item):
        calls.append(item)
        return orig(item)

    monkeypatch.setattr(codex_attempt, "_mcp_neighbors_from", _spy)
    era = {"isError": True, "content": [{"type": "text", "text": json.dumps(
        {"error": "graph_reingest_required", "stored_era": "old"})}]}
    _, env, _ = _run(tmp_path, monkeypatch, _graph_item(era, "g1") + H._msg("原本を直接読んで調べました。"), "era-u1")
    assert calls == []
    assert env["graph_degraded"] == "graph_reingest_required"
    assert env["headline"] == "原本を直接読んで調べました。"

    ok = {"content": [{"type": "text", "text": json.dumps({"neighbors": [{"name": "Y"}]})}]}
    ctrl = tmp_path / "ctrl"
    ctrl.mkdir()
    _, env2, _ = _run(ctrl, monkeypatch, _graph_item(ok, "g2") + H._msg("近傍を確認しました。"), "era-ok-u1")
    assert len(calls) == 1
    assert "graph_degraded" not in env2


# ===== 後始末 =====

def test_leftover_child_processes_of_codex_are_killed_after_the_turn(tmp_path, monkeypatch):
    """codex が起動した子プロセス（同じプロセスグループ）を、codex 自身が終わった後も残さない。"""
    pid_file = tmp_path / "child.pid"
    body = ("import subprocess, pathlib\n"
            "_c = subprocess.Popen(['sleep', '60'])\n"
            f"pathlib.Path(r'{pid_file}').write_text(str(_c.pid))\n"
            + H._msg("確認しました。"))
    _, env, _ = _run(tmp_path, monkeypatch, body, "killpg-u1")
    pid = int(pid_file.read_text())
    try:
        deadline = time.time() + 10
        while time.time() < deadline and _alive(pid):
            time.sleep(0.05)
        assert not _alive(pid), "codex の子プロセスが残っている"
    finally:
        if _alive(pid):
            os.kill(pid, 9)
    assert env["headline"] == "確認しました。"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # ゾンビ（終了済み・未回収）は生存扱いにしない。
    try:
        state = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"
