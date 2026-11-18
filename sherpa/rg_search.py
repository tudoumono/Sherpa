"""grep の候補発見に使う ripgrep の呼び出し（read-only）。ファイルの中身は読まず、候補のファイルだけを返す。
rg は「どのファイルに当たりがあるか」を速く絞るためだけに使い、ヒットの確定（行・節・前後行・秘匿・上限）は `grep_tool` が Python で行う。
rg の探索順: Codex 同梱の rg（`sandbox._codex_bundled_rg_dir` と同じ規則）→ リポジトリの `tools/codex` 配下の同梱 rg → PATH 上の rg。どれも無ければ None（呼び出し元が純 Python 走査に縮退する）。
設計: docs/design/rag.md「読み取り部品の道具」
"""
from __future__ import annotations

import base64
import functools
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata

from . import scope_infer

_log = logging.getLogger("sherpa")

# Python の `str.splitlines()` が行の区切りとみなす文字。含む検索語は 1 行に当たり得ない（rg には渡さない）。
_LINE_BREAK_CHARS = "\n\r\v\f\x1c\x1d\x1e\x85  \x00"


class RgUnavailable(Exception):
    """rg を使えない（見つからない・起動できない・途中で失敗した）。呼び出し元が純 Python 走査に縮退する。"""


class RgDeadlineExceeded(Exception):
    """rg の走査中に期限を超えた（呼び出し元が `GrepDeadlineExceeded` に翻訳する）。"""


class Budget:
    """rg の走査に使える残り時間（秒）。複数回の rg 起動で共有する。`seconds=None` は無期限。"""

    def __init__(self, seconds: float | None):
        self._seconds = seconds
        self._t0 = time.perf_counter()

    def remaining(self) -> float | None:
        if self._seconds is None:
            return None
        return max(0.0, self._seconds - (time.perf_counter() - self._t0))


def _bundled_rg() -> str | None:
    """Codex 同梱の rg。導入先の探し方は `sandbox._codex_bundled_rg_dir()`（`shutil.which("codex")` 起点）と同じで、PATH に codex が無い配置のためにリポジトリ内の `tools/codex` も見る。"""
    try:
        from .providers.codex import sandbox
        d = sandbox._codex_bundled_rg_dir()
        if d is not None:
            return str(d / "rg")
    except Exception:  # noqa: BLE001 — 同梱の探索に失敗しても PATH の rg へ進む
        pass
    from . import worlds
    root = worlds._repo_root() / "tools" / "codex"
    for cand in [root / "codex-path" / "rg", *sorted(root.glob("**/vendor/*/codex-path/rg"))]:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


@functools.lru_cache(maxsize=1)
def find_rg() -> str | None:
    """使う rg の実行ファイル。見つからなければ None（プロセス内で 1 回だけ探す）。"""
    return _bundled_rg() or shutil.which("rg")


def usable_query(q: str) -> bool:
    """rg に渡せる検索語か（行の区切りを含む語は 1 行に当たり得ないため純 Python 側に任せる）。"""
    return bool(q) and not any(c in q for c in _LINE_BREAK_CHARS)


def _base_cmd(rg: str, *, whitelist: list[str], exclude_exts=(), fixed: bool = True) -> list[str]:
    """`whitelist` は含める条件の引数（`-g`／`--iglob` の並び）。除外より先に置く（後の指定が優先されるため、除外が勝つ）。
    `fixed=False` は検索語を正規表現（`_term_pattern`）で渡す。
    """
    cmd = [rg, "--json", *(["-F"] if fixed else []), "-i", "--no-config", "--hidden", "--no-ignore", *whitelist]
    for name in sorted(scope_infer.VCS_DIR_NAMES):
        cmd += ["-g", f"!{name}/"]
    for ext in exclude_exts:  # 拡張子だけの名前（`.pdf`）は除外に含めない
        cmd += ["--iglob", f"!?*{ext}"]
    return cmd


# rg の正規表現で字面にするために `\\` を前に置く文字。
_REGEX_META = "\\.+*?()|[]{}^$#&-~"


# 非 ASCII の語を探すときに、UTF-8 の字面と並べてバイト列でも探す文字コード（rg の `-E` の名前）。
LEGACY_ENCODINGS = ("sjis", "euc-jp")
_ROWS = range(0xA1, 0xFF)


def _jis_to_sjis(lead: int, trail: int) -> bytes:
    """EUC-JP の 2 バイト（JIS X 0208 の区・点）→ 同じ字の CP932 の 2 バイト。"""
    row, cell = lead - 0xA0, trail - 0xA0
    first = ((row + 1) >> 1) + (0x80 if row <= 62 else 0xC0)
    second = (cell + 0x3F if cell <= 63 else cell + 0x40) if row % 2 else cell + 0x9E
    return bytes([first, second])


