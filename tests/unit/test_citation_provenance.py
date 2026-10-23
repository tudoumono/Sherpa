"""出典・citation に添える世代（トップフォルダ）と原本の更新時刻の回帰テスト。
`documents.modified_at_map`（資料ごとの `stat` だけ・中身は読まない）・`chat_service._sources`／`_answer_qa`・共有（`_safe_share_answer`）。
資料フォルダ全体の取り込み時刻を全出典に使わず、資料ごとの値になることを固定する。DB・ES 不要。
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
from sherpa import chat_service as CS  # noqa: E402
from sherpa import documents, worlds  # noqa: E402
from sherpa.ingest import importance  # noqa: E402
from sherpa.store.shares import _safe_share_answer  # noqa: E402

_W = "provenancetest"
_T_OLD = 1_751_328_000  # 2025-07-01T00:00:00Z
_T_NEW = 1_782_864_000  # 2026-07-01T00:00:00Z


def _iso(t: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "root"
    arch = tmp_path / "arch"
    root.mkdir()
    arch.mkdir()
    for rel, t in (("4期/設計/仕様書.md", _T_OLD), ("5期/設計/仕様書.md", _T_NEW), ("直下.md", _T_NEW),
                   (importance.CONTROL_FILENAME, _T_NEW)):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        os.utime(p, (t, t))
    (arch / "zip1").mkdir()
    (arch / "zip1" / "inner.md").write_text("x", encoding="utf-8")
    os.utime(arch / "zip1" / "inner.md", (_T_OLD, _T_OLD))
    monkeypatch.setattr(worlds, "world_dir", lambda w: root if w == _W else None)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: arch)
    monkeypatch.setattr(CS.store, "get_world_status_row", lambda w: None)
    monkeypatch.setattr(CS.importance, "resolve_many", lambda w, rels, root=None, sig=None: {})
    return root


def test_modified_at_map_is_per_document_not_per_world(tree):
    out = documents.modified_at_map(_W, ["4期/設計/仕様書.md", "5期/設計/仕様書.md", "直下.md"])
    assert out == {"4期/設計/仕様書.md": _iso(_T_OLD), "5期/設計/仕様書.md": _iso(_T_NEW), "直下.md": _iso(_T_NEW)}


def test_modified_at_map_skips_unresolvable_rels_and_control_file(tree):
    out = documents.modified_at_map(_W, ["missing.md", "../outside.md", "", None, importance.CONTROL_FILENAME,
                                          "4期/設計/仕様書.md"])
    assert out == {"4期/設計/仕様書.md": _iso(_T_OLD)}


def test_modified_at_map_omits_archive_member_and_unregistered_world(tree):
    assert documents.modified_at_map(_W, ["zip1/inner.md"]) == {}  # 展開先の写しの時刻を原本の更新時刻として出さない
    assert documents.modified_at_map("nosuchworld", ["4期/設計/仕様書.md"]) == {}


def test_sources_carry_generation_and_own_modified_at(tree):
    out = {s["doc_id"]: s for s in CS._sources(["4期/設計/仕様書.md", "5期/設計/仕様書.md", "直下.md"], _W)}
    assert out["4期/設計/仕様書.md"]["generation"] == "4期"
    assert out["4期/設計/仕様書.md"]["modified_at"] == _iso(_T_OLD)
    assert out["5期/設計/仕様書.md"]["generation"] == "5期"
    assert out["5期/設計/仕様書.md"]["modified_at"] == _iso(_T_NEW)
    # 資料フォルダの直下に置いた資料: 世代の欄は無く、更新時刻だけ
    assert "generation" not in out["直下.md"] and out["直下.md"]["modified_at"] == _iso(_T_NEW)
    # 既存の項目は変わらない
    s = out["4期/設計/仕様書.md"]
    assert s["download_url"].startswith(f"/documents/download?world={_W}&rel=")
    assert set(s) == {"doc_id", "download_url", "generation", "modified_at"}


def test_sources_unresolvable_doc_keeps_generation_only(tree):
    out = CS._sources(["4期/無い.md"], _W)
    assert len(out) == 1 and out[0]["doc_id"] == "4期/無い.md"
    assert out[0]["generation"] == "4期" and "modified_at" not in out[0]


def test_answer_qa_citations_gain_provenance_and_keep_existing_keys(tree):
    c1 = {"doc_id": "4期/設計/仕様書.md", "span": [3, 4], "quote": "本文", "ext": ".md", "match": "税"}
    c2 = {"doc_id": "直下.md", "span": [1, 1], "quote": "直下の本文", "ext": ".md", "excerpt_source": "rag"}
    env = CS._answer_qa({"type": "qa", "citations": [c1, c2]}, _W)
    got = env["data"]["citations"]
    assert got[0] == {**c1, "generation": "4期", "modified_at": _iso(_T_OLD)}
    assert got[1] == {**c2, "modified_at": _iso(_T_NEW)}
    assert env["summary"] == {"total": 2}
    assert [s["doc_id"] for s in env["sources"]] == ["4期/設計/仕様書.md", "直下.md"]


def test_answer_qa_without_citations_is_unchanged(tree):
    env = CS._answer_qa({"type": "qa", "citations": []}, _W)
    assert env["data"]["citations"] == [] and env["sources"] == []


def test_share_keeps_generation_and_modified_at_only_when_strings():
    answer = {"lens": "qa", "headline": "h", "scope": {"world": "w"},
              "sources": [
                  {"doc_id": "4期/a.md", "download_url": "/documents/download?world=w&rel=4%E6%9C%9F%2Fa.md",
                   "generation": "4期", "modified_at": "2026-07-01T00:00:00Z"},
                  {"doc_id": "直下.md", "modified_at": "2026-07-01T00:00:00Z"},
                  {"doc_id": "b.md", "generation": {"x": 1}, "modified_at": 5, "unknown": "落とす"}]}
    rows = {r["doc_id"]: r for r in _safe_share_answer(answer)["sources"]}
    assert rows["4期/a.md"]["generation"] == "4期" and rows["4期/a.md"]["modified_at"] == "2026-07-01T00:00:00Z"
    assert "generation" not in rows["直下.md"] and rows["直下.md"]["modified_at"] == "2026-07-01T00:00:00Z"
    assert set(rows["b.md"]) == {"doc_id"}
