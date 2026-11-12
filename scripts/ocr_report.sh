#!/usr/bin/env bash
# Sherpa OCR レポート: OCR の仕事（ocr_jobs）の状態・失敗の理由・対象の種類・画像の大きさ・同じ画像の重なりを
# 調べ、画面に 1 画面の表、ファイルに詳細を出す（読むだけ・何も書き換えない）。
# どこからでも叩ける: bash <Sherpaの置き場所>/scripts/ocr_report.sh [--root DIR] [--out DIR]
#   --root  Sherpa の置き場所（既定はこのスクリプトの 1 つ上）
#   --out   レポートの保存先（既定 <root>/data/run/ocr-report。書けなければ /tmp/ocr-report）
# 失敗した仕事を積み直すのは make ocr-retry（REASON=<この表の「失敗の理由」の左の語>）。
# 守ること: ファイル名・フォルダ名・資料フォルダ名・ホスト名・IP・ユーザー名・.env の値を出力に出さない。
#   資料フォルダは件数の多い順に W1, W2, … に置き換える（対応は DB の中だけで作り、出力に残さない）。
# 依存は bash（3.2 でも可）・coreutils。PostgreSQL へは psql か docker のコンテナ経由でつなぐ（どちらも無ければ終了コード 3）。
unset POSIXLY_CORRECT CDPATH
export LC_ALL=C
umask 077

ROOT=""
OUTDIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --root) shift; ROOT="${1:-}" ;;
    --root=*) ROOT="${1#--root=}" ;;
    --out) shift; OUTDIR="${1:-}" ;;
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

TMP="$(mktemp -d "${TMPDIR:-/tmp}/ocr-report.XXXXXX" 2>/dev/null)" || { echo "作業用フォルダを作れません" >&2; exit 2; }
trap 'rm -rf "$TMP"' EXIT
trap 'exit 130' INT TERM
: >"$TMP/sum"
: >"$TMP/det"

have() { command -v "$1" >/dev/null 2>&1; }
S() { printf '%s\n' "$*" >>"$TMP/sum"; }
D() { printf '%s\n' "$*" >>"$TMP/det"; }
B() { S "$*"; D "$*"; }

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

# ---------------------------------------------------------------- .env と PostgreSQL の入口
ENVFILE="$ROOT/.env"
envget() {
  local v
  v="$(printenv "$1" 2>/dev/null)"
  if [ -z "$v" ] && [ -f "$ENVFILE" ]; then
    v="$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$ENVFILE" 2>/dev/null | tail -n 1 | sed -E "s/^[^=]*=//; s/\r$//; s/^[\"']//; s/[\"']$//")"
  fi
  printf '%s' "$v"
}

if [ -z "$OUTDIR" ]; then OUTDIR="$ROOT/data/run/ocr-report"; fi
if ! mkdir -p "$OUTDIR" 2>/dev/null || [ ! -w "$OUTDIR" ]; then
  OUTDIR="/tmp/ocr-report"
  mkdir -p "$OUTDIR" 2>/dev/null
fi

DSN="$(envget SHERPA_PG_DSN)"; [ -n "$DSN" ] || DSN="$(envget DATABASE_URL)"

# DSN（URI 形式・キー=値 形式）から user / dbname だけ取り出す（コンテナ経由でも同じ DB を見るため）
dsn_get() { # $1=user|dbname
  local rest cred hostdb kv
  case "$DSN" in
    postgres://*|postgresql://*)
      rest="${DSN#*://}"; cred=""; hostdb="${rest##*@}"
      case "$rest" in *@*) cred="${rest%@*}" ;; esac
      if [ "$1" = user ]; then printf '%s' "${cred%%:*}"
      else hostdb="${hostdb#*/}"; printf '%s' "${hostdb%%\?*}"; fi ;;
    *)
      for kv in $DSN; do
        case "$kv" in
          user=*) [ "$1" = user ] && printf '%s' "${kv#user=}" ;;
          dbname=*) [ "$1" = dbname ] && printf '%s' "${kv#dbname=}" ;;
        esac
      done ;;
  esac
}

