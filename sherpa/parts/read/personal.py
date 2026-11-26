"""個人ファイル（本人の `workspace/files/`）を探す道具 `personal_search` と読む道具 `personal_read`。
親が作った「開いてよいファイルの一覧」と `files/` の実パス（`PersonalContext`）を受け取り、一覧にあるファイルだけを開く。
書き込みはしない。ES・Neo4j・埋め込みには触れない。資料フォルダの除外規則（`path_rules`）はかけない。
設計: docs/design/interfaces.md「MCP の道具」、docs/06-認証と個人領域.md §3
"""
from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from ... import doc_readers, grep_tool, redact_keys
from ...chat_personal import _PERSONAL_SEARCHABLE_EXT
from ...ingest import text_kind
from .hit_text import _clip_utf8_bytes
from .tools import (_READ_INVALID_ARGS_ERROR_CODE, _close_quiet_local, _finish_reader_result, _open_verified_original,
                    _redact, _redact_deep, _redact_plain, _stream_doc_lines)

TOOL_NAMES = frozenset({"personal_search", "personal_read"})
ID_PREFIX = "personal:"
OFFICE_EXT = frozenset({".xlsx", ".docx", ".pptx", ".pdf"})
TEXT_EXT = frozenset(_PERSONAL_SEARCHABLE_EXT) - OFFICE_EXT
READABLE_EXT = TEXT_EXT | OFFICE_EXT

INDEX_MAX_FILES = 2000
_INDEX_MAX_BYTES = 4 * 1024 * 1024
_TERMS_MAX = 10
_TERM_MAX_CHARS = 200
_HITS_MAX = 30
_HIT_TEXT_MAX_BYTES = 600
_PARTIAL_FILES_MAX = 20
_READ_PAGE_LINES = 200
_XLSX_SHEETS_MAX = 20
_XLSX_SCAN_ROWS = 2000
_XLSX_SCAN_COLS = 100
_DOCX_SCAN_PARAGRAPHS = 2000
_DOCX_CALL_PARAGRAPHS = 200
_PAGES_SCAN_MAX = 200
_PPTX_CALL_PAGES = 20
_PDF_CALL_PAGES = 10

_NOT_ALLOWED = {"error": "id が無効、または読めない個人ファイルです", "error_code": "personal_not_allowed"}
_UNAVAILABLE = {"error": "個人ファイルの道具はこのターンでは使えません", "error_code": "personal_unavailable"}
_IO_FAILED = {"error": "読み取りに失敗しました", "error_code": "read_io_failed"}
_DEADLINE_NOTICE = "時間内に全部を探し終えられませんでした。見つかった分だけを返しています。語を絞るか id で 1 ファイルに絞ってください"


@dataclass(frozen=True)
class PersonalContext:
    """このターンで開いてよい個人ファイル。`files_dir` は `files/` の実パス、`allowed` は開いてよい rel_path。"""
    files_dir: Path
    allowed: frozenset


