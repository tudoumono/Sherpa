"""通常開発環境へ接続しないrun固有のenv、port、pathを構築する。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from ui_automation.runner.artifacts import write_private_text_atomic
from ui_automation.runner.filesystem_safety import (
    assert_no_mount_targets,
    assert_no_unsafe_hardlinks,
    chmod_path_no_follow,
    chmod_tree_no_follow,
    rmtree_no_follow,
)
from ui_automation.runner.models import EnvProfile, RunPaths


class IsolationViolation(RuntimeError):
    pass


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROJECT_PART = re.compile(r"[^a-z0-9_-]+")
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_OWNER_LABEL = "com.sherpa.ui-automation.owner"
RUNTIME_MARKER_NAME = ".ui-automation-runtime.json"
_DOCKER_ENDPOINT_ENV = {
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
}


def _read_private_json_no_follow(path: Path, *, description: str) -> object:
    """Read runner-owned JSON from the exact inode that passed validation."""

    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > 1024 * 1024
    ):
        raise IsolationViolation(f"{description} is not a private single-link regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > 1024 * 1024
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
            if total > 1024 * 1024:
                raise IsolationViolation(f"{description} exceeds the safety limit")
            chunks.append(chunk)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise IsolationViolation(f"{description} changed while it was read")
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IsolationViolation(f"{description} is not valid UTF-8 JSON") from exc
    finally:
        os.close(descriptor)


LOCAL_DOCKER_SOCKET = Path("/var/run/docker.sock")
LOCAL_DOCKER_ENDPOINT = f"unix://{LOCAL_DOCKER_SOCKET}"
_LOCKED_PATHS = {
    "HOME",
    "SHERPA_KB_DIR",
    "SHERPA_USERS_DIR",
    "SHERPA_DERIVED_DIR",
    "SHERPA_OBSERVATION_DIR",
    "CODEX_HOME",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "PLAYWRIGHT_BROWSERS_PATH",
}
_LOCKED_PROJECT = {"COMPOSE_PROJECT_NAME", "SHERPA_COMPOSE_PROJECT"}
_ISOLATED_STORE_IDENTITIES = {
    "NEO4J_USER": "neo4j",
    "PGDATABASE": "sherpa",
    "PGUSER": "sherpa",
}
_POSTGRES_DISCRETE_IDENTITY_VARIABLES = {"PGDATABASE", "PGUSER"}
_PAIRWISE_ISOLATED_RUNTIME_BY_VARIABLE = {
    "DATABASE_URL": {"database_localhost", "database_loopback"},
    "ES_URL": {"es_localhost", "es_loopback"},
    "NEO4J_URI": {"neo4j_localhost", "neo4j_loopback"},
    "SHERPA_ENV_FILE": {"env_file_primary", "env_file_secondary"},
    "SHERPA_PORT": {"app_port_primary", "app_port_secondary"},
}
_SHARED_PLAYWRIGHT_CACHE_PREFIX = "sherpa-ui-playwright-cache-"
_SHARED_PLAYWRIGHT_CACHE_MARKER = ".ui-automation-shared-playwright.json"


def _runner_environment(
    product_environment: dict[str, str],
    *,
    trusted_path: str,
    trusted_python_executable: str,
) -> dict[str, str]:
    environment = dict(product_environment)
    environment["PATH"] = trusted_path
    environment["PYTHON"] = trusted_python_executable
    environment["PYTHON_BIN"] = trusted_python_executable
    return environment


def run_runner_environment_boundary_self_check() -> dict[str, object]:
    product = {"PATH": "/product/invalid", "PYTHON_BIN": "/product/missing", "KEEP": "unchanged"}
    runner = _runner_environment(
        product,
        trusted_path="/runner/bin",
        trusted_python_executable="/runner/python",
    )
    checks = {
        "product_path_unchanged": product["PATH"] == "/product/invalid",
        "product_python_unchanged": product["PYTHON_BIN"] == "/product/missing",
        "runner_path_separated": runner["PATH"] == "/runner/bin",
        "runner_python_separated": runner["PYTHON_BIN"] == runner["PYTHON"] == "/runner/python",
        "unrelated_values_preserved": runner["KEEP"] == "unchanged",
    }
    if not all(checks.values()):
        raise AssertionError("runner/product execution environment boundary self-check failed")
    return {"status": "PASS", "checks": checks, "check_count": len(checks), "raw_values_recorded": False}


def _read_env_file(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.is_file():
        raise FileNotFoundError(f"specified env file is missing: {path}")
    values: dict[str, str] = {}
    for line_number, original in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"unsupported env line {path}:{line_number}")
        key, raw = line.split("=", 1)
        key = key.strip()
        if not _ENV_NAME.fullmatch(key):
            raise ValueError(f"invalid env name {key!r} at {path}:{line_number}")
        if "$(" in raw or "`" in raw or "\n" in raw or "\r" in raw:
            raise ValueError(f"executable or multiline env values are not allowed at {path}:{line_number}")
        try:
            tokens = shlex.split(raw, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError(f"invalid quoting at {path}:{line_number}: {exc}") from exc
        if not tokens:
            value = ""
        elif len(tokens) == 1:
            value = tokens[0]
        else:
            raise ValueError(f"unquoted whitespace is not allowed at {path}:{line_number}")
        values[key] = value
    return values


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={shlex.quote(value)}" for key, value in sorted(values.items())]
    write_private_text_atomic(path, "\n".join(lines) + ("\n" if lines else ""))


def _write_private_json(path: Path, value: object) -> None:
    write_private_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_compose_override(path: Path, owner_hash: str) -> None:
    label = f"      {COMPOSE_OWNER_LABEL}: {json.dumps(owner_hash)}"
    services = "\n".join(f"  {name}:\n    labels:\n{label}" for name in ("postgres", "neo4j", "elasticsearch", "ocr-worker"))
    volumes = "\n".join(f"  {name}:\n    labels:\n{label}" for name in ("pg", "neo4j", "es"))
    networks = "\n".join(f"  {name}:\n    labels:\n{label}" for name in ("default", "ocr-internal"))
    write_private_text_atomic(
        path,
        f"services:\n{services}\nvolumes:\n{volumes}\nnetworks:\n{networks}\n",
    )


def _copy_read_only_tree(source: Path, target: Path) -> None:
    """外部browser binaryをrun-owned copyへ閉じ込め、元cacheを実行時に触らせない。"""
    shutil.copytree(source, target, symlinks=False)
    assert_no_mount_targets(target)
    assert_no_unsafe_hardlinks(target)
    for path in sorted(target.rglob("*"), reverse=True):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise IsolationViolation("copied Playwright cache contains an unsafe entry")
        mode = stat.S_IMODE(metadata.st_mode)
        desired = (mode & ~0o222) | stat.S_IRUSR | (stat.S_IXUSR if stat.S_ISDIR(metadata.st_mode) else 0)
        chmod_path_no_follow(path, desired, require_owner_uid=os.geteuid())
    mode = stat.S_IMODE(target.lstat().st_mode)
    chmod_path_no_follow(target, (mode & ~0o222) | stat.S_IRUSR | stat.S_IXUSR, require_owner_uid=os.geteuid())


def _allocate_ports(count: int) -> list[int]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [int(sock.getsockname()[1]) for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def local_docker_socket_identity() -> dict[str, object]:
    """Runnerが許可するlocal Docker Unix socketを検証し、非秘密identityを返す。"""

    if LOCAL_DOCKER_SOCKET.is_symlink():
        raise IsolationViolation("local Docker socket must not be a symlink")
    try:
        metadata = LOCAL_DOCKER_SOCKET.lstat()
    except OSError as exc:
        raise IsolationViolation("local Docker socket is unavailable") from exc
    if not stat.S_ISSOCK(metadata.st_mode):
        raise IsolationViolation("local Docker endpoint is not a Unix socket")
    identity_material = (
        f"{LOCAL_DOCKER_SOCKET.resolve()}\0{metadata.st_dev}\0{metadata.st_ino}\0"
        f"{metadata.st_uid}\0{metadata.st_gid}\0{stat.S_IMODE(metadata.st_mode)}"
    )
    return {
        "endpoint_sha256": hashlib.sha256(LOCAL_DOCKER_ENDPOINT.encode("utf-8")).hexdigest(),
        "socket_path_sha256": hashlib.sha256(str(LOCAL_DOCKER_SOCKET.resolve()).encode("utf-8")).hexdigest(),
        "socket_identity_sha256": hashlib.sha256(identity_material.encode("utf-8")).hexdigest(),
        "socket_mode": oct(stat.S_IMODE(metadata.st_mode)),
        "socket_uid": metadata.st_uid,
        "socket_gid": metadata.st_gid,
        "remote": False,
    }


def verify_local_docker_environment(
    environment: dict[str, str],
    *,
    expected_socket_identity_sha256: str | None = None,
) -> dict[str, object]:
    """全Docker入口でrunner固定のlocal socket以外をfail-closedにする。"""

    if environment.get("DOCKER_HOST") != LOCAL_DOCKER_ENDPOINT:
        raise IsolationViolation("Docker endpoint differs from the runner-fixed local Unix socket")
    forbidden = sorted(name for name in _DOCKER_ENDPOINT_ENV - {"DOCKER_HOST"} if name in environment)
    if forbidden:
        raise IsolationViolation("Docker context/TLS/config overrides are forbidden: " + ", ".join(forbidden))
    identity = local_docker_socket_identity()
    if expected_socket_identity_sha256 and identity["socket_identity_sha256"] != expected_socket_identity_sha256:
        raise IsolationViolation("local Docker socket identity changed during the profile")
    return identity


def local_docker_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Ambient Docker discoveryを除去したlocal-daemon専用environmentを返す。"""

    environment = dict(os.environ if base is None else base)
    for name in _DOCKER_ENDPOINT_ENV:
        environment.pop(name, None)
    environment["DOCKER_HOST"] = LOCAL_DOCKER_ENDPOINT
    return environment


def runtime_parent_path() -> Path:
    """通常runtimeを作成・回収してよい単一のabsolute parentを返す。"""

    configured = os.environ.get("SHERPA_UI_RUNTIME_PARENT", "").strip()
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute() or candidate.is_symlink():
            raise IsolationViolation("SHERPA_UI_RUNTIME_PARENT must be an absolute non-symlink directory")
    else:
        candidate = Path(tempfile.gettempdir())
    if not candidate.is_dir():
        raise IsolationViolation("UI automation runtime parent is unavailable")
    return candidate.resolve(strict=True)


