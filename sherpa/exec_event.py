"""Execution Event v2 のビルダ。

v1 の最小契約 `{type:"node", id, kind, label, detail, status}` を土台に、階層構造を表すフィールド
（`event_type`/`parent_id`/`run_id`/`agent_run_id`/`parent_agent_run_id`/`task_id`/`phase`/`seq`/`metrics`/
`evidence_ids`）を加算的に足す。v1 の平坦描画は先頭5つしか読まないため、新フィールドは無視されても安全。
`agents.py` 系には依存しない。

`id` の予約名前空間（`RESERVED_ID_PREFIXES`/`BUDGET_LIMIT_REACHED_ID`）は `chat_service._cap_trace_v2` の集約/マーカー
ノード専用。`build_event` はこれらの id を拒否し、`_build_reserved_event` だけが受け付ける。
"""
from __future__ import annotations

# Execution Event 種別（クローズド語彙）
EVENT_TYPES = frozenset({
    "run_started", "plan_created", "task_created", "task_delegated",
    "agent_started", "agent_completed", "agent_failed", "agent_cancelled",
    "tool_started", "tool_completed", "tool_failed",
    "candidate_discovered", "candidate_verified", "candidate_rejected",
    "evidence_committed",
    "evaluation_completed", "replan_requested",
    "additional_task_created",
    "budget_updated", "budget_limit_reached",
    "hook_started", "hook_completed", "hook_failed",
    "finalization_started",
    "run_completed", "run_stopped",
})

# 実行フェーズ
PHASES = frozenset({"gather", "plan", "delegate", "evaluate", "finalize"})

# 描画クラス分岐用の `kind`（`build_event` は検証せず、`kind_for_event_type` の戻り値としてのみ閉じる）
KINDS = frozenset({"think", "tool", "agent", "evidence", "evaluation", "hook"})

_KIND_EXACT = {"evidence_committed": "evidence", "replan_requested": "evaluation"}
_KIND_PREFIXES = (
    ("agent_", "agent"), ("tool_", "tool"), ("candidate_", "evidence"),
    ("evaluation_", "evaluation"), ("hook_", "hook"),
)

# 予約名前空間: `_cap_trace_v2` の集約/マーカーノード専用の id（通常イベントは名乗れない）
BUDGET_LIMIT_REACHED_ID = "trace-budget-limit-reached"
RESERVED_ID_PREFIXES = ("trace-omitted:", "trace-subtree:")


def _is_reserved_id(id: str) -> bool:
    return id == BUDGET_LIMIT_REACHED_ID or any(id.startswith(p) for p in RESERVED_ID_PREFIXES)


def kind_for_event_type(event_type: str | None) -> str:
    """event_type → 描画用 kind。未分類/None は think。"""
    if event_type in _KIND_EXACT:
        return _KIND_EXACT[event_type]
    for prefix, kind in _KIND_PREFIXES:
        if event_type and event_type.startswith(prefix):
            return kind
    return "think"


def _build(id: str, kind: str, label: str, detail: str, status: str, *,
           event_type: str | None, parent_id: str | None, run_id: str | None,
           agent_run_id: str | None, parent_agent_run_id: str | None, task_id: str | None,
           phase: str | None, seq: int | None, metrics: dict | None,
           evidence_ids: list[str] | None) -> dict:
    """`build_event`/`_build_reserved_event` 共通の dict 組み立て（event_type/phase のクローズド語彙検証を行う）。"""
    if event_type is not None and event_type not in EVENT_TYPES:
        raise ValueError(f"unknown Execution Event type: {event_type!r}")
    if phase is not None and phase not in PHASES:
        raise ValueError(f"unknown Execution Event phase: {phase!r}")
    return {
        "type": "node", "id": id, "kind": kind, "label": label, "detail": detail, "status": status,
        "event_type": event_type, "parent_id": parent_id, "run_id": run_id,
        "agent_run_id": agent_run_id, "parent_agent_run_id": parent_agent_run_id,
        "task_id": task_id, "phase": phase, "seq": seq,
        "metrics": metrics, "evidence_ids": evidence_ids,
    }


def build_event(id: str, kind: str, label: str, detail: str, status: str, *,
                 event_type: str | None = None, parent_id: str | None = None,
                 run_id: str | None = None, agent_run_id: str | None = None,
                 parent_agent_run_id: str | None = None, task_id: str | None = None,
                 phase: str | None = None, seq: int | None = None,
                 metrics: dict | None = None, evidence_ids: list[str] | None = None) -> dict:
    """v2 ノードイベントを組み立てる。先頭5引数は `providers/base.py::_node` と同じ位置・順序。

    `id` が予約名前空間なら `ValueError`。`event_type`/`phase` は `EVENT_TYPES`/`PHASES` の範囲外で `ValueError`。
    `kind` は検証しない自由文字列（自動導出しない）。
    """
    if _is_reserved_id(id):
        raise ValueError(f"id {id!r} は予約名前空間（集約/マーカー専用）のため通常イベントには使えない")
    return _build(id, kind, label, detail, status, event_type=event_type, parent_id=parent_id,
                 run_id=run_id, agent_run_id=agent_run_id, parent_agent_run_id=parent_agent_run_id,
                 task_id=task_id, phase=phase, seq=seq, metrics=metrics, evidence_ids=evidence_ids)


def _build_reserved_event(id: str, kind: str, label: str, detail: str, status: str, *,
                          event_type: str | None = None, parent_id: str | None = None,
                          run_id: str | None = None, agent_run_id: str | None = None,
                          parent_agent_run_id: str | None = None, task_id: str | None = None,
                          phase: str | None = None, seq: int | None = None,
                          metrics: dict | None = None, evidence_ids: list[str] | None = None) -> dict:
    """集約/マーカーノード専用のビルダ（予約名前空間の id だけを受け付け、そうでなければ `ValueError`）。
    呼び出し元は `chat_service._cap_trace_v2` のみ。
    """
    if not _is_reserved_id(id):
        raise ValueError(f"id {id!r} は予約名前空間ではない（_build_reserved_event は集約/マーカー専用）")
    return _build(id, kind, label, detail, status, event_type=event_type, parent_id=parent_id,
                 run_id=run_id, agent_run_id=agent_run_id, parent_agent_run_id=parent_agent_run_id,
                 task_id=task_id, phase=phase, seq=seq, metrics=metrics, evidence_ids=evidence_ids)
