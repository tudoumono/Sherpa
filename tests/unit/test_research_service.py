"""PART-4 `sherpa/research_service.py` の単体テスト（DB 不要・LLM は `agentic_search._post` を差し替え）。

`tests/unit/test_ext2_evidence.py` と同じ手法（`_post` を固定応答列に差し替え・実 fixtures corpus
`v1` に対して実際にツールを実行する）を使う。API 層の配線（認証・監査・HTTP ステータス）は
`tests/api/test_ext_api.py` の `research` セクションで検証済み——ここは `research_service` 単体の
契約（model 解決・truncate・cid 秘匿・request_id 伝播・providers/base.py private helper の契約）に絞る。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import pytest  # noqa: E402

import sherpa.agentic_search as A  # noqa: E402
import sherpa.research_service as RS  # noqa: E402
from sherpa import scope as scope_mod  # noqa: E402
from sherpa.providers import base as PB  # noqa: E402

_REAL_DOC = "4期/04_運用/障害記録.md"   # fixtures/corpus/v1 実在ファイル（test_ext2_evidence.py と同一）


@pytest.fixture(autouse=True)
def _hermetic_es_graph(monkeypatch):
    """ツール定義配列を決定的にする（実 ES/Neo4j 到達可否に依存しない・test_ext2_evidence.py と同じ）。"""
    monkeypatch.setattr(A, "es_index", A.es_index)
    monkeypatch.setattr(A.es_index, "available", lambda: False)
    monkeypatch.setattr(A, "_graph_available", lambda: False)


@pytest.fixture(autouse=True)
def _hermetic_world_lock_and_resolve(monkeypatch):
    """本ファイルは DB 不要の unit テスト（CI の unit ジョブに Postgres は無い）。`run_research` が
    実際に触る DB 依存点を差し替える:

    - `world_lock_shared`（実 Postgres advisory lock）→ no-op。実際の相互排他契約
      （共有ロック同士は並行・排他ロックとは直列化・lock_timeout）は
      `tests/integration/test_world_lock_shared_semantics.py`（要 Postgres）で別途検証済み。
    - `worlds.resolve_external_world`（registry 到達不可を `ExternalResolverError` にする
      strict 版・DB 必須）→ `worlds.world_dir()`（DB 不達なら fixtures/corpus へ自己修復する
      既存の多段フォールバック・`SHERPA_USE_FIXTURES=1` 環境で完結）を使う fake に差し替える。
    - `store.get_world`（`world_dir()` が内部で呼ぶ registry 照会）→ 常に None。DB へ到達できて
      しまう環境では、実際に登録済みの world（同名の row）を拾ってしまい、このテストが
      共有 dev DB の状態に依存してしまう（hermetic の趣旨に反する）ため、常に「未登録」扱いに
      固定し `world_dir()` を確実に fixtures/corpus 経路へ倒す。
    """
    import contextlib

    from sherpa import research_service as _rs
    from sherpa import store as _store
    from sherpa import worlds as _worlds

    @contextlib.contextmanager
    def _noop_lock(world_id, *, timeout_ms=None, connect_timeout=None):
        yield

    def _fake_resolve(world_id, **kw):
        d = _worlds.world_dir(world_id)
        return _worlds.ExternalWorldResolution("ok" if d else "not_found", d)

    monkeypatch.setattr(_rs, "world_lock_shared", _noop_lock)
    monkeypatch.setattr(_worlds, "resolve_external_world", _fake_resolve)
    monkeypatch.setattr(_store, "get_world", lambda world_id: None)


def _install_post(monkeypatch, seq):
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))


def _success_seq():
    """呼ぶたびに新しい list を返す（`_post` は `list.pop` で消費するため使い回せない）。

    帰属呼び出しは2回分用意する: 1回目は `agentic_search.openai_style` 自身が内部で行う帰属
    （重複排除**前**の添字に対する判定・`run_research` はこの結果を使わない）、2回目は
    `run_research` が最終重複排除の**後**にやり直す帰属（実際に Evidence Packet の `used` へ
    反映されるのはこちら）。"""
    return [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search",
             "arguments": '{"query":"税率改定に伴う障害の記録"}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "read_around",
             "arguments": f'{{"doc_id":"{_REAL_DOC}","line":1}}'}}]}}]},
        {"choices": [{"message": {"content": "税率改定に伴う障害です。"}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c3", "function": {"name": "submit_attribution", "arguments": '{"used":["ev-1"]}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c4", "function": {"name": "submit_attribution", "arguments": '{"used":["ev-1"]}'}}]}}]},
    ]


# ==== resolve_model_and_provider（model 許容値検証・省略時既定・設定不備）====

def test_resolve_model_and_provider_default_is_ollama():
    provider, model = RS.resolve_model_and_provider(None, {})
    assert provider == "ollama"
    assert model == "qwen2.5"   # 組み込み既定（カタログ未設定時）


def test_resolve_model_and_provider_explicit_model_selects_openai():
    provider, model = RS.resolve_model_and_provider("gpt-5.4-mini", {})
    assert provider == "openai" and model == "gpt-5.4-mini"


def test_resolve_model_and_provider_rejects_unknown_model():
    with pytest.raises(RS.ModelNotAllowed):
        RS.resolve_model_and_provider("not-a-real-model", {})


def test_model_not_allowed_reclassified_to_timeout_when_deadline_exceeded(monkeypatch):
    """RV7 是正の固定: `ModelNotAllowed` はロック取得より前（`resolve_model_and_provider`）に
    しか起きないが、それより前段の設定読取（`store.get_system_settings()`）が長引いて既に
    デッドラインを超えていた場合、400（`ModelNotAllowed`）ではなく504（`ResearchTimeout`）を
    優先する——`_ResearchError` を継承させ、`run_research` 末尾の共通デッドライン判定へ
    合流させる契約を偽時計で決定的に固定する。"""
    from sherpa import research_service as _rs

    clock = {"t": 0.0}
    monkeypatch.setattr(_rs.time, "monotonic", lambda: clock["t"])

    def _slow_settings(**kw):
        clock["t"] = 1000.0   # 設定読取がデッドラインを丸ごと使い切ったことにする
        return {}

    monkeypatch.setattr(_rs.store, "get_system_settings", _slow_settings)
    with pytest.raises(RS.ResearchTimeout):
        RS.run_research(world="v1", query="x", scope_paths=[], model="not-a-real-model",
                        max_iterations=1, max_results=20, timeout_s=30, key_id=1,
                        system_settings=None)


def test_model_not_allowed_stays_400_when_deadline_not_exceeded(monkeypatch):
    """対照実験: 設定読取が速く、期限内に不許可モデルと判明した場合は、これまでどおり
    `ModelNotAllowed`（呼び出し元は400にする）のまま——デッドライン優先の再分類は
    「期限を超えた場合だけ」に限定されることの固定。"""
    with pytest.raises(RS.ModelNotAllowed):
        RS.run_research(world="v1", query="x", scope_paths=[], model="not-a-real-model",
                        max_iterations=1, max_results=20, timeout_s=30, key_id=1,
                        system_settings={})


def test_resolve_model_and_provider_raises_when_catalog_default_empty_and_hardcoded_fallback_not_allowed():
    """管理者がカタログを明示設定（allowed はあるが default 空欄）した場合、
    `model_catalog.resolve_model` は組み込み既定（qwen2.5）へ縮退するが、その値は管理者の
    allowed リストに含まれない——黙ってそれを使わず、設定不備として `ProviderUnavailable` にする。
    """
    sys_s = {"model_catalog": {"ollama": {"subsearch": {"allowed": ["custom-local"], "default": ""}}}}
    with pytest.raises(RS.ProviderUnavailable):
        RS.resolve_model_and_provider(None, sys_s)


def test_resolve_model_and_provider_default_still_works_when_catalog_untouched():
    """カタログが管理者未設定（組み込み既定のまま）の従来環境では、省略時解決は壊れない
    （設定不備の検出が正常系を巻き込んでいないことの固定）。"""
    provider, model = RS.resolve_model_and_provider(None, {"model_catalog": {}})
    assert (provider, model) == ("ollama", "qwen2.5")


# ==== resolve_model_and_provider: provider 引数の語彙検証・エラー文言 ====

def test_resolve_model_and_provider_rejects_unknown_provider_before_other_checks():
    """`provider` が `RESEARCH_PROVIDERS`（"ollama"/"openai"）に無い任意文字列なら、
    model 解決を試みる前に `ModelNotAllowed` にする（`ExtResearchReq.provider` は pydantic の
    `Literal` で外部境界を422にするが、本関数を直接呼ぶ経路もここ1箇所で弾く）。"""
    with pytest.raises(RS.ModelNotAllowed) as exc_info:
        RS.resolve_model_and_provider(None, {}, provider="gemini")
    assert "gemini" in str(exc_info.value)


def test_resolve_model_and_provider_explicit_provider_model_not_allowed_names_provider():
    """`provider` 明示指定時、そのモデルが許可されていない場合のメッセージに provider 名を
    含める（どちらの用途で拒否されたかが分かるように）。"""
    with pytest.raises(RS.ModelNotAllowed) as exc_info:
        RS.resolve_model_and_provider("not-a-real-model", {}, provider="openai")
    assert "openai" in str(exc_info.value)


def test_resolve_model_and_provider_converts_corrupted_default_to_provider_unavailable():
    """`model`・`provider` 両方省略時、保存されている `research_default_provider` が破損値
    （`RESEARCH_PROVIDERS` に無い）なら黙って "ollama" にフォールバックせず
    `ProviderUnavailable`（呼び出し回数ゼロ）にする——PUT 側は既に不正値を 422 で拒否しているため、
    ここでの検知は「保存後に何らかの経路で壊れた値」への防波堤。"""
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS.resolve_model_and_provider(None, {"research_default_provider": "gemini"})
    assert exc_info.value.llm_calls == 0


def test_run_research_rejects_corrupted_default_provider_without_calling_post(monkeypatch):
    """`run_research` を通しても、破損した `research_default_provider` は `_post` へ一切到達
    しないまま `ProviderUnavailable` になる（未計測のまま 503 相当）。"""
    calls = []
    monkeypatch.setattr(A, "_post", lambda *a, **kw: calls.append(1))
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=1,
                        max_results=20, timeout_s=None, key_id=1,
                        system_settings={"research_default_provider": "gemini"})
    assert not calls
    assert exc_info.value.llm_calls == 0


def test_research_providers_is_public_constant():
    """他モジュール（system_extras.py 等）が private 名を参照しないで済むよう、
    provider 集合は公開定数 `RESEARCH_PROVIDERS`（`_` 始まりでない）として提供する。"""
    assert RS.RESEARCH_PROVIDERS == frozenset({"ollama", "openai"})


# ==== default_research_provider: 未設定は既定・破損値はフォールバックせずエラー ====

def test_default_research_provider_treats_missing_none_and_empty_as_unset():
    """未設定（キー無し／`None`／空文字）だけが組み込み既定 "ollama" になる。有効な値
    （"openai"）はそのまま返す。"""
    assert RS.default_research_provider({}) == "ollama"
    assert RS.default_research_provider({"research_default_provider": None}) == "ollama"
    assert RS.default_research_provider({"research_default_provider": ""}) == "ollama"
    assert RS.default_research_provider({"research_default_provider": "openai"}) == "openai"


def test_default_research_provider_rejects_corrupted_value_instead_of_silently_falling_back():
    """保存されているのに `RESEARCH_PROVIDERS` に無い値（未知文字列・非文字列＝破損 JSONB）は
    黙って "ollama" にフォールバックせず `ValueError` を送出する——`TypeError`（`in frozenset` は
    非 hashable で例外）も送出しない（型検査を先に行う）。"""
    for bad in (["openai"], {"x": 1}, 42, False, "gemini", "OLLAMA"):
        with pytest.raises(ValueError):
            RS.default_research_provider({"research_default_provider": bad})


# ==== _connect_openai: 送信前 fail-closed preflight（_post 呼び出しゼロで拒否） ====

def test_connect_openai_rejects_placeholder_key_without_calling_post(monkeypatch):
    """`.env.example` のプレースホルダ（`sk-REPLACE_ME`）は「キーあり」と誤認せず拒否する
    （真偽値だけの `if not key` 判定はこれをすり抜けていた）。"""
    calls = []
    monkeypatch.setattr(A, "_post", lambda *a, **kw: calls.append(1))
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS._connect_openai({"openai_api_key": "sk-REPLACE_ME"})
    assert not calls
    assert str(exc_info.value) == RS.keys.NO_CENTRAL_KEY_MESSAGE


def test_connect_openai_rejects_invalid_cloud_provider_without_calling_post(monkeypatch):
    """strict=True で鍵を解決する——`cloud_provider`（A7）の非空の不正値を黙って既定 openai へ
    倒れたキーで実送信しない（外部へは生の設定値を出さない固定文言）。"""
    calls = []
    monkeypatch.setattr(A, "_post", lambda *a, **kw: calls.append(1))
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS._connect_openai({"cloud_provider": "not-a-real-provider",
                            "openai_api_key": "sk-real-key-value-for-unit-test"})
    assert not calls
    msg = str(exc_info.value)
    assert "設定が正しくありません" in msg
    assert "クラウド（OpenAI）" in msg
    assert "not-a-real-provider" not in msg   # 生の設定値は外部応答に出さない


def test_connect_openai_rejects_azure_endpoint_without_deployment_without_calling_post(monkeypatch):
    """Azure 等の接続先で用途別デプロイ名（`model_catalog` の openai/subsearch セル・実際に
    下調べ検索が送信する用途）が未解決/組み込み既定のままなら、そのモデル名では送信できないため
    拒否する（`providers.openai_direct_block_reason(usage="subsearch")` 経由）。"""
    calls = []
    monkeypatch.setattr(A, "_post", lambda *a, **kw: calls.append(1))
    sys_s = {"openai_api_key": "sk-real-key-value-for-unit-test",
            "openai_endpoint_kind": "azure",
            "openai_base_url": "https://example.openai.azure.com/openai/v1"}
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS._connect_openai(sys_s)
    assert not calls
    assert "デプロイ名" in str(exc_info.value)


def test_connect_openai_rejects_when_only_chat_deployment_configured_and_subsearch_unset(monkeypatch):
    """preflight は実際に送信する用途（subsearch）のモデル/デプロイ名セルを検査する——chat 用途に
    だけデプロイ名を設定し subsearch 用途を未設定のままにした場合、chat セルだけを見る誤判定なら
    通ってしまうが、実際に `_post` へ送るのは subsearch のモデルであり、それが未解決のままなら
    拒否する（`_post` 呼び出しゼロ＝`llm_calls` 未計測のまま 503）。"""
    calls = []
    monkeypatch.setattr(A, "_post", lambda *a, **kw: calls.append(1))
    sys_s = {"openai_api_key": "sk-real-key-value-for-unit-test",
            "openai_endpoint_kind": "azure",
            "openai_base_url": "https://example.openai.azure.com/openai/v1",
            "model_catalog": {"openai": {"chat": {"allowed": ["my-chat-deployment"],
                                                  "default": "my-chat-deployment"}}}}
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS._connect_openai(sys_s)
    assert not calls
    assert "デプロイ名" in str(exc_info.value)


# ==== max_results: 使用済み Evidence を優先して残す ====

def _entry(ev_id, used):
    return {"evidence_id": ev_id, "used": used, "source_type": "document"}


def test_truncate_preferring_used_keeps_used_over_unused_when_over_limit():
    entries = [_entry("ev-1", False), _entry("ev-2", True), _entry("ev-3", False)]
    kept, dropped_used = RS._truncate_preferring_used(entries, 1)
    assert [e["evidence_id"] for e in kept] == ["ev-2"]
    assert dropped_used == []   # 唯一の used 件は残っている＝落とされていない


def test_truncate_preferring_used_preserves_original_order_among_kept():
    entries = [_entry("ev-1", True), _entry("ev-2", False), _entry("ev-3", True), _entry("ev-4", False)]
    kept, dropped_used = RS._truncate_preferring_used(entries, 3)
    # 使用済み2件(ev-1,ev-3)は必ず残り、残り1枠は未使用の先頭(ev-2)——ただし出力順は元の ev-* 順。
    assert [e["evidence_id"] for e in kept] == ["ev-1", "ev-2", "ev-3"]
    assert dropped_used == []


def test_truncate_preferring_used_noop_when_under_limit():
    entries = [_entry("ev-1", False), _entry("ev-2", True)]
    kept, dropped_used = RS._truncate_preferring_used(entries, 10)
    assert kept == entries
    assert dropped_used == []


def test_truncate_preferring_used_reports_dropped_used_ids_when_used_exceeds_limit():
    """used=True の件数自体が limit を超える場合、優先しても収まりきらない分は切り捨てられる——
    その切り捨てられた evidence_id を `dropped_used_ids` として報告する（`remaining_gaps` へ
    注記するため呼び出し元が使う）。"""
    entries = [_entry("ev-1", True), _entry("ev-2", True), _entry("ev-3", True), _entry("ev-4", False)]
    kept, dropped_used = RS._truncate_preferring_used(entries, 2)
    assert [e["evidence_id"] for e in kept] == ["ev-1", "ev-2"]
    assert dropped_used == ["ev-3"]   # 3件目の used はそれでも切り捨てられる


def _taxcalc_seq():
    """呼ぶたびに新しい list を返す（`_post` は `list.pop` で消費するため使い回せない）。
    帰属呼び出しは2回分（内部＋重複排除後の再帰属・`_success_seq()` と同じ理由）。"""
    return [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAXCALC"}'}}]}}]},
        {"choices": [{"message": {"content": "税計算関連の資料が複数見つかりました。"},
                     "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c3", "function": {"name": "submit_attribution",
             "arguments": '{"used":["ev-7"]}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c4", "function": {"name": "submit_attribution",
             "arguments": '{"used":["ev-7"]}'}}]}}]},
    ]


def _taxcalc_seq_multi_used():
    """`_taxcalc_seq` と同じ探索だが、attribution が複数件（ev-1・ev-3・ev-7）を使ったと
    申告する——`max_results` がそれより小さいと、優先しても収まりきらない used を切り捨てる
    ケースを固定するため。"""
    return [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAXCALC"}'}}]}}]},
        {"choices": [{"message": {"content": "税計算関連の資料が複数見つかりました。"},
                     "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c3", "function": {"name": "submit_attribution",
             "arguments": '{"used":["ev-1","ev-3","ev-7"]}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c4", "function": {"name": "submit_attribution",
             "arguments": '{"used":["ev-1","ev-3","ev-7"]}'}}]}}]},
    ]


def test_max_results_truncation_reports_dropped_used_in_gaps_and_full_used_ev_ids_for_audit(monkeypatch):
    """used=True の件数（3件）が `max_results`（2件）を超える場合:
    - Evidence Packet からそれでも溢れた分（ev-7）は `remaining_gaps` に注記される。
    - `run_research` が返す `used_ev_ids`（監査専用・`ExtResearchRes` には出ない）は
      切り詰めの影響を受けず、実際に使った ev-* の**全集合**（3件）のままである
      （`ext_api.py` の監査 `ev_ids` はこちらを使う契約）。"""
    _install_post(monkeypatch, _taxcalc_seq_multi_used())
    result = RS.run_research(world="v1", query="TAXCALCの仕様は？", scope_paths=[], model=None,
                             max_iterations=None, max_results=2, timeout_s=None, key_id=1,
                             system_settings={})
    packet = result["evidence_packet"]
    assert len(packet["evidence"]) == 2
    kept_ids = {e["evidence_id"] for e in packet["evidence"]}
    assert kept_ids == {"ev-1", "ev-3"}   # ev-* 採番順で先頭2件の used が残る
    assert any("ev-7" in gap for gap in packet["remaining_gaps"]), packet["remaining_gaps"]
    assert set(result["used_ev_ids"]) == {"ev-1", "ev-3", "ev-7"}


def test_max_results_truncation_integration_prefers_actually_used_evidence(monkeypatch):
    """複数件ヒットする実クエリ（fixtures/corpus/v1）で、attribution が使ったと申告した1件が
    `max_results=1` でも生き残ることを固定する（先頭からの単純切り詰めなら ev-1 が残ってしまう）。"""
    _install_post(monkeypatch, _taxcalc_seq())
    full = RS.run_research(world="v1", query="TAXCALCの仕様は？", scope_paths=[], model=None,
                           max_iterations=None, max_results=50, timeout_s=None, key_id=1,
                           system_settings={})
    ev_all = full["evidence_packet"]["evidence"]
    assert len(ev_all) >= 8, "ripgrep 'TAXCALC' は複数ファイルに一致する前提（実測8件）"
    target = next(e for e in ev_all if e["evidence_id"] == "ev-7")
    assert target["used"] is True
    assert target["source_path"] == "4期/03_開発/01_ソース/TAXCALC.cbl"

    _install_post(monkeypatch, _taxcalc_seq())
    capped = RS.run_research(world="v1", query="TAXCALCの仕様は？", scope_paths=[], model=None,
                             max_iterations=None, max_results=1, timeout_s=None, key_id=1,
                             system_settings={})
    ev_capped = capped["evidence_packet"]["evidence"]
    assert len(ev_capped) == 1
    assert ev_capped[0]["evidence_id"] == "ev-7", "単純な先頭切り詰めなら ev-1 が残ってしまうはず"
    assert ev_capped[0]["used"] is True


# ==== iterations と llm_calls の分離 ====

def test_iterations_counts_tool_steps_llm_calls_counts_all_http_calls(monkeypatch):
    """実測: ツール呼び出し2回（ripgrep_search・read_around）＋最終合成＋帰属呼び出し1回
    （openai_style 内部・重複排除で ev-N 採番がずれない単一 citation のため、研究サービス側の
    再帰属は発行されない＝RV12 是正で二重発行を解消）の計4回が `llm_calls`。`iterations`
    （可視ステップ数）はツール呼び出し分の2のみで一致しない。"""
    _install_post(monkeypatch, _success_seq())
    result = RS.run_research(world="v1", query="税率改定の障害は？", scope_paths=[], model=None,
                             max_iterations=None, max_results=20, timeout_s=None, key_id=1,
                             system_settings={})
    assert result["iterations"] == 2
    assert result["llm_calls"] == 4   # ツール2回+最終合成+帰属(内部のみ・再帰属は発行されない)
    assert result["iterations"] != result["llm_calls"]


# ==== cid の外部露出防止（label:world:path・path は末尾に name を含むため再結合しない）====

def test_sanitize_structural_evidence_builds_label_world_path_id_without_duplicating_name():
    """`card_meta.path`（`lens_service.neo4j_related` の `path_names`）は既に対象ノード名を
    末尾に含むため、`name` を再結合すると `ROOT/X/X` のように重複する——重結合しないことを固定する。
    内部 cid（`label:world:rel#name` 形式そのもの）は出さない。world・label は含める
    （追跡可能性の回復・別ノードとの衝突防止）。"""
    meta = [{"source_type": "graph", "verification_method": "graph_node_verified",
            "matched_doc_ids": ["program:test:4期/03_開発/TAXCALC.cbl#TAXCALC"],
            "card_meta": {"name": "TAXCALC", "role": "実装", "category": "プログラム",
                          "path": ["4期", "03_開発", "TAXCALC"], "label": "Program"}}]
    out = RS._sanitize_structural_evidence(meta, "v1")
    assert out[0]["matched_doc_ids"] == ["program:v1:4期/03_開発/TAXCALC"]
    ids_str = str(out[0]["matched_doc_ids"])
    assert "TAXCALC/TAXCALC" not in ids_str, "path が既に末尾に name を含むのに再結合して重複している"
    assert "program:test:" not in ids_str, "内部 cid（label:world:rel#name）そのものが漏れている"
    assert "v1" in ids_str, "world が含まれていない"
    assert "program" in ids_str, "label が含まれていない"


def test_sanitize_structural_evidence_passthrough_for_doc_backed_card():
    """裏付け doc がある graph card（`graph_verified`）は実 doc パスのみを持つため対象外。"""
    meta = [{"source_type": "graph", "verification_method": "graph_verified",
            "matched_doc_ids": ["4期/03_開発/01_ソース/TAXCALC.cbl"],
            "card_meta": {"name": "TAXCALC", "label": "Program"}}]
    assert RS._sanitize_structural_evidence(meta, "v1") == meta


def test_sanitize_structural_evidence_passthrough_for_document_source_type():
    meta = [{"source_type": "document", "verification_method": "span_verified", "matched_doc_ids": None}]
    assert RS._sanitize_structural_evidence(meta, "v1") == meta


def test_sanitize_structural_evidence_falls_back_when_card_meta_empty():
    meta = [{"source_type": "graph", "verification_method": "graph_node_verified",
            "matched_doc_ids": ["label:name"], "card_meta": {}}]
    out = RS._sanitize_structural_evidence(meta, "v1")
    assert out[0]["matched_doc_ids"] == ["node:v1:unknown"]


def test_sanitize_structural_evidence_different_worlds_do_not_collide():
    """同じ label/path でも world が違えば別の id になる（world 欠落だった旧実装の是正）。"""
    meta = [{"source_type": "graph", "verification_method": "graph_node_verified",
            "matched_doc_ids": ["x"], "card_meta": {"name": "N", "label": "Program", "path": ["N"]}}]
    id_a = RS._sanitize_structural_evidence(meta, "world-a")[0]["matched_doc_ids"][0]
    id_b = RS._sanitize_structural_evidence(meta, "world-b")[0]["matched_doc_ids"][0]
    assert id_a != id_b


# ==== X-Request-Id → task_id（§8.1・下流 exec_event 伝播の現状固定） ====

def test_research_task_id_embeds_request_id():
    assert RS.research_task_id("abc-123") == "ext-research:abc-123"


def test_research_task_id_default_without_request_id():
    assert RS.research_task_id(None) == "ext-research"
    assert RS.research_task_id("") == "ext-research"


def test_no_exec_event_build_event_calls_during_successful_research(monkeypatch):
    """§8.1「下流の実行イベントへの伝播」: PART-4 の実行経路（`depth` 既定 "light"）は
    Execution Event v2（`exec_event.build_event`）を一度も呼ばない——runtime spy で実際に
    成功経路を1回走らせて実測する（ソース文字列検査だと、将来 `depth` が変わる・別の呼び出し元が
    増えるといった変化を実行時の振る舞いとしては検出できないことがある）。

    唯一の呼び出し点である `agentic_search._eval_node` は評価フェーズ（`depth in ("medium",
    "deep")`）専用で、`run_research` は `depth` を渡さない（既定 "light"）ため到達しない——
    このテストはその「到達しない」という結果を実行時に固定する。0回である前提が崩れたら、
    その発行点へ `research_task_id()` を実際に `task_id=` として配線すること（`research_task_id`
    docstring 参照・コメントで契約を置き換えず実装で伝播させる）。
    """
    from sherpa import exec_event

    calls = []
    orig_build_event = exec_event.build_event

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return orig_build_event(*args, **kwargs)

    monkeypatch.setattr(exec_event, "build_event", spy)
    _install_post(monkeypatch, _success_seq())
    result = RS.run_research(world="v1", query="税率改定の障害は？", scope_paths=[], model=None,
                             max_iterations=None, max_results=20, timeout_s=None, key_id=1,
                             system_settings={})
    assert result["answer"], "成功経路が実際に完走していない（spy の意味が無い）"
    assert calls == [], f"exec_event.build_event が呼ばれた: {calls}"


# ==== providers/base.py private helper の契約テスト（base.py は無改修で観測のみ）====

def test_dedupe_citations_and_evidence_contract_dedupes_by_key_and_returns_triple():
    """`_dedupe_citations_and_evidence(cites, evidence_meta, world)` の入出力契約:
    `(citations, evidence_meta, dropped)` の3-tuple・citations/evidence_meta は同じ長さ・
    同じ `citation_dedupe_key` の重複は1件に潰れる。"""
    c1 = {"doc_id": _REAL_DOC, "span": [1, 1], "quote": "障害記録"}
    c2 = dict(c1)   # 完全重複
    cites, meta, dropped = PB._dedupe_citations_and_evidence(
        [c1, c2], [{"doc_id": _REAL_DOC, "span": [1, 1]}, {"doc_id": _REAL_DOC, "span": [1, 1]}], "v1")
    assert isinstance(dropped, list)
    assert len(cites) == len(meta)
    assert len(cites) == 1, "同一 citation_dedupe_key は1件に統合される契約"


def test_dedupe_structural_evidence_contract_dedupes_identical_entries():
    """`_dedupe_structural_evidence(items)` の入出力契約: `doc_id=None` の構造 Evidence は
    `matched_doc_ids`/`list_meta`/`card_meta` を鍵に重複排除される（完全一致は1件、
    内容が違えば残る）。"""
    a = {"doc_id": None, "matched_doc_ids": ["x"], "list_meta": {"count": 1}}
    b = dict(a)
    c = {"doc_id": None, "matched_doc_ids": ["y"], "list_meta": {"count": 1}}
    out = PB._dedupe_structural_evidence([a, b, c])
    assert len(out) == 2


def test_evidence_packet_evidence_contract_shape_and_ev_numbering():
    """`_evidence_packet_evidence(evidence_meta, attributed_ev_ids)` の入出力契約:
    `ev-{1始まりの連番}`・必須キー一式・`used` は attributed_ev_ids 判定。"""
    meta = [{"doc_id": "a.md", "span": [1, 2], "verification_method": "span_verified"},
            {"doc_id": "b.md", "span": [3, 4], "verification_method": "span_verified"}]
    out = PB._evidence_packet_evidence(meta, attributed_ev_ids={"ev-2"})
    assert [e["evidence_id"] for e in out] == ["ev-1", "ev-2"]
    assert out[0]["used"] is False and out[1]["used"] is True
    for e in out:
        assert {"evidence_id", "source_type", "source_path", "source_span",
               "verification_method", "used"} <= set(e.keys())


# ==== デッドライン優先度・設定例外変換・帰属可否・digest 整合・usage 合算・scope 再検証 ====

def test_get_system_settings_failure_becomes_provider_unavailable_with_zero_calls(monkeypatch):
    """`store.get_system_settings()` が例外を投げても（DB 障害等）未捕捉のまま伝播させず、
    `ProviderUnavailable`（llm_calls=0）にする（try の外にあった旧実装は 500＋監査属性無しだった）。"""
    monkeypatch.setattr(RS.store, "get_system_settings", lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=None,
                        max_results=20, timeout_s=None, key_id=1, system_settings=None)
    assert exc_info.value.llm_calls == 0


def test_get_system_settings_failure_logs_masked_exception(monkeypatch, caplog):
    """RV7 是正の固定: 設定読取失敗（cause 付きで 503 へ翻訳される例外）も、他の予期しない例外と
    同じく元例外の型とマスク済みメッセージを WARNING ログへ残す——これまではこの経路は `from e`
    で cause こそ保持するものの、それをログへ明示的に残す処理が無く、ext_api.py 側の `from None`
    で外部境界を跨いだ時点で診断の手掛かりが一切残らなくなっていた。"""
    import logging

    monkeypatch.setattr(RS.store, "get_system_settings", lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        with pytest.raises(RS.ProviderUnavailable):
            RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=None,
                            max_results=20, timeout_s=None, key_id=1, system_settings=None)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged
    assert "db down" in logged


def test_invalid_scope_raised_when_pinned_root_scope_check_fails():
    """`scope_paths` は pinned root（共有ロック内で再解決した現在の world）に対して authoritative に
    再検証される——未知の prefix は `InvalidScope`（呼び出し元は 422 にする）。preflight
    （`ext_api._resolve_world_or_error` 後の検証）は別プロセスの rebind 等で世代がずれうるため、
    実行側でも独立に確認する契約を固定する。"""
    with pytest.raises(RS.InvalidScope):
        RS.run_research(world="v1", query="x", scope_paths=["no-such-scope-xyz"], model=None,
                        max_iterations=None, max_results=20, timeout_s=None, key_id=1,
                        system_settings={})


def test_invalid_scope_reclassified_to_timeout_when_deadline_exceeded_during_lock_release(monkeypatch):
    """RV5 是正の固定: `InvalidScope` の raise 時点ではまだ期限内でも、そのスコープを抜ける際の
    共有ロック解放（DB 往復）で時間が経過し、その**後**にデッドラインを越えることがある。
    即座に `InvalidScope`（422）を再送出せず、ロック解放・metering を終えた後の共通デッドライン
    判定を必ず経由させ、その時点で期限超過なら `ResearchTimeout`（504）を優先する契約を、
    偽時計（`time.monotonic`）でロック解放中に時間が進むことを決定的に再現して固定する。"""
    import contextlib

    from sherpa import research_service as _rs

    clock = {"t": 0.0}
    monkeypatch.setattr(_rs.time, "monotonic", lambda: clock["t"])

    @contextlib.contextmanager
    def _lock_that_expires_on_release(world_id, *, timeout_ms=None, connect_timeout=None):
        try:
            yield
        finally:
            # ロック解放（DB 往復）中に時間が経過し、デッドラインを越えたことにする。
            clock["t"] = 1000.0

    monkeypatch.setattr(_rs, "world_lock_shared", _lock_that_expires_on_release)
    with pytest.raises(RS.ResearchTimeout):
        RS.run_research(world="v1", query="x", scope_paths=["no-such-scope-xyz"], model=None,
                        max_iterations=None, max_results=20, timeout_s=30, key_id=1,
                        system_settings={})


def test_scope_walk_deadline_exceeded_reclassifies_to_research_timeout(monkeypatch):
    """RV6 是正の固定: scope_paths の木走査自体（`scope_infer.safe_files` の `deadline` 引数・
    1ディレクトリごとの確認）がリクエスト全体の絶対期限を超えて中断した場合、`InvalidScope`
    （422）ではなく `ResearchTimeout`（504）になる。`ScopeWalkDeadlineExceeded` は `OSError`
    ではないため `except OSError` では捕まらず `except Exception` へ落ちるが、その時点で
    既にデッドライン超過が確定しているため、末尾の共通デッドライン判定が確実に
    `ResearchTimeout` へ倒す（`run_research` が `scope_mod.valid_scope_paths(...,
    deadline=absolute_deadline)` を渡す配線の固定）。"""
    from sherpa import scope_infer

    def _boom_walk(root, *, strict=False, deadline=None):
        raise scope_infer.ScopeWalkDeadlineExceeded("simulated deadline mid-walk")
        yield  # pragma: no cover - 到達しない（generator 形にするためのダミー yield）

    monkeypatch.setattr(scope_infer, "safe_files", _boom_walk)
    # `timeout_s` を既に過去にしておく——末尾の共通デッドライン判定（実時間の `_remaining()`）が
    # このテストの実行時間（ミリ秒未満）に関わらず必ず「超過済み」と判定するようにする
    # （walk 自体の打ち切りタイミングそのものは `tests/unit/test_scope_infer.py` で別途検証済み）。
    with pytest.raises(RS.ResearchTimeout):
        RS.run_research(world="v1", query="x", scope_paths=["4期"], model=None,
                        max_iterations=1, max_results=20, timeout_s=-1, key_id=1,
                        system_settings={})


def test_lock_contention_stays_503_even_if_deadline_already_passed(monkeypatch):
    """ロック競合（`psycopg.errors.LockNotAvailable`）は honest busy signal として 503 のまま返す——
    デッドライン優先の再分類（504化）の対象外にする（そうしないと「busy」というシグナルが
    「timeout」に潰れて消える）。"""
    import contextlib

    import psycopg

    from sherpa import research_service as _rs

    @contextlib.contextmanager
    def _boom_lock(world_id, *, timeout_ms=None, connect_timeout=None):
        raise psycopg.errors.LockNotAvailable("simulated lock contention")
        yield  # pragma: no cover - 到達しない（contextmanager 形にするためのダミー yield）

    monkeypatch.setattr(_rs, "world_lock_shared", _boom_lock)
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=1,
                        max_results=20, timeout_s=5, key_id=1, system_settings={})
    assert "競合" in str(exc_info.value)


def test_lock_contention_logs_masked_exception(monkeypatch, caplog):
    """RV7 是正の固定: ロック競合も他の予期しない例外と同じく元例外の型とマスク済みメッセージを
    WARNING ログへ残す（診断性の統一）。"""
    import contextlib
    import logging

    import psycopg

    from sherpa import research_service as _rs

    @contextlib.contextmanager
    def _boom_lock(world_id, *, timeout_ms=None, connect_timeout=None):
        raise psycopg.errors.LockNotAvailable("simulated lock contention")
        yield  # pragma: no cover - 到達しない（contextmanager 形にするためのダミー yield）

    monkeypatch.setattr(_rs, "world_lock_shared", _boom_lock)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        with pytest.raises(RS.ProviderUnavailable):
            RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=1,
                            max_results=20, timeout_s=5, key_id=1, system_settings={})
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "LockNotAvailable" in logged


def test_world_resolver_failure_logs_masked_exception(monkeypatch, caplog):
    """RV7 是正の固定: world resolver 到達不可（`ExternalResolverError`）も WARNING ログへ
    元例外の型とマスク済みメッセージを残す。"""
    import logging

    from sherpa import worlds as _worlds

    def _boom_resolve(world_id, **kw):
        raise _worlds.ExternalResolverError("simulated registry unreachable")

    monkeypatch.setattr(_worlds, "resolve_external_world", _boom_resolve)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        with pytest.raises(RS.ProviderUnavailable):
            RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=1,
                            max_results=20, timeout_s=5, key_id=1, system_settings={})
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ExternalResolverError" in logged


def test_scope_walk_oserror_logs_masked_exception(monkeypatch, caplog):
    """RV7 是正の固定: scope_paths 走査中の `OSError`（権限エラー等）も WARNING ログへ
    元例外の型とマスク済みメッセージを残す。"""
    import logging

    from sherpa import scope_infer

    def _boom_walk(*a, **kw):
        raise PermissionError("simulated permission error")
        yield  # pragma: no cover - 到達しない（generator 形にするためのダミー yield）

    monkeypatch.setattr(scope_infer, "safe_files", _boom_walk)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        with pytest.raises(RS.ProviderUnavailable):
            RS.run_research(world="v1", query="x", scope_paths=["4期"], model=None,
                            max_iterations=1, max_results=20, timeout_s=5, key_id=1,
                            system_settings={})
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "PermissionError" in logged


def test_unexpected_exception_message_never_leaks_secret_fragment(monkeypatch, caplog):
    """RV5 是正の固定: 予期しない例外（型を問わない・`agentic_search._post` 由来を想定）が
    改行入りキー等で urllib/http.client の例外メッセージへ `Authorization` ヘッダ値ごと
    エコーする場合でも、`run_research` が投げる `ProviderUnavailable` の外部向けメッセージ
    （そのまま `ext_api.py` が HTTPException の detail＝外部レスポンスにする）に生の例外文字列や
    キー断片が一切含まれないことを固定する。原因は（マスク済みで）サーバーログにのみ残る契約。"""
    import logging

    fake_key = "sk-proj-FAKESECRETVALUEFORTEST1234567890ABCDEFGH"

    def _boom(url, headers, body, timeout=90):
        raise RuntimeError(
            f"<urlopen error: invalid header value b'Authorization: Bearer {fake_key}\\r\\n...'>")

    monkeypatch.setattr(A, "_post", _boom)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        with pytest.raises(RS.ProviderUnavailable) as exc_info:
            RS.run_research(world="v1", query="税率改定の障害は？", scope_paths=[], model=None,
                            max_iterations=1, max_results=20, timeout_s=None, key_id=1,
                            system_settings={})
    msg = str(exc_info.value)
    assert fake_key not in msg
    assert "Bearer" not in msg
    assert msg == "AIプロバイダとの通信で予期しないエラーが発生しました。時間をおいて再試行してください。"
    # サーバーログにはマスク済みの原因が残る（生のキーはログにも出さない・マスク痕跡は残る）。
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert fake_key not in logged
    assert "REDACTED" in logged


def test_connection_refused_gets_dedicated_message_naming_provider(monkeypatch, caplog):
    """PART-4a 是正: 接続拒否（例: Ollama 未起動で `urlopen` が `URLError`/`ConnectionRefusedError`
    を投げる）は、汎用の「予期しないエラー」固定文言ではなく、どのプロバイダに繋がらなかったかが
    分かる専用の固定文言にする（provider 名は固定語彙のみ・生の例外文字列や接続先 URL は出さない）。
    実機で観測した事象（サーバログ `URLError: <urlopen error [Errno 111] Connection refused>`）の再現。"""
    import logging
    import urllib.error

    def _boom(url, headers, body, timeout=90):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(A, "_post", _boom)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        with pytest.raises(RS.ProviderUnavailable) as exc_info:
            RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=1,
                            max_results=20, timeout_s=None, key_id=1, system_settings={})
    msg = str(exc_info.value)
    assert msg == "下調べに使う AI（ローカル（Ollama））に接続できません。管理者に設定を確認してください。"
    assert "Errno" not in msg and "urlopen" not in msg   # 外部応答には生の例外文字列を出さない
    # サーバーログには元例外（マスク済み）は残ってよい（実機で観測した文言そのもの）——ここで
    # 固定するのは「接続先 URL 自体は含まれない」こと（`URLError.__str__` は reason のみを含み、
    # urlopen に渡した URL 文字列そのものは含まない）。
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged   # ログには残る（黙って握り潰さない）
    assert "http://" not in logged and "https://" not in logged


def test_per_call_timeout_stays_generic_message_not_connection_specific(monkeypatch):
    """per-call の応答タイムアウト（`TimeoutError`／timeout を reason に包んだ `URLError`）は
    接続失敗集合に**含めない**——全体デッドライン超過は別途 `ResearchTimeout`（504）が優先され、
    デッドラインに余裕が残っている per-call timeout を「AI に接続できません／管理者に設定を
    確認してください」に倒すのは一時的な現象を設定不備と誤認させる。旧来の汎用
    「時間をおいて再試行してください」文言のまま。"""
    import urllib.error

    def _boom(url, headers, body, timeout=90):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(A, "_post", _boom)
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS.run_research(world="v1", query="x", scope_paths=[], model="gpt-5.4-mini",
                        max_iterations=1, max_results=20, timeout_s=None, key_id=1,
                        system_settings={"openai_api_key": "sk-test-fake-key-for-unit-test"})
    assert str(exc_info.value) == (
        "AIプロバイダとの通信で予期しないエラーが発生しました。時間をおいて再試行してください。")


def test_grep_tool_connection_error_does_not_get_ai_connection_message(monkeypatch):
    """接続失敗判定は LLM 送信由来の例外だけに適用する——grep ツール実行
    （`agentic_search.run_tool`）がファイル I/O 起因の `ConnectionResetError`（SMB/NFS 切断等）を
    投げても、「下調べに使う AI に接続できません」という誤った文言にはならない（従来どおり汎用の
    「予期しないエラー」のまま）。"""
    def boom_run_tool(name, args, world, scope_paths, **kw):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(A, "run_tool", boom_run_tool)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
    ]
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=1,
                        max_results=20, timeout_s=None, key_id=1, system_settings={})
    assert str(exc_info.value) == (
        "AIプロバイダとの通信で予期しないエラーが発生しました。時間をおいて再試行してください。")


def test_host_and_network_unreachable_and_tls_errors_get_connection_message(monkeypatch):
    """EHOSTUNREACH(113)/ENETUNREACH(101)/ENETDOWN・`ssl.SSLError` も接続失敗集合に含める
    （いずれも `_post` の物理送信そのものが失敗した例外＝`_sherpa_llm_send_error` マーカー付き）。"""
    import errno
    import ssl

    cases = [
        OSError(errno.EHOSTUNREACH, "No route to host"),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        OSError(errno.ENETDOWN, "Network is down"),
        ssl.SSLError("certificate verify failed"),
    ]
    for exc in cases:
        def _boom(url, headers, body, timeout=90, _exc=exc):
            raise _exc

        monkeypatch.setattr(A, "_post", _boom)
        with pytest.raises(RS.ProviderUnavailable) as exc_info:
            RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=1,
                            max_results=20, timeout_s=None, key_id=1, system_settings={})
        assert str(exc_info.value) == (
            "下調べに使う AI（ローカル（Ollama））に接続できません。管理者に設定を確認してください。"
        ), f"failed for {exc!r}"


def test_final_synthesis_connection_failure_uses_provider_message_not_generic_synthesis_text(monkeypatch):
    """最終合成の HTTP 呼び出しが接続失敗で失敗した場合（`agentic_search` が
    `failure_kind="connection"` を payload に残す）、`synthesis_failed` の汎用文言
    （「回答の合成中に...失敗しました」）ではなく、接続失敗専用の provider 付き固定文言にする。"""
    import urllib.error

    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search",
             "arguments": '{"query":"税率改定に伴う障害の記録"}'}}]}}]},
    ]

    def failing_post(url, headers, body, timeout=90):
        if seq:
            return seq.pop(0)
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(A, "_post", failing_post)
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS.run_research(world="v1", query="税率改定の障害は？", scope_paths=[], model=None,
                        max_iterations=1, max_results=20, timeout_s=None, key_id=1,
                        system_settings={})
    assert str(exc_info.value) == (
        "下調べに使う AI（ローカル（Ollama））に接続できません。管理者に設定を確認してください。")


def test_unexpected_exception_masks_azure_style_key_without_bearer_prefix(monkeypatch, caplog):
    """RV6 是正の固定: Azure OpenAI の `api-key` ヘッダ方式は値そのものがヘッダになり、
    "Bearer "/"api-key: " のような接頭辞を伴わない——汎用パターン（`_BEARER_RE`/
    `_API_KEY_HEADER_RE`）だけでは、接頭辞の無い断片エコーを検出できない。旧実装は
    `_mask_secrets(str(e), None)` と実キーを渡していなかったためこのケースで漏洩した
    （RV 実測再現）。`_connect_openai` が実際に解決したキー（`resolved_secret`）を渡すことで、
    完全一致・断片一致の両方で確実にマスクされることを固定する。"""
    import logging

    azure_key = "AZUREKEYFAKEVALUEFORTEST1234567890abcdefgh"   # "sk-"/"Bearer "を含まない

    def _boom(url, headers, body, timeout=90):
        # 接頭辞なしでキー値そのものだけがエコーされるケースを模す。
        raise RuntimeError(f"invalid header value: {azure_key}")

    monkeypatch.setattr(A, "_post", _boom)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        with pytest.raises(RS.ProviderUnavailable) as exc_info:
            RS.run_research(world="v1", query="x", scope_paths=[], model="gpt-5.4-mini",
                            max_iterations=1, max_results=20, timeout_s=None, key_id=1,
                            system_settings={"openai_api_key": azure_key})
    assert azure_key not in str(exc_info.value)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert azure_key not in logged
    assert "REDACTED" in logged


def test_unexpected_exception_cause_not_chained_to_raw_exception(monkeypatch):
    """RV6 是正の固定: `except Exception` 分岐が構築する `ProviderUnavailable` は raw の元例外を
    `__cause__`/`__context__` として保持しない——保持すると、この例外の traceback が後段
    （ASGI ミドルウェアの delivery-failure ログ等）でフォーマットされた際、Python の
    traceback formatter が例外チェーンを無条件に辿って raw 例外のメッセージ（秘密を含みうる）
    まで出力してしまう。`logging.Formatter().formatException()` で実際にフォーマットした
    文字列を検査して固定する（`exc_info` のタプルを直接渡す・RV が指定した検証方法）。"""
    import logging

    fake_key = "sk-proj-FAKESECRETVALUEFORCAUSECHAINTEST1234567890"

    def _boom(url, headers, body, timeout=90):
        raise RuntimeError(f"boom with secret: {fake_key}")

    monkeypatch.setattr(A, "_post", _boom)
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=1,
                        max_results=20, timeout_s=None, key_id=1, system_settings={})
    exc = exc_info.value
    assert exc.__cause__ is None
    assert exc.__context__ is None
    formatted = logging.Formatter().formatException((type(exc), exc, exc.__traceback__))
    assert fake_key not in formatted
    assert "direct cause" not in formatted
    assert "During handling of the above exception" not in formatted


def test_absolute_deadline_param_used_directly_without_timeout_s_reconversion():
    """RV6 是正の固定: `absolute_deadline` を渡した場合、`timeout_s` から絶対期限を作り直さず
    そのまま使う——巨大な `timeout_s`（999999秒）を同時に渡していても、`absolute_deadline` が
    既に過去なら即座に `ResearchTimeout` になることで、`timeout_s` 経由の再変換をしていないこと
    を確認する（`ext_api.ext_research` がこの引数でハンドラ入口の絶対期限をそのまま渡す契約・
    旧実装は `timeout_s` へ変換してから本関数が改めて絶対期限を計算し直しており、その変換の
    端数切り上げ＋往復時間ぶん元の期限より最大約1秒遅く 200/エラーを返しうる不具合があった）。"""
    import time

    with pytest.raises(RS.ResearchTimeout):
        RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=1,
                        max_results=20, timeout_s=999999, key_id=1, system_settings={},
                        absolute_deadline=time.monotonic() - 1)


def test_turn_timeout_sets_stop_event_proactively_when_deadline_already_passed(monkeypatch):
    """RV8 是正の固定: `_turn_timeout()` は残り時間が既に尽きている場合、`threading.Timer`
    （絶対期限ちょうどに発火・OS スケジューリングの遅延を持ちうる）の発火を待たず、その場で
    `stop_event` を前倒しで発火させる——以後のターンが新規送信できないようにする（「期限後に
    何度も課金/待機し続ける」ことを防ぐ・1回分の下振れだけに抑える）。実際の `threading.Timer`
    を no-op に差し替え、`stop_event` が立つ経路が `_turn_timeout()` 経由だけであることを保証
    した上で固定する。

    FB統合後: ターン1の `_send` は `_resolve_timeout(timeout)`（`_turn_timeout()` を呼び出す）が
    stop_event を立てた**直後**に、`_send` 自身の送信直前チェック（`llm.begin_openai_send`/
    `_consume_call` より前）で即座に `_SendAborted("stop")` として中断される——物理送信
    （`_post`）自体が一切発生しない。旧実装（`_send` が stop_event を確認しなかった）ではターン1も
    実際に送信されていたため、この違いを「ターン0の1回だけが送信され、ターン1用に用意した応答は
    未消費のまま残る」ことで固定する（過剰送信を防ぐ・より安全な挙動への変化）。"""
    from sherpa import research_service as _rs

    class _NoopTimer:
        def __init__(self, interval, function):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(_rs.threading, "Timer", _NoopTimer)

    clock = {"t": 0.0}
    monkeypatch.setattr(_rs.time, "monotonic", lambda: clock["t"])

    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "ripgrep_search", "arguments": '{"query":"y"}'}}]}}]},
    ]

    def fake_post(url, headers, body, timeout=90):
        if seq:
            return seq.pop(0)
        raise AssertionError("stop_event 発火後にターンを送信してはいけない")

    def fake_run_tool(name, args, world, scope_paths, **kw):
        clock["t"] = 1000.0   # ツール実行中に残り時間を使い切ったことにする（毎回呼んでも無害）
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(A, "_post", fake_post)
    monkeypatch.setattr(A, "run_tool", fake_run_tool)

    with pytest.raises(RS.ResearchTimeout):
        RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=5,
                        max_results=20, timeout_s=30, key_id=1, system_settings={})
    assert len(seq) == 1, "ターン0の1回だけ送信されるはず（ターン1は `_send` 自身の停止チェックで中断される）"


def test_synthesis_failed_final_payload_becomes_provider_unavailable_not_empty_200(monkeypatch):
    """`agentic_search.openai_style` の最終合成が通信失敗で `candidate_text=""` へ縮退した場合
    （`final["synthesis_failed"]=True`）、`run_research` は成功として扱わず `ProviderUnavailable`
    にする（黙った空回答の 200 を返さない）。ここでは接続失敗ではない汎用例外（`RuntimeError`）を
    使う——接続失敗（`ConnectionError` 等）専用の provider 付き文言は
    `test_final_synthesis_connection_failure_uses_provider_message_not_generic_synthesis_text`
    で別途固定する（`failure_kind` 判別）。"""
    # `max_iterations=1` でターン数上限に到達させ、tools 無しの「最終合成」専用呼び出し
    # （openai_style の else 分岐）へ到達させる——このシーケンスは turn 1（ripgrep_search）だけを
    # 用意し、最終合成の `_send` を失敗させる。
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search",
             "arguments": '{"query":"税率改定に伴う障害の記録"}'}}]}}]},
    ]

    def failing_post(url, headers, body, timeout=90):
        if seq:
            return seq.pop(0)
        raise RuntimeError("simulated non-network failure during final synthesis")

    monkeypatch.setattr(A, "_post", failing_post)
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS.run_research(world="v1", query="税率改定の障害は？", scope_paths=[], model=None,
                        max_iterations=1, max_results=20, timeout_s=None, key_id=1,
                        system_settings={})
    assert "合成" in str(exc_info.value)


def test_empty_answer_despite_natural_completion_becomes_provider_unavailable(monkeypatch):
    """RV10 是正の固定: 最終合成が `finish_reason="stop"`（自然完了 allowlist・`attribution_eligible`
    が真になる条件）で完了しても、本文が空文字なら黙って成功扱い（`answer=""` の 200）にしない
    ——通信自体には失敗していない（`synthesis_failed` は立たない）が、実質的にはモデルが有効な
    回答を返せなかったのと同じであり、`synthesis_failed` と同じ `ProviderUnavailable` にする。"""
    _install_post(monkeypatch, [
        # ツール呼び出しをせず、いきなり空文字＋自然完了で終わるケース（`no_tool_calls` 分岐）。
        {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
    ])
    with pytest.raises(RS.ProviderUnavailable) as exc_info:
        RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=None,
                        max_results=20, timeout_s=None, key_id=1, system_settings={})
    assert "空の応答" in str(exc_info.value)


def test_refusal_response_is_not_misclassified_as_empty_answer(monkeypatch):
    """RV11 是正の固定: OpenAI の refusal（拒否）応答（`content=None`・`refusal="..."`・
    `finish_reason="stop"`）は `agentic_search._openai_style_text` が拒否理由の文章を本文として
    拾うため、`answer_text` は空にならず、上の「空回答→ProviderUnavailable」検出には引っかからず
    200（拒否理由が answer に載る）で成功する。"""
    _install_post(monkeypatch, [
        {"choices": [{"message": {"content": None, "refusal": "この内容にはお答えできません。"},
                     "finish_reason": "stop"}]},
    ])
    result = RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=None,
                             max_results=20, timeout_s=None, key_id=1, system_settings={})
    assert result["answer"] == "この内容にはお答えできません。"


def test_empty_answer_from_budget_exceeded_path_is_not_reclassified(monkeypatch):
    """budget_exceeded（call 予算切れ・意図的に合成を試みず空回答で打ち切る既存契約）は
    `attribution_eligible` に到達する前に early return するため、上の新しい空回答検出には
    引っかからない——`ResearchTimeout`/`ProviderUnavailable` へ誤って倒れないことを固定する。"""
    from sherpa.agentic_search import _CallBudget

    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAXCALC"}'}}]}}]},
    ]
    _install_post(monkeypatch, seq)
    # call_budget を最初の1回（ripgrep_search 応答の消費）で使い切らせ、最終合成の直前で
    # budget_exceeded 分岐（`_consume_call` が False を返す）へ入らせる。
    orig_openai_style = A.openai_style

    def _capped_openai_style(*a, **kw):
        kw["call_budget"] = _CallBudget(1)
        return orig_openai_style(*a, **kw)

    monkeypatch.setattr(A, "openai_style", _capped_openai_style)
    result = RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=None,
                             max_results=20, timeout_s=None, key_id=1, system_settings={})
    assert result["answer"] == ""
    assert result["evidence_packet"]["stop_reason"] == "budget_exceeded"


def test_attribution_skipped_when_not_eligible_leaves_used_false(monkeypatch):
    """`finish_reason` が自然完了 allowlist（"stop"）以外（例: 打ち切り経由の最終合成）のときは
    `attribution_eligible=False` になり、`run_research` は重複排除後の再帰属を試みない——
    `submit_attribution` の応答を1つも消費せず、Evidence の `used` は全件 False のままになる。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search",
             "arguments": '{"query":"税率改定に伴う障害の記録"}'}}]}}]},
        # finish_reason を明示しない（"stop" 以外）＝非自然完了。もし再帰属が誤って呼ばれれば
        # このシーケンスが尽きて `IndexError`（list.pop）になり、テストが検出する。
        {"choices": [{"message": {"content": "税率改定に伴う障害です。"}}]},
    ]
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))
    result = RS.run_research(world="v1", query="税率改定の障害は？", scope_paths=[], model=None,
                             max_iterations=None, max_results=20, timeout_s=None, key_id=1,
                             system_settings={})
    assert result["answer"]
    for e in result["evidence_packet"]["evidence"]:
        assert e["used"] is False


