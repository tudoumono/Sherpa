from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from pathlib import Path

from .database import correlate_real_ai_turn_database


_FAILURE_TEXT = (
    "接続できませんでした",
    "利用できません",
    "応答を返す前に終了しました",
    "タイムアウトしました",
    "未応答のため決定的回答に切替",
    "設定を確認してください",
)

_DEGRADATION_PATTERN = re.compile(
    "|".join(
        re.escape(text)
        for text in (
            "未応答",
            "応答なし",
            "応答がありません",
            "決定的回答",
            "検索方法を切替",
            "別の方法で調べ直",
            "縮退",
            "フォールバック",
            "fallback",
            "no response",
            "empty response",
            "接続できませんでした",
            "タイムアウトしました",
            "応答を返す前に終了",
            "利用できないAI",
            "設定を確認してください",
        )
    ),
    re.IGNORECASE,
)

_REAL_PROVIDERS = {"bedrock", "codex", "gemini", "ollama", "openai"}


def final_nodes(events: list[dict]) -> list[dict]:
    nodes: OrderedDict[str, dict] = OrderedDict()
    for event in events:
        if event.get("type") == "node" and event.get("id"):
            nodes[event["id"]] = event
    return list(nodes.values())


def answer_event(events: list[dict]) -> dict:
    answers = [(index, event) for index, event in enumerate(events) if event.get("type") == "answer"]
    assert len(answers) == 1, f"expected exactly one final answer event, got {len(answers)}"
    assert not any(event.get("type") == "error" for event in events), "SSE contains an error event"
    answer_index, answer = answers[0]
    assert answer_index == len(events) - 1, "the unique final answer was not the terminal SSE event"
    assert not any(event.get("type") == "stopped" for event in events), "SSE contains both stopped and answer terminals"
    return answer


def assert_sse_event_sequence(events: list[dict], *, expected_turn_id: str, require_v2: bool = False) -> dict:
    """Validate raw event identity/order before any node-id folding is allowed."""
    assert events, "SSE replay is empty"
    assert expected_turn_id, "expected turn id is empty"
    assert all(isinstance(event, dict) for event in events), "SSE replay contains a non-object event"
    assert not any(event.get("type") == "_result" for event in events), "internal provider result leaked into public SSE"

    node_events = [event for event in events if event.get("type") == "node"]
    assert node_events, "SSE replay contains no node events"
    transition_keys: list[tuple[str, str]] = []
    for event in node_events:
        node_id = str(event.get("id") or "")
        status = str(event.get("status") or "")
        assert node_id, f"SSE node has no id: {event}"
        assert status in {"active", "done", "failed", "cancelled"}, f"SSE node has invalid status: {event}"
        transition_keys.append((node_id, status))
    assert len(transition_keys) == len(set(transition_keys)), (
        "SSE contains a duplicated node/status transition; replay event identity is not unique"
    )

    v2_fields = ("event_type", "run_id", "seq")
    has_v2_event = any(any(event.get(field) is not None for field in v2_fields) for event in node_events)
    if require_v2:
        assert has_v2_event, "trace_version=2 answer was produced from SSE nodes without v2 event identity fields"
    v2_sequences: list[int] = []
    if has_v2_event:
        for event in node_events:
            assert event.get("event_type"), f"v2 SSE node has no event_type: {event}"
            assert str(event.get("run_id") or "") == expected_turn_id, (
                f"v2 SSE node run_id does not identify turn {expected_turn_id}: {event}"
            )
            seq = event.get("seq")
            assert isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1, f"v2 SSE node has invalid seq: {event}"
            v2_sequences.append(seq)
        assert len(v2_sequences) == len(set(v2_sequences)), "v2 SSE event seq values are not unique"
        assert v2_sequences == sorted(v2_sequences), "v2 SSE event seq values are not monotonically increasing"

    return {
        "event_count": len(events),
        "node_event_count": len(node_events),
        "node_transition_count": len(transition_keys),
        "unique_node_transitions": True,
        "v2_payload": has_v2_event,
        "v2_required": require_v2,
        "v2_sequence_count": len(v2_sequences),
        "v2_sequence_unique": True if has_v2_event else None,
        "v2_sequence_monotonic": True if has_v2_event else None,
    }


