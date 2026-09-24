"""隔離されたCompose storeとFastAPIプロセスの起動・停止。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ui_automation.runner.artifacts import (
    SecretRedactor,
    append_private_text,
    ingest_secret_registry,
    read_private_json_no_follow,
    safe_url,
    sanitized_environment,
    write_private_text_atomic,
)
from ui_automation.runner.filesystem_safety import (
    assert_no_mount_targets,
    chmod_path_no_follow,
    chmod_tree_no_follow,
    rmtree_no_follow,
    unlink_runtime_control_socket_no_follow,
)
from ui_automation.stack.isolation import (
    COMPOSE_OWNER_LABEL,
    COMPOSE_PROJECT_LABEL,
    IsolationViolation,
    LOCAL_DOCKER_ENDPOINT,
    RUNTIME_MARKER_NAME,
    RunIsolation,
    _DOCKER_ENDPOINT_ENV,
    _compose_project_name,
    _process_start_ticks,
    local_docker_environment,
    runtime_parent_path,
    runtime_marker_payload,
    scrub_runtime_secret_files,
    validate_runtime_marker,
    validate_runtime_marker_contract,
    verify_local_docker_environment,
)


class StackFailure(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _runtime_parent() -> Path:
    return runtime_parent_path()


def _runtime_marker_is_active(payload: dict[str, object]) -> bool:
    pid = payload.get("creator_pid")
    ticks = payload.get("creator_start_ticks")
    return (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and isinstance(ticks, int)
        and not isinstance(ticks, bool)
        and _process_start_ticks(pid) == ticks
    )


def _legacy_artifact_run_is_active(artifacts_root: Path, run_id: str) -> bool:
    marker = artifacts_root / run_id / ".ui-automation-run.json"
    try:
        if marker.is_symlink() or not marker.is_file():
            return True
        payload = read_private_json_no_follow(marker)
    except (OSError, ValueError):
        # Unknown legacy ownership is active for cleanup purposes: preserve it.
        return True
    if not isinstance(payload, dict) or payload.get("run_id") != run_id or payload.get("status") != "running":
        return True
    pid = payload.get("pid")
    ticks = payload.get("pid_start_ticks")
    if isinstance(pid, int) and not isinstance(pid, bool) and isinstance(ticks, int) and not isinstance(ticks, bool):
        return _process_start_ticks(pid) == ticks
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return True
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _read_private_regular_file(path: Path, *, description: str) -> str:
    """Read one private file from the same inode that passed validation."""

    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise IsolationViolation(f"{description} is not a runner-owned 0600 regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or opened.st_size > 64 * 1024
        ):
            raise IsolationViolation(f"{description} changed or failed opened-inode validation")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            total += len(chunk)
            if total > 64 * 1024:
                raise IsolationViolation(f"{description} exceeds the safety limit")
            chunks.append(chunk)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise IsolationViolation(f"{description} changed while it was read")
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IsolationViolation(f"{description} is not UTF-8") from exc
    finally:
        os.close(descriptor)


def _read_private_bytes_no_follow(
    path: Path,
    *,
    description: str,
    max_size: int = 16 * 1024 * 1024,
) -> tuple[bytes, os.stat_result]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > max_size
    ):
        raise IsolationViolation(f"{description} is not a bounded private single-link regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > max_size
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise IsolationViolation(f"{description} changed or failed opened-inode validation")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise IsolationViolation(f"{description} exceeds the safety limit")
            chunks.append(chunk)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise IsolationViolation(f"{description} changed while it was read")
        return b"".join(chunks), opened
    finally:
        os.close(descriptor)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _recover_abandoned_app(runtime: Path) -> tuple[bool, dict[str, object], list[str]]:
    """start ticks一致時だけabandoned app process groupを停止する。"""

    assert_no_mount_targets(runtime)
    pid_path = runtime / "run" / "api.pid"
    identity_path = pid_path.with_name(pid_path.name + ".identity.json")
    evidence: dict[str, object] = {
        "pid_evidence_present": pid_path.exists() or pid_path.is_symlink(),
        "start_identity_present": identity_path.exists() or identity_path.is_symlink(),
        "signal_sent": False,
        "pid_reuse_detected": False,
    }
    if not evidence["pid_evidence_present"] and not evidence["start_identity_present"]:
        return True, evidence, []
    if not evidence["pid_evidence_present"] or not evidence["start_identity_present"]:
        return False, evidence, ["abandoned application has incomplete PID/start-identity evidence"]
    try:
        raw_pid = _read_private_regular_file(pid_path, description="abandoned application PID evidence").strip()
        identity = json.loads(_read_private_regular_file(identity_path, description="abandoned application start-identity evidence"))
        pid = int(raw_pid) if raw_pid.isdigit() else 0
        identity_pid = identity.get("pid") if isinstance(identity, dict) else None
        start_ticks = identity.get("start_ticks") if isinstance(identity, dict) else None
        if pid <= 1 or identity_pid != pid or not isinstance(start_ticks, int) or isinstance(start_ticks, bool) or start_ticks <= 0:
            raise IsolationViolation("abandoned application PID/start-identity evidence is invalid")
        evidence["process_identity_sha256"] = hashlib.sha256(f"{pid}:{start_ticks}".encode("ascii")).hexdigest()
        current_ticks = _process_start_ticks(pid)
        if current_ticks is None:
            if _process_group_exists(pid):
                return False, evidence, ["abandoned application group remains but its leader start identity is unavailable"]
            pid_path.unlink(missing_ok=True)
            identity_path.unlink(missing_ok=True)
            return True, evidence, []
        if current_ticks != start_ticks:
            evidence["pid_reuse_detected"] = True
            if _process_group_exists(pid):
                return False, evidence, ["recorded PID was reused while its process-group identity remains ambiguous"]
            pid_path.unlink(missing_ok=True)
            identity_path.unlink(missing_ok=True)
            return True, evidence, []
        proc = Path("/proc") / str(pid)
        if proc.stat().st_uid != os.geteuid():
            raise IsolationViolation("abandoned application process belongs to another user")
        if os.getpgid(pid) != pid:
            raise IsolationViolation("abandoned application is not the recorded process-group leader")
        if _process_start_ticks(pid) != start_ticks or os.getpgid(pid) != pid:
            raise IsolationViolation("abandoned application identity changed immediately before SIGTERM")
        os.killpg(pid, signal.SIGTERM)
        evidence["signal_sent"] = True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _process_start_ticks(pid) == start_ticks:
            time.sleep(0.05)
        if _process_start_ticks(pid) == start_ticks:
            if os.getpgid(pid) != pid or _process_start_ticks(pid) != start_ticks:
                return False, evidence, ["abandoned application identity changed before SIGKILL; signal refused"]
            os.killpg(pid, signal.SIGKILL)
            evidence["kill_escalated"] = True
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _process_start_ticks(pid) == start_ticks:
                time.sleep(0.05)
        current_ticks = _process_start_ticks(pid)
        if current_ticks is not None and current_ticks != start_ticks:
            evidence["pid_reuse_detected"] = True
        if current_ticks == start_ticks or _process_group_exists(pid):
            return False, evidence, ["abandoned application process group remained after bounded termination"]
        pid_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        return True, evidence, []
    except (OSError, ValueError, json.JSONDecodeError, IsolationViolation) as exc:
        return False, evidence, [f"abandoned application recovery failed: {type(exc).__name__}: {exc}"]


def _run_recovery_docker(environment: dict[str, str], repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    verify_local_docker_environment(environment)
    return subprocess.run(
        ["docker", *args],
        cwd=repository,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _orphan_resource_ids(
    environment: dict[str, str],
    repository: Path,
    kind: str,
    project_name: str,
    owner_hash: str,
) -> set[str]:
    noun = {"container": "container", "volume": "volume", "network": "network"}[kind]
    found: set[str] = set()
    for label in (
        f"{COMPOSE_PROJECT_LABEL}={project_name}",
        f"{COMPOSE_OWNER_LABEL}={owner_hash}",
    ):
        args = [noun, "ls"]
        if kind == "container":
            args.append("--all")
        args.extend(["--quiet", "--filter", f"label={label}"])
        completed = _run_recovery_docker(environment, repository, *args)
        if completed.returncode != 0:
            raise StackFailure("stale_runtime_recovery", f"Docker could not enumerate abandoned {kind} resources")
        found.update(line.strip() for line in completed.stdout.splitlines() if line.strip())
    return found


def _inspect_orphan_resource(
    environment: dict[str, str],
    repository: Path,
    kind: str,
    resource_id: str,
    project_name: str,
    owner_hash: str,
) -> dict[str, object]:
    completed = _run_recovery_docker(environment, repository, kind, "inspect", resource_id)
    if completed.returncode != 0:
        raise StackFailure("stale_runtime_recovery", f"Docker could not inspect abandoned {kind} resource")
    try:
        decoded = json.loads(completed.stdout)
        item = decoded[0]
        if not isinstance(item, dict):
            raise TypeError("inspect item is not an object")
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise StackFailure("stale_runtime_recovery", f"Docker returned invalid abandoned {kind} inspect data") from exc
    config = item.get("Config")
    labels = config.get("Labels") if kind == "container" and isinstance(config, dict) else item.get("Labels")
    labels = labels if isinstance(labels, dict) else {}
    return {
        "kind": kind,
        "raw_id": resource_id,
        "id_sha256": hashlib.sha256(resource_id.encode("utf-8")).hexdigest(),
        "project_matches": labels.get(COMPOSE_PROJECT_LABEL) == project_name,
        "owner_matches": labels.get(COMPOSE_OWNER_LABEL) == owner_hash,
    }


def _recover_orphan_docker_resources(
    environment: dict[str, str],
    repository: Path,
    project_name: str,
    owner_hash: str,
) -> tuple[bool, list[dict[str, object]], list[str]]:
    rows: list[dict[str, object]] = []
    try:
        for kind in ("container", "network", "volume"):
            rows.extend(
                _inspect_orphan_resource(environment, repository, kind, resource_id, project_name, owner_hash)
                for resource_id in sorted(_orphan_resource_ids(environment, repository, kind, project_name, owner_hash))
            )
        mismatches = [row for row in rows if not row["project_matches"] or not row["owner_matches"]]
        if mismatches:
            return False, rows, ["abandoned Docker resource deletion refused because dual-label ownership did not match"]
        for kind in ("container", "network", "volume"):
            identifiers = [str(row["raw_id"]) for row in rows if row["kind"] == kind]
            if not identifiers:
                continue
            verb = ["container", "rm", "--force"] if kind == "container" else [kind, "rm"]
            completed = _run_recovery_docker(environment, repository, *verb, *identifiers)
            if completed.returncode != 0:
                return False, rows, [f"Docker could not remove abandoned {kind} resources"]
        remaining = sum(
            len(_orphan_resource_ids(environment, repository, kind, project_name, owner_hash))
            for kind in ("container", "network", "volume")
        )
        if remaining:
            return False, rows, [f"{remaining} abandoned Docker resource(s) remain after cleanup"]
        return True, rows, []
    except (OSError, subprocess.SubprocessError, StackFailure, IsolationViolation) as exc:
        return False, rows, [f"abandoned Docker recovery failed: {type(exc).__name__}: {exc}"]
    finally:
        for row in rows:
            row.pop("raw_id", None)


def _normalize_runtime_permissions(
    runtime: Path,
    marker: dict[str, object],
    *,
    docker_environment: dict[str, str],
    repository: Path,
) -> bool:
    """Make container-created paths removable without widening the deletion target."""

    validate_runtime_marker(runtime, marker)
    assert_no_mount_targets(runtime)
    unlink_runtime_control_socket_no_follow(runtime, require_owner_uid=os.geteuid())
    try:
        chmod_tree_no_follow(runtime, directory_mode=0o700, file_mode=0o600, allow_symlinks=True)
        return False
    except PermissionError:
        validate_runtime_marker(runtime, marker)
        assert_no_mount_targets(runtime)
        runtime_metadata = runtime.stat()
        marker_path = runtime / RUNTIME_MARKER_NAME
        marker_sha256 = hashlib.sha256(marker_path.read_bytes()).hexdigest()
        runtime_identity = f"{runtime_metadata.st_dev}:{runtime_metadata.st_ino}"
        completed = _run_recovery_docker(
            docker_environment,
            repository,
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--label",
            f"{COMPOSE_PROJECT_LABEL}={marker['project']}",
            "--label",
            f"{COMPOSE_OWNER_LABEL}={marker['owner_hash']}",
            "--user",
            "0:0",
            "--mount",
            f"type=bind,src={runtime},dst=/sherpa-ui-owned-runtime,bind-recursive=disabled",
            "--entrypoint",
            "/bin/sh",
            "postgres:16",
            "-ceu",
            'test "$(stat -c \'%d:%i\' /sherpa-ui-owned-runtime)" = "$1"; '
            'actual_marker="$(sha256sum /sherpa-ui-owned-runtime/.ui-automation-runtime.json)"; '
            'test "${actual_marker%% *}" = "$2"; '
            'hardlinks="$(find /sherpa-ui-owned-runtime -type f -links +1 -print -quit)" || exit 1; '
            'test -z "$hardlinks"; '
            'exec chown -hR -- "$3" /sherpa-ui-owned-runtime',
            "runtime-identity-check",
            runtime_identity,
            marker_sha256,
            f"{os.geteuid()}:{os.getegid()}",
        )
        if completed.returncode != 0:
            raise IsolationViolation("Docker ownership repair could not normalize the owned runtime")
        validate_runtime_marker(runtime, marker)
        assert_no_mount_targets(runtime)
        chmod_tree_no_follow(runtime, directory_mode=0o700, file_mode=0o600, allow_symlinks=True)
        return True


def _remove_owned_runtime(
    runtime: Path,
    parent: Path,
    marker: dict[str, object],
    *,
    docker_environment: dict[str, str],
    repository: Path,
) -> bool:
    validate_runtime_marker(runtime, marker)
    if runtime.parent != parent or not runtime.name.startswith("sherpa-ui-automation-"):
        raise IsolationViolation("runtime removal target is outside the dedicated runtime parent")
    repaired = _normalize_runtime_permissions(
        runtime,
        marker,
        docker_environment=docker_environment,
        repository=repository,
    )
    assert_no_mount_targets(runtime)
    rmtree_no_follow(runtime)
    return repaired


def recover_stale_run_runtimes(
    *,
    repository: Path,
    artifacts_root: Path,
    current_run_id: str,
) -> dict[str, object]:
    """以前の異常終了runをdual-label/PID世代一致で安全に回収する。"""

    rows: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        parent = _runtime_parent()
    except IsolationViolation as exc:
        return {"status": "FAIL", "recovered": 0, "runtimes": [], "errors": [str(exc)]}
    lock_path = parent / f".sherpa-ui-automation-recovery-{os.geteuid()}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_descriptor: int | None = None
    try:
        lock_descriptor = os.open(lock_path, flags, 0o600)
        lock_metadata = os.fstat(lock_descriptor)
        if (
            lock_metadata.st_uid != os.geteuid()
            or not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_nlink != 1
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            raise IsolationViolation("stale runtime recovery lock is not runner-owned")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
    except (OSError, IsolationViolation) as exc:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        return {
            "status": "FAIL",
            "recovered": 0,
            "runtimes": [],
            "errors": [f"stale runtime recovery lock failed: {type(exc).__name__}: {exc}"],
        }
    recovery_home: Path | None = None
    outcome: dict[str, object] | None = None
    try:
        candidates = sorted(
            path
            for path in parent.iterdir()
            if path.name.startswith("sherpa-ui-automation-") and not path.name.startswith("sherpa-ui-playwright-cache-")
        )
        ambient_controls = sorted(name for name in _DOCKER_ENDPOINT_ENV if name in os.environ)
        docker_environment: dict[str, str] | None = None
        daemon_identity: dict[str, object] | None = None
        if ambient_controls:
            errors.append(
                "ambient Docker endpoint/context/TLS/config overrides prevent orphan Docker recovery: " + ", ".join(ambient_controls)
            )
        elif candidates:
            recovery_home = Path(tempfile.mkdtemp(prefix="sherpa-ui-docker-recovery-", dir=parent))
            chmod_path_no_follow(recovery_home, 0o700, require_owner_uid=os.geteuid())
            docker_environment = local_docker_environment(dict(os.environ))
            docker_environment["HOME"] = str(recovery_home)
            try:
                socket_identity = verify_local_docker_environment(docker_environment)
                completed = _run_recovery_docker(docker_environment, repository, "info", "--format", "{{json .ID}}")
                if completed.returncode != 0:
                    raise StackFailure("stale_runtime_recovery", f"local Docker daemon identity probe exited {completed.returncode}")
                daemon_id = json.loads(completed.stdout.strip())
                if not isinstance(daemon_id, str) or not daemon_id:
                    raise StackFailure("stale_runtime_recovery", "local Docker daemon returned an invalid identity")
                daemon_identity = {
                    **socket_identity,
                    "daemon_id_sha256": hashlib.sha256(daemon_id.encode("utf-8")).hexdigest(),
                    "raw_daemon_id_recorded": False,
                }
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError, StackFailure, IsolationViolation) as exc:
                errors.append(f"orphan Docker identity attestation failed: {type(exc).__name__}: {exc}")
                docker_environment = None
        for runtime in candidates:
            row: dict[str, object] = {
                "runtime_name_sha256": hashlib.sha256(runtime.name.encode("utf-8")).hexdigest(),
                "runtime_removed": False,
                "secrets_scrubbed": False,
            }
            try:
                marker, current_schema = validate_runtime_marker_contract(runtime, allow_legacy=True)
                row["marker_schema"] = marker.get("schema", "legacy")
                row["current_schema"] = current_schema
                if (
                    marker.get("run_id") == current_run_id
                    or (current_schema and _runtime_marker_is_active(marker))
                    or (not current_schema and _legacy_artifact_run_is_active(artifacts_root, str(marker.get("run_id") or "")))
                ):
                    row["disposition"] = "active-preserved"
                    rows.append(row)
                    continue
                if not current_schema:
                    scrub = scrub_runtime_secret_files(runtime, marker)
                    row["secrets_scrubbed"] = scrub.get("status") == "PASS"
                    row["disposition"] = "legacy-secret-scrubbed-runtime-retained"
                    row["scrub_errors"] = scrub.get("errors") or []
                    if scrub.get("status") != "PASS":
                        errors.append("legacy runtime secret scrub failed")
                    rows.append(row)
                    continue
                app_absent, app_evidence, app_errors = _recover_abandoned_app(runtime)
                row["application"] = app_evidence
                if app_errors:
                    errors.extend(app_errors)
                compose_absent = False
                resources: list[dict[str, object]] = []
                docker_errors: list[str] = []
                if docker_environment is not None:
                    compose_absent, resources, docker_errors = _recover_orphan_docker_resources(
                        docker_environment,
                        repository,
                        str(marker["project"]),
                        str(marker["owner_hash"]),
                    )
                else:
                    docker_errors.append("local Docker identity was not attested; orphan resource absence is unproven")
                row["docker_resources"] = resources
                row["compose_resources_absent"] = compose_absent
                if docker_errors:
                    errors.extend(docker_errors)
                scrub = scrub_runtime_secret_files(runtime, marker)
                secrets_scrubbed = scrub.get("status") == "PASS"
                row["secrets_scrubbed"] = secrets_scrubbed
                row["scrub_errors"] = scrub.get("errors") or []
                if not secrets_scrubbed:
                    errors.append("abandoned runtime secret scrub failed")
                if app_absent and compose_absent and secrets_scrubbed:
                    row["docker_ownership_repair_used"] = _remove_owned_runtime(
                        runtime,
                        parent,
                        marker,
                        docker_environment=docker_environment,
                        repository=repository,
                    )
                    row["runtime_removed"] = True
                    row["disposition"] = "recovered"
                else:
                    row["disposition"] = "secret-scrubbed-runtime-retained"
                    errors.append("abandoned runtime retained because app/resource absence or secret scrub was unproven")
            except (OSError, ValueError, RuntimeError, IsolationViolation) as exc:
                row["disposition"] = "refused"
                row["error"] = f"{type(exc).__name__}: {exc}"
                errors.append(f"stale runtime recovery refused an unsafe candidate: {type(exc).__name__}: {exc}")
            rows.append(row)
        outcome = {
            "status": "FAIL" if errors else "PASS",
            "recovered": sum(bool(row.get("runtime_removed")) for row in rows),
            "secret_scrubbed": sum(bool(row.get("secrets_scrubbed")) for row in rows),
            "runtimes": rows,
            "docker_daemon_identity": daemon_identity,
            "errors": errors,
            "raw_secret_values_recorded": False,
            "deletion_requires_current_marker_and_dual_labels": True,
        }
    finally:
        if recovery_home is not None:
            try:
                assert_no_mount_targets(recovery_home)
                rmtree_no_follow(recovery_home)
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"stale runtime recovery HOME cleanup failed: {type(exc).__name__}: {exc}")
        if lock_descriptor is not None:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            except OSError as exc:
                errors.append(f"stale runtime recovery lock release failed: {type(exc).__name__}: {exc}")
            try:
                os.close(lock_descriptor)
            except OSError as exc:
                errors.append(f"stale runtime recovery lock close failed: {type(exc).__name__}: {exc}")
    if outcome is None:
        raise RuntimeError("stale runtime recovery produced no result")
    outcome["status"] = "FAIL" if errors else "PASS"
    outcome["errors"] = errors
    return outcome


class IsolatedStack:
    def __init__(
        self,
        isolation: RunIsolation,
        redactor: SecretRedactor,
        *,
        timeout_seconds: int = 240,
        enable_ocr: bool = False,
    ) -> None:
        self.isolation = isolation
        self.redactor = redactor
        self.timeout_seconds = timeout_seconds
        self.enable_ocr = enable_ocr
        self.app_process: subprocess.Popen[str] | None = None
        self._app_log_handle = None
        self._app_log_thread: threading.Thread | None = None
        self._app_log_errors: list[str] = []
        self._app_log_lock = threading.Lock()
        self._app_start_attempts: list[dict[str, Any]] = []
        self._active_app_attempt: dict[str, Any] | None = None
        self._compose_attempted = False
        self._stores_started = False
        self._app_start_count = 0
        self._app_lock = threading.RLock()
        self._control_stop = threading.Event()
        self._control_socket: socket.socket | None = None
        self._control_thread: threading.Thread | None = None
        self._control_errors: list[str] = []
        self._restart_transition_applied = False
        self._secret_registry_signature: tuple[int, int] | None = None
        self._app_pid: int | None = None
        self._app_start_ticks: int | None = None
        self._last_compose_resources: list[dict[str, Any]] = []

    @property
    def repository(self) -> Path:
        return self.isolation.paths.repository

    @property
    def environment(self) -> dict[str, str]:
        return self.isolation.environment

    @property
    def runner_environment(self) -> dict[str, str]:
        return self.isolation.runner_environment

    def compose_command(self, *args: str, profile: str | None = None) -> list[str]:
        self._assert_local_docker_endpoint()
        command = [
            "docker",
            "compose",
            "--project-name",
            self.isolation.project_name,
            "--env-file",
            str(self.isolation.env_file),
            "--file",
            str(self.isolation.compose_file),
            "--file",
            str(self.isolation.compose_override_file),
        ]
        if profile:
            command.extend(["--profile", profile])
        command.extend(args)
        return command

    def _assert_local_docker_endpoint(self) -> dict[str, object]:
        try:
            return verify_local_docker_environment(
                self.runner_environment,
                expected_socket_identity_sha256=self.isolation.docker_socket_identity_sha256,
            )
        except IsolationViolation as exc:
            raise StackFailure("docker_identity", str(exc)) from exc

    def _docker_command(self, *args: str) -> list[str]:
        self._assert_local_docker_endpoint()
        return ["docker", *args]

    def attest_local_docker_daemon(self) -> None:
        """最初のDocker操作としてlocal socketとdaemon IDをhash証跡化する。"""

        evidence_path = self.isolation.paths.profile_root / "state" / "docker-daemon-identity.json"
        try:
            identity = self._assert_local_docker_endpoint()
            completed = subprocess.run(
                self._docker_command("info", "--format", "{{json .ID}}"),
                cwd=self.repository,
                env=self.runner_environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, StackFailure) as exc:
            self.redactor.write_json(
                evidence_path,
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "raw_endpoint_values_recorded": False,
                    "raw_daemon_id_recorded": False,
                },
            )
            if isinstance(exc, StackFailure):
                raise
            raise StackFailure("docker_identity", f"local Docker daemon identity probe failed: {type(exc).__name__}") from exc
        if completed.returncode != 0:
            self.redactor.write_json(
                evidence_path,
                {
                    **identity,
                    "status": "FAIL",
                    "exit_code": completed.returncode,
                    "raw_endpoint_values_recorded": False,
                    "raw_daemon_id_recorded": False,
                },
            )
            raise StackFailure("docker_identity", f"local Docker daemon identity probe exited {completed.returncode}")
        try:
            daemon_id = json.loads((completed.stdout or "").strip())
        except json.JSONDecodeError as exc:
            self.redactor.write_json(
                evidence_path,
                {**identity, "status": "FAIL", "error_type": type(exc).__name__, "raw_daemon_id_recorded": False},
            )
            raise StackFailure("docker_identity", "local Docker daemon returned an invalid identity") from exc
        if not isinstance(daemon_id, str) or not daemon_id.strip():
            self.redactor.write_json(
                evidence_path,
                {**identity, "status": "FAIL", "error_type": "empty-daemon-identity", "raw_daemon_id_recorded": False},
            )
            raise StackFailure("docker_identity", "local Docker daemon returned an empty identity")
        self.redactor.write_json(
            evidence_path,
            {
                **identity,
                "status": "PASS",
                "endpoint_is_runner_fixed": self.runner_environment.get("DOCKER_HOST") == LOCAL_DOCKER_ENDPOINT,
                "daemon_id_sha256": hashlib.sha256(daemon_id.encode("utf-8")).hexdigest(),
                "raw_daemon_id_recorded": False,
                "ambient_context_tls_config_allowed": False,
            },
        )

    def _resource_ids(self, kind: str, label: str) -> set[str]:
        noun = {"container": "container", "volume": "volume", "network": "network"}[kind]
        command = self._docker_command(noun, "ls")
        if kind == "container":
            command.append("--all")
        command.extend(["--quiet", "--filter", f"label={label}"])
        completed = subprocess.run(
            command,
            cwd=self.repository,
            env=self.runner_environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            raise StackFailure("compose_ownership", f"Docker could not enumerate {kind} resources")
        return {line.strip() for line in (completed.stdout or "").splitlines() if line.strip()}

    def _inspect_resource(self, kind: str, resource_id: str) -> dict[str, Any]:
        completed = subprocess.run(
            self._docker_command(kind, "inspect", resource_id),
            cwd=self.repository,
            env=self.runner_environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            raise StackFailure("compose_ownership", f"Docker could not inspect {kind} resource {resource_id[:12]}")
        try:
            decoded = json.loads(completed.stdout)
            if not isinstance(decoded, list) or not decoded or not isinstance(decoded[0], dict):
                raise TypeError("inspect response is not a non-empty object list")
            payload = decoded[0]
        except (TypeError, json.JSONDecodeError) as exc:
            raise StackFailure(
                "compose_ownership",
                f"Docker returned invalid inspect data for {kind} resource {resource_id[:12]}",
            ) from exc
        config = payload.get("Config")
        labels = config.get("Labels") if kind == "container" and isinstance(config, dict) else payload.get("Labels")
        labels = labels if isinstance(labels, dict) else {}
        raw_name = payload.get("Name") or resource_id
        name = str(raw_name).lstrip("/")
        project = labels.get(COMPOSE_PROJECT_LABEL)
        owner = labels.get(COMPOSE_OWNER_LABEL)
        return {
            "kind": kind,
            "id": str(payload.get("Id") or payload.get("ID") or resource_id),
            "name": name,
            "project_matches": project == self.isolation.project_name,
            "owner_matches": owner == self.isolation.owner_hash,
        }

    def _collect_compose_resources(self) -> list[dict[str, Any]]:
        project_filter = f"{COMPOSE_PROJECT_LABEL}={self.isolation.project_name}"
        owner_filter = f"{COMPOSE_OWNER_LABEL}={self.isolation.owner_hash}"
        rows: list[dict[str, Any]] = []
        for kind in ("container", "volume", "network"):
            resource_ids = self._resource_ids(kind, project_filter) | self._resource_ids(kind, owner_filter)
            rows.extend(self._inspect_resource(kind, resource_id) for resource_id in sorted(resource_ids))
        rows.sort(key=lambda row: (str(row["kind"]), str(row["name"]), str(row["id"])))
        self._last_compose_resources = rows
        return rows

    def _verify_compose_ownership(
        self,
        stage: str,
        *,
        require_present: bool = False,
        require_absent: bool = False,
    ) -> list[dict[str, Any]]:
        evidence_path = self.isolation.paths.profile_root / "state" / f"compose-ownership-{stage}.json"
        try:
            rows = self._collect_compose_resources()
            mismatches = [row for row in rows if not row["project_matches"] or not row["owner_matches"]]
            failures = []
            if mismatches:
                failures.append(f"{len(mismatches)} resource(s) failed project+owner verification")
            if require_present and not rows:
                failures.append("no run-owned Compose resources were found")
            if require_absent and rows:
                failures.append(f"{len(rows)} run-owned Compose resource(s) remain")
            self.redactor.write_json(
                evidence_path,
                {
                    "status": "FAIL" if failures else "PASS",
                    "stage": stage,
                    "project": self.isolation.project_name,
                    "owner_hash": self.isolation.owner_hash,
                    "resources": rows,
                    "failures": failures,
                },
            )
            if failures:
                raise StackFailure("compose_ownership", "; ".join(failures))
            return rows
        except (OSError, subprocess.SubprocessError, StackFailure) as exc:
            if not evidence_path.is_file():
                self.redactor.write_json(
                    evidence_path,
                    {
                        "status": "FAIL",
                        "stage": stage,
                        "project": self.isolation.project_name,
                        "owner_hash": self.isolation.owner_hash,
                        "resources": self._last_compose_resources,
                        "failures": [f"{type(exc).__name__}: {exc}"],
                    },
                )
            if isinstance(exc, StackFailure):
                raise
            raise StackFailure("compose_ownership", f"Compose ownership verification failed: {type(exc).__name__}") from exc

    def prepare_codex_auth(self) -> None:
        log_path = self.isolation.paths.profile_root / "services" / "codex-auth.log"
        key = self.environment.get("OPENAI_API_KEY", "")
        if not key or key in {"REPLACE_ME", "sk-REPLACE_ME"}:
            self._write_redacted_text(
                log_path,
                "OPENAI_API_KEY is not configured; ephemeral Codex authentication was not created.\n",
            )
            return
        codex = shutil.which("codex", path=self.runner_environment.get("PATH"))
        if not codex:
            self._write_redacted_text(log_path, "Codex CLI is not installed.\n")
            return
        try:
            completed = subprocess.run(
                [codex, "login", "--with-api-key"],
                cwd=self.repository,
                env=self.runner_environment,
                input=key + "\n",
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            text = f"exit={completed.returncode}\n{output}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            text = f"Codex authentication setup failed: {type(exc).__name__}: {exc}\n"
        self._write_redacted_text(log_path, text)

    def check_ports(self) -> None:
        self._assert_local_docker_endpoint()
        path = self.isolation.paths.profile_root / "services" / "port-check.log"
        try:
            completed = subprocess.run(
                [str(self.repository / "scripts" / "check-ports.sh")],
                cwd=self.repository,
                env=self.runner_environment,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._write_redacted_text(path, str(exc) + "\n")
            raise StackFailure("port_check", f"port check could not run: {exc}") from exc
        output = (completed.stdout or "") + (completed.stderr or "")
        self._write_redacted_text(path, output)
        if completed.returncode != 0:
            raise StackFailure("port_check", f"isolated port validation failed with exit {completed.returncode}")

    def start_stores(self) -> None:
        self._compose_attempted = True
        self._verify_compose_ownership("before-up")
        command = self.compose_command("up", "-d", "postgres", "neo4j", "elasticsearch")
        completed = self._run_logged(
            command, self.isolation.paths.profile_root / "services" / "compose-up.log", timeout=self.timeout_seconds
        )
        if completed.returncode != 0:
            raise StackFailure("compose_up", f"core store startup failed with exit {completed.returncode}")
        self._verify_compose_ownership("after-core-up", require_present=True)
        self._wait_services(("postgres", "neo4j", "elasticsearch"))
        if self.enable_ocr or _enabled(self.environment.get("SHERPA_OCR_ENABLED")):
            self.environment["SHERPA_OCR_ENABLED"] = "1"
            self._verify_compose_ownership("before-ocr-up", require_present=True)
            completed = self._run_logged(
                self.compose_command("up", "-d", "ocr-worker", profile="ocr"),
                self.isolation.paths.profile_root / "services" / "ocr-up.log",
                timeout=self.timeout_seconds,
            )
            if completed.returncode != 0:
                raise StackFailure("ocr_up", f"OCR worker startup failed with exit {completed.returncode}")
            self._verify_compose_ownership("after-ocr-up", require_present=True)
            self._wait_services(("ocr-worker",), allow_completed=True)
        self._stores_started = True

    def _app_pid_path(self) -> Path:
        configured = self.environment.get("APP_PID_FILE", "").strip()
        if not configured:
            raise RuntimeError("APP_PID_FILE is not configured")
        path = Path(configured)
        if not path.is_absolute() or path.is_symlink():
            raise RuntimeError("APP_PID_FILE must be an absolute non-symlink path")
        runtime = self.isolation.paths.runtime_root.resolve()
        try:
            path.parent.resolve(strict=True).relative_to(runtime)
        except (FileNotFoundError, ValueError) as exc:
            raise RuntimeError("APP_PID_FILE parent is outside the run-owned runtime") from exc
        return path

    def _write_app_pid(self, pid: int) -> None:
        path = self._app_pid_path()
        if path.exists() or path.is_symlink():
            raise RuntimeError("APP_PID_FILE already exists before app startup")
        start_ticks = _process_start_ticks(pid)
        if start_ticks is None:
            raise RuntimeError("application process start identity is unavailable")
        temporary = path.with_name(f".{path.name}.{pid}.{time.time_ns()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            payload = f"{pid}\n".encode("ascii")
            if os.write(descriptor, payload) != len(payload):
                raise OSError("APP_PID_FILE atomic write was incomplete")
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        identity_path = self._app_pid_identity_path()
        identity_temporary = identity_path.with_name(f".{identity_path.name}.{pid}.{time.time_ns()}.tmp")
        descriptor = os.open(identity_temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            payload = json.dumps({"pid": pid, "start_ticks": start_ticks}, sort_keys=True).encode("utf-8") + b"\n"
            if os.write(descriptor, payload) != len(payload):
                raise OSError("application process identity write was incomplete")
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(identity_temporary, identity_path)
            self._app_start_ticks = start_ticks
        except BaseException:
            identity_temporary.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _write_app_start_attempts(self) -> None:
        self.redactor.write_json(
            self.isolation.paths.profile_root / "state" / "app-start-attempts.json",
            {
                "status": "PASS",
                "profile": self.isolation.profile.name,
                "attempt_count": len(self._app_start_attempts),
                "attempts": self._app_start_attempts,
                "raw_environment_values_recorded": False,
                "raw_command_line_recorded": False,
            },
        )

    def _begin_app_start_attempt(self, app_log: Path, metadata: os.stat_result) -> None:
        scenario = self.isolation.scenario_contract
        variable = str(scenario.get("variable") or "") if scenario else ""
        scenario_name = str(scenario.get("scenario") or "") if scenario else ""
        secret = bool(scenario.get("secret")) if scenario else False
        effective_value = self.environment.get(variable, "") if variable else ""
        attempt = {
            "attempt_id": f"app-start-{self._app_start_count + 1}",
            "attempt_index": self._app_start_count,
            "profile": self.isolation.profile.name,
            "scenario_variable": variable or None,
            "scenario": scenario_name or None,
            "scenario_value_sha256": (None if secret or not variable else hashlib.sha256(effective_value.encode("utf-8")).hexdigest()),
            "started_at_ns": time.time_ns(),
            "log_relative_path": str(app_log.relative_to(self.isolation.paths.profile_root)),
            "log_inode_sha256": hashlib.sha256(f"{metadata.st_dev}:{metadata.st_ino}".encode("ascii")).hexdigest(),
            "spawned": False,
            "ready_observed": False,
            "failure_observed": False,
            "exit_observed": False,
            "exit_code": None,
            "outcome": "starting",
            "raw_scenario_value_recorded": False,
        }
        self._app_start_attempts.append(attempt)
        self._active_app_attempt = attempt
        self._write_app_start_attempts()

    def _snapshot_active_app_attempt(self, app_log: Path, **updates: Any) -> None:
        attempt = self._active_app_attempt
        if attempt is None:
            raise RuntimeError("application start attempt evidence is unavailable")
        with self._app_log_lock:
            handle = self._app_log_handle
            if handle is not None:
                handle.flush()
                os.fsync(handle.fileno())
            content, metadata = _read_private_bytes_no_follow(
                app_log,
                description="application start log",
            )
            if hashlib.sha256(f"{metadata.st_dev}:{metadata.st_ino}".encode("ascii")).hexdigest() != attempt["log_inode_sha256"]:
                raise RuntimeError("application start log changed inode or failed ownership validation")
        attempt.update(updates)
        attempt.update(
            {
                "log_snapshot_size": len(content),
                "log_snapshot_sha256": hashlib.sha256(content).hexdigest(),
                "log_snapshot_at_ns": time.time_ns(),
            }
        )
        self._write_app_start_attempts()

    def _app_pid_identity_path(self) -> Path:
        path = self._app_pid_path()
        return path.with_name(path.name + ".identity.json")

    def _read_app_pid_identity(self) -> tuple[int, int]:
        path = self._app_pid_identity_path()
        try:
            payload = json.loads(_read_private_regular_file(path, description="application PID identity sidecar"))
        except (OSError, json.JSONDecodeError, IsolationViolation) as exc:
            raise RuntimeError("application PID identity sidecar is unreadable") from exc
        pid = payload.get("pid") if isinstance(payload, dict) else None
        start_ticks = payload.get("start_ticks") if isinstance(payload, dict) else None
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 1
            or not isinstance(start_ticks, int)
            or isinstance(start_ticks, bool)
            or start_ticks <= 0
        ):
            raise RuntimeError("application PID identity sidecar is invalid")
        return pid, start_ticks

    def _read_app_pid(self) -> int:
        path = self._app_pid_path()
        try:
            value = _read_private_regular_file(path, description="APP_PID_FILE").strip()
        except (OSError, IsolationViolation) as exc:
            raise RuntimeError("APP_PID_FILE is unreadable") from exc
        if not value.isdigit() or int(value) <= 0:
            raise RuntimeError("APP_PID_FILE does not contain a valid PID")
        return int(value)

    @staticmethod
    def _terminate_app_process(process: subprocess.Popen[str]) -> None:
        start_ticks = _process_start_ticks(process.pid)
        if start_ticks is None:
            if process.poll() is None:
                raise RuntimeError("application process start identity is unavailable; signal delivery was refused")
            process.wait()
            return
        if process.poll() is not None:
            process.wait()
            if _process_group_exists(process.pid):
                raise RuntimeError("application leader exited while its process group remains; signal identity is unproven")
            return
        try:
            group = os.getpgid(process.pid)
        except ProcessLookupError:
            process.wait(timeout=5)
            return
        if group != process.pid:
            raise RuntimeError("application is not its recorded process-group leader; signal delivery was refused")
        try:
            if _process_start_ticks(process.pid) != start_ticks or os.getpgid(process.pid) != process.pid:
                raise RuntimeError("application identity changed immediately before SIGTERM")
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=5)
            return
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                if _process_start_ticks(process.pid) != start_ticks or os.getpgid(process.pid) != process.pid:
                    raise RuntimeError("application identity changed before SIGKILL; signal delivery was refused")
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        if group == process.pid and _process_group_exists(group):
            raise RuntimeError("application leader exited while its process group remains; further signal identity is unproven")

    def start_app(self) -> None:
        with self._app_lock:
            log_name = "app.log" if self._app_start_count == 0 else f"app-restart-{self._app_start_count}.log"
            app_log = self.isolation.paths.profile_root / "services" / log_name
            app_log.parent.mkdir(parents=True, exist_ok=True)
            if app_log.is_dir() and not app_log.is_symlink():
                raise StackFailure("app_start", "application log target is a directory")
            app_log.unlink(missing_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            log_descriptor = os.open(app_log, flags, 0o600)
            log_metadata = os.fstat(log_descriptor)
            if not stat.S_ISREG(log_metadata.st_mode) or log_metadata.st_uid != os.geteuid() or log_metadata.st_nlink != 1:
                os.close(log_descriptor)
                app_log.unlink(missing_ok=True)
                raise StackFailure("app_start", "application log failed opened-inode validation")
            self._app_log_handle = os.fdopen(log_descriptor, "w", encoding="utf-8")
            self._begin_app_start_attempt(app_log, log_metadata)
            try:
                self.app_process = subprocess.Popen(
                    [str(self.repository / "scripts" / "run-api.sh"), "serve"],
                    cwd=self.repository,
                    env=self.environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
                self._app_pid = self.app_process.pid
                self._write_app_pid(self.app_process.pid)
                self._snapshot_active_app_attempt(
                    app_log,
                    spawned=True,
                    process_identity_sha256=hashlib.sha256(f"{self.app_process.pid}:{self._app_start_ticks}".encode("ascii")).hexdigest(),
                )
            except OSError as exc:
                if self.app_process is not None:
                    self._terminate_app_process(self.app_process)
                    self.app_process = None
                    self._app_pid = None
                    self._app_start_ticks = None
                self._app_log_handle.close()
                self._app_log_handle = None
                self._snapshot_active_app_attempt(
                    app_log,
                    failure_observed=True,
                    failure_stage="app_start",
                    outcome="spawn_error",
                    error_type=type(exc).__name__,
                    finished_at_ns=time.time_ns(),
                )
                raise StackFailure("app_start", f"FastAPI process could not start: {exc}") from exc
            except RuntimeError as exc:
                if self.app_process is not None:
                    self._terminate_app_process(self.app_process)
                    self.app_process = None
                    self._app_pid = None
                    self._app_start_ticks = None
                self._app_log_handle.close()
                self._app_log_handle = None
                self._snapshot_active_app_attempt(
                    app_log,
                    failure_observed=True,
                    failure_stage="app_start",
                    outcome="identity_error",
                    error_type=type(exc).__name__,
                    finished_at_ns=time.time_ns(),
                )
                raise StackFailure("app_start", f"FastAPI PID ownership could not be established: {exc}") from exc
            self._app_log_thread = threading.Thread(
                target=self._copy_redacted_app_log,
                args=(self.app_process, self._app_log_handle),
                name=f"ui-app-log-{self.isolation.profile.name}-{self._app_start_count}",
                daemon=True,
            )
            self._app_log_thread.start()
            try:
                self._wait_app()
            except StackFailure as exc:
                return_code = self.app_process.poll() if self.app_process is not None else None
                self._snapshot_active_app_attempt(
                    app_log,
                    failure_observed=True,
                    failure_stage=exc.stage,
                    outcome="startup_failure",
                    exit_observed=return_code is not None,
                    exit_code=return_code,
                    finished_at_ns=time.time_ns(),
                )
                raise
            self._snapshot_active_app_attempt(
                app_log,
                ready_observed=True,
                outcome="ready",
                finished_at_ns=time.time_ns(),
            )
            self._app_start_count += 1

    def restart_app(self) -> None:
        with self._app_lock:
            self._stop_app()
            self._write_redacted_text(
                self.isolation.paths.profile_root / "services" / "restart-app.log",
                "Restarting only the isolated FastAPI process; stores and volumes are preserved.\n",
            )
            self.start_app()

    def restart_stack(self) -> None:
        with self._app_lock:
            self._stop_app()
            self._verify_compose_ownership("before-stack-restart", require_present=True)
            completed = self._run_logged(
                self.compose_command("restart", "postgres", "neo4j", "elasticsearch"),
                self.isolation.paths.profile_root / "services" / "restart-stack.log",
                timeout=self.timeout_seconds,
            )
            if completed.returncode != 0:
                raise StackFailure("stack_restart", f"core store restart failed with exit {completed.returncode}")
            self._wait_services(("postgres", "neo4j", "elasticsearch"))
            if self.enable_ocr or _enabled(self.environment.get("SHERPA_OCR_ENABLED")):
                completed = self._run_logged(
                    self.compose_command("restart", "ocr-worker", profile="ocr"),
                    self.isolation.paths.profile_root / "services" / "restart-ocr.log",
                    timeout=self.timeout_seconds,
                )
                if completed.returncode != 0:
                    raise StackFailure("stack_restart", f"OCR worker restart failed with exit {completed.returncode}")
                self._wait_services(("ocr-worker",), allow_completed=True)
            self.start_app()

    def stop_neo4j(self) -> str:
        """Stop only this run's Neo4j container after proving dual-label ownership."""
        with self._app_lock:
            self._verify_compose_ownership("before-neo4j-stop", require_present=True)
            completed = self._run_logged(
                self.compose_command("stop", "--timeout", "15", "neo4j"),
                self.isolation.paths.profile_root / "services" / "neo4j-stop.log",
                timeout=self.timeout_seconds,
            )
            if completed.returncode != 0:
                raise StackFailure("neo4j_stop", f"isolated Neo4j stop failed with exit {completed.returncode}")
            self._verify_compose_ownership("after-neo4j-stop", require_present=True)
            available, detail = self.service_probe("neo4j", allow_completed=False)
            if available:
                raise StackFailure("neo4j_stop", f"isolated Neo4j remained available: {detail}")
            return detail

    def start_neo4j(self) -> str:
        """Start only this run's existing Neo4j service and wait for real readiness."""
        with self._app_lock:
            self._verify_compose_ownership("before-neo4j-start", require_present=True)
            completed = self._run_logged(
                self.compose_command("up", "-d", "neo4j"),
                self.isolation.paths.profile_root / "services" / "neo4j-start.log",
                timeout=self.timeout_seconds,
            )
            if completed.returncode != 0:
                raise StackFailure("neo4j_start", f"isolated Neo4j start failed with exit {completed.returncode}")
            self._verify_compose_ownership("after-neo4j-start", require_present=True)
            self._wait_services(("neo4j",))
            available, detail = self.service_probe("neo4j", allow_completed=False)
            if not available:
                raise StackFailure("neo4j_start", f"isolated Neo4j did not recover: {detail}")
            return detail

    def restart_app_with_profile_environment(self, transition_id: str) -> list[str]:
        expected = self.isolation.restart_transition_id
        if not expected or transition_id != expected:
            raise ValueError("restart transition does not match the declared profile transition")
        if self._restart_transition_applied:
            raise ValueError("restart transition was already applied")
        changed: list[str] = []
        for name, value in self.isolation.restart_environment.items():
            if value is None:
                self.environment.pop(name, None)
            else:
                self.environment[name] = value
            changed.append(name)
        self._restart_transition_applied = True
        self.redactor.write_json(
            self.isolation.paths.profile_root / "state" / "restart-effective-environment.json",
            {
                "transition_id": transition_id,
                "changed_keys": sorted(changed),
                "effective_environment": sanitized_environment(self.environment, self.redactor),
            },
        )
        self.restart_app()
        return sorted(changed)

    def start_control_server(self) -> None:
        """所有権で保護したUnix socketで、caseから固定のapp再起動だけを受け付ける。"""
        control_dir = self.isolation.paths.runtime_root / "control"
        control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        chmod_path_no_follow(control_dir, 0o700, require_owner_uid=os.geteuid())
        socket_path = control_dir / "runner.sock"
        if socket_path.exists():
            socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        # The socket lives below a runner-owned 0700 directory.  Avoid a
        # pathname chmod here: Unix-socket inodes cannot be opened for fchmod,
        # and a path-based chmod would reintroduce a replacement race.
        server.listen(1)
        server.settimeout(0.5)
        self._control_socket = server
        self.environment["SHERPA_UI_CONTROL_SOCKET"] = str(socket_path)
        self._control_thread = threading.Thread(
            target=self._control_loop,
            name=f"ui-control-{self.isolation.profile.name}",
            daemon=True,
        )
        self._control_thread.start()

    def _control_loop(self) -> None:
        server = self._control_socket
        if server is None:
            return
        while not self._control_stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    chunks: list[bytes] = []
                    size = 0
                    connection.settimeout(5)
                    while size <= 4096:
                        chunk = connection.recv(min(1024, 4097 - size))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                        if b"\n" in chunk:
                            break
                    request = b"".join(chunks).split(b"\n", 1)[0]
                    if size > 4096:
                        raise ValueError("control request is too large")
                    payload = json.loads(request.decode("utf-8"))
                    base = {
                        "run_id": self.isolation.run_id,
                        "profile": self.isolation.profile.name,
                    }
                    if payload == {"action": "restart_app", **base}:
                        self.restart_app()
                        response = {"ok": True, "action": "restart_app", "app_start_count": self._app_start_count}
                        self._write_control_event("PASS", "restart_app")
                    elif payload == {"action": "stop_neo4j", **base}:
                        detail = self.stop_neo4j()
                        response = {
                            "ok": True,
                            "action": "stop_neo4j",
                            "service": "neo4j",
                            "available": False,
                            "detail": detail,
                        }
                        self._write_control_event("PASS", "stop_neo4j")
                    elif payload == {"action": "start_neo4j", **base}:
                        detail = self.start_neo4j()
                        response = {
                            "ok": True,
                            "action": "start_neo4j",
                            "service": "neo4j",
                            "available": True,
                            "detail": detail,
                        }
                        self._write_control_event("PASS", "start_neo4j")
                    elif payload == {
                        "action": "restart_app_with_profile_env",
                        **base,
                        "transition_id": self.isolation.restart_transition_id,
                    }:
                        changed = self.restart_app_with_profile_environment(str(payload["transition_id"]))
                        response = {
                            "ok": True,
                            "action": "restart_app_with_profile_env",
                            "transition_id": payload["transition_id"],
                            "changed_keys": changed,
                            "app_start_count": self._app_start_count,
                        }
                        self._write_control_event("PASS", "restart_app_with_profile_env")
                    else:
                        raise ValueError("control request does not match this run/profile or fixed action")
                except Exception as exc:
                    message = self.redactor.redact_text(f"{type(exc).__name__}: {exc}")
                    self._control_errors.append(message)
                    self._write_control_event("FAIL", message)
                    response = {"ok": False, "error": message}
                try:
                    connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
                except OSError:
                    pass

    def _write_control_event(self, status: str, detail: str) -> None:
        path = self.isolation.paths.profile_root / "services" / "control.jsonl"
        append_private_text(
            path,
            self.redactor.redact_text(json.dumps({"at": time.time(), "status": status, "detail": detail})) + "\n",
        )

    def _stop_control_server(self) -> None:
        self._control_stop.set()
        server = self._control_socket
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        thread = self._control_thread
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                self._control_errors.append("control server did not stop")
        self._control_socket = None
        self._control_thread = None

    def _wait_services(self, services: tuple[str, ...], *, allow_completed: bool = False) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        last: dict[str, str] = {}
        while time.monotonic() < deadline:
            ready = True
            for service in services:
                ok, detail = self.service_probe(service, "ocr" if service == "ocr-worker" else None, allow_completed=allow_completed)
                last[service] = detail
                if not ok:
                    ready = False
            if ready:
                return
            time.sleep(1)
        raise StackFailure("store_health", f"services did not become ready: {last}")

    def service_probe(self, service: str, profile: str | None = None, *, allow_completed: bool = True) -> tuple[bool, str]:
        if not service or not re_safe_service(service):
            return False, "invalid compose service name"
        command = self.compose_command("ps", "--all", "-q", service, profile=profile)
        completed = subprocess.run(
            command,
            cwd=self.repository,
            env=self.runner_environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        container_id = (completed.stdout or "").strip().splitlines()
        if completed.returncode != 0 or not container_id:
            return False, f"service {service} has no container"
        cid = container_id[0]
        inspected = subprocess.run(
            self._docker_command("inspect", cid),
            cwd=self.repository,
            env=self.runner_environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if inspected.returncode != 0:
            return False, f"container {cid[:12]} cannot be inspected"
        try:
            info = json.loads(inspected.stdout)[0]
            labels = info.get("Config", {}).get("Labels") or {}
            state = info.get("State") or {}
        except (IndexError, TypeError, json.JSONDecodeError):
            return False, f"container {cid[:12]} inspect response is invalid"
        if labels.get(COMPOSE_PROJECT_LABEL) != self.isolation.project_name:
            return False, f"container {cid[:12]} belongs to another compose project"
        if labels.get(COMPOSE_OWNER_LABEL) != self.isolation.owner_hash:
            return False, f"container {cid[:12]} does not carry this run's owner label"
        health = (state.get("Health") or {}).get("Status")
        status = str(health or state.get("Status") or "unknown")
        exit_code = int(state.get("ExitCode") or 0)
        ok = status in {"healthy", "running"} or (allow_completed and status == "exited" and exit_code == 0)
        return ok, f"container={cid[:12]} status={status} exit={exit_code}"

    def verify_store_identities(self) -> list[str]:
        failures: list[str] = []
        state_dir = self.isolation.paths.profile_root / "state"
        try:
            import psycopg

            with psycopg.connect(self.isolation.database_url, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_database(), current_user, inet_server_addr()::text, inet_server_port()")
                    database, user, address, port = cursor.fetchone()
            self.redactor.write_json(
                state_dir / "postgres-identity.json",
                {"database": database, "user": user, "address": address, "port": port, "compose_project": self.isolation.project_name},
            )
        except Exception as exc:
            failures.append(f"PostgreSQL identity check failed: {type(exc).__name__}: {exc}")
        try:
            request = urllib.request.Request(self.environment["ES_URL"].rstrip("/") + "/", headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read())
            self.redactor.write_json(
                state_dir / "elasticsearch-identity.json",
                {
                    "status": response.status,
                    "cluster_uuid": data.get("cluster_uuid"),
                    "version": (data.get("version") or {}).get("number"),
                    "compose_project": self.isolation.project_name,
                },
            )
        except Exception as exc:
            failures.append(f"Elasticsearch identity check failed: {type(exc).__name__}: {exc}")
        try:
            from neo4j import GraphDatabase

            driver = GraphDatabase.driver(
                self.environment["NEO4J_URI"],
                # This verifies the runner-owned store itself, not the product's
                # selected NEO4J_USER.  Keeping the Compose bootstrap principal
                # here lets the environment suite start the real stack and then
                # prove invalid product credentials through /admin/health.
                auth=("neo4j", self.environment["NEO4J_PASSWORD"]),
            )
            with driver:
                driver.verify_connectivity()
                record = driver.execute_query("RETURN 1 AS ok").records[0]
            self.redactor.write_json(
                state_dir / "neo4j-identity.json",
                {"ok": int(record["ok"]), "compose_project": self.isolation.project_name},
            )
        except Exception as exc:
            failures.append(f"Neo4j identity check failed: {type(exc).__name__}: {exc}")
        return [self.redactor.redact_text(item) for item in failures]

    def _wait_app(self) -> None:
        deadline = time.monotonic() + min(self.timeout_seconds, 120)
        probe_path = self.isolation.paths.profile_root / "services" / "health-probe.jsonl"
        rows: list[dict[str, Any]] = []
        health_url = self.isolation.base_url + "/healthz"
        while time.monotonic() < deadline:
            if self.app_process is not None and self.app_process.poll() is not None:
                thread = self._app_log_thread
                if thread is not None:
                    thread.join(timeout=5)
                rows.append({"status": 0, "error": f"process exited {self.app_process.returncode}"})
                self.redactor.write_json(probe_path.with_suffix(".json"), rows)
                raise StackFailure("app_start", f"FastAPI exited before readiness with {self.app_process.returncode}")
            try:
                with urllib.request.urlopen(health_url, timeout=3) as response:
                    rows.append({"status": response.status, "url": safe_url(health_url)})
                    if response.status == 200:
                        self.redactor.write_json(probe_path.with_suffix(".json"), rows)
                        return
            except urllib.error.HTTPError as exc:
                rows.append({"status": exc.code, "url": safe_url(health_url)})
            except OSError as exc:
                rows.append({"status": 0, "error": type(exc).__name__, "url": safe_url(health_url)})
            time.sleep(1)
        self.redactor.write_json(probe_path.with_suffix(".json"), rows)
        raise StackFailure("app_health", f"FastAPI health check timed out at {safe_url(health_url)}")

    def _run_logged(self, command: list[str], path: Path, *, timeout: int) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                command,
                cwd=self.repository,
                env=self.runner_environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else "command timed out"
            self._write_redacted_text(path, output + "\n")
            raise StackFailure("compose_timeout", f"command timed out after {timeout}s") from exc
        self._write_redacted_text(path, output)
        return completed

    def collect_compose_logs(self) -> None:
        path = self.isolation.paths.profile_root / "services" / "compose.log"
        if not self._compose_attempted:
            self._write_redacted_text(path, "Compose was not started for this profile.\n")
            return
        try:
            completed = subprocess.run(
                self.compose_command("logs", "--no-color", "--timestamps", profile="ocr" if self.enable_ocr else None),
                cwd=self.repository,
                env=self.runner_environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            output = f"compose log collection failed: {type(exc).__name__}: {exc}\n"
        self._write_redacted_text(path, output)

    def cleanup(self) -> list[str]:
        errors: list[str] = []
        try:
            self._stop_control_server()
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"control server cleanup failed: {type(exc).__name__}: {exc}")
        errors.extend(self._control_errors)
        try:
            self.collect_compose_logs()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"compose log collection failed: {type(exc).__name__}: {exc}")
        try:
            self._stop_app()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"app cleanup failed: {type(exc).__name__}: {exc}")
        app_absent = self.app_process is None and self._app_pid is None and self._app_start_ticks is None
        compose_absent = not self._compose_attempted
        if self._compose_attempted:
            try:
                self._verify_compose_ownership("before-down")
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                errors.append(f"compose ownership before cleanup failed: {type(exc).__name__}: {exc}")
            else:
                try:
                    completed = subprocess.run(
                        self.compose_command(
                            "down",
                            "--volumes",
                            "--remove-orphans",
                            "--timeout",
                            "15",
                            profile="ocr" if self.enable_ocr else None,
                        ),
                        cwd=self.repository,
                        env=self.runner_environment,
                        capture_output=True,
                        text=True,
                        timeout=90,
                        check=False,
                    )
                    output = (completed.stdout or "") + (completed.stderr or "")
                    self._write_redacted_text(
                        self.isolation.paths.profile_root / "services" / "cleanup.log",
                        output,
                    )
                    if completed.returncode != 0:
                        errors.append(f"compose cleanup exited {completed.returncode}")
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                    errors.append(f"compose cleanup failed: {type(exc).__name__}: {exc}")
                try:
                    self._verify_compose_ownership("after-down", require_absent=True)
                    compose_absent = True
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                    errors.append(f"compose resources remain after cleanup: {type(exc).__name__}: {exc}")
        secrets_scrubbed = False
        try:
            scrub = scrub_runtime_secret_files(
                self.isolation.paths.runtime_root,
                self._runtime_marker_contract(),
            )
            secrets_scrubbed = scrub.get("status") == "PASS"
            self.redactor.write_json(
                self.isolation.paths.profile_root / "state" / "runtime-secret-scrub.json",
                scrub,
            )
            for message in scrub.get("errors") or ():
                errors.append(str(message))
        except (OSError, RuntimeError, ValueError, IsolationViolation) as exc:
            errors.append(f"runtime secret scrub failed: {type(exc).__name__}: {exc}")
        if app_absent and compose_absent and secrets_scrubbed:
            try:
                self._remove_runtime()
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(f"runtime cleanup failed: {type(exc).__name__}: {exc}")
        else:
            errors.append("run-owned runtime retained because app/resource absence or runtime secret scrub was not proven")
        self._write_cleanup_recovery(
            errors,
            app_absent=app_absent,
            compose_absent=compose_absent,
            secrets_scrubbed=secrets_scrubbed,
        )
        return [self.redactor.redact_text(item) for item in errors]

    def _write_cleanup_recovery(
        self,
        errors: list[str],
        *,
        app_absent: bool,
        compose_absent: bool,
        secrets_scrubbed: bool,
    ) -> None:
        runtime = self.isolation.paths.runtime_root
        path = self.isolation.paths.profile_root / "state" / "cleanup-recovery.json"
        self.redactor.write_json(
            path,
            {
                "status": "PASS" if not errors else "FAIL",
                "project": self.isolation.project_name,
                "owner_hash": self.isolation.owner_hash,
                "app_absent": app_absent,
                "compose_resources_absent": compose_absent,
                "runtime_secrets_scrubbed": secrets_scrubbed,
                "runtime_retained": runtime.exists(),
                "runtime_name": runtime.name,
                "resources": self._last_compose_resources,
                "errors": [self.redactor.redact_text(item) for item in errors],
                "recovery_policy": "Only resources matching both project and owner labels may be removed.",
            },
        )
        chmod_path_no_follow(path, 0o600, require_owner_uid=os.geteuid())

    def _stop_app(self) -> None:
        assert_no_mount_targets(self.isolation.paths.runtime_root)
        errors: list[str] = []
        if self.app_process is not None:
            process = self.app_process
            expected_pid = self._app_pid
            expected_start_ticks = self._app_start_ticks
            in_memory_identity_ok = expected_pid == process.pid and expected_start_ticks is not None
            if not in_memory_identity_ok:
                errors.append("in-memory app process identity does not match the spawned process")
            else:
                if process.poll() is None and _process_start_ticks(process.pid) != expected_start_ticks:
                    in_memory_identity_ok = False
                    errors.append("live app PID start identity differs; signal delivery was refused")
            try:
                recorded_pid = self._read_app_pid()
                recorded_identity = self._read_app_pid_identity()
                if recorded_pid != expected_pid or recorded_identity != (expected_pid, expected_start_ticks):
                    errors.append("APP_PID_FILE or its start-identity sidecar differs from the spawned app")
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"application PID evidence validation failed: {type(exc).__name__}: {exc}")
            if in_memory_identity_ok:
                self._terminate_app_process(process)
            if process.poll() is not None:
                for path in (self._app_pid_path(), self._app_pid_identity_path()):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as exc:
                        errors.append(f"application PID evidence cleanup failed: {type(exc).__name__}: {exc}")
                self.app_process = None
                self._app_pid = None
                self._app_start_ticks = None
        if self._app_log_handle is not None:
            thread = self._app_log_thread
            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    errors.append("redacted app log collector did not stop")
                else:
                    self._app_log_handle.close()
                    self._app_log_handle = None
                    self._app_log_thread = None
            else:
                self._app_log_handle.close()
                self._app_log_handle = None
        if self._app_log_errors:
            errors.extend(self._app_log_errors)
            self._app_log_errors.clear()
        if errors:
            raise RuntimeError("; ".join(errors))

    def _copy_redacted_app_log(
        self,
        process: subprocess.Popen[str],
        handle,
    ) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                self._refresh_runtime_secrets()
                with self._app_log_lock:
                    handle.write(self.redactor.redact_text(line))
                    handle.flush()
        except (OSError, ValueError) as exc:
            self._app_log_errors.append(self.redactor.redact_text(f"redacted app log collection failed: {type(exc).__name__}: {exc}"))
        finally:
            stream.close()

    def _refresh_runtime_secrets(self) -> None:
        try:
            metadata = self.isolation.secret_registry.stat()
        except OSError:
            return
        signature = (metadata.st_mtime_ns, metadata.st_size)
        if signature == self._secret_registry_signature:
            return
        ingest_secret_registry(self.isolation.secret_registry, self.redactor)
        self._secret_registry_signature = signature

    def _write_redacted_text(self, path: Path, value: str) -> None:
        write_private_text_atomic(path, self.redactor.redact_text(value))

    def _runtime_marker_contract(self) -> dict[str, object]:
        return runtime_marker_payload(
            run_id=self.isolation.run_id,
            profile_name=self.isolation.profile.name,
            project_name=self.isolation.project_name,
            owner_hash=self.isolation.owner_hash,
            creator_pid=self.isolation.creator_pid,
            creator_start_ticks=self.isolation.creator_start_ticks,
        )

    def _remove_runtime(self) -> None:
        runtime = self.isolation.paths.runtime_root
        marker = self._runtime_marker_contract()
        validate_runtime_marker(runtime, marker)
        allowed_parent = runtime_parent_path()
        if runtime.parent != allowed_parent:
            raise RuntimeError("runtime directory is outside its dedicated parent")
        if not runtime.name.startswith("sherpa-ui-automation-"):
            raise RuntimeError("runtime directory name is not owned by UI automation")
        _remove_owned_runtime(
            runtime,
            allowed_parent,
            marker,
            docker_environment=self.environment,
            repository=self.repository,
        )


def re_safe_service(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character in "_-" for character in value)
