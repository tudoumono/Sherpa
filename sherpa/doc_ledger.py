"""文書台帳（パス基準）。資料フォルダのフォルダ木を走査して文書を表す（doc_id＝rel_path）。
範囲・プレビュー・DL が参照する単一の出所。原本 DL は `documents.resolve`。
設計: docs/design/data.md「KB（取り込み・範囲・同一性）」
"""
from __future__ import annotations

import logging
from typing import NamedTuple

from . import corpus_docs, documents, scope_infer as si, store, worlds
from .ingest import importance, text_kind

_log = logging.getLogger("sherpa")


def documents_for(world: str, *, root=None, deadline: float | None = None, files=None) -> list:
    """資料フォルダの文書一覧（rel_path＝doc_id・範囲メタ付き）。
    `root`／`files`／`deadline` は `corpus_docs.world_documents` へそのまま渡す（二重の木走査を避ける。`files` は materialize 済み list）。
    直近 ingest run の blocked flag を突き合わせ、読み取れなかった文書は `state="unreadable"` にする。
    直近 run を確認できなかった場合（`last_run_blocked_docs` が None）は、ソース枝の ready 文書を `state="unknown"` に倒す（黙って「使えます」にしない）。
    """
    rows = corpus_docs.world_documents(world, root=root, deadline=deadline, files=files)
    blocked = corpus_docs.last_run_blocked_docs(world, deadline=deadline)
    if blocked is None:
        return [
            {**r, "state": "unknown", "label": "状態を確認できませんでした", "reason": None}
            if r.get("branch") == "source" and r.get("state") == "ready" else r
            for r in rows
        ]
    if not blocked:
        return rows
    out = []
    for r in rows:
        reason = blocked.get(r["name"])
        if reason and r.get("state") != "unreadable":
            r = {**r, "state": "unreadable", "label": "読み取れません", "reason": reason}
        out.append(r)
    return out


class Listing(NamedTuple):
    """一覧の道具向けの文書行。`ledger_at`＝台帳から出したときの台帳の行の更新時刻（ISO 8601）・歩いたときは None。"""
    rows: list
    ledger_at: str | None


def _listing_row_from_ledger(r: dict) -> dict | None:
    """台帳の 1 行 → 一覧の道具が使う形（`name`／`doctype`／`branch`／`status`）。
    doctype／branch が欠けていれば状態に関係なく名前から補い、補えなければ None（呼び出し元が `_unresolved_listing_row` で扱う）。
    """
    name = r["name"]
    doctype, branch, status = r.get("doctype"), r.get("branch"), r.get("status")
    if doctype is None or branch is None:
        got = corpus_docs.classify_by_name(name)
        if got is None:
            return None
        doctype, branch = doctype or got[0], branch or got[1]
    return {"name": name, "doctype": doctype, "branch": branch, "status": status}


def _unresolved_listing_row(r: dict) -> dict:
    """名前から種類を補えない台帳の行。台帳の値のまま（空は空）で、歩かずに出す。
    補えない行は、歩くと `unreadable` で `doctype`／`branch` が空になる行か、古い版が書いた行・中身を読まないと種類が決まらない行。
    後者は `_listing_state` が `unknown`（種類が分からない）にする。
    """
    return {"name": r["name"], "doctype": r.get("doctype"), "branch": r.get("branch"),
            "status": r.get("status"), "kind_unknown": True}


def _blocked_of_ledger_run(run) -> dict | None:
    """台帳を置き換えた run の blocked flag（`{doc: reason}`）。run が無い・まだ終わっていない（flags が確定していない）ときは None（呼び出し元が unknown に倒す）。"""
    if run is None or run.get("status") in ("queued", "extracting"):
        return None
    flags = run.get("flags")
    flags = flags if isinstance(flags, list) else []
    return {f["doc"]: f["reason"] for f in flags
            if isinstance(f, dict) and f.get("action") == "blocked"
            and isinstance(f.get("doc"), str) and f.get("reason")}


