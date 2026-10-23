"""取り込み前のディスク容量の見積もり（警告だけ・取り込みは止めない）。

登録・更新のボタンを押したあと、始める前に「取り込みに必要な空き」と「今の空き」を見せる。
原本は読み取り専用で歩く（stat と、docx／xlsx／pptx の ZIP の中の XML の要素数・字数を数えるだけ。本文は外へ出さない）。
書き込み先ごとに必要量を出し、同じファイルシステムの分は合算して空きと比べる。
設計: docs/design/operations.md「取り込み前のディスク容量の見積もり」
"""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import es_index, log_setup, scope_infer, store, worlds
from .ingest import office_md, text_kind
from .ingest.analyzers import registry as analyzer_registry
from .ingest.archive_extract import archive_kind
from .ingest.ooxml import word as ooxml_word

_log = logging.getLogger("sherpa")

MB = 1_000_000
GIB = 1024 ** 3

# ---- 係数（この Mac での実測から安全側に丸めた固定値。根拠は設計書の表） ----

# 判定: 空き < 必要量 は「足りない」、空き < 必要量×1.2 は「余裕が少ない」。
TIGHT_FACTOR = 1.2
# ES は使用率が 90% を超えると新しい索引を作れなくなる（ES 側の既定）ため、取り込み後の使用率がこれ以上なら「余裕が少ない」に上げる。
ES_WATERMARK = 0.90

# 派生物（md／rag／ir）の 1 ファイルあたり固定分（サイドカー約 9 本×4KB）と、検索キャッシュ等の全ファイル共通分。
_DERIVED_FILE_BYTES = 40 * 1024
_DERIVED_ALL_FILE_BYTES = 512
# 変換結果のキャッシュは派生物とハードリンクで同じ実体を共有する。リンク分のメタ情報として派生物の 2% を足す。
_CONV_CACHE_OVERHEAD = 0.02

# 埋め込み（1 次元 1 バイトで持つ）: ES は取り込みが終わって詰めた後で 1 次元あたり 3.5B（ベクトル本体と HNSW。_source には入らない）、
# 取り込み中は一時的な写し（recovery source）で膨らむので終わった後の 1.5 倍で見る。
# 埋め込みキャッシュ（SQLite）は 1 次元 1B ＋ 1 行ぶんの固定分。
_ES_VECTOR_BYTES_PER_DIM = 3.5
_ES_PEAK_FACTOR = 1.5
_EMBED_CACHE_BYTES_PER_DIM = 1
_EMBED_CACHE_ROW_BYTES = 700
# 埋め込みの次元を設定から読めないときの値（OpenAI の既定次元＝最大側）。
_DEFAULT_EMBED_DIM = 1536

# Neo4j: ストアの下限（トランザクションログのセグメント等）とファイルあたりの目安。
_NEO4J_FIXED_INITIAL = GIB
_NEO4J_FIXED_UPDATE = 256 * 1024 * 1024
_NEO4J_SOURCE_FILE_BYTES = 20 * 1024
_NEO4J_OTHER_FILE_BYTES = 4 * 1024

# OCR の結果（2 世代ぶん）。画像 1 枚あたり、PDF 1MB あたり。
_OCR_IMAGE_BYTES = 24 * 1024
_OCR_PDF_BYTES_PER_MB = 120 * 1024

# ログ: RUN_DIR のログ全体の上限（log_setup と同じ値）の残りと、取り込み 1 回で増える目安。
_LOG_BASE_BYTES = 50 * 1024 * 1024
_LOG_PER_FILE_BYTES = 1024

# LibreOffice の一時ファイル（プロファイル・出力）。旧形式があるときだけ。
_TMP_BASE_BYTES = 64 * 1024 * 1024
_TMP_LEGACY_BYTES = 128 * 1024 * 1024

# アーカイブは中身が分からないため、展開後の大きさを 3 倍、その派生物を展開後の 2 倍と置く。
_ARCHIVE_EXPAND = 3.0
_ARCHIVE_DERIVED_RATIO = 2.0
_ARCHIVE_CHUNKS_PER_MB = 650.0
_ARCHIVE_VEC_RATIO = 0.5

# OOXML の中身を数える上限。XML 1 本・1 ファイルで読む量（展開後のバイト）を超えたら、読めた範囲の密度で全体へ外挿する。
# 数える時間の合計を使い切ったら、残りのファイルは族の係数の式に戻す。
_MEASURE_PART_BYTES = 8 * 1024 * 1024
_MEASURE_FILE_BYTES = 32 * 1024 * 1024
_MEASURE_READ_BYTES = 1024 * 1024
_MEASURE_BUDGET_SEC = 120

# 1 チャンクの本文の上限（字）。表の行（セルの並び）が _CHUNK_CHARS、段落・図形の文字列が _SPAN_CHARS を超えると複数のチャンクに分かれる。
_CHUNK_CHARS = 1800
_SPAN_CHARS = 1200
# 表の 1 セルが rag.md に出るときの飾り（項目名・括弧）の目安（字）。
_XLSX_CELL_DECOR_CHARS = 12

# 数えた要素から派生物（md／rag／ir・assets 込み）のバイト数を出す係数。実際に変換した結果への当てはめを約 1 割安全側に丸めた固定値。
# 字数あたりの値は日本語（UTF-8 で 1 字 3 バイト）の文書で当てはめたもので、英数字だけの文書では多めに見える。
_XLSX_DERIVED_PER_CELL = 110
_XLSX_DERIVED_PER_ROW = 340
_XLSX_DERIVED_PER_CHAR = 8
_DOCX_DERIVED_PER_PARA = 380
_DOCX_DERIVED_PER_CELL = 60
_DOCX_DERIVED_PER_ROW = 850
_DOCX_DERIVED_PER_CHAR = 8
_PPTX_DERIVED_PER_CHUNK = 620
_PPTX_DERIVED_PER_ROW = 1000
_PPTX_DERIVED_PER_CHAR = 8

