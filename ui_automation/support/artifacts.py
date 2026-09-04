from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import struct
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from ui_automation.runner.artifacts import (
    append_private_text,
    safe_url as _runner_safe_url,
    write_private_text_atomic,
)
from ui_automation.runner.filesystem_safety import assert_no_mount_targets, chmod_path_no_follow


_SECRET_KEY = re.compile(
    r"(?i)(authorization|cookie|set-cookie|password|passwd|api[_-]?key|secret|"
    r"session(?:[_-]?(?:id|token))?(?![A-Za-z0-9_-])|"
    r"(?<![A-Za-z0-9])token(?![A-Za-z0-9]))"
)
_NON_IDENTITY_CONTROL_CLASSES = frozenset(
    {"act-btn", "btn-ghost", "btn-primary", "btn-secondary", "danger", "filterchip", "iconbtn", "mini", "on", "small"}
)
_SAFE_CONTROL_HREF = re.compile(r"(?:/|[A-Za-z0-9])[A-Za-z0-9._~!&'()*+,;=:@%/+-]*")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)(authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n\"}]+"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|password|passwd|secret|token|sid|"
        r"session[_-]?(?:id|token))\s*[:=]\s*[^\s,;\"'<>]{4,}"
    ),
    re.compile(r"(?i)\bsession\s*[:=]\s*[A-Za-z0-9._~+/-]{12,}"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@"),
)
_JSON_SECRET_VALUE = re.compile(
    r'(?i)("(?:authorization|cookie|set-cookie|password|passwd|api[_-]?key|secret|'
    r"session|session[_-]?(?:id|token)|"
    r'(?:[A-Za-z0-9]+[_-])*token(?:[_-][A-Za-z0-9]+)*)"\s*:\s*)'
    r'"(?:[^"\\]|\\.)*"'
)
_TRACE_SECRET_HEADER_PAIR = re.compile(
    r'(?is)"name"\s*:\s*"(?:authorization|cookie|set-cookie|password|passwd|api[_-]?key|secret|'
    r"session|session[_-]?(?:id|token)|"
    r'(?:[A-Za-z0-9]+[_-])*token(?:[_-][A-Za-z0-9]+)*)"'
    r'(?:(?!\}\s*[,\]]).)*?"value"\s*:\s*"(?!<redacted>)[^\"]+"'
)
_TRACE_SECRET_HEADER_PAIR_REVERSED = re.compile(
    r'(?is)"value"\s*:\s*"(?!<redacted>)[^\"]+"'
    r'(?:(?!\}\s*[,\]]).)*?"name"\s*:\s*"(?:authorization|cookie|set-cookie|password|passwd|'
    r"api[_-]?key|secret|session|session[_-]?(?:id|token)|"
    r'(?:[A-Za-z0-9]+[_-])*token(?:[_-][A-Za-z0-9]+)*)"'
)
_URL_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@")
_AUTH_BOOTSTRAP_CONSOLE_401 = "Failed to load resource: the server responded with a status of 401 (Unauthorized)"
_AUTH_BOOTSTRAP_401_PATHS = frozenset(
    {
        "/auth/login",
        "/auth/me",
        "/chat/turns/running",
        "/health/summary",
    }
)
_NAVIGATION_CANCEL_MAX_SECONDS = 2.0
_REQUEST_FAILURE_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_REQUEST_FAILURE_VALUE = re.compile(r"net::ERR_[A-Z0-9_]+")
_HTTP_ERROR_CONSOLE = re.compile(r"Failed to load resource: the server responded with a status of ([45]\d\d) \(([^\r\n()]*)\)")

_SAFE_AUTHORIZATION_KEYS = frozenset({"status", "role", "auth_disabled"})
_SAFE_ACTION_AUTHORIZATION_KEYS = _SAFE_AUTHORIZATION_KEYS | frozenset({"observed_at_epoch_seconds", "evidence_correlation_id"})
_ROLE_EVIDENCE_CORRELATION = re.compile(r"(?:control-action|screenshot)-role-\d{10,14}-\d+")
_SCREENSHOT_FILENAME = re.compile(r"\d{3}__[a-z0-9]+(?:-[a-z0-9]+){2,}\.png")
_SCREENSHOT_FEATURE_PREFIXES = frozenset(
    {
        "admin",
        "auth",
        "chat",
        "environment",
        "graph",
        "help",
        "ingest",
        "keyboard",
        "navigation",
        "planner",
        "search",
        "security",
        "settings",
        "smoke",
        "thought",
        "workspace",
        "world",
        "worlds",
    }
)


def _is_safe_control_href(value: str) -> bool:
    if not value or len(value) > 140 or any(character in value for character in "\r\n\t?#${}"):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        if not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname):
            return False
        try:
            parsed.port
        except ValueError:
            return False
        return bool(_SAFE_CONTROL_HREF.fullmatch(parsed.path or "/"))
    return (
        not parsed.netloc
        and ":" not in value.split("/", 1)[0]
        and not any(part == ".." for part in value.split("/"))
        and bool(_SAFE_CONTROL_HREF.fullmatch(value))
    )


def _is_safe_authorization_observation(value) -> bool:
    """Recognize the deliberately non-secret role/status evidence shape.

    ``authorization`` remains a secret-bearing key everywhere else.  This narrow
    exception lets screenshot/control attestations retain an HTTP status and role
    without weakening header/cookie redaction.
    """

    return (
        isinstance(value, dict)
        and frozenset(value) in {_SAFE_AUTHORIZATION_KEYS, _SAFE_ACTION_AUTHORIZATION_KEYS}
        and isinstance(value.get("status"), int)
        and not isinstance(value.get("status"), bool)
        and value.get("role") in {"admin", "user", "anonymous", "unknown"}
        and isinstance(value.get("auth_disabled"), bool)
        and (
            frozenset(value) == _SAFE_AUTHORIZATION_KEYS
            or (
                isinstance(value.get("observed_at_epoch_seconds"), (int, float))
                and not isinstance(value.get("observed_at_epoch_seconds"), bool)
                and isinstance(value.get("evidence_correlation_id"), str)
                and _ROLE_EVIDENCE_CORRELATION.fullmatch(value["evidence_correlation_id"])
            )
        )
    )


def _is_valid_control_authorization_observation(value) -> bool:
    """Require a role/status pair that a real ``/auth/me`` can prove."""

    return bool(
        _is_safe_authorization_observation(value)
        and (
            (value["role"] in {"admin", "user"} and 200 <= value["status"] < 300)
            or (value["role"] == "anonymous" and value["status"] in {401, 403})
        )
        and (value["auth_disabled"] is False or value["role"] == "admin")
    )


