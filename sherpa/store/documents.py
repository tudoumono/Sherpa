"""文書台帳（P3・DATA-MODEL documents）。

`sherpa/store/__init__.py` から純移動（フェーズ4 S4）。ロジックは一切変更していない。
"""
from __future__ import annotations

from .db import _KB_ID, _connect, _ensure


def list_document_worlds() -> list:
    """文書台帳に行が存在する world_id（列名は歴史的に `version`）の一覧（バッチ2・3番・2026-07-03）。

    孤児掃除（`sherpa.reconcile`）が「registry に無い world の documents が残っていないか」を
    判定するための一覧取得。ES/Neo4j/派生MD と違い、documents はレジストリ削除の正規経路
    （`worker.wipe_world`→`replace_documents(world, [])`）を通らずに作られる余地がある
    （テスト直書き・過去の異常終了 等）と、これまで reconcile の対象外だったため取りこぼしていた。
    """
    _ensure()
    with _connect() as c:
        return [r["version"] for r in
               c.execute("SELECT DISTINCT version FROM documents WHERE kb_id=%s", (_KB_ID,)).fetchall()]


def replace_documents(world, rows) -> int:
    """world の文書台帳を丸ごと入れ替える（再 seed・冪等）。`rows` は doc dict のリスト。

    列名 `version` は歴史的（DB 不変・語彙統一のスコープ外）。引数/値は world 用語。
    `importance`/`importance_reason`/`importance_source`（RV1是正#2）: `rows` に無ければ `None`
    （§2 truth table＝無ければ3列とも NULL・`ingest/worker.py::_ledger_rows` が ingest 時に解決して
    渡す。列自体は他の呼び出し元（テスト直書き等）が省略しても後方互換）。
    """
    _ensure()
    with _connect() as c:
        c.execute("DELETE FROM documents WHERE kb_id=%s AND version=%s", (_KB_ID, world))
        for r in rows:
            c.execute(
                "INSERT INTO documents (kb_id, version, name, layer, scope_path, doctype, branch, "
                "  original_path, md_path, status, importance, importance_reason, importance_source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_KB_ID, world, r.get("name"), r.get("layer") or "version", r.get("scope_path"),
                 r.get("doctype"), r.get("branch"), r.get("original_path"), r.get("md_path"), r.get("status"),
                 r.get("importance"), r.get("importance_reason"), r.get("importance_source")))
    return len(rows)


def list_documents(world) -> list:
    """world の文書台帳（name/layer/scope_path/doctype/branch/original_path/md_path/status/
    importance/importance_reason/importance_source）。列名 version は DB 不変。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT name, layer, scope_path, doctype, branch, original_path, md_path, status, "
            "  importance, importance_reason, importance_source "
            "FROM documents WHERE kb_id=%s AND version=%s ORDER BY name", (_KB_ID, world)).fetchall()


def document_exists(world, name) -> bool:
    """world の文書台帳に `name`（rel_path・safe_files 由来の正準表記）が**完全一致**で存在するか。

    `UNIQUE (kb_id, version, name)` の索引付き1行 SELECT（O(1)・world 内の文書総数に依存しない）。
    原本DL 前段の別名拒否（大文字小文字/8.3短縮名等のエイリアスや列挙不能ディレクトリ内の
    既知ファイル名は台帳の正準表記と一致しない限り実在扱いしない）に使う。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT 1 FROM documents WHERE kb_id=%s AND version=%s AND name=%s LIMIT 1",
            (_KB_ID, world, name)).fetchone()
    return row is not None


def count_documents(world) -> int:
    """world の文書台帳の総件数（`doc_ver` 索引の範囲カウント・GET /documents ページング用）。

    フォルダを歩かない——`kb_id, version` の索引範囲を数えるだけ（S工事②・2026-09-01）。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE kb_id=%s AND version=%s",
            (_KB_ID, world)).fetchone()
    return row["n"] if row else 0


def list_documents_page(world, *, limit: int, offset: int) -> list:
    """world の文書台帳を `name` 順にページング取得（LIMIT/OFFSET）。列は `list_documents` と同じ。

    `GET /documents` の定数時間化用（S工事②・2026-09-01）——世界全体を読まず、要求されたページ分だけ
    索引付き SELECT で取る（フォルダを歩かない）。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT name, layer, scope_path, doctype, branch, original_path, md_path, status, "
            "  importance, importance_reason, importance_source "
            "FROM documents WHERE kb_id=%s AND version=%s ORDER BY name LIMIT %s OFFSET %s",
            (_KB_ID, world, limit, offset)).fetchall()
