"""原本直読ツールの中核（純関数・開いた binary file object を受け取るだけ・書き込みなし）。
Codex（MCP 経由）と API 経路の頭脳が同じ関数で Excel／Word／PowerPoint／PDF／テキスト・コードの中身を読む。
設計: docs/design/codex.md「MCP の道具」

呼び出し元（`agentic_search.run_tool`）が doc_id→実パスの解決（封じ込め・秘匿名除外・範囲・層）と、
検査した実パスと開いた実体の一致確認（dev/ino 突合）を済ませた後の open 済みファイルをここへ渡す。
このモジュールは資料フォルダ・範囲・秘匿判定・path→fd の解決を知らない。

- サイズ上限はここで一括して見る（`SHERPA_DOC_READ_MAX_BYTES`・既定 50 MiB）。xlsx／docx／pptx は zip 展開後サイズも見る
  （`SHERPA_DOC_READ_MAX_UNZIP_BYTES`・既定 200 MiB）。例外は種別だけを残し、パス・内容はエラーにもログにも出さない。
- セル／段落／ページのテキストは「伏せ字（`clean`）→切り詰め」の順で処理する（先に切ると秘密パターンが断片化して検出をすり抜ける）。
  `clean` は呼び出し元が `agentic_search._redact` を渡す（省略時は無処理）。
- `clean` があれば `redact_keys.KeyBlockRedactor(clean)`（1 呼び出しで使い捨て）でラップし、原本どおりの並び（xlsx は行優先、
  docx は段落と表の本文出現順、pptx はスライド→shape 順、pdf はページ順、file_head は全文）で切り詰めより前に適用する。
  要求範囲が途中から始まる場合も、xlsx は 1 行目から・pptx／pdf は要求範囲の `_KEY_LOOKBACK_PAGES` 前から要求範囲の終わりまで辿って
  状態を確定し、出力だけを要求範囲に絞る（範囲より前で始まる鍵ブロックの漏れを防ぐ）。
- ファイルオブジェクトの所有権: 即時 reject（サイズ超過・zip 展開超過）の時だけここが close する。読み込みに進んだ場合
  （`_cached_load`）は、キャッシュ命中で不要になった今回分だけ close し、新規ロード分は読み込んだオブジェクトに所有権が移る
  （追い出し後も明示 close しない）。
"""
from __future__ import annotations

import os
import struct
import threading
import time
import zipfile
import zlib
from collections import OrderedDict
from typing import Callable

from . import redact_keys, text_encoding

# ---- サイズ上限（既定 50 MiB・env で上書き可）----

_MAX_BYTES_DEFAULT = 50 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def _max_bytes() -> int:
    return _env_int("SHERPA_DOC_READ_MAX_BYTES", _MAX_BYTES_DEFAULT)


_SIZE_ERROR = {"error": "大きすぎて開けません（サイズ）"}


def _too_big_fd(f) -> bool:
    """`f` の `fstat` サイズが上限を超えるか。stat 不能はここでは判定しない（実際に読む箇所で捕まる）。"""
    try:
        return os.fstat(f.fileno()).st_size > _max_bytes()
    except OSError:
        return False


# ---- zip 展開サイズ上限（xlsx/docx/pptx は zip・zip bomb 対策・既定 200 MiB）----
# central directory の `file_size` だけで見積もる（展開はしない）

_MAX_UNZIP_BYTES_DEFAULT = 200 * 1024 * 1024
_MAX_ZIP_ENTRIES = 10_000
_MAX_SINGLE_ENTRY_BYTES = 100 * 1024 * 1024
_UNZIP_SIZE_ERROR = {"error": "大きすぎて開けません（展開サイズ）"}


def _max_unzip_bytes() -> int:
    return _env_int("SHERPA_DOC_READ_MAX_UNZIP_BYTES", _MAX_UNZIP_BYTES_DEFAULT)


def _zip_bounds_error(f) -> dict | None:
    """`f` を zip として開き、central directory の `file_size`・件数だけで上限超過なら `_UNZIP_SIZE_ERROR` を返す（展開しない）。
    壊れた zip は None（実パーサの例外処理に任せる）。`f` の読み取り位置は `finally` で先頭へ戻す。
    """
    try:
        f.seek(0)
        with zipfile.ZipFile(f) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        return None
    except OSError:
        return None
    finally:
        try:
            f.seek(0)
        except OSError:
            pass
    if len(infos) > _MAX_ZIP_ENTRIES:
        return dict(_UNZIP_SIZE_ERROR)
    total = 0
    for info in infos:
        if info.file_size > _MAX_SINGLE_ENTRY_BYTES:
            return dict(_UNZIP_SIZE_ERROR)
        total += info.file_size
        if total > _max_unzip_bytes():
            return dict(_UNZIP_SIZE_ERROR)
    return None


_ZIP_READ_CHUNK_BYTES = 1024 * 1024  # entry を実測する際の読み流しチャンク
_ZIP_SUPPORTED_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_LOCAL_HEADER_FIXED_SIZE = 30  # local file header の固定部サイズ
_LOCAL_HEADER_SIG = b"PK\x03\x04"
_OFFICE_OPEN_ERROR = {"error": "Office ファイルを開けませんでした"}


def _zip_entry_data_start(f, info: "zipfile.ZipInfo") -> int | None:
    """`info.header_offset` の local file header を自分で読み、圧縮データの開始位置を返す（name_len／extra_len は local header 自身から読む）。
    読めない・シグネチャ不一致なら None（呼び出し元は拒否）。
    """
    try:
        f.seek(info.header_offset)
        header = f.read(_LOCAL_HEADER_FIXED_SIZE)
    except OSError:
        return None
    if len(header) != _LOCAL_HEADER_FIXED_SIZE or header[:4] != _LOCAL_HEADER_SIG:
        return None
    name_len, extra_len = struct.unpack_from("<HH", header, 26)
    return info.header_offset + _LOCAL_HEADER_FIXED_SIZE + name_len + extra_len


def _has_overlapping_ranges(bounds: list[tuple[int, int]]) -> bool:
    """圧縮データ区間 `[start, end)` の集合に重なり（同一 `header_offset` を含む）があるか。境界が接するだけなら許す。"""
    ordered = sorted(bounds)
    prev_end = -1
    for start, end in ordered:
        if start < prev_end:
            return True
        prev_end = max(prev_end, end)
    return False


