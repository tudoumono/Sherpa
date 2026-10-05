"""利用者向け引用の本文を、rag チャンクの locator/chunk_id から人間向け MD の該当節へ引き直す（表示専用）。
設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」

検索・スコアリング・AI が読む本文（rag.md/rag_chunks.jsonl）は変えない（read-only）。
手順:
① chunk_id を特定する（ES ヒットは持っている／grep ヒットは rag.md の行範囲からアンカーを逆引き＝`_chunk_id_from_rag_md_span`）。
② `{rel}.rag_chunks.jsonl` から該当 chunk の `region_context`（sheet/cell_range）を引く（`_region_for_chunk`）。
③ 人間向け `{rel}.md` の `## シート「{sheet}」` 配下で `### {cell_range}` に一致する節を返す（`_find_human_md_section`）。

xlsx のみ対象。対応が取れなければ `resolve_human_excerpt` は None を返し、呼び出し側は rag 文へフォールバックする。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import es_index, worlds

# rag_chunks.jsonl の読み取り安全弁。`es_index._RAG_CHUNKS_FILE_CAP_BYTES` と同じ値に揃える。
_RAG_CHUNKS_SCAN_CAP_BYTES = es_index._RAG_CHUNKS_FILE_CAP_BYTES
# rag.md／人間向け MD の読み取り安全弁。`human_md._MAX_HUMAN_MD_BYTES` と揃える。
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
    """`cand` が `root` 配下に閉じ込められているかを検証した実パス（symlink 脱出・traversal は None）。"""
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
    """`{rel}.rag_chunks.jsonl` から `chunk_id` の `region_context`（sheet/cell_range）を引く。不在・不整合は None。"""
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


def _chunk_id_from_rag_md_span(world: str, doc_id: str, span) -> str | None:
    """grep ヒットの span（rag.md の行範囲・1-based・両端含む）を、その節の chunk_id へ逆引きする。
    アンカーはレコードの見出しより前に出るため、節の開始行（`span[0]`）までの最後のアンカーを使う。
    """
    if not (isinstance(span, (list, tuple)) and len(span) == 2):
        return None
    s, _e = span
    if not isinstance(s, int) or isinstance(s, bool) or s < 1:
        return None
    rag_md_path = worlds.rag_md_path(world, doc_id)
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
    """人間向け MD の `## シート「{sheet}」` 配下で `### {cell_range}` に完全一致する節（`{"heading", "text"}`）。無ければ None。"""
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


def resolve_human_excerpt(world: str, doc_id: str, *, chunk_id: str | None = None, span=None) -> dict | None:
    """成功時 `{"text": 節本文（見出し込み）, "hint": 位置ヒント|None}`。対応が取れなければ None。"""
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
    """利用者向け引用本文を解決する。返り値は `{"quote", "excerpt_source": "human_md"|"rag", "locator_hint"}`。
    人間 MD の該当節が引ければ quote を差し替え、引けなければ `fallback_quote` のまま "rag"。
    """
    section = resolve_human_excerpt(world, doc_id, chunk_id=chunk_id, span=span)
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
