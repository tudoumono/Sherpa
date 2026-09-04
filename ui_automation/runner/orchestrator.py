"""全profileを順次実行し、失敗後も次へ進めて最後に集計する。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ui_automation.runner.artifacts import (
    SecretRedactor,
    audit_existing_artifact_runs,
    collect_secret_values,
    file_hash_rows,
    harden_artifact_permissions,
    ingest_secret_registry,
    initialize_run_root,
    mark_run_complete,
    prune_runs,
    run_artifact_security_self_check,
    sanitized_environment,
    write_private_text_atomic,
    write_file_hashes,
)
from ui_automation.runner.capabilities import CapabilityChecker
from ui_automation.runner.config import (
    load_capabilities,
    load_profiles,
    repository_root,
    validate_capability_profile_scopes,
)
from ui_automation.runner.filesystem_safety import (
    assert_no_mount_targets,
    assert_no_unsafe_hardlinks,
    chmod_path_no_follow,
    rmtree_no_follow,
)
from ui_automation.runner.manifest_validation import (
    finalize_environment_coverage,
    finalize_feature_coverage,
    validate_coverage,
    validate_environment_manifest,
)
from ui_automation.runner.models import EnvProfile, ProfileResult
from ui_automation.runner.pytest_executor import marker_for, run_pytest_process_cleanup_self_check, run_pytest_profile
from ui_automation.runner.reports import collect_usage_summary, write_summary
from ui_automation.stack import (
    IsolatedStack,
    StackFailure,
    cleanup_failed_isolation,
    create_isolation,
    recover_stale_run_runtimes,
)
from ui_automation.stack.isolation import (
    SharedPlaywrightCache,
    cleanup_shared_playwright_cache,
    initial_shared_playwright_cache_evidence,
    inspect_shared_playwright_cache,
    prepare_shared_playwright_cache,
    recover_stale_shared_playwright_caches,
    run_runner_environment_boundary_self_check,
)
from ui_automation.support.artifacts import run_browser_failure_policy_self_check


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


_GENERATED_REJECTION_SIGNATURES = {
    "PATH": re.compile(
        r"(?:/usr/bin/env|\benv):.*bash.*(?:No such file|not found|見つかりません|ディレクトリ)",
        re.IGNORECASE,
    ),
    "PGUSER": re.compile(
        r"(?:スキーマ初期化に失敗|(?:role|user name|authentication).*(?:does not exist|failed|specified))",
        re.IGNORECASE,
    ),
    "SHERPA_HOST": re.compile(r"ERROR:\s*\[Errno\s+-2\]\s*Name or service not known", re.IGNORECASE),
}


def _rejection_windows(content: str, patterns: tuple[str, ...]) -> list[dict[str, Any]]:
    """Return bounded log records where every declared regex matches together."""

    lines = content.splitlines() or [content]
    candidates: list[dict[str, Any]] = []
    for start in range(len(lines)):
        for width in range(1, min(3, len(lines) - start) + 1):
            window = "\n".join(lines[start : start + width])
            if patterns and all(re.search(pattern, window, re.IGNORECASE) for pattern in patterns):
                candidates.append({"start_line": start + 1, "end_line": start + width, "text": window})
                break
    return candidates


def _private_file_bytes(path: Path, *, max_size: int = 16 * 1024 * 1024) -> tuple[bytes, os.stat_result]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.geteuid() or metadata.st_size > max_size:
        raise ValueError("evidence is not a runner-owned single-link regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or opened.st_size > max_size
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise ValueError("evidence changed or failed opened-inode validation")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise ValueError("evidence exceeds the size limit")
            chunks.append(chunk)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("evidence changed while it was read")
        return b"".join(chunks), opened
    finally:
        os.close(descriptor)


def _private_json_value(path: Path) -> Any:
    payload, _ = _private_file_bytes(path, max_size=1024 * 1024)
    return json.loads(payload.decode("utf-8"))


def _private_json_object(path: Path) -> dict[str, Any]:
    loaded = _private_json_value(path)
    if not isinstance(loaded, dict):
        raise ValueError("evidence is not a JSON object")
    return loaded


def run_expected_rejection_policy_self_check() -> dict[str, Any]:
    checks = {
        "same_record_required": not _rejection_windows("workers\nnoise\nnoise\n2", ("workers", "2")),
        "path_signature_accepts_exact_failure": bool(
            _GENERATED_REJECTION_SIGNATURES["PATH"].search("/usr/bin/env: 'bash': No such file or directory")
        ),
        "path_signature_rejects_generic_bash": not bool(_GENERATED_REJECTION_SIGNATURES["PATH"].search("unrelated bash diagnostic")),
        "host_signature_accepts_exact_failure": bool(
            _GENERATED_REJECTION_SIGNATURES["SHERPA_HOST"].search("ERROR: [Errno -2] Name or service not known")
        ),
        "host_signature_rejects_generic_address": not bool(_GENERATED_REJECTION_SIGNATURES["SHERPA_HOST"].search("address already in use")),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise AssertionError("expected rejection policy self-check failed: " + ", ".join(failed))
    return {"status": "PASS", "checks": checks, "check_count": len(checks)}


@dataclass(frozen=True)
class RunOptions:
    suite: str
    env_file: Path | None = None
    profiles: tuple[str, ...] = ()
    headed: bool = False
    stack_timeout: int = 240
    case_timeout_ms: int = 120_000
    retention: int = 10


class UiAutomationRunner:
    def __init__(self, options: RunOptions) -> None:
        self.options = options
        self.repository = repository_root()
        self.ui_root = self.repository / "ui_automation"
        self.config_root = self.ui_root / "config"
        self.artifacts_root = self.ui_root / "artifacts"
        self.run_id = new_run_id()
        self.run_root = self.artifacts_root / self.run_id
        self.started_at = utc_now()
        self.results: list[ProfileResult] = []
        self.global_failures: list[str] = []
        self._secret_values: list[str] = []
        self.feature_coverage: dict[str, Any] = {}
        self._case_registry: list[str] = []
        self._profile_case_plans: dict[str, list[str]] = {}
        self.environment_coverage: dict[str, Any] = {}
        self.usage_summary: dict[str, Any] = {}
        self.source_env_file: Path | None = None
        self._shared_playwright_cache: SharedPlaywrightCache | None = None
        self._shared_playwright_cache_preparation_failed = False
        self._shutdown_requested = False
        self._shutdown_signal: int | None = None

    def request_shutdown(self, signum: int) -> None:
        """Record a signal without throwing through an active cleanup block."""

        self._shutdown_requested = True
        self._shutdown_signal = int(signum)

    def shutdown_requested(self) -> bool:
        return self._shutdown_requested

    def _stop_requested(self, result: ProfileResult) -> bool:
        if not self._shutdown_requested:
            return False
        result.fail(
            f"controlled shutdown requested by signal {self._shutdown_signal or 0}",
            stage="interrupted",
        )
        return True

    def run(self) -> int:
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        initialize_run_root(self.run_root, run_id=self.run_id, started_at=self.started_at)
        legacy_redactor = SecretRedactor(collect_secret_values(dict(os.environ)))
        try:
            security_self_check = run_artifact_security_self_check(self.run_root, legacy_redactor)
        except (AssertionError, OSError, ValueError, json.JSONDecodeError) as exc:
            security_self_check = {
                "status": "FAIL",
                "error_type": type(exc).__name__,
                "error": legacy_redactor.redact_text(str(exc)),
            }
            self.global_failures.append("artifact redaction/attestation startup self-check failed")
        legacy_redactor.write_json(self.run_root / "security" / "artifact-security-self-check.json", security_self_check)
        try:
            browser_failure_self_check = run_browser_failure_policy_self_check()
        except (AssertionError, TypeError, ValueError) as exc:
            browser_failure_self_check = {
                "status": "FAIL",
                "error_type": type(exc).__name__,
                "error": legacy_redactor.redact_text(str(exc)),
            }
            self.global_failures.append("browser request-failure/role policy startup self-check failed")
        legacy_redactor.write_json(
            self.run_root / "security" / "browser-failure-policy-self-check.json",
            browser_failure_self_check,
        )
        try:
            rejection_self_check = run_expected_rejection_policy_self_check()
        except (AssertionError, TypeError, ValueError) as exc:
            rejection_self_check = {
                "status": "FAIL",
                "error_type": type(exc).__name__,
                "error": legacy_redactor.redact_text(str(exc)),
            }
            self.global_failures.append("expected startup-rejection policy self-check failed")
        legacy_redactor.write_json(
            self.run_root / "security" / "expected-rejection-policy-self-check.json",
            rejection_self_check,
        )
        try:
            pytest_cleanup_self_check = run_pytest_process_cleanup_self_check()
        except (AssertionError, OSError, RuntimeError, ValueError) as exc:
            pytest_cleanup_self_check = {
                "status": "FAIL",
                "error_type": type(exc).__name__,
                "error": legacy_redactor.redact_text(str(exc)),
            }
            self.global_failures.append("pytest session-cleanup startup self-check failed")
        legacy_redactor.write_json(
            self.run_root / "security" / "pytest-process-cleanup-self-check.json",
            pytest_cleanup_self_check,
        )
        try:
            runner_boundary_self_check = run_runner_environment_boundary_self_check()
        except (AssertionError, OSError, RuntimeError, ValueError) as exc:
            runner_boundary_self_check = {
                "status": "FAIL",
                "error_type": type(exc).__name__,
                "error": legacy_redactor.redact_text(str(exc)),
            }
            self.global_failures.append("runner/product execution-environment boundary self-check failed")
        legacy_redactor.write_json(
            self.run_root / "security" / "runner-environment-boundary-self-check.json",
            runner_boundary_self_check,
        )
        legacy_audit = audit_existing_artifact_runs(
            self.artifacts_root,
            current_run_id=self.run_id,
            redactor=legacy_redactor,
        )
        legacy_redactor.write_json(self.run_root / "security" / "legacy-artifact-audit.json", legacy_audit)
        if legacy_audit.get("status") != "PASS":
            self.global_failures.append("legacy artifact migration quarantined or refused unsafe pre-attestation evidence")
        try:
            stale_runtime_recovery = recover_stale_run_runtimes(
                repository=self.repository,
                artifacts_root=self.artifacts_root,
                current_run_id=self.run_id,
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            stale_runtime_recovery = {
                "status": "FAIL",
                "recovered": 0,
                "secret_scrubbed": 0,
                "runtimes": [],
                "errors": [f"stale runtime recovery raised: {type(exc).__name__}: {exc}"],
                "raw_secret_values_recorded": False,
            }
        legacy_redactor.write_json(
            self.run_root / "state" / "stale-runtime-recovery.json",
            stale_runtime_recovery,
        )
        if stale_runtime_recovery.get("status") != "PASS":
            self.global_failures.append("stale profile runtime recovery did not prove safe cleanup")
        self._recover_stale_shared_playwright_caches()
        profiles: list[EnvProfile] = []
        capabilities: list[dict[str, Any]] = []
        try:
            self.source_env_file = self._resolve_source_env_file()
            profiles = load_profiles(self.config_root, self.options.suite)
            capabilities = load_capabilities(self.config_root)
            scope_profiles: dict[str, EnvProfile] = {}
            for suite in ("full", "smoke", "chat", "env"):
                for candidate in load_profiles(self.config_root, suite):
                    scope_profiles.setdefault(candidate.name, candidate)
            validate_capability_profile_scopes(
                capabilities,
                list(scope_profiles.values()),
            )
            profiles = self._filter_profiles(profiles)
            self._prepare_profile_case_plans(profiles)
        except Exception as exc:
            self.global_failures.append(f"configuration load failed: {type(exc).__name__}: {exc}")

        self._validate_manifests()
        self._union_registered_cases_into_plans(profiles)
        try:
            if profiles:
                try:
                    self._prepare_shared_playwright_cache()
                except (OSError, ValueError, RuntimeError) as exc:
                    self.global_failures.append(f"shared Playwright cache preparation failed: {type(exc).__name__}: {exc}")
            for profile in profiles:
                if self._shutdown_requested:
                    self.global_failures.append("execution interrupted by user; remaining profiles were not started")
                    break
                try:
                    self.results.append(self._run_profile(profile, capabilities))
                except KeyboardInterrupt:
                    self.global_failures.append("execution interrupted by user")
                    break
                except Exception as exc:
                    result = ProfileResult(profile=profile.name, suite=self.options.suite, started_at=utc_now())
                    result.fail(f"runner internal error: {type(exc).__name__}: {exc}", stage="runner")
                    result.finished_at = utc_now()
                    self.results.append(result)
                if self._shutdown_requested:
                    self.global_failures.append("execution interrupted by user; remaining profiles were not started")
                    break
        finally:
            self._finalize_shared_playwright_cache()

        if not profiles:
            self.global_failures.append("no executable profiles were selected")
        summary: dict[str, Any] = {}
        converged = False
        for _ in range(4):
            summary = self._synchronize_reports()
            security_ok, incidents = self._final_security_pass()
            if security_ok and not incidents:
                converged = True
                break
        if not converged:
            self.global_failures.append("final artifact security scan did not converge after quarantining generated files")
            summary = self._synchronize_reports()
            redactor = SecretRedactor(self._secret_values)
            residual = redactor.scan_leaks(self.run_root)
            if residual:
                redactor.quarantine_leaks(self.run_root)
                summary["status"] = "FAIL"
        try:
            mark_run_complete(
                self.run_root,
                run_id=self.run_id,
                finished_at=utc_now(),
            )
            removed = prune_runs(
                self.artifacts_root,
                keep=self.options.retention,
                protected_run_id=self.run_id,
            )
            SecretRedactor(self._secret_values).write_json(
                self.run_root / "retention.json",
                {"keep": self.options.retention, "removed": removed},
            )
        except Exception as exc:
            self.global_failures.append(f"retention cleanup failed: {type(exc).__name__}: {exc}")
        # retention証跡も含めて再集計・再走査する。markerは実行終了状態を表すため
        # completedのまま、ここで見つかった問題は最終statusへ反映する。
        for _ in range(4):
            summary = self._synchronize_reports()
            security_ok, incidents = self._final_security_pass()
            if security_ok and not incidents:
                break
        else:
            self.global_failures.append("post-retention artifact security scan did not converge")
            summary = self._synchronize_reports()
        return 0 if summary["status"] == "PASS" else 1

    def _synchronize_reports(self) -> dict[str, Any]:
        self._ensure_unexecuted_case_results()
        self._refresh_profile_results()
        self._finalize_feature_coverage()
        self._finalize_environment_coverage()
        self._finalize_usage_summary()
        self._write_coverage_reports()
        return self._write_reports()

    def _filter_profiles(self, profiles: list[EnvProfile]) -> list[EnvProfile]:
        if not self.options.profiles:
            return profiles
        requested = set(self.options.profiles)
        available = {profile.name for profile in profiles}
        missing = sorted(requested - available)
        if missing:
            self.global_failures.append("requested profiles are unavailable for this suite: " + ", ".join(missing))
        return [profile for profile in profiles if profile.name in requested]

    def _prepare_profile_case_plans(self, profiles: list[EnvProfile]) -> None:
        """Resolve the exact pytest selection once, before any service can fail.

        Explicit nodeids are already a complete contract.  Marker-only profiles
        are collected once per unique marker/target combination so a browser or
        session fixture failure cannot erase which cases that profile owed.
        Profiles whose success condition is a fail-closed startup rejection do
        not owe a browser case.
        """

        cache: dict[tuple[str, str], list[str]] = {}
        plan_rows: list[dict[str, Any]] = []
        cases_root = self.ui_root / "cases"
        for profile in profiles:
            effective_suite = self._effective_suite(profile)
            marker = marker_for(profile, effective_suite)
            if profile.expect_startup_failure:
                nodeids: list[str] = []
                source = "expected-startup-rejection"
            elif profile.case_nodeids:
                nodeids = list(profile.case_nodeids)
                source = "explicit-nodeids"
            else:
                cache_key = (effective_suite, marker)
                if cache_key not in cache:
                    command = [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-c",
                        str(self.ui_root / "pytest.ini"),
                        str(cases_root),
                        "--collect-only",
                        "-q",
                        "-m",
                        marker,
                    ]
                    try:
                        completed = subprocess.run(
                            command,
                            cwd=self.repository,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            timeout=60,
                            check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        self.global_failures.append(f"profile case-plan collection failed for marker {marker!r}: {type(exc).__name__}")
                        cache[cache_key] = []
                    else:
                        collected = []
                        for raw in completed.stdout.splitlines():
                            candidate = raw.strip().replace("\\", "/")
                            if candidate.startswith("ui_automation/cases/"):
                                candidate = candidate.removeprefix("ui_automation/")
                            if candidate.startswith("cases/") and "::" in candidate:
                                collected.append(candidate)
                        cache[cache_key] = sorted(set(collected))
                        if completed.returncode != 0 or not cache[cache_key]:
                            self.global_failures.append(
                                f"profile case-plan collection returned {completed.returncode} and "
                                f"{len(cache[cache_key])} case(s) for marker {marker!r}"
                            )
                nodeids = list(cache[cache_key])
                source = "pytest-marker-collection"
            self._profile_case_plans[profile.name] = nodeids
            plan_rows.append(
                {
                    "profile": profile.name,
                    "effective_suite": effective_suite,
                    "marker": marker,
                    "source": source,
                    "expected_startup_rejection": profile.expect_startup_failure,
                    "nodeids": nodeids,
                    "count": len(nodeids),
                }
            )
        SecretRedactor(collect_secret_values(dict(os.environ))).write_json(
            self.run_root / "state" / "profile-case-plans.json",
            {"profiles": plan_rows, "profile_count": len(plan_rows)},
        )

    def _validate_manifests(self) -> None:
        try:
            coverage = validate_coverage(self.repository, self.config_root)
            self._case_registry = [
                str(row.get("nodeid")) for row in coverage.get("case_coverage") or () if isinstance(row, dict) and row.get("nodeid")
            ]
            if self.options.suite == "full":
                self.feature_coverage = coverage
                self.global_failures.extend(self.feature_coverage.get("errors") or ())
            if self.options.suite in {"full", "env"}:
                self.environment_coverage = validate_environment_manifest(self.repository, self.config_root)
                self.global_failures.extend(self.environment_coverage.get("errors") or ())
        except Exception as exc:
            self.global_failures.append(f"manifest validation failed: {type(exc).__name__}: {exc}")

    def _union_registered_cases_into_plans(self, profiles: list[EnvProfile]) -> None:
        """Collectionが壊れてもcoverage登録caseを1つの実行planへ必ず残す。"""

        if self.options.suite != "full" or not self._case_registry:
            return
        registered = sorted(set(self._case_registry))
        already_planned = {nodeid for nodeids in self._profile_case_plans.values() for nodeid in nodeids}
        missing = sorted(set(registered) - already_planned)
        if not missing:
            return
        eligible = [profile for profile in profiles if not profile.expect_startup_failure]
        owner = next((profile for profile in eligible if profile.name == "baseline-full"), None)
        owner = owner or (eligible[0] if eligible else (profiles[0] if profiles else None))
        if owner is None:
            owner_name = "collection-failure"
            result = ProfileResult(
                profile=owner_name,
                suite="full",
                started_at=utc_now(),
                finished_at=utc_now(),
                duration_seconds=0,
            )
            result.fail("configuration/collection failed before a full profile could be selected", stage="collection")
            self.results.append(result)
        else:
            owner_name = owner.name
        nodeids = sorted(set(self._profile_case_plans.get(owner_name, ())) | set(missing))
        self._profile_case_plans[owner_name] = nodeids

        plan_path = self.run_root / "state" / "profile-case-plans.json"
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"profiles": [], "profile_count": 0}
        rows = payload.get("profiles") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = []
        owner_row = next((row for row in rows if isinstance(row, dict) and row.get("profile") == owner_name), None)
        if owner_row is None:
            owner_row = {
                "profile": owner_name,
                "effective_suite": "full",
                "marker": "full",
                "source": "coverage-registry-fallback",
                "expected_startup_rejection": False,
            }
            rows.append(owner_row)
        else:
            owner_row["source"] = str(owner_row.get("source") or "unknown") + "+coverage-registry-fallback"
        owner_row["nodeids"] = nodeids
        owner_row["count"] = len(nodeids)
        payload = {
            **(payload if isinstance(payload, dict) else {}),
            "profiles": rows,
            "profile_count": len(rows),
            "coverage_registry_union": {
                "owner_profile": owner_name,
                "registered_count": len(registered),
                "fallback_count": len(missing),
                "fallback_nodeids": missing,
            },
        }
        SecretRedactor(collect_secret_values(dict(os.environ))).write_json(plan_path, payload)

    def _prepare_shared_playwright_cache(self) -> None:
        redactor = SecretRedactor(collect_secret_values(dict(os.environ)))
        path = self.run_root / "state" / "playwright-browser-cache-run-initial.json"
        try:
            cache = prepare_shared_playwright_cache(
                repository=self.repository,
                run_id=self.run_id,
                source_env_file=self.source_env_file,
            )
            self._shared_playwright_cache = cache
            if cache is None:
                evidence: dict[str, Any] = {
                    "phase": "run-initial",
                    "status": "PASS",
                    "available": False,
                    "copy_count": 0,
                    "reason": "no external Playwright browser cache was configured or present",
                }
            else:
                evidence = initial_shared_playwright_cache_evidence(cache)
            redactor.write_json(path, evidence)
        except (OSError, ValueError, RuntimeError) as exc:
            self._shared_playwright_cache_preparation_failed = True
            redactor.write_json(
                path,
                {
                    "phase": "run-initial",
                    "status": "FAIL",
                    "available": False,
                    "copy_count": 0,
                    "error_type": type(exc).__name__,
                    "error": redactor.redact_text(str(exc)),
                },
            )
            raise

    def _recover_stale_shared_playwright_caches(self) -> None:
        redactor = SecretRedactor(collect_secret_values(dict(os.environ)))
        path = self.run_root / "state" / "playwright-browser-cache-stale-recovery.json"
        try:
            evidence = recover_stale_shared_playwright_caches()
            redactor.write_json(path, evidence)
            for error in evidence.get("errors", []):
                if not isinstance(error, dict):
                    continue
                error_type = str(error.get("error_type", "unknown"))
                reason = redactor.redact_text(str(error.get("reason", "unknown")))
                self.global_failures.append(f"stale shared Playwright cache recovery refused: {error_type}: {reason}")
        except (OSError, ValueError, RuntimeError) as exc:
            self.global_failures.append(f"stale shared Playwright cache recovery failed: {type(exc).__name__}: {exc}")
            redactor.write_json(
                path,
                {
                    "phase": "run-start-stale-recovery",
                    "status": "FAIL",
                    "scanned_count": 0,
                    "removed_count": 0,
                    "active_preserved_count": 0,
                    "error_type": type(exc).__name__,
                    "error": redactor.redact_text(str(exc)),
                },
            )

    def _finalize_shared_playwright_cache(self) -> None:
        redactor = SecretRedactor(self._secret_values or collect_secret_values(dict(os.environ)))
        final_path = self.run_root / "state" / "playwright-browser-cache-run-final.json"
        cleanup_path = self.run_root / "state" / "playwright-browser-cache-cleanup.json"
        cache = self._shared_playwright_cache
        if cache is None:
            status = "FAIL" if self._shared_playwright_cache_preparation_failed else "PASS"
            redactor.write_json(
                final_path,
                {
                    "phase": "run-final",
                    "status": status,
                    "available": False,
                    "full_content_hash_performed": False,
                    "reason": (
                        "shared cache preparation failed"
                        if self._shared_playwright_cache_preparation_failed
                        else "no external Playwright browser cache was configured or present"
                    ),
                },
            )
            redactor.write_json(
                cleanup_path,
                {
                    "status": status,
                    "attempted": False,
                    "removed": False,
                    "errors": (["shared cache preparation failed before an owned cache was returned"] if status == "FAIL" else []),
                },
            )
            return

        try:
            evidence = inspect_shared_playwright_cache(cache, full_content=True, phase="run-final")
            redactor.write_json(final_path, evidence)
            if evidence.get("status") != "PASS":
                self.global_failures.append("shared Playwright cache or its external source changed during the run")
        except (OSError, ValueError, RuntimeError) as exc:
            self.global_failures.append(f"shared Playwright cache final integrity failed: {type(exc).__name__}: {exc}")
            redactor.write_json(
                final_path,
                {
                    "phase": "run-final",
                    "status": "FAIL",
                    "available": True,
                    "full_content_hash_performed": False,
                    "error_type": type(exc).__name__,
                    "error": redactor.redact_text(str(exc)),
                },
            )

        cleanup_errors = cleanup_shared_playwright_cache(cache)
        removed = not cache.root.exists()
        cleanup_evidence_errors = list(cleanup_errors)
        if not removed and not cleanup_evidence_errors:
            cleanup_evidence_errors.append("shared Playwright cache root remains after cleanup")
        if cleanup_evidence_errors:
            for message in cleanup_evidence_errors:
                self.global_failures.append(redactor.redact_text(message))
        redactor.write_json(
            cleanup_path,
            {
                "status": "PASS" if not cleanup_evidence_errors else "FAIL",
                "attempted": True,
                "removed": removed,
                "root_path_sha256": hashlib.sha256(str(cache.root).encode()).hexdigest(),
                "copy_count": cache.copy_count,
                "errors": [redactor.redact_text(message) for message in cleanup_evidence_errors],
                "ownership_marker_required": True,
            },
        )

    def _effective_suite(self, profile: EnvProfile) -> str:
        if self.options.suite == "full" and "full" not in profile.suites and "env" in profile.suites:
            return "env"
        return self.options.suite

    def _run_profile(self, profile: EnvProfile, requirements: list[dict[str, Any]]) -> ProfileResult:
        started_clock = time.monotonic()
        result = ProfileResult(
            profile=profile.name,
            suite=self._effective_suite(profile),
            started_at=utc_now(),
            stage="isolation",
        )
        if profile.generated_scenario is not None:
            result.environment_scenario = profile.generated_scenario.as_dict()
        elif profile.pairwise_scenario is not None:
            result.environment_scenario = profile.pairwise_scenario.as_dict()
        stack: IsolatedStack | None = None
        isolation = None
        preflight_listener: socket.socket | None = None
        artifact_trust_failed = False
        redactor = SecretRedactor(collect_secret_values(dict(os.environ)))
        try:
            isolation = create_isolation(
                repository=self.repository,
                run_root=self.run_root,
                run_id=self.run_id,
                profile=profile,
                source_env_file=self.source_env_file,
                shared_playwright_cache=self._shared_playwright_cache,
                headed=self.options.headed,
                case_timeout_ms=self.options.case_timeout_ms,
            )
            effective_suite = self._effective_suite(profile)
            ocr_workload = self._requires_ocr_worker(profile, isolation.environment)
            if ocr_workload:
                if not (profile.generated_scenario is not None and profile.generated_scenario.variable == "SHERPA_OCR_ENABLED"):
                    isolation.environment["SHERPA_OCR_ENABLED"] = "1"
                isolation.environment.setdefault("SHERPA_ARMS", "ooxml,pdf_text,vision")
                isolation.environment.setdefault("SHERPA_LEGACY_BACKEND", "libreoffice")
            values = collect_secret_values(isolation.environment)
            values.extend(collect_secret_values({key: value or "" for key, value in isolation.restart_environment.items()}))
            self._secret_values.extend(values)
            redactor = SecretRedactor(values)
            if profile.pairwise_scenario is not None:
                result.environment_scenario = redactor.redact_value(profile.pairwise_scenario.as_dict())
            else:
                result.environment_scenario = redactor.redact_value(isolation.scenario_contract)
            stack = IsolatedStack(
                isolation,
                redactor,
                timeout_seconds=self.options.stack_timeout,
                enable_ocr=ocr_workload,
            )
            if self._stop_requested(result):
                return result
            expected_path = isolation.paths.profile_root / "state" / "effective-environment.json"
            isolation.environment["SHERPA_UI_EXPECTED_ENV_JSON"] = str(expected_path)
            isolation.environment["SHERPA_UI_CAPABILITIES_JSON"] = str(isolation.paths.profile_root / "services" / "capabilities.json")
            pairwise_failures = self._write_effective_environment(isolation, expected_path, redactor)
            self._verify_fixture_copy(isolation, redactor, phase="initial")
            self._verify_ocr_cache_copy(isolation, redactor, phase="initial")
            self._verify_playwright_browser_cache(isolation, redactor, phase="initial")

            if pairwise_failures:
                result.fail(
                    "pairwise effective environment mismatch: " + ", ".join(pairwise_failures),
                    stage="pairwise_contract",
                )
                return result

            if isolation.precondition_failures:
                redactor.write_json(
                    isolation.paths.profile_root / "state" / "precondition-failure.json",
                    {
                        "status": "FAIL",
                        "profile": profile.name,
                        "missing_environment": list(isolation.precondition_failures),
                    },
                )
                result.fail(
                    "required real values are unavailable: " + ", ".join(isolation.precondition_failures),
                    stage="precondition",
                )
                return result

            result.stage = "docker_identity"
            stack.attest_local_docker_daemon()
            if profile.codex_auth_mode == "prepare":
                stack.prepare_codex_auth()
            else:
                redactor.write_json(
                    isolation.paths.profile_root / "state" / "codex-auth-intentionally-unconfigured.json",
                    {
                        "mode": profile.codex_auth_mode,
                        "codex_home_is_run_owned": str(isolation.paths.runtime_root) in isolation.environment.get("CODEX_HOME", ""),
                        "auth_file_present": (Path(isolation.environment["CODEX_HOME"]) / "auth.json").exists(),
                    },
                )
            checker = CapabilityChecker(
                requirements=requirements,
                suite=effective_suite,
                environment=isolation.environment,
                runner_environment=isolation.runner_environment,
                profile_name=profile.name,
                repository=self.repository,
                runtime_root=isolation.paths.runtime_root,
                profile_root=isolation.paths.profile_root,
                redactor=redactor,
                compose_probe=lambda service, compose_profile, allow_completed: stack.service_probe(
                    service,
                    compose_profile,
                    allow_completed=allow_completed,
                ),
            )
            preflight = checker.check_phase("preflight")
            missing = checker.failures(preflight)
            if missing:
                names = [item.requirement_id for item in missing]
                result.fail("required capabilities are missing: " + ", ".join(names), stage="capabilities")
                blockers = [name for name in names if self._capability_blocks_execution(name)]
                if blockers:
                    result.fail("execution cannot continue without: " + ", ".join(blockers), stage="capabilities")
                    return result
            if self._stop_requested(result):
                return result

            preflight_listener = self._start_fixed_preflight_fixture(profile, isolation, redactor)
            result.stage = "port_check"
            stack.check_ports()
            result.stage = "stores"
            stack.start_stores()
            identity_failures = stack.verify_store_identities()
            post_stack = checker.check_phase("post_stack")
            missing = checker.failures(post_stack)
            if missing or identity_failures:
                names = [item.requirement_id for item in missing]
                detail = ("required store capabilities failed: " + ", ".join(names)) if names else ""
                result.fail("; ".join([item for item in [detail, *identity_failures] if item]), stage="store_identity")
                return result
            if self._stop_requested(result):
                return result

            result.stage = "application"
            stack.start_app()
            post_app = checker.check_phase("post_app")
            missing = checker.failures(post_app)
            if missing:
                result.fail(
                    "required application capabilities failed: " + ", ".join(item.requirement_id for item in missing),
                    stage="app_capability",
                )
                return result
            if self._stop_requested(result):
                return result
            if profile.expect_startup_failure:
                result.fail("application started although this profile requires a fail-closed startup", stage="expected_rejection")
                return result

            result.stage = "restart_verification"
            if profile.restart == "app":
                stack.restart_app()
            elif profile.restart == "stack":
                stack.restart_stack()
                restart_identity_failures = stack.verify_store_identities()
                if restart_identity_failures:
                    result.fail("; ".join(restart_identity_failures), stage="restart_identity")
                    return result
            stack.start_control_server()
            if self._stop_requested(result):
                return result

            result.stage = "pytest"
            outcome = run_pytest_profile(
                repository=self.repository,
                profile=profile,
                effective_suite=effective_suite,
                environment=isolation.environment,
                trusted_python_executable=isolation.trusted_python_executable,
                profile_root=isolation.paths.profile_root,
                redactor=redactor,
                cancel_requested=self.shutdown_requested,
            )
            result.tests = outcome.counts
            if outcome.registry_errors:
                artifact_trust_failed = True
                result.fail(
                    f"runtime secret registry failed closed with {len(outcome.registry_errors)} error(s)",
                    stage="secret_registry",
                )
            if outcome.process_leak_detected:
                result.fail(
                    "pytest session left descendant processes after its leader exited; they were recovered but the profile remains failed",
                    stage="pytest_process_cleanup",
                )
            if outcome.process_cleanup_errors:
                result.fail(
                    f"pytest session cleanup could not prove an empty owned session ({len(outcome.process_cleanup_errors)} error(s))",
                    stage="pytest_process_cleanup",
                )
            if outcome.exit_code != 0:
                reason = f"pytest exited {outcome.exit_code}"
                if outcome.timed_out:
                    reason += " after suite timeout"
                if outcome.interrupted:
                    reason += " after controlled shutdown request"
                result.fail(reason, stage="pytest")
            else:
                if result.failures:
                    result.status = "FAIL"
                    result.exit_code = 1
                    result.stage = "capabilities"
                else:
                    result.status = "PASS"
                    result.exit_code = 0
                    result.stage = "completed"
            post_tests = checker.check_phase("post_tests", suite_passed=outcome.exit_code == 0)
            missing = checker.failures(post_tests)
            if missing:
                result.fail(
                    "real provider assertions did not pass: " + ", ".join(item.requirement_id for item in missing),
                    stage="provider_evidence",
                )
        except StackFailure as exc:
            self._write_runner_error(profile, isolation, redactor, exc.stage, str(exc))
            if profile.expect_startup_failure and self._matches_expected_rejection(
                profile,
                exc.stage,
                str(exc),
                isolation,
                redactor,
                prior_failure_count=len(result.failures),
            ):
                self._record_expected_rejection(result, stage=exc.stage)
            else:
                result.fail(str(exc), stage=exc.stage)
        except Exception as exc:
            self._write_runner_error(profile, isolation, redactor, result.stage, f"{type(exc).__name__}: {exc}")
            if profile.expect_startup_failure and self._matches_expected_rejection(
                profile,
                result.stage,
                str(exc),
                isolation,
                redactor,
                prior_failure_count=len(result.failures),
            ):
                self._record_expected_rejection(result, stage=result.stage)
            else:
                result.fail(f"{type(exc).__name__}: {exc}", stage=result.stage)
        finally:
            if preflight_listener is not None:
                try:
                    preflight_listener.close()
                except OSError as exc:
                    result.fail(f"run-owned listener cleanup failed: {type(exc).__name__}", stage="cleanup")
            if isolation is not None:
                _, registry_errors = ingest_secret_registry(isolation.secret_registry, redactor)
                self._secret_values.extend(redactor.known_secrets())
                artifact_trust_failed = artifact_trust_failed or bool(registry_errors)
                for message in registry_errors:
                    result.fail(message, stage="secret_registry")
                try:
                    self._verify_fixture_copy(isolation, redactor, phase="final")
                except (OSError, ValueError) as exc:
                    result.fail(f"fixture copy integrity failed: {type(exc).__name__}: {exc}", stage="fixture_integrity")
                try:
                    self._verify_ocr_cache_copy(isolation, redactor, phase="final")
                except (OSError, ValueError) as exc:
                    result.fail(
                        f"OCR model cache copy integrity failed: {type(exc).__name__}: {exc}",
                        stage="fixture_integrity",
                    )
                try:
                    self._verify_playwright_browser_cache(isolation, redactor, phase="final")
                except (OSError, ValueError) as exc:
                    result.fail(
                        f"Playwright browser cache integrity failed: {type(exc).__name__}: {exc}",
                        stage="fixture_integrity",
                    )
            if stack is not None:
                try:
                    cleanup_errors = stack.cleanup()
                except (OSError, RuntimeError, ValueError) as exc:
                    cleanup_errors = [f"stack cleanup raised unexpectedly: {type(exc).__name__}: {exc}"]
                result.cleanup_ok = not cleanup_errors
                for message in cleanup_errors:
                    result.fail(message, stage="cleanup")
            else:
                orphan_cleanup_errors = cleanup_failed_isolation(
                    run_id=self.run_id,
                    profile_name=profile.name,
                    evidence_path=(self.run_root / self._profile_part(profile.name) / "state" / "failed-isolation-cleanup.json"),
                )
                result.cleanup_ok = not orphan_cleanup_errors
                for message in orphan_cleanup_errors:
                    result.fail(message, stage="cleanup")
            if isolation is not None:
                profile_root = isolation.paths.profile_root
            else:
                profile_root = self.run_root / self._profile_part(profile.name)
                profile_root.mkdir(parents=True, exist_ok=True)
            if artifact_trust_failed:
                try:
                    self._discard_untrusted_profile_artifacts(profile_root, redactor)
                except (OSError, RuntimeError, ValueError) as exc:
                    result.fail(
                        f"untrusted profile artifact discard failed: {type(exc).__name__}: {exc}",
                        stage="secret_registry",
                    )
            else:
                try:
                    self._sync_final_service_logs(profile_root)
                except OSError as exc:
                    result.fail(f"final service log synchronization failed: {type(exc).__name__}: {exc}", stage="evidence")
            permission_failures = harden_artifact_permissions(profile_root)
            for message in permission_failures:
                result.fail(message, stage="artifact_permissions")
            if not permission_failures:
                incidents = redactor.quarantine_leaks(profile_root)
                if incidents:
                    result.secret_leaks = incidents
                    result.fail(f"secret-like content was removed from {len(incidents)} artifact file(s)", stage="secret_scan")
                    redactor.write_json(profile_root / "security" / "leak-report.json", {"incidents": incidents})
                redactor.redact_text_files(profile_root)
            result.finished_at = utc_now()
            result.duration_seconds = time.monotonic() - started_clock
            assert_no_mount_targets(profile_root)
            result.artifacts = sorted(
                str(path.relative_to(profile_root)) for path in profile_root.rglob("*") if not path.is_symlink() and path.is_file()
            )
            if "result.json" not in result.artifacts:
                result.artifacts.append("result.json")
            redactor.write_json(profile_root / "result.json", result.as_dict())
        return result

    @staticmethod
    def _start_fixed_preflight_fixture(profile: EnvProfile, isolation, redactor: SecretRedactor) -> socket.socket | None:
        if profile.preflight_fixture is None:
            return None
        if profile.preflight_fixture != "occupy_app_port":
            raise ValueError("unsupported fixed preflight fixture")
        port = int(isolation.environment["SHERPA_PORT"])
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(4)
        except OSError:
            listener.close()
            raise
        redactor.write_json(
            isolation.paths.profile_root / "state" / "preflight-owned-listener.json",
            {
                "fixture": profile.preflight_fixture,
                "pid": os.getpid(),
                "port": port,
                "compose_project": isolation.project_name,
                "owner_relation": "runner-owned-listener-is-not-the-declared-app-process",
                "external_process_stopped": False,
            },
        )
        return listener

    @staticmethod
    def _capability_blocks_execution(requirement_id: str) -> bool:
        return requirement_id in {
            "python-runtime",
            "docker-engine",
            "docker-compose",
            "isolated-compose-project",
            "isolated-data-roots",
            "playwright-python",
            "chromium",
            "no-network-interception",
        }

    @staticmethod
    def _requires_ocr_worker(profile: EnvProfile, environment: dict[str, str]) -> bool:
        if profile.name == "ingestion-real":
            return True
        pairwise = profile.pairwise_scenario
        if pairwise is not None and pairwise.group_id == "ingest-arms-ocr-vlm-legacy":
            return environment.get("SHERPA_OCR_ENABLED", "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        scenario = profile.generated_scenario
        if scenario is None:
            return False
        if scenario.variable == "SHERPA_OCR_ENABLED":
            return environment.get("SHERPA_OCR_ENABLED", "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        if scenario.variable == "SHERPA_OBSERVATION_DIR":
            # この変数の実効果は、OCR/VLM job のDB行だけでなく、指定した
            # observation root配下へ生成された実artifactとのhash相関で確認する。
            # workerを起動しないまま空ディレクトリの存在だけで通してはいけない。
            return True
        return scenario.category == "ingestion" and (
            scenario.variable.startswith("SHERPA_OCR_")
            or scenario.variable.startswith("SHERPA_VLM_")
            or scenario.variable
            in {
                "SHERPA_ARMS",
                "SHERPA_LEGACY_BACKEND",
                "SHERPA_LEGACY_TIMEOUT",
                "SHERPA_OFFICE_COM_TOKEN",
                "SHERPA_OFFICE_COM_URL",
                "SHERPA_OFFICE_TRANSFER_MODE",
                "SHERPA_POWERSHELL_BIN",
                "SHERPA_SOFFICE_BIN",
            }
        )

    @staticmethod
    def _verify_fixture_copy(isolation, redactor: SecretRedactor, *, phase: str) -> None:
        source = file_hash_rows(isolation.world_source_path)
        runtime = file_hash_rows(isolation.world_path)
        assert_no_mount_targets(isolation.world_path)
        evidence = {
            "phase": phase,
            "match": source == runtime,
            "source_file_count": len(source),
            "runtime_file_count": len(runtime),
            "source": source,
            "runtime": runtime,
            "runtime_read_only": all(
                (path.stat().st_mode & 0o222) == 0
                for path in [isolation.world_path, *isolation.world_path.rglob("*")]
                if not path.is_symlink()
            ),
        }
        redactor.write_json(
            isolation.paths.profile_root / "state" / f"fixture-copy-integrity-{phase}.json",
            evidence,
        )
        if phase == "initial":
            write_file_hashes(
                isolation.world_path,
                isolation.paths.profile_root / "state" / "fixture-files.sha256.json",
            )
        if not evidence["match"] or not evidence["runtime_read_only"]:
            raise ValueError("runtime World copy differs from source or is writable")

    @staticmethod
    def _verify_ocr_cache_copy(isolation, redactor: SecretRedactor, *, phase: str) -> None:
        source_path = isolation.ocr_model_cache_source_path
        source = file_hash_rows(source_path) if source_path is not None else []
        runtime = file_hash_rows(isolation.ocr_model_cache_path)
        initial_path = isolation.paths.profile_root / "state" / "ocr-cache-copy-integrity-initial.json"
        evidence: dict[str, Any] = {
            "phase": phase,
            "source_configured": source_path is not None,
            "source_file_count": len(source),
            "runtime_file_count": len(runtime),
            "source": source,
            "runtime": runtime,
        }
        failure: str | None = None
        if phase == "initial":
            evidence["copy_match"] = source == runtime
            if source_path is not None and source != runtime:
                failure = "runtime OCR model cache copy differs from its explicit source"
        else:
            initial_source: list[dict[str, object]] = []
            if initial_path.is_file():
                payload = json.loads(initial_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("source"), list):
                    initial_source = payload["source"]
            evidence["source_unchanged"] = source == initial_source
            if source_path is not None and source != initial_source:
                failure = "explicit OCR model cache source was modified during the run"
        redactor.write_json(
            isolation.paths.profile_root / "state" / f"ocr-cache-copy-integrity-{phase}.json",
            evidence,
        )
        if failure:
            raise ValueError(failure)

    @staticmethod
    def _verify_playwright_browser_cache(
        isolation,
        redactor: SecretRedactor,
        *,
        phase: str,
    ) -> None:
        shared = isolation.shared_playwright_cache
        evidence_path = isolation.paths.profile_root / "state" / f"playwright-browser-cache-{phase}.json"
        if shared is None:
            redactor.write_json(
                evidence_path,
                {
                    "phase": phase,
                    "status": "PASS",
                    "available": False,
                    "inspection": "metadata-and-permissions-only",
                    "content_hashed": False,
                    "browser_profile_and_home_are_run_owned": True,
                },
            )
            return
        try:
            if isolation.playwright_browsers_path != shared.cache_path:
                raise ValueError("profile Playwright path differs from the run-shared cache")
            evidence = inspect_shared_playwright_cache(
                shared,
                full_content=False,
                phase=f"profile-{phase}",
            )
            evidence.update(
                {
                    "content_hashed": False,
                    "shared_across_profiles": True,
                    "outside_profile_runtime": not shared.cache_path.is_relative_to(isolation.paths.runtime_root),
                    "browser_profile_and_home_are_run_owned": True,
                }
            )
            if not evidence["outside_profile_runtime"]:
                evidence["status"] = "FAIL"
            redactor.write_json(evidence_path, evidence)
        except (OSError, ValueError, RuntimeError) as exc:
            redactor.write_json(
                evidence_path,
                {
                    "phase": f"profile-{phase}",
                    "status": "FAIL",
                    "available": True,
                    "inspection": "metadata-and-permissions-only",
                    "content_hashed": False,
                    "error_type": type(exc).__name__,
                    "error": redactor.redact_text(str(exc)),
                },
            )
            raise ValueError("shared Playwright browser cache metadata or ownership verification failed") from exc
        if evidence.get("status") != "PASS" or not evidence.get("outside_profile_runtime"):
            raise ValueError("shared Playwright browser cache changed or became writable during the profile")

    def _write_runner_error(
        self,
        profile: EnvProfile,
        isolation,
        redactor: SecretRedactor,
        stage: str,
        message: str,
    ) -> None:
        root = isolation.paths.profile_root if isolation is not None else self.run_root / self._profile_part(profile.name)
        path = root / "services" / "runner-error.log"
        write_private_text_atomic(path, redactor.redact_text(f"stage={stage}\n{message}\n"))

    def _write_effective_environment(self, isolation, path: Path, redactor: SecretRedactor) -> list[str]:
        environment = isolation.environment
        observables = []
        for value in isolation.profile.observables:
            if isinstance(value, dict):
                observables.append(value)
            else:
                observables.append({"id": str(value), "status": "declared"})
        expected = {
            "auth_disabled": environment.get("SHERPA_AUTH_DISABLED", "").lower() in {"1", "true", "yes"},
            "agent": environment.get("SHERPA_AGENT", ""),
            "exec_event_v2": environment.get("SHERPA_EXEC_EVENT_V2", ""),
            "subagents_enabled": environment.get("SHERPA_SUBAGENTS_ENABLED", ""),
            "stream_pace": environment.get("SHERPA_STREAM_PACE", ""),
            "ports": isolation.ports,
            "declared": list(isolation.profile.expected),
            "process_profile": redactor.redact_value(isolation.profile.env),
            "env_file_profile": redactor.redact_value(isolation.profile.env_file),
        }
        expected.update(redactor.redact_value(isolation.profile.expected_values))
        pairwise_expected = isolation.profile.expected_values.get("pairwise")
        pairwise_applied: dict[str, Any] | None = None
        pairwise_failures: list[str] = []
        pairwise = isolation.profile.pairwise_scenario
        if pairwise is not None:
            expected_factors: list[dict[str, Any]] = []
            applied_factors: list[dict[str, Any]] = []
            evidence_factors: list[dict[str, Any]] = []
            for factor in pairwise.factors:
                expected_raw = isolation.pairwise_expected_values.get(factor.key)
                actual_raw = environment.get(factor.key)
                matched = actual_raw == expected_raw
                if not matched:
                    pairwise_failures.append(f"{factor.key}[{factor.level}]")
                if factor.secret:
                    expected_state = "set" if expected_raw not in {None, ""} else "unset"
                    actual_state = "set" if actual_raw not in {None, ""} else "unset"
                    expected_factor = {
                        "key": factor.key,
                        "level": factor.level,
                        "expectation": expected_state,
                    }
                    applied_factor = {
                        "key": factor.key,
                        "level": factor.level,
                        "expectation": actual_state,
                    }
                    evidence_factor = {
                        "key": factor.key,
                        "level": factor.level,
                        "sensitive": True,
                        "expected_state": expected_state,
                        "actual_state": actual_state,
                        "matched": matched,
                    }
                else:
                    expected_value = redactor.redact_value(expected_raw)
                    actual_value = redactor.redact_value(actual_raw)
                    expected_factor = {
                        "key": factor.key,
                        "level": factor.level,
                        "expectation": "literal",
                        "value": expected_value,
                    }
                    applied_factor = {
                        "key": factor.key,
                        "level": factor.level,
                        "expectation": "literal",
                        "value": actual_value,
                    }
                    evidence_factor = {
                        "key": factor.key,
                        "level": factor.level,
                        "sensitive": False,
                        "expected": expected_value,
                        "actual": actual_value,
                        "matched": matched,
                    }
                expected_factors.append(expected_factor)
                applied_factors.append(applied_factor)
                evidence_factors.append(evidence_factor)
            pairwise_expected = {
                "id": pairwise.group_id,
                "row_id": pairwise.row_id,
                "factors": expected_factors,
                "observables": list(pairwise.observables),
            }
            pairwise_applied = {
                "id": pairwise.group_id,
                "row_id": pairwise.row_id,
                "factors": applied_factors,
                "observables": list(pairwise.observables),
            }
            expected["pairwise"] = pairwise_expected
            redactor.write_json(
                isolation.paths.profile_root / "state" / "pairwise-effective.json",
                {
                    "status": "PASS" if not pairwise_failures else "FAIL",
                    "profile": isolation.profile.name,
                    "group_id": pairwise.group_id,
                    "row_id": pairwise.row_id,
                    "factors": evidence_factors,
                    "all_effective_values_matched": not pairwise_failures,
                },
            )
        elif isinstance(pairwise_expected, dict):
            pairwise_failures.append("legacy pairwise profile has no generated row contract")
        redactor.write_json(
            path,
            {
                "profile": isolation.profile.name,
                "expected": expected,
                "probes": observables,
                "generated_scenario": isolation.scenario_contract or None,
                "pairwise": {"applied": pairwise_applied} if pairwise_applied else None,
                "effective_environment": sanitized_environment(environment, redactor),
                "precedence": "profile process env > supplied/profile env file > product default",
            },
        )
        return pairwise_failures

    def _matches_expected_rejection(
        self,
        profile: EnvProfile,
        stage: str,
        message: str,
        isolation,
        redactor: SecretRedactor,
        *,
        prior_failure_count: int,
    ) -> bool:
        profile_root = isolation.paths.profile_root if isolation is not None else self.run_root / self._profile_part(profile.name)
        source_paths = {
            "port-check-log": Path("services/port-check.log"),
            "app-log": Path("services/app.log"),
            "compose-up-log": Path("services/compose-up.log"),
        }
        patterns = profile.expected_error_patterns
        scenario = profile.generated_scenario
        scenario_errors: list[str] = []
        scenario_variable = scenario.variable if scenario is not None else None
        scenario_value = ""
        scenario_secret = bool(scenario.secret) if scenario is not None else False
        if scenario is not None:
            if scenario.expected_outcome != "reject":
                scenario_errors.append("generated scenario does not declare reject")
            if scenario.expected_error_patterns != patterns or scenario.expected_error_sources != profile.expected_error_sources:
                scenario_errors.append("generated scenario rejection contract differs from its profile")
            if isolation is None:
                if stage != "isolation":
                    scenario_errors.append("generated rejection has no isolation evidence outside the isolation stage")
            else:
                contract = isolation.scenario_contract
                expected_contract = {
                    "variable": scenario.variable,
                    "scenario": scenario.scenario,
                    "expected_outcome": "reject",
                    "expected_failure_stage": profile.expected_failure_stage,
                    "expected_error_patterns": list(patterns),
                    "expected_error_sources": list(profile.expected_error_sources),
                }
                for key, expected_value in expected_contract.items():
                    if contract.get(key) != expected_value:
                        scenario_errors.append(f"scenario contract mismatch: {key}")
                scenario_value = isolation.environment.get(scenario.variable, "")

        attempt_errors: list[str] = []
        attempt_evidence: dict[str, Any] = {"required": stage in {"app_start", "app_health"}, "validated": False}
        source_content_override: dict[str, str] = {}
        if stage in {"app_start", "app_health"}:
            if isolation is None:
                attempt_errors.append("application rejection has no completed isolation")
            else:
                attempts_path = profile_root / "state" / "app-start-attempts.json"
                try:
                    attempt_document = _private_json_object(attempts_path)
                    attempts = attempt_document.get("attempts")
                    if (
                        attempt_document.get("status") != "PASS"
                        or attempt_document.get("profile") != profile.name
                        or not isinstance(attempts, list)
                        or len(attempts) != 1
                        or attempt_document.get("attempt_count") != len(attempts)
                        or attempt_document.get("raw_environment_values_recorded") is not False
                        or attempt_document.get("raw_command_line_recorded") is not False
                        or any(not isinstance(item, dict) for item in attempts)
                    ):
                        raise ValueError("application attempt evidence violates its schema")
                    indexes = [item.get("attempt_index") for item in attempts]
                    if indexes != list(range(len(attempts))):
                        raise ValueError("application attempt indexes are not contiguous and ordered")
                    latest = attempts[-1]
                    if latest.get("attempt_id") != f"app-start-{len(attempts)}" or latest.get("profile") != profile.name:
                        raise ValueError("latest application attempt identity is inconsistent")
                    if latest.get("failure_observed") is not True or latest.get("ready_observed") is not False:
                        raise ValueError("latest application attempt did not observe a startup failure")
                    started_at_ns = latest.get("started_at_ns")
                    finished_at_ns = latest.get("finished_at_ns")
                    snapshot_at_ns = latest.get("log_snapshot_at_ns")
                    if (
                        latest.get("spawned") is not True
                        or latest.get("raw_scenario_value_recorded") is not False
                        or not isinstance(started_at_ns, int)
                        or isinstance(started_at_ns, bool)
                        or not isinstance(finished_at_ns, int)
                        or isinstance(finished_at_ns, bool)
                        or not isinstance(snapshot_at_ns, int)
                        or isinstance(snapshot_at_ns, bool)
                        or not (0 < started_at_ns <= finished_at_ns <= snapshot_at_ns <= time.time_ns())
                        or time.time_ns() - snapshot_at_ns > 300 * 1_000_000_000
                        or not re.fullmatch(r"[0-9a-f]{64}", str(latest.get("process_identity_sha256") or ""))
                    ):
                        raise ValueError("latest application attempt lacks a recent ordered process/log identity")
                    if latest.get("failure_stage") != stage or latest.get("outcome") != "startup_failure":
                        raise ValueError("latest application attempt does not match the observed failure stage")
                    if scenario is not None:
                        if latest.get("scenario_variable") != scenario.variable or latest.get("scenario") != scenario.scenario:
                            raise ValueError("application attempt does not match the generated scenario")
                        expected_value_hash = (
                            None
                            if scenario.secret
                            else hashlib.sha256(isolation.environment.get(scenario.variable, "").encode("utf-8")).hexdigest()
                        )
                        if latest.get("scenario_value_sha256") != expected_value_hash:
                            raise ValueError("application attempt scenario value hash differs from the product environment")
                    if stage == "app_start":
                        exit_code = latest.get("exit_code")
                        if (
                            latest.get("exit_observed") is not True
                            or not isinstance(exit_code, int)
                            or isinstance(exit_code, bool)
                            or exit_code == 0
                        ):
                            raise ValueError("app_start rejection lacks an observed non-zero product exit")
                    log_relative = latest.get("log_relative_path")
                    if log_relative != "services/app.log":
                        raise ValueError("startup rejection is not tied to the initial application log")
                    log_path = profile_root / log_relative
                    log_bytes, log_metadata = _private_file_bytes(log_path)
                    inode_hash = hashlib.sha256(f"{log_metadata.st_dev}:{log_metadata.st_ino}".encode("ascii")).hexdigest()
                    if inode_hash != latest.get("log_inode_sha256"):
                        raise ValueError("application rejection log inode differs from its start attempt")
                    snapshot_size = latest.get("log_snapshot_size")
                    snapshot_hash = latest.get("log_snapshot_sha256")
                    if (
                        not isinstance(snapshot_size, int)
                        or isinstance(snapshot_size, bool)
                        or snapshot_size <= 0
                        or not isinstance(snapshot_hash, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash)
                    ):
                        raise ValueError("application rejection log snapshot is absent or invalid")
                    if len(log_bytes) < snapshot_size or hashlib.sha256(log_bytes[:snapshot_size]).hexdigest() != snapshot_hash:
                        raise ValueError("application rejection log no longer contains the attested failure snapshot")
                    source_content_override["app-log"] = log_bytes[:snapshot_size].decode("utf-8", errors="replace")
                    if stage == "app_health":
                        health = _private_json_value(profile_root / "services" / "health-probe.json")
                        rows = health if isinstance(health, list) else None
                        if not rows or any(not isinstance(row, dict) for row in rows):
                            raise ValueError("application health rejection lacks probe rows")
                        if any(row.get("status") == 200 for row in rows):
                            raise ValueError("application health rejection contains a successful readiness probe")
                    attempt_evidence.update(
                        {
                            "validated": True,
                            "attempt_id": latest["attempt_id"],
                            "attempt_index": latest["attempt_index"],
                            "exit_observed": latest.get("exit_observed") is True,
                            "exit_code": latest.get("exit_code"),
                            "log_snapshot_size": snapshot_size,
                            "log_snapshot_sha256": snapshot_hash,
                        }
                    )
                except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
                    attempt_errors.append(f"{type(exc).__name__}: {exc}")
        evaluated: list[dict[str, Any]] = []
        matched_source: str | None = None
        for source in profile.expected_error_sources:
            content = ""
            available = False
            relative_path: str | None = None
            if source == "runner-message":
                content = message
                available = True
            elif source in source_content_override:
                content = source_content_override[source]
                available = True
            else:
                relative = source_paths.get(source)
                if relative is not None:
                    relative_path = str(relative)
                    path = profile_root / relative
                    try:
                        content = _private_file_bytes(path)[0].decode("utf-8", errors="replace")
                        available = True
                    except (OSError, ValueError):
                        available = False
            matches = [bool(re.search(pattern, content, re.IGNORECASE)) if available else False for pattern in patterns]
            windows = _rejection_windows(content, patterns) if available else []
            bound_windows: list[dict[str, Any]] = []
            for window in windows:
                anchor = "profile-contract"
                if scenario_variable is not None:
                    text = str(window["text"])
                    anchor = ""
                    if scenario_variable in text:
                        anchor = "variable"
                    elif not scenario_secret and len(scenario_value) >= 4 and scenario_value in text:
                        anchor = "effective-value"
                    else:
                        signature = _GENERATED_REJECTION_SIGNATURES.get(scenario_variable)
                        if signature is not None and signature.search(text):
                            anchor = "variable-specific-signature"
                if anchor:
                    bound_windows.append(
                        {
                            "start_line": window["start_line"],
                            "end_line": window["end_line"],
                            "cause_anchor": anchor,
                        }
                    )
            all_patterns_matched = bool(bound_windows)
            evaluated.append(
                {
                    "source": source,
                    "relative_path": relative_path,
                    "available": available,
                    "pattern_matches": matches,
                    "all_patterns_matched_in_this_source": all_patterns_matched,
                    "bounded_candidate_count": len(windows),
                    "cause_bound_candidate_count": len(bound_windows),
                    "cause_bound_windows": bound_windows,
                }
            )
            if matched_source is None and all_patterns_matched:
                matched_source = source
        stage_matched = bool(profile.expected_failure_stage) and (stage == profile.expected_failure_stage)
        scenario_matched = not scenario_errors
        attempt_matched = not attempt_errors and (not attempt_evidence["required"] or attempt_evidence["validated"])
        matched = stage_matched and matched_source is not None and scenario_matched and attempt_matched
        try:
            redactor.write_json(
                profile_root / "state" / "expected-rejection.json",
                {
                    "profile": profile.name,
                    "declared_stage": profile.expected_failure_stage,
                    "observed_stage": stage,
                    "stage_matched": stage_matched,
                    "declared_patterns": list(patterns),
                    "declared_sources": list(profile.expected_error_sources),
                    "sources": evaluated,
                    "matched_source": matched_source,
                    "scenario_variable": scenario_variable,
                    "scenario_contract_matched": scenario_matched,
                    "scenario_errors": scenario_errors,
                    "attempt": attempt_evidence,
                    "attempt_errors": attempt_errors,
                    "prior_failure_count": prior_failure_count,
                    "matched": matched,
                    "pass_eligible_before_cleanup": matched and prior_failure_count == 0,
                },
            )
        except OSError:
            return False
        return matched

    @staticmethod
    def _record_expected_rejection(result: ProfileResult, *, stage: str) -> None:
        if result.failures:
            result.fail(
                "expected rejection was observed but cannot override earlier failures",
                stage=stage,
            )
            return
        result.status = "PASS"
        result.exit_code = 0
        result.stage = "expected_rejection"

    def _finalize_environment_coverage(self) -> None:
        if not self.environment_coverage:
            return
        self.environment_coverage = finalize_environment_coverage(
            copy.deepcopy(self.environment_coverage),
            run_root=self.run_root,
            profile_results=[item.as_dict() for item in self.results],
        )
        if self.environment_coverage.get("status") != "PASS":
            message = "environment variable scenario coverage is incomplete or lacks execution evidence"
            if message not in self.global_failures:
                self.global_failures.append(message)

    def _finalize_feature_coverage(self) -> None:
        if not self.feature_coverage:
            return
        self.feature_coverage = finalize_feature_coverage(
            copy.deepcopy(self.feature_coverage),
            run_root=self.run_root,
        )
        if self.feature_coverage.get("status") != "PASS":
            message = "UI feature/case coverage is incomplete or lacks passing evidence"
            if message not in self.global_failures:
                self.global_failures.append(message)

    def _finalize_usage_summary(self) -> None:
        self.usage_summary = collect_usage_summary(self.run_root)
        if self.usage_summary.get("errors"):
            message = "usage evidence is malformed or contains conflicting duplicate turns"
            if message not in self.global_failures:
                self.global_failures.append(message)
        if self.options.suite in {"full", "chat"} and not int((self.usage_summary.get("totals") or {}).get("turns") or 0):
            message = "full/chat run produced no normalized real-provider usage records"
            if message not in self.global_failures:
                self.global_failures.append(message)
        SecretRedactor(self._secret_values).write_json(
            self.run_root / "usage-summary.json",
            self.usage_summary,
        )

    def _write_coverage_reports(self) -> None:
        redactor = SecretRedactor(self._secret_values)
        feature = self.feature_coverage or {
            "status": "NOT_EVALUATED",
            "suite": self.options.suite,
            "reason": "feature execution coverage is evaluated by the full suite",
        }
        environment = self.environment_coverage or {
            "status": "NOT_EVALUATED",
            "suite": self.options.suite,
            "reason": "environment execution coverage is evaluated by the full or env suite",
        }
        redactor.write_json(self.run_root / "feature-coverage.json", feature)
        redactor.write_json(self.run_root / "env-coverage.json", environment)

    def _write_reports(self) -> dict[str, Any]:
        redactor = SecretRedactor(self._secret_values)
        return write_summary(
            run_root=self.run_root,
            run_id=self.run_id,
            suite=self.options.suite,
            started_at=self.started_at,
            finished_at=utc_now(),
            results=self.results,
            global_failures=list(dict.fromkeys(self.global_failures)),
            usage_summary=self.usage_summary,
            redactor=redactor,
        )

    def _final_security_pass(self) -> tuple[bool, list[dict[str, Any]]]:
        redactor = SecretRedactor(self._secret_values)
        try:
            assert_no_mount_targets(self.run_root)
        except RuntimeError as exc:
            failure = f"artifact mount boundary validation failed: {type(exc).__name__}: {exc}"
            if failure not in self.global_failures:
                self.global_failures.append(failure)
            return False, []
        permission_failures = harden_artifact_permissions(self.run_root)
        for message in permission_failures:
            failure = f"artifact permission policy failed: {message}"
            if failure not in self.global_failures:
                self.global_failures.append(failure)
        if permission_failures:
            return False, []
        incidents = redactor.quarantine_leaks(self.run_root)
        if incidents:
            redactor.write_json(self.run_root / "security" / "leak-report.json", {"incidents": incidents})
            by_profile = {item.profile: item for item in self.results}
            for incident in incidents:
                first = str(incident["path"]).split("/", 1)[0]
                result = by_profile.get(first)
                if result is not None:
                    result.secret_leaks.append(incident)
                    result.fail("final artifact scan removed secret-like content", stage="secret_scan")
                else:
                    self.global_failures.append("final artifact scan removed secret-like content")
        redactor.redact_text_files(self.run_root)
        return True, incidents

    def _refresh_profile_results(self) -> None:
        redactor = SecretRedactor(self._secret_values)
        for result in self.results:
            profile_root = self.run_root / self._profile_part(result.profile)
            profile_root.mkdir(parents=True, exist_ok=True)
            assert_no_mount_targets(profile_root)
            result.artifacts = sorted(
                str(path.relative_to(profile_root))
                for path in profile_root.rglob("*")
                if not path.is_symlink() and path.is_file() and path.name != "result.json"
            )
            result.artifacts.append("result.json")
            redactor.write_json(profile_root / "result.json", result.as_dict())

    def _ensure_unexecuted_case_results(self) -> None:
        """Materialize per-profile FAIL results for every planned case without evidence."""

        redactor = SecretRedactor(self._secret_values)
        total_missing = 0
        for result in self.results:
            profile_part = self._profile_part(result.profile)
            profile_root = self.run_root / profile_part
            profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            assert_no_mount_targets(profile_root)
            expected = set(self._profile_case_plans.get(result.profile, ()))
            guard_path = profile_root / "reports" / "strict-outcomes.json"
            if guard_path.is_file():
                try:
                    guard = json.loads(guard_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    guard = None
                if isinstance(guard, dict) and isinstance(guard.get("selected_nodeids"), list):
                    expected.update(str(item) for item in guard["selected_nodeids"] if isinstance(item, str))
            observed: set[str] = set()
            for path in profile_root.glob("*/*/result.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict) and payload.get("nodeid"):
                    observed.add(str(payload["nodeid"]))
            missing = sorted(expected - observed)
            if not missing:
                continue
            total_missing += len(missing)
            for nodeid in missing:
                path_text, _, test_name = nodeid.partition("::")
                parts = Path(path_text).parts
                feature = parts[parts.index("cases") + 1] if "cases" in parts else "misc"
                descriptive_id = f"{Path(path_text).stem}--{test_name or 'module-collection'}"
                case_id = re.sub(r"[^A-Za-z0-9._-]+", "-", descriptive_id)
                case_root = profile_root / feature / case_id
                redactor.write_json(
                    case_root / "state" / "evidence-unavailable.json",
                    {
                        "status": "FAIL",
                        "profile": result.profile,
                        "nodeid": nodeid,
                        "reason": "case produced no result because collection, application startup, or a session fixture failed",
                        "semantic_screenshot_available": False,
                        "browser_trace_available": False,
                    },
                )
                redactor.write_json(
                    case_root / "result.json",
                    {
                        "nodeid": nodeid,
                        "outcome": "failed",
                        "duration_seconds": 0,
                        "page_errors": 0,
                        "request_failures": 0,
                        "cleanup_errors": [],
                        "evidence_failures": [
                            "required case result was absent for this profile",
                            "required semantic screenshot was unavailable",
                            "required browser/service/state evidence was unavailable",
                        ],
                        "diagnostic_failures": ["collection, startup, or session fixture prevented case execution"],
                        "error": "case did not execute in this profile; no success evidence was synthesized",
                        "security_leak_count": 0,
                        "security_removed_files": [],
                    },
                )
            failure = f"{len(missing)} planned case(s) produced no artifact result in profile {result.profile}"
            if failure not in result.failures:
                result.fail(failure, stage="case_evidence")
        if total_missing:
            message = f"{total_missing} profile/case execution(s) produced no artifact result"
            if message not in self.global_failures:
                self.global_failures.append(message)

    def _discard_untrusted_profile_artifacts(self, profile_root: Path, redactor: SecretRedactor) -> None:
        """Delete child evidence when its runtime-secret registry cannot be trusted."""

        run_root = self.run_root.resolve(strict=True)
        if profile_root.is_symlink():
            raise RuntimeError("refused to discard a symlink profile artifact path")
        resolved = profile_root.resolve(strict=True)
        if resolved.parent != run_root:
            raise RuntimeError("refused to discard an unowned or non-profile artifact path")
        assert_no_mount_targets(resolved)
        removed_files = sum(1 for path in resolved.rglob("*") if not path.is_symlink() and path.is_file())
        assert_no_mount_targets(resolved)
        for child in list(resolved.iterdir()):
            if child.is_symlink():
                child.unlink()
            elif child.is_dir():
                assert_no_mount_targets(child)
                rmtree_no_follow(child)
            else:
                child.unlink()
        chmod_path_no_follow(resolved, 0o700, require_owner_uid=os.geteuid())
        redactor.write_json(
            resolved / "security" / "untrusted-artifacts-discarded.json",
            {
                "status": "FAIL",
                "reason": "runtime secret registry could not prove generated evidence safe",
                "removed_file_count": removed_files,
                "raw_values_persisted": False,
            },
        )

    @staticmethod
    def _sync_final_service_logs(profile_root: Path) -> None:
        assert_no_mount_targets(profile_root)
        assert_no_unsafe_hardlinks(profile_root)
        profile_services = profile_root / "services"
        if not profile_services.is_dir():
            return
        app_candidates = sorted(profile_services.glob("app*.log"))
        if any(path.is_symlink() for path in app_candidates):
            raise RuntimeError("final service log source must not be a symlink")
        app_sources = [path for path in app_candidates if path.is_file()]
        compose_sources = []
        for name in ("compose.log", "compose-up.log", "ocr-up.log", "cleanup.log"):
            source = profile_services / name
            if source.is_symlink():
                raise RuntimeError("final compose log source must not be a symlink")
            if source.is_file():
                compose_sources.append(source)
        for case_result in sorted(profile_root.glob("*/*/result.json")):
            case_services = case_result.parent / "services"
            case_services.mkdir(parents=True, exist_ok=True)
            if app_sources:
                sections = [f"[{source.name}]\n{source.read_text(encoding='utf-8', errors='replace')}" for source in app_sources]
                write_private_text_atomic(case_services / "app.log", "\n".join(sections))
            if compose_sources:
                sections = [f"[{source.name}]\n{source.read_text(encoding='utf-8', errors='replace')}" for source in compose_sources]
                write_private_text_atomic(case_services / "compose.log", "\n".join(sections))

    @staticmethod
    def _profile_part(value: str) -> str:
        clean = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
        return (clean or "profile")[:80]

    def _resolve_source_env_file(self) -> Path | None:
        if self.options.env_file is not None:
            candidate = self.options.env_file
        else:
            configured = os.environ.get("SHERPA_ENV_FILE", "").strip()
            if not configured:
                return None
            candidate = Path(configured)
            if not candidate.is_absolute():
                candidate = self.repository / candidate
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"specified SHERPA_ENV_FILE is missing: {resolved}")
        return resolved
