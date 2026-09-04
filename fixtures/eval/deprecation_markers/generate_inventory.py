"""`benchmarks/deprecation_inventory.json`（廃止マーカーの実データ棚卸し）を再生成する。

再生成:
    .venv/bin/python fixtures/eval/deprecation_markers/generate_inventory.py

`fixtures/eval/excel_ja`／`fixtures/eval/office_ja`／`fixtures/eval/deprecation_markers` 配下の
xlsx/docx/pptx を `scan_deprecation_markers.build_inventory` で走査し、決定的な JSON
（sort_keys・末尾改行1つ）として書き出す。タイムスタンプ等の非決定要素は含まない。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]


def _load_scanner():
    spec = importlib.util.spec_from_file_location("deprecation_markers_scanner", _HERE / "scan_deprecation_markers.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> None:
    scanner = _load_scanner()
    roots = [
        _ROOT / "fixtures" / "eval" / "excel_ja",
        _ROOT / "fixtures" / "eval" / "office_ja",
        _ROOT / "fixtures" / "eval" / "deprecation_markers",
    ]
    inventory = scanner.build_inventory(roots, _ROOT)
    out_path = _HERE / "benchmarks" / "deprecation_inventory.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out_path} ({len(inventory)} files with markers)")


if __name__ == "__main__":
    main()
