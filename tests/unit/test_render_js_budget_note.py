"""STOP-1: `web/chat/render.js` の `budgetNoteHTML`（調査予算到達の明示表示）を実ファイルから
抽出して node で直接実行し固定する。

`budgetNoteHTML` は DOM/`Sherpa` 名前空間に依存しない純粋関数だが、`esc`（`web/common.js::
_sherpaEsc`）だけを参照する——ファイル全体を評価せず（`import`/`Sherpa` グローバル前提の副作用が
あり Node 単体では動かせない）、該当定義だけを文字列で切り出し、`esc` の実体も実ファイルから
切り出して結合する（手書きで複製すると実装とテストが乖離しうるため常に実ファイルを読む・
`tests/unit/test_ingest_js_reason_text.py` と同じ流儀）。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
RENDER_JS = ROOT / "web" / "chat" / "render.js"
COMMON_JS = ROOT / "web" / "common.js"


def _extract_esc() -> str:
    """`web/common.js` から `const _sherpaEsc = ...;` の1文だけを切り出す。

    末尾探索は素朴な `str.index(";", start)` を使わない——エスケープ先の文字列自体
    （例: `'&amp;'`）に `;` を含むため、最初の `;` で切ると文がエスケープ表内で途切れる。
    この定義の直後は空行（`\\n\\n`）のため、そこまでを1文として切り出す。
    """
    src = COMMON_JS.read_text(encoding="utf-8")
    start = src.index("const _sherpaEsc")
    end = src.index("\n\n", start)
    return src[start:end] + "\nconst esc = _sherpaEsc;"


def _extract_budget_note_snippet() -> str:
    """`const BUDGET_EXHAUSTED_STOP_REASONS = ...` から `function budgetNoteHTML(...) {...}` の
    閉じ括弧までを切り出す。"""
    src = RENDER_JS.read_text(encoding="utf-8")
    start = src.index("const BUDGET_EXHAUSTED_STOP_REASONS")
    fn_start = src.index("function budgetNoteHTML")
    end = src.index("\n}", fn_start) + len("\n}")
    snippet = src[start:end]
    assert "BUDGET_NOTE_TEXT" in snippet, "抽出範囲に BUDGET_NOTE_TEXT が含まれていない"
    return snippet


def _run_budget_note(evidence_packet) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node が見つからない（budgetNoteHTML 振る舞いテストは対象外）")
    script = _extract_esc() + "\n" + _extract_budget_note_snippet() + (
        f"\nconsole.log(JSON.stringify(budgetNoteHTML({json.dumps(evidence_packet)})));"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"node 実行に失敗:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.parametrize("stop_reason", ["turns_exhausted", "budget_exceeded", "tools_per_turn_exceeded"])
def test_budget_note_shown_for_budget_exhausted_stop_reasons(stop_reason):
    out = _run_budget_note({"stop_reason": stop_reason})
    assert "budget-note" in out
    assert "調査の上限に達したため、途中までの結果で答えています" in out
    assert "続きを調べて" in out


@pytest.mark.parametrize("stop_reason", [
    "no_tool_calls", "evaluation_sufficient", "evaluation_blocked",
    "evidence_verification_failed", "refusal", "truncated", "content_filtered", "unknown",
])
def test_budget_note_not_shown_for_non_budget_stop_reasons(stop_reason):
    """通常完了・根拠不足・出力上限・安全フィルタ・回答拒否・理由不明は対象外
    （「範囲を絞る／続きを調べて」という案内が当てはまらない別カテゴリのため）。"""
    assert _run_budget_note({"stop_reason": stop_reason}) == ""


def test_budget_note_not_shown_without_evidence_packet():
    """非 agentic（Evidence Packet が無い）ターンでは注記を出さない。"""
    assert _run_budget_note(None) == ""
    assert _run_budget_note({}) == ""
