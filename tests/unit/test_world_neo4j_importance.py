"""world_neo4j._attach_importance / run_world_impact のソートキー（I2・J3）の単体テスト。

Neo4j 不要——`_attach_importance` は plain な items リスト＋world_id 文字列だけを受け取る
後処理（`importance.resolve_for_world` と `worlds.world_dir` だけを monkeypatch すれば足りる）。
`run_world_impact` 側は `world_impact`/`resolve_world_entity`（いずれも Neo4j セッション依存）を
差し替え、ソートキーの配線だけを検証する。
"""
from __future__ import annotations

from sherpa.ingest import importance as imp
from sherpa.ingest import world_neo4j as wn


def _res(value, reason=None):
    return imp.Resolution(value=value, reason=reason, config_path="_重要度.txt", rule_line=1)


def _item(name, path=None, evidence=None, category="c"):
    return {"name": name, "category": category, "path": path, "evidence": evidence or []}


# ===== _attach_importance（items への付与） =====

def test_attach_importance_no_control_file_is_noop(monkeypatch):
    monkeypatch.setattr(wn.worlds, "world_dir", lambda w: "/tmp/whatever")
    monkeypatch.setattr(wn.importance, "resolve_for_world", lambda w, root=None: {})
    items = [_item("A", path="a.md"), _item("B", path="b.md")]
    before = [dict(it) for it in items]
    wn._attach_importance(items, "w")
    assert items == before   # 完全不変（受け入れ条件）


def test_attach_importance_unregistered_world_skips_resolve_call(monkeypatch):
    monkeypatch.setattr(wn.worlds, "world_dir", lambda w: None)
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(wn.importance, "resolve_for_world", _boom)
    items = [_item("A", path="a.md")]
    wn._attach_importance(items, "w")
    assert "importance" not in items[0]
    assert called["n"] == 0   # wd が無ければ resolve_for_world 自体を呼ばない


def test_attach_importance_uses_own_path_when_no_evidence(monkeypatch):
    monkeypatch.setattr(wn.worlds, "world_dir", lambda w: "/tmp/x")
    monkeypatch.setattr(wn.importance, "resolve_for_world",
                        lambda w, root=None: {"a.md": _res("高", "契約書")})
    items = [_item("A", path="a.md")]
    wn._attach_importance(items, "w")
    assert items[0]["importance"] == "高" and items[0]["importance_reason"] == "契約書"
    assert "importance_mixed" not in items[0]


def test_attach_importance_picks_highest_among_path_and_evidence(monkeypatch):
    """path 自身は未解決でも、evidence の中に `高` があればそれを採る（最高位＝§J3）。"""
    monkeypatch.setattr(wn.worlds, "world_dir", lambda w: "/tmp/x")
    monkeypatch.setattr(wn.importance, "resolve_for_world", lambda w, root=None: {
        "b.md": _res("低"), "c.md": _res("高", "重要仕様")})
    items = [_item("A", path="a.md", evidence=[{"doc": "b.md"}, {"doc": "c.md"}])]
    wn._attach_importance(items, "w")
    assert items[0]["importance"] == "高" and items[0]["importance_reason"] == "重要仕様"


def test_attach_importance_marks_mixed_when_two_or_more_distinct_values(monkeypatch):
    monkeypatch.setattr(wn.worlds, "world_dir", lambda w: "/tmp/x")
    monkeypatch.setattr(wn.importance, "resolve_for_world", lambda w, root=None: {
        "a.md": _res("高"), "b.md": _res("低")})
    items = [_item("A", path="a.md", evidence=[{"doc": "b.md"}])]
    wn._attach_importance(items, "w")
    assert items[0]["importance"] == "高" and items[0].get("importance_mixed") is True


def test_attach_importance_single_value_repeated_is_not_mixed(monkeypatch):
    monkeypatch.setattr(wn.worlds, "world_dir", lambda w: "/tmp/x")
    monkeypatch.setattr(wn.importance, "resolve_for_world", lambda w, root=None: {
        "a.md": _res("高"), "b.md": _res("高")})
    items = [_item("A", path="a.md", evidence=[{"doc": "b.md"}])]
    wn._attach_importance(items, "w")
    assert "importance_mixed" not in items[0]


def test_attach_importance_no_candidates_leaves_item_unchanged(monkeypatch):
    """path も evidence も無い item（候補ゼロ）は解決しようがない＝無改変。"""
    monkeypatch.setattr(wn.worlds, "world_dir", lambda w: "/tmp/x")
    monkeypatch.setattr(wn.importance, "resolve_for_world", lambda w, root=None: {"a.md": _res("高")})
    items = [_item("A", path=None, evidence=[])]
    wn._attach_importance(items, "w")
    assert "importance" not in items[0]


# ===== run_world_impact（ソートキーの配線・J3） =====

def test_run_world_impact_sort_key_prioritizes_importance(monkeypatch):
    """第1ソートキーが重要度になる（`高`＝最優先・以降は旧来どおり category,name 順）。"""
    items = [_item("zeta", path="z.md"), _item("alpha", path="a.md"), _item("beta", path="b.md")]

    def _stub_world_impact(*a, **k):
        wn._attach_importance(items, "w")   # 実装と同じ配線（world_impact 内で後処理を呼ぶ）
        return items

    monkeypatch.setattr(wn, "resolve_world_entity", lambda *a, **k: [])
    monkeypatch.setattr(wn, "world_impact", _stub_world_impact)
    monkeypatch.setattr(wn.worlds, "world_dir", lambda w: "/tmp/x")
    monkeypatch.setattr(wn.importance, "resolve_for_world", lambda w, root=None: {"b.md": _res("高")})

    out = wn.run_world_impact(session=None, term="t", world_id="w")
    names = [i["name"] for i in out["items"]]
    assert names[0] == "beta"                 # 高＝最優先
    assert names[1:] == ["alpha", "zeta"]      # 残りは category, name 順（旧ソートと同じ）


def test_run_world_impact_sort_key_matches_legacy_order_without_control_file(monkeypatch):
    """`_重要度.txt` の無い world は、旧ソート（category, name のみ）と完全に同じ順序になる
    （受け入れ条件＝影響一覧の出力完全不変）。"""
    items = [_item("zeta", path="z.md"), _item("alpha", path="a.md"), _item("beta", path="b.md")]

    def _stub_world_impact(*a, **k):
        wn._attach_importance(items, "w")
        return items

    monkeypatch.setattr(wn, "resolve_world_entity", lambda *a, **k: [])
    monkeypatch.setattr(wn, "world_impact", _stub_world_impact)
    monkeypatch.setattr(wn.worlds, "world_dir", lambda w: None)   # 未登録＝無 world

    out = wn.run_world_impact(session=None, term="t", world_id="w")
    names = [i["name"] for i in out["items"]]
    assert names == ["alpha", "beta", "zeta"]   # 旧ソート（category, name）と完全一致
