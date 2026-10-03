"""原本の UTF-8 / CP932 判定を検索・精読・取り込みで共有する。
BOM を優先し、strict UTF-8 が通らない場合だけ置換文字の少ない符号化を選ぶ。判定は最大 64 MiB を固定長チャンクで読む。
UTF-16・EUC-JP の自動判別は対象外（NUL は「化け」に数えるので ASCII 主体の UTF-16 は undetermined になる）。
設計: docs/design/rag.md「サイズガードと文字コード・対象外」
"""
from __future__ import annotations

import codecs
import itertools
import os
import re
import threading
from collections import OrderedDict

DETECT_CAP_BYTES = 64 * 1024 * 1024
_SCAN_CHUNK_BYTES = 64 * 1024
_UTF8_SIG_BOM = b"\xef\xbb\xbf"

# 化け比率（置換文字＋NUL＋CP932 のありえない文字）がこれを超え、かつ空でない行の過半にも化けがあれば判別不能
UNDETERMINED_RATIO = 0.02


def quality_of(ratio: float, majority_garbled: bool = False) -> str:
    """置換文字比率＋行の過半化け判定 → 読み取りの質。
    `"ok"`（化けなし）／`"partial"`（一部化け・対象外にしない・要確認）／`"undetermined"`（比率超過かつ過半の行が化け）。
    """
    if ratio <= 0.0:
        return "ok"
    if ratio > UNDETERMINED_RATIO and majority_garbled:
        return "undetermined"
    return "partial"


# 同じ原本を grep・精読・引用検証で読むため、ファイル状態をキーに判定を再利用する
# 値は (encoding, 化け比率, 空でない行の過半が化けか)
_CACHE_MAX = 65536
_cache_lock = threading.Lock()
_cache: "OrderedDict[tuple, tuple[str, float, bool]]" = OrderedDict()


def _cache_get(key: tuple) -> tuple[str, float, bool] | None:
    with _cache_lock:
        value = _cache.get(key)
        if value is not None:
            _cache.move_to_end(key)
        return value


def _cache_put(key: tuple, value: tuple[str, float, bool]) -> None:
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def _iter_bytes_chunks(raw: bytes):
    for offset in range(0, len(raw), _SCAN_CHUNK_BYTES):
        yield raw[offset:offset + _SCAN_CHUNK_BYTES]


def _iter_fd_chunks(fd: int, cap: int, *, start: int = 0):
    """ファイル位置を動かさず、`start` から先頭 cap バイトまでを固定長チャンクで読む（`start` は BOM を読み飛ばす用）。"""
    offset = start
    end = start + cap
    while offset < end:
        chunk = os.pread(fd, min(_SCAN_CHUNK_BYTES, end - offset), offset)
        if not chunk:
            return
        offset += len(chunk)
        yield chunk


# CP932 の日本語文書にほぼ現れない文字（空白以外の C0 制御文字・私用領域＝外字）は置換文字と同様に比率へ数える。
# 符号化の採否（`_choose`）には使わない
_ALLOWED_C0 = frozenset("\t\n\r\f\v\x1a")
_C0_NOT_ALLOWED = "".join(chr(c) for c in range(0x20) if chr(c) not in _ALLOWED_C0)
_PUA_LO, _PUA_HI = chr(0xE000), chr(0xF8FF)  # 私用領域（外字）


def _implausible_count(text: str) -> int:
    return sum(1 for ch in text
               if ("\x00" <= ch < "\x20" and ch not in _ALLOWED_C0) or _PUA_LO <= ch <= _PUA_HI)


# 「空でない行の過半に化けがある」判定用の正規表現（UTF-8 側は置換文字＋NUL のみ）
_REPLACEMENT_CHAR = chr(0xFFFD)
_UTF8_GARBLE_RE = re.compile("[" + re.escape(_REPLACEMENT_CHAR + "\x00") + "]")
# 行の化け判定: 置換文字・空白以外の制御文字がある行は化け。外字だけの行は、同じ行に下の `_KANA_RE` の
# 手がかりがあれば化けに数えない
_CP932_GARBLE_RE = re.compile("[" + re.escape(_C0_NOT_ALLOWED + _REPLACEMENT_CHAR) + "]")
_PUA_RE = re.compile("[" + _PUA_LO + "-" + _PUA_HI + "]")
# 救済の手がかり: 全角のかな・半角の空白・数字（EBCDIC を CP932 で読んだ結果には出ない）
_KANA_RE = re.compile("[\u3040-\u30ff 0-9]")


