"""`sherpa.ingest.check`（案 B の置き方の点検）: 決まり 1〜4 と今ある記録の 合う／注意／合わない。

入力は相対パスの一覧と、変更一覧を読む関数。資料は試験の中に書いた小さな架空の木と、V2・V5・V9 と同じ形の木。
"""
from __future__ import annotations

import json

import pytest

from sherpa.ingest.check import (BAD, GOOD, RULES, WARN, Item, Report, inspect_folder, json_text, summarize,
                                 to_json, to_markdown, write_json, write_markdown)
from sherpa.ingest.periods import read_periods

EXAMPLE_LIMIT = 5


def _clist(period: str, basis: str) -> str:
    return f"# {period} の変更一覧（基準: {basis}）\n区分,種別,名前,新しい名前\n"


def _items(files) -> list:
    return inspect_folder(list(files), files.get).items


def _pick(items, rule, result, path=None) -> list:
    return [it for it in items if it.rule == rule and it.result == result and (path is None or it.path == path)]


def _periods(files: dict, pj: str, names, tails=("Svc/A.md",), where="") -> dict:
    for n in names:
        for t in tails:
            files[f"{pj}/{where}{n}/{t}"] = "x\n"
    return files


# ---------------------------------------------------------------- 決まり 1

def test_rule1_subsystems_are_listed_and_root_files_warn():
    files = {"README.md": "x\n"}
    _periods(files, "受注", ["1期", "2期"])
    _periods(files, "出荷", ["1期", "2期"])
    items = _items(files)
    pj = _pick(items, "決まり1", GOOD)
    assert len(pj) == 1 and pj[0].extra["pjs"] == ["出荷", "受注"]
    root_files = _pick(items, "決まり1", WARN)
    assert len(root_files) == 1 and root_files[0].examples == ["README.md"] and root_files[0].count == 1


def test_rule1_no_subsystem_is_bad():
    items = _items({"a.md": "x\n"})
    assert len(_pick(items, "決まり1", BAD)) == 1 and not _pick(items, "決まり1", GOOD)


def test_rule1_periods_directly_under_root_warn():
    items = _items({"1期/Svc/A.md": "x\n", "2期/Svc/A.md": "x\n", "2期/変更一覧.csv": _clist("2期", "1期")})
    assert len(_pick(items, "決まり1", WARN, ".")) == 1 and not _pick(items, "決まり1", GOOD)


# ---------------------------------------------------------------- 決まり 2

def test_rule2_slot_found_by_layout_and_by_record():
    files = {"P/3期/変更一覧.csv": _clist("3期", "2期")}
    _periods(files, "P", ["2期", "3期"])
    _periods(files, "Q", ["7期", "8期"], where="開発/")
    slots = {it.path: it for it in _pick(_items(files), "決まり2", GOOD)}
    assert slots["P"].extra["found_by"] == ["record", "layout"] and slots["P"].extra["children"] == ["2期", "3期"]
    assert slots["Q/開発"].extra["found_by"] == ["layout"]


def test_rule2_pj_without_slot_warns():
    warn = _pick(_items({"P/docs/a.md": "x\n", "P/src/b.md": "x\n"}), "決まり2", WARN, "P")
    assert len(warn) == 1 and "期を持たない" in warn[0].fix


def test_rule2_near_slot_with_some_numbers_is_bad():
    bad = _pick(_items(_periods({}, "P", ["2021年度", "2022年度", "最新"], where="開発/")), "決まり2", BAD, "P/開発")
    assert len(bad) == 1 and bad[0].extra["unnumbered"] == ["最新"] and bad[0].count == 1
    assert "最新" in bad[0].what and "漢数字" not in bad[0].what


def test_rule2_near_slot_with_duplicate_numbers_is_bad():
    bad = _pick(_items(_periods({}, "P", ["1期", "1期_旧", "2期"], where="開発/")), "決まり2", BAD, "P/開発")
    assert len(bad) == 1 and bad[0].extra["duplicated"] == ["1期", "1期_旧"] and bad[0].extra["unnumbered"] == []


