"""`HeuristicProvider`（リファクタリング計画 フェーズ5 S4・`sherpa/agents.py` から純移動）。

LLM 未接続のときの決定的な頭脳。ツールは本物（Neo4j/grep）＝取得は本物・生成はテンプレ。
`sherpa/agents.py` が facade として本モジュールから再エクスポートするため、
`_select_provider`（まだ agents.py に残る）の `return HeuristicProvider()` は無改修で動く。

**シーム規則（危険な継ぎ目・`tests/unit/test_agents_seams.py` が固定）**:
`run()` 内の `_gather` 呼び出しは `from sherpa import agents as _facade` で**実行時解決**する
（`sherpa/providers/base.py` の `_GenProvider.run` と同じ方式・その docstring 参照）。理由:
`tests/unit/test_agents_seams.py::test_gather_seam_intercepted_by_heuristic_provider` が
`monkeypatch.setattr(agents, "_gather", fake)` で `sherpa.agents._gather`（facade の re-export
属性）を差し替えて介入を検証している。本モジュール内でモジュールレベルの `_gather`（`.base` から
直接 import した名前）をそのまま呼ぶと、patch 対象は `sherpa.providers.base`／本モジュール自身の
名前空間ではなく `sherpa.agents` 側の属性のため、素通りしてしまう（Python の名前束縛はコピーで
別名参照ではない）。`_plain_run`・`_node` はテストが個別に patch する対象ではないため直接 import
でよい。
"""
from __future__ import annotations

import time
from typing import Iterator

from .base import Ctx, Provider, _node, _plain_run
from .prompts import _AUTHOR_FALLBACK_NOTE


class HeuristicProvider(Provider):
    """LLM 未接続のときの決定的な頭脳。ツールは本物（Neo4j/grep）＝取得は本物・生成はテンプレ。"""
    label, model = "簡易（AIなし）", "—"
    provider_id = "heuristic"    # F3: LLM を呼ばない＝usage なし（env に usage を載せない）。

    def run(self, ctx: Ctx) -> Iterator[dict]:
        if not ctx.knowledge:                          # ナレッジ参照オフ＝素の会話（AIなしは正直な定型文）
            yield from _plain_run(self, ctx); return
        # シーム規則（フェーズ5 S4・危険な継ぎ目・モジュール docstring 参照）: `_gather` は facade
        # （`sherpa.agents`）属性経由で実行時解決する（`agents._gather` の monkeypatch を効かせ続けるため）。
        from sherpa import agents as _facade
        decision = env = None
        for ev in _facade._gather(ctx):
            if isinstance(ev, dict) and ev.get("type") == "_env":
                decision, env = ev["decision"], ev["env"]
            else:
                yield ev
        if env is None:                                # _gather が clarify question を出して停止＝確認待ち（RV High）
            return
        if decision.get("lens") == "author":           # P1-a: 他頭脳は資料を作らず下書き案内を前置
            env["headline"] = _AUTHOR_FALLBACK_NOTE + env.get("headline", "")
        yield _node("compose", "think", "回答を作成", "出典を添えて整えています", "active")
        if ctx.pace:
            time.sleep(ctx.pace)
        yield _node("compose", "think", "回答を作成", "回答を作成しました", "done")
        yield {"type": "answer_delta", "text": env["headline"]}
        yield {"type": "_result", "env": env, "decision": decision}
