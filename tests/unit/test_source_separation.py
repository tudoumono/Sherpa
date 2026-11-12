"""原本の置き場と派生物の置き場が重なる登録を断り、後片付けの削除が原本を消さない。原本に見立てた一時フォルダの中だけで行う。"""
from __future__ import annotations

import pytest

from sherpa import es_index, store, world_admin_service, worlds
from sherpa.ingest import wipe
from sherpa.store import ocr_jobs
from tests._replace_row import offline_row


@pytest.fixture
def derived(monkeypatch, tmp_path):
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.setenv("SHERPA_OBSERVATION_DIR", str(tmp_path / "obs"))
    monkeypatch.delenv("SHERPA_BROWSE_ROOTS", raising=False)
    (tmp_path / "derived").mkdir()
    return tmp_path / "derived"


def test_register_rejects_source_inside_derived_dir(derived):
    src = derived / "demo"
    src.mkdir()
    with pytest.raises(world_admin_service.WorldAdminValidationError, match="派生物"):
        world_admin_service.resolve_root(str(src))


def test_register_rejects_source_containing_derived_dir(derived):
    with pytest.raises(world_admin_service.WorldAdminValidationError, match="派生物"):
        world_admin_service.resolve_root(str(derived.parent))


def test_register_rejects_source_reached_through_symlinked_parent(derived, tmp_path):
    (derived / "demo").mkdir()
    (tmp_path / "via").symlink_to(derived)
    with pytest.raises(world_admin_service.WorldAdminValidationError, match="派生物"):
        world_admin_service.resolve_root(str(tmp_path / "via" / "demo"))


def test_wipe_does_not_delete_source_that_is_the_derived_dir(derived, monkeypatch):
    src = derived / "demo"
    src.mkdir()
    (src / "original.txt").write_text("原本", encoding="utf-8")
    monkeypatch.setattr(store, "list_worlds_db", lambda: [{"world_id": "demo", "root_path": str(src)}])
    monkeypatch.setattr(worlds, "observation_removal_target", lambda w: None)
    monkeypatch.setattr(store, "set_world_sig", lambda *a, **kw: None)
    monkeypatch.setattr(store, "replace_documents", lambda w, rows, **kw: 0)
    monkeypatch.setattr(ocr_jobs, "purge_world", lambda w, **kw: {})
    monkeypatch.setattr(es_index, "delete_world", lambda w, **kw: True)
    monkeypatch.setattr(store, "clear_failed_docs", lambda w: 0)
    with pytest.raises(ValueError):
        wipe.wipe_locked("demo", reflect=False, row=offline_row("demo", kind="delete"))
    assert (src / "original.txt").read_text(encoding="utf-8") == "原本"
