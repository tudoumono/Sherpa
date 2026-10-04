"""資料フォルダごとの解決範囲の設定（ANA-15 P5）の契約: 最近傍で決まらない COPY が設定で決まる／別名で #include が決まる／
設定の検証（世代の外・循環）／設定を変えると署名が変わる／設定が無ければ今と同じ。"""
from __future__ import annotations

import pytest

from sherpa.ingest import resolve_settings, worker, world_graph

WID = "p5_test"
_CBL = "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. MAINPG.\n       WORKING-STORAGE SECTION.\n           COPY SHARED-CPY.\n       PROCEDURE DIVISION.\n           GOBACK.\n"
_CPY = "       01 REC-A.\n          05 F1 PIC X.\n"


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _world(tmp_path):
    """同じ世代 G の中に、MAIN から等距離の同名コピーブックが 2 つ（最近傍では決まらない）。"""
    _write(tmp_path, "G/sys/MAINPG.cbl", _CBL)
    _write(tmp_path, "G/lib1/SHARED-CPY.cpy", _CPY)
    _write(tmp_path, "G/lib2/SHARED-CPY.cpy", _CPY)
    _write(tmp_path, "H/lib1/SHARED-CPY.cpy", _CPY)
    return tmp_path


def _copies(root, cfg=None):
    nodes, edges, flags = world_graph.build_world(root, WID, resolve_config=cfg)
    by = {n["cid"]: n["path"] for n in nodes}
    got = [(by[e["src"]], by[e["dst"]], [s.get("rule") for s in e.get("sources", [])]) for e in edges if e["type"] == "COPIES"]
    return got, [f for f in flags if f.get("reason") in ("ambiguous", "unresolved", "cross_scope")]


def test_copy_paths_resolve_ambiguous_copy_in_priority_order(tmp_path):
    root = _world(tmp_path)
    got, flags = _copies(root)
    assert got == [] and [f["reason"] for f in flags] == ["ambiguous"]
    got, flags = _copies(root, {"copy_paths": ["G/lib2", "G/lib1"]})
    assert [(s, d) for s, d, _ in got] == [("G/sys/MAINPG.cbl", "G/lib2/SHARED-CPY.cpy")]
    assert got[0][2] == ["copy_path_setting"] and flags == []
    # 別の世代（H）の prefix は使わない（世代跨ぎなし）
    got, flags = _copies(root, {"copy_paths": ["H/lib1"]})
    assert got == [] and [f["reason"] for f in flags] == ["ambiguous"]


def test_empty_settings_give_same_graph_as_no_settings(tmp_path):
    root = _world(tmp_path)
    base = world_graph.build_world(root, WID)
    assert world_graph.build_world(root, WID, resolve_config={"copy_paths": [], "path_aliases": {}}) == base


def test_nearest_wins_over_copy_paths(tmp_path):
    root = _world(tmp_path)
    _write(root, "G/sys/SHARED-CPY.cpy", _CPY)          # 同じフォルダに 1 つ＝最近傍で一意
    got, _ = _copies(root, {"copy_paths": ["G/lib2"]})
    assert [(d, r) for _, d, r in got] == [("G/sys/SHARED-CPY.cpy", ["nearest_name"])]


def test_path_alias_resolves_include(tmp_path):
    _write(tmp_path, "G/src/main.c", '#include "myinc/log.h"\nint main(void){return 0;}\n')
    _write(tmp_path, "G/shared/inc/log.h", "void lg(void);\n")
    nodes, edges, flags = world_graph.build_world(tmp_path, WID)
    assert not [e for e in edges if e["type"] == "INVOKES"]
    nodes, edges, flags = world_graph.build_world(
        tmp_path, WID, resolve_config={"path_aliases": {"myinc": "G/shared/inc"}})
    by = {n["cid"]: n["path"] for n in nodes}
    inv = [(by[e["src"]], by[e["dst"]], [s["rule"] for s in e["sources"]]) for e in edges if e["type"] == "INVOKES"]
    assert inv == [("G/src/main.c", "G/shared/inc/log.h", ["path_alias_setting"])]


def test_normalize_prefixes_and_rejections():
    s = resolve_settings.normalize({"copy_paths": [" /G\\lib//", "G/lib", "./G/x/"], "path_aliases": {"a": "G/y"}})
    assert s == {"copy_paths": ["G/lib", "G/x"], "path_aliases": {"a": "G/y"}}
    for bad in ({"copy_paths": ["C:foo"]}, {"copy_paths": [""]}, {"copy_paths": ["/"]}, {"copy_paths": ["G/../H"]}, {"copy_paths": ["C:/x"]},
                {"path_aliases": {"a/b": "G"}}, {"path_aliases": {"a": "G/../H"}},
                {"path_aliases": {"a": "b/x", "b": "a/y"}}, {"path_aliases": {"G": "G/inc"}}):
        with pytest.raises(resolve_settings.ResolveSettingsError):
            resolve_settings.normalize(bad)


def test_missing_prefix_is_warned_not_rejected(tmp_path):
    (tmp_path / "G" / "lib").mkdir(parents=True)
    s = resolve_settings.normalize({"copy_paths": ["G/lib", "G/none"]})
    assert resolve_settings.warnings_for(s, tmp_path) == ["COPY の取り込み元の場所「G/none」は資料フォルダの中に見つかりません"]


def test_settings_change_the_world_signature_only_when_present():
    parts = [("a", 1, 2, 3)]
    base = worker._sig(parts)
    assert worker._sig(parts, resolve_settings.signature_material(resolve_settings.empty())) == base
    one = resolve_settings.signature_material({"copy_paths": ["G/a", "G/b"], "path_aliases": {}})
    two = resolve_settings.signature_material({"copy_paths": ["G/b", "G/a"], "path_aliases": {}})
    assert len({base, worker._sig(parts, one), worker._sig(parts, two)}) == 3


def test_pinned_settings_are_read_once_for_the_whole_run(monkeypatch):
    """取り込みの間は始めに読んだ設定だけを使う（保存済みが途中で変わっても、署名・グラフ・確定の記録は同じ値）。"""
    from sherpa import store
    rows = iter([{"resolve_settings": {"copy_paths": ["G/lib1"], "path_aliases": {}}},
                 {"resolve_settings": {"copy_paths": ["G/lib2"], "path_aliases": {}}}])
    monkeypatch.setattr(store, "get_world", lambda w: next(rows))
    with resolve_settings.pinned("w"):
        first = resolve_settings.signature_of("w")
        assert resolve_settings.signature_of("w") == first and resolve_settings.load("w")["copy_paths"] == ["G/lib1"]
    assert resolve_settings.load("w")["copy_paths"] == ["G/lib2"]      # 固定を外せば保存済みを読む


def test_alias_chain_warning_checks_the_expanded_location(tmp_path):
    (tmp_path / "G" / "real").mkdir(parents=True)
    s = resolve_settings.normalize({"path_aliases": {"a": "b/real", "b": "G"}})
    assert resolve_settings.warnings_for(s, tmp_path) == []
