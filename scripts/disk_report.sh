#!/usr/bin/env bash
# Sherpa 容量レポート: ディスクの使われ方を調べ、画面にサマリー、ファイルに詳細を出す（読むだけ・何も消さない）。
# どこからでも叩ける: bash <Sherpaの置き場所>/scripts/disk_report.sh [--deep] [--root DIR] [--out DIR]
#   --deep  重い調べ（ES の集計・チャンクの種類の数え上げ・埋め込みの平均バイト数）も行う
#   --root  Sherpa の置き場所（既定はこのスクリプトの 1 つ上）
#   --out   レポートの保存先（既定 <root>/data/run/disk-report。書けなければ /tmp/disk-report）
# 守ること: ファイル名・フォルダ名・資料フォルダ名・ホスト名・IP・ユーザー名・.env の値を出力に出さない。
# 依存は bash（3.2 でも可）・coreutils・find。docker・curl・python3・sqlite3 は無ければその項目を飛ばす。
unset POSIXLY_CORRECT CDPATH
export LC_ALL=C
umask 077

DEEP=0
ROOT=""
OUTDIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --deep) DEEP=1 ;;
    --root) shift; ROOT="$1" ;;
    --root=*) ROOT="${1#--root=}" ;;
    --out) shift; OUTDIR="$1" ;;
    --out=*) OUTDIR="${1#--out=}" ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "不明な引数です: $1（--help を見てください）" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$ROOT" ]; then
  _src="${BASH_SOURCE[0]:-$0}"
  ROOT="$(cd -P "$(dirname "$_src")/.." 2>/dev/null && pwd)"
fi
ROOT="$(cd -P "$ROOT" 2>/dev/null && pwd)" || { echo "Sherpa の置き場所が開けません" >&2; exit 2; }

TAB="$(printf '\t')"
NOW_H="$(date '+%Y-%m-%d %H:%M')"
NOW_F="$(date '+%Y%m%d-%H%M%S')"
VERSION_TXT="$({ tr -d ' \r\n' <"$ROOT/VERSION"; } 2>/dev/null)"
[ -n "$VERSION_TXT" ] || VERSION_TXT="?"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/disk-report.XXXXXX" 2>/dev/null)" || { echo "作業用フォルダを作れません" >&2; exit 2; }
trap 'rm -rf "$TMP"' EXIT
trap 'exit 130' INT TERM
: >"$TMP/sum"
: >"$TMP/det"

readable_dir() { [ -d "$1" ] && [ -r "$1" ] && [ -x "$1" ]; }
have() { command -v "$1" >/dev/null 2>&1; }
S() { printf '%s\n' "$*" >>"$TMP/sum"; }
D() { printf '%s\n' "$*" >>"$TMP/det"; }
B() { S "$*"; D "$*"; }
prog() { printf '... %s\n' "$*" >&2; }
idxs() { awk -v n="$NW" 'BEGIN { for (i = 1; i <= n; i++) print i }'; }

# コマンドに時間の上限を付ける（timeout が無い環境ではそのまま実行する）
if have timeout; then tmo() { timeout "$@"; }
elif have gtimeout; then tmo() { gtimeout "$@"; }
else tmo() { shift; "$@"; }
fi

# 表の 1 行を作る。引数は "幅:文字列"。幅が正なら左寄せ、負なら右寄せ。全角は 2 桁で数える
row() {
  awk 'BEGIN {
    for (i = 1; i < ARGC; i++) {
      a = ARGV[i]; p = index(a, ":"); w = substr(a, 1, p - 1) + 0; t = substr(a, p + 1)
      aw = (w < 0) ? -w : w; pad = aw - dw(t); if (pad < 0) pad = 0
      sp = sprintf("%*s", pad, "")
      out = out ((w < 0) ? sp t : t sp)
    }
    sub(/ +$/, "", out); print out; exit
  }
  function dw(s,   x, r, b) { x = s; r = gsub(/[ -~]/, "", x); b = length(x); return r + (b / 3) * 2 }' "$@"
}

hs() {
  awk -v b="$1" 'BEGIN {
    if (b == "" || b < 0) { print "-"; exit }
    split("B K M G T", u, " "); i = 1
    while (b >= 1024 && i < 5) { b /= 1024; i++ }
    if (i == 1) printf "%dB\n", b; else printf "%.1f%s\n", b, u[i]
  }'
}

fsize() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null; }

# ---------------------------------------------------------------- .env と設定
ENVFILE="$ROOT/.env"
envget() {
  local v
  v="$(printenv "$1" 2>/dev/null)"
  if [ -z "$v" ] && [ -f "$ENVFILE" ]; then
    v="$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$ENVFILE" 2>/dev/null | tail -n 1 | sed -E "s/^[^=]*=//; s/\r$//; s/^[\"']//; s/[\"']$//")"
  fi
  printf '%s' "$v"
}
absp() {
  case "$1" in
    "") printf '%s' "$2" ;;
    /*) printf '%s' "$1" ;;
    *) printf '%s' "$ROOT/${1#./}" ;;
  esac
}
DERIVED="$(absp "$(envget SHERPA_DERIVED_DIR)" "$ROOT/data/derived")"
OBSDIR="$(absp "$(envget SHERPA_OBSERVATION_DIR)" "$ROOT/data/observations")"
RUNDIR="$(absp "$(envget RUN_DIR)" "$ROOT/data/run")"
KBDIR="$(absp "$(envget SHERPA_KB_DIR)" "$ROOT/data/kb")"
ES_PORT="$(envget SHERPA_ES_PORT)"; [ -n "$ES_PORT" ] || ES_PORT=9200
ES_URL="http://127.0.0.1:$ES_PORT"
TMPBASE="${TMPDIR:-/tmp}"

# 保存先。書けなければ /tmp/disk-report
if [ -z "$OUTDIR" ]; then OUTDIR="$ROOT/data/run/disk-report"; fi
if ! mkdir -p "$OUTDIR" 2>/dev/null || [ ! -w "$OUTDIR" ]; then
  OUTDIR="/tmp/disk-report"
  mkdir -p "$OUTDIR" 2>/dev/null
fi

if find --version >/dev/null 2>&1; then BLKUNIT=1024; else BLKUNIT=512; fi

# 走査用の一覧: 「inode 512バイト単位のブロック数 リンク数 ./相対パス」。GNU find は所有者名を引かない -printf、BSD find は -ls を整える
# （-ls は所有者・グループ名に空白があると列がずれるため）。$1 = f（通常ファイルだけ）/ nd（ディレクトリ以外）
if find . -maxdepth 0 -printf '' >/dev/null 2>&1; then
  list_tree() {
    if [ "$1" = f ]; then find . -type f -printf '%i %b %n ./%P\n'
    else find . ! -type d -printf '%i %b %n ./%P\n'; fi
  }
else
  list_tree() {
    local t='-type f'; [ "$1" = f ] || t='! -type d'
    # shellcheck disable=SC2086
    find . $t -ls | awk 'match($0, /^ *[0-9]+ +[0-9]+ +[^ ]+ +[0-9]+ +[^ ]+ +[^ ]+ +[0-9]+ +[^ ]+ +[^ ]+ +[^ ]+ /) {
      n = split($0, f, " "); p = substr($0, RLENGTH + 1); sub(/ -> .*$/, "", p); print f[1], f[2], f[4], p }'
  }
fi

# 同じ inode は 1 回だけ数えた「件数 実バイト数」（ハードリンクの二重計上を避ける）
dd_sum() {
  find "$@" -type f -ls 2>/dev/null | awk -v U="$BLKUNIT" '!($1 in s) { s[$1] = 1; n++; b += $2 * U } END { printf "%d %.0f\n", n, b }'
}

# ---------------------------------------------------------------- docker / PostgreSQL の入口
HAVE_DOCKER=0
if have docker && tmo 20 docker info >/dev/null 2>&1; then HAVE_DOCKER=1; fi

find_container() { # $1=イメージ名（postgres / neo4j）。Sherpa の compose が付ける env を優先する
  [ "$HAVE_DOCKER" = 1 ] || return 1
  local n env_hit="" first=""
  for n in $(tmo 15 docker ps --format "{{.Names}}${TAB}{{.Image}}" 2>/dev/null | awk -F'\t' -v im="$1" '$2 ~ "^(docker.io/library/)?" im "(:|@|$)" { print $1 }'); do
    [ -n "$first" ] || first="$n"
    if [ "$1" = postgres ]; then
      if tmo 15 docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$n" 2>/dev/null | grep -q '^POSTGRES_DB=sherpa$'; then env_hit="$n"; break; fi
    fi
  done
  if [ -n "$env_hit" ]; then printf '%s' "$env_hit"; else printf '%s' "$first"; fi
}
PGC="$(find_container postgres)"
NEOC="$(find_container neo4j)"

PGUSER_="$(envget PGUSER)"; [ -n "$PGUSER_" ] || PGUSER_=sherpa
PGDB_="$(envget PGDATABASE)"; [ -n "$PGDB_" ] || PGDB_=sherpa

# 値は引数に出さず、子プロセスの環境変数だけで psql へ渡す
pgq() {
  if [ -n "$PGC" ]; then
    tmo 60 docker exec -i "$PGC" psql -U "$PGUSER_" -d "$PGDB_" -X -A -t -F "$TAB" -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null
    return $?
  fi
  have psql || return 1
  (
    dsn="$(envget SHERPA_PG_DSN)"; [ -n "$dsn" ] || dsn="$(envget DATABASE_URL)"
    if [ -n "$dsn" ]; then
      case "$dsn" in
        postgres://*|postgresql://*)
          rest="${dsn#*://}"; hostdb="${rest##*@}"; cred=""
          case "$rest" in *@*) cred="${rest%@*}" ;; esac
          export PGUSER="${cred%%:*}"
          case "$cred" in *:*) export PGPASSWORD="${cred#*:}" ;; esac
          hp="${hostdb%%/*}"; export PGDATABASE="${hostdb#*/}"; PGDATABASE="${PGDATABASE%%\?*}"
          export PGHOST="${hp%%:*}"; case "$hp" in *:*) export PGPORT="${hp##*:}" ;; esac ;;
        *)
          for kv in $dsn; do
            case "$kv" in
              host=*) export PGHOST="${kv#host=}" ;; port=*) export PGPORT="${kv#port=}" ;;
              dbname=*) export PGDATABASE="${kv#dbname=}" ;; user=*) export PGUSER="${kv#user=}" ;;
              password=*) export PGPASSWORD="${kv#password=}" ;;
            esac
          done ;;
      esac
    else
      export PGHOST=127.0.0.1 PGUSER="$PGUSER_" PGDATABASE="$PGDB_"
      p="$(envget PGPORT)"; [ -n "$p" ] && export PGPORT="$p"
      pw="$(envget PGPASSWORD)"; [ -n "$pw" ] || pw="$(envget POSTGRES_PASSWORD)"
      [ -n "$pw" ] && export PGPASSWORD="$pw"
    fi
    export PGCONNECT_TIMEOUT=5
    tmo 60 psql -X -A -t -F "$TAB" -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null
  )
}

