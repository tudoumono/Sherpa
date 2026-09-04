"""`doc_ledger.documents_for` の直近 run 突き合わせ（unreadable 上書き）の単体テスト。

既定 accepts の言語（cobol/copybook/jcl）は `resolve_lazy` が内容を読まない短絡（§7 裁定10）のため、
`corpus_docs.classify_document`（列挙・scan_report・文書一覧が共有する判定）だけでは実際の読み取り
失敗を検知できない——実際にファイルを開く `world_graph.build_world` Pass1 の blocked flag を直近
ingest run から突き合わせて上書きする経路を、実際の OSError から固定する（分類短絡は維持）。

直近 run の確認自体ができない場合（DB 例外・打切り期限超過）は、「blocked 無し」と混同せず
`state="unknown"` へ倒す（黙って `ready`/`使えます` にしない）ことも併せて固定する。
"""
from __future__ import annotations

import time
from pathlib import Path

from sherpa import corpus_docs, doc_ledger, store, worlds
from sherpa.ingest import world_graph


def _world(monkeypatch, tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    return wd, der


def test_documents_for_marks_unreadable_code_file_from_last_run_blocked_flags(monkeypatch, tmp_path):
    """実経路: 受理済み（拡張子一致）コード文書の実読込失敗（`world_graph.build_world` の
    OSError→blocked）が直近 run に記録されていれば、文書一覧（`doc_ledger.documents_for`）は
    fast-path の「使えます」ではなく `state="unreadable"`（理由付き）で表示する。
    """
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "BADPROG.cbl").write_text("       PROGRAM-ID. BADPROG.\n", encoding="utf-8")

    # 短絡そのものの確認: accepts() 内容を読まないため列挙は「使えます」に見える（既知の限界）。
    docs_before = corpus_docs.world_documents("w")
    assert docs_before[0]["state"] == "ready" and docs_before[0]["doctype"] == "cobol"

    # 実経路: build_world の実読込で OSError → blocked flag（Pass1 の既存挙動そのもの）。
    real_read_text = Path.read_text

    def _boom(self, *a, **kw):
        if self.name == "BADPROG.cbl":               # 対象ファイル限定（他の読み取りは通常どおり）
            raise OSError("simulated read failure")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _boom)
    _nodes, _edges, flags = world_graph.build_world(wd, "w")
    blocked = [f for f in flags if f.get("action") == "blocked"]
    assert blocked == [{"doc": "BADPROG.cbl", "reason": "unreadable_code_file", "action": "blocked"}]

    # 直近 ingest run にこの flags が記録されている状態を模す（DB 実書込はしない・store だけ差替え）。
    # RV2是正#a3: `last_run_flags` は `list_ingest_runs`（`source_doc_ids` 込みの重い SELECT）
    # ではなく `get_latest_run_summary`（狭い SELECT・常に1行 or None）を使う。
    monkeypatch.setattr(store, "get_latest_run_summary",
                        lambda world, **kw: {"extraction_snapshot": {"flags": flags}})

    docs = doc_ledger.documents_for("w")
    row = next(d for d in docs if d["name"] == "BADPROG.cbl")
    assert row["state"] == "unreadable"
    assert row["reason"] == "unreadable_code_file"
    assert row["doctype"] == "cobol"                  # 何のファイルかは分かる状態を保つ（null に倒さない）


def test_documents_for_unaffected_when_no_blocked_docs(monkeypatch, tmp_path):
    """直近 run に doc 付き blocked flag が無ければ、一覧は走査結果のまま（余計な上書きをしない）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "OKPROG.cbl").write_text("       PROGRAM-ID. OKPROG.\n", encoding="utf-8")
    monkeypatch.setattr(store, "get_latest_run_summary", lambda world, **kw: None)   # run 自体が無い

    docs = doc_ledger.documents_for("w")
    assert docs[0]["state"] == "ready"


def test_documents_for_ignores_blocked_flags_without_doc(monkeypatch, tmp_path):
    """`doc` が無い blocked flag（例: `world_unresolved`）は突き合わせ対象にしない（一致しようが無い）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "OKPROG.cbl").write_text("       PROGRAM-ID. OKPROG.\n", encoding="utf-8")
    monkeypatch.setattr(store, "get_latest_run_summary", lambda world, **kw: {"extraction_snapshot": {"flags": [
        {"doc": None, "reason": "world_unresolved", "action": "blocked"}]}})

    docs = doc_ledger.documents_for("w")
    assert docs[0]["state"] == "ready"


