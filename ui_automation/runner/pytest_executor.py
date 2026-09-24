"""profileごとにpytestを別processで完走させる。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ui_automation.runner.artifacts import SecretRedactor, ingest_secret_registry, write_private_text_atomic
from ui_automation.runner.models import EnvProfile


_MARKERS = {
    "full": "ui_automation and not env_seed_lifecycle and not ingestion_real and not provider_profile_real",
    "smoke": "smoke",
    "chat": "chat or thought_flow",
    "env": "environment and not env_seed_lifecycle",
}
_FORBIDDEN_EXTRA_ARGS = {"-x", "--exitfirst", "--lf", "--last-failed", "--collect-only"}

_PYTEST_SUPERVISOR = r"""
import ctypes
import os
import select
import signal
import sys
import time

control_fd = int(sys.argv[1])
status_fd = int(sys.argv[2])
command = sys.argv[3:]
shutdown = False
shutdown_started = 0.0

def emit(message):
    try:
        os.write(status_fd, (message + "\n").encode("ascii"))
    except OSError:
        pass

def request_shutdown(_signum=None, _frame=None):
    global shutdown, shutdown_started
    if not shutdown:
        shutdown = True
        shutdown_started = time.monotonic()

def direct_children():
    try:
        raw = open(f"/proc/{os.getpid()}/task/{os.getpid()}/children", encoding="ascii").read()
    except OSError:
        return []
    return [int(value) for value in raw.split() if value.isdecimal()]

def stop_direct_children(requested_signal):
    for pid in direct_children():
        try:
            os.kill(pid, requested_signal)
        except ProcessLookupError:
            pass

def reap(main_pid, main_reported):
    has_children = False
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return False, main_reported
        if pid == 0:
            return True, main_reported
        if pid == main_pid and not main_reported:
            emit(f"EXIT:{os.waitstatus_to_exitcode(status)}")
            main_reported = True
        has_children = True

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    emit("ERROR:subreaper")
    os._exit(125)
for watched_signal in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(watched_signal, request_shutdown)
emit("READY")
started = False
main_pid = 0
main_reported = False
release_requested = False

while True:
    has_children, main_reported = reap(main_pid, main_reported)
    if release_requested and not has_children:
        emit("RELEASED")
        os._exit(0)
    if shutdown:
        elapsed = time.monotonic() - shutdown_started
        stop_direct_children(signal.SIGTERM if elapsed < 2.0 else signal.SIGKILL)
        if not has_children:
            os._exit(125)
    readable, _, _ = select.select([control_fd], [], [], 0.05)
    if not readable:
        continue
    try:
        token = os.read(control_fd, 1)
    except OSError:
        token = b""
    if not token:
        request_shutdown()
    elif token == b"S" and not started and not shutdown:
        main_pid = os.fork()
        if main_pid == 0:
            os.close(control_fd)
            os.close(status_fd)
            for reset_signal in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(reset_signal, signal.SIG_DFL)
            try:
                os.execve(command[0], command, os.environ)
            except OSError:
                os._exit(126)
        started = True
        emit("STARTED")
    elif token == b"R" and started:
        release_requested = True
    else:
        emit("ERROR:protocol")
        request_shutdown()
