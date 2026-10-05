"""rag.md の LLM 成形＋規則フォールバックの単体テスト。

実 LLM 呼び出しは発生しない（`graph_extract.complete_json`/`available` か `llm.post_json` を差し替え）。
`SHERPA_KB_DIR`/`SHERPA_DERIVED_DIR` を `tmp_path` へ隔離する。
"""
from __future__ import annotations

import contextlib
import json
import threading
import time

import pytest

from sherpa import json_io, llm, metering, store, worlds
from sherpa.ingest import evidence_render, graph_extract, llm_render
from sherpa.store import usage_events as ue

CFG = {"provider": "openai", "model": "gpt-5.5"}
BODY = "出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」"
GOOD_TEXT = "出所: 原本「a.xlsx」\nこの記録には「対象システム: BETA」とある。"
SETTLED = "生成手段: LLM（openai/gpt-5.5）＋規則（LLM成形 1 件）"


@contextlib.contextmanager
def _noop_lock(world_id):
    yield


def _isolate(monkeypatch, tmp_path, world: str = "v1") -> str:
    monkeypatch.setenv("SHERPA_KB_DIR", str(tmp_path / "kb"))
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    monkeypatch.setattr(store, "get_world", lambda world_id: None)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": "on"})
    # run_world_pass は書込直前に store.world_lock を通す＝DB 非依存の no-op に差し替える。
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    return world


def _build_markdown(records: list[tuple]) -> str:
    """`evidence_render._markdown()` と同じ組み立てで rag.md を作る
    （`chunk_id, section, key_heading, body, region` のタプル列）。"""
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


def _rule_md(*bodies: str) -> str:
    return llm_render.stamp_rule_only(_build_markdown(
        [(f"c{i}", ("文書「a」",), None, b, None) for i, b in enumerate(bodies, 1)]))


def _ai_observation_body(text: str = "画像に写っている内容の書き起こし") -> str:
    return llm_render._AI_OBSERVATION_BODY_MARKER + "\n観測内容: " + text


def _fake_complete(text: str = GOOD_TEXT, calls: list | None = None):
    def _f(system, user, cfg_arg, timeout=None):
        if calls is not None:
            calls.append(user)
        return json.dumps({"text": text})
    return _f


def _boom(msg: str = "呼んではいけない"):
    def _f(*a, **kw):
        raise AssertionError(msg)
    return _f


# ---- トグル解決 ------------------------------------------------------------------------------

@pytest.mark.parametrize("settings, expected", [
    ({}, False),
    ({"rag_llm_render": "on"}, True),
    ({"rag_llm_render": "off"}, False),
    ({"rag_llm_render": True}, True),      # 旧版が書いた boolean も意図を保持する
    ({"rag_llm_render": False}, False),
    ({"rag_llm_render": "maybe"}, False),  # 未知値は既定 off
])
def test_toggle_resolution(monkeypatch, settings, expected):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: settings)
    assert llm_render.rag_llm_render_enabled() is expected


def test_toggle_system_settings_read_failure_falls_back_to_default(monkeypatch):
    def _fail(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "get_system_settings", _fail)
    assert llm_render.rag_llm_render_enabled() is False


