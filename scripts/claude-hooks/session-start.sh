#!/usr/bin/env bash
# Claude Code SessionStart フック: セッション開始時に基点・未コミット状態・worktree 一覧・ポート状況を
# 1画面で見せる。セッション開始を阻害してはならないため、途中の失敗は無視して常に exit 0 で終える。
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" 2>/dev/null || exit 0

# 時間制限は GNU の timeout があればそれ、無ければ Python（scripts/lib/portable_tools.py）。どちらも
# 無ければ制限なしで実行する（フックの目的は表示だけ・失敗は無視する）。
_limited() {  # SEC CMD...
  local sec="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$sec" "$@"
  elif command -v python3 >/dev/null 2>&1; then
    python3 "$ROOT/scripts/lib/portable_tools.py" run-limited "$sec" "$@"
  else
    "$@"
  fi
}

echo "=== git 状態 ==="
_limited 15 git fetch origin --quiet 2>/dev/null

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
echo "ブランチ: ${branch:-不明}"

if git rev-parse --verify main >/dev/null 2>&1 && git rev-parse --verify origin/main >/dev/null 2>&1; then
    counts="$(git rev-list --left-right --count main...origin/main 2>/dev/null)"
    ahead="$(echo "$counts" | awk '{print $1}')"
    behind="$(echo "$counts" | awk '{print $2}')"
    echo "main vs origin/main: ahead ${ahead:-?} / behind ${behind:-?}"
fi

dirty="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
echo "未コミット変更: ${dirty:-0} 件"

echo
echo "=== worktree 一覧 ==="
git worktree list 2>/dev/null

if [ -x "$ROOT/scripts/check-ports.sh" ]; then
    echo
    echo "=== ポート状況 ==="
    _limited 20 "$ROOT/scripts/check-ports.sh" 2>&1
fi

exit 0
