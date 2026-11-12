#!/usr/bin/env python3
"""`world_graph.build_world` の出力の期待値（ゴールデン）を作り直す／コミット間で比べる道具。

`fixtures/corpus/` 直下の各資料フォルダを正規化したグラフ（tests/unit/_graph_golden.py）に対して:

    scripts/graph_golden.py --write               # tests/unit/goldens/world_graph/ の期待値を作り直す
    scripts/graph_golden.py --diff <git-ref>      # <git-ref> の build_world と今の作業ツリーの build_world を比べる
    scripts/graph_golden.py --diff main --case c1 --case v1 --max-rows 20

  --case  資料フォルダ名（複数指定可・省略時は全部）。
  終了コード: --diff は差分 0 なら 0・あれば 1・実行できない（資料フォルダの構築が例外で終わったものを含む）なら 2。--write は 0。

--diff の ref 側は `git worktree add --detach` の一時チェックアウトを別プロセスで動かし、終わったら消す。
両側とも入力（fixtures/corpus・解決の設定）は今の作業ツリーのものを使う＝差分はコードの違いだけから出る。
試験: tests/unit/test_world_graph_golden.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "unit"))
sys.path.insert(0, str(ROOT))
import _graph_golden as gg  # noqa: E402


def _dump(repo: Path, out: Path, cases: list[str]) -> None:
    """子プロセス側: `repo` の sherpa で各資料フォルダを build_world し、正規化したグラフを JSON に書く。"""
    sys.path.insert(0, str(repo))
    import sherpa
    if Path(sherpa.__file__).resolve().parents[1] != repo.resolve():
        raise SystemExit(f"sherpa が {repo} から読まれていません: {sherpa.__file__}")
    result = {}
    for case in cases:
        try:
            result[case] = gg.build_graph(case, gg.CORPUS)
        except Exception as e:
            result[case] = {"error": f"{type(e).__name__}: {e}"}
    out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def _run_side(repo: Path, cases: list[str], tmp: Path, tag: str) -> dict:
    out = tmp / f"{tag}.json"
    env = dict(os.environ, PYTHONPATH=str(repo), SHERPA_DERIVED_DIR=str(tmp / f"derived-{tag}"))
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_dump", str(repo), str(out), *cases],
                   cwd=repo, env=env, check=True)
    return json.loads(out.read_text(encoding="utf-8"))


def _write(cases: list[str]) -> int:
    for name in cases:
        graph = gg.build_graph(name)
        gg.write_golden(name, graph)
        c = gg.counts(graph)
        print(f"{name}: nodes={c['nodes']} edges={c['edges']} flags={c['flags']} sha256={gg.digest(graph)[:12]}")
    return 0


def _diff(ref: str, cases: list[str], max_rows: int) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="graph-golden-"))
    wt = tmp / "wt-old"
    try:
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", str(wt), ref], check=True)
        old = _run_side(wt, cases, tmp, "old")
        new = _run_side(ROOT, cases, tmp, "new")
    except subprocess.CalledProcessError as e:
        print(f"実行できません: {e}", file=sys.stderr)
        return 2
    finally:
        subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(wt)], check=False,
                       stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(ROOT), "worktree", "prune"], check=False)
        shutil.rmtree(tmp, ignore_errors=True)
    differing = errors = 0
    for case in cases:
        o, n = old[case], new[case]
        if "error" in o or "error" in n:
            errors += 1
            print(f"!! {case}: 実行エラー old={o.get('error')} new={n.get('error')}")
            continue
        if gg.digest(o) == gg.digest(n):
            print(f"== {case}: 同じ（nodes={len(n['nodes'])} edges={len(n['edges'])} flags={len(n['flags'])}）")
            continue
        differing += 1
        print(f"-- {case}: 違う（sha256 {gg.digest(o)[:12]} -> {gg.digest(n)[:12]}）")
        for row in (gg.diff_counts(gg.counts(o), gg.counts(n)) + gg.diff_graphs(o, n, limit=max_rows))[:max_rows * 4]:
            print("   " + row)
    print(f"\n比べた資料フォルダ: {len(cases)} / 違いあり: {differing} / 実行エラー: {errors}")
    return 2 if errors else (1 if differing else 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--diff", metavar="GIT_REF")
    ap.add_argument("--case", action="append")
    ap.add_argument("--max-rows", type=int, default=10)
    ap.add_argument("--_dump", nargs="+", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a._dump:
        _dump(Path(a._dump[0]), Path(a._dump[1]), a._dump[2:])
        return 0
    cases = a.case or gg.corpus_names()
    if a.write:
        return _write(cases)
    if a.diff:
        return _diff(a.diff, cases, a.max_rows)
    ap.error("--write か --diff のどちらかを指定してください")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