def listing_documents(world: str, *, deadline: float | None = None) -> Listing:
    """一覧の道具（list_docs／glob_search／folder_tree）向けの文書行（`name` 昇順）。
    台帳があればそれだけを読み（資料フォルダは歩かない）、`ledger_at`（台帳の行の更新時刻）を付ける。
    台帳の行・時点・blocked の突き合わせ（台帳を置き換えた run の flags だけ）は `world_swap_lock_shared` の有界の待ちの中で、1 つのスナップショットから読む。
    台帳が空・台帳が読めない・rebind／削除の待ちが上限を超えたときは `documents_for`（実走査）の結果を返す（`ledger_at=None`）。
    名前から種類を補えない行があっても台帳は捨てない。`unreadable` の行は歩いたときと同じ（`doctype`／`branch` 空・`state="unreadable"`）、それ以外は `doctype`／`branch` 空・`state="unknown"`（種類が分からない）で出す。
    台帳を置き換えた run を結びつけられないときは、突き合わせず `documents_for` と同じくソース枝の ready 文書を `unknown` に倒す。
    行は `name`／`doctype`／`branch`／`state` を持つ（`documents_for` の同名キーと同じ意味）。
    設計: docs/design/codex.md「一覧の道具」
    """
    try:
        with store.world_swap_lock_shared(world, timeout_ms=SWAP_LOCK_WAIT_MS):
            snap = store.read_ledger_for_listing(world)
    except Exception as e:
        _log.warning("listing_documents: world=%s 台帳を読めないため資料フォルダを歩きます: %s", world, e)
        snap = None
    if snap is not None and snap["rows"]:
        rows = []
        for r in snap["rows"]:
            if text_kind.is_sensitive_doc_id(r["name"]) or importance.is_importance_control_path(r["name"]):
                continue
            rows.append(_listing_row_from_ledger(r) or _unresolved_listing_row(r))
        rows = _reconcile_ledger_blocked(rows, _blocked_of_ledger_run(snap["run"]))
        out = [_listing_out(r) for r in sorted(rows, key=lambda r: r["name"])]
        return Listing(out, snap["ledger_at"].isoformat())
    return Listing(documents_for(world, deadline=deadline), None)


def is_kind_unknown(row: dict) -> bool:
    """一覧の行が「種類が分からない」行か（台帳から名前で種類を補えず、読み取れない行でもない行。doctype か branch のどちらかが欠ける）。"""
    return row.get("state") == "unknown" and (row.get("doctype") is None or row.get("branch") is None)


def kind_unknown_notice(n: int) -> str:
    """種類が分からない行があるときに結果へ付ける注意（層・種類の絞り込みの件数や「該当なし」を確定させないため）。"""
    return (f"種類が分からない資料が {n} 件ある。層・種類の絞り込みの件数や「該当なし」は確定しない。"
            "中身で確かめる。")


def partial_notice(n: int) -> str:
    """state が `partial`（一部使えます）の資料があるときに結果へ付ける注意。名前で見つかり開けるが、意味の索引・資料どうしの関係には入っていない。"""
    return (f"state が partial の資料が {n} 件ある。ファイル名では見つかり、読む道具で開ける。"
            "意味の索引（es_search）と資料どうしの関係（graph_neighbors）には入っていない。"
            "テキスト・ソースは文字列の検索（ripgrep_search）で中身も探せるが、Office・PDF は"
            "変換されるまで文字列の中身検索に当たらない（開いて読む）。"
            "旧形式の Office（.doc・.xls・.ppt）は変換されるまで開けない。")


def _listing_out(r: dict) -> dict:
    """一覧の 1 行。名前から種類を補えず、最終の状態が読み取れない行は、歩いたときと同じ形（種類なし・資料の層）にそろえる。"""
    state = _listing_state(r)
    if r.get("kind_unknown") and state == "unreadable":
        return {"name": r["name"], "doctype": None, "branch": None, "state": state}
    return {"name": r["name"], "doctype": r["doctype"], "branch": r["branch"], "state": state}


def _listing_state(r: dict) -> str:
    """一覧の行の `state`。種類が分からない行（`kind_unknown`）は、読み取れない行以外を `unknown` にする。"""
    status = _LEDGER_STATUS_DISPLAY.get(r["status"], r["status"]) or "ready"
    if r.get("kind_unknown") and status != "unreadable":
        return "unknown"
    return status


def ledger_rels(world: str) -> list | None:
    """台帳の文書名（rel_path・`name` 昇順）。秘匿名・重要度の設定ファイルは除く。
    資料フォルダは歩かない。台帳が空・読めない・rebind／削除の待ちが上限を超えたときは None（呼び出し元が歩く経路へ戻す）。
    """
    try:
        with store.world_swap_lock_shared(world, timeout_ms=SWAP_LOCK_WAIT_MS):
            names = store.list_document_names(world)
    except Exception as e:
        _log.warning("ledger_rels: world=%s 台帳を読めないため資料フォルダを歩きます: %s", world, e)
        return None
    if not names:
        return None
    return [n for n in names
            if not text_kind.is_sensitive_doc_id(n) and not importance.is_importance_control_path(n)]


def public_documents(world: str, *, deadline: float | None = None) -> list:
    """API／画面向けの文書一覧（物理パスは出さない）。`status` は `documents_for()` の `state` をそのまま通す。
    `deadline` を超えて歩き終えないときは `scope_infer.ScopeWalkDeadlineExceeded`。
    """
    # root 未解決なら空を返す
    wd = worlds.world_dir(world)
    if not wd:
        return []
    files = list(si.safe_files(wd, deadline=deadline, also=worlds.archives_dir(world)))
    return [{"name": r["name"], "top_scope": r.get("top_scope"), "phase": r.get("phase"),
             "category": r.get("category"), "doctype": r.get("doctype"),
             "branch": r.get("branch"), "status": r.get("state", "ready")}
            for r in documents_for(world, root=wd, files=files)]