PGUSER_="$(dsn_get user)"; [ -n "$PGUSER_" ] || PGUSER_="$(envget PGUSER)"; [ -n "$PGUSER_" ] || PGUSER_=sherpa
PGDB_="$(dsn_get dbname)"; [ -n "$PGDB_" ] || PGDB_="$(envget PGDATABASE)"; [ -n "$PGDB_" ] || PGDB_=sherpa

PGC=""
if have docker && tmo 20 docker info >/dev/null 2>&1; then
  first=""
  for n in $(tmo 15 docker ps --format "{{.Names}}${TAB}{{.Image}}" 2>/dev/null | awk -F'\t' '$2 ~ "^(docker.io/library/)?postgres(:|@|$)" { print $1 }'); do
    [ -n "$first" ] || first="$n"
    if tmo 15 docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$n" 2>/dev/null | grep -q '^POSTGRES_DB=sherpa$'; then PGC="$n"; break; fi
  done
  [ -n "$PGC" ] || PGC="$first"
fi

# 値は引数に出さず、子プロセスの環境変数だけで psql へ渡す
pgq_psql() {
  (
    if [ -n "$DSN" ]; then
      case "$DSN" in
        postgres://*|postgresql://*)
          rest="${DSN#*://}"; hostdb="${rest##*@}"; cred=""
          case "$rest" in *@*) cred="${rest%@*}" ;; esac
          export PGUSER="${cred%%:*}"
          case "$cred" in *:*) export PGPASSWORD="${cred#*:}" ;; esac
          hp="${hostdb%%/*}"; export PGDATABASE="${hostdb#*/}"; PGDATABASE="${PGDATABASE%%\?*}"
          export PGHOST="${hp%%:*}"; case "$hp" in *:*) export PGPORT="${hp##*:}" ;; esac ;;
        *)
          for kv in $DSN; do
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
    tmo 120 psql -X -A -t -F "$TAB" -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null
  )
}

# psql があれば DSN（無ければ既定）でつなぎ、無ければ PostgreSQL のコンテナの中の psql を使う
pgq() {
  if have psql; then pgq_psql "$1"; return $?; fi
  [ -n "$PGC" ] || return 1
  tmo 120 docker exec -i "$PGC" psql -U "$PGUSER_" -d "$PGDB_" -X -A -t -F "$TAB" -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null
}

# ---------------------------------------------------------------- 問い合わせ
# j = 仕事 1 件につき 1 行。wn=資料フォルダの番号（件数の多い順・名前は持ち出さない）、kind=対象の種類、
# px=画素数（大きさが無いものは NULL）、ikey=同じ画像の判定キー（画像のハッシュ。無ければ入力の ID）。
JCTE="with w as (select world, count(*) n from ocr_jobs group by world),
r as (select world, row_number() over (order by n desc, md5(world)) wn from w),
j as (select r.wn, o.status, o.attempts, o.error_code,
  case when o.route_input->>'input_kind' = 'page_render' then 'page'
       when o.route_input->>'reason_code' = 'metafile_embedded_bitmap' then 'meta_img'
       when o.route_input->>'reason_code' = 'metafile_rendered' then 'meta_draw'
       when o.route_input->>'input_kind' = 'asset' then 'image'
       else 'other' end as kind,
  case when (o.route_input->'pixel_size'->>0) ~ '^[0-9]+\$' and (o.route_input->'pixel_size'->>1) ~ '^[0-9]+\$'
       then (o.route_input->'pixel_size'->>0)::bigint * (o.route_input->'pixel_size'->>1)::bigint end as px,
  coalesce(o.route_input->>'asset_sha256', o.route_input_id) as ikey
  from ocr_jobs o join r on r.world = o.world)"

BAND="case when px is null then 'z' when px < 1000000 then 'a' when px < 4000000 then 'b'
  when px < 8000000 then 'c' when px < 12000000 then 'd' when px < 16000000 then 'e' else 'f' end"

