"""推定トレース（presumed_impact）の単体テスト。LLM/Neo4j 不要（grep と session を stub）。

●確実が0件のとき、業務語を grep → 同文のコード識別子を**実在ノードに裏付け**して「関連の可能性（推定）」に。
コード成果物（Module/Copybook/DataItem/Batch/Table）だけを拾い、一般語/概念ラベル/実在しない語は出さない。
scope/path 同一性を守る（同名が複数世代で曖昧なら任意に繋がない・RV High）。一般語は除外（RV Med）。

secRV 範囲外是正 追補（2026-07-19・RV指摘 MED-1）: `impact_service.run_impact` は presumed_impact
呼び出しを `except Exception: result["presumed"] = []` で広く吸収していたため、overload
（`GraphQueryOverloadError`）まで「関連コードは無かった」（0件）へ潰し、偽陰性になっていた。
`except GraphQueryOverloadError: raise` を広域 except の前に置いた是正を、run_impact 単位で検証する
（overload は伝播・overload 以外は従来どおり best-effort で `[]` に潰れることの両方を固定）。
"""
from __future__ import annotations

import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証
from sherpa import grep_tool, impact_service as I


class _FakeRecord:
    """`neo4j.Record` の最小スタブ（`.data()` のみ使う）。"""
    def __init__(self, d): self._d = d
    def data(self): return dict(self._d)


class _Res:
    # secRV 範囲外是正（2026-07-19）: presumed_impact は `_run_read_capped`（ingest/world_neo4j.py）
    # 経由になり、結果を `.data()` 一括展開ではなく `for record in result` でストリーム反復する
    # ため、`__iter__` で `_FakeRecord`（.data() 持ち）を返す（tests/unit/test_lens_service.py の
    # ストリーム反復版フェイクと同じパターン）。
    def __init__(self, rows): self._rows = rows
    def __iter__(self):
        return iter(_FakeRecord(r) for r in self._rows)


class _Session:
    """presumed_impact が呼ぶ最小 Neo4j session スタブ（ノード行を返すだけ・query は無視）。"""
    def __init__(self, rows): self.rows = rows
    def run(self, q, **kw): return _Res(self.rows)


class _RaisingSession:
    """`_run_read_capped` が捕捉/変換すべき Neo4jError を送出するスタブ（secRV 範囲外是正・2026-07-19）。"""
    def __init__(self, exc): self._exc = exc
    def run(self, q, **kw): raise self._exc


def _n(name, label, top="src", path=None):
    return {"name": name, "label": label, "cid": f"{label.lower()}:{top}:{name}",
            "path": path or f"{top}/{name}", "top": top}


_NODES = [_n("TAXCALC", "Module"), _n("TAX-RATE", "DataItem"), _n("TAXRATE", "Copybook"),
          _n("NIGHTLY", "Batch"), _n("消費税率", "Parameter", top="設計"), _n("FOO-BIZ", "Function", top="設計")]


def _stub_grep(hits):
    o = grep_tool.grep_search
    grep_tool.grep_search = lambda term, world, scope_paths=None, max_hits=30, truncated_docs=None: hits
    return o


def test_presumed_grounds_code_only():
    o = _stub_grep([{"doc_id": "設計/消費税.md", "line": 3,
                     "text": "# 消費税\n消費税率について。TAXCALC が担当し、TAXRATE を取り込み TAX-RATE を使用。"
                             "夜間 NIGHTLY で実行。FOO-BIZ や UNKNOWN-XYZ や DATA も記載。"}])
    try:
        out = I.presumed_impact(_Session(_NODES), "消費税率", "w")
        got = {p["name"] for p in out}
        # コード成果物だけ。Function(FOO-BIZ)/Parameter/実在しない語/一般語(DATA) は除外
        assert got == {"TAXCALC", "TAX-RATE", "TAXRATE", "NIGHTLY"}, got
        assert all(p["judgement"] == "presumed" and p.get("path") for p in out)   # 同一性(path)を保持
        ev = next(p for p in out if p["name"] == "TAX-RATE")["evidence"][0]
        assert ev["doc"] == "設計/消費税.md" and "TAX-RATE" in ev["quote"]   # 根拠（doc＋引用）つき
    finally:
        grep_tool.grep_search = o


def test_presumed_empty_when_no_hits_or_no_code():
    o = _stub_grep([])
    try:
        assert I.presumed_impact(_Session(_NODES), "未知語", "w") == []     # grep 0件
    finally:
        grep_tool.grep_search = o
    o = _stub_grep([{"doc_id": "x.md", "line": 1, "text": "消費税率の説明。コード参照なし。"}])
    try:
        assert I.presumed_impact(_Session(_NODES), "消費税率", "w") == []   # コード識別子の裏付け無し
        assert I.presumed_impact(_Session([_n("消費税率", "Parameter")]), "消費税率", "w") == []  # コードノード無し
    finally:
        grep_tool.grep_search = o


