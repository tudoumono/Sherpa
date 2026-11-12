"""ソースの組のフォルダ名の一覧（1 か所）。表記の揺れの判定・フォルダの点検・管理画面・ビルドの定義の読み取りが同じものを見る。
一覧は増やさない。名前は全角・半角と大文字・小文字をそろえて比べる。
設計: docs/03-鏡モデル.md §2.5・docs/design/scope.md「ビルドの定義と実行名」
"""
from __future__ import annotations

import unicodedata

SOURCE_DIR_NAMES = frozenset({"src", "source", "sources", "ソース", "ソースコード"})


def _fold(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def is_source_dir(name: str) -> bool:
    """フォルダ名がソースの組の名前か。"""
    return _fold(name) in SOURCE_DIR_NAMES


def in_source_group(rel: str) -> bool:
    """パスの途中にソースの組のフォルダがあるか（ファイル名は見ない）。"""
    return any(is_source_dir(p) for p in rel.split("/")[:-1])


def source_group_dirs(rels) -> list:
    """資料のパスの一覧から、ソースの組のフォルダ（名前が一覧に入るフォルダ）を返す。"""
    found: set = set()
    for rel in rels:
        parts = rel.split("/")[:-1]
        for i, part in enumerate(parts):
            if is_source_dir(part):
                found.add("/".join(parts[:i + 1]))
    return sorted(found)