def test_rule2_near_slot_without_any_number_warns():
    items = _items(_periods({}, "P", ["改修A", "改修B"], where="工程/"))
    warn = _pick(items, "決まり2", WARN, "P/工程")
    assert len(warn) == 1 and "期のフォルダなら" in warn[0].fix and "問題ない" in warn[0].fix
    assert not _pick(items, "決まり2", BAD)


def test_rule2_partly_numbered_with_kanji_name_is_bad_with_note():
    bad = _pick(_items(_periods({}, "P", ["1期", "二期"], where="開発/")), "決まり2", BAD, "P/開発")
    assert len(bad) == 1 and "漢数字は番号として読まない" in bad[0].what and "二期" in bad[0].what


def test_rule2_all_kanji_names_warn_with_note():
    items = _items(_periods({}, "P", ["一期", "二期"], where="開発/"))
    warn = _pick(items, "決まり2", WARN, "P/開発")
    assert len(warn) == 1 and "漢数字は番号として読まない" in warn[0].what and "一期" in warn[0].what
    assert not _pick(items, "決まり2", BAD) and not _pick(items, "決まり2", GOOD)


def test_rule2_same_parts_outside_the_slot_are_bad():
    files = {"P/本番資料/Svc/A.md": "x\n", "P/本番資料/Svc/B.md": "x\n", "P/共通/メモ.md": "x\n"}
    _periods(files, "P", ["2期", "3期"], where="改修/", tails=("Svc/A.md", "Svc/C.md"))
    items = _items(files)
    bad = _pick(items, "決まり2", BAD, "P/本番資料")
    assert len(bad) == 1 and bad[0].count == 1 and "並びに入っていない" in bad[0].what
    assert not _pick(items, "決まり2", BAD, "P/共通")
    assert [it.path for it in _pick(items, "決まり2", GOOD)] == ["P/改修"]


def test_rule2_shallowest_folder_only_per_branch():
    files = {"P/旧/Svc/A.md": "x\n", "P/旧/二次/Svc/A.md": "x\n"}
    _periods(files, "P", ["2期", "3期"], where="改修/")
    assert [it.path for it in _pick(_items(files), "決まり2", BAD)] == ["P/旧"]


def test_rule2_inside_and_above_a_slot_are_not_candidates():
    items = _items(_periods({}, "P", ["1期", "2期"], tails=("基本設計/A.md", "開発/A.md")))
    assert not _pick(items, "決まり2", BAD) and not _pick(items, "決まり2", WARN)


# ---------------------------------------------------------------- 決まり 3

def _pair_files(a_path: str, b_path: str) -> dict:
    return {"P/1期/メモ.md": "x\n", "P/2期/メモ.md": "x\n", f"P/1期/{a_path}": "x\n", f"P/2期/{b_path}": "x\n"}


def test_rule3_same_place_counts_as_good():
    items = _items(_pair_files("Svc/A.java", "Svc/A.java"))
    good = _pick(items, "決まり3", GOOD, "P")
    assert len(good) == 1 and good[0].count == 2
    assert not _pick(items, "決まり3", WARN) and not _pick(items, "決まり3", BAD)


def test_rule3_source_folder_spelling_variant_warns():
    items = _items(_pair_files("src/Svc/A.java", "ソース/Svc/A.java"))
    warn = _pick(items, "決まり3", WARN, "P")
    assert len(warn) == 1 and warn[0].extra["names"] == ["src", "ソース"] and warn[0].count == 1
    assert warn[0].examples == ["1期/src/Svc/A.java → 2期/ソース/Svc/A.java"]
    assert not _pick(items, "決まり3", BAD)


def test_rule3_document_folder_spelling_variant_warns():
    warn = _pick(_items(_pair_files("docs/Svc/A.md", "設計書/Svc/A.md")), "決まり3", WARN, "P")
    assert warn[0].extra["names"] == ["docs", "設計書"]


def test_rule3_variant_outside_the_known_pairs_is_bad():
    items = _items(_pair_files("src/Svc/A.java", "program/Svc/A.java"))
    bad = _pick(items, "決まり3", BAD, "P")
    assert len(bad) == 1 and bad[0].count == 1 and "別の部品" in bad[0].treatment
    assert not _pick(items, "決まり3", WARN)