def _url_origin(value: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def _navigation_cancel_correlation(
    failure: dict,
    http_rows: list[dict],
    navigation_events: list[dict],
) -> dict | None:
    """Return one proven same-frame document navigation for an aborted request.

    A Chromium ``ERR_ABORTED`` is not evidence of navigation by itself.  The
    cancellation is expected only when one successful main-frame document
    transaction and a committed main-frame navigation on the same page/frame
    and origin bracket it within a narrow window.  Ambiguous candidates are
    deliberately rejected.
    """

    if (
        failure.get("failure") != "net::ERR_ABORTED"
        or failure.get("resource_type") == "document"
        or failure.get("is_navigation_request") is True
    ):
        return None
    failure_sequence = failure.get("event_sequence")
    failure_ts = failure.get("ts")
    page_id = str(failure.get("page_id") or "")
    frame_id = str(failure.get("frame_id") or "")
    request_id = str(failure.get("request_id") or "")
    failure_origin = _url_origin(str(failure.get("url") or ""))
    if (
        not isinstance(failure_sequence, int)
        or isinstance(failure_sequence, bool)
        or not isinstance(failure_ts, (int, float))
        or isinstance(failure_ts, bool)
        or not page_id
        or not frame_id
        or not request_id
        or failure_origin is None
    ):
        return None

    request_rows = [row for row in http_rows if row.get("phase") == "request" and row.get("request_id") == request_id]
    if len(request_rows) != 1:
        return None
    source_request = request_rows[0]
    source_sequence = source_request.get("event_sequence")
    source_frame_url = str(source_request.get("frame_url") or "")
    if (
        source_request.get("page_id") != page_id
        or source_request.get("frame_id") != frame_id
        or source_request.get("method") != failure.get("method")
        or source_request.get("url") != failure.get("url")
        or source_request.get("resource_type") != failure.get("resource_type")
        or not isinstance(source_sequence, int)
        or isinstance(source_sequence, bool)
        or source_sequence >= failure_sequence
        or _url_origin(source_frame_url) != failure_origin
    ):
        return None

    candidates: list[dict] = []
    for navigation in http_rows:
        navigation_sequence = navigation.get("event_sequence")
        navigation_ts = navigation.get("ts")
        if (
            navigation.get("phase") != "request"
            or navigation.get("resource_type") != "document"
            or navigation.get("is_navigation_request") is not True
            or navigation.get("is_main_frame") is not True
            or navigation.get("page_id") != page_id
            or navigation.get("frame_id") != frame_id
            or navigation.get("frame_url") != source_frame_url
            or not isinstance(navigation_sequence, int)
            or isinstance(navigation_sequence, bool)
            or not isinstance(navigation_ts, (int, float))
            or isinstance(navigation_ts, bool)
            or navigation_sequence >= failure_sequence
            or float(navigation_ts) > float(failure_ts) + 0.05
            or float(failure_ts) - float(navigation_ts) > _NAVIGATION_CANCEL_MAX_SECONDS
            or _url_origin(str(navigation.get("url") or "")) != failure_origin
        ):
            continue
        navigation_request_id = str(navigation.get("request_id") or "")
        responses = [row for row in http_rows if row.get("phase") == "response" and row.get("request_id") == navigation_request_id]
        if len(responses) != 1:
            continue
        response = responses[0]
        response_sequence = response.get("event_sequence")
        response_ts = response.get("ts")
        status = response.get("status")
        if (
            response.get("page_id") != page_id
            or response.get("frame_id") != frame_id
            or response.get("url") != navigation.get("url")
            or response.get("is_navigation_request") is not True
            or response.get("is_main_frame") is not True
            or not isinstance(response_sequence, int)
            or isinstance(response_sequence, bool)
            or not navigation_sequence < response_sequence < failure_sequence
            or source_sequence >= response_sequence
            or not isinstance(response_ts, (int, float))
            or isinstance(response_ts, bool)
            or float(response_ts) > float(failure_ts) + 0.05
            or not isinstance(status, int)
            or isinstance(status, bool)
            or not 200 <= status < 400
            or source_frame_url == str(navigation.get("url") or "")
        ):
            continue
        later_document_requests = [
            row
            for row in http_rows
            if row.get("phase") == "request"
            and row.get("resource_type") == "document"
            and row.get("is_navigation_request") is True
            and row.get("is_main_frame") is True
            and row.get("page_id") == page_id
            and row.get("frame_id") == frame_id
            and row.get("request_id") != navigation_request_id
            and isinstance(row.get("event_sequence"), int)
            and not isinstance(row.get("event_sequence"), bool)
            and response_sequence < row["event_sequence"]
        ]
        next_document_sequence = min(int(row["event_sequence"]) for row in later_document_requests) if later_document_requests else None
        commits = [
            row
            for row in navigation_events
            if row.get("event") == "main-frame-committed"
            and row.get("page_id") == page_id
            and row.get("frame_id") == frame_id
            and isinstance(row.get("event_sequence"), int)
            and not isinstance(row.get("event_sequence"), bool)
            and failure_sequence < row["event_sequence"]
            and (next_document_sequence is None or row["event_sequence"] < next_document_sequence)
            and isinstance(row.get("ts"), (int, float))
            and not isinstance(row.get("ts"), bool)
            and float(row["ts"]) + 0.05 >= float(failure_ts)
            and float(row["ts"]) - float(navigation_ts) <= _NAVIGATION_CANCEL_MAX_SECONDS
        ]
        if len(commits) != 1:
            continue
        commit = commits[0]
        commit_sequence = commit.get("event_sequence")
        commit_ts = commit.get("ts")
        if (
            not isinstance(commit_sequence, int)
            or isinstance(commit_sequence, bool)
            or commit.get("url") != navigation.get("url")
            or not response_sequence < failure_sequence < commit_sequence
            or not isinstance(commit_ts, (int, float))
            or isinstance(commit_ts, bool)
            or float(commit_ts) + 0.05 < float(failure_ts)
            or float(commit_ts) - float(navigation_ts) > _NAVIGATION_CANCEL_MAX_SECONDS
        ):
            continue
        candidates.append(
            {
                "source_request_id": request_id,
                "source_request_event_sequence": source_sequence,
                "page_id": page_id,
                "frame_id": frame_id,
                "source_frame_url": source_frame_url,
                "request_id": navigation_request_id,
                "url": str(navigation["url"]),
                "status": status,
                "request_event_sequence": navigation_sequence,
                "response_event_sequence": response_sequence,
                "commit_event_sequence": commit_sequence,
                "elapsed_to_failure_ms": max(0, round((float(failure_ts) - float(navigation_ts)) * 1000)),
            }
        )
    return candidates[0] if len(candidates) == 1 else None


def _exact_failure_allowance_match(
    failure: dict,
    source_request: dict,
    allowances: list[dict],
) -> dict | None:
    """Return one unused, exact, case-local allowance for one real request."""

    try:
        path = urlsplit(str(failure.get("url") or "")).path
    except ValueError:
        return None
    failure_sequence = failure.get("event_sequence")
    source_sequence = source_request.get("event_sequence")
    page_id = str(failure.get("page_id") or "")
    frame_id = str(failure.get("frame_id") or "")
    if (
        not page_id
        or not frame_id
        or source_request.get("page_id") != page_id
        or source_request.get("frame_id") != frame_id
        or source_request.get("request_id") != failure.get("request_id")
        or source_request.get("method") != failure.get("method")
        or source_request.get("url") != failure.get("url")
        or source_request.get("resource_type") != failure.get("resource_type")
        or not isinstance(failure_sequence, int)
        or isinstance(failure_sequence, bool)
        or not isinstance(source_sequence, int)
        or isinstance(source_sequence, bool)
        or source_sequence >= failure_sequence
        or _url_origin(str(source_request.get("url") or "")) != _url_origin(str(source_request.get("frame_url") or ""))
    ):
        return None
    matches = [
        allowance
        for allowance in allowances
        if not allowance["matched_request_ids"]
        and failure_sequence > allowance["registered_after_event_sequence"]
        and allowance["method"] == failure.get("method")
        and allowance["path"] == path
        and allowance["resource_type"] == failure.get("resource_type")
        and allowance["failure"] == failure.get("failure")
    ]
    return matches[0] if len(matches) == 1 else None


def _request_failure_expectation_contract_failures(
    nodeid: str,
    request_failures: list[dict],
    allowances: list[dict],
    *,
    http_rows: list[dict] | None = None,
    navigation_events: list[dict] | None = None,
) -> list[str]:
    """Reject incomplete expected classifications and unused broad exceptions."""

    contract_failures: list[str] = []
    for failure in request_failures:
        if failure.get("expected") is not True:
            continue
        request_id = str(failure.get("request_id") or "")
        source = failure.get("expected_source")
        reason = failure.get("expected_reason")
        if failure.get("expectation_case") != nodeid:
            contract_failures.append(f"expected request failure has no exact case binding: {request_id or '<missing>'}")
        if not isinstance(reason, str) or len(reason.strip()) < 24:
            contract_failures.append(f"expected request failure has no detailed reason: {request_id or '<missing>'}")
        if source == "correlated-document-navigation":
            correlation = failure.get("navigation_correlation")
            if not isinstance(correlation, dict):
                contract_failures.append(f"navigation cancellation lacks structured correlation: {request_id or '<missing>'}")
                continue
            sequences = (
                correlation.get("request_event_sequence"),
                correlation.get("source_request_event_sequence"),
                correlation.get("response_event_sequence"),
                failure.get("event_sequence"),
                correlation.get("commit_event_sequence"),
            )
            if (
                correlation.get("source_request_id") != request_id
                or correlation.get("page_id") != failure.get("page_id")
                or correlation.get("frame_id") != failure.get("frame_id")
                or correlation.get("source_frame_url") != failure.get("frame_url")
                or not all(isinstance(value, int) and not isinstance(value, bool) for value in sequences)
                or not sequences[0] < sequences[2] < sequences[3] < sequences[4]
                or not sequences[1] < sequences[2]
                or correlation.get("source_frame_url") == correlation.get("url")
                or not isinstance(correlation.get("status"), int)
                or isinstance(correlation.get("status"), bool)
                or not 200 <= correlation["status"] < 400
                or (
                    http_rows is not None
                    and navigation_events is not None
                    and _navigation_cancel_correlation(failure, http_rows, navigation_events) != correlation
                )
            ):
                contract_failures.append(f"navigation cancellation correlation is incomplete: {request_id or '<missing>'}")
        elif source == "explicit-case-allowance":
            allowance_id = str(failure.get("allowance_id") or "")
            matching = [allowance for allowance in allowances if allowance.get("allowance_id") == allowance_id]
            correlation = failure.get("allowance_correlation")
            if len(matching) != 1 or not isinstance(correlation, dict):
                contract_failures.append(f"explicit request failure lacks one allowance: {request_id or '<missing>'}")
                continue
            allowance = matching[0]
            source_requests = (
                [row for row in http_rows if row.get("phase") == "request" and row.get("request_id") == request_id]
                if http_rows is not None
                else []
            )
            try:
                failure_path = urlsplit(str(failure.get("url") or "")).path
            except ValueError:
                failure_path = ""
            if (
                allowance.get("matched_request_ids") != [request_id]
                or correlation.get("case") != nodeid
                or correlation.get("method") != failure.get("method")
                or failure.get("method") != allowance.get("method")
                or correlation.get("path") != failure_path
                or failure_path != allowance.get("path")
                or correlation.get("resource_type") != failure.get("resource_type")
                or failure.get("resource_type") != allowance.get("resource_type")
                or correlation.get("failure") != failure.get("failure")
                or failure.get("failure") != allowance.get("failure")
                or correlation.get("registered_after_event_sequence") != allowance.get("registered_after_event_sequence")
                or (
                    http_rows is not None
                    and (
                        len(source_requests) != 1
                        or source_requests[0].get("method") != failure.get("method")
                        or source_requests[0].get("url") != failure.get("url")
                        or source_requests[0].get("resource_type") != failure.get("resource_type")
                        or source_requests[0].get("page_id") != failure.get("page_id")
                        or source_requests[0].get("frame_id") != failure.get("frame_id")
                    )
                )
            ):
                contract_failures.append(f"explicit request failure allowance is not exact: {request_id or '<missing>'}")
        else:
            contract_failures.append(f"expected request failure has an unknown source: {request_id or '<missing>'}")

    matched_ids: list[str] = []
    for allowance in allowances:
        matches = allowance.get("matched_request_ids")
        if not isinstance(matches, list) or len(matches) != 1 or not all(isinstance(item, str) and item for item in matches):
            contract_failures.append(f"request-failure allowance must match exactly once: {allowance.get('allowance_id') or '<missing>'}")
            continue
        matched_ids.extend(matches)
    if len(matched_ids) != len(set(matched_ids)):
        contract_failures.append("one request failure was consumed by multiple explicit allowances")
    return contract_failures


def _http_error_console_correlation(
    console: dict,
    http_rows: list[dict],
    allowance: dict,
    consumed_request_ids: set[str],
) -> dict | None:
    """Correlate one generic Chromium HTTP error to one unambiguous response."""

    match = _HTTP_ERROR_CONSOLE.fullmatch(str(console.get("text") or ""))
    console_sequence = console.get("event_sequence")
    console_ts = console.get("ts")
    page_id = str(console.get("page_id") or "")
    location_url = str(console.get("location_url") or "")
    if (
        match is None
        or not isinstance(console_sequence, int)
        or isinstance(console_sequence, bool)
        or not isinstance(console_ts, (int, float))
        or isinstance(console_ts, bool)
        or not page_id
        or not location_url
        or int(match.group(1)) != allowance.get("status")
        or not isinstance(allowance.get("registered_after_event_sequence"), int)
        or isinstance(allowance.get("registered_after_event_sequence"), bool)
    ):
        return None
    candidates: list[dict] = []
    for response in http_rows:
        response_sequence = response.get("event_sequence")
        response_ts = response.get("ts")
        request_id = str(response.get("request_id") or "")
        expected_message = (
            f"Failed to load resource: the server responded with a status of {response.get('status')} ({response.get('status_text') or ''})"
        )
        if (
            response.get("phase") != "response"
            or response.get("page_id") != page_id
            or response.get("url") != location_url
            or response.get("status") != int(match.group(1))
            or expected_message != console.get("text")
            or not request_id
            or request_id in consumed_request_ids
            or not isinstance(response_sequence, int)
            or isinstance(response_sequence, bool)
            or not response_sequence < console_sequence
            or not isinstance(response_ts, (int, float))
            or isinstance(response_ts, bool)
            or not 0 <= float(console_ts) - float(response_ts) <= 2
        ):
            continue
        candidates.append(response)
    if len(candidates) != 1:
        return None
    response = candidates[0]
    try:
        response_path = urlsplit(str(response.get("url") or "")).path
    except ValueError:
        return None
    if (
        response.get("method") != allowance.get("method")
        or response_path != allowance.get("path")
        or response.get("status") != allowance.get("status")
        or allowance["registered_after_event_sequence"] >= response["event_sequence"]
    ):
        return None
    return {
        "request_id": str(response["request_id"]),
        "page_id": page_id,
        "location_url": location_url,
        "method": str(response["method"]),
        "path": response_path,
        "status": int(response["status"]),
        "status_text": str(response.get("status_text") or ""),
        "response_event_sequence": int(response["event_sequence"]),
        "console_event_sequence": int(console_sequence),
        "elapsed_ms": max(0, round((float(console_ts) - float(response["ts"])) * 1000)),
    }


def _unique_401_console_response_candidate(
    console: dict,
    responses: list[dict],
    consumed_request_ids: set[str],
) -> dict | None:
    """Return one same-page 401 response in the narrow console time window.

    Chromium's 401 console text contains no URL or request identifier. Picking
    the newest response would silently guess when two eligible requests overlap,
    so ambiguity must remain an unexpected console error.
    """

    console_sequence = console.get("event_sequence")
    console_ts = console.get("ts")
    page_id = str(console.get("page_id") or "")
    location_url = str(console.get("location_url") or "")
    if (
        not page_id
        or not location_url
        or not isinstance(console_sequence, int)
        or isinstance(console_sequence, bool)
        or not isinstance(console_ts, (int, float))
        or isinstance(console_ts, bool)
    ):
        return None
    candidates = [
        row
        for row in responses
        if str(row.get("request_id") or "") not in consumed_request_ids
        and row.get("page_id") == page_id
        and row.get("url") == location_url
        and isinstance(row.get("event_sequence"), int)
        and not isinstance(row.get("event_sequence"), bool)
        and row["event_sequence"] < console_sequence
        and isinstance(row.get("ts"), (int, float))
        and not isinstance(row.get("ts"), bool)
        and 0 <= float(console_ts) - float(row["ts"]) <= 0.5
    ]
    return candidates[0] if len(candidates) == 1 else None


def _automatic_401_console_correlation(
    console: dict,
    http_rows: list[dict],
    consumed_request_ids: set[str],
) -> dict | None:
    """Classify one exact-location 401 console event from a shared sequence."""

    if console.get("type") != "error" or console.get("text") != _AUTH_BOOTSTRAP_CONSOLE_401:
        return None
    responses = [row for row in http_rows if row.get("phase") == "response" and row.get("status") == 401]
    matched = _unique_401_console_response_candidate(console, responses, consumed_request_ids)
    if matched is None:
        return None
    try:
        matched_path = urlsplit(str(matched.get("url") or "")).path
    except ValueError:
        return None
    evidence_probe = matched.get("evidence_probe")
    evidence_correlation_id = matched.get("evidence_correlation_id")
    if (
        evidence_probe in {"control-action-role-v1", "screenshot-role-v1"}
        and matched.get("method") == "GET"
        and matched_path == "/auth/me"
        and isinstance(evidence_correlation_id, str)
        and _ROLE_EVIDENCE_CORRELATION.fullmatch(evidence_correlation_id)
    ):
        expected_source = "correlated-role-probe-401"
        expected_reason = f"same-page {evidence_probe} /auth/me response produced this exact Chromium 401 diagnostic"
    elif (
        console.get("auth_bootstrap_scope") is True
        and evidence_probe is None
        and evidence_correlation_id is None
        and matched.get("method") in {"GET", "POST"}
        and matched_path in _AUTH_BOOTSTRAP_401_PATHS
    ):
        expected_source = "correlated-auth-bootstrap-401"
        expected_reason = "scoped real login bootstrap produced this exact same-page Chromium 401 diagnostic"
    else:
        return None
    return {
        "expected_source": expected_source,
        "expected_reason": expected_reason,
        "request_id": str(matched["request_id"]),
        "console_http_correlation": {
            "request_id": str(matched["request_id"]),
            "page_id": matched.get("page_id"),
            "location_url": console.get("location_url"),
            "method": matched.get("method"),
            "path": matched_path,
            "status": matched.get("status"),
            "status_text": matched.get("status_text"),
            "evidence_probe": evidence_probe,
            "evidence_correlation_id": evidence_correlation_id,
            "response_event_sequence": matched.get("event_sequence"),
            "console_event_sequence": console.get("event_sequence"),
            "elapsed_ms": max(0, round((float(console["ts"]) - float(matched["ts"])) * 1000)),
        },
    }


def _unique_request_failure_console_candidate(
    console: dict,
    request_failures: list[dict],
    consumed_request_ids: set[str],
) -> dict | None:
    """Return one unambiguous failure behind a generic Chromium diagnostic."""

    console_sequence = console.get("event_sequence")
    console_ts = console.get("ts")
    page_id = str(console.get("page_id") or "")
    location_url = str(console.get("location_url") or "")
    if (
        not page_id
        or not location_url
        or not isinstance(console_sequence, int)
        or isinstance(console_sequence, bool)
        or not isinstance(console_ts, (int, float))
        or isinstance(console_ts, bool)
    ):
        return None
    candidates = [
        failure
        for failure in request_failures
        if str(failure.get("request_id") or "") not in consumed_request_ids
        and failure.get("page_id") == page_id
        and failure.get("url") == location_url
        and console.get("text") == f"Failed to load resource: {failure.get('failure')}"
        and isinstance(failure.get("event_sequence"), int)
        and not isinstance(failure.get("event_sequence"), bool)
        and failure["event_sequence"] < console_sequence
        and isinstance(failure.get("ts"), (int, float))
        and not isinstance(failure.get("ts"), bool)
        and 0 <= float(console_ts) - float(failure["ts"]) <= 2
    ]
    return candidates[0] if len(candidates) == 1 else None


def _console_expectation_contract_failures(
    nodeid: str,
    console_rows: list[dict],
    allowances: list[dict],
    request_failures: list[dict],
    http_rows: list[dict],
) -> list[str]:
    failures: list[str] = []
    consumed_401_response_ids: set[str] = set()
    consumed_http_error_response_ids: set[str] = set()
    consumed_console_failure_request_ids: set[str] = set()
    for row in sorted(
        console_rows,
        key=lambda item: (
            item.get("event_sequence")
            if isinstance(item.get("event_sequence"), int) and not isinstance(item.get("event_sequence"), bool)
            else 2**63
        ),
    ):
        if row.get("expected") is True and row.get("expected_source") not in {
            "correlated-role-probe-401",
            "correlated-auth-bootstrap-401",
            "correlated-explicit-request-failure",
            "explicit-http-error-response",
        }:
            failures.append("expected console error has an unknown or missing evidence source")
            continue
        if row.get("expected_source") in {"correlated-role-probe-401", "correlated-auth-bootstrap-401"}:
            correlation = row.get("console_http_correlation")
            matching = [
                response
                for response in http_rows
                if response.get("phase") == "response" and response.get("request_id") == (correlation or {}).get("request_id")
            ]
            if (
                row.get("expected") is not True
                or row.get("expectation_case") != nodeid
                or not isinstance(row.get("expected_reason"), str)
                or len(row["expected_reason"].strip()) < 24
                or not isinstance(correlation, dict)
                or len(matching) != 1
            ):
                failures.append("401 console diagnostic lacks an exact case/HTTP correlation")
                continue
            response = matching[0]
            try:
                response_path = urlsplit(str(response.get("url") or "")).path
            except ValueError:
                response_path = ""
            eligible_responses = [
                candidate for candidate in http_rows if candidate.get("phase") == "response" and candidate.get("status") == 401
            ]
            if row.get("expected_source") == "correlated-role-probe-401":
                expected_probe = (
                    response.get("evidence_probe") in {"control-action-role-v1", "screenshot-role-v1"}
                    and response.get("method") == "GET"
                    and response_path == "/auth/me"
                    and isinstance(response.get("evidence_correlation_id"), str)
                    and _ROLE_EVIDENCE_CORRELATION.fullmatch(response["evidence_correlation_id"])
                )
            else:
                expected_probe = (
                    response.get("evidence_probe") is None
                    and response.get("evidence_correlation_id") is None
                    and row.get("auth_bootstrap_scope") is True
                    and response_path in _AUTH_BOOTSTRAP_401_PATHS
                )
            unique_response = _unique_401_console_response_candidate(
                row,
                eligible_responses,
                consumed_401_response_ids,
            )
            if (
                not expected_probe
                or unique_response is None
                or unique_response.get("request_id") != response.get("request_id")
                or response.get("status") != 401
                or correlation.get("status") != 401
                or correlation.get("status_text") != response.get("status_text")
                or correlation.get("page_id") != row.get("page_id")
                or correlation.get("location_url") != row.get("location_url")
                or row.get("location_url") != response.get("url")
                or correlation.get("path") != response_path
                or correlation.get("method") != response.get("method")
                or correlation.get("evidence_probe") != response.get("evidence_probe")
                or correlation.get("evidence_correlation_id") != response.get("evidence_correlation_id")
                or not isinstance(correlation.get("elapsed_ms"), int)
                or not 0 <= correlation["elapsed_ms"] <= 500
                or not isinstance(correlation.get("response_event_sequence"), int)
                or not isinstance(correlation.get("console_event_sequence"), int)
                or correlation["response_event_sequence"] >= correlation["console_event_sequence"]
                or row.get("text")
                != (f"Failed to load resource: the server responded with a status of 401 ({response.get('status_text') or ''})")
            ):
                failures.append("401 console diagnostic has a stale or out-of-scope HTTP correlation")
            else:
                consumed_401_response_ids.add(str(response["request_id"]))
            continue
        if row.get("expected_source") == "correlated-explicit-request-failure":
            correlation = row.get("request_failure_correlation")
            matching = [failure for failure in request_failures if failure.get("request_id") == (correlation or {}).get("request_id")]
            if (
                row.get("expected") is not True
                or row.get("expectation_case") != nodeid
                or not isinstance(row.get("expected_reason"), str)
                or len(row["expected_reason"].strip()) < 24
                or not isinstance(correlation, dict)
                or len(matching) != 1
            ):
                failures.append("request-failure console diagnostic lacks an exact case correlation")
                continue
            request_failure = matching[0]
            unique_failure = _unique_request_failure_console_candidate(
                row,
                request_failures,
                consumed_console_failure_request_ids,
            )
            if (
                request_failure.get("expected_source") != "explicit-case-allowance"
                or unique_failure is None
                or unique_failure.get("request_id") != request_failure.get("request_id")
                or correlation.get("allowance_id") != request_failure.get("allowance_id")
                or correlation.get("page_id") != row.get("page_id")
                or correlation.get("location_url") != row.get("location_url")
                or row.get("location_url") != request_failure.get("url")
                or correlation.get("method") != request_failure.get("method")
                or correlation.get("resource_type") != request_failure.get("resource_type")
                or correlation.get("failure") != request_failure.get("failure")
                or row.get("text") != f"Failed to load resource: {request_failure.get('failure')}"
                or not isinstance(correlation.get("elapsed_ms"), int)
                or not 0 <= correlation["elapsed_ms"] <= 2000
                or not isinstance(correlation.get("request_failure_event_sequence"), int)
                or not isinstance(correlation.get("console_event_sequence"), int)
                or correlation["request_failure_event_sequence"] >= correlation["console_event_sequence"]
            ):
                failures.append("request-failure console diagnostic has a stale network correlation")
            else:
                consumed_console_failure_request_ids.add(str(request_failure["request_id"]))
            continue
        if row.get("expected_source") != "explicit-http-error-response":
            continue
        correlation = row.get("http_error_correlation")
        allowance_id = str(row.get("allowance_id") or "")
        matching = [allowance for allowance in allowances if allowance.get("allowance_id") == allowance_id]
        if (
            row.get("expected") is not True
            or row.get("expectation_case") != nodeid
            or not isinstance(row.get("expected_reason"), str)
            or len(row["expected_reason"].strip()) < 24
            or len(matching) != 1
            or not isinstance(correlation, dict)
        ):
            failures.append("expected console error lacks an exact case/response contract")
            continue
        allowance = matching[0]
        request_id = str(correlation.get("request_id") or "")
        matching_responses = [
            response for response in http_rows if response.get("phase") == "response" and response.get("request_id") == request_id
        ]
        response = matching_responses[0] if len(matching_responses) == 1 else None
        try:
            response_path = urlsplit(str((response or {}).get("url") or "")).path
        except ValueError:
            response_path = ""
        recomputed_correlation = _http_error_console_correlation(
            row,
            http_rows,
            allowance,
            consumed_http_error_response_ids,
        )
        if (
            request_id not in allowance.get("matched_request_ids", [])
            or response is None
            or recomputed_correlation != correlation
            or response.get("page_id") != row.get("page_id")
            or response.get("method") != allowance.get("method")
            or response_path != allowance.get("path")
            or response.get("status") != allowance.get("status")
            or response.get("event_sequence") != correlation.get("response_event_sequence")
            or correlation.get("page_id") != row.get("page_id")
            or correlation.get("location_url") != row.get("location_url")
            or row.get("location_url") != response.get("url")
            or correlation.get("method") != allowance.get("method")
            or correlation.get("path") != allowance.get("path")
            or correlation.get("status") != allowance.get("status")
            or not isinstance(correlation.get("elapsed_ms"), int)
            or not 0 <= correlation["elapsed_ms"] <= 2000
            or not isinstance(correlation.get("response_event_sequence"), int)
            or not isinstance(correlation.get("console_event_sequence"), int)
            or correlation["response_event_sequence"] >= correlation["console_event_sequence"]
        ):
            failures.append(f"expected console error has a stale response correlation: {allowance_id or '<missing>'}")
        else:
            consumed_http_error_response_ids.add(request_id)
    consumed: list[str] = []
    for allowance in allowances:
        matched = allowance.get("matched_request_ids")
        expected_count = allowance.get("expected_count")
        if not isinstance(matched, list) or len(matched) != expected_count:
            failures.append(
                f"HTTP console allowance matched {len(matched) if isinstance(matched, list) else 'invalid'} of "
                f"{expected_count}: {allowance.get('allowance_id') or '<missing>'}"
            )
            continue
        consumed.extend(str(item) for item in matched)
    if len(consumed) != len(set(consumed)):
        failures.append("one HTTP response was consumed by multiple console allowances")
    correlated_http_ids = [
        str((row.get("console_http_correlation") or {}).get("request_id"))
        for row in console_rows
        if row.get("expected_source") in {"correlated-role-probe-401", "correlated-auth-bootstrap-401"}
    ]
    if "" in correlated_http_ids or len(correlated_http_ids) != len(set(correlated_http_ids)):
        failures.append("one 401 HTTP response was consumed by multiple console diagnostics")
    correlated_failure_ids = [
        str((row.get("request_failure_correlation") or {}).get("request_id"))
        for row in console_rows
        if row.get("expected_source") == "correlated-explicit-request-failure"
    ]
    if "" in correlated_failure_ids or len(correlated_failure_ids) != len(set(correlated_failure_ids)):
        failures.append("one request failure was consumed by multiple console diagnostics")
    return failures


def _replay_automatic_browser_correlations(
    *,
    console_rows: list[dict],
    request_failures: list[dict],
    http_rows: list[dict],
    navigation_events: list[dict],
) -> dict:
    """Purely replay automatic classifications from persisted browser rows."""

    navigation_correlations: list[dict] = []
    unexpected_request_ids: list[str] = []
    for failure in request_failures:
        correlation = _navigation_cancel_correlation(failure, http_rows, navigation_events)
        if correlation is None:
            unexpected_request_ids.append(str(failure.get("request_id") or "<missing>"))
        else:
            navigation_correlations.append(
                {
                    "request_id": str(failure.get("request_id") or ""),
                    "correlation": correlation,
                }
            )

    consumed_401_request_ids: set[str] = set()
    console_correlations: list[dict] = []
    unexpected_console_sequences: list[int | str] = []
    relevant_console_rows = [row for row in console_rows if row.get("type") == "error" and row.get("text") == _AUTH_BOOTSTRAP_CONSOLE_401]
    for console in sorted(
        relevant_console_rows,
        key=lambda row: (
            row.get("event_sequence")
            if isinstance(row.get("event_sequence"), int) and not isinstance(row.get("event_sequence"), bool)
            else 2**63
        ),
    ):
        classification = _automatic_401_console_correlation(
            console,
            http_rows,
            consumed_401_request_ids,
        )
        if classification is None:
            unexpected_console_sequences.append(console.get("event_sequence", "<missing>"))
            continue
        consumed_401_request_ids.add(classification["request_id"])
        console_correlations.append(
            {
                "event_sequence": console.get("event_sequence"),
                **classification,
            }
        )
    return {
        "navigation_correlations": navigation_correlations,
        "console_correlations": console_correlations,
        "unexpected_request_ids": unexpected_request_ids,
        "unexpected_console_sequences": unexpected_console_sequences,
    }


def run_browser_failure_policy_self_check() -> dict:
    """Exercise fail-open bypasses without a browser, network, or mock provider."""

    old_url = "http://127.0.0.1:58123/ui/chat.html"
    target_url = "http://127.0.0.1:58123/ui/settings.html"
    source_url = "http://127.0.0.1:58123/api/slow"
    source = {
        "ts": 100.02,
        "event_sequence": 10,
        "phase": "request",
        "page_id": "browser-page-001",
        "frame_id": "guid:frame-1",
        "frame_url": old_url,
        "request_id": "browser-000001",
        "method": "GET",
        "url": source_url,
        "resource_type": "fetch",
    }
    navigation = {
        "ts": 100.0,
        "event_sequence": 5,
        "phase": "request",
        "page_id": "browser-page-001",
        "frame_id": "guid:frame-1",
        "frame_url": old_url,
        "request_id": "browser-000002",
        "method": "GET",
        "url": target_url,
        "resource_type": "document",
        "is_navigation_request": True,
        "is_main_frame": True,
    }
    response = {
        **navigation,
        "ts": 100.1,
        "event_sequence": 20,
        "phase": "response",
        "status": 200,
    }
    failure = {
        **source,
        "ts": 100.15,
        "event_sequence": 30,
        "failure": "net::ERR_ABORTED",
        "expected": False,
    }
    commit = {
        "ts": 100.16,
        "event_sequence": 40,
        "event": "main-frame-committed",
        "page_id": "browser-page-001",
        "frame_id": "guid:frame-1",
        "url": target_url,
    }
    http = [source, navigation, response]
    checks: dict[str, bool] = {
        "valid_navigation_cancel_passes": _navigation_cancel_correlation(failure, http, [commit]) is not None,
        "abort_without_navigation_fails": _navigation_cancel_correlation(failure, [source], []) is None,
        "abortcontroller_without_document_transaction_fails": (_navigation_cancel_correlation(failure, [source], []) is None),
        "response_without_commit_fails": _navigation_cancel_correlation(failure, http, []) is None,
    }

    later_same_url_commit = {**commit, "ts": 100.3, "event_sequence": 50}
    later_document_request = {
        **navigation,
        "ts": 100.25,
        "event_sequence": 45,
        "request_id": "browser-000004",
        "frame_url": target_url,
    }
    checks["later_same_url_commit_does_not_ambiguate_transaction"] = (
        _navigation_cancel_correlation(
            failure,
            [*http, later_document_request],
            [commit, later_same_url_commit],
        )
        is not None
    )
    duplicate_transaction_commit = {**commit, "ts": 100.17, "event_sequence": 41}
    checks["duplicate_commit_in_same_navigation_transaction_fails"] = (
        _navigation_cancel_correlation(
            failure,
            http,
            [commit, duplicate_transaction_commit],
        )
        is None
    )

    foreign_navigation = {**navigation, "frame_id": "guid:frame-2"}
    foreign_response = {**response, "frame_id": "guid:frame-2"}
    foreign_commit = {**commit, "frame_id": "guid:frame-2"}
    checks["different_frame_navigation_fails"] = (
        _navigation_cancel_correlation(failure, [source, foreign_navigation, foreign_response], [foreign_commit]) is None
    )

    destination_source = {**source, "frame_url": target_url}
    destination_failure = {**failure, "frame_url": target_url}
    destination_navigation = {**navigation, "frame_url": target_url}
    destination_response = {**response, "frame_url": target_url}
    checks["destination_owned_source_fails"] = (
        _navigation_cancel_correlation(
            destination_failure,
            [destination_source, destination_navigation, destination_response],
            [commit],
        )
        is None
    )

    late_response = {**response, "event_sequence": 35, "ts": 100.17}
    checks["failure_before_document_response_fails"] = (
        _navigation_cancel_correlation(failure, [source, navigation, late_response], [commit]) is None
    )
    failed_response = {**response, "status": 500}
    checks["failed_document_response_fails"] = (
        _navigation_cancel_correlation(failure, [source, navigation, failed_response], [commit]) is None
    )

    second_navigation = {
        **navigation,
        "event_sequence": 6,
        "request_id": "browser-000003",
    }
    second_response = {
        **response,
        "event_sequence": 21,
        "request_id": "browser-000003",
    }
    second_commit = {**commit, "event_sequence": 41}
    checks["ambiguous_navigation_fails"] = (
        _navigation_cancel_correlation(
            failure,
            [source, navigation, response, second_navigation, second_response],
            [commit, second_commit],
        )
        is None
    )

    allowance = {
        "allowance_id": "request-failure-allowance-001",
        "method": "GET",
        "path": "/api/slow",
        "resource_type": "fetch",
        "failure": "net::ERR_ABORTED",
        "reason": "the self-check intentionally cancels this exact request once",
        "registered_after_event_sequence": 15,
        "matched_request_ids": [],
    }
    checks["exact_case_allowance_passes"] = _exact_failure_allowance_match(failure, source, [allowance]) is allowance
    for field, wrong_value in (
        ("method", "POST"),
        ("url", "http://127.0.0.1:58123/api/other"),
        ("resource_type", "script"),
        ("failure", "net::ERR_FAILED"),
    ):
        altered = {**failure, field: wrong_value}
        checks[f"allowance_{field}_mismatch_fails"] = _exact_failure_allowance_match(altered, source, [allowance]) is None
    late_allowance = {**allowance, "registered_after_event_sequence": 30}
    checks["pre_registration_failure_fails"] = _exact_failure_allowance_match(failure, source, [late_allowance]) is None

    allowance["matched_request_ids"] = ["browser-000001"]
    expected_failure = {
        **failure,
        "expected": True,
        "expected_source": "explicit-case-allowance",
        "expectation_case": "self-check::exact",
        "expected_reason": allowance["reason"],
        "allowance_id": allowance["allowance_id"],
        "allowance_correlation": {
            "case": "self-check::exact",
            "method": "GET",
            "path": "/api/slow",
            "resource_type": "fetch",
            "failure": "net::ERR_ABORTED",
            "registered_after_event_sequence": 15,
        },
    }
    checks["structured_allowance_contract_passes"] = not _request_failure_expectation_contract_failures(
        "self-check::exact",
        [expected_failure],
        [allowance],
    )
    unused_allowance = {**allowance, "matched_request_ids": []}
    checks["unused_allowance_contract_fails"] = bool(
        _request_failure_expectation_contract_failures("self-check::exact", [], [unused_allowance])
    )
    overused_allowance = {**allowance, "matched_request_ids": ["browser-000001", "browser-000002"]}
    checks["overused_allowance_contract_fails"] = bool(
        _request_failure_expectation_contract_failures("self-check::exact", [], [overused_allowance])
    )
    incomplete_expected = {**expected_failure, "allowance_correlation": None}
    checks["unstructured_expected_failure_fails"] = bool(
        _request_failure_expectation_contract_failures(
            "self-check::exact",
            [incomplete_expected],
            [allowance],
        )
    )

    http_error_response = {
        "ts": 200.0,
        "event_sequence": 50,
        "phase": "response",
        "page_id": "browser-page-001",
        "request_id": "browser-000050",
        "method": "GET",
        "url": "http://127.0.0.1:58123/graph",
        "status": 500,
        "status_text": "Internal Server Error",
    }
    http_error_console = {
        "ts": 200.01,
        "event_sequence": 51,
        "page_id": "browser-page-001",
        "location_url": "http://127.0.0.1:58123/graph",
        "type": "error",
        "text": "Failed to load resource: the server responded with a status of 500 (Internal Server Error)",
        "expected": False,
    }
    console_allowance = {
        "allowance_id": "http-console-allowance-001",
        "method": "GET",
        "path": "/graph",
        "status": 500,
        "expected_count": 1,
        "reason": "the self-check requires this exact real HTTP failure diagnostic",
        "registered_after_event_sequence": 49,
        "matched_request_ids": [],
    }
    http_console_correlation = _http_error_console_correlation(
        http_error_console,
        [http_error_response],
        console_allowance,
        set(),
    )
    checks["exact_http_console_response_passes"] = http_console_correlation is not None
    checks["http_console_path_mismatch_fails"] = (
        _http_error_console_correlation(
            http_error_console,
            [http_error_response],
            {**console_allowance, "path": "/other"},
            set(),
        )
        is None
    )
    checks["http_console_pre_registration_response_fails"] = (
        _http_error_console_correlation(
            http_error_console,
            [http_error_response],
            {**console_allowance, "registered_after_event_sequence": 50},
            set(),
        )
        is None
    )
    unrelated_http_error_response = {
        **http_error_response,
        "ts": 200.005,
        "event_sequence": 49,
        "request_id": "browser-000049",
        "url": "http://127.0.0.1:58123/other",
    }
    checks["exact_console_location_disambiguates_unrelated_http_response"] = (
        _http_error_console_correlation(
            http_error_console,
            [http_error_response, unrelated_http_error_response],
            console_allowance,
            set(),
        )
        == http_console_correlation
    )
    duplicate_http_error_response = {
        **http_error_response,
        "ts": 200.006,
        "event_sequence": 48,
        "request_id": "browser-000048",
    }
    checks["same_location_http_response_is_ambiguous"] = (
        _http_error_console_correlation(
            http_error_console,
            [http_error_response, duplicate_http_error_response],
            console_allowance,
            set(),
        )
        is None
    )
    checks["missing_http_console_location_fails"] = (
        _http_error_console_correlation(
            {**http_error_console, "location_url": ""},
            [http_error_response],
            console_allowance,
            set(),
        )
        is None
    )
    matched_console_allowance = {**console_allowance, "matched_request_ids": ["browser-000050"]}
    expected_http_console = {
        **http_error_console,
        "expected": True,
        "expected_source": "explicit-http-error-response",
        "expectation_case": "self-check::exact",
        "expected_reason": console_allowance["reason"],
        "allowance_id": console_allowance["allowance_id"],
        "http_error_correlation": http_console_correlation,
    }
    checks["structured_http_console_contract_passes"] = not _console_expectation_contract_failures(
        "self-check::exact",
        [expected_http_console],
        [matched_console_allowance],
        [],
        [http_error_response],
    )
    checks["different_location_http_console_contract_passes"] = not _console_expectation_contract_failures(
        "self-check::exact",
        [expected_http_console],
        [matched_console_allowance],
        [],
        [http_error_response, unrelated_http_error_response],
    )
    checks["same_location_http_console_contract_fails"] = bool(
        _console_expectation_contract_failures(
            "self-check::exact",
            [expected_http_console],
            [matched_console_allowance],
            [],
            [http_error_response, duplicate_http_error_response],
        )
    )
    checks["unused_http_console_allowance_fails"] = bool(
        _console_expectation_contract_failures(
            "self-check::exact",
            [],
            [console_allowance],
            [],
            [http_error_response],
        )
    )

    request_failure_console = {
        "ts": 100.16,
        "event_sequence": 31,
        "page_id": "browser-page-001",
        "location_url": source_url,
        "text": "Failed to load resource: net::ERR_ABORTED",
        "expected": True,
        "expected_source": "correlated-explicit-request-failure",
        "expectation_case": "self-check::exact",
        "expected_reason": "the exact request failure produced this one Chromium console diagnostic",
        "request_failure_correlation": {
            "request_id": "browser-000001",
            "allowance_id": "request-failure-allowance-001",
            "page_id": "browser-page-001",
            "location_url": source_url,
            "method": "GET",
            "path": "/api/slow",
            "resource_type": "fetch",
            "failure": "net::ERR_ABORTED",
            "request_failure_event_sequence": 30,
            "console_event_sequence": 31,
            "elapsed_ms": 10,
        },
    }
    checks["request_failure_console_contract_passes"] = not _console_expectation_contract_failures(
        "self-check::exact",
        [request_failure_console],
        [],
        [expected_failure],
        [],
    )
    unrelated_request_failure = {
        **failure,
        "ts": 100.14,
        "event_sequence": 29,
        "request_id": "browser-000029",
        "url": "http://127.0.0.1:58123/api/other",
    }
    duplicate_location_request_failure = {
        **unrelated_request_failure,
        "request_id": "browser-000028",
        "url": source_url,
    }
    checks["exact_failure_console_location_disambiguates_unrelated_failure"] = (
        _unique_request_failure_console_candidate(
            request_failure_console,
            [expected_failure, unrelated_request_failure],
            set(),
        )
        is expected_failure
    )
    checks["same_location_request_failures_are_ambiguous"] = (
        _unique_request_failure_console_candidate(
            request_failure_console,
            [expected_failure, duplicate_location_request_failure],
            set(),
        )
        is None
    )
    checks["missing_request_failure_console_location_fails"] = (
        _unique_request_failure_console_candidate(
            {**request_failure_console, "location_url": ""},
            [expected_failure],
            set(),
        )
        is None
    )
    checks["same_location_request_failure_console_contract_fails"] = bool(
        _console_expectation_contract_failures(
            "self-check::exact",
            [request_failure_console],
            [],
            [expected_failure, duplicate_location_request_failure],
            [],
        )
    )

    auth_response = {
        "ts": 300.0,
        "event_sequence": 60,
        "phase": "response",
        "page_id": "browser-page-001",
        "request_id": "browser-000060",
        "method": "GET",
        "url": "http://127.0.0.1:58123/auth/me",
        "status": 401,
        "status_text": "Unauthorized",
        "evidence_probe": None,
    }
    auth_console = {
        "ts": 300.01,
        "event_sequence": 61,
        "page_id": "browser-page-001",
        "location_url": "http://127.0.0.1:58123/auth/me",
        "text": _AUTH_BOOTSTRAP_CONSOLE_401,
        "expected": True,
        "expected_source": "correlated-auth-bootstrap-401",
        "expectation_case": "self-check::exact",
        "expected_reason": "the scoped real login bootstrap produced this exact 401 diagnostic",
        "auth_bootstrap_scope": True,
        "console_http_correlation": {
            "request_id": "browser-000060",
            "page_id": "browser-page-001",
            "location_url": "http://127.0.0.1:58123/auth/me",
            "method": "GET",
            "path": "/auth/me",
            "status": 401,
            "status_text": "Unauthorized",
            "evidence_probe": None,
            "response_event_sequence": 60,
            "console_event_sequence": 61,
            "elapsed_ms": 10,
        },
    }
    checks["auth_bootstrap_console_contract_passes"] = not _console_expectation_contract_failures(
        "self-check::exact",
        [auth_console],
        [],
        [],
        [auth_response],
    )
    checks["auth_bootstrap_console_without_scope_fails"] = bool(
        _console_expectation_contract_failures(
            "self-check::exact",
            [{**auth_console, "auth_bootstrap_scope": False}],
            [],
            [],
            [auth_response],
        )
    )
    second_auth_response = {
        **auth_response,
        "ts": 300.005,
        "event_sequence": 59,
        "request_id": "browser-000059",
        "url": "http://127.0.0.1:58123/auth/me",
    }
    unrelated_401_response = {
        **auth_response,
        "ts": 300.004,
        "event_sequence": 58,
        "request_id": "browser-000058",
        "url": "http://127.0.0.1:58123/workspace/files",
    }
    checks["unique_auth_bootstrap_401_candidate_passes"] = (
        _unique_401_console_response_candidate(auth_console, [auth_response], set()) is auth_response
    )
    checks["ambiguous_auth_bootstrap_401_candidate_fails"] = (
        _unique_401_console_response_candidate(
            auth_console,
            [auth_response, second_auth_response],
            set(),
        )
        is None
    )
    checks["ambiguous_auth_bootstrap_console_contract_fails"] = bool(
        _console_expectation_contract_failures(
            "self-check::exact",
            [auth_console],
            [],
            [],
            [auth_response, second_auth_response],
        )
    )
    checks["auth_bootstrap_location_disambiguates_unrelated_401"] = (
        _unique_401_console_response_candidate(
            auth_console,
            [auth_response, unrelated_401_response],
            set(),
        )
        is auth_response
    )
    checks["auth_bootstrap_plus_unrelated_401_contract_passes"] = not _console_expectation_contract_failures(
        "self-check::exact",
        [auth_console],
        [],
        [],
        [auth_response, unrelated_401_response],
    )
    checks["missing_401_console_location_fails"] = (
        _unique_401_console_response_candidate(
            {**auth_console, "location_url": ""},
            [auth_response],
            set(),
        )
        is None
    )

    startup_responses: list[dict] = []
    startup_consoles: list[dict] = []
    for offset, path in enumerate(("/health/summary", "/auth/me", "/chat/turns/running")):
        sequence = 80 + offset * 2
        timestamp = 400.0 + offset * 0.01
        request_id = f"browser-000{sequence:03d}"
        url = f"http://127.0.0.1:58123{path}"
        startup_responses.append(
            {
                **auth_response,
                "ts": timestamp,
                "event_sequence": sequence,
                "request_id": request_id,
                "url": url,
            }
        )
        startup_consoles.append(
            {
                **auth_console,
                "ts": timestamp + 0.001,
                "event_sequence": sequence + 1,
                "location_url": url,
                "console_http_correlation": {
                    **auth_console["console_http_correlation"],
                    "request_id": request_id,
                    "location_url": url,
                    "path": path,
                    "response_event_sequence": sequence,
                    "console_event_sequence": sequence + 1,
                    "elapsed_ms": 1,
                },
            }
        )
    checks["real_startup_auth_bootstrap_401_sequence_passes"] = not _console_expectation_contract_failures(
        "self-check::exact",
        startup_consoles,
        [],
        [],
        startup_responses,
    )

    role_response = {
        **auth_response,
        "ts": 310.0,
        "event_sequence": 70,
        "request_id": "browser-000070",
        "evidence_probe": "control-action-role-v1",
        "evidence_correlation_id": "control-action-role-1700000000000-1",
    }
    role_console = {
        **auth_console,
        "ts": 310.01,
        "event_sequence": 71,
        "expected_source": "correlated-role-probe-401",
        "expected_reason": "the exact real role probe produced this one Chromium 401 diagnostic",
        "auth_bootstrap_scope": False,
        "console_http_correlation": {
            **auth_console["console_http_correlation"],
            "request_id": "browser-000070",
            "evidence_probe": "control-action-role-v1",
            "evidence_correlation_id": "control-action-role-1700000000000-1",
            "response_event_sequence": 70,
            "console_event_sequence": 71,
        },
    }
    second_role_response = {
        **role_response,
        "ts": 310.005,
        "event_sequence": 69,
        "request_id": "browser-000069",
        "evidence_probe": "screenshot-role-v1",
        "evidence_correlation_id": "screenshot-role-1700000000000-2",
    }
    checks["unique_role_probe_401_candidate_passes"] = (
        _unique_401_console_response_candidate(role_console, [role_response], set()) is role_response
    )
    checks["ambiguous_role_probe_401_candidate_fails"] = (
        _unique_401_console_response_candidate(
            role_console,
            [role_response, second_role_response],
            set(),
        )
        is None
    )
    checks["ambiguous_role_probe_console_contract_fails"] = bool(
        _console_expectation_contract_failures(
            "self-check::exact",
            [role_console],
            [],
            [],
            [role_response, second_role_response],
        )
    )
    role_unrelated_401 = {
        **unrelated_401_response,
        "ts": 310.004,
        "event_sequence": 68,
        "request_id": "browser-000068",
    }
    checks["role_probe_location_disambiguates_unrelated_401"] = (
        _unique_401_console_response_candidate(
            role_console,
            [role_response, role_unrelated_401],
            set(),
        )
        is role_response
    )
    checks["role_probe_plus_unrelated_401_contract_passes"] = not _console_expectation_contract_failures(
        "self-check::exact",
        [role_console],
        [],
        [],
        [role_response, role_unrelated_401],
    )

    rapid_role_responses: list[dict] = []
    rapid_role_consoles: list[dict] = []
    for offset in range(5):
        sequence = 100 + offset * 2
        timestamp = 500.0 + offset * 0.01
        request_id = f"browser-000{sequence:03d}"
        rapid_role_responses.append(
            {
                **role_response,
                "ts": timestamp,
                "event_sequence": sequence,
                "request_id": request_id,
                "evidence_correlation_id": f"control-action-role-1700000000000-{offset + 10}",
            }
        )
        rapid_role_consoles.append(
            {
                **role_console,
                "ts": timestamp + 0.001,
                "event_sequence": sequence + 1,
                "console_http_correlation": {
                    **role_console["console_http_correlation"],
                    "request_id": request_id,
                    "evidence_correlation_id": f"control-action-role-1700000000000-{offset + 10}",
                    "response_event_sequence": sequence,
                    "console_event_sequence": sequence + 1,
                    "elapsed_ms": 1,
                },
            }
        )
    checks["rapid_role_probe_401_sequence_passes"] = not _console_expectation_contract_failures(
        "self-check::exact",
        rapid_role_consoles,
        [],
        [],
        rapid_role_responses,
    )

    # Sanitized replay fixture extracted from fresh smoke
    # 20260824T195737Z-4fd63e.  It retains the real callback ordering: a login
    # document response, ten cancellations, the matching commit, a later
    # same-URL goto/commit, three startup 401s, and five rapid role probes.
    replay_origin = "http://127.0.0.1:58123"
    replay_page_id = "browser-page-001"
    replay_frame_id = "guid:frame-smoke-replay"
    replay_chat_url = f"{replay_origin}/ui/chat.html"
    replay_login_url = f"{replay_origin}/ui/login.html?<redacted>"
    replay_source_specs = (
        (8, 16, 38, "/health/summary", "fetch"),
        (9, 17, 37, "/chat/turns/running", "fetch"),
        (10, 18, 39, "/auth/me", "fetch"),
        (12, 26, 43, "/ui/chat/state.js", "script"),
        (13, 27, 40, "/ui/chat/share-dialog.js", "script"),
        (14, 28, 34, "/ui/chat/render.js", "script"),
        (15, 29, 35, "/ui/chat/history.js", "script"),
        (16, 30, 36, "/ui/chat/stream.js", "script"),
        (17, 31, 41, "/ui/chat/scope.js", "script"),
        (18, 32, 42, "/ui/chat/menus.js", "script"),
    )
    replay_requests: list[dict] = []
    replay_failures: list[dict] = []
    for request_number, request_sequence, failure_sequence, path, resource_type in replay_source_specs:
        request_id = f"browser-{request_number:06d}"
        url = replay_origin + path
        common = {
            "page_id": replay_page_id,
            "frame_id": replay_frame_id,
            "frame_url": replay_chat_url,
            "request_id": request_id,
            "method": "GET",
            "url": url,
            "resource_type": resource_type,
            "is_main_frame": True,
            "is_navigation_request": False,
        }
        replay_requests.append(
            {
                **common,
                "ts": 600.0 + request_sequence / 1000,
                "event_sequence": request_sequence,
                "phase": "request",
            }
        )
        replay_failures.append(
            {
                **common,
                "ts": 600.0 + failure_sequence / 1000,
                "event_sequence": failure_sequence,
                "failure": "net::ERR_ABORTED",
                "expected": False,
            }
        )
    replay_navigation_request = {
        "ts": 600.021,
        "event_sequence": 21,
        "phase": "request",
        "page_id": replay_page_id,
        "frame_id": replay_frame_id,
        "frame_url": replay_chat_url,
        "request_id": "browser-000011",
        "method": "GET",
        "url": replay_login_url,
        "resource_type": "document",
        "is_main_frame": True,
        "is_navigation_request": True,
    }
    replay_navigation_response = {
        **replay_navigation_request,
        "ts": 600.033,
        "event_sequence": 33,
        "phase": "response",
        "status": 200,
    }
    replay_later_navigation_request = {
        **replay_navigation_request,
        "ts": 600.055,
        "event_sequence": 55,
        "frame_url": replay_login_url,
        "request_id": "browser-000024",
    }
    replay_later_navigation_response = {
        **replay_later_navigation_request,
        "ts": 600.056,
        "event_sequence": 56,
        "phase": "response",
        "status": 200,
    }
    replay_commits = [
        {
            "ts": 600.044,
            "event_sequence": 44,
            "event": "main-frame-committed",
            "page_id": replay_page_id,
            "frame_id": replay_frame_id,
            "url": replay_login_url,
        },
        {
            "ts": 600.057,
            "event_sequence": 57,
            "event": "main-frame-committed",
            "page_id": replay_page_id,
            "frame_id": replay_frame_id,
            "url": replay_login_url,
        },
    ]
    replay_auth_specs = (
        (10, 19, 20, "/auth/me"),
        (9, 22, 23, "/chat/turns/running"),
        (8, 24, 25, "/health/summary"),
    )
    replay_401_responses: list[dict] = []
    replay_401_consoles: list[dict] = []
    for request_number, response_sequence, console_sequence, path in replay_auth_specs:
        url = replay_origin + path
        replay_401_responses.append(
            {
                "ts": 600.0 + response_sequence / 1000,
                "event_sequence": response_sequence,
                "phase": "response",
                "page_id": replay_page_id,
                "request_id": f"browser-{request_number:06d}",
                "method": "GET",
                "url": url,
                "status": 401,
                "status_text": "Unauthorized",
                "evidence_probe": None,
                "evidence_correlation_id": None,
            }
        )
        replay_401_consoles.append(
            {
                "ts": 600.0 + console_sequence / 1000,
                "event_sequence": console_sequence,
                "page_id": replay_page_id,
                "type": "error",
                "text": _AUTH_BOOTSTRAP_CONSOLE_401,
                "location_url": url,
                "auth_bootstrap_scope": True,
            }
        )
    for request_number, response_sequence, console_sequence in (
        (30, 69, 70),
        (31, 73, 74),
        (32, 75, 76),
        (33, 78, 79),
        (35, 82, 83),
    ):
        correlation_id = f"control-action-role-1700000000000-{request_number}"
        replay_401_responses.append(
            {
                "ts": 600.0 + response_sequence / 1000,
                "event_sequence": response_sequence,
                "phase": "response",
                "page_id": replay_page_id,
                "request_id": f"browser-{request_number:06d}",
                "method": "GET",
                "url": replay_origin + "/auth/me",
                "status": 401,
                "status_text": "Unauthorized",
                "evidence_probe": "control-action-role-v1",
                "evidence_correlation_id": correlation_id,
            }
        )
        replay_401_consoles.append(
            {
                "ts": 600.0 + console_sequence / 1000,
                "event_sequence": console_sequence,
                "page_id": replay_page_id,
                "type": "error",
                "text": _AUTH_BOOTSTRAP_CONSOLE_401,
                "location_url": replay_origin + "/auth/me",
                "auth_bootstrap_scope": True,
            }
        )
    replay_http = [
        *replay_requests,
        replay_navigation_request,
        replay_navigation_response,
        replay_later_navigation_request,
        replay_later_navigation_response,
        *replay_401_responses,
    ]
    fresh_smoke_replay = _replay_automatic_browser_correlations(
        console_rows=replay_401_consoles,
        request_failures=replay_failures,
        http_rows=replay_http,
        navigation_events=replay_commits,
    )
    checks["fresh_smoke_artifact_replay_passes"] = (
        len(fresh_smoke_replay["navigation_correlations"]) == 10
        and len(fresh_smoke_replay["console_correlations"]) == 8
        and not fresh_smoke_replay["unexpected_request_ids"]
        and not fresh_smoke_replay["unexpected_console_sequences"]
    )
    replay_without_exact_commit = _replay_automatic_browser_correlations(
        console_rows=replay_401_consoles,
        request_failures=replay_failures,
        http_rows=replay_http,
        navigation_events=replay_commits[1:],
    )
    checks["fresh_smoke_replay_without_exact_commit_fails"] = len(replay_without_exact_commit["unexpected_request_ids"]) == 10

    checks["admin_http_role_passes"] = _is_valid_control_authorization_observation({"status": 200, "role": "admin", "auth_disabled": False})
    checks["anonymous_http_role_passes"] = _is_valid_control_authorization_observation(
        {"status": 401, "role": "anonymous", "auth_disabled": False}
    )
    checks["status_zero_role_fails"] = not _is_valid_control_authorization_observation(
        {"status": 0, "role": "unknown", "auth_disabled": False}
    )
    checks["unknown_success_role_fails"] = not _is_valid_control_authorization_observation(
        {"status": 200, "role": "unknown", "auth_disabled": False}
    )

    failed = sorted(name for name, passed in checks.items() if not passed)
    assert not failed, "browser failure policy self-check failed: " + ", ".join(failed)
    return {
        "status": "PASS",
        "policy": "fail-closed navigation cancellation, exact case allowance, and verified role evidence",
        "checks": [{"name": name, "status": "PASS"} for name in sorted(checks)],
        "checked_count": len(checks),
    }


def _is_safe_secret_metadata_scalar(name: str, value) -> bool:
    if isinstance(value, bool):
        return True
    if name.endswith("_sha256") and isinstance(value, str):
        return re.fullmatch(r"[0-9a-f]{64}", value) is not None
    if name == "authorization_evidence_correlation_id" and isinstance(value, str):
        return re.fullmatch(r"control-action-role-\d{10,14}-\d+", value) is not None
    if name == "authorization_request_id" and isinstance(value, str):
        return re.fullmatch(r"browser-\d{6,}", value) is not None
    if name == "authorization_mode" and isinstance(value, str):
        return value in {"awaited-pre-action", "concurrent-action-probe"}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return name.endswith(("_count", "_seconds", "_epoch_seconds", "_masked"))
    return False


_CLOSED_SHADOW_TRACKING_SCRIPT = r"""
(() => {
  const installed = Symbol.for('sherpa.ui.closedShadowTracking');
  if (Element.prototype[installed]) return;
  const original = Element.prototype.attachShadow;
  Object.defineProperty(Element.prototype, 'attachShadow', {
    configurable: true,
    writable: true,
    value(init) {
      const root = original.call(this, init);
      if (init && init.mode === 'closed') {
        this.setAttribute('data-sherpa-ui-closed-shadow-host', '1');
      }
      return root;
    },
  });
  Object.defineProperty(Element.prototype, installed, {value: true});
})()
"""

_CONTROL_ACTION_CAPTURE_SCRIPT_TEMPLATE = r"""
(() => {
  const installed = Symbol.for('sherpa.ui.trustedControlCapture');
  if (window[installed]) return;
  const captureState = {authorizationProbe: 'per-trusted-event'};
  Object.defineProperty(window, installed, {value: captureState});

  const nonce = __SHERPA_CONTROL_NONCE__;
  const bindingName = '__sherpaUiRecordTrustedControlAction';
  const startBindingName = '__sherpaUiRecordTrustedControlStart';
  const armedAuthorizationKey = '__sherpaUiArmedControlAuthorization';
  const browserNow = Date.now.bind(Date);
  let authorizationSequence = 0;
  const viewportClass = width => width <= 500 ? 'narrow' : width >= 1024 ? 'desktop' : 'intermediate';
  const interactiveSelector = [
    'button', 'input:not([type="hidden"])', 'select', 'textarea', 'a[href]', 'summary',
    '[role="button"]', '[role="checkbox"]', '[role="combobox"]', '[role="link"]',
    '[role="switch"]', '[role="tab"]', '[role="textbox"]', '[tabindex]', '[contenteditable]'
  ].join(',');

  const observeAuthorization = async correlationId => {
    const observed = () => browserNow() / 1000;
    if (!/^https?:$/.test(location.protocol)) {
      throw new Error('control authorization requires an HTTP document');
    }
    const response = await fetch('/auth/me', {
      method: 'GET',
      credentials: 'same-origin',
      keepalive: true,
      headers: {
        'Accept': 'application/json',
        'X-Sherpa-UI-Evidence-Probe': 'control-action-role-v1',
        'X-Sherpa-UI-Evidence-Correlation': correlationId,
      },
    });
    if (response.status === 401 || response.status === 403) {
      return {
        status: response.status,
        role: 'anonymous',
        auth_disabled: false,
        observed_at_epoch_seconds: observed(),
        evidence_correlation_id: correlationId,
      };
    }
    if (response.status < 200 || response.status >= 300) {
      throw new Error('control authorization returned a non-success status');
    }
    const body = await response.json().catch(() => ({}));
    const role = String(body.role || '');
    if (!['admin', 'user'].includes(role)) {
      throw new Error('control authorization returned no verified role');
    }
    return {
      status: response.status,
      role,
      auth_disabled: body.auth_disabled === true,
      observed_at_epoch_seconds: observed(),
      evidence_correlation_id: correlationId,
    };
  };

  const explicitControlAttribute = 'data-sherpa-ui-evidence-control-key';
  const interactiveFromEvent = event => {
    const path = typeof event.composedPath === 'function' ? event.composedPath() : [event.target];
    for (const candidate of path) {
      if (candidate instanceof Element
          && (candidate.matches(interactiveSelector) || candidate.hasAttribute(explicitControlAttribute))) {
        return candidate;
      }
    }
    return null;
  };
  const nonIdentityClasses = new Set([
    'act-btn', 'btn-ghost', 'btn-primary', 'btn-secondary', 'danger',
    'filterchip', 'iconbtn', 'mini', 'on', 'small'
  ]);
  const hrefPreferredClasses = new Set(['crumb', 'help-link', 'tab-link']);
  const normalizedHrefKey = element => {
    let value = String(element.getAttribute('href') || '').trim();
    if (!value || value.length > 140 || /[\r\n\t${}]/.test(value)) return null;
    value = value.split(/[?#]/, 1)[0];
    if (!value) return null;
    if (value.startsWith('./')) value = value.slice(2);
    if (/^https?:\/\//i.test(value)) {
      try {
        const parsed = new URL(value);
        if (parsed.username || parsed.password || !/^[a-z0-9.-]+$/i.test(parsed.hostname)) return null;
        const path = parsed.pathname || '/';
        if (!/^(?:\/|[a-z0-9])[a-z0-9._~!&'()*+,;=:@%/+\-]*$/i.test(path)) return null;
        return `@href:${parsed.protocol.toLowerCase()}//${parsed.host.toLowerCase()}${path}`;
      } catch (_) { return null; }
    }
    if (value.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(value)) return null;
    if (value.split('/').some(part => part === '..')) return null;
    if (!/^(?:\/|[a-z0-9])[a-z0-9._~!&'()*+,;=:@%/+\-]*$/i.test(value)) return null;
    return `@href:${value}`;
  };
  const isExplicitControlKey = (element, key) => {
    if (/^[A-Za-z][A-Za-z0-9_.:-]*$/.test(key)) return true;
    if (/^@selector:\[data-[a-z0-9_-]+\]$/.test(key)) return true;
    const classMatch = key.match(/^@selector:\.([A-Za-z][A-Za-z0-9_-]*)$/);
    if (classMatch && !nonIdentityClasses.has(classMatch[1].toLowerCase())) return true;
    if (/^@id-prefix:[A-Za-z][A-Za-z0-9_.:-]{1,119}[-_.:]$/.test(key)) return true;
    if (/^@unkeyed:web\/[A-Za-z0-9_./-]+:\d+:[a-z][a-z0-9-]*$/.test(key)
        && !key.split(':', 2)[1].split('/').includes('..')) return true;
    return key.startsWith('@href:') && key === normalizedHrefKey(element);
  };
  const controlKeys = (element, allowDetailsParent = true) => {
    const explicitKey = String(element.getAttribute(explicitControlAttribute) || '');
    if (isExplicitControlKey(element, explicitKey)) {
      return [explicitKey];
    }
    const parentKeys = allowDetailsParent && element.matches('summary')
      ? controlKeys(element.closest('details') || element, false)
      : [];
    const dataKeys = Array.from(element.attributes || [])
      .map(attribute => String(attribute.name || '').toLowerCase())
      .filter(name => /^data-[a-z0-9_-]+$/.test(name))
      .map(name => `@selector:[${name}]`);
    if (dataKeys.length) return Array.from(new Set([...dataKeys, ...parentKeys]));

    const id = String(element.id || '');
    if (/^[A-Za-z][A-Za-z0-9_.:-]*$/.test(id)) {
      const dynamic = id.match(/^([A-Za-z][A-Za-z0-9_.:-]{1,119}?[-_.:])(?:\d+|[0-9a-f]{8,}(?:-[0-9a-f-]+)?)$/i);
      return Array.from(new Set([dynamic ? `@id-prefix:${dynamic[1]}` : id, ...parentKeys]));
    }

    const classes = Array.from(element.classList || [])
      .map(name => String(name))
      .filter(name => /^[A-Za-z][A-Za-z0-9_-]*$/.test(name) && !nonIdentityClasses.has(name.toLowerCase()));
    const preferHref = classes.some(name => hrefPreferredClasses.has(name.toLowerCase()));
    const hrefKey = element.matches('a[href]') ? normalizedHrefKey(element) : null;
    if (classes.length && !preferHref) {
      return Array.from(new Set([...classes.map(name => `@selector:.${name}`), ...parentKeys]));
    }
    if (hrefKey) return Array.from(new Set([hrefKey, ...parentKeys]));
    if (classes.length) return Array.from(new Set([...classes.map(name => `@selector:.${name}`), ...parentKeys]));
    return parentKeys;
  };
  const record = event => {
    if (event.isTrusted !== true) return;
    const element = interactiveFromEvent(event);
    if (!element) return;
    const keys = controlKeys(element);
    element.removeAttribute(explicitControlAttribute);
    if (!keys.length) return;
    const binding = window[bindingName];
    const startBinding = window[startBindingName];
    if (typeof binding !== 'function' || typeof startBinding !== 'function') return;
    const width = Number(window.innerWidth || 0);
    const height = Number(window.innerHeight || 0);
    const semanticKey = event.type === 'keydown' && ['Enter', 'Escape', ' ', 'Tab'].includes(event.key)
      ? event.key
      : event.type === 'keydown' ? 'other' : null;
    const armed = window[armedAuthorizationKey];
    const armedValid = armed
      && Number(armed.expires_at_epoch_ms || 0) >= browserNow()
      && keys.includes(String(armed.control_key || ''))
      && /^control-action-role-\d{10,14}-\d+$/.test(String(armed.correlation_id || ''));
    if (armedValid) delete window[armedAuthorizationKey];
    const correlationId = armedValid
      ? String(armed.correlation_id)
      : `control-action-role-${Math.floor(browserNow())}-${++authorizationSequence}`;
    // Snapshot the trusted browser event synchronously, then bind a fresh real
    // /auth/me result to this exact event.  A role observed when the document
    // loaded is deliberately not reused: the session can expire while the
    // page remains open.
    const payload = {
      nonce,
      browser_event_epoch_seconds: browserNow() / 1000,
      event_type: String(event.type),
      keyboard_key: semanticKey,
      is_trusted: true,
      page_path: location.pathname || '/',
      control_keys: keys,
      target: {
        tag: String(element.tagName || '').toLowerCase(),
        id: String(element.id || ''),
        data_attributes: Array.from(element.attributes || [])
          .map(attribute => String(attribute.name || '').toLowerCase())
          .filter(name => /^data-[a-z0-9_-]+$/.test(name)),
        classes: Array.from(element.classList || []).filter(name => /^[a-z][a-z0-9_-]*$/i.test(name)),
      },
      viewport: {width, height},
      viewport_class: viewportClass(width),
      evidence_correlation_id: correlationId,
      authorization_mode: armedValid ? 'awaited-pre-action' : 'concurrent-action-probe',
    };
    void Promise.resolve(startBinding(payload)).catch(() => {});
    if (!armedValid) {
      void observeAuthorization(correlationId)
        .then(authorization => binding({...payload, authorization_observation: authorization}))
        .catch(() => {});
    }
  };

  for (const eventType of ['click', 'input', 'change', 'keydown', 'submit']) {
    document.addEventListener(eventType, record, true);
  }
})()
"""

_DOM_SECRET_SCAN_SCRIPT = r"""
secrets => {
  const controllerKey = '__sherpaUiSecretCapture';
  const marker = 'data-sherpa-ui-secret-mask';
  const styleId = 'sherpa-ui-secret-mask-style';
  const previous = window[controllerKey];
  if (previous && typeof previous.cleanup === 'function') {
    previous.cleanup();
  }

  const known = (Array.isArray(secrets) ? secrets : [])
    .map(value => String(value || ''))
    .filter(value => value.length >= 6);
  const patterns = [
    /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/gi,
    /\bsk-[A-Za-z0-9_-]{12,}/g,
    /\bAIza[A-Za-z0-9_-]{12,}/g,
    /\b(?:authorization|cookie|set-cookie|api[_ -]?key|password|passwd|secret|token|sid|session[_-]?(?:id|token))\s*[:=]\s*[^\s,;"'<>]{4,}/gi,
    /\bsession\s*[:=]\s*[A-Za-z0-9._~+/-]{12,}/gi,
  ];
  const sensitiveField = [
    'input[type="password"]',
    'input[id*="key" i]', 'input[name*="key" i]',
    'input[id*="token" i]', 'input[name*="token" i]',
    'input[id*="secret" i]', 'input[name*="secret" i]',
    'input[id*="password" i]', 'input[name*="password" i]',
    'input[id*="session" i]', 'input[name*="session" i]',
    'input[id*="cookie" i]', 'input[name*="cookie" i]',
    'input[id*="authorization" i]', 'input[name*="authorization" i]',
    'textarea[id*="key" i]', 'textarea[name*="key" i]',
    'textarea[id*="token" i]', 'textarea[name*="token" i]',
    'textarea[id*="secret" i]', 'textarea[name*="secret" i]',
    'textarea[id*="password" i]', 'textarea[name*="password" i]',
    'textarea[id*="session" i]', 'textarea[name*="session" i]',
    'textarea[id*="cookie" i]', 'textarea[name*="cookie" i]',
    'textarea[id*="authorization" i]', 'textarea[name*="authorization" i]',
  ].join(',');
  const opaquePixelSurface = [
    'iframe', 'img', 'svg image', 'canvas', 'video', 'embed', 'object',
    'input[type="image"]'
  ].join(',');

  const style = document.createElement('style');
  style.id = styleId;
  style.textContent = `[${marker}="1"] { visibility: hidden !important; }`;
  (document.head || document.documentElement).appendChild(style);

  const state = {
    known: new Set(),
    pattern: new Set(),
    sensitive: new Set(),
    text: new Set(),
    fields: new Set(),
    attributes: new Set(),
    opaque: new Set(),
    customHosts: new Set(),
    closedShadowHosts: new Set(),
    pseudo: new Set(),
    masked: new Set(),
  };
  // A document-level stylesheet does not cross an open ShadowRoot.  Keep an
  // inline, important opacity guard on every marked element as the actual
  // capture boundary, and restore the previous inline declaration afterwards.
  // This also prevents an inaccessible pixel surface from painting a secret
  // while Playwright resolves its mask locator.
  const originalOpacity = new Map();

  const isVisible = element => {
    if (!(element instanceof Element)) return false;
    const css = window.getComputedStyle(element);
    if (css.display === 'none' || css.visibility === 'hidden' || css.visibility === 'collapse') {
      return false;
    }
    if (Number(css.opacity) === 0) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const matchesKnown = value => known.some(secret => value.includes(secret));
  const matchesPattern = value => patterns.some(pattern => {
    pattern.lastIndex = 0;
    return pattern.test(value);
  });
  const mark = (element, kinds, surface) => {
    if (!(element instanceof Element)) return;
    if (kinds.known) state.known.add(element);
    if (kinds.pattern) state.pattern.add(element);
    state[surface].add(element);
    state.masked.add(element);
    if (!originalOpacity.has(element)) {
      originalOpacity.set(element, {
        value: element.style.getPropertyValue('opacity'),
        priority: element.style.getPropertyPriority('opacity'),
        hadStyleAttribute: element.hasAttribute('style'),
      });
    }
    element.style.setProperty('opacity', '0', 'important');
    if (element.getAttribute(marker) !== '1') element.setAttribute(marker, '1');
  };
  const classify = value => {
    const text = String(value || '');
    if (!text) return {known: false, pattern: false};
    return {known: matchesKnown(text), pattern: matchesPattern(text)};
  };
  const deepest = candidates => candidates.filter(element => !candidates.some(
    other => other !== element && element.contains(other)
  ));

  const collectElements = root => {
    const elements = [];
    const visit = container => {
      for (const element of Array.from(container.querySelectorAll('*'))) {
        elements.push(element);
        if (element.shadowRoot) visit(element.shadowRoot);
      }
    };
    if (root.body) elements.push(root.body);
    visit(root);
    return elements;
  };

  const scan = () => {
    const elements = collectElements(document);
    const knownText = [];
    const patternText = [];
    for (const element of elements) {
      if (isVisible(element)) {
        const result = classify(element.innerText || element.textContent || '');
        if (result.known) knownText.push(element);
        if (result.pattern) patternText.push(element);
      }

      if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
        const result = classify(element.value);
        if (result.known || result.pattern) mark(element, result, 'fields');
      }

      for (const attribute of Array.from(element.attributes || [])) {
        if (attribute.name === marker) continue;
        const result = classify(`${attribute.name}=${attribute.value}`);
        if (result.known || result.pattern) mark(element, result, 'attributes');
      }

      if (isVisible(element)) {
        const css = window.getComputedStyle(element);
        if (element.matches(opaquePixelSurface) || css.backgroundImage !== 'none') {
          mark(element, {known: false, pattern: false}, 'opaque');
        }
        // A closed ShadowRoot is intentionally invisible to page JavaScript.  Mask
        // unknown custom-element hosts as opaque pixels rather than attesting that
        // their inaccessible rendered content was scanned.  sherpa-topbar is a
        // repository-owned light-DOM component and is the sole audited exception.
        if (element.tagName.includes('-') && element.tagName !== 'SHERPA-TOPBAR') {
          mark(element, {known: false, pattern: false}, 'customHosts');
        }
        if (element.getAttribute('data-sherpa-ui-closed-shadow-host') === '1') {
          mark(element, {known: false, pattern: false}, 'closedShadowHosts');
        }
        for (const pseudo of ['::before', '::after']) {
          const pseudoStyle = window.getComputedStyle(element, pseudo);
          const content = pseudoStyle.content || '';
          const result = classify(content);
          if (result.known || result.pattern) mark(element, result, 'pseudo');
          if (pseudoStyle.backgroundImage !== 'none' || /^url\(/i.test(content)) {
            mark(element, {known: false, pattern: false}, 'opaque');
          }
        }
      }
    }
    for (const element of deepest(knownText)) {
      const text = element.innerText || element.textContent || '';
      mark(element, {known: true, pattern: matchesPattern(text)}, 'text');
    }
    for (const element of deepest(patternText)) {
      const text = element.innerText || element.textContent || '';
      mark(element, {known: matchesKnown(text), pattern: true}, 'text');
    }
    for (const element of elements.filter(item => item.matches && item.matches(sensitiveField))) {
      mark(element, {known: false, pattern: false}, 'sensitive');
    }
    for (const group of Object.values(state)) {
      for (const element of group) {
        if (!element.isConnected) group.delete(element);
      }
    }
    const markedCount = elements.filter(element => element.getAttribute(marker) === '1').length;
    let stabilityHash = 2166136261;
    for (const element of elements.filter(item => item.getAttribute(marker) === '1')) {
      const raw = [
        element.tagName,
        element.id,
        element.innerText || element.textContent || '',
        (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) ? element.value : '',
        Array.from(element.attributes || []).map(attribute => `${attribute.name}=${attribute.value}`).join('|'),
      ].join('\u001f');
      for (let index = 0; index < raw.length; index += 1) {
        stabilityHash ^= raw.charCodeAt(index);
        stabilityHash = Math.imul(stabilityHash, 16777619) >>> 0;
      }
    }
    return {
      scan_completed: true,
      known_match_count: state.known.size,
      pattern_match_count: state.pattern.size,
      sensitive_field_count: state.sensitive.size,
      visible_text_match_count: state.text.size,
      input_value_match_count: state.fields.size,
      attribute_match_count: state.attributes.size,
      detected_element_count: new Set([
        ...state.known, ...state.pattern, ...state.sensitive, ...state.opaque,
        ...state.pseudo, ...state.customHosts, ...state.closedShadowHosts,
      ]).size,
      masked_element_count: state.masked.size,
      open_shadow_masked_element_count: Array.from(state.masked).filter(
        element => element.getRootNode() instanceof ShadowRoot
      ).length,
      unmasked_visible_element_count: Array.from(state.masked).filter(isVisible).length,
      inline_mask_failure_count: Array.from(state.masked).filter(element => (
        element.style.getPropertyValue('opacity') !== '0'
        || element.style.getPropertyPriority('opacity') !== 'important'
      )).length,
      dom_marked_element_count: markedCount,
      opaque_pixel_surface_count: state.opaque.size,
      pseudo_content_match_count: state.pseudo.size,
      shadow_root_count: elements.filter(element => Boolean(element.shadowRoot)).length,
      uninspectable_custom_element_count: state.customHosts.size,
      closed_shadow_host_count: state.closedShadowHosts.size,
      stability_fingerprint: stabilityHash.toString(16).padStart(8, '0'),
    };
  };

  let observer = null;
  const cleanup = () => {
    if (observer) observer.disconnect();
    collectElements(document).forEach(element => element.removeAttribute(marker));
    for (const [element, original] of originalOpacity.entries()) {
      if (original.value) {
        element.style.setProperty('opacity', original.value, original.priority);
      } else {
        element.style.removeProperty('opacity');
      }
      if (!original.hadStyleAttribute && !(element.getAttribute('style') || '').trim()) {
        element.removeAttribute('style');
      }
    }
    originalOpacity.clear();
    document.getElementById(styleId)?.remove();
    delete window[controllerKey];
  };
  const initial = scan();
  const observedRoots = new WeakSet();
  const observerOptions = {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
  };
  const observeRoots = () => {
    const roots = [document.documentElement];
    collectElements(document).forEach(element => {
      if (element.shadowRoot) roots.push(element.shadowRoot);
    });
    roots.forEach(root => {
      if (!observedRoots.has(root)) {
        observer.observe(root, observerOptions);
        observedRoots.add(root);
      }
    });
  };
  observer = new MutationObserver(mutations => {
    const onlyOwnMarkers = mutations.length > 0 && mutations.every(
      mutation => mutation.type === 'attributes' && mutation.attributeName === marker
    );
    if (!onlyOwnMarkers) {
      scan();
      observeRoots();
    }
  });
  observeRoots();
  window[controllerKey] = {scan, cleanup};
  return initial;
}
"""

_DOM_SECRET_RESCAN_SCRIPT = r"""
() => {
  const controller = window.__sherpaUiSecretCapture;
  if (!controller || typeof controller.scan !== 'function') {
    throw new Error('DOM secret capture controller is unavailable');
  }
  return controller.scan();
}
"""

_DOM_SECRET_CLEANUP_SCRIPT = r"""
() => {
  const controller = window.__sherpaUiSecretCapture;
  if (!controller || typeof controller.cleanup !== 'function') {
    throw new Error('DOM secret capture controller is unavailable during cleanup');
  }
  controller.cleanup();
  return true;
}
"""


def redact(value):
    if isinstance(value, dict):
        if _is_safe_authorization_observation(value):
            return {str(key): redact(item) for key, item in value.items()}
        sibling_name = str(value.get("name") or "")
        sibling_secret = bool(_SECRET_KEY.search(sibling_name))
        return {
            str(k): (
                "<redacted>"
                if (
                    _SECRET_KEY.search(str(k))
                    and not _is_safe_authorization_observation(v)
                    and not _is_safe_secret_metadata_scalar(str(k), v)
                )
                or (sibling_secret and str(k).lower() == "value" and not isinstance(v, bool))
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        out = _JSON_SECRET_VALUE.sub(r'\1"<redacted>"', value)
        out = _URL_USERINFO.sub(r"\1<redacted>@", out)
        for pattern in _SECRET_PATTERNS:
            out = pattern.sub("<redacted>", out)
        return out
    return value


def safe_url(url: str) -> str:
    # userinfoはkey名にかかわらず常に破棄し、query値も保存しない。
    return _runner_safe_url(url)


class CaseEvidence:
    def __init__(self, case_dir: Path, nodeid: str) -> None:
        self.case_dir = case_dir
        self.nodeid = nodeid
        self.started = time.time()
        self.console: list[dict] = []
        self.page_errors: list[dict] = []
        self.request_failures: list[dict] = []
        self.http: list[dict] = []
        self.navigation_events: list[dict] = []
        self.control_actions: list[dict] = []
        self._control_action_starts: dict[str, dict] = {}
        self._pending_control_actions: dict[str, dict] = {}
        self._control_role_authorizations: dict[str, dict] = {}
        self._completed_control_correlations: set[str] = set()
        self._pending_screenshot_action_ids: set[str] = set()
        self._armed_explicit_control_keys: set[str] = set()
        self._browser_request_ids: dict[str, str] = {}
        self._browser_request_sequence = 0
        self._browser_network_event_sequence = 0
        self._attached_page_sequence = 0
        self._attached_pages: list = []
        self._attached_page_ids: dict[int, str] = {}
        self._control_action_sequence = 0
        self._pre_action_role_sequence = 0
        self._screenshot_role_sequence = 0
        self._context = None
        self._trace_active = False
        self._saved_trace_segments = 0
        self._secret_values: list[str] = []
        self._secret_registry = self._validate_secret_registry()
        self._console_http_error_allowances: list[dict] = []
        self._console_http_error_allowance_sequence = 0
        self._allowed_request_failures: list[dict] = []
        self._request_failure_allowance_sequence = 0
        self._auth_bootstrap_active = False
        self._cleanups: list[tuple[str, Callable[[], None]]] = []
        self.cleanup_errors: list[str] = []
        self.screenshot_mask_events: list[dict] = []
        self.screenshot_attestations: list[dict] = []
        self.trace_attestations: list[dict] = []
        self.provider_correlations: list[dict] = []
        self.provider_usage_unreported: list[dict] = []
        self.sse_collections: list[dict] = []
        self.sse_events: list[dict] = []
        self.sse_timings: list[dict] = []
        self.database_correlations: list[dict] = []
        self.case_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        chmod_path_no_follow(self.case_dir, 0o700, require_owner_uid=os.geteuid())
        for rel in ("screenshots", "browser", "network", "services", "state", "security"):
            directory = self.case_dir / rel
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            chmod_path_no_follow(directory, 0o700, require_owner_uid=os.geteuid())
        for key, value in os.environ.items():
            if key != "SHERPA_UI_SECRET_REGISTRY" and _SECRET_KEY.search(key):
                self.register_secret(value)

    def _validate_secret_registry(self) -> Path:
        configured = os.environ.get("SHERPA_UI_SECRET_REGISTRY", "").strip()
        assert configured, "SHERPA_UI_SECRET_REGISTRY is required so runtime secrets can be removed from pytest and JUnit evidence"
        raw = Path(configured)
        assert raw.is_absolute(), "SHERPA_UI_SECRET_REGISTRY must be an absolute path"
        assert not raw.is_symlink(), "SHERPA_UI_SECRET_REGISTRY must not be a symlink"
        parent = raw.parent.resolve(strict=True)
        assert parent.is_dir(), "SHERPA_UI_SECRET_REGISTRY parent must be a directory"
        parent_stat = parent.stat()
        assert parent_stat.st_uid == os.geteuid(), "secret registry parent is not runner-owned"
        assert stat.S_IMODE(parent_stat.st_mode) & 0o077 == 0, "secret registry parent must not be accessible by group or other users"
        resolved = parent / raw.name
        artifact_root = self.case_dir.parents[2].resolve()
        assert not resolved.is_relative_to(artifact_root), "secret registry must be outside the artifact directory"
        if resolved.exists():
            registry_stat = resolved.lstat()
            assert stat.S_ISREG(registry_stat.st_mode), "secret registry is not a regular file"
            assert registry_stat.st_uid == os.geteuid(), "secret registry is not runner-owned"
            assert stat.S_IMODE(registry_stat.st_mode) == 0o600, "secret registry permissions must be exactly 0600"
            assert registry_stat.st_nlink == 1, "secret registry must not be hardlinked"
        return resolved

    def attach_page(self, page) -> None:
        self._attached_page_sequence += 1
        attached_page_id = f"browser-page-{self._attached_page_sequence:03d}"
        self._attached_pages.append(page)
        self._attached_page_ids[id(page)] = attached_page_id
        control_nonce = secrets.token_urlsafe(24)
        self.register_secret(control_nonce)
        capture_script = _CONTROL_ACTION_CAPTURE_SCRIPT_TEMPLATE.replace(
            "__SHERPA_CONTROL_NONCE__",
            json.dumps(control_nonce),
        )
        page.expose_binding(
            "__sherpaUiRecordTrustedControlStart",
            lambda source, payload: self._record_trusted_control_start(
                source,
                payload,
                nonce=control_nonce,
                page_id=attached_page_id,
            ),
        )
        page.expose_binding(
            "__sherpaUiRecordTrustedControlAction",
            lambda source, payload: self._record_trusted_control_action(
                source,
                payload,
                nonce=control_nonce,
                page_id=attached_page_id,
            ),
        )
        page.add_init_script(_CLOSED_SHADOW_TRACKING_SCRIPT)
        page.add_init_script(capture_script)
        try:
            page.evaluate(_CLOSED_SHADOW_TRACKING_SCRIPT)
            page.evaluate(capture_script)
        except Exception:
            # A navigation may replace the initial document concurrently; the
            # init script still installs before the destination's own scripts.
            pass
        page.on(
            "console",
            lambda msg: self._record_browser_console(msg, page=page, page_id=attached_page_id),
        )
        page.on(
            "pageerror",
            lambda exc: self.page_errors.append(
                {
                    "ts": time.time(),
                    "error": self.redact(str(exc)),
                    "url": self.redact(safe_url(page.url)),
                }
            ),
        )
        page.on(
            "requestfailed",
            lambda request: self._record_browser_request_failed(request, page_id=attached_page_id),
        )
        page.on("request", lambda request: self._record_browser_request(request, page_id=attached_page_id))
        page.on("response", lambda response: self._record_browser_response(response, page_id=attached_page_id))
        page.on(
            "framenavigated",
            lambda frame: self._record_frame_navigated(frame, page_id=attached_page_id),
        )

    @staticmethod
    def _browser_request_identity(request) -> str:
        implementation = getattr(request, "_impl_obj", None)
        guid = getattr(implementation, "_guid", None)
        if guid:
            return f"guid:{guid}"
        return f"object:{id(implementation if implementation is not None else request)}"

    def _browser_request_id(self, request) -> str:
        identity = self._browser_request_identity(request)
        request_id = self._browser_request_ids.get(identity)
        if request_id is None:
            self._browser_request_sequence += 1
            request_id = f"browser-{self._browser_request_sequence:06d}"
            self._browser_request_ids[identity] = request_id
        return request_id

    def _next_browser_network_event_sequence(self) -> int:
        self._browser_network_event_sequence += 1
        return self._browser_network_event_sequence

    @staticmethod
    def _browser_frame_metadata(request) -> tuple[str, str, bool]:
        try:
            frame = request.frame
        except Exception:
            return "", "", False
        if frame is None:
            return "", "", False
        implementation = getattr(frame, "_impl_obj", None)
        guid = getattr(implementation, "_guid", None)
        frame_id = f"guid:{guid}" if guid else f"object:{id(implementation if implementation is not None else frame)}"
        try:
            frame_url = safe_url(str(frame.url or ""))
        except Exception:
            frame_url = ""
        try:
            is_main_frame = frame.parent_frame is None
        except Exception:
            is_main_frame = False
        return frame_id, frame_url, is_main_frame

    def _record_browser_request(self, request, *, page_id: str) -> None:
        event_sequence = self._next_browser_network_event_sequence()
        event_ts = time.time()
        evidence_probe = self._role_probe_marker(request)
        frame_id, frame_url, is_main_frame = self._browser_frame_metadata(request)
        try:
            is_navigation_request = bool(request.is_navigation_request())
        except Exception:
            is_navigation_request = False
        self.http.append(
            {
                "ts": event_ts,
                "event_sequence": event_sequence,
                "page_id": page_id,
                "phase": "request",
                "request_id": self._browser_request_id(request),
                "method": request.method,
                "url": self.redact(safe_url(request.url)),
                "resource_type": request.resource_type,
                "frame_id": frame_id,
                "frame_url": self.redact(frame_url),
                "is_main_frame": is_main_frame,
                "is_navigation_request": is_navigation_request,
                "evidence_probe": evidence_probe,
                "evidence_correlation_id": self._role_probe_correlation(request) if evidence_probe else None,
            }
        )

    def _record_browser_console(self, message, *, page, page_id: str) -> None:
        event_sequence = self._next_browser_network_event_sequence()
        event_ts = time.time()
        message_type = message.type
        text = self.redact(message.text)
        try:
            location = message.location or {}
            location_url = self.redact(safe_url(str(location.get("url") or "")))
            location_line = location.get("lineNumber", location.get("line"))
            location_column = location.get("columnNumber", location.get("column"))
        except Exception:
            location_url = ""
            location_line = None
            location_column = None
        self.console.append(
            {
                "ts": event_ts,
                "event_sequence": event_sequence,
                "page_id": page_id,
                "type": message_type,
                "text": text,
                "url": self.redact(safe_url(page.url)),
                "location_url": location_url,
                "location_line": location_line if isinstance(location_line, int) and not isinstance(location_line, bool) else None,
                "location_column": (
                    location_column if isinstance(location_column, int) and not isinstance(location_column, bool) else None
                ),
                "auth_bootstrap_scope": self._auth_bootstrap_active,
                "expected": False,
                "expected_source": None,
                "expected_reason": None,
                "expectation_case": None,
            }
        )

    def _record_browser_response(self, response, *, page_id: str) -> None:
        event_sequence = self._next_browser_network_event_sequence()
        response_ts = time.time()
        request = response.request
        evidence_probe = self._role_probe_marker(request)
        frame_id, frame_url, is_main_frame = self._browser_frame_metadata(request)
        try:
            is_navigation_request = bool(request.is_navigation_request())
        except Exception:
            is_navigation_request = False
        self.http.append(
            {
                "ts": response_ts,
                "event_sequence": event_sequence,
                "page_id": page_id,
                "phase": "response",
                "request_id": self._browser_request_id(request),
                "method": request.method,
                "url": self.redact(safe_url(response.url)),
                "resource_type": request.resource_type,
                "frame_id": frame_id,
                "frame_url": self.redact(frame_url),
                "is_main_frame": is_main_frame,
                "is_navigation_request": is_navigation_request,
                "status": response.status,
                "status_text": self.redact(str(response.status_text or "")),
                "evidence_probe": evidence_probe,
                "evidence_correlation_id": self._role_probe_correlation(request) if evidence_probe else None,
            }
        )
        if evidence_probe == "control-action-role-v1":
            self._complete_pending_control_action_from_response(
                response,
                page_id=page_id,
                observed_at=response_ts,
            )

    def _record_frame_navigated(self, frame, *, page_id: str) -> None:
        event_sequence = self._next_browser_network_event_sequence()
        event_ts = time.time()
        implementation = getattr(frame, "_impl_obj", None)
        guid = getattr(implementation, "_guid", None)
        frame_id = f"guid:{guid}" if guid else f"object:{id(implementation if implementation is not None else frame)}"
        try:
            is_main_frame = frame.parent_frame is None
        except Exception:
            is_main_frame = False
        if not is_main_frame:
            return
        try:
            url = self.redact(safe_url(str(frame.url or "")))
        except Exception:
            url = ""
        self.navigation_events.append(
            {
                "ts": event_ts,
                "event_sequence": event_sequence,
                "event": "main-frame-committed",
                "page_id": page_id,
                "frame_id": frame_id,
                "url": url,
            }
        )

    @staticmethod
    def _role_probe_marker(request) -> str | None:
        try:
            if request.method != "GET" or urlsplit(request.url).path != "/auth/me":
                return None
            marker = request.header_value("x-sherpa-ui-evidence-probe")
            return marker if marker in {"control-action-role-v1", "screenshot-role-v1"} else None
        except Exception:
            return None

    @staticmethod
    def _role_probe_correlation(request) -> str | None:
        try:
            value = request.header_value("x-sherpa-ui-evidence-correlation")
        except Exception:
            return None
        return value if value and _ROLE_EVIDENCE_CORRELATION.fullmatch(value) else None

    @classmethod
    def _is_control_role_probe(cls, request) -> bool:
        return cls._role_probe_marker(request) == "control-action-role-v1"

    def _record_browser_request_failed(self, request, *, page_id: str) -> None:
        event_sequence = self._next_browser_network_event_sequence()
        event_ts = time.time()
        request_id = self._browser_request_id(request)
        failure = self.redact(request.failure or "request failed")
        frame_id, frame_url, is_main_frame = self._browser_frame_metadata(request)
        try:
            is_navigation_request = bool(request.is_navigation_request())
        except Exception:
            is_navigation_request = False
        row = {
            "ts": event_ts,
            "event_sequence": event_sequence,
            "page_id": page_id,
            "request_id": request_id,
            "method": request.method,
            "url": self.redact(safe_url(request.url)),
            "resource_type": request.resource_type,
            "frame_id": frame_id,
            "frame_url": self.redact(frame_url),
            "is_main_frame": is_main_frame,
            "is_navigation_request": is_navigation_request,
            "failure": failure,
            "expected": False,
            "expected_reason": None,
            "expected_source": None,
            "evidence_probe": self._role_probe_marker(request),
            "evidence_correlation_id": self._role_probe_correlation(request),
        }
        self.request_failures.append(row)
        self.http.append({**row, "phase": "requestfailed"})

    def _matching_explicit_failure_allowance(self, failure: dict) -> dict | None:
        request_rows = [row for row in self.http if row.get("phase") == "request" and row.get("request_id") == failure.get("request_id")]
        if len(request_rows) != 1:
            return None
        return _exact_failure_allowance_match(
            failure,
            request_rows[0],
            self._allowed_request_failures,
        )

    def finalize_request_failure_expectations(self) -> None:
        """Classify request failures only from complete, correlated evidence."""

        for allowance in self._allowed_request_failures:
            allowance["matched_request_ids"] = []
        for failure in self.request_failures:
            failure["expected"] = False
            failure["expected_reason"] = None
            failure["expected_source"] = None
            failure.pop("expectation_case", None)
            failure.pop("navigation_correlation", None)
            failure.pop("allowance_id", None)
            failure.pop("allowance_correlation", None)

            correlation = _navigation_cancel_correlation(
                failure,
                self.http,
                self.navigation_events,
            )
            if correlation is not None:
                target_path = urlsplit(correlation["url"]).path
                failure["expected"] = True
                failure["expected_source"] = "correlated-document-navigation"
                failure["expectation_case"] = self.nodeid
                failure["expected_reason"] = (
                    f"same-page main-frame navigation to {target_path} committed after "
                    f"HTTP {correlation['status']} and cancelled this non-document request"
                )
                failure["navigation_correlation"] = correlation
            else:
                allowance = self._matching_explicit_failure_allowance(failure)
                if allowance is not None:
                    request_id = str(failure["request_id"])
                    allowance["matched_request_ids"].append(request_id)
                    failure["expected"] = True
                    failure["expected_source"] = "explicit-case-allowance"
                    failure["expectation_case"] = self.nodeid
                    failure["expected_reason"] = allowance["reason"]
                    failure["allowance_id"] = allowance["allowance_id"]
                    failure["allowance_correlation"] = {
                        "case": self.nodeid,
                        "method": allowance["method"],
                        "path": allowance["path"],
                        "resource_type": allowance["resource_type"],
                        "failure": allowance["failure"],
                        "registered_after_event_sequence": allowance["registered_after_event_sequence"],
                    }

            for http_failure in self.http:
                if http_failure.get("phase") != "requestfailed" or http_failure.get("request_id") != failure.get("request_id"):
                    continue
                for key in (
                    "expected",
                    "expected_reason",
                    "expected_source",
                    "expectation_case",
                    "navigation_correlation",
                    "allowance_id",
                    "allowance_correlation",
                ):
                    if key in failure:
                        http_failure[key] = failure[key]
                    else:
                        http_failure.pop(key, None)

    def _request_failure_allowance_evidence(self) -> list[dict]:
        return [
            {
                "allowance_id": allowance["allowance_id"],
                "case": self.nodeid,
                "method": allowance["method"],
                "path": allowance["path"],
                "resource_type": allowance["resource_type"],
                "failure": allowance["failure"],
                "reason": allowance["reason"],
                "registered_after_event_sequence": allowance["registered_after_event_sequence"],
                "required_matches": 1,
                "maximum_matches": 1,
                "matched_count": len(allowance["matched_request_ids"]),
                "matched_request_ids": list(allowance["matched_request_ids"]),
            }
            for allowance in self._allowed_request_failures
        ]

    def finalize_console_error_expectations(self) -> None:
        """Classify console errors only from one-to-one network evidence."""

        self.finalize_request_failure_expectations()
        self._correlate_401_console_errors()
        for allowance in self._console_http_error_allowances:
            allowance["matched_request_ids"] = []
        for console in self.console:
            if console.get("expected_source") in {
                "correlated-explicit-request-failure",
                "explicit-http-error-response",
            }:
                console["expected"] = False
                console["expected_source"] = None
                console["expected_reason"] = None
                console["expectation_case"] = None
                console.pop("request_failure_correlation", None)
                console.pop("http_error_correlation", None)
                console.pop("allowance_id", None)

        consumed_failure_request_ids: set[str] = set()
        for console in self.console:
            if console.get("type") != "error" or console.get("expected") is True:
                continue
            console_sequence = console.get("event_sequence")
            console_ts = console.get("ts")
            failure = _unique_request_failure_console_candidate(
                console,
                self.request_failures,
                consumed_failure_request_ids,
            )
            if failure is None or failure.get("expected") is not True or failure.get("expected_source") != "explicit-case-allowance":
                continue
            request_id = str(failure["request_id"])
            consumed_failure_request_ids.add(request_id)
            console["expected"] = True
            console["expected_source"] = "correlated-explicit-request-failure"
            console["expected_reason"] = (
                f"exact Chromium diagnostic correlated one-to-one with the case's allowed {failure['resource_type']} request failure"
            )
            console["expectation_case"] = self.nodeid
            console["request_failure_correlation"] = {
                "request_id": request_id,
                "allowance_id": failure.get("allowance_id"),
                "page_id": failure.get("page_id"),
                "location_url": console.get("location_url"),
                "method": failure.get("method"),
                "path": urlsplit(str(failure.get("url") or "")).path,
                "resource_type": failure.get("resource_type"),
                "failure": failure.get("failure"),
                "request_failure_event_sequence": failure.get("event_sequence"),
                "console_event_sequence": console_sequence,
                "elapsed_ms": max(0, round((float(console_ts) - float(failure["ts"])) * 1000)),
            }

        consumed_response_request_ids: set[str] = set()
        for console in self.console:
            if console.get("type") != "error" or console.get("expected") is True:
                continue
            matches: list[tuple[dict, dict]] = []
            for allowance in self._console_http_error_allowances:
                if len(allowance["matched_request_ids"]) >= allowance["expected_count"]:
                    continue
                correlation = _http_error_console_correlation(
                    console,
                    self.http,
                    allowance,
                    consumed_response_request_ids,
                )
                if correlation is not None:
                    matches.append((allowance, correlation))
            if len(matches) != 1:
                continue
            allowance, correlation = matches[0]
            request_id = correlation["request_id"]
            consumed_response_request_ids.add(request_id)
            allowance["matched_request_ids"].append(request_id)
            console["expected"] = True
            console["expected_source"] = "explicit-http-error-response"
            console["expected_reason"] = allowance["reason"]
            console["expectation_case"] = self.nodeid
            console["allowance_id"] = allowance["allowance_id"]
            console["http_error_correlation"] = correlation

    def _console_http_error_allowance_evidence(self) -> list[dict]:
        return [
            {
                "allowance_id": allowance["allowance_id"],
                "case": self.nodeid,
                "method": allowance["method"],
                "path": allowance["path"],
                "status": allowance["status"],
                "reason": allowance["reason"],
                "registered_after_event_sequence": allowance["registered_after_event_sequence"],
                "expected_count": allowance["expected_count"],
                "matched_count": len(allowance["matched_request_ids"]),
                "matched_request_ids": list(allowance["matched_request_ids"]),
            }
            for allowance in self._console_http_error_allowances
        ]

    @staticmethod
    def _trusted_control_keys(raw_keys) -> list[str]:
        if not isinstance(raw_keys, list):
            return []
        trusted: set[str] = set()
        for key in raw_keys:
            if not isinstance(key, str) or len(key) > 160:
                continue
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]*", key):
                trusted.add(key)
                continue
            if re.fullmatch(r"@selector:\[data-[a-z0-9_-]+\]", key):
                trusted.add(key)
                continue
            class_match = re.fullmatch(r"@selector:\.([A-Za-z][A-Za-z0-9_-]*)", key)
            if class_match and class_match.group(1).lower() not in _NON_IDENTITY_CONTROL_CLASSES:
                trusted.add(key)
                continue
            prefix_match = re.fullmatch(r"@id-prefix:([A-Za-z][A-Za-z0-9_.:-]{1,119}[-_.:])", key)
            if prefix_match:
                trusted.add(key)
                continue
            if key.startswith("@href:") and _is_safe_control_href(key.removeprefix("@href:")):
                trusted.add(key)
                continue
            unkeyed_match = re.fullmatch(r"@unkeyed:(web/[A-Za-z0-9_./-]+):(\d+):([a-z][a-z0-9-]*)", key)
            if unkeyed_match and ".." not in Path(unkeyed_match.group(1)).parts:
                trusted.add(key)
        return sorted(trusted)

    def _record_trusted_control_start(self, source, payload, *, nonce: str, page_id: str) -> None:
        """Remember the exact trusted event before its asynchronous role probe."""

        if not isinstance(payload, dict) or payload.get("nonce") != nonce or payload.get("is_trusted") is not True:
            return
        control_keys = self._trusted_control_keys(payload.get("control_keys"))
        page_path = str(payload.get("page_path") or "")
        correlation_id = str(payload.get("evidence_correlation_id") or "")
        try:
            occurred_at = float(payload.get("browser_event_epoch_seconds"))
        except (TypeError, ValueError):
            return
        recorded_at = time.time()
        if (
            not control_keys
            or not page_path.startswith("/")
            or "?" in page_path
            or "#" in page_path
            or re.fullmatch(r"control-action-role-\d{10,14}-\d+", correlation_id) is None
            or not recorded_at - 60 <= occurred_at <= recorded_at + 60
        ):
            return
        start = {
            "occurred_at_epoch_seconds": occurred_at,
            "page_path": page_path,
            "page_id": page_id,
        }
        for control_key in control_keys:
            self._control_action_starts[control_key] = start
            self._armed_explicit_control_keys.discard(control_key)
        pending_payload = dict(payload)
        pending_payload["control_keys"] = control_keys
        pending_payload["source_frame_url"] = (
            self.redact(safe_url(str(getattr(source.get("frame"), "url", "")))) if isinstance(source, dict) else ""
        )
        self._pending_control_actions[correlation_id] = {
            "payload": pending_payload,
            "page_id": page_id,
        }
        self._finalize_pending_control_action(correlation_id)

    def _complete_pending_control_action_from_response(self, response, *, page_id: str, observed_at: float) -> None:
        request = response.request
        correlation_id = self._role_probe_correlation(request)
        if not correlation_id or correlation_id in self._completed_control_correlations:
            return
        status = int(response.status)
        role = "anonymous" if status in {401, 403} else "unknown"
        auth_disabled = False
        if status not in {401, 403}:
            try:
                body = response.json()
            except Exception:
                body = {}
            if isinstance(body, dict):
                candidate = str(body.get("role") or "")
                role = candidate if candidate in {"admin", "user"} else "unknown"
                auth_disabled = body.get("auth_disabled") is True
        authorization = {
            "status": status,
            "role": role,
            "auth_disabled": auth_disabled,
            "observed_at_epoch_seconds": max(float(observed_at), time.time()),
            "evidence_correlation_id": correlation_id,
        }
        if not _is_valid_control_authorization_observation(authorization):
            return
        self._control_role_authorizations[correlation_id] = {
            "page_id": page_id,
            "authorization": authorization,
        }
        self._finalize_pending_control_action(correlation_id)

    def _finalize_pending_control_action(self, correlation_id: str) -> None:
        pending = self._pending_control_actions.get(correlation_id)
        authorization_record = self._control_role_authorizations.get(correlation_id)
        if pending is None or authorization_record is None:
            return
        if pending.get("page_id") != authorization_record.get("page_id"):
            return
        payload = dict(pending["payload"])
        payload["authorization_observation"] = authorization_record["authorization"]
        self._record_trusted_control_action(
            None,
            payload,
            nonce=str(payload.get("nonce") or ""),
            page_id=str(pending["page_id"]),
        )

    def _record_trusted_control_action(self, source, payload, *, nonce: str, page_id: str) -> None:
        """Persist only genuine browser input and only non-secret control identity."""

        if not isinstance(payload, dict) or payload.get("nonce") != nonce or payload.get("is_trusted") is not True:
            return
        control_keys = self._trusted_control_keys(payload.get("control_keys"))
        page_path = str(payload.get("page_path") or "")
        viewport = payload.get("viewport")
        authorization = payload.get("authorization_observation")
        if not control_keys or not page_path.startswith("/") or "?" in page_path or "#" in page_path:
            return
        if not isinstance(viewport, dict) or not all(isinstance(viewport.get(key), (int, float)) for key in ("width", "height")):
            return
        width, height = int(viewport["width"]), int(viewport["height"])
        if width <= 0 or height <= 0 or not _is_valid_control_authorization_observation(authorization):
            return
        event_type = str(payload.get("event_type") or "")
        if event_type not in {"click", "input", "change", "keydown", "submit"}:
            return
        target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
        target_id = str(target.get("id") or "")
        data_attributes = sorted(
            {
                str(item).lower()
                for item in target.get("data_attributes") or ()
                if isinstance(item, str) and re.fullmatch(r"data-[a-z0-9_-]+", item.lower())
            }
        )
        classes = sorted(
            {str(item) for item in target.get("classes") or () if isinstance(item, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", item)}
        )
        recorded_at = time.time()
        try:
            occurred_at = float(payload.get("browser_event_epoch_seconds"))
            authorization_observed_at = float(authorization.get("observed_at_epoch_seconds"))
        except (TypeError, ValueError):
            return
        if not recorded_at - 60 <= occurred_at <= recorded_at + 60:
            return
        correlation_id = str(authorization.get("evidence_correlation_id") or "")
        if not re.fullmatch(r"control-action-role-\d{10,14}-\d+", correlation_id):
            return
        if correlation_id in self._completed_control_correlations:
            return
        cached = self._control_role_authorizations.get(correlation_id)
        cached_authorization = cached.get("authorization") if isinstance(cached, dict) else None
        if (
            not isinstance(cached, dict)
            or cached.get("page_id") != page_id
            or not _is_valid_control_authorization_observation(cached_authorization)
            or any(
                cached_authorization.get(key) != authorization.get(key)
                for key in ("status", "role", "auth_disabled", "evidence_correlation_id")
            )
            or abs(float(cached_authorization["observed_at_epoch_seconds"]) - float(authorization["observed_at_epoch_seconds"])) > 5
        ):
            return
        authorization_mode = str(payload.get("authorization_mode") or "")
        valid_authorization_time = (
            0 <= occurred_at - authorization_observed_at <= 5
            if authorization_mode == "awaited-pre-action"
            else occurred_at <= authorization_observed_at <= recorded_at + 1
            if authorization_mode == "concurrent-action-probe"
            else False
        )
        if not valid_authorization_time:
            return
        safe_target_id = target_id if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]*", target_id) else ""
        if any(key.startswith("@id-prefix:") for key in control_keys):
            # Dynamic suffixes may be record/session identifiers.  The stable
            # prefix proves the control family without persisting that suffix.
            safe_target_id = ""
        self._control_action_sequence += 1
        self.control_actions.append(
            {
                "schema_version": 1,
                "action_id": f"control-action-{self._control_action_sequence:06d}",
                "nodeid": self.nodeid,
                "recorded_at_epoch_seconds": recorded_at,
                "occurred_at_epoch_seconds": occurred_at,
                "browser_event_epoch_seconds": occurred_at,
                "authorization_observed_at_epoch_seconds": authorization_observed_at,
                "authorization_evidence_correlation_id": correlation_id,
                "authorization_mode": authorization_mode,
                "page_id": page_id,
                "event_type": event_type,
                "keyboard_key": payload.get("keyboard_key")
                if payload.get("keyboard_key") in {None, "Enter", "Escape", " ", "Tab", "other"}
                else "other",
                "is_trusted": True,
                "capture_source": "playwright-exposed-binding-with-page-nonce",
                "page_path": page_path,
                "control_keys": control_keys,
                "target": {
                    "tag": str(target.get("tag") or "")[:32].lower(),
                    "id": safe_target_id,
                    "data_attributes": data_attributes,
                    "classes": classes,
                },
                "viewport": {"width": width, "height": height},
                "viewport_class": "narrow" if width <= 500 else "desktop" if width >= 1024 else "intermediate",
                "authorization_observation": authorization,
                "state_contract": {
                    "required": ["normal", "abnormal"],
                    "inference_allowed": False,
                    "observed": [],
                },
                "state_attestations": [],
                "post_action_screenshot_required": True,
                "source_frame_url": self.redact(safe_url(str(getattr(source.get("frame"), "url", "")))) if isinstance(source, dict) else "",
            }
        )
        self.control_actions[-1]["source_frame_url"] = str(payload.get("source_frame_url") or self.control_actions[-1]["source_frame_url"])
        self._completed_control_correlations.add(correlation_id)
        self._pending_control_actions.pop(correlation_id, None)
        self._control_role_authorizations.pop(correlation_id, None)

    def attest_control_state(
        self,
        *,
        control_key: str,
        state: str,
        assertion: str,
        action_id: str | None = None,
    ) -> str:
        """Bind an explicit test assertion to a previously captured trusted action.

        State is never inferred from a click or a screenshot.  A future case must
        call this only after checking the real normal/abnormal outcome.  One action
        may prove only one state, so normal and abnormal need separate interactions.
        """

        assert state in {"normal", "abnormal"}, "control state must be normal or abnormal"
        assert len(assertion.strip()) >= 12, "control state attestation requires a concrete assertion description"

        def matching_candidates(expected_start: dict | None) -> list[dict]:
            if not expected_start:
                return []
            return [
                row
                for row in self.control_actions
                if control_key in row.get("control_keys", ())
                and (action_id is None or row.get("action_id") == action_id)
                and abs(float(row.get("occurred_at_epoch_seconds") or 0) - float(expected_start["occurred_at_epoch_seconds"])) <= 0.001
                and row.get("page_path") == expected_start.get("page_path")
                and row.get("page_id") == expected_start.get("page_id")
            ]

        # The trusted DOM listener must complete a real /auth/me round trip
        # before it calls the Playwright binding.  Pump each attached page once
        # so an immediately-following assertion cannot bind to stale evidence.
        for page in reversed(self._attached_pages):
            try:
                if not page.is_closed():
                    page.wait_for_timeout(100)
            except Exception:
                continue
        expected_start = self._control_action_starts.get(control_key)
        candidates = matching_candidates(expected_start)
        deadline = time.monotonic() + 5
        while not candidates and time.monotonic() < deadline:
            progressed = False
            for page in reversed(self._attached_pages):
                try:
                    if not page.is_closed():
                        page.wait_for_timeout(25)
                        progressed = True
                except Exception:
                    continue
            if not progressed:
                break
            expected_start = self._control_action_starts.get(control_key)
            candidates = matching_candidates(expected_start)
        assert candidates, f"no trusted control action exists for exact key {control_key}"
        action = candidates[-1]
        attestations = action.setdefault("state_attestations", [])
        assert not attestations, f"control action {action['action_id']} already has a state attestation"
        attestation = {
            "state": state,
            "outcome": "passed",
            "source": "explicit-test-assertion",
            "assertion": assertion.strip(),
            "control_key": control_key,
            "action_id": action["action_id"],
            "page_path": action["page_path"],
            "attested_at_epoch_seconds": time.time(),
        }
        attestations.append(attestation)
        action["state_contract"]["observed"] = sorted({str(row["state"]) for row in attestations})
        self._pending_screenshot_action_ids.add(str(action["action_id"]))
        return str(action["action_id"])

    def begin_auth_bootstrap(self) -> None:
        assert not self._auth_bootstrap_active, "authentication bootstrap scope is already active"
        self._auth_bootstrap_active = True

    def arm_control_authorization(self, page, *, control_key: str) -> dict:
        """Bind an awaited pre-action role probe to the next exact trusted control.

        Authentication-changing actions cannot use a concurrent role probe:
        login/logout may reach the server before that probe.  This method
        completes and correlates the probe first, then exposes only its opaque
        correlation ID to one exact control event for at most five seconds.
        """

        trusted_keys = self._trusted_control_keys([control_key])
        assert trusted_keys == [control_key], f"unsafe control key for pre-action authorization: {control_key}"
        page_id = self._attached_page_ids.get(id(page))
        assert page_id is not None, "pre-action authorization page is not attached to evidence capture"
        self._pre_action_role_sequence += 1
        correlation_id = f"control-action-role-{int(time.time() * 1000)}-{self._pre_action_role_sequence}"
        observation = page.evaluate(
            """
            async ({correlationId, controlKey}) => {
              const response = await fetch('/auth/me', {
                method: 'GET', credentials: 'same-origin', keepalive: true,
                headers: {
                  'Accept': 'application/json',
                  'X-Sherpa-UI-Evidence-Probe': 'control-action-role-v1',
                  'X-Sherpa-UI-Evidence-Correlation': correlationId,
                },
              });
              let role = 'unknown';
              let authDisabled = false;
              if (response.status === 401 || response.status === 403) {
                role = 'anonymous';
              } else {
                const body = await response.json().catch(() => ({}));
                role = ['admin', 'user'].includes(String(body.role)) ? String(body.role) : 'unknown';
                authDisabled = body.auth_disabled === true;
              }
              const authorization = {
                status: response.status,
                role,
                auth_disabled: authDisabled,
                observed_at_epoch_seconds: Date.now() / 1000,
                evidence_correlation_id: correlationId,
              };
              window.__sherpaUiArmedControlAuthorization = {
                correlation_id: correlationId,
                control_key: controlKey,
                expires_at_epoch_ms: Date.now() + 5000,
              };
              return authorization;
            }
            """,
            {"correlationId": correlation_id, "controlKey": control_key},
        )
        assert _is_valid_control_authorization_observation(observation), "pre-action authorization response has no verified role/status"
        assert observation["evidence_correlation_id"] == correlation_id
        deadline = time.monotonic() + 2
        while correlation_id not in self._control_role_authorizations and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        cached = self._control_role_authorizations.get(correlation_id)
        assert cached is not None and cached.get("page_id") == page_id, "pre-action role probe lacks browser HTTP evidence"
        cached_authorization = cached.get("authorization")
        assert _is_valid_control_authorization_observation(cached_authorization)
        assert all(
            cached_authorization.get(key) == observation.get(key) for key in ("status", "role", "auth_disabled", "evidence_correlation_id")
        ), "pre-action role probe disagrees with browser response evidence"
        assert abs(float(cached_authorization["observed_at_epoch_seconds"]) - float(observation["observed_at_epoch_seconds"])) <= 5
        return self.redact(observation)

    def arm_control(self, locator, *, control_key: str) -> None:
        """Bind one exact DOM target to its source-derived manifest key.

        Native controls are discovered automatically.  This explicit path is
        required for delegated handlers on ``div``/``tr``/``th`` and for the
        few source controls that intentionally have no stable DOM identity.
        The marker is single-use and a case fails if no trusted browser event
        consumes it.
        """

        assert self._trusted_control_keys([control_key]) == [control_key], f"unsafe explicit control key: {control_key}"
        assert locator.count() == 1, f"explicit control locator must resolve exactly once: {control_key}"
        assert locator.is_visible(), f"explicit control is not visible: {control_key}"
        locator.evaluate(
            """
            (element, key) => {
              const attribute = 'data-sherpa-ui-evidence-control-key';
              if (element.hasAttribute(attribute)) throw new Error('unkeyed control is already armed');
              element.setAttribute(attribute, key);
            }
            """,
            control_key,
        )
        self._armed_explicit_control_keys.add(control_key)

    def arm_unkeyed_control(self, locator, *, control_key: str) -> None:
        """Assign one source-derived key to one exact unkeyed DOM control."""

        assert control_key.startswith("@unkeyed:"), f"unkeyed arm requires an @unkeyed key: {control_key}"
        self.arm_control(locator, control_key=control_key)

    def end_auth_bootstrap(self) -> None:
        assert self._auth_bootstrap_active, "authentication bootstrap scope is not active"
        self._auth_bootstrap_active = False

    def register_secret(self, value: str | None) -> None:
        secret = str(value or "")
        if len(secret) >= 6 and secret not in self._secret_values:
            self._secret_values.append(secret)
            append_private_text(self._secret_registry, json.dumps({"value": secret}, ensure_ascii=False) + "\n")

    def redact(self, value):
        value = redact(value)
        if isinstance(value, dict):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self._secret_values, key=len, reverse=True):
                value = value.replace(secret, "<redacted>")
            return value
        return value

    def allow_http_error_console(
        self,
        *,
        method: str,
        path: str,
        status: int,
        expected_count: int,
        reason: str,
    ) -> str:
        """Expect a fixed number of Chromium errors backed by exact responses."""

        normalized_method = method.strip().upper()
        normalized_reason = reason.strip()
        try:
            parsed = urlsplit(path)
        except ValueError as exc:
            raise AssertionError("HTTP console allowance path is invalid") from exc
        assert normalized_method in _REQUEST_FAILURE_METHODS, "HTTP console allowance method is unsupported"
        assert (
            parsed.path == path
            and path.startswith("/")
            and not path.startswith("//")
            and not parsed.scheme
            and not parsed.netloc
            and not parsed.query
            and not parsed.fragment
            and ".." not in path.split("/")
            and len(path) <= 240
        ), "HTTP console allowance requires one exact absolute URL path"
        assert isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599
        assert isinstance(expected_count, int) and not isinstance(expected_count, bool) and 1 <= expected_count <= 10
        assert 24 <= len(normalized_reason) <= 300, "HTTP console allowance requires a detailed non-secret reason"
        identity = (normalized_method, path, status)
        assert not any((row["method"], row["path"], row["status"]) == identity for row in self._console_http_error_allowances), (
            "duplicate HTTP console allowance in one case is ambiguous"
        )
        self._console_http_error_allowance_sequence += 1
        allowance_id = f"http-console-allowance-{self._console_http_error_allowance_sequence:03d}"
        self._console_http_error_allowances.append(
            {
                "allowance_id": allowance_id,
                "method": normalized_method,
                "path": path,
                "status": status,
                "expected_count": expected_count,
                "reason": self.redact(normalized_reason),
                "registered_after_event_sequence": self._browser_network_event_sequence,
                "matched_request_ids": [],
            }
        )
        return allowance_id

    def allow_request_failure(
        self,
        *,
        method: str,
        path: str,
        resource_type: str,
        failure: str,
        reason: str,
    ) -> str:
        """Allow one exact, case-local failure and retain its justification."""

        normalized_method = method.strip().upper()
        normalized_resource_type = resource_type.strip().lower()
        normalized_failure = failure.strip()
        normalized_reason = reason.strip()
        try:
            parsed = urlsplit(path)
        except ValueError as exc:
            raise AssertionError("request-failure allowance path is invalid") from exc
        assert normalized_method in _REQUEST_FAILURE_METHODS, "request-failure allowance method is unsupported"
        assert (
            parsed.path == path
            and path.startswith("/")
            and not path.startswith("//")
            and not parsed.scheme
            and not parsed.netloc
            and not parsed.query
            and not parsed.fragment
            and ".." not in path.split("/")
            and len(path) <= 240
        ), "request-failure allowance requires one exact absolute URL path without query or traversal"
        assert re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", normalized_resource_type), "request-failure allowance resource type is invalid"
        assert _REQUEST_FAILURE_VALUE.fullmatch(normalized_failure), "request-failure allowance failure must be exact"
        assert len(normalized_reason) >= 24 and len(normalized_reason) <= 300, (
            "request-failure allowance requires a detailed non-secret reason"
        )
        identity = (normalized_method, path, normalized_resource_type, normalized_failure)
        assert not any(
            (row["method"], row["path"], row["resource_type"], row["failure"]) == identity for row in self._allowed_request_failures
        ), "duplicate request-failure allowance in one case is ambiguous"
        self._request_failure_allowance_sequence += 1
        allowance_id = f"request-failure-allowance-{self._request_failure_allowance_sequence:03d}"
        self._allowed_request_failures.append(
            {
                "allowance_id": allowance_id,
                "method": normalized_method,
                "path": path,
                "resource_type": normalized_resource_type,
                "failure": normalized_failure,
                "reason": self.redact(normalized_reason),
                "registered_after_event_sequence": self._browser_network_event_sequence,
                "matched_request_ids": [],
            }
        )
        return allowance_id

    def start_trace(self, context) -> None:
        if self._trace_active:
            return
        self._context = context
        # Trace DOM/API records are sanitized below. Pixel frames cannot be reliably
        # inspected for secrets, so embedded trace screenshots are forbidden.
        # DOM snapshots and response-resource blobs can contain pixels/binary
        # payloads that cannot be reliably secret-scanned.  Keep the Playwright
        # action/network metadata trace only.
        context.tracing.start(screenshots=False, snapshots=False, sources=False)
        self._trace_active = True

    def stop_trace(self, *, save: bool = True) -> None:
        if not self._trace_active or self._context is None:
            return
        for cookie in self._context.cookies():
            self.register_secret(cookie.get("value"))
        raw = self._secret_registry.parent / (f".playwright-trace-{os.getpid()}-{secrets.token_hex(8)}.zip")
        try:
            if save:
                self._context.tracing.stop(path=str(raw))
                raw_metadata = raw.lstat()
                assert not raw.is_symlink() and stat.S_ISREG(raw_metadata.st_mode) and raw_metadata.st_nlink == 1, (
                    "raw Playwright trace failed filesystem boundaries"
                )
                chmod_path_no_follow(raw, 0o600, require_owner_uid=os.geteuid())
                target = self.case_dir / "browser" / "trace.zip"
                assert not target.is_symlink(), "trace target must not be a symlink"
                if target.is_file():
                    assert target.lstat().st_nlink == 1, "trace target must not be hardlinked"
                    archived = self.case_dir / "browser" / (f"trace-{self._saved_trace_segments:03d}.zip")
                    assert not archived.exists(), f"trace segment already exists: {archived.name}"
                    target.replace(archived)
                    for attestation in self.trace_attestations:
                        if attestation.get("trace") == str(target.relative_to(self.case_dir)):
                            attestation["trace"] = str(archived.relative_to(self.case_dir))
                            self._persist_trace_attestation(archived, attestation)
                    self._trace_sidecar(target).unlink(missing_ok=True)
                self._sanitize_trace(raw, target)
                chmod_path_no_follow(target, 0o600, require_owner_uid=os.geteuid())
                self._saved_trace_segments += 1
            else:
                self._context.tracing.stop()
        finally:
            self._trace_active = False

    def screenshot(self, page, step: int, name: str, *, full_page: bool = True) -> Path:
        clean = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "screen"
        path = self.case_dir / "screenshots" / f"{step:03d}__{clean}.png"
        assert not path.exists() and not path.is_symlink(), f"semantic screenshot already exists: {path.name}"
        marked = page.locator('[data-sherpa-ui-secret-mask="1"]')
        captured = False
        initial_scan = None
        final_scan = None
        pre_capture = None
        post_capture = None
        try:
            initial_scan = page.evaluate(_DOM_SECRET_SCAN_SCRIPT, self._secret_values)
            self._assert_dom_scan_safe(initial_scan, stage="before screenshot")
            for attempt in range(3):
                pre_capture = self._capture_context_observation(page)
                page.screenshot(
                    path=str(path),
                    full_page=full_page,
                    mask=[marked],
                    mask_color="#222222",
                )
                final_scan = page.evaluate(_DOM_SECRET_RESCAN_SCRIPT)
                self._assert_dom_scan_safe(final_scan, stage="after screenshot")
                post_capture = self._capture_context_observation(page)
                stable = (
                    initial_scan["stability_fingerprint"] == final_scan["stability_fingerprint"]
                    and initial_scan["detected_element_count"] == final_scan["detected_element_count"]
                    and initial_scan["dom_marked_element_count"] == final_scan["dom_marked_element_count"]
                    and self._stable_capture_context(pre_capture) == self._stable_capture_context(post_capture)
                )
                if stable:
                    screenshot_metadata = path.lstat()
                    assert (
                        not path.is_symlink()
                        and stat.S_ISREG(screenshot_metadata.st_mode)
                        and screenshot_metadata.st_nlink == 1
                        and screenshot_metadata.st_size > 0
                    ), "browser did not produce a safe semantic screenshot"
                    chmod_path_no_follow(path, 0o600, require_owner_uid=os.geteuid())
                    captured = True
                    break
                path.unlink(missing_ok=True)
                initial_scan = final_scan
                if attempt < 2:
                    page.wait_for_timeout(50)
            assert captured, "DOM masking or capture context did not converge; screenshot was discarded"
        finally:
            cleanup_error = None
            try:
                page.evaluate(_DOM_SECRET_CLEANUP_SCRIPT)
            except Exception as exc:  # Playwright exceptions must fail closed here.
                cleanup_error = exc
            if not captured or cleanup_error is not None:
                path.unlink(missing_ok=True)
            if cleanup_error is not None:
                raise AssertionError("DOM secret masking cleanup failed; screenshot was discarded") from cleanup_error

        assert initial_scan is not None and final_scan is not None
        assert pre_capture is not None and post_capture is not None
        relative = str(path.relative_to(self.case_dir))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        viewport = post_capture["viewport"]
        auth_observation = post_capture["authorization"]
        png_width, png_height = self._png_dimensions(path)
        viewport_class = "narrow" if viewport["width"] <= 500 else "desktop" if viewport["width"] >= 1024 else "intermediate"
        attestation = {
            "screenshot": path.name,
            "path": relative,
            "sha256": digest,
            "captured_at_epoch_seconds": time.time(),
            "page_path": post_capture["page_path"],
            "viewport": {
                "width": int(viewport["width"]),
                "height": int(viewport["height"]),
            },
            "viewport_class": viewport_class,
            "png_dimensions": {"width": png_width, "height": png_height},
            "full_page": bool(full_page),
            "pre_capture_context": pre_capture,
            "post_capture_context": post_capture,
            "page_id": post_capture["page_id"],
            "authorization_observation": auth_observation,
            "control_action_ids": sorted(self._pending_screenshot_action_ids),
            "scan_completed": True,
            "capture_succeeded": True,
            "mutation_observer_enabled_during_capture": True,
            "known_match_count": final_scan["known_match_count"],
            "pattern_match_count": final_scan["pattern_match_count"],
            "sensitive_field_count": final_scan["sensitive_field_count"],
            "visible_text_match_count": final_scan["visible_text_match_count"],
            "input_value_match_count": final_scan["input_value_match_count"],
            "attribute_match_count": final_scan["attribute_match_count"],
            "opaque_pixel_surface_count": final_scan["opaque_pixel_surface_count"],
            "pseudo_content_match_count": final_scan["pseudo_content_match_count"],
            "shadow_root_count": final_scan["shadow_root_count"],
            "uninspectable_custom_element_count": final_scan["uninspectable_custom_element_count"],
            "closed_shadow_host_count": final_scan["closed_shadow_host_count"],
            "detected_element_count": final_scan["detected_element_count"],
            "masked_element_count": final_scan["masked_element_count"],
            "open_shadow_masked_element_count": final_scan["open_shadow_masked_element_count"],
            "unmasked_visible_element_count": final_scan["unmasked_visible_element_count"],
            "inline_mask_failure_count": final_scan["inline_mask_failure_count"],
            "pre_capture_known_match_count": initial_scan["known_match_count"],
            "pre_capture_pattern_match_count": initial_scan["pattern_match_count"],
            "initial_detected_element_count": initial_scan["detected_element_count"],
            "initial_masked_element_count": initial_scan["masked_element_count"],
            "pre_capture_dom_marked_element_count": initial_scan["dom_marked_element_count"],
            "post_capture_dom_marked_element_count": final_scan["dom_marked_element_count"],
            "policy": "capture-time DOM secret scan and element masking",
        }
        self.screenshot_attestations.append(attestation)
        try:
            self._atomic_write_json(self._screenshot_sidecar(path), attestation)
        except Exception:
            self.screenshot_attestations.pop()
            path.unlink(missing_ok=True)
            raise
        self._pending_screenshot_action_ids.difference_update(attestation["control_action_ids"])
        self.screenshot_mask_events.append(
            {
                "screenshot": path.name,
                "visible_secret_groups_masked": final_scan["known_match_count"],
                "secret_pattern_groups_masked": final_scan["pattern_match_count"],
                "secret_input_fields_masked": final_scan["input_value_match_count"],
                "sensitive_fields_masked": final_scan["sensitive_field_count"],
                "masked_element_count": final_scan["masked_element_count"],
                "policy": "known and secret-pattern DOM content is masked before capture",
            }
        )
        return path

    @staticmethod
    def _stable_capture_context(observation: dict) -> dict:
        authorization = observation.get("authorization") if isinstance(observation, dict) else None
        return {
            "page_path": observation.get("page_path") if isinstance(observation, dict) else None,
            "page_id": observation.get("page_id") if isinstance(observation, dict) else None,
            "viewport": observation.get("viewport") if isinstance(observation, dict) else None,
            "authorization": {
                "status": authorization.get("status") if isinstance(authorization, dict) else None,
                "role": authorization.get("role") if isinstance(authorization, dict) else None,
                "auth_disabled": authorization.get("auth_disabled") if isinstance(authorization, dict) else None,
            },
        }

    def _capture_context_observation(self, page) -> dict:
        page_id = self._attached_page_ids.get(id(page))
        assert page_id is not None, "screenshot page is not attached to evidence capture"
        self._screenshot_role_sequence += 1
        correlation_id = f"screenshot-role-{int(time.time() * 1000)}-{self._screenshot_role_sequence}"
        observation = page.evaluate(
            """
            async ({correlationId, pageId}) => {
              const viewport = {width: window.innerWidth, height: window.innerHeight};
              const observed = () => Date.now() / 1000;
              try {
                const response = await fetch('/auth/me', {
                  method: 'GET',
                  credentials: 'same-origin',
                  headers: {
                    'Accept': 'application/json',
                    'X-Sherpa-UI-Evidence-Probe': 'screenshot-role-v1',
                    'X-Sherpa-UI-Evidence-Correlation': correlationId,
                  },
                });
                if (response.status === 401 || response.status === 403) {
                  return {
                    page_path: location.pathname || '/', page_id: pageId, viewport,
                    authorization: {
                      status: response.status, role: 'anonymous', auth_disabled: false,
                      observed_at_epoch_seconds: observed(), evidence_correlation_id: correlationId,
                    },
                  };
                }
                const body = await response.json().catch(() => ({}));
                return {
                  page_path: location.pathname || '/', page_id: pageId, viewport,
                  authorization: {
                    status: response.status,
                    role: ['admin', 'user'].includes(String(body.role)) ? String(body.role) : 'unknown',
                    auth_disabled: body.auth_disabled === true,
                    observed_at_epoch_seconds: observed(),
                    evidence_correlation_id: correlationId,
                  },
                };
              } catch (_) {
                return {
                  page_path: location.pathname || '/', page_id: pageId, viewport,
                  authorization: {
                    status: 0, role: 'unknown', auth_disabled: false,
                    observed_at_epoch_seconds: observed(), evidence_correlation_id: correlationId,
                  },
                };
              }
            }
            """,
            {"correlationId": correlation_id, "pageId": page_id},
        )
        observation = self.redact(observation)
        authorization = observation.get("authorization") if isinstance(observation, dict) else None
        assert _is_valid_control_authorization_observation(authorization), "screenshot authorization response has no verified role/status"
        return observation

    @staticmethod
    def _png_dimensions(path: Path) -> tuple[int, int]:
        header = path.read_bytes()[:24]
        assert len(header) == 24 and header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR", (
            "semantic screenshot is not a valid PNG"
        )
        return struct.unpack(">II", header[16:24])

    @staticmethod
    def _assert_dom_scan_safe(scan, *, stage: str) -> None:
        assert isinstance(scan, dict) and scan.get("scan_completed") is True, f"DOM secret scan did not complete {stage}"
        required_counts = (
            "known_match_count",
            "pattern_match_count",
            "sensitive_field_count",
            "visible_text_match_count",
            "input_value_match_count",
            "attribute_match_count",
            "opaque_pixel_surface_count",
            "pseudo_content_match_count",
            "shadow_root_count",
            "uninspectable_custom_element_count",
            "closed_shadow_host_count",
            "detected_element_count",
            "masked_element_count",
            "open_shadow_masked_element_count",
            "unmasked_visible_element_count",
            "inline_mask_failure_count",
            "dom_marked_element_count",
        )
        assert all(isinstance(scan.get(key), int) and scan[key] >= 0 for key in required_counts), (
            f"DOM secret scan returned invalid counters {stage}"
        )
        assert scan["masked_element_count"] >= scan["detected_element_count"], (
            f"DOM secret detection could not mask every matching element {stage}"
        )
        assert scan["unmasked_visible_element_count"] == 0, f"DOM secret masking left a rendered matching element visible {stage}"
        assert scan["inline_mask_failure_count"] == 0, f"DOM secret masking did not install every inline capture guard {stage}"
        assert scan["dom_marked_element_count"] >= scan["detected_element_count"], (
            f"DOM secret mask markers could not cover every matching element {stage}"
        )

    def write_json(self, relative: str, value) -> Path:
        path = self.case_dir / relative
        write_private_text_atomic(
            path,
            json.dumps(self.redact(value), ensure_ascii=False, indent=2, default=str) + "\n",
        )
        return path

    def _atomic_write_json(self, path: Path, value) -> None:
        write_private_text_atomic(
            path,
            json.dumps(self.redact(value), ensure_ascii=False, sort_keys=True, default=str) + "\n",
        )

    def _screenshot_sidecar(self, screenshot: Path) -> Path:
        return self.case_dir / "security" / "screenshot-sidecars" / f"{screenshot.name}.json"

    def _trace_sidecar(self, trace: Path) -> Path:
        return self.case_dir / "security" / "trace-sidecars" / f"{trace.name}.json"

    def _persist_trace_attestation(self, trace: Path, attestation: dict) -> None:
        self._atomic_write_json(self._trace_sidecar(trace), attestation)

    def write_jsonl(self, relative: str, rows: list[dict]) -> Path:
        path = self.case_dir / relative
        text = "".join(json.dumps(self.redact(row), ensure_ascii=False, default=str) + "\n" for row in rows)
        write_private_text_atomic(path, text)
        return path

    def record_api(self, *, method: str, url: str, status: int, elapsed_ms: int) -> None:
        self.http.append(
            {
                "ts": time.time(),
                "phase": "independent-client",
                "method": method,
                "url": self.redact(safe_url(url)),
                "status": status,
                "elapsed_ms": elapsed_ms,
            }
        )

    def record_sse_collection(
        self,
        *,
        path: str,
        status: int,
        events: list[dict],
        timings: list[dict],
    ) -> None:
        """Aggregate every independent SSE observation into the canonical evidence files.

        Custom per-turn files remain useful for diagnosis, but they must not leave an
        empty ``network/sse.jsonl`` that merely looks like valid evidence.
        """

        assert len(events) == len(timings), "SSE event and receipt-timing counts differ"
        match = re.search(r"/chat/turns/([^/?]+)/stream(?:\?|$)", path)
        turn_id = match.group(1) if match else ""
        collection_index = len(self.sse_collections)
        self.sse_collections.append(
            {
                "collection_index": collection_index,
                "source_path": safe_url(path),
                "status": int(status),
                "turn_id": turn_id,
                "event_count": len(events),
                "timing_count": len(timings),
            }
        )
        self.sse_events.extend(dict(event) for event in events)
        self.sse_timings.extend(
            {
                **dict(timing),
                "collection_index": collection_index,
                "source_path": safe_url(path),
                "turn_id": turn_id,
            }
            for timing in timings
        )

    def record_database_correlation(
        self,
        *,
        conversation_id: int,
        turn_id: str | None,
        source: str,
        assistant_message_id: int | None = None,
        audit_id: int | None = None,
    ) -> None:
        """Record that this case inspected the real conversation rows in Postgres."""

        row = {
            "conversation_id": int(conversation_id),
            "turn_id": str(turn_id or ""),
            "source": str(source),
            "assistant_message_id": int(assistant_message_id) if assistant_message_id is not None else None,
            "audit_id": int(audit_id) if audit_id is not None else None,
        }
        if row not in self.database_correlations:
            self.database_correlations.append(row)

    def record_provider_correlation(
        self,
        *,
        turn_id: str,
        provider: str,
        model: str,
        input_tokens,
        output_tokens,
        operation: str,
        configured_agent: str | None = None,
    ) -> None:
        identifier = str(turn_id).strip()
        selected_provider = str(provider).strip().lower()
        selected_model = str(model).strip()
        assert identifier, "provider correlation requires a case-unique turn identifier"
        assert selected_provider and selected_model, "provider correlation requires provider and model"
        assert not any(row["turn_id"] == identifier for row in self.provider_correlations), (
            f"provider correlation already contains turn {identifier}"
        )
        input_count = int(input_tokens or 0)
        output_count = int(output_tokens or 0)
        assert input_count + output_count > 0, "provider correlation requires non-zero token usage"
        self.provider_correlations.append(
            {
                "turn_id": identifier,
                "operation": operation,
                "provider": selected_provider,
                "model": selected_model,
                "input_tokens": input_count,
                "output_tokens": output_count,
                "configured_agent": str(configured_agent or selected_provider).strip().lower(),
                "usage_provider": selected_provider,
                "usage_model": selected_model,
            }
        )

    def record_usage_event(self, usage: dict, *, turn_id: str, operation: str) -> None:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if input_tokens is None and output_tokens is None:
            provider = str(usage.get("provider") or "").strip().lower()
            model = str(usage.get("model") or "").strip()
            calls = int(usage.get("calls") or 0)
            assert provider and model and calls > 0, "unreported-token usage evidence still requires real provider/model/call identity"
            self.provider_usage_unreported.append(
                {
                    "turn_id": turn_id,
                    "operation": operation,
                    "provider": provider,
                    "model": model,
                    "calls": calls,
                    "reason": "provider response did not report token usage",
                }
            )
            return
        self.record_provider_correlation(
            turn_id=turn_id,
            provider=usage.get("provider"),
            model=usage.get("model"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            operation=operation,
        )

    def add_cleanup(self, label: str, callback: Callable[[], None]) -> None:
        self._cleanups.append((label, callback))

    def run_cleanups(self) -> None:
        while self._cleanups:
            label, callback = self._cleanups.pop()
            try:
                callback()
            except Exception as exc:
                self.cleanup_errors.append(f"{label}: {type(exc).__name__}: {self.redact(str(exc))}")

    def flush(self) -> None:
        self.finalize_request_failure_expectations()
        self.finalize_console_error_expectations()
        self.write_jsonl("browser/console.jsonl", self.console)
        self.write_jsonl("browser/page-errors.jsonl", self.page_errors)
        self.write_jsonl("browser/request-failures.jsonl", self.request_failures)
        self.write_jsonl(
            "browser/request-failure-allowances.jsonl",
            self._request_failure_allowance_evidence(),
        )
        self.write_jsonl(
            "browser/console-error-allowances.jsonl",
            self._console_http_error_allowance_evidence(),
        )
        self.write_jsonl("browser/control-actions.jsonl", self.control_actions)
        if self._pending_control_actions:
            self.write_json(
                "browser/incomplete-control-actions.json",
                {
                    "count": len(self._pending_control_actions),
                    "actions": [
                        {
                            "correlation_sha256": hashlib.sha256(correlation.encode("utf-8")).hexdigest(),
                            "page_id": str(record.get("page_id") or ""),
                            "page_path": str(record.get("payload", {}).get("page_path") or ""),
                            "control_keys": self._trusted_control_keys(record.get("payload", {}).get("control_keys")),
                        }
                        for correlation, record in sorted(self._pending_control_actions.items())
                    ],
                },
            )
        self.write_jsonl("network/http.jsonl", self.http)
        self.write_jsonl("network/navigation.jsonl", self.navigation_events)
        # 全caseで同じ証跡treeを作る。独立clientを使ったcaseは全streamを
        # canonical filesへ集約し、custom名だけが残る状態を許さない。
        self.write_jsonl("network/sse.jsonl", self.sse_events)
        self.write_jsonl("network/sse-timing.jsonl", self.sse_timings)
        self.write_jsonl("network/sse-collections.jsonl", self.sse_collections)
        if self.database_correlations:
            self.write_jsonl("state/database-correlations.jsonl", self.database_correlations)
            if not (self.case_dir / "state" / "db-summary.json").is_file():
                self.write_json(
                    "state/db-summary.json",
                    {
                        "evidence_kind": "case-database-correlation",
                        "correlations": self.database_correlations,
                    },
                )
        self.write_jsonl("security/screenshot-mask-events.jsonl", self.screenshot_mask_events)
        self.write_jsonl("security/screenshot-attestations.jsonl", self.screenshot_attestations)
        self.write_jsonl("security/trace-attestations.jsonl", self.trace_attestations)
        if self.provider_correlations:
            self.write_jsonl("state/provider-correlation.jsonl", self.provider_correlations)
        if self.provider_usage_unreported:
            self.write_jsonl(
                "state/provider-usage-unreported.jsonl",
                self.provider_usage_unreported,
            )

    def capture_service_logs(self) -> None:
        profile_services = self.case_dir.parents[1] / "services"
        if not profile_services.is_dir():
            self.write_json(
                "services/log-collection.json",
                {"available": False, "reason": "profile service log directory is absent"},
            )
            return
        app_candidates = [profile_services / "app.log"]
        app_candidates.extend(sorted(profile_services.glob("app-start*.log")))
        app_candidates.extend(sorted(profile_services.glob("app-restart-*.log")))
        app_sources = []
        for source in app_candidates:
            if source.is_symlink():
                raise AssertionError(f"service log must not be a symlink: {source.name}")
            if source.is_file() and source.lstat().st_nlink != 1:
                raise AssertionError(f"service log must not be hardlinked: {source.name}")
            if source.is_file() and source not in app_sources:
                app_sources.append(source)
        if app_sources:
            sections = []
            for source in app_sources:
                body = self.redact(source.read_text(encoding="utf-8", errors="replace"))
                sections.append(f"[{source.name}]\n{body}")
            write_private_text_atomic(self.case_dir / "services" / "app.log", "\n".join(sections))
        compose_sources = []
        for name in ("compose.log", "compose-up.log", "ocr-up.log", "cleanup.log"):
            source = profile_services / name
            if source.is_symlink():
                raise AssertionError(f"compose log must not be a symlink: {name}")
            if source.is_file():
                assert source.lstat().st_nlink == 1, f"compose log must not be hardlinked: {name}"
                compose_sources.append(source)
        if compose_sources:
            sections = []
            for source in compose_sources:
                body = self.redact(source.read_text(encoding="utf-8", errors="replace"))
                sections.append(f"[{source.name}]\n{body}")
            write_private_text_atomic(self.case_dir / "services" / "compose.log", "\n".join(sections))
        self.write_json(
            "services/log-collection.json",
            {
                "available": bool(app_sources or compose_sources),
                "app_logs": [source.name for source in app_sources],
                "compose_logs": [source.name for source in compose_sources],
            },
        )

    def capture_profile_state(self) -> None:
        profile_state = self.case_dir.parents[1] / "state"
        postgres_source = profile_state / "postgres-identity.json"
        fixture_source = profile_state / "fixture-files.sha256.json"
        if postgres_source.is_file():
            identity = json.loads(postgres_source.read_text(encoding="utf-8"))
            assert isinstance(identity, dict) and identity, "runner Postgres identity is empty"
            self.write_json("state/postgres-identity.json", identity)
            if not (self.case_dir / "state" / "db-summary.json").is_file():
                self.write_json(
                    "state/db-summary.json",
                    {"evidence_kind": "runner-postgres-identity", "postgres": identity},
                )
        if fixture_source.is_file():
            fixture_hashes = json.loads(fixture_source.read_text(encoding="utf-8"))
            assert fixture_hashes, "runner fixture hash evidence is empty"
            self.write_json("state/fixture-files.sha256.json", fixture_hashes)
            if not (self.case_dir / "state" / "files.sha256").is_file():
                self.write_json("state/files.sha256", fixture_hashes)

    def _correlate_401_console_errors(self) -> None:
        """Rebuild auth and role 401 evidence in one shared event sequence."""

        for console in self.console:
            if console.get("expected_source") not in {
                "correlated-role-probe-401",
                "correlated-auth-bootstrap-401",
            }:
                continue
            console["expected"] = False
            console["expected_source"] = None
            console["expected_reason"] = None
            console["expectation_case"] = None
            console.pop("console_http_correlation", None)

        consumed: set[str] = set()
        for console in sorted(
            self.console,
            key=lambda row: (
                row.get("event_sequence")
                if isinstance(row.get("event_sequence"), int) and not isinstance(row.get("event_sequence"), bool)
                else 2**63
            ),
        ):
            if console.get("expected") is True:
                continue
            classification = _automatic_401_console_correlation(
                console,
                self.http,
                consumed,
            )
            if classification is None:
                continue
            consumed.add(classification["request_id"])
            console["expected"] = True
            console["expected_source"] = classification["expected_source"]
            console["expected_reason"] = classification["expected_reason"]
            console["expectation_case"] = self.nodeid
            console["console_http_correlation"] = classification["console_http_correlation"]

    def finish(self, outcome: str, error: str | None = None) -> list[str]:
        self.flush()
        self.capture_service_logs()
        state_capture_failures = []
        try:
            self.capture_profile_state()
        except (AssertionError, json.JSONDecodeError, OSError, ValueError) as exc:
            state_capture_failures.append(f"runner state evidence capture failed: {type(exc).__name__}: {self.redact(str(exc))}")
        diagnostic_failures = self._diagnostic_failures() + state_capture_failures
        evidence_failures = self._required_evidence_failures()
        finish_failures = diagnostic_failures + evidence_failures
        if finish_failures:
            outcome = "failed"
            details = "; ".join(finish_failures)
            error = f"{error}; {details}" if error else details
        result = {
            "nodeid": self.nodeid,
            "outcome": outcome,
            "duration_seconds": round(time.time() - self.started, 3),
            "page_errors": len(self.page_errors),
            "request_failures": len(self.request_failures),
            "cleanup_errors": self.cleanup_errors,
            "evidence_failures": evidence_failures,
            "diagnostic_failures": diagnostic_failures,
            "error": self.redact(error) if error else None,
        }
        leaks = self._scan_for_secrets()
        if leaks:
            outcome = "failed"
            security_error = f"secret-like or unattested material was removed from {len(leaks)} artifact file(s)"
            error = f"{error}; {security_error}" if error else security_error
            result["outcome"] = outcome
            result["error"] = self.redact(error)
            result["security_leak_count"] = len(leaks)
            result["security_removed_files"] = leaks
            self.write_json("security/leak-report.json", {"removed_files": leaks, "count": len(leaks)})
        else:
            result["security_leak_count"] = 0
            result["security_removed_files"] = []
        self.write_json("result.json", result)
        return finish_failures + [f"secret-like or unattested material removed from {path}" for path in leaks]

    def _diagnostic_failures(self) -> list[str]:
        self.finalize_request_failure_expectations()
        self.finalize_console_error_expectations()
        failures = []
        if self.page_errors:
            failures.append(f"browser emitted {len(self.page_errors)} page error(s)")
        unexpected_console = [row for row in self.console if row.get("type") == "error" and not row.get("expected")]
        if unexpected_console:
            failures.append(f"browser emitted {len(unexpected_console)} unexpected console error(s)")
        unexpected_requests = [row for row in self.request_failures if not row.get("expected")]
        if unexpected_requests:
            failures.append(f"browser emitted {len(unexpected_requests)} unexpected request failure(s)")
        failures.extend(
            _request_failure_expectation_contract_failures(
                self.nodeid,
                self.request_failures,
                self._allowed_request_failures,
                http_rows=self.http,
                navigation_events=self.navigation_events,
            )
        )
        failures.extend(
            _console_expectation_contract_failures(
                self.nodeid,
                self.console,
                self._console_http_error_allowances,
                self.request_failures,
                self.http,
            )
        )
        if self.cleanup_errors:
            failures.append(f"case cleanup reported {len(self.cleanup_errors)} error(s)")
        if self._pending_control_actions:
            failures.append(f"trusted control authorization remained incomplete for {len(self._pending_control_actions)} action(s)")
        if self._pending_screenshot_action_ids:
            failures.append(
                f"explicit control-state assertions lack a later bound screenshot for {len(self._pending_screenshot_action_ids)} action(s)"
            )
        if self._armed_explicit_control_keys:
            failures.append(f"explicit control evidence was armed but not consumed for {len(self._armed_explicit_control_keys)} control(s)")
        return failures

    def _required_evidence_failures(self) -> list[str]:
        required_files = (
            "browser/console.jsonl",
            "browser/page-errors.jsonl",
            "browser/request-failures.jsonl",
            "browser/request-failure-allowances.jsonl",
            "browser/console-error-allowances.jsonl",
            "browser/control-actions.jsonl",
            "browser/trace.zip",
            "network/http.jsonl",
            "network/navigation.jsonl",
            "network/sse.jsonl",
            "network/sse-timing.jsonl",
            "services/app.log",
            "services/compose.log",
            "state/db-summary.json",
            "state/files.sha256",
            "state/postgres-identity.json",
            "state/fixture-files.sha256.json",
            "security/screenshot-attestations.jsonl",
            "security/trace-attestations.jsonl",
        )
        failures = [
            f"required evidence is missing: {relative}"
            for relative in required_files
            if (self.case_dir / relative).is_symlink() or not (self.case_dir / relative).is_file()
        ]
        for relative in required_files:
            path = self.case_dir / relative
            if not path.is_symlink() and path.is_file() and path.lstat().st_nlink != 1:
                failures.append(f"required evidence must not be hardlinked: {relative}")

        def load_json(relative: str):
            path = self.case_dir / relative
            if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                failures.append(f"required JSON evidence is malformed: {relative}")
                return None

        jsonl_require_rows = {
            "network/http.jsonl",
            "security/screenshot-attestations.jsonl",
            "security/trace-attestations.jsonl",
        }
        for relative in (item for item in required_files if item.endswith(".jsonl")):
            path = self.case_dir / relative
            if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
                continue
            try:
                lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                rows = [json.loads(line) for line in lines]
            except (OSError, UnicodeError, json.JSONDecodeError):
                failures.append(f"required JSONL evidence is malformed: {relative}")
                continue
            if any(not isinstance(row, dict) for row in rows):
                failures.append(f"required JSONL evidence contains a non-object row: {relative}")
            if relative in jsonl_require_rows and not rows:
                failures.append(f"required JSONL evidence contains no rows: {relative}")

        for relative in ("services/app.log", "services/compose.log"):
            path = self.case_dir / relative
            if not path.is_symlink() and path.is_file() and path.lstat().st_nlink == 1:
                try:
                    if not path.read_text(encoding="utf-8", errors="replace").strip():
                        failures.append(f"required service evidence is empty: {relative}")
                except OSError:
                    failures.append(f"required service evidence is unreadable: {relative}")

        db_summary = load_json("state/db-summary.json")
        if isinstance(db_summary, dict):
            evidence_kind = str(db_summary.get("evidence_kind") or "").strip()
            payload_values = [value for key, value in db_summary.items() if key != "evidence_kind"]
            if not evidence_kind or not any(value not in (None, "", [], {}) for value in payload_values):
                failures.append("database summary lacks an evidence kind or non-empty correlated payload")
        elif db_summary is not None:
            failures.append("database summary must be a JSON object")

        postgres_identity = load_json("state/postgres-identity.json")
        if isinstance(postgres_identity, dict):
            required_identity = ("address", "compose_project", "database", "port", "user")
            if not all(postgres_identity.get(key) not in (None, "") for key in required_identity):
                failures.append("Postgres identity evidence is incomplete")
        elif postgres_identity is not None:
            failures.append("Postgres identity evidence must be a JSON object")

        def hash_ledger(payload, relative: str) -> dict[str, str] | None:
            rows: dict[str, str] = {}
            if isinstance(payload, dict):
                candidates = [(path, digest, None) for path, digest in payload.items()]
            elif isinstance(payload, list):
                candidates = [(row.get("path"), row.get("sha256"), row.get("size")) for row in payload if isinstance(row, dict)]
                if len(candidates) != len(payload):
                    failures.append(f"file hash ledger contains a non-object row: {relative}")
                    return None
            else:
                failures.append(f"file hash ledger must be an object or list: {relative}")
                return None
            for raw_path, raw_digest, size in candidates:
                name = str(raw_path or "")
                digest = str(raw_digest or "")
                valid_size = size is None or (isinstance(size, int) and not isinstance(size, bool) and size >= 0)
                if not name or re.fullmatch(r"[0-9a-f]{64}", digest) is None or not valid_size:
                    failures.append(f"file hash ledger contains an invalid row: {relative}")
                    return None
                rows[name] = digest
            if not rows or len(rows) != len(candidates):
                failures.append(f"file hash ledger is empty or has duplicate paths: {relative}")
                return None
            return rows

        files_ledger = hash_ledger(load_json("state/files.sha256"), "state/files.sha256")
        fixture_ledger = hash_ledger(load_json("state/fixture-files.sha256.json"), "state/fixture-files.sha256.json")
        if files_ledger is not None and fixture_ledger is not None and files_ledger != fixture_ledger:
            failures.append("case file hash ledger differs from the isolated fixture copy ledger")

        assert_no_mount_targets(self.case_dir)
        screenshots = sorted(self.case_dir.rglob("*.png"))
        if not screenshots:
            failures.append("required evidence is missing: semantic screenshot")
        for screenshot in screenshots:
            relative = str(screenshot.relative_to(self.case_dir))
            if screenshot.is_symlink():
                failures.append(f"screenshot evidence must not be a symlink: {relative}")
                continue
            if screenshot.lstat().st_nlink != 1:
                failures.append(f"screenshot evidence must not be hardlinked: {relative}")
                continue
            semantic_name = screenshot.name.partition("__")[2]
            feature_prefix = semantic_name.split("-", 1)[0]
            if _SCREENSHOT_FILENAME.fullmatch(screenshot.name) is None or feature_prefix not in _SCREENSHOT_FEATURE_PREFIXES:
                failures.append(f"screenshot name must identify sequence, feature, and state: {relative}")
            matching = [row for row in self.screenshot_attestations if row.get("path") == relative]
            if len(matching) != 1:
                failures.append(f"screenshot security attestation count is {len(matching)}: {relative}")
                continue
            if not self._valid_screenshot_attestation(screenshot, matching[0]):
                failures.append(f"screenshot security attestation is invalid: {relative}")
            if not self._valid_sidecar(self._screenshot_sidecar(screenshot), matching[0]):
                failures.append(f"screenshot atomic sidecar is missing or invalid: {relative}")
        unexpected_attestations = {str(row.get("path") or "") for row in self.screenshot_attestations} - {
            str(path.relative_to(self.case_dir)) for path in screenshots
        }
        for relative in sorted(unexpected_attestations):
            failures.append(f"screenshot security attestation has no PNG: {relative or '<empty>'}")
        if not self.trace_attestations:
            failures.append("required evidence is missing: trace screenshot policy attestation")
        elif not all(
            row.get("embedded_screenshots") is False
            and row.get("dom_snapshots_enabled") is False
            and row.get("snapshot_records_sanitized") is True
            and row.get("screencast_record_count") == 0
            and row.get("opaque_resource_members_retained") == 0
            for row in self.trace_attestations
        ):
            failures.append("trace screenshot policy attestation is invalid")
        trace_paths = sorted((self.case_dir / "browser").glob("trace*.zip"))
        attested_traces = {str(row.get("trace") or ""): row for row in self.trace_attestations}
        for trace_path in trace_paths:
            relative = str(trace_path.relative_to(self.case_dir))
            if trace_path.is_symlink() or trace_path.lstat().st_nlink != 1:
                failures.append(f"trace evidence failed symlink/hardlink boundaries: {relative}")
                continue
            row = attested_traces.get(relative)
            if row is None or row.get("sha256") != hashlib.sha256(trace_path.read_bytes()).hexdigest():
                failures.append(f"trace security attestation is missing or stale: {relative}")
            elif not self._valid_sidecar(self._trace_sidecar(trace_path), row):
                failures.append(f"trace atomic sidecar is missing or invalid: {relative}")
        unexpected_trace_attestations = set(attested_traces) - {str(path.relative_to(self.case_dir)) for path in trace_paths}
        for relative in sorted(unexpected_trace_attestations):
            failures.append(f"trace security attestation has no trace archive: {relative or '<empty>'}")
        if self.sse_collections:
            if any(row.get("status") != 200 for row in self.sse_collections):
                failures.append("independent SSE collection did not return HTTP 200")
            empty = [row for row in self.sse_collections if int(row.get("event_count") or 0) == 0]
            if empty:
                failures.append(f"independent SSE collection produced no raw event for {len(empty)} stream(s)")
            if len(self.sse_events) != len(self.sse_timings):
                failures.append("canonical SSE event and receipt-timing counts differ")
            streamed_turns = {str(row.get("turn_id") or "") for row in self.sse_collections}
            streamed_turns.discard("")
            correlated_turns = {str(row.get("turn_id") or "") for row in self.database_correlations}
            missing_db = sorted(streamed_turns - correlated_turns)
            if missing_db:
                failures.append(
                    "independent SSE turn has no direct Postgres correlation: "
                    + ", ".join(hashlib.sha256(value.encode()).hexdigest()[:12] for value in missing_db)
                )
        return failures

    @staticmethod
    def _valid_sidecar(path: Path, expected: dict) -> bool:
        if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
            return False
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return actual == expected

    def _sanitize_trace(self, source: Path, target: Path) -> None:
        screencast_records = 0
        discarded_resource_members = 0
        retained_members = 0
        try:
            with (
                zipfile.ZipFile(source, "r") as zin,
                zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout,
            ):
                for info in zin.infolist():
                    if info.filename.startswith("resources/"):
                        discarded_resource_members += 1
                        continue
                    if not info.filename.endswith((".trace", ".network", ".stacks", ".version")):
                        discarded_resource_members += 1
                        continue
                    data = zin.read(info.filename)
                    if info.filename.endswith((".trace", ".network", ".stacks")):
                        data = self._redact_trace_records(data)
                    if info.filename.endswith(".trace"):
                        screencast_records += self._count_trace_screencast_records(data)
                    for secret in sorted(self._secret_values, key=len, reverse=True):
                        data = data.replace(secret.encode("utf-8"), b"<redacted>")
                    zout.writestr(info, data)
                    retained_members += 1
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            source.unlink(missing_ok=True)
        if screencast_records:
            target.unlink(missing_ok=True)
            self.write_json(
                "security/trace-screenshot-policy-failed.json",
                {"discarded": True, "screencast_record_count": screencast_records},
            )
            raise AssertionError("trace contained embedded screenshot records and was discarded")
        leaked_entries = []
        with zipfile.ZipFile(target, "r") as trace:
            for name in trace.namelist():
                sample = trace.read(name).decode("utf-8", errors="ignore")
                if (
                    any(pattern.search(sample) for pattern in _SECRET_PATTERNS)
                    or _TRACE_SECRET_HEADER_PAIR.search(sample)
                    or _TRACE_SECRET_HEADER_PAIR_REVERSED.search(sample)
                    or any(secret in sample for secret in self._secret_values)
                ):
                    leaked_entries.append(name)
        if leaked_entries:
            target.unlink(missing_ok=True)
            self.write_json(
                "security/trace-redaction-failed.json",
                {"discarded": True, "entry_count": len(leaked_entries), "entries": leaked_entries},
            )
            raise AssertionError(f"trace contained secret-like material in {len(leaked_entries)} entries")
        attestation = {
            "trace": str(target.relative_to(self.case_dir)),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "embedded_screenshots": False,
            "screencast_record_count": 0,
            "dom_snapshots_enabled": False,
            "snapshot_records_sanitized": True,
            "opaque_resource_members_retained": 0,
            "discarded_resource_member_count": discarded_resource_members,
            "retained_metadata_member_count": retained_members,
            "policy": "metadata-only trace; DOM snapshots and opaque resource members disabled",
        }
        self.trace_attestations.append(attestation)
        try:
            self._persist_trace_attestation(target, attestation)
        except Exception:
            self.trace_attestations.pop()
            target.unlink(missing_ok=True)
            raise

    @staticmethod
    def _count_trace_screencast_records(data: bytes) -> int:
        count = 0
        for raw in data.decode("utf-8", errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                if '"type":"screencast-frame"' in raw or '"type": "screencast-frame"' in raw:
                    count += 1
                continue
            if isinstance(row, dict) and row.get("type") == "screencast-frame":
                count += 1
        return count

    def _valid_screenshot_attestation(self, path: Path, row: dict) -> bool:
        if path.is_symlink() or not path.is_file() or not isinstance(row, dict):
            return False
        count_keys = (
            "known_match_count",
            "pattern_match_count",
            "sensitive_field_count",
            "detected_element_count",
            "masked_element_count",
            "pre_capture_known_match_count",
            "pre_capture_pattern_match_count",
            "initial_detected_element_count",
            "initial_masked_element_count",
            "pre_capture_dom_marked_element_count",
            "post_capture_dom_marked_element_count",
            "opaque_pixel_surface_count",
            "pseudo_content_match_count",
            "shadow_root_count",
            "uninspectable_custom_element_count",
            "closed_shadow_host_count",
            "open_shadow_masked_element_count",
            "unmasked_visible_element_count",
            "inline_mask_failure_count",
        )
        if not all(isinstance(row.get(key), int) and row[key] >= 0 for key in count_keys):
            return False
        if row.get("scan_completed") is not True or row.get("capture_succeeded") is not True:
            return False
        if not isinstance(row.get("captured_at_epoch_seconds"), (int, float)):
            return False
        page_path = row.get("page_path")
        viewport = row.get("viewport")
        authorization = row.get("authorization_observation")
        pre_capture = row.get("pre_capture_context")
        post_capture = row.get("post_capture_context")
        page_id = row.get("page_id")
        control_action_ids = row.get("control_action_ids")
        png_dimensions = row.get("png_dimensions")
        if not isinstance(page_path, str) or not page_path.startswith("/"):
            return False
        if not isinstance(viewport, dict) or not all(
            isinstance(viewport.get(key), int) and viewport[key] > 0 for key in ("width", "height")
        ):
            return False
        if not _is_valid_control_authorization_observation(authorization):
            return False
        role = authorization["role"]
        status = authorization["status"]
        if role in {"admin", "user"} and not 200 <= status < 300:
            return False
        if role == "anonymous" and status not in {401, 403}:
            return False
        if authorization["auth_disabled"] and (role != "admin" or not 200 <= status < 300):
            return False
        expected_class = "narrow" if viewport["width"] <= 500 else "desktop" if viewport["width"] >= 1024 else "intermediate"
        if row.get("viewport_class") != expected_class:
            return False
        if not isinstance(png_dimensions, dict) or not all(
            isinstance(png_dimensions.get(key), int) and png_dimensions[key] > 0 for key in ("width", "height")
        ):
            return False
        if png_dimensions["width"] < viewport["width"] or png_dimensions["height"] < viewport["height"]:
            return False
        if not isinstance(row.get("full_page"), bool):
            return False
        if not isinstance(pre_capture, dict) or not isinstance(post_capture, dict):
            return False
        if post_capture != {
            "page_path": page_path,
            "page_id": page_id,
            "viewport": viewport,
            "authorization": authorization,
        }:
            return False
        if self._stable_capture_context(pre_capture) != self._stable_capture_context(post_capture):
            return False
        if not isinstance(page_id, str) or not page_id.startswith("browser-page-"):
            return False
        if (
            not isinstance(control_action_ids, list)
            or len(control_action_ids) != len(set(control_action_ids))
            or not all(isinstance(item, str) and re.fullmatch(r"control-action-\d{6,}", item) for item in control_action_ids)
        ):
            return False
        if any(context.get("page_id") != page_id for context in (pre_capture, post_capture)):
            return False
        correlations: set[str] = set()
        for context in (pre_capture, post_capture):
            context_authorization = context.get("authorization")
            if not _is_valid_control_authorization_observation(context_authorization):
                return False
            correlation_id = str(context_authorization.get("evidence_correlation_id") or "")
            if not re.fullmatch(r"screenshot-role-\d{10,14}-\d+", correlation_id):
                return False
            if not self._screenshot_probe_http_correlated(context):
                return False
            correlations.add(correlation_id)
        if len(correlations) != 2:
            return False
        if row["masked_element_count"] < row["detected_element_count"]:
            return False
        if row["initial_masked_element_count"] < row["initial_detected_element_count"]:
            return False
        if row["unmasked_visible_element_count"] != 0 or row["inline_mask_failure_count"] != 0:
            return False
        if row["pre_capture_dom_marked_element_count"] < row["initial_detected_element_count"]:
            return False
        if row["post_capture_dom_marked_element_count"] < row["detected_element_count"]:
            return False
        if row.get("mutation_observer_enabled_during_capture") is not True:
            return False
        return row.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest()

    def _screenshot_probe_http_correlated(self, context: dict) -> bool:
        authorization = context.get("authorization") if isinstance(context, dict) else None
        if not isinstance(authorization, dict):
            return False
        correlation_id = str(authorization.get("evidence_correlation_id") or "")
        rows = [
            row
            for row in self.http
            if row.get("evidence_probe") == "screenshot-role-v1" and row.get("evidence_correlation_id") == correlation_id
        ]
        requests = [row for row in rows if row.get("phase") == "request"]
        responses = [row for row in rows if row.get("phase") == "response"]
        failures = [row for row in rows if row.get("phase") == "requestfailed"]
        if len(requests) != 1 or len(responses) != 1 or failures:
            return False
        request, response = requests[0], responses[0]
        observed_at = authorization.get("observed_at_epoch_seconds")
        return (
            bool(request.get("request_id"))
            and response.get("request_id") == request.get("request_id")
            and request.get("page_id") == context.get("page_id") == response.get("page_id")
            and response.get("status") == authorization.get("status")
            and isinstance(observed_at, (int, float))
            and not isinstance(observed_at, bool)
            and isinstance(response.get("ts"), (int, float))
            and not isinstance(response.get("ts"), bool)
            and abs(float(response["ts"]) - float(observed_at)) <= 5
        )

    def _redact_trace_records(self, data: bytes) -> bytes:
        text = data.decode("utf-8", errors="replace")
        rows = text.splitlines(keepends=True)
        output: list[str] = []
        for row in rows:
            ending = "\n" if row.endswith("\n") else ""
            raw = row[:-1] if ending else row
            try:
                sanitized = json.dumps(self.redact(json.loads(raw)), ensure_ascii=False, separators=(",", ":"))
            except (json.JSONDecodeError, TypeError):
                sanitized = self.redact(raw)
            output.append(str(sanitized) + ending)
        return "".join(output).encode("utf-8")

    def _scan_for_secrets(self) -> list[str]:
        removed: list[str] = []
        assert_no_mount_targets(self.case_dir)
        for path in sorted(self.case_dir.rglob("*")):
            if path.is_symlink():
                removed.append(str(path.relative_to(self.case_dir)))
                assert_no_mount_targets(self.case_dir)
                path.unlink(missing_ok=True)
                continue
            if not path.is_file() or path.name == "leak-report.json":
                continue
            if path.suffix.lower() == ".png":
                relative = str(path.relative_to(self.case_dir))
                matching = [row for row in self.screenshot_attestations if row.get("path") == relative]
                if len(matching) == 1 and self._valid_screenshot_attestation(path, matching[0]):
                    continue
                removed.append(relative)
                assert_no_mount_targets(self.case_dir)
                path.unlink(missing_ok=True)
                continue
            raw = path.read_bytes()
            sample = raw.decode("utf-8", errors="ignore")
            if (
                any(pattern.search(sample) for pattern in _SECRET_PATTERNS)
                or _TRACE_SECRET_HEADER_PAIR.search(sample)
                or _TRACE_SECRET_HEADER_PAIR_REVERSED.search(sample)
                or any(secret in sample for secret in self._secret_values)
            ):
                removed.append(str(path.relative_to(self.case_dir)))
                assert_no_mount_targets(self.case_dir)
                path.unlink(missing_ok=True)
        return removed


def case_directory(root: Path, profile: str, nodeid: str) -> Path:
    path_text, _, test_name = nodeid.partition("::")
    parts = Path(path_text).parts
    feature = "misc"
    if "cases" in parts:
        idx = parts.index("cases")
        if idx + 1 < len(parts):
            feature = parts[idx + 1]
    case_id = test_name or Path(path_text).stem
    feature = re.sub(r"[^A-Za-z0-9._-]+", "-", feature)
    case_id = re.sub(r"[^A-Za-z0-9._-]+", "-", case_id)
    return root / profile / feature / case_id
