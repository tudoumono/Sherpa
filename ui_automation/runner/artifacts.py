"""証跡の生成、秘密除去、漏えい時の隔離、保持世代管理。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import struct
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import fcntl

from ui_automation.runner.filesystem_safety import (
    HardlinkBoundaryViolation,
    MountBoundaryViolation,
    assert_no_mount_targets,
    assert_no_unsafe_hardlinks,
    chmod_path_no_follow,
    chmod_tree_no_follow,
    ensure_directory_no_follow,
    rmtree_no_follow,
    unlink_runtime_control_socket_no_follow,
)


_SECRET_KEY = re.compile(
    r"(?i)(authorization|cookie|password|passwd|api[_-]?key|secret|bearer|private[_-]?key|"
    r"session[_-]?(?:id|token)|access[_-]?(?:token|key)|^(?:home|codex_home)$)"
)
_TEXT_SUFFIXES = {
    ".css",
    ".csv",
    ".env",
    ".html",
    ".js",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".py",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_RAW_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("bearer_token", re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("openai_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{12,}")),
    ("google_key", re.compile(rb"\bAIza[A-Za-z0-9_-]{12,}")),
    ("authorization_header", re.compile(rb"(?i)\bAuthorization\s*[:=]\s*[^\r\n]{8,}")),
    ("cookie_header", re.compile(rb"(?i)\b(?:Set-)?Cookie\s*:\s*[^\r\n]{8,}")),
    (
        "url_userinfo",
        re.compile(rb"(?i)\b[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@"),
    ),
    (
        "named_secret_value",
        re.compile(
            rb'(?is)"name"\s*:\s*"(?:cookie|set-cookie|authorization|password|api[_-]?key|token)"'
            rb'(?:(?!\}\s*[,\]]).){0,4096}?"value"\s*:\s*"(?!<redacted>")[^"\r\n]{4,}"'
        ),
    ),
    (
        "secret_value_named",
        re.compile(
            rb'(?is)"value"\s*:\s*"(?!<redacted>")[^"\r\n]{4,}"'
            rb'(?:(?!\}\s*[,\]]).){0,4096}?"name"\s*:\s*"(?:cookie|set-cookie|authorization|password|api[_-]?key|token)"'
        ),
    ),
)


def _looks_secret_key(name: str) -> bool:
    return bool(_SECRET_KEY.search(name))


def _safe_secret_metadata_scalar(name: str, value: Any) -> bool:
    """Permit only validated non-secret metadata whose key contains a secret word."""

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


def safe_url(value: str) -> str:
    """URL の認証情報とquery値を保存しない表示へ変換する。"""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return "<invalid-url>"
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port:
        host = f"{host}:{port}"
    query = "<redacted>" if parts.query else ""
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


def write_private_bytes_atomic(path: Path, value: bytes) -> None:
    """Replace a private artifact without mutating an existing inode."""

    ensure_directory_no_follow(path.parent, mode=0o700, require_owner_uid=os.geteuid())
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(path.parent, parent_flags)
    descriptor = -1
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        opened_parent = os.fstat(parent_descriptor)
        assert_no_mount_targets(path.parent)
        current_parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or opened_parent.st_uid != os.geteuid()
            or (opened_parent.st_dev, opened_parent.st_ino) != (current_parent.st_dev, current_parent.st_ino)
        ):
            raise PermissionError("artifact parent changed or is not runner-owned")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path.name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)


def write_private_text_atomic(path: Path, value: str) -> None:
    write_private_bytes_atomic(path, value.encode("utf-8"))


def read_private_json_no_follow(path: Path, *, maximum_bytes: int = 1024 * 1024) -> Any:
    """Read private JSON from the same single-link inode that was inspected."""

    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > maximum_bytes
    ):
        raise PermissionError("private JSON failed type, owner, link-count, mode, or size validation")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > maximum_bytes
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise PermissionError("private JSON changed or failed opened-inode validation")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise PermissionError("private JSON exceeds the safety limit")
            chunks.append(chunk)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise PermissionError("private JSON changed while it was read")
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermissionError("private JSON is not valid UTF-8 JSON") from exc
    finally:
        os.close(descriptor)


def append_private_text(path: Path, value: str) -> None:
    """Append by replacing the file, never by mutating its published inode."""

    ensure_directory_no_follow(path.parent, mode=0o700, require_owner_uid=os.geteuid())
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(path.parent, parent_flags)
    source_descriptor = -1
    temporary_descriptor = -1
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        parent_metadata = os.fstat(parent_descriptor)
        assert_no_mount_targets(path.parent)
        current_parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
            or (parent_metadata.st_dev, parent_metadata.st_ino) != (current_parent.st_dev, current_parent.st_ino)
        ):
            raise PermissionError("private append parent changed or is not runner-owned")
        # Directory flock serializes every cooperative registry writer while
        # keeping the lock itself outside the hardlink attack surface.
        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
        source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        source_descriptor = os.open(path.name, source_flags, dir_fd=parent_descriptor)
        metadata = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PermissionError("private append target failed inode, owner, link-count, or mode validation")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(source_descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 2 * 1024 * 1024:
                raise PermissionError("private append target exceeds the safety limit")
            chunks.append(chunk)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PermissionError("private append target changed while it was read")
        temporary_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        temporary_descriptor = os.open(temporary_name, temporary_flags, 0o600, dir_fd=parent_descriptor)
        os.fchmod(temporary_descriptor, 0o600)
        payload = b"".join(chunks) + value.encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(temporary_descriptor, payload[offset:])
            if written <= 0:
                raise OSError("private append made no progress")
            offset += written
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        # Recheck immediately before replacement so concurrent, non-cooperative
        # changes fail without losing registry entries.
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise PermissionError("private append target changed before replacement")
        os.replace(temporary_name, path.name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        fcntl.flock(parent_descriptor, fcntl.LOCK_UN)
        os.close(parent_descriptor)


class SecretRedactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._lock = threading.RLock()
        self._secrets: tuple[str, ...] = ()
        self.add_secrets(secrets)

    def add_secrets(self, secrets: Iterable[str]) -> int:
        cleaned = {str(value) for value in secrets if value and len(str(value)) >= 4}
        with self._lock:
            before = set(self._secrets)
            combined = before | cleaned
            self._secrets = tuple(sorted(combined, key=len, reverse=True))
            return len(combined - before)

    def known_secrets(self) -> tuple[str, ...]:
        with self._lock:
            return self._secrets

    def redact_text(self, text: str) -> str:
        result = text
        with self._lock:
            secrets_snapshot = self._secrets
        for value in secrets_snapshot:
            result = result.replace(value, "<redacted>")
        result = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1<redacted>", result)
        result = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "<redacted>", result)
        secret_assignment = (
            r'(?i)(["\']?(?:password|passwd|api[_-]?key|secret|access[_-]?token|authorization|cookie)'
            r'["\']?\s*[:=]\s*)'
        )
        # Preserve JSON structure.  In particular, an evidence object such as
        # ``"authorization": {"role": "admin"}`` is a non-secret shape and
        # must not be turned into invalid JSON by replacing its opening brace.
        result = re.sub(
            secret_assignment + r'(["\'])([^"\']*)\2',
            r"\1\2<redacted>\2",
            result,
        )
        result = re.sub(
            secret_assignment + r'(?![\{\[])[^\s,"\'}\]]+',
            r'\1"<redacted>"',
            result,
        )
        result = re.sub(
            r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@",
            r"\1<redacted>@",
            result,
        )
        return result

    def redact_value(self, value: Any, *, key: str = "") -> Any:
        if key and _looks_secret_key(key) and not isinstance(value, (dict, list, tuple)):
            # Schema metadata such as GeneratedEnvScenario.secret is a boolean
            # classification flag, not a credential.  Collapsing False to the
            # truthy string "set" weakens downstream value-equality checks.
            # Booleans cannot carry secret material, so preserve their type.
            if _safe_secret_metadata_scalar(key, value):
                return value
            if value is None or value == "":
                return "unset"
            return "set"
        if isinstance(value, dict):
            return {str(k): self.redact_value(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_value(item) for item in value]
        if isinstance(value, str):
            if "://" in value:
                return self.redact_text(safe_url(value))
            return self.redact_text(value)
        return value

    def write_json(self, path: Path, value: Any) -> None:
        write_private_text_atomic(
            path,
            json.dumps(self.redact_value(value), ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        )

    def redact_text_files(self, root: Path) -> None:
        assert_no_mount_targets(root)
        assert_no_unsafe_hardlinks(root)
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            try:
                original = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            redacted: str
            if path.suffix.lower() == ".json":
                try:
                    parsed = json.loads(original)
                except json.JSONDecodeError:
                    redacted = self.redact_text(original)
                else:
                    redacted = json.dumps(
                        self.redact_value(parsed),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                        default=str,
                    ) + ("\n" if original.endswith("\n") else "")
            elif path.suffix.lower() == ".jsonl":
                lines = original.splitlines()
                parsed_lines: list[Any] = []
                try:
                    for line in lines:
                        if line.strip():
                            parsed_lines.append(json.loads(line))
                except json.JSONDecodeError:
                    redacted = self.redact_text(original)
                else:
                    redacted = "\n".join(
                        json.dumps(self.redact_value(value), ensure_ascii=False, sort_keys=True, default=str) for value in parsed_lines
                    ) + ("\n" if original.endswith("\n") else "")
            else:
                redacted = self.redact_text(original)
            if redacted != original:
                write_private_text_atomic(path, redacted)
            chmod_path_no_follow(path, 0o600, require_owner_uid=os.geteuid())

    def _matches(self, data: bytes) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        for label, pattern in _RAW_PATTERNS:
            count = len(pattern.findall(data))
            if count:
                found.append((label, count))
        with self._lock:
            secrets_snapshot = self._secrets
        for value in secrets_snapshot:
            count = data.count(value.encode("utf-8"))
            if count:
                found.append(("known_secret", count))
        return found

    def _zip_matches(self, path: Path) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.file_size > 100 * 1024 * 1024:
                        found.append(("oversized_unscannable_zip_member", 1))
                        continue
                    for label, count in self._matches(archive.read(info)):
                        found.append((f"zip:{label}", count))
        except (OSError, zipfile.BadZipFile, RuntimeError):
            found.append(("unscannable_zip", 1))
        return found

    @staticmethod
    def _binary_sidecar(path: Path) -> tuple[Path, str] | None:
        if path.suffix.lower() == ".png" and path.parent.name == "screenshots":
            case_root = path.parent.parent
            return case_root / "security" / "screenshot-sidecars" / f"{path.name}.json", "screenshot"
        if path.suffix.lower() == ".zip" and path.parent.name == "browser" and path.name.startswith("trace"):
            case_root = path.parent.parent
            return case_root / "security" / "trace-sidecars" / f"{path.name}.json", "trace"
        return None

    @classmethod
    def _binary_attestation_matches(cls, path: Path) -> bool:
        resolved = cls._binary_sidecar(path)
        if resolved is None:
            return True
        sidecar, kind = resolved
        if sidecar.is_symlink() or not sidecar.is_file():
            return False
        try:
            metadata = sidecar.stat()
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, json.JSONDecodeError):
            return False
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            return False
        if not isinstance(payload, dict) or payload.get("sha256") != digest:
            return False
        case_root = path.parent.parent
        relative = str(path.relative_to(case_root))
        if kind == "trace":
            return (
                payload.get("trace") == relative
                and payload.get("embedded_screenshots") is False
                and payload.get("dom_snapshots_enabled") is False
                and payload.get("opaque_resource_members_retained") == 0
                and payload.get("screencast_record_count") == 0
            )
        if payload.get("path") != relative or payload.get("scan_completed") is not True:
            return False
        if payload.get("capture_succeeded") is not True or payload.get("mutation_observer_enabled_during_capture") is not True:
            return False
        authorization = payload.get("authorization_observation")
        pre_capture = payload.get("pre_capture_context")
        post_capture = payload.get("post_capture_context")
        page_id = payload.get("page_id")
        control_action_ids = payload.get("control_action_ids")
        viewport = payload.get("viewport")
        png_dimensions = payload.get("png_dimensions")
        if (
            not isinstance(authorization, dict)
            or not isinstance(pre_capture, dict)
            or not isinstance(post_capture, dict)
            or not isinstance(viewport, dict)
            or not isinstance(png_dimensions, dict)
            or not isinstance(page_id, str)
            or not page_id.startswith("browser-page-")
            or not isinstance(control_action_ids, list)
            or len(control_action_ids) != len(set(control_action_ids))
            or not all(isinstance(item, str) and re.fullmatch(r"control-action-\d{6,}", item) for item in control_action_ids)
        ):
            return False
        role = authorization.get("role")
        status = authorization.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or not isinstance(authorization.get("auth_disabled"), bool):
            return False
        if role in {"admin", "user"} and not 200 <= status < 300:
            return False
        if role == "anonymous" and status not in {401, 403}:
            return False
        if role not in {"admin", "user", "anonymous"}:
            return False
        if authorization.get("auth_disabled") is True and (role != "admin" or not 200 <= status < 300):
            return False
        if post_capture != {
            "page_path": payload.get("page_path"),
            "page_id": page_id,
            "viewport": viewport,
            "authorization": authorization,
        }:
            return False
        if any(context.get("page_id") != page_id for context in (pre_capture, post_capture)):
            return False
        if any(context.get("page_path") != payload.get("page_path") for context in (pre_capture, post_capture)):
            return False
        if any(context.get("viewport") != viewport for context in (pre_capture, post_capture)):
            return False
        stable_authorizations = []
        correlations: list[str] = []
        for context in (pre_capture, post_capture):
            context_authorization = context.get("authorization")
            if not isinstance(context_authorization, dict):
                return False
            stable_authorizations.append(tuple(context_authorization.get(key) for key in ("status", "role", "auth_disabled")))
            correlation_id = str(context_authorization.get("evidence_correlation_id") or "")
            observed_at = context_authorization.get("observed_at_epoch_seconds")
            if (
                re.fullmatch(r"screenshot-role-\d{10,14}-\d+", correlation_id) is None
                or not isinstance(observed_at, (int, float))
                or isinstance(observed_at, bool)
            ):
                return False
            correlations.append(correlation_id)
        if stable_authorizations[0] != stable_authorizations[1] or len(set(correlations)) != 2:
            return False
        try:
            http_rows = [
                json.loads(line) for line in (case_root / "network" / "http.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
            ]
        except (OSError, json.JSONDecodeError):
            return False
        if control_action_ids:
            try:
                action_rows = [
                    json.loads(line)
                    for line in (case_root / "browser" / "control-actions.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, json.JSONDecodeError):
                return False
            available_action_ids = {str(row.get("action_id")) for row in action_rows if isinstance(row, dict) and row.get("action_id")}
            if not set(control_action_ids).issubset(available_action_ids):
                return False
        for context, correlation_id in zip((pre_capture, post_capture), correlations, strict=True):
            rows = [
                row
                for row in http_rows
                if isinstance(row, dict)
                and row.get("evidence_probe") == "screenshot-role-v1"
                and row.get("evidence_correlation_id") == correlation_id
            ]
            requests = [row for row in rows if row.get("phase") == "request"]
            responses = [row for row in rows if row.get("phase") == "response"]
            failures = [row for row in rows if row.get("phase") == "requestfailed"]
            if len(requests) != 1 or len(responses) != 1 or failures:
                return False
            request, response = requests[0], responses[0]
            context_authorization = context["authorization"]
            response_ts = response.get("ts")
            observed_at = context_authorization["observed_at_epoch_seconds"]
            if not (
                bool(request.get("request_id"))
                and response.get("request_id") == request.get("request_id")
                and request.get("page_id") == page_id == response.get("page_id")
                and response.get("status") == context_authorization.get("status")
                and isinstance(response_ts, (int, float))
                and not isinstance(response_ts, bool)
                and abs(float(response_ts) - float(observed_at)) <= 5
            ):
                return False
        try:
            header = path.read_bytes()[:24]
            width, height = struct.unpack(">II", header[16:24])
        except (OSError, struct.error):
            return False
        return header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR" and png_dimensions == {"width": width, "height": height}

    def _collect_leaks(self, root: Path, *, quarantine: bool) -> list[dict[str, Any]]:
        assert_no_mount_targets(root)
        incidents: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.name == "leak-report.json":
                continue
            try:
                binary_sidecar = self._binary_sidecar(path)
                if binary_sidecar is not None and not self._binary_attestation_matches(path):
                    matches = [("missing_or_invalid_atomic_attestation", 1)]
                elif path.suffix.lower() == ".zip":
                    matches = self._zip_matches(path)
                else:
                    if path.stat().st_size > 100 * 1024 * 1024:
                        matches = [("oversized_unscannable_file", 1)]
                    else:
                        matches = self._matches(path.read_bytes())
            except OSError:
                matches = [("unreadable_file", 1)]
            if not matches:
                continue
            incidents.append(
                {
                    "path": str(path.relative_to(root)),
                    "categories": [{"name": label, "count": count} for label, count in matches],
                }
            )
            if quarantine:
                assert_no_mount_targets(root)
                path.unlink(missing_ok=True)
                if binary_sidecar is not None:
                    binary_sidecar[0].unlink(missing_ok=True)
        return incidents

    def scan_leaks(self, root: Path) -> list[dict[str, Any]]:
        """生成物を変更せず、秘密候補の場所と件数だけを返す。"""
        return self._collect_leaks(root, quarantine=False)

    def quarantine_leaks(self, root: Path) -> list[dict[str, Any]]:
        """秘密候補を含む生成物を削除し、値を含まない件数だけ返す。"""
        return self._collect_leaks(root, quarantine=True)


def run_artifact_security_self_check(run_root: Path, redactor: SecretRedactor) -> dict[str, Any]:
    """Exercise final JSON/JSONL redaction and atomic PNG validation contracts."""

    from ui_automation.support.artifacts import redact as case_evidence_redact

    check_root = run_root / "state" / "artifact-security-self-check"
    check_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    sensitive_value = "self-check-credential-" + hashlib.sha256(os.urandom(32)).hexdigest()
    redactor.add_secrets([sensitive_value])
    correlation = f"control-action-role-{int(time.time() * 1000)}-1"
    fixture = {
        "authorization_observed_at_epoch_seconds": 1_700_000_000.25,
        "authorization_evidence_correlation_id": correlation,
        "authorization_request_id": "browser-000001",
        "authorization_mode": "awaited-pre-action",
        "session_id_sha256": "a" * 64,
        "token_hash_sha256": "b" * 64,
        "secret_input_fields_masked": 2,
        "authorization_header": sensitive_value,
        "codex_session_id": sensitive_value,
    }
    json_path = check_root / "structure.json"
    jsonl_path = check_root / "structure.jsonl"
    write_private_text_atomic(json_path, json.dumps(fixture, ensure_ascii=False) + "\n")
    write_private_text_atomic(jsonl_path, json.dumps(fixture, ensure_ascii=False) + "\n")
    redactor.redact_text_files(check_root)
    json_value = json.loads(json_path.read_text(encoding="utf-8"))
    jsonl_value = json.loads(jsonl_path.read_text(encoding="utf-8").strip())
    for value in (json_value, jsonl_value):
        assert value["authorization_observed_at_epoch_seconds"] == fixture["authorization_observed_at_epoch_seconds"]
        assert value["authorization_evidence_correlation_id"] == correlation
        assert value["authorization_request_id"] == "browser-000001"
        assert value["authorization_mode"] == "awaited-pre-action"
        assert value["session_id_sha256"] == "a" * 64
        assert value["token_hash_sha256"] == "b" * 64
        assert value["secret_input_fields_masked"] == 2
        assert value["authorization_header"] == "set"
        assert value["codex_session_id"] == "set"
    case_redacted = json.loads(json.dumps(case_evidence_redact(fixture), ensure_ascii=False))
    assert case_redacted["authorization_observed_at_epoch_seconds"] == fixture["authorization_observed_at_epoch_seconds"]
    assert case_redacted["authorization_evidence_correlation_id"] == correlation
    assert case_redacted["authorization_request_id"] == "browser-000001"
    assert case_redacted["authorization_mode"] == "awaited-pre-action"
    assert case_redacted["session_id_sha256"] == "a" * 64
    assert case_redacted["token_hash_sha256"] == "b" * 64
    assert case_redacted["secret_input_fields_masked"] == 2
    assert case_redacted["authorization_header"] == "<redacted>"
    assert case_redacted["codex_session_id"] == "<redacted>"

    with tempfile.TemporaryDirectory(prefix="sherpa-ui-hardlink-check-", dir="/tmp") as temporary:
        temporary_root = Path(temporary)
        external = temporary_root / "external.txt"
        owned = temporary_root / "owned"
        owned.mkdir(mode=0o700)
        write_private_text_atomic(external, "outside-content\n")
        linked = owned / "linked.txt"
        os.link(external, linked)
        external_mode = stat.S_IMODE(external.stat().st_mode)
        hardlink_rejected = False
        try:
            assert_no_unsafe_hardlinks(owned)
        except HardlinkBoundaryViolation:
            hardlink_rejected = True
        assert hardlink_rejected, "recursive hardlink boundary did not reject a multiply-linked file"
        append_rejected = False
        try:
            append_private_text(linked, "must-not-append\n")
        except PermissionError:
            append_rejected = True
        assert append_rejected, "private append accepted a multiply-linked file"
        assert external.read_text(encoding="utf-8") == "outside-content\n"
        assert stat.S_IMODE(external.stat().st_mode) == external_mode
        chmod_rejected = False
        try:
            chmod_path_no_follow(linked, 0o600, require_owner_uid=os.geteuid())
        except HardlinkBoundaryViolation:
            chmod_rejected = True
        assert chmod_rejected, "opened-inode chmod accepted a multiply-linked file"
        write_private_text_atomic(linked, "replacement\n")
        assert external.read_text(encoding="utf-8") == "outside-content\n"
        assert linked.stat().st_nlink == 1
        registry = owned / "registry.jsonl"
        write_private_text_atomic(registry, '{"value":"first"}\n')
        registry_inode = registry.stat().st_ino
        append_private_text(registry, '{"value":"second"}\n')
        assert registry.stat().st_ino != registry_inode, "private append mutated its published inode"
        assert registry.read_text(encoding="utf-8").splitlines() == ['{"value":"first"}', '{"value":"second"}']

        socket_runtime = temporary_root / "socket-runtime"
        socket_control = socket_runtime / "control"
        socket_control.mkdir(parents=True, mode=0o700)
        chmod_path_no_follow(socket_runtime, 0o700, require_owner_uid=os.geteuid())
        chmod_path_no_follow(socket_control, 0o700, require_owner_uid=os.geteuid())
        runner_socket = socket_control / "runner.sock"
        replacement_socket = socket_control / "replacement.sock"
        displaced_socket = socket_control / "displaced.sock"
        os.mknod(runner_socket, stat.S_IFSOCK | 0o600)
        os.mknod(replacement_socket, stat.S_IFSOCK | 0o600)
        from ui_automation.runner import filesystem_safety

        original_rename = filesystem_safety.os.rename
        race_injected = False

        def race_rename(source, destination, *args, **kwargs):
            nonlocal race_injected
            if not race_injected and source == "runner.sock":
                race_injected = True
                original_rename(
                    "runner.sock",
                    "displaced.sock",
                    src_dir_fd=kwargs["src_dir_fd"],
                    dst_dir_fd=kwargs["dst_dir_fd"],
                )
                original_rename(
                    "replacement.sock",
                    "runner.sock",
                    src_dir_fd=kwargs["src_dir_fd"],
                    dst_dir_fd=kwargs["dst_dir_fd"],
                )
            return original_rename(source, destination, *args, **kwargs)

        filesystem_safety.os.rename = race_rename
        try:
            try:
                unlink_runtime_control_socket_no_follow(socket_runtime, require_owner_uid=os.geteuid())
            except MountBoundaryViolation:
                pass
            else:
                raise AssertionError("runtime control-socket replacement race was accepted")
        finally:
            filesystem_safety.os.rename = original_rename
        assert stat.S_ISSOCK(displaced_socket.lstat().st_mode), "original control socket was deleted during replacement race"
        assert stat.S_ISSOCK(runner_socket.lstat().st_mode), "replacement control socket was deleted during replacement race"
        displaced_socket.unlink()
        runner_socket.unlink()
        os.mknod(runner_socket, stat.S_IFSOCK | 0o600)
        assert unlink_runtime_control_socket_no_follow(socket_runtime, require_owner_uid=os.geteuid())
        assert not runner_socket.exists(), "validated runtime control socket remains after cleanup"

    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
        "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
    )
    with tempfile.TemporaryDirectory(prefix="sherpa-ui-attestation-", dir="/tmp") as temporary:
        case_root = Path(temporary) / "profile" / "smoke" / "case"
        screenshot_dir = case_root / "screenshots"
        sidecar_dir = case_root / "security" / "screenshot-sidecars"
        network_dir = case_root / "network"
        browser_dir = case_root / "browser"
        for directory in (screenshot_dir, sidecar_dir, network_dir, browser_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        screenshot = screenshot_dir / "001__security-self-check.png"
        write_private_bytes_atomic(screenshot, png_bytes)
        now = time.time()
        correlations = [
            f"screenshot-role-{int(now * 1000)}-1",
            f"screenshot-role-{int(now * 1000)}-2",
        ]
        contexts = []
        http_rows = []
        for index, screenshot_correlation in enumerate(correlations, 1):
            observed_at = now + index / 100
            contexts.append(
                {
                    "page_path": "/ui/home.html",
                    "page_id": "browser-page-001",
                    "viewport": {"width": 1440, "height": 1000},
                    "authorization": {
                        "status": 200,
                        "role": "admin",
                        "auth_disabled": False,
                        "observed_at_epoch_seconds": observed_at,
                        "evidence_correlation_id": screenshot_correlation,
                    },
                }
            )
            request_id = f"browser-{index:06d}"
            common = {
                "ts": observed_at,
                "page_id": "browser-page-001",
                "request_id": request_id,
                "method": "GET",
                "url": "http://127.0.0.1/auth/me",
                "evidence_probe": "screenshot-role-v1",
                "evidence_correlation_id": screenshot_correlation,
            }
            http_rows.extend(({**common, "phase": "request"}, {**common, "phase": "response", "status": 200}))
        network_path = network_dir / "http.jsonl"
        write_private_text_atomic(network_path, "".join(json.dumps(row) + "\n" for row in http_rows))
        write_private_text_atomic(
            browser_dir / "control-actions.jsonl",
            json.dumps({"action_id": "control-action-000001"}) + "\n",
        )
        authorization = contexts[1]["authorization"]
        sidecar = sidecar_dir / f"{screenshot.name}.json"
        attestation = {
            "path": "screenshots/001__security-self-check.png",
            "sha256": hashlib.sha256(png_bytes).hexdigest(),
            "scan_completed": True,
            "capture_succeeded": True,
            "mutation_observer_enabled_during_capture": True,
            "page_path": "/ui/home.html",
            "page_id": "browser-page-001",
            "viewport": {"width": 1440, "height": 1000},
            "png_dimensions": {"width": 1, "height": 1},
            "authorization_observation": authorization,
            "control_action_ids": ["control-action-000001"],
            "pre_capture_context": contexts[0],
            "post_capture_context": contexts[1],
        }
        write_private_text_atomic(sidecar, json.dumps(attestation))
        assert redactor._binary_attestation_matches(screenshot), "valid correlated PNG attestation was rejected"
        attestation["pre_capture_context"]["authorization"]["evidence_correlation_id"] = correlations[1]
        write_private_text_atomic(sidecar, json.dumps(attestation))
        assert not redactor._binary_attestation_matches(screenshot), "duplicate PNG role correlation was accepted"

    return {
        "status": "PASS",
        "json_structure_preserved": True,
        "jsonl_structure_preserved": True,
        "validated_metadata_preserved": True,
        "case_evidence_redaction_preserved": True,
        "secret_scalars_removed": True,
        "atomic_png_http_correlation_validated": True,
        "atomic_png_action_binding_validated": True,
        "duplicate_png_role_correlation_rejected": True,
        "hardlink_recursive_mutation_rejected": True,
        "hardlink_append_rejected_before_secret_write": True,
        "hardlink_chmod_rejected_before_mutation": True,
        "atomic_append_replaced_published_inode": True,
        "atomic_replace_preserved_external_inode": True,
        "runtime_control_socket_replacement_race_rejected": True,
        "runtime_control_socket_exact_unlink_succeeded": True,
    }


def sanitized_environment(environment: dict[str, str], redactor: SecretRedactor) -> dict[str, Any]:
    """試験に関係するenvだけをallowlist方式で証跡化する。"""
    selected: dict[str, Any] = {}
    allowed_exact = {
        "CODEX_HOME",
        "COMPOSE_PROJECT_NAME",
        "DATABASE_URL",
        "ES_URL",
        "HOME",
        "NEO4J_URI",
        "NO_PROXY",
        "OLLAMA_URL",
        "OPENAI_BASE_URL",
        "PGPORT",
        "PYTHON_BIN",
        "TMPDIR",
    }
    for key in sorted(environment):
        if not (key.startswith("SHERPA_") or key in allowed_exact):
            continue
        value = environment[key]
        selected[key] = redactor.redact_value(value, key=key)
    return selected


def file_hash_rows(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if root.is_dir():
        assert_no_mount_targets(root)
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                rows.append({"path": str(path.relative_to(root)), "sha256": digest, "size": path.stat().st_size})
    return rows


def write_file_hashes(root: Path, output: Path) -> None:
    rows = file_hash_rows(root)
    write_private_text_atomic(output, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")


def _write_run_marker(root: Path, payload: dict[str, Any]) -> None:
    marker = root / ".ui-automation-run.json"
    temporary = root / f".ui-automation-run.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(marker)
    finally:
        temporary.unlink(missing_ok=True)


def initialize_run_root(root: Path, *, run_id: str, started_at: str) -> None:
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_stat = root.parent.stat()
    if parent_stat.st_uid != os.geteuid():
        raise PermissionError("artifact root is not owned by the runner user")
    chmod_path_no_follow(root.parent, 0o700, require_owner_uid=os.geteuid())
    root.mkdir(parents=False, exist_ok=False, mode=0o700)
    chmod_path_no_follow(root, 0o700, require_owner_uid=os.geteuid())
    runner_pid = os.getpid()
    runner_start_ticks = _process_start_ticks(runner_pid)
    if runner_start_ticks is None:
        raise RuntimeError("runner process start identity is unavailable")
    _write_run_marker(
        root,
        {
            "run_id": run_id,
            "started_at": started_at,
            "status": "running",
            "pid": runner_pid,
            "pid_start_ticks": runner_start_ticks,
        },
    )


def mark_run_complete(root: Path, *, run_id: str, finished_at: str) -> None:
    """実行終了後だけmarkerをcompletedへ原子的に遷移させる。"""
    marker = root / ".ui-automation-run.json"
    if marker.is_symlink() or not marker.is_file():
        raise RuntimeError("run ownership marker is missing or unsafe")
    payload = read_private_json_no_follow(marker)
    if payload.get("run_id") != run_id or payload.get("status") != "running":
        raise RuntimeError("run ownership marker cannot transition to completed")
    if payload.get("pid") != os.getpid() or _process_start_ticks(os.getpid()) != payload.get("pid_start_ticks"):
        raise RuntimeError("run ownership marker process identity differs from the completing runner")
    payload["status"] = "completed"
    payload["finished_at"] = finished_at
    _write_run_marker(root, payload)


def audit_existing_artifact_runs(
    artifacts_root: Path,
    *,
    current_run_id: str,
    redactor: SecretRedactor,
) -> dict[str, Any]:
    """Harden legacy runs and quarantine evidence that lacks atomic attestations.

    Old markers are never promoted to ``completed``.  They receive an explicit
    ``legacy-quarantined`` status so retention can count them without treating
    them as successful evidence.
    """

    chmod_path_no_follow(artifacts_root, 0o700, require_owner_uid=os.geteuid())
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    migrated = 0
    quarantined_files = 0
    root_real = artifacts_root.resolve(strict=True)
    for path in sorted(artifacts_root.iterdir()):
        if path.name == current_run_id or path.name.startswith("."):
            continue
        if path.is_symlink():
            errors.append(f"legacy artifact symlink refused: {path.name}")
            continue
        if not path.is_dir():
            continue
        try:
            if path.resolve(strict=True).parent != root_real or path.stat().st_uid != os.geteuid():
                errors.append(f"legacy artifact ownership refused: {path.name}")
                continue
        except OSError as exc:
            errors.append(f"legacy artifact inspection failed: {path.name}: {type(exc).__name__}")
            continue
        marker = path / ".ui-automation-run.json"
        try:
            payload = read_private_json_no_follow(marker)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"legacy artifact marker unreadable: {path.name}: {type(exc).__name__}")
            continue
        if not isinstance(payload, dict) or payload.get("run_id") != path.name:
            errors.append(f"legacy artifact marker identity mismatch: {path.name}")
            continue
        original_status = payload.get("status")
        if original_status == "running":
            if _pid_is_alive(payload.get("pid"), payload.get("pid_start_ticks")):
                rows.append(
                    {
                        "run_id": path.name,
                        "status_before": "running",
                        "status_after": "running",
                        "permissions_hardened": False,
                        "quarantined_file_count": 0,
                        "active_preserved": True,
                    }
                )
                continue
            if not _valid_start_ticks(payload.get("pid_start_ticks")) and _pid_exists(payload.get("pid")):
                errors.append(f"legacy running artifact lacks PID start identity and was preserved: {path.name}")
                rows.append(
                    {
                        "run_id": path.name,
                        "status_before": "running",
                        "status_after": "running",
                        "permissions_hardened": False,
                        "quarantined_file_count": 0,
                        "active_preserved": True,
                        "identity_ambiguous": True,
                    }
                )
                continue
        if original_status is None and (
            _pid_is_alive(payload.get("pid"), payload.get("pid_start_ticks"))
            or (not _valid_start_ticks(payload.get("pid_start_ticks")) and _pid_exists(payload.get("pid")))
        ):
            errors.append(f"legacy status-less artifact has a live or ambiguous PID and was preserved: {path.name}")
            rows.append(
                {
                    "run_id": path.name,
                    "status_before": "missing",
                    "status_after": "missing",
                    "permissions_hardened": False,
                    "quarantined_file_count": 0,
                    "active_preserved": True,
                    "identity_ambiguous": not _valid_start_ticks(payload.get("pid_start_ticks")),
                }
            )
            continue
        permission_errors = harden_artifact_permissions(path)
        if permission_errors:
            errors.extend(f"{path.name}: {message}" for message in permission_errors)
            continue
        if original_status is None:
            payload["status"] = "legacy-quarantined"
            payload["legacy_reason"] = "pre-attestation run; completion state is unknown"
            _write_run_marker(path, payload)
            migrated += 1
        elif original_status == "running":
            payload["status"] = "legacy-quarantined"
            payload["legacy_reason"] = "abandoned run; recorded PID is no longer active"
            _write_run_marker(path, payload)
            migrated += 1
        elif original_status not in {"running", "completed", "legacy-quarantined"}:
            errors.append(f"legacy artifact marker has unsupported status: {path.name}")
            continue
        incidents = redactor.quarantine_leaks(path)
        quarantined_files += len(incidents)
        rows.append(
            {
                "run_id": path.name,
                "status_before": original_status or "missing",
                "status_after": payload.get("status"),
                "permissions_hardened": True,
                "quarantined_file_count": len(incidents),
            }
        )
    return {
        "status": "FAIL" if errors or migrated or quarantined_files else "PASS",
        "legacy_run_count": sum(row["status_after"] == "legacy-quarantined" for row in rows),
        "migrated_count": migrated,
        "quarantined_file_count": quarantined_files,
        "runs": rows,
        "errors": errors,
    }


def _process_start_ticks(pid: int) -> int | None:
    """Linux PID reuseを区別する/proc process identityを返す。"""

    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    closing = raw.rfind(") ")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
    try:
        # /proc/<pid>/stat field 22。fields[0]はfield 3 (state)。
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _pid_is_alive(pid: object, start_ticks: object) -> bool:
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 1
        or not isinstance(start_ticks, int)
        or isinstance(start_ticks, bool)
        or start_ticks <= 0
    ):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return _process_start_ticks(pid) == start_ticks


def _valid_start_ticks(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _pid_exists(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _retention_lock(artifacts_root: Path):
    path = artifacts_root / ".retention.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PermissionError("retention lock is not a runner-owned regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def prune_runs(
    artifacts_root: Path,
    *,
    keep: int = 10,
    protected_run_id: str | None = None,
) -> list[str]:
    """完了済み・非稼働の所有runだけをlock下で古い順に削除する。"""
    if keep < 1:
        raise ValueError("retention must keep at least one run")
    if not artifacts_root.is_dir():
        return []
    with _retention_lock(artifacts_root):
        candidates: list[tuple[Path, dict[str, Any]]] = []
        root_real = artifacts_root.resolve(strict=True)
        root_metadata = root_real.stat()
        if root_metadata.st_uid != os.geteuid() or stat.S_IMODE(root_metadata.st_mode) != 0o700:
            raise PermissionError("artifact retention root is not runner-owned mode 0700")
        for path in artifacts_root.iterdir():
            if path.is_symlink() or not path.is_dir():
                continue
            marker = path / ".ui-automation-run.json"
            if marker.is_symlink() or not marker.is_file():
                continue
            try:
                path_metadata = path.stat()
                marker_metadata = marker.stat()
                if (
                    path_metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(path_metadata.st_mode) != 0o700
                    or marker_metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(marker_metadata.st_mode) != 0o600
                ):
                    continue
                data = read_private_json_no_follow(marker)
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("run_id") != path.name or data.get("status") not in {"completed", "legacy-quarantined"}:
                continue
            if path.resolve().parent != root_real:
                continue
            if path.name != protected_run_id and _pid_is_alive(
                data.get("pid"),
                data.get("pid_start_ticks"),
            ):
                continue
            candidates.append((path, data))
        candidates.sort(key=lambda item: item[0].name, reverse=True)
        removed: list[str] = []
        for path, data in candidates[keep:]:
            # TOCTOUを狭めるため、削除直前にもmarkerとPIDを再検証する。
            marker = path / ".ui-automation-run.json"
            try:
                if path.is_symlink() or marker.is_symlink() or path.resolve(strict=True).parent != root_real:
                    continue
                path_metadata = path.stat()
                marker_metadata = marker.stat()
                if (
                    path_metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(path_metadata.st_mode) != 0o700
                    or marker_metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(marker_metadata.st_mode) != 0o600
                ):
                    continue
                current = read_private_json_no_follow(marker)
            except (OSError, json.JSONDecodeError):
                continue
            if current != data or current.get("status") not in {"completed", "legacy-quarantined"}:
                continue
            if path.name == protected_run_id or _pid_is_alive(
                current.get("pid"),
                current.get("pid_start_ticks"),
            ):
                continue
            assert_no_mount_targets(path)
            rmtree_no_follow(path)
            removed.append(path.name)
        return removed


def collect_secret_values(environment: dict[str, str]) -> list[str]:
    values: list[str] = []
    for key, value in environment.items():
        if key == "SHERPA_UI_SECRET_REGISTRY":
            continue
        normalized_key = key.strip().lower()
        path_identity = normalized_key in {"home", "codex_home"}
        if _looks_secret_key(key) and not path_identity and value and value.lower() not in {"set", "unset", "none"}:
            values.append(value)
        if value and "://" in value:
            try:
                parts = urlsplit(value)
            except ValueError:
                parts = None
            if parts is not None:
                # A DSN username such as the product's ordinary ``sherpa``
                # identity is not a credential and may legitimately occur in
                # project names, labels, logs, and trace method names.  Treating
                # it as a global substring secret quarantines unrelated evidence.
                # Full URL userinfo is still rejected by ``url_userinfo`` and
                # safe_url removes both fields; only the password is a reusable
                # known-secret value.
                if parts.password:
                    values.append(parts.password)
        if value and re.search(r"(?i)(?:^|\s)password\s*=", value):
            match = re.search(r"(?i)(?:^|\s)password\s*=\s*([^\s]+)", value)
            if match:
                values.append(match.group(1).strip("'\""))
    canary = os.environ.get("SHERPA_UI_SECRET_CANARY")
    if canary:
        values.append(canary)
    return values


def harden_artifact_permissions(root: Path) -> list[str]:
    """run所有treeをdirectory 0700/file 0600へ固定し、symlinkを拒否する。"""
    errors: list[str] = []
    if not root.exists():
        return ["artifact root is missing"]
    try:
        chmod_tree_no_follow(
            root,
            directory_mode=0o700,
            file_mode=0o600,
            allow_symlinks=False,
            require_owner_uid=os.geteuid(),
        )
    except (OSError, RuntimeError) as exc:
        return [f"artifact filesystem boundary validation failed: {type(exc).__name__}: {exc}"]
    return errors


def ingest_secret_registry(path: Path, redactor: SecretRedactor) -> tuple[int, list[str]]:
    """runner-owned 0600 registryから値だけをメモリへ取り込み、値自体は返さない。"""
    errors: list[str] = []
    try:
        metadata = path.lstat()
    except OSError as exc:
        return 0, [f"secret registry cannot be inspected: {type(exc).__name__}"]
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return 0, ["secret registry is not a regular no-symlink file"]
    if metadata.st_uid != os.getuid():
        return 0, ["secret registry owner does not match the runner"]
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        return 0, ["secret registry permissions are not exactly 0600"]
    if metadata.st_size > 2 * 1024 * 1024:
        return 0, ["secret registry exceeds the 2 MiB safety limit"]
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size > 2 * 1024 * 1024
        ):
            os.close(descriptor)
            return 0, ["secret registry changed or failed opened-inode validation"]
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except (OSError, UnicodeDecodeError) as exc:
        return 0, [f"secret registry cannot be read safely: {type(exc).__name__}"]
    values: list[str] = []
    for line_number, line in enumerate(lines, 1):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"secret registry line {line_number} is invalid JSON")
            continue
        if not isinstance(payload, dict) or set(payload) != {"value"} or not isinstance(payload["value"], str):
            errors.append(f"secret registry line {line_number} violates the value-only contract")
            continue
        value = payload["value"]
        if len(value) < 4 or len(value) > 64 * 1024:
            errors.append(f"secret registry line {line_number} has an invalid value length")
            continue
        values.append(value)
    return redactor.add_secrets(values), errors