sqlq() { # $1=SQLite ファイル $2=SQL → 1 行目（列は | 区切り）。読み取り専用で開く
  if have sqlite3; then
    tmo 120 sqlite3 -readonly "$1" "$2" 2>/dev/null | head -n 1
  elif have python3; then
    tmo 120 python3 -I -c 'import sqlite3,sys,urllib.parse
c=sqlite3.connect("file:"+urllib.parse.quote(sys.argv[1])+"?mode=ro",uri=True,timeout=5)
print("|".join(str(x) for x in c.execute(sys.argv[2]).fetchone()))' "$1" "$2" 2>/dev/null
  else
    return 1
  fi
}

# ---------------------------------------------------------------- 資料フォルダの登録情報（PostgreSQL の worlds 表）
REG="$TMP/reg.tsv"
REGMODE=unknown   # ok = DB から取れた / unknown = 取れない
prog "登録情報"
# 0 行のときは「別の DB につながっている」可能性があるので、全部を未登録とは言わず不明にする
if pgq "select world_id, root_path from worlds" >"$REG" 2>/dev/null && [ -s "$REG" ]; then REGMODE=ok; fi

# ---------------------------------------------------------------- ① ディスク
sec_disk() {
  prog "ディスク"
  local list="$TMP/disk.list" p
  : >"$list"
  add_loc() {
    p="$2"
    while [ -n "$p" ] && [ ! -e "$p" ] && [ "$p" != "/" ]; do p="$(dirname "$p")"; done
    df -Pk "$p" 2>/dev/null | awk -v L="$1" 'NR == 2 { printf "%s\t%s|%s\t%s\t%s\t%s\t%s\n", L, $1, $6, $2 * 1024, $3 * 1024, $4 * 1024, $5 }' >>"$list"
  }
  add_loc "本体" "$ROOT"
  add_loc "派生物" "$DERIVED"
  add_loc "観測" "$OBSDIR"
  add_loc "ログ" "$RUNDIR"
  local dnote="" droot=""
  if [ "$HAVE_DOCKER" = 1 ]; then
    droot="$(tmo 20 docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
    if [ -n "$droot" ] && [ -d "$droot" ]; then add_loc "Docker" "$droot"
    else dnote="Docker の保存先: この端末からは見えません（別の環境にあります）"; fi
  else
    dnote="Docker の保存先: 調べられません（docker が使えません）"
  fi
  add_loc "一時" "$TMPBASE"

  awk -F'\t' '{
    k = $2
    if (!(k in lab)) { order[++n] = k; lab[k] = $1; tot[k] = $3; use[k] = $4; av[k] = $5; pc[k] = $6 }
    else lab[k] = lab[k] "・" $1
  } END { for (i = 1; i <= n; i++) { k = order[i]; printf "%s\t%.0f\t%.0f\t%.0f\t%s\n", lab[k], tot[k], use[k], av[k], pc[k] } }' "$list" >"$TMP/disk.grp"

  B "[ディスク]"
  B "$(row '34:' '-8:全体' '-8:使用' '-8:空き' '-7:使用率')"
  local lab tot use av pc
  while IFS="$TAB" read -r lab tot use av pc; do
    B "$(row "34: $lab" "-8:$(hs "$tot")" "-8:$(hs "$use")" "-8:$(hs "$av")" "-7:$pc")"
  done <"$TMP/disk.grp"
  [ -n "$dnote" ] && B " $dnote"
  return 0
}

