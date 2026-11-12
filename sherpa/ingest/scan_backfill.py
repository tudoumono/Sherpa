"""署名が変わらない更新（`worker.sync` の unchanged 経路）で、欠けた manifest・件数・取り込みの集計を補う。
設計: docs/design/ingest.md「取り込みの集計」
"""
from __future__ import annotations

import logging

from .. import corpus_docs, scope_infer, store, text_encoding, worlds

_log = logging.getLogger(__name__)


def _stamp_only(report) -> bool:
    """集計の計算が失敗して、判定の版の印だけが残った集計か。"""
    return isinstance(report, dict) and set(report) == {"encoding_detect_version"}


def _try_scan_report(world: str, what: str, expected_manifest: dict | None = None) -> dict | None:
    try:
        return text_encoding.stamp_detect_version(corpus_docs.scan_report(world, expected_manifest=expected_manifest))
    except Exception:
        _log.warning("取り込み集計（scan_report）の%sに失敗しました: world=%s", what, world, exc_info=True)
        return None


def _retry_failed_scan(world: str, cur: dict, manifest: dict, sig: str) -> None:
    """版の印だけの集計は、先に集計を試す。成功したら集計とその件数を 1 回の UPDATE（今の署名のときだけ）で保存し、失敗したら件数も集計も更新しない。
    再試行は取り込み冒頭の列挙を使い回さず木を歩き直し、その rel の集合か各ファイルの mtime・ctime・size が manifest と一致しない集計（件数 None）も、根を解決できないときも保存しない。粗い件数（manifest の数え上げ）は公開しない。
    """
    if cur.get("last_manifest") is None:
        store.set_world_sig(world, sig, manifest=manifest)
    if not worlds.world_dir(world):
        return
    with scope_infer.fresh_walk_scope():
        report = _try_scan_report(world, "再試行", manifest)
    if report is None or report.get("document_count") is None:
        return
    store.set_scan_report_and_doc_count(world, report, report["document_count"], sig)


def backfill_unchanged(world: str, sig: str, manifest: dict) -> None:
    """lock 保持中に呼ぶ。他の writer が割り込んでいないか再読し、sig が一致するときだけ補う（不一致なら何もしない）。"""
    cur = store.get_world(world)
    if cur is None or cur.get("last_sig") != sig:
        return
    if _stamp_only(cur.get("last_scan_report")):
        _retry_failed_scan(world, cur, manifest, sig)
        return
    # root は1回だけ解決し、以降の doc_count 集計へ渡す（`manifest_doctype_count_from_root`）
    root = worlds.world_dir(world)
    if cur.get("last_manifest") is None and cur.get("last_doc_count") is None:
        # 両方 NULL は1回の UPDATE でまとめて補完する（`last_synced_at` は変えない）
        store.backfill_manifest_and_doc_count(
            world, manifest, corpus_docs.manifest_doctype_count_from_root(manifest, root), sig)
    else:
        if cur.get("last_manifest") is None:
            store.set_world_sig(world, sig, manifest=manifest)
            cur = store.get_world(world)         # `last_manifest` が埋まった最新行を使い直す
        if cur.get("last_doc_count") is None:
            saved_manifest = cur.get("last_manifest")
            if saved_manifest is None:
                saved_manifest = manifest
            store.backfill_doc_count(
                world, corpus_docs.manifest_doctype_count_from_root(saved_manifest, root), sig)
    if cur.get("last_scan_report") is None or corpus_docs.scan_report_missing_fields(cur.get("last_scan_report")):
        # sig 一致を確認済みの区間内なので、この世代に対する scan_report として正当（`last_synced_at` は更新しない）
        report = _try_scan_report(world, "バックフィル")
        if report is not None:
            store.set_scan_report(world, report)
