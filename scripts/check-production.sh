#!/usr/bin/env bash
# Lightweight production preflight. It avoids touching data and does not start services.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${SHERPA_ENV_FILE:-$ROOT/.env.production}"
# 読み方は scripts/run-common.sh に一本化（明示指定 ＞ env ファイル ＞ 既定）。run-common は
# source 時に SHERPA_ENV_FILE（無ければ .env）を見るため、本スクリプトの既定（.env.production）を先に確定させる。
export SHERPA_ENV_FILE="$ENV_FILE"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
failures=0

fail() {
  echo "NG: $*" >&2
  failures=$((failures + 1))
}

warn() {
  echo "WARN: $*" >&2
}

ok() {
  echo "OK: $*"
}

if [ ! -f "$ENV_FILE" ]; then
  fail "env file not found: $ENV_FILE (copy .env.example first, then fill in its \"0. 本番チェックリスト\" section)"
else
  sherpa_source_dotenv "$ENV_FILE"
  ok "env file: $ENV_FILE"
fi

command -v "${PYTHON_BIN:-python3}" >/dev/null 2>&1 || fail "python not found: ${PYTHON_BIN:-python3}"

# ENV-ONE（env 例の1本化・2026-09-03）: 選び間違い（閉域網へ dev 用 env をそのまま持ち込む事故）を
# 人の注意でなく機械のガードで塞ぐ。ここから3点は sherpa/api.py の起動時ガード
# （`_warn_change_me_placeholders`／`_warn_default_admin_password`）と同じ判定を prod-check 側でも
# 事前に行う（DB/サーバ起動なしで気付けるように）。

# a) CHANGE_ME プレースホルダ（`.env.example` 冒頭「0. 本番チェックリスト」節のコメント値）が
# そのまま env に残っていないか。値だけを見る（キー名に CHANGE_ME を含む変数は通常無い）。
_change_me_lines="$(env | grep -E '^[A-Za-z_][A-Za-z0-9_]*=.*CHANGE_ME' || true)"
if [ -n "$_change_me_lines" ]; then
  _change_me_keys="$(printf '%s\n' "$_change_me_lines" | sed -E 's/=.*$//' | sort -u | tr '\n' ' ')"
  fail "env にプレースホルダ（CHANGE_ME）が残っています: ${_change_me_keys% } (.env.example の「0. 本番チェックリスト」節を埋め忘れていませんか？)"
else
  ok "env に CHANGE_ME プレースホルダは残っていません"
fi

# b) SHERPA_ADMIN_PASSWORD が未設定（sherpa/auth.py::DEFAULT_ADMIN_PASSWORD 参照・明示設定なら
#    開発既定と同値でも許容＝閉域前提＋初回ログインで変更強制・ユーザー決定 2026-07-10/2026-09-03 復元・
# ドリフトしたらそちらが正）のまま。明示的に変更しない限り閉域網でも既定パスワードで稼働してしまう。
_ADMIN_DEV_DEFAULT="Sherpa2026!"
if [ -z "${SHERPA_ADMIN_PASSWORD:-}" ]; then
  fail "SHERPA_ADMIN_PASSWORD が未設定です（未設定だと開発既定 ${_ADMIN_DEV_DEFAULT} が使われます。実際の値を設定してください）"
elif [ "${SHERPA_ADMIN_PASSWORD}" = "$_ADMIN_DEV_DEFAULT" ]; then
  ok "SHERPA_ADMIN_PASSWORD は開発既定と同値（明示設定・初回ログインで変更強制されます）"
else
  ok "SHERPA_ADMIN_PASSWORD is explicitly set"
fi

