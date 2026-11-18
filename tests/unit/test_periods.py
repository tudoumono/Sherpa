"""`sherpa.ingest.periods`（期の置き場・並び順・親の期・表記の揺れ）。入力は相対パスの一覧と、変更一覧を読む関数だけ。"""
from __future__ import annotations

from sherpa.ingest.periods import period_number, read_periods


def _clist(period: str, basis: str) -> str:
    return f"# {period} の変更一覧（基準: {basis}）\n区分,種別,名前,新しい名前\n"


def _read(files):
    """files は 相対パス → 本文（本文の要らないファイルは None 以外の適当な文字列でよい）。"""
    return read_periods(list(files), lambda rel: files.get(rel))


def _numbered(pj: str, names, tail="pgm/A.src", where="") -> dict:
    return {f"{pj}/{where}{n}/{tail}": "x\n" for n in names}


def _parents(res, pj: str) -> dict:
    return {(x.period, x.scope): (x.parent, x.kind) for x in res.by_pj[pj].parents}


def test_period_number_takes_the_first_digits_and_not_kanji():
    f = period_number
    assert [f("4期"), f("01_初版"), f("2023年度"), f("R02"), f("第3次"), f("第１０次")] == [4, 1, 2023, 2, 3, 10]
    assert f("最新") is None and f("旧") is None and f("第三次") is None


def test_period_places_are_found_at_any_depth_in_each_pj_even_when_the_order_of_levels_differs():
    files = {
        "国内送金/4期/pgm/DOM001.src": "x", "国内送金/5期/pgm/DOM001.src": "x",
        "国際送金/基本設計/1期/設計書.md": "x", "国際送金/基本設計/2期/設計書.md": "x",
        "国際送金/開発/1期/src/INT001.src": "x", "国際送金/開発/2期/src/INT001.src": "x",
        "口座/本番/資料/R1/a/ACC001.src": "x", "口座/本番/資料/R2/a/ACC001.src": "x",
    }
    res = _read(files)
    assert set(res.slots) == {"国内送金", "国際送金/基本設計", "国際送金/開発", "口座/本番/資料"}
    assert res.period_of("国際送金/開発/2期/src/INT001.src") == ("2期", 2)
    assert res.period_of("国内送金/5期/pgm/DOM001.src") == ("5期", 1)
    assert res.period_of("口座/本番/資料/R2/a/ACC001.src") == ("R2", 3)
    assert res.rel_path("国際送金/開発/2期/src/INT001.src") == "国際送金/開発/src/INT001.src"
    assert res.slots["国際送金/開発"].order == ["1期", "2期"] and res.slots["国際送金/開発"].layout
    assert [s.path for s in res.by_pj["国際送金"].slots] == ["国際送金/基本設計", "国際送金/開発"]


def test_a_place_whose_children_do_not_all_carry_a_number_is_not_a_period_place():
    files = _numbered("案件", ["1期", "2期", "最新", "旧"], "src/A.src")
    res = _read(files)
    assert not res.slots and res.period_of("案件/1期/src/A.src")[0] is None
    numbered = {k: v for k, v in files.items() if "最新" not in k and "旧" not in k}
    assert set(_read(numbered).slots) == {"案件"}


def test_a_place_named_in_a_change_list_is_still_a_period_place_but_the_order_stays_undecided():
    files = {**_numbered("案件", ["1期", "2期", "最新"], "src/A.src"), "案件/2期/変更一覧.csv": _clist("2期", "1期")}
    res = _read(files)
    slot = res.slots["案件"]
    assert slot.order is None and slot.recorded and not slot.layout
    assert _parents(res, "案件") == {("1期", None): (None, "none"), ("2期", None): ("1期", "list"), ("最新", None): (None, "none")}


def test_numbered_siblings_with_no_shared_relative_path_are_not_periods():
    assert not _read({"基盤/module1/a/ONE.src": "x", "基盤/module2/b/TWO.src": "x"}).slots
    assert set(_read({"基盤/module1/a/ONE.src": "x", "基盤/module2/a/ONE.src": "x"}).slots) == {"基盤"}


