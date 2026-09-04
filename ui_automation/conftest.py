from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from ui_automation.support.artifacts import CaseEvidence, case_directory
from ui_automation.support.config import UiConfig
from ui_automation.support.live_api import LiveApi
from ui_automation.support.ui import AdminCredentials, ensure_admin_page
from ui_automation.support.world import ensure_real_world

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightError = RuntimeError
    sync_playwright = None


_MARKERS = {
    "ui_automation": "independent real-service UI automation",
    "smoke": "minimum real-stack UI path",
    "auth": "authentication and authorization",
    "navigation": "navigation and responsive shell",
    "worlds_ingest": "real World registration and ingestion",
    "search_graph": "real search and graph services",
    "chat": "real AI conversation lifecycle",
    "thought_flow": "SSE and structured execution trace",
    "settings": "personal and system settings",
    "workspace": "real personal workspace storage",
    "admin": "administrator surfaces and audit",
    "environment": "effective environment profile",
    "env_seed_lifecycle": "fresh seed, same-database env transition, and UI override precedence",
    "ingestion_real": "real OCR, VLM, PDF, OOXML, and legacy Office ingestion profile",
    "subagent_planner_real": "real OpenAI planner and delegated subagent profile",
    "auth_expiry_real": "real session expiry during the chat UI path",
    "provider_timeout_real": "real provider timeout without a success fallback",
    "codex_cli_failure_real": "real Codex CLI nonzero exit without a success fallback",
    "provider_profile_real": "real provider selection and usage correlation profile",
    "destructive": "changes only an isolated runner-owned stack",
}


def pytest_configure(config) -> None:
    for name, description in _MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items) -> None:
    first_login = "cases/auth/test_auth.py::test_login_session_logout"
    items.sort(key=lambda item: (0 if item.nodeid.endswith(first_login) else 1, item.nodeid))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


@pytest.fixture(scope="session")
def ui_config() -> UiConfig:
    return UiConfig.from_env()


@pytest.fixture(scope="session")
def live_base_url(ui_config: UiConfig) -> str:
    request = urllib.request.Request(
        ui_config.base_url + "/healthz",
        headers={"Accept": "application/json", "User-Agent": "sherpa-ui-automation-preflight"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            assert response.status == 200, f"real Sherpa health check returned {response.status}"
            payload = json.loads(body.decode("utf-8"))
            assert payload.get("ok") is True, f"real Sherpa is not ready: {payload}"
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise AssertionError(f"real Sherpa is unavailable at {ui_config.base_url}: {exc}") from exc
    return ui_config.base_url


@pytest.fixture(scope="session")
def admin_credentials(ui_config: UiConfig) -> AdminCredentials:
    initial = ui_config.require_admin_password()
    changed = ui_config.require_admin_changed_password()
    return AdminCredentials(
        username=ui_config.admin_user,
        initial_password=initial,
        changed_password=changed,
        active_password=initial,
    )


@pytest.fixture(scope="session")
def browser(ui_config: UiConfig, live_base_url: str):
    assert sync_playwright is not None, "Playwright is required; no browser substitute is allowed"
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(headless=ui_config.headless)
        except PlaywrightError as exc:
            raise AssertionError(f"real Chromium could not start: {exc}") from exc
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def artifact_case(request, ui_config: UiConfig):
    evidence = CaseEvidence(
        case_directory(ui_config.artifact_root, ui_config.profile, request.node.nodeid),
        request.node.nodeid,
    )
    request.node._ui_evidence = evidence
    yield evidence
    report = getattr(request.node, "rep_call", None)
    teardown_errors = getattr(request.node, "_ui_teardown_errors", [])
    outcome = "failed" if teardown_errors else (report.outcome if report is not None else "error")
    error_parts = []
    if report is not None and report.failed:
        error_parts.append(str(report.longrepr))
    error_parts.extend(teardown_errors)
    error = "; ".join(error_parts) or None
    finish_failures = evidence.finish(outcome, error)
    assert not finish_failures, f"artifact evidence finalization failed: {finish_failures}"


@pytest.fixture
def page(browser, ui_config: UiConfig, artifact_case: CaseEvidence, request):
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000},
        accept_downloads=True,
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
    )
    context.set_default_timeout(ui_config.timeout_ms)
    context.set_default_navigation_timeout(ui_config.timeout_ms)
    current = context.new_page()
    artifact_case.attach_page(current)
    try:
        yield current
    finally:
        teardown_errors = []
        report = getattr(request.node, "rep_call", None)
        if report is not None and report.failed and not current.is_closed():
            try:
                feature = artifact_case.case_dir.parent.name
                artifact_case.screenshot(current, 990, f"{feature}-failure-final-state")
            except Exception as exc:
                artifact_case.page_errors.append({"error": f"failure screenshot failed: {exc}"})
        artifact_case.run_cleanups()
        try:
            artifact_case.stop_trace(save=True)
        except Exception as exc:
            teardown_errors.append(f"trace finalization failed: {type(exc).__name__}: {exc}")
        artifact_case.flush()
        try:
            context.close()
        except Exception as exc:
            teardown_errors.append(f"browser context close failed: {type(exc).__name__}: {exc}")
        artifact_case.finalize_request_failure_expectations()
        artifact_case.finalize_console_error_expectations()
        if (report is None or report.passed) and artifact_case.page_errors:
            teardown_errors.append("browser page errors: " + "; ".join(row["error"] for row in artifact_case.page_errors))
        if report is None or report.passed:
            console_errors = [row for row in artifact_case.console if row.get("type") == "error" and not row.get("expected")]
            if console_errors:
                teardown_errors.append("browser console errors: " + "; ".join(str(row.get("text")) for row in console_errors))
            unexpected_request_failures = [row for row in artifact_case.request_failures if not row.get("expected")]
            if unexpected_request_failures:
                teardown_errors.append(
                    "browser request failures: "
                    + "; ".join(f"{row.get('method')} {row.get('url')}: {row.get('failure')}" for row in unexpected_request_failures)
                )
        if artifact_case.cleanup_errors:
            teardown_errors.append("cleanup failures: " + "; ".join(artifact_case.cleanup_errors))
        request.node._ui_teardown_errors = teardown_errors
        if teardown_errors:
            pytest.fail("; ".join(teardown_errors))


