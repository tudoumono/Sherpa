"""原本直読ツールの中核（純関数・**開いた binary file object** を受け取るだけ・書き込み一切なし）。

Codex（MCP 経由）と API 経路の頭脳（OpenAI/Gemini のツール呼び出し）が**同じ関数**で原本
（Excel／Word／PowerPoint／PDF／テキスト・コード）の中身を読む
（`docs/proposals/2026-09-10-Codex原本直読と調査スキル.md` §2-9）。毎回 Python を書かせず
シート・段落・ページを直接返すことで、トークンと実行時間を削り再現性を上げる——Codex がコードを
書くのは突合・集計など定型外の作業だけにする。

呼び出し元（`agentic_search.run_tool`）が doc_id→実パスの解決（封じ込め・秘匿名除外・scope・層）を
済ませ、さらに**検査した実パスと開いた実体が一致すること**（TOCTOU 対策・dev/ino 突合）まで
確認した**後**の open 済みファイルオブジェクトをここへ渡す——このモジュール自身は world/scope/
秘匿判定も、path→fd の解決も一切知らない（純粋に file object と引数だけを受ける）。

サイズ上限は呼び出し元でなくここで一括して見る（`SHERPA_DOC_READ_MAX_BYTES`・既定 50 MiB・
`os.fstat(f.fileno())` で判定）。xlsx/docx/pptx は zip 展開後サイズも見る
（`SHERPA_DOC_READ_MAX_UNZIP_BYTES`・既定 200 MiB・zip bomb 対策）。例外は種別だけを残して握る
（パス・内容はエラーメッセージにもログにも出さない）。

セル/段落/ページのテキストは**伏せ字（`clean`）→切り詰め**の順で処理する——先に切ってから
伏せ字を掛けると、秘密パターン（PEM 鍵等）が切断境界をまたいだ場合に断片化して検出をすり抜ける
（`agentic_search._redact` の正規表現は無傷の断片にしか効かない）。`clean` は呼び出し元
（`run_tool`）が `agentic_search._redact` を渡す（省略時は無処理＝`doc_readers` 単体のテストで
redaction 抜きの生セルを検証できるようにするため）。

さらに `clean` が渡された場合、各関数は `clean` を直接使わず `redact_keys.KeyBlockRedactor(clean)`
（1回の呼び出しにつき使い捨て）でラップして使う——秘密鍵ブロック（PEM `PRIVATE KEY`）の
BEGIN/END は**原本の出現順**で見た1構造単位（1セル・1段落・1表行・1スライド・1ページ）を
またぐことがあり、`clean` 単体（同一文字列内でしか対にならない）では取りこぼす。各関数は
自分が扱う構造の**原本どおりの並び**（xlsx は行優先、docx は `doc.iter_inner_content()` の
本文出現順＝段落と表が混在する並び、pptx はスライド→shape 順、pdf はページ順、file_head は
全文）で、切り詰めるより前にこの状態付きラッパーを適用する。要求範囲（`range_a1`/`pages`）が
原本の途中から始まる場合でも、状態は**xlsx は 1 行目から・pptx/pdf は要求範囲の一定幅前（`_KEY_LOOKBACK_PAGES`）から要求範囲の終わりまで**を辿って確定し、
出力だけを要求範囲に絞る（xlsx/pptx/pdf）——途中から読み始めると、選択範囲より前で始まった
鍵ブロックの BEGIN を見逃したまま END だけを見て状態が立たず、選択範囲内の鍵本文が漏れる。
docx は `doc.iter_inner_content()` で段落・表の両方の窓が済むまで全要素を辿る（片方でも残る間は窓外の要素も状態のために辿る）。

ファイルオブジェクトの所有権: 呼び出し元が渡した `f` は、各関数が「もう使わない」と分かった
時点（サイズ超過・zip 展開超過などの即時 reject）でのみここが明示的に close する。読み込みに
進んだ場合（`_cached_load` 経由）は、キャッシュ命中で不要になった今回分の `f` だけを close し、
新規ロード分は読み込んだオブジェクト（openpyxl の Workbook 等）に所有権が移る——キャッシュから
追い出された後も明示的には close しない（他スレッドが参照中の可能性があるため・GC に任せる）。
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

from . import redact_keys

# ---- サイズ上限（既定 50 MiB・env で上書き可）--------------------------------------------------

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
    """`f`（開いたバイナリファイル）の `fstat` サイズが上限を超えるか。stat 不能はここでは
    判定しない（実際に読む箇所で捕まる・fail-safe）。"""
    try:
        return os.fstat(f.fileno()).st_size > _max_bytes()
    except OSError:
        return False


# ---- zip 展開サイズ上限（xlsx/docx/pptx＝内部は zip・zip bomb 対策・既定 200 MiB）---------------
# 圧縮後サイズが上限内でも、展開（実解凍）サイズは無関係に巨大化しうる（高圧縮率の細工ファイル）。
# 実際に展開する前に central directory のメタデータ（`file_size`）だけで見積もる——展開自体はしない。

_MAX_UNZIP_BYTES_DEFAULT = 200 * 1024 * 1024
_MAX_ZIP_ENTRIES = 10_000
_MAX_SINGLE_ENTRY_BYTES = 100 * 1024 * 1024
_UNZIP_SIZE_ERROR = {"error": "大きすぎて開けません（展開サイズ）"}


def _max_unzip_bytes() -> int:
    return _env_int("SHERPA_DOC_READ_MAX_UNZIP_BYTES", _MAX_UNZIP_BYTES_DEFAULT)


def _zip_bounds_error(f) -> dict | None:
    """`f` を zip として開き、central directory の `file_size`（展開後サイズ・件数）だけを見て
    上限超過なら `_UNZIP_SIZE_ERROR` を返す（実際に展開しない＝安全に見積もる）。壊れた zip
    （`BadZipFile`）はここでは判定せず None——実際のパーサ（openpyxl/docx/pptx）の例外処理に
    「開けませんでした」の判定を委ねる（二重に別のエラー文言を出さない）。

    呼び出し前後の `f` の読み取り位置は呼び出し元の責務——ここは `finally` で必ず先頭へ戻す
    （zip central directory の走査で `f` の位置が動くため、後続の実パーサが正しく読めるように）。
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


