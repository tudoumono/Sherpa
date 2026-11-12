"""`world_graph.build_world` の出力を、順序に依らない決定的な形に正規化する補助（グラフの回帰試験の土台）。

試験（test_world_graph_golden.py）と差分の道具（scripts/graph_golden.py）が共用する。test_ 接頭辞でないので収集されない。

- ノードは cid 順・エッジは (src, type, dst, 全体の JSON) 順・flags は全フィールドの JSON 順に並べる。
- 辞書のキーはソート。集合（`__set__`）・タプル（`__tuple__`）は型を残す（型が変わったら差として出す）。集合はソートし、リストとタプルは順序を保つ。
- 時刻・絶対パスは今のところ出力に含まれない。万一資料フォルダの絶対パスが文字列に入っても `<WORLD_DIR>` に置き換える。
"""
from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "fixtures" / "corpus"
GOLDEN_DIR = Path(__file__).resolve().parent / "goldens" / "world_graph"
RESOLVE_DIR = GOLDEN_DIR / "_resolve"
WORLD_ID = "golden_fixed"


def corpus_names() -> list[str]:
    return sorted(p.name for p in CORPUS.iterdir() if p.is_dir())


def load_resolve_config(name: str):
    """資料フォルダごとの解決の設定。`_resolve/<name>.json` があれば取り込みと同じ正規化（`resolve_settings.normalize`）で読む。"""
    f = RESOLVE_DIR / f"{name}.json"
    if not f.is_file():
        return None
    from sherpa.ingest import resolve_settings
    return resolve_settings.normalize(json.loads(f.read_text(encoding="utf-8")))


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _norm(obj, wd: str):
    if isinstance(obj, str):
        return str(obj).replace(wd, "<WORLD_DIR>") if wd else str(obj)
    if isinstance(obj, dict):
        return {str(k): _norm(v, wd) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (set, frozenset)):
        return {"__set__": sorted((_norm(v, wd) for v in obj), key=_canon)}
    if isinstance(obj, tuple):
        return {"__tuple__": [_norm(v, wd) for v in obj]}
    if isinstance(obj, list):
        return [_norm(v, wd) for v in obj]
    return obj


def normalize(nodes, edges, flags, world_dir=None) -> dict:
    wd = str(Path(world_dir).resolve()) if world_dir else ""
    n = sorted((_norm(x, wd) for x in nodes), key=lambda x: x["cid"])
    e = sorted((_norm(x, wd) for x in edges),
               key=lambda x: (x["src"], x["type"], x["dst"], _canon(x)))
    f = sorted((_norm(x, wd) for x in flags), key=_canon)
    return {"nodes": n, "edges": e, "flags": f}


def canonical_json(graph: dict) -> str:
    return _canon(graph)


def digest(graph: dict) -> str:
    return hashlib.sha256(canonical_json(graph).encode("utf-8")).hexdigest()


def counts(graph: dict) -> dict:
    return {
        "nodes": len(graph["nodes"]),
        "node_labels": dict(sorted(Counter(x["label"] for x in graph["nodes"]).items())),
        "edges": len(graph["edges"]),
        "edge_types": dict(sorted(Counter(x["type"] for x in graph["edges"]).items())),
        "flags": len(graph["flags"]),
        "flag_reasons": dict(sorted(Counter(str(x.get("reason")) for x in graph["flags"]).items())),
    }


def build_graph(name: str, corpus: Path = CORPUS) -> dict:
    """`corpus/<name>` を取り込みと同じ呼び方（解決の設定つき）で `build_world` に通して正規化する。"""
    from sherpa.ingest import world_graph
    wd = corpus / name
    nodes, edges, flags = world_graph.build_world(wd, WORLD_ID, resolve_config=load_resolve_config(name))
    return normalize(nodes, edges, flags, wd)


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def graph_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.graph.json.gz"


def write_golden(name: str, graph: dict) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    meta = {"world": name, "sha256": digest(graph), "counts": counts(graph)}
    golden_path(name).write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with open(graph_path(name), "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(canonical_json(graph).encode("utf-8"))


def read_golden(name: str) -> dict:
    return json.loads(golden_path(name).read_text(encoding="utf-8"))


def read_graph(name: str) -> dict | None:
    p = graph_path(name)
    if not p.is_file():
        return None
    with gzip.open(p, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def diff_counts(old: dict, new: dict) -> list[str]:
    """件数の差を「何が何件増減したか」の行にする。"""
    rows: list[str] = []
    for k in ("nodes", "edges", "flags"):
        if old[k] != new[k]:
            rows.append(f"{k}: {old[k]} -> {new[k]}")
    for k in ("node_labels", "edge_types", "flag_reasons"):
        for key in sorted(set(old[k]) | set(new[k])):
            a, b = old[k].get(key, 0), new[k].get(key, 0)
            if a != b:
                rows.append(f"{k}[{key}]: {a} -> {b} ({b - a:+d})")
    return rows


def _key_of(kind: str, x: dict) -> str:
    return x["cid"] if kind == "nodes" else _canon(x)


def diff_graphs(old: dict, new: dict, limit: int = 5) -> list[str]:
    """ノード（cid 単位）・エッジ・flags の差分の先頭 `limit` 件ずつ。"""
    rows: list[str] = []
    for kind in ("nodes", "edges", "flags"):
        o = {_key_of(kind, x): x for x in old[kind]}
        n = {_key_of(kind, x): x for x in new[kind]}
        removed = sorted(set(o) - set(n))
        added = sorted(set(n) - set(o))
        changed = sorted(k for k in set(o) & set(n) if o[k] != n[k]) if kind == "nodes" else []
        for tag, keys, src in (("-", removed, o), ("+", added, n)):
            for k in keys[:limit]:
                rows.append(f"{kind} {tag} {k if kind != 'nodes' else _canon(src[k])}")
            if len(keys) > limit:
                rows.append(f"{kind} {tag} ... 他 {len(keys) - limit} 件")
        for k in changed[:limit]:
            fields = sorted(f for f in set(o[k]) | set(n[k]) if o[k].get(f) != n[k].get(f))
            rows.append(f"{kind} ~ {k} fields={fields}: {_canon({f: o[k].get(f) for f in fields})} -> "
                        f"{_canon({f: n[k].get(f) for f in fields})}")
        if len(changed) > limit:
            rows.append(f"{kind} ~ ... 他 {len(changed) - limit} 件")
    return rows
