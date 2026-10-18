#!/usr/bin/env python3
"""保存済みの台帳評価を原因別に分類し直し、結果を別の場所へ新規ファイルとして書く。

読むのは出力フォルダの trials/（または smoke/）・runtime/ の記録だけ。元の scores.jsonl・summary.md・result.json は
書き換えない。モデル・API・Docker は使わない。
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import sys

try:
    from scripts.bench import ledger_scoring as scoring
except ImportError:
    import ledger_scoring as scoring



def _eval_module():
    try:
        from scripts.bench import ledger_eval
    except ImportError:
        import ledger_eval
    return ledger_eval


OUTPUTS = ("classification.jsonl", "scores.jsonl", "summary.md", "behavior.md", "reclassify-meta.json")


def write_new(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as out:
        out.write(text)


def reclassify(results: Path, out: Path, phase_dir: str = "trials", compare_with: Path | None = None,
               corpus_files: list[str] | None = None) -> dict:
    results, out = results.resolve(), out.resolve()
    if not (results / phase_dir).is_dir():
        raise scoring.EvalError(f"{phase_dir}/ がありません")
    if out == results or (results / phase_dir) in (out, *out.parents) or (results / "runtime") in (out, *out.parents):
        raise scoring.EvalError("出力先は生データ（trials・smoke・runtime）の外に指定してください")
    if any((out / name).exists() for name in (*OUTPUTS, "compare.md")):
        raise scoring.EvalError("出力先に既存の再分類ファイルがあります。上書きしないので別の出力先を指定してください")
    out.mkdir(parents=True, exist_ok=True)
    enrich = None
    if corpus_files is not None:
        ledger_eval = _eval_module()
        enrich = lambda target, result: ledger_eval.extra_metrics(target, result, corpus_files)  # noqa: E731
    rows = scoring.collect(results, phase_dir, fresh=True, server_tools=True, enrich=enrich)
    meta_path = results / "metadata.json"
    model = json.loads(meta_path.read_text(encoding="utf-8")).get("model") if meta_path.is_file() else None
    lines = []
    for base in sorted((results / phase_dir).glob("*/*/*")):
        for attempt in scoring.load_attempts(base, results, fresh=True) if base.is_dir() else ():
            result = attempt["result"]
            lines.append(json.dumps({
                "style": result.get("style"), "system": result.get("system"), "id": result.get("id"),
                "attempt_no": attempt["n"], "conversation_id": result.get("conversation_id"),
                "recorded_status": result.get("status"), "wall_seconds": result.get("wall_seconds"),
                **attempt["classification"]}, ensure_ascii=False))
    write_new(out / "classification.jsonl", "\n".join(lines) + ("\n" if lines else ""))
    write_new(out / "scores.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    systems = tuple(n for n in (*scoring.SYSTEMS, "self") if any(r["system"] == n for r in rows)) or scoring.SYSTEMS
    write_new(out / "summary.md", scoring.summary_markdown(rows, systems, model))
    if compare_with is not None:
        other = compare_with.resolve()
        write_new(out / "compare.md", scoring.compare_markdown(
            rows, scoring.collect(other, phase_dir, fresh=True), results.name, other.name))
    write_new(out / "behavior.md", scoring.behavior_markdown(rows))
    meta = {"source": str(results), "phase_dir": phase_dir,
            "source_metadata_fingerprint": json.loads((results / "metadata.json").read_text(encoding="utf-8")).get("fingerprint")
            if (results / "metadata.json").is_file() else None,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "counts": {c: sum(r["class"] == c for r in rows) for c in scoring.CLASSES},
            "note": "元の scores.jsonl・summary.md・result.json は変更していない。分類の規則は ledger_scoring.classify_trial。"}
    write_new(out / "reclassify-meta.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path, help="評価の出力フォルダ（読み取りのみ）")
    parser.add_argument("--out", required=True, type=Path, help="再分類の書き出し先（新規ファイルのみ）")
    parser.add_argument("--compare-with", type=Path, default=None, help="同じ質問・構成の別の出力フォルダ（比較表 compare.md を書く）")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="追加指標（根拠の到達・見直し）を保存済みの回答から取り直すための manifest（読み取りのみ）")
    parser.add_argument("--phase-dir", choices=("trials", "smoke"), default="trials")
    args = parser.parse_args(argv)
    try:
        files = None
        if args.manifest is not None:
            ledger_eval = _eval_module()
            files = ledger_eval.read_manifest(args.manifest)[0]["corpus_files"]
        meta = reclassify(args.results, args.out, args.phase_dir, args.compare_with, files)
    except (scoring.EvalError, OSError, ValueError) as exc:
        print(f"再分類を停止: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(meta["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
