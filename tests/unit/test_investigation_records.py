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


def _review(verdict="mostly_answered", added_items=None, extra_perspectives=None):
    """COD-18 ⑤: `investigation_ledger.validate_review_entry()` を満たす最小の見直し。"""
    return {"purpose": "目的の言い直し", "perspectives": ["画面"], "summary": "分かったこと",
            "added_items": added_items or [], "removed_items": [], "verdict": verdict,
            "extra_perspectives": extra_perspectives or [], "ts": 1.0, "terminal_count": 1}


def test_trim_to_budget_truncates_items_from_the_back_and_sets_truncated():
    """合算サイズが上限を超えたら id 降順（後ろ）から削り、`truncated=True` になる。超えなければ
    何も削らない。`reviews`（COD-18 ⑤）は省略可引数——既存の4引数呼び出しも動く。"""
    # 2桁ゼロ埋め id（i00..i49）＝文字列の昇順と数値の昇順が一致する（`sorted(..., reverse=True)` の
    # 削除順が直感どおりになるようにテスト側で揃える）。
    ids = [f"i{i:02d}" for i in range(50)]
    manifest = {"question_kind": "qa", "created_at": "2026-10-01T00:00:00Z", "items": ids}
    big_subject = "x" * 25000   # 合計で MAX_RECORD_BYTES（1 MiB）を確実に超える大きさ
    items = {i: _item(i, subject=big_subject) for i in ids}
    coverage = {i: ["hit"] for i in ids}

    _m, trimmed_items, trimmed_coverage, trimmed_reviews, truncated = store_investigation.trim_to_budget(
        manifest, items, coverage)
    assert truncated is True
    assert len(trimmed_items) < 50
    assert trimmed_reviews == []
    # 後ろ（id 降順）から削る＝先頭側（i00 等）は残り、末尾側（i49 等）から落ちる。
    assert "i00" in trimmed_items
    assert "i49" not in trimmed_items

    # 小さい入力は変更なし（reviews 省略時の既定は空配列）。
    small_items = {"i0": _item("i0")}
    _m2, out_items, out_coverage, out_reviews, out_truncated = store_investigation.trim_to_budget(
        manifest, small_items, {"i0": ["hit"]})
    assert out_truncated is False
    assert out_items == small_items
    assert out_reviews == []

    # reviews を渡しても超えなければそのまま保持される。
    _m3, _i3, _c3, out_reviews3, out_truncated3 = store_investigation.trim_to_budget(
        manifest, small_items, {"i0": ["hit"]}, [_review()])
    assert out_truncated3 is False
    assert out_reviews3 == [_review()]


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


def test_render_markdown_includes_mid_review_section():
    """COD-18 ⑤: Markdown に「途中の見直し」節が出て、目的・観点・分かったこと・足した/外した
    項目と理由・判断・追加で調べられる観点を含む（見直しが無ければ「(なし)」）。"""
    record = {
        "complete": True, "truncated": False,
        "manifest": {"question_kind": "qa", "created_at": "2026-10-01T00:00:00Z", "items": ["i1"]},
        "items": {"i1": _item("i1")},
        "coverage": {"i1": ["hit"]},
        "reviews": [_review(verdict="mostly_answered", added_items=[{"id": "i2", "reason": "発見"}],
                            extra_perspectives=["帳票"])],
    }
    md = investigation_record_render.render_markdown(record)
    assert "## 途中の見直し" in md
    section = md.split("## 途中の見直し")[1].split("## 確認できなかった項目")[0]
    assert "目的の言い直し" in section
    assert "画面" in section
    assert "分かったこと" in section
    assert "i2: 発見" in section
    assert "おおむね出た（mostly_answered）" in section
    assert "帳票" in section

    no_review_record = {**record, "reviews": []}
    md_no_review = investigation_record_render.render_markdown(no_review_record)
    assert "## 途中の見直し\n\n(なし)" in md_no_review


