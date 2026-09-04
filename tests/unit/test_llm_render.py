"""L5（rag.md の LLM 成形＋規則フォールバック・§8.3/§8.6・
`docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md`）の単体テスト。

実 LLM 呼び出しは一切発生しない（`graph_extract.complete_json`/`available` を monkeypatch）。
`SHERPA_KB_DIR`/`SHERPA_DERIVED_DIR` を `tmp_path` へ隔離し、共有 `data/derived` を書き換えない。
"""
from __future__ import annotations

import contextlib
import json

import pytest

from sherpa import json_io, llm, metering, store, worlds
from sherpa.ingest import graph_extract, llm_render
from sherpa.store import usage_events as ue


@contextlib.contextmanager
def _noop_lock(world_id):
    yield


def _isolate(monkeypatch, tmp_path, world: str = "v1") -> str:
    monkeypatch.setenv("SHERPA_KB_DIR", str(tmp_path / "kb"))
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    monkeypatch.setattr(store, "get_world", lambda world_id: None)
    # 既定 OFF（2026-09-05 裁定）のため、run_world_pass 系のテストはトグルを明示 ON にして実行する。
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": "on"})
    # rv-oom-resume item5（2026-09-05）: run_world_pass は書込直前に世代を再照合するため
    # store.world_lock を通す（`test_worker_sig_discipline.py` と同じ流儀で DB 非依存の no-op に
    # 差し替える＝unit テストは外部サービス不要という契約を保つ）。
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    return world


def _build_markdown(records: list[tuple]) -> str:
    """`evidence_render._markdown()` と同じ組み立てアルゴリズムでテスト用 rag.md を作る
    （`chunk_id, section, key_heading, body, region` のタプル列）。`_split_records` が
    アンカー間の見出し類（chrome）と本文（body）を正しく切り分けられることの検証土台。
    """
    lines = ["# AI検索用文書", "", "原本: a.xlsx", "変換プロファイル: p1 / evidence-rag-renderer-v1alpha9", ""]
    last_section = None
    for chunk_id, section, key_heading, body, region in records:
        lines.append(f"<!-- chunk:{chunk_id} -->")
        if section != last_section:
            lines.extend(["## " + " / ".join(section), ""])
            if region:
                lines.extend([f"原本領域: {region}", ""])
            last_section = section
        if key_heading:
            lines.extend([f"### {key_heading}", ""])
        lines.extend([body, ""])
    return "\n".join(lines).rstrip() + "\n"


# ---- トグル解決（system_settings > 既定 off・2026-09-05 裁定＝実測で文体整形のみと判明） ------

def test_toggle_default_off_when_unset(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})
    assert llm_render.rag_llm_render_enabled() is False


