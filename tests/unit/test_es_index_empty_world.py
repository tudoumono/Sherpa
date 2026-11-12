"""本文が空の資料だけの資料フォルダの全文検索の索引づくり（`es_index.index_world`）の単体テスト。"""
from __future__ import annotations

from sherpa import es_index
from tests._replace_row import offline_row


def test_index_world_empty_text_only_world_converges_without_no_chunks(monkeypatch, tmp_path):
    wd = tmp_path / "root"
    wd.mkdir()
    (wd / "a.md").write_text("   \n\t\n", encoding="utf-8")
    (wd / "b.md").write_text("", encoding="utf-8")
    monkeypatch.setattr(es_index.worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(es_index.worlds, "derived_md_dir", lambda w: tmp_path / "derived" / "md")
    monkeypatch.setattr(es_index.worlds, "derived_rag_dir", lambda w: tmp_path / "derived" / "rag")
    confirmed = []
    monkeypatch.setattr(es_index, "_confirm_content_sig", lambda w, sig, idx=None, **kw: confirmed.append(sig))
    alias_to: list = []

    def fake_req(method, path, body=None, **kw):    # 付け替えた先を引き直すと答える小さな模擬（公開は確かめてから旧い索引を消す）
        if method == "POST" and path == "/_aliases":
            alias_to[:] = [a["add"]["index"] for a in body["actions"] if "add" in a]
        if method == "GET" and path.startswith("/_alias/") and alias_to:
            return {alias_to[0]: {"aliases": {path.split("/")[-1]: {}}}}
        return {}
    monkeypatch.setattr(es_index, "_req", fake_req)
    for _ in range(2):
        r = es_index.index_world("w", content_sig="sig-x", row=offline_row("w"))
        assert r.get("error") is None
        assert r["indexed"] == 0 and r["chunks"] == 0
        assert r["rag_degraded"] == 2
        assert {d["reason"] for d in r["rag_degraded_docs"]} == {"empty_text"}
    assert confirmed == ["sig-x", "sig-x"]