# 台帳の `status` を `public_documents()` の表示語彙へ合わせる（`indexed`→`ready`）
_LEDGER_STATUS_DISPLAY = {"indexed": "ready"}


def _ledger_row_to_public(r: dict) -> dict:
    """台帳の 1 行 → 公開形（`public_documents()` と同じ形）。範囲メタは `name` から導出する（実走査しない）。"""
    meta = si.rel_scope_meta(r["name"])
    status = r.get("status")
    out = {"name": r["name"], "top_scope": meta["top_scope"], "phase": meta["phase"],
           "category": meta["category"], "doctype": r.get("doctype"), "branch": r.get("branch"),
           "status": _LEDGER_STATUS_DISPLAY.get(status, status or "ready")}
    return out


def _reconcile_ledger_blocked(rows: list, blocked) -> list:
    """台帳行に直近 run の blocked flag を突き合わせる（`documents_for()` の台帳版）。
    直近 run を確認できなかった場合（`blocked is None`）は、ソース枝の indexed 行を "unknown" に倒す。
    """
    if blocked is None:
        return [
            {**r, "status": "unknown"} if r.get("branch") == "source" and r.get("status") == "indexed" else r
            for r in rows
        ]
    if not blocked:
        return rows
    out = []
    for r in rows:
        if r["name"] in blocked and r.get("status") != "unreadable":
            r = {**r, "status": "unreadable"}
        out.append(r)
    return out


# 参照先の入れ替え（rebind／削除）の最中に読み取りが待つ上限（ミリ秒）。超えたら `LockNotAvailable`（呼び出し元が 503 にする）。
SWAP_LOCK_WAIT_MS = 5_000


def public_documents_page(world: str, *, limit: int | None, offset: int = 0) -> tuple[list, int]:
    """`GET /documents` の応答本体（総件数＋文書一覧）。
    `limit=None` は全件、指定時のみ LIMIT/OFFSET。台帳に行があれば台帳だけを読み（フォルダは歩かない）、
    COUNT とページは 1 つの読み取りトランザクションで同じ世代に固定する（取り込みの最中も待たない）。
    rebind／削除の最中だけ `world_swap_lock_shared` で待ち、上限（`SWAP_LOCK_WAIT_MS`）を超えたら `LockNotAvailable`。
    台帳が空なら `public_documents`（実走査・期限つき）へ縮退してメモリ上でスライスする。期限を超えたら `scope_infer.ScopeWalkDeadlineExceeded`。
    """
    with store.world_swap_lock_shared(world, timeout_ms=SWAP_LOCK_WAIT_MS):
        total, rows = store.count_and_list_documents(world, limit=limit, offset=offset)
        if total > 0:
            rows = _reconcile_ledger_blocked(rows, corpus_docs.last_run_blocked_docs(world))
            # 秘匿名の行は一覧に出さない（全件取得時は件数も再計算）
            rows = [r for r in rows if not text_kind.is_sensitive_doc_id(r["name"])]
            docs = [_ledger_row_to_public(r) for r in rows]
            return docs, (len(docs) if limit is None else total)
    all_docs = public_documents(world, deadline=si.user_op_deadline())
    if limit is None:
        return all_docs, len(all_docs)
    return all_docs[offset:offset + limit], len(all_docs)


def preview_documents(world: str, *, root=None, files=None) -> list:
    """取り込みプレビュー用の文書一覧（走査結果 → 画面の形・物理パスは出さない）。
    派生 MD を持つ文書には読み取り方法の要約（`provenance`）を best-effort で付ける。
    `state`／`label`／`reason` は `documents_for()` のものを通す。`encoding_partial` は符号化が不確実な資料（画面が「要確認」を出す材料）。
    `analyzer` は担当アナライザの内部名（コード文書のみ）。
    `root`／`files`: 呼び出し側が解決・列挙済みなら渡す（二重の木走査を避ける）。
    """
    wd = root if root is not None else worlds.world_dir(world)
    if not wd:
        return []
    out = []
    for r in documents_for(world, root=wd, files=files):
        doc = {"name": r["name"], "doctype": r.get("doctype"), "branch": r.get("branch"),
               "analyzer": r.get("analyzer"),
               "top_scope": r.get("top_scope"), "phase": r.get("phase"),
               "category": r.get("category"), "folder": "/".join(r["name"].split("/")[:-1]),
               "state": r.get("state", "ready"), "label": r.get("label", "使えます"),
               "reason": r.get("reason")}
        if r.get("encoding_partial"):
            doc["encoding_partial"] = True
        prov = corpus_docs.provenance_summary(r.get("md_path"))
        if prov:
            doc["provenance"] = prov
        out.append(doc)
    return out


def original_path(rel: str, world: str):
    """根拠 DL の原本 Path（パス基準・無ければ None）。"""
    return documents.resolve(rel, world)
