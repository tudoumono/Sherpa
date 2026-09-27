"""起点のファイルから、呼び出し・コピー・DB アクセスの辺を下り向きにたどって届くファイル（呼び出し先の鎖）と、
各ファイルの SQL の候補（字句で数えた目印）を返す（docs/proposals/2026-09-25-回答のぶれを減らす.md S1a/S1b）。

影響分析（`world_neo4j.world_impact`）は上り向き（変えたら何が影響を受けるか）。こちらは下り向き
（起点が実際に何を呼び、最後にどこで DB を読むか）。ファイル単位でたどる——関数（子ノード）への呼び出しは、
その関数を定義するファイルへ届いたものとして扱う（子ノードの `path`＝定義しているファイル）。
グラフに辺が無い呼び出し（関数ポインタ・マクロ・未解決の呼び出し）はたどれない＝届かなかったファイルは
「呼ばれていない」とは限らない。
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath

from .ingest import world_neo4j
from .safe_open import open_file_nofollow_walk

_CHAIN_REL = "INVOKES|COPIES|ACCESSES"
MAX_DEPTH = 8
MAX_FILES = 300
MAX_STARTS = 20   # 起点の候補がこれより多いときは曖昧＝パスでの指定を求める

_EXEC_SQL = re.compile(rb"\bEXEC\s+SQL\b", re.IGNORECASE)
_SELECT = re.compile(rb"\bSELECT\b", re.IGNORECASE)
_FROM = re.compile(rb"\bFROM\b", re.IGNORECASE)
_SELECT_WINDOW = 40   # SELECT の後、文の終わり（;）か FROM までを見る行数の上限


def resolve_start_paths(session, world_id: str, term: str, scope_prefixes=None) -> list[str]:
    """起点語（ファイル名・パス・グラフ上の名前）→ 起点のファイルパス群（範囲内・重複なし）。"""
    rows = world_neo4j._run_read_capped(
        session,
        "MATCH (n:Entity {world_id:$w}) "
        "WHERE (n.name=$term OR n.path=$term OR n.path ENDS WITH ('/' + $term)) "
        "  AND coalesce(n.status,'active')='active' "
        f"  AND {world_neo4j._scope_pred('n')} "
        "RETURN DISTINCT n.path AS path",
        world=world_id, w=world_id, term=term, prefixes=list(scope_prefixes or []))
    return sorted({r["path"] for r in rows if r.get("path")})


def downstream_chain(session, world_id: str, start_paths, scope_prefixes=None,
                     max_depth: int = MAX_DEPTH, max_files: int = MAX_FILES) -> dict:
    """起点のファイル群から下り向きに幅優先でたどる。1 段ごとに 1 回問い合わせる（同じファイルは 2 度たどらない）。

    返り値: `hops`（段・元ファイル・辺の種類・via・根拠の doc:line・届いた先のパス/名前/種類）、`files`（届いた
    ファイル・起点を除く）、`truncated_depth`（段数の上限でまだ先があった）・`truncated_files`（ファイル数の上限で
    たどらなかった先があった）。
    """
    start = [p for p in dict.fromkeys(start_paths) if p]
    seen = set(start)
    frontier = list(start)
    hops: list[dict] = []
    truncated_files = False
    truncated_depth = False
    prefixes = list(scope_prefixes or [])

    def _out_rows(paths):
        rows = world_neo4j._run_read_capped(
            session,
            "MATCH (src:Entity) WHERE src.world_id=$world AND src.path IN $paths "
            "  AND coalesce(src.status,'active')='active' "
            f"MATCH (src)-[e:{_CHAIN_REL}]->(t:Entity) "
            "WHERE t.world_id=$world AND coalesce(e.status,'active')='active' "
            "  AND coalesce(t.status,'active')='active' "
            f"  AND {world_neo4j._scope_pred('t')} "
            "RETURN src.path AS from_path, type(e) AS type, e.via AS via, e.doc AS doc, e.line AS line, "
            "  t.path AS to_path, t.name AS to_name, [l IN labels(t) WHERE l<>'Entity'][0] AS to_label "
            "ORDER BY to_path, from_path, type, line",   # 上限で打ち切るときも毎回同じファイルを残す
            world=world_id, paths=paths, prefixes=prefixes)
        # 同じファイルの中の呼び出し・定義は鎖に数えない
        return [r for r in rows if r.get("to_path") != r.get("from_path")]

    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        nxt: list[str] = []
        for r in _out_rows(frontier):
            to_path = r.get("to_path")
            if to_path and to_path not in seen:
                if len(seen) >= max_files:
                    truncated_files = True
                    continue                  # 上限の先はたどらず、表示もしない
                seen.add(to_path)
                nxt.append(to_path)
            hops.append({"depth": depth, "from_path": r.get("from_path"), "type": r.get("type"),
                         "via": r.get("via"), "doc": r.get("doc"), "line": r.get("line"),
                         "to_path": to_path, "to_name": r.get("to_name"), "to_label": r.get("to_label")})
        frontier = nxt
    else:
        # 段数の上限まで来た: 最後の段のファイルから、まだ下り向きの辺が出ているときだけ打ち切りとする
        truncated_depth = bool(frontier) and bool(_out_rows(frontier))
    return {"start": start, "hops": hops, "files": sorted(seen - set(start)),
            "truncated_depth": truncated_depth, "truncated_files": truncated_files}


def sql_markers(root, rel_path: str) -> dict:
    """資料フォルダ `root` のファイルの SQL の候補（`EXEC SQL` の行数・`SELECT` の後に `FROM` がある箇所数）。
    字句で数えるだけ（コメントや文字列の中の語も数える＝SQL を持つとは断定しない）。ファイル全体を 1 行ずつ読む
    （本文は返さない＝数だけ）。

    資料フォルダの外・無いファイル・読めないファイルは `{"readable": False}`（「SQL なし」と区別する）。
    """
    rel = PurePosixPath(str(rel_path or ""))
    parts = rel.parts
    if not parts or rel.is_absolute() or any(x in ("", ".", "..") for x in parts):
        return {"readable": False}
    exec_sql = select_from = pending = 0
    fd = None
    try:
        # 途中のディレクトリを symlink に差し替えられても資料フォルダの外を読まない（1 段ずつ O_NOFOLLOW）
        fd = open_file_nofollow_walk(Path(root), tuple(parts))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {"readable": False}
        with os.fdopen(fd, "rb") as fh:
            fd = None
            for ln in fh:
                if _EXEC_SQL.search(ln):
                    exec_sql += 1
                m = _SELECT.search(ln)
                if m:
                    if _FROM.search(ln, m.end()):
                        select_from += 1
                        pending = 0
                    else:
                        pending = _SELECT_WINDOW
                elif pending:
                    if _FROM.search(ln):
                        select_from += 1
                        pending = 0
                    elif b";" in ln:
                        pending = 0
                    else:
                        pending -= 1
    except (OSError, ValueError):
        return {"readable": False}
    finally:
        if fd is not None:
            os.close(fd)
    return {"readable": True, "exec_sql": exec_sql, "select_from": select_from}
