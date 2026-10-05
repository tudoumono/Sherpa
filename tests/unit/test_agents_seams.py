"""agents facade patch シームテスト（リファクタリング計画フェーズ5 S1・store フェーズ4の教訓の先取り）。

store フェーズ4（`sherpa/store.py` → `sherpa/store/` パッケージ化）の RV では、facade からの
再エクスポート後に「ローカル束縛（`from sherpa.store import X` を分割先モジュール内で行い、
以後 `X` を直接呼ぶ）」へ変わった箇所が、`store.X` への monkeypatch を素通りさせる不具合が
見つかった（`_audit_insert` 等）。agents.py → `sherpa/providers/` パッケージ化でも同じ危険が
計画書フェーズ5節で名指しされている継ぎ目が2つある:

  - `_gather`（CodexProvider._run_authoring が呼ぶ「本物の取得」フック。
    `tests/unit/test_agents_author.py` が `agents._gather` を patch する）。
  - `_select_provider` の registry（各 Provider クラスを facade 属性として実行時解決する）。

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


# ===== `_select_provider`（registry）経由 =====


def test_provider_info_uses_same_fresh_snapshot_for_agent_and_label_no_generation_drift(monkeypatch):
    """WEB-1 是正: fresh read とキャッシュ済み `get_system_settings()` に相反する中央設定を
    仕込んでも、`provider_info()` の "agent"（agent 未設定時の `default_agent` 経由）は fresh
    スナップショットの値で決まる（`get_provider` 側の label/model と同じ世代）。`effective_agent`
    がキャッシュ側を別途読んでいたら、fresh では Codex の認証（中央 OpenAI キー）があるのにキャッシュ
    では無い（"simple"）という別世代の食い違いが生じ得た。"""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setenv("CODEX_HOME", "/nonexistent-codex-home")
    monkeypatch.delenv("SHERPA_AGENT", raising=False)
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh",
                        lambda **kw: {"openai_api_key": "sk-real-key-for-test"})
    # キャッシュ（get_system_settings）には相反する値（キー無し）を仕込む——provider_info がこちらを
    # 誤って読んでいたら "agent" が "simple" になってしまう。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda **kw: {})
    info = A.provider_info({})
    assert info["agent"] == "codex", (
        "provider_info が fresh スナップショットでなくキャッシュを見ている＝設定世代がずれている"
    )


