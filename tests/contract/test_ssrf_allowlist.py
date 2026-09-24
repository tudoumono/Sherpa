"""R2a-S3（2026-07-13 横断レビュー対応・SSRF 封じ）契約テスト。

`llm.ollama_url()` は Ollama 接続の**単一チョークポイント**（R2a-S1）。ここでは:
  1. sherpa/ 全体を静的に走査し、Ollama REST パス文字列（"/api/chat" 等）が
     `llm.ollama_url()` を経由せず直接組み立てられている行が無いことを pin する
     （新シンクがチョークポイントを迂回したら本テストが落ちる）。
  2. 対象シンクとして列挙済みの6モジュール（agents=providers/ollama・embeddings・graph_admin・
     intent_llm・graph_extract・health）＋実装時に判明した7人目の consumer（vision_arm）が
     実際にチョークポイントを参照していることを確認する。
  3. 各シンクを非 allowlist URL（allowlist 空＝loopback 以外は全拒否）で呼び、ネットワーク呼び出しが
     一切発生せず（`urlopen`/`OpenerDirector.open`＝`llm.urlopen_no_redirect` の内部経路も含む・
     未呼び出し）、既存の broad except に乗って安全に degrade することを確認する。
  4. `llm._canonical_host_port`/`is_loopback_host`/`assert_ollama_url_allowed` の正規化 unit テスト。

外部サービス不要（DB/Neo4j/ES への実接続をしない・store.get_system_settings は monkeypatch で差し替える）。
"""
from __future__ import annotations

import json
import pathlib
import re
import urllib.request

import pytest

from sherpa import embeddings, graph_admin, health, intent_llm, llm, store
from sherpa.ingest import graph_extract
from sherpa.providers import ollama as ollama_provider

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHERPA = ROOT / "sherpa"


class _NetworkGuard:
    """`urlopen(...)` の呼び出しを記録してから拒否する（呼び出しの有無を確定的に検出するため）。"""

    def __init__(self):
        self.calls: list = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        raise AssertionError("非allowlist URL なのにネットワークへ出た（llm.ollama_url のチョークポイントを迂回している）")


@pytest.fixture(autouse=True)
def _no_network_by_default(monkeypatch):
    """`urllib.request.urlopen` を計測付きの guard に差し替える（既定でネットワーク不可）。

    RV: 単に例外を投げるだけの sentinel だと、シンク側の既存 broad `except Exception` が
    その例外自体を「degrade 成功」として吸収してしまい、`llm.ollama_url` のチョークポイントが
    実際には迂回されていても（＝本来ブロックすべき通信が発生していても）テストが誤って緑になる
    （例外の型ではなく「呼ばれたか」を見ないと、broad except の存在自体が偽陰性を作る）。
    ここでは呼び出しを `calls` に記録してから例外を投げる＝各テストは呼び出し後に
    `guard.calls == []`（本当に一度も呼ばれていないこと）を明示的に確認する。

    LOW（secRV 再RV・2026-07-14）: Ollama 経路（`llm.urlopen_no_redirect`）は redirect 非追跡のため
    `urllib.request.urlopen` を直接呼ばず、専用の `OpenerDirector`（`build_opener(_NoRedirect)`）の
    `.open()` を直接呼ぶ。`urlopen` だけを patch すると、この経路だけ guard を素通りしてしまい、
    「通信が起きたら必ず検知する」という本テストの前提が実態と一致しない。
    `urllib.request.OpenerDirector.open`（クラスメソッド）も合わせて patch し、どちらの経路で
    通信が発生しても必ず検知できるようにする。
    """
    guard = _NetworkGuard()
    monkeypatch.setattr(urllib.request, "urlopen", guard)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", guard)
    return guard


@pytest.fixture(autouse=True)
def _empty_admin_allowlist(monkeypatch):
    """`store.get_system_settings` を DB 非依存の空 dict に固定（allowlist 空＝loopback 以外は全拒否）。"""
    monkeypatch.setattr(store, "get_system_settings", lambda: {})


# ===== 1. grep 網羅（新シンクの迂回検出） =====

# Ollama の REST エンドポイント（`/api/chat`・`/api/tags`・`/api/embed`・その他公式 API）。
# 引用符で囲まれた**リテラル**としての出現だけを対象にする（コメント中の裸の言及や docstring の
# バッククォート表記は誤検知になるため対象外＝実際の呼び出しコードだけを狙い撃つ）。
_OLLAMA_PATH_LITERAL_RE = re.compile(r"""(["'])(/api/(?:chat|tags|embed|generate|pull|show))\1""")