def test_rule3_ambiguous_file_names_are_not_compared():
    files = {**_pair_files("src/Svc/A.java", "program/Svc/A.java"), "P/1期/src/Other/A.java": "x\n"}
    assert not _pick(_items(files), "決まり3", BAD)


def test_rule3_only_slots_with_an_order():
    files = _periods({}, "P", ["改修A", "改修B"], tails=("src/A.java",))
    files.update({"P/改修B/変更一覧.csv": _clist("改修B", "改修A"), "P/改修A/program/A.java": "x\n"})
    assert not _pick(_items(files), "決まり3", GOOD)


def test_rule3_no_shared_relative_path_between_neighbours_warns():
    files = {"P/2期/Svc/A.java": "x\n", "P/3期/Svc/B.java": "x\n", "P/3期/変更一覧.csv": _clist("3期", "2期")}
    items = _items(files)
    assert not _pick(items, "決まり3", GOOD)
    warn = _pick(items, "決まり3", WARN, "P")
    assert len(warn) == 1 and warn[0].count == 1 and warn[0].examples == ["2期 → 3期"]
    assert "前の期へたどれない" in warn[0].treatment and warn[0].fix


def test_rule3_one_neighbour_pair_without_shared_path_warns_even_if_another_has_some():
    items = _items({"P/2期/Svc/A.java": "x\n", "P/3期/Svc/B.java": "x\n", "P/3期/Svc/C.java": "x\n", "P/4期/Svc/C.java": "x\n"})
    good = _pick(items, "決まり3", GOOD, "P")
    warn = _pick(items, "決まり3", WARN, "P")
    assert len(good) == 1 and good[0].count == 1
    assert len(warn) == 1 and warn[0].examples == ["2期 → 3期"] and warn[0].count == 1


def test_rule3_same_file_name_in_several_folders_still_counts_shared_paths():
    items = _items({f"P/{n}/{d}/A.java": "x\n" for n in ("2期", "3期") for d in ("src", "test")})
    good = _pick(items, "決まり3", GOOD, "P")
    assert len(good) == 1 and good[0].count == 2
    assert not _pick(items, "決まり3", WARN) and not _pick(items, "決まり3", BAD)


def test_nested_slot_made_by_a_basis_only_name_is_bad_and_hidden_from_rules_3_and_4():
    files = {"P/本番資料/Svc/A.md": "x\n", "P/改修/3期/変更一覧.csv": _clist("3期", "本番資料")}
    _periods(files, "P", ["2期", "3期", "4期"], where="改修/")
    items = _items(files)
    assert [it.path for it in _pick(items, "決まり2", GOOD)] == ["P"]
    bad = _pick(items, "決まり2", BAD)
    assert [it.path for it in bad] == ["P/改修"] and bad[0].extra["outer"] == "P" and bad[0].extra["outer_period"] == "改修"
    assert not [it for it in items if it.rule in ("決まり3", "決まり4") and it.path == "P/改修"]
    assert not any("3期" in e for it in items if it.rule == "決まり4" for e in it.examples)
    assert not _pick(items, "決まり2", WARN)


# ---------------------------------------------------------------- 決まり 4

def test_rule4_order_and_parents_by_order_warn():
    items = _items(_periods({}, "P", ["2期", "3期", "4期"]))
    order = _pick(items, "決まり4", GOOD, "P")
    assert len(order) == 1 and order[0].extra["order"] == ["2期", "3期", "4期"]
    kinds = {it.extra["kind"]: it for it in _pick(items, "決まり4", WARN, "P")}
    assert kinds["order"].examples == ["3期 → 2期", "4期 → 3期"] and kinds["oldest"].examples == ["2期 → 親なし"]
    assert not _pick(items, "決まり4", BAD)


def test_rule4_parent_from_change_list_is_good():
    files = _periods({"P/3期/変更一覧.csv": _clist("3期", "2期")}, "P", ["2期", "3期"])
    good = [it for it in _pick(_items(files), "決まり4", GOOD, "P") if it.extra.get("kind") == "list"]
    assert len(good) == 1 and good[0].examples == ["3期 → 2期"] and good[0].treatment and not good[0].fix


