"""原本の UTF-8 / CP932 判定を検索・精読・取り込みで共有する。

BOM を優先し、strict UTF-8 が通らない場合だけ置換文字の少ない符号化を選ぶ。
判定は最大 64 MiB を固定長チャンクで読み、ファイルサイズに比例したメモリを使わない。
UTF-16 や EUC-JP 等の自動判別は対象外——ただし NUL（U+0000）は「化け」として数えるため、
strict UTF-8 を素通りする ASCII 主体の UTF-16 も undetermined になる（SRH-05）。
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

# 選んだ符号化でも「化け」比率（置換文字＋NUL＋CP932のありえない文字。下記参照）がこれを超え、
# かつ空でない行の過半にも化けがあれば「文字コードを判別できない」（quality_of 参照）とみなす
# （SRH-05）。`ingest.text_kind.sniff_content` の未知拡張子バイナリ判定（閾値0.02・第2段専用の
# 別の判断軸）とは独立の定数——本モジュールは登録済み拡張子を含む全テキスト読み込み経路で
# 「どこまで正しく読めたか」を判定するため、値が同じでも用途が違う。
UNDETERMINED_RATIO = 0.02


def quality_of(ratio: float, majority_garbled: bool = False) -> str:
    """置換文字比率＋行の過半化け判定 → 読み取りの質。`"ok"`（化けなし）／`"partial"`（一部が
    化けている・対象外にしない・要確認）／`"undetermined"`（`UNDETERMINED_RATIO` 超**かつ**
    空でない行の過半に化けがある＝文字コードを判別できない）。

    `majority_garbled`（SRH-05）: 比率だけで判定すると、化けた段落が1つでも長い文書全体の
    比率を押し下げて undetermined になり得る一方、短いファイルの1バイト化けは比率を押し上げて
    しまう——「一部だけ化けている原本は対象外にせず要確認」という契約を守るため、比率超過に加えて
    空でない行の過半に化けが広がっていることも要求する（`detect_bytes_quality`/`detect_fd_quality`
    が `_LineGarbleTracker` で算出する）。省略時（`False`）は比率のみで `"partial"` 止まりになる
    （`ratio<=0.0` の `"ok"` 判定には影響しない）。
    """
    if ratio <= 0.0:
        return "ok"
    if ratio > UNDETERMINED_RATIO and majority_garbled:
        return "undetermined"
    return "partial"


# 同じ原本を grep・精読・引用検証で読むため、ファイル状態をキーに判定を再利用する。
# 値は `(encoding, replacement_ratio, majority_garbled)`——`ratio`＝選んだ符号化での化け比率
# （0.0＝完全に読めた）、`majority_garbled`＝空でない行の過半に化けがあるか（`quality_of` 参照）。
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
    """ファイル位置を動かさず、`start` から先頭 cap バイトまでを固定長チャンクで読む。

    `start`（既定0）: UTF-8 BOM の後ろだけを判定範囲にしたいとき（`detect_fd_quality` の
    BOM 分岐・SRH-05）に BOM の3バイトを読み飛ばす——bytes 版の `raw[len(_UTF8_SIG_BOM):]`
    スライスに相当する fd 版の入口。
    """
    offset = start
    end = start + cap
    while offset < end:
        chunk = os.pread(fd, min(_SCAN_CHUNK_BYTES, end - offset), offset)
        if not chunk:
            return
        offset += len(chunk)
        yield chunk


# CP932 はほぼ全バイトを文字として受け入れるため、置換文字の少なさだけでは EBCDIC 等の
# 別符号化も CP932 と判定してしまう。CP932 の日本語文書にほぼ現れない文字（空白以外の C0 制御文字・
# 私用領域＝外字）は「ありえない文字」として、置換文字と同じく比率の分子に数える（SRH-05）
# ——以前はこの比率だけで CP932 の採否そのものを棄却していたが、実際に読める CP932 原本
# （ときどき外字が混じる程度）まで undetermined に誤って落としていたため、採否（`_choose`）は
# 真の置換文字数の少なさだけで決め、ありえない文字は「読めたが要確認」側の比率へ計上するに
# とどめる。NUL（U+0000）は C0 制御文字の一種としてここに含まれる。
_ALLOWED_C0 = frozenset("\t\n\r\f\v\x1a")
_C0_NOT_ALLOWED = "".join(chr(c) for c in range(0x20) if chr(c) not in _ALLOWED_C0)
_PUA_LO, _PUA_HI = chr(0xE000), chr(0xF8FF)   # 私用領域（外字）——CP932 のありえない文字判定に使う


def _implausible_count(text: str) -> int:
    return sum(1 for ch in text
               if ("\x00" <= ch < "\x20" and ch not in _ALLOWED_C0) or _PUA_LO <= ch <= _PUA_HI)


# `quality_of` の「空でない行の過半に化けがある」判定用（`_implausible_count` と同じ文字集合を
# 正規表現化——判定基準を2重に持たない・SRH-05）。UTF-8 側は置換文字＋NUL のみ（CP932 特有の
# 私用領域/制御文字判定は当たらない＝別の符号化として読んだ結果には別の基準を使う）。
_REPLACEMENT_CHAR = chr(0xFFFD)
_UTF8_GARBLE_RE = re.compile("[" + re.escape(_REPLACEMENT_CHAR + "\x00") + "]")
# 行の過半判定（対象外の判定）: 置換文字・空白以外の制御文字がある行は化けた行。外字（私用領域）だけの
# 行は、同じ行に救済の手がかり（下の `_KANA_RE`）があれば化けに数えない（外字の多い正しい CP932 を
# 対象外にしない）。手がかりの無い行の外字は化けに数える（EBCDIC の英数字を CP932 で読むと、数字が外字、
# 英大文字が半角カナ・漢字になる）。
_CP932_GARBLE_RE = re.compile("[" + re.escape(_C0_NOT_ALLOWED + _REPLACEMENT_CHAR) + "]")
_PUA_RE = re.compile("[" + _PUA_LO + "-" + _PUA_HI + "]")
# 救済の手がかり: 全角のかな・半角の空白・数字。EBCDIC の表示文字は 0x40 以上で、CP932 の 2 バイト目も
# 0x40 以上なので、EBCDIC を CP932 で読んだ結果には 0x20〜0x3F（空白・数字）が出ない＝外字変換表や
# 漢字と半角カナだけの名簿は救い、EBCDIC は救わない。
_KANA_RE = re.compile("[\u3040-\u30ff 0-9]")


class _LineGarbleTracker:
    """`quality_of` の「空でない行の過半に化けがある」を、チャンク境界をまたいでも定数状態で
    数える（SRH-05）。未確定の末尾行（次チャンクへ続く／改行の無い巨大な1行）は本文を保持せず、
    真偽値2つ（非空か・化けを含むか）だけを持ち越す——メモリは判定範囲（最大64MiB）に対して
    チャンク長＋定数状態で有界（1行が判定範囲全体という極端なケースでも本文を蓄積しない）。
    """

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
        """EOF で改行なしに終わる末尾行を1行として確定する（呼び出し元は走査完了後に1回呼ぶ）。"""
        self._close_line(self._pending_nonblank, self._pending_garbled,
                         self._pending_soft, self._pending_rescue)
        self._pending_nonblank = self._pending_garbled = self._pending_soft = self._pending_rescue = False

    @property
    def majority_garbled(self) -> bool:
        return self.total_nonblank > 0 and self.garbled_nonblank * 2 > self.total_nonblank


def _utf8_pass(chunks, complete: bool, *, errors: str) -> tuple[int, int, int, _LineGarbleTracker]:
    """UTF-8 として1回だけ復号し、`(置換文字数, NUL数, 復号後の文字数, 行トラッカー)` を返す。

    `errors="strict"`: 不正バイト列があれば `UnicodeDecodeError` を送出する（呼び出し元が
    「strict UTF-8 として読めるか」の判定に使う——成功時は置換文字数は常に0）。
    `errors="replace"`: 常に成功し、不正バイト列は置換文字として数える（BOM 本文の判定用）。
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors=errors)
    tracker = _LineGarbleTracker(_UTF8_GARBLE_RE)
    n_repl = n_nul = length = 0
    for chunk in itertools.chain(chunks, (None,)):
        last = chunk is None
        # 上限で切った末尾の不完全な多バイト列は、不正な UTF-8 と数えない（`complete=False`）。
        t = decoder.decode(b"" if last else chunk, final=complete if last else False)
        n_repl += t.count(_REPLACEMENT_CHAR)
        n_nul += t.count("\x00")
        length += len(t)
        tracker.feed(t)
    tracker.finish()
    return n_repl, n_nul, length, tracker


