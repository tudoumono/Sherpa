"""登録アナライザ対象ファイル（xml_config/properties/yaml_config・cobol 等）のサイズ上限退行是正。

アナライザ拡張 S3b の Codex RV 指摘（高）: `.xml`/`.properties`/`.yaml`/`.yml` を軽量テキスト枠
（`ingest.text_kind`）から登録アナライザへ移したことで、`corpus_docs._text_oversize()` が
`doctype == text_kind.CODE_DOCTYPE_LABEL` のときだけ発火する既存の絞り込みに引っかからず、
8MiB 上限（`text_kind.MAX_BYTES`・grep 上限と同じ）が事実上外れていた——`world_graph.build_world()`
Pass1 が `read_text()` で巨大ファイルを全量メモリに保持してしまう（単一 worker を1ファイルで
OOM させ得る）。是正＝`_text_oversize` の適用条件を `result["kind"] == "code"` 全般に広げ、登録
アナライザ（新設3種＋既存の cobol/copybook/jcl/java）すべてに同じ上限を一律適用する（単一の
真実源＝`text_kind.MAX_BYTES`・新定数は増やさない）。
"""
from __future__ import annotations

from sherpa import corpus_docs, worlds
from sherpa.ingest import text_kind
from sherpa.ingest.failure_reasons import REASON_CATALOG


def _world(monkeypatch, tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "observation_current_dir", lambda w: None)
    return wd, der


def test_oversize_xml_config_is_unreadable_with_size_exceeded_reason(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "huge.xml").write_text("<beans><bean class=\"com.acme.Foo\"/></beans>\n", encoding="utf-8")
    monkeypatch.setattr(text_kind, "MAX_BYTES", 4)     # 実ファイルを軽く保ったまま上限だけ小さくする

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["huge.xml"]
    d = docs[0]
    assert d["state"] == "unreadable"
    assert d["reason"] == "size_exceeded"
    assert d["label"] == REASON_CATALOG["size_exceeded"]["label"]
    assert d["doctype"] == "xml_config"       # 何のファイルかは分かる状態を保つ（登録アナライザの doctype）
    assert d["branch"] == "source"

    rep = corpus_docs.scan_report("w")
    assert rep["unreadable"] == 1 and rep["indexed"] == 0
    assert rep["skipped_ext"] == {".xml": 1}

    # status API は原本の doctype をそのまま返す（Office 変換失敗と同じ慣習・サイズ超過でも原本は数える）。
    assert corpus_docs.status_document_doctype("huge.xml", "w") == "xml_config"


def test_oversize_properties_is_unreadable_with_size_exceeded_reason(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "huge.properties").write_text("db.url=jdbc:postgresql://localhost/db\n", encoding="utf-8")
    monkeypatch.setattr(text_kind, "MAX_BYTES", 4)

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["huge.properties"]
    d = docs[0]
    assert d["state"] == "unreadable" and d["reason"] == "size_exceeded"
    assert d["doctype"] == "properties"


def test_oversize_yaml_is_unreadable_with_size_exceeded_reason(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "huge.yml").write_text("server:\n  port: 8080\n", encoding="utf-8")
    monkeypatch.setattr(text_kind, "MAX_BYTES", 4)

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["huge.yml"]
    d = docs[0]
    assert d["state"] == "unreadable" and d["reason"] == "size_exceeded"
    assert d["doctype"] == "yaml_config"


def test_undersize_xml_config_still_indexes_normally(monkeypatch, tmp_path):
    """上限を広げても既定の 8MiB 以内は従来どおり `ready`（回帰防止）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "small.xml").write_text("<beans><bean class=\"com.acme.Foo\"/></beans>\n", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["small.xml"]
    d = docs[0]
    assert d["state"] == "ready"
    assert d["doctype"] == "xml_config"