def _zip_actual_size_error(f) -> dict | None:
    """展開サイズの実測検査。central directory の宣言値（`file_size`／CRC）に依存せず、各 entry の圧縮データを local file header から特定し、
    `zlib.decompressobj(-15)` で `max_length` 付きに伸長して実出力バイト数を累計する（stored は読んだ分が出力）。
    累計が展開上限を超えたら拒否。加えて (a) 圧縮データを使い切っても終端に達しない、(b) 実出力が宣言 `file_size` と食い違う、
    (c) 圧縮方式が stored／deflate 以外、のいずれも拒否する。
    伸長の前に全 entry の圧縮データ区間の重なりを見て拒否する（同じ実データを entry 数だけ伸長させる増幅を防ぐ）。
    """
    total = 0
    budget = _max_unzip_bytes()
    try:
        f.seek(0)
        with zipfile.ZipFile(f) as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            data_starts: list[int] = []
            for info in infos:
                data_start = _zip_entry_data_start(f, info)
                if data_start is None:
                    return dict(_UNZIP_SIZE_ERROR)
                data_starts.append(data_start)
            if _has_overlapping_ranges([(ds, ds + info.compress_size)
                                       for ds, info in zip(data_starts, infos)]):
                return dict(_UNZIP_SIZE_ERROR)
            for info, data_start in zip(infos, data_starts):
                if info.compress_type not in _ZIP_SUPPORTED_METHODS:
                    return dict(_UNZIP_SIZE_ERROR)
                try:
                    f.seek(data_start)
                except OSError:
                    return dict(_UNZIP_SIZE_ERROR)
                remaining = info.compress_size
                actual = 0
                decompressor = (zlib.decompressobj(-15)
                                if info.compress_type == zipfile.ZIP_DEFLATED else None)
                while remaining > 0:
                    chunk = f.read(min(_ZIP_READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        return dict(_UNZIP_SIZE_ERROR)  # compress_size ぶん読み切れない＝壊れている
                    remaining -= len(chunk)
                    if decompressor is None:
                        out_len = len(chunk)  # stored: 読んだ分がそのまま出力
                    else:
                        try:
                            out_len = len(decompressor.decompress(chunk, max(1, budget - total + 1)))
                        except zlib.error:
                            return dict(_UNZIP_SIZE_ERROR)
                    actual += out_len
                    total += out_len
                    if total > budget:
                        return dict(_UNZIP_SIZE_ERROR)
                if decompressor is not None and not decompressor.eof:
                    return dict(_UNZIP_SIZE_ERROR)
                if actual != info.file_size:
                    return dict(_UNZIP_SIZE_ERROR)
    except zipfile.BadZipFile:
        return None
    except OSError:
        return None
    finally:
        try:
            f.seek(0)
        except OSError:
            pass
    return None


def _precheck_office(f) -> dict | None:
    """xlsx／docx／pptx 共通の事前検査（サイズ→zip 展開上限の見積もり→展開上限の実測）。reject する場合は `f` を close してエラー dict を返す。問題なければ None。
    検査が想定外の例外（zip 内のファイル名を含みうる）を送出しても、固定文言に丸めて member 名を外部へ漏らさない。
    """
    if _too_big_fd(f):
        _close_quiet(f)
        return dict(_SIZE_ERROR)
    try:
        err = _zip_bounds_error(f)
        if err is not None:
            _close_quiet(f)
            return err
        err = _zip_actual_size_error(f)
        if err is not None:
            _close_quiet(f)
            return err
    except Exception:
        _close_quiet(f)
        return dict(_OFFICE_OPEN_ERROR)
    return None


# ---- 解析済みオブジェクトのキャッシュ（fstat (dev, ino, mtime_ns, size) キー・LRU 8 件）----
# 同じファイルへの複数回の呼び出しで毎回 zip／XML を読み直さないための実行時キャッシュ（プロセス内のみ）

# 鍵ブロックの状態を確定するために選択ページより前を辿る幅（xlsx は XML を先頭から流す構造のため 1 行目から辿る）
_KEY_LOOKBACK_PAGES = 50  # pptx/pdf: 選択ページの直前これだけを先読みして状態を確定する
# 1 回の読取に使える時間（秒・env で上書き可）。超過したら明示エラーで止める
_MAX_SECONDS_DEFAULT = 10.0
_TIME_ERROR = {"error": "大きすぎて開けません（時間）"}


def _max_seconds() -> float:
    raw = os.environ.get("SHERPA_DOC_READ_MAX_SECONDS")
    try:
        v = float(raw) if raw is not None else _MAX_SECONDS_DEFAULT
    except ValueError:
        v = _MAX_SECONDS_DEFAULT
    return v if v > 0 else _MAX_SECONDS_DEFAULT


class _TimeBudget:
    """読取ループの時間予算。`tick()` を要素ごとに呼び、超過したら True を返す。"""

    def __init__(self):
        self._t0 = time.monotonic()
        self._limit = _max_seconds()
        self._n = 0

    def tick(self, every: int = 256) -> bool:
        self._n += 1
        if self._n % every:
            return False
        return (time.monotonic() - self._t0) > self._limit


def _scan_pages_with_lookback(nos: list[int]) -> list[int]:
    """選択ページごとに直前 `_KEY_LOOKBACK_PAGES` ページを先読み対象に加えた昇順の走査集合。"""
    scan: set[int] = set()
    for no in nos:
        scan.update(range(max(1, no - _KEY_LOOKBACK_PAGES), no + 1))
    return sorted(scan)


def _reset_dims_keeping_record(ws) -> None:
    """`reset_dimensions` の前に、記録寸法を初回だけ worksheet に退避する（予算超過時の縮退に使う）。"""
    if not hasattr(ws, "_sherpa_rec_dims"):
        ws._sherpa_rec_dims = (ws.max_row, ws.max_column)
    ws.reset_dimensions()


class _TimeOver(Exception):
    """読取の時間予算超過（呼び出し側が `_TIME_ERROR` に変換する）。"""


def _ws_dims(ws, budget: "_TimeBudget | None" = None) -> tuple[int, int]:
    """シートの実寸（行数・列数）。記録寸法（<dimension>）には頼らず実データを走査して数える（時間予算つき・超過は `_TimeOver`）。"""
    _reset_dims_keeping_record(ws)
    budget = budget or _TimeBudget()  # 呼び出し全体で共有できる
    rows = 0
    cols = 0
    for row in ws.iter_rows(values_only=True):
        if budget.tick(every=1):  # 幅の広い行でも時間が掛かるため毎行判定
            raise _TimeOver()
        rows += 1
        cols = max(cols, len(row))
    if budget.tick(every=1):
        raise _TimeOver()
    return max(1, rows), max(1, cols)


_XLSX_MAX_ROWS = 1048576  # Excel の行・列の上限（これを超える範囲指定は拒否）
_XLSX_MAX_COLS = 16384

_CACHE_CAP = 8
_CACHE_ENTRY_MAX_BYTES = 8 * 1024 * 1024  # これより大きい入力はキャッシュしない
_CACHE_TOTAL_MAX_BYTES = 32 * 1024 * 1024  # 保持中の入力バイト合計の上限
_cache_lock = threading.Lock()
_cache: "OrderedDict[tuple, object]" = OrderedDict()


def _close_quiet(obj) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _cached_load(f, loader):
    """`loader(f)` の結果を `f` の fstat キーで LRU（8 件）キャッシュする。
    命中: 今回の `f` を close して既存オブジェクトを返す。不命中: `loader(f)` を呼び、`f` の所有権は戻り値に移る（ここで close しない）。
    `loader` が例外なら登録しない。追い出しは参照を落とすだけで close しない。
    """
    try:
        st = os.fstat(f.fileno())
        key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        if st.st_size > _CACHE_ENTRY_MAX_BYTES:  # 大きな入力はキャッシュしない
            key = None
    except OSError:
        key = None
    if key is not None:
        with _cache_lock:
            obj = _cache.get(key)
            if obj is not None:
                _cache.move_to_end(key)
                hit = True
            else:
                hit = False
        if hit:
            _close_quiet(f)
            return obj
    obj = loader(f)
    if key is not None:
        with _cache_lock:
            _cache[key] = obj
            _cache.move_to_end(key)
            # 件数と、保持している入力バイト合計の両方で上限を掛ける
            while len(_cache) > _CACHE_CAP or sum(k[3] for k in _cache) > _CACHE_TOTAL_MAX_BYTES:
                if len(_cache) <= 1:
                    break
                _cache.popitem(last=False)  # 参照を落とすだけ（close しない）
    return obj


# ---- ページ指定のパース（"3" / "2-5" / "1,3,5"・pptx/pdf 共通）----

def _parse_page_spec(spec: str | None, total: int, max_count: int,
                     ignored: list[str] | None = None) -> tuple[list[int], bool]:
    """`spec` を 1-based ページ番号の昇順リストへ解決する（`total` 範囲外・非数値は無視）。
    `spec` 省略／空文字は先頭から `max_count` 件。戻り値 `(ページ番号一覧, truncated)`（`truncated` は `max_count` で切り詰めたか）。
    `ignored` を渡すと、無視した指定（非数値・`total` 範囲外にかかる部分）の文字列をそこへ積む（呼び出し元が注記として返す）。
    範囲指定は列挙前に `[1, total]` へ切り、`max_count` を超えた時点で列挙を打ち切る（巨大な範囲指定で長時間占有させない）。
    """
    spec = (spec or "").strip()
    if not spec:
        pages = list(range(1, min(total, max_count) + 1))
        return pages, total > max_count
    seen: set[int] = set()
    result: list[int] = []
    stop = False
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if stop:
            # 件数の上限に達した後も、残りの指定は検証だけ行い、無効な指定を黙って捨てない
            if ignored is not None:
                try:
                    a, sep, b = part.partition("-")
                    lo, hi = (int(a), int(b)) if sep else (int(part), int(part))
                    if min(lo, hi) < 1 or max(lo, hi) > total:
                        ignored.append(part)
                except ValueError:
                    ignored.append(part)
            continue
        if "-" in part:
            a, _sep, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                if ignored is not None:
                    ignored.append(part)
                continue
            if lo > hi:
                lo, hi = hi, lo
            if ignored is not None and (lo < 1 or hi > total):
                ignored.append(part)
            lo = max(lo, 1)  # total 範囲外を列挙前に切る
            hi = min(hi, total)
            for p in range(lo, hi + 1):
                if p not in seen:
                    seen.add(p)
                    result.append(p)
                    if len(result) > max_count:
                        stop = True
                        break
        else:
            try:
                p = int(part)
            except ValueError:
                if ignored is not None:
                    ignored.append(part)
                continue
            if ignored is not None and not 1 <= p <= total:
                ignored.append(part)
            if 1 <= p <= total and p not in seen:
                seen.add(p)
                result.append(p)
                if len(result) > max_count:
                    stop = True
    result.sort()
    truncated = len(result) > max_count
    return result[:max_count], truncated


def _resolve_pages(spec: str | None, total: int, default_spec: str, default_count: int,
                   hard_max: int) -> tuple[list[int], bool, dict | None, dict]:
    """pdf／pptx 共通のページ指定の解決。戻り値 `(ページ番号, truncated, エラー, 追加の申告欄)`。
    指定が無い・既定の指定（`default_spec`）のままなら先頭 `default_count` 件で、残りがあれば `truncated`＋`pages_remaining`。
    非数値・範囲外の指定は黙って捨てず `pages_ignored` に返し、有効なページが 1 つも無ければエラーにする。
    """
    notes: dict = {}
    spec_s = (spec or "").strip()
    if not spec_s or spec_s == default_spec:
        nos, trunc = _parse_page_spec(None, total, default_count)
        if trunc:
            notes["pages_remaining"] = total - len(nos)
            notes["default_range"] = True
        return nos, trunc, None, notes
    ignored: list[str] = []
    nos, trunc = _parse_page_spec(spec_s, total, hard_max, ignored)
    if ignored:
        notes["pages_ignored"] = ignored[:20]
        if len(ignored) > 20:
            notes["pages_ignored_total"] = len(ignored)
    if trunc:
        notes["pages_capped"] = hard_max  # 1 回に返せるページ数の上限で指定の一部を返していない
    if not nos:
        return [], False, {"error": f"ページ指定が不正または範囲外です（総ページ数 {total}）"}, notes
    return nos, trunc, None, notes


# ---- Excel（openpyxl）----

_CELL_MAX_CHARS = 32767  # Excel 自体の 1 セル上限（通常のセルは切らない）
_XLSX_TOTAL_CHARS_MAX = 2_000_000  # 1 回の返却で積む文字数の上限（超えたら行単位で打ち切り印を付ける）
_FORMULA_NO_VALUE = "値なし（数式）"  # 数式セルで保存値（キャッシュ）が無いときの表示


def _cell_str(v, clean: Callable[[str], str] | None) -> str:
    if v is None:
        return ""
    s = str(v)
    if clean is not None:
        s = clean(s)  # 伏せ字→切り詰めの順
    return s[:_CELL_MAX_CHARS]


def _formula_cells_without_value(f, sheet: str, min_row: int, max_row: int, min_col: int, max_col: int,
                                 empty_pos: set[tuple[int, int]]) -> set[tuple[int, int]] | None:
    """`empty_pos`（保存値が空だったセルの (行, 列)）のうち、数式セルだったものの集合。数式側のブックを同じ範囲だけ読んで判定する。
    読めなければ None（呼び出し元は「数式の判定ができなかった」と申告する）。
    """
    try:
        import openpyxl
        f.seek(0)
        wb = openpyxl.load_workbook(f, read_only=True, data_only=False)
        ws = wb[sheet]
        found: set[tuple[int, int]] = set()
        budget = _TimeBudget()
        for r, row in enumerate(ws.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col,
                                              values_only=True), start=min_row):
            if budget.tick(every=1):
                return None
            for c, v in enumerate(row, start=min_col):
                if (r, c) in empty_pos and isinstance(v, str) and v.startswith("="):
                    found.add((r, c))
        return found
    except Exception:
        return None


def _load_workbook(f):
    import openpyxl
    f.seek(0)
    return openpyxl.load_workbook(f, read_only=True, data_only=True)


def xlsx_sheets(f) -> dict:
    """シート一覧と大きさ（`{"sheets": [{"name","max_row","max_col"}]}`）。開けなければ `{"error"}`。
    実寸の走査が時間予算を超えたシート以降は、シート名は返し、寸法は記録値（`dims_estimated: true`・記録が無ければ寸法キーを出さない）。
    `f` は呼び出し元が検証済みで開いたファイル（所有権はモジュール docstring 参照）。
    """
    err = _precheck_office(f)
    if err is not None:
        return err
    try:
        wb = _cached_load(f, _load_workbook)
        out = []
        budget = _TimeBudget()  # ブック全体で 1 つの予算
        over = False
        for ws in wb.worksheets:
            if not over:
                try:
                    r, c = _ws_dims(ws, budget)
                    out.append({"name": ws.title, "max_row": r, "max_col": c})
                    continue
                except _TimeOver:
                    over = True
            # 予算超過: シート名は必ず返す。寸法は記録値＝推定（記録が無ければキーを出さない）
            rec_r, rec_c = getattr(ws, "_sherpa_rec_dims", (ws.max_row, ws.max_column))
            entry = {"name": ws.title, "dims_estimated": True}
            if rec_r:
                entry["max_row"] = rec_r
            if rec_c:
                entry["max_col"] = rec_c
            out.append(entry)
        return {"sheets": out}
    except Exception:
        return {"error": "Excel を開けませんでした"}


def xlsx_range(f, sheet: str, range_a1: str | None = None,
              max_rows: int = 200, max_cols: int = 50,
              clean: Callable[[str], str] | None = None) -> dict:
    """`_xlsx_range` の入口。キャッシュ命中で `f` が閉じられても数式の判定に使えるよう、`f` の複製（別 fd）を用意して渡す。"""
    try:
        formula_f = os.fdopen(os.dup(f.fileno()), "rb")
    except (OSError, ValueError):
        formula_f = None
    try:
        return _xlsx_range(f, formula_f, sheet, range_a1, max_rows, max_cols, clean)
    finally:
        if formula_f is not None:
            _close_quiet(formula_f)


def _xlsx_range(f, formula_f, sheet: str, range_a1: str | None, max_rows: int, max_cols: int,
                clean: Callable[[str], str] | None) -> dict:
    """セル範囲を表で返す（`range_a1` 省略時は先頭から `max_rows`×`max_cols`）。
    セル値は文字列化する（Excel の 1 セル上限 `_CELL_MAX_CHARS` を超える分だけ切り、`cells_clipped` に件数）。`clean` は切り詰める前に適用し、
    `KeyBlockRedactor(clean)` を行優先で 1 つ使い回す（鍵ブロックが複数セルにまたがっても伏せ続ける）。`max_rows`／`max_cols` は 1〜既定値にクランプする。
    数式で保存値が無いセルは空にせず `_FORMULA_NO_VALUE` を返し（`formula_no_value` に件数）、判定できなければ `formula_check: "unavailable"`。
    積んだ文字数が `_XLSX_TOTAL_CHARS_MAX` を超えたら行単位で打ち切り `size_clipped: true`。
    上限を超えたら切り詰めて `truncated: true`（`range` は実際に返した範囲）。`clean` がある場合、状態のため 1 行目から選択範囲の最終行まで
    （行全体）を辿り、選択外の行は出力に残さない。
    """
    err = _precheck_office(f)
    if err is not None:
        return err
    try:
        wb = _cached_load(f, _load_workbook)
    except Exception:
        return {"error": "Excel を開けませんでした"}
    if sheet not in wb.sheetnames:
        return {"error": "シートが見つかりません"}
    ws = wb[sheet]
    try:
        max_rows = int(max_rows) if max_rows is not None else 200
    except (TypeError, ValueError):
        max_rows = 200
    max_rows = min(max(max_rows, 1), 200)  # 上限もクランプ
    try:
        max_cols = int(max_cols) if max_cols is not None else 50
    except (TypeError, ValueError):
        max_cols = 50
    max_cols = min(max(max_cols, 1), 50)
    _range_budget = _TimeBudget()  # 呼び出し全体（実寸算出＋走査）で 1 つの予算
    try:
        if range_a1:
            from openpyxl.utils.cell import range_boundaries
            min_col, min_row, max_col, max_row = range_boundaries(range_a1)
            if None in (min_col, min_row, max_col, max_row):
                return {"error": "range が不正です"}
        else:
            min_col, min_row = 1, 1
            max_row, max_col = _ws_dims(ws, _range_budget)
    except _TimeOver:
        return dict(_TIME_ERROR)
    except Exception:
        return {"error": "range が不正です"}
    if max_row > _XLSX_MAX_ROWS or max_col > _XLSX_MAX_COLS or min_row > _XLSX_MAX_ROWS or min_col > _XLSX_MAX_COLS:
        return {"error": "range が不正です（Excel の上限を超えています）"}  # 巨大な行番号で空行を延々と辿らせない
    req_rows = max_row - min_row + 1
    req_cols = max_col - min_col + 1
    truncated = req_rows > max_rows or req_cols > max_cols
    eff_max_row = min(max_row, min_row + max_rows - 1)
    eff_max_col = min(max_col, min_col + max_cols - 1)
    cleaner = redact_keys.KeyBlockRedactor(clean) if clean is not None else None
    try:
        # 状態は原本の先頭行から選択範囲の終わりまで（行優先）辿って確定し、出力だけ要求範囲に絞る
        # （選択範囲より前で始まる鍵ブロックの漏れを防ぐ。時間は `_TimeBudget` で上限を掛ける）
        scan_from = 1 if cleaner is not None else min_row
        # 列も状態のためには 1 列目から行全体（実データの全列）を辿り、出力だけ列窓（min_col..eff_max_col）に切る（足りない列は空文字）
        scan_min_col = 1 if cleaner is not None else min_col
        scan_max_col = None if cleaner is not None else eff_max_col
        if cleaner is not None:
            _reset_dims_keeping_record(ws)  # max_col=None でも記録寸法に補完されないよう先に捨てる
        width = eff_max_col - min_col + 1
        rows_out = []
        empty_pos: set[tuple[int, int]] = set()
        clipped_cells = 0
        total_chars = 0
        size_clipped = False
        last_row = eff_max_row
        budget = _range_budget
        for row_no, row in enumerate(
            ws.iter_rows(min_row=scan_from, max_row=eff_max_row, min_col=scan_min_col, max_col=scan_max_col,
                        values_only=True),
            start=scan_from,
        ):
            if budget.tick(every=1):  # 幅の広い行は 1 行が重いため毎行判定
                return dict(_TIME_ERROR)
            cells = [_cell_str(v, cleaner) for v in row]
            if row_no >= min_row:
                row_chars = sum(len(c) for c in cells[min_col - scan_min_col:min_col - scan_min_col + width])
                if rows_out and total_chars + row_chars > _XLSX_TOTAL_CHARS_MAX:  # この行を足すと上限超え＝この行以降を返さない
                    size_clipped = True
                    last_row = row_no - 1
                    break
                lo = min_col - scan_min_col
                win = cells[lo:lo + width]
                for ci, v in enumerate(row[lo:lo + width]):
                    if v is None:
                        empty_pos.add((row_no, min_col + ci))
                    elif len(str(v)) > _CELL_MAX_CHARS:
                        clipped_cells += 1
                total_chars += sum(len(c) for c in win)
                rows_out.append(win + [""] * (width - len(win)))
        if budget.tick(every=1):
            return dict(_TIME_ERROR)
    except Exception:
        return {"error": "セル範囲の読み取りに失敗しました"}
    formula_cells: set[tuple[int, int]] | None = set()
    if empty_pos:
        formula_cells = (_formula_cells_without_value(formula_f, sheet, min_row, last_row, min_col, eff_max_col, empty_pos)
                         if formula_f is not None else None)
        if formula_cells:
            for (r, c) in formula_cells:
                rows_out[r - min_row][c - min_col] = _FORMULA_NO_VALUE
    from openpyxl.utils import get_column_letter
    actual_range = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(eff_max_col)}{last_row}"
    out = {"sheet": sheet, "range": actual_range, "rows": rows_out, "truncated": truncated or size_clipped}
    if size_clipped:
        out["size_clipped"] = True
    if clipped_cells:
        out["cells_clipped"] = clipped_cells
    if formula_cells:
        out["formula_no_value"] = len(formula_cells)
    if formula_cells is None:
        out["formula_check"] = "unavailable"
    return out


