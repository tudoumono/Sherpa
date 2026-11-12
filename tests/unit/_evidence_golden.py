"""`evidence_spike.extract` / `extract_assets` の出力を、開発用データの原本ごとに固定する補助（evidence の回帰試験の土台）。

試験（test_evidence_golden.py）と期待値の作り直し（scripts/evidence_golden.py）が共用する。test_ 接頭辞でないので収集されない。

- 対象は `fixtures/` 配下の PDF・PPTX・DOCX・XLSX（巨大な JPX-018 だけ除く）。
- 各原本について Evidence IR の決定的 JSON（`evidence_ir.to_json_str`）の SHA-256・件数・取り出した素材ファイル名の SHA-256 を残す。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
GOLDEN_FILE = Path(__file__).resolve().parent / "goldens" / "evidence" / "golden.json"
SUFFIXES = {".pdf", ".pptx", ".docx", ".xlsx"}
SKIP = {"fixtures/eval/excel_ja/inputs/JPX-018.xlsx"}


def sources() -> list[str]:
    """期待値を持つ原本の ROOT 相対パス（ソート済み）。"""
    return sorted(
        p.relative_to(ROOT).as_posix()
        for p in FIXTURES.rglob("*")
        if p.is_file() and p.suffix.lower() in SUFFIXES and p.relative_to(ROOT).as_posix() not in SKIP
    )


def snapshot(rel: str) -> dict:
    from sherpa.ingest import evidence_ir, evidence_spike

    path = ROOT / rel
    ir = evidence_spike.extract(path)
    text = evidence_ir.to_json_str(ir)
    with tempfile.TemporaryDirectory() as tmp:
        written = sorted(p.name for p in evidence_spike.extract_assets(path, ir, Path(tmp) / "assets"))
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "elements": len(ir.elements),
        "relations": len(ir.relations),
        "coverage": len(ir.coverage),
        "element_types": dict(sorted(Counter(e.type for e in ir.elements).items())),
        "assets_sha256": hashlib.sha256("\n".join(written).encode("utf-8")).hexdigest(),
        "assets": len(written),
    }


def read_golden() -> dict:
    return json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))


def write_golden(data: dict) -> None:
    GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def diff_rows(old: dict, new: dict) -> list[str]:
    rows = []
    for k in ("elements", "relations", "coverage", "assets"):
        if old.get(k) != new.get(k):
            rows.append(f"{k}: {old.get(k)} -> {new.get(k)}")
    for key in sorted(set(old.get("element_types", {})) | set(new.get("element_types", {}))):
        a, b = old.get("element_types", {}).get(key, 0), new.get("element_types", {}).get(key, 0)
        if a != b:
            rows.append(f"element_types[{key}]: {a} -> {b} ({b - a:+d})")
    return rows
