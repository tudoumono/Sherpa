"""資料フォルダの完全削除は、どの段の失敗も握りつぶさず伝播させる（呼び出し側が消す仕事の行と資料フォルダの行を残せる）。"""
from __future__ import annotations

import pytest

from sherpa import es_index, store, worlds
from sherpa.ingest import wipe
from sherpa.store import ocr_jobs
from tests._replace_row import offline_row


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """外部境界（DB・Neo4j・ES）を差し替え、呼ばれた段の順を記録する。派生物の置き場は一時ディレクトリ。"""
    seen: list = []
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.setattr(worlds, "observation_removal_target", lambda w: None)
    monkeypatch.setattr(store, "set_world_sig", lambda *a, **kw: seen.append("sig"))
    monkeypatch.setattr(store, "replace_documents", lambda w, rows, **kw: seen.append("ledger") or 0)
    monkeypatch.setattr(ocr_jobs, "purge_world", lambda w, **kw: seen.append("ocr") or {})
    monkeypatch.setattr(es_index, "delete_world", lambda w, **kw: seen.append("es") or True)
    monkeypatch.setattr(store, "clear_failed_docs", lambda w: seen.append("failed_docs") or 0)
    return seen


def test_wipe_runs_every_stage_in_order(calls):
    worlds.derived_dir("w").mkdir(parents=True)
    wipe.wipe_locked("w", reflect=False, row=offline_row("w", kind="delete"))
    assert calls == ["sig", "ledger", "ocr", "es", "failed_docs"]
    assert not worlds.derived_dir("w").exists()


def test_wipe_does_not_swallow_a_derived_removal_failure(calls, monkeypatch):
    worlds.derived_dir("w").mkdir(parents=True)

    def _denied(path, *a, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(wipe.shutil, "rmtree", _denied)
    with pytest.raises(PermissionError):
        wipe.wipe_locked("w", reflect=False, row=offline_row("w", kind="delete"))
    assert "es" not in calls and "failed_docs" not in calls


def test_wipe_does_not_swallow_an_es_failure(calls, monkeypatch):
    def _boom(world, **kw):
        raise OSError("es down")

    monkeypatch.setattr(es_index, "delete_world", _boom)
    with pytest.raises(OSError):
        wipe.wipe_locked("w", reflect=False, row=offline_row("w", kind="delete"))
    assert "failed_docs" not in calls


def test_wipe_fails_when_es_index_is_not_deleted(calls, monkeypatch):
    """ES に届かない・消せなかったときは失敗にして、資料フォルダの行を消す段へ進ませない。"""
    monkeypatch.setattr(es_index, "delete_world", lambda w, **kw: False)
    with pytest.raises(RuntimeError):
        wipe.wipe_locked("w", reflect=False, row=offline_row("w", kind="delete"))
    assert "failed_docs" not in calls


def test_compensation_keeps_the_world_row_when_any_stage_fails(calls, monkeypatch):
    """登録の失敗の補償は、全段が成功したときだけ資料フォルダの行を消す。"""
    import contextlib

    from sherpa.ingest import replace_guard
    deleted: list = []

    @contextlib.contextmanager
    def _held(world, run_id, *, kind="build"):
        yield offline_row(world, kind=kind)

    monkeypatch.setattr(replace_guard, "held_row", _held)
    monkeypatch.setattr(store, "delete_world_row", lambda w: deleted.append(w) or True)

    assert wipe.compensate_failed_register("w", None, reflect=False) is True
    assert deleted == ["w"]

    deleted.clear()
    monkeypatch.setattr(ocr_jobs, "purge_world", lambda w, **kw: (_ for _ in ()).throw(RuntimeError("pg down")))
    assert wipe.compensate_failed_register("w", None, reflect=False) is False
    assert deleted == []