"""


@dataclass(frozen=True)
class PytestOutcome:
    exit_code: int
    counts: dict[str, int]
    timed_out: bool
    command: list[str]
    registry_errors: tuple[str, ...]
    interrupted: bool
    process_cleanup_errors: tuple[str, ...]
    process_leak_detected: bool


@dataclass(frozen=True, order=True)
class _ProcessIdentity:
    pid: int
    start_ticks: int
    process_group: int
    session: int
    uid: int


@dataclass(frozen=True)
class _ProcessSnapshot:
    identity: _ProcessIdentity
    parent_pid: int


@dataclass
class _SupervisorHandle:
    process: subprocess.Popen
    identity: _ProcessIdentity
    pidfd: int
    control_descriptor: int
    status_descriptor: int
    status_buffer: bytearray


@dataclass(frozen=True)
class _ProcessCleanupResult:
    status: str
    leak_detected: bool
    initial_count: int
    observed_count: int
    recovered_count: int
    term_signaled_count: int
    escalated_count: int
    final_count: int | None
    errors: tuple[str, ...]
    leader_identity_sha256: str | None
    supervisor_zombie_preserved: bool
    supervisor_reaped: bool
    supervisor_generation_pinned: bool

    def evidence(self) -> dict[str, object]:
        return {
            "status": self.status,
            "leak_detected": self.leak_detected,
            "initial_process_count": self.initial_count,
            "observed_process_count": self.observed_count,
            "recovered_process_count": self.recovered_count,
            "term_signaled_process_count": self.term_signaled_count,
            "escalated_process_count": self.escalated_count,
            "final_process_count": self.final_count,
            "errors": list(self.errors),
            "leader_identity_sha256": self.leader_identity_sha256,
            "supervisor_zombie_preserved": self.supervisor_zombie_preserved,
            "supervisor_reaped": self.supervisor_reaped,
            "supervisor_generation_pinned": self.supervisor_generation_pinned,
            "command_line_recorded": False,
            "raw_command_recorded": False,
        }


class _ProcessIdentityError(RuntimeError):
    """A procfs identity could not be established without a race."""


@dataclass(frozen=True)
class _SupervisorRecovery:
    supervisor_reaped: bool
    supervisor_zombie_preserved: bool
    supervisor_generation_pinned: bool
    descendant_free_proven: bool
    final_count: int | None
    errors: tuple[str, ...]


class _SupervisorLaunchError(RuntimeError):
    def __init__(self, recovery: _SupervisorRecovery | None):
        super().__init__("pytest supervisor launch failed")
        self.recovery = recovery


def _waitid_return_code(result: os.waitid_result) -> int:
    if result.si_code == os.CLD_EXITED:
        return int(result.si_status)
    if result.si_code in {os.CLD_KILLED, os.CLD_DUMPED}:
        return -int(result.si_status)
    raise _ProcessIdentityError("pytest leader produced an unsupported wait status")


def _observe_child_exit_without_reaping(process: subprocess.Popen) -> int | None:
    """Observe a direct child exit while retaining its zombie as the SID pin."""

    try:
        result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError as exc:
        raise _ProcessIdentityError("pytest leader was reaped before session cleanup") from exc
    except OSError as exc:
        raise _ProcessIdentityError("pytest leader exit status could not be observed") from exc
    if result is None:
        return None
    return _waitid_return_code(result)


def _wait_for_child_exit_without_reaping(process: subprocess.Popen, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while True:
        result = _observe_child_exit_without_reaping(process)
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def _signal_retained_pidfd(descriptor: int, requested_signal: signal.Signals) -> str | None:
    try:
        signal.pidfd_send_signal(descriptor, requested_signal, None, 0)
    except ProcessLookupError:
        return None
    except OSError:
        return "retained pytest leader pidfd signal delivery failed"
    return None


def _open_spawned_pidfd(process: subprocess.Popen) -> int:
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise _ProcessIdentityError("pidfd process cleanup is unavailable")
    try:
        return os.pidfd_open(process.pid, 0)
    except OSError as exc:
        raise _ProcessIdentityError("pytest leader pidfd could not be retained") from exc


def _stop_and_reap_spawned_leader(process: subprocess.Popen, leader_pidfd: int) -> tuple[bool, tuple[str, ...]]:
    """Bounded exact-process fallback used before pytest can spawn children."""

    errors: list[str] = []
    try:
        exit_code = _observe_child_exit_without_reaping(process)
    except _ProcessIdentityError as exc:
        return False, (str(exc),)
    for requested_signal, timeout in ((signal.SIGTERM, 6.0), (signal.SIGKILL, 2.0)):
        if exit_code is not None:
            break
        error = _signal_retained_pidfd(leader_pidfd, requested_signal)
        if error:
            errors.append(error)
        try:
            exit_code = _wait_for_child_exit_without_reaping(process, timeout)
        except _ProcessIdentityError as exc:
            errors.append(str(exc))
            break
    if exit_code is None:
        errors.append("pytest wrapper did not exit after bounded pidfd recovery")
        return False, tuple(dict.fromkeys(errors))
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, ChildProcessError):
        errors.append("pytest wrapper could not be reaped after bounded pidfd recovery")
        return False, tuple(dict.fromkeys(errors))
    return True, tuple(dict.fromkeys(errors))


def _drain_supervisor_status(descriptor: int, buffer: bytearray) -> tuple[str, ...]:
    lines: list[str] = []
    while True:
        try:
            chunk = os.read(descriptor, 4096)
        except BlockingIOError:
            break
        except OSError as exc:
            raise _ProcessIdentityError("pytest supervisor status pipe became unreadable") from exc
        if not chunk:
            break
        buffer.extend(chunk)
        while b"\n" in buffer:
            raw, _, remainder = buffer.partition(b"\n")
            buffer[:] = remainder
            try:
                lines.append(raw.decode("ascii"))
            except UnicodeDecodeError as exc:
                raise _ProcessIdentityError("pytest supervisor status was not ASCII") from exc
    return tuple(lines)


def _wait_for_supervisor_status(
    descriptor: int,
    buffer: bytearray,
    expected: str,
    *,
    timeout: float,
) -> tuple[str, ...]:
    deadline = time.monotonic() + timeout
    observed: list[str] = []
    while time.monotonic() < deadline:
        observed.extend(_drain_supervisor_status(descriptor, buffer))
        if expected in observed or any(line.startswith("ERROR:") for line in observed):
            return tuple(observed)
        time.sleep(0.02)
    return tuple(observed)


def _parse_junit(path: Path) -> tuple[dict[str, int], list[str]]:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    errors: list[str] = []
    if path.is_symlink() or not path.is_file():
        return counts, ["sanitized JUnit is missing or is a symlink"]
    if path.lstat().st_nlink != 1:
        return counts, ["sanitized JUnit is hardlinked"]
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return counts, ["sanitized JUnit is malformed or unreadable"]
    if root.tag not in {"testsuite", "testsuites"}:
        return counts, ["sanitized JUnit has an unsupported root element"]
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        return counts, ["sanitized JUnit contains no test suite"]
    for suite in suites:
        for key in counts:
            try:
                value = int(suite.attrib.get(key, 0))
            except ValueError:
                errors.append(f"sanitized JUnit has a non-integer {key} count")
                continue
            if value < 0:
                errors.append(f"sanitized JUnit has a negative {key} count")
                continue
            counts[key] += value
    if counts["tests"] <= 0:
        errors.append("sanitized JUnit contains zero tests")
    return counts, errors


def _write_sanitized_junit(raw_path: Path, destination: Path, redactor: SecretRedactor) -> None:
    """Redact parsed XML nodes so replacement markers are escaped on serialization."""

    root = ET.parse(raw_path).getroot()
    for element in root.iter():
        for key, value in tuple(element.attrib.items()):
            element.set(key, redactor.redact_text(value))
        if element.text is not None:
            element.text = redactor.redact_text(element.text)
        if element.tail is not None:
            element.tail = redactor.redact_text(element.tail)
    serialized = ET.tostring(root, encoding="unicode", xml_declaration=True)
    write_private_text_atomic(destination, serialized)


def marker_for(profile: EnvProfile, effective_suite: str) -> str:
    marker = profile.marker or _MARKERS[effective_suite]
    if marker == "environment":
        return "environment and not env_seed_lifecycle"
    return marker


def _parse_proc_snapshot(raw: str, *, expected_pid: int, uid: int) -> _ProcessSnapshot:
    """Parse Linux proc stat without treating spaces in ``comm`` as fields."""

    open_paren = raw.find("(")
    close_paren = raw.rfind(")")
    if open_paren <= 0 or close_paren <= open_paren:
        raise _ProcessIdentityError("proc stat has no unambiguous command boundary")
    try:
        parsed_pid = int(raw[:open_paren].strip())
        # The tail starts at field 3 (state). pgrp/session/starttime are
        # therefore indices 2/3/19, respectively.
        tail = raw[close_paren + 1 :].split()
        parent_pid = int(tail[1])
        process_group = int(tail[2])
        session_id = int(tail[3])
        start_ticks = int(tail[19])
    except (IndexError, ValueError) as exc:
        raise _ProcessIdentityError("proc stat has malformed identity fields") from exc
    if parsed_pid != expected_pid or parent_pid < 0 or min(process_group, session_id, start_ticks) <= 0:
        raise _ProcessIdentityError("proc stat identity fields are inconsistent")
    return _ProcessSnapshot(
        identity=_ProcessIdentity(
            pid=parsed_pid,
            start_ticks=start_ticks,
            process_group=process_group,
            session=session_id,
            uid=uid,
        ),
        parent_pid=parent_pid,
    )


def _parse_proc_stat(raw: str, *, expected_pid: int, uid: int) -> _ProcessIdentity:
    return _parse_proc_snapshot(raw, expected_pid=expected_pid, uid=uid).identity


def _read_process_identity(pid: int) -> _ProcessIdentity | None:
    proc_path = Path("/proc") / str(pid)
    try:
        first_raw = (proc_path / "stat").read_text(encoding="utf-8")
        metadata = proc_path.stat()
        second_raw = (proc_path / "stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, UnicodeError) as exc:
        raise _ProcessIdentityError("owned proc identity is unreadable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise _ProcessIdentityError("proc identity path is not a directory")
    first = _parse_proc_stat(first_raw, expected_pid=pid, uid=metadata.st_uid)
    second = _parse_proc_stat(second_raw, expected_pid=pid, uid=metadata.st_uid)
    if first != second:
        raise _ProcessIdentityError("proc identity changed while it was read")
    return second


def _read_process_snapshot(pid: int) -> _ProcessSnapshot | None:
    proc_path = Path("/proc") / str(pid)
    try:
        first_raw = (proc_path / "stat").read_text(encoding="utf-8")
        metadata = proc_path.stat()
        second_raw = (proc_path / "stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, UnicodeError) as exc:
        raise _ProcessIdentityError("owned proc snapshot is unreadable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise _ProcessIdentityError("proc snapshot path is not a directory")
    first = _parse_proc_snapshot(first_raw, expected_pid=pid, uid=metadata.st_uid)
    second = _parse_proc_snapshot(second_raw, expected_pid=pid, uid=metadata.st_uid)
    if first != second:
        raise _ProcessIdentityError("proc snapshot changed while it was read")
    return second


def _identity_sha256(identity: _ProcessIdentity) -> str:
    encoded = (f"{identity.pid}:{identity.start_ticks}:{identity.process_group}:{identity.session}:{identity.uid}").encode()
    return hashlib.sha256(encoded).hexdigest()


def _enumerate_owned_descendants(
    supervisor: _ProcessIdentity,
) -> tuple[tuple[_ProcessIdentity, ...], tuple[str, ...]]:
    """Return the current same-UID process tree rooted below a pinned supervisor."""

    errors: list[str] = []
    snapshots: list[_ProcessSnapshot] = []
    try:
        with os.scandir("/proc") as iterator:
            entries = list(iterator)
    except OSError:
        return (), ("procfs process enumeration failed",)
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            metadata = entry.stat(follow_symlinks=False)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            errors.append("a procfs entry could not be classified during descendant enumeration")
            continue
        if metadata.st_uid != supervisor.uid:
            continue
        try:
            snapshot = _read_process_snapshot(int(entry.name))
        except _ProcessIdentityError:
            errors.append("an owned proc snapshot could not be proven during descendant enumeration")
            continue
        if snapshot is not None and snapshot.identity.uid == supervisor.uid:
            snapshots.append(snapshot)

    supervisor_snapshot = next((item for item in snapshots if item.identity.pid == supervisor.pid), None)
    if supervisor_snapshot is None or supervisor_snapshot.identity != supervisor:
        errors.append("pytest supervisor identity was not pinned during descendant enumeration")
        return (), tuple(dict.fromkeys(errors))

    descendants: set[_ProcessIdentity] = set()
    lineage_pids = {supervisor.pid}
    pending = deque(snapshots)
    made_progress = True
    while made_progress:
        made_progress = False
        retained: deque[_ProcessSnapshot] = deque()
        while pending:
            snapshot = pending.popleft()
            if snapshot.identity == supervisor:
                continue
            if snapshot.parent_pid in lineage_pids:
                descendants.add(snapshot.identity)
                lineage_pids.add(snapshot.identity.pid)
                made_progress = True
            else:
                retained.append(snapshot)
        pending = retained
    return tuple(sorted(descendants)), tuple(dict.fromkeys(errors))


def _is_current_descendant(identity: _ProcessIdentity, supervisor: _ProcessIdentity) -> bool:
    """Revalidate a candidate's live PPID chain immediately before signaling."""

    current_pid = identity.pid
    expected_identity = identity
    seen: set[int] = set()
    while current_pid not in seen:
        seen.add(current_pid)
        try:
            snapshot = _read_process_snapshot(current_pid)
        except _ProcessIdentityError:
            return False
        if snapshot is None or snapshot.identity != expected_identity or snapshot.identity.uid != supervisor.uid:
            return False
        if snapshot.parent_pid == supervisor.pid:
            try:
                return _read_process_identity(supervisor.pid) == supervisor
            except _ProcessIdentityError:
                return False
        if snapshot.parent_pid <= 1:
            return False
        try:
            parent = _read_process_snapshot(snapshot.parent_pid)
        except _ProcessIdentityError:
            return False
        if parent is None or parent.identity.uid != supervisor.uid:
            return False
        current_pid = parent.identity.pid
        expected_identity = parent.identity
    return False


