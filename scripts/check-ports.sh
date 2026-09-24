#!/usr/bin/env bash
# ポートの整合と占有を、ストア起動（docker compose up）の**前**に検査する（2026-08-18）。
#
# なぜ: compose の公開ポート（PGPORT/SHERPA_ES_PORT/SHERPA_NEO4J_*）とアプリの接続先（DATABASE_URL/ES_URL/NEO4J_URI）
# は別変数で、片方だけ変えると「compose は 5433 で公開・アプリは 5432 へ接続＝**他人の PostgreSQL に繋ぎに行く**」
# 事故になる。また、既に他プロセスがそのポートを聴いていると compose up が失敗するか、失敗せずに（他人のサービスへ）
# 静かに繋がる。どちらも起動後に気づくと原因が分かりづらいので、起動前に表で見せて止める。
#
# 検査:
#   (a) 整合: アプリの接続先が localhost を指すとき、そのポートが compose の公開ポートと一致すること。
#   (b) 占有: 公開ポート（PG/ES/bolt/http/アプリ SHERPA_PORT）ごとに listen 中なら所有者を調べ、
#       自分たち（compose project label が一致するコンテナ／PIDまで一致する自アプリ）以外なら fail。
# 使い方: ./scripts/check-ports.sh  または  make check-ports（start.sh / check-production.sh も呼ぶ）
# 終了コード: 1 つでも fail があれば非0。
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# .env の読み方は run-common.sh に一本化（明示指定 ＞ .env ＞ 既定・SHERPA_ENV_FILE 対応）。
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
# SHERPA_SKIP_PORT_CHECK（MED・2026-08-18 Codex RV 指摘3）: fixes 文言が「.env に書いて省略できる」と
# 案内しているのに、この列挙に無いと sherpa_env_default（未設定のときだけ .env から読む・run-common.sh）
# が拾わず、コマンドラインで明示しない限り効かなかった（別ホストのストアをまだ起動していない構成で
# make start が fail-close する）。既存の優先順（明示指定 ＞ .env ＞ 既定）は変えない。
sherpa_env_default PGPORT SHERPA_ES_PORT SHERPA_NEO4J_BOLT_PORT SHERPA_NEO4J_HTTP_PORT \
                   SHERPA_PG_DSN DATABASE_URL ES_URL NEO4J_URI COMPOSE_PROJECT_NAME \
                   SHERPA_SKIP_PORT_CHECK

note() { echo "・ $*"; }
ok()   { echo "OK: $*"; }
warn() { echo "WARN: $*" >&2; }
fail() { problems+=("$*"); failures=$((failures + 1)); }   # 表の後でまとめて出す（読みやすさ）
failures=0
problems=()
rows=()          # 表の行（項目|compose 側|アプリ側|状態|判定）
fixes=()         # 直し方（fail のとき）

# ---------------------------------------------------------------------------
# URL/DSN からホストとポートを取り出す（python があればそれで厳密に、無ければ sed で近似）。
# 出力: "host port"（取れなければ空）。DATABASE_URL は libpq の key=value 形式（host=… port=…）にも対応。
# ---------------------------------------------------------------------------
parse_hostport() {  # $1=URL or DSN  $2=既定ポート
  local s="$1" defport="$2"
  [ -n "$s" ] || return 0
  case "$s" in
    *://*)
      if command -v python3 >/dev/null 2>&1; then
        python3 - "$s" "$defport" <<'PY'
import sys
from urllib.parse import urlsplit
try:
    u = urlsplit(sys.argv[1])
    if not u.hostname:
        raise ValueError("host is empty")
    print(u.hostname, (u.port or int(sys.argv[2])))
except (TypeError, ValueError):
    raise SystemExit(2)
PY
      else
        local rest host port
        rest="${s#*://}"; rest="${rest##*@}"; rest="${rest%%/*}"; rest="${rest%%\?*}"
        host="${rest%%:*}"; port="${rest##*:}"
        [ "$port" = "$rest" ] && port="$defport"
        printf '%s %s\n' "${host:-localhost}" "$port"
      fi ;;
    *)  # key=value 形式（host=… port=…）
      local host port
      host="$(printf '%s' "$s" | grep -oE '(^|[[:space:]])host=[^[:space:]]+' | head -1 | sed 's/.*host=//' || true)"
      port="$(printf '%s' "$s" | grep -oE '(^|[[:space:]])port=[^[:space:]]+' | head -1 | sed 's/.*port=//' || true)"
      printf '%s %s\n' "${host:-localhost}" "${port:-$defport}" ;;
  esac
}

