"""System-side semantic observations for generated environment profiles.

Every adapter in this module observes a product surface in addition to the
pytest process environment.  The caller is responsible for the common
explicit-error/outcome handling; this module only returns successful, concrete
observations or raises when the real system does not demonstrate the contract.

The context is intentionally duck typed.  It is expected to expose ``page``,
``api``, ``config``, ``evidence``, ``contract`` and, optionally, ``cached`` and
``fixture`` with the same meanings as the environment case context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from ui_automation.runner.filesystem_safety import assert_no_mount_targets
from ui_automation.stack.isolation import verify_local_docker_environment


ROOT = Path(__file__).resolve().parents[2]

_SECRET_VARIABLES = frozenset(
    {
        "ANTHROPIC_AWS_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
        "GEMINI_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "OPENAI_API_KEY",
        "PGPASSWORD",
        "POSTGRES_PASSWORD",
        "SHERPA_ADMIN_PASSWORD",
        "SHERPA_AUDIT_IP_SALT",
        "SHERPA_PG_DSN",
        "all_proxy",
        "http_proxy",
        "https_proxy",
    }
)

_PROBE_VARIABLES: dict[str, frozenset[str]] = {
    "ai-health-cache-age": frozenset({"SHERPA_HEALTH_AI_TTL"}),
    "ai-health-duration": frozenset({"SHERPA_HEALTH_AI_TIMEOUT"}),
    "app-log": frozenset(
        {
            "CODEX_BIN_REAL",
            "SHERPA_AGENTIC_MAX_TOOL_RESULT_BYTES",
            "SHERPA_AGENTIC_MAX_TOTAL_TOOL_RESULT_BYTES",
            "SHERPA_ASGI_APP",
            "SHERPA_GREP_FILE_CAP_BYTES",
            "SHERPA_REQUIRE_ENV_FILE",
            "SHERPA_UVICORN_WORKERS",
            "WSL_DISTRO_NAME",
        }
    ),
    "artifact-file": frozenset({"SHERPA_MARP_BIN"}),
    "artifact-render": frozenset({"CHROME_PATH", "CHROMIUM_PATH"}),
    "audit-ip-hash": frozenset({"SHERPA_AUDIT_IP_SALT"}),
    "author-error": frozenset({"SHERPA_CODEX_TIMEOUT_AUTHOR"}),
    "bedrock-auth-kind": frozenset({"AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_SECRET_ACCESS_KEY"}),
    "bedrock-region": frozenset({"AWS_REGION"}),
    "codex-invocation-summary": frozenset({"SHERPA_CODEX_SANDBOX"}),
    "codex-login-status": frozenset({"CODEX_HOME"}),
    "codex-tls-probe": frozenset({"CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE"}),
    "codex-version": frozenset({"CODEX_BIN_REAL", "PATH"}),
    "command-resolution": frozenset({"PATH"}),
    "compatibility-warning": frozenset({"SHERPA_AUTH_ENABLED"}),
    "compose-config": frozenset({"SHERPA_ENV_FILE"}),
    "compose-health": frozenset({"SHERPA_STORE_WAIT"}),
    "compose-mount": frozenset({"SHERPA_OCR_WORLD_ROOT"}),
    "connection-test": frozenset({"SHERPA_EXTRA_AGENTS", "SHERPA_OPENAI_API_VERSION", "SHERPA_OPENAI_AUTH_HEADER"}),
    "created-file-owner": frozenset({"SHERPA_OCR_GID", "SHERPA_OCR_UID"}),
    "derived-files": frozenset({"SHERPA_ARMS", "SHERPA_DERIVED_DIR", "SHERPA_LEGACY_BACKEND"}),
    "folder-picker": frozenset({"SHERPA_BROWSE_ROOTS"}),
    "fs-list-rejection": frozenset({"SHERPA_BROWSE_ROOTS"}),
    "hashed-codex-home": frozenset({"CODEX_HOME", "HOME"}),
    "hashed-home": frozenset({"HOME"}),
    "health-cache-age": frozenset({"SHERPA_HEALTH_TTL"}),
    "health-duration": frozenset({"SHERPA_HEALTH_TIMEOUT"}),
    "ingest-error": frozenset({"SHERPA_LEGACY_TIMEOUT", "SHERPA_VLM_TIMEOUT"}),
    "ingest-log": frozenset({"ES_MAPPING_VERSION", "OPENAI_EMBED_MODEL", "SHERPA_DISABLE_EMBED"}),
    "legacy-backend-selection": frozenset({"WSL_DISTRO_NAME"}),
    "legacy-status": frozenset({"SHERPA_LEGACY_BACKEND"}),
    "login": frozenset(),
    "login-redirect": frozenset({"SHERPA_AUTH_DISABLED"}),
    "login-result": frozenset({"SHERPA_ADMIN_PASSWORD", "SHERPA_COOKIE_SECURE"}),
    "model-files-sha256": frozenset({"SHERPA_OCR_MODEL_CACHE"}),
    "observation-model": frozenset({"SHERPA_VLM_MODEL"}),
    "observation-provider": frozenset({"SHERPA_VLM_PROVIDER"}),
    "ocr-worker-log": frozenset({"SHERPA_OCR_MODEL_CACHE"}),
    "ollama-listen-address": frozenset({"OLLAMA_HOST"}),
    "ollama-model-path": frozenset({"OLLAMA_HOME", "OLLAMA_MODELS_DIR"}),
    "password-change-required": frozenset({"SHERPA_ADMIN_PASSWORD"}),
    "port-owner-check": frozenset({"APP_PROC_NEEDLE"}),
    "postgres-session": frozenset({"SHERPA_SESSION_DAYS"}),
    "postgres-settings": frozenset({"SHERPA_PERSONAL_API_KEYS"}),
    "preflight-exit": frozenset({"SHERPA_REQUIRE_ENV_FILE"}),
    "preflight-log": frozenset({"SHERPA_SKIP_PORT_CHECK"}),
    "process-count": frozenset({"SHERPA_UVICORN_WORKERS"}),
    "process-exit": frozenset({"SHERPA_APP_WAIT", "SHERPA_SKIP_PORT_CHECK", "SHERPA_STOP_WAIT"}),
    "provider-auth-kind": frozenset({"SHERPA_OPENAI_AUTH_HEADER"}),
    "provider-endpoint-kind": frozenset({"OPENAI_BASE_URL"}),
    "provider-model": frozenset({"OPENAI_CHAT_MODEL"}),
    "provider-url-summary": frozenset({"SHERPA_OPENAI_API_VERSION", "SHERPA_OPENAI_ENDPOINT_KIND"}),
    "proxy-log": frozenset({"NO_PROXY", "no_proxy"}),
    "redacted-audit": frozenset({"SHERPA_ADMIN_PASSWORD"}),
    "redacted-compose-config": frozenset({"NEO4J_PASSWORD", "POSTGRES_PASSWORD"}),
    "redacted-effective-environment": frozenset(
        {
            "ALL_PROXY",
            "DATABASE_URL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "PGPASSWORD",
            "SHERPA_AUDIT_IP_SALT",
            "SHERPA_PG_DSN",
            "all_proxy",
            "http_proxy",
            "https_proxy",
        }
    ),
    "request-count": frozenset({"SHERPA_HEALTH_TTL"}),
    "restart-effective-environment": frozenset(),
    "role-boundary": frozenset({"SHERPA_AUTH_DISABLED"}),
    "session-expiry": frozenset({"SHERPA_SESSION_DAYS"}),
    "settings-connection-state": frozenset(
        {
            "ANTHROPIC_AWS_API_KEY",
            "AWS_BEARER_TOKEN_BEDROCK",
            "GEMINI_API_KEY",
            "OLLAMA_URL",
            "OPENAI_API_KEY",
        }
    ),
    "settings-fields": frozenset({"SHERPA_PERSONAL_API_KEYS"}),
    "settings-labels": frozenset({"SHERPA_OPENAI_ENDPOINT_KIND"}),
    "settings-options": frozenset({"SHERPA_EXTRA_AGENTS"}),
    "settings-reasoning": frozenset({"SHERPA_CODEX_REASONING"}),
    "settings-toggle": frozenset({"SHERPA_ALLOW_WEB_SEARCH"}),
    "shutdown-duration": frozenset({"SHERPA_STOP_WAIT"}),
    "startup-duration": frozenset({"SHERPA_APP_WAIT", "SHERPA_STORE_WAIT"}),
    "status-command-duration": frozenset({"SHERPA_HEALTH_CURL_TIMEOUT"}),
    "status-output": frozenset({"SHERPA_HEALTH_CURL_TIMEOUT"}),
    "unicode-ui": frozenset({"LANG"}),
    "usage-page": frozenset({"SHERPA_USAGE_METERING"}),
    "volume-identity": frozenset({"KEEP_STORES"}),
    "volume-names": frozenset({"COMPOSE_PROJECT_NAME", "SHERPA_COMPOSE_PROJECT"}),
    "worker-version": frozenset({"SHERPA_POWERSHELL_BIN"}),
    "world-registry": frozenset({"SHERPA_KB_DIR"}),
    "world-root-check": frozenset({"SHERPA_OCR_WORLD_ROOT"}),
    "write-boundary": frozenset({"HOME", "RUN_DIR", "SHERPA_CODEX_SANDBOX"}),
}

# Public, immutable view used by the manifest validator.  A probe id alone is
# not a semantic contract: generated environment coverage is valid only when
# the concrete ``(variable, probe)`` pair is declared here.
SEMANTIC_PROBE_VARIABLES = {probe_id: variables for probe_id, variables in _PROBE_VARIABLES.items()}


def supports_semantic_probe(probe_id: str, variable: str | None) -> bool:
    """Return whether a generated variable/probe pair has system semantics."""

    return bool(variable) and str(variable) in SEMANTIC_PROBE_VARIABLES.get(str(probe_id), frozenset())


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _bool_value(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    raise AssertionError(f"invalid boolean value reached semantic observation: {value!r}")


def _scenario(ctx: Any) -> dict[str, Any]:
    value = (ctx.contract or {}).get("generated_scenario") or {}
    assert isinstance(value, dict), "generated_scenario must be an object"
    return value


def _expected(ctx: Any) -> dict[str, Any]:
    value = (ctx.contract or {}).get("expected") or {}
    assert isinstance(value, dict), "environment expected contract must be an object"
    return value


def _profile_root(ctx: Any) -> Path:
    path = ctx.config.expected_env_path
    assert path is not None and Path(path).is_file(), "expected environment evidence is absent"
    return Path(path).resolve().parent.parent


def _cache(ctx: Any, key: str, callback: Callable[[], Any]) -> Any:
    cached = getattr(ctx, "cached", None)
    if callable(cached):
        return cached(f"system-semantics:{key}", callback)
    store = getattr(ctx, "cache", None)
    if not isinstance(store, dict):
        store = {}
        setattr(ctx, "cache", store)
    namespaced = f"system-semantics:{key}"
    if namespaced not in store:
        store[namespaced] = callback()
    return store[namespaced]


def _fixture(ctx: Any, name: str) -> Any:
    accessor = getattr(ctx, "fixture", None)
    if callable(accessor):
        return accessor(name)
    return ctx.request.getfixturevalue(name)


def _load_json(path: Path) -> Any:
    assert path.is_file() and not path.is_symlink(), f"required JSON evidence is absent: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_file_summary(path: Path) -> dict[str, Any]:
    assert path.is_file() and not path.is_symlink(), f"required real file is absent: {path}"
    raw = path.read_bytes()
    assert b"\x00" not in raw, f"text evidence contains a sparse/NUL region: {path.name}"
    return {
        "name": path.name,
        "bytes": len(raw),
        "sha256": _sha256_bytes(raw),
        "line_count": len(raw.splitlines()),
        "mode": oct(stat.S_IMODE(path.stat().st_mode)),
    }


def _tree_summary(root: Path) -> dict[str, Any]:
    assert root.is_dir() and not root.is_symlink(), f"required real directory is absent: {root}"
    assert_no_mount_targets(root)
    digest = hashlib.sha256()
    files = 0
    directories = 0
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        assert not path.is_symlink(), f"semantic tree must not contain symlinks: {path}"
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            directories += 1
            digest.update(f"d\0{relative}\n".encode())
        elif path.is_file():
            raw = path.read_bytes()
            files += 1
            total_bytes += len(raw)
            digest.update(f"f\0{relative}\0{len(raw)}\0".encode())
            digest.update(hashlib.sha256(raw).digest())
        else:
            raise AssertionError(f"semantic tree contains a special file: {path}")
    return {
        "files": files,
        "directories": directories,
        "bytes": total_bytes,
        "tree_sha256": digest.hexdigest(),
    }


def _execution_tree_summary(root: Path) -> dict[str, Any]:
    """Hash execution outputs without reading or exporting Codex authentication material."""

    assert root.is_dir() and not root.is_symlink()
    assert_no_mount_targets(root)
    runtime_root = Path(os.environ["RUN_DIR"]).resolve().parent
    digest = hashlib.sha256()
    files = 0
    directories = 0
    auth_entries = 0
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = path.resolve()
            assert path.name == "auth.json" and target.is_relative_to(runtime_root)
            auth_entries += 1
            digest.update(f"auth-link\0{relative}\0{_sha256_text(str(target))}\n".encode())
        elif path.is_dir():
            directories += 1
            digest.update(f"d\0{relative}\n".encode())
        elif path.is_file():
            size = path.stat().st_size
            files += 1
            total_bytes += size
            if path.name == "auth.json":
                auth_entries += 1
                digest.update(f"auth-file\0{relative}\0{size}\n".encode())
            else:
                raw = path.read_bytes()
                digest.update(f"f\0{relative}\0{len(raw)}\0".encode())
                digest.update(hashlib.sha256(raw).digest())
        else:
            raise AssertionError(f"execution tree contains a special file: {path}")
    return {
        "files": files,
        "directories": directories,
        "bytes": total_bytes,
        "auth_entries_presence_only": auth_entries,
        "tree_sha256": digest.hexdigest(),
    }


def _assert_probe_contract(ctx: Any, probe_id: str) -> dict[str, Any]:
    assert probe_id in _PROBE_VARIABLES, f"unsupported system semantic probe: {probe_id}"
    scenario = _scenario(ctx)
    variable = str(scenario.get("variable") or "")
    allowed = _PROBE_VARIABLES[probe_id]
    if scenario:
        assert variable, "generated scenario omitted its variable"
        declared = {str(value) for value in scenario.get("observables") or ()}
        assert probe_id in declared, f"{probe_id} is not declared by generated scenario for {variable}"
        assert variable in allowed, f"{probe_id} cannot semantically demonstrate generated variable {variable}"
        process = scenario.get("process") or {}
        assert isinstance(process, dict), "generated process contract must be an object"
        mode = str(process.get("mode") or "")
        actual = os.environ.get(variable)
        if mode == "unset" and variable not in {
            "CODEX_HOME",
            "HOME",
            "SHERPA_BROWSE_ROOTS",
            "SHERPA_DERIVED_DIR",
            "SHERPA_ENV_FILE",
            "SHERPA_KB_DIR",
            "SHERPA_OCR_MODEL_CACHE",
            "SHERPA_OCR_WORLD_ROOT",
        }:
            assert actual is None, f"{variable} should be absent in the generated default process"
        elif mode not in {"", "absent", "unset"}:
            expected_value = process.get("value")
            if variable in _SECRET_VARIABLES:
                assert bool(actual) is (str(expected_value) == "set"), f"{variable} secret presence differs from generated scenario"
            else:
                assert actual == expected_value, f"{variable} process value differs from generated scenario"
    else:
        declared = {str(item.get("id") if isinstance(item, dict) else item) for item in (ctx.contract or {}).get("probes") or ()}
        assert probe_id in declared, f"static profile did not declare {probe_id}"
    return {
        "variable": variable or None,
        "scenario": scenario.get("scenario") if scenario else None,
        "scenario_set": scenario.get("scenario_set") if scenario else None,
        "process_present": bool(variable and os.environ.get(variable) is not None),
        "process_value_sha256": (
            _sha256_text(os.environ[variable]) if variable and variable not in _SECRET_VARIABLES and variable in os.environ else None
        ),
        "secret_presence_only": variable in _SECRET_VARIABLES,
    }


def _result(probe_id: str, contract: dict[str, Any], source: str, **measurements: Any) -> dict[str, Any]:
    assert source.strip(), f"{probe_id} returned an empty evidence source"
    assert measurements, f"{probe_id} returned no concrete measurements"
    return {
        "source": source,
        "probe_id": probe_id,
        "generated_contract": contract,
        "measurements": measurements,
    }


def _db_rows(ctx: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for system semantic database evidence") from exc
    with psycopg.connect(
        ctx.config.database_url,
        row_factory=dict_row,
        connect_timeout=5,
    ) as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _app_pid(ctx: Any) -> tuple[int, Path, str]:
    raw_path = os.environ.get("APP_PID_FILE", "")
    assert raw_path, "APP_PID_FILE is absent from the isolated process"
    path = Path(raw_path)
    assert not path.is_symlink(), f"runner-owned PID path is a symlink: {path}"
    run_dir = Path(os.environ["RUN_DIR"]).resolve()
    assert path.parent.resolve() == run_dir, "APP_PID_FILE is outside the runner-owned RUN_DIR"
    candidates: list[int] = []
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        assert text.isdigit(), f"runner-owned PID file is malformed: {path}"
        candidates.append(int(text))
    port = int(urlsplit(ctx.config.base_url).port or 0)
    signature = f"--port {port}".encode()
    for proc_root in Path("/proc").iterdir():
        if not proc_root.name.isdigit() or int(proc_root.name) in candidates:
            continue
        try:
            if proc_root.stat().st_uid != os.geteuid():
                continue
            raw = (proc_root / "cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if signature in raw and b"uvicorn" in raw:
            candidates.append(int(proc_root.name))
    live: list[tuple[int, str]] = []
    for pid in candidates:
        process_root = Path("/proc") / str(pid)
        try:
            cmdline = (process_root / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if signature.decode() in cmdline and "uvicorn" in cmdline:
            live.append((pid, cmdline))
    assert len(live) == 1, f"expected one runner-owned uvicorn process on port {port}, got {len(live)}"
    pid, cmdline = live[0]
    process_root = Path("/proc") / str(pid)
    assert process_root.is_dir(), f"resolved application process is not running: {pid}"
    assert cmdline.strip(), f"application process {pid} has an empty command line"
    assert process_root.stat().st_uid == os.geteuid()
    if path.exists():
        assert path.stat().st_uid == os.geteuid()
    return pid, path, cmdline


@contextmanager
def _status_pid_evidence(ctx: Any):
    """Expose the runner-owned app PID only while product status.sh is exercised."""

    pid, path, cmdline = _app_pid(ctx)
    created = not path.exists()
    if created:
        write_private_text_atomic(path, f"{pid}\n")
    try:
        yield pid, path, cmdline
    finally:
        if created and path.is_file() and path.read_text(encoding="utf-8").strip() == str(pid):
            path.unlink()


def _proc_environment(pid: int) -> dict[str, str]:
    raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    values: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        values[key.decode(errors="replace")] = value.decode(errors="replace")
    return values


def _run(
    argv: list[str],
    *,
    timeout: float = 30,
    input_text: str | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    assert argv and all(isinstance(value, str) and value for value in argv)
    effective_environment = dict(os.environ) if environment is None else environment
    if argv[0] == "docker":
        verify_local_docker_environment(effective_environment)
    return subprocess.run(
        argv,
        cwd=ROOT,
        env=effective_environment,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _compose_argv(*args: str, ocr: bool = False) -> list[str]:
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    env_file = os.environ.get("SHERPA_ENV_FILE", "")
    assert project.startswith("sherpa-ui-automation-"), "Compose project is not runner-owned"
    assert env_file and Path(env_file).is_file(), "explicit Compose env file is absent"
    argv = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        env_file,
    ]
    if ocr:
        argv.extend(["--profile", "ocr"])
    argv.extend(args)
    return argv


def _compose_config() -> dict[str, Any]:
    completed = _run(_compose_argv("config", "--format", "json", ocr=True), timeout=45)
    assert completed.returncode == 0, "real docker compose config failed"
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict) and isinstance(payload.get("services"), dict)
    return payload


def _docker_containers() -> list[dict[str, Any]]:
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    listed = _run(
        [
            "docker",
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.ID}}",
        ],
        timeout=30,
    )
    assert listed.returncode == 0, "docker ps failed for isolated Compose project"
    identifiers = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    assert identifiers, "isolated Compose project has no containers"
    inspected = _run(["docker", "inspect", *identifiers], timeout=30)
    assert inspected.returncode == 0, "docker inspect failed for isolated containers"
    payload = json.loads(inspected.stdout)
    assert isinstance(payload, list) and payload
    for item in payload:
        labels = (item.get("Config") or {}).get("Labels") or {}
        assert labels.get("com.docker.compose.project") == project
    return payload


def _service_logs() -> list[Path]:
    return sorted(
        path
        for path in (_profile_root_from_environment() / "services").glob("*.log")
        if path.is_file() and not path.is_symlink() and path.stat().st_size > 0
    )


def _profile_root_from_environment() -> Path:
    configured = os.environ.get("SHERPA_UI_EXPECTED_ENV_JSON", "")
    assert configured, "SHERPA_UI_EXPECTED_ENV_JSON is absent"
    return Path(configured).resolve().parent.parent


def _combined_log_tail(paths: Iterable[Path], limit_per_file: int = 200_000) -> tuple[bytes, list[str]]:
    chunks: list[bytes] = []
    names: list[str] = []
    for path in paths:
        raw = path.read_bytes()
        assert b"\x00" not in raw, f"service log contains a sparse/NUL region: {path.name}"
        chunks.append(raw[-limit_per_file:])
        names.append(path.name)
    return b"\n".join(chunks), names


def _anonymous_get(ctx: Any, path: str) -> tuple[int, str]:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        ctx.config.base_url + path,
        headers={"User-Agent": "sherpa-ui-automation-anonymous-semantic-probe"},
    )
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")


def _active_session(ctx: Any) -> tuple[dict[str, Any], str]:
    cookies = ctx.page.context.cookies(ctx.config.base_url)
    session_candidates = [row for row in cookies if row.get("httpOnly") and isinstance(row.get("value"), str) and row.get("value")]
    assert len(session_candidates) == 1, f"expected one authenticated HttpOnly session cookie, got {len(session_candidates)}"
    cookie = session_candidates[0]
    token_hash = _sha256_text(str(cookie["value"]))
    rows = _db_rows(
        ctx,
        "SELECT id,user_id,created_at,expires_at,last_seen_at,revoked_at FROM auth_sessions WHERE token_hash=%s",
        (token_hash,),
    )
    assert len(rows) == 1, "browser session cookie does not correlate to one database session"
    return rows[0], _sha256_text(f"auth-session:{rows[0]['id']}")[:16]


def _real_world(ctx: Any) -> str:
    def collect() -> str:
        from ui_automation.support.world import ensure_real_world

        return ensure_real_world(ctx.api, ctx.config, ctx.evidence)

    return str(_cache(ctx, "real-world", collect))


def _world_index_name(world_id: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]", "-", world_id.lower())[:40].strip("-._") or "w"
    return f"sherpa-kb-{slug}-{hashlib.sha1(world_id.encode()).hexdigest()[:10]}"


def _elasticsearch_mapping(ctx: Any, world_id: str) -> dict[str, Any]:
    endpoint = os.environ["ES_URL"].rstrip("/")
    parsed = urlsplit(endpoint)
    assert parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    url = f"{endpoint}/{_world_index_name(world_id)}/_mapping"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read())
    elapsed_ms = int((time.monotonic() - started) * 1000)
    ctx.evidence.record_api(method="GET", url=url, status=response.status, elapsed_ms=elapsed_ms)
    assert response.status == 200 and isinstance(payload, dict) and payload
    mappings = (next(iter(payload.values())) or {}).get("mappings") or {}
    return {
        "properties": mappings.get("properties") or {},
        "meta": mappings.get("_meta") or {},
        "elapsed_ms": elapsed_ms,
    }


def _settings_surfaces(ctx: Any) -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        settings = ctx.api.get_json("/settings", save_as="state/system-semantic-settings.json")
        admin = ctx.api.get_json("/admin/settings", save_as="state/system-semantic-admin-settings.json")
        config = ctx.api.get_json("/config", save_as="state/system-semantic-provider-config.json")
        assert isinstance(settings, dict) and isinstance(admin, dict) and isinstance(config, dict)
        return {"settings": settings, "admin": admin, "config": config}

    return _cache(ctx, "settings-surfaces", collect)


def _admin_health(ctx: Any, *, refresh: bool) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    path = "/admin/health?refresh=1" if refresh else "/admin/health"
    response = ctx.api.get_json(path)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    assert isinstance(response.get("components"), list) and response["components"]
    return response, elapsed_ms


def _component(health: dict[str, Any], component_id: str) -> dict[str, Any]:
    rows = [row for row in health.get("components") or [] if row.get("id") == component_id]
    assert len(rows) == 1, f"health response has no unique {component_id} component"
    return rows[0]


def _provider_connection(
    ctx: Any,
    provider: str,
    *,
    expected_ok: bool = True,
) -> dict[str, Any]:
    settings = _settings_surfaces(ctx)["settings"]
    body: dict[str, Any] = {"provider": provider}
    model_key = {
        "bedrock": "bedrock_model",
        "codex": "codex_model",
        "gemini": "gemini_model",
        "ollama": "ollama_model",
        "openai": "openai_model",
    }.get(provider)
    if model_key and settings.get(model_key):
        body[model_key] = settings[model_key]
    started = time.monotonic()
    response = ctx.api.post_json("/settings/test", body)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    assert response.get("provider") == provider, response
    assert bool(response.get("ok")) is expected_ok, (
        f"real {provider} connection result differed from configured availability: "
        f"detail_sha256={_sha256_text(str(response.get('detail') or ''))}"
    )
    return {
        "provider": provider,
        "model_sha256": _sha256_text(str(response.get("model") or "")),
        "detail_sha256": _sha256_text(str(response.get("detail") or "")),
        "elapsed_ms": elapsed_ms,
        "ok": expected_ok,
    }


def _exercise_codex_turn(ctx: Any) -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        from ui_automation.support.database import conversation_database_snapshot
        from ui_automation.support.chat_flow import (
            last_assistant_message,
            prepare_chat,
            start_turn_from_ui,
            wait_for_completed_ui,
        )

        world_id = _real_world(ctx)
        settings = _settings_surfaces(ctx)["settings"]
        assert settings.get("agent") == "codex", "Codex semantic probe requires the real selected agent to be codex"
        prepare_chat(ctx.page, ctx.config, world_id)
        marker = f"system-semantic-{time.time_ns()}"
        started = start_turn_from_ui(
            ctx.page,
            ctx.config,
            "UI Automation Evidence World の固有事実を1つ検索し、根拠付きで短く答えてください。 " + marker,
        )
        events = ctx.api.collect_sse(
            f"/chat/turns/{quote(str(started['turn_id']))}/stream?cursor=0",
            save_as="network/system-semantic-codex-sse.jsonl",
        )
        wait_for_completed_ui(ctx.page, ctx.config.timeout_ms)
        conversation = ctx.api.get_json(f"/conversations/{int(started['conversation_id'])}")
        assistant = last_assistant_message(conversation)
        database = conversation_database_snapshot(
            ctx.config.database_url,
            int(started["conversation_id"]),
            ctx.evidence,
            turn_id=str(started["turn_id"]),
        )
        database_assistants = [row for row in database["messages"] if row.get("role") == "assistant"]
        assert database_assistants and database_assistants[-1].get("content") == assistant.get("content"), (
            "system semantic Codex API answer differs from direct Postgres"
        )
        answer = assistant.get("answer") or {}
        usage = answer.get("usage") or {}
        assert str(usage.get("provider") or "") == "codex", usage
        assert int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0) > 0
        nodes = [event for event in events if event.get("type") == "node"]
        assert nodes and any(event.get("kind") == "tool" for event in nodes)
        ctx.evidence.add_cleanup(
            "delete system semantic Codex conversation",
            lambda: ctx.api.delete_json(f"/conversations/{int(started['conversation_id'])}"),
        )
        return {
            "turn_id_sha256": _sha256_text(str(started["turn_id"])),
            "conversation_id_sha256": _sha256_text(str(started["conversation_id"])),
            "provider": "codex",
            "model_sha256": _sha256_text(str(usage.get("model") or "")),
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "node_event_count": len(nodes),
            "tool_event_count": sum(event.get("kind") == "tool" for event in nodes),
            "answer_sha256": _sha256_text(str(answer.get("headline") or assistant.get("content") or "")),
        }

    return _cache(ctx, "codex-turn", collect)


def _exercise_author_turn(ctx: Any, *, expect_error: bool) -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        from ui_automation.support.chat_flow import prepare_chat, start_turn_from_ui
        from ui_automation.support.database import conversation_database_snapshot

        world_id = _real_world(ctx)
        settings = _settings_surfaces(ctx)["settings"]
        assert settings.get("agent") == "codex"
        prepare_chat(ctx.page, ctx.config, world_id)
        started = start_turn_from_ui(
            ctx.page,
            ctx.config,
            "取り込んだ資料を検索して、根拠付き回答を作成してください。",
        )
        events = ctx.api.collect_sse(
            f"/chat/turns/{quote(str(started['turn_id']))}/stream?cursor=0",
            save_as="network/system-semantic-author-error-sse.jsonl",
        )
        errors = [event for event in events if event.get("type") == "error"]
        answers = [event for event in events if event.get("type") == "answer"]
        if expect_error:
            assert errors or not answers, "author timeout profile emitted a terminal successful answer instead of a real error"
        else:
            assert answers and not errors, "positive author timeout did not complete a real author turn"
        database = conversation_database_snapshot(
            ctx.config.database_url,
            int(started["conversation_id"]),
            ctx.evidence,
            turn_id=str(started["turn_id"]),
        )
        turn_audits = [row for row in database["audit"] if row.get("action") == "chat.turn"]
        assert turn_audits, "author turn has no direct Postgres audit row"
        if expect_error:
            assert turn_audits[-1].get("outcome") == "error", "author timeout was not persisted as an error"
        ctx.evidence.add_cleanup(
            "delete system semantic failed conversation",
            lambda: ctx.api.delete_json(f"/conversations/{int(started['conversation_id'])}"),
        )
        return {
            "turn_id_sha256": _sha256_text(str(started["turn_id"])),
            "error_event_count": len(errors),
            "answer_event_count": len(answers),
            "event_count": len(events),
        }

    return _cache(ctx, f"author-turn:{expect_error}", collect)


def observe_secure_http_login(ctx: Any) -> dict[str, Any]:
    """Observe a Secure login cookie without pretending it works over HTTP.

    The normal ``admin_page`` fixture cannot authenticate when a correctly
    configured browser refuses to send a Secure cookie over the runner's HTTP
    origin.  This probe therefore performs one real login request, hashes the
    token only in memory, correlates it with Postgres, and proves that an
    independent cookie-less request remains unauthenticated.
    """

    def collect() -> dict[str, Any]:
        from sherpa import auth as product_auth

        assert urlsplit(ctx.config.base_url).scheme == "http"
        assert _bool_value(os.environ.get("SHERPA_COOKIE_SECURE")) is True
        assert _bool_value(os.environ.get("SHERPA_AUTH_DISABLED")) is False
        credentials = _fixture(ctx, "admin_credentials")
        body = json.dumps(
            {
                "username": credentials.username,
                "password": credentials.active_password,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        login_url = ctx.config.base_url.rstrip("/") + "/auth/login"
        request = urllib.request.Request(
            login_url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "sherpa-ui-automation-secure-cookie-probe",
            },
            method="POST",
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=max(ctx.config.timeout_ms / 1000, 10)) as response:
            status = int(response.status)
            payload = json.loads(response.read().decode("utf-8"))
            set_cookie_headers = response.headers.get_all("Set-Cookie") or []
        ctx.evidence.record_api(
            method="POST",
            url=login_url,
            status=status,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        assert status == 200 and payload.get("ok") is True
        parsed = SimpleCookie()
        for header in set_cookie_headers:
            parsed.load(header)
        assert "sherpa_session" in parsed, "real login response did not issue the session cookie"
        morsel = parsed["sherpa_session"]
        token = morsel.value
        assert token and bool(morsel["secure"]) and bool(morsel["httponly"])
        token_hash = product_auth.token_hash(token)
        sessions = _db_rows(
            ctx,
            "SELECT id,created_at,expires_at,revoked_at FROM auth_sessions WHERE token_hash=%s",
            (token_hash,),
        )
        assert len(sessions) == 1 and sessions[0].get("revoked_at") is None

        def revoke_session() -> None:
            import psycopg

            with psycopg.connect(ctx.config.database_url, connect_timeout=5) as connection:
                connection.execute(
                    "UPDATE auth_sessions SET revoked_at=now() WHERE token_hash=%s AND revoked_at IS NULL",
                    (token_hash,),
                )

        ctx.evidence.add_cleanup("revoke secure HTTP environment-probe session", revoke_session)

        me_url = ctx.config.base_url.rstrip("/") + "/auth/me"
        anonymous = urllib.request.Request(
            me_url,
            headers={"Accept": "application/json", "User-Agent": "sherpa-ui-automation-secure-cookie-probe"},
            method="GET",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(anonymous, timeout=10) as response:
                anonymous_status = int(response.status)
                response.read()
        except urllib.error.HTTPError as exc:
            anonymous_status = int(exc.code)
            exc.read()
        ctx.evidence.record_api(
            method="GET",
            url=me_url,
            status=anonymous_status,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        assert anonymous_status == 401, "Secure cookie was incorrectly treated as an HTTP-authenticated session"
        lifetime_seconds = (sessions[0]["expires_at"] - sessions[0]["created_at"]).total_seconds()
        return {
            "auth_disabled": False,
            "login_status": status,
            "uid_matches_runner_admin": payload.get("uid") == ctx.config.admin_user,
            "role": "admin",
            "http_only_session_count": 1,
            "secure_flags": [True],
            "same_site_flags": [str(morsel["samesite"] or "")],
            "cookie_transmitted_over_http": False,
            "anonymous_me_status": anonymous_status,
            "database_session_active": True,
            "session_id_sha256": _sha256_text(str(sessions[0]["id"])),
            "token_hash_sha256": _sha256_text(token_hash),
            "lifetime_seconds": lifetime_seconds,
            "secret_values_returned": False,
        }

    return _cache(ctx, "secure-http-login", collect)


def _auth_probe(ctx: Any, probe_id: str, contract: dict[str, Any]) -> dict[str, Any]:
    variable = str(_scenario(ctx).get("variable") or "")
    secure_http = (
        probe_id in {"login", "login-result"}
        and variable == "SHERPA_COOKIE_SECURE"
        and os.environ.get("SHERPA_COOKIE_SECURE", "").strip().casefold() in {"1", "true", "yes", "on"}
        and urlsplit(ctx.config.base_url).scheme == "http"
        and not _bool_value(os.environ.get("SHERPA_AUTH_DISABLED"))
    )
    if secure_http:
        observed = observe_secure_http_login(ctx)
        return _result(
            probe_id,
            contract,
            "real HTTP login Set-Cookie header, direct auth_sessions row, and cookie-less /auth/me rejection",
            **observed,
        )
    me = ctx.api.get_json("/auth/me")
    auth_disabled = bool(me.get("auth_disabled"))
    if probe_id == "login-redirect":
        status, location = _anonymous_get(ctx, "/ui/home.html")
        if auth_disabled:
            assert status == 200 and not location
        else:
            assert status in {302, 303, 307, 308}
            assert "/ui/login.html" in location
        return _result(
            probe_id,
            contract,
            "anonymous real HTTP navigation and authenticated GET /auth/me",
            auth_disabled=auth_disabled,
            anonymous_status=status,
            redirected_to_login=bool(location and "/ui/login.html" in location),
        )

    if probe_id == "role-boundary":
        me_status, _ = _anonymous_get(ctx, "/auth/me")
        admin_status, _ = _anonymous_get(ctx, "/admin/settings")
        if auth_disabled:
            assert me_status == 200 and admin_status == 200
        else:
            assert me_status == 401 and admin_status == 401
        return _result(
            probe_id,
            contract,
            "anonymous GET /auth/me and /admin/settings authorization boundary",
            auth_disabled=auth_disabled,
            anonymous_me_status=me_status,
            anonymous_admin_status=admin_status,
            fail_closed_when_enabled=not auth_disabled,
        )

    if probe_id in {"login", "login-result"}:
        assert me.get("uid") == ctx.config.admin_user and me.get("role") == "admin"
        if auth_disabled:
            assert probe_id != "login", "auth-enabled profile returned compatibility admin"
            return _result(
                probe_id,
                contract,
                "authenticated GET /auth/me compatibility-mode result",
                auth_disabled=True,
                synthesized_admin=True,
                session_issued=False,
            )
        session, session_digest = _active_session(ctx)
        cookies = [row for row in ctx.page.context.cookies(ctx.config.base_url) if row.get("httpOnly")]
        expected_secure = _bool_value(os.environ.get("SHERPA_COOKIE_SECURE"))
        assert all(bool(row.get("secure")) is expected_secure for row in cookies)
        return _result(
            probe_id,
            contract,
            "real login UI session correlated with GET /auth/me and auth_sessions",
            auth_disabled=False,
            uid_matches=True,
            role="admin",
            session_id_sha256=session_digest,
            database_session_active=session.get("revoked_at") is None,
            cookie_http_only=True,
            cookie_secure=expected_secure,
            cookie_same_site=sorted({str(row.get("sameSite")) for row in cookies}),
        )

    if probe_id in {"postgres-session", "session-expiry"}:
        assert not auth_disabled, "session lifetime is not applicable in auth-disabled mode"
        session, session_digest = _active_session(ctx)
        seconds = (session["expires_at"] - session["created_at"]).total_seconds()
        configured_days = int(os.environ.get("SHERPA_SESSION_DAYS") or "7")
        assert abs(seconds - configured_days * 86400) < 5
        assert session.get("revoked_at") is None
        return _result(
            probe_id,
            contract,
            "browser cookie hash correlated with direct auth_sessions expiry query",
            session_id_sha256=session_digest,
            configured_days=configured_days,
            lifetime_seconds=seconds,
            active=True,
        )

    if probe_id == "password-change-required":
        credentials = _fixture(ctx, "admin_credentials")
        rows = _db_rows(
            ctx,
            "SELECT uid,must_change_password,last_login_at,password_hash IS NOT NULL AS password_set FROM users WHERE uid=%s",
            (ctx.config.admin_user,),
        )
        assert len(rows) == 1 and rows[0]["password_set"] is True
        assert rows[0]["must_change_password"] is False
        assert bool(credentials.initial_change_completed)
        audits = _db_rows(
            ctx,
            "SELECT id,action,outcome FROM audit_log WHERE actor_user_id=%s "
            "AND action='auth.initial_password_changed' ORDER BY id DESC LIMIT 1",
            (ctx.config.admin_user,),
        )
        assert len(audits) == 1 and audits[0]["outcome"] == "success"
        return _result(
            probe_id,
            contract,
            "real first-login UI transition, users row, and password-change audit",
            initial_change_completed=True,
            database_must_change=False,
            password_hash_present=True,
            audit_id_sha256=_sha256_text(str(audits[0]["id"])),
        )

    if probe_id in {"audit-ip-hash", "redacted-audit"}:
        rows = _db_rows(
            ctx,
            "SELECT id,action,ip_hash,detail::text AS detail,before_state::text AS before_state,"
            "after_state::text AS after_state FROM audit_log ORDER BY id",
        )
        assert rows, "real authentication produced no audit rows"
        if probe_id == "audit-ip-hash":
            hashed = [row for row in rows if row.get("ip_hash")]
            assert hashed and all(re.fullmatch(r"[0-9a-f]{64}", row["ip_hash"]) for row in hashed)
            expected_hash = _sha256_text(os.environ.get("SHERPA_AUDIT_IP_SALT", "") + "127.0.0.1")
            assert any(row["ip_hash"] == expected_hash for row in hashed)
            return _result(
                probe_id,
                contract,
                "direct audit_log IP hash rows correlated with loopback request origin",
                hashed_row_count=len(hashed),
                sha256_format_valid=True,
                expected_salted_loopback_hash_present=True,
            )
        secret = os.environ.get("SHERPA_ADMIN_PASSWORD", "")
        assert secret, "admin password presence is required for redaction verification"
        serialized = "\n".join(str(row.get(key) or "") for row in rows for key in ("detail", "before_state", "after_state"))
        assert secret not in serialized, "admin password leaked into audit_log JSON"
        return _result(
            probe_id,
            contract,
            "direct audit_log JSON scan using presence-only runtime secret",
            audited_row_count=len(rows),
            secret_present_in_process=True,
            secret_plaintext_match_count=0,
            serialized_audit_sha256=_sha256_text(serialized),
        )

    raise AssertionError(f"unhandled auth semantic probe: {probe_id}")


def _browser_probe(ctx: Any, probe_id: str, contract: dict[str, Any]) -> dict[str, Any]:
    if probe_id == "fs-list-rejection":
        response = ctx.api.request("GET", "/fs/list?path=%2F", expected=403)
        body = response.json()
        assert body.get("detail") and "範囲外" in str(body["detail"])
        return _result(
            probe_id,
            contract,
            "real GET /fs/list outside the configured browse root",
            status=response.status,
            rejected=True,
            detail_sha256=_sha256_text(str(body["detail"])),
        )

    if probe_id == "folder-picker":
        ctx.page.goto(ctx.config.base_url + "/ui/ingest.html")
        ctx.page.locator("#pickbtn").wait_for(state="visible")
        ctx.page.locator("#pickbtn").click()
        ctx.page.locator("#ovl.open").wait_for(state="visible")
        ctx.page.locator("#pbody [data-cd]").first.wait_for(state="visible")
        entries = ctx.page.locator("#pbody [data-cd]").evaluate_all(
            "els => els.map(el => ({name: el.textContent.trim(), path: el.dataset.cd}))"
        )
        assert entries and all(row.get("path") for row in entries)
        configured_roots = [Path(value).resolve() for value in os.environ.get("SHERPA_BROWSE_ROOTS", "").split(":") if value] or [
            Path("/mnt").resolve()
        ]
        assert all(
            any(
                Path(str(row["path"])).resolve() == root or Path(str(row["path"])).resolve().is_relative_to(root)
                for root in configured_roots
            )
            for row in entries
        )
        ctx.page.locator("#pbody [data-cd]").first.click()
        current = ctx.page.locator("#pickcur").inner_text().strip()
        assert current and any(Path(current).resolve() == root or Path(current).resolve().is_relative_to(root) for root in configured_roots)
        assert ctx.page.locator("#pchoose").is_enabled()
        ctx.page.locator("#pcancel").click()
        return _result(
            probe_id,
            contract,
            "real ingest folder-picker DOM backed by GET /fs/list",
            top_level_entry_count=len(entries),
            all_entries_under_allowed_root=True,
            selected_path_sha256=_sha256_text(str(Path(current).resolve())),
            choose_enabled=True,
        )

    if probe_id == "unicode-ui":
        ctx.page.goto(ctx.config.base_url + "/ui/home.html")
        ctx.page.locator("body").wait_for(state="visible")
        body = ctx.page.locator("body").inner_text()
        locale = os.environ.get("LANG")
        pid, _, _ = _app_pid(ctx)
        process_locale = _proc_environment(pid).get("LANG")
        assert process_locale == locale
        assert ctx.page.locator("html").get_attribute("lang") == "ja"
        assert any(word in body for word in ("資料", "会話", "ホーム", "設定"))
        return _result(
            probe_id,
            contract,
            "real Chromium Japanese document and FastAPI procfs locale",
            process_locale=process_locale,
            document_language="ja",
            japanese_text_present=True,
            body_text_sha256=_sha256_text(body),
        )

    if probe_id == "artifact-render":
        resolver = _run(
            [
                os.environ.get("PYTHON_BIN") or os.sys.executable,
                "-c",
                "from sherpa.providers.codex.sandbox import _detect_chrome_path; print(_detect_chrome_path() or '')",
            ]
        )
        assert resolver.returncode == 0 and resolver.stdout.strip(), "product Chromium resolver did not resolve an executable"
        executable = Path(resolver.stdout.strip())
        assert executable.is_file() and os.access(executable, os.X_OK)
        version = _run([str(executable), "--version"], timeout=20)
        assert version.returncode == 0 and (version.stdout or version.stderr).strip()
        rendered = _run(
            [
                str(executable),
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--dump-dom",
                "data:text/html;charset=utf-8,%3Ch1%3ESherpa%20artifact%3C%2Fh1%3E",
            ],
            timeout=30,
        )
        assert rendered.returncode == 0 and "Sherpa artifact" in rendered.stdout
        return _result(
            probe_id,
            contract,
            "product Chromium resolver and real headless HTML render process",
            executable_path_sha256=_sha256_text(str(executable.resolve())),
            executable=True,
            version_sha256=_sha256_text((version.stdout or version.stderr).strip()),
            rendered_dom_sha256=_sha256_text(rendered.stdout),
        )

    raise AssertionError(f"unhandled browser semantic probe: {probe_id}")


def _health_probe(ctx: Any, probe_id: str, contract: dict[str, Any]) -> dict[str, Any]:
    if probe_id == "compose-health":
        containers = _docker_containers()
        core = []
        for item in containers:
            labels = (item.get("Config") or {}).get("Labels") or {}
            service = labels.get("com.docker.compose.service")
            if service not in {"postgres", "neo4j", "elasticsearch"}:
                continue
            state = item.get("State") or {}
            health = (state.get("Health") or {}).get("Status")
            assert state.get("Status") == "running" and health == "healthy", f"real Compose service is not healthy: {service}"
            core.append(
                {
                    "service": service,
                    "container_id_sha256": _sha256_text(str(item.get("Id") or "")),
                    "state": state.get("Status"),
                    "health": health,
                }
            )
        assert {row["service"] for row in core} == {"postgres", "neo4j", "elasticsearch"}
        configured = float(os.environ.get("SHERPA_STORE_WAIT") or "120")
        bootstrap_logs = sorted((_profile_root(ctx) / "services").glob("bootstrap*.log"))
        bootstrap_text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in bootstrap_logs)
        assert f"{configured:g}" in bootstrap_text and "healthy" in bootstrap_text.casefold(), (
            "healthy containers alone do not prove SHERPA_STORE_WAIT; bootstrap.sh was not exercised"
        )
        return _result(
            probe_id,
            contract,
            "docker inspect State.Health for the runner-owned Compose project",
            services=sorted(core, key=lambda row: row["service"]),
            all_core_services_healthy=True,
            configured_store_wait_seconds=configured,
            bootstrap_log_sha256=_sha256_text(bootstrap_text),
        )

    if probe_id in {"health-cache-age", "request-count"}:
        first, first_ms = _admin_health(ctx, refresh=True)
        second, second_ms = _admin_health(ctx, refresh=False)
        ttl = float(os.environ.get("SHERPA_HEALTH_TTL") or "15")
        same = first.get("checked_at") == second.get("checked_at")
        if ttl > 0:
            assert same, "health cache recomputed within its configured positive TTL"
            effective_computations = 1
        else:
            assert not same, "zero health TTL unexpectedly reused a cached snapshot"
            effective_computations = 2
        return _result(
            probe_id,
            contract,
            "two real GET /admin/health requests and checked_at cache identity",
            configured_ttl_seconds=ttl,
            checked_at_reused=same,
            request_count=2,
            effective_snapshot_computations=effective_computations,
            elapsed_ms=[first_ms, second_ms],
        )

    if probe_id == "health-duration":
        health, elapsed_ms = _admin_health(ctx, refresh=True)
        timeout_seconds = float(os.environ.get("SHERPA_HEALTH_TIMEOUT") or "3")
        loaded = _run(
            [
                os.environ.get("PYTHON_BIN") or os.sys.executable,
                "-c",
                "from sherpa.health import _TIMEOUT; print(_TIMEOUT)",
            ]
        )
        assert loaded.returncode == 0 and float(loaded.stdout.strip()) == timeout_seconds
        components = health.get("components") or []
        max_latency = max(int(row.get("latency_ms") or 0) for row in components)
        permitted_ms = int(max(timeout_seconds, 0) * 1000 + 2000)
        assert max_latency <= permitted_ms
        assert elapsed_ms <= permitted_ms * max(1, len(components))
        raise AssertionError(
            "SHERPA_HEALTH_TIMEOUT reached the product constant, but no real health dependency was delayed "
            "past that deadline; a fast healthy response cannot prove timeout behavior "
            f"(configured={timeout_seconds:g}s, response={elapsed_ms}ms, max_component={max_latency}ms)"
        )

    if probe_id in {"ai-health-cache-age", "ai-health-duration"}:
        first, first_ms = _admin_health(ctx, refresh=True)
        second, second_ms = _admin_health(ctx, refresh=False)
        ai_ids = {"openai", "gemini", "bedrock", "ollama", "codex"}
        first_ai = [row for row in first.get("components") or [] if row.get("id") in ai_ids]
        second_ai = [row for row in second.get("components") or [] if row.get("id") in ai_ids]
        assert len(first_ai) == len(second_ai) == len(ai_ids)

        def canonical(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
            return [(row.get("id"), row.get("ok"), row.get("detail"), row.get("latency_ms")) for row in rows]

        timeout_seconds = float(os.environ.get("SHERPA_HEALTH_AI_TIMEOUT") or "8")
        loaded = _run(
            [
                os.environ.get("PYTHON_BIN") or os.sys.executable,
                "-c",
                "from sherpa.health import _AI_TIMEOUT; print(_AI_TIMEOUT)",
            ]
        )
        assert loaded.returncode == 0 and float(loaded.stdout.strip()) == timeout_seconds
        deadline_ms = int(max(timeout_seconds, 0) * 1000 + 6000)
        assert first_ms <= deadline_ms, "forced real AI health request exceeded the product-wide bounded deadline"
        if probe_id == "ai-health-cache-age":
            ttl = float(os.environ.get("SHERPA_HEALTH_AI_TTL") or "60")
            identical = canonical(first_ai) == canonical(second_ai)
            if ttl > 0:
                assert identical, "non-refresh AI health request did not reuse its positive-TTL snapshot"
            else:
                attempts = [second_ai]
                for _ in range(2):
                    candidate, _ = _admin_health(ctx, refresh=False)
                    attempts.append([row for row in candidate.get("components") or [] if row.get("id") in ai_ids])
                    if canonical(attempts[-1]) != canonical(first_ai):
                        break
                assert any(canonical(rows) != canonical(first_ai) for rows in attempts), (
                    "zero AI health TTL repeatedly returned an indistinguishable cached snapshot"
                )
            return _result(
                probe_id,
                contract,
                "forced then cached real /admin/health AI component snapshots",
                configured_ttl_seconds=ttl,
                ai_component_count=len(first_ai),
                component_snapshots_identical=identical,
                nonrefresh_probe_count=1 if ttl > 0 else len(attempts),
                first_elapsed_ms=first_ms,
                cached_elapsed_ms=second_ms,
            )
        assert canonical(first_ai) == canonical(second_ai), (
            "AI timeout profile did not retain the forced result in the positive default cache"
        )
        raise AssertionError(
            "SHERPA_HEALTH_AI_TIMEOUT reached the product constant, but no real AI provider was delayed "
            "past that deadline; a fast completed probe cannot prove timeout behavior "
            f"(configured={timeout_seconds:g}s, response={first_ms}ms)"
        )

    if probe_id == "startup-duration":
        service_root = _profile_root(ctx) / "services"
        health_path = service_root / "health-probe.json"
        assert health_path.is_file()
        health_rows = _load_json(health_path)
        assert isinstance(health_rows, list) and health_rows[-1].get("status") == 200
        variable = str(_scenario(ctx).get("variable") or "")
        configured = float(os.environ.get(variable) or (30 if variable == "SHERPA_APP_WAIT" else 120))
        if variable == "SHERPA_APP_WAIT":
            start_logs = sorted(service_root.glob("start*.log"))
            text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in start_logs)
            assert "Sherpa アプリを起動します" in text and f"{configured:g}秒" in text, (
                "runner launched run-api.sh directly, so SHERPA_APP_WAIT was not exercised by start.sh"
            )
            source_kind = "scripts/start.sh health wait"
        else:
            bootstrap_logs = sorted(service_root.glob("bootstrap*.log"))
            text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in bootstrap_logs)
            assert f"{configured:g}" in text and "healthy" in text.casefold(), (
                "runner used its own Compose wait, so SHERPA_STORE_WAIT was not exercised by bootstrap.sh"
            )
            source_kind = "scripts/bootstrap.sh store wait"
        return _result(
            probe_id,
            contract,
            source_kind + " and successful real health probe",
            configured_wait_seconds=configured,
            readiness_attempt_count=len(health_rows),
            final_status=200,
            script_log_sha256=_sha256_text(text),
        )

    if probe_id in {"status-command-duration", "status-output"}:
        started = time.monotonic()
        with _status_pid_evidence(ctx):
            completed = _run(["bash", "-x", str(ROOT / "scripts" / "status.sh")], timeout=45)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        output = (completed.stdout or "") + (completed.stderr or "")
        assert completed.returncode == 0
        assert "PostgreSQL" in output and "Elasticsearch" in output and "Neo4j" in output
        assert "FastAPI" in output and ctx.config.base_url in output
        assert re.search(r"\[OK\].*uvicorn.*healthz", output), "status.sh did not exercise its live PID plus healthz path"
        configured = float(os.environ.get("SHERPA_HEALTH_CURL_TIMEOUT") or "3")
        assert re.search(rf"curl .*--max-time ['\"]?{re.escape(f'{configured:g}')}", output), (
            "status.sh trace did not pass SHERPA_HEALTH_CURL_TIMEOUT to its real curl request"
        )
        assert elapsed_ms <= int((configured + 10) * 1000)
        if probe_id == "status-command-duration":
            raise AssertionError(
                "SHERPA_HEALTH_CURL_TIMEOUT was passed to curl, but the real endpoint answered before the deadline; "
                "argument propagation alone cannot prove timeout behavior"
            )
        return _result(
            probe_id,
            contract,
            "real scripts/status.sh execution against isolated containers and app",
            exit_code=completed.returncode,
            elapsed_ms=elapsed_ms,
            configured_health_timeout_seconds=configured,
            all_store_labels_present=True,
            isolated_url_present=True,
            output_sha256=_sha256_text(output),
            output_line_count=len(output.splitlines()),
        )

    raise AssertionError(f"unhandled health semantic probe: {probe_id}")


def _filesystem_probe(ctx: Any, probe_id: str, contract: dict[str, Any]) -> dict[str, Any]:
    if probe_id == "compose-config":
        config = _compose_config()
        services = config["services"]
        assert {"postgres", "neo4j", "elasticsearch"} <= set(services)
        expected_ports = _expected(ctx).get("ports") or {}
        rendered = json.dumps(config, sort_keys=True, separators=(",", ":"))
        for port in expected_ports.values():
            assert str(port) in rendered
        env_file = Path(os.environ["SHERPA_ENV_FILE"])
        return _result(
            probe_id,
            contract,
            "real docker compose config using the explicit runner env file",
            service_names=sorted(services),
            expected_port_count=len(expected_ports),
            all_expected_ports_rendered=True,
            env_file_sha256=_sha256_bytes(env_file.read_bytes()),
            config_sha256=_sha256_text(rendered),
        )

    if probe_id in {"redacted-compose-config", "volume-names", "volume-identity"}:
        config = _compose_config()
        containers = _docker_containers()
        project = os.environ["COMPOSE_PROJECT_NAME"]
        if probe_id == "redacted-compose-config":
            rendered = json.dumps(config, sort_keys=True, separators=(",", ":"))
            verified = []
            redacted_rendered = rendered
            for name in ("POSTGRES_PASSWORD", "NEO4J_PASSWORD"):
                value = os.environ.get(name, "")
                assert value and value in rendered, f"Compose did not consume {name}"
                redacted_rendered = redacted_rendered.replace(value, "<redacted>")
                assert value not in redacted_rendered
                verified.append(name)
            return _result(
                probe_id,
                contract,
                "in-memory real Compose interpolation followed by secret non-disclosure assertion",
                verified_secret_fields=verified,
                secret_values_returned=False,
                redacted_config_sha256=_sha256_text(redacted_rendered),
                config_service_count=len(config["services"]),
            )
        if probe_id == "volume-identity":
            lifecycle_path = _profile_root(ctx) / "state" / "keep-stores-lifecycle.json"
            assert lifecycle_path.is_file(), (
                "named volumes still existing before stop does not prove KEEP_STORES; "
                "a real scripts/stop.sh lifecycle observation is absent"
            )
            lifecycle = _load_json(lifecycle_path)
            requested = _bool_value(os.environ.get("KEEP_STORES"))
            assert bool(lifecycle.get("keep_stores")) is requested
            assert bool(lifecycle.get("stores_running_after_stop")) is requested
            assert lifecycle.get("project_sha256") == _sha256_text(project)
            return _result(
                probe_id,
                contract,
                "real scripts/stop.sh KEEP_STORES lifecycle evidence",
                keep_stores=requested,
                stores_running_after_stop=bool(lifecycle.get("stores_running_after_stop")),
                project_sha256=_sha256_text(project),
                lifecycle_sha256=_sha256_text(json.dumps(lifecycle, ensure_ascii=False, sort_keys=True)),
            )
        volume_rows: list[dict[str, Any]] = []
        for item in containers:
            service = ((item.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.service")
            for mount in item.get("Mounts") or []:
                if mount.get("Type") != "volume":
                    continue
                name = str(mount.get("Name") or "")
                labels = mount.get("Labels") or {}
                assert name.startswith(project + "_")
                if labels:
                    assert labels.get("com.docker.compose.project") == project
                volume_rows.append(
                    {
                        "service": service,
                        "name_sha256": _sha256_text(name),
                        "project_prefix": True,
                        "destination": mount.get("Destination"),
                    }
                )
        assert len(volume_rows) >= 3
        return _result(
            probe_id,
            contract,
            "docker inspect named-volume mounts for the runner-owned Compose project",
            project_sha256=_sha256_text(project),
            named_volume_count=len(volume_rows),
            all_names_project_scoped=True,
            volumes=sorted(volume_rows, key=lambda row: (str(row["service"]), str(row["destination"]))),
        )

    if probe_id == "compose-mount":
        config = _compose_config()
        ocr = config["services"].get("ocr-worker") or {}
        volumes = ocr.get("volumes") or []
        expected = Path(os.environ["SHERPA_OCR_WORLD_ROOT"]).resolve()
        matches = []
        for volume in volumes:
            source = volume.get("source") if isinstance(volume, dict) else None
            target = volume.get("target") if isinstance(volume, dict) else None
            if source and Path(str(source)).resolve() == expected:
                matches.append((source, target, bool(volume.get("read_only"))))
        assert len(matches) == 1 and matches[0][2] is True
        return _result(
            probe_id,
            contract,
            "real Compose OCR profile bind-mount interpolation",
            world_root_sha256=_sha256_text(str(expected)),
            matching_mount_count=1,
            target_sha256=_sha256_text(str(matches[0][1])),
            read_only=True,
        )

    if probe_id == "created-file-owner":
        containers = _docker_containers()
        ocr = [
            item
            for item in containers
            if ((item.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.service") == "ocr-worker"
        ]
        assert len(ocr) == 1, "OCR ownership profile has no real OCR container"
        configured_user = str((ocr[0].get("Config") or {}).get("User") or "")
        expected_uid = int(os.environ.get("SHERPA_OCR_UID") or os.getuid())
        expected_gid = int(os.environ.get("SHERPA_OCR_GID") or os.getgid())
        assert configured_user == f"{expected_uid}:{expected_gid}"
        observation_root = Path(os.environ["SHERPA_OBSERVATION_DIR"])
        assert_no_mount_targets(observation_root)
        files = [path for path in observation_root.rglob("*") if path.is_file()]
        assert files, "OCR worker created no observation file for ownership verification"
        assert all(path.stat().st_uid == expected_uid and path.stat().st_gid == expected_gid for path in files)
        return _result(
            probe_id,
            contract,
            "docker inspect OCR user plus host stat of real observation files",
            configured_uid=expected_uid,
            configured_gid=expected_gid,
            created_file_count=len(files),
            all_created_files_match=True,
            files_tree_sha256=_tree_summary(observation_root)["tree_sha256"],
        )

    if probe_id == "world-registry":
        kb_root = Path(os.environ["SHERPA_KB_DIR"]).resolve()
        runtime_root = Path(os.environ["RUN_DIR"]).resolve().parent
        assert kb_root == runtime_root or kb_root.is_relative_to(runtime_root)
        assert kb_root.is_dir() and not kb_root.is_symlink()
        marker = f"semantic-kb-{os.getpid()}-{time.time_ns()}"
        marker_root = kb_root / marker
        marker_file = marker_root / "evidence.txt"
        marker_root.mkdir(mode=0o700)
        write_private_text_atomic(marker_file, "real SHERPA_KB_DIR discovery evidence\n")
        try:
            options = ctx.api.get_json("/world-options")
            worlds = options.get("worlds") or []
            assert marker in worlds, "GET /world-options did not discover the configured KB root"
        finally:
            if marker_file.is_file():
                marker_file.unlink()
            if marker_root.is_dir():
                marker_root.rmdir()
        return _result(
            probe_id,
            contract,
            "physical marker under SHERPA_KB_DIR discovered by real GET /world-options",
            kb_root_sha256=_sha256_text(str(kb_root)),
            marker_id_sha256=_sha256_text(marker),
            marker_discovered=True,
            marker_removed=True,
            world_option_count=len(worlds),
        )

    if probe_id in {"derived-files", "world-root-check"}:
        world_id = _real_world(ctx)
        rows = _db_rows(
            ctx,
            "SELECT world_id,root_path,storage_mode,last_sig,last_synced_at FROM worlds WHERE world_id=%s",
            (world_id,),
        )
        assert len(rows) == 1
        registered = Path(rows[0]["root_path"]).resolve()
        expected_root = ctx.config.world_path.resolve()
        assert registered == expected_root
        if probe_id == "derived-files":
            derived = Path(os.environ["SHERPA_DERIVED_DIR"]) / world_id
            summary = _tree_summary(derived)
            assert summary["files"] > 0
            documents = _db_rows(
                ctx,
                "SELECT count(*)::int AS n FROM documents WHERE version=%s",
                (world_id,),
            )[0]["n"]
            assert documents > 0
            variable = str(_scenario(ctx).get("variable") or "")
            semantic: dict[str, Any] = {}
            admin = _settings_surfaces(ctx)["admin"]
            preview = ctx.api.get_json(f"/ingest/preview?world={quote(world_id)}")
            preview_rows = {str(row.get("name") or ""): row for row in preview.get("documents") or []}
            if variable == "SHERPA_ARMS":
                arms = admin.get("arms") or {}
                raw_arms = os.environ.get("SHERPA_ARMS")
                expected_arms = [
                    value.strip() for value in ("ooxml,pdf_text" if raw_arms is None else raw_arms).split(",") if value.strip()
                ]
                assert list(arms.get("env_default") or []) == expected_arms
                assert list(arms.get("enabled") or []) == expected_arms
                methods = {
                    str((row.get("provenance") or {}).get("method") or "") for row in preview_rows.values() if row.get("state") == "ready"
                }
                assert set(expected_arms) & methods, "configured ingestion arms produced no matching real document provenance"
                semantic = {
                    "enabled_arms": expected_arms,
                    "ready_provenance_methods": sorted(methods),
                }
            elif variable == "SHERPA_LEGACY_BACKEND":
                legacy = admin.get("legacy_backend") or {}
                expected_backend = os.environ.get("SHERPA_LEGACY_BACKEND") or "none"
                assert legacy.get("default") == expected_backend
                assert legacy.get("effective") == expected_backend
                legacy_rows = [row for name, row in preview_rows.items() if Path(name).suffix.lower() in {".doc", ".xls", ".ppt"}]
                if expected_backend != "none":
                    assert legacy_rows and any(row.get("state") == "ready" for row in legacy_rows)
                    assert any((row.get("provenance") or {}).get("legacy_backend") == expected_backend for row in legacy_rows)
                semantic = {
                    "effective_legacy_backend": expected_backend,
                    "legacy_document_count": len(legacy_rows),
                    "ready_legacy_document_count": sum(row.get("state") == "ready" for row in legacy_rows),
                }
            else:
                assert derived.resolve().is_relative_to(Path(os.environ["SHERPA_DERIVED_DIR"]).resolve())
                semantic = {"configured_derived_root_used": True}
            return _result(
                probe_id,
                contract,
                "real World ingestion, derived filesystem tree, and documents table",
                world_id_sha256=_sha256_text(world_id),
                derived_tree=summary,
                database_document_count=documents,
                semantic_effect=semantic,
            )
        if probe_id == "world-root-check":
            configured = Path(os.environ["SHERPA_OCR_WORLD_ROOT"]).resolve()
            assert expected_root == configured or expected_root.is_relative_to(configured)
            assert not configured.is_symlink()
            return _result(
                probe_id,
                contract,
                "direct worlds registry and resolved OCR containment root",
                registered_root_sha256=_sha256_text(str(registered)),
                ocr_root_sha256=_sha256_text(str(configured)),
                world_contained=True,
                symlink_free=True,
            )
    if probe_id in {"hashed-home", "hashed-codex-home"}:
        pid, _, _ = _app_pid(ctx)
        process_env = _proc_environment(pid)
        names = ["HOME"] if probe_id == "hashed-home" else ["HOME", "CODEX_HOME"]
        observations = {}
        for name in names:
            configured = Path(os.environ[name]).resolve()
            assert configured.is_dir() and not configured.is_symlink()
            assert Path(process_env[name]).resolve() == configured
            assert configured.is_relative_to(Path(os.environ["RUN_DIR"]).resolve().parent)
            observations[name] = {
                "path_sha256": _sha256_text(str(configured)),
                "mode": oct(stat.S_IMODE(configured.stat().st_mode)),
                "owner_matches": configured.stat().st_uid == os.geteuid(),
                "tree": _tree_summary(configured),
            }
        if probe_id == "hashed-codex-home":
            connection = _provider_connection(ctx, "codex")
            observations["codex_connection"] = connection
        return _result(
            probe_id,
            contract,
            "FastAPI procfs environment, runner-owned home directories, and hashed metadata",
            directories=observations,
            raw_paths_returned=False,
        )

    if probe_id == "model-files-sha256":
        root = Path(os.environ["SHERPA_OCR_MODEL_CACHE"]).resolve()
        summary = _tree_summary(root)
        assert summary["files"] > 0, "real OCR model cache is empty"
        initial_path = _profile_root(ctx) / "state" / "ocr-cache-copy-integrity-initial.json"
        assert initial_path.is_file(), "runner OCR copy-integrity evidence is absent"
        initial = _load_json(initial_path)
        initial_rows = initial.get("runtime") or []
        actual_rows = []
        assert_no_mount_targets(root)
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                raw = path.read_bytes()
                actual_rows.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "sha256": _sha256_bytes(raw),
                        "size": len(raw),
                    }
                )
        assert actual_rows == initial_rows, "runtime OCR model files differ from the runner's initial per-file SHA-256 ledger"
        if initial.get("source_configured"):
            assert initial.get("copy_match") is True
        return _result(
            probe_id,
            contract,
            "real OCR model-cache file hashing and runner copy-integrity evidence",
            model_cache_path_sha256=_sha256_text(str(root)),
            model_cache=summary,
            nonempty=True,
            initial_ledger_match=True,
            initial_ledger_sha256=_sha256_text(json.dumps(initial_rows, ensure_ascii=False, sort_keys=True)),
            explicit_source_configured=bool(initial.get("source_configured")),
        )

    if probe_id == "ollama-model-path":
        variable = str(_scenario(ctx).get("variable") or "")
        configured = Path(os.environ[variable]).resolve()
        assert configured.is_dir() and not configured.is_symlink()
        command = shutil.which("ollama", path=os.environ.get("PATH"))
        assert command, "real Ollama CLI is unavailable"
        environment = dict(os.environ)
        environment[variable] = str(configured)
        listed = _run([command, "list"], timeout=30, environment=environment)
        assert listed.returncode == 0, "real Ollama model listing failed"
        summary = _tree_summary(configured)
        assert summary["files"] > 0, "configured Ollama model directory is empty"
        return _result(
            probe_id,
            contract,
            "real Ollama CLI model listing and configured model filesystem",
            variable=variable,
            path_sha256=_sha256_text(str(configured)),
            model_tree=summary,
            list_output_sha256=_sha256_text(listed.stdout),
            list_line_count=len(listed.stdout.splitlines()),
        )

    if probe_id == "redacted-effective-environment":
        variable = str(_scenario(ctx).get("variable") or "")
        assert variable in _SECRET_VARIABLES
        pid, _, _ = _app_pid(ctx)
        process_env = _proc_environment(pid)
        actual = process_env.get(variable)
        expected_file = _load_json(Path(ctx.config.expected_env_path))
        serialized = json.dumps(expected_file, ensure_ascii=False, sort_keys=True)
        if actual:
            assert actual not in serialized, f"{variable} leaked into effective environment evidence"
        effective = expected_file.get("effective_environment") or {}
        assert variable in effective
        assert effective[variable] in {"<redacted>", "set"}, f"{variable} was not redacted in runner evidence"
        return _result(
            probe_id,
            contract,
            "FastAPI procfs presence correlated with redacted runner environment JSON",
            variable=variable,
            process_present=actual is not None,
            evidence_marker=effective[variable],
            plaintext_match_count=0,
            secret_value_returned=False,
        )

    if probe_id == "restart-effective-environment":
        transition = _scenario(ctx).get("restart_transition") or {}
        path = _profile_root(ctx) / "state" / "restart-effective-environment.json"
        payload = _load_json(path)
        assert payload.get("transition_id") == transition.get("id")
        expected_keys = sorted(str(key) for key in transition.get("changed_keys") or ())
        assert sorted(payload.get("changed_keys") or ()) == expected_keys
        admin = _settings_surfaces(ctx)["admin"]
        observable = transition.get("observable") or {}
        if observable.get("kind") == "admin_ollama_url_seed":
            actual_url = (admin.get("cloud") or {}).get("ollama_url")
            precedence_path = _profile_root(ctx) / "state" / "environment-seed-precedence.json"
            expected_url = observable.get("ui_override") if precedence_path.is_file() else observable.get("fresh")
            assert actual_url == expected_url
            assert actual_url != observable.get("transition_env"), (
                "same-database restart incorrectly replaced the persisted seed from environment"
            )
            actual_hash = _sha256_text(str(actual_url))
        else:
            assert observable, "restart transition has no semantic observable"
            actual_hash = None
        return _result(
            probe_id,
            contract,
            "runner restart environment record and post-restart admin settings API",
            transition_id=str(payload.get("transition_id")),
            changed_keys=expected_keys,
            effective_environment_redacted=True,
            observed_value_sha256=actual_hash,
            environment_change_did_not_overwrite_persisted_value=True,
        )

    if probe_id == "write-boundary":
        run_dir = Path(os.environ["RUN_DIR"]).resolve()
        home = Path(os.environ["HOME"]).resolve()
        world = ctx.config.world_path.resolve()
        assert all(path.is_dir() and not path.is_symlink() for path in (run_dir, home, world))
        assert run_dir.is_relative_to(home.parent)
        probe_file = run_dir / f"semantic-write-{os.getpid()}.txt"
        assert not probe_file.exists()
        try:
            write_private_text_atomic(probe_file, "runner-owned write boundary\n")
            assert probe_file.stat().st_uid == os.geteuid()
            created_sha = _sha256_bytes(probe_file.read_bytes())
        finally:
            if probe_file.exists():
                probe_file.unlink()
        world_mode = stat.S_IMODE(world.stat().st_mode)
        assert world_mode & stat.S_IWUSR == 0, "copied source World is unexpectedly user-writable"
        return _result(
            probe_id,
            contract,
            "actual create/stat/delete in RUN_DIR plus source World permission boundary",
            run_dir_path_sha256=_sha256_text(str(run_dir)),
            home_path_sha256=_sha256_text(str(home)),
            write_probe_sha256=created_sha,
            write_probe_removed=True,
            world_path_sha256=_sha256_text(str(world)),
            world_user_writable=False,
            world_mode=oct(world_mode),
        )

    if probe_id == "artifact-file":
        resolver = _run(
            [
                os.environ.get("PYTHON_BIN") or os.sys.executable,
                "-c",
                "from sherpa.providers.codex.sandbox import _marp_bin; print(_marp_bin() or '')",
            ]
        )
        assert resolver.returncode == 0 and resolver.stdout.strip(), "product Marp resolver found no executable"
        marp = Path(resolver.stdout.strip())
        assert marp.is_file() and os.access(marp, os.X_OK)
        version = _run([str(marp), "--version"], timeout=20)
        assert version.returncode == 0
        output_root = Path(os.environ["RUN_DIR"]) / "semantic-artifact"
        output_root.mkdir(mode=0o700, exist_ok=True)
        source = output_root / "evidence.md"
        output = output_root / "evidence.html"
        write_private_text_atomic(source, "---\nmarp: true\n---\n# Sherpa UI Automation\n")
        try:
            rendered = _run([str(marp), str(source), "--html", "-o", str(output)], timeout=60)
            assert rendered.returncode == 0 and output.is_file() and output.stat().st_size > 0
            output_summary = _safe_file_summary(output)
        finally:
            for path in (output, source):
                if path.exists():
                    path.unlink()
            output_root.rmdir()
        return _result(
            probe_id,
            contract,
            "product Marp resolver and real CLI HTML artifact generation",
            marp_path_sha256=_sha256_text(str(marp.resolve())),
            version_sha256=_sha256_text((version.stdout or version.stderr).strip()),
            artifact=output_summary,
            temporary_artifact_removed=True,
        )

    raise AssertionError(f"unhandled filesystem semantic probe: {probe_id}")


def _log_probe(ctx: Any, probe_id: str, contract: dict[str, Any]) -> dict[str, Any]:
    paths = _service_logs()
    assert paths, "runner captured no nonempty real service logs"
    if probe_id == "preflight-log":
        path = _profile_root(ctx) / "services" / "port-check.log"
        summary = _safe_file_summary(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        configured = _bool_value(os.environ.get("SHERPA_SKIP_PORT_CHECK"))
        if configured:
            assert re.search(r"skip|省略|回避", text, re.IGNORECASE)
        else:
            assert all(name in text for name in ("PostgreSQL", "Elasticsearch", "Neo4j", "FastAPI"))
        return _result(
            probe_id,
            contract,
            "real scripts/check-ports.sh captured output",
            skip_requested=configured,
            log=summary,
            all_service_rows_present=not configured,
        )

    if probe_id == "proxy-log":
        app_logs = [path for path in paths if path.name.startswith("app")]
        before, _ = _combined_log_tail(app_logs)
        response = ctx.api.request("GET", "/healthz", expected=200)
        deadline = time.monotonic() + 3
        after = before
        while time.monotonic() < deadline:
            after, _ = _combined_log_tail(app_logs)
            if after.count(b"GET /healthz") > before.count(b"GET /healthz"):
                break
            time.sleep(0.05)
        assert after.count(b"GET /healthz") > before.count(b"GET /healthz")
        configured = os.environ.get(str(_scenario(ctx).get("variable") or ""), "")
        assert "127.0.0.1" in configured or "localhost" in configured or configured == ""
        return _result(
            probe_id,
            contract,
            "real local HTTP request correlated with FastAPI access log under NO_PROXY",
            status=response.status,
            configured_bypass_entry_count=len([part for part in configured.split(",") if part]),
            direct_app_log_request_delta=after.count(b"GET /healthz") - before.count(b"GET /healthz"),
            log_tail_sha256=_sha256_bytes(after),
        )

    if probe_id == "ocr-worker-log":
        containers = _docker_containers()
        ocr = [
            item
            for item in containers
            if ((item.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.service") == "ocr-worker"
        ]
        assert len(ocr) == 1, "OCR log probe has no real OCR container"
        container_id = str(ocr[0].get("Id") or "")
        completed = _run(["docker", "logs", container_id], timeout=30)
        output = (completed.stdout or "") + (completed.stderr or "")
        assert completed.returncode == 0 and output.strip()
        model_root = Path(os.environ["SHERPA_OCR_MODEL_CACHE"])
        assert _tree_summary(model_root)["files"] > 0
        model_mounts = [mount for mount in ocr[0].get("Mounts") or [] if mount.get("Destination") == "/models"]
        assert len(model_mounts) == 1
        assert Path(str(model_mounts[0].get("Source") or "")).resolve() == model_root.resolve()
        assert model_mounts[0].get("RW") is False
        return _result(
            probe_id,
            contract,
            "real docker logs for OCR worker and nonempty model cache",
            container_id_sha256=_sha256_text(container_id),
            log_bytes=len(output.encode()),
            log_sha256=_sha256_text(output),
            log_line_count=len(output.splitlines()),
            model_cache_nonempty=True,
            model_cache_mount_matches=True,
            model_cache_read_only=True,
        )

    if probe_id == "author-error":
        raw_timeout = os.environ.get("SHERPA_CODEX_TIMEOUT_AUTHOR")
        try:
            timeout_seconds = float(raw_timeout) if raw_timeout not in {None, ""} else 600.0
        except ValueError:
            timeout_seconds = -1.0
        if timeout_seconds <= 0:
            turn = _exercise_author_turn(ctx, expect_error=True)
            expected_error = True
        else:
            turn = _exercise_author_turn(ctx, expect_error=False)
            expected_error = False
        combined, names = _combined_log_tail(paths)
        text = combined.decode("utf-8", errors="replace")
        matches = len(re.findall(r"author|timeout|timed out|タイムアウト", text, re.IGNORECASE))
        if expected_error:
            assert matches > 0
        return _result(
            probe_id,
            contract,
            "real Codex author turn selected by the configured author timeout and application log",
            turn=turn,
            configured_timeout_seconds=timeout_seconds,
            expected_error=expected_error,
            matching_error_line_count=matches,
            log_files=names,
            log_sha256=_sha256_bytes(combined),
        )

    if probe_id in {"ingest-error", "ingest-log"}:
        world_id = _real_world(ctx)
        runs = _db_rows(
            ctx,
            "SELECT id,status,extraction_snapshot,published_snapshot FROM ingest_runs WHERE version=%s ORDER BY id DESC",
            (world_id,),
        )
        assert runs, "real World ingestion created no ingest_runs row"
        combined, names = _combined_log_tail(paths)
        if probe_id == "ingest-error":
            variable = str(_scenario(ctx).get("variable") or "")
            if variable == "SHERPA_VLM_TIMEOUT":
                expression = "from sherpa.ingest.arms.vision_arm import _vlm_timeout_sec; print(_vlm_timeout_sec())"
                default_timeout = 180.0
            else:
                expression = "from sherpa.ingest.arms.legacy_convert import _timeout_sec; print(_timeout_sec())"
                default_timeout = 60.0
            completed = _run([os.environ.get("PYTHON_BIN") or os.sys.executable, "-c", expression])
            assert completed.returncode == 0
            effective_timeout = float(completed.stdout.strip())
            raw_timeout = os.environ.get(variable)
            try:
                requested_timeout = float(raw_timeout) if raw_timeout not in {None, ""} else default_timeout
            except ValueError:
                requested_timeout = default_timeout
            expected_timeout = requested_timeout if requested_timeout > 0 else default_timeout
            assert effective_timeout == expected_timeout
            failed = [row for row in runs if row.get("status") == "failed"]
            warning_payloads = [
                row
                for row in runs
                if any(
                    isinstance(flag, dict) and flag.get("action") in {"warn", "blocked"}
                    for flag in ((row.get("extraction_snapshot") or {}).get("flags") or [])
                )
            ]
            if effective_timeout > 0:
                assert runs[0].get("status") == "succeeded", "positive/fallback ingestion timeout did not complete the real World ingest"
            return _result(
                probe_id,
                contract,
                "real World ingest_runs failure/warning rows and captured application log",
                world_id_sha256=_sha256_text(world_id),
                ingest_run_count=len(runs),
                failed_run_count=len(failed),
                warned_run_count=len(warning_payloads),
                configured_variable=variable,
                requested_timeout_seconds=requested_timeout,
                effective_timeout_seconds=effective_timeout,
                fallback_to_safe_default=requested_timeout <= 0,
                log_files=names,
                log_sha256=_sha256_bytes(combined),
            )
        documents = _db_rows(
            ctx,
            "SELECT count(*)::int AS n FROM documents WHERE version=%s",
            (world_id,),
        )[0]["n"]
        assert documents > 0
        assert b"POST /worlds" in combined or b"ingest" in combined.casefold()
        variable = str(_scenario(ctx).get("variable") or "")
        mapping = _elasticsearch_mapping(ctx, world_id)
        meta = mapping["meta"]
        properties = mapping["properties"]
        if variable == "SHERPA_DISABLE_EMBED":
            disabled = bool(os.environ.get(variable))
            assert ("embedding" not in properties) is disabled
            semantic = {
                "disabled_by_presence": disabled,
                "embedding_mapping_present": "embedding" in properties,
            }
        elif variable == "OPENAI_EMBED_MODEL":
            expected_model = os.environ.get(variable) or "text-embedding-3-small"
            assert meta.get("embed_model") == expected_model
            assert "embedding" in properties
            semantic = {
                "embed_model_sha256": _sha256_text(expected_model),
                "mapping_model_exact_match": True,
                "embedding_mapping_present": True,
            }
        elif variable == "ES_MAPPING_VERSION":
            assert str(meta.get("mapping_version")) == "3"
            if os.environ.get(variable) is not None:
                raise AssertionError(
                    "ES_MAPPING_VERSION is a fixed product constant; the runtime environment value did not control the real index mapping"
                )
            semantic = {"fixed_mapping_version": str(meta.get("mapping_version"))}
        else:
            raise AssertionError(f"ingest-log has no variable-specific adapter for {variable}")
        return _result(
            probe_id,
            contract,
            "real World ingestion database rows and captured FastAPI/worker logs",
            world_id_sha256=_sha256_text(world_id),
            ingest_run_count=len(runs),
            document_count=documents,
            latest_status=runs[0]["status"],
            semantic_effect=semantic,
            mapping_elapsed_ms=mapping["elapsed_ms"],
            log_files=names,
            log_sha256=_sha256_bytes(combined),
        )

    if probe_id == "app-log":
        app_paths = [path for path in paths if path.name.startswith("app")]
        combined, names = _combined_log_tail(app_paths)
        text = combined.decode("utf-8", errors="replace")
        assert "Uvicorn running" in text and "Application startup complete" in text
        variable = str(_scenario(ctx).get("variable") or "")
        pid, _, cmdline = _app_pid(ctx)
        process_env = _proc_environment(pid)
        assert process_env.get(variable) == os.environ.get(variable)
        semantic: dict[str, Any]
        if variable == "SHERPA_ASGI_APP":
            effective = str(os.environ.get(variable) or "sherpa.api:app")
            assert effective in cmdline
            semantic = {"effective_asgi_app_sha256": _sha256_text(effective)}
        elif variable == "SHERPA_UVICORN_WORKERS":
            effective = os.environ.get(variable) or "1"
            assert f"--workers {effective}" in cmdline
            semantic = {"effective_workers": int(effective)}
        elif variable == "SHERPA_REQUIRE_ENV_FILE":
            assert Path(os.environ["SHERPA_ENV_FILE"]).is_file()
            semantic = {
                "required": _bool_value(os.environ.get(variable)),
                "explicit_env_file_consumed": True,
                "env_file_sha256": _sha256_bytes(Path(os.environ["SHERPA_ENV_FILE"]).read_bytes()),
            }
        elif variable in {
            "SHERPA_AGENTIC_MAX_TOOL_RESULT_BYTES",
            "SHERPA_AGENTIC_MAX_TOTAL_TOOL_RESULT_BYTES",
            "SHERPA_GREP_FILE_CAP_BYTES",
        }:
            target = {
                "SHERPA_AGENTIC_MAX_TOOL_RESULT_BYTES": (
                    "sherpa.agentic_search",
                    "TOOL_RESULT_MAX_BYTES",
                ),
                "SHERPA_AGENTIC_MAX_TOTAL_TOOL_RESULT_BYTES": (
                    "sherpa.agentic_search",
                    "TOOL_RESULT_MAX_TOTAL_BYTES",
                ),
                "SHERPA_GREP_FILE_CAP_BYTES": ("sherpa.grep_tool", "_GREP_FILE_CAP_BYTES"),
            }[variable]
            completed = _run(
                [
                    os.environ.get("PYTHON_BIN") or os.sys.executable,
                    "-c",
                    f"from {target[0]} import {target[1]}; print({target[1]})",
                ]
            )
            assert completed.returncode == 0 and completed.stdout.strip().isdigit()
            effective_limit = int(completed.stdout.strip())
            raw = os.environ.get(variable)
            if raw is not None and raw.isdigit():
                requested = int(raw)
                if variable == "SHERPA_AGENTIC_MAX_TOOL_RESULT_BYTES":
                    accepted = 1024 <= requested <= 8 * 1024 * 1024
                    default = 65536
                elif variable == "SHERPA_AGENTIC_MAX_TOTAL_TOOL_RESULT_BYTES":
                    accepted = 4096 <= requested <= 64 * 1024 * 1024
                    default = 1048576
                else:
                    accepted = 65536 <= requested <= 64 * 1024 * 1024
                    default = 8 * 1024 * 1024
                assert effective_limit == (requested if accepted else default)
            semantic = {
                "effective_limit_bytes": effective_limit,
                "product_constant_process_exit": completed.returncode,
            }
        elif variable == "WSL_DISTRO_NAME":
            completed = _run(
                [
                    os.environ.get("PYTHON_BIN") or os.sys.executable,
                    "-c",
                    "from sherpa.ingest.arms.legacy_convert import wsl_to_windows_path; "
                    "print(wsl_to_windows_path('/srv/sherpa/evidence.doc') or '')",
                ]
            )
            converted = completed.stdout.strip()
            distro = os.environ.get(variable) or ""
            assert completed.returncode == 0
            if distro:
                assert converted.startswith(f"\\\\wsl.localhost\\{distro}\\")
            else:
                assert not converted
            semantic = {
                "configured_distro_sha256": _sha256_text(distro) if distro else None,
                "native_path_conversion_available": bool(converted),
                "converted_path_sha256": _sha256_text(converted) if converted else None,
            }
        elif variable == "CODEX_BIN_REAL":
            configured = os.environ.get(variable) or ""
            resolved = shutil.which("codex", path=os.environ.get("PATH")) or ""
            assert not configured or Path(configured).resolve() == Path(resolved).resolve(), (
                "CODEX_BIN_REAL is install-only and is not consumed by the runtime Codex resolver"
            )
            semantic = {
                "configured_path_sha256": _sha256_text(configured) if configured else None,
                "runtime_codex_path_sha256": _sha256_text(resolved) if resolved else None,
                "paths_match": not configured or Path(configured).resolve() == Path(resolved).resolve(),
            }
        else:
            raise AssertionError(f"app-log has no variable-specific system adapter for {variable}")
        return _result(
            probe_id,
            contract,
            "real FastAPI startup log plus procfs command/environment correlation",
            log_files=names,
            log_bytes=len(combined),
            log_sha256=_sha256_bytes(combined),
            startup_complete=True,
            variable=variable,
            process_value_matches=True,
            command_line_sha256=_sha256_text(cmdline),
            semantic_effect=semantic,
        )

    raise AssertionError(f"unhandled log semantic probe: {probe_id}")


def _exercise_stop_wait(ctx: Any) -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        configured = int(float(os.environ.get("SHERPA_STOP_WAIT") or "10"))
        assert configured >= 0
        run_dir = Path(os.environ["RUN_DIR"]).resolve()
        marker = f"sherpa-stop-semantic-{os.getpid()}-{time.time_ns()}"
        pid_path = run_dir / f"{marker}.pid"
        caddy_path = run_dir / f"{marker}.caddy.pid"
        child = subprocess.Popen(
            [
                os.environ.get("PYTHON_BIN") or os.sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)",
                marker,
            ],
            cwd=ROOT,
            env=dict(os.environ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # stop.sh expects this child to lead its own process group. Keep it
            # in the surrounding pytest session so runner cleanup can still
            # enumerate and reap it if this test is interrupted.
            process_group=0,
        )
        write_private_text_atomic(pid_path, f"{child.pid}\n")
        environment = dict(os.environ)
        environment.update(
            {
                "APP_PID_FILE": str(pid_path),
                "APP_PROC_NEEDLE": marker,
                "CADDY_PID_FILE": str(caddy_path),
                "CADDY_PROC_NEEDLE": marker + "-caddy",
                "KEEP_STORES": "1",
                "SHERPA_STOP_WAIT": str(configured),
            }
        )
        try:
            started = time.monotonic()
            completed = _run(
                [str(ROOT / "scripts" / "stop.sh")],
                timeout=configured + 30,
                environment=environment,
            )
            elapsed = time.monotonic() - started
            child.wait(timeout=5)
            assert completed.returncode == 0
            assert child.returncode is not None and child.returncode < 0
            assert elapsed >= max(0.0, configured - 1.5)
            assert elapsed <= configured + 5
            output = (completed.stdout or "") + (completed.stderr or "")
            assert "SIGKILL" in output
            assert not pid_path.exists()
            app_health_unchanged = ctx.api.get_json("/healthz").get("ok") is True
            assert app_health_unchanged
            return {
                "configured_stop_wait_seconds": configured,
                "observed_stop_seconds": elapsed,
                "owned_child_forced_exit": True,
                "child_pid_sha256": _sha256_text(str(child.pid)),
                "stop_output_sha256": _sha256_text(output),
                "pid_file_removed": True,
                "stores_preserved": True,
                "app_health_unchanged": app_health_unchanged,
            }
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if pid_path.is_file():
                pid_path.unlink()
            if caddy_path.is_file():
                caddy_path.unlink()

    return _cache(ctx, "stop-wait-lifecycle", collect)


def _process_probe(ctx: Any, probe_id: str, contract: dict[str, Any]) -> dict[str, Any]:
    if probe_id in {"codex-login-status", "codex-version", "command-resolution"}:
        codex = shutil.which("codex", path=os.environ.get("PATH"))
        assert codex, "Codex CLI is absent from the generated PATH"
        version = _run([codex, "--version"], timeout=20)
        assert version.returncode == 0 and (version.stdout or version.stderr).strip()
        if probe_id == "codex-login-status":
            connection = _provider_connection(ctx, "codex")
            home = Path(os.environ["CODEX_HOME"])
            auth = home / "auth.json"
            assert auth.is_file() and not auth.is_symlink()
            assert stat.S_IMODE(auth.stat().st_mode) & 0o077 == 0
            return _result(
                probe_id,
                contract,
                "product /settings/test Codex login check and CODEX_HOME auth metadata",
                connection=connection,
                codex_home_sha256=_sha256_text(str(home.resolve())),
                auth_file_present=True,
                auth_file_mode=oct(stat.S_IMODE(auth.stat().st_mode)),
                auth_content_returned=False,
            )
        variable = str(_scenario(ctx).get("variable") or "")
        if probe_id == "codex-version" and variable == "CODEX_BIN_REAL":
            configured = os.environ.get(variable) or ""
            assert not configured or Path(configured).resolve() == Path(codex).resolve(), (
                "CODEX_BIN_REAL is not consumed by the runtime command resolver"
            )
        resolved = {name: shutil.which(name, path=os.environ.get("PATH")) for name in ("codex", "docker", "curl", "bash")}
        assert resolved["codex"] == codex and all(resolved.values())
        return _result(
            probe_id,
            contract,
            "real executable lookup and version process under generated PATH",
            resolved={name: _sha256_text(str(path)) for name, path in resolved.items()},
            all_required_commands_resolved=True,
            codex_version_sha256=_sha256_text((version.stdout or version.stderr).strip()),
            scenario_variable=variable,
        )

    if probe_id in {"codex-invocation-summary", "codex-tls-probe"}:
        turn = _exercise_codex_turn(ctx)
        pid, _, _ = _app_pid(ctx)
        process_env = _proc_environment(pid)
        variable = str(_scenario(ctx).get("variable") or "")
        assert process_env.get(variable) == os.environ.get(variable)
        if probe_id == "codex-tls-probe":
            configured = Path(os.environ[variable]).resolve()
            assert configured.exists() and not configured.is_symlink()
        authoring = Path(os.environ["SHERPA_USERS_DIR"])
        summary = _execution_tree_summary(authoring)
        assert summary["files"] > 0, "real Codex turn produced no user-area execution files"
        sandbox_enabled = _bool_value(os.environ.get("SHERPA_CODEX_SANDBOX"), default=True)
        assert_no_mount_targets(authoring)
        configs = sorted(authoring.rglob("config.toml"))
        if probe_id == "codex-invocation-summary" and sandbox_enabled:
            assert configs, "sandboxed real Codex turn produced no permission-profile config"
            config_texts = [path.read_text(encoding="utf-8", errors="replace") for path in configs]
            assert any(
                'default_permissions = "sherpa-authoring"' in value
                and '":root" = "deny"' in value
                and '"." = "write"' in value
                and "enabled = false" in value
                for value in config_texts
            ), "Codex invocation did not use the configured permission-profile sandbox"
            config_hashes = [_sha256_text(value) for value in config_texts]
        else:
            config_hashes = []
        return _result(
            probe_id,
            contract,
            "real Codex chat turn, nonzero usage, tool node, and user-area execution files",
            turn=turn,
            variable=variable,
            app_process_value_matches=True,
            user_area_tree=summary,
            sandbox_enabled=sandbox_enabled,
            permission_profile_config_count=len(configs),
            permission_profile_config_sha256=config_hashes,
        )

    if probe_id == "ollama-listen-address":
        raw = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
        process_rows = []
        for proc_root in Path("/proc").iterdir():
            if not proc_root.name.isdigit():
                continue
            try:
                if proc_root.stat().st_uid != os.geteuid():
                    continue
                cmdline = (proc_root / "cmdline").read_bytes().replace(b"\0", b" ")
                if b"ollama" not in cmdline or b"serve" not in cmdline:
                    continue
                proc_env = _proc_environment(int(proc_root.name))
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            process_rows.append(
                {
                    "pid_sha256": _sha256_text(proc_root.name),
                    "configured_host_sha256": _sha256_text(proc_env.get("OLLAMA_HOST", "")),
                    "host_exact_match": proc_env.get("OLLAMA_HOST", "127.0.0.1:11434") == raw,
                }
            )
        assert len(process_rows) == 1 and process_rows[0]["host_exact_match"], (
            "OLLAMA_HOST was not applied to the real ollama serve process"
        )
        parsed = urlsplit(raw if "://" in raw else "http://" + raw)
        host = parsed.hostname or "127.0.0.1"
        connect_host = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host
        port = parsed.port or 11434
        with socket.create_connection((connect_host, port), timeout=5):
            pass
        request = urllib.request.Request(
            f"http://{connect_host}:{port}/api/tags",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
        assert response.status == 200 and isinstance(payload.get("models"), list)
        return _result(
            probe_id,
            contract,
            "real TCP connect and Ollama /api/tags request",
            configured_host_sha256=_sha256_text(raw),
            connect_host=connect_host,
            port=port,
            status=response.status,
            model_count=len(payload["models"]),
            server_process=process_rows[0],
        )

    if probe_id in {"port-owner-check", "process-count", "process-exit", "preflight-exit"}:
        pid, pid_file, cmdline = _app_pid(ctx)
        assert f"--port {urlsplit(ctx.config.base_url).port}" in cmdline
        if probe_id == "port-owner-check":
            needle = os.environ.get("APP_PROC_NEEDLE") or "uvicorn"
            assert needle and needle in cmdline
            return _result(
                probe_id,
                contract,
                "runner PID file and live procfs command line owning the isolated app port",
                pid_sha256=_sha256_text(str(pid)),
                pid_file_sha256=_sha256_text(str(pid_file.resolve())),
                pid_file_present=pid_file.is_file(),
                needle_sha256=_sha256_text(needle),
                command_matches=True,
                isolated_port=int(urlsplit(ctx.config.base_url).port or 0),
            )
        if probe_id == "process-count":
            expected_workers = int(os.environ.get("SHERPA_UVICORN_WORKERS") or "1")
            child_file = Path("/proc") / str(pid) / "task" / str(pid) / "children"
            child_ids = [int(value) for value in child_file.read_text().split()] if child_file.is_file() else []
            worker_pids = []
            for child in child_ids:
                child_cmd = Path("/proc") / str(child) / "cmdline"
                if child_cmd.is_file() and b"uvicorn" in child_cmd.read_bytes():
                    worker_pids.append(child)
            effective_count = len(worker_pids) if expected_workers > 1 else 1
            assert effective_count == expected_workers
            return _result(
                probe_id,
                contract,
                "live uvicorn procfs parent/worker topology",
                configured_workers=expected_workers,
                observed_workers=effective_count,
                master_pid_sha256=_sha256_text(str(pid)),
                worker_pid_sha256=sorted(_sha256_text(str(value)) for value in worker_pids),
            )
        port_check = _profile_root(ctx) / "services" / "port-check.log"
        assert port_check.is_file()
        health = ctx.api.get_json("/healthz")
        assert health.get("ok") is True
        variable = str(_scenario(ctx).get("variable") or "")
        log_text = port_check.read_text(encoding="utf-8", errors="replace")
        if variable == "SHERPA_SKIP_PORT_CHECK":
            skipped = _bool_value(os.environ.get(variable))
            if skipped:
                assert re.search(r"skip|省略|回避", log_text, re.IGNORECASE)
            else:
                assert all(value in log_text for value in ("PostgreSQL", "Elasticsearch", "Neo4j", "FastAPI"))
            semantic = {"skip_requested": skipped, "preflight_branch_observed": True}
        elif variable == "SHERPA_APP_WAIT":
            raise AssertionError("process-exit cannot prove SHERPA_APP_WAIT because the runner bypassed scripts/start.sh")
        elif variable == "SHERPA_STOP_WAIT":
            semantic = _exercise_stop_wait(ctx)
        elif variable == "SHERPA_REQUIRE_ENV_FILE":
            required = _bool_value(os.environ.get(variable))
            assert not required or Path(os.environ["SHERPA_ENV_FILE"]).is_file()
            semantic = {"required": required, "real_run_api_survived_env_file_gate": True}
        else:
            raise AssertionError(f"process-exit has no variable-specific adapter for {variable}")
        return _result(
            probe_id,
            contract,
            "successful real preflight log, live FastAPI process, and /healthz",
            process_alive=True,
            pid_sha256=_sha256_text(str(pid)),
            preflight_log=_safe_file_summary(port_check),
            health_ok=True,
            command_line_sha256=_sha256_text(cmdline),
            semantic_effect=semantic,
        )

    if probe_id == "shutdown-duration":
        observation = _exercise_stop_wait(ctx)
        return _result(
            probe_id,
            contract,
            "real scripts/stop.sh termination lifecycle against an owned sacrificial process",
            **observation,
        )

    if probe_id == "worker-version":
        resolver = _run(
            [
                os.environ.get("PYTHON_BIN") or os.sys.executable,
                "-c",
                "from sherpa.ingest.arms.legacy_convert import _powershell_bin; print(_powershell_bin() or '')",
            ]
        )
        assert resolver.returncode == 0
        executable = resolver.stdout.strip()
        assert executable and Path(executable).is_file() and os.access(executable, os.X_OK)
        completed = _run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$PSVersionTable.PSVersion.ToString()",
            ],
            timeout=30,
        )
        output = (completed.stdout or completed.stderr).strip()
        assert completed.returncode == 0 and re.search(r"\d+\.\d+", output)
        return _result(
            probe_id,
            contract,
            "real configured PowerShell worker executable version process",
            executable_path_sha256=_sha256_text(str(Path(executable).resolve())),
            exit_code=completed.returncode,
            version_sha256=_sha256_text(output),
        )

    raise AssertionError(f"unhandled process semantic probe: {probe_id}")


def _settings_probe(ctx: Any, probe_id: str, contract: dict[str, Any]) -> dict[str, Any]:
    surfaces = _settings_surfaces(ctx)
    settings = surfaces["settings"]
    admin = surfaces["admin"]
    variable = str(_scenario(ctx).get("variable") or "")

    if probe_id in {"legacy-backend-selection", "legacy-status"}:
        if probe_id == "legacy-backend-selection" and variable == "WSL_DISTRO_NAME":
            completed = _run(
                [
                    os.environ.get("PYTHON_BIN") or os.sys.executable,
                    "-c",
                    "from sherpa.ingest.arms.legacy_convert import wsl_to_windows_path; "
                    "print(wsl_to_windows_path('/srv/sherpa/evidence.doc') or '')",
                ]
            )
            assert completed.returncode == 0
            converted = completed.stdout.strip()
            distro = os.environ.get(variable) or ""
            if distro:
                assert converted.startswith(f"\\\\wsl.localhost\\{distro}\\")
            else:
                assert not converted
            return _result(
                probe_id,
                contract,
                "real product WSL-to-Windows path resolver process",
                distro_present=bool(distro),
                distro_sha256=_sha256_text(distro) if distro else None,
                conversion_available=bool(converted),
                converted_path_sha256=_sha256_text(converted) if converted else None,
            )
        legacy = admin.get("legacy_backend") or {}
        assert legacy.get("effective") in set(legacy.get("options") or ())
        ctx.page.goto(ctx.config.base_url + "/ui/admin-settings.html")
        ctx.page.locator("#legacy-block").wait_for(state="visible")
        selected = ctx.page.locator("#legacy-radios input:checked").get_attribute("value")
        assert selected == legacy.get("effective")
        status_text = ctx.page.locator("#legacy-status").inner_text().strip()
        assert status_text
        expected_backend = os.environ.get("SHERPA_LEGACY_BACKEND") or "none"
        assert legacy.get("default") == expected_backend
        assert legacy.get("effective") == expected_backend
        return _result(
            probe_id,
            contract,
            "GET /admin/settings legacy resolution correlated with visible selected UI option",
            configured=legacy.get("configured"),
            effective=legacy.get("effective"),
            default=legacy.get("default"),
            option_count=len(legacy.get("options") or []),
            ui_selected=selected,
            ui_status_sha256=_sha256_text(status_text),
        )

    if probe_id in {"observation-model", "observation-provider"}:
        vlm = admin.get("vlm") or {}
        effective = vlm.get("effective") or {}
        assert effective.get("provider") in set(vlm.get("providers") or ())
        assert str(effective.get("model") or "")
        ctx.page.goto(ctx.config.base_url + "/ui/admin-settings.html")
        ctx.page.locator("#vlm-block").wait_for(state="visible")
        ui_provider = ctx.page.locator("#vlm-provider").input_value()
        ui_model = ctx.page.locator("#vlm-model").input_value()
        assert ui_provider == effective["provider"] and ui_model == effective["model"]
        key = "model" if probe_id == "observation-model" else "provider"
        defaults = vlm.get("default") or {}
        expected_value = os.environ.get(variable) or ("qwen2.5vl" if key == "model" else "ollama")
        assert defaults.get(key) == expected_value
        assert effective.get(key) == expected_value
        return _result(
            probe_id,
            contract,
            "GET /admin/settings VLM resolution correlated with admin UI controls",
            semantic_field=key,
            effective_value_sha256=_sha256_text(str(effective[key])),
            ui_exact_match=True,
            environment_default_exact_match=True,
            available=bool(vlm.get("available")),
            cloud_allowed=bool(effective.get("cloud_allowed")),
        )

    if probe_id == "postgres-settings":
        cloud = admin.get("cloud") or {}
        expected_allowed = _bool_value(os.environ.get("SHERPA_PERSONAL_API_KEYS"))
        assert bool(cloud.get("personal_api_keys_allowed")) is expected_allowed
        assert bool(settings.get("personal_api_keys_allowed")) is expected_allowed
        rows = _db_rows(
            ctx,
            "SELECT key,value FROM system_settings WHERE key IN ('personal_api_keys_allowed','cloud_provider')",
        )
        return _result(
            probe_id,
            contract,
            "admin/user settings APIs correlated with direct system_settings rows",
            personal_keys_allowed=expected_allowed,
            api_views_match=True,
            persisted_setting_keys=sorted(str(row["key"]) for row in rows),
            persisted_row_count=len(rows),
        )

    if probe_id == "bedrock-region":
        completed = _run(
            [
                os.environ.get("PYTHON_BIN") or os.sys.executable,
                "-c",
                "from sherpa.providers.bedrock import _bedrock_region; print(_bedrock_region())",
            ]
        )
        assert completed.returncode == 0
        region = completed.stdout.strip()
        assert region == "ap-northeast-1"
        configured = os.environ.get("AWS_REGION")
        if configured is not None:
            raise AssertionError(
                "AWS_REGION is declared as a tested UI setting but the real Bedrock resolver "
                "intentionally ignores it and remains fixed to ap-northeast-1"
            )
        return _result(
            probe_id,
            contract,
            "real Bedrock product region resolver process",
            region=region,
            configured_environment_present=False,
            fixed_region=True,
        )

    if probe_id == "bedrock-auth-kind":
        cloud = admin.get("cloud") or {}
        credentials = {name: bool(os.environ.get(name)) for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE")}
        if credentials["AWS_ACCESS_KEY_ID"]:
            assert credentials["AWS_SECRET_ACCESS_KEY"]
            auth_kind = "sigv4-static"
        elif credentials["AWS_PROFILE"]:
            auth_kind = "sigv4-profile"
        else:
            auth_kind = "none"
        if credentials.get(variable) and auth_kind == "none":
            raise AssertionError(f"{variable} was present but did not form a usable real Bedrock auth chain")
        if auth_kind == "none":
            connection = None
        else:
            assert cloud.get("provider") == "bedrock"
            connection = _provider_connection(ctx, "bedrock")
        return _result(
            probe_id,
            contract,
            "presence-only AWS credential classification and real Bedrock connection test",
            auth_kind=auth_kind,
            credential_presence=credentials,
            credential_values_returned=False,
            selected_cloud_provider=cloud.get("provider"),
            connection=connection,
        )

    if probe_id == "compatibility-warning":
        me = ctx.api.get_json("/auth/me")
        disabled = _bool_value(os.environ.get("SHERPA_AUTH_DISABLED"))
        assert bool(me.get("auth_disabled")) is disabled
        ctx.page.goto(ctx.config.base_url + "/ui/login.html")
        visible = ctx.page.locator("[role='alert'], .warning, .warn, .notice").all_inner_texts()
        compatibility = [text for text in visible if "SHERPA_AUTH_ENABLED" in text or "互換" in text]
        assert compatibility, "legacy SHERPA_AUTH_ENABLED was accepted without a visible compatibility warning"
        return _result(
            probe_id,
            contract,
            "real auth behavior and visible login compatibility warning",
            auth_disabled=disabled,
            warning_count=len(compatibility),
            warning_sha256=[_sha256_text(text) for text in compatibility],
        )

    if probe_id in {
        "connection-test",
        "provider-auth-kind",
        "provider-endpoint-kind",
        "provider-model",
        "provider-url-summary",
    }:
        provider = "openai"
        if probe_id == "connection-test" and variable == "SHERPA_EXTRA_AGENTS":
            requested = [item.strip().casefold() for item in (os.environ.get(variable) or "").split(",") if item.strip()]
            available = {str(row.get("agent") or row.get("id") or "") for row in settings.get("constructs_available") or []}
            known = {"openai", "gemini", "bedrock", "heuristic"}
            assert all(item in available for item in requested if item in known)
            providers = [item for item in requested if item in {"openai", "gemini", "bedrock"}]
            connections = [_provider_connection(ctx, item) for item in providers]
            return _result(
                probe_id,
                contract,
                "SHERPA_EXTRA_AGENTS choices plus real connection tests for enabled providers",
                requested_agents=requested,
                all_known_requested_agents_available=True,
                provider_connections=connections,
            )
        connection = _provider_connection(ctx, provider)
        endpoint_kind = str(settings.get("openai_endpoint_kind") or "")
        endpoint_host = str(settings.get("openai_base_url_host") or "")
        assert endpoint_kind in {"openai", "azure", "custom"}
        if endpoint_kind == "openai":
            assert endpoint_host in {"", "api.openai.com"}
        else:
            assert endpoint_host and "/" not in endpoint_host and "@" not in endpoint_host
        if probe_id == "provider-model":
            configured_model = os.environ.get("OPENAI_CHAT_MODEL")
            if configured_model is not None:
                raise AssertionError(
                    "OPENAI_CHAT_MODEL is not consumed by Sherpa runtime settings; the tested classification cannot be demonstrated"
                )
            assert settings.get("openai_model") == "gpt-5.5"
        if probe_id == "provider-auth-kind":
            auth_kind = str(os.environ.get("SHERPA_OPENAI_AUTH_HEADER") or "bearer").casefold()
            assert auth_kind in {"bearer", "api-key"}
        else:
            auth_kind = None
        return _result(
            probe_id,
            contract,
            "public settings endpoint identity plus real OpenAI-compatible connection test",
            endpoint_kind=endpoint_kind,
            endpoint_host=endpoint_host,
            host_has_no_path_or_userinfo=True,
            model_sha256=_sha256_text(str(settings.get("openai_model") or "")),
            auth_kind=auth_kind,
            connection=connection,
        )

    if probe_id == "settings-connection-state":
        provider_by_variable = {
            "ANTHROPIC_AWS_API_KEY": "bedrock",
            "AWS_BEARER_TOKEN_BEDROCK": "bedrock",
            "GEMINI_API_KEY": "gemini",
            "OLLAMA_URL": "ollama",
            "OPENAI_API_KEY": "openai",
        }
        provider = provider_by_variable[variable]
        key_flag = {
            "bedrock": "bedrock_key_set",
            "gemini": "gemini_key_set",
            "openai": "openai_key_set",
        }.get(provider)
        configured_presence = bool(os.environ.get(variable))
        if key_flag:
            settings_presence = bool(settings.get(key_flag))
            assert settings_presence is configured_presence, f"{variable} presence was not reflected by the governed settings seed"
        else:
            settings_presence = bool(settings.get("ollama_url"))
        declared_product_error = str(_scenario(ctx).get("expected_outcome") or "") == "explicit-error"
        expected_connected = False if declared_product_error else configured_presence or provider == "ollama"
        connection = _provider_connection(ctx, provider, expected_ok=expected_connected)
        health, _ = _admin_health(ctx, refresh=True)
        component = _component(health, provider)
        assert bool(component.get("ok")) is expected_connected
        if declared_product_error:
            assert connection.get("ok") is False and expected_connected is False
            patterns = [str(value) for value in _scenario(ctx).get("expected_error_patterns") or ()]
            assert patterns, "explicit provider-error scenario has no expected error patterns"
            ctx.cache["observed_product_error"] = {
                "source": "api",
                "matched_patterns": patterns,
                "status_codes": [200],
                "evidence_refs": [f"network/http.jsonl:POST /settings/test ({provider} ok=false)"],
                "message_sha256": connection["detail_sha256"],
                "structured_ok": False,
                "provider": provider,
                "secret_values_returned": False,
            }
        return _result(
            probe_id,
            contract,
            "settings presence state, real connection test, and forced admin health component",
            provider=provider,
            configured_presence=configured_presence,
            settings_presence=settings_presence,
            connection=connection,
            expected_connected=expected_connected,
            health_ok=bool(component.get("ok")),
            health_latency_ms=int(component.get("latency_ms") or 0),
        )

    if probe_id in {
        "settings-fields",
        "settings-labels",
        "settings-options",
        "settings-reasoning",
        "settings-toggle",
    }:
        ctx.page.goto(ctx.config.base_url + "/ui/settings.html")
        ctx.page.locator("#save").wait_for(state="visible")
        if probe_id == "settings-fields":
            allowed = bool(settings.get("personal_api_keys_allowed"))
            key_rows = ["#okey-row", "#gkey-row", "#bkey-row"]
            visible = [ctx.page.locator(selector).is_visible() for selector in key_rows]
            notes = [
                ctx.page.locator(selector).is_visible()
                for selector in ("#okey-disabled-note", "#gkey-disabled-note", "#bkey-disabled-note")
            ]
            assert all(visible) is allowed
            assert all(notes) is (not allowed)
            measurements = {
                "personal_keys_allowed": allowed,
                "key_field_visibility": visible,
                "disabled_note_visibility": notes,
            }
        elif probe_id == "settings-labels":
            note = ctx.page.locator("#openai-endpoint-note")
            kind = str(settings.get("openai_endpoint_kind") or "openai")
            if kind == "openai":
                assert not note.is_visible()
                label_hash = None
            else:
                assert note.is_visible() and settings.get("openai_base_url_host") in note.inner_text()
                label_hash = _sha256_text(note.inner_text())
            measurements = {"endpoint_kind": kind, "label_sha256": label_hash}
        elif probe_id == "settings-options":
            choices = settings.get("constructs_available") or []
            options = ctx.page.locator("#agent option").evaluate_all(
                "els => els.map(el => ({value: el.value, agent: el.dataset.agent, label: el.textContent.trim()}))"
            )
            assert len(options) == len(choices) and all(row.get("label") for row in options)
            assert {row["value"] for row in options} == {row["id"] for row in choices}
            requested = {value.strip().casefold() for value in (os.environ.get("SHERPA_EXTRA_AGENTS") or "").split(",") if value.strip()}
            available_agents = {str(row.get("agent") or row.get("id") or "") for row in choices}
            known = {"openai", "gemini", "bedrock", "heuristic"}
            assert {value for value in requested if value in known} <= available_agents
            measurements = {
                "api_option_count": len(choices),
                "ui_option_count": len(options),
                "exact_id_set_match": True,
                "requested_agent_count": len(requested),
                "known_requested_agents_present": True,
            }
        elif probe_id == "settings-reasoning":
            selected = ctx.page.locator("#reason").input_value()
            assert selected == settings.get("codex_reasoning")
            expected_reasoning = os.environ.get("SHERPA_CODEX_REASONING") or "low"
            assert selected == expected_reasoning
            measurements = {
                "api_reasoning": settings.get("codex_reasoning"),
                "ui_reasoning": selected,
                "environment_match": True,
            }
        else:
            available = bool(settings.get("web_search_available"))
            row_visible = ctx.page.locator("#cwebsearch-row").is_visible()
            expected_available = _bool_value(os.environ.get("SHERPA_ALLOW_WEB_SEARCH"))
            assert available is expected_available and row_visible is expected_available
            measurements = {
                "api_available": available,
                "ui_toggle_visible": row_visible,
                "environment_match": True,
            }
        return _result(
            probe_id,
            contract,
            "GET /settings correlated with real settings-page controls",
            **measurements,
        )

    if probe_id == "usage-page":
        metering = admin.get("usage_metering") or {}
        expected_enabled = _bool_value(os.environ.get("SHERPA_USAGE_METERING"))
        assert bool(metering.get("effective")) is expected_enabled
        stats = ctx.api.get_json("/admin/usage/stats?days=30")
        ctx.page.goto(ctx.config.base_url + "/ui/usage.html")
        ctx.page.locator("#summary-tiles").wait_for(state="visible")
        assert ctx.page.locator("#access-denied").is_hidden()
        assert isinstance(stats, dict) and stats
        return _result(
            probe_id,
            contract,
            "admin usage-metering setting, real usage statistics API, and rendered usage page",
            metering_effective=expected_enabled,
            stats_key_count=len(stats),
            summary_tiles_visible=True,
            access_denied=False,
        )

    raise AssertionError(f"unhandled settings semantic probe: {probe_id}")


# These probes already had a direct adapter in ``environment_probes.py``, but the
# old adapter observed a broad subsystem (for example, generic health or merely
# the pytest environment).  This registry is deliberately keyed by *both* the
# observable and the generated variable.  Callers must fall back to their old
# direct adapter for every pair not listed here, including static profiles.
DIRECT_PROBE_VARIABLES: dict[str, frozenset[str]] = {
    "app-process": frozenset({"PYTHON_BIN"}),
    "cookie-flags": frozenset({"LAN"}),
    "effective-environment": frozenset({"NEO4J_URI", "PGDATABASE", "PGHOST", "PGUSER"}),
    "healthz": frozenset({"SHERPA_HOST", "SHERPA_PORT"}),
    "listen-address": frozenset(
        {
            "LAN",
            "PGPORT",
            "SHERPA_HOST",
            "SHERPA_LAN",
            "SHERPA_NEO4J_BOLT_PORT",
            "SHERPA_NEO4J_HTTP_PORT",
            "SHERPA_PORT",
        }
    ),
    "neo4j-http": frozenset({"SHERPA_NEO4J_HTTP_PORT"}),
    "neo4j-identity": frozenset({"NEO4J_URI", "NEO4J_USER", "SHERPA_NEO4J_BOLT_PORT"}),
    "pid-file": frozenset({"APP_PID_FILE"}),
    "postgres-identity": frozenset(
        {
            "PGDATABASE",
            "PGHOST",
            "PGPORT",
            "PGUSER",
            "POSTGRES_DB",
            "POSTGRES_USER",
        }
    ),
    "process-identity": frozenset({"APP_PID_FILE"}),
    "python-version": frozenset({"PYTHON_BIN"}),
    "status-url": frozenset({"LAN", "SHERPA_LAN"}),
}

# Namespaced alias for aggregators that import more than one semantic module.
SYSTEM_DIRECT_PROBE_VARIABLES = DIRECT_PROBE_VARIABLES


def supports_direct_probe(probe_id: str, variable: str | None) -> bool:
    """Return whether a generated variable/probe pair has a strict system adapter."""

    return bool(variable) and str(variable) in DIRECT_PROBE_VARIABLES.get(probe_id, frozenset())


def _assert_direct_probe_contract(ctx: Any, probe_id: str, variable: str) -> dict[str, Any]:
    scenario = _scenario(ctx)
    assert scenario, "system direct adapters only handle generated variable profiles"
    assert str(scenario.get("variable") or "") == variable, f"direct adapter variable differs from generated scenario: {variable}"
    declared = {str(value) for value in scenario.get("observables") or ()}
    assert probe_id in declared, f"{probe_id} is not declared for generated variable {variable}"
    assert supports_direct_probe(probe_id, variable), f"unsupported system direct probe pair: {probe_id}/{variable}"
    process = scenario.get("process") or {}
    assert isinstance(process, dict), "generated process contract must be an object"
    actual = os.environ.get(variable)
    return {
        "variable": variable,
        "scenario": scenario.get("scenario"),
        "scenario_set": scenario.get("scenario_set"),
        "process_mode": process.get("mode"),
        "process_present": actual is not None,
        "process_value_sha256": _sha256_text(actual) if actual is not None else None,
    }


def _app_process_record(ctx: Any) -> dict[str, Any]:
    """Resolve the real uvicorn master without trusting the optional PID file."""

    port = int(urlsplit(ctx.config.base_url).port or 0)
    assert 1 <= port <= 65535, "runner application URL has no valid port"
    records: list[dict[str, Any]] = []
    for proc_root in Path("/proc").iterdir():
        if not proc_root.name.isdigit():
            continue
        try:
            if proc_root.stat().st_uid != os.geteuid():
                continue
            raw_args = (proc_root / "cmdline").read_bytes().split(b"\0")
            args = [value.decode("utf-8", errors="replace") for value in raw_args if value]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if "-m" not in args or "uvicorn" not in args:
            continue
        try:
            port_index = args.index("--port")
            command_port = int(args[port_index + 1])
        except (ValueError, IndexError):
            command_port = next(
                (int(value.split("=", 1)[1]) for value in args if value.startswith("--port=") and value.split("=", 1)[1].isdigit()),
                -1,
            )
        if command_port != port:
            continue
        try:
            host_index = args.index("--host")
            command_host = args[host_index + 1]
        except (ValueError, IndexError):
            command_host = next(
                (value.split("=", 1)[1] for value in args if value.startswith("--host=")),
                "",
            )
        records.append(
            {
                "pid": int(proc_root.name),
                "root": proc_root,
                "args": args,
                "command_host": command_host,
                "command_port": command_port,
            }
        )
    assert len(records) == 1, f"expected one same-user uvicorn master on port {port}, got {len(records)}"
    return records[0]


def _decode_proc_address(raw: str, family: socket.AddressFamily) -> str:
    packed = bytes.fromhex(raw)
    if family == socket.AF_INET:
        return socket.inet_ntop(family, packed[::-1])
    # Linux renders each native-endian uint32 of an IPv6 address separately.
    reordered = b"".join(packed[offset : offset + 4][::-1] for offset in range(0, len(packed), 4))
    return socket.inet_ntop(family, reordered)


def _process_listeners(record: dict[str, Any]) -> list[dict[str, Any]]:
    process_root = Path(record["root"])
    socket_inodes: set[str] = set()
    try:
        descriptors = list((process_root / "fd").iterdir())
    except (FileNotFoundError, PermissionError, ProcessLookupError) as exc:
        raise AssertionError("cannot inspect application socket descriptors") from exc
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        matched = re.fullmatch(r"socket:\[(\d+)\]", target)
        if matched:
            socket_inodes.add(matched.group(1))
    assert socket_inodes, "application process owns no inspectable sockets"

    listeners: list[dict[str, Any]] = []
    for name, family in (("tcp", socket.AF_INET), ("tcp6", socket.AF_INET6)):
        table = process_root / "net" / name
        if not table.is_file():
            continue
        for line in table.read_text(encoding="ascii", errors="replace").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A" or fields[9] not in socket_inodes:
                continue
            raw_address, raw_port = fields[1].split(":", 1)
            listeners.append(
                {
                    "address": _decode_proc_address(raw_address, family),
                    "port": int(raw_port, 16),
                    "family": "ipv4" if family == socket.AF_INET else "ipv6",
                    "inode_sha256": _sha256_text(fields[9]),
                }
            )
    assert listeners, "application process owns no TCP listening socket"
    return sorted(listeners, key=lambda row: (row["port"], row["family"], row["address"]))


def _strict_switch(variable: str, value: str | None) -> bool:
    normalized = (value or "").strip().casefold()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    raise AssertionError(f"{variable} has an invalid boolean value in the real process")


def _effective_lan_mode() -> tuple[bool, str]:
    direct = os.environ.get("LAN")
    if direct:
        return _strict_switch("LAN", direct), "LAN"
    configured = os.environ.get("SHERPA_LAN")
    return _strict_switch("SHERPA_LAN", configured), "SHERPA_LAN/default"


def _direct_application_listener(ctx: Any, variable: str) -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        record = _app_process_record(ctx)
        listeners = _process_listeners(record)
        configured_port = int(os.environ.get("SHERPA_PORT") or "8000")
        base_port = int(urlsplit(ctx.config.base_url).port or 0)
        assert configured_port == base_port == int(record["command_port"]), (
            "SHERPA_PORT differs across environment, runner URL, and uvicorn command"
        )
        matching = [row for row in listeners if row["port"] == configured_port]
        assert matching, "uvicorn owns no listening socket on SHERPA_PORT"

        lan_enabled, lan_source = _effective_lan_mode()
        expected_host = "0.0.0.0" if lan_enabled else (os.environ.get("SHERPA_HOST") or "127.0.0.1")
        assert record["command_host"] == expected_host, {
            "variable": variable,
            "expected_bind": expected_host,
            "uvicorn_bind": record["command_host"],
            "lan_source": lan_source,
        }
        listening_addresses = {str(row["address"]) for row in matching}
        expected_addresses = {
            "0.0.0.0": {"0.0.0.0"},
            "::": {"::"},
            "localhost": {"127.0.0.1", "::1"},
        }.get(expected_host, {expected_host})
        assert listening_addresses.intersection(expected_addresses), {
            "expected_bind": sorted(expected_addresses),
            "procfs_listeners": sorted(listening_addresses),
        }

        health = ctx.api.get_json("/healthz")
        assert health.get("ok") is True
        status = ctx.api.request("GET", "/ui/status.html", expected=200)
        assert status.body and b"<html" in status.body[:1000].lower()
        return {
            "configured_port": configured_port,
            "runner_url_port": base_port,
            "uvicorn_command_port": int(record["command_port"]),
            "expected_bind_address": expected_host,
            "uvicorn_command_bind_address": record["command_host"],
            "procfs_listeners": matching,
            "lan_enabled": lan_enabled,
            "lan_precedence_source": lan_source,
            "healthz_ok": True,
            "status_document_status": status.status,
            "reachable_origin": ctx.config.base_url,
            "app_pid_sha256": _sha256_text(str(record["pid"])),
        }

    return _cache(ctx, f"system-direct-listener:{variable}", collect)


def _direct_lan_cookie(ctx: Any, variable: str) -> dict[str, Any]:
    listener = _direct_application_listener(ctx, variable)
    ctx.page.goto(ctx.config.base_url + "/ui/status.html")
    me = ctx.page.evaluate(
        "async () => { const r = await fetch('/auth/me', {credentials: 'same-origin'}); return {status: r.status, body: await r.json()}; }"
    )
    assert isinstance(me, dict) and int(me.get("status") or 0) == 200
    body = me.get("body") or {}
    cookies = [row for row in ctx.page.context.cookies(ctx.config.base_url) if row.get("httpOnly")]
    auth_disabled = bool(body.get("auth_disabled"))
    if auth_disabled:
        assert not cookies
        return {
            **listener,
            "auth_disabled": True,
            "http_only_session_count": 0,
            "browser_auth_status": 200,
        }
    assert body.get("role") == "admin" and cookies, "plain-HTTP LAN browser could not send its authenticated session cookie"
    forced = os.environ.get("SHERPA_COOKIE_SECURE", "").strip()
    expected_secure = _strict_switch("SHERPA_COOKIE_SECURE", forced) if forced else urlsplit(ctx.config.base_url).scheme == "https"
    secure_flags = [bool(row.get("secure")) for row in cookies]
    assert all(flag is expected_secure for flag in secure_flags)
    if listener["lan_enabled"] and urlsplit(ctx.config.base_url).scheme == "http":
        assert not expected_secure, "plain-HTTP LAN mode produced Secure session cookies that browsers cannot send"
    return {
        **listener,
        "auth_disabled": False,
        "browser_auth_status": 200,
        "browser_role": body.get("role"),
        "http_only_session_count": len(cookies),
        "secure_flags": secure_flags,
        "expected_secure": expected_secure,
        "session_values_returned": False,
    }


def _hosts_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    loopback = {"localhost", "127.0.0.1", "::1"}
    if left in loopback and right in loopback:
        return True
    try:
        left_addresses = {row[4][0] for row in socket.getaddrinfo(left, None, type=socket.SOCK_STREAM)}
        right_addresses = {row[4][0] for row in socket.getaddrinfo(right, None, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        return False
    return bool(left_addresses.intersection(right_addresses))


def _direct_postgres_identity(ctx: Any, variable: str) -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from sherpa.store import db as store_db
        except ModuleNotFoundError as exc:
            raise AssertionError("psycopg is required for direct PostgreSQL evidence") from exc

        selected_dsn = store_db._dsn()
        if os.environ.get("SHERPA_PG_DSN"):
            selected_source = "SHERPA_PG_DSN"
        elif os.environ.get("DATABASE_URL"):
            selected_source = "DATABASE_URL"
        else:
            selected_source = "PG* discrete variables"
        with psycopg.connect(
            selected_dsn,
            row_factory=dict_row,
            connect_timeout=5,
        ) as connection:
            row = connection.execute(
                "SELECT current_database() AS database,current_user AS username,"
                "inet_server_addr()::text AS address,inet_server_port() AS server_port,"
                "current_setting('server_version') AS server_version,pg_backend_pid() AS backend_pid"
            ).fetchone()
            assert row
            client_host = str(connection.info.host or "")
            client_port = int(connection.info.port)
            client_database = str(connection.info.dbname or "")
            client_user = str(connection.info.user or "")

        runner = _load_json(_profile_root(ctx) / "state" / "postgres-identity.json")
        assert isinstance(runner, dict) and runner
        assert str(row["database"]) == str(runner.get("database"))
        assert str(row["username"]) == str(runner.get("user") or runner.get("username"))
        assert str(row["address"]) == str(runner.get("address"))
        assert int(row["server_port"]) == int(runner.get("port"))
        assert int(row["server_port"]) == 5432, "PostgreSQL server did not report its container-internal port"
        assert str(runner.get("compose_project")) == os.environ.get("COMPOSE_PROJECT_NAME")

        compose = _compose_config()
        postgres_service = (compose.get("services") or {}).get("postgres") or {}
        compose_environment = postgres_service.get("environment") or {}
        assert isinstance(compose_environment, dict)
        expected_user = os.environ.get("POSTGRES_USER") or "sherpa"
        expected_database = os.environ.get("POSTGRES_DB") or "sherpa"
        expected_client_host = os.environ.get("PGHOST") or "localhost"
        expected_client_user = os.environ.get("PGUSER") or "sherpa"
        expected_client_database = os.environ.get("PGDATABASE") or "sherpa"
        expected_client_port = int(os.environ.get("PGPORT") or "5432")

        if variable == "POSTGRES_USER":
            assert str(compose_environment.get("POSTGRES_USER") or "") == expected_user
            assert str(row["username"]) == expected_user
        elif variable == "POSTGRES_DB":
            assert str(compose_environment.get("POSTGRES_DB") or "") == expected_database
            assert str(row["database"]) == expected_database
        elif variable == "PGHOST":
            assert _hosts_equivalent(client_host, expected_client_host), {
                "selected_dsn_source": selected_source,
                "actual_host": client_host,
                "expected_host": expected_client_host,
            }
        elif variable == "PGUSER":
            assert client_user == expected_client_user and str(row["username"]) == expected_client_user
        elif variable == "PGDATABASE":
            assert client_database == expected_client_database and str(row["database"]) == expected_client_database
        elif variable == "PGPORT":
            assert client_port == expected_client_port
            published = {
                int(str(item.get("published")))
                for item in postgres_service.get("ports") or []
                if str(item.get("published") or "").isdigit() and int(item.get("target") or 0) == 5432
            }
            assert expected_client_port in published, "PGPORT differs from the real Compose PostgreSQL published port"
        else:
            raise AssertionError(f"no PostgreSQL direct semantics for {variable}")

        with socket.create_connection((client_host, client_port), timeout=5):
            pass
        return {
            "selected_connection_source": selected_source,
            "client_host": client_host,
            "client_port": client_port,
            "client_database": client_database,
            "client_user": client_user,
            "database": row["database"],
            "database_user": row["username"],
            "server_address": row["address"],
            "server_internal_port": row["server_port"],
            "runner_server_internal_port": int(runner["port"]),
            "server_version_sha256": _sha256_text(str(row["server_version"])),
            "backend_pid_sha256": _sha256_text(str(row["backend_pid"])),
            "runner_identity_exact_match": True,
            "compose_project": runner.get("compose_project"),
            "compose_postgres_user": compose_environment.get("POSTGRES_USER"),
            "compose_postgres_database": compose_environment.get("POSTGRES_DB"),
            "password_returned": False,
        }

    return _cache(ctx, f"system-direct-postgres:{variable}", collect)


def _direct_neo4j_identity(ctx: Any, variable: str) -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        try:
            from neo4j import GraphDatabase
            from sherpa.ingest import world_neo4j
        except ModuleNotFoundError as exc:
            raise AssertionError("neo4j driver is required for direct graph evidence") from exc
        selected = world_neo4j._env()
        uri = str(selected.get("uri") or "")
        username = str(selected.get("user") or "")
        password = str(selected.get("pw") or "")
        parsed = urlsplit(uri)
        assert parsed.scheme in {"bolt", "neo4j"} and parsed.hostname and parsed.port
        try:
            with GraphDatabase.driver(uri, auth=(username, password)) as driver:
                driver.verify_connectivity()
                server = driver.get_server_info()
                with driver.session() as session:
                    component = session.run(
                        "CALL dbms.components() YIELD name,versions,edition RETURN name,versions,edition LIMIT 1"
                    ).single()
                    current = session.run(
                        "SHOW CURRENT USER YIELD user,roles,passwordChangeRequired,suspended "
                        "RETURN user,roles,passwordChangeRequired,suspended"
                    ).single()
        except Exception as exc:
            scenario = _scenario(ctx)
            if variable != "NEO4J_USER" or str(scenario.get("expected_outcome") or "") != "explicit-error":
                raise
            health, _ = _admin_health(ctx, refresh=True)
            health_component = _component(health, "neo4j")
            assert health_component.get("ok") is False, "invalid NEO4J_USER did not fail the product health probe"
            safe_message = json.dumps(health_component, ensure_ascii=False, sort_keys=True)
            patterns = [str(value) for value in scenario.get("expected_error_patterns") or ()]
            matched_patterns = [pattern for pattern in patterns if re.search(pattern, safe_message, re.IGNORECASE)]
            assert patterns and set(matched_patterns) == set(patterns), {
                "declared_patterns": patterns,
                "matched_patterns": matched_patterns,
                "component_detail": health_component.get("detail"),
            }
            ctx.cache["observed_product_error"] = {
                "source": "api",
                "matched_patterns": matched_patterns,
                "status_codes": [200],
                "evidence_refs": ["network/http.jsonl:GET /admin/health?refresh=1 (neo4j ok=false)"],
                "message_sha256": _sha256_text(safe_message),
                "structured_ok": False,
                "component": "neo4j",
                "secret_values_returned": False,
            }
            raise AssertionError("real Sherpa health API rejected the configured NEO4J_USER") from exc
        assert component and current
        actual_user = str(current.get("user") or "")
        server_address = getattr(server, "address", None)
        server_host = str(getattr(server_address, "host", parsed.hostname) or parsed.hostname)
        server_port = int(getattr(server_address, "port", parsed.port) or parsed.port)

        expected_user = os.environ.get("NEO4J_USER", "neo4j")
        expected_uri = os.environ.get("NEO4J_URI") or ("bolt://localhost:" + (os.environ.get("SHERPA_NEO4J_BOLT_PORT") or "7687"))
        expected_parsed = urlsplit(expected_uri)
        if variable == "NEO4J_USER":
            assert username == expected_user and actual_user == expected_user
        elif variable == "NEO4J_URI":
            assert uri == expected_uri
            assert _hosts_equivalent(str(parsed.hostname), str(expected_parsed.hostname))
            assert parsed.port == expected_parsed.port == server_port
        elif variable == "SHERPA_NEO4J_BOLT_PORT":
            expected_port = int(os.environ.get(variable) or "7687")
            assert parsed.port == expected_port == server_port
        else:
            raise AssertionError(f"no Neo4j direct semantics for {variable}")
        with socket.create_connection((str(parsed.hostname), int(parsed.port)), timeout=5):
            pass
        return {
            "selected_uri_scheme": parsed.scheme,
            "selected_host": parsed.hostname,
            "selected_port": parsed.port,
            "selected_user": username,
            "current_user": actual_user,
            "roles": sorted(str(role) for role in (current.get("roles") or [])),
            "password_change_required": bool(current.get("passwordChangeRequired")),
            "suspended": bool(current.get("suspended")),
            "server_host": server_host,
            "server_port": server_port,
            "server_agent_sha256": _sha256_text(str(getattr(server, "agent", ""))),
            "component": component.get("name"),
            "versions": list(component.get("versions") or []),
            "edition": component.get("edition"),
            "password_present": bool(password),
            "password_returned": False,
        }

    return _cache(ctx, f"system-direct-neo4j:{variable}", collect)


def _direct_neo4j_http(ctx: Any, variable: str) -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        port = int(os.environ.get("SHERPA_NEO4J_HTTP_PORT") or "7474")
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
        url = f"http://127.0.0.1:{port}/"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "sherpa-ui-system-semantic"},
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            server_header = response.headers.get("Server", "")
            status = response.status
        elapsed_ms = int((time.monotonic() - started) * 1000)
        ctx.evidence.record_api(method="GET", url=url, status=status, elapsed_ms=elapsed_ms)
        assert status == 200 and body
        compose = _compose_config()
        neo4j_service = (compose.get("services") or {}).get("neo4j") or {}
        published = {
            int(str(item.get("published")))
            for item in neo4j_service.get("ports") or []
            if str(item.get("published") or "").isdigit() and int(item.get("target") or 0) == 7474
        }
        assert port in published, "SHERPA_NEO4J_HTTP_PORT differs from the real Compose published port"
        return {
            "configured_port": port,
            "compose_published_port": port,
            "tcp_connected": True,
            "http_status": status,
            "content_type": content_type,
            "server_header_sha256": _sha256_text(server_header),
            "body_sha256": _sha256_bytes(body),
            "body_bytes": len(body),
            "elapsed_ms": elapsed_ms,
        }

    return _cache(ctx, f"system-direct-neo4j-http:{variable}", collect)


def _direct_app_runtime_identity(ctx: Any, variable: str) -> dict[str, Any]:
    def collect() -> dict[str, Any]:
        record = _app_process_record(ctx)
        pid = int(record["pid"])
        process_root = Path(record["root"])
        process_env = _proc_environment(pid)
        executable = (process_root / "exe").resolve()
        assert executable.is_file() and os.access(executable, os.X_OK)
        if variable == "APP_PID_FILE":
            configured = os.environ.get(variable) or str(Path(os.environ["RUN_DIR"]) / "api.pid")
            process_value = process_env.get(variable) or str(Path(process_env.get("RUN_DIR") or os.environ["RUN_DIR"]) / "api.pid")
            assert Path(process_value).resolve() == Path(configured).resolve(), (
                "APP_PID_FILE differs between the generated contract and app process"
            )
            pid_file = Path(process_value)
            assert pid_file.is_file() and not pid_file.is_symlink(), "the real app lifecycle did not create APP_PID_FILE"
            text = pid_file.read_text(encoding="utf-8").strip()
            assert text.isdigit() and int(text) == pid
            assert pid_file.stat().st_uid == process_root.stat().st_uid == os.geteuid()
            return {
                "app_pid_sha256": _sha256_text(str(pid)),
                "pid_file_path_sha256": _sha256_text(str(pid_file.resolve())),
                "pid_file_mode": oct(stat.S_IMODE(pid_file.stat().st_mode)),
                "pid_file_owner_matches_process": True,
                "pid_file_points_to_uvicorn": True,
                "uvicorn_command_port": record["command_port"],
            }

        assert variable == "PYTHON_BIN"
        configured = os.environ.get(variable) or ""
        if configured:
            resolved = shutil.which(configured, path=os.environ.get("PATH"))
            expected = Path(resolved or configured).resolve()
        else:
            venv_python = ROOT / ".venv" / "bin" / "python"
            fallback = str(venv_python) if venv_python.is_file() else "python3"
            resolved = shutil.which(fallback, path=os.environ.get("PATH"))
            assert resolved
            expected = Path(resolved).resolve()
        assert executable == expected, {
            "configured_python_sha256": _sha256_text(configured) if configured else None,
            "expected_executable_sha256": _sha256_text(str(expected)),
            "proc_executable_sha256": _sha256_text(str(executable)),
        }
        process_python = process_env.get("PYTHON_BIN") or ""
        if process_python:
            process_python_resolved = shutil.which(process_python, path=process_env.get("PATH"))
            assert process_python_resolved
            assert Path(process_python_resolved).resolve() == executable
        version = _run([str(executable), "--version"], timeout=15)
        version_text = (version.stdout or version.stderr).strip()
        assert version.returncode == 0 and version_text.startswith("Python ")
        assert record["args"][:3] == [record["args"][0], "-m", "uvicorn"]
        return {
            "app_pid_sha256": _sha256_text(str(pid)),
            "configured_python_sha256": _sha256_text(configured) if configured else None,
            "process_environment_python_sha256": (_sha256_text(process_python) if process_python else None),
            "proc_executable_sha256": _sha256_text(str(executable)),
            "configured_matches_proc_executable": True,
            "uvicorn_module_invocation": True,
            "python_version_sha256": _sha256_text(version_text),
        }

    return _cache(ctx, f"system-direct-runtime:{variable}", collect)


_AUTH = frozenset(
    {
        "audit-ip-hash",
        "login",
        "login-redirect",
        "login-result",
        "password-change-required",
        "postgres-session",
        "redacted-audit",
        "role-boundary",
        "session-expiry",
    }
)
_BROWSER = frozenset({"artifact-render", "folder-picker", "fs-list-rejection", "unicode-ui"})
_FILESYSTEM = frozenset(
    {
        "artifact-file",
        "compose-config",
        "compose-mount",
        "created-file-owner",
        "derived-files",
        "hashed-codex-home",
        "hashed-home",
        "model-files-sha256",
        "ollama-model-path",
        "redacted-compose-config",
        "redacted-effective-environment",
        "restart-effective-environment",
        "volume-identity",
        "volume-names",
        "world-registry",
        "world-root-check",
        "write-boundary",
    }
)
_HEALTH = frozenset(
    {
        "ai-health-cache-age",
        "ai-health-duration",
        "compose-health",
        "health-cache-age",
        "health-duration",
        "request-count",
        "startup-duration",
        "status-command-duration",
        "status-output",
    }
)
_LOGS = frozenset({"app-log", "author-error", "ingest-error", "ingest-log", "ocr-worker-log", "preflight-log", "proxy-log"})
_PROCESS = frozenset(
    {
        "codex-invocation-summary",
        "codex-login-status",
        "codex-tls-probe",
        "codex-version",
        "command-resolution",
        "ollama-listen-address",
        "port-owner-check",
        "preflight-exit",
        "process-count",
        "process-exit",
        "shutdown-duration",
        "worker-version",
    }
)
_SETTINGS = frozenset(
    {
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
        "settings-connection-state",
        "settings-fields",
        "settings-labels",
        "settings-options",
        "settings-reasoning",
        "settings-toggle",
        "usage-page",
    }
)

SUPPORTED_PROBES = frozenset().union(
    _AUTH,
    _BROWSER,
    _FILESYSTEM,
    _HEALTH,
    _LOGS,
    _PROCESS,
    _SETTINGS,
)

assert SUPPORTED_PROBES == frozenset(_PROBE_VARIABLES), "system semantic dispatch and variable contracts differ"


def run_system_semantic_probe(ctx: Any, probe_id: str) -> dict[str, Any]:
    """Run one supported, real system-side semantic observation."""

    contract = _assert_probe_contract(ctx, probe_id)
    if probe_id in _AUTH:
        return _auth_probe(ctx, probe_id, contract)
    if probe_id in _BROWSER:
        return _browser_probe(ctx, probe_id, contract)
    if probe_id in _FILESYSTEM:
        return _filesystem_probe(ctx, probe_id, contract)
    if probe_id in _HEALTH:
        return _health_probe(ctx, probe_id, contract)
    if probe_id in _LOGS:
        return _log_probe(ctx, probe_id, contract)
    if probe_id in _PROCESS:
        return _process_probe(ctx, probe_id, contract)
    if probe_id in _SETTINGS:
        return _settings_probe(ctx, probe_id, contract)
    raise AssertionError(f"unsupported system semantic probe: {probe_id}")


def run_system_direct_probe(
    ctx: Any,
    probe_id: str,
    variable: str | None = None,
) -> dict[str, Any]:
    """Run a variable-specific replacement for one formerly broad direct probe.

    The caller must first use :func:`supports_direct_probe`.  Unsupported pairs
    intentionally raise rather than silently degrading to a generic health or
    environment-presence observation.
    """

    variable = str(variable or _scenario(ctx).get("variable") or "")
    contract = _assert_direct_probe_contract(ctx, probe_id, variable)
    application_variables = {"LAN", "SHERPA_HOST", "SHERPA_LAN", "SHERPA_PORT"}
    postgres_variables = {
        "PGDATABASE",
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "POSTGRES_DB",
        "POSTGRES_USER",
    }
    neo4j_variables = {"NEO4J_URI", "NEO4J_USER", "SHERPA_NEO4J_BOLT_PORT"}

    if variable in application_variables:
        if probe_id == "cookie-flags":
            measurements = _direct_lan_cookie(ctx, variable)
            source = "procfs uvicorn listener, reachable status/health URLs, and real browser session-cookie transmission"
        else:
            measurements = _direct_application_listener(ctx, variable)
            source = "uvicorn command line, owned procfs TCP listener, and real health/status HTTP"
    elif variable in postgres_variables:
        measurements = _direct_postgres_identity(ctx, variable)
        source = "product-selected psycopg connection identity, isolated Compose config, runner identity ledger, and TCP connection"
    elif variable in neo4j_variables:
        measurements = _direct_neo4j_identity(ctx, variable)
        source = "product-selected Neo4j driver session, current-user query, server identity, and TCP connection"
    elif variable == "SHERPA_NEO4J_HTTP_PORT":
        measurements = _direct_neo4j_http(ctx, variable)
        source = "real Neo4j HTTP response, TCP connection, and Compose published port"
    elif variable in {"APP_PID_FILE", "PYTHON_BIN"}:
        measurements = _direct_app_runtime_identity(ctx, variable)
        source = "same-user uvicorn procfs executable/environment/command line and lifecycle file"
    else:
        raise AssertionError(f"unsupported system direct probe pair: {probe_id}/{variable}")
    return _result(probe_id, contract, source, variable=variable, **measurements)


from ui_automation.runner.artifacts import write_private_text_atomic