def test_env_default_enabled_ignores_system_settings(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": "on"})
    assert llm_render.env_default_enabled() is False


# ---- LLM 設定解決 ----------------------------------------------------------------------------

def test_available_delegates_to_graph_extract_with_render_usage(monkeypatch):
    calls = []

    def _fake_available(settings, strict=False, usage="extract"):
        calls.append((settings, strict, usage))
        return dict(CFG)

    monkeypatch.setattr(graph_extract, "available", _fake_available)
    assert llm_render.available({"x": 1}) == CFG
    assert calls == [({"x": 1}, True, "render")]


def test_available_returns_none_on_invalid_cloud_provider_config(monkeypatch):
    from sherpa import keys as _keys

    def _bad(settings, strict=False, usage="extract"):
        raise _keys.InvalidCloudProviderConfigError("bad")

    monkeypatch.setattr(graph_extract, "available", _bad)
    assert llm_render.available() is None


# ---- 生成手段スタンプ ------------------------------------------------------------------------

def test_stamp_rule_only_inserts_after_profile_line():
    md = "# AI検索用文書\n\n原本: a.xlsx\n変換プロファイル: p1 / v1\n抽出範囲: ok=1\n\n<!-- chunk:c1 -->\n本文\n"
    lines = llm_render.stamp_rule_only(md).splitlines()
    assert lines[lines.index("変換プロファイル: p1 / v1") + 1] == "生成手段: 規則"


def test_stamp_rule_only_fail_safe_when_profile_line_missing():
    md = "何か想定外の形式\n<!-- chunk:c1 -->\n本文\n"
    stamped = llm_render.stamp_rule_only(md)
    assert stamped.startswith("生成手段: 規則\n")
    assert md in stamped


@pytest.mark.parametrize("md, expected", [
    (llm_render.stamp_rule_only("変換プロファイル: p1 / v1\n\n<!-- chunk:c1 -->\n本文\n"), True),
    (SETTLED + "\n<!-- chunk:c1 -->\n本文\n", False),
    ("<!-- chunk:c1 -->\n本文\n", True),   # 生成手段行が無い＝fail-open で成形対象
])
def test_needs_llm_pass(md, expected):
    assert llm_render.needs_llm_pass(md) is expected


# ---- レコード分割 ----------------------------------------------------------------------------

def _rebuild(header, records) -> str:
    return header + "".join(r["anchor"] + r["chrome"] + r["body"] + r["trailing"] for r in records)


def test_split_records_round_trip_single_record():
    md = _build_markdown([("c1", ("文書「a」",), None, "出所: 原本「a.xlsx」\n本文: 「あ」", None)])
    header, records = llm_render._split_records(md)
    assert len(records) == 1
    assert records[0]["body"] == "出所: 原本「a.xlsx」\n本文: 「あ」"
    assert _rebuild(header, records) == md


def test_split_records_round_trip_multi_record_shared_section():
    md = _build_markdown([
        ("c1", ("シート「一覧」",), "機能ID「F-1」", "出所: 原本「a.xlsx」 / シート「一覧」\n機能ID: 「F-1」", "A1:B2"),
        ("c2", ("シート「一覧」",), "機能ID「F-2」", "出所: 原本「a.xlsx」 / シート「一覧」\n機能ID: 「F-2」", None),
    ])
    header, records = llm_render._split_records(md)
    assert len(records) == 2
    assert records[0]["body"] == "出所: 原本「a.xlsx」 / シート「一覧」\n機能ID: 「F-1」"
    assert records[1]["body"] == "出所: 原本「a.xlsx」 / シート「一覧」\n機能ID: 「F-2」"
    assert "## シート「一覧」" in records[0]["chrome"]
    assert "## シート「一覧」" not in records[1]["chrome"]   # 同一セクション継続＝見出しを再出力しない
    assert "### 機能ID「F-2」" in records[1]["chrome"]
    assert _rebuild(header, records) == md


def test_split_records_no_anchors_returns_none():
    assert llm_render._split_records("見出しだけの文書\n本文\n") is None


# ---- 機械検証 --------------------------------------------------------------------------------

@pytest.mark.parametrize("original, candidate, expected", [
    ("出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」",
     "出所: 原本「a.xlsx」\nこの記録には「対象システム: BETA」という記載がある。", True),
    ("出所: 原本「a.xlsx」\n本文: 「対象システム: BETA」",
     "出所: 原本「a.xlsx」\nこの記録には何らかの記載がある。", False),            # 引用値の脱落
    ("出所: 原本「a.xlsx」\n可視性: 「非表示のシートにあります」\n本文: 「あ」",
     "出所: 原本「a.xlsx」\n可視性: 「見えます」\n本文: 「あ」", False),            # 保護行の改変
    ("出所: 「a」", "", False),
    ("出所: 「a」", "   ", False),
    ("出所: 「a」", None, False),
])
def test_validate(original, candidate, expected):
    assert llm_render._validate(original, candidate) is expected


# ---- format_document -------------------------------------------------------------------------

def test_format_document_cache_hit_skips_llm_call(monkeypatch):
    key = llm_render._cache_key(CFG, BODY)
    cache = {key: {"status": "ok", "text": "整えた本文「対象システム: BETA」"}}
    monkeypatch.setattr(graph_extract, "complete_json", _boom("キャッシュヒットで LLM を呼んではいけない"))
    result = llm_render.format_document("v1", "a.xlsx", _rule_md(BODY), CFG, cache)
    assert result.llm_count == 1
    assert "整えた本文「対象システム: BETA」" in result.markdown
    assert SETTLED in result.markdown
    assert result.changed is True


def test_format_document_valid_llm_output_is_cached(monkeypatch):
    cache: dict = {}
    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete())
    result = llm_render.format_document("v1", "a.xlsx", _rule_md(BODY), CFG, cache)
    assert result.llm_count == 1
    assert result.changed is True
    assert cache[llm_render._cache_key(CFG, BODY)]["status"] == "ok"


