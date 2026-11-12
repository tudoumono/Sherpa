"""優先・除外 見直し（区切り 2・3 巡目）: 規則の対象の種別・保存の競合・ES の優先を問い合わせの中で。名前は架空。"""
from __future__ import annotations

import json

import pytest

from sherpa import path_rules as PR

WORLD = "prrv6"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_WORLD", raising=False)
    monkeypatch.delenv(PR.ENV_KEY, raising=False)
    monkeypatch.setattr(PR, "_last_good", None)
    monkeypatch.setattr(PR, "_fail_until", 0.0)
    monkeypatch.setattr(PR, "_TYPE_CACHE", {})


def _set(monkeypatch, items):
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: items}))


# ---- 1. 規則に対象の種別を持つ ----

def test_derived_artifacts_follow_the_target_type_not_the_extension(monkeypatch):
    _set(monkeypatch, [
        {"path": "ZZdoc/README", "kind": "exclude", "outside_scope": False, "target_type": "file"},     # 拡張子の無いファイル
        {"path": "ZZdoc/A.docs", "kind": "exclude", "outside_scope": False, "target_type": "folder"},   # ドットを含むフォルダ
        {"path": "ZZdoc/legacy", "kind": "exclude", "outside_scope": False}])                          # 種別の無い保存値
    r = PR.rules_for(WORLD)
    assert r.derived_of_excluded("ZZdoc/README.rag.md") and r.derived_of_excluded("ZZdoc/README.assets/p.png")
    assert not r.derived_of_excluded("ZZdoc/A.docs.rag.md")
    assert r.derived_of_excluded("ZZdoc/legacy.rag.md")                       # 種別が分からない規則は、派生物も除外する側に倒す
    assert r.excluded("ZZdoc/A.docs/x.md")                                                              # フォルダの配下は除外
    assert PR.filter_output({"hits": [{"rag_path": "ZZdoc/README.rag.md"}, {"rag_path": "ZZdoc/A.docs.rag.md"}]}, WORLD) == {
        "hits": [{"rag_path": "ZZdoc/A.docs.rag.md"}]}


def test_sandbox_denies_artifacts_of_an_extensionless_file_only(tmp_path):
    from sherpa.providers.codex import sandbox
    kb, rag = tmp_path / "kb", tmp_path / "rag"
    for d in (kb / "ZZdoc" / "A.docs", rag / "ZZdoc"):
        d.mkdir(parents=True)
    (kb / "ZZdoc" / "README").write_text("x")
    (rag / "ZZdoc" / "README.rag.md").write_text("x")
    (rag / "ZZdoc" / "A.docs.rag.md").write_text("keep")
    rules = PR.Rules(PR.validate_entries([{"path": "ZZdoc/README", "kind": "exclude", "target_type": "file"},
                                          {"path": "ZZdoc/A.docs", "kind": "exclude", "target_type": "folder"}]))
    deny = {p.replace(str(tmp_path.resolve()) + "/", "") for p in sandbox._exclude_deny_entries(
        [str(kb), str(rag)], rules, derived_roots=[str(rag.resolve())])}
    assert deny == {"kb/ZZdoc/README", "kb/ZZdoc/A.docs", "rag/ZZdoc/README.rag.md"}


