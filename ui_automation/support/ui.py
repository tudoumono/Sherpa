from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote


def unique_id(prefix: str) -> str:
    value = f"{prefix}-{time.time_ns()}"
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"{prefix}-{digest}"


def runtime_password() -> str:
    return "Ui-" + secrets.token_urlsafe(18) + "-7!"


@dataclass
class AdminCredentials:
    username: str
    initial_password: str
    changed_password: str
    active_password: str
    initial_change_completed: bool = False


def login_without_trace(
    page,
    base_url: str,
    credentials: AdminCredentials,
    next_path: str,
    timeout_ms: int,
    evidence=None,
) -> bool:
    page.goto(f"{base_url}/ui/login.html?next={quote(next_path, safe='')}")
    page.locator("#username").fill(credentials.username)
    page.locator("#password").fill(credentials.active_password)
    if evidence is not None:
        evidence.arm_control_authorization(page, control_key="submit")
    page.locator("#submit").click()
    target_path = next_path.split("?", 1)[0]
    page.wait_for_function(
        "target => location.pathname === target || location.pathname === '/ui/change-password.html'",
        arg=target_path,
        timeout=timeout_ms,
    )
    if page.locator("#password").count():
        page.locator("#password").fill("")

    changed_now = page.url.split("?", 1)[0].endswith("/ui/change-password.html")
    if changed_now:
        page.locator("#current-password").fill(credentials.active_password)
        page.locator("#new-password").fill(credentials.changed_password)
        page.locator("#confirm-password").fill(credentials.changed_password)
        page.locator("#submit").click()
        page.wait_for_url(f"**{target_path}**", timeout=timeout_ms)
        for selector in ("#current-password", "#new-password", "#confirm-password"):
            if page.locator(selector).count():
                page.locator(selector).fill("")
        credentials.active_password = credentials.changed_password
        credentials.initial_change_completed = True
    return changed_now


def ensure_admin_page(page, config, evidence, credentials: AdminCredentials, path: str = "/ui/chat.html"):
    target = config.base_url + path
    evidence.stop_trace(save=False)
    evidence.begin_auth_bootstrap()
    try:
        page.goto(target)
        if "/ui/login.html" in page.url:
            login_without_trace(
                page,
                config.base_url,
                credentials,
                path,
                config.timeout_ms,
                evidence,
            )
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(100)
    finally:
        evidence.end_auth_bootstrap()
    evidence.start_trace(page.context)
    return page
