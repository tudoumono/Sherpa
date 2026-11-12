"""調査計画書: 目録の節のパーサ・計画書から回答への整形。
json（A）のときは従来の動作のまま、は `test_codex_ledger_gate.py` が見る。
"""
from __future__ import annotations

import pytest

from sherpa.providers.codex import plan_ledger as PL

pytestmark = pytest.mark.unit


def _plan(rows: str, *, persp: str = "- 観点A", review: str = "", conclusion: str = "", extra: str = "") -> str:
    return (
        "# 調査計画: 質問\n## 前提\n解釈メモ\n## 範囲\nフォルダ全体\n## 母集団\n" + rows
        + "\n## 分かったこと\nメモ\n## 観点\n" + persp + "\n## 見直し\n" + review
        + "\n## 結論\n" + conclusion + extra
    )


ROWS_OPEN = "- [ ] a · program · 対象A · 要: source\n- [ ] b · job · 対象B · 要: source, spec_doc\n"
ROWS_DONE = (
    "- [x] a · program · 対象A · 要: source\n      - source: src/a.cbl:12\n"
    "- [-] b · job · 対象B · 要: source\n      - 範囲外: ジョブ定義を探したが無かった\n"
)


# ---- パーサ ----

def test_parse_rows_marks_needs_and_subs():
    p = PL.parse_plan(_plan(ROWS_DONE))
    assert p.has_population
    assert [(r.id, r.mark, r.kind, r.subject) for r in p.rows] == [
        ("a", "x", "program", "対象A"), ("b", "-", "job", "対象B")]
    assert p.rows[0].needs == ("source",)
    assert p.rows[0].sources == ("src/a.cbl:12",)
    assert p.rows[1].reason == ("範囲外", "ジョブ定義を探したが無かった")


def test_parse_needs_multiple_and_perspectives():
    p = PL.parse_plan(_plan(ROWS_OPEN, persp="- 観点A\n- 観点B"))
    assert p.rows[1].needs == ("source", "spec_doc")
    assert p.perspectives == ("観点A", "観点B")
    assert [r.id for r in p.unstarted] == ["a", "b"]


def test_parse_missing_population_section():
    p = PL.parse_plan("# 調査計画: x\n## 前提\nだけ\n")
    assert not p.has_population and p.rows == ()
    assert PL.parse_plan(None).rows == () and PL.parse_plan("").has_population is False


def test_parse_broken_lines_are_kept_as_malformed_rows():
    p = PL.parse_plan(_plan("- [ ] だけ書いた行\n- [x] ok · k · s · 要: source\n- 記号の無い行\n- [?] 不明な記号\n"))
    assert len(p.rows) == 4
    assert all(p.rows[i].malformed for i in (0, 2, 3))
    assert p.rows[0].malformed and p.rows[0].mark == " "
    assert not p.rows[1].malformed


def test_sub_lines_attach_to_previous_row_only():
    p = PL.parse_plan(_plan("      - source: 孤立:1\n- [x] a · k · s · 要: source\n    - source: x.cbl:3\n    - 補足: 何か\n"))
    assert len(p.rows) == 2 and p.rows[0].malformed
    assert len(p.rows[1].subs) == 2


@pytest.mark.parametrize("word", ["範囲外", "読めない", "無い"])
def test_reason_words(word):
    p = PL.parse_plan(_plan(f"- [-] a · k · s · 要: source\n    - {word}: 試した内容\n"))
    assert p.rows[0].reason == (word, "試した内容")
    assert word in PL.unconfirmed_items(p)[0]["reason"] or PL.unconfirmed_items(p)[0]["reason"].startswith(
        PL._REASON_PHRASES[word])


def test_closed_row_without_reason_word_is_still_unconfirmed():
    p = PL.parse_plan(_plan("- [-] a · k · s · 要: source\n"))
    assert p.rows[0].malformed and p.unstarted
    assert "読み取れず" in PL.unconfirmed_items(p)[0]["reason"]


