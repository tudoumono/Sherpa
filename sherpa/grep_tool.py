"""直接 grep ツール（read-only）。world の 1 つのフォルダ木を行単位で全文検索し、根拠つき（`doc_id`＝rel_path＋`span`）のヒットを返す。
RAG（ES/Neo4j）を経由しない素の grep 経路。`doc_id` は world root 相対パス（グラフの来歴・DL キーと一致）。
設計: docs/design/rag.md「読み取り部品の道具」
"""
from __future__ import annotations

import hashlib
import heapq
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path

from . import layer as layer_mod
from . import scope_infer
from . import text_encoding
from .doc_kinds import CODE_EXT
from .env_int import env_int
from .ingest import text_kind

_log = logging.getLogger("sherpa")

# 決定的 MD（Office/PDF 由来）とソース原文。grep は両方を対象にする。
_MD_EXT = {".md", ".markdown"}
# Evidence IR 由来の検索向け Markdown（`ingest/evidence_render.py::render`）。
_RAG_SUFFIX = ".rag.md"
# OCR 観測の本文ファイル名の接尾辞。
_OBSERVATION_SUFFIX = ".rag_observations.md"
# 拡張子だけで確実にテキストと分かる集合。この集合の所属だけで「コード」とも「対象外」とも見なさない。
# コードか資料か・読めるかの最終確定は `grep_search` 本体の `corpus_docs.classify_document` に集約する。
# 集合に無い拡張子（未登録・拡張子なし）も走査候補から除外せず、内容がテキストと判定できるかで決める。
_TEXT_EXT = _MD_EXT | CODE_EXT | {".txt"} | text_kind.CODE_EXT | text_kind.DOCUMENT_EXT

# world 識別子は英数字＋限定記号のみ（`/`・`..` 不可）。
_WORLD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # fullmatch 専用


# 1 ファイルの走査上限（バイト）。`_CappedStreamReader` が bounded chunk でストリーミング走査するため、メモリはこの値にもファイル実サイズにも比例しない。
# 走査コスト・時間の安全弁。
_GREP_FILE_CAP_BYTES = env_int("SHERPA_GREP_FILE_CAP_BYTES", 64 * 1024 * 1024, 65536, 64 * 1024 * 1024)
# MD の見出し節引用は節全体になり得るため、ヒット 1 件あたりの引用テキストを UTF-8 バイト上限でクリップする。
_GREP_HIT_TEXT_MAX_BYTES = 64 * 1024


def _clip_utf8_bytes(s: str, max_bytes: int) -> str:
    """UTF-8 のバイト数が `max_bytes` を超えないよう `s` を切り詰める（マルチバイト文字の境界を壊さない）。"""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


# ヒット本文の省略の印。離れた行をつなぐときの行の位置（1 始まり）を残し、連続した原文に見せない。
_GAP_MARK_RE = re.compile(r"⋯（(\d+)(?:〜(\d+))?行目を省略）⋯")
_TAIL_MARK_RE = re.compile(r"⋯（(\d+)行目以降を省略）⋯")
_MARK_BYTES_EST = 64  # 省略の印 1 つぶんのバイト数の見積り（実測は 50 未満）
# 節が上限を超えたときに、一致行の前後に残す行数。
_HIT_CONTEXT_LINES = 3


def _gap_mark(first: int, last: int) -> str:
    return f"⋯（{first}〜{last}行目を省略）⋯" if last > first else f"⋯（{first}行目を省略）⋯"


def _tail_mark(first: int) -> str:
    return f"⋯（{first}行目以降を省略）⋯"


def _assemble_lines(kept: list[tuple[int, str]], last_line: int | None) -> str:
    """行番号つきの行（昇順）を、離れた箇所に省略の印を挟んでつなぐ。`last_line` より後ろが残っていれば末尾にも印を付ける。"""
    out: list[str] = []
    prev: int | None = None
    for n, t in kept:
        if prev is not None and n > prev + 1:
            out.append(_gap_mark(prev + 1, n - 1))
        out.append(t)
        prev = n
    if last_line is not None and prev is not None and last_line > prev:
        out.append(_tail_mark(prev + 1))
    return "\n".join(out)


def _clip_line_around(line: str, ql: str, max_bytes: int) -> str:
    """1 行が `max_bytes` を超えるとき、`ql`（小文字）の一致位置を含む前後を残す（切れた側に `…`）。"""
    if len(line.encode("utf-8")) <= max_bytes:
        return line
    pos = line.lower().find(ql)
    if pos < 0:
        pos = 0
    end = min(len(line), pos + len(ql))

    def _cut(k: int) -> str:
        a, b = max(0, pos - k), min(len(line), end + k)
        return ("…" if a > 0 else "") + line[a:b] + ("…" if b < len(line) else "")

    lo, hi, best = 0, len(line), _cut(0)
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = _cut(mid)
        if len(cand.encode("utf-8")) <= max_bytes:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    return best if len(best.encode("utf-8")) <= max_bytes else _clip_utf8_bytes(best, max_bytes)