def test_rule4_parent_from_change_list_beats_the_order():
    files = _periods({"P/4期/変更一覧.csv": _clist("4期", "2期")}, "P", ["2期", "3期", "4期"])
    good = [it for it in _pick(_items(files), "決まり4", GOOD, "P") if it.extra.get("kind") == "list"]
    assert good[0].examples == ["4期 → 2期"]


def test_rule4_unordered_periods_without_list_are_bad():
    files = _periods({"P/改修B/変更一覧.csv": _clist("改修B", "改修A")}, "P", ["改修A", "改修B", "改修C"])
    items = _items(files)
    bad = _pick(items, "決まり4", BAD, "P")
    assert len(bad) == 1 and bad[0].extra["kind"] == "none" and bad[0].examples == ["改修A → 決まらない", "改修C → 決まらない"]
    warn = _pick(items, "決まり4", WARN, "P")
    assert len(warn) == 1 and warn[0].extra["unnumbered"] == ["改修A", "改修B", "改修C"]
    assert [it.examples for it in _pick(items, "決まり4", GOOD, "P")] == [["改修B → 改修A"]]


def test_rule4_unordered_reason_names_duplicates_and_kanji():
    files = _periods({"P/一期/変更一覧.csv": _clist("一期", "零期")}, "P", ["一期", "二期", "3期", "3期_旧"])
    unordered = [it for it in _pick(_items(files), "決まり4", WARN, "P") if "並びが決まらない" in it.what]
    assert len(unordered) == 1
    assert unordered[0].extra["duplicated"] == ["3期", "3期_旧"] and "漢数字は番号として読まない" in unordered[0].what


def test_rule4_list_scope_and_other_subsystems():
    files = _periods({"P/3期/受注/変更一覧.csv": _clist("3期", "2期")}, "P", ["2期", "3期"], tails=("受注/A.md", "請求/A.md"))
    items = _items(files)
    ex = {it.extra["kind"]: it.examples for it in _pick(items, "決まり4", GOOD, "P") + _pick(items, "決まり4", WARN, "P") if "kind" in it.extra}
    assert ex["list"] == ["3期（受注） → 2期"] and ex["order"] == ["3期 → 2期"]


def test_rule4_conflicting_bases_are_bad():
    files = _periods({"P/3期/変更一覧.csv": _clist("3期", "2期"), "P/3期/変更一覧2.csv": _clist("3期", "1期")}, "P", ["1期", "2期", "3期"])
    bad = _pick(_items(files), "決まり4", BAD, "P")
    assert [it.extra["kind"] for it in bad] == ["conflict"] and bad[0].examples == ["3期 → 決まらない"]


# ---------------------------------------------------------------- 今ある記録

def test_records_lists_read_and_not_used():
    files = {"P/3期/変更一覧.csv": _clist("3期", "2期"), "P/2期/変更一覧.csv": _clist("9期", "1期"), "P/4期/変更一覧.csv": "メモ\n"}
    _periods(files, "P", ["2期", "3期", "4期"])
    items = _items(files)
    read = [it for it in _pick(items, "今ある記録", GOOD, "") if it.what.startswith("変更一覧を")]
    assert len(read) == 1 and read[0].count == 1 and "P/3期/変更一覧.csv（期 3期・基準 2期・適用範囲 期全体）" in read[0].examples
    bad = _pick(items, "今ある記録", BAD)
    assert len(bad) == 2
    assert bad[0].what.startswith("使わなかった") and bad[0].examples[0].startswith("P/2期/変更一覧.csv（一覧の見出しの期 9期")
    assert bad[1].what.startswith("1 行目の見出しが読めない") and bad[1].examples == ["P/4期/変更一覧.csv"]


def test_records_without_change_lists_say_so():
    items = _items(_periods({}, "P", ["1期", "2期"]))
    assert next(it.what for it in _pick(items, "今ある記録", GOOD)) == "変更一覧は見つからなかった"