def _looks_strict_utf8(chunks, complete: bool) -> tuple[bool, float, _LineGarbleTracker | None]:
    """strict UTF-8 として復号できるか。復号できた場合は NUL を「化け」に数えた比率と行トラッカーも
    返す（SRH-05: strict UTF-8 を素通りする ASCII 主体の UTF-16 を `"ok"` にしないため——
    strict モードでは `n_repl` は常に0なので、比率は事実上 NUL の比率）。`ok=False` の
    ratio/tracker は無意味（呼び出し元は使わない）。
    """
    try:
        n_repl, n_nul, length, tracker = _utf8_pass(chunks, complete, errors="strict")
    except UnicodeDecodeError:
        return False, 0.0, None
    ratio = (n_repl + n_nul) / length if length else 0.0
    return True, ratio, tracker


def _count_replacement_both(chunks, complete: bool):
    """同じ範囲を両符号化で読み、置換文字・NUL・復号後の文字数と、CP932 で読んだときの
    ありえない文字数（NUL含む）、および行単位の化け判定用トラッカーを両符号化ぶん返す。

    戻り値 `(n_utf8, len_utf8, n_cp932, len_cp932, n_implausible, n_nul_utf8, tr_utf8, tr_cp932)`。
    `n_utf8`/`n_cp932` は置換文字数のみ（`_choose` の採否判定＝「置換文字が少ない方を選ぶ」に使う・
    SRH-05でも据え置き）。NUL・ありえない文字は採否には使わず、選ばれた側の比率にだけ加算する
    （`_choose` 参照）。メモリはチャンク長＋`_LineGarbleTracker` の定数状態で有界。
    """
    dec_utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
    dec_cp932 = codecs.getincrementaldecoder("cp932")(errors="replace")
    # Python の euc_jp は NEC 特殊文字（13 区）・利用者定義（85〜94 区）を拒否するため、euc_jis_2004 も試す。
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
        n_implausible += _implausible_count(t932)   # NUL を含む（cp932側・上の定数コメント参照）
        tr_utf8.feed(t8)
        tr_cp932.feed(t932)
    tr_utf8.finish()
    tr_cp932.finish()
    return n_utf8, len_utf8, n_cp932, len_cp932, n_implausible, n_nul_utf8, tr_utf8, tr_cp932, bool(dec_eucs)


