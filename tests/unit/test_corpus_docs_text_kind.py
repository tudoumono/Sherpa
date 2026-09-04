"""軽量テキスト枠（`sherpa.ingest.text_kind`）と `corpus_docs.classify_document()` の結線テスト。

ユーザー裁定 2026-09-02: 未登録拡張子のテキストファイルも台帳・出典・grep/glob・ES全文までは
通す（ベクトル・グラフ・LLM は一切通さない）。`classify_document()` の「担当なし」経路（既存の
言語アナライザ登録簿・Office/画像・`.md`/`.txt` のいずれにも該当しない拡張子）に対してのみ適用し、
既存判定は常に優先されることを固定する（`tests/unit/test_corpus_docs_analyzer_registry.py` と
同じ `_world` ヘルパー流儀）。
"""
from __future__ import annotations

import json

from sherpa import agentic_search as A
from sherpa import corpus_docs, es_index, grep_tool, layer as layer_mod, worlds
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


# ---- 第1段（拡張子マップ）: コード側 ----

def test_stage1_code_extension_is_branch_source(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "app.py").write_text("print('hi')\n", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["app.py"]
    d = docs[0]
    assert d["doctype"] == text_kind.CODE_DOCTYPE_LABEL
    assert d["branch"] == "source"
    assert d["analyzer"] is None                    # 登録アナライザは無い
    assert d["state"] == "ready"

    rep = corpus_docs.scan_report("w")
    assert rep["by_doctype"] == {text_kind.CODE_DOCTYPE_LABEL: 1} and rep["indexed"] == 1
    assert rep["analyzer_declined"] == 0 and rep["skipped_other"] == 0

    assert corpus_docs.status_document_doctype("app.py", "w") == text_kind.CODE_DOCTYPE_LABEL
    assert corpus_docs.status_document_requires_coverage("app.py", "w") is False


# ---- 第1段（拡張子マップ）: 資料側 ----

def test_stage1_document_extension_is_branch_office(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["data.csv"]
    d = docs[0]
    assert d["doctype"] == text_kind.DOCUMENT_DOCTYPE_LABEL
    assert d["branch"] == "office"                   # 資料側は既存の .txt/.md と同じ慣習
    assert d["analyzer"] is None
    assert d["state"] == "ready"

    rep = corpus_docs.scan_report("w")
    assert rep["by_doctype"] == {text_kind.DOCUMENT_DOCTYPE_LABEL: 1} and rep["indexed"] == 1

    assert corpus_docs.status_document_doctype("data.csv", "w") == text_kind.DOCUMENT_DOCTYPE_LABEL
    assert corpus_docs.status_document_requires_coverage("data.csv", "w") is False


def test_log_extension_is_not_noise_and_is_indexed(monkeypatch, tmp_path):
    """`.log` は対象外にしない（トラブルシュートで価値がある・サイズ上限で守る＝要件）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "app.log").write_text("2026-09-02 ERROR something broke\n", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["app.log"]
    assert docs[0]["doctype"] == text_kind.DOCUMENT_DOCTYPE_LABEL


# ---- 第2段（内容推定）: 未知拡張子・拡張子なし ----

def test_stage2_unknown_extension_sniffed_as_code(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "build.gradle").write_text(
        "plugins {\n  id 'java'\n}\ndependencies {\n  implementation 'x:y:1.0'\n}\n", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["build.gradle"]
    assert docs[0]["doctype"] == text_kind.CODE_DOCTYPE_LABEL
    assert docs[0]["branch"] == "source"


def test_stage2_extensionless_natural_text_sniffed_as_document(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "README").write_text(
        "この文書は業務手順について説明しています。まず最初に申請を行い、"
        "承認を得てから作業を開始してください。", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["README"]
    assert docs[0]["doctype"] == text_kind.DOCUMENT_DOCTYPE_LABEL
    assert docs[0]["branch"] == "office"


def test_status_document_doctype_does_not_content_sniff(monkeypatch, tmp_path):
    """`status_document_doctype`/`manifest_doctype_count` は軽量テキスト枠の第2段（内容推定）を
    行わない（`allow_content_sniff=False`）——`ingest/worker.py::_run_locked` のホットパスから
    毎 sync 呼ばれる `manifest_doctype_count` は「追加の走査/world root 再解決をしない」契約を
    持つ（`documents.resolve`→`worlds.world_dir` の再解決に踏み込むと、world root 解決を
    モックしていない/できない呼び出し文脈で壊れる・2026-09-02 実測）。第1段（拡張子マップ）で
    判定できる拡張子は従来どおり対象のまま。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")   # 第2段が必要
    (wd / "app.py").write_text("print(1)\n", encoding="utf-8")                      # 第1段で判定可能

    reads = []
    orig_read_head = corpus_docs._read_head

    def _tracking_read_head(rp, size=4096):
        reads.append(rp.name)
        return orig_read_head(rp, size)

    monkeypatch.setattr(corpus_docs, "_read_head", _tracking_read_head)

    assert corpus_docs.status_document_doctype("build.gradle", "w") is None   # 第2段が必要＝対象外のまま
    assert corpus_docs.status_document_doctype("app.py", "w") == text_kind.CODE_DOCTYPE_LABEL
    assert reads == []                                                        # read_head は一切呼ばれない

    manifest = {"build.gradle": [1, 2, 3], "app.py": [1, 2, 3]}
    assert corpus_docs.manifest_doctype_count(manifest, "w") == 1             # app.py だけ数える


def test_stage2_binary_unknown_extension_is_not_listed(monkeypatch, tmp_path):
    """バイナリ（NUL バイト支配的）と判定される未知拡張子は台帳に載らない（対象外のまま）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "blob.dat").write_bytes(b"\x00\x01\x02\x03binary")

    docs = corpus_docs.world_documents("w")
    assert docs == []
    rep = corpus_docs.scan_report("w")
    assert rep["skipped_other"] == 1 and rep["skipped_ext"] == {".dat": 1}


# ---- 秘匿ファイル慣習拡張子（安全側の例外・§ ING-TEXT-1 の判断） ----

def test_sensitive_extension_env_is_not_classified_as_document(monkeypatch, tmp_path):
    """`.env` は要件の設定ファイル系リストに含まれるが、`agentic_search.verify_doc_exists()` の
    既存の安全側判定（`tests/unit/test_ext2_evidence.py` 参照）と衝突しないよう対象外に据え置く。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / ".env").write_text("SECRET=1\n", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert docs == []
    assert corpus_docs.status_document_doctype(".env", "w") is None


def test_sensitive_extension_key_is_not_classified_as_document(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "config.key").write_text("private\n", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert docs == []
    assert corpus_docs.status_document_doctype("config.key", "w") is None


# ---- 意味層の内部制御ファイル（旧・`worlds.semantic_paths()` の world配下フォールバック位置・
#      GRAPH-SRC 2026-09-04 で機構自体は撤去済みだが `is_semantic_control_path` は残置ガードとして残る）----

def test_semantic_control_json_is_not_classified_as_generic_code(monkeypatch, tmp_path):
    """`semantic/concepts.json`／`semantic/l_extract.json`（旧・意味層機構の
    world配下フォールバック位置）は `.json` を汎用コード扱いにしても
    「ただの文書」として台帳/grep/ES に露出しない（実 fixture `fixtures/corpus/v1/semantic/
    concepts.json` で発覚・2026-09-02）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "semantic").mkdir()
    (wd / "semantic" / "concepts.json").write_text('{"entities": []}', encoding="utf-8")
    (wd / "semantic" / "l_extract.json").write_text('{"entities": []}', encoding="utf-8")
    (wd / "app.py").write_text("print(1)\n", encoding="utf-8")   # 対照: 通常の .py は従来どおり対象

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["app.py"]
    assert corpus_docs.status_document_doctype("semantic/concepts.json", "w") is None
    assert corpus_docs.status_document_doctype("semantic/l_extract.json", "w") is None


# ---- 登録簿に候補は居たが accepts() が全滅した拡張子（§7 裁定10）は軽量テキスト枠の対象外 ----

def test_declined_registered_extension_stays_unsupported_not_swept_into_generic_text(monkeypatch, tmp_path):
    """登録アナライザの候補は居たが `accepts()` が全滅した拡張子は、既存の資料種別
    （`.md`/`.txt`/Office/画像）に該当しなければ従来どおり「未対応」のまま——軽量テキスト枠の
    第2段（内容推定）に巻き込まれて「テキスト資料」に化けない（`test_corpus_docs_analyzer_registry.py`
    の既存契約と同じ）。"""
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    class _AlwaysDeclineCobol(Analyzer):
        name = "decline_cobol"
        extensions = frozenset({".cbl"})

        def accepts(self, rel_path, head_text=""):
            return False

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (_AlwaysDeclineCobol(),))
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "PROG.cbl").write_text("line 1\nline 2\n", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert docs == []
    rep = corpus_docs.scan_report("w")
    assert rep["analyzer_declined"] == 1 and rep["indexed"] == 0


# ---- ノイズ/一時ファイル ----

def test_noise_extension_is_excluded(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "app.py.swp").write_text("junk", encoding="utf-8")
    (wd / "backup.bak").write_text("junk", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert docs == []


def test_noise_name_prefix_is_excluded(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "~$note.csv").write_text("junk", encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert docs == []


# ---- サイズ上限（8MiB・grep 上限と同じ）----

def test_oversize_generic_code_is_unreadable_with_size_exceeded_reason(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    big = wd / "huge.py"
    big.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(text_kind, "MAX_BYTES", 4)          # サイズ上限を小さく差し替えて実ファイルを軽く保つ

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["huge.py"]
    d = docs[0]
    assert d["state"] == "unreadable"
    assert d["reason"] == "size_exceeded"
    assert d["label"] == REASON_CATALOG["size_exceeded"]["label"]
    assert d["doctype"] == text_kind.CODE_DOCTYPE_LABEL      # 何のファイルかは分かる状態を保つ
    assert d["branch"] == "source"

    rep = corpus_docs.scan_report("w")
    assert rep["unreadable"] == 1 and rep["indexed"] == 0
    assert rep["skipped_ext"] == {".py": 1}

    # status API は原本の doctype をそのまま返す（Office 変換失敗と同じ慣習・サイズ超過でも原本は数える）。
    assert corpus_docs.status_document_doctype("huge.py", "w") == text_kind.CODE_DOCTYPE_LABEL


def test_oversize_generic_document_is_unreadable(monkeypatch, tmp_path):
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "huge.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    monkeypatch.setattr(text_kind, "MAX_BYTES", 4)

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["huge.csv"]
    d = docs[0]
    assert d["state"] == "unreadable" and d["reason"] == "size_exceeded"
    assert d["doctype"] == text_kind.DOCUMENT_DOCTYPE_LABEL
    assert d["branch"] == "office"


def test_es_index_skips_unreadable_oversize_doc(monkeypatch, tmp_path):
    """サイズ超過（`state="unreadable"`）は ES 索引の唯一のスキップゲートに乗る（本文を読まない）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "huge.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(text_kind, "MAX_BYTES", 4)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {})

    r = es_index.index_world("w")
    assert r["indexed"] == 0 and r["chunks"] == 0


# ---- 層フィルタ（探す対象＝コード/資料・§3.4）横断一貫性 ----
# `tests/unit/test_layer_cross_cutting.py` と同じ流儀（grep_tool・es_index の branch が一致）。

def test_layer_and_branch_agree_across_grep_and_es_index(monkeypatch, tmp_path):
    wd, der = _world(monkeypatch, tmp_path)
    (wd / "app.py").write_text("TARGETWORD in python source\n", encoding="utf-8")
    (wd / "data.csv").write_text("TARGETWORD,in,csv\n", encoding="utf-8")

    code_hits = {h["doc_id"] for h in grep_tool.grep_search("TARGETWORD", world="w", layer="code")}
    docs_hits = {h["doc_id"] for h in grep_tool.grep_search("TARGETWORD", world="w", layer="docs")}
    assert code_hits == {"app.py"}
    assert docs_hits == {"data.csv"}

    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    captured: dict = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r["indexed"] == 2
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    by_doc = {b["doc_id"]: b for b in bodies}
    assert by_doc["app.py"]["branch"] == "source"
    assert by_doc["data.csv"]["branch"] == "office"
    assert layer_mod.es_filter("code") == {"term": {"branch": "source"}}


def test_read_around_can_read_generic_text_files(monkeypatch, tmp_path):
    """grep がヒットする軽量テキスト枠のファイルは read_around でも精読できる（`agentic_search.
    _READABLE_EXT` も `text_kind.CODE_EXT`/`DOCUMENT_EXT` を含む）——grep はヒットするが read_around
    が拒否する非対称（W0 RV High と同型）を防ぐ。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / "app.py").write_text("line1\nTARGETWORD line2\nline3\n", encoding="utf-8")
    (wd / "data.csv").write_text("line1\nTARGETWORD,line2\nline3\n", encoding="utf-8")

    res_code = A.run_tool("read_around", {"doc_id": "app.py", "line": 2, "window": 1}, "w", None)[0]
    assert "error" not in res_code and "TARGETWORD" in res_code["text"]

    res_doc = A.run_tool("read_around", {"doc_id": "data.csv", "line": 2, "window": 1}, "w", None)[0]
    assert "error" not in res_doc and "TARGETWORD" in res_doc["text"]


def test_read_around_still_rejects_sensitive_extension(monkeypatch, tmp_path):
    """`.env`/`.key` は `_READABLE_EXT` の拡大後も read_around で読めない（RV BLOCKER の意図を保つ）。"""
    wd, _der = _world(monkeypatch, tmp_path)
    (wd / ".env").write_text("SECRET=1\n", encoding="utf-8")

    res = A.run_tool("read_around", {"doc_id": ".env", "line": 1, "window": 1}, "w", None)[0]
    assert "error" in res