reason_label() {
  case "$1" in
    oom) printf 'メモリ不足' ;;
    timeout) printf '時間切れ' ;;
    lease_expired) printf '応答なしで期限切れ' ;;
    engine_failure) printf '読み取りの失敗' ;;
    artifact_publish_failed) printf '結果の保存の失敗' ;;
    *) printf '' ;;
  esac
}

kind_label() {
  case "$1" in
    image) printf '埋め込み画像' ;;
    meta_img) printf 'WMF/EMF の中の画像' ;;
    meta_draw) printf 'WMF/EMF の図全体の描画' ;;
    page) printf 'スキャン PDF のページ' ;;
    *) printf 'その他' ;;
  esac
}

band_label() {
  case "$1" in
    a) printf '～1M画素' ;;
    b) printf '1～4M画素' ;;
    c) printf '4～8M画素' ;;
    d) printf '8～12M画素' ;;
    e) printf '12～16M画素' ;;
    f) printf '16M画素～' ;;
    *) printf '大きさ不明' ;;
  esac
}

if ! pgq "select 1" >/dev/null 2>&1; then
  echo "[OCR] 調べられません（PostgreSQL につながりません）" >&2
  exit 3
fi
if ! pgq "select count(*) from ocr_jobs" >"$TMP/probe" 2>/dev/null; then
  echo "[OCR] 調べられません（OCR の仕事の表を読めません）" >&2
  exit 3
fi
TOTAL_JOBS="$(head -n 1 "$TMP/probe")"

# ---------------------------------------------------------------- ① 状態
sec_status() {
  pgq "$JCTE select wn, status, count(*) from j group by 1, 2 order by 1, 2" >"$TMP/q_status" 2>/dev/null
  awk -F'\t' '
    { n[$1, $2] = $3; if (!($1 in seen)) { seen[$1] = 1; ws[++nw] = $1 } ; all[$2] += $3 }
    END {
      split("queued leased succeeded failed stale cancelled", st, " ")
      line = "0"; t = 0
      for (i = 1; i <= 6; i++) { line = line "\t" (all[st[i]] + 0); t += all[st[i]] }
      print line "\t" t
      for (k = 1; k <= nw; k++) {
        w = ws[k]; line = w; t = 0
        for (i = 1; i <= 6; i++) { line = line "\t" (n[w, st[i]] + 0); t += n[w, st[i]] }
        print line "\t" t
      }
    }' "$TMP/q_status" >"$TMP/t_status"
  B "[仕事の状態]"
  local hdr
  hdr="$(row '14:' '-8:待ち' '-8:実行中' '-8:成功' '-8:失敗' '-8:古い' '-8:取消' '-9:合計')"
  B "$hdr"
  local wn q l s f st c t shown=0 total_w
  total_w="$(awk 'END { print NR - 1 }' "$TMP/t_status")"
  while IFS="$TAB" read -r wn q l s f st c t; do
    if [ "$wn" = 0 ]; then
      B "$(row '14: 全体' "-8:$q" "-8:$l" "-8:$s" "-8:$f" "-8:$st" "-8:$c" "-9:$t")"
      continue
    fi
    shown=$((shown + 1))
    D "$(row "14: W$wn" "-8:$q" "-8:$l" "-8:$s" "-8:$f" "-8:$st" "-8:$c" "-9:$t")"
    if [ "$shown" -le 4 ]; then
      S "$(row "14: W$wn" "-8:$q" "-8:$l" "-8:$s" "-8:$f" "-8:$st" "-8:$c" "-9:$t")"
    elif [ "$shown" = 5 ]; then
      S " 他 $((total_w - 4)) 資料フォルダは詳細版を見てください"
    fi
  done <"$TMP/t_status"
}