def test_periods_are_ordered_by_number_not_by_text_and_the_parent_is_conditional():
    res = _read(_numbered("口座", ["2期", "4期", "10期"]))
    assert res.slots["口座"].order == ["2期", "4期", "10期"]
    assert _parents(res, "口座") == {("2期", None): (None, "oldest"), ("4期", None): ("2期", "order"), ("10期", None): ("4期", "order")}


def test_full_width_digits_are_read_as_numbers_when_ordering():
    res = _read(_numbered("口座", ["第１次", "第２次", "第１０次"]))
    assert res.slots["口座"].order == ["第１次", "第２次", "第１０次"]
    assert _parents(res, "口座")[("第１０次", None)] == ("第２次", "order")


def test_equal_numbers_do_not_make_a_period_place():
    for names in (("R1", "v1", "3期"), ("1期", "01_初版", "3期")):
        assert not _read(_numbered("口座", names)).slots
    assert set(_read(_numbered("口座", ("R1", "v2", "3期"))).slots) == {"口座"}


def test_the_change_list_base_wins_over_the_name_order():
    files = {**_numbered("口座", ["1期", "2期", "3期"]), "口座/3期/変更一覧.csv": _clist("3期", "1期")}
    parents = _parents(_read(files), "口座")
    assert parents[("3期", None)] == ("1期", "list") and parents[("2期", None)] == ("1期", "order")


def test_periods_of_different_pjs_are_never_joined():
    res = _read({**_numbered("国内送金", ["1期", "2期", "3期"]), **_numbered("国際送金", ["1期", "2期"])})
    assert set(res.by_pj) == {"国内送金", "国際送金"}
    assert _parents(res, "国内送金")[("3期", None)] == ("2期", "order")
    assert _parents(res, "国際送金")[("2期", None)] == ("1期", "order") and ("3期", None) not in _parents(res, "国際送金")


def test_a_change_list_of_one_pj_does_not_decide_the_parent_of_the_same_named_period_in_another_pj():
    files = {**_numbered("国内送金", ["1期", "2期", "3期"]), **_numbered("国際送金", ["1期", "2期", "3期"]),
             "国内送金/3期/変更一覧.csv": _clist("3期", "1期")}
    res = _read(files)
    assert _parents(res, "国内送金")[("3期", None)] == ("1期", "list")
    assert _parents(res, "国際送金")[("3期", None)] == ("2期", "order")
    assert [cl.pj for cl in res.change_lists] == ["国内送金"]


def test_a_slot_without_the_layout_condition_is_still_a_slot_of_its_pj():
    files = {f"国内送金/{n}期/pgm/D{n}.src": "x" for n in (1, 2, 3)}
    files.update({f"国際送金/{n}期/pgm/I{n}.src": "x" for n in (1, 2, 3)})
    files["国内送金/3期/変更一覧.csv"] = _clist("3期", "1期")
    res = _read(files)
    assert not res.slots["国際送金"].layout and res.slots["国際送金"].pj == "国際送金"
    assert res.slots["国内送金"].recorded and not res.slots["国内送金"].layout
    assert _parents(res, "国際送金")[("3期", None)] == ("2期", "order")


def test_a_change_list_outside_every_pj_is_not_applied_to_a_known_pj():
    files = {**_numbered("国内送金", ["1期", "2期", "3期"]), **_numbered("国際送金", ["1期", "2期"]),
             "変更一覧.csv": _clist("3期", "1期"), "変更一覧2.csv": _clist("2期", "1期")}
    res = _read(files)
    assert [cl.pj for cl in res.change_lists] == [None, None]
    assert _parents(res, "国内送金")[("3期", None)] == ("2期", "order")


def test_conflicting_bases_and_cycles_are_reported_as_conflict():
    files = {**_numbered("P", ["1期", "2期", "3期"]), "P/3期/変更一覧.csv": _clist("3期", "2期"), "P/3期/変更一覧2.csv": _clist("3期", "1期")}
    assert _parents(_read(files), "P")[("3期", None)] == (None, "conflict")
    loop = {**_numbered("P", ["1期", "2期"]), "P/1期/変更一覧.csv": _clist("1期", "2期"), "P/2期/変更一覧.csv": _clist("2期", "1期")}
    assert _parents(_read(loop), "P")[("2期", None)] == (None, "conflict")


