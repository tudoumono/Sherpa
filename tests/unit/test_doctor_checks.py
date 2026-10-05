"""`scripts/doctor_checks.py`（`make doctor` の検査本体）の単体テスト。

外部サービスには触れない。ストア疎通は `sherpa.health` の `_check_one` を差し替え、設定の判定は
system_settings dict と `agent_constructs` 等の差し替えで決定的にする。点検の項目ごとに
「正常」と「異常」を表（parametrize）で確かめる。実 DB に触れる確認は
`tests/contract/test_doctor_integration.py`。
"""
from __future__ import annotations

import json
import logging
import os
import shlex

import pytest

import scripts.doctor_checks as doctor_checks
from sherpa import agent_constructs, health, keys, llm, model_catalog
from sherpa.ingest import graph_extract

_FIXED = "この検査自体が予期しないエラーで失敗しました（設定を確認してください）"
_AZ = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1"}
_REAL = "sk-real-key-1234567890"
_ROW_OLLAMA = {"agent": "ollama", "codex_model_provider": None, "search_helper": "",
               "has_openai_key": False, "ollama_url": None}


class _Resp:
    def __init__(self, payload: bytes):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._p


def _tags(*names: str) -> bytes:
    return json.dumps({"models": [{"name": n} for n in names]}).encode()


def _agent(monkeypatch, default="ollama", cmp=None, kind=None, per_row=False):
    """effective_agent を差し替える。per_row=True なら利用者行の `agent` 欄を優先する。"""
    monkeypatch.setattr(agent_constructs, "effective_agent",
                        (lambda s, **k: (s or {}).get("agent") or default) if per_row
                        else (lambda *a, **k: default))
    if cmp:
        monkeypatch.setattr(agent_constructs, "codex_model_provider",
                            (lambda s, **k: (s or {}).get("codex_model_provider") or cmp) if per_row
                            else (lambda *a, **k: cmp))
    if kind:
        monkeypatch.setattr(llm, "openai_endpoint_kind", lambda *a, **k: kind)


def _agent_raises(monkeypatch, mode):
    def f(settings, **k):
        if mode == "all" or settings is not None:
            raise RuntimeError("boom")
        return "ollama"
    monkeypatch.setattr(agent_constructs, "effective_agent", f)


def _results(rs):
    return {r.id: r for r in rs}


def _no_send(monkeypatch, calls=None):
    def f(system, user, cfg, timeout=None):
        if calls is not None:
            calls.append(cfg)
        raise AssertionError("実送信してはいけない構成で complete_json が呼ばれた")
    monkeypatch.setattr(graph_extract, "complete_json", f)


# ---------------------------------------------------------------------------
# 共通境界（秘密マスク・固定文言・ログ経由の伏せ字）
# ---------------------------------------------------------------------------

def test_sanitize_text_masks_secrets_strips_control_and_truncates():
    out = doctor_checks._sanitize_text("Authorization: Bearer \x1b[0msk-should-not-leak-123456789012345678")
    assert "sk-should-not-leak" not in out   # ANSI を挟んでも除去→マスクの順で伏せる
    out = doctor_checks._sanitize_text("line1\x1b[31mRED\x1b[0m\nline2\ttabbed")
    assert not {"\x1b", "\n", "\t"} & set(out)
    assert len(doctor_checks._sanitize_text("x" * 10000)) <= doctor_checks._MAX_DETAIL_CHARS + len("…（省略）")
    assert "\n" not in doctor_checks.CheckResult("id", "label", "ng", "a\nb\x1b[31m").detail


@pytest.mark.parametrize("fn", ["_fetch_system_settings_readonly", "_read_active_user_configs_readonly"])
def test_readonly_select_sets_statement_timeout(monkeypatch, fn):
    from sherpa.store import db as db_mod
    captured = {}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            class _C:
                def fetchall(self):
                    return []
            return _C()

    monkeypatch.setattr(db_mod, "_connect", lambda **kw: captured.update(kw) or _Conn())
    getattr(doctor_checks, fn)()
    assert captured["connect_timeout"] == doctor_checks._PG_READONLY_TIMEOUT
    assert captured["options"] == doctor_checks._PG_READONLY_OPTIONS


def test_guarded_check_returns_fixed_literal_never_exception_text():
    secret = "sk-realsecretvalue1234567890ABCDEFGH"
    for exc in (RuntimeError(f"leak {secret}"), TypeError("bad"), KeyError("missing")):
        @doctor_checks._guarded_check("some_check", "何かの検査")
        def _boom(exc=exc):
            raise exc
        r = _boom()
        assert (r.id, r.label, r.status, r.detail) == ("some_check", "何かの検査", "ng", _FIXED)

    @doctor_checks._guarded_check("some_check", "何かの検査")
    def _ok():
        return doctor_checks.CheckResult("some_check", "何かの検査", "ok", "問題ありません")
    assert _ok().detail == "問題ありません"


@pytest.mark.parametrize("name,level,msg,secret", [
    ("sherpa.health", logging.WARNING, "dsn password=my secret password here", "my secret password"),
    ("sherpa.agent_constructs", logging.WARNING, "SHERPA_AGENT raw secret leak test", "raw secret leak test"),
    ("httpx", logging.DEBUG, "Authorization: Bearer sk-realsecretvalue", "sk-realsecretvalue"),
])
def test_log_redaction_replaces_sherpa_and_httpx_messages(caplog, name, level, msg, secret):
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)   # 既存の root ハンドラがあっても効く
    try:
        with doctor_checks._log_redaction_active():
            with caplog.at_level(level, logger=name):
                logging.getLogger(name).log(level, msg)
            assert secret not in caplog.text
            assert "（doctor 実行中のため詳細は省略" in caplog.text
    finally:
        root.removeHandler(handler)


def test_log_redaction_leaves_other_loggers_and_clears_traceback(caplog):
    with doctor_checks._log_redaction_active():
        with caplog.at_level(logging.WARNING, logger="some_other_lib"):
            logging.getLogger("some_other_lib").warning("unrelated message stays intact")
        assert "unrelated message stays intact" in caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="sherpa.health"):
            try:
                raise ValueError("dsn password=my secret password here")
            except ValueError:
                logging.getLogger("sherpa.health").exception("failed", stack_info=True)
        assert "my secret password" not in caplog.text
        for rec in caplog.records:
            assert rec.exc_info is None and rec.exc_text is None and rec.stack_info is None


def test_log_redaction_is_scoped_reversible_and_reentrant():
    original = logging.Logger.callHandlers
    with doctor_checks._log_redaction_active():
        wrapper = logging.Logger.callHandlers
        assert wrapper is not original
        with doctor_checks._log_redaction_active():
            assert logging.Logger.callHandlers is wrapper
        assert logging.Logger.callHandlers is wrapper
    assert logging.Logger.callHandlers is original


