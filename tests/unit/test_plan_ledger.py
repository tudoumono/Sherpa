"""評価用の調査計画書（`SHERPA_LEDGER_STYLE`）: 目録の節のパーサ・plan_gate の判定表・計画書から回答への整形。
json（既定）のときは従来の動作のまま、は `test_codex_ledger_gate.py` が見る。
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
    assert "理由が不正" in PL.unconfirmed_items(p)[0]["reason"]


# ---- 進捗 ----

def test_progressed_by_mark_sub_count_and_new_rows():
    base = PL.parse_plan(_plan(ROWS_OPEN))
    assert PL.progressed(None, base)
    assert not PL.progressed(base, PL.parse_plan(_plan(ROWS_OPEN)))
    marked = PL.parse_plan(_plan("- [x] a · program · 対象A · 要: source\n- [ ] b · job · 対象B · 要: source\n"))
    assert PL.progressed(base, marked)
    more_subs = PL.parse_plan(_plan(ROWS_OPEN + "    - 補足: x\n"))
    assert PL.progressed(base, more_subs)
    more_rows = PL.parse_plan(_plan(ROWS_OPEN + "- [ ] c · k · s · 要: source\n"))
    assert PL.progressed(base, more_rows)


# ---- 判定表（plan_gate） ----

def _decide(state, text, cap=1):
    return PL.decide(state, PL.parse_plan(text) if text is not None else None, review_cap=cap)


def test_gate_missing_section_rejects_then_gives_up():
    s = PL.PlanGateState()
    assert _decide(s, None).action == "reject_missing"
    assert _decide(s, "## 前提\nだけ").action == "reject_missing"
    d = _decide(s, None)
    assert d.action == "accept" and d.reason == "plan_missing"


def test_gate_unstarted_with_progress_rejects_and_names_ids():
    s = PL.PlanGateState()
    d = _decide(s, _plan(ROWS_OPEN))
    assert d.action == "reject_unstarted" and "a、b" in d.prompt


def test_gate_unstarted_without_progress_accepts():
    s = PL.PlanGateState()
    _decide(s, _plan(ROWS_OPEN))
    d = _decide(s, _plan(ROWS_OPEN))
    assert d.action == "accept" and d.reason == "no_progress"


def test_gate_all_done_asks_review_then_accepts_when_nothing_added():
    s = PL.PlanGateState()
    d = _decide(s, _plan(ROWS_DONE))
    assert d.action == "review" and s.review_requests == 1
    d = _decide(s, _plan(ROWS_DONE, review="- 1: 追加なし"))
    assert d.action == "accept" and d.reason == "complete" and s.review_rounds == 0


def test_gate_review_that_adds_row_continues_then_reviews_again():
    s = PL.PlanGateState()
    assert _decide(s, _plan(ROWS_DONE), cap=3).action == "review"
    grown = _plan(ROWS_DONE + "- [ ] c · k · s · 要: source\n")
    assert _decide(s, grown, cap=3).action == "reject_unstarted"
    assert s.review_rounds == 1
    finished = _plan(ROWS_DONE + "- [x] c · k · s · 要: source\n    - source: c.cbl:1\n")
    assert _decide(s, finished, cap=3).action == "review"
    assert s.review_requests == 2


def test_gate_review_that_adds_only_a_perspective_counts_as_growth():
    s = PL.PlanGateState()
    _decide(s, _plan(ROWS_DONE), cap=3)
    d = _decide(s, _plan(ROWS_DONE, persp="- 観点A\n- 観点B"), cap=3)
    assert s.review_rounds == 1 and d.action == "review"


def test_gate_cap_reached_accepts_even_if_review_grew():
    s = PL.PlanGateState()
    _decide(s, _plan(ROWS_DONE), cap=1)
    grown_done = _plan(ROWS_DONE + "- [x] c · k · s · 要: source\n    - source: c.cbl:1\n")
    d = _decide(s, grown_done, cap=1)
    assert d.action == "accept" and d.reason == "review_cap"


def test_gate_quick_has_no_review():
    s = PL.PlanGateState()
    d = _decide(s, _plan(ROWS_DONE), cap=0)
    assert d.action == "accept" and d.reason == "complete"


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
    assert out.index("Aが影響を受ける") < out.index("## 前提") < out.index("## 調べた範囲") < out.index("参照した資料:")
    assert "[a]" not in out and "[b]" not in out and "[x]" in out
    assert "- 対象B（範囲外のため確認できませんでした: ジョブ定義を探したが無かった）" in out
    assert "- 対象C（調べ終わっていません）" in out
    assert "母集団 3 件（program 2 件・job 1 件）" in out
    assert out.rstrip().endswith("- src/a.cbl:12\n- src/z.cbl")


def test_compose_falls_back_to_codex_body_without_conclusion():
    plan = PL.parse_plan(_plan(ROWS_DONE))
    out = PL.compose_answer(plan, "Codex の回答\n参照した資料:\n- src/a.cbl")
    assert out.startswith("未確認: Codex の回答")
    assert out.count("src/a.cbl") == 1


def test_style_env(monkeypatch):
    monkeypatch.delenv(PL.STYLE_ENV, raising=False)
    assert PL.ledger_style() == "json"
    monkeypatch.setenv(PL.STYLE_ENV, "plan_gate")
    assert PL.ledger_style() == "plan_gate"
    monkeypatch.setenv(PL.STYLE_ENV, "bogus")
    assert PL.ledger_style() == "json"


@pytest.mark.parametrize("rows", [
    "- [x] a · k · 対象 · 要: source\n- [x] a · k · 対象 · 要: source\n",
    "- [-] a · k · 対象 · 要: source\n    - 無い: \n",
    "- [x] a · k · 対象\n",
])
def test_invalid_population_is_not_accepted_as_complete(rows):
    plan = PL.parse_plan(_plan(rows))
    assert plan.unstarted and PL.unconfirmed_items(plan)  # 未確認の扱いは変えない
    state = PL.PlanGateState()
    assert PL.decide(state, plan, review_cap=1).action == "review"  # 書式不正だけでは未着手の差し戻しをしない
    assert state.format_warnings == 1 and state.continuations == 0


def test_same_number_of_updated_details_is_progress_and_review_identity_can_grow():
    before = PL.parse_plan(_plan(ROWS_OPEN + "    - 補足: 最初の調査\n"))
    after = PL.parse_plan(_plan(ROWS_OPEN + "    - 補足: 別の根拠を調査\n"))
    assert PL.progressed(before, after)
    state = PL.PlanGateState()
    PL.decide(state, PL.parse_plan(_plan(ROWS_DONE)), review_cap=3)
    changed = PL.parse_plan(_plan(ROWS_DONE.replace("b · job · 対象B", "c · job · 対象C")))
    assert PL.decide(state, changed, review_cap=3).action == "review"
    assert state.review_rounds == 1 and state.review_requests == 2


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
    assert PL.compose_answer(plan).split("\n\n## 前提")[0] == expected


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


@pytest.mark.parametrize("plain,ollama,level", [(True, False, 2), (False, True, 2), (False, False, 0), (False, False, 1)])
def test_plan_style_does_not_override_plain_ollama_or_old_schema(monkeypatch, plain, ollama, level):
    monkeypatch.setenv(PL.STYLE_ENV, "plan_main")
    assert PL.effective_style(plain=plain, ollama=ollama, schema_level=level) == "json"


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


def test_model_answer_stays_as_body_with_plan_as_supplement():
    plan = PL.parse_plan(_plan(ROWS_DONE, conclusion="短い結論 [a]。src/only.cbl も関係。"))
    out, info = PL.compose_with_model_answer(plan, _MODEL)
    body = _MODEL.split("\n\n参照した資料:")[0]
    assert out.startswith(body + "\n\n## 調べた範囲（調査計画書）")
    assert "未確認:" not in out and "短い結論" not in out
    assert out.index("## 調べた範囲") < out.index("参照した資料:")
    assert "確認済み 1 件・確認できなかった 1 件・未着手 0 件" in out
    assert "### 確認できなかった項目\n- 対象B（範囲外のため確認できませんでした: ジョブ定義を探したが無かった）" in out
    assert "計画書の記載の不備" not in out
    assert info["plan_only_paths"] == ["src/only.cbl"] and info["refs_added_from_plan"] == 0


def test_supplement_splits_unverified_and_format_issues():
    rows = ("- [ ] a · program · 対象A · 要: source\n- [x] b · program · 対象B · 要: source\n"
            "- [x] c · program · 対象C\n- [-] d · job · 対象D · 要: source\n    - 無い: 探したが無し\n")
    plan = PL.parse_plan(_plan(rows))
    out, info = PL.compose_with_model_answer(plan, "本文です。")
    assert "### 確認できなかった項目\n- 対象A（調べ終わっていません）\n- 対象D（" in out
    assert "### 計画書の記載の不備\n- 対象B（" in out and "- 対象C（" in out
    assert [x["id"] for x in info["unverified_items"]] == ["a", "d"]
    assert [x["id"] for x in info["format_issues"]] == ["b", "c"]
    assert "未確認" not in out


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


def test_text_after_the_model_block_moves_before_the_supplement():
    plan = PL.parse_plan(_plan(ROWS_DONE))
    out, _ = PL.compose_with_model_answer(plan, "本文。\n\n参照した資料:\n- src/a.cbl:12\n\n注意書きです。")
    assert out.index("注意書きです。") < out.index("## 調べた範囲") < out.rindex("参照した資料:")


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


# ---- グラフの打ち切りを範囲外として受け付けない ----

ROWS_CLOSED = ROWS_DONE


def _decide_graph(state, text, incomplete, cap=1, continue_cap=PL.PLAN_CONTINUE_CAP):
    return PL.decide(state, PL.parse_plan(text), review_cap=cap, continue_cap=continue_cap,
                     graph_incomplete=incomplete)


def test_gate_rejects_closed_rows_once_when_graph_was_cut_off():
    s = PL.PlanGateState()
    d = _decide_graph(s, _plan(ROWS_CLOSED), True)
    assert d.action == "reject_graph" and "coverage.complete=false" in d.prompt and "範囲外の理由になりません" in d.prompt
    assert s.graph_nudges == 1 and s.continuations == 1
    d = _decide_graph(s, _plan(ROWS_CLOSED), True)
    assert d.action == "review"


def test_gate_does_not_reject_for_graph_without_closed_rows_or_cut_off():
    only_done = "- [x] a · program · 対象A · 要: source\n      - source: src/a.cbl:12\n"
    assert _decide_graph(PL.PlanGateState(), _plan(only_done), True).action == "review"
    assert _decide_graph(PL.PlanGateState(), _plan(ROWS_CLOSED), False).action == "review"


def test_gate_graph_rejection_applies_before_accepting_after_a_review():
    s = PL.PlanGateState()
    assert _decide_graph(s, _plan(ROWS_CLOSED), False).action == "review"
    d = _decide_graph(s, _plan(ROWS_CLOSED), True)
    assert d.action == "reject_graph"
    d = _decide_graph(s, _plan(ROWS_CLOSED), True)
    assert d.action == "accept" and d.reason == "complete"


def test_gate_graph_rejection_respects_continue_cap():
    s = PL.PlanGateState(continuations=3)
    assert _decide_graph(s, _plan(ROWS_CLOSED), True, cap=0, continue_cap=3).action == "accept"
    assert s.graph_nudges == 0


def test_graph_incomplete_reads_graph_impact_rows_only_from_the_call_log():
    class Merged:
        graph = [{"tool": "graph_resolve", "complete": False},
                 {"tool": "graph_impact", "complete": True},
                 {"tool": "graph_impact", "complete": False, "depth_truncated": True}]

    class St:
        call_log = Merged()
        _call_log_dir = None
        turn_uid = "t"

    assert PL.graph_incomplete(St()) is True
    assert PL._graph_stats(St()) == {"impact_calls": 2, "impact_incomplete": 1, "impact_depth_truncated": 1}
    Merged.graph = [{"tool": "graph_impact", "complete": True}]
    assert PL.graph_incomplete(St()) is False
    St.call_log = None
    assert PL.graph_incomplete(St()) is False


def test_gate_stats_lists_closed_rows_with_reason_and_detail():
    plan = PL.parse_plan(_plan(ROWS_CLOSED + "- [-] c · k · 対象C · 要: source\n"))

    class St:
        _plan_gate = PL.PlanGateState(graph_nudges=1)

    gate = PL._gate_stats(St(), plan, {"impact_incomplete": 1})
    assert gate["graph_nudges"] == 1 and gate["accepted_with_graph_incomplete"] is True
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


# ---- 対象 ID の集合による欠落の照合 ----

def _row(path, dist=1, kind="program"):
    name = path.rsplit("/", 1)[-1]
    return {"name": name, "path": path, "cid": "cid:" + path, "kind": kind, "distance": dist}


def _g(rows, *, start="ZCPY", complete=True, count=None):
    return {"tool": "graph_impact", "complete": complete, "count": len(rows) if count is None else count,
            "start": {"name": start, "path": f"copy/{start}.cpy", "cid": f"cid:copy/{start}.cpy"}, "rows": rows}


_COPY_ASK = "コピーブック ZCPY の項目の桁数を変更した場合に、直接取り込むプログラムを全件挙げて"
_IMPACT_ASK = "コピーブック ZCPY の項目の桁数を変更した場合の影響範囲を全件"
_ROWS3 = [_row("src/alpha.cbl"), _row("src/beta.cbl"), _row("src/gamma.cbl"), _row("jcl/delta.jcl", 2, "job")]


def _ans(*lines, refs=()):
    return "\n".join(lines) + ("\n\n参照した資料:\n" + "\n".join(f"- {r}" for r in refs) if refs else "")


def test_direct_copy_request_needs_programs_even_without_statement_use():
    ans = _ans("- src/alpha.cbl 受付", "- src/beta.cbl 集計", "- src/gamma.cbl は実行文で使用していないため対象外")
    c = PL.check_targets([_g(_ROWS3)], _COPY_ASK, ans)
    assert c["condition_mode"] == "direct_copies" and c["status"] == "missing"
    assert [m["id"] for m in c["missing"]] == ["src/gamma.cbl"] and c["excluded_invalid_count"] == 1
    assert c["required_count"] == 3 and c["retrieved_complete"] is False


def test_default_request_needs_all_rows_and_accepts_a_reasoned_exclusion():
    ans = _ans("- src/alpha.cbl", "- src/beta.cbl", "- jcl/delta.jcl", "- src/gamma.cbl は別システムの同名ファイルのため対象外")
    c = PL.check_targets([_g(_ROWS3)], _IMPACT_ASK, ans)
    assert c["condition_mode"] == "default" and c["status"] == "no_missing"
    assert (c["included_count"], c["excluded_count"], c["required_count"]) == (3, 1, 4)
    assert c["retrieved_complete"] is True and c["coverage_complete"] is True


def test_narrowing_words_make_the_condition_undeterminable_and_never_reject():
    for ask in ("ZCPY を実行文で使うものだけ挙げて", "ZCPY を使用している箇所のみ", "ZCPY を参照している箇所"):
        c = PL.check_targets([_g(_ROWS3)], ask, "何も書かない")
        assert c["status"] == "undeterminable" and c["condition_mode"] == "undeterminable" and c["missing_count"] == 0
        d = PL.decide(PL.PlanGateState(), PL.parse_plan(_plan(ROWS_DONE)), review_cap=1, target=c)
        assert d.action == "review"


def test_swapped_ids_with_the_same_count_are_detected():
    rows = [_row("src/p1.cbl"), _row("src/p2.cbl"), _row("src/p3.cbl")]
    ans = _ans("- src/q1.cbl", "- src/q2.cbl", "- src/q3.cbl")
    c = PL.check_targets([_g(rows)], _IMPACT_ASK, ans)
    assert c["included_count"] == 0 and c["missing_count"] == 3
    ans2 = _ans("- src/p3.cbl", "- src/q2.cbl", "- src/p1.cbl")
    c2 = PL.check_targets([_g(rows)], _IMPACT_ASK, ans2)
    assert [m["id"] for m in c2["missing"]] == ["src/p2.cbl"]


def test_paths_only_in_the_reference_block_are_not_included():
    rows = [_row("src/alpha.cbl"), _row("src/beta.cbl")]
    ans = _ans("影響先は2本です。", refs=("src/alpha.cbl:1", "src/beta.cbl:2"))
    c = PL.check_targets([_g(rows)], _IMPACT_ASK, ans)
    assert c["missing_count"] == 2
    ok = PL.check_targets([_g(rows)], _IMPACT_ASK, _ans("- src/alpha.cbl", "- src/beta.cbl"))
    assert ok["status"] == "no_missing"


def test_unique_file_or_program_names_match_but_ambiguous_ones_need_the_path():
    rows = [_row("src/a/shared.cbl"), _row("src/b/shared.cbl"), _row("src/uniq.cbl")]
    c = PL.check_targets([_g(rows)], _IMPACT_ASK, _ans("- UNIQ を呼ぶ", "- shared.cbl が関係する"))
    assert [m["id"] for m in c["missing"]] == ["src/a/shared.cbl", "src/b/shared.cbl"]


def test_truncated_results_never_make_coverage_complete():
    rows = [_row("src/alpha.cbl")]
    ans = _ans("- src/alpha.cbl")
    c = PL.check_targets([_g(rows, complete=False)], _IMPACT_ASK, ans)
    assert c["status"] == "no_missing" and c["retrieved_complete"] is True and c["coverage_complete"] is False
    c2 = PL.check_targets([_g(rows, count=5)], _IMPACT_ASK, ans)
    assert c2["coverage_complete"] is False


def test_no_graph_result_zero_targets_and_unrelated_answers_do_not_reject():
    assert PL.check_targets([], _IMPACT_ASK, "本文")["status"] == "not_applicable"
    assert PL.check_targets([_g([])], _IMPACT_ASK, "本文")["status"] == "not_applicable"
    assert PL.check_targets([{**_g([_row("src/a.cbl")]), "error": "boom"}], _IMPACT_ASK, "")["status"] == "not_applicable"
    state = PL.PlanGateState()
    d = PL.decide(state, PL.parse_plan(_plan(ROWS_DONE)), review_cap=1, target=PL.check_targets([], _IMPACT_ASK, "無関係"))
    assert d.action == "review"


def test_only_the_first_start_and_starts_inside_included_results_are_required():
    first = _g([_row("src/alpha.cbl"), _row("src/beta.cbl")])
    chained = {**_g([_row("src/chain.cbl")], start="beta"), "start": {"name": "beta", "path": "src/beta.cbl", "cid": "cid:src/beta.cbl"}}
    stray = {**_g([_row("src/stray.cbl")], start="other"), "start": {"name": "o", "path": "x/other.cpy", "cid": "cid:x/other.cpy"}}
    targets, calls = PL.required_targets([first, stray, chained])
    assert set(targets) == {"src/alpha.cbl", "src/beta.cbl", "src/chain.cbl"} and len(calls) == 2


def test_missing_targets_use_the_review_slot_once_with_a_concrete_prompt():
    rows = [_row(f"src/p{i:02d}.cbl", 1 + i % 2) for i in range(45)]
    c = PL.check_targets([_g(rows)], _IMPACT_ASK, _ans("- src/p00.cbl"))
    state, plan = PL.PlanGateState(), PL.parse_plan(_plan(ROWS_DONE))
    d = PL.decide(state, plan, review_cap=1, target=c)
    assert d.action == "reject_targets" and state.review_requests == 1
    assert "44 件" in d.prompt and "（ほか 4 件）" in d.prompt and "src/p01.cbl（距離 2・種別 program）" in d.prompt
    assert "src/p41.cbl" not in d.prompt and "依頼の条件" in d.prompt
    c2 = PL.check_targets([_g(rows)], _IMPACT_ASK, _ans(*(f"- src/p{i:02d}.cbl" for i in range(3))))
    d2 = PL.decide(state, plan, review_cap=1, target=c2)  # 枠は増えない
    assert d2.action == "accept" and state.review_requests == 1


def test_gate_step_resolves_or_records_unresolved_after_the_slot_is_used():
    rows = [_row("src/alpha.cbl"), _row("src/beta.cbl")]
    plan = PL.parse_plan(_plan(ROWS_DONE))
    miss = PL.check_targets([_g(rows)], _IMPACT_ASK, _ans("- src/alpha.cbl"))
    ok = PL.check_targets([_g(rows)], _IMPACT_ASK, _ans("- src/alpha.cbl", "- src/beta.cbl"))
    st = PL.PlanGateState()
    assert PL.gate_step(st, plan, review_cap=1, target=miss).action == "reject_targets"
    assert PL.gate_step(st, plan, review_cap=1, target=ok).action == "accept" and st.unresolved is None
    assert PL.target_check_record(st, dry_run=False)["decision"] == "reject_missing"
    st2 = PL.PlanGateState()
    PL.gate_step(st2, plan, review_cap=1, target=miss)
    assert PL.gate_step(st2, plan, review_cap=1, target=miss).action == "accept"
    assert st2.unresolved is miss and "1 件" in PL.unresolved_notice(miss) and "src/beta.cbl" in PL.unresolved_notice(miss)
    assert PL.target_check_record(st2, dry_run=False)["decision"] == "accepted_unresolved"
    st3 = PL.PlanGateState()  # 枠が無い深さでは差し戻さず、未解決として受理する
    assert PL.gate_step(st3, plan, review_cap=0, target=miss).action == "accept" and st3.unresolved is miss


def test_without_missing_targets_the_generic_review_is_unchanged():
    st = PL.PlanGateState()
    d = PL.decide(st, PL.parse_plan(_plan(ROWS_DONE)), review_cap=1)
    assert d.action == "review" and d.prompt == PL._REVIEW_PROMPT


def test_dry_run_records_the_decision_but_never_rejects():
    rows = [_row("src/alpha.cbl"), _row("src/beta.cbl")]
    miss = PL.check_targets([_g(rows)], _IMPACT_ASK, _ans("- src/alpha.cbl"))
    st = PL.PlanGateState()
    d = PL.gate_step(st, PL.parse_plan(_plan(ROWS_OPEN)), review_cap=1, target=miss, dry_run=True)
    assert d.action == "accept" and st.review_requests == 0 and st.continuations == 0 and st.unresolved is None
    assert st.log[0]["action"] == "reject_unstarted" and st.log[0]["dry_run"] is True
    d = PL.gate_step(st, PL.parse_plan(_plan(ROWS_DONE)), review_cap=1, target=miss, dry_run=True)
    assert d.action == "accept" and st.log[1]["action"] == "reject_targets"
    rec = PL.target_check_record(st, dry_run=True)
    assert rec["decision"] == "recorded_only" and rec["dry_run"] is True and rec["missing"] == ["src/beta.cbl"]


def test_format_only_problems_do_not_reject_but_real_unstarted_rows_do():
    bad = PL.parse_plan(_plan("- [x] a · k · 対象\n- [-] b · k · 対象B · 要: source\n    - 無い: \n"))
    st = PL.PlanGateState()
    assert PL.decide(st, bad, review_cap=0).action == "accept" and st.continuations == 0 and st.format_warnings == 1
    real = PL.parse_plan(_plan("- [x] a · k · 対象\n- [ ] b · k · 対象B · 要: source\n"))
    st = PL.PlanGateState()
    d = PL.decide(st, real, review_cap=1)
    assert d.action == "reject_unstarted" and "b" in d.prompt and st.continuations == 1
    stray = PL.parse_plan(_plan("- [x] a · k · 対象A · 要: source\n    - source: src/a.cbl:1\n説明の文です\n"))
    assert PL.decide(PL.PlanGateState(), stray, review_cap=0).action == "accept"
