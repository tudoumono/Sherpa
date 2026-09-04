"""H3（SC-4 接続）: 利用者向け引用の本文を、rag チャンクの locator/chunk_id から人間向け MD の
該当節へ引き直す（表示専用の後処理）。

正典: `docs/proposals/2026-08-22-検索接続切替.md` §9（SC-4 への追加要件・ユーザー裁定 2026-08-23）・
`docs/proposals/2026-08-28-人間向けMDの刷新.md` §7（H3）。

契約（§9 原文）: 「該当箇所（利用者が読む抜粋）の本文は決定的 `{rel}.md`（再現優先）から出す。
検索と AI が読むのは rag.md のままで、人に見せる抜粋だけ決定的 MD の該当節へ引き直す。対応が
取れない例外だけ rag 文へフォールバックし、その旨を小さく表示する」。**検索・スコアリング・AI が
読む本文（rag.md/rag_chunks.jsonl）は一切変えない**——本モジュールはそれらを読むだけ（read-only）。

対応付けの手順（xlsx が本命・人間向けMD刷新提案書 §8「H3」）:
1. chunk_id を特定する（ES ヒットは既に持つ／grep ヒットは rag.md の行範囲からアンカー
   `<!-- chunk:{chunk_id} -->` を逆引きする＝`_chunk_id_from_rag_md_span`）。
2. `{rel}.rag_chunks.jsonl` から該当 chunk_id の `region_context`（`sheet`/`cell_range`＝表の
   範囲そのもの）を引く（`_region_for_chunk`）。cell 単位の citation.locator ではなく
   region_context を使う理由: citation.locator は個々のセル座標（例 "B12"）で人間向け MD の
   `### {表の範囲}` 見出しとは直接一致しない一方、region_context.cell_range は表そのものの範囲
   （例 "A1:C4"）で人間向け MD の見出しと**文字列として完全一致**する（`evidence_render.py`
   `_region_context`/`human_md.py::_render_xlsx_table` が同じ `openpyxl.utils.get_column_letter`
   表現を使うため）——座標のコンテインメント判定を自前実装せずに済む。
3. 人間向け `{rel}.md`（`derived_md_dir`・legacy/human_md 層）の `## シート「{sheet}」` 配下で
   `### {cell_range}` に完全一致する節を返す（`_find_human_md_section`）。

docx/pptx（`region_context.sheet` を持たない・H2 は xlsx/docx のみでpptxはそもそも人間MD未対応）は
対象外——「無理な断定をしない」（正典どおり）。`resolve_human_excerpt` は対応が取れない入力に対し
常に `None` を返す（呼び出し側は元の rag 文へフォールバックする）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import es_index, grep_tool, worlds

# rag_chunks.jsonl（1文書分）の読み取り安全弁。`es_index._RAG_CHUNKS_FILE_CAP_BYTES`（生成側が
# 1文書分でこの上限を超えたら索引自体を無効化する契約）と同じ値に揃える——表示側だけこれより
# 緩い/厳しい上限を持つと、索引された/されないの境界と表示可否の境界がずれる。
_RAG_CHUNKS_SCAN_CAP_BYTES = es_index._RAG_CHUNKS_FILE_CAP_BYTES
# rag.md（アンカー逆引き用）／人間向け MD の読み取り安全弁。`human_md._MAX_HUMAN_MD_BYTES`
# （生成側の1ファイル出力上限＝8MiB）と揃える。
_MD_SCAN_CAP_BYTES = 8 * 1024 * 1024

_SHEET_HEADING_RE = re.compile(r"^##\s+シート「(.+?)」")
_TABLE_HEADING_RE = re.compile(r"^###\s+(.+)$")
_SHEET_LOCATOR_RE = re.compile(r"シート「(.+?)」")


def _valid_doc_id(doc_id) -> bool:
    if not isinstance(doc_id, str) or not doc_id or doc_id.startswith("/") or "\\" in doc_id or "\x00" in doc_id:
        return False
    parts = doc_id.split("/")
    return ".." not in parts and "" not in parts


def _confined_path(root: Path, cand: Path) -> Path | None:
    """`cand` が `root` 配下に閉じ込められているかを検証した実パス（symlink 脱出・traversal 拒否）。

    `agentic_search._safe_doc_path` の「字面パスと resolve 済みパスの突き合わせ」ほど厳密ではない
    （doc_id はここに至るまでに呼び出し側で既に検証済みの引用データという前提の上の防御的二重確認・
    プライマリの読み取り専用境界は `agentic_search._safe_doc_path`／`grep_tool` が担う）。
    """
    try:
        rr = root.resolve()
        rp = cand.resolve()
        if not (rp == rr or rp.is_relative_to(rr)):
            return None
    except OSError:
        return None
    return rp


def _read_capped(path: Path, cap_bytes: int) -> str | None:
    """`path` を読む。サイズ超過・不在・symlink・読み取り失敗は None（fail-closed・呼び出し側はフォールバック）。"""
    try:
        if not path.is_file() or path.is_symlink():
            return None
        if path.stat().st_size > cap_bytes:
            return None
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None


def sheet_from_locator(locator, section_path=None) -> str | None:
    """locator（`sheet` キー）／`section_path`（`シート「name」` 形式の先頭要素）からシート名を推定する。"""
    if isinstance(locator, dict):
        sheet = locator.get("sheet")
        if isinstance(sheet, str) and sheet:
            return sheet
    if isinstance(section_path, list) and section_path:
        head = section_path[0]
        if isinstance(head, str):
            m = _SHEET_LOCATOR_RE.search(head)
            if m:
                return m.group(1)
    return None


def _rag_chunks_path(world: str, doc_id: str) -> Path | None:
    if not _valid_doc_id(doc_id):
        return None
    root = worlds.derived_rag_dir(world)
    if not root:
        return None
    root = Path(root)
    return _confined_path(root, root / (doc_id + ".rag_chunks.jsonl"))


def _region_for_chunk(world: str, doc_id: str, chunk_id: str) -> dict | None:
    """`{rel}.rag_chunks.jsonl` から `chunk_id` の `region_context`（sheet/cell_range 持ち）を引く。
    best-effort（生成側フォーマット不整合・不在は None）。"""
    if not isinstance(chunk_id, str) or not chunk_id:
        return None
    p = _rag_chunks_path(world, doc_id)
    if p is None:
        return None
    text = _read_capped(p, _RAG_CHUNKS_SCAN_CAP_BYTES)
    if text is None:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("chunk_id") != chunk_id:
            continue
        region = row.get("region_context")
        if (isinstance(region, dict) and isinstance(region.get("sheet"), str) and region.get("sheet")
                and isinstance(region.get("cell_range"), str) and region.get("cell_range")):
            return region
        return None
    return None


def _rag_md_path(world: str, doc_id: str) -> Path | None:
    """grep がこの doc_id を検索した際に実際に読んだのが rag.md かどうかも確認する（`preferred_derived_name`
    を grep_search と共有＝§3.5 の「同じ1ファイルを見る」規則をここでも守る）。legacy md しか無い/
    rag 優先が無効なときは None（=呼び出し元は rag.md のアンカー逆引きを試みない）。"""
    if not _valid_doc_id(doc_id):
        return None
    if not grep_tool.rag_grep_enabled():
        return None
    rag_root = worlds.derived_rag_dir(world)
    if not rag_root:
        return None
    rag_root = Path(rag_root)
    preferred = grep_tool.preferred_derived_name(rag_root, doc_id)
    if not preferred.endswith(grep_tool._RAG_SUFFIX):
        return None
    return _confined_path(rag_root, rag_root / preferred)


def _chunk_id_from_rag_md_span(world: str, doc_id: str, span) -> str | None:
    """grep ヒットの span（rag.md の行範囲・1-based・両端含む）を、その節を生成した chunk_id へ
    逆引きする。

    `grep_tool.grep_search` の MD 節検出（`_emit_md_section`）は「#」で始まる行を境界にする——rag.md
    のアンカー `<!-- chunk:{chunk_id} -->` は「#」で始まらないため境界にならず、**次の**レコードの
    見出し直前に置かれたアンカーが今のレコードの節の末尾（`span[1]`）に含まれてしまう（`evidence_
    render.py::_markdown` はアンカー→（同一section なら）見出し省略→次のレコード本文、の順で出す
    ため、あるレコードの `span` の終端行は次のレコードのアンカー行と重なりうる）。したがって
    「終端行までの最後のアンカー」ではなく、**節の開始行（`span[0]`＝見出し行自身）までの最後の
    アンカー**を使う——アンカーは常にそのレコードの見出しより前に出るため、これが正しい対応になる。
    """
    if not (isinstance(span, (list, tuple)) and len(span) == 2):
        return None
    s, _e = span
    if not isinstance(s, int) or isinstance(s, bool) or s < 1:
        return None
    rag_md_path = _rag_md_path(world, doc_id)
    if rag_md_path is None:
        return None
    text = _read_capped(rag_md_path, _MD_SCAN_CAP_BYTES)
    if text is None:
        return None
    current = None
    for i, line in enumerate(text.splitlines(), start=1):
        if i > s:
            break
        cid = es_index.rag_md_anchor_chunk_id(line)
        if cid is not None:
            current = cid
    return current


def _resolve_human_md_path(world: str, doc_id: str) -> Path | None:
    if not _valid_doc_id(doc_id):
        return None
    root = worlds.derived_md_dir(world)
    if not root:
        return None
    root = Path(root)
    return _confined_path(root, root / (doc_id + ".md"))


def _find_human_md_section(markdown: str, sheet: str, cell_range: str) -> dict | None:
    """人間向け MD の `## シート「{sheet}」` 配下で `### {cell_range}` に完全一致する節を1つ返す
    （`{"heading": 表示用見出し, "text": 見出し込みの節本文}`）。見つからなければ None。"""
    in_sheet = False
    heading: str | None = None
    buf: list[str] = []
    result: dict | None = None

    def _flush() -> None:
        nonlocal result
        if result is None and heading is not None:
            body = "\n".join(buf).strip()
            if body:
                result = {"heading": heading.removeprefix("### ").strip(),
                          "text": (heading + "\n\n" + body).strip()}

    for line in markdown.splitlines():
        if line.startswith("## "):
            _flush()
            m = _SHEET_HEADING_RE.match(line)
            in_sheet = bool(m and m.group(1) == sheet)
            heading, buf = None, []
            continue
        if line.startswith("### "):
            _flush()
            m = _TABLE_HEADING_RE.match(line)
            range_text = m.group(1).strip() if m else None
            if in_sheet and range_text == cell_range:
                heading, buf = line, []
            else:
                heading, buf = None, []
            continue
        if heading is not None:
            buf.append(line)
    _flush()
    return result


def resolve_human_excerpt(world: str, doc_id: str, *, chunk_id: str | None = None, span=None,
                          locator=None, section_path=None) -> dict | None:
    """成功時 `{"text": 節本文（見出し込み）, "hint": 位置ヒント文字列|None}`。対応が取れなければ
    None（呼び出し側は元の rag 文へフォールバックする・§9 契約）。"""
    if chunk_id is None:
        chunk_id = _chunk_id_from_rag_md_span(world, doc_id, span)
    if chunk_id is None:
        return None
    region = _region_for_chunk(world, doc_id, chunk_id)
    if region is None:
        return None
    sheet, cell_range = region["sheet"], region["cell_range"]
    md_path = _resolve_human_md_path(world, doc_id)
    if md_path is None:
        return None
    text = _read_capped(md_path, _MD_SCAN_CAP_BYTES)
    if text is None:
        return None
    section = _find_human_md_section(text, sheet, cell_range)
    if section is None:
        return None
    from . import citations
    hint = citations.locator_hint({"sheet": sheet, "cell_range": cell_range}) or None
    return {"text": section["text"], "hint": hint}


def display_quote(world: str, doc_id: str, fallback_quote: str, *, chunk_id: str | None = None,
                  span=None, locator=None, section_path=None) -> dict:
    """利用者向け引用本文を解決する。返り値は必ず
    `{"quote": str, "excerpt_source": "human_md"|"rag", "locator_hint": str|None}`。

    人間MD該当節が引ければ quote をその節本文へ差し替え、excerpt_source="human_md"。引けなければ
    quote は `fallback_quote`（rag.md/legacy 由来の元の本文）のまま、excerpt_source="rag"
    （§9「対応が取れない例外だけ rag 文へフォールバック」）。locator_hint は SC-3 の位置ヒント
    （`citations.locator_hint`）を、対応可否に関わらず可能な限り添える（sheet が locator/section_path
    のどちらからも取れないときは None）。
    """
    section = resolve_human_excerpt(world, doc_id, chunk_id=chunk_id, span=span,
                                    locator=locator, section_path=section_path)
    if section is not None:
        return {"quote": section["text"], "excerpt_source": "human_md", "locator_hint": section.get("hint")}
    from . import citations
    hint = None
    sheet = sheet_from_locator(locator, section_path)
    if sheet:
        cell_range = locator.get("cell_range") if isinstance(locator, dict) else None
        merged = {"sheet": sheet}
        if cell_range:
            merged["cell_range"] = cell_range
        hint = citations.locator_hint(merged) or None
    elif isinstance(locator, dict):
        hint = citations.locator_hint(locator) or None
    return {"quote": fallback_quote, "excerpt_source": "rag", "locator_hint": hint}