# ---------------------------------------------------------------------------
# ストア疎通
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn", ["check_postgres", "check_neo4j"])
@pytest.mark.parametrize("ok,status", [(True, "ok"), (False, "ng")])
def test_store_connect(monkeypatch, fn, ok, status):
    monkeypatch.setattr(health, "_check_one", lambda *a: {
        "ok": ok, "detail": None if ok else "接続拒否（ConnectionRefusedError）", "hint": "make up"})
    r = getattr(doctor_checks, fn)()
    assert r.status == status
    assert "connection refused" not in r.detail.lower()   # 生の例外文字列を出さない


def test_es_connect_ok(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_es_get", lambda path, timeout=5.0: {"version": {"number": "8.19.20"}})
    r = doctor_checks.check_es_connect()
    assert r.status == "ok" and "8.19.20" in r.detail


@pytest.mark.parametrize("resp", [RuntimeError("refused"), None, 42, {"tagline": "x"}],
                         ids=["transport", "null", "number", "no-version"])
def test_es_connect_ng(monkeypatch, resp):
    def _get(path, timeout=5.0):
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(doctor_checks, "_es_get", _get)
    assert doctor_checks.check_es_connect().status == "ng"


def test_es_kuromoji_skip_when_es_unreachable():
    assert doctor_checks.check_es_kuromoji(es_ok=False).status == "skip"


@pytest.mark.parametrize("resp,status", [
    ([{"component": "analysis-kuromoji"}], "ok"),
    ([{"component": "analysis-icu"}], "ng"),
    ([{"component": "not-analysis-kuromoji-really"}], "ng"),   # 部分一致では OK にしない
    ({}, "ng"),
    (RuntimeError("boom"), "ng"),
], ids=["present", "absent", "substring", "not-list", "unreadable"])
def test_es_kuromoji(monkeypatch, resp, status):
    def _get(path, timeout=5.0):
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(doctor_checks, "_es_get", _get)
    assert doctor_checks.check_es_kuromoji(es_ok=True).status == status


# ---------------------------------------------------------------------------
# 設定の読み取りと接続先の妥当性
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("loader,fetch,value", [
    ("_load_system_settings", "_fetch_system_settings_readonly", {"cloud_provider": "openai"}),
    ("_load_active_user_configs", "_read_active_user_configs_readonly", [dict(_ROW_OLLAMA)]),
])
def test_load_settings_skip_ok_ng(monkeypatch, loader, fetch, value):
    load = getattr(doctor_checks, loader)
    check, got = load(False)
    assert check.status == "skip" and got is None
    monkeypatch.setattr(doctor_checks, fetch, lambda: value)
    check, got = load(True)
    assert check.status == "ok" and got == value

    def _boom():
        raise RuntimeError("permission denied for table x")
    monkeypatch.setattr(doctor_checks, fetch, _boom)
    check, got = load(True)   # 読み取り失敗は SKIP でなく NG（全項目 SKIP・exit 0 になる穴を塞ぐ）
    assert check.status == "ng" and got is None
    assert "permission denied" not in check.detail.lower()


@pytest.mark.parametrize("sys_s,env,status", [
    (None, {}, "skip"),
    ({}, {}, "ok"),
    ({}, {"SHERPA_OPENAI_ENDPOINT_KIND": "azure"}, "ng"),   # 初回シード前でも env 候補を検証する
    ({"openai_endpoint_seed_version": 1}, {}, "ok"),
    ({"openai_endpoint_seed_version": 1, "openai_endpoint_kind": "bogus"}, {}, "ng"),
])
def test_check_openai_endpoint(monkeypatch, sys_s, env, status):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert doctor_checks.check_openai_endpoint(sys_s).status == status


def test_openai_endpoint_status_merges_env_candidate_but_db_wins(monkeypatch):
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x.openai.azure.com/openai/v1")
    sys_s = {"openai_api_key": "sk-azure-key-1234567890"}
    r = doctor_checks._openai_endpoint_status(sys_s)
    assert r["status"] == "ok"
    assert r["effective_sys_s"]["openai_base_url"] == "https://x.openai.azure.com/openai/v1"
    assert "openai_base_url" not in sys_s   # 元の dict は書き換えない
    # DB に行があれば env 候補は無視する（本番のシードは既存行を上書きしない）
    monkeypatch.setenv("SHERPA_OPENAI_ENDPOINT_KIND", "azure")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://y.openai.azure.com/openai/v1")
    db = {"openai_endpoint_kind": "custom", "openai_base_url": "https://custom.example.com/v1"}
    r = doctor_checks._openai_endpoint_status(db)
    assert r["status"] == "ok"
    assert r["effective_sys_s"]["openai_base_url"] == "https://custom.example.com/v1"


def test_openai_endpoint_status_ng_when_merged_combo_invalid(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    r = doctor_checks._openai_endpoint_status({"openai_endpoint_kind": "azure"})   # base_url 無しの azure
    assert r["status"] == "ng" and r["effective_sys_s"] is None


def test_cloud_probes_zero_sends_when_endpoint_unresolved(monkeypatch):
    """接続先が不正／デプロイ名未登録の構成では、実送信の境界を一度も呼ばない。"""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x.openai.azure.com/openai/v1")
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    _agent(monkeypatch, "openai")
    _no_send(monkeypatch)
    monkeypatch.setattr(llm, "openai_post_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    st = doctor_checks._openai_endpoint_status({"cloud_provider": "openai", "openai_api_key": "sk-azure-1234567890"})
    res = _results(doctor_checks.check_cloud_llm_probes(st["effective_sys_s"], [], probe_cloud=True))
    assert res["llm_openai"].status == "ng" and "デプロイ名" in res["llm_openai"].detail
    bad = {"openai_endpoint_seed_version": 1, "openai_endpoint_kind": "bogus", "cloud_provider": "openai",
           "openai_api_key": _REAL}
    st = doctor_checks._openai_endpoint_status(bad)
    assert st["effective_sys_s"] is None
    assert {r.status for r in doctor_checks.check_cloud_llm_probes(None, [], probe_cloud=True)} == {"skip"}


# ---------------------------------------------------------------------------
# 実効頭脳の判定（判定不能は fail-closed）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["all", "rows"])
def test_effective_agent_failure_is_fail_closed(monkeypatch, mode):
    _agent_raises(monkeypatch, mode)
    rows = [{"agent": "openai", "codex_model_provider": None}]
    assert doctor_checks._agent_resolution_indeterminate({}, rows) is True
    assert doctor_checks._agent_actually_used("openai", {}, rows) is True
    assert doctor_checks._cloud_provider_consumed("openai", {}, rows) is True
    required, needs_auth, _ = doctor_checks._codex_required({}, rows)
    assert required is True and needs_auth is True


def test_agent_resolvable_is_not_indeterminate(monkeypatch):
    _agent(monkeypatch, "ollama")
    rows = [{"agent": "openai", "codex_model_provider": None}]
    assert doctor_checks._agent_resolution_indeterminate({}, rows) is False
    assert doctor_checks._agent_resolution_indeterminate({}, None) is False   # rows=None は別項目が NG を報告
    assert doctor_checks._agent_actually_used("openai", {}, []) is False


_KEYS_RAW = {"cloud_provider": "openai"}
_PERSONAL = {"cloud_provider": "openai", "personal_api_keys_allowed": True}


@pytest.mark.parametrize("agent,cmp,kind,sys_s,rows,expected", [
    ("ollama", None, None, {"cloud_provider": "openai", "openai_api_key": "sk-real-key"}, [], "ok"),
    ("openai", None, None, _KEYS_RAW, [{**_ROW_OLLAMA, "agent": "openai"}], "ng"),   # 欠落＋消費中
    ("openai", None, None, {**_KEYS_RAW, "openai_api_key": "sk-REPLACE_ME"}, [], "ng"),
    ("ollama", None, None, {**_KEYS_RAW, "openai_api_key": "sk-REPLACE_ME"}, [_ROW_OLLAMA], "ng"),   # 第2経路で消費
    ("ollama", None, None, {**_KEYS_RAW, "openai_api_key": "   "}, [_ROW_OLLAMA], "ng"),
    ("ollama", "ollama", None, {}, [_ROW_OLLAMA], "skip"),   # 一度も選んでいない＋未消費
    ("ollama", "ollama", None, _KEYS_RAW, [_ROW_OLLAMA], "ng"),   # 明示選択済みは未消費でも NG
    ("codex", "openai", "azure", _KEYS_RAW, [], "ng"),
    ("ollama", "openai", "azure", {}, [], "skip"),   # 残存した codex 設定だけでは消費扱いにしない
    ("ollama", None, None, _KEYS_RAW, None, "ng"),   # rows 不明は fail-closed
    ("openai", None, None, _PERSONAL, [{**_ROW_OLLAMA, "agent": "openai", "has_openai_key": True}], "ok"),
    ("openai", None, None, {**_PERSONAL, "personal_api_keys_allowed": False},
     [{**_ROW_OLLAMA, "agent": "openai", "has_openai_key": True}], "ng"),
])
def test_check_selected_provider_key(monkeypatch, agent, cmp, kind, sys_s, rows, expected):
    _agent(monkeypatch, agent, cmp, kind)
    r = doctor_checks.check_selected_provider_key(sys_s, rows)
    assert r.status == expected
    assert "sk-" not in r.detail


def test_check_selected_provider_key_skip_without_settings_and_indeterminate_ng(monkeypatch):
    assert doctor_checks.check_selected_provider_key(None, None).status == "skip"
    _agent_raises(monkeypatch, "all")
    r = doctor_checks.check_selected_provider_key({"cloud_provider": "openai", "openai_api_key": _REAL}, [])
    assert r.status == "ng" and r.detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL


@pytest.mark.parametrize("key,expected", [
    ("sk-real-key", True), ("sk-REPLACE_ME", False), (42, False), ({"a": 1}, False), (["a"], False), (True, False),
])
def test_central_auth_available_never_crashes_on_non_string(key, expected):
    assert doctor_checks._central_auth_available("openai", {"cloud_provider": "openai", "openai_api_key": key}) is expected


@pytest.mark.parametrize("allowed,rows,expected", [
    (False, [{"has_openai_key": True}], 0),
    (True, None, 0),
    (True, [{"has_openai_key": True}, {"has_openai_key": False}, {"has_openai_key": True}], 2),
])
def test_personal_key_holder_count(allowed, rows, expected):
    assert doctor_checks._personal_key_holder_count("openai", {"personal_api_keys_allowed": allowed}, rows) == expected


@pytest.mark.parametrize("agent,cmp,kind,blocked,provider,sys_s,rows,expected", [
    ("openai", None, None, None, "openai", {}, [], True),
    ("ollama", "ollama", "openai", None, "openai", {},
     [_ROW_OLLAMA, {**_ROW_OLLAMA, "agent": "codex", "codex_model_provider": "ollama"}], False),
    ("ollama", None, None, None, "openai", {}, None, True),
    ("ollama", "openai", "azure", None, "openai", {}, [], False),   # 実効頭脳が codex でなければ残存設定は無視
    ("codex", "openai", "azure", None, "openai", {}, [], True),
    ("gemini", None, None, True, "gemini", {}, [], False),   # チャットで閉じた頭脳は消費しない
    ("gemini", None, None, False, "gemini", {}, [], True),
    ("ollama", None, None, None, "openai", {"cloud_provider": "openai", "openai_api_key": _REAL}, [], True),   # 第2経路
])
def test_cloud_provider_consumed(monkeypatch, agent, cmp, kind, blocked, provider, sys_s, rows, expected):
    _agent(monkeypatch, agent, cmp, kind)
    if blocked is not None:
        monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda a: blocked)
    assert doctor_checks._cloud_provider_consumed(provider, sys_s, rows) is expected


@pytest.mark.parametrize("agent,sys_s,expected", [
    ("ollama", {}, []),
    ("openai", {}, ["chat"]),
    ("ollama", {"cloud_provider": "openai", "openai_api_key": _REAL}, ["intent", "render", "embed"]),
    ("openai", {"cloud_provider": "openai", "openai_api_key": _REAL}, ["chat", "intent", "render", "embed"]),
    ("ollama", {"cloud_provider": "openai", "openai_api_key": "sk-REPLACE_ME"}, ["intent", "render", "embed"]),
    ("ollama", {"cloud_provider": "openai", "openai_api_key": "   "}, ["intent", "render", "embed"]),
])
def test_consumed_llm_purposes(monkeypatch, agent, sys_s, expected):
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    _agent(monkeypatch, agent)
    assert doctor_checks._consumed_llm_purposes("openai", sys_s, []) == expected


@pytest.mark.parametrize("sys_s,rows,expected", [
    ({"cloud_provider": "openai", "openai_api_key": _REAL}, [], True),
    ({"cloud_provider": "openai"}, [], False),
    (_PERSONAL, [{"has_openai_key": False}, {"has_openai_key": True}], True),
    ({**_PERSONAL, "personal_api_keys_allowed": False}, [{"has_openai_key": True}], False),
    (_PERSONAL, None, True),   # rows 不明は消費している扱い
])
def test_second_path_truthy(sys_s, rows, expected):
    assert doctor_checks._second_path_truthy("openai", sys_s, rows) is expected


def test_second_path_purposes_excludes_embed_when_disabled(monkeypatch):
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    assert set(doctor_checks._second_path_purposes("openai")) == {"intent", "render"}


def test_classify_llm_probe_failure_never_leaks_dynamic_text():
    import socket
    import urllib.error
    classify = doctor_checks._classify_llm_probe_failure
    leaky = type("Leak_sk-SUPERSECRET1234567890ABCDEFGH", (RuntimeError,), {})
    assert classify(leaky("boom")) == "error（RuntimeError）"

    class _BadStatus(Exception):
        status_code = "sk-not-an-int-1234567890"

    class _BoomStatus(Exception):
        @property
        def status_code(self):
            raise RuntimeError("getter exploded")
    assert classify(_BadStatus("x")) == "error（UnknownError）"
    assert classify(_BoomStatus("x")) == "error"
    assert classify(urllib.error.HTTPError("https://x", 401, "U", {}, None)) == "auth status=401（HTTPError）"
    assert classify(urllib.error.HTTPError("https://x", 500, "I", {}, None)) == "http_5xx status=500（HTTPError）"
    assert classify(socket.gaierror("x")) == "dns（DNSError）"
    assert classify(TimeoutError()) == "timeout（TimeoutError）"


# ---------------------------------------------------------------------------
# クラウド LLM プローブ
# ---------------------------------------------------------------------------

def test_cloud_probes_skip_without_settings_and_when_billing_gate_closed():
    assert {r.status for r in doctor_checks.check_cloud_llm_probes(None, None, probe_cloud=True)} == {"skip"}
    res = _results(doctor_checks.check_cloud_llm_probes({"cloud_provider": "openai"}, [], probe_cloud=False))
    assert res["llm_openai"].status == "skip" and "PROBE_CLOUD" in res["llm_openai"].detail


def test_cloud_probes_indeterminate_agent_is_ng(monkeypatch):
    _agent_raises(monkeypatch, "all")
    res = _results(doctor_checks.check_cloud_llm_probes(
        {"cloud_provider": "openai", "openai_api_key": _REAL}, [], probe_cloud=True))
    assert set(res) == {"llm_openai"}
    assert res["llm_openai"].detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL


@pytest.mark.parametrize("registered", [False, True])
def test_cloud_probes_azure_deployment_static_check_without_probe_cloud(monkeypatch, registered):
    """chat 直結で Azure なのにデプロイ名が未登録なら、課金ゲートに関わらず送信せず NG。"""
    _agent(monkeypatch, "openai")
    _no_send(monkeypatch)
    if registered:
        monkeypatch.setattr(model_catalog, "resolve_model",
                            lambda provider, purpose, s, **k: f"my-{purpose}-deployment")
    sys_s = {"cloud_provider": "openai", **_AZ, "openai_api_key": "sk-real-key"}
    res = _results(doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=False))
    if registered:
        assert res["llm_openai"].status == "skip"
    else:
        assert res["llm_openai"].status == "ng" and "デプロイ名" in res["llm_openai"].detail


def test_cloud_probes_azure_static_check_scope(monkeypatch):
    """静的検査の対象は実効頭脳が openai の構成だけ（codex(ollama) のみは対象外・利用者行は走査）。"""
    sys_s = {"cloud_provider": "openai", **_AZ, "codex_model_provider": "ollama"}
    _agent(monkeypatch, "codex")
    assert _results(doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=False))["llm_openai"].status == "skip"
    _agent(monkeypatch, "codex", per_row=True)
    rows = [{"agent": "openai", "codex_model_provider": None}]
    res = _results(doctor_checks.check_cloud_llm_probes(sys_s, rows, probe_cloud=True))
    assert res["llm_openai"].status == "ng" and "デプロイ名" in res["llm_openai"].detail


