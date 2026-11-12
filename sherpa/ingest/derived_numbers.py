"""派生物の層（md／rag／ir）の直下に置く `.ingest_no`（取り込みの番号と設定の値）と、その場で書き換える関数の包み。
設計: docs/design/data.md「PostgreSQL（主な表と役割）」
"""
from __future__ import annotations

import functools
import json
from contextvars import ContextVar
from pathlib import Path

from .. import json_io
from ..store import replace_rows
from ..store.replace_rows import ReplaceRow

INGEST_NO_FILE = ".ingest_no"
_begun: ContextVar[set] = ContextVar("derived_begun_layers")   # 包みごとに、書き換えに入った層を控える


def layer_dir(derived, layer: str) -> Path:
    """公開中の md 層 `derived` から見た `layer`（`md`／`rag`／`ir`）の層のディレクトリ。"""
    return Path(derived) if layer == "md" else Path(derived).with_name(layer)


def write(layer_path, row: ReplaceRow, item: str) -> None:
    """層の直下に、この行の取り込みの番号と `item` の設定の値を書く（層のディレクトリが無ければ作らない）。"""
    if not Path(layer_path).is_dir():
        return
    json_io.write_text_atomic(Path(layer_path) / INGEST_NO_FILE, json.dumps(
        {"ingest_no": row.ingest_no, "settings_sig": replace_rows.settings_sig(row, item)}) + "\n")


def read(layer_path) -> dict | None:
    """層の `.ingest_no`（`{"ingest_no", "settings_sig"}`）。無い・読めなければ None。"""
    rec = json_io.read_json(Path(layer_path) / INGEST_NO_FILE, default=None)
    return rec if isinstance(rec, dict) else None


def begin(row: ReplaceRow, *layers: str) -> None:
    """層（`layers`）を最初に書き換える直前（前提の確認を終えた後）に 1 回呼ぶ。層を「入れ替えた」にする。呼ばなかった層は、包みも番号を置き換えない。"""
    for layer in layers:
        replace_rows.before_swap(row, row.world, layer)
    _begun.get().update(layers)


def rewrites_layers(*layers: str, derived_at: int = 1):
    """層（`layers`）をその場で書き換える関数の包み。必須の `row` を受け取り、関数が `begin` で書き換えに入った層だけ、全部書き終えたら（`error` が無ければ）
    層の `.ingest_no` を置き換えて「古いものを消した」にする。途中で落ちると番号は古いままで、再開が層ごとやり直す。
    `derived` は位置引数の `derived_at` 番目。
    """
    def wrap(fn):
        @functools.wraps(fn)
        def run(*args, row: ReplaceRow, **kwargs):
            begun: set[str] = set()
            token = _begun.set(begun)
            try:
                rep = fn(*args, row=row, **kwargs)
            finally:
                _begun.reset(token)
            if not rep.get("error"):
                for layer in layers:
                    if layer in begun:
                        write(layer_dir(args[derived_at], layer), row, layer)
                        replace_rows.after_old_removed(row, row.world, layer)
            return rep
        return run
    return wrap