def test_toggle_system_settings_on(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": "on"})
    assert llm_render.rag_llm_render_enabled() is True


def test_toggle_legacy_boolean_values_are_honored(monkeypatch):
    # 旧版が書いた boolean（dev 実DBで実際に観測）を黙って無視して既定へ倒さない＝意図を保持する。
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": True})
    assert llm_render.rag_llm_render_enabled() is True
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": False})
    assert llm_render.rag_llm_render_enabled() is False


def test_toggle_system_settings_off(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": "off"})
    assert llm_render.rag_llm_render_enabled() is False


def test_toggle_unknown_value_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": "maybe"})
    assert llm_render.rag_llm_render_enabled() is False


def test_toggle_system_settings_read_failure_falls_back_to_default(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "get_system_settings", _boom)
    assert llm_render.rag_llm_render_enabled() is False


def test_env_default_enabled_ignores_system_settings(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": "on"})
    assert llm_render.env_default_enabled() is False


# ---- LLM 設定解決（graph_extract の再利用） -------------------------------------------------

def test_available_delegates_to_graph_extract(monkeypatch):
    """L5 残課題の是正: `usage="render"` を渡す（model_catalog の render セル・未設定なら
    extract の解決結果へ自動フォールバック＝`model_catalog._USAGE_FALLBACK`）。"""
    calls = []

    def _fake_available(settings, strict=False, usage="extract"):
        calls.append((settings, strict, usage))
        return {"provider": "openai", "model": "gpt-5.5"}

    monkeypatch.setattr(graph_extract, "available", _fake_available)
    cfg = llm_render.available({"x": 1})
    assert cfg == {"provider": "openai", "model": "gpt-5.5"}
    assert calls == [({"x": 1}, False, "render")]


def test_available_returns_none_on_invalid_cloud_provider_config(monkeypatch):
    from sherpa import keys as _keys

    def _boom(settings, strict=False, usage="extract"):
        raise _keys.InvalidCloudProviderConfigError("bad")

    monkeypatch.setattr(graph_extract, "available", _boom)
    assert llm_render.available() is None


# ---- 生成手段スタンプ ------------------------------------------------------------------------

def test_stamp_rule_only_inserts_after_profile_line():
    md = "# AI検索用文書\n\n原本: a.xlsx\n変換プロファイル: p1 / v1\n抽出範囲: ok=1\n\n<!-- chunk:c1 -->\n本文\n"
    stamped = llm_render.stamp_rule_only(md)
    lines = stamped.splitlines()
    assert lines[lines.index("変換プロファイル: p1 / v1") + 1] == "生成手段: 規則"


def test_stamp_rule_only_fail_safe_when_profile_line_missing():
    md = "何か想定外の形式\n<!-- chunk:c1 -->\n本文\n"
    stamped = llm_render.stamp_rule_only(md)
    assert stamped.startswith("生成手段: 規則\n")
    assert md in stamped


def test_needs_llm_pass_true_for_rule_only():
    md = llm_render.stamp_rule_only("変換プロファイル: p1 / v1\n\n<!-- chunk:c1 -->\n本文\n")
    assert llm_render.needs_llm_pass(md) is True


def test_needs_llm_pass_false_once_settled():
    md = "生成手段: LLM（openai/gpt-5.5）＋規則（LLM成形 1 件）\n<!-- chunk:c1 -->\n本文\n"
    assert llm_render.needs_llm_pass(md) is False


def test_needs_llm_pass_true_when_line_absent():
    """想定外に生成手段行が無い（旧世代等）＝fail-open で成形対象に含める。"""
    assert llm_render.needs_llm_pass("<!-- chunk:c1 -->\n本文\n") is True


# ---- レコード分割（アンカー間 chrome/body の切り分け・往復再構成の安全網） ------------------------

def test_split_records_round_trip_single_record():
    md = _build_markdown([
        ("c1", ("文書「a」",), None, "出所: 原本「a.xlsx」\n本文: 「あ」", None),
    ])
    header, records = llm_render._split_records(md)
    assert len(records) == 1
    assert records[0]["body"] == "出所: 原本「a.xlsx」\n本文: 「あ」"
    rebuilt = header + "".join(
        r["anchor"] + r["chrome"] + r["body"] + r["trailing"] for r in records)
    assert rebuilt == md


def test_split_records_round_trip_multi_record_shared_section():
    """同一セクション内の2件目は見出しを再出力しない（`chrome` が短くなる）が、body の切り分けは
    引き続き正しい——シート先頭の1件目は原本領域行の後始末込みで chrome へ吸収される。"""
    md = _build_markdown([
        ("c1", ("シート「一覧」",), "機能ID「F-1」", "出所: 原本「a.xlsx」 / シート「一覧」\n機能ID: 「F-1」",
         "A1:B2"),
        ("c2", ("シート「一覧」",), "機能ID「F-2」", "出所: 原本「a.xlsx」 / シート「一覧」\n機能ID: 「F-2」", None),
    ])
    header, records = llm_render._split_records(md)
    assert len(records) == 2
    assert records[0]["body"] == "出所: 原本「a.xlsx」 / シート「一覧」\n機能ID: 「F-1」"
    assert records[1]["body"] == "出所: 原本「a.xlsx」 / シート「一覧」\n機能ID: 「F-2」"
    assert "## シート「一覧」" in records[0]["chrome"]
    assert "## シート「一覧」" not in records[1]["chrome"]   # 同一セクション継続＝見出しを再出力しない
    assert "### 機能ID「F-2」" in records[1]["chrome"]        # key見出し自体はrecordごとに出る
    rebuilt = header + "".join(
        r["anchor"] + r["chrome"] + r["body"] + r["trailing"] for r in records)
    assert rebuilt == md


def test_split_records_no_anchors_returns_none():
    assert llm_render._split_records("見出しだけの文書\n本文\n") is None


# ---- 機械検証（保護行・原値の逐語保持） ------------------------------------------------------

def test_validate_accepts_faithful_rewrite():
    original = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    candidate = "出所: 原本「a.xlsx」\nこの記録には「対象システム: BETA」という記載がある。"
    assert llm_render._validate(original, candidate) is True


def test_validate_rejects_dropped_quoted_value():
    original = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    candidate = "出所: 原本「a.xlsx」\nこの記録には何らかの記載がある。"
    assert llm_render._validate(original, candidate) is False


def test_validate_rejects_altered_protected_line():
    original = "出所: 原本「a.xlsx」\n可視性: 「非表示のシートにあります」\n本文: 「あ」"
    candidate = "出所: 原本「a.xlsx」\n可視性: 「見えます」\n本文: 「あ」"
    assert llm_render._validate(original, candidate) is False


def test_validate_rejects_empty_or_non_string():
    assert llm_render._validate("出所: 「a」", "") is False
    assert llm_render._validate("出所: 「a」", "   ") is False
    assert llm_render._validate("出所: 「a」", None) is False


# ---- 1文書の成形（format_document） ----------------------------------------------------------

def _one_record_rule_markdown(body: str = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」") -> str:
    md = _build_markdown([("c1", ("文書「a」",), None, body, None)])
    return llm_render.stamp_rule_only(md)


def test_format_document_cache_hit_skips_llm_call(monkeypatch):
    body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    md = _one_record_rule_markdown(body)
    cfg = {"provider": "openai", "model": "gpt-5.5"}
    key = llm_render._cache_key(cfg, body)
    cache = {key: {"status": "ok", "text": "整えた本文「対象システム: BETA」"}}

    def _boom(*a, **kw):
        raise AssertionError("LLM を呼んではいけない（キャッシュヒット）")

    monkeypatch.setattr(graph_extract, "complete_json", _boom)
    result = llm_render.format_document("v1", "a.xlsx", md, cfg, cache)
    assert result.llm_count == 1
    assert "整えた本文「対象システム: BETA」" in result.markdown
    assert "生成手段: LLM（openai/gpt-5.5）＋規則（LLM成形 1 件）" in result.markdown
    assert result.changed is True


def test_format_document_valid_llm_output_is_cached(monkeypatch):
    body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    md = _one_record_rule_markdown(body)
    cfg = {"provider": "openai", "model": "gpt-5.5"}
    cache: dict = {}

    def _fake_complete(system, user, cfg_arg, timeout=None):
        return json.dumps({"text": "出所: 原本「a.xlsx」\nこの記録には「対象システム: BETA」とある。"})

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete)
    result = llm_render.format_document("v1", "a.xlsx", md, cfg, cache)
    assert result.llm_count == 1
    assert result.changed is True
    key = llm_render._cache_key(cfg, body)
    assert cache[key]["status"] == "ok"


def test_format_document_invalid_llm_output_falls_back_to_rule_and_caches_invalid(monkeypatch):
    body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    md = _one_record_rule_markdown(body)
    cfg = {"provider": "openai", "model": "gpt-5.5"}
    cache: dict = {}
    calls = []

    def _fake_complete(system, user, cfg_arg, timeout=None):
        calls.append(1)
        return json.dumps({"text": "値を落とした本文"})    # 「対象システム: BETA」が消えている＝検証NG

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete)
    result = llm_render.format_document("v1", "a.xlsx", md, cfg, cache)
    assert result.llm_count == 0
    assert result.changed is False           # 規則版のまま＝ファイル内容は不変
    assert body in result.markdown
    key = llm_render._cache_key(cfg, body)
    assert cache[key] == {"status": "invalid"}

    # 2回目は「既知の検証失敗」としてLLMを再度呼ばない。
    result2 = llm_render.format_document("v1", "a.xlsx", md, cfg, cache)
    assert result2.llm_count == 0
    assert len(calls) == 1


def test_format_document_llm_exception_does_not_cache_and_keeps_rule_text(monkeypatch):
    body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    md = _one_record_rule_markdown(body)
    cfg = {"provider": "openai", "model": "gpt-5.5"}
    cache: dict = {}

    def _boom(*a, **kw):
        raise RuntimeError("一時的な接続エラー")

    monkeypatch.setattr(graph_extract, "complete_json", _boom)
    result = llm_render.format_document("v1", "a.xlsx", md, cfg, cache)
    assert result.llm_count == 0
    assert result.changed is False
    assert cache == {}                        # 失敗はキャッシュしない＝次回再試行


def test_format_document_unparsable_markdown_returns_none():
    assert llm_render.format_document("v1", "a.xlsx", "アンカーが無い文書", {}, {}) is None


# ---- AI観測レコード（L8・§8.2）: 成形対象外＋直前recordへの補助文脈 ------------------------------

def _ai_observation_body(text: str = "画像に写っている内容の書き起こし") -> str:
    return llm_render._AI_OBSERVATION_BODY_MARKER + "\n観測内容: " + text


def test_is_ai_observation_body_detects_marker():
    assert llm_render._is_ai_observation_body(_ai_observation_body()) is True
    assert llm_render._is_ai_observation_body("出所: 原本「a.xlsx」") is False


def test_format_document_skips_llm_for_observation_record(monkeypatch):
    """AI観測レコード自身の本文はLLM成形の対象外＝生の記録のまま（呼び出しもキャッシュもしない）。"""
    canonical_body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    observation_body = _ai_observation_body()
    md = llm_render.stamp_rule_only(_build_markdown([
        ("c1", ("文書「a」",), None, canonical_body, None),
        ("c2", ("文書「a」",), None, observation_body, None),
    ]))
    cfg = {"provider": "openai", "model": "gpt-5.5"}
    calls = []

    def _fake_complete(system, user, cfg_arg, timeout=None):
        calls.append(user)
        return json.dumps({"text": "出所: 原本「a.xlsx」\nこの記録には「対象システム: BETA」とある。"})

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete)
    result = llm_render.format_document("v1", "a.xlsx", md, cfg, {})
    assert observation_body in result.markdown          # 観測本文は一字一句そのまま残る
    assert len(calls) == 1                               # 観測record分は呼ばない（canonicalの1回だけ）
    assert result.llm_count == 1


def test_format_document_passes_following_observation_as_auxiliary_context(monkeypatch):
    """直後のrecordがAI観測なら、対応するcanonical recordのプロンプトへ参考情報として同梱する
    （`_ai_observation_records`のsort_keyが対象要素の直後に置く設計に依拠）。"""
    canonical_body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    observation_body = _ai_observation_body("画像内の手書きメモ")
    md = llm_render.stamp_rule_only(_build_markdown([
        ("c1", ("文書「a」",), None, canonical_body, None),
        ("c2", ("文書「a」",), None, observation_body, None),
    ]))
    cfg = {"provider": "openai", "model": "gpt-5.5"}
    prompts = []

    def _fake_complete(system, user, cfg_arg, timeout=None):
        prompts.append(user)
        return json.dumps({"text": "整形済み。「対象システム: BETA」を含む。"})

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete)
    llm_render.format_document("v1", "a.xlsx", md, cfg, {})
    assert len(prompts) == 1
    assert "参考情報" in prompts[0]
    assert observation_body in prompts[0]
    assert canonical_body in prompts[0]


