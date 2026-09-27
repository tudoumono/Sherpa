"""`sherpa/call_chain.py`（下り向きの鎖・SQL の目印）の単体テスト。Neo4j は問い合わせ関数の差し替えで代える。"""
from __future__ import annotations

from sherpa import call_chain


def test_downstream_chain_walks_files_once_and_reports_depth_limit(monkeypatch):
    graph = {   # ファイル → そこから出る辺（関数への呼び出しは定義ファイルへ届く）
        "a.c": [("INVOKES", "b.c"), ("INVOKES", "a.c")],
        "b.c": [("INVOKES", "c.c"), ("COPIES", "a.c")],
        "c.c": [("ACCESSES", "d.c")],
        "d.c": [("INVOKES", "e.c")],
    }

    def fake_run(session, cypher, *, world, **params):
        rows = []
        for src in params["paths"]:
            for typ, dst in graph.get(src, []):
                rows.append({"from_path": src, "type": typ, "via": "call", "doc": src, "line": 1,
                             "to_path": dst, "to_name": dst, "to_label": "Module"})
        return rows

    monkeypatch.setattr(call_chain.world_neo4j, "_run_read_capped", fake_run)
    chain = call_chain.downstream_chain(None, "w", ["a.c"], max_depth=3)
    assert chain["files"] == ["b.c", "c.c", "d.c"]
    assert [h["depth"] for h in chain["hops"] if h["to_path"] == "d.c"] == [3]
    assert not any(h["to_path"] == "a.c" and h["from_path"] == "a.c" for h in chain["hops"])
    assert chain["truncated_depth"] is True    # 3 段目の先（e.c）が残っている
    assert call_chain.downstream_chain(None, "w", ["a.c"], max_depth=8)["truncated_depth"] is False
    capped = call_chain.downstream_chain(None, "w", ["a.c"], max_files=2)
    assert capped["truncated_files"] is True and capped["files"] == ["b.c"]
    assert not any(h["to_path"] == "c.c" for h in capped["hops"])   # 上限の先は表示しない
    assert call_chain.downstream_chain(None, "w", ["c.c"], max_depth=2)["truncated_depth"] is False   # 最後の段が葉


def test_sql_markers_counts_embedded_sql_and_stays_inside_the_world(tmp_path):
    root = tmp_path / "kb"
    (root / "src").mkdir(parents=True)
    (root / "src" / "q.c").write_bytes(b"int f(){\n EXEC SQL DECLARE c CURSOR FOR\n  SELECT a\n  FROM t;\n}\n")
    (root / "src" / "long.sql").write_bytes(b"SELECT a,\n b,\n c,\n d,\n e\nFROM t;\n")
    (tmp_path / "outside.c").write_bytes(b"EXEC SQL SELECT 1 FROM x;\n")
    assert call_chain.sql_markers(root, "src/q.c") == {"readable": True, "exec_sql": 1, "select_from": 1}
    assert call_chain.sql_markers(root, "src/long.sql")["select_from"] == 1   # SELECT と FROM が離れていても数える
    assert call_chain.sql_markers(root, "../outside.c") == {"readable": False}
    assert call_chain.sql_markers(root, "src/missing.c") == {"readable": False}