def assert_sse_cursor_replay(full_events: list[dict], replayed_events: list[dict], *, cursor: int) -> dict:
    """Prove the server's hidden transport sequence through its public cursor contract."""
    assert isinstance(cursor, int) and not isinstance(cursor, bool) and 0 < cursor < len(full_events), (
        f"cursor must select a non-empty strict suffix: cursor={cursor}, events={len(full_events)}"
    )
    expected = full_events[cursor:]
    assert replayed_events == expected, {
        "reason": "cursor replay duplicated, omitted, reordered, or changed an SSE event",
        "cursor": cursor,
        "expected_count": len(expected),
        "actual_count": len(replayed_events),
    }
    full_hashes = [hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode()).hexdigest() for event in full_events]
    replay_hashes = [
        hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode()).hexdigest() for event in replayed_events
    ]
    assert replay_hashes == full_hashes[cursor:]
    return {
        "cursor": cursor,
        "full_event_count": len(full_events),
        "replayed_event_count": len(replayed_events),
        "exact_suffix": True,
        "first_replayed_event_sha256": replay_hashes[0],
        "last_replayed_event_sha256": replay_hashes[-1],
    }


def assert_no_degraded_trace(source: str, nodes: list[dict]) -> None:
    """Reject fallback/degradation markers in one independently observed trace."""
    findings: list[dict[str, str | int]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            findings.append({"index": index, "field": "node", "text": repr(node)[:300]})
            continue
        for field in ("label", "detail"):
            value = str(node.get(field) or "")
            if _DEGRADATION_PATTERN.search(value):
                findings.append(
                    {
                        "index": index,
                        "id": str(node.get("id") or ""),
                        "field": field,
                        "text": value[:300],
                    }
                )
    assert not findings, f"{source} trace contains provider fallback/planner degradation markers: {findings}"


def _assert_provider_completion(provider: str, nodes: list[dict]) -> dict:
    """Require the provider's own success node, not only usage or a terminal answer envelope."""
    terminal = [node for node in nodes if node.get("status") == "done"]
    if provider == "codex":
        matches = [
            node for node in terminal if str(node.get("id") or "") == "codex" and "回答をまとめました" in str(node.get("detail") or "")
        ]
        expected = "Codex terminal node with an actual agent answer"
        completion_mode = "codex-terminal-node"
    else:
        matches = [
            node for node in terminal if str(node.get("id") or "") == "brain" and str(node.get("detail") or "").strip() == "回答しました"
        ]
        completion_mode = "provider-terminal-brain-node"
        if not matches:
            # The direct agentic path deliberately emits its provider-produced answer and
            # `_result` immediately after the final tool node; unlike plain/hybrid paths it
            # has no synthetic `brain` completion node (providers/base.py::_agentic_run).
            # The caller independently requires exact answer_delta reconstruction, non-zero
            # usage for this provider, a persisted DB/audit match, and a fallback-free trace.
            # Requiring a completed real tool here distinguishes that path from an ungrounded
            # terminal answer envelope while preserving the product's actual event contract.
            matches = [node for node in terminal if str(node.get("kind") or "") == "tool"]
            completion_mode = "provider-agentic-tool-and-usage"
        expected = (
            f"{provider} terminal brain node, or direct-agentic terminal tool backed by exact answer deltas and non-zero provider usage"
        )
    assert matches, f"real {provider} usage existed but no {expected} was observed"
    return {
        "node_id": str(matches[-1].get("id") or ""),
        "label": str(matches[-1].get("label") or ""),
        "detail": str(matches[-1].get("detail") or ""),
        "mode": completion_mode,
    }


def _delta_text(event: dict) -> str | None:
    if event.get("type") != "answer_delta":
        return None
    if isinstance(event.get("text"), str):
        return event["text"]
    delta = event.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        for key in ("text", "content"):
            if isinstance(delta.get(key), str):
                return delta[key]
    data = event.get("data")
    if isinstance(data, dict):
        for key in ("text", "content"):
            if isinstance(data.get(key), str):
                return data[key]
    raise AssertionError("answer_delta event has no supported text field")


def assert_answer_delta_correlation(events: list[dict], assistant_message: dict) -> dict:
    chunks = [_delta_text(event) for event in events if event.get("type") == "answer_delta"]
    assert chunks, "SSE contained no answer_delta chunks"
    reconstructed = "".join(chunk for chunk in chunks if chunk is not None).replace("\r\n", "\n")
    answer = assistant_message.get("answer") or {}
    final_headline = str(answer.get("headline") or "").replace("\r\n", "\n")
    stored_content = str(assistant_message.get("content") or "").replace("\r\n", "\n")
    assert final_headline, "final answer has no headline for delta correlation"
    assert reconstructed == final_headline, "ordered answer_delta reconstruction differs from the final answer headline"
    assert not stored_content or stored_content == final_headline, "conversation content differs from its structured final answer headline"
    return {
        "chunk_count": len(chunks),
        "reconstructed_chars": len(reconstructed),
        "final_chars": len(final_headline),
        "reconstructed_sha256": hashlib.sha256(reconstructed.encode()).hexdigest(),
        "final_sha256": hashlib.sha256(final_headline.encode()).hexdigest(),
        "exact_match": True,
    }


def assert_node_status_lifecycle(events: list[dict]) -> dict:
    histories: OrderedDict[str, list[dict]] = OrderedDict()
    for index, event in enumerate(events):
        if event.get("type") != "node" or not event.get("id"):
            continue
        histories.setdefault(str(event["id"]), []).append(
            {
                "event_index": index,
                "status": str(event.get("status") or ""),
                "kind": str(event.get("kind") or ""),
                "event_type": event.get("event_type"),
                "seq": event.get("seq"),
            }
        )
    assert histories, "SSE contained no node lifecycle events"
    transitioned: list[str] = []
    for node_id, rows in histories.items():
        statuses = [row["status"] for row in rows]
        assert statuses[-1] == "done", f"node {node_id} did not reach done: {statuses}"
        assert statuses.count("done") == 1, f"node {node_id} emitted more than one terminal done event: {statuses}"
        assert all(status == "active" for status in statuses[:-1]), f"node {node_id} has an invalid status transition: {statuses}"
        if statuses[0] == "active":
            transitioned.append(node_id)
    assert transitioned, "no SSE node demonstrated an active-to-done status transition"
    return {
        "node_count": len(histories),
        "active_to_done_count": len(transitioned),
        "terminal_done_count": sum(rows[-1]["status"] == "done" for rows in histories.values()),
        "nodes": [{"id": node_id, "timeline": rows} for node_id, rows in histories.items()],
    }


def assert_persisted_trace_after_cap(events: list[dict], assistant_message: dict) -> dict:
    streamed = final_nodes(events)
    stored = assistant_message.get("trace") or []
    assert streamed and stored, "streamed or persisted execution trace is empty"
    streamed_ids = [str(node.get("id")) for node in streamed]
    stored_ids = [str(node.get("id")) for node in stored]
    trace_version = (assistant_message.get("answer") or {}).get("trace_version")
    aggregate_prefixes = ("trace-omitted:", "trace-subtree:")
    aggregate_nodes = [node for node in stored if str(node.get("id") or "").startswith(aggregate_prefixes)]
    budget_markers = [node for node in stored if node.get("id") == "trace-budget-limit-reached"]
    summary_nodes = [node for node in stored if node.get("id") == "trace-omitted"]

    assert len(streamed_ids) == len(set(streamed_ids)), "folded SSE final nodes contain duplicate ids"
    assert len(stored_ids) == len(set(stored_ids)), "persisted trace contains duplicate ids"
    assert all(isinstance(node, dict) for node in stored), "persisted trace contains a non-object node"

    is_v2 = trace_version == 2
    streamed_id_set = set(streamed_ids)
    normalized_streamed: OrderedDict[str, dict] = OrderedDict()
    for node in streamed:
        normalized = {**node, "detail": str(node.get("detail") or "")[:200]}
        if is_v2:
            parent_id = normalized.get("parent_id")
            normalized["parent_id"] = parent_id if parent_id in streamed_id_set else None
        normalized_streamed[str(node["id"])] = normalized

    retained_nodes = [
        node
        for node in stored
        if not str(node.get("id") or "").startswith(aggregate_prefixes)
        and node.get("id") != "trace-budget-limit-reached"
        and node.get("id") != "trace-omitted"
    ]
    retained_ids = [str(node.get("id") or "") for node in retained_nodes]
    assert all(node_id in normalized_streamed for node_id in retained_ids), "persisted trace contains an unknown non-aggregate node"
    for node in retained_nodes:
        node_id = str(node["id"])
        assert node == normalized_streamed[node_id], {
            "reason": "persisted retained node differs from the normalized final SSE node",
            "node_id": node_id,
            "streamed_normalized": normalized_streamed[node_id],
            "persisted": node,
        }
    retained_positions = [streamed_ids.index(node_id) for node_id in retained_ids]
    assert retained_positions == sorted(retained_positions), "persisted retained nodes changed SSE node order"

    if is_v2:
        assert not summary_nodes, "v2 trace used the legacy trace-omitted marker"
        assert len(budget_markers) <= 1, "v2 persisted trace contains duplicate budget markers"
        assert not (budget_markers and aggregate_nodes), (
            "v2 trace mixes nested aggregates with a budget marker, so exact source-node representation cannot be proven"
        )
        represented_ids = set(retained_ids)
        aggregate_members: dict[str, set[str]] = {}
        group_aggregates = [node for node in aggregate_nodes if str(node.get("id") or "").startswith("trace-omitted:")]
        subtree_aggregates = [node for node in aggregate_nodes if str(node.get("id") or "").startswith("trace-subtree:")]
        assert not (group_aggregates and subtree_aggregates), (
            "v2 trace mixes soft-group and hard-subtree aggregates, so exact source-node representation cannot be proven"
        )

        for node in aggregate_nodes:
            metrics = node.get("metrics") or {}
            omitted = int(metrics.get("omitted_count") or 0)
            assert omitted > 0, "v2 aggregate trace node omitted its represented count"
            node_id = str(node.get("id") or "")
            assert node.get("type") == "node" and node.get("status") == "done", f"invalid v2 aggregate node: {node}"
            if node_id.startswith("trace-omitted:"):
                group_key = (node.get("parent_id"), node.get("kind") or "think", node.get("agent_run_id"))
                canonical = json.dumps(list(group_key), ensure_ascii=False)
                expected_id = f"trace-omitted:{hashlib.sha1(canonical.encode()).hexdigest()}"
                assert node_id == expected_id, "v2 soft aggregate id does not match its normalized grouping key"
                members = {
                    streamed_id
                    for streamed_id, candidate in normalized_streamed.items()
                    if streamed_id not in retained_ids
                    and (candidate.get("parent_id"), candidate.get("kind") or "think", candidate.get("agent_run_id")) == group_key
                }
            else:
                root_matches = [
                    streamed_id
                    for streamed_id in streamed_ids
                    if f"trace-subtree:{hashlib.sha1(streamed_id.encode()).hexdigest()}" == node_id
                ]
                assert len(root_matches) == 1, f"v2 subtree aggregate does not identify exactly one streamed root: {node_id}"
                root_id = root_matches[0]
                members = {root_id}
                changed = True
                while changed:
                    changed = False
                    for streamed_id, candidate in normalized_streamed.items():
                        if streamed_id not in members and candidate.get("parent_id") in members:
                            members.add(streamed_id)
                            changed = True
                members -= set(retained_ids)
            assert len(members) == omitted, (
                f"v2 aggregate {node_id} claims {omitted} nodes but exactly maps to {len(members)} streamed nodes"
            )
            evidence_ids = sorted(
                {evidence_id for member_id in members for evidence_id in (normalized_streamed[member_id].get("evidence_ids") or [])}
            )
            capped_evidence = evidence_ids[:20]
            expected_metrics = {"omitted_count": len(members)}
            if len(evidence_ids) > len(capped_evidence):
                expected_metrics["omitted_evidence_count"] = len(evidence_ids) - len(capped_evidence)
            if node_id.startswith("trace-omitted:"):
                expected_aggregate = {
                    "type": "node",
                    "id": node_id,
                    "kind": group_key[1],
                    "label": "（省略）",
                    "detail": f"…{group_key[1]} 系のイベントを {len(members)} 件省略",
                    "status": "done",
                    "event_type": None,
                    "parent_id": group_key[0],
                    "run_id": None,
                    "agent_run_id": group_key[2],
                    "parent_agent_run_id": None,
                    "task_id": None,
                    "phase": None,
                    "seq": None,
                    "metrics": expected_metrics,
                    "evidence_ids": capped_evidence or None,
                }
            else:
                expected_aggregate = {
                    "type": "node",
                    "id": node_id,
                    "kind": "think",
                    "label": "（省略）",
                    "detail": f"…古いサブツリーを1件（{len(members)}件のイベント）省略",
                    "status": "done",
                    "event_type": None,
                    "parent_id": None,
                    "run_id": None,
                    "agent_run_id": None,
                    "parent_agent_run_id": None,
                    "task_id": None,
                    "phase": None,
                    "seq": None,
                    "metrics": expected_metrics,
                    "evidence_ids": capped_evidence or None,
                }
            assert node == expected_aggregate, {
                "reason": "v2 aggregate payload differs from its exact represented source set",
                "expected": expected_aggregate,
                "persisted": node,
            }
            overlap = represented_ids & members
            assert not overlap, f"v2 source nodes are represented more than once: {sorted(overlap)}"
            represented_ids.update(members)
            aggregate_members[node_id] = members

        if budget_markers:
            marker = budget_markers[0]
            assert stored[0] == marker, "v2 budget marker is not the first persisted trace node"
            assert marker.get("type") == "node" and marker.get("status") == "done"
            assert marker.get("event_type") == "budget_limit_reached"
            omitted_ids = set(streamed_ids) - represented_ids
            omitted = int((marker.get("metrics") or {}).get("omitted_count") or 0)
            assert omitted == len(omitted_ids), (
                f"v2 budget marker claims {omitted} nodes but exactly {len(omitted_ids)} final SSE nodes are absent"
            )
            detail = str(marker.get("detail") or "")
            total_match = re.search(r"元の合計\s*(\d+)\s*件", detail)
            assert total_match and int(total_match.group(1)) == len(streamed), (
                "v2 budget marker does not record the exact original final-node count"
            )
            represented_ids.update(omitted_ids)
            expected_marker = {
                "type": "node",
                "id": "trace-budget-limit-reached",
                "kind": "think",
                "label": "（上限に到達）",
                "detail": f"…イベントが多すぎるため {omitted} 件を切り詰めました（元の合計 {len(streamed)} 件）",
                "status": "done",
                "event_type": "budget_limit_reached",
                "parent_id": None,
                "run_id": None,
                "agent_run_id": None,
                "parent_agent_run_id": None,
                "task_id": None,
                "phase": None,
                "seq": None,
                "metrics": {"omitted_count": omitted},
                "evidence_ids": None,
            }
            assert marker == expected_marker, "v2 budget marker payload differs from the exact omission accounting"
            aggregate_members[str(marker["id"])] = omitted_ids

        assert represented_ids == set(streamed_ids), {
            "reason": "v2 persisted trace does not represent every final SSE node exactly once",
            "missing": sorted(set(streamed_ids) - represented_ids),
            "unknown": sorted(represented_ids - set(streamed_ids)),
        }
        representation_positions: list[int] = []
        for node in stored:
            node_id = str(node["id"])
            if node_id == "trace-budget-limit-reached":
                representation_positions.append(-1)
            elif node_id in normalized_streamed:
                representation_positions.append(streamed_ids.index(node_id))
            else:
                representation_positions.append(min(streamed_ids.index(member_id) for member_id in aggregate_members[node_id]))
        assert representation_positions == sorted(representation_positions), (
            "v2 persisted trace changed the source-event representation order"
        )
        stored_set = set(stored_ids)
        assert all(node.get("parent_id") in {None, ""} or node.get("parent_id") in stored_set for node in stored), (
            "v2 persisted trace contains an orphan parent reference"
        )
        cap_mode = "aggregate" if aggregate_nodes else "budget" if budget_markers else "none"
    else:
        assert not aggregate_nodes and not budget_markers, "v1 persisted trace contains a v2 aggregate marker"
        if summary_nodes:
            assert len(summary_nodes) == 1 and stored[0].get("id") == "trace-omitted"
            assert retained_ids == streamed_ids[-len(retained_ids) :]
            detail = str(summary_nodes[0].get("detail") or "")
            match = re.search(r"(\d+)\s*件", detail)
            assert match and int(match.group(1)) + len(retained_ids) == len(streamed), (
                "v1 trace omission marker does not account for streamed nodes"
            )
            expected_omitted = len(streamed) - len(retained_ids)
            assert summary_nodes[0] == {
                "type": "node",
                "id": "trace-omitted",
                "kind": "think",
                "status": "done",
                "label": "（省略）",
                "detail": f"…前半 {expected_omitted} 件省略",
            }, "v1 trace omission marker payload differs from its exact source-node count"
            cap_mode = "legacy-tail"
        else:
            assert stored == list(normalized_streamed.values()), "uncapped v1 persisted trace differs from normalized SSE nodes"
            cap_mode = "none"
    assert all(node.get("status") == "done" for node in stored), "persisted trace contains a nonterminal node"
    return {
        "trace_version": 2 if trace_version == 2 else 1,
        "streamed_final_count": len(streamed),
        "persisted_count": len(stored),
        "cap_mode": cap_mode,
        "cap_applied": cap_mode != "none",
        "aggregate_count": len(aggregate_nodes),
        "budget_marker_count": len(budget_markers),
        "legacy_summary_count": len(summary_nodes),
        "retained_node_count": len(retained_nodes),
        "exact_retained_node_match": True,
        "exact_source_representation": True,
    }


def assert_real_ai_result(
    settings: dict,
    events: list[dict],
    assistant_message: dict,
    *,
    require_tool: bool,
    evidence,
    turn_id: str,
    conversation_id: int,
    database_url: str,
    checkpoint: dict,
    operation: str = "chat",
) -> dict:
    agent = str(settings.get("agent") or settings.get("construct_id") or "")
    assert agent and "heuristic" not in agent.lower(), f"positive chat used a non-AI heuristic configuration: {agent!r}"
    answer = assistant_message.get("answer") or {}
    headline = str(answer.get("headline") or assistant_message.get("content") or "")
    assert headline.strip(), "assistant answer is empty"
    assert not any(text in headline for text in _FAILURE_TEXT), f"provider fallback/error was rendered as success: {headline[:300]}"
    assert not _DEGRADATION_PATTERN.search(headline), f"provider degradation was rendered as success: {headline[:300]}"
    assert not answer.get("busy"), "provider busy response was rendered as success"
    usage = answer.get("usage")
    assert isinstance(usage, dict), "real AI answer has no provider usage evidence"
    provider = str(usage.get("provider") or "").strip().lower()
    assert provider and provider != "heuristic", f"invalid usage provider: {provider!r}"
    assert provider in _REAL_PROVIDERS, f"unsupported real provider completion contract: {provider!r}"
    normalized_agent = agent.strip().lower()
    if normalized_agent in _REAL_PROVIDERS:
        assert provider == normalized_agent, f"configured provider {normalized_agent!r} differs from persisted usage provider {provider!r}"
    token_total = sum(int(usage.get(key) or 0) for key in ("input_tokens", "output_tokens"))
    assert token_total > 0, f"real AI usage is zero: {usage}"
    evidence.record_provider_correlation(
        turn_id=turn_id,
        provider=provider,
        model=usage.get("model"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        operation=operation,
        configured_agent=agent,
    )
    event_sequence = assert_sse_event_sequence(
        events,
        expected_turn_id=turn_id,
        require_v2=answer.get("trace_version") == 2,
    )
    terminal_answer = answer_event(events)
    assert int(terminal_answer.get("conversation_id") or 0) == int(conversation_id), (
        "terminal SSE answer identifies a different conversation"
    )
    sse_message = terminal_answer.get("message")
    assert isinstance(sse_message, dict), "terminal SSE answer has no assistant message envelope"
    assert int(sse_message.get("id") or 0) == int(assistant_message["id"]), (
        "terminal SSE answer message id differs from the conversation API"
    )
    for key in ("id", "role", "content", "lens", "route", "trace", "answer", "created_at"):
        assert sse_message.get(key) == assistant_message.get(key), (
            f"terminal SSE assistant envelope field {key!r} differs from the conversation API"
        )
    assert sse_message.get("role") == "assistant", "terminal SSE message is not an assistant message"
    assert str(sse_message.get("content") or "") == headline, "terminal SSE assistant content differs from the final answer headline"
    delta = assert_answer_delta_correlation(events, assistant_message)
    nodes = final_nodes(events)
    assert nodes, "SSE contained no structured execution nodes"
    assert all(node.get("status") == "done" for node in nodes), f"unfinished execution nodes: {nodes}"
    sse_node_events = [event for event in events if event.get("type") == "node"]
    assert_no_degraded_trace("raw SSE", sse_node_events)
    api_trace = assistant_message.get("trace") or []
    assert isinstance(api_trace, list) and api_trace, "conversation API assistant message has no execution trace"
    assert_no_degraded_trace("conversation API", api_trace)
    provider_completion = _assert_provider_completion(provider, nodes)
    if require_tool:
        assert any(node.get("kind") == "tool" for node in nodes), "knowledge chat executed no real tool node"
    database_correlation = correlate_real_ai_turn_database(
        database_url,
        conversation_id=conversation_id,
        assistant_message_id=int(assistant_message["id"]),
        checkpoint=checkpoint,
        reported_usage=usage,
        reported_trace=api_trace,
        reported_message=assistant_message,
    )
    database_trace = database_correlation.pop("_stored_trace")
    assert_no_degraded_trace("Postgres", database_trace)
    evidence.record_database_correlation(
        conversation_id=conversation_id,
        turn_id=turn_id,
        source="correlate_real_ai_turn_database",
        assistant_message_id=database_correlation["assistant_message_id"],
        audit_id=database_correlation["audit_id"],
    )
    log_correlation = _assert_real_ai_service_log_correlation(
        checkpoint,
        turn_id=turn_id,
        timeout_seconds=5,
    )
    turn_digest = hashlib.sha256(turn_id.encode()).hexdigest()[:12]
    evidence.write_json(
        f"state/real-ai-turn-{turn_digest}-correlation.json",
        {
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "operation": operation,
            "sse_terminal_answer": True,
            "answer_delta_exact_match": True,
            "sse_event_sequence": event_sequence,
            "terminal_answer_conversation_id": int(terminal_answer["conversation_id"]),
            "terminal_answer_message_id": int(sse_message["id"]),
            "database": database_correlation,
            "application_log": log_correlation,
            "provider_completion": provider_completion,
            "provider_invocation_completion_basis": [
                "terminal SSE answer",
                "persisted non-zero provider usage",
                "provider-specific terminal execution node",
                "fallback-free SSE/API/Postgres traces",
                "matching chat.turn audit row",
                "successful turn start and SSE stream access-log records",
            ],
        },
    )
    return {
        "provider": provider,
        "model": str(usage.get("model") or ""),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "answer_delta_count": delta["chunk_count"],
        "answer_sha256": delta["final_sha256"],
        "node_count": len(nodes),
        "assistant_message_id": database_correlation["assistant_message_id"],
        "audit_id": database_correlation["audit_id"],
        "service_log_sha256": log_correlation["observed_bytes_sha256"],
        "provider_completion_node": provider_completion["node_id"],
        "sse_event_count": event_sequence["event_count"],
    }


def _assert_real_ai_service_log_correlation(
    checkpoint: dict,
    *,
    turn_id: str,
    timeout_seconds: float,
) -> dict:
    service_root_value = checkpoint.get("service_root")
    offsets = checkpoint.get("service_log_offsets")
    assert isinstance(service_root_value, str) and service_root_value, "real AI checkpoint has no runner service-log directory"
    assert isinstance(offsets, dict) and offsets, "real AI checkpoint has no log offsets"
    service_root = Path(service_root_value)
    assert service_root.is_dir(), f"runner service-log directory disappeared: {service_root}"
    post_pattern = re.compile(r'"POST /chat/turns HTTP/1\.1" 200(?: OK)?')
    stream_pattern = re.compile(rf'"GET /chat/turns/{re.escape(turn_id)}/stream\?cursor=0 HTTP/1\.1" 200(?: OK)?')
    crash_pattern = re.compile(rf"chat turn crashed: turn_id={re.escape(turn_id)}(?:\s|$)")
    planner_degradation_pattern = re.compile(
        r"(?:sub_planner|planner)[^\n]*(?:縮退|失敗|空のため|利用不能|fallback)",
        re.IGNORECASE,
    )
    deadline = time.monotonic() + timeout_seconds
    observed = b""
    files: list[str] = []
    while True:
        chunks: list[bytes] = []
        files = []
        for path in sorted(service_root.glob("app*.log")):
            if not path.is_file():
                continue
            raw = path.read_bytes()
            offset = int(offsets.get(str(path), 0))
            if offset < 0 or offset > len(raw):
                offset = 0
            chunks.append(raw[offset:])
            files.append(path.name)
        observed = b"\n".join(chunks)
        text = observed.decode("utf-8", errors="replace")
        if post_pattern.search(text) and stream_pattern.search(text):
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f"application logs did not record both successful start and SSE completion for turn {turn_id}")
        time.sleep(0.1)
    assert not crash_pattern.search(text), "application log recorded a crash for the successful turn"
    assert not planner_degradation_pattern.search(text), "application log recorded planner degradation during a turn reported as successful"
    return {
        "log_files": files,
        "observed_bytes": len(observed),
        "observed_bytes_sha256": hashlib.sha256(observed).hexdigest(),
        "turn_start_request": {"method": "POST", "path": "/chat/turns", "status": 200},
        "turn_stream_request": {
            "method": "GET",
            "path_template": "/chat/turns/{turn_id}/stream?cursor=0",
            "status": 200,
            "turn_id_matched": True,
        },
        "turn_crash_log_match": False,
        "planner_degradation_log_match": False,
        "raw_log_persisted": False,
    }


def assert_trace_correlation(events: list[dict], assistant_message: dict, ui_nodes: list[dict]) -> None:
    streamed = final_nodes(events)
    trace = assistant_message.get("trace") or []
    assert streamed, "SSE has no final execution nodes"
    assert trace, "assistant message has no persisted trace"
    assert_no_degraded_trace("raw SSE", [event for event in events if event.get("type") == "node"])
    assert_no_degraded_trace("conversation API", trace)
    assert_no_degraded_trace("rendered UI", ui_nodes)

    # The live UI receives the unabridged replay even when persistence applies a
    # v1 tail cap or v2 aggregate budget.  Compare it to the final state of every
    # streamed node, not merely to the retained DB tail; otherwise a missing or
    # duplicated node before that tail would be invisible to the test.
    def rendered_detail(node: dict) -> str:
        detail = str(node.get("detail") or "")
        if str(node.get("kind") or "") != "tool":
            return detail.strip()
        match = re.search(r"「([^」]*)」", detail)
        if not match or not match.group(1):
            return detail.strip()
        # web/chat/render.js::_renderDetail renders the quoted query chip first,
        # followed immediately by the remaining detail text.
        rest = (detail[: match.start()] + detail[match.end() :]).strip()
        return (match.group(1) + rest).strip()

    streamed_projection = [
        {
            "label": str(node.get("label") or "").strip(),
            "detail": rendered_detail(node),
            "status": str(node.get("status") or ""),
            "kind": "tool" if str(node.get("kind") or "") == "tool" else "think",
        }
        for node in streamed
    ]
    ui_projection = [
        {
            "label": str(node.get("label") or "").strip(),
            "detail": str(node.get("detail") or "").strip(),
            "status": str(node.get("status") or ""),
            "kind": str(node.get("kind") or ""),
        }
        for node in ui_nodes
    ]
    assert ui_projection == streamed_projection, {
        "reason": "rendered trace duplicated, omitted, reordered, or changed a final node",
        "streamed": streamed_projection,
        "rendered": ui_projection,
    }
    # Independently validate exact v1 persistence or complete v1/v2 cap
    # accounting.  This also rejects orphaned aggregates and nonterminal DB/API
    # nodes rather than treating a shorter trace as an acceptable prefix/tail.
    assert_persisted_trace_after_cap(events, assistant_message)
