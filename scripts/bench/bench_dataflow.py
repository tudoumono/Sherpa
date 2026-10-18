"""合成コーパスのグラフ構築時間とデータの流れの辺数を計測する。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sherpa.ingest.world_graph import build_world


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not args.corpus.is_dir():
        parser.error("corpus は既存のフォルダを指定してください")
    files = sum(p.is_file() and p.name != "gold.json" for p in args.corpus.rglob("*"))
    if not files:
        parser.error("corpus にファイルがありません")
    started = time.perf_counter()
    nodes, edges, flags = build_world(args.corpus, "bench_dataflow")
    elapsed = time.perf_counter() - started
    result = {"files": files, "seconds": elapsed, "ms_per_file": elapsed * 1000 / files,
              "nodes": len(nodes), "edges": len(edges),
              "flow_edges": sum(e["type"] == "FLOWS_TO" for e in edges), "flags": len(flags)}
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["ms_per_file"] <= 100 else 1


if __name__ == "__main__":
    raise SystemExit(main())