def test_target_type_is_checked_on_disk_and_legacy_values_are_resolved_when_read(monkeypatch, tmp_path):
    from sherpa import store, worlds
    (tmp_path / "ZZdoc" / "A.docs").mkdir(parents=True)
    (tmp_path / "ZZdoc" / "README").write_text("x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: tmp_path / "_arch")
    assert PR.detect_target_type(WORLD, "ZZdoc/README") == "file"
    assert PR.detect_target_type(WORLD, "ZZdoc/A.docs") == "folder"
    assert PR.detect_target_type(WORLD, "ZZdoc/none") == ""
    typed = PR.with_target_types(WORLD, PR.validate_entries([{"path": "ZZdoc/README", "kind": "exclude", "target_type": "folder"}]))
    assert typed[0].target_type == "file"                                  # クライアントの申告ではなく実物
    legacy = {PR.SETTINGS_KEY: {WORLD: [{"path": "ZZdoc/README", "kind": "exclude", "outside_scope": False}]}}
    monkeypatch.setattr(store, "get_system_settings", lambda **k: legacy)
    assert PR.rules_for(WORLD).file_exclude_paths == ("ZZdoc/README",)


# ---- 4. 優先は問い合わせの中で ----

def test_es_priority_is_applied_inside_the_query_before_the_cut(monkeypatch):
    """通常の取得が上限 k で優先の資料を押し出しても、優先の下だけを別に取って統合するので残る。"""
    from sherpa import es_index
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_embed_system_settings_snapshot", lambda: None)
    calls = []

    def _req(method, path, body=None, **kw):
        calls.append(body)
        q = json.dumps(body.get("query", {}))
        only_prio = '"minimum_should_match": 1' in q and '"ZZspec"' in q and "must_not" not in q.split('"filter"')[0]
        pool = ([{"_id": "p", "_score": 1.0, "_source": {"doc_id": "ZZspec/p.md", "line": 1, "text": "p", "ext": ".md"}}]
                if only_prio else
                [{"_id": "x", "_score": 2.0, "_source": {"doc_id": "x.md", "line": 1, "text": "x", "ext": ".md"}},
                 {"_id": "p", "_score": 1.0, "_source": {"doc_id": "ZZspec/p.md", "line": 1, "text": "p", "ext": ".md"}}])
        return {"hits": {"hits": pool[:body.get("size", 99)]}}
    monkeypatch.setattr(es_index, "_req", _req)
    hits, _ = es_index.search(WORLD, "q", k=1, vector=False)                 # 設定なし: 通常の 1 件だけ
    assert [h["doc_id"] for h in hits] == ["x.md"] and len(calls) == 1
    _set(monkeypatch, [{"path": "ZZspec", "kind": "priority", "outside_scope": False}])
    hits, _ = es_index.search(WORLD, "q", k=1, vector=False)
    assert [h["doc_id"] for h in hits] == ["ZZspec/p.md"]                    # 取った後の並べ替えでは押し出された優先は戻らない
    hits, _ = es_index.search(WORLD, "q", k=2, vector=False)
    assert [h["doc_id"] for h in hits] == ["ZZspec/p.md", "x.md"]


# ---- 区切り 3・1 巡目の是正 ----

def test_priority_search_embeds_the_query_only_once(monkeypatch):
    """優先がある資料フォルダのベクトル検索で、質問の埋め込みは優先・通常の 2 本の問い合わせを通して 1 回だけ。"""
    from sherpa import embeddings, es_index
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(embeddings, "cfg", lambda *a, **k: {"provider": "p", "model": "m", "dim": 3})
    monkeypatch.setattr(es_index, "_embed_system_settings_snapshot", lambda: None)
    monkeypatch.setattr(es_index, "_index_meta_read", lambda w, idx=None: (es_index._index_meta(w), False))  # 読み取りの失敗種別は差し替えた `_index_meta` の値から
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "embed_provider": "p", "embed_model": "m", "dim": 3, "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID,
        "quant": embeddings.ES_VECTOR_QUANT})
    monkeypatch.setattr(embeddings, "vector_to_es_bytes", lambda v: "AAAA")
    embeds = []
    monkeypatch.setattr(embeddings, "embed", lambda texts, ec, **k: embeds.append(texts) or [[0.1, 0.2, 0.3]])
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {"hits": {"hits": [
        {"_id": "1", "_score": 1.0, "_source": {"doc_id": "x.md", "line": 1, "text": "t", "ext": ".md"}}]}})
    monkeypatch.setattr(es_index, "_mark_keyword_matches", lambda w, q, hits: hits)
    _set(monkeypatch, [{"path": "ZZspec", "kind": "priority", "outside_scope": False}])
    es_index.search_knn_only(WORLD, "q", k=3)
    assert len(embeds) == 1
    embeds.clear()
    es_index.search(WORLD, "q", k=3, vector=True)
    assert len(embeds) == 1
    embeds.clear()
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({}))                          # 設定が無ければ従来どおり 1 本・埋め込み 1 回
    es_index.search_knn_only(WORLD, "q", k=3)
    assert len(embeds) == 1


