"""台帳評価の誤採点・欠測のゼロ埋め・登録ルートへの正解混入を防ぐ。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import subprocess
import time
from types import SimpleNamespace

import pytest

from scripts.bench.ledger_eval import (
    API, LIMIT_RE, EvalError, Runner, StallTimeout, evidence_reach, extra_metrics, fixed_checkout, measure,
    predicted_paths, read_manifest, require_free_port, score_answer, stream_events, validate_run_conditions,
)
from scripts.bench import ledger_eval


@pytest.mark.parametrize("body,refs,expected", [
    ("code/source.cbl と code/invented.cbl", [{"path": "docs/extra.md"}],
     ["code/invented.cbl", "code/source.cbl", "docs/extra.md"]),
    ("`multihop/article name.md:4` と `article name.md:5`", [], ["multihop/article name.md"]),
    ("`source.cbl:2` と根拠", [{"path": "src/eval/code/source.cbl", "ranges": [[1, 2]]}],
     ["code/source.cbl"]),
])
def test_paths_include_hallucinated_and_non_gold_citations(body, refs, expected):
    corpus = ["code/source.cbl", "docs/extra.md", "multihop/article name.md"]
    answer = {"body": body, "referenced_docs": refs}
    assert predicted_paths(answer, corpus) == expected
    score = score_answer(answer, {"expected_paths": ["code/source.cbl"]}, corpus)
    if "code/invented.cbl" in expected:
        assert score["recall"] == 1
        assert score["precision"] == pytest.approx(1 / 3)


def test_empty_gold_and_empty_predictions_are_undefined_not_perfect_scores():
    score = score_answer({"body": "該当なし"}, {"expected_paths": [], "null": True}, ["docs/a.md"])
    assert score["precision"] is None
    assert score["recall"] is None
    assert score["null_answer"] is True
    assert score["null_no_evidence"] is True
    missing = measure({}, None, None)
    for key in ("unconfirmed_rate", "input_tokens", "resume_count", "spawn_count", "es_search_calls"):
        assert missing[key] is None


def test_resume_and_review_metrics_preserve_actual_counts_and_missing_values():
    activity = {"agents": [{"role": "parent", "tokens": {"input_tokens": 429},
                             "tools": {"spawn_agent": {"calls": 2}, "mcp__sherpa__es_search": {"calls": 3}}}]}
    call_stats = {"calls": 5, "missing": 0, "tools": [
        {"role": "parent", "tool": "es_search", "calls": 1}, {"role": "child", "tool": "es_search", "calls": 2},
        {"role": "child", "tool": "read_around", "calls": 2}]}
    answer = {"activity": activity, "call_stats": call_stats,
              "investigation": {"rows": 4, "unconfirmed_items": [{"id": "i2"}]}}
    stats = {"style": "plan_gate", "attempts": 5, "resume_count": 3,
             "plan": {"review_requests": 1, "review_rounds": 0}}
    result = measure(answer, stats, None)
    assert result["resume_count"] == 3
    assert result["review_requests"] == 1
    assert result["review_rounds"] == 0
    assert result["unconfirmed_rate"] == .25
    assert result["spawn_count"] == 2
    assert result["es_search_calls"] == 3
    assert not LIMIT_RE.search(json.dumps(activity))
    assert LIMIT_RE.search('{"status_code":429}')
    assert LIMIT_RE.search("usage limit reached")
    assert not LIMIT_RE.search("道具の表示件数の上限に達したため、残りはファイルを読んだ")


def test_legacy_source_download_paths_and_unverified_citations_are_not_lost():
    answer = {"body": "架空の調査結果。", "codex_referenced_docs": {"listed": 2, "verified": 1},
              "sources": [{"doc_id": "article", "download_url": "/documents/download?world=eval&rel=docs%2Farticle.md"}],
              "sources_unverified": [{"path": "docs/invented.md", "reason": "確認できない"}]}
    score = score_answer(answer, {"expected_paths": ["docs/article.md"]}, ["docs/article.md"])
    assert score["recall"] == 1
    assert score["precision"] == .5


def test_manifest_rejects_gold_inside_registered_corpus(tmp_path: Path):
    root = tmp_path / "corpus"
    (root / "code").mkdir(parents=True)
    (root / "code/a.cbl").write_text("synthetic source", encoding="utf-8")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"corpus_root": str(root), "corpus_files": ["code/a.cbl"],
                               "questions": [{"id": "q1", "system": "code", "question": "架空の影響は？",
                                              "hop": 1, "expected_paths": ["code/a.cbl"]}]}), encoding="utf-8")
    first, digest = read_manifest(path)
    assert first["questions"][0]["scope_paths"] == ["code"]
    assert len(digest) == 64
    (root / "gold.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EvalError, match="corpus_files 外"):
        read_manifest(path)


@pytest.mark.parametrize("database,port", [("sherpa", 15433), ("postgres", 15433), ("sherpa_ledger_eval", 15432)])
def test_schema_guard_rejects_development_database_before_connecting(database, port):
    runner = SimpleNamespace(env={"PGDATABASE": database}, args=SimpleNamespace(pg_port=port))
    with pytest.raises(EvalError, match="承認対象"):
        Runner.check_database(runner)


def test_output_credentials_cannot_be_written_inside_the_registered_corpus(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    with pytest.raises(EvalError, match="重ねない"):
        Runner(SimpleNamespace(out=root / "results"), {"corpus_root": str(root)}, "digest")
    assert list(root.iterdir()) == []


def test_plan_trials_reject_json_fallback_and_missing_stats_but_keep_missing_plans():
    answer = {"activity": {"settings": {"model": "gpt-5.6-sol", "depth": "standard",
                                        "mode": "standard", "schema_level": 2}}}
    for style in ("plan_gate", "plan_main"):
        for stats in (None, {}, {"style": "json"}):
            with pytest.raises(EvalError, match="台帳構成"):
                validate_run_conditions(answer, stats, style)
        validate_run_conditions(answer, {"style": style, "plan": {"plan_present": False, "rows": 0,
                                "stopped_reason": "plan_missing"}}, style)


def test_json_style_does_not_hide_plain_or_old_or_missing_output_schema():
    settings = {"model": "gpt-5.6-sol", "depth": "standard", "mode": "standard", "schema_level": 2}
    validate_run_conditions({"activity": {"settings": settings}}, {"style": "json"}, "json")
    for changes in ({"mode": "plain"}, {"mode": None}, {"schema_level": 0},
                    {"schema_level": 1}, {"schema_level": None}):
        with pytest.raises(EvalError, match="モード・出力スキーマ"):
            validate_run_conditions({"activity": {"settings": {**settings, **changes}}}, {"style": "json"}, "json")


def test_fixed_checkout_rejects_tracked_changes_and_head_changes_but_preserves_untracked(tmp_path: Path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=True)

    git("init", "--quiet")
    git("config", "user.name", "Synthetic evaluator")
    git("config", "user.email", "evaluator@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", "/dev/null")
    tracked = tmp_path / "implementation.py"
    tracked.write_text("first\n", encoding="utf-8")
    git("add", "implementation.py")
    git("commit", "--quiet", "-m", "test: first")
    commit = fixed_checkout(tmp_path)
    untracked = tmp_path / "AGENTS.md"
    untracked.write_text("preserve", encoding="utf-8")
    assert fixed_checkout(tmp_path, commit) == commit
    tracked.write_text("changed\n", encoding="utf-8")
    with pytest.raises(EvalError, match="未コミット"):
        fixed_checkout(tmp_path, commit)
    git("add", "implementation.py")
    with pytest.raises(EvalError, match="未コミット"):
        fixed_checkout(tmp_path, commit)
    git("commit", "--quiet", "-m", "test: second")
    with pytest.raises(EvalError, match="HEAD 変更"):
        fixed_checkout(tmp_path, commit)
    assert untracked.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_port_probe_rejects_listeners_and_accepts_recently_closed_server(host):
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, 0))
        server.listen(1)
        server.settimeout(3)
        port = server.getsockname()[1]
        with pytest.raises(EvalError, match="ポートが使用中"):
            require_free_port(port)
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(3)
                connection.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert connection.recv(1) == b""
    with socket.socket() as plain:
        with pytest.raises(OSError):
            plain.bind(("127.0.0.1", port))
    require_free_port(port)
    with socket.socket() as replacement:
        replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        replacement.bind((host, port))
        replacement.listen(1)
        with pytest.raises(EvalError, match="ポートが使用中"):
            require_free_port(port)


def test_run_conditions_check_the_model_given_on_the_command_line():
    settings = {"model": "gpt-5.5", "depth": "standard", "mode": "standard", "schema_level": 2}
    answer = {"activity": {"settings": settings}}
    validate_run_conditions(answer, {"style": "json"}, "json", "gpt-5.5")
    with pytest.raises(EvalError, match="モデル"):
        validate_run_conditions(answer, {"style": "json"}, "json")  # 既定は従来どおり gpt-5.6-sol


def test_api_key_runs_require_a_dedicated_codex_home(tmp_path: Path):
    args = SimpleNamespace(out=tmp_path / "results", auth_mode="apikey", codex_home=None)
    with pytest.raises(EvalError, match="--codex-home"):
        Runner(args, {"corpus_root": str(tmp_path)}, "digest")


def test_self_manifest_is_accepted_but_unreviewed_questions_are_left_out_by_default(tmp_path: Path):
    from scripts.bench.ledger_eval import select_questions
    root = tmp_path / "corpus"
    (root / "sherpa").mkdir(parents=True)
    (root / "sherpa/a.py").write_text("x = 1\n", encoding="utf-8")
    qs = [{"id": "q1", "system": "self", "question": "一覧は？", "hop": 0, "expected_paths": ["sherpa/a.py"],
           "scope_paths": ["sherpa"]},
          {"id": "q2", "system": "self", "question": "食い違いは？", "hop": 0, "expected_paths": ["sherpa/a.py"],
           "scope_paths": ["sherpa"], "needs_user_review": True}]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"corpus_root": str(root), "corpus_files": ["sherpa/a.py"], "questions": qs}), encoding="utf-8")
    manifest, _ = read_manifest(path)
    assert [q["id"] for q in select_questions(manifest)[0]] == ["q1"]
    assert select_questions(manifest)[1] == {"self": 1}
    assert [q["id"] for q in select_questions(manifest, True)[0]] == ["q1", "q2"]
    manifest["questions"].append({"id": "m", "system": "code", "question": "q", "hop": 1, "expected_paths": []})
    with pytest.raises(EvalError, match="別の manifest"):
        select_questions(manifest)


def test_token_totals_keep_new_cached_and_output_separate_and_do_not_zero_fill_missing():
    from scripts.bench.ledger_eval import pilot_tokens
    agents = [{"tokens": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 7}},
              {"tokens": {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 3}}]
    got = measure({"activity": {"agents": agents}}, None, None)
    assert (got["input_tokens"], got["cached_input_tokens"], got["new_input_tokens"], got["output_tokens"]) == (150, 70, 80, 10)
    missing = measure({"activity": {"agents": [{"tokens": {"input_tokens": 5}}]}}, None, None)
    assert missing["new_input_tokens"] is None and missing["output_tokens"] is None
    rows = [{"style": "json", "system": "self", "id": "a", "counted": True, "new_input_tokens": 80, "cached_input_tokens": 70,
             "output_tokens": 10},
            {"style": "json", "system": "self", "id": "b", "counted": True, "new_input_tokens": None, "cached_input_tokens": 1,
             "output_tokens": 1},
            {"style": "json", "system": "self", "id": "c", "counted": False, "new_input_tokens": 999, "cached_input_tokens": 0,
             "output_tokens": 0}]
    tokens = pilot_tokens(rows, {"self": 4})
    assert tokens["pilot_total"] == {"new_input_tokens": 80, "cached_input_tokens": 70, "output_tokens": 10}
    assert tokens["by_group"]["json/self"]["missing"] == 1
    assert tokens["estimated_total"]["new_input_tokens"] == 320
    assert [t["id"] for t in tokens["per_trial"]] == ["a", "b"]


CORPUS = ["docs/a.md", "docs/b.md", "src/c.py", "src/d.py"]


def test_evidence_reach_separates_body_reference_list_and_claim_refs():
    answer = {"body": "docs/a.md を見た。", "sources": [{"doc_id": "docs/b.md"}],
              "referenced_docs": [{"path": "src/c.py"}],
              "data": {"claims": [{"evidence_refs": ["src/d.py:10-20", "src/c.py:3"]}, {"evidence_refs": []}]}}
    reach = evidence_reach(answer, {"docs/a.md", "docs/b.md", "src/c.py", "src/d.py"}, CORPUS)
    assert reach["expected"] == 4
    assert reach["body"] == {"hits": 1, "recall": .25}
    assert reach["referenced"] == {"hits": 2, "recall": .5}
    assert reach["claims"] == {"hits": 2, "recall": .5}
    empty = evidence_reach({"body": ""}, set(), CORPUS)
    assert empty["body"] == {"hits": 0, "recall": None}


def test_extra_metrics_reads_saved_records_and_marks_absent_ones_missing(tmp_path: Path):
    result = {"expected_paths": ["docs/a.md", "docs/b.md"], "review_requests": 2}
    bare = extra_metrics(tmp_path, result, CORPUS)  # 古い記録: 回答も stats も無い
    assert bare["evidence_reach"] is None and bare["graph"] is None and bare["gate"] is None
    assert bare["gate_closed_count"] is None and bare["review"] is None

    (tmp_path / "answer.json").write_text(json.dumps({"body": "docs/a.md docs/b.md"}), encoding="utf-8")
    (tmp_path / "stats.json").write_text(json.dumps({
        "review_cap": 2, "graph": {"impact_calls": 4, "impact_incomplete": 1, "impact_depth_truncated": 0},
        "gate": {"graph_nudges": 1, "accepted_with_graph_incomplete": True,
                 "closed_rows": [{"id": "r1", "reason": "unreadable", "detail": ""},
                                 {"id": "r2", "reason": "unreadable", "detail": ""}]}}), encoding="utf-8")
    (tmp_path / "plan.md").write_text("- [x] r0 · x\n- [-] r1 · x\n      - 読めない: y\n- [-] r2 · x\n", encoding="utf-8")
    (tmp_path / "review-1.md").write_text("docs/a.md のみ", encoding="utf-8")
    (tmp_path / "review-2.md").write_text("docs/a.md docs/b.md src/c.py", encoding="utf-8")
    out = extra_metrics(tmp_path, result, CORPUS)
    assert out["graph"]["impact_calls"] == 4
    assert out["gate"]["accepted_with_graph_incomplete"] is True
    assert out["gate_closed_count"] == 2 and out["gate_closed_reasons"] == {"unreadable": 2}
    review = out["review"]
    assert review["review_cap"] == 2 and review["requests"] == 2
    assert [c["k"] for c in review["candidates"]] == [1, 2]
    assert review["before"]["hits"] == 1 and review["final"]["hits"] == 2
    assert review["gained_hits"] == 1 and review["recall_delta"] == .5


def test_stage_reach_separates_adopted_composed_final_and_lost_in_processing(tmp_path: Path):
    corpus = ["docs/a.md", "docs/b.md", "docs/c.md", "docs/d.md"]
    result = {"expected_paths": ["docs/a.md", "docs/b.md"]}
    answer = {"body": "docs/a.md を見た", "sources": [{"path": "docs/c.md"}], "sources_unverified": [{"path": "docs/d.md"}],
              "found_docs": [{"path": "docs/b.md"}, "docs/c.md"],
              "data": {"claims": [{"evidence_refs": ["docs/a.md"]}]},
              "activity": {"agents": [
                  {"role": "parent", "tokens": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2}},
                  {"role": "child", "tokens": {"input_tokens": 5, "cached_input_tokens": 1, "output_tokens": 1}},
                  {"role": "child", "tokens": {"input_tokens": 7, "cached_input_tokens": 2, "output_tokens": 3}}]}}
    (tmp_path / "answer.json").write_text(json.dumps(answer), encoding="utf-8")
    (tmp_path / "adopted.md").write_text("docs/a.md docs/b.md", encoding="utf-8")
    (tmp_path / "composed.md").write_text("docs/a.md docs/b.md", encoding="utf-8")
    (tmp_path / "stats.json").write_text(json.dumps({
        "candidate": {"finals_total": 2, "picked_index": 1}, "compose": {"applied": True, "plan_rows": 3},
        "review": {"app_gating": False, "app_review_cap": 0, "app_review_requests": 0, "instruction_review_cap": 1},
        "trimmed": ["x"]}), encoding="utf-8")
    out = extra_metrics(tmp_path, result, corpus)
    reach = out["reach_v2"]
    assert reach["adopted"] == {"hits": 2, "pred": 2, "recall": 1.0, "precision": 1.0}
    assert reach["final_body"]["hits"] == 1 and reach["final_sources"] == {"hits": 0, "pred": 2, "recall": 0.0, "precision": 0.0}
    assert reach["final"]["pred"] == 3 and reach["claims"]["hits"] == 1
    assert reach["found_docs"]["hits"] == 1 and reach["found_docs"]["pred"] == 2
    assert out["lost_in_processing"] == {"paths": ["docs/b.md"], "count": 1}
    assert out["added_in_processing"] == {"gold": 0, "non_gold": 2}
    assert out["candidate"]["finals_total"] == 2 and out["compose"]["applied"] is True
    assert out["review_semantics"]["instruction_review_cap"] == 1 and out["trimmed"] == ["x"]
    assert out["subagent_threads"] == 2  # timeline が無ければ activity の子の数
    assert out["tokens_parent"] == {"input": 10, "cached": 4, "output": 2}
    assert out["tokens_child"] == {"input": 12, "cached": 3, "output": 4}
    (tmp_path / "timeline.json").write_text(json.dumps({"children": {"threads": 3}}), encoding="utf-8")
    assert extra_metrics(tmp_path, result, corpus)["subagent_threads"] == 3


def test_stage_reach_without_new_records_is_missing_not_zero(tmp_path: Path):
    (tmp_path / "answer.json").write_text(json.dumps({"body": "docs/a.md"}), encoding="utf-8")
    out = extra_metrics(tmp_path, {"expected_paths": ["docs/a.md"]}, CORPUS)
    assert out["reach_v2"]["adopted"] is None and out["reach_v2"]["composed"] is None
    assert out["reach_v2"]["final"]["recall"] == 1.0 and out["reach_v2"]["found_docs"] is None
    assert out["lost_in_processing"] is None and out["added_in_processing"] is None
    for key in ("candidate", "compose", "review_semantics", "trimmed", "tokens_parent", "tokens_child"):
        assert out[key] is None
    assert out["subagent_threads"] is None


def _git_repo(path: Path):
    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True)

    git("init", "--quiet")
    git("config", "user.name", "Synthetic evaluator")
    git("config", "user.email", "evaluator@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", "/dev/null")
    (path / "implementation.py").write_text("first\n", encoding="utf-8")
    git("add", "implementation.py")
    git("commit", "--quiet", "-m", "test: first")
    return git


def test_allow_dirty_records_the_diff_and_stops_when_it_or_head_changes(tmp_path: Path):
    git = _git_repo(tmp_path)
    clean = ledger_eval.dirty_state(tmp_path)
    assert clean["changed_files"] == [] and clean["untracked_files"] == []
    (tmp_path / "scripts" / "bench").mkdir(parents=True)
    (tmp_path / "scripts" / "bench" / "new.py").write_text("x", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("対象外", encoding="utf-8")
    tracked = tmp_path / "implementation.py"
    tracked.write_text("changed\n", encoding="utf-8")
    state = ledger_eval.dirty_state(tmp_path)
    assert state["diff_sha256"] != clean["diff_sha256"]
    assert state["changed_files"] == ["implementation.py"] and state["untracked_files"] == ["scripts/bench/new.py"]
    with pytest.raises(EvalError, match="未コミット"):
        fixed_checkout(tmp_path)
    commit = fixed_checkout(tmp_path, allow_dirty=True)
    assert fixed_checkout(tmp_path, commit, allow_dirty=True, expected_diff=state["diff_sha256"]) == commit
    tracked.write_text("changed again\n", encoding="utf-8")
    with pytest.raises(EvalError, match="差分"):
        fixed_checkout(tmp_path, commit, allow_dirty=True, expected_diff=state["diff_sha256"])
    tracked.write_text("changed\n", encoding="utf-8")
    git("commit", "--quiet", "-am", "test: second")
    with pytest.raises(EvalError, match="HEAD 変更"):
        fixed_checkout(tmp_path, commit, allow_dirty=True)


def test_env_identity_keeps_secret_values_out_of_the_record_and_the_hash():
    base = {"SHERPA_ES_PORT": "19201", "SHERPA_ADMIN_PASSWORD": "alpha", "SHERPA_API_TOKEN": "t", "PATH": "/bin"}
    one = ledger_eval.env_identity(base)
    two = ledger_eval.env_identity({**base, "SHERPA_ADMIN_PASSWORD": "beta"})
    assert one["sherpa_env"] == {"SHERPA_ES_PORT": "19201"}
    assert one["sherpa_env_secret_names"] == ["SHERPA_ADMIN_PASSWORD", "SHERPA_API_TOKEN"]
    assert "alpha" not in json.dumps(one) and one["sherpa_env_sha256"] == two["sherpa_env_sha256"]
    assert ledger_eval.env_identity({**base, "SHERPA_ES_PORT": "1"})["sherpa_env_sha256"] != one["sherpa_env_sha256"]


def test_question_and_style_lists_are_validated():
    assert ledger_eval.id_list("a, b") == ("a", "b")
    assert ledger_eval.style_list("plan_main,json") == ("plan_main", "json")
    for bad in ("", "a,a"):
        with pytest.raises(argparse.ArgumentTypeError):
            ledger_eval.id_list(bad)
    for bad in ("json,json", "other", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            ledger_eval.style_list(bad)


class _Server:
    """SSE を返すだけの実 HTTP サーバ（外部境界）。script は (待ち秒, 送る行) の列。"""

    def __init__(self, script):
        import http.server
        import threading

        outer = script

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for wait, line in outer:
                        time.sleep(wait)
                        self.wfile.write(line)
                        self.wfile.flush()
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def __enter__(self):
        return self.httpd.server_address[1]

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _read(port, tmp_path, stall, timeout=30):
    api = API(port, 5)
    started = time.monotonic()
    with api.request("GET", "/s", stream=True, timeout=stall) as stream, (tmp_path / "e.jsonl").open("w") as out:
        return list(stream_events(stream, out, started, timeout, stall))


def test_stream_stops_when_only_keepalives_arrive(tmp_path: Path):
    script = [(0, b'data: {"type": "a"}\n\n')] + [(.2, b": keepalive\n\n")] * 20
    with _Server(script) as port:
        with pytest.raises(StallTimeout):
            _read(port, tmp_path, stall=.8)
    assert (tmp_path / "e.jsonl").read_text().count("\n") == 1  # 止めるまでのイベントは残る


def test_stream_stops_when_nothing_arrives(tmp_path: Path):
    with _Server([(0, b'data: {"type": "a"}\n\n'), (5, b"data: {}\n\n")]) as port:
        with pytest.raises(StallTimeout):
            _read(port, tmp_path, stall=.5)


def test_stream_keeps_going_while_events_keep_arriving(tmp_path: Path):
    script = [(.3, b'data: {"type": "a"}\n\n')] * 4
    with _Server(script) as port:
        assert len(_read(port, tmp_path, stall=.8)) == 4


def test_stalled_trial_is_retried_once_with_the_same_fingerprint(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ledger_eval, "fixed_checkout", lambda root, expected=None: "head")

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            raise socket.timeout("silent")

    class FakeAPI:  # HTTP 境界の代役。応答が途絶えるターンを返す
        posted = 0

        def request(self, method, path, payload=None, *, stream=False, timeout=None):
            if path == "/chat/turns" and method == "POST":
                FakeAPI.posted += 1
                return {"turn_id": f"t{FakeAPI.posted}", "conversation_id": FakeAPI.posted}
            if stream:
                return Stream()
            return {"ok": True, "turns": []}

    runner = SimpleNamespace(out=tmp_path, state=tmp_path / "runtime", fingerprint="fp", commit="head",
                             retry_from=None, retry_only=frozenset(), api=FakeAPI(), world="w",
                             args=SimpleNamespace(timeout=60, stall_timeout=.5), consecutive_infra=0)
    runner.trial = lambda *a, **k: Runner.trial(runner, *a, **k)
    runner.stop_trial = lambda result: Runner.stop_trial(runner, result)
    question = {"system": "code", "id": "q1", "hop": 1, "question": "?", "scope_paths": ["code"]}
    runner.args.max_infra_streak = 5
    result = runner.trial("json", question, "pilot")
    assert FakeAPI.posted == 2
    assert result["attempt_no"] == 2 and result["stall_count"] == 2
    assert result["classification"]["class"] == "infrastructure" and result["classification"]["reason"] == "stall_timeout"
    first = json.loads((tmp_path / "trials/json/code/q1/result.json").read_text())
    assert first["stalled"] and first["fingerprint"] == "fp" and first["stall_count"] == 1
    assert (tmp_path / "trials/json/code/q1/retries/2/result.json").is_file()
    assert runner.consecutive_infra == 1  # 自動再試行の 1 回目は連続障害に数えない


def _check_manifest(**override):
    q = {"id": "q1", "system": "code", "question": "架空の影響は？", "hop": 1, "expected_paths": ["code/a.cbl"],
         "scope_paths": ["code"]}
    manifest = {"corpus_files": ["code/a.cbl", "self/x.py", "sherpa/b.py"], "questions": [{**q, **override}]}
    return ledger_eval.check_manifest(manifest)[0]["error"]


def test_check_finds_a_question_added_without_scope_paths_before_the_api_does():
    q = {"id": "q1", "system": "self", "question": "一覧は？", "hop": 0, "expected_paths": ["sherpa/b.py"]}
    findings = ledger_eval.check_manifest({"corpus_files": ["sherpa/b.py"], "questions": [q]})
    assert "scope_paths がない" in findings[0]["error"]
    assert _check_manifest() is None
    assert _check_manifest(system="self", id="s1", scope_paths=[], expected_paths=["sherpa/b.py"]) is None


@pytest.mark.parametrize("override,message", [
    ({"scope_paths": []}, "scope_paths が空"),
    ({"scope_paths": ["nofolder"]}, "フォルダでない"),
    ({"scope_paths": ["code/a.cbl"]}, "フォルダでない"),
    ({"scope_paths": ["../code"]}, "登録ルート外"),
    ({"expected_paths": ["code/none.cbl"]}, "corpus_files にない"),
    ({"expected_paths": ["sherpa/b.py"]}, "scope_paths の外"),
    ({"expected_paths": []}, "null でない"),
    ({"null": True}, "null の問題は expected_paths が空"),
    ({"question": "  "}, "question が空"),
    ({"hop": -1}, "hop"),
    ({"system": "nope"}, "system が不正"),
])
def test_check_reports_each_invalid_field(override, message):
    assert message in _check_manifest(**override)


def test_check_accepts_null_questions_and_reports_duplicates():
    assert _check_manifest(null=True, expected_paths=[]) is None
    q = {"id": "q1", "system": "code", "question": "?", "hop": 1, "expected_paths": ["code/a.cbl"], "scope_paths": ["code"]}
    findings = ledger_eval.check_manifest({"corpus_files": ["code/a.cbl"], "questions": [q, dict(q)]})
    assert findings[0]["error"] is None and "重複" in findings[1]["error"]


def test_check_phase_prints_a_list_and_fails_on_findings(tmp_path: Path, capsys):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"corpus_files": ["code/a.cbl"], "questions": [
        {"id": "ok", "system": "code", "question": "?", "hop": 1, "expected_paths": ["code/a.cbl"], "scope_paths": ["code"]},
        {"id": "bad", "system": "code", "question": "?", "hop": 1, "expected_paths": ["code/a.cbl"]}]}), encoding="utf-8")
    before = path.read_bytes()
    assert ledger_eval.main(["check", "--manifest", str(path)]) == 2
    out = capsys.readouterr().out
    assert "OK  code/ok" in out and "NG  code/bad" in out and "2 問中 1 問" in out
    assert path.read_bytes() == before


def test_extra_metrics_copies_target_check_gate_log_and_normalizations(tmp_path: Path):
    assert extra_metrics(tmp_path, {}, CORPUS)["target_check"] is None
    check = {"status": "missing", "missing_count": 2, "decision": "revert", "dry_run": True}
    (tmp_path / "stats.json").write_text(json.dumps({"target_check": check, "gate_log": [{"decision": "revert"}],
                                                     "plan_normalizations": [{"row": "r1"}]}), encoding="utf-8")
    out = extra_metrics(tmp_path, {}, CORPUS)
    assert out["target_check"] == check and out["gate_log"] == [{"decision": "revert"}]
    assert out["plan_normalizations"] == [{"row": "r1"}]
    (tmp_path / "stats.json").write_text(json.dumps({"target_check": "x", "gate_log": {}}), encoding="utf-8")
    bad = extra_metrics(tmp_path, {}, CORPUS)
    assert bad["target_check"] is None and bad["gate_log"] is None and bad["plan_normalizations"] is None
