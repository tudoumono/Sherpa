"""1 つの会話の各ターンの活動記録（`answer["activity"]`）を数字だけで表示する（`make turn-activity CONV=<番号>`）。

質問・回答の本文・会話の題名・参照した資料・ツールの引数は DB から取得しない（SQL で activity と、
終了理由・所要・台帳の件数などの数字と閉じた語彙の欄だけを選んで読む。activity 自体も本文や資料名を
持たない契約）。台帳は完了・継続回数・状態別の件数だけを読み、項目の id（モデルが付けた名前）は読まない。
activity の文字列（ツール名・未解析の種類・モデル名・設定値・エラーコード）は識別子の形のものだけを表示し、
それ以外は中身を出さず「（その他）」にまとめる。版（VERSION＋git の短い SHA・利用者の本文ではない）は形が想定と
違っても見当が付くよう、英数字と . + _ - 以外を ? に置き換えて出す。activity は Codex 経路で 0.13.1 以降に保存した
ターンにだけある。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))   # scripts/ から直接起動しても sherpa を読めるように（world_admin.py と同じ）

# 表示規則（識別子の形の文字列だけを出す・ツール/エージェントの畳み込み）は
# 管理者の利用明細エクスポート（sherpa/usage_export.py）と共有する単一の真実源。
from sherpa.turn_activity_format import (  # noqa: E402,F401
    _IDENT,
    _OTHER,
    _TOP_TOOLS,
    _UNPARSED_KEY,
    _VERSION_UNSAFE,
    _WORD,
    _agent_lines,
    _kib,
    _n,
    _safe,
    _sec,
    _version,
    format_turn,
    label_agents,
    merge_tools,
)


def _rows(conversation_id: int) -> list[dict]:
    # 読むだけの道具なので、スキーマを整える _ensure()（DDL）は呼ばない。
    from sherpa.store.db import _connect
    with _connect() as c:
        return c.execute(
            "SELECT id, created_at, answer->'activity' AS activity, answer->>'stop_kind' AS stop_kind, "
            "answer->>'codex_error_code' AS codex_error_code, "
            "CASE WHEN (answer->>'duration_ms') ~ '^[0-9]+$' THEN (answer->>'duration_ms')::bigint END "
            "AS duration_ms, jsonb_build_object("
            "  'complete', answer->'investigation'->'complete', "
            "  'continuations', answer->'investigation'->'continuations', "
            "  'counts', answer->'investigation'->'counts') AS investigation "
            "FROM messages WHERE conversation_id=%s AND role='assistant' AND answer IS NOT NULL ORDER BY id",
            (conversation_id,),
        ).fetchall()


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print("使い方: make turn-activity CONV=<会話番号>", file=sys.stderr)
        return 2
    conv = int(argv[1])
    rows = _rows(conv)
    if not rows:
        print(f"会話 {conv}: 回答の記録がありません")
        return 1
    print(f"会話 {conv}（回答 {len(rows)} 件）")
    for row in rows:
        for line in format_turn(dict(row)):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