def clip_hit_text(text: str, q: str, hit_line: int | None, max_bytes: int, *, span_start: int | None = None) -> str:
    """ヒット本文 `text` を `max_bytes` に収める。収まるならそのまま返す。
    収まらないときは「見出し行（先頭が `#`）＋一致行の前後」を残し、離れた箇所は行の位置つきの省略の印でつなぐ（先頭から切って一致行を落とさない）。
    一致行が見つからない・行の位置が不明なときは先頭から切る。`hit_line` はヒットの行（1 始まり）、`span_start` は本文の先頭行の行番号（分かるときだけ）。
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    ql = q.lower()
    raw = text.split("\n")
    if not ql or hit_line is None:
        return _clip_utf8_bytes(text, max_bytes)
    # ① 省略の印を取り除き、残りの行に行番号を振る（印の範囲から番号を引き継ぐ）
    kinds: list = []
    for t in raw:
        g = _GAP_MARK_RE.fullmatch(t)
        tm = _TAIL_MARK_RE.fullmatch(t)
        kinds.append(("gap", int(g.group(1)), int(g.group(2) or g.group(1))) if g
                     else ("tail", int(tm.group(1))) if tm else None)
    real = [i for i, k in enumerate(kinds) if k is None]
    m = None
    if span_start is not None and not any(kinds):
        idx = hit_line - span_start
        if 0 <= idx < len(raw) and ql in raw[idx].lower():
            m = idx
    if m is None:
        m = next((i for i in real if ql in raw[i].lower()), None)
    if m is None:
        return _clip_utf8_bytes(text, max_bytes)
    num: dict[int, int] = {m: hit_line}
    n = hit_line
    for i in range(m + 1, len(raw)):
        k = kinds[i]
        if k is None:
            n += 1
            num[i] = n
        elif k[0] == "gap":
            n = k[2]
    n = hit_line
    for i in range(m - 1, -1, -1):
        k = kinds[i]
        if k is None:
            n -= 1
            num[i] = n
        elif k[0] == "gap":
            n = k[1]
    ents = [(num[i], raw[i]) for i in real]
    tail_marks = [k[1] for k in kinds if k and k[0] == "tail"]
    total_last = (tail_marks[-1] - 1) if tail_marks else ents[-1][0]
    mi = real.index(m)
    nb = [len(t.encode("utf-8")) + 1 for _n, t in ents]
    has_heading = mi > 0 and ents[0][1].lstrip().startswith("#")

    def _cost(lo: int, hi: int) -> int:
        c = sum(nb[lo:hi + 1])
        c += sum(_MARK_BYTES_EST for j in range(lo, hi) if ents[j + 1][0] > ents[j][0] + 1)
        if has_heading and lo > 0:
            c += nb[0] + (_MARK_BYTES_EST if ents[lo][0] > ents[0][0] + 1 else 0)
        if ents[hi][0] < total_last:
            c += _MARK_BYTES_EST
        return c

    lo = hi = mi
    if _cost(lo, hi) > max_bytes:
        # 一致行だけでも収まらない: 見出しは予算の 1/4 まで、一致行は一致位置の前後に切る
        head = _clip_utf8_bytes(ents[0][1], max_bytes // 4) if has_heading else None
        marks = _MARK_BYTES_EST * ((1 if has_heading and ents[mi][0] > ents[0][0] + 1 else 0)
                                   + (1 if ents[mi][0] < total_last else 0))
        room = max_bytes - marks - (len(head.encode("utf-8")) + 1 if head is not None else 0) - 1
        if room < 64:
            head, room = None, max_bytes - marks - 1
        kept = ([(ents[0][0], head)] if head is not None else []) + [(ents[mi][0], _clip_line_around(ents[mi][1], ql, max(1, room)))]
        return _clip_utf8_bytes(_assemble_lines(kept, total_last), max_bytes)
    while True:
        grew = False
        if lo > 0 and _cost(lo - 1, hi) <= max_bytes:
            lo -= 1
            grew = True
        if hi < len(ents) - 1 and _cost(lo, hi + 1) <= max_bytes:
            hi += 1
            grew = True
        if not grew:
            break
    kept = ([ents[0]] if has_heading and lo > 0 else []) + ents[lo:hi + 1]
    return _clip_utf8_bytes(_assemble_lines(kept, total_last), max_bytes)


# ストリーミング走査の読み取り単位（バイト）。cap にもファイル実サイズにも依存しない。
_SCAN_CHUNK_BYTES = 64 * 1024
# 1 行あたりの保持上限（バイト）。改行が来ないまま伸びる単一行でメモリが増え続けないための上限で、超えた行は一部を破棄して次の改行まで読み進める（行番号の同期は保つ）。
# read 側（`agentic_search`）は `_CappedStreamReader` に独立の定数（`_READ_LINE_MAX_BYTES`）を渡す。
_GREP_LINE_MAX_BYTES = 2 * 1024 * 1024

# 隣接ヒット窓の重複排除（`grep_search` 内 `seen`）に使う小さな有界窓。重複が起こり得るのは直近の窓どうしだけ。
_SEEN_RECENT_MAX = 16


class _CappedStreamReader:
    """open 済みバイナリファイル `f` を bounded chunk で読み、`cap` バイトまでの行（改行を含まない生バイト列）を順に yield する。
    メモリは `_SCAN_CHUNK_BYTES`・`_GREP_LINE_MAX_BYTES`・繰り越し中の未確定バイト列だけで頭打ち。
    属性は列挙の進行に伴って更新される:
    - `total_read`: これまでに読んだ総バイト数。
    - `truncated`: `total_read > cap` になった時点で True（ちょうど cap のファイルは False）。
    - `line_overflowed`: 1 行が `line_max_bytes` を超えて内容の一部を破棄したら True（探せていない範囲がある）。
    """

    def __init__(self, f, line_max_bytes: int | None = None):
        self._f = f
        self.total_read = 0
        self.truncated = False
        self.line_overflowed = False
        # `None` は呼び出し時点の `_GREP_LINE_MAX_BYTES` を使う（`__init__` 実行時に解決）。
        self._line_max_bytes = _GREP_LINE_MAX_BYTES if line_max_bytes is None else line_max_bytes

    def lines(self, cap: int):
        budget = cap + 1
        carry = b""
        line_max = self._line_max_bytes
        while budget > 0:
            chunk = self._f.read(min(_SCAN_CHUNK_BYTES, budget))
            if not chunk:
                break
            budget -= len(chunk)
            self.total_read += len(chunk)
            if self.total_read > cap:
                self.truncated = True
            data = carry + chunk
            carry = b""
            start = 0
            while True:
                nl = data.find(b"\n", start)
                if nl == -1:
                    break
                line_bytes = data[start:nl]
                if len(line_bytes) > line_max:
                    # 同じ chunk 内で改行が見つかった場合も、1 行の保持上限を一律に適用する。
                    line_bytes = line_bytes[:line_max]
                    self.line_overflowed = True
                yield line_bytes
                start = nl + 1
            tail = data[start:]
            if len(tail) > line_max:
                tail = tail[:line_max]
                self.line_overflowed = True
            carry = tail
        # cap で打ち切られた場合、未完の行は丸ごと破棄する（中途行を誤ヒットさせない）。EOF に達した場合は最終行として残す。
        if carry and not self.truncated:
            yield carry


def _logical_lines(reader, cap: int, encoding: str = "utf-8"):
    """ストリーム読みの生バイト行（`\n` 区切り）→ `str.splitlines()` と同一の論理行の列。
    read_around/read_doc も `splitlines()` で行を数えるため、ヒットの行番号と精読の行番号が一致する。
    `\n` 区切りの生バイト行を個別に decode → `splitlines()` して連結した結果は、全体を decode → `splitlines()` した結果と一致する。空セグメントは空行 1 本として数える。
    `encoding` は `text_encoding` の判定結果。`utf-8-sig` は先頭セグメントでだけ BOM を落とす。
    """
    rest_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
    first = True
    for raw_line in reader.lines(cap=cap):
        decoded = text_encoding.decode(raw_line, encoding if first else rest_encoding)
        first = False
        yield from (decoded.splitlines() or [""])


def valid_world(v: str) -> bool:
    """world 識別子の許容文字（worlds/scope/api が共用する単一の検証）。`fullmatch()` を使う（末尾の LF を通さない）。"""
    return bool(_WORLD_RE.fullmatch(v or ""))


def strip_derived_suffix(name: str) -> str:
    """派生ファイルの物理名（rel）→ 原本 rel。`.rag.md` → `.rag_observations.md` → 一般の `.md` の順に、最初に一致した 1 つだけを剥がす。"""
    if name.endswith(_RAG_SUFFIX):
        return name[: -len(_RAG_SUFFIX)]
    if name.endswith(_OBSERVATION_SUFFIX):
        return name[: -len(_OBSERVATION_SUFFIX)]
    if name.endswith(".md"):
        return name[:-3]
    return name


def preferred_derived_name(rag_root: Path, rel: str) -> str:
    """原本 rel（拡張子込み）→ 検索/精読対象の派生ファイル名。`{rel}.rag.md` が `rag_root` に実在すればそちら、無ければ `{rel}.md`（legacy 側の実在確認は呼び出し元）。
    返す名前が `.rag.md` で終わるかで、呼び出し元は物理ルート（`rag_root` か md 層）を判別する。
    `grep_search` と `parts/read/tools._safe_doc_path` が共有し、常に同じ 1 ファイルを見る。
    """
    if (rag_root / (rel + _RAG_SUFFIX)).is_file():
        return rel + _RAG_SUFFIX
    return rel + ".md"


class GrepDeadlineExceeded(Exception):
    """`grep_search(deadline=...)` がツリー列挙中に期限を超えたことを示す（呼び出し元が翻訳する）。"""


_DEADLINE_CHECK_ENTRIES = 256  # ツリー列挙中に `deadline` を再確認する間隔
_DEADLINE_CHECK_LINES = 256  # 1 ファイルの行走査ループ中に `deadline` を再確認する間隔


def _walk_pruned(root: Path):
    """`root` 配下の全エントリ（ファイル・フォルダ）を返す。版管理の記録のフォルダ（`scope_infer.VCS_DIR_NAMES`）には入らない。シンボリックリンクは辿らない。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not scope_infer.is_vcs_dir_name(d)]
        base = Path(dirpath)
        for name in dirnames:
            yield base / name
        for name in filenames:
            yield base / name