def test_review_caps_by_depth():
    assert [PL.review_cap_for(p) for p in ("quick", "standard", "deep", "max")] == [0, 1, 3, 6]
    assert PL.review_cap_for(None) == 1


# ---- 回答の整形 ----

def test_unconfirmed_includes_source_less_done_row():
    p = PL.parse_plan(_plan("- [x] a · k · 対象A · 要: source\n- [x] b · k · 対象B · 要: spec_doc\n"))
    items = PL.unconfirmed_items(p)
    assert [i["id"] for i in items] == ["a"]


def test_compose_order_strips_id_tags_and_lists_unconfirmed():
    plan = PL.parse_plan(_plan(
        ROWS_DONE + "- [ ] c · program · 対象C · 要: source\n",
        conclusion="Aが影響を受ける [a]。Bは未確認 [b]。配列 [x] は残る。"))
    out = PL.compose_answer(plan, "本文\n参照した資料:\n- src/z.cbl")
    assert out.index("Aが影響を受ける") < out.index("参照した資料:")
    assert "[a]" not in out and "[b]" not in out and "[x]" in out
    # 確認の状況は本文に入れず、回答の補足の欄と調査の記録へ渡す（`turn_report` と同じ `plan_gaps`）
    assert "## 調べた範囲" not in out and "対象C" not in out and "母集団" not in out
    _, unverified = PL.plan_gaps(plan)
    assert {"item": "対象C", "reason": "調べ終わっていません"} in [{k: u[k] for k in ("item", "reason")} for u in unverified]
    assert out.rstrip().endswith("- src/a.cbl:12\n- src/z.cbl")


def test_compose_fallback_leaves_premise_scope_and_format_issues_out_of_the_body():
    rows = ("- [ ] a · program · 対象A · 要: source\n- [x] b · program · 対象B · 要: source\n"
            "- [x] c · program · 対象C\n- [-] d · job · 対象D · 要: source\n    - 無い: 探したが無し\n")
    plan = PL.parse_plan("## 前提\n仮定の写し\n## 範囲\n範囲の写し\n## 母集団\n" + rows + "## 結論\n結論です [d]。\n")
    out = PL.compose_answer(plan)
    assert "仮定の写し" not in out and "範囲の写し" not in out and "## 前提" not in out
    assert "## 調べた範囲" not in out and "対象" not in out.replace("結論", "")  # 確認の状況も記載の不備の行も本文に出さない
    fmt, unverified = PL.plan_gaps(plan)
    assert [u["item"] for u in unverified] == ["対象A", "対象D"] and len(fmt) == 2


def test_compose_falls_back_to_codex_body_without_conclusion():
    plan = PL.parse_plan(_plan(ROWS_DONE))
    out = PL.compose_answer(plan, "Codex の回答\n参照した資料:\n- src/a.cbl")
    assert out.startswith("未確認: Codex の回答")
    assert out.count("src/a.cbl") == 1


@pytest.mark.parametrize("rows", [
    "- [x] a · k · 対象 · 要: source\n- [x] a · k · 対象 · 要: source\n",
    "- [-] a · k · 対象 · 要: source\n    - 無い: \n",
    "- [x] a · k · 対象\n",
])
def test_invalid_population_is_treated_as_unconfirmed(rows):
    plan = PL.parse_plan(_plan(rows))
    assert plan.unstarted and PL.unconfirmed_items(plan)


def test_missing_source_is_marked_unconfirmed_in_the_conclusion_without_rewriting():
    plan = PL.parse_plan(_plan("- [x] a · k · 対象A · 要: source\n", conclusion="対象Aの記述です [a]。"))
    assert PL.compose_answer(plan).startswith("未確認: 対象Aの記述です。")