# ---- Word（python-docx）----

_DOCX_TABLE_MAX = 20
_DOCX_TABLE_ROW_MAX = 50
_DOCX_NESTED_DEPTH_MAX = 3  # 表の中の表を辿る深さ
_DOCX_NESTED_ROWS_MAX = 200  # 1 つの入れ子の表から読む行数
_DOCX_EXTRA_ITEMS_MAX = 50  # ヘッダー・フッター／テキストボックス／脚注の各種別で返す件数
_DOCX_EXTRA_TEXT_MAX = 2000  # 1 件あたりの文字数

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return "{%s}%s" % (_W_NS, tag)


def _in_fallback(el) -> bool:
    """`mc:AlternateContent` の代替表現（Fallback）の中か（同じ図形が二重に数えられるのを避ける）。"""
    p = el.getparent()
    while p is not None:
        if isinstance(p.tag, str) and p.tag.endswith("}Fallback"):
            return True
        p = p.getparent()
    return False


def _el_paragraph_lines(root) -> list[str]:
    """XML 要素の下の段落（`w:p`）ごとの文字列（`w:t`・`w:tab`・`w:br` を連結）。"""
    lines = []
    for p in root.iter(_w("p")):
        buf = []
        for n in p.iter():
            if n.tag == _w("t") and n.text:
                buf.append(n.text)
            elif n.tag == _w("tab"):
                buf.append("\t")
            elif n.tag in (_w("br"), _w("cr")):
                buf.append("\n")
        lines.append("".join(buf))
    return lines