def test_a_period_that_leads_to_a_conflict_further_back_is_a_conflict_too():
    files = {**_numbered("P", ["1期", "2期", "3期"]), "P/2期/変更一覧.csv": _clist("2期", "1期"), "P/2期/変更一覧2.csv": _clist("2期", "3期")}
    assert _parents(_read(files), "P")[("3期", None)] == (None, "conflict")


# ---------------------------------------------------------------- 変更一覧の適用範囲

def _docs_files() -> dict:
    return {
        "T1/invoice/src/render.src": "x", "T1/payment/src/rates.src": "x", "T2/payment/src/fees.src": "x",
        "T2/invoice/docs/変更一覧.csv": _clist("T2", "T1"), "T2/payment/docs/変更一覧.csv": _clist("T2", "T1"),
    }


def test_change_list_under_docs_applies_to_the_subsystem_above_it():
    res = _read(_docs_files())
    assert {cl.path: (cl.period, cl.basis, cl.scope) for cl in res.change_lists} == {
        "T2/invoice/docs/変更一覧.csv": ("T2", "T1", "invoice"), "T2/payment/docs/変更一覧.csv": ("T2", "T1", "payment")}


def test_change_list_directly_under_the_period_folder_applies_to_every_subsystem():
    files = {k: v for k, v in _docs_files().items() if "変更一覧" not in k}
    files["T2/変更一覧.csv"] = _clist("T2", "T1")
    assert [(cl.scope, cl.period) for cl in _read(files).change_lists] == [(None, "T2")]


def test_change_list_whose_subsystem_cannot_be_told_from_its_place_keeps_the_folder_name():
    files = {"T1/invoice/src/a.src": "x", "T2/invoice/docs/変更一覧.csv": _clist("T2", "T1"), "T2/invoice/src/b.src": "x"}
    assert [cl.scope for cl in _read(files).change_lists] == ["docs"]


def test_change_list_in_a_period_folder_of_another_name_is_not_used():
    files = {**_numbered("P", ["1期", "2期", "3期"]), "P/2期/変更一覧.csv": _clist("9期", "1期")}
    res = _read(files)
    assert not res.change_lists and res.ignored_lists[0][0] == "P/2期/変更一覧.csv" and "9期" in res.ignored_lists[0][1]


def test_change_list_with_an_unreadable_header_and_unreadable_files_are_collected():
    files = {**_numbered("P", ["1期", "2期"]), "P/2期/変更一覧.csv": "メモ\n", "P/1期/変更一覧.md": None}
    res = read_periods(list(files), lambda rel: files[rel] if rel != "P/1期/変更一覧.md" else None)
    assert res.unparsed_lists == ["P/2期/変更一覧.csv"] and res.unreadable == ["P/1期/変更一覧.md"]


# ---------------------------------------------------------------- 並びの隣どうし・表記の揺れ

def _pair(a: str, b: str) -> dict:
    return {"P/1期/メモ.md": "x", "P/2期/メモ.md": "x", f"P/1期/{a}": "x", f"P/2期/{b}": "x"}


def test_neighbours_count_the_same_relative_paths():
    cmp = _read(_pair("Svc/A.java", "Svc/A.java")).by_pj["P"].comparisons["P"]
    assert cmp.same == 2 and not cmp.variants and not cmp.others and not cmp.empty_pairs


def test_source_and_document_spelling_variants_are_paired():
    cmp = _read(_pair("src/Svc/A.java", "ソース/Svc/A.java")).by_pj["P"].comparisons["P"]
    assert cmp.variants == {("src", "ソース"): ["1期/src/Svc/A.java → 2期/ソース/Svc/A.java"]} and not cmp.others
    cmp = _read(_pair("docs/Svc/A.md", "設計書/Svc/A.md")).by_pj["P"].comparisons["P"]
    assert list(cmp.variants) == [("docs", "設計書")]


def test_a_difference_outside_the_known_pairs_is_another_part():
    cmp = _read(_pair("src/Svc/A.java", "program/Svc/A.java")).by_pj["P"].comparisons["P"]
    assert not cmp.variants and cmp.others == ["1期/src/Svc/A.java → 2期/program/Svc/A.java"]


