"""人間が読める `{rel}.md` レンダラ（H2・`docs/proposals/2026-08-28-人間向けMDの刷新.md` §3.1/§3.2）。

document-ir（`sherpa/ingest/document_ir.py`）を、xlsx（`arms/ooxml_arm._build_xlsx_ir`）・docx
（`arms/ooxml_arm._build_docx_ir`）の人間向け MD 生成と共有する共通土台にする: `ooxml/excel.py::regions()`・
`arms/ooxml_arm._docx_table_walk` が解決済みの表候補・結合セル（row_span/column_span）・ネスト表位置を
そのまま消費し、独立した簡易パーサは持たない（二重実装しない）。

pptx/PDF はこのモジュールの対象外（正典 §3.3/§3.4 の裁定どおり据え置き）。`.rag.md`／`evidence_render.py`
（RAG 側の表現）には一切触れない。

**全量方針の安全弁（正典 §10 裁定#1）**: 打切りはしないが、次の3段の安全弁を持つ。
(1) 走査自体の安全弁は `excel.DEFAULT_CAP_CELLS`（500万セル）——超過した表・シートには必ず注記を出す。
(2) 結合セルは値を継続セルへ複製する展開（R5・正典 §10 裁定#2）だが、面積が `_MAX_MERGE_DUPLICATE_CELLS`
（200セル）を超える結合は複製せず起点セルに値を1回だけ出し「（結合R×C）」と注記する（32,767文字級の
セル値を巨大結合へ複製すると MD サイズが結合面積に比例して膨張するため）。
(3) 出力そのものにも `{rel}.md` **1ファイル全体**（xlsx は複数シートあってもシートごとに
リセットしない・grep はファイル単位で読むため）で `_MAX_HUMAN_MD_BYTES`（8MiB・grep の既定
読み取り上限と揃えた目安値）の上限を持ち、行グループ単位で逐次書き出しながら消費し、尽きたら
以降の生成を打ち切って注記する（全量を単一文字列に保持してから切り詰めるのではなく、生成
そのものを早期終了する）。区切り（`"\n\n".join`）・グループ見出し・分割注記も含めて実バイトで
計上する。

1表のパイプ表が `_MAX_GROUP_CHARS` を超えたら行単位でグループに分けて注記を出す（1行だけが単独で
上限を超える場合も含む・(3)の安全弁に達しない限り silent-drop はしない＝全ての行はどこかのグループに
出力される）。数式・原値・書式の表示は対象外（正典 §10 裁定#4）。

表候補が1件も無い xlsx でも `render_xlsx` は必ず非 None を返す（シート見出し＋注記のみの MD になる）:
「原本を開いても何も読めなかった」ことと「変換自体が未対応/失敗だった」ことを区別するため
（`{rel}.md` 自体が生成されないと後者と見分けが付かなくなる）。
"""
from __future__ import annotations

from . import document_ir
from .ooxml import excel

# レンダラ自体の版。`office_md._current_human_md_sig()` が docx/xlsx 抽出器の版と
# 合成し、`{rel}.derived.json` の `asset_versions.human_md` として rel ごとに記録する（単一 asset の
# 選択的再生成＝RAG-KV の drift 連鎖と同じ考え方）。この関数の出力形状（見出し/注記文言/分割規則）を
# 変えたら上げる。
# v4→v5（HM1 の docx 見送り分・2026-09-03）: `render_docx` へ画像存在注記（`ir.picture_count`）を追加。
HUMAN_MD_RENDERER_VERSION = "human-md-renderer-v5"

# 1グループ（画面に1回に出すパイプ表の塊）あたりの目安上限文字数。key-value化提案書の
# `evidence_render.MAX_GROUP_CHARS`（RAG チャンク用途・1800）とは用途が異なる（人間が画面で読む単位）
# ため独立の定数にする。通常サイズの表はまず分割されず、巨大な表だけ複数グループに分かれる程度の
# 桁を選んだ（ハード制約ではなく可読性のための目安）。
_MAX_GROUP_CHARS = 20_000