# 原本を歩く時間の上限（秒）。超えたら見積もりは失敗として扱い、取り込みは止めない。
_WALK_DEADLINE_SEC = 600
_STUCK_GRACE_SEC = 60
# 数え終えた結果を覚えておく時間（秒）。
_RESULT_TTL_SEC = 300
_FAILED_RETRY_SEC = 10


@dataclass(frozen=True)
class Coef:
    derived_ratio: float       # 原本 1 バイトあたりの派生物（md／rag／ir・assets 込み）
    derived_file: int          # 原本 1 ファイルあたりの派生物の固定分
    chunks_per_mb: float       # 原本 1MB あたりのチャンク数
    chunks_file: float         # 原本 1 ファイルあたりのチャンク数
    es_chunk_bytes: int        # チャンク 1 件の ES の本文（ベクトルを除く。ベクトルの無いチャンクも入る）
    embedded: bool             # ベクトルを持つか
    cache_ratio: float = 0.0   # 変換前処理のキャッシュ（旧形式の変換結果）
    vec_ratio: float = 1.0     # チャンクのうちベクトルを持つ割合（表の行・同じ節の続きは束ねて 1 本）


# 派生物の比は document.json を書かない分（実測で派生物の約 25%）を引いて安全側に丸めた値。
# ベクトルの割合は束ねによる縮み（本文 2.2〜3 倍・表 4.6〜20 行/束）の小さい側。
COEFS: dict[str, Coef] = {
    "pdf": Coef(2.4, _DERIVED_FILE_BYTES, 1900, 4, 1000, True, vec_ratio=0.5),
    "docx": Coef(1.2, _DERIVED_FILE_BYTES, 650, 4, 1000, True, vec_ratio=0.5),
    "xlsx": Coef(2.8, _DERIVED_FILE_BYTES, 620, 4, 1000, True, vec_ratio=0.25),
    "pptx": Coef(1.0, _DERIVED_FILE_BYTES, 720, 4, 1000, True, vec_ratio=0.5),
    "legacy": Coef(0.8, _DERIVED_FILE_BYTES, 1900, 4, 1000, True, cache_ratio=0.5, vec_ratio=0.5),
    "image": Coef(0.9, _DERIVED_FILE_BYTES, 0, 1, 1200, True),
    "text": Coef(0.0, 0, 200, 1, 6000, False),
    "source": Coef(0.0, 0, 0, 0, 0, False),
    "other": Coef(0.0, 0, 0, 0, 0, False),
}
FAMILIES = tuple(COEFS) + ("archive",)

_OOXML = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx"}
_TEXT_EXT = frozenset({".txt", ".md", ".rst"}) | text_kind.DOCUMENT_EXT

# 画面に出す置き場の名前（専門用語を避ける）。
PLACE_LABELS = {
    "derived": "変換した資料",
    "embed": "検索用の数値データ",
    "es": "全文検索の索引",
    "neo4j": "つながりのデータ",
    "ocr": "文字読み取りの結果",
    "logs": "記録（ログ）",
    "tmp": "作業用の一時領域",
}
PLACES = tuple(PLACE_LABELS)

STATUS_ORDER = {"ok": 0, "unknown": 1, "tight": 2, "short": 3}


# ---- OOXML の中身を数える ----
# 設計: docs/design/operations.md「取り込み前のディスク容量の見積もり」

_MEASURED_FAMILIES = ("docx", "xlsx", "pptx")


class _MeasureTimeout(Exception):
    """数える時間の上限を超えた。"""


_XLSX_T_RE = re.compile(rb"<t(?: [^>]*)?>([^<]*)</t>")
_XLSX_PHONETIC_RE = re.compile(rb"<rPh[ >].*?</rPh>", re.DOTALL)
_DOCX_T_RE = re.compile(rb"<w:t(?: [^>]*)?>([^<]*)</w:t>")
_DOCX_ROW_RE = re.compile(rb"<w:tr[ >]")
_PPTX_T_RE = re.compile(rb"<a:t(?: [^>]*)?>([^<]*)</a:t>")
_PPTX_ROW_RE = re.compile(rb"<a:tr[ >]")
_PPTX_FRAME_RE = re.compile(rb"<p:graphicFrame[ >].*?</p:graphicFrame>", re.DOTALL)
_XML_ENTITY_RE = re.compile(rb"&#?\w+;")


def _text_chars(texts: list[bytes]) -> int:
    """XML の文字列の字数（文字参照は 1 字。前後の空白は数えない）。"""
    return len(_XML_ENTITY_RE.sub(b"x", b"".join(texts)).decode("utf-8", "ignore").strip())


def _stream_parts(zf: zipfile.ZipFile, info: zipfile.ZipInfo, sep: bytes, step, *, cap: int,
                  deadline: float) -> float:
    """XML 1 本を先頭から `cap` バイトまで読み、`sep`（要素の終わり）で区切るたびに `step(区切り)` を呼ぶ。

    戻り値は外挿の倍率（全体の大きさ ÷ 数えた範囲の大きさ。全体を読めたら 1.0）。本文はメモリに溜めず、区切り 1 つぶんだけ持つ。
    """
    read = consumed = 0
    carry = b""
    complete = False
    with zf.open(info) as fh:
        while read < cap:
            if time.monotonic() > deadline:
                raise _MeasureTimeout()
            chunk = fh.read(min(_MEASURE_READ_BYTES, cap - read))
            if not chunk:
                complete = True
                break
            read += len(chunk)
            parts = (carry + chunk).split(sep)
            carry = parts.pop()
            for seg in parts:
                step(seg)
                consumed += len(seg) + len(sep)
    if complete or read >= info.file_size:
        if carry:
            step(carry)
        return 1.0
    if consumed <= 0:
        raise ValueError("区切りが見つからない")
    return info.file_size / consumed