def test_format_document_auxiliary_context_does_not_bypass_protected_line_validation(monkeypatch):
    """観測を補助文脈として渡しても、canonical自身の「」原値の逐語検証は変わらず効く（fail-closed）。
    LLM が参考情報の内容を新事実として書きつつ canonical の引用値を落とした場合、その出力は不採用
    になり canonical の本文はそのまま残る。"""
    canonical_body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    observation_body = _ai_observation_body("画像内の手書きメモ")
    md = llm_render.stamp_rule_only(_build_markdown([
        ("c1", ("文書「a」",), None, canonical_body, None),
        ("c2", ("文書「a」",), None, observation_body, None),
    ]))
    cfg = {"provider": "openai", "model": "gpt-5.5"}

    def _fake_complete(system, user, cfg_arg, timeout=None):
        # 保護行(出所:)は残しつつ、「対象システム: BETA」という引用値を落として観測内容を混入させる。
        return json.dumps({"text": "出所: 原本「a.xlsx」\n画像内の手書きメモの内容を反映した。"})

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete)
    result = llm_render.format_document("v1", "a.xlsx", md, cfg, {})
    assert result.llm_count == 0                   # 検証NG＝不採用
    assert canonical_body in result.markdown        # canonicalの原値は不変のまま残る
    assert observation_body in result.markdown      # 観測本文も不変のまま残る


