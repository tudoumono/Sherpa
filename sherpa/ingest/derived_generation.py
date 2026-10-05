"""公開中の派生物がどの World 署名から作られたかを「世代ID」として返す。

公開時に World 署名（`worker.world_signature`）を派生物へ刻み（`office_md._WORLD_SIG_MARKER`）、
それを世代IDとして扱う。公開中の派生物は常に1つで、過去世代は残らない。OCR など後段の処理が
「いまの派生物が書き込み先として正しいか」を判定するのに使う。
設計: docs/design/rag.md「OCR（非同期・隔離ワーカー）」
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .office_md import _WORLD_SIG_MARKER


def active_dir(derived_root: str | Path) -> Path:
    """公開中の派生物ディレクトリ（人間用 md 層 `md/`）。

    世代IDの元になる `.world_sig` マーカーはこの層に置く。RAG 正本・中間表現は `active_rag_dir`/`active_ir_dir`。
    """
    return Path(derived_root) / "md"


def active_rag_dir(derived_root: str | Path) -> Path:
    """公開中の RAG 正本＋証跡ディレクトリ（`rag/`）。"""
    return Path(derived_root) / "rag"


def active_ir_dir(derived_root: str | Path) -> Path:
    """公開中の中間表現ディレクトリ（`ir/`）。"""
    return Path(derived_root) / "ir"


def active_world_sig(derived_root: str | Path) -> str | None:
    """公開中の派生物に刻まれた World 署名そのもの（`worlds.last_sig` と同じ値）。"""
    marker = active_dir(derived_root) / _WORLD_SIG_MARKER
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def generation_id_for(world_sig: str) -> str:
    """World 署名から世代ID（64桁16進・`store/ocr_jobs.py` の契約）を作る。

    投入側と照合側は必ずこの関数を通す（生の署名と混ぜると世代不一致が続く）。
    """
    return hashlib.sha256(world_sig.strip().encode("utf-8")).hexdigest()


def active_generation_id(derived_root: str | Path) -> str | None:
    """公開中の派生物の世代ID。刻まれていなければ None（＝不明）。

    None は「一致」として扱わない（`ocr_worker` は不一致扱いで OCR 結果を書かない）。
    """
    sig = active_world_sig(derived_root)
    return generation_id_for(sig) if sig else None
