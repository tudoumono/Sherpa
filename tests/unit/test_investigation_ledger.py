"""調査台帳の純関数部分（`sherpa.investigation_ledger`）の単体テスト。

`load_ledger`/`ledger_complete`/`no_progress`/`write_*_atomic`/coverage/reviews の契約を固定する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sherpa import investigation_ledger as L

_SRC = {"kind": "source", "path": "src/foo.py", "line": 1}
_SPEC = {"kind": "spec_doc", "path": "docs/x.md", "line": 1}


def _item(item_id: str, *, status: str = "source_confirmed", evidence=None,
          reason: str = "", owner: str = "worker-1", kind: str = "fact",
          subject: str = "foo.py", required_checks=None) -> dict:
    return {
        "id": item_id, "kind": kind, "subject": subject,
        "required_checks": required_checks if required_checks is not None else ["source"],
        "evidence": evidence if evidence is not None else [dict(_SRC)],
        "status": status, "reason": reason, "owner": owner,
    }


def _write_item_raw(dir: Path, item_id: str, payload) -> None:
    items_dir = dir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    (items_dir / f"{item_id}.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _manifest(*ids: str, question_kind: str = "list") -> dict:
    return {"question_kind": question_kind, "created_at": "2026-09-21T00:00:00Z", "items": list(ids)}


def _write_manifest_raw(dir: Path, payload) -> None:
    (dir / "manifest.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _verdict(tmp_path, *items, manifest_ids=None, **kw):
    for it in items:
        L.write_item_atomic(tmp_path, it)
    ids = manifest_ids if manifest_ids is not None else [it["id"] for it in items]
    L.write_manifest_atomic(tmp_path, _manifest(*ids))
    snap = L.load_ledger(tmp_path)
    return snap, L.ledger_complete(snap, **kw)


def _snap(items: dict, manifest=None, ids=None):
    manifest = manifest if manifest is not None else _manifest(*(ids if ids is not None else items))
    return L.LedgerSnapshot(manifest=manifest, items=items, invalid_ids=())


# ===== load_ledger / ledger_complete =====

def test_empty_and_missing_dir_are_empty_incomplete_snapshot(tmp_path):
    for d in (tmp_path, tmp_path / "does-not-exist"):
        snap = L.load_ledger(d)
        assert snap.manifest is None and snap.items == {} and snap.invalid_ids == ()
        verdict = L.ledger_complete(snap)
        assert verdict.complete is False
        assert verdict.non_terminal_ids == verdict.invalid_ids == verdict.missing_ids == ()
        assert verdict.unregistered_ids == ()
        assert verdict.terminal_counts == {}


def test_all_terminal_items_complete_true_with_counts(tmp_path):
    snap, verdict = _verdict(
        tmp_path, _item("a1"),
        _item("a2", status="spec_only", required_checks=["spec_doc"], evidence=[dict(_SPEC)]),
        _item("a3"))
    assert verdict.complete is True and verdict.manifest_invalid is False
    assert verdict.non_terminal_ids == () and verdict.invalid_ids == ()
    assert verdict.terminal_counts == {"source_confirmed": 2, "spec_only": 1}
    # required_extra=() は既定と同じ・source を追加必須にすると source を宣言していない a2 だけ未充足
    assert L.ledger_complete(snap, required_extra=()) == verdict
    extra = L.ledger_complete(snap, required_extra=("source",))
    assert extra.complete is False and extra.unsatisfied == {"a2": ("source",)}


def test_one_non_terminal_item_blocks_complete(tmp_path):
    _, verdict = _verdict(tmp_path, _item("done"), _item("open", status="pending"))
    assert verdict.complete is False and verdict.non_terminal_ids == ("open",)


_BAD_REASON = "x" * (L.REASON_MAX_LEN + 1)
_INVALID_ITEMS = {
    "broken_json": "{not valid json",
    "unknown_status": _item("unknown_status", status="not_a_real_status"),
    "id_mismatch": _item("some_other_id"),
    "evidence_text_key": _item("evidence_text_key", evidence=[{**_SRC, "kind": "read", "text": "本文"}]),
    "unreadable_no_reason": _item("unreadable_no_reason", status="unreadable", reason="", evidence=[]),
    "nfis_no_reason": _item("nfis_no_reason", status="not_found_in_scope", reason="", evidence=[]),
    "unavailable_no_reason": _item("unavailable_no_reason", status="unavailable", reason="", evidence=[]),
    "confirmed_no_evidence": _item("confirmed_no_evidence", evidence=[]),
    "spec_only_no_evidence": _item("spec_only_no_evidence", status="spec_only", evidence=[]),
    "conflict_no_evidence": _item("conflict_no_evidence", status="conflict", evidence=[]),
    "top_text": {**_item("top_text"), "text": "document body" * 10000},
    "top_extra": {**_item("top_extra"), "debug_note": "本来存在しないはずのキー"},
    "status_list": {**_item("status_list"), "status": []},
    "status_dict": {**_item("status_dict"), "status": {}},
    "evidence_extra_key": _item("evidence_extra_key", evidence=[{**_SRC, "kind": "read", "body": "資料本文"}]),
    "path_dict": _item("path_dict", evidence=[{"kind": "read", "path": {"text": "資料本文"}, "line": 1}]),
    "line_list": _item("line_list", evidence=[{"kind": "read", "path": "src/foo.py", "line": []}]),
    "long_subject": _item("long_subject", subject="x" * (L.SUBJECT_MAX_LEN + 1)),
    "long_reason": _item("long_reason", reason=_BAD_REASON),
    "rc_unknown": _item("rc_unknown", required_checks=["source", "not_a_real_kind"]),
    "rc_duplicate": _item("rc_duplicate", required_checks=["source", "source"]),
    "rc_empty": _item("rc_empty", required_checks=[]),
    "confirmed_spec_doc_only": _item("confirmed_spec_doc_only", evidence=[dict(_SPEC)]),
    "conflict_source_only": _item("conflict_source_only", status="conflict",
                                  required_checks=["source", "spec_doc"], evidence=[dict(_SRC)]),
    "conflict_spec_only": _item("conflict_spec_only", status="conflict",
                                required_checks=["source", "spec_doc"], evidence=[dict(_SPEC)]),
}


@pytest.mark.parametrize("stem", sorted(_INVALID_ITEMS))
def test_invalid_item_is_reported_without_raising(tmp_path, stem):
    stem_name = "file_a" if stem == "id_mismatch" else stem
    _write_item_raw(tmp_path, stem_name, _INVALID_ITEMS[stem])
    L.write_manifest_atomic(tmp_path, _manifest(stem_name))
    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)
    assert snap.invalid_ids == (stem_name,)
    assert verdict.invalid_ids == (stem_name,)
    assert stem_name not in snap.items
    assert verdict.complete is False


def test_reasoned_unreadable_is_valid(tmp_path):
    snap, verdict = _verdict(tmp_path, _item("u", status="unreadable",
                                              reason="ファイルが破損しており読み取れない", evidence=[]))
    assert "u" in snap.items
    assert verdict.invalid_ids == () and verdict.terminal_counts == {"unreadable": 1}
    assert verdict.complete is True


def test_conflict_with_both_kinds_of_evidence_is_valid(tmp_path):
    snap, verdict = _verdict(tmp_path, _item(
        "conflict_both", status="conflict", required_checks=["source", "spec_doc"],
        evidence=[dict(_SRC), dict(_SPEC)]))
    assert verdict.invalid_ids == () and verdict.complete is True
    assert L.ledger_complete(snap, required_extra=("source",)).complete is True


def test_manifest_references_missing_item_file(tmp_path):
    _, verdict = _verdict(tmp_path, _item("present"), manifest_ids=["present", "ghost"])
    assert verdict.missing_ids == ("ghost",) and verdict.complete is False


@pytest.mark.parametrize("rogue", [
    _item("rogue"), _item("rogue", status="pending"), "{not valid json"], ids=["terminal", "non_terminal", "broken"])
def test_unregistered_item_does_not_block_complete(tmp_path, rogue):
    L.write_item_atomic(tmp_path, _item("known"))
    if isinstance(rogue, str):
        _write_item_raw(tmp_path, "rogue", rogue)
    else:
        L.write_item_atomic(tmp_path, rogue)
    L.write_manifest_atomic(tmp_path, _manifest("known"))
    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)
    assert verdict.unregistered_ids == ("rogue",)
    assert verdict.missing_ids == () and verdict.non_terminal_ids == ()
    assert verdict.manifest_invalid is False and verdict.complete is True
    # 無効な未登録 item は invalid_ids には出ないが snap.invalid_ids では検知される
    assert verdict.invalid_ids == ()
    assert snap.invalid_ids == (("rogue",) if isinstance(rogue, str) else ())


def test_manifest_missing_or_broken_blocks_complete_even_with_all_terminal_items(tmp_path):
    L.write_item_atomic(tmp_path, _item("a1"))
    L.write_item_atomic(tmp_path, _item("a2", status="spec_only"))
    verdict = L.ledger_complete(L.load_ledger(tmp_path))
    assert verdict.manifest_invalid is True and verdict.complete is False
    (tmp_path / "manifest.json").write_text("{not valid json", encoding="utf-8")
    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)
    assert snap.manifest is None and verdict.manifest_invalid is True and verdict.complete is False


@pytest.mark.parametrize("payload", [
    {"created_at": "2026-09-21T00:00:00Z", "items": ["a1"]},
    {"question_kind": "list", "items": ["a1"]},
    {"question_kind": "list", "created_at": "2026-09-21T00:00:00Z", "items": ["a1"], "notes": "body" * 1000},
], ids=["no_question_kind", "no_created_at", "extra_key"])
def test_invalid_manifest_blocks_complete(tmp_path, payload):
    L.write_item_atomic(tmp_path, _item("a1"))
    _write_manifest_raw(tmp_path, payload)
    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)
    assert snap.manifest is None and verdict.manifest_invalid is True and verdict.complete is False


def test_unregistered_items_alone_do_not_complete_without_a_registered_item(tmp_path):
    L.write_item_atomic(tmp_path, _item("rogue"))
    L.write_manifest_atomic(tmp_path, _manifest())
    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)
    assert snap.manifest == {"question_kind": "list", "created_at": "2026-09-21T00:00:00Z", "items": []}
    assert verdict.unregistered_ids == ("rogue",)
    assert verdict.manifest_invalid is True and verdict.complete is False


# ===== validate_item / validate_manifest =====

def test_validate_item_contract():
    assert L.validate_item(_item("ok")) == []
    assert L.validate_item(_item("whatever")) == []   # expected_id 未指定なら id 一致は見ない
    problems = L.validate_item("not-a-dict")
    assert problems and all(isinstance(p, str) for p in problems)
    missing = _item("m")
    del missing["reason"]
    assert L.validate_item(missing) != []
    assert any("status" in p for p in L.validate_item(_item("bad", status="not_a_real_status")))
    assert L.validate_item(_item("some_other_id"), expected_id="file_a") != []
    assert L._validate_item(_item("ok"), expected_id="ok") is True
    assert L._validate_item(_item("bad", status="not_a_real_status"), expected_id="bad") is False


def test_validate_manifest_contract():
    assert L.validate_manifest(_manifest("a", "b")) == []
    assert L.validate_manifest("nope") != []
    assert L.validate_manifest({"question_kind": "list", "items": ["a"]}) != []
    assert L.validate_manifest({"question_kind": "", "created_at": "2026-09-21T00:00:00Z", "items": []}) != []


# ===== required_checks の充足（Verdict.unsatisfied） =====

def test_spec_only_declares_source_but_only_gathers_spec_doc_is_unsatisfied(tmp_path):
    snap, verdict = _verdict(tmp_path, _item(
        "id", status="spec_only", required_checks=["spec_doc", "source"], evidence=[dict(_SPEC)]))
    assert "id" in snap.items
    assert verdict.unsatisfied == {"id": ("source",)}
    assert "id" in verdict.non_terminal_ids
    assert verdict.complete is False


def test_unsatisfied_item_becomes_complete_once_switched_to_not_found_in_scope(tmp_path):
    snap, verdict = _verdict(tmp_path, _item(
        "id", status="not_found_in_scope", reason="登録範囲に見つからなかった",
        required_checks=["spec_doc", "source"], evidence=[]))
    assert verdict.unsatisfied == {} and verdict.non_terminal_ids == () and verdict.complete is True
    assert L.ledger_complete(snap, required_extra=("source",)).complete is True


def test_spec_only_with_required_checks_matching_evidence_is_satisfied_and_terminal(tmp_path):
    _, verdict = _verdict(tmp_path, _item(
        "id", status="spec_only", required_checks=["spec_doc"], evidence=[dict(_SPEC)]))
    assert verdict.unsatisfied == {} and verdict.non_terminal_ids == () and verdict.complete is True


def test_non_source_required_checks_never_block_completion(tmp_path):
    snap, verdict = _verdict(tmp_path, _item(
        "id", status="source_confirmed", required_checks=["source", "spec_doc", "callgraph", "log_config"],
        evidence=[dict(_SRC)]))
    assert verdict.unsatisfied == {} and verdict.complete is True
    assert L.ledger_complete(snap, required_extra=("spec_doc",)).complete is True


# ===== load_ledger: symlink を辿らない =====

def test_manifest_symlink_is_invalid_and_linked_ids_do_not_leak(tmp_path):
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    L.write_manifest_atomic(other_dir, _manifest("other-secret-1", "other-secret-2"))
    ledger_dir = tmp_path / "ledger"
    (ledger_dir / "items").mkdir(parents=True)
    (ledger_dir / "manifest.json").symlink_to(other_dir / "manifest.json")

    snap = L.load_ledger(ledger_dir)
    assert snap.manifest is None
    verdict = L.ledger_complete(snap)
    assert verdict.manifest_invalid is True and verdict.missing_ids == () and verdict.complete is False


def test_item_symlink_is_invalid_and_linked_content_is_not_read(tmp_path):
    other_dir = tmp_path / "other"
    L.write_item_atomic(other_dir, _item("other-secret-item"))
    ledger_dir = tmp_path / "ledger"
    (ledger_dir / "items").mkdir(parents=True)
    L.write_manifest_atomic(ledger_dir, _manifest("a"))
    (ledger_dir / "items" / "a.json").symlink_to(other_dir / "items" / "other-secret-item.json")

    snap = L.load_ledger(ledger_dir)
    assert "a" not in snap.items and snap.invalid_ids == ("a",)
    verdict = L.ledger_complete(snap)
    assert verdict.invalid_ids == ("a",) and verdict.complete is False


def test_items_dir_symlink_makes_ledger_invalid(tmp_path):
    other_dir = tmp_path / "other"
    L.write_item_atomic(other_dir, _item("other-secret-item"))
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    L.write_manifest_atomic(ledger_dir, _manifest("a"))
    (ledger_dir / "items").symlink_to(other_dir / "items")

    snap = L.load_ledger(ledger_dir)
    assert snap.items == {} and snap.invalid_ids == ()
    verdict = L.ledger_complete(snap)
    assert verdict.manifest_invalid is True and verdict.complete is False


def test_ledger_dir_itself_symlink_is_empty_snapshot(tmp_path):
    other_dir = tmp_path / "other"
    L.write_manifest_atomic(other_dir, _manifest("a"))
    L.write_item_atomic(other_dir, _item("a"))
    ledger_dir = tmp_path / "ledger"
    ledger_dir.symlink_to(other_dir)

    snap = L.load_ledger(ledger_dir)
    assert snap.manifest is None and snap.items == {} and snap.invalid_ids == ()
    assert L.ledger_complete(snap).complete is False


# ===== no_progress =====

def test_no_progress_returns_only_unchanged_non_terminal_items():
    def snap(moved_reason):
        return L.LedgerSnapshot(manifest=None, invalid_ids=(), items={
            "stuck": _item("stuck", status="in_progress", reason="調査中"),
            "moved": _item("moved", status="in_progress", reason=moved_reason),
            "done": _item("done"),
        })
    assert L.no_progress(snap("調査中"), snap("続報あり")) == ["stuck"]


def test_no_progress_ignores_items_missing_in_either_snapshot():
    prev = L.LedgerSnapshot(manifest=None, items={"only_prev": _item("only_prev", status="pending")}, invalid_ids=())
    curr = L.LedgerSnapshot(manifest=None, items={"only_curr": _item("only_curr", status="pending")}, invalid_ids=())
    assert L.no_progress(prev, curr) == []


def _unsatisfied_snapshot():
    item = _item("a", status="spec_only", evidence=[{"kind": "spec_doc", "path": "d.md", "line": 1}])
    item["required_checks"] = ["source", "spec_doc"]
    return L.LedgerSnapshot(manifest={"question_kind": "list", "created_at": "t", "items": ["a"]},
                            items={"a": item}, invalid_ids=())


def test_unsatisfied_unchanged_item_is_no_progress():
    from sherpa.providers.codex import ledger_gate as LG
    snap = _unsatisfied_snapshot()
    assert L.ledger_complete(snap).unsatisfied == {"a": ("source",)}
    assert L.no_progress(snap, snap) == ["a"]
    assert LG._ledger_progressed(snap, snap) is False


# ===== write_item_atomic / write_manifest_atomic =====

def test_write_item_atomic_round_trip_and_cleanliness(tmp_path):
    item = _item("rt")
    path = L.write_item_atomic(tmp_path, item)
    assert path == tmp_path / "items" / "rt.json"
    assert L.load_ledger(tmp_path).items["rt"] == item
    assert list((tmp_path / "items").glob("*.tmp")) == []


def test_write_item_atomic_rejects_path_traversal_id(tmp_path):
    with pytest.raises(ValueError):
        L.write_item_atomic(tmp_path, _item("../x"))


def test_write_manifest_atomic_round_trip(tmp_path):
    manifest = _manifest("a", "b", question_kind="compare")
    assert L.write_manifest_atomic(tmp_path, manifest) == tmp_path / "manifest.json"
    assert L.load_ledger(tmp_path).manifest == manifest


def test_load_ledger_is_deterministic_across_reads(tmp_path):
    L.write_item_atomic(tmp_path, _item("a"))
    L.write_item_atomic(tmp_path, _item("b", status="pending"))
    L.write_manifest_atomic(tmp_path, _manifest("a", "b", "c"))
    assert L.ledger_complete(L.load_ledger(tmp_path)) == L.ledger_complete(L.load_ledger(tmp_path))


def test_write_refuses_symlinked_dirs(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    ledger_dir = tmp_path / "investigation"
    ledger_dir.mkdir()
    (ledger_dir / "items").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        L.write_item_atomic(ledger_dir, _item("a"))
    assert list(outside.iterdir()) == []

    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        L.write_manifest_atomic(linked, {"question_kind": "list", "created_at": "t", "items": ["a"]})
    assert list(outside.iterdir()) == []


# ===== unverified（閉じた理由コード） =====

def test_unverified_is_terminal_and_does_not_block_completion():
    snap = _snap({"a": _item("a", status="unverified", reason="not_searched", evidence=[])})
    verdict = L.ledger_complete(snap)
    assert verdict.complete is True and verdict.terminal_counts == {"unverified": 1}


@pytest.mark.parametrize("reason,valid", [(r, True) for r in sorted(L.UNVERIFIED_REASON_CODES)]
                         + [("", False), ("自由記述の理由", False), ("search_truncated ", False)])
def test_unverified_reason_must_be_in_closed_vocabulary(reason, valid):
    problems = L.validate_item(_item("a", status="unverified", reason=reason, evidence=[]))
    assert (problems == []) is valid


# ===== coverage.jsonl =====

def test_append_and_load_coverage_round_trip(tmp_path):
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "hit")
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "truncated")
    L.append_coverage_atomic(tmp_path, "b", "read_doc", "unreadable")
    assert L.load_coverage(tmp_path) == {"a": ("hit", "truncated"), "b": ("unreadable",)}
    lines = [json.loads(x) for x in (tmp_path / "coverage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(set(x.keys()) == {"item", "tool", "outcome", "ts"} for x in lines)   # 本文・引数は書かない


def test_append_coverage_rejects_unsafe_item_id_and_unknown_outcome(tmp_path):
    with pytest.raises(ValueError):
        L.append_coverage_atomic(tmp_path, "../outside", "ripgrep_search", "hit")
    assert not (tmp_path / "coverage.jsonl").exists()
    with pytest.raises(ValueError):
        L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "not-a-real-outcome")


def test_load_coverage_empty_when_file_missing(tmp_path):
    assert L.load_coverage(tmp_path) == {}


@pytest.mark.parametrize("name,append", [
    ("coverage.jsonl", lambda d: L.append_coverage_atomic(d, "a", "ripgrep_search", "hit")),
    ("reviews.jsonl", lambda d: L.append_review_atomic(d, _review())),
])
def test_append_refuses_symlinked_jsonl_file(tmp_path, name, append):
    outside = tmp_path / "outside.jsonl"
    ledger_dir = tmp_path / "investigation"
    ledger_dir.mkdir()
    (ledger_dir / name).symlink_to(outside)
    with pytest.raises(OSError):
        append(ledger_dir)
    assert not outside.exists()


def test_load_coverage_ignores_broken_and_malformed_lines(tmp_path):
    (tmp_path / "coverage.jsonl").write_text(
        "{broken\n"
        + json.dumps({"item": "a", "tool": "ripgrep_search", "outcome": "hit", "ts": 1.0}) + "\n"
        + json.dumps({"item": "b", "tool": "ripgrep_search", "outcome": "not-a-real-outcome", "ts": 1.0}) + "\n"
        + json.dumps({"item": "c", "outcome": "hit", "ts": 1.0}) + "\n",
        encoding="utf-8")
    assert L.load_coverage(tmp_path) == {"a": ("hit",)}


# ===== apply_unverified_downgrades =====

def _not_found(item_id: str, subject: str = "対象") -> dict:
    return _item(item_id, status="not_found_in_scope", reason="調べたが見つからなかった",
                 subject=subject, evidence=[])


def test_downgrade_truncated_coverage_to_unverified_search_truncated(tmp_path):
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "truncated")
    new_snap = L.apply_unverified_downgrades(tmp_path, _snap({"a": _not_found("a")}))
    assert new_snap.items["a"]["status"] == "unverified"
    assert new_snap.items["a"]["reason"] == "search_truncated"
    assert json.loads((tmp_path / "items/a.json").read_text(encoding="utf-8")) == new_snap.items["a"]


def test_downgrade_no_coverage_at_all_to_unverified_not_searched(tmp_path):
    new_snap = L.apply_unverified_downgrades(tmp_path, _snap({"a": _not_found("a")}))
    assert new_snap.items["a"]["status"] == "unverified"
    assert new_snap.items["a"]["reason"] == "not_searched"


def test_downgrade_clean_zero_hit_coverage_stays_not_found_in_scope(tmp_path):
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "no_hits")
    L.append_coverage_atomic(tmp_path, "a", "es_search", "no_hits")
    new_snap = L.apply_unverified_downgrades(tmp_path, _snap({"a": _not_found("a")}))
    assert new_snap.items["a"]["status"] == "not_found_in_scope"
    assert new_snap.items["a"]["reason"] == "調べたが見つからなかった"
    assert not (tmp_path / "items/a.json").exists()


@pytest.mark.parametrize("outcome,expected_reason", [
    ("timeout", "timeout"), ("unreadable", "unreadable"), ("limit", "search_truncated"), ("error", "search_error"),
])
def test_downgrade_reason_priority_for_each_problem_outcome(tmp_path, outcome, expected_reason):
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", outcome)
    new_snap = L.apply_unverified_downgrades(tmp_path, _snap({"a": _not_found("a")}))
    assert new_snap.items["a"]["status"] == "unverified"
    assert new_snap.items["a"]["reason"] == expected_reason


def test_downgrade_ignores_unregistered_and_confirmed_and_other_statuses(tmp_path):
    for i in "abc":
        L.append_coverage_atomic(tmp_path, i, "ripgrep_search", "timeout")
    items = {
        "a": _not_found("a"),                                                     # 未登録
        "b": _item("b"),                                                          # 確認済み系
        "c": _item("c", status="unverified", reason="not_searched", evidence=[]), # 既に unverified
    }
    new_snap = L.apply_unverified_downgrades(tmp_path, _snap(items, ids=["b", "c"]))
    assert new_snap.items["a"]["status"] == "not_found_in_scope"
    assert new_snap.items["b"]["status"] == "source_confirmed"
    assert new_snap.items["c"]["reason"] == "not_searched"
    assert not (tmp_path / "items/b.json").exists() and not (tmp_path / "items/c.json").exists()


def test_downgrade_exclude_ids_keeps_items_already_not_found_before_this_turn(tmp_path):
    items = {i: _not_found(i) for i in "abc"}
    L.append_coverage_atomic(tmp_path, "c", "ripgrep_search", "truncated")
    new_snap = L.apply_unverified_downgrades(tmp_path, _snap(items), exclude_ids=frozenset({"a", "c"}))
    assert new_snap.items["a"]["status"] == "not_found_in_scope"      # 除外・記録なしは据え置き
    assert new_snap.items["c"]["reason"] == "search_truncated"        # 除外でも記録があれば判定する
    assert new_snap.items["b"]["status"] == "unverified"
    assert new_snap.items["b"]["reason"] == "not_searched"
    assert not (tmp_path / "items/a.json").exists()


# ===== 中間の見直し（reviews.jsonl） =====

def _review(verdict: str = "mostly_answered", added_items=None, extra_perspectives=None,
            ts: float = 1.0, terminal_count: int = 1) -> dict:
    return {"purpose": "依頼の目的", "perspectives": ["画面"], "summary": "分かったこと",
            "added_items": added_items or [], "removed_items": [], "verdict": verdict,
            "extra_perspectives": extra_perspectives or [], "ts": ts, "terminal_count": terminal_count}


def test_validate_review_entry_rejects_extra_missing_and_oversized_fields():
    assert L.validate_review_entry(_review()) == []
    missing = _review()
    del missing["summary"]
    no_count = _review()
    del no_count["terminal_count"]
    bad_cases = [
        {**_review(), "note": "x"},
        missing,
        {**_review(), "perspectives": []},
        {**_review(), "purpose": "x" * (L.SUBJECT_MAX_LEN + 1)},
        _review(verdict="insufficient", extra_perspectives=["観点"]),
        {**_review(), "added_items": [{"id": "../x", "reason": "理由"}]},
        {**_review(), "added_items": [{"id": "a", "reason": "理由", "extra": 1}]},
        {**_review(), "ts": True},
        _review(terminal_count=0),
        _review(terminal_count=-1),
        {**_review(), "terminal_count": True},
        no_count,
    ]
    for bad in bad_cases:
        assert L.validate_review_entry(bad) != [], bad


def test_append_and_load_reviews_round_trip_skips_invalid_lines(tmp_path):
    L.append_review_atomic(tmp_path, _review(verdict="insufficient"))
    L.append_review_atomic(tmp_path, _review(verdict="mostly_answered", extra_perspectives=["帳票"]))
    with (tmp_path / "reviews.jsonl").open("a", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps({"purpose": "欠落だらけ"}) + "\n")
    reviews = L.load_reviews(tmp_path)
    assert len(reviews) == 2
    assert reviews[0]["verdict"] == "insufficient"
    assert reviews[1]["extra_perspectives"] == ["帳票"]


def test_ledger_complete_requires_review_and_tracks_pending_added_items():
    manifest = _manifest("a", "b")
    items = {"a": _item("a")}
    snap = L.LedgerSnapshot(manifest=manifest, items=items, invalid_ids=())

    default = L.ledger_complete(snap, reviews=(_review(),))     # require_review 省略は review を見ない
    assert default.review_missing is False and default.review_pending_ids == ()

    complete_snap = _snap({"a": _item("a")})
    v_missing = L.ledger_complete(complete_snap, require_review=True)
    assert v_missing.complete is False and v_missing.review_missing is True

    added = (_review(added_items=[{"id": "b", "reason": "発見"}]),)
    v_pending = L.ledger_complete(snap, reviews=added, require_review=True)
    assert v_pending.complete is False and v_pending.review_missing is False
    assert v_pending.review_pending_ids == ("b",)
    assert "b" in v_pending.missing_ids

    done_snap = L.LedgerSnapshot(manifest=manifest, items={**items, "b": _item("b")}, invalid_ids=())
    v_done = L.ledger_complete(done_snap, reviews=added, require_review=True)
    assert v_done.complete is True and v_done.review_pending_ids == ()

    v_invalid = L.ledger_complete(done_snap, reviews=({"purpose": "欠落"},), require_review=True)
    assert v_invalid.review_missing is True


def test_load_reviews_and_append_refuse_when_dir_itself_is_symlink_or_not_a_directory(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    L.append_review_atomic(real_dir, _review())
    assert len(L.load_reviews(real_dir)) == 1
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)
    assert L.load_reviews(link_dir) == ()
    with pytest.raises(PermissionError):
        L.append_review_atomic(link_dir, _review())

    file_as_dir = tmp_path / "not_a_dir"
    file_as_dir.write_text("x", encoding="utf-8")
    assert L.load_reviews(file_as_dir) == ()
    with pytest.raises(PermissionError):
        L.append_review_atomic(file_as_dir, _review())


def test_append_review_atomic_rejects_beyond_count_and_byte_limits(tmp_path):
    for _ in range(L.REVIEWS_MAX_COUNT):
        L.append_review_atomic(tmp_path, _review())
    assert len(L.load_reviews(tmp_path)) == L.REVIEWS_MAX_COUNT
    with pytest.raises(ValueError):
        L.append_review_atomic(tmp_path, _review())

    bytes_dir = tmp_path / "bytes"
    bytes_dir.mkdir()
    (bytes_dir / "reviews.jsonl").write_bytes(b"x" * L.REVIEWS_MAX_BYTES)
    with pytest.raises(ValueError):
        L.append_review_atomic(bytes_dir, _review())


def test_sanitize_review_text_strips_control_chars_escapes_markdown_and_truncates():
    cleaned = L.sanitize_review_text("観点*です\n\t`コード`[リンク](x)|パイプ")
    assert "\n" not in cleaned and "\t" not in cleaned
    assert cleaned == r"観点\*です\`コード\`\[リンク\](x)\|パイプ"
    # 上限で切ったら末尾に省略の印を付ける。
    assert L.sanitize_review_text("x" * (L.REVIEW_TEXT_PER_ITEM_MAX + 50)) == "x" * L.REVIEW_TEXT_PER_ITEM_MAX + L.TRUNCATION_MARK
    assert L.sanitize_review_text("x" * L.REVIEW_TEXT_PER_ITEM_MAX) == "x" * L.REVIEW_TEXT_PER_ITEM_MAX
    assert L.sanitize_review_text(None) == ""
    joined = L.sanitize_review_text_list(["x" * L.REVIEW_TEXT_PER_ITEM_MAX for _ in range(20)])
    assert len(joined) == L.REVIEW_TEXT_TOTAL_MAX + len(L.TRUNCATION_MARK) and joined.endswith(L.TRUNCATION_MARK)
    assert L.sanitize_review_text_list([]) == ""
    assert L.sanitize_review_text_list(["帳票", None, "  ", "画面"]) == "帳票、画面"
