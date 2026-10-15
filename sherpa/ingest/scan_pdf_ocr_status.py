"""文字の層が無いスキャン PDF（来歴 method=`pdf_scan_ocr`）の OCR の状態を、失敗の一覧へ反映する。

取り込みのたび（`worker` の全件 run の後・unchanged の分岐の後）に `settle` を呼ぶ。読み取りが動いていない・終わったのに本文が無い文書を
`ocr_unavailable`／`ocr_failed` で載せ、本文が検索に出る文書は外す。best-effort で、取り込みの成否には影響しない。
設計: docs/design/rag.md「全体の流れ」
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .. import json_io, store, worlds
from . import ai_observation, derived_generation, office_md

_log = logging.getLogger("sherpa.ingest.convert")

SCAN_OCR_METHOD = "pdf_scan_ocr"
REASON_UNAVAILABLE = "ocr_unavailable"
REASON_FAILED = "ocr_failed"
_OCR_REASONS = frozenset({REASON_UNAVAILABLE, REASON_FAILED})
_META_SUFFIX = ".md.meta.json"


def scan_ocr_rels(world: str) -> list[str]:
    """公開中の人向け MD の来歴（`*.md.meta.json`）から、method が `pdf_scan_ocr` の資料の rel を昇順で返す。meta.json だけを読む。"""
    dmd = worlds.derived_md_dir(world)
    if not dmd.is_dir():
        return []
    out = []
    for meta_path in sorted(dmd.rglob("*" + _META_SUFFIX)):
        try:
            if SCAN_OCR_METHOD not in meta_path.read_text(encoding="utf-8"):
                continue
        except OSError:
            continue
        meta = json_io.read_json(meta_path, default=None)
        if isinstance(meta, dict) and meta.get("method") == SCAN_OCR_METHOD:
            out.append(meta_path.relative_to(dmd).as_posix()[: -len(_META_SUFFIX)])
    return out


def has_published_observation(obs_dir: Path | None, rel: str) -> bool:
    """公開中の OCR 観測（`{rel}.ai_observations.jsonl`）に、rag.md へ載る本文（`ai_observation.answer_observations`）が1件でもあるか。読めない・壊れているときは False。"""
    if obs_dir is None:
        return False
    path = Path(str(obs_dir.joinpath(*rel.split("/"))) + ".ai_observations.jsonl")
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                observation_set = ai_observation.from_json_str(line)
                if any(o.text.strip() for o in ai_observation.answer_observations(observation_set)):
                    return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return False


def settle(world: str, world_sig: str | None = None) -> None:
    """スキャン PDF ごとに OCR の状態を評価し、失敗の一覧を更新する（`store.world_lock` 保持中に呼ぶ）。

    ① 観測が公開済みで本文がある → ocr_* の記録を外す ② OCR worker が使えない → `ocr_unavailable`
    ③ その世代の job が全て終わっているのに観測が無い → `ocr_failed` ④ 処理待ち・処理中 → 何もしない。
    OCR が無効なら何もしない。`world_sig` 省略時は確定済みの署名（未確定なら何もしない）を使う。
    """
    if not office_md.ocr_enabled():
        return
    try:
        _settle(world, world_sig)
    except Exception:
        _log.warning("スキャン PDF の OCR の状態の評価に失敗しました（取り込み自体は継続）: world=%s", world, exc_info=True)


def _settle(world: str, world_sig: str | None) -> None:
    rels = scan_ocr_rels(world)
    if not rels:
        return
    recorded = {r["rel"]: r["reason"] for r in store.list_failed_docs(world)}
    obs_dir = office_md._resolve_ocr_observation_dir(world)
    pending = []
    for rel in rels:
        if has_published_observation(obs_dir, rel):
            if recorded.get(rel) in _OCR_REASONS:
                store.remove_failed_doc(world, rel)
        else:
            pending.append(rel)
    if not pending:
        return

    from ..store import ocr_jobs
    from . import ocr_worker

    if not ocr_jobs.worker_availability_summary(ocr_worker.profile_hash())["available"]:
        reason = REASON_UNAVAILABLE
    else:
        sig = world_sig or (store.get_world(world) or {}).get("last_sig")
        if not sig:
            return
        state = ocr_jobs.generation_state(world, derived_generation.generation_id_for(sig))
        if not (state["terminal"] and not state["unpublished_jobs"]):
            return                                                  # 処理待ち・処理中・公開待ち
        reason = REASON_FAILED
    for rel in pending:
        if recorded.get(rel) != reason:
            store.record_failed_doc(world, rel, reason)
