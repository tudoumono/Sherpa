"""`compare_documents`（GEN-DIFF）本体: 2文書の RAG 正本（`.rag.md`）を突き合わせる素朴な決定的 diff。

正典: `docs/proposals/2026-09-03-世代間diff比較.md` §3〜§8。**方針（裁定）**: ツールは grep と同格の
素朴な決定的ツール——レコード同定・業務キー対応付け・要約・ask_user 連携は行わない。曖昧な対応文書
（§4）は候補一覧を返すだけで会話側（agentic loop の LLM）が確認する。

**独立モジュールにする理由**: 登録元（`agentic_search.py`・`mcp_server.py`）はこのモジュールを import
する側（逆は循環 import になるため不可）。パス封じ込め（doc_id 検証→resolve+is_relative_to→字面パスと
resolve() の一致で symlink 検知）は `agentic_search._safe_doc_path` と同じ流儀をここで独立に実装する
（`rag_parent_return.py` が `search_service.py` の境界のために同じ判断をしたのと同型——重複は既知の
設計判断・BUDGET-2 系の共有実装化は将来のフォローアップ）。
"""
from __future__ import annotations

import difflib
import os
import re
from pathlib import Path

from . import corpus_docs, doc_ledger, documents, scope as scope_mod, worlds
from .ingest import evidence_render


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """`grep_tool._env_int`/`agentic_search._env_int` と同型（循環 import 回避のため独立実装）。"""
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


# rag.md 1件あたりの読み取り上限（バイト・非有界にしない安全弁）。`agentic_search.
# _READ_AROUND_FILE_CAP_BYTES` と同じ env 名・既定値を共有する（§5「新しい env を増やさない」方針・
# `agentic_search` を import できない＝循環回避のため値だけ独立に読む）。
_RAG_MD_READ_CAP_BYTES = _env_int("SHERPA_READ_AROUND_FILE_CAP_BYTES", 64 * 1024 * 1024, 65536, 64 * 1024 * 1024)

# 対応文書候補列挙（§4 step3・basename 類似度）の上限件数。順位付け・確度の言語化はしない
# （`difflib.get_close_matches` の並びをそのまま返すだけ）——上限は tool result 予算を圧迫しない安全弁。
_CANDIDATES_MAX = 10

# `evidence_render._markdown` の固定ヘッダ書式（機械的な行一致だけで読み取る・追加解析はしない）。
_HEADER_SHA_RE = re.compile(r"^原本SHA-256:\s*(\S+)")
_HEADER_PROFILE_RE = re.compile(r"^変換プロファイル:\s*(\S+)\s*/\s*(\S+)\s*$")
_HEADER_SCAN_LINES = 20   # ヘッダは冒頭7行程度・余裕を持たせた走査上限


def _rag_md_path(world: str, doc_id: str) -> Path | None:
    """doc_id（rel_path）→ `{rel}.rag.md` の実パス（無効/範囲外/不在/symlink は None）。

    `agentic_search._safe_doc_path` と同じ封じ込め流儀（トラバーサル拒否→resolve+is_relative_to→
    字面パスと resolve() の一致で経路上の symlink を検知）を rag.md 専用に独立実装する。
    `rag_parent_return._rag_md_path` は ES ヒット由来の既検証 doc_id を前提にした軽量版だが、
    ここは LLM が直接生成したツール引数を受けるためより厳格な版を使う。
    """
    if not isinstance(doc_id, str) or not doc_id or doc_id.startswith("/") or "\\" in doc_id or "\x00" in doc_id:
        return None
    parts = doc_id.split("/")
    if ".." in parts or "" in parts:
        return None
    root = worlds.derived_rag_dir(world)
    if not root:
        return None
    root = Path(root)
    lexical_rel = doc_id + ".rag.md"
    cand = root / lexical_rel
    try:
        rr = root.resolve()
        rp = cand.resolve()
        if not (rp == rr or rp.is_relative_to(rr)):
            return None
        if rp != rr / lexical_rel:          # 字面パスと不一致＝経路上のどこかに symlink があった
            return None
        if not rp.is_file():
            return None
    except OSError:
        return None
    return rp


def _doc_exists(doc_id: str, world: str) -> bool:
    """doc_id が world 内に文書として実在するか（`agentic_search.verify_doc_exists` と同じ2判定・
    scope は呼び出し元＝`compare`/`_discover` が別途見る）。"""
    try:
        if corpus_docs.status_document_doctype(doc_id, world) is None:
            return False
        return documents.resolve(doc_id, world) is not None
    except Exception:
        return False


