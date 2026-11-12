"""一覧の道具 `list_docs`・`glob_search`（取り込みの台帳の一覧を、範囲・層・絞り込みで返す）。
設計: docs/design/codex.md「MCP の道具」
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from ... import corpus_docs, doc_reach, documents, scope_infer
from ... import layer as layer_mod
from ... import scope as scope_mod
from ...ingest import importance, text_kind
from . import name_lookup

# glob_search の返却上限（打ち切りは明示する）。env 化しない
GLOB_MAX_RESULTS = 200
# `_重要度.txt` の glob と同じ長さ上限を流用する（`importance._match_segment_glob` を共有するため）
GLOB_PATTERN_MAX_LEN = importance._MAX_PATTERN_LEN
# 続きの呼び出しで一覧の同一性を確かめる印。台帳から出したときは `ledger_at` に行のハッシュを足した値、資料フォルダを歩いたときはこの値
GLOB_WALK_GENERATION = "walk"
GLOB_GENERATION_MISMATCH_ERROR = "glob_generation_mismatch"
GLOB_GENERATION_REQUIRED_ERROR = "glob_generation_required"
_GLOB_WALK_NOTICE = "資料フォルダを歩いて出した一覧なので、ページの間で一覧が変わることがある。"
_BUDGET_TOO_SMALL_HINT = "1件も返せません。範囲を絞るか、1件あたりの上限を上げてください"
# 取り込みの後に置かれた資料を歩く時間の上限（秒）。道具の期限と、この上限の早い方まで歩く
AFTER_INGEST_WALK_SECONDS = 20.0
# `after_ingest` に載せる最大件数（名前の昇順）
AFTER_INGEST_MAX = 50
_AFTER_INGEST_NOTICE = ("取り込みの後に置かれた資料です。検索の索引（全文検索・意味の検索・グラフ）には入っていません。"
                        "中身は読む道具で開けます。")
_AFTER_INGEST_UNCHECKED_NOTICE = ("取り込みの後に置かれた資料は、時間内に確かめきれませんでした。"
                                  "フォルダを絞ると確かめられます。")


_after_ingest_walks: dict = {}  # 道具の子プロセスだけが使う（子は 1 回の依頼で終わる）


def _walk_after_ingest(world: str, targets: list, deadline: float) -> set | None:
    """`targets` の部分木の実ファイルを歩く。期限切れ・読めない部分があれば None。"""
    skipped: dict = {}
    try:
        rels = documents.scope_rel_set(world, targets, deadline=deadline, skipped=skipped)
    except scope_infer.ScopeWalkDeadlineExceeded:
        return None
    return None if documents.walk_incomplete(skipped) else rels


def _walk_after_ingest_once(world: str, targets: list, deadline: float) -> set | None:
    """名前の検索で台帳に無かったときの走査。道具の子プロセスの中では 1 回の依頼で 1 度だけ歩く。"""
    if not os.environ.get("SHERPA_MCP_WORLD"):
        return _walk_after_ingest(world, targets, deadline)
    key = (world, tuple(sorted(targets)))
    if key not in _after_ingest_walks:
        _after_ingest_walks[key] = _walk_after_ingest(world, targets, deadline)
    return _after_ingest_walks[key]


def _attach_after_ingest(result: dict, docs: set, extra: list | None, n: int) -> None:
    """台帳に無い実ファイルの一覧を道具の結果へ添える。確かめきれなかったときはその旨を添える。"""
    if extra is None:
        result["after_ingest_unchecked"] = True
        result["after_ingest_notice"] = _AFTER_INGEST_UNCHECKED_NOTICE
    elif extra:
        shown = extra[:AFTER_INGEST_MAX]
        result["after_ingest"] = shown
        if n > len(shown):
            result["after_ingest_truncated"] = True
            result["after_ingest_count"] = n
        result["after_ingest_notice"] = _AFTER_INGEST_NOTICE
        docs.update(d["rel_path"] for d in shown)


def _after_ingest(world: str, prefix: str, sp, layer, listed: set, pattern: str, doctype_filter: str,
                  deadline: float | None, rules, glob_pattern: str | None = None) -> tuple[list | None, int]:
    """台帳に無い実ファイルを `prefix` の部分木から歩いて `(全件を名前の昇順に並べた一覧, 件数)` で返す。期限切れ・読めない部分があったときは `(None, 0)`。
    守ること: 秘匿名・重要度の設定ファイル・版管理のフォルダは出さない（歩き方は `documents.scope_rel_set` と同じ）。中身は読まない。
    設計: docs/design/codex.md「MCP の道具」
    """
    if not prefix:
        targets = list(sp or [])
    elif sp:
        targets = []
        for s in sp:
            if s == prefix or s.startswith(prefix + "/"):
                targets.append(s)
            elif prefix.startswith(s + "/"):
                targets.append(prefix)
        targets = list(dict.fromkeys(targets))
        if not targets:
            return [], 0
    else:
        targets = [prefix]
    limit = time.monotonic() + AFTER_INGEST_WALK_SECONDS
    walk_deadline = limit if deadline is None else min(deadline, limit)
    rels = (_walk_after_ingest(world, targets, walk_deadline) if prefix
            else _walk_after_ingest_once(world, targets, walk_deadline))
    if rels is None:
        return None, 0
    out = []
    for rel in sorted(rels):
        if time.monotonic() > walk_deadline:
            return None, 0
        if rel in listed or text_kind.is_sensitive_doc_id(rel) or not scope_mod.in_scope(rel, sp):
            continue
        if rules.excluded(rel):
            continue
        cls = corpus_docs.classify_by_name(rel)
        doctype = cls[0] if cls else None
        if pattern and pattern not in rel.lower():
            continue
        if glob_pattern and not importance._match_segment_glob(glob_pattern, rel.lower()):
            continue
        if doctype_filter and str(doctype or "").lower() != doctype_filter:
            continue
        if not doc_reach.in_layer_confirmed(rel, world, layer):
            continue
        found = documents.resolve(rel, world)
        if found is None:
            continue
        try:
            st = found.stat()
        except OSError:
            continue
        out.append({"rel_path": rel, "doctype": doctype, "size": st.st_size,
                    "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))})
    if time.monotonic() > walk_deadline:
        return None, 0
    return out, len(out)


def list_docs(args: dict, world: str, sp, layer, deadline: float | None, rules) -> tuple[dict, set]:
    """`list_docs` を実行し `(結果, 出典に載せる rel_path 集合)` を返す。"""
    from ... import doc_ledger  # 最後の取り込みの台帳（無ければ資料フォルダを走査）
    docs: set = set()
    prefix = str(args.get("path_prefix") or "").strip().strip("/")
    pattern = str(args.get("name_pattern") or "").strip().lower()
    doctype_filter = str(args.get("doctype") or "").strip().lower()
    state_filter = str(args.get("state") or "").strip().lower()
    try:
        limit = int(args.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 500))  # `limit` は 1〜500 にクランプ
    try:
        offset = int(args.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)
    # 層判定は `doc_ledger`（`classify_document` 確定済み）の `branch=="source"` を使う（grep／ES と同じ確定判定）
    listing = doc_ledger.listing_documents(world, deadline=deadline, prefixes=[prefix] if prefix else sp)
    rows = [r for r in listing.rows
           if scope_mod.in_scope(r["name"], sp)
           and not rules.excluded(r["name"])
           and layer_mod.in_layer_row(r, layer)]
    if prefix:
        rows = [r for r in rows if scope_mod.in_scope(r["name"], [prefix])]  # 同じ prefix 一致ロジックを再利用
    if pattern:
        rows = [r for r in rows if pattern in r["name"].lower()]
    unknown_total = sum(1 for r in rows if doc_ledger.is_kind_unknown(r))  # 種類・状態の絞り込み前（落とした分も数える）
    if doctype_filter:
        rows = [r for r in rows if str(r.get("doctype") or "").lower() == doctype_filter]
    # state（"ready"／"unreadable"／"unknown"／"partial"）も通す（読み取り不可な文書を「使える」文書と同列に見せない）
    if state_filter:
        rows = [r for r in rows if str(r.get("state", "ready")).lower() == state_filter]
    rows.sort(key=lambda r: r["name"])  # rel_path 昇順固定（同条件なら offset が安定する）
    page = rows[offset:offset + limit]
    out = [{"rel_path": r["name"], "doctype": r.get("doctype"), "state": r.get("state", "ready")}
          for r in page]
    for d in out:
        docs.add(d["rel_path"])  # 一覧に出した分だけ出典（sources）に載せる
    shown_end = offset + len(out)
    truncated = shown_end < len(rows)
    result = {"count": len(rows), "offset": offset, "docs": out, "truncated": truncated,
              "next_offset": shown_end if truncated else None}
    if listing.ledger_at:
        result["ledger_at"] = listing.ledger_at  # 最後の取り込みの時点の一覧（以降の変更は含まない）
    partial_total = sum(1 for r in rows if r.get("state") == "partial")
    if partial_total:
        result["partial"] = partial_total
        result["partial_notice"] = doc_ledger.partial_notice(partial_total)
    if unknown_total:
        shown_unknown = sum(1 for r in rows if doc_ledger.is_kind_unknown(r))
        result["kind_unknown"] = shown_unknown
        result["kind_unknown_dropped"] = unknown_total - shown_unknown  # doctype／state の絞り込みで落とした分
        result["kind_unknown_notice"] = doc_ledger.kind_unknown_notice(unknown_total)
    # 取り込みの後に置かれた資料（フォルダで絞ったとき・名前で台帳に無かったときだけ歩く。state は台帳の状態なので指定時は対象外）
    if (prefix or (pattern and listing.ledger_at and not rows)) and not state_filter:
        listed = {r["name"] for r in listing.rows}
        extra, n = _after_ingest(world, prefix, sp, layer, listed, pattern, doctype_filter, deadline, rules)
        _attach_after_ingest(result, docs, extra, n)
    return result, docs


def _validate_glob_pattern(raw) -> str | None:
    """`glob_search` の `pattern` 引数を検証する（無効なら None）。`doc_id`（`_safe_doc_path`）と同じトラバーサル拒否（絶対パス・バックスラッシュ・NUL・`..`／空セグメント）を適用する。"""
    if not isinstance(raw, str):
        return None
    pattern = raw.strip()
    if not pattern or len(pattern) > GLOB_PATTERN_MAX_LEN:
        return None
    if pattern.startswith("/") or "\\" in pattern or "\x00" in pattern:
        return None
    parts = pattern.split("/")
    if ".." in parts or "" in parts:
        return None
    return pattern


def _glob_match_pattern(pattern: str) -> str:
    """スラッシュを含まないパターンは「どの階層のファイル名にも一致」とみなし `**/` を前置する（ripgrep の `--glob` と同じ慣習）。スラッシュを含むパターンはそのまま（資料フォルダ root からの絞り込み）。"""
    return pattern if "/" in pattern else f"**/{pattern}"


def _glob_generation(listing, rules) -> str:
    """続きの呼び出しで一覧の同一性を確かめる印。台帳から出したときは `ledger_at` に全行の（名前・種類・状態）と除外と範囲を広げる優先の規則のハッシュを足し、行の追加・更新・削除・規則の変更のどれでも変わる値にする。歩いたときは `walk`（除外があれば印を足す）。"""
    ex = rules.paging_signature()
    if not listing.ledger_at:
        return f"{GLOB_WALK_GENERATION}:{ex}" if ex else GLOB_WALK_GENERATION
    h = hashlib.sha1(ex.encode("utf-8"))
    for r in listing.rows:
        h.update(f"{r['name']}\0{r.get('doctype')}\0{r.get('branch')}\0{r.get('state')}\n".encode("utf-8"))
    return f"{listing.ledger_at}#{len(listing.rows)}:{h.hexdigest()[:12]}"


def _glob_offset(args: dict) -> int:
    try:
        return max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        return 0


def glob_canonical_args(args):
    """重複判定のキー用に、省略と同じ意味の引数（`offset: 0`・空の generation）を落とす。"""
    if not isinstance(args, dict):
        return args
    out = {}  # 検索が使う引数だけ（未定義の引数は無視する）
    if "pattern" in args:
        valid = _validate_glob_pattern(args["pattern"]) if isinstance(args["pattern"], str) else None
        out["pattern"] = _glob_match_pattern(valid).lower() if valid is not None else args["pattern"]  # 検索と同じ検証・照合の形
    if _glob_offset(args):
        out["offset"] = _glob_offset(args)
        if str(args.get("generation") or "").strip():
            out["generation"] = str(args["generation"]).strip()  # offset 0 の検索は generation を見ない
    return out


def glob_restart_args(args, result: dict) -> list:
    """世代の違いのエラーに対応する先頭ページの呼び出しの形（offset 0・generation なし・同じ pattern）。エラーでなければ空。"""
    if result.get("error") != GLOB_GENERATION_MISMATCH_ERROR or not isinstance(args, dict):
        return []
    return [{"pattern": args["pattern"]}] if "pattern" in args else []


def _fit_paths(result: dict, max_bytes: int | None) -> bool:
    """`max_bytes` を超えるとき、`paths` を後ろから削って収める。`count`・`generation` は残し、`next_offset` は offset＋残した件数にする。削ったら真。
    1 件も残せないときは、構造のままの予算不足のエラー（進まない `next_offset` は返さない）に置き換える。"""
    def _size(r: dict) -> int:
        return len(json.dumps(r, ensure_ascii=False).encode("utf-8"))

    paths = result.get("paths")
    if not max_bytes or not paths or _size(result) <= max_bytes:
        return False

    def _build(k: int) -> dict:
        return {**result, "paths": paths[:k], "truncated": True, "byte_clipped": True, "next_offset": result["offset"] + k}

    lo, hi, best = 1, len(paths) - 1, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _size(_build(mid)) <= max_bytes:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best == 0:
        offset = result["offset"]
        result.clear()
        result.update({"error": "tool_result_budget_too_small", "hint": _BUDGET_TOO_SMALL_HINT, "offset": offset, "byte_clipped": True})
        return True
    result.update(_build(best))
    return True


def glob_search(args: dict, world: str, sp, layer, deadline: float | None, rules, max_bytes: int | None = None) -> tuple[dict, set]:
    """`glob_search` を実行し `(結果, 出典に載せる rel_path 集合)` を返す。名前の昇順に `GLOB_MAX_RESULTS` 件ずつ、続きは `offset`＋前の応答の `generation` で取る。"""
    from ... import doc_ledger  # list_docs と同じ一覧の出所
    pattern = _validate_glob_pattern(args.get("pattern"))
    if pattern is None:
        return {"error": "pattern が不正です（絶対パス・`..`・NUL・空文字・長すぎるパターンは使えません）"}, set()
    match_pattern = _glob_match_pattern(pattern).lower()
    # 範囲・層は list_docs と同じ確定判定（`branch=="source"`）を使う
    listing = doc_ledger.listing_documents(world, deadline=deadline)
    matched_rows = [r for r in listing.rows
                    if scope_mod.in_scope(r["name"], sp)
                    and not rules.excluded(r["name"])
                    and layer_mod.in_layer_row(r, layer)
                    and importance._match_segment_glob(match_pattern, r["name"].lower())]
    matched_rows.sort(key=lambda r: r["name"])  # rel_path 昇順固定（同じ名前は無いので offset が安定する）
    offset = _glob_offset(args)
    generation = _glob_generation(listing, rules)
    given = str(args.get("generation") or "").strip()
    if offset > 0 and not given:
        return {"error": GLOB_GENERATION_REQUIRED_ERROR,
                "message": "続き（offset が 1 以上）には前の応答の generation が要ります。generation を付けて呼び直してください"}, set()
    if offset > 0 and given != generation:
        # 続きは同じ一覧の続きでなければ返さない（入れ替わった一覧の別の並びを黙って返さない）
        return {"error": GLOB_GENERATION_MISMATCH_ERROR,
                "message": "資料が入れ替わりました。offset 0 から取り直してください（generation を付けずに呼び直す）"}, set()
    unknown_matched = sum(1 for r in matched_rows if doc_ledger.is_kind_unknown(r))  # 全一致について数える
    shown = [r["name"] for r in matched_rows[offset:offset + GLOB_MAX_RESULTS]]
    shown_end = offset + len(shown)
    truncated = shown_end < len(matched_rows)
    result = {"count": len(matched_rows), "offset": offset, "paths": shown, "truncated": truncated,
              "next_offset": shown_end if truncated else None, "generation": generation}
    docs: set = set()
    if listing.ledger_at:
        result["ledger_at"] = listing.ledger_at
        if not matched_rows and offset == 0:  # 名前で台帳に無かったときだけ歩く
            listed = {r["name"] for r in listing.rows}
            extra, n = _after_ingest(world, "", sp, layer, listed, "", "", deadline, rules, glob_pattern=match_pattern)
            _attach_after_ingest(result, docs, extra, n)
    else:
        result["notice"] = _GLOB_WALK_NOTICE
    partial_paths = [r["name"] for r in matched_rows[offset:offset + GLOB_MAX_RESULTS] if r.get("state") == "partial"]
    if partial_paths:
        result["partial_paths"] = partial_paths
        result["partial_notice"] = doc_ledger.partial_notice(len(partial_paths))
    if unknown_matched:
        result["kind_unknown"] = unknown_matched
        result["kind_unknown_notice"] = doc_ledger.kind_unknown_notice(unknown_matched)
    if _fit_paths(result, max_bytes):
        shown = result.get("paths", [])  # 削った分は出典に載せない
    return result, docs | set(shown)  # 出した分だけ出典に載せる


def list_tool(name: str, args: dict, world: str, sp, layer, deadline: float | None, rules, max_bytes: int | None = None) -> tuple[dict, set]:
    """一覧系の道具（`list_docs`・`name_lookup`・`glob_search`）を実行し `(結果, 出典に載せる rel_path 集合)` を返す。"""
    if name == "name_lookup":
        return name_lookup.name_lookup(args, world, sp, layer, rules, max_bytes)
    if name == "glob_search":
        return glob_search(args, world, sp, layer, deadline, rules, max_bytes)
    return list_docs(args, world, sp, layer, deadline, rules)