_ZIP_READ_CHUNK_BYTES = 1024 * 1024   # entry を実測する際の読み流しチャンク（一括ロードしない）
_ZIP_SUPPORTED_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_LOCAL_HEADER_FIXED_SIZE = 30   # sig(4)+version(2)+flags(2)+method(2)+mtime(2)+mdate(2)+crc(4)+compress_size(4)+file_size(4)+name_len(2)+extra_len(2)
_LOCAL_HEADER_SIG = b"PK\x03\x04"
_OFFICE_OPEN_ERROR = {"error": "Office ファイルを開けませんでした"}


def _zip_entry_data_start(f, info: "zipfile.ZipInfo") -> int | None:
    """`info.header_offset` の local file header を自分で読み、圧縮データの開始位置を返す
    （central directory の `extra` フィールドは local header と長さが食い違いうるため、
    name_len/extra_len は central directory の値ではなく local header 自身から読む）。
    local header が読めない・シグネチャ不一致なら None（呼び出し元は拒否扱いにする）。
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
    """圧縮データ区間 `[start, end)` の集合に重複・重なり（同一 `header_offset` を含む）が
    あるかを判定する。境界だけが接する（`start == 直前の end`）ものは重ならないので許す。
    """
    ordered = sorted(bounds)
    prev_end = -1
    for start, end in ordered:
        if start < prev_end:
            return True
        prev_end = max(prev_end, end)
    return False


def _zip_actual_size_error(f) -> dict | None:
    """`_zip_bounds_error`（central directory の `file_size` 宣言値だけを合計する見積もり）は、
    その宣言値自体を実際より小さく偽装されると素通りする——central directory の生バイトを
    直接書き換えて `file_size`（と対応する CRC）を「偽装した宣言サイズぶんの実データ」に合わせて
    しまえば、宣言値に依存する検査（`zipfile.ZipFile.open()` は central directory の `file_size`
    に達した時点で decompress を打ち切り、そこまでの実データの CRC を central directory の CRC と
    突き合わせるだけ）は矛盾を検知できない——実際の展開後サイズは無関係に巨大になりうる。

    宣言値（`info.file_size`/CRC）に一切依存せず実測する。`zf.open()` は使わず、
    各 entry の圧縮データの開始位置を **local file header から自分で** 特定し
    （`_zip_entry_data_start`）、`info.compress_size` バイトを `_ZIP_READ_CHUNK_BYTES` 単位で
    読みながら `zlib.decompressobj(-15)` で伸長して（`max_length` 付き＝一括展開しない）実際の
    出力バイト数を累計する（stored は無圧縮＝読んだバイト数がそのまま出力）。累計が展開上限を
    超えた時点で拒否。加えて (a) 伸長ストリームが `compress_size` を使い切っても終端（eof）に
    達しない、(b) 実際の出力バイト数が申告 `file_size` と食い違う、(c) 圧縮方式が stored/deflate
    以外——のいずれでも拒否する（宣言値との整合性はここでも見るが、真の出力量を先に数え終えた
    **後**の付帯チェックに過ぎない＝宣言サイズを小さく偽装しても展開後の実サイズはもう誤魔化せない）。

    伸長を始める**前**に、全 entry の圧縮データ区間 `[data_start, data_start + compress_size)`
    の重複・重なり（同一 `header_offset` を複数 entry が指す場合を含む）を先に見て拒否する——
    central directory に大量の entry を書きつつ、それぞれの `header_offset` を同じ1件の local
    file header に向けさせれば、宣言サイズの検査（本関数の主目的）は entry 単位では通っても、
    同じ実データを entry の数だけ繰り返し伸長させられ計算コストが増幅する（宣言サイズ自体は
    1件ぶんのままで検出をすり抜ける）。
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
                        return dict(_UNZIP_SIZE_ERROR)   # compress_size ぶん読み切れない＝壊れている
                    remaining -= len(chunk)
                    if decompressor is None:
                        out_len = len(chunk)              # stored: 無圧縮＝読んだ分がそのまま出力
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
    """xlsx/docx/pptx 共通の事前検査（サイズ→zip 展開上限の見積もり→展開上限の実測）。reject する
    場合は `f` を close してエラー dict を返す（この時点では誰にも所有権が渡っていない）。
    問題なければ None。

    検査自体（`_zip_bounds_error`/`_zip_actual_size_error`）が想定外の例外
    （暗号化フラグ付き entry 等・zip 内のファイル名を含みうる）を送出した場合でも、呼び出し元へ
    そのまま伝播させず固定文言に丸める——member 名（攻撃者が自由に埋め込める）が例外メッセージ
    経由で外部 LLM の tool 結果やログへ漏れないようにする多層防御（`_zip_bounds_error`/
    `_zip_actual_size_error` は自身で判定できる異常は既に `BadZipFile`/`OSError` として個別に
    処理済みだが、ここは「それ以外の想定外」を最後に受け止める安全網）。
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


# ---- 解析済みオブジェクトのキャッシュ（fstat (dev, ino, mtime_ns, size) キー・LRU 8件）----------
# 同じファイルへの複数回の呼び出し（例: xlsx_sheets → xlsx_range を何度も・docx_paragraphs の
# ページング・pptx_slides/pdf_pages の複数範囲）で毎回 zip/XML を読み直さないための実行時キャッシュ
# （プロセス内のみ・世代管理や永続化はしない＝再取り込みの契約とは無関係）。

# 鍵ブロックの状態を確定するために選択ページより前を辿る幅。秘密鍵ブロックがこれを超えて跨ぐことは
# 現実に無く、長い PDF の末尾を読むたびに全ページを抽出するコストを避ける（xlsx は XML を先頭から
# 流す構造上、先読み幅を狭めても時間が縮まないので 1 行目から辿る）。
_KEY_LOOKBACK_PAGES = 50                 # pptx/pdf: 選択ページの直前これだけを先読みして状態を確定する
# 1 回の読取に使える時間（秒・env で上書き可）。openpyxl の read_only は指定行まで XML を先頭から
# 流すため、巨大なシートの末尾を読むと行数に比例して時間が掛かる＝上限を超えたら明示エラーで止める。
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
    """読取ループの時間予算。`tick()` を要素ごとに呼び、超過したら True を返す（呼び出し側が即エラー）。"""

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
    """選択ページごとに直前 `_KEY_LOOKBACK_PAGES` ページを先読み対象に加えた昇順の走査集合
    （離れた区間ごとに先読みする＝`1,300` のような指定で全ページを辿らない）。"""
    scan: set[int] = set()
    for no in nos:
        scan.update(range(max(1, no - _KEY_LOOKBACK_PAGES), no + 1))
    return sorted(scan)


def _reset_dims_keeping_record(ws) -> None:
    """`reset_dimensions`（破壊的・記録寸法を消す）の前に、記録寸法を初回だけ worksheet に退避する。
    どの経路（実寸算出・行全体走査）から reset しても、予算超過時の縮退に記録値を使えるようにする。"""
    if not hasattr(ws, "_sherpa_rec_dims"):
        ws._sherpa_rec_dims = (ws.max_row, ws.max_column)
    ws.reset_dimensions()


class _TimeOver(Exception):
    """読取の時間予算超過（呼び出し側が `_TIME_ERROR` に変換する）。"""


def _ws_dims(ws, budget: "_TimeBudget | None" = None) -> tuple[int, int]:
    """シートの実寸（行数・列数）。read_only の `max_row`/`max_column` は xlsx の <dimension> 記録に
    依存し、無い／実データより狭いブックでは None や過小になる＝記録には頼らず、常に実データを自前で
    走査して数える（時間予算つき・超過は `_TimeOver`）。"""
    _reset_dims_keeping_record(ws)
    budget = budget or _TimeBudget()          # 呼び出し全体で共有できる（シートごとにリセットしない）
    rows = 0
    cols = 0
    for row in ws.iter_rows(values_only=True):
        if budget.tick(every=1):              # 行数が少なくても幅の広い行で時間が掛かる＝毎行判定
            raise _TimeOver()
        rows += 1
        cols = max(cols, len(row))
    if budget.tick(every=1):
        raise _TimeOver()
    return max(1, rows), max(1, cols)


_XLSX_MAX_ROWS = 1048576                 # Excel の行・列の上限（これを超える範囲指定は拒否）
_XLSX_MAX_COLS = 16384

_CACHE_CAP = 8
_CACHE_ENTRY_MAX_BYTES = 8 * 1024 * 1024      # これより大きい入力はキャッシュしない（展開後は数十倍になる）
_CACHE_TOTAL_MAX_BYTES = 32 * 1024 * 1024     # 保持中の入力バイト合計の上限
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
    """`loader(f)` の結果を `f` の fstat（dev, ino, mtime_ns, size）キーで LRU（8件）キャッシュする。

    キャッシュ命中: 今回開いた `f`（もう使わない）を close して既存オブジェクトを返す。
    キャッシュ不命中: `loader(f)` を呼ぶ——**`f` の所有権はこの時点で `loader` の戻り値に移る**
    （openpyxl の read-only Workbook・pdfplumber の PDF は `f` を保持して遅延読取するため、
    ここで `f` を close してはいけない）。`loader` が例外を送出した場合はキャッシュへ登録しない
    （呼び出し元の `except` が捕まえる——`f` は他に参照が無ければ GC が自然に close する）。

    追い出し（LRU 満杯）は参照を落とすだけで明示 close しない——追い出された後も別スレッドが
    その場で受け取ったオブジェクトを使い続けている可能性があるため。
    """
    try:
        st = os.fstat(f.fileno())
        key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        if st.st_size > _CACHE_ENTRY_MAX_BYTES:   # 大きな入力は展開後のメモリが読めないためキャッシュしない
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
            # 件数と、保持している入力バイト合計（展開後の目安）の両方で上限を掛ける
            while len(_cache) > _CACHE_CAP or sum(k[3] for k in _cache) > _CACHE_TOTAL_MAX_BYTES:
                if len(_cache) <= 1:
                    break
                _cache.popitem(last=False)   # 参照を落とすだけ（close しない）
    return obj


# ---- ページ指定のパース（"3" / "2-5" / "1,3,5"・pptx/pdf 共通）---------------------------------

def _parse_page_spec(spec: str | None, total: int, max_count: int) -> tuple[list[int], bool]:
    """`spec` を 1-based ページ番号の昇順リストへ解決する（`total` 範囲外・非数値は無視）。

    `spec` 省略/空文字は先頭から `max_count` 件。戻り値は `(ページ番号一覧, truncated)`——
    `truncated` は指定範囲が `max_count` を超えて切り詰めたか（1回の呼び出しの上限）。

    範囲指定（`"1-10**12"` 等）は列挙前に `[1, total]` へ切ってから走査し、
    `max_count` を超えた時点で列挙自体を打ち切る——`total` を超える巨大な `hi` を
    そのまま `range(lo, hi+1)` に渡すと、`total=1` でも `"1-1000000000000"` の
    ような指定で長時間占有し、1回のツール呼び出しが返らなくなるため。
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
            lo = max(lo, 1)          # total 範囲外を列挙前に切る
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