def _docx_extras(doc, clean: Callable[[str], str] | None) -> dict:
    """本文の段落・表の外にある文字（ヘッダー・フッター・テキストボックス・脚注／文末脚注）と、読めない図形・グラフ等の件数。
    項目ごとに新しい伏せ字処理を掛け（本文の状態を持ち越さない）、件数・文字数の上限で切ったら `extras_clipped` に件数を返す。
    """
    out: dict = {}
    clipped = 0

    def _fin(t: str) -> str | None:
        nonlocal clipped
        t = t or ""
        if not t.strip():
            return None
        if clean is not None:
            t = redact_keys.KeyBlockRedactor(clean)(t)
        if len(t) > _DOCX_EXTRA_TEXT_MAX:
            clipped += 1
            t = t[:_DOCX_EXTRA_TEXT_MAX]
        return t

    hf: list[dict] = []
    seen_hf: set[tuple[str, str]] = set()
    try:
        for si, sec in enumerate(doc.sections):
            for kind, part in (("header", sec.header), ("first_page_header", sec.first_page_header),
                               ("even_page_header", sec.even_page_header), ("footer", sec.footer),
                               ("first_page_footer", sec.first_page_footer),
                               ("even_page_footer", sec.even_page_footer)):
                if part.is_linked_to_previous:
                    continue
                lines = [p.text for p in part.paragraphs]
                for tb in part.tables:
                    for row in tb.rows:
                        lines.append(" | ".join(c.text for c in row.cells))
                text = _fin("\n".join(lines))
                if text is None or (kind, text) in seen_hf:
                    continue
                seen_hf.add((kind, text))
                hf.append({"section": si, "kind": kind, "text": text})
    except Exception:
        out["headers_footers_unread"] = True
    if hf:
        if len(hf) > _DOCX_EXTRA_ITEMS_MAX:
            clipped += len(hf) - _DOCX_EXTRA_ITEMS_MAX
        out["headers_footers"] = hf[:_DOCX_EXTRA_ITEMS_MAX]

    body = doc.element.body
    boxes: list[str] = []
    try:
        roots = [body]
        for sec in doc.sections:
            for part in (sec.header, sec.first_page_header, sec.even_page_header,
                         sec.footer, sec.first_page_footer, sec.even_page_footer):
                if not part.is_linked_to_previous:
                    roots.append(part._element)
        txs = [tx for root in roots for tx in root.iter(_w("txbxContent"))]
        for tx in txs:
            if _in_fallback(tx):
                continue
            text = _fin("\n".join(_el_paragraph_lines(tx)))
            if text is not None and text not in boxes:
                boxes.append(text)
    except Exception:
        out["textboxes_unread"] = True
    if boxes:
        if len(boxes) > _DOCX_EXTRA_ITEMS_MAX:
            clipped += len(boxes) - _DOCX_EXTRA_ITEMS_MAX
        out["textboxes"] = boxes[:_DOCX_EXTRA_ITEMS_MAX]

    for key, reltype in (("footnotes", "/footnotes"), ("endnotes", "/endnotes")):
        notes: list[dict] = []
        try:
            from lxml import etree
            for rel in doc.part.rels.values():
                if rel.is_external or not rel.reltype.endswith(reltype):
                    continue
                root = etree.fromstring(rel.target_part.blob)
                for fn in root.iter(_w(key[:-1])):
                    if fn.get(_w("type")) in ("separator", "continuationSeparator", "continuationNotice"):
                        continue
                    text = _fin("\n".join(_el_paragraph_lines(fn)))
                    if text is not None:
                        notes.append({"id": fn.get(_w("id")), "text": text})
        except Exception:
            out[f"{key}_unread"] = True
        if notes:
            if len(notes) > _DOCX_EXTRA_ITEMS_MAX:
                clipped += len(notes) - _DOCX_EXTRA_ITEMS_MAX
            out[key] = notes[:_DOCX_EXTRA_ITEMS_MAX]

    try:
        unread = 0
        for d in body.iter(_w("drawing"), _w("pict"), _w("object")):
            if _in_fallback(d) or any(True for _ in d.iter(_w("txbxContent"))):
                continue
            unread += 1
        if unread:
            out["unread_objects"] = unread  # 画像・グラフ・図・埋め込みオブジェクト（文字は読めない）
    except Exception:
        pass
    if clipped:
        out["extras_clipped"] = clipped
    return out


