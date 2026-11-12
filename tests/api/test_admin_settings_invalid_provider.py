"""管理設定の応答は、保存済みの embed_provider・vlm.provider が不正値のとき invalid を true にする（実 DB）。"""
from __future__ import annotations

import pytest
from psycopg.types.json import Json

from sherpa import store
from sherpa.routers import system_extras


def _set(key, value):
    with store._connect() as c:
        c.execute(
            "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, 'test') "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, Json(value)))
    store._invalidate_system_settings_cache()


def _clear():
    with store._connect() as c:
        c.execute("DELETE FROM system_settings WHERE key IN ('embed_provider', 'vlm')")
    store._invalidate_system_settings_cache()


@pytest.fixture(autouse=True)
def _clean():
    try:
        store.init_schema()
        _clear()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    yield
    _clear()


def test_view_flags_invalid_saved_providers():
    _set("embed_provider", "gemini")
    _set("vlm", {"provider": "gemini"})
    v = system_extras._admin_settings_view()
    assert v["embed_provider"]["invalid"] is True and v["vlm"]["invalid"] is True
    _clear()
    v = system_extras._admin_settings_view()
    assert v["embed_provider"]["invalid"] is False and v["vlm"]["invalid"] is False
