from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ui_automation.stack.isolation import verify_local_docker_environment
from ui_automation.support.chat import (
    answer_event,
    assert_node_status_lifecycle,
    assert_persisted_trace_after_cap,
    assert_real_ai_result,
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
from ui_automation.support.env_semantics_content import (
    _PROBE_VARIABLES as CONTENT_SEMANTIC_PROBE_VARIABLES,
    SUPPORTED_PROBES as CONTENT_SEMANTIC_PROBES,
    run_content_direct_probe,
    run_content_semantic_probe,
    supports_direct_probe as supports_content_direct_probe,
)
from ui_automation.support.env_semantics_system import (
    SUPPORTED_PROBES as SYSTEM_SEMANTIC_PROBES,
    observe_secure_http_login,
    run_system_direct_probe,
    run_system_semantic_probe,
    supports_direct_probe as supports_system_direct_probe,
    supports_semantic_probe as supports_system_semantic_probe,
)
from ui_automation.support.live_api import LiveApi


_HEALTH_PROBES = {
    "ai-health-cache-age",
    "ai-health-duration",
    "compose-health",
    "health-cache-age",
    "health-duration",
    "health-summary",
    "healthz",
    "request-count",
    "startup-duration",
    "status-api",
    "status-command-duration",
    "status-output",
    "status-state",
    "status-url",
    "store-connectivity",
}
_SETTINGS_PROBES = {
    "admin-settings",
    "bedrock-auth-kind",
    "bedrock-region",
    "compatibility-warning",
    "connection-test",
    "legacy-backend-selection",
    "legacy-status",
    "observation-model",
    "observation-provider",
    "postgres-settings",
    "provider-auth-kind",
    "provider-endpoint-kind",
    "provider-model",
    "provider-url-summary",
    "settings",
    "settings-agent",
    "settings-connection-state",
    "settings-fields",
    "settings-labels",
    "settings-options",
    "settings-reasoning",
    "settings-subagents",
    "settings-toggle",
    "usage-page",
}
_AUTH_PROBES = {
    "audit-ip-hash",
    "auth-me",
    "cookie-flags",
    "login",
    "login-redirect",
    "login-result",
    "password-change-required",
    "postgres-session",
    "redacted-audit",
    "role-boundary",
    "session-cookie",
    "session-expiry",
    "set-cookie-flags",
}
_DATABASE_PROBES = {
    "database-identity",
    "postgres",
    "postgres-identity",
    "postgres-trace",
}
_BROWSER_PROBES = {
    "admin-page",
    "artifact-render",
    "browser-version",
    "folder-picker",
    "fs-list-rejection",
    "navigation",
    "ui-shell",
    "unicode-ui",
}
_PROCESS_PROBES = {
    "app-process",
    "codex-invocation-summary",
    "codex-login-status",
    "codex-tls-probe",
    "codex-version",
    "command-resolution",
    "libreoffice-version",
    "listen-address",
    "ollama-listen-address",
    "open-ports",
    "pid-file",
    "pid-path",
    "port-owner-check",
    "ports",
    "preflight-exit",
    "process-count",
    "process-exit",
    "process-identity",
    "process-locale",
    "python-version",
    "shutdown-duration",
    "worker-version",
}
_LOG_PROBES = {
    "app-log",
    "author-error",
    "ingest-error",
    "ingest-log",
    "ocr-worker-log",
    "preflight-log",
    "proxy-log",
}
_FILESYSTEM_PROBES = {
    "artifact-file",
    "compose-config",
    "compose-mount",
    "created-file-owner",
    "derived-files",
    "files-sha256",
    "hashed-codex-home",
    "hashed-home",
    "model-files-sha256",
    "ollama-model-path",
    "redacted-compose-config",
    "redacted-effective-environment",
    "restart-effective-environment",
    "volume-identity",
    "volume-names",
    "world-path",
    "world-registry",
    "world-root-check",
    "write-boundary",
}
_WORLD_PROBES = {
    "conversion-duration",
    "conversion-result",
    "derived-markdown",
    "documents",
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
    "ingest-status",
    "query-duration",
    "refresh-time",
    "search-hit-size",
    "search-hits",
    "search-source",
    "selected-arm",
    "truncated-flag",
}
_OBSERVATION_PROBES = {
    "container-group",
    "container-memory-limit",
    "container-user",
    "observation",
    "ocr-observations",
    "ocr-worker-state",
    "processed-page-count",
    "rendered-image-size",
}
_WORKSPACE_PROBES = {
    "expiry-time",
    "upload-rejection",
    "upload-result",
    "workspace-files",
}
_CHAT_PROBES = {
    "author-duration",
    "author-trace",
    "bedrock-probe",
    "chat-answer",
    "chat-error",
    "conversation-api",
    "conversation-continuity",
    "conversation-ui",
    "evaluation-cycle-count",
    "evidence-verification",
    "gemini-probe",
    "ollama-probe",
    "provider-duration",
    "provider-probe",
    "provider-request-count",
    "provider-request-summary",
    "provider-result",
    "provider-usage",
    "real-provider-result",
    "redacted-provider-request",
    "sse",
    "sse-node-count",
    "sse-schema",
    "sse-timing",
    "sse-tool-count",
    "sse-tool-nodes",
    "subagent-call-count",
    "subagent-step-count",
    "tls-probe",
    "tool-nodes",
    "trace",
    "trace-result-size",
    "trace-total-size",
    "turn-duration",
    "turn-slot-release",
    "ui-trace",
    "ui-trace-order",
    "usage",
    "usage-records",
}
_AUDIT_PROBES = {"audit"}
_COMPOSE_PROBES = {"compose-labels", "compose-processes"}
_EFFECTIVE_PROBES = {"effective-environment", "variable-presence"}
_OFFICE_PROBES = {"redacted-worker-request", "worker-probe", "worker-request-summary"}
_GRAPH_STORE_PROBES = {"neo4j-http", "neo4j-identity"}
_RUNNER_ISOLATION_DEFAULTS = {
    "APP_PID_FILE",
    "CODEX_HOME",
    "COMPOSE_PROJECT_NAME",
    "DATABASE_URL",
    "ES_URL",
    "HOME",
    "NEO4J_PASSWORD",
    "NEO4J_URI",
    "NEO4J_USER",
    "NO_PROXY",
    "PGPORT",
    "POSTGRES_DB",
    "POSTGRES_PASSWORD",
    "POSTGRES_USER",
    "PYTHON_BIN",
    "RUN_DIR",
    "SHERPA_ADMIN_PASSWORD",
    "SHERPA_BROWSE_ROOTS",
    "SHERPA_COMPOSE_PROJECT",
    "SHERPA_COOKIE_SECURE",
    "SHERPA_DERIVED_DIR",
    "SHERPA_ENV_FILE",
    "SHERPA_ES_PORT",
    "SHERPA_HEALTH_CURL_TIMEOUT",
    "SHERPA_HOST",
    "SHERPA_KB_DIR",
    "SHERPA_LAN",
    "SHERPA_NEO4J_BOLT_PORT",
    "SHERPA_NEO4J_HTTP_PORT",
    "SHERPA_OBSERVATION_DIR",
    "SHERPA_OCR_MODEL_CACHE",
    "SHERPA_OCR_WORLD_ROOT",
    "SHERPA_PORT",
    "SHERPA_REQUIRE_ENV_FILE",
    "SHERPA_SKIP_PORT_CHECK",
    "SHERPA_USERS_DIR",
    "SHERPA_UVICORN_WORKERS",
    "TMPDIR",
}


class ObservedProductError(AssertionError):
    def __init__(self, observation: dict) -> None:
        super().__init__("the real product emitted the declared explicit error")
        self.observation = observation


class ProbeNotRunAfterProductError(AssertionError):
    """A prior observable already proved the declared product rejection."""

    def __init__(self, observation: dict) -> None:
        super().__init__("probe was not run after the real product rejected the environment value")
        self.observation = observation


_DIRECT_SEMANTIC_PROBES = {
    "admin-page",
    "admin-settings",
    "app-process",
    "audit",
    "auth-me",
    "browser-version",
    "chat-answer",
    "conversation-api",
    "conversation-ui",
    "cookie-flags",
    "compose-labels",
    "compose-processes",
    "database-identity",
    "documents",
    "effective-environment",
    "files-sha256",
    "health-summary",
    "healthz",
    "ingest-status",
    "libreoffice-version",
    "listen-address",
    "navigation",
    "neo4j-http",
    "neo4j-identity",
    "ocr-observations",
    "ocr-worker-state",
    "open-ports",
    "pid-file",
    "pid-path",
    "ports",
    "postgres",
    "postgres-identity",
    "process-identity",
    "process-locale",
    "provider-result",
    "provider-usage",
    "python-version",
    "real-provider-result",
    "search-hits",
    "session-cookie",
    "set-cookie-flags",
    "settings",
    "settings-agent",
    "settings-subagents",
    "sse",
    "sse-schema",
    "status-api",
    "status-state",
    "status-url",
    "store-connectivity",
    "trace",
    "ui-shell",
    "ui-trace",
    "usage",
    "usage-records",
    "variable-presence",
    "workspace-files",
    "world-path",
}
OBSERVABLE_ADAPTER_IDS = frozenset().union(
    _HEALTH_PROBES,
    _SETTINGS_PROBES,
    _AUTH_PROBES,
    _DATABASE_PROBES,
    _BROWSER_PROBES,
    _PROCESS_PROBES,
    _LOG_PROBES,
    _FILESYSTEM_PROBES,
    _WORLD_PROBES,
    _OBSERVATION_PROBES,
    _WORKSPACE_PROBES,
    _CHAT_PROBES,
    _AUDIT_PROBES,
    _COMPOSE_PROBES,
    _EFFECTIVE_PROBES,
    _OFFICE_PROBES,
    _GRAPH_STORE_PROBES,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bool(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise AssertionError(f"environment boolean contract is invalid: {value!r}")


def _load_json(path: Path) -> dict:
    assert path.is_file(), f"required runner evidence is absent: {path}"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"runner evidence must be a JSON object: {path}"
    return payload


def _profile_root(ctx: "EnvironmentProbeContext") -> Path:
    expected_path = ctx.config.expected_env_path
    assert expected_path is not None
    return expected_path.parent.parent


def _parse_env_file(path: Path) -> dict[str, str]:
    assert path.is_file(), f"effective environment file is absent: {path}"
    values: dict[str, str] = {}
    for original in path.read_text(encoding="utf-8").splitlines():
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line, f"environment file has a malformed line: {path}"
        key, value = line.split("=", 1)
        parsed = shlex.split(value, comments=False, posix=True)
        assert len(parsed) <= 1, f"environment value has ambiguous whitespace: {path}"
        values[key] = parsed[0] if parsed else ""
    return values


@dataclass
class EnvironmentProbeContext:
    request: object
    page: object
    api: object
    config: object
    evidence: object
    contract: dict
    cache: dict[str, object] = field(default_factory=dict)

    @property
    def expected(self) -> dict:
        value = self.contract.get("expected") or {}
        assert isinstance(value, dict), "environment expected contract must be an object"
        return value

    def cached(self, key: str, callback):
        if key not in self.cache:
            self.cache[key] = callback()
        return self.cache[key]

    def fixture(self, name: str):
        return self.request.getfixturevalue(name)


def _generated_scenario(ctx: EnvironmentProbeContext) -> dict:
    value = ctx.contract.get("generated_scenario") or {}
    assert isinstance(value, dict), "generated environment scenario must be an object"
    return value


def expected_outcome(ctx: EnvironmentProbeContext) -> str:
    outcome = str(_generated_scenario(ctx).get("expected_outcome") or "observed")
    assert outcome in {"observed", "accepted-boundary", "explicit-error", "reject"}, f"unsupported environment expected outcome: {outcome}"
    return outcome


def _service_log_offsets(ctx: EnvironmentProbeContext, *, include_existing: bool) -> dict[Path, int]:
    root = _profile_root(ctx) / "services"
    return {path: 0 if include_existing else path.stat().st_size for path in root.glob("*.log") if path.is_file()}


def _matching_patterns(text: str, patterns: list[str]) -> list[str]:
    matched: set[str] = set()
    for pattern in patterns:
        try:
            found = re.search(pattern, text, re.IGNORECASE) is not None
        except re.error as exc:  # 台帳検証でも弾くが、実行時もfail-closedにする。
            raise AssertionError(f"invalid expected-error regex {pattern!r}: {exc}") from exc
        if found:
            matched.add(pattern)
    return sorted(matched)


def _explicit_product_error(
    ctx: EnvironmentProbeContext,
    *,
    http_start: int,
    api_error_start: int,
    log_offsets: dict[Path, int],
) -> dict | None:
    cached = ctx.cache.get("observed_product_error")
    if isinstance(cached, dict):
        return cached
    scenario = _generated_scenario(ctx)
    patterns = [str(value) for value in scenario.get("expected_error_patterns") or []]
    allowed_sources = {str(value) for value in scenario.get("expected_error_sources") or []}
    assert patterns and allowed_sources, "explicit-error contract requires both error patterns and permitted evidence sources"
    status_codes = sorted(
        {
            int(row["status"])
            for row in ctx.evidence.http[http_start:]
            if row.get("phase") in {"response", "independent-client"} and int(row.get("status") or 0) >= 400
        }
    )
    candidates: list[tuple[str, str, str]] = []
    if "api" in allowed_sources:
        for row in ctx.api.structured_errors_since(api_error_start):
            message = str(row.get("message") or "")
            if message:
                candidates.append(
                    (
                        "api",
                        message,
                        f"network/http.jsonl:{row.get('method')} {row.get('path')}",
                    )
                )
    if "ui" in allowed_sources:
        selectors = "[role='alert'], .error, .danger, #msg, #regmsg, #listmsg"
        for index, text in enumerate(ctx.page.locator(selectors).all_inner_texts()):
            if text.strip():
                candidates.append(("ui", text, f"browser-visible-error-{index}"))
    if "service-log" in allowed_sources:
        error_words = re.compile(
            r"error|invalid|failed|failure|exception|refused|timeout|不正|失敗|エラー",
            re.IGNORECASE,
        )
        for path, offset in log_offsets.items():
            if not path.is_file():
                continue
            body = path.read_bytes()[offset:].decode("utf-8", errors="replace")
            lines = [line for line in body.splitlines() if error_words.search(line)]
            if lines:
                candidates.append(("service-log", "\n".join(lines), f"services/{path.name}"))
    for source, text, reference in candidates:
        matched = _matching_patterns(text, patterns)
        if set(matched) != set(patterns):
            continue
        observation = {
            "source": source,
            "matched_patterns": matched,
            "status_codes": status_codes,
            "evidence_refs": [reference],
            "message_sha256": _sha256(text),
        }
        ctx.cache["observed_product_error"] = observation
        return observation
    return None


def _unexpected_product_errors(
    ctx: EnvironmentProbeContext,
    *,
    http_start: int,
    log_offsets: dict[Path, int],
) -> dict:
    statuses = sorted(
        {
            int(row["status"])
            for row in ctx.evidence.http[http_start:]
            if row.get("phase") in {"response", "independent-client"} and int(row.get("status") or 0) >= 400
        }
    )
    visible_errors = 0
    candidates = ctx.page.locator("[role='alert'], .error, .danger")
    for index in range(candidates.count()):
        item = candidates.nth(index)
        if item.is_visible() and item.inner_text().strip():
            visible_errors += 1
    error_words = re.compile(
        r"\b(?:error|invalid|failed|failure|exception|refused|timeout)\b|不正|失敗|エラー",
        re.IGNORECASE,
    )
    service_error_lines = 0
    for path, offset in log_offsets.items():
        if path.is_file():
            body = path.read_bytes()[offset:].decode("utf-8", errors="replace")
            service_error_lines += sum(bool(error_words.search(line)) for line in body.splitlines())
    return {
        "http_error_statuses": statuses,
        "visible_error_count": visible_errors,
        "new_service_error_line_count": service_error_lines,
        "error_absent": not statuses and visible_errors == 0 and service_error_lines == 0,
    }


def _history_contract_active(ctx: EnvironmentProbeContext) -> bool:
    if all(ctx.expected.get(key) == 0 for key in ("history_turns", "history_msg_chars", "history_char_budget")):
        return True
    scenario = _generated_scenario(ctx)
    return str(scenario.get("variable") or "") in {
        "SHERPA_HISTORY_TURNS",
        "SHERPA_HISTORY_MSG_CHARS",
        "SHERPA_HISTORY_CHAR_BUDGET",
    }


def _trace_contract_active(ctx: EnvironmentProbeContext) -> bool:
    return ctx.expected.get("exec_event_v2") is not None


def _disable_embedding_contract_active(ctx: EnvironmentProbeContext) -> bool:
    if ctx.expected.get("disable_embed_present") is True:
        return True
    scenario = _generated_scenario(ctx)
    return (
        str(scenario.get("variable") or "") == "SHERPA_DISABLE_EMBED"
        and expected_outcome(ctx) != "explicit-error"
        and bool(os.environ.get("SHERPA_DISABLE_EMBED"))
    )


def declared_observable_ids(config_path: Path) -> set[str]:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise AssertionError("PyYAML is required to validate the environment probe registry") from exc
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    identifiers: set[str] = set()
    for raw in (document.get("variables") or {}).values():
        if isinstance(raw, dict) and raw.get("classification") == "tested":
            identifiers.update(str(value) for value in raw.get("observable") or ())
    for raw in (document.get("profiles") or {}).values():
        if not isinstance(raw, dict):
            continue
        for value in raw.get("observables") or ():
            identifiers.add(str(value.get("id") if isinstance(value, dict) else value))
    return {value for value in identifiers if value}


def declared_observable_pairs(config_path: Path) -> set[tuple[str, str]]:
    """Return every tested ``(variable, observable)`` contract in the manifest."""

    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise AssertionError("PyYAML is required to validate environment observable pairs") from exc
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    pairs: set[tuple[str, str]] = set()
    for variable, raw in (document.get("variables") or {}).items():
        if not isinstance(raw, dict) or raw.get("classification") != "tested":
            continue
        for probe_id in raw.get("observable") or ():
            if str(probe_id):
                pairs.add((str(variable), str(probe_id)))
    return pairs


def _application_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        scenario = ctx.contract.get("generated_scenario") or {}
        pairwise = (ctx.contract.get("pairwise") or {}).get("applied") or {}
        if scenario:
            variable = str(scenario.get("variable") or "")
            assert variable, "generated environment scenario omitted its variable"
            process = scenario.get("process") or {}
            env_file = scenario.get("env_file") or {}
            process_mode = str(process.get("mode") or "")
            process_contract = process.get("value")
            actual = os.environ.get(variable)
            secret = str(process_contract) in {"set", "unset"} and bool(
                (ctx.contract.get("effective_environment") or {}).get(variable) == "<redacted>" or process_contract == "set"
            )
            if process_mode == "unset":
                if variable in _RUNNER_ISOLATION_DEFAULTS:
                    assert actual not in {None, ""}, f"{variable} has no runner-owned default required by stack isolation"
                else:
                    assert actual is None, f"{variable} was required to be absent from the real pytest/application process"
            elif secret:
                expected_set = process_contract == "set"
                assert bool(actual) is expected_set, f"{variable} secret presence differs from the generated process contract"
            elif process_mode not in {"absent", ""}:
                assert actual == process_contract, f"{variable} differs between the generated contract and real process"

            env_path = Path(os.environ.get("SHERPA_ENV_FILE", ""))
            file_values = _parse_env_file(env_path)
            file_mode = str(env_file.get("mode") or "")
            file_contract = env_file.get("value")
            file_actual = file_values.get(variable)
            if file_mode == "unset":
                assert file_actual is None, f"{variable} unexpectedly remains in SHERPA_ENV_FILE"
            elif str(file_contract) in {"set", "unset"} and secret:
                assert bool(file_actual) is (file_contract == "set")
            elif file_mode not in {"absent", ""}:
                assert file_actual == file_contract, f"{variable} differs between generated contract and SHERPA_ENV_FILE"
            return {
                "source": "process-environment-and-explicit-env-file",
                "variable": variable,
                "scenario": scenario.get("scenario"),
                "process_mode": process_mode,
                "process_present": actual is not None,
                "runner_isolation_default": (process_mode == "unset" and variable in _RUNNER_ISOLATION_DEFAULTS),
                "process_value_sha256": _sha256(actual) if actual is not None and not secret else None,
                "env_file_mode": file_mode,
                "env_file_present": file_actual is not None,
                "env_file_value_sha256": (_sha256(file_actual) if file_actual is not None and not secret else None),
                "precedence": ctx.contract.get("precedence"),
            }
        if pairwise:
            factors = pairwise.get("factors") or []
            assert factors, "pairwise contract contains no applied factors"
            observed = []
            for factor in factors:
                key = str(factor.get("key") or "")
                actual = os.environ.get(key)
                expectation = str(factor.get("expectation") or "")
                if expectation == "literal":
                    assert actual == str(factor.get("value")), f"pairwise factor {key} does not match the real process environment"
                elif expectation == "set":
                    assert actual not in {None, ""}, f"pairwise factor {key} is not set"
                elif expectation == "unset":
                    assert actual in {None, ""}, f"pairwise factor {key} is unexpectedly set"
                else:
                    raise AssertionError(f"pairwise factor {key} has invalid expectation {expectation}")
                observed.append({"key": key, "expectation": expectation, "present": bool(actual)})
            return {"source": "real-pytest-process-environment", "pairwise_factors": observed}
        effective = ctx.contract.get("effective_environment") or {}
        assert isinstance(effective, dict) and effective, "runner effective environment is absent"
        return {
            "source": "runner-and-real-process-environment",
            "declared_key_count": len(effective),
            "sherpa_key_count": sum(key.startswith("SHERPA_") for key in os.environ),
        }

    return ctx.cached("application", collect)


def _health_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        started = time.monotonic()
        healthz = ctx.api.get_json("/healthz", save_as="state/environment-healthz.json")
        health = ctx.api.get_json("/health/summary", save_as="state/environment-health-summary.json")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        assert healthz.get("ok") is True, healthz
        assert health.get("status") == "ok", health
        return {
            "source": "GET /healthz and GET /health/summary",
            "healthz_ok": True,
            "summary_status": health.get("status"),
            "component_count": len(health.get("components") or {}),
            "elapsed_ms": elapsed_ms,
        }

    return ctx.cached("health", collect)


def _settings_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        settings = ctx.api.get_json("/settings", save_as="state/environment-settings.json")
        admin = ctx.api.get_json("/admin/settings", save_as="state/environment-admin-settings.json")
        assert isinstance(settings.get("constructs_available"), list), settings
        assert isinstance(admin, dict) and admin, admin
        return {
            "source": "GET /settings and GET /admin/settings",
            "agent": settings.get("agent"),
            "construct_id": settings.get("construct_id"),
            "construct_count": len(settings.get("constructs_available") or []),
            "sub_profile_count": len(settings.get("sub_profiles_available") or []),
            "admin_sections": sorted(str(key) for key in admin),
            "personal_keys_allowed": admin.get("personal_api_keys"),
        }

    observed = ctx.cached("settings", collect)
    expected_agent = ctx.expected.get("agent")
    if expected_agent not in {None, "", "unset"}:
        assert observed["agent"] == expected_agent, {
            "expected_agent": expected_agent,
            "observed_agent": observed["agent"],
        }
    expected_subagents = _bool(ctx.expected.get("subagents_enabled"))
    if expected_subagents is not None:
        assert bool(observed["sub_profile_count"]) is expected_subagents, {
            "expected_subagents_enabled": expected_subagents,
            "observed_sub_profile_count": observed["sub_profile_count"],
        }
    return observed


def _auth_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        secure_http = (
            os.environ.get("SHERPA_COOKIE_SECURE", "").strip().casefold() in {"1", "true", "yes", "on"}
            and urlsplit(ctx.config.base_url).scheme == "http"
            and not _bool(os.environ.get("SHERPA_AUTH_DISABLED"))
        )
        if secure_http:
            observed = observe_secure_http_login(ctx)
            return {
                "source": "real HTTP login Set-Cookie, direct auth_sessions row, and cookie-less /auth/me rejection",
                **observed,
            }
        me = ctx.api.get_json("/auth/me", save_as="state/environment-auth-me.json")
        cookies = ctx.page.context.cookies(ctx.config.base_url)
        sessions = [row for row in cookies if row.get("httpOnly")]
        expected_auth = ctx.expected.get("auth_disabled")
        if expected_auth is not None:
            assert me.get("auth_disabled") is bool(expected_auth), {
                "expected_auth_disabled": bool(expected_auth),
                "observed_auth_disabled": me.get("auth_disabled"),
            }
        if not me.get("auth_disabled"):
            assert me.get("uid") == ctx.config.admin_user and me.get("role") == "admin", me
            assert sessions, "authenticated real browser has no HttpOnly session cookie"
        return {
            "source": "GET /auth/me and real browser cookie jar",
            "auth_disabled": me.get("auth_disabled"),
            "uid_matches_runner_admin": me.get("uid") == ctx.config.admin_user,
            "role": me.get("role"),
            "http_only_session_count": len(sessions),
            "secure_flags": [bool(row.get("secure")) for row in sessions],
            "same_site_flags": [row.get("sameSite") for row in sessions],
        }

    return ctx.cached("auth", collect)


def _database_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ModuleNotFoundError as exc:
            raise AssertionError("psycopg is required for real database identity evidence") from exc
        with psycopg.connect(ctx.config.database_url, row_factory=dict_row, connect_timeout=5) as conn:
            client_host = str(conn.info.host or "")
            client_port = int(conn.info.port)
            client_database = str(conn.info.dbname or "")
            client_user = str(conn.info.user or "")
            row = conn.execute(
                "SELECT current_database() AS database, current_user AS username, "
                "inet_server_addr()::text AS address, inet_server_port() AS server_port"
            ).fetchone()
            assert row
        runner = _load_json(_profile_root(ctx) / "state" / "postgres-identity.json")
        assert str(row["database"]) == str(runner.get("database"))
        assert str(row["server_port"]) == str(runner.get("port"))
        assert int(row["server_port"]) == 5432, "PostgreSQL server did not report its container-internal port"
        expected_public_port = int(urlsplit(ctx.config.database_url).port or 5432)
        assert client_port == expected_public_port, {
            "configured_public_port": expected_public_port,
            "psycopg_connection_port": client_port,
        }
        return {
            "source": "direct psycopg identity query and runner store identity",
            "database": row["database"],
            "username": row["username"],
            "address": row["address"],
            "client_host": client_host,
            "client_port": client_port,
            "client_database": client_database,
            "client_user": client_user,
            "server_internal_port": row["server_port"],
            "runner_server_internal_port": int(runner["port"]),
            "runner_compose_project": runner.get("compose_project"),
        }

    return ctx.cached("database", collect)


def _browser_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        ctx.page.goto(ctx.config.base_url + "/ui/home.html")
        body = ctx.page.locator("body")
        assert body.is_visible(), "real Sherpa UI body is not visible"
        topbar = ctx.page.locator("sherpa-topbar")
        assert topbar.is_visible(), "real Sherpa UI navigation is not visible"
        ctx.page.locator("#sherpa-nav a").first.wait_for(state="visible")
        navigation = ctx.page.locator("#sherpa-nav a").evaluate_all(
            "links => links.map(link => ({href: link.getAttribute('href'), label: link.textContent.trim()}))"
        )
        expected_hrefs = {
            "home.html",
            "chat.html",
            "ingest.html",
            "graph.html",
            "manual.html",
            "settings.html",
        }
        observed_hrefs = {str(row.get("href") or "") for row in navigation}
        assert expected_hrefs <= observed_hrefs, "real top navigation is missing required destinations: " + ", ".join(
            sorted(expected_hrefs - observed_hrefs)
        )
        assert all(row.get("label") for row in navigation), "real navigation contains an empty label"
        assert all(not href.lower().startswith("javascript:") for href in observed_hrefs)
        version = ctx.page.evaluate("navigator.userAgent")
        return {
            "source": "Chromium DOM and navigator",
            "url": ctx.page.url,
            "title": ctx.page.title(),
            "topbar_visible": True,
            "navigation": navigation,
            "browser_sha256": _sha256(str(version)),
            "document_language": ctx.page.locator("html").get_attribute("lang"),
        }

    return ctx.cached("browser", collect)


def _navigation_observation(ctx: EnvironmentProbeContext) -> dict:
    browser = _browser_observation(ctx)
    settings_authorization = ctx.evidence.arm_control_authorization(
        ctx.page,
        control_key="@href:settings.html",
    )
    assert settings_authorization["status"] == 200 and settings_authorization["role"] == "admin"
    ctx.page.locator("#sherpa-nav a[href='settings.html']").click()
    ctx.page.wait_for_url("**/ui/settings.html")
    settings_url = ctx.page.url
    assert ctx.page.locator("sherpa-topbar").is_visible()
    home_authorization = ctx.evidence.arm_control_authorization(
        ctx.page,
        control_key="@href:home.html",
    )
    assert home_authorization["status"] == 200 and home_authorization["role"] == "admin"
    ctx.page.locator("#sherpa-nav a[href='home.html']").click()
    ctx.page.wait_for_url("**/ui/home.html")
    return {
        "source": "two real top-navigation link activations and resulting documents",
        "declared_navigation": browser["navigation"],
        "settings_path": urlsplit(settings_url).path,
        "home_path": urlsplit(ctx.page.url).path,
    }


def _admin_page_observation(ctx: EnvironmentProbeContext) -> dict:
    ctx.page.goto(ctx.config.base_url + "/ui/admin-settings.html")
    ctx.page.locator("#main-content").wait_for(state="visible")
    denied = ctx.page.locator("#access-denied")
    assert denied.count() == 1 and not denied.is_visible(), "the isolated administrator was denied by the real admin page"
    me = ctx.api.get_json("/auth/me", save_as="state/environment-admin-page-auth.json")
    assert me.get("role") == "admin", "admin-page observation was not made as an administrator"
    assert urlsplit(ctx.page.url).path == "/ui/admin-settings.html"
    assert ctx.page.locator("#cloud-card").is_visible(), "central AI settings are absent from the real administrator page"
    return {
        "source": "real admin settings DOM and independently authenticated GET /auth/me",
        "path": urlsplit(ctx.page.url).path,
        "role": me.get("role"),
        "main_visible": True,
        "access_denied_visible": False,
        "central_ai_settings_visible": True,
    }


def _status_page_observation(ctx: EnvironmentProbeContext) -> dict:
    ctx.page.goto(ctx.config.base_url + "/ui/status.html")
    ctx.page.locator("#status-pill").wait_for(state="visible")
    ctx.page.wait_for_function(
        "() => document.querySelector('#status-pill')?.textContent.trim() !== '確認中…' "
        "&& document.querySelectorAll('#health-tbody tr').length > 0"
    )
    detail = ctx.api.get_json("/admin/health", save_as="state/environment-admin-health.json")
    status = str(detail.get("status") or "")
    expected_label = {
        "ok": "正常",
        "degraded": "一部機能制限",
        "down": "停止",
    }.get(status)
    assert expected_label is not None, f"real admin health returned an unknown status: {status}"
    observed_label = ctx.page.locator("#status-pill").inner_text().strip()
    assert observed_label == expected_label, {
        "admin_health_status": status,
        "ui_status_label": observed_label,
    }
    components = detail.get("components") or []
    assert isinstance(components, list) and components, "real admin health returned no component observations"
    row_count = ctx.page.locator("#health-tbody tr").count()
    assert row_count == len(components), {
        "admin_health_component_count": len(components),
        "ui_component_row_count": row_count,
    }
    parsed = urlsplit(ctx.page.url)
    expected_origin = urlsplit(ctx.config.base_url)
    assert parsed.path == "/ui/status.html"
    assert (parsed.scheme, parsed.hostname, parsed.port) == (
        expected_origin.scheme,
        expected_origin.hostname,
        expected_origin.port,
    )
    return {
        "source": "real status-page DOM correlated with independent GET /admin/health",
        "path": parsed.path,
        "same_runner_origin": True,
        "status": status,
        "label": observed_label,
        "component_count": len(components),
        "rendered_row_count": row_count,
    }


def _process_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        pid_file = Path(os.environ.get("APP_PID_FILE", ""))
        assert pid_file.is_file(), f"runner-owned application PID file is absent: {pid_file}"
        pid_text = pid_file.read_text(encoding="utf-8").strip()
        assert pid_text.isdigit(), f"application PID is invalid: {pid_file}"
        process_path = Path("/proc") / pid_text
        assert process_path.is_dir(), f"application process {pid_text} is not alive"
        assert pid_file.stat().st_uid == os.getuid(), "application PID file is not owned by the isolated test user"
        assert process_path.stat().st_uid == os.getuid(), "application process is not owned by the isolated test user"
        python_bin = Path(os.environ.get("PYTHON_BIN", ""))
        assert python_bin.is_file(), f"configured Python executable is absent: {python_bin}"
        app_port = int(urlsplit(ctx.config.base_url).port or 0)
        resolved = {
            "python": shutil.which(str(python_bin)),
            "codex": shutil.which("codex"),
            "soffice": shutil.which(os.environ.get("SHERPA_SOFFICE_BIN") or "soffice"),
        }
        expected_ports = ctx.expected.get("ports") or {}
        assert isinstance(expected_ports, dict), "expected ports contract must be an object"
        observed_ports = {
            "SHERPA_PORT": str(app_port),
            "PGPORT": str(urlsplit(ctx.config.database_url).port or ""),
            "SHERPA_ES_PORT": str(urlsplit(os.environ.get("ES_URL", "")).port or ""),
            "SHERPA_NEO4J_BOLT_PORT": str(urlsplit(os.environ.get("NEO4J_URI", "")).port or ""),
            "SHERPA_NEO4J_HTTP_PORT": os.environ.get("SHERPA_NEO4J_HTTP_PORT", ""),
        }
        assert set(expected_ports) == set(observed_ports), {
            "missing_expected_port_contracts": sorted(set(observed_ports) - set(expected_ports)),
            "unexpected_expected_port_contracts": sorted(set(expected_ports) - set(observed_ports)),
        }
        for name, actual in observed_ports.items():
            assert actual == str(expected_ports[name]), {
                "port": name,
                "expected": expected_ports[name],
                "observed": actual,
            }
            assert os.environ.get(name) == actual, {
                "port": name,
                "process_environment": os.environ.get(name),
                "observed_endpoint": actual,
            }
            with socket.create_connection(("127.0.0.1", int(actual)), timeout=3):
                pass
        locale_value = os.environ.get("LANG")
        scenario = _generated_scenario(ctx)
        if scenario.get("variable") == "LANG":
            process_contract = scenario.get("process") or {}
            mode = str(process_contract.get("mode") or "")
            if mode == "unset":
                assert locale_value is None
            elif mode not in {"absent", ""}:
                assert locale_value == process_contract.get("value")
        return {
            "source": "runner PID file, procfs, executable resolution, and TCP connect",
            "pid": int(pid_text),
            "process_alive": True,
            "pid_file_name": pid_file.name,
            "pid_file_sha256": _sha256(str(pid_file.resolve())),
            "pid_file_mode": oct(pid_file.stat().st_mode & 0o777),
            "same_user_owner": True,
            "application_port": app_port,
            "application_port_open": True,
            "resolved_commands": {key: bool(value) for key, value in resolved.items()},
            "locale": locale_value,
            "observed_ports": observed_ports,
            "all_expected_ports_open": True,
        }

    return ctx.cached("process", collect)


def _log_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        service_root = _profile_root(ctx) / "services"
        candidates = sorted(service_root.glob("*.log"))
        nonempty = [path for path in candidates if path.is_file() and path.stat().st_size > 0]
        assert nonempty, f"runner captured no nonempty real service logs in {service_root}"
        combined = "\n".join(path.read_text(encoding="utf-8", errors="replace")[-20000:] for path in nonempty)
        return {
            "source": "runner-owned real service log files",
            "files": [path.name for path in nonempty],
            "bytes": sum(path.stat().st_size for path in nonempty),
            "contains_startup_record": bool(combined.strip()),
        }

    return ctx.cached("logs", collect)


def _filesystem_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        profile_root = _profile_root(ctx)
        fixture_hashes = _load_json(profile_root / "state" / "fixture-files.sha256.json")
        env_file = Path(os.environ.get("SHERPA_ENV_FILE", ""))
        required_dirs = {
            key: Path(os.environ.get(key, ""))
            for key in (
                "HOME",
                "RUN_DIR",
                "SHERPA_KB_DIR",
                "SHERPA_USERS_DIR",
                "SHERPA_DERIVED_DIR",
                "SHERPA_OBSERVATION_DIR",
            )
        }
        missing = [key for key, path in required_dirs.items() if not path.is_dir()]
        assert not missing, "runner-owned data directories are absent: " + ", ".join(missing)
        assert env_file.is_file(), f"explicit SHERPA_ENV_FILE is absent: {env_file}"
        roots = [path.resolve() for path in required_dirs.values()]
        assert len(roots) == len(set(roots)), "isolated filesystem roots unexpectedly alias each other"
        return {
            "source": "real filesystem, explicit env file, and runner fixture hashes",
            "env_file_mode": oct(env_file.stat().st_mode & 0o777),
            "directory_count": len(required_dirs),
            "directories": {key: {"exists": path.is_dir(), "owner": path.stat().st_uid} for key, path in required_dirs.items()},
            "fixture_file_count": len(fixture_hashes),
        }

    return ctx.cached("filesystem", collect)


def _compose_observation(ctx: EnvironmentProbeContext) -> dict:
    database = _database_observation(ctx)
    project = str(database.get("runner_compose_project") or "")
    assert project.startswith("sherpa-ui-automation-"), "database identity has no runner-owned Compose project"
    for key in ("COMPOSE_PROJECT_NAME", "SHERPA_COMPOSE_PROJECT"):
        assert os.environ.get(key) == project, f"{key} does not name the runner-owned Compose project"
    identities = {
        name: _load_json(_profile_root(ctx) / "state" / f"{name}-identity.json") for name in ("postgres", "elasticsearch", "neo4j")
    }
    assert all(value.get("compose_project") == project for value in identities.values()), (
        "store identities do not share the runner-owned Compose project"
    )
    verify_local_docker_environment(dict(os.environ))
    completed = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            '{{.ID}}\t{{.Label "com.docker.compose.project"}}',
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, "docker could not inspect the isolated Compose processes"
    container_rows = [line.split("\t", 1) for line in completed.stdout.splitlines() if line]
    assert len(container_rows) >= 3, "fewer than three isolated store containers are running"
    assert all(len(row) == 2 and row[1] == project for row in container_rows), (
        "a running container has a Compose project label outside the isolated run"
    )
    compose_log = _profile_root(ctx) / "services" / "compose-up.log"
    assert compose_log.is_file() and compose_log.stat().st_size > 0
    return {
        "source": "docker process labels, direct store identities, and Compose service log",
        "project": project,
        "store_identity_count": len(identities),
        "running_container_count": len(container_rows),
        "container_id_sha256": sorted(_sha256(row[0]) for row in container_rows),
        "compose_log_bytes": compose_log.stat().st_size,
    }


def _pairwise_factor_values(ctx: EnvironmentProbeContext) -> dict[str, dict]:
    expected = ctx.expected.get("pairwise") or {}
    factors = expected.get("factors") or []
    result = {str(factor.get("key")): factor for factor in factors if isinstance(factor, dict) and factor.get("key")}
    assert result, "pairwise semantic observation has no factor contract"
    return result


def _literal_pairwise_value(factors: dict[str, dict], key: str) -> str:
    factor = factors.get(key) or {}
    assert factor.get("expectation") == "literal" and "value" in factor, (
        f"pairwise semantic observation requires a non-secret literal for {key}"
    )
    return str(factor["value"])


def _pairwise_connection_effects(ctx: EnvironmentProbeContext) -> dict:
    factors = _pairwise_factor_values(ctx)
    process = _process_observation(ctx)
    database = _database_observation(ctx)
    compose = _compose_observation(ctx)
    neo4j = _graph_store_observation(ctx)
    health = _health_observation(ctx)
    env_file = Path(_literal_pairwise_value(factors, "SHERPA_ENV_FILE"))
    assert env_file.resolve() == Path(os.environ["SHERPA_ENV_FILE"]).resolve()
    assert env_file.is_file(), "pairwise SHERPA_ENV_FILE does not identify the consumed file"
    app_port = int(_literal_pairwise_value(factors, "SHERPA_PORT"))
    assert urlsplit(ctx.config.base_url).port == app_port
    database_url = _literal_pairwise_value(factors, "DATABASE_URL")
    assert database_url == ctx.config.database_url == os.environ.get("DATABASE_URL")
    assert urlsplit(database_url).port == int(database["client_port"])
    neo4j_uri = _literal_pairwise_value(factors, "NEO4J_URI")
    assert neo4j_uri == os.environ.get("NEO4J_URI")
    assert urlsplit(neo4j_uri).port == int(process["observed_ports"]["SHERPA_NEO4J_BOLT_PORT"])
    es_url = _literal_pairwise_value(factors, "ES_URL")
    assert es_url == os.environ.get("ES_URL")
    es_identity = _es_request(ctx, "GET", "/")
    assert es_identity.get("cluster_name") and es_identity.get("version"), es_identity
    return {
        "source": ("real env-file path, app health/TCP listener, direct Postgres, Elasticsearch, Neo4j, and Compose identity observations"),
        "effects": {
            "SHERPA_ENV_FILE": {
                "consumed_path_sha256": _sha256(str(env_file.resolve())),
                "exists": True,
            },
            "SHERPA_PORT": {
                "base_url_port": app_port,
                "tcp_open": process["all_expected_ports_open"],
                "health_ok": health["healthz_ok"],
            },
            "DATABASE_URL": {
                "connected_database": database["database"],
                "public_connection_port": database["client_port"],
                "server_internal_port": database["server_internal_port"],
            },
            "NEO4J_URI": {
                "connected_component": neo4j["component"],
                "connected_versions": neo4j["versions"],
            },
            "ES_URL": {
                "cluster_name": es_identity["cluster_name"],
                "version": (es_identity.get("version") or {}).get("number"),
            },
        },
        "compose_project": compose["project"],
    }


def _pairwise_auth_effects(ctx: EnvironmentProbeContext) -> dict:
    factors = _pairwise_factor_values(ctx)
    auth_disabled = _bool(_literal_pairwise_value(factors, "SHERPA_AUTH_DISABLED"))
    cookie_secure = _bool(_literal_pairwise_value(factors, "SHERPA_COOKIE_SECURE"))
    session_days = int(_literal_pairwise_value(factors, "SHERPA_SESSION_DAYS"))
    auth = _auth_observation(ctx)
    assert auth["auth_disabled"] is auth_disabled
    assert all(flag is cookie_secure for flag in auth["secure_flags"]), "browser session Secure flags differ from SHERPA_COOKIE_SECURE"
    if cookie_secure and not auth_disabled and urlsplit(ctx.config.base_url).scheme == "http":
        assert auth.get("cookie_transmitted_over_http") is False
        assert auth.get("anonymous_me_status") == 401
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for pairwise session lifetime evidence") from exc
    with psycopg.connect(ctx.config.database_url, row_factory=dict_row, connect_timeout=5) as conn:
        sessions = conn.execute(
            "SELECT id,created_at,expires_at,revoked_at FROM auth_sessions WHERE user_id=%s ORDER BY id",
            (ctx.config.admin_user,),
        ).fetchall()
    active = [dict(row) for row in sessions if row.get("revoked_at") is None]
    if auth_disabled:
        assert not active and auth["http_only_session_count"] == 0, "auth-disabled pairwise row unexpectedly issued a real session"
        lifetime_seconds = None
    else:
        assert len(active) == 1 and auth["http_only_session_count"] == 1, (
            "auth-enabled pairwise row did not issue exactly one browser/database session"
        )
        lifetime_seconds = (active[0]["expires_at"] - active[0]["created_at"]).total_seconds()
        assert abs(lifetime_seconds - session_days * 86400) < 5, "database session expiry differs from SHERPA_SESSION_DAYS"
    return {
        "source": "real login cookie attributes, HTTP transmission boundary, and direct auth_sessions query",
        "effects": {
            "SHERPA_AUTH_DISABLED": {
                "auth_disabled": auth_disabled,
                "session_suppressed": auth_disabled,
            },
            "SHERPA_COOKIE_SECURE": {
                "expected_secure": cookie_secure,
                "browser_secure_flags": auth["secure_flags"],
                "transmitted_over_http": auth.get("cookie_transmitted_over_http"),
            },
            "SHERPA_SESSION_DAYS": {
                "configured_days": session_days,
                "database_lifetime_seconds": lifetime_seconds,
                "not_applicable_without_session": auth_disabled,
            },
        },
    }


def _pairwise_ingest_effects(ctx: EnvironmentProbeContext) -> dict:
    factors = _pairwise_factor_values(ctx)
    expected_arms = [value.strip() for value in _literal_pairwise_value(factors, "SHERPA_ARMS").split(",") if value.strip()]
    ocr_enabled = _bool(_literal_pairwise_value(factors, "SHERPA_OCR_ENABLED"))
    expected_vlm_provider = _literal_pairwise_value(factors, "SHERPA_VLM_PROVIDER")
    expected_legacy = _literal_pairwise_value(factors, "SHERPA_LEGACY_BACKEND")
    admin = ctx.api.get_json("/admin/settings", save_as="state/pairwise-ingest-admin-settings.json")
    arms = admin.get("arms") or {}
    legacy = admin.get("legacy_backend") or {}
    vlm = admin.get("vlm") or {}
    vlm_effective = vlm.get("effective") or {}
    assert arms.get("enabled") == expected_arms, "real ingest arm registry differs from the pairwise SHERPA_ARMS factor"
    assert legacy.get("effective") == expected_legacy, "real legacy converter differs from the pairwise backend factor"
    assert str(vlm_effective.get("provider") or "") == expected_vlm_provider, (
        "real VLM resolution differs from the pairwise provider factor"
    )
    expected_vlm_model = str(vlm_effective.get("model") or "")
    assert expected_vlm_model, "pairwise VLM resolution omitted its real model identity"

    world = _world_observation(ctx)
    world_id = ctx.fixture("real_world")
    preview = ctx.api.get_json(
        LiveApi.query("/ingest/preview", world=world_id),
        save_as="state/pairwise-ingest-preview.json",
    )
    documents = {str(row.get("name") or ""): row for row in preview.get("documents") or []}
    required_methods = {
        "office/tax-evidence.docx": "ooxml",
        "office/tax-cases.xlsx": "ooxml",
        "office/nightly-operations.pptx": "ooxml",
        "media/text-evidence.pdf": "pdf_text",
    }
    for rel_path, method in required_methods.items():
        row = documents.get(rel_path) or {}
        assert row.get("state") == "ready" and (row.get("provenance") or {}).get("method") == method, (
            f"pairwise ingest lost required {method} output for {rel_path}"
        )

    legacy_row = documents.get("legacy/legacy-note.doc") or {}
    legacy_provenance = legacy_row.get("provenance") or {}
    if expected_legacy == "libreoffice":
        assert legacy_row.get("state") == "ready"
        assert legacy_provenance.get("method") == "ooxml"
        assert legacy_provenance.get("legacy_backend") == "libreoffice"
        legacy_converted = True
    else:
        assert legacy_provenance.get("method") == "legacy_source_notice", (
            "disabled legacy backend did not publish an explicit unsupported notice"
        )
        legacy_converted = False

    vision_enabled = "vision" in expected_arms
    vlm_available = bool(vlm.get("available"))
    vision_row = documents.get("media/vlm-evidence.bmp") or {}
    vision_method = str((vision_row.get("provenance") or {}).get("method") or "")
    vision_expected = vision_enabled and vlm_available
    assert (vision_method == "vision") is vision_expected, "real image conversion does not match the arms and VLM availability interaction"

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for pairwise OCR evidence") from exc
    # Paddle OCRのrefresh/enqueueはVLMのvision armとは独立している。
    # ``OCR enabled + vision armなし`` でも実jobが必要であり、両者を結合すると
    # job欠落を正しく検出できない。
    ocr_jobs_expected = bool(ocr_enabled)
    paddle_executed = False
    if ocr_jobs_expected:
        snapshot = wait_for_ingestion_database_snapshot(
            ctx.config.database_url,
            world_id,
            ctx.evidence,
            timeout_seconds=max(ctx.config.timeout_ms / 1000, 180),
        )
        jobs = snapshot.get("ocr_jobs") or []
        paddle_jobs = [row for row in jobs if str(row.get("source_rel_path") or "").endswith("media/ocr-evidence.png")]
        assert paddle_jobs and all(row.get("status") == "succeeded" for row in paddle_jobs)
        assert any(str((row.get("result_payload") or {}).get("provider") or "").lower() == "paddleocr" for row in paddle_jobs), (
            "OCR-enabled pairwise row did not execute the real Paddle provider"
        )
        paddle_executed = True
    else:
        with psycopg.connect(ctx.config.database_url, row_factory=dict_row, connect_timeout=5) as connection:
            jobs = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,source_rel_path,status,result_payload FROM ocr_jobs WHERE world=%s ORDER BY id",
                    (world_id,),
                ).fetchall()
            ]
        assert not jobs, "pairwise interaction disabled OCR routing but persisted real OCR jobs"

    if vision_expected:
        with psycopg.connect(ctx.config.database_url, row_factory=dict_row, connect_timeout=5) as connection:
            usage = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,provider,model,input_tokens,output_tokens,calls FROM usage_events WHERE kind='vlm' ORDER BY id",
                ).fetchall()
            ]
        matching_vlm = [
            row
            for row in usage
            if str(row.get("provider") or "") == expected_vlm_provider
            and str(row.get("model") or "") == expected_vlm_model
            and int(row.get("calls") or 0) > 0
        ]
        assert matching_vlm, "vision conversion has no matching real VLM usage event"
        for row in matching_vlm:
            ctx.evidence.record_usage_event(
                row,
                turn_id=f"pairwise-vlm:{row['id']}",
                operation="pairwise-vlm",
            )
    else:
        matching_vlm = []

    return {
        "source": ("real admin ingest settings, World conversion preview, OCR job database, and VLM usage database"),
        "world_id_sha256": _sha256(world_id),
        "effects": {
            "SHERPA_ARMS": {
                "enabled": expected_arms,
                "ooxml_outputs": len(required_methods) - 1,
                "pdf_text_outputs": 1,
                "vision_output": vision_method == "vision",
            },
            "SHERPA_OCR_ENABLED": {
                "enabled": ocr_enabled,
                "vision_route_available": vision_expected,
                "job_count": len(jobs),
                "paddle_executed": paddle_executed,
            },
            "SHERPA_VLM_PROVIDER": {
                "provider": expected_vlm_provider,
                "model": expected_vlm_model,
                "available": vlm_available,
                "real_usage_event_count": len(matching_vlm),
            },
            "SHERPA_LEGACY_BACKEND": {
                "backend": expected_legacy,
                "converted": legacy_converted,
                "method": legacy_provenance.get("method"),
            },
        },
        "indexed_documents": world["indexed"],
    }


