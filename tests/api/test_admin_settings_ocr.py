"""管理画面の OCR の入切（GET/PUT /admin/settings の `ocr`・`ocr_enabled`）。要 Postgres。DB 不可は SKIP。"""
from __future__ import annotations

from test_system_settings import _ADMIN, _clean_system_settings, _put, admin, admin_pair  # noqa: F401

from sherpa.ingest import office_md


def test_admin_settings_ocr_default_off_round_trip_and_env_wins(admin, monkeypatch):
    monkeypatch.delenv("SHERPA_OCR_ENABLED", raising=False)
    assert admin.get(_ADMIN).json()["ocr"] == {
        "configured": None, "effective": False, "default": False, "env_override": None}
    r1 = _put(admin, {"ocr_enabled": True})
    assert r1.status_code == 200, r1.text
    assert r1.json()["ocr"]["configured"] is True and r1.json()["ocr"]["effective"] is True
    assert office_md.ocr_enabled() is True
    monkeypatch.setenv("SHERPA_OCR_ENABLED", "0")
    ocr = admin.get(_ADMIN).json()["ocr"]
    assert ocr["configured"] is True and ocr["effective"] is False and ocr["env_override"] is False
    assert office_md.ocr_enabled() is False
    monkeypatch.delenv("SHERPA_OCR_ENABLED")
    r2 = _put(admin, {"ocr_enabled": None})
    assert r2.status_code == 200, r2.text
    assert r2.json()["ocr"]["configured"] is None and r2.json()["ocr"]["effective"] is False
