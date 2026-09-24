#!/usr/bin/env python3
"""敵対レビュー（RV）の定型プロンプトを組み立てる。

`docs/20-開発ハーネス.md` §3（敵対レビュー）・§4（RV 台帳）が正典。標準ライブラリのみを使い、
同じ引数なら同じ文字列を返す（決定的出力＝レビュー結果の再現性・台帳との突き合わせのため、
日時などの非決定要素を一切埋め込まない）。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

_DEFAULT_PREMISES = "実行環境は閉域 LAN、利用者は最大 20 人程度、単一 worker で動く。"

_OUTPUT_FORMAT = (
    "指摘は 1 件ごとに次の 1 行の形式で書く。\n"
    "`[高/中/低] ファイル:行 — 症状 — 再現 — 最小修正`\n"
    "再現は実際にコードを読んで根拠を示す（推測で書かない・実在しないコードを指摘しない）。\n"
    "一覧の末尾に必ず「クローズ可」か「要修正」のどちらかを 1 行で書く。指摘が無いときは一覧の"
    "代わりに「指摘なし」と明記したうえで「クローズ可」と書く。"
)

_MANNER = (
    "作業ツリーは変更しない（読み取り専用でレビューする）。実害のある欠陥（契約違反・データ破壊・"
    "セキュリティ上の問題）だけを指摘し、指摘ごとに最小の是正を添える。設計の好みの範囲の改善提案は"
    "書かない。"
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="敵対レビュー（RV）の定型プロンプトを標準出力に組み立てる。")
    p.add_argument("--target", required=True, help="対象の git 範囲または SHA（例: HEAD・abc123..def456）")
    p.add_argument("--proposal", help="設計の裁定をまとめた文書へのパス（任意）")
    p.add_argument("--ledger", help="RV 台帳ファイルへのパス（任意・指定すると全文を同梱する）")
    p.add_argument("--intent", required=True, help="このスライスでのユーザーの意図・完成形の要約（必須）")
    p.add_argument("--round", type=int, default=1, help="何巡目のレビューか（既定 1）")
    p.add_argument("--premises", default=_DEFAULT_PREMISES,
                    help="前提（既定: 閉域 LAN・最大 20 人程度・単一 worker）")
    p.add_argument("--focus", help="レビュアーに確認してほしい事項（任意）")
    p.add_argument("--scope", help="対象ファイルの説明（任意）")
    p.add_argument("--worktree", help="差分確認に使う切り離した worktree のパス（任意）")
    return p.parse_args(argv)


def build_prompt(args: argparse.Namespace) -> str:
    """`args` から定型プロンプトを組み立てる（純関数・副作用なし＝決定的出力の実体）。"""
    sections: list[str] = []

    sections.append(
        f"あなたは敵対レビュアー（{args.round} 巡目）です。差分に含まれる実害のある欠陥だけを指摘し、"
        "最小の是正を促してください。この依頼の外の会話の文脈は見えないため、以下に書かれていない"
        "情報を推測で補わないでください。"
    )

    target_lines = [
        "## 対象",
        f"対象範囲: `{args.target}`",
    ]
    # 範囲（A..B）は diff、単一対象は show。マージコミットは既定の combined diff だと差分が空に
    # 見えるので first-parent の差分で示す（通常コミットでは出力は変わらない）。
    if ".." in args.target:
        diff_cmd = f"git diff {args.target}"
    else:
        diff_cmd = f"git show --diff-merges=first-parent {args.target}"
    target_lines.append(f"`{diff_cmd}` でこの対象の差分を確認すること。")
    target_lines.append("作業ツリーは変更しない（読み取り専用でレビューする）。")
    if args.worktree:
        # レビュアーの cwd は対象 worktree に切り替わり得るので、呼び出し元基準の絶対パスに解決して埋め込む。
        wt = str(pathlib.Path(args.worktree).resolve())
        target_lines.append(
            f"差分は `{wt}` の切り離した worktree で確認する"
            f"（`{diff_cmd.replace('git ', f'git -C {wt} ', 1)}`）。メインのチェックアウトは読まない。"
        )
    if args.scope:
        target_lines.append(f"対象ファイルの説明: {args.scope}")
    sections.append("\n".join(target_lines))

    canon_lines = ["## 正典", "リポジトリの CLAUDE.md と docs/ 配下の正典文書に矛盾しないかを確認すること。"]
    if args.proposal:
        canon_lines.append(f"設計の裁定は次の文書に従う: `{args.proposal}`")
    sections.append("\n".join(canon_lines))

    sections.append("## 前提\n" + args.premises)
    sections.append("## 作法\n" + _MANNER)

    sections.append(
        "## 意図の要約\n" + args.intent + "\n\n"
        "意図の要約に照らして、完成形からの逸脱（スコープ外の変更・意図と異なる実装）も指摘の対象とする。"
    )

    if args.ledger:
        ledger_text = pathlib.Path(args.ledger).read_text(encoding="utf-8")
        sections.append(
            "## 台帳（先に読むこと）\n"
            "以下は既存の RV 台帳の全文である。指摘を書く前に先に読むこと。台帳で「採用」「受容」"
            "「却下」に分類済みの項目は、新しい事実（新しいコード・新しい根拠）が無い限り再指摘しない。"
            "再指摘する場合は『台帳 #<番号> に対して』と番号を付け、何が新しい事実かを示すこと。\n\n"
            + ledger_text.rstrip("\n")
        )

    if args.focus:
        sections.append("## 確認事項\n" + args.focus)

    sections.append("## 出力形式\n" + _OUTPUT_FORMAT)

    return "\n\n".join(sections) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    sys.stdout.write(build_prompt(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