def test_ollama_api_path_literals_only_appear_beside_ollama_url_chokepoint():
    """sherpa/ 内で Ollama REST パスのリテラルが現れる行は、必ず同じ行に `ollama_url(` を伴う。

    `llm.py` 自身（チョークポイントの定義・docstring の例示）は対象外。新シンクが
    `base + "/api/chat"` のように直接 URL を組み立てたら、この行だけ `ollama_url(` を伴わずに
    検出されテストが落ちる（break-and-confirm: `llm.ollama_url` 内の
    `assert_ollama_url_allowed(base)` 呼び出しを一時的にコメントアウトしても本テストは
    独立に検出対象が違うので落ちない＝これは「呼び出し経路」ではなく「経路の存在」を pin する
    テストであることに注意。経路自体の検証は下の per-sink degrade テスト側が担う）。
    """
    violations = []
    for path in sorted(SHERPA.rglob("*.py")):
        if path == SHERPA / "llm.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _OLLAMA_PATH_LITERAL_RE.search(line) and "ollama_url(" not in line:
                violations.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not violations, (
        "Ollama API パスが llm.ollama_url() を経由せず直接構築されている可能性:\n" + "\n".join(violations))


# RV Low（2026-07-14）: チョークポイント迂回の**別の組み立て方**（分割リテラル・urljoin）を pin する。
#   - `path = "/api/" + endpoint` のように REST パスを分割すると上の全リテラル正規表現を逃れる。
#   - `urljoin(base, "/api/chat")` も base+path のリテラル同居を避けられる。
# どちらも現状 sherpa/ に出現ゼロ＝新シンクがこの形で迂回を試みたら落ちる（誤検知ゼロを実測確認済み）。
_EVASION_FRAGMENT_RE = re.compile(r"""(["'])/api/\1""")     # 末尾を欠く分割リテラル `"/api/"`
_URLJOIN_RE = re.compile(r"\burljoin\s*\(")


def test_no_ollama_url_evasion_patterns():
    """分割リテラル（`"/api/"`）・`urljoin(` による Ollama URL 構築が sherpa/（llm.py 除く）に無い。"""
    violations = []
    for path in sorted(SHERPA.rglob("*.py")):
        if path == SHERPA / "llm.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if (_EVASION_FRAGMENT_RE.search(line) or _URLJOIN_RE.search(line)) and "ollama_url(" not in line:
                violations.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not violations, (
        "Ollama URL がチョークポイント（llm.ollama_url）を迂回して組み立てられている可能性"
        "（分割リテラル/urljoin）:\n" + "\n".join(violations))


# 対象シンクの列挙（R2a 事前分析）＋実装時に判明した7人目の consumer（vision_arm・R2a-S1・旧 markitdown_ocr_arm）。
_KNOWN_SINK_MODULES = {
    "providers/ollama.py": "OllamaProvider（agents の agentic ループ／単発ストリーミング頭脳）",
    "embeddings.py": "embeddings（ES kNN 用ベクトル埋め込み）",
    "graph_admin.py": "graph_admin.ask_graph（管理グラフへの自然言語質問）",
    "ingest/graph_extract.py": "graph_extract.complete_json（知識抽出。intent_llm.classify も再利用）",
    "health.py": "health（状態ドット `_ping_ollama` ／システム状態 AI 再チェック `_ai_check_ollama`）",
    "ingest/arms/vision_arm.py": "vision_arm（VLM 画像読取・R2a-S1 実装時に判明した7人目・旧 markitdown_ocr_arm）",
}


def test_known_sink_modules_reference_ollama_url_chokepoint():
    """列挙済みの既知シンクが実際に `llm.ollama_url()` を呼んでいることを確認する（正の存在確認）。

    上のテストが「新シンクの迂回」（不在の検出）を担うのに対し、こちらは「列挙した既知シンクが
    経路を外れていないか」（列挙自体の陳腐化）を確認する。
    """
    for rel, desc in _KNOWN_SINK_MODULES.items():
        text = (SHERPA / rel).read_text(encoding="utf-8")
        assert "llm.ollama_url(" in text, f"{rel}（{desc}）が llm.ollama_url() を経由していない"


def test_intent_llm_delegates_to_graph_extract_chokepoint():
    """intent_llm.classify は自前で Ollama URL を組み立てず、`graph_extract.complete_json`
    （チョークポイント経由・上のテストで確認済み）に委譲する設計を保つ（抽出層と同じ規約の再利用）。
    """
    text = (SHERPA / "intent_llm.py").read_text(encoding="utf-8")
    assert "llm.ollama_url(" not in text, (
        "intent_llm.py が自前で ollama_url を呼び始めている（graph_extract への委譲前提が崩れている）")
    assert "complete_json" in text


# ===== 2. per-sink degrade（非 allowlist URL で呼んでもネットワークへ出ず安全に degrade） =====

_UNLISTED_LAN = "http://192.168.1.99:11434"          # RFC1918・admin allowlist 未登録
_UNLISTED_LINK_LOCAL = "http://169.254.169.254:11434"  # クラウドメタデータ相当・admin allowlist 未登録
_UNLISTED_EXTERNAL = "http://ollama.example.com:11434"  # 外部ドメイン・admin allowlist 未登録


# 各テストは `_no_network_by_default`（本ファイル autouse・urlopen を計測付きで拒否する guard）を
# 明示的に受け取り、呼び出し後に `guard.calls == []` を確認する。シンク側の broad except が
# 「呼ばれたが拒否された」例外を吸収してしまうため、戻り値（degrade した結果）の確認だけでは
# 「本当に一度もネットワークへ出なかったか」までは確定できない（例外の型ではなく副作用そのものを見る）。

def test_embeddings_degrades_without_network_for_unlisted_url(monkeypatch, _no_network_by_default):
    # `SHERPA_DISABLE_EMBED`（他テストの kill-switch・複数ファイルが os.environ に直接 setdefault する
    # ため import された時点でプロセス全体に残る）をこのテストだけ外す。実 I/O は本テストが検証したい
    # SSRF ガード自身が防ぐ（urlopen は guard で fail-closed）。
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    # env の OPENAI_API_KEY/GEMINI_API_KEY（開発環境の .env に placeholder が入っていることがある）に
    # auto 選択が引っ張られないよう明示的に外す＝ollama_url だけが候補になることを固定する。
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    c = embeddings.cfg({"ollama_url": _UNLISTED_LAN})
    assert c is not None and c["provider"] == "ollama"
    assert embeddings.embed(["hello"], c) is None            # ベクトル無効へ degrade（BM25 のみ）
    assert _no_network_by_default.calls == []


def test_graph_extract_probe_degrades_without_network_for_unlisted_url(_no_network_by_default):
    cfg = {"provider": "ollama", "url": _UNLISTED_EXTERNAL, "model": "qwen2.5"}
    ok, detail = graph_extract._probe(cfg)
    assert ok is False and detail                             # llm_error 相当（旧 l_extract を壊さない）
    assert _no_network_by_default.calls == []


def test_intent_llm_classify_degrades_without_network_for_unlisted_url(monkeypatch, _no_network_by_default):
    # embeddings のテストと同じ理由（env の OPENAI_API_KEY/GEMINI_API_KEY の placeholder に auto 選択が
    # 引っ張られないよう外す＝ollama_url だけが候補になることを固定し、確実に ollama 経路を検証する）。
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = {"ollama_url": _UNLISTED_LAN}
    assert intent_llm.classify("消費税率は?", settings) is None  # clarify へフォールバック（無害）
    assert _no_network_by_default.calls == []


def test_graph_admin_ask_graph_degrades_without_network_for_unlisted_url(_no_network_by_default):
    settings = {"agent": "ollama", "ollama_url": _UNLISTED_LINK_LOCAL, "ollama_model": "qwen2.5"}
    result = graph_admin.ask_graph("グラフの状況は?", "v1", settings=settings)
    assert result["status"] == "failed"                        # 例外は外に漏れない
    assert _no_network_by_default.calls == []


def test_ollama_provider_agentic_run_degrades_without_network_for_unlisted_url(_no_network_by_default):
    """agents（agentic ループ）: `_agentic_run` 経由で失敗し、run() 全体は単発 grep へフォールバックして
    クラッシュしない（`_facade._gather` は route/dispatch を差し替えるので Neo4j 不要）。"""
    from sherpa.agents import Ctx

    p = ollama_provider.OllamaProvider(_UNLISTED_LAN, "qwen2.5")
    ctx = Ctx(message="消費税率は?", world="v1", knowledge=True,
             route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
             dispatch=lambda lens, inp: {"lens": "qa", "headline": "", "summary": {"total": 0},
                                         "data": {}, "sources": [], "scope": {}},
             scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
             make_sources=lambda docs: [])
    result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
    assert result["env"]["lens"] == "qa"                       # 単発 grep フォールバックの env がそのまま返る
    assert _no_network_by_default.calls == []


@pytest.mark.parametrize("lens", ["impact", "troubleshoot"])
def test_ollama_provider_agentic_run_degrades_without_network_for_unlisted_url_graph_lenses(
        lens, _no_network_by_default):
    """SC-6e: impact/troubleshoot（グラフ必須レンズ）でも、agentic ループ開始前の接続先検証
    （`_agentic_target_check`）が可用性解決（ES/Neo4j への実接続）より先に走り、不許可の
    Ollama URL では可用性解決へ進む前に fail-closed で止まる（qa 固定の既存テストでは
    レンズ別の回帰を検出できない・全レンズで同じ順序を保証する）。"""
    from sherpa.agents import Ctx

    p = ollama_provider.OllamaProvider(_UNLISTED_LAN, "qwen2.5")
    ctx = Ctx(message="消費税率を変えたら夜間バッチに影響ある?", world="v1", knowledge=True,
             route=lambda m: {"lens": lens, "input": m, "reason": "t"},
             dispatch=lambda l, inp: {"lens": lens, "headline": "", "summary": {"total": 0},
                                      "data": {}, "sources": [], "scope": {}},
             scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
             make_sources=lambda docs: [])
    result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
    assert result["env"]["lens"] == lens                       # 単発 grep フォールバックの env がそのまま返る
    assert _no_network_by_default.calls == []


def test_ollama_provider_plain_stream_degrades_without_network_for_unlisted_url(_no_network_by_default):
    """agents（単発ストリーミング・ナレッジ参照オフ）: `_stream` が失敗しても `_plain_run` が
    固定フォールバック文言に degrade する（クラッシュしない）。"""
    from sherpa.agents import Ctx

    p = ollama_provider.OllamaProvider(_UNLISTED_EXTERNAL, "qwen2.5")
    ctx = Ctx(message="こんにちは", world="v1", knowledge=False,
             route=lambda m: {}, dispatch=lambda lens, inp: {})
    result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
    assert result["env"]["headline"]                           # _plain_text() の固定文言（空にならない）
    assert _no_network_by_default.calls == []


def test_ollama_provider_send_paths_are_io_free_with_injected_system_settings(monkeypatch):
    """SC-6e: fresh system_settings を注入すれば、`_agentic_target_check`（agentic ループ
    開始前の「純粋な文字列検証」契約）は `store.get_system_settings()` を一切呼ばない（DB getter を
    記録型スタブに差し替え、呼び出しが無い＝`calls == []` を明示 assert する）。あわせて
    `_agentic_loop`/`_stream`/`_attribute` の全 `llm.ollama_url()` 呼び出しが同一の注入済み
    snapshot（同一オブジェクト）を使うことも確認する（URL 解決と allowlist 判定が別世代の設定を
    見る穴を防ぐ）。

    host は非 loopback（192.168.1.70）を使う——loopback は `_assert_host_port_allowed` が
    allowlist 判定より先に常に許可するため、`_allowlisted_hosts()`（延いては DB read）を経由
    しなくても偶然テストが通ってしまい、DB を読んでいる regression を検出できない。
    """
    db_calls: list = []
    monkeypatch.setattr(store, "get_system_settings", lambda: db_calls.append(1) or {})

    fresh = {"ollama_allowlist": ["192.168.1.70:11434"]}
    p = ollama_provider.OllamaProvider("http://192.168.1.70:11434", "qwen2.5", system_settings=fresh)
    p._agentic_target_check()   # allowlist 登録済みホスト＝許可（DB を読まず fresh を直接見る）
    assert db_calls == [], "system_settings 注入済みなのに DB getter が呼ばれた"

    seen: list = []

    def _record_and_stop(base, path, *, extra_allowed=None, system_settings=None):
        seen.append(system_settings)
        raise RuntimeError("stop-after-record")   # ネットワークへ進む前に打ち切る

    monkeypatch.setattr(ollama_provider.llm, "ollama_url", _record_and_stop)

    from sherpa.agents import Ctx
    ctx = Ctx(message="消費税率は?", world="v1",
             route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
             dispatch=lambda lens, inp: {}, scope_meta={"world": "v1", "scope_paths": [], "source": "all"})
    for call in (lambda: p._agentic_loop(ctx),
                lambda: next(p._stream("hi")),
                lambda: p._attribute("text", "digest", {})):
        try:
            call()
        except RuntimeError:
            pass
    assert len(seen) == 3
    assert all(s is fresh for s in seen)   # 同一オブジェクト＝3経路とも同じ snapshot を使い回している


def test_select_provider_wires_same_system_settings_object_into_ollama_provider(monkeypatch):
    """本番配線の固定: `providers/__init__.py::_select_provider` の `agent == "ollama"` 分岐が、
    入口で読んだ fresh sys_s を `OllamaProvider(url, model, system_settings=sys_s)` の第3引数へ
    渡し忘れる regression（上のテストが塞ぐ「省略時は DB read」契約の再発）を検出する。
    `agents.OllamaProvider`（facade 実行時解決の対象・`_select_provider` が `_facade.OllamaProvider`
    経由で呼ぶ）を recorder に差し替え、渡された system_settings が呼び出し元で渡した sentinel と
    同一オブジェクトであることを identity で確認する（コピーや等価な別 dict では検出できない）。"""
    from sherpa import agents, providers as providers_pkg

    sentinel = {"ollama_allowlist": ["192.168.1.80:11434"]}
    captured: list = []

    class _RecorderOllamaProvider:
        def __init__(self, url, model, system_settings=None):
            captured.append(system_settings)

    monkeypatch.setattr(agents, "OllamaProvider", _RecorderOllamaProvider)
    providers_pkg.get_provider({"agent": "ollama"}, system_settings=sentinel)
    assert len(captured) == 1, "OllamaProvider が構築されなかった"
    assert captured[0] is sentinel, "入口で読んだ system_settings と別オブジェクトが渡された"


def test_health_ping_ollama_degrades_without_network_for_malformed_central_url(monkeypatch, _no_network_by_default):
    """`_ping_ollama`（中央設定 `system_settings.ollama_url` 由来・env はもう読まない）は
    値が解釈不能なら `_canonical_host_port` が None → SsrfBlocked → 既存の broad except で ok=False に
    degrade する。中央設定は本来 `admin_settings_put` の `_validate_central_ollama_url` で書込時に
    弾かれる値だが、ここでは「万一 DB に不正値が残っていても実行時に安全側へ倒れる」多層防御を確認する
    （直接 `sherpa.store.get_system_settings` を差し替えて再現する）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"ollama_url": "not a url"})
    out = health._check_one("ollama", "ローカルLLM（Ollama）", "none", health._ping_ollama, "hint")
    assert out["ok"] is False
    assert _no_network_by_default.calls == []


def test_health_ai_check_ollama_degrades_without_network_for_unlisted_url(monkeypatch, _no_network_by_default):
    """`_ai_check_ollama`（per-user 設定）は env の暗黙 allowlist を持たない＝
    未登録の接続先はそのまま SsrfBlocked→ok=False に degrade する（`ai_snapshot` と同じ呼び方）。"""
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    settings = {"ollama_url": _UNLISTED_LAN}
    out = health._check_one("ollama", "ローカルLLM（Ollama）", "none",
                            lambda: health._ai_check_ollama(settings), "hint")
    assert out["ok"] is False
    assert _no_network_by_default.calls == []


# ===== 3. 正規化 unit（_canonical_host_port / is_loopback_host / assert_ollama_url_allowed） =====

def test_canonical_host_port_ipv6_loopback():
    assert llm._canonical_host_port("http://[::1]:11434") == ("::1", 11434)
    assert llm.is_loopback_host("::1") is True


def test_canonical_host_port_strips_trailing_dot():
    """`example.com.`（末尾ドット・DNS 上は同一ホスト）は `example.com` に正規化される。"""
    assert llm._canonical_host_port("http://example.com.:11434") == ("example.com", 11434)
    assert llm._canonical_host_port("http://example.com:11434") == ("example.com", 11434)


def test_canonical_host_port_rejects_userinfo():
    """重大バグ是正: `urlparse().hostname` は userinfo（`user:pass@`）を黙って除去してしまうため、
    以前は `http://user:pass@allowed-host:11434` の資格情報部分が黙って捨てられ、host:port だけで
    admin allowlist に一致して許可されてしまっていた（保存値・監査ログに資格情報が平文で残る実害）。
    userinfo を含む URL は解釈不能として拒否する（None）。"""
    assert llm._canonical_host_port("http://192.168.1.50:11434") == ("192.168.1.50", 11434)
    assert llm._canonical_host_port("http://admin:secret@192.168.1.50:11434") is None
    assert llm._canonical_host_port("http://admin@192.168.1.50:11434") is None


def test_canonical_host_port_default_port_vs_explicit():
    """RV HIGH #1（2026-07-14）: ポート省略は **scheme の既定ポート**（http=80）を補う（旧実装は
    無条件で Ollama の正規ポート 11434 を補っており、下の
    `test_canonical_host_port_omitted_port_no_longer_aliases_ollama_default_port` が pin する
    バイパスの原因だった）。明示ポートが違えばタプルも別扱い。"""
    assert llm._canonical_host_port("http://192.168.1.50") == ("192.168.1.50", 80)
    assert llm._canonical_host_port("http://192.168.1.50:11434") == ("192.168.1.50", 11434)
    assert llm._canonical_host_port("http://192.168.1.50:8080") == ("192.168.1.50", 8080)


def test_canonical_host_port_omitted_port_defaults_to_scheme_port():
    """RV HIGH #1（2026-07-14）: http は 80・https は 443 を補う（Ollama の正規ポートではない）。"""
    assert llm._canonical_host_port("http://example.com") == ("example.com", 80)
    assert llm._canonical_host_port("https://example.com") == ("example.com", 443)


def test_canonical_host_port_omitted_port_no_longer_aliases_ollama_default_port():
    """RV HIGH #1（2026-07-14）: 旧実装のバイパス再現＝admin が `host:11434` を allowlist に
    登録しても、ポート省略の `http://host`（wire port は実際には 80）はもう一致しない
    （break-and-confirm: `_canonical_host_port` の既定ポートを 11434 に戻すと本テストは落ちる）。"""
    assert llm._canonical_host_port("http://192.168.1.50") != llm._canonical_host_port(
        "http://192.168.1.50:11434")


def test_canonical_host_port_scheme_handling():
    """http/https は同じ host:port に正規化される（scheme はタプルに含まれない、port 明示時）。
    非 http(s) は不正。"""
    assert llm._canonical_host_port("https://192.168.1.50:11434") == llm._canonical_host_port(
        "http://192.168.1.50:11434")
    assert llm._canonical_host_port("ftp://192.168.1.50:11434") is None
    assert llm._canonical_host_port("not a url") is None
    assert llm._canonical_host_port("") is None


def test_canonical_host_port_rejects_path_query_fragment():
    """RV HIGH #2（2026-07-14）: `base` は接続先の起点（host:port）のみを表すべき契約。path
    （"/" 以外）・query・fragment を含む URL は解釈不能として None を返す（`ollama_url(base, path)`
    の base+path 連結で呼び出し側が意図した path が上書き/追加され任意パスへ到達できてしまうのを防ぐ）。
    """
    assert llm._canonical_host_port("http://127.0.0.1:11434/api/tags") is None
    assert llm._canonical_host_port("http://127.0.0.1:11434?x=1") is None
    assert llm._canonical_host_port("http://127.0.0.1:11434#frag") is None
    assert llm._canonical_host_port("http://127.0.0.1:11434/") == ("127.0.0.1", 11434)  # 末尾スラッシュのみは許可
    assert llm._canonical_host_port("http://127.0.0.1:11434") == ("127.0.0.1", 11434)   # path 無しは許可


def test_assert_ollama_url_allowed_loopback_always_passes_regardless_of_allowlist():
    llm.assert_ollama_url_allowed("http://127.0.0.1:11434")   # allowlist 空でも常に許可
    llm.assert_ollama_url_allowed("http://localhost:11434")


def test_assert_ollama_url_allowed_blocks_unlisted_non_loopback():
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed(_UNLISTED_LAN)
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed(_UNLISTED_LINK_LOCAL)
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed(_UNLISTED_EXTERNAL)


def test_assert_ollama_url_allowed_passes_exact_admin_allowlist_entry(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda: {"ollama_allowlist": ["192.168.1.50:11434"]})
    llm.assert_ollama_url_allowed("http://192.168.1.50:11434")   # host:port 完全一致
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://192.168.1.50:8080")  # ポート違いは別扱い＝拒否


def test_assert_ollama_url_allowed_env_ollama_url_is_no_longer_implicit_member(monkeypatch):
    """重大バグ是正: `OLLAMA_URL` env はもう実行時の一般許可リストへ暗黙加算されない。
    「UI(DB) が唯一の真実源・env は初回起動シードのみ」という所有原則（`system_settings.ollama_url`
    は `sherpa.api._seed_ollama_url_from_env` で env から一度だけシードされる）と矛盾しており、
    以前は admin が管理画面の allowlist から接続先を削除しても、env 経由でそのまま許可され続ける
    （UI での削除が効かない）穴があった。"""
    monkeypatch.setattr(store, "get_system_settings", lambda: {})   # allowlist 未設定
    monkeypatch.setenv("OLLAMA_URL", _UNLISTED_LAN)
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed(_UNLISTED_LAN)


def test_assert_ollama_url_allowed_vlm_env_does_not_grant_general_ollama_url_access(monkeypatch):
    """`SHERPA_VLM_OLLAMA_URL`（VLM 専用の接続先 env）も一般の Ollama 許可リストへは加算されない
    （VLM 専用の権限を個人 `ollama_url` の保存・実行へ流用させない）。VLM 自身の送信は
    `vision_arm._read_ollama` が `extra_allowed` でこの呼び出しだけに閉じて許可する
    （`tests/unit/test_vision_arm_ssrf.py` 参照）。"""
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    monkeypatch.setenv("SHERPA_VLM_OLLAMA_URL", _UNLISTED_LAN)
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed(_UNLISTED_LAN)


def test_assert_ollama_url_allowed_extra_allowed_scopes_to_this_call_only(monkeypatch):
    """`extra_allowed`（VLM 専用の局所許可・`vision_arm._read_ollama` が使う）は呼び出しにだけ効き、
    一般の allowlist（`_allowlisted_hosts()`）を汚染しない。"""
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    hp = llm._canonical_host_port(_UNLISTED_LAN)
    llm.assert_ollama_url_allowed(_UNLISTED_LAN, extra_allowed={hp})   # このセットのおかげで許可される
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed(_UNLISTED_LAN)   # extra_allowed 無しでは相変わらず拒否
    assert hp not in llm._allowlisted_hosts()   # 一般 allowlist は汚染されていない


def test_assert_ollama_url_allowed_rejects_userinfo_even_for_loopback(monkeypatch):
    """`test_canonical_host_port_rejects_userinfo`（上）の正規化拒否が、実際の宛先検証
    （`assert_ollama_url_allowed`）にも伝播することを固定する（loopback でもすり抜けない）。"""
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://user:pass@localhost:11434")


def test_assert_ollama_url_allowed_malformed_url_does_not_leak_raw_value(monkeypatch):
    """解釈不能な URL（`_canonical_host_port` が None を返すケース・ここでは userinfo 付き）の
    エラー文言に、生の `base` をそのまま埋め込まない（呼び出し元＝`usage_chat._resolve_cfg` 等が
    この文言をそのまま 503 の detail に含めるため、パスワード等が外部応答へ反射されるのを防ぐ）。"""
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    with pytest.raises(llm.SsrfBlocked) as exc:
        llm.assert_ollama_url_allowed("http://user:secret-password@localhost:11434")
    assert "secret-password" not in str(exc.value)
    assert "user:secret-password" not in str(exc.value)


def test_assert_ollama_url_allowed_in_malformed_url_does_not_leak_raw_value():
    """`assert_ollama_url_allowed_in`（`_validate_central_ollama_url` が使う置換後正本チェック）も
    同様に生の `base` を反映しない。"""
    with pytest.raises(llm.SsrfBlocked) as exc:
        llm.assert_ollama_url_allowed_in("http://user:secret-password@localhost:11434", set())
    assert "secret-password" not in str(exc.value)


def test_assert_ollama_url_allowed_omitted_port_does_not_alias_explicit_allowlist_entry(monkeypatch):
    """RV HIGH #1（2026-07-14）: admin が `host:11434` を allowlist に登録しても、ポート省略の
    `http://host`（wire port は実際には 80）は許可されない（旧実装は無条件で 11434 を補っていたため
    誤って一致・許可してしまっていた＝allowlist の意図と実到達先のズレによるバイパス）。
    break-and-confirm: `_canonical_host_port` の既定ポート補完を旧仕様（常に 11434）に戻すと本テストは落ちる。
    """
    monkeypatch.setattr(store, "get_system_settings", lambda: {"ollama_allowlist": ["192.168.1.60:11434"]})
    llm.assert_ollama_url_allowed("http://192.168.1.60:11434")           # 明示ポート一致は許可
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://192.168.1.60")             # ポート省略（=:80 相当）は拒否


def test_assert_ollama_url_allowed_rejects_path_query_fragment_even_for_loopback(monkeypatch):
    """RV HIGH #2（2026-07-14）: loopback／allowlist 済みホストであっても、base に
    path（"/" 以外）・query・fragment が混入していれば拒否する（`ollama_url(base, path)` の
    base+path 連結で意図した path を上書き/追加できてしまう「任意パス到達」の防止）。
    break-and-confirm: `_canonical_host_port` の path/query/fragment チェックを外すと本テストは落ちる。
    """
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://127.0.0.1:11434/api/tags")
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://127.0.0.1:11434?x=1")
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://127.0.0.1:11434#/api/chat")
    monkeypatch.setattr(store, "get_system_settings", lambda: {"ollama_allowlist": ["192.168.1.61:11434"]})
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed("http://192.168.1.61:11434/secret")
    llm.assert_ollama_url_allowed("http://127.0.0.1:11434/")             # 末尾スラッシュのみは許可（不変）
    llm.assert_ollama_url_allowed("http://127.0.0.1:11434")              # path 無しは許可（不変）


# ===== 5. `llm.no_proxy_requests()`（embeddings.py の undefined 参照を実装で解消） =====
# 以前は `embeddings.py` の ollama 分岐が存在しない `llm.no_proxy_requests()` を呼んでおり、
# 呼び出すたびに `AttributeError` になっていた。broad except で毎回無音 degrade（`None`）していた
# ため、Ollama embeddings は実際には一度も POST に到達したことが無かった（開示済み）。

def test_no_proxy_requests_selects_no_proxy_opener_for_urlopen_no_redirect(monkeypatch):
    """`no_proxy_requests()` の `with` ブロック内でだけ `urlopen_no_redirect` が
    `_NO_REDIRECT_NO_PROXY_OPENER`（env の HTTP(S)_PROXY を無視する opener）を使う。ブロックの
    外側は従来どおり `_NO_REDIRECT_OPENER`（env を尊重）のまま＝OpenAI/Gemini は影響を受けない。"""
    calls: list = []

    class _FakeOpener:
        def __init__(self, tag):
            self.tag = tag

        def open(self, req, timeout=None):
            calls.append(self.tag)

            class _Resp:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

            return _Resp()

    monkeypatch.setattr(llm, "_NO_REDIRECT_OPENER", _FakeOpener("proxy"))
    monkeypatch.setattr(llm, "_NO_REDIRECT_NO_PROXY_OPENER", _FakeOpener("no-proxy"))

    llm.urlopen_no_redirect("http://example.invalid")
    assert calls == ["proxy"]

    with llm.no_proxy_requests():
        llm.urlopen_no_redirect("http://example.invalid")
    assert calls == ["proxy", "no-proxy"]

    llm.urlopen_no_redirect("http://example.invalid")   # ブロックを抜けたら既定の opener へ戻る
    assert calls == ["proxy", "no-proxy", "proxy"]


def test_embeddings_ollama_reaches_post_for_loopback_url(monkeypatch):
    """対照テスト: loopback URL（既定で許可される）に対して `embeddings._embed_batch`
    の ollama 分岐が実際に `llm.urlopen_no_redirect` まで到達し、応答が正しく反映されることを固定
    する（以前は `no_proxy_requests()` が未定義のため必ず `AttributeError`→broad except で `None`
    になっており、この到達自体が一度も検証されていなかった）。"""
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    calls: list = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"embeddings": [[0.25, 0.5]]}'

    def _fake_open(req, timeout=None):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(llm, "urlopen_no_redirect", _fake_open)

    c = embeddings.cfg({"ollama_url": "http://127.0.0.1:11434"})
    assert c is not None and c["provider"] == "ollama"
    c = {**c, "dim": 2}   # フェイク応答のベクトル長（2）に合わせる（実際の nomic-embed-text は768次元・
    # ここでは POST 到達と応答反映だけを見るため次元検証はテスト対象外）。
    vecs = embeddings.embed(["hello"], c)
    assert vecs == [[0.25, 0.5]]
    assert calls, "POST へ到達しなかった（no_proxy_requests 未定義による無音 degrade が再発している）"


def test_no_redirect_openers_have_expected_proxy_and_redirect_handlers(monkeypatch):
    """上の2テストはどちらも opener/urlopen_no_redirect 自体をフェイク化するため、実際の
    `_NO_REDIRECT_OPENER`／`_NO_REDIRECT_NO_PROXY_OPENER`（モジュール読み込み時に1回だけ構築される
    本物のオブジェクト）が正しいハンドラ構成を持つことは固定していなかった。ここでは本物の
    OpenerDirector を直接検査する（フェイク化しない）。

    `urllib.request.OpenerDirector` は、渡された `ProxyHandler` が `<scheme>_open` 等の実メソッドを
    1つも持たない場合（`proxies={}` で構築＝どの scheme のエントリも無い）、そのハンドラ自体を
    `.handlers` へ一切追加しない（`ProxyHandler({}).http_open` は存在しない・空 dict では
    バインドされない）。これは「プロキシしない」ことの構造的な証拠になる＝`.proxies == {}` という
    属性値だけを見るより厳密（env が空のテスト環境ではどちらの構築でも `.proxies == {}` に見えて
    しまい区別できないため）。

    モジュール読み込み時の singleton（`llm._NO_REDIRECT_NO_PROXY_OPENER`）は import 時点の env
    スナップショットで構築済みのため、この場で `monkeypatch.setenv` しても遡って反映されない
    （`ProxyHandler` は構築時にしか env を読まない）。import 時点でたまたま HTTP_PROXY が未設定
    だと、singleton だけを見る検証は「実装から `ProxyHandler({})` を削除しても env 次第で通って
    しまう」false green になり得るため、`llm._build_no_proxy_opener()`（同じ実装を呼べるファクトリ）
    を **HTTP_PROXY 設定後に呼んで**新規構築した実 opener でも同じ不変条件を確認する。"""
    def _handlers_of(opener, cls):
        return [h for h in opener.handlers if isinstance(h, cls)]

    assert llm._NO_REDIRECT_OPENER is not llm._NO_REDIRECT_NO_PROXY_OPENER

    # 両方とも redirect 非追跡ハンドラ（_NoRedirect）を持つ。
    for opener in (llm._NO_REDIRECT_OPENER, llm._NO_REDIRECT_NO_PROXY_OPENER):
        no_redirect_handlers = _handlers_of(opener, llm._NoRedirect)
        assert len(no_redirect_handlers) == 1
        # 3xx は追跡しない（redirect_request が None を返す）ことを実際に呼んで確認する。
        assert no_redirect_handlers[0].redirect_request(None, None, 302, "Found", {}, "http://x") is None

    # `_NO_REDIRECT_NO_PROXY_OPENER` は `ProxyHandler({})`（明示的な空 dict）で構築されているため、
    # `.handlers` に ProxyHandler が一切現れない（=常に直結・env を読まない）。
    assert _handlers_of(llm._NO_REDIRECT_NO_PROXY_OPENER, urllib.request.ProxyHandler) == []

    # HTTP_PROXY を先に設定してから、同じファクトリで実 opener を新規構築する（proxy env が
    # 「ある」状態でも `_build_no_proxy_opener()` が本当に env を無視することの直接証拠）。
    monkeypatch.setenv("HTTP_PROXY", "http://myproxy.internal:8080")
    monkeypatch.delenv("http_proxy", raising=False)
    fresh_no_proxy_opener = llm._build_no_proxy_opener()
    assert _handlers_of(fresh_no_proxy_opener, urllib.request.ProxyHandler) == []

    # 対照実験: llm.py が `_NO_REDIRECT_OPENER` を構築するのと**同じ引数**
    # （`build_opener(_NoRedirect)`・明示的な ProxyHandler を渡さない）で、同じ HTTP_PROXY 設定済みの
    # env から新規に構築すると、既定の ProxyHandler（env 依存）が実際に含まれることを確認する＝
    # 「明示的な空 dict を渡す」ことが本当に意味のある差分であることの証拠。
    default_pattern_opener = urllib.request.build_opener(llm._NoRedirect)
    default_proxy_handlers = _handlers_of(default_pattern_opener, urllib.request.ProxyHandler)
    assert len(default_proxy_handlers) == 1
    assert default_proxy_handlers[0].proxies.get("http") == "http://myproxy.internal:8080"


# `sherpa.llm` を新規プロセスで import し、モジュール読み込み時に構築される singleton
# （`_NO_REDIRECT_OPENER`／`_NO_REDIRECT_NO_PROXY_OPENER`）自体のハンドラ構成を検査する。
# `_build_no_proxy_opener()` ファクトリ単体の検証（上のテスト）だけでは、singleton の構築行が
# ファクトリを迂回する変更（例: `_NO_REDIRECT_NO_PROXY_OPENER = urllib.request.build_opener(
# _NoRedirect, urllib.request.ProxyHandler())` のように直接書き換えて `ProxyHandler({})` を
# 使わなくする退行）を検出できない——同一プロセス内で `importlib.reload` する手もあるが、
# 他テストが既に import 済みの `llm` モジュールや依存モジュールの状態（キャッシュされた
# `os.environ` の読み取り結果等）を巻き込みかねないため、独立プロセスでの新規 import が最も
# 確実。DB/Neo4j/ES など外部サービスには一切触れない自己完結スクリプト。
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
    """本番の singleton（`llm._NO_REDIRECT_NO_PROXY_OPENER`）自体が、HTTP_PROXY が設定された
    環境で新規 import されても proxy を一切拾わないことを、独立プロセスでの実 import で固定する
    （`_build_no_proxy_opener()` ファクトリ単体の検証だけでは検出できない singleton 側の迂回を
    塞ぐ・このファイル冒頭の `_SINGLETON_OPENER_CHECK_SCRIPT` docstring 参照）。

    subprocess の env は `os.environ` を継承せず、必要最小限（`PATH`/`HOME` と、この検証用の
    `HTTP_PROXY`）だけを明示的に組み立てる。継承環境に `REQUEST_METHOD`（CGI 実行の痕跡）が
    残っていると、urllib の httpoxy 対策（`getproxies_environment` は CGI 文脈と判定した場合に
    `HTTP_PROXY` を無視する）が働き、対照実験（既定パターンの opener が `HTTP_PROXY` を拾う
    ことの確認）側が意図せず失敗する環境依存を避けるため。"""
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

    assert result["no_proxy_opener_has_proxy_handler"] is False, (
        "_NO_REDIRECT_NO_PROXY_OPENER singleton が HTTP_PROXY 環境下で ProxyHandler を持ってしまった"
        "（factory を迂回する変更が singleton 構築側に入った可能性がある）")
    assert result["default_opener_has_proxy_handler"] is True   # 対照実験: 既定は env を尊重する
    assert result["default_opener_proxy_value"] == "http://myproxy.internal:8080"
    assert result["no_proxy_opener_no_redirect_count"] == 1
    assert result["default_opener_no_redirect_count"] == 1
    assert result["no_proxy_opener_redirect_request_returns_none"] is True


def test_embeddings_ollama_sends_post_json_body_within_no_proxy_requests_context(monkeypatch):
    """`llm.urlopen_no_redirect` そのものを丸ごと差し替えず、`_NO_REDIRECT_NO_PROXY_OPENER`
    （`no_proxy_requests()` のコンテキスト内でだけ選ばれる本物の opener）の `.open()` だけを差し替え、
    実際に `urlopen_no_redirect()` 内のオープナー選択ロジックを経由させる。`embeddings.py` の
    ollama 分岐から `with llm.no_proxy_requests():` を外すと、`_no_proxy_ctx` が False のままになり
    `_NO_REDIRECT_OPENER`（差し替えていない本物）が選ばれて本テストの opener が一度も呼ばれず、
    期待どおり失敗する（前の対照テストの `urlopen_no_redirect` 丸ごと差し替えだと素通りしていた
    false green を閉じる）。HTTP メソッド・JSON ボディ・呼び出し時点の ContextVar の値も合わせて
    直接検証する。"""
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

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
    c = {**c, "dim": 2}
    vecs = embeddings.embed(["hello"], c)
    assert vecs == [[0.25, 0.5]]
    assert captured.get("method") == "POST"
    assert captured.get("body") == {"model": c["model"], "input": ["hello"]}
    assert captured.get("ctx") is True, "no_proxy_requests() のコンテキスト外で送信された"