def _scan_parts(zf: zipfile.ZipFile, infos: list, sep: bytes, new_acc, step, deadline: float) -> dict:
    """`infos` の XML を順に数える。XML ごとの倍率と、1 ファイルで読む合計の上限で読めなかった XML の分を掛けて合算する。

    `_` で始まる項目は XML をまたがない作業用の値（倍率を掛けず合算もしない）。`_spent` は読んだバイト数（1 ファイルの上限を他の XML と分け合う）。
    """
    budget = _MEASURE_FILE_BYTES
    total_size = scanned_size = 0
    out = new_acc()
    for info in infos:
        total_size += info.file_size
        if budget <= 0:
            continue
        cap = min(_MEASURE_PART_BYTES, budget)
        acc = new_acc()
        scale = _stream_parts(zf, info, sep, lambda seg, acc=acc: step(acc, seg), cap=cap, deadline=deadline)
        budget -= min(info.file_size, cap)
        scanned_size += info.file_size
        for k, v in acc.items():
            if not k.startswith("_"):
                out[k] += v * scale
    out["_spent"] = _MEASURE_FILE_BYTES - budget
    if scanned_size and total_size > scanned_size:
        rest = total_size / scanned_size
        for k in out:
            if not k.startswith("_"):
                out[k] *= rest
    return out


def _scan_elements(zf: zipfile.ZipFile, groups: list, step, budget: int, deadline: float) -> tuple[dict, int, int]:
    """本体以外の XML（`groups` は `(XML の一覧, 1 要素の終わり)` の並び）を、要素ごとに `step(acc, 区切り)` で数える。

    戻り値は `(合算, 数えられなかった XML の ZIP 上の大きさ, 読んだバイト数)`。1 ファイルで読む合計の上限（`budget`）を使い切った後の XML と、
    区切りが見つからず読めなかった XML は数えず、その大きさを返す（呼び出し側が族の係数の式で見積もる）。
    """
    out = {"chunks": 0.0, "paras": 0.0, "chars": 0.0}
    uncounted = spent = 0
    for infos, sep in groups:
        for info in infos:
            if budget <= 0:
                uncounted += info.compress_size
                continue
            cap = min(_MEASURE_PART_BYTES, budget)
            acc = dict.fromkeys(out, 0.0)
            used = min(info.file_size, cap)
            budget -= used
            spent += used
            try:
                scale = _stream_parts(zf, info, sep, lambda seg, acc=acc: step(acc, seg), cap=cap, deadline=deadline)
            except ValueError:
                uncounted += info.compress_size
                continue
            for k, v in acc.items():
                out[k] += v * scale
    return out, uncounted, spent


def _unique_media(zf: zipfile.ZipFile) -> tuple[int, int]:
    """埋め込み画像の（個数, バイト数）。同じ中身（CRC と大きさが同じ）は 1 つと数える（派生物の assets は中身で重複を除く）。"""
    seen: dict = {}
    for info in zf.infolist():
        if "/media/" in info.filename and not info.is_dir():
            seen.setdefault((info.CRC, info.file_size), info.file_size)
    return len(seen), sum(seen.values())


def _xlsx_step(acc: dict, seg: bytes) -> None:
    n = seg.count(b"</c>")
    if not n:
        return
    acc["rows"] += 1
    acc["cells"] += n
    acc["strs"] += seg.count(b' t="s"')
    if b"<is>" in seg:
        acc["inline"] += _text_chars(_XLSX_T_RE.findall(seg))


def _scan_xlsx(zf: zipfile.ZipFile, deadline: float) -> dict:
    sheets = [i for i in zf.infolist() if i.filename.startswith("xl/worksheets/") and i.filename.lower().endswith(".xml")]
    if not sheets:
        raise ValueError("シートが無い")
    acc = _scan_parts(zf, sheets, b"</row>", lambda: {"rows": 0.0, "cells": 0.0, "strs": 0.0, "inline": 0.0},
                      _xlsx_step, deadline)
    # 文字列セルの字数は、共有文字列表の 1 件あたりの平均字数 × 参照するセル数で見る（ふりがなは描画されないので除く）。
    avg = 0.0
    try:
        shared = zf.getinfo("xl/sharedStrings.xml")
    except KeyError:
        shared = None
    if shared is not None and shared.file_size:
        tally = [0, 0]

        def _si(seg: bytes) -> None:
            tally[0] += 1
            tally[1] += _text_chars(_XLSX_T_RE.findall(_XLSX_PHONETIC_RE.sub(b"", seg)))
        _stream_parts(zf, shared, b"</si>", _si, cap=_MEASURE_PART_BYTES, deadline=deadline)
        avg = tally[1] / tally[0] if tally[0] else 0.0
    media_n, media_bytes = _unique_media(zf)
    return {"rows": acc["rows"], "cells": acc["cells"], "text": acc["strs"] * avg + acc["inline"],
            "media_n": media_n, "media_bytes": media_bytes}


def _docx_step(acc: dict, seg: bytes) -> None:
    acc["_depth"] = max(0, acc["_depth"] + seg.count(b"<w:tbl>") - seg.count(b"</w:tbl>"))
    acc["trows"] += len(_DOCX_ROW_RE.findall(seg))
    chars = _text_chars(_DOCX_T_RE.findall(seg))
    if chars:
        acc["chars"] += chars
        if acc["_depth"]:
            acc["tcells"] += 1
        else:
            acc["paras"] += 1
            acc["chunks"] += math.ceil(chars / _SPAN_CHARS)


def _docx_element_step(acc: dict, seg: bytes) -> None:
    """ヘッダー・フッター（パートごと）、脚注・文末脚注・コメント（1 件ごと）は、段落をつないだ 1 要素として数える。"""
    chars = _text_chars(_DOCX_T_RE.findall(seg))
    if chars:
        acc["paras"] += 1
        acc["chars"] += chars
        acc["chunks"] += math.ceil(chars / _SPAN_CHARS)


