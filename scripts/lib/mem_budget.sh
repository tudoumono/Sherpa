#!/usr/bin/env bash
# メモリの割り振り: ホストの全体のメモリ M から、Elasticsearch・Neo4j・OCR ワーカーの上限を決める（1 か所）。
# 設計: docs/design/operations.md「メモリの割り振り」。bash 3.2（macOS）と bash 5（Linux）の両方で動く。
#
# 使い方:
#   source して mem_budget_apply を呼ぶ（run-common.sh の sherpa_compose と start.sh が使う）。
#   直接実行: scripts/lib/mem_budget.sh [--total-mb N] [--env | --line | --json]
#     --total-mb N  全体を N MB として計算する（検出しない）。--env は export 行・--line は 1 行・--json は JSON。
#
# 規則（単位 MB・整数の切り捨て）:
#   OS と余裕    = max(1536, M の 10%)
#   ES ヒープ    = clamp(M の 5%, 512, 4096)   ES 上限（コンテナ）= ヒープ×2 + 512
#   Neo4j ヒープ = clamp(M の 8%, 512, 4096)   Neo4j ページキャッシュ = clamp(M の 4%, 256, 2048)
#     SHERPA_NEO4J_HEAP_FIXED／SHERPA_NEO4J_PAGECACHE_FIXED（4g・4096m の形・.env）があればその値（自動との大小は問わない）。
#     LibreOffice・OCR は自動の値を引いて計算し、増やした分は「残り」から出る。
#   Neo4j 1 回の書き込み上限（dbms.memory.transaction.total.max）= Neo4j ヒープの 70%（Neo4j の既定と同じ）。
#     SHERPA_NEO4J_TX_MEMORY_MAX（10m 以上の 4g・2048m の形、または 0＝無制限・.env）があればその値。
#   LibreOffice  = clamp(M の 15%, 3072, 4096)（soffice 1 本の上限。残りが足りなければ減らす）
#   OCR 上限     = min(clamp(M の 1/4, 4096, 8192), 他と LibreOffice を引いた残り)（SHERPA_OCR_MEMORY_LIMIT は上限として扱い、自動の値より小さいときだけそれを使う）
#   残り         = M − OS − ES 上限 − Neo4j(ヒープ+キャッシュ) − OCR（Web・取り込みワーカー・LibreOffice・PostgreSQL 用の目安）
# M は ホストの全体と Docker の全体（docker info の MemTotal）の小さい方。

_mb_clamp() {  # $1=値 $2=下限 $3=上限
  local v="$1"
  [ "$v" -lt "$2" ] && v="$2"
  [ "$v" -gt "$3" ] && v="$3"
  echo "$v"
}

# 単位付きの値（4g・4096m・4096）を MB にする。読めなければ空。
_mb_parse() {
  local s
  s="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$s" in
    *[!0-9gmb]*|"") return 0 ;;
  esac
  s="${s%b}"
  case "$s" in
    *g) s="${s%g}"; case "$s" in ""|*[!0-9]*) return 0 ;; esac; echo $((s * 1024)) ;;
    *m) s="${s%m}"; case "$s" in ""|*[!0-9]*) return 0 ;; esac; echo "$s" ;;
    *[!0-9]*|"") return 0 ;;
    *) echo $((s / 1048576)) ;;
  esac
}

# ホストの全体のメモリ（MB）。取れなければ空。
mem_budget_host_mb() {
  local kb bytes
  if [ -r /proc/meminfo ]; then
    kb="$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null || true)"
    [ -n "$kb" ] && { echo $((kb / 1024)); return 0; }
  fi
  if command -v sysctl >/dev/null 2>&1; then
    bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
    case "$bytes" in ""|*[!0-9]*) ;; *) echo $((bytes / 1048576)); return 0 ;; esac
  fi
  return 0
}

# Docker の全体のメモリ（MB）。macOS / Windows の Docker は VM の上限なのでホストより小さいことがある。
mem_budget_docker_mb() {
  local bytes
  command -v docker >/dev/null 2>&1 || return 0
  if command -v timeout >/dev/null 2>&1; then
    bytes="$(timeout 20 docker info --format '{{.MemTotal}}' 2>/dev/null || true)"
  else
    bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || true)"
  fi
  case "$bytes" in ""|*[!0-9]*) return 0 ;; esac
  echo $((bytes / 1048576))
}

