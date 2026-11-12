"""OOXML zip の安全検査（zip 爆弾: 展開合計・圧縮率・メンバ数の上限）。convert と doc が上限を共有する。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

_ZIP_MAX_UNCOMPRESSED = 500 * 1024 * 1024  # zip爆弾: 展開合計上限 500MB
_ZIP_MAX_RATIO = 200  # zip爆弾: 圧縮率上限
_ZIP_MAX_MEMBERS = 10_000  # zip爆弾: メンバ数上限（EOCD の bounded 検査にも使う）


_ZIP_READ_CHUNK = 1024 * 1024  # zip爆弾: メンバ実測時の読み取り単位


def _zip_bomb_reason(p: Path, compressed_size: int) -> str | None:
    """OOXML zip の安全検査。危険なら理由文字列、安全なら None（壊れ zip は None）。

    中央ディレクトリの自己申告値（`ZipInfo.file_size`）は使わず、各メンバを実際にストリーム展開した実測バイト数で上限を判定する。
    """
    try:
        with zipfile.ZipFile(p) as z:
            infos = z.infolist()
            if len(infos) > _ZIP_MAX_MEMBERS:
                return "too_many_members"
            total = 0
            for info in infos:
                with z.open(info) as member:
                    while True:
                        chunk = member.read(_ZIP_READ_CHUNK)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _ZIP_MAX_UNCOMPRESSED:
                            return "uncompressed_too_large"
            if compressed_size > 0 and total / compressed_size > _ZIP_MAX_RATIO:
                return "compression_ratio"
    except zipfile.BadZipFile:
        return None
    return None