def _docx_extra_parts(zf: zipfile.ZipFile) -> list:
    """取り込みが本文のほかに読む部品（`ingest/ooxml/word.py` の `header_footer_names`・`footnotes`・`endnotes`・`comments`）。"""
    headers, footers = ooxml_word.header_footer_names(zf)
    names = set(zf.namelist())

    def infos(parts: list) -> list:
        return [zf.getinfo(n) for n in parts if n in names]
    return [(infos(headers), b"</w:hdr>"), (infos(footers), b"</w:ftr>"),
            (infos(["word/footnotes.xml"]), b"</w:footnote>"), (infos(["word/endnotes.xml"]), b"</w:endnote>"),
            (infos(["word/comments.xml"]), b"</w:comment>")]


def _scan_docx(zf: zipfile.ZipFile, deadline: float) -> dict:
    acc = _scan_parts(zf, [zf.getinfo("word/document.xml")], b"</w:p>", lambda: {
        "paras": 0.0, "chunks": 0.0, "tcells": 0.0, "trows": 0.0, "chars": 0.0, "_depth": 0}, _docx_step, deadline)
    extra, uncounted, _ = _scan_elements(zf, _docx_extra_parts(zf), _docx_element_step,
                                         _MEASURE_FILE_BYTES - acc["_spent"], deadline)
    media_n, media_bytes = _unique_media(zf)
    return {"paras": acc["paras"] + extra["paras"], "chunks": acc["chunks"] + extra["chunks"],
            "tcells": acc["tcells"], "trows": acc["trows"], "text": acc["chars"] + extra["chars"],
            "uncounted_bytes": uncounted, "media_n": media_n, "media_bytes": media_bytes}


def _pptx_step(acc: dict, seg: bytes) -> None:
    texts = _PPTX_T_RE.findall(seg)
    if not texts:
        return
    acc["chars"] += _text_chars(texts)
    if b"<a:tr" in seg:
        acc["trows"] += len(_PPTX_ROW_RE.findall(seg))
        texts = _PPTX_T_RE.findall(_PPTX_FRAME_RE.sub(b"", seg))
    chars = _text_chars(texts) if texts else 0
    if chars:
        acc["chunks"] += math.ceil(chars / _SPAN_CHARS)


def _scan_pptx_frames(zf: zipfile.ZipFile, infos: list, deadline: float, budget: int) -> tuple[float, int, int]:
    """スライドの雛形（レイアウト・マスター・ノートマスター）に出る文字列のチャンク数。XML ごとに、同じ段落の文字列は 1 つと数える。

    1 ファイルで読む合計の上限（`budget`）で読めなかった XML は、読めた XML の平均で見る。1 本も読めなかったときは数えられなかった XML の ZIP 上の大きさを返す。
    戻り値は `(チャンク数, 数えられなかった大きさ, 読んだバイト数)`。
    """
    spent = 0
    total = 0.0
    done = 0
    for info in infos:
        if budget <= 0:
            break
        seen: set = set()

        def paragraph(seg: bytes, seen=seen) -> None:
            text = b"".join(_PPTX_T_RE.findall(seg)).strip()
            if text:
                seen.add(text)
        scale = _stream_parts(zf, info, b"</a:p>", paragraph, cap=min(_MEASURE_PART_BYTES, budget), deadline=deadline)
        used = min(info.file_size, _MEASURE_PART_BYTES)
        budget -= used
        spent += used
        total += len(seen) * scale
        done += 1
    if not done:
        return 0.0, sum(i.compress_size for i in infos), spent
    return total * len(infos) / done, 0, spent


def _pptx_note_step(acc: dict, seg: bytes) -> None:
    """発表者ノートは、スライド 1 枚のノート全体を 1 要素として数える。"""
    chars = _text_chars(_PPTX_T_RE.findall(seg))
    if chars:
        acc["chars"] += chars
        acc["chunks"] += math.ceil(chars / _SPAN_CHARS)


def _scan_pptx(zf: zipfile.ZipFile, deadline: float) -> dict:
    names = zf.namelist()
    slides = [zf.getinfo(n) for n in names if n.startswith("ppt/slides/") and n.endswith(".xml") and n.count("/") == 2]
    frames = [zf.getinfo(n) for n in names if n.endswith(".xml") and n.count("/") == 2 and
              n.startswith(("ppt/slideLayouts/", "ppt/slideMasters/", "ppt/notesMasters/"))]
    notes = [zf.getinfo(n) for n in names if n.startswith("ppt/notesSlides/") and n.endswith(".xml") and n.count("/") == 2]
    if not slides:
        raise ValueError("スライドが無い")
    on_slides = _scan_parts(zf, slides, b"</p:sp>", lambda: {"chunks": 0.0, "trows": 0.0, "chars": 0.0},
                            _pptx_step, deadline)
    budget = _MEASURE_FILE_BYTES - on_slides["_spent"]
    frame_chunks, frames_uncounted, spent = _scan_pptx_frames(zf, frames, deadline, budget)
    on_notes, notes_uncounted, _ = _scan_elements(zf, [(notes, b"</p:notes>")], _pptx_note_step, budget - spent, deadline)
    media_n, media_bytes = _unique_media(zf)
    return {"chunks": on_slides["chunks"] + on_notes["chunks"], "frame_chunks": frame_chunks,
            "trows": on_slides["trows"], "text": on_slides["chars"] + on_notes["chars"],
            "uncounted_bytes": frames_uncounted + notes_uncounted, "media_n": media_n, "media_bytes": media_bytes}


def _estimate_xlsx(f: dict) -> tuple[float, float]:
    rows, cells, text = f["rows"], f["cells"], f["text"]
    splits = max(1, math.ceil((cells * _XLSX_CELL_DECOR_CHARS + text) / rows / _CHUNK_CHARS)) if rows else 1
    derived = (cells * _XLSX_DERIVED_PER_CELL + rows * _XLSX_DERIVED_PER_ROW + text * _XLSX_DERIVED_PER_CHAR
               + f["media_bytes"])
    return derived, rows * splits + f["media_n"]


def _uncounted(family: str, f: dict) -> tuple[float, float]:
    """数えられなかった XML の分を、族の係数の式（ZIP 上の大きさあたり）で見積もった `(派生物のバイト数, チャンク数)`。"""
    c = COEFS[family]
    size = f.get("uncounted_bytes", 0)
    return size * c.derived_ratio, size / MB * c.chunks_per_mb


