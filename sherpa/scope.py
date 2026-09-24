"""範囲（scope）＝**フォルダ木のフィルタ**（鏡モデル・MIRROR-MODEL §3）。

旧モデル（layer=common/version・auto-scope 推定・structure.json・版プレフィックス）は**全撤去**。
鏡では world ディレクトリの**フォルダ階層そのもの**が範囲＝`scope_prefixes`（rel_path の prefix・どの階層でも）。
- グラフ traversal の範囲は Cypher 側（`world_neo4j._scope_pred`）で効かせる（in-memory 版＝`world_graph.subgraph`）。
- grep・lens の根拠は本モジュールの `in_scope`（rel_path prefix 一致）で絞る。
特定テーマの名前は持たない（範囲値はすべてフォルダ＝データ由来）。
"""
from __future__ import annotations

import re

from . import scope_infer, worlds
from .ingest import importance, text_kind
from .ingest.analyzers import registry as _analyzer_registry

# 範囲ツリーに数える本文の拡張子（grep 対象＝設計書MD＋ソース原文＋MD化できる Office/PDF＋
# 軽量テキスト枠）。json 等の付帯物は数えない。
# corpus_docs は PDF も文書化する（office_md.convertible_exts）ので、範囲ツリー/検証でも PDF フォルダを選べるよう含める（rv-full2 #2）。
# ソース原文（コード）分はアナライザ登録簿が単一の真実源（§2.4）。
# 旧形式 Office（.doc/.xls/.ppt）と画像 Evidence（.png/.jpg/.jpeg）も取り込み対象
# （office_md.LEGACY_OFFICE_EXT／RASTER_EVIDENCE_EXT）＝これらしか無いフォルダも範囲として
# 選べる必要がある（欠けると「取り込まれているのに範囲セレクタに出ないフォルダ」が生まれる・
# 実環境指摘 2026-09-02）。office_md は import が重いためここは値の複製＋整合テストで固定する
# （tests/unit/test_scope_content_ext.py::test_content_ext_covers_ingest_extensions）。
# 軽量テキスト枠（`ingest.text_kind`＝未登録拡張子のテキストファイル）の第1段拡張子マップは
# `text_kind` が葉ノード（軽量・`re` のみ）のため値を複製せず直接 import する（office_md と
# 違い drift テストは不要——同じ集合を直接参照するため drift しようがない）。第2段（内容推定・
# 未知拡張子/拡張子なし）はここでは数えない（走査コストを増やさない・§ ING-TEXT-1 の設計判断）。
_CONTENT_EXT = {".md", ".markdown", ".txt", ".docx", ".xlsx", ".pptx", ".pdf",
                ".doc", ".xls", ".ppt", ".png", ".jpg", ".jpeg"} \
    | _analyzer_registry.registered_extensions() | text_kind.CODE_EXT | text_kind.DOCUMENT_EXT

# 出典に出さない内部来歴マーカー（DL できる文書ではない・chat_service と共有）。
NON_DOC = {"名寄せ"}


def _norm(sp) -> str:
    return (sp or "").strip().strip("/")


def normalize_scope_paths(scope_paths) -> list:
    """strip / 空除去 / 重複排除（順序保持）。API 検証と各レンズ入力の共通正規化。"""
    out = []
    for s in (_norm(x) for x in (scope_paths or [])):
        if s and s not in out:
            out.append(s)
    return out


def _content_rels(world: str, root=None, strict: bool = False, deadline: float | None = None):
    """world 内の本文ファイルの rel_path を列挙（範囲ツリー・既知 prefix の元）。

    `root`（省略可）: 呼び出し側が既に world root を解決済みなら渡す（再解決しない）。
    未指定時は従来どおり `worlds.world_dir(world)`（内部利用の多段フォールバック解決）。
    `strict`/`deadline`: `scope_infer.safe_files` へそのまま渡す（`strict` は外部 API 経路の
    OSError re-raise、`deadline` は木走査の打ち切り・両者とも同モジュール docstring 参照）。
    """
    wd = root if root is not None else worlds.world_dir(world)
    if not wd:
        return []
    return [rel for rp, rel in scope_infer.safe_files(wd, strict=strict, deadline=deadline)
           if rp.suffix.lower() in _CONTENT_EXT and not importance.is_importance_control_path(rel)]


