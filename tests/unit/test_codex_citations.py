"""S2（提案書 2026-09-10-Codex原本直読と調査スキル.md §2-5・§6 裁定 #3）: Codex 原本直読の出典化。

`sherpa/providers/codex/citations.py` の3段（解析→正規化→機械検証）を DB 非依存で検証する。
"""
from __future__ import annotations

import pytest

from sherpa.providers.codex import citations as C

# ===== parse_referenced_docs =====


def test_parse_no_block_returns_answer_unchanged():
    answer = "消費税率は10%です。根拠は税計算仕様書に記載されています。"
    body, refs = C.parse_referenced_docs(answer)
    assert body == answer
    assert refs == []


def test_parse_basic_bullet_block():
    answer = (
        "消費税率は10%です。\n\n"
        "参照した資料:\n"
        "- 4期/02_設計/01_基本設計/税計算仕様書.md\n"
        "- 4期/01_標準/消費税法.md\n"
    )
    body, refs = C.parse_referenced_docs(answer)
    assert body == "消費税率は10%です。"
    assert refs == [
        "4期/02_設計/01_基本設計/税計算仕様書.md",
        "4期/01_標準/消費税法.md",
    ]


@pytest.mark.parametrize("heading", [
    "参照した資料:",
    "参照した資料：",
    "**参照した資料:**",
    "## 参照した資料：",
    "- 参照した資料:",
])
def test_parse_heading_variants(heading):
    answer = f"本文です。\n\n{heading}\n- 4期/01_標準/消費税法.md\n"
    body, refs = C.parse_referenced_docs(answer)
    assert body == "本文です。"
    assert refs == ["4期/01_標準/消費税法.md"]


def test_parse_numbered_and_backtick_and_multi_split_and_paren_annotation():
    answer = (
        "本文。\n\n"
        "参照した資料:\n"
        "1. `4期/02_設計/01_基本設計/税計算仕様書.md`（Sheet1!B3:D10）\n"
        "2. 4期/01_標準/消費税法.md、4期/01_標準/経理コーディング規約.md\n"
    )
    body, refs = C.parse_referenced_docs(answer)
    assert body == "本文。"
    # 区切り文字を含む行は「行全体」も候補に残す（パス自体に区切り文字を含む場合の取りこぼし防止・
    # 実在確認で落ちる）。
    assert refs == [
        "4期/02_設計/01_基本設計/税計算仕様書.md",
        "4期/01_標準/消費税法.md、4期/01_標準/経理コーディング規約.md",
        "4期/01_標準/消費税法.md",
        "4期/01_標準/経理コーディング規約.md",
    ]


def test_parse_dedupes_preserving_first_occurrence_order():
    answer = (
        "本文。\n\n"
        "参照した資料:\n"
        "- 4期/01_標準/消費税法.md\n"
        "- 4期/02_設計/01_基本設計/税計算仕様書.md\n"
        "- 4期/01_標準/消費税法.md\n"
    )
    _, refs = C.parse_referenced_docs(answer)
    assert refs == [
        "4期/01_標準/消費税法.md",
        "4期/02_設計/01_基本設計/税計算仕様書.md",
    ]


def test_parse_trims_trailing_blank_lines_before_block():
    answer = "本文です。\n\n\n\n参照した資料:\n- 4期/01_標準/消費税法.md\n"
    body, _ = C.parse_referenced_docs(answer)
    assert body == "本文です。"


def test_parse_keeps_paragraph_after_blank_line():
    answer = (
        "本文。\n\n"
        "参照した資料:\n"
        "- 4期/01_標準/消費税法.md\n"
        "\n"
        "この後の文は本文として残る。\n"
    )
    body, refs = C.parse_referenced_docs(answer)
    assert refs == ["4期/01_標準/消費税法.md"]
    assert body == "本文。\n\nこの後の文は本文として残る。"   # ブロック後の本文は残す（落とさない）


