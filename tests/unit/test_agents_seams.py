"""agents facade patch シームテスト（リファクタリング計画フェーズ5 S1・store フェーズ4の教訓の先取り）。

store フェーズ4（`sherpa/store.py` → `sherpa/store/` パッケージ化）の RV では、facade からの
再エクスポート後に「ローカル束縛（`from sherpa.store import X` を分割先モジュール内で行い、
以後 `X` を直接呼ぶ）」へ変わった箇所が、`store.X` への monkeypatch を素通りさせる不具合が
見つかった（`_audit_insert` 等）。agents.py → `sherpa/providers/` パッケージ化でも同じ危険が
計画書フェーズ5節で名指しされている継ぎ目が2つある:

  - `_gather`（HeuristicProvider/_GenProvider/CodexProvider._run_authoring が共通で呼ぶ「本物の
    取得」フック。`tests/unit/test_agents_author.py` など複数テストが `agents._gather` を patch する）。
  - `_select_provider` の registry（`BedrockProvider`／`_bedrock_auth_available` を
    `tests/unit/test_health.py` 等が facade 属性として直接代入/monkeypatch する）。

このファイルの各テストは**現状（分割前・純粋に同一モジュール内の関数呼び出し）では必ず緑**になる
（同一モジュール内の関数呼び出しはグローバル名解決＝`func.__globals__` がモジュールの `__dict__` その
ものなので、`monkeypatch.setattr(agents, "_gather", fake)` は既存の呼び出し元にそのまま効く）。

**分割スライス（S3〜S11）で、呼び出し元がローカル束縛（`from ..base import _gather` した上で
`_gather(...)` と直接呼ぶ等）に変わると、このテストだけが落ちる**ことが目的。分割時は計画書が
指示する通り「facade 属性経由の実行時解決」（例: 関数内 `from sherpa import agents as _facade` して
`_facade._gather(...)` と呼ぶ）を維持すること。
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A


def _fail_if_called(*_a, **_k):
    raise AssertionError("route/dispatch が呼ばれた＝_gather の facade patch が素通りしている")


# ===== `_gather` シーム: HeuristicProvider.run() 経由 =====

def test_gather_seam_intercepted_by_heuristic_provider(monkeypatch):
    """`agents._gather` を monkeypatch すると、HeuristicProvider().run(ctx) の駆動経路
    （knowledge=True→`_gather(ctx)` を呼ぶ分岐）がその偽実装を通ることを確認する。

    HeuristicProvider は subprocess/HTTP を一切起こさない最小の駆動経路（LLM 未接続の決定的頭脳）
    のため、_gather の継ぎ目だけを切り出して検証できる。
    """
    calls = []

    def fake_gather(ctx):
        calls.append(ctx)
        yield A._node("fake", "think", "偽の取得", "s1 pin", "done")
        yield {
            "type": "_env",
            "decision": {"lens": "qa", "input": ctx.message, "reason": "fake-seam"},
            "env": {"headline": "fake headline from patched _gather",
                     "summary": {"total": 0}, "data": {}, "sources": []},
        }

    monkeypatch.setattr(A, "_gather", fake_gather)

    ctx = A.Ctx(message="s1 seam test", world="v1",
                route=_fail_if_called, dispatch=_fail_if_called, knowledge=True)
    events = list(A.HeuristicProvider().run(ctx))

    assert len(calls) == 1 and calls[0] is ctx, (
        "monkeypatch した agents._gather が HeuristicProvider().run() から呼ばれていない"
        "（facade patch が素通りしている＝分割時にローカル束縛化した可能性）"
    )
    assert any(isinstance(e, dict) and e.get("id") == "fake" for e in events), (
        "fake _gather が yield した node が run() の出力にそのまま通っていない"
    )
    result = next(e for e in events if isinstance(e, dict) and e.get("type") == "_result")
    assert result["env"]["headline"] == "fake headline from patched _gather"
    assert result["decision"]["lens"] == "qa"


# ===== `_gather` シーム: _GenProvider.run() 経由 =====

def test_gather_seam_intercepted_by_gen_provider(monkeypatch):
    """`agents._gather` の monkeypatch が `_GenProvider.run()`（base.py 側の呼び出し・facade 実行時解決）
    にも効くことを確認する（RV LOW 2026-07-14: Heuristic 経由のテストだけでは base.py 側の
    ローカル束縛化の退行を直接検知できない指摘の回収。CodexProvider._run_authoring 経由は
    `tests/unit/test_agents_author.py::test_authoring_lock_released_on_generator_close` が検知器）。

    `_stream` をスタブ化した最小サブクラスで駆動する（HTTP なし・`make_sources=None` なので
    agentic 分岐は通らず `_gather` 経路に直行する）。
    """
    calls = []

    def fake_gather(ctx):
        calls.append(ctx)
        yield A._node("fake", "think", "偽の取得", "rv low-2 pin", "done")
        yield {
            "type": "_env",
            "decision": {"lens": "qa", "input": ctx.message, "reason": "fake-seam"},
            "env": {"headline": "placeholder", "summary": {"total": 0}, "data": {}, "sources": []},
        }

    class _StubGen(A._GenProvider):
        label = "stub"

        def _stream(self, prompt):
            yield "stub-answer"

    monkeypatch.setattr(A, "_gather", fake_gather)

    ctx = A.Ctx(message="gen seam test", world="v1",
                route=_fail_if_called, dispatch=_fail_if_called, knowledge=True)
    events = list(_StubGen().run(ctx))

    assert len(calls) == 1 and calls[0] is ctx, (
        "monkeypatch した agents._gather が _GenProvider.run() から呼ばれていない"
        "（base.py がローカル束縛化した可能性＝facade 実行時解決の退行）"
    )
    result = next(e for e in events if isinstance(e, dict) and e.get("type") == "_result")
    assert result["env"]["headline"] == "stub-answer"   # _stream スタブの回答が env に反映される
    assert result["decision"]["lens"] == "qa"


# ===== `_select_provider` シーム: get_provider() 経由（registry） =====

class _FakeBedrockProvider:
    """`BedrockProvider` facade patch 用の最小フェイク（subprocess/HTTP なし）。"""

    def __init__(self, region, model, api_key):
        self.region, self.model, self.api_key = region, model, api_key


def test_provider_info_reports_effective_agent_not_saved_when_a7_mismatches(monkeypatch):
    """`provider_info()` のヘッダバッジ用 "agent" は保存値ではなく effective_agent()
    （A7 で選択中でないクラウド系 agent は ollama 扱い）を返す。保存済み agent=openai だが
    A7 選択が gemini のときに "ollama" を返すことで、実行（`get_provider`）とバッジ表示が
    食い違わないことを確認する。

    WEB-1: `provider_info()` の system_settings 読取点は `store._read_system_settings_fresh()`
    （共有キャッシュを介さない fresh read・`get_provider()` と同じ世代を共有する）——
    `get_system_settings` ではない。"""
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh", lambda: {"cloud_provider": "gemini"})
    info = A.provider_info({"agent": "openai", "openai_api_key": "x"})
    assert info["agent"] == "ollama"


def test_provider_info_uses_same_fresh_snapshot_for_agent_and_label_no_generation_drift(monkeypatch):
    """WEB-1 是正: fresh read とキャッシュ済み `get_system_settings()` に相反する A7 選択を
    仕込んでも、`provider_info()` の "agent"（`effective_agent` 経由）は fresh スナップショットの
    値で決まる（`get_provider` 側の label/model と同じ世代）。`effective_agent` がキャッシュ側を
    別途読んでいたら、fresh では A7 一致（"openai" のまま）なのにキャッシュでは不一致
    （"ollama" へフォールバック）という別世代の食い違いが生じ得た。"""
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh", lambda: {"cloud_provider": "openai"})
    # キャッシュ（get_system_settings）には相反する値を仕込む——provider_info がこちらを誤って
    # 読んでいたら "agent" が "ollama"（A7 不一致のフォールバック）になってしまう。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda **kw: {"cloud_provider": "gemini"})
    info = A.provider_info({"agent": "openai", "openai_api_key": "x"})
    assert info["agent"] == "openai", (
        "provider_info が fresh スナップショットでなくキャッシュ（相反する A7 選択）を"
        "見ている＝設定世代がずれている"
    )


def test_select_provider_seam_bedrock_auth_unavailable_falls_back_to_unwired(monkeypatch):
    """`agents._bedrock_auth_available` を False に patch すると、`get_provider({"agent": "bedrock"})`
    が `_UnwiredProvider`（未接続の正直な案内）へフォールバックすることを確認する。
    """
    # 4構成（2026-08-15）: bedrock は env で有効化した時だけ実行できる（`agent_constructs`）。
    # ここで検証したいのは facade patch のシームなので、有効化した上で従来の分岐を確認する。
    # A7: bedrock を選択中のプロバイダにする（既定 openai のままだと effective_agent() が
    # 先に ollama へ倒してしまい、この分岐（_bedrock_auth_available）まで到達しない）。
    monkeypatch.setenv("SHERPA_EXTRA_AGENTS", "bedrock")
    # WEB-1: `get_provider()` の1ターン唯一の読取点は `store._read_system_settings_fresh()`
    # （共有キャッシュを介さない生の読取・TOCTOU 対策）——`get_system_settings` ではない。
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh", lambda: {"cloud_provider": "bedrock"})
    monkeypatch.setattr(A, "_bedrock_auth_available", lambda api_key=None: False)
    p = A.get_provider({"agent": "bedrock"})
    assert isinstance(p, A._UnwiredProvider), (
        "_bedrock_auth_available を False に patch したのに UnwiredProvider にならない"
        "（facade patch が素通りしている可能性）"
    )


def test_select_provider_seam_bedrock_auth_available_uses_patched_class(monkeypatch):
    """`agents._bedrock_auth_available` を True・`agents.BedrockProvider` をフェイクに patch すると、
    `get_provider({"agent": "bedrock"})` がそのフェイククラスのインスタンスを返すことを確認する
    （`_select_provider` の registry 経由の参照が facade 属性経由であることの pin）。
    """
    monkeypatch.setenv("SHERPA_EXTRA_AGENTS", "bedrock")   # 4構成: env で有効化してから検証する
    # A7（クラウドプロバイダ排他選択）: bedrock を選択中のプロバイダにしないと
    # `_select_provider` が未接続で早期returnする（`sherpa.keys.selected_cloud_provider`）。
    # WEB-1: `get_provider()` の1ターン唯一の読取点は `store._read_system_settings_fresh()`
    # （共有キャッシュを介さない生の読取・TOCTOU 対策）——`get_system_settings` ではない。
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh",
                        lambda: {"personal_api_keys_allowed": True, "cloud_provider": "bedrock"})
    monkeypatch.setattr(A, "_bedrock_auth_available", lambda api_key=None: True)
    monkeypatch.setattr(A, "BedrockProvider", _FakeBedrockProvider)
    # `bedrock_region`（個人設定）は撤去済み＝settings に含めても _select_provider は
    # 読まず、常に None（region 東京固定は BedrockProvider 側の責務）を渡す。
    p = A.get_provider({"agent": "bedrock", "bedrock_region": "jp",
                        "bedrock_model": "m", "bedrock_api_key": "k"})
    assert isinstance(p, _FakeBedrockProvider), (
        "monkeypatch した agents.BedrockProvider が _select_provider から使われていない"
        "（facade patch が素通りしている＝分割時にローカル束縛化した可能性）"
    )
    assert p.region is None and p.model == "m" and p.api_key == "k"
