#!/usr/bin/env bash
# 新しい貢献者向けの開発環境の入口（1コマンド＝ make dev-setup）。
#
# やること: venv 作成＋開発依存インストール（requirements-dev.txt・変更検出時だけ再インストール）
#           → git フック有効化（make hooks と同じ）→ .env 雛形の用意（無ければ作るだけ・中身は表示しない）
#           → 次にやることの案内（ストア起動・レーン分離テスト）。
# 既にあるものはスキップする（何度実行しても安全＝冪等・既存の環境を壊さない）。
# ストア（Postgres/Neo4j/ES）はここでは起動しない（`make up`・`make bootstrap` は別入口。
# CONTRIBUTING.md 参照）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON_BIN:-python3}"
VENV="${SHERPA_VENV:-$ROOT/.venv}"

if ! command -v "$PY" >/dev/null 2>&1; then
  cat >&2 <<EOF
✗ Python3 が見つかりません（コマンド: ${PY}）。
  Ubuntu / WSL2 での導入例:
    sudo apt update && sudo apt install -y python3 python3-venv python3-pip
EOF
  exit 1
fi

echo "=== Python 環境 (${VENV}) ==="
if [ ! -x "$VENV/bin/python" ]; then
  echo "venv を作成します: $VENV"
  "$PY" -m venv "$VENV"
else
  echo "venv は既にあります（スキップ）"
fi

# ハッシュ計算は sha256sum（stock macOS に無い）を避け、venv の python で行う（scripts/start.sh と同じ手口）。
# requirements-dev.txt は requirements.txt を `-r` で含むため、両方の変更を1本のハッシュで検出できる。
REQ_HASH_FILE="$VENV/.requirements-dev.sha256"
req_hash() {
  "$VENV/bin/python" -c \
    'import hashlib,sys; print(hashlib.sha256(b"".join(open(f,"rb").read() for f in sys.argv[1:])).hexdigest())' \
    requirements.txt requirements-dev.txt constraints.txt
}
CUR_HASH="$(req_hash)"
if [ ! -f "$REQ_HASH_FILE" ] || [ "$(cat "$REQ_HASH_FILE" 2>/dev/null || true)" != "$CUR_HASH" ]; then
  echo "開発依存をインストールします（requirements-dev.txt -c constraints.txt の変更を検出）..."
  "$VENV/bin/python" -m pip install -r requirements-dev.txt -c constraints.txt
  echo "$CUR_HASH" > "$REQ_HASH_FILE"
else
  echo "開発依存は最新です（requirements-dev.txt 変更なし・インストールを省略）。"
fi

echo
echo "=== .env 雛形 ==="
if [ -f .env ]; then
  echo ".env は既にあります（スキップ・中身は変更しません）"
else
  cp .env.example .env
  echo ".env.example から .env を作成しました（AI キー等は起動後に管理画面か .env で設定）"
fi

echo
echo "=== git フック (core.hooksPath) ==="
git config core.hooksPath scripts/git-hooks
echo "core.hooksPath=scripts/git-hooks（post-checkout: agent worktree の基点をローカル main へ揃える）"

echo
echo "=== AI エージェントの記憶の置き場（個人ごと・git 管理外） ==="
mkdir -p .claude/agent-memory/doc-writer .claude/agent-memory/feature-implementer .claude/agent-memory/light-task-runner
echo ".claude/agent-memory/ を用意しました（既にあれば何もしません）"

echo
echo "=== Claude Code フックの雛形 ==="
if [ -f .claude/settings.json ]; then
  echo ".claude/settings.json は既にあります（スキップ・上書きしません）"
else
  echo "雛形: .claude/settings.example.json（.claude/settings.json は git 管理外＝各自コピーして使う）"
  echo "  cp .claude/settings.example.json .claude/settings.json"
fi

cat <<'EOF'

=== 準備完了。次のコマンド ===
  .venv/bin/python -m pytest tests/unit -m unit -q   # 単体テスト（外部サービス不要・まず確認）
  make up                                            # Postgres/Neo4j/ES を起動（Docker 必須・初回のみ待つ）
  make bootstrap                                      # 作業ディレクトリ用意・ストア起動待ち（make up の後）
  make start                                          # アプリを起動（http://127.0.0.1:8000/ui/chat.html）

複数の作業を並行して進めるときは、レーンごとに DB/world を分離する scripts/gate-lane.sh を使います
（詳しくは CONTRIBUTING.md・docs/20-開発ハーネス.md §5・.claude/skills/lane/SKILL.md）。
EOF
