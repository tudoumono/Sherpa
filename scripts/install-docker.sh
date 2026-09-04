#!/usr/bin/env bash
# Docker Engine を Ubuntu 24.04 / WSL2(systemd) に入れる。
# ※ sudo パスワードを1回聞かれます（非対話の自動実行はできないため手動）。
set -euo pipefail

echo "== Docker Engine install (Ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") / WSL2) =="
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# WSL2 は systemd が pid1 のため、サービスとして有効化できる
sudo systemctl enable --now docker
# sudo 無しで docker を使えるように（反映には再ログイン or `newgrp docker`）
sudo usermod -aG docker "$USER"

echo
echo "OK. 反映するには新しいシェルを開く（または: newgrp docker）。"
echo "確認: docker run --rm hello-world"
echo "次:   make up && make graph   （Sherpa のストア起動＋統合グラフ投入＋検証）"