@pytest.fixture
def admin_page(
    page,
    ui_config: UiConfig,
    artifact_case: CaseEvidence,
    admin_credentials: AdminCredentials,
):
    return ensure_admin_page(page, ui_config, artifact_case, admin_credentials)


@pytest.fixture
def live_api(page, ui_config: UiConfig, artifact_case: CaseEvidence) -> LiveApi:
    return LiveApi(
        ui_config.base_url,
        page.context,
        artifact_case,
        timeout_seconds=max(ui_config.timeout_ms / 1000, 10),
    )


@pytest.fixture
def isolated_stack(
    ui_config: UiConfig,
    admin_page,
    live_api: LiveApi,
    artifact_case: CaseEvidence,
) -> None:
    ui_config.require_isolated()
    _ensure_usage_metering(live_api, artifact_case)


def _ensure_usage_metering(live_api: LiveApi, artifact_case: CaseEvidence) -> None:
    system_settings = live_api.get_json("/admin/settings")
    metering = system_settings.get("usage_metering") or {}
    assert isinstance(metering, dict), "system usage-metering view is not structured"
    original_metering = metering.get("configured")
    if metering.get("effective") is not True:
        enabled = live_api.put_json("/admin/settings", {"usage_metering": True})
        assert (enabled.get("usage_metering") or {}).get("effective") is True, (
            "real AI usage metering could not be enabled for the isolated test"
        )
        artifact_case.add_cleanup(
            "restore system usage metering",
            lambda: live_api.put_json(
                "/admin/settings",
                {"usage_metering": original_metering},
            ),
        )
    artifact_case.write_json(
        "state/usage-metering-enabled.json",
        {"effective": True, "restores_configured_value": original_metering},
    )


@pytest.fixture
def real_world(admin_page, live_api: LiveApi, ui_config: UiConfig, artifact_case: CaseEvidence) -> str:
    _ensure_usage_metering(live_api, artifact_case)
    return ensure_real_world(live_api, ui_config, artifact_case)