def test_records_unreadable_files_warn():
    files = _periods({"P/1期/変更一覧.csv": None}, "P", ["1期", "2期"])
    warn = [it for it in _pick(_items(files), "今ある記録", WARN) if "読めないファイル" in it.what]
    assert len(warn) == 1 and warn[0].examples == ["P/1期/変更一覧.csv"]


def test_build_settings_are_not_counted_and_the_report_says_so():
    files = _periods({"P/1期/app/pom.xml": "<project/>\n", "P/1期/ng/build.gradle": "def r = a / b\n"}, "P", ["1期", "2期"])
    rep = inspect_folder(list(files), files.get)
    assert not [it for it in rep.items if "ビルドの設定" in it.what or "Gradle" in it.what]
    assert rep.not_counted and "数えない" in rep.not_counted[0]
    assert "数えない" in to_markdown(rep, "r") and to_json(rep, "r")["not_counted"] == list(rep.not_counted)


# ---------------------------------------------------------------- 出力

def test_every_item_other_than_good_has_treatment_and_fix():
    for files in (_v2(), _v5(), _v9()):
        for it in _items(files):
            assert it.treatment and (it.result == GOOD or it.fix), it


def test_add_refuses_an_item_without_treatment_or_fix():
    rep = Report()
    with pytest.raises(ValueError):
        rep.add("決まり1", WARN, "", "x", "扱い", "")
    with pytest.raises(ValueError):
        rep.add("決まり1", BAD, "", "x", "", "直し方")
    assert not rep.items


def test_a_passed_periods_result_is_used_without_reading_again():
    files = _periods({}, "P", ["1期", "2期"])
    res = read_periods(list(files), files.get)

    def never(rel):
        raise AssertionError(rel)

    assert inspect_folder([], never, res).items == inspect_folder(list(files), files.get).items


def test_reports_are_written_where_asked_and_the_input_is_not_touched(tmp_path):
    files = {"README.md": "x\n"}
    _periods(files, "P", ["2期", "3期"])
    rep = inspect_folder(list(files), files.get)
    md, js = tmp_path / "r.md", tmp_path / "r.json"
    write_markdown(rep, "r", md)
    write_json(rep, "r", js)
    body = json.loads(js.read_text(encoding="utf-8"))
    assert body["root"] == "r" and set(body["summary"]) == {n for n, _ in RULES} and body == json.loads(json_text(rep, "r"))
    assert {"rule", "result", "path", "what", "treatment", "fix", "count", "examples"} <= set(body["items"][0])
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# フォルダの点検報告（r）") and "| 決まり | 合う | 注意 | 合わない |" in text
    assert "次の取り込み直しの版での扱い:" in text and "直し方:" in text and str(tmp_path) not in text
    assert summarize(rep.items) == body["summary"] and isinstance(rep.items[0], Item)
    assert files["README.md"] == "x\n"


def test_examples_are_capped_but_counts_are_complete():
    files = {f"f{i}.md": "x\n" for i in range(9)}
    _periods(files, "P", ["1期", "2期"])
    rep = inspect_folder(list(files), files.get)
    item = _pick(rep.items, "決まり1", WARN)[0]
    assert item.count == 9 and len(item.examples) == EXAMPLE_LIMIT
    assert "全 9 件、上限まで" in to_markdown(rep, "r")


# ---------------------------------------------------------------- 実際の形に近い木（V2・V5・V9 と同じ形）

