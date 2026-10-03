"""Microsoft Excel による表示値補完。

``office_display.enabled`` が有効なときだけ Windows Office worker へ対象セルを問い合わせ、
表示値・実効書式・worker profile を Evidence extension へ追加する。
worker 不在・応答不正・セル欠落のときは Linux 基本経路（``excel_display``）の結果を変えない。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


OFFICE_NATIVE_DISPLAY_PROFILE = "office-native-display-v1"
DISPLAY_SOURCE = "Microsoft Excel"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfficeDisplayConfig:
    enabled: bool = False
    mode: str = "office_com"


@dataclass(frozen=True)
class OfficeDisplayReport:
    enabled: bool
    mode: str
    status: str
    requested_cells: int
    applied_cells: int
    worker_profile: dict[str, Any] | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def config() -> OfficeDisplayConfig:
    """全体設定を読む。未設定・DB 不達は無効。"""
    try:
        from sherpa import store

        raw = store.get_system_settings().get("office_display")
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return OfficeDisplayConfig()
    enabled = raw.get("enabled") is True
    mode = raw.get("mode") if isinstance(raw.get("mode"), str) else "office_com"
    return OfficeDisplayConfig(enabled=enabled, mode=mode)


def config_signature() -> str:
    """Evidence 再生成判定に使う設定署名（worker 到達性には依存しない）。"""
    current = config()
    return f"{OFFICE_NATIVE_DISPLAY_PROFILE}:{'enabled' if current.enabled else 'disabled'}:{current.mode}"


def _targets(ir) -> tuple[dict[str, set[str]], list[tuple[Any, str]]]:
    targets: dict[str, set[str]] = {}
    cells: list[tuple[Any, str]] = []
    for element in ir.elements:
        if element.type != "cell" or not element.locator.sheet or not element.locator.cell_range:
            continue
        coordinate = element.locator.cell_range.upper()
        if ":" in coordinate:
            continue
        targets.setdefault(element.locator.sheet, set()).add(coordinate)
        cells.append((element, coordinate))
    return targets, cells


def _worker_profile(response: dict[str, Any]) -> dict[str, Any]:
    profile = {
        "schema": response.get("schema"),
        "worker_version": response.get("worker_version"),
        "office_app": response.get("office_app"),
        "office_version": response.get("office_version"),
        "worker_profile": response.get("worker_profile"),
    }
    encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    profile["profile_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return profile


def enrich_evidence(ir, source_path: str | Path) -> OfficeDisplayReport:
    """対象セルを Office native の表示値で補完し、実行結果を返す。

    ``source_path`` は旧 XLS でも変換後でなく原本を渡す。応答が無いセルの Linux metadata は上書きしない。
    """
    current = config()
    if not current.enabled:
        return OfficeDisplayReport(False, current.mode, "disabled", 0, 0)
    if current.mode != "office_com":
        return OfficeDisplayReport(True, current.mode, "fallback_linux", 0, 0, reason="unsupported_mode")

    targets, cells = _targets(ir)
    if not cells:
        return OfficeDisplayReport(True, current.mode, "not_applicable", 0, 0)

    try:
        from .arms import legacy_convert

        response = legacy_convert.extract_excel_display(Path(source_path), targets)
    except Exception as exc:
        _log.warning("Office native表示補完を実行できませんでした（Linux結果を維持）: %s", source_path, exc_info=True)
        return OfficeDisplayReport(
            True, current.mode, "fallback_linux", len(cells), 0, reason=f"worker_error:{exc.__class__.__name__}")
    if response is None:
        return OfficeDisplayReport(
            True, current.mode, "fallback_linux", len(cells), 0, reason="microsoft_excel_worker_unavailable")

    by_locator = {
        (item["sheet"], item["cell"]): item
        for item in response.get("cells", [])
        if isinstance(item, dict) and isinstance(item.get("sheet"), str) and isinstance(item.get("cell"), str)
    }
    profile = _worker_profile(response)
    applied = 0
    for element, coordinate in cells:
        native = by_locator.get((element.locator.sheet, coordinate))
        if native is None:
            continue
        # Linux 側の書式は linux_number_format に退避し、number_format を Office の実効値にする
        linux_number_format = element.extension.get("number_format")
        element.extension.update({
            "linux_number_format": linux_number_format,
            "number_format": native["number_format"],
            "office_base_number_format": native.get("base_number_format"),
            "office_number_format_local": native.get("number_format_local"),
            "office_number_format_source": native.get("number_format_source"),
            "office_base_font_color": native.get("base_font_color"),
            "office_base_fill_color": native.get("base_fill_color"),
            "office_display_font_color": native.get("display_font_color"),
            "office_display_fill_color": native.get("display_fill_color"),
            "display_value": native["text"],
            "display_source": DISPLAY_SOURCE,
            "display_status": "rendered",
            "display_reason": None,
            "office_display_profile": profile,
        })
        applied += 1
    status = "applied" if applied == len(cells) else "partial" if applied else "fallback_linux"
    reason = None if applied == len(cells) else "worker_cell_missing"
    return OfficeDisplayReport(True, current.mode, status, len(cells), applied, profile, reason)