# c) SHERPA_ENV=production が設定されているか（`make serve`/`start.sh serve` は自身で立てるが、
# ここは事前点検＝env ファイル側に書いてあるかを見る。無くても本チェック自体は最後まで進める）。
_env_norm="$(printf '%s' "${SHERPA_ENV:-}" | tr '[:upper:]' '[:lower:]' | awk '{$1=$1};1')"
case "$_env_norm" in
  prod|production) ok "SHERPA_ENV=${SHERPA_ENV} (本番)" ;;
  *) fail "SHERPA_ENV=production が設定されていません（現在値: '${SHERPA_ENV:-}'）。本番ガード（fixtures拒否・AUTH_DISABLED無効化・CHANGE_ME/既定パスワード拒否等）が有効になりません" ;;
esac

# ポートの整合（compose 公開⇔アプリ接続先）と占有（他プロセス）。env ファイルは同じ SHERPA_ENV_FILE を見る。
if "$ROOT/scripts/check-ports.sh"; then
  ok "ports: compose/app consistent and not taken by others"
else
  fail "ports: mismatch or taken by another process (see table above; make check-ports)"
fi
command -v docker >/dev/null 2>&1 || warn "docker command not found (stores may run elsewhere)"

# OpenAI 互換の接続先（Azure OpenAI（`*.openai.azure.com`）や Private Link 経由のゲートウェイを
# 指すことがある）。
#
# env（=初回シードの候補値）と system_settings（=起動後の実効値）を**明示的にモード分離**する。
# env だけを検査すると「実際に使われている接続先」と食い違う preflight 結果になり得る（env は
# 初回起動時に一度だけ取り込まれ、以後は読まれない・sherpa/llm.py::openai_base_url 参照・初回起動が
# 既に済んだ環境で env ファイルを後から書き換えても実効値には反映されないため）。DB へ到達でき、
# かつマーカー（`openai_endpoint_seed_version`）があれば「system_settings 実効値モード」（DB の
# 現在値を検査）へ切り替える。DB 未起動／未到達（`make check-production` はサービス起動前に実行
# することが多い）・マーカーが無い（初回起動前）・python 依存未導入などは「env 候補モード」へ
# fail-safe する。

# scheme/host/port の検査ロジックを共通化（DB モード・env モードの両方から呼ぶ）。
# 引数: ラベル（メッセージ prefix）／scheme／host／port（port は空文字可）。
_check_openai_endpoint_host() {
  local label="$1" scheme="$2" host="$3" port="$4"
  if [ -z "$host" ]; then
    fail "$label の形式を読み取れません（scheme://host が必要です・解析できません）"
    return
  fi
  # $host は接続用の生ホスト（角括弧なし・check_production_openai_probe.py::_scheme_host_port 参照）。
  # getent／/dev/tcp へは常にこの生ホストを渡す。表示専用に IPv6（":" を含む）だけ角括弧を付けた
  # 別変数を用意する（角括弧付きの表示値をそのまま getent/tcp に渡すと、正当な IPv6 接続先が
  # 名前解決に失敗して hard fail する）。
  local display_host="$host"
  case "$host" in *:*) display_host="[$host]" ;; esac
  if [ "$scheme" != "https" ]; then
    fail "$label は https:// にしてください（APIキーを平文送信しないため）: $scheme://$display_host${port:+:$port}"
    return
  fi
  ok "$label scheme: $scheme://$display_host"
  # 名前解決（check-ports.sh::resolve_host と同じ発想。別プロセスとして実行される check-ports.sh の
  # 関数はこのシェルから直接呼べないため、判定ロジックだけをここに複製する）。
  if ! command -v getent >/dev/null 2>&1; then
    warn "getent が無いため $label のホスト名解決を確認できません"
    return
  fi
  if ! getent ahosts "$host" >/dev/null 2>&1 && ! getent hosts "$host" >/dev/null 2>&1; then
    fail "$label のホスト名 '$display_host' を名前解決できません（.env.example の例をそのまま有効化していませんか？）"
    return
  fi
  ok "$label host resolves: $display_host"
  # TCP 疎通は試みるだけ（read-only）。Private Link 等の閉域ゲートウェイは、この preflight の
  # 実行元（踏み台・CI 等）から到達できない構成でも正常なことがあるため、失敗は warn 止まり
  # （fail にしない・check-ports.sh の別ホスト到達検査とはここが異なる）。
  local tcp_port="${port:-443}"
  if command -v timeout >/dev/null 2>&1 \
     && timeout 3 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$host" "$tcp_port" >/dev/null 2>&1; then
    ok "$label reachable: $display_host:$tcp_port"
  else
    warn "$label の $display_host:$tcp_port に TCP 接続できません（3秒）。"\
