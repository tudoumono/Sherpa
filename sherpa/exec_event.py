"""Execution Event v2 のビルダ（設計: docs/proposals/2026-08-22-拡張設計.md §2・EXT-1）。

v1 の最小契約 `{type:"node", id, kind, label, detail, status}` を土台に、階層構造を表す新規フィールド
（`event_type`/`parent_id`/`run_id`/`agent_run_id`/`parent_agent_run_id`/`task_id`/`phase`/`seq`/
`metrics`/`evidence_ids`）を**加算的に**足す。v1 の平坦描画（`stream.js::onNode`・`answer.trace_version`
省略時の経路）は `id`/`kind`/`label`/`detail`/`status` の5つしか読まないため、新フィールドが付いた
イベントが混ざっても無視されるだけで安全（§2.3）。`answer.trace_version=2` のときは
`web/chat/render.js::TraceTreeV2`（EXT-4・拡張設計 §10）が `agent_run_id`/`parent_agent_run_id`
（サブエージェント レーン分け）・`parent_id`（レーン内の親子ネスト）・`metrics`/`event_type`/
`evidence_ids`（担当バッジ・集約・候補/根拠集計）を実際に読んで階層描画する。`agents.py` 系には
依存しない（`agentic_search.py` のローカル `_node`/`_nid` と同じ「循環回避」方針）。

v1 の `_node()` はこの同型ペイロードを独立に作る実装が2つある（`build_event` の5引数互換の範囲は
`providers/base.py::_node(id, kind, label, detail, status)` のみ＝そのまま。`agentic_search.py::_node
(label, detail)` は2引数で id/kind/status を内部生成する別シグネチャのため**互換ではない**＝
将来この呼び出し箇所を置き換えるときは薄いラッパー（label/detail から本関数を呼ぶ変換）が要る。
本モジュールはどちらの `_node()` も呼ばない・書き換えない）。

`_cap_trace_v2` の集約/マーカーノード生成（`_build_reserved_event`）に加え、実イベント発行側でも
既に `build_event` を使っている箇所がある: `providers/base.py` の `evidence_committed`
（`_evidence_committed_node`）とハイブリッド下調べ役の `agent_completed`
（`_sub_agent_completed_node`・`agent_run_id`/`metrics` 付与）、`agentic_search.py` の評価フェーズ
各イベント（`_eval_node`）。ただし `parent_id`（レーン内の親子ネスト）を実際に渡す呼び出しは
まだ無く、これら以外の大半のノード種別は引き続き `_node()` の平坦ペイロードのまま
（残る置き換えは個別スライスが担う）。

`id` の予約名前空間（`RESERVED_ID_PREFIXES`/`BUDGET_LIMIT_REACHED_ID`）: `trace-omitted:`/`trace-subtree:`
プレフィックスと固定 id `trace-budget-limit-reached` は `chat_service._cap_trace_v2` が生成する
集約/マーカーノード専用。`build_event`（通常イベント用）はこれらの id を拒否し、
`_build_reserved_event`（集約/マーカー生成専用の内部関数）だけが受け付ける＝一般イベントが
予約 id を名乗って集約/マーカーと衝突する事故を構造的に防ぐ。
"""
from __future__ import annotations

# Execution Event 種別（§2.4・原文の語彙をそのまま採用したクローズド語彙）。
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

# Depth/Cost/Verification Profile とは別軸の実行フェーズ（§2.2 `phase` フィールド）。
PHASES = frozenset({"gather", "plan", "delegate", "evaluate", "finalize"})

# 描画クラス分岐用の `kind`（§2.2）。既存2値（think/tool）＋v2 新設4値。build_event は検証しない
# （v1 から「未検証の自由文字列」のまま・`kind_for_event_type` の戻り値としてのみ閉じる）。
KINDS = frozenset({"think", "tool", "agent", "evidence", "evaluation", "hook"})

# v1 最小契約のキー（既存フロント3ファイルが読む5つ・欠けると描画が壊れる＝§2.3）。
V1_FIELDS = ("id", "kind", "label", "detail", "status")

_KIND_EXACT = {"evidence_committed": "evidence", "replan_requested": "evaluation"}
_KIND_PREFIXES = (
    ("agent_", "agent"), ("tool_", "tool"), ("candidate_", "evidence"),
    ("evaluation_", "evaluation"), ("hook_", "hook"),
)

# 予約名前空間: chat_service._cap_trace_v2 の集約/マーカーノード専用の id。通常イベント
# （build_event）はこれらを名乗れない＝集約/マーカーとの衝突を構造的に防ぐ。
BUDGET_LIMIT_REACHED_ID = "trace-budget-limit-reached"
RESERVED_ID_PREFIXES = ("trace-omitted:", "trace-subtree:")


def _is_reserved_id(id: str) -> bool:
    return id == BUDGET_LIMIT_REACHED_ID or any(id.startswith(p) for p in RESERVED_ID_PREFIXES)


def kind_for_event_type(event_type: str | None) -> str:
    """event_type → 描画用 kind（§2.4 の対応表）。未分類/None は think（既存の暗黙既定を踏襲）。"""
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
    """`build_event`/`_build_reserved_event` 共通の dict 組み立て。id の予約可否検証は
    呼び出し元（両関数）の責務＝ここでは event_type/phase のクローズド語彙検証だけを行う。
    """
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
    """v2 ノードイベントを組み立てる（§2.2）。先頭5引数は `providers/base.py::_node(id, kind, label,
    detail, status)` と同じ位置・順序＝**その**呼び出し箇所の置き換えは新規キーワード引数を足すだけで
    済む（`agentic_search.py::_node(label, detail)` は2引数の別シグネチャで対象外・モジュール docstring
    参照）。

    `id` が予約名前空間（`RESERVED_ID_PREFIXES`/`BUDGET_LIMIT_REACHED_ID`）に該当する場合は
    `ValueError`（通常イベントは集約/マーカー専用の id を名乗れない）。`event_type`/`phase` は
    クローズド語彙（`EVENT_TYPES`/`PHASES`）で、範囲外の値も `ValueError`（呼び出し側はこの2つを
    固定文字列リテラルで渡す前提＝タイプミスは実装時に検出したい）。`kind` は v1 から未検証の
    自由文字列のまま（呼び出し側が `kind_for_event_type(event_type)` を明示的に使うか、独自の kind を
    渡すかを選べる＝自動導出はしない）。
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
    """集約/マーカーノード専用のビルダ（`build_event` の逆＝予約名前空間の id だけを受け付ける）。

    呼び出し元は `chat_service._cap_trace_v2` の集約/マーカー生成コードのみ（`lens_service._run_capped`
    を `chat_service` が直接呼ぶのと同じ「同一機能を分担する2モジュール間の意図的な private 共用」・
    `chat_service._known_terms` docstring 参照）。id が予約名前空間でなければ `ValueError`
    （呼び出し側の実装ミス＝ハッシュ化を忘れた等を実装時に検出するため）。
    """
    if not _is_reserved_id(id):
        raise ValueError(f"id {id!r} は予約名前空間ではない（_build_reserved_event は集約/マーカー専用）")
    return _build(id, kind, label, detail, status, event_type=event_type, parent_id=parent_id,
                 run_id=run_id, agent_run_id=agent_run_id, parent_agent_run_id=parent_agent_run_id,
                 task_id=task_id, phase=phase, seq=seq, metrics=metrics, evidence_ids=evidence_ids)
