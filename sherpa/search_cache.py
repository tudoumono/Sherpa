"""検索の走査費用を消す派生側ディスクキャッシュ（SQLite）。
置き場は `worlds.derived_dir(world)/search_cache/cache.sqlite3`＝派生側だけ。登録ディレクトリ（原本）には書かない。
キーはファイルの状態 `(dev, inode, size, mtime_ns)`。更新・置換は次の呼び出しで自動的に無効化される（鏡モデルの即反映と矛盾しない）。
載せるもの: ① ファイル分類の結果（`corpus_docs.classify_file`）② `read_doc` の総行数 ③ `grep_search` の候補一覧（ページ送り用）。
守ること: キャッシュの失敗は呼び出し元を失敗させない（読めない・書けないときは無かったことにする）。更新から `_RACY_NS` 以内のファイルは確定させない。
設計: docs/design/rag.md「読み取り部品の道具」・docs/design/data.md「ファイル」
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from . import scope_infer, worlds

_log = logging.getLogger("sherpa")

# 保存形式の版。上げると既存のキャッシュは作り直される。
_SCHEMA = 1
# 更新から 2 秒以内のファイルはキャッシュに確定させない。
_RACY_NS = 2_000_000_000
_READ_META_MAX_ROWS = 50_000
_PAGES_MAX_ROWS = 16
_PAGES_TTL = 300.0  # 候補一覧の有効期間（秒）


def stat_key(st: os.stat_result) -> tuple[int, int, int, int]:
    """キャッシュのキー `(dev, inode, size, mtime_ns)`。"""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def settled(st: os.stat_result) -> bool:
    """更新から十分に時間が経ち、キャッシュに確定させてよいファイルか。"""
    return time.time_ns() - st.st_mtime_ns > _RACY_NS


def _stamp() -> str:
    """分類契約（アナライザ構成・分類契約版・拡張子表・符号化判定の閾値）が変わったらキャッシュを作り直すための印。"""
    from . import corpus_docs, text_encoding
    from .ingest import text_kind
    from .ingest.analyzers import registry
    parts = (registry.config_signature(),
             sorted(text_kind.CODE_EXT), sorted(text_kind.DOCUMENT_EXT), sorted(text_kind.NOISE_EXT),
             sorted(text_kind.SENSITIVE_EXT), text_kind.MAX_BYTES, sorted(text_kind._SENSITIVE_NAME_EXACT),
             text_kind._SENSITIVE_NAME_PREFIXES, text_kind.NOISE_NAME_PREFIXES,
             sorted(corpus_docs._NONCODE_DOCTYPE.items()), sorted(corpus_docs._OFFICE_DOCTYPE.items()),
             text_encoding.UNDETERMINED_RATIO, text_encoding.DETECT_CAP_BYTES)
    return f"{_SCHEMA}:{hashlib.sha1(repr(parts).encode('utf-8')).hexdigest()}"


def _db_path(world: str) -> Path | None:
    """キャッシュ DB の場所。派生ルートが明示（`SHERPA_DERIVED_DIR`）か実在するときだけ作る（既定の `data/derived` を勝手に作らない）。"""
    if not world:
        return None
    base = worlds.derived_dir(world)
    try:
        if not base.is_dir():
            if not os.environ.get("SHERPA_DERIVED_DIR"):
                return None
            base.mkdir(parents=True, exist_ok=True)
        d = base / "search_cache"
        d.mkdir(exist_ok=True)
        return d / "cache.sqlite3"
    except OSError:
        return None


def _connect(world: str) -> sqlite3.Connection | None:
    """DB を開く（無ければ作る）。開けないときは None。"""
    path = _db_path(world)
    if path is None:
        return None
    try:
        con = sqlite3.connect(str(path), timeout=3.0, isolation_level=None)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        con.executescript(
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);"
            "CREATE TABLE IF NOT EXISTS classify(rel TEXT PRIMARY KEY, dev INTEGER, ino INTEGER, size INTEGER,"
            " mtime_ns INTEGER, payload TEXT);"
            "CREATE TABLE IF NOT EXISTS readmeta(dev INTEGER, ino INTEGER, size INTEGER, mtime_ns INTEGER,"
            " params TEXT, payload TEXT, PRIMARY KEY(dev, ino, size, mtime_ns, params));"
            "CREATE TABLE IF NOT EXISTS pages(key TEXT PRIMARY KEY, created REAL, sig TEXT, payload TEXT);")
        stamp = _stamp()
        row = con.execute("SELECT v FROM meta WHERE k='stamp'").fetchone()
        if row is None or row[0] != stamp:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM classify")
            con.execute("DELETE FROM readmeta")
            con.execute("DELETE FROM pages")
            con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('stamp', ?)", (stamp,))
            con.execute("COMMIT")
        return con
    except (sqlite3.Error, OSError) as e:
        _log.debug("search_cache: 開けないためキャッシュなしで続けます (%s)", type(e).__name__)
        return None


class ClassifyCache:
    """ファイル分類の結果（rel → 分類 dict）のディスクキャッシュ。1 回の列挙で使い、最後に `flush()` で書く。
    `preload=True` は全件を 1 回で読む（資料フォルダ全体の列挙向け）。False は 1 件ずつ引く（grep の候補ファイル向け）。
    """

    def __init__(self, world: str, *, preload: bool = False):
        self._world = world
        self._preload = preload
        self._con: sqlite3.Connection | None = None
        self._opened = False
        self._rows: dict[str, tuple] = {}
        self._new: dict[str, tuple] = {}
        self._seen: set[str] = set()

    def _db(self) -> sqlite3.Connection | None:
        if not self._opened:
            self._opened = True
            self._con = _connect(self._world)
            if self._con is not None and self._preload:
                try:
                    for rel, dev, ino, size, mtime_ns, payload in self._con.execute(
                            "SELECT rel, dev, ino, size, mtime_ns, payload FROM classify"):
                        self._rows[rel] = ((dev, ino, size, mtime_ns), payload)
                except sqlite3.Error:
                    self._rows = {}
        return self._con

    def get(self, rel: str, st: os.stat_result) -> dict | None:
        """`rel` の保存済み分類。ファイルの状態が一致しなければ None。"""
        self._seen.add(rel)
        con = self._db()
        if con is None:
            return None
        key = stat_key(st)
        if self._preload:
            row = self._rows.get(rel)
        else:
            try:
                r = con.execute("SELECT dev, ino, size, mtime_ns, payload FROM classify WHERE rel=?",
                                (rel,)).fetchone()
            except sqlite3.Error:
                return None
            row = ((r[0], r[1], r[2], r[3]), r[4]) if r else None
        if row is None or row[0] != key:
            return None
        try:
            return json.loads(row[1])
        except ValueError:
            return None

    def put(self, rel: str, st: os.stat_result, payload: dict) -> None:
        """分類を保存候補に積む。更新直後のファイルは積まない。"""
        self._seen.add(rel)
        if not settled(st) or self._db() is None:
            return
        self._new[rel] = (stat_key(st), json.dumps(payload, ensure_ascii=False))

    def flush(self, *, prune: bool = False) -> None:
        """積んだ分を書き出す。`prune=True`（全件を見終えた列挙）は、今回見なかった rel の行を消す。"""
        con = self._con
        if con is None:
            return
        try:
            stale = [rel for rel in self._rows if rel not in self._seen] if (prune and self._preload) else []
            if self._new or stale:
                con.execute("BEGIN IMMEDIATE")
                con.executemany(
                    "INSERT OR REPLACE INTO classify(rel, dev, ino, size, mtime_ns, payload) VALUES(?,?,?,?,?,?)",
                    [(rel, *key, payload) for rel, (key, payload) in self._new.items()])
                con.executemany("DELETE FROM classify WHERE rel=?", [(rel,) for rel in stale])
                con.execute("COMMIT")
        except sqlite3.Error as e:
            _log.debug("search_cache: 分類の書き込みに失敗しました (%s)", type(e).__name__)
        finally:
            self.close()

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
            self._con = None


def read_meta_get(world: str, st: os.stat_result, params: str) -> dict | None:
    """`read_doc` が保存した総行数など。ファイルの状態と読み取りの条件（`params`）が一致しなければ None。"""
    con = _connect(world)
    if con is None:
        return None
    try:
        r = con.execute("SELECT payload FROM readmeta WHERE dev=? AND ino=? AND size=? AND mtime_ns=? AND params=?",
                        (*stat_key(st), params)).fetchone()
        return json.loads(r[0]) if r else None
    except (sqlite3.Error, ValueError):
        return None
    finally:
        con.close()


def read_meta_put(world: str, st: os.stat_result, params: str, payload: dict) -> None:
    """`read_doc` の総行数などを保存する。更新直後のファイルは保存しない。"""
    if not settled(st):
        return
    con = _connect(world)
    if con is None:
        return
    try:
        con.execute("INSERT OR REPLACE INTO readmeta(dev, ino, size, mtime_ns, params, payload) VALUES(?,?,?,?,?,?)",
                    (*stat_key(st), params, json.dumps(payload, ensure_ascii=False)))
        n = con.execute("SELECT COUNT(*) FROM readmeta").fetchone()[0]
        if n > _READ_META_MAX_ROWS:
            con.execute("DELETE FROM readmeta WHERE rowid IN (SELECT rowid FROM readmeta ORDER BY rowid LIMIT ?)",
                        (n - _READ_META_MAX_ROWS // 2,))
    except sqlite3.Error as e:
        _log.debug("search_cache: 総行数の書き込みに失敗しました (%s)", type(e).__name__)
    finally:
        con.close()


def tree_signature(dirs) -> str:
    """フォルダ木の署名。フォルダ（ファイルは見ない）の更新時刻を畳む。ファイルの追加・削除・改名で変わる。
    版管理の記録のフォルダ（`scope_infer.VCS_DIR_NAMES`）には入らず、シンボリックリンクは辿らない。
    """
    h = hashlib.sha1()
    for root in dirs:
        stack = [str(root)]
        while stack:
            d = stack.pop()
            try:
                mt = os.stat(d).st_mtime_ns
                h.update(f"{d}\0{mt}\n".encode("utf-8", "surrogateescape"))
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False) and not scope_infer.is_vcs_dir_name(e.name):
                            stack.append(e.path)
            except OSError:
                h.update(f"{d}\0!\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def pages_get(world: str, key: str, sig: str) -> dict | None:
    """保存済みの候補一覧。署名が違う・期限切れなら None。"""
    con = _connect(world)
    if con is None:
        return None
    try:
        r = con.execute("SELECT created, sig, payload FROM pages WHERE key=?", (key,)).fetchone()
        if r is None or r[1] != sig or time.time() - r[0] > _PAGES_TTL:
            return None
        return json.loads(r[2])
    except (sqlite3.Error, ValueError):
        return None
    finally:
        con.close()


def pages_put(world: str, key: str, sig: str, payload: dict) -> None:
    """候補一覧を保存する（古いものは件数上限で捨てる）。"""
    con = _connect(world)
    if con is None:
        return
    try:
        now = time.time()
        con.execute("INSERT OR REPLACE INTO pages(key, created, sig, payload) VALUES(?,?,?,?)",
                    (key, now, sig, json.dumps(payload, ensure_ascii=False)))
        con.execute("DELETE FROM pages WHERE created < ? OR key NOT IN "
                    "(SELECT key FROM pages ORDER BY created DESC LIMIT ?)", (now - _PAGES_TTL, _PAGES_MAX_ROWS))
    except sqlite3.Error as e:
        _log.debug("search_cache: 候補一覧の書き込みに失敗しました (%s)", type(e).__name__)
    finally:
        con.close()