def test_azure_deployment_reason_and_embed_static_check(monkeypatch):
    reason = doctor_checks._openai_azure_deployment_reason
    assert reason("chat", {"openai_api_key": "sk-real-key"}) is None
    assert doctor_checks._embed_static_check("openai", {"cloud_provider": "openai"}) is None
    assert "デプロイ名" in reason("chat", _AZ) and "チャット" in reason("chat", _AZ)
    emb = doctor_checks._embed_static_check("openai", _AZ)
    assert "デプロイ名" in emb and "埋め込み" in emb
    orig = model_catalog.resolve_model
    monkeypatch.setattr(model_catalog, "resolve_model", lambda p, purpose, s, **k:
                        "my-embed-deployment" if (p == "openai" and purpose == "embed") else orig(p, purpose, s, **k))
    assert reason("embed", _AZ) is None   # purpose ごとに独立して判定
    assert reason("chat", _AZ) is not None

    def _boom(*a, **k):
        raise TypeError("壊れた設定")
    monkeypatch.setattr(model_catalog, "resolve_model", _boom)
    assert reason("chat", _AZ) is not None   # 壊れたカタログは NG 理由として返す（伝播しない）
    assert doctor_checks._embed_static_check("openai", {}) is not None


def test_cloud_probes_intent_render_probed_without_static_ng(monkeypatch):
    _agent(monkeypatch, "ollama")
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    calls = []
    monkeypatch.setattr(graph_extract, "complete_json", lambda s, u, cfg, timeout=None: calls.append(cfg) or '{"ok":true}')
    sys_s = {"cloud_provider": "openai", **_AZ, "openai_api_key": _REAL}
    res = _results(doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True))
    assert res["llm_openai"].status == "ok" and len(calls) == 2   # intent + render