# .env から取り込む設定（未設定のときだけ）。呼び出し側は sherpa_env_default を個別に呼ばずこれを使う。
mem_budget_load_env() {
  sherpa_env_default SHERPA_OCR_MEMORY_LIMIT SHERPA_NEO4J_HEAP_FIXED SHERPA_NEO4J_PAGECACHE_FIXED SHERPA_NEO4J_TX_MEMORY_MAX
}

# 設定値 $2 を MB にして、$3 MB 以上のときだけ MB_PARSED に入れて 0 を返す。読めない・小さすぎるときは 1 を返し、MB_NEO_NOTE に変数名 $1 を足す。
_mb_setting() {  # $1=変数名 $2=値 $3=下限 MB
  local parsed
  MB_PARSED=""
  parsed="$(_mb_parse "$2")"
  if [ -n "$parsed" ] && [ "$parsed" -ge "$3" ]; then
    MB_PARSED="$parsed"
    return 0
  fi
  MB_NEO_NOTE="${MB_NEO_NOTE:+${MB_NEO_NOTE}・}$1"
  return 1
}

# 計算の結果を MB_* に入れる。$1=全体 MB。SHERPA_OCR_MEMORY_LIMIT は上限: 読める値で、自動の値より小さいときだけ OCR にそれを使う（MB_OCR_OVERRIDDEN=1）。
mem_budget_compute() {
  local m="$1" ocr_default avail user
  MB_TOTAL="$m"
  MB_OS=$((m / 10)); [ "$MB_OS" -lt 1536 ] && MB_OS=1536
  MB_ES_HEAP="$(_mb_clamp $((m * 5 / 100)) 512 4096)"
  MB_ES_LIMIT=$((MB_ES_HEAP * 2 + 512))
  MB_NEO_HEAP="$(_mb_clamp $((m * 8 / 100)) 512 4096)"
  MB_NEO_PC="$(_mb_clamp $((m * 4 / 100)) 256 2048)"
  avail=$((m - MB_OS - MB_ES_LIMIT - MB_NEO_HEAP - MB_NEO_PC))
  MB_LO="$(_mb_clamp $((m * 15 / 100)) 3072 4096)"
  [ "$MB_LO" -gt $((avail - 256)) ] && MB_LO=$((avail - 256))
  [ "$MB_LO" -lt 0 ] && MB_LO=0
  ocr_default="$(_mb_clamp $((m / 4)) 4096 8192)"
  [ "$ocr_default" -gt $((avail - MB_LO)) ] && ocr_default=$((avail - MB_LO))
  [ "$ocr_default" -lt 256 ] && ocr_default=256
  MB_OCR_OVERRIDDEN=0
  MB_OCR="$ocr_default"
  user="${SHERPA_OCR_MEMORY_LIMIT:-}"
  if [ -n "$user" ]; then
    local parsed
    parsed="$(_mb_parse "$user")"
    if [ -n "$parsed" ] && [ "$parsed" -lt "$ocr_default" ]; then
      MB_OCR_OVERRIDDEN=1
      MB_OCR="$parsed"
    fi
  fi
  # Neo4j の設定値は LibreOffice・OCR を決めた後に反映する（他の部品の割り振りは自動のまま。足りない分は残りの警告で知らせる）
  MB_NEO_NOTE=""; MB_NEO_HEAP_FIXED=0; MB_NEO_PC_FIXED=0; MB_NEO_TX_FIXED=0
  if [ -n "${SHERPA_NEO4J_HEAP_FIXED:-}" ] && _mb_setting SHERPA_NEO4J_HEAP_FIXED "$SHERPA_NEO4J_HEAP_FIXED" 512; then
    MB_NEO_HEAP="$MB_PARSED"; MB_NEO_HEAP_FIXED=1
  fi
  if [ -n "${SHERPA_NEO4J_PAGECACHE_FIXED:-}" ] && _mb_setting SHERPA_NEO4J_PAGECACHE_FIXED "$SHERPA_NEO4J_PAGECACHE_FIXED" 256; then
    MB_NEO_PC="$MB_PARSED"; MB_NEO_PC_FIXED=1
  fi
  MB_NEO_TX=$((MB_NEO_HEAP * 70 / 100))
  if [ "${SHERPA_NEO4J_TX_MEMORY_MAX:-}" = "0" ]; then
    MB_NEO_TX=0; MB_NEO_TX_FIXED=1
  elif [ -n "${SHERPA_NEO4J_TX_MEMORY_MAX:-}" ] && _mb_setting SHERPA_NEO4J_TX_MEMORY_MAX "$SHERPA_NEO4J_TX_MEMORY_MAX" 10; then
    MB_NEO_TX="$MB_PARSED"; MB_NEO_TX_FIXED=1
  fi
  MB_REST=$((m - MB_OS - MB_ES_LIMIT - MB_NEO_HEAP - MB_NEO_PC - MB_LO - MB_OCR))
  [ "$MB_REST" -lt 0 ] && MB_REST=0
  return 0
}

