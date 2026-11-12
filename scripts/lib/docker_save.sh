# shellcheck shell=bash
# イメージを tar に書き出す。Docker が containerd でイメージを持つときは、取得した機種の分だけを書き出す。
docker_save_image() {
  local platform
  if docker info --format '{{json .DriverStatus}}' 2>/dev/null | grep -q 'io.containerd.snapshotter'; then
    platform="${DOCKER_DEFAULT_PLATFORM:-$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')}"
    docker save --platform "$platform" "$1" -o "$2"
  else
    docker save "$1" -o "$2"
  fi
}