@pytest.mark.parametrize("source,valid", [
    (":12", False),
    ("`   :12`", False),
    ("src/a.cbl:12", True),
    ("資料/仮ソース.cbl:12-15", True),
    ("`資料/仮ソース.cbl:12,16`", True),
])
def test_source_requires_nonblank_path_before_line_reference(source, valid):
    plan = PL.parse_plan(_plan(
        f"- [x] a · k · 対象A · 要: source\n    - source: {source}\n", conclusion="対象Aの記述です [a]。"))
    assert plan.rows[0].sources == ((source,) if valid else ())
    assert bool(PL.unconfirmed_items(plan)) is not valid
    assert PL.compose_answer(plan).startswith("未確認:") is not valid


@pytest.mark.parametrize("path", [" ", "資料/仮ソース.cbl"])
def test_source_with_backticks_around_only_the_path(path):
    source = f"`{path}`:12"
    plan = PL.parse_plan(_plan(
        f"- [x] a · k · 対象A · 要: source\n    - source: {source}\n", conclusion="対象Aの記述です [a]。"))
    assert PL._path_of(source) == path.strip()
    assert bool(plan.rows[0].sources) == bool(path.strip())
    assert PL.compose_answer(plan).startswith("未確認:") == (not path.strip())


@pytest.mark.parametrize("conclusion,expected", [
    ("対象Aの記述です。", "未確認: 対象Aの記述です。"),
    ("対象Aの記述です [unknown]。", "未確認: 対象Aの記述です [unknown]。"),
    ("対象Aの記述です [a]。", "対象Aの記述です。"),
    ("対象Bの記述です [b]。", "未確認: 対象Bの記述です。"),
    ("対象の記述です [a] [b]。", "未確認: 対象の記述です。"),
])
def test_conclusion_requires_a_known_confirmed_id(conclusion, expected):
    plan = PL.parse_plan(_plan(ROWS_DONE, conclusion=conclusion))
    assert PL.compose_answer(plan).split("\n\n参照した資料:")[0] == expected


def test_conclusion_formatting_does_not_mask_unreferenced_claims():
    conclusion = "### 小見出し\n\n---\n```text\n根拠を付けていない記述。\n```\n- 対象Aです [a]。"
    plan = PL.parse_plan(_plan(ROWS_DONE, conclusion=conclusion))
    assert PL.compose_answer(plan).startswith(
        "### 小見出し\n\n---\n```text\n未確認: 根拠を付けていない記述。\n```\n- 対象Aです。")


@pytest.mark.parametrize("link", ["file", "directory"])
def test_read_plan_does_not_read_through_symlinks(tmp_path, link):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "plan.md").write_text(_plan(ROWS_DONE))
    investigation = tmp_path / "investigation"
    if link == "directory":
        investigation.symlink_to(outside, target_is_directory=True)
    else:
        investigation.mkdir()
        (investigation / "plan.md").symlink_to(outside / "plan.md")
    assert PL.read_plan(investigation) is None



def test_plan_mcp_toolset_does_not_expose_or_accept_json_ledger_commands(monkeypatch):
    from sherpa import mcp_server as mcp
    monkeypatch.setenv("SHERPA_MCP_TOOLSET", "plan")
    monkeypatch.setattr(mcp.es_index, "available", lambda: False)
    tools = mcp._tool_defs()
    assert not any(t["name"].startswith("ledger_") for t in tools)
    assert all("item" not in t["inputSchema"].get("properties", {}) for t in tools)
    response = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "ledger_status", "arguments": {}}})
    assert response["result"]["isError"]


# ---- 根拠の行（1 行に複数の範囲・複数のファイル） ----

