"""Evidence から作る観測まわり（図の中の文字・VLM 補足観測・OCR 観測の読込と合流）。

`office_md` の派生物生成（rag.md・先読み）が呼ぶ。
設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
"""
from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath

from .. import derived_suffixes, json_io

_log = logging.getLogger("sherpa.ingest.convert")

# `drops` に積む、観測を読み切れなかった理由（文字列のリスト）。それ以外のキーは図の文字・画像を省いた件数。
ISSUES_KEY = "observation_issues"


def add_issue(drops: dict | None, reason: str) -> None:
    """`drops` があれば、観測を読み切れなかった理由を足す（同じ理由は 1 回だけ）。"""
    if drops is not None and reason not in drops.setdefault(ISSUES_KEY, []):
        drops[ISSUES_KEY].append(reason)


def split_drops(drops: dict) -> tuple[dict, list[str]]:
    """`drops` を、図の文字・画像を省いた件数（来歴 `metafile_truncated`）と、観測を読み切れなかった理由に分ける。"""
    counts = {k: v for k, v in drops.items() if k != ISSUES_KEY}
    return counts, list(drops.get(ISSUES_KEY) or [])


def _build_figure_texts(extracted_evidence, assets_dir: Path,
                        drops: dict | None = None) -> dict[str, list[list[str]]]:
    """WMF/EMF の図の描画命令に載っている文字を、図（Evidence 要素）ごとに取り出す。
    `drops`（省略可）を渡すと、行数・文字数の上限で落とした分と上限に達した図・除いたビットマップの数、
    図の中の文字を最後まで読めなかった図（解析の失敗・打ち切り）の数 `figures_incomplete` を足す（本文は変えない・図 1 つにつき 1 回数える）。

    OCR の有効/無効や完了に依存せず、抽出済み assets だけから決定的に作る。失敗は「この文書の図の
    文字なし」へ縮退し（`figures_incomplete` に数える）、rag.md の生成は止めない。
    """
    from . import metafile_text, ocr_router

    try:
        paths: dict[str, str] = {}
        for binding in ocr_router.inventory_assets(assets_dir):
            if binding.parent_sha256 is None and not binding.is_readable_raster():
                paths.setdefault(binding.asset_sha256, binding.relative_path)
        if not paths:
            return {}
        cache: dict[str, list[str]] = {}
        result: dict[str, list[list[str]]] = {}
        for element in extracted_evidence.elements:
            for candidate in ocr_router._raster_candidates(element):
                raw = candidate.get("asset_sha256")
                if not isinstance(raw, str):
                    continue
                digest = raw.strip().lower()
                digest = digest if digest.startswith("sha256:") else "sha256:" + digest
                relative = paths.get(digest)
                if relative is None:
                    continue
                if digest not in cache:
                    content = metafile_text.read_asset_content(assets_dir.joinpath(*relative.split("/")))
                    cache[digest] = content.lines if content is not None else []
                    if drops is not None and content is not None:
                        for key, n in (("lines_dropped", content.lines_dropped),
                                       ("chars_dropped", content.chars_dropped),
                                       ("figures_text_capped", int(content.text_items_capped)),
                                       ("figures_bitmaps_capped", int(content.bitmaps_capped)),
                                       ("bitmaps_excluded", content.bitmaps_excluded),
                                       ("figures_incomplete", int(content.reason is not None))):
                            if n:
                                drops[key] = drops.get(key, 0) + n
                if cache[digest]:
                    result.setdefault(element.element_id, []).append(cache[digest])
        return result
    except Exception:
        _log.warning("図の中の文字の抽出に失敗しました（この文書では出さずに継続）", exc_info=True)
        if drops is not None:
            drops["figures_incomplete"] = drops.get("figures_incomplete", 0) + 1
        return {}


def _vlm_observation_possible() -> bool:
    """`vision` が有効アームにあり VLM が実効利用可か（False なら `_build_vlm_observation_set` は VLM を呼ばない）。"""
    from . import arms as _arms
    from .arms import vision_arm

    return "vision" in set(_arms.enabled_arm_names()) and vision_arm.resolve_vlm() is not None


def _build_vlm_observation_set(extracted_evidence, rel: str, assets_dir: Path):
    """`vision` が有効かつ VLM 実効可のときだけ、canonical が読めない画像要素を補足観測する。

    既定（`vision` が有効アームに無い、または VLM が実効利用不可）は None＝`evidence_render.render(observation_set=None)` と同じ。
    候補選定は `ocr_router.build_manifest` を呼び直すだけで、`.ocr_route.json` とは独立。
    `asset_root` は直前に抽出済みの `{rel}.assets/`。
    """
    from . import ocr_router
    from .arms import vision_arm

    if not _vlm_observation_possible():
        return None
    assets = ocr_router.inventory_assets(assets_dir)
    if not assets:
        return None
    try:
        manifest = ocr_router.build_manifest(extracted_evidence, source_rel_path=rel, assets=assets)
    except ValueError:
        return None
    decisions = [
        item for item in manifest.decisions if item.status == "selected" and item.input_kind == "asset"
    ]
    if not decisions:
        return None
    try:
        return vision_arm.build_asset_observations(
            extracted_evidence, decisions=decisions, asset_root=assets_dir)
    except Exception:
        # 補足観測は任意。VLM/Set 構築の想定外失敗で Canonical の rag.md 生成を巻き添えにしない
        _log.warning(
            "VLM 補足観測の生成に失敗しました（Canonical の生成は継続）: %s", rel, exc_info=True)
        return None


