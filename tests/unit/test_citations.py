"""citations.py の単体テスト（SEARCH-CUT-3: locator まわりの契約と RV 是正）。

- from_es_hit: `locator`/`chunk_id` は**常に**付けない（citation dict は公開・保存される・RV MED-1）。
- locator_hint: sheet+cell_range／page／slide／未知の組み合わせ→空文字。型検証・改行除去・引用符
  エスケープ・長さ上限（RV MED-3）。
- dedupe_round_robin_by_doc_span: span が無効（rag_chunks 由来の `[None, None]`）でも quote が異なる
  citation は潰さない（RV MED-2）。span が実値のときは従来どおり。
"""
from __future__ import annotations

from sherpa import citations


def test_from_es_hit_never_adds_locator_or_chunk_id():
    """rag_chunks 由来ヒット（locator/chunk_id あり）でも citation dict は従来形のまま（RV MED-1）。"""
    loc = {"part": "xl/worksheets/sheet1.xml", "sheet": "明細", "cell_range": "A2"}
    h = {"doc_id": "b.xlsx", "line": None, "text": "行", "ext": ".xlsx", "locator": loc, "chunk_id": "rc1"}
    c = citations.from_es_hit(h, "query")
    assert c == {"doc_id": "b.xlsx", "span": [None, None], "quote": "行", "ext": ".xlsx", "match": "query"}
    assert "locator" not in c and "chunk_id" not in c


def test_from_es_hit_without_locator_is_unchanged():
    h = {"doc_id": "a.md", "line": 3, "text": "本文", "ext": ".md"}
    c = citations.from_es_hit(h, "query")
    assert c == {"doc_id": "a.md", "span": [3, 3], "quote": "本文", "ext": ".md", "match": "query"}


def test_from_es_hit_chunk_id_only_is_still_byte_identical():
    """chunk_id はあるが locator が無い rag チャンクでも、citation は chunk_id を持たない
    （RV MED-1 が名指しした回帰ケース）。"""
    h = {"doc_id": "c.docx", "line": None, "text": "y", "ext": ".docx", "chunk_id": "rc2"}
    c = citations.from_es_hit(h, "q", include_match=False)
    assert c == {"doc_id": "c.docx", "span": [None, None], "quote": "y", "ext": ".docx"}


# ==== locator_hint（自然文整形・型/長さ安全化） ====

def test_locator_hint_sheet_and_cell_range():
    assert citations.locator_hint({"sheet": "明細", "cell_range": "A2"}) == "シート「明細」A2"


def test_locator_hint_page():
    assert citations.locator_hint({"part": "doc.pdf", "page": 3}) == "p.3"


def test_locator_hint_slide():
    assert citations.locator_hint({"part": "ppt/slides/slide4.xml", "slide": 4}) == "スライド4"


def test_locator_hint_unknown_shape_is_empty():
    assert citations.locator_hint({"part": "src/foo.cob"}) == ""            # part のみ＝未知
    assert citations.locator_hint({"sheet": "明細"}) == ""                   # cell_range 無し＝未知
    assert citations.locator_hint({}) == ""
    assert citations.locator_hint(None) == ""
    assert citations.locator_hint("not-a-mapping") == ""


def test_locator_hint_prefers_sheet_over_page_and_slide():
    """優先順位: sheet+cell_range > page > slide（同時に複数持つ形は実データ上は想定外だが決定的に）。"""
    assert citations.locator_hint({"sheet": "明細", "cell_range": "B1", "page": 1}) == "シート「明細」B1"
    assert citations.locator_hint({"page": 2, "slide": 5}) == "p.2"


def test_locator_hint_rejects_non_positive_or_bool_page_and_slide():
    """RV MED-3: page/slide は正の非 bool 整数のみ。0/負数/True 等は表示しない。"""
    assert citations.locator_hint({"page": 0}) == ""
    assert citations.locator_hint({"page": -1}) == ""
    assert citations.locator_hint({"page": True}) == ""     # bool は int のサブクラス＝明示的に除外
    assert citations.locator_hint({"slide": 0}) == ""
    assert citations.locator_hint({"slide": -3}) == ""


def test_locator_hint_rejects_non_string_sheet_or_cell_range():
    """RV MED-3: 数値の cell_range 等、型が違うものは素通りさせない。"""
    assert citations.locator_hint({"sheet": "明細", "cell_range": 12}) == ""
    assert citations.locator_hint({"sheet": 1, "cell_range": "A2"}) == ""


def test_locator_hint_strips_newlines_and_escapes_quote_marks():
    """RV MED-3: シート名の改行・「」はそのまま出さない（LLM プロンプトの引用境界を壊さない）。"""
    hint = citations.locator_hint({"sheet": "明細「本番」\n注記", "cell_range": "A2"})
    assert hint == "シート「明細『本番』 注記」A2"
    assert "\n" not in hint


def test_locator_hint_caps_overlong_fields():
    """RV MED-3: 長大なシート名は上限を超えたら不正値扱い（黙って空文字）。"""
    assert citations.locator_hint({"sheet": "あ" * 41, "cell_range": "A2"}) == ""
    assert citations.locator_hint({"sheet": "あ" * 40, "cell_range": "A2"}) != ""


