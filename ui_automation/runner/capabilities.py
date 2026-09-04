"""実サービス能力を段階別に確認し、未充足を明示的な失敗にする。"""

from __future__ import annotations

import importlib.util
import fnmatch
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ui_automation.runner.artifacts import SecretRedactor, safe_url
from ui_automation.runner.config import capability_evidence_path
from ui_automation.runner.policy import scan_source_policy, validate_forbidden_tokens
from ui_automation.stack.isolation import verify_local_docker_environment


@dataclass(frozen=True)
class CapabilityResult:
    requirement_id: str
    phase: str
    status: str
    required: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.requirement_id,
            "phase": self.phase,
            "status": self.status,
            "required": self.required,
            "detail": self.detail,
        }


def requirement_phase(requirement: dict[str, Any]) -> str:
    check = requirement.get("check") or {}
    if isinstance(check, dict) and check.get("phase"):
        return str(check["phase"])
    kind = str(requirement.get("kind", ""))
    requirement_id = str(requirement.get("id", ""))
    if kind == "compose_service" or requirement_id in {"postgres", "elasticsearch", "neo4j"}:
        return "post_stack"
    if requirement_id == "fastapi-application":
        return "post_app"
    if kind == "provider":
        return "post_tests"
    return "preflight"


class CapabilityChecker:
    def __init__(
        self,
        *,
        requirements: list[dict[str, Any]],
        suite: str,
        environment: dict[str, str],
        runner_environment: dict[str, str],
        profile_name: str,
        repository: Path,
        runtime_root: Path,
        profile_root: Path,
        redactor: SecretRedactor,
        compose_probe: Callable[[str, str | None, bool], tuple[bool, str]] | None = None,
    ) -> None:
        self.requirements = requirements
        self.suite = suite
        self.environment = environment
        self.runner_environment = runner_environment
        self.profile_name = profile_name
        self.repository = repository
        self.runtime_root = runtime_root.resolve()
        self.profile_root = profile_root
        self.redactor = redactor
        self.compose_probe = compose_probe
        self.results: list[CapabilityResult] = []
        self._evidence_metadata: dict[str, dict[str, Any]] = {}

    def check_phase(self, phase: str, *, suite_passed: bool | None = None) -> list[CapabilityResult]:
        phase_results: list[CapabilityResult] = []
        for requirement in self.requirements:
            if not self._applies(requirement) or requirement_phase(requirement) != phase:
                continue
            result, evidence = self._check(requirement, phase=phase, suite_passed=suite_passed)
            try:
                metadata = self._write_requirement_evidence(requirement, result, evidence)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                requirement_id = str(requirement.get("id") or "unnamed")
                required = self._is_required(requirement)
                result = CapabilityResult(
                    requirement_id=requirement_id,
                    phase=phase,
                    status="FAIL" if required else "NOT_CONFIGURED",
                    required=required,
                    detail=self.redactor.redact_text(
                        f"capability response evidence is unavailable or invalid: {type(exc).__name__}: {exc}"
                    ),
                )
                metadata = {
                    "declared_path": str(capability_evidence_path(requirement_id)),
                    "exists": False,
                    "validated": False,
                    "error": result.detail,
                }
            self._evidence_metadata[result.requirement_id] = metadata
            self.results.append(result)
            phase_results.append(result)
        self._write_index()
        return phase_results

    @staticmethod
    def failures(results: list[CapabilityResult]) -> list[CapabilityResult]:
        return [item for item in results if item.required and item.status != "PASS"]

    def _applies(self, requirement: dict[str, Any]) -> bool:
        suites = requirement.get("required_for", requirement.get("suites", ()))
        if suites and self.suite not in {str(item) for item in suites}:
            return False
        required_profiles = tuple(str(item) for item in requirement.get("required_profiles") or ())
        if required_profiles and not any(fnmatch.fnmatchcase(self.profile_name, pattern) for pattern in required_profiles):
            return False
        excluded_profiles = tuple(str(item) for item in requirement.get("excluded_profiles") or ())
        return not any(fnmatch.fnmatchcase(self.profile_name, pattern) for pattern in excluded_profiles)

    def _is_required(self, requirement: dict[str, Any]) -> bool:
        if "required" in requirement:
            return bool(requirement["required"])
        return True

    def _check(
        self,
        requirement: dict[str, Any],
        *,
        phase: str,
        suite_passed: bool | None,
    ) -> tuple[CapabilityResult, str]:
        requirement_id = str(requirement.get("id") or "unnamed")
        kind = str(requirement.get("kind") or "")
        check = requirement.get("check") or {}
        required = self._is_required(requirement)
        try:
            if not isinstance(check, dict):
                raise ValueError("check must be a mapping")
            ok, detail, evidence = self._dispatch(kind, check, suite_passed=suite_passed)
        except Exception as exc:
            ok, detail, evidence = False, f"{type(exc).__name__}: {exc}", ""
        status = "PASS" if ok else ("FAIL" if required else "NOT_CONFIGURED")
        safe_detail = self.redactor.redact_text(detail)
        return CapabilityResult(requirement_id, phase, status, required, safe_detail), self.redactor.redact_text(evidence)

    def _dispatch(self, kind: str, check: dict[str, Any], *, suite_passed: bool | None) -> tuple[bool, str, str]:
        if kind == "command":
            return self._command(check)
        if kind == "python_module":
            return self._python_module(check)
        if kind == "tcp":
            return self._tcp(check)
        if kind == "http":
            return self._http(check)
        if kind in {"file", "directory"}:
            return self._path(check, directory=kind == "directory")
        if kind == "setting":
            return self._setting(check)
        if kind == "secret_presence":
            return self._secret_presence(check)
        if kind == "browser":
            return self._browser(check)
        if kind == "compose_service":
            return self._compose_service(check)
        if kind == "source_policy":
            return self._source_policy(check)
        if kind == "provider":
            return self._provider(check, suite_passed=suite_passed)
        return False, f"unsupported capability kind: {kind or '<empty>'}", ""

    def _command(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        command = check.get("command")
        command_env = check.get("command_env")
        if command_env:
            command = self.environment.get(str(command_env)) or check.get("fallback_command")
        if isinstance(command, str):
            if command == "${PYTHON:-python3}":
                command = self.runner_environment.get("PYTHON_BIN") or sys.executable
            argv = [str(command)]
        elif isinstance(command, list):
            argv = [str(item) for item in command]
        else:
            return False, "command is not configured", ""
        args = check.get("args") or ()
        if isinstance(args, str):
            args = shlex.split(args)
        argv.extend(str(item) for item in args)
        if Path(argv[0]).name == "docker":
            verify_local_docker_environment(self.runner_environment)
        completed = subprocess.run(
            argv,
            cwd=self.repository,
            env=self.runner_environment,
            capture_output=True,
            text=True,
            timeout=float(check.get("timeout_seconds", 30)),
            check=False,
        )
        expected = int(check.get("expected_exit", 0))
        output = (completed.stdout or "") + (completed.stderr or "")
        ok = completed.returncode == expected
        return ok, f"exit={completed.returncode}, expected={expected}", output

    def _python_module(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        modules = check.get("modules") or ([check.get("module")] if check.get("module") else [])
        missing = [str(name) for name in modules if importlib.util.find_spec(str(name)) is None]
        if missing:
            return False, f"missing Python modules: {', '.join(missing)}", ""
        return True, f"available modules: {', '.join(str(item) for item in modules)}", ""

    def _tcp(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        host = str(check.get("host") or self.environment.get(str(check.get("host_env", ""))) or "127.0.0.1")
        port_value = check.get("port") or self.environment.get(str(check.get("port_env", "")), "")
        try:
            port = int(str(port_value))
        except ValueError:
            return False, f"invalid TCP port: {port_value!r}", ""
        try:
            with socket.create_connection((host, port), timeout=float(check.get("timeout_seconds", 3))):
                pass
        except OSError as exc:
            return False, f"TCP unavailable at {host}:{port}: {exc}", ""
        return True, f"TCP reachable at {host}:{port}", ""

    def _expand(self, value: str) -> str:
        pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
        return pattern.sub(lambda match: self.environment.get(match.group(1), ""), value)

    def _http(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        base = ""
        if check.get("url_env"):
            base = self.environment.get(str(check["url_env"]), "")
        if not base and check.get("fallback"):
            base = self._expand(str(check["fallback"]))
        if not base and check.get("url"):
            base = str(check["url"])
        if not base:
            return False, "HTTP URL is not configured", ""
        path = str(check.get("path") or "")
        url = base.rstrip("/") + (path if path.startswith("/") else f"/{path}" if path else "")
        expected = int(check.get("expected_status", 200))
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        status = 0
        body = b""
        try:
            with urllib.request.urlopen(request, timeout=float(check.get("timeout_seconds", 10))) as response:
                status = response.status
                body = response.read(4096)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read(4096)
        except (OSError, ValueError) as exc:
            return False, f"HTTP unavailable at {safe_url(url)}: {exc}", ""
        return status == expected, f"HTTP {status} at {safe_url(url)}, expected {expected}", body.decode("utf-8", "replace")

    def _path(self, check: dict[str, Any], *, directory: bool) -> tuple[bool, str, str]:
        if check.get("must_exist", True) is not True:
            return False, "path capability must require a real existing path", ""
        raw = ""
        if check.get("path_env"):
            raw = self.environment.get(str(check["path_env"]), "")
        if not raw and check.get("path"):
            raw = str(check["path"])
        if not raw:
            return False, "path is not configured", ""
        path = Path(raw)
        if not path.is_absolute():
            path = self.repository / path
        exists = path.is_dir() if directory else path.is_file()
        if not exists:
            return False, f"required {'directory' if directory else 'file'} is missing: {path}", ""
        if check.get("must_not_be_empty"):
            try:
                nonempty = any(path.iterdir()) if directory else path.stat().st_size > 0
            except OSError:
                nonempty = False
            if not nonempty:
                return False, f"required path is empty: {path}", ""
        return True, f"path available: {path}", ""

    def _setting(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        names = check.get("envs") or ([check.get("env")] if check.get("env") else [])
        missing = [str(name) for name in names if not self.environment.get(str(name))]
        if missing:
            return False, f"settings are missing: {', '.join(missing)}", ""
        rejected = {str(item) for item in check.get("reject_values") or ()}
        required_prefix = str(check.get("required_prefix") or "")
        for name in names:
            value = self.environment[str(name)]
            if value in rejected:
                return False, f"{name} uses a rejected value", ""
            if required_prefix and not value.startswith(required_prefix):
                return False, f"{name} must start with {required_prefix!r}", ""
            if check.get("must_be_under_run_directory"):
                try:
                    Path(value).resolve().relative_to(self.runtime_root)
                except (OSError, ValueError):
                    return False, f"{name} is outside the isolated runtime directory", ""
            if check.get("reject_repository_data_directory"):
                try:
                    Path(value).resolve().relative_to((self.repository / "data").resolve())
                except (OSError, ValueError):
                    pass
                else:
                    return False, f"{name} points into the repository data directory", ""
        return True, f"validated settings: {', '.join(str(item) for item in names)}", ""

    def _secret_presence(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        placeholders = {str(item) for item in check.get("reject_placeholders") or ()}
        candidates = check.get("env_any_of") or ()
        if candidates:
            selected = next(
                (
                    str(name)
                    for name in candidates
                    if self.environment.get(str(name), "") and self.environment.get(str(name), "") not in placeholders
                ),
                "",
            )
            if not selected:
                return False, "none of the required credential alternatives is configured", ""
            companions = check.get("all_with") or {}
            required_companions: list[str] = []
            if isinstance(companions, dict):
                raw = companions.get(selected) or ()
                required_companions = [str(item) for item in (raw if isinstance(raw, list) else [raw]) if item]
            elif isinstance(companions, list):
                required_companions = [str(item) for item in companions]
            missing = [name for name in required_companions if not self.environment.get(name)]
            if missing:
                return False, f"credential {selected} requires: {', '.join(missing)}", ""
            return True, f"credential alternative {selected} is set", ""
        name = str(check.get("env_or_seeded_setting") or check.get("env") or "")
        value = self.environment.get(name, "")
        if not value or value in placeholders:
            return False, f"required secret {name or '<unnamed>'} is unset or a placeholder", ""
        return True, f"required secret {name} is set", ""

    def _browser(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        engine = str(check.get("engine") or "chromium")
        if check.get("launch") is not True:
            return False, "browser capability must declare a real launch", ""
        headless = bool(check.get("headless", True))
        script = (
            "from playwright.sync_api import sync_playwright; "
            "p=sync_playwright().start(); "
            f"b=getattr(p, {engine!r}).launch(headless={headless!r}); "
            "print(b.version); b.close(); p.stop()"
        )
        completed = subprocess.run(
            [self.runner_environment.get("PYTHON_BIN", sys.executable), "-c", script],
            cwd=self.repository,
            env=self.runner_environment,
            capture_output=True,
            text=True,
            timeout=float(check.get("timeout_seconds", 45)),
            check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        return completed.returncode == 0, f"{engine} launch exit={completed.returncode}", output

    def _compose_service(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        if self.compose_probe is None:
            return False, "compose service probe is unavailable", ""
        allow_completed = bool(check.get("require_healthy_or_completed_probe", False))
        return (
            *self.compose_probe(
                str(check.get("service") or ""),
                str(check.get("profile")) if check.get("profile") else None,
                allow_completed,
            ),
            "",
        )

    def _source_policy(self, check: dict[str, Any]) -> tuple[bool, str, str]:
        contract_errors = validate_forbidden_tokens(check.get("forbidden_tokens"))
        roots = check.get("roots") or ["ui_automation"]
        violations = []
        for raw in roots:
            path = Path(str(raw))
            if not path.is_absolute():
                path = self.repository / path
            violations.extend(scan_source_policy(path))
        serialized = json.dumps(
            {
                "contract_errors": contract_errors,
                "violations": [item.as_dict() for item in violations],
            },
            ensure_ascii=False,
            indent=2,
        )
        ok = not contract_errors and not violations
        detail = f"source policy contract_errors={len(contract_errors)} violations={len(violations)}"
        return ok, detail, serialized

    def _provider(self, check: dict[str, Any], *, suite_passed: bool | None) -> tuple[bool, str, str]:
        for contract in ("perform_real_request", "perform_real_image_request"):
            if contract in check and check[contract] is not True:
                return False, f"{contract} must be true when declared", ""
        expected = str(check.get("provider") or self.environment.get(str(check.get("provider_env") or ""), "")).strip().lower()
        observation_mode = bool(check.get("require_observation_record") or check.get("perform_real_image_request"))
        pattern = str(
            check.get("evidence_glob") or ("**/state/vlm-observation.json" if observation_mode else "**/state/provider-correlation.json*")
        )
        paths = sorted(self.profile_root.glob(pattern))
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
                payloads = (
                    [json.loads(line) for line in text.splitlines() if line.strip()] if path.suffix == ".jsonl" else [json.loads(text)]
                )
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{path.name}: {type(exc).__name__}")
                continue
            for payload in payloads:
                if not isinstance(payload, dict):
                    errors.append(f"{path.name}: evidence is not an object")
                    continue
                rows.append(payload)
        if not rows:
            suffix = "; " + ", ".join(errors) if errors else ""
            return False, f"real provider evidence is missing for {expected or 'configured provider'}{suffix}", ""
        if observation_mode:
            matched = [
                row
                for row in rows
                if not expected or str(row.get("provider") or row.get("observation_provider") or "").strip().lower() == expected
            ]
            ok = bool(matched) and all(
                bool(row.get("observation_id") or row.get("result_observation_set_hash") or row.get("artifact_published"))
                for row in matched
            )
            models = [str(row.get("observation_model") or row.get("model") or "").strip() for row in matched]
            expected_model = self.environment.get(str(check.get("model_env") or ""), "").strip()
            ok = ok and all(models) and (not expected_model or all(model == expected_model for model in models))
        else:
            matched = [
                row
                for row in rows
                if str(row.get("usage_provider") or "").strip().lower() == expected
                and str(row.get("configured_agent") or "").strip().lower() == expected
            ]
            # 同じprofileではchat、embedding、VLM等の実provider記録が共存する。
            # このcapabilityが担当するproviderの相関行だけを評価し、他providerの
            # 正規なoperationを誤って不一致扱いしない。
            ok = bool(matched)
            if ok and check.get("require_nonzero_usage", True):
                try:
                    ok = all(int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0) > 0 for row in matched)
                except (TypeError, ValueError):
                    ok = False
            if ok:
                models = [str(row.get("usage_model") or row.get("model") or "").strip() for row in matched]
                ok = all(models)
        evidence = json.dumps(
            {
                "profile": self.profile_name,
                "expected_provider": expected,
                "matching_records": len(matched),
                "records": rows,
                "suite_passed": suite_passed,
            },
            ensure_ascii=False,
            indent=2,
        )
        detail = f"real provider correlation records={len(matched)} provider={expected or '<unset>'}"
        return ok, detail, evidence

    def _write_requirement_evidence(
        self,
        requirement: dict[str, Any],
        result: CapabilityResult,
        evidence: str,
    ) -> dict[str, Any]:
        requirement_id = str(requirement.get("id") or "unnamed")
        canonical = capability_evidence_path(requirement_id)
        declared = Path(str(requirement.get("evidence") or ""))
        if declared != canonical:
            raise ValueError(f"capability {requirement_id!r} evidence path is not canonical")
        path = self.profile_root / canonical
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            raise OSError(f"capability evidence path is a symlink: {canonical}")
        self.redactor.write_json(
            path,
            {
                "capability": result.as_dict(),
                "canonical_evidence": str(canonical),
                "response": evidence or result.detail,
            },
        )
        metadata = path.lstat()
        if path.is_symlink() or not path.is_file() or metadata.st_size == 0:
            raise OSError(f"capability evidence was not written safely: {canonical}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"capability evidence is not an object: {canonical}")
        capability = payload.get("capability")
        if not isinstance(capability, dict):
            raise ValueError(f"capability evidence has no result object: {canonical}")
        if (
            capability.get("id") != result.requirement_id
            or capability.get("status") != result.status
            or payload.get("canonical_evidence") != str(canonical)
            or payload.get("response") in {None, ""}
        ):
            raise ValueError(f"capability evidence does not match its probe result: {canonical}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "declared_path": str(canonical),
            "exists": True,
            "validated": True,
            "size": metadata.st_size,
            "sha256": digest,
        }

    def _write_index(self) -> None:
        requirements = []
        for item in self.results:
            row = item.as_dict()
            row["evidence"] = self._evidence_metadata.get(
                item.requirement_id,
                {
                    "declared_path": str(capability_evidence_path(item.requirement_id)),
                    "exists": False,
                    "validated": False,
                },
            )
            requirements.append(row)
        self.redactor.write_json(
            self.profile_root / "services" / "capabilities.json",
            {"suite": self.suite, "requirements": requirements},
        )
