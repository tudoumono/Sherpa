"""`web/ingest.js` の `summaryText`（取り込み状況の要約行）を実ファイルから抽出して node で
直接実行し固定する（`tests/unit/test_ingest_js_reason_text.py` と同じ「node 不在なら skip」流儀・
DOM/`Sherpa` 名前空間には依存しない純粋関数のため `esc` だけ最小スタブを与える）。
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

_BASE_S = {
    "indexed": 5, "office_md": 0, "skipped_office": 0, "office_failed": 0,
    "analyzer_declined_as_document": 0, "analyzer_declined": 0, "skipped_other": 0,
    "graph_nodes": 3, "es_chunks": None,
    "counts_as_of": "2026-01-01T00:00:00+00:00", "unreachable_as_text": 4,
}


def _extract_summary_text() -> str:
    """`function summaryText(s) {` から閉じ括弧までを切り出す。"""
    src = INGEST_JS.read_text(encoding="utf-8")
    start = src.index("function summaryText(s) {")
    end = src.index("\n}", start) + len("\n}")
    return src[start:end]


def _run_summary_text(s: dict) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node が見つからない（summaryText 振る舞いテストは対象外）")
    stub = "function esc(x){ return String(x); }\n"
    script = stub + _extract_summary_text() + (
        f"\nconsole.log(JSON.stringify(summaryText({json.dumps(s)})));"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"node 実行に失敗:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
    return json.loads(result.stdout)


def test_unreachable_as_text_shown_when_counted():
    """集計時刻（`counts_as_of`）がある＝実測なら件数を出す。"""
    out = _run_summary_text(_BASE_S)
    assert "本文が読めず対象外 4 件" in out


def test_unreachable_as_text_hidden_when_not_counted():
    """`counts_as_of` が無い（未集計・旧形式集計の欠落補完中）ときは、その値が非ゼロでも件数を
    出さない——未集計を「対象外 0 件／N 件」と誤解させない（画面側の表示分岐）。"""
    s = {**_BASE_S, "counts_as_of": None}
    out = _run_summary_text(s)
    assert "本文が読めず対象外" not in out


def test_unreachable_as_text_hidden_when_zero_even_if_counted():
    """実測で0件なら（従来どおり）行自体を出さない——「未集計」と「実測0件」はどちらも件数の
    行を出さない点は同じだが、`countsAsOfNote` 側の「（未集計）」有無で区別が付く。"""
    s = {**_BASE_S, "unreachable_as_text": 0}
    out = _run_summary_text(s)
    assert "本文が読めず対象外" not in out