def test_presumed_skips_ambiguous_cross_generation():
    """RV High: 同名コードが複数世代にある時、文書世代と一致すればその世代に・一致しなければ曖昧で繋がない。"""
    nodes = [_n("TAX-RATE", "DataItem", top="4期"), _n("TAX-RATE", "DataItem", top="5期")]
    # 文書世代が一致しない（共通フォルダ）→ 曖昧で出さない
    o = _stub_grep([{"doc_id": "共通/メモ.md", "line": 1, "text": "TAX-RATE に関する共通メモ"}])
    try:
        assert I.presumed_impact(_Session(nodes), "税率", "w") == []
    finally:
        grep_tool.grep_search = o
    # 文書世代が 4期 → 4期の TAX-RATE だけに紐付く（別世代へ誤爆しない）
    o = _stub_grep([{"doc_id": "4期/設計/税.md", "line": 1, "text": "TAX-RATE を参照"}])
    try:
        out = I.presumed_impact(_Session(nodes), "税率", "w")
        assert len(out) == 1 and out[0]["top_scope"] == "4期"
    finally:
        grep_tool.grep_search = o


def test_presumed_dedup_and_cap():
    hits = [{"doc_id": "a.md", "line": 1, "text": "TAXCALC TAXCALC TAX-RATE"},
            {"doc_id": "b.md", "line": 2, "text": "TAXCALC また TAX-RATE"}]
    o = _stub_grep(hits)
    try:
        out = I.presumed_impact(_Session(_NODES), "税", "w")
        names = [p["name"] for p in out]
        assert names.count("TAXCALC") == 1 and names.count("TAX-RATE") == 1   # ノード単位で重複排除
    finally:
        grep_tool.grep_search = o


def test_presumed_impact_propagates_neo4j_overload_fail_loud():
    """secRV 範囲外是正（2026-07-19）: presumed_impact のノード取得クエリも `_run_read_capped`
    経由になり、timeout は空へ黙って縮退せず `GraphQueryOverloadError` を raise する（fail-loud）。
    このケース自体は presumed_impact 単体として縮退せず伝播することを固定する（`run_impact` 側での
    扱い＝re-raise か best-effort での吸収かは MED-1 是正（下記）以降 `except Exception` の**前**に
    `except GraphQueryOverloadError: raise` があるため re-raise される・下の
    `test_run_impact_propagates_presumed_overload_fail_loud` を参照）。
    """
    from neo4j.exceptions import Neo4jError

    from sherpa.ingest.world_neo4j import GraphQueryOverloadError
    exc = Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", message="timed out")
    o = _stub_grep([{"doc_id": "設計/消費税.md", "line": 1, "text": "TAXCALC が担当"}])
    try:
        import pytest
        with pytest.raises(GraphQueryOverloadError):
            I.presumed_impact(_RaisingSession(exc), "消費税率", "w")
    finally:
        grep_tool.grep_search = o


# ---- MED-1（secRV 範囲外是正 追補・2026-07-19）: run_impact 側の broad except が overload を握り潰さない ----

def _empty_world_impact(*_a, **_kw):
    """`run_world_impact` のフェイク（●確実 items=0 件・presumed 分岐へ進ませる）。"""
    return {"type": "impact", "world_id": "w", "scope_prefixes": [], "start": "税",
            "include_deprecated": False, "starts": [], "items": []}


def test_run_impact_propagates_presumed_overload_fail_loud(monkeypatch):
    """presumed_impact が `GraphQueryOverloadError` を raise すると、`run_impact` の広域
    `except Exception` に握り潰されず（[] に潰れず）そのまま伝播する。潰すと「関連コードは
    見つからなかった」（0件）と「調べられなかった」（安全弁で打ち切り）の区別がつかない偽陰性になる。
    """
    import pytest

    from sherpa.ingest import world_neo4j as wn
    monkeypatch.setattr(wn, "run_world_impact", _empty_world_impact)

    def _boom(*_a, **_kw):
        raise wn.GraphQueryOverloadError("timeout", world="w")
    monkeypatch.setattr(I, "presumed_impact", _boom)

    with pytest.raises(wn.GraphQueryOverloadError):
        I.run_impact(object(), "税", "w")


def test_run_impact_still_absorbs_general_presumed_exception_to_empty_list(monkeypatch):
    """回帰: overload 以外の一般例外は従来どおり best-effort で `presumed=[]` に潰す
    （MED-1 是正は overload だけを例外にする＝既存の best-effort 挙動自体は維持）。"""
    from sherpa.ingest import world_neo4j as wn
    monkeypatch.setattr(wn, "run_world_impact", _empty_world_impact)

    def _boom(*_a, **_kw):
        raise RuntimeError("unrelated bug")
    monkeypatch.setattr(I, "presumed_impact", _boom)

    result = I.run_impact(object(), "税", "w")
    assert result["presumed"] == []