is_local_host() {  # localhost / 127.0.0.1 / ::1 / [::1] を「このホスト＝compose の公開先」とみなす
  case "$1" in localhost|127.0.0.1|::1|\[::1\]) return 0 ;; *) return 1 ;; esac
}

valid_port() {  # 1..65535 の10進整数だけを許す（ss/docker の引数にも渡すため、文字列を素通ししない）
  local port="$1"
  case "$port" in ""|*[!0-9]*) return 1 ;; esac
  [ "$((10#$port))" -ge 1 ] && [ "$((10#$port))" -le 65535 ]
}

# ---------------------------------------------------------------------------
# (a) 整合検査。compose 側のポート ⇔ アプリ側の接続先ポート。
# ---------------------------------------------------------------------------
check_pair() {  # $1=項目名 $2=compose変数名 $3=compose側ポート $4=アプリ変数名 $5=アプリ側URL/DSN
  local label="$1" cvar="$2" cport="$3" avar="$4" aval="$5" host aport parsed
  if [ -n "${BAD_PORTS[$cvar]:-}" ]; then return 0; fi
  if [ -z "$aval" ]; then
    rows+=("$label|$cvar=$cport|($avar 未設定→同じポート)|-|OK")
    return
  fi
  if ! parsed="$(parse_hostport "$aval" "$cport")" || [ -z "$parsed" ]; then
    rows+=("$label|$cvar=$cport|$avar|**URL/DSN不正**|NG")
    fail "$label: $avar から接続先ホスト/ポートを読み取れません"
    fixes+=("$label: $avar を正しい URL/DSN に直してください。localhost の compose に繋ぐなら $avar を消し、$cvar だけを設定できます。")
    return
  fi
  read -r host aport <<<"$parsed"
  if ! valid_port "$aport"; then
    rows+=("$label|$cvar=$cport|$avar → $host:$aport|**ポート不正**|NG")
    fail "$label: $avar のポートは 1〜65535 の整数で指定してください（現在: $aport）"
    fixes+=("$label: $avar を正しい URL/DSN に直してください。")
    return
  fi
  if ! is_local_host "$host"; then
    check_remote "$label" "$cvar" "$cport" "$avar" "$host" "$aport"
    return
  fi
  if [ "$aport" = "$cport" ]; then
    rows+=("$label|$cvar=$cport|$avar → $host:$aport|一致|OK")
  else
    rows+=("$label|$cvar=$cport|$avar → $host:$aport|**不一致**|NG")
    fail "$label: compose の公開ポート ($cvar=$cport) とアプリの接続先 ($avar のポート $aport) が違います"
    fixes+=("$label: $cvar と $avar のポートを揃えてください。localhost の compose に繋ぐなら $avar を消して $cvar だけにするのが簡単です（$avar は別ホストへ繋ぐ時だけ設定）。")
  fi
}

# 別ホストの接続先: 以前は無条件 OK にしていたが、閉域実機で「例のホスト名（*.example.local）のポートだけ
# 書き換えて有効化」→名前解決できず取り込みが全滅、を rc=0 で通してしまった（2026-08-18）。
# ここでは (1) 名前解決（getent ahosts＝/etc/hosts も DNS も見る・数値 IP もそのまま通る）、
# (2) TCP 疎通（bash の /dev/tcp・timeout 3 秒）を確かめ、どちらも駄目なら NG にする。
# 別ホストのストアをまだ起動していない段階で通したいときは SHERPA_SKIP_PORT_CHECK=1 で (2) だけ省略できる
# （(1) は設定の誤りそのものなので省略しない）。
resolve_host() {  # $1=host → 解決できれば 0
  local host="$1"
  command -v getent >/dev/null 2>&1 || return 0     # getent が無い最小環境では判定不能＝通す（fail-open はここだけ・後段の疎通で拾う）
  getent ahosts "$host" >/dev/null 2>&1 || getent hosts "$host" >/dev/null 2>&1
}