# 結合セルの R5 展開（値を継続セルへ複製）を行う面積の上限。これを超える結合は複製せず起点セルへ
# 1回だけ値を出す（正典 §10 裁定#2 の精密化）: Excel のセル値は最大 32,767 文字まで持てるため、
# 面積の大きい結合へそのまま複製すると MD サイズ・レンダリング時間が結合面積に比例して膨張する
# （例: 32,767文字 × 10行 × 100列の結合を複製すると単独で数十MBになる）。
_MAX_MERGE_DUPLICATE_CELLS = 200

# 人間向け MD の出力上限（**文書全体**・grep の既定読み取り上限＝`sherpa/grep_tool.py` の
# `_GREP_FILE_CAP_BYTES` 既定 8MiB と揃えた目安値。値は意図的に固定定数にする＝grep 側の env
# 上書きに追随させると、検索設定の変更だけで過去に生成済みの MD の妥当性が変わってしまうため）。
# 8MiB を超えて書いても grep はそれ以降を読まず実質検索不能な領域になるだけなので、生成側でも
# その手前で打ち切って注記する。**シートごとにリセットしない**（xlsx は複数シートあっても
# `{rel}.md` 1ファイル全体で 8MiB——grep が読むのはファイル単位でありシート単位ではないため、
# シートごとに予算をリセットするとファイル全体では上限を大きく超えうる）。
_MAX_HUMAN_MD_BYTES = 8 * 1024 * 1024

# シートの可視性注記（HM1・`docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md` §8.4 の非対称
# 是正）。`document_ir.Element.visibility_reason`（xlsx の `arms/ooxml_arm._build_xlsx_ir` が設定する
# `"hidden_sheet"`/`"very_hidden"`）をそのまま画面に出さず、平文の注記へ変換する——**事実（見た目上
# 非表示かどうか）だけを述べ、AI の観測・推測・内部語彙（enum 値そのもの）は一切含めない**。
_SHEET_VISIBILITY_NOTES = {
    "hidden_sheet": "（非表示のシートです）",
    "very_hidden": "（完全に非表示のシートです。通常の操作では再表示できません）",
}


def _sheet_visibility_note(reason: str | None) -> str:
    """`reason`（`Element.visibility_reason`）に対応する見出し用の平文注記。対象外の reason
    （`None`・xlsx の sheet 要素には出ない値）は空文字列（見出しに何も追加しない）。"""
    return _SHEET_VISIBILITY_NOTES.get(reason or "", "")


# 打切りの理由を説明する注記（「出力上限に達したため以降を省略しました」等・末尾の注記だけでなく、
# 個々の表が丸ごと表示できなかった時の注記も含む）自身の分をあらかじめ確保しておく予約量。
# 本文（見出し・注記・表・グループ見出し・区切りバイトを含む全て）の消費はこの分を差し引いた枠の
# 中でだけ行い、打切り理由の注記そのものは `consume()` を経由せずこの予約枠（`note()`）から書く
# ——本文の消費だけで枠を使い切ると、打切り時に付ける注記を書く余地が無くなり最終出力が上限を
# 超えてしまう（正典 §10 裁定#1 の安全弁(3)は「最終出力が上限以内」が契約）。`note()` は
# 呼び出しごとにこの枠を実際に減らす（文書内の複数の表がそれぞれ打切り注記を出しうるため、
# 固定枠を使い回すのではなく累積で管理する）。
_TRUNCATION_NOTE_RESERVE_BYTES = 1024