def _signal_process_identity(identity: _ProcessIdentity, requested_signal: signal.Signals) -> tuple[str, str | None]:
    """Signal exactly one revalidated process through a pidfd."""

    identity_hash = _identity_sha256(identity)
    try:
        current = _read_process_identity(identity.pid)
    except _ProcessIdentityError:
        return "refused", f"identity {identity_hash} could not be revalidated; signal delivery was refused"
    if current is None:
        return "gone", None
    if current != identity:
        return "refused", f"identity {identity_hash} changed; signal delivery was refused"
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return "refused", "pidfd signal delivery is unavailable"
    try:
        descriptor = os.pidfd_open(identity.pid, 0)
    except ProcessLookupError:
        return "gone", None
    except OSError:
        return "refused", f"pidfd for identity {identity_hash} could not be opened"
    try:
        try:
            current = _read_process_identity(identity.pid)
        except _ProcessIdentityError:
            return "refused", f"identity {identity_hash} became ambiguous after pidfd open"
        if current is None:
            return "gone", None
        if current != identity:
            return "refused", f"identity {identity_hash} changed after pidfd open; signal delivery was refused"
        try:
            signal.pidfd_send_signal(descriptor, requested_signal, None, 0)
        except ProcessLookupError:
            return "gone", None
        except OSError:
            return "refused", f"pidfd signal delivery failed for identity {identity_hash}"
    finally:
        os.close(descriptor)
    return "sent", None


def _capture_pytest_leader(process: subprocess.Popen) -> _ProcessIdentity:
    identity = _read_process_identity(process.pid)
    if identity is None:
        raise _ProcessIdentityError("pytest session leader exited before its identity was captured")
    if identity.uid != os.geteuid():
        raise _ProcessIdentityError("pytest session leader has an unexpected owner")
    if identity.pid != identity.process_group or identity.pid != identity.session:
        raise _ProcessIdentityError("pytest process is not a dedicated session and process-group leader")
    return identity


def _recover_supervisor_without_descendant_escape(
    *,
    process: subprocess.Popen,
    supervisor: _ProcessIdentity,
    supervisor_pidfd: int,
    known_descendants: set[_ProcessIdentity],
    timeout: float = 20.0,
) -> _SupervisorRecovery:
    """Ask the live subreaper to drain its tree; never SIGKILL the boundary."""

    errors: list[str] = []
    generation_pinned = True
    observed: set[_ProcessIdentity] = set(known_descendants)
    term_sent: set[_ProcessIdentity] = set()
    kill_sent: set[_ProcessIdentity] = set()
    supervisor_signal_error = _signal_retained_pidfd(supervisor_pidfd, signal.SIGTERM)
    if supervisor_signal_error:
        errors.append(supervisor_signal_error)
    deadline = time.monotonic() + timeout
    kill_after = time.monotonic() + min(5.0, timeout / 2)
    supervisor_exit: int | None = None
    while time.monotonic() < deadline:
        try:
            current = _read_process_identity(supervisor.pid)
        except _ProcessIdentityError:
            current = None
        if current != supervisor:
            generation_pinned = False
            errors.append("pytest supervisor generation was lost during bounded recovery")
            break
        try:
            supervisor_exit = _observe_child_exit_without_reaping(process)
        except _ProcessIdentityError as exc:
            errors.append(str(exc))
            break
        if supervisor_exit is not None:
            break
        members, scan_errors = _enumerate_owned_descendants(supervisor)
        errors.extend(scan_errors)
        observed.update(members)
        requested_signal = signal.SIGKILL if time.monotonic() >= kill_after else signal.SIGTERM
        signaled = kill_sent if requested_signal == signal.SIGKILL else term_sent
        for member in members:
            if member in signaled or not _is_current_descendant(member, supervisor):
                continue
            outcome, error = _signal_process_identity(member, requested_signal)
            if error:
                errors.append(error)
            if outcome == "sent":
                signaled.add(member)
        time.sleep(0.05)

    zombie_preserved = supervisor_exit is not None
    supervisor_reaped = False
    descendant_free_proven = False
    final_count: int | None = None
    if supervisor_exit in {0, 125} and generation_pinned:
        # The supervisor has only two normal exits: release after waitpid says
        # it has no child, or shutdown after recursively draining all adopted
        # children. A signal exit never constitutes a zero-child proof.
        alive_known: list[_ProcessIdentity] = []
        for candidate in observed:
            try:
                current = _read_process_identity(candidate.pid)
            except _ProcessIdentityError:
                current = None
            if current == candidate:
                alive_known.append(candidate)
        if alive_known:
            errors.append("known pytest descendants survived supervisor shutdown")
            final_count = len(alive_known)
        else:
            descendant_free_proven = True
            final_count = 0
        try:
            reaped_code = process.wait(timeout=2)
        except (subprocess.TimeoutExpired, ChildProcessError):
            errors.append("pytest supervisor could not be reaped after bounded recovery")
        else:
            supervisor_reaped = True
            if reaped_code != supervisor_exit:
                errors.append("pytest supervisor wait status changed during bounded recovery")
    elif supervisor_exit is not None:
        errors.append("pytest supervisor exited without a descendant-free attestation")
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, ChildProcessError):
            errors.append("unattested pytest supervisor could not be reaped")
        else:
            supervisor_reaped = True
    else:
        errors.append("pytest supervisor did not stop within bounded descendant recovery")
    return _SupervisorRecovery(
        supervisor_reaped=supervisor_reaped,
        supervisor_zombie_preserved=zombie_preserved,
        supervisor_generation_pinned=generation_pinned,
        descendant_free_proven=descendant_free_proven,
        final_count=final_count,
        errors=tuple(dict.fromkeys(errors)),
    )