@pytest.mark.parametrize("value,expected", [
    ("a.py:105-118, 147-158; b.py:1070-1077", ("a.py:105-118, 147-158", "b.py:1070-1077")),
    ("sherpa/x.py:696-701, 884-885, 908-912", ("sherpa/x.py:696-701, 884-885, 908-912",)),
    ("a.py:1；b.py:2,3", ("a.py:1", "b.py:2,3")),
    ("`a.py:1-2`; `b.py:3`", ("`a.py:1-2`", "`b.py:3`")),
    ("a.py:1; 壊れた; :5", ("a.py:1",)),
    ("a.py:12-", ()),
    ("行番号なし", ()),
])
def test_source_line_reads_several_ranges_and_files(value, expected):
    plan = PL.parse_plan(_plan(f"- [x] a · k · 対象A · 要: source\n    - source: {value}\n"))
    assert plan.rows[0].sources == expected
    assert bool(PL.unconfirmed_items(plan)) is not bool(expected)


def test_compose_lists_paths_with_lines_merged_per_file():
    plan = PL.parse_plan(_plan(
        "- [x] a · k · 対象A · 要: source\n    - source: x.py:696-701, 884-885; y.py:1\n"
        "- [x] b · k · 対象B · 要: source\n    - source: x.py:884-885, 908-912\n",
        conclusion="Aです [a]。Bです [b]。"))
    out = PL.compose_answer(plan, "本文\n参照した資料:\n- x.py\n- z.py")
    assert out.rstrip().endswith("参照した資料:\n- x.py:696-701, 884-885, 908-912\n- y.py:1\n- z.py")
    assert not out.startswith("未確認:")


# ---- モデルの回答を本文に残す整形 ----

_MODEL = ("影響するプログラムは次の通りです。\n- src/a.cbl 受付\n- src/q.cbl 集計\n\n"
          "参照した資料:\n- src/q.cbl:5\n- src/a.cbl:12")


def test_model_answer_stays_as_body_without_plan_supplement():
    plan = PL.parse_plan(_plan(ROWS_DONE, conclusion="短い結論 [a]。src/only.cbl も関係。"))
    out, info = PL.compose_with_model_answer(plan, _MODEL)
    assert out == _MODEL
    assert "調べた範囲" not in out and "未確認:" not in out and "短い結論" not in out
    assert info["plan_only_paths"] == ["src/only.cbl"]
    assert info["refs_added_from_plan"] == 0


def test_plan_row_ids_are_removed_from_the_model_body_but_other_brackets_stay():
    plan = PL.parse_plan(_plan(ROWS_DONE, conclusion="結論 [a]。"))
    out, _ = PL.compose_with_model_answer(plan, "Aは受付です [a]。配列は x[0] と [注] を使う [b]。\n\n参照した資料:\n- src/a.cbl:12")
    assert out.startswith("Aは受付です [a]。配列は x[0] と [注] を使う [b]。\n\n参照した資料:")


def test_compose_keeps_a_bracketed_value_that_equals_a_row_id():
    plan = PL.parse_plan(_plan("- [x] A1 · k · 対象A · 要: source\n    - source: src/a.cbl:1\n"))
    out, _ = PL.compose_with_model_answer(plan, "状態 [A1] は保留を表します。")
    assert out == "状態 [A1] は保留を表します。\n\n参照した資料:\n- src/a.cbl:1"


def test_supplement_gaps_are_kept_in_the_info_not_in_the_body():
    rows = ("- [ ] a · program · 対象A · 要: source\n- [x] b · program · 対象B · 要: source\n"
            "- [x] c · program · 対象C\n- [-] d · job · 対象D · 要: source\n    - 無い: 探したが無し\n")
    plan = PL.parse_plan(_plan(rows))
    out, info = PL.compose_with_model_answer(plan, "本文です。")
    assert out == "本文です。"
    assert [x["id"] for x in info["unverified_items"]] == ["a", "d"]
    assert [x["id"] for x in info["format_issues"]] == ["b", "c"]