def load_context(index_path: str | None, files_dir: str | None) -> PersonalContext | None:
    """親が書いた一覧（JSON・`{"files": [rel_path, ...]}`）と `files/` の実パスから `PersonalContext` を作る。読めない・形が違う・symlink は None。"""
    if not index_path or not files_dir or not os.path.isabs(index_path) or not os.path.isabs(files_dir):
        return None
    if os.path.realpath(files_dir) != files_dir or not os.path.isdir(files_dir):
        return None
    try:
        fd = os.open(index_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _INDEX_MAX_BYTES:
            return None
        with os.fdopen(fd, "rb") as f:
            fd = -1
            data = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not all(isinstance(x, str) for x in files):
        return None
    return PersonalContext(Path(files_dir), frozenset(x for x in files[:INDEX_MAX_FILES] if valid_rel(x)))


def valid_rel(rel: str) -> bool:
    """`files/` からの相対パスとして開いてよい形か（`..`・絶対パス・NUL・バックスラッシュ・空の要素・秘匿名・読めない形式を拒む）。"""
    if not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        return False
    if any(p in ("", ".", "..") for p in rel.split("/")):
        return False
    if text_kind.is_sensitive_doc_id(rel):
        return False
    return Path(rel).suffix.lower() in READABLE_EXT


def rel_of(doc_id) -> str | None:
    """道具の結果・引数の id（`personal:<rel_path>`）から rel_path を取り出す。形が違えば None。"""
    if not isinstance(doc_id, str) or not doc_id.startswith(ID_PREFIX):
        return None
    return doc_id[len(ID_PREFIX):] or None


def _open(ctx: PersonalContext, rel: str) -> tuple:
    """一覧にある `rel` を、symlink を拒み `files/` の中に閉じたまま開く（検査した実体と開いた実体の dev・ino を突き合わせる）。戻り値 `(f, error)`。"""
    if rel not in ctx.allowed or not valid_rel(rel):
        return None, dict(_NOT_ALLOWED)
    root = ctx.files_dir
    try:
        rp = (root / rel).resolve()
        if rp != root / rel or not rp.is_file():
            return None, dict(_NOT_ALLOWED)
        st = rp.stat()
    except OSError:
        return None, dict(_IO_FAILED)
    return _open_verified_original(root, rel, st)


def _matches(text: str, lowered: list[str]) -> bool:
    low = (text or "").lower()
    return any(t in low for t in lowered)


def _snippet(lines: list[str], lowered: list[str]) -> str:
    """一致した最初の行とその前後（前 1 行・後 2 行）。"""
    for i, ln in enumerate(lines):
        if _matches(ln, lowered):
            return "\n".join(lines[max(0, i - 1):i + 3])
    return "\n".join(lines[:3])


class _SearchDeadline(Exception):
    """検索全体の期限を過ぎた（そのファイルの残りを読まずに打ち切る）。"""


def _past(deadline) -> bool:
    return deadline is not None and time.monotonic() > deadline


def _call_reader(ctx, rel, deadline, fn, *a, **kw) -> dict:
    """ファイルを開き直して原本読取の関数を 1 回呼ぶ（期限・伏せ字を通す）。期限を過ぎていれば呼ばず、期限後のエラーとともに `_SearchDeadline`。"""
    if _past(deadline):
        raise _SearchDeadline
    f, err = _open(ctx, rel)
    if err is not None:
        return err
    with doc_readers.call_deadline(deadline):
        result = fn(f, *a, **kw)
    if not isinstance(result, dict):
        return {"error": "読み取りに失敗しました"}
    if result.get("error") and _past(deadline):
        raise _SearchDeadline
    return _redact_deep(result)


def _scan_text(ctx, rel, lowered, add) -> bool:
    """テキストのファイルを行で探す。戻り値は読み取りの上限で一部しか見ていないか。"""
    f, err = _open(ctx, rel)
    if err is not None:
        raise OSError(err["error"])
    try:
        reader, it, _caution = _stream_doc_lines(f)
        lines = redact_keys.redact_window_lines(list(it), _redact_plain)
    finally:
        _close_quiet_local(f)
    for i, ln in enumerate(lines):
        if _matches(ln, lowered):
            text = "\n".join(f"{k + 1}: {lines[k]}" for k in range(max(0, i - 1), min(len(lines), i + 3)))
            if not add({"line": i + 1}, text):
                break
    return bool(reader.truncated or reader.line_overflowed)


def _scan_xlsx(ctx, rel, lowered, add, deadline) -> bool:
    from openpyxl.utils.cell import get_column_letter, range_boundaries
    listing = _call_reader(ctx, rel, deadline, doc_readers.xlsx_sheets)
    if listing.get("error"):
        raise OSError(listing["error"])
    sheets = listing.get("sheets") or []
    partial = len(sheets) > _XLSX_SHEETS_MAX or bool(listing.get("truncated"))
    for sh in sheets[:_XLSX_SHEETS_MAX]:
        r = _call_reader(ctx, rel, deadline, doc_readers.xlsx_range, sh.get("name") or "",
                         max_rows=_XLSX_SCAN_ROWS, max_cols=_XLSX_SCAN_COLS, clean=_redact)
        if r.get("error"):
            partial = True
            continue
        partial = partial or bool(r.get("truncated") or r.get("size_clipped"))
        try:
            col0, row0, _c1, _r1 = range_boundaries(r.get("range") or "A1")
        except (TypeError, ValueError):
            col0, row0 = 1, 1
        for ri, row in enumerate(r.get("rows") or []):
            ci = next((i for i, c in enumerate(row) if c and _matches(str(c), lowered)), None)
            if ci is None:
                continue
            cell = f"{get_column_letter(col0 + ci)}{row0 + ri}"
            if not add({"sheet": r.get("sheet"), "cell": cell}, " | ".join(str(c) for c in row if c)):
                return partial
    return partial


def _scan_docx(ctx, rel, lowered, add, deadline) -> bool:
    start, partial, first = 0, False, True
    while start < _DOCX_SCAN_PARAGRAPHS:
        r = _call_reader(ctx, rel, deadline, doc_readers.docx_paragraphs, start=start,
                         count=_DOCX_CALL_PARAGRAPHS, clean=_redact)
        if r.get("error"):
            if first:
                raise OSError(r["error"])
            return True
        paras = r.get("paragraphs") or []
        texts = [p.get("text") or "" for p in paras]
        for k, p in enumerate(paras):
            if _matches(texts[k], lowered):
                if not add({"paragraph": p.get("i")}, "\n".join(texts[max(0, k - 1):k + 2])):
                    return partial
        if first:
            first = False
            tables = r.get("tables") or []
            partial = (r.get("total_tables") or 0) > len(tables)
            for t in tables:
                partial = partial or (t.get("total_rows") or 0) > len(t.get("rows") or [])
                for ri, row in enumerate(t.get("rows") or []):
                    if any(c and _matches(str(c), lowered) for c in row):
                        if not add({"table": t.get("i"), "row": (t.get("row_start") or 0) + ri},
                                   " | ".join(str(c) for c in row if c)):
                            return partial
        start += _DOCX_CALL_PARAGRAPHS
        if start >= (r.get("total") or 0):
            return partial
    return True


def _scan_pages(ctx, rel, lowered, add, deadline, kind: str) -> bool:
    """pptx（スライド）・pdf（ページ）を区切りごとに読んで探す。"""
    fn, step, key, loc_key = ((doc_readers.pptx_slides, _PPTX_CALL_PAGES, "slides", "slide") if kind == "pptx"
                              else (doc_readers.pdf_pages, _PDF_CALL_PAGES, "pages", "page"))
    first, partial, total = 1, False, None
    while first <= min(total or first, _PAGES_SCAN_MAX):
        r = _call_reader(ctx, rel, deadline, fn, pages=f"{first}-{first + step - 1}", clean=_redact)
        if r.get("error"):
            if total is None:
                raise OSError(r["error"])
            return True
        total = r.get("total") or 0
        for item in r.get(key) or []:
            if kind == "pptx":
                lines = list(item.get("texts") or []) + [" | ".join(str(c) for c in row if c)
                                                       for t in item.get("tables") or [] for row in t]
                if item.get("notes"):
                    lines.append(str(item["notes"]))
            else:
                partial = partial or bool(item.get("text_truncated"))
                lines = (item.get("text") or "").splitlines()
            if any(_matches(ln, lowered) for ln in lines):
                if not add({loc_key: item.get("no")}, _snippet(lines, lowered)):
                    return partial
        first += step
    return partial or (total or 0) > _PAGES_SCAN_MAX


def _search(args: dict, ctx: PersonalContext, deadline, max_bytes: int) -> dict:
    raw = args.get("terms")
    raw = [raw] if isinstance(raw, str) else raw
    terms = [t.strip()[:_TERM_MAX_CHARS] for t in (raw if isinstance(raw, list) else [])
             if isinstance(t, str) and t.strip()][:_TERMS_MAX]
    if not terms:
        return {"error": "terms（探す語）を 1 つ以上渡してください", "error_code": _READ_INVALID_ARGS_ERROR_CODE}
    if args.get("id"):
        one = rel_of(args.get("id"))
        if one is None or one not in ctx.allowed:
            return dict(_NOT_ALLOWED)
        return search_files(ctx, terms, deadline=deadline, max_bytes=max_bytes, targets=[one])
    return search_files(ctx, terms, deadline=deadline, max_bytes=max_bytes)


def search_files(ctx: PersonalContext, terms: list[str], *, deadline, max_bytes: int, hits_max: int = _HITS_MAX,
                 targets: list[str] | None = None, skipped: list[str] | None = None) -> dict:
    """一覧のファイル（`targets` を渡せばその中だけ）から、語のどれかを含む行・セルなどを探す。テキストは行を、Office・PDF は中の文字を見る。
    `skipped` を渡すと、開けなかった・期限で見られなかったファイルの rel_path をそこへ足す。"""
    lowered = [t.lower() for t in terms]
    targets = sorted(ctx.allowed) if targets is None else targets
    hits: list[dict] = []
    out = {"hits": hits, "files_searched": 0, "truncated": False}
    partial_files: list[str] = []
    unreadable = 0
    for n, rel in enumerate(targets):
        if deadline is not None and time.monotonic() > deadline:
            out.update(deadline_exceeded=True, truncated=True, notice=_DEADLINE_NOTICE)
            if skipped is not None:
                skipped.extend(targets[n:])
            break
        doc_id = ID_PREFIX + rel

        def add(loc: dict, text: str, _id=doc_id) -> bool:
            hits.append({"id": _id, "loc": loc, "text": _clip_utf8_bytes(text, _HIT_TEXT_MAX_BYTES)})
            if len(hits) >= hits_max:
                out["truncated"] = True
                return False
            return True

        ext = Path(rel).suffix.lower()
        try:
            with doc_readers.precheck_once():
                if ext == ".xlsx":
                    partial = _scan_xlsx(ctx, rel, lowered, add, deadline)
                elif ext == ".docx":
                    partial = _scan_docx(ctx, rel, lowered, add, deadline)
                elif ext in (".pptx", ".pdf"):
                    partial = _scan_pages(ctx, rel, lowered, add, deadline, ext[1:])
                else:
                    partial = _scan_text(ctx, rel, lowered, add)
        except _SearchDeadline:
            out["files_searched"] += 1
            partial_files.append(doc_id)
            out.update(deadline_exceeded=True, truncated=True, notice=_DEADLINE_NOTICE)
            if skipped is not None:
                skipped.extend(targets[n:])
            break
        except (OSError, grep_tool.GrepDeadlineExceeded):
            unreadable += 1
            if skipped is not None:
                skipped.append(rel)
            continue
        out["files_searched"] += 1
        if partial:
            partial_files.append(doc_id)
        if out["truncated"]:
            break
    out["count"] = len(hits)
    if unreadable:
        out["unreadable_files"] = unreadable
    if partial_files:
        out["partial_files"] = partial_files[:_PARTIAL_FILES_MAX]
    while hits and len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > max_bytes:
        hits.pop()
        out.update(truncated=True, count=len(hits))
    return out


def _read_text(f, doc_id: str, args: dict, deadline, max_bytes: int) -> dict:
    """テキストを開始行から読む（`read_doc` と同じ形・1 回 200 行まで・バイト予算を超える直前の行で止める）。"""
    try:
        start = max(1, int(args.get("start_line") or 1))
    except (TypeError, ValueError):
        _close_quiet_local(f)
        return {"error": "start_line は整数で", "error_code": _READ_INVALID_ARGS_ERROR_CODE}
    window: list[str] = []
    total = 0
    tracker = redact_keys.KeyBlockRedactor(_redact_plain)
    try:
        reader, it, caution = _stream_doc_lines(f)
        for idx, t in enumerate(it):
            if deadline is not None and idx % 256 == 0 and time.monotonic() > deadline:
                raise grep_tool.GrepDeadlineExceeded("読み取りがデッドラインを超えました")
            if start - 1 <= idx < start - 1 + _READ_PAGE_LINES:
                window.append(t)
            elif idx < start - 1:
                tracker.track(t)
            total += 1
    finally:
        _close_quiet_local(f)
    if total and start > total:
        return {"error": f"range 外です（start_line={start}・全{total}行）", "error_code": _READ_INVALID_ARGS_ERROR_CODE}
    window = redact_keys.redact_window_lines(window, _redact_plain, tracker.in_key_block)
    out_lines: list[str] = []
    used, end, clipped = 0, start - 1, False
    for k, w in enumerate(window):
        ln = f"{start + k}: {w}"
        size = len(ln.encode("utf-8")) + (1 if out_lines else 0)
        if used + size > max_bytes:
            if not out_lines:
                out_lines.append(_clip_utf8_bytes(ln, max_bytes))
                end = start + k
            clipped = True
            break
        out_lines.append(ln)
        used += size
        end = start + k
    result = {"id": doc_id, "start_line": start, "end_line": end, "total_lines": total, "text": "\n".join(out_lines)}
    if clipped:
        result["text_truncated"] = True
    if reader.truncated or reader.line_overflowed:
        result["file_truncated"] = True
    if caution:
        result["encoding_caution"] = caution
    return result


def _office_call(ext: str, args: dict, max_bytes: int) -> tuple[str, object, dict] | dict:
    """Office・PDF の読み方を引数から決める。戻り値は `(原本読取の道具名, 関数, 引数)`、引数の誤りは error の dict。"""
    def ints(*names):
        return {n: args[n] for n in names if isinstance(args.get(n), int) and not isinstance(args.get(n), bool)}
    if ext == ".xlsx":
        if not args.get("sheet"):
            return "xlsx_sheets", doc_readers.xlsx_sheets, {}
        kw = {"clean": _redact, "budget_chars": max_bytes, **ints("max_rows", "max_cols")}
        if args.get("range"):
            kw["range_a1"] = str(args["range"])
        return "xlsx_range", doc_readers.xlsx_range, {"sheet": str(args["sheet"]), **kw}
    if ext == ".docx":
        return "docx_paragraphs", doc_readers.docx_paragraphs, {
            "clean": _redact, **ints("start", "count", "table_start", "table_row_start")}
    kw = {"clean": _redact, **({"pages": str(args["pages"])} if args.get("pages") else {})}
    if ext == ".pptx":
        return "pptx_slides", doc_readers.pptx_slides, kw
    return "pdf_pages", doc_readers.pdf_pages, {**kw, **ints("text_offset")}


def _read(args: dict, ctx: PersonalContext, deadline, max_bytes: int) -> dict:
    doc_id = str(args.get("id") or "")
    rel = rel_of(doc_id)
    if rel is None:
        return dict(_NOT_ALLOWED)
    ext = Path(rel).suffix.lower()
    f, err = _open(ctx, rel)
    if err is not None:
        return err
    if ext not in OFFICE_EXT:
        return _read_text(f, doc_id, args, deadline, max_bytes)
    reader_name, fn, kw = _office_call(ext, args, max_bytes)
    with doc_readers.call_deadline(deadline):
        result = fn(f, **kw)
    if (deadline is not None and time.monotonic() > deadline
            and isinstance(result, dict) and result.get("error") == doc_readers._TIME_ERROR["error"]):
        raise grep_tool.GrepDeadlineExceeded("読み取りがデッドラインを超えました")
    result = _redact_deep(result)
    if not isinstance(result, dict) or result.get("error"):
        return result if isinstance(result, dict) else dict(_IO_FAILED)
    result = _finish_reader_result(reader_name, result, doc_id, max_bytes)
    result = {("id" if k == "doc_id" else k): v for k, v in result.items()}
    result.setdefault("id", doc_id)
    return result


def run_tool(name: str, args: dict, ctx: PersonalContext | None, *, deadline: float | None = None,
             tool_result_max_bytes: int | None = None) -> tuple[dict, set, list, list]:
    """個人ファイルの道具を実行し `(結果, 空集合, [], [])` を返す（資料フォルダの出典・引用候補には何も足さない）。
    `ctx` が None（このターンで個人ファイルを使わない）なら拒む。利用者・置き場は `ctx` だけから決め、引数からは受け取らない。"""
    from .tools import TOOL_RESULT_MAX_BYTES
    if ctx is None or name not in TOOL_NAMES:
        return dict(_UNAVAILABLE), set(), [], []
    max_bytes = tool_result_max_bytes or TOOL_RESULT_MAX_BYTES
    args = args if isinstance(args, dict) else {}
    result = _search(args, ctx, deadline, max_bytes) if name == "personal_search" else _read(args, ctx, deadline, max_bytes)
    return result, set(), [], []


def read_rels(name: str, args, result) -> list[str]:
    """道具の結果から、読んだ（中身を返した）個人ファイルの rel_path を出現順・重複なしで返す。失敗・0 件は空。"""
    if name not in TOOL_NAMES or not isinstance(result, dict) or result.get("error"):
        return []
    if name == "personal_read":
        rel = rel_of((args or {}).get("id") if isinstance(args, dict) else None)
        return [rel] if rel else []
    out: list[str] = []
    for h in result.get("hits") or []:
        rel = rel_of(h.get("id")) if isinstance(h, dict) else None
        if rel and rel not in out:
            out.append(rel)
    return out