def _reap_unreleased_supervisor_without_pidfd(
    process: subprocess.Popen,
    control_descriptor: int,
) -> _SupervisorRecovery:
    """Close the start gate, then reap the direct child without PID signaling."""

    errors = ["pytest supervisor pidfd was unavailable"]
    try:
        os.close(control_descriptor)
    except OSError:
        errors.append("unreleased pytest supervisor control gate could not be closed")
    try:
        exit_code = _wait_for_child_exit_without_reaping(process, 5)
    except _ProcessIdentityError as exc:
        errors.append(str(exc))
        exit_code = None
    if exit_code is None:
        errors.append("unreleased pytest supervisor did not exit after control EOF")
        return _SupervisorRecovery(
            supervisor_reaped=False,
            supervisor_zombie_preserved=False,
            supervisor_generation_pinned=True,
            descendant_free_proven=False,
            final_count=None,
            errors=tuple(dict.fromkeys(errors)),
        )
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, ChildProcessError):
        errors.append("unreleased pytest supervisor could not be reaped")
        reaped = False
    else:
        reaped = True
    # The only byte that authorizes fork was never sent. Irrespective of the
    # supervisor's own exit status, it therefore cannot have a descendant.
    return _SupervisorRecovery(
        supervisor_reaped=reaped,
        supervisor_zombie_preserved=True,
        supervisor_generation_pinned=True,
        descendant_free_proven=reaped,
        final_count=0 if reaped else None,
        errors=tuple(dict.fromkeys(errors)),
    )


def _launch_pytest_supervisor(
    *,
    trusted_python_executable: str,
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    output_handle: object,
) -> _SupervisorHandle:
    control_read, control_write = os.pipe()
    status_read, status_write = os.pipe()
    process: subprocess.Popen | None = None
    pidfd: int | None = None
    identity: _ProcessIdentity | None = None
    start_released = False
    recovery: _SupervisorRecovery | None = None
    try:
        process = subprocess.Popen(
            [trusted_python_executable, "-c", _PYTEST_SUPERVISOR, str(control_read), str(status_write), *command],
            cwd=cwd,
            env=environment,
            stdout=output_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            pass_fds=(control_read, status_write),
        )
        # Retain an exact handle before any protocol release can create pytest
        # descendants. Capture failure therefore still has a bounded safe stop.
        pidfd = _open_spawned_pidfd(process)
        identity = _capture_pytest_leader(process)
        os.close(control_read)
        control_read = -1
        os.close(status_write)
        status_write = -1
        os.set_blocking(status_read, False)
        status_buffer = bytearray()
        ready = _wait_for_supervisor_status(status_read, status_buffer, "READY", timeout=3)
        if "READY" not in ready or any(line.startswith("ERROR:") for line in ready):
            raise _ProcessIdentityError("pytest subreaper supervisor did not become ready")
        if os.write(control_write, b"S") != 1:
            raise _ProcessIdentityError("pytest supervisor start release was incomplete")
        start_released = True
        started = _wait_for_supervisor_status(status_read, status_buffer, "STARTED", timeout=3)
        if "STARTED" not in started or any(line.startswith("ERROR:") for line in started):
            raise _ProcessIdentityError("pytest supervisor did not attest child start")
        return _SupervisorHandle(
            process=process,
            identity=identity,
            pidfd=pidfd,
            control_descriptor=control_write,
            status_descriptor=status_read,
            status_buffer=status_buffer,
        )
    except Exception as exc:
        if process is not None and pidfd is not None and identity is not None and start_released:
            recovery = _recover_supervisor_without_descendant_escape(
                process=process,
                supervisor=identity,
                supervisor_pidfd=pidfd,
                known_descendants=set(),
            )
        elif process is not None and pidfd is not None:
            reaped, recovery_errors = _stop_and_reap_spawned_leader(process, pidfd)
            recovery = _SupervisorRecovery(
                supervisor_reaped=reaped,
                supervisor_zombie_preserved=reaped,
                supervisor_generation_pinned=reaped,
                descendant_free_proven=reaped,
                final_count=0 if reaped else None,
                errors=recovery_errors,
            )
        elif process is not None:
            # pidfd acquisition precedes the start release, so this process is
            # still a direct child with a closed fork gate and no descendants.
            recovery = _reap_unreleased_supervisor_without_pidfd(process, control_write)
            control_write = -1
        for descriptor in (control_write, status_read):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if pidfd is not None:
            os.close(pidfd)
        raise _SupervisorLaunchError(recovery) from exc
    finally:
        for descriptor in (control_read, status_write):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _cleanup_pytest_session(
    *,
    process: subprocess.Popen,
    leader: _ProcessIdentity,
    leader_pidfd: int,
    control_descriptor: int,
    status_descriptor: int,
    status_buffer: bytearray,
    main_exit_code_at_cleanup: int | None,
    known_descendants: set[_ProcessIdentity],
    preexisting_errors: tuple[str, ...] = (),
    term_grace_seconds: float = 10.0,
    kill_grace_seconds: float = 5.0,
) -> _ProcessCleanupResult:
    """Drain a subreaper-owned pytest tree before releasing its supervisor."""

    errors: list[str] = list(preexisting_errors)
    observed: set[_ProcessIdentity] = set()
    term_signaled: set[_ProcessIdentity] = set()
    escalated: set[_ProcessIdentity] = set()
    main_exit_code = main_exit_code_at_cleanup
    supervisor_zombie_preserved = False
    supervisor_generation_pinned = True
    supervisor_reaped = False
    released = False

    def consume_status() -> None:
        nonlocal main_exit_code, released
        for line in _drain_supervisor_status(status_descriptor, status_buffer):
            if line.startswith("EXIT:"):
                try:
                    parsed = int(line.removeprefix("EXIT:"))
                except ValueError:
                    errors.append("pytest supervisor reported a malformed exit status")
                else:
                    if main_exit_code is not None and main_exit_code != parsed:
                        errors.append("pytest supervisor reported conflicting pytest exit statuses")
                    main_exit_code = parsed
            elif line == "RELEASED":
                released = True
            elif line == "READY" or line == "STARTED" or line == "BUSY":
                continue
            elif line.startswith("ERROR:"):
                errors.append("pytest supervisor reported an internal protocol error")
            else:
                errors.append("pytest supervisor reported an unknown status")

    def prove_pinned_leader() -> bool:
        nonlocal supervisor_generation_pinned
        try:
            current = _read_process_identity(leader.pid)
        except _ProcessIdentityError:
            current = None
        if current != leader:
            supervisor_generation_pinned = False
            errors.append("pytest supervisor identity did not remain pinned through descendant cleanup")
            return False
        return True

    consume_status()
    if not prove_pinned_leader():
        initial: tuple[_ProcessIdentity, ...] = ()
        scan_errors: tuple[str, ...] = ()
    else:
        initial, scan_errors = _enumerate_owned_descendants(leader)
    errors.extend(scan_errors)
    observed.update(initial)
    known_descendants.update(initial)
    leak_detected = main_exit_code is not None and bool(initial)

    def signal_until_empty(requested_signal: signal.Signals, grace_seconds: float) -> tuple[_ProcessIdentity, ...]:
        deadline = time.monotonic() + grace_seconds
        signaled = term_signaled if requested_signal == signal.SIGTERM else escalated
        last_members: tuple[_ProcessIdentity, ...] = ()
        while True:
            consume_status()
            if not prove_pinned_leader():
                return last_members
            try:
                supervisor_exit = _observe_child_exit_without_reaping(process)
            except _ProcessIdentityError as exc:
                errors.append(str(exc))
                return last_members
            if supervisor_exit is not None:
                errors.append("pytest supervisor exited before descendant cleanup was released")
                exact_known: list[_ProcessIdentity] = []
                for candidate in known_descendants:
                    try:
                        current = _read_process_identity(candidate.pid)
                    except _ProcessIdentityError:
                        current = None
                    if current != candidate:
                        continue
                    exact_known.append(candidate)
                    if candidate not in signaled:
                        outcome, error = _signal_process_identity(candidate, requested_signal)
                        if error:
                            errors.append(error)
                        if outcome == "sent":
                            signaled.add(candidate)
                return tuple(sorted(exact_known))
            members, current_errors = _enumerate_owned_descendants(leader)
            errors.extend(current_errors)
            observed.update(members)
            known_descendants.update(members)
            last_members = members
            for member in members:
                if member in signaled:
                    continue
                if not _is_current_descendant(member, leader):
                    continue
                outcome, error = _signal_process_identity(member, requested_signal)
                if error:
                    errors.append(error)
                if outcome == "sent":
                    signaled.add(member)
            if not members or time.monotonic() >= deadline:
                return last_members
            time.sleep(0.05)

    remaining = signal_until_empty(signal.SIGTERM, term_grace_seconds)
    if remaining:
        remaining = signal_until_empty(signal.SIGKILL, kill_grace_seconds)

    if remaining and prove_pinned_leader():
        errors.append("pytest descendants survived direct pidfd recovery; supervisor fallback was required")
        fallback_error = _signal_retained_pidfd(leader_pidfd, signal.SIGTERM)
        if fallback_error:
            errors.append(fallback_error)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            consume_status()
            try:
                supervisor_exit = _observe_child_exit_without_reaping(process)
            except _ProcessIdentityError as exc:
                errors.append(str(exc))
                break
            if supervisor_exit is not None:
                supervisor_zombie_preserved = True
                break
            time.sleep(0.05)

    if prove_pinned_leader() and not supervisor_zombie_preserved:
        final_members, final_scan_errors = _enumerate_owned_descendants(leader)
        errors.extend(final_scan_errors)
    elif supervisor_zombie_preserved:
        exact_known = []
        for candidate in known_descendants:
            try:
                current = _read_process_identity(candidate.pid)
            except _ProcessIdentityError:
                current = None
            if current == candidate:
                exact_known.append(candidate)
        final_members = tuple(sorted(exact_known))
    else:
        final_members = remaining
    observed.update(final_members)
    known_descendants.update(final_members)

    if not final_members and prove_pinned_leader() and not supervisor_zombie_preserved:
        release_delivered = False
        try:
            if os.write(control_descriptor, b"R") != 1:
                errors.append("pytest supervisor cleanup release was incomplete")
            else:
                release_delivered = True
        except OSError:
            errors.append("pytest supervisor cleanup release could not be delivered")
        if release_delivered:
            try:
                release_status = _wait_for_supervisor_status(status_descriptor, status_buffer, "RELEASED", timeout=2)
            except _ProcessIdentityError as exc:
                errors.append(str(exc))
                release_status = ()
        else:
            release_status = ()
        for line in release_status:
            if line == "RELEASED":
                released = True
            elif line.startswith("ERROR:"):
                errors.append("pytest supervisor rejected its cleanup release")
        if not released:
            errors.append("pytest supervisor did not attest descendant-free release")
        try:
            supervisor_exit = _wait_for_child_exit_without_reaping(process, 2)
        except _ProcessIdentityError as exc:
            errors.append(str(exc))
            supervisor_exit = None
        supervisor_zombie_preserved = supervisor_exit is not None
        if supervisor_exit != 0:
            errors.append("pytest supervisor did not exit cleanly after release")

    if supervisor_zombie_preserved and prove_pinned_leader():
        try:
            reaped_code = process.wait(timeout=2)
        except (subprocess.TimeoutExpired, ChildProcessError):
            errors.append("pytest supervisor could not be reaped after descendant cleanup")
        else:
            supervisor_reaped = True
            if released and reaped_code != 0:
                errors.append("pytest supervisor wait status changed after release")

    final_count: int | None = len(final_members)
    if not supervisor_reaped:
        recovery = _recover_supervisor_without_descendant_escape(
            process=process,
            supervisor=leader,
            supervisor_pidfd=leader_pidfd,
            known_descendants=known_descendants,
        )
        errors.extend(recovery.errors)
        supervisor_zombie_preserved = recovery.supervisor_zombie_preserved
        supervisor_reaped = recovery.supervisor_reaped
        supervisor_generation_pinned = supervisor_generation_pinned and recovery.supervisor_generation_pinned
        if recovery.descendant_free_proven:
            final_members = ()
        final_count = recovery.final_count
    unique_errors = tuple(dict.fromkeys(errors))
    final_set = set(final_members)
    recovered = observed - final_set
    failed = leak_detected or bool(unique_errors) or final_count != 0 or not supervisor_reaped
    return _ProcessCleanupResult(
        status="FAIL" if failed else "PASS",
        leak_detected=leak_detected,
        initial_count=len(initial),
        observed_count=len(observed),
        recovered_count=len(recovered),
        term_signaled_count=len(term_signaled),
        escalated_count=len(escalated),
        final_count=final_count,
        errors=unique_errors,
        leader_identity_sha256=_identity_sha256(leader),
        supervisor_zombie_preserved=supervisor_zombie_preserved,
        supervisor_reaped=supervisor_reaped,
        supervisor_generation_pinned=supervisor_generation_pinned,
    )


