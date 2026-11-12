"""名前から資料を引く道具 `name_lookup`（名前の台帳を引く）。
ソースの中に書かれた名前（`declared`）とファイル名（`filename`）のどちらからでも、その名前のソースを探せる。
一致が無いときは、資料フォルダの `name_rules` で置き換えた目標の形の一致（拡張子の有無は問わない・`via_rule` 付き）を出し、それも無ければ番号だけが違う近い名前を確認していない候補として出す。台帳だけを読む（グラフ・ES が不調でも動く）。
続きは `offset`（返す並び 1 つに効く）で取り、台帳の世代（`generation`）が変わったページは合わせない。
設計: docs/design/codex.md「MCP の道具」
"""
from __future__ import annotations

import hashlib
import json

from ... import layer as layer_mod
from ... import scope as scope_mod
from ...ingest import name_rows, resolve_settings, text_kind
from ...store import name_ledger

MATCH_MAX = 100
CANDIDATE_MAX = 20
_SOURCE_ORDER = {name_rows.SOURCE_DECLARED: 0, name_rows.SOURCE_FILENAME: 1, name_rows.SOURCE_BUILD: 2}
UNCONFIRMED_NOTICE = "確認していない候補です。同じものかは原本で確かめてください。"
_NOT_FOUND_NOTICE = ("名前の台帳（ソースの中に書かれた名前とファイル名）には無い名前です。"
                     "ソースが無いと答える前に、ripgrep_search で本文も確かめてください。")
GENERATION_MISMATCH_ERROR = "name_lookup_generation_mismatch"
GENERATION_REQUIRED_ERROR = "name_lookup_generation_required"
_BUDGET_TOO_SMALL_HINT = "1件も返せません。範囲を絞るか、1件あたりの上限を上げてください"
_GENERATION_NOTICE = "資料が入れ替わったので offset 0 から取り直してください（generation を付けずに呼び直す）。"
_EMPTY_NOTICE = "名前の台帳がまだ作られていません（取り込み直しが必要です）。ソースが無いとは言えません。"


def _visible(rows: list, sp, layer, kind: str, rules, prefix: str = "") -> list:
    """範囲・層・秘匿名・種類で絞る（一覧の道具と同じ範囲の規則）。`prefix` は範囲の内側をさらに狭めるだけで広げない。ファイル名の行は種類が分かるときだけ種類で絞る。"""
    return [r for r in rows
            if scope_mod.in_scope(r["rel_path"], sp)
            and (not prefix or scope_mod.in_scope(r["rel_path"], [prefix]))
            and not rules.excluded(r["rel_path"])
            and layer_mod.in_layer_code(True, layer)
            and not text_kind.is_sensitive_doc_id(r["rel_path"])
            and (not kind or not r["kind"] or r["kind"].lower() == kind)]


def _rules_sig(name_rules: list) -> str:
    """並び（via_rule）を決める `name_rules` を決まった形で並べて短くハッシュした印。規則が無ければ空文字。"""
    if not name_rules:
        return ""
    body = json.dumps([[r["from"], r["to"]] for r in name_rules], ensure_ascii=False)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]


def _row_key(r: dict) -> tuple:
    """同じ rel_path で source・kind が違う行も決まった順に並べる（offset を安定させる）。"""
    return (_SOURCE_ORDER.get(r["source"], 9), r["rel_path"], r["kind"] or "", r["name"])


def _view(r: dict) -> dict:
    return {"rel_path": r["rel_path"], "kind": r["kind"] or None, "source": r["source"]}


def _by_rules(world: str, name_rules: list, norm: str, sp, layer, kind: str, rules, prefix: str = "", conn=None) -> list:
    """資料フォルダの `name_rules` を上から当て、見える一致が出た最初の決まりの一致を `(行, 表示)` で返す（グラフと同じく最初の決まりで止める）。

    確認済みの一致と区別できるよう `via_rule` を付ける。
    """
    for rule in name_rules:
        out: list = []
        for mapped in map(name_rows.normalize_name, resolve_settings.map_target(norm, [rule])):
            found = [r for r in name_ledger.lookup_shape(world, mapped, conn) if resolve_settings.name_matches(mapped, r["name"])]
            rows = sorted(_visible(found, sp, layer, kind, rules, prefix), key=_row_key)
            out += [(r, {**_view(r), "via_rule": {"from": rule["from"], "to": rule["to"], "name": mapped}}) for r in rows]
        if out:
            return out
    return []


def _offset(args: dict) -> int:
    try:
        return max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        return 0


def _page(result: dict, offset: int, seq: list, limit: int, flag: str) -> list:
    """並び `seq` の `offset` から `limit` 件を返し、残りがあれば `flag`=true と `next_offset` を `result` に足す。"""
    page = seq[offset:offset + limit]
    more = offset + len(page) < len(seq)
    result[flag] = more
    if more:
        result["next_offset"] = offset + len(page)
    return page


