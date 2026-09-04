from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_part(value: str, fallback: str) -> str:
    part = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return part[:100] or fallback


@dataclass(frozen=True)
class UiConfig:
    base_url: str
    artifact_root: Path
    run_id: str
    profile: str
    admin_user: str
    admin_password: str
    admin_changed_password: str
    database_url: str
    world_path: Path
    expected_env_path: Path | None
    control_socket: Path | None
    timeout_ms: int
    headless: bool
    isolated: bool
    expected_auth_disabled: bool | None

    @classmethod
    def from_env(cls) -> "UiConfig":
        base_url = os.environ.get("SHERPA_UI_BASE_URL", "").strip().rstrip("/")
        assert base_url, "SHERPA_UI_BASE_URL is required (an already running real Sherpa instance)"
        parsed = urlsplit(base_url)
        assert parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username, (
            "SHERPA_UI_BASE_URL must be an http(s) URL without embedded credentials"
        )
        run_id = _safe_part(os.environ.get("SHERPA_UI_RUN_ID", "manual"), "manual")
        profile = _safe_part(os.environ.get("SHERPA_UI_ENV_PROFILE", "default"), "default")
        artifact_value = os.environ.get("SHERPA_UI_ARTIFACT_DIR")
        artifact_root = Path(artifact_value).resolve() if artifact_value else (ROOT / "artifacts" / run_id).resolve()
        expected_env = os.environ.get("SHERPA_UI_EXPECTED_ENV_JSON", "").strip()
        expected_auth = os.environ.get("SHERPA_UI_EXPECT_AUTH_DISABLED")
        control_socket = os.environ.get("SHERPA_UI_CONTROL_SOCKET", "").strip()
        return cls(
            base_url=base_url,
            artifact_root=artifact_root,
            run_id=run_id,
            profile=profile,
            admin_user=os.environ.get("SHERPA_UI_ADMIN_USER", "admin").strip() or "admin",
            admin_password=os.environ.get("SHERPA_UI_ADMIN_PASSWORD", ""),
            admin_changed_password=os.environ.get("SHERPA_UI_ADMIN_CHANGED_PASSWORD", ""),
            database_url=os.environ.get("SHERPA_UI_DATABASE_URL", "").strip(),
            world_path=Path(os.environ.get("SHERPA_UI_WORLD_PATH", str(ROOT / "fixtures" / "world"))).resolve(),
            expected_env_path=Path(expected_env).resolve() if expected_env else None,
            control_socket=Path(control_socket).resolve() if control_socket else None,
            timeout_ms=max(int(os.environ.get("SHERPA_UI_TIMEOUT_MS", "120000")), 1000),
            headless=_flag("SHERPA_UI_BROWSER_HEADLESS", True),
            isolated=_flag("SHERPA_UI_ISOLATED", False),
            expected_auth_disabled=(None if expected_auth is None else _flag("SHERPA_UI_EXPECT_AUTH_DISABLED")),
        )

    def require_admin_password(self) -> str:
        assert self.admin_password, "SHERPA_UI_ADMIN_PASSWORD is required; credentials are never inferred"
        return self.admin_password

    def require_admin_changed_password(self) -> str:
        value = self.admin_changed_password
        assert value, "SHERPA_UI_ADMIN_CHANGED_PASSWORD is required so a fresh database can complete the real first-login password change"
        assert value != self.require_admin_password(), "initial and changed admin passwords must differ"
        assert len(value) >= 8 and all(33 <= ord(char) <= 126 for char in value), (
            "SHERPA_UI_ADMIN_CHANGED_PASSWORD must satisfy the product password character contract"
        )
        lowered = value.lower()
        assert "admin" not in lowered and "password" not in lowered, "SHERPA_UI_ADMIN_CHANGED_PASSWORD contains a product-rejected word"
        return value

    def require_isolated(self) -> None:
        assert self.isolated, "This case changes real state and requires a runner-created isolated stack (SHERPA_UI_ISOLATED=1)"

    def require_control_socket(self) -> Path:
        assert self.control_socket is not None, "SHERPA_UI_CONTROL_SOCKET is required for the real application restart case"
        assert self.control_socket.exists(), f"runner control socket is absent: {self.control_socket}"
        return self.control_socket

    def expected_environment(self) -> dict:
        assert self.expected_env_path is not None, "SHERPA_UI_EXPECTED_ENV_JSON is required for environment cases"
        assert self.expected_env_path.is_file(), f"expected environment evidence is missing: {self.expected_env_path}"
        data = json.loads(self.expected_env_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict) and isinstance(data.get("expected"), dict), (
            "effective environment evidence must contain an expected object"
        )
        return data