# 全体のメモリ（MB）を決める: ホストと Docker の小さい方。どちらも取れなければ 8192。
# MB_HOST / MB_DOCKER / MB_TOTAL_SOURCE も入れる。
mem_budget_detect() {
  MB_HOST="$(mem_budget_host_mb)"
  MB_DOCKER="$(mem_budget_docker_mb)"
  MB_TOTAL_SOURCE=host
  local t="${MB_HOST:-}"
  if [ -n "$MB_DOCKER" ] && { [ -z "$t" ] || [ "$MB_DOCKER" -lt "$t" ]; }; then
    t="$MB_DOCKER"; MB_TOTAL_SOURCE=docker
  fi
  if [ -z "$t" ]; then t=8192; MB_TOTAL_SOURCE=default; fi
  MB_TOTAL_DETECTED="$t"
}

mem_budget_line() {
  awk -v t="$MB_TOTAL" -v os="$MB_OS" -v esh="$MB_ES_HEAP" -v esl="$MB_ES_LIMIT" \
      -v nh="$MB_NEO_HEAP" -v np="$MB_NEO_PC" -v tx="$MB_NEO_TX" -v nf="$MB_NEO_HEAP_FIXED$MB_NEO_PC_FIXED$MB_NEO_TX_FIXED" -v note="$MB_NEO_NOTE" \
      -v lo="$MB_LO" -v ocr="$MB_OCR" -v rest="$MB_REST" -v ov="$MB_OCR_OVERRIDDEN" 'BEGIN{
    fx = (nf != "000" ? "（設定値あり）" : "")
    ig = (note != "" ? "。読めないため無視した設定: " note "（4g・4096m のように単位を付けてください）" : "")
    txs = (tx == 0 ? "無制限" : sprintf("%.1f", tx/1024))
    printf "メモリの割り振り: 全体 %.1fGB・OS %.1f・ES ヒープ %.2f（上限 %.2f）・Neo4j ヒープ %.1f＋キャッシュ %.1f・書き込み上限 %s%s・LibreOffice %.1f・OCR 上限 %.1f%s・残り %.1fGB（Web・取り込みワーカー・LibreOffice・PostgreSQL の目安）%s%s\n",
      t/1024, os/1024, esh/1024, esl/1024, nh/1024, np/1024, txs, fx, lo/1024, ocr/1024, (ov ? "（設定値で絞った）" : ""), rest/1024, (rest < 4096 ? "。残りが少ないため、メモリを増やすか OCR を止めてください" : ""), ig }'
}

mem_budget_json() {
  printf '{"total_mb":%s,"host_mb":%s,"docker_mb":%s,"total_source":"%s","os_mb":%s,"es_heap_mb":%s,"es_limit_mb":%s,"neo4j_heap_mb":%s,"neo4j_pagecache_mb":%s,"neo4j_tx_memory_mb":%s,"libreoffice_mb":%s,"libreoffice_bytes":%s,"ocr_limit_mb":%s,"ocr_overridden":%s,"rest_mb":%s}\n' \
    "$MB_TOTAL" "${MB_HOST:-null}" "${MB_DOCKER:-null}" "${MB_TOTAL_SOURCE:-host}" "$MB_OS" "$MB_ES_HEAP" "$MB_ES_LIMIT" \
    "$MB_NEO_HEAP" "$MB_NEO_PC" "$MB_NEO_TX" "$MB_LO" "$((MB_LO * 1048576))" "$MB_OCR" "$([ "$MB_OCR_OVERRIDDEN" = 1 ] && echo true || echo false)" "$MB_REST"
}

