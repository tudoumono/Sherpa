"""決まり 5 の読む場所の見分け（ソースの組の一覧は 1 か所・ソースの組の無い単位・組の外のソース・管理画面の印）。"""
from __future__ import annotations

from sherpa.ingest import build_link, source_groups
from sherpa.ingest.check import GOOD, WARN, inspect_folder

RELS = [
    "sysA/1期/ソース/Makefile", "sysA/1期/ソース/main.c", "sysA/2期/ソース/Makefile", "sysA/2期/ソース/main.c",
    "sysA/1期/設計/a.md", "sysA/2期/設計/a.md",
    "sysB/tool/run.c", "sysB/docs/a.md",
]


def _items(rels, marks):
    rep = inspect_folder(rels, lambda rel: None, build={"report": None, "marks_sig": "", "marks": marks})
    return [it for it in rep.items if it.rule == "決まり5" and it.path != "." or it.rule == "決まり5" and it.extra.get("found_by") == "marks"
            or it.rule == "決まり5" and "外にある" in it.what]


def test_source_group_list_is_shared():
    from sherpa.ingest.periods import SOURCE_DIR_NAMES
    assert SOURCE_DIR_NAMES is source_groups.SOURCE_DIR_NAMES
    assert build_link.in_source_group("p/ＳＲＣ/Makefile") and not build_link.in_source_group("p/build/Makefile")


def test_places_layout_and_missing_group_and_outside_source():
    items = _items(RELS, {})
    good = {it.path for it in items if it.result == GOOD}
    assert {"sysA/1期", "sysA/2期"} <= good
    warn = {it.path: it for it in items if it.result == WARN}
    assert "sysB" in warn
    outside = [it for it in items if "外にある" in it.what]
    assert outside and outside[0].examples == ["sysB/tool/run.c"]


def test_read_mark_covers_outside_source():
    items = _items(RELS, {"sysB/tool": "read"})
    assert not [it for it in items if "外にある" in it.what]
    assert any(it.extra.get("found_by") == "marks" for it in items)