# ---------------------------------------------------------------- ② 失敗の理由
sec_reasons() {
  pgq "$JCTE select coalesce(error_code, ''), count(*), round(avg(attempts), 1) from j where status = 'failed' group by 1 order by 2 desc, 1" >"$TMP/q_reason" 2>/dev/null
  B ""
  if [ ! -s "$TMP/q_reason" ]; then B "[失敗の理由] 失敗した仕事はありません"; return 0; fi
  B "$(row '30:[失敗の理由]' '-8:件数' '-10:平均試行')"
  local code n avg label shown=0
  while IFS="$TAB" read -r code n avg; do
    # 理由は仕組みが付ける短い英小文字の語だけ表示する（それ以外は中身を出さずその他にまとめる）
    case "$code" in
      "") label="(理由なし)"; code="(none)" ;;
      *[!a-z0-9_]*) code="other"; label="" ;;
      *) label="$(reason_label "$code")" ;;
    esac
    line="$(row "16: $code" "22:$label" "-8:$n" "-10:$avg")"
    D "$line"
    shown=$((shown + 1))
    if [ "$shown" -le 6 ]; then S "$line"; elif [ "$shown" = 7 ]; then S " 他の理由は詳細版を見てください"; fi
  done <"$TMP/q_reason"
}

# ---------------------------------------------------------------- ③ 対象の種類
sec_kinds() {
  pgq "$JCTE select kind, count(*), count(*) filter (where status = 'succeeded'), count(*) filter (where status = 'failed'),
    count(*) filter (where status in ('queued', 'leased')) from j group by 1" >"$TMP/q_kind" 2>/dev/null
  B ""
  B "$(row '30:[対象の種類]' '-8:件数' '-8:成功' '-8:失敗' '-8:未処理')"
  local k lbl n s f p
  for k in image meta_img meta_draw page other; do
    n=0; s=0; f=0; p=0
    while IFS="$TAB" read -r kk nn ss ff pp; do
      if [ "$kk" = "$k" ]; then n="$nn"; s="$ss"; f="$ff"; p="$pp"; fi
    done <"$TMP/q_kind"
    if [ "$k" = other ] && [ "$n" = 0 ]; then continue; fi
    B "$(row "30: $(kind_label "$k")" "-8:$n" "-8:$s" "-8:$f" "-8:$p")"
  done
  S " （未処理 = 待ち + 実行中）"
}

# ---------------------------------------------------------------- ④ 画像の大きさの帯
sec_bands() {
  pgq "$JCTE select $BAND as band, count(*), count(*) filter (where status = 'succeeded'),
    count(*) filter (where status = 'failed'), count(*) filter (where status = 'failed' and error_code = 'oom'),
    count(*) filter (where status in ('queued', 'leased')) from j group by 1 order by 1" >"$TMP/q_band" 2>/dev/null
  B ""
  B "$(row '30:[画像の大きさ]' '-8:件数' '-8:成功' '-8:失敗' '-16:うちメモリ不足' '-8:未処理')"
  local b n s f o p
  while IFS="$TAB" read -r b n s f o p; do
    B "$(row "30: $(band_label "$b")" "-8:$n" "-8:$s" "-8:$f" "-16:$o" "-8:$p")"
  done <"$TMP/q_band"
  S " （画素数 = 幅 × 高さ。ページの描画は大きさを持たないため「大きさ不明」）"
}

# ---------------------------------------------------------------- ⑤ 同じ画像（同じハッシュ）を除いた件数
sec_dups() {
  pgq "$JCTE select 'all', count(*), count(distinct ikey) from j
    union all select 'open', count(*), count(distinct ikey) from j where status in ('queued', 'leased', 'failed')
    union all select 'failed', count(*), count(distinct ikey) from j where status = 'failed'
    union all select 'solved', count(*), count(distinct ikey) from j where status = 'failed'
      and ikey in (select ikey from j where status = 'succeeded')" >"$TMP/q_dup" 2>/dev/null
  B ""
  B "$(row '30:[同じ画像を除く]' '-8:仕事' '-12:画像の種類' '-8:重なり')"
  local k lbl n d
  while IFS="$TAB" read -r k n d; do
    case "$k" in
      all) lbl="全体" ;;
      open) lbl="未完了（待ち・実行中・失敗）" ;;
      failed) lbl="失敗" ;;
      solved) lbl="失敗のうち同じ画像が成功済み" ;;
      *) continue ;;
    esac
    B "$(row "30: $lbl" "-8:$n" "-12:$d" "-8:$((n - d))")"
  done <"$TMP/q_dup"
}

