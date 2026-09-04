"""文書一覧応答（GET /ingest/preview）の「どう読み取ったか」要約フィールド（S2・表示のみ）。

来歴サイドカー `{md}.meta.json`（fixtures として tmp world に用意）を読み、Office 枝の文書に `provenance`
（method/confidence/legacy_backend?/has_conflicts?）を付ける。物理パス（md_path）は応答に出さない。
ソース枝（派生MD 無し）は付けない＝後方互換。認可（admin 必須）込みで TestClient 経由で検証する。
"""
from __future__ import annotations

import json
import pathlib
import tempfile

from fastapi.testclient import TestClient

from sherpa.api import app

W = "preview_prov_tmp"


def _docs_by_name(body: dict) -> dict:
    return {d["name"]: d for d in body["documents"]}


def test_preview_documents_expose_provenance_and_hide_md_path(auth_disabled):
    from sherpa import worlds
    old_world_dir, old_derived = worlds.world_dir, worlds.derived_md_dir
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        root, der = pathlib.Path(td), pathlib.Path(dd)
        (root / "設計").mkdir()
        (root / "src").mkdir()
        (root / "設計" / "report.docx").write_bytes(b"PK\x03\x04 office binary")  # Office 原本（バイナリ）
        (root / "src" / "TAXCALC.cbl").write_text("       PROGRAM-ID. TAXCALC.\n", encoding="utf-8")
        # build_derived が書くのと同じ派生MD＋来歴サイドカー（旧形式変換＋照合差分のケース）。
        (der / "設計").mkdir(parents=True)
        (der / "設計" / "report.docx.md").write_text("# 見出し\n本文", encoding="utf-8")
        (der / "設計" / "report.docx.md.meta.json").write_text(json.dumps({
            "arm": "ooxml", "method": "ooxml", "confidence": 1.0,
            "notes": ["legacy_backend=libreoffice", "soffice=7.5"],
            "merge": "deterministic-v1",
            "conflicts": [{"type": "numeric_only_in_secondary", "value": "9"}],
        }, ensure_ascii=False), encoding="utf-8")

        worlds.world_dir = lambda w: root if w == W else old_world_dir(w)
        worlds.derived_md_dir = lambda w: der if w == W else old_derived(w)
        try:
            c = TestClient(app, raise_server_exceptions=False)
            r = c.get("/ingest/preview", params={"world": W})
            assert r.status_code == 200, r.text
            docs = _docs_by_name(r.json())

            office = docs["設計/report.docx"]
            assert office["provenance"] == {
                "method": "ooxml", "confidence": 1.0,
                "legacy_backend": "libreoffice", "has_conflicts": True}
            assert "md_path" not in office                          # 物理パスは出さない

            # ソース枝は派生MD が無い＝provenance を付けない（後方互換）。
            assert "provenance" not in docs["src/TAXCALC.cbl"]
        finally:
            worlds.world_dir, worlds.derived_md_dir = old_world_dir, old_derived


def test_preview_documents_without_sidecar_omit_provenance(auth_disabled):
    """派生MD はあるが meta.json が無い（旧取り込み等）＝provenance を付けない（後方互換）。"""
    from sherpa import worlds
    old_world_dir, old_derived = worlds.world_dir, worlds.derived_md_dir
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        root, der = pathlib.Path(td), pathlib.Path(dd)
        (root / "plain.docx").write_bytes(b"PK\x03\x04")
        (der / "plain.docx.md").write_text("本文", encoding="utf-8")     # サイドカー無し
        worlds.world_dir = lambda w: root if w == W else old_world_dir(w)
        worlds.derived_md_dir = lambda w: der if w == W else old_derived(w)
        try:
            c = TestClient(app, raise_server_exceptions=False)
            r = c.get("/ingest/preview", params={"world": W})
            assert r.status_code == 200, r.text
            office = _docs_by_name(r.json())["plain.docx"]
            assert "provenance" not in office and "md_path" not in office
        finally:
            worlds.world_dir, worlds.derived_md_dir = old_world_dir, old_derived


def test_ingest_preview_failure_returns_503_with_graph_message(auth_disabled, monkeypatch):
    """RV1是正#6: `build_preview`（world 世代プローブ＝キャッシュの鍵読みを含む）が例外を出したら、
    握り潰して生の 500 にせず、`/graph` と同じ固定文言・ログ付きで 503 にする（silent degradation
    なしの家風・`sherpa/routers/graph.py::graph_get` と同型）。"""
    from sherpa.routers import graph as graph_router
    from sherpa.routers import worlds as worlds_router

    def _boom(world):
        raise RuntimeError("db down")

    monkeypatch.setattr(worlds_router, "build_preview", _boom)
    c = TestClient(app)
    r = c.get("/ingest/preview", params={"world": W})
    assert r.status_code == 503
    assert r.json()["detail"] == graph_router._GRAPH_UNAVAILABLE_MESSAGE
