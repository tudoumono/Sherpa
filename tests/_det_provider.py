"""テスト専用の決定的な頭脳（プロダクトには存在しない）。

チャットの頭脳は Codex 調査と簡易だけで、AI なしの定型回答は閉じている。ただし /chat・SSE・保存・
監査の配線テストは LLM を起動せずに「取得は本物（Neo4j/grep）・生成はテンプレ」の回答を要るため、
保存済み agent が `heuristic`（テストの既定・`tests/_world_setup.py`）のときだけ、`tests/conftest.py` が
`sherpa.providers._DisabledProvider("heuristic")` をこの頭脳へ差し替える。閉じた頭脳の案内そのものを
検証するテストは `sherpa.providers._DisabledProvider_real` を使う。
"""
from __future__ import annotations

import time
from typing import Iterator

from sherpa.providers.base import Ctx, Provider, _node, _plain_run


class DeterministicTestProvider(Provider):
    label, model = "テスト用の決定的な頭脳", "—"
    provider_id = "heuristic"    # LLM を呼ばない＝usage なし

    def run(self, ctx: Ctx) -> Iterator[dict]:
        if not ctx.knowledge:
            yield from _plain_run(self, ctx)
            return
        # `agents._gather` の monkeypatch を効かせるため facade 経由で実行時解決する。
        from sherpa import agents as _facade
        decision = env = None
        for ev in _facade._gather(ctx):
            if isinstance(ev, dict) and ev.get("type") == "_env":
                decision, env = ev["decision"], ev["env"]
            else:
                yield ev
        if env is None:
            return
        yield _node("compose", "think", "回答を作成", "出典を添えて整えています", "active")
        if ctx.pace:
            time.sleep(ctx.pace)
        yield _node("compose", "think", "回答を作成", "回答を作成しました", "done")
        yield {"type": "answer_delta", "text": env["headline"]}
        yield {"type": "_result", "env": env, "decision": decision}


def install() -> None:
    """閉じた頭脳 `heuristic` の案内 provider（`_DisabledProvider("heuristic")`）をテスト頭脳へ差し替える。"""
    import sherpa.providers as P
    if getattr(P, "_DisabledProvider_real", None) is not None:
        return
    real = P._DisabledProvider
    P._DisabledProvider_real = real

    class _TestDisabledProvider(real):
        def __new__(cls, agent: str):
            if agent == "heuristic":
                return DeterministicTestProvider()
            return super().__new__(cls)

    P._DisabledProvider = _TestDisabledProvider