def test_format_document_cache_key_distinguishes_auxiliary_context():
    body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    cfg = {"provider": "openai", "model": "gpt-5.5"}
    assert llm_render._cache_key(cfg, body) != llm_render._cache_key(cfg, body, "観測あり")


def test_format_document_observation_only_document_never_calls_llm(monkeypatch):
    """先頭recordがいきなりAI観測（直前canonicalが無い）でも、観測record自身は呼ばれない。"""
    observation_body = _ai_observation_body()
    md = llm_render.stamp_rule_only(_build_markdown([
        ("c1", ("文書「a」",), None, observation_body, None),
    ]))

    def _boom(*a, **kw):
        raise AssertionError("観測レコードだけの文書でLLMを呼んではいけない")

    monkeypatch.setattr(graph_extract, "complete_json", _boom)
    result = llm_render.format_document("v1", "a.xlsx", md, {}, {})
    assert result.llm_count == 0
    assert result.changed is False
    assert result.markdown == md


# ---- world 単位の背景パス（run_world_pass） ---------------------------------------------------

def test_run_world_pass_noop_when_toggle_off(monkeypatch, tmp_path):
    world = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": "off"})

    def _boom(*a, **kw):
        raise AssertionError("トグルOFFではファイルを触ってはいけない")

    monkeypatch.setattr(worlds, "derived_rag_dir", _boom)
    result = llm_render.run_world_pass(world)
    assert result.docs_scanned == 0
    assert result.changed_rels == []