class _OutputBudget:
    """出力バイト数の予算を追跡する（`render_xlsx` は**文書全体**で1つ生成し、全シート・全表・
    全行グループの書き出しで共有する＝シートごとにリセットしない。`render_docx` も文書全体で
    見出し・段落・表を通じて1つ共有する）。

    本文用の枠（`remaining`）と、打切り理由の注記専用の予約枠は分離している——`consume()` は
    本文用の枠だけを減らし、`note()` は予約枠だけから書く（本文の消費が注記の置き場を奪わない）。
    """
    def __init__(self, limit: int | None = None):
        # 既定値をデフォルト引数として束縛すると def 時の `_MAX_HUMAN_MD_BYTES` に固定され、
        # テストの `monkeypatch.setattr(human_md, "_MAX_HUMAN_MD_BYTES", …)` が効かなくなる
        # （`excel.py` の cap 定数と同じ「モジュール属性は呼び出し時に読む」設計に揃える）。
        total = _MAX_HUMAN_MD_BYTES if limit is None else limit
        # 極端に小さい limit（テスト用途）では予約を半分までに抑え、本文の枠が丸ごと消える
        # 事態を避ける（実運用の 8MiB では reserve は全体の 0.01% 未満で無視できる差）。
        reserve = min(_TRUNCATION_NOTE_RESERVE_BYTES, total // 2)
        self.remaining = total - reserve
        self._note_remaining = reserve
        self.truncated = False

    def consume(self, text: str) -> bool:
        """`text` を書き出してよければ予算を消費して True。予算切れなら False を返し
        `truncated` を立てる（呼び出し側はそれ以上の生成を打ち切る）。

        `+2` は実際の結合規則（`"\n\n".join(...)`＝2バイトの区切り）に合わせた見積もり。
        各ブロックに +2 ずつ計上するのは、リスト全体の区切りバイト数（要素数-1）×2 より
        わずかに多い（安全側＝決して過小評価にならない——過小評価だけが最終出力を名目上の
        上限より超過させる実害を生む）。"""
        if self.truncated:
            return False
        size = len(text.encode("utf-8")) + 2
        if size > self.remaining:
            self.truncated = True
            return False
        self.remaining -= size
        return True

    def note(self, text: str) -> str:
        """予約枠から注記を書き出す（`consume()` の本文枠とは別会計・複数回呼べる）。

        「打切りの理由を説明する注記」（例: 出力上限に達した旨の末尾注記・表単位の打切り注記）は、
        本文用の枠（`remaining`）が既に尽きているまさにその場面でこそ必要になる——`consume()`
        経由だと `truncated` 済みで常に失敗し、最も必要な時に注記が消える。呼び出しごとに
        `_note_remaining` を実際に減らす（単純に固定枠を毎回使い回すと、複数の表がそれぞれ
        打切り注記を出す文書で予約枠を合計超過しうるため）。枠を使い切ったら以降は空文字列
        （既に書いた注記はそのまま・新たに足さないだけ）。単発の注記が残り枠を超える場合は
        UTF-8 境界を壊さないよう安全側へ切り詰める。
        """
        if self._note_remaining <= 0:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) > self._note_remaining:
            encoded = encoded[: self._note_remaining]
            while encoded and (encoded[-1] & 0xC0) == 0x80:  # UTF-8 継続バイトの途中で切らない
                encoded = encoded[:-1]
        self._note_remaining -= len(encoded)
        return encoded.decode("utf-8", errors="ignore")