def docx_paragraphs(f, start: int = 0, count: int = 200,
                    clean: Callable[[str], str] | None = None, table_start: int = 0,
                    table_row_start: int = 0) -> dict:
    """段落（`i`＝絶対インデックス・`style`・`text`）と表（`table_start` から 20 表・各表は `table_row_start` から 50 行）を返す。
    表の続きは `table_start`／`table_row_start` を進めて呼び直す。
    `clean` は段落・表セルのテキストへ適用する。`clean` があれば `KeyBlockRedactor(clean)` を 1 個作り、`doc.iter_inner_content()`
    （段落と表の本文出現順）を辿って窓外の要素も含めて全て通す（出力は窓内のみ）。両方の窓が済むまで辿る（片方が残る間は窓外も状態のため辿る）。
    表の窓が最後の出力なら、その表は出力行の窓までしか辿らない。
    結合セルは `id(cell._tc)` で初出だけを通し、以降は初出の結果を再利用する（END の二重検出を防ぐ）。
    セルの中の表は `_DOCX_NESTED_DEPTH_MAX` 段まで行を ` | ` 連結でセル文字列に続ける（深さ・行数の上限を超えた分は `nested_tables_clipped` に件数）。
    最初の窓（`start`・`table_start`・`table_row_start` が全て 0）にだけ、`_docx_extras` の結果（ヘッダー・フッター・テキストボックス・脚注・
    読めない図形の件数）を載せる。
    """
    err = _precheck_office(f)
    if err is not None:
        return err
    try:
        import docx
        from docx.table import Table as _DocxTable
        doc = _cached_load(f, lambda ff: docx.Document(ff))
    except Exception:
        return {"error": "Word を開けませんでした"}
    try:
        start = max(0, int(start or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        count = max(1, int(count or 200))
    except (TypeError, ValueError):
        count = 200
    try:
        t_start = max(0, int(table_start or 0))
        r_start = max(0, int(table_row_start or 0))
    except (TypeError, ValueError):
        t_start, r_start = 0, 0
    try:
        cleaner = redact_keys.KeyBlockRedactor(clean) if clean is not None else None

        def _apply(s: str) -> str:
            return cleaner(s) if cleaner is not None else s

        total = len(doc.paragraphs)
        total_tables = len(doc.tables)
        table_window_hi = t_start + _DOCX_TABLE_MAX
        row_window_hi = r_start + _DOCX_TABLE_ROW_MAX
        tables_truncated = total_tables > table_window_hi

        out_paras: list[dict] = []
        tables_out: list[dict] = []
        p_idx = 0
        t_idx = 0
        # 結合セルは `row.cells` が同一の `_tc` を複数回返す。同じ `_tc` を 2 度ラッパーへ通すと END が重複して状態が壊れるため、
        # `id(cell._tc)` で初出のみ処理して結果を再利用する。`tc_keepalive` で `_tc` を生かし続け、GC による `id()` の再利用を防ぐ
        tc_seen: dict[int, str] = {}
        tc_keepalive: list = []
        nested_clipped = 0

        def _cell_with_nested(cell, depth: int) -> str:
            nonlocal nested_clipped
            val = _apply(cell.text)
            nested = list(cell.tables)
            if not nested:
                return val
            parts = [val] if val else []
            for nt in nested:
                if depth >= _DOCX_NESTED_DEPTH_MAX:
                    nested_clipped += 1
                    continue
                for ri, nrow in enumerate(nt.rows):
                    if ri >= _DOCX_NESTED_ROWS_MAX:
                        nested_clipped += 1
                        break
                    parts.append(" | ".join(_cell_with_nested(c, depth + 1) for c in nrow.cells))
            return "\n".join(parts)

        def _apply_cell(cell) -> str:
            tc = cell._tc
            tc_id = id(tc)
            if tc_id in tc_seen:
                return tc_seen[tc_id]
            tc_keepalive.append(tc)
            val = _cell_with_nested(cell, 1)
            tc_seen[tc_id] = val
            return val

        # 走査は「出力に残す最後の要素」を過ぎたら打ち切る。表は出力行の窓（row_window_hi）まで。窓より前の要素は状態のためだけに辿る
        budget = _TimeBudget()
        for item in doc.iter_inner_content():
            if budget.tick(every=64):
                return dict(_TIME_ERROR)
            paras_done = p_idx >= min(start + count, total)
            tables_done = t_idx >= min(table_window_hi, total_tables)
            if paras_done and tables_done:
                break
            if isinstance(item, _DocxTable):
                want_table = t_start <= t_idx < table_window_hi
                rows_all = item.rows
                n_rows = len(rows_all)
                rows_out = []
                # 出力窓の後ろに出力する要素が残るなら、この表の全行を状態のために辿る。残らないなら出力行の窓まで
                more_output_follows = (not paras_done) or (t_idx + 1 < min(table_window_hi, total_tables))
                row_limit = n_rows if more_output_follows else (row_window_hi if want_table else 0)
                for ri, row in enumerate(rows_all):
                    if ri >= row_limit:
                        break
                    if budget.tick():
                        return dict(_TIME_ERROR)
                    cells_out = [_apply_cell(cell) for cell in row.cells]
                    if want_table and r_start <= ri < row_window_hi:
                        rows_out.append(cells_out)
                if want_table:
                    if n_rows > row_window_hi:
                        tables_truncated = True
                    tables_out.append({"i": t_idx, "row_start": r_start,
                                       "total_rows": n_rows, "rows": rows_out})
                t_idx += 1
            else:
                in_window = start <= p_idx < start + count
                if paras_done and tables_done:
                    break
                cleaned = _apply(item.text)  # 窓外の段落も状態のために辿る（出力はしない）
                if in_window:
                    out_paras.append({"i": p_idx, "style": (item.style.name if item.style else None),
                                      "text": cleaned})
                p_idx += 1
        if budget.tick(every=1):
            return dict(_TIME_ERROR)
        truncated = (start + count) < total or tables_truncated
        result = {"total": total, "total_tables": total_tables, "paragraphs": out_paras,
                  "tables": tables_out, "truncated": truncated}
        if nested_clipped:
            result["nested_tables_clipped"] = nested_clipped
        if start == 0 and t_start == 0 and r_start == 0:
            result.update(_docx_extras(doc, clean))
        return result
    except Exception:
        return {"error": "Word の読み取りに失敗しました"}


# ---- PowerPoint（python-pptx）----

_PPTX_SLIDES_MAX = 20
_PPTX_DEFAULT_PAGES = "1-10"
_PPTX_GROUP_DEPTH_MAX = 8
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# 文字を読めない種類（画像・埋め込みオブジェクト・動画等）。件数だけ返す
_PPTX_UNREAD_TYPES = frozenset({7, 10, 11, 12, 13, 16, 22, 23, 26})


def _pptx_walk(shapes, slide, apply, texts: list, tables: list, st: dict, depth: int = 0) -> None:
    """shape を出現順に辿り、文字・表・グラフのタイトル・SmartArt の文字を `texts`／`tables` へ積む。グループは再帰する。
    読めない種類（画像・埋め込みオブジェクト等）と、深さ上限を超えたグループは `st["unread"]` に件数を足す。
    """
    for shape in shapes:
        try:
            stype = int(shape.shape_type) if shape.shape_type is not None else None
        except Exception:
            stype = None
        if stype == 6:  # GROUP
            if depth >= _PPTX_GROUP_DEPTH_MAX:
                st["unread"] += 1
            else:
                _pptx_walk(shape.shapes, slide, apply, texts, tables, st, depth + 1)
            continue
        if getattr(shape, "has_text_frame", False):
            t = shape.text_frame.text
            if t:
                texts.append(apply(t))
        if getattr(shape, "has_table", False):
            tables.append([[apply(cell.text) for cell in row.cells] for row in shape.table.rows])
            continue
        if getattr(shape, "has_chart", False):
            try:
                chart = shape.chart
                bits = []
                if chart.has_title and chart.chart_title.has_text_frame:
                    bits.append(chart.chart_title.text_frame.text)
                for plot in chart.plots:
                    for ser in plot.series:
                        if ser.name:
                            bits.append(str(ser.name))
                if bits:
                    texts.append(apply("[グラフ] " + " / ".join(b for b in bits if b)))
            except Exception:
                st["unread"] += 1
            continue
        el = getattr(shape, "_element", None)
        is_diagram = stype == 21
        if not is_diagram and el is not None:  # python-pptx は SmartArt の shape_type を None で返すため、graphicData の uri でも判定する
            try:
                is_diagram = any(isinstance(n.tag, str) and n.tag.endswith("}graphicData")
                                 and "diagram" in (n.get("uri") or "") for n in el.iter())
            except Exception:
                is_diagram = False
        if is_diagram and el is not None:  # DIAGRAM（SmartArt）
            try:
                from lxml import etree
                rid = None
                for n in el.iter():
                    if isinstance(n.tag, str) and n.tag.endswith("}relIds"):
                        rid = n.get("{%s}dm" % _R_NS)
                        break
                words = []
                if rid:
                    root = etree.fromstring(slide.part.related_part(rid).blob)
                    words = [n.text for n in root.iter("{%s}t" % _A_NS) if n.text]
                if words:
                    texts.append(apply("[SmartArt] " + " / ".join(words)))
                else:
                    st["unread"] += 1
            except Exception:
                st["unread"] += 1
            continue
        if stype in _PPTX_UNREAD_TYPES:
            st["unread"] += 1


def pptx_slides(f, pages: str | None = _PPTX_DEFAULT_PAGES,
               clean: Callable[[str], str] | None = None) -> dict:
    """スライドのテキスト・表・ノートを返す（1 回 20 枚まで）。グループの中・グラフのタイトルと系列名・SmartArt の文字も `texts` に含め、
    読めない図形（画像・埋め込みオブジェクト等）はスライドごとに `unread_shapes`（件数）で返す。
    ページ指定が既定のまま・省略で残りがあれば `truncated`＋`pages_remaining`、範囲外・不正な指定は `pages_ignored`（有効が 0 ならエラー）。`clean` はテキスト・表・ノートへ適用し、`KeyBlockRedactor(clean)` を
    スライド→shape 順で使い回す。走査は選択範囲の `_KEY_LOOKBACK_PAGES` 枚前から最終スライドまで行い、選択外は出力に残さない。
    """
    err = _precheck_office(f)
    if err is not None:
        return err
    try:
        import pptx
        prs = _cached_load(f, lambda ff: pptx.Presentation(ff))
    except Exception:
        return {"error": "PowerPoint を開けませんでした"}
    try:
        cleaner = redact_keys.KeyBlockRedactor(clean) if clean is not None else None

        def _apply(s: str) -> str:
            return cleaner(s) if cleaner is not None else s

        slides_list = list(prs.slides)
        total = len(slides_list)
        nos, truncated, page_err, page_notes = _resolve_pages(pages, total, _PPTX_DEFAULT_PAGES, 10, _PPTX_SLIDES_MAX)
        if page_err is not None:
            return {**page_err, **page_notes}
        want = set(nos)
        # 鍵ブロックの状態は選択スライドごとに直前 _KEY_LOOKBACK_PAGES 枚から辿って確定し、出力だけ要求範囲に絞る（`clean` 無しは要求スライドのみ）
        scan_nos = _scan_pages_with_lookback(nos) if (cleaner is not None and nos) else nos
        out = []
        budget = _TimeBudget()
        prev_no = None
        for no in scan_nos:
            if budget.tick(every=1):
                return dict(_TIME_ERROR)
            if cleaner is not None and prev_no is not None and no != prev_no + 1:
                cleaner = redact_keys.KeyBlockRedactor(clean)  # 離れた区間へ前区間の状態を持ち越さない
            prev_no = no
            slide = slides_list[no - 1]
            texts, tables = [], []
            st = {"unread": 0}
            _pptx_walk(slide.shapes, slide, _apply, texts, tables, st)
            notes = None
            if getattr(slide, "has_notes_slide", False):
                notes = _apply(slide.notes_slide.notes_text_frame.text)
            if no in want:
                item = {"no": no, "texts": texts, "tables": tables, "notes": notes}
                if st["unread"]:
                    item["unread_shapes"] = st["unread"]
                out.append(item)
        return {"total": total, "slides": out, "truncated": truncated, **page_notes}
    except Exception:
        return {"error": "PowerPoint の読み取りに失敗しました"}


# ---- PDF（pdfplumber）----

_PDF_PAGES_MAX = 10
_PDF_DEFAULT_PAGES = "1-5"
_PDF_NO_TEXT_NOTE = "文字なし（スキャンの可能性）"
_PDF_PAGE_TEXT_MAX_CHARS = 20000


def pdf_pages(f, pages: str | None = _PDF_DEFAULT_PAGES,
             clean: Callable[[str], str] | None = None) -> dict:
    """ページのテキストを返す（1 回 10 ページ・1 ページ 20,000 文字まで）。文字層が無いページは `no_text_layer: true`＋`note` で返す。
    ページ指定が既定のまま・省略で残りがあれば `truncated`＋`pages_remaining`、範囲外・不正な指定は `pages_ignored`（有効が 0 ならエラー）。`clean` は切り詰める前に適用し、`KeyBlockRedactor(clean)` を
    ページ順で使い回す。走査は選択ページの `_KEY_LOOKBACK_PAGES` ページ前から最終ページまで行い、選択外は出力に残さない。
    """
    if _too_big_fd(f):
        _close_quiet(f)
        return dict(_SIZE_ERROR)
    try:
        import pdfplumber
        f.seek(0)
        pdf = _cached_load(f, lambda ff: pdfplumber.open(ff))
    except Exception:
        return {"error": "PDF を開けませんでした"}
    try:
        cleaner = redact_keys.KeyBlockRedactor(clean) if clean is not None else None
        total = len(pdf.pages)
        nos, truncated, page_err, page_notes = _resolve_pages(pages, total, _PDF_DEFAULT_PAGES, 5, _PDF_PAGES_MAX)
        if page_err is not None:
            return {**page_err, **page_notes}
        want = set(nos)
        # 鍵ブロックの状態は選択ページごとに直前 _KEY_LOOKBACK_PAGES ページから辿って確定し、出力だけ要求範囲に絞る（`clean` 無しは要求ページのみ）
        scan_nos = _scan_pages_with_lookback(nos) if (cleaner is not None and nos) else nos
        out = []
        budget = _TimeBudget()
        prev_no = None
        for no in scan_nos:
            if budget.tick(every=1):
                return dict(_TIME_ERROR)
            if cleaner is not None and prev_no is not None and no != prev_no + 1:
                cleaner = redact_keys.KeyBlockRedactor(clean)  # 離れた区間へ前区間の状態を持ち越さない
            prev_no = no
            text = pdf.pages[no - 1].extract_text() or ""
            if cleaner is not None and text:
                text = cleaner(text)
            if no not in want:
                continue
            item = {"no": no, "text": text[:_PDF_PAGE_TEXT_MAX_CHARS]}
            if not text.strip():
                item["no_text_layer"] = True
                item["note"] = _PDF_NO_TEXT_NOTE
            if len(text) > _PDF_PAGE_TEXT_MAX_CHARS:
                item["text_truncated"] = True
            out.append(item)
        return {"total": total, "pages": out, "truncated": truncated, **page_notes}
    except Exception:
        return {"error": "PDF の読み取りに失敗しました"}


# ---- 先頭バイト（テキスト・コード共通の軽量プレビュー）----

_FILE_HEAD_DEFAULT = 65536


def file_head(f, max_bytes: int = _FILE_HEAD_DEFAULT,
             clean: Callable[[str], str] | None = None) -> dict:
    """先頭 max_bytes バイトを UTF-8 / CP932 で返す。不正・途中で切れた文字は置換する。
    符号化の判定は先頭の最大 DETECT_CAP_BYTES を対象に、返却量とは独立に行う（max_bytes を超える範囲を読むことがある）。
    clean は返却対象の全文に適用する（max_bytes の境界をまたぐ秘密は未読）。渡された f はこの関数が閉じる。
    """
    if _too_big_fd(f):
        _close_quiet(f)
        return dict(_SIZE_ERROR)
    try:
        cap = max(1, int(max_bytes or _FILE_HEAD_DEFAULT))
    except (TypeError, ValueError):
        cap = _FILE_HEAD_DEFAULT
    try:
        size = os.fstat(f.fileno()).st_size
        enc = text_encoding.detect_fd(f.fileno())
        f.seek(0)
        raw = f.read(cap)
    except OSError:
        # 固定の理由コード（呼び出し元が `backend_failures["read_io"]` へ反映する）
        _close_quiet(f)
        return {"error": "ファイルを開けませんでした", "error_code": "read_io_failed"}
    _close_quiet(f)
    text = text_encoding.decode(raw, enc)
    if clean is not None and text:
        # 他の `doc_readers` 関数と同じ経路に揃え、全文を 1 回で伏せ字にする
        text = redact_keys.KeyBlockRedactor(clean)(text)
    return {"size": size, "text": text, "truncated": size > len(raw)}
