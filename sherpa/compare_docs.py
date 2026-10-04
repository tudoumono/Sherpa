"""`compare_documents` ツール本体: 2文書の RAG 正本（`.rag.md`）を突き合わせる素朴な決定的 diff。

レコード同定・業務キー対応付け・要約は行わない。対応文書が曖昧なときは候補一覧を返すだけで、確認は会話側が行う。
パス封じ込め（doc_id 検証→resolve+is_relative_to→字面パスと resolve() の一致で symlink 検知）は `worlds.rag_md_path` が行う。
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from . import corpus_docs, doc_ledger, documents, scope as scope_mod, worlds
from .env_int import env_int
from .ingest import evidence_render


# rag.md 1件あたりの読み取り上限（バイト）。`agentic_search._READ_AROUND_FILE_CAP_BYTES` と同じ env 名・既定値
_RAG_MD_READ_CAP_BYTES = env_int("SHERPA_READ_AROUND_FILE_CAP_BYTES", 64 * 1024 * 1024, 65536, 64 * 1024 * 1024)

# 対応文書候補列挙（basename 類似度）の上限件数
_CANDIDATES_MAX = 10

# `evidence_render._markdown` の固定ヘッダ書式（機械的な行一致だけで読み取る）
_HEADER_SHA_RE = re.compile(r"^原本SHA-256:\s*(\S+)")
_HEADER_PROFILE_RE = re.compile(r"^変換プロファイル:\s*(\S+)\s*/\s*(\S+)\s*$")
_HEADER_SCAN_LINES = 20  # ヘッダの走査上限


def _doc_exists(doc_id: str, world: str) -> bool:
    """doc_id が world 内に文書として実在するか（`status_document_reachable` の `True` 確定だけを実在とする＝fail-closed）。
    軽量テキスト枠の第2段も内容を読んで判定する（`allow_content_sniff=True`）。"""
    try:
        if corpus_docs.status_document_reachable(doc_id, world, allow_content_sniff=True) is not True:
            return False
        return documents.resolve(doc_id, world) is not None
    except Exception:
        return False


def _read_capped(path: Path, cap_bytes: int) -> tuple[str | None, bool]:
    """`path` を `cap_bytes` まで読む。戻り `(text, truncated)`。読み取り失敗時は `(None, False)`。

    cap を超えた側は冒頭 cap 分だけで比較し、呼び出し元が `notices[]` に積む。
    """
    try:
        with path.open("rb") as f:
            data = f.read(cap_bytes + 1)
    except OSError:
        return None, False
    truncated = len(data) > cap_bytes
    if truncated:
        data = data[:cap_bytes]
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="ignore")
    return text, truncated


def _parse_header(text: str) -> dict:
    """rag.md 冒頭ヘッダから SHA-256／変換プロファイル／`RAG_RENDERER_VERSION` を読み取る。"""
    sha256 = None
    parser_profile = None
    renderer_version = None
    for line in text.splitlines()[:_HEADER_SCAN_LINES]:
        if sha256 is None:
            m = _HEADER_SHA_RE.match(line)
            if m:
                sha256 = m.group(1)
                continue
        if renderer_version is None:
            m = _HEADER_PROFILE_RE.match(line)
            if m:
                parser_profile, renderer_version = m.group(1), m.group(2)
    return {"sha256": sha256, "parser_profile": parser_profile, "renderer_version": renderer_version}


def _generation_and_suffix(doc_id: str) -> tuple[str, str]:
    """rel_path の第1セグメント（世代＝トップフォルダ）とそれ以降。"""
    if "/" not in doc_id:
        return doc_id, ""
    gen, suffix = doc_id.split("/", 1)
    return gen, suffix


def _in_scope(doc_id: str, scope_paths) -> bool:
    return scope_paths is None or scope_mod.in_scope(doc_id, scope_paths)


def _basename_candidates(world: str, source_doc_id: str, target_generation: str, scope_paths,
                         deadline: float | None) -> list:
    """厳密一致が0件のときの対応文書候補の列挙（`difflib.get_close_matches` による basename 類似度・順位付けはしない）。"""
    try:
        rows = doc_ledger.documents_for(world, deadline=deadline)
    except Exception:
        return []
    names = []
    for r in rows:
        rel = r.get("name")
        if not rel or "/" not in rel:
            continue
        if rel.split("/", 1)[0] != target_generation:
            continue
        if not _in_scope(rel, scope_paths):
            continue
        names.append(rel)
    if not names:
        return []
    source_base = Path(source_doc_id).name
    basenames = [Path(n).name for n in names]
    close = difflib.get_close_matches(source_base, basenames, n=_CANDIDATES_MAX, cutoff=0.4)
    out: list = []
    seen: set = set()
    for cb in close:
        for rel in names:
            if Path(rel).name == cb and rel not in seen:
                out.append(rel)
                seen.add(rel)
                break
        if len(out) >= _CANDIDATES_MAX:
            break
    return out


def _discover(world: str, source_doc_id: str, target_generation: str, scope_paths,
             deadline: float | None) -> tuple[str | None, list]:
    """明示ペア以外の対応文書の同定。戻り `(right_doc_id|None, candidates)`。

    世代を除いた相対 suffix の完全一致（0件か1件）を、構築した候補パスの実在確認で判定し、無ければ basename 類似度の候補列挙へ倒す。
    設計: docs/design/scope.md「同一性＝パス」
    """
    _gen, suffix = _generation_and_suffix(source_doc_id)
    candidate_id = f"{target_generation}/{suffix}" if suffix else target_generation
    if _in_scope(candidate_id, scope_paths) and _doc_exists(candidate_id, world):
        return candidate_id, []
    return None, _basename_candidates(world, source_doc_id, target_generation, scope_paths, deadline)


def compare(world: str, args: dict, *, scope_paths=None, deadline: float | None = None) -> dict:
    """`compare_documents` ツール本体（決定的・LLM 呼び出しなし）。

    引数は明示ペア（`left_doc_id`+`right_doc_id`・最優先）か世代発見（`source_doc_id`+`target_generation`）のどちらか。
    `scope_paths` で範囲外の doc_id は拒否する。`deadline` は `doc_ledger.documents_for` の走査へ転送する。

    戻り値の `status`: `"comparable"`（`diff`/`notices`/`compare_conditions`）／`"needs_disambiguation"`（`candidates[]`）／
    `"unsupported"`（片側以上に `.rag.md` が無い）。引数不備・範囲外は `{"error": ...}`。
    """
    args = args or {}
    left_doc_id = str(args.get("left_doc_id") or "").strip()
    right_doc_id = str(args.get("right_doc_id") or "").strip()
    source_doc_id = str(args.get("source_doc_id") or "").strip()
    target_generation = str(args.get("target_generation") or "").strip()

    if left_doc_id and right_doc_id:
        pass  # 明示ペア最優先（発見処理を経由しない）
    elif source_doc_id and target_generation:
        if not _in_scope(source_doc_id, scope_paths):
            return {"error": "指定 doc_id は対象範囲外です"}
        resolved, candidates = _discover(world, source_doc_id, target_generation, scope_paths, deadline)
        if resolved is None:
            return {"status": "needs_disambiguation", "source_doc_id": source_doc_id,
                    "target_generation": target_generation, "candidates": candidates}
        left_doc_id, right_doc_id = source_doc_id, resolved
    else:
        return {"error": "left_doc_id+right_doc_id か source_doc_id+target_generation のどちらかを指定してください"}

    if not _in_scope(left_doc_id, scope_paths) or not _in_scope(right_doc_id, scope_paths):
        return {"error": "指定 doc_id は対象範囲外です"}

    left_path = worlds.rag_md_path(world, left_doc_id)
    right_path = worlds.rag_md_path(world, right_doc_id)
    if left_path is None or right_path is None:
        return {"status": "unsupported", "left_doc_id": left_doc_id, "right_doc_id": right_doc_id,
                "reason": "片方以上に RAG 正本（.rag.md）が無い文書です（コード原文等・比較材料が無い）"}

    left_text, left_trunc = _read_capped(left_path, _RAG_MD_READ_CAP_BYTES)
    right_text, right_trunc = _read_capped(right_path, _RAG_MD_READ_CAP_BYTES)
    if left_text is None or right_text is None:
        # 固定理由コード（`agentic_search._record_tool_result_error_code` が拾って `backend_failures["read_io"]` へ反映する）
        return {"status": "unsupported", "left_doc_id": left_doc_id, "right_doc_id": right_doc_id,
                "reason": "RAG 正本の読み取りに失敗しました", "error_code": "read_io_failed"}

    left_meta = _parse_header(left_text)
    right_meta = _parse_header(right_text)

    notices: list = []
    if left_trunc:
        notices.append(f"{left_doc_id} の RAG 正本が大きすぎるため冒頭のみで比較した")
    if right_trunc:
        notices.append(f"{right_doc_id} の RAG 正本が大きすぎるため冒頭のみで比較した")
    for doc_id, meta in ((left_doc_id, left_meta), (right_doc_id, right_meta)):
        rv = meta.get("renderer_version")
        # 停止せず注記だけ添えて実施する（機械的に検出できた事実のみ）
        if rv and rv != evidence_render.RAG_RENDERER_VERSION:
            gen, _suffix = _generation_and_suffix(doc_id)
            notices.append(f"片側({gen})の表現バージョンが古い({rv})")

    diff_lines = list(difflib.unified_diff(
        left_text.splitlines(), right_text.splitlines(),
        fromfile=left_doc_id, tofile=right_doc_id, lineterm=""))

    return {
        "status": "comparable",
        "diff": "\n".join(diff_lines),
        "notices": notices,
        "compare_conditions": {
            "left": {"doc_id": left_doc_id, "sha256": left_meta.get("sha256"),
                    "renderer_version": left_meta.get("renderer_version")},
            "right": {"doc_id": right_doc_id, "sha256": right_meta.get("sha256"),
                     "renderer_version": right_meta.get("renderer_version")},
        },
    }