# ---- grep 打切りの平文申告（`truncated_docs` out-param → `notes`・非エージェント経路） ----
# `grep_tool.grep_search(truncated_docs=...)` はヒット0件でも打切りを申告できる「本命」経路。
# `presumed_impact` の grep（●確実0件時の推定トレース）が打ち切られたら、`run_impact` の出力へ
# 平文の注記を加算する（`lens_service.run_qa`/`run_troubleshoot` と同じ流儀）。

def test_presumed_impact_forwards_truncated_docs_out_param():
    """`presumed_impact` は `truncated_docs`（省略可の out-param）をそのまま grep_search へ転送する。"""
    def fake_grep(term, world, scope_paths=None, max_hits=30, truncated_docs=None):
        if truncated_docs is not None:
            truncated_docs.append("大きい資料.md")
        return []
    o = grep_tool.grep_search
    grep_tool.grep_search = fake_grep
    try:
        out_list: list = []
        I.presumed_impact(_Session(_NODES), "税", "w", truncated_docs=out_list)
        assert out_list == ["大きい資料.md"]
    finally:
        grep_tool.grep_search = o


def test_run_impact_truncated_docs_becomes_plain_note(monkeypatch):
    """presumed_impact の grep が打ち切った文書があれば、平文の注記（`notes`）が run_impact の
    出力へ加算される（内部語彙 file_truncated/cap/バイトは出さない）。"""
    from sherpa.ingest import world_neo4j as wn
    monkeypatch.setattr(wn, "run_world_impact", _empty_world_impact)

    def fake_presumed(session, term, world, scope_prefixes=None, truncated_docs=None):
        if truncated_docs is not None:
            truncated_docs.append("大きい資料.md")
        return []
    monkeypatch.setattr(I, "presumed_impact", fake_presumed)

    result = I.run_impact(object(), "税", "w")
    assert result["notes"] == ["「大きい資料.md」は大きすぎて全体を検索できていません（先頭部分のみ）。"]
    for forbidden in ("file_truncated", "cap", "バイト", "byte"):
        assert forbidden not in result["notes"][0]


def test_run_impact_no_truncation_output_unchanged(monkeypatch):
    """打切りが無いとき、run_impact の出力は `notes` キーを持たない従来どおりの形（加算的変更）。"""
    from sherpa.ingest import world_neo4j as wn
    monkeypatch.setattr(wn, "run_world_impact", _empty_world_impact)
    monkeypatch.setattr(I, "presumed_impact", lambda *a, **k: [])

    result = I.run_impact(object(), "税", "w")
    assert "notes" not in result
    assert result["presumed"] == []


# ---- SHERPA_IMPACT_MAX_DEPTH ----
# import 時に一度だけ確定する定数は実プロセスを新規に起こして検証する（`_fresh_import`）。
# （`ingest.world_neo4j.IMPACT_MAX_DEPTH` と同じ env を共用・循環 import のため定数は複製）。

def test_impact_max_depth_fresh_import_env_unset_is_default():
    assert FI.fresh_import_attr("sherpa.impact_service", "IMPACT_MAX_DEPTH",
                                env={"SHERPA_IMPACT_MAX_DEPTH": None}) == 8


def test_impact_max_depth_fresh_import_env_valid_value():
    assert FI.fresh_import_attr("sherpa.impact_service", "IMPACT_MAX_DEPTH",
                                env={"SHERPA_IMPACT_MAX_DEPTH": "12"}) == 12


def test_impact_max_depth_fresh_import_env_invalid_falls_back_to_default():
    for bad in ("0", "65", "abc"):
        assert FI.fresh_import_attr("sherpa.impact_service", "IMPACT_MAX_DEPTH",
                                    env={"SHERPA_IMPACT_MAX_DEPTH": bad}) == 8, bad


def test_impact_max_depth_env_change_after_import_has_no_effect(monkeypatch):
    before = I.IMPACT_MAX_DEPTH
    monkeypatch.setenv("SHERPA_IMPACT_MAX_DEPTH", "40")
    assert I.IMPACT_MAX_DEPTH == before == 8


def test_run_impact_default_depth_param_is_impact_max_depth():
    """既定値と異なる env（20）で fresh import し、`run_impact` の `depth` 既定値が実際に
    `IMPACT_MAX_DEPTH` を参照していることを確認する（旧リテラル `depth=8` への退行を検出できる形）。"""
    assert FI.fresh_import_param_default(
        "sherpa.impact_service", "run_impact", "depth",
        env={"SHERPA_IMPACT_MAX_DEPTH": "20"}) == 20
