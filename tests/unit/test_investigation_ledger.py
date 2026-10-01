"""調査台帳の純関数部分（`sherpa.investigation_ledger`）の単体テスト。

正典: `docs/archive/2026-09-21-調査台帳を文脈の外に置く.md` §3/§7。ここでは統合（provider.py 等
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


# ===== COD-16（`docs/proposals/2026-09-29-調査の網羅と未確認の明示.md` §2）: `unverified` =====

def test_unverified_is_terminal_and_does_not_block_completion():
    item = _item("a", status="unverified", reason="not_searched", evidence=[])
    manifest = _manifest("a")
    snap = L.LedgerSnapshot(manifest=manifest, items={"a": item}, invalid_ids=())
    verdict = L.ledger_complete(snap)
    assert verdict.complete is True
    assert verdict.terminal_counts == {"unverified": 1}


@pytest.mark.parametrize("reason", sorted(L.UNVERIFIED_REASON_CODES))
def test_unverified_accepts_each_closed_reason_code(reason):
    item = _item("a", status="unverified", reason=reason, evidence=[])
    assert L.validate_item(item) == []


@pytest.mark.parametrize("reason", ["", "自由記述の理由", "search_truncated "])
def test_unverified_rejects_reason_outside_closed_vocabulary(reason):
    item = _item("a", status="unverified", reason=reason, evidence=[])
    assert L.validate_item(item) != []


# ===== coverage.jsonl（項目ごとの記録）=====

def test_append_and_load_coverage_round_trip(tmp_path):
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "hit")
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "truncated")
    L.append_coverage_atomic(tmp_path, "b", "read_doc", "unreadable")
    coverage = L.load_coverage(tmp_path)
    assert coverage == {"a": ("hit", "truncated"), "b": ("unreadable",)}
    # 本文・引数は書かない契約——各行はちょうど4キー（item/tool/outcome/ts）だけ。
    lines = [json.loads(l) for l in (tmp_path / "coverage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(set(l.keys()) == {"item", "tool", "outcome", "ts"} for l in lines)


def test_append_coverage_rejects_unsafe_item_id(tmp_path):
    with pytest.raises(ValueError):
        L.append_coverage_atomic(tmp_path, "../outside", "ripgrep_search", "hit")
    assert not (tmp_path / "coverage.jsonl").exists()


def test_append_coverage_rejects_unknown_outcome(tmp_path):
    with pytest.raises(ValueError):
        L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "not-a-real-outcome")


def test_load_coverage_empty_when_file_missing(tmp_path):
    assert L.load_coverage(tmp_path) == {}


def test_append_coverage_atomic_refuses_symlinked_coverage_file(tmp_path):
    """`coverage.jsonl` 自体が symlink に差し替えられていたら（`_reject_symlinked_dir` は
    dir の経路しか見ないため、この TOCTOU はファイル側で別に塞ぐ）追記せず `OSError`。"""
    outside = tmp_path / "outside.jsonl"
    ledger_dir = tmp_path / "investigation"
    ledger_dir.mkdir()
    (ledger_dir / "coverage.jsonl").symlink_to(outside)
    with pytest.raises(OSError):
        L.append_coverage_atomic(ledger_dir, "a", "ripgrep_search", "hit")
    assert not outside.exists(), "symlink 先へ書いてはいけない"


def test_load_coverage_ignores_broken_and_malformed_lines(tmp_path):
    (tmp_path / "coverage.jsonl").write_text(
        "{broken\n"
        + json.dumps({"item": "a", "tool": "ripgrep_search", "outcome": "hit", "ts": 1.0}) + "\n"
        + json.dumps({"item": "b", "tool": "ripgrep_search", "outcome": "not-a-real-outcome", "ts": 1.0}) + "\n"
        + json.dumps({"item": "c", "outcome": "hit", "ts": 1.0}) + "\n",   # tool 欠落＝キー集合不一致
        encoding="utf-8")
    assert L.load_coverage(tmp_path) == {"a": ("hit",)}


# ===== apply_unverified_downgrades（提案書§5 受け入れ）=====

def _not_found(item_id: str, subject: str = "対象") -> dict:
    return _item(item_id, status="not_found_in_scope", reason="調べたが見つからなかった",
                subject=subject, evidence=[])


def test_downgrade_truncated_coverage_to_unverified_search_truncated(tmp_path):
    """受け入れ①: 切り詰めが起きた検索に item を付けた項目を not_found_in_scope で終えると
    unverified（理由 search_truncated）になる。"""
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "truncated")
    manifest = _manifest("a")
    snap = L.LedgerSnapshot(manifest=manifest, items={"a": _not_found("a")}, invalid_ids=())
    new_snap = L.apply_unverified_downgrades(tmp_path, snap)
    assert new_snap.items["a"]["status"] == "unverified"
    assert new_snap.items["a"]["reason"] == "search_truncated"
    on_disk = json.loads((tmp_path / "items/a.json").read_text(encoding="utf-8"))
    assert on_disk == new_snap.items["a"]


def test_downgrade_no_coverage_at_all_to_unverified_not_searched(tmp_path):
    """受け入れ②: item 付きの検索が無い「無い」は unverified（not_searched）になる。"""
    manifest = _manifest("a")
    snap = L.LedgerSnapshot(manifest=manifest, items={"a": _not_found("a")}, invalid_ids=())
    new_snap = L.apply_unverified_downgrades(tmp_path, snap)
    assert new_snap.items["a"]["status"] == "unverified"
    assert new_snap.items["a"]["reason"] == "not_searched"


def test_downgrade_clean_zero_hit_coverage_stays_not_found_in_scope(tmp_path):
    """受け入れ③: 切り詰めの無い0件の検索だけなら not_found_in_scope のまま。"""
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "no_hits")
    L.append_coverage_atomic(tmp_path, "a", "es_search", "no_hits")
    manifest = _manifest("a")
    snap = L.LedgerSnapshot(manifest=manifest, items={"a": _not_found("a")}, invalid_ids=())
    new_snap = L.apply_unverified_downgrades(tmp_path, snap)
    assert new_snap.items["a"]["status"] == "not_found_in_scope"
    assert new_snap.items["a"]["reason"] == "調べたが見つからなかった"
    assert not (tmp_path / "items/a.json").exists(), "書換えが起きないので元のまま（このテストは書いていない）"


@pytest.mark.parametrize("outcome,expected_reason", [
    ("timeout", "timeout"),
    ("unreadable", "unreadable"),
    ("limit", "search_truncated"),
    ("error", "search_error"),
])
def test_downgrade_reason_priority_for_each_problem_outcome(tmp_path, outcome, expected_reason):
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", outcome)
    manifest = _manifest("a")
    snap = L.LedgerSnapshot(manifest=manifest, items={"a": _not_found("a")}, invalid_ids=())
    new_snap = L.apply_unverified_downgrades(tmp_path, snap)
    assert new_snap.items["a"]["status"] == "unverified"
    assert new_snap.items["a"]["reason"] == expected_reason


def test_downgrade_ignores_unregistered_and_confirmed_and_other_statuses(tmp_path):
    """登録集合に無い item・確認済み系の item・既に unverified の item は対象外
    （coverage に問題があっても書き換えない）。"""
    L.append_coverage_atomic(tmp_path, "a", "ripgrep_search", "timeout")
    L.append_coverage_atomic(tmp_path, "b", "ripgrep_search", "timeout")
    L.append_coverage_atomic(tmp_path, "c", "ripgrep_search", "timeout")
    manifest = _manifest("b", "c")   # "a" は未登録
    items = {
        "a": _not_found("a"),                                      # 未登録
        "b": _item("b", status="source_confirmed"),                # 確認済み系
        "c": _item("c", status="unverified", reason="not_searched", evidence=[]),   # 既に unverified
    }
    snap = L.LedgerSnapshot(manifest=manifest, items=items, invalid_ids=())
    new_snap = L.apply_unverified_downgrades(tmp_path, snap)
    assert new_snap.items["a"]["status"] == "not_found_in_scope"   # 未登録は素通り
    assert new_snap.items["b"]["status"] == "source_confirmed"
    assert new_snap.items["c"]["reason"] == "not_searched"         # 変わらない
    assert not (tmp_path / "items/b.json").exists() and not (tmp_path / "items/c.json").exists()


def test_downgrade_exclude_ids_keeps_items_already_not_found_before_this_turn(tmp_path):
    """`exclude_ids`（このターンの開始時点で既に not_found_in_scope だった item）に
    含まれる id は、item 付きの記録が無ければ降格しない——前のターンで確定した項目を「調べた
    記録が無い」という理由だけで unverified/not_searched にしない。記録があれば通常どおり判定する。"""
    manifest = _manifest("a", "b", "c")
    items = {"a": _not_found("a"), "b": _not_found("b"), "c": _not_found("c")}
    L.append_coverage_atomic(tmp_path, "c", "ripgrep_search", "truncated")   # c だけ記録あり
    snap = L.LedgerSnapshot(manifest=manifest, items=items, invalid_ids=())
    new_snap = L.apply_unverified_downgrades(tmp_path, snap, exclude_ids=frozenset({"a", "c"}))
    assert new_snap.items["a"]["status"] == "not_found_in_scope"   # 除外対象・記録なしは据え置き
    assert new_snap.items["c"]["reason"] == "search_truncated"     # 除外対象でも記録があれば判定する
    assert new_snap.items["b"]["status"] == "unverified"           # 除外されない item は通常どおり降格
    assert new_snap.items["b"]["reason"] == "not_searched"
    assert not (tmp_path / "items/a.json").exists()


# ===== COD-18 ⑤（利用者2026-10-01指示・`docs/proposals/課題管理簿.md` COD-18）: =====
# ===== 中間の見直し（reviews.jsonl・`validate_review_entry`/`ledger_complete(require_review=...)`） =====

def _review(verdict: str = "mostly_answered", added_items=None, extra_perspectives=None,
           ts: float = 1.0, terminal_count: int = 1) -> dict:
    return {"purpose": "依頼の目的", "perspectives": ["画面"], "summary": "分かったこと",
            "added_items": added_items or [], "removed_items": [], "verdict": verdict,
            "extra_perspectives": extra_perspectives or [], "ts": ts,
            "terminal_count": terminal_count}


def test_validate_review_entry_rejects_extra_missing_and_oversized_fields():
    """正規形はキー集合完全一致・各欄の型/上限・`insufficient` では `extra_perspectives` 空を
    要求する（本文を持つ唯一の正規形だが、自由記述欄の無制限肥大化は上限で塞ぐ）。"""
    assert L.validate_review_entry(_review()) == []
    # 余分なキー（本文混入の経路）は拒否。
    assert L.validate_review_entry({**_review(), "note": "x"}) != []
    # 必須キー欠落。
    missing = _review()
    del missing["summary"]
    assert L.validate_review_entry(missing) != []
    # perspectives は1件以上必須（空配列は無効）。
    assert L.validate_review_entry({**_review(), "perspectives": []}) != []
    # purpose の上限超過。
    assert L.validate_review_entry({**_review(), "purpose": "x" * (L.SUBJECT_MAX_LEN + 1)}) != []
    # verdict が insufficient なのに extra_perspectives が非空＝矛盾として拒否。
    bad = _review(verdict="insufficient", extra_perspectives=["観点"])
    assert L.validate_review_entry(bad) != []
    # added_items の要素はキー集合 {id, reason} ちょうど・id は安全な書式のみ。
    assert L.validate_review_entry(
        {**_review(), "added_items": [{"id": "../x", "reason": "理由"}]}) != []
    assert L.validate_review_entry(
        {**_review(), "added_items": [{"id": "a", "reason": "理由", "extra": 1}]}) != []
    # ts は数値（bool は int の部分型のため明示的に拒否）。
    assert L.validate_review_entry({**_review(), "ts": True}) != []


def test_append_review_atomic_refuses_symlinked_reviews_file(tmp_path):
    """`reviews.jsonl` 自体が symlink に差し替えられていたら追記せず `OSError`（`coverage.jsonl`
    と同じ TOCTOU 対策・ファイル側で個別確認する）。"""
    outside = tmp_path / "outside.jsonl"
    ledger_dir = tmp_path / "investigation"
    ledger_dir.mkdir()
    (ledger_dir / "reviews.jsonl").symlink_to(outside)
    with pytest.raises(OSError):
        L.append_review_atomic(ledger_dir, _review())
    assert not outside.exists(), "symlink 先へ書いてはいけない"


def test_append_and_load_reviews_round_trip_skips_invalid_lines(tmp_path):
    L.append_review_atomic(tmp_path, _review(verdict="insufficient"))
    L.append_review_atomic(tmp_path, _review(verdict="mostly_answered", extra_perspectives=["帳票"]))
    # 壊れた行・正規形に合わない行は読み飛ばす（fail-safe・`load_coverage` と同じ流儀）。
    with (tmp_path / "reviews.jsonl").open("a", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps({"purpose": "欠落だらけ"}) + "\n")
    reviews = L.load_reviews(tmp_path)
    assert len(reviews) == 2
    assert reviews[0]["verdict"] == "insufficient"
    assert reviews[1]["extra_perspectives"] == ["帳票"]


def test_ledger_complete_requires_review_and_tracks_pending_added_items():
    """`require_review=True` のとき: 見直しが1件も無ければ `review_missing`＝complete 不可。
    見直しはあるが `added_items` に挙げた id が未終端・未登録なら `review_pending_ids`＝complete
    不可。その id が終端になれば complete。`require_review=False`（既定）では現行と完全に
    同じ（どちらも常に偽/空）。"""
    manifest = _manifest("a", "b")
    items = {"a": _item("a", status="source_confirmed")}
    snap = L.LedgerSnapshot(manifest=manifest, items=items, invalid_ids=())

    # 既定（require_review 省略）は review を一切見ない＝現行と同じ結果。
    verdict_default = L.ledger_complete(snap, reviews=(_review(),))
    assert verdict_default.review_missing is False
    assert verdict_default.review_pending_ids == ()

    # 見直しが無い＝review_missing で未完了（他が全部揃っていても）。
    complete_items = {"a": _item("a", status="source_confirmed")}
    complete_snap = L.LedgerSnapshot(manifest=_manifest("a"), items=complete_items, invalid_ids=())
    v_missing = L.ledger_complete(complete_snap, require_review=True)
    assert v_missing.complete is False
    assert v_missing.review_missing is True

    # 見直しはあるが、足したと申告した id "b" がまだ非終端＝review_pending。
    v_pending = L.ledger_complete(snap, reviews=(_review(added_items=[{"id": "b", "reason": "発見"}]),),
                                  require_review=True)
    assert v_pending.complete is False
    assert v_pending.review_missing is False
    assert v_pending.review_pending_ids == ("b",)
    assert "b" in v_pending.missing_ids   # 登録済みだが item ファイル未作成＝継続判定は missing 経路で拾う

    # "b" を終端化すれば、同じ見直しで complete になる。
    done_items = {**items, "b": _item("b", status="source_confirmed")}
    done_snap = L.LedgerSnapshot(manifest=manifest, items=done_items, invalid_ids=())
    v_done = L.ledger_complete(done_snap, reviews=(_review(added_items=[{"id": "b", "reason": "発見"}]),),
                               require_review=True)
    assert v_done.complete is True
    assert v_done.review_pending_ids == ()

    # 無効な見直し（正規形に合わない）は無視され、review_missing のまま。
    v_invalid_review = L.ledger_complete(done_snap, reviews=({"purpose": "欠落"},), require_review=True)
    assert v_invalid_review.review_missing is True


# ===== RV是正（1巡目・順番）: terminal_count は見直しを書いた時点の終端件数・1未満は無効 =====

def test_validate_review_entry_rejects_terminal_count_below_one():
    """`terminal_count`（見直しを書いた時点で台帳にあった終端 item の件数）は1以上の整数が必須
    ——0・負・非整数・欠落はすべて無効（受付の拒否は mcp_server.py 側の責務だが、正規形としても
    1未満の見直しは `load_reviews()` の「有効な見直し」から外れる・下記で確認）。"""
    assert L.validate_review_entry(_review(terminal_count=1)) == []
    assert L.validate_review_entry(_review(terminal_count=0)) != []
    assert L.validate_review_entry(_review(terminal_count=-1)) != []
    assert L.validate_review_entry({**_review(), "terminal_count": True}) != []
    bad = _review()
    del bad["terminal_count"]
    assert L.validate_review_entry(bad) != []


# ===== RV是正（3巡目・置き場所）: load_reviews/append_review_atomic は台帳と同じ symlink 規律 =====

def test_load_reviews_and_append_refuse_when_dir_itself_is_symlink_or_not_a_directory(tmp_path):
    """`dir` 自身が symlink、またはディレクトリでなければ `load_ledger` と同じ流儀で拒否する——
    `reviews.jsonl` という末端ファイルだけを見ていると、`dir` 自体が別の調査台帳ディレクトリへの
    symlink の場合にリンク先の見直しを読み込んでしまう（そのリーク経路を塞ぐ）。"""
    # dir 自体が symlink（リンク先に正当な見直しがあっても読まない）。
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    L.append_review_atomic(real_dir, _review())
    assert len(L.load_reviews(real_dir)) == 1   # リンク先を直接読めば1件あることの確認
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)
    assert L.load_reviews(link_dir) == ()
    with pytest.raises(PermissionError):
        L.append_review_atomic(link_dir, _review())

    # dir がディレクトリでない（通常ファイル）。
    file_as_dir = tmp_path / "not_a_dir"
    file_as_dir.write_text("x", encoding="utf-8")
    assert L.load_reviews(file_as_dir) == ()
    with pytest.raises(PermissionError):
        L.append_review_atomic(file_as_dir, _review())


# ===== RV是正（4巡目・上限）: 件数とバイト数の上限 =====

def test_append_review_atomic_rejects_beyond_count_and_byte_limits(tmp_path):
    """件数が `REVIEWS_MAX_COUNT` に達した追記、ファイル総バイト数が `REVIEWS_MAX_BYTES` を
    超える追記はいずれも書き込まず `ValueError`。読込（`load_reviews`）も上限までしか読まない。"""
    for _ in range(L.REVIEWS_MAX_COUNT):
        L.append_review_atomic(tmp_path, _review())
    assert len(L.load_reviews(tmp_path)) == L.REVIEWS_MAX_COUNT
    with pytest.raises(ValueError):
        L.append_review_atomic(tmp_path, _review())   # 件数の上限超過

    # バイト数の上限: 上限ぴったりまで既存ファイルを埋めてから1件追記しようとすると拒否される
    # （既存コンテンツはテスト側で直接書く——内部関数の monkeypatch ではなくファイルの実体を操作）。
    bytes_dir = tmp_path / "bytes"
    bytes_dir.mkdir()
    path = bytes_dir / "reviews.jsonl"
    path.write_bytes(b"x" * L.REVIEWS_MAX_BYTES)
    with pytest.raises(ValueError):
        L.append_review_atomic(bytes_dir, _review())   # バイト数の上限超過


# ===== RV是正（5巡目・正規化）: 表示・プロンプト注入へ出す前の共通サニタイズ =====

def test_sanitize_review_text_strips_control_chars_escapes_markdown_and_truncates():
    """改行・制御文字を取り除き、Markdown の表示崩れを起こす記号をエスケープし、1件あたり・
    全体それぞれの上限で切る（回答末尾の定型文・「続き」の注入文・調査の記録の Markdown 表示が
    共通で通す関数）。"""
    raw = "観点*です\n\t`コード`[リンク](x)|パイプ"
    cleaned = L.sanitize_review_text(raw)
    assert "\n" not in cleaned and "\t" not in cleaned
    assert cleaned == r"観点\*です\`コード\`\[リンク\](x)\|パイプ"
    assert L.sanitize_review_text("x" * (L.REVIEW_TEXT_PER_ITEM_MAX + 50)) == "x" * L.REVIEW_TEXT_PER_ITEM_MAX
    assert L.sanitize_review_text(None) == ""

    many = ["x" * L.REVIEW_TEXT_PER_ITEM_MAX for _ in range(20)]
    joined = L.sanitize_review_text_list(many)
    assert len(joined) == L.REVIEW_TEXT_TOTAL_MAX
    assert L.sanitize_review_text_list([]) == ""
    assert L.sanitize_review_text_list(["帳票", None, "  ", "画面"]) == "帳票、画面"