# ---------------------------------------------------------------- ⑥ 詳細だけに載せる表
sec_detail() {
  D ""
  D "[資料フォルダ × 失敗の理由]"
  pgq "$JCTE select wn, coalesce(error_code, ''), count(*) from j where status = 'failed' group by 1, 2 order by 1, 3 desc, 2" >"$TMP/q_wr" 2>/dev/null
  local wn code n
  while IFS="$TAB" read -r wn code n; do
    case "$code" in
      "") code="(none)" ;;
      *[!a-z0-9_]*) code="other" ;;
    esac
    D "$(row "6: W$wn" "20:$code" "-9:$n")"
  done <"$TMP/q_wr"
  D ""
  D "[資料フォルダ × 対象の種類]"
  pgq "$JCTE select wn, kind, count(*) from j group by 1, 2 order by 1, 2" >"$TMP/q_wk" 2>/dev/null
  while IFS="$TAB" read -r wn code n; do
    D "$(row "6: W$wn" "26:$(kind_label "$code")" "-9:$n")"
  done <"$TMP/q_wk"
  D ""
  D "[対象の種類 × 画像の大きさ × 状態]"
  pgq "$JCTE select kind, $BAND as band, status, count(*) from j group by 1, 2, 3 order by 1, 2, 3" >"$TMP/q_kb" 2>/dev/null
  local kk bb st
  while IFS="$TAB" read -r kk bb st n; do
    D "$(row "26: $(kind_label "$kk")" "14:$(band_label "$bb")" "10:$st" "-9:$n")"
  done <"$TMP/q_kb"
}

# ---------------------------------------------------------------- 実行
if [ "${TOTAL_JOBS:-0}" = 0 ]; then
  B "[OCR] 仕事はありません"
else
  sec_status
  sec_reasons
  sec_kinds
  sec_bands
  sec_dups
  sec_detail
fi

# ---------------------------------------------------------------- 保存して画面に出す
SUMF="$OUTDIR/ocr-report-$NOW_F-summary.txt"
DETF="$OUTDIR/ocr-report-$NOW_F-detail.txt"
case "$OUTDIR" in
  "$ROOT/data/run/ocr-report") DETF_SHOWN="data/run/ocr-report/ocr-report-$NOW_F-detail.txt" ;;
  /tmp/ocr-report) DETF_SHOWN="/tmp/ocr-report/ocr-report-$NOW_F-detail.txt" ;;
  *) DETF_SHOWN="(--out で指定した場所)/ocr-report-$NOW_F-detail.txt" ;;
esac
{
  printf '%s\n' "Sherpa OCR レポート $NOW_H  版 $VERSION_TXT"
  cat "$TMP/sum"
  printf '%s\n' "詳細: $DETF_SHOWN"
} >"$TMP/sum.final"
{
  printf '%s\n' "Sherpa OCR レポート（詳細）$NOW_H  版 $VERSION_TXT"
  echo
  cat "$TMP/det"
} >"$TMP/det.final"
cp "$TMP/sum.final" "$SUMF" 2>/dev/null && cp "$TMP/det.final" "$DETF" 2>/dev/null
SAVED=$?
# 古いレポートは 10 本（要約と詳細の組）まで残す
ls -1 "$OUTDIR" 2>/dev/null | grep -E '^ocr-report-[0-9]{8}-[0-9]{6}-detail\.txt$' | sort -r | awk 'NR > 10' | while IFS= read -r f; do
  rm -f "$OUTDIR/$f" "$OUTDIR/${f%-detail.txt}-summary.txt" 2>/dev/null || : >"$TMP/prune_failed"
done

cat "$TMP/sum.final"
[ "$SAVED" = 0 ] || echo "保存できませんでした（保存先に書けません）"
[ ! -e "$TMP/prune_failed" ] || echo "古いレポートを消せませんでした（保存先の権限を確認してください）"
exit 0
