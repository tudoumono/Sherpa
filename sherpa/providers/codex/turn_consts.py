"""1 ターンの段が共有する定数と補助（依存の無い末端のモジュール）。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

from pathlib import Path

# skills_base。`sherpa/` 配下を指す（本モジュールは `sherpa/providers/codex/` にあるため `parents[2]`）。
_SKILLS_BASE = Path(__file__).resolve().parents[2] / "skills_base"

# `--output-schema` に渡す固定スキーマファイル（同じディレクトリに同梱）。v2 は v1 の3キーに `claims`（確定/推定/不明の主張配列）を足した版で、`SHERPA_CODEX_OUTPUT_SCHEMA` の値で選ぶ。
_OUTPUT_SCHEMA_PATH = Path(__file__).resolve().parent / "output_schema.json"
_OUTPUT_SCHEMA_PATH_V2 = Path(__file__).resolve().parent / "output_schema_v2.json"

_MCP_SIDECAR_NAME = ".mcp_sidecar.jsonl"  # sandbox 有効時は codex_home 配下（run_dir の外）
# 資料作成（author）専用の推論の強さ。通常レンズの基準値（管理画面）とは別軸。
_REASONING_AUTHOR = "medium"

# 成果物の move／台帳登録に1件でも失敗したとき、回答本文の末尾に付ける固定文。
_CREATED_FILES_FAILURE_NOTE = "（作成したファイルの一部を保存できませんでした。管理者に確認してください）"
# Marp の書き出し（HTML・PDF・PPTX）が失敗したとき。Markdown の原稿は保存されている。
_MARP_FAILURE_NOTE = "スライドの書き出し（HTML・PDF・PPTX への変換）に失敗しました。Markdown の原稿だけを保存しています。"
# 壁時計上限（`SHERPA_CODEX_WALL_CLOCK_LIMIT_S`）で打ち切った時に headline へ付ける注記。
_WALL_CLOCK_LIMIT_NOTE = "（時間の上限に達したため、ここまでの結果で打ち切りました）"


def _int_or_none(raw) -> int | None:
    """`_mcp_budget_env` の文字列値（数値の文字列、または欠落時の `"-"`）を `activity.settings`（JSON 数値）へ変換する。数値でなければ None（推定で埋めない）。"""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