def render_xlsx(ir: document_ir.DocumentIR) -> str | None:
    """xlsx の document-ir から人間向け MD を生成する（正典 §3.1）。`ir` にシートが1つも無ければ None
    （document-ir 自体が構築できていない＝呼び出し元の異常）だが、シートはあるが表候補が0件（空シート
    のみ）でも None は返さない＝見出し＋注記だけの MD になる（モジュール docstring 参照）。

    シートごとに `## シート「{名前}」` を出し、その下へ `regions()` が検出した表候補ごとに
    `### {セル範囲}` の小見出し＋パイプ表を並べる（シート丸ごと1枚のパイプ表ではない）。**見出し・
    注記・表・区切り（`"\n\n".join`）のすべて**が `_MAX_HUMAN_MD_BYTES` 予算を共有する（表の
    セル本文だけでなく、シート見出しや各表の見出し/注記も予算を消費する——さもないと表以外の
    部分だけで予算を超えても誰も検知できない）。**予算は文書全体で1つ・シートごとにリセットしない**
    （`{rel}.md` は1ファイル全体で grep の読み取り上限に収める契約——シートごとにリセットすると
    複数シートを持つ xlsx でファイル全体が上限を超えうる）。予算切れ以降は現在の表だけでなく
    **残りのシートも丸ごと省略**し、文書の末尾に1回だけ注記する。打切り注記は予約枠
    （`_OutputBudget.note`）から書くため、最終出力は必ず `_MAX_HUMAN_MD_BYTES` 以内に収まる。
    """
    sheets = [e for e in ir.elements if e.type == "sheet"]
    if not sheets:
        return None
    budget = _OutputBudget()
    out: list[str] = []
    omitted_sheets = 0
    for si, sheet in enumerate(sheets):
        if budget.truncated:
            omitted_sheets = len(sheets) - si
            break
        sheet_parts: list[str] = []
        heading = f"## シート「{sheet.source_map.get('sheet', '')}」" + _sheet_visibility_note(sheet.visibility_reason)
        if budget.consume(heading):
            sheet_parts.append(heading)
        if sheet.source_map.get("truncated"):
            note = (f"（注記: このシートは走査上限（{excel.DEFAULT_CAP_CELLS:,}セル）を超えるため、"
                     "一部の内容を省略しました）")
            if budget.consume(note):
                sheet_parts.append(note)
        picture_count = sheet.source_map.get("picture_count")
        if picture_count:
            # HM1: 画像の「存在」だけの事実（枚数）。内容の解釈・OCR/VLM 観測は絶対に載せない
            # （正典の裁定＝人間向けMDへAIの候補事実を混ぜない）。
            note = f"（画像が{picture_count}枚あります。内容は原本で確認してください）"
            if budget.consume(note):
                sheet_parts.append(note)
        tables = sorted(
            (e for e in ir.elements if e.type == "table" and e.parent_id == sheet.element_id),
            key=lambda e: e.order)
        if not tables:
            note = "（このシートには値のあるセルが見つかりませんでした）"
            if budget.consume(note):
                sheet_parts.append(note)
        else:
            for table in tables:
                if budget.truncated:
                    break
                rendered = _render_xlsx_table(table, budget)
                if rendered:
                    sheet_parts.append(rendered)
                elif budget.truncated:
                    break
        if sheet_parts:
            out.append("\n\n".join(sheet_parts))
        elif budget.truncated:
            omitted_sheets = len(sheets) - si   # 見出しすら入らなかった＝このシートから丸ごと省略
            break
    if budget.truncated:
        mib = _MAX_HUMAN_MD_BYTES // (1024 * 1024)
        note = f"（注記: 出力上限（{mib}MiB）に達したため、以降の内容を省略しました"
        note += f"・未表示のシート {omitted_sheets} 件）" if omitted_sheets else "）"
        out.append(budget.note(note))
    return "\n\n".join(out)


def _render_xlsx_table(table: document_ir.Element, budget: "_OutputBudget") -> str:
    if budget.truncated:
        return ""
    sm = table.source_map
    parts: list[str] = []
    heading = f"### {sm.get('range', '')}"
    if not budget.consume(heading):
        return ""
    parts.append(heading)
    for note in _table_notes(sm):
        if not budget.consume(note):
            break
        parts.append(note)
    grid = _render_cells_grid(table.cells or [], budget)
    if grid:
        parts.append(grid)
    return "\n\n".join(p for p in parts if p)


def _count_descendant_elements(element_id: str | None, by_parent: dict) -> int:
    """`element_id` の子孫要素数（自分自身は含まない・再帰）。

    打切りで丸ごと未着手のまま省略した部分木の要素数を「以降 N 要素」の N へ正しく反映する
    ために使う——直下の子だけを数えると、ネスト表がさらにネスト表を持つ（孫・曾孫…）場合に
    数え漏らし、省略件数が過小報告になる（outer→child→grandchild の3階層なら child だけでなく
    grandchild も数える必要がある）。
    """
    total = 0
    for child in by_parent.get(element_id, []):
        total += 1 + _count_descendant_elements(child.element_id, by_parent)
    return total