def _pairwise_positive_agent_turn(ctx: EnvironmentProbeContext, *, expected_agent: str, evidence_name: str) -> dict:
    settings = ctx.api.get_json("/settings", save_as=f"state/{evidence_name}-settings.json")
    assert settings.get("agent") == expected_agent
    world_id = ctx.fixture("real_world")
    prepare_chat(ctx.page, ctx.config, world_id)
    started = start_turn_from_ui(
        ctx.page,
        ctx.config,
        "SHERPA-LIVE-REFERENCE-314 の運用時刻を実資料と実ツールで確認してください。",
    )
    conversation_id = int(started["conversation_id"])
    turn_id = str(started["turn_id"])
    ctx.evidence.add_cleanup(
        f"delete {evidence_name} conversation {conversation_id}",
        lambda: _cleanup_conversation(ctx.api, conversation_id),
    )
    events = ctx.api.collect_sse(
        f"/chat/turns/{turn_id}/stream?cursor=0",
        save_as=f"network/{evidence_name}-sse.jsonl",
    )
    answer_event(events)
    wait_for_completed_ui(ctx.page, ctx.config.timeout_ms)
    conversation = ctx.api.get_json(
        f"/conversations/{conversation_id}",
        save_as=f"state/{evidence_name}-conversation.json",
    )
    assistant = last_assistant_message(conversation)
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
        operation=evidence_name,
    )
    assert correlation["provider"] == expected_agent, f"pairwise turn executed {correlation['provider']!r}, expected {expected_agent!r}"
    return correlation