"Private Link 等でこの preflight の実行元から到達できない構成なら問題ありません。到達できるはずの構成なら"\
" プロキシ/ファイアウォールの許可先（$display_host）を確認してください。"
  fi
}

# 接続先の検査（system_settings 実効値モード／env 候補モードの判定・検証本体とも）は
# scripts/check_production_openai_probe.py（単体テスト可能な独立モジュール・
# tests/unit/test_check_production_openai_probe.py 参照）に一本化した。
#
# env 候補モードも本番の起動時シード resolver（`sherpa.llm.openai_endpoint_seed_candidate`）を
# そのまま呼ぶ（bash 側で https/host/port だけを独自に検査する簡易チェックはしない＝未知
# kind/auth_header・Azure 等で base_url 欠落・userinfo/query 混入の検出は resolver に委ねる・
# 本番/azure_smoke.py と同じ検証を共有する）。env の値はこのプロセスが `os.environ` から直接読む
# ＝コマンドライン引数には一切載せない（`ps`/`/proc/<pid>/cmdline` 経由の露出を避ける）。
# `timeout` で全体に上限を掛ける（DB ホストが応答しない場合にこの preflight 自体が固まらないため）。
_openai_probe=""
if command -v "${PYTHON_BIN:-python3}" >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
  _openai_probe="$(timeout 5 "${PYTHON_BIN:-python3}" "$ROOT/scripts/check_production_openai_probe.py" 2>/dev/null)" \
    || _openai_probe=""
fi
_openai_status="$(printf '%s\n' "$_openai_probe" | sed -n '1p')"

if [ "$_openai_status" = "MARKER_FOUND" ]; then
  _openai_kind="$(printf '%s\n' "$_openai_probe" | sed -n '2p')"
  _openai_scheme="$(printf '%s\n' "$_openai_probe" | sed -n '3p')"
  _openai_host="$(printf '%s\n' "$_openai_probe" | sed -n '4p')"
  _openai_port="$(printf '%s\n' "$_openai_probe" | sed -n '5p')"
  [ "$_openai_port" = "-" ] && _openai_port=""
  ok "接続先の検査モード: system_settings（起動後の実効値・初回シード済みのため env は見ません）"
  if [ "$_openai_kind" = "openai" ]; then
    ok "system_settings openai_endpoint_kind: openai（本家既定）"
  else
    _check_openai_endpoint_host "system_settings openai_base_url" \
      "$_openai_scheme" "$_openai_host" "$_openai_port"
  fi
elif [ "$_openai_status" = "DB_ENDPOINT_INVALID" ]; then
  # マーカー確定済み＝DB が真実源。env 候補は別経路の値のため、ここへは倒さない
  # （倒すと DB 側の実際の不正値を見逃す・check_production_openai_probe.py の probe() 参照）。
  ok "接続先の検査モード: system_settings（起動後の実効値・初回シード済みのため env は見ません）"
  fail "system_settings の kind または base URL が不正です（openai_endpoint_kind が openai/azure/custom"\
