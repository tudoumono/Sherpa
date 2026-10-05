#!/usr/bin/env python3
"""アナライザの旧新の差分の道具（ANA-19・段階 2a の置き換えの受入条件）。

`fixtures/corpus/` の各ケース（最上位のフォルダ）を、旧と新のコードで `world_graph.build_world` にかけ、
ノード（cid）・辺 `(始点, 型, 終点, via, 行)`・申告（`flags` の dict 全体）の差分を JSON と表で出す。
さらに各ファイルについて、アナライザの出力そのもの（`collect_defs` の `DefResult` 全欄・`extract_refs` の `RefResult` 全欄
〔`refs` の name・kind・line・via 等・`dropped`・`file_context` の package／namespaces／imports〕・`global_imports`）を
旧新で並べて比べる（リストは安定した並べ替えで比べる＝並びの違いは差分にしない）。出力の終わりに「比べたファイル数・
拡張子ごとの件数」を出す（ここに無い拡張子は未検証）。
入力（fixture）は両側とも「新」側のものを使うので、差分はコードの違いだけから出る。

使い方:
    scripts/analyzer_diff.py --old <コミット>                     # 新＝今の作業ツリー
    scripts/analyzer_diff.py --old <コミット> --new <コミット>     # 2 つのコミットを比べる
    scripts/analyzer_diff.py --old main --ext .java,.jsp --case java1 --case java-fw
    scripts/analyzer_diff.py --old main --json /tmp/diff.json --max-rows 200

  --ext   比べる対象の拡張子（カンマ区切り・繰り返しも可）。ノードの path・辺の doc・申告の from/doc の拡張子で絞る。
  --case  ケース名（最上位のフォルダ名）。複数指定可。省略時は全ケース。
  --extra-root  fixtures/corpus 以外のソースの木（評価用データなど）を母集団に足す（複数可・アナライザの出力の比較だけが対象）。
  --json  差分の JSON の出力先（省略時は表だけ）。
  終了コード: 差分 0 なら 0・あれば 1・実行できない／ケースの実行エラーがあれば 2。
  --new を指定したときは、入力の fixture もそのコミットのものを両側に使う（省略時は今の作業ツリー）。

旧・新のコミットは `git worktree add --detach` の一時のチェックアウトで動かし、終わったら消す（作業ツリーを汚さない）。
各ケースの取り込みの時間（秒）も出す（速さの報告用・差分の判定には使わない）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS_REL = Path("fixtures") / "corpus"
EXTRA_ROOTS: list[str] = []


def _dump(repo: Path, corpus: Path, cases: list[str], out: Path) -> None:
    """子プロセス側: `repo` の sherpa でケースごとに build_world して JSON に書く。"""
    sys.path.insert(0, str(repo))
    from sherpa.ingest import world_graph  # noqa: E402
    import sherpa  # noqa: E402
    if Path(sherpa.__file__).resolve().parents[1] != repo.resolve():
        raise SystemExit(f"sherpa が {repo} から読まれていません: {sherpa.__file__}")
    result: dict = {}
    for case in cases:
        t0 = time.perf_counter()
        try:
            nodes, edges, flags = world_graph.build_world(corpus / case, case)
        except Exception as e:  # 失敗も差分として出す
            result[case] = {"error": f"{type(e).__name__}: {e}", "seconds": time.perf_counter() - t0}
            continue
        result[case] = {
            "seconds": time.perf_counter() - t0,
            "nodes": {n["cid"]: n for n in nodes},
            "edges": [{"key": [e["src"], e["type"], e["dst"], e.get("via"), e.get("line")], "doc": e.get("doc")}
                      for e in edges],
            "flags": flags,
        }
    roots = {c: corpus / c for c in cases}
    for i, x in enumerate(filter(None, os.environ.get("SHERPA_ANALYZER_DIFF_EXTRA", "").split(os.pathsep))):
        roots[f"extra{i}:{Path(x).name}"] = Path(x)
    result["__analyzer__"] = {name: _dump_analyzer_outputs(root) for name, root in roots.items()}
    out.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str), encoding="utf-8")


def _norm(obj):
    """dataclass・tuple を JSON 化できる形にし、リストは安定した並べ替え（順序の違いを差分にしない）。"""
    import dataclasses
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _norm(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _norm(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return sorted((_norm(v) for v in obj), key=_canon)
    return obj


def _dump_analyzer_outputs(root: Path) -> dict:
    """子プロセス側: `root` 配下の各ファイルを `world_graph` と同じ手順でアナライザへ通し、出力を正規化して返す。"""
    from sherpa import corpus_docs, text_encoding
    from sherpa.ingest.analyzers import registry
    out: dict = {}
    for rp in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = rp.relative_to(root).as_posix()
        cands = registry.candidates(rel)
        if not cands:
            continue
        try:
            text, raw = corpus_docs.read_full_text_and_raw(rp)
            enc, ratio, garbled = text_encoding.detect_bytes_quality(raw, complete=True)
            if text_encoding.quality_of(ratio, garbled) == "undetermined":
                continue
            an = next((a for a in cands if a.accepts(rel, text_encoding.decode(raw[:getattr(a, "head_bytes", 4096)], enc))),
                      None)
            if an is None:
                continue
            ent = {"analyzer": an.name, "defs": _norm(an.collect_defs(text, rel)),
                   "refs": _norm(an.extract_refs(text, rel)), "global_imports": _norm(an.global_imports(text, rel))}
        except Exception as e:  # 失敗も差分として出す
            ent = {"error": f"{type(e).__name__}: {e}"}
        out[rel] = ent
    return out


def _items(ent: dict) -> Counter:
    """1 ファイルの出力を `(欄, 項目の JSON)` の多重集合にする。"""
    c: Counter = Counter()
    for k, v in ent.items():
        if k in ("analyzer", "error"):
            c[(k, _canon(v))] += 1
        elif k == "refs":
            for kk, vv in v.items():
                if kk == "file_context" and isinstance(vv, dict):
                    for k3, v3 in vv.items():
                        for item in (v3 if isinstance(v3, list) else [v3]):
                            c[(f"refs.file_context.{k3}", _canon(item))] += 1
                else:
                    for item in (vv if isinstance(vv, list) else [vv]):
                        c[(f"refs.{kk}", _canon(item))] += 1
        elif k == "defs":
            for kk, vv in v.items():
                for item in (vv if isinstance(vv, list) else [vv]):
                    c[(f"defs.{kk}", _canon(item))] += 1
        else:
            for item in v:
                c[(k, _canon(item))] += 1
    return c


def _analyzer_diff(old: dict, new: dict, exts: set[str] | None) -> dict:
    roots: dict = {}
    by_ext: Counter = Counter()
    for root in sorted(set(old) | set(new)):
        o, n = old.get(root, {}), new.get(root, {})
        files = sorted(f for f in set(o) | set(n) if _ext_ok(f, exts))
        for f in files:
            by_ext[Path(f).suffix.lower()] += 1
        changed = {}
        for f in files:
            if f in o and f in n:
                a, b = _items(o[f]), _items(n[f])
                if a != b:
                    changed[f] = {"added": [list(k) for k in sorted((b - a).elements())],
                                  "removed": [list(k) for k in sorted((a - b).elements())]}
        r = {"added_files": [f for f in files if f not in o], "removed_files": [f for f in files if f not in n],
             "changed": changed}
        if any(r.values()):
            roots[root] = r
    return {"roots": roots, "compared_files": sum(by_ext.values()), "by_ext": dict(sorted(by_ext.items()))}


def _print_analyzer(rep: dict, max_rows: int) -> None:
    print("\n== アナライザの出力の比較（collect_defs／extract_refs／global_imports）")
    for root, r in rep["roots"].items():
        print(f"-- {root}")
        rows = [("+ file", f) for f in r["added_files"]] + [("- file", f) for f in r["removed_files"]]
        for f, d in r["changed"].items():
            rows += [(f"~ {f}", "") ] + [("   +", f"{k[0]} {k[1]}") for k in d["added"]] \
                + [("   -", f"{k[0]} {k[1]}") for k in d["removed"]]
        for tag, text in rows[:max_rows]:
            print(f"{tag}  {text}".rstrip())
        if len(rows) > max_rows:
            print(f"... 他 {len(rows) - max_rows} 行（--max-rows で増やす・--json で全件）")
    print(f"比べたファイル数: {rep['compared_files']}（拡張子別: "
          + (", ".join(f"{k or '(なし)'}={v}" for k, v in rep["by_ext"].items()) or "なし")
          + "）。ここに無い拡張子は未検証。")


def _run_side(repo: Path, corpus: Path, cases: list[str], tmp: Path, tag: str) -> dict:
    out = tmp / f"{tag}.json"
    env = dict(os.environ, SHERPA_ANALYZER_DIFF_EXTRA=os.pathsep.join(EXTRA_ROOTS), PYTHONPATH=str(repo), SHERPA_DERIVED_DIR=str(tmp / f"derived-{tag}"))
    subprocess.run([sys.executable, str(ROOT / "scripts" / "analyzer_diff.py"), "--_dump", str(repo), str(corpus),
                    str(out), *cases], cwd=repo, env=env, check=True)
    return json.loads(out.read_text(encoding="utf-8"))


def _checkout(rev: str, tmp: Path, tag: str, added: list[Path]) -> Path:
    dest = tmp / f"wt-{tag}"
    subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", str(dest), rev], check=True)
    added.append(dest)
    return dest


def _ext_ok(path: str | None, exts: set[str] | None) -> bool:
    if exts is None:
        return True
    return bool(path) and Path(path).suffix.lower() in exts


def _flag_in_ext(f: dict, exts: set[str] | None) -> bool:
    """`from`・`doc`・`paths`（copy_cycle など）のどれかに対象の拡張子があれば対象。"""
    if exts is None:
        return True
    paths = [f.get("from"), f.get("doc"), *(f.get("paths") or [])]
    return any(_ext_ok(p, exts) for p in paths if isinstance(p, str))


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _case_diff(old: dict, new: dict, exts: set[str] | None) -> dict:
    d: dict = {"seconds": {"old": old.get("seconds"), "new": new.get("seconds")}}
    if "error" in old or "error" in new:
        d["error"] = {"old": old.get("error"), "new": new.get("error")}
        return d
    on = {c: n for c, n in old["nodes"].items() if _ext_ok(n.get("path"), exts)}
    nn = {c: n for c, n in new["nodes"].items() if _ext_ok(n.get("path"), exts)}
    d["nodes"] = {"added": sorted(set(nn) - set(on)), "removed": sorted(set(on) - set(nn)),
                  "changed": sorted(c for c in set(on) & set(nn) if _canon(on[c]) != _canon(nn[c]))}
    oe = Counter(tuple(e["key"]) for e in old["edges"] if _ext_ok(e["doc"], exts))
    ne = Counter(tuple(e["key"]) for e in new["edges"] if _ext_ok(e["doc"], exts))
    d["edges"] = {"added": [list(k) for k in sorted((ne - oe).elements(), key=repr)],
                  "removed": [list(k) for k in sorted((oe - ne).elements(), key=repr)]}
    of = Counter(_canon(f) for f in old["flags"] if _flag_in_ext(f, exts))
    nf = Counter(_canon(f) for f in new["flags"] if _flag_in_ext(f, exts))
    d["flags"] = {"added": sorted((nf - of).elements()), "removed": sorted((of - nf).elements())}
    return d


def _is_empty(d: dict) -> bool:
    if "error" in d:
        return False
    return not any(d[s][k] for s in ("nodes", "edges", "flags") for k in d[s])


def _print_table(report: dict, max_rows: int) -> None:
    cases = report["cases"]
    print(f"旧: {report['old']}\n新: {report['new']}\n対象の拡張子: {report['ext'] or '全部'}\n")
    print(f"{'ケース':<16}{'ノード+':>8}{'ノード-':>8}{'ノード変':>8}{'辺+':>6}{'辺-':>6}{'申告+':>7}{'申告-':>7}"
          f"{'旧秒':>8}{'新秒':>8}")
    for name, d in cases.items():
        s = d["seconds"]
        if "error" in d:
            print(f"{name:<16}  実行エラー 旧={d['error']['old']} 新={d['error']['new']}")
            continue
        print(f"{name:<16}{len(d['nodes']['added']):>8}{len(d['nodes']['removed']):>8}{len(d['nodes']['changed']):>8}"
              f"{len(d['edges']['added']):>6}{len(d['edges']['removed']):>6}"
              f"{len(d['flags']['added']):>7}{len(d['flags']['removed']):>7}"
              f"{s['old']:>8.2f}{s['new']:>8.2f}")
    for name, d in cases.items():
        if _is_empty(d):
            continue
        print(f"\n== {name}")
        rows = []
        if "error" not in d:
            rows += [("+ node", c) for c in d["nodes"]["added"]] + [("- node", c) for c in d["nodes"]["removed"]]
            rows += [("~ node", c) for c in d["nodes"]["changed"]]
            rows += [("+ edge", " ".join(map(str, k))) for k in d["edges"]["added"]]
            rows += [("- edge", " ".join(map(str, k))) for k in d["edges"]["removed"]]
            rows += [("+ flag", f) for f in d["flags"]["added"]] + [("- flag", f) for f in d["flags"]["removed"]]
        for tag, text in rows[:max_rows]:
            print(f"{tag}  {text}")
        if len(rows) > max_rows:
            print(f"... 他 {len(rows) - max_rows} 行（--max-rows で増やす・--json で全件）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", help="旧のコミット（必須）")
    ap.add_argument("--new", help="新のコミット（省略時は今の作業ツリー）")
    ap.add_argument("--ext", action="append", help="対象の拡張子（カンマ区切り・繰り返しも可。例: .java,.jsp または --ext .java --ext .jsp）")
    ap.add_argument("--case", action="append", help="ケース名（複数可・省略時は全部）")
    ap.add_argument("--extra-root", action="append", default=[], help="母集団に足すソースの木（複数可）")
    ap.add_argument("--json", help="差分の JSON の出力先")
    ap.add_argument("--max-rows", type=int, default=50, help="表に出す差分の行数の上限（ケースごと）")
    ap.add_argument("--_dump", nargs="+", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a._dump:  # 内部用: 子プロセスでの 1 側の実行
        repo, corpus, out, *cases = a._dump
        _dump(Path(repo), Path(corpus), cases, Path(out))
        return 0
    if not a.old:
        ap.error("--old が必要です")

    EXTRA_ROOTS[:] = [str(Path(x).resolve()) for x in a.extra_root]
    exts = {e if e.startswith(".") else "." + e for x in a.ext for e in x.lower().split(",") if e} if a.ext else None

    added: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix="analyzer-diff-") as td:
            tmp = Path(td)
            old_repo = _checkout(a.old, tmp, "old", added)
            new_repo = _checkout(a.new, tmp, "new", added) if a.new else ROOT
            corpus = new_repo / CORPUS_REL  # 入力は新側の fixtures を両側へ渡す
            names = sorted(p.name for p in corpus.iterdir() if p.is_dir())
            if a.case:
                unknown = sorted(set(a.case) - set(names))
                if unknown:
                    print(f"ケースが見つかりません: {unknown}", file=sys.stderr)
                    return 2
                names = a.case
            old = _run_side(old_repo, corpus, names, tmp, "old")
            new = _run_side(new_repo, corpus, names, tmp, "new")
    except subprocess.CalledProcessError as e:
        print(f"実行に失敗しました: {e}", file=sys.stderr)
        return 2
    finally:
        for wt in added:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(wt)], check=False)
        subprocess.run(["git", "-C", str(ROOT), "worktree", "prune"], check=False)

    old_an, new_an = old.pop("__analyzer__"), new.pop("__analyzer__")
    old_an = {k: v for k, v in old_an.items() if k in names or k.startswith("extra")}
    new_an = {k: v for k, v in new_an.items() if k in names or k.startswith("extra")}
    report = {"old": a.old, "new": a.new or "作業ツリー", "ext": sorted(exts) if exts else None,
              "cases": {n: _case_diff(old[n], new[n], exts) for n in names},
              "analyzer": _analyzer_diff(old_an, new_an, exts)}
    if a.json:
        Path(a.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    _print_table(report, a.max_rows)
    _print_analyzer(report["analyzer"], a.max_rows)
    if any("error" in d for d in report["cases"].values()):
        return 2  # ケースの実行エラーは差分に混ぜず、常に実行不能として扱う
    if report["analyzer"]["roots"]:
        return 1
    return 0 if all(_is_empty(d) for d in report["cases"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
