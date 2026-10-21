"""`ir/*.document.json` を書かない（読み手が無い中間ファイル）。人が読む MD・rag.md・ES の材料は変わらず、
古い版が残した document.json は変換結果キャッシュの復元で飛ばし、作り直しも再変換も起こさない。"""
from __future__ import annotations

import pathlib

import openpyxl
import pytest

from sherpa.ingest import office_md
from sherpa.ingest.arms import ooxml_arm

pytestmark = pytest.mark.unit


def _xlsx(path: pathlib.Path, **cells) -> pathlib.Path:
    wb = openpyxl.Workbook()
    for addr, value in cells.items():
        wb.active[addr] = value
    wb.save(path)
    return path


def _tree(root: pathlib.Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_build_writes_no_document_json_and_cache_skips_old_ones(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _xlsx(src / "a.xlsx", A1="甲の値", B2="乙")
    der = tmp_path / "derived" / "md"
    rep = office_md.build_derived(src, der, world_sig="sig1")
    assert rep["converted"] == 1 and rep["document_ir_generated"] == 1 and rep["document_ir_failed"] == 0
    root = der.parent
    assert not list(root.rglob("*.document.json"))
    assert (der / office_md._DOCUMENT_IR_SIG_MARKER).is_file()          # 印は今まで通り確定する（毎回の作り直しにならない）
    assert office_md.document_ir_sig_drift(der) is False
    first = {k: v for k, v in _tree(root).items() if not k.startswith("_conv_cache/")}

    # 0.19.5 までの変換結果キャッシュ・公開中の派生に document.json が残っている（利用者が手で消す前）
    cache_dir = office_md._conv_cache_root_for(der) / "a.xlsx.d" / "ir"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "a.xlsx.document.json").write_text("{}", encoding="utf-8")
    (root / "ir" / "a.xlsx.document.json").write_text("{}", encoding="utf-8")

    def no_convert(self, path):
        raise AssertionError("キャッシュから戻すはずの原本を変換し直した")

    monkeypatch.setattr(ooxml_arm.OoxmlArm, "convert", no_convert)
    rep2 = office_md.build_derived(src, der, world_sig="sig1")
    assert rep2["converted"] == 1 and rep2["failed"] == 0
    second = {k: v for k, v in _tree(root).items() if not k.startswith("_conv_cache/")}
    assert second == first                                             # MD・rag.md・rag_chunks・evidence が同じ・document.json は戻らない

    assert not (cache_dir / "a.xlsx.document.json").exists()           # ヒットした時点でキャッシュ側の実体も消える（ディスクが返る）

    # 利用者が残りを手で消しても、作り直し・再変換は起きない
    (cache_dir / "a.xlsx.document.json").unlink(missing_ok=True)
    assert office_md.build_derived(src, der, world_sig="sig1")["converted"] == 1
    assert office_md.document_ir_sig_drift(der) is False


def test_settle_document_ir_sig_for_notices_without_document_json(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _xlsx(src / "a.xlsx", A1="甲の値")
    (src / "broken.docx").write_bytes(b"not a zip")                    # 失敗の知らせになる
    der = tmp_path / "derived" / "md"
    office_md.build_derived(src, der, world_sig="sig1")
    assert office_md._md_is_failure_notice(der, "broken.docx")
    marker = der / office_md._DOCUMENT_IR_SIG_MARKER
    marker.unlink(missing_ok=True)                                      # 印の無い旧い派生
    evidence_marker = der / office_md._EVIDENCE_IR_SIG_MARKER
    sig = evidence_marker.read_text(encoding="utf-8")

    evidence_marker.write_text("schema=old", encoding="utf-8")         # Evidence IR が今の版でなければ確定しない
    assert office_md.settle_document_ir_sig_for_notices(src, der) is False and not marker.exists()
    evidence_marker.write_text(sig, encoding="utf-8")
    ev = der.parent / "ir" / "a.xlsx.evidence.json"
    saved = ev.read_bytes()
    ev.unlink()                                                         # 知らせ以外の文書の evidence.json が無ければ確定しない
    assert office_md.settle_document_ir_sig_for_notices(src, der) is False and not marker.exists()
    ev.write_bytes(saved)
    assert office_md.settle_document_ir_sig_for_notices(src, der) is True
    assert office_md.document_ir_sig_drift(der) is False
    assert not list(der.parent.rglob("*.document.json"))


def test_remove_document_json_counts_shared_inode_once(tmp_path):
    der = tmp_path / "derived" / "md"
    (tmp_path / "derived" / "ir").mkdir(parents=True)
    cache = office_md._conv_cache_root_for(der) / "a.xlsx.d" / "ir"
    cache.mkdir(parents=True)
    pub = tmp_path / "derived" / "ir" / "a.xlsx.document.json"
    pub.write_text("x" * 100, encoding="utf-8")
    import os
    os.link(pub, cache / "a.xlsx.document.json")
    (tmp_path / "derived" / "ir" / "keep.evidence.json").write_text("{}", encoding="utf-8")
    assert office_md.remove_document_json(der) == (2, 100)
    assert not list((tmp_path / "derived").rglob("*.document.json"))
    assert (tmp_path / "derived" / "ir" / "keep.evidence.json").exists()
    assert office_md.remove_document_json(der) == (0, 0)
