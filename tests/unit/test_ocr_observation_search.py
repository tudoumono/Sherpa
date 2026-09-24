"""OCR 観測は rag.md へ統合済み・観測専用ツリーは grep の対象外（O1・§8.1 一本化・2026-09-03）。

観測（画像の中の文字）は Canonical（決定的に変換した MD/Evidence）とは**別の木**（隔離 OCR worker が
書く別成果物）に置く。2026-08-16 時点では grep がこの別の木を直接走査して検索対象化していたが、
O1（2026-09-03）で OCR 観測を VLM と合流して `rag.md`（RAG 正本）自体へ「AI観測」レコードとして
含める経路に統合した（`sherpa/ingest/office_md.py::_build_observation_set`／
`sherpa/ingest/ai_observation.py::merge_sets`）。検索は `rag.md` 経由で足りるため、grep は
観測専用ツリー（`worlds.observation_current_dir`）をもう別ルートとして走査しない
（`sherpa/grep_tool.py::grep_search` 参照・二重ヒットを避ける）。

このファイルは今、その「もう走査しない」契約を固定する。観測専用ツリーへ書いた文字列だけでは
grep から見えないこと（＝rag.md への統合を経由しない限り検索できないこと）を確認する。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from pathlib import Path  # noqa: E402

from sherpa import grep_tool, worlds  # noqa: E402
from sherpa.ingest import observation_render  # noqa: E402

CANONICAL = "a" * 64
OBSERVATION = "b" * 64


def _publish_observation(base: Path, rel: str, body: str) -> Path:
    """観測領域に「公開済みの1世代」を最小構成で作る。"""
    target = base / observation_render.OBSERVATION_GENERATIONS_NAME / CANONICAL / OBSERVATION
    doc = target / (rel + ".rag_observations.md")
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(body, encoding="utf-8")
    (base / observation_render.OBSERVATION_POINTER_NAME).write_text(json.dumps({
        "schema": observation_render.OBSERVATION_POINTER_SCHEMA,
        "canonical_generation_id": CANONICAL,
        "observation_generation_id": OBSERVATION,
    }), encoding="utf-8")
    return doc


def test_observation_only_text_is_not_searched_directly(tmp_path, monkeypatch):
    """観測専用ツリー（`{rel}.rag_observations.md`）にしか無い文字列は grep から見えない
    （O1・rag.md へ統合していない生成物を検索対象にしない＝観測ツリーの直接走査を撤去済み）。"""
    world_dir = tmp_path / "world"
    (world_dir / "5期更改").mkdir(parents=True)
    (world_dir / "5期更改" / "資料.docx").write_bytes(b"dummy")
    observations = tmp_path / "obs"
    _publish_observation(observations, "5期更改/資料.docx",
                         "# OCR補助観測\n\n画像内文字（補正なし）:\nグラフを生成\n")

    monkeypatch.setattr(worlds, "world_dir", lambda _w: world_dir)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda _w: tmp_path / "no-derived")
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda _w: tmp_path / "no-rag")
    monkeypatch.setattr(worlds, "observation_current_dir",
                        lambda _w: observations / observation_render.OBSERVATION_GENERATIONS_NAME
                        / CANONICAL / OBSERVATION)

    hits = grep_tool.grep_search("グラフを生成", world="test")

    assert hits == [], "観測専用ツリーが grep の検索ルートとして走査されている（rag.md 統合済みのはず）"


def test_stale_observations_are_not_searched(tmp_path, monkeypatch):
    """取り込みが進んで Canonical が変わったら、古い観測は読まない（fail-safe）。"""
    observations = tmp_path / "obs"
    _publish_observation(observations, "資料.docx", "画像内文字（補正なし）:\n古い内容\n")

    monkeypatch.setenv("SHERPA_OBSERVATION_DIR", str(observations.parent / "obs-base"))
    monkeypatch.setattr(worlds, "observation_dir", lambda _w, **_kw: observations)
    # Canonical が別世代＝pointer と一致しない
    monkeypatch.setattr(worlds, "derived_dir", lambda _w: tmp_path / "derived")
    from sherpa.ingest import derived_generation
    monkeypatch.setattr(derived_generation, "active_generation_id", lambda _root: "c" * 64)

    assert worlds.observation_current_dir("test") is None


def test_no_observations_is_not_an_error(tmp_path, monkeypatch):
    """観測がまだ無い world でも検索は普通に動く。"""
    monkeypatch.setattr(worlds, "observation_dir", lambda _w, **_kw: tmp_path / "empty")
    monkeypatch.setattr(worlds, "derived_dir", lambda _w: tmp_path / "derived")

    assert worlds.observation_current_dir("test") is None