def test_cloud_probes_no_double_probe_for_codex_indirect_chat(monkeypatch):
    _agent(monkeypatch, "codex", cmp="openai", kind="openai")
    calls = []
    monkeypatch.setattr(graph_extract, "complete_json", lambda s, u, cfg, timeout=None: calls.append(cfg) or '{"ok":true}')
    res = _results(doctor_checks.check_cloud_llm_probes(
        {"cloud_provider": "openai", "openai_api_key": _REAL}, [], probe_cloud=True))
    assert res["llm_openai"].status == "ok" and len(calls) == 2   # chat は codex 経由の間接消費


def test_cloud_probes_probe_only_openai_when_gate_open(monkeypatch):
    _agent(monkeypatch, "openai")

    def _cj(system, user, cfg, timeout=None):
        assert cfg["provider"] == "openai"
        return '{"ok":true}'
    monkeypatch.setattr(graph_extract, "complete_json", _cj)
    res = _results(doctor_checks.check_cloud_llm_probes(
        {"cloud_provider": "openai", "openai_api_key": "sk-real-key"}, [], probe_cloud=True))
    assert set(res) == {"llm_openai"} and res["llm_openai"].status == "ok"


@pytest.mark.parametrize("msg", [
    "sk-abcdefgh1234567890ABCDEFGHIJK",
    "sk-ab\ncdefgh1234\t567890ABCDEFGHIJK",
    "sk-ab cdefgh1234-567890ABCDEFGHIJK",
], ids=["intact", "control-split", "space-split"])
def test_cloud_probes_failure_detail_never_contains_free_text(monkeypatch, msg):
    _agent(monkeypatch, "openai")
    secret = "sk-abcdefgh1234567890ABCDEFGHIJK"

    def _boom(system, user, cfg, timeout=None):
        assert cfg["key"] == secret
        raise RuntimeError(f"invalid key: {msg}")
    monkeypatch.setattr(graph_extract, "complete_json", _boom)
    res = _results(doctor_checks.check_cloud_llm_probes(
        {"cloud_provider": "openai", "openai_api_key": secret}, [], probe_cloud=True))
    assert res["llm_openai"].status == "ng"
    assert res["llm_openai"].detail == "接続に失敗しました: error（RuntimeError）"


