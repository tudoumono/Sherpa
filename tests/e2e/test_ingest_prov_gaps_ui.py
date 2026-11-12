"""来歴の observation_issues・office_display があるときだけ、読み取りの足りなかった分のバッジを出す。"""
from __future__ import annotations

from mock_api import PREVIEW
from playwright.sync_api import expect
from test_ingest_ui import _doc, _open


def _row_with(prov, name="図.xlsx"):
    return _doc(name, provenance={"method": "ooxml", **prov})


def test_gap_badges_show_for_observation_and_office_display(page, web_base_url):
    docs = [
        _row_with({"observation_issues": ["ocr_observation_unreadable"]}, "obs.xlsx"),
        _row_with({"office_display": {"status": "partial", "reason": None, "requested_cells": 5, "applied_cells": 2}}, "part.xlsx"),
        _row_with({"office_display": {"status": "fallback_linux", "reason": "x", "requested_cells": 5, "applied_cells": 0}}, "fb.xlsx"),
        _row_with({}, "clean.xlsx"),
    ]
    _open(page, web_base_url, preview={**PREVIEW, "documents": docs})
    rows = page.locator("#rows")
    expect(rows.locator("tr", has_text="obs.xlsx")).to_contain_text("画像の読み取り結果の一部を使えていません")
    expect(rows.locator("tr", has_text="part.xlsx")).to_contain_text("Excel の表示の一部を補えていません")
    expect(rows.locator("tr", has_text="fb.xlsx")).to_contain_text("Excel の表示を補えていません")
    clean = rows.locator("tr", has_text="clean.xlsx")
    expect(clean).not_to_contain_text("使えていません")
    expect(clean).not_to_contain_text("補えていません")


def test_gap_badge_shows_for_pptx_flat_slides(page, web_base_url):
    docs = [_row_with({"pptx_flat_slides": 2}, "flat.pptx"), _row_with({}, "ok.pptx")]
    _open(page, web_base_url, preview={**PREVIEW, "documents": docs})
    rows = page.locator("#rows")
    expect(rows.locator("tr", has_text="flat.pptx")).to_contain_text("一部のスライドで図形の取り消し線・重なりを読み取れていません")
    expect(rows.locator("tr", has_text="ok.pptx")).not_to_contain_text("読み取れていません")