"のいずれでもない、または openai_base_url に userinfo/query の混入・不正な scheme/port の可能性が"\
"あります。管理画面の「接続先」欄を確認してください）"
else
  # env 候補モード: DB 未到達／マーカー無し（初回起動前）／python 依存未導入いずれか。
  case "$_openai_status" in
    DB_UNREACHABLE) ok "接続先の検査モード: env 候補（DB 未到達のため初回シード候補を検査します）" ;;
    NO_MARKER) ok "接続先の検査モード: env 候補（初回起動前＝system_settings 未確定のため）" ;;
    *) ok "接続先の検査モード: env 候補（system_settings 実効値を確認できないため）" ;;
  esac
  _openai_env_status="$(printf '%s\n' "$_openai_probe" | sed -n '2p')"
  case "$_openai_env_status" in
    ENV_CANDIDATE_INVALID)
      _openai_env_reason="$(printf '%s\n' "$_openai_probe" | sed -n '3p')"
      fail "OPENAI_BASE_URL 等の env 設定が不正です: ${_openai_env_reason:-（詳細不明）}"
      ;;
    ENV_CANDIDATE_OK)
      _openai_kind="$(printf '%s\n' "$_openai_probe" | sed -n '3p')"
      _openai_scheme="$(printf '%s\n' "$_openai_probe" | sed -n '4p')"
      _openai_host="$(printf '%s\n' "$_openai_probe" | sed -n '5p')"
      _openai_port="$(printf '%s\n' "$_openai_probe" | sed -n '6p')"
      [ "$_openai_port" = "-" ] && _openai_port=""
      if [ "$_openai_kind" = "openai" ]; then
        ok "OPENAI_BASE_URL is not set (default OpenAI endpoint)"
      else
        _check_openai_endpoint_host "OPENAI_BASE_URL" "$_openai_scheme" "$_openai_host" "$_openai_port"
      fi
      ;;
    *)
      warn "OpenAI 接続先の env 候補を検査できません（python 依存が未導入の可能性）"
      ;;
  esac
fi

# Elasticsearch requires vm.max_map_count >= 262144 to start (closed-network field report ⑧,
# 2026-08-18). Read-only: this preflight never writes /etc/sysctl.d/ itself -- a co-located product
# may already require a larger value via its own file (sysctl.d applies files in filename order,
# last one wins), and silently lowering it here would break that product.
max_map_count=""
if command -v sysctl >/dev/null 2>&1; then
  max_map_count="$(sysctl -n vm.max_map_count 2>/dev/null || true)"
fi
if [ -z "$max_map_count" ] && [ -r /proc/sys/vm/max_map_count ]; then
  max_map_count="$(cat /proc/sys/vm/max_map_count 2>/dev/null || true)"
fi
if [ -z "$max_map_count" ]; then
  warn "could not read vm.max_map_count (no sysctl command and /proc/sys/vm/max_map_count unreadable); Elasticsearch requires >= 262144"
elif ! [[ "$max_map_count" =~ ^[0-9]+$ ]]; then
  warn "could not parse vm.max_map_count output: '$max_map_count'"
elif [ "$max_map_count" -lt 262144 ]; then
  fail "vm.max_map_count=$max_map_count is below the Elasticsearch minimum (262144). If no co-located product already sets a larger value elsewhere (check: grep -rn max_map_count /etc/sysctl.conf /etc/sysctl.d/), add /etc/sysctl.d/99-sherpa-vm-max-map-count.conf with 'vm.max_map_count=262144' and run 'sysctl --system'."
else
  ok "vm.max_map_count=$max_map_count (>= 262144)"
fi

if "${PYTHON_BIN:-python3}" - <<'PY' >/dev/null 2>&1
import fastapi, uvicorn, psycopg, neo4j
PY
then
  ok "python runtime dependencies import"
else
  # 2026-07-13-横断レビュー対応.md R6a: このスクリプトは pip を実行しない（案内文言のみ）。
  # constraints.txt を付けない案内だとピン外れの依存が入りうるため、start.sh と同じ形にそろえる。
  fail "python dependencies are missing; run: ${PYTHON_BIN:-python3} -m pip install -r requirements.txt -c constraints.txt"