def _estimate_docx(f: dict) -> tuple[float, float]:
    derived = (f["paras"] * _DOCX_DERIVED_PER_PARA + f["tcells"] * _DOCX_DERIVED_PER_CELL
               + f["trows"] * _DOCX_DERIVED_PER_ROW + f["text"] * _DOCX_DERIVED_PER_CHAR + f["media_bytes"])
    rest_derived, rest_chunks = _uncounted("docx", f)
    return derived + rest_derived, f["chunks"] + f["trows"] + f["media_n"] + rest_chunks


def _estimate_pptx(f: dict) -> tuple[float, float]:
    chunks = f["chunks"] + f["frame_chunks"]
    derived = (chunks * _PPTX_DERIVED_PER_CHUNK + f["trows"] * _PPTX_DERIVED_PER_ROW
               + f["text"] * _PPTX_DERIVED_PER_CHAR + f["media_bytes"])
    rest_derived, rest_chunks = _uncounted("pptx", f)
    return derived + rest_derived, chunks + f["trows"] + f["media_n"] + rest_chunks


_SCANNERS = {"xlsx": (_scan_xlsx, _estimate_xlsx), "docx": (_scan_docx, _estimate_docx),
             "pptx": (_scan_pptx, _estimate_pptx)}


def measure_ooxml(path, family: str, *, deadline: float) -> tuple[float, float] | None:
    """docx／xlsx／pptx の ZIP の中の XML を軽く数えて、`(派生物のバイト数, チャンク数)` を見積もる。

    数えるのは要素の数と文字数だけで、本文は外へ出さない。読む量・時間には上限があり（標本から外挿）、
    開けない・壊れている・上限を超えたファイルは None（呼び出し側が族の係数の式に戻す）。例外は送出しない。
    """
    scan, estimate = _SCANNERS[family]
    try:
        with zipfile.ZipFile(path) as zf:
            return estimate(scan(zf, deadline))
    except _MeasureTimeout:
        return None
    except Exception:
        _log.debug("disk_estimate: 中身を数えられませんでした（係数の式で見積もります）", exc_info=True)
        return None


# ---- 原本を数える ----

def _new_counts() -> dict:
    # measured: 中身を数えられたファイルの [件数, 原本の大きさ, 派生物のバイト数, チャンク数]（族ごと）。
    return {"families": {f: [0, 0] for f in FAMILIES}, "legacy_max": 0,
            "measured": {f: [0, 0, 0.0, 0.0] for f in _MEASURED_FAMILIES}}


def _family_sets() -> tuple[frozenset, frozenset]:
    source = frozenset(analyzer_registry.registered_extensions()) | text_kind.CODE_EXT
    return source, _TEXT_EXT


def family_of(rel: str, source_ext: frozenset, text_ext: frozenset) -> str:
    """原本の種類（係数の族）。拡張子だけで決める（中身は読まない）。"""
    if archive_kind(rel):
        return "archive"
    ext = PurePosixPath(rel).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in _OOXML:
        return _OOXML[ext]
    if ext in office_md.LEGACY_OFFICE_EXT:
        return "legacy"
    if ext in office_md.RASTER_EVIDENCE_EXT:
        return "image"
    if ext in source_ext:
        return "source"
    if ext in text_ext:
        return "text"
    return "other"


def _add(counts: dict, family: str, size: int, measured: tuple[float, float] | None = None) -> None:
    c = counts["families"][family]
    c[0] += 1
    c[1] += size
    if family == "legacy" and size > counts["legacy_max"]:
        counts["legacy_max"] = size
    if measured is not None:
        m = counts["measured"][family]
        m[0] += 1
        m[1] += size
        m[2] += measured[0]
        m[3] += measured[1]


def count_sources(root, *, manifest: dict | None = None, deadline: float | None = None,
                  progress=None) -> tuple[dict, dict]:
    """原本を数える。戻り値は `(全体, 追加・変更分)`。

    ① 取り込みと同じ歩き方（`scope_infer.safe_files`）で列挙する ② 種類別に件数と大きさを足す
    ③ docx／xlsx／pptx は ZIP の中の XML を軽く数えて派生物とチャンクの見積もりを添える（`measure_ooxml`・時間の上限つき）
    ④ `manifest`（前回取り込みの明細）があれば、無いもの・値が違うものを「追加・変更分」にも足す。
    原本には書かない。
    """
    source_ext, text_ext = _family_sets()
    total, delta = _new_counts(), _new_counts()
    started = time.monotonic()
    measure_until = started + _MEASURE_BUDGET_SEC
    if deadline is not None:
        measure_until = min(measure_until, started + max(0.0, deadline - started) / 2)
    n = 0
    for rp, rel in scope_infer.safe_files(root, deadline=deadline):
        try:
            st = rp.stat()
        except OSError:
            continue
        fam = family_of(rel, source_ext, text_ext)
        changed = False
        if manifest is not None:
            prev = manifest.get(rel)
            changed = prev is None or list(prev) != [st.st_mtime_ns, st.st_ctime_ns, st.st_size]
        est = None
        # 更新で変わっていないファイルは数えない（足すのは追加・変更ぶんだけ。ES は今の実測と比べる）。
        if fam in _MEASURED_FAMILIES and (manifest is None or changed) and time.monotonic() < measure_until:
            est = measure_ooxml(rp, fam, deadline=measure_until)
        _add(total, fam, st.st_size, est)
        if changed:
            _add(delta, fam, st.st_size, est)
        n += 1
        if progress is not None and n % 500 == 0:
            progress(n)
    if progress is not None:
        progress(n)
    return total, delta


# ---- 必要量の計算 ----

def _embed_dim() -> int:
    """埋め込みの次元。無効なら 0。設定を読めないときは最大側。"""
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return 0
    try:
        from . import embeddings
        cfg = embeddings.cfg()
    except Exception:
        return _DEFAULT_EMBED_DIM
    return int(cfg["dim"]) if cfg else 0


