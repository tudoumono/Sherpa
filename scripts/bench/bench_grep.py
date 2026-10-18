"""使い方: python3 scripts/bench/bench_grep.py <corpus_dir> <label> <out_dir> [profile]
grep 実装別ベンチ: Sherpa の grep_search(純Python) / rg / 走査 (safe_files, documents_for) を wall と CPU で比べる。"""
import cProfile, io, json, os, pstats, resource, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
corpus = Path(sys.argv[1]); label = sys.argv[2]; out_dir = Path(sys.argv[3]); do_profile = len(sys.argv) > 4 and sys.argv[4] == "profile"
os.environ["SHERPA_MCP_WORLD"] = "bench"; os.environ["SHERPA_MCP_WORLD_ROOT"] = str(corpus.resolve())
os.environ["SHERPA_DERIVED_DIR"] = str(corpus.resolve().parent / f"derived-{label}")
from sherpa import grep_tool, scope_infer, doc_ledger, documents
gold = json.loads((corpus / "gold.json").read_text(encoding="utf-8"))
q = gold["questions"][0]; rare = q["start"]["item"]; cpyname = q["start"]["copybook"].rsplit("/",1)[-1][:-4]
queries = [("rare_item", rare), ("copybook_name", cpyname), ("common_token", "WS-COUNT"), ("ja_term", "消費税率")]
def cpu_self():
    r = resource.getrusage(resource.RUSAGE_SELF); return r.ru_utime + r.ru_stime
def cpu_child():
    r = resource.getrusage(resource.RUSAGE_CHILDREN); return r.ru_utime + r.ru_stime
def timed(fn, cpu=cpu_self):
    c0, t0 = cpu(), time.monotonic(); out = fn(); return time.monotonic() - t0, cpu() - c0, out
rows = []
def rec(**kw): rows.append(kw); print(json.dumps(kw, ensure_ascii=False), flush=True)
# 走査だけ
w, c, n = timed(lambda: sum(1 for _ in scope_infer.safe_files(corpus)))
rec(corpus=label, op="safe_files_walk", wall=round(w,3), cpu=round(c,3), n=n)
w, c, n = timed(lambda: len(doc_ledger.documents_for("bench")))
rec(corpus=label, op="documents_for(list_docs/glob)", wall=round(w,3), cpu=round(c,3), n=n)
w, c, n = timed(lambda: len(documents.world_rel_set("bench")))
rec(corpus=label, op="world_rel_set(es_search)", wall=round(w,3), cpu=round(c,3), n=n)
for qname, query in queries:
    # Sherpa 純Python（roots 指定＝重要度解決なし）
    w, c, hits = timed(lambda: grep_tool.grep_search(query, roots=[str(corpus)], max_hits=50))
    rec(corpus=label, op="grep_search(roots)", q=qname, wall=round(w,3), cpu=round(c,3), hits=len(hits))
    # Sherpa 純Python（world 指定＝本番経路: classify + importance 解決を含む）
    w, c, hits = timed(lambda: grep_tool.grep_search(query, world="bench", max_hits=50))
    rec(corpus=label, op="grep_search(world)", q=qname, wall=round(w,3), cpu=round(c,3), hits=len(hits))
    # ripgrep（固定文字列・大小無視・CP932 ファイルは UTF-8 のままだと ASCII 語は当たる）
    w, c, p = timed(lambda: subprocess.run(["rg", "-n", "-i", "-F", "--no-messages", "-j", "4", query, str(corpus)], capture_output=True, text=True, errors="replace"), cpu=cpu_child)
    rec(corpus=label, op="rg -j4", q=qname, wall=round(w,3), cpu=round(c,3), hits=len(p.stdout.splitlines()))
    w, c, p = timed(lambda: subprocess.run(["rg", "-n", "-i", "-F", "--no-messages", "-j", "1", query, str(corpus)], capture_output=True, text=True, errors="replace"), cpu=cpu_child)
    rec(corpus=label, op="rg -j1", q=qname, wall=round(w,3), cpu=round(c,3), hits=len(p.stdout.splitlines()))
if do_profile:
    pr = cProfile.Profile(); pr.enable(); grep_tool.grep_search(rare, world="bench", max_hits=50); pr.disable()
    s = io.StringIO(); pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(28); print(s.getvalue())
    s = io.StringIO(); pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(18); print(s.getvalue())
json.dump(rows, open(out_dir / f"grep_{label}.json", "w"), ensure_ascii=False, indent=1)
