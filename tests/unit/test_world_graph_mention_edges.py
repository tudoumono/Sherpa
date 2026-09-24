"""S2（辞書突合→言及エッジ）の単体テスト（2026-09-04-グラフのソース正典化.md §2・K3〜K5）。

Pass1 の定義索引を辞書として、資料文書（`branch=="office"`）の本文と決定的に突合し
`Document -DOCUMENTS(via="mention")-> コード` を張る `world_graph._mention_pass` を、
Java/COBOL に依存しない最小のフェイクアナライザで固定する
（`tests/unit/test_world_graph_edge_extra.py` と同じ流儀）。
"""
from __future__ import annotations

import pathlib

from sherpa import lens_service
from sherpa.ingest import world_graph
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import Analyzer, DefItem, DefResult, RefResult


class _FakeAnalyzer(Analyzer):
    """Pass1 の定義（`DefItem`）だけを rel ごとに固定で返す最小フェイク（`.fk` 拡張子担当・参照は無し）。"""

    name = "fake"
    extensions = frozenset({".fk"})
    doctype = "fake"

    def __init__(self, defs_by_rel: dict):
        self._defs = defs_by_rel

    def collect_defs(self, text, rel_path):
        return self._defs.get(rel_path, DefResult())

    def extract_refs(self, text, rel_path):
        return RefResult()


def _def(label, name):
    return DefResult(primary=DefItem(label=label, name=name))


def _def_with_child(primary_label, primary_name, child_label, child_name, cid_key):
    """主体1件＋修飾名（`cid_key`）を持つ子定義1件（コピーブック子項目型・S2-LEAFNAME）。"""
    return DefResult(primary=DefItem(label=primary_label, name=primary_name),
                     children=[DefItem(label=child_label, name=child_name, cid_key=cid_key, line=1)])


def _world(tmp_path, files: dict):
    wd = tmp_path / "world"
    wd.mkdir()
    for rel, content in files.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return wd


def _mention_edges(edges):
    return [e for e in edges if e["type"] == "DOCUMENTS"]


# ---- トークナイザ（純関数） -----------------------------------------------------------------

def test_mention_tokenize_containment_boundary_hyphen_and_japanese_mix():
    """`[A-Za-z0-9_-]+` の最大連続——包含（XXX_A/XXX_AA）は別トークンに分かれ部分文字列一致は
    構造的に起きない（K4①）。ハイフンは1トークンを構成し、日本語は自然な境界になる。"""
    text = "XXX_A と XXX_AA と TAX-RATE税率（消費税）を扱う。"
    toks = world_graph._mention_tokenize(text)
    assert "XXX_A" in toks
    assert "XXX_AA" in toks
    assert "TAX-RATE" in toks                    # ハイフン込みで1トークン
    assert "TAX" not in toks                     # 部分文字列は独立トークンとして出ない（誤爆しない）
    assert not any("税率" in t for t in toks)     # 日本語文字はトークン化されない

    # 重複トークンは初出順で1つにまとめる（探索・上限判定を軽くするだけ）。
    toks2 = world_graph._mention_tokenize("AAA BBB AAA")
    assert toks2 == ["AAA", "BBB"]


def test_mention_tokenize_includes_cobol_identifier_chars_hash_at_dollar():
    """トークン文字集合はアナライザの COBOL 識別子文字集合（`static_analysis._PROGRAM_ID` 等の
    `[A-Z0-9#@$-]`）と揃える（rv-s2-mention #3）——`#`/`@`/`$` を含む識別子も1トークンとして
    扱う。以前は `BILL@01` が `BILL`/`01` に分割され、無関係な定義（`BILL`）へ誤って言及
    リンクしていた。"""
    toks = world_graph._mention_tokenize("この文書は BILL@01 を扱う。")
    assert "BILL@01" in toks
    assert "BILL" not in toks
    assert "01" not in toks


def test_mention_edge_uses_full_token_and_does_not_link_to_unrelated_prefix(tmp_path, monkeypatch):
    """`BILL@01` という定義名は1トークンとして辞書に載り、別定義 `BILL` への誤リンクを起こさない
    （rv-s2-mention #3・トークン文字集合の是正）。"""
    wd = _world(tmp_path, {
        "g1/a.fk": "x", "g1/b.fk": "y",
        "g1/note.md": "この文書は BILL@01 を扱う。",
    })
    defs = {"g1/a.fk": _def("Module", "BILL@01"), "g1/b.fk": _def("Module", "BILL")}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    _, edges, _ = world_graph.build_world(wd, "w")
    dsts = {e["dst"] for e in _mention_edges(edges)}
    assert dsts == {world_graph._cid("Module", "w", "g1/a.fk", "BILL@01")}
    assert world_graph._cid("Module", "w", "g1/b.fk", "BILL") not in dsts


