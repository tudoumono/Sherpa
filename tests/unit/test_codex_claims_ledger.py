"""`claims` と調査台帳の突合（`sherpa.providers.codex.provider._claims_vs_ledger`）。

正典: `docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md` §3「claims は台帳からの投影」・
§6 ステップ4後半。`claims[].evidence_refs`（Codex 自身の自己申告・`"path:line"` 形式の文字列
配列）を、調査台帳の item が持つ `evidence`（`path`/`line`）と突き合わせ、1件も一致しない
confirmed 主張は裏付けの実在を確認できないとして推定（inferred）へ格下げする。

ここでは純関数（`_claims_vs_ledger`/`_parse_evidence_ref`/`_normalize_evidence_path`）だけを
対象にする——provider.py への統合（envelope・codex.log への反映）は `test_codex_ledger_gate.py`
の偽 codex 経由テストが対象。
"""
from __future__ import annotations

from sherpa import investigation_ledger as IL
from sherpa.providers.codex import provider as PV


def _snapshot(items: dict | None = None, *, has_manifest: bool = True,
             manifest_ids: list | None = None) -> IL.LedgerSnapshot:
    """テスト用の `LedgerSnapshot`。`items` は `{id: item}`（item は `_ledger_item` で作る）。
    `manifest_ids` を渡すと manifest の登録集合を `items` のキー一覧とは別に指定できる
    （未登録 item のテスト用・既定は `items` の全キーを登録済みとする）。`has_manifest=False` は
    `load_ledger` が manifest を読めなかった状態（ファイル無し・内容不正のどちらも同じ形——
    区別は `_claims_vs_ledger` の `manifest_file_exists` 引数で行う）。"""
    manifest = ({"question_kind": "list", "created_at": "2026-09-21T00:00:00Z",
                "items": manifest_ids if manifest_ids is not None else list((items or {}).keys())}
               if has_manifest else None)
    return IL.LedgerSnapshot(manifest=manifest, items=items or {}, invalid_ids=())


def _ledger_item(item_id: str, evidence: list) -> dict:
    """台帳 item（正規形8キー）。このテストでは `evidence` 以外はダミーでよい。"""
    return {"id": item_id, "kind": "row", "subject": "対象", "required_checks": [],
            "evidence": evidence, "status": "source_confirmed", "reason": "", "owner": "parent"}


def _claim(claim_id: str = "c1", status: str = "confirmed", evidence_refs: list | None = None,
          reason: str = "", reason_code: str = "") -> dict:
    """claim（`_parse_claim` が返す7キー形）。"""
    return {"id": claim_id, "status": status, "text": "主張の本文",
            "evidence_refs": evidence_refs if evidence_refs is not None else [],
            "reason": reason, "reason_code": reason_code, "evidence_kinds": ["source"]}


_EVIDENCE_A = [{"kind": "source", "path": "src/a.py", "line": 12}]


# ===== manifest ファイルの3状態（absent/invalid/valid） =====

def test_manifest_file_absent_leaves_claims_unchanged():
    """manifest.json ファイル自体が無い＝台帳を作らなかった依頼——この対応関係を適用せず
    `claims` を無変更で返す（正典§3）。"""
    claims = [_claim(evidence_refs=["src/a.py:12"])]
    snap = _snapshot(has_manifest=False)
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=False)
    assert out is claims   # 無変更＝同じリストをそのまま返す
    assert summary == {"ledger": False, "manifest_state": "absent"}


def test_manifest_file_exists_but_invalid_downgrades_all_confirmed():
    """[RV是正2巡目・中1] manifest.json ファイルは存在するが内容が規約に合わない
    （`created_at` 欠落・symlink 等・`investigation_ledger.load_ledger` が manifest を `None`
    にする）場合、「台帳を作らなかった依頼」と混同してはならない——壊れた台帳から final を
    生成しない、と同じ原則で「登録集合は空」として扱い、根拠の出所を示せない confirmed は
    （実在する evidence を挙げていても）全て inferred へ格下げする。"""
    claims = [_claim(evidence_refs=["src/a.py:12"])]   # 実在しそうな参照でも登録集合が空なら不一致
    snap = IL.LedgerSnapshot(manifest=None, items={}, invalid_ids=())
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "inferred"
    assert summary["ledger"] is True
    assert summary["manifest_state"] == "invalid"
    assert summary["downgraded"] == 1


# ===== confirmed の突合（manifest は正規形＝manifest_state="valid"） =====

def test_confirmed_all_refs_match_ledger_stays_confirmed():
    claims = [_claim(evidence_refs=["src/a.py:12"])]
    snap = _snapshot({"i1": _ledger_item("i1", _EVIDENCE_A)})
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "confirmed"
    assert out[0]["reason"] == ""
    assert summary == {"ledger": True, "checked": 1, "downgraded": 0, "unmatched_refs": 0,
                       "manifest_state": "valid"}


def test_confirmed_no_ref_matches_downgrades_to_inferred():
    claims = [_claim(evidence_refs=["src/other.py:5"])]
    snap = _snapshot({"i1": _ledger_item("i1", _EVIDENCE_A)})
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "inferred"
    assert out[0]["reason_code"] == ""
    assert "根拠が調査台帳に無い" in out[0]["reason"]
    assert summary == {"ledger": True, "checked": 1, "downgraded": 1, "unmatched_refs": 1,
                       "manifest_state": "valid"}


def test_confirmed_partial_ref_match_stays_confirmed():
    claims = [_claim(evidence_refs=["src/a.py:12", "src/other.py:5"])]
    snap = _snapshot({"i1": _ledger_item("i1", _EVIDENCE_A)})
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "confirmed"
    # 一致しなかった側の参照は unmatched_refs に数える（維持されても集計はする）。
    assert summary == {"ledger": True, "checked": 1, "downgraded": 0, "unmatched_refs": 1,
                       "manifest_state": "valid"}


