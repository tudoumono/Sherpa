"""L6′: 「廃止」構造マーカーの実データ棚卸しを検証する（sherpa/ 非依存・常に green）。

背景: `docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md` §2（提案A）は、Office 文書の
「廃止」運用（セルを図形で覆う・取り消し線・隠し文字・変更履歴削除・非表示シート/行/列/スライド）を
Evidence IR に載せる設計を扱う。本テストはその**受け入れ判定の土台**として、
`fixtures/eval/**` の実データにこれらの表現が実在するかどうかの一次事実（構造走査）を検証する。

この走査（`fixtures/eval/deprecation_markers/scan_deprecation_markers.py`）は sherpa/ 側の取り込み
パイプライン（`evidence_spike.py` 等・L3 レーンが並行して書き換え中）を一切経由しない。したがって
本ファイルは L3 の進捗に関わらず常に green（L3 の受け入れ判定は
`test_deprecation_marker_acceptance.py` が担う）。

棚卸しJSONの再生成: `.venv/bin/python fixtures/eval/deprecation_markers/generate_inventory.py`
新規フィクスチャの再生成: `.venv/bin/python fixtures/eval/deprecation_markers/generate_fixtures.py`
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DEP_DIR = _ROOT / "fixtures" / "eval" / "deprecation_markers"
_INVENTORY_JSON = _DEP_DIR / "benchmarks" / "deprecation_inventory.json"


def _load_scanner():
    """このリポジトリはパッケージ化していないため、cwd/sys.path に依存しないファイルパスロードを使う
    （`sherpa/ingest/relation_rules.py::_load_rule` と同じパターン）。"""
    spec = importlib.util.spec_from_file_location(
        "deprecation_markers_scanner", _DEP_DIR / "scan_deprecation_markers.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_scanner = _load_scanner()

_INVENTORY_ROOTS = [
    _ROOT / "fixtures" / "eval" / "excel_ja",
    _ROOT / "fixtures" / "eval" / "office_ja",
    _ROOT / "fixtures" / "eval" / "deprecation_markers",
]


def _current_inventory() -> dict:
    return _scanner.build_inventory(_INVENTORY_ROOTS, _ROOT)


def test_inventory_matches_committed_snapshot():
    """コミット済み `deprecation_inventory.json` が実データの再走査結果と一致する（drift 検知）。

    ずれた場合は `generate_inventory.py` で再生成してから差分をレビューする（対象ディレクトリの
    フィクスチャが増減・変更された場合に想定内で発生しうる）。
    """
    committed = json.loads(_INVENTORY_JSON.read_text(encoding="utf-8"))
    assert _current_inventory() == committed


def test_real_fixture_hidden_sheet_and_rows_columns():
    """JPX-006.xlsx（実データ）: 非表示シート「旧版」・完全非表示シート「内部退避」・
    シート「現行」の非表示行3/非表示列Dが実在する。"""
    entry = _current_inventory()["fixtures/eval/excel_ja/inputs/JPX-006.xlsx"]
    assert entry["hidden_sheets"] == ["旧版"]
    assert entry["very_hidden_sheets"] == ["内部退避"]
    assert entry["hidden_rows"]["現行"] == [3]
    assert entry["hidden_columns"]["現行"] == ["D"]


def test_real_fixture_stamp_image_covers_cell():
    """JPX-011.xlsx（実データ）: `assets/廃止スタンプ.png` がセルへ画像アンカーとして実在する
    （A2 起点＝旧売上額/旧税額の行を覆う）。"""
    entry = _current_inventory()["fixtures/eval/excel_ja/inputs/JPX-011.xlsx"]
    pictures = [a for a in entry["drawing_anchors"] if a["kind"] == "picture"]
    assert pictures, "picture anchor が見つからない"
    assert pictures[0]["from_cell_zero_based"] == [0, 1]  # 0-based: 列A・行2


def test_real_fixture_covering_text_shapes_with_deprecation_label():
    """JPX-009/010/016/020/021/099.xlsx（実データ）: 「廃止」を明記したテキストボックスが
    セルへ重ねて配置されている。"""
    inventory = _current_inventory()
    for name in ("JPX-009", "JPX-010", "JPX-016", "JPX-020", "JPX-021", "JPX-099"):
        entry = inventory[f"fixtures/eval/excel_ja/inputs/{name}.xlsx"]
        shapes = [a for a in entry["drawing_anchors"] if a["kind"] == "shape"]
        assert any("廃止" in a["text"] for a in shapes), f"{name}: 「廃止」を含む shape が無い"


def test_real_fixture_hidden_pptx_slide():
    """OJA-PPTX-HARD.pptx（実データ）: `p:sld/@show="0"` の非表示スライドが実在する。"""
    entry = _current_inventory()["fixtures/eval/office_ja/inputs/OJA-PPTX-HARD.pptx"]
    assert entry["hidden_slides"] == ["ppt/slides/slide2.xml"]


def test_new_fixture_covers_representations_missing_from_real_data():
    """本レーンが新規生成した最小フィクスチャ（`inputs/DEP-*`）に、実データ棚卸しで欠けていた表現
    （xlsx取り消し線・pptx covered_by_text 等）が含まれることを確認する（§「足りない入力を作る」）。"""
    inventory = _current_inventory()

    xlsx = inventory["fixtures/eval/deprecation_markers/inputs/DEP-XLSX-MARKERS.xlsx"]
    assert xlsx["strikethrough_cells"] == ["対象!B2"]

    docx = inventory["fixtures/eval/deprecation_markers/inputs/DEP-DOCX-MARKERS.docx"]
    assert docx["strike_runs"] and docx["double_strike_runs"] and docx["hidden_runs"] and docx["deleted_runs"]

    pptx = inventory["fixtures/eval/deprecation_markers/inputs/DEP-PPTX-MARKERS.pptx"]
    assert pptx["hidden_slides"] == ["ppt/slides/slide4.xml"]
    slide2_shapes = pptx["shapes_by_slide"]["ppt/slides/slide2.xml"]
    assert any(s["text"] == "廃止" for s in slide2_shapes)
