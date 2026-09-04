"""chat_turns.TurnBuffer が Execution Event v2 ペイロードを無改修で運べることの単体テスト
（EXT-1 受け入れ条件(c)・`docs/proposals/2026-08-22-拡張設計.md` §11.3①・DB/Neo4j 不要）。

`TurnBuffer` は payload を型を持たない `dict` として append-only に保持するだけ（`chat_turns.py`
モジュール docstring「薄いラッパー」）。本テストは v2 ノード（`parent_id`/`agent_run_id` 等を持つ）が
(a) `replay_from` でそのまま取り出せ、(b) SSE 送出と同じ JSON シリアライズ（`json.dumps(...,
ensure_ascii=False, default=str)`）を経ても全フィールドが失われないことを固定する。
"""
from __future__ import annotations

import json

from sherpa import chat_turns as CT
from sherpa import exec_event as EE


def _v2_node() -> dict:
    return EE.build_event("tool-1", "tool", "検索", "詳細", "done", event_type="tool_started",
                          parent_id="agent-1", run_id="run-1", agent_run_id="sub:worker1:1",
                          parent_agent_run_id="main", task_id="task-1", phase="gather", seq=2,
                          metrics={"tool_bytes": 42}, evidence_ids=["ev-1", "ev-2"])


def test_turn_buffer_replay_returns_v2_payload_unmodified():
    buf = CT.TurnBuffer()
    ev = _v2_node()
    buf.append(ev)
    out = buf.replay_from(0)
    assert len(out) == 1
    assert out[0].payload == ev


def test_turn_buffer_v2_payload_survives_sse_json_round_trip():
    """`iter_sse` が使うのと同じシリアライズ（`chat_turns.py::iter_sse` 内の `gen()` 参照）。"""
    buf = CT.TurnBuffer()
    ev = _v2_node()
    buf.append(ev)
    [rec] = buf.replay_from(0)
    wire = json.dumps(rec.payload, ensure_ascii=False, default=str)
    restored = json.loads(wire)
    assert restored == ev


def test_turn_buffer_mixed_v1_and_v2_nodes_pass_through_regardless_of_type():
    """v1 ノードと v2 ノードが混在しても buffer 側は型を見て特別扱いせず、両方そのまま運ぶ。"""
    buf = CT.TurnBuffer()
    v1_node = {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
              "detail": "内容を把握しました", "status": "done"}
    v2_node = _v2_node()
    buf.append(v1_node)
    buf.append(v2_node)
    out = [e.payload for e in buf.replay_from(0)]
    assert out == [v1_node, v2_node]


def test_turn_buffer_does_not_dedup_repeated_ids_append_only():
    """RV是正⑥（テスト名/実態合わせ）: chat_service 側の dedup（`trace_nodes[ev["id"]] = ev`）は
    呼び出し側の責務であり、`TurnBuffer` 自体は同じ `id` が複数回 append されても最新で上書きせず
    全件そのまま append-only で保持する（`chat_turns.py` モジュール docstring「薄いラッパー」の実体）。
    同一 id を異なる detail で2回 append し、両方が別イベントとして残ることで実際に検証する。
    """
    buf = CT.TurnBuffer()
    first = {"type": "node", "id": "tool-1", "kind": "tool", "label": "検索", "detail": "active", "status": "active"}
    second = {"type": "node", "id": "tool-1", "kind": "tool", "label": "検索", "detail": "done", "status": "done"}
    buf.append(first)
    buf.append(second)
    out = [e.payload for e in buf.replay_from(0)]
    assert len(out) == 2                                                # dedup されず2件とも残る
    assert out == [first, second]


# ===== 先頭イベントの保護 =====
# `chat_service.stream_message` はターンの trace_version をライブ配信中に判定できるよう、ストリーム
# 先頭に軽量なマーカー（`{"type":"trace_meta",...}`）を流す契約にした。長時間実行のターンで
# MAX_BUFFER_EVENTS/MAX_BUFFER_BYTES を超え、cursor=0（会話に戻ったときの再購読・
# resumeRunningTurn）で replay したときに、その先頭マーカーだけが古参として真っ先に間引かれると
# 再入場時に v1 表示へ化けてしまう（trace_version を再判定できない）。`TurnBuffer` 自体は payload の
# 中身を一切解釈しない契約のまま、「先頭1件は位置だけで保護する」という汎用ポリシーで解決したことを
# 固定する（chat_service 固有の意味論には踏み込まない）。

def test_turn_buffer_eviction_protects_first_event_by_position():
    """件数上限（`MAX_BUFFER_EVENTS`）を超えても、先頭イベント（trace_meta 相当）は間引かれない。"""
    buf = CT.TurnBuffer()
    first = {"type": "trace_meta", "trace_version": 2}
    buf.append(first)
    for i in range(CT.MAX_BUFFER_EVENTS + 50):
        buf.append({"type": "node", "id": f"n{i}", "kind": "tool", "label": "検索",
                   "detail": str(i), "status": "done"})
    out = [e.payload for e in buf.replay_from(0)]
    assert out[0] == first                                   # 先頭は最後まで残る
    assert len(out) <= CT.MAX_BUFFER_EVENTS
    assert out[-1]["id"] == f"n{CT.MAX_BUFFER_EVENTS + 49}"   # 直近1件も従来どおり残る


def test_turn_buffer_eviction_protects_first_event_by_bytes():
    """バイト上限（`MAX_BUFFER_BYTES`）超過でも同様に先頭イベントを保護する。"""
    buf = CT.TurnBuffer()
    first = {"type": "trace_meta", "trace_version": 2}
    buf.append(first)
    big_detail = "x" * 2000
    n = (CT.MAX_BUFFER_BYTES // 2000) + 20   # バイト上限を確実に超える件数
    for i in range(n):
        buf.append({"type": "node", "id": f"n{i}", "kind": "tool", "label": "検索",
                   "detail": big_detail, "status": "done"})
    out = [e.payload for e in buf.replay_from(0)]
    assert out[0] == first
    assert out[-1]["id"] == f"n{n - 1}"


def test_turn_buffer_eviction_degenerate_two_events_stops_safely():
    """保護対象（先頭＋直近1件）だけになったら、それ以上は削れずそのまま止まる（無限ループ/
    先頭 or 直近の破壊を起こさない）。"""
    buf = CT.TurnBuffer()
    first = {"type": "trace_meta", "trace_version": 2}
    buf.append(first)
    huge = {"type": "node", "id": "huge", "kind": "tool", "label": "検索",
           "detail": "x" * (CT.MAX_BUFFER_BYTES * 2), "status": "done"}
    buf.append(huge)
    out = [e.payload for e in buf.replay_from(0)]
    assert out == [first, huge]
