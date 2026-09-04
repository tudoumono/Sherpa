"""`web/ingest.js` の `reasonText`/`officeBlockedReasonText`（内部コード→平文写像）の振る舞いを
実ファイルから抽出して node で直接実行し固定する（専門用語ゼロ・例外クラス名を出さない契約）。

この2関数は DOM/`Sherpa` 名前空間に依存しない純粋関数のため、ファイル全体を評価せず
（ファイル末尾に `reloadAll()` 等の実行時副作用があり Node 単体では動かせない）、該当定義だけを
文字列で切り出して評価する——手書きで複製すると実装とテストが乖離しうるため、常に実ファイルを
読む（`tests/unit/test_web_assets.py` の `node --check` 構文ゲートと同じ「node 不在なら skip」流儀）。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
INGEST_JS = ROOT / "web" / "ingest.js"


def _extract_reason_functions() -> str:
    """`const REASON_JA = {...}` から `function reasonText(...) {...}` の閉じ括弧までを切り出す。"""
    src = INGEST_JS.read_text(encoding="utf-8")
    start = src.index("const REASON_JA")
    fn_start = src.index("function reasonText")
    end = src.index("\n}", fn_start) + len("\n}")
    snippet = src[start:end]
    assert "officeBlockedReasonText" in snippet, "抽出範囲に officeBlockedReasonText が含まれていない"
    return snippet


def _run_reason_text(reason) -> str | None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node が見つからない（reasonText 振る舞いテストは対象外）")
    script = _extract_reason_functions() + (
        f"\nconsole.log(JSON.stringify(reasonText({json.dumps(reason)})));"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"node 実行に失敗:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
    return json.loads(result.stdout)


def test_office_blocked_unhandled_exception_does_not_leak_class_name():
    """`office_md_blocked:{doc}\\t{unhandled_exception:ClassName}` は例外クラス名を出さない
    （専門用語ゼロ・内部実装の詳細を利用者に見せない）。"""
    out = _run_reason_text("office_md_blocked:broken.xlsx\tunhandled_exception:RuntimeError")
    assert out == "Office 文書を変換できませんでした（想定外のエラー）"
    assert "RuntimeError" not in out


def test_office_blocked_unhandled_os_error_does_not_leak_class_name():
    out = _run_reason_text("office_md_blocked:broken.xlsx\tunhandled_os_error:PermissionError")
    assert out == "Office 文書を変換できませんでした（想定外のエラー）"
    assert "PermissionError" not in out


def test_office_blocked_manifest_write_failed_maps_to_plain_text():
    out = _run_reason_text("office_md_blocked:broken.xlsx\tmanifest_write_failed")
    assert out == "Office 文書を変換できませんでした（記録の書き込みに失敗しました）"


def test_office_blocked_unknown_inner_reason_falls_back_without_leaking_raw_value():
    """未知の内側理由（将来の新規コード）は fail-open で汎用メッセージへ倒す
    （raw 値をそのまま出さない）。"""
    out = _run_reason_text("office_md_blocked:broken.xlsx\tsome_future_reason_code")
    assert out == "Office 文書を変換できませんでした"
    assert "some_future_reason_code" not in out


def test_non_office_reason_codes_still_map_to_plain_japanese():
    assert _run_reason_text("read_failed") == "ファイルを読み取れませんでした"
    assert _run_reason_text("unreadable_code_file") == "コードを読み取れなかったため取り込みを止めました"


def test_unknown_reason_code_passes_through_unchanged():
    """`REASON_JA`/`office_md_blocked:` のどちらにも該当しない値は raw のまま返す
    （fail-open・原因追跡の手がかりを残す）。"""
    assert _run_reason_text("some_other_internal_code") == "some_other_internal_code"