tcp_reachable() {  # $1=host $2=port → 3 秒以内に接続できれば 0
  local host="$1" port="$2"
  if command -v timeout >/dev/null 2>&1; then
    timeout 3 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$host" "$port" >/dev/null 2>&1
  else
    (exec 3<>"/dev/tcp/$host/$port") >/dev/null 2>&1
  fi
}

check_remote() {  # $1=項目名 $2=compose変数名 $3=compose側ポート $4=アプリ変数名 $5=host $6=port
  local label="$1" cvar="$2" cport="$3" avar="$4" host="$5" aport="$6"
  if ! resolve_host "$host"; then
    rows+=("$label|$cvar=$cport|$avar → $host:$aport|**別ホスト・名前解決できず**|NG")
    fail "$label: $avar のホスト名 '$host' を名前解決できません（.env.example の例をそのまま有効化していませんか？）"
    fixes+=("$label: $avar のホスト名を実在の別ホストに書き換えるか、localhost の compose に繋ぐなら $avar を消して $cvar だけにしてください。")
    return
  fi
  if [ "${SHERPA_SKIP_PORT_CHECK:-0}" = 1 ]; then
    rows+=("$label|$cvar=$cport|$avar → $host:$aport|別ホスト（疎通検査は省略）|OK")
    return
  fi
  if tcp_reachable "$host" "$aport"; then
    rows+=("$label|$cvar=$cport|$avar → $host:$aport|別ホスト・疎通OK|OK")
  else
    rows+=("$label|$cvar=$cport|$avar → $host:$aport|**別ホスト・接続できず**|NG")
    fail "$label: $avar の接続先 $host:$aport に TCP 接続できません（3秒以内に応答なし）"
    fixes+=("$label: 別ホストのストア（$label）が起動しているか、ファイアウォールでポート $aport が許可されているかを確認してください。まだ起動していない段階で先へ進むなら SHERPA_SKIP_PORT_CHECK=1 で疎通検査だけ省略できます。")
  fi
}

PGPORT_V="${PGPORT:-5432}"
ESPORT_V="${SHERPA_ES_PORT:-9200}"
BOLT_V="${SHERPA_NEO4J_BOLT_PORT:-7687}"
HTTP_V="${SHERPA_NEO4J_HTTP_PORT:-7474}"
APP_V="${SHERPA_PORT:-8000}"
PROJECT="${COMPOSE_PROJECT_NAME:-sherpa-mvp}"

# 値が不正なまま ss / docker の検索条件へ渡さない。整合表には項目ごとに1度だけ出す。
declare -A BAD_PORTS=()
validate_config_port() {  # $1=表示名 $2=変数名 $3=値
  local label="$1" var="$2" port="$3"
  valid_port "$port" && return
  BAD_PORTS["$var"]=1
  rows+=("$label|$var=$port|-|**ポート不正**|NG")
  fail "$label: $var は 1〜65535 の整数で指定してください（現在: $port）"
  fixes+=("$label: $var を 1〜65535 の空いているポートへ直してください。")
}
validate_config_port "PostgreSQL"    PGPORT                 "$PGPORT_V"
validate_config_port "Elasticsearch" SHERPA_ES_PORT         "$ESPORT_V"
validate_config_port "Neo4j(bolt)"   SHERPA_NEO4J_BOLT_PORT "$BOLT_V"
validate_config_port "Neo4j(http)"   SHERPA_NEO4J_HTTP_PORT "$HTTP_V"
validate_config_port "アプリ"        SHERPA_PORT            "$APP_V"

# PG は SHERPA_PG_DSN ＞ DATABASE_URL の順にアプリが参照する（sherpa/store/db.py::_dsn）。使われる方だけ見る。
if [ -n "${SHERPA_PG_DSN:-}" ]; then
  check_pair "PostgreSQL" PGPORT "$PGPORT_V" SHERPA_PG_DSN "$SHERPA_PG_DSN"
else
  check_pair "PostgreSQL" PGPORT "$PGPORT_V" DATABASE_URL "${DATABASE_URL:-}"
