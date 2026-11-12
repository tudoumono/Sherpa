"""名前の台帳（`name_ledger`）の永続化。読み書きの唯一の窓口。
資料フォルダごとに「名前（正規化した大文字）・種類（グラフのノードのラベル）・出どころ・相対パス」を持つ。
出どころは `declared`（ソースの中に書かれた名前）と `filename`（ファイル名の拡張子を除いた部分）。
取り込みの台帳（`documents`）と同じ時点で入れ替え、資料フォルダの削除（`replace_documents(world, [])`）で一緒に消える。
表の作成は `store/schema_ingest.py`（`db._SCHEMA` に展開される）が持つ。
設計: docs/design/scope.md「期の中の参照の張り方と名前の台帳」
"""
from __future__ import annotations

import contextlib

from . import replace_rows as _replace_rows
from .db import _KB_ID, _connect, _ensure

_COLUMNS = "name, kind, source, rel_path"
_shown = _replace_rows.shown_cond   # 読みはどれも、資料フォルダの「今見せる番号」の行だけを相手にする


def clear_in(conn, world: str, *, row) -> None:
    """接続 `conn` のトランザクションの中で、資料フォルダの名前の台帳の全部の番号の行と「作った」記録を消す。`row` は入れ替えの行。"""
    _replace_rows.before_swap_in(conn, row, world, "ledger")
    conn.execute("DELETE FROM name_ledger WHERE world=%s", (world,))
    conn.execute("DELETE FROM name_ledger_built WHERE world=%s", (world,))


def write_in(conn, world: str, rows: list, *, row) -> int:
    """接続 `conn` のトランザクションの中で、資料フォルダの名前の台帳を丸ごと入れ替え、「作った」と記録する。行と「作った」記録には `row.ingest_no` を書く。
    `rows` は `name`・`kind`・`source`・`rel_path` を持つ dict。
    """
    clear_in(conn, world, row=row)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO name_ledger (world, name, kind, source, rel_path, ingest_no) VALUES (%s,%s,%s,%s,%s,%s)",
            [(world, r["name"], r.get("kind") or "", r["source"], r["rel_path"], row.ingest_no) for r in rows])
    conn.execute("INSERT INTO name_ledger_built (world, ingest_no) VALUES (%s,%s)", (world, row.ingest_no))
    return len(rows)


def apply_delta_in(conn, world: str, remove_paths: list, add_rows: list, *, shown: int) -> None:
    """接続 `conn` のトランザクションの中で、名前の台帳（今見せる番号 `shown` の行）から `remove_paths` と `add_rows` のパスの行だけを消し、`add_rows` を足す。
    台帳をまだ作っていない資料フォルダには何も書かない（作る前に行だけがあると「作った」記録と食い違う）。
    行が変わったときは「作った」時点も更新する（ページ送りの世代が変わる）。`ensure_table` の後に呼ぶ。
    """
    if not is_built(world, conn):
        return
    paths = sorted({*remove_paths, *(r["rel_path"] for r in add_rows)})
    removed = 0
    if paths:
        removed = conn.execute("DELETE FROM name_ledger WHERE world=%s AND ingest_no=%s AND rel_path=ANY(%s)",
                               (world, shown, paths)).rowcount
    if add_rows:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO name_ledger (world, name, kind, source, rel_path, ingest_no) VALUES (%s,%s,%s,%s,%s,%s)",
                [(world, r["name"], r.get("kind") or "", r["source"], r["rel_path"], shown) for r in add_rows])
    if removed or add_rows:
        conn.execute("UPDATE name_ledger_built SET built_at=clock_timestamp() WHERE world=%s AND ingest_no=%s",
                     (world, shown))


def doc_rows_match(world: str, rel: str, rows: list) -> bool:
    """今見せる番号のパス `rel` の行が `rows`（`rel_path` が `rel` のもの）と同じか。台帳をまだ作っていない資料フォルダは真（書かない）。"""
    _ensure()
    with _connect() as c:
        if not is_built(world, c):
            return True
        cond, p = _shown(c, world)
        have = c.execute(f"SELECT name, kind, source FROM name_ledger WHERE world=%s AND rel_path=%s{cond}", (world, rel, *p)).fetchall()
    return (sorted((r["name"], r["kind"] or "", r["source"]) for r in have)
            == sorted((r["name"], r.get("kind") or "", r["source"]) for r in rows if r["rel_path"] == rel))


