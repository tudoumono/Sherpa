"""Ollama 接続先の許可リスト（SSRF 封じ）契約テスト。

`llm.ollama_url()` は Ollama 接続の単一チョークポイント。ここでは:
  1. sherpa/ 全体を静的に走査し、Ollama REST パスが `llm.ollama_url()` を迂回して組み立てられていないこと
  2. 既知のシンクが実際にチョークポイントを参照していること
  3. 非 allowlist URL で各シンクを呼んでも通信が一切発生せず安全に degrade すること
  4. `llm._canonical_host_port`／`assert_ollama_url_allowed` の正規化・許可判定
  5. `llm.no_proxy_requests()` と opener の構成

外部サービス不要（`store.get_system_settings` は monkeypatch で差し替える）。
"""
from __future__ import annotations

import json
import pathlib
import re
import urllib.request

import pytest

from sherpa import embeddings, graph_admin, health, intent_llm, llm, store
from sherpa.ingest import graph_extract

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHERPA = ROOT / "sherpa"


class _NetworkGuard:
    """`urlopen(...)` の呼び出しを記録してから拒否する（呼ばれたかどうかを確定的に検出するため）。"""

    def __init__(self):
        self.calls: list = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        raise AssertionError("非allowlist URL なのにネットワークへ出た（llm.ollama_url のチョークポイントを迂回している）")


@pytest.fixture(autouse=True)
def _no_network_by_default(monkeypatch):
    """`urlopen` と `OpenerDirector.open`（`llm.urlopen_no_redirect` の内部経路）の両方を guard にする。

    シンク側の broad except が拒否例外を吸収するため、各テストは `guard.calls == []` で
    「一度も呼ばれていないこと」を確認する。
    """
    guard = _NetworkGuard()
    monkeypatch.setattr(urllib.request, "urlopen", guard)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", guard)
    return guard


@pytest.fixture(autouse=True)
def _empty_admin_allowlist(monkeypatch):
    """allowlist 空（loopback 以外は全拒否）に固定する。"""
    monkeypatch.setattr(store, "get_system_settings", lambda: {})


def _set_allowlist(monkeypatch, entries):
    monkeypatch.setattr(store, "get_system_settings", lambda: {"ollama_allowlist": entries})


# ===== 1. grep 網羅（新シンクの迂回検出） =====

# 引用符で囲まれたリテラルとしての Ollama REST パスだけを対象にする（コメント・docstring は対象外）。
_OLLAMA_PATH_LITERAL_RE = re.compile(r"""(["'])(/api/(?:chat|tags|embed|generate|pull|show))\1""")
_EVASION_FRAGMENT_RE = re.compile(r"""(["'])/api/\1""")     # 末尾を欠く分割リテラル
_URLJOIN_RE = re.compile(r"\burljoin\s*\(")


