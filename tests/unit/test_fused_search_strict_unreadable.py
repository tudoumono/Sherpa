"""検索の実在確認: 原本が無いことと読めないことを区別する（strict は読めないとき失敗にする）。ES は外部境界のため差し替える。"""
from __future__ import annotations

import os

import pytest

from sherpa.parts.read import fused_search as ss


def _hit(doc):
    return {"key": doc, "doc_id": doc, "path": doc, "line": None, "snippet": "s",
            "engine_score": 1.0, "judgement": None, "paths": None}


@pytest.fixture
def world_root(tmp_path):
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "a.txt").write_text("x")
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "b.txt").write_text("x")
    os.chmod(tmp_path / "locked", 0)
    yield tmp_path
    os.chmod(tmp_path / "locked", 0o755)


def _search(monkeypatch, root, docs, strict):
    monkeypatch.setattr(ss, "_search_keyword", lambda w, q, sp, k, s, layer=None: ([_hit(d) for d in docs], None))
    return ss.search("w", "q", engines=["keyword"], root=root, strict=strict)


@pytest.mark.skipif(os.geteuid() == 0, reason="root は権限で拒否されない")
def test_strict_search_fails_when_origin_unreadable(monkeypatch, world_root):
    with pytest.raises(PermissionError):
        _search(monkeypatch, world_root, ["locked/b.txt"], strict=True)


def test_strict_search_still_drops_genuinely_missing_file(monkeypatch, world_root):
    res = _search(monkeypatch, world_root, ["ok/a.txt", "ok/gone.txt"], strict=True)
    assert [h["doc_id"] for h in res["hits"]] == ["ok/a.txt"]
