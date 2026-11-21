#!/usr/bin/env bash
# ローカル LLM（Ollama）で回答が浅い・エラーになる原因の切り分け（読むだけ。書くのは詳細の txt 1 つ）。
# どこからでも叩ける: bash <Slouchの置き場所>/scripts/local_llm_report.sh [引数]
#   --users-dir <dir>   users のフォルダ（既定 SHERPA_USERS_DIR。無ければ <置き場所>/data/users）
#   --conv <番号>[,<番号>]  見る会話（省略で新しい順に --turns 本。既定 5）
#   --ctx <数>          Ollama の文脈長（Ollama の機械で `ollama ps` の CONTEXT の数字）
#   --ollama-url <url>  Ollama の接続先（既定 OLLAMA_URL。無ければ http://localhost:11434）
#   --ollama-log <path> Ollama のログ（既定 ~/.ollama/logs/server.log）
#   --probe             Ollama に小さい依頼を送って道具の名前と推論の強さを試す
# 守ること: 質問・回答の文・資料のファイル名・パス・ホスト名・利用者名・鍵を出力に出さない（道具の名前と数字だけ）。
unset POSIXLY_CORRECT CDPATH
_src="${BASH_SOURCE[0]:-$0}"
ROOT="$(cd -P "$(dirname "$_src")/.." 2>/dev/null && pwd)" || { echo "置き場所が開けません" >&2; exit 2; }

if [ -f "$ROOT/scripts/run-common.sh" ]; then
  # shellcheck source=scripts/run-common.sh
  . "$ROOT/scripts/run-common.sh"
  if command -v sherpa_env_default >/dev/null 2>&1; then
    sherpa_env_default SHERPA_USERS_DIR OLLAMA_URL
  fi
fi

if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi
exec "$PYTHON_BIN" "$ROOT/scripts/local_llm_report.py" "$@"