def test_turn_report_counts_format_issues_as_unconfirmed_in_the_answer_counts_and_items(tmp_path):
    rows = ("- [ ] a · program · 対象A · 要: source\n- [x] b · program · 対象B · 要: source\n"
            "    - source: src/b.cbl:1\n- [x] c · program · 対象C · 要: source\n"
            "- [-] d · job · 対象D · 要: source\n    - 無い: 探したが無し\n- [x] 壊れた行\n")
    st = _St(tmp_path, "本文です。", _plan(rows))
    st._plan_gate = None
    st._investigation_stopped_reason = None
    st._plan_final = PL.read_plan(st._investigation_dir)
    report = PL.turn_report(st)
    # 記載の不備（c: source 欠落・壊れた行: 形式不正）も、全行の件数と確認できなかった項目に入れ、調査の記録にも残す
    assert report["plan_counts"] == {"total": 5, "confirmed": 1, "unconfirmed": 3, "unstarted": 1}
    assert [x["item"] for x in report["plan_unverified_items"]] == ["対象A", "対象C", "対象D", "壊れた行"]
    assert {x["id"] for x in report["plan_format_issues"]} >= {"c"} and len(report["plan_format_issues"]) == 2
    assert report["plan_unverified_items"] == report["unconfirmed_items"]


def test_turn_report_counts_a_source_less_done_row_among_two_needing_source(tmp_path):
    rows = "- [x] a · k · 対象A · 要: source\n    - source: src/a.cbl:1\n- [x] b · k · 対象B · 要: source\n"
    st = _St(tmp_path, "本文です。", _plan(rows))
    st._plan_gate = None
    st._investigation_stopped_reason = None
    st._plan_final = PL.read_plan(st._investigation_dir)
    report = PL.turn_report(st)
    assert report["plan_counts"] == {"total": 2, "confirmed": 1, "unconfirmed": 1, "unstarted": 0}
    assert [x["item"] for x in report["plan_unverified_items"]] == ["対象B"]


def test_turn_report_does_not_count_body_text_lines_as_items(tmp_path):
    rows = "対象は次の 2 件。\n- [x] a · k · 対象A · 要: source\n    - source: src/a.cbl:1\n- [x] b · k · 対象B · 要: source\n"
    st = _St(tmp_path, "本文です。", _plan(rows))
    st._plan_gate = None
    st._investigation_stopped_reason = None
    st._plan_final = PL.read_plan(st._investigation_dir)
    report = PL.turn_report(st)
    assert report["plan_counts"] == {"total": 2, "confirmed": 1, "unconfirmed": 1, "unstarted": 0}
    assert [x["item"] for x in report["plan_unverified_items"]] == ["対象B"]


def test_ids_that_look_like_indexes_links_or_code_are_not_removed():
    ids = {"0", "a", "A1", "A2"}
    f = PL._strip_id_tags
    assert f("x[0] を使う [0]。", ids) == "x[0] を使う。"             # 添字は残し、独立した印だけ外す
    assert f("`x[0] [a]` とあります [a]。", ids) == "`x[0] [a]` とあります。"   # インラインコードの中は対象外
    assert f("``a `[a]` b`` です [A1][A2]。", ids) == "``a `[a]` b`` です。"
    assert f("[a](http://example.com) と [a][ref] と m[a] と f(x)[a]", ids) == "[a](http://example.com) と [a][ref] と m[a] と f(x)[a]"
    assert f("文末です[a]\n", ids) == "文末です\n"                      # 日本語の直後は独立した印
    # 数字だけの id は日本語の直後でも添字として残し、英字を含む id は日本語の直後でも外す
    assert f("値[0] と 注記 [0]。", ids) == "値[0] と 注記。" and f("x[0]", ids) == "x[0]"
    assert f("である[A1]。", ids) == "である。" and f("である[a]。", ids) == "である。"
    assert f("[A1]", ids) == "" and f("説明 [A1] [A2]\n次 [zz]", ids) == "説明\n次 [zz]"
    fenced = "前 [a]\n```py\nx[0] = [a]\n```\n後 [a]\n"
    assert f(fenced, ids) == "前\n```py\nx[0] = [a]\n```\n後\n"
    assert f("~~~\n[a]\n```\n[a]\n~~~\n[a]", ids) == "~~~\n[a]\n```\n[a]\n~~~\n"


