"""world_neo4j.run_world_impact の並び順。重要度は使わず、`category`・`name` の順だけで並ぶ。

Neo4j 不要——`world_impact`/`resolve_world_entity`（いずれも Neo4j セッション依存）を差し替え、並べ替えだけを検証する。
"""
from __future__ import annotations

from sherpa.ingest import world_neo4j as wn


def _item(name, path=None, category="c", **extra):
    return {"name": name, "category": category, "path": path, "evidence": [], **extra}


def test_run_world_impact_orders_by_category_and_name_even_when_items_carry_importance(monkeypatch):
    items = [_item("zeta", path="z.md"), _item("alpha", path="a.md"),
             _item("beta", path="b.md", importance="高"), _item("gamma", path="g.md", category="a")]
    monkeypatch.setattr(wn, "resolve_world_entity", lambda *a, **k: [])
    monkeypatch.setattr(wn, "read_unresolved", lambda *a, **k: {"available": False, "items": [], "omitted": 0})
    monkeypatch.setattr(wn, "read_plugin_failures", lambda *a, **k: [])
    monkeypatch.setattr(wn, "read_unparsed_sources", lambda *a, **k: None)
    monkeypatch.setattr(wn, "world_impact", lambda *a, **k: items)

    out = wn.run_world_impact(session=None, term="t", world_id="w")
    assert [i["name"] for i in out["items"]] == ["gamma", "alpha", "beta", "zeta"]