_RG_FALLBACK_LOGGED = False


def _log_rg_fallback(reason: str) -> None:
    """rg を使えず純 Python 走査に縮退したことをログで知らせる（プロセスごとに 1 回）。"""
    global _RG_FALLBACK_LOGGED
    if not _RG_FALLBACK_LOGGED:
        _RG_FALLBACK_LOGGED = True
        _log.warning("grep_search: %s。純 Python 走査に縮退します（結果の形は同じで、遅くなります）", reason)


def _resolve_entry(p: Path, rootr: Path, is_derived: bool, der_rag, scope_paths) -> str | None:
    """走査対象のファイル `p` → 検索結果の `doc_id`（rel）。対象外（ファイルでない・重要度の設定ファイル・派生 MD の選ばれなかった側・秘匿名・範囲外）は None。ファイルの中身は読まない。"""
    from . import scope  # 遅延 import（循環回避）
    from .ingest import importance  # 遅延 import（循環回避）
    ext = p.suffix.lower()
    # 派生ツリー（Office/PDF の決定的 MD）は拡張子で絞る。原本ツリーは絞らず、可否は `classify_document` に委ねる。
    ok_ext = (ext in _MD_EXT) if is_derived else True
    if not (p.is_file() and not p.is_symlink() and ok_ext):
        return None
    try:
        rel = p.resolve().relative_to(rootr).as_posix()
    except ValueError:
        return None
    if importance.is_importance_control_path(rel):  # 重要度設定ファイル自体は検索対象外
        return None
    if is_derived and (rel.endswith(_RAG_SUFFIX) or rel.endswith(".md")):
        # `.rag.md` と legacy `{原本rel}.md` は同じ原本の 2 つの物理ファイルになり得る。`preferred_derived_name()` が選ぶ側だけを検索対象にする（二重ヒットを作らない）。
        origin_rel = strip_derived_suffix(rel)
        if preferred_derived_name(der_rag, origin_rel) != rel:
            return None
        rel = origin_rel
        # 秘匿名は派生 MD の物理名から復元した原本名で判定する（派生ツリーの走査には `classify_document` の秘匿除外が掛からないため）。
        if text_kind.is_sensitive_doc_id(origin_rel):
            _log.warning("grep_search: 秘匿名のため派生MDを対象外にしました（ext=%s）",
                         Path(origin_rel).suffix.lower())
            return None
    if not scope.in_scope(rel, scope_paths):  # 範囲外の文書はそもそも読まない
        return None
    return rel


def _prepare_roots(world: str, roots):
    """`(roots_spec, wd, der_rag)`。`roots_spec` は `(root, is_derived)` のリスト（derived＝Office→決定的 MD の置き場・rel は元 Office に対応＝末尾 .md を剥がす）。"""
    from . import worlds  # 遅延 import（循環回避）
    if roots is not None:
        return [(Path(r), False) for r in roots], None, None
    wd = worlds.world_dir(world)
    roots_spec = [(wd, False)] if wd else []
    # アーカイブ展開先（`worlds.archives_dir`）も原本ツリーと同じ規律（`is_derived=False`）で grep 対象にする。
    der_archives = worlds.archives_dir(world)
    if der_archives.is_dir():
        roots_spec.append((der_archives, False))
    # rag と md は別ディレクトリのため、両方を is_derived ルートとして歩く。優先判定（`preferred_derived_name`）は `der_rag` を固定で参照する。
    der_rag = worlds.derived_rag_dir(world)
    if der_rag.is_dir():
        roots_spec.append((der_rag, True))
    der_md = worlds.derived_md_dir(world)
    if der_md.is_dir():
        roots_spec.append((der_md, True))
    # OCR 観測は `rag.md` に統合済みのため、観測専用ツリーは歩かない（二重ヒットを作らない）。
    return roots_spec, wd, der_rag