def _pairwise_unwired_openai_turn(ctx: EnvironmentProbeContext) -> dict:
    settings = ctx.api.get_json("/settings", save_as="state/pairwise-main-openai-unwired-settings.json")
    assert settings.get("agent") == "openai"
    assert settings.get("openai_key_set") is False
    world_id = ctx.fixture("real_world")
    prepare_chat(ctx.page, ctx.config, world_id)
    started = start_turn_from_ui(
        ctx.page,
        ctx.config,
        "実OpenAI接続が未設定なら、その状態を成功回答にせず明示してください。",
    )
    conversation_id = int(started["conversation_id"])
    turn_id = str(started["turn_id"])
    ctx.evidence.add_cleanup(
        f"delete unwired OpenAI conversation {conversation_id}",
        lambda: _cleanup_conversation(ctx.api, conversation_id),
    )
    events = ctx.api.collect_sse(
        f"/chat/turns/{turn_id}/stream?cursor=0",
        save_as="network/pairwise-main-openai-unwired-sse.jsonl",
    )
    answer_event(events)
    wait_for_completed_ui(ctx.page, ctx.config.timeout_ms)
    conversation = ctx.api.get_json(
        f"/conversations/{conversation_id}",
        save_as="state/pairwise-main-openai-unwired-conversation.json",
    )
    assistant = last_assistant_message(conversation)
    answer = assistant.get("answer") or {}
    headline = str(answer.get("headline") or assistant.get("content") or "")
    usage = answer.get("usage")
    assert "管理者が AI プロバイダのキーを設定してください" in headline
    assert not isinstance(usage, dict) or not usage.get("provider"), "unwired OpenAI response falsely reported a real provider invocation"
    database = conversation_database_snapshot(
        ctx.config.database_url,
        conversation_id,
        ctx.evidence,
        turn_id=turn_id,
    )
    assistant_rows = [row for row in database["messages"] if row.get("role") == "assistant"]
    assert assistant_rows and assistant_rows[-1].get("content") == assistant.get("content")
    assert any(row.get("action") == "chat.turn" for row in database["audit"])
    observation = {
        "turn_id_sha256": _sha256(turn_id),
        "conversation_id_sha256": _sha256(str(conversation_id)),
        "honest_unwired_message": True,
        "provider_usage_absent": True,
        "database_persisted": True,
        "audit_persisted": True,
    }
    ctx.evidence.write_json("state/pairwise-main-openai-unwired-observed.json", observation)
    return observation


