"""文書台帳（documents）の読み書き。
設計: docs/design/data.md「KB（取り込み・範囲・同一性）」
"""
from __future__ import annotations

from . import ledger_stage, name_ledger, replace_rows
from .db import _KB_ID, _connect, _ensure


def list_document_worlds() -> list:
    """文書台帳に行が存在する world_id の一覧（孤児掃除用）。"""
    _ensure()
    with _connect() as c:
        return [r["version"] for r in
               c.execute("SELECT DISTINCT version FROM documents WHERE kb_id=%s", (_KB_ID,)).fetchall()]


def replace_documents(world, rows, *, row, names: list | None = None) -> int:
    """world の文書台帳を入れ替える（冪等）。`rows` は doc dict のリスト。`row` は入れ替えの行（`ingest_replacements`）。
    `rows` があれば、`row.ingest_no` の番号で下書きに入れてから見せる番号を切り替える（`ledger_stage`）。`names`（名前の台帳の行）も同じ番号で入れ、同じ時点で切り替わる。
    `rows` が空（資料フォルダの削除）のときは、全部の番号の行と名前の台帳を同じトランザクションで消す。
    列名 `version` は world_id を保持する。importance 系 3 列は `rows` に無ければ NULL。
    設計: docs/design/data.md「PostgreSQL（主な表と役割）」
    """
    _ensure()
    if rows:
        ledger_stage.stage_ledger(world, rows, names, row=row)
        ledger_stage.switch_ledger(world, row=row)
        return len(rows)
    with _connect() as c:
        replace_rows.before_swap_in(c, row, world, "ledger")
        c.execute("DELETE FROM documents WHERE kb_id=%s AND version=%s", (_KB_ID, world))
        name_ledger.clear_in(c, world, row=row)
        c.execute("UPDATE worlds SET shown_ingest_no=%s WHERE kb_id=%s AND world_id=%s",
                  (row.ingest_no, _KB_ID, world))
    return 0


_FILE_STATE_GENERATION_COLUMNS = ("root_path", "last_sig", "created_at", "updated_at", "last_synced_at")


def apply_file_state_delta(world, *, add_rows, remove_names, expected: dict, add_names: list | None = None) -> bool:
    """「ファイルの状態を更新」専用: 台帳へ指定の行だけ足し、指定の名前の行だけ消す（他の行・`worlds` の署名と明細は触らない）。
    `worlds` の行が読み取り時点の世代（`expected` の `_FILE_STATE_GENERATION_COLUMNS`）と同じで、台帳の変更印も `expected["ledger_stamp"]` と同じときだけ、1 トランザクションで書く。
    名前の台帳（`name_ledger`）も同じトランザクションで、消した行のパスの行を消し、足した行の名前（`add_names`）を入れ替える。
    戻り値: 書いたら True・世代が変わっていたら False（書かない）。取り込み（`replace_documents`）からは呼ばない。
    """
    _ensure()
    gen = [expected[k] for k in _FILE_STATE_GENERATION_COLUMNS]
    with _connect() as c:
        locked = c.execute(
            "SELECT shown_ingest_no FROM worlds WHERE kb_id=%s AND world_id=%s AND root_path=%s "
            "AND last_sig IS NOT DISTINCT FROM %s AND created_at IS NOT DISTINCT FROM %s "
            "AND updated_at IS NOT DISTINCT FROM %s AND last_synced_at IS NOT DISTINCT FROM %s FOR UPDATE",
            (_KB_ID, world, *gen)).fetchone()
        if locked is None:
            return False
        shown = locked["shown_ingest_no"]
        stamp = c.execute("SELECT COUNT(*) AS n, MAX(updated_at) AS at FROM documents "
                          "WHERE kb_id=%s AND version=%s AND ingest_no=%s", (_KB_ID, world, shown)).fetchone()
        if (stamp["n"], stamp["at"]) != tuple(expected["ledger_stamp"]):
            return False
        for name in remove_names:
            c.execute("DELETE FROM documents WHERE kb_id=%s AND version=%s AND name=%s AND ingest_no=%s",
                      (_KB_ID, world, name, shown))
        for r in add_rows:
            c.execute(
                "INSERT INTO documents (kb_id, version, name, layer, scope_path, doctype, branch, "
                "  original_path, md_path, status, ingest_no) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (kb_id, version, ingest_no, name) DO NOTHING",
                (_KB_ID, world, r["name"], r.get("layer") or "version", r.get("scope_path"), r.get("doctype"),
                 r.get("branch"), r.get("original_path"), r.get("md_path"), r.get("status"), shown))
        name_ledger.apply_delta_in(c, world, list(remove_names), list(add_names or []), shown=shown)
    return True