def test_documents_for_falls_back_to_unknown_when_last_run_check_raises(monkeypatch, tmp_path):
    """直近 run の DB 参照が例外で失敗しても `ready`/`使えます` に黙って倒れない——分類短絡の
    対象（`branch=="source"`・`state=="ready"`）を `state="unknown"` へ倒す（fail-closed の表示側）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "BADPROG.cbl").write_text("       PROGRAM-ID. BADPROG.\n", encoding="utf-8")

    def _boom(world, **kw):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(store, "get_latest_run_summary", _boom)

    docs = doc_ledger.documents_for("w")
    row = next(d for d in docs if d["name"] == "BADPROG.cbl")
    assert row["state"] == "unknown"
    assert row["label"] == "状態を確認できませんでした"
    assert row["doctype"] == "cobol"                  # 何のファイルかは分かる状態を保つ


def test_last_run_flags_uses_narrow_summary_not_heavy_list_ingest_runs(monkeypatch, tmp_path):
    """RV2是正#a3: `last_run_flags` は `get_latest_run_summary`（`source_doc_ids` を持たない
    狭い SELECT）を使い、`list_ingest_runs`（world 全文書名の重い JSONB 配列込み）を一切呼ばない
    ——`doc_ledger.public_documents_page` の `world_lock_shared`（共有ロック）保持中に呼ばれても
    O(N)（文書総数比例）の転送・deserialize を持ち込まない。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "OKPROG.cbl").write_text("       PROGRAM-ID. OKPROG.\n", encoding="utf-8")

    def _must_not_call(world, **kw):
        raise AssertionError("list_ingest_runs は呼ばれてはいけない（get_latest_run_summary を使う）")

    calls = {"n": 0}

    def _summary(world, **kw):
        calls["n"] += 1
        return {"extraction_snapshot": {"flags": []}}

    monkeypatch.setattr(store, "list_ingest_runs", _must_not_call)
    monkeypatch.setattr(store, "get_latest_run_summary", _summary)

    assert corpus_docs.last_run_flags("w") == []
    assert calls["n"] == 1


def test_last_run_blocked_docs_returns_none_without_db_call_when_deadline_already_past(monkeypatch):
    """`deadline` が呼び出し時点で既に過ぎていれば、DB へは触れずに `None`（確認できなかった）を
    返す（list_docs ツール打切り契約・`agentic_search.run_tool` 経由の呼び出しを模す）。"""
    def _must_not_call(world, **kw):
        raise AssertionError("get_latest_run_summary は呼ばれてはいけない（期限切れ後）")

    monkeypatch.setattr(store, "get_latest_run_summary", _must_not_call)

    assert corpus_docs.last_run_blocked_docs("w", deadline=time.monotonic() - 1) is None


def test_documents_for_passes_remaining_time_as_db_timeout(monkeypatch, tmp_path):
    """`deadline` 指定時は残り時間を `connect_timeout`/`statement_timeout_ms` として
    `store.get_latest_run_summary`（RV2是正#a3・以前は `store.list_ingest_runs`）へ渡す
    （list_docs ツールの打切り契約を DB 参照にも及ぼす）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "OKPROG.cbl").write_text("       PROGRAM-ID. OKPROG.\n", encoding="utf-8")

    seen = {}

    def _capture(world, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(store, "get_latest_run_summary", _capture)

    doc_ledger.documents_for("w", deadline=time.monotonic() + 30)

    assert seen.get("connect_timeout") is not None and 0 < seen["connect_timeout"] <= 30
    assert seen.get("statement_timeout_ms") is not None and 0 < seen["statement_timeout_ms"] <= 30000


def test_documents_for_blocked_clears_after_newer_successful_run(monkeypatch, tmp_path):
    """古い失敗 run が特定 doc を blocked にしていても、直近（最新）run が成功していれば
    その doc の unreadable 表示は消える。`get_latest_run_summary` は常に「最新の1件のみ」
    （`ORDER BY id DESC LIMIT 1`・`limit` パラメータ自体を持たない）ため、`list_ingest_runs` の
    ときにあった「呼び出し元が limit を誤って大きくして複数 run を合成してしまう」回帰の余地は
    構造的に無くなった——ここでは最新 run（成功）だけを返すモックで、古い run の blocked flag が
    混入しないことを直接固定する。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "OKPROG.cbl").write_text("       PROGRAM-ID. OKPROG.\n", encoding="utf-8")

    new_ok_run = {"id": 2, "extraction_snapshot": {"flags": []}}   # 最新 run のみ（古い blocked run は返らない）
    monkeypatch.setattr(store, "get_latest_run_summary", lambda world, **kw: new_ok_run)

    docs = doc_ledger.documents_for("w")
    assert docs[0]["state"] == "ready"


def test_preview_documents_passes_through_analyzer_distinct_from_doctype(monkeypatch, tmp_path):
    """`doc_ledger.preview_documents` は `analyzer`（`Analyzer.name`）を一覧応答に含める——
    `doctype`（種別表示用）とは独立した値であることを name≠doctype のダミー言語で固定する
    （§7 裁定2の受入条件＝取り込み画面で担当アナライザの来歴を参照できるようにする）。"""
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    class _DummyLangAnalyzer(Analyzer):
        name = "dummylang"
        extensions = frozenset({".dummy"})
        doctype = "ダミー言語"

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "thing.dummy").write_text("何か新言語のソース", encoding="utf-8")
    monkeypatch.setattr(registry, "_ANALYZERS", (_DummyLangAnalyzer(),))
    monkeypatch.setattr(store, "get_latest_run_summary", lambda world, **kw: None)

    docs = doc_ledger.preview_documents("w")
    assert docs[0]["doctype"] == "ダミー言語"
    assert docs[0]["analyzer"] == "dummylang"          # doctype の別名ではない