def test_fences_inside_list_items_and_link_definitions_without_a_space_stay():
    text = "- 例 [a]\n    ```md\n    参照した資料:\n    [a]\n    ```\n\n詳細は [b]。\n\n[b]:https://example.com\n"
    assert PL._strip_id_tags(text, {"a", "b"}) == (
        "- 例\n    ```md\n    参照した資料:\n    [a]\n    ```\n\n詳細は [b]。\n\n[b]:https://example.com\n")


def test_link_definitions_inside_code_blocks_do_not_protect_ids():
    assert PL._strip_id_tags("本文 [a]。\n```md\n[a]:https://example.com\n```\n", {"a"}) == "本文。\n```md\n[a]:https://example.com\n```\n"


def test_link_reference_definitions_stay():
    assert PL._strip_id_tags("詳細は[資料][a]。\n\n[a]: https://example.com\n", {"a"}) == "詳細は[資料][a]。\n\n[a]: https://example.com\n"
    assert PL._strip_id_tags("詳細は [a] を参照 [b]。\n\n[a]: https://example.com\n", {"a", "b"}) == (
        "詳細は [a] を参照。\n\n[a]: https://example.com\n")                 # 定義のある名前の [a] はリンクとして残す
    assert PL._strip_id_tags("詳細は [A]。\n\n[a]: https://example.com\n", {"A"}) == "詳細は [A]。\n\n[a]: https://example.com\n"


def test_task_list_checkboxes_stay_when_a_plan_id_is_x():
    ids = {"x", "a"}
    f = PL._strip_id_tags
    assert f("- [x] 対象の確認 [a]\n- [ ] 残り\n1. [X] 済み\n", ids) == "- [x] 対象の確認\n- [ ] 残り\n1. [X] 済み\n"
    assert f("文の途中の [x] は印として外す。", ids) == "文の途中の は印として外す。"
    assert f("> - [x] 引用の中の項目 [a]\n>> 1. [ ] 入れ子\n", ids) == "> - [x] 引用の中の項目\n>> 1. [ ] 入れ子\n"
    code = '前 [a]\n\n    print("[a]")\n\n    x = [a]\n後 [a]\n'
    assert f(code, ids) == '前\n\n    print("[a]")\n\n    x = [a]\n後\n'          # 空行の後の字下げはコードの塊
    assert f("- 項目 [a]\n    - 子の項目 [a]\n", ids) == "- 項目\n    - 子の項目\n"   # 箇条書きの続きの字下げは本文
    quoted = "> 引用 [a]\n> ```md\n> [a]\n> ```\n>\n>     x = [a]\n> 後 [a]\n"
    assert f(quoted, ids) == "> 引用\n> ```md\n> [a]\n> ```\n>\n>     x = [a]\n> 後\n"    # 引用の中のコードも触らない


def test_model_body_keeps_brackets_when_a_plan_id_is_numeric():
    plan = PL.parse_plan(_plan("- [x] 0 · k · 対象 · 要: source\n    - source: src/a.cbl:1\n", conclusion="結論 [0]。"))
    out, _ = PL.compose_with_model_answer(plan, "配列は x[0] です [0]。\n\n参照した資料:\n- src/a.cbl:1")
    assert out.startswith("配列は x[0] です [0]。")


@pytest.mark.parametrize("text", ["", "## 母集団\n- [x] 壊れた行\n", "# 見出しだけ\n"])
def test_empty_or_broken_plan_keeps_model_answer(text):
    plan = PL.parse_plan(text)
    if not plan.rows:
        return  # 呼び出し側（compose_turn_answer）が何もしない。本文はそのまま
    out, info = PL.compose_with_model_answer(plan, _MODEL)
    assert out.startswith(_MODEL.split("\n\n参照した資料:")[0])
    assert info["format_issues"]