def runtime_marker_payload(
    *,
    run_id: str,
    profile_name: str,
    project_name: str,
    owner_hash: str,
    creator_pid: int,
    creator_start_ticks: int,
) -> dict[str, object]:
    return {
        "schema": 2,
        "kind": "ui-automation-profile-runtime",
        "run_id": run_id,
        "profile": profile_name,
        "project": project_name,
        "owner_hash": owner_hash,
        "creator_pid": creator_pid,
        "creator_start_ticks": creator_start_ticks,
    }


def validate_runtime_marker(runtime: Path, expected: dict[str, object] | None = None) -> dict[str, object]:
    """Direct-child runtimeと0600 ownership markerを値一致で検証する。"""

    if runtime.is_symlink() or not runtime.is_dir() or runtime.resolve() != runtime:
        raise IsolationViolation("runtime root is missing or unsafe")
    metadata = runtime.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise IsolationViolation("runtime root ownership or private mode differs")
    marker = runtime / RUNTIME_MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        raise IsolationViolation("runtime ownership marker is missing or unsafe")
    try:
        payload = _read_private_json_no_follow(marker, description="runtime ownership marker")
    except (OSError, IsolationViolation) as exc:
        raise IsolationViolation("runtime ownership marker is unreadable") from exc
    if not isinstance(payload, dict):
        raise IsolationViolation("runtime ownership marker is not an object")
    if expected is not None and payload != expected:
        raise IsolationViolation("runtime ownership marker differs from the expected profile")
    return payload


def validate_runtime_marker_contract(
    runtime: Path,
    *,
    allow_legacy: bool = False,
) -> tuple[dict[str, object], bool]:
    """Markerの自己整合性を検証し、現行schemaかどうかを返す。"""

    payload = validate_runtime_marker(runtime)
    run_id = payload.get("run_id")
    profile_name = payload.get("profile")
    project_name = payload.get("project")
    owner_hash = payload.get("owner_hash")
    if not all(isinstance(value, str) and value for value in (run_id, profile_name, project_name)):
        raise IsolationViolation("runtime ownership marker has invalid identity fields")
    current_project = _compose_project_name(str(run_id), str(profile_name))
    legacy_project = _legacy_compose_project_name(str(run_id), str(profile_name))
    if project_name != current_project and not (allow_legacy and project_name == legacy_project):
        raise IsolationViolation("runtime ownership marker project is not derived from run/profile")
    if not runtime.name.startswith(str(project_name) + "-"):
        raise IsolationViolation("runtime directory name does not match its marker project")
    if not isinstance(owner_hash, str) or re.fullmatch(r"[0-9a-f]{64}", owner_hash) is None:
        if not allow_legacy:
            raise IsolationViolation("runtime ownership marker owner hash is invalid")
        owner_hash = None

    creator_pid = payload.get("creator_pid")
    creator_start_ticks = payload.get("creator_start_ticks")
    current_schema = (
        payload.get("schema") == 2
        and payload.get("kind") == "ui-automation-profile-runtime"
        and isinstance(creator_pid, int)
        and not isinstance(creator_pid, bool)
        and creator_pid > 1
        and isinstance(creator_start_ticks, int)
        and not isinstance(creator_start_ticks, bool)
        and creator_start_ticks > 0
        and owner_hash is not None
    )
    if current_schema:
        if project_name != current_project:
            raise IsolationViolation("current runtime marker uses a legacy project identity")
        expected = runtime_marker_payload(
            run_id=str(run_id),
            profile_name=str(profile_name),
            project_name=str(project_name),
            owner_hash=str(owner_hash),
            creator_pid=int(creator_pid),
            creator_start_ticks=int(creator_start_ticks),
        )
        if payload != expected:
            raise IsolationViolation("runtime ownership marker has unknown current-schema fields")
        return payload, True
    if not allow_legacy:
        raise IsolationViolation("runtime ownership marker is not the current schema")
    legacy_keys = {"run_id", "profile", "project", "owner_hash"}
    if set(payload) not in ({"run_id", "profile", "project"}, legacy_keys):
        raise IsolationViolation("runtime ownership marker is neither current nor recognized legacy schema")
    return payload, False


def scrub_runtime_secret_files(runtime: Path, expected_marker: dict[str, object]) -> dict[str, object]:
    """所有marker一致後、credentialを含み得るrun-owned file/treeだけを必ず消す。"""

    validate_runtime_marker(runtime, expected_marker)
    assert_no_mount_targets(runtime)
    removed: list[str] = []
    errors: list[str] = []
    direct_targets = [runtime / "user-home", runtime / "secrets"]
    direct_targets.extend(path for path in runtime.iterdir() if path.name.startswith("profile") and path.suffix == ".env")
    planned: list[tuple[Path, str]] = []
    for target in direct_targets:
        if target.parent != runtime:
            errors.append(f"secret scrub refused a non-direct runtime target: {target.name}")
            continue
        try:
            if target.is_symlink() or target.is_file():
                planned.append((target, "unlink"))
            elif target.is_dir():
                assert_no_mount_targets(target)
                planned.append((target, "tree"))
            elif target.exists():
                raise IsolationViolation("secret scrub target is a special filesystem entry")
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(f"secret scrub preflight failed for {target.name}: {type(exc).__name__}: {exc}")
    if errors:
        return {
            "status": "FAIL",
            "removed": [],
            "errors": errors,
            "raw_secret_values_recorded": False,
        }
    assert_no_mount_targets(runtime)
    for target, operation in planned:
        try:
            assert_no_mount_targets(runtime)
            if operation == "unlink":
                target.unlink(missing_ok=True)
            else:
                assert_no_mount_targets(target)
                rmtree_no_follow(target)
            removed.append(target.name)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(f"secret scrub failed for {target.name}: {type(exc).__name__}: {exc}")
    return {
        "status": "PASS" if not errors else "FAIL",
        "removed": sorted(set(removed)),
        "errors": errors,
        "raw_secret_values_recorded": False,
    }


def directory_integrity_snapshot(
    root: Path,
    *,
    include_content: bool,
    require_owner: bool = False,
    require_read_only: bool = False,
) -> dict[str, object]:
    """Directory treeをsymlinkなしで検査し、metadataと任意のcontent hashへ畳み込む。"""

    if root.is_symlink() or not root.is_dir():
        raise IsolationViolation("browser cache root must be a non-symlink directory")
    assert_no_mount_targets(root)
    metadata_digest = hashlib.sha256()
    content_digest = hashlib.sha256() if include_content else None
    count = 0
    total_bytes = 0
    owner_ok = True
    read_only = True
    expected_uid = os.geteuid()
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else str(path.relative_to(root))
        metadata = path.lstat()
        if path.is_symlink():
            raise IsolationViolation("browser cache must not contain symlinks")
        kind = "directory" if path.is_dir() else "file" if path.is_file() else "other"
        if kind == "other":
            raise IsolationViolation("browser cache must contain only files and directories")
        mode = stat.S_IMODE(metadata.st_mode)
        owner_ok = owner_ok and metadata.st_uid == expected_uid
        read_only = read_only and mode & 0o222 == 0
        if kind == "directory":
            read_only = read_only and mode & stat.S_IRUSR != 0 and mode & stat.S_IXUSR != 0
        else:
            read_only = read_only and mode & stat.S_IRUSR != 0
            total_bytes += metadata.st_size
        metadata_digest.update(
            f"{relative}\0{kind}\0{metadata.st_size}\0{metadata.st_mtime_ns}\0{mode}\0{metadata.st_uid}\0{metadata.st_gid}\n".encode()
        )
        if content_digest is not None:
            content_size = metadata.st_size if kind == "file" else 0
            content_digest.update(f"{relative}\0{kind}\0{content_size}\0".encode())
        if kind == "file":
            if content_digest is not None:
                before = path.stat()
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        content_digest.update(chunk)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise IsolationViolation("browser cache file changed while its content was hashed")
                content_digest.update(b"\0")
        count += 1
    if require_owner and not owner_ok:
        raise IsolationViolation("shared browser cache contains entries not owned by the runner user")
    if require_read_only and not read_only:
        raise IsolationViolation("shared browser cache contains writable or owner-inaccessible entries")
    return {
        "metadata_sha256": metadata_digest.hexdigest(),
        "content_sha256": content_digest.hexdigest() if content_digest is not None else None,
        "entry_count": count,
        "total_bytes": total_bytes,
        "owner_ok": owner_ok,
        "read_only": read_only,
    }


def directory_metadata_signature(root: Path) -> tuple[str, int]:
    """互換用: metadataと全file contentを単一signatureへ畳み込む。"""

    snapshot = directory_integrity_snapshot(root, include_content=True)
    digest = hashlib.sha256()
    digest.update(str(snapshot["metadata_sha256"]).encode())
    digest.update(b"\0")
    digest.update(str(snapshot["content_sha256"]).encode())
    return digest.hexdigest(), int(snapshot["entry_count"])


@dataclass(frozen=True)
class SharedPlaywrightCache:
    run_id: str
    runtime_parent: Path
    root: Path
    marker: Path
    cache_path: Path
    source_path: Path
    owner_hash: str
    creator_pid: int
    creator_start_ticks: int
    source_metadata_sha256: str
    source_content_sha256: str
    source_entry_count: int
    source_total_bytes: int
    cache_metadata_sha256: str
    cache_content_sha256: str
    cache_entry_count: int
    cache_total_bytes: int
    copy_count: int = 1


def _process_start_ticks(pid: int) -> int | None:
    """LinuxのPID再利用を区別できるprocess start identityを返す。"""

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


def _shared_playwright_marker_contract(
    *,
    run_id: str,
    owner_hash: str,
    root_name: str,
    creator_pid: int,
    creator_start_ticks: int,
    state: str,
) -> dict[str, object]:
    return {
        "schema": 2,
        "kind": "playwright-browser-cache",
        "state": state,
        "run_id": run_id,
        "owner_hash": owner_hash,
        "root_name": root_name,
        "cache_relative": "playwright-browsers",
        "copy_count": 1,
        "creator_pid": creator_pid,
        "creator_start_ticks": creator_start_ticks,
    }


