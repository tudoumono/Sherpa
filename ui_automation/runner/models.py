"""ランナー内部で共有する値オブジェクト。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VALID_SUITES = frozenset({"full", "smoke", "chat", "env"})
VALID_REJECTION_ERROR_SOURCES = frozenset({"runner-message", "port-check-log", "app-log", "compose-up-log"})


@dataclass(frozen=True)
class GeneratedEnvScenario:
    variable: str
    scenario: str
    scenario_set: str
    category: str
    secret: bool
    declared_restart: str
    observables: tuple[str, ...]
    process_mode: str
    process_value: str | None = None
    env_file_mode: str = "absent"
    env_file_value: str | None = None
    prerequisites: tuple[str, ...] = ()
    expected_outcome: str = "observed"
    expected_error_patterns: tuple[str, ...] = ()
    expected_error_sources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "scenario": self.scenario,
            "scenario_set": self.scenario_set,
            "category": self.category,
            "secret": self.secret,
            "declared_restart": self.declared_restart,
            "observables": list(self.observables),
            "process": {"mode": self.process_mode, "value": self.process_value},
            "env_file": {"mode": self.env_file_mode, "value": self.env_file_value},
            "prerequisites": list(self.prerequisites),
            "expected_outcome": self.expected_outcome,
            "expected_error_patterns": list(self.expected_error_patterns),
            "expected_error_sources": list(self.expected_error_sources),
        }


@dataclass(frozen=True)
class PairwiseFactor:
    key: str
    level: str
    secret: bool
    process_mode: str
    process_value: str | None = None
    env_file_mode: str = "absent"
    env_file_value: str | None = None
    prerequisites: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        process: dict[str, Any] = {"mode": self.process_mode}
        env_file: dict[str, Any] = {"mode": self.env_file_mode}
        if not self.secret:
            process["value"] = self.process_value
            env_file["value"] = self.env_file_value
        return {
            "key": self.key,
            "level": self.level,
            "sensitive": self.secret,
            "process": process,
            "env_file": env_file,
            "prerequisites": list(self.prerequisites),
        }


@dataclass(frozen=True)
class PairwiseScenario:
    group_id: str
    row_id: str
    factors: tuple[PairwiseFactor, ...]
    observables: tuple[str, ...]
    case_nodeid: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated": True,
            "kind": "pairwise",
            "group_id": self.group_id,
            "row_id": self.row_id,
            "factors": [factor.as_dict() for factor in self.factors],
            "observables": list(self.observables),
            "case_nodeid": self.case_nodeid,
        }


def env_string(value: Any) -> str | None:
    """YAML の値を subprocess 環境変数として曖昧さなく文字列化する。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