def _decode_one(seq: bytes, encoding: str) -> str | None:
    """rg（WHATWG の解読）が `seq` を読んだときの 1 文字。読めない・1 文字にならないときは None。
    CP932 は Python の `cp932` と同じ。EUC-JP の 2 バイトは CP932 と同じ表（JIS X 0208・NEC と IBM の拡張を含む）で、
    3 バイトの 0x8FA2B7 は全角のチルダに読む。
    """
    try:
        if encoding == "sjis":
            ch = seq.decode("cp932")
        elif len(seq) == 2 and seq[0] in _ROWS:
            ch = _jis_to_sjis(seq[0], seq[1]).decode("cp932")
        elif seq == b"\x8f\xa2\xb7":
            ch = "\uff5e"
        else:
            ch = seq.decode("euc_jp")
    except UnicodeDecodeError:
        return None
    return ch if len(ch) == 1 else None


@functools.lru_cache(maxsize=2)
def _fold_table(encoding: str) -> dict[str, tuple[bytes, ...]]:
    """`encoding` の 1〜3 バイトの並び → 解読した 1 文字の `casefold()`。その値ごとに、同じ値になる並びを全部まとめる（重複した符号も含む）。"""
    seqs = [bytes([b]) for b in range(0x100)]
    if encoding == "sjis":
        seqs += [bytes([lead, tr]) for lead in (*range(0x81, 0xA0), *range(0xE0, 0xFD))
                 for tr in (*range(0x40, 0x7F), *range(0x80, 0xFD))]
    else:
        seqs += [bytes([a, b]) for a in _ROWS for b in _ROWS]
        seqs += [bytes([0x8E, b]) for b in range(0xA1, 0xE0)]
        seqs += [bytes([0x8F, a, b]) for a in _ROWS for b in _ROWS]
    table: dict[str, list[bytes]] = {}
    for seq in seqs:
        ch = _decode_one(seq, encoding)
        if ch is not None:
            table.setdefault(ch.casefold(), []).append(seq)
    return {k: tuple(v) for k, v in table.items()}


def _legacy_alts(term: str, encoding: str) -> list[tuple[bytes, ...]] | None:
    """`term` を `encoding` で書いたときの文字ごとの並びの候補（大文字小文字の違う字と、重複した符号の並びを含む）。書けない文字を含む語は None。"""
    table = _fold_table(encoding)
    out = []
    for ch in term:
        seqs = table.get(ch.casefold())
        if not seqs:
            return None
        out.append(seqs)
    return out


def _legacy_pattern(term: str, encoding: str) -> str | None:
    """`term` の `encoding` のバイト列に当たる rg の正規表現。`-E <encoding> -i` で解読してから当てたときに当たる場所は必ず当たる（余計に当たる分は呼び出し元の確定で落ちる）。"""
    alts = _legacy_alts(term, encoding)
    if alts is None:
        return None
    parts = []
    for seqs in alts:
        xs = sorted({"".join(f"\\x{b:02x}" for b in seq) for seq in seqs})
        parts.append(xs[0] if len(xs) == 1 else "(?:" + "|".join(xs) + ")")
    return "(?-u:" + "".join(parts) + ")"


@functools.lru_cache(maxsize=256)
def _legacy_bytes_res(term: str) -> tuple[re.Pattern, ...]:
    """`_legacy_pattern` と同じ並びに当たる Python の正規表現（文字コードごと・行のバイト列から、どの語に当たったかを決める用）。"""
    out = []
    for enc in LEGACY_ENCODINGS:
        alts = _legacy_alts(term, enc)
        if alts is not None:
            out.append(re.compile(b"".join(b"(?:" + b"|".join(re.escape(x) for x in seqs) + b")" for seqs in alts),
                                  re.IGNORECASE))
    return tuple(out)


def _term_pattern(term: str, with_legacy: bool) -> str:
    """rg に `-e` で渡す 1 語の正規表現。字面の語（UTF-8 のまま当てる）と、`with_legacy` のときは非 ASCII の語の CP932・EUC-JP のバイト列のどれか。"""
    plain = "".join("\\" + c if c in _REGEX_META else c for c in term)
    alts = [p for enc in LEGACY_ENCODINGS
            if with_legacy and not term.isascii() and (p := _legacy_pattern(term, enc)) is not None]
    return "|".join([f"(?:{plain})", *alts]) if alts else plain


_GLOB_SPECIAL = "\\*?[]{}!"