def _count_elements_with_descendants(elements, by_parent: dict) -> int:
    """`elements`（それぞれ自分自身）＋各々の子孫を合算した要素数。

    一度も着手していない部分木（見出し・段落・表の並びの残り、またはネスト表の兄弟の残り）を
    まとめて省略件数に計上する時に使う。
    """
    return sum(1 + _count_descendant_elements(e.element_id, by_parent) for e in elements)


def render_docx(ir: document_ir.DocumentIR) -> str | None:
    """docx の document-ir から人間向け MD を生成する（正典 §3.2）。本文が1件も無ければ None。

    見出し・段落・表を原本の出現順（`Element.order`）どおりに並べる。表は `_docx_table_walk` が
    解決済みの row_span/column_span をそのまま展開したパイプ表にし、ネスト表は直後に小見出し付きで
    続ける（Markdown のパイプ表はセル内に表を持てないため）。**見出し・段落・表のすべて**が文書
    全体で `_MAX_HUMAN_MD_BYTES` を共有する（表だけでなく見出し/段落も消費する——さもないと見出し・
    段落だけで予算を超えるケースを見逃す）。予算切れ以降の要素は出力せず、文書の末尾へ
    「（以降 N 要素を省略）」と注記する（予約枠から書くため必ず収まる・`_OutputBudget.note`）。
    `N` はトップレベルで省略した要素だけでなく、**ネスト表の中で省略した要素（孫・曾孫…を含む
    部分木すべて）も含む**（`_count_elements_with_descendants` 参照・`omitted` カウンタを
    再帰全体で共有し `_render_docx_element` 自身が加算する）。

    `ir.picture_count`（HM1・xlsx の画像存在注記の docx 版）: docx はシート等の中間スコープを
    持たないため、文書冒頭に1回だけ「画像がN枚あります」の事実（枚数のみ・内容の解釈は載せない＝
    xlsx と同じ方針）を出す。
    """
    by_parent: dict[str | None, list[document_ir.Element]] = {}
    for e in ir.elements:
        if e.type in ("heading", "paragraph", "table"):
            by_parent.setdefault(e.parent_id, []).append(e)
    top = sorted(by_parent.get(None, []), key=lambda e: e.order)
    if not top:
        return None
    budget = _OutputBudget()
    out: list[str] = []
    if ir.picture_count:
        # HM1: 画像の「存在」だけの事実（枚数）。内容の解釈・OCR/VLM 観測は絶対に載せない
        # （xlsx の render_xlsx と同じ方針）。
        note = f"（画像が{ir.picture_count}枚あります。内容は原本で確認してください）"
        if budget.consume(note):
            out.append(note)
    omitted = [0]                                     # ミュータブルな1要素リスト＝再帰全体で共有するカウンタ
    for i, e in enumerate(top):
        if budget.truncated:
            omitted[0] += _count_elements_with_descendants(top[i:], by_parent)
            break
        rendered = _render_docx_element(e, by_parent, budget, omitted)
        if rendered:
            out.append(rendered)
        # rendered が空文字列でも、_render_docx_element 自身が自己申告済み（下記 docstring の
        # 自己申告契約）なのでここでは何もしない＝二重計上しない。
    if budget.truncated:
        out.append(budget.note(f"（以降 {omitted[0]} 要素を省略）"))
    return "\n\n".join(out) if out else None