def _v9() -> dict:
    rels = []
    rels += [f"倉庫管理/7期/src/Stock/{x}" for x in ("Audit/StockAudit.cs", "Import/LegacyStockImport.cs", "Import/StockCsvImport.cs",
                                                    "Model/StockItem.cs", "Model/StockLocation.cs", "Repo/StockRepository.cs",
                                                    "Service/StockService.cs", "Stock.csproj")]
    rels += ["倉庫管理/7期/設計/設計書.md"]
    rels += [f"倉庫管理/8期/src/Stock/{x}" for x in ("Audit/StockAudit.cs", "Model/StockItem.cs", "Service/StockService.cs", "Stock.csproj")]
    rels += [f"倉庫管理/9期/ソースコード/Stock/{x}" for x in ("Audit/StockAudit.cs", "Notice/StockNotifier.cs")]
    rels += [f"共通基盤/B01/{x}" for x in ("Shared/AppLog.cs", "Shared/Money.cs", "Shared/Shared.csproj", "Shared/Validator.cs", "テーブル一覧.csv")]
    rels += [f"共通基盤/B02/{x}" for x in ("Reporting/ReportWriter.cs", "Reporting/Reporting.csproj", "Shared/Money.cs", "Shared/Rounding.cs",
                                       "Shared/Shared.csproj", "テーブル一覧.csv")]
    rels += [f"出荷連携/基本設計/{p}/出荷設計書.md" for p in ("2021年度", "2022年度", "最新")]
    rels += [f"出荷連携/開発/2021年度/Ship/{x}" for x in ("Connect/Gateway.cs", "Fee/FeeCalculator.cs", "Model/ShipRecord.cs", "Service/ShipService.cs")]
    rels += [f"出荷連携/開発/2022年度/Ship/{x}" for x in ("Connect/Gateway.cs", "Fee/FeeCalculator.cs", "Lookup/ShipLookup.cs", "Service/ShipService.cs")]
    rels += [f"出荷連携/開発/最新/Ship/{x}" for x in ("Fee/FeeCalculator.cs", "Refund/RefundPolicy.cs", "Service/ShipService.cs", "Ship.csproj")]
    rels += [f"販売促進/10_新規/案件A/Promo/{x}" for x in ("Model/Campaign.cs", "Promo.csproj", "Report/CampaignReport.cs", "Rule/DiscountRule.cs",
                                                      "Service/CampaignService.cs")]
    rels += ["販売促進/10_新規/案件A/設計/設計書.md"]
    rels += [f"販売促進/20_更新/案件A/Promo/{x}" for x in ("Promo.csproj", "Report/ReportJob.cs", "Rule/DiscountRule.cs")]
    rels += ["販売促進/30_更新/案件A/Promo/Service/CampaignService.cs"]
    return {r: "x\n" for r in rels}


def _v5() -> dict:
    rels = [f"予約管理システム/改修/2期/{x}" for x in ("Booking/Model/Slot.cs", "Booking/Service/BookingService.cs", "Booking/docs/変更一覧.csv",
                                                  "Common/Money.cs", "Payment/Fee/FeeCalculator.cs", "Payment/docs/設計書.md", "Payment/変更一覧.csv")]
    rels += [f"予約管理システム/改修/3期/{x}" for x in ("Booking/Service/BookingService.cs", "Payment/Settle/Refund.cs")]
    rels += [f"予約管理システム/改修/4期/{x}" for x in ("Booking/Repo/ReservationRepository.cs", "Notify/Mail/MailSender.cs", "変更一覧.csv")]
    rels += [f"予約管理システム/本番資料/{x}" for x in ("Booking/Model/Slot.cs", "Booking/Repo/ReservationRepository.cs", "Booking/Service/BookingService.cs",
                                                  "Booking/docs/設計書.md", "Common/Money.cs", "Notify/Mail/MailSender.cs", "Payment/Fee/FeeCalculator.cs",
                                                  "Payment/docs/設計書.md", "ReservationSystem.sln")]
    texts = {r: "x\n" for r in rels}
    texts["予約管理システム/改修/4期/変更一覧.csv"] = _clist("4期", "2期")
    texts["予約管理システム/改修/2期/Payment/変更一覧.csv"] = _clist("2期", "本番資料")
    texts["予約管理システム/改修/2期/Booking/docs/変更一覧.csv"] = _clist("2期", "本番資料")
    return texts


def _v2() -> dict:
    rels = [f"基本設計/改修A/{x}" for x in ("出荷/設計書/SHP_設計書.md", "受注/設計書/ODR_設計書_A.md", "受注/設計書/ODR_設計書_B.md", "請求/設計書/BIL_設計書.md")]
    rels += ["基本設計/改修B/受注/設計書/ODR_設計書_B.md", "基本設計/改修C/受注/設計書/ODR_設計書_B.md"]
    rels += [f"開発/改修A/{x}" for x in ("共通/copy/CMNERR.cpy", "出荷/src/SHP301.cbl", "受注/ソース/ODR301.cbl", "請求/prog/BIL301.cbl")]
    rels += [f"開発/改修B/{x}" for x in ("出荷/src/SHP301.cbl", "受注/ソース/ODR302.cbl")]
    rels += ["開発/改修C/受注/ソース/ODR302.cbl", "開発/改修C/請求/prog/BIL302.cbl"]
    texts = {r: "x\n" for r in rels}
    for pj in ("出荷", "受注"):
        texts[f"基本設計/改修B/{pj}/変更一覧.csv"] = _clist("改修B", "改修A")
    return texts


