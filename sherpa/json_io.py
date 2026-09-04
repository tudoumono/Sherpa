"""JSON 入出力の小ユーティリティ（**アトミック書き込み**の単一の真実源・RV DRY）。

派生領域の concepts/キャッシュ/抽出結果は、書き込み途中で同時読み（build_world 等）が**部分 JSON** を
掴まないよう **tmp→os.replace** で原子置換すべき。各所で個別実装されていたのを集約する。
"""
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
    """JSON を**アトミックに**書く（tmp→os.replace）。親 dir は自動作成。`indent` 省略時はコンパクト（キャッシュ向け）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # tmp 名は**一意**にする＝同一ファイルへの同時書き込みが tmp を奪い合わない（lock 外の経路あり・RV③ High）。
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=ensure_ascii, indent=indent), encoding="utf-8")
        os.replace(tmp, p)                                 # 同一 dir/FS なら原子置換（最後の writer 勝ち・破損なし）
    except OSError:
        try:
            tmp.unlink()                                   # 失敗時は自分の tmp を後始末（取りこぼさない）
        except OSError:
            pass
        raise
    return p


def write_text_atomic(path, text: str) -> Path:
    """任意のテキストを**アトミックに**書く（tmp→os.replace）。`write_json_atomic` と同じ流儀（親 dir 自動作成・
    tmp 名は一意・失敗時は自分の tmp を後始末）だが、呼び出し側で既に直列化済みの文字列（例:
    `document_ir.to_json_str` の決定的 JSON 文字列）をそのまま書きたい場合に使う（DOC-IR-001.5・修正4）。"""
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
