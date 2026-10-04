"""OOXML の rels 読み取り（共通 `ooxml.rels`）を使う 5 つの呼び出し側が、外部 Target の扱い・相対パスの正規化・Type の保持を
関数ごとの規則どおりに保つことを、同じ rels 1 つに対する出力で固定する。"""
from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

pytestmark = pytest.mark.unit

from sherpa.ingest import evidence_spike, metafile_text
from sherpa.ingest.ooxml import excel, word

_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_IMG = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_RELS_XML = f"""<?xml version="1.0"?>
<Relationships xmlns="{_REL_NS}">
  <Relationship Id="rId1" Type="{_IMG}" Target="../media/a.png"/>
  <Relationship Id="rId2" Type="t2" Target="/xl/media/b.png"/>
  <Relationship Id="rId3" Type="t3" Target="https://example.test/x" TargetMode="External"/>
  <Relationship Id="rId4" Type="t4" Target="c/./d.xml" TargetMode="Internal"/>
  <Relationship Id="rId5" Type="t5" Target="../../../up.png"/>
  <Relationship Id="rId6" Type="t6"/>
  <Relationship Type="t7" Target="noid.png"/>
</Relationships>""".encode("utf-8")
_PART = "xl/drawings/drawing1.xml"
_RELS_NAME = "xl/drawings/_rels/drawing1.xml.rels"


def _zip() -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(_RELS_NAME, _RELS_XML)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def test_internal_targets_resolved_and_external_dropped():
    zf = _zip()
    entries = {_RELS_NAME: _RELS_XML}
    expected = {"rId1": "xl/media/a.png", "rId2": "xl/media/b.png", "rId4": "xl/drawings/c/d.xml", "rId5": "../up.png"}
    assert excel._load_rels(zf, _PART) == expected
    assert evidence_spike._relationships(entries, _PART) == expected
    assert evidence_spike._relationship_records(entries, _PART)["rId1"] == {"target": "xl/media/a.png", "type": _IMG}
    assert set(evidence_spike._relationship_records(entries, _PART)) == set(expected)
    # metafile_text は文書の外へ出る `..` を捨てる（`evidence_spike` の normpath は残す）
    assert metafile_text._rels(zf, _PART) == {**expected, "rId5": "up.png"}


def test_raw_targets_kept_for_word_and_external_hashed_for_images():
    zf = _zip()
    raw = word.load_rels(zf, _PART)
    assert raw["rId3"] == "https://example.test/x" and raw["rId1"] == "../media/a.png"
    assert raw["rId6"] is None and "noid.png" not in raw.values()
    img = evidence_spike._image_relationship_records({_RELS_NAME: _RELS_XML}, _PART)
    assert img["rId3"] == {
        "relationship_type": "t3", "target_mode": "External",
        "external_target_sha256": "sha256:" + hashlib.sha256(b"https://example.test/x").hexdigest(),
    }
    assert img["rId1"] == {"relationship_type": _IMG, "target_mode": "Internal", "media_part": "xl/media/a.png"}
    assert "rId6" not in img


def test_missing_or_broken_rels_is_empty():
    zf = _zip()
    assert excel._load_rels(zf, "xl/none.xml") == {} and metafile_text._rels(zf, "xl/none.xml") == {}
    assert evidence_spike._relationships({_RELS_NAME: b"<broken"}, _PART) == {}
    assert word.load_rels(zf, "xl/none.xml") == {}