def test_format_document_invalid_llm_output_falls_back_to_rule_and_caches_invalid(monkeypatch):
    md = _rule_md(BODY)
    cache: dict = {}
    calls: list = []
    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete("値を落とした本文", calls))
    result = llm_render.format_document("v1", "a.xlsx", md, CFG, cache)
    assert result.llm_count == 0
    assert result.changed is False
    assert BODY in result.markdown
    assert cache[llm_render._cache_key(CFG, BODY)] == {"status": "invalid"}

    # 既知の検証失敗は再度 LLM を呼ばない。
    assert llm_render.format_document("v1", "a.xlsx", md, CFG, cache).llm_count == 0
    assert len(calls) == 1


def test_format_document_llm_exception_does_not_cache_and_keeps_rule_text(monkeypatch):
    cache: dict = {}

    def _fail(*a, **kw):
        raise RuntimeError("一時的な接続エラー")

    monkeypatch.setattr(graph_extract, "complete_json", _fail)
    result = llm_render.format_document("v1", "a.xlsx", _rule_md(BODY), CFG, cache)
    assert result.llm_count == 0
    assert result.changed is False
    assert cache == {}                        # 失敗はキャッシュしない＝次回再試行


def test_format_document_unparsable_markdown_returns_none():
    assert llm_render.format_document("v1", "a.xlsx", "アンカーが無い文書", {}, {}) is None


# ---- AI観測レコード: 成形対象外＋直前 record への補助文脈 ----------------------------------------

def test_is_ai_observation_body_detects_marker():
    assert llm_render._is_ai_observation_body(_ai_observation_body()) is True
    assert llm_render._is_ai_observation_body("出所: 原本「a.xlsx」") is False


def test_format_document_skips_llm_for_observation_record(monkeypatch):
    obs = _ai_observation_body()
    calls: list = []
    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete(calls=calls))
    result = llm_render.format_document("v1", "a.xlsx", _rule_md(BODY, obs), CFG, {})
    assert obs in result.markdown          # 観測本文は一字一句そのまま
    assert len(calls) == 1                 # canonical の 1 回だけ
    assert result.llm_count == 1


def test_format_document_passes_following_observation_as_auxiliary_context(monkeypatch):
    obs = _ai_observation_body("画像内の手書きメモ")
    prompts: list = []
    monkeypatch.setattr(graph_extract, "complete_json",
                        _fake_complete("整形済み。「対象システム: BETA」を含む。", prompts))
    llm_render.format_document("v1", "a.xlsx", _rule_md(BODY, obs), CFG, {})
    assert len(prompts) == 1
    assert "参考情報" in prompts[0]
    assert obs in prompts[0]
    assert BODY in prompts[0]


def test_format_document_auxiliary_context_does_not_bypass_protected_line_validation(monkeypatch):
    # 保護行は残しつつ canonical の引用値を落として観測内容を混入させた出力は不採用（fail-closed）。
    obs = _ai_observation_body("画像内の手書きメモ")
    monkeypatch.setattr(graph_extract, "complete_json",
                        _fake_complete("出所: 原本「a.xlsx」\n画像内の手書きメモの内容を反映した。"))
    result = llm_render.format_document("v1", "a.xlsx", _rule_md(BODY, obs), CFG, {})
    assert result.llm_count == 0
    assert BODY in result.markdown
    assert obs in result.markdown


def test_format_document_cache_key_distinguishes_auxiliary_context():
    assert llm_render._cache_key(CFG, BODY) != llm_render._cache_key(CFG, BODY, "観測あり")


def test_format_document_observation_only_document_never_calls_llm(monkeypatch):
    md = _rule_md(_ai_observation_body())
    monkeypatch.setattr(graph_extract, "complete_json", _boom("観測レコードだけの文書で LLM を呼んではいけない"))
    result = llm_render.format_document("v1", "a.xlsx", md, {}, {})
    assert result.llm_count == 0
    assert result.changed is False
    assert result.markdown == md