def _cleanup_pytest_session_guarded(
    *,
    process: subprocess.Popen,
    leader: _ProcessIdentity,
    leader_pidfd: int,
    control_descriptor: int,
    status_descriptor: int,
    status_buffer: bytearray,
    main_exit_code_at_cleanup: int | None,
    known_descendants: set[_ProcessIdentity],
    preexisting_errors: tuple[str, ...] = (),
    term_grace_seconds: float = 10.0,
    kill_grace_seconds: float = 5.0,
) -> _ProcessCleanupResult:
    try:
        return _cleanup_pytest_session(
            process=process,
            leader=leader,
            leader_pidfd=leader_pidfd,
            control_descriptor=control_descriptor,
            status_descriptor=status_descriptor,
            status_buffer=status_buffer,
            main_exit_code_at_cleanup=main_exit_code_at_cleanup,
            known_descendants=known_descendants,
            preexisting_errors=preexisting_errors,
            term_grace_seconds=term_grace_seconds,
            kill_grace_seconds=kill_grace_seconds,
        )
    except Exception:
        if process.returncode is None:
            recovery = _recover_supervisor_without_descendant_escape(
                process=process,
                supervisor=leader,
                supervisor_pidfd=leader_pidfd,
                known_descendants=known_descendants,
            )
        else:
            recovery = _SupervisorRecovery(
                supervisor_reaped=True,
                supervisor_zombie_preserved=False,
                supervisor_generation_pinned=False,
                descendant_free_proven=False,
                final_count=None,
                errors=("pytest supervisor was already reaped before fallback proof",),
            )
        return _ProcessCleanupResult(
            status="FAIL",
            leak_detected=main_exit_code_at_cleanup is not None and bool(known_descendants),
            initial_count=len(known_descendants),
            observed_count=len(known_descendants),
            recovered_count=len(known_descendants) if recovery.descendant_free_proven else 0,
            term_signaled_count=0,
            escalated_count=0,
            final_count=recovery.final_count,
            errors=("pytest descendant cleanup raised an internal error", *recovery.errors),
            leader_identity_sha256=_identity_sha256(leader),
            supervisor_zombie_preserved=recovery.supervisor_zombie_preserved,
            supervisor_reaped=recovery.supervisor_reaped,
            supervisor_generation_pinned=recovery.supervisor_generation_pinned,
        )