def test_unknown_target_type_fails_closed_and_is_retried(monkeypatch):
    """種別を判定できなかった規則は、派生物も除外する側に倒し、判定できなかった結果を覚えない（復旧したら再判定する）。"""
    from sherpa import store
    legacy = {PR.SETTINGS_KEY: {WORLD: [{"path": "ZZdoc/README", "kind": "exclude", "outside_scope": False}]}}
    monkeypatch.setattr(store, "get_system_settings", lambda **k: legacy)
    answers = iter(["", "file", "folder"])
    monkeypatch.setattr(PR, "detect_target_type", lambda w, p: next(answers))
    r = PR.rules_for(WORLD)                                                  # 1 回目: 判定できない
    assert r.derived_of_excluded("ZZdoc/README.rag.md") and r.file_exclude_paths == ("ZZdoc/README",)
    r = PR.rules_for(WORLD)                                                  # 2 回目: 再判定して file
    assert r.items[0].target_type == "file" and r.derived_of_excluded("ZZdoc/README.rag.md")
    monkeypatch.setattr(PR, "_TYPE_CACHE", {})
    r = PR.rules_for(WORLD)                                                  # folder と確かめられたら派生物には当てない
    assert r.items[0].target_type == "folder" and not r.derived_of_excluded("ZZdoc/README.rag.md")


def test_sandbox_denies_artifacts_while_the_type_is_unknown(tmp_path):
    from sherpa.providers.codex import sandbox
    kb, rag = tmp_path / "kb", tmp_path / "rag"
    for d in (kb / "ZZdoc", rag / "ZZdoc"):
        d.mkdir(parents=True)
    (kb / "ZZdoc" / "README").write_text("x")
    (rag / "ZZdoc" / "README.rag.md").write_text("x")
    rules = PR.Rules(PR.validate_entries([{"path": "ZZdoc/README", "kind": "exclude"}]))   # 種別不明
    deny = {p.replace(str(tmp_path.resolve()) + "/", "") for p in sandbox._exclude_deny_entries(
        [str(kb), str(rag)], rules, derived_roots=[str(rag.resolve())])}
    assert deny == {"kb/ZZdoc/README", "rag/ZZdoc/README.rag.md"}


def test_worst_case_deny_budget_matches_what_the_sandbox_really_enumerates(tmp_path):
    """見積もりの最悪（規則 1 件あたり、各 root 1 件＋md・rag の層の接尾）が、実際の列挙と一致し、上限ちょうどなら例外にならず、超えれば例外になる。"""
    from sherpa import derived_suffixes as DS
    from sherpa.providers.codex import sandbox
    per = PR.deny_entries_per_rule()
    assert per == 4 + len(DS.BY_LAYER["md"]) + len(DS.BY_LAYER["rag"]) == 10
    n = PR.DENY_MAX_ENTRIES // per                                           # 規則の上限（200 件）
    names = [f"f{i:03d}.docx" for i in range(n)]
    roots = {k: tmp_path / k for k in ("kb", "arch", "md", "rag")}
    for k, root in roots.items():
        root.mkdir()
        for nm in names:
            (root / nm).write_text("x")
            for sfx in DS.BY_LAYER.get(k, ()):
                (root / (nm + sfx)).write_text("x")
    items = PR.validate_entries([{"path": nm, "kind": "exclude", "target_type": "file"} for nm in names])
    assert PR.worst_case_deny_entries(items) == PR.DENY_MAX_ENTRIES
    PR.check_deny_budget(items)
    deny = sandbox._exclude_deny_entries([str(r) for r in roots.values()], PR.Rules(items),
                                         derived_roots={str(roots["md"].resolve()): "md", str(roots["rag"].resolve()): "rag"})
    assert len(deny) == PR.DENY_MAX_ENTRIES
    more = PR.validate_entries([{"path": nm, "kind": "exclude", "target_type": "file"} for nm in [*names, "f999.docx"]])
    with pytest.raises(PR.PathRulesError, match="除外が多すぎます"):
        PR.check_deny_budget(more)
    folders = PR.validate_entries([{"path": f"d{i}", "kind": "exclude", "target_type": "folder"} for i in range(n)])
    PR.check_deny_budget(folders)                                            # folder の規則も file と同じ最悪で数える