def _render_docx_element(e: document_ir.Element, by_parent: dict, budget: "_OutputBudget",
                          omitted: list[int]) -> str:
    """**自己申告契約**: 予算切れが原因で自分自身の出力が空になった場合、自分自身の分
    （＋見出し/段落は子を持たないため常に1・表はまだ着手していない子孫も含めて）を
    `omitted[0]` へ加算してから `""` を返す。呼び出し元は戻り値が空文字列でもこの自己申告を
    信頼し、二重に加算しない——子を再帰呼び出しした場合の省略は子自身の自己申告に任せ、
    呼び出し元は「着手すらしなかった兄弟（＋その子孫）」だけを
    `_count_elements_with_descendants` でまとめて計上する。
    """
    if budget.truncated:
        return ""
    if e.type == "heading":
        level = max(1, min(6, e.source_map.get("level") or 1))
        text = "#" * level + " " + (e.text or "")
        if budget.consume(text):
            return text
        omitted[0] += 1
        return ""
    if e.type == "paragraph":
        text = e.text or ""
        if not text:
            return ""
        if budget.consume(text):
            return text
        omitted[0] += 1
        return ""
    parts: list[str] = []
    for note in _table_notes(e.source_map):
        if not budget.consume(note):
            # 表そのもの（自分自身）と、まだ着手していない子孫（グリッド・ネスト表）を
            # 丸ごと省略する——ここで早期returnするとネスト表のループにも到達しないため。
            omitted[0] += 1 + _count_descendant_elements(e.element_id, by_parent)
            return ""
        parts.append(note)
    grid = _render_cells_grid(e.cells or [], budget)
    if grid:
        parts.append(grid)
    nested_list = sorted(by_parent.get(e.element_id, []), key=lambda c: c.order)
    for j, nested in enumerate(nested_list):
        if budget.truncated:
            omitted[0] += _count_elements_with_descendants(nested_list[j:], by_parent)
            break
        host_row = nested.source_map.get("host_row")
        host_col = nested.source_map.get("host_column")
        heading = f"#### ネスト表（{host_row}行{host_col}列）"
        if not budget.consume(heading):
            omitted[0] += _count_elements_with_descendants(nested_list[j:], by_parent)
            break
        nested_text = _render_docx_element(nested, by_parent, budget, omitted)
        if nested_text:
            parts.append(heading)
            parts.append(nested_text)
        # nested_text が空文字列でも nested 自身が自己申告済み（二重計上しない）。
    if not parts and budget.truncated:
        omitted[0] += 1     # 表自体は何も出せなかった（ネストの省略は上のループで個別に計上済み）
    return "\n\n".join(p for p in parts if p)


# `Element.source_map` の異常系フラグ → 注記文の対応表（xlsx の `truncated`/`split_budget_exhausted`・
# docx の `flags`（`arms/ooxml_arm._docx_table_walk` 参照）を共通の1関数で注記化する。
_FLAG_NOTES = {
    "docx_column_span_clamped": "結合/列の指定が異常に大きかったため、表示上の範囲を制限しました",
    "docx_row_span_clamped": "縦結合の指定が表の行数を超えていたため、範囲を制限しました",
    "docx_vmerge_text_merged": "結合セルの継続セルに本文があったため、結合の先頭セルへ統合しました",
    "docx_column_overflow_dropped": "表の列数が上限（63列）を超えたため、超過分の列を省略しました",
}


def _table_notes(sm: dict) -> list[str]:
    notes = []
    if sm.get("truncated"):
        notes.append("（注記: 走査上限に達したため、この表の続きが省略されている可能性があります）")
    if sm.get("split_budget_exhausted"):
        notes.append("（注記: 隣接する表と癒着している可能性があります）")
    for flag in sm.get("flags", []):
        text = _FLAG_NOTES.get(flag)
        if text:
            notes.append(f"（注記: {text}）")
    return notes