def _family_needs(counts: dict, dim: int) -> dict:
    """種類別の件数・大きさから、置き場ごとの必要量（バイト）を出す。`es` は取り込みが終わって詰めた後の大きさ。"""
    need = {"derived": 0.0, "embed": 0.0, "es": 0.0, "neo4j": 0.0, "ocr": 0.0}
    files = 0
    for fam, (n, size) in counts["families"].items():
        if not n:
            continue
        files += n
        mb = size / MB
        if fam == "archive":
            expanded = size * _ARCHIVE_EXPAND
            need["derived"] += expanded + expanded * _ARCHIVE_DERIVED_RATIO + n * _DERIVED_FILE_BYTES
            chunks = expanded / MB * _ARCHIVE_CHUNKS_PER_MB + 4 * n
            vecs = chunks * _ARCHIVE_VEC_RATIO
            need["es"] += chunks * 1000 + vecs * _ES_VECTOR_BYTES_PER_DIM * dim
            need["embed"] += vecs * (_EMBED_CACHE_BYTES_PER_DIM * dim + _EMBED_CACHE_ROW_BYTES) if dim else 0
            need["neo4j"] += n * _NEO4J_OTHER_FILE_BYTES
            continue
        c = COEFS[fam]
        # 中身を数えられたファイルは数えた値、残りは族の係数の式で見る。
        m_n, m_size, m_derived, m_chunks = counts.get("measured", {}).get(fam, (0, 0, 0.0, 0.0))
        rest_n, rest_size = n - m_n, size - m_size
        need["derived"] += (c.derived_ratio * rest_size + c.derived_file * n + c.cache_ratio * size + m_derived)
        chunks = c.chunks_per_mb * rest_size / MB + c.chunks_file * rest_n + m_chunks
        vecs = chunks * c.vec_ratio if c.embedded else 0
        need["es"] += chunks * c.es_chunk_bytes + vecs * _ES_VECTOR_BYTES_PER_DIM * dim
        if vecs and dim:
            need["embed"] += vecs * (_EMBED_CACHE_BYTES_PER_DIM * dim + _EMBED_CACHE_ROW_BYTES)
        need["neo4j"] += n * (_NEO4J_SOURCE_FILE_BYTES if fam == "source" else _NEO4J_OTHER_FILE_BYTES)
        if fam == "image":
            need["ocr"] += n * _OCR_IMAGE_BYTES
        elif fam == "pdf":
            need["ocr"] += mb * _OCR_PDF_BYTES_PER_MB
    need["derived"] += files * _DERIVED_ALL_FILE_BYTES
    need["derived"] *= 1 + _CONV_CACHE_OVERHEAD
    if not office_md.ocr_enabled():
        need["ocr"] = 0.0
    return need


def _logs_need(files: int, current_log_bytes: int) -> float:
    room = max(0, log_setup._TOTAL_MAX_BYTES - current_log_bytes)
    return float(min(room, _LOG_BASE_BYTES + _LOG_PER_FILE_BYTES * files))


def _tmp_need(counts: dict) -> float:
    if counts["families"]["legacy"][0]:
        return float(_TMP_BASE_BYTES + _TMP_LEGACY_BYTES + 2 * counts["legacy_max"])
    return float(_TMP_BASE_BYTES)


def _file_total(counts: dict) -> int:
    return sum(v[0] for v in counts["families"].values())


def compute_needs(total: dict, delta: dict | None, *, mode: str, dim: int, linked: bool = True,
                  es_actual: int | None = None, log_bytes: int = 0, settled: bool = False) -> dict:
    """置き場ごとの必要量（バイト）。ES は取り込み中の最大（一時的な写しで膨らむ）。`settled=True` なら取り込みが終わって詰めた後。

    登録（初回）: 全原本ぶんを 1 組（作りかけを公開へ改名するだけで 2 組にはならない）。
    更新: 今使っている分は空きに含まれているので足さない。足すのは、追加・変更ぶんの新しい派生物
    （変換のキャッシュが派生物とリンクしていなければ全ぶん）・追加・変更ぶんの埋め込み・グラフ・OCR、
    ES は作り直し中に新旧 2 世代が並ぶので新しい 1 世代ぶん（今の実測が詰めた後の大きさなので、取り込み中の最大に直して比べる）。
    """
    peak = 1.0 if settled else _ES_PEAK_FACTOR
    if mode == "register" or delta is None:
        need = _family_needs(total, dim)
        need["es"] *= peak
        need["neo4j"] += _NEO4J_FIXED_INITIAL
        need["logs"] = _logs_need(_file_total(total), log_bytes)
        need["tmp"] = _tmp_need(total)
        return {k: int(math.ceil(v)) for k, v in need.items()}
    changed = _file_total(delta)
    full = _family_needs(total, dim)
    part = _family_needs(delta, dim)
    need = {
        "derived": part["derived"] if linked else full["derived"],
        "embed": part["embed"],
        "es": max(full["es"], float(es_actual or 0)) * peak if changed else 0.0,
        "neo4j": part["neo4j"] + (_NEO4J_FIXED_UPDATE if changed else 0),
        "ocr": part["ocr"],
        "logs": _logs_need(changed, log_bytes) if changed else 0.0,
        "tmp": _tmp_need(delta) if changed else 0.0,
    }
    return {k: int(math.ceil(v)) for k, v in need.items()}


# ---- 置き場とファイルシステム ----

@dataclass
class Fs:
    key: tuple | None
    free: int = 0
    total: int = 0
    reason: str | None = None   # key が None（確かめられない）ときの理由


def _existing_ancestor(path: Path) -> Path | None:
    p = Path(path)
    for _ in range(64):
        try:
            os.stat(p)
            return p
        except OSError:
            if p.parent == p:
                return None
            p = p.parent
    return None