fi

case "${SHERPA_USE_FIXTURES:-}" in
  1|true|TRUE|yes|YES) fail "SHERPA_USE_FIXTURES is enabled in production env" ;;
  *) ok "fixtures flag is not enabled" ;;
esac

case "${SHERPA_AUTH_DISABLED:-}" in
  1|true|TRUE|yes|YES) fail "SHERPA_AUTH_DISABLED=1 disables login and must not be used in production" ;;
  *) ok "auth is enabled by default (SHERPA_AUTH_DISABLED is not set)" ;;
esac

# 2026-07-13-横断レビュー対応.md R4: サーバ起動側の fail-closed ガードと同じ2項目をプリフライトでも確認する。
# Python 側 agents._codex_sandbox_enabled() は値を strip().lower() してから判定するため、
# preflight でも同じ正規化（前後空白除去＋小文字化）をしてから無効値集合と突き合わせる（RV LOW・偽OK防止）。
_sandbox_raw="${SHERPA_CODEX_SANDBOX-1}"
_sandbox_norm="$(printf '%s' "$_sandbox_raw" | tr '[:upper:]' '[:lower:]' | awk '{$1=$1};1')"
case "$_sandbox_norm" in
  0|false|no|off|"") fail "SHERPA_CODEX_SANDBOX is disabled; Codex sandbox read containment (permission profile) would be bypassed in production" ;;
  *) ok "Codex sandbox is enabled (SHERPA_CODEX_SANDBOX)" ;;
esac

workers_val="${SHERPA_UVICORN_WORKERS:-1}"
if ! [[ "$workers_val" =~ ^[0-9]+$ ]]; then
  warn "SHERPA_UVICORN_WORKERS='$workers_val' is not a plain integer; could not verify single-worker operation"
elif [ "$workers_val" -gt 1 ]; then
  fail "SHERPA_UVICORN_WORKERS=$workers_val (multi-worker); chat-turn background execution and rate limiting use in-process state that is not shared across workers"
else
  ok "SHERPA_UVICORN_WORKERS=$workers_val (single worker)"
fi

if [ -z "${DATABASE_URL:-}" ] && [ -z "${SHERPA_PG_DSN:-}" ] && [ -z "${PGPASSWORD:-}" ] && [ -z "${POSTGRES_PASSWORD:-}" ]; then
  fail "no Postgres connection setting found (set DATABASE_URL, SHERPA_PG_DSN, PGPASSWORD, or POSTGRES_PASSWORD)"
else
  ok "Postgres connection setting present"
fi

if [ -n "${DATABASE_URL:-}" ] || [ -n "${SHERPA_PG_DSN:-}" ] || [ -n "${PGPASSWORD:-}" ] || [ -n "${POSTGRES_PASSWORD:-}" ]; then
  if "${PYTHON_BIN:-python3}" - <<'PY'
import os
import sys

import psycopg
from psycopg.rows import dict_row

from sherpa import auth


def dsn():
    if os.environ.get("SHERPA_PG_DSN"):
        return os.environ["SHERPA_PG_DSN"]
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    return "host={h} port={p} dbname={d} user={u} password={pw}".format(
        h=os.environ.get("PGHOST", "localhost"),
        p=os.environ.get("PGPORT", "5432"),
        d=os.environ.get("PGDATABASE", "sherpa"),
        u=os.environ.get("PGUSER", "sherpa"),
        pw=os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD", "sherpa_dev"),
    )


try:
    with psycopg.connect(dsn(), row_factory=dict_row) as c:
        row = c.execute(
            "SELECT password_hash, COALESCE(must_change_password, FALSE) AS must_change_password "
            "FROM users WHERE uid='admin'"
        ).fetchone()
except Exception as e:
    print(f"WARN_DB_CHECK:{e}", file=sys.stderr)
    sys.exit(2)