class _LineGarbleTracker:
    """`quality_of` の「空でない行の過半に化けがある」を、チャンク境界をまたいでも定数状態で数える。"""

    __slots__ = ("_pending_nonblank", "_pending_garbled", "_pending_soft", "_pending_rescue",
                 "total_nonblank", "garbled_nonblank", "_re", "_soft_re", "_rescue_re")

    def __init__(self, garble_re: re.Pattern, soft_re: re.Pattern | None = None,
                 rescue_re: re.Pattern | None = None):
        """`soft_re` に当たる文字は、同じ行に `rescue_re` の文字が無いときだけ化けに数える。"""
        self._pending_nonblank = False
        self._pending_garbled = False
        self._pending_soft = False
        self._pending_rescue = False
        self.total_nonblank = 0
        self.garbled_nonblank = 0
        self._re = garble_re
        self._soft_re = soft_re
        self._rescue_re = rescue_re

    def _soft(self, text: str) -> bool:
        return self._soft_re is not None and bool(self._soft_re.search(text))

    def _rescue(self, text: str) -> bool:
        return self._rescue_re is not None and bool(self._rescue_re.search(text))

    def _close_line(self, nonblank: bool, garbled: bool, soft: bool, rescue: bool) -> None:
        if nonblank:
            self.total_nonblank += 1
            if garbled or (soft and not rescue):
                self.garbled_nonblank += 1

    def feed(self, text: str) -> None:
        if not text:
            return
        start = 0
        while True:
            nl = text.find("\n", start)
            if nl < 0:
                break
            line = text[start:nl]
            self._close_line(self._pending_nonblank or bool(line.strip()),
                             self._pending_garbled or bool(self._re.search(line)),
                             self._pending_soft or self._soft(line),
                             self._pending_rescue or self._rescue(line))
            self._pending_nonblank = self._pending_garbled = self._pending_soft = self._pending_rescue = False
            start = nl + 1
        tail = text[start:]
        if tail:
            self._pending_nonblank = self._pending_nonblank or bool(tail.strip())
            self._pending_garbled = self._pending_garbled or bool(self._re.search(tail))
            self._pending_soft = self._pending_soft or self._soft(tail)
            self._pending_rescue = self._pending_rescue or self._rescue(tail)

    def finish(self) -> None:
        """EOF で改行なしに終わる末尾行を確定する（走査完了後に 1 回呼ぶ）。"""
        self._close_line(self._pending_nonblank, self._pending_garbled,
                         self._pending_soft, self._pending_rescue)
        self._pending_nonblank = self._pending_garbled = self._pending_soft = self._pending_rescue = False

    @property
    def majority_garbled(self) -> bool:
        return self.total_nonblank > 0 and self.garbled_nonblank * 2 > self.total_nonblank


