"""システム設定 `embed_request_timeout_s`・`embed_batch_size` の保存・検証・既定へ戻す動き（置き場と検証は `embed_parallel` と同じ）。"""
from __future__ import annotations

import pytest

from sherpa import store
from test_system_settings import _ADMIN, _clean_system_settings, _put, admin, admin_pair, admin_uid  # noqa: F401


@pytest.mark.parametrize("body", [
    *[{"embed_request_timeout_s": b} for b in (0, 9, 601, True, "60", 1.5)],
    *[{"embed_batch_size": b} for b in (0, -1, 201, True, "50", 1.5)],
])
def test_out_of_range_is_422_and_not_stored(admin, body):
    assert _put(admin, body).status_code == 422
    store._invalidate_system_settings_cache()
    assert not set(body) & set(store.get_system_settings())


@pytest.mark.parametrize("field, value, default", [("embed_request_timeout_s", 180, 60), ("embed_batch_size", 8, 50)])
def test_saved_value_is_effective_and_null_returns_to_default(admin, field, value, default):
    v = _put(admin, {field: value}).json()[field]
    assert v["configured"] == value and v["effective"] == value and v["default"] == default
    assert store.get_system_settings()[field] == value
    v = _put(admin, {field: None}).json()[field]
    assert v["configured"] is None and v["effective"] == default