def test_last_block_is_references_with_plan_refs_appended_without_duplicates():
    plan = PL.parse_plan(_plan(
        "- [x] a · k · 対象A · 要: source\n    - source: src/a.cbl:12; src/a.cbl:30; src/n.cbl:1\n"))
    out, info = PL.compose_with_model_answer(plan, _MODEL)
    assert out.endswith("参照した資料:\n- src/q.cbl:5\n- src/a.cbl:12\n- src/a.cbl:30\n- src/n.cbl:1")
    assert out.count("参照した資料:") == 1 and info["refs_added_from_plan"] == 2


def test_plan_refs_make_a_block_when_the_model_has_none():
    plan = PL.parse_plan(_plan("- [x] a · k · 対象A · 要: source\n    - source: src/a.cbl:12\n"))
    out, _ = PL.compose_with_model_answer(plan, "本文です。")
    assert out.endswith("参照した資料:\n- src/a.cbl:12")


def test_text_after_the_model_block_moves_before_the_references():
    plan = PL.parse_plan(_plan(ROWS_DONE))
    out, _ = PL.compose_with_model_answer(plan, "本文。\n\n参照した資料:\n- src/a.cbl:12\n\n注意書きです。")
    assert out.index("注意書きです。") < out.rindex("参照した資料:")


class _St:
    def __init__(self, tmp_path, answer, plan_text):
        d = tmp_path / "inv"
        d.mkdir(exist_ok=True)
        if plan_text is not None:
            (d / "plan.md").write_text(plan_text, encoding="utf-8")
        self._ledger_style, self._investigation_dir, self.answer = "plan_main", d, answer
        self._plan_final = self._compose_stats = None


def test_turn_compose_falls_back_to_conclusion_only_when_model_answer_is_empty(tmp_path):
    st = _St(tmp_path, "  ", _plan(ROWS_DONE, conclusion="結論の文です。"))
    PL.compose_turn_answer(st)
    assert st.answer.startswith("未確認: 結論の文です。")
    assert st._compose_stats["body_source"] == "plan_conclusion_fallback" and st._compose_stats["applied"]


def test_turn_compose_leaves_answer_when_plan_is_missing_or_has_no_rows(tmp_path):
    for plan_text in (None, "# 母集団なし\n"):
        st = _St(tmp_path, "モデルの回答", plan_text)
        PL.compose_turn_answer(st)
        assert st.answer == "モデルの回答"
        assert st._compose_stats["applied"] is False and st._compose_stats["body_source"] == "unchanged"


def test_plan_only_paths_are_capped_at_fifty(tmp_path):
    rows = "".join(f"- [x] r{i} · k · 対象{i} · 要: source\n    - source: src/p{i}.cbl:1\n" for i in range(60))
    st = _St(tmp_path, "本文です。", _plan(rows))
    PL.compose_turn_answer(st)
    assert len(st._compose_stats["plan_only_paths"]) == 50


# ---- グラフの結果の記録 ----

ROWS_CLOSED = ROWS_DONE


def test_graph_incomplete_reads_graph_impact_rows_only_from_the_call_log():
    class Merged:
        graph = [{"tool": "graph_resolve", "complete": False},
                 {"tool": "graph_impact", "complete": True},
                 {"tool": "graph_impact", "complete": False, "depth_truncated": True}]

    class St:
        call_log = Merged()
        _call_log_dir = None
        turn_uid = "t"

    assert PL._graph_stats(St()) == {"impact_calls": 2, "impact_incomplete": 1, "impact_depth_truncated": 1}
    Merged.graph = [{"tool": "graph_impact", "complete": True}]
    assert PL._graph_stats(St())["impact_incomplete"] == 0
    St.call_log = None
    assert PL._graph_stats(St())["impact_calls"] == 0


