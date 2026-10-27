"""範囲（scope）＝フォルダ木のフィルタ。

world のフォルダ階層そのものが範囲＝`scope_prefixes`（rel_path の prefix・どの階層でも）。
- グラフ traversal の範囲は Cypher 側（`world_neo4j._scope_pred`）で効かせる。
- grep・lens の根拠は本モジュールの `in_scope`（rel_path prefix 一致）で絞る。
設計: docs/design/scope.md「範囲（scope）＝フォルダ部分木のフィルタ」
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from pathlib import PurePosixPath
from typing import NamedTuple

from . import doc_ledger, scope_infer, store, worlds
from .ingest import importance, text_kind
from .ingest.analyzers import registry as _analyzer_registry

_log = logging.getLogger("sherpa")

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


def _by_ext(names) -> list:
    return [rel for rel in names if PurePosixPath(rel).suffix.lower() in _CONTENT_EXT]


def _walk_rels(world: str, root=None, strict: bool = False, deadline: float | None = None) -> list:
    """台帳が無いときだけ資料フォルダを歩いて本文の rel_path を列挙する。`root` を渡すと再解決しない。
    `strict`／`deadline` は `safe_files` へ渡す（OSError の re-raise／木走査の打ち切り）。
    `deadline` 省略時は利用者の操作向けの短い期限を付ける（超過で `ScopeWalkDeadlineExceeded`）。
    """
    wd = root if root is not None else worlds.world_dir(world)
    if not wd:
        return []
    if deadline is None:
        deadline = scope_infer.user_op_deadline()
    also = worlds.archives_dir(world)  # アーカイブの展開先も範囲ツリーの対象にする
    return [rel for rp, rel in scope_infer.safe_files(wd, strict=strict, deadline=deadline, also=also)
           if rp.suffix.lower() in _CONTENT_EXT and not importance.is_importance_control_path(rel)]


def _content_rels(world: str, root=None, strict: bool = False, deadline: float | None = None):
    """world 内の本文ファイルの rel_path を列挙する（範囲ツリー・既知 prefix の元）。

    取り込みの台帳（`documents` の name）があればそれだけから出し、資料フォルダは歩かない（`root`／`strict`／`deadline` は使わない）。
    台帳が空・読めないときだけ `_walk_rels` で歩く。
    """
    names = doc_ledger.ledger_rels(world)
    if names is not None:
        return _by_ext(names)
    return _walk_rels(world, root, strict, deadline)


class _PrefixIndex(NamedTuple):
    """本文の rel の一覧から出る結果。`scopes`＝(path, label, depth, count) を path 昇順・`known`＝選べる prefix の集合。不変。"""
    scopes: tuple
    known: frozenset


def _build_index(rels) -> _PrefixIndex:
    counts: dict = {}
    for rel in rels:
        for p in scope_infer.ancestor_scopes(rel):  # 祖先 prefix ごとに配下の件数を数える
            counts[p] = counts.get(p, 0) + 1
    scopes = tuple((p, _leaf_token(p) or p, p.count("/"), counts[p]) for p in sorted(counts))
    return _PrefixIndex(scopes, frozenset(counts))


# 資料フォルダごとの台帳由来の結果。鍵＝台帳の変更印（`store.ledger_stamp`）で、変われば作り直す。
_INDEX_CACHE: dict = {}
_INDEX_LOCKS: dict = {}
_INDEX_LOCKS_GUARD = threading.Lock()


def _index_lock(world: str) -> threading.Lock:
    with _INDEX_LOCKS_GUARD:
        return _INDEX_LOCKS.setdefault(world, threading.Lock())


def _prefix_index(world: str, root=None, strict: bool = False, deadline: float | None = None) -> _PrefixIndex:
    """台帳があれば資料フォルダごとに 1 回だけ作って使い回し、台帳が変わったら作り直す。台帳が無いときは歩いて都度作る（保存しない）。
    ① 変更印を引く（台帳が空→歩く・引けない→従来どおり `_content_rels`） ② 同じ印のキャッシュがあれば返す
    ③ 無ければ資料フォルダごとのロックで 1 本だけ、印を引き直してから台帳を読んで作る。
    守ること: 印は台帳の全件読みより先に引く（古い印に新しい中身が付くのは許すが、逆は許さない）。
    """
    try:
        stamp = store.ledger_stamp(world)
    except Exception as e:
        _log.warning("scope: world=%s 台帳の変更印を引けません: %s", world, e)
        return _build_index(_content_rels(world, root, strict, deadline))
    if not stamp[0]:
        _INDEX_CACHE.pop(world, None)
        return _build_index(_walk_rels(world, root, strict, deadline))
    hit = _INDEX_CACHE.get(world)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    with _index_lock(world):
        try:
            stamp = store.ledger_stamp(world)
        except Exception as e:
            _log.warning("scope: world=%s 台帳の変更印を引けません: %s", world, e)
            return _build_index(_content_rels(world, root, strict, deadline))
        if not stamp[0]:
            _INDEX_CACHE.pop(world, None)
            return _build_index(_walk_rels(world, root, strict, deadline))
        hit = _INDEX_CACHE.get(world)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        names = doc_ledger.ledger_rels(world)
        if names is None:
            _INDEX_CACHE.pop(world, None)
            return _build_index(_walk_rels(world, root, strict, deadline))
        index = _build_index(_by_ext(names))
        _INDEX_CACHE[world] = (stamp, index)
        return index


def known_scope_prefixes(world: str, root=None, strict: bool = False,
                         deadline: float | None = None) -> set:
    """選択可能なフォルダ prefix の集合（全祖先パス込み）。`root`/`strict`/`deadline` は `_walk_rels` 参照。"""
    return set(_prefix_index(world, root, strict, deadline).known)


def valid_scope_paths(world: str, scope_paths, root=None, strict: bool = False,
                      deadline: float | None = None) -> bool:
    """選択 prefix が全て既知のフォルダ prefix か（未知は弾く）。空は真（world 全体）。

    `root`・`strict` は `_walk_rels` 参照。`deadline` 超過時は `scope_infer.ScopeWalkDeadlineExceeded` を送出する。
    """
    sel = normalize_scope_paths(scope_paths)
    if not sel:
        return True
    known = _prefix_index(world, root, strict, deadline).known
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
    """範囲セレクタ用ツリー。world のフォルダ prefix（祖先込み・件数・見出し）を返す。画面は選んだ `path` を `scope_paths` として送る。
    台帳があれば台帳から出し（資料フォルダごとに 1 回作って使い回す・`_prefix_index`）、台帳が無く歩くときの期限超過は `scope_infer.ScopeWalkDeadlineExceeded`。"""
    scopes = [{"path": p, "label": label, "depth": depth, "count": count}
              for p, label, depth, count in _prefix_index(world).scopes]
    return {"world": world, "label": worlds.world_label(world), "scopes": scopes}


_ETAG_FORMAT = "1"  # 木の形を変えたら上げる（ブラウザの古い写しを無効にする）


def scope_tree_etag(world: str) -> str | None:
    """`scope_tree(world)` の ETag（台帳の変更印と見出しから作る）。台帳が空・印を引けないときは None。"""
    try:
        count, at = store.ledger_stamp(world)
    except Exception:
        return None
    if not count:
        return None
    raw = f"{_ETAG_FORMAT}|{world}|{worlds.world_label(world)}|{count}|{at.isoformat() if at else ''}"
    return '"' + hashlib.sha1(raw.encode("utf-8")).hexdigest() + '"'


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
