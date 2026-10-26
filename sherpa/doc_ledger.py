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
    doctype／branch が欠けていれば状態に関係なく名前から補い、補えなければ None（呼び出し元が歩く経路へ戻す）。
    """
    name = r["name"]
    doctype, branch, status = r.get("doctype"), r.get("branch"), r.get("status")
    if doctype is None or branch is None:
        got = corpus_docs.classify_by_name(name)
        if got is None:
            return None
        doctype, branch = doctype or got[0], branch or got[1]
    return {"name": name, "doctype": doctype, "branch": branch, "status": status}


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
    台帳が空・台帳が読めない・rebind／削除の待ちが上限を超えた・行を補えないときは `documents_for`（実走査）の結果を返す（`ledger_at=None`）。
    台帳を置き換えた run を結びつけられないときは、突き合わせず `documents_for` と同じくソース枝の ready 文書を `unknown` に倒す。
    行は `name`／`doctype`／`branch`／`state` を持つ（`documents_for` の同名キーと同じ意味）。
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
            row = _listing_row_from_ledger(r)
            if row is None:
                rows = None
                break
            rows.append(row)
        if rows is not None:
            rows = _reconcile_ledger_blocked(rows, _blocked_of_ledger_run(snap["run"]))
            out = [{"name": r["name"], "doctype": r["doctype"], "branch": r["branch"],
                    "state": _LEDGER_STATUS_DISPLAY.get(r["status"], r["status"]) or "ready"}
                   for r in sorted(rows, key=lambda r: r["name"])]
            return Listing(out, snap["ledger_at"].isoformat())
    return Listing(documents_for(world, deadline=deadline), None)


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


def _importance_fields(rel: str, res_map: dict) -> dict:
    """`importance`／`importance_reason`／`importance_source` を値があれば返す（無ければ空 dict）。"""
    res = res_map.get(rel)
    if res is None:
        return {}
    out = {"importance": res.value, "importance_source": f"{res.config_path}:{res.rule_line}行目"}
    if res.reason:
        out["importance_reason"] = res.reason
    return out


def public_documents(world: str, *, deadline: float | None = None) -> list:
    """API／画面向けの文書一覧（物理パスは出さない）。`status` は `documents_for()` の `state` をそのまま通す。
    重要度（値・理由・由来）はあれば付ける。`deadline` を超えて歩き終えないときは `scope_infer.ScopeWalkDeadlineExceeded`。
    """
    # 文書列挙と重要度解決は同一 root・同一の列挙結果から行う（root 未解決なら空を返す）
    wd = worlds.world_dir(world)
    if not wd:
        return []
    files = list(si.safe_files(wd, deadline=deadline, also=worlds.archives_dir(world)))
    res_map = importance.resolve_for_world(world, root=wd, files=files)
    return [{"name": r["name"], "top_scope": r.get("top_scope"), "phase": r.get("phase"),
             "category": r.get("category"), "doctype": r.get("doctype"),
             "branch": r.get("branch"), "status": r.get("state", "ready"),
             **_importance_fields(r["name"], res_map)}
            for r in documents_for(world, root=wd, files=files)]


# 台帳の `status` を `public_documents()` の表示語彙へ合わせる（`indexed`→`ready`）
_LEDGER_STATUS_DISPLAY = {"indexed": "ready"}


def _ledger_row_to_public(r: dict) -> dict:
    """台帳の 1 行 → 公開形（`public_documents()` と同じ形）。範囲メタは `name` から導出し、重要度は台帳列をそのまま通す（実走査しない）。"""
    meta = si.rel_scope_meta(r["name"])
    status = r.get("status")
    out = {"name": r["name"], "top_scope": meta["top_scope"], "phase": meta["phase"],
           "category": meta["category"], "doctype": r.get("doctype"), "branch": r.get("branch"),
           "status": _LEDGER_STATUS_DISPLAY.get(status, status or "ready")}
    if r.get("importance") is not None:
        out["importance"] = r["importance"]
        out["importance_source"] = r.get("importance_source")
        if r.get("importance_reason"):
            out["importance_reason"] = r["importance_reason"]
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


def preview_documents(world: str, *, root=None, files=None, sig: str | None = None) -> list:
    """取り込みプレビュー用の文書一覧（走査結果 → 画面の形・物理パスは出さない）。
    派生 MD を持つ文書には読み取り方法の要約（`provenance`）、重要度（値・理由・由来）を best-effort で付ける。
    `state`／`label`／`reason` は `documents_for()` のものを通す。`encoding_partial` は符号化が不確実な資料（画面が「要確認」を出す材料）。
    `analyzer` は担当アナライザの内部名（コード文書のみ）。
    `root`／`files`／`sig`: 呼び出し側が解決・列挙・署名取得済みなら渡す（二重の木走査を避ける）。未登録の資料フォルダの `sig`（空文字）は渡さない。
    """
    # 文書列挙と重要度解決は同一 root から行う
    wd = root if root is not None else worlds.world_dir(world)
    if not wd:
        return []
    res_map = importance.resolve_for_world(world, root=wd, files=files, sig=sig)
    out = []
    for r in documents_for(world, root=wd, files=files):
        doc = {"name": r["name"], "doctype": r.get("doctype"), "branch": r.get("branch"),
               "analyzer": r.get("analyzer"),
               "top_scope": r.get("top_scope"), "phase": r.get("phase"),
               "category": r.get("category"), "folder": "/".join(r["name"].split("/")[:-1]),
               "state": r.get("state", "ready"), "label": r.get("label", "使えます"),
               "reason": r.get("reason"),
               **_importance_fields(r["name"], res_map)}
        if r.get("encoding_partial"):
            doc["encoding_partial"] = True
        prov = corpus_docs.provenance_summary(r.get("md_path"))
        if prov:
            doc["provenance"] = prov
        out.append(doc)
    return out


def control_diagnostics(world: str, *, root=None, files=None) -> list:
    """`_重要度.txt` の構文診断（台帳・取り込みプレビューの警告バナー用）。引数は `importance.diagnostics_for_world` へ渡す。"""
    return importance.diagnostics_for_world(world, root=root, files=files)


def original_path(rel: str, world: str):
    """根拠 DL の原本 Path（パス基準・無ければ None）。"""
    return documents.resolve(rel, world)