def _read_capped(path: Path, cap_bytes: int) -> tuple[str | None, bool]:
    """`path` を `cap_bytes` まで読む。戻り `(text, truncated)`。読み取り失敗時は `(None, False)`。

    `difflib.unified_diff` はシーケンス全体の比較が要る（真のストリーミング diff は成立しない）ため、
    「非有界にしない」は読み取りサイズの安全弁として実装する——cap を超えた側は冒頭 cap 分だけで比較し、
    その旨を呼び出し元が `notices[]` に積む（黙って打ち切らない）。
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
    """rel_path の第1セグメント（世代＝`docs/03-鏡モデル.md` §2 用語の `generation`）とそれ以降。"""
    if "/" not in doc_id:
        return doc_id, ""
    gen, suffix = doc_id.split("/", 1)
    return gen, suffix


def _in_scope(doc_id: str, scope_paths) -> bool:
    return scope_paths is None or scope_mod.in_scope(doc_id, scope_paths)


def _basename_candidates(world: str, source_doc_id: str, target_generation: str, scope_paths,
                         deadline: float | None) -> list:
    """§4 step3: 厳密一致（§4 step2）が0件のときの候補列挙（basename 類似度）。

    機構は作らない——`difflib.get_close_matches`（標準ライブラリ・追加依存なし）で拾えるだけ拾って
    そのまま返す。順位付け・確度スコアの言語化はしない（会話側の LLM が確認に使うヒントに留める）。
    """
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
    """§4: 明示ペア以外の対応文書の同定。戻り `(right_doc_id|None, candidates)`。

    **同一性＝パス**（`docs/03-鏡モデル.md` §2.1）のため、世代を除いた相対 suffix の完全一致は
    「0件か1件」の二択——構築した候補パスが実在するかを直接確認するだけで済む（全文書を舐めて
    ファジー一致を探す必要はない）。実在しなければ basename 類似度の候補列挙へフォールバックする。
    """
    _gen, suffix = _generation_and_suffix(source_doc_id)
    candidate_id = f"{target_generation}/{suffix}" if suffix else target_generation
    if _in_scope(candidate_id, scope_paths) and _doc_exists(candidate_id, world):
        return candidate_id, []
    return None, _basename_candidates(world, source_doc_id, target_generation, scope_paths, deadline)


def compare(world: str, args: dict, *, scope_paths=None, deadline: float | None = None) -> dict:
    """`compare_documents` ツール本体（§3・決定的・LLM 呼び出しゼロ）。

    引数は明示ペア（`left_doc_id`+`right_doc_id`・最優先）か世代発見（`source_doc_id`+
    `target_generation`）のどちらか。`scope_paths`（会話ターン全体にかかる硬いフィルタ・他ツールと
    同型）で範囲外の doc_id は拒否する。`deadline` は `doc_ledger.documents_for` の走査（候補列挙時
    のみ発生しうる）へそのまま転送する。

    戻り値の `status`: `"comparable"`（`diff`/`notices`/`compare_conditions` を返す）／
    `"needs_disambiguation"`（対応文書候補が0件/複数件・`candidates[]` を返す）／
    `"unsupported"`（片側以上に `.rag.md` が無い・比較材料が無い）。引数不備・範囲外は他の
    run_tool 系ツールと同型の `{"error": ...}`。
    """
    args = args or {}
    left_doc_id = str(args.get("left_doc_id") or "").strip()
    right_doc_id = str(args.get("right_doc_id") or "").strip()
    source_doc_id = str(args.get("source_doc_id") or "").strip()
    target_generation = str(args.get("target_generation") or "").strip()

    if left_doc_id and right_doc_id:
        pass                                    # 明示ペア最優先（§4 step1）＝発見処理を経由しない
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

    left_path = _rag_md_path(world, left_doc_id)
    right_path = _rag_md_path(world, right_doc_id)
    if left_path is None or right_path is None:
        return {"status": "unsupported", "left_doc_id": left_doc_id, "right_doc_id": right_doc_id,
                "reason": "片方以上に RAG 正本（.rag.md）が無い文書です（コード原文等・比較材料が無い）"}

    left_text, left_trunc = _read_capped(left_path, _RAG_MD_READ_CAP_BYTES)
    right_text, right_trunc = _read_capped(right_path, _RAG_MD_READ_CAP_BYTES)
    if left_text is None or right_text is None:
        return {"status": "unsupported", "left_doc_id": left_doc_id, "right_doc_id": right_doc_id,
                "reason": "RAG 正本の読み取りに失敗しました"}

    left_meta = _parse_header(left_text)
    right_meta = _parse_header(right_text)

    notices: list = []
    if left_trunc:
        notices.append(f"{left_doc_id} の RAG 正本が大きすぎるため冒頭のみで比較した")
    if right_trunc:
        notices.append(f"{right_doc_id} の RAG 正本が大きすぎるため冒頭のみで比較した")
    for doc_id, meta in ((left_doc_id, left_meta), (right_doc_id, right_meta)):
        rv = meta.get("renderer_version")
        # §3 step4: 停止せず注記だけ添えて実施する（機械的に検出できた事実のみ・解釈はしない）。
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