def test_parse_last_heading_wins_when_multiple():
    answer = (
        "参照した資料という言葉について説明します。\n\n"
        "実際の本文はここです。\n\n"
        "参照した資料:\n"
        "- 4期/01_標準/消費税法.md\n"
    )
    body, refs = C.parse_referenced_docs(answer)
    assert refs == ["4期/01_標準/消費税法.md"]
    assert "実際の本文はここです。" in body
    assert "参照した資料:" not in body


# ===== normalize_doc_ref =====


@pytest.fixture
def _derived_roots(monkeypatch, tmp_path):
    """KB root・派生 md/rag root を tmp_path 配下に隔離する（実 fixtures/data に依存しない）。"""
    from sherpa import worlds as W

    kb = tmp_path / "kb"
    kb.mkdir()
    md = tmp_path / "derived" / "md"
    md.mkdir(parents=True)
    rag = tmp_path / "derived" / "rag"
    rag.mkdir(parents=True)
    monkeypatch.setattr(W, "_fixtures", lambda: False)
    monkeypatch.setattr(W, "world_dir", lambda w: kb)
    monkeypatch.setattr(W, "derived_md_dir", lambda w: md)
    monkeypatch.setattr(W, "derived_rag_dir", lambda w: rag)
    return {"kb": kb, "md": md, "rag": rag}


def test_normalize_relative_path_passthrough(_derived_roots):
    assert C.normalize_doc_ref("4期/02_設計/税計算仕様書.md", "test") == "4期/02_設計/税計算仕様書.md"


def test_normalize_kb_absolute_path_to_relative(_derived_roots):
    kb = _derived_roots["kb"]
    (kb / "4期").mkdir()
    abs_p = str((kb / "4期" / "税計算仕様書.md").resolve())
    assert C.normalize_doc_ref(abs_p, "test") == "4期/税計算仕様書.md"


def test_normalize_derived_rag_md_absolute_strips_suffix(_derived_roots):
    rag = _derived_roots["rag"]
    abs_p = str(rag / "4期" / "xxx.xlsx.rag.md")
    assert C.normalize_doc_ref(abs_p, "test") == "4期/xxx.xlsx"


def test_normalize_derived_rag_chunks_absolute_strips_suffix(_derived_roots):
    rag = _derived_roots["rag"]
    abs_p = str(rag / "4期" / "xxx.xlsx.rag_chunks.jsonl")
    assert C.normalize_doc_ref(abs_p, "test") == "4期/xxx.xlsx"


def test_normalize_derived_rag_assets_absolute_strips_to_original(_derived_roots):
    rag = _derived_roots["rag"]
    abs_p = str(rag / "4期" / "xxx.xlsx.assets" / "sheet1.png")
    assert C.normalize_doc_ref(abs_p, "test") == "4期/xxx.xlsx"


def test_normalize_derived_md_absolute_strips_suffix(_derived_roots):
    md = _derived_roots["md"]
    abs_p = str(md / "4期" / "xxx.docx.md")
    assert C.normalize_doc_ref(abs_p, "test") == "4期/xxx.docx"


def test_normalize_derived_md_meta_absolute_strips_suffix(_derived_roots):
    md = _derived_roots["md"]
    abs_p = str(md / "4期" / "xxx.docx.md.meta.json")
    assert C.normalize_doc_ref(abs_p, "test") == "4期/xxx.docx"


def test_normalize_absolute_path_outside_all_roots_is_none(_derived_roots):
    assert C.normalize_doc_ref("/etc/passwd", "test") is None


def test_normalize_relative_dotdot_is_none(_derived_roots):
    assert C.normalize_doc_ref("../etc/passwd", "test") is None
    assert C.normalize_doc_ref("4期/../../etc/passwd", "test") is None


def test_normalize_backslash_normalized_to_forward_slash(_derived_roots):
    assert C.normalize_doc_ref("4期\\02_設計\\x.md", "test") == "4期/02_設計/x.md"


def test_normalize_empty_and_none_like_inputs(_derived_roots):
    assert C.normalize_doc_ref("", "test") is None
    assert C.normalize_doc_ref("   ", "test") is None


