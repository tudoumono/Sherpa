"""入れ替えに入る前・行を消す前に、原本と設定が行に確定した値のままかを確かめる。
行の `source_sig`（走査で確かめた原本の署名）と `settings`（ものごとの設定の値）を、今の値と比べる。違えば行に理由を書いて `SourceChanged` を投げる（自動では再開しない）。
設計: docs/design/data.md「PostgreSQL（主な表と役割）」
"""
from __future__ import annotations

from pathlib import Path

from .. import scope_infer, store, worlds
from ..store import settings as store_settings
from ..store.replace_rows import ReplaceRow, SourceChanged
from . import resolve_settings, source_scan

SOURCE_MODE_KEY = "_source"   # 行の `settings` の印。"last" の行（規則版・LLM の書き換え）は走査せず、前回取り込んだ `last_sig` と比べる
SOURCE_LAST = "last"


def current_settings(world: str) -> dict:
    """ものごとの設定の値（`fix_sources` が行に確定する形）。作り方を決める版・構成・設定だけで、鍵は含めない。今の `get_system_settings` の値（固定の間は固定した値）で計算する。"""
    from .. import embeddings, es_index
    from . import importance, llm_render, office_md
    from .analyzers.registry import config_signature
    from .world_graph import MENTION_SCHEMA_VERSION
    arms = office_md._current_arms_sig()
    llm = llm_render.available() or {}
    try:
        ec = embeddings.cfg()
        embed = {"provider": ec.get("provider"), "model": ec.get("model")} if ec else None
    except Exception:
        embed = None
    return {
        "archives": {},
        "llm_render": {"provider": llm.get("provider"), "model": llm.get("model"), "prompt": llm_render._PROMPT_VERSION},
        "ir": {"ir": office_md._current_document_ir_sig(), "arms": arms},
        "md": {"human": office_md._current_human_md_sig(), "pdf": office_md._current_pdf_human_md_sig(), "arms": arms},
        "rag": {"rag": office_md._current_rag_sig(), "arms": arms},
        "graph": {"resolve": resolve_settings.signature_of(world), "analyzers": repr(config_signature()),
                  "mention": MENTION_SCHEMA_VERSION, "marks": _marks_value(world)},
        "ledger": {"importance": importance.IMPORTANCE_SCHEMA_VERSION},
        "es": {"mapping": es_index.ES_MAPPING_VERSION, "arms": arms, "human": office_md._current_human_md_sig(),
               "analyzers": es_index._analyzer_config_sig(), "chunk_lines": es_index._CHUNK_LINES, "embed": embed},
    }


def _marks_value(world: str) -> str:
    """ビルドの定義を読む場所の印そのものの値（印が変われば、グラフと名前の台帳の番号を新しくして作り直す）。
    守ること: 当てはめた結果の値は使わない（アーカイブの展開物で変わり、正しい最初の取り込みを止める）。"""
    from .. import build_marks
    from .build_link import marks_signature
    return marks_signature(build_marks.load(world))


def _fresh_settings(world: str) -> dict:
    """固定を外し、保存済みの今の設定で `current_settings` を計算する。"""
    with resolve_settings.unpinned(), store_settings.pinned_system_settings(store_settings._read_system_settings_fresh()):
        return current_settings(world)


def verify(row: ReplaceRow, item: str | None) -> None:
    """行が確定した原本と、`item` の設定の値（`None` なら行を消す直前の全部）が、今も同じか確かめる。違えば `SourceChanged`。
    ①消す仕事・掃除の行と、原本をまだ確定していない行は確かめない ②行は読み直して今の値で比べる（持っている行は確定の前のものかもしれない）
    ③1 文書の行はその文書の明細だけ、全体の行は走査し直した署名を比べる。規則版・書き換えの行は、入れ替えの前は前回取り込んだ `last_sig`、行を消す直前は走査し直した署名も比べる
    ④設定の値は行にある項目だけを今の保存済みの設定で計算し直して比べる。
    """
    from ..store import replace_rows
    cur = replace_rows.read_row(row.id)
    if cur is None or cur.kind != "build" or cur.source_state != "fixed":
        return
    if cur.doc:
        if not read_ok(cur, cur.doc):
            raise SourceChanged(f"原本が変わりました: world={cur.world} rel={cur.doc}")
    else:
        last = cur.settings.get(SOURCE_MODE_KEY) == SOURCE_LAST
        if last and _last_sig(cur.world) != cur.source_sig:
            raise SourceChanged(f"前回取り込んだ署名が変わりました: world={cur.world}")
        if (not last or (item is None and cur.source_sig)) and _scan_sig(cur.world) != cur.source_sig:
            raise SourceChanged(f"原本が変わりました: world={cur.world}")
    wanted = {item} if item else {k for k in cur.settings if not k.startswith("_")}
    fresh = _fresh_settings(cur.world)
    for k in sorted(wanted):
        if k in cur.settings and fresh.get(k) != cur.settings[k]:
            raise SourceChanged(f"設定が変わりました: world={cur.world} item={k}")


def derive_one(wd, derived, rp: Path, rel: str, *, row: ReplaceRow, world: str | None = None) -> dict:
    """1 ファイルの変換（`office_md.derive_one`）の前に、原本 `rel` が確定した明細のままかを確かめる。違えば `SourceChanged`。"""
    from . import office_md
    if not read_ok(row, rel):
        raise SourceChanged(f"原本が変わりました: rel={rel}")
    return office_md.derive_one(wd, derived, rp, rel, row=row, world=world)


def read_ok(row: ReplaceRow, rel: str) -> bool:
    """原本 `rel` が、走査で確かめたときの明細（更新時刻・変更時刻・大きさ）のままか。アーカイブの中の文書は、入っているアーカイブの原本の明細と比べる。
    行が原本の一覧を持たない（走査しない回・確定前）ときは真。
    """
    listing = _listing_of(row)
    if listing is None:
        return True
    own = rel if rel in listing else _archive_of(rel, listing)
    wd = worlds.world_dir(row.world)
    if own is None or not wd:
        return False
    try:
        st = (Path(wd) / own).stat()
    except OSError:
        return False
    return list(listing[own]) == [st.st_mtime_ns, st.st_ctime_ns, st.st_size]


def _archive_of(rel: str, listing: dict) -> str | None:
    """`rel` が入っているアーカイブの rel（一覧にあるものの中で最も深い親）。入っていなければ None。"""
    from .archive_extract import archive_kind
    parts = rel.split("/")
    for i in range(len(parts) - 1, 0, -1):
        parent = "/".join(parts[:i])
        if parent in listing and archive_kind(parent) is not None:
            return parent
    return None


_LISTING: dict = {}   # 直近の行の一覧（読むたびに DB を引かない）。確定前の None は覚えない


def _listing_of(row: ReplaceRow) -> dict | None:
    from ..store import replace_rows
    key = (row.id, row.ingest_no)
    if _LISTING.get("key") != key:
        listing = replace_rows.read_source_list(row)
        if listing is None:
            return None
        _LISTING.clear()
        _LISTING.update(key=key, listing=listing)
    return _LISTING["listing"]


def _scan_sig(world: str) -> str | None:
    with resolve_settings.unpinned(), scope_infer.fresh_walk_scope():   # 取り込みの冒頭の列挙を使い回さず、今の木を歩き直す
        return source_scan.world_signature(world)


def _last_sig(world: str) -> str | None:
    w = store.get_world(world)
    return (w or {}).get("last_sig") or ""
