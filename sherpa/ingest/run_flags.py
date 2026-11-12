"""取り込みの結果（flags）へ載せる警告の組み立て。

派生物の生成・アーカイブの展開・孤児の掃除・OCR の準備で起きた失敗を、取り込みの結果に warn として出す
（取り込みを止めるものではない）。per-file の詳細は `failed_files` を単一の出所にし、ここへは複製しない。
"""
from __future__ import annotations


def _warn(reason: str, doc: str | None = None, **extra) -> dict:
    return {"doc": doc, "action": "warn", "reason": reason, **extra}


def archive_sync_errors(manifest: dict) -> list[str | None]:
    """アーカイブの展開結果のうち、更新を読めず前回の展開物を残しているもの（`last_sync_error`）の rel。"""
    return [rel for rel, item in sorted(manifest.items()) if isinstance(item, dict) and item.get("last_sync_error")]


def office_flags(d: dict) -> list[dict]:
    """派生 MD の生成結果 `d`（`office_md.build_derived` の戻り値＋`archive_sync_errors`）を警告の flag にして返す。"""
    flags: list[dict] = []
    if d.get("derived_partial"):              # 一部の文書を作れなかった（旧い写しがあれば残して公開・失敗の一覧に各文書が載る）
        flags.append(_warn("office_md:derived_partial:" + ",".join(
            f"{k}={v}" for k, v in sorted(d["derived_partial"].items())),
            count=sum(d["derived_partial"].values()), many=bool(d.get("derived_partial_many"))))
    if d.get("error"):
        flags.append(_warn(f"office_md:{d['error']}"))
    if d.get("ocr_routes_error"):
        flags.append(_warn(f"office_md:ocr_routes_failed:{d['ocr_routes_error']}"))
    if d.get("ocr_enqueue_error"):
        flags.append(_warn(f"office_md:ocr_{d['ocr_enqueue_error']}"))
    reasons = sorted({f["reason"] for f in d.get("observation_failures") or ()})
    if reasons:
        flags.append(_warn("office_md:observation_issues:" + ",".join(reasons), count=len(d["observation_failures"])))
    flags.extend(_warn("archive_sync_failed", doc=rel) for rel in d.get("archive_sync_errors") or ())
    return flags


def reconcile_flags(result: dict | None) -> list[dict]:
    """孤児の掃除の結果から、掃除できなかったもの・掃除自体を行えなかったことの flag を返す（`None` は掃除の対象外）。"""
    if not isinstance(result, dict):
        return []
    skipped = result.get("skipped")
    if skipped and skipped != "test_db_isolated":
        return [_warn(f"reconcile_skipped:{skipped}")]
    failed = result.get("failed")
    return [_warn("reconcile_failed:" + ",".join(failed), count=len(failed))] if failed else []