def run_pytest_process_cleanup_self_check() -> dict[str, object]:
    """Exercise subreaper traversal, escape recovery, and PID reuse refusal."""

    def wait_main(handle: _SupervisorHandle, timeout: float = 5) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in _drain_supervisor_status(handle.status_descriptor, handle.status_buffer):
                if line.startswith("EXIT:"):
                    return int(line.removeprefix("EXIT:"))
                if line.startswith("ERROR:"):
                    raise AssertionError("self-check supervisor reported an error")
            time.sleep(0.02)
        raise AssertionError("self-check pytest child did not exit")

    def launch(script: str) -> _SupervisorHandle:
        return _launch_pytest_supervisor(
            trusted_python_executable=sys.executable,
            command=[sys.executable, "-c", script],
            cwd=Path.cwd(),
            environment=dict(os.environ),
            output_handle=subprocess.DEVNULL,
        )

    def close_handle(handle: _SupervisorHandle) -> None:
        for descriptor in (handle.control_descriptor, handle.status_descriptor, handle.pidfd):
            try:
                os.close(descriptor)
            except OSError:
                pass

    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sherpa-junit-redaction-", dir="/tmp") as junit_directory:
        junit_root = Path(junit_directory)
        raw_junit = junit_root / "raw.xml"
        sanitized_junit = junit_root / "sanitized.xml"
        secret = "runtime-secret-access-token"
        write_private_text_atomic(
            raw_junit,
            '<?xml version="1.0" encoding="utf-8"?>'
            '<testsuites><testsuite tests="1" failures="1" errors="0" skipped="0">'
            f'<testcase name="{secret}"><failure>access_token: "{secret}" &amp; retained</failure></testcase>'
            "</testsuite></testsuites>",
        )
        _write_sanitized_junit(raw_junit, sanitized_junit, SecretRedactor([secret]))
        parsed_junit = ET.parse(sanitized_junit).getroot()
        sanitized_text = sanitized_junit.read_text(encoding="utf-8")
        failure = parsed_junit.find("./testsuite/testcase/failure")
        testcase = parsed_junit.find("./testsuite/testcase")
        assert secret not in sanitized_text
        assert testcase is not None and testcase.attrib["name"] == "<redacted>"
        assert failure is not None and "<redacted>" in (failure.text or "") and "& retained" in (failure.text or "")
        counts, errors = _parse_junit(sanitized_junit)
        assert counts == {"tests": 1, "failures": 1, "errors": 0, "skipped": 0} and not errors
    checks.append("junit-redaction-preserves-valid-xml")
    synthetic = "321 (pytest child (with spaces)) S 1 654 654 0 -1 0 0 0 0 0 0 0 0 0 0 0 1 0 98765"
    parsed = _parse_proc_stat(synthetic, expected_pid=321, uid=os.geteuid())
    assert parsed.process_group == 654 and parsed.session == 654 and parsed.start_ticks == 98765
    checks.append("proc-stat-command-with-spaces")

    no_child = launch("import time; time.sleep(0.1)")
    try:
        no_child_exit = wait_main(no_child)
        assert no_child.process.returncode is None
        no_child_cleanup = _cleanup_pytest_session(
            process=no_child.process,
            leader=no_child.identity,
            leader_pidfd=no_child.pidfd,
            control_descriptor=no_child.control_descriptor,
            status_descriptor=no_child.status_descriptor,
            status_buffer=no_child.status_buffer,
            main_exit_code_at_cleanup=no_child_exit,
            known_descendants=set(),
            term_grace_seconds=1,
            kill_grace_seconds=1,
        )
        assert no_child_cleanup.status == "PASS"
        assert no_child_cleanup.final_count == 0 and no_child_cleanup.supervisor_reaped
        assert no_child_cleanup.supervisor_generation_pinned and no_child_cleanup.supervisor_zombie_preserved
    finally:
        close_handle(no_child)
    checks.append("supervisor-pinned-until-descendant-free-reap")

    leak_script = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,process_group=0);"
        "time.sleep(0.4)"
    )
    leaking_leader = launch(leak_script)
    try:
        leaking_exit = wait_main(leaking_leader)
        leak_cleanup = _cleanup_pytest_session(
            process=leaking_leader.process,
            leader=leaking_leader.identity,
            leader_pidfd=leaking_leader.pidfd,
            control_descriptor=leaking_leader.control_descriptor,
            status_descriptor=leaking_leader.status_descriptor,
            status_buffer=leaking_leader.status_buffer,
            main_exit_code_at_cleanup=leaking_exit,
            known_descendants=set(),
            term_grace_seconds=2,
            kill_grace_seconds=2,
        )
        assert leak_cleanup.leak_detected
        assert leak_cleanup.status == "FAIL"
        assert leak_cleanup.recovered_count >= 1 and leak_cleanup.final_count == 0
    finally:
        close_handle(leaking_leader)
    checks.append("normal-pytest-exit-leak-is-recovered-and-fails")

    escape_script = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True);"
        "time.sleep(0.4)"
    )
    escaping_leader = launch(escape_script)
    try:
        escaping_exit = wait_main(escaping_leader)
        escape_cleanup = _cleanup_pytest_session(
            process=escaping_leader.process,
            leader=escaping_leader.identity,
            leader_pidfd=escaping_leader.pidfd,
            control_descriptor=escaping_leader.control_descriptor,
            status_descriptor=escaping_leader.status_descriptor,
            status_buffer=escaping_leader.status_buffer,
            main_exit_code_at_cleanup=escaping_exit,
            known_descendants=set(),
            term_grace_seconds=2,
            kill_grace_seconds=2,
        )
        assert escape_cleanup.leak_detected and escape_cleanup.status == "FAIL"
        assert escape_cleanup.recovered_count >= 1 and escape_cleanup.final_count == 0
    finally:
        close_handle(escaping_leader)
    checks.append("setsid-escape-is-adopted-recovered-and-fails")

    mismatch_process = launch("import time; time.sleep(300)")
    try:
        deadline = time.monotonic() + 2
        members: tuple[_ProcessIdentity, ...] = ()
        while time.monotonic() < deadline:
            members, member_errors = _enumerate_owned_descendants(mismatch_process.identity)
            assert not member_errors
            if members:
                break
            time.sleep(0.02)
        assert members
        mismatch_identity = members[0]
        forged_identity = _ProcessIdentity(
            pid=mismatch_identity.pid,
            start_ticks=mismatch_identity.start_ticks + 1,
            process_group=mismatch_identity.process_group,
            session=mismatch_identity.session,
            uid=mismatch_identity.uid,
        )
        signal_outcome, signal_error = _signal_process_identity(forged_identity, signal.SIGTERM)
        assert signal_outcome == "refused" and signal_error
        mismatch_cleanup = _cleanup_pytest_session(
            process=mismatch_process.process,
            leader=mismatch_process.identity,
            leader_pidfd=mismatch_process.pidfd,
            control_descriptor=mismatch_process.control_descriptor,
            status_descriptor=mismatch_process.status_descriptor,
            status_buffer=mismatch_process.status_buffer,
            main_exit_code_at_cleanup=None,
            known_descendants=set(members),
            term_grace_seconds=2,
            kill_grace_seconds=2,
        )
        assert mismatch_cleanup.status == "PASS" and mismatch_cleanup.final_count == 0
    finally:
        close_handle(mismatch_process)
    checks.append("start-tick-mismatch-refuses-signal")

    fallback_process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    fallback_pidfd = _open_spawned_pidfd(fallback_process)
    try:
        fallback_reaped, fallback_errors = _stop_and_reap_spawned_leader(fallback_process, fallback_pidfd)
        assert fallback_reaped and not fallback_errors
    finally:
        os.close(fallback_pidfd)
    checks.append("pre-capture-pidfd-fallback-is-bounded-and-reaped")

    no_pidfd_control_read, no_pidfd_control_write = os.pipe()
    no_pidfd_status_read, no_pidfd_status_write = os.pipe()
    no_pidfd_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _PYTEST_SUPERVISOR,
            str(no_pidfd_control_read),
            str(no_pidfd_status_write),
            sys.executable,
            "-c",
            "import time; time.sleep(300)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=(no_pidfd_control_read, no_pidfd_status_write),
    )
    os.close(no_pidfd_control_read)
    os.close(no_pidfd_status_write)
    try:
        no_pidfd_recovery = _reap_unreleased_supervisor_without_pidfd(
            no_pidfd_process,
            no_pidfd_control_write,
        )
        no_pidfd_control_write = -1
        assert no_pidfd_recovery.supervisor_reaped
        assert no_pidfd_recovery.descendant_free_proven and no_pidfd_recovery.final_count == 0
        assert no_pidfd_process.returncode is not None
    finally:
        if no_pidfd_control_write >= 0:
            os.close(no_pidfd_control_write)
        os.close(no_pidfd_status_read)
    checks.append("pidfd-open-failure-closes-start-gate-and-reaps")

    from ui_automation.runner.policy import scan_source_policy

    with tempfile.TemporaryDirectory(prefix="sherpa-pytest-policy-", dir="/tmp") as policy_directory:
        policy_root = Path(policy_directory)
        (policy_root / "cases").mkdir()
        (policy_root / "support").mkdir()
        (policy_root / "cases" / "bad.py").write_text(
            "import subprocess\nsubprocess.Popen(['x'], start_new_session=True)\n",
            encoding="utf-8",
        )
        (policy_root / "support" / "allowed.py").write_text(
            "import subprocess\nsubprocess.Popen(['x'], process_group=0)\n",
            encoding="utf-8",
        )
        policy_violations = scan_source_policy(policy_root)
        assert any(item.path == "cases/bad.py" and item.rule == "pytest_supervision_escape" for item in policy_violations)
        assert not any(item.path == "support/allowed.py" for item in policy_violations)
    checks.append("cases-support-session-escape-policy-is-fail-closed")

    def assert_failed_but_reaped(result: _ProcessCleanupResult) -> None:
        assert result.status == "FAIL"
        assert result.final_count == 0
        assert result.supervisor_reaped and result.supervisor_generation_pinned

    release_failure = launch("import time; time.sleep(0.05)")
    try:
        release_failure_exit = wait_main(release_failure)
        os.close(release_failure.control_descriptor)
        release_failure.control_descriptor = -1
        release_failure_result = _cleanup_pytest_session_guarded(
            process=release_failure.process,
            leader=release_failure.identity,
            leader_pidfd=release_failure.pidfd,
            control_descriptor=-1,
            status_descriptor=release_failure.status_descriptor,
            status_buffer=release_failure.status_buffer,
            main_exit_code_at_cleanup=release_failure_exit,
            known_descendants=set(),
            term_grace_seconds=1,
            kill_grace_seconds=1,
        )
        assert_failed_but_reaped(release_failure_result)
    finally:
        close_handle(release_failure)
    checks.append("release-write-failure-is-recovered-and-fails")

    status_drop = launch("import time; time.sleep(0.05)")
    try:
        status_drop_exit = wait_main(status_drop)
        os.close(status_drop.status_descriptor)
        status_drop.status_descriptor = -1
        status_drop_result = _cleanup_pytest_session_guarded(
            process=status_drop.process,
            leader=status_drop.identity,
            leader_pidfd=status_drop.pidfd,
            control_descriptor=status_drop.control_descriptor,
            status_descriptor=-1,
            status_buffer=status_drop.status_buffer,
            main_exit_code_at_cleanup=status_drop_exit,
            known_descendants=set(),
            term_grace_seconds=1,
            kill_grace_seconds=1,
        )
        assert_failed_but_reaped(status_drop_result)
        assert "pytest descendant cleanup raised an internal error" in status_drop_result.errors
    finally:
        close_handle(status_drop)
    checks.append("status-drop-internal-fallback-is-recovered-and-fails")

    release_timeout = launch("import time; time.sleep(0.05)")
    dummy_read, dummy_write = os.pipe()
    try:
        release_timeout_exit = wait_main(release_timeout)
        release_timeout_result = _cleanup_pytest_session_guarded(
            process=release_timeout.process,
            leader=release_timeout.identity,
            leader_pidfd=release_timeout.pidfd,
            control_descriptor=dummy_write,
            status_descriptor=release_timeout.status_descriptor,
            status_buffer=release_timeout.status_buffer,
            main_exit_code_at_cleanup=release_timeout_exit,
            known_descendants=set(),
            term_grace_seconds=1,
            kill_grace_seconds=1,
        )
        assert_failed_but_reaped(release_timeout_result)
        assert "pytest supervisor did not attest descendant-free release" in release_timeout_result.errors
    finally:
        os.close(dummy_read)
        os.close(dummy_write)
        close_handle(release_timeout)
    checks.append("release-timeout-is-recovered-and-fails")

    internal_failure = launch("import time; time.sleep(0.05)")
    try:
        time.sleep(0.2)
        internal_failure_result = _cleanup_pytest_session_guarded(
            process=internal_failure.process,
            leader=internal_failure.identity,
            leader_pidfd=internal_failure.pidfd,
            control_descriptor=internal_failure.control_descriptor,
            status_descriptor=internal_failure.status_descriptor,
            status_buffer=None,  # type: ignore[arg-type]
            main_exit_code_at_cleanup=0,
            known_descendants=set(),
            term_grace_seconds=1,
            kill_grace_seconds=1,
        )
        assert_failed_but_reaped(internal_failure_result)
        assert "pytest descendant cleanup raised an internal error" in internal_failure_result.errors
    finally:
        close_handle(internal_failure)
    checks.append("cleanup-exception-is-recovered-with-zero-descendant-proof")

    return {"status": "PASS", "checks": checks, "check_count": len(checks)}


