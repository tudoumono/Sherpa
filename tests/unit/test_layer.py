"""探す対象（層フィルタ）の単体テスト（`sherpa/layer.py`・
docs/proposals/2026-08-29-調べ方ブロック.md §3.4/§3.5/§8）。

DB/ES/Neo4j 不要（純関数のみ）。
"""
from __future__ import annotations

from sherpa import layer as L
from sherpa.doc_kinds import CODE_EXT


# ===== normalize_layer（既定・不正値・旧回答は both） =====

def test_normalize_layer_valid_values_passthrough():
    assert L.normalize_layer("docs") == "docs"
    assert L.normalize_layer("code") == "code"
    assert L.normalize_layer("both") == "both"


def test_normalize_layer_none_defaults_to_both():
    """§8 裁定論点3/4: 既定省略（`None`）だけが黙って both になる（旧回答/省略の扱い）。"""
    assert L.normalize_layer(None) == "both"


def test_normalize_layer_invalid_internal_value_raises():
    """HTTP 入口は pydantic Literal が防ぐため、None でも docs/code/both でもない
    値が届くのは呼び出し側のバグ——黙って both へ丸めず ValueError にする（fail-loud）。"""
    import pytest
    for bad in ("", "bogus", 123, [], {"x": 1}):
        with pytest.raises(ValueError):
            L.normalize_layer(bad)


def test_normalize_layer_case_and_whitespace_insensitive():
    assert L.normalize_layer(" CODE ") == "code"
    assert L.normalize_layer("Docs") == "docs"


# ===== layer_of / in_layer（CODE_EXT が単一の真実源） =====

def test_layer_of_code_extensions():
    for ext in CODE_EXT:
        assert L.layer_of(f"src/PROG{ext}") == "code", ext


def test_layer_of_non_code_is_docs():
    """決定的MD・直置き .md/.txt・Office/PDF 派生MD（doc_id は元拡張子）はいずれも docs。"""
    for rel in ("設計/仕様.md", "メモ.txt", "report.docx", "資料.pdf", "台帳.xlsx", "資料"):
        assert L.layer_of(rel) == "docs", rel


def test_in_layer_both_is_always_true_regardless_of_extension():
    assert L.in_layer("PROG.cbl", "both") is True
    assert L.in_layer("設計.md", "both") is True
    assert L.in_layer("設計.md", None) is True   # 省略も both 扱い


def test_in_layer_code_matches_only_code_ext():
    assert L.in_layer("PROG.cbl", "code") is True
    assert L.in_layer("設計.md", "code") is False


def test_in_layer_docs_excludes_code_ext():
    assert L.in_layer("設計.md", "docs") is True
    assert L.in_layer("PROG.cbl", "docs") is False


def test_in_layer_invalid_value_raises():
    """不正値は normalize_layer 経由で ValueError（黙って both へ丸めない）。"""
    import pytest
    with pytest.raises(ValueError):
        L.in_layer("PROG.cbl", "nonsense")


# ===== layer_of_code / in_layer_code（accepts() 確定後の bool を受け取る確定判定・
# `layer_of`/`in_layer` の拡張子近似とは独立——CODE-1a の accepts 全滅時の資料落ちに追随する） =====

def test_layer_of_code_true_is_code():
    assert L.layer_of_code(True) == "code"


def test_layer_of_code_false_is_docs():
    """未対応・unreadable も含め、確定 False は一律 docs 側（CODE_EXT membership では判定しない）。"""
    assert L.layer_of_code(False) == "docs"


def test_in_layer_code_both_is_always_true():
    assert L.in_layer_code(True, "both") is True
    assert L.in_layer_code(False, "both") is True
    assert L.in_layer_code(False, None) is True


def test_in_layer_code_matches_confirmed_bool_not_extension():
    """拡張子が CODE_EXT に属していても、確定 False（accepts 全滅の資料落ち）は docs 側に一致する。"""
    assert L.in_layer_code(True, "code") is True
    assert L.in_layer_code(False, "code") is False
    assert L.in_layer_code(False, "docs") is True
    assert L.in_layer_code(True, "docs") is False


def test_in_layer_code_invalid_value_raises():
    import pytest
    with pytest.raises(ValueError):
        L.in_layer_code(True, "nonsense")


# ===== es_filter（ES search()/search_knn_only() 用の filter 節） =====

def test_es_filter_both_is_none_no_filter_added():
    assert L.es_filter("both") is None
    assert L.es_filter(None) is None


def test_es_filter_invalid_value_raises():
    import pytest
    with pytest.raises(ValueError):
        L.es_filter("nonsense")


def test_es_filter_code_is_branch_term():
    """ext membership ではなく確定判定（`branch=="source"`）で絞る（§7 裁定10・grep/agentic と同じ）。"""
    assert L.es_filter("code") == {"term": {"branch": "source"}}


def test_es_filter_docs_is_must_not_branch_term():
    assert L.es_filter("docs") == {"bool": {"must_not": {"term": {"branch": "source"}}}}


# ===== applies_to_lens（§3.5・裁定1: impact/troubleshoot は非適用） =====

def test_applies_to_lens_impact_and_troubleshoot_are_false():
    assert L.applies_to_lens("impact") is False
    assert L.applies_to_lens("troubleshoot") is False


def test_applies_to_lens_qa_and_author_are_true():
    assert L.applies_to_lens("qa") is True
    assert L.applies_to_lens("author") is True


# ===== effective_layer（実検索へ渡す値・非適用レンズは強制 both） =====

def test_effective_layer_returns_requested_value_for_applied_lens():
    assert L.effective_layer({"layer": "code"}, "qa") == "code"
    assert L.effective_layer({"layer": "docs"}, "author") == "docs"


def test_effective_layer_forces_both_for_non_applied_lens():
    """回答メタが layer_applied=False を返すレンズでは、実際の検索も必ず both（要求値を無視）。"""
    assert L.effective_layer({"layer": "code"}, "impact") == "both"
    assert L.effective_layer({"layer": "code"}, "troubleshoot") == "both"


def test_effective_layer_none_scope_meta_is_safe():
    assert L.effective_layer(None, "qa") is None
    assert L.effective_layer(None, "impact") == "both"


# ===== scope_with_layer（chat_service._dispatch / providers.base._agentic_run 共有ヘルパー） =====

def test_scope_with_layer_default_when_scope_meta_is_none():
    sm = L.scope_with_layer(None, world="w1", lens="qa")
    assert sm == {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
                 "layer_applied": True}


def test_scope_with_layer_preserves_existing_fields_and_adds_layer_applied():
    given = {"world": "w1", "scope_paths": ["4期/設計"], "source": "explicit", "layer": "code"}
    sm = L.scope_with_layer(given, world="w1", lens="qa")
    assert sm == {**given, "layer_applied": True}


def test_scope_with_layer_marks_non_applied_for_impact_and_troubleshoot():
    given = {"world": "w1", "scope_paths": [], "source": "all", "layer": "code"}
    assert L.scope_with_layer(given, world="w1", lens="impact")["layer_applied"] is False
    assert L.scope_with_layer(given, world="w1", lens="troubleshoot")["layer_applied"] is False


def test_scope_with_layer_does_not_mutate_input_dict():
    given = {"world": "w1", "scope_paths": [], "source": "all"}
    L.scope_with_layer(given, world="w1", lens="qa")
    assert "layer_applied" not in given   # 呼び出し元の元 dict は変更しない（コピーを返す）