if not row:
    print("WARN_ADMIN_MISSING", file=sys.stderr)
    sys.exit(3)

initial = auth.initial_admin_password()
if auth.verify_password(initial, row.get("password_hash")):
    print("ADMIN_INITIAL_PASSWORD", file=sys.stderr)
    sys.exit(4)
if row.get("must_change_password"):
    print("WARN_ADMIN_MUST_CHANGE", file=sys.stderr)
    sys.exit(5)
sys.exit(0)
PY
  then
    ok "initial admin password has been changed"
  else
    rc=$?
    case "$rc" in
      2) warn "could not inspect admin password in DB; run after DB is reachable" ;;
      3) warn "admin user is not present yet; first app start will create it and require password change" ;;
      4) fail "admin is still using the initial password; change it before production" ;;
      5) warn "admin still has must_change_password=true; complete first login password change" ;;
      *) warn "admin password check returned unexpected status $rc" ;;
    esac
  fi
fi

# 2026-07-13-横断レビュー対応.md R6b（版ごとのフォルダに展開し、current リンクを一度に切り替える
# 方式）: 本番は $ROOT が /opt/sherpa/current のような symlink で、実体は releases/<版> を指す運用に
# なり得る。データ系パスがその実体（release 版ディレクトリ）配下を指していると、次のリリース切替で
# 新しい版ディレクトリに置き換わった瞬間にデータが見えなくなる（消えたように見える）。
#
# RV MEDIUM（2026-07-15 再RV）: realpath が無い環境で `|| printf '%s' "$val"`（未解決の文字列を
# そのまま比較）に黙ってフォールバックすると、symlink 越しの一致を見逃して false negative
# （本来 fail すべき誤配置を OK と誤判定）になり得る。realpath -> python3 -> それでも解決できなければ
# **fail-closed**（「確認できない」として fail）の順にする。
_resolve_real_path() {  # $1=path -> stdout に canonical path（解決できなければ非0で失敗）
  local p="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath -m "$p" 2>/dev/null && return 0
  fi
  "${PYTHON_BIN:-python3}" -c 'import os, sys
print(os.path.realpath(sys.argv[1]))' "$p" 2>/dev/null
}

REAL_ROOT=""
if ! REAL_ROOT="$(_resolve_real_path "$ROOT")"; then
  warn "could not resolve a canonical path for the app directory ($ROOT); realpath and ${PYTHON_BIN:-python3} both unavailable/failed"
fi

for var in SHERPA_USERS_DIR SHERPA_DERIVED_DIR; do
  val="${!var:-}"
  if [ -z "$val" ]; then
    fail "$var is not set"
  elif [ "${val#/}" = "$val" ]; then
    fail "$var should be an absolute path in production: $val"
  elif printf '%s\n' "$val" | grep -q '/fixtures\($\|/\)'; then
    fail "$var points under fixtures: $val"
  elif [ -z "$REAL_ROOT" ]; then
    fail "$var=$val -- cannot verify it is outside the release tree (path resolution unavailable: install realpath or ensure ${PYTHON_BIN:-python3} is on PATH, then re-run)"
  else
    real_val=""
    if ! real_val="$(_resolve_real_path "$val")"; then
      fail "$var=$val -- could not resolve a canonical path to verify it is outside the release tree"
    else
      case "$real_val" in
        "$REAL_ROOT"|"$REAL_ROOT"/*)
          fail "$var points inside the app directory ($ROOT, resolves to $REAL_ROOT): $val -- it would be orphaned on a release cutover (versioned dir + symlink swap); point it at a fixed path outside the release tree (e.g. /srv/sherpa/...)" ;;
        *)
          ok "$var=$val" ;;
      esac
    fi
  fi
done

if [ "${failures:-0}" -gt 0 ]; then
  echo "production preflight failed: $failures issue(s)" >&2
  exit 1
fi

echo "production preflight passed"