def test_run_world_pass_noop_when_llm_unavailable(monkeypatch, tmp_path):
    world = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_render, "available", lambda settings=None: None)

    def _boom(*a, **kw):
        raise AssertionError("LLM未接続ではファイルを触ってはいけない")

    monkeypatch.setattr(worlds, "derived_rag_dir", _boom)
    result = llm_render.run_world_pass(world)
    assert result.docs_scanned == 0


def test_run_world_pass_end_to_end_writes_and_prunes_cache(monkeypatch, tmp_path):
    world = _isolate(monkeypatch, tmp_path)
    cfg = {"provider": "openai", "model": "gpt-5.5"}
    monkeypatch.setattr(llm_render, "available", lambda settings=None: cfg)

    body_a = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    md_a = llm_render.stamp_rule_only(_build_markdown([("ca", ("文書「a」",), None, body_a, None)]))
    rag_dir = worlds.derived_rag_dir(world)
    rag_dir.mkdir(parents=True)
    (rag_dir / "a.xlsx.rag.md").write_text(md_a, encoding="utf-8")

    # 既に成形済み（settled）な文書は対象外＝触らない。
    settled_md = "生成手段: LLM（openai/gpt-5.5）＋規則（LLM成形 1 件）\n<!-- chunk:cb -->\n出所: 「b」\n"
    (rag_dir / "b.xlsx.rag.md").write_text(settled_md, encoding="utf-8")

    def _fake_complete(system, user, cfg_arg, timeout=None):
        return json.dumps({"text": "出所: 原本「a.xlsx」\nこの記録には「対象システム: BETA」とある。"})

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete)
    result = llm_render.run_world_pass(world)

    assert result.docs_scanned == 2
    assert result.changed_rels == ["a.xlsx"]
    assert result.llm_records == 1
    written = (rag_dir / "a.xlsx.rag.md").read_text(encoding="utf-8")
    assert "生成手段: LLM（openai/gpt-5.5）＋規則（LLM成形 1 件）" in written
    assert "対象システム: BETA" in written
    assert (rag_dir / "b.xlsx.rag.md").read_text(encoding="utf-8") == settled_md   # 不変

    cache = llm_render._load_cache(world)
    assert len(cache) == 1                    # a.xlsx の1record分だけ（訪問しなかったb分は無い＝鏡剪定）

    # 2回目のパス: a.xlsx は既に settled（LLM＋規則）へ変わっているため対象外＝LLMは呼ばれない。
    monkeypatch.setattr(graph_extract, "complete_json",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("再度呼ばれてはいけない")))
    result2 = llm_render.run_world_pass(world)
    assert result2.changed_rels == []


