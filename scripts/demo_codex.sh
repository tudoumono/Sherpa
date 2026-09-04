#!/usr/bin/env bash
# Sherpa MVP — M0 demo (§9 M0 の受け入れ): Codex が workspace で走り、
# kb を「読めるが書けない」ことを確認する（権限/サンドボックスの最小実証）。
#   - sandbox=workspace-write ＋ -C workspace → 書込は workspace のみ
#   - kb は workspace の外 → 読めるが書けない（要件どおり）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
sherpa_source_dotenv   # 明示指定 ＞ .env ＞ 既定（読み方は run-common に一本化）

VER="${SHERPA_VERSION:-v1}"
UID_="${SHERPA_UID:-admin}"
WS="$ROOT/data/users/$UID_/workspace"
KB="$ROOT/data/kb"

# ingest 役として読取確認用のマーカーを KB に配置（人手＝書込許可されている取り込み側の代理）
mkdir -p "$KB/md/$VER"
echo "# demo: KB は read-only で読めるはず" > "$KB/md/$VER/_demo.md"

echo "== Codex M0 demo =="
echo "workspace = $WS  (writable)"
echo "kb        = $KB  (read-only 期待)"
echo

codex exec --json --skip-git-repo-check \
  -s workspace-write -C "$WS" \
  "次を順に行い結果を1段落で報告して:
   1) $KB/md/$VER/_demo.md を読み、本文を表示する（読めるはず）。
   2) $KB/md/$VER/should_fail.md の作成を試み、書込が拒否されたか報告する（kb は read-only であるべき）。
   3) $WS/outputs/ok.txt に 'ok' を書く（workspace は書込可のはず）。"

echo
echo "確認: data/users/$UID_/workspace/outputs/ok.txt が作成され、kb 側 should_fail.md は作られていないこと。"
