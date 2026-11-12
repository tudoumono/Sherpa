"""全体の取り込み直しで作れなかった文書の、前回公開していた派生物を新しい世代へ引き継ぐ。

作れなかった文書の旧本文を、空のステージングで置き換えて消さない。
設計: docs/design/rag.md「失敗した資料のやり直し」
"""
from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

_log = logging.getLogger("sherpa.ingest.convert")

_KEEP_SUFFIX = ".keep"
_FAILURE_KEYS = ("rag_failures", "evidence_ir_failures", "unhandled_failures")


def failed_rels(rep: dict) -> list[str]:
    """派生物を作れなかった文書（rag・evidence・想定外の失敗）の rel を、重複なく出現順に返す。"""
    seen: dict[str, None] = {}
    for key in _FAILURE_KEYS:
        for item in rep.get(key) or ():
            doc = item.get("doc") if isinstance(item, dict) else None
            if isinstance(doc, str):
                seen.setdefault(doc, None)
    return list(seen)


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink(missing_ok=True)


class CarryoverError(OSError):
    """ステージングの出力を消した後に旧出力を置けなかった。世代を公開せず止める（旧本文も新出力も欠けた世代を出さない）。"""


def keep_previous_outputs(
    rep: dict, published: dict[str, Path], staging: dict[str, Path],
    names_of: Callable[[str], list[tuple[str, str]]],
) -> list[str]:
    """作れなかった文書ごとに、ステージングの中途半端な出力を捨てて公開中の旧出力（md／rag／ir）をそのまま写す。

    `published`・`staging` は層名（md／rag／ir）→ディレクトリ、`names_of(rel)` は rel の分の `(層名, 名前)`。
    公開中に旧出力が無い文書（初回）は何も変えない。写せたら rel を返す。控えへ写す段で失敗した文書は警告を出し、ステージングの出力のまま残す。
    守ること: ステージングの出力を消した後に置き換えが失敗したら `CarryoverError` を送出する（呼び出し元は公開しない）。
    """
    kept: list[str] = []
    for rel in failed_rels(rep):
        names = names_of(rel)
        previous = [(layer, name) for layer, name in names
                    if (published[layer] / name).exists() or (published[layer] / name).is_symlink()]
        if not previous:
            continue
        copies: list[tuple[Path, Path]] = []
        swapping = False
        try:
            for layer, name in previous:              # ① 旧出力を控えへ写す（ここで失敗してもステージングは触っていない）
                src, tmp = published[layer] / name, staging[layer] / (name + _KEEP_SUFFIX)
                tmp.parent.mkdir(parents=True, exist_ok=True)
                _remove(tmp)
                copies.append((tmp, staging[layer] / name))
                if src.is_dir():
                    shutil.copytree(src, tmp)
                else:
                    shutil.copy2(src, tmp)
            swapping = True
            for layer, name in names:                 # ② ステージングの中途半端な出力を消して控えを置く
                _remove(staging[layer] / name)
            for tmp, dst in copies:
                tmp.rename(dst)
        except OSError as e:
            if swapping:
                raise CarryoverError(f"作れなかった文書の旧い派生物を置けなかったため、この世代を公開しません: {rel}") from e
            _log.warning("作れなかった文書の旧い派生物を引き継げませんでした: %s", rel, exc_info=True)
            for tmp, _dst in copies:
                with contextlib.suppress(OSError):
                    _remove(tmp)
            continue
        kept.append(rel)
    return kept
