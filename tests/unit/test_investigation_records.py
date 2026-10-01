"""調査台帳の記録（`investigation_records`・COD-18「調査台帳を回答ごとに残す」提案書）。

純関数（`trim_to_budget`・`investigation_record_render.render_markdown`）は PG 不要。
`save_investigation_record`/`get_investigation_record` の実 DB 往復とカスケード削除は要 Postgres
（DB down は skip・既存 tests/unit の流儀）。
"""
from __future__ import annotations

import pytest

from sherpa import investigation_record_render
from sherpa import store
from sherpa.store import investigation_records as store_investigation

pytestmark = pytest.mark.unit


def _item(item_id, *, status="source_confirmed", subject="対象", reason="",
         evidence=None, checks=("source",)):
    return {"id": item_id, "kind": "qa", "subject": subject,
           "required_checks": list(checks),
           "evidence": evidence if evidence is not None else [
               {"kind": "source", "path": f"a/{item_id}.md", "line": 1}],
           "status": status, "reason": reason, "owner": "main"}


def test_trim_to_budget_truncates_items_from_the_back_and_sets_truncated():
    """合算サイズが上限を超えたら id 降順（後ろ）から削り、`truncated=True` になる。超えなければ
    何も削らない。"""
    # 2桁ゼロ埋め id（i00..i49）＝文字列の昇順と数値の昇順が一致する（`sorted(..., reverse=True)` の
    # 削除順が直感どおりになるようにテスト側で揃える）。
    ids = [f"i{i:02d}" for i in range(50)]
    manifest = {"question_kind": "qa", "created_at": "2026-10-01T00:00:00Z", "items": ids}
    big_subject = "x" * 25000   # 合計で MAX_RECORD_BYTES（1 MiB）を確実に超える大きさ
    items = {i: _item(i, subject=big_subject) for i in ids}
    coverage = {i: ["hit"] for i in ids}

    _m, trimmed_items, trimmed_coverage, truncated = store_investigation.trim_to_budget(
        manifest, items, coverage)
    assert truncated is True
    assert len(trimmed_items) < 50
    # 後ろ（id 降順）から削る＝先頭側（i00 等）は残り、末尾側（i49 等）から落ちる。
    assert "i00" in trimmed_items
    assert "i49" not in trimmed_items

    # 小さい入力は変更なし。
    small_items = {"i0": _item("i0")}
    _m2, out_items, out_coverage, out_truncated = store_investigation.trim_to_budget(
        manifest, small_items, {"i0": ["hit"]})
    assert out_truncated is False
    assert out_items == small_items


def test_render_markdown_table_and_unconfirmed_section():
    """md は質問の種類・完了・項目の表・確認できなかった項目の節を決定的に含む（COD-18 §2）。
    `|`/改行を含む subject・reason はテーブル破壊を起こさないようエスケープする。"""
    record = {
        "complete": False, "truncated": True,
        "manifest": {"question_kind": "troubleshoot", "created_at": "2026-10-01T00:00:00Z",
                     "items": ["i1", "i2"]},
        "items": {
            "i1": _item("i1", status="source_confirmed", subject="対象1"),
            "i2": _item("i2", status="not_found_in_scope", subject="対象|2\n続き",
                        reason="範囲内に見つからず", evidence=[], checks=["source"]),
        },
        "coverage": {"i1": ["hit"], "i2": ["no_hits"]},
    }
    md = investigation_record_render.render_markdown(record)
    assert md.startswith("# 調査の記録")
    assert "質問の種類: トラブルの原因（troubleshoot）" in md   # 人が読む表示＋元の値
    assert "完了: いいえ" in md
    assert "一部を切り詰めて保存しています" in md
    assert "source_confirmed" in md and "a/i1.md:1" in md
    assert "対象\\|2 続き" in md   # | と改行がエスケープされる
    assert "## 確認できなかった項目" in md
    assert "範囲内に見つからない（not_found_in_scope）: 範囲内に見つからず" in md
    assert "対象1" not in md.split("## 確認できなかった項目")[1].split("## 検索の結果")[0]   # 確認済みの i1 は節に出ない


def _try_init():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def test_save_and_get_investigation_record_round_trip_and_cascades_on_conversation_delete():
    """保存→取得の往復（COD-18 §1）と、会話の物理削除（`messages` FK CASCADE）で記録も消えること
    （正典§7「削除の伝播」と同じ `ON DELETE CASCADE` の実動作確認）。"""
    _try_init()
    conv = store.create_conversation(user_id="admin", world="v1", title="investigation record test")
    msg = store.add_message(conv["id"], "assistant", "headline", answer={"headline": "headline"})

    manifest = {"question_kind": "qa", "created_at": "2026-10-01T00:00:00Z", "items": ["i1"]}
    items = {"i1": _item("i1")}
    coverage = {"i1": ["hit"]}
    saved = store_investigation.save_investigation_record(
        msg["id"], conv["id"], complete=True, manifest=manifest, items=items, coverage=coverage)
    assert saved["complete"] is True and saved["truncated"] is False
    assert saved["manifest"] == manifest
    assert saved["items"] == items

    fetched = store_investigation.get_investigation_record(msg["id"])
    assert fetched["conversation_id"] == conv["id"]
    assert fetched["items"]["i1"]["status"] == "source_confirmed"

    assert store.delete_conversation(conv["id"], "admin") is True
    assert store_investigation.get_investigation_record(msg["id"]) is None