def test_normalize_strips_quotes_and_backticks(_derived_roots):
    assert C.normalize_doc_ref("`4期/02_設計/税計算仕様書.md`", "test") == "4期/02_設計/税計算仕様書.md"


# ===== verified_referenced_docs =====


def test_verified_drops_sensitive_names(monkeypatch):
    monkeypatch.setattr(
        "sherpa.agentic_search.verify_doc_exists", lambda doc, world, scope_paths=None: True)
    out = C.verified_referenced_docs(
        ["4期/01_標準/消費税法.md", ".env", "4期/secrets/id_rsa"], "test")
    assert out == ["4期/01_標準/消費税法.md"]


def test_verified_drops_nonexistent_and_preserves_order_and_dedupes(monkeypatch):
    existing = {"4期/a.md", "4期/b.md"}
    monkeypatch.setattr(
        "sherpa.agentic_search.verify_doc_exists",
        lambda doc, world, scope_paths=None: doc in existing)
    out = C.verified_referenced_docs(
        ["4期/a.md", "4期/missing.md", "4期/b.md", "4期/a.md"], "test")
    assert out == ["4期/a.md", "4期/b.md"]


def test_verified_empty_input_returns_empty():
    assert C.verified_referenced_docs([], "test") == []


def test_parse_keeps_text_after_the_reference_block():
    """参照ブロックの後ろに本文（注意事項）が続くとき、それを落とさない。"""
    from sherpa.providers.codex.citations import parse_referenced_docs
    body, refs = parse_referenced_docs("結論。\n\n参照した資料:\n- a/b.md\n\n本番への反映は承認後に実施する。")
    assert refs == ["a/b.md"]
    assert body == "結論。\n\n本番への反映は承認後に実施する。"


def test_parse_keeps_parentheses_inside_file_names():
    """ファイル名の括弧は削らない（削ると別文書に化ける）。空白の後ろの注記だけ落とす。"""
    from sherpa.providers.codex.citations import parse_referenced_docs
    _, refs = parse_referenced_docs("x\n参照した資料:\n- 4期/消費税法（架空）.md\n- `a/b.xlsx` （Sheet1!B3）\n- c/d.xlsx （3 ページ）")
    assert refs == ["4期/消費税法（架空）.md", "a/b.xlsx", "c/d.xlsx"]


def test_parse_skips_blank_lines_right_after_heading():
    from sherpa.providers.codex.citations import parse_referenced_docs
    body, refs = parse_referenced_docs("x\n参照した資料:\n\n- a/b.md\n- c/d.md")
    assert refs == ["a/b.md", "c/d.md"] and body == "x"


def test_parse_line_with_separators_keeps_whole_line_and_parts():
    """区切り文字を含む 1 行は、行全体と各片の両方を候補にする（実在確認で正しい方だけ残る）。"""
    from sherpa.providers.codex.citations import parse_referenced_docs
    _, refs = parse_referenced_docs("x\n参照した資料: a/b.md, c/d.md")
    assert refs == ["a/b.md, c/d.md", "a/b.md", "c/d.md"]


def test_parse_keeps_raw_names_that_formatting_would_alter():
    """整形（箇条書き除去・括弧除去・分割）で別名に化ける名前は整形前の候補も残る。"""
    from sherpa.providers.codex.citations import parse_referenced_docs
    _, refs = parse_referenced_docs("x\n参照した資料:\n1.要件定義/a.md\n- 4期/売上,原価.xlsx\n- 4期/仕様書(旧).xlsx")
    assert "1.要件定義/a.md" in refs and "4期/売上,原価.xlsx" in refs and "4期/仕様書(旧).xlsx" in refs


def test_parse_block_at_start_keeps_following_body():
    from sherpa.providers.codex.citations import parse_referenced_docs
    body, refs = parse_referenced_docs("参照した資料:\n- 4期/a.md\n\n消費税率は10%です。")
    assert refs == ["4期/a.md"] and body == "消費税率は10%です。"


