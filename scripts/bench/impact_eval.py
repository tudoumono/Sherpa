"""使い方: python3 scripts/bench/impact_eval.py <corpus_dir> <label> <out_dir>
影響範囲の解き方の比較: (A) キーワード検索（rg で項目名） (B) 静的グラフの逆向き閉包（LLM なし） (C) A∪B。gold.json と突き合わせ Recall/Precision/時間を出す。"""
import json, resource, subprocess, sys, time
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sherpa.ingest import world_graph
corpus = Path(sys.argv[1]); label = sys.argv[2]; out_dir = Path(sys.argv[3])
gold = json.loads((corpus / "gold.json").read_text(encoding="utf-8"))
def cpu(): r = resource.getrusage(resource.RUSAGE_SELF); return r.ru_utime + r.ru_stime
t0, c0 = time.monotonic(), cpu()
nodes, edges, flags = world_graph.build_world(corpus, "bench")
build_wall, build_cpu = time.monotonic() - t0, cpu() - c0
by_cid = {n["cid"]: n for n in nodes}
IMPACT = {"COPIES", "CONTAINS", "INVOKES", "ACCESSES"}
rev = defaultdict(set)      # dst -> {src}  （src が dst に依存）
docs_of = defaultdict(set)  # target cid -> {document path}
for e in edges:
    if e["type"] in IMPACT:
        rev[e["dst"]].add(e["src"])
    elif e["type"] == "DOCUMENTS":
        docs_of[e["dst"]].add(by_cid[e["src"]]["path"] if e["src"] in by_cid else e["src"])
def closure(starts):
    seen = set(starts); stack = list(starts)
    while stack:
        x = stack.pop()
        for y in rev.get(x, ()):
            if y not in seen:
                seen.add(y)
                stack.append(y)
    return seen
def prf(pred, gold_set):
    pred, gold_set = set(pred), set(gold_set)
    tp = len(pred & gold_set)
    return (tp / len(gold_set) if gold_set else 1.0, tp / len(pred) if pred else 1.0, len(pred), len(gold_set))
rows = []
for q in gold["questions"]:
    cpy_path, item = q["start"]["copybook"], q["start"]["item"]
    g = q["gold"]; gold_pgms = set(g["programs_direct"]) | set(g["programs_indirect"]); gold_all = gold_pgms | set(g["jobs"]) | set(g["docs"])
    # (A) キーワード: rg で項目名を含むファイル
    t = time.monotonic()
    p = subprocess.run(["rg", "-l", "-F", "-i", item, str(corpus)], capture_output=True, text=True)
    kw_files = {str(Path(l).relative_to(corpus)) for l in p.stdout.splitlines()} - {cpy_path}
    kw_wall = time.monotonic() - t
    # (B) グラフ閉包: 項目ノード（DataItem）と、その親コピーブックノードを起点に逆向きにたどる
    t = time.monotonic()
    starts = [n["cid"] for n in nodes if n.get("path") == cpy_path and n.get("name") == item]
    if not starts:  # DataItem が無ければコピーブック主体
        starts = [n["cid"] for n in nodes if n.get("path") == cpy_path and n["label"] == "Copybook"]
    cl = closure(starts)
    g_files = {by_cid[c]["path"] for c in cl if c in by_cid} - {cpy_path}
    g_docs = set().union(*(docs_of.get(c, set()) for c in cl)) if cl else set()
    g_all = g_files | g_docs
    graph_wall = time.monotonic() - t
    rows.append({"q": q["id"], "kw": prf(kw_files, gold_all), "kw_pgms": prf({f for f in kw_files if f.endswith('.cbl')}, gold_pgms),
                 "graph": prf(g_all, gold_all), "graph_pgms": prf(g_files & {f for f in g_files if f.endswith('.cbl')}, gold_pgms),
                 "graph_jobs": prf({f for f in g_files if f.endswith('.jcl')}, set(g["jobs"])), "graph_docs": prf(g_docs, set(g["docs"])),
                 "union": prf(kw_files | g_all, gold_all), "kw_wall": kw_wall, "graph_wall": graph_wall,
                 "gold_n": {"direct": len(g["programs_direct"]), "indirect": len(g["programs_indirect"]), "jobs": len(g["jobs"]), "docs": len(g["docs"])}})
import statistics as st
def avg(key, i): return round(st.mean(r[key][i] for r in rows), 3)
print(f"[{label}] build_world wall={build_wall:.1f}s cpu={build_cpu:.1f}s nodes={len(nodes)} edges={len(edges)} questions={len(rows)}")
print(f"  gold avg per question: direct={st.mean(r['gold_n']['direct'] for r in rows):.1f} indirect={st.mean(r['gold_n']['indirect'] for r in rows):.1f} jobs={st.mean(r['gold_n']['jobs'] for r in rows):.1f} docs={st.mean(r['gold_n']['docs'] for r in rows):.1f}")
for k in ("kw", "kw_pgms", "graph", "graph_pgms", "graph_jobs", "graph_docs", "union"):
    print(f"  {k:<11} recall={avg(k,0):.3f} precision={avg(k,1):.3f}")
print(f"  per-question wall: keyword(rg)={st.mean(r['kw_wall'] for r in rows)*1000:.0f}ms graph_closure={st.mean(r['graph_wall'] for r in rows)*1000:.2f}ms")
zero = sum(1 for r in rows if r["graph"][0] < 1.0); print(f"  questions with graph recall<1: {zero}")
json.dump({"build": {"wall": build_wall, "cpu": build_cpu}, "rows": rows}, open(out_dir / f"impact_{label}.json", "w"), ensure_ascii=False, indent=1)