def _choose(n_utf8: int, len_utf8: int, n_cp932: int, len_cp932: int, n_implausible: int,
           n_nul_utf8: int, tr_utf8: _LineGarbleTracker, tr_cp932: _LineGarbleTracker,
           euc_ok: bool = False) -> tuple[str, float, bool]:
    """符号化と、その符号化での化け比率・行の過半化け判定を決める。

    採否（cp932 か utf-8 か）は真の置換文字数（`n_cp932`/`n_utf8`）の少なさだけで判定する
    （SRH-05: 「ありえない文字の比率で棄却する」規則は撤去——実際に読める CP932 原本を
    誤って undetermined に落とさない）。選ばれた側の比率には NUL・（cp932 なら）ありえない
    文字も「化け」として加算する。
    """
    if n_cp932 < n_utf8:
        ratio = (n_cp932 + n_implausible) / len_cp932 if len_cp932 else 0.0
        # CP932 で読むと化ける（比率 > 0）のに、全体が strict な EUC-JP として読める＝EUC-JP の原本。
        # 行ごとの日本語が短いと CP932 の読みが置換文字を出さず行の過半判定に掛からないため、ここで
        # 対象外（判別できない）に倒す。ひらがな・全角記号を含む CP932 は先頭バイト 0x81〜0x9F で
        # strict な EUC-JP を通らないので、この条件に当たらない。
        if euc_ok and ratio > 0:
            return "cp932", 1.0, True
        return "cp932", ratio, tr_cp932.majority_garbled
    ratio = (n_utf8 + n_nul_utf8) / len_utf8 if len_utf8 else 0.0
    return "utf-8", ratio, tr_utf8.majority_garbled


def detect_bytes_quality(raw: bytes, *, complete: bool) -> tuple[str, float, bool]:
    """`detect_bytes` と同じ符号化選定に加え、選んだ符号化での化け比率（置換文字＋NUL＋（cp932なら）
    ありえない文字・0.0＝完全に読めた）と、空でない行の過半に化けがあるか（`quality_of` 参照）を
    返す。

    BOM（`utf-8-sig`）は符号化としては優先するが、以前と異なり比率は測る——BOM の後ろを UTF-8 の
    置換読みで数える（SRH-05: BOM 付きで本体が CP932 等の原本を黙って `"ok"` にしないため）。
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

    complete はファイル末尾まで含む場合だけ True。BOM、strict UTF-8 の順に判定し、
    それ以外は置換文字が真に少ない場合だけ CP932 を選ぶ。同数なら UTF-8。
    """
    return detect_bytes_quality(raw, complete=complete)[0]


def detect_fd_quality(fd: int) -> tuple[str, float, bool]:
    """`detect_fd` と同じ符号化選定に加え、選んだ符号化での化け比率と行の過半化け判定を返す
    （SRH-05・`detect_bytes_quality` 参照）。

    符号化と同じキー（ファイル state）でキャッシュする——`detect_fd`/`detect_fd_quality` を
    同じ fd に対して呼んでも2回目はキャッシュヒット（再スキャンしない）。
    """
    st = os.fstat(fd)
    key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    complete = st.st_size <= DETECT_CAP_BYTES
    head = os.pread(fd, 3, 0)
    if head.startswith(_UTF8_SIG_BOM):
        # BOM の3バイトを読み飛ばした本文だけを判定範囲にする（bytes 版の
        # `raw[len(_UTF8_SIG_BOM):]` に相当・SRH-05）。
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
    """開いた fd の先頭から最大 DETECT_CAP_BYTES を判定する。I/O 失敗は呼出元へ伝える。

    pread でファイル位置を維持し、呼出元が検証・保持している fd を開き直さず使う。
    """
    return detect_fd_quality(fd)[0]


def decode(raw: bytes, enc: str) -> str:
    """不正バイトを置換して読む。utf-8-sig は先頭 BOM だけを取り除く。"""
    return raw.decode(enc, errors="replace")
