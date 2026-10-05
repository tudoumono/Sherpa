#!/usr/bin/env bash
# requirements.txt と constraints.txt（カレントディレクトリ）の連結ハッシュ。start.sh（再インストールの要否）と
# install_offline_kit.sh（導入成功後の記録）が同じ式を使う＝片方だけ変えると閉域で start.sh が PyPI へ出ようとして止まる。
# 使い方: req_hash <venv の python>
req_hash() {
  "$1" -c \
    'import hashlib,sys; print(hashlib.sha256(b"".join(open(f,"rb").read() for f in sys.argv[1:])).hexdigest())' \
    requirements.txt constraints.txt
}