# ---- run_world_pass --------------------------------------------------------------------------

@pytest.mark.parametrize("toggle, llm_available", [("off", True), ("on", False)])
def test_run_world_pass_noop_when_toggle_off_or_llm_unavailable(monkeypatch, tmp_path, toggle, llm_available):
    world = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"rag_llm_render": toggle})
    monkeypatch.setattr(llm_render, "available", lambda settings=None: dict(CFG) if llm_available else None)
    monkeypatch.setattr(worlds, "derived_rag_dir", _boom("ファイルを触ってはいけない"))
    result = llm_render.run_world_pass(world)
    assert result.docs_scanned == 0
    assert result.changed_rels == []


def _write_rag(world: str, name: str, md: str):
    rag_dir = worlds.derived_rag_dir(world)
    rag_dir.mkdir(parents=True, exist_ok=True)
    path = rag_dir / f"{name}.rag.md"
    path.write_text(md, encoding="utf-8")
    return path


def test_run_world_pass_end_to_end_writes_and_prunes_cache(monkeypatch, tmp_path):
    world = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_render, "available", lambda settings=None: dict(CFG))
    _write_rag(world, "a.xlsx", _rule_md(BODY))
    settled_md = SETTLED + "\n<!-- chunk:cb -->\n出所: 「b」\n"   # 成形済み＝対象外
    b_path = _write_rag(world, "b.xlsx", settled_md)
    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete())
    result = llm_render.run_world_pass(world)

    assert result.docs_scanned == 2
    assert result.changed_rels == ["a.xlsx"]
    assert result.llm_records == 1
    written = (worlds.derived_rag_dir(world) / "a.xlsx.rag.md").read_text(encoding="utf-8")
    assert SETTLED in written
    assert "対象システム: BETA" in written
    assert b_path.read_text(encoding="utf-8") == settled_md
    assert len(llm_render._load_cache(world)) == 1    # 訪問しなかった b 分は無い＝鏡剪定

    # 2 回目: a.xlsx は settled へ変わっているため LLM は呼ばれない。
    monkeypatch.setattr(graph_extract, "complete_json", _boom("再度呼ばれてはいけない"))
    assert llm_render.run_world_pass(world).changed_rels == []


def test_run_world_pass_skips_sensitive_original_name(monkeypatch, tmp_path):
    # 秘匿名は is_sensitive 導入前の rag.md が残っていても LLM（外部 API）へ本文を送らない。
    world = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_render, "available", lambda settings=None: dict(CFG))
    body = "出所: 原本「credentials.xlsx」\n本文: 「秘密の値」"
    md = llm_render.stamp_rule_only(
        _build_markdown([("ca", ("文書「credentials.xlsx」",), None, body, None)]))
    path = _write_rag(world, "credentials.xlsx", md)
    calls: list = []
    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete("改変後テキスト", calls))
    result = llm_render.run_world_pass(world)
    assert calls == []
    assert result.changed_rels == []
    assert result.docs_scanned == 0
    assert path.read_text(encoding="utf-8") == md