def _restore_pairwise_ai_settings(ctx: EnvironmentProbeContext, user: dict, admin: dict) -> None:
    ctx.api.put_json(
        "/admin/settings",
        {"cloud_provider": (admin.get("cloud") or {}).get("provider")},
    )
    ctx.api.put_json("/settings", {"agent": user.get("agent")})


def _pairwise_ai_selection_effects(ctx: EnvironmentProbeContext) -> dict:
    factors = _pairwise_factor_values(ctx)
    expected_agent = _literal_pairwise_value(factors, "SHERPA_AGENT")
    extra_agent = _literal_pairwise_value(factors, "SHERPA_EXTRA_AGENTS")
    expected_ollama_url = _literal_pairwise_value(factors, "OLLAMA_URL")
    openai_factor = factors.get("OPENAI_API_KEY") or {}
    assert openai_factor.get("expectation") in {"set", "unset"}
    openai_expected = openai_factor.get("expectation") == "set"
    user_before = ctx.api.get_json("/settings", save_as="state/pairwise-ai-settings-before.json")
    admin_before = ctx.api.get_json("/admin/settings", save_as="state/pairwise-ai-admin-settings-before.json")
    ctx.evidence.add_cleanup(
        "restore pairwise main and cloud provider settings",
        lambda: _restore_pairwise_ai_settings(ctx, user_before, admin_before),
    )
    initial_cloud = admin_before.get("cloud") or {}
    assert str(initial_cloud.get("ollama_url") or "") == expected_ollama_url, (
        "central Ollama seed differs from the pairwise OLLAMA_URL factor"
    )
    assert bool(initial_cloud.get("openai_key_set")) is openai_expected

    ctx.api.put_json("/admin/settings", {"cloud_provider": "openai"})
    main_settings = ctx.api.put_json("/settings", {"agent": expected_agent})
    assert main_settings.get("agent") == expected_agent
    if expected_agent == "openai" and not openai_expected:
        main_turn = _pairwise_unwired_openai_turn(ctx)
        main_execution = "honest-unwired"
        main_provider = None
    else:
        main_turn = _pairwise_positive_agent_turn(
            ctx,
            expected_agent=expected_agent,
            evidence_name="pairwise-main-agent",
        )
        main_execution = "real-provider"
        main_provider = main_turn["provider"]

    ollama_probe = ctx.api.post_json(
        "/settings/test",
        {"provider": "ollama", "ollama_url": expected_ollama_url},
        save_as="state/pairwise-ollama-connection.json",
    )
    assert ollama_probe.get("provider") == "ollama" and ollama_probe.get("ok") is True, (
        "pairwise OLLAMA_URL did not pass a real model connection probe"
    )

    assert extra_agent in {"gemini", "bedrock"}, "pairwise additional provider must be a product-gated real provider"
    if extra_agent == "gemini":
        assert bool(initial_cloud.get("gemini_key_set")), "Gemini pairwise row requires a real GEMINI_API_KEY seeded into central settings"
    else:
        sigv4_present = bool(
            (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")) or os.environ.get("AWS_PROFILE")
        )
        assert bool(initial_cloud.get("bedrock_key_set")) or sigv4_present, (
            "Bedrock pairwise row requires a real bearer key or SigV4 credential chain"
        )
    extra_admin = ctx.api.put_json("/admin/settings", {"cloud_provider": extra_agent})
    assert (extra_admin.get("cloud") or {}).get("provider") == extra_agent
    extra_settings = ctx.api.put_json("/settings", {"agent": extra_agent})
    assert extra_settings.get("agent") == extra_agent
    extra_probe = ctx.api.post_json(
        "/settings/test",
        {"provider": extra_agent},
        save_as="state/pairwise-extra-provider-connection.json",
    )
    assert extra_probe.get("provider") == extra_agent and extra_probe.get("ok") is True, (
        f"pairwise additional provider {extra_agent} failed its real connection probe"
    )
    extra_turn = _pairwise_positive_agent_turn(
        ctx,
        expected_agent=extra_agent,
        evidence_name="pairwise-extra-agent",
    )
    return {
        "source": (
            "real central and personal settings, main and additional provider connection probes, "
            "real UI/SSE/Postgres/audit/usage turns, and Ollama model probe"
        ),
        "effects": {
            "SHERPA_AGENT": {
                "configured": expected_agent,
                "execution": main_execution,
                "executed_provider": main_provider,
            },
            "SHERPA_EXTRA_AGENTS": {
                "configured": extra_agent,
                "connection_ok": True,
                "executed_provider": extra_turn["provider"],
                "nonzero_tokens": (extra_turn["input_tokens"] + extra_turn["output_tokens"] > 0),
            },
            "OPENAI_API_KEY": {
                "expected_present": openai_expected,
                "settings_key_present": bool(initial_cloud.get("openai_key_set")),
                "openai_main_honest_unwired": (expected_agent == "openai" and not openai_expected),
            },
            "OLLAMA_URL": {
                "endpoint_host": urlsplit(expected_ollama_url).hostname,
                "endpoint_port": urlsplit(expected_ollama_url).port,
                "real_connection_ok": True,
                "model": ollama_probe.get("model"),
            },
        },
    }


def _process_parent_pid(pid: int) -> int | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    suffix = stat.rsplit(")", 1)
    if len(suffix) != 2:
        return None
    fields = suffix[1].strip().split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _process_is_descendant(pid: int, ancestor: int, parents: dict[int, int | None]) -> bool:
    seen: set[int] = set()
    current: int | None = pid
    while current is not None and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


def _safe_codex_invocation_snapshot(app_pid: int) -> dict | None:
    proc_root = Path("/proc")
    pids = [int(path.name) for path in proc_root.iterdir() if path.name.isdigit()]
    parents = {pid: _process_parent_pid(pid) for pid in pids}
    descendants = [pid for pid in pids if pid != app_pid and _process_is_descendant(pid, app_pid, parents)]
    mcp_child_seen = False
    codex_rows: list[tuple[int, list[str]]] = []
    for pid in descendants:
        try:
            raw = (proc_root / str(pid) / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        if not argv:
            continue
        if "sherpa.mcp_server" in argv:
            mcp_child_seen = True
        if "exec" in argv and "--json" in argv and any("codex" in Path(value).name.lower() for value in argv[:2]):
            codex_rows.append((pid, argv))
    if not codex_rows:
        return None
    pid, argv = codex_rows[0]
    config_text = ""
    codex_home = ""
    try:
        environ = (proc_root / str(pid) / "environ").read_bytes().split(b"\0")
    except OSError:
        environ = []
    for item in environ:
        if item.startswith(b"CODEX_HOME="):
            codex_home = item.split(b"=", 1)[1].decode("utf-8", errors="replace")
            break
    if codex_home:
        config_path = Path(codex_home) / "config.toml"
        try:
            config_text = config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            config_text = ""
    config_arguments = [value for value in argv if value.startswith(("mcp_servers.", "web_search="))]
    return {
        "strict_config": "--strict-config" in argv,
        "ephemeral": "--ephemeral" in argv,
        "workspace_write": any(argv[index : index + 2] == ["-s", "workspace-write"] for index in range(max(0, len(argv) - 1))),
        "resume": "resume" in argv,
        "mcp_configured": (
            "[mcp_servers.sherpa]" in config_text or any(value.startswith("mcp_servers.sherpa.") for value in config_arguments)
        ),
        "mcp_child_seen": mcp_child_seen,
        "web_search_disabled": (
            re.search(r'^web_search\s*=\s*["\']disabled["\']', config_text, re.MULTILINE) is not None
            or any("web_search=" in value and "disabled" in value for value in config_arguments)
        ),
        "config_observed": bool(config_text),
        "codex_home_sha256": _sha256(codex_home) if codex_home else None,
        "raw_argv_persisted": False,
        "raw_config_persisted": False,
    }


def _capture_codex_invocation(app_pid: int, timeout_seconds: float, *, expect_mcp: bool) -> dict:
    deadline = time.monotonic() + timeout_seconds
    latest: dict | None = None
    while time.monotonic() < deadline:
        snapshot = _safe_codex_invocation_snapshot(app_pid)
        if snapshot is not None:
            latest = snapshot
            if not expect_mcp or snapshot["mcp_child_seen"] or snapshot["config_observed"]:
                break
        time.sleep(0.02)
    assert latest is not None, "real Codex subprocess was not observed under the Sherpa app process"
    return latest


def _pairwise_codex_effects(ctx: EnvironmentProbeContext) -> dict:
    factors = _pairwise_factor_values(ctx)
    mcp_enabled = _bool(_literal_pairwise_value(factors, "SHERPA_CODEX_MCP"))
    sandbox_enabled = _bool(_literal_pairwise_value(factors, "SHERPA_CODEX_SANDBOX"))
    web_enabled = _bool(_literal_pairwise_value(factors, "SHERPA_ALLOW_WEB_SEARCH"))
    before = ctx.api.get_json("/settings", save_as="state/pairwise-codex-settings-before.json")
    ctx.evidence.add_cleanup(
        "restore pairwise Codex personal settings",
        lambda: ctx.api.put_json(
            "/settings",
            {
                "agent": before.get("agent"),
                "codex_web_search": before.get("codex_web_search"),
            },
        ),
    )
    settings = ctx.api.put_json(
        "/settings",
        {"agent": "codex", "codex_web_search": True},
        save_as="state/pairwise-codex-settings-effective.json",
    )
    assert settings.get("agent") == "codex"
    assert settings.get("codex_web_search") is True
    pid_file = Path(os.environ.get("APP_PID_FILE", ""))
    assert pid_file.is_file(), "real Codex observation requires the runner app PID file"
    app_pid = int(pid_file.read_text(encoding="utf-8").strip())
    world_id = ctx.fixture("real_world")
    prepare_chat(ctx.page, ctx.config, world_id)
    started = start_turn_from_ui(
        ctx.page,
        ctx.config,
        (
            "SHERPA-LIVE-REFERENCE-314 の運用時刻を確認してください。"
            "利用可能ならSherpa MCPのlist_docsとread_aroundを実行し、根拠を示してください。"
        ),
    )
    conversation_id = int(started["conversation_id"])
    turn_id = str(started["turn_id"])
    ctx.evidence.add_cleanup(
        f"delete pairwise Codex conversation {conversation_id}",
        lambda: _cleanup_conversation(ctx.api, conversation_id),
    )
    invocation = _capture_codex_invocation(
        app_pid,
        timeout_seconds=min(max(ctx.config.timeout_ms / 1000, 5), 30),
        expect_mcp=mcp_enabled,
    )
    events = ctx.api.collect_sse(
        f"/chat/turns/{turn_id}/stream?cursor=0",
        save_as="network/pairwise-codex-sse.jsonl",
    )
    answer_event(events)
    wait_for_completed_ui(ctx.page, ctx.config.timeout_ms)
    conversation = ctx.api.get_json(
        f"/conversations/{conversation_id}",
        save_as="state/pairwise-codex-conversation.json",
    )
    assistant = last_assistant_message(conversation)
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
        operation="pairwise-codex",
    )
    nodes = [event for event in events if event.get("type") == "node"]
    mcp_tool_seen = any(str(node.get("label") or "") in {"資料の一覧を確認", "該当箇所を精読"} for node in nodes)
    assert invocation["strict_config"] is sandbox_enabled
    assert invocation["workspace_write"] is (not sandbox_enabled)
    assert invocation["ephemeral"] is (not sandbox_enabled)
    assert invocation["resume"] is False
    assert invocation["mcp_configured"] is mcp_enabled
    if mcp_enabled:
        assert invocation["mcp_child_seen"] or mcp_tool_seen, (
            "MCP-enabled pairwise turn neither spawned the real MCP process nor emitted an MCP tool node"
        )
    else:
        assert not invocation["mcp_child_seen"] and not mcp_tool_seen, "MCP-disabled pairwise turn still executed the Sherpa MCP server"
    assert invocation["web_search_disabled"] is (not web_enabled), (
        "Codex invocation/config did not apply the admin web-search gate to an enabled user setting"
    )
    ctx.evidence.write_json("state/pairwise-codex-invocation.json", invocation)
    return {
        "source": (
            "real Codex subprocess procfs flags, transient redacted config booleans, "
            "SSE tool trace, conversation API, Postgres, audit, usage, and app log"
        ),
        "effects": {
            "SHERPA_CODEX_MCP": {
                "enabled": mcp_enabled,
                "configured": invocation["mcp_configured"],
                "process_or_tool_seen": invocation["mcp_child_seen"] or mcp_tool_seen,
            },
            "SHERPA_CODEX_SANDBOX": {
                "enabled": sandbox_enabled,
                "strict_config": invocation["strict_config"],
                "workspace_write": invocation["workspace_write"],
                "ephemeral": invocation["ephemeral"],
            },
            "SHERPA_ALLOW_WEB_SEARCH": {
                "enabled": web_enabled,
                "user_enabled": True,
                "forced_disabled": invocation["web_search_disabled"],
            },
        },
        "provider": correlation["provider"],
        "model": correlation["model"],
    }


def observe_pairwise_interaction(ctx: EnvironmentProbeContext) -> dict:
    expected = ctx.expected.get("pairwise") or {}
    group_id = str(expected.get("id") or "")
    adapters = {
        "connection-port-precedence": _pairwise_connection_effects,
        "auth-session": _pairwise_auth_effects,
        "ai-selection": _pairwise_ai_selection_effects,
        "codex-mcp-sandbox-web": _pairwise_codex_effects,
        "ingest-arms-ocr-vlm-legacy": _pairwise_ingest_effects,
    }
    adapter = adapters.get(group_id)
    if adapter is None:
        raise AssertionError(
            f"pairwise group {group_id} has no complete factor-effect adapter; "
            "configuration presence and generic health are not accepted as semantic evidence"
        )
    observation = adapter(ctx)
    effects = observation.get("effects") or {}
    factor_keys = set(_pairwise_factor_values(ctx))
    assert set(effects) == factor_keys, "pairwise semantic evidence does not account for every factor in the row"
    return {"id": group_id, "row_id": expected.get("row_id"), **observation}


def _graph_store_observation(ctx: EnvironmentProbeContext) -> dict:
    try:
        from neo4j import GraphDatabase
    except ModuleNotFoundError as exc:
        raise AssertionError("neo4j driver is required for real graph-store evidence") from exc
    uri = os.environ.get("NEO4J_URI", "")
    username = os.environ.get("NEO4J_USER", "")
    password = os.environ.get("NEO4J_PASSWORD", "")
    assert uri and username and password, "Neo4j connection environment is incomplete"
    with GraphDatabase.driver(uri, auth=(username, password)) as driver:
        driver.verify_connectivity()
        with driver.session() as session:
            row = session.run("CALL dbms.components() YIELD name, versions RETURN name, versions LIMIT 1").single()
    assert row and row.get("name") and row.get("versions")
    http_port = int(os.environ.get("SHERPA_NEO4J_HTTP_PORT", "0"))
    assert http_port > 0
    with socket.create_connection(("127.0.0.1", http_port), timeout=3):
        pass
    return {
        "source": "Neo4j driver identity query and HTTP TCP connect",
        "component": row.get("name"),
        "versions": list(row.get("versions") or []),
        "http_port": http_port,
        "http_port_open": True,
    }


def _audit_observation(ctx: EnvironmentProbeContext) -> dict:
    audit = ctx.api.get_json("/admin/audit?limit=10", save_as="state/environment-audit.json")
    rows = audit.get("rows") or []
    assert isinstance(rows, list) and rows, "real audit API returned no rows"
    return {
        "source": "GET /admin/audit and isolated Postgres audit rows",
        "row_count": len(rows),
        "actions": sorted({str(row.get("action")) for row in rows if row.get("action")}),
    }


def _world_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        world_id = ctx.fixture("real_world")
        status = ctx.api.get_json(f"/worlds/{world_id}/status", save_as="state/environment-world-status.json")
        preview = ctx.api.get_json(
            LiveApi.query("/ingest/preview", world=world_id),
            save_as="state/environment-ingest-preview.json",
        )
        search = ctx.api.get_json(
            LiveApi.query("/admin/es/search", world=world_id, query="SHERPA-LIVE-ALPHA-927", k=10),
            save_as="state/environment-search.json",
        )
        docs = preview.get("documents") or []
        hits = search.get("hits") or []
        assert int(status.get("indexed") or 0) > 0, status
        assert docs and hits, "real ingest/search observation returned no documents or hits"
        return {
            "source": "real World status, ingest preview, and Elasticsearch search",
            "world_id_sha256": _sha256(str(world_id)),
            "indexed": status.get("indexed"),
            "document_count": len(docs),
            "search_hit_count": len(hits),
            "methods": sorted(
                {str((row.get("provenance") or {}).get("method")) for row in docs if (row.get("provenance") or {}).get("method")}
            ),
        }

    return ctx.cached("world", collect)


def _observation_pipeline(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        world_id = ctx.fixture("real_world")
        snapshot = wait_for_ingestion_database_snapshot(
            ctx.config.database_url,
            world_id,
            ctx.evidence,
            timeout_seconds=max(ctx.config.timeout_ms / 1000, 180),
        )
        jobs = snapshot.get("ocr_jobs") or []
        workers = snapshot.get("ocr_workers") or []
        assert jobs and all(row.get("status") == "succeeded" for row in jobs), jobs
        assert any(row.get("available") and row.get("model_hashes_valid") for row in workers), workers
        return {
            "source": "direct Postgres OCR jobs and worker heartbeats",
            "job_count": len(jobs),
            "published_count": sum(bool(row.get("artifact_published")) for row in jobs),
            "worker_count": len(workers),
            "source_paths": sorted(str(row.get("source_rel_path")) for row in jobs),
        }

    return ctx.cached("observation_pipeline", collect)


def _workspace_observation(ctx: EnvironmentProbeContext) -> dict:
    listing = ctx.api.get_json("/workspace/files", save_as="state/environment-workspace.json")
    files = listing.get("files") or []
    assert isinstance(files, list), listing
    users_dir = Path(os.environ.get("SHERPA_USERS_DIR", ""))
    assert users_dir.is_dir(), "isolated users directory is absent"
    return {
        "source": "GET /workspace/files and runner-owned users directory",
        "file_count": len(files),
        "users_directory_owner": users_dir.stat().st_uid,
        "users_directory_writable": os.access(users_dir, os.W_OK),
    }


def _cleanup_conversation(api, conversation_id: int) -> None:
    response = api.request("GET", f"/conversations/{conversation_id}", expected={200, 404})
    if response.status == 200:
        api.delete_json(f"/conversations/{conversation_id}")


def _chat_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        world_id = ctx.fixture("real_world")
        settings = ctx.api.get_json("/settings")
        prepare_chat(ctx.page, ctx.config, world_id)
        started = start_turn_from_ui(
            ctx.page,
            ctx.config,
            "SHERPA-LIVE-REFERENCE-314 の運用時刻を実資料と実ツールで確認してください。",
        )
        conversation_id = int(started["conversation_id"])
        turn_id = str(started["turn_id"])
        ctx.evidence.add_cleanup(
            f"delete environment conversation {conversation_id}",
            lambda: _cleanup_conversation(ctx.api, conversation_id),
        )
        events = ctx.api.collect_sse(f"/chat/turns/{turn_id}/stream?cursor=0")
        answer_event(events)
        wait_for_completed_ui(ctx.page, ctx.config.timeout_ms)
        nodes = ui_trace_nodes(ctx.page)
        conversation = ctx.api.get_json(
            f"/conversations/{conversation_id}",
            save_as="state/environment-conversation.json",
        )
        assistant = last_assistant_message(conversation)
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
            operation="environment-chat-probe",
        )
        lifecycle = assert_node_status_lifecycle(events)
        cap_correlation = assert_persisted_trace_after_cap(events, assistant)
        database = conversation_database_snapshot(
            ctx.config.database_url,
            conversation_id,
            ctx.evidence,
            turn_id=turn_id,
        )
        database_assistants = [row for row in database["messages"] if row.get("role") == "assistant"]
        assert database_assistants, "direct Postgres has no persisted assistant message"
        database_assistant = database_assistants[-1]
        assert database_assistant.get("content") == assistant.get("content")
        assert database_assistant.get("trace") == assistant.get("trace")
        assert database_assistant.get("answer") == assistant.get("answer")
        database_cap = assert_persisted_trace_after_cap(events, database_assistant)
        assert database_cap == cap_correlation
        ctx.evidence.write_json(
            "state/environment-trace-transition-and-cap.json",
            {
                "lifecycle": lifecycle,
                "conversation_api_persistence": cap_correlation,
                "postgres_persistence": database_cap,
                "api_postgres_exact_match": True,
            },
        )
        expected_v2 = _bool(ctx.expected.get("exec_event_v2"))
        if expected_v2 is not None:
            trace_version = (assistant.get("answer") or {}).get("trace_version")
            assert (trace_version == 2) is expected_v2, {
                "expected_exec_event_v2": expected_v2,
                "observed_trace_version": trace_version,
            }
        return {
            "source": "real UI turn, independent SSE, conversation API, trace, and usage",
            "turn_id_sha256": _sha256(turn_id),
            "conversation_id_sha256": _sha256(str(conversation_id)),
            "event_count": len(events),
            "node_count": len(nodes),
            "answer_delta_count": correlation.get("answer_delta_count"),
            "provider": correlation.get("provider"),
            "model": correlation.get("model"),
            "trace_version": cap_correlation["trace_version"],
            "trace_cap_mode": cap_correlation["cap_mode"],
        }

    return ctx.cached("chat", collect)


def _history_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        configured = {
            "turns": int(os.environ.get("SHERPA_HISTORY_TURNS", "6")),
            "message_chars": int(os.environ.get("SHERPA_HISTORY_MSG_CHARS", "1200")),
            "char_budget": int(os.environ.get("SHERPA_HISTORY_CHAR_BUDGET", "6000")),
        }
        history_expected = all(value > 0 for value in configured.values())
        marker = f"SHERPA-HISTORY-{secrets.token_hex(16)}"
        ctx.evidence.register_secret(marker)
        world_id = ctx.fixture("real_world")
        settings = ctx.api.get_json("/settings")
        assert str(settings.get("agent") or "").strip().lower() == "openai", (
            "history-disabled observation must use real stateless OpenAI; Codex native resume "
            "retains session context outside SHERPA_HISTORY_*"
        )
        prepare_chat(ctx.page, ctx.config, world_id)
        knowledge = ctx.page.locator("#kbtoggle")
        if knowledge.get_attribute("aria-pressed") == "true":
            knowledge.click()
        first = start_turn_from_ui(
            ctx.page,
            ctx.config,
            f"次の識別子を記憶し、回答に同じ識別子を1回含めてください: {marker}",
        )
        conversation_id = int(first["conversation_id"])
        ctx.evidence.add_cleanup(
            f"delete history environment conversation {conversation_id}",
            lambda: _cleanup_conversation(ctx.api, conversation_id),
        )
        first_events = ctx.api.collect_sse(
            f"/chat/turns/{first['turn_id']}/stream?cursor=0",
            save_as="network/history-first-sse.jsonl",
        )
        answer_event(first_events)
        wait_for_completed_ui(ctx.page, ctx.config.timeout_ms)
        first_conversation = ctx.api.get_json(f"/conversations/{conversation_id}")
        first_assistant = last_assistant_message(first_conversation)
        assert_real_ai_result(
            settings,
            first_events,
            first_assistant,
            require_tool=False,
            evidence=ctx.evidence,
            turn_id=str(first["turn_id"]),
            conversation_id=conversation_id,
            database_url=ctx.config.database_url,
            checkpoint=first["_real_ai_checkpoint"],
            operation="environment-history-first-turn",
        )
        assert marker in str(first_assistant.get("content") or ""), (
            "the first real provider response did not acknowledge the exact history marker"
        )

        second = start_turn_from_ui(
            ctx.page,
            ctx.config,
            "この会話で直前に記憶した識別子だけを、そのまま回答してください。",
        )
        assert int(second["conversation_id"]) == conversation_id
        second_events = ctx.api.collect_sse(
            f"/chat/turns/{second['turn_id']}/stream?cursor=0",
            save_as="network/history-second-sse.jsonl",
        )
        answer_event(second_events)
        wait_for_completed_ui(ctx.page, ctx.config.timeout_ms)
        conversation = ctx.api.get_json(f"/conversations/{conversation_id}")
        second_assistant = last_assistant_message(conversation)
        assert_real_ai_result(
            settings,
            second_events,
            second_assistant,
            require_tool=False,
            evidence=ctx.evidence,
            turn_id=str(second["turn_id"]),
            conversation_id=conversation_id,
            database_url=ctx.config.database_url,
            checkpoint=second["_real_ai_checkpoint"],
            operation="environment-history-second-turn",
        )
        marker_returned = marker in str(second_assistant.get("content") or "")
        assert marker_returned is history_expected, "the second real provider turn did not match the configured history priming contract"
        database = conversation_database_snapshot(ctx.config.database_url, conversation_id, ctx.evidence)
        roles = [str(row.get("role") or "") for row in database["messages"]]
        assert roles == ["user", "assistant", "user", "assistant"], roles
        assert marker in str(database["messages"][0].get("content") or "")
        ctx.evidence.write_json(
            "state/history-provider-behavior.json",
            {
                "configured": configured,
                "history_expected": history_expected,
                "marker_sha256": _sha256(marker),
                "first_response_acknowledged_marker": True,
                "second_response_returned_marker": marker_returned,
                "postgres_roles": roles,
                "database_history_retained": True,
                "provider_behavior_matched": True,
            },
        )
        return {
            "source": "two real provider turns, conversation UI, SSE, API, and direct Postgres",
            "configured": configured,
            "history_expected": history_expected,
            "marker_returned": marker_returned,
            "database_message_count": len(roles),
            "provider_turn_count": 2,
        }

    return ctx.cached("history", collect)


def _es_request(ctx: EnvironmentProbeContext, method: str, path: str, body=None) -> dict:
    endpoint = os.environ.get("ES_URL", "").rstrip("/")
    parsed = urlsplit(endpoint)
    assert parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }, "ES_URL must resolve to the runner-owned loopback service"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        endpoint + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    started = time.monotonic()
    status = 0
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
    finally:
        ctx.evidence.record_api(
            method=method,
            url=endpoint + path,
            status=status,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    assert status == 200, f"Elasticsearch {method} {path} returned {status}"
    assert isinstance(payload, dict)
    return payload


def _disable_embedding_observation(ctx: EnvironmentProbeContext) -> dict:
    def collect() -> dict:
        raw_value = os.environ.get("SHERPA_DISABLE_EMBED")
        assert raw_value is not None and bool(raw_value), "embedding presence-semantics observation requires a nonempty environment value"
        world_id = ctx.fixture("real_world")
        slug = re.sub(r"[^a-z0-9._-]", "-", world_id.lower())[:40].strip("-._") or "w"
        index_name = f"sherpa-kb-{slug}-{hashlib.sha1(world_id.encode()).hexdigest()[:10]}"
        mapping = _es_request(ctx, "GET", f"/{index_name}/_mapping")
        index_mapping = next(iter(mapping.values()))
        properties = (index_mapping.get("mappings") or {}).get("properties") or {}
        assert "embedding" not in properties, "SHERPA_DISABLE_EMBED was present but the real index retained a dense vector mapping"
        search = _es_request(
            ctx,
            "POST",
            f"/{index_name}/_search",
            {"size": 100, "query": {"match_all": {}}, "_source": ["doc_id", "embedding"]},
        )
        hits = (search.get("hits") or {}).get("hits") or []
        assert hits, "real World index has no documents for embedding presence observation"
        assert all("embedding" not in (row.get("_source") or {}) for row in hits), (
            "SHERPA_DISABLE_EMBED was present but an indexed source contains a vector"
        )
        try:
            import psycopg
        except ModuleNotFoundError as exc:
            raise AssertionError("psycopg is required for embedding usage evidence") from exc
        with psycopg.connect(ctx.config.database_url, connect_timeout=5) as connection:
            usage_count = int(
                connection.execute(
                    "SELECT count(*) FROM usage_events WHERE kind='embed' AND world=%s",
                    (world_id,),
                ).fetchone()[0]
            )
        assert usage_count == 0, "SHERPA_DISABLE_EMBED was present but real embedding usage was metered"
        observation = {
            "source": "real World ingest, Elasticsearch mapping/source, and direct usage database",
            "environment_present": True,
            "environment_value_sha256": _sha256(raw_value),
            "world_id_sha256": _sha256(world_id),
            "index_name_sha256": _sha256(index_name),
            "indexed_document_count": len(hits),
            "dense_vector_mapping_present": False,
            "source_vector_count": 0,
            "embed_usage_count": usage_count,
        }
        ctx.evidence.write_json("state/disable-embed-presence-observation.json", observation)
        return observation

    return ctx.cached("disable_embedding", collect)


def _office_observation(ctx: EnvironmentProbeContext) -> dict:
    world = _world_observation(ctx)
    office_bin = os.environ.get("SHERPA_SOFFICE_BIN") or "soffice"
    resolved = shutil.which(office_bin)
    assert resolved, f"configured LibreOffice executable is unavailable: {office_bin}"
    result = subprocess.run(
        [resolved, "--headless", "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0 and (result.stdout or result.stderr).strip(), "real LibreOffice version probe failed"
    return {
        "source": "real ingest preview and LibreOffice executable",
        "document_count": world["document_count"],
        "version_output_sha256": _sha256((result.stdout or result.stderr).strip()),
    }


def _explicit_failure(ctx: EnvironmentProbeContext, probe_id: str) -> dict:
    _application_observation(ctx)
    raise AssertionError(
        f"observable {probe_id} has an explicit failing adapter because no safe real UI/API/DB/log/"
        "file/process observation contract is available"
    )


def probe_adapter_name(probe_id: str) -> str:
    if probe_id in CONTENT_SEMANTIC_PROBES:
        return "content-semantic"
    if probe_id in SYSTEM_SEMANTIC_PROBES:
        return "system-semantic"
    groups = (
        (_HEALTH_PROBES, "health"),
        (_SETTINGS_PROBES, "settings"),
        (_AUTH_PROBES, "auth"),
        (_DATABASE_PROBES, "database"),
        (_BROWSER_PROBES, "browser"),
        (_PROCESS_PROBES, "process"),
        (_LOG_PROBES, "logs"),
        (_FILESYSTEM_PROBES, "filesystem"),
        (_WORLD_PROBES, "world"),
        (_OBSERVATION_PROBES, "observation_pipeline"),
        (_WORKSPACE_PROBES, "workspace"),
        (_CHAT_PROBES, "chat"),
        (_AUDIT_PROBES, "audit"),
        (_COMPOSE_PROBES, "compose"),
        (_EFFECTIVE_PROBES, "application"),
        (_OFFICE_PROBES, "office"),
        (_GRAPH_STORE_PROBES, "graph_store"),
    )
    for values, adapter in groups:
        if probe_id in values:
            return adapter
    return "explicit-failure"


def probe_pair_adapter_name(probe_id: str, variable: str) -> str:
    """Resolve a generated pair without allowing a broad legacy fallback."""

    if supports_content_direct_probe(probe_id, variable):
        return "content-direct"
    if supports_system_direct_probe(probe_id, variable):
        return "system-direct"
    if variable in CONTENT_SEMANTIC_PROBE_VARIABLES.get(probe_id, frozenset()):
        return "content-semantic"
    if supports_system_semantic_probe(probe_id, variable):
        return "system-semantic"
    return "semantic-gap"


def run_environment_probe(ctx: EnvironmentProbeContext, probe_id: str) -> dict:
    application = _application_observation(ctx)
    outcome = expected_outcome(ctx)
    scenario_variable = str(_generated_scenario(ctx).get("variable") or "")
    if outcome == "reject":
        raise AssertionError("a startup-rejection environment profile unexpectedly reached pytest")
    if supports_content_direct_probe(probe_id, scenario_variable):
        adapter_name = "content-direct"
    elif supports_system_direct_probe(probe_id, scenario_variable):
        adapter_name = "system-direct"
    elif (
        not scenario_variable
        and _history_contract_active(ctx)
        and probe_id
        in {
            "provider-request-summary",
            "conversation-continuity",
            "conversation-ui",
            "postgres",
        }
    ):
        adapter_name = "history"
    elif _trace_contract_active(ctx) and probe_id in {
        "sse",
        "sse-schema",
        "ui-trace",
        "postgres",
        "postgres-trace",
    }:
        adapter_name = "chat"
    elif _disable_embedding_contract_active(ctx) and probe_id in {
        "app-log",
        "ingest-log",
        "ingest-result",
        "elasticsearch-document",
        "elasticsearch-vector-field",
        "variable-presence",
    }:
        adapter_name = "disable_embedding"
    elif scenario_variable:
        adapter_name = probe_pair_adapter_name(probe_id, scenario_variable)
    elif probe_id == "admin-page":
        adapter_name = "admin_page"
    elif probe_id == "navigation":
        adapter_name = "navigation"
    elif probe_id in {"status-state", "status-url"}:
        adapter_name = "status_page"
    elif probe_id == "libreoffice-version":
        adapter_name = "office"
    elif probe_id in CONTENT_SEMANTIC_PROBES:
        adapter_name = "content-semantic"
    elif probe_id in SYSTEM_SEMANTIC_PROBES:
        adapter_name = "system-semantic"
    elif probe_id not in _DIRECT_SEMANTIC_PROBES:
        adapter_name = "semantic-gap"
    else:
        adapter_name = probe_adapter_name(probe_id)
    adapters = {
        "health": _health_observation,
        "settings": _settings_observation,
        "auth": _auth_observation,
        "database": _database_observation,
        "browser": _browser_observation,
        "navigation": _navigation_observation,
        "admin_page": _admin_page_observation,
        "status_page": _status_page_observation,
        "process": _process_observation,
        "logs": _log_observation,
        "filesystem": _filesystem_observation,
        "world": _world_observation,
        "observation_pipeline": _observation_pipeline,
        "workspace": _workspace_observation,
        "chat": _chat_observation,
        "audit": _audit_observation,
        "compose": _compose_observation,
        "application": _application_observation,
        "office": _office_observation,
        "graph_store": _graph_store_observation,
        "history": _history_observation,
        "disable_embedding": _disable_embedding_observation,
        "content-direct": lambda value: run_content_direct_probe(value, probe_id),
        "system-direct": lambda value: run_system_direct_probe(value, probe_id, scenario_variable),
        "content-semantic": lambda value: run_content_semantic_probe(value, probe_id),
        "system-semantic": lambda value: run_system_semantic_probe(value, probe_id),
        "explicit-failure": lambda value: _explicit_failure(value, probe_id),
        "semantic-gap": lambda value: _explicit_failure(value, probe_id),
    }
    http_start = len(ctx.evidence.http)
    api_error_start = ctx.api.structured_error_count()
    log_offsets = _service_log_offsets(ctx, include_existing=outcome == "explicit-error")
    if outcome == "explicit-error" and isinstance(ctx.cache.get("observed_product_error"), dict):
        raise ProbeNotRunAfterProductError(ctx.cache["observed_product_error"])
    try:
        observed = adapters[adapter_name](ctx)
    except Exception as exc:
        if outcome == "explicit-error":
            product_error = _explicit_product_error(
                ctx,
                http_start=http_start,
                api_error_start=api_error_start,
                log_offsets=log_offsets,
            )
            if product_error:
                raise ObservedProductError(product_error) from exc
        raise
    assert isinstance(observed, dict) and observed.get("source"), f"environment adapter {adapter_name} returned no real evidence source"
    if outcome == "explicit-error":
        product_error = _explicit_product_error(
            ctx,
            http_start=http_start,
            api_error_start=api_error_start,
            log_offsets=log_offsets,
        )
        if product_error:
            raise ObservedProductError(product_error)
        raise AssertionError("the real product accepted an invalid environment value without the declared error")
    boundary_errors = None
    if outcome == "accepted-boundary":
        boundary_errors = _unexpected_product_errors(ctx, http_start=http_start, log_offsets=log_offsets)
        assert boundary_errors["error_absent"], "the boundary value reached a product error instead of an accepted boundary state"
    return {
        "status": "verified",
        "adapter": adapter_name,
        "application": application,
        "observation": observed,
        "actual_outcome": outcome,
        "boundary_error_observation": boundary_errors,
    }


def registry_summary(config_path: Path) -> dict:
    declared = declared_observable_ids(config_path)
    resolved = {probe_id: probe_adapter_name(probe_id) for probe_id in sorted(declared)}
    declared_pairs = declared_observable_pairs(config_path)
    pair_adapters = {f"{variable}/{probe_id}": probe_pair_adapter_name(probe_id, variable) for variable, probe_id in sorted(declared_pairs)}
    pair_gaps = [
        {
            "variable": variable,
            "probe_id": probe_id,
            "legacy_adapter": probe_adapter_name(probe_id),
        }
        for variable, probe_id in sorted(declared_pairs)
        if probe_pair_adapter_name(probe_id, variable) == "semantic-gap"
    ]
    semantic_implementations = _DIRECT_SEMANTIC_PROBES | CONTENT_SEMANTIC_PROBES | SYSTEM_SEMANTIC_PROBES
    return {
        "declared_count": len(declared),
        "resolved_count": len(resolved),
        "explicit_failure": sorted(
            probe_id for probe_id, adapter in resolved.items() if adapter == "explicit-failure" or probe_id not in semantic_implementations
        ),
        "adapters": resolved,
        "declared_pair_count": len(declared_pairs),
        "resolved_pair_count": len(declared_pairs) - len(pair_gaps),
        "pair_semantic_gap_count": len(pair_gaps),
        "pair_semantic_gaps": pair_gaps,
        "pair_adapters": pair_adapters,
    }