def _try_init():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def test_save_and_get_investigation_record_round_trip_and_cascades_on_conversation_delete():
    """保存→取得の往復（COD-18 §1・⑤の `reviews` 列も含む）と、会話の物理削除（`messages` FK
    CASCADE）で記録も消えること（正典§7「削除の伝播」と同じ `ON DELETE CASCADE` の実動作確認）。"""
    _try_init()
    conv = store.create_conversation(user_id="admin", world="v1", title="investigation record test")
    msg = store.add_message(conv["id"], "assistant", "headline", answer={"headline": "headline"})

    manifest = {"question_kind": "qa", "created_at": "2026-10-01T00:00:00Z", "items": ["i1"]}
    items = {"i1": _item("i1")}
    coverage = {"i1": ["hit"]}
    reviews = [_review()]
    saved = store_investigation.save_investigation_record(
        msg["id"], conv["id"], complete=True, manifest=manifest, items=items, coverage=coverage,
        reviews=reviews, coverage_detail={"i1": [{"tool": "read_doc", "outcome": "hit", "doc": "a.md"}]},
        extras={"dropped": {"items": 2}})
    assert saved["complete"] is True and saved["truncated"] is True     # 先に縮めた分も `truncated` に出る
    assert saved["detail"]["dropped"] == {"items": 2} and saved["detail"]["coverage"]["i1"][0]["doc"] == "a.md"
    assert saved["manifest"] == manifest
    assert saved["items"] == items
    assert saved["reviews"] == reviews

    fetched = store_investigation.get_investigation_record(msg["id"])
    assert fetched["conversation_id"] == conv["id"]
    assert fetched["items"]["i1"]["status"] == "source_confirmed"
    assert fetched["reviews"] == reviews

    # 保存より後に分かった欠落は、回答に注記を足して残す（本文は変えない・調査の記録の導線は外れる）。
    store.append_answer_notice(msg["id"], "investigation_record_failed", "調査の記録を保存できませんでした。",
                               unrecorded=True)
    with store._connect() as c:
        row = c.execute("SELECT content, answer FROM messages WHERE id=%s", (msg["id"],)).fetchone()
    assert row["answer"]["notices"] == [{"kind": "investigation_record_failed", "text": "調査の記録を保存できませんでした。"}]
    assert row["answer"]["body"] == "headline" and row["content"].endswith("headline")
    with store._connect() as c:   # 後から足した注記は集計表の注記の種類にも反映される
        tm = c.execute("SELECT notice_kinds FROM turn_metrics WHERE message_id=%s", (msg["id"],)).fetchone()
    assert tm["notice_kinds"] == ["investigation_record_failed"]

    assert store.delete_conversation(conv["id"], "admin") is True
    assert store_investigation.get_investigation_record(msg["id"]) is None


def test_save_investigation_record_defaults_reviews_to_empty_list_when_omitted():
    """`reviews` 省略時（台帳ゲートが走らない構成・見直し自体が無いターン）は空配列で保存される
    ——既存呼び出し（`reviews` を渡さない構成）が壊れないことの確認。"""
    _try_init()
    conv = store.create_conversation(user_id="admin", world="v1", title="investigation record test 2")
    msg = store.add_message(conv["id"], "assistant", "headline", answer={"headline": "headline"})
    saved = store_investigation.save_investigation_record(
        msg["id"], conv["id"], complete=True,
        manifest={"question_kind": "qa", "created_at": "2026-10-01T00:00:00Z", "items": ["i1"]},
        items={"i1": _item("i1")}, coverage={"i1": ["hit"]})
    assert saved["reviews"] == []
    assert store.delete_conversation(conv["id"], "admin") is True


def test_record_trim_reports_what_was_dropped_and_reviews_load_reports_omissions(tmp_path):
    """調査記録の 1MiB の削減は、何を何件落としたかを返す。見直しの読み取りの上限・不正行も内訳で分かる。"""
    ids = [f"i{i:02d}" for i in range(50)]
    items = {i: _item(i, subject="x" * 25000) for i in ids}
    detail = {i: [{"tool": "read_doc", "outcome": "hit", "doc": "a.md"}] for i in ids}
    *_rest, dropped = store_investigation.trim_record(
        {"question_kind": "qa", "created_at": "t", "items": ids}, items, {i: ["hit"] for i in ids}, [_review()], detail)
    assert dropped["items"] > 0 and "reviews" not in dropped
    text = investigation_record_render.describe_dropped(dropped)
    assert f"調べた項目 {dropped['items']} 件" in text and "保存していません" in text
    assert investigation_record_render.describe_dropped({}) == ""
    # 見直しは件数の上限・不正行を数えて返す。
    from sherpa import investigation_ledger as L
    for _ in range(L.REVIEWS_MAX_COUNT):
        L.append_review_atomic(tmp_path, {**_review(), "ts": 1.0})
    with (tmp_path / "reviews.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"broken": true}\n' + __import__("json").dumps(_review()) + "\n")
    reviews, report = L.load_reviews_report(tmp_path)
    assert len(reviews) == L.REVIEWS_MAX_COUNT
    assert report == {"invalid": 1, "over_count": 1, "over_bytes": False}
