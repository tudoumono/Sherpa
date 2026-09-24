"""`corpus_docs.scan_report(world, expected_rels=...)` の世代混在ガード（波3 3巡目 RV）。

取り込み冒頭の manifest の rel 集合を渡すと、本走査（`si.safe_files`）が実際に見た rel 集合と
一致した場合だけ `document_count` を実値で返し、不一致（走査中にファイルが増減した世代混在）なら
`None` にして更新を保留する。`expected_rels` 省略時は従来どおり比較せず実値を返す。
"""
from __future__ import annotations

from sherpa import corpus_docs, worlds


def _world(monkeypatch, tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    return wd, der


def test_expected_rels_matching_actual_scan_returns_document_count(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "a.md").write_text("hello", encoding="utf-8")

    rep = corpus_docs.scan_report("w", expected_rels=frozenset({"a.md"}))

    assert rep["document_count"] == 1


def test_expected_rels_mismatch_after_file_added_mid_scan_yields_none(monkeypatch, tmp_path):
    """冒頭 manifest `{a.md}` の後に `b.md` が追加された（取り込み中の増減）状態で本走査すると、
    実走査集合 `{a.md, b.md}` が冒頭集合と食い違うため `document_count` は `None`（更新保留）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "a.md").write_text("hello", encoding="utf-8")
    (wd / "b.md").write_text("world", encoding="utf-8")   # 冒頭 manifest 確定後に増えたファイルを模す

    rep = corpus_docs.scan_report("w", expected_rels=frozenset({"a.md"}))

    assert rep["document_count"] is None


def test_expected_rels_mismatch_after_file_removed_mid_scan_yields_none(monkeypatch, tmp_path):
    """冒頭 manifest `{a.md, b.md}` の後に `b.md` が削除された状態で本走査すると同様に `None`。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "a.md").write_text("hello", encoding="utf-8")

    rep = corpus_docs.scan_report("w", expected_rels=frozenset({"a.md", "b.md"}))

    assert rep["document_count"] is None


def test_expected_rels_omitted_keeps_legacy_behavior(monkeypatch, tmp_path):
    """`expected_rels` を渡さない既存の呼び出し元（recount エンドポイント・バックフィル等）は
    比較を行わず、従来どおり実値をそのまま返す。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "a.md").write_text("hello", encoding="utf-8")

    rep = corpus_docs.scan_report("w")

    assert rep["document_count"] == 1
