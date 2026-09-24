"""範囲ツリーの拡張子集合（scope._CONTENT_EXT）と取り込み側の正典集合の整合を固定する。

取り込みが文書化する拡張子が範囲ツリーの集合から欠けると、「取り込まれているのに
範囲セレクタに出ないフォルダ」が生まれる（旧形式 Office だけのフォルダが見えない・
実環境指摘 2026-09-02）。scope.py は import を軽く保つため値を複製しており、この
テストが唯一の drift 検知になる。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import scope  # noqa: E402
from sherpa.ingest import office_md  # noqa: E402


def test_content_ext_covers_ingest_extensions():
    ingest_exts = (office_md.CONVERTIBLE_EXT | office_md.PDF_EXT
                   | set(office_md.LEGACY_OFFICE_EXT) | set(office_md.RASTER_EVIDENCE_EXT))
    missing = ingest_exts - scope._CONTENT_EXT
    assert not missing, (
        f"取り込み対象の拡張子が範囲ツリーの集合から欠けています: {sorted(missing)}"
        "（そのフォルダは範囲セレクタに出なくなる）")


def test_content_ext_includes_plain_text_and_analyzers():
    assert {".md", ".markdown", ".txt"} <= scope._CONTENT_EXT
    from sherpa.ingest.analyzers import registry
    assert registry.registered_extensions() <= scope._CONTENT_EXT


def test_content_ext_includes_text_kind_extension_maps():
    """軽量テキスト枠（`ingest.text_kind`）の第1段拡張子マップも範囲ツリーに数える対象——
    scope.py は `text_kind` を直接 import する（office_md と違い値を複製しない）ため、この
    アサーションは drift 検知ではなく契約の明文化（直接参照なので drift しようがない）。
    """
    from sherpa.ingest import text_kind
    assert text_kind.CODE_EXT <= scope._CONTENT_EXT
    assert text_kind.DOCUMENT_EXT <= scope._CONTENT_EXT