def replace_rows(world: str, rows: list, *, row) -> int:
    """資料フォルダの名前の台帳だけを丸ごと入れ替え、今見せる番号を `row` の番号にする（1 トランザクション）。取り込みは `store.replace_documents(..., names=)` で取り込みの台帳と一緒に入れ替える。"""
    _ensure()
    with _connect() as c:
        n = write_in(c, world, rows, row=row)
        c.execute("UPDATE worlds SET shown_ingest_no=%s WHERE kb_id=%s AND world_id=%s", (row.ingest_no, _KB_ID, world))
        return n


@contextlib.contextmanager
def snapshot():
    """1 回の問い合わせ全体を同じ時点で読むための接続（REPEATABLE READ・読み取り専用）。`conn` を受ける読み取り関数へ渡す。"""
    _ensure()
    with _connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield c


@contextlib.contextmanager
def _use(conn):
    """`conn` があればそれを、無ければ新しい接続を使う。"""
    if conn is not None:
        yield conn
        return
    _ensure()
    with _connect() as c:
        yield c


def lookup(world: str, name: str, conn=None) -> list:
    """正規化済みの `name` に完全一致する行。"""
    with _use(conn) as c:
        cond, p = _shown(c, world)
        return c.execute(f"SELECT {_COLUMNS} FROM name_ledger WHERE world=%s AND name=%s{cond} ORDER BY source, rel_path",
                         (world, name, *p)).fetchall()


def lookup_shape(world: str, shape: str, conn=None) -> list:
    """正規化済みの目標の形 `shape`（`?` はちょうど 1 文字）に、名前そのもの、または `.拡張子` を付けた名前が一致する行。"""
    esc = shape.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("?", "_")
    with _use(conn) as c:
        cond, p = _shown(c, world)
        return c.execute(
            f"SELECT {_COLUMNS} FROM name_ledger WHERE world=%s AND (name LIKE %s ESCAPE '\\' OR name LIKE %s ESCAPE '\\'){cond} "
            "ORDER BY name, source, rel_path",
            (world, esc, esc + ".%", *p)).fetchall()


def like_prefix(world: str, prefix: str, tail_len: int, conn=None) -> list:
    """`prefix` の後ろに `tail_len` 文字が続く名前の行（番号だけが違う近い名前の絞り込み・数字かどうかは呼び出し側が確かめる）。"""
    esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _use(conn) as c:
        cond, p = _shown(c, world)
        return c.execute(
            f"SELECT {_COLUMNS} FROM name_ledger WHERE world=%s AND name LIKE %s ESCAPE '\\'{cond} ORDER BY name, source, rel_path",
            (world, esc + "_" * tail_len, *p)).fetchall()


def read_rows(world: str) -> list:
    """資料フォルダの名前の台帳の全行（点検が使う）。"""
    _ensure()
    with _connect() as c:
        cond, p = _shown(c, world)
        return c.execute(f"SELECT {_COLUMNS} FROM name_ledger WHERE world=%s{cond} ORDER BY name, source, rel_path",
                         (world, *p)).fetchall()


def is_built(world: str, conn=None) -> bool:
    """資料フォルダの名前の台帳を作り終えているか（名前が 1 つも無くても作っていれば真）。"""
    with _use(conn) as c:
        cond, p = _shown(c, world)
        return c.execute(f"SELECT 1 FROM name_ledger_built WHERE world=%s{cond}", (world, *p)).fetchone() is not None


def built_at(world: str, conn=None) -> str | None:
    """資料フォルダの名前の台帳を最後に作った時点（ISO 形式）。未作成なら `None`。ページ送りの世代の印に使う。"""
    with _use(conn) as c:
        cond, p = _shown(c, world)
        row = c.execute(f"SELECT built_at FROM name_ledger_built WHERE world=%s{cond}", (world, *p)).fetchone()
    if row is None:
        return None
    v = row["built_at"] if isinstance(row, dict) else row[0]
    return v.isoformat()