def _render_cells_grid(cells: list[document_ir.Cell], budget: "_OutputBudget") -> str:
    """位置付きセル配列（結合の起点のみ・row_span/column_span 付き）を、パイプ表にする。

    **R5 展開（値の複製）は面積が `_MAX_MERGE_DUPLICATE_CELLS` 以下の結合だけ**（正典 §10 裁定#2 の
    精密化）: それを超える結合は起点セルに値を1回だけ出し「（結合R×C）」と注記する（複製しない）。

    **行グループ単位の逐次生成**: `grid[row][col]` の密な二次元配列を先に確保してから
    行を組み立てるのではなく、行を `min_row`→`max_row` の順に1行ずつ生成し、その場で
    `_MAX_GROUP_CHARS` グループへ振り分ける（sweep-line）。結合セルの範囲は `active`（列→(値,終了行)）
    だけで追跡するため、保持する状態は「今アクティブな列の集合」に比例するだけで済み、行数×列数に
    比例した中間構造を作らない。

    **グループごとに見出し＋本体を1単位として予算判定する**（正典 §10 裁定#1）: 本体と見出しを
    別々に `consume()` すると、本体単独では上限内に収まる行グループでも「見出し分の追加消費で
    予算が尽きた」という理由で本体ごと無警告に欠落しうる——見出しは「全何グループか」が
    全行を処理し終えるまで分からないため、確定した時点の逐次番号だけを含む「#### グループN」を
    本体と1つの文字列として `consume()` する（1グループしか無かったと後で判明すれば見出し
    自体を出力しない・consume 済みの余白は無駄になるが安全側）。入らなかったグループは
    省略数（`omitted_groups`）へ自己申告し、そこで生成を打ち切る（全量を単一文字列に保持
    してから切り詰めるのではない）。

    起点が持たない座標（不整形な入力の穴）は空セルで埋める（行ごとの列数を揃え、パイプ表として
    崩れないようにする＝値の欠落ではなく表示上の穴埋め）。
    """
    if not cells:
        return ""
    if budget.truncated:
        return ""
    min_row = min(c.row for c in cells)
    max_row = max(c.row + c.row_span - 1 for c in cells)
    min_col = min(c.column for c in cells)
    max_col = max(c.column + c.column_span - 1 for c in cells)

    by_start_row: dict[int, list[document_ir.Cell]] = {}
    for c in cells:
        by_start_row.setdefault(c.row, []).append(c)

    active: dict[int, tuple[str, int]] = {}    # column -> (text, end_row)
    shown: list[tuple[str, str]] = []          # 確定して収まった (見出し, 本体) の並び
    group_index = 0                            # 逐次のグループ番号（1始まり・全体数は最後まで不明）
    total_rows_shown = 0
    omitted_groups = 0
    current: list[str] = []
    current_chars = 0
    any_oversized = False
    stopped_early = False

    def _flush(rows: list[str]) -> bool:
        nonlocal group_index, total_rows_shown, omitted_groups
        group_index += 1
        body = "\n".join(rows)
        header = f"#### グループ{group_index}"
        if not budget.consume(f"{header}\n\n{body}"):     # 見出し＋本体を1単位として判定（二重計上を避ける）
            omitted_groups += 1
            return False
        shown.append((header, body))
        total_rows_shown += len(rows)
        return True

    for r in range(min_row, max_row + 1):
        for c in by_start_row.get(r, ()):
            end_row = r + c.row_span - 1
            area = c.row_span * c.column_span
            if area <= _MAX_MERGE_DUPLICATE_CELLS:
                text = _normalize_cell_text(c.text)
                for col in range(c.column, c.column + c.column_span):
                    active[col] = (text, end_row)
            else:
                # 面積が大きすぎる結合は複製しない: 起点セル（起点行・起点列）にだけ値＋注記を
                # 1回だけ出す。`end_row`（結合の最終行）ではなく `r`（今の行）を終了行として
                # `active` に入れる——`end_row` のまま残すと、この行から結合の最終行までの
                # 全ての行で同じ注記付きの値が繰り返し出てしまう（起点セル以外は空欄のまま
                # にする、という方針が崩れる）。
                text = f"{_normalize_cell_text(c.text)}（結合{c.row_span}×{c.column_span}）"
                active[c.column] = (text, r)
        row_str = "| " + " | ".join(
            active.get(col, ("", 0))[0] for col in range(min_col, max_col + 1)) + " |"
        row_chars = len(row_str) + 1
        if row_chars > _MAX_GROUP_CHARS:
            any_oversized = True                # 正典 §10 裁定#1: 1行だけの超過も分割注記の対象にする
        if current and current_chars + row_chars > _MAX_GROUP_CHARS:
            if not _flush(current):
                stopped_early = True
                break
            current, current_chars = [], 0
        current.append(row_str)
        current_chars += row_chars
        for col_key in [k for k, (_, er) in active.items() if er <= r]:
            del active[col_key]
    else:
        if current:
            if not _flush(current):
                stopped_early = True
    return _render_groups(budget, shown, any_oversized, stopped_early,
                          total_rows_shown=total_rows_shown, omitted_groups=omitted_groups)


