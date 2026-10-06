#!/usr/bin/env bash
# インストールの記録・印・起動の拒否（source 専用・bash 3.2 で動く書き方）。
# 使う側: install.sh（記録の更新・印）／scripts/start.sh・scripts/run-api.sh（起動前の拒否）／scripts/stop.sh（systemd）。
# 設計: docs/proposals/2026-10-05-パッケージと導入・更新の一本化.md §4.4。
#
# data/.installing          インストール中の印（./install.sh が成功したときだけ消す）
# data/.installed           前回の成功したインストールの記録（版・コミット・種類・依存の指紋）
# data/.installed.prev      その前の記録（前の .venv／tools の組を戻すときの照合用）
# data/.installed-manifest  そのとき入れたアプリのファイル一覧（ハッシュ付き・古いファイルの片付けに使う）
#
# 呼び出し側が ROOT（アプリのフォルダ）を決めてから読む。

PKG_INFO_NAME="PACKAGE-INFO"
PKG_MANIFEST_NAME="PACKAGE-MANIFEST.sha256"
# ./install.sh が入れ替える実行物（ROOT からの相対パス）。
PKG_SWAP_ITEMS=".venv tools/node tools/codex tools/marp/node_modules"

pkg_data_dir()       { printf '%s\n' "$ROOT/data"; }
pkg_installing_file() { printf '%s\n' "$ROOT/data/.installing"; }
pkg_record_file()    { printf '%s\n' "$ROOT/data/.installed"; }
pkg_record_prev_file() { printf '%s\n' "$ROOT/data/.installed.prev"; }
pkg_manifest_record() { printf '%s\n' "$ROOT/data/.installed-manifest"; }

# KEY=VALUE 形式のファイルから最初の KEY の値を返す（無ければ空）。
pkg_kv_get() {  # file key
  [ -f "$1" ] || return 0
  grep -m1 "^$2=" "$1" 2>/dev/null | cut -d= -f2- || true
}

# 標準入力の sha256（hex のみ）。sha256sum → shasum → python3 の順。
pkg_sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -d' ' -f1
  else
    "${PYTHON_BIN:-python3}" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
  fi
}

# requirements.txt と constraints.txt の連結ハッシュ（scripts/lib/req_hash.sh・pkg_tool.py と同じ式）。
pkg_requirements_hash() {
  cat "$ROOT/requirements.txt" "$ROOT/constraints.txt" | pkg_sha256_stdin
}

pkg_install_recorded() { [ -f "$(pkg_record_file)" ]; }

# 起動してよい状態かを確かめる。だめなら理由と直し方を標準エラーへ出して 1 を返す。
# ①インストール中の印 ②展開したが ./install.sh が終わっていない（メタデータと記録の版・コミットの不一致）
# ③依存の記録との不一致（requirements／constraints）。開発用のチェックアウト（メタデータも記録も無い）は何もしない。
pkg_install_guard() {
  local mark info record meta_v meta_c rec_v rec_c rec_req now_req
  mark="$(pkg_installing_file)"
  info="$ROOT/$PKG_INFO_NAME"
  record="$(pkg_record_file)"
  if [ -e "$mark" ]; then
    cat >&2 <<EOF
✗ インストールが途中のため、起動できません（印: ${mark#"$ROOT"/}）。
  ./install.sh が最後まで終わっていません。ログを確認し、原因を直してから ./install.sh をやり直してください。
  アプリだけのパッケージで「フルが必要」と止まったときは、同じ版のフルのパッケージを入れ直すか、
  前の版の圧縮ファイルを展開し直して ./install.sh してください（データには触れません）。
EOF
    return 1
  fi
  if [ -f "$info" ]; then
    meta_v="$(pkg_kv_get "$info" version)"
    meta_c="$(pkg_kv_get "$info" commit)"
    rec_v="$(pkg_kv_get "$record" version)"
    rec_c="$(pkg_kv_get "$record" commit)"
    if [ ! -f "$record" ] || [ "$meta_v" != "$rec_v" ] || [ "$meta_c" != "$rec_c" ]; then
      cat >&2 <<EOF
✗ 展開したパッケージ（版 ${meta_v}・コミット ${meta_c}）が、まだインストールされていないため起動できません。
  前回インストールした版: ${rec_v:-なし}（コミット ${rec_c:-なし}）
  展開のあとは ./install.sh を実行してください（make start・systemctl start・再起動は、それまで断られます）。
EOF
      return 1
    fi
  fi
  if [ -f "$record" ] && [ -f "$ROOT/requirements.txt" ] && [ -f "$ROOT/constraints.txt" ]; then
    rec_req="$(pkg_kv_get "$record" fp.requirements)"
    if [ -n "$rec_req" ]; then
      now_req="$(pkg_requirements_hash)"
      if [ "$rec_req" != "$now_req" ]; then
        cat >&2 <<EOF
✗ 依存の記録（requirements／constraints）と今のファイルが違うため、起動できません。
  起動は依存を自分では入れません。フルのパッケージで ./install.sh を実行してください。
EOF
        return 1
      fi
    fi
  fi
  return 0
}

# このアプリのフォルダを WorkingDirectory にしている、動作中の systemd ユニット名を 1 行ずつ出す。
pkg_systemd_active_units() {
  command -v systemctl >/dev/null 2>&1 || return 0
  # systemd が動いていない環境（WSL など）は、対象ユニットなしとして続ける。
  [ -d "${SHERPA_SYSTEMD_RUN_DIR:-/run/systemd/system}" ] || return 0
  local unit wd listing
  if ! listing="$(systemctl list-units --type=service --state=active --no-legend --plain 'sherpa*' 2>&1)"; then
    echo "✗ systemctl の照会に失敗しました: ${listing}" >&2
    return 1
  fi
  printf '%s\n' "$listing" | awk '{print $1}' | while IFS= read -r unit; do
    [ -n "$unit" ] || continue
    wd="$(systemctl show -p WorkingDirectory --value "$unit" 2>/dev/null || true)"
    if [ "$wd" = "$ROOT" ]; then printf '%s\n' "$unit"; fi
  done
}