def get_document(world, name) -> dict | None:
    """world の文書台帳の 1 行（`list_documents` と同じ列・無ければ None）。"""
    _ensure()
    with _connect() as c:
        cond, p = replace_rows.shown_cond(c, world)
        return c.execute(
            "SELECT name, layer, scope_path, doctype, branch, original_path, md_path, status, "
            "  importance, importance_reason, importance_source "
            f"FROM documents WHERE kb_id=%s AND version=%s AND name=%s{cond}", (_KB_ID, world, name, *p)).fetchone()


def replace_document(world, name, row_data: dict | None, *, row, names: list | None = None) -> None:
    """world の文書台帳の 1 行だけを入れ替える（`row_data` が None なら消す）。列は `replace_documents` と同じ。
    今見せている番号（`worlds.shown_ingest_no`）の行だけを入れ替え、番号はそのままにする（`shown_ingest_no` も進めない）。この入れ替えの番号は `doc_ingest_no` に書く。
    `names` があれば、名前の台帳のその文書（パス `name`）の行も同じトランザクションで `names` に入れ替える（`names=[]` なら消す）。
    """
    _ensure()
    with _connect() as c:
        replace_rows.before_swap_in(c, row, world, "ledger")
        w = c.execute("SELECT shown_ingest_no FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world)).fetchone()
        shown = w["shown_ingest_no"] if w else 0
        c.execute("DELETE FROM documents WHERE kb_id=%s AND version=%s AND name=%s AND ingest_no=%s",
                  (_KB_ID, world, name, shown))
        if row_data is not None:
            c.execute(
                "INSERT INTO documents (kb_id, version, name, layer, scope_path, doctype, branch, "
                "  original_path, md_path, status, importance, importance_reason, importance_source, "
                "  ingest_no, doc_ingest_no) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_KB_ID, world, name, row_data.get("layer") or "version", row_data.get("scope_path"),
                 row_data.get("doctype"), row_data.get("branch"), row_data.get("original_path"),
                 row_data.get("md_path"), row_data.get("status"), row_data.get("importance"),
                 row_data.get("importance_reason"), row_data.get("importance_source"), shown, row.ingest_no))
        if names is not None:
            name_ledger.apply_delta_in(c, world, [name], names, shown=shown)


def document_names_match(world, name, names: list) -> bool:
    """名前の台帳の、今見せる番号のその文書（パス `name`）の行が `names` と同じか。名前の台帳をまだ作っていない資料フォルダは真（書かない）。"""
    return name_ledger.doc_rows_match(world, name, names)


def list_documents(world) -> list:
    """world の文書台帳（name/layer/scope_path/doctype/branch/original_path/md_path/status/importance 系）。"""
    _ensure()
    with _connect() as c:
        cond, p = replace_rows.shown_cond(c, world)
        return c.execute(
            "SELECT name, layer, scope_path, doctype, branch, original_path, md_path, status, "
            "  importance, importance_reason, importance_source "
            f"FROM documents WHERE kb_id=%s AND version=%s{cond} ORDER BY name", (_KB_ID, world, *p)).fetchall()


def list_document_names(world) -> list:
    """world の文書台帳の `name` だけ（`name` 昇順・範囲の選択肢や実在の判定が使う）。"""
    _ensure()
    with _connect() as c:
        cond, p = replace_rows.shown_cond(c, world)
        return [r["name"] for r in c.execute(
            f"SELECT name FROM documents WHERE kb_id=%s AND version=%s{cond} ORDER BY name", (_KB_ID, world, *p)).fetchall()]


def ledger_stamp(world) -> tuple:
    """world の文書台帳の変更印 `(件数, 行の最大 updated_at)`。台帳が空なら `(0, None)`。
    索引 `doc_ver` で引く 1 行の集計で、台帳の全件は読まない（置き換えると updated_at が進む）。
    """
    _ensure()
    with _connect() as c:
        cond, p = replace_rows.shown_cond(c, world)
        row = c.execute(f"SELECT COUNT(*) AS n, MAX(updated_at) AS at FROM documents WHERE kb_id=%s AND version=%s{cond}",
                        (_KB_ID, world, *p)).fetchone()
    return (row["n"], row["at"]) if row else (0, None)


def document_exists(world, name) -> bool:
    """world の文書台帳に `name`（正準表記）が完全一致で存在するか。原本DL の別名拒否に使う。"""
    _ensure()
    with _connect() as c:
        cond, p = replace_rows.shown_cond(c, world)
        row = c.execute(
            f"SELECT 1 FROM documents WHERE kb_id=%s AND version=%s AND name=%s{cond} LIMIT 1",
            (_KB_ID, world, name, *p)).fetchone()
    return row is not None


def count_documents(world) -> int:
    """world の文書台帳の総件数（GET /documents のページング用）。"""
    _ensure()
    with _connect() as c:
        cond, p = replace_rows.shown_cond(c, world)
        row = c.execute(
            f"SELECT COUNT(*) AS n FROM documents WHERE kb_id=%s AND version=%s{cond}",
            (_KB_ID, world, *p)).fetchone()
    return row["n"] if row else 0