def _utf8_pass(chunks, complete: bool, *, errors: str) -> tuple[int, int, int, _LineGarbleTracker]:
    """UTF-8 として 1 回だけ復号し、`(置換文字数, NUL数, 復号後の文字数, 行トラッカー)` を返す。
    `errors="strict"` は不正バイト列で `UnicodeDecodeError`、`"replace"` は不正バイトを置換文字として数える。
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors=errors)
    tracker = _LineGarbleTracker(_UTF8_GARBLE_RE)
    n_repl = n_nul = length = 0
    for chunk in itertools.chain(chunks, (None,)):
        last = chunk is None
        # 上限で切った末尾の不完全な多バイト列は不正と数えない
        t = decoder.decode(b"" if last else chunk, final=complete if last else False)
        n_repl += t.count(_REPLACEMENT_CHAR)
        n_nul += t.count("\x00")
        length += len(t)
        tracker.feed(t)
    tracker.finish()
    return n_repl, n_nul, length, tracker


def _looks_strict_utf8(chunks, complete: bool) -> tuple[bool, float, _LineGarbleTracker | None]:
    """strict UTF-8 として復号できるか。できた場合は NUL を化けに数えた比率と行トラッカーも返す。"""
    try:
        n_repl, n_nul, length, tracker = _utf8_pass(chunks, complete, errors="strict")
    except UnicodeDecodeError:
        return False, 0.0, None
    ratio = (n_repl + n_nul) / length if length else 0.0
    return True, ratio, tracker


def _count_replacement_both(chunks, complete: bool):
    """同じ範囲を UTF-8・CP932 の両方で読み、置換文字・NUL・文字数・ありえない文字数・行トラッカーを返す。
    戻り値 `(n_utf8, len_utf8, n_cp932, len_cp932, n_implausible, n_nul_utf8, tr_utf8, tr_cp932, euc_ok)`。
    """
    dec_utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
    dec_cp932 = codecs.getincrementaldecoder("cp932")(errors="replace")
    # euc_jp は NEC 特殊文字・利用者定義を拒否するため euc_jis_2004 も試す
    dec_eucs = {name: codecs.getincrementaldecoder(name)(errors="strict") for name in ("euc_jp", "euc_jis_2004")}
    n_utf8 = n_cp932 = len_utf8 = len_cp932 = n_implausible = n_nul_utf8 = 0
    tr_utf8 = _LineGarbleTracker(_UTF8_GARBLE_RE)
    tr_cp932 = _LineGarbleTracker(_CP932_GARBLE_RE, _PUA_RE, _KANA_RE)
    for chunk in itertools.chain(chunks, (None,)):
        last = chunk is None
        t8 = dec_utf8.decode(b"" if last else chunk, final=complete if last else False)
        t932 = dec_cp932.decode(b"" if last else chunk, final=complete if last else False)
        for name in list(dec_eucs):
            try:
                dec_eucs[name].decode(b"" if last else chunk, final=complete if last else False)
            except UnicodeDecodeError:
                del dec_eucs[name]
        n_utf8 += t8.count(_REPLACEMENT_CHAR)
        n_nul_utf8 += t8.count("\x00")
        len_utf8 += len(t8)
        n_cp932 += t932.count(_REPLACEMENT_CHAR)
        len_cp932 += len(t932)
        n_implausible += _implausible_count(t932)
        tr_utf8.feed(t8)
        tr_cp932.feed(t932)
    tr_utf8.finish()
    tr_cp932.finish()
    return n_utf8, len_utf8, n_cp932, len_cp932, n_implausible, n_nul_utf8, tr_utf8, tr_cp932, bool(dec_eucs)


def _choose(n_utf8: int, len_utf8: int, n_cp932: int, len_cp932: int, n_implausible: int,
           n_nul_utf8: int, tr_utf8: _LineGarbleTracker, tr_cp932: _LineGarbleTracker,
           euc_ok: bool = False) -> tuple[str, float, bool]:
    """符号化と、その符号化での化け比率・行の過半化け判定を決める。
    採否は真の置換文字数の少なさだけで決める。選ばれた側の比率には NUL・（cp932 なら）ありえない文字も加算する。
    """
    if n_cp932 < n_utf8:
        ratio = (n_cp932 + n_implausible) / len_cp932 if len_cp932 else 0.0
        # CP932 で化けるのに strict な EUC-JP として読める＝EUC-JP の原本。判別不能側に倒す
        if euc_ok and ratio > 0:
            return "cp932", 1.0, True
        return "cp932", ratio, tr_cp932.majority_garbled
    ratio = (n_utf8 + n_nul_utf8) / len_utf8 if len_utf8 else 0.0
    return "utf-8", ratio, tr_utf8.majority_garbled


def detect_bytes_quality(raw: bytes, *, complete: bool) -> tuple[str, float, bool]:
    """`detect_bytes` と同じ符号化選定に加え、選んだ符号化での化け比率と行の過半化け判定を返す。
    BOM（`utf-8-sig`）は符号化として優先するが、比率は BOM の後ろを UTF-8 の置換読みで測る。
    """
    if raw.startswith(_UTF8_SIG_BOM):
        n_repl, n_nul, length, tracker = _utf8_pass(_iter_bytes_chunks(raw[len(_UTF8_SIG_BOM):]),
                                                     complete, errors="replace")
        ratio = (n_repl + n_nul) / length if length else 0.0
        return "utf-8-sig", ratio, tracker.majority_garbled
    ok, ratio, tracker = _looks_strict_utf8(_iter_bytes_chunks(raw), complete)
    if ok:
        return "utf-8", ratio, tracker.majority_garbled
    return _choose(*_count_replacement_both(_iter_bytes_chunks(raw), complete))


def detect_bytes(raw: bytes, *, complete: bool) -> str:
    """ファイル先頭のバイト列から utf-8 / utf-8-sig / cp932 を判定する。
    complete はファイル末尾まで含む場合だけ True。BOM、strict UTF-8 の順に判定し、それ以外は置換文字が真に少ない場合だけ CP932。同数なら UTF-8。
    """
    return detect_bytes_quality(raw, complete=complete)[0]


def detect_fd_quality(fd: int) -> tuple[str, float, bool]:
    """`detect_fd` と同じ符号化選定に加え、化け比率と行の過半化け判定を返す。ファイル状態をキーにキャッシュする。"""
    st = os.fstat(fd)
    key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    complete = st.st_size <= DETECT_CAP_BYTES
    head = os.pread(fd, 3, 0)
    if head.startswith(_UTF8_SIG_BOM):
        # BOM の 3 バイトを読み飛ばした本文だけを判定範囲にする
        n_repl, n_nul, length, tracker = _utf8_pass(
            _iter_fd_chunks(fd, DETECT_CAP_BYTES, start=len(_UTF8_SIG_BOM)),
            complete, errors="replace")
        ratio = (n_repl + n_nul) / length if length else 0.0
        result = ("utf-8-sig", ratio, tracker.majority_garbled)
    else:
        ok, ratio, tracker = _looks_strict_utf8(_iter_fd_chunks(fd, DETECT_CAP_BYTES), complete)
        if ok:
            result = ("utf-8", ratio, tracker.majority_garbled)
        else:
            result = _choose(*_count_replacement_both(_iter_fd_chunks(fd, DETECT_CAP_BYTES), complete))
    _cache_put(key, result)
    return result


def detect_fd(fd: int) -> str:
    """開いた fd の先頭から最大 DETECT_CAP_BYTES を判定する。pread でファイル位置を保ち、I/O 失敗は呼出元へ伝える。"""
    return detect_fd_quality(fd)[0]


def decode(raw: bytes, enc: str) -> str:
    """不正バイトを置換して読む。utf-8-sig は先頭 BOM だけを取り除く。"""
    return raw.decode(enc, errors="replace")