def test_gate_stats_lists_closed_rows_with_reason_and_detail():
    plan = PL.parse_plan(_plan(ROWS_CLOSED + "- [-] c · k · 対象C · 要: source\n"))
    gate = PL._gate_stats(plan, {"impact_incomplete": 1})
    assert gate["accepted_with_graph_incomplete"] is True
    assert gate["closed_rows"] == [{"id": "b", "reason": "範囲外", "detail": "ジョブ定義を探したが無かった"},
                                   {"id": "c", "reason": None, "detail": ""}]


# ---- 表記の正規化 ----

def _row_of(line: str, subs: str = ""):
    return PL.parse_plan(_plan(line + "\n" + subs)).rows[0]


def test_bracketed_id_is_normalized_and_recorded():
    plan = PL.parse_plan(_plan("- [x] [cb-01] · program · 対象A · 要: source\n    - source: src/a.cbl:1\n"))
    r = plan.rows[0]
    assert r.id == "cb-01" and not r.malformed and r.mark == "x" and r.sources == ("src/a.cbl:1",)
    assert plan.normalizations == [{"original": "- [x] [cb-01] · program · 対象A · 要: source",
                                    "kinds": ["id_brackets"],
                                    "normalized": "- [x] cb-01 · program · 対象A · 要: source"}]


def test_three_field_row_gets_a_fixed_kind_mark_without_changing_the_subject():
    plan = PL.parse_plan(_plan("- [x] a1 · 対象A · 要: spec_doc\n"))
    r = plan.rows[0]
    assert (r.id, r.kind, r.subject, r.needs) == ("a1", "（未記入）", "対象A", ("spec_doc",))
    assert not r.malformed and plan.normalizations[0]["kinds"] == ["kind_missing"]


def test_normalization_never_confirms_unstarted_or_invents_sources():
    plan = PL.parse_plan(_plan("- [ ] [u1] · 対象U · 要: source\n- [x] [d1] · 対象D · 要: source\n"))
    u, d = plan.rows
    assert u.mark == " " and not u.malformed and u in plan.unstarted
    assert d.mark == "x" and d.sources == ()
    assert [x["item"] for x in PL.unconfirmed_items(plan)] == ["対象U", "対象D"]


def test_unreadable_rows_are_not_normalized():
    for line in ("- [x] a1 · 対象A\n", "- [x] a b · k · 対象 · 要: source\n", "- [x] [a1 · k · 対象 · 要: source\n",
                 "- [x] a1 · k · 対象 · source\n"):
        plan = PL.parse_plan(_plan(line))
        assert plan.rows[0].malformed and not plan.normalizations
    # 理由の無い [-] は正規化しても書式不正のまま
    plan = PL.parse_plan(_plan("- [-] [x9] · 対象 · 要: source\n"))
    assert plan.rows[0].id == "x9" and plan.rows[0].malformed and plan.normalizations


def test_unknown_id_in_sub_lines_is_not_attached_to_other_rows():
    plan = PL.parse_plan(_plan("- [x] [a1] · k · 対象A · 要: source\n    - source: src/a.cbl:1\n- [x] b1 · k · 対象B · 要: source\n"))
    assert plan.rows[1].sources == () and plan.rows[0].sources == ("src/a.cbl:1",)


def test_reference_heading_inside_a_code_block_is_not_moved_by_composition():
    plan = PL.parse_plan(_plan(ROWS_DONE, conclusion="結論 [a]。"))
    model = "説明。\n```text\n参照した資料:\n- missing.md\n```\n続き。\n\n参照した資料:\n- src/a.cbl:12"
    out, _ = PL.compose_with_model_answer(plan, model)
    assert out.startswith("説明。\n```text\n参照した資料:\n- missing.md\n```\n続き。")
