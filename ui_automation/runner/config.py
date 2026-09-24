"""独立 UI 試験用 YAML manifest の読み込み。"""

from __future__ import annotations

import fnmatch
import hashlib
import itertools
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from ui_automation.runner.models import (
    EnvProfile,
    GeneratedEnvScenario,
    PairwiseFactor,
    PairwiseScenario,
    VALID_REJECTION_ERROR_SOURCES,
    env_string,
)


_SCENARIOS = ("default", "valid", "boundary", "invalid", "precedence", "restart")
_OUTCOME_SCENARIOS = frozenset({"boundary", "invalid"})
_ALLOWED_EXPECTED_OUTCOMES = frozenset({"reject", "explicit-error", "accepted-boundary"})
_PAIRWISE_RUNTIME_VALUES = {
    "app_port_primary",
    "app_port_secondary",
    "database_localhost",
    "database_loopback",
    "env_file_primary",
    "env_file_secondary",
    "es_localhost",
    "es_loopback",
    "neo4j_localhost",
    "neo4j_loopback",
}
_GENERATED_RUNTIME_VALUES = frozenset(
    {
        "alternate_path",
        "dynamic_port",
        "empty_path",
        "invalid_connection",
        "invalid_path",
        "isolated_connection",
        "isolated_project",
        "isolated_secret",
        "python_bin",
        "safe_path",
        "valid_path",
    }
)
_PAIRWISE_ISOLATED_RUNTIME_BY_VARIABLE = {
    "DATABASE_URL": {"database_localhost", "database_loopback"},
    "ES_URL": {"es_localhost", "es_loopback"},
    "NEO4J_URI": {"neo4j_localhost", "neo4j_loopback"},
    "SHERPA_ENV_FILE": {"env_file_primary", "env_file_secondary"},
    "SHERPA_PORT": {"app_port_primary", "app_port_secondary"},
}
_CAPABILITY_CHECK_KEYS = {
    "browser": {"engine", "headless", "launch", "phase", "timeout_seconds"},
    "command": {
        "args",
        "command",
        "command_env",
        "expected_exit",
        "fallback_command",
        "phase",
        "timeout_seconds",
    },
    "compose_service": {
        "phase",
        "profile",
        "require_healthy_or_completed_probe",
        "service",
    },
    "directory": {"must_exist", "must_not_be_empty", "path", "path_env", "phase"},
    "file": {"must_exist", "must_not_be_empty", "path", "path_env", "phase"},
    "http": {
        "expected_status",
        "fallback",
        "path",
        "phase",
        "timeout_seconds",
        "url",
        "url_env",
    },
    "provider": {
        "evidence_glob",
        "model_env",
        "perform_real_image_request",
        "perform_real_request",
        "provider",
        "provider_env",
        "require_nonzero_usage",
        "require_observation_record",
    },
    "python_module": {"module", "modules", "phase"},
    "secret_presence": {
        "all_with",
        "env",
        "env_any_of",
        "env_or_seeded_setting",
        "phase",
        "reject_placeholders",
    },
    "setting": {
        "env",
        "envs",
        "must_be_under_run_directory",
        "phase",
        "reject_repository_data_directory",
        "reject_values",
        "required_prefix",
    },
    "source_policy": {"forbidden_tokens", "phase", "roots"},
    "tcp": {"host", "host_env", "phase", "port", "port_env", "timeout_seconds"},
}
_CAPABILITY_REQUIREMENT_KEYS = {
    "check",
    "evidence",
    "excluded_profiles",
    "id",
    "kind",
    "required",
    "required_for",
    "required_profiles",
    "suites",
}
_CAPABILITY_POLICY = {
    "allow_fake_provider": False,
    "allow_mock": False,
    "allow_request_interception": False,
    "allow_stub": False,
    "continue_after_failure": True,
    "credentials_are_presence_only": True,
    "missing_status": "FAIL",
    "record_secret_values": False,
}
_CAPABILITY_PHASES = {"preflight", "post_stack", "post_app", "post_tests"}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def capability_evidence_path(requirement_id: str) -> Path:
    safe_id = re.sub(r"[^a-z0-9_-]+", "-", requirement_id.lower()).strip("-_")
    return Path("services") / f"capability-{safe_id or 'unnamed'}.json"


def _load_yaml(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"required configuration is missing: {path}")
        return {}
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required; install requirements-dev.txt") from exc
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    return loaded


