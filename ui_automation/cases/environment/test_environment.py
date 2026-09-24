from __future__ import annotations

import json
import os

import pytest
from playwright.sync_api import expect

from ui_automation.support.config import ROOT
from ui_automation.support.control import (
    restart_application,
    restart_application_with_profile_env,
)
from ui_automation.support.environment_probes import (
    EnvironmentProbeContext,
    ObservedProductError,
    ProbeNotRunAfterProductError,
    declared_observable_ids,
    expected_outcome,
    observe_pairwise_interaction,
    probe_adapter_name,
    probe_pair_adapter_name,
    registry_summary,
    run_environment_probe,
)
from ui_automation.support.ui import ensure_admin_page


def _probe_id(probe) -> str:
    if isinstance(probe, str):
        return probe.strip()
    assert isinstance(probe, dict), f"environment probe must be a string or object: {probe!r}"
    return str(probe.get("id") or "").strip()


@pytest.mark.environment
@pytest.mark.ui_automation
def test_effective_environment_contract(request, page, live_api, ui_config, artifact_case, admin_credentials):
    contract = ui_config.expected_environment()
    assert contract.get("profile") == ui_config.profile, {
        "runner_profile": contract.get("profile"),
        "pytest_profile": ui_config.profile,
    }
    expected = contract["expected"]
    probes = [_probe_id(item) for item in (contract.get("probes") or [])]
    assert probes and all(probes), "current environment profile declared no observable probes"
    matrix_path = ROOT / "ui_automation" / "config" / "env_matrix.yaml"
    registry = registry_summary(matrix_path)
    declared = declared_observable_ids(matrix_path)
    assert registry["declared_count"] == registry["resolved_count"]
    assert registry["pair_semantic_gap_count"] == 0, registry["pair_semantic_gaps"]
    unknown = sorted(set(probes) - declared)
    assert not unknown, "profile declares probes absent from env_matrix registry: " + ", ".join(unknown)

    pairwise_expected = expected.get("pairwise")
    pairwise_observables: set[str] = set()
    if isinstance(pairwise_expected, dict):
        pairwise_observables = {str(value) for value in pairwise_expected.get("observables") or [] if value}
    all_probes = sorted(set(probes) | pairwise_observables)
    cookie_secure = os.environ.get("SHERPA_COOKIE_SECURE", "").strip().casefold() in {"1", "true", "yes", "on"}
    auth_disabled = os.environ.get("SHERPA_AUTH_DISABLED", "").strip().casefold() in {"1", "true", "yes", "on"}
    secure_cookie_over_http = cookie_secure and not auth_disabled and ui_config.base_url.startswith("http://")
    if secure_cookie_over_http:
        page.goto(ui_config.base_url + "/ui/login.html")
        artifact_case.start_trace(page.context)
        admin_page = page
    else:
        admin_page = ensure_admin_page(page, ui_config, artifact_case, admin_credentials)
    context = EnvironmentProbeContext(
        request=request,
        page=admin_page,
        api=live_api,
        config=ui_config,
        evidence=artifact_case,
        contract=contract,
    )
    verified: dict[str, dict] = {}
    failures: list[dict] = []
    product_errors: list[dict] = []
    not_run_after_error: list[dict] = []
    scenario_variable = str((contract.get("generated_scenario") or {}).get("variable") or "")
    for probe_id in all_probes:
        adapter = probe_pair_adapter_name(probe_id, scenario_variable) if scenario_variable else probe_adapter_name(probe_id)
        try:
            verified[probe_id] = run_environment_probe(context, probe_id)
        except ObservedProductError as exc:
            product_errors.append(exc.observation)
            verified[probe_id] = {
                "status": "product-rejected",
                "adapter": adapter,
                "actual_outcome": "explicit-error",
                "observation": exc.observation,
            }
        except ProbeNotRunAfterProductError as exc:
            row = {
                "probe": probe_id,
                "status": "not-run-after-product-error",
                "adapter": adapter,
                "prior_error_evidence_refs": list(exc.observation.get("evidence_refs") or []),
            }
            not_run_after_error.append(row)
            verified[probe_id] = row
        except Exception as exc:
            failure = {
                "probe": probe_id,
                "adapter": adapter,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            failures.append(failure)
            verified[probe_id] = {"status": "failed", **failure}
        artifact_case.write_json(
            "state/environment-probes.json",
            {
                "profile": ui_config.profile,
                "registry": registry,
                "results": verified,
                "pending": sorted(set(all_probes) - set(verified)),
            },
        )

    declared_outcome = expected_outcome(context)
    if declared_outcome == "explicit-error":
        actual_outcome = "explicit-error" if product_errors else "accepted-invalid"
    elif declared_outcome == "accepted-boundary":
        actual_outcome = "accepted-boundary" if not failures else "probe-failure"
    elif declared_outcome == "reject":
        actual_outcome = "unexpected-start"
    else:
        actual_outcome = "observed" if not failures else "probe-failure"
    outcome_matched = actual_outcome == declared_outcome
    artifact_case.write_json(
        "state/environment-outcome.json",
        {
            "expected_outcome": declared_outcome,
            "actual_outcome": actual_outcome,
            "matched": outcome_matched,
            "evidence_refs": [
                "state/environment-probes.json",
                *sorted({reference for observation in product_errors for reference in observation.get("evidence_refs") or []}),
            ],
            "observed_product_error_count": len(product_errors),
            "not_run_after_product_error_count": len(not_run_after_error),
            "probe_failure_count": len(failures),
        },
    )
    if not outcome_matched:
        failures.append(
            {
                "probe": "environment-outcome",
                "adapter": "outcome-correlation",
                "error_type": "EnvironmentOutcomeMismatch",
                "error": (f"expected {declared_outcome} but observed {actual_outcome}"),
            }
        )

    if pairwise_expected is not None:
        assert isinstance(pairwise_expected, dict), "pairwise expectation must be an object"
        pairwise_applied = (contract.get("pairwise") or {}).get("applied") or {}
        assert isinstance(pairwise_applied, dict), "runner pairwise application evidence is absent"
        group_id = str(pairwise_expected.get("id") or "")
        assert group_id and pairwise_applied.get("id") == group_id, "pairwise expected/applied group identifiers differ"
        row_id = str(pairwise_expected.get("row_id") or "")
        assert row_id and pairwise_applied.get("row_id") == row_id, "pairwise expected/applied row identifiers differ"
        expected_factors = {
            str(factor.get("key")): factor
            for factor in pairwise_expected.get("factors") or []
            if isinstance(factor, dict) and factor.get("key")
        }
        applied_factors = {
            str(factor.get("key")): factor
            for factor in pairwise_applied.get("factors") or []
            if isinstance(factor, dict) and factor.get("key")
        }
        assert expected_factors and set(applied_factors) == set(expected_factors), (
            "pairwise applied factors do not match the fixed contract"
        )
        for key, factor in expected_factors.items():
            expectation = str(factor.get("expectation") or "")
            assert expectation in {"literal", "set", "unset"}, f"pairwise factor {key} has an invalid expectation"
            applied = applied_factors[key]
            if expectation == "literal":
                assert applied.get("expectation") == "literal"
                assert str(applied.get("value")) == str(factor.get("value")), f"pairwise literal factor was not applied: {key}"
            else:
                assert applied.get("expectation") == expectation, f"pairwise presence factor was not applied: {key}"
                assert "value" not in factor and "value" not in applied, f"pairwise presence-only factor exposed a value: {key}"
        expected_observables = {str(value) for value in pairwise_expected.get("observables") or []}
        applied_observables = {str(value) for value in pairwise_applied.get("observables") or []}
        assert expected_observables and applied_observables == expected_observables
        successful = {key for key, value in verified.items() if value.get("status") == "verified"}
        assert expected_observables <= successful, (
            "pairwise interaction has declared observables that failed real observation: "
            + ", ".join(sorted(expected_observables - successful))
        )
        try:
            semantic_effects = observe_pairwise_interaction(context)
        except Exception as exc:
            semantic_effects = {
                "id": group_id,
                "row_id": row_id,
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            failures.append(
                {
                    "probe": f"pairwise:{group_id}:{row_id}",
                    "adapter": "pairwise-factor-effects",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        else:
            semantic_effects["status"] = "verified"
        artifact_case.write_json(
            "state/pairwise-observed.json",
            {
                "id": group_id,
                "row_id": row_id,
                "factors": [applied_factors[key] for key in sorted(applied_factors)],
                "observables": {key: verified[key] for key in sorted(expected_observables)},
                "factor_effects": semantic_effects,
            },
        )

    artifact_case.screenshot(admin_page, 10, "environment-real-probe-registry-observed")
    artifact_case.write_json(
        "state/effective-environment-observed.json",
        {
            "profile": ui_config.profile,
            "verified_probes": verified,
            "failure_count": len(failures),
        },
    )
    assert not failures, "real environment probes failed after all declared probes were attempted: " + json.dumps(
        failures, ensure_ascii=False
    )


@pytest.mark.env_seed_lifecycle
@pytest.mark.destructive
def test_fresh_env_seed_then_env_change_then_ui_override_persists(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    contract = ui_config.expected_environment()
    scenario = contract.get("generated_scenario") or {}
    transition = scenario.get("restart_transition") or {}
    observable = transition.get("observable") or {}
    assert observable.get("kind") == "admin_ollama_url_seed", "seed lifecycle profile must declare the central Ollama URL observable"
    assert observable.get("fresh_database") is True, "seed lifecycle profile did not attest that its first stack uses a fresh database"
    fresh_expected = str(observable.get("fresh") or "")
    transition_env = str(observable.get("transition_env") or "")
    ui_override = str(observable.get("ui_override") or "")
    assert all(
        value.startswith("http://127.0.0.1:")
        for value in (
            fresh_expected,
            transition_env,
            ui_override,
        )
    ), "seed lifecycle endpoints must remain on loopback"
    assert len({fresh_expected, transition_env, ui_override}) == 3
    transition_id = str(transition.get("id") or "")
    expected_changed = {str(key) for key in transition.get("changed_keys") or []}
    assert transition_id and "OLLAMA_URL" in expected_changed

    fresh = live_api.get_json(
        "/admin/settings",
        save_as="state/env-seed-fresh-database.json",
    )
    assert (fresh.get("cloud") or {}).get("ollama_url") == fresh_expected, "fresh database did not adopt the declared environment seed"
    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    expect(admin_page.locator("#cloud-ollama-url")).to_have_value(fresh_expected)
    artifact_case.screenshot(admin_page, 10, "environment-fresh-database-adopted-seed")

    transitioned = restart_application_with_profile_env(
        ui_config,
        artifact_case,
        transition_id,
    )
    assert set(transitioned.get("changed_keys") or []) == expected_changed
    after_env_change = live_api.get_json(
        "/admin/settings",
        save_as="state/env-seed-after-profile-env-change.json",
    )
    assert (after_env_change.get("cloud") or {}).get("ollama_url") == fresh_expected, (
        "changing the environment on the same database overwrote the persisted initial seed"
    )
    assert transition_env != fresh_expected
    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    expect(admin_page.locator("#cloud-ollama-url")).to_have_value(fresh_expected)
    artifact_case.screenshot(admin_page, 20, "environment-same-database-kept-persisted-seed")

    admin_page.locator("#cloud-ollama-url").fill(ui_override)
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/admin/settings"),
        timeout=ui_config.timeout_ms,
    ) as save_info:
        admin_page.locator("#save").click()
    assert save_info.value.status == 200, save_info.value.text()
    saved = save_info.value.json()
    assert (saved.get("cloud") or {}).get("ollama_url") == ui_override
    artifact_case.screenshot(admin_page, 30, "environment-ui-override-saved-over-env-default")

    restart_application(ui_config, artifact_case)
    persisted = live_api.get_json(
        "/admin/settings",
        save_as="state/env-seed-ui-override-after-restart.json",
    )
    assert (persisted.get("cloud") or {}).get("ollama_url") == ui_override, (
        "UI-saved central setting did not remain authoritative after application restart"
    )
    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    expect(admin_page.locator("#cloud-ollama-url")).to_have_value(ui_override)
    artifact_case.write_json(
        "state/environment-seed-precedence.json",
        {
            "fresh_seed": fresh_expected,
            "transition_env": transition_env,
            "persisted_after_env_change": fresh_expected,
            "ui_override_after_restart": ui_override,
            "changed_keys": sorted(expected_changed),
        },
    )
    artifact_case.screenshot(admin_page, 40, "environment-ui-override-persisted-after-restart")
