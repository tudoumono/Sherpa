"""取り込みの結果から、失敗した文書の一覧（`{rel: 理由}`）の材料を集める。読むだけで何も作り直さない。
"""
from __future__ import annotations

import os

from .. import json_io, store, worlds
from . import office_md, text_kind

# `office_md.build_derived()` の per-file 失敗リスト（`[{"doc": rel, "reason": str}]`）のキー。末尾の `_failures` を落とした残りを stage 名にする
FAILURE_LIST_KEYS = ("unhandled_failures", "legacy_conversion_failures", "conversion_failures",
                     "document_ir_failures", "evidence_ir_failures", "rag_failures")


def is_noise_rel(rel: str) -> bool:
    """ロックファイルなどのノイズ（`text_kind.is_noise`）の rel か。失敗の一覧には載せない。"""
    return text_kind.is_noise(os.path.basename(rel), os.path.splitext(rel)[1].lower())


def failed_doc_reasons(drep: dict) -> dict[str, str]:
    """`office_md.build_derived()` の各段 `*_failures` から、失敗（縮退）した文書ごとの生の理由 `{rel: reason}` を作る。

    同じ文書が複数の段に載っているときは `FAILURE_LIST_KEYS` の先に出る段の理由を採る。
    """
    out: dict[str, str] = {}
    for key in FAILURE_LIST_KEYS:
        for entry in drep.get(key) or []:
            doc, raw_reason = entry.get("doc"), entry.get("reason")
            if isinstance(doc, str) and isinstance(raw_reason, str) and doc not in out and not is_noise_rel(doc):
                out[doc] = raw_reason
    return out


def failed_docs_from_derived(world: str) -> dict[str, str]:
    """公開中の派生（`md/{rel}.md.meta.json`）から、失敗の知らせの文書 `{rel: 理由コード}` を集める。読むだけで何も作り直さない。

    失敗の一覧の導入前に取り込んだ資料フォルダの初回用。最新の run の `failed_files` に載る文書（知らせを持たない失敗）も足す。
    """
    out: dict[str, str] = {}
    dmd = worlds.derived_md_dir(world)
    if dmd.is_dir():
        suffix = ".md.meta.json"
        for meta_path in sorted(dmd.rglob("*" + suffix)):
            reason = office_md.failure_notice_reason(json_io.read_json(meta_path, default=None))
            rel = meta_path.relative_to(dmd).as_posix()[: -len(suffix)]
            if reason and not is_noise_rel(rel):
                out[meta_path.relative_to(dmd).as_posix()[: -len(suffix)]] = reason
    snap = ((store.get_latest_run_summary(world) or {}).get("extraction_snapshot") or {})
    for it in ((snap.get("failed_files") or {}).get("items") or []):
        doc = it.get("doc") if isinstance(it, dict) else None
        if isinstance(doc, str) and doc not in out and not is_noise_rel(doc):
            out[doc] = it.get("detail") or it.get("reason") or "other"
    return out
