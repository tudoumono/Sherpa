from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from ui_automation.runner.artifacts import write_private_bytes_atomic
from ui_automation.runner.filesystem_safety import (
    assert_no_mount_targets,
    assert_no_unsafe_hardlinks,
    chmod_path_no_follow,
    chmod_tree_no_follow,
    rmtree_no_follow,
)

from ui_automation.stack.isolation import verify_local_docker_environment
from ui_automation.support.chat import (
    answer_event,
    assert_node_status_lifecycle,
    assert_persisted_trace_after_cap,
    assert_real_ai_result,
    final_nodes,
)
from ui_automation.support.chat_flow import (
    last_assistant_message,
    prepare_chat,
    start_turn_from_ui,
    ui_trace_nodes,
    wait_for_completed_ui,
)
from ui_automation.support.database import (
    conversation_database_snapshot,
    wait_for_ingestion_database_snapshot,
)
from ui_automation.support.live_api import LiveApi


_CHAT_PROBES = {
    "author-duration",
    "author-trace",
    "bedrock-probe",
    "chat-error",
    "conversation-continuity",
    "evaluation-cycle-count",
    "evidence-verification",
    "gemini-probe",
    "ollama-probe",
    "provider-duration",
    "provider-probe",
    "provider-request-count",
    "provider-request-summary",
    "redacted-provider-request",
    "sse-node-count",
    "sse-timing",
    "sse-tool-count",
    "sse-tool-nodes",
    "subagent-call-count",
    "subagent-step-count",
    "tls-probe",
    "tool-nodes",
    "trace-result-size",
    "trace-total-size",
    "turn-duration",
    "turn-slot-release",
    "ui-trace-order",
}
_DATABASE_PROBES = {"postgres-trace"}
_OBSERVATION_PROBES = {
    "container-group",
    "container-memory-limit",
    "container-user",
    "observation",
    "processed-page-count",
    "rendered-image-size",
}
_OFFICE_PROBES = {
    "redacted-worker-request",
    "worker-probe",
    "worker-request-summary",
}
_WORKSPACE_PROBES = {"expiry-time", "upload-rejection", "upload-result"}
_WORLD_PROBES = {
    "conversion-duration",
    "conversion-result",
    "derived-markdown",
    "elasticsearch-document",
    "elasticsearch-identity",
    "elasticsearch-mapping",
    "elasticsearch-source",
    "elasticsearch-vector-field",
    "graph-error",
    "graph-node-count",
    "graph-row-count",
    "ingest-result",
    "ingest-run",
    "query-duration",
    "refresh-time",
    "search-hit-size",
    "search-source",
    "selected-arm",
    "truncated-flag",
}

SUPPORTED_PROBES = frozenset().union(
    _CHAT_PROBES,
    _DATABASE_PROBES,
    _OBSERVATION_PROBES,
    _OFFICE_PROBES,
    _WORKSPACE_PROBES,
    _WORLD_PROBES,
)

# Generated env scenarios which used to be routed through the broad ``direct``
# adapters in environment_probes.py.  Keep this registry deliberately narrow:
# the caller can fall back to the old adapter for every other variable/profile,
# while these pairs require a variable-specific, real product effect.
DIRECT_PROBE_VARIABLES: dict[str, frozenset[str]] = {
    "chat-answer": frozenset({"SHERPA_AGENT"}),
    "documents": frozenset({"SHERPA_VERSION"}),
    "ocr-observations": frozenset({"SHERPA_OBSERVATION_DIR"}),
    "provider-usage": frozenset({"SHERPA_AGENT"}),
    "settings-agent": frozenset({"SHERPA_AGENT"}),
    "status-api": frozenset({"SHERPA_VERSION"}),
    "usage-records": frozenset({"SHERPA_USAGE_METERING"}),
    "workspace-files": frozenset({"SHERPA_USERS_DIR"}),
    "world-path": frozenset({"SHERPA_VERSION"}),
}