def list_documents_page(world, *, limit: int, offset: int) -> list:
    """world の文書台帳を `name` 順にページング取得する（LIMIT/OFFSET）。列は `list_documents` と同じ。"""
    _ensure()
    with _connect() as c:
        cond, p = replace_rows.shown_cond(c, world)
        return c.execute(
            "SELECT name, layer, scope_path, doctype, branch, original_path, md_path, status, "
            "  importance, importance_reason, importance_source "
            f"FROM documents WHERE kb_id=%s AND version=%s{cond} ORDER BY name LIMIT %s OFFSET %s",
            (_KB_ID, world, *p, limit, offset)).fetchall()


def count_and_list_documents(world, *, limit: int | None, offset: int = 0) -> tuple[int, list]:
    """総件数と文書台帳のページ（`limit=None` は全件）を、1 つの読み取り専用 REPEATABLE READ トランザクションで読む。
    取り込みの台帳書き込み（`replace_documents`）が COUNT とページの間に入っても、両者は同じ世代の台帳になる。
    総件数が 0 のときは行を読まない。
    """
    _ensure()
    with _connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cond, p = replace_rows.shown_cond(c, world)
        keys = (_KB_ID, world, *p)
        cols = ("SELECT name, layer, scope_path, doctype, branch, original_path, md_path, status, "
                "  importance, importance_reason, importance_source "
                f"FROM documents WHERE kb_id=%s AND version=%s{cond} ORDER BY name")
        row = c.execute(f"SELECT COUNT(*) AS n FROM documents WHERE kb_id=%s AND version=%s{cond}", keys).fetchone()
        total = row["n"] if row else 0
        if total == 0:
            return 0, []
        if limit is None:
            return total, c.execute(cols, keys).fetchall()
        return total, c.execute(cols + " LIMIT %s OFFSET %s", (*keys, limit, offset)).fetchall()


def _prefix_cond(prefixes) -> tuple[str, tuple]:
    """`name` が、いずれかのフォルダ prefix（その名前そのもの、または配下）である条件。`name` の前方一致なので索引 `doc_ver_no_name_prefix` で引ける。"""
    if not prefixes:
        return "", ()
    parts, params = [], []
    for p in prefixes:
        parts.append("(name = %s OR (name ~>=~ %s AND name ~<~ %s))")
        params += [p, p + "/", p + "0"]
    return " AND (" + " OR ".join(parts) + ")", tuple(params)


def read_ledger_for_listing(world, prefixes=None) -> dict:
    """一覧の道具向けに、台帳の行・台帳の時点・台帳を置き換えた run の blocked を 1 つの読み取り専用 REPEATABLE READ トランザクションで読む。
    戻り値は `{"rows": [{name, doctype, branch, status}], "total": 台帳の総件数, "ledger_at": 台帳の行の最大 updated_at, "run": {status, flags} | None}`。行は一覧が使う 4 列だけ。
    `prefixes`（フォルダ prefix の列）を渡すと、行はその配下の分だけを SQL で絞って読む。`total`・`ledger_at`・`run` は絞らない台帳全体のもの。
    `run`＝台帳を書いた時点（`partial` 以外の行の最大 updated_at＝後から足された行は含めない）より前に作られた最新の run（同じ資料フォルダの run は 1 つずつ動くので、台帳を最後に書いたのはこの run）。無ければ None。
    台帳が空なら `rows=[]`・`total=0`・`ledger_at=None`・`run=None`。
    """
    _ensure()
    with _connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cond, p = replace_rows.shown_cond(c, world)
        keys = (_KB_ID, world, *p)
        head = c.execute(f"SELECT COUNT(*) AS n, MAX(updated_at) AS at FROM documents WHERE kb_id=%s AND version=%s{cond}",
                         keys).fetchone()
        if not head or head["n"] == 0:
            return {"rows": [], "total": 0, "ledger_at": None, "run": None}
        pcond, pp = _prefix_cond(prefixes)
        rows = c.execute(f"SELECT name, doctype, branch, status FROM documents WHERE kb_id=%s AND version=%s{cond}{pcond}",
                         (*keys, *pp)).fetchall()
        written = c.execute(
            f"SELECT MAX(updated_at) AS at FROM documents WHERE kb_id=%s AND version=%s{cond} "
            "AND status IS DISTINCT FROM 'partial'", keys).fetchone()["at"]
        run = c.execute(
            "SELECT status, extraction_snapshot->'flags' AS flags FROM ingest_runs "
            "WHERE kb_id=%s AND version=%s AND created_at <= %s ORDER BY id DESC LIMIT 1",
            (_KB_ID, world, written or head["at"])).fetchone()
        return {"rows": rows, "total": head["n"], "ledger_at": head["at"], "run": run}
