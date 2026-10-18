"""使い方: python3 scripts/bench/proto_grep_rg.py <corpus_dir> <query>...
試作: rg で候補を取り、必要件数に達したら rg を止める（全文を Python で走査しない）。
ASCII の検索語は 1 回（CP932 でも ASCII 部分は同じバイト）。非 ASCII を含む語だけ -E sjis の 2 回目を走らせる。"""
import json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sherpa import text_encoding

def rg_stream(query, root, need, encoding=None):
    cmd = ["rg", "--json", "-i", "-F", "--no-messages", "-j", "1", "--sort", "path", "--max-columns", "4000"]
    if encoding:
        cmd += ["-E", encoding]
    cmd += [query, str(root)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
    hits = []
    try:
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "match":
                continue
            d = ev["data"]; path = d["path"].get("text")
            if path is None:
                continue
            hits.append((path, d["line_number"], (d["lines"].get("text") or "").rstrip("\n")))
            if len(hits) >= need:
                break
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()
    return hits

def grep_rg(query, root, max_hits=45, offset=0):
    root = Path(root); need = max_hits + offset
    passes = [("utf-8", rg_stream(query, root, need))]
    if any(ord(c) > 127 for c in query):
        passes.append(("cp932", rg_stream(query, root, need, encoding="sjis")))
    by_file = {}
    for enc_label, src in passes:
        for path, ln, text in src:
            by_file.setdefault(path, {}).setdefault(enc_label, []).append((ln, text))
    out = []
    for path in sorted(by_file):
        if len(passes) > 1:
            with open(path, "rb") as f:
                enc = text_encoding.detect_fd(f.fileno()).lower()
            use = "cp932" if enc in ("cp932", "shift_jis", "sjis") else "utf-8"
            rows = by_file[path].get(use) or next(iter(by_file[path].values()))
        else:
            rows = by_file[path]["utf-8"]
        for ln, text in rows:
            out.append({"doc_id": str(Path(path).relative_to(root)), "line": ln, "text": text, "match": query})
    return out[offset:offset+max_hits], len(out)

if __name__ == "__main__":
    root = sys.argv[1]
    for q in sys.argv[2:]:
        t=time.monotonic(); hits, total = grep_rg(q, root); print(f"grep_rg {q!r}: {time.monotonic()-t:.3f}s shown={len(hits)} collected={total}")
