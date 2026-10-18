"""影響範囲の評価セット（S5）: 合成コーパスをその場で生成し、静的グラフの逆向き閉包を gold と突き合わせる。

使い方（手元で単独実行する場合）: python3 scripts/bench/eval_gate.py [--scale S|T] [--keep DIR]
CI での固定は `tests/contract/test_impact_eval_gate.py`（同じ採点関数）。Neo4j 不要。Recall・Precision がどちらも 1.000
でなければ非ゼロで終わり、落ちた問いごとに「漏れ（gold にあって結果に無い）」「余計（結果にあって gold に無い）」を架空名で列挙する。
gold は `gen_corpus.py` が依存を作る側で書くので、解析器（`world_graph.build_world`）とは独立。
実 Neo4j の BFS 経路の採点は `tests/integration/test_impact_bfs_eval.py`（同じ問い・同じ採点関数）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GEN = ROOT / "scripts" / "bench" / "gen_corpus.py"

# S は README の再現手順と同じ規模（bench の「S」）。T は手元の素早い確認用の小規模。
SCALES: dict[str, dict[str, int]] = {
    "S": {"programs": 300, "copybooks": 60, "jobs": 60, "docs": 200, "seed": 7, "questions": 40},
    "T": {"programs": 60, "copybooks": 10, "jobs": 12, "docs": 30, "seed": 7, "questions": 8},
}
IMPACT_EDGES = frozenset({"COPIES", "CONTAINS", "INVOKES", "ACCESSES"})
LIST_CAP = 12          # 失敗時に 1 種類あたり並べる件数の上限
QUESTION_CAP = 10      # 失敗時に詳細を並べる問いの数の上限（先頭から）


def generate(out: Path, scale: str = "S") -> Path:
    """`gen_corpus.py` を `SCALES[scale]` で走らせる（`--seed` 固定で決定的）。"""
    s = SCALES[scale]
    args = [sys.executable, str(GEN), "--out", str(out)]
    for key in ("programs", "copybooks", "jobs", "docs", "seed", "questions"):
        args += [f"--{key}", str(s[key])]
    subprocess.run(args, check=True, capture_output=True, text=True)
    return out


def load_questions(corpus: Path) -> list[dict]:
    return json.loads((corpus / "gold.json").read_text(encoding="utf-8"))["questions"]


def gold_files(q: dict) -> set[str]:
    g = q["gold"]
    return set(g["programs_direct"]) | set(g["programs_indirect"]) | set(g["jobs"]) | set(g["docs"])


@dataclass
class Graph:
    nodes: list[dict]
    edges: list[dict]
    rev: dict[str, set[str]] = field(default_factory=dict)
    docs_of: dict[str, set[str]] = field(default_factory=dict)
    by_cid: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def build(cls, corpus: Path) -> Graph:
        from sherpa.ingest import world_graph
        nodes, edges, _flags = world_graph.build_world(corpus, "eval_gate")
        return cls.of(nodes, edges)

    @classmethod
    def of(cls, nodes: list[dict], edges: list[dict]) -> Graph:
        g = cls(nodes, edges)
        g.by_cid = {n["cid"]: n for n in nodes}
        rev: dict[str, set[str]] = defaultdict(set)
        docs_of: dict[str, set[str]] = defaultdict(set)
        for e in edges:
            if e["type"] in IMPACT_EDGES:
                rev[e["dst"]].add(e["src"])
            elif e["type"] == "DOCUMENTS":
                docs_of[e["dst"]].add(g.by_cid[e["src"]]["path"] if e["src"] in g.by_cid else e["src"])
        g.rev, g.docs_of = dict(rev), dict(docs_of)
        return g

    def start_cids(self, q: dict) -> list[str]:
        """起点＝項目ノード。無ければコピーブックのノード（`impact_eval.py` と同じ）。"""
        cpy, item = q["start"]["copybook"], q["start"]["item"]
        starts = [n["cid"] for n in self.nodes if n.get("path") == cpy and n.get("name") == item]
        return starts or [n["cid"] for n in self.nodes if n.get("path") == cpy and n["label"] == "Copybook"]

    def answer(self, q: dict) -> set[str]:
        seen = set(self.start_cids(q))
        stack = list(seen)
        while stack:
            x = stack.pop()
            for y in self.rev.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        files = {self.by_cid[c]["path"] for c in seen if c in self.by_cid}
        docs = set().union(*(self.docs_of.get(c, set()) for c in seen)) if seen else set()
        return (files | docs) - {q["start"]["copybook"]}


@dataclass
class QuestionScore:
    qid: str
    start: str
    recall: float
    precision: float
    missing: list[str]
    extra: list[str]

    @property
    def exact(self) -> bool:
        return not self.missing and not self.extra


def score_question(q: dict, got: set[str]) -> QuestionScore:
    gold = gold_files(q)
    tp = len(got & gold)
    return QuestionScore(
        qid=q["id"], start=f'{q["start"]["copybook"]} / {q["start"]["item"]}',
        recall=tp / len(gold) if gold else 1.0, precision=tp / len(got) if got else 1.0,
        missing=sorted(gold - got), extra=sorted(got - gold))


def score_all(questions: list[dict], answers: dict[str, set[str]]) -> list[QuestionScore]:
    return [score_question(q, answers[q["id"]]) for q in questions]


def summary(scores: list[QuestionScore]) -> tuple[float, float]:
    n = len(scores) or 1
    return sum(s.recall for s in scores) / n, sum(s.precision for s in scores) / n


def _kind(path: str) -> str:
    return {".cbl": "プログラム", ".jcl": "ジョブ", ".md": "設計書"}.get(Path(path).suffix, "その他")


def _lines(label: str, paths: list[str]) -> list[str]:
    if not paths:
        return []
    by_kind: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        by_kind[_kind(p)].append(Path(p).name)
    out = [f"    {label} {len(paths)} 件"]
    for kind, names in by_kind.items():
        shown = ", ".join(names[:LIST_CAP]) + (f" …他 {len(names) - LIST_CAP} 件" if len(names) > LIST_CAP else "")
        out.append(f"      {kind} {len(names)}: {shown}")
    return out


def format_report(scores: list[QuestionScore], label: str) -> str:
    r, p = summary(scores)
    bad = [s for s in scores if not s.exact]
    lines = [f"[{label}] 問い {len(scores)} 件・Recall {r:.3f}・Precision {p:.3f}（しきい値 1.000／1.000）"]
    if bad:
        lines.append(f"NG: {len(bad)} 問が gold と一致しません（漏れ＝gold にあって結果に無い／余計＝結果にあって gold に無い）")
        for s in bad[:QUESTION_CAP]:
            lines.append(f"  {s.qid}（起点 {s.start}）Recall {s.recall:.3f}・Precision {s.precision:.3f}")
            lines += _lines("漏れ", s.missing) + _lines("余計", s.extra)
        if len(bad) > QUESTION_CAP:
            lines.append(f"  …他 {len(bad) - QUESTION_CAP} 問（先頭 {QUESTION_CAP} 問だけ詳細を表示）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scale", choices=sorted(SCALES), default="S")
    ap.add_argument("--keep", help="生成したコーパスをこのディレクトリに残す（既定は一時ディレクトリで後始末）")
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="sherpa-eval-") as tmp:
        corpus = Path(a.keep) if a.keep else Path(tmp) / "corpus"
        t0 = time.monotonic()
        generate(corpus, a.scale)
        questions = load_questions(corpus)
        t1 = time.monotonic()
        graph = Graph.build(corpus)
        t2 = time.monotonic()
        scores = score_all(questions, {q["id"]: graph.answer(q) for q in questions})
        t3 = time.monotonic()
    stats = ", ".join(f"{k} {v}" for k, v in SCALES[a.scale].items())
    print(f"影響範囲の評価セット（静的グラフ閉包・Neo4j 無し）規模 {a.scale}: {stats}")
    print(f"  生成 {t1 - t0:.1f}s・build_world {t2 - t1:.1f}s（ノード {len(graph.nodes)}・辺 {len(graph.edges)}）・採点 {t3 - t2:.2f}s")
    print(format_report(scores, a.scale))
    return 0 if all(s.exact for s in scores) else 1


if __name__ == "__main__":
    sys.exit(main())