def _walk_importance(world: str, wd, deadline: float | None = None) -> dict:
    """ヒットの優先順位付け（`_offer`）用に world の重要度を 1 回だけ解決する（`_重要度.txt` が無ければ空 dict）。純 Python 走査用で、フォルダ木を歩く。
    `deadline` があるときは木の列挙に期限を掛け（超過は `GrepDeadlineExceeded`）、列挙済みの一覧を `resolve_for_world` へ渡して期限なしの再走査を避ける。
    """
    from .ingest import importance  # 遅延 import（循環回避）
    # `sig` を渡すと `resolve_for_world` が world 全体をもう一度全木走査しない（registry の `last_sig` を使う）。
    # 取得できなくても fail-closed にはせず `sig=None` のまま自前計算へ戻す。
    sig = None
    try:
        from . import store  # 遅延 import（循環回避）
        row = store.get_world_status_row(world)
        sig = (row or {}).get("last_sig") or None
    except Exception:
        sig = None
    if deadline is None:
        return importance.resolve_for_world(world, root=wd, sig=sig)
    try:
        files = list(scope_infer.safe_files(wd, deadline=deadline))
    except scope_infer.ScopeWalkDeadlineExceeded as e:
        raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました") from e
    return importance.resolve_for_world(world, root=wd, sig=sig if sig is not None else "", files=files)


def _candidate_importance(world: str, wd, controls: list, entries: list) -> dict:
    """候補ファイルだけの重要度（フォルダ木は歩かない）。`controls` は rg が見つけた全ての `_重要度.txt`、`entries` は候補の `(パス, rel)`。設定ファイルが無ければ空 dict。"""
    from .ingest import importance  # 遅延 import（循環回避）
    files = []
    for c in controls:
        try:
            files.append((Path(c), Path(c).relative_to(wd).as_posix()))
        except ValueError:
            continue
    if not files:
        return {}
    # `sig` は空文字を渡す（None だとフォルダ木を再走査する）。
    return importance.resolve_for_world(world, root=wd, sig="", files=files + list(entries))


def _discover_candidates(terms: list, roots_spec: list, wd, world: str, *, cacheable: bool, offset: int,
                         budget, deadline: float | None = None):
    """rg で候補のファイルを集める。`(候補 {パス: {語の添字: 行}}, 重要度の設定ファイルのパス一覧)`。
    ① `offset` 付きの呼び出しは、同じ（検索語・roots・資料フォルダの状態）で保存済みの候補一覧があればそれを切り出して使う
    ② 無ければ原本の木と派生 MD の木を rg で走査する（Office／PDF／画像と、`.rag.md` がある文書だけの `md/` のフォルダは歩かない＝`rg_prune`・非 ASCII の語は CP932 の 2 回目も）③ 重要度の設定ファイルを探す ④ 候補一覧を保存する
    rg を使えない・失敗したときは `rg_search.RgUnavailable`、期限切れは `GrepDeadlineExceeded`。
    """
    from . import rg_prune, rg_search, search_cache
    from .ingest import importance
    orig_roots = [r for r, d in roots_spec if not d and r.is_dir()]
    der_roots = [r for r, d in roots_spec if d and r.is_dir()]
    key = sig = None
    if cacheable:
        key = hashlib.sha1(json.dumps([terms, [[str(r), d] for r, d in roots_spec]],
                                      ensure_ascii=False).encode("utf-8")).hexdigest()
        try:
            sig = search_cache.tree_signature(orig_roots + der_roots, deadline=deadline)
        except scope_infer.ScopeWalkDeadlineExceeded as e:
            raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました") from e
        if offset > 0:
            saved = search_cache.pages_get(world, key, sig)
            if saved is not None:
                return ({p: {int(i): ln for i, ln in d.items()} for p, d in saved["cand"].items()},
                        saved["controls"])
    first_only = len(terms) == 1
    skip_exts: tuple = ()
    try:
        if cacheable:  # 資料フォルダの署名があるときだけ、rg に歩かせない場所を決める（`rg_prune`）
            pl = rg_prune.plan(world, orig_roots, der_roots, sig, budget)
            skip_exts = tuple(pl["skip_exts"])
            der_roots = rg_prune.derived_roots(der_roots, pl)
        cand: dict = {}
        for root_list, derived in ((orig_roots, False), (der_roots, True)):
            if not root_list:
                continue
            for path, hit in rg_search.discover(terms, root_list, derived=derived, budget=budget,
                                                first_only=first_only,
                                                exclude_exts=() if derived else skip_exts).items():
                slot = cand.setdefault(path, {})
                for i, ln in hit.items():
                    slot.setdefault(i, ln)
        controls = (rg_search.list_files_named([wd], importance.CONTROL_FILENAME, budget=budget)
                    if wd else [])
    except rg_search.RgDeadlineExceeded as e:
        raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました") from e
    if key is not None:
        search_cache.pages_put(world, key, sig, {"cand": cand, "controls": controls})
    return cand, controls


def _candidate_entries(cand: dict, roots_spec: list, der_rag, scope_paths, want: int | None = None,
                       deadline: float | None = None):
    """rg の候補 `cand`（`{パス: {語の添字: 行}}`）を（根の順・Path の順）に並べ、検索対象になるファイルだけ `(パス, rel, is_derived, 当たった語の添字)` で返す。
    `want` があれば、その語に当たったファイルだけ。ファイルの中身は読まない。`deadline` があれば `_DEADLINE_CHECK_ENTRIES` 件ごとに確認し、超過で `GrepDeadlineExceeded`。
    """
    n = 0
    for root, is_derived in roots_spec:
        if not root.is_dir():
            continue
        rootr = root.resolve()
        prefix = str(root) + os.sep
        mine = sorted((Path(pth), hit) for pth, hit in cand.items()
                      if pth.startswith(prefix) and (want is None or want in hit))
        for p, hit in mine:
            n += 1
            if deadline is not None and n % _DEADLINE_CHECK_ENTRIES == 0 and time.monotonic() > deadline:
                raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました")
            rel = _resolve_entry(p, rootr, is_derived, der_rag, scope_paths)
            if rel is not None:
                yield p, rel, is_derived, hit


