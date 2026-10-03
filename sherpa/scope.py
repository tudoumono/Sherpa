"""範囲（scope）＝フォルダ木のフィルタ。

world のフォルダ階層そのものが範囲＝`scope_prefixes`（rel_path の prefix・どの階層でも）。
- グラフ traversal の範囲は Cypher 側（`world_neo4j._scope_pred`）で効かせる。
- grep・lens の根拠は本モジュールの `in_scope`（rel_path prefix 一致）で絞る。
設計: docs/design/scope.md「範囲（scope）＝フォルダ部分木のフィルタ」
"""
from __future__ import annotations

import re

from . import scope_infer, worlds
from .ingest import importance, text_kind
from .ingest.analyzers import registry as _analyzer_registry

# 範囲ツリーに数える本文の拡張子（grep 対象＋旧形式 Office・画像 Evidence・軽量テキスト枠）。
# ソース原文の拡張子はアナライザ登録簿が真実源。office_md は import が重いため値を複製する
# （tests/unit/test_scope_content_ext.py で整合を固定）。`ingest.text_kind` は軽量なので直接 import する。
_CONTENT_EXT = {".md", ".markdown", ".txt", ".docx", ".xlsx", ".pptx", ".pdf",
                ".doc", ".xls", ".ppt", ".png", ".jpg", ".jpeg"} \
    | _analyzer_registry.registered_extensions() | text_kind.CODE_EXT | text_kind.DOCUMENT_EXT

# 出典に出さない内部来歴マーカー（chat_service と共有）
NON_DOC = {"名寄せ"}


def _norm(sp) -> str:
    return (sp or "").strip().strip("/")


def normalize_scope_paths(scope_paths) -> list:
    """strip / 空除去 / 重複排除（順序保持）。"""
    out = []
    for s in (_norm(x) for x in (scope_paths or [])):
        if s and s not in out:
            out.append(s)
    return out


def _content_rels(world: str, root=None, strict: bool = False, deadline: float | None = None):
    """world 内の本文ファイルの rel_path を列挙する（範囲ツリー・既知 prefix の元）。

    `root` を渡すと再解決しない。`strict`/`deadline` は `scope_infer.safe_files` へ渡す（OSError の re-raise／木走査の打ち切り）。
    """
    wd = root if root is not None else worlds.world_dir(world)
    if not wd:
        return []
    also = worlds.archives_dir(world)  # アーカイブの展開先も範囲ツリーの対象にする
    return [rel for rp, rel in scope_infer.safe_files(wd, strict=strict, deadline=deadline, also=also)
           if rp.suffix.lower() in _CONTENT_EXT and not importance.is_importance_control_path(rel)]


def known_scope_prefixes(world: str, root=None, strict: bool = False,
                         deadline: float | None = None) -> set:
    """選択可能なフォルダ prefix の集合（全祖先パス込み）。`root`/`strict`/`deadline` は `_content_rels` 参照。"""
    out = set()
    for rel in _content_rels(world, root, strict, deadline):
        out.update(scope_infer.ancestor_scopes(rel))
    return out


def valid_scope_paths(world: str, scope_paths, root=None, strict: bool = False,
                      deadline: float | None = None) -> bool:
    """選択 prefix が全て既知のフォルダ prefix か（未知は弾く）。空は真（world 全体）。

    `root`・`strict` は `_content_rels` 参照。`deadline` 超過時は `scope_infer.ScopeWalkDeadlineExceeded` を送出する。
    """
    sel = normalize_scope_paths(scope_paths)
    if not sel:
        return True
    known = known_scope_prefixes(world, root, strict, deadline)
    return bool(known) and all(s in known for s in sel)


def in_scope(rel_path: str, scope_paths) -> bool:
    """rel_path が選択範囲内か。空選択＝常に真。prefix 前方一致。"""
    sel = normalize_scope_paths(scope_paths)
    if not sel:
        return True
    rp = _norm(rel_path)
    return any(rp == s or rp.startswith(s + "/") for s in sel)


def _leaf_token(path: str) -> str:
    """フォルダ prefix の末端から並び番号（`01_` 等）を外した表示語。区切りを伴う数字接頭だけ外す（`4期保守` は外さない）。"""
    return re.sub(r"^\d+[_\-．.]+", "", path.split("/")[-1]).strip()


def scope_tree(world: str) -> dict:
    """範囲セレクタ用ツリー。world のフォルダ prefix（祖先込み・件数・見出し）を返す。画面は選んだ `path` を `scope_paths` として送る。"""
    rels = _content_rels(world)
    prefixes = set()
    for rel in rels:
        prefixes.update(scope_infer.ancestor_scopes(rel))  # 祖先 prefix
    counts = {p: 0 for p in prefixes}
    for rel in rels:
        for p in prefixes:
            if rel == p or rel.startswith(p + "/"):
                counts[p] += 1
    scopes = [{"path": p, "label": _leaf_token(p) or p, "depth": p.count("/"), "count": counts[p]}
              for p in sorted(prefixes)]
    return {"world": world, "label": worlds.world_label(world), "scopes": scopes}


# ---- レンズ（grep/近傍）の根拠を範囲で剪定（traversal は Cypher 側で絞る）----

def _evidence_docs(item) -> list:
    """item の根拠 doc（rel_path）。影響=evidence[list]／近傍=evidence{edges,grep}。"""
    ev = item.get("evidence")
    docs = []
    if isinstance(ev, list):
        docs += [e.get("doc") for e in ev]
    elif isinstance(ev, dict):
        docs += [e.get("doc") for e in ev.get("edges", [])]
        docs += [g.get("doc_id") for g in ev.get("grep", [])]
    return [d for d in docs if d and d not in NON_DOC]


def _keep_ev(doc, scope_paths) -> bool:
    if not doc or doc in NON_DOC:  # マーカーは doc でない＝範囲対象外（残す）
        return True
    return in_scope(doc, scope_paths)


def _prune_evidence(item, scope_paths) -> dict:
    it = dict(item)
    ev = item.get("evidence")
    if isinstance(ev, list):
        it["evidence"] = [e for e in ev if _keep_ev(e.get("doc"), scope_paths)]
    elif isinstance(ev, dict):
        it["evidence"] = {**ev,
                          "edges": [e for e in ev.get("edges", []) if _keep_ev(e.get("doc"), scope_paths)],
                          "grep": [g for g in ev.get("grep", []) if _keep_ev(g.get("doc_id"), scope_paths)]}
    return it


def filter_items(items, scope_paths):
    """近傍/根拠 item を範囲で絞り evidence を範囲内に剪定する（根拠なしの構造ノードは残す）。空選択は素通し。"""
    if not normalize_scope_paths(scope_paths):
        return items
    out = []
    for it in items:
        docs = _evidence_docs(it)
        if not docs or any(in_scope(d, scope_paths) for d in docs):
            out.append(_prune_evidence(it, scope_paths))
    return out