def test_a_file_name_that_is_not_unique_in_a_period_is_not_compared():
    files = {**_pair("src/Svc/A.java", "program/Svc/A.java"), "P/1期/src/Other/A.java": "x"}
    assert not _read(files).by_pj["P"].comparisons["P"].others


def test_neighbours_without_a_shared_relative_path_are_listed():
    files = {"P/2期/Svc/A.java": "x", "P/3期/Svc/B.java": "x", "P/3期/Svc/C.java": "x", "P/4期/Svc/C.java": "x"}
    cmp = _read(files).by_pj["P"].comparisons["P"]
    assert cmp.same == 1 and cmp.empty_pairs == ["2期 → 3期"]


def test_only_slots_with_an_order_are_compared():
    files = {**_numbered("P", ["改修A", "改修B"], "src/A.java"), "P/改修B/変更一覧.csv": _clist("改修B", "改修A")}
    assert not _read(files).by_pj["P"].comparisons


def test_the_same_file_name_in_several_folders_still_counts_shared_paths():
    files = {f"P/{n}/{d}/A.java": "x" for n in ("2期", "3期") for d in ("src", "test")}
    cmp = _read(files).by_pj["P"].comparisons["P"]
    assert cmp.same == 2 and not cmp.variants and not cmp.others


# ---------------------------------------------------------------- 入れ子の置き場

def test_a_slot_below_the_period_folder_of_another_slot_is_nested_and_not_a_slot():
    files = {"P/本番資料/Svc/A.md": "x", "P/改修/3期/変更一覧.csv": _clist("3期", "本番資料"),
             **_numbered("P", ["2期", "3期", "4期"], "Svc/A.md", where="改修/")}
    res = _read(files)
    info = res.by_pj["P"]
    assert [s.path for s in info.slots] == ["P"] and [(n.slot.path, n.outer, n.outer_period) for n in info.nested] == [("P/改修", "P", "改修")]
    assert res.period_of("P/改修/3期/Svc/A.md") == ("改修", 1)
    assert {x.period for x in info.parents} == {"本番資料", "改修"} and "P/改修" not in info.comparisons


def test_root_itself_is_a_slot_only_when_a_change_list_names_its_children():
    files = {"1期/Svc/A.md": "x", "2期/Svc/A.md": "x"}
    assert not _read(files).slots
    res = _read({**files, "2期/変更一覧.csv": _clist("2期", "1期")})
    assert set(res.slots) == {""} and res.slots[""].pj is None and res.slots[""].children == ["1期", "2期"]
    assert [cl.pj for cl in res.change_lists] == [None]


# ---------------------------------------------------------------- 入力の扱い

def test_the_tree_is_made_from_the_file_names_only_and_in_a_fixed_order():
    files = {"P/2期/a.md": "x", "P/1期/a.md": "x", "README.md": "x", "P/a.md": "x"}
    a = read_periods(list(files), files.get)
    b = read_periods(reversed(list(files)), files.get)
    assert a.tree.files == b.tree.files == ["README.md", "P/a.md", "P/1期/a.md", "P/2期/a.md"]
    assert a.pjs == ["P"] and a.outside_files == ["README.md", "P/a.md"]


def test_read_text_is_asked_only_for_change_list_candidates():
    asked = []
    files = {"P/1期/a.src": "x", "P/2期/a.src": "x", "P/2期/変更一覧.csv": _clist("2期", "1期"), "P/1期/変更一覧.pdf": "x"}
    read_periods(list(files), lambda rel: asked.append(rel) or files[rel])
    assert asked == ["P/2期/変更一覧.csv"]


def test_a_long_whitespace_only_line_does_not_stall_the_header_match():
    import time

    from sherpa.ingest.periods.records import read_change_lists
    t0 = time.monotonic()
    lists, unparsed, _ = read_change_lists(["サブA/第2期/変更一覧.tsv"], lambda rel: "\t" * 5000 + "\n")
    assert lists == [] and unparsed == ["サブA/第2期/変更一覧.tsv"]
    assert time.monotonic() - t0 < 2
