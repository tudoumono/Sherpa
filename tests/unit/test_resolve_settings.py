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
    assert s == {"copy_paths": ["G/lib", "G/x"], "path_aliases": {"a": "G/y"}, "name_rules": []}
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


def test_name_rules_normalize_and_rejections():
    s = resolve_settings.normalize({"name_rules": [{"from": " zza*0 ", "to": "ZZA*1"}, {"from": "ZZB0", "to": "ZZB1"}]})
    assert s["name_rules"] == [{"from": "zza*0", "to": "ZZA*1"}, {"from": "ZZB0", "to": "ZZB1"}]   # 順序は保つ
    assert not resolve_settings.is_empty(s) and resolve_settings.is_empty(resolve_settings.empty())
    for bad in ([{"from": "*0", "to": "1"}], [{"from": "0", "to": "*1"}], [{"from": "*0*", "to": "*1*"}],
                [{"from": "ZZA", "to": "zza"}], [{"from": "A0", "to": "A1"}, {"from": "a0", "to": "A2"}],
                [{"from": "", "to": "A1"}], [{"from": "A 0", "to": "A1"}], [{"from": "A/0", "to": "A1"}],
                [{"from": "A" * 129, "to": "B"}], [{"from": "A0"}], [{"from": "A0", "to": "A1", "x": 1}], "A0=A1",
                [{"from": f"A{i}", "to": f"B{i}"} for i in range(resolve_settings.MAX_ENTRIES + 1)]):
        with pytest.raises(resolve_settings.ResolveSettingsError):
            resolve_settings.normalize({"name_rules": bad})


def test_map_target_is_case_insensitive_ordered_and_deduplicated():
    rules = [{"from": "*0", "to": "*1"}, {"from": "zzaprog0", "to": "ZZAPROG1"}, {"from": "ZZA*", "to": "ZZB*"}, {"from": "Q", "to": "R"}]
    assert resolve_settings.map_target("ZzaProg0", rules) == ["ZzaProg1", "ZZBProg0"]   # `*` の部分は参照の書き方のまま
    assert resolve_settings.map_target("orderService0", [{"from": "*0", "to": "*Impl"}]) == ["orderServiceImpl"]
    assert resolve_settings.map_target("PROG1", rules) == []
    assert resolve_settings.map_target("0", rules) == []                       # `*` は 1 文字以上
    assert resolve_settings.map_target("q", rules) == ["R"] and resolve_settings.map_target("q", []) == []
    assert resolve_settings.map_target("ZZAPROG00", [{"from": "*00", "to": "*0?"}]) == ["ZZAPROG0?"]
    assert resolve_settings.map_target("ZZA?00", [{"from": "*00", "to": "*0?"}]) == []   # 参照の名前の `?` は当てない


def test_name_rule_question_mark_only_on_the_definition_side():
    s = resolve_settings.normalize({"name_rules": [{"from": "*00", "to": "*0？"}, {"from": "ZZB0", "to": "ZZB???"}]})
    assert s["name_rules"][0] == {"from": "*00", "to": "*0？"}
    for bad in ([{"from": "*0?", "to": "*00"}], [{"from": "ZZ？0", "to": "ZZ00"}], [{"from": "*00", "to": "*0????"}]):
        with pytest.raises(resolve_settings.ResolveSettingsError) as e:
            resolve_settings.normalize({"name_rules": bad})
        assert "?" in str(e.value)


def test_name_matches_ignores_extension_and_case_and_counts_one_char():
    m = resolve_settings.name_matches
    assert m("ZZAPROG0?", "ZZAPROG01.c") and m("ZZAPROG0?", "zzaprog02") and m("zzaprog0？", "ＺＺＡＰＲＯＧ０１")
    assert not m("ZZAPROG0?", "ZZAPROG0") and not m("ZZAPROG0?", "ZZAPROG011.c") and not m("ZZAPROG0?", "ZZBPROG01.c")
    assert m("ZZAPROG01.C", "ZZAPROG01.c") and not m("ZZAPROG01", "ZZAPROG01.c.bak")   # 外すのは最後の拡張子 1 つだけ
    assert not m("ZZAPROG0?", ".c")
    assert m("ZZAPROG01", "ZZAPROG01.c") and not m("ZZAPROG01.C", "ZZAPROG01.c.main")   # 修飾名の末尾は拡張子と見なさない


def test_empty_name_rules_keep_the_signature_byte_identical():
    old = {"copy_paths": ["G/a"], "path_aliases": {"x": "G/b"}}
    assert resolve_settings.signature_material({**old, "name_rules": []}) == resolve_settings.signature_material(old)
    assert resolve_settings.signature_material({"copy_paths": [], "path_aliases": {}, "name_rules": []}) == ""
    with_rules = resolve_settings.signature_material({**old, "name_rules": [{"from": "*0", "to": "*1"}]})
    assert with_rules not in ("", resolve_settings.signature_material(old))