_PROBE_VARIABLES: dict[str, frozenset[str]] = {
    "author-duration": frozenset({"SHERPA_CODEX_TIMEOUT_AUTHOR"}),
    "author-trace": frozenset({"SHERPA_CODEX_REASONING_AUTHOR", "SHERPA_MARP_BIN"}),
    "bedrock-probe": frozenset(),
    "chat-error": frozenset(
        {
            "SHERPA_LLM_TIMEOUT",
            "SHERPA_AGENTIC_MAX_TURNS",
            "SHERPA_AGENTIC_MAX_TOOLS_PER_TURN",
            "SHERPA_SUB_PLAN_MAX_CALLS",
            "SHERPA_CODEX_TIMEOUT",
        }
    ),
    "container-group": frozenset({"SHERPA_OCR_GID"}),
    "container-memory-limit": frozenset({"SHERPA_OCR_MEMORY_LIMIT"}),
    "container-user": frozenset({"SHERPA_OCR_UID"}),
    "conversation-continuity": frozenset({"SHERPA_HISTORY_TURNS", "SHERPA_HISTORY_MSG_CHARS", "SHERPA_HISTORY_CHAR_BUDGET"}),
    "evaluation-cycle-count": frozenset({"SHERPA_AGENTIC_EVAL_CYCLE_TURNS"}),
    "evidence-verification": frozenset({"SHERPA_AGENTIC_EVIDENCE_VERIFY"}),
    "conversion-duration": frozenset({"SHERPA_LEGACY_TIMEOUT"}),
    "conversion-result": frozenset(
        {"SHERPA_SOFFICE_BIN", "SHERPA_OFFICE_COM_URL", "SHERPA_OFFICE_COM_TOKEN", "SHERPA_OFFICE_TRANSFER_MODE", "SHERPA_POWERSHELL_BIN"}
    ),
    "derived-markdown": frozenset({"SHERPA_PDF_TEXT_MIN_CHARS"}),
    "elasticsearch-document": frozenset(),
    "elasticsearch-identity": frozenset({"SHERPA_ES_PORT", "ES_URL"}),
    "elasticsearch-mapping": frozenset({"ES_MAPPING_VERSION"}),
    "elasticsearch-source": frozenset({"SHERPA_SEARCH_RAG_ES"}),
    "elasticsearch-vector-field": frozenset({"OPENAI_EMBED_MODEL", "SHERPA_DISABLE_EMBED"}),
    "expiry-time": frozenset({"SHERPA_WORKSPACE_TTL_DAYS"}),
    "gemini-probe": frozenset(),
    "graph-error": frozenset({"SHERPA_NEO4J_QUERY_TIMEOUT_S"}),
    "graph-node-count": frozenset({"SHERPA_GRAPH_NODE_LIMIT"}),
    "graph-row-count": frozenset({"SHERPA_NEO4J_MAX_ROWS"}),
    "ingest-result": frozenset({"OPENAI_EMBED_URL"}),
    "ingest-run": frozenset({"SHERPA_POLL_SECONDS"}),
    "observation": frozenset({"SHERPA_OCR_MAX_PAGES", "SHERPA_OCR_MAX_PIXELS", "SHERPA_VLM_OLLAMA_URL", "SHERPA_OCR_ENABLED"}),
    "ollama-probe": frozenset(),
    "postgres-trace": frozenset({"SHERPA_EXEC_EVENT_V2"}),
    "processed-page-count": frozenset({"SHERPA_OCR_MAX_PAGES"}),
    "provider-duration": frozenset({"SHERPA_LLM_TIMEOUT", "SHERPA_VLM_TIMEOUT"}),
    "provider-probe": frozenset(
        {
            "OLLAMA_HOST",
            "OLLAMA_HOME",
            "OLLAMA_MODELS_DIR",
            "SHERPA_VLM_OLLAMA_URL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        }
    ),
    "provider-request-count": frozenset({"SHERPA_HEALTH_AI_TTL"}),
    "provider-request-summary": frozenset({"SHERPA_HISTORY_TURNS", "SHERPA_HISTORY_MSG_CHARS", "SHERPA_HISTORY_CHAR_BUDGET"}),
    "query-duration": frozenset({"SHERPA_NEO4J_QUERY_TIMEOUT_S"}),
    "redacted-provider-request": frozenset({"OPENAI_CHAT_URL", "OPENAI_EMBED_URL"}),
    "redacted-worker-request": frozenset({"SHERPA_OFFICE_COM_TOKEN"}),
    "refresh-time": frozenset({"SHERPA_POLL_SECONDS"}),
    "rendered-image-size": frozenset({"SHERPA_OCR_MAX_PIXELS"}),
    "search-hit-size": frozenset({"SHERPA_GREP_HIT_TEXT_MAX_BYTES"}),
    "search-source": frozenset({"SHERPA_SEARCH_RAG_GREP"}),
    "selected-arm": frozenset({"SHERPA_PDF_TEXT_MIN_CHARS"}),
    "sse-node-count": frozenset({"SHERPA_AGENTIC_MAX_TURNS"}),
    "sse-timing": frozenset({"SHERPA_STREAM_PACE"}),
    "sse-tool-count": frozenset({"SHERPA_AGENTIC_MAX_TOOLS_PER_TURN"}),
    "sse-tool-nodes": frozenset({"SHERPA_SUBAGENTS_ENABLED"}),
    "subagent-call-count": frozenset({"SHERPA_SUB_PLAN_MAX_CALLS"}),
    "subagent-step-count": frozenset({"SHERPA_SUB_PLAN_MAX_STEPS"}),
    "tls-probe": frozenset({"SSL_CERT_FILE", "SSL_CERT_DIR"}),
    "tool-nodes": frozenset({"SHERPA_CODEX_MCP", "SHERPA_ALLOW_WEB_SEARCH"}),
    "trace-result-size": frozenset({"SHERPA_AGENTIC_MAX_TOOL_RESULT_BYTES"}),
    "trace-total-size": frozenset({"SHERPA_AGENTIC_MAX_TOTAL_TOOL_RESULT_BYTES"}),
    "truncated-flag": frozenset({"SHERPA_GRAPH_NODE_LIMIT", "SHERPA_NEO4J_MAX_ROWS"}),
    "turn-duration": frozenset({"SHERPA_CODEX_TIMEOUT"}),
    "turn-slot-release": frozenset({"SHERPA_CODEX_TIMEOUT"}),
    "ui-trace-order": frozenset({"SHERPA_STREAM_PACE"}),
    "upload-rejection": frozenset({"SHERPA_VLM_MAX_IMAGE_MB"}),
    "upload-result": frozenset({"SHERPA_WORKSPACE_MAX_BYTES", "SHERPA_EXT_CONVERT_MAX_BYTES"}),
    "worker-probe": frozenset({"SHERPA_OFFICE_COM_URL"}),
    "worker-request-summary": frozenset({"SHERPA_OFFICE_TRANSFER_MODE"}),
}

assert set(_PROBE_VARIABLES) == set(SUPPORTED_PROBES)

_FAILURE_WORDS = (
    "接続できません",
    "利用できません",
    "タイムアウト",
    "エラー",
    "失敗",
)


def _sha256(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scenario(ctx) -> dict:
    value = ctx.contract.get("generated_scenario") or {}
    assert isinstance(value, dict), "generated scenario must be an object"
    return value


def _variable(ctx) -> str:
    return str(_scenario(ctx).get("variable") or "")


def supports_direct_probe(probe_id: str, variable: str) -> bool:
    """Return whether this module has a strict adapter for the exact pair.

    A probe id alone is intentionally insufficient.  Several legacy direct
    adapters are shared by unrelated variables; accepting one of those merely
    because the endpoint is healthy would recreate the false-positive gap this
    module closes.
    """

    return str(variable) in DIRECT_PROBE_VARIABLES.get(str(probe_id), frozenset())


def _assert_direct_probe_contract(ctx, probe_id: str) -> str:
    scenario = _scenario(ctx)
    assert scenario, "content direct semantics require a generated environment scenario"
    variable = str(scenario.get("variable") or "")
    declared = {str(value) for value in scenario.get("observables") or ()}
    assert variable, "generated direct-semantic scenario omitted its variable"
    assert probe_id in declared, f"{probe_id} is not declared by generated scenario for {variable}"
    assert supports_direct_probe(probe_id, variable), f"{probe_id} has no variable-specific content direct adapter for {variable}"
    return variable


def _scenario_name(ctx) -> str:
    return str(_scenario(ctx).get("scenario") or "declared")


def _assert_probe_contract(ctx, probe_id: str) -> None:
    assert probe_id in _PROBE_VARIABLES
    scenario = _scenario(ctx)
    if scenario:
        variable = str(scenario.get("variable") or "")
        declared = {str(value) for value in scenario.get("observables") or ()}
        assert variable, "generated content-semantic scenario omitted its variable"
        assert probe_id in declared, f"{probe_id} is not declared by generated scenario for {variable}"
        assert variable in _PROBE_VARIABLES[probe_id], f"{probe_id} cannot semantically demonstrate generated variable {variable}"
        process = scenario.get("process") or {}
        assert isinstance(process, dict), "generated process contract must be an object"
        mode = str(process.get("mode") or "")
        actual = os.environ.get(variable)
        if mode in {"absent", "unset"}:
            assert actual is None, f"{variable} should be absent in the generated content-semantic process"
        elif mode:
            expected_value = process.get("value")
            if bool(scenario.get("secret")):
                assert bool(actual) is (str(expected_value) == "set"), f"{variable} secret presence differs from generated scenario"
            else:
                assert actual == expected_value, f"{variable} process value differs from generated scenario"
    else:
        declared = {str(item.get("id") if isinstance(item, dict) else item) for item in (ctx.contract or {}).get("probes") or ()}
        assert probe_id in declared, f"static profile did not declare {probe_id}"


def _cache(ctx, key: str, callback):
    if hasattr(ctx, "cached"):
        return ctx.cached(key, callback)
    cache = getattr(ctx, "cache")
    if key not in cache:
        cache[key] = callback()
    return cache[key]


def _result(ctx, probe_id: str, source: str, measurements: dict) -> dict:
    variable = _variable(ctx)
    raw = os.environ.get(variable) if variable else None
    secret = bool(_scenario(ctx).get("secret"))
    return {
        "source": source,
        "probe_id": probe_id,
        "scenario_variable": variable or None,
        "scenario": _scenario_name(ctx),
        "configured_presence": raw is not None,
        "configured_value_sha256": (_sha256(raw) if raw is not None and not secret else None),
        "secret_presence_only": secret,
        "measurements": measurements,
    }


def _bool_value(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return max(minimum, min(default, maximum))
    try:
        parsed = int(raw)
    except ValueError:
        return max(minimum, min(default, maximum))
    return parsed if minimum <= parsed <= maximum else max(minimum, min(default, maximum))


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _service_root(ctx) -> Path:
    assert ctx.config.expected_env_path is not None
    root = ctx.config.expected_env_path.parent.parent / "services"
    assert root.is_dir(), f"profile service log directory is absent: {root}"
    return root


def _run_owned_browse_root(ctx) -> Path:
    browse_root = Path(os.environ["SHERPA_BROWSE_ROOTS"]).resolve()
    fixture_world = ctx.config.world_path.resolve()
    runtime_root = browse_root.parent
    marker = runtime_root / ".ui-automation-runtime.json"
    assert fixture_world.parent == browse_root, "content semantic probe may create inputs only beside the isolated fixture World"
    assert marker.is_file() and not marker.is_symlink(), "isolated runtime ownership marker is absent; refusing to create a probe World"
    return browse_root


def _remove_probe_world_source(ctx, root: Path) -> None:
    browse_root = _run_owned_browse_root(ctx)
    resolved = root.resolve()
    assert resolved.parent == browse_root and resolved.name.startswith("env-semantic-"), (
        f"refusing to remove a non-probe World source: {resolved}"
    )
    browse_mode = stat.S_IMODE(browse_root.stat().st_mode)
    try:
        chmod_path_no_follow(browse_root, browse_mode | stat.S_IWUSR | stat.S_IXUSR, require_owner_uid=os.geteuid())
        if resolved.exists():
            chmod_tree_no_follow(resolved, directory_mode=0o700, file_mode=0o600, allow_symlinks=False)
            assert_no_mount_targets(resolved)
            rmtree_no_follow(resolved)
    finally:
        chmod_path_no_follow(browse_root, browse_mode, require_owner_uid=os.geteuid())


def _create_probe_world(
    ctx,
    purpose: str,
    files: dict[str, bytes],
    *,
    mutable: frozenset[str] = frozenset(),
) -> dict:
    """Create and register a real, runner-owned World used to cross a limit.

    The repository fixture stays untouched.  Only the isolated browse-root copy is
    made temporarily writable, and cleanup validates the exact run-owned child.
    """

    browse_root = _run_owned_browse_root(ctx)
    token = _sha256(f"{ctx.config.run_id}:{_variable(ctx)}:{purpose}")[:12]
    source_root = browse_root / f"env-semantic-{purpose}-{token}"
    assert not source_root.exists(), f"probe World source already exists: {source_root}"
    browse_mode = stat.S_IMODE(browse_root.stat().st_mode)
    try:
        chmod_path_no_follow(browse_root, browse_mode | stat.S_IWUSR | stat.S_IXUSR, require_owner_uid=os.geteuid())
        source_root.mkdir(mode=0o700)
        for relative, raw in sorted(files.items()):
            rel = Path(relative)
            assert not rel.is_absolute() and ".." not in rel.parts and rel.parts
            path = source_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            write_private_bytes_atomic(path, raw)
            chmod_path_no_follow(path, 0o600 if relative in mutable else 0o400, require_owner_uid=os.geteuid())
        assert_no_mount_targets(source_root)
        assert_no_unsafe_hardlinks(source_root)
        candidates = list(source_root.rglob("*"))
        assert not any(path.is_symlink() for path in candidates), "new probe World source contains a symlink"
        for directory in sorted((path for path in candidates if path.is_dir()), reverse=True):
            chmod_path_no_follow(directory, 0o500, require_owner_uid=os.geteuid())
        chmod_path_no_follow(source_root, 0o500, require_owner_uid=os.geteuid())
    finally:
        chmod_path_no_follow(browse_root, browse_mode, require_owner_uid=os.geteuid())

    started = time.monotonic()
    created = ctx.api.post_json(
        "/worlds",
        {"path": str(source_root), "label": f"Environment semantic {purpose}"},
        save_as=f"state/content-semantic-{purpose}-world-create.json",
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    world = created.get("world") or {}
    world_id = str(world.get("world_id") or "")
    assert world_id, f"probe World registration returned no world id: {created}"

    def cleanup() -> None:
        ctx.api.request("DELETE", f"/worlds/{world_id}", expected={200, 404})
        _remove_probe_world_source(ctx, source_root)

    ctx.evidence.add_cleanup(f"delete real {purpose} probe World", cleanup)
    return {
        "world_id": world_id,
        "world_id_sha256": _sha256(world_id),
        "source_root": source_root,
        "registration_elapsed_ms": elapsed_ms,
        "registration": created,
    }


def _cleanup_conversation(api, conversation_id: int) -> None:
    response = api.request("GET", f"/conversations/{conversation_id}", expected={200, 404})
    if response.status == 200:
        api.delete_json(f"/conversations/{conversation_id}")


def _wait_for_slot_release(ctx, turn_id: str) -> dict:
    deadline = time.monotonic() + max(ctx.config.timeout_ms / 1000, 10)
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = ctx.api.get_json("/chat/turns/running")
        running = latest.get("turns") or []
        if all(str(row.get("turn_id")) != turn_id for row in running):
            return latest
        time.sleep(0.2)
    raise AssertionError(f"real chat turn retained its execution slot: {latest}")


def _chat_prompt(variable: str) -> str:
    if variable == "SHERPA_USAGE_METERING":
        # Two strong, conflicting routing cues force the real Tier-2 intent
        # classifier.  That auxiliary provider call is what usage_metering is
        # specified to record (the main answer remains in messages.answer).
        return "SHERPA-LIVE-REFERENCE-314 の障害原因と変更影響を実資料と実ツールで調べ、夜間運用時刻を答えてください。"
    if variable == "SHERPA_STREAM_PACE":
        # ``base._gather`` emits both troubleshoot tool nodes as active,
        # performs the real dispatch, and then applies one configured pace
        # before each done event.  The two consecutive done events therefore
        # provide an external SSE interval with no provider call or search in
        # between; whole-turn duration or answer-token cadence would measure a
        # different concern and can falsely pass because of provider latency.
        return "SHERPA-LIVE-REFERENCE-314 の障害原因を関係グラフと運用手順の両方の実ツールで調べ、夜間運用時刻を答えてください。"
    if variable in {"SHERPA_CODEX_REASONING_AUTHOR", "SHERPA_CODEX_TIMEOUT_AUTHOR", "SHERPA_MARP_BIN"}:
        return (
            "取り込んだ資料だけを根拠に、SHERPA-LIVE-REFERENCE-314 の運用時刻を説明する"
            "短いMarkdown資料を個人ワークスペースへ実際に作成してください。"
        )
    if variable in {"SHERPA_SUBAGENTS_ENABLED", "SHERPA_SUB_PLAN_MAX_CALLS", "SHERPA_SUB_PLAN_MAX_STEPS"}:
        return (
            "複数の専門担当へ実際に委譲し、SHERPA-LIVE-REFERENCE-314 と "
            "SHERPA-LIVE-ALPHA-927 を資料から確認して、使った根拠を示してください。"
        )
    if variable == "SHERPA_ALLOW_WEB_SEARCH":
        return (
            "Web検索ツールを実際に使える場合だけ1回使い、その後、ローカル資料 "
            "SHERPA-LIVE-REFERENCE-314 の運用時刻を実資料で確認してください。"
        )
    if variable == "SHERPA_CODEX_MCP":
        return (
            "Sherpa MCP の ripgrep_search と read_around を実際に使って、SHERPA-LIVE-REFERENCE-314 の運用時刻を根拠付きで答えてください。"
        )
    if variable in {
        "SHERPA_AGENTIC_MAX_TURNS",
        "SHERPA_AGENTIC_MAX_TOOLS_PER_TURN",
        "SHERPA_AGENTIC_MAX_TOOL_RESULT_BYTES",
        "SHERPA_AGENTIC_MAX_TOTAL_TOOL_RESULT_BYTES",
        "SHERPA_AGENTIC_EVAL_CYCLE_TURNS",
        "SHERPA_AGENTIC_EVIDENCE_VERIFY",
    }:
        return (
            "SHERPA-LIVE-REFERENCE-314、SHERPA-LIVE-ALPHA-927、TAXCALC、NIGHTLYを"
            "別々の実ツール呼び出しで詳しく調査し、上限に達した場合はその事実も示してください。"
        )
    return "SHERPA-LIVE-REFERENCE-314 の運用時刻を実資料と実ツールで確認してください。"


def _structured_error(message: dict) -> bool:
    answer = message.get("answer") or {}
    headline = str(answer.get("headline") or message.get("content") or "")
    return bool(
        answer.get("error") is True
        or answer.get("status") == "error"
        or answer.get("error_code")
        or any(word in headline for word in _FAILURE_WORDS)
    )


def _sse_receive_timing(ctx, events: list[dict]) -> dict:
    """Correlate each raw SSE event with its independent-client receipt time.

    The event JSON deliberately remains unchanged.  Receipt times are local
    measurements emitted by ``LiveApi.collect_sse`` to a separate timing
    JSONL, so this does not trust a server-supplied timestamp or product
    internal.  Node ids are hashed before returning evidence.
    """

    timing_reader = getattr(ctx.api, "last_sse_timings", None)
    assert callable(timing_reader), "independent SSE client did not expose receipt timings"
    timings = timing_reader()
    assert len(timings) == len(events), {
        "event_count": len(events),
        "timing_count": len(timings),
    }

    indexed: list[tuple[dict, int]] = []
    for index, event in enumerate(events):
        row = timings[index]
        assert int(row.get("index", -1)) == index, row
        assert str(row.get("type") or "") == str(event.get("type") or ""), {
            "index": index,
            "event_type": event.get("type"),
            "timing_type": row.get("type"),
        }
        indexed.append((event, int(row.get("elapsed_since_open_ms") or 0)))

    answer_delta_times = [received_ms for event, received_ms in indexed if event.get("type") == "answer_delta"]
    answer_delta_intervals = [current - previous for previous, current in zip(answer_delta_times, answer_delta_times[1:], strict=False)]

    active_received: dict[str, int] = {}
    lifecycle_intervals: list[dict] = []
    for event, received_ms in indexed:
        if event.get("type") != "node":
            continue
        node_id = str(event.get("id") or "")
        status_value = str(event.get("status") or "")
        if status_value == "active":
            active_received[node_id] = received_ms
        elif status_value == "done" and node_id in active_received:
            lifecycle_intervals.append(
                {
                    "node_id_sha256": _sha256(node_id),
                    "kind": str(event.get("kind") or ""),
                    "active_to_done_receive_ms": received_ms - active_received[node_id],
                }
            )

    tool_done = [
        (str(event.get("id") or ""), received_ms)
        for event, received_ms in indexed
        if event.get("type") == "node" and event.get("kind") == "tool" and event.get("status") == "done"
    ]
    consecutive_tool_done_intervals = [
        {
            "previous_node_id_sha256": _sha256(previous[0]),
            "current_node_id_sha256": _sha256(current[0]),
            "receive_interval_ms": current[1] - previous[1],
        }
        for previous, current in zip(tool_done, tool_done[1:], strict=False)
    ]
    paced_tool_pair_receive_ms = next(
        (
            current[1] - previous[1]
            for previous, current in zip(tool_done, tool_done[1:], strict=False)
            if previous[0] == "tool-graph" and current[0] == "tool-docs"
        ),
        None,
    )
    return {
        "sse_timing_source": "independent HTTP SSE client monotonic receipt clock",
        "sse_timing_event_count": len(timings),
        "sse_open_to_first_event_ms": indexed[0][1] if indexed else None,
        "answer_delta_receive_intervals_ms": answer_delta_intervals,
        "node_active_to_done_receive_intervals": lifecycle_intervals,
        "consecutive_tool_done_receive_intervals": consecutive_tool_done_intervals,
        "paced_tool_pair_receive_ms": paced_tool_pair_receive_ms,
    }


def _chat_turn(ctx) -> dict:
    def collect() -> dict:
        variable = _variable(ctx)
        world_id = _metering_direct_world(ctx) if variable == "SHERPA_USAGE_METERING" else ctx.fixture("real_world")
        settings = ctx.api.get_json("/settings", save_as="state/content-semantics-chat-settings.json")
        me = ctx.api.get_json("/auth/me")
        user_id = str(me.get("uid") or "")
        assert user_id, "real chat user identity is absent"
        usage_checkpoint_rows = _postgres_rows(
            ctx,
            "SELECT COALESCE(MAX(id),0)::bigint AS id FROM usage_events",
        )
        usage_checkpoint = int(usage_checkpoint_rows[0]["id"])
        prepare_chat(ctx.page, ctx.config, world_id)
        started_at = time.monotonic()
        started = start_turn_from_ui(ctx.page, ctx.config, _chat_prompt(variable))
        conversation_id = int(started["conversation_id"])
        turn_id = str(started["turn_id"])
        ctx.evidence.add_cleanup(
            f"delete content semantics conversation {conversation_id}",
            lambda: _cleanup_conversation(ctx.api, conversation_id),
        )
        events = ctx.api.collect_sse(
            f"/chat/turns/{turn_id}/stream?cursor=0",
            save_as="network/content-semantics-sse.jsonl",
        )
        receive_timing = _sse_receive_timing(ctx, events)
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        _wait_for_slot_release(ctx, turn_id)
        try:
            wait_for_completed_ui(ctx.page, ctx.config.timeout_ms)
        except Exception:
            pass
        conversation = ctx.api.get_json(
            f"/conversations/{conversation_id}",
            save_as="state/content-semantics-conversation.json",
        )
        assistant = last_assistant_message(conversation)
        database = conversation_database_snapshot(
            ctx.config.database_url,
            conversation_id,
            ctx.evidence,
            turn_id=turn_id,
        )
        database_assistants = [row for row in database["messages"] if row.get("role") == "assistant"]
        assert database_assistants, "real turn has no Postgres assistant row"
        assert database_assistants[-1].get("content") == assistant.get("content")
        error_events = [row for row in events if row.get("type") == "error"]
        answer_events = [row for row in events if row.get("type") == "answer"]
        is_error = bool(error_events) or _structured_error(assistant)
        nodes = final_nodes(events)
        ui_nodes = ui_trace_nodes(ctx.page) if ctx.page.locator("#flow details.fturn").count() else []
        correlation = None
        lifecycle = None
        persisted = None
        if not is_error:
            answer_event(events)
            correlation = assert_real_ai_result(
                settings,
                events,
                assistant,
                require_tool=True,
                evidence=ctx.evidence,
                turn_id=turn_id,
                conversation_id=conversation_id,
                database_url=ctx.config.database_url,
                checkpoint=started["_real_ai_checkpoint"],
                operation="environment-content-semantics",
            )
            lifecycle = assert_node_status_lifecycle(events)
            persisted = assert_persisted_trace_after_cap(events, assistant)
        tool_nodes = [node for node in nodes if node.get("kind") == "tool"]
        subagent_nodes = [node for node in nodes if str(node.get("id") or "").startswith("sub:") or "委譲" in str(node.get("label") or "")]
        node_payload_sizes = [len(json.dumps(node, ensure_ascii=False, sort_keys=True).encode("utf-8")) for node in tool_nodes]
        node_summaries = [
            {
                "id_sha256": _sha256(str(node.get("id") or "")),
                "kind": str(node.get("kind") or ""),
                "label": str(node.get("label") or "")[:160],
                "status": str(node.get("status") or ""),
                "payload_bytes": len(json.dumps(node, ensure_ascii=False, sort_keys=True).encode("utf-8")),
            }
            for node in nodes
        ]
        audit_outcomes = [str(row.get("outcome") or "") for row in database["audit"] if row.get("action") == "chat.turn"]
        database_usage = (database_assistants[-1].get("answer") or {}).get("usage") or {}
        turn_audits = [row for row in database["audit"] if row.get("action") == "chat.turn"]
        terminal_audit = turn_audits[-1] if turn_audits else {}
        audit_detail = terminal_audit.get("detail") or {}
        answer_payload = assistant.get("answer") or {}
        answer_data = answer_payload.get("data") or {}
        evidence_packet = answer_data.get("evidence_packet") or {}
        evidence_rows = [row for row in evidence_packet.get("evidence") or [] if isinstance(row, dict)]
        verification_methods = [row.get("verification_method") for row in evidence_rows]
        auxiliary_usage = _postgres_rows(
            ctx,
            "SELECT id,kind,provider,model,input_tokens,cached_input_tokens,output_tokens,"
            "reasoning_output_tokens,calls,user_id,world FROM usage_events "
            "WHERE id>%s AND user_id=%s AND world=%s ORDER BY id",
            (usage_checkpoint, user_id, world_id),
        )
        if is_error:
            assert not answer_events, "provider failure also emitted a terminal success answer"
            assert "error" in audit_outcomes, "provider failure has no error audit row"
        assistant_text = str((assistant.get("answer") or {}).get("headline") or assistant.get("content") or "")
        return {
            "source": "real browser turn, independent SSE, conversation API, direct Postgres, audit and usage",
            "turn_id_sha256": _sha256(turn_id),
            "conversation_id_sha256": _sha256(str(conversation_id)),
            "elapsed_ms": elapsed_ms,
            "event_count": len(events),
            "event_types": [str(row.get("type") or "") for row in events],
            "answer_delta_count": sum(row.get("type") == "answer_delta" for row in events),
            "node_count": len(nodes),
            "tool_node_count": len(tool_nodes),
            "subagent_node_count": len(subagent_nodes),
            "ui_node_count": len(ui_nodes),
            "sse_node_order": [_sha256(str(node.get("id") or "")) for node in nodes],
            "ui_node_order": [_sha256(str(node.get("id") or "")) for node in ui_nodes],
            "tool_payload_max_bytes": max(node_payload_sizes, default=0),
            "tool_payload_total_bytes": sum(node_payload_sizes),
            "node_summaries": node_summaries,
            "provider": (correlation or {}).get("provider"),
            "model": (correlation or {}).get("model"),
            "input_tokens": (correlation or {}).get("input_tokens", 0),
            "output_tokens": (correlation or {}).get("output_tokens", 0),
            "answer_sha256": (correlation or {}).get("answer_sha256"),
            "grounded_fixture_fact_seen": "02:15" in assistant_text,
            "assistant_message_id": (correlation or {}).get("assistant_message_id"),
            "database_provider": database_usage.get("provider"),
            "database_model": database_usage.get("model"),
            "database_input_tokens": int(database_usage.get("input_tokens") or 0),
            "database_output_tokens": int(database_usage.get("output_tokens") or 0),
            "database_usage_storage": "messages.answer.usage" if database_usage else None,
            "usage_event_checkpoint": usage_checkpoint,
            "auxiliary_usage_event_count": len(auxiliary_usage),
            "auxiliary_usage_events": auxiliary_usage,
            "audit_id": (correlation or {}).get("audit_id"),
            "audit_provider": audit_detail.get("provider"),
            "audit_outcome": terminal_audit.get("outcome"),
            "evidence_packet_present": bool(evidence_packet),
            "evidence_row_count": len(evidence_rows),
            "evidence_verification_methods": verification_methods,
            "trace_version": (persisted or {}).get("trace_version"),
            "persisted_trace_count": (persisted or {}).get("persisted_count", 0),
            "active_to_done_count": (lifecycle or {}).get("active_to_done_count", 0),
            "structured_error": is_error,
            "error_event_count": len(error_events),
            "audit_outcomes": audit_outcomes,
            "turn_slot_released": True,
            "database_exact_content_match": True,
            **receive_timing,
        }

    return _cache(ctx, "content_semantics_chat_turn", collect)


def _history_turns(ctx) -> dict:
    def collect() -> dict:
        variable = _variable(ctx)
        configured = {
            "SHERPA_HISTORY_TURNS": _bounded_int("SHERPA_HISTORY_TURNS", 6, 0, 1000000),
            "SHERPA_HISTORY_MSG_CHARS": _bounded_int("SHERPA_HISTORY_MSG_CHARS", 1200, 0, 100000000),
            "SHERPA_HISTORY_CHAR_BUDGET": _bounded_int("SHERPA_HISTORY_CHAR_BUDGET", 6000, 0, 100000000),
        }
        target_limit = configured.get(variable)
        assert target_limit is not None
        assert target_limit <= 32_000, (
            f"{variable}={target_limit} is too large for the bounded real-provider history workload; "
            "the matrix must supply a reviewable boundary value"
        )
        world_id = ctx.fixture("real_world")
        settings = ctx.api.get_json("/settings")
        assert str(settings.get("agent") or "").strip().lower() == "openai", (
            "SHERPA_HISTORY_* must use the real stateless OpenAI provider; Codex native resume "
            "would bypass the history-priming boundary under test"
        )
        prepare_chat(ctx.page, ctx.config, world_id)
        knowledge = ctx.page.locator("#kbtoggle")
        if knowledge.get_attribute("aria-pressed") == "true":
            knowledge.click()
        conversation_id: int | None = None
        pairs: list[tuple[str, str]] = []
        markers: list[str] = []

        def marker(label: str, index: int) -> str:
            value = (
                f"SHERPA-HISTORY-{label}-{index:02d}-"
                + _sha256(f"{ctx.config.run_id}:{ctx.config.profile}:{variable}:{label}:{index}")[:16]
            )
            ctx.evidence.register_secret(value)
            markers.append(value)
            return value

        def execute(question: str, label: str) -> str:
            nonlocal conversation_id
            started = start_turn_from_ui(ctx.page, ctx.config, question)
            observed_conversation_id = int(started["conversation_id"])
            if conversation_id is None:
                conversation_id = observed_conversation_id
                ctx.evidence.add_cleanup(
                    f"delete content history conversation {conversation_id}",
                    lambda: _cleanup_conversation(ctx.api, int(conversation_id)),
                )
            assert observed_conversation_id == conversation_id
            events = ctx.api.collect_sse(
                f"/chat/turns/{started['turn_id']}/stream?cursor=0",
                save_as=f"network/content-history-{label}-sse.jsonl",
            )
            answer_event(events)
            wait_for_completed_ui(ctx.page, ctx.config.timeout_ms)
            conversation = ctx.api.get_json(f"/conversations/{conversation_id}")
            assistant = last_assistant_message(conversation)
            assert_real_ai_result(
                settings,
                events,
                assistant,
                require_tool=False,
                evidence=ctx.evidence,
                turn_id=str(started["turn_id"]),
                conversation_id=conversation_id,
                database_url=ctx.config.database_url,
                checkpoint=started["_real_ai_checkpoint"],
                operation=f"environment-content-history-{label}",
            )
            content = str(assistant.get("content") or "")
            pairs.append((question, content))
            return content

        if variable == "SHERPA_HISTORY_TURNS":
            assert target_limit <= 12, (
                "SHERPA_HISTORY_TURNS workload is deliberately capped at 12 real provider pairs; use a smaller explicit matrix boundary"
            )
            for index in range(max(1, target_limit + 1)):
                value = marker("TURN", index)
                execute(
                    f"固有識別子 {value} を受領しました。回答は ACK-TURN-{index:02d} のみとしてください。",
                    f"turn-{index:02d}",
                )
        elif variable == "SHERPA_HISTORY_MSG_CHARS":
            keep = marker("MESSAGE-KEEP", 0)
            drop = marker("MESSAGE-DROP", 1)
            if target_limit > 0:
                assert target_limit > len(keep) + 96, (
                    "positive SHERPA_HISTORY_MSG_CHARS matrix value is too small to prove both a retained prefix and truncated suffix"
                )
            prefix = f"固有識別子 {keep} を受領しました。回答は ACK-MESSAGE のみとしてください。\n"
            filler = "境" * max(64, target_limit - len(prefix) + 64)
            response = execute(prefix + filler + f"\n末尾固有識別子 {drop}", "message-boundary")
            assert keep not in response and drop not in response, (
                "the preparatory assistant response repeated a marker and contaminated the message-clipping observation"
            )
        elif variable == "SHERPA_HISTORY_CHAR_BUDGET":
            history_turns = configured["SHERPA_HISTORY_TURNS"]
            message_chars = configured["SHERPA_HISTORY_MSG_CHARS"]
            if target_limit <= 0:
                value = marker("BUDGET-ZERO", 0)
                execute(
                    f"固有識別子 {value} を受領しました。回答は ACK-BUDGET-ZERO のみとしてください。",
                    "budget-zero",
                )
            else:
                assert history_turns >= 2 and message_chars >= 128, (
                    "the surrounding history limits leave no room for an isolated positive SHERPA_HISTORY_CHAR_BUDGET crossing"
                )
                ack_payload = "ACK-BUDGET-" + ("A" * 384)
                for index in range(history_turns - 1):
                    value = marker("BUDGET-LONG", index)
                    question = f"固有識別子 {value} を受領しました。回答は次の文字列だけにしてください:\n{ack_payload}\n" + (
                        "予" * (message_chars + 64)
                    )
                    execute(question, f"budget-long-{index:02d}")
                value = marker("BUDGET-NEWEST", history_turns - 1)
                execute(
                    f"固有識別子 {value} を受領しました。回答は ACK-BUDGET-NEWEST のみとしてください。",
                    "budget-newest",
                )
        else:
            raise AssertionError(f"unsupported exact history workload: {variable}")

        suffix = "…（省略）"

        def clipped(value: str) -> str:
            limit = configured["SHERPA_HISTORY_MSG_CHARS"]
            return value if len(value) <= limit else value[:limit] + suffix

        selected = pairs[-configured["SHERPA_HISTORY_TURNS"] :] if configured["SHERPA_HISTORY_TURNS"] > 0 else []
        budget = configured["SHERPA_HISTORY_CHAR_BUDGET"]
        kept: list[tuple[str, str]] = []
        for user_text, assistant_text in reversed(selected):
            clipped_pair = (clipped(user_text), clipped(assistant_text))
            cost = len(clipped_pair[0]) + len(clipped_pair[1])
            if cost > budget:
                break
            budget -= cost
            kept.append(clipped_pair)
        expected_history = "\n".join(value for pair in reversed(kept) for value in pair)
        expected_markers = [value for value in markers if value in expected_history]
        omitted_markers = [value for value in markers if value not in expected_history]
        if target_limit > 0:
            assert expected_markers and omitted_markers, {
                "variable": variable,
                "configured": configured,
                "workload_pairs": len(pairs),
                "expected_marker_count": len(expected_markers),
                "omitted_marker_count": len(omitted_markers),
            }
            if variable == "SHERPA_HISTORY_TURNS":
                assert markers[0] in omitted_markers and set(markers[-target_limit:]) <= set(expected_markers), (
                    "the exact workload did not isolate the turn-count cap from the character caps"
                )
                boundary_basis = "N+1 complete pairs; newest N retained and oldest pair omitted"
            elif variable == "SHERPA_HISTORY_MSG_CHARS":
                assert markers[0] in expected_markers and markers[1] in omitted_markers, (
                    "the exact workload did not retain the prefix marker and truncate the suffix marker"
                )
                boundary_basis = "one message straddles the exact prefix clipping boundary"
            else:
                assert len(pairs) <= configured["SHERPA_HISTORY_TURNS"]
                assert markers[-1] in expected_markers and any(value in omitted_markers for value in markers[:-1]), (
                    "the exact workload did not isolate the total character budget from the turn-count cap"
                )
                boundary_basis = "all pairs fit the turn cap; newest pair retained and an older pair exceeds remaining budget"
        else:
            assert not expected_markers, {
                "variable": variable,
                "configured": configured,
                "unexpected_expected_markers": len(expected_markers),
            }
            boundary_basis = "zero boundary removes every full marker before provider input"

        recall_question = (
            "過去のユーザー発言として現在見えている、SHERPA-HISTORY- で始まる固有識別子を"
            "省略せず一字一句そのまま1行ずつ列挙してください。見えていなければ NONE だけを回答してください。"
        )
        recalled = execute(recall_question, "boundary-recall")
        missing = [value for value in expected_markers if value not in recalled]
        leaked = [value for value in omitted_markers if value in recalled]
        assert not missing and not leaked, {
            "variable": variable,
            "configured": configured,
            "missing_expected_marker_hashes": [_sha256(value) for value in missing],
            "returned_omitted_marker_hashes": [_sha256(value) for value in leaked],
        }
        assert conversation_id is not None
        database = conversation_database_snapshot(ctx.config.database_url, conversation_id, ctx.evidence)
        roles = [str(row.get("role") or "") for row in database["messages"]]
        assert roles == ["user", "assistant"] * len(pairs), roles
        return {
            "configured": configured,
            "boundary_variable": variable,
            "boundary_value": target_limit,
            "workload_pair_count": len(pairs) - 1,
            "expected_retained_marker_hashes": [_sha256(value) for value in expected_markers],
            "expected_omitted_marker_hashes": [_sha256(value) for value in omitted_markers],
            "provider_returned_all_retained_markers": not missing,
            "provider_returned_no_omitted_markers": not leaked,
            "postgres_message_roles": roles,
            "provider_turn_count": len(pairs),
            "exact_cap_crossing_observed": True,
            "boundary_basis": boundary_basis,
        }

    return _cache(ctx, "content_semantics_history", collect)


def _ai_health_cache_state(ctx) -> dict:
    def collect() -> dict:
        raw = os.environ.get("SHERPA_HEALTH_AI_TTL")
        ttl = float(raw) if raw not in {None, ""} else 60.0
        observations = []
        for index, refresh in enumerate((True, False, False), 1):
            started = time.monotonic()
            payload = ctx.api.get_json(
                LiveApi.query("/admin/health", refresh=str(refresh).lower()),
                save_as=f"state/content-ai-health-{index}.json",
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            rows = [row for row in payload.get("components") or [] if row.get("id") in {"openai", "gemini", "bedrock", "ollama", "codex"}]
            assert len(rows) == 5, f"AI health response omitted components: {rows}"
            observations.append({"elapsed_ms": elapsed_ms, "components": rows})
            time.sleep(0.05)
        assert any(row.get("ok") is True for row in observations[0]["components"]), "real AI health request reached no available provider"
        cached_payload_reused = all(item["components"] == observations[0]["components"] for item in observations[1:])
        if ttl > 0:
            assert cached_payload_reused, "AI health component results changed inside the configured cache TTL"
        else:
            latency_vectors = [tuple(int(row.get("latency_ms") or 0) for row in item["components"]) for item in observations]
            assert len(set(latency_vectors)) > 1 or any(item["elapsed_ms"] >= 10 for item in observations[1:]), (
                "zero AI-health TTL produced no observable provider re-check; the response alone cannot support a request-count claim"
            )
        chat = _chat_turn(ctx)
        assert not chat["structured_error"]
        assert chat["input_tokens"] + chat["output_tokens"] > 0
        return {
            "source": ("three real admin-health requests plus a correlated real provider chat turn"),
            "configured_ttl_seconds": ttl,
            "request_elapsed_ms": [item["elapsed_ms"] for item in observations],
            "component_latency_ms": [
                {str(row.get("id")): int(row.get("latency_ms") or 0) for row in item["components"]} for item in observations
            ],
            "cached_component_payload_reused": cached_payload_reused,
            "available_provider_count": sum(row.get("ok") is True for row in observations[0]["components"]),
            "health_http_request_count": len(observations),
            "chat_provider": chat["provider"],
            "chat_usage_tokens": chat["input_tokens"] + chat["output_tokens"],
        }

    return _cache(ctx, "content_semantics_ai_health_cache", collect)


def _secret_absent_from_service_logs(ctx, variable: str) -> dict:
    raw = os.environ.get(variable)
    if not raw:
        return {"configured": False, "service_log_match_count": 0}
    assert ctx.config.expected_env_path is not None
    service_root = ctx.config.expected_env_path.parent.parent / "services"
    matches = 0
    checked = 0
    for path in service_root.glob("*.log"):
        if not path.is_file():
            continue
        checked += 1
        matches += path.read_bytes().count(raw.encode("utf-8"))
    assert matches == 0, f"{variable} appeared verbatim in captured service logs"
    return {
        "configured": True,
        "value_sha256": _sha256(raw),
        "service_log_count": checked,
        "service_log_match_count": matches,
    }


def _ollama_json(ctx, endpoint: str, path: str, body: dict | None = None) -> tuple[int, dict, int]:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        endpoint.rstrip("/") + path,
        data=encoded,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    ctx.evidence.record_api(
        method="POST" if body is not None else "GET",
        url=endpoint.rstrip("/") + path,
        status=status,
        elapsed_ms=elapsed_ms,
    )
    assert isinstance(payload, dict)
    return status, payload, elapsed_ms


def _ollama_runtime(ctx, *, endpoint_value: str | None = None, model: str | None = None) -> dict:
    endpoint = (endpoint_value or os.environ.get("OLLAMA_HOST") or os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    parsed = urlsplit(endpoint)
    assert parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }, "Ollama semantic probe requires an explicit loopback service"
    status, payload, elapsed_ms = _ollama_json(ctx, endpoint, "/api/tags")
    assert status == 200 and isinstance(payload.get("models"), list), payload
    models = payload["models"]
    assert models, "real Ollama daemon has no installed models"
    available_names = {str(row.get("name") or row.get("model") or "") for row in models}
    selected_model = model or next(iter(sorted(available_names)))
    assert selected_model in available_names or any(name.split(":", 1)[0] == selected_model.split(":", 1)[0] for name in available_names), {
        "selected_model": selected_model,
        "available_model_hashes": sorted(_sha256(name) for name in available_names),
    }
    inference_status, inference, inference_ms = _ollama_json(
        ctx,
        endpoint,
        "/api/generate",
        {
            "model": selected_model,
            "prompt": "Return the single word OK.",
            "stream": False,
            "options": {"num_predict": 8},
        },
    )
    assert inference_status == 200 and str(inference.get("response") or "").strip(), "real Ollama inference returned no answer"
    prompt_tokens = int(inference.get("prompt_eval_count") or 0)
    output_tokens = int(inference.get("eval_count") or 0)
    assert prompt_tokens + output_tokens > 0, "real Ollama inference did not report non-zero usage"
    variable = _variable(ctx)
    model_path = None
    model_file_count = None
    if variable in {"OLLAMA_HOME", "OLLAMA_MODELS_DIR"} and os.environ.get(variable):
        configured = Path(os.environ[variable])
        model_path = configured / "models" if variable == "OLLAMA_HOME" else configured
        assert model_path.is_dir(), f"configured Ollama model directory is absent: {model_path}"
        assert_no_mount_targets(model_path)
        model_candidates = list(model_path.rglob("*"))
        assert not any(path.is_symlink() for path in model_candidates), "configured Ollama model directory contains a symlink"
        model_files = [path for path in model_candidates if path.is_file()]
        assert model_files, (
            f"real Ollama returned models but configured {variable} contains no model files; "
            "the environment value did not affect the running daemon"
        )
        model_file_count = len(model_files)
    return {
        "source": "real Ollama /api/tags response and configured model storage",
        "endpoint_origin_sha256": _sha256(f"{parsed.scheme}://{parsed.netloc}"),
        "status": status,
        "elapsed_ms": elapsed_ms,
        "model_count": len(models),
        "model_names_sha256": sorted(_sha256(str(row.get("name") or row.get("model") or "")) for row in models),
        "configured_model_path_sha256": _sha256(str(model_path.resolve())) if model_path else None,
        "configured_model_file_count": model_file_count,
        "inference_status": inference_status,
        "inference_elapsed_ms": inference_ms,
        "inference_model_sha256": _sha256(selected_model),
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "nonzero_usage": True,
    }


def _vlm_state(ctx) -> dict:
    def collect() -> dict:
        state = _world_state(ctx)
        world_id = ctx.fixture("real_world")
        admin = ctx.api.get_json("/admin/settings", save_as="state/content-semantics-vlm-settings.json")
        effective = (admin.get("vlm") or {}).get("effective") or {}
        provider = str(effective.get("provider") or "").strip().lower()
        model = str(effective.get("model") or "").strip()
        assert provider and model, "real VLM settings omitted provider/model identity"
        document = state["preview_documents"].get("media/vlm-evidence.bmp") or {}
        provenance = document.get("provenance") or {}
        assert document.get("state") == "ready" and provenance.get("method") == "vision", (
            "the deterministic image did not complete through the real vision arm"
        )
        usage = _postgres_rows(
            ctx,
            "SELECT id,ts,provider,model,input_tokens,cached_input_tokens,output_tokens,"
            "reasoning_output_tokens,calls FROM usage_events WHERE kind='vlm' ORDER BY id",
        )
        matching = [
            row
            for row in usage
            if str(row.get("provider") or "").strip().lower() == provider
            and str(row.get("model") or "").strip() == model
            and int(row.get("calls") or 0) > 0
        ]
        assert matching, "real vision conversion has no matching VLM usage row"
        token_total = sum(
            int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0) + int(row.get("reasoning_output_tokens") or 0)
            for row in matching
        )
        assert token_total > 0, "real VLM conversion reported zero token usage"
        for row in matching:
            ctx.evidence.record_usage_event(
                row,
                turn_id=f"content-vlm:{row['id']}",
                operation="environment-content-vlm",
            )
        return {
            "source": "real World vision provenance, effective VLM settings and non-zero Postgres usage",
            "world_id_sha256": _sha256(world_id),
            "provider": provider,
            "model": model,
            "conversion_method": provenance.get("method"),
            "provenance_notes": provenance.get("notes") or [],
            "usage_row_count": len(matching),
            "usage_calls": sum(int(row.get("calls") or 0) for row in matching),
            "usage_tokens": token_total,
            "nonzero_usage": True,
        }

    return _cache(ctx, "content_semantics_vlm", collect)


def _chat_semantics(ctx, probe_id: str) -> dict:
    variable = _variable(ctx)
    if variable == "OPENAI_EMBED_URL" and probe_id == "redacted-provider-request":
        state = _world_state(ctx)
        world_id = ctx.fixture("real_world")
        usage = _postgres_rows(
            ctx,
            "SELECT id,provider,model,input_tokens,output_tokens,calls FROM usage_events WHERE kind='embed' AND world=%s ORDER BY id",
            (world_id,),
        )
        assert usage and all(int(row.get("calls") or 0) > 0 for row in usage)
        assert "embedding" in state["es_properties"]
        return _result(
            ctx,
            probe_id,
            "real World embedding usage, Elasticsearch vectors and redacted service logs",
            {
                "world_id_sha256": _sha256(world_id),
                "usage_rows": len(usage),
                "providers": sorted({str(row.get("provider") or "") for row in usage}),
                "models": sorted({str(row.get("model") or "") for row in usage}),
                "nonzero_calls": sum(int(row.get("calls") or 0) for row in usage),
                "vector_mapping_present": True,
                "redaction": _secret_absent_from_service_logs(ctx, variable),
            },
        )
    if variable in {"OLLAMA_HOST", "OLLAMA_HOME", "OLLAMA_MODELS_DIR"} and probe_id == "provider-probe":
        observed = _ollama_runtime(ctx)
        return _result(ctx, probe_id, observed["source"], observed)
    if variable == "SHERPA_VLM_OLLAMA_URL" and probe_id == "provider-probe":
        observed = _vlm_state(ctx)
        assert observed["provider"] == "ollama"
        endpoint = os.environ.get("SHERPA_VLM_OLLAMA_URL") or "http://127.0.0.1:11434"
        endpoint_probe = _ollama_runtime(ctx, endpoint_value=endpoint, model=observed["model"])
        measurements = {**observed, "configured_endpoint_probe": endpoint_probe}
        return _result(
            ctx,
            probe_id,
            "real vision conversion/usage and inference against the configured Ollama endpoint",
            measurements,
        )
    if variable == "SHERPA_VLM_TIMEOUT" and probe_id == "provider-duration":
        observed = _vlm_state(ctx)
        timeout_seconds = _positive_float("SHERPA_VLM_TIMEOUT", 180.0)
        raise AssertionError(
            "real VLM conversion and non-zero usage completed, but this fixture did not reach "
            f"the effective {timeout_seconds:g}s VLM budget; provider-duration cannot be "
            f"claimed from an untriggered timeout (provider={observed['provider']}, "
            f"usage_calls={observed['usage_calls']})"
        )
    if variable == "SHERPA_HEALTH_AI_TTL" and probe_id == "provider-request-count":
        observed = _ai_health_cache_state(ctx)
        return _result(ctx, probe_id, observed["source"], observed)
    if variable.startswith("SHERPA_HISTORY_"):
        measurements = _history_turns(ctx)
        return _result(
            ctx,
            probe_id,
            "two real provider turns correlated with conversation API and Postgres",
            measurements,
        )
    observed = _chat_turn(ctx)
    if probe_id in {"redacted-provider-request", "redacted-worker-request"}:
        observed = {**observed, "redaction": _secret_absent_from_service_logs(ctx, variable)}
        if variable == "OPENAI_CHAT_URL" and os.environ.get(variable):
            raise AssertionError(
                "a real provider answered and logs are redacted, but no captured transport "
                "record correlates the request to OPENAI_CHAT_URL; provider success alone is insufficient"
            )
    if probe_id in {"provider-probe", "ollama-probe", "gemini-probe", "bedrock-probe", "tls-probe"}:
        assert not observed["structured_error"], "real provider probe ended in a structured error"
        assert observed["provider"] and observed["input_tokens"] + observed["output_tokens"] > 0
        if variable in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        } and os.environ.get(variable):
            raise AssertionError(
                f"{variable} was present and a real provider answered, but no runner-owned proxy "
                "access log or transport correlation proves that the request traversed that "
                "proxy; provider success alone is not semantic proxy evidence"
            )
    if probe_id == "provider-request-count":
        assert observed["input_tokens"] + observed["output_tokens"] > 0
    if probe_id == "sse-node-count":
        cap = int(os.environ.get("SHERPA_AGENTIC_MAX_TURNS", "12"))
        limit_nodes = [row for row in observed["node_summaries"] if "上限" in row["label"] or "limit" in row["label"].lower()]
        assert observed["structured_error"] or limit_nodes, (
            "the real turn did not reach SHERPA_AGENTIC_MAX_TURNS; node_count<=limit does not demonstrate enforcement"
        )
        observed = {**observed, "configured_turn_cap": cap, "limit_nodes": limit_nodes}
    if probe_id == "evaluation-cycle-count":
        cycle_turns = _bounded_int("SHERPA_AGENTIC_EVAL_CYCLE_TURNS", 3, 1, 20)
        evaluation_nodes = [row for row in observed["node_summaries"] if row["label"] == "調査状況を評価"]
        assert evaluation_nodes, (
            "the real UI turn exposed no evaluation node; the configured research-cycle "
            "boundary is not observable through the current UI execution path"
        )
        observed = {
            **observed,
            "configured_evaluation_cycle_turns": cycle_turns,
            "evaluation_node_count": len(evaluation_nodes),
            "evaluation_nodes": evaluation_nodes,
        }
    if probe_id == "evidence-verification":
        raw = os.environ.get("SHERPA_AGENTIC_EVIDENCE_VERIFY")
        enabled = raw is None or raw.strip().lower() not in {"0", "false", "no"}
        methods = observed["evidence_verification_methods"]
        assert observed["evidence_packet_present"] and observed["evidence_row_count"] > 0, (
            "the real grounded answer has no Evidence Packet rows to prove verification behavior"
        )
        if enabled:
            assert all(isinstance(method, str) and method for method in methods), (
                "evidence verification is enabled but a committed Evidence Packet row has no verification method"
            )
        else:
            assert all(method is None for method in methods), (
                "evidence verification is explicitly disabled but a committed row still reports a verification method"
            )
        observed = {**observed, "evidence_verification_enabled": enabled}
    if probe_id == "sse-tool-count":
        cap = _bounded_int("SHERPA_AGENTIC_MAX_TOOLS_PER_TURN", 16, 1, 256)
        assert observed["tool_node_count"] <= cap, observed
        assert observed["tool_node_count"] == cap or any("上限" in row["label"] for row in observed["node_summaries"]), (
            "the real provider did not fill the per-turn tool cap; an under-limit success "
            "cannot demonstrate SHERPA_AGENTIC_MAX_TOOLS_PER_TURN"
        )
        observed = {**observed, "effective_tool_cap": cap, "limit_reached": True}
    if probe_id == "trace-result-size":
        cap = _bounded_int("SHERPA_AGENTIC_MAX_TOOL_RESULT_BYTES", 65536, 1024, 8 * 1024 * 1024)
        assert observed["tool_payload_max_bytes"] <= cap, observed
        assert observed["tool_payload_max_bytes"] >= max(1, cap - 8), (
            "the real tool result stayed below the configured cap; upper-bound enforcement was not exercised"
        )
        observed = {**observed, "effective_limit_bytes": cap}
    if probe_id == "trace-total-size":
        per_call = _bounded_int("SHERPA_AGENTIC_MAX_TOOL_RESULT_BYTES", 65536, 1024, 8 * 1024 * 1024)
        cap = _bounded_int(
            "SHERPA_AGENTIC_MAX_TOTAL_TOOL_RESULT_BYTES",
            per_call * 16,
            4096,
            64 * 1024 * 1024,
        )
        assert observed["tool_payload_total_bytes"] <= cap, observed
        assert observed["tool_payload_total_bytes"] >= max(1, cap - 8), (
            "the real aggregate tool payload stayed below the configured cap; total-bound enforcement was not exercised"
        )
        observed = {**observed, "effective_limit_bytes": cap}
    if probe_id in {"subagent-call-count", "subagent-step-count", "sse-tool-nodes"}:
        enabled = _bool_value("SHERPA_SUBAGENTS_ENABLED", False)
        if enabled:
            assert observed["subagent_node_count"] > 0, "subagent-enabled real turn produced no delegated execution node"
        else:
            assert observed["subagent_node_count"] == 0, "subagent-disabled real turn still produced a delegated execution node"
        if probe_id == "subagent-call-count":
            cap = _bounded_int("SHERPA_SUB_PLAN_MAX_CALLS", 24, 1, 500)
            assert observed["subagent_node_count"] >= cap, (
                "the real plan did not reach SHERPA_SUB_PLAN_MAX_CALLS; a lower count does not demonstrate the call ceiling"
            )
            observed = {**observed, "effective_call_cap": cap, "limit_reached": True}
        elif probe_id == "subagent-step-count":
            cap = _bounded_int("SHERPA_SUB_PLAN_MAX_STEPS", 3, 1, 100)
            assert observed["subagent_node_count"] >= cap, (
                "the real plan did not reach SHERPA_SUB_PLAN_MAX_STEPS; a lower count does not demonstrate the step ceiling"
            )
            observed = {**observed, "effective_step_cap": cap, "limit_reached": True}
    if probe_id in {"provider-duration", "turn-duration"}:
        timeout_name = "SHERPA_CODEX_TIMEOUT" if variable == "SHERPA_CODEX_TIMEOUT" else "SHERPA_LLM_TIMEOUT"
        default_timeout = 180.0 if "CODEX" in timeout_name else 60.0
        raw_timeout = os.environ.get(timeout_name)
        timeout_seconds = float(raw_timeout) if raw_timeout not in {None, ""} else default_timeout
        timeout_ms = int(timeout_seconds * 1000)
        assert observed["structured_error"] or observed["elapsed_ms"] <= timeout_ms + 15000
        observed = {**observed, "effective_timeout_ms": timeout_ms}
        if variable == timeout_name and not observed["structured_error"]:
            raise AssertionError(
                f"the real provider finished in {observed['elapsed_ms']}ms without reaching "
                f"the configured {timeout_ms}ms {timeout_name} boundary; an ordinary success "
                "cannot demonstrate timeout enforcement"
            )
    if probe_id == "author-duration":
        raw_timeout = os.environ.get("SHERPA_CODEX_TIMEOUT_AUTHOR")
        timeout_ms = int((float(raw_timeout) if raw_timeout not in {None, ""} else 600.0) * 1000)
        assert observed["structured_error"] or observed["elapsed_ms"] <= timeout_ms + 15000
        observed = {**observed, "effective_timeout_ms": timeout_ms}
        if not observed["structured_error"]:
            raise AssertionError(
                "the real author turn completed without reaching SHERPA_CODEX_TIMEOUT_AUTHOR; "
                "ordinary duration below the budget does not demonstrate timeout enforcement"
            )
    if probe_id == "sse-timing":
        pace = float(os.environ.get("SHERPA_STREAM_PACE", "0.35") or 0)
        assert pace >= 0, f"SHERPA_STREAM_PACE must not be negative: {pace}"
        interval_ms = observed["paced_tool_pair_receive_ms"]
        assert interval_ms is not None, (
            "the real troubleshoot turn did not emit the consecutive graph/doc tool-done pair; "
            "whole-turn duration is not accepted as stream-pace evidence"
        )
        configured_ms = pace * 1000
        # Monotonic receipt timestamps have millisecond resolution and traverse
        # the local HTTP stack.  Keep the tolerance narrow enough that the
        # matrix's 50ms value cannot be mistaken for the 350ms default.
        tolerance_ms = max(20.0, configured_ms * 0.30)
        if pace == 0:
            assert interval_ms <= 50, {
                "configured_pace_ms": configured_ms,
                "tool_done_receive_interval_ms": interval_ms,
            }
        else:
            assert configured_ms - tolerance_ms <= interval_ms <= configured_ms + tolerance_ms, {
                "configured_pace_ms": configured_ms,
                "tolerance_ms": tolerance_ms,
                "tool_done_receive_interval_ms": interval_ms,
            }
        observed = {
            **observed,
            "configured_pace_seconds": pace,
            "configured_pace_ms": configured_ms,
            "pace_receive_tolerance_ms": tolerance_ms,
            "pace_effect_observed_on": "consecutive real SSE tool-done events",
        }
    if probe_id == "ui-trace-order":
        assert observed["ui_node_count"] == observed["persisted_trace_count"], observed
        assert observed["ui_node_order"] == observed["sse_node_order"], observed
    if probe_id == "postgres-trace":
        expected_version = 2 if _bool_value("SHERPA_EXEC_EVENT_V2") else 1
        assert observed["trace_version"] == expected_version, observed
        observed = {**observed, "expected_trace_version": expected_version}
    if probe_id in {"tool-nodes", "author-trace"}:
        assert observed["tool_node_count"] > 0 and observed["persisted_trace_count"] > 0
    if probe_id == "tool-nodes" and variable == "SHERPA_CODEX_MCP":
        labels = [row["label"] for row in observed["node_summaries"] if row["kind"] == "tool"]
        mcp_labels = [
            label
            for label in labels
            if label
            in {
                "資料を検索（grep）",
                "資料を検索（全文）",
                "該当箇所を精読",
                "資料の一覧を確認",
                "関係グラフをたどる",
            }
            or label.startswith("ツール:")
        ]
        enabled = os.environ.get("SHERPA_CODEX_MCP", "1").strip().lower() not in {"", "0", "false", "no", "off"}
        assert bool(mcp_labels) is enabled, {
            "mcp_enabled": enabled,
            "tool_labels": labels,
            "identified_mcp_labels": mcp_labels,
        }
        observed = {**observed, "mcp_enabled": enabled, "mcp_tool_labels": mcp_labels}
    if probe_id == "tool-nodes" and variable == "SHERPA_ALLOW_WEB_SEARCH":
        enabled = _bool_value("SHERPA_ALLOW_WEB_SEARCH")
        web_nodes = [row for row in observed["node_summaries"] if "web" in row["label"].lower() or "Web検索" in row["label"]]
        if enabled:
            assert web_nodes, (
                "web search was enabled but the real turn exposed no identifiable web-search "
                "execution node; a generic knowledge tool is not proof"
            )
        else:
            assert not web_nodes, "web search was disabled but a web-search node was emitted"
        observed = {**observed, "web_search_enabled": enabled, "web_nodes": web_nodes}
    if probe_id == "author-trace" and variable in {
        "SHERPA_CODEX_REASONING_AUTHOR",
        "SHERPA_MARP_BIN",
    }:
        raise AssertionError(
            f"the real author turn emitted generic tool nodes, but those nodes do not prove "
            f"the concrete {variable} effect (reasoning selection or Marp artifact process)"
        )
    if probe_id == "tls-probe" and os.environ.get(variable):
        raise AssertionError(
            f"the real provider answered with {variable} configured, but no captured TLS peer/CA "
            "correlation proves that this certificate path was consumed"
        )
    if probe_id == "turn-slot-release":
        assert observed["turn_slot_released"] is True
        if variable == "SHERPA_CODEX_TIMEOUT" and not observed["structured_error"]:
            raise AssertionError("the slot was released after a normal turn, but the configured Codex timeout was not triggered")
    if probe_id == "chat-error":
        timeout_variable = variable in {
            "SHERPA_LLM_TIMEOUT",
            "SHERPA_CODEX_TIMEOUT",
            "SHERPA_CODEX_TIMEOUT_AUTHOR",
        }
        if timeout_variable:
            assert observed["structured_error"], (
                "the configured timeout was not triggered; a normal answer cannot demonstrate the chat error contract"
            )
        elif variable in {
            "SHERPA_AGENTIC_MAX_TURNS",
            "SHERPA_AGENTIC_MAX_TOOLS_PER_TURN",
            "SHERPA_SUB_PLAN_MAX_CALLS",
        }:
            limit_nodes = [row for row in observed["node_summaries"] if "上限" in row["label"]]
            assert observed["structured_error"] or limit_nodes, f"the real turn did not reach {variable}; no limit error was observable"
    return _result(ctx, probe_id, observed["source"], observed)


def _es_request(ctx, method: str, path: str, body=None) -> tuple[dict, int]:
    endpoint = os.environ.get("ES_URL", "").rstrip("/")
    parsed = urlsplit(endpoint)
    assert parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }, "Elasticsearch content probe requires the runner-owned loopback endpoint"
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        endpoint + path,
        data=encoded,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    started = time.monotonic()
    status = 0
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    ctx.evidence.record_api(method=method, url=endpoint + path, status=status, elapsed_ms=elapsed_ms)
    assert status == 200, f"Elasticsearch {method} {path} returned {status}: {payload}"
    assert isinstance(payload, dict)
    return payload, elapsed_ms


def _world_index_name(world_id: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]", "-", world_id.lower())[:40].strip("-._") or "w"
    return f"sherpa-kb-{slug}-{hashlib.sha1(world_id.encode()).hexdigest()[:10]}"


def _postgres_rows(ctx, query: str, params=()) -> list[dict]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for content semantic evidence") from exc
    with psycopg.connect(ctx.config.database_url, row_factory=dict_row, connect_timeout=5) as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _world_state(ctx) -> dict:
    def collect() -> dict:
        world_id = ctx.fixture("real_world")
        status = ctx.api.get_json(
            f"/worlds/{world_id}/status",
            save_as="state/content-semantics-world-status.json",
        )
        preview = ctx.api.get_json(
            LiveApi.query("/ingest/preview", world=world_id),
            save_as="state/content-semantics-ingest-preview.json",
        )
        ledger = ctx.api.get_json(
            LiveApi.query("/documents", world=world_id),
            save_as="state/content-semantics-documents.json",
        )
        search_started = time.monotonic()
        search = ctx.api.get_json(
            LiveApi.query(
                "/admin/es/search",
                world=world_id,
                query="SHERPA-LIVE-ALPHA-927",
                k=50,
            ),
            save_as="state/content-semantics-search.json",
        )
        search_ms = int((time.monotonic() - search_started) * 1000)
        hits = search.get("hits") or []
        assert hits and any("SHERPA-LIVE-ALPHA-927" in str(row) for row in hits)
        graph_started = time.monotonic()
        graph_response = ctx.api.request("GET", LiveApi.query("/graph", world=world_id), expected=200)
        graph_ms = int((time.monotonic() - graph_started) * 1000)
        graph = graph_response.json()
        full_graph = ctx.api.get_json(LiveApi.query("/graph", world=world_id, limit=0))
        assert graph.get("nodes") and full_graph.get("nodes")
        index = _world_index_name(world_id)
        identity, identity_ms = _es_request(ctx, "GET", "/")
        mapping, mapping_ms = _es_request(ctx, "GET", f"/{index}/_mapping")
        es_search, es_search_ms = _es_request(
            ctx,
            "POST",
            f"/{index}/_search",
            {"size": 100, "query": {"match_all": {}}},
        )
        mapping_entry = next(iter(mapping.values()))
        mappings = mapping_entry.get("mappings") or {}
        properties = mappings.get("properties") or {}
        meta = mappings.get("_meta") or {}
        es_hits = (es_search.get("hits") or {}).get("hits") or []
        assert es_hits, "real World Elasticsearch index contains no source documents"
        derived_root = Path(os.environ["SHERPA_DERIVED_DIR"]) / world_id / "md"
        assert_no_mount_targets(derived_root)
        derived_candidates = sorted(derived_root.rglob("*.md"))
        assert not any(path.is_symlink() for path in derived_candidates), "real World derived Markdown contains a symlink"
        derived_files = [path for path in derived_candidates if path.is_file()]
        assert derived_files, "real World has no derived Markdown outputs"
        required = {
            "office/tax-evidence.docx": "ooxml",
            "office/tax-cases.xlsx": "ooxml",
            "office/nightly-operations.pptx": "ooxml",
            "media/text-evidence.pdf": "pdf_text",
            "legacy/legacy-note.doc": "ooxml",
        }
        documents = {str(row.get("name") or ""): row for row in preview.get("documents") or []}
        for rel_path, method in required.items():
            row = documents.get(rel_path) or {}
            assert row.get("state") == "ready", {rel_path: row}
            assert (row.get("provenance") or {}).get("method") == method, {rel_path: row}
        runs = _postgres_rows(
            ctx,
            "SELECT id,status,source_doc_ids,extraction_snapshot,published_snapshot,created_at "
            "FROM ingest_runs WHERE version=%s ORDER BY id",
            (world_id,),
        )
        assert runs and runs[-1]["status"] == "succeeded", runs
        return {
            "source": "real World UI/API, Elasticsearch, Neo4j-backed graph, direct Postgres and derived files",
            "world_id_sha256": _sha256(world_id),
            "status": status,
            "preview_documents": documents,
            "ledger_document_count": len(ledger.get("documents") or []),
            "search_hits": hits,
            "search_elapsed_ms": search_ms,
            "graph": graph,
            "full_graph": full_graph,
            "graph_elapsed_ms": graph_ms,
            "graph_etag": graph_response.headers.get("ETag"),
            "es_identity": {
                "cluster_name": identity.get("cluster_name"),
                "version": (identity.get("version") or {}).get("number"),
                "elapsed_ms": identity_ms,
            },
            "es_mapping_elapsed_ms": mapping_ms,
            "es_search_elapsed_ms": es_search_ms,
            "es_properties": properties,
            "es_meta": meta,
            "es_sources": [row.get("_source") or {} for row in es_hits],
            "derived_files": [
                {
                    "relative": path.relative_to(derived_root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path.read_bytes()),
                }
                for path in derived_files
            ],
            "ingest_runs": runs,
        }

    return _cache(ctx, "content_semantics_world", collect)


def _graph_limit() -> int:
    raw = os.environ.get("SHERPA_GRAPH_NODE_LIMIT", "100")
    try:
        parsed = int(raw)
    except ValueError:
        return 100
    return parsed if parsed > 0 else 100


def _qa_citations(ctx, world_id: str, question: str) -> list[dict]:
    payload = ctx.api.post_json("/qa/run", {"world": world_id, "question": question, "scope_paths": []})
    citations = payload.get("citations") or []
    assert isinstance(citations, list)
    return [row for row in citations if isinstance(row, dict)]


def _grep_source_state(ctx) -> dict:
    def collect() -> dict:
        world_id = ctx.fixture("real_world")
        derived_root = Path(os.environ["SHERPA_DERIVED_DIR"]) / world_id / "md"
        assert_no_mount_targets(derived_root)
        pairs = []
        for rag_path in sorted(derived_root.rglob("*.rag.md")):
            assert not rag_path.is_symlink(), f"RAG Markdown must not be a symlink: {rag_path}"
            relative = rag_path.relative_to(derived_root).as_posix()
            origin_rel = relative[: -len(".rag.md")]
            legacy_path = derived_root / f"{origin_rel}.md"
            assert not legacy_path.is_symlink(), f"legacy Markdown must not be a symlink: {legacy_path}"
            if not legacy_path.is_file():
                continue
            rag_text = rag_path.read_text(encoding="utf-8", errors="replace")
            legacy_text = legacy_path.read_text(encoding="utf-8", errors="replace")
            if rag_text != legacy_text:
                pairs.append((origin_rel, rag_path, legacy_path, rag_text, legacy_text))
        assert pairs, "real World has no distinct rag/legacy Markdown pair"
        enabled = _bool_value("SHERPA_SEARCH_RAG_GREP")
        mismatches = []
        for origin_rel, rag_path, legacy_path, rag_text, legacy_text in pairs:
            rag_tokens = set(re.findall(r"SHERPA-[A-Z0-9-]{6,}", rag_text))
            legacy_tokens = set(re.findall(r"SHERPA-[A-Z0-9-]{6,}", legacy_text))
            candidates = sorted(rag_tokens & legacy_tokens, key=lambda value: (-len(value), value))
            if not candidates:
                rag_words = set(re.findall(r"[A-Za-z][A-Za-z0-9_-]{7,}", rag_text))
                legacy_words = set(re.findall(r"[A-Za-z][A-Za-z0-9_-]{7,}", legacy_text))
                candidates = sorted(rag_words & legacy_words, key=lambda value: (-len(value), value))
            for query in candidates[:20]:
                citations = _qa_citations(ctx, world_id, query)
                for citation in citations:
                    if str(citation.get("doc_id") or "") != origin_rel:
                        continue
                    quote = str(citation.get("quote") or "")
                    if not quote:
                        continue
                    in_rag = quote in rag_text
                    in_legacy = quote in legacy_text
                    selected = "rag" if in_rag and not in_legacy else ("legacy" if in_legacy and not in_rag else "ambiguous")
                    expected = "rag" if enabled else "legacy"
                    if selected == expected:
                        return {
                            "source": ("real /qa/run grep result correlated to the uniquely matching physical rag/legacy derived Markdown"),
                            "world_id_sha256": _sha256(world_id),
                            "rag_grep_enabled": enabled,
                            "expected_source": expected,
                            "selected_source": selected,
                            "origin_rel_sha256": _sha256(origin_rel),
                            "query_sha256": _sha256(query),
                            "quote_bytes": len(quote.encode("utf-8")),
                            "quote_sha256": _sha256(quote),
                            "rag_file_sha256": _sha256(rag_path.read_bytes()),
                            "legacy_file_sha256": _sha256(legacy_path.read_bytes()),
                            "candidate_pair_count": len(pairs),
                        }
                    mismatches.append({"origin": _sha256(origin_rel), "query": _sha256(query), "selected": selected})
        raise AssertionError(
            "real /qa/run produced no quote that uniquely demonstrates the configured "
            f"SHERPA_SEARCH_RAG_GREP source (enabled={enabled}, attempts={len(mismatches)})"
        )

    return _cache(ctx, "content_semantics_grep_source", collect)


def _grep_hit_limit_state(ctx) -> dict:
    def collect() -> dict:
        cap = _bounded_int("SHERPA_GREP_HIT_TEXT_MAX_BYTES", 64 * 1024, 1024, 8 * 1024 * 1024)
        marker = "SHERPA-GREP-LIMIT-" + _sha256(ctx.config.run_id)[:12].upper()
        prefix = f"# Real grep byte-limit evidence\n\n{marker}\n"
        line = "Z" * 100 + "\n"
        repeats = (cap + 4096 - len(prefix.encode("utf-8"))) // len(line) + 2
        raw = (prefix + line * repeats).encode("utf-8")
        assert len(raw) > cap
        probe = _create_probe_world(ctx, "grep-hit-limit", {"limit-evidence.md": raw})
        citations = _qa_citations(ctx, probe["world_id"], marker)
        matching = [row for row in citations if row.get("doc_id") == "limit-evidence.md"]
        assert matching, "real /qa/run returned no hit for the limit-crossing document"
        quote = str(matching[0].get("quote") or "")
        quote_bytes = len(quote.encode("utf-8"))
        assert quote_bytes <= cap, {
            "configured_cap": cap,
            "quote_bytes": quote_bytes,
        }
        assert quote_bytes >= cap - 4, (
            "the generated real grep section crossed the cap, but the returned quote did not "
            "reach it; clipping enforcement cannot be distinguished"
        )
        return {
            "source": ("runner-owned real World with an over-cap Markdown section and live /qa/run grep"),
            "world_id_sha256": probe["world_id_sha256"],
            "registration_elapsed_ms": probe["registration_elapsed_ms"],
            "configured_limit_bytes": cap,
            "source_section_bytes": len(raw),
            "returned_quote_bytes": quote_bytes,
            "returned_quote_sha256": _sha256(quote),
            "limit_reached": True,
        }

    return _cache(ctx, "content_semantics_grep_hit_limit", collect)


def _poll_state(ctx) -> dict:
    def collect() -> dict:
        poll_seconds = int(os.environ.get("SHERPA_POLL_SECONDS", "0") or 0)
        relative = "poll-evidence.md"
        probe = _create_probe_world(
            ctx,
            "folder-poll",
            {relative: b"# Poll evidence\n\nversion=before\n"},
            mutable=frozenset({relative}),
        )
        world_id = probe["world_id"]
        before = _postgres_rows(
            ctx,
            "SELECT id,status,created_at FROM ingest_runs WHERE version=%s ORDER BY id",
            (world_id,),
        )
        assert before and before[-1]["status"] == "succeeded"
        baseline_id = int(before[-1]["id"])
        source = probe["source_root"] / relative
        source_metadata = source.lstat()
        assert not source.is_symlink() and source_metadata.st_nlink == 1, "poll source failed filesystem boundaries"
        started = time.monotonic()
        write_private_bytes_atomic(source, b"# Poll evidence\n\nversion=after\nSHERPA-POLL-OBSERVED\n")
        chmod_path_no_follow(source, 0o600, require_owner_uid=os.geteuid())
        observed_run = None
        deadline = started + (max(15.0, poll_seconds * 4.0 + 10.0) if poll_seconds > 0 else 2.0)
        while time.monotonic() < deadline:
            rows = _postgres_rows(
                ctx,
                "SELECT id,status,created_at FROM ingest_runs WHERE version=%s AND id>%s ORDER BY id",
                (world_id, baseline_id),
            )
            succeeded = [row for row in rows if row.get("status") == "succeeded"]
            if succeeded:
                observed_run = succeeded[-1]
                break
            time.sleep(0.25)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if poll_seconds > 0:
            assert observed_run is not None, f"folder source changed but no automatic ingest run appeared within {elapsed_ms}ms"
            assert elapsed_ms >= max(0, poll_seconds * 1000 - 500), "automatic refresh happened before the configured poll interval"
        else:
            assert observed_run is None, "SHERPA_POLL_SECONDS disabled automatic polling but a new ingest run appeared"
        return {
            "source": ("mutable runner-owned real World, automatic poll interval and direct ingest_runs rows"),
            "world_id_sha256": probe["world_id_sha256"],
            "configured_poll_seconds": poll_seconds,
            "baseline_ingest_run_id_sha256": _sha256(str(baseline_id)),
            "automatic_ingest_observed": observed_run is not None,
            "automatic_ingest_run_id_sha256": (_sha256(str(observed_run["id"])) if observed_run else None),
            "elapsed_after_source_change_ms": elapsed_ms,
        }

    return _cache(ctx, "content_semantics_folder_poll", collect)


def _vlm_image_limit_state(ctx) -> dict:
    def collect() -> dict:
        effective_mb = _positive_float("SHERPA_VLM_MAX_IMAGE_MB", 20.0)
        limit_bytes = int(effective_mb * 1024 * 1024)
        assert 1 <= limit_bytes <= 64 * 1024 * 1024, "VLM image limit is outside the bounded semantic-test payload range"
        fixture = ctx.config.world_path / "media/vlm-evidence.bmp"
        assert fixture.is_file()
        small = fixture.read_bytes()
        assert len(small) < limit_bytes, "deterministic accepted image is not below the effective VLM limit"
        oversized = small + b"\x00" * (limit_bytes + 1 - len(small))
        checkpoint_rows = _postgres_rows(ctx, "SELECT COALESCE(max(id),0) AS id FROM usage_events WHERE kind='vlm'")
        checkpoint = int(checkpoint_rows[0]["id"])
        probe = _create_probe_world(
            ctx,
            "vlm-image-limit",
            {"accepted.bmp": small, "rejected.bmp": oversized},
        )
        preview = ctx.api.get_json(
            LiveApi.query("/ingest/preview", world=probe["world_id"]),
            save_as="state/content-vlm-image-limit-preview.json",
        )
        documents = {str(row.get("name") or ""): row for row in preview.get("documents") or []}
        accepted = documents.get("accepted.bmp") or {}
        rejected = documents.get("rejected.bmp") or {}
        accepted_method = str((accepted.get("provenance") or {}).get("method") or "")
        rejected_method = str((rejected.get("provenance") or {}).get("method") or "")
        assert accepted.get("state") == "ready" and accepted_method == "vision", (
            "below-limit image did not execute the real vision provider"
        )
        assert rejected_method != "vision", "limit+1-byte image was sent through the real vision provider"
        usage = _postgres_rows(
            ctx,
            "SELECT id,provider,model,input_tokens,cached_input_tokens,output_tokens,"
            "reasoning_output_tokens,calls FROM usage_events WHERE kind='vlm' AND id>%s ORDER BY id",
            (checkpoint,),
        )
        assert usage and sum(int(row.get("calls") or 0) for row in usage) == 1, (
            "VLM usage does not show exactly one accepted request and zero over-limit requests"
        )
        token_total = sum(
            int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0) + int(row.get("reasoning_output_tokens") or 0)
            for row in usage
        )
        assert token_total > 0
        for row in usage:
            ctx.evidence.record_usage_event(
                row,
                turn_id=f"content-vlm-limit:{row['id']}",
                operation="environment-vlm-image-limit",
            )
        return {
            "source": ("real World with below-limit and limit+1 images, conversion provenance and non-zero VLM usage"),
            "world_id_sha256": probe["world_id_sha256"],
            "registration_elapsed_ms": probe["registration_elapsed_ms"],
            "effective_limit_mb": effective_mb,
            "effective_limit_bytes": limit_bytes,
            "accepted_size_bytes": len(small),
            "rejected_size_bytes": len(oversized),
            "accepted_method": accepted_method,
            "rejected_method": rejected_method,
            "usage_calls": sum(int(row.get("calls") or 0) for row in usage),
            "usage_tokens": token_total,
            "rejection_observed": True,
        }

    return _cache(ctx, "content_semantics_vlm_image_limit", collect)


def _world_semantics(ctx, probe_id: str) -> dict:
    if _variable(ctx) == "SHERPA_POLL_SECONDS" and probe_id in {
        "refresh-time",
        "ingest-run",
    }:
        observed = _poll_state(ctx)
        return _result(ctx, probe_id, observed["source"], observed)
    if probe_id == "search-source":
        observed = _grep_source_state(ctx)
        return _result(ctx, probe_id, observed["source"], observed)
    if probe_id == "search-hit-size":
        observed = _grep_hit_limit_state(ctx)
        return _result(ctx, probe_id, observed["source"], observed)
    state = _world_state(ctx)
    variable = _variable(ctx)
    measurements: dict = {
        "world_id_sha256": state["world_id_sha256"],
        "indexed": int(state["status"].get("indexed") or 0),
        "document_count": state["ledger_document_count"],
        "search_hit_count": len(state["search_hits"]),
        "search_elapsed_ms": state["search_elapsed_ms"],
        "graph_node_count": len(state["graph"].get("nodes") or []),
        "graph_full_node_count": len(state["full_graph"].get("nodes") or []),
        "graph_elapsed_ms": state["graph_elapsed_ms"],
        "derived_file_count": len(state["derived_files"]),
        "latest_ingest_status": state["ingest_runs"][-1]["status"],
    }
    if probe_id == "elasticsearch-identity":
        assert state["es_identity"]["cluster_name"] and state["es_identity"]["version"]
        measurements["elasticsearch"] = state["es_identity"]
        endpoint = urlsplit(os.environ["ES_URL"])
        if variable == "SHERPA_ES_PORT":
            expected_port = int(os.environ["SHERPA_ES_PORT"])
            assert endpoint.port == expected_port
            measurements["configured_port_matched_endpoint"] = True
        elif variable == "ES_URL":
            measurements["endpoint_origin_sha256"] = _sha256(f"{endpoint.scheme}://{endpoint.netloc}")
    elif probe_id == "elasticsearch-mapping":
        assert state["es_properties"].get("doc_id", {}).get("type") == "keyword"
        expected_version = os.environ.get("ES_MAPPING_VERSION") or "3"
        assert str(state["es_meta"].get("mapping_version")) == expected_version
        if variable == "ES_MAPPING_VERSION" and os.environ.get(variable) is not None:
            raise AssertionError(
                "ES_MAPPING_VERSION is explicitly set but the selected value cannot be "
                "distinguished from the product's fixed mapping version by a real reindex"
            )
        measurements.update(
            {
                "mapping_version": state["es_meta"].get("mapping_version"),
                "property_count": len(state["es_properties"]),
                "mapping_elapsed_ms": state["es_mapping_elapsed_ms"],
            }
        )
    elif probe_id == "elasticsearch-source":
        mode = "rag" if _bool_value("SHERPA_SEARCH_RAG_ES") else "legacy"
        assert state["es_meta"].get("search_chunk_mode") == mode
        rag_rows = [row for row in state["es_sources"] if row.get("chunk_id")]
        if mode == "rag":
            assert rag_rows, "RAG Elasticsearch mode produced no record-level chunks"
        measurements.update(
            {
                "search_chunk_mode": mode,
                "indexed_source_count": len(state["es_sources"]),
                "rag_source_count": len(rag_rows),
            }
        )
    elif probe_id == "elasticsearch-vector-field":
        present = "embedding" in state["es_properties"]
        source_vectors = sum("embedding" in row for row in state["es_sources"])
        disabled = bool(os.environ.get("SHERPA_DISABLE_EMBED"))
        if disabled:
            assert not present and source_vectors == 0
        elif variable in {"OPENAI_EMBED_MODEL", "OPENAI_EMBED_URL"}:
            assert present and source_vectors > 0, "configured real embedding path produced no vector field or source vectors"
            model = os.environ.get("OPENAI_EMBED_MODEL") or "text-embedding-3-small"
            assert state["es_meta"].get("embed_model") == model
        measurements.update(
            {
                "vector_mapping_present": present,
                "source_vector_count": source_vectors,
                "embed_provider": state["es_meta"].get("embed_provider"),
                "embed_model": state["es_meta"].get("embed_model"),
                "embedding_disabled_by_presence": disabled,
            }
        )
    elif probe_id == "elasticsearch-document":
        assert all(row.get("doc_id") and row.get("text") for row in state["es_sources"])
        measurements["source_document_count"] = len(state["es_sources"])
    elif probe_id in {"graph-node-count", "truncated-flag"} and variable == "SHERPA_GRAPH_NODE_LIMIT":
        limit = _graph_limit()
        assert state["graph_etag"] and state["graph_etag"].endswith(f'.{limit}"')
        assert len(state["graph"].get("nodes") or []) <= limit
        assert len(state["full_graph"].get("nodes") or []) > limit, (
            "the real graph did not cross SHERPA_GRAPH_NODE_LIMIT; ETag/config agreement alone does not demonstrate truncation"
        )
        measurements.update(
            {
                "effective_node_limit": limit,
                "etag_limit_token_matched": True,
                "truncated": len(state["full_graph"].get("nodes") or []) > limit,
            }
        )
    elif probe_id in {"graph-row-count", "truncated-flag"} and variable == "SHERPA_NEO4J_MAX_ROWS":
        limit = _bounded_int("SHERPA_NEO4J_MAX_ROWS", 10000, 100, 1_000_000)
        observed_rows = len(state["full_graph"].get("nodes") or [])
        assert observed_rows >= limit, (
            "the deterministic World does not reach SHERPA_NEO4J_MAX_ROWS, so the real "
            "Neo4j emergency ceiling cannot be claimed as exercised"
        )
        measurements.update(
            {
                "effective_emergency_row_limit": limit,
                "observed_graph_rows": observed_rows,
                "limit_reached": observed_rows >= limit,
            }
        )
    elif probe_id in {"query-duration", "graph-error"}:
        timeout = _bounded_int("SHERPA_NEO4J_QUERY_TIMEOUT_S", 30, 1, 600)
        assert state["graph_elapsed_ms"] <= timeout * 1000 + 5000
        raise AssertionError(
            f"the real graph query completed in {state['graph_elapsed_ms']}ms without reaching "
            f"the {timeout}s Neo4j timeout; an ordinary success cannot demonstrate timeout handling"
        )
    elif probe_id in {"derived-markdown", "selected-arm", "conversion-result"}:
        required = {
            "office/tax-evidence.docx": "ooxml",
            "office/tax-cases.xlsx": "ooxml",
            "office/nightly-operations.pptx": "ooxml",
            "media/text-evidence.pdf": "pdf_text",
            "legacy/legacy-note.doc": "ooxml",
        }
        methods = {path: (state["preview_documents"][path].get("provenance") or {}).get("method") for path in required}
        assert methods == required
        assert all(row["bytes"] > 0 for row in state["derived_files"])
        if variable == "SHERPA_PDF_TEXT_MIN_CHARS":
            raise AssertionError(
                "SHERPA_PDF_TEXT_MIN_CHARS changes only the product's internal good/sparse "
                "diagnostic; both paths intentionally select pdf_text and no UI/API/DB/file "
                "effect distinguishes the configured threshold"
            )
        legacy_provenance = state["preview_documents"]["legacy/legacy-note.doc"].get("provenance") or {}
        if variable == "SHERPA_SOFFICE_BIN":
            assert legacy_provenance.get("legacy_backend") == "libreoffice", (
                "SHERPA_SOFFICE_BIN was not exercised by the real legacy conversion"
            )
        if variable in {
            "SHERPA_OFFICE_COM_URL",
            "SHERPA_OFFICE_COM_TOKEN",
            "SHERPA_OFFICE_TRANSFER_MODE",
            "SHERPA_POWERSHELL_BIN",
        }:
            assert legacy_provenance.get("legacy_backend") == "office_com", f"{variable} was not exercised by the real legacy conversion"
        measurements.update(
            {
                "selected_methods": methods,
                "derived_files": state["derived_files"],
                "legacy_backend": legacy_provenance.get("legacy_backend"),
            }
        )
    elif probe_id in {"ingest-result", "ingest-run"}:
        assert state["ingest_runs"][-1]["published_snapshot"]
        measurements.update(
            {
                "ingest_run_count": len(state["ingest_runs"]),
                "published_snapshot_present": True,
            }
        )
        if variable == "OPENAI_EMBED_URL":
            usage = _postgres_rows(
                ctx,
                "SELECT id,provider,model,input_tokens,output_tokens,calls FROM usage_events WHERE kind='embed' AND world=%s ORDER BY id",
                (ctx.fixture("real_world"),),
            )
            assert usage and sum(int(row.get("calls") or 0) for row in usage) > 0
            assert "embedding" in state["es_properties"]
            measurements.update(
                {
                    "embed_usage_rows": len(usage),
                    "embed_usage_calls": sum(int(row.get("calls") or 0) for row in usage),
                    "vector_mapping_present": True,
                }
            )
    elif probe_id == "refresh-time":
        world_id = ctx.fixture("real_world")
        started = time.monotonic()
        refreshed = ctx.api.post_json(f"/worlds/{world_id}/refresh")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        poll_seconds = int(os.environ.get("SHERPA_POLL_SECONDS", "0") or 0)
        assert refreshed.get("ok") is True
        measurements.update({"explicit_refresh_elapsed_ms": elapsed_ms, "configured_poll_seconds": poll_seconds})
    elif probe_id == "conversion-duration":
        runs = state["ingest_runs"]
        assert runs[-1].get("created_at") is not None
        raise AssertionError(
            "the real legacy conversion succeeded, but ingest_runs has no per-document finish "
            "timestamp and the workload did not reach SHERPA_LEGACY_TIMEOUT; created_at alone "
            "is not timeout enforcement evidence"
        )
    return _result(ctx, probe_id, state["source"], measurements)


def _ocr_container_ids() -> list[str]:
    project = os.environ.get("COMPOSE_PROJECT_NAME") or os.environ.get("SHERPA_COMPOSE_PROJECT")
    assert project and project.startswith("sherpa-ui-automation-")
    verify_local_docker_environment(dict(os.environ))
    listed = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=ocr-worker",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert listed.returncode == 0, listed.stderr
    return [line.strip() for line in listed.stdout.splitlines() if line.strip()]


def _ocr_disabled_state(ctx) -> dict:
    world_id = ctx.fixture("real_world")
    jobs = _postgres_rows(
        ctx,
        "SELECT id,status,source_rel_path FROM ocr_jobs WHERE world=%s ORDER BY id",
        (world_id,),
    )
    refresh_runs = _postgres_rows(
        ctx,
        "SELECT id,status FROM ocr_refresh_runs WHERE world=%s ORDER BY id",
        (world_id,),
    )
    containers = _ocr_container_ids()
    assert not jobs and not refresh_runs, "OCR-disabled profile persisted OCR jobs or refresh runs"
    assert not containers, "OCR-disabled profile still runs an OCR worker container"
    return {
        "source": "direct OCR queue Postgres rows and runner-owned Docker service inspection",
        "world_id_sha256": _sha256(world_id),
        "ocr_enabled": False,
        "job_count": 0,
        "refresh_run_count": 0,
        "worker_container_count": 0,
    }


def _observation_state(ctx) -> dict:
    def collect() -> dict:
        world_id = ctx.fixture("real_world")
        snapshot = wait_for_ingestion_database_snapshot(
            ctx.config.database_url,
            world_id,
            ctx.evidence,
            timeout_seconds=max(ctx.config.timeout_ms / 1000, 180),
        )
        jobs = snapshot.get("ocr_jobs") or []
        succeeded = [row for row in jobs if row.get("status") == "succeeded"]
        assert succeeded, "real OCR pipeline has no succeeded jobs"
        inputs = []
        providers = set()
        models = set()
        observations = 0
        for row in succeeded:
            payload = row.get("result_payload") or {}
            assert isinstance(payload, dict)
            assert row.get("result_observation_set_hash") and row.get("artifact_published") is True
            providers.add(str(payload.get("provider") or ""))
            models.add(str(payload.get("model") or ""))
            observations += len(payload.get("observations") or [])
            inputs.extend(payload.get("inputs") or [])
        assert all(providers) and all(models)
        pixel_sizes = [
            item.get("pixel_size")
            for item in inputs
            if isinstance(item, dict) and isinstance(item.get("pixel_size"), list) and len(item["pixel_size"]) == 2
        ]
        page_inputs = [item for item in inputs if isinstance(item, dict) and item.get("input_kind") == "page_render"]
        job_durations_ms = [
            int((row["finished_at"] - row["created_at"]).total_seconds() * 1000)
            for row in succeeded
            if row.get("finished_at") is not None and row.get("created_at") is not None
        ]
        container_ids = _ocr_container_ids()
        assert len(container_ids) == 1, f"expected one real OCR worker container, got {container_ids}"
        verify_local_docker_environment(dict(os.environ))
        inspected = subprocess.run(
            ["docker", "inspect", container_ids[0]],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert inspected.returncode == 0
        container = json.loads(inspected.stdout)[0]
        config = container.get("Config") or {}
        host = container.get("HostConfig") or {}
        return {
            "source": "real OCR job/observation Postgres rows and Docker OCR worker inspection",
            "world_id_sha256": _sha256(world_id),
            "job_count": len(jobs),
            "succeeded_job_count": len(succeeded),
            "observation_count": observations,
            "providers": sorted(providers),
            "models": sorted(models),
            "pixel_sizes": pixel_sizes,
            "page_render_count": len(page_inputs),
            "job_durations_ms": job_durations_ms,
            "container_id_sha256": _sha256(container_ids[0]),
            "container_user": str(config.get("User") or ""),
            "container_memory_bytes": int(host.get("Memory") or 0),
        }

    return _cache(ctx, "content_semantics_observation", collect)


def _memory_bytes(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*", value, re.I)
    assert match, f"invalid container memory contract: {value!r}"
    scale = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    return int(float(match.group(1)) * scale[match.group(2).lower()])


def _observation_semantics(ctx, probe_id: str) -> dict:
    variable = _variable(ctx)
    if variable == "SHERPA_VLM_OLLAMA_URL" and probe_id == "observation":
        state = _vlm_state(ctx)
        return _result(ctx, probe_id, state["source"], state)
    if variable == "SHERPA_OCR_ENABLED" and not _bool_value("SHERPA_OCR_ENABLED"):
        state = _ocr_disabled_state(ctx)
        return _result(ctx, probe_id, state["source"], state)
    state = _observation_state(ctx)
    measurements = dict(state)
    measurements.pop("source", None)
    if probe_id == "processed-page-count":
        limit = _bounded_int("SHERPA_OCR_MAX_PAGES", 20, 1, 1000000)
        assert state["page_render_count"] <= limit
        assert state["page_render_count"] == limit, (
            "the real PDF workload did not reach SHERPA_OCR_MAX_PAGES; an under-limit observation cannot demonstrate page truncation"
        )
        measurements.update({"effective_page_limit": limit, "limit_reached": True})
    elif probe_id == "rendered-image-size":
        limit = _bounded_int("SHERPA_OCR_MAX_PIXELS", 4000, 1, 1000000)
        assert state["pixel_sizes"]
        longest = max(max(int(value) for value in size) for size in state["pixel_sizes"])
        assert longest <= limit
        assert longest == limit, (
            "the real page render did not reach SHERPA_OCR_MAX_PIXELS; an under-limit image cannot demonstrate pixel clamping"
        )
        measurements.update(
            {
                "effective_pixel_limit": limit,
                "longest_observed_side": longest,
                "limit_reached": True,
            }
        )
    elif probe_id in {"container-user", "container-group"}:
        expected_uid = os.environ.get("SHERPA_OCR_UID", "1000")
        expected_gid = os.environ.get("SHERPA_OCR_GID", "1000")
        expected = {expected_uid, f"{expected_uid}:{expected_gid}", f":{expected_gid}"}
        assert state["container_user"] in expected, {
            "expected_uid": expected_uid,
            "expected_gid": expected_gid,
            "container_user": state["container_user"],
        }
        measurements.update({"expected_uid": expected_uid, "expected_gid": expected_gid})
    elif probe_id == "container-memory-limit":
        raw = os.environ.get("SHERPA_OCR_MEMORY_LIMIT") or "8g"
        expected = _memory_bytes(raw)
        assert state["container_memory_bytes"] == expected
        measurements["expected_memory_bytes"] = expected
    elif probe_id == "observation":
        assert state["observation_count"] > 0 and state["succeeded_job_count"] > 0
        if variable == "SHERPA_OCR_MAX_PAGES":
            limit = _bounded_int("SHERPA_OCR_MAX_PAGES", 20, 1, 1000000)
            assert state["page_render_count"] == limit, "OCR observation workload did not reach the configured page limit"
            measurements.update({"effective_page_limit": limit, "limit_reached": True})
        elif variable == "SHERPA_OCR_MAX_PIXELS":
            limit = _bounded_int("SHERPA_OCR_MAX_PIXELS", 4000, 1, 1000000)
            assert state["pixel_sizes"]
            longest = max(max(int(value) for value in size) for size in state["pixel_sizes"])
            assert longest == limit, "OCR observation workload did not reach the configured pixel limit"
            measurements.update({"effective_pixel_limit": limit, "longest_observed_side": longest, "limit_reached": True})
    return _result(ctx, probe_id, state["source"], measurements)


def _upload_workspace_file(ctx, path: Path) -> tuple[int, dict | None]:
    ctx.page.goto(ctx.config.base_url + "/ui/workspace.html")
    with ctx.page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/workspace/files"),
        timeout=ctx.config.timeout_ms,
    ) as response_info:
        ctx.page.locator("#file-input").set_input_files(str(path))
    response = response_info.value
    try:
        payload = response.json()
    except Exception:
        payload = None
    return response.status, payload


def _workspace_state(ctx) -> dict:
    def collect() -> dict:
        variable = _variable(ctx)
        run_dir = Path(os.environ["RUN_DIR"])
        probe_dir = run_dir / "content-semantic-inputs"
        probe_dir.mkdir(parents=True, exist_ok=True)
        limit = int(os.environ.get("SHERPA_WORKSPACE_MAX_BYTES", str(10 * 1024 * 1024)))
        accepted_size = 1 if limit != 0 else 0
        accepted_path = probe_dir / "workspace-limit-accepted.txt"
        write_private_bytes_atomic(accepted_path, b"A" * accepted_size)
        accepted_status, accepted_payload = _upload_workspace_file(ctx, accepted_path)
        created_ids: list[int] = []
        if accepted_status == 200 and isinstance(accepted_payload, dict):
            created_ids.append(int(accepted_payload["id"]))
        rejected_size = limit + 1 if 0 <= limit <= 16 * 1024 * 1024 else None
        rejected_status = None
        if rejected_size is not None:
            rejected_path = probe_dir / "workspace-limit-rejected.txt"
            write_private_bytes_atomic(rejected_path, b"B" * rejected_size)
            rejected_status, _ = _upload_workspace_file(ctx, rejected_path)
        listing = ctx.api.get_json("/workspace/files", save_as="state/content-semantics-workspace-files.json")
        for row in listing.get("files") or []:
            if int(row.get("id") or 0) in created_ids:
                ctx.evidence.add_cleanup(
                    f"delete content workspace file {row['id']}",
                    lambda file_id=int(row["id"]): ctx.api.request("DELETE", f"/workspace/files/{file_id}", expected={200, 404}),
                )
        database_rows = (
            _postgres_rows(
                ctx,
                "SELECT id,rel_path,size_bytes,created_at,expires_at,status FROM personal_workspace_files WHERE id = ANY(%s) ORDER BY id",
                (created_ids,),
            )
            if created_ids
            else []
        )
        if limit == 0:
            assert accepted_status == 200 and accepted_size == 0
            assert rejected_status == 413
        else:
            assert accepted_status == 200
            if rejected_status is not None:
                assert rejected_status == 413
        return {
            "source": "real browser multipart uploads, workspace API, physical limit response and Postgres ledger",
            "scenario_variable": variable,
            "configured_max_bytes": limit,
            "accepted_size_bytes": accepted_size,
            "accepted_status": accepted_status,
            "rejected_size_bytes": rejected_size,
            "rejected_status": rejected_status,
            "database_rows": database_rows,
            "listed_file_count": len(listing.get("files") or []),
        }

    return _cache(ctx, "content_semantics_workspace", collect)


def _ext_convert_limit(ctx) -> dict:
    def collect() -> dict:
        created = ctx.api.post_json("/ext/v1/admin/keys", {"label": "ui-automation-content-semantics"})
        key = str(created.get("key") or "")
        key_id = int(created.get("id") or 0)
        assert key and key_id
        ctx.evidence.register_secret(key)
        ctx.evidence.add_cleanup(
            f"revoke content semantic external key {key_id}",
            lambda: ctx.api.request("DELETE", f"/ext/v1/admin/keys/{key_id}", expected={200, 404}),
        )
        limit = int(os.environ.get("SHERPA_EXT_CONVERT_MAX_BYTES", str(50 * 1024 * 1024)))
        assert 0 <= limit <= 64 * 1024 * 1024, "external conversion limit is too large for a bounded UI automation payload"
        data = b"X" * (limit + 1)
        boundary = "----SherpaUiAutomationContentBoundary"
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                'filename="limit.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
            ).encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        request = urllib.request.Request(
            ctx.config.base_url + "/ext/v1/convert",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-API-Key": key,
                "Accept": "application/json",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                status = response.status
                response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.read()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        ctx.evidence.record_api(
            method="POST",
            url=ctx.config.base_url + "/ext/v1/convert",
            status=status,
            elapsed_ms=elapsed_ms,
        )
        assert status == 413, f"external conversion accepted {limit + 1} bytes at limit {limit}"
        return {
            "source": "real external API key, multipart conversion request and measured 413 limit response",
            "configured_max_bytes": limit,
            "attempted_bytes": limit + 1,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "key_id_sha256": _sha256(str(key_id)),
            "key_sha256": _sha256(key),
        }

    return _cache(ctx, "content_semantics_ext_convert", collect)


def _workspace_semantics(ctx, probe_id: str) -> dict:
    if _variable(ctx) == "SHERPA_VLM_MAX_IMAGE_MB":
        state = _vlm_image_limit_state(ctx)
        return _result(
            ctx,
            probe_id,
            state["source"],
            state,
        )
    if _variable(ctx) == "SHERPA_EXT_CONVERT_MAX_BYTES":
        state = _ext_convert_limit(ctx)
        return _result(ctx, probe_id, state["source"], state)
    state = _workspace_state(ctx)
    measurements = dict(state)
    measurements.pop("source", None)
    if probe_id == "expiry-time":
        ttl_days = int(os.environ.get("SHERPA_WORKSPACE_TTL_DAYS", "90") or 0)
        assert state["database_rows"], "workspace TTL probe has no real ledger row"
        row = state["database_rows"][-1]
        if ttl_days == 0:
            assert row.get("expires_at") is None
            lifetime_seconds = None
        else:
            assert row.get("expires_at") is not None
            lifetime_seconds = (row["expires_at"] - row["created_at"]).total_seconds()
            assert abs(lifetime_seconds - ttl_days * 86400) < 5
        measurements.update({"configured_ttl_days": ttl_days, "database_lifetime_seconds": lifetime_seconds})
    elif probe_id in {"upload-result", "upload-rejection"}:
        assert state["accepted_status"] == 200 and state["rejected_status"] == 413
    return _result(ctx, probe_id, state["source"], measurements)


def _office_health(ctx) -> dict:
    url = (os.environ.get("SHERPA_OFFICE_COM_URL") or "").strip().rstrip("/")
    token = os.environ.get("SHERPA_OFFICE_COM_TOKEN") or ""
    if not url:
        return {
            "mode": "local-or-unavailable",
            "url_configured": False,
            "worker_status": None,
            "token_present": bool(token),
        }
    parsed = urlsplit(url)
    assert parsed.scheme in {"http", "https"} and parsed.hostname
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Sherpa-Token"] = token
    request = urllib.request.Request(url + "/healthz", headers=headers, method="GET")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    ctx.evidence.record_api(method="GET", url=url + "/healthz", status=status, elapsed_ms=elapsed_ms)
    assert status == 200 and isinstance(payload, dict), payload
    return {
        "mode": "http",
        "url_configured": True,
        "url_origin_sha256": _sha256(f"{parsed.scheme}://{parsed.netloc}"),
        "worker_status": status,
        "worker_payload_sha256": _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        "elapsed_ms": elapsed_ms,
        "token_present": bool(token),
        "token_sha256": _sha256(token) if token else None,
    }


def _office_semantics(ctx, probe_id: str) -> dict:
    world = _world_state(ctx)
    health = _office_health(ctx)
    admin = ctx.api.get_json("/admin/settings", save_as="state/content-semantics-office-settings.json")
    legacy = admin.get("legacy_backend") or {}
    office = legacy.get("office_com") or {}
    legacy_row = world["preview_documents"].get("legacy/legacy-note.doc") or {}
    provenance = legacy_row.get("provenance") or {}
    assert legacy_row.get("state") == "ready"
    assert provenance.get("method") == "ooxml"
    if health["url_configured"]:
        assert health["worker_status"] == 200
    measurements = {
        "worker": health,
        "settings_mode": office.get("mode"),
        "settings_available": office.get("available"),
        "legacy_effective_backend": legacy.get("effective"),
        "legacy_provenance": provenance,
    }
    if os.environ.get("SHERPA_OFFICE_COM_URL") or os.environ.get("SHERPA_OFFICE_COM_TOKEN"):
        assert provenance.get("legacy_backend") == "office_com", (
            "configured Office worker connection was not exercised by the real legacy conversion"
        )
    if probe_id == "worker-request-summary":
        configured = (os.environ.get("SHERPA_OFFICE_TRANSFER_MODE") or "path").strip() or "path"
        assert configured in {"path", "upload", "auto"}, (
            "the configured Office transfer mode is not an accepted product mode and cannot be reported as an exercised request path"
        )
        assert provenance.get("legacy_backend") == "office_com", "Office transfer mode was not exercised by a real Office worker conversion"
        measurements.update(
            {
                "configured_transfer_mode": configured,
                "real_conversion_completed": True,
                "request_path_basis": "successful Office worker conversion provenance",
            }
        )
    elif probe_id == "redacted-worker-request":
        measurements["redaction"] = _secret_absent_from_service_logs(ctx, "SHERPA_OFFICE_COM_TOKEN")
    elif probe_id == "worker-probe" and health["url_configured"]:
        assert health["worker_status"] == 200
    return _result(
        ctx,
        probe_id,
        "real Office worker health/settings and ingested legacy document provenance",
        measurements,
    )


def _isolated_effective_directory(ctx, variable: str) -> Path:
    raw = os.environ.get(variable)
    assert raw, f"{variable} has no effective directory in the UI automation process"
    configured = Path(raw)
    assert configured.is_absolute(), f"{variable} must resolve to an absolute runner-owned path"
    assert not configured.is_symlink(), f"{variable} must not be a symlink"
    resolved = configured.resolve(strict=True)
    assert resolved.is_dir(), f"{variable} effective path is not a directory"
    run_dir = Path(os.environ["RUN_DIR"]).resolve(strict=True)
    runtime_root = run_dir.parent
    marker = runtime_root / ".ui-automation-runtime.json"
    assert marker.is_file() and not marker.is_symlink(), "runtime ownership marker is absent"
    try:
        resolved.relative_to(runtime_root)
    except ValueError as exc:
        raise AssertionError(f"{variable} escaped the runner-owned runtime root") from exc
    return resolved


def _workspace_direct_state(ctx) -> dict:
    def collect() -> dict:
        users_root = _isolated_effective_directory(ctx, "SHERPA_USERS_DIR")
        me = ctx.api.get_json("/auth/me", save_as="state/direct-users-auth-me.json")
        uid = str(me.get("uid") or "")
        assert uid, "authenticated workspace user has no uid"

        token = _sha256(f"{ctx.config.run_id}:{ctx.config.profile}:users-dir")[:14]
        filename = f"env-users-dir-{token}.txt"
        body = f"SHERPA-USERS-DIR-PHYSICAL-{token}\n".encode()
        source_dir = Path(os.environ["RUN_DIR"]) / "content-direct-inputs"
        source_dir.mkdir(parents=True, exist_ok=True)
        source = source_dir / filename
        write_private_bytes_atomic(source, body)

        status, payload = _upload_workspace_file(ctx, source)
        assert status == 200 and isinstance(payload, dict), f"real workspace upload failed: status={status} payload={payload}"
        file_id = int(payload.get("id") or 0)
        assert file_id > 0
        ctx.evidence.add_cleanup(
            f"delete direct users-dir workspace file {file_id}",
            lambda: ctx.api.request("DELETE", f"/workspace/files/{file_id}", expected={200, 404}),
        )

        rows = _postgres_rows(
            ctx,
            "SELECT id,user_id,rel_path,original_path,size_bytes,sha256,status,deleted_at FROM personal_workspace_files WHERE id=%s",
            (file_id,),
        )
        assert len(rows) == 1, "workspace upload has no unique Postgres ledger row"
        row = rows[0]
        expected_path = (users_root / uid / "workspace" / "files" / filename).resolve()
        physical = Path(str(row.get("original_path") or ""))
        assert physical.is_absolute() and not physical.is_symlink()
        assert physical.resolve(strict=True) == expected_path
        assert physical.parent == expected_path.parent
        assert stat.S_ISREG(physical.stat().st_mode), "workspace upload is not a regular file"
        physical_bytes = physical.read_bytes()
        physical_sha = _sha256(physical_bytes)
        assert physical_bytes == body
        assert row.get("user_id") == uid and row.get("rel_path") == filename
        assert row.get("status") == "uploaded" and row.get("deleted_at") is None
        assert int(row.get("size_bytes") or -1) == len(body)
        assert str(row.get("sha256") or "") == physical_sha == str(payload.get("sha256") or "")

        before_delete = ctx.api.get_json("/workspace/files", save_as="state/direct-users-workspace-before-delete.json")
        assert any(int(item.get("id") or 0) == file_id for item in before_delete.get("files") or [])

        deleted = ctx.api.delete_json(f"/workspace/files/{file_id}", save_as="state/direct-users-workspace-delete.json")
        assert deleted.get("ok") is True and int(deleted.get("id") or 0) == file_id
        assert not physical.exists(), "workspace DELETE updated the API but left the physical file under SHERPA_USERS_DIR"
        after_rows = _postgres_rows(
            ctx,
            "SELECT status,deleted_at FROM personal_workspace_files WHERE id=%s",
            (file_id,),
        )
        assert len(after_rows) == 1
        assert after_rows[0].get("status") == "deleted"
        assert after_rows[0].get("deleted_at") is not None
        after_delete = ctx.api.get_json("/workspace/files", save_as="state/direct-users-workspace-after-delete.json")
        assert all(int(item.get("id") or 0) != file_id for item in after_delete.get("files") or [])
        audits = _postgres_rows(
            ctx,
            "SELECT action,outcome FROM audit_log WHERE resource_id=%s ORDER BY id",
            (f"pwf:{file_id}",),
        )
        successful_actions = {str(item.get("action")) for item in audits if item.get("outcome") == "success"}
        assert {"workspace.file_uploaded", "workspace.file_deleted"} <= successful_actions

        measured = {
            "source": (
                "real browser multipart upload and DELETE correlated with the exact "
                "SHERPA_USERS_DIR file, Postgres ledger, listing API, and audit rows"
            ),
            "users_root_sha256": _sha256(str(users_root)),
            "user_id_sha256": _sha256(uid),
            "workspace_relative_path": physical.relative_to(users_root).as_posix(),
            "uploaded_file_sha256": physical_sha,
            "uploaded_size_bytes": len(body),
            "upload_http_status": status,
            "physical_file_existed_after_upload": True,
            "delete_http_ok": True,
            "physical_file_absent_after_delete": True,
            "database_status_after_delete": after_rows[0]["status"],
            "audit_actions": sorted(successful_actions),
        }
        ctx.evidence.write_json("state/direct-users-dir-effect.json", measured)
        return measured

    return _cache(ctx, "content_direct_users_dir", collect)


def _load_json_object(path: Path, label: str) -> dict:
    assert path.is_file() and not path.is_symlink(), f"{label} is absent or is a symlink"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(f"{label} is not valid JSON") from exc
    assert isinstance(value, dict), f"{label} must contain a JSON object"
    return value


def _observation_direct_state(ctx) -> dict:
    def collect() -> dict:
        observation_root = _isolated_effective_directory(ctx, "SHERPA_OBSERVATION_DIR")
        containers = _ocr_container_ids()
        assert len(containers) == 1, (
            f"SHERPA_OBSERVATION_DIR semantics require the real OCR worker; runner-owned worker containers={len(containers)}"
        )
        world_id = ctx.fixture("real_world")
        state = _observation_state(ctx)
        assert state["succeeded_job_count"] > 0
        jobs = _postgres_rows(
            ctx,
            "SELECT id,source_rel_path,status,result_observation_set_hash,result_payload,"
            "observation_count,artifact_published FROM ocr_jobs "
            "WHERE world=%s ORDER BY id",
            (world_id,),
        )
        required_sources = ("media/ocr-evidence.png", "media/vlm-evidence.bmp")
        selected: dict[str, dict] = {}
        for source_rel in required_sources:
            candidates = [
                row
                for row in jobs
                if str(row.get("source_rel_path") or "").replace("\\", "/") == source_rel and row.get("status") == "succeeded"
            ]
            assert candidates, f"real observation DB has no succeeded job for {source_rel}"
            selected[source_rel] = candidates[-1]

        world_root = observation_root / world_id
        assert world_root.is_dir() and not world_root.is_symlink()
        pointer = _load_json_object(world_root / "md-observations.current.json", "observation pointer")
        canonical_id = str(pointer.get("canonical_generation_id") or "")
        observation_id = str(pointer.get("observation_generation_id") or "")
        assert re.fullmatch(r"[0-9a-f]{64}", canonical_id)
        assert re.fullmatch(r"[0-9a-f]{64}", observation_id)
        generation = world_root / "md-observation-generations" / canonical_id / observation_id
        assert generation.is_dir() and not generation.is_symlink()
        try:
            generation.resolve(strict=True).relative_to(observation_root)
        except ValueError as exc:
            raise AssertionError("active observation generation escaped SHERPA_OBSERVATION_DIR") from exc
        manifest = _load_json_object(
            generation / ".observation-generation.json",
            "observation generation manifest",
        )
        for key in (
            "canonical_generation_id",
            "observation_generation_id",
            "artifact_sha256",
            "artifact_count",
            "artifact_bytes",
        ):
            assert manifest.get(key) == pointer.get(key), f"observation pointer/manifest mismatch for {key}"
        assert_no_mount_targets(generation)
        generation_candidates = list(generation.rglob("*"))
        assert not any(path.is_symlink() for path in generation_candidates), "observation generation contains a symlink"
        artifact_files = [path for path in generation_candidates if path.is_file() and path.name != ".observation-generation.json"]
        assert len(artifact_files) == int(manifest.get("artifact_count") or -1)
        assert sum(path.stat().st_size for path in artifact_files) == int(manifest.get("artifact_bytes") or -1)

        artifacts = []
        for source_rel, row in selected.items():
            relative = Path(source_rel)
            assert not relative.is_absolute() and ".." not in relative.parts
            base = generation.joinpath(*relative.parts)
            physical_paths = {
                "observation_sets": Path(str(base) + ".ai_observations.jsonl"),
                "markdown": Path(str(base) + ".rag_observations.md"),
                "chunks": Path(str(base) + ".rag_observation_chunks.jsonl"),
            }
            assert all(path.is_file() and not path.is_symlink() for path in physical_paths.values()), (
                f"published observation artifact set is incomplete for {source_rel}"
            )
            set_rows = [
                json.loads(line) for line in physical_paths["observation_sets"].read_text(encoding="utf-8").splitlines() if line.strip()
            ]
            assert set_rows and all(isinstance(item, dict) for item in set_rows)
            expected_hash = str(row.get("result_observation_set_hash") or "")
            matched_sets = [item for item in set_rows if str(item.get("observation_set_hash") or "") == expected_hash]
            assert expected_hash and matched_sets, f"physical observation JSONL does not contain DB result hash for {source_rel}"
            payload = row.get("result_payload") or {}
            assert isinstance(payload, dict)
            assert matched_sets[-1].get("provider") == payload.get("provider")
            assert matched_sets[-1].get("model") == payload.get("model")
            assert int(row.get("observation_count") or 0) > 0
            assert row.get("artifact_published") is True
            markdown = physical_paths["markdown"].read_text(encoding="utf-8")
            chunks = [json.loads(line) for line in physical_paths["chunks"].read_text(encoding="utf-8").splitlines() if line.strip()]
            assert markdown.strip() and chunks
            if source_rel.endswith("ocr-evidence.png"):
                assert "SHERPA-OCR-IMAGE-773" in markdown
            artifacts.append(
                {
                    "source_rel_path": source_rel,
                    "provider": str(payload.get("provider") or ""),
                    "model": str(payload.get("model") or ""),
                    "observation_count": int(row.get("observation_count") or 0),
                    "observation_set_sha256": expected_hash,
                    "physical_paths": {kind: path.relative_to(observation_root).as_posix() for kind, path in physical_paths.items()},
                    "physical_sha256": {kind: _sha256(path.read_bytes()) for kind, path in physical_paths.items()},
                    "chunk_count": len(chunks),
                }
            )

        measured = {
            "source": (
                "real OCR and VLM jobs correlated by observation-set hash to the active "
                "immutable generation physically under SHERPA_OBSERVATION_DIR"
            ),
            "observation_root_sha256": _sha256(str(observation_root)),
            "world_id_sha256": _sha256(world_id),
            "canonical_generation_sha256": _sha256(canonical_id),
            "observation_generation_sha256": _sha256(observation_id),
            "manifest_artifact_count": len(artifact_files),
            "manifest_artifact_bytes": sum(path.stat().st_size for path in artifact_files),
            "worker_container_sha256": _sha256(containers[0]),
            "artifacts": artifacts,
        }
        ctx.evidence.write_json("state/direct-observation-dir-effect.json", measured)
        return measured

    return _cache(ctx, "content_direct_observation_dir", collect)


def _register_direct_world(ctx, requested_world_id: str, purpose: str) -> dict:
    def collect() -> dict:
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", requested_world_id), (
            f"SHERPA_VERSION does not name a valid default World: {requested_world_id!r}"
        )
        root = ctx.config.world_path.resolve(strict=True)
        listing = ctx.api.get_json("/worlds", save_as=f"state/direct-{purpose}-worlds-before.json")
        worlds = listing.get("worlds") or []
        matching = [row for row in worlds if Path(str(row.get("root_path") or "")).resolve() == root]
        assert len(matching) <= 1
        created = False
        if matching:
            world = matching[0]
            assert str(world.get("world_id") or "") == requested_world_id, (
                "the runner World path is already bound to a different id than the requested default"
            )
        else:
            assert not worlds, "isolated database contains an unrelated World; refusing to alter it"
            created_payload = ctx.api.post_json(
                "/worlds",
                {
                    "path": str(root),
                    "label": f"UI Automation {purpose} World",
                    "world_id": requested_world_id,
                },
                save_as=f"state/direct-{purpose}-world-create.json",
            )
            world = created_payload.get("world") or {}
            created = True
        world_id = str(world.get("world_id") or "")
        assert world_id == requested_world_id
        assert Path(str(world.get("root_path") or "")).resolve(strict=True) == root
        if created:
            ctx.evidence.add_cleanup(
                f"delete direct {purpose} World {world_id}",
                lambda: ctx.api.request("DELETE", f"/worlds/{world_id}", expected={200, 404}),
            )
        return {
            "world_id": world_id,
            "root": root,
            "created": created,
        }

    return _cache(ctx, f"content_direct_world:{purpose}:{requested_world_id}", collect)


def _metering_direct_world(ctx) -> str:
    world_id = "ui-metering-" + _sha256(f"{ctx.config.run_id}:{ctx.config.profile}:metering")[:12]
    return str(_register_direct_world(ctx, world_id, "metering")["world_id"])


def _default_world_state(ctx) -> dict:
    def collect() -> dict:
        raw = os.environ.get("SHERPA_VERSION")
        expected_world = raw if raw else "v1"
        registered = _register_direct_world(ctx, expected_world, "default")
        root = registered["root"]

        status = ctx.api.get_json(
            f"/worlds/{expected_world}/status",
            save_as="state/direct-default-world-status.json",
        )
        omitted_documents = ctx.api.get_json("/documents", save_as="state/direct-default-documents-omitted.json")
        explicit_documents = ctx.api.get_json(
            LiveApi.query("/documents", world=expected_world),
            save_as="state/direct-default-documents-explicit.json",
        )
        omitted_preview = ctx.api.get_json("/ingest/preview", save_as="state/direct-default-preview-omitted.json")
        explicit_preview = ctx.api.get_json(
            LiveApi.query("/ingest/preview", world=expected_world),
            save_as="state/direct-default-preview-explicit.json",
        )
        assert status.get("ok") is True and status.get("world_id") == expected_world
        assert Path(str(status.get("root_path") or "")).resolve(strict=True) == root
        assert int(status.get("indexed") or 0) > 0
        assert omitted_documents.get("world") == expected_world
        assert explicit_documents.get("world") == expected_world
        omitted_docs = omitted_documents.get("documents") or []
        explicit_docs = explicit_documents.get("documents") or []
        assert omitted_docs and omitted_docs == explicit_docs, "world-omitted /documents did not select the configured default World"
        omitted_preview_docs = omitted_preview.get("documents") or []
        explicit_preview_docs = explicit_preview.get("documents") or []
        assert omitted_preview_docs and omitted_preview_docs == explicit_preview_docs, (
            "world-omitted /ingest/preview did not select the configured default World"
        )
        database_worlds = _postgres_rows(
            ctx,
            "SELECT world_id,root_path FROM worlds WHERE world_id=%s",
            (expected_world,),
        )
        database_documents = _postgres_rows(
            ctx,
            "SELECT count(*)::int AS count FROM documents WHERE version=%s",
            (expected_world,),
        )
        assert len(database_worlds) == 1
        assert Path(str(database_worlds[0].get("root_path") or "")).resolve(strict=True) == root
        database_document_count = int(database_documents[0]["count"])
        assert database_document_count == len(omitted_docs) > 0
        doc_ids = sorted(str(row.get("path") or row.get("name") or "") for row in omitted_docs)
        preview_ids = sorted(str(row.get("name") or "") for row in omitted_preview_docs)
        assert doc_ids == preview_ids

        measured = {
            "source": (
                "world-omitted and explicit documents/preview APIs correlated with the "
                "requested default World, status API, Postgres registry/documents, and physical root"
            ),
            "effective_default_world": expected_world,
            "default_world_sha256": _sha256(expected_world),
            "world_root_sha256": _sha256(str(root)),
            "status_world_matches": True,
            "status_indexed_documents": int(status.get("indexed") or 0),
            "omitted_documents_world_matches": True,
            "omitted_explicit_document_exact_match": True,
            "omitted_explicit_preview_exact_match": True,
            "api_document_count": len(omitted_docs),
            "database_document_count": database_document_count,
            "document_ids_sha256": _sha256("\n".join(doc_ids)),
            "registered_for_probe": bool(registered["created"]),
        }
        ctx.evidence.write_json("state/direct-default-world-effect.json", measured)
        return measured

    return _cache(ctx, "content_direct_default_world", collect)


def _agent_direct_state(ctx) -> dict:
    def collect() -> dict:
        settings = ctx.api.get_json("/settings", save_as="state/direct-agent-settings.json")
        provider_config = ctx.api.get_json("/config", save_as="state/direct-agent-provider-config.json")
        me = ctx.api.get_json("/auth/me")
        uid = str(me.get("uid") or "")
        assert uid
        settings_agent = str(settings.get("agent") or "").strip().lower()
        config_agent = str(provider_config.get("agent") or "").strip().lower()
        config_model = str(provider_config.get("model") or "").strip()
        assert settings_agent and settings_agent != "heuristic"
        assert config_agent == settings_agent and config_model
        raw = (os.environ.get("SHERPA_AGENT") or "").strip().lower()
        if raw:
            assert settings_agent == raw, "SHERPA_AGENT did not become the effective fresh-DB settings agent"
        overrides = _postgres_rows(
            ctx,
            "SELECT agent FROM user_settings WHERE user_id=%s",
            (uid,),
        )
        persisted_override = str(overrides[0].get("agent") or "").strip().lower() if overrides else ""
        assert not persisted_override, "a persisted per-user agent override masked SHERPA_AGENT in the fresh database"

        ctx.page.goto(ctx.config.base_url + "/ui/settings.html")
        agent_select = ctx.page.locator("#agent")
        agent_select.wait_for(state="visible", timeout=ctx.config.timeout_ms)
        deadline = time.monotonic() + ctx.config.timeout_ms / 1000
        ui_construct = ""
        while time.monotonic() < deadline:
            ui_construct = str(agent_select.input_value() or "")
            if ui_construct:
                break
            time.sleep(0.05)
        assert ui_construct == str(settings.get("construct_id") or "")

        chat = _chat_turn(ctx)
        assert not chat["structured_error"]
        assert chat["grounded_fixture_fact_seen"], "real provider answer omitted the deterministic 02:15 fixture fact"
        assert chat["provider"] == settings_agent == chat["database_provider"]
        assert chat["model"] == config_model == chat["database_model"]
        assert int(chat["input_tokens"]) + int(chat["output_tokens"]) > 0
        assert chat["input_tokens"] == chat["database_input_tokens"]
        assert chat["output_tokens"] == chat["database_output_tokens"]
        assert chat["database_usage_storage"] == "messages.answer.usage"
        assert chat["audit_outcome"] == "success"
        assert chat["audit_provider"] == settings_agent
        assert chat["tool_node_count"] > 0
        assert chat["answer_delta_count"] > 0

        measured = {
            "source": (
                "effective settings/config and settings UI correlated with a real provider "
                "turn, SSE/tool trace, messages.answer usage, Postgres content, and chat.turn audit"
            ),
            "settings_agent": settings_agent,
            "settings_construct": str(settings.get("construct_id") or ""),
            "ui_construct": ui_construct,
            "provider": chat["provider"],
            "model": chat["model"],
            "input_tokens": int(chat["input_tokens"]),
            "output_tokens": int(chat["output_tokens"]),
            "database_provider": chat["database_provider"],
            "database_model": chat["database_model"],
            "database_usage_storage": chat["database_usage_storage"],
            "audit_provider": chat["audit_provider"],
            "audit_outcome": chat["audit_outcome"],
            "answer_sha256": chat["answer_sha256"],
            "grounded_fixture_fact_seen": True,
            "sse_event_count": chat["event_count"],
            "tool_node_count": chat["tool_node_count"],
            "answer_delta_count": chat["answer_delta_count"],
            "database_exact_content_match": chat["database_exact_content_match"],
            "per_user_override_absent": True,
        }
        ctx.evidence.write_json("state/direct-agent-effect.json", measured)
        return measured

    return _cache(ctx, "content_direct_agent", collect)


def _usage_metering_direct_state(ctx) -> dict:
    def collect() -> dict:
        admin = ctx.api.get_json("/admin/settings", save_as="state/direct-usage-metering-settings.json")
        metering = admin.get("usage_metering") or {}
        assert isinstance(metering, dict)
        configured = metering.get("configured")
        effective = metering.get("effective")
        expected = (os.environ.get("SHERPA_USAGE_METERING") or "").strip().lower() in {
            "1",
            "true",
        }
        assert configured is None, "persisted usage_metering masked SHERPA_USAGE_METERING in the fresh database"
        assert effective is expected

        chat = _chat_turn(ctx)
        assert not chat["structured_error"]
        assert chat["grounded_fixture_fact_seen"]
        assert int(chat["input_tokens"]) + int(chat["output_tokens"]) > 0
        assert chat["database_provider"] == chat["provider"]
        assert chat["database_model"] == chat["model"]
        auxiliary = chat["auxiliary_usage_events"]
        if expected:
            assert auxiliary, "usage metering was effective but the forced real intent-provider call created no auxiliary usage_events row"
            assert any(row.get("kind") == "intent" for row in auxiliary), (
                "the ambiguous real chat did not persist its intent-provider usage"
            )
            for row in auxiliary:
                ctx.evidence.record_usage_event(
                    row,
                    turn_id=f"direct-metering:{row['id']}",
                    operation="environment-direct-usage-metering",
                )
        else:
            assert not auxiliary, "usage metering was disabled but the same real chat persisted auxiliary usage rows"
        measured = {
            "source": (
                "one real ambiguous browser chat bracketed by a Postgres usage_events checkpoint, "
                "with independent SSE and non-zero messages.answer provider usage"
            ),
            "metering_effective": expected,
            "usage_event_checkpoint": chat["usage_event_checkpoint"],
            "auxiliary_usage_event_count": len(auxiliary),
            "auxiliary_kinds": sorted({str(row.get("kind") or "") for row in auxiliary}),
            "auxiliary_calls": sum(int(row.get("calls") or 0) for row in auxiliary),
            "main_usage_storage": chat["database_usage_storage"],
            "main_provider": chat["provider"],
            "main_model": chat["model"],
            "main_input_tokens": int(chat["input_tokens"]),
            "main_output_tokens": int(chat["output_tokens"]),
            "main_usage_nonzero": True,
            "database_exact_content_match": chat["database_exact_content_match"],
            "audit_outcome": chat["audit_outcome"],
        }
        ctx.evidence.write_json("state/direct-usage-metering-effect.json", measured)
        return measured

    return _cache(ctx, "content_direct_usage_metering", collect)


def run_content_direct_probe(ctx, probe_id: str) -> dict:
    """Run a strict variable-specific replacement for a legacy direct probe."""

    variable = _assert_direct_probe_contract(ctx, probe_id)
    if variable == "SHERPA_USERS_DIR":
        state = _workspace_direct_state(ctx)
    elif variable == "SHERPA_OBSERVATION_DIR":
        state = _observation_direct_state(ctx)
    elif variable == "SHERPA_VERSION":
        state = _default_world_state(ctx)
    elif variable == "SHERPA_AGENT":
        state = _agent_direct_state(ctx)
    elif variable == "SHERPA_USAGE_METERING":
        state = _usage_metering_direct_state(ctx)
    else:  # pragma: no cover - guarded by DIRECT_PROBE_VARIABLES
        raise AssertionError(f"content direct probe has no variable-specific adapter for {variable}")
    measurements = dict(state)
    source = str(measurements.pop("source"))
    return _result(ctx, probe_id, source, measurements)


def run_content_semantic_probe(ctx, probe_id: str) -> dict:
    assert probe_id in SUPPORTED_PROBES, f"unsupported content semantic probe: {probe_id}"
    _assert_probe_contract(ctx, probe_id)
    if probe_id in _CHAT_PROBES or probe_id in _DATABASE_PROBES:
        return _chat_semantics(ctx, probe_id)
    if probe_id in _OBSERVATION_PROBES:
        return _observation_semantics(ctx, probe_id)
    if probe_id in _OFFICE_PROBES:
        return _office_semantics(ctx, probe_id)
    if probe_id in _WORKSPACE_PROBES:
        return _workspace_semantics(ctx, probe_id)
    if probe_id in _WORLD_PROBES:
        return _world_semantics(ctx, probe_id)
    raise AssertionError(f"content semantic probe has no real-service adapter: {probe_id}")
