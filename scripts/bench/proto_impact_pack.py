"""使い方: python3 scripts/bench/proto_impact_pack.py <label> <bench_dir>（<bench_dir>/graph-<label>.json と <bench_dir>/corpora/<label>/gold.json を読む）
試作: 影響範囲の「根拠パック」。起点（コピーブック項目）から構造辺を逆向きにたどり、
影響先ごとに根拠行（COPY/CALL/EXEC 文の path:line）・段数・経路を機械的に付けて 1 回で返す。
LLM に N 回読ませる代わりに、静的解析の根拠をそのまま台帳の evidence にできる形で出す。"""
import json, sys, time
from collections import defaultdict, deque
from pathlib import Path

def load_graph(path):
    g = json.load(open(path)); return g["nodes"], g["edges"]

IMPACT = {"COPIES", "CONTAINS", "INVOKES", "ACCESSES"}

class Pack:
    def __init__(self, nodes, edges):
        self.by_cid = {n["cid"]: n for n in nodes}
        self.rev = defaultdict(list)   # dst -> [(src, edge)]
        self.docs_of = defaultdict(set)
        for e in edges:
            if e["type"] in IMPACT:
                self.rev[e["dst"]].append((e["src"], e))
            elif e["type"] == "DOCUMENTS":
                self.docs_of[e["dst"]].add(e["src"])
    def build(self, start_cids, max_depth=1000):
        # BFS（最短段数）。各影響先に「根拠となる辺」（参照している側のファイルの path:line）を 1 本以上付ける。
        dist = {c: 0 for c in start_cids}; via = defaultdict(list); q = deque(start_cids); truncated = False
        while q:
            x = q.popleft()
            if dist[x] >= max_depth:
                truncated = True
                continue
            for src, e in self.rev.get(x, ()):
                via[src].append({"edge": e["type"], "through": self.by_cid.get(x, {}).get("name"), "path": e.get("doc"), "line": e.get("line")})
                if src not in dist:
                    dist[src] = dist[x] + 1; q.append(src)
        affected = []
        for cid, d in dist.items():
            if d == 0:
                continue
            n = self.by_cid.get(cid)
            if not n:
                continue
            affected.append({"path": n["path"], "label": n["label"], "name": n["name"], "depth": d,
                             "evidence": via[cid][:3], "evidence_total": len(via[cid])})
        affected.sort(key=lambda a: (a["depth"], a["path"]))
        docs = set()
        for cid in dist:
            docs |= self.docs_of.get(cid, set())
        related_docs = sorted(self.by_cid[c]["path"] for c in docs if c in self.by_cid)
        by_kind = defaultdict(list)
        for a in affected:
            by_kind[a["label"]].append(a)
        summary = {k: {"count": len(v), "max_depth": max(a["depth"] for a in v)} for k, v in by_kind.items()}
        # 台帳 item の自動生成: 影響先 1 件＝1 item。根拠は静的解析の辺の path:line（ソースの行そのもの）。
        ledger_items = [{"id": f"imp-{i:05d}", "kind": "impact", "subject": a["path"], "required_checks": ["source"],
                         "evidence": [{"kind": "source", "path": ev["path"], "line": ev["line"]} for ev in a["evidence"][:1]],
                         "status": "source_confirmed", "reason": "", "owner": "parent"} for i, a in enumerate(affected)]
        return {"summary": summary, "affected": affected, "related_docs": related_docs, "truncated": truncated, "ledger_items": ledger_items}

if __name__ == "__main__":
    label = sys.argv[1]; base = Path(sys.argv[2])
    nodes, edges = load_graph(base / f"graph-{label}.json")
    gold = json.loads((base / "corpora" / label / "gold.json").read_text())
    t = time.monotonic(); pk = Pack(nodes, edges); print(f"[{label}] index build {time.monotonic()-t:.2f}s")
    sizes = []; recalls = []; walls = []
    for q in gold["questions"]:
        cp, item = q["start"]["copybook"], q["start"]["item"]
        starts = [n["cid"] for n in nodes if n.get("path") == cp and n.get("name") == item] or [n["cid"] for n in nodes if n.get("path") == cp and n["label"] == "Copybook"]
        t = time.monotonic(); res = pk.build(starts); walls.append(time.monotonic() - t)
        full = json.dumps({k: v for k, v in res.items() if k != "ledger_items"}, ensure_ascii=False)
        compact = json.dumps({"summary": res["summary"], "affected": [[a["path"], a["depth"], a["evidence"][0]["path"], a["evidence"][0]["line"]] for a in res["affected"]], "related_docs": res["related_docs"]}, ensure_ascii=False)
        sizes.append((len(full.encode()), len(compact.encode())))
        g = q["gold"]; gold_all = set(g["programs_direct"]) | set(g["programs_indirect"]) | set(g["jobs"]) | set(g["docs"])
        pred = {a["path"] for a in res["affected"]} | set(res["related_docs"])
        recalls.append(len(pred & gold_all) / len(gold_all))
    import statistics as st
    print(f"  recall={st.mean(recalls):.3f} wall/question={st.mean(walls)*1000:.1f}ms  pack bytes: full median={st.median(s[0] for s in sizes)/1024:.0f}KB max={max(s[0] for s in sizes)/1024:.0f}KB | compact median={st.median(s[1] for s in sizes)/1024:.0f}KB max={max(s[1] for s in sizes)/1024:.0f}KB (≈tokens: compact median {st.median(s[1] for s in sizes)/3:.0f})")
    print("  例（先頭1件）:", json.dumps(res["affected"][0], ensure_ascii=False)[:300])
    print("  台帳 item 例:", json.dumps(res["ledger_items"][0], ensure_ascii=False)[:300])
