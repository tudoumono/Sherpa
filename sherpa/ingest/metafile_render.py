"""WMF/EMF の図全体の描画（LibreOffice）をバックグラウンドで 1 件ずつ進める。

取り込み・今すぐ更新は LibreOffice を呼ばず、描画が要る図に状態（``pending``／``unavailable``）だけを
``{rel}.assets/_metafile/{親hash}.render.json`` へ残す。ここが派生領域を走査して、
``pending``（と、LibreOffice が後から入った ``unavailable``）を親 hash ごとに 1 回だけ描き、
共有キャッシュ（``metafile_text.render_cache_dir``）へ置いてから、該当文書の assets へ写し、
ルート（``.ocr_route.json``）を書き直して OCR を積み直す。状態はディスク上にあるので、再起動しても続きから進む。

LibreOffice の実行は ``legacy_convert`` の直列ロックの内側で 1 件ずつ。1 回の pass で描く数は有界。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from . import metafile_text

_log = logging.getLogger("sherpa.ingest.convert")

MAX_RENDERS_PER_PASS = 8
LOCK_TIMEOUT_MS = 2000
_pass_lock = threading.Lock()                 # 同時に 2 つの pass を走らせない（single-flight）


@dataclass(frozen=True)
class _Item:
    assets: Path
    rel: str
    parent_hex: str
    file: str | None


def _pending_items(derived: Path, *, libreoffice: bool) -> list[_Item]:
    rag = derived / "rag"
    items: list[_Item] = []
    if not rag.is_dir():
        return items
    for state_path in sorted(rag.rglob(f"*{metafile_text.RENDER_STATE_SUFFIX}")):
        if state_path.parent.name != metafile_text.CHILD_DIR or not state_path.is_file():
            continue
        assets = state_path.parent.parent
        if not assets.name.endswith(".assets"):
            continue
        parent_hex = state_path.name[: -len(metafile_text.RENDER_STATE_SUFFIX)]
        record = metafile_text.read_render_state_record(assets, parent_hex)
        if record is None:
            continue
        state = record["state"]
        if state in ("pending", "rendered_unapplied") or (state == "unavailable" and libreoffice):
            rel = assets.relative_to(rag).as_posix()[: -len(".assets")]
            items.append(_Item(assets, rel, parent_hex, record.get("file") if isinstance(record.get("file"), str) else None))
    return items


def _render(item: _Item) -> tuple[bytes | None, str | None]:
    """親 hash の描画をメモリ上に作る（ディスクには何も書かない）。戻りは ``(png, None)`` か ``(None, 理由)``。"""
    from .arms import legacy_convert

    try:
        data = (item.assets / (item.file or "")).read_bytes()
    except OSError:
        return None, "convert_failed"
    content = metafile_text.extract(data)
    if content.kind is None:
        return None, "convert_failed"
    png, reason = legacy_convert.render_metafile_png(data, content.kind)
    if png is not None and not metafile_text._valid_render(png):
        return None, "bad_output"
    return png, reason or ("convert_failed" if png is None else None)


def _store_in_cache(cache: Path, parent_hex: str, png: bytes | None, reason: str | None) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    if png is not None:
        tmp = cache / f"{parent_hex}.png.tmp"
        tmp.write_bytes(png)
        tmp.replace(cache / f"{parent_hex}.png")
    else:
        (cache / f"{parent_hex}.failed").write_text(reason or "convert_failed", encoding="utf-8")


def _rewrite_route(derived: Path, rel: str):
    from . import evidence_ir, ocr_router

    evidence_path = derived / "ir" / f"{rel}.evidence.json"
    ir = evidence_ir.read_json_file(evidence_path)
    assets_dir = derived / "rag" / f"{rel}.assets"
    manifest = ocr_router.build_manifest(
        ir, source_rel_path=rel, assets=ocr_router.inventory_assets(assets_dir))
    ocr_router.write_json_atomic(derived / "ir" / f"{rel}.ocr_route.json", manifest)
    return manifest


def _apply_to_documents(world: str, derived: Path, items: list[_Item]) -> int:
    """キャッシュの結果を該当文書の assets へ反映し、ルートを書き直して OCR を積み直す。"""
    from . import derived_generation, ocr_worker
    from ..store import ocr_jobs

    from . import office_md

    try:
        sig = (derived / "md" / office_md._WORLD_SIG_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        sig = ""
    enqueue = bool(sig) and office_md.ocr_enabled()
    done = 0
    for rel in sorted({item.rel for item in items}):
        try:
            metafile_text.materialize_children(derived / "rag" / f"{rel}.assets", keep_state=True)
            manifest = _rewrite_route(derived, rel)
        except Exception:
            _log.warning("図の描画結果のルート反映に失敗しました（次回 pass で再試行）: %s", rel, exc_info=True)
            continue
        if enqueue:
            # OCR の refresh run は処理中だと再投入されない（この文書を既に通り過ぎている）ので、
            # 書き直したルートの選択済み入力をここで直接積む（route_input_id で冪等）。
            try:
                ocr_jobs.enqueue_manifest_jobs(
                    world, manifest, canonical_generation_id=derived_generation.generation_id_for(sig),
                    engine_profile_hash=ocr_worker.profile_hash())
            except Exception:
                _log.warning("描画後の OCR job の enqueue に失敗しました（次回 pass で再試行）: %s", rel, exc_info=True)
                continue
        # ルート書き直しと job の enqueue が済んだここで初めて、再試行用の状態を消す。
        for item in items:
            if item.rel == rel:
                metafile_text.clear_render_state(item.assets, item.parent_hex)
        done += 1
    return done


def run_pass(targets: list[tuple[str, Path]] | None = None, *, max_renders: int = MAX_RENDERS_PER_PASS,
             lock=None) -> dict:
    """描画待ちを最大 ``max_renders`` 件（親 hash 単位）描く。戻りは ``{"rendered", "failed", "remaining"}``。

    OCR が無効なら何もしない。描画（LibreOffice）は排他の外でメモリ上に行い、結果は排他の中で、
    その資料フォルダと対象の図がまだ現在のものだと確かめてからだけキャッシュ・assets・ルートへ書く
    （削除・付け替えで消えた派生領域に書き戻さない。古い結果は捨てる）。
    ``targets`` は ``(world, 派生領域のルート)``。省略時は登録済みの資料フォルダ全部。``lock(world)`` は
    排他の context manager（省略時は ``store.world_lock`` を短いタイムアウトで取り、取れなければその
    資料フォルダは今回見送る）。
    """
    from . import office_md
    from .arms import legacy_convert

    if not office_md.ocr_enabled():
        return {"rendered": 0, "failed": 0, "remaining": 0, "skipped": "ocr_disabled"}
    if not _pass_lock.acquire(blocking=False):
        return {"rendered": 0, "failed": 0, "remaining": 0, "skipped": "busy"}
    try:
        default_targets = targets is None
        if targets is None:
            from .. import worlds
            targets = [(w, worlds.derived_dir(w)) for w in worlds.discover_world_ids()]
        libreoffice = legacy_convert.soffice_available()
        rendered = failed = remaining = 0
        budget = max_renders
        for world, derived in targets:
            items = _pending_items(derived, libreoffice=libreoffice)
            if not items:
                continue
            cache = derived / metafile_text.RENDER_CACHE_DIR
            fresh: dict[str, tuple[bytes | None, str | None]] = {}
            seen: set[str] = set()
            for item in items:
                if item.parent_hex in seen:
                    continue
                seen.add(item.parent_hex)
                if metafile_text.cached_render(cache, item.parent_hex) is not None or \
                        metafile_text.cached_failure(cache, item.parent_hex) is not None:
                    continue
                if budget <= 0 or not libreoffice:
                    continue
                budget -= 1
                fresh[item.parent_hex] = _render(item)
            try:
                if lock is not None:
                    ctx = lock(world)
                else:
                    from ..store import world_lock
                    ctx = world_lock(world, timeout_ms=LOCK_TIMEOUT_MS)
                with ctx:
                    if default_targets:
                        from .. import worlds
                        if world not in worlds.discover_world_ids():
                            continue                     # 削除・付け替え済み: 結果を捨てる
                    current = _pending_items(derived, libreoffice=libreoffice)   # 今も同じ図が待っているか
                    live = {item.parent_hex for item in current}
                    applied: list[_Item] = []
                    for parent_hex in sorted(live):
                        if parent_hex in fresh:
                            png, reason = fresh[parent_hex]
                            _store_in_cache(cache, parent_hex, png, reason)
                            if png is not None:
                                rendered += 1
                            else:
                                failed += 1
                        elif metafile_text.cached_render(cache, parent_hex) is None and \
                                metafile_text.cached_failure(cache, parent_hex) is None:
                            remaining += sum(1 for item in current if item.parent_hex == parent_hex)
                            continue
                        applied.extend(item for item in current if item.parent_hex == parent_hex)
                    if applied:
                        _apply_to_documents(world, derived, applied)
            except Exception:
                remaining += len(items)
                _log.warning("図の描画結果の反映を見送りました（次回 pass）: world=%s", world, exc_info=True)
        return {"rendered": rendered, "failed": failed, "remaining": remaining}
    finally:
        _pass_lock.release()


def start_loop(stop: threading.Event, *, interval: float = 300.0, first_delay: float = 60.0) -> threading.Thread:
    """アプリのプロセス内で ``run_pass`` を繰り返す daemon thread を起動する（``stop`` で止まる）。"""
    def _run() -> None:
        wait = first_delay
        while not stop.wait(wait):
            wait = interval
            try:
                result = run_pass()
                if result.get("rendered") or result.get("failed"):
                    _log.info("図全体の描画 pass: %s", result)
                if result.get("remaining") and result.get("rendered"):
                    wait = 15.0                          # まだ残っていれば間を詰める
            except Exception:
                _log.warning("図全体の描画 pass に失敗しました（次回に再試行）", exc_info=True)

    thread = threading.Thread(target=_run, daemon=True, name="sherpa-metafile-render")
    thread.start()
    return thread