def test_confirmed_ref_to_unregistered_item_evidence_is_not_trusted():
    """manifest に登録されていない item（`investigation_ledger.ledger_complete` の
    `unregistered_ids` と同じ——親が把握していない）の `evidence` は裏付けとして採用しない。
    item "b" は台帳ファイルとしては存在する（正規形として有効）が manifest には登録されて
    いない——その evidence だけを参照する confirmed は、台帳に無い扱いで格下げする。"""
    items = {
        "a": _ledger_item("a", _EVIDENCE_A),
        "b": _ledger_item("b", [{"kind": "source", "path": "src/b.py", "line": 12}]),
    }
    snap = _snapshot(items, manifest_ids=["a"])   # b は未登録
    claims = [_claim(evidence_refs=["src/b.py:12"])]
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "inferred"
    assert summary["downgraded"] == 1


def test_confirmed_ref_to_registered_item_evidence_still_matches():
    """上のテストと対にして固定する: 登録済み item の evidence は引き続き採用される。"""
    items = {
        "a": _ledger_item("a", _EVIDENCE_A),
        "b": _ledger_item("b", [{"kind": "source", "path": "src/b.py", "line": 12}]),
    }
    snap = _snapshot(items, manifest_ids=["a", "b"])   # 両方登録
    claims = [_claim(evidence_refs=["src/b.py:12"])]
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "confirmed"
    assert summary["downgraded"] == 0


def test_manifest_with_empty_items_trusts_no_evidence():
    """manifest はあるが登録集合が空（`items: []`）なら、どの item の evidence も採用しない——
    全 confirmed が格下げされる（manifest 無効時の安全側の挙動）。"""
    items = {"a": _ledger_item("a", _EVIDENCE_A)}
    snap = _snapshot(items, manifest_ids=[])
    claims = [_claim(evidence_refs=["src/a.py:12"])]
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "inferred"
    assert summary["downgraded"] == 1


def test_confirmed_empty_evidence_refs_downgrades():
    """`_parse_claim`（provider.py）は裏付けゼロの confirmed を通常は拒否するが、この純関数
    自体は防御的に「refs 空＝一致ゼロ」として扱う。"""
    claims = [_claim(evidence_refs=[])]
    snap = _snapshot({"i1": _ledger_item("i1", _EVIDENCE_A)})
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "inferred"
    assert summary["downgraded"] == 1
    assert summary["unmatched_refs"] == 0   # 参照が1件も無いので延べ件数は0


def test_inferred_and_unknown_claims_are_untouched():
    claims = [
        _claim("c1", status="inferred", evidence_refs=[], reason="推定の理由"),
        _claim("c2", status="unknown", evidence_refs=[], reason_code="unexplored"),
    ]
    snap = _snapshot({"i1": _ledger_item("i1", _EVIDENCE_A)})
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out == claims
    assert summary == {"ledger": True, "checked": 0, "downgraded": 0, "unmatched_refs": 0,
                       "manifest_state": "valid"}


def test_reason_prefix_preserves_original_reason_text():
    claims = [_claim(evidence_refs=["src/other.py:5"], reason="既存の理由")]
    snap = _snapshot({"i1": _ledger_item("i1", _EVIDENCE_A)})
    out, _ = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["reason"].startswith("根拠が調査台帳に無い")
    assert "既存の理由" in out[0]["reason"]


# ===== path/line の正規化 =====

def test_leading_dot_slash_path_matches_bare_path():
    claims = [_claim(evidence_refs=["./src/a.py:12"])]
    snap = _snapshot({"i1": _ledger_item("i1", _EVIDENCE_A)})
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "confirmed"
    assert summary["downgraded"] == 0


def test_line_number_as_string_matches_int_line():
    """ref 文字列側の行番号は常に文字列表現（`"path:12"`）——台帳側は int（`12`）で保持される。
    `_parse_evidence_ref` が int へ変換してから突き合わせるため一致する。"""
    snap = _snapshot({"i1": _ledger_item("i1", [{"kind": "source", "path": "src/a.py", "line": 12}])})
    claims = [_claim(evidence_refs=["src/a.py:12"])]
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "confirmed"
    assert summary["downgraded"] == 0


def test_malformed_ref_without_line_number_does_not_match():
    claims = [_claim(evidence_refs=["src/a.py"])]   # コロン無し＝行番号を持たない
    snap = _snapshot({"i1": _ledger_item("i1", _EVIDENCE_A)})
    out, summary = PV._claims_vs_ledger(claims, snap, manifest_file_exists=True)
    assert out[0]["status"] == "inferred"
    assert summary["unmatched_refs"] == 1


# ===== 補助関数の直接固定 =====

def test_parse_evidence_ref_splits_on_last_colon():
    assert PV._parse_evidence_ref("src/a.py:12") == ("src/a.py", 12)
    assert PV._parse_evidence_ref("./src/a.py:12") == ("src/a.py", 12)
    assert PV._parse_evidence_ref("no-colon-here") is None
    assert PV._parse_evidence_ref("src/a.py:not-a-number") is None
    assert PV._parse_evidence_ref("") is None
    assert PV._parse_evidence_ref(None) is None


def test_normalize_evidence_path_strips_leading_dot_slash_and_backslashes():
    assert PV._normalize_evidence_path("./src/a.py") == "src/a.py"
    assert PV._normalize_evidence_path("src/a.py") == "src/a.py"
    assert PV._normalize_evidence_path(".\\src\\a.py") == "src/a.py"
