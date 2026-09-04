"""層フィルタ（探す対象・調べ方ブロック §3.4）の横断一貫性テスト。

`sherpa.layer.layer_of()`（拡張子だけの近似・`CODE_EXT` membership）は、CODE-1a の `accepts()`
全滅時の資料落ち（§7 裁定10）を表現できない——登録拡張子でも `accepts()` が全滅すれば資料/未対応へ
倒れるため、拡張子だけでは「実際に code と確定したか」を判定できない。grep_tool（`classify_document`
直接呼び出し）・agentic_search（`doc_ledger` の `branch` 経由）・es_index（索引時に保存する `branch`
フィールド）の3経路すべてが、同じ確定判定（`layer_of_code()`/`in_layer_code()`）に一致することを、
`.txt` を巡って受理/拒否する1つのダミーアナライザ（rel_path で結果が分かれる＝accepts 全滅の
資料落ち経路を実際に踏む）を使い、実ファイル・実 `classify_document` で横断固定する。
"""
from __future__ import annotations

import json

from sherpa import agentic_search as A
from sherpa import corpus_docs, es_index, grep_tool, worlds
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult


class _ConditionalTxtAnalyzer(Analyzer):
    """`.txt` を要求するが、`accepted.txt` だけ受理し `declined.txt` は拒否する（§7 裁定10 の
    「accepts() 全滅→既存の資料種別（テキスト）へ資料落ち」を同一アナライザ・同一拡張子で再現する）。"""

    name = "xcut_txt"
    extensions = frozenset({".txt"})
    doctype = "受理された言語"

    def accepts(self, rel_path, head_text=""):
        return rel_path.endswith("accepted.txt")

    def collect_defs(self, text, rel_path):
        return DefResult()

    def extract_refs(self, text, rel_path):
        return RefResult()


def _setup_world(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_ANALYZERS", (_ConditionalTxtAnalyzer(),))
    wd = tmp_path / "world"; wd.mkdir()
    der = tmp_path / "derived"; der.mkdir()
    (wd / "accepted.txt").write_text("TARGETWORD 受理された内容", encoding="utf-8")
    (wd / "declined.txt").write_text("TARGETWORD 拒否された内容", encoding="utf-8")
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "observation_current_dir", lambda w: None)
    return wd, der


def test_accept_vs_decline_classification_agrees_across_grep_agentic_es(monkeypatch, tmp_path):
    world = "xcut"
    _setup_world(monkeypatch, tmp_path)

    # 前提確認: corpus_docs の確定判定（単一の真実源）が期待どおり分かれていること。
    docs = {d["name"]: d for d in corpus_docs.world_documents(world)}
    assert docs["accepted.txt"]["branch"] == "source"      # 受理＝confirmed code
    assert docs["declined.txt"]["branch"] == "office"      # 拒否＝資料落ち（既存の資料種別へ）
    assert docs["declined.txt"]["doctype"] == "テキスト"

    # ---- grep_tool: layer="code" は accepted のみ、layer="docs" は declined のみヒットする ----
    code_hits = {h["doc_id"] for h in grep_tool.grep_search("TARGETWORD", world=world, layer="code")}
    docs_hits = {h["doc_id"] for h in grep_tool.grep_search("TARGETWORD", world=world, layer="docs")}
    assert code_hits == {"accepted.txt"}
    assert docs_hits == {"declined.txt"}

    # ---- agentic_search._safe_doc_path: 同じ確定判定で層外を拒否する（read_around の実行ゲート） ----
    assert A._safe_doc_path(world, "accepted.txt", layer="code") is not None
    assert A._safe_doc_path(world, "accepted.txt", layer="docs") is None
    assert A._safe_doc_path(world, "declined.txt", layer="docs") is not None
    assert A._safe_doc_path(world, "declined.txt", layer="code") is None

    # ---- agentic_search list_docs ツール: 同じ確定判定（doc_ledger の branch）で一覧を絞る ----
    res_code, _, _, _ = A.run_tool("list_docs", {"limit": 50}, world, None, layer="code")
    res_docs, _, _, _ = A.run_tool("list_docs", {"limit": 50}, world, None, layer="docs")
    assert {d["rel_path"] for d in res_code["docs"]} == {"accepted.txt"}
    assert {d["rel_path"] for d in res_docs["docs"]} == {"declined.txt"}

    # ---- es_index: 索引時に保存する branch フィールドが同じ確定判定と一致する
    # （実 ES 不要・bulk ボディを捕捉し、`layer.es_filter` と同じ term 一致を素の Python で模す）。
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
    r = es_index.index_world(world)
    assert r["indexed"] == 2
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    by_doc = {b["doc_id"]: b for b in bodies}
    assert by_doc["accepted.txt"]["branch"] == "source"
    assert by_doc["declined.txt"]["branch"] == "office"
    # `layer.es_filter("code")`=={"term": {"branch": "source"}}／`"docs"`==must_not 同条件と同じ選別。
    code_side = {doc_id for doc_id, b in by_doc.items() if b["branch"] == "source"}
    docs_side = {doc_id for doc_id, b in by_doc.items() if b["branch"] != "source"}
    assert code_side == {"accepted.txt"}
    assert docs_side == {"declined.txt"}
