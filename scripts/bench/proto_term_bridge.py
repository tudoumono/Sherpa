"""試作: 設計書の表から「業務語（日本語ラベル）↔ 識別子」の橋を決定的に作り、日本語の問いを識別子→グラフ閉包へ落とす。
使い方: python3 scripts/bench/proto_term_bridge.py <derived_rag_dir> <graph.json> <日本語の語>...
材料は取り込みが既に作っている rag_chunks.jsonl の identifier_mentions（役割 record_identity）と同じレコードの他のラベル値。
"""
import gzip
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,}$")


def norm_id(s: str) -> str:
    return s.upper().replace("_", "-")


def load_chunks(path: Path):
    raw = path.read_bytes()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def build_aliases(rag_dir: Path) -> dict:
    """識別子（正規化）→ {日本語ラベル値} を、同じレコードに同居する値から作る。"""
    aliases = defaultdict(set)
    for p in rag_dir.rglob("*.rag_chunks.jsonl"):
        for ch in load_chunks(p):
            ids = {norm_id(m["value"]) for m in ch.get("identifier_mentions") or []
                   if m.get("role") == "record_identity" and ID_RE.match(m.get("value") or "")}
            if not ids:
                continue
            # 同じレコードの「ラベル: 「値」」の行から日本語の値を拾う
            for line in (ch.get("body") or "").splitlines():
                m = re.match(r"^(.+?):\s*「?(.+?)」?$", line.strip())
                if not m:
                    continue
                val = m.group(2).strip()
                if re.search(r"[ぁ-んァ-ン一-龥]", val):
                    for i in ids:
                        aliases[i].add(val)
    return aliases


def main() -> int:
    rag_dir, graph_path, terms = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
    aliases = build_aliases(rag_dir)
    print(f"橋の数: 識別子 {len(aliases)} 件 / 日本語ラベル {sum(len(v) for v in aliases.values())} 件")
    for k, v in list(aliases.items())[:6]:
        print("  ", k, "<-", sorted(v))
    g = json.loads(graph_path.read_text(encoding="utf-8"))
    by_cid = {n["cid"]: n for n in g["nodes"]}
    by_name = defaultdict(list)
    for n in g["nodes"]:
        by_name[norm_id(n["name"])].append(n["cid"])
    rev = defaultdict(set)
    for e in g["edges"]:
        if e["type"] in ("COPIES", "CONTAINS", "INVOKES", "ACCESSES"):
            rev[e["dst"]].add(e["src"])
    term_to_ids = defaultdict(set)
    for i, labels in aliases.items():
        for lab in labels:
            term_to_ids[lab].add(i)
    for term in terms:
        ids = {i for lab, s in term_to_ids.items() if term in lab for i in s}
        starts = [c for i in ids for c in by_name.get(i, [])]
        seen = set(starts)
        dq = deque(starts)
        while dq:
            x = dq.popleft()
            for y in rev.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    dq.append(y)
        files = sorted({by_cid[c]["path"] for c in seen if c in by_cid})
        print(f"問い「{term}」 → 識別子 {sorted(ids)} → 影響先 {len(files)} 件: {[f.split('/')[-1] for f in files]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