def _search_term(q: str, *, roots_spec: list, der_rag, imp_map: dict, entries, classify_cache, max_hits: int,
                 scope_paths, deadline: float | None, layer, truncated_docs, offset: int, stats,
                 partial_on_deadline: bool = False):
    """1 つの検索語 `q` のヒットを選抜して返す（`grep_search` の本体）。
    `entries` が None なら `roots_spec` のフォルダ木を全量歩く（純 Python 走査）。反復可能なら候補ファイル `(パス, rel, is_derived)` だけを開く（rg で絞った走査）。
    どちらも、開いたファイルは同じ判定・同じ整形を通す。
    """
    from . import corpus_docs  # 遅延 import（循環回避）
    from .ingest import importance  # 遅延 import（循環回避）

    def _check_deadline() -> None:
        if deadline is not None and time.monotonic() > deadline:
            raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました")

    def _count_unreadable() -> None:
        if stats is not None:
            stats["unreadable_files"] = stats.get("unreadable_files", 0) + 1

    ql = q.lower()
    # top-K（優先度つき）ヒット選抜。ヒットは `_offer` を通じて有界ヒープ（容量 `heap_cap`）へ出し入れする。
    # `(rank, -seq)` の昇順（重要度が高いほど・同 rank は発見順が早いほど）で最下位を追い出す。`seq` は全ルート・全ファイルを通した発見順の単調増加カウンタ（tie-break）。
    # 純 Python 走査は `imp_map` が空なら選抜結果が発見順の先頭 `heap_cap` 件になるため満杯で打ち切る。非空なら常に全量走査する。
    # 候補だけを開く走査は候補を（重要度 rank 降順, 発見順）に並べて開き、満杯でそれ以後のファイルの rank が最下位以下になった時点で打ち切る（選抜結果は変わらない）。
    heap: list[tuple[int, int, dict]] = []
    seq = 0
    offset = max(0, offset)  # 負値は 0 扱い
    # `heap_cap`: offset 分だけ余分に選抜し、末尾で `[offset:offset+max_hits]` を切り出す。
    heap_cap = max_hits + offset

    def _offer(hit: dict) -> None:
        nonlocal seq
        seq += 1
        if max_hits <= 0:
            return
        res = imp_map.get(hit["doc_id"])
        rank = importance.rank_of(res)
        hit.update(importance.public_fields(res))  # importance/importance_reason（条件付き）
        entry = (rank, -seq, hit)
        if len(heap) < heap_cap:
            heapq.heappush(heap, entry)
        elif entry > heap[0]:
            heapq.heapreplace(heap, entry)

    def _closed(file_rank: int) -> bool:
        """以後このファイルのヒットが採用され得ないか（満杯のヒープの最下位以下の rank）。"""
        if entries is None:
            return not imp_map and len(heap) >= heap_cap
        return len(heap) >= heap_cap and bool(heap) and file_rank <= heap[0][0]

    def _scan_entry(p: Path, rel: str, is_derived: bool) -> bool:
        """1 ファイルの判定（範囲・層・秘匿・サイズ）と走査。以後を開く必要がなければ True。"""
        ext = p.suffix.lower()
        file_rank = importance.rank_of(imp_map.get(rel))
        is_code = False
        encoding_caution = None  # 対象外にしない「一部が化けている」ヒットへの注意文
        if not is_derived:
            # `corpus_docs.classify_document` の判定を実行ゲートにする（拡張子の許可リストではなくこれが最終判定）。
            # 内容判定に必要なヘッダが読めない文書はこの 1 件だけ skip する。`_TEXT_EXT` 外の拡張子だけが、ここで先頭数 KB の内容判定（1 ファイル 1 回）を要する。`corpus_docs.reachable_as_text` と同じ判定式（`_classify_verdict_reachable`）を共有する。
            # `text_quality` を渡すと、文字コードを判別できない原本は grep からも除外される。「一部が化けている」（`encoding_partial`）は対象のまま、各ヒットへ注意文（`encoding_caution`）を付ける。
            verdict = corpus_docs.classify_file(p, rel, Path(rel).suffix.lower(), cache=classify_cache)
            if not corpus_docs._classify_verdict_reachable(verdict):
                if verdict.get("kind") == "unreadable":  # 内容判定に必要なヘッダを読めなかった＝探せていないファイル
                    _count_unreadable()
                return False
            if verdict.get("encoding_partial"):
                encoding_caution = corpus_docs._ENCODING_CAUTION["partial"]
            # 軽量テキスト枠と登録アナライザ対象（`kind=="code"`）は、台帳・グラフと同じ基準（`text_kind.MAX_BYTES`＝8MiB・`corpus_docs._text_oversize` と同じ判定式）でサイズ超過を grep からも除外する。
            # 黙って消さず `truncated_docs` へ伝える（ヒットが無い文書も含む）。登録拡張子コード・Office 派生 MD・`.md`/`.txt` は対象外。
            if (verdict["kind"] == "code"
                    or verdict.get("doctype") in (text_kind.CODE_DOCTYPE_LABEL, text_kind.DOCUMENT_DOCTYPE_LABEL)):
                try:
                    oversize = p.stat().st_size > text_kind.MAX_BYTES
                except OSError:
                    oversize = False
                if oversize:
                    if truncated_docs is not None and rel not in truncated_docs:
                        truncated_docs.append(rel)
                    return False
            is_code = verdict["kind"] == "code"
        # 層判定は `classify_document` の確定結果を使う。派生 MD（Office/画像）は常に docs 層。
        if not layer_mod.in_layer_code(is_code, layer):
            return False
        # ストリーミング走査: `_CappedStreamReader` が bounded chunk で読み、改行区切りの行を順に処理する。保持するのは現在の窓（MD は直近の見出し行とその行番号、ソースは前後 2 行の小窓）だけ。
        # 持続的な OSError はこの 1 件だけ skip して他の文書の検索を続ける（ここまでのヒットは残す）。
        try:
            f = p.open("rb")
        except OSError:
            _count_unreadable()
            return False
        try:
            # ファイル読込直後（全文走査に入る前）の期限確認。
            _check_deadline()
            # 派生ツリーは Sherpa 自身が UTF-8 で書いたものなので判定を省く。原本ツリーだけ実バイト列から符号化を判定する。
            try:
                enc = "utf-8" if is_derived else text_encoding.detect_fd(f.fileno())
            except OSError:
                _count_unreadable()
                return False  # この 1 件だけ飛ばす
            reader = _CappedStreamReader(f)
            is_md = is_derived or ext in _MD_EXT
            out_ext = Path(rel).suffix.lower()  # doc_id（元ファイル）の拡張子で表示
            # `seen`（隣接ヒット窓の同一 span 重複排除）は `maxlen` 付き `deque` の小さな有界窓にする（重複は直近の窓どうしでしか起きないため、ヒット総数に比例させない）。
            seen: deque = deque(maxlen=_SEEN_RECENT_MAX)
            line_i = 0
            if is_md:
                section_start = 1
                section_has_hit = False
                section_hit_line = 0
                section_buf: list[str] = []
                section_buf_bytes = 0
                section_capped = False
                section_recent: deque = deque(maxlen=_HIT_CONTEXT_LINES + 1)  # 節内の直近の行（上限超えの節で一致行の前を復元する）
                hit_outside_buf = False  # 一致行が節の保持範囲（上限）より後ろにある
                hit_ctx: list[tuple[int, str]] = []  # 保持範囲の外の一致行の前後
                hit_after_left = 0
            else:
                recent: deque = deque(maxlen=5)  # 直近 5 行（前後 2 行窓の復元に必要な最小限）
                pending: list[int] = []  # まだ確定していないヒット行（1-based）

            def _add_hit(hit_line: int, s: int, e: int, text: str, clipped: bool = False) -> None:
                key = (str(p), s, e)
                if key in seen:
                    return
                seen.append(key)
                # 見つけ次第 top-K ヒープへ供する。`file_truncated` はファイルの走査を終えてから、ヒープに残っているこのファイル由来のエントリへ事後に付ける。
                hit = {
                    "doc_id": rel,  # world root 相対パス
                    "path": str(p),  # 内部用（物理パス）。API 露出は lens 層で除去。
                    "ext": out_ext,
                    "line": hit_line,
                    "span": [s, e],
                    "text": text,
                    "match": q,
                }
                if encoding_caution:  # 一部が化けているファイルの目印
                    hit["encoding_caution"] = encoding_caution
                if clipped:
                    hit["section_truncated"] = True
                _offer(hit)

            def _emit_md_section(end_line: int) -> None:
                nonlocal section_has_hit
                if not section_has_hit:
                    return
                if hit_outside_buf:
                    # 保持範囲より後ろの一致行: 見出し行＋一致行の前後を、省略の印でつないで返す
                    kept: list[tuple[int, str]] = []
                    if section_buf and section_buf[0].lstrip().startswith("#"):
                        kept.append((section_start, section_buf[0]))
                    kept.extend(x for x in hit_ctx if not kept or x[0] > kept[0][0])
                    text = _assemble_lines(kept, end_line)
                    clipped_text = clip_hit_text(text, q, section_hit_line, _GREP_HIT_TEXT_MAX_BYTES)
                    _add_hit(section_hit_line, section_start, end_line, clipped_text, clipped=True)
                    section_has_hit = False
                    return
                text = "\n".join(section_buf)
                if not section_capped:
                    text = text.strip()
                clipped_text = clip_hit_text(text, q, section_hit_line, _GREP_HIT_TEXT_MAX_BYTES,
                                             span_start=section_start)
                _add_hit(section_hit_line, section_start, end_line, clipped_text,
                         clipped=section_capped or clipped_text != text)
                section_has_hit = False

            hit_limit_reached = False  # ファイル内でヒット数上限に達したか（達したら flush を省く）
            try:
                for t in _logical_lines(reader, _GREP_FILE_CAP_BYTES, encoding=enc):
                    if (deadline is not None and line_i > 0 and line_i % _DEADLINE_CHECK_LINES == 0
                            and time.monotonic() > deadline):
                        raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました")
                    line_no = line_i + 1
                    if is_md:
                        is_heading = t.lstrip().startswith("#")
                        if is_heading:
                            _emit_md_section(line_no - 1)
                            section_start = line_no
                            section_buf = []
                            section_buf_bytes = 0
                            section_capped = False
                            section_recent.clear()
                            hit_outside_buf = False
                            hit_ctx = []
                            hit_after_left = 0
                        if not section_capped:
                            sep = 1 if section_buf else 0
                            section_buf.append(t)
                            section_buf_bytes += sep + len(t.encode("utf-8"))
                            if section_buf_bytes >= _GREP_HIT_TEXT_MAX_BYTES:
                                section_capped = True
                        section_recent.append((line_no, t))
                        if hit_outside_buf and hit_after_left > 0:
                            hit_ctx.append((line_no, t))
                            hit_after_left -= 1
                        if ql in t.lower() and not section_has_hit:
                            section_has_hit = True
                            section_hit_line = line_no
                            if line_no - section_start >= len(section_buf):
                                hit_outside_buf = True
                                hit_ctx = list(section_recent)
                                hit_after_left = _HIT_CONTEXT_LINES
                    else:
                        recent.append((line_no, t))
                        if ql in t.lower():
                            pending.append(line_no)
                        while pending and pending[0] <= line_no - 2:
                            h = pending.pop(0)
                            s, e = max(1, h - 2), h + 2
                            text = "\n".join(txt for (ln, txt) in recent if s <= ln <= e)
                            clipped_text = clip_hit_text(text, q, h, _GREP_HIT_TEXT_MAX_BYTES, span_start=s)
                            _add_hit(h, s, e, clipped_text, clipped=clipped_text != text)
                    line_i += 1
                    # ヒープが満杯でこのファイルのヒットが採用され得ないなら、ファイル内で break する（最終節／未確定 pending 行の flush は行わない）。
                    if _closed(file_rank):
                        hit_limit_reached = True
                        break
                if not hit_limit_reached:
                    if is_md:
                        _emit_md_section(line_i)
                    else:
                        for h in pending:
                            s, e = max(1, h - 2), min(line_i, h + 2)
                            text = "\n".join(txt for (ln, txt) in recent if s <= ln <= e)
                            clipped_text = clip_hit_text(text, q, h, _GREP_HIT_TEXT_MAX_BYTES, span_start=s)
                            _add_hit(h, s, e, clipped_text, clipped=clipped_text != text)
            except OSError:
                _count_unreadable()
        finally:
            f.close()
        # ファイル全体（cap まで）の走査を終えた時点で「探せていない範囲があるか」が確定する。
        # この 1 ファイル由来でヒープに残っているエントリ（`path` が一致するもの）にだけ `file_truncated` を付ける。打切りが無いヒットはキーを作らない。
        effective_truncated = reader.truncated or reader.line_overflowed
        if effective_truncated:
            p_str = str(p)
            for _rank, _neg_seq, h in heap:
                if h["path"] == p_str:
                    h["file_truncated"] = True
            if truncated_docs is not None and rel not in truncated_docs:
                truncated_docs.append(rel)
        # ファイル境界の打切り点。以後のファイル・root を開かない。
        return _closed(file_rank)

    try:
        if entries is None:
            stop_scan = False  # ファイル境界の打切り点
            for root, is_derived in roots_spec:
                _check_deadline()
                if not root.is_dir():
                    continue
                rootr = root.resolve()
                walked = []
                for i, p in enumerate(_walk_pruned(root)):
                    if (deadline is not None and i > 0 and i % _DEADLINE_CHECK_ENTRIES == 0
                            and time.monotonic() > deadline):
                        raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました")
                    walked.append(p)
                for p in sorted(walked):
                    # 各エントリの処理（ファイル読込・全文走査を含む）ごとに期限を確認する。
                    _check_deadline()
                    rel = _resolve_entry(p, rootr, is_derived, der_rag, scope_paths)
                    if rel is None:
                        continue
                    if _scan_entry(p, rel, is_derived):
                        stop_scan = True
                        break
                if stop_scan:
                    break
        elif heap_cap > 0:
            # 重要度の設定が無ければ rank は一様＝発見順にそのまま開く（遅延列挙・満杯で打ち切り）。あれば rank 降順（同 rank は発見順）に並べて開く。
            ordered_entries = entries
            if imp_map:
                ordered_entries = sorted(enumerate(entries), key=lambda ie: (-importance.rank_of(imp_map.get(ie[1][1])), ie[0]))
                ordered_entries = (e for _i, e in ordered_entries)
            for p, rel, is_derived in ordered_entries:
                _check_deadline()
                if bool(heap) and len(heap) >= heap_cap and importance.rank_of(imp_map.get(rel)) <= heap[0][0]:
                    break
                if _scan_entry(p, rel, is_derived):
                    break
        _check_deadline()
    except GrepDeadlineExceeded:
        if not (partial_on_deadline and stats is not None):
            raise
        stats["deadline_exceeded"] = True  # 期限までに選抜できたヒットだけを返す
    # heap は有界。最終順序だけ `(-rank, seq)` 昇順（重要度が高いほど先・同 rank は発見順）へ並べ替える。`entry`=(rank, -seq, hit)。
    heap.sort(key=lambda entry: (-entry[0], -entry[1]))
    ordered = [hit for _rank, _neg_seq, hit in heap]
    # ページング: 後ろ `max_hits` 件を返す（offset=0 なら結果は不変）。
    return ordered[offset:offset + max_hits]


