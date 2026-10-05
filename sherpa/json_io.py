"""JSON 入出力の小ユーティリティ。書き込みは tmp→os.replace のアトミック置換。"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def read_json(path, default=None):
    """JSON を安全に読む。無い/壊れ/IO エラーは `default` を返す（呼び出し側のフォールバックに使う）。"""
    try:
        p = Path(path)
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data, *, indent=None, ensure_ascii=False) -> Path:
    """JSON をアトミックに書く（親 dir は自動作成。`indent` 省略時はコンパクト）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # tmp 名は一意にする（同一ファイルへの同時書き込みで tmp を奪い合わない）
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=ensure_ascii, indent=indent), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return p


def write_text_atomic(path, text: str) -> Path:
    """任意のテキストをアトミックに書く（`write_json_atomic` と同じ流儀。直列化済みの文字列用）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return p