def _normalize_cell_text(text: str | None) -> str:
    """パイプ表1セル分として安全な1行文字列にする（改行は `<br>`・`|` はエスケープ）。値は失わない。"""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return t.replace("|", "\\|")


def _render_groups(budget: "_OutputBudget", shown: list[tuple[str, str]], any_oversized: bool,
                    stopped_early: bool, *, total_rows_shown: int, omitted_groups: int) -> str:
    """`_render_cells_grid` が確定させた `(見出し, 本体)` の並びを最終的なパイプ表テキストへ直列化する。

    `shown` の各要素は既に `budget.consume()` 済み（見出し＋本体を1単位として）——ここでは
    再消費しない（二重計上を避ける）。複数グループある場合だけ各グループの見出しと、冒頭に
    「Nグループに分割して表示します」の要約注記を添える（1グループしか無ければ見出し無しで
    本体だけを返す＝旧来の見た目を維持）。

    追加する注記自体の予算の取り方は `stopped_early` で分ける: **通常時**（`stopped_early=False`）
    は要約/警告注記も他の本文と同じ `consume()` で計上する（正典 §10 裁定#1 の実バイト厳密）。
    **`stopped_early` 時**は `budget.truncated` が既に True（直前の `_flush` が `consume()` に
    失敗した結果）で、以後どんな `consume()` も即座に失敗するため、打切り理由の注記だけは
    `budget.note()`（予約枠・呼び出しごとに実際に減る有限の枠）から書く——本文の枠が尽きた
    まさにその場面で必要になる注記を `consume()` 経由にすると、最も必要な時に消えてしまう。
    """
    def _try_add(text: str) -> str:
        if stopped_early:
            return budget.note(text)
        return text if budget.consume(text) else ""

    if not shown:
        if not stopped_early:
            return ""
        return budget.note("（注記: 出力上限に達したため、この表は表示できませんでした）")

    if len(shown) == 1 and not any_oversized and not stopped_early and omitted_groups == 0:
        return shown[0][1]                                  # 唯一・打切りなし・オーバーサイズなし＝素の本体

    parts: list[str] = []
    multi = len(shown) > 1 or omitted_groups > 0
    if multi:
        summary = (f"（注記: 表が大きいため複数グループに分割して表示します。ここまでの "
                   f"{total_rows_shown} 行）" if stopped_early else
                   f"（注記: 表が大きいため {len(shown)} グループに分割して表示します。"
                   f"全 {total_rows_shown} 行）")
        note = _try_add(summary)
        if note:
            parts.append(note)
        for header, body in shown:
            parts.append(header)
            parts.append(body)
    else:
        if any_oversized:
            note = _try_add("（注記: この表には非常に大きい行が含まれるため、表示が崩れる可能性があります）")
            if note:
                parts.append(note)
        parts.append(shown[0][1])                            # 見出し無し（1グループのみ）
    if stopped_early:
        note = (budget.note(f"（注記: 出力上限に達したため、以降 {omitted_groups} グループを省略しました）")
                if omitted_groups else
                budget.note("（注記: 出力上限に達したため、この表の続きを省略しました）"))
        if note:
            parts.append(note)
    return "\n\n".join(parts)