def run_pytest_profile(
    *,
    repository: Path,
    profile: EnvProfile,
    effective_suite: str,
    environment: dict[str, str],
    profile_root: Path,
    redactor: SecretRedactor,
    trusted_python_executable: str,
    cancel_requested: Callable[[], bool] | None = None,
) -> PytestOutcome:
    reports = profile_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    junit = reports / "junit.xml"
    guard = reports / "strict-outcomes.json"
    log = reports / "pytest.log"
    env = dict(environment)
    env["SHERPA_UI_PYTEST_GUARD_RESULT"] = str(guard)
    extra = list(profile.pytest_args)
    for value in extra:
        if value in _FORBIDDEN_EXTRA_ARGS or value.startswith("--maxfail") or value == "-n" or value.startswith("-n="):
            raise ValueError(f"profile pytest_args contains a forbidden early-exit/parallel option: {value}")
    cases_root = (repository / "ui_automation" / "cases").resolve()
    targets: list[str] = []
    for nodeid in profile.case_nodeids:
        relative = nodeid.split("::", 1)[0]
        target_path = (repository / "ui_automation" / relative).resolve()
        try:
            target_path.relative_to(cases_root)
        except ValueError as exc:
            raise ValueError(f"profile case_nodeid is outside ui_automation/cases: {nodeid}") from exc
        if not target_path.is_file():
            raise ValueError(f"profile case_nodeid file is missing: {nodeid}")
        targets.append(str(repository / "ui_automation" / nodeid))
    if not targets:
        targets = [str(cases_root)]
    else:
        env["SHERPA_UI_EXPECTED_NODEIDS_JSON"] = json.dumps(list(profile.case_nodeids))
    registry_path = Path(env["SHERPA_UI_SECRET_REGISTRY"])
    raw_junit_descriptor, raw_junit_name = tempfile.mkstemp(
        prefix=".pytest-junit-",
        suffix=".xml",
        dir=registry_path.parent,
        text=True,
    )
    os.fchmod(raw_junit_descriptor, 0o600)
    os.close(raw_junit_descriptor)
    raw_junit = Path(raw_junit_name)
    command = [
        trusted_python_executable,
        "-m",
        "pytest",
        "-c",
        str(repository / "ui_automation" / "pytest.ini"),
        *targets,
        "-m",
        marker_for(profile, effective_suite),
        "--continue-on-collection-errors",
        "--runxfail",
        "--junitxml",
        str(raw_junit),
        "-ra",
        "-p",
        "ui_automation.runner.pytest_guard",
        *extra,
    ]
    timeout = int(env.get("SHERPA_UI_SUITE_TIMEOUT_SECONDS", "7200"))
    timed_out = False
    interrupted = False
    raw_descriptor, raw_name = tempfile.mkstemp(
        prefix=".pytest-output-",
        suffix=".log",
        dir=registry_path.parent,
        text=True,
    )
    raw_log = Path(raw_name)
    os.fchmod(raw_descriptor, 0o600)
    supervisor: _SupervisorHandle | None = None
    exit_code = 1
    main_exit_code: int | None = None
    known_descendants: set[_ProcessIdentity] = set()
    monitor_errors: list[str] = []
    process_cleanup = _ProcessCleanupResult(
        status="FAIL",
        leak_detected=False,
        initial_count=0,
        observed_count=0,
        recovered_count=0,
        term_signaled_count=0,
        escalated_count=0,
        final_count=0,
        errors=("pytest process session was not established",),
        leader_identity_sha256=None,
        supervisor_zombie_preserved=False,
        supervisor_reaped=False,
        supervisor_generation_pinned=False,
    )
    try:
        try:
            with os.fdopen(raw_descriptor, "w", encoding="utf-8") as handle:
                try:
                    supervisor = _launch_pytest_supervisor(
                        trusted_python_executable=trusted_python_executable,
                        command=command,
                        cwd=repository,
                        environment=env,
                        output_handle=handle,
                    )
                except _SupervisorLaunchError as exc:
                    recovery = exc.recovery
                    process_cleanup = _ProcessCleanupResult(
                        status="FAIL",
                        leak_detected=False,
                        initial_count=0,
                        observed_count=0,
                        recovered_count=0,
                        term_signaled_count=0,
                        escalated_count=0,
                        final_count=recovery.final_count if recovery else None,
                        errors=("pytest supervisor launch failed", *(recovery.errors if recovery else ())),
                        leader_identity_sha256=None,
                        supervisor_zombie_preserved=recovery.supervisor_zombie_preserved if recovery else False,
                        supervisor_reaped=recovery.supervisor_reaped if recovery else False,
                        supervisor_generation_pinned=recovery.supervisor_generation_pinned if recovery else False,
                    )
                    raise
                deadline = time.monotonic() + timeout
                while True:
                    for line in _drain_supervisor_status(supervisor.status_descriptor, supervisor.status_buffer):
                        if line.startswith("EXIT:"):
                            try:
                                observed_exit = int(line.removeprefix("EXIT:"))
                            except ValueError as exc:
                                raise _ProcessIdentityError("pytest supervisor reported a malformed exit status") from exc
                            if main_exit_code is not None and main_exit_code != observed_exit:
                                raise _ProcessIdentityError("pytest supervisor reported conflicting exit statuses")
                            main_exit_code = observed_exit
                        elif line.startswith("ERROR:"):
                            raise _ProcessIdentityError("pytest supervisor reported an internal protocol error")
                        elif line not in {"READY", "STARTED", "BUSY"}:
                            raise _ProcessIdentityError("pytest supervisor reported an unknown status")
                    descendants, descendant_errors = _enumerate_owned_descendants(supervisor.identity)
                    known_descendants.update(descendants)
                    monitor_errors.extend(descendant_errors)
                    if main_exit_code is not None:
                        exit_code = main_exit_code
                        break
                    supervisor_exit = _observe_child_exit_without_reaping(supervisor.process)
                    if supervisor_exit is not None:
                        raise _ProcessIdentityError("pytest supervisor exited before reporting pytest completion")
                    if cancel_requested is not None and cancel_requested():
                        interrupted = True
                        exit_code = 130
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        exit_code = 124
                        break
                    time.sleep(min(0.05, remaining))
        finally:
            if supervisor is not None:
                try:
                    process_cleanup = _cleanup_pytest_session_guarded(
                        process=supervisor.process,
                        leader=supervisor.identity,
                        leader_pidfd=supervisor.pidfd,
                        control_descriptor=supervisor.control_descriptor,
                        status_descriptor=supervisor.status_descriptor,
                        status_buffer=supervisor.status_buffer,
                        main_exit_code_at_cleanup=main_exit_code,
                        known_descendants=known_descendants,
                        preexisting_errors=tuple(dict.fromkeys(monitor_errors)),
                    )
                finally:
                    for descriptor in (
                        supervisor.control_descriptor,
                        supervisor.status_descriptor,
                        supervisor.pidfd,
                    ):
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
            redactor.write_json(reports / "pytest-process-cleanup.json", process_cleanup.evidence())
    finally:
        # Child tests register runtime-created passwords/cookies here. Import
        # them before publishing stdout or JUnit, then expose only text that
        # has passed value-based redaction. Raw JUnit stays in the run-owned
        # secret area and is never placed under the artifact tree.
        _, registry_errors = ingest_secret_registry(registry_path, redactor)
        try:
            if registry_errors:
                # Runtime-created credentials cannot be proven absent from child
                # stdout/JUnit if the registry is unreadable. Never publish them.
                sanitized = "pytest output discarded because the runtime secret registry failed closed\n"
                exit_code = 1
            else:
                raw_log_metadata = raw_log.lstat()
                if raw_log.is_symlink() or raw_log_metadata.st_nlink != 1:
                    sanitized = "pytest evidence discarded because raw output failed filesystem boundaries\n"
                    exit_code = 1
                else:
                    raw_text = raw_log.read_text(encoding="utf-8", errors="replace")
                    sanitized = redactor.redact_text(raw_text)
                if raw_junit.is_symlink():
                    sanitized = "pytest evidence discarded because raw JUnit failed filesystem boundaries\n"
                    exit_code = 1
                elif raw_junit.is_file():
                    raw_junit_metadata = raw_junit.lstat()
                    if raw_junit_metadata.st_nlink != 1:
                        sanitized = "pytest evidence discarded because raw JUnit failed filesystem boundaries\n"
                        exit_code = 1
                    elif raw_junit_metadata.st_size:
                        try:
                            _write_sanitized_junit(raw_junit, junit, redactor)
                        except (ET.ParseError, OSError, UnicodeError):
                            sanitized = "pytest evidence discarded because raw JUnit was malformed or unreadable\n"
                            exit_code = 1
            write_private_text_atomic(log, sanitized)
        finally:
            raw_log.unlink(missing_ok=True)
            raw_junit.unlink(missing_ok=True)
    if process_cleanup.status != "PASS" and exit_code == 0:
        exit_code = 1
    if registry_errors:
        junit.unlink(missing_ok=True)
        guard.unlink(missing_ok=True)
        counts = {"tests": 0, "failures": 0, "errors": 1, "skipped": 0}
        junit_errors = ["runtime secret registry failed closed"]
    else:
        counts, junit_errors = _parse_junit(junit)
        if junit_errors:
            counts["errors"] = max(1, counts["errors"])
            if exit_code == 0:
                exit_code = 1
    redactor.write_json(
        reports / "junit-validation.json",
        {"status": "FAIL" if junit_errors else "PASS", "errors": junit_errors, "counts": counts},
    )
    guard_errors: list[str] = []
    guard_payload: dict = {}
    if guard.is_symlink() or not guard.is_file() or guard.lstat().st_nlink != 1:
        guard_errors.append("strict outcome evidence is missing or failed filesystem boundaries")
    else:
        try:
            loaded_guard = json.loads(guard.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            guard_errors.append("strict outcome evidence is malformed or unreadable")
        else:
            if not isinstance(loaded_guard, dict) or loaded_guard.get("status") not in {"PASS", "FAIL"}:
                guard_errors.append("strict outcome evidence violates its schema")
            else:
                guard_payload = loaded_guard
                if loaded_guard["status"] != "PASS":
                    guard_errors.append("strict outcome plugin reported a forbidden outcome")
    if guard_errors:
        redactor.write_json(
            guard,
            {"status": "FAIL", "reasons": guard_errors, "original_status": guard_payload.get("status")},
        )
        if exit_code == 0:
            exit_code = 1
    if counts["skipped"] and exit_code == 0:
        exit_code = 1
    redactor.write_json(
        reports / "pytest-result.json",
        {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "interrupted": interrupted,
            "counts": counts,
            "secret_registry_error_count": len(registry_errors),
            "junit_errors": junit_errors,
            "strict_guard_errors": guard_errors,
            "process_cleanup_status": process_cleanup.status,
            "process_leak_detected": process_cleanup.leak_detected,
            "process_cleanup_error_count": len(process_cleanup.errors),
            "marker": marker_for(profile, effective_suite),
            "command": [Path(command[0]).name, *command[1:]],
        },
    )
    return PytestOutcome(
        exit_code=exit_code,
        counts=counts,
        timed_out=timed_out,
        command=command,
        registry_errors=tuple(registry_errors),
        interrupted=interrupted,
        process_cleanup_errors=process_cleanup.errors,
        process_leak_detected=process_cleanup.leak_detected,
    )