# ---- Excel（openpyxl）--------------------------------------------------------------------------

_CELL_MAX_CHARS = 200


def _cell_str(v, clean: Callable[[str], str] | None) -> str:
    if v is None:
        return ""
    s = str(v)
    if clean is not None:
        s = clean(s)             # 伏せ字→切り詰めの順
    return s[:_CELL_MAX_CHARS]


def _load_workbook(f):
    import openpyxl
    f.seek(0)
    return openpyxl.load_workbook(f, read_only=True, data_only=True)


def xlsx_sheets(f) -> dict:
    """シート一覧と大きさ（`{"sheets": [{"name","max_row","max_col"}]}`）。開けなければ `{"error"}`。
    実寸の走査が時間予算を超えたシート以降は、シート名は必ず返し、寸法は `reset_dimensions` 前に控えた
    記録値（`dims_estimated: true`・記録が無ければ `max_row`/`max_col` キーを出さない＝不明）。

    `f`: 呼び出し元が TOCTOU 検証済みで開いたバイナリファイル（このモジュールが close するかは
    モジュール docstring の「ファイルオブジェクトの所有権」参照）。
    """
    err = _precheck_office(f)
    if err is not None:
        return err
    try:
        wb = _cached_load(f, _load_workbook)
        out = []
        budget = _TimeBudget()                 # ブック全体で 1 つの予算
        over = False
        for ws in wb.worksheets:
            if not over:
                try:
                    r, c = _ws_dims(ws, budget)
                    out.append({"name": ws.title, "max_row": r, "max_col": c})
                    continue
                except _TimeOver:
                    over = True
            # 予算超過: シート名は必ず返す（xlsx_range の sheet 指定に要る）。寸法は記録値＝推定
            # （記録が無ければ不明＝キーを出さない。0 行 0 列＝空、と偽らない）。
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

    セル値は文字列化し1セル `_CELL_MAX_CHARS` 文字で切る（`None` は空文字）。`clean`
    （省略可・`agentic_search._redact` 相当）は切り詰める**前**に適用する——`clean` が
    渡された場合は `redact_keys.KeyBlockRedactor(clean)` でラップし、行優先（`ws.iter_rows` の
    並び＝原本の出現順）で1つのインスタンスを使い回す。秘密鍵ブロックが複数セルにまたがっても
    （BEGIN を含むセルが200字切り詰めで途中欠けても）状態を持ち越して伏せ続ける。要求範囲・
    `max_rows`/`max_cols` 引数は 1〜既定値の範囲にクランプする（引数で無制限に広げさせない）。
    実効上限を超えたら切り詰めて `truncated: true`（`range` は実際に返した範囲）。`clean` がある
    場合、行の走査自体はシート先頭行（1行目）から選択範囲の最終行まで（行全体・実データの全列）行う——選択外の行は
    セルをクリーナーに通すだけで出力には残さない（先頭から辿らないと選択範囲より前で始まった
    鍵ブロックを見落とす）。
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
    max_rows = min(max(max_rows, 1), 200)              # 上限もクランプ（下限だけでなく）
    try:
        max_cols = int(max_cols) if max_cols is not None else 50
    except (TypeError, ValueError):
        max_cols = 50
    max_cols = min(max(max_cols, 1), 50)
    _range_budget = _TimeBudget()             # 呼び出し全体（実寸算出＋走査）で 1 つの予算
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
        return {"error": "range が不正です（Excel の上限を超えています）"}   # 巨大な行番号で空行を延々と辿らせない
    req_rows = max_row - min_row + 1
    req_cols = max_col - min_col + 1
    truncated = req_rows > max_rows or req_cols > max_cols
    eff_max_row = min(max_row, min_row + max_rows - 1)
    eff_max_col = min(max_col, min_col + max_cols - 1)
    cleaner = redact_keys.KeyBlockRedactor(clean) if clean is not None else None
    try:
        # 状態は原本の先頭行から選択範囲の終わりまで（行優先）を辿って確定し、出力だけ要求範囲
        # （`min_row` 以降）に絞る——鍵ブロックの BEGIN が選択範囲より前の行にあると、そこから
        # 読み飛ばした場合に END だけを見て状態が立たないまま鍵本文（選択範囲内）が漏れるため。
        # openpyxl の read_only は min_row を指定してもシート XML を先頭から流す＝先読み幅を狭めても
        # 時間は縮まないので、状態は 1 行目から確定する（時間は `_TimeBudget` で上限を掛ける）。
        scan_from = 1 if cleaner is not None else min_row
        # 列も、状態のためには 1 列目からシートの最終列まで辿る（列窓の外のセルに BEGIN があると
        # 見落とす）。出力は列窓（min_col..eff_max_col）だけ。
        # 状態のためには行全体（max_col=None＝<dimension> に頼らず実データの全列）を辿り、出力だけ
        # 列窓（min_col..eff_max_col）に切る（足りない列は空文字で埋める）。
        scan_min_col = 1 if cleaner is not None else min_col
        scan_max_col = None if cleaner is not None else eff_max_col
        if cleaner is not None:
            _reset_dims_keeping_record(ws)    # max_col=None でも記録寸法に補完されないよう先に捨てる（控えは残す）
        width = eff_max_col - min_col + 1
        rows_out = []
        budget = _range_budget
        for row_no, row in enumerate(
            ws.iter_rows(min_row=scan_from, max_row=eff_max_row, min_col=scan_min_col, max_col=scan_max_col,
                        values_only=True),
            start=scan_from,
        ):
            if budget.tick(every=1):          # 幅の広い行は 1 行が重い＝毎行判定
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


