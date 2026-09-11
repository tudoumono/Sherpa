#!/usr/bin/env bash
# 並列レーン開発の収束表を出す。`git worktree list --porcelain` を読み取り専用で走査し、
# main 本体と `tmp/worktrees/rv-*`（RV 用の切り離し worktree）を除く各 worktree について
# 1行のレーン状態を出す。最終ゲート結果は追わない（メインが `make gate-slice` を自分で
# 実行した出力を別途貼る・docs/20-開発ハーネス.md §5・.claude/skills/lane/SKILL.md ④）。
set -u

MAIN_SHA="$(git rev-parse main 2>/dev/null)"
if [ -z "$MAIN_SHA" ]; then
  echo "main ブランチが解決できません" >&2
  exit 1
fi

echo "| レーン(ブランチ) | パス | 基点=main? | ahead/behind main | 変更ファイル数 | 未コミット件数 | 状態 |"
echo "|---|---|---|---|---|---|---|"

# porcelain 出力はレコードを空行で区切る。1レコードずつ読み、path/branch を抽出する。
worktree=""
branch=""
is_first_record=1

_emit() {
  local path="$1" branch="$2"
  [ -z "$path" ] && return 0
  case "$path" in
    */tmp/worktrees/rv-*) return 0 ;;
  esac

  local lane_label="${branch:-detached}"

  local merge_base status_baseline behind_from_base baseline_stale
  merge_base="$(git -C "$path" merge-base main HEAD 2>/dev/null)"
  if [ -z "$merge_base" ]; then
    status_baseline="不明"
    baseline_stale=1
  elif [ "$merge_base" = "$MAIN_SHA" ]; then
    status_baseline="ok"
    baseline_stale=0
  else
    behind_from_base="$(git -C "$path" rev-list --count "$merge_base".."$MAIN_SHA" 2>/dev/null)"
    status_baseline="behind ${behind_from_base:-?}"
    baseline_stale=1
  fi

  local lr behind ahead
  lr="$(git -C "$path" rev-list --left-right --count main...HEAD 2>/dev/null)"
  behind="$(echo "$lr" | awk '{print $1}')"
  ahead="$(echo "$lr" | awk '{print $2}')"
  [ -z "$behind" ] && behind="?"
  [ -z "$ahead" ] && ahead="?"
  local ahead_behind="+${ahead}/-${behind}"

  local changed_files
  changed_files="$(git -C "$path" diff --name-only main...HEAD 2>/dev/null | wc -l | tr -d ' ')"

  local uncommitted
  uncommitted="$(git -C "$path" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"

  local state
  if [ "${uncommitted:-0}" -gt 0 ]; then
    state="作業中"
  elif [ "${ahead:-0}" = "0" ]; then
    state="空"
  elif [ "$baseline_stale" = "1" ]; then
    state="要rebase"
  elif git -C "$path" merge-tree --write-tree main HEAD >/dev/null 2>&1; then
    state="ゲート待ち"
  else
    state="衝突"
  fi

  echo "| ${lane_label} | ${path} | ${status_baseline} | ${ahead_behind} | ${changed_files} | ${uncommitted} | ${state} |"
}

while IFS= read -r line; do
  if [ -z "$line" ]; then
    if [ "$is_first_record" = 1 ]; then
      is_first_record=0
    else
      _emit "$worktree" "$branch"
    fi
    worktree=""
    branch=""
    continue
  fi
  case "$line" in
    "worktree "*) worktree="${line#worktree }" ;;
    "branch "*) branch="${line#branch refs/heads/}" ;;
  esac
# porcelain 出力の末尾には空行が無いため、`echo` で仕上げの空行を足して最後のレコードも
# 上のループ内の空行分岐で拾わせる（ループ外に別処理を持たない一本化）。
done < <(git worktree list --porcelain; echo)