def known_scope_prefixes(world: str, root=None, strict: bool = False,
                         deadline: float | None = None) -> set:
    """選択可能なフォルダ prefix の集合（全祖先パス込み・どの階層でも選べる・導出は scope_infer に集約）。

    `root`/`strict`/`deadline`: `_content_rels` 参照。
    """
    out = set()
    for rel in _content_rels(world, root, strict, deadline):
        out.update(scope_infer.ancestor_scopes(rel))
    return out


def valid_scope_paths(world: str, scope_paths, root=None, strict: bool = False,
                      deadline: float | None = None) -> bool:
    """選択 prefix が全て既知のフォルダ prefix か（未知は弾く）。空は真（world 全体）。

    `root`: 呼び出し側（外部 API 等）が strict resolver で既に world root を確定済みなら渡す——
    渡された場合はここで `worlds.world_dir()` を再度呼ばない（preflight 後の再解決を避ける）。
    `strict`: `_content_rels` 参照（OSError を re-raise させるか）。
    `deadline`（省略可）: `scope_infer.safe_files` の木走査打ち切り期限（`time.monotonic()` 系の
    絶対期限）をそのまま渡す。超過時は `scope_infer.ScopeWalkDeadlineExceeded` を送出する
    （呼び出し元が捕捉して 504 等へ翻訳する・既存呼び出し元は省略時＝無期限のまま無変更）。
    """
    sel = normalize_scope_paths(scope_paths)
    if not sel:
        return True
    known = known_scope_prefixes(world, root, strict, deadline)
    return bool(known) and all(s in known for s in sel)


def in_scope(rel_path: str, scope_paths) -> bool:
    """rel_path が選択範囲内か。空選択＝常に真（world 全体）。prefix 前方一致（どの階層でも）。"""
    sel = normalize_scope_paths(scope_paths)
    if not sel:
        return True
    rp = _norm(rel_path)
    return any(rp == s or rp.startswith(s + "/") for s in sel)


def _leaf_token(path: str) -> str:
    """フォルダ prefix の末端から並び番号(`01_`等)を外した表示語。

    **区切り（_ - . 等）を伴う数字接頭だけ**を外す（`01_受付`→`受付`）。`4期保守` のように数字が名前の一部
    （直後が区切りでない）なら外さない（`4` を消さない）。
    """
    return re.sub(r"^\d+[_\-．.]+", "", path.split("/")[-1]).strip()


def scope_tree(world: str) -> dict:
    """範囲セレクタ用ツリー（D）。world のフォルダ prefix（祖先込み・件数・見出し）を返す。

    画面はこれを描いて複数選択させ、選んだ `path` を `scope_paths` として送る（rel_path prefix）。
    """
    rels = _content_rels(world)
    prefixes = set()
    for rel in rels:
        prefixes.update(scope_infer.ancestor_scopes(rel))   # 祖先 prefix（導出は scope_infer に集約・rv-full B3）
    counts = {p: 0 for p in prefixes}                        # 件数/label はここで（統合しない・Codex 指摘）
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
    if not doc or doc in NON_DOC:                     # マーカーは doc でない＝範囲対象外（残す）
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
    """近傍/根拠 item を範囲で絞り＋evidence を範囲内に剪定（構造ノード＝根拠なしは残す）。空選択は素通し。"""
    if not normalize_scope_paths(scope_paths):
        return items
    out = []
    for it in items:
        docs = _evidence_docs(it)
        if not docs or any(in_scope(d, scope_paths) for d in docs):
            out.append(_prune_evidence(it, scope_paths))
    return out