def test_pdf_page_images_are_excluded_and_denied_and_suffixes_come_from_the_writers_constants(tmp_path):
    """PDF のページ画像（`{doc_id}.pages/`）も除外・deny される。接尾の一覧は書く側と同じ定数から作る。"""
    from sherpa import derived_suffixes as DS, pdf_page_image
    from sherpa.ingest import office_md
    from sherpa.providers.codex import sandbox
    # 書く側が同じ定数を使う（書く側で名前が変われば、ここで検出される）
    rag = tmp_path / "rag"
    (rag / "ZZdoc").mkdir(parents=True)
    assert pdf_page_image.pages_dir(rag, "ZZdoc/x.pdf").relative_to(rag).as_posix() == "ZZdoc/x.pdf" + DS.PAGES
    assert set(sum(office_md._REL_FILE_SUFFIXES.values(), ())) == set(DS.MD + DS.RAG + DS.IR)
    _roots, names = office_md._publish_targets(tmp_path / "derived" / "md", "ZZdoc/x.pdf")
    assert {n.split("x.pdf", 1)[1] for _l, n in names if n.startswith("ZZdoc/x.pdf")} >= {DS.ASSETS, DS.PAGES, DS.MANIFEST}
    assert set(PR.DERIVED_SUFFIXES) == set(DS.ALL) and DS.PAGES in PR.DERIVED_SUFFIXES and DS.ASSETS in PR.DERIVED_SUFFIXES
    for layer_suffixes in DS.BY_LAYER.values():
        assert set(layer_suffixes) <= set(PR.DERIVED_SUFFIXES)
    # 除外・deny
    rules = PR.Rules(PR.validate_entries([{"path": "ZZdoc/x.pdf", "kind": "exclude", "target_type": "file"}]))
    assert rules.derived_of_excluded("ZZdoc/x.pdf.pages/p1.abc.d150.full.v1.png") and rules.derived_of_excluded("ZZdoc/x.pdf.pages")
    kb = tmp_path / "kb"
    (kb / "ZZdoc").mkdir(parents=True)
    (kb / "ZZdoc" / "x.pdf").write_text("x")
    (rag / "ZZdoc" / "x.pdf.pages").mkdir(parents=True, exist_ok=True)
    (rag / "ZZdoc" / "x.pdf.pages" / "p1.png").write_text("png")
    deny = {p.replace(str(tmp_path.resolve()) + "/", "") for p in sandbox._exclude_deny_entries(
        [str(kb), str(rag)], rules, derived_roots={str(rag.resolve()): "rag"})}
    assert deny == {"kb/ZZdoc/x.pdf", "rag/ZZdoc/x.pdf.pages"}