def _is_unreadable_line(exc: ValueError) -> bool:
    """1 行が JSON として読めない・形が崩れている（古い観測として弾いたものとは区別する）。"""
    return isinstance(exc, json.JSONDecodeError) or str(exc).endswith(("shape", "must be a JSON object"))


def _load_ocr_observation_sets(extracted_evidence, rel: str, obs_dir: Path | None,
                               drops: dict | None = None) -> list:
    """`obs_dir`（公開中 OCR 観測ディレクトリ）から、この `rel` 分の Observation Set 群を読む。

    OCR は隔離 worker が非同期に書く別成果物（`{rel}.ai_observations.jsonl`・1 job＝1 行）。
    各 Set は `ai_observation.from_json_str(..., ir=extracted_evidence)` で `source_content_hash` が今の Evidence と一致することを再検証し、
    古い観測は行ごとに弾く。読めた行は残す。ファイルを読めない・読めない行があれば `drops` に理由（`ocr_observation_unreadable`）を足す。
    """
    if obs_dir is None:
        return []
    from . import ai_observation

    rel_posix = PurePosixPath(rel.replace("\\", "/"))
    base = obs_dir.joinpath(*rel_posix.parts)
    jsonl_path = Path(str(base) + derived_suffixes.OBSERVATION)
    if not jsonl_path.is_file():
        return []
    sets = []
    try:
        with jsonl_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    sets.append(ai_observation.from_json_str(line, ir=extracted_evidence))
                except ValueError as exc:
                    if _is_unreadable_line(exc):
                        _log.warning("OCR 観測 Set の読めない行を飛ばしました（読めた行は使います）: %s", rel, exc_info=True)
                        add_issue(drops, "ocr_observation_unreadable")
    except (OSError, UnicodeDecodeError):
        _log.warning("OCR 観測 Set を読めませんでした: %s", rel, exc_info=True)
        add_issue(drops, "ocr_observation_unreadable")
    return sets


def _build_observation_set(extracted_evidence, rel: str, assets_dir: Path, *, obs_dir: Path | None = None,
                           with_vlm: bool = True, drops: dict | None = None):
    """VLM（同期）と OCR（非同期・公開済み分）の観測 Set を合流し、`evidence_render.render` へ渡す 1 つの Set にする。
    `with_vlm=False` は VLM を呼ばない（先読みの子用。親が VLM の補足観測が起きないと判定済みのとき）。

    どちらも無ければ None。片方だけなら合成せずそのまま返す。両方あるときだけ `merge_sets` で畳み、合流が失敗したら
    一方だけを通常の結果として返さず None（補足読み取りは未反映）にして `drops` に理由（`observation_merge_failed`）を足す。
    """
    vlm_set = _build_vlm_observation_set(extracted_evidence, rel, assets_dir) if with_vlm else None
    ocr_sets = _load_ocr_observation_sets(extracted_evidence, rel, obs_dir, drops)
    candidates = list(ocr_sets)
    if vlm_set is not None:
        candidates.append(vlm_set)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    from . import ai_observation
    try:
        return ai_observation.merge_sets(candidates, ir=extracted_evidence)
    except Exception:
        _log.warning("VLM/OCR 観測 Set の合流に失敗しました（補足読み取りは反映しません）: %s", rel, exc_info=True)
        add_issue(drops, "observation_merge_failed")
        return None


def build_observation_inputs(extracted_evidence, rel: str, assets_dir: Path, obs_dir: Path | None,
                             drops: dict, *, with_vlm: bool = True) -> tuple[object | None, dict]:
    """描画に渡す観測 Set と図の中の文字を作る。`drops` に省いた件数と観測を読み切れなかった理由を足す。"""
    observation_set = _build_observation_set(
        extracted_evidence, rel, assets_dir, obs_dir=obs_dir, with_vlm=with_vlm, drops=drops)
    return observation_set, _build_figure_texts(extracted_evidence, assets_dir, drops)


def _merge_provenance_metadata(md_path: Path, values: dict) -> None:
    """既存MD来歴へ任意補完profileを追記する。sidecar未生成の経路はEvidence内profileだけを正本にする。"""
    meta_path = md_path.parent / (md_path.name + ".meta.json")
    if not meta_path.is_file():
        return
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        raw.update(values)
        json_io.write_text_atomic(
            meta_path, json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    except (OSError, ValueError):
        pass


def record_figure_notes(dr: Path, rel: str, pending_meta: dict, drops: dict, *, refresh: bool = False) -> list[str]:
    """図の文字・画像を省いた件数（`metafile_truncated`）と観測の読み残しの理由（`observation_issues`）を来歴へ残す。

    サイドカーがまだ無ければ `pending_meta` に載せ、後の `_write_provenance` が書く。rag.md の本文は変えない。読み残しの理由を返す。
    `refresh=True`（作り直し）は、今回無かった項目も空で書いて前回の記録を消す。
    """
    counts, issues = split_drops(drops)
    notes = {"metafile_truncated": counts, "observation_issues": issues}
    if not refresh:
        notes = {k: v for k, v in notes.items() if v}
    if not (dr / (rel + ".md.meta.json")).is_file():
        pending_meta.update(notes)
    elif notes:
        _merge_provenance_metadata(dr / (rel + ".md"), notes)
    return issues