# ---- Word（python-docx）------------------------------------------------------------------------

_DOCX_TABLE_MAX = 20
_DOCX_TABLE_ROW_MAX = 50


def docx_paragraphs(f, start: int = 0, count: int = 200,
                    clean: Callable[[str], str] | None = None, table_start: int = 0,
                    table_row_start: int = 0) -> dict:
    """段落（`i`＝絶対インデックス・`style`・`text`）と表（`table_start` から 20 表・各表は
    `table_row_start` から 50 行）を返す。表の続きは `table_start`／`table_row_start` を進めて
    呼び直す（段落の `start`/`count` と同じ規律・上限で読めない表を作らない）。

    `clean`（省略可）は段落・表セルのテキストへ適用する（`doc_readers` 共通の「伏せ字してから
    使う」契約を xlsx/pdf と揃える）。`doc.paragraphs`/`doc.tables` は別々のフラットな
    リストで、段落と表が本文でどう混ざって並んでいたか（出現順）を保たない——秘密鍵ブロックが
    「段落→表のセル→次の段落」のように**構造をまたいで**現れると、独立に処理したのでは状態を
    引き継げず取りこぼす。そのため `clean` が渡された場合は `redact_keys.KeyBlockRedactor(clean)`
    を1個だけ作り、`doc.iter_inner_content()`（段落と表を本文の出現順で返す）を辿って**ウィンドウ
    外の要素も含めて全て**このインスタンスへ通す（出力にはウィンドウ内の分だけを残す）——
    ページング呼び出し（`start`/`table_start` などで一部だけを要求）でも鍵ブロックの状態を
    正しく判定するため、両方の窓が済むまで本文を出現順に辿る。縦・横結合セルは python-docx の
    `row.cells` が同一の `_tc`（XML 要素）を複数回返す——`id(cell._tc)` で初出だけをこの
    インスタンスへ通し、以降の出現は初出の結果を再利用する（重複供給で END を二重に検出し
    状態が誤って閉じるのを防ぐ）。
    
    走査の打ち切り: 段落の窓と表の窓の**両方**が済んだ時点で止める（片方でも残っている間は、窓外の
    段落・表・表の全行も状態のためだけに辿る＝出力窓の後ろにある要素へ鍵ブロックの状態が届く）。
    表の窓が最後の出力なら、その表は出力行の窓までしか辿らない（巨大な表の全行を毎回辿らない）。
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
        # 縦・横結合セルは python-docx の `row.cells` が同一の `_tc`（XML 要素）を複数回
        # 返す——結合先頭以外は素通りせず結合元と同じ内容を返す実装のため、同じ `_tc` を
        # 二度伏せ字ラッパーへ通すと END が重複供給されて状態が壊れる（例: 縦結合セルの END が
        # 2回目の出現で誤って状態を閉じ、直後の本当の鍵本文が漏れる）。`id(cell._tc)` で
        # 初出のみ処理し、以降の出現は初出の結果を再利用する。`row.cells` は毎回新しい `_Cell`
        # ラッパー（lxml のプロキシオブジェクト）を作るため、直前の `cell`/`_tc` への参照を
        # 手放すと GC で同じ `id()` が別の（結合されていない）要素に再利用されうる——
        # `tc_keepalive` で初出の `_tc` を関数の呼び出しが終わるまで生かし続け、`id()` の
        # 使い回しによる誤同一視（結合されていないのに同じ id と誤認する）を防ぐ。
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

        # 走査は「出力に残す最後の要素」を過ぎたら打ち切る（以降の要素は出力に出ないので鍵ブロックの
        # 状態も要らない）。表は出力する行の窓（row_window_hi）まで辿れば十分＝巨大な表の全行を
        # 毎回辿らない。ウィンドウより前の要素は状態のためだけに辿る（出力はしない）。
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
                # 出力窓の後ろにまだ出力する要素（段落・表）が残るなら、この表の全行を状態のために
                # 辿る（最終行の BEGIN が次の段落の鍵本文に効く）。残らないなら出力行の窓まで。
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
                cleaned = _apply(item.text)                # 窓外の段落も状態のために辿る（出力はしない）
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


# ---- PowerPoint（python-pptx）-------------------------------------------------------------------

_PPTX_SLIDES_MAX = 20


def pptx_slides(f, pages: str | None = "1-10",
               clean: Callable[[str], str] | None = None) -> dict:
    """スライドのテキスト・表・ノートを返す（1回20枚まで）。`clean`（省略可）はテキスト・表・
    ノートへ適用する（切り詰めは無いが xlsx/pdf と契約を揃える）。`clean` が渡された場合は
    `redact_keys.KeyBlockRedactor(clean)` を1個作り、スライド→shape 順（表・ノート含む・原本の
    出現順）で使い回す——秘密鍵ブロックが shape をまたいでも状態を持ち越して伏せ続ける。
    走査自体は選択範囲（`pages`）の `_KEY_LOOKBACK_PAGES` 枚前から最終スライドまで行う——選択外の
    スライドもクリーナーへ通すだけで出力には残さない（前で始まった鍵ブロックを見落とさないため・
    先読みは一定枚数に留めて毎回 1 枚目から辿るコストを避ける）。
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
        # 鍵ブロックの状態は選択スライドごとに直前 _KEY_LOOKBACK_PAGES 枚から辿って確定し、出力だけ
        # 要求範囲に絞る——BEGIN が選択範囲より前のスライドにあると、そこを読み飛ばした場合に
        # 状態が立たないまま選択範囲内の鍵本文が漏れるため（`clean` 無しの通常経路は従来どおり
        # 要求されたスライドだけを辿る）。
        scan_nos = _scan_pages_with_lookback(nos) if (cleaner is not None and nos) else nos
        out = []
        budget = _TimeBudget()
        prev_no = None
        for no in scan_nos:
            if budget.tick(every=1):
                return dict(_TIME_ERROR)
            if cleaner is not None and prev_no is not None and no != prev_no + 1:
                cleaner = redact_keys.KeyBlockRedactor(clean)   # 離れた区間へ前区間の状態を持ち越さない
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