def test_cloud_probes_personal_key_unused_and_placeholder_never_send(monkeypatch):
    calls = []
    _no_send(monkeypatch, calls)
    rows = [{"agent": "openai", "codex_model_provider": None, "search_helper": "", "has_openai_key": True}]
    res = _results(doctor_checks.check_cloud_llm_probes(_PERSONAL, rows, probe_cloud=True))
    assert res["llm_openai"].status == "skip" and "個人キー" in res["llm_openai"].detail
    # 中央キーも個人キーも実在しなければ送信前ガードで NG（送信しない）
    sys_s = {**_PERSONAL, "openai_api_key": "sk-REPLACE_ME"}
    rows = [{"agent": "codex", "codex_model_provider": "openai", "search_helper": "", "has_openai_key": False}]
    res = _results(doctor_checks.check_cloud_llm_probes(sys_s, rows, probe_cloud=True))
    assert res["llm_openai"].status == "ng"
    # 未使用の cloud_provider へは送らず SKIP
    _agent(monkeypatch, "ollama")
    res = _results(doctor_checks.check_cloud_llm_probes(_KEYS_RAW, [_ROW_OLLAMA], probe_cloud=True))
    assert res["llm_openai"].status == "skip" and "使われていません" in res["llm_openai"].detail
    assert calls == []


@pytest.mark.parametrize("key", [None, "", "sk-REPLACE_ME", {"unexpected": "object"}])
def test_run_raw_llm_probe_missing_key_sends_nothing(monkeypatch, key):
    calls = []
    _no_send(monkeypatch, calls)
    monkeypatch.setattr(keys, "resolve_api_key", lambda provider, s, **k: key)
    assert isinstance(doctor_checks._run_raw_llm_probe("openai", _KEYS_RAW), doctor_checks._MissingApiKeyError)
    assert calls == []


