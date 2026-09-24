#!/usr/bin/env python3
"""並列レーン開発の定型委譲文を組み立てる。

`docs/20-開発ハーネス.md` §2（役割と入出力）・§5（ゲート）・§8（作業の作法）が正典。標準ライブラリ
のみを使い、同じ引数なら同じ文字列を返す（決定的出力＝台帳・報告との突き合わせのため、日時などの
非決定要素を一切埋め込まない）。
"""
from __future__ import annotations

import argparse
import sys

_COAUTHOR = "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"

_BASELINE_CHECK = (
    "作業開始前に基点を確認する。`git merge-base main HEAD` の出力が `git rev-parse main` の出力と"
    "一致しない場合は、基点が古い（`.claude/worktrees/agent-*` の post-checkout フックが効いていない、"
    "またはローカル main が push 前に更新された）。その場合は `git reset --hard main` してから"
    "作業ブランチを作り直す。"
)

_REPORT_FORMAT = (
    "完了報告には次を必ず含める（無いものは未完了として扱う）。\n"
    "- 変更ファイル一覧。\n"
    "- 実行した検証コマンドと pytest の要約行（例: `12 passed in 3.1s`）と終了コード。\n"
    "- スモークやサンプル実行の数値は「SMOKE」と明記し、テスト結果として書かない。\n"
    "- worktree のパスとコミット SHA。\n"
    "- 迷った点。"
)

_TEST_DISCIPLINE = (
    "モックは外部境界だけに置く。テストを削除・スキップして緑にすることを禁止する"
    "（`docs/20-開発ハーネス.md` §6 テスト規範が正典）。pytest は Bash ツールの timeout を明示"
    "（例: 600000 ms）して同期で走らせ、背景化して完了前に報告しない。共有 DB を使うテストは"
    "他セッションと競合するため、長い群は `scripts/gate-lane.sh <worktree> <lane> --only ...` で"
    "レーン別 DB に分離して走らせる。"
)

_COMMIT_RULE = (
    f"完了時は 1 コミットにまとめ、コミットメッセージの末尾に `{_COAUTHOR}` を付ける。"
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="並列レーン開発の定型委譲文を標準出力に組み立てる。")
    p.add_argument("--lane", required=True, help="レーン名（[a-z0-9-]）")
    p.add_argument("--branch", required=True, help="作業ブランチ名（例: <topic>/<lane>）")
    p.add_argument("--goal", required=True, help="このレーンの目的")
    p.add_argument("--files", required=True, help="対象ファイルの説明")
    p.add_argument("--accept", required=True, help="受け入れ条件")
    p.add_argument("--avoid", required=True, help="やらないこと")
    p.add_argument("--conventions", help="既存規約への参照（任意）")
    p.add_argument("--tests", help="回すべきテストの指示（任意）")
    return p.parse_args(argv)


def build_prompt(args: argparse.Namespace) -> str:
    """`args` から定型委譲文を組み立てる（純関数・副作用なし＝決定的出力の実体）。"""
    sections: list[str] = []

    sections.append(
        f"あなたはレーン `{args.lane}`（作業ブランチ `{args.branch}`）の実装担当です。この依頼の外の"
        "会話の文脈は見えないため、以下に書かれていない情報を推測で補わないでください。"
    )

    sections.append("## 基点確認（着手前に必ず実行）\n" + _BASELINE_CHECK)

    sections.append(f"## 作業ブランチ\n`{args.branch}` を作業ブランチとして使う。")

    sections.append("## 目的\n" + args.goal)

    sections.append("## 対象\n" + args.files)

    conv_lines = ["## 既存規約"]
    if args.conventions:
        conv_lines.append(args.conventions)
    conv_lines.append(
        "リポジトリの CLAUDE.md と docs/ 配下の正典文書に矛盾しないこと。コメントは現在の契約・"
        "不変条件・非自明な理由のみ（修正の経緯や実装日はコミットメッセージへ）。"
    )
    sections.append("\n".join(conv_lines))

    sections.append("## 受け入れ条件\n" + args.accept)

    sections.append("## やらないこと\n" + args.avoid)

    if args.tests:
        sections.append("## テスト\n" + args.tests)

    sections.append("## テストの作法\n" + _TEST_DISCIPLINE)

    sections.append("## コミット\n" + _COMMIT_RULE)

    sections.append("## 報告形式\n" + _REPORT_FORMAT)

    return "\n\n".join(sections) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    sys.stdout.write(build_prompt(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