def test_verified_takes_whole_line_only_when_it_exists(monkeypatch):
    """1 行 1 件: 行全体が実在すればそれだけ（片は採らない）。行全体が無ければ各片を採る。"""
    from sherpa.providers.codex import citations as C
    from sherpa import agentic_search as A
    existing = {"4期/売上,原価.xlsx", "原価.xlsx", "a.md", "b.md"}
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: d in existing)
    _, lines = C.parse_referenced_doc_lines("x\n参照した資料:\n- 4期/売上,原価.xlsx\n- a.md、b.md")
    assert C.verified_referenced_docs(lines, "v1") == ["4期/売上,原価.xlsx", "a.md", "b.md"]


def test_parse_heading_with_inline_item_keeps_following_paragraph():
    """見出し行に項目があるときは直後の空行で終端＝後続の段落を参照として飲み込まない。"""
    from sherpa.providers.codex.citations import parse_referenced_docs
    body, refs = parse_referenced_docs("結論。\n\n参照した資料: 4期/a.md\n\n本番への反映は承認後に実施する。")
    assert refs == ["4期/a.md"] and body == "結論。\n\n本番への反映は承認後に実施する。"


def test_parse_bullet_without_space_yields_both_raw_and_stripped():
    """`・パス`／`1.パス`（記号の後に空白なし）は行そのものと記号を外した形の両方を候補にする。"""
    from sherpa.providers.codex.citations import parse_referenced_doc_lines
    _, lines = parse_referenced_doc_lines("x\n参照した資料:\n・4期/a.md\n1.要件定義/b.md")
    assert lines[0][0] == ["・4期/a.md", "4期/a.md"]
    assert lines[1][0] == ["1.要件定義/b.md", "要件定義/b.md"]


def test_parse_block_ends_at_prose_line_without_blank_separator():
    """空行で区切られていない後続本文（箇条書きでもパスらしくもない行）は参照に飲み込まない。"""
    from sherpa.providers.codex.citations import parse_referenced_docs
    body, refs = parse_referenced_docs("本文。\n\n参照した資料:\n- a/b.md\n注意: 本番反映は承認後に行う。")
    assert refs == ["a/b.md"] and body == "本文。\n\n注意: 本番反映は承認後に行う。"


def test_parse_annotations_without_space_yield_bare_path_candidates():
    """空白を挟まない注記（全角括弧・半角括弧・※）付きでも素のパスが候補の末尾に入る。"""
    from sherpa.providers.codex.citations import parse_referenced_doc_lines
    _, lines = parse_referenced_doc_lines("x\n参照した資料:\n- 4期/a.xlsx（Sheet1）\n- 4期/b.xlsx(3ページ)\n- 4期/a.md ※要約のみ")
    assert "4期/a.xlsx" in lines[0][0] and lines[0][0][0] == "4期/a.xlsx（Sheet1）"
    assert "4期/b.xlsx" in lines[1][0]
    assert "4期/a.md" in lines[2][0]


def test_parse_block_ends_at_prose_line_containing_date_or_path():
    """箇条書きでない行に日付やパスの言及があっても、空白を含む文なら本文として残す。"""
    from sherpa.providers.codex.citations import parse_referenced_docs
    body, refs = parse_referenced_docs("本文。\n\n参照した資料:\n- a/b.md\n注意: 反映は 2026/09/10 以降に行う。")
    assert refs == ["a/b.md"] and body == "本文。\n\n注意: 反映は 2026/09/10 以降に行う。"
    body, refs = parse_referenced_docs("本文。\n\n参照した資料:\n- a/b.md\n注意: 設定は config/app.yml を参照。")
    assert refs == ["a/b.md"] and "config/app.yml" in body