def _esc(name: str) -> str | None:
    """フォルダ名・ファイル名 1 つを rg のグロブの字面に直す（特別な文字は `\\` で打ち消す・途中の空白や記号はそのまま書ける）。
    gitignore の書き方で表せない名前（前後の空白・制御文字・改行・タブ・リンクの名前に混じる不正なバイト）は None。
    """
    if not name or name != name.strip() or any(unicodedata.category(c) in ("Cc", "Cs") for c in name):
        return None
    return "".join("\\" + c if c in _GLOB_SPECIAL else c for c in name)


def fold_case(text: str) -> str:
    """英字を `[xX]` の文字クラスにして、グロブの一部だけを大文字小文字の両方に当てる（範囲の部分は区別したまま、拡張子だけ畳む用）。"""
    return "".join(f"[{c}{c.upper()}]" if c.isalpha() and c.isascii() else "\\" + c if c in _GLOB_SPECIAL else c
                   for c in text)


def scope_prefixes(scope_paths) -> list[str] | None:
    """正規化済みの範囲 → rg のグロブの頭（`/a/b`）の列。グロブにできない名前が 1 つでもあれば None（範囲で絞らず根から歩く）。
    `.`／`..`／空の部品を含む範囲は、どのファイルにも当たらないので読み飛ばす（全部そうなら空の列＝何も歩かない）。
    """
    out: list[str] = []
    for s in scope_paths:
        parts = s.split("/")
        if any(x in ("", ".", "..") for x in parts):
            continue
        esc = [_esc(x) for x in parts]
        if None in esc:
            return None
        out.append("/" + "/".join(esc))
    return out


def _whitelist(derived: bool, prefixes: list[str] | None) -> list[str]:
    """含める条件の引数。範囲なしの派生 MD の木は `.md`／`.markdown` だけ。範囲ありは、範囲が指す物理ファイルだけ
    （派生 MD の木は、原本 rel が範囲の下にある `.md`／`.markdown` と、範囲がファイルを指すときの物理名 `p`・`p.rag.md`・`p.md`）。
    範囲の部分は大文字小文字を区別する（`--iglob` にしない）。拡張子は `[mM][dD]` の形で畳む。
    """
    if prefixes is None:
        return ["--iglob", "*.md", "--iglob", "*.markdown"] if derived else []
    globs: list[str] = []
    for p in prefixes:
        if not derived:
            globs += [p, p + "/**"]
            continue
        globs += [p, p + ".rag.md", p + ".md"]
        globs += [f"{p}/**/*.{fold_case(e)}" for e in ("md", "markdown")]
    return [a for g in globs for a in ("-g", g)]


def _starts(roots, scoped: bool) -> list[tuple[str | None, list[str]]]:
    """rg を起こす単位 `(cwd, 起点の列)`。範囲あり（グロブは cwd からの相対で当たる）は根ごとに cwd=根・起点 `.`。範囲なしは根をそのまま渡す。"""
    roots = [str(r) for r in roots]
    if not scoped:
        return [(None, roots)] if roots else []
    return [(r, ["."]) for r in roots]


def _absolute(cwd: str | None, path: str) -> str:
    """cwd 起点の相対パス `./x` を根の絶対パスに戻す。"""
    return cwd + path[1:] if cwd is not None and path.startswith("./") else path


def _decode_path(path: dict) -> str | None:
    if "text" in path:
        return path["text"]
    if "bytes" in path:
        try:
            return os.fsdecode(base64.b64decode(path["bytes"]))
        except (ValueError, TypeError):
            return None
    return None


def _error_paths(err, cwd: str | None = None) -> list[str]:
    """rg が標準エラーに出した `rg: <パス>: <理由>` の行から、読めなかった通常ファイルのパスを取り出す（フォルダは含めない）。"""
    err.seek(0)
    out = []
    for raw in err.read().splitlines():
        line = os.fsdecode(raw)
        if not line.startswith("rg: ") or ": " not in line[4:]:
            continue
        path = _absolute(cwd, line[4:line.rindex(": ")])
        if os.path.isfile(path) and not os.path.islink(path):
            out.append(path)
    return out


