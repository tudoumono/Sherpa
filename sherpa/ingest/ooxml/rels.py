"""OOXML の関係ファイル（`_rels/*.rels`）の共通読み取り。

ZIP から rels を読んで `Id`・`Type`・`Target`・`TargetMode` の生の属性値を返すところまでをここに集める。
外部 Target の扱い（除外／sha256 で保持）・Target の解決規則・返す形は呼び出し側ごとに違うため、ここでは決めない。
"""
from __future__ import annotations

import posixpath
from pathlib import PurePosixPath
from typing import Callable, NamedTuple
from xml.etree import ElementTree as ET

_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class Relationship(NamedTuple):
    """`<Relationship>` の属性の生の値（無い属性は None）。"""
    id: str | None
    type: str | None
    target: str | None
    mode: str | None


def rels_name(part: str) -> str:
    """`part`（zip 内のパートパス）に対応する rels のパート名（`dir/_rels/name.rels`）。"""
    path = PurePosixPath(part)
    return str(path.parent / "_rels" / f"{path.name}.rels")


def load_relationships(read: Callable[[str], bytes], part: str) -> list[Relationship]:
    """`part` の rels を `read(rels のパート名)` で読み、`Relationship` を出現順で返す。
    rels の欠落（`read` が `KeyError`）・破損（XML 不正）は空リスト。`read` は `zf.read` か `entries.__getitem__`。
    """
    try:
        root = ET.fromstring(read(rels_name(part)))
    except (KeyError, ET.ParseError):
        return []
    return [
        Relationship(r.get("Id"), r.get("Type"), r.get("Target"), r.get("TargetMode"))
        for r in root.iter(f"{_RELS}Relationship")
    ]


def resolve_target(part: str, target: str) -> str:
    """rels の `Target` を zip 内の絶対パートパスへ解決する。先頭 `/` は zip ルートから、それ以外は `part` のディレクトリからの相対。"""
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