# ---------------------------------------------------------------- ② 派生物（資料フォルダごと）
cat >"$TMP/lsname.awk" <<'AWK'
match($0, /^[0-9]+ [0-9]+ [0-9]+ /) { p = substr($0, RLENGTH + 1); sub(/^\.\//, "", p); print p }
AWK

cat >"$TMP/scan.awk" <<'AWK'
function parse() {
  if (match($0, /^[0-9]+ [0-9]+ [0-9]+ /)) { P = substr($0, RLENGTH + 1); return 1 }
  return 0
}
function conv_orphan(p,   s, pos, off, rest, cand) {
  if (!SRCOK) return 0
  s = substr(p, length(PC) + 1)
  if (s ~ /\.key\.json$/) { cand = s; sub(/\.key\.json$/, "", cand); return !(cand in src) }
  off = 0; rest = s
  while ((pos = index(rest, ".d/")) > 0) {
    cand = substr(s, 1, off + pos - 1)
    if (cand in convkey) return !(cand in src)
    off += pos + 2; rest = substr(s, off + 1)
  }
  return 0
}
function legacy_orphan(p,   r) {
  if (!SRCOK) return 0
  r = substr(p, length(PL) + 1); sub(/\.key$/, "", r); sub(/\.(docx|xlsx|pptx)$/, "", r)
  return !(r in src)
}
BEGIN {
  PC = "./_conv_cache/"; PL = "./_legacy_cache/"; PA = "./archives/"
  if (SRCOK && SRC != "") { while ((getline line < SRC) > 0) src[line] = 1; close(SRC) }
  nk = split("md rag ir _conv_cache _legacy_cache _metafile_render_cache archives archives_work search_cache semantic", kr, " ")
  for (i = 1; i <= nk; i++) known[kr[i]] = 1
}
NR == FNR {
  if (!parse()) next
  if (index(P, PA) == 1) src[substr(P, length(PA) + 1)] = 1
  else if (index(P, PC) == 1 && P ~ /\.key\.json$/) { r = substr(P, length(PC) + 1); sub(/\.key\.json$/, "", r); convkey[r] = 1 }
  next
}
{
  if (!parse()) { bad++; next }
  ino = $1; links = $3; bytes = $2 * U; p = P; lp = tolower(p)
  if (match(p, /^\.\/[^\/]+\//)) { r = substr(p, 3, RLENGTH - 3); if (!(r in known)) r = "other" } else r = "(root)"
  tn++; traw += bytes
  if (!(ino in ts)) { ts[ino] = 1; tnd++; tb += bytes }
  rn[r]++
  if (!((r SUBSEP ino) in rs)) { rs[r SUBSEP ino] = 1; rna[r]++; rb[r] += bytes }

  k = ""
  if (lp ~ /\.assets\//) k = (lp ~ /\.(png|jpe?g|gif|bmp|tiff?|webp|emf|wmf|svg)$/) ? "assets_img" : "assets_other"
  else if (lp ~ /\.pages(\/|$)/) k = "pages"
  else if (r == "_legacy_cache" && lp ~ /\.(docx|xlsx|pptx)$/) k = "legacy_ooxml"
  else if (lp ~ /\.md\.meta\.json$/) k = "md.meta.json"
  else if (lp ~ /\.document\.json$/) k = "document.json"
  else if (lp ~ /\.evidence\.json$/) k = "evidence.json"
  else if (lp ~ /\.context\.json$/) k = "context.json"
  else if (lp ~ /\.derived\.json$/) k = "derived.json"
  else if (lp ~ /\.ocr_route\.json$/) k = "ocr_route.json"
  else if (lp ~ /\.rag_chunks\.jsonl$/) k = "rag_chunks.jsonl"
  else if (lp ~ /\.rag\.md$/) k = "rag.md"
  else if (lp ~ /\.md$/) k = "md"
  else k = "other"
  if (!(ino in ks)) { ks[ino] = 1; kn[k]++; kb[k] += bytes }

  if (r == "_conv_cache" && links == 1 && p ~ /\.d\// && p !~ /\.d\.tmp\//) { l1n++; l1b += bytes }

  if (r == "semantic") {
    b = p; sub(/.*\//, "", b); e = ""
    if (b == "embed_cache.sqlite3") e = "json_db"
    else if (b == "embed_cache.sqlite3-wal") e = "json_wal"
    else if (b == "embed_cache.sqlite3-shm") e = "json_shm"
    else if (b == "embed_cache.f32.sqlite3") e = "f32_db"
    else if (b == "embed_cache.f32.sqlite3-wal") e = "f32_wal"
    else if (b == "embed_cache.f32.sqlite3-shm") e = "f32_shm"
    else if (b == "embed_cache.i8.sqlite3") e = "i8_db"
    else if (b == "embed_cache.i8.sqlite3-wal") e = "i8_wal"
    else if (b == "embed_cache.i8.sqlite3-shm") e = "i8_shm"
    else if (b ~ /\.tmp$/) e = "tmp"
    if (e != "") { en[e]++; eb[e] += bytes }
  }

  g = ""
  if (p ~ /\.staging(\/|$)/) g = "staging"
  else if (p ~ /\.retired(\/|$)/) g = "retired"
  else if (p ~ /\.reconvert-bak(\/|$)/) g = "reconvert-bak"
  else if (p ~ /\.rebind-bak(\/|$)/) g = "rebind-bak"
  else if (p ~ /\.d\.tmp(\/|$)/) g = "d.tmp"
  else if (p ~ /\.tmp$/) g = "tmp"
  else if (r == "archives_work") g = "archives_work"
  else if (r == "_conv_cache" && conv_orphan(p)) g = "conv_orphan"
  else if (r == "_legacy_cache" && legacy_orphan(p)) g = "legacy_orphan"
  if (g != "" && !(ino in gs)) { gs[ino] = 1; gn[g]++; gb[g] += bytes }
}
END {
  printf "T\t-\t%.0f\t%.0f\t%.0f\t%.0f\n", tnd, tb, tn, traw
  for (r in rn) printf "R\t%s\t%.0f\t%.0f\t%.0f\n", r, rna[r], rb[r], rn[r]
  for (k in kn) printf "K\t%s\t%.0f\t%.0f\n", k, kn[k], kb[k]
  for (g in gn) printf "G\t%s\t%.0f\t%.0f\n", g, gn[g], gb[g]
  for (e in en) printf "E\t%s\t%.0f\t%.0f\n", e, en[e], eb[e]
  printf "L\tconv_l1\t%.0f\t%.0f\n", l1n, l1b
  printf "B\tbad\t%.0f\n", bad
  printf "S\tsrcok\t%d\n", SRCOK
}
AWK

# st.<i> から 1 つの値を引く: stv <i> <TAG> <KEY> <列番号>
stv() { awk -F'\t' -v t="$2" -v k="$3" -v c="$4" '$1 == t && $2 == k { print $c; f = 1; exit } END { if (!f) print 0 }' "$TMP/st.$1"; }

NW=0
BASEGARB=()
scan_worlds() {
  prog "派生物の走査（件数が多いと数分かかります）"
  local d name i=0 reg src
  readable_dir "$DERIVED" || return 0
  for d in "$DERIVED"/*; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    # 置き換えの途中や退避で残ったフォルダは資料フォルダではなくゴミの候補として数える
    case "$name" in
      *.reconvert-bak|*.rebind-bak|*.staging|*.retired|*.tmp) BASEGARB+=("$d"); continue ;;
    esac
    i=$((i + 1))
    WDIR[$i]="$d"
    reg="?"; src=""
    if [ "$REGMODE" = ok ]; then
      src="$(awk -F'\t' -v w="$name" '$1 == w { print $2; exit }' "$REG")"
      if awk -F'\t' -v w="$name" '$1 == w { f = 1 } END { exit !f }' "$REG"; then reg=1; else reg=0; fi
    elif [ -d "$KBDIR/$name" ]; then
      src="$KBDIR/$name"; reg=1
    fi
    WREG[$i]="$reg"
    SRCOK=0
    WSRCNOTE[$i]=""
    if [ "$DEEP" != 1 ]; then
      WSRCNOTE[$i]="deep"
    elif [ -n "$src" ] && [ -d "$src" ]; then
      # 派生物側と同じ表記で揃える（名前のエスケープ差で一致しなくなるのを避ける）
      : >"$TMP/srcerr.$i"
      ( cd "$src" 2>/dev/null && list_tree nd 2>"$TMP/srcerr.$i" ) | awk -f "$TMP/lsname.awk" >"$TMP/src.$i"
      SRCOK=1
      # 一部でも読めなかったら、読めなかった所の原本を「無い」と見てしまうので突き合わせない
      if [ -s "$TMP/srcerr.$i" ]; then SRCOK=0; WSRCNOTE[$i]="fail"; fi
      # 一覧が空なのに原本にファイルがあるなら、突き合わせない（原本が無いものを過大に数えない）
      if [ ! -s "$TMP/src.$i" ] && [ -n "$( ( cd "$src" 2>/dev/null && find . ! -type d 2>/dev/null | head -n 1 ) )" ]; then SRCOK=0; WSRCNOTE[$i]="fail"; fi
      if [ ! -s "$TMP/src.$i" ] && ! ( cd "$src" ) 2>/dev/null; then SRCOK=0; WSRCNOTE[$i]="fail"; fi
    else
      WSRCNOTE[$i]="fail"
    fi
    CDFAIL=0
    ( cd "$d" ) 2>/dev/null || CDFAIL=1
    if [ "$CDFAIL" = 1 ]; then : >"$TMP/ls.$i"; echo "Permission denied" >"$TMP/err.$i"
    else ( cd "$d" 2>/dev/null && list_tree f 2>"$TMP/err.$i" ) >"$TMP/ls.$i"; fi
    awk -v U=512 -v SRC="$TMP/src.$i" -v SRCOK="$SRCOK" -f "$TMP/scan.awk" "$TMP/ls.$i" "$TMP/ls.$i" >"$TMP/st.$i"
    WDENY[$i]="$(grep -ci 'permission denied' "$TMP/err.$i" 2>/dev/null)"
    rm -f "$TMP/ls.$i" "$TMP/src.$i"
  done
  NW=$i
}

# 資料フォルダ名 → ES の索引名のもと（sherpa/es_index.py の _index と同じ規則）
make_bases() {
  : >"$TMP/bases.tsv"
  [ "$NW" -gt 0 ] || return 0
  have python3 || return 0
  local i
  for i in $(idxs); do basename "${WDIR[$i]}"; done | python3 -I -c '
import hashlib,re,sys
for w in sys.stdin.read().split("\n"):
    if not w:
        continue
    slug = re.sub(r"[^a-z0-9._-]", "-", w.lower())[:40].strip("-._") or "w"
    print("%s\tsherpa-kb-%s-%s" % (w, slug, hashlib.sha1(w.encode("utf-8")).hexdigest()[:10]))
' >"$TMP/bases.tsv" 2>/dev/null
}

LABEL=()
rank_worlds() {
  : >"$TMP/baselabel.tsv"; : >"$TMP/order.idx"
  [ "$NW" -gt 0 ] || return 0
  local i reg bytes idx w=0 u=0
  : >"$TMP/rank.in"
  for i in $(idxs); do printf '%s\t%s\t%s\n' "${WREG[$i]}" "$(stv "$i" T - 4)" "$i" >>"$TMP/rank.in"; done
  # 大きい順に、登録済み（または不明）を W1.. 、登録されていないものを U1.. と名付ける
  sort -t"$TAB" -k2,2nr "$TMP/rank.in" >"$TMP/rank.sorted"
  while IFS="$TAB" read -r reg bytes idx; do
    if [ "$reg" = 0 ]; then u=$((u + 1)); LABEL[$idx]="U$u"; else w=$((w + 1)); LABEL[$idx]="W$w"; fi
  done <"$TMP/rank.sorted"
  for i in $(idxs); do printf '%s\t%s\n' "${LABEL[$i]}" "$i"; done |
    awk -F'\t' '{ t = substr($1, 1, 1); n = substr($1, 2) + 0; print (t == "W" ? 0 : 1), n, $2 }' | sort -n -k1,1 -k2,2 | awk '{ print $3 }' >"$TMP/order.idx"
  if [ -s "$TMP/bases.tsv" ]; then
    for i in $(idxs); do
      awk -F'\t' -v w="$(basename "${WDIR[$i]}")" -v l="${LABEL[$i]}" '$1 == w { print $2 "\t" l; exit }' "$TMP/bases.tsv" >>"$TMP/baselabel.tsv"
    done
  fi
}

UNREG_N=0; UNREG_B=0
sec_derived() {
  local i n g k kc kb
  if [ -d "$DERIVED" ] && ! readable_dir "$DERIVED"; then B "[派生物] 調べられません（権限）"; return 0; fi
  if [ ! -d "$DERIVED" ]; then B "[派生物] 見つかりません（派生物の置き場がありません）"; return 0; fi
  if [ "$NW" = 0 ]; then B "[派生物] 資料フォルダの派生物はありません"; return 0; fi
  D ""
  D "================ 派生物（資料フォルダごと）================"
  D "大きさはディスク上の実際の大きさ。同じ実体へのハードリンクは 1 回だけ数えます（ファイルのみ・フォルダ自体は含まず）。"
  D "「単独」は、その役割の中だけで重複を除いた値。「実際」は全部を通して重複を除いた値。"
  S "$(row '9:[派生物]' '-9:実際' '-7:md' '-7:rag' '-7:ir' '-7:conv' '-7:legacy' '-7:ゴミ')"
  local shown=0
  for i in $(cat "$TMP/order.idx"); do
    local lab="${LABEL[$i]}" total garb line
    total="$(stv "$i" T - 4)"
    garb="$(awk -F'\t' '$1 == "G" { s += $4 } END { printf "%.0f", s }' "$TMP/st.$i")"
    line="$(row "9: $lab" "-9:$(hs "$total")" "-7:$(hs "$(stv "$i" R md 4)")" "-7:$(hs "$(stv "$i" R rag 4)")" "-7:$(hs "$(stv "$i" R ir 4)")" "-7:$(hs "$(stv "$i" R _conv_cache 4)")" "-7:$(hs "$(stv "$i" R _legacy_cache 4)")" "-7:$(hs "$garb")")"
    shown=$((shown + 1))
    if [ "$shown" -le 8 ]; then S "$line"; elif [ "$shown" = 9 ]; then S " 他 $((NW - 8)) 件は詳細版を見てください"; fi
    if [ "${WREG[$i]}" = 0 ]; then UNREG_N=$((UNREG_N + 1)); UNREG_B=$((UNREG_B + total)); fi

    D ""
    D "---- $lab ----"
    D "$(row '40:実際の大きさ（重複除外）' "-10:$(hs "$total")" "-12:$(stv "$i" T - 3) 件")"
    D "$(row '40:重複を除かない合計' "-10:$(hs "$(stv "$i" T - 6)")" "-12:$(stv "$i" T - 5) 件")"
    case "${WREG[$i]}" in
      0) D "登録: されていません（どの資料フォルダにも対応しない派生物）" ;;
      1) D "登録: されています" ;;
      *) D "登録: 登録情報が取れないため不明" ;;
    esac
    if [ "${WSRCNOTE[$i]}" = fail ]; then D "原本の一覧: 取れませんでした（原本の有無を使う項目は調べられません）"; fi
    [ "${WDENY[$i]:-0}" -gt 0 ] 2>/dev/null && D "読めません（権限）: ${WDENY[$i]} 箇所"
    [ "$(stv "$i" B bad 3)" -gt 0 ] 2>/dev/null && D "解釈できなかった行: $(stv "$i" B bad 3) 行"
    D "$(row '24:役割' '-10:単独' '-9:件数' '-11:重複込み')"
    for g in md rag ir _conv_cache _legacy_cache _metafile_render_cache archives archives_work search_cache semantic other "(root)"; do
      [ "$(stv "$i" R "$g" 3)" = 0 ] && continue
      D "$(row "24: $g" "-10:$(hs "$(stv "$i" R "$g" 4)")" "-9:$(stv "$i" R "$g" 3)" "-11:$(stv "$i" R "$g" 5)")"
    done
    for g in staging retired; do
      [ "$(stv "$i" G "$g" 3)" = 0 ] && continue
      D "$(row "24: *.$g" "-10:$(hs "$(stv "$i" G "$g" 4)")" "-9:$(stv "$i" G "$g" 3)" "-11:")"
    done
    D "$(row '24:種類（末尾）' '-10:大きさ' '-9:件数')"
    for g in document.json evidence.json context.json rag.md rag_chunks.jsonl md md.meta.json assets_img assets_other pages ocr_route.json derived.json legacy_ooxml other; do
      D "$(row "24: $g" "-10:$(hs "$(stv "$i" K "$g" 4)")" "-9:$(stv "$i" K "$g" 3)")"
    done
    D "_conv_cache のうちリンク数 1（公開側と共有せず二重持ちの疑い）: $(stv "$i" L conv_l1 3) 件 $(hs "$(stv "$i" L conv_l1 4)")"
    D "$(row '40:埋め込みキャッシュ' '-10:大きさ' '-9:件数')"
    for g in json_db json_wal json_shm f32_db f32_wal f32_shm i8_db i8_wal i8_shm tmp; do
      [ "$(stv "$i" E "$g" 3)" = 0 ] && continue
      case "$g" in
        json_db) n="embed_cache.sqlite3（JSON 形式）" ;; json_wal) n="  -wal" ;; json_shm) n="  -shm" ;;
        f32_db) n="embed_cache.f32.sqlite3（float32 形式）" ;; f32_wal) n="  f32 -wal" ;; f32_shm) n="  f32 -shm" ;;
        i8_db) n="embed_cache.i8.sqlite3（新形式）" ;; i8_wal) n="  i8 -wal" ;; i8_shm) n="  i8 -shm" ;; *) n=".tmp" ;;
      esac
      D "$(row "40: $n" "-10:$(hs "$(stv "$i" E "$g" 4)")" "-9:$(stv "$i" E "$g" 3)")"
    done
  done
  cat "$TMP"/st.* >"$TMP/allk" 2>/dev/null
  local all_total
  all_total="$(awk -F'\t' '$1 == "T" { s += $4 } END { printf "%.0f", s }' "$TMP/allk")"
  D ""
  D "全資料フォルダの実際の大きさの合計: $(hs "$all_total")"
  awk -F'\t' '$1 == "K" && $2 != "other" { n[$2] += $3; b[$2] += $4 } END { for (k in n) printf "%s\t%.0f\t%.0f\n", k, n[k], b[k] }' "$TMP/allk" | sort -t"$TAB" -k3,3nr >"$TMP/kinds"
  D ""
  D "$(row '24:種類（全資料フォルダ）' '-10:大きさ' '-9:件数')"
  while IFS="$TAB" read -r k kc kb; do D "$(row "24: $k" "-10:$(hs "$kb")" "-9:$kc")"; done <"$TMP/kinds"
  local ks=() cnt=0
  while IFS="$TAB" read -r k kc kb; do
    cnt=$((cnt + 1)); [ "$cnt" -gt 6 ] && break
    ks[$cnt]="$(row "17:$k" "-7:$(hs "$kb")")"
  done <"$TMP/kinds"
  S "[種類]"
  [ "$cnt" -ge 1 ] && S " ${ks[1]}   ${ks[2]}"
  [ "$cnt" -ge 3 ] && S " ${ks[3]}   ${ks[4]}"
  [ "$cnt" -ge 5 ] && S " ${ks[5]}   ${ks[6]}"
  return 0
}

# ---------------------------------------------------------------- 埋め込みキャッシュの行数
EMBED_ROWS=0
sec_embed() {
  prog "埋め込み"
  local i f lab r tbl av dim
  [ "$NW" -gt 0 ] || return 0
  D ""
  D "================ 埋め込みキャッシュ ================"
  dim="$(pgq "select value from system_settings where key = 'embed_dim'" 2>/dev/null | head -n 1)"
  case "$dim" in *[!0-9]*) dim="(数字ではない値)" ;; esac
  if [ -n "$dim" ]; then D "管理画面の設定の次元: $dim"
  elif [ "$REGMODE" = ok ]; then D "管理画面の設定の次元: 設定なし（既定）"
  else D "管理画面の設定の次元: 調べられません（DB に届きません）"; fi
  local any=0
  for i in $(cat "$TMP/order.idx"); do
    lab="${LABEL[$i]}"
    f="${WDIR[$i]}/semantic/embed_cache.sqlite3"
    if [ -f "$f" ]; then
      any=1
      r="$(sqlq "$f" 'select count(*) from kv')"
      if [ -z "$r" ]; then D "$lab JSON 形式: 行数を調べられません（sqlite3 / python3 が無いか、開けません）"
      else
        EMBED_ROWS=$((EMBED_ROWS + r))
        if [ "$DEEP" = 1 ]; then
          av="$(sqlq "$f" 'select count(*), cast(avg(length(vec)) as integer) from (select vec from kv limit 5000)')"
          D "$lab JSON 形式: $r 行（先頭 5000 行の 1 行平均 ${av#*|} バイト）"
        else
          D "$lab JSON 形式: $r 行（--deep で 1 行の平均バイト数も調べます）"
        fi
      fi
    fi
    f="${WDIR[$i]}/semantic/embed_cache.f32.sqlite3"
    if [ -f "$f" ]; then
      any=1
      tbl="$(sqlq "$f" "select name from sqlite_master where type = 'table' and name not like 'sqlite_%' order by name")"
      if [ -n "$tbl" ]; then r="$(sqlq "$f" "select count(*) from \"$tbl\"")"; D "$lab float32 形式: ${r:-調べられません} 行"
      else D "$lab float32 形式: 行数を調べられません"; fi
    fi
    f="${WDIR[$i]}/semantic/embed_cache.i8.sqlite3"
    if [ -f "$f" ]; then
      any=1
      r="$(sqlq "$f" 'select count(*) from kv3')"
      D "$lab 新形式: ${r:-調べられません} 行"
    fi
  done
  [ "$any" = 1 ] || D "埋め込みキャッシュはありません"
  local ej ef ei fl il
  ej="$(awk -F'\t' '$1 == "E" && ($2 == "json_db" || $2 == "json_wal" || $2 == "json_shm") { s += $4 } END { printf "%.0f", s }' "$TMP"/st.* 2>/dev/null)"
  ef="$(awk -F'\t' '$1 == "E" && ($2 == "f32_db" || $2 == "f32_wal" || $2 == "f32_shm") { s += $4 } END { printf "%.0f", s }' "$TMP"/st.* 2>/dev/null)"
  ei="$(awk -F'\t' '$1 == "E" && ($2 == "i8_db" || $2 == "i8_wal" || $2 == "i8_shm") { s += $4 } END { printf "%.0f", s }' "$TMP"/st.* 2>/dev/null)"
  if [ "${ef:-0}" = 0 ]; then fl="-"; else fl="$(hs "$ef")"; fi
  if [ "${ei:-0}" = 0 ]; then il="-"; else il="$(hs "$ei")"; fi
  S "[埋め込み] キャッシュ JSON $(hs "${ej:-0}")  float32 $fl  新形式 $il  行数 $EMBED_ROWS"
  return 0
}

# ---------------------------------------------------------------- ③ ゴミの候補
GARB_DERIVED_B=0; GARB_ES_B=0; GARB_UNKNOWN=0
sec_garbage() {
  prog "ゴミの候補"
  local g n b lab r tot_b=0 out_n=0 out_b=0 i
  # 区分ごとの行は登録済み（と不明）の資料フォルダだけ。登録されていないものは「フォルダ全体」の 1 行だけで足す
  : >"$TMP/st.reg"
  for i in $(idxs); do [ "${WREG[$i]}" = 0 ] || cat "$TMP/st.$i" >>"$TMP/st.reg"; [ "${WDENY[$i]:-0}" -gt 0 ] 2>/dev/null && GARB_UNKNOWN=1; done
  D ""
  D "================ ゴミの候補（消さずに数えるだけ）================"
  D "取り込みが止まっている前提で消せるかもしれないもの。区分どうしは重ならないように数えています。"
  D "$(row '40:区分' '-9:件数' '-10:大きさ')"
  [ "$DEEP" = 1 ] || D "原本との突き合わせ（原本が無いキャッシュの数え）は --deep で行います。"
  for g in staging retired reconvert-bak rebind-bak d.tmp tmp archives_work conv_orphan legacy_orphan; do
    if [ "$DEEP" != 1 ] && { [ "$g" = conv_orphan ] || [ "$g" = legacy_orphan ]; }; then
      case "$g" in conv_orphan) lab="_conv_cache の原本が無いもの" ;; *) lab="_legacy_cache の原本が無いもの" ;; esac
      D "$(row "40: $lab" '-19:--deep で調べます')"
      continue
    fi
    n="$(awk -F'\t' -v g="$g" '$1 == "G" && $2 == g { s += $3 } END { printf "%.0f", s }' "$TMP/st.reg")"
    b="$(awk -F'\t' -v g="$g" '$1 == "G" && $2 == g { s += $4 } END { printf "%.0f", s }' "$TMP/st.reg")"
    case "$g" in
      staging) lab="*.staging" ;; retired) lab="*.retired" ;; reconvert-bak) lab="*.reconvert-bak" ;;
      rebind-bak) lab=".rebind-bak（資料フォルダの中）" ;; d.tmp) lab="*.d.tmp" ;; tmp) lab="*.tmp" ;;
      archives_work) lab="archives_work/" ;; conv_orphan) lab="_conv_cache の原本が無いもの" ;; *) lab="_legacy_cache の原本が無いもの" ;;
    esac
    if [ -d "$DERIVED" ] && ! readable_dir "$DERIVED"; then D "$(row "40: $lab" '-19:調べられません（権限）')"; continue; fi
    D "$(row "40: $lab" "-9:$n" "-10:$(hs "$b")")"
    tot_b=$((tot_b + b))
  done
  if [ -d "$DERIVED" ] && ! readable_dir "$DERIVED"; then
    GARB_UNKNOWN=1
    D "$(row '40: 派生物の置き場の直下の退避・一時' '-19:調べられません（権限）')"
    D "$(row '40: 登録されていない派生物のフォルダ' '-19:調べられません（権限）')"
  else
    if [ -d "$DERIVED" ]; then
      r="$(dd_sum "$DERIVED"/.[!.]* ${BASEGARB[@]+"${BASEGARB[@]}"} 2>/dev/null)"
      [ -n "$r" ] && { out_n="${r% *}"; out_b="${r#* }"; }
    fi
    D "$(row '40: 派生物の置き場の直下の退避・一時' "-9:$out_n" "-10:$(hs "$out_b")")"
    tot_b=$((tot_b + out_b))
    D "$(row '40: 登録されていない派生物のフォルダ（全体）' "-9:$UNREG_N" "-10:$(hs "$UNREG_B")")"
    tot_b=$((tot_b + UNREG_B))
  fi
  local logn=0 logb=0
  if [ -d "$RUNDIR" ] && ! readable_dir "$RUNDIR"; then
    GARB_UNKNOWN=1
    D "$(row '40: 退避済みの古いログ' '-19:調べられません（権限）')"
  else
  if [ -d "$RUNDIR" ]; then
    r="$(find "$RUNDIR" -maxdepth 1 -type f -name '*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]*' -ls 2>/dev/null | awk -v U="$BLKUNIT" '!($1 in s) { s[$1] = 1; n++; b += $2 * U } END { printf "%d %.0f", n, b }')"
    [ -n "$r" ] && { logn="${r% *}"; logb="${r#* }"; }
  fi
  D "$(row '40: 退避済みの古いログ' "-9:$logn" "-10:$(hs "$logb")")"
  tot_b=$((tot_b + logb))
  fi
  local rep_n=0 rep_b=0
  if [ -d "$OUTDIR" ]; then
    r="$(find "$OUTDIR" -maxdepth 1 -type f -name 'disk-report-*' -ls 2>/dev/null | awk -v U="$BLKUNIT" '{ n++; b += $2 * U } END { printf "%d %.0f", n, b }')"
    [ -n "$r" ] && { rep_n="${r% *}"; rep_b="${r#* }"; }
  fi
  D "$(row '40: この容量レポート自身（10 本まで残す）' "-9:$rep_n" "-10:$(hs "$rep_b")")"
  GARB_DERIVED_B="$tot_b"
  if [ "$GARB_UNKNOWN" = 1 ]; then D "$(row '40: 合計（派生物側＋ログ）' '-9:' "-10:$(hs "$tot_b")（読めた分だけ）")"
  else D "$(row '40: 合計（派生物側＋ログ）' '-9:' "-10:$(hs "$tot_b")")"; fi
  return 0
}

# ---------------------------------------------------------------- ④ Elasticsearch
sec_es() {
  prog "Elasticsearch"
  D ""
  D "================ Elasticsearch ================"
  if ! have curl; then B "[ES] 調べられません（curl が無い）"; return 0; fi
  if ! tmo 15 curl -s -m 8 -f "$ES_URL/" >/dev/null 2>&1; then B "[ES] つながりません"; return 0; fi
  tmo 20 curl -s -m 15 -f "$ES_URL/_cat/indices?h=index,docs.count,store.size,pri.store.size&bytes=b&expand_wildcards=all" 2>/dev/null >"$TMP/es.cat"
  tmo 20 curl -s -m 15 -f "$ES_URL/_cat/aliases?h=alias,index" 2>/dev/null >"$TMP/es.alias"
  # 索引ごとに ラベル・状態（別名が指す／旧形式の実体／別名なし）を決める
  awk -v LBL="$TMP/baselabel.tsv" -v AL="$TMP/es.alias" '
    BEGIN {
      while ((getline l < LBL) > 0) { split(l, a, "\t"); lab[a[1]] = a[2] }
      while ((getline l < AL) > 0) { split(l, a, " "); tgt[a[2]] = 1 }
    }
    {
      idx = $1
      if (idx !~ /^sherpa-kb-/) { other_b += $3; other_n++; next }
      base = idx; gen = ""
      if (match(base, /-g[0-9]+$/)) { gen = substr(base, RSTART + 1); base = substr(base, 1, RSTART - 1) }
      l = (base in lab) ? lab[base] : "W?"
      state = (idx in tgt || gen == "") ? "使用中" : "別名なし"
      printf "%s\t%s\t%s\t%s\t%s\t%s\n", l, (gen == "" ? "-" : gen), $2, $3, $4, state
    }
    END { if (other_n) printf "(他)\t-\t0\t%.0f\t0\t他\n", other_b }' "$TMP/es.cat" >"$TMP/es.rows"
  sort -t"$TAB" -k4,4nr "$TMP/es.rows" >"$TMP/es.rows.s"
  D "$(row '8:資料' '16:世代' '-12:件数' '-10:大きさ' '-10:主のみ' '2: 状態')"
  local l gen dc sz pri st
  while IFS="$TAB" read -r l gen dc sz pri st; do
    D "$(row "8:$l" "16:$gen" "-12:$dc" "-10:$(hs "$sz")" "-10:$(hs "$pri")" "2: $st")"
  done <"$TMP/es.rows.s"
  local n_idx b_use n_old b_old
  n_idx="$(awk -F'\t' '$6 != "他"' "$TMP/es.rows" | wc -l | tr -d ' ')"
  b_use="$(awk -F'\t' '$6 == "使用中" { s += $4 } END { printf "%.0f", s }' "$TMP/es.rows")"
  n_old="$(awk -F'\t' '$6 == "別名なし"' "$TMP/es.rows" | wc -l | tr -d ' ')"
  b_old="$(awk -F'\t' '$6 == "別名なし" { s += $4 } END { printf "%.0f", s }' "$TMP/es.rows")"
  GARB_ES_B="$b_old"
  D "別名が指していない古い世代の索引: $n_old 本 $(hs "$b_old")（ゴミの候補）"
  local fs ftot fav
  fs="$(tmo 20 curl -s -m 15 -f "$ES_URL/_nodes/stats/fs" 2>/dev/null | grep -o '"total":{[^}]*}' | head -n 1)"
  ftot="$(printf '%s' "$fs" | grep -o '"total_in_bytes":[0-9]*' | head -n 1 | cut -d: -f2)"
  fav="$(printf '%s' "$fs" | grep -o '"available_in_bytes":[0-9]*' | head -n 1 | cut -d: -f2)"
  if [ -n "$ftot" ]; then D "ES から見たディスク: 全体 $(hs "$ftot") 空き $(hs "$fav")"; else D "ES から見たディスク: 調べられません"; fi
  # 別名が指す（または旧形式の）索引のマッピング
  tmo 20 curl -s -m 15 -f "$ES_URL/_cat/indices/sherpa-kb-*?h=index&expand_wildcards=all" 2>/dev/null >"$TMP/es.names"
  awk -v AL="$TMP/es.alias" 'BEGIN { while ((getline l < AL) > 0) { split(l, a, " "); tgt[a[2]] = 1 } }
    { if ($1 in tgt || $1 !~ /-g[0-9]+$/) print $1 }' "$TMP/es.names" >"$TMP/es.inuse"
  D ""
  D "$(row '8:資料' '-8:次元' '-18:_source.excludes')"
  local ix base lbl m dims ex f body res
  for ix in $(cat "$TMP/es.inuse"); do
    base="$(printf '%s' "$ix" | sed -E 's/-g[0-9]+$//')"
    lbl="$(awk -F'\t' -v b="$base" '$1 == b { print $2; exit }' "$TMP/baselabel.tsv")"; [ -n "$lbl" ] || lbl="W?"
    m="$(tmo 20 curl -s -m 15 -f "$ES_URL/$ix/_mapping" 2>/dev/null)"
    dims="$(printf '%s' "$m" | grep -o '"dims":[0-9]*' | head -n 1 | cut -d: -f2)"
    if printf '%s' "$m" | grep -q '"excludes"'; then ex="あり"; else ex="なし"; fi
    D "$(row "8:$lbl" "-8:${dims:--}" "-18:$ex")"
    if [ "$DEEP" = 1 ]; then
      for f in ext vector_status content_type; do
        if [ "$f" = content_type ] && ! printf '%s' "$m" | grep -q '"content_type"'; then D "  $f: 索引に無い"; continue; fi
        body="{\"size\":0,\"aggs\":{\"a\":{\"terms\":{\"field\":\"$f\",\"size\":40}}}}"
        res="$(tmo 120 curl -s -m 100 -f -H 'Content-Type: application/json' -d "$body" "$ES_URL/$ix/_search" 2>/dev/null)"
        if [ -z "$res" ]; then D "  $f: 調べられません"; continue; fi
        D "  $f: $(printf '%s' "$res" | grep -o '{"key":"[^"]*","doc_count":[0-9]*}' | sed -E "s/\{\"key\":\"([^\"]*)\",\"doc_count\":([0-9]*)\}/\1${TAB}\2/" | awk -F'\t' -v m="$f" '
          { ok = (m == "ext") ? (length($1) <= 9 && $1 ~ /^[.][a-z0-9]+$/) : (length($1) <= 31 && $1 ~ /^[a-z][a-z_]*$/)
            if (ok) { printf "%s=%s ", $1, $2; next }
            o += $2; n++ }
          END { if (n) printf "その他=%d", o }')"
      done
    fi
  done
  S "$(row '5:[ES]' "-9:索引 ${n_idx} 本" "-16:使用中 $(hs "$b_use")" "-26:別名なし $n_old 本 $(hs "$b_old")")"
  [ -n "$fav" ] && S "$(row '5:' "-30:ES から見たディスクの空き $(hs "$fav")")"
  return 0
}

# ---------------------------------------------------------------- ⑤ PostgreSQL ⑥ Neo4j
sec_pg() {
  prog "PostgreSQL"
  D ""
  D "================ PostgreSQL ================"
  local size wal lg
  size="$(pgq "select pg_database_size(current_database())" 2>/dev/null | head -n 1)"
  if [ -z "$size" ]; then
    if [ -z "$PGC" ] && ! have psql; then B "[PG] 調べられません（コンテナも psql も無い）"
    else B "[PG] 調べられません（つながらない・または止まっています）"; fi
    return 0
  fi
  D "DB の大きさ: $(hs "$size")"
  D "$(row '34:表（上位 15）' '-10:表' '-10:索引')"
  pgq "select c.relname, pg_table_size(c.oid), pg_indexes_size(c.oid) from pg_class c join pg_namespace n on n.oid = c.relnamespace where c.relkind = 'r' and n.nspname = 'public' order by pg_total_relation_size(c.oid) desc limit 15" 2>/dev/null |
    while IFS="$TAB" read -r t a b; do D "$(row "34: $t" "-10:$(hs "$a")" "-10:$(hs "$b")")"; done
  wal="$(pgq "select coalesce(sum(size), 0) from pg_ls_waldir()" 2>/dev/null | head -n 1)"
  if [ -z "$wal" ] && [ -n "$PGC" ]; then
    wal="$(tmo 30 docker exec "$PGC" sh -c 'du -sk "${PGDATA:-/var/lib/postgresql/data}/pg_wal" 2>/dev/null' 2>/dev/null | awk 'NF { print $1 * 1024 }')"
  fi
  if [ -n "$wal" ]; then D "WAL（pg_wal の合計）: $(hs "$wal")"; else D "WAL: 調べられません"; fi
  if [ -n "$PGC" ]; then
    lg="$(tmo 30 docker exec "$PGC" sh -c 'for d in /var/log/postgresql "${PGDATA:-/var/lib/postgresql/data}/log"; do [ -d "$d" ] && du -sk "$d" 2>/dev/null; done' 2>/dev/null | awk '{ s += $1 } END { print s * 1024 }')"
    D "コンテナの中のログ: $(hs "${lg:-0}")（標準出力のログは Docker のコンテナのログ側）"
  fi
  S "$(row '5:[PG]' "-9:$(hs "$size")" "-14:(WAL $(if [ -n "$wal" ]; then hs "$wal"; else echo -; fi))")"
  return 0
}

sec_neo() {
  prog "Neo4j"
  D ""
  D "================ Neo4j ================"
  if [ -z "$NEOC" ]; then
    D "Neo4j: 調べられません（起動中のコンテナがありません）"
    S "$(row '8:[Neo4j]' '-30:調べられません（止まっています）')"
    return 0
  fi
  local out dbk txk allk lgk oth
  out="$(tmo 60 docker exec "$NEOC" sh -c 'for d in /data/databases /data/transactions /data /logs; do du -sk "$d" 2>/dev/null; done' 2>/dev/null)"
  if [ -z "$out" ]; then
    D "Neo4j: コンテナの中を読めません（権限）"
    S "$(row '8:[Neo4j]' '-30:読めません（権限）')"
    return 0
  fi
  dbk="$(printf '%s\n' "$out" | awk '$2 == "/data/databases" { print $1 }')"
  txk="$(printf '%s\n' "$out" | awk '$2 == "/data/transactions" { print $1 }')"
  allk="$(printf '%s\n' "$out" | awk '$2 == "/data" { print $1 }')"
  lgk="$(printf '%s\n' "$out" | awk '$2 == "/logs" { print $1 }')"
  : "${dbk:=0}" "${txk:=0}" "${allk:=0}" "${lgk:=0}"
  oth=$((allk - dbk - txk)); [ "$oth" -lt 0 ] && oth=0
  D "/data 全体: $(hs $((allk * 1024)))"
  D "  databases: $(hs $((dbk * 1024)))"
  D "  transactions（トランザクションログ）: $(hs $((txk * 1024)))"
  D "  その他: $(hs $((oth * 1024)))"
  D "/logs: $(hs $((lgk * 1024)))"
  S "$(row '8:[Neo4j]' "-22:data $(hs $((allk * 1024))) (tx $(hs $((txk * 1024))))" "-12:logs $(hs $((lgk * 1024)))")"
  return 0
}

# ---------------------------------------------------------------- ⑦ Docker
role_of() { # 名前 → 役割名（Sherpa が決める部分だけ）
  case "$1" in
    *postgres*) echo postgres ;;
    *elasticsearch*) echo elasticsearch ;;
    *neo4j*) echo neo4j ;;
    *ocr*) echo ocr-worker ;;
    *) echo other ;;
  esac
}

# docker の「1.2GB」「512kB」を B に直す（docker は 1000 進）
h2b='function h2b(v,   u, m, x) { u = v; sub(/^[0-9.]+/, "", u); x = v + 0; m = 1
  if (u == "kB" || u == "KB") m = 1000; else if (u == "MB") m = 1e6; else if (u == "GB") m = 1e9; else if (u == "TB") m = 1e12
  return x * m }'

h2bytes() { awk "$h2b"' BEGIN { printf "%.0f", h2b(ARGV[1]) }' "$1"; }

sec_docker() {
  prog "Docker"
  D ""
  D "================ Docker ================"
  if [ "$HAVE_DOCKER" != 1 ]; then
    if have docker; then B "[Docker] 調べられません（docker に接続できません）"; else B "[Docker] 調べられません（docker がありません）"; fi
    return 0
  fi
  tmo 60 docker system df --format "{{.Type}}${TAB}{{.TotalCount}}${TAB}{{.Active}}${TAB}{{.Size}}${TAB}{{.Reclaimable}}" 2>/dev/null >"$TMP/dock.df"
  D "$(row '18:種類' '-6:数' '-8:使用中' '-12:大きさ' '-18:回収できる量')"
  local ty tc ac sz rc
  while IFS="$TAB" read -r ty tc ac sz rc; do
    case "$ty" in "Images") ty="イメージ" ;; "Containers") ty="コンテナ" ;; "Local Volumes") ty="ボリューム" ;; "Build Cache") ty="ビルドキャッシュ" ;; esac
    D "$(row "18:$ty" "-6:$tc" "-8:$ac" "-12:$sz" "-18:$rc")"
  done <"$TMP/dock.df"
  local img_sz img_rc vol_sz bc_sz
  img_sz="$(awk -F'\t' '$1 == "Images" { print $4 }' "$TMP/dock.df")"; img_rc="$(awk -F'\t' '$1 == "Images" { print $5 }' "$TMP/dock.df")"
  vol_sz="$(awk -F'\t' '$1 == "Local Volumes" { print $4 }' "$TMP/dock.df")"
  bc_sz="$(awk -F'\t' '$1 == "Build Cache" { print $4 }' "$TMP/dock.df")"
  img_rc="${img_rc%% *}"
  img_sz="$(hs "$(h2bytes "$img_sz")")"; img_rc="$(hs "$(h2bytes "$img_rc")")"
  vol_sz="$(hs "$(h2bytes "$vol_sz")")"; bc_sz="$(hs "$(h2bytes "$bc_sz")")"

  # ボリューム（役割名で）
  tmo 90 docker system df -v 2>/dev/null | awk '
    /^Local Volumes space usage:/ { f = 1; next }
    f && /^$/ { if (seen) exit; next }
    f && /^VOLUME NAME/ { seen = 1; next }
    f && seen { print $1 "\t" $2 "\t" $3 }' >"$TMP/dock.vol"
  tmo 30 docker volume ls -q -f dangling=true 2>/dev/null >"$TMP/dock.dangling"
  D ""
  D "$(row '22:ボリューム' '-8:つながり' '-12:大きさ' '2: 状態')"
  local nm lk vs rl st orph_n=0 orph_sz=""
  while IFS="$TAB" read -r nm lk vs; do
    case "$nm" in
      *_pg) rl=postgres ;; *_es) rl=elasticsearch ;; *_neo4j) rl=neo4j ;; *_ollama) rl=ollama ;;
      *) rl="" ;;
    esac
    if [ -z "$rl" ]; then
      if printf '%s' "$nm" | grep -Eq '^[0-9a-f]{64}$'; then rl="(無名)"; else rl="その他"; fi
    fi
    if grep -qxF "$nm" "$TMP/dock.dangling" 2>/dev/null; then st="どのコンテナにもつながっていない"; orph_n=$((orph_n + 1)); orph_sz="$orph_sz$vs "
    else st="使用中"; fi
    D "$(row "22: $rl" "-8:$lk" "-12:$vs" "2: $st")"
  done <"$TMP/dock.vol"
  D "つながっていないボリューム: $orph_n 個${orph_sz:+（大きさ: ${orph_sz}）}"

  tmo 90 docker ps -a -s -f status=exited -f status=created -f status=dead --format "{{.Names}}${TAB}{{.Size}}" 2>/dev/null >"$TMP/dock.stopped"
  local stop_n stop_b
  stop_n="$(wc -l <"$TMP/dock.stopped" | tr -d ' ')"
  stop_b="$(awk -F'\t' "$h2b"' { v = $2; sub(/ .*/, "", v); s += h2b(v) } END { printf "%.0f", s }' "$TMP/dock.stopped")"
  D "止まったコンテナ: $stop_n 個（書き込み層の合計 約$(hs "$stop_b")）"

  tmo 30 docker ps -aq 2>/dev/null >"$TMP/dock.cids"
  : >"$TMP/dock.usedimg"
  if [ -s "$TMP/dock.cids" ]; then tmo 60 docker inspect -f '{{.Image}}' $(cat "$TMP/dock.cids") 2>/dev/null >"$TMP/dock.usedimg"; fi
  tmo 60 docker image ls -a --no-trunc --format "{{.ID}}${TAB}{{.Size}}" 2>/dev/null >"$TMP/dock.imgs"
  local all_n un_n un_b
  all_n="$(wc -l <"$TMP/dock.imgs" | tr -d ' ')"
  awk -F'\t' -v U="$TMP/dock.usedimg" "$h2b"' BEGIN { while ((getline l < U) > 0) used[l] = 1 }
    !($1 in used) { n++; b += h2b($2) } END { printf "%d %.0f\n", n, b }' "$TMP/dock.imgs" >"$TMP/dock.unimg"
  un_n="$(cut -d' ' -f1 "$TMP/dock.unimg")"; un_b="$(cut -d' ' -f2 "$TMP/dock.unimg")"
  D "イメージ: 全部で $all_n 個、どのコンテナにも使われていない $un_n 個（約$(hs "$un_b")・層の共有で重なる分を含む）"

  D ""
  D "$(row '22:コンテナのログ' '-12:大きさ')"
  local cid lp nmx lsz tot_log=0 unreadable=0
  : >"$TMP/dock.logs"
  for cid in $(cat "$TMP/dock.cids"); do
    lp="$(tmo 15 docker inspect -f '{{.LogPath}}' "$cid" 2>/dev/null)"
    [ -n "$lp" ] || continue
    nmx="$(tmo 15 docker inspect -f '{{.Name}}' "$cid" 2>/dev/null | sed 's|^/||')"
    rl="$(role_of "$nmx")"
    lsz="$(fsize "$lp")"
    if [ -n "$lsz" ]; then printf '%s\t%s\n' "$rl" "$lsz" >>"$TMP/dock.logs"; tot_log=$((tot_log + lsz)); else unreadable=$((unreadable + 1)); fi
  done
  sort -t"$TAB" -k2,2nr "$TMP/dock.logs" | head -n 8 | while IFS="$TAB" read -r rl lsz; do D "$(row "22: $rl" "-12:$(hs "$lsz")")"; done
  [ "$unreadable" -gt 0 ] && D "読めません（権限）: $unreadable 個のコンテナのログ"
  D "ログの合計: $(hs "$tot_log")（読めたもの）"

  local logtxt="$(hs "$tot_log")"
  [ "$unreadable" -gt 0 ] && logtxt="${logtxt}（一部読めません）"
  S "[Docker] イメージ ${img_sz}（回収可 ${img_rc}）  ビルドキャッシュ $bc_sz  ボリューム $vol_sz"
  S "         止まったコンテナ $stop_n 個  孤立ボリューム $orph_n 個  未使用イメージ $un_n 個（約$(hs "$un_b")）"
  S "         コンテナのログ合計 $logtxt"
  return 0
}

# ---------------------------------------------------------------- ⑧ チャンクの種類（--deep）
sec_chunks() {
  if [ "$DEEP" != 1 ]; then D ""; D "（チャンクの種類は --deep のときだけ調べます）"; return 0; fi
  prog "チャンクの種類（--deep）"
  D ""
  D "================ チャンクの種類（--deep）================"
  if ! have python3; then B "[チャンク] 調べられません（python3 が無い）"; return 0; fi
  [ "$NW" -gt 0 ] || { D "派生物がありません"; return 0; }
  cat >"$TMP/chunks.py" <<'PY'
import gzip, json, os, re, statistics, sys
from collections import Counter

FAM = {"xlsx": "Excel", "xlsm": "Excel", "xls": "Excel", "pdf": "PDF", "docx": "Word", "doc": "Word", "pptx": "PowerPoint", "ppt": "PowerPoint"}
SUF = ".rag_chunks.jsonl"


def pct(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def opener(path):
    with open(path, "rb") as f:
        head = f.read(2)
    if head == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


for arg in sys.argv[1:]:
    label, rag = arg.split("=", 1)
    ct = Counter()
    fam = Counter()
    lens = {"table_record": [], "coordinate_record": []}
    runs = []
    files = 0
    bad = 0
    for dp, _, fns in os.walk(rag):
        for fn in fns:
            if not fn.endswith(SUF):
                continue
            files += 1
            stem = fn[: -len(SUF)]
            ext = stem.rsplit(".", 1)[-1].lower() if "." in stem else ""
            family = FAM.get(ext, "その他")
            prev = None
            run = 0
            try:
                with opener(os.path.join(dp, fn)) as f:
                    for line in f:
                        try:
                            c = json.loads(line)
                        except ValueError:
                            bad += 1
                            continue
                        t = c.get("content_type") or "(なし)"
                        if t != "(なし)" and not re.match(r"^[a-z][a-z_]{0,30}$", str(t)):
                            t = "その他"
                        ct[t] += 1
                        fam[family] += 1
                        if t in lens:
                            lens[t].append(len(c.get("body") or ""))
                        if t == "table_record" and c.get("parent_id"):
                            if c["parent_id"] == prev:
                                run += 1
                            else:
                                if run:
                                    runs.append(run)
                                prev = c["parent_id"]
                                run = 1
                        else:
                            if run:
                                runs.append(run)
                            prev = None
                            run = 0
            except OSError:
                bad += 1
            if run:
                runs.append(run)
    print("== %s  chunks ファイル %d 本%s" % (label, files, ("（読めない行・ファイル %d）" % bad) if bad else ""))
    tot = sum(ct.values())
    for t, n in ct.most_common():
        print("   %-22s %10d" % (t, n))
    for t in ("table_record", "coordinate_record"):
        v = lens[t]
        if v:
            print("   %s 本文の長さ（文字）: 平均 %.0f  中央値 %.0f  件数 %d" % (t, sum(v) / len(v), statistics.median(v), len(v)))
    if runs:
        print("   表の行の連続数（同じ parent_id）: 中央値 %.0f  90%%点 %d  表の数 %d" % (statistics.median(runs), pct(runs, 0.9), len(runs)))
    print("   拡張子の族: " + "  ".join("%s %d" % (k, fam[k]) for k in ("Excel", "PDF", "Word", "PowerPoint", "その他")))
    big = sum(ct[t] for t in ("table_record", "coordinate_record"))
    print("S|%s chunks %d  表系 %d (%.0f%%)" % (label, tot, big, (100.0 * big / tot) if tot else 0))
PY
  local args=() i
  for i in $(cat "$TMP/order.idx"); do
    [ -d "${WDIR[$i]}/rag" ] && args+=("${LABEL[$i]}=${WDIR[$i]}/rag")
  done
  if [ "${#args[@]}" = 0 ]; then D "rag/ がありません"; return 0; fi
  tmo 3000 python3 -I "$TMP/chunks.py" "${args[@]}" 2>/dev/null >"$TMP/chunks.out"
  grep -v '^S|' "$TMP/chunks.out" | while IFS= read -r l; do D "$l"; done
  S "[チャンク] (--deep)"
  grep '^S|' "$TMP/chunks.out" | head -n 4 | while IFS= read -r l; do S " ${l#S|}"; done
  return 0
}

# ---------------------------------------------------------------- 実行
sec_disk
scan_worlds
make_bases
rank_worlds
sec_derived
sec_embed
sec_garbage
sec_es
sec_pg
sec_neo
sec_docker
sec_chunks

tot_all=$((GARB_DERIVED_B + GARB_ES_B))
if [ "$GARB_UNKNOWN" = 1 ]; then
  S "[ゴミ候補] 一部調べられません（権限）  読めた分 $(hs "$tot_all")"
else
  S "[ゴミ候補] 合計 $(hs "$tot_all")（派生物・ログ $(hs "$GARB_DERIVED_B")、ES 旧世代 $(hs "$GARB_ES_B")）"
fi
[ "$DEEP" = 1 ] || S "           （原本の突き合わせは --deep）"
D ""
D "ゴミの候補の合計（派生物側＋ログ＋ES の古い世代）: $(hs "$tot_all")"

# ---------------------------------------------------------------- 保存して画面に出す
SUMF="$OUTDIR/disk-report-$NOW_F-summary.txt"
DETF="$OUTDIR/disk-report-$NOW_F-detail.txt"
# 画面には絶対パスを出さず、場所の種類とファイル名だけにする
case "$OUTDIR" in
  "$ROOT/data/run/disk-report") DETF_SHOWN="data/run/disk-report/disk-report-$NOW_F-detail.txt" ;;
  /tmp/disk-report) DETF_SHOWN="/tmp/disk-report/disk-report-$NOW_F-detail.txt" ;;
  *) DETF_SHOWN="(--out で指定した場所)/disk-report-$NOW_F-detail.txt" ;;
esac
{
  printf '%s\n' "Sherpa 容量レポート $NOW_H  版 $VERSION_TXT"
  cat "$TMP/sum"
  printf '%s\n' "詳細: $DETF_SHOWN"
} >"$TMP/sum.final"
{
  printf '%s\n' "Sherpa 容量レポート（詳細）$NOW_H  版 $VERSION_TXT"
  [ "$DEEP" = 1 ] && printf '%s\n' "モード: --deep"
  echo
  cat "$TMP/det"
} >"$TMP/det.final"
cp "$TMP/sum.final" "$SUMF" 2>/dev/null && cp "$TMP/det.final" "$DETF" 2>/dev/null
SAVED=$?
# 古いレポートは 10 本（要約と詳細の組）まで残す
ls -1 "$OUTDIR" 2>/dev/null | grep -E '^disk-report-[0-9]{8}-[0-9]{6}-detail\.txt$' | sort -r | awk 'NR > 10' | while IFS= read -r f; do
  rm -f "$OUTDIR/$f" "$OUTDIR/${f%-detail.txt}-summary.txt"
done

cat "$TMP/sum.final"
[ "$SAVED" = 0 ] || echo "保存できませんでした（保存先に書けません）"
exit 0