def test_ollama_api_path_literals_only_appear_beside_ollama_url_chokepoint():
    """Ollama REST パスのリテラルが現れる行は、同じ行に `ollama_url(` を伴う（llm.py 自身は対象外）。"""
    violations = []
    for path in sorted(SHERPA.rglob("*.py")):
        if path == SHERPA / "llm.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _OLLAMA_PATH_LITERAL_RE.search(line) and "ollama_url(" not in line:
                violations.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not violations, (
        "Ollama API パスが llm.ollama_url() を経由せず直接構築されている可能性:\n" + "\n".join(violations))


def test_no_ollama_url_evasion_patterns():
    """分割リテラル（`"/api/"`）・`urljoin(` による Ollama URL 構築が sherpa/（llm.py 除く）に無い。"""
    violations = []
    for path in sorted(SHERPA.rglob("*.py")):
        if path == SHERPA / "llm.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if (_EVASION_FRAGMENT_RE.search(line) or _URLJOIN_RE.search(line)) and "ollama_url(" not in line:
                violations.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not violations, (
        "Ollama URL がチョークポイント（llm.ollama_url）を迂回して組み立てられている可能性"
        "（分割リテラル/urljoin）:\n" + "\n".join(violations))


_KNOWN_SINK_MODULES = {
    "embeddings.py": "embeddings（ES kNN 用ベクトル埋め込み）",
    "simple_chat.py": "simple_chat._resolve_llm（簡易の AI の接続先）",
    "ingest/graph_extract.py": "graph_extract.complete_json（intent_llm.classify も再利用）",
    "health.py": "health（状態ドット／AI 再チェック）",
    "ingest/arms/vision_arm.py": "vision_arm（VLM 画像読取）",
}


def test_known_sink_modules_reference_ollama_url_chokepoint():
    """列挙済みの既知シンクが `llm.ollama_url()` を呼んでいる（列挙の陳腐化の検出）。"""
    for rel, desc in _KNOWN_SINK_MODULES.items():
        text = (SHERPA / rel).read_text(encoding="utf-8")
        assert "llm.ollama_url(" in text, f"{rel}（{desc}）が llm.ollama_url() を経由していない"


def test_intent_llm_delegates_to_graph_extract_chokepoint():
    """intent_llm.classify は自前で Ollama URL を組み立てず `graph_extract.complete_json` に委譲する。"""
    text = (SHERPA / "intent_llm.py").read_text(encoding="utf-8")
    assert "llm.ollama_url(" not in text
    assert "complete_json" in text


# ===== 2. per-sink degrade（非 allowlist URL でネットワークへ出ず安全に degrade） =====

_UNLISTED_LAN = "http://192.168.1.99:11434"            # RFC1918・未登録
_UNLISTED_LINK_LOCAL = "http://169.254.169.254:11434"  # クラウドメタデータ相当・未登録
_UNLISTED_EXTERNAL = "http://ollama.example.com:11434"  # 外部ドメイン・未登録


def _only_ollama_candidate(monkeypatch):
    """env のクラウド鍵・kill-switch に auto 選択が引っ張られないよう外し、Ollama だけが候補になるようにする。"""
    for k in ("SHERPA_DISABLE_EMBED", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_embeddings_degrades_without_network_for_unlisted_url(monkeypatch, _no_network_by_default):
    _only_ollama_candidate(monkeypatch)
    c = embeddings.cfg({"ollama_url": _UNLISTED_LAN})
    assert c is not None and c["provider"] == "ollama"
    assert embeddings.embed(["hello"], c) is None            # ベクトル無効へ degrade（BM25 のみ）
    assert _no_network_by_default.calls == []


def test_graph_extract_probe_degrades_without_network_for_unlisted_url(_no_network_by_default):
    ok, detail = graph_extract._probe({"provider": "ollama", "url": _UNLISTED_EXTERNAL, "model": "qwen2.5"})
    assert ok is False and detail
    assert _no_network_by_default.calls == []


def test_intent_llm_classify_degrades_without_network_for_unlisted_url(monkeypatch, _no_network_by_default):
    _only_ollama_candidate(monkeypatch)
    assert intent_llm.classify("消費税率は?", {"ollama_url": _UNLISTED_LAN}) is None  # clarify へフォールバック
    assert _no_network_by_default.calls == []


def test_simple_provider_is_unwired_without_network_for_unlisted_url(_no_network_by_default):
    """許可外の接続先なら `_resolve_llm` で止まり頭脳は未接続（ナレッジ参照のオン/オフどちらも通信なし）。"""
    from sherpa import providers as providers_pkg
    from sherpa.agents import Ctx

    p = providers_pkg.get_provider({"agent": "simple"},
                                   system_settings={"ollama_url": _UNLISTED_EXTERNAL})
    assert isinstance(p, providers_pkg._UnwiredProvider)
    for knowledge in (True, False):
        ctx = Ctx(message="消費税率は?", world="v1", knowledge=knowledge,
                  route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
                  dispatch=lambda lens, inp: {}, make_sources=lambda docs: [])
        result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
        assert "接続されていません" in result["env"]["headline"]
    assert _no_network_by_default.calls == []


def test_select_provider_wires_simple_with_resolved_target_for_allowlisted_url():
    """許可リストにある接続先なら簡易の頭脳が組み立てられ、接続先は `llm.ollama_url` 経由の `/api/chat`。"""
    from sherpa import providers as providers_pkg

    p = providers_pkg.get_provider(
        {"agent": "simple"},
        system_settings={"ollama_url": _UNLISTED_LAN, "ollama_allowlist": [_UNLISTED_LAN.split("//")[1]]})
    assert type(p).__name__ == "SimpleProvider"
    assert p.provider_id == "ollama" and p._endpoint.endswith("/api/chat")


def test_select_provider_wires_same_system_settings_object_into_simple_provider(monkeypatch):
    """`_select_provider` が入口で読んだ system_settings と同一オブジェクトを SimpleProvider へ渡す（identity で確認）。"""
    from sherpa import agents, providers as providers_pkg

    sentinel = {"ollama_allowlist": ["192.168.1.80:11434"], "ollama_url": "http://192.168.1.80:11434",
                "research_default_provider": "ollama"}
    captured: list = []

    class _RecorderSimpleProvider:
        def __init__(self, provider, model, endpoint, headers, system_settings=None):
            captured.append(system_settings)

    monkeypatch.setattr(agents, "SimpleProvider", _RecorderSimpleProvider)
    providers_pkg.get_provider({"agent": "simple"}, system_settings=sentinel)
    assert len(captured) == 1, "SimpleProvider が構築されなかった"
    assert captured[0] is sentinel, "入口で読んだ system_settings と別オブジェクトが渡された"


def test_health_ping_ollama_degrades_without_network_for_malformed_central_url(monkeypatch, _no_network_by_default):
    """DB に不正な中央設定が残っていても `_ping_ollama` は通信せず ok=False に倒れる（多層防御）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"ollama_url": "not a url"})
    out = health._check_one("ollama", "ローカルLLM（Ollama）", "none", health._ping_ollama, "hint")
    assert out["ok"] is False
    assert _no_network_by_default.calls == []


def test_health_ai_check_ollama_degrades_without_network_for_unlisted_url(monkeypatch, _no_network_by_default):
    """per-user 設定の `_ai_check_ollama` は env の暗黙 allowlist を持たず、未登録の接続先は ok=False。"""
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    settings = {"ollama_url": _UNLISTED_LAN}
    out = health._check_one("ollama", "ローカルLLM（Ollama）", "none",
                            lambda: health._ai_check_ollama(settings), "hint")
    assert out["ok"] is False
    assert _no_network_by_default.calls == []


# ===== 3. 正規化・許可判定 =====

@pytest.mark.parametrize("url, expected", [
    ("http://[::1]:11434", ("::1", 11434)),
    ("http://example.com.:11434", ("example.com", 11434)),     # 末尾ドットは同一ホスト
    ("http://example.com:11434", ("example.com", 11434)),
    ("http://192.168.1.50", ("192.168.1.50", 80)),             # ポート省略は scheme の既定ポート
    ("https://example.com", ("example.com", 443)),
    ("http://192.168.1.50:8080", ("192.168.1.50", 8080)),
    ("https://192.168.1.50:11434", ("192.168.1.50", 11434)),   # scheme はタプルに含まれない
    ("http://127.0.0.1:11434/", ("127.0.0.1", 11434)),         # 末尾スラッシュのみは許可
    ("http://127.0.0.1:11434", ("127.0.0.1", 11434)),
    # 解釈不能は None
    ("http://admin:secret@192.168.1.50:11434", None),          # userinfo は黙って捨てず拒否
    ("http://admin@192.168.1.50:11434", None),
    ("ftp://192.168.1.50:11434", None),
    ("not a url", None),
    ("", None),
    ("http://127.0.0.1:11434/api/tags", None),                 # path/query/fragment は任意パス到達を防ぐため拒否
    ("http://127.0.0.1:11434?x=1", None),
    ("http://127.0.0.1:11434#frag", None),
])
def test_canonical_host_port(url, expected):
    assert llm._canonical_host_port(url) == expected


def test_canonical_host_port_ipv6_loopback_and_omitted_port_does_not_alias_ollama_port():
    assert llm.is_loopback_host("::1") is True
    # ポート省略（wire port は 80）が Ollama の正規ポート 11434 に化けない
    assert llm._canonical_host_port("http://192.168.1.50") != llm._canonical_host_port(
        "http://192.168.1.50:11434")


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://localhost:11434"])
def test_assert_ollama_url_allowed_loopback_always_passes_regardless_of_allowlist(url):
    llm.assert_ollama_url_allowed(url)


@pytest.mark.parametrize("url", [_UNLISTED_LAN, _UNLISTED_LINK_LOCAL, _UNLISTED_EXTERNAL])
def test_assert_ollama_url_allowed_blocks_unlisted_non_loopback(url):
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed(url)


def test_assert_ollama_url_allowed_passes_exact_admin_allowlist_entry(monkeypatch):
    _set_allowlist(monkeypatch, ["192.168.1.50:11434"])
    llm.assert_ollama_url_allowed("http://192.168.1.50:11434")   # host:port 完全一致
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://192.168.1.50:8080")  # ポート違いは別扱い


def test_assert_ollama_url_allowed_env_ollama_url_is_no_longer_implicit_member(monkeypatch):
    """`OLLAMA_URL` env は実行時の許可リストへ暗黙加算されない（UI で削除した接続先が env 経由で通る穴の再現）。"""
    monkeypatch.setenv("OLLAMA_URL", _UNLISTED_LAN)
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed(_UNLISTED_LAN)


def test_assert_ollama_url_allowed_extra_allowed_scopes_to_this_call_only():
    """`extra_allowed` は呼び出し単位の局所許可で、一般の allowlist を汚染しない。"""
    hp = llm._canonical_host_port(_UNLISTED_LAN)
    llm.assert_ollama_url_allowed(_UNLISTED_LAN, extra_allowed={hp})
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed(_UNLISTED_LAN)
    assert hp not in llm._allowlisted_hosts()


def test_assert_ollama_url_allowed_rejects_userinfo_even_for_loopback():
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://user:pass@localhost:11434")


def test_assert_ollama_url_allowed_malformed_url_does_not_leak_raw_value():
    """解釈不能な URL のエラー文言に生の base（資格情報）を埋め込まない（503 detail へ反射されるため）。"""
    for fn in (lambda u: llm.assert_ollama_url_allowed(u),
               lambda u: llm.assert_ollama_url_allowed_in(u, set())):
        with pytest.raises(llm.SsrfBlocked) as exc:
            fn("http://user:secret-password@localhost:11434")
        assert "secret-password" not in str(exc.value)
        assert "user:secret-password" not in str(exc.value)


def test_assert_ollama_url_allowed_omitted_port_does_not_alias_explicit_allowlist_entry(monkeypatch):
    """`host:11434` を登録しても、ポート省略の `http://host`（wire port 80）は許可されない（バイパスの再現）。"""
    _set_allowlist(monkeypatch, ["192.168.1.60:11434"])
    llm.assert_ollama_url_allowed("http://192.168.1.60:11434")
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://192.168.1.60")


def test_assert_ollama_url_allowed_rejects_path_query_fragment_even_for_loopback(monkeypatch):
    """loopback／allowlist 済みでも base に path・query・fragment が混入していれば拒否する。"""
    for bad in ("http://127.0.0.1:11434/api/tags", "http://127.0.0.1:11434?x=1",
                "http://127.0.0.1:11434#/api/chat"):
        with pytest.raises(llm.SsrfBlocked):
            llm.assert_ollama_url_allowed(bad)
    _set_allowlist(monkeypatch, ["192.168.1.61:11434"])
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://192.168.1.61:11434/secret")
    llm.assert_ollama_url_allowed("http://127.0.0.1:11434/")
    llm.assert_ollama_url_allowed("http://127.0.0.1:11434")


# ===== 4. `llm.no_proxy_requests()` と opener 構成 =====

def test_no_proxy_requests_selects_no_proxy_opener_for_urlopen_no_redirect(monkeypatch):
    """`with no_proxy_requests()` の内側だけ `_NO_REDIRECT_NO_PROXY_OPENER` が使われ、外側は既定に戻る。"""
    calls: list = []

    class _FakeOpener:
        def __init__(self, tag):
            self.tag = tag

        def open(self, req, timeout=None):
            calls.append(self.tag)
            return object()

    monkeypatch.setattr(llm, "_NO_REDIRECT_OPENER", _FakeOpener("proxy"))
    monkeypatch.setattr(llm, "_NO_REDIRECT_NO_PROXY_OPENER", _FakeOpener("no-proxy"))

    llm.urlopen_no_redirect("http://example.invalid")
    with llm.no_proxy_requests():
        llm.urlopen_no_redirect("http://example.invalid")
    llm.urlopen_no_redirect("http://example.invalid")
    assert calls == ["proxy", "no-proxy", "proxy"]


def test_no_redirect_openers_have_expected_proxy_and_redirect_handlers(monkeypatch):
    """本物の opener のハンドラ構成を検査する。no-proxy 側は ProxyHandler({}) で構築され `.handlers` に
    ProxyHandler が現れない（空 dict のハンドラは追加されない＝直結の構造的証拠）。"""
    def _handlers_of(opener, cls):
        return [h for h in opener.handlers if isinstance(h, cls)]

    assert llm._NO_REDIRECT_OPENER is not llm._NO_REDIRECT_NO_PROXY_OPENER

    for opener in (llm._NO_REDIRECT_OPENER, llm._NO_REDIRECT_NO_PROXY_OPENER):
        no_redirect_handlers = _handlers_of(opener, llm._NoRedirect)
        assert len(no_redirect_handlers) == 1
        assert no_redirect_handlers[0].redirect_request(None, None, 302, "Found", {}, "http://x") is None

    assert _handlers_of(llm._NO_REDIRECT_NO_PROXY_OPENER, urllib.request.ProxyHandler) == []

    # singleton は import 時の env で構築済みのため、HTTP_PROXY 設定後にファクトリで新規構築して確認する
    monkeypatch.setenv("HTTP_PROXY", "http://myproxy.internal:8080")
    monkeypatch.delenv("http_proxy", raising=False)
    assert _handlers_of(llm._build_no_proxy_opener(), urllib.request.ProxyHandler) == []

    # 対照: ProxyHandler を明示しない既定構築は env の proxy を拾う
    default_proxy_handlers = _handlers_of(urllib.request.build_opener(llm._NoRedirect), urllib.request.ProxyHandler)
    assert len(default_proxy_handlers) == 1
    assert default_proxy_handlers[0].proxies.get("http") == "http://myproxy.internal:8080"


# 新規プロセスで `sherpa.llm` を import し、モジュール読み込み時に構築される singleton 自体を検査する
# （ファクトリを迂回して singleton だけが書き換わる退行を検出するため）。
_SINGLETON_OPENER_CHECK_SCRIPT = """
import json
import urllib.request

import sherpa.llm as llm


def _handlers_of(opener, cls):
    return [h for h in opener.handlers if isinstance(h, cls)]


no_proxy_handlers = _handlers_of(llm._NO_REDIRECT_NO_PROXY_OPENER, urllib.request.ProxyHandler)
default_proxy_handlers = _handlers_of(llm._NO_REDIRECT_OPENER, urllib.request.ProxyHandler)
no_redirect_on_no_proxy = _handlers_of(llm._NO_REDIRECT_NO_PROXY_OPENER, llm._NoRedirect)
no_redirect_on_default = _handlers_of(llm._NO_REDIRECT_OPENER, llm._NoRedirect)

print(json.dumps({
    "no_proxy_opener_has_proxy_handler": len(no_proxy_handlers) > 0,
    "default_opener_has_proxy_handler": len(default_proxy_handlers) > 0,
    "default_opener_proxy_value": (
        default_proxy_handlers[0].proxies.get("http") if default_proxy_handlers else None),
    "no_proxy_opener_no_redirect_count": len(no_redirect_on_no_proxy),
    "default_opener_no_redirect_count": len(no_redirect_on_default),
    "no_proxy_opener_redirect_request_returns_none": (
        no_redirect_on_no_proxy[0].redirect_request(None, None, 302, "Found", {}, "http://x") is None
        if no_redirect_on_no_proxy else None),
}))
"""


def test_no_redirect_no_proxy_opener_singleton_ignores_http_proxy_env_at_fresh_import():
    """HTTP_PROXY 設定下で新規 import しても no-proxy singleton は proxy を拾わない（独立プロセスで確認）。
    subprocess の env は最小限にする（REQUEST_METHOD が残ると urllib が HTTP_PROXY を無視し対照が崩れる）。"""
    import os
    import subprocess
    import sys

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "HTTP_PROXY": "http://myproxy.internal:8080",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _SINGLETON_OPENER_CHECK_SCRIPT],
        env=env, capture_output=True, text=True, timeout=30, cwd=str(ROOT))
    assert proc.returncode == 0, f"subprocess が失敗: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result["no_proxy_opener_has_proxy_handler"] is False
    assert result["default_opener_has_proxy_handler"] is True
    assert result["default_opener_proxy_value"] == "http://myproxy.internal:8080"
    assert result["no_proxy_opener_no_redirect_count"] == 1
    assert result["default_opener_no_redirect_count"] == 1
    assert result["no_proxy_opener_redirect_request_returns_none"] is True


def test_embeddings_ollama_sends_post_json_body_within_no_proxy_requests_context(monkeypatch):
    """loopback の Ollama 埋め込みが `_NO_REDIRECT_NO_PROXY_OPENER.open()` まで到達し、POST・JSON ボディ・
    `no_proxy_requests()` コンテキスト内で送信され、応答が反映される（`with no_proxy_requests()` を外すと落ちる）。"""
    _only_ollama_candidate(monkeypatch)
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"embeddings": [[0.25, 0.5]]}'

    def _fake_open(req, timeout=None):
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["ctx"] = llm._no_proxy_ctx.get()
        return _Resp()

    monkeypatch.setattr(llm._NO_REDIRECT_NO_PROXY_OPENER, "open", _fake_open)

    c = embeddings.cfg({"ollama_url": "http://127.0.0.1:11434"})
    assert c is not None and c["provider"] == "ollama"
    c = {**c, "dim": 2}   # フェイク応答のベクトル長に合わせる
    assert embeddings.embed(["hello"], c) == [[0.25, 0.5]]
    assert captured.get("method") == "POST"
    assert captured.get("body") == {"model": c["model"], "input": ["hello"]}
    assert captured.get("ctx") is True, "no_proxy_requests() のコンテキスト外で送信された"