def test_locator_hint_rejects_huge_page_and_slide_numbers():
    """RV 追加是正: page/slide に上限が無いと巨大整数がそのまま `f"p.{page}"` に展開され、
    位置ヒント全体の長さ上限（60字）を突破しうる。6桁（999999）を超える値は不正値として拒否する。"""
    assert citations.locator_hint({"page": 999_999}) == "p.999999"      # 境界値は許可
    assert citations.locator_hint({"page": 1_000_000}) == ""             # 7桁は拒否
    assert citations.locator_hint({"page": 10 ** 100}) == ""             # 巨大整数も拒否
    assert citations.locator_hint({"slide": 10 ** 100}) == ""


def test_locator_hint_overall_length_never_exceeds_cap():
    """全体上限（60字）は sheet+cell_range 経路だけでなく page/slide 経路にも一律に掛ける（防御的二重適用）。"""
    assert len(citations.locator_hint({"page": 999_999})) <= 60


def test_locator_hint_strips_unicode_line_separators():
    """RV 追加是正: `\\n`/`\\r` だけでなく `str.splitlines()` が改行とみなす全種
    （U+2028 LINE SEPARATOR・U+2029 PARAGRAPH SEPARATOR・垂直タブ等）を単一空白に正規化する
    （改行境界を明示的な \\uXXXX エスケープで書く＝ソースに不可視文字を直書きしない）。
    """
    sheet = "明細\u2028注記\u2029続き\x0b末尾"
    assert len(sheet.splitlines()) == 4          # 前提確認: 4文字とも str の改行境界として認識される
    hint = citations.locator_hint({"sheet": sheet, "cell_range": "A2"})
    assert hint == "シート「明細 注記 続き 末尾」A2"
    assert "\u2028" not in hint and "\u2029" not in hint and "\x0b" not in hint


# ==== citation_dedupe_key（base.py の agentic 集約3箇所と共通の鍵規則） ====

def test_citation_dedupe_key_uses_span_when_valid():
    assert citations.citation_dedupe_key({"doc_id": "x.md", "span": [3, 3], "quote": "p"}) == ("x.md", (3, 3))


def test_citation_dedupe_key_falls_back_to_quote_when_span_invalid():
    key_a = citations.citation_dedupe_key({"doc_id": "b.xlsx", "span": [None, None], "quote": "単価100円"})
    key_b = citations.citation_dedupe_key({"doc_id": "b.xlsx", "span": [None, None], "quote": "数量5個"})
    assert key_a != key_b
    assert key_a == ("b.xlsx", (None, None), "単価100円")


# ==== dedupe_round_robin_by_doc_span（rag_chunks の複数ヒットを潰さない） ====

def test_dedupe_keeps_valid_span_citations_unique_as_before():
    a = {"doc_id": "x.md", "span": [1, 1], "quote": "p"}
    b = {"doc_id": "x.md", "span": [1, 1], "quote": "q"}   # 同一 span＝従来どおり重複排除
    assert citations.dedupe_round_robin_by_doc_span([a, b]) == [a]


def test_dedupe_keeps_distinct_rag_chunks_with_null_span():
    """RV MED-2: span=[None, None] の rag チャンクは quote が違えば別ヒットとして残す。"""
    a = {"doc_id": "b.xlsx", "span": [None, None], "quote": "単価100円"}
    b = {"doc_id": "b.xlsx", "span": [None, None], "quote": "数量5個"}
    merged = citations.dedupe_round_robin_by_doc_span([a, b])
    assert merged == [a, b]


def test_dedupe_still_collapses_identical_null_span_quote():
    """quote まで完全一致なら従来どおり重複排除（実質同一引用）。"""
    a = {"doc_id": "b.xlsx", "span": [None, None], "quote": "単価100円"}
    b = {"doc_id": "b.xlsx", "span": [None, None], "quote": "単価100円"}
    assert citations.dedupe_round_robin_by_doc_span([a, b]) == [a]


# ==== merge_overlapping_citations（同一 doc の重なる span を1件に統合）====

def test_merge_overlapping_citations_containment_keeps_widest_span():
    """行1-10 が行3-5 を包含する場合、1件に統合され span は最も広い方（[1,10]）になる。"""
    wide = {"doc_id": "a.md", "span": [1, 10], "quote": "wide"}
    narrow = {"doc_id": "a.md", "span": [3, 5], "quote": "narrow"}
    out_c, out_m, flags = citations.merge_overlapping_citations(
        [wide, narrow], [{"k": "w"}, {"k": "n"}])
    assert out_c == [{"doc_id": "a.md", "span": [1, 10], "quote": "wide"}]
    assert out_m == [{"k": "w", "span": [1, 10]}]   # evidence_meta も widest 側の1件・span は同期
    assert flags == [True]


