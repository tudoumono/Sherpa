"""human_md（人間向け `{rel}.md`）の版の drift の軽量再生成。"""
from __future__ import annotations

import logging

from ..store.replace_rows import ReplaceRow
from . import office_md

_log = logging.getLogger("sherpa.ingest.convert")


def refresh_human_md_drift(wd, dmd, world, *, row: ReplaceRow) -> tuple[bool, str | None]:
    """human_md の版が食い違う文書だけを作り直す（rag/ES には触れない）。戻り値は `(直しに行ったか, 失敗の理由)`。"""
    if not office_md.human_md_sig_drift(wd, dmd, world=world):
        return False, None
    result = office_md.refresh_human_md(wd, dmd, row=row, world=world)
    failed = result.get("human_md_failed", 0)
    if not failed:
        return True, None
    _log.warning(
        "human_md の軽量再生成で一部の文書が失敗しました（次回 sync で再試行）: "
        "world=%s detail=%s", world, result)
    return True, f"human_md_refresh_failed:{failed}"