def test_priority_query_failure_keeps_priority_hits_from_the_normal_result(monkeypatch):
    """優先だけの問い合わせが失敗しても、通常の結果に入っている優先のヒットを捨てない（優先を先に寄せて返す）。"""
    from sherpa import es_index
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_embed_system_settings_snapshot", lambda: None)
    _set(monkeypatch, [{"path": "ZZspec", "kind": "priority", "outside_scope": False}])

    def _req(method, path, body=None, **kw):
        q = json.dumps(body.get("query", {}))
        if '"minimum_should_match": 1' in q and '"ZZspec"' in q:
            raise RuntimeError("503 service unavailable")       # 優先だけの問い合わせが失敗
        hx = [{"_id": "x", "_score": 2.0, "_source": {"doc_id": "x.md", "line": 1, "text": "x", "ext": ".md"}},
              {"_id": "p", "_score": 1.0, "_source": {"doc_id": "ZZspec/p.md", "line": 1, "text": "p", "ext": ".md"}}]
        return {"hits": {"hits": hx[:body.get("size", 99)]}}
    monkeypatch.setattr(es_index, "_req", _req)
    hits, reason = es_index.search(WORLD, "q", k=2, vector=False)
    assert [h["doc_id"] for h in hits] == ["ZZspec/p.md", "x.md"] and reason is None


def _legacy_world(monkeypatch, tmp_path):
    from sherpa import store, worlds
    (tmp_path / "A.docx").mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: tmp_path / "_arch")
    saved = {PR.SETTINGS_KEY: {WORLD: [{"path": "A.docx", "kind": "exclude", "outside_scope": False, "target_type": "folder"}]}}
    state = {"db": saved}

    def _get(**k):
        if state["db"] is None:
            raise OSError("db down")
        return state["db"]
    monkeypatch.setattr(store, "get_system_settings", _get)
    return state


def _replace_dir_with_file(tmp_path):
    import shutil
    shutil.rmtree(tmp_path / "A.docx")
    (tmp_path / "A.docx").write_text("x")


def test_type_cache_expires_from_the_time_it_was_checked_not_the_last_use(monkeypatch, tmp_path):
    """続けて呼んでも（3 秒以内に検索が続いても）、実物を確かめた時刻から数えて期限が切れ、置き換えを検出する（キャッシュを手で消さない）。"""
    _legacy_world(monkeypatch, tmp_path)
    clock = {"t": 100.0}
    monkeypatch.setattr(PR.time, "monotonic", lambda: clock["t"])
    calls = []
    real = PR.detect_target_type
    monkeypatch.setattr(PR, "detect_target_type", lambda w, p: calls.append(p) or real(w, p))
    assert PR.rules_for(WORLD).items[0].target_type == "folder"             # t=100: 実物を確かめる
    _replace_dir_with_file(tmp_path)
    clock["t"] = 102.0
    assert PR.rules_for(WORLD).items[0].target_type == "folder"             # t=102: 3 秒以内＝覚えた判定
    clock["t"] = 104.0
    r = PR.rules_for(WORLD)                                                 # t=104: 確かめた時刻（100）から 4 秒＝再判定
    assert r.items[0].target_type == "file" and r.derived_of_excluded("A.docx.rag.md")
    assert len(calls) == 2


def test_type_is_rechecked_even_when_the_database_cannot_be_read(monkeypatch, tmp_path):
    """DB が読めず最後に読めた写しを使うときも、種別は今の実物で確かめ直す。"""
    state = _legacy_world(monkeypatch, tmp_path)
    clock = {"t": 100.0}
    monkeypatch.setattr(PR.time, "monotonic", lambda: clock["t"])
    assert PR.rules_for(WORLD).items[0].target_type == "folder"
    state["db"] = None
    _replace_dir_with_file(tmp_path)
    clock["t"] = 110.0
    monkeypatch.setattr(PR, "_fail_until", 0.0)
    r = PR.rules_for(WORLD)                                                 # 読めない → 最後に読めた写し（種別は folder で保存）
    assert r.items[0].target_type == "file" and r.derived_of_excluded("A.docx.rag.md")