# ---- 長さ下限 --------------------------------------------------------------------------------

def test_min_len_filters_short_names_env_adjustable(tmp_path, monkeypatch):
    wd = _world(tmp_path, {"g1/a.fk": "x", "g1/note.md": "この文書は ABC に言及する。"})
    defs = {"g1/a.fk": _def("Module", "ABC")}    # 3文字＝既定4文字未満
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    _, edges, _ = world_graph.build_world(wd, "w")
    assert _mention_edges(edges) == []

    monkeypatch.setenv("SHERPA_MENTION_MIN_LEN", "3")
    _, edges, _ = world_graph.build_world(wd, "w")
    doc_edges = _mention_edges(edges)
    assert len(doc_edges) == 1
    assert doc_edges[0]["via"] == "mention"


def test_dictionary_excludes_short_names_before_document_scan_is_skipped(tmp_path, monkeypatch):
    """rv-s2-mention #4: 長さ下限未満の名前は**辞書構築の時点で**除外される（以前は文書側の
    トークンを都度足切りするだけで、辞書自体には短名が残っていた）——定義が全て短名だけの
    world は辞書が空になり、文書走査自体が省かれる（読めない資料があっても `unreadable_mention_doc`
    すら申告されない＝走査に入っていない証拠）。"""
    wd = _world(tmp_path, {"g1/a.fk": "x", "g1/unreadable.md": "ABC に言及する。"})
    defs = {"g1/a.fk": _def("Module", "ABC")}    # 3文字＝既定4文字未満
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    real_read_text = pathlib.Path.read_text

    def _boom_read_text(self, *a, **kw):          # 呼ばれたら「走査に入ってしまった」証拠として失敗させる
        if self.name == "unreadable.md":
            raise OSError("boom")
        return real_read_text(self, *a, **kw)
    monkeypatch.setattr(pathlib.Path, "read_text", _boom_read_text)

    _, edges, flags = world_graph.build_world(wd, "w")
    assert _mention_edges(edges) == []
    assert flags == []                            # unreadable_mention_doc も出ない＝走査が省かれた


def test_ambiguous_alias_count_excludes_names_below_min_len(tmp_path, monkeypatch):
    """rv-s2-mention #4: 長さ下限未満の別名（表示名）は辞書構築時点で除外されるため、同世代衝突が
    あっても `mention_ambiguous_names` へ数えない（突合し得ない名前まで誤ってカウントしていた
    穴の是正）。"""
    wd = _world(tmp_path, {
        "g1/a.fk": "x", "g1/b.fk": "y",
        "g1/note.md": "この文書は AB という項目を扱う。",
    })
    defs = {
        "g1/a.fk": _def_with_child("Copybook", "GROUPA", "DataItem", "AB", "GROUPA.AB"),
        "g1/b.fk": _def_with_child("Copybook", "GROUPB", "DataItem", "AB", "GROUPB.AB"),
    }   # 表示名 "AB" は2文字＝既定4文字未満・同世代で衝突
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    _, edges, flags = world_graph.build_world(wd, "w")
    assert _mention_edges(edges) == []
    assert not [f for f in flags if f.get("reason") == "mention_ambiguous_names"]


# ---- 修飾名を持つ子定義は単純名（表示名）でも突合できる（S2-LEAFNAME） --------------------------

def test_qualified_child_matched_via_simple_leaf_name(tmp_path, monkeypatch):
    """`cid_key`（修飾名・例 `GROUP.LEAFNAME`）を持つ子定義は、トークナイザが `.` で分断するため
    修飾名そのものは辞書突合が構造的に一致しえない——資料文書に書かれた単純名（表示名）
    `LEAFNAME` でも突合できることを固定する（2026-09-05 調査で実証されたバグの修正）。
    dst cid はノードの実際の識別子（修飾名）で組み立てる——トークン文字列をそのまま使わない。
    """
    wd = _world(tmp_path, {
        "g1/a.fk": "x",
        "g1/note.md": "この文書は LEAFNAME 項目を扱う。",
    })
    defs = {"g1/a.fk": _def_with_child("Copybook", "GROUP", "DataItem", "LEAFNAME", "GROUP.LEAFNAME")}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    _, edges, flags = world_graph.build_world(wd, "w")
    doc_edges = _mention_edges(edges)
    assert len(doc_edges) == 1
    assert doc_edges[0]["dst"] == world_graph._cid("DataItem", "w", "g1/a.fk", "GROUP.LEAFNAME")
    assert not [f for f in flags if f.get("reason") == "mention_ambiguous_names"]