fi
check_pair "Elasticsearch" SHERPA_ES_PORT "$ESPORT_V" ES_URL "${ES_URL:-}"
check_pair "Neo4j(bolt)"   SHERPA_NEO4J_BOLT_PORT "$BOLT_V" NEO4J_URI "${NEO4J_URI:-}"

# ---------------------------------------------------------------------------
# (b) 占有検査。listen 中のポートの所有者を調べる。
#   - docker が使えるなら `docker ps --filter publish=<port>` で公開元の compose project label を照合する。
#   - docker が使えない／照会に失敗したときは「不明」として **fail 扱い**にする（fail-closed）。
#     理由: 占有されているのに「たぶん自分たち」と通すと、他人のサービスへ静かに繋ぐ事故（この検査の主目的）を
#     見逃す。誤検知の場合は所有者を確認して SHERPA_SKIP_PORT_CHECK=1 で通せる（原因と直し方を出すのが仕事）。
#   - アプリのポートは、pid ファイルの自プロセス（live_matching_pid）が聴いているなら自分たち。
# ---------------------------------------------------------------------------
listener_info() {  # $1=port → listen 中なら "pid=… proc=…"（分かる範囲）を出して 0、空いていれば 1
  local port="$1" out
  if command -v ss >/dev/null 2>&1; then
    # ss がある＝それを信じる。ただし ss 自体が失敗（古い iproute2 で -H 未対応等）したら「空き」に倒さず
    # /dev/tcp 探査へ落とす（以前は `|| true` で全ポート「空き」＝fail-open になっていた・RV）。
    if out="$(ss -Hltnp "sport = :$port" 2>/dev/null)"; then
      [ -n "$out" ] || return 1
      # users:(("python3",pid=3,fd=3),("python3",pid=2,fd=3),("uvicorn",pid=1,fd=3))
      # → proc=python3 pids=3,2,1  ※ 先頭1件だけ見ると多ワーカー uvicorn（子が先に並ぶ）で親 pid を
      #   取りこぼし「他プロセス」と誤判定する（RV 実測）ため、pid は**全部**拾う。
      local procs pids
      procs="$(printf '%s' "$out" | grep -oE 'users:\(\("[^"]+"' | head -1 | sed -E 's/users:\(\("([^"]+)"/\1/' || true)"
      pids="$(printf '%s' "$out" | grep -oE 'pid=[0-9]+' | sed 's/pid=//' | sort -u | tr '\n' ',' | sed 's/,$//' || true)"
      printf 'proc=%s pids=%s' "${procs:-?}" "${pids:-?}"
      return 0
    fi
  fi
  # ss が無い環境（最小コンテナ等）: bash の /dev/tcp で接続を試す（listen していれば繋がる）。
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    exec 3>&- 2>/dev/null || true
    printf 'proc=? pid=?'
    return 0
  fi
  return 1
}

docker_owner() {  # $1=port → ours:<names> / other:<names>。docker 不可なら "!" を出す
  command -v docker >/dev/null 2>&1 || { printf '!'; return; }
  local lines name project names="" all_ours=1
  if ! lines="$(docker ps --filter "publish=$port" --format '{{.Names}}|{{.Label "com.docker.compose.project"}}' 2>/dev/null)"; then
    printf '!'; return
  fi
  [ -n "$lines" ] || return 0
  while IFS='|' read -r name project; do
    [ -n "$name" ] || continue
    names="${names:+$names }$name"
    [ "$project" = "$PROJECT" ] || all_ours=0
  done <<<"$lines"
  if [ "$all_ours" = 1 ]; then
    printf 'ours:%s' "$names"
  else
    printf 'other:%s' "$names"
  fi
}

check_port() {  # $1=項目名 $2=変数名 $3=port $4=種別（store|app）
  local label="$1" var="$2" port="$3" kind="$4" info owner
  if [ -n "${BAD_PORTS[$var]:-}" ]; then return 0; fi
  if ! info="$(listener_info "$port")"; then
    rows+=("$label 占有|$var=$port|-|空き|OK")
    return
  fi
  info="${info:-proc=? pid=?}"
  if [ "$kind" = app ]; then
    local pid
    if pid="$(live_matching_pid "$APP_PID_FILE" "$APP_PROC_NEEDLE" 2>/dev/null)"; then
      # pids=a,b,c のいずれかが自アプリの pid（親）なら自分たち。多ワーカーでは子 pid が並ぶ。
      case ",${info##*pids=}," in *",$pid,"*)
        rows+=("$label 占有|$var=$port|-|使用中（自アプリ pid=$pid）|OK"); return ;;
      esac
    fi
    rows+=("$label 占有|$var=$port|-|**他プロセスが使用中**（$info）|NG")
    fail "$label: ポート $port は既に他のプロセスが使っています（$info）"
    fixes+=("$label: そのプロセスを止めるか、$var を空いているポートに変えてください。")
    return
  fi
  owner="$(docker_owner "$port")"
  case "$owner" in
    "!")
      rows+=("$label 占有|$var=$port|-|**使用中・所有者不明**（docker 照会不可・$info）|NG")
      fail "$label: ポート $port は使用中ですが、docker で所有者を確認できません（$info）"
      fixes+=("$label: 自分たちの compose project のコンテナなら問題ありません。確認のうえ SHERPA_SKIP_PORT_CHECK=1 で通せます。他人のサービスなら $var を空いているポートに変えてください。") ;;
    "")
      rows+=("$label 占有|$var=$port|-|**他プロセスが使用中**（$info）|NG")
      fail "$label: ポート $port は既に他のプロセスが使っています（$info）"
      fixes+=("$label: そのプロセスを止めるか、$var を空いているポートに変えてください（変えたらアプリ側の接続先も自動で追随します。DATABASE_URL/ES_URL/NEO4J_URI を明示している場合はそちらも）。") ;;
    ours:*)
      rows+=("$label 占有|$var=$port|-|使用中（自コンテナ ${owner#ours:}）|OK") ;;
    other:*)
      rows+=("$label 占有|$var=$port|-|**他コンテナが使用中**（${owner#other:}）|NG")
      fail "$label: ポート $port は別の Docker コンテナ（${owner#other:}）が公開しています"
      fixes+=("$label: そのコンテナを止めるか、$var を空いているポートに変えてください。") ;;
    *)
      rows+=("$label 占有|$var=$port|-|**所有者判定不正**（$owner）|NG")
      fail "$label: ポート $port の Docker 所有者を安全に判定できません"
      fixes+=("$label: docker ps で所有者を確認し、原因を解消してください。") ;;
  esac
}