def test_run_raw_llm_probe_preparation_error_does_not_propagate(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("model_catalog is broken")
    monkeypatch.setattr(model_catalog, "resolve_model", _boom)
    e = doctor_checks._run_raw_llm_probe("openai", {"cloud_provider": "openai", "openai_api_key": "sk-real-key"})
    assert doctor_checks._classify_llm_probe_failure(e) == "error（RuntimeError）"


def test_retired_cloud_provider_reports_ng_and_sends_nothing(monkeypatch):
    calls = []
    _no_send(monkeypatch, calls)
    sys_s = {"cloud_provider": "bedrock", "openai_api_key": "sk-real-looking-key"}
    res = next(r for r in doctor_checks.check_cloud_llm_probes(sys_s, [], True) if r.id == "llm_openai")
    assert res.status == "ng" and "廃止されました" in res.detail
    assert doctor_checks._run_raw_llm_probe("openai", sys_s) is not None
    assert calls == []


# ---------------------------------------------------------------------------
# Codex の必須判定と worker モデル
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("settings,rows,agent,cmp,expected,note", [
    (None, None, None, None, (True, True), None),
    ({}, None, None, None, (True, True), None),
    ({}, [], "codex", "openai", (True, True), "0"),
    ({}, [{"agent": "openai", "codex_model_provider": None},
          {"agent": "codex", "codex_model_provider": "openai"}], "openai", "openai", (True, True), "2"),
    ({}, [{"agent": "ollama", "codex_model_provider": None, "user_id": "alice"},
          {"agent": "openai", "codex_model_provider": None}], "ollama", "openai", (False, False), "!alice"),
    ({}, [{"agent": "codex", "codex_model_provider": "ollama"}], "ollama", "openai", (True, False), None),
    ({}, [{"agent": "codex", "codex_model_provider": "ollama"},
          {"agent": "codex", "codex_model_provider": "openai"}], "ollama", "openai", (True, True), None),
])
def test_codex_required(monkeypatch, settings, rows, agent, cmp, expected, note):
    if agent:
        _agent(monkeypatch, agent, cmp, per_row=True)
    required, needs_auth, text = doctor_checks._codex_required(settings, rows)
    assert (required, needs_auth) == expected
    if note:
        assert (note[1:] not in text) if note.startswith("!") else (note in text)


@pytest.mark.parametrize("sys_s,rows,agent,cmp,status,detail", [
    (None, [], None, None, "skip", None),
    ({}, [], "ollama", None, "skip", None),
    ({}, [], "codex", "openai", "ok", None),
    ({"openai_base_url": "https://myres.openai.azure.com/openai/v1", "openai_endpoint_kind": "azure"},
     [], "codex", "openai", "ok", "デプロイ名"),
    ({"openai_endpoint_kind": "custom", "openai_base_url": "https://example.com/v1"},
     [], "codex", "openai", "skip", "custom"),
    ({**_AZ}, [], "codex", "ollama", "ok", None),
    ({**_AZ}, [{"agent": "codex", "codex_model_provider": "openai"}], "ollama", "openai", "ok", None),
    ({"codex_worker_model": "gpt-5.9-custom"}, [], "codex", "openai", "skip", "独自設定"),
])
def test_check_codex_multi_agent_worker_model(monkeypatch, sys_s, rows, agent, cmp, status, detail):
    if agent:
        _agent(monkeypatch, agent, cmp, per_row=True)
    r = doctor_checks.check_codex_multi_agent_worker_model(sys_s, rows)
    assert r.status == status
    if detail:
        assert detail in r.detail


# ---------------------------------------------------------------------------
# Ollama の用途別プローブ
# ---------------------------------------------------------------------------

def _ollama(monkeypatch, embed=None, simple=None, catalog=False, url="http://localhost:11434"):
    from sherpa import embeddings, simple_chat
    monkeypatch.setattr(embeddings, "cfg", lambda *a, **k: embed)
    monkeypatch.setattr(keys, "resolve_ollama_url", lambda s, **k: (s or {}).get("ollama_url") or url)
    if catalog:
        monkeypatch.setattr(model_catalog, "resolve_model", lambda provider, usage, *a, **k: f"{provider}-{usage}")
    if simple:
        monkeypatch.setattr(simple_chat, "resolve_model_and_provider", lambda m, sys_s, p=None: simple)


def test_resolve_ollama_usages_undeterminable_is_none(monkeypatch):
    assert doctor_checks._resolve_ollama_usages(None, []) is None
    assert doctor_checks._resolve_ollama_usages({}, None) is None
    _ollama(monkeypatch, catalog=True)
    _agent(monkeypatch, "codex", per_row=True)
    bad_url = [{"agent": "codex", "codex_model_provider": "ollama", "ollama_url": {"unexpected": "object"}}]
    assert doctor_checks._resolve_ollama_usages({}, bad_url) is None   # 非文字列の URL は黙って無視しない

    def _boom(*a, **k):
        raise TypeError("壊れた設定")
    monkeypatch.setattr(model_catalog, "resolve_model", _boom)
    assert doctor_checks._resolve_ollama_usages({}, []) is None
    _ollama(monkeypatch)
    _agent_raises(monkeypatch, "rows")
    assert doctor_checks._resolve_ollama_usages({}, [dict(_ROW_OLLAMA)]) is None
    from sherpa import embeddings
    _agent(monkeypatch, "openai")
    monkeypatch.setattr(embeddings, "cfg", _boom)
    assert doctor_checks._resolve_ollama_usages({}, [dict(_ROW_OLLAMA, agent="openai")]) is None
    assert doctor_checks._resolve_ollama_usages({"research_default_provider": "bogus"}, []) is None


def test_resolve_ollama_usages_per_purpose(monkeypatch):
    _ollama(monkeypatch, simple=("openai", "gpt-sub"), catalog=True)
    _agent(monkeypatch, "openai", per_row=True)
    openai_row = {"agent": "openai", "codex_model_provider": None, "ollama_url": None, "search_helper": ""}
    sys_o = {"research_default_provider": "openai"}
    assert doctor_checks._resolve_ollama_usages(sys_o, [openai_row]) == []
    # Codex(Ollama) は利用者ごとの URL とモデルで検査し、同じ (URL, モデル) は 1 件にまとめる
    codex = {"agent": "codex", "codex_model_provider": "ollama", "ollama_url": "http://personal:11434"}
    u = doctor_checks._resolve_ollama_usages(sys_o, [codex, dict(codex)])
    assert [(x["url"], x["model"]) for x in u] == [("http://personal:11434", "codex-codex")]
    assert "Codex" in u[0]["purposes"][0]


def test_resolve_ollama_usages_simple_ai_is_probed_even_when_all_use_codex_openai(monkeypatch):
    _ollama(monkeypatch, simple=("ollama", "subsearch-model"))
    sys_s = {"research_default_provider": "ollama", "ollama_url": "http://localhost:11434"}
    u = doctor_checks._resolve_ollama_usages(sys_s, [{"agent": "codex", "codex_model_provider": "openai",
                                                       "ollama_url": None}])
    assert [(x["url"], x["purposes"]) for x in u] == [("http://localhost:11434", ["簡易（検索して答える）"])]


def test_resolve_ollama_usages_embed_only_when_resolved_to_ollama(monkeypatch):
    _ollama(monkeypatch)
    _agent(monkeypatch, "openai", per_row=True)
    openai_row = {"agent": "openai", "codex_model_provider": None, "ollama_url": None, "search_helper": ""}
    sys_o = {"research_default_provider": "openai"}
    _ollama(monkeypatch, embed={"provider": "ollama", "url": "http://localhost:11434", "model": "nomic-embed-text"})
    u = doctor_checks._resolve_ollama_usages(sys_o, [openai_row])
    assert [(x["model"], x["purposes"]) for x in u] == [("nomic-embed-text", ["埋め込み（ベクトル検索）"])]
    _ollama(monkeypatch, embed={"provider": "openai", "key": "sk-x"})
    assert doctor_checks._resolve_ollama_usages(sys_o, [openai_row]) == []


@pytest.mark.parametrize("tags,ref,ok,has,hasnt", [
    (["qwen2.5:latest"], "qwen2.5", True, ["qwen2.5:latest"], []),
    (["qwen2.5:7b"], "qwen2.5", False, ["qwen2.5:latest", "qwen2.5:7b"], []),   # タグ違いは NG
    (["llama3:latest"], "qwen2.5", False, ["pull"], []),
    (["qwen2.5:latest"], "qwen2.5:7b", False, ["qwen2.5:7b"], []),
    (["library/qwen2.5:latest"], "qwen2.5", True, [], []),
    (["registry.ollama.ai/library/qwen2.5:latest"], "qwen2.5", True, [], []),
    (["QWen2.5:LATEST"], "qwen2.5", True, [], []),
    (["qwen2.5:latest"], "qwen2.5:", False, ["不正"], []),
    (["qwen2.5:latest"], "registry.example.com:5000/qwen2.5", False, ["不正"], ["pull"]),
])
def test_probe_ollama_usage_tag_matching(monkeypatch, tags, ref, ok, has, hasnt):
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda url, timeout=None: _Resp(_tags(*tags)))
    got_ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", ref, {})
    assert got_ok is ok
    assert all(s in detail for s in has) and not any(s in detail for s in hasnt)


@pytest.mark.parametrize("payload", [b"null", b"42", b'{"models": 1}', b'{"models": [1, 2, 3]}', b"not even json"])
def test_probe_ollama_usage_malformed_response_is_ng(monkeypatch, payload):
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda url, timeout=None: _Resp(payload))
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", {})
    assert ok is False and detail


def test_probe_ollama_usage_hides_raw_error_and_credentials(monkeypatch):
    def _boom(url, timeout=None):
        raise RuntimeError("http://user:hunter2@localhost:11434/api/tags failed")
    monkeypatch.setattr(llm, "urlopen_no_redirect", _boom)
    ok, detail = doctor_checks._probe_ollama_usage("http://user:hunter2@localhost:11434", "qwen2.5", {})
    assert ok is False and "hunter2" not in detail and "localhost:11434" in detail
    ok, detail = doctor_checks._probe_ollama_usage(123, "qwen2.5", {})   # 非文字列 URL でも落ちない
    assert ok is False and detail


def test_probe_ollama_usage_passes_system_settings(monkeypatch):
    captured = {}

    def _url(base, path, *, extra_allowed=None, system_settings=None):
        captured["s"] = system_settings
        return base + path
    monkeypatch.setattr(llm, "ollama_url", _url)
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda url, timeout=None: _Resp(_tags("qwen2.5:latest")))
    sentinel = {"cloud_provider": "openai"}
    doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", sentinel)
    assert captured["s"] is sentinel


