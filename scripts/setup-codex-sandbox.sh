#!/usr/bin/env bash
# Codex のサンドボックス（permission profile＝bubblewrap）が Ubuntu 23.10 以降で起動できない問題
# （AppArmor のユーザー名前空間制限 kernel.apparmor_restrict_unprivileged_userns=1）を診断・是正する。
#
# 使い方:
#   ./scripts/setup-codex-sandbox.sh check   # 既定・確認のみ（root 不要・何も変更しない）
#   sudo bash scripts/setup-codex-sandbox.sh apply   # 制限を緩和する（root 必須・OS の設定を変更）
#
# 実機の事故（何か月もサンドボックス内のコマンドが全部失敗していたのに気付けなかった）を踏まえ、
# `make doctor` の「Codex のサンドボックス」検査（scripts/doctor_checks.py::check_codex_sandbox）と
# 対になる——このスクリプトは OS 側の前提条件、doctor は Codex 自身が実際にコマンドを実行できるかを見る。
set -euo pipefail

_SYSCTL_KEY="kernel.apparmor_restrict_unprivileged_userns"
_SYSCTL_KEY_RE='kernel\.apparmor_restrict_unprivileged_userns'
_CONF_PATH="/etc/sysctl.d/99-sherpa-codex-userns.conf"

_os_pretty() {
  if [ -r /etc/os-release ]; then
    (
      # shellcheck source=/dev/null
      . /etc/os-release
      echo "${PRETTY_NAME:-不明}"
    )
  else
    echo "不明（/etc/os-release が見つかりません）"
  fi
}

# 0=有効・1=無効またはこのカーネルに無し。
_apparmor_enabled() {
  if [ -r /sys/module/apparmor/parameters/enabled ]; then
    [ "$(cat /sys/module/apparmor/parameters/enabled)" = "Y" ]
  else
    return 1
  fi
}

# キーが無ければ空文字列を出す（エラーにしない）。
_sysctl_value() {
  sysctl -n "$_SYSCTL_KEY" 2>/dev/null || true
}

# /etc/sysctl.d → /run/sysctl.d → /usr/lib/sysctl.d の順で、同名ファイルは高優先ディレクトリが
# 勝つ（systemd-sysctl の規則）。生き残ったファイルのうちこのキーを持つものを basename 順に見て、
# 最後（＝再起動後に実際に効く値を書く）ファイルのパスを出す。見つからなければ何も出さない。
_winning_conf_file() {
  local dir f bn last=""
  local -A seen=()
  local -a survivors=()
  for dir in /etc/sysctl.d /run/sysctl.d /usr/lib/sysctl.d; do
    [ -d "$dir" ] || continue
    for f in "$dir"/*.conf; do
      [ -f "$f" ] || continue
      bn="$(basename "$f")"
      [ -n "${seen[$bn]:-}" ] && continue
      seen[$bn]=1
      if grep -Eq "^[[:space:]]*${_SYSCTL_KEY_RE}[[:space:]]*=" "$f" 2>/dev/null; then
        survivors+=("$bn|$f")
      fi
    done
  done
  [ "${#survivors[@]}" -eq 0 ] && return 0
  while IFS='|' read -r _ path; do
    last="$path"
  done < <(printf '%s\n' "${survivors[@]}" | sort)
  printf '%s\n' "$last"
}

# 名前空間の試験を行うユーザー名（アプリを動かすユーザー＝sudo 実行時は SUDO_USER）。
_ns_test_user() {
  if [ "$(id -u)" = "0" ] && [ -n "${SUDO_USER:-}" ]; then
    echo "$SUDO_USER"
  else
    id -un
  fi
}

_ns_test() {
  local user
  user="$(_ns_test_user)"
  if [ "$user" = "$(id -un)" ]; then
    unshare -Urn true >/dev/null 2>&1
  else
    sudo -u "$user" unshare -Urn true >/dev/null 2>&1
  fi
}

cmd_check() {
  local value wf test_user ns_ok=1

  echo "OS: $(_os_pretty)"
  echo "カーネル: $(uname -r)"
  if _apparmor_enabled; then
    echo "AppArmor: 有効"
  else
    echo "AppArmor: 無効（またはこのカーネルに無し）"
  fi

  value="$(_sysctl_value)"
  if [ -z "$value" ]; then
    echo "${_SYSCTL_KEY}: この制限はありません（設定は不要）"
  else
    echo "${_SYSCTL_KEY}: ${value}"
    wf="$(_winning_conf_file)"
    if [ -n "$wf" ]; then
      echo "再起動後の値を決めるファイル: $wf"
    else
      echo "再起動後の値を決めるファイル: 見つかりません（カーネル既定値の可能性があります）"
    fi
  fi

  test_user="$(_ns_test_user)"
  if _ns_test; then
    echo "名前空間の試験（${test_user}・unshare -Urn true）: 成功"
    ns_ok=1
  else
    echo "名前空間の試験（${test_user}・unshare -Urn true）: 失敗"
    ns_ok=0
  fi

  echo
  if [ -n "$value" ] && [ "$value" = "1" ] && [ "$ns_ok" = "0" ]; then
    echo "判定: このままでは Codex のサンドボックスを起動できません。"
    echo "      sudo bash scripts/setup-codex-sandbox.sh apply で直せます。"
  elif [ "$ns_ok" = "0" ]; then
    echo "判定: 名前空間の作成に失敗していますが、原因はこのスクリプトの対象"
    echo "      （AppArmor のユーザー名前空間制限）以外の可能性があります。"
  else
    echo "判定: 問題は見つかりませんでした。"
  fi
  [ "$ns_ok" = "1" ] || exit 1
}

cmd_apply() {
  if [ "$(id -u)" != "0" ]; then
    echo "sudo bash scripts/setup-codex-sandbox.sh apply で実行してください" >&2
    exit 2
  fi
  if ! _apparmor_enabled || [ -z "$(_sysctl_value)" ]; then
    echo "不要です（この OS には AppArmor のユーザー名前空間制限がありません）"
    exit 0
  fi

  mkdir -p "$(dirname "$_CONF_PATH")"
  {
    echo "# Sherpa: Codex のサンドボックス（permission profile＝bubblewrap）が Ubuntu 23.10 以降の"
    echo "# AppArmor ユーザー名前空間制限で起動できない問題への対処。"
    echo "# 生成: scripts/setup-codex-sandbox.sh apply"
    echo "${_SYSCTL_KEY}=0"
  } > "$_CONF_PATH"
  echo "書き込みました: $_CONF_PATH"
  sysctl -p "$_CONF_PATH"

  local wf
  wf="$(_winning_conf_file)"
  if [ "$wf" != "$_CONF_PATH" ]; then
    echo "再起動後は ${wf} の値が効くため、この設定は再起動で元に戻ります。" >&2
    echo "${wf} の ${_SYSCTL_KEY} を 0 に直すか削除してください。" >&2
    exit 1
  fi

  local test_user
  test_user="$(_ns_test_user)"
  if _ns_test; then
    echo "名前空間の試験（${test_user}・unshare -Urn true）: 成功"
  else
    echo "名前空間の試験（${test_user}・unshare -Urn true）: 失敗（別の原因が残っている可能性があります）"
    exit 1
  fi
  echo "確認: make doctor の「Codex のサンドボックス」が OK になることを確かめてください。"
}

action="${1:-check}"
case "$action" in
  check) cmd_check ;;
  apply) cmd_apply ;;
  *)
    echo "使い方: $0 [check|apply]" >&2
    exit 2
    ;;
esac