def load_profiles(config_root: Path, suite: str) -> list[EnvProfile]:
    document = _load_yaml(config_root / "env_matrix.yaml")
    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError("env_matrix.yaml must define at least one profile")
    selected_suites = {suite, "env"} if suite == "full" else {suite}
    profiles = [
        EnvProfile.from_mapping(str(name), raw)
        for name, raw in raw_profiles.items()
        if isinstance(raw, dict) and selected_suites.intersection(str(item) for item in (raw.get("suites") or ()))
    ]
    execution = document.get("execution") or {}
    if suite in {"full", "env"}:
        profiles.extend(generate_pairwise_profiles(document))
    if suite in {"full", "env"} and bool(execution.get("generate_profiles_from_variables")):
        profiles.extend(generate_environment_profiles(document, config_root.parent.parent))
    if not profiles:
        raise ValueError(f"env_matrix.yaml has no profile for suite {suite!r}")
    names = [profile.name for profile in profiles]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ValueError("duplicate generated/declared profile names: " + ", ".join(duplicates))
    return profiles


def generated_profile_name(variable: str, scenario: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", variable.lower()).strip("-")
    digest = hashlib.sha256(variable.encode("utf-8")).hexdigest()[:8]
    prefix = slug[:48].rstrip("-")
    return f"envgen-{prefix}-{digest}-{scenario}"


def _pairwise_rows(levels: list[list[str]]) -> list[tuple[str, ...]]:
    """全factor値ペアを覆う決定的なgreedy covering arrayを返す。"""
    candidates = list(itertools.product(*levels))
    required = {
        (left, candidate[left], right, candidate[right])
        for candidate in candidates
        for left in range(len(levels))
        for right in range(left + 1, len(levels))
    }
    uncovered = set(required)
    selected: list[tuple[str, ...]] = []
    while uncovered:
        best: tuple[str, ...] | None = None
        best_coverage: set[tuple[int, str, int, str]] = set()
        for candidate in candidates:
            coverage = {
                (left, candidate[left], right, candidate[right]) for left in range(len(levels)) for right in range(left + 1, len(levels))
            } & uncovered
            if len(coverage) > len(best_coverage):
                best = candidate
                best_coverage = coverage
        if best is None:
            raise ValueError("pairwise covering array generation made no progress")
        selected.append(best)
        uncovered -= best_coverage
        candidates.remove(best)
    return selected


def _pairwise_value_spec(raw: Any, *, location: str) -> tuple[str, str | None, tuple[str, ...]]:
    mode, value, prerequisites = _override_spec(raw)
    if mode == "runtime" and value not in _PAIRWISE_RUNTIME_VALUES:
        raise ValueError(f"{location} uses unsupported isolated runtime value: {value}")
    if mode not in {"unset", "literal", "inherit", "runtime"}:
        raise ValueError(f"{location} must set, unset, inherit, or use an isolated runtime value")
    return mode, value, prerequisites


def generate_pairwise_profiles(document: dict[str, Any]) -> list[EnvProfile]:
    execution = document.get("execution") or {}
    groups = execution.get("pairwise_groups") or ()
    variables = document.get("variables") or {}
    if not isinstance(groups, list) or not groups:
        raise ValueError("execution.pairwise_groups must be a non-empty list")
    profiles: list[EnvProfile] = []
    seen_groups: set[str] = set()
    for raw_group in groups:
        if not isinstance(raw_group, dict):
            raise ValueError("each pairwise group must be a mapping")
        group_id = str(raw_group.get("id") or "")
        if not group_id or group_id in seen_groups:
            raise ValueError(f"pairwise group id is missing or duplicated: {group_id or '<empty>'}")
        seen_groups.add(group_id)
        prefix = str(raw_group.get("profile") or "")
        case_nodeid = str(raw_group.get("case_nodeid") or "")
        observables = tuple(str(item) for item in raw_group.get("observables") or ())
        factor_order = [str(item) for item in raw_group.get("variables") or ()]
        raw_factors = raw_group.get("factors") or {}
        if not prefix or not case_nodeid or not observables:
            raise ValueError(f"pairwise {group_id}: profile, case_nodeid, and observables are required")
        if len(factor_order) < 2 or not isinstance(raw_factors, dict):
            raise ValueError(f"pairwise {group_id}: at least two factor mappings are required")
        if set(factor_order) != {str(key) for key in raw_factors}:
            raise ValueError(f"pairwise {group_id}: variables and factors must contain identical keys")

        parsed: dict[str, dict[str, PairwiseFactor]] = {}
        ordered_levels: list[list[str]] = []
        for variable in factor_order:
            definition = variables.get(variable)
            if not isinstance(definition, dict) or definition.get("classification") != "tested":
                raise ValueError(f"pairwise {group_id}: variable is not classified tested: {variable}")
            raw_levels = raw_factors.get(variable)
            if not isinstance(raw_levels, list) or len(raw_levels) < 2:
                raise ValueError(f"pairwise {group_id}/{variable}: at least two levels are required")
            parsed_levels: dict[str, PairwiseFactor] = {}
            effective_specs: set[tuple[str, str | None, str, str | None]] = set()
            for raw_level in raw_levels:
                if not isinstance(raw_level, dict):
                    raise ValueError(f"pairwise {group_id}/{variable}: level must be a mapping")
                level_id = str(raw_level.get("id") or "")
                if not level_id or level_id in parsed_levels:
                    raise ValueError(f"pairwise {group_id}/{variable}: level id is missing or duplicated")
                if "process" not in raw_level:
                    raise ValueError(f"pairwise {group_id}/{variable}/{level_id}: process is required")
                process_mode, process_value, process_requirements = _pairwise_value_spec(
                    raw_level.get("process"), location=f"pairwise {group_id}/{variable}/{level_id}.process"
                )
                if "env_file" in raw_level:
                    file_mode, file_value, file_requirements = _pairwise_value_spec(
                        raw_level.get("env_file"),
                        location=f"pairwise {group_id}/{variable}/{level_id}.env_file",
                    )
                else:
                    file_mode, file_value, file_requirements = "absent", None, ()
                isolated_values = _PAIRWISE_ISOLATED_RUNTIME_BY_VARIABLE.get(variable)
                if isolated_values is not None:
                    if process_mode != "runtime" or process_value not in isolated_values:
                        raise ValueError(
                            f"pairwise {group_id}/{variable}/{level_id}: process must use the matching runner-owned isolated runtime value"
                        )
                    if file_mode not in {"absent", "runtime"} or (file_mode == "runtime" and file_value not in isolated_values):
                        raise ValueError(
                            f"pairwise {group_id}/{variable}/{level_id}: env_file must be absent "
                            "or use the matching runner-owned isolated runtime value"
                        )
                secret = bool(definition.get("secret"))
                if secret and process_mode == "literal" and process_value:
                    raise ValueError(f"pairwise {group_id}/{variable}/{level_id}: secret literals are forbidden")
                effective_spec = (process_mode, process_value, file_mode, file_value)
                if effective_spec in effective_specs:
                    raise ValueError(f"pairwise {group_id}/{variable}: levels must have distinct effective values")
                effective_specs.add(effective_spec)
                parsed_levels[level_id] = PairwiseFactor(
                    key=variable,
                    level=level_id,
                    secret=secret,
                    process_mode=process_mode,
                    process_value=process_value,
                    env_file_mode=file_mode,
                    env_file_value=file_value,
                    prerequisites=tuple(dict.fromkeys((*process_requirements, *file_requirements))),
                )
            parsed[variable] = parsed_levels
            ordered_levels.append(list(parsed_levels))

        rows = _pairwise_rows(ordered_levels)
        profile_template = raw_group.get("profile_template") or {}
        if not isinstance(profile_template, dict):
            raise ValueError(f"pairwise {group_id}: profile_template must be a mapping")
        for index, level_ids in enumerate(rows, 1):
            row_id = f"r{index:02d}"
            name = prefix if index == 1 else f"{prefix}-{row_id}"
            template = dict(profile_template)
            template.setdefault("suites", ["full", "env"])
            template.setdefault("marker", "environment")
            template.setdefault("case_nodeids", [case_nodeid])
            template.setdefault("fresh_database", True)
            if index == 1:
                if raw_group.get("primary_marker"):
                    template["marker"] = str(raw_group["primary_marker"])
                if raw_group.get("primary_case_nodeids"):
                    template["case_nodeids"] = [str(item) for item in raw_group["primary_case_nodeids"]]
            template["observables"] = list(observables)
            factors = tuple(parsed[variable][level_id] for variable, level_id in zip(factor_order, level_ids, strict=True))
            profile = EnvProfile.from_mapping(name, template)
            profiles.append(
                replace(
                    profile,
                    pairwise_scenario=PairwiseScenario(
                        group_id=group_id,
                        row_id=row_id,
                        factors=factors,
                        observables=observables,
                        case_nodeid=case_nodeid,
                    ),
                )
            )
    return profiles


def _override_spec(raw: Any) -> tuple[str, str | None, tuple[str, ...]]:
    if isinstance(raw, dict):
        selectors = [key for key in ("from_env", "source_env", "runtime", "value") if key in raw]
        if len(selectors) != 1 or set(raw) - set(selectors):
            raise ValueError("environment test value mapping must contain exactly one of from_env, source_env, runtime, or value")
        source = raw.get("from_env") or raw.get("source_env")
        if source:
            name = str(source)
            return "inherit", name, (name,)
        if "runtime" in raw and raw.get("runtime"):
            return "runtime", str(raw["runtime"]), ()
        if "value" in raw:
            return "literal", env_string(raw.get("value")), ()
        raise ValueError("environment test value source cannot be empty")
    if raw is None:
        return "unset", None, ()
    return "literal", env_string(raw), ()


def _expected_outcome_spec(
    variable: str,
    raw: dict[str, Any],
    scenario: str,
) -> tuple[str, tuple[str, ...], str | None, tuple[str, ...]]:
    if scenario not in _OUTCOME_SCENARIOS:
        return "observed", (), None, ()
    declarations = raw.get("expected_outcomes") or {}
    if not isinstance(declarations, dict) or scenario not in declarations:
        raise ValueError(f"{variable}: expected_outcomes.{scenario} is required")
    selected = declarations[scenario]
    if isinstance(selected, str):
        outcome = selected
        patterns: tuple[str, ...] = ()
        error_sources: tuple[str, ...] = ()
        failure_stage = None
    elif isinstance(selected, dict):
        outcome = str(selected.get("outcome") or "")
        patterns = tuple(str(value) for value in selected.get("error_patterns") or ())
        error_sources = tuple(str(value) for value in selected.get("error_sources") or ())
        failure_stage = str(selected["failure_stage"]) if selected.get("failure_stage") else None
    else:
        raise ValueError(f"{variable}: expected_outcomes.{scenario} must be a string or mapping")
    if outcome not in _ALLOWED_EXPECTED_OUTCOMES:
        allowed = ", ".join(sorted(_ALLOWED_EXPECTED_OUTCOMES))
        raise ValueError(f"{variable}: expected_outcomes.{scenario} must be one of {allowed}")
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{variable}: expected_outcomes.{scenario} has an invalid regex: {exc}") from exc
    if outcome == "reject":
        if not failure_stage:
            raise ValueError(f"{variable}: reject outcome for {scenario} requires failure_stage")
        if not patterns:
            raise ValueError(f"{variable}: reject outcome for {scenario} requires error_patterns")
        invalid_sources = sorted(set(error_sources) - VALID_REJECTION_ERROR_SOURCES)
        if not error_sources or invalid_sources:
            raise ValueError(
                f"{variable}: reject outcome for {scenario} requires error_sources from " + ", ".join(sorted(VALID_REJECTION_ERROR_SOURCES))
            )
    elif outcome == "explicit-error":
        if not patterns or not error_sources:
            raise ValueError(f"{variable}: explicit-error outcome for {scenario} requires error_patterns and error_sources")
    elif failure_stage:
        raise ValueError(f"{variable}: failure_stage is only valid for a reject outcome")
    return outcome, patterns, failure_stage, error_sources


def _scenario_spec(
    variable: str,
    raw: dict[str, Any],
    scenario: str,
) -> tuple[str, str | None, str, str | None, tuple[str, ...]]:
    overrides = raw.get("test_values") or {}
    if not isinstance(overrides, dict) or scenario not in overrides:
        raise ValueError(f"{variable}: explicit test_values.{scenario} is required")
    if isinstance(overrides, dict) and scenario in overrides:
        selected = overrides[scenario]
        if scenario == "precedence" and isinstance(selected, dict) and ("process" in selected or "env_file" in selected):
            process_mode, process_value, process_requirements = _override_spec(selected.get("process"))
            file_mode, file_value, file_requirements = _override_spec(selected.get("env_file"))
            for mode, value in ((process_mode, process_value), (file_mode, file_value)):
                if mode == "runtime" and value not in _GENERATED_RUNTIME_VALUES:
                    raise ValueError(f"{variable}/{scenario}: unsupported isolated runtime test value: {value}")
            return process_mode, process_value, file_mode, file_value, tuple(dict.fromkeys((*process_requirements, *file_requirements)))
        mode, value, requirements = _override_spec(selected)
        if mode == "runtime" and value not in _GENERATED_RUNTIME_VALUES:
            raise ValueError(f"{variable}/{scenario}: unsupported isolated runtime test value: {value}")
        return mode, value, "absent", None, requirements

    raise AssertionError("validated environment test value was not selected")


def generate_environment_profiles(document: dict[str, Any], _repository: Path) -> list[EnvProfile]:
    variables = document.get("variables") or {}
    execution_coverage = document.get("execution_coverage") or {}
    required_scenarios = tuple(str(item) for item in execution_coverage.get("required_scenarios") or _SCENARIOS)
    if set(required_scenarios) != set(_SCENARIOS):
        raise ValueError("generated environment profiles require exactly default/valid/boundary/invalid/precedence/restart")
    profiles: list[EnvProfile] = []
    for variable, raw_value in sorted(variables.items()):
        if not isinstance(raw_value, dict) or raw_value.get("classification") != "tested":
            continue
        raw = raw_value
        scenario_set = str(raw.get("scenario_set") or "standard")
        category = str(raw.get("category") or "uncategorized")
        declared_restart = str(raw.get("restart") or "app")
        observables = tuple(str(item) for item in raw.get("observable") or ())
        for scenario in required_scenarios:
            process_mode, process_value, file_mode, file_value, prerequisites = _scenario_spec(str(variable), raw, scenario)
            profile_env: dict[str, str | None] = {}
            if str(variable) in {
                "SHERPA_HISTORY_TURNS",
                "SHERPA_HISTORY_MSG_CHARS",
                "SHERPA_HISTORY_CHAR_BUDGET",
            }:
                # Codex native resume keeps the provider session independently
                # of Sherpa's history priming limits.  Use a real stateless
                # provider so this matrix isolates SHERPA_HISTORY_* itself.
                profile_env["SHERPA_AGENT"] = "openai"
                prerequisites = tuple(dict.fromkeys((*prerequisites, "OPENAI_API_KEY")))
            expected_outcome, error_patterns, failure_stage, error_sources = _expected_outcome_spec(str(variable), raw, scenario)
            generated = GeneratedEnvScenario(
                variable=str(variable),
                scenario=scenario,
                scenario_set=scenario_set,
                category=category,
                secret=bool(raw.get("secret")),
                declared_restart=declared_restart,
                observables=observables,
                process_mode=process_mode,
                process_value=process_value,
                env_file_mode=file_mode,
                env_file_value=file_value,
                prerequisites=prerequisites,
                expected_outcome=expected_outcome,
                expected_error_patterns=error_patterns,
                expected_error_sources=error_sources,
            )
            expected_rejection = expected_outcome == "reject"
            restart = "none"
            if scenario == "restart":
                restart = "stack" if declared_restart == "stack" else "app"
            profiles.append(
                EnvProfile(
                    name=generated_profile_name(str(variable), scenario),
                    suites=("env",),
                    env=profile_env,
                    restart=restart,
                    fresh_database=True,
                    expected=(f"{variable} {scenario} scenario is observed through a real isolated process",),
                    observables=observables,
                    marker="environment",
                    expect_startup_failure=expected_rejection,
                    expected_error_patterns=error_patterns,
                    expected_error_sources=error_sources,
                    expected_failure_stage=failure_stage,
                    generated_scenario=generated,
                    case_nodeids=("cases/environment/test_environment.py::test_effective_environment_contract",),
                )
            )
    return profiles


def load_capabilities(config_root: Path) -> list[dict[str, Any]]:
    document = _load_yaml(config_root / "capabilities.full.yaml")
    unsupported_document_keys = sorted(set(map(str, document)) - {"description", "policy", "requirements", "suite", "version"})
    if unsupported_document_keys:
        raise ValueError("capabilities.full.yaml has unsupported top-level declarations: " + ", ".join(unsupported_document_keys))
    if document.get("version") != 1 or document.get("suite") != "full":
        raise ValueError("capabilities.full.yaml must declare version: 1 and suite: full")
    policy = document.get("policy") or {}
    if not isinstance(policy, dict) or policy != _CAPABILITY_POLICY:
        raise ValueError("capabilities.full.yaml policy must exactly match the enforced runner policy")
    requirements = document.get("requirements") or []
    if not isinstance(requirements, list):
        raise ValueError("capabilities.full.yaml requirements must be a list")
    result: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    evidence_paths: set[Path] = set()
    for index, item in enumerate(requirements):
        if not isinstance(item, dict):
            raise ValueError(f"capability requirement #{index + 1} must be a mapping")
        identifier = str(item.get("id") or "")
        if not identifier or identifier in identifiers:
            raise ValueError(f"capability requirement #{index + 1} has a missing or duplicate id")
        identifiers.add(identifier)
        unsupported_requirement_keys = sorted(set(map(str, item)) - _CAPABILITY_REQUIREMENT_KEYS)
        if unsupported_requirement_keys:
            raise ValueError(f"capability {identifier!r} has unsupported declarations: " + ", ".join(unsupported_requirement_keys))
        kind = str(item.get("kind") or "")
        allowed_check_keys = _CAPABILITY_CHECK_KEYS.get(kind)
        if allowed_check_keys is None:
            raise ValueError(f"capability {identifier!r} has unsupported kind {kind!r}")
        check = item.get("check") or {}
        if not isinstance(check, dict):
            raise ValueError(f"capability {identifier!r}.check must be a mapping")
        unsupported_check_keys = sorted(set(map(str, check)) - allowed_check_keys)
        if unsupported_check_keys:
            raise ValueError(f"capability {identifier!r}.check has unsupported declarations: " + ", ".join(unsupported_check_keys))
        phase = str(check.get("phase") or "")
        if phase and phase not in _CAPABILITY_PHASES:
            raise ValueError(f"capability {identifier!r} has invalid phase {phase!r}")
        suites = item.get("required_for", item.get("suites"))
        if not isinstance(suites, list) or not suites or set(map(str, suites)) - {"full", "smoke", "chat", "env"}:
            raise ValueError(f"capability {identifier!r} requires a non-empty valid required_for list")
        for scope_key in ("required_profiles", "excluded_profiles"):
            scopes = item.get(scope_key)
            if scopes is not None and (
                not isinstance(scopes, list) or not scopes or not all(isinstance(value, str) and value for value in scopes)
            ):
                raise ValueError(f"capability {identifier!r}.{scope_key} must be a non-empty string list")
        if "required" in item and not isinstance(item["required"], bool):
            raise ValueError(f"capability {identifier!r}.required must be boolean")
        evidence = str(item.get("evidence") or "")
        expected_evidence = capability_evidence_path(identifier)
        if Path(evidence) != expected_evidence:
            raise ValueError(f"capability {identifier!r}.evidence must be its canonical response path: {expected_evidence}")
        if expected_evidence in evidence_paths:
            raise ValueError(f"capability {identifier!r} collides with another canonical evidence path")
        evidence_paths.add(expected_evidence)
        result.append(item)
    return result


def validate_capability_profile_scopes(
    requirements: list[dict[str, Any]],
    profiles: list[EnvProfile],
) -> None:
    names_and_suites = [(profile.name, set(profile.suites)) for profile in profiles]
    errors: list[str] = []
    for requirement in requirements:
        requirement_id = str(requirement.get("id") or "unnamed")
        required_for = set(map(str, requirement.get("required_for", requirement.get("suites")) or ()))
        for pattern in requirement.get("required_profiles") or ():
            matches = [name for name, suites in names_and_suites if suites & required_for and fnmatch.fnmatchcase(name, str(pattern))]
            if not matches:
                errors.append(f"{requirement_id}: required profile pattern has no target: {pattern}")
        for pattern in requirement.get("excluded_profiles") or ():
            if not any(suites & required_for and fnmatch.fnmatchcase(name, str(pattern)) for name, suites in names_and_suites):
                errors.append(f"{requirement_id}: excluded profile pattern has no target: {pattern}")
    if errors:
        raise ValueError("invalid capability profile scopes: " + "; ".join(errors))