@pytest.mark.parametrize("ref,expected", [
    ("registry.ollama.ai/qwen2.5", None),
    ("http://qwen2.5", None),
    ("registry.example.com:5000/qwen2.5", None),
    ("qwen2.5\n", None),
    ("myorg/qwen2.5:7b\n", None),
    ("https://registry.example.com/myorg/qwen2.5:7b", ("registry.example.com", "myorg", "qwen2.5", "7b")),
    ("registry.example.com:5000/myorg/qwen2.5:7b", ("registry.example.com:5000", "myorg", "qwen2.5", "7b")),
])
def test_normalize_ollama_ref(ref, expected):
    assert doctor_checks._normalize_ollama_ref(ref) == expected


def test_check_ollama_probes_ng_skip_and_one_item_per_usage(monkeypatch):
    assert doctor_checks.check_ollama_probes(None, None)[0].status == "ng"
    monkeypatch.setattr(doctor_checks, "_resolve_ollama_usages", lambda *a, **k: [])
    assert doctor_checks.check_ollama_probes({}, [])[0].status == "skip"
    monkeypatch.setattr(doctor_checks, "_resolve_ollama_usages", lambda *a, **k: [
        {"url": "http://a:11434", "model": "m1", "purposes": ["用途A"]},
        {"url": "http://b:11434", "model": "m2", "purposes": ["用途B"]}])
    seen = []
    monkeypatch.setattr(doctor_checks, "_probe_ollama_usage", lambda url, model, s: seen.append(s) or (True, "ok"))
    sentinel = {"cloud_provider": "openai"}
    rs = doctor_checks.check_ollama_probes(sentinel, [])
    assert len({r.id for r in rs}) == 2 and all(s is sentinel for s in seen)


def test_check_ollama_probes_ng_for_missing_embed_model(monkeypatch):
    _agent(monkeypatch, "openai")
    _ollama(monkeypatch, embed={"provider": "ollama", "url": "http://localhost:11434", "model": "nomic-embed-text"})
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda url, timeout=None: _Resp(_tags("qwen2.5:latest")))
    rs = doctor_checks.check_ollama_probes({"research_default_provider": "openai"},
                                           [dict(_ROW_OLLAMA, agent="openai")])
    assert len(rs) == 1 and rs[0].status == "ng" and "pull" in rs[0].detail


# ---------------------------------------------------------------------------
# Codex 経路
# ---------------------------------------------------------------------------

def _codex_cli(monkeypatch, *, found=True, rc=0, login_exc=None):
    class _P:
        returncode = rc
        stdout = "codex-cli 0.144.1\n" if rc == 0 else ""
        stderr = "" if rc == 0 else "unknown flag"
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex" if found else None)
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _P())

    def _login(settings, sys_s):
        if login_exc:
            raise login_exc
    monkeypatch.setattr(health, "_ai_check_codex", _login)


_LEAK = RuntimeError("Authorization: Bearer sk-should-not-leak-123456789012345678")


@pytest.mark.parametrize("kw,args,expected", [
    (dict(found=False), (None, None, False, False), ("skip", "skip", "skip")),
    (dict(found=False), ({"agent": "codex"}, [], True, True), ("ng", None, None)),
    (dict(), ({}, None, False, True), ("ok", "ok", "ok")),
    (dict(rc=1), ({"agent": "codex"}, [], True, True), (None, "ng", None)),
    (dict(rc=1), (None, None, False, False), (None, "skip", None)),
    (dict(login_exc=_LEAK), ({"agent": "codex"}, [], True, False), ("ok", "ok", "skip")),   # Ollama のみは認証確認しない
    (dict(login_exc=_LEAK), ({"agent": "codex"}, [], True, True), (None, None, "ng")),
    (dict(login_exc=_LEAK), ({}, None, False, True), (None, None, "skip")),
    (dict(), (None, [], True, True), ("ok", "ok", "skip")),   # 接続先未確定なら認証確認も送信もしない
])
def test_check_codex(monkeypatch, kw, args, expected):
    _codex_cli(monkeypatch, **kw)
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("not expected")))
    monkeypatch.setattr(doctor_checks, "_run_raw_llm_probe",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    sys_s, rows, required, needs_auth = args
    rs = _results(doctor_checks.check_codex(sys_s, rows, required=required, needs_openai_auth=needs_auth,
                                            note="", probe_cloud=True))
    got = (rs["codex_cli"].status, rs["codex_version"].status, rs["codex_auth"].status)
    assert all(e is None or e == g for e, g in zip(expected, got)), got
    assert "sk-should-not-leak" not in rs["codex_auth"].detail
    if expected == ("ok", "ok", "ok"):
        assert "0.144.1" in rs["codex_version"].detail


def test_check_codex_indeterminate_is_ng_even_when_cli_ok(monkeypatch):
    _codex_cli(monkeypatch)
    rs = doctor_checks.check_codex({}, [], required=True, needs_openai_auth=True, note="",
                                   probe_cloud=False, indeterminate=True)
    assert {r.id for r in rs} == {"codex_cli", "codex_version", "codex_auth"}
    assert all(r.status == "ng" and r.detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL for r in rs)


def test_check_codex_auth_not_sent_when_endpoint_ng(monkeypatch):
    _agent(monkeypatch, "codex", cmp="openai")
    monkeypatch.setattr(doctor_checks, "_run_raw_llm_probe",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: None)
    st = doctor_checks._openai_endpoint_status({"cloud_provider": "openai", "openai_endpoint_kind": "bogus",
                                                "openai_endpoint_seed_version": 1, "openai_api_key": _REAL})
    assert st["status"] == "ng"
    rs = _results(doctor_checks.check_codex(st["effective_sys_s"], [], required=True, needs_openai_auth=True,
                                            note="", probe_cloud=True))
    assert rs["codex_auth"].status == "skip"


_AZ_KEYED = {**_AZ, "openai_api_key": "sk-real-key"}


@pytest.mark.parametrize("reason,required,probe_cloud,status,detail", [
    ("デプロイ名が未設定です", True, False, "ng", "デプロイ名"),
    (None, True, False, "skip", None),   # 設定形式は妥当・実接続は PROBE_CLOUD 待ち
    ("キー未設定", False, False, "skip", None),
    (None, True, True, "ok", None),
])
def test_codex_auth_azure_backing(monkeypatch, reason, required, probe_cloud, status, detail):
    monkeypatch.setattr(health, "_ai_check_codex", lambda *a: (_ for _ in ()).throw(AssertionError("login")))
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", lambda s, **k: reason)
    seen = {}
    monkeypatch.setattr(graph_extract, "complete_json",
                        lambda system, user, cfg, timeout=None: seen.update(t=timeout) or '{"ok":true}')
    r = doctor_checks._check_codex_auth(_AZ_KEYED, None, required=required, note="", probe_cloud=probe_cloud)
    assert r.status == status
    if detail:
        assert detail in r.detail
    if probe_cloud:
        assert seen["t"] == doctor_checks._CODEX_TIMEOUT   # doctor 専用の短い timeout


def test_codex_auth_invalid_kind_and_unexpected_exception_are_ng(monkeypatch):
    assert doctor_checks._check_codex_auth({"openai_endpoint_kind": "bogus"}, None, required=True,
                                           note="", probe_cloud=False).status == "ng"

    def _boom(s, **k):
        raise TypeError("sk-realsecretvalue1234567890ABCDEFGH")
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", _boom)
    r = doctor_checks._check_codex_azure_compat(_AZ_KEYED, None, required=True, probe_cloud=False)
    assert (r.status, r.id, r.detail) == ("ng", "codex_auth", _FIXED)


@pytest.mark.parametrize("msg", [
    "sk-abcdefgh1234567890ABCDEFGHIJK",
    "sk-ab\ncdefgh1234\t567890ABCDEFGHIJK",
    "sk-ab cdefgh1234-567890ABCDEFGHIJK",
], ids=["intact", "control-split", "space-split"])
def test_codex_auth_probe_failure_detail_never_contains_free_text(monkeypatch, msg):
    secret = "sk-abcdefgh1234567890ABCDEFGHIJK"
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", lambda s, **k: None)
    monkeypatch.setattr("sherpa.keys.resolve_api_key", lambda provider, s, **k: secret)

    def _boom(system, user, cfg, timeout=None):
        raise RuntimeError(f"invalid key: {msg}")
    monkeypatch.setattr(graph_extract, "complete_json", _boom)
    r = doctor_checks._check_codex_auth({**_AZ, "openai_api_key": secret}, None, required=True, note="", probe_cloud=True)
    assert r.status == "ng" and r.detail == "接続に失敗しました: error（RuntimeError）"


_NOKEY = f"{keys.NO_CENTRAL_KEY_MESSAGE}（Azure 等の接続先の認証にも使います）"


@pytest.mark.parametrize("reason_fn,status,has,hasnt", [
    (lambda explicit: None if explicit else _NOKEY, "skip", "個人キー", None),   # キーだけが不足なら個人キーで救済
    (lambda explicit: "デプロイ名を登録してください" if explicit else _NOKEY, "ng", "デプロイ名", "個人キー"),
    (lambda explicit: "Codex サンドボックス有効時のみ対応です", "ng", "サンドボックス", None),
])
def test_codex_auth_personal_key_only_rescues_missing_key(monkeypatch, reason_fn, status, has, hasnt):
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason",
                        lambda s, *, explicit_openai_api_key=None, **k: reason_fn(explicit_openai_api_key))
    sys_s = {**_AZ, "personal_api_keys_allowed": True}
    rows = [{"agent": "codex", "codex_model_provider": "openai", "search_helper": "", "has_openai_key": True}]
    r = doctor_checks._check_codex_auth(sys_s, rows, required=True, note="", probe_cloud=True)
    assert r.status == status and has in r.detail and (hasnt is None or hasnt not in r.detail)