def test_digest_truncation_filters_packet_and_notes_remaining_gaps(monkeypatch):
    """`build_evidence_digest` が件数/バイト上限で一部の ev-N を打ち切った場合、Packet の
    `evidence[]` はそれらを含めず（`adopted_ev_ids` で絞る）、省略件数を `remaining_gaps` に
    注記する（`providers/base.py::_omitted_evidence_gap_note` を再利用）。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAXCALC"}'}}]}}]},
        {"choices": [{"message": {"content": "税計算関連の資料が複数見つかりました。"},
                     "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c3", "function": {"name": "submit_attribution", "arguments": '{"used":[]}'}}]}}]},
    ]
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    real_digest = A.build_evidence_digest

    def fake_digest(citations_, combined_evidence_meta):
        text, ev_map = real_digest(citations_, combined_evidence_meta)
        truncated = {"ev-1": ev_map.get("ev-1", [])} if "ev-1" in ev_map else ev_map
        return text, truncated

    monkeypatch.setattr(A, "build_evidence_digest", fake_digest)
    result = RS.run_research(world="v1", query="TAXCALCの仕様は？", scope_paths=[], model=None,
                             max_iterations=None, max_results=50, timeout_s=None, key_id=1,
                             system_settings={})
    packet = result["evidence_packet"]
    assert len(packet["evidence"]) == 1, "打ち切り後の ev_map に無いエントリが Packet に残っている"
    assert packet["evidence"][0]["evidence_id"] == "ev-1"
    assert any("上限超過" in g for g in packet["remaining_gaps"]), (
        f"省略件数の注記が remaining_gaps に無い: {packet['remaining_gaps']}")


def test_attribution_usage_merges_onto_main_loop_tokens_not_replacing_them(monkeypatch):
    """再帰属呼び出し（`attribute_openai_style`）が消費したトークンは、主ループの累計へ
    **加算**される——空の accumulator から始めて上書きすると主ループ分のトークンが失われる。

    研究サービス側の再帰属は、重複排除で ev-N 採番が実際にずれた場合だけ発行される（RV12 是正・
    二重発行の是正）ため、ここでは重なる span（`[1,2]`/`[2,3]`）を持つ2件の citation を用意し、
    `providers/base.py::_dedupe_citations_and_evidence` の重なり統合で2件→1件に減る（＝ev-N
    採番がずれる）シナリオを固定する——このシナリオでのみ再帰属が実際に発行され、その usage も
    主ループの累計へ正しく加算されることを検証する。"""
    def fake_run_tool(name, args, world, scope_paths, **kw):
        c1 = {"doc_id": _REAL_DOC, "span": [1, 2], "quote": "障害記録", "ext": ".md"}
        c2 = {"doc_id": _REAL_DOC, "span": [2, 3], "quote": "障害記録", "ext": ".md"}
        return ({"hits": []}, {_REAL_DOC}, [c1, c2], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search",
             "arguments": '{"query":"税率改定に伴う障害の記録"}'}}]}}]},
        {"choices": [{"message": {"content": "税率改定に伴う障害です。"}, "finish_reason": "stop"},
                    ],
         "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c3", "function": {"name": "submit_attribution", "arguments": '{"used":["ev-1"]}'}}]}}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c4", "function": {"name": "submit_attribution", "arguments": '{"used":["ev-1"]}'}}]}}],
         "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
    ]
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))
    calls = []
    monkeypatch.setattr(RS.metering, "record", lambda *a, **kw: calls.append((a, kw)))
    RS.run_research(world="v1", query="税率改定の障害は？", scope_paths=[], model=None,
                    max_iterations=None, max_results=20, timeout_s=None, key_id=1,
                    system_settings={})
    assert len(calls) == 1
    tokens = calls[0][0][3]   # metering.record(kind, provider, model, usage, ...) の位置引数4番目
    assert tokens is not None
    assert tokens["input_tokens"] == 100 + 5 + 7, tokens
    assert tokens["output_tokens"] == 10 + 1 + 2, tokens


def test_metering_record_uses_fixed_small_timeout_not_residual_deadline(monkeypatch):
    """RV8 是正の固定: `finally` の `metering.record()` はリクエストの残り時間
    （`_remaining()`）を再利用せず、`_METERING_DB_TIMEOUT_S`（固定・小さい）で bound する——
    この時点では残り時間が既に 0 以下になっていることがあり得るため、残り時間ベースにすると
    「即座に諦める」（記録を試みる前に諦める）か「無期限」のどちらかに倒れてしまう。「記録は
    試みるが無期限にはブロックしない」という独立の契約を固定する。"""
    monkeypatch.setattr(A, "_post",
                        lambda url, headers, body, timeout=90: {"choices": [{"message": {"content": "ok"}}]})
    calls = []
    monkeypatch.setattr(RS.metering, "record", lambda *a, **kw: calls.append(kw))
    # timeout_s を極端に小さくし、残り時間がほぼ 0（またはテスト実行時間次第で負）になるようにする
    # ——それでも metering.record への timeout は固定値のままであることを確認する。
    RS.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=None,
                    max_results=20, timeout_s=5, key_id=1, system_settings={})
    assert len(calls) == 1
    assert calls[0]["connect_timeout"] == RS._METERING_DB_TIMEOUT_S
    assert calls[0]["statement_timeout_ms"] == RS._METERING_DB_TIMEOUT_S * 1000


def test_scope_validation_uses_freshly_resolved_root_not_a_stale_one(monkeypatch):
    """`scope_paths` の authoritative 再検証は、共有ロック内で今まさに解決した root
    （`worlds.resolve_external_world` の戻り値）を使う——preflight 時点の別の（世代がずれた）
    root を混ぜて呼ばないことを配線レベルで固定する（順序競合対策の実地確認）。"""
    from sherpa import research_service as _rs
    from sherpa import worlds as _worlds

    seen = {}
    real_valid = scope_mod.valid_scope_paths

    def spy_valid(world_id, sp, root=None, strict=False, deadline=None):
        seen["root"] = root
        return real_valid(world_id, sp, root=root, strict=strict, deadline=deadline)

    monkeypatch.setattr(_rs.scope_mod, "valid_scope_paths", spy_valid)
    # 最小の1発完了シーケンス（no-tool-call の即時回答）——finish_reason 無しなので帰属も
    # 発動しない・このテストの関心事（scope 再検証の root）とは無関係な呼び出しを増やさない。
    monkeypatch.setattr(A, "_post",
                        lambda url, headers, body, timeout=90: {"choices": [{"message": {"content": "ok"}}]})
    _rs.run_research(world="v1", query="x", scope_paths=[], model=None, max_iterations=None,
                     max_results=20, timeout_s=None, key_id=1, system_settings={})
    assert seen["root"] == _worlds.world_dir("v1")