if [ "${SHERPA_SKIP_PORT_CHECK:-0}" = 1 ]; then
  warn "SHERPA_SKIP_PORT_CHECK=1 のため占有検査を省略します（整合検査のみ）"
else
  check_port "PostgreSQL"    PGPORT                 "$PGPORT_V" store
  check_port "Elasticsearch" SHERPA_ES_PORT         "$ESPORT_V" store
  check_port "Neo4j(bolt)"   SHERPA_NEO4J_BOLT_PORT "$BOLT_V"   store
  check_port "Neo4j(http)"   SHERPA_NEO4J_HTTP_PORT "$HTTP_V"   store
  check_port "アプリ"        SHERPA_PORT            "$APP_V"    app
fi

# ---------------------------------------------------------------------------
# (c) 表で表示
# ---------------------------------------------------------------------------
echo "=== ポート検査（env: $(sherpa_dotenv_file)$([ -f "$(sherpa_dotenv_file)" ] || printf ' ※無し')） ==="
{
  echo "項目|compose 側|アプリ側|状態|判定"
  echo "----|----------|--------|----|----"
  printf '%s\n' "${rows[@]}"
} | column -t -s '|' 2>/dev/null || printf '%s\n' "${rows[@]}"

if [ "$failures" -gt 0 ]; then
  echo "" >&2
  printf 'NG: %s\n' "${problems[@]}" >&2
  echo "ポート検査で $failures 件の問題があります。直し方:" >&2
  printf '  - %s\n' "${fixes[@]}" >&2
  echo "  設定ファイル: $(sherpa_dotenv_file)（ポートは PGPORT / SHERPA_ES_PORT / SHERPA_NEO4J_BOLT_PORT / SHERPA_NEO4J_HTTP_PORT / SHERPA_PORT の各1か所）" >&2
  exit 1
fi
ok "ポートの整合・占有に問題はありません"