def _run(cmd: list[str], budget: Budget | None, unreadable: list, cwd: str | None = None):
    """rg を走らせ、JSON の match イベントを順に返す。正常終了（summary イベントを受信）しなければ `RgUnavailable`。
    rg が読めなかったファイルのパスを `unreadable` に足す（呼び出し元が候補に加え、純 Python 走査と同じ「読めなかったファイル」の扱いにする）。
    """
    err = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, cwd=cwd)
    except OSError as e:
        err.close()
        raise RgUnavailable(f"rg を起動できません ({type(e).__name__})") from e
    killed = threading.Event()
    timer = None
    remaining = budget.remaining() if budget is not None else None
    if remaining is not None:
        timer = threading.Timer(remaining, lambda: (killed.set(), proc.kill()))
        timer.daemon = True
        timer.start()
    summary = False
    try:
        for raw in proc.stdout:
            if raw.startswith(b'{"type":"match"'):
                try:
                    yield json.loads(raw)["data"]
                except (ValueError, KeyError):
                    continue
            elif raw.rstrip().endswith(b'"type":"summary"}'):
                summary = True
    finally:
        if timer is not None:
            timer.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        unreadable.extend(_error_paths(err, cwd))
        err.close()
    if killed.is_set():
        raise RgDeadlineExceeded("rg の走査が期限を超えました")
    if not summary:
        raise RgUnavailable(f"rg が正常に終わりませんでした (rc={proc.returncode})")


def discover(terms: list[str], roots, *, derived: bool, budget: Budget | None = None,
             first_only: bool = False, exclude_exts=(), prefixes: list[str] | None = None) -> dict[str, dict[int, int]]:
    """`roots`（フォルダの列）を rg で走査し、`terms` のどれかを含む行があるファイルを `{パス: {語の添字: 最初の行番号}}` で返す。
    `derived=True` は派生 MD の木（`.md`／`.markdown` だけ・UTF-8 のみ）。それ以外は原本の木で、非 ASCII の語は UTF-8・CP932・EUC-JP のバイト列のどれかを 1 回の走査で探す（`_term_pattern`）。
    `\ufffd` を含む語があるときだけ、今までどおり CP932・EUC-JP で解読する 2・3 回目を走らせる。
    `first_only=True` は 1 語専用で、ファイルごとに最初の 1 行で止める（語の添字は 0）。複数語は行の本文から語ごとの当たりを判定し、判定できない行はすべての語に当たったものとして残す（候補は漏らさない）。
    `exclude_exts`（`.pdf` のような拡張子の列）に一致するファイルは歩かない。呼び出し元が、同じ綴りのフォルダが木に無いことを確かめてから渡す（フォルダにも当たり中身ごと消えるため）。
    `prefixes`（`scope_prefixes` の結果）があれば、根はそのまま起点にし、範囲に当たるファイルだけを含める条件（`-g`）で絞る。起点のパスを範囲の中へ明示して渡さないので、根の中の symlink は辿らない。空の列は何も歩かない。
    ASCII の語は大文字小文字を区別せず探す。非 ASCII の畳み方は rg の簡易ケースフォールディングで、Python の `lower()` とは一部異なる（確定は呼び出し元が Python で行う）。
    """
    rg = find_rg()
    if rg is None:
        raise RgUnavailable("rg が見つかりません")
    if prefixes is not None and not prefixes:
        return {}
    starts = _starts(roots, prefixes is not None)
    lowered = [t.lower() for t in terms]
    legacy = not derived and any(not t.isascii() for t in terms)
    passes: list[tuple[bool, list[str]]] = [(not legacy, [])]  # (検索語を字面で渡すか, 足す引数)
    if legacy and any("\ufffd" in t for t in terms):
        passes = [(True, []), *((True, ["-E", enc]) for enc in LEGACY_ENCODINGS)]
    out: dict[str, dict[int, int]] = {}
    for (fixed, extra), (cwd, begin) in itertools.product(passes, starts):
        cmd = _base_cmd(rg, whitelist=_whitelist(derived, prefixes), exclude_exts=exclude_exts, fixed=fixed) + extra
        if first_only:
            cmd += ["-m", "1"]
        for t in terms:
            cmd += ["-e", t if fixed else _term_pattern(t, True)]
        cmd += ["--", *begin]
        unreadable: list = []
        for data in _run(cmd, budget, unreadable, cwd):
            path = _decode_path(data.get("path") or {})
            if path is None:
                continue
            path = _absolute(cwd, path)
            line_no = data.get("line_number") or 0
            slot = out.setdefault(path, {})
            if first_only:
                slot.setdefault(0, line_no)
                continue
            text = (data.get("lines") or {}).get("text")
            text_l = text.lower() if text is not None else None
            raw = text.encode("utf-8") if text is not None and not fixed else None
            hit = [i for i, tl in enumerate(lowered) if text_l is not None and (
                tl in text_l or (raw is not None and not terms[i].isascii()
                                 and any(rx.search(raw) for rx in _legacy_bytes_res(terms[i]))))]
            for i in (hit or range(len(terms))):
                slot.setdefault(i, line_no)
        for path in unreadable:  # 読めなかったファイルも候補に残す（開いて読めないことを純 Python 走査と同じ形で申告するため）
            slot = out.setdefault(path, {})
            for i in range(len(terms)):
                slot.setdefault(i, 0)
    return out