def _grep_terms(terms: list, world: str, roots, max_hits: int, scope_paths, deadline, layer, truncated_docs,
                offset: int, stats, engine: str, partial_on_deadline: bool = False) -> dict:
    """`terms`（前後の空白を除いた空でない語）ごとのヒットを `{語: ヒット列}` で返す。`grep_search`／`grep_search_many` の共通の入口。
    ① 範囲の根（roots_spec）を決める ② rg が使えれば候補のファイルを 1 回の走査で集める（使えなければ純 Python 走査）
    ③ 重要度を解決する ④ 語ごとに `_search_term` でヒットを選抜する。
    """
    from . import rg_search, search_cache  # 遅延 import（循環回避）

    def _check_deadline() -> float | None:
        """期限を確認し、超過なら送出する。期限ありのときは残り秒数を返す（rg の走査の制限時間に使う）。"""
        if deadline is None:
            return None
        now = time.monotonic()
        if now > deadline:
            raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました")
        return deadline - now

    if not valid_world(world):
        return {t: [] for t in terms}
    roots_spec, wd, der_rag = _prepare_roots(world, roots)
    use_cache = roots is None
    classify_cache = search_cache.ClassifyCache(world) if use_cache else None
    partial = partial_on_deadline and stats is not None
    out: dict = {}
    try:
        entries = None
        imp_map: dict = {}
        if engine != "python" and roots_spec and all(rg_search.usable_query(t) for t in terms):
            if rg_search.find_rg() is None:
                _log_rg_fallback("rg が見つかりません")
            else:
                try:
                    budget = rg_search.Budget(_check_deadline())
                    cand, controls = _discover_candidates(terms, roots_spec, wd, world, cacheable=use_cache,
                                                          offset=max(0, offset), budget=budget, deadline=deadline)
                    if roots is None and wd and controls:
                        resolved = list(_candidate_entries(cand, roots_spec, der_rag, scope_paths, deadline=deadline))
                        imp_map = _candidate_importance(world, wd, controls, [(p, rel) for p, rel, _d, _h in resolved])
                        entries = resolved
                    else:
                        entries = True  # 候補の列挙は語ごとに遅延して行う
                except rg_search.RgUnavailable as e:
                    _log_rg_fallback(f"rg を使えません（{e}）")
                    entries = None
        if entries is None and roots is None and wd:
            imp_map = _walk_importance(world, wd, deadline)
        for i, t in enumerate(terms):
            if entries is None:
                mine = None
            else:
                want = None if len(terms) == 1 else i
                if entries is True:
                    stream = _candidate_entries(cand, roots_spec, der_rag, scope_paths, want, deadline)
                else:
                    stream = (e for e in entries if want is None or want in e[3])
                mine = ((p, rel, d) for p, rel, d, _h in stream)
            out[t] = _search_term(t, roots_spec=roots_spec, der_rag=der_rag, imp_map=imp_map, entries=mine,
                                  classify_cache=classify_cache, max_hits=max_hits, scope_paths=scope_paths,
                                  deadline=deadline, layer=layer, truncated_docs=truncated_docs,
                                  offset=offset, stats=stats, partial_on_deadline=partial)
        return out
    except GrepDeadlineExceeded:
        if not partial:
            raise
        stats["deadline_exceeded"] = True
        return {t: out.get(t, []) for t in terms}
    finally:
        if classify_cache is not None:
            classify_cache.flush()