def _fake_codex(tmp_path, *, out=(), err=(), rc=0):
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    p = d / "codex"
    body = ["#!/bin/sh"] + [f"echo {shlex.quote(x)}" for x in out] + [f"echo {shlex.quote(x)} >&2" for x in err]
    p.write_text("\n".join(body + [f"exit {rc}"]) + "\n", encoding="utf-8")
    os.chmod(p, 0o755)
    return d


@pytest.mark.parametrize("out,err,rc,status,substring", [
    (("SANDBOX_OK", "RG_OK", "KB_READONLY"), (), 0, "ok", "rg あり"),
    (("SANDBOX_OK", "RG_OK", "KB_WRITABLE"), (), 0, "ng", "書き込めてしまいます"),
    ((), ("bwrap: Failed RTM_NEWADDR: Operation not permitted",), 1, "ng",
     "sudo bash scripts/setup-codex-sandbox.sh apply"),
    ((), ("bwrap: execvp /opt/codex/codex: No such file or directory",), 1, "ng", "導入先"),
], ids=["ok", "kb-writable", "rtm-newaddr", "execvp"])
def test_check_codex_sandbox_classifies_fake_codex(tmp_path, monkeypatch, out, err, rc, status, substring):
    monkeypatch.setenv("PATH", f"{_fake_codex(tmp_path, out=out, err=err, rc=rc)}{os.pathsep}{os.environ.get('PATH', '')}")
    r = doctor_checks.check_codex_sandbox(codex_required=True)
    assert r.status == status and substring in r.detail


def test_check_codex_sandbox_disabled_ng_and_not_required_skip(monkeypatch):
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    r = doctor_checks.check_codex_sandbox(codex_required=True)
    assert r.status == "ng" and "SHERPA_CODEX_SANDBOX" in r.detail
    assert doctor_checks.check_codex_sandbox(codex_required=False).status == "skip"


# ---------------------------------------------------------------------------
# 無効化された頭脳構成・レポート・終了コード
# ---------------------------------------------------------------------------

def test_disabled_agent_configs(monkeypatch):
    assert doctor_checks._disabled_agent_configs(None, None).status == "skip"
    _agent(monkeypatch, "gemini", per_row=True)
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda a: a in ("gemini", "bedrock"))
    rows = [{"agent": "gemini", "codex_model_provider": None, "user_id": "user-should-not-leak"},
            {"agent": "bedrock", "codex_model_provider": None},
            {"agent": "openai", "codex_model_provider": None}]
    r = doctor_checks._disabled_agent_configs({}, rows)
    assert r.status == "ng" and "3" in r.detail and "user-should-not-leak" not in r.detail
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda a: False)
    assert doctor_checks._disabled_agent_configs({}, rows).status == "ok"


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("no", False), ("1", True), ("true", True), ("YES", True), ("on", True),
])
def test_probe_cloud_enabled_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PROBE_CLOUD", raising=False)
    else:
        monkeypatch.setenv("PROBE_CLOUD", value)
    assert doctor_checks.probe_cloud_enabled() is expected


def test_format_report_and_exit_code(monkeypatch):
    mk = doctor_checks.CheckResult
    assert "OK=1 NG=1 SKIP=1" in doctor_checks.format_report([mk("a", "A", "ok", "d"), mk("b", "B", "ng", "d"),
                                                              mk("c", "C", "skip", "d")])
    monkeypatch.setattr(doctor_checks, "run_all", lambda **k: [mk("a", "A", "ng", "d")])
    assert doctor_checks.main([]) == 1
    monkeypatch.setattr(doctor_checks, "run_all", lambda **k: [mk("a", "A", "ok", "d"), mk("b", "B", "skip", "d")])
    assert doctor_checks.main([]) == 0
