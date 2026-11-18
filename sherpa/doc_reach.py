"""資料フォルダの文書が、出典として載せてよい・原本として配信してよい実体か（範囲は見ない）。
出典の確かめ（`parts.read.tools.verify_doc_exists`）と原本ダウンロード（`routers.documents.doc_download`）が同じ判定を共有する。
設計: docs/design/data.md「秘匿ファイルの扱い」
"""
from __future__ import annotations

from . import corpus_docs, documents, scope_infer


def doc_reachable(doc_id: str, world: str) -> bool:
    """doc_id が資料フォルダ内に文書として実在し、取り込みが外すものでないか。
    (1) 版管理の記録のフォルダの中でない。
    (2) 文書種別: `corpus_docs.status_document_reachable(..., allow_content_sniff=True)` が `True`（秘匿名・重要度設定ファイル・判定不能は fail-closed で落ちる）。
    (3) 実在: `documents.resolve`（root 配下への直接解決・アーカイブ展開先を含む）。
    """
    try:
        parts = doc_id.split("/")
        if any(scope_infer.is_vcs_dir_name(p) for p in parts[:-1]):
            return False
        if corpus_docs.status_document_reachable(doc_id, world, allow_content_sniff=True) is not True:
            return False
        return documents.resolve(doc_id, world) is not None
    except Exception:
        return False


def in_layer_confirmed(doc_id: str, world: str, layer) -> bool:
    """台帳に無いファイルが層に入るか。拡張子でなく取り込みと同じ分類（名前で決まらなければ先頭を読む）で決める。
    守ること: 取り込みが外すファイル（`doc_reachable` を通らないもの）は層に関わらず入れない。
    層が絞られているときに分類できない・読めないファイルも入れない（層の外の名前を出さない）。
    """
    from . import layer as layer_mod
    if not doc_reachable(doc_id, world):
        return False
    if layer_mod.normalize_layer(layer) == "both":
        return True
    got = corpus_docs.classify_by_name(doc_id)
    if got is not None:
        return layer_mod.in_layer_code(got[1] == "source", layer)
    found = documents.resolve(doc_id, world)
    if found is None:
        return False
    verdict = corpus_docs.classify_file(found, doc_id, found.suffix.lower())
    if verdict.get("kind") not in ("code", "document"):
        return False
    return layer_mod.in_layer_code(verdict["kind"] == "code", layer)