def test_leaf_name_collision_in_same_generation_is_ambiguous_and_flagged(tmp_path, monkeypatch):
    """同一世代（top_scope）内で単純名（表示名）が衝突する2つの修飾子付き子定義は、単純名としては
    曖昧＝張らない（既存の曖昧規律と同じ流儀）——かつ world 単位で1件 `mention_ambiguous_names`
    を申告する（実測目的・裁定の点2）。"""
    wd = _world(tmp_path, {
        "g1/a.fk": "x", "g1/b.fk": "y",
        "g1/note.md": "この文書は LEAFNAME 項目を扱う。",
    })
    defs = {
        "g1/a.fk": _def_with_child("Copybook", "GROUPA", "DataItem", "LEAFNAME", "GROUPA.LEAFNAME"),
        "g1/b.fk": _def_with_child("Copybook", "GROUPB", "DataItem", "LEAFNAME", "GROUPB.LEAFNAME"),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    _, edges, flags = world_graph.build_world(wd, "w")
    assert _mention_edges(edges) == []                 # 単純名が同世代で衝突＝任意選択しない
    assert {"reason": "mention_ambiguous_names", "count": 1} in flags


def test_duplicate_dst_from_qualified_and_simple_alias_collapses_to_one_edge():
    """同一定義を指す辞書エントリが（修飾キー／単純名の）異なる名前で複数存在しても、
    同一文書内の同一 (doc,dst) の重複エッジは1本にまとめる（裁定の点1・防御的規律）。"""
    nodes: dict = {}
    edges: list = []
    flags: list = []
    mdict = {
        "LEAFNAME": [("DataItem", "g1/a.fk", "GROUP.LEAFNAME")],
        "GROUPALIAS": [("DataItem", "g1/a.fk", "GROUP.LEAFNAME")],   # 同じ定義を指す別名エントリ
    }
    text = "この文書は LEAFNAME と GROUPALIAS の両方に言及する。"
    world_graph._mention_edges_for_doc("g1/note.md", text, mdict, min_len=4, max_per_doc=200,
                                       world_id="w", nodes=nodes, edges=edges, flags=flags)
    doc_edges = _mention_edges(edges)
    assert len(doc_edges) == 1
    assert doc_edges[0]["dst"] == world_graph._cid("DataItem", "w", "g1/a.fk", "GROUP.LEAFNAME")


# ---- 同一世代内の同名重複＝曖昧 ---------------------------------------------------------------

def test_same_generation_duplicate_name_is_ambiguous_and_not_linked(tmp_path, monkeypatch):
    wd = _world(tmp_path, {
        "g1/a.fk": "x", "g1/b.fk": "y",
        "g1/note.md": "この文書は SOMENAME に言及する。",
    })
    defs = {"g1/a.fk": _def("Module", "SOMENAME"), "g1/b.fk": _def("Batch", "SOMENAME")}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    _, edges, flags = world_graph.build_world(wd, "w")
    assert _mention_edges(edges) == []            # 同世代に2定義＝曖昧・任意選択しない
    assert not [f for f in flags if f.get("reason") == "mention_overflow"]


# ---- 別世代の同名＝全世代へ張る（K5） ----------------------------------------------------------

def test_different_generation_same_name_links_to_all_generations(tmp_path, monkeypatch):
    wd = _world(tmp_path, {
        "g1/a.fk": "x", "g2/a.fk": "x",
        "g1/note.md": "この文書は SOMENAME に言及する。",
    })
    defs = {"g1/a.fk": _def("Module", "SOMENAME"), "g2/a.fk": _def("Module", "SOMENAME")}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    _, edges, flags = world_graph.build_world(wd, "w")
    doc_edges = _mention_edges(edges)
    assert flags == []
    dsts = {e["dst"] for e in doc_edges}
    assert dsts == {world_graph._cid("Module", "w", "g1/a.fk", "SOMENAME"),
                    world_graph._cid("Module", "w", "g2/a.fk", "SOMENAME")}
    src_cid = world_graph._document_cid("w", "g1/note.md")
    for e in doc_edges:
        assert e["src"] == src_cid
        assert e["via"] == "mention"
        assert e["extraction_method"] == "static"
        assert e["status"] == "active"
        assert e["doc"] == "g1/note.md"


# ---- 1文書あたりの言及エッジ上限 ---------------------------------------------------------------

def test_mention_overflow_caps_edges_and_flags_the_excess(tmp_path, monkeypatch):
    wd = _world(tmp_path, {
        "g1/a.fk": "x", "g1/b.fk": "y",
        "g1/note.md": "この文書は NAME-ONE と NAME-TWO に言及する。",
    })
    defs = {"g1/a.fk": _def("Module", "NAME-ONE"), "g1/b.fk": _def("Module", "NAME-TWO")}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))
    monkeypatch.setenv("SHERPA_MENTION_MAX_PER_DOC", "1")

    _, edges, flags = world_graph.build_world(wd, "w")
    doc_edges = _mention_edges(edges)
    assert len(doc_edges) == 1
    assert {"reason": "mention_overflow", "doc": "g1/note.md", "count": 1} in flags


