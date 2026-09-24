"""起点のファイルから呼び出し・コピー・DB アクセスを下り向きにたどり、届いたファイルと SQL の候補を表示する
（`make graph-chain`・読み取り専用・本文は出さない）。

「読むべきファイルの一覧をグラフから作る」（docs/proposals/2026-09-25-回答のぶれを減らす.md S1a）の前提確認用:
実環境のグラフで、起点のソースから SQL を持つソースまで鎖が届くかを見る。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sherpa import call_chain, worlds  # noqa: E402

_TYPE_LABEL = {"INVOKES": "呼び出し", "COPIES": "コピー", "ACCESSES": "アクセス"}


def _pick_world(requested: str) -> str | None:
    if requested:
        return requested
    ids = list(worlds.discover_world_ids())
    if len(ids) == 1:
        return ids[0]
    print("取込ディレクトリを WORLD= で指定してください。候補: " + ("、".join(ids) if ids else "（なし）"),
          file=sys.stderr)
    return None


def _has_sql(m: dict | None) -> bool:
    return bool(m and m.get("readable") and (m["exec_sql"] or m["select_from"]))


def _sql_note(m: dict | None) -> str:
    if m is None:
        return ""
    if not m.get("readable"):
        return "  [SQL の有無を判定できません（読めないファイル）]"
    if not _has_sql(m):
        return ""
    return f"  [SQL の候補: EXEC SQL {m['exec_sql']} 行・SELECT〜FROM {m['select_from']} 箇所]"


def format_chain(world: str, chain: dict, markers: dict) -> list[str]:
    out = [f"起点: {', '.join(chain['start'])}"]
    seen_edges = set()
    cur = 0
    for h in chain["hops"]:
        key = (h["from_path"], h["type"], h["to_path"] or h["to_name"])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        if h["depth"] != cur:
            cur = h["depth"]
            out.append(f"深さ {cur}")
        to = h["to_path"] or f"（{h['to_label'] or '名前'}）{h['to_name']}"
        via = f"・{h['via']}" if h.get("via") else ""
        ref = f"  根拠 {h['doc']}:{h['line']}" if h.get("doc") else ""
        out.append(f"  {h['from_path']} → {to}（{_TYPE_LABEL.get(h['type'], h['type'])}{via}）{ref}"
                   f"{_sql_note(markers.get(h['to_path']))}")
    sql_files = [p for p in chain["files"] if _has_sql(markers.get(p))]
    unknown = [p for p in chain["start"] + chain["files"]
               if markers.get(p) is not None and not markers[p].get("readable")]
    out.append(f"届いたファイル: {len(chain['files'])} 件（SQL の候補があるファイル: {len(sql_files)} 件"
               + (f"・判定できないファイル: {len(unknown)} 件" if unknown else "") + "）")
    for p in sql_files:
        out.append(f"  SQL の候補: {p}")
    for p in chain["start"]:
        if _has_sql(markers.get(p)):
            out.append(f"  （起点自身にも SQL の候補: {p}）")
    if sql_files or any(_has_sql(markers.get(p)) for p in chain["start"]):
        out.append("  ※ SQL の候補は字句で数えた目印です（コメントの中の語も数えます）。中身は原本で確かめてください。")
    for p in unknown:
        out.append(f"  判定できない（読めない）: {p}")
    if chain.get("truncated_depth"):
        out.append("（段数の上限で打ち切りました。DEPTH を増やすと先まで見られます）")
    if chain.get("truncated_files"):
        out.append("（ファイル数の上限で打ち切りました。起点を絞ってください）")
    if not chain["hops"]:
        out.append("起点から下り向きの辺がありません（グラフに呼び出し・コピー・アクセスのつながりが無い）。")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="起点のファイルから呼び出し先の鎖をたどって表示する（読み取り専用）")
    ap.add_argument("--world", default="", help="取込ディレクトリ（1 つだけ登録されているなら省略可）")
    ap.add_argument("--from", dest="start", required=True, help="起点のファイル名かパス（グラフ上の名前でもよい）")
    ap.add_argument("--depth", type=int, default=call_chain.MAX_DEPTH, help="たどる段数の上限")
    args = ap.parse_args(argv)
    # 読むだけの道具なので、スキーマを整える init_schema()（DDL・移行・索引作成）は走らせない
    # （資料フォルダの解決と共有ロックが内部で _ensure() を呼ぶため・未初期化の DB は読み取りエラーで止まる）。
    from sherpa.store import db as _db
    _db._inited = True
    world = _pick_world(args.world)
    if not world:
        return 2
    from neo4j import GraphDatabase

    from sherpa.ingest import world_neo4j
    from sherpa.store.db import world_lock_shared
    env = world_neo4j._env()
    # 辺の種類が world に 1 本も無いときの「存在しない種類」の通知は画面に出さない（結果は変わらない）。
    driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]), notifications_min_severity="OFF")
    try:
        # 資料フォルダの付け替え・取り込みと重ならないよう共有ロックの中で、資料フォルダを固定して
        # グラフの照会と資料の走査を同じ世代で行う。
        with world_lock_shared(world):
            root = worlds.world_dir(world)
            if not root:
                print(f"取込ディレクトリの資料フォルダが見つかりません: {world}", file=sys.stderr)
                return 2
            with worlds.pin_world_root(world, root), driver.session() as s:
                starts = call_chain.resolve_start_paths(s, world, args.start)
                if len(starts) > call_chain.MAX_STARTS:
                    print(f"起点の候補が多すぎます（{len(starts)} 件）。ファイルのパスで指定してください。", file=sys.stderr)
                    return 1
                if not starts:
                    print(f"起点が見つかりません: {args.start}（ファイル名・パス・グラフ上の名前で指定してください）",
                          file=sys.stderr)
                    return 1
                chain = call_chain.downstream_chain(s, world, starts, max_depth=max(1, args.depth))
                try:
                    world_neo4j.check_schema_era(s, world)   # 古い世代のグラフを正しい結果として出さない
                except world_neo4j.GraphSchemaEraError as e:
                    print(f"グラフが古い形式のため表示できません（取り込み直しが必要です）: {e}", file=sys.stderr)
                    return 1
                markers = {p: call_chain.sql_markers(root, p) for p in chain["start"] + chain["files"]}
    finally:
        driver.close()
    print("\n".join(format_chain(world, chain, markers)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