def test_clear_cache_removes_file_and_is_noop_when_absent(monkeypatch, tmp_path):
    world = _isolate(monkeypatch, tmp_path)
    llm_render.clear_cache(world)      # 不在でも例外を出さない
    path = llm_render._cache_path(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    json_io.write_json_atomic(path, {"entries": {"k": {"status": "ok", "text": "x"}}})
    assert path.exists()
    llm_render.clear_cache(world)
    assert not path.exists()


# ---- 多重起動抑止 ----------------------------------------------------------------------------

def _wait_idle(world: str):
    for _ in range(50):
        if not llm_render.is_running(world):
            break
        time.sleep(0.05)


def test_schedule_background_prevents_concurrent_runs():
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
        assert llm_render.schedule_background("w-guard", _work) is False
        assert calls == ["w-guard"]
    finally:
        release.set()
        _wait_idle("w-guard")
    assert llm_render.is_running("w-guard") is False


def test_schedule_background_reports_failure_without_raising():
    def _fail(world):
        raise RuntimeError("背景処理の失敗")

    assert llm_render.schedule_background("w-fail", _fail) is True
    _wait_idle("w-fail")
    assert llm_render.is_running("w-fail") is False   # 例外を吸収しレジストリからも外れる


# ---- metering 配線（kind='rag_render'）-------------------------------------------------------
# `llm.post_json`（HTTP 最下層）だけを差し替え、`complete_json` 内の実 `metering.acc_add` を通す。

_real_metering_record = metering.record


def _metering_setup(monkeypatch, tmp_path, *, metering_on: bool):
    world = _isolate(monkeypatch, tmp_path)
    cfg = {**CFG, "key": "test-key"}
    monkeypatch.setattr(llm_render, "available", lambda settings=None: cfg)
    if metering_on:
        # conftest の autouse が no-op にした `metering.record` を本物へ戻す。
        monkeypatch.setattr(metering, "record", _real_metering_record)
    calls: list = []
    monkeypatch.setattr(ue, "add_usage_event", lambda **kw: calls.append(kw))
    path = _write_rag(world, "a.xlsx", _rule_md(BODY))
    return world, cfg, calls, path


def _fake_openai_chat_post(prompt_tokens: int = 40, completion_tokens: int = 12, text: str = GOOD_TEXT):
    def _post(url, headers, body, timeout=90):
        return {"choices": [{"message": {"content": json.dumps({"text": text})}}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}
    return _post


def test_run_world_pass_records_rag_render_for_real_llm_call(monkeypatch, tmp_path):
    world, _cfg, calls, _p = _metering_setup(monkeypatch, tmp_path, metering_on=True)
    monkeypatch.setattr(llm, "post_json", _fake_openai_chat_post())
    assert llm_render.run_world_pass(world).llm_records == 1
    assert len(calls) == 1
    c = calls[0]
    assert c["kind"] == "rag_render" and c["provider"] == "openai" and c["model"] == "gpt-5.5"
    assert c["input_tokens"] == 40 and c["output_tokens"] == 12
    assert c["calls"] == 1 and c["world"] == world and c["user_id"] is None


def test_run_world_pass_cache_hit_records_nothing(monkeypatch, tmp_path):
    world, cfg, calls, _p = _metering_setup(monkeypatch, tmp_path, metering_on=True)
    key = llm_render._cache_key(cfg, BODY)
    llm_render._save_cache(world, {key: {"status": "ok", "text": "整えた本文「対象システム: BETA」"}})
    monkeypatch.setattr(llm, "post_json", _boom("キャッシュヒットで LLM を呼んではいけない"))
    assert llm_render.run_world_pass(world).llm_records == 1   # キャッシュは採用される
    assert calls == []


def test_run_world_pass_records_again_after_regenerate(monkeypatch, tmp_path):
    # 規則版で再生成（キャッシュ一掃＋rag.md を規則版へ巻き戻し）の後の再成形も 1 行計上される。
    world, _cfg, calls, rag_path = _metering_setup(monkeypatch, tmp_path, metering_on=True)
    monkeypatch.setattr(llm, "post_json", _fake_openai_chat_post())
    llm_render.run_world_pass(world)
    assert len(calls) == 1

    llm_render.clear_cache(world)
    rag_path.write_text(_rule_md(BODY), encoding="utf-8")
    calls.clear()
    llm_render.run_world_pass(world)
    assert len(calls) == 1
    assert calls[0]["kind"] == "rag_render"


def test_run_world_pass_without_metering_restored_records_nothing(monkeypatch, tmp_path):
    # conftest の autouse が `metering.record` を no-op にしたままなら実 LLM 呼び出しでも記録しない。
    world, _cfg, calls, _p = _metering_setup(monkeypatch, tmp_path, metering_on=False)
    monkeypatch.setattr(llm, "post_json", _fake_openai_chat_post())
    assert llm_render.run_world_pass(world).llm_records == 1
    assert calls == []


def test_flow_diagram_record_is_never_llm_formatted():
    # Mermaid（決定的成果物）は成形対象外。マーカーは evidence_render と同一 literal。
    L = llm_render
    assert L._FLOW_DIAGRAM_BODY_MARKER == evidence_render.FLOW_DIAGRAM_BODY_MARKER
    body = evidence_render.FLOW_DIAGRAM_BODY_MARKER + "\n出所: 原本「a.xlsx」\n```mermaid\nflowchart TD\n```"
    assert L._is_machine_artifact_body(body)
    assert L._is_machine_artifact_body(L._AI_OBSERVATION_BODY_MARKER + " x")
    assert not L._is_machine_artifact_body("通常のレコード本文")