# compose が受ける変数。OCR は設定値で絞っていないとき計算値を入れる（古い .env の大きい値で残りを食わせない）。
mem_budget_env() {
  echo "export SHERPA_ES_HEAP_RESOLVED=${MB_ES_HEAP}m"
  echo "export SHERPA_ES_MEM_LIMIT_RESOLVED=${MB_ES_LIMIT}m"
  echo "export SHERPA_NEO4J_HEAP_RESOLVED=${MB_NEO_HEAP}m"
  echo "export SHERPA_NEO4J_PAGECACHE_RESOLVED=${MB_NEO_PC}m"
  echo "export SHERPA_NEO4J_TX_MEMORY_RESOLVED=$([ "$MB_NEO_TX" = 0 ] && echo 0 || echo "${MB_NEO_TX}m")"
  [ "$MB_OCR_OVERRIDDEN" = 1 ] || echo "export SHERPA_OCR_MEMORY_LIMIT=${MB_OCR}m"
}

# 割り振り済み（SHERPA_NEO4J_HEAP_RESOLVED がある）で 1 回の書き込みの上限だけが無いとき、明示の値かヒープの 7 割を export する。
# 守ること: 上限を compose の既定値（小さい固定値）に落とさない。
mem_budget_fill_tx_from_resolved_heap() {
  local heap
  [ -n "${SHERPA_NEO4J_TX_MEMORY_RESOLVED:-}" ] && return 0
  if [ "${SHERPA_NEO4J_TX_MEMORY_MAX:-}" = "0" ]; then
    export SHERPA_NEO4J_TX_MEMORY_RESOLVED=0
    return 0
  fi
  if [ -n "${SHERPA_NEO4J_TX_MEMORY_MAX:-}" ] && _mb_setting SHERPA_NEO4J_TX_MEMORY_MAX "$SHERPA_NEO4J_TX_MEMORY_MAX" 10; then
    export SHERPA_NEO4J_TX_MEMORY_RESOLVED="${MB_PARSED}m"
    return 0
  fi
  heap="$(_mb_parse "$SHERPA_NEO4J_HEAP_RESOLVED")"
  [ -n "$heap" ] && export SHERPA_NEO4J_TX_MEMORY_RESOLVED="$((heap * 70 / 100))m"
  return 0
}

# 検出 → 計算 → compose 用の変数を export する。既に割り振り済み（変数がある）なら何もしない。
mem_budget_apply() {  # $1=全体 MB（省略時は検出）
  if [ -n "${SHERPA_NEO4J_HEAP_RESOLVED:-}" ]; then
    mem_budget_fill_tx_from_resolved_heap
    return 0
  fi
  if [ -n "${1:-}" ]; then
    MB_HOST=""; MB_DOCKER=""; MB_TOTAL_SOURCE=given; MB_TOTAL_DETECTED="$1"
  else
    mem_budget_detect
  fi
  mem_budget_compute "$MB_TOTAL_DETECTED"
  eval "$(mem_budget_env)"
}

# 結果を RUN_DIR などの JSON に書く（取り込みワーカーなど他の部品が同じ規則を読む）。
mem_budget_write_json() {  # $1=書き出し先
  local dest="$1" tmp
  mkdir -p "$(dirname "$dest")"
  tmp="$dest.tmp.$$"
  mem_budget_json > "$tmp" && mv -f "$tmp" "$dest"
}

if [ -z "${_MB_CLI:-}" ] && [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  _MB_CLI=1
  mode=line total=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --total-mb) total="${2:?--total-mb に MB を指定してください}"; shift 2 ;;
      --env|--line|--json) mode="${1#--}"; shift ;;
      *) echo "usage: $0 [--total-mb N] [--env|--line|--json]" >&2; exit 2 ;;
    esac
  done
  ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
  # shellcheck source=scripts/run-common.sh
  . "$ROOT/scripts/run-common.sh"
  mem_budget_load_env
  if [ -n "$total" ]; then
    MB_HOST=""; MB_DOCKER=""; MB_TOTAL_SOURCE=given
  else
    mem_budget_detect; total="$MB_TOTAL_DETECTED"
  fi
  mem_budget_compute "$total"
  case "$mode" in
    env) mem_budget_env ;;
    json) mem_budget_json ;;
    *) mem_budget_line ;;
  esac
fi
