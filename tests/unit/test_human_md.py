"""人間向け MD レンダラ（`sherpa.ingest.human_md`）の単体テスト（DB不要）。

document-ir（`document_ir.Element`/`Cell`）を直接組み立てて渡し、`office_md`/`ooxml_arm` の抽出処理
そのもの（`_docx_table_walk`/`regions()` 等）とは切り離してレンダラ自身の契約を検証する:
- 結合セルの R5 展開（値を継続セルへ複製）。
- 全量方針（打切りをしない・セル数の安全弁付き）＋大きな表の行単位グループ分割
  （省略注記付き・1行だけの超過も対象・silent-drop なし）。
- xlsx はシート/表候補ごとの見出し（0件でも見出し＋注記のみで必ず非 None）、docx は原本の
  出現順（`order`）とネスト表の展開・`flags` の注記化。
- 行グループ単位の逐次生成（dense matrix を事前確保しない・巨大 span でも時間が破綻しない）。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from sherpa.ingest import document_ir, human_md
from sherpa.ingest.ooxml import excel

_EXTRACTION = document_ir.Extraction(method="test", confidence=1.0)
_FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "fixtures"


def _cell(row, column, text, row_span=1, column_span=1) -> document_ir.Cell:
    return document_ir.Cell(row=row, column=column, text=text, row_span=row_span, column_span=column_span)


def _table(element_id, parent_id, order, cells, source_map=None) -> document_ir.Element:
    return document_ir.Element(
        element_id=element_id, type="table", parent_id=parent_id, order=order,
        visibility="visible", status="active", text=None, cells=cells,
        source_map=source_map or {}, extraction=_EXTRACTION)


def _sheet(element_id, order, name, source_map=None, visibility_reason=None) -> document_ir.Element:
    sm = {"sheet": name}
    if source_map:
        sm.update(source_map)
    return document_ir.Element(
        element_id=element_id, type="sheet", parent_id=None, order=order,
        visibility=("hidden" if visibility_reason else "visible"), visibility_reason=visibility_reason,
        status="active", text=None, cells=None,
        source_map=sm, extraction=_EXTRACTION)


# ---- 結合セルの R5 展開（値を継続セルへ複製）----

def _grid(cells):
    """`_render_cells_grid` を既定予算で呼ぶ薄いラッパ（テストの呼び出しを短くする）。"""
    return human_md._render_cells_grid(cells, human_md._OutputBudget())


def test_render_cells_grid_expands_row_and_column_span():
    cells = [
        _cell(1, 1, "見出し結合", column_span=2),
        _cell(2, 1, "縦結合", row_span=2),
        _cell(2, 2, "値2"),
        _cell(3, 2, "値3"),
    ]
    grid_md = _grid(cells)
    rows = grid_md.split("\n")
    assert rows == ["| 見出し結合 | 見出し結合 |", "| 縦結合 | 値2 |", "| 縦結合 | 値3 |"]


def test_render_cells_grid_empty_returns_empty_string():
    assert _grid([]) == ""


def test_normalize_cell_text_escapes_pipe_and_newline():
    assert human_md._normalize_cell_text("a|b") == "a\\|b"
    assert human_md._normalize_cell_text("a\nb\r\nc") == "a<br>b<br>c"
    assert human_md._normalize_cell_text(None) == ""


# ---- 全量方針＋グループ分割（正典 §10 裁定#1・silent-drop なし）----

def test_render_cells_grid_splits_on_budget_without_dropping_rows(monkeypatch):
    monkeypatch.setattr(human_md, "_MAX_GROUP_CHARS", 20)
    cells = [_cell(r, 1, f"値{r}") for r in range(1, 8)]
    out = _grid(cells)
    assert "グループに分割して表示します" in out
    for r in range(1, 8):
        assert f"値{r}" in out                            # 分割されても値は必ずどこかに残る


def test_render_cells_grid_no_annotation_when_within_budget():
    cells = [_cell(1, 1, "a"), _cell(2, 1, "b")]
    out = _grid(cells)
    assert "グループ" not in out


def test_render_cells_grid_single_oversized_row_still_annotated(monkeypatch):
    """1行だけで `_MAX_GROUP_CHARS` を超える場合、グループは1つのままでも必ず注記を出す
    （グループ数だけで判定すると、この単独行超過を見逃す）。"""
    monkeypatch.setattr(human_md, "_MAX_GROUP_CHARS", 20)
    cells = [_cell(1, 1, "x" * 100)]                       # 1セルだけの巨大表＝グループは常に1つ
    out = _grid(cells)
    assert "x" * 100 in out                                 # 値は失われない
    assert "非常に大きい行が含まれる" in out                # グループ数は1でも注記が出る


def test_render_cells_grid_bounded_time_near_five_million_cells():
    """行×列が約500万セル（`excel.DEFAULT_CAP_CELLS` の安全弁の桁）に相当する巨大な結合1件でも、
    行グループ単位の逐次生成（dense matrix を事前確保しない）により現実的な時間で完走する
    （壊れた/巨大な span を与えられても O(面積) の中間構造を一括確保しない設計の回帰検知）。
    面積が `_MAX_MERGE_DUPLICATE_CELLS` を大幅に超えるため R5 複製はされない（起点セルのみ・別途
    `test_render_cells_grid_large_merge_area_skips_duplication_stays_small` が面積上限自体を検証）。
    """
    row_span, column_span = 250_000, 20                    # 250,000 x 20 = 5,000,000 セル相当
    cell = _cell(1, 1, "x", row_span=row_span, column_span=column_span)
    start = time.monotonic()
    out = _grid([cell])
    elapsed = time.monotonic() - start
    assert elapsed < 20.0, f"想定より遅い（dense matrix 事前確保への先祖返りの疑い）: {elapsed:.1f}s"
    assert "x（結合250000×20）" in out
    # 250,000行は既定予算で必ず出力予算切れ（stopped_early）になる。見出し＋本体を1単位として
    # 予算判定するため、収まった分のグループ見出しは出るが、入らなかったグループ数は
    # 予約枠からの注記で報告される（正典 §10 裁定#1 の実バイト厳密の安全弁）。
    assert "以降" in out and "グループを省略しました" in out


def test_render_cells_grid_419_rows_near_budget_boundary_no_silent_drop():
    """見出し＋本体を別々に予算判定すると、本体は単独では上限内に収まっているのに「後から
    追加するグループ見出し」の分だけ予算が尽きて本体ごと無警告で欠落する事故になる
    （419行×19,995文字＝8MiBのすぐ内側の分量で、各行がほぼ単独で1グループになりこの事故を
    誘発する境界値）。全行が出力に含まれるか、含まれない分は必ず省略数として報告される
    （無警告の欠落は作らない）ことを確認する。"""
    rows, cell_len = 419, 19_995
    cells = [_cell(r, 1, f"ROW{r:05d}:" + "x" * (cell_len - 9)) for r in range(1, rows + 1)]
    out = _grid(cells)
    shown = sum(1 for r in range(1, rows + 1) if f"ROW{r:05d}:" in out)
    assert len(out.encode("utf-8")) <= human_md._MAX_HUMAN_MD_BYTES
    if shown < rows:
        assert "省略" in out                 # 欠落があるなら必ず理由付きで報告される
    else:
        assert shown == rows


# ---- R5 展開の面積上限・出力予算（正典 §10 裁定#1/#2 の精密化）----

def test_render_cells_grid_large_merge_area_skips_duplication_stays_small():
    """32,767文字（Excelのセル最大文字数）× 10行 × 100列の結合（面積1000 > 200）は R5 複製を
    せず、MD 全体が数MBに収まる（複製していれば 32,767文字 × 1000セル ≈ 32MB になるところを防ぐ）。
    """
    huge_text = "x" * 32_767
    cell = _cell(1, 1, huge_text, row_span=10, column_span=100)
    out = _grid([cell])
    assert len(out.encode("utf-8")) < 2 * 1024 * 1024        # 数MBに収まる（複製なら32MB超）
    assert huge_text in out                                   # 値そのものは失わない
    assert "（結合10×100）" in out


def test_render_cells_grid_small_merge_still_duplicates():
    """面積が `_MAX_MERGE_DUPLICATE_CELLS` 以下の結合は従来どおり R5 展開（複製）する。"""
    cell = _cell(1, 1, "値", row_span=2, column_span=2)       # 面積4 <= 200
    out = _grid([cell])
    rows = out.split("\n")
    assert rows == ["| 値 | 値 |", "| 値 | 値 |"]
    assert "結合" not in out                                  # 複製時は面積の注記を出さない


def test_render_cells_grid_oversized_merge_annotation_appears_only_once():
    """面積が上限を超える結合は、起点セル（起点行）にだけ注記付きの値を1回出す——`end_row` まで
    `active` に残すと結合の行数ぶん同じ注記が繰り返し出てしまう回帰の検知（3行×67列＝面積201）。"""
    cell = _cell(1, 1, "値", row_span=3, column_span=67)      # 面積201 > 200
    out = _grid([cell])
    rows = out.split("\n")
    assert len(rows) == 3                                     # 3行とも出力される（穴埋めは空セル）
    annotation = "値（結合3×67）"
    assert out.count(annotation) == 1                          # 3回ではなく1回だけ
    assert annotation in rows[0]                                # 起点行（1行目）に出る
    assert rows[1] == "| " + " | ".join([""] * 67) + " |"      # 2行目は全列空欄
    assert rows[2] == "| " + " | ".join([""] * 67) + " |"      # 3行目も全列空欄


def test_output_budget_stops_generation_once_exhausted():
    """`_OutputBudget` が尽きると、以降の行グループ生成そのものを打ち切る
    （全量を単一文字列に保持してから切り詰めるのではない）。
    limit=400（reserve=200）は「この表は表示できませんでした」注記（予約枠から書く）が
    切り詰められず全文で入る最小限のサイズ——limit を極端に小さくすると注記自体が
    reserve の枠を超えて切り詰められ、注記文言の完全一致を検証できなくなる。"""
    budget = human_md._OutputBudget(limit=400)
    cells = [_cell(r, 1, f"行{r}のそこそこ長いテキストです") for r in range(1, 50)]
    out = human_md._render_cells_grid(cells, budget)
    assert budget.truncated is True
    assert "出力上限に達したため" in out
    # 予算を使い切った後の行はもう生成されていない（全ての行番号が含まれるわけではない）
    assert not all(f"行{r}のそこそこ長いテキストです" in out for r in range(1, 50))


def test_render_xlsx_sheet_output_budget_omits_remaining_tables(monkeypatch):
    """シート単位の出力上限に達したら、以降の表は省略し件数を注記する
    （grep の既定読み取り上限＝8MiB と揃えた安全弁）。"""
    monkeypatch.setattr(human_md, "_MAX_HUMAN_MD_BYTES", 100)
    sheet = _sheet("sheet:1", 1, "台帳")
    tables = [
        _table(f"table:{i}", "sheet:1", i, [_cell(1, 1, "x" * 60)], {"range": f"A{i}:A{i}"})
        for i in range(1, 4)
    ]
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.xlsx",
        source=document_ir.Source(path="x.xlsx", content_hash="sha256:0", file_type="xlsx"),
        elements=[sheet, *tables])
    md = human_md.render_xlsx(ir)
    assert md is not None
    assert "出力上限" in md
    assert "### A1:A1" in md                                  # 最初の表は出る
    assert "### A3:A3" not in md                              # 予算切れで以降は省略される


def test_render_xlsx_final_output_bounded_by_default_budget_and_note_at_end():
    """既定の 8MiB 予算を実際に超える分量を与えても、最終出力は必ず 8MiB 以内に収まり
    （見出し・注記も予算消費に含める・打切り注記は予約枠から書く）、打切り注記は文書出力の
    末尾に置かれる（予算は文書全体で1つ・シートごとにリセットしない）。"""
    sheet = _sheet("sheet:1", 1, "台帳")
    big_cell_text = "x" * 100_000                             # 1セル100KB
    tables = [
        _table(f"table:{i}", "sheet:1", i, [_cell(1, 1, big_cell_text)], {"range": f"A{i}:A{i}"})
        for i in range(1, 120)                                 # 120 * 100KB ≈ 12MB > 8MiB
    ]
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.xlsx",
        source=document_ir.Source(path="x.xlsx", content_hash="sha256:0", file_type="xlsx"),
        elements=[sheet, *tables])
    md = human_md.render_xlsx(ir)
    assert md is not None
    assert len(md.encode("utf-8")) <= human_md._MAX_HUMAN_MD_BYTES
    assert "### A1:A1" in md                                   # 最初の表は出る
    assert "出力上限" in md                                    # 打切り注記（文書単位）が出ている
    tail = md.rsplit("\n\n", 1)[-1]
    assert "出力上限" in tail                                  # 注記は文書出力の末尾に置かれる


def test_render_xlsx_budget_shared_across_sheets_not_reset_per_sheet():
    """予算は `{rel}.md` 1ファイル全体で共有し、シートごとにリセットしない（正典 §10 裁定#1の
    厳密化）。1シート目だけで 8MiB を超える分量を与えても、最終ファイルは 8,388,608 バイト
    以内に収まり、後続のシートは（1シート目の途中で予算が尽きるため）丸ごと省略される
    （シートごとにリセットするなら2・3シート目も普通に出てしまうところを防ぐ）。"""
    big_cell_text = "x" * 100_000                              # 1セル100KB
    sheets: list[document_ir.Element] = []
    tables: list[document_ir.Element] = []
    for s in range(1, 4):                                      # 3シート × 90表 ≈ 27MB > 8MiB
        sheets.append(_sheet(f"sheet:{s}", s, f"シート{s}"))    # 1シート目だけで約9MB＝単独で上限超過
        for t in range(1, 91):
            tables.append(_table(f"table:{s}:{t}", f"sheet:{s}", t,
                                  [_cell(1, 1, big_cell_text)], {"range": f"A{t}:A{t}"}))
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.xlsx",
        source=document_ir.Source(path="x.xlsx", content_hash="sha256:0", file_type="xlsx"),
        elements=[*sheets, *tables])
    md = human_md.render_xlsx(ir)
    assert md is not None
    assert len(md.encode("utf-8")) <= 8 * 1024 * 1024          # 8,388,608 bytes 以内（正典の実数値）
    assert "シート「シート1」" in md                             # 最初のシートは出る（途中までにせよ）
    assert "シート「シート2」" not in md                         # 1シート目の途中で予算切れ＝以降のシートは丸ごと省略される
    assert "シート「シート3」" not in md
    assert "出力上限" in md


def test_render_xlsx_900_row_sheet_stays_within_byte_limit(monkeypatch):
    """区切り（`"\n\n".join`）を実際のバイト数（2バイト）で計上しないと、ブロック数が多い
    （行グループが多数に分かれる）表では 1 ブロックあたりの過小計上が積み重なり、最終出力が
    名目上の上限をわずかに超えてしまう（旧実装は `+1` で見積もっていた）。900行を強制的に
    個別の行グループへ分割させ（`_MAX_GROUP_CHARS` を小さくする）、最終バイト数が
    8,388,608 以内であることを確認する。"""
    monkeypatch.setattr(human_md, "_MAX_GROUP_CHARS", 100)     # ほぼ全行が個別グループになる
    # 1行あたり約12.8KB×900行≈11.5MB＝8MiBを明確に超え、実際に途中で打ち切られる分量にする
    # （content量が上限より十分小さいと、区切りバイトの過小計上（1ブロックあたり数バイト）が
    # 積み重なっても余裕（未使用の予算）に埋もれてしまい、この境界の精度を検証できない）。
    cells = [_cell(r, 1, f"行{r:04d}のセル内容はそこそこ長めのテキストです" * 200) for r in range(1, 901)]
    sheet = _sheet("sheet:1", 1, "台帳")
    table = _table("table:1", "sheet:1", 1, cells, {"range": "A1:A900"})
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.xlsx",
        source=document_ir.Source(path="x.xlsx", content_hash="sha256:0", file_type="xlsx"),
        elements=[sheet, table])
    md = human_md.render_xlsx(ir)
    assert md is not None
    assert len(md.encode("utf-8")) <= 8 * 1024 * 1024


# ---- xlsx: シート/表候補ごとの見出し（正典 §3.1）----

def test_render_xlsx_multiple_tables_with_notes():
    sheet = _sheet("sheet:1", 1, "台帳")
    t1 = _table("table:1", "sheet:1", 1, [_cell(1, 1, "a")], {"range": "A1:A1"})
    t2 = _table("table:2", "sheet:1", 2, [_cell(1, 1, "b")],
                {"range": "C1:C1", "truncated": True, "split_budget_exhausted": True})
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.xlsx",
        source=document_ir.Source(path="x.xlsx", content_hash="sha256:0", file_type="xlsx"),
        elements=[sheet, t1, t2])
    md = human_md.render_xlsx(ir)
    assert md is not None
    assert "## シート「台帳」" in md
    assert "### A1:A1" in md and "### C1:C1" in md
    assert md.index("A1:A1") < md.index("C1:C1")          # order どおり
    assert "走査上限に達した" in md
    assert "隣接する表と癒着している可能性" in md


def test_render_xlsx_empty_sheet_gets_note_not_none():
    """表候補が0件でも `{rel}.md` は必ず生成する（見出し＋注記のみ）。「原本を開いても何も
    読めなかった」ことと「変換自体が未対応/失敗だった」ことを区別するため。"""
    sheet = _sheet("sheet:1", 1, "空シート")
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.xlsx",
        source=document_ir.Source(path="x.xlsx", content_hash="sha256:0", file_type="xlsx"),
        elements=[sheet])
    md = human_md.render_xlsx(ir)
    assert md is not None
    assert "## シート「空シート」" in md
    assert "値のあるセルが見つかりませんでした" in md


def test_render_xlsx_no_sheets_returns_none():
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.xlsx",
        source=document_ir.Source(path="x.xlsx", content_hash="sha256:0", file_type="xlsx"),
        elements=[])
    assert human_md.render_xlsx(ir) is None


def test_render_xlsx_sheet_truncated_note():
    """シート側の `truncated`（`excel.sheet_truncated` 由来）も human_md が注記する
    （表候補（`table`）側の `truncated` とは別のシグナル）。"""
    sheet = _sheet("sheet:1", 1, "台帳", source_map={"truncated": True})
    table = _table("table:1", "sheet:1", 1, [_cell(1, 1, "a")], {"range": "A1:A1"})
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.xlsx",
        source=document_ir.Source(path="x.xlsx", content_hash="sha256:0", file_type="xlsx"),
        elements=[sheet, table])
    md = human_md.render_xlsx(ir)
    assert md is not None
    assert f"{excel.DEFAULT_CAP_CELLS:,}セル" in md
    assert "走査上限（" in md


# ---- xlsx: シート可視性・画像存在の注記（HM1・正典 §8.4 の非対称是正）----

def test_render_xlsx_hidden_sheet_heading_annotated():
    """非表示シートは見出しへ平文注記が付く。可視シートには何も付かない（余計な注記の水増しをしない）。"""
    visible = _sheet("sheet:1", 1, "対象")
    hidden = _sheet("sheet:2", 2, "旧版", visibility_reason="hidden_sheet")
    very_hidden = _sheet("sheet:3", 3, "内部退避", visibility_reason="very_hidden")
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.xlsx",
        source=document_ir.Source(path="x.xlsx", content_hash="sha256:0", file_type="xlsx"),
        elements=[visible, hidden, very_hidden])
    md = human_md.render_xlsx(ir)
    assert md is not None
    assert "## シート「対象」（" not in md               # 可視シートの見出しには注記を付けない
    assert "## シート「旧版」（非表示のシートです）" in md
    assert "## シート「内部退避」（完全に非表示のシートです。通常の操作では再表示できません）" in md
    # AI の観測・内部語彙（enum 値そのもの）は一切出さない
    assert "hidden_sheet" not in md
    assert "very_hidden" not in md


def test_render_xlsx_picture_count_note():
    """画像がある（`picture_count`）シートだけに存在注記が出る。中身の解釈は含めない。"""
    with_picture = _sheet("sheet:1", 1, "対象", source_map={"picture_count": 1})
    without_picture = _sheet("sheet:2", 2, "旧版")
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.xlsx",
        source=document_ir.Source(path="x.xlsx", content_hash="sha256:0", file_type="xlsx"),
        elements=[with_picture, without_picture])
    md = human_md.render_xlsx(ir)
    assert md is not None
    assert "（画像が1枚あります。内容は原本で確認してください）" in md
    # 「旧版」セクションには画像注記が出ない（このシートに picture_count が無いため）
    old_section = md.split("## シート「旧版」")[1]
    assert "画像が" not in old_section


def test_render_xlsx_dep_markers_real_fixture_round_trip():
    """DEP-XLSX-MARKERS.xlsx の実往復（`ooxml_arm._build_xlsx_ir` → `human_md.render_xlsx`）で
    受け入れ条件を固定する: 非表示（「旧版」）・完全非表示（「内部退避」）の見出しに注記が付き、
    可視（「対象」）には付かない。画像1枚の存在注記が「対象」にだけ出る。AI 観測・内部語彙は出ない。
    """
    from sherpa.ingest.arms import ooxml_arm

    p = _FIXTURES_ROOT / "eval" / "deprecation_markers" / "inputs" / "DEP-XLSX-MARKERS.xlsx"
    if not p.is_file():
        pytest.skip(f"fixture が無い環境: {p}")
    ir = ooxml_arm._build_xlsx_ir(p)
    assert ir is not None
    md = human_md.render_xlsx(ir)
    assert md is not None
    assert "## シート「対象」" in md
    assert "## シート「旧版」（非表示のシートです）" in md
    assert "## シート「内部退避」（完全に非表示のシートです。通常の操作では再表示できません）" in md
    target_section = md.split("## シート「旧版」")[0]
    assert "（画像が1枚あります。内容は原本で確認してください）" in target_section  # 画像注記は「対象」側に出る
    other_sections = md.split("## シート「旧版」")[1]
    assert "画像が" not in other_sections               # 「旧版」「内部退避」には画像が無い
    assert "hidden_sheet" not in md
    assert "very_hidden" not in md
    assert "occluded" not in md   # 覆い判定（RAG側の判断）は human_md へ持ち込まない


# ---- docx: 出現順（`order`）とネスト表（正典 §3.2）----

def test_render_docx_preserves_body_order():
    heading = document_ir.Element(
        element_id="heading:1", type="heading", parent_id=None, order=1,
        visibility="visible", status="active", text="タイトル", cells=None,
        source_map={"level": 2}, extraction=_EXTRACTION)
    para = document_ir.Element(
        element_id="para:1", type="paragraph", parent_id=None, order=2,
        visibility="visible", status="active", text="本文です", cells=None,
        source_map={}, extraction=_EXTRACTION)
    table = _table("table:1", None, 3, [_cell(1, 1, "セル")])
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.docx",
        source=document_ir.Source(path="x.docx", content_hash="sha256:0", file_type="docx"),
        elements=[table, heading, para])                  # わざと逆順で渡す＝order でソートされることを確認
    md = human_md.render_docx(ir)
    assert md is not None
    assert md.index("## タイトル") < md.index("本文です") < md.index("| セル |")


def test_render_docx_nested_table_appears_after_host():
    outer = _table("table:1", None, 1, [_cell(1, 1, "外側セル")])
    nested = _table("table:2", "table:1", 1, [_cell(1, 1, "ネスト値")],
                    {"host_row": 1, "host_column": 1})
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.docx",
        source=document_ir.Source(path="x.docx", content_hash="sha256:0", file_type="docx"),
        elements=[outer, nested])
    md = human_md.render_docx(ir)
    assert md is not None
    assert "#### ネスト表（1行1列）" in md
    assert md.index("外側セル") < md.index("ネスト表") < md.index("ネスト値")


def test_render_docx_nested_omission_count_includes_grandchildren(monkeypatch):
    """打切りで丸ごと未着手になったネスト表は、その孫（さらに深いネスト表）も「以降 N 要素」に
    含める——直下の子だけを数えると、outer→child→grandchild の3階層で child しか数えず
    grandchild を数え漏らす（省略件数の過小報告は「無警告の情報欠落は作らない」契約に反する）。"""
    monkeypatch.setattr(human_md, "_MAX_HUMAN_MD_BYTES", 80)
    outer = _table("table:outer", None, 1, [_cell(1, 1, "外側セル")])
    child = _table("table:child", "table:outer", 1, [_cell(1, 1, "子セル")],
                   {"host_row": 1, "host_column": 1})
    grandchild = _table("table:grandchild", "table:child", 1, [_cell(1, 1, "孫セル")],
                        {"host_row": 1, "host_column": 1})
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.docx",
        source=document_ir.Source(path="x.docx", content_hash="sha256:0", file_type="docx"),
        elements=[outer, child, grandchild])
    md = human_md.render_docx(ir)
    assert md is not None
    assert "外側セル" in md
    assert "子セル" not in md and "孫セル" not in md      # child・grandchild とも丸ごと省略される
    assert "以降 2 要素を省略" in md


def test_render_docx_picture_count_note():
    """HM1 の docx 見送り分: `ir.picture_count` があれば文書冒頭に画像存在注記が出る（枚数のみ・
    内容の解釈は含めない・xlsx の存在注記と同じ文言）。"""
    para = document_ir.Element(
        element_id="para:1", type="paragraph", parent_id=None, order=1,
        visibility="visible", status="active", text="本文です", cells=None,
        source_map={}, extraction=_EXTRACTION)
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.docx",
        source=document_ir.Source(path="x.docx", content_hash="sha256:0", file_type="docx"),
        elements=[para], picture_count=2)
    md = human_md.render_docx(ir)
    assert md is not None
    assert "（画像が2枚あります。内容は原本で確認してください）" in md
    assert md.index("画像が2枚") < md.index("本文です")   # 文書冒頭に出る


def test_render_docx_no_picture_count_note_when_zero():
    para = document_ir.Element(
        element_id="para:1", type="paragraph", parent_id=None, order=1,
        visibility="visible", status="active", text="本文です", cells=None,
        source_map={}, extraction=_EXTRACTION)
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.docx",
        source=document_ir.Source(path="x.docx", content_hash="sha256:0", file_type="docx"),
        elements=[para])
    md = human_md.render_docx(ir)
    assert md is not None
    assert "画像が" not in md


def test_render_docx_hard_real_fixture_round_trip_has_picture_note():
    """OJA-DOCX-HARD.docx の実往復（`ooxml_arm._build_docx_ir` → `human_md.render_docx`）で
    画像存在注記の受け入れ条件を固定する（この fixture は本文中に画像を1枚含む）。"""
    from sherpa.ingest.arms import ooxml_arm

    p = _FIXTURES_ROOT / "eval" / "office_ja" / "inputs" / "OJA-DOCX-HARD.docx"
    if not p.is_file():
        pytest.skip(f"fixture が無い環境: {p}")
    ir = ooxml_arm._build_docx_ir(p)
    assert ir is not None
    assert ir.picture_count == 1
    md = human_md.render_docx(ir)
    assert md is not None
    assert "（画像が1枚あります。内容は原本で確認してください）" in md


def test_render_docx_returns_none_when_empty():
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.docx",
        source=document_ir.Source(path="x.docx", content_hash="sha256:0", file_type="docx"),
        elements=[])
    assert human_md.render_docx(ir) is None


def test_render_docx_paragraphs_consume_shared_budget_and_note_omitted_count(monkeypatch):
    """見出し・段落も表と同じ出力予算を消費する——表が無い文書でも、段落だけで予算を使い切ったら
    以降の要素を省略し、文書の末尾に省略件数を注記する（従来は表のセルしか予算を消費しなかった
    ため、見出し/段落だけで予算超過するケースを検知できなかった）。"""
    monkeypatch.setattr(human_md, "_MAX_HUMAN_MD_BYTES", 200)
    paras = [
        document_ir.Element(
            element_id=f"para:{i}", type="paragraph", parent_id=None, order=i,
            visibility="visible", status="active", text=f"段落{i}の本文はそこそこ長い文章です",
            cells=None, source_map={}, extraction=_EXTRACTION)
        for i in range(1, 10)
    ]
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.docx",
        source=document_ir.Source(path="x.docx", content_hash="sha256:0", file_type="docx"),
        elements=paras)
    md = human_md.render_docx(ir)
    assert md is not None
    assert "段落1の本文" in md
    assert "段落9の本文" not in md                            # 全部は残らない（予算切れで省略される）
    assert "以降" in md and "要素を省略" in md
    assert md.index("段落1の本文") < md.index("要素を省略")   # 注記は末尾側に置かれる


def test_render_docx_paragraph_exhaustion_omits_entire_later_table(monkeypatch):
    """段落だけで予算をほぼ使い切った場合、後続の表は（一部行だけでなく）まるごと省略される
    （表以外の要素の消費が表の省略判定にも正しく波及することの確認）。"""
    monkeypatch.setattr(human_md, "_MAX_HUMAN_MD_BYTES", 200)
    para = document_ir.Element(
        element_id="para:1", type="paragraph", parent_id=None, order=1,
        visibility="visible", status="active", text="段落本文" * 30,
        cells=None, source_map={}, extraction=_EXTRACTION)
    table = _table("table:1", None, 2, [_cell(1, 1, "セル")])
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.docx",
        source=document_ir.Source(path="x.docx", content_hash="sha256:0", file_type="docx"),
        elements=[para, table])
    md = human_md.render_docx(ir)
    assert md is not None
    assert "セル" not in md                                   # 表はまるごと省略される
    assert "要素を省略" in md


def test_render_docx_table_flags_produce_notes():
    """`_docx_table_walk` が付けた `flags`（列 span クランプ・vMerge 継続セルの本文救済）を
    human_md がそれぞれ別の注記として表示する。"""
    table = _table("table:1", None, 1, [_cell(1, 1, "セル")],
                   {"flags": ["docx_column_span_clamped", "docx_vmerge_text_merged"]})
    ir = document_ir.DocumentIR(
        schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="x.docx",
        source=document_ir.Source(path="x.docx", content_hash="sha256:0", file_type="docx"),
        elements=[table])
    md = human_md.render_docx(ir)
    assert md is not None
    assert "結合/列の指定が異常に大きかった" in md
    assert "継続セルに本文があった" in md
