"""台帳評価の採点: 部分回答の原因別分類・サーバ側の道具回数・再試行の選び方・版の指紋での停止を固定する。

保存物の構造だけを写した架空の fixture（tests/fixtures/ledger_eval/results）を使う。モデルや API は呼ばない。
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from scripts.bench import ledger_eval, ledger_scoring as sc

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ledger_eval" / "results"
LOG = FIXTURE / "codex-log.txt"


def rows_by_id(root: Path = FIXTURE) -> dict[str, dict]:
    return {r["id"]: r for r in sc.collect(root, codex_log=LOG)}


def test_partial_answers_are_split_by_cause_not_by_api_status():
    rows = rows_by_id()
    assert rows["synthetic-complete"]["class"] == sc.COMPLETE
    reverted = rows["synthetic-reverted"]
    assert reverted["class"] == sc.PRODUCT_PARTIAL
    assert reverted["classification"]["reason"] == "review_reverted"
    overloaded = rows["synthetic-overloaded"]
    assert overloaded["class"] == sc.INFRASTRUCTURE
    assert overloaded["classification"]["reason"] == "cli_capacity"
    assert overloaded["classification"]["evidence"]["cli_error"]["code"] == "server_overloaded"
    assert rows["synthetic-interrupted"]["class"] == sc.STOPPED
    assert rows["synthetic-interrupted"]["classification"]["evidence"]["stop_drained"] is True
    # 元の result.json は status=ok・codex_error_code=None のまま。それでも基盤障害と分かる。
    assert overloaded["status"] == "ok"
    assert json.loads((FIXTURE / "trials/json/code/synthetic-overloaded/answer.json").read_text())["codex_error_code"] is None


def test_recovered_partial_without_cli_evidence_is_unverified_not_product_partial():
    row = rows_by_id()["synthetic-unverified"]
    assert row["class"] == sc.UNVERIFIED
    assert row["classification"]["reason"] == "answer_recovery_without_cli_evidence"
    assert not row["counted"]


def test_cli_exit_code_alone_marks_infrastructure_when_rollout_is_absent(tmp_path: Path):
    answer = {"completion": "partial", "notices": [{"kind": "answer_recovery"}]}
    exit_info = sc.read_codex_log(LOG, 3)
    assert exit_info == {"returncode": 1, "events": {"thread.started": 1, "error": 5, "turn.failed": 1}}
    assert sc.classify_trial({"status": "ok"}, answer, None, exit_info)["reason"] == "cli_exit_nonzero"
    assert sc.read_codex_log(LOG, 99) is None


def test_terminal_error_kinds_separate_capacity_usage_limit_and_service_errors():
    assert sc.error_kind("x", "server_overloaded") == "capacity"
    assert sc.error_kind("Selected model is at capacity.", None) == "capacity"
    assert sc.error_kind("You hit the usage limit", None) == "usage_limit"
    assert sc.error_kind("boom", "other") == "service_error"
    answer = {"completion": "partial", "notices": []}
    cli = {"terminal_error": {"message": "usage limit reached", "code": None}, "parent_found": True}
    assert sc.classify_trial({"status": "ok"}, answer, cli)["reason"] == "cli_usage_limit"


def test_child_errors_do_not_decide_the_parent_class():
    cli = sc.read_cli_terminal(FIXTURE / "runtime/users/admin/workspace/.codex-sessions/1")
    assert cli["parent_found"] and cli["rollouts"] == 2
    assert cli["terminal_error"] is None
    assert cli["task_complete_count"] == 1


def test_complete_answer_with_cli_error_is_kept_but_warned():
    cli = {"terminal_error": {"message": "late", "code": None}, "parent_found": True}
    verdict = sc.classify_trial({"status": "ok"}, {"completion": "complete", "notices": []}, cli)
    assert verdict["class"] == sc.COMPLETE
    assert verdict["warnings"] == ["cli_error_with_complete_answer"]


def test_runner_failures_and_unfinished_records_are_not_scored_as_answers():
    assert sc.classify_trial({"status": "failed", "error_type": "EvalError"}, None)["class"] == sc.INFRASTRUCTURE
    assert sc.classify_trial({"status": "usage_limit"}, None)["reason"] == "usage_limit"
    assert sc.classify_trial({"status": "running"}, None)["class"] == sc.STOPPED
    assert sc.classify_trial({"status": "ok"}, None)["class"] == sc.UNVERIFIED


def test_server_side_tool_counts_are_summed_by_name_and_checked_against_calls():
    answer = json.loads((FIXTURE / "trials/json/code/synthetic-complete/answer.json").read_text())
    seen = sc.observed_tools(answer["call_stats"])
    assert seen["by_tool"] == {"es_search": 3, "read_around": 7}
    assert seen["es_search_calls"] == 3
    assert seen["observation_complete"] is True
    assert seen["diagnostics"]["summed_calls"] == seen["diagnostics"]["recorded_calls"] == 10
    # 外側の exec(61) は加算しない。ランナーの測定値も call_stats から出す。
    measured = ledger_eval.measure(answer, None, None)
    assert measured["es_search_calls"] == 3
    assert measured["tool_counts"] == {"exec": 61}
    assert measured["tool_observation_complete"] is True


def test_tool_observation_mismatch_missing_and_absent_stats_are_not_zero():
    stats = json.loads((FIXTURE / "trials/json/code/synthetic-complete/answer.json").read_text())["call_stats"]
    assert sc.observed_tools({**stats, "calls": 11})["observation_complete"] is False
    assert sc.observed_tools({**stats, "missing": 2})["observation_complete"] is False
    assert sc.observed_tools({**stats, "missing": 2})["es_search_calls"] == 3
    broken = {**stats, "tools": [*stats["tools"], {"role": "parent", "tool": "es_search", "calls": 9}]}
    seen = sc.observed_tools(broken)
    assert seen["diagnostics"]["duplicate_conflicts"] == 1 and seen["es_search_calls"] is None
    no_es = sc.observed_tools({"calls": 4, "missing": 0, "tools": [{"role": "child", "tool": "read_around", "calls": 4}]})
    assert no_es["es_search_calls"] == 0 and no_es["observation_complete"] is True
    for absent in (None, {}):
        seen = sc.observed_tools(absent)
        assert seen["es_search_calls"] is None and seen["observation_complete"] is False


def test_effective_attempt_is_the_latest_valid_one_and_history_is_kept():
    rows = rows_by_id()
    retried = rows["synthetic-retried"]
    assert retried["class"] == sc.COMPLETE
    assert retried["attempt_no"] == 2 and retried["attempts_total"] == 2
    assert retried["attempt_classes"] == [sc.INFRASTRUCTURE, sc.COMPLETE]
    assert retried["cost_seconds"] == {sc.INFRASTRUCTURE: 300.0, sc.COMPLETE: 120.0}
    # 元の試行の記録は動かない。
    first = json.loads((FIXTURE / "trials/json/code/synthetic-retried/result.json").read_text())
    assert first["status"] == "ok" and first["conversation_id"] == 5


def test_retry_selects_only_unresolved_classes():
    rows = list(rows_by_id().values())
    assert sc.select_retries(rows, sc.DEFAULT_RETRY) == [
        ("json", "code", "synthetic-overloaded"), ("json", "multihop", "synthetic-interrupted")]
    assert sc.select_retries(rows, [sc.UNVERIFIED]) == [("json", "multihop", "synthetic-unverified")]
    for bad in ([sc.COMPLETE], ["nonsense"]):
        with pytest.raises(sc.EvalError, match="--only"):
            sc.select_retries(rows, bad)


def test_summary_counts_classes_and_excludes_failures_from_means():
    rows = list(rows_by_id().values())
    text = sc.summary_markdown(rows)
    line = next(ln for ln in text.splitlines() if ln.startswith("|json|all|"))
    cells = line.strip("|").split("|")
    # 完答2(complete+retried)・製品部分1・基盤障害1・停止1・原因未確定1
    assert cells[2:7] == ["2", "1", "1", "1", "1"]
    assert "n=3" in cells[7]  # Recall は完答2+製品部分1だけ
    assert cells[-2] == "700.0" and cells[-1] == "150.0"  # 障害秒(c)は 400 秒＋再試行で解消した 300 秒
    code = next(ln for ln in text.splitlines() if ln.startswith("|json|code|")).strip("|").split("|")
    assert code[2:7] == ["2", "1", "1", "0", "0"]


def test_undefined_scores_stay_missing_in_the_tables():
    rows = [{"style": "json", "system": "code", "id": "q", "hop": 1, "class": sc.COMPLETE, "counted": True,
             "recall": None, "precision": None, "cost_seconds": {}}]
    line = next(ln for ln in sc.summary_markdown(rows).splitlines() if ln.startswith("|json|all|"))
    assert line.strip("|").split("|")[7:9] == ["—", "—"]


def test_version_mismatch_stops_with_the_differing_keys():
    recorded = json.loads((FIXTURE / "metadata.json").read_text())
    sc.check_same_version(recorded, dict(recorded))
    for key, value in (("commit", "other-head"), ("cli_sha256", "other-cli"), ("corpus_sha256", "other-corpus")):
        with pytest.raises(sc.EvalError, match=key) as err:
            sc.check_same_version(recorded, {**recorded, key: value})
        assert "新しい出力フォルダ" in str(err.value)


def _runner_args(out: Path, retry: bool = True):
    return SimpleNamespace(out=out, pg_port=15433, neo_http_port=17475, neo_bolt_port=17688, es_port=19201,
                           api_port=18072, timeout=1.0, retry_from=out if retry else None, only=sc.DEFAULT_RETRY)


def _build_runner(monkeypatch, tmp_path: Path, head: str, out: Path):
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir(exist_ok=True)
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(ledger_eval, "fixed_checkout", lambda root, expected=None: head)
    monkeypatch.setattr(ledger_eval.shutil, "which", lambda name, path=None: str(codex))
    monkeypatch.setenv("PATH", str(codex.parent))
    manifest = {"corpus_root": str(corpus), "corpus_sha256": "synthetic-corpus", "questions": [], "seed": 7}
    return ledger_eval.Runner(_runner_args(out), manifest, "synthetic-manifest")


def test_runner_refuses_to_continue_results_from_another_version(monkeypatch, tmp_path: Path):
    out = tmp_path / "results"
    shutil.copytree(FIXTURE, out)
    before = (out / "metadata.json").read_bytes()
    with pytest.raises(sc.EvalError, match="版の指紋"):
        _build_runner(monkeypatch, tmp_path, "another-head", out)
    assert (out / "metadata.json").read_bytes() == before
    assert not list(out.glob("trials/**/retries/3"))


def test_retry_requires_recorded_metadata(monkeypatch, tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(sc.EvalError, match="metadata.json"):
        _build_runner(monkeypatch, tmp_path, "any-head", empty)


def _trial_runner(out: Path, only=sc.DEFAULT_RETRY, retry=True):
    return SimpleNamespace(out=out, state=out / "runtime", fingerprint="synthetic-fingerprint", commit="head",
                           retry_from=out if retry else None, retry_only=frozenset(only) if retry else frozenset())


def test_trial_reuses_valid_attempts_and_skips_unselected_ones(monkeypatch):
    monkeypatch.setattr(ledger_eval, "fixed_checkout", lambda root, expected=None: "head")

    def run(runner, qid, system="code"):
        return ledger_eval.Runner.trial(runner, "json", {"system": system, "id": qid, "hop": 1}, "pilot")

    runner = _trial_runner(FIXTURE)
    assert run(runner, "synthetic-complete")["classification"]["class"] == sc.COMPLETE
    assert run(runner, "synthetic-retried")["attempt_no"] == 2
    assert run(runner, "synthetic-unverified", "multihop") is None  # --only に無い分類は触らない
    assert run(runner, "never-ran") is None  # 記録の無い質問は再試行の対象外
    wrong = _trial_runner(FIXTURE)
    wrong.fingerprint = "other"
    with pytest.raises(sc.EvalError, match="指紋"):
        run(wrong, "synthetic-complete")
    plain = _trial_runner(FIXTURE, retry=False)
    with pytest.raises(sc.EvalError, match="retry"):
        run(plain, "synthetic-overloaded")


def test_retry_arguments_are_validated_before_anything_runs(tmp_path: Path, capsys):
    base = ["--manifest", str(tmp_path / "m.json")]
    for argv in (["retry", *base], ["pilot", *base, "--out", str(tmp_path), "--retry-from", str(tmp_path)],
                 ["retry", *base, "--retry-from", str(tmp_path), "--out", str(tmp_path / "other")],
                 ["retry", *base, "--retry-from", str(tmp_path), "--only", "complete"]):
        with pytest.raises(SystemExit) as err:
            ledger_eval.main(argv)
        assert err.value.code == 2
    assert "--retry-from" in capsys.readouterr().err


def _write_rate(root: Path, reached, reset):
    path = root / "runtime/users/admin/workspace/.codex-sessions/9/sessions/2026/01/01/rollout-09.jsonl"
    path.parent.mkdir(parents=True)
    rate = {"primary": {"used_percent": 99.0, "resets_at": reset}, "rate_limit_reached_type": reached,
            "spend_control_reached": None}
    lines = [{"type": "session_meta", "payload": {"id": "x"}},
             {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": rate}}]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def test_usage_gate_stops_on_reached_flag_not_on_high_usage(tmp_path: Path):
    _write_rate(tmp_path, None, 2000)
    sc.usage_gate(tmp_path, now=1000)  # 99% でも到達フラグが無ければ通す
    (tmp_path / "runtime").rename(tmp_path / "runtime-old")
    _write_rate(tmp_path, "primary", 2000)
    with pytest.raises(sc.UsageLimit, match="到達フラグ"):
        sc.usage_gate(tmp_path, now=1000)
    sc.usage_gate(tmp_path, now=2001)  # リセット時刻を過ぎた古い記録は到達とみなさない


def test_usage_stop_record_needs_explicit_clearing_and_is_kept(tmp_path: Path):
    (tmp_path / "stopped.json").write_text("{}", encoding="utf-8")
    with pytest.raises(sc.UsageLimit, match="--clear-usage-stop"):
        sc.usage_gate(tmp_path)
    sc.usage_gate(tmp_path, clear=True)
    assert not (tmp_path / "stopped.json").exists()
    assert (tmp_path / "stopped-1.json").read_text(encoding="utf-8") == "{}"


def test_reclassify_writes_new_files_only_and_leaves_raw_records_untouched(tmp_path: Path):
    from scripts.bench import ledger_reclassify

    source = tmp_path / "results"
    shutil.copytree(FIXTURE, source)
    out = source / "analysis" / "reclassify"
    snapshot = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    meta = ledger_reclassify.reclassify(source, out)
    assert meta["counts"][sc.INFRASTRUCTURE] == 1 and meta["counts"][sc.COMPLETE] == 2
    lines = [json.loads(line) for line in (out / "classification.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 7  # 再試行の 1 回目も試行として残る
    first = {line["id"]: line["class"] for line in lines if line["attempt_no"] == 1}
    assert first["synthetic-retried"] == sc.INFRASTRUCTURE
    assert {p: p.read_bytes() for p in snapshot} == snapshot
    with pytest.raises(sc.EvalError, match="上書きしない"):
        ledger_reclassify.reclassify(source, out)
    with pytest.raises(sc.EvalError, match="生データ"):
        ledger_reclassify.reclassify(source, source / "trials" / "json" / "new")


def _row(style, system, qid, recall, precision, **extra):
    return {"style": style, "system": system, "id": qid, "hop": 1, "counted": True, "class": sc.COMPLETE,
            "recall": recall, "precision": precision, "wall_seconds": 60.0, "input_tokens": 2_000_000,
            "attempts_total": 1, "stall_count": 0, **sc.rate_metrics({"recall": recall, "wall_seconds": 60.0,
                                                                       "input_tokens": 2_000_000}), **extra}


def test_efficiency_metrics_are_missing_not_zero_without_a_denominator():
    assert sc.rate_metrics({"recall": .5, "input_tokens": 2_000_000, "wall_seconds": 30}) == {
        "recall_per_mtoken": .25, "recall_per_min": 1.0}
    assert sc.rate_metrics({"recall": None, "input_tokens": 5, "wall_seconds": 5}) == {
        "recall_per_mtoken": None, "recall_per_min": None}
    assert sc.rate_metrics({"recall": 1.0, "input_tokens": None, "wall_seconds": 0}) == {
        "recall_per_mtoken": None, "recall_per_min": None}
    assert sc.percentile([1, 2, 3, 4], .5) == 2.5 and sc.percentile([], .5) is None


def test_stalled_trial_is_infrastructure_and_counted_per_question(tmp_path: Path):
    verdict = sc.classify_trial({"status": "failed", "error_type": "StallTimeout", "stalled": True,
                                 "stall_seconds": 600}, None)
    assert verdict["class"] == sc.INFRASTRUCTURE and verdict["reason"] == "stall_timeout" and verdict["retriable"]
    base = tmp_path / "trials/json/code/q"
    for path, result in ((base, {"status": "failed", "stalled": True, "stall_seconds": 600, "wall_seconds": 600}),
                         (base / "retries/2", {"status": "ok", "wall_seconds": 90, "recall": 1.0})):
        path.mkdir(parents=True)
        (path / "result.json").write_text(json.dumps({"style": "json", "system": "code", "id": "q", "hop": 1, **result}))
        if "recall" in result:
            (path / "answer.json").write_text(json.dumps({"completion": "complete", "notices": []}))
    row = sc.collect(tmp_path, codex_log=tmp_path / "none.log")[0]
    assert row["class"] == sc.COMPLETE and row["attempts_total"] == 2 and row["stall_count"] == 1
    assert row["cost_seconds"][sc.INFRASTRUCTURE] == 600


def test_summary_names_the_recorded_model_and_adds_metrics_without_new_fields():
    old = [_row("json", "code", "q1", .5, .5)]  # 追加項目の無い古い記録
    text = sc.summary_markdown(old, ("code",), "gpt-test-model")
    assert "gpt-test-model" in text and "gpt-5.6-sol" not in text
    assert "|構成|系統・hop|完答(a)|" in text  # 既存の表は残る
    section = text.split("## 追加指標")[1]
    assert "|json|独自|1|0.500|0.500|60.0|60.0|2000000|—|" in section
    assert "|json|all|—|—|—|" in section  # 根拠の到達は欠測
    assert "impact_incomplete" in section and "0/0" in section


def test_metrics_tables_aggregate_reach_graph_gate_and_review():
    reach = {"body": {"recall": .5}, "referenced": {"recall": 1.0}, "claims": {"recall": 0.0}}
    rows = [_row("plan_gate", "self", "a", 1.0, 1.0, evidence_reach=reach,
                 graph={"impact_calls": 4, "impact_incomplete": 1, "impact_depth_truncated": 2},
                 gate={"accepted_with_graph_incomplete": True}, gate_closed_count=3,
                 review={"requests": 2, "gained_hits": 3}),
            _row("plan_gate", "multihop", "b", .5, .5, graph={"impact_calls": 4, "impact_incomplete": 0,
                                                              "impact_depth_truncated": 0},
                 gate={"accepted_with_graph_incomplete": False}, gate_closed_count=1,
                 review={"requests": 1, "gained_hits": 0}, stall_count=1)]
    text = sc.metrics_markdown(rows, ("multihop", "self"))
    assert "|plan_gate|all|2|0.750|0.750|" in text
    assert "|plan_gate|公開|1|0.500|" in text and "|plan_gate|独自|1|1.000|" in text
    assert "|plan_gate|self|0.500|1.000|0.000|" in text
    assert "|plan_gate|all|0.125 (1/8)|0.250 (2/8)|2.000|1/2|3|3|1.000|" in text
    assert text.count("|1|\n") >= 1  # 無応答の回数


def test_compare_reports_per_question_differences_and_wins_between_styles():
    run1 = [_row("json", "code", "q1", 1.0, .5), _row("json", "code", "q2", .5, .5),
            _row("plan_main", "code", "q1", .5, .5), _row("plan_main", "code", "q2", .5, .5)]
    run2 = [_row("json", "code", "q1", .5, .5), _row("json", "code", "q2", .5, 1.0),
            _row("plan_main", "code", "q1", 1.0, .5)]
    text = sc.compare_markdown(run1, run2, "one", "two")
    assert "|json|2|0.750|0.500|0.250|0.354|-0.250|0.354|" in text
    assert "|plan_main|1|0.500|1.000|-0.500|—|0.000|—|" in text
    assert "|json|code|q1|1.000|0.500|0.500|0.500|0.500|0.000|" in text
    assert "|one|json|plan_main|1|0|1|" in text and "|two|json|plan_main|0|1|0|" in text
    assert "|one|json|plan_gate|0|0|0|" in text


def _runner_with(monkeypatch, tmp_path: Path, **args):
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir(exist_ok=True)
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(ledger_eval.shutil, "which", lambda name, path=None: str(codex))
    monkeypatch.setenv("PATH", str(codex.parent))
    questions = [{"system": "code", "id": i, "hop": 1} for i in ("q1", "q2")]
    manifest = {"corpus_root": str(corpus), "corpus_sha256": "c", "questions": questions, "seed": 7}
    base = vars(_runner_args(tmp_path / "out", retry=False))
    return ledger_eval.Runner(SimpleNamespace(**{**base, "retry_from": None, **args}), manifest, "m")


def test_runner_narrows_questions_and_styles_without_changing_the_fingerprint(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ledger_eval, "fixed_checkout", lambda root, expected=None: "head")
    full = _runner_with(monkeypatch, tmp_path, out=tmp_path / "a", rep=2)
    narrow = _runner_with(monkeypatch, tmp_path, out=tmp_path / "b", questions=("q2",), styles=("plan_main", "json"))
    assert [q["id"] for q in narrow.questions] == ["q2"] and narrow.styles == ("plan_main", "json")
    assert full.styles == sc.STYLES and full.fingerprint == narrow.fingerprint
    meta = json.loads((tmp_path / "a" / "metadata.json").read_text(encoding="utf-8"))
    assert meta["rep"] == 2 and meta["sherpa_env"]["SHERPA_ES_PORT"] == "19201"
    assert "SHERPA_ADMIN_PASSWORD" in meta["sherpa_env_secret_names"] and len(meta["sherpa_env_sha256"]) == 64
    assert meta["question_ids"] == ["code/q1", "code/q2"]  # 絞っても記録は全体
    with pytest.raises(sc.EvalError, match="未知"):
        _runner_with(monkeypatch, tmp_path, out=tmp_path / "c", questions=("nope",))


def test_allow_dirty_changes_the_fingerprint_and_is_checked_each_stage(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ledger_eval, "fixed_checkout", lambda root, expected=None, **kw: "head")
    clean = _runner_with(monkeypatch, tmp_path, out=tmp_path / "a")
    monkeypatch.setattr(ledger_eval, "dirty_state", lambda root: {"diff_sha256": "d1", "changed_files": ["x.py"],
                                                                 "untracked_files": ["tests/new.py"]})
    dirty = _runner_with(monkeypatch, tmp_path, out=tmp_path / "b", allow_dirty=True)
    assert dirty.fingerprint != clean.fingerprint and dirty.diff_sha256 == "d1"
    meta = json.loads((tmp_path / "b" / "metadata.json").read_text(encoding="utf-8"))
    assert meta["diff_sha256"] == "d1" and meta["changed_files"] == ["x.py"] and meta["untracked_files"] == ["tests/new.py"]
    seen = []
    monkeypatch.setattr(ledger_eval, "fixed_checkout", lambda root, expected=None, **kw: seen.append(kw) or "head")
    ledger_eval.checkout_guard(dirty)
    assert seen == [{"allow_dirty": True, "expected_diff": "d1"}]


def test_trial_records_rep_run_sequence_and_times(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ledger_eval, "fixed_checkout", lambda root, expected=None: "head")
    done = tmp_path / "trials/json/code/old"
    done.mkdir(parents=True)
    (done / "result.json").write_text(json.dumps({"run_seq": 4}), encoding="utf-8")
    assert sc.next_run_seq(tmp_path) == 5 and sc.next_run_seq(tmp_path / "none") == 1

    class FakeAPI:
        def request(self, *a, **k):
            raise sc.EvalError("API は呼ばない")

    runner = SimpleNamespace(out=tmp_path, state=tmp_path / "runtime", fingerprint="fp", commit="head", rep=3,
                             retry_from=None, retry_only=frozenset(), api=FakeAPI(), world="w",
                             args=SimpleNamespace(timeout=60, stall_timeout=1, max_infra_streak=5), consecutive_infra=0)
    runner.stop_trial = lambda result: None
    with pytest.raises(sc.EvalError):
        ledger_eval.Runner.trial(runner, "json", {"system": "code", "id": "q9", "hop": 1, "question": "?",
                                                  "scope_paths": ["code"]}, "pilot")
    saved = json.loads((tmp_path / "trials/json/code/q9/result.json").read_text(encoding="utf-8"))
    assert saved["rep"] == 3 and saved["run_seq"] == 5
    assert saved["started_at"] <= saved["ended_at"]


def test_stage_summary_aggregates_totals_means_loss_threads_tokens_and_review_semantics():
    def stage(hits, pred, recall, precision):
        return {"hits": hits, "pred": pred, "recall": recall, "precision": precision}

    def row(style, qid, rep, cap):
        return _row(style, "code", qid, .5, .5, rep=rep, expected_paths=["a", "b"], subagent_threads=2 if qid == "q1" else 0,
                    tokens_parent={"input": 10, "cached": 4, "output": 2}, tokens_child={"input": 5, "cached": 1, "output": 1},
                    reach_v2={"adopted": stage(2, 2, 1.0, 1.0), "final": stage(1, 4, .5, .25)},
                    lost_in_processing={"paths": ["b"], "count": 1}, added_in_processing={"gold": 0, "non_gold": 2},
                    review_semantics={"app_gating": style != "plan_main", "app_review_cap": cap, "app_review_requests": 1,
                                      "instruction_review_cap": 1})

    rows = [row("json", "q1", 1, 2), row("json", "q2", 2, 2), row("plan_main", "q1", 1, 0)]
    text = sc.summary_markdown(rows, ("code",), "m")
    assert "json: app_review_cap=2 instruction_review_cap=1 app_gating=True" in text
    assert "plan_main: app_review_cap=0 instruction_review_cap=1 app_gating=False" in text
    assert "A 見直し上限 2 回" not in text
    assert "|json|all|adopted|2|4|4|4|1.000|1.000|1.000|1.000|" in text
    assert "|json|all|final|2|2|8|4|0.500|0.250|0.500|0.250|" in text
    assert "|json|all|composed|0|—|" in text  # 記録の無い段は欠測
    assert "|json|all|2|2|2|0|4|" in text  # 試行 2・lost延べ 2・lostのあった試行 2・added 0 / 4
    assert "|json|all|2|1|20|8|4|10|2|2|" in text  # 子のスレッドのあった試行 1
    assert "|json|2|2|2|1.000|1|True|" in text
    assert "|plan_main|1|0|1|1.000|1|False|" in text
    assert "|1|json|1|0.500|0.500|0.500|1|" in text and "|2|json|1|" in text
    old = sc.summary_markdown([_row("json", "code", "q1", .5, .5)], ("code",), "m")
    assert "見直し上限: 試行に記録が無い" in old and "反復番号の記録なし" in old


def test_gate_dry_run_goes_to_the_server_env_and_the_fingerprint_only_when_given(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ledger_eval, "fixed_checkout", lambda root, expected=None, **kw: "head")
    plain = _runner_with(monkeypatch, tmp_path, out=tmp_path / "a")
    dry = _runner_with(monkeypatch, tmp_path, out=tmp_path / "b", gate_dry_run=True)
    assert "SHERPA_LEDGER_GATE_DRYRUN" not in plain.server_env("plan_gate")
    assert dry.server_env("plan_gate")["SHERPA_LEDGER_GATE_DRYRUN"] == "1"
    assert dry.fingerprint != plain.fingerprint
    meta = json.loads((tmp_path / "b" / "metadata.json").read_text(encoding="utf-8"))
    assert meta["gate_dry_run"] is True and "gate_dry_run" not in json.loads((tmp_path / "a" / "metadata.json").read_text())
    assert meta["sherpa_env"]["SHERPA_LEDGER_GATE_DRYRUN"] == "1"


def test_end_state_never_calls_stopped_or_missing_attempts_complete():
    answer = {"completion": "stopped", "stop_kind": "user_stop", "notices": [{"kind": "x"}, {"kind": "a"}, "bad"]}
    stopped = sc.end_state(sc.classify_trial({"status": "ok"}, answer), answer)
    assert stopped["class"] == sc.STOPPED and stopped["completed"] is False and stopped["counted"] is False
    assert stopped["completion"] == "stopped" and stopped["stop_kind"] == "user_stop" and stopped["notice_kinds"] == ["a", "x"]
    done = {"completion": "complete"}
    assert sc.end_state(sc.classify_trial({"status": "ok"}, done), done)["completed"] is True
    partial = sc.end_state(sc.classify_trial({"status": "ok"}, {"completion": "partial"}), {"completion": "partial"})
    assert partial["class"] == sc.PRODUCT_PARTIAL and partial["completed"] is False and partial["counted"] is True
    stalled = sc.end_state(sc.classify_trial({"status": "failed", "stalled": True}, None), None)
    assert stalled["class"] == sc.INFRASTRUCTURE and stalled["completed"] is False
    assert stalled["completion"] is None and stalled["stop_kind"] is None and stalled["notice_kinds"] is None


def test_collect_adds_end_state_to_every_row():
    for row in rows_by_id().values():
        assert row["end_state"]["class"] == row["class"]
        assert row["end_state"]["completed"] is (row["class"] == sc.COMPLETE)


def test_summary_aggregates_target_check_and_gate_log_and_keeps_absent_records_apart():
    def check(status, decision, dry, missing):
        return {"status": status, "decision": decision, "dry_run": dry, "missing_count": missing}

    rows = [_row("plan_gate", "code", "q1", .5, .5, target_check=check("missing", "revert", True, 2),
                 gate_log=[{"decision": "revert"}, {"decision": "accept"}]),
            _row("plan_gate", "code", "q2", .5, .5, target_check=check("ok", "accept", True, 0),
                 gate_log=[{"decision": "accept"}]),
            _row("json", "code", "q3", .5, .5)]
    text = sc.summary_markdown(rows, ("code",), "m")
    assert "target_check: 記録あり 2 試行 / 記録なし 1 試行" in text
    assert "|missing|1|" in text and "|ok|1|" in text and "|revert|1|" in text and "|dry_run|2|" in text
    assert "missing の延べ: 2（missing_count の記録あり 2 / 2 試行、1 件以上あった試行 1）" in text
    assert "gate_log: 記録あり 2 試行 / 記録なし 1 試行" in text
    assert "|accept|2|" in text and "|判定|件数|" in text
    old = sc.summary_markdown([_row("json", "code", "q1", .5, .5)], ("code",), "m")
    assert "target_check: 記録あり 0 試行 / 記録なし 1 試行" in old and "missing の延べ" not in old