def fs_of(path: Path) -> Fs:
    """`path`（無ければ最も近い親）が乗るファイルシステムと空き。同じ `st_dev` は同じ置き場として合算する。"""
    anc = _existing_ancestor(path)
    if anc is None:
        return Fs(None, reason="場所が見つかりません")
    try:
        du = shutil.disk_usage(anc)
        return Fs(("dev", os.stat(anc).st_dev), du.free, du.total)
    except OSError:
        return Fs(None, reason="空きを読めません")


def _compose_project() -> str:
    return os.environ.get("COMPOSE_PROJECT_NAME") or "sherpa-mvp"


def _docker_mountpoint(volume: str) -> str | None:
    """Docker の名前付きボリュームの置き場所（ホストから見たパス）。読めなければ None。"""
    try:
        r = subprocess.run(["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", volume],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and out else None


def _fs_of_exact(path: str) -> Fs | None:
    """ホストから実際に読めるパスだけ（親へはさかのぼらない＝別の場所の空きを返さない）。"""
    try:
        du = shutil.disk_usage(path)
        return Fs(("dev", os.stat(path).st_dev), du.free, du.total)
    except OSError:
        return None


def docker_filesystems() -> tuple[Fs, Fs]:
    """`(ES, Neo4j)` が乗るファイルシステム。読めない・分からないときは理由つきの未確認にする（推測で足りるとしない）。"""
    proj = _compose_project()
    es_mp = _docker_mountpoint(f"{proj}_es")
    neo_mp = _docker_mountpoint(f"{proj}_neo4j")
    es_fs = _fs_of_exact(es_mp) if es_mp else None
    if es_fs is None:
        st = es_index.fs_stats()
        es_fs = Fs(("es",), st["free"], st["total"]) if st else Fs(None, reason="全文検索の仕組みに問い合わせられません")
    neo_fs = _fs_of_exact(neo_mp) if neo_mp else None
    if neo_fs is None:
        # 2 つのボリュームが Docker の同じ保存先（…/volumes/<名前>/_data）にあれば、同じファイルシステムに乗る。
        if neo_mp and es_mp and Path(neo_mp).parent.parent == Path(es_mp).parent.parent and es_fs.key:
            neo_fs = Fs(es_fs.key, es_fs.free, es_fs.total)
        else:
            neo_fs = Fs(None, reason="データの保管場所を読めません")
    return es_fs, neo_fs


def place_filesystems(world_id: str) -> dict[str, Fs]:
    es_fs, neo_fs = docker_filesystems()
    obs = worlds.observation_base_dir()
    return {
        "derived": fs_of(worlds.derived_dir(world_id)),
        "embed": fs_of(worlds.semantic_dir(world_id)),
        "ocr": fs_of(obs),
        "logs": fs_of(log_setup.run_dir()),
        "tmp": fs_of(Path(tempfile.gettempdir())),
        "es": es_fs,
        "neo4j": neo_fs,
    }


def _log_bytes() -> int:
    try:
        return sum(p.stat().st_size for p in log_setup.run_dir().glob("*.log*") if p.is_file())
    except OSError:
        return 0


def conv_cache_linked(world_id: str, sample: int = 20) -> bool:
    """変換のキャッシュが派生物とハードリンクで実体を共有しているか（公開中の rag の数ファイルを見て、過半数が 2 以上なら共有）。
    キャッシュが無い・読めないときは共有していない側（全ぶんの新しい組が要る）に倒す。
    """
    rag = worlds.derived_rag_dir(world_id)
    links: list[int] = []
    try:
        for dp, _dn, fns in os.walk(rag):
            for f in fns:
                links.append(os.stat(os.path.join(dp, f)).st_nlink)
                if len(links) >= sample:
                    break
            if len(links) >= sample:
                break
    except OSError:
        return False
    return bool(links) and sum(1 for n in links if n >= 2) * 2 > len(links)


# ---- 判定 ----

def fmt_bytes(n: int) -> str:
    """画面用の大きさ。必要量は切り上げ側に寄せて見せる。"""
    if n >= GIB:
        return f"{math.ceil(n / GIB * 10) / 10:g}GB"
    return f"{max(1, math.ceil(n / (1024 * 1024)))}MB"


def judge(need: int, free: int) -> str:
    if free < need:
        return "short"
    if free < need * TIGHT_FACTOR:
        return "tight"
    return "ok"


def assess(needs: dict, fss: dict[str, Fs], es_settled: int | None = None) -> dict:
    """置き場ごとの必要量（ES は取り込み中の最大）を、同じファイルシステム同士で合算して空きと比べる。
    `es_settled` は ES が取り込みの終わりに詰まった後の大きさ（画面に添えるだけで判定には使わない）。
    """
    groups: dict = {}
    for place in PLACES:
        fs = fss[place]
        key = fs.key if fs.key is not None else ("unknown", place)
        g = groups.setdefault(key, {"fs": fs, "places": []})
        g["places"].append(place)
    lines = []
    for g in groups.values():
        fs: Fs = g["fs"]
        places = [p for p in g["places"] if needs.get(p, 0) > 0]
        if not places:
            continue
        need = sum(needs[p] for p in places)
        names = "・".join(PLACE_LABELS[p] for p in places)
        if fs.key is None:
            lines.append({"status": "unknown", "label": names, "need_bytes": need, "free_bytes": None,
                          "total_bytes": None, "reason": fs.reason, "places": places,
                          "text": f"{names}: 空きを確かめられません（必要な空きは約{fmt_bytes(need)}。{fs.reason}）"})
            continue
        status = judge(need, fs.free)
        note = ""
        if status == "ok" and "es" in places and fs.total and (fs.total - fs.free + need) / fs.total >= ES_WATERMARK:
            status = "tight"
            note = "。全文検索は使用率が9割を超えると新しい索引を作れなくなります"
        if "es" in places and es_settled:
            note += f"。全文検索の索引は取り込みが終わると約{fmt_bytes(es_settled)}に詰まります"
        verdict = {"ok": "足ります", "tight": "余裕が少ない見込みです", "short": "足りない見込みです"}[status]
        lines.append({"status": status, "label": names, "need_bytes": need, "free_bytes": fs.free,
                      "total_bytes": fs.total, "reason": None, "places": places,
                      "text": f"{names}: 取り込みに必要な空き（取り込み中の最大）約{fmt_bytes(need)}／今の空き {fmt_bytes(fs.free)}"
                              f"（{verdict}）{note}"})
    lines.sort(key=lambda x: -STATUS_ORDER[x["status"]])
    worst = lines[0]["status"] if lines else "ok"
    return {"status": worst, "lines": lines, "need_bytes": sum(needs.values())}


# ---- 実行（背景で数えて結果を取りに来る形） ----

@dataclass
class _Job:
    root: str
    world_id: str | None
    mode: str
    state: str = "counting"
    scanned: int = 0
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    result: dict | None = None
    error: str | None = None


_JOBS: dict[str, _Job] = {}
_LOCK = threading.Lock()


def estimate_now(root: str, world_id: str | None, *, progress=None, deadline: float | None = None) -> dict:
    """原本を数えて見積もりを作る（同期）。DB・ES・Docker・ディスクの外部境界はここから呼ぶ。"""
    mode = "update" if world_id else "register"
    manifest = None
    if world_id:
        row = store.get_world(world_id) or {}
        manifest = row.get("last_manifest") or {}
    total, delta = count_sources(root, manifest=manifest, deadline=deadline, progress=progress)
    # 更新の足場: 今の ES の実測・キャッシュのリンク状況
    es_actual = es_index.store_bytes(world_id) if world_id else None
    linked = conv_cache_linked(world_id) if world_id else True
    needs = compute_needs(total, delta if world_id else None, mode=mode, dim=_embed_dim(), linked=linked,
                          es_actual=es_actual, log_bytes=_log_bytes())
    fss = place_filesystems(world_id or "_new")
    settled = compute_needs(total, delta if world_id else None, mode=mode, dim=_embed_dim(), linked=linked,
                            es_actual=es_actual, log_bytes=_log_bytes(), settled=True)
    res = assess(needs, fss, es_settled=settled["es"])
    res["es_settled_bytes"] = settled["es"]
    changed = _file_total(delta) if world_id else None
    res.update({
        "mode": mode,
        "files": _file_total(total),
        "bytes": sum(v[1] for v in total["families"].values()),
        "changed_files": changed,
        "needs": needs,
        "unchanged": bool(world_id) and not changed,
    })
    return res


def _run(job_key: str) -> None:
    job = _JOBS[job_key]
    try:
        res = estimate_now(job.root, job.world_id, deadline=time.monotonic() + _WALK_DEADLINE_SEC,
                           progress=lambda n: setattr(job, "scanned", n))
        job.result, job.state = res, "ready"
    except Exception as e:
        _log.warning("disk_estimate: 見積もりに失敗しました root=%s (%s)", job.root, e.__class__.__name__,
                     exc_info=True)
        job.error, job.state = e.__class__.__name__, "failed"
    finally:
        job.finished = time.monotonic()


def _snapshot(job: _Job) -> dict:
    out = {"state": job.state, "mode": job.mode, "scanned": job.scanned}
    if job.state == "ready":
        out.update(job.result or {})
    elif job.state == "failed":
        out["message"] = "必要な空きを見積もれませんでした。取り込みはそのまま始められます"
    return out


def request_estimate(root: str, world_id: str | None) -> dict:
    """見積もりを始める（同じ場所を数え中ならそれに合流）。数え終えていればその結果を返す。何度呼んでもよい。"""
    key = f"{world_id or '-'}|{root}"
    now = time.monotonic()
    with _LOCK:
        for k in [k for k, j in _JOBS.items() if j.finished and now - j.finished > _RESULT_TTL_SEC]:
            _JOBS.pop(k, None)
        job = _JOBS.get(key)
        if job is not None and job.finished and (
                now - job.finished > _RESULT_TTL_SEC or (job.state == "failed" and now - job.finished > _FAILED_RETRY_SEC)):
            job = None
        if job is not None and not job.finished and now - job.started > _WALK_DEADLINE_SEC + _STUCK_GRACE_SEC:
            job = None   # 1 回のシステムコールで止まった走査（応答しないマウント先）には合流し続けない
        if job is None:
            job = _JOBS[key] = _Job(root=root, world_id=world_id, mode="update" if world_id else "register")
            threading.Thread(target=_run, args=(key,), name="disk-estimate", daemon=True).start()
        return _snapshot(job)


def latest(root: str, world_id: str | None) -> dict | None:
    """直近に数え終えた結果（無ければ None）。取り込みを始めたときのログ用。"""
    with _LOCK:
        job = _JOBS.get(f"{world_id or '-'}|{root}")
        if job is None or job.state != "ready" or not job.finished or time.monotonic() - job.finished > _RESULT_TTL_SEC:
            return None
        return job.result


def log_start(op: str, root: str, world_id: str | None) -> None:
    """取り込みを始めたときの 1 行ログ。見積もりが無い・読めないときもその旨を残す。警告つきで始めたなら WARNING。"""
    try:
        res = latest(root, world_id)
        if res is None:
            _log.info("disk_estimate: 取り込み開始 op=%s world=%s 見積もりなし", op, world_id)
            return
        free = [ln for ln in res["lines"] if ln["free_bytes"] is not None]
        parts = " / ".join(
            f"{'+'.join(ln['places'])}:必要{fmt_bytes(ln['need_bytes'])}・空き"
            f"{fmt_bytes(ln['free_bytes']) if ln['free_bytes'] is not None else '不明'}・{ln['status']}"
            for ln in res["lines"])
        level = logging.WARNING if res["status"] in ("short", "tight") else logging.INFO
        _log.log(level, "disk_estimate: 取り込み開始 op=%s world=%s 判定=%s 必要=%s 空き確認=%d か所 %s",
                 op, world_id, res["status"], fmt_bytes(res["need_bytes"]), len(free), parts)
    except Exception:
        _log.debug("disk_estimate: 開始ログを出せませんでした", exc_info=True)
