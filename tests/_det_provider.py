"""テスト専用の決定的な頭脳（プロダクトには存在しない）。

チャットの頭脳は Codex 調査と簡易だけで、AI なしの定型回答は閉じている。ただし /chat・SSE・保存・
監査の配線テストは LLM を起動せずに「取得は本物（Neo4j/grep）・生成はテンプレ」の回答を要るため、
保存済み agent が `heuristic`（テストの既定・`tests/_world_setup.py`）のときだけ、`tests/conftest.py` が
`sherpa.providers.get_provider` をこの頭脳を返すものへ差し替える。差し替え前の実体は
`sherpa.providers._get_provider_real`。
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
    """保存済み agent が `heuristic` のときだけ `get_provider` をテスト頭脳を返すものへ差し替える。"""
    import sherpa.providers as P
    if getattr(P, "_get_provider_real", None) is not None:
        return
    real = P.get_provider
    P._get_provider_real = real

    def get_provider(settings: dict | None = None, system_settings: dict | None = None):
        saved = (settings or {}).get("agent")
        if isinstance(saved, str) and saved.strip().lower() == "heuristic":
            from sherpa.providers.prompts import ANSWER_POLICY
            p = DeterministicTestProvider()
            p.system_prompt = ANSWER_POLICY
            return p
        return real(settings, system_settings)

    P.get_provider = get_provider