# ---- PDF（pdfplumber）---------------------------------------------------------------------------

_PDF_PAGES_MAX = 10
_PDF_PAGE_TEXT_MAX_CHARS = 20000


def pdf_pages(f, pages: str | None = "1-5",
             clean: Callable[[str], str] | None = None) -> dict:
    """ページのテキストを返す（1回10ページ・1ページ20,000文字まで）。`clean`（省略可）は
    `_PDF_PAGE_TEXT_MAX_CHARS` で切る**前**に適用する。`clean` が渡された場合は
    `redact_keys.KeyBlockRedactor(clean)` を1個作り、ページ順（原本の出現順）で使い回す——
    秘密鍵ブロックがページをまたいでも状態を持ち越して伏せ続ける。走査自体は選択ページの `_KEY_LOOKBACK_PAGES` ページ前から
    選択範囲（`pages`）の最終ページまで行う——選択外のページもクリーナーへ通すだけで出力には
    残さない（1ページ目から辿らないと選択範囲より前で始まった鍵ブロックを見落とす）。
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
        # 鍵ブロックの状態は選択ページごとに直前 _KEY_LOOKBACK_PAGES ページから辿って確定し、出力だけ
        # 要求範囲に絞る——BEGIN が選択範囲より前のページにあると、そこを読み飛ばした場合に
        # 状態が立たないまま選択範囲内の鍵本文が漏れるため（`clean` 無しの通常経路は従来どおり
        # 要求されたページだけを辿る）。
        scan_nos = _scan_pages_with_lookback(nos) if (cleaner is not None and nos) else nos
        out = []
        budget = _TimeBudget()
        prev_no = None
        for no in scan_nos:
            if budget.tick(every=1):
                return dict(_TIME_ERROR)
            if cleaner is not None and prev_no is not None and no != prev_no + 1:
                cleaner = redact_keys.KeyBlockRedactor(clean)   # 離れた区間へ前区間の状態を持ち越さない
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


# ---- 先頭バイト（テキスト・コード共通の軽量プレビュー）------------------------------------------

_FILE_HEAD_DEFAULT = 65536


def file_head(f, max_bytes: int = _FILE_HEAD_DEFAULT,
             clean: Callable[[str], str] | None = None) -> dict:
    """先頭 `max_bytes` バイトを UTF-8（不正/途中で切れたバイト列は置換）でデコードして返す。

    `corpus_docs._read_head` と同じ「バイト単位で読んでからデコード」の流儀（マルチバイト文字の
    境界で誤って余分/不足を読まない）。BOM/エンコーディング判定は既存に無いため行わない
    （既存流儀どおり）。`clean`（省略可）はデコード後の全文へ適用する——`max_bytes` はここでは
    OS 読み取り自体の上限のため、境界をまたいだ秘密パターンはそもそも読めておらず `clean` の
    前後を問わず救えない（読めた範囲内の秘密を伏せるのが `clean` の役目）。この関数は使い切りの
    読み取り（キャッシュしない）——常に自分で `f` を close する。
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
        f.seek(0)
        raw = f.read(cap)
    except OSError:
        _close_quiet(f)
        return {"error": "ファイルを開けませんでした"}
    _close_quiet(f)
    text = raw.decode("utf-8", errors="replace")
    if clean is not None and text:
        # 全文を1回で扱う（複数要素にまたがる状態は不要だが、`KeyBlockRedactor` で他の
        # `doc_readers` 関数と同じ経路に揃える——単発呼び出しでも動作は `clean` 直呼びと同じ）。
        text = redact_keys.KeyBlockRedactor(clean)(text)
    return {"size": size, "text": text, "truncated": size > len(raw)}