def test_parse_long_extension_and_prose_with_doc_mention():
    """`.properties` のような長い拡張子も参照として拾う。資料名を含む注意文（空白あり）は本文に残す。"""
    from sherpa.providers.codex.citations import parse_referenced_docs
    body, refs = parse_referenced_docs("結論。\n参照した資料:\na.md\napplication.properties\nb.md")
    assert refs == ["a.md", "application.properties", "b.md"]
    body, refs = parse_referenced_docs("結論。\n参照した資料:\n- a/b.md\n注意: 手順.md を参照してから本番反映する。")
    assert refs == ["a/b.md"] and body == "結論。\n\n注意: 手順.md を参照してから本番反映する。"


def test_parse_does_not_truncate_space_names_or_multi_items_at_first_space(monkeypatch):
    """空白を含むファイル名や `a.md, b.md` の複数件は最初の空白で切らない（別文書化・欠落の防止）。
    `path ※注記` のように明示の記号が続くときだけ切る。"""
    from sherpa.providers.codex import citations as C
    from sherpa import agentic_search as A
    existing = {"設計資料/基本設計.xlsx", "設計資料/基本設計.xlsx 旧版.xlsx", "a.md", "b.md", "c.md"}
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: d in existing)
    _, lines = C.parse_referenced_doc_lines("x\n参照した資料:\n- `設計資料/基本設計.xlsx 旧版.xlsx`\n- a.md, b.md\n- c.md ※要約のみ")
    assert C.verified_referenced_docs(lines, "v1") == ["設計資料/基本設計.xlsx 旧版.xlsx", "a.md", "b.md", "c.md"]


def test_parse_quoted_path_with_space_is_a_reference_line():
    """行全体が引用符で囲まれたパスは内部に空白があっても参照（ブロックが途中で終わらない）。"""
    from sherpa.providers.codex.citations import parse_referenced_docs
    _, refs = parse_referenced_docs("x\n参照した資料:\na.md\n`設計資料/基本 設計.xlsx`\nb.md")
    assert refs == ["a.md", "設計資料/基本 設計.xlsx", "b.md"]


def test_parse_keeps_names_with_apostrophes():
    from sherpa.providers.codex.citations import parse_referenced_doc_lines
    _, lines = parse_referenced_doc_lines("本文。\n\n参照した資料:\n- it's.md\n- b.md")
    assert lines[0][0][0] == "it's.md" and lines[1][0][0] == "b.md"


def test_parse_prose_with_multiple_code_spans_stays_in_body():
    from sherpa.providers.codex.citations import parse_referenced_docs
    body, refs = parse_referenced_docs("本文。\n参照した資料:\na.md\n`config/app.yml` の変更後は `systemctl restart app`")
    assert refs == ["a.md"] and "systemctl restart app" in body


def test_parse_quoted_sentence_stays_in_body_and_apostrophe_name_with_note_yields_bare_path():
    from sherpa.providers.codex import citations as C
    body, refs = C.parse_referenced_docs("本文。\n\n参照した資料:\n- a/b.md\n“注意: 反映は 2026/09/10 以降”\n最後の一文。")
    assert refs == ["a/b.md"] and "注意: 反映は 2026/09/10 以降" in body and "最後の一文。" in body
    assert "a/John's.md" in C._line_alternatives("- a/John's.md（要約）")[0]


def test_parse_backtick_quoted_name_with_apostrophe_is_a_reference_line():
    from sherpa.providers.codex.citations import parse_referenced_docs
    _, refs = parse_referenced_docs("x\n参照した資料:\na.md\n`a/John's.md`\nb.md")
    assert refs == ["a.md", "a/John's.md", "b.md"]


def test_parse_quoted_multi_item_line_is_a_reference_line():
    from sherpa.providers.codex.citations import parse_referenced_docs
    body, refs = parse_referenced_docs("本文\n\n参照した資料:\n`a/b.xlsx`、`c/d.xlsx`\n`e/f.xlsx`\n")
    assert "a/b.xlsx" in refs and "c/d.xlsx" in refs and "e/f.xlsx" in refs and body == "本文"
    body, refs = parse_referenced_docs("本文\n参照した資料:\na.md\n`config/app.yml` の変更後は `systemctl restart app`")
    assert refs == ["a.md"] and "systemctl" in body