def _fit(result: dict, max_bytes: int | None) -> bool:
    """`max_bytes` を超えるとき、`matches`（空なら `candidates`）を後ろから削って収める。`count`・`generation` は残し、`next_offset` は offset＋残した件数にする。削ったら真（`byte_clipped` を残す＝利用統計の `tool_result_clipped` が数える）。
    1 件も残せないときは、構造のままの予算不足のエラー（進まない `next_offset` は返さない）に置き換える。"""
    def _size(r: dict) -> int:
        return len(json.dumps(r, ensure_ascii=False).encode("utf-8"))

    key = "matches" if result.get("matches") else "candidates"
    items = result.get(key)
    if not max_bytes or not isinstance(items, list) or not items or _size(result) <= max_bytes:
        return False
    flag = "truncated" if key == "matches" else "candidates_truncated"

    def _build(k: int) -> dict:
        return {**result, key: items[:k], flag: True, "byte_clipped": True, "next_offset": result["offset"] + k}

    lo, hi, best = 1, len(items) - 1, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _size(_build(mid)) <= max_bytes:  # 足すものを全部含めた完成の形で測る
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


def canonical_args(args):
    """重複判定のキー用に、省略と同じ意味の引数（空の任意引数・`offset: 0`・generation なし）を落として正規化する。"""
    if not isinstance(args, dict):
        return args
    out = {}  # 検索が使う引数だけ（未定義の引数は無視する）
    for k in ("name", "kind", "path_prefix", "generation"):
        v = str(args[k]).strip() if args.get(k) is not None else ""
        v = v.strip("/") if k == "path_prefix" else v.lower() if k == "kind" else name_rows.normalize_name(v) if k == "name" else v
        if v:
            out[k] = v
    if _offset(args):
        out["offset"] = _offset(args)
    return out


def restart_args(args, result: dict) -> list:
    """世代の違いのエラーに対応する先頭ページの呼び出しの形（offset 0・generation なし・同じ name・kind・path_prefix）。エラーでなければ空。"""
    if result.get("error") != GENERATION_MISMATCH_ERROR or not isinstance(args, dict):
        return []
    first = {k: args[k] for k in ("name", "kind", "path_prefix") if k in args}
    return [first]


def name_lookup(args: dict, world: str, sp, layer, rules, max_bytes: int | None = None) -> tuple[dict, set]:
    """`name_lookup` を実行し `(結果, 出典に載せる rel_path 集合)` を返す。確認していない候補は出典に載せない。

    世代の確認から行の読み出しまでを同じスナップショットで行う（途中で台帳が入れ替わっても、応答の行と `generation` は同じ世代）。
    """
    if not str(args.get("name") or "").strip():
        return {"error": "name を指定してください"}, set()
    with name_ledger.snapshot() as conn:
        result, cites = _lookup(conn, args, world, sp, layer, rules)
    if _fit(result, max_bytes) and cites:
        cites = {m["rel_path"] for m in result.get("matches", [])}  # 削った分は出典に載せない
    return result, cites


def _lookup(conn, args: dict, world: str, sp, layer, rules) -> tuple[dict, set]:
    """① 引数と世代の検査 ② 一致（matches）③ 決まりによる一致（via_rule）④ 近い候補（candidates）の順に、最初に空でない並びを `offset` からページで返す。"""
    name = str(args.get("name") or "").strip()
    kind = str(args.get("kind") or "").strip().lower()
    prefix = str(args.get("path_prefix") or "").strip().strip("/")
    offset = _offset(args)
    name_rules = resolve_settings.load(world, conn).get("name_rules") or []
    generation = f"{name_ledger.built_at(world, conn) or 'none'}:{_rules_sig(name_rules)}"
    if rules.paging_signature():
        generation += f":{rules.paging_signature()}"  # 除外と範囲を広げる優先の規則が変わると並びが変わる
    given = str(args.get("generation") or "").strip()
    if offset > 0 and not given:
        return {"error": GENERATION_REQUIRED_ERROR,
                "notice": "offset を付けるときは前の応答の generation をそのまま渡してください。"}, set()
    if given and given != generation:
        return {"error": GENERATION_MISMATCH_ERROR, "notice": _GENERATION_NOTICE}, set()
    norm = name_rows.normalize_name(name)
    matches = sorted(_visible(name_ledger.lookup(world, norm, conn), sp, layer, kind, rules, prefix), key=_row_key)
    result: dict = {"name": name, "normalized": norm, "count": len(matches), "offset": offset, "generation": generation}
    if matches:
        shown = _page(result, offset, matches, MATCH_MAX, "truncated")
        result["matches"] = [_view(r) for r in shown]
        return result, {r["rel_path"] for r in shown}
    via_rule = _by_rules(world, name_rules, norm, sp, layer, kind, rules, prefix, conn)
    if via_rule:
        shown = _page(result, offset, via_rule, MATCH_MAX, "truncated")
        result.update({"count": len(via_rule), "matches": [m for _r, m in shown]})
        return result, {r["rel_path"] for r, _m in shown}
    key = name_rows.near_key(norm)
    near = sorted((r for r in _visible(name_ledger.like_prefix(world, key[0], key[1], conn), sp, layer, kind, rules, prefix)
                   if name_rows.is_near(norm, r["name"])), key=lambda r: (r["name"], *_row_key(r))) if key else []
    result["matches"] = []
    result["truncated"] = False
    shown = _page(result, offset, near, CANDIDATE_MAX, "candidates_truncated")
    result["candidates"] = [{"name": r["name"], **_view(r), "unconfirmed": True} for r in shown]
    if near:
        result["unconfirmed"] = True
        result["candidates_total"] = len(near)
        result["notice"] = UNCONFIRMED_NOTICE
    elif not name_ledger.is_built(world, conn):
        result["degrade_reason"] = "name_ledger_empty"
        result["notice"] = _EMPTY_NOTICE
    else:
        result["notice"] = _NOT_FOUND_NOTICE
    return result, set()
