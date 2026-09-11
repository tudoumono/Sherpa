#!/usr/bin/env bash
# Codex CLI（read-only サンドボックス）で RV（敵対レビュー）を実行する。
# `docs/20-開発ハーネス.md` §3 が正典。プロンプトは引数または標準入力で渡す。
# usage: rv_codex.sh -C <worktree> -n <name> [-m <model>] ["<prompt>"]
set -euo pipefail

usage() { echo 'usage: rv_codex.sh -C <worktree> -n <name> [-m <model>] ["<prompt>"]' >&2; exit 2; }

DIR=""; NAME=""; MODEL=""
while getopts ":C:n:m:" opt; do
  case "$opt" in
    C) DIR="$OPTARG" ;;
    n) NAME="$OPTARG" ;;
    m) MODEL="$OPTARG" ;;
    *) usage ;;
  esac
done
shift $((OPTIND-1))

[ -n "$DIR" ] || usage
[ -n "$NAME" ] || usage

# codex CLI が使えない環境（開発者ローカルの外）向けの fail-closed。
# レビュアーの役割は Codex 実装専用ではない（docs/20 §2）ので、無い場合は Claude 実装へ誘導する。
if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI が見つかりません。Claude 実装のレビュアー（adversarial-reviewer エージェント）を使ってください。" >&2
  exit 2
fi

if [ "$#" -gt 0 ]; then
  PROMPT="$*"
else
  PROMPT="$(cat)"
fi
[ -n "$PROMPT" ] || usage

OUT_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/sherpa-rv/$NAME"
mkdir -p "$OUT_DIR"
# 前回実行の最終メッセージが残っていると、今回が失敗した場合に古い結果を新しい結果と誤読する
# （呼び出し側は stdout のみを見る）。今回分を書く前に必ず消す。
rm -f "$OUT_DIR/last.txt"

ARGS=(exec --json --skip-git-repo-check -C "$DIR" -s read-only -o "$OUT_DIR/last.txt")
[ -n "$MODEL" ] && ARGS+=(-m "$MODEL")

{
  echo "name=$NAME"
  echo "workdir=$DIR"
  echo "model=${MODEL:-default}"
  echo "started=$(date -Iseconds)"
} > "$OUT_DIR/meta"

set +e
codex "${ARGS[@]}" "$PROMPT" </dev/null >>"$OUT_DIR/events.jsonl" 2>>"$OUT_DIR/stderr.log"
RC=$?
set -e

{
  echo "exit_code=$RC"
  echo "finished=$(date -Iseconds)"
} >> "$OUT_DIR/meta"

if [ "$RC" -ne 0 ] || [ ! -s "$OUT_DIR/last.txt" ]; then
  echo "レビュー結果なし（exit=${RC}・ログ: $OUT_DIR/stderr.log）" >&2
  if [ "$RC" -ne 0 ]; then
    exit "$RC"
  fi
  exit 1
fi

cat "$OUT_DIR/last.txt"
exit 0