def _shared_playwright_marker_payload(cache: SharedPlaywrightCache) -> dict[str, object]:
    return _shared_playwright_marker_contract(
        run_id=cache.run_id,
        owner_hash=cache.owner_hash,
        root_name=cache.root.name,
        creator_pid=cache.creator_pid,
        creator_start_ticks=cache.creator_start_ticks,
        state="ready",
    )


def _write_private_json_atomic(path: Path, value: object) -> None:
    """marker更新中のkillでも直前の完全なmarkerを残す。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _assert_shared_playwright_cache_ownership(cache: SharedPlaywrightCache) -> None:
    root = cache.root
    expected_prefix = _SHARED_PLAYWRIGHT_CACHE_PREFIX + hashlib.sha256(cache.run_id.encode()).hexdigest()[:16] + "-"
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise IsolationViolation("shared Playwright cache root is missing or unsafe")
    if root.parent != cache.runtime_parent or not root.name.startswith(expected_prefix):
        raise IsolationViolation("shared Playwright cache root is outside its dedicated runtime parent")
    root_metadata = root.stat()
    if root_metadata.st_uid != os.geteuid() or stat.S_IMODE(root_metadata.st_mode) != 0o700:
        raise IsolationViolation("shared Playwright cache root ownership or private mode differs")
    if cache.marker != root / _SHARED_PLAYWRIGHT_CACHE_MARKER:
        raise IsolationViolation("shared Playwright cache marker path differs")
    if cache.marker.is_symlink() or not cache.marker.is_file():
        raise IsolationViolation("shared Playwright cache ownership marker is missing or unsafe")
    try:
        payload = _read_private_json_no_follow(cache.marker, description="shared Playwright cache ownership marker")
    except (OSError, IsolationViolation) as exc:
        raise IsolationViolation("shared Playwright cache ownership marker is unreadable") from exc
    if payload != _shared_playwright_marker_payload(cache):
        raise IsolationViolation("shared Playwright cache ownership marker differs")
    if _process_start_ticks(cache.creator_pid) != cache.creator_start_ticks:
        raise IsolationViolation("shared Playwright cache creator process identity differs")
    if cache.cache_path != root / "playwright-browsers":
        raise IsolationViolation("shared Playwright cache path differs from its marker contract")
    if cache.cache_path.is_symlink() or not cache.cache_path.is_dir():
        raise IsolationViolation("shared Playwright browser directory is missing or unsafe")


def _resolve_playwright_cache_source(repository: Path, source_env_file: Path | None) -> Path | None:
    inherited = dict(os.environ)
    source_file_values = _read_env_file(source_env_file)
    raw = inherited.get("PLAYWRIGHT_BROWSERS_PATH") or source_file_values.get("PLAYWRIGHT_BROWSERS_PATH")
    if raw == "0":
        return None
    if raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = repository / candidate
    else:
        original_home = inherited.get("HOME", "")
        if not original_home:
            return None
        candidate = Path(original_home) / ".cache" / "ms-playwright"
    if candidate.is_symlink():
        raise IsolationViolation("external Playwright browser cache must not be a symlink")
    if not candidate.is_dir():
        return None
    resolved = candidate.resolve()
    directory_integrity_snapshot(resolved, include_content=False)
    return resolved


def _resolve_shared_playwright_runtime_parent() -> Path:
    configured_parent = os.environ.get("SHERPA_UI_RUNTIME_PARENT")
    raw_runtime_parent = Path(configured_parent) if configured_parent else Path(tempfile.gettempdir())
    if raw_runtime_parent.is_symlink():
        raise IsolationViolation("shared Playwright runtime parent must not be a symlink")
    runtime_parent = raw_runtime_parent.resolve()
    if runtime_parent.is_symlink() or not runtime_parent.is_dir():
        raise IsolationViolation("shared Playwright runtime parent is missing or unsafe")
    return runtime_parent


def _read_recovery_marker(candidate: Path, runtime_parent: Path) -> dict[str, object]:
    if candidate.parent != runtime_parent or candidate.is_symlink() or not candidate.is_dir():
        raise IsolationViolation("stale shared cache candidate is outside the runtime parent or unsafe")
    if candidate.resolve() != candidate:
        raise IsolationViolation("stale shared cache candidate does not have a canonical direct path")
    candidate_metadata = candidate.stat()
    if candidate_metadata.st_uid != os.geteuid() or stat.S_IMODE(candidate_metadata.st_mode) != 0o700:
        raise IsolationViolation("stale shared cache candidate ownership or private mode differs")
    marker = candidate / _SHARED_PLAYWRIGHT_CACHE_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise IsolationViolation("stale shared cache candidate has no safe ownership marker")
    try:
        payload = _read_private_json_no_follow(marker, description="stale shared cache marker")
    except (OSError, IsolationViolation) as exc:
        raise IsolationViolation("stale shared cache marker is unreadable") from exc
    required_keys = {
        "schema",
        "kind",
        "state",
        "run_id",
        "owner_hash",
        "root_name",
        "cache_relative",
        "copy_count",
        "creator_pid",
        "creator_start_ticks",
    }
    if not isinstance(payload, dict) or set(payload) != required_keys:
        raise IsolationViolation("stale shared cache marker schema differs")
    run_id = payload["run_id"]
    owner_hash = payload["owner_hash"]
    if (
        payload["schema"] != 2
        or payload["kind"] != "playwright-browser-cache"
        or payload["state"] not in {"preparing", "ready"}
        or not isinstance(run_id, str)
        or not 1 <= len(run_id) <= 200
        or not isinstance(owner_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", owner_hash) is None
        or payload["root_name"] != candidate.name
        or payload["cache_relative"] != "playwright-browsers"
        or type(payload["copy_count"]) is not int
        or payload["copy_count"] != 1
        or type(payload["creator_pid"]) is not int
        or payload["creator_pid"] <= 0
        or type(payload["creator_start_ticks"]) is not int
        or payload["creator_start_ticks"] <= 0
    ):
        raise IsolationViolation("stale shared cache marker contract differs")
    expected_prefix = _SHARED_PLAYWRIGHT_CACHE_PREFIX + hashlib.sha256(run_id.encode()).hexdigest()[:16] + "-"
    if not candidate.name.startswith(expected_prefix):
        raise IsolationViolation("stale shared cache directory does not match its marker run identity")
    return payload


def _creator_process_is_active(payload: dict[str, object]) -> bool:
    creator_pid = int(payload["creator_pid"])
    creator_start_ticks = int(payload["creator_start_ticks"])
    current_start_ticks = _process_start_ticks(creator_pid)
    if current_start_ticks is not None:
        return current_start_ticks == creator_start_ticks
    try:
        os.kill(creator_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 生存プロセスを誤ってstale扱いしない。
        return True
    return True


def _validate_stale_shared_cache_tree(candidate: Path, payload: dict[str, object]) -> None:
    assert_no_mount_targets(candidate)
    assert_no_unsafe_hardlinks(candidate)
    marker = candidate / _SHARED_PLAYWRIGHT_CACHE_MARKER
    cache_path = candidate / "playwright-browsers"
    allowed_direct_children = {marker.name, cache_path.name}
    marker_temporary = re.compile(rf"^\.{re.escape(marker.name)}\.{int(payload['creator_pid'])}\.[0-9a-f]{{16}}\.tmp$")
    for child in candidate.iterdir():
        is_interrupted_atomic_marker = payload["state"] == "preparing" and marker_temporary.fullmatch(child.name)
        if child.name not in allowed_direct_children and not is_interrupted_atomic_marker:
            raise IsolationViolation("stale shared cache contains an unexpected top-level entry")
        if is_interrupted_atomic_marker:
            child_metadata = child.lstat()
            if (
                child.is_symlink()
                or not child.is_file()
                or child_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(child_metadata.st_mode) != 0o600
            ):
                raise IsolationViolation("stale shared cache has an unsafe interrupted marker update")
    for path in candidate.rglob("*"):
        metadata = path.lstat()
        if path.is_symlink() or metadata.st_uid != os.geteuid():
            raise IsolationViolation("stale shared cache contains an unsafe or foreign-owned entry")
        if not path.is_dir() and not path.is_file():
            raise IsolationViolation("stale shared cache contains a special filesystem entry")
    if payload["state"] == "ready":
        if cache_path.is_symlink() or not cache_path.is_dir():
            raise IsolationViolation("ready stale shared cache has no safe browser directory")
        directory_integrity_snapshot(
            cache_path,
            include_content=False,
            require_owner=True,
            require_read_only=True,
        )


def _remove_stale_shared_cache(candidate: Path, payload: dict[str, object], runtime_parent: Path) -> None:
    # 検査後の差し替えと、検査中にcreatorが再び一致する競合を拒否する。
    if _read_recovery_marker(candidate, runtime_parent) != payload:
        raise IsolationViolation("stale shared cache marker changed before cleanup")
    if _creator_process_is_active(payload):
        raise IsolationViolation("stale shared cache creator became active before cleanup")
    _validate_stale_shared_cache_tree(candidate, payload)
    chmod_tree_no_follow(candidate, directory_mode=0o700, file_mode=0o600, allow_symlinks=False)
    assert_no_mount_targets(candidate)
    rmtree_no_follow(candidate)
    if candidate.exists():
        raise IsolationViolation("stale shared cache root remains after cleanup")


def recover_stale_shared_playwright_caches() -> dict[str, object]:
    """前回のhard crashで残った厳密所有確認済みcacheだけを次run開始時に回収する。"""

    runtime_parent = _resolve_shared_playwright_runtime_parent()
    removed: list[str] = []
    active: list[str] = []
    errors: list[dict[str, str]] = []
    scanned = 0
    for candidate in sorted(runtime_parent.iterdir()):
        if not candidate.name.startswith(_SHARED_PLAYWRIGHT_CACHE_PREFIX):
            continue
        scanned += 1
        path_hash = hashlib.sha256(str(candidate).encode()).hexdigest()
        try:
            payload = _read_recovery_marker(candidate, runtime_parent)
            if _creator_process_is_active(payload):
                active.append(path_hash)
                continue
            _validate_stale_shared_cache_tree(candidate, payload)
            _remove_stale_shared_cache(candidate, payload, runtime_parent)
            removed.append(path_hash)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(
                {
                    "root_path_sha256": path_hash,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            )
    return {
        "phase": "run-start-stale-recovery",
        "status": "PASS" if not errors else "FAIL",
        "runtime_parent_sha256": hashlib.sha256(str(runtime_parent).encode()).hexdigest(),
        "marker_schema": 2,
        "scanned_count": scanned,
        "removed_count": len(removed),
        "active_preserved_count": len(active),
        "removed_root_path_sha256": removed,
        "active_root_path_sha256": active,
        "errors": errors,
    }


def prepare_shared_playwright_cache(
    *,
    repository: Path,
    run_id: str,
    source_env_file: Path | None,
) -> SharedPlaywrightCache | None:
    """外部cacheをrunにつき一度だけprivate/read-only領域へ複製する。"""

    source = _resolve_playwright_cache_source(repository.resolve(), source_env_file)
    if source is None:
        return None
    source_snapshot = directory_integrity_snapshot(source, include_content=True)
    runtime_parent = _resolve_shared_playwright_runtime_parent()
    creator_pid = os.getpid()
    creator_start_ticks = _process_start_ticks(creator_pid)
    if creator_start_ticks is None:
        raise IsolationViolation("cannot establish Playwright cache creator process identity")
    prefix = _SHARED_PLAYWRIGHT_CACHE_PREFIX + hashlib.sha256(run_id.encode()).hexdigest()[:16] + "-"
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=runtime_parent)).resolve()
    chmod_path_no_follow(root, 0o700, require_owner_uid=os.geteuid())
    marker = root / _SHARED_PLAYWRIGHT_CACHE_MARKER
    target = root / "playwright-browsers"
    owner_hash = hashlib.sha256(run_id.encode() + b"\0" + secrets.token_bytes(32)).hexdigest()
    cache: SharedPlaywrightCache | None = None
    try:
        _write_private_json_atomic(
            marker,
            _shared_playwright_marker_contract(
                run_id=run_id,
                owner_hash=owner_hash,
                root_name=root.name,
                creator_pid=creator_pid,
                creator_start_ticks=creator_start_ticks,
                state="preparing",
            ),
        )
        _copy_read_only_tree(source, target)
        cache_snapshot = directory_integrity_snapshot(
            target,
            include_content=True,
            require_owner=True,
            require_read_only=True,
        )
        if (
            source_snapshot["content_sha256"] != cache_snapshot["content_sha256"]
            or source_snapshot["entry_count"] != cache_snapshot["entry_count"]
            or source_snapshot["total_bytes"] != cache_snapshot["total_bytes"]
        ):
            raise IsolationViolation("shared Playwright cache copy differs from its external source")
        cache = SharedPlaywrightCache(
            run_id=run_id,
            runtime_parent=runtime_parent,
            root=root,
            marker=marker,
            cache_path=target,
            source_path=source,
            owner_hash=owner_hash,
            creator_pid=creator_pid,
            creator_start_ticks=creator_start_ticks,
            source_metadata_sha256=str(source_snapshot["metadata_sha256"]),
            source_content_sha256=str(source_snapshot["content_sha256"]),
            source_entry_count=int(source_snapshot["entry_count"]),
            source_total_bytes=int(source_snapshot["total_bytes"]),
            cache_metadata_sha256=str(cache_snapshot["metadata_sha256"]),
            cache_content_sha256=str(cache_snapshot["content_sha256"]),
            cache_entry_count=int(cache_snapshot["entry_count"]),
            cache_total_bytes=int(cache_snapshot["total_bytes"]),
        )
        _write_private_json_atomic(marker, _shared_playwright_marker_payload(cache))
        _assert_shared_playwright_cache_ownership(cache)
        return cache
    except Exception as exc:
        cleanup_error: Exception | None = None
        try:
            chmod_tree_no_follow(root, directory_mode=0o700, file_mode=0o600, allow_symlinks=True)
            assert_no_mount_targets(root)
            rmtree_no_follow(root)
        except (OSError, ValueError, RuntimeError) as cleanup_exc:
            cleanup_error = cleanup_exc
        if cleanup_error is not None:
            raise IsolationViolation(
                "shared Playwright cache preparation and rollback cleanup both failed: "
                f"{type(exc).__name__}; {type(cleanup_error).__name__}"
            ) from exc
        raise


def initial_shared_playwright_cache_evidence(cache: SharedPlaywrightCache) -> dict[str, object]:
    _assert_shared_playwright_cache_ownership(cache)
    return {
        "phase": "run-initial",
        "status": "PASS",
        "available": True,
        "copy_count": cache.copy_count,
        "source_path_sha256": hashlib.sha256(str(cache.source_path).encode()).hexdigest(),
        "cache_path_sha256": hashlib.sha256(str(cache.cache_path).encode()).hexdigest(),
        "source_metadata_sha256": cache.source_metadata_sha256,
        "source_content_sha256": cache.source_content_sha256,
        "source_entry_count": cache.source_entry_count,
        "source_total_bytes": cache.source_total_bytes,
        "cache_metadata_sha256": cache.cache_metadata_sha256,
        "cache_content_sha256": cache.cache_content_sha256,
        "cache_entry_count": cache.cache_entry_count,
        "cache_total_bytes": cache.cache_total_bytes,
        "copy_content_match": cache.source_content_sha256 == cache.cache_content_sha256,
        "owner_marker_verified": True,
        "private_root_mode": "0o700",
        "access_contract": "single-run-copy-shared-read-only-across-profiles",
    }


def inspect_shared_playwright_cache(
    cache: SharedPlaywrightCache,
    *,
    full_content: bool,
    phase: str,
) -> dict[str, object]:
    _assert_shared_playwright_cache_ownership(cache)
    shared = directory_integrity_snapshot(
        cache.cache_path,
        include_content=full_content,
        require_owner=True,
        require_read_only=True,
    )
    evidence: dict[str, object] = {
        "phase": phase,
        "available": True,
        "copy_count": cache.copy_count,
        "cache_path_sha256": hashlib.sha256(str(cache.cache_path).encode()).hexdigest(),
        "entry_count": shared["entry_count"],
        "total_bytes": shared["total_bytes"],
        "metadata_sha256": shared["metadata_sha256"],
        "metadata_unchanged": (
            shared["metadata_sha256"] == cache.cache_metadata_sha256
            and shared["entry_count"] == cache.cache_entry_count
            and shared["total_bytes"] == cache.cache_total_bytes
        ),
        "owner_ok": shared["owner_ok"],
        "read_only": shared["read_only"],
        "owner_marker_verified": True,
        "inspection": "full-content" if full_content else "metadata-and-permissions-only",
    }
    if full_content:
        source = directory_integrity_snapshot(cache.source_path, include_content=True)
        evidence.update(
            {
                "source_path_sha256": hashlib.sha256(str(cache.source_path).encode()).hexdigest(),
                "content_sha256": shared["content_sha256"],
                "content_unchanged": shared["content_sha256"] == cache.cache_content_sha256,
                "source_metadata_sha256": source["metadata_sha256"],
                "source_content_sha256": source["content_sha256"],
                "source_unchanged": (
                    source["metadata_sha256"] == cache.source_metadata_sha256
                    and source["content_sha256"] == cache.source_content_sha256
                    and source["entry_count"] == cache.source_entry_count
                    and source["total_bytes"] == cache.source_total_bytes
                ),
                "source_and_cache_content_match": source["content_sha256"] == shared["content_sha256"],
            }
        )
    checks = [
        bool(evidence["metadata_unchanged"]),
        bool(evidence["owner_ok"]),
        bool(evidence["read_only"]),
    ]
    if full_content:
        checks.extend(
            [
                bool(evidence["content_unchanged"]),
                bool(evidence["source_unchanged"]),
                bool(evidence["source_and_cache_content_match"]),
            ]
        )
    evidence["status"] = "PASS" if all(checks) else "FAIL"
    return evidence


def cleanup_shared_playwright_cache(cache: SharedPlaywrightCache) -> list[str]:
    """markerが完全一致するrun-owned shared cacheだけを削除する。"""

    try:
        _assert_shared_playwright_cache_ownership(cache)
    except (OSError, ValueError, RuntimeError) as exc:
        return [f"shared Playwright cache cleanup refused ownership mismatch: {type(exc).__name__}: {exc}"]
    try:
        chmod_tree_no_follow(cache.root, directory_mode=0o700, file_mode=0o600, allow_symlinks=True)
        assert_no_mount_targets(cache.root)
        rmtree_no_follow(cache.root)
        if cache.root.exists():
            raise RuntimeError("shared Playwright cache root remains after cleanup")
    except (OSError, ValueError, RuntimeError) as exc:
        return [f"shared Playwright cache cleanup failed: {type(exc).__name__}: {exc}"]
    return []


def _safe_project_part(value: str, maximum: int = 22) -> str:
    cleaned = _PROJECT_PART.sub("-", value.lower()).strip("-_")
    return (cleaned or "profile")[:maximum]


def _compose_project_name(run_id: str, profile_name: str) -> str:
    run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    profile_digest = hashlib.sha256(profile_name.encode("utf-8")).hexdigest()[:10]
    return f"sherpa-ui-automation-{run_digest}-{_safe_project_part(profile_name, 8)}-{profile_digest}"


def _legacy_compose_project_name(run_id: str, profile_name: str) -> str:
    """Schema 1以前のmarkerをsecret scrubだけに限定して識別する。"""

    return f"sherpa-ui-automation-{_safe_project_part(run_id, 16)}-{_safe_project_part(profile_name, 22)}"


def _profile_value(profile: EnvProfile, name: str, default: str) -> str:
    if name not in profile.env:
        return default
    value = profile.env[name]
    return "" if value is None else value


def _mapping_value(values: dict[str, str | None], name: str, default: str) -> str:
    if name not in values:
        return default
    value = values[name]
    return "" if value is None else value


def _apply_value_mode(
    values: dict[str, str | None],
    *,
    variable: str,
    mode: str,
    value: str | None,
    inherited: dict[str, str],
    runtime_values: dict[str, str] | None = None,
    missing: list[str],
) -> None:
    if mode == "absent":
        return
    if mode == "unset":
        values[variable] = None
        return
    if mode == "literal":
        values[variable] = value or ""
        return
    if mode == "inherit":
        source = value or variable
        inherited_value = inherited.get(source, "")
        if not inherited_value:
            missing.append(source)
            values[variable] = None
        else:
            values[variable] = inherited_value
        return
    if mode == "runtime" and runtime_values is not None:
        if value not in runtime_values:
            raise IsolationViolation(f"unsupported generated runtime value for {variable}: {value}")
        values[variable] = runtime_values[value]
        return
    if mode != "runtime":
        raise IsolationViolation(f"unsupported generated value mode for {variable}: {mode}")


def _resolve_restart_environment(
    raw: dict[str, object],
    inherited: dict[str, str],
) -> tuple[dict[str, str | None], list[str]]:
    resolved: dict[str, str | None] = {}
    missing: list[str] = []
    for name, value in raw.items():
        if not _ENV_NAME.fullmatch(str(name)):
            raise IsolationViolation(f"restart_env contains an invalid environment name: {name!r}")
        if isinstance(value, dict):
            source = value.get("from_env") or value.get("source_env")
            if not source:
                raise IsolationViolation(f"restart_env {name} mapping must use from_env/source_env")
            actual = inherited.get(str(source), "")
            if not actual:
                missing.append(str(source))
                resolved[str(name)] = None
            else:
                resolved[str(name)] = actual
        else:
            resolved[str(name)] = None if value is None else env_scalar(value)
    return resolved, missing


def env_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _assert_loopback_connection(name: str, value: str) -> None:
    if not value:
        return
    host = ""
    if "://" in value:
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            port = parsed.port
        except ValueError as exc:
            raise IsolationViolation(f"{name} has an invalid URL port") from exc
        allowed_schemes = {
            "DATABASE_URL": {"postgres", "postgresql"},
            "SHERPA_PG_DSN": {"postgres", "postgresql"},
            "ES_URL": {"http", "https"},
            "NEO4J_URI": {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"},
        }
        allowed = allowed_schemes.get(name, set())
        if parsed.scheme.lower() not in allowed:
            raise IsolationViolation(f"{name} scheme {parsed.scheme!r} is invalid; allowed={sorted(allowed)}")
        if not host:
            raise IsolationViolation(f"{name} has an invalid empty host")
        if port is not None and not 1 <= port <= 65535:
            raise IsolationViolation(f"{name} has an invalid port outside 1..65535")
    else:
        if name not in {"DATABASE_URL", "SHERPA_PG_DSN"}:
            raise IsolationViolation(f"{name} is invalid because an explicit URL scheme is required")
        if not re.search(r"(?:^|\s)[A-Za-z_][A-Za-z0-9_]*=", value):
            raise IsolationViolation(f"{name} is invalid because libpq DSN key=value syntax is required")
        match = re.search(r"(?:^|\s)host=([^\s]+)", value)
        host = match.group(1) if match else "localhost"
        port_match = re.search(r"(?:^|\s)port=([^\s]+)", value)
        if port_match:
            raw_port = port_match.group(1)
            if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
                raise IsolationViolation(f"{name} has an invalid libpq port")
    if host not in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        raise IsolationViolation(f"{name} points outside loopback; isolated tests refuse external stores")


@dataclass(frozen=True)
class RunIsolation:
    run_id: str
    profile: EnvProfile
    paths: RunPaths
    project_name: str
    owner_hash: str
    creator_pid: int
    creator_start_ticks: int
    environment: dict[str, str]
    trusted_runner_path: str
    trusted_python_executable: str
    env_file: Path
    compose_file: Path
    compose_override_file: Path
    base_url: str
    database_url: str
    world_source_path: Path
    world_path: Path
    ocr_model_cache_source_path: Path | None
    ocr_model_cache_path: Path
    playwright_browsers_path: Path | None
    playwright_browsers_signature: str | None
    playwright_browsers_entry_count: int
    shared_playwright_cache: SharedPlaywrightCache | None
    admin_password: str
    ports: dict[str, str]
    scenario_contract: dict[str, object]
    pairwise_expected_values: dict[str, str | None]
    precondition_failures: tuple[str, ...]
    restart_environment: dict[str, str | None]
    restart_transition_id: str | None
    secret_registry: Path
    docker_socket_identity_sha256: str

    @property
    def runner_environment(self) -> dict[str, str]:
        """Return infrastructure env without applying the product PATH/PYTHON_BIN scenario.

        Environment profiles intentionally corrupt PATH and PYTHON_BIN to prove
        product startup is fail-closed.  Docker, Compose, Playwright and pytest
        are runner infrastructure, so allowing those values to select their
        executables would test the runner instead of Sherpa.
        """

        return _runner_environment(
            self.environment,
            trusted_path=self.trusted_runner_path,
            trusted_python_executable=self.trusted_python_executable,
        )


def create_isolation(
    *,
    repository: Path,
    run_root: Path,
    run_id: str,
    profile: EnvProfile,
    source_env_file: Path | None,
    shared_playwright_cache: SharedPlaywrightCache | None,
    headed: bool,
    case_timeout_ms: int,
) -> RunIsolation:
    inherited = dict(os.environ)
    trusted_runner_path = inherited.get("PATH") or os.defpath
    trusted_python_path = Path(sys.executable)
    if not trusted_python_path.is_absolute():
        trusted_python_path = (Path.cwd() / trusted_python_path).absolute()
    trusted_python_executable = str(trusted_python_path)
    try:
        trusted_python_metadata = trusted_python_path.stat()
    except OSError as exc:
        raise IsolationViolation("the runner Python executable cannot be resolved") from exc
    if (
        not Path(trusted_python_executable).is_absolute()
        or not stat.S_ISREG(trusted_python_metadata.st_mode)
        or not os.access(trusted_python_executable, os.X_OK)
    ):
        raise IsolationViolation("the runner Python executable is not an executable regular file")
    source_file_values = _read_env_file(source_env_file)
    supplied_docker_controls = sorted(name for name in _DOCKER_ENDPOINT_ENV if name in inherited or name in source_file_values)
    if supplied_docker_controls:
        raise IsolationViolation(
            "ambient or env-file Docker endpoint/context/TLS/config overrides are forbidden: " + ", ".join(supplied_docker_controls)
        )
    docker_socket_identity = local_docker_socket_identity()
    runtime_parent = runtime_parent_path()
    file_values: dict[str, str | None] = dict(source_file_values)
    for key, value in profile.env_file.items():
        file_values[key] = value
    process_values = dict(profile.env)
    profile_overrides = set(profile.env) | set(profile.env_file)
    for key in sorted((_LOCKED_PATHS | _LOCKED_PROJECT | _DOCKER_ENDPOINT_ENV).intersection(profile_overrides)):
        raise IsolationViolation(f"profile {profile.name!r} may not override isolated setting {key}")
    restart_blocked = (
        _LOCKED_PATHS
        | _LOCKED_PROJECT
        | _DOCKER_ENDPOINT_ENV
        | {
            "DATABASE_URL",
            "ES_URL",
            "NEO4J_URI",
            "PGPORT",
            "SHERPA_ES_PORT",
            "SHERPA_NEO4J_BOLT_PORT",
            "SHERPA_NEO4J_HTTP_PORT",
            "SHERPA_PG_DSN",
            "SHERPA_PORT",
        }
    )
    blocked_restart = sorted(restart_blocked.intersection(profile.restart_env))
    if blocked_restart or any(str(name).startswith(("SHERPA_UI_", "COMPOSE_")) for name in profile.restart_env):
        raise IsolationViolation("restart_env may not alter runner isolation, control, port, or store settings")
    compose_file_source = repository / "docker-compose.yml"
    if compose_file_source.is_symlink() or not compose_file_source.is_file():
        raise IsolationViolation("the repository docker-compose.yml must be a regular non-symlink file")
    compose_file = compose_file_source.resolve()
    world_source_path = (repository / "ui_automation" / "fixtures" / "world").resolve()
    if not world_source_path.is_dir():
        raise IsolationViolation("deterministic World fixture source is missing")
    assert_no_mount_targets(world_source_path)
    for source_item in world_source_path.rglob("*"):
        if source_item.is_symlink():
            raise IsolationViolation("deterministic World fixture must not contain symlinks")
    if "SHERPA_BROWSE_ROOTS" in profile.env:
        requested = Path(profile.env["SHERPA_BROWSE_ROOTS"] or "").resolve()
        if requested != world_source_path.parent:
            raise IsolationViolation("profile may not point SHERPA_BROWSE_ROOTS outside the test fixture root")
    for name in ("SHERPA_PG_DSN", "DATABASE_URL", "ES_URL", "NEO4J_URI"):
        if name in profile.env and profile.env[name] is not None:
            _assert_loopback_connection(name, profile.env[name] or "")
    generated_ports = _allocate_ports(6 if profile.pairwise_scenario is not None else 5)
    port_names = (
        "PGPORT",
        "SHERPA_ES_PORT",
        "SHERPA_NEO4J_BOLT_PORT",
        "SHERPA_NEO4J_HTTP_PORT",
        "SHERPA_PORT",
    )
    precondition_failures: list[str] = []
    scenario = profile.generated_scenario
    pairwise = profile.pairwise_scenario
    if pairwise is not None:
        for factor in pairwise.factors:
            isolated_values = _PAIRWISE_ISOLATED_RUNTIME_BY_VARIABLE.get(factor.key)
            if isolated_values is not None and (
                factor.process_mode != "runtime"
                or factor.process_value not in isolated_values
                or factor.env_file_mode not in {"absent", "runtime"}
                or (factor.env_file_mode == "runtime" and factor.env_file_value not in isolated_values)
            ):
                raise IsolationViolation(f"pairwise {factor.key} must use only its runner-owned isolated runtime values")
            if factor.process_mode != "runtime":
                _apply_value_mode(
                    process_values,
                    variable=factor.key,
                    mode=factor.process_mode,
                    value=factor.process_value,
                    inherited=inherited,
                    missing=precondition_failures,
                )
            if factor.env_file_mode != "runtime":
                _apply_value_mode(
                    file_values,
                    variable=factor.key,
                    mode=factor.env_file_mode,
                    value=factor.env_file_value,
                    inherited=inherited,
                    missing=precondition_failures,
                )
            for requirement in factor.prerequisites:
                if not inherited.get(requirement, ""):
                    precondition_failures.append(requirement)
    if scenario is not None:
        runtime_port = (
            str(generated_ports[port_names.index(scenario.variable)]) if scenario.variable in port_names else str(generated_ports[-1])
        )
        if scenario.process_mode != "runtime" or scenario.process_value == "dynamic_port":
            _apply_value_mode(
                process_values,
                variable=scenario.variable,
                mode=scenario.process_mode,
                value=scenario.process_value,
                inherited=inherited,
                runtime_values={"dynamic_port": runtime_port},
                missing=precondition_failures,
            )
        if scenario.env_file_mode != "runtime" or scenario.env_file_value == "dynamic_port":
            _apply_value_mode(
                file_values,
                variable=scenario.variable,
                mode=scenario.env_file_mode,
                value=scenario.env_file_value,
                inherited=inherited,
                runtime_values={"dynamic_port": runtime_port},
                missing=precondition_failures,
            )
        for requirement in scenario.prerequisites:
            if not inherited.get(requirement, ""):
                precondition_failures.append(requirement)
    if pairwise is not None:
        early_runtime_values = {
            "app_port_primary": str(generated_ports[4]),
            "app_port_secondary": str(generated_ports[5]),
        }
        for factor in pairwise.factors:
            if factor.process_mode == "runtime" and factor.process_value in early_runtime_values:
                _apply_value_mode(
                    process_values,
                    variable=factor.key,
                    mode=factor.process_mode,
                    value=factor.process_value,
                    inherited=inherited,
                    runtime_values=early_runtime_values,
                    missing=precondition_failures,
                )
            if factor.env_file_mode == "runtime" and factor.env_file_value in early_runtime_values:
                _apply_value_mode(
                    file_values,
                    variable=factor.key,
                    mode=factor.env_file_mode,
                    value=factor.env_file_value,
                    inherited=inherited,
                    runtime_values=early_runtime_values,
                    missing=precondition_failures,
                )
    ports = {
        name: _mapping_value(process_values, name, str(generated)) for name, generated in zip(port_names, generated_ports[:5], strict=True)
    }
    for name, raw_port in ports.items():
        if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            raise IsolationViolation(f"{name} has an invalid port outside 1..65535")
    project_name = _compose_project_name(run_id, profile.name)
    owner_nonce = secrets.token_bytes(32)
    owner_hash = hashlib.sha256(run_id.encode("utf-8") + b"\0" + profile.name.encode("utf-8") + b"\0" + owner_nonce).hexdigest()
    creator_pid = os.getpid()
    creator_start_ticks = _process_start_ticks(creator_pid)
    if creator_start_ticks is None:
        raise IsolationViolation("cannot establish profile runtime creator process identity")
    if scenario is not None and scenario.variable in _LOCKED_PROJECT and scenario.scenario in {"boundary", "invalid"}:
        raise IsolationViolation(f"{scenario.variable} must remain the run-owned project; mismatched project input is invalid")
    runtime_root = Path(tempfile.mkdtemp(prefix=f"{project_name}-", dir=runtime_parent)).resolve()
    marker = runtime_root / RUNTIME_MARKER_NAME
    compose_override_file = runtime_root / "compose.owner.override.yaml"
    try:
        _write_private_json(
            marker,
            runtime_marker_payload(
                run_id=run_id,
                profile_name=profile.name,
                project_name=project_name,
                owner_hash=owner_hash,
                creator_pid=creator_pid,
                creator_start_ticks=creator_start_ticks,
            ),
        )
        _write_compose_override(compose_override_file, owner_hash)
    except OSError:
        assert_no_mount_targets(runtime_root)
        rmtree_no_follow(runtime_root)
        raise
    profile_root = run_root / _safe_project_part(profile.name, 80)
    paths = RunPaths(repository.resolve(), run_root.resolve(), profile_root.resolve(), runtime_root)
    profile_root.mkdir(parents=True, exist_ok=True)
    for relative in ("browser", "network", "reports", "security", "services", "state"):
        (profile_root / relative).mkdir(parents=True, exist_ok=True)
    fixture_runtime_root = runtime_root / "fixtures"
    world_path = fixture_runtime_root / "world"
    shutil.copytree(world_source_path, world_path, symlinks=False)
    assert_no_mount_targets(world_path)
    assert_no_unsafe_hardlinks(world_path)
    chmod_tree_no_follow(world_path, directory_mode=0o555, file_mode=0o444, allow_symlinks=False, require_owner_uid=os.geteuid())
    chmod_path_no_follow(fixture_runtime_root, 0o555, require_owner_uid=os.geteuid())
    browse_root = fixture_runtime_root

    kb_dir = runtime_root / "data" / "kb"
    users_dir = runtime_root / "data" / "users"
    derived_dir = runtime_root / "data" / "derived"
    observation_dir = runtime_root / "data" / "observations"
    user_home = runtime_root / "user-home"
    codex_home = user_home / ".codex"
    cache_home = user_home / ".cache"
    config_home = user_home / ".config"
    data_home = user_home / ".local" / "share"
    temp_dir = runtime_root / "tmp"
    run_dir = runtime_root / "run"
    scenario_dir = runtime_root / "scenario-values"
    runtime_ocr_model_cache = runtime_root / "ocr-model-cache"
    for path in (
        kb_dir,
        users_dir,
        derived_dir,
        observation_dir,
        codex_home,
        cache_home,
        config_home,
        data_home,
        temp_dir,
        run_dir,
        scenario_dir,
        runtime_ocr_model_cache,
    ):
        path.mkdir(parents=True, exist_ok=True)
    chmod_path_no_follow(user_home, stat.S_IRWXU, require_owner_uid=os.geteuid())
    chmod_path_no_follow(codex_home, stat.S_IRWXU, require_owner_uid=os.geteuid())

    postgres_password = (
        process_values["POSTGRES_PASSWORD"]
        if "POSTGRES_PASSWORD" in process_values and process_values["POSTGRES_PASSWORD"] is not None
        else secrets.token_urlsafe(24)
    )
    neo4j_password = (
        process_values["NEO4J_PASSWORD"]
        if "NEO4J_PASSWORD" in process_values and process_values["NEO4J_PASSWORD"] is not None
        else secrets.token_urlsafe(24)
    )
    admin_password = f"UiA-{secrets.token_urlsafe(18)}!7a"
    admin_new_password = f"UiA-New-{secrets.token_urlsafe(18)}!8b"
    pg_port = ports["PGPORT"]
    es_port = ports["SHERPA_ES_PORT"]
    bolt_port = ports["SHERPA_NEO4J_BOLT_PORT"]
    app_port = ports["SHERPA_PORT"]
    encoded_postgres_password = quote(postgres_password or "", safe="")
    generated_database_url = f"postgresql://sherpa:{encoded_postgres_password}@127.0.0.1:{pg_port}/sherpa"
    generated_env_file_name = "profile.env"
    if pairwise is not None:
        env_file_factor = next((factor for factor in pairwise.factors if factor.key == "SHERPA_ENV_FILE"), None)
        if env_file_factor is not None:
            if env_file_factor.process_mode != "runtime" or env_file_factor.process_value not in {
                "env_file_primary",
                "env_file_secondary",
            }:
                raise IsolationViolation("pairwise SHERPA_ENV_FILE must select a runner-owned runtime file")
            if env_file_factor.process_value == "env_file_secondary":
                generated_env_file_name = "profile-secondary.env"
    generated_env_file = runtime_root / generated_env_file_name

    pairwise_runtime_values: dict[str, str] = {}
    if pairwise is not None:
        pairwise_runtime_values = {
            "app_port_primary": str(generated_ports[4]),
            "app_port_secondary": str(generated_ports[5]),
            "database_localhost": generated_database_url.replace("@127.0.0.1:", "@localhost:", 1),
            "database_loopback": generated_database_url,
            "env_file_primary": str(runtime_root / "profile.env"),
            "env_file_secondary": str(runtime_root / "profile-secondary.env"),
            "es_localhost": f"http://localhost:{es_port}",
            "es_loopback": f"http://127.0.0.1:{es_port}",
            "neo4j_localhost": f"bolt://localhost:{bolt_port}",
            "neo4j_loopback": f"bolt://127.0.0.1:{bolt_port}",
        }
        for factor in pairwise.factors:
            if factor.process_mode == "runtime" and factor.process_value not in {
                "app_port_primary",
                "app_port_secondary",
            }:
                _apply_value_mode(
                    process_values,
                    variable=factor.key,
                    mode=factor.process_mode,
                    value=factor.process_value,
                    inherited=inherited,
                    runtime_values=pairwise_runtime_values,
                    missing=precondition_failures,
                )
            if factor.env_file_mode == "runtime" and factor.env_file_value not in {
                "app_port_primary",
                "app_port_secondary",
            }:
                _apply_value_mode(
                    file_values,
                    variable=factor.key,
                    mode=factor.env_file_mode,
                    value=factor.env_file_value,
                    inherited=inherited,
                    runtime_values=pairwise_runtime_values,
                    missing=precondition_failures,
                )

    if scenario is not None:
        path_targets = {
            "APP_PID_FILE": run_dir / "scenario-api.pid",
            "CODEX_HOME": codex_home,
            "HOME": user_home,
            "OLLAMA_HOME": user_home / ".ollama",
            "OLLAMA_MODELS_DIR": user_home / ".ollama" / "models",
            "SHERPA_BROWSE_ROOTS": browse_root,
            "SHERPA_DERIVED_DIR": derived_dir,
            "SHERPA_ENV_FILE": generated_env_file,
            "SHERPA_KB_DIR": kb_dir,
            "SHERPA_OBSERVATION_DIR": observation_dir,
            "SHERPA_OCR_MODEL_CACHE": runtime_root / "ocr-model-cache",
            "SHERPA_OCR_WORLD_ROOT": world_path,
            "SHERPA_USERS_DIR": users_dir,
        }
        valid_path = path_targets.get(scenario.variable, scenario_dir / "valid path 日本語")
        alternate_path = scenario_dir / "env-file-alternate"
        empty_path = run_dir / "boundary-api.pid" if scenario.variable == "APP_PID_FILE" else scenario_dir / "empty"
        invalid_path = scenario_dir / "missing" / "not-created"
        for path in (valid_path, alternate_path, empty_path):
            if scenario.variable == "APP_PID_FILE" and path in {valid_path, empty_path}:
                path.parent.mkdir(parents=True, exist_ok=True)
            elif path not in {generated_env_file, world_path, browse_root}:
                path.mkdir(parents=True, exist_ok=True)
        isolated_connection = {
            "DATABASE_URL": generated_database_url,
            "SHERPA_PG_DSN": generated_database_url,
            "ES_URL": f"http://127.0.0.1:{es_port}",
            "NEO4J_URI": f"bolt://127.0.0.1:{bolt_port}",
        }.get(scenario.variable, "")
        isolated_secret = {
            "POSTGRES_PASSWORD": postgres_password,
            "PGPASSWORD": postgres_password,
            "NEO4J_PASSWORD": neo4j_password,
        }.get(scenario.variable, secrets.token_urlsafe(24))
        runtime_values = {
            "alternate_path": str(alternate_path),
            "empty_path": str(empty_path),
            "invalid_connection": "invalid://127.0.0.1:not-a-port",
            "invalid_path": str(invalid_path),
            "isolated_connection": isolated_connection,
            "isolated_secret": isolated_secret,
            "isolated_project": project_name,
            "python_bin": os.path.abspath(os.sys.executable),
            "safe_path": str(scenario_dir / "bin") + os.pathsep + inherited.get("PATH", ""),
            "valid_path": str(valid_path),
        }
        (scenario_dir / "bin").mkdir(parents=True, exist_ok=True)
        if scenario.process_mode == "runtime" and scenario.process_value != "dynamic_port":
            _apply_value_mode(
                process_values,
                variable=scenario.variable,
                mode=scenario.process_mode,
                value=scenario.process_value,
                inherited=inherited,
                runtime_values=runtime_values,
                missing=precondition_failures,
            )
        if scenario.env_file_mode == "runtime" and scenario.env_file_value != "dynamic_port":
            _apply_value_mode(
                file_values,
                variable=scenario.variable,
                mode=scenario.env_file_mode,
                value=scenario.env_file_value,
                inherited=inherited,
                runtime_values=runtime_values,
                missing=precondition_failures,
            )

    playwright_browsers_path: Path | None = None
    playwright_browsers_signature: str | None = None
    playwright_browsers_entry_count = 0
    if shared_playwright_cache is not None:
        if shared_playwright_cache.run_id != run_id:
            raise IsolationViolation("shared Playwright cache belongs to another run")
        _assert_shared_playwright_cache_ownership(shared_playwright_cache)
        if shared_playwright_cache.cache_path.is_relative_to(runtime_root):
            raise IsolationViolation("shared Playwright cache must remain outside profile runtime cleanup")
        playwright_browsers_path = shared_playwright_cache.cache_path
        playwright_browsers_signature = shared_playwright_cache.cache_metadata_sha256
        playwright_browsers_entry_count = shared_playwright_cache.cache_entry_count

    postgres_discrete_identity_scenario = bool(scenario is not None and scenario.variable in _POSTGRES_DISCRETE_IDENTITY_VARIABLES)
    normalized_file_values = {key: value for key, value in file_values.items() if value is not None}
    if postgres_discrete_identity_scenario:
        # Product precedence is SHERPA_PG_DSN/DATABASE_URL before the discrete
        # libpq variables.  Remove those runner defaults only for these
        # generated profiles, otherwise PGUSER/PGDATABASE could appear tested
        # while the real application silently used the generated URL instead.
        normalized_file_values.pop("DATABASE_URL", None)
        normalized_file_values.pop("SHERPA_PG_DSN", None)
    normalized_file_values.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    if playwright_browsers_path is not None:
        normalized_file_values["PLAYWRIGHT_BROWSERS_PATH"] = str(playwright_browsers_path)
    environment = dict(normalized_file_values)
    environment.update(inherited)
    for key, value in process_values.items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    if postgres_discrete_identity_scenario:
        environment.pop("DATABASE_URL", None)
        environment.pop("SHERPA_PG_DSN", None)
        environment.update(
            {
                "PGHOST": "127.0.0.1",
                "PGPASSWORD": postgres_password,
                "PGPORT": pg_port,
            }
        )
        if scenario.variable != "PGDATABASE":
            environment["PGDATABASE"] = _ISOLATED_STORE_IDENTITIES["PGDATABASE"]
        if scenario.variable != "PGUSER":
            environment["PGUSER"] = _ISOLATED_STORE_IDENTITIES["PGUSER"]
    neo4j_user_scenario_value = (
        ("NEO4J_USER" in environment, environment.get("NEO4J_USER")) if scenario is not None and scenario.variable == "NEO4J_USER" else None
    )
    removed_compose_overrides = {
        key: {
            "present": True,
            "nonempty": bool(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
        for key, value in sorted(environment.items())
        if key.startswith("COMPOSE_")
    }
    ambient_daemon_endpoint_evidence = {
        key: {
            "present": key in environment,
            "nonempty": bool(environment.get(key, "")),
            "sha256": (hashlib.sha256(environment[key].encode("utf-8")).hexdigest() if key in environment else None),
        }
        for key in sorted(_DOCKER_ENDPOINT_ENV)
    }
    for key in tuple(environment):
        if key.startswith("COMPOSE_") or key in _DOCKER_ENDPOINT_ENV:
            environment.pop(key, None)
    normalized_file_values = {
        key: value for key, value in normalized_file_values.items() if not key.startswith("COMPOSE_") and key not in _DOCKER_ENDPOINT_ENV
    }
    environment["DOCKER_HOST"] = LOCAL_DOCKER_ENDPOINT
    _write_env_file(generated_env_file, normalized_file_values)

    explicit_pg_dsn = process_values.get("SHERPA_PG_DSN") if "SHERPA_PG_DSN" in process_values else None
    explicit_database = process_values.get("DATABASE_URL") if "DATABASE_URL" in process_values else None
    if postgres_discrete_identity_scenario:
        environment.pop("SHERPA_PG_DSN", None)
        environment.pop("DATABASE_URL", None)
        database_url = generated_database_url
    else:
        if "SHERPA_PG_DSN" in process_values:
            if explicit_pg_dsn is None:
                environment.pop("SHERPA_PG_DSN", None)
            else:
                environment["SHERPA_PG_DSN"] = explicit_pg_dsn
        elif "DATABASE_URL" in process_values:
            environment.pop("SHERPA_PG_DSN", None)
        else:
            environment["SHERPA_PG_DSN"] = generated_database_url
        database_url = explicit_database if explicit_database is not None else generated_database_url
        if database_url is None:
            database_url = generated_database_url
        environment["DATABASE_URL"] = database_url

    es_url = process_values.get("ES_URL") if process_values.get("ES_URL") is not None else f"http://127.0.0.1:{es_port}"
    neo4j_uri = process_values.get("NEO4J_URI") if process_values.get("NEO4J_URI") is not None else f"bolt://127.0.0.1:{bolt_port}"
    environment["ES_URL"] = es_url or ""
    environment["NEO4J_URI"] = neo4j_uri or ""
    for name in ("SHERPA_PG_DSN", "DATABASE_URL", "ES_URL", "NEO4J_URI"):
        _assert_loopback_connection(name, environment.get(name, ""))

    ocr_model_cache = runtime_ocr_model_cache
    ocr_model_cache_source: Path | None = None
    source_ocr_cache = source_file_values.get("SHERPA_OCR_MODEL_CACHE")
    if source_ocr_cache and not (scenario is not None and scenario.variable == "SHERPA_OCR_MODEL_CACHE"):
        candidate = Path(source_ocr_cache)
        if not candidate.is_absolute():
            candidate = repository / candidate
        if candidate.is_symlink() or not candidate.resolve().is_dir():
            raise IsolationViolation("SHERPA_OCR_MODEL_CACHE from the explicit env file must be an existing non-symlink directory")
        candidate = candidate.resolve()
        assert_no_mount_targets(candidate)
        for source_item in candidate.rglob("*"):
            if source_item.is_symlink():
                raise IsolationViolation("SHERPA_OCR_MODEL_CACHE must not contain symlinks")
            if not source_item.is_dir() and not source_item.is_file():
                raise IsolationViolation("SHERPA_OCR_MODEL_CACHE must contain only regular files and directories")
        assert_no_mount_targets(candidate)
        assert_no_mount_targets(runtime_root)
        assert_no_mount_targets(runtime_ocr_model_cache)
        shutil.copytree(candidate, runtime_ocr_model_cache, dirs_exist_ok=True)
        assert_no_mount_targets(candidate)
        assert_no_mount_targets(runtime_root)
        assert_no_mount_targets(runtime_ocr_model_cache)
        ocr_model_cache_source = candidate
        ocr_model_cache = runtime_ocr_model_cache

    isolation_values = {
        "APP_PID_FILE": str(run_dir / "api.pid"),
        "CODEX_HOME": str(codex_home),
        "COMPOSE_PROJECT_NAME": project_name,
        "HOME": str(user_home),
        "NEO4J_PASSWORD": neo4j_password,
        "NEO4J_USER": "neo4j",
        "NO_PROXY": "127.0.0.1,localhost",
        "POSTGRES_DB": "sherpa",
        "POSTGRES_PASSWORD": postgres_password,
        "POSTGRES_USER": "sherpa",
        "PYTHON_BIN": os.environ.get("PYTHON_BIN") or os.sys.executable,
        "RUN_DIR": str(run_dir),
        "SHERPA_ADMIN_PASSWORD": admin_password,
        "SHERPA_BROWSE_ROOTS": str(browse_root),
        "SHERPA_COMPOSE_PROJECT": project_name,
        "SHERPA_COOKIE_SECURE": _mapping_value(process_values, "SHERPA_COOKIE_SECURE", "0"),
        "SHERPA_DERIVED_DIR": str(derived_dir),
        "SHERPA_ENV_FILE": str(generated_env_file),
        "SHERPA_HEALTH_CURL_TIMEOUT": "3",
        "SHERPA_HOST": "127.0.0.1",
        "SHERPA_KB_DIR": str(kb_dir),
        "SHERPA_LAN": "0",
        "SHERPA_OBSERVATION_DIR": str(observation_dir),
        "SHERPA_OCR_MODEL_CACHE": str(ocr_model_cache),
        "SHERPA_OCR_WORLD_ROOT": str(world_path),
        "SHERPA_REQUIRE_ENV_FILE": "1",
        "SHERPA_SKIP_PORT_CHECK": "0",
        "SHERPA_USERS_DIR": str(users_dir),
        "SHERPA_UVICORN_WORKERS": "1",
        "TMPDIR": str(temp_dir),
        "XDG_CACHE_HOME": str(cache_home),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "no_proxy": "127.0.0.1,localhost",
    }
    if scenario is not None and scenario.variable in isolation_values and scenario.variable in process_values:
        scenario_value = process_values[scenario.variable]
        if scenario_value is not None:
            isolation_values[scenario.variable] = scenario_value
    isolation_values.update(ports)
    environment.update(isolation_values)
    if neo4j_user_scenario_value is not None:
        present, value = neo4j_user_scenario_value
        if present and value is not None:
            environment["NEO4J_USER"] = value
        else:
            environment.pop("NEO4J_USER", None)
    # 外部cacheをprofile processへ直接渡さない。run-owned shared copyだけを許可する。
    environment.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    if playwright_browsers_path is not None:
        environment["PLAYWRIGHT_BROWSERS_PATH"] = str(playwright_browsers_path)
    _write_private_json(
        profile_root / "state" / "docker-control.json",
        {
            "compose_environment_overrides_removed": removed_compose_overrides,
            "ambient_daemon_endpoint_overrides": ambient_daemon_endpoint_evidence,
            "daemon_endpoint": {
                **docker_socket_identity,
                "policy": "runner-fixed-local-unix-socket",
                "ambient_context_tls_config_allowed": False,
                "raw_endpoint_recorded": False,
            },
            "compose_files": {
                "base_sha256": hashlib.sha256(compose_file.read_bytes()).hexdigest(),
                "owner_override_sha256": hashlib.sha256(compose_override_file.read_bytes()).hexdigest(),
            },
            "project": project_name,
            "owner_hash": owner_hash,
            "raw_endpoint_values_recorded": False,
        },
    )

    restart_environment, restart_missing = _resolve_restart_environment(profile.restart_env, inherited)
    precondition_failures.extend(restart_missing)
    secret_dir = runtime_root / "secrets"
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    chmod_path_no_follow(secret_dir, 0o700, require_owner_uid=os.geteuid())
    secret_registry = secret_dir / "registry.jsonl"
    secret_registry.touch(mode=0o600, exist_ok=False)
    chmod_path_no_follow(secret_registry, 0o600, require_owner_uid=os.geteuid())

    base_url = f"http://127.0.0.1:{app_port}"
    environment.update(
        {
            "SHERPA_UI_ADMIN_PASSWORD": admin_password,
            "SHERPA_UI_ADMIN_NEW_PASSWORD": admin_new_password,
            "SHERPA_UI_ADMIN_CHANGED_PASSWORD": admin_new_password,
            "SHERPA_UI_ADMIN_USER": "admin",
            "SHERPA_UI_ARTIFACT_DIR": str(run_root.resolve()),
            "SHERPA_UI_BASE_URL": base_url,
            "SHERPA_UI_BROWSER_HEADLESS": "0" if headed else "1",
            "SHERPA_UI_DATABASE_URL": database_url,
            "SHERPA_UI_ENV_PROFILE": profile.name,
            "SHERPA_UI_EXPECT_AUTH_DISABLED": "1" if environment.get("SHERPA_AUTH_DISABLED", "").lower() in {"1", "true", "yes"} else "0",
            "SHERPA_UI_ISOLATED": "1",
            "SHERPA_UI_RUN_ID": run_id,
            "SHERPA_UI_SECRET_REGISTRY": str(secret_registry),
            "SHERPA_UI_TIMEOUT_MS": str(case_timeout_ms),
            "SHERPA_UI_WORLD_PATH": str(world_path),
            "UI_AUTOMATION_BASE_URL": base_url,
        }
    )
    runner_boundary = {
        name: {
            "product_sha256": hashlib.sha256(environment.get(name, "").encode("utf-8")).hexdigest(),
            "runner_sha256": hashlib.sha256(
                (trusted_runner_path if name == "PATH" else trusted_python_executable).encode("utf-8")
            ).hexdigest(),
            "separated": environment.get(name, "") != (trusted_runner_path if name == "PATH" else trusted_python_executable),
        }
        for name in ("PATH", "PYTHON_BIN")
    }
    targeted_runner_control = scenario.variable if scenario is not None and scenario.variable in runner_boundary else None
    _write_private_json(
        profile_root / "state" / "runner-execution-boundary.json",
        {
            "status": "PASS",
            "profile": profile.name,
            "scenario_variable": targeted_runner_control,
            "controls": runner_boundary,
            "trusted_python_absolute": Path(trusted_python_executable).is_absolute(),
            "trusted_python_executable": os.access(trusted_python_executable, os.X_OK),
            "runner_uses_product_path_or_python_bin": False,
            "raw_values_recorded": False,
        },
    )
    scenario_contract: dict[str, object] = {}
    if scenario is not None:
        process_actual = process_values.get(scenario.variable)
        process_mode = scenario.process_mode
        if scenario.variable == "APP_PID_FILE" and process_mode in {"absent", "unset"}:
            process_actual = environment["APP_PID_FILE"]
            process_mode = "runtime"
        file_actual = file_values.get(scenario.variable)
        scenario_contract = {
            "generated": True,
            "variable": scenario.variable,
            "scenario": scenario.scenario,
            "scenario_set": scenario.scenario_set,
            "category": scenario.category,
            "secret": scenario.secret,
            "declared_restart": scenario.declared_restart,
            "runner_restart": profile.restart,
            "expected_startup_failure": profile.expect_startup_failure,
            "expected_failure_stage": profile.expected_failure_stage,
            "expected_outcome": scenario.expected_outcome,
            "expected_error_patterns": list(scenario.expected_error_patterns),
            "expected_error_sources": list(scenario.expected_error_sources),
            "process": {
                "mode": process_mode,
                "value": ("set" if process_actual else "unset") if scenario.secret else process_actual,
            },
            "env_file": {
                "mode": scenario.env_file_mode,
                "value": ("set" if file_actual else "unset") if scenario.secret else file_actual,
            },
            "observables": list(scenario.observables),
            "precondition_missing": sorted(set(precondition_failures)),
        }
    if profile.restart_env:
        scenario_contract["restart_transition"] = {
            "id": profile.restart_transition_id,
            "changed_keys": sorted(profile.restart_env),
            "observable": profile.restart_observable,
            "sources": {
                key: str(value.get("from_env") or value.get("source_env"))
                for key, value in profile.restart_env.items()
                if isinstance(value, dict)
            },
        }
    pairwise_expected_values: dict[str, str | None] = {}
    if pairwise is not None:
        for factor in pairwise.factors:
            pairwise_expected_values[factor.key] = process_values.get(factor.key)
    return RunIsolation(
        run_id=run_id,
        profile=profile,
        paths=paths,
        project_name=project_name,
        owner_hash=owner_hash,
        creator_pid=creator_pid,
        creator_start_ticks=creator_start_ticks,
        environment=environment,
        trusted_runner_path=trusted_runner_path,
        trusted_python_executable=trusted_python_executable,
        env_file=generated_env_file,
        compose_file=compose_file,
        compose_override_file=compose_override_file,
        base_url=base_url,
        database_url=database_url,
        world_source_path=world_source_path,
        world_path=world_path,
        ocr_model_cache_source_path=ocr_model_cache_source,
        ocr_model_cache_path=ocr_model_cache,
        playwright_browsers_path=playwright_browsers_path,
        playwright_browsers_signature=playwright_browsers_signature,
        playwright_browsers_entry_count=playwright_browsers_entry_count,
        shared_playwright_cache=shared_playwright_cache,
        admin_password=admin_password,
        ports=ports,
        scenario_contract=scenario_contract,
        pairwise_expected_values=pairwise_expected_values,
        precondition_failures=tuple(sorted(set(precondition_failures))),
        restart_environment=restart_environment,
        restart_transition_id=profile.restart_transition_id,
        secret_registry=secret_registry,
        docker_socket_identity_sha256=str(docker_socket_identity["socket_identity_sha256"]),
    )


def cleanup_failed_isolation(
    *,
    run_id: str,
    profile_name: str,
    evidence_path: Path | None = None,
) -> list[str]:
    """create_isolation途中失敗時も、所有marker一致のruntimeだけを回収する。"""
    project_name = _compose_project_name(run_id, profile_name)
    try:
        parent = runtime_parent_path()
    except IsolationViolation as exc:
        return [f"failed isolation cleanup cannot resolve runtime parent: {exc}"]
    try:
        candidates = list(parent.iterdir())
    except OSError as exc:
        return [f"failed isolation cleanup cannot inspect runtime parent: {type(exc).__name__}: {exc}"]
    errors: list[str] = []
    rows: list[dict[str, object]] = []
    for runtime in candidates:
        if runtime.is_symlink() or not runtime.is_dir() or not runtime.name.startswith(project_name + "-"):
            continue
        row: dict[str, object] = {
            "runtime_name_sha256": hashlib.sha256(runtime.name.encode("utf-8")).hexdigest(),
            "secrets_scrubbed": False,
            "runtime_removed": False,
        }
        try:
            payload, current_schema = validate_runtime_marker_contract(runtime)
            if (
                not current_schema
                or payload.get("run_id") != run_id
                or payload.get("profile") != profile_name
                or payload.get("project") != project_name
                or runtime.resolve().parent != parent
            ):
                raise IsolationViolation("failed isolation runtime ownership differs from this profile")
            scrub = scrub_runtime_secret_files(runtime, payload)
            row["secrets_scrubbed"] = scrub.get("status") == "PASS"
            row["scrub_errors"] = scrub.get("errors") or []
            if scrub.get("status") != "PASS":
                errors.extend(str(message) for message in scrub.get("errors") or ())
                rows.append(row)
                continue
            chmod_tree_no_follow(runtime, directory_mode=0o700, file_mode=0o600, allow_symlinks=True)
            assert_no_mount_targets(runtime)
            rmtree_no_follow(runtime)
            row["runtime_removed"] = True
        except (OSError, ValueError, RuntimeError, IsolationViolation) as exc:
            errors.append(f"failed isolation cleanup failed for {runtime.name}: {type(exc).__name__}: {exc}")
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    if evidence_path is not None:
        try:
            _write_private_json(
                evidence_path,
                {
                    "status": "FAIL" if errors else "PASS",
                    "run_id": run_id,
                    "profile": profile_name,
                    "candidate_count": len(rows),
                    "runtimes": rows,
                    "errors": errors,
                    "raw_secret_values_recorded": False,
                },
            )
        except OSError as exc:
            errors.append(f"failed isolation cleanup evidence failed: {type(exc).__name__}: {exc}")
    return errors
