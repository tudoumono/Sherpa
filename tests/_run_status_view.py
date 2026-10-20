"""`store.get_latest_run_status` が返す形を、全件の `extraction_snapshot` から Python で作る（DB を使わないテストの stub と、SQL 側の結果の照合用の基準）。"""
from __future__ import annotations

from sherpa.ingest import world_graph


def flag_counts(flags_all) -> dict:
    dict_flags = [f for f in flags_all if isinstance(f, dict)]
    dropped = world_graph.unparsed_sources_from_flags(dict_flags)
    return {"always_shown": sorted({f["reason"] for f in dict_flags
                                    if f.get("reason") in ("plugin_failed", "rebind_failed_rolled_back",
                                                            "rebind_rollback_failed")}),
            "unparsed_syntax": dropped["syntax"], "unparsed_oversize": dropped["size_exceeded"],
            "mention_unreadable": sum(1 for f in dict_flags if f.get("reason") == "unreadable_mention_doc"),
            "md_failed_many": sum(f.get("count") or 0 for f in dict_flags
                                  if f.get("many") is True
                                  and str(f.get("reason", "")).startswith("office_md:derived_partial"))}


def status_view(run, limit: int = 200):
    if run is None:
        return None
    snap = run.get("extraction_snapshot")
    snap = snap if isinstance(snap, dict) else {}
    flags_all = snap.get("flags") or []
    return {"id": run.get("id"), "status": run.get("status"), "progress": run.get("progress"),
            "extraction_snapshot": {k: v for k, v in snap.items() if k != "flags"},
            "flags_total": len(flags_all), "flags_head": flags_all[:limit],
            "flag_counts": flag_counts(flags_all)}