def test_clear_cache_removes_file(monkeypatch, tmp_path):
    world = _isolate(monkeypatch, tmp_path)
    path = llm_render._cache_path(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    json_io.write_json_atomic(path, {"entries": {"k": {"status": "ok", "text": "x"}}})
    assert path.exists()
    llm_render.clear_cache(world)
    assert not path.exists()


def test_clear_cache_is_noop_when_absent(monkeypatch, tmp_path):
    world = _isolate(monkeypatch, tmp_path)
    llm_render.clear_cache(world)      # 例外を出さない


# ---- 多重起動抑止 ----------------------------------------------------------------------------

def test_schedule_background_prevents_concurrent_runs():
    import threading
    import time

    started = threading.Event()
    release = threading.Event()
    calls = []

    def _work(world):
        calls.append(world)
        started.set()
        release.wait(timeout=5)

    try:
        assert llm_render.schedule_background("w-guard", _work) is True
        assert started.wait(timeout=5)
        assert llm_render.is_running("w-guard") is True
        assert llm_render.schedule_background("w-guard", _work) is False   # 実行中は起動しない
        assert calls == ["w-guard"]                                        # 2回目は呼ばれていない
    finally:
        release.set()
        for _ in range(50):
            if not llm_render.is_running("w-guard"):
                break
            time.sleep(0.05)
    assert llm_render.is_running("w-guard") is False


def test_schedule_background_reports_failure_without_raising(caplog):
    def _boom(world):
        raise RuntimeError("背景処理の失敗")

    import time
    assert llm_render.schedule_background("w-fail", _boom) is True
    for _ in range(50):
        if not llm_render.is_running("w-fail"):
            break
        time.sleep(0.05)
    assert llm_render.is_running("w-fail") is False   # 例外を吸収し、レジストリからも外れる


# ---- M1: LLM 成形の metering 配線（kind='rag_render'） ------------------------------------------
# `graph_extract.complete_json` は差し替えず、`llm.post_json`（HTTP 送信の最下層）だけを差し替える
# ——`complete_json` 内部の実 `metering.acc_add` 呼び出しをそのまま通す（`test_metering_sites.py` の
# 消費サイトテストと同じ流儀）ことで、run_world_pass 側の acc_begin/acc_end 配線を実地で検証する。

_real_metering_record = metering.record


def _enable_metering(monkeypatch):
    # conftest.py::_hermetic_metering_record（autouse）が no-op にした `metering.record` を、
    # 捕捉しておいた本物の関数オブジェクトへ明示的に戻す（TOGGLE-RM・2026-09-03 で計測は常時ON）。
    monkeypatch.setattr(metering, "record", _real_metering_record)


def _spy_usage(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ue, "add_usage_event", lambda **kw: calls.append(kw))
    return calls


def _fake_openai_chat_post(prompt_tokens: int, completion_tokens: int, text: str):
    def _post(url, headers, body, timeout=90):
        return {"choices": [{"message": {"content": json.dumps({"text": text})}}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}
    return _post


def test_run_world_pass_records_rag_render_for_real_llm_call(monkeypatch, tmp_path):
    world = _isolate(monkeypatch, tmp_path)
    cfg = {"provider": "openai", "model": "gpt-5.5", "key": "test-key"}
    monkeypatch.setattr(llm_render, "available", lambda settings=None: cfg)
    _enable_metering(monkeypatch)
    calls = _spy_usage(monkeypatch)

    body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    md = llm_render.stamp_rule_only(_build_markdown([("ca", ("文書「a」",), None, body, None)]))
    rag_dir = worlds.derived_rag_dir(world)
    rag_dir.mkdir(parents=True)
    (rag_dir / "a.xlsx.rag.md").write_text(md, encoding="utf-8")

    monkeypatch.setattr(llm, "post_json", _fake_openai_chat_post(
        40, 12, "出所: 原本「a.xlsx」\nこの記録には「対象システム: BETA」とある。"))
    result = llm_render.run_world_pass(world)
    assert result.llm_records == 1
    assert len(calls) == 1
    c = calls[0]
    assert c["kind"] == "rag_render" and c["provider"] == "openai" and c["model"] == "gpt-5.5"
    assert c["input_tokens"] == 40 and c["output_tokens"] == 12
    assert c["calls"] == 1 and c["world"] == world and c["user_id"] is None


def test_run_world_pass_cache_hit_records_nothing(monkeypatch, tmp_path):
    world = _isolate(monkeypatch, tmp_path)
    cfg = {"provider": "openai", "model": "gpt-5.5", "key": "test-key"}
    monkeypatch.setattr(llm_render, "available", lambda settings=None: cfg)
    _enable_metering(monkeypatch)
    calls = _spy_usage(monkeypatch)

    body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    md = llm_render.stamp_rule_only(_build_markdown([("ca", ("文書「a」",), None, body, None)]))
    rag_dir = worlds.derived_rag_dir(world)
    rag_dir.mkdir(parents=True)
    (rag_dir / "a.xlsx.rag.md").write_text(md, encoding="utf-8")

    key = llm_render._cache_key(cfg, body)
    llm_render._save_cache(world, {key: {"status": "ok", "text": "整えた本文「対象システム: BETA」"}})

    def _boom(*a, **kw):
        raise AssertionError("キャッシュヒットで LLM を呼んではいけない")

    monkeypatch.setattr(llm, "post_json", _boom)
    result = llm_render.run_world_pass(world)
    assert result.llm_records == 1     # キャッシュテキストは採用される（呼び出しはしない）
    assert calls == []                 # 実費用の無いキャッシュヒットは計上しない（acc_add が一度も呼ばれない）


def test_run_world_pass_records_again_after_regenerate(monkeypatch, tmp_path):
    """「規則版で再生成」（`worker.regenerate_rag_rule_only`: キャッシュ一掃＋rag.md を規則版へ
    巻き戻し）相当の操作の後、次の背景パスでの再成形も改めて1行計上されることを固定する。"""
    world = _isolate(monkeypatch, tmp_path)
    cfg = {"provider": "openai", "model": "gpt-5.5", "key": "test-key"}
    monkeypatch.setattr(llm_render, "available", lambda settings=None: cfg)
    _enable_metering(monkeypatch)
    calls = _spy_usage(monkeypatch)

    body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    base_md = _build_markdown([("ca", ("文書「a」",), None, body, None)])
    rag_dir = worlds.derived_rag_dir(world)
    rag_dir.mkdir(parents=True)
    rag_path = rag_dir / "a.xlsx.rag.md"
    rag_path.write_text(llm_render.stamp_rule_only(base_md), encoding="utf-8")

    monkeypatch.setattr(llm, "post_json", _fake_openai_chat_post(
        40, 12, "出所: 原本「a.xlsx」\nこの記録には「対象システム: BETA」とある。"))
    llm_render.run_world_pass(world)
    assert len(calls) == 1

    llm_render.clear_cache(world)                                          # キャッシュ一掃
    rag_path.write_text(llm_render.stamp_rule_only(base_md), encoding="utf-8")   # 規則版へ巻き戻し
    calls.clear()
    llm_render.run_world_pass(world)
    assert len(calls) == 1
    assert calls[0]["kind"] == "rag_render"


def test_run_world_pass_without_metering_restored_records_nothing(monkeypatch, tmp_path):
    """`_enable_metering()` で明示的に戻さない限り、conftest.py::_hermetic_metering_record
    （autouse）が `metering.record` を no-op にしたままのため、実 LLM 呼び出しが起きても記録しない
    （TOGGLE-RM・2026-09-03 で計測は常時ONだが、unit テストの実 DB 書き込み防止は autouse fixture
    が担う）。"""
    world = _isolate(monkeypatch, tmp_path)
    cfg = {"provider": "openai", "model": "gpt-5.5", "key": "test-key"}
    monkeypatch.setattr(llm_render, "available", lambda settings=None: cfg)
    calls = _spy_usage(monkeypatch)   # metering.record は autouse fixture により既定 no-op

    body = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
    md = llm_render.stamp_rule_only(_build_markdown([("ca", ("文書「a」",), None, body, None)]))
    rag_dir = worlds.derived_rag_dir(world)
    rag_dir.mkdir(parents=True)
    (rag_dir / "a.xlsx.rag.md").write_text(md, encoding="utf-8")

    monkeypatch.setattr(llm, "post_json", _fake_openai_chat_post(
        40, 12, "出所: 原本「a.xlsx」\nこの記録には「対象システム: BETA」とある。"))
    result = llm_render.run_world_pass(world)
    assert result.llm_records == 1
    assert calls == []


def test_flow_diagram_record_is_never_llm_formatted():
    """L9 のフロー図レコード（Mermaid＝決定的成果物）は LLM 成形の対象外（検収是正の配線）。

    保護行検証は固定 prefix 行と「」原値しか見ないため、スキップしないと Mermaid フェンス内が
    素通しで書き換えられうる。マーカーは evidence_render.FLOW_DIAGRAM_BODY_MARKER と同一literal。
    """
    from sherpa.ingest import evidence_render, llm_render as L
    assert L._FLOW_DIAGRAM_BODY_MARKER == evidence_render.FLOW_DIAGRAM_BODY_MARKER
    body = evidence_render.FLOW_DIAGRAM_BODY_MARKER + "\n出所: 原本「a.xlsx」\n```mermaid\nflowchart TD\n```"
    assert L._is_machine_artifact_body(body)
    assert L._is_machine_artifact_body(L._AI_OBSERVATION_BODY_MARKER + " x")
    assert not L._is_machine_artifact_body("通常のレコード本文")
