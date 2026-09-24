"""調査台帳の純関数部分（`sherpa.investigation_ledger`）の単体テスト。

正典: `docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md` §3/§7。ここでは統合（provider.py 等
への組み込み）は対象外——`load_ledger`/`ledger_complete`/`no_progress`/`write_item_atomic`/
`write_manifest_atomic` の契約だけを固定する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sherpa import investigation_ledger as L


def _item(item_id: str, *, status: str = "source_confirmed", evidence=None,
          reason: str = "", owner: str = "worker-1", kind: str = "fact",
          subject: str = "foo.py", required_checks=None) -> dict:
    """テスト用の正規形 item（既定は終端状態・有効）。"""
    return {
        "id": item_id,
        "kind": kind,
        "subject": subject,
        "required_checks": required_checks if required_checks is not None else ["source"],
        "evidence": evidence if evidence is not None else [{"kind": "source", "path": "src/foo.py", "line": 1}],
        "status": status,
        "reason": reason,
        "owner": owner,
    }


def _write_item_raw(dir: Path, item_id: str, payload) -> None:
    """`write_item_atomic` を経由せず、テストが意図的に壊れた内容を items/{id}.json に置くための素の書込。"""
    items_dir = dir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    (items_dir / f"{item_id}.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _manifest(*ids: str, question_kind: str = "list") -> dict:
    """テスト用の正規形 manifest（登録集合＝渡した id 一覧）。"""
    return {"question_kind": question_kind, "created_at": "2026-09-21T00:00:00Z", "items": list(ids)}


def _write_manifest_raw(dir: Path, payload) -> None:
    """`write_manifest_atomic` を経由せず、テストが意図的に壊れた内容を manifest.json に置くための素の書込。"""
    (dir / "manifest.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# ===== load_ledger / ledger_complete =====

def test_empty_dir_incomplete_zero_items(tmp_path):
    snap = L.load_ledger(tmp_path)
    assert snap.manifest is None
    assert snap.items == {}
    verdict = L.ledger_complete(snap)
    assert verdict.complete is False
    assert verdict.non_terminal_ids == ()
    assert verdict.invalid_ids == ()
    assert verdict.missing_ids == ()
    assert verdict.unregistered_ids == ()
    assert verdict.terminal_counts == {}


def test_missing_dir_is_empty_snapshot(tmp_path):
    missing = tmp_path / "does-not-exist"
    snap = L.load_ledger(missing)
    assert snap.manifest is None
    assert snap.items == {}
    assert snap.invalid_ids == ()
    assert L.ledger_complete(snap).complete is False


def test_all_terminal_items_complete_true_with_counts(tmp_path):
    L.write_item_atomic(tmp_path, _item("a1", status="source_confirmed"))
    L.write_item_atomic(tmp_path, _item("a2", status="spec_only", required_checks=["spec_doc"],
                                         evidence=[{"kind": "spec_doc", "path": "docs/x.md", "line": 1}]))
    L.write_item_atomic(tmp_path, _item("a3", status="source_confirmed"))
    L.write_manifest_atomic(tmp_path, _manifest("a1", "a2", "a3"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert verdict.complete is True
    assert verdict.manifest_invalid is False
    assert verdict.non_terminal_ids == ()
    assert verdict.invalid_ids == ()
    assert verdict.terminal_counts == {"source_confirmed": 2, "spec_only": 1}

    # required_extra=() では上と完全に同じ結果（既定値は現行と不変）。呼び出し側が source を
    # 追加で必須にすると、required_checks に source を宣言していない a2（spec_only）だけが
    # 未充足になる——a1/a3（source_confirmed・すでに source の evidence あり）は影響されない。
    assert L.ledger_complete(snap, required_extra=()) == verdict
    extra_verdict = L.ledger_complete(snap, required_extra=("source",))
    assert extra_verdict.complete is False
    assert extra_verdict.unsatisfied == {"a2": ("source",)}


def test_one_non_terminal_item_blocks_complete(tmp_path):
    L.write_item_atomic(tmp_path, _item("done", status="source_confirmed"))
    L.write_item_atomic(tmp_path, _item("open", status="pending"))
    L.write_manifest_atomic(tmp_path, _manifest("done", "open"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.complete is False
    assert verdict.non_terminal_ids == ("open",)


def test_broken_json_item_is_invalid_without_raising(tmp_path):
    _write_item_raw(tmp_path, "broken", "{not valid json")
    L.write_item_atomic(tmp_path, _item("ok", status="source_confirmed"))
    L.write_manifest_atomic(tmp_path, _manifest("broken", "ok"))  # 登録集合に含める＝invalid_ids に現れる条件

    snap = L.load_ledger(tmp_path)  # 例外を投げないことそのものが検証対象
    verdict = L.ledger_complete(snap)

    assert verdict.complete is False
    assert snap.invalid_ids == ("broken",)
    assert verdict.invalid_ids == ("broken",)
    assert "broken" not in snap.items


def test_unknown_status_is_invalid(tmp_path):
    _write_item_raw(tmp_path, "weird", _item("weird", status="not_a_real_status"))
    L.write_manifest_atomic(tmp_path, _manifest("weird"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.invalid_ids == ("weird",)
    assert verdict.invalid_ids == ("weird",)
    assert verdict.complete is False


def test_id_mismatch_with_stem_is_invalid(tmp_path):
    # ファイル名は "file_a" だが item 内の id は違う値。
    _write_item_raw(tmp_path, "file_a", _item("some_other_id", status="source_confirmed"))
    L.write_manifest_atomic(tmp_path, _manifest("file_a"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.invalid_ids == ("file_a",)
    assert verdict.invalid_ids == ("file_a",)
    assert "file_a" not in snap.items


def test_evidence_with_text_key_is_invalid(tmp_path):
    payload = _item("leaky", status="source_confirmed",
                     evidence=[{"kind": "read", "path": "src/foo.py", "line": 1, "text": "本文をここに書かない"}])
    _write_item_raw(tmp_path, "leaky", payload)
    L.write_manifest_atomic(tmp_path, _manifest("leaky"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.invalid_ids == ("leaky",)
    assert verdict.invalid_ids == ("leaky",)
    assert "leaky" not in snap.items


def test_unreadable_without_reason_is_invalid(tmp_path):
    """[中] 4巡目是正: 確認不能系の終端（unreadable）は理由が空なら無効——調べずに終端化できない。"""
    L.write_item_atomic(tmp_path, _item("no_reason_unreadable", status="unreadable", reason="", evidence=[]))
    L.write_manifest_atomic(tmp_path, _manifest("no_reason_unreadable"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("no_reason_unreadable",)


def test_not_found_in_scope_without_reason_is_invalid(tmp_path):
    """[中] 4巡目是正: not_found_in_scope も理由必須。"""
    L.write_item_atomic(tmp_path, _item("no_reason_nfis", status="not_found_in_scope", reason="", evidence=[]))
    L.write_manifest_atomic(tmp_path, _manifest("no_reason_nfis"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("no_reason_nfis",)


def test_unavailable_without_reason_is_invalid(tmp_path):
    """[中] 4巡目是正: unavailable も理由必須。"""
    L.write_item_atomic(tmp_path, _item("no_reason_unavailable", status="unavailable", reason="", evidence=[]))
    L.write_manifest_atomic(tmp_path, _manifest("no_reason_unavailable"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("no_reason_unavailable",)


def test_source_confirmed_without_evidence_is_invalid(tmp_path):
    """[中] 4巡目是正: 確認済み系の終端（source_confirmed）は根拠が空なら無効——
    根拠なしの「確認済み」を許さない。"""
    L.write_item_atomic(tmp_path, _item("no_evidence_confirmed", status="source_confirmed", evidence=[]))
    L.write_manifest_atomic(tmp_path, _manifest("no_evidence_confirmed"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("no_evidence_confirmed",)


def test_spec_only_without_evidence_is_invalid(tmp_path):
    """[中] 4巡目是正: spec_only も根拠必須。"""
    L.write_item_atomic(tmp_path, _item("no_evidence_spec_only", status="spec_only", evidence=[]))
    L.write_manifest_atomic(tmp_path, _manifest("no_evidence_spec_only"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("no_evidence_spec_only",)


def test_conflict_without_evidence_is_invalid(tmp_path):
    """[中] 4巡目是正: conflict も根拠必須。"""
    L.write_item_atomic(tmp_path, _item("no_evidence_conflict", status="conflict", evidence=[]))
    L.write_manifest_atomic(tmp_path, _manifest("no_evidence_conflict"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("no_evidence_conflict",)


def test_unreadable_with_reason_is_valid(tmp_path):
    """[中] 4巡目是正: 理由があれば unreadable は有効なまま（理由必須は「理由が空」だけを拒む）。"""
    L.write_item_atomic(tmp_path, _item("readable_unreadable", status="unreadable",
                                         reason="ファイルが破損しており読み取れない", evidence=[]))
    L.write_manifest_atomic(tmp_path, _manifest("readable_unreadable"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert "readable_unreadable" in snap.items
    assert verdict.invalid_ids == ()
    assert verdict.terminal_counts == {"unreadable": 1}
    assert verdict.complete is True


def test_item_top_level_extra_key_is_invalid(tmp_path):
    """[中] 2巡目是正: item 最上位も正規形8キーとの完全一致を要求する（余分なキーで本文が通らない）。"""
    payload = _item("leaky_top", status="source_confirmed")
    payload["text"] = "document body" * 10000
    _write_item_raw(tmp_path, "leaky_top", payload)
    L.write_manifest_atomic(tmp_path, _manifest("leaky_top"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.invalid_ids == ("leaky_top",)
    assert verdict.invalid_ids == ("leaky_top",)
    assert verdict.complete is False


def test_item_top_level_arbitrary_extra_key_is_invalid(tmp_path):
    """[中] 2巡目是正: `text` 以外の任意の余分キーも同様に無効（denylist ではなくホワイトリスト）。"""
    payload = _item("has_extra", status="source_confirmed")
    payload["debug_note"] = "本来存在しないはずのキー"
    _write_item_raw(tmp_path, "has_extra", payload)
    L.write_manifest_atomic(tmp_path, _manifest("has_extra"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("has_extra",)
    assert verdict.complete is False


def test_status_as_list_is_invalid_without_raising(tmp_path):
    """[中] 是正: 非 str の status（unhashable）でも例外を投げず無効にする（fail-safe）。"""
    payload = _item("bad_status_list", status="source_confirmed")
    payload["status"] = []
    _write_item_raw(tmp_path, "bad_status_list", payload)
    L.write_manifest_atomic(tmp_path, _manifest("bad_status_list"))

    snap = L.load_ledger(tmp_path)  # TypeError が出ないことそのものが検証対象
    verdict = L.ledger_complete(snap)

    assert snap.invalid_ids == ("bad_status_list",)
    assert verdict.invalid_ids == ("bad_status_list",)
    assert "bad_status_list" not in snap.items


def test_status_as_dict_is_invalid_without_raising(tmp_path):
    """[中] 是正: dict の status も同様に無効・例外なし。"""
    payload = _item("bad_status_dict", status="source_confirmed")
    payload["status"] = {}
    _write_item_raw(tmp_path, "bad_status_dict", payload)
    L.write_manifest_atomic(tmp_path, _manifest("bad_status_dict"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.invalid_ids == ("bad_status_dict",)
    assert verdict.invalid_ids == ("bad_status_dict",)
    assert "bad_status_dict" not in snap.items


def test_evidence_extra_key_is_invalid(tmp_path):
    """[中] 是正: evidence 要素は {kind, path, line} 以外のキーを持てない。"""
    payload = _item("extra_key", status="source_confirmed",
                     evidence=[{"kind": "read", "path": "src/foo.py", "line": 1, "body": "資料本文"}])
    _write_item_raw(tmp_path, "extra_key", payload)
    L.write_manifest_atomic(tmp_path, _manifest("extra_key"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("extra_key",)


def test_evidence_path_as_dict_is_invalid(tmp_path):
    """[中] 是正: path に入れ子データ（本文を隠す経路）を許さない。"""
    payload = _item("nested_path", status="source_confirmed",
                     evidence=[{"kind": "read", "path": {"text": "資料本文"}, "line": 1}])
    _write_item_raw(tmp_path, "nested_path", payload)
    L.write_manifest_atomic(tmp_path, _manifest("nested_path"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("nested_path",)


def test_evidence_line_as_list_is_invalid(tmp_path):
    """[中] 是正: line は int でなければならない。"""
    payload = _item("bad_line", status="source_confirmed",
                     evidence=[{"kind": "read", "path": "src/foo.py", "line": []}])
    _write_item_raw(tmp_path, "bad_line", payload)
    L.write_manifest_atomic(tmp_path, _manifest("bad_line"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("bad_line",)


def test_subject_over_max_len_is_invalid(tmp_path):
    """[中] 是正: subject の自由記述に本文を書き込む逃げ道を上限で塞ぐ。"""
    payload = _item("long_subject", status="source_confirmed", subject="x" * (L.SUBJECT_MAX_LEN + 1))
    _write_item_raw(tmp_path, "long_subject", payload)
    L.write_manifest_atomic(tmp_path, _manifest("long_subject"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("long_subject",)


def test_reason_over_max_len_is_invalid(tmp_path):
    """[中] 是正: reason にも同様の上限。"""
    payload = _item("long_reason", status="source_confirmed", reason="x" * (L.REASON_MAX_LEN + 1))
    _write_item_raw(tmp_path, "long_reason", payload)
    L.write_manifest_atomic(tmp_path, _manifest("long_reason"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("long_reason",)


def test_manifest_references_missing_item_file(tmp_path):
    L.write_item_atomic(tmp_path, _item("present", status="source_confirmed"))
    L.write_manifest_atomic(tmp_path, _manifest("present", "ghost"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert verdict.missing_ids == ("ghost",)
    assert verdict.complete is False


def test_item_not_in_manifest_is_unregistered_but_does_not_block_complete(tmp_path):
    L.write_item_atomic(tmp_path, _item("known", status="source_confirmed"))
    L.write_item_atomic(tmp_path, _item("rogue", status="source_confirmed"))
    L.write_manifest_atomic(tmp_path, _manifest("known"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert verdict.unregistered_ids == ("rogue",)
    assert verdict.missing_ids == ()
    # 未登録は親が把握していない情報として無視される＝complete を妨げない（このモジュールの裁定）。
    # ただし登録集合（manifest の items）自体は非空でなければならない（§High是正・下の
    # test_unregistered_items_alone_do_not_complete_without_a_registered_item と対）。
    assert verdict.manifest_invalid is False
    assert verdict.complete is True


def test_unregistered_non_terminal_item_does_not_block_complete(tmp_path):
    """[中] 2巡目是正: 未登録 item の状態は完了判定の対象外——非終端でも complete を妨げない
    （親が把握していない item は unregistered_ids にだけ現れる）。"""
    L.write_item_atomic(tmp_path, _item("known", status="source_confirmed"))
    L.write_item_atomic(tmp_path, _item("rogue", status="pending"))
    L.write_manifest_atomic(tmp_path, _manifest("known"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.non_terminal_ids == ()
    assert verdict.unregistered_ids == ("rogue",)
    assert verdict.complete is True


def test_unregistered_invalid_item_does_not_block_complete(tmp_path):
    """[中] 2巡目是正: 未登録 item が無効（壊れた JSON）でも complete を妨げない——
    unregistered_ids にだけ現れる（`snap.invalid_ids` には引き続き現れる＝検知自体はする）。"""
    L.write_item_atomic(tmp_path, _item("known", status="source_confirmed"))
    _write_item_raw(tmp_path, "rogue", "{not valid json")
    L.write_manifest_atomic(tmp_path, _manifest("known"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.invalid_ids == ("rogue",)
    assert verdict.invalid_ids == ()
    assert verdict.unregistered_ids == ("rogue",)
    assert verdict.complete is True


def test_missing_manifest_blocks_complete_even_with_all_terminal_items(tmp_path):
    """[高] 是正: manifest が無い（＝登録集合が確定していない）なら、item が全て終端でも complete
    にしない（正典§4「壊れた台帳から final を生成しない」）。"""
    L.write_item_atomic(tmp_path, _item("a1", status="source_confirmed"))
    L.write_item_atomic(tmp_path, _item("a2", status="spec_only"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.manifest_invalid is True
    assert verdict.complete is False


def test_broken_manifest_json_blocks_complete_even_with_all_terminal_items(tmp_path):
    """[高] 是正: manifest.json が壊れている（JSON パース不能）場合も complete=False。"""
    L.write_item_atomic(tmp_path, _item("a1", status="source_confirmed"))
    (tmp_path / "manifest.json").write_text("{not valid json", encoding="utf-8")

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.manifest is None
    assert verdict.manifest_invalid is True
    assert verdict.complete is False


def test_manifest_missing_question_kind_is_invalid(tmp_path):
    """[中] 3巡目是正: manifest の必須欄（question_kind）が欠けたら manifest_invalid・complete=False。"""
    L.write_item_atomic(tmp_path, _item("a1", status="source_confirmed"))
    _write_manifest_raw(tmp_path, {"created_at": "2026-09-21T00:00:00Z", "items": ["a1"]})

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.manifest is None
    assert verdict.manifest_invalid is True
    assert verdict.complete is False


def test_manifest_missing_created_at_is_invalid(tmp_path):
    """[中] 3巡目是正: manifest の必須欄（created_at）が欠けても同様。"""
    L.write_item_atomic(tmp_path, _item("a1", status="source_confirmed"))
    _write_manifest_raw(tmp_path, {"question_kind": "list", "items": ["a1"]})

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.manifest is None
    assert verdict.manifest_invalid is True
    assert verdict.complete is False


def test_manifest_extra_key_is_invalid(tmp_path):
    """[中] 3巡目是正: manifest も item と同じ流儀でキー集合の完全一致を要求する
    （余分なキーへ本文を書かせない）。"""
    L.write_item_atomic(tmp_path, _item("a1", status="source_confirmed"))
    _write_manifest_raw(tmp_path, {"question_kind": "list", "created_at": "2026-09-21T00:00:00Z",
                                    "items": ["a1"], "notes": "document body" * 1000})

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.manifest is None
    assert verdict.manifest_invalid is True
    assert verdict.complete is False


def test_unregistered_items_alone_do_not_complete_without_a_registered_item(tmp_path):
    """[高] 是正: manifest はあるが `items`（登録集合）が空配列 → 登録集合ゼロ。この状態で
    未登録 item だけが全て終端でも complete にはならない（unregistered は complete を妨げない、
    という緩和が「登録集合ゼロなら何でも complete」に化けないことを固定する）。"""
    L.write_item_atomic(tmp_path, _item("rogue", status="source_confirmed"))
    L.write_manifest_atomic(tmp_path, _manifest())  # items=[]

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert snap.manifest == {"question_kind": "list", "created_at": "2026-09-21T00:00:00Z", "items": []}
    assert verdict.unregistered_ids == ("rogue",)
    assert verdict.manifest_invalid is True
    assert verdict.complete is False


# ===== validate_item / validate_manifest（公開関数・真偽判定は not validate_*(...)） =====

def test_validate_item_valid_item_returns_empty_list():
    assert L.validate_item(_item("ok", status="source_confirmed")) == []


def test_validate_item_non_dict_returns_reason():
    problems = L.validate_item("not-a-dict")
    assert problems != []
    assert all(isinstance(p, str) for p in problems)


def test_validate_item_missing_required_key_returns_reason():
    payload = _item("missing_key", status="source_confirmed")
    del payload["reason"]
    assert L.validate_item(payload) != []


def test_validate_item_unknown_status_returns_reason():
    problems = L.validate_item(_item("bad", status="not_a_real_status"))
    assert problems != []
    assert any("status" in p for p in problems)


def test_validate_item_expected_id_mismatch_returns_reason():
    payload = _item("some_other_id", status="source_confirmed")
    assert L.validate_item(payload, expected_id="file_a") != []


def test_validate_item_expected_id_none_skips_id_check():
    """`expected_id` を渡さなければ id の一致は検査しない（load_ledger 以外の用途向け）。"""
    payload = _item("whatever", status="source_confirmed")
    assert L.validate_item(payload) == []


def test_underscore_validate_item_wrapper_matches_validate_item():
    valid = _item("ok", status="source_confirmed")
    invalid = _item("bad", status="not_a_real_status")
    assert L._validate_item(valid, expected_id="ok") is True
    assert L._validate_item(invalid, expected_id="bad") is False


def test_validate_manifest_valid_manifest_returns_empty_list():
    assert L.validate_manifest(_manifest("a", "b")) == []


def test_validate_manifest_non_dict_returns_reason():
    assert L.validate_manifest("nope") != []


def test_validate_manifest_missing_required_key_returns_reason():
    problems = L.validate_manifest({"question_kind": "list", "items": ["a"]})
    assert problems != []


def test_validate_manifest_empty_question_kind_returns_reason():
    problems = L.validate_manifest(
        {"question_kind": "", "created_at": "2026-09-21T00:00:00Z", "items": []})
    assert problems != []


# ===== required_checks の語彙閉包（EVIDENCE_KINDS の外・重複・空配列は無効） =====

def test_required_checks_unknown_kind_is_invalid(tmp_path):
    L.write_item_atomic(tmp_path, _item("bad_rc", status="source_confirmed",
                                         required_checks=["source", "not_a_real_kind"]))
    L.write_manifest_atomic(tmp_path, _manifest("bad_rc"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("bad_rc",)
    assert verdict.complete is False


def test_required_checks_duplicate_is_invalid(tmp_path):
    L.write_item_atomic(tmp_path, _item("dup_rc", status="source_confirmed",
                                         required_checks=["source", "source"]))
    L.write_manifest_atomic(tmp_path, _manifest("dup_rc"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("dup_rc",)


def test_required_checks_empty_is_invalid(tmp_path):
    """空配列は無効——何を確認すべきか宣言していない item は完了判定できない。"""
    L.write_item_atomic(tmp_path, _item("empty_rc", status="source_confirmed", required_checks=[]))
    L.write_manifest_atomic(tmp_path, _manifest("empty_rc"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("empty_rc",)


# ===== 終端状態が意味として要求する根拠種別（evidence の kind） =====

def test_source_confirmed_with_spec_doc_only_evidence_is_invalid(tmp_path):
    """source_confirmed は kind='source' の evidence が無ければ成立しない
    （設計書の根拠だけでは足りない）。"""
    L.write_item_atomic(tmp_path, _item(
        "spec_doc_only_confirmed", status="source_confirmed", required_checks=["source"],
        evidence=[{"kind": "spec_doc", "path": "docs/x.md", "line": 1}]))
    L.write_manifest_atomic(tmp_path, _manifest("spec_doc_only_confirmed"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("spec_doc_only_confirmed",)


def test_conflict_with_source_only_evidence_is_invalid(tmp_path):
    L.write_item_atomic(tmp_path, _item(
        "conflict_source_only", status="conflict", required_checks=["source", "spec_doc"],
        evidence=[{"kind": "source", "path": "src/foo.py", "line": 1}]))
    L.write_manifest_atomic(tmp_path, _manifest("conflict_source_only"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("conflict_source_only",)


def test_conflict_with_spec_doc_only_evidence_is_invalid(tmp_path):
    L.write_item_atomic(tmp_path, _item(
        "conflict_spec_only", status="conflict", required_checks=["source", "spec_doc"],
        evidence=[{"kind": "spec_doc", "path": "docs/x.md", "line": 1}]))
    L.write_manifest_atomic(tmp_path, _manifest("conflict_spec_only"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.invalid_ids == ("conflict_spec_only",)


def test_conflict_with_both_kinds_of_evidence_is_valid(tmp_path):
    """[対] 上の2件と対にして固定する: source と spec_doc の両方が揃えば conflict は有効。"""
    L.write_item_atomic(tmp_path, _item(
        "conflict_both", status="conflict", required_checks=["source", "spec_doc"],
        evidence=[{"kind": "source", "path": "src/foo.py", "line": 1},
                  {"kind": "spec_doc", "path": "docs/x.md", "line": 1}]))
    L.write_manifest_atomic(tmp_path, _manifest("conflict_both"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert verdict.invalid_ids == ()
    assert verdict.complete is True

    # conflict は source・spec_doc の両方をすでに evidence に持つため、source を追加で必須に
    # しても（required_extra）未充足にならず完了したまま。
    assert L.ledger_complete(snap, required_extra=("source",)).complete is True


# ===== required_checks の充足（Verdict.unsatisfied） =====
#
# item 自体は有効（validate_item を通る）でも、required_checks に宣言した根拠種別の一部が
# evidence に無ければ「未充足」として完了を妨げる——required_checks の宣言と実際に集めた証跡の
# 乖離を機械的に検知する（設計書の根拠だけで required_checks=[spec_doc, source] を満たしたと
# 誤判定しない）。

def test_spec_only_declares_source_but_only_gathers_spec_doc_is_unsatisfied(tmp_path):
    L.write_item_atomic(tmp_path, _item(
        "id", status="spec_only", required_checks=["spec_doc", "source"],
        evidence=[{"kind": "spec_doc", "path": "docs/x.md", "line": 1}]))
    L.write_manifest_atomic(tmp_path, _manifest("id"))

    snap = L.load_ledger(tmp_path)
    assert "id" in snap.items, "item 自体は有効なはず（無効なら invalid_ids に入る）"
    verdict = L.ledger_complete(snap)

    assert verdict.unsatisfied == {"id": ("source",)}
    assert "id" in verdict.non_terminal_ids, "未充足は継続判定が効くよう non_terminal_ids にも含める"
    assert verdict.complete is False


def test_unsatisfied_item_becomes_complete_once_switched_to_not_found_in_scope(tmp_path):
    """未充足だった item を not_found_in_scope＋reason に変えると、根拠種別の充足を要求しない
    終端になるため complete になる（他の登録 item が揃っていれば）。"""
    L.write_item_atomic(tmp_path, _item(
        "id", status="not_found_in_scope", reason="登録範囲に見つからなかった",
        required_checks=["spec_doc", "source"], evidence=[]))
    L.write_manifest_atomic(tmp_path, _manifest("id"))

    snap = L.load_ledger(tmp_path)
    verdict = L.ledger_complete(snap)

    assert verdict.unsatisfied == {}
    assert verdict.non_terminal_ids == ()
    assert verdict.complete is True

    # 理由付きの終端（reason 必須・evidence を持たない）は required_extra の対象外——source を
    # 追加で必須にしても、根拠なしの not_found_in_scope はそのまま完了を妨げない。
    assert L.ledger_complete(snap, required_extra=("source",)).complete is True


def test_spec_only_with_required_checks_matching_evidence_is_satisfied_and_terminal(tmp_path):
    L.write_item_atomic(tmp_path, _item(
        "id", status="spec_only", required_checks=["spec_doc"],
        evidence=[{"kind": "spec_doc", "path": "docs/x.md", "line": 1}]))
    L.write_manifest_atomic(tmp_path, _manifest("id"))

    verdict = L.ledger_complete(L.load_ledger(tmp_path))

    assert verdict.unsatisfied == {}
    assert verdict.non_terminal_ids == ()
    assert verdict.complete is True


# ===== load_ledger: symlink 拒否（RV 高-1・2026-09-22 9巡目是正） =====
#
# 台帳は model-shell が書ける領域にあり、リンク先を親権限（Sherpa 本体）で読むと本文・他会話の
# 情報が漏れる——load_ledger() は symlink を一切辿らない契約を固定する。

def test_manifest_symlink_is_invalid_and_linked_ids_do_not_leak(tmp_path):
    """manifest.json が他ディレクトリの manifest への symlink でも、リンク先を読まず manifest を
    無効にする——リンク先の登録 id が missing_ids に出てこない（漏洩しない）ことを固定する。"""
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    L.write_manifest_atomic(other_dir, _manifest("other-secret-1", "other-secret-2"))

    ledger_dir = tmp_path / "ledger"
    (ledger_dir / "items").mkdir(parents=True)
    (ledger_dir / "manifest.json").symlink_to(other_dir / "manifest.json")

    snap = L.load_ledger(ledger_dir)

    assert snap.manifest is None, "symlink の manifest.json のリンク先が読まれている"
    verdict = L.ledger_complete(snap)
    assert verdict.manifest_invalid is True
    assert verdict.missing_ids == (), "リンク先の登録 id が missing_ids として漏れている"
    assert verdict.complete is False


def test_item_symlink_is_invalid_and_linked_content_is_not_read(tmp_path):
    """item ファイルが symlink の場合、リンク先の中身を読まずその id を invalid_ids に入れる。"""
    other_dir = tmp_path / "other"
    L.write_item_atomic(other_dir, _item("other-secret-item", status="source_confirmed"))

    ledger_dir = tmp_path / "ledger"
    (ledger_dir / "items").mkdir(parents=True)
    L.write_manifest_atomic(ledger_dir, _manifest("a"))
    (ledger_dir / "items" / "a.json").symlink_to(other_dir / "items" / "other-secret-item.json")

    snap = L.load_ledger(ledger_dir)

    assert "a" not in snap.items, "symlink item のリンク先の中身が読み込まれている"
    assert snap.invalid_ids == ("a",)
    verdict = L.ledger_complete(snap)
    assert verdict.invalid_ids == ("a",)
    assert verdict.complete is False


def test_items_dir_symlink_makes_ledger_invalid(tmp_path):
    """items/ 自体が他ディレクトリへの symlink の場合、中を列挙せず items を空にし、登録集合
    （manifest）も無効にする。"""
    other_dir = tmp_path / "other"
    L.write_item_atomic(other_dir, _item("other-secret-item", status="source_confirmed"))

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    L.write_manifest_atomic(ledger_dir, _manifest("a"))
    (ledger_dir / "items").symlink_to(other_dir / "items")

    snap = L.load_ledger(ledger_dir)

    assert snap.items == {}, "symlink の items/ 配下が列挙・読み込みされている"
    assert snap.invalid_ids == ()
    verdict = L.ledger_complete(snap)
    assert verdict.manifest_invalid is True
    assert verdict.complete is False


def test_ledger_dir_itself_symlink_is_empty_snapshot(tmp_path):
    """台帳ディレクトリ自身が symlink の場合、空スナップショット（manifest なし）として扱う。"""
    other_dir = tmp_path / "other"
    L.write_manifest_atomic(other_dir, _manifest("a"))
    L.write_item_atomic(other_dir, _item("a", status="source_confirmed"))

    ledger_dir = tmp_path / "ledger"
    ledger_dir.symlink_to(other_dir)

    snap = L.load_ledger(ledger_dir)

    assert snap.manifest is None
    assert snap.items == {}
    assert snap.invalid_ids == ()
    assert L.ledger_complete(snap).complete is False


# ===== no_progress =====

def test_no_progress_returns_only_unchanged_non_terminal_items(tmp_path):
    prev = L.LedgerSnapshot(
        manifest=None,
        items={
            "stuck": _item("stuck", status="in_progress", reason="調査中"),
            "moved": _item("moved", status="in_progress", reason="調査中"),
            "done": _item("done", status="source_confirmed"),
        },
        invalid_ids=(),
    )
    curr = L.LedgerSnapshot(
        manifest=None,
        items={
            "stuck": _item("stuck", status="in_progress", reason="調査中"),          # 無変化
            "moved": _item("moved", status="in_progress", reason="続報あり"),          # reason が変化
            "done": _item("done", status="source_confirmed"),                        # 終端は対象外
        },
        invalid_ids=(),
    )

    assert L.no_progress(prev, curr) == ["stuck"]


def test_no_progress_ignores_items_missing_in_either_snapshot(tmp_path):
    prev = L.LedgerSnapshot(manifest=None, items={"only_prev": _item("only_prev", status="pending")}, invalid_ids=())
    curr = L.LedgerSnapshot(manifest=None, items={"only_curr": _item("only_curr", status="pending")}, invalid_ids=())

    assert L.no_progress(prev, curr) == []


# ===== write_item_atomic / write_manifest_atomic =====

def test_write_item_atomic_round_trip_readable_by_load_ledger(tmp_path):
    item = _item("rt", status="source_confirmed")
    path = L.write_item_atomic(tmp_path, item)

    assert path == tmp_path / "items" / "rt.json"
    snap = L.load_ledger(tmp_path)
    assert snap.items["rt"] == item


def test_write_item_atomic_rejects_path_traversal_id(tmp_path):
    with pytest.raises(ValueError):
        L.write_item_atomic(tmp_path, _item("../x", status="source_confirmed"))


def test_write_item_atomic_leaves_no_tmp_file_behind(tmp_path):
    L.write_item_atomic(tmp_path, _item("clean", status="source_confirmed"))

    leftovers = list((tmp_path / "items").glob("*.tmp"))
    assert leftovers == []


def test_write_manifest_atomic_round_trip(tmp_path):
    manifest = _manifest("a", "b", question_kind="compare")
    path = L.write_manifest_atomic(tmp_path, manifest)

    assert path == tmp_path / "manifest.json"
    snap = L.load_ledger(tmp_path)
    assert snap.manifest == manifest


# ===== 決定性 =====

def test_load_ledger_is_deterministic_across_reads(tmp_path):
    L.write_item_atomic(tmp_path, _item("a", status="source_confirmed"))
    L.write_item_atomic(tmp_path, _item("b", status="pending"))
    L.write_manifest_atomic(tmp_path, _manifest("a", "b", "c"))

    first = L.ledger_complete(L.load_ledger(tmp_path))
    second = L.ledger_complete(L.load_ledger(tmp_path))

    assert first == second


# ===== 書込側も symlink を辿らない（MCP サーバはサンドボックスの外で動く） =====

def test_write_item_atomic_refuses_symlinked_items_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    ledger_dir = tmp_path / "investigation"
    ledger_dir.mkdir()
    (ledger_dir / "items").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        L.write_item_atomic(ledger_dir, _item("a"))
    assert list(outside.iterdir()) == [], "symlink 先へ書いてはいけない"


def test_write_manifest_atomic_refuses_symlinked_ledger_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    ledger_dir = tmp_path / "investigation"
    ledger_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        L.write_manifest_atomic(ledger_dir, {"question_kind": "list", "created_at": "t", "items": ["a"]})
    assert list(outside.iterdir()) == []


# ===== 未充足の終端状態は無進捗判定でも非終端 =====

def _unsatisfied_snapshot():
    item = _item("a", status="spec_only", evidence=[{"kind": "spec_doc", "path": "d.md", "line": 1}])
    item["required_checks"] = ["source", "spec_doc"]
    manifest = {"question_kind": "list", "created_at": "t", "items": ["a"]}
    return L.LedgerSnapshot(manifest=manifest, items={"a": item}, invalid_ids=())


def test_no_progress_reports_unchanged_unsatisfied_item():
    snap = _unsatisfied_snapshot()
    assert L.ledger_complete(snap).unsatisfied == {"a": ("source",)}
    assert L.no_progress(snap, snap) == ["a"]


def test_ledger_progressed_false_when_unsatisfied_item_unchanged():
    from sherpa.providers.codex import provider as PV
    snap = _unsatisfied_snapshot()
    assert PV._ledger_progressed(snap, snap) is False