def test_merge_overlapping_citations_partial_overlap_unions_span():
    """行1-3・行1-5・行2-6 のように部分的に重なる citation は1件に統合され、span は和集合
    （[1,6]）になる。quote は元の中で最も広い（同点は先勝ち）ものを引き継ぐ。"""
    a = {"doc_id": "a.md", "span": [1, 3], "quote": "q1"}
    b = {"doc_id": "a.md", "span": [1, 5], "quote": "q2"}
    c = {"doc_id": "a.md", "span": [2, 6], "quote": "q3"}
    out_c, out_m, flags = citations.merge_overlapping_citations([a, b, c], [{}, {}, {}])
    assert len(out_c) == 1
    assert out_c[0]["span"] == [1, 6]
    assert out_c[0]["quote"] == "q2"   # (1,5) と (2,6) は同じ幅4だが先勝ちで (1,5)=q2
    assert flags == [True]


def test_merge_overlapping_citations_evidence_meta_span_syncs_with_merged_citation():
    """統合後は citation.span だけでなく evidence_meta.span も同じ和集合へ更新される
    （citation と evidence_meta の span が食い違わないことの契約）。"""
    a = {"doc_id": "a.md", "span": [1, 3], "quote": "q1"}
    b = {"doc_id": "a.md", "span": [2, 6], "quote": "q2"}
    em_a = {"doc_id": "a.md", "span": [1, 3], "verification_method": "span_verified"}
    em_b = {"doc_id": "a.md", "span": [2, 6], "verification_method": "span_verified"}
    out_c, out_m, flags = citations.merge_overlapping_citations([a, b], [em_a, em_b])
    assert out_c[0]["span"] == [1, 6]
    assert out_m[0]["span"] == [1, 6]   # citation と evidence_meta で span が食い違わない
    assert flags == [True]


def test_merge_overlapping_citations_non_overlapping_spans_stay_separate():
    """行1-3・行5-8 のように重ならない citation は別件のまま（1行も共有しない）。"""
    a = {"doc_id": "a.md", "span": [1, 3], "quote": "x"}
    b = {"doc_id": "a.md", "span": [5, 8], "quote": "y"}
    out_c, out_m, flags = citations.merge_overlapping_citations([a, b], [{}, {}])
    assert out_c == [a, b]
    assert out_m == [{}, {}]
    assert flags == [False, False]


def test_merge_overlapping_citations_adjacent_no_shared_line_does_not_merge():
    """行1-3・行4-5 は隣接する（間に隙間が無い）が、1行も共有しないため統合しない（境界値）。"""
    a = {"doc_id": "a.md", "span": [1, 3], "quote": "x"}
    b = {"doc_id": "a.md", "span": [4, 5], "quote": "y"}
    out_c, _out_m, flags = citations.merge_overlapping_citations([a, b], [{}, {}])
    assert out_c == [a, b]
    assert flags == [False, False]


def test_merge_overlapping_citations_touching_spans_do_merge():
    """行1-3・行3-5 は行3を共有する＝重なる（統合対象）。"""
    a = {"doc_id": "a.md", "span": [1, 3], "quote": "x"}
    b = {"doc_id": "a.md", "span": [3, 5], "quote": "y"}
    out_c, _out_m, flags = citations.merge_overlapping_citations([a, b], [{}, {}])
    assert len(out_c) == 1
    assert out_c[0]["span"] == [1, 5]
    assert flags == [True]


def test_merge_overlapping_citations_different_doc_ids_never_merge():
    """同じ行範囲でも doc_id が違えば別件のまま。"""
    a = {"doc_id": "a.md", "span": [1, 5], "quote": "x"}
    b = {"doc_id": "b.md", "span": [1, 5], "quote": "y"}
    out_c, _out_m, flags = citations.merge_overlapping_citations([a, b], [{}, {}])
    assert out_c == [a, b]
    assert flags == [False, False]


def test_merge_overlapping_citations_no_span_citations_pass_through_untouched():
    """span を持たない（None 等・rag_chunks 由来）citation は対象外（そのまま素通し・統合しない）。"""
    a = {"doc_id": "a.md", "span": [None, None], "quote": "x"}
    b = {"doc_id": "a.md", "span": [None, None], "quote": "y"}
    out_c, out_m, flags = citations.merge_overlapping_citations([a, b], [{"k": 1}, {"k": 2}])
    assert out_c == [a, b]
    assert out_m == [{"k": 1}, {"k": 2}]
    assert flags == [False, False]


def test_merge_overlapping_citations_reversed_span_is_excluded_from_merging():
    """`start > end`（逆転した不正な span）の citation は対象外（`_span_range` が None を返す）
    ——正しい span の citation と重なるように見えても統合されない。"""
    a = {"doc_id": "a.md", "span": [1, 5], "quote": "normal"}
    reversed_span = {"doc_id": "a.md", "span": [5, 1], "quote": "reversed"}
    out_c, _out_m, flags = citations.merge_overlapping_citations([a, reversed_span], [{}, {}])
    assert out_c == [a, reversed_span]   # どちらも素通し（統合されない）
    assert flags == [False, False]


def test_merge_overlapping_citations_empty_input_returns_empty():
    assert citations.merge_overlapping_citations([], []) == ([], [], [])