@dataclass(frozen=True)
class EnvProfile:
    name: str
    suites: tuple[str, ...]
    env: dict[str, str | None] = field(default_factory=dict)
    env_file: dict[str, str | None] = field(default_factory=dict)
    restart: str = "stack"
    fresh_database: bool = True
    expected: tuple[Any, ...] = ()
    expected_values: dict[str, Any] = field(default_factory=dict)
    observables: tuple[Any, ...] = ()
    pytest_args: tuple[str, ...] = ()
    marker: str | None = None
    expect_startup_failure: bool = False
    expected_error_patterns: tuple[str, ...] = ()
    expected_error_sources: tuple[str, ...] = ()
    expected_failure_stage: str | None = None
    generated_scenario: GeneratedEnvScenario | None = None
    pairwise_scenario: PairwiseScenario | None = None
    restart_env: dict[str, Any] = field(default_factory=dict)
    restart_transition_id: str | None = None
    restart_observable: dict[str, Any] = field(default_factory=dict)
    case_nodeids: tuple[str, ...] = ()
    preflight_fixture: str | None = None
    codex_auth_mode: str = "prepare"

    @classmethod
    def from_mapping(cls, name: str, raw: dict[str, Any]) -> "EnvProfile":
        suites = tuple(str(item) for item in raw.get("suites", ()))
        invalid = sorted(set(suites) - VALID_SUITES)
        if invalid:
            raise ValueError(f"profile {name!r} has invalid suites: {', '.join(invalid)}")
        env_raw = raw.get("env") or {}
        env_file_raw = raw.get("env_file") or {}
        if not isinstance(env_raw, dict):
            raise ValueError(f"profile {name!r}.env must be a mapping")
        if not isinstance(env_file_raw, dict):
            raise ValueError(f"profile {name!r}.env_file must be a mapping")
        restart = str(raw.get("restart", "stack"))
        if restart not in {"none", "app", "stack"}:
            raise ValueError(f"profile {name!r}.restart must be none, app, or stack")
        preflight_fixture = str(raw["preflight_fixture"]) if raw.get("preflight_fixture") else None
        if preflight_fixture not in {None, "occupy_app_port"}:
            raise ValueError(f"profile {name!r}.preflight_fixture is not an allowed fixed fixture")
        codex_auth_mode = str(raw.get("codex_auth_mode") or "prepare")
        if codex_auth_mode not in {"prepare", "intentionally-unconfigured"}:
            raise ValueError(f"profile {name!r}.codex_auth_mode must be prepare or intentionally-unconfigured")
        fresh_database = bool(raw.get("fresh_database", True))
        if not fresh_database:
            raise ValueError(
                f"profile {name!r}.fresh_database=false is unsupported; cross-restart persistence must stay inside one isolated profile"
            )
        pytest_args = raw.get("pytest_args") or ()
        if isinstance(pytest_args, str):
            raise ValueError(f"profile {name!r}.pytest_args must be a list")
        expect_startup_failure = bool(raw.get("expect_startup_failure", False))
        expected_failure_stage = str(raw["expected_failure_stage"]) if raw.get("expected_failure_stage") else None
        expected_error_patterns = tuple(str(item) for item in raw.get("expected_error_patterns") or ())
        expected_error_sources = tuple(str(item) for item in raw.get("expected_error_sources") or ())
        for pattern in expected_error_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"profile {name!r} has an invalid rejection regex: {exc}") from exc
        if expect_startup_failure:
            if not expected_failure_stage or not expected_error_patterns or not expected_error_sources:
                raise ValueError(f"profile {name!r} expected startup rejection requires stage, patterns, and fixed evidence sources")
            invalid_sources = sorted(set(expected_error_sources) - VALID_REJECTION_ERROR_SOURCES)
            if invalid_sources:
                raise ValueError(f"profile {name!r} has unsupported rejection evidence sources: " + ", ".join(invalid_sources))
        elif expected_failure_stage or expected_error_patterns or expected_error_sources:
            raise ValueError(f"profile {name!r} declares rejection evidence without expect_startup_failure=true")
        return cls(
            name=name,
            suites=suites,
            env={str(key): env_string(value) for key, value in env_raw.items()},
            env_file={str(key): env_string(value) for key, value in env_file_raw.items()},
            restart=restart,
            fresh_database=fresh_database,
            expected=tuple(raw.get("expected") or ()),
            expected_values=dict(raw.get("expected_values") or {}),
            observables=tuple(raw.get("observables") or ()),
            pytest_args=tuple(str(item) for item in pytest_args),
            marker=str(raw["marker"]) if raw.get("marker") else None,
            expect_startup_failure=expect_startup_failure,
            expected_error_patterns=expected_error_patterns,
            expected_error_sources=expected_error_sources,
            expected_failure_stage=expected_failure_stage,
            restart_env=dict(raw.get("restart_env") or {}),
            restart_transition_id=(
                str(raw["restart_transition_id"])
                if raw.get("restart_transition_id")
                else ("profile-restart" if raw.get("restart_env") else None)
            ),
            restart_observable=dict(raw.get("restart_observable") or {}),
            case_nodeids=tuple(str(item) for item in raw.get("case_nodeids") or ()),
            preflight_fixture=preflight_fixture,
            codex_auth_mode=codex_auth_mode,
        )


@dataclass(frozen=True)
class RunPaths:
    repository: Path
    run_root: Path
    profile_root: Path
    runtime_root: Path


@dataclass
class ProfileResult:
    profile: str
    suite: str
    status: str = "FAIL"
    stage: str = "not_started"
    exit_code: int = 1
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    tests: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    cleanup_ok: bool = False
    secret_leaks: list[dict[str, Any]] = field(default_factory=list)
    environment_scenario: dict[str, Any] = field(default_factory=dict)

    def fail(self, message: str, *, stage: str | None = None) -> None:
        self.status = "FAIL"
        self.exit_code = 1
        if stage:
            self.stage = stage
        self.failures.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "suite": self.suite,
            "status": self.status,
            "stage": self.stage,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "tests": dict(self.tests),
            "failures": list(self.failures),
            "artifacts": list(self.artifacts),
            "cleanup_ok": self.cleanup_ok,
            "secret_leaks": list(self.secret_leaks),
            "environment_scenario": dict(self.environment_scenario),
        }
