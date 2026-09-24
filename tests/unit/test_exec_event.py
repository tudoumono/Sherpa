"""exec_event（Execution Event v2 ビルダ）の単体テスト（EXT-1・純関数のみ・DB/Neo4j 不要）。

- `EVENT_TYPES`: 設計書 §2.4 の26種を過不足なく持つことをピン留めする。
- `kind_for_event_type`: §2.4 の対応表（prefix/exact 両方）を全種別について固定する。
- `build_event`: v1 最小契約（id/kind/label/detail/status）を必ず埋める・v2 新規フィールドは
  未指定なら None・`event_type`/`phase` はクローズド語彙外だと ValueError。
"""
from __future__ import annotations

import pytest

from sherpa import exec_event as EE

# 設計書 §2.4 の26種別と、それぞれが対応すべき kind（本文の対応表どおりに手で書き下す＝実測ピン）。
_EXPECTED_KIND = {
    "run_started": "think", "plan_created": "think", "task_created": "think", "task_delegated": "think",
    "agent_started": "agent", "agent_completed": "agent", "agent_failed": "agent", "agent_cancelled": "agent",
    "tool_started": "tool", "tool_completed": "tool", "tool_failed": "tool",
    "candidate_discovered": "evidence", "candidate_verified": "evidence", "candidate_rejected": "evidence",
    "evidence_committed": "evidence",
    "evaluation_completed": "evaluation", "replan_requested": "evaluation",
    "additional_task_created": "think",
    "budget_updated": "think", "budget_limit_reached": "think",
    "hook_started": "hook", "hook_completed": "hook", "hook_failed": "hook",
    "finalization_started": "think",
    "run_completed": "think", "run_stopped": "think",
}


def test_event_types_matches_design_doc_2_4_exactly():
    assert EE.EVENT_TYPES == frozenset(_EXPECTED_KIND)
    assert len(EE.EVENT_TYPES) == 26


@pytest.mark.parametrize("event_type,expected_kind", sorted(_EXPECTED_KIND.items()))
def test_kind_for_event_type_matches_2_4_table(event_type, expected_kind):
    assert EE.kind_for_event_type(event_type) == expected_kind


def test_kind_for_event_type_none_or_unknown_defaults_to_think():
    assert EE.kind_for_event_type(None) == "think"
    assert EE.kind_for_event_type("") == "think"
    assert EE.kind_for_event_type("not_a_real_type") == "think"


def test_build_event_fills_v1_minimal_contract():
    ev = EE.build_event("id1", "tool", "ラベル", "詳細", "done")
    for f in EE.V1_FIELDS:
        assert f in ev
    assert ev["type"] == "node"
    assert ev["id"] == "id1" and ev["kind"] == "tool" and ev["label"] == "ラベル"
    assert ev["detail"] == "詳細" and ev["status"] == "done"


def test_build_event_v2_fields_default_to_none():
    ev = EE.build_event("id1", "tool", "ラベル", "詳細", "done")
    for f in ("event_type", "parent_id", "run_id", "agent_run_id", "parent_agent_run_id",
              "task_id", "phase", "seq", "metrics", "evidence_ids"):
        assert ev[f] is None


def test_build_event_carries_all_v2_fields_when_given():
    ev = EE.build_event("t1", "tool", "検索", "検索中", "active", event_type="tool_started",
                        parent_id="agent-1", run_id="run-1", agent_run_id="sub:worker1:1",
                        parent_agent_run_id="main", task_id="task-1", phase="gather", seq=3,
                        metrics={"tool_bytes": 100}, evidence_ids=["ev-1"])
    assert ev["event_type"] == "tool_started"
    assert ev["parent_id"] == "agent-1"
    assert ev["run_id"] == "run-1"
    assert ev["agent_run_id"] == "sub:worker1:1"
    assert ev["parent_agent_run_id"] == "main"
    assert ev["task_id"] == "task-1"
    assert ev["phase"] == "gather"
    assert ev["seq"] == 3
    assert ev["metrics"] == {"tool_bytes": 100}
    assert ev["evidence_ids"] == ["ev-1"]


def test_build_event_rejects_unknown_event_type():
    with pytest.raises(ValueError):
        EE.build_event("id1", "tool", "l", "d", "done", event_type="not_a_real_type")


def test_build_event_rejects_unknown_phase():
    with pytest.raises(ValueError):
        EE.build_event("id1", "tool", "l", "d", "done", phase="not_a_real_phase")


def test_build_event_does_not_auto_derive_kind_from_event_type():
    """kind は呼び出し側が明示する（自動導出はしない・§ build_event docstring）。矛盾する組み合わせも
    そのまま通る（呼び出し側が `kind_for_event_type` を使うかどうかは選択できる）。"""
    ev = EE.build_event("id1", "think", "l", "d", "done", event_type="tool_started")
    assert ev["kind"] == "think"          # event_type なら本来 "tool" だが、明示した kind を尊重する