def test_v9_near_slots_with_latest_are_bad():
    items = _items(_v9())
    bad = {it.path: it for it in _pick(items, "決まり2", BAD)}
    assert set(bad) == {"出荷連携/開発", "出荷連携/基本設計"}
    assert all(it.extra["unnumbered"] == ["最新"] for it in bad.values())
    assert [it.path for it in _pick(items, "決まり2", WARN)] == ["出荷連携"]
    assert {it.path for it in _pick(items, "決まり2", GOOD)} == {"倉庫管理", "共通基盤", "販売促進"}


def test_v9_spelling_variant_between_neighbour_periods():
    items = _items(_v9())
    warn = {tuple(it.extra.get("names", ())): it for it in _pick(items, "決まり3", WARN, "倉庫管理")}
    assert set(warn) == {("src", "ソースコード"), ()} and warn[()].examples == ["8期 → 9期"]
    assert not _pick(items, "決まり3", BAD)


def test_v9_without_change_lists_parents_come_from_the_order():
    items = _items(_v9())
    assert next(it.what for it in _pick(items, "今ある記録", GOOD)) == "変更一覧は見つからなかった"
    assert not _pick(items, "決まり4", BAD)
    assert {it.path for it in _pick(items, "決まり4", WARN) if it.extra.get("kind") == "order"} == {"倉庫管理", "共通基盤", "販売促進"}


def test_v5_inner_slot_is_bad_and_absent_from_rules_3_and_4():
    files = _v5()
    res = read_periods(list(files), files.get)
    items = inspect_folder(list(files), files.get, res).items
    assert set(res.slots) == {"予約管理システム", "予約管理システム/改修"}
    assert [it.path for it in _pick(items, "決まり2", GOOD)] == ["予約管理システム"]
    bad = _pick(items, "決まり2", BAD)
    assert [it.path for it in bad] == ["予約管理システム/改修"] and bad[0].extra["outer"] == "予約管理システム"
    assert "期のフォルダの高さが違う" in bad[0].treatment and "2期" in bad[0].treatment and "同じ高さに並べる" in bad[0].fix
    for rule in ("決まり3", "決まり4"):
        assert not [it for it in items if it.rule == rule and it.path == "予約管理システム/改修"]
    assert not _pick(items, "決まり3", GOOD) and not _pick(items, "決まり3", WARN)
    assert not any("2期" in e for it in _pick(items, "決まり4", WARN) + _pick(items, "決まり4", GOOD) for e in it.examples + [it.what])
    ignored = [it for it in _pick(items, "今ある記録", BAD) if "使わなかった" in it.what]
    assert len(ignored) == 1 and ignored[0].count == len(res.ignored_lists) == 3


def test_v2_period_folders_are_read_from_the_change_lists():
    items = _items(_v2())
    slots = {it.path: it for it in _pick(items, "決まり2", GOOD)}
    assert set(slots) == {"基本設計", "開発"} and all(it.extra["found_by"] == ["record"] for it in slots.values())
    unordered = {it.path: it for it in _pick(items, "決まり4", WARN) if "並びが決まらない" in it.what}
    assert set(unordered) == {"基本設計", "開発"} and unordered["開発"].extra["unnumbered"] == ["改修A", "改修B", "改修C"]
    assert [it.examples for it in _pick(items, "決まり4", GOOD, "基本設計") if it.extra.get("kind") == "list"] == [["改修B（出荷） → 改修A", "改修B（受注） → 改修A"]]
    assert {it.path for it in _pick(items, "決まり4", BAD)} == {"基本設計", "開発"}
