"""資料フォルダの走査・署名・差分（取り込みの入口で 1 回だけ歩く）。
設計: docs/design/rag.md「全体の流れ」
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import scope_infer, worlds
from . import importance, resolve_settings, world_graph
from .analyzers import registry as analyzer_registry

_SCAN_PROGRESS_INTERVAL = 500   # 走査進捗の報告間隔（ファイル数）


@dataclass
class _Scan:
    """1 回の走査の結果。`walk`＝本体の列挙（同じ取り込みの MD 化前の工程が使い回す）・`parts`＝`(rel, mtime_ns, ctime_ns, size)` のソート済み。"""
    walk: scope_infer.WalkSnapshot
    parts: list
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_ms: int | None = None


class _WorldState(tuple):
    """`world_state()` の戻り値 `(署名, 明細)`。同じ走査の結果（`scan`）を添える（添えない場合は None）。"""
    scan: _Scan | None = None


def _scan_walk(wd, progress=None) -> _Scan:
    """`wd` 配下の対象ファイルを 1 回だけ歩き、列挙（`walk`）と `(rel, mtime_ns, ctime_ns, size)` のソート済みリスト（`parts`）を返す。

    `progress`（省略可）は走査済みファイル数を `_SCAN_PROGRESS_INTERVAL` 件ごとと最後に報告する。"""
    files, parts, skipped = [], [], {}
    started_at = datetime.now(timezone.utc).isoformat()
    started_mono = time.monotonic()
    for rp, rel in scope_infer.safe_files(wd, skipped=skipped):
        files.append((rp, rel))
        try:
            st = rp.stat()
            parts.append((rel, st.st_mtime_ns, st.st_ctime_ns, st.st_size))   # ctime も含める
        except OSError:
            parts.append((rel, None, None, None))
        if progress is not None and len(parts) % _SCAN_PROGRESS_INTERVAL == 0:
            progress(len(parts))
    if progress is not None:
        progress(len(parts))
    parts.sort()
    return _Scan(scope_infer.WalkSnapshot.from_walk(wd, files, skipped), parts,
                 started_at, datetime.now(timezone.utc).isoformat(),
                 round((time.monotonic() - started_mono) * 1000))


def _scan_dir(wd, progress=None) -> list:
    """`wd` 配下の対象ファイルを `(rel, mtime_ns, ctime_ns, size)` のソート済みリストで返す（stat のみで中身は読まない）。"""
    return _scan_walk(wd, progress).parts


def _sig(parts, resolve_sig: str = "") -> str:
    # 重要度のスキーマ版・アナライザの有効構成・言及エッジの仕様版と実効値（`world_graph._mention_min_len`/`_mention_max_per_doc`）を材料に含める。
    # これらが変わると、ソースが不変でも署名が変わり全再構築される。
    # 資料フォルダの解決範囲の設定（`resolve_settings.signature_material`）があるときはそのハッシュも材料にする（設定が無ければ材料に足さない）
    # 取り込みから外す版管理の記録のフォルダの名前も材料にする（外す名前が変われば、アーカイブの中だけにあった分も含めて台帳を作り直す）
    material = (importance.IMPORTANCE_SCHEMA_VERSION, analyzer_registry.config_signature(),
                world_graph.MENTION_SCHEMA_VERSION, world_graph._mention_min_len(),
                world_graph._mention_max_per_doc(), parts,
                ("vcs_excluded", tuple(sorted(scope_infer.VCS_DIR_NAMES))))
    if resolve_sig:
        material += (("resolve_settings", resolve_sig),)
    return hashlib.sha1(repr(material).encode("utf-8")).hexdigest()


def _manifest(parts) -> dict:
    """`parts` → `rel -> [mtime_ns, ctime_ns, size]` の dict（差分チェックの基準・JSONB 保存用）。"""
    return {rel: [m, c, s] for (rel, m, c, s) in parts}


def world_signature_of_root(wd, resolve_sig: str = "") -> str:
    """解決済みの root（`Path`）から署名を計算する（`worlds.world_dir()` を再度呼ばない）。

    root 取得済みの呼び出し元はこちらを使う（再解決の間に rebind が起きると、古い root のスキャン結果を新しい root の署名で扱ってしまう）。
    """
    return _sig(_scan_dir(wd), resolve_sig)


def world_signature(world) -> str | None:
    """資料フォルダの安価な署名（各ファイルの rel/mtime/ctime/size の集約＋重要度スキーマ版＋アナライザ構成署名の SHA1・`_sig()`）。変更検知の基準。不在は None。"""
    wd = worlds.world_dir(world)
    return world_signature_of_root(wd, resolve_settings.signature_of(world)) if wd else None


def world_state(world, progress=None):
    """`(署名, ファイル明細)` を1スキャンで返す。資料フォルダ不在は `(None, None)`。`progress` は `_scan_walk` へ転送する。

    戻り値の `.scan` に同じ走査の結果を添える（`run` が MD 化前の工程で歩き直さないため）。"""
    wd = worlds.world_dir(world)
    if not wd:
        return _WorldState((None, None))
    scan = _scan_walk(wd, progress=progress)
    return _state_of(world, scan)


def _state_of(world, scan: _Scan) -> _WorldState:
    """走査の結果から `(署名, 明細)` を作る（署名は今の解決範囲の設定で計算する）。"""
    state = _WorldState((_sig(scan.parts, resolve_settings.signature_of(world)), _manifest(scan.parts)))
    state.scan = scan
    return state


def diff_dir(wd, prev_manifest, prev_sig=None, resolve_sig: str = "") -> dict:
    """フォルダ現状と取り込み済み明細の差分を返す（read-only・グラフ/台帳/ES には書かない）。

    返値は `added`/`removed`/`changed`（rel のリスト）と `total`（現在のファイル数）・`indexed`（前回取込のファイル数）。
    `prev_manifest` が None/空なら未取り込み扱い（全ファイルが added）。ただし明細が未保存でも署名（`prev_sig`）が現状と一致すれば差分なし。
    """
    parts = _scan_dir(wd)
    cur = _manifest(parts)
    prev = prev_manifest or {}
    if not prev and prev_sig is not None and _sig(parts, resolve_sig) == prev_sig:
        return {"added": [], "removed": [], "changed": [], "total": len(cur), "indexed": len(cur)}
    added = sorted(r for r in cur if r not in prev)
    removed = sorted(r for r in prev if r not in cur)
    changed = sorted(r for r in cur if r in prev and list(cur[r]) != list(prev[r]))
    return {"added": added, "removed": removed, "changed": changed,
            "total": len(cur), "indexed": len(prev)}
