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

def _parse_page_spec(spec: str | None, total: int, max_count: int) -> tuple[list[int], bool]:
    """`spec` を 1-based ページ番号の昇順リストへ解決する（`total` 範囲外・非数値は無視）。
    `spec` 省略／空文字は先頭から `max_count` 件。戻り値 `(ページ番号一覧, truncated)`（`truncated` は `max_count` で切り詰めたか）。
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
        if stop:
            break
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _sep, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
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
                continue
            if 1 <= p <= total and p not in seen:
                seen.add(p)
                result.append(p)
                if len(result) > max_count:
                    stop = True
    result.sort()
    truncated = len(result) > max_count
    return result[:max_count], truncated


# ---- Excel（openpyxl）----

_CELL_MAX_CHARS = 200


def _cell_str(v, clean: Callable[[str], str] | None) -> str:
    if v is None:
        return ""
    s = str(v)
    if clean is not None:
        s = clean(s)  # 伏せ字→切り詰めの順
    return s[:_CELL_MAX_CHARS]


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
    """セル範囲を表で返す（`range_a1` 省略時は先頭から `max_rows`×`max_cols`）。
    セル値は文字列化し 1 セル `_CELL_MAX_CHARS` 文字で切る。`clean` は切り詰める前に適用し、`KeyBlockRedactor(clean)` を行優先で 1 つ使い回す
    （鍵ブロックが複数セルにまたがっても伏せ続ける）。`max_rows`／`max_cols` は 1〜既定値にクランプする。
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
                win = cells[min_col - scan_min_col:min_col - scan_min_col + width]
                rows_out.append(win + [""] * (width - len(win)))
        if budget.tick(every=1):
            return dict(_TIME_ERROR)
    except Exception:
        return {"error": "セル範囲の読み取りに失敗しました"}
    from openpyxl.utils import get_column_letter
    actual_range = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(eff_max_col)}{eff_max_row}"
    return {"sheet": sheet, "range": actual_range, "rows": rows_out, "truncated": truncated}


# ---- Word（python-docx）----

_DOCX_TABLE_MAX = 20
_DOCX_TABLE_ROW_MAX = 50


def docx_paragraphs(f, start: int = 0, count: int = 200,
                    clean: Callable[[str], str] | None = None, table_start: int = 0,
                    table_row_start: int = 0) -> dict:
    """段落（`i`＝絶対インデックス・`style`・`text`）と表（`table_start` から 20 表・各表は `table_row_start` から 50 行）を返す。
    表の続きは `table_start`／`table_row_start` を進めて呼び直す。
    `clean` は段落・表セルのテキストへ適用する。`clean` があれば `KeyBlockRedactor(clean)` を 1 個作り、`doc.iter_inner_content()`
    （段落と表の本文出現順）を辿って窓外の要素も含めて全て通す（出力は窓内のみ）。両方の窓が済むまで辿る（片方が残る間は窓外も状態のため辿る）。
    表の窓が最後の出力なら、その表は出力行の窓までしか辿らない。
    結合セルは `id(cell._tc)` で初出だけを通し、以降は初出の結果を再利用する（END の二重検出を防ぐ）。
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

        def _apply_cell(cell) -> str:
            tc = cell._tc
            tc_id = id(tc)
            if tc_id in tc_seen:
                return tc_seen[tc_id]
            tc_keepalive.append(tc)
            val = _apply(cell.text)
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
        return {"total": total, "total_tables": total_tables, "paragraphs": out_paras,
                "tables": tables_out, "truncated": truncated}
    except Exception:
        return {"error": "Word の読み取りに失敗しました"}


# ---- PowerPoint（python-pptx）----

_PPTX_SLIDES_MAX = 20


def pptx_slides(f, pages: str | None = "1-10",
               clean: Callable[[str], str] | None = None) -> dict:
    """スライドのテキスト・表・ノートを返す（1 回 20 枚まで）。`clean` はテキスト・表・ノートへ適用し、`KeyBlockRedactor(clean)` を
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
        nos, truncated = _parse_page_spec(pages, total, _PPTX_SLIDES_MAX)
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
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    t = shape.text_frame.text
                    if t:
                        texts.append(_apply(t))
                if getattr(shape, "has_table", False):
                    tables.append([[_apply(cell.text) for cell in row.cells]
                                  for row in shape.table.rows])
            notes = None
            if getattr(slide, "has_notes_slide", False):
                notes = _apply(slide.notes_slide.notes_text_frame.text)
            if no in want:
                out.append({"no": no, "texts": texts, "tables": tables, "notes": notes})
        return {"total": total, "slides": out, "truncated": truncated}
    except Exception:
        return {"error": "PowerPoint の読み取りに失敗しました"}


# ---- PDF（pdfplumber）----

_PDF_PAGES_MAX = 10
_PDF_PAGE_TEXT_MAX_CHARS = 20000


def pdf_pages(f, pages: str | None = "1-5",
             clean: Callable[[str], str] | None = None) -> dict:
    """ページのテキストを返す（1 回 10 ページ・1 ページ 20,000 文字まで）。`clean` は切り詰める前に適用し、`KeyBlockRedactor(clean)` を
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
        nos, truncated = _parse_page_spec(pages, total, _PDF_PAGES_MAX)
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
            if len(text) > _PDF_PAGE_TEXT_MAX_CHARS:
                item["text_truncated"] = True
            out.append(item)
        return {"total": total, "pages": out, "truncated": truncated}
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