# ---- 読めない資料文書はスキップ＋申告 -----------------------------------------------------------

def test_unreadable_resource_doc_is_skipped_and_flagged(tmp_path, monkeypatch):
    wd = _world(tmp_path, {
        "g1/a.fk": "x",
        "g1/unreadable.md": "この文書は SOMENAME に言及する。",
    })
    defs = {"g1/a.fk": _def("Module", "SOMENAME")}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    real_read_text = pathlib.Path.read_text

    def _boom_read_text(self, *a, **kw):          # 対象ファイル限定（他の読み取りは通常どおり）
        if self.name == "unreadable.md":
            raise OSError("boom")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", _boom_read_text)

    _, edges, flags = world_graph.build_world(wd, "w")
    assert _mention_edges(edges) == []
    assert {"reason": "unreadable_mention_doc", "doc": "g1/unreadable.md"} in flags


# ---- ソース原文は突合対象外 --------------------------------------------------------------------

def test_source_branch_code_files_are_not_scanned_for_mentions(tmp_path, monkeypatch):
    """`branch=="source"`（コード自身）は資料ではない＝辞書突合の対象外（裁定5）。
    コードファイルが自分自身/他コードの定義名を字面に含んでいても言及エッジは張らない。"""
    wd = _world(tmp_path, {"g1/a.fk": "SOMENAME", "g1/b.fk": "SOMENAME を呼ぶ"})
    defs = {"g1/a.fk": _def("Module", "SOMENAME")}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    _, edges, _ = world_graph.build_world(wd, "w")
    assert _mention_edges(edges) == []


# ---- 副次確認: DOCUMENTS は troubleshoot 近傍（_RELATED_REL）に既に載る -------------------------

def test_documents_edge_type_is_included_in_troubleshoot_related_rel():
    """言及エッジ（`type=="DOCUMENTS"`）は `lens_service._RELATED_REL` に既に含まれる——
    トラブルシュート近傍へ自動で載ることをここで固定する（Neo4j 実体は integration 側の担当）。"""
    assert "DOCUMENTS" in lens_service._RELATED_REL


def test_mention_edge_is_documents_type_reachable_via_related_rel(tmp_path, monkeypatch):
    wd = _world(tmp_path, {"g1/a.fk": "x", "g1/note.md": "この文書は SOMENAME に言及する。"})
    defs = {"g1/a.fk": _def("Module", "SOMENAME")}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    _, edges, _ = world_graph.build_world(wd, "w")
    doc_edges = _mention_edges(edges)
    assert len(doc_edges) == 1
    assert doc_edges[0]["type"] in lens_service._RELATED_REL


# ---- 言及エッジの実効設定は world 署名の材料に含まれる（rv-s2-mention #2） ------------------------

def test_world_signature_changes_with_mention_min_len_and_max_per_doc(monkeypatch, tmp_path):
    """`SHERPA_MENTION_MIN_LEN`/`SHERPA_MENTION_MAX_PER_DOC` の実効値は `worker.world_signature`
    （`worker._sig` の材料）に含まれる——env を変えると、ソースファイル自体が不変でも署名が
    変わる。旧実装は `world_graph.MENTION_SCHEMA_VERSION`（仕様版）だけを材料にしており、
    設定変更後も既存 world の言及エッジが旧しきい値のまま素通りしていた。"""
    from sherpa.ingest import worker
    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "a.md").write_text("x", encoding="utf-8")

    sig_default = worker.world_signature_of_root(wd)

    monkeypatch.setenv("SHERPA_MENTION_MIN_LEN", "6")
    sig_min_len = worker.world_signature_of_root(wd)
    assert sig_min_len != sig_default
    monkeypatch.delenv("SHERPA_MENTION_MIN_LEN", raising=False)
    assert worker.world_signature_of_root(wd) == sig_default, "既定値へ戻せば署名も再現する"

    monkeypatch.setenv("SHERPA_MENTION_MAX_PER_DOC", "50")
    sig_max_per_doc = worker.world_signature_of_root(wd)
    assert sig_max_per_doc != sig_default
    monkeypatch.delenv("SHERPA_MENTION_MAX_PER_DOC", raising=False)
    assert worker.world_signature_of_root(wd) == sig_default