def grep_search(query: str, world: str = "v1", roots=None, max_hits: int = 50,
                scope_paths=None, deadline: float | None = None, layer=None,
                truncated_docs: list | None = None, offset: int = 0, stats: dict | None = None, *,
                engine: str = "auto", partial_on_deadline: bool = False):
    """`query` を含む行を world のフォルダ木から探し、根拠つきヒットを返す（read-only）。
    各ヒット: `{doc_id(=rel_path), path(内部用・API 非露出), ext, line, span:[start,end], text, match}`。
    登録者の重要度（`_重要度.txt`・`ingest.importance`）があれば `importance`/`importance_reason` を追加する（無ければキー自体を作らない）。
    MD は該当見出し節を `text`/`span`、ソースは該当行＋前後数行。同一 (doc, 節) は 1 件に集約する。
    `scope_paths`（フォルダ prefix）を渡すと、その範囲の文書だけ読む。

    走査の仕組み: 候補のファイルは ripgrep（`rg_search`）が絞り、選ばれたファイルだけを開いて下の整形（節・前後行・秘匿・上限）を通す。
    rg が見つからない・失敗したときは、フォルダ木を全量歩く純 Python 走査に縮退してログで知らせる（結果の形は同じ）。`engine="python"` は純 Python 走査を強制する。
    `roots` 明示指定でない呼び出しは、候補の一覧とファイル分類の結果を派生側のディスクキャッシュ（`search_cache`）に置き、`offset` 付きの呼び出しは候補の一覧を再利用する。

    ヒットの選抜: 上限 `max_hits` 件の top-K。優先度は `(重要度 rank 降順, 発見順昇順)`。`offset` 対応のためヒープ容量は `heap_cap`（=`max_hits+offset`）。
    `_重要度.txt` が無い world（または `roots` 明示指定）は rank が一様で、ヒープが満杯になった時点で以後のヒットは採用され得ないため、ファイル内とファイル境界の 2 点で走査を早期終了する
    （早期終了した最終節／未確定の pending 行は flush しないが、選抜結果は変わらない）。純 Python 走査で `imp_map` が非空の world は常に全量走査する（rg の候補走査は rank 順に開いて同じ結果で打ち切る）。

    打切りの申告（読み込みが `_GREP_FILE_CAP_BYTES` に達し、cap より後ろは検索できていない可能性）:
    - ヒット元が打ち切られていたら、そのヒットにだけ `file_truncated: True` を付ける（通常のヒットにはキーを作らない）。
    - `truncated_docs`（省略可）にリストを渡すと、打ち切られた文書の `doc_id` を重複なく追記する（ヒット 0 件の文書も載る）。早期終了したファイル以降の打切りは報告されない。rg の走査では、開いたファイル（候補）だけが対象になる。

    `stats`（省略可）に dict を渡すと、開けない・読み取り中にエラーになって飛ばしたファイルの件数を `stats["unreadable_files"]` へ加える（名前は残さない）。
    節・窓の本文を `_GREP_HIT_TEXT_MAX_BYTES` で切ったヒットには `section_truncated: True` を付ける（通常のヒットにはキーを作らない）。

    軽量テキスト枠（`ingest.text_kind`＝未登録拡張子のテキスト）は、台帳/ES と同じ基準（`text_kind.MAX_BYTES`＝8MiB）でサイズ超過を丸ごと対象外にし、`truncated_docs` へ載せる。

    `layer`（`"docs"|"code"|"both"`・既定 `None`＝`"both"`）: 探す対象。`classify_document` の確定結果（`layer_mod.in_layer_code`）に一致しない文書は読まない。

    `deadline`（`time.monotonic()` 系の絶対期限・既定 None＝無期限）: 関数の開始直後・ルートごとの走査開始時・ツリー列挙中（ソートの前・`_DEADLINE_CHECK_ENTRIES` 件ごと）・
    各エントリの処理直前・ファイル読込直後・行走査ループ内（`_DEADLINE_CHECK_LINES` 行ごと）・各 `return` の直前で確認し、超過で `GrepDeadlineExceeded` を送出する（部分結果は返さない）。rg の走査中の超過も同じ例外にする。

    `partial_on_deadline`（既定 False）: 真で `stats` も渡されたときは、期限切れで例外を送出せず、期限までに選抜できたヒットを返して `stats["deadline_exceeded"] = True` を立てる（呼び出し元が打ち切りを申告する）。既定は従来どおり `GrepDeadlineExceeded`。

    `offset`（既定 0）: 上位 `offset + max_hits` 件を選抜し、`[offset:offset+max_hits]` を返す。
    設計: docs/design/rag.md「読み取り部品の道具」
    """
    if deadline is not None and time.monotonic() > deadline:
        if not (partial_on_deadline and stats is not None):
            raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました")
        stats["deadline_exceeded"] = True
        return []
    q = (query or "").strip()
    if not q:
        return []
    return _grep_terms([q], world, roots, max_hits, scope_paths, deadline, layer, truncated_docs, offset, stats,
                       engine, partial_on_deadline)[q]


def grep_search_many(queries, world: str = "v1", roots=None, max_hits: int = 50,
                     scope_paths=None, deadline: float | None = None, layer=None,
                     truncated_docs: list | None = None, offset: int = 0, stats: dict | None = None, *,
                     engine: str = "auto") -> dict:
    """複数の検索語を 1 回の走査にまとめて探し、語ごとのヒットを `{語: ヒット列}`（語は前後の空白を除き、空と重複は捨てる）で返す。
    語ごとの結果は `grep_search(語, ...)` と同じ（同じ引数・同じ整形・同じ上限）。rg の走査・重要度の解決・候補の一覧を語の間で共有する。
    `truncated_docs`・`stats` は全ての語の分を足し込む。
    設計: docs/design/rag.md「読み取り部品の道具」
    """
    if deadline is not None and time.monotonic() > deadline:
        raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました")
    terms: list = []
    for raw in queries or []:
        t = (raw or "").strip()
        if t and t not in terms:
            terms.append(t)
    if not terms:
        return {}
    return _grep_terms(terms, world, roots, max_hits, scope_paths, deadline, layer, truncated_docs, offset, stats,
                       engine)
