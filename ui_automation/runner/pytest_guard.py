"""skip/xfail/未収集を成功として扱わせない pytest plugin。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ui_automation.runner.artifacts import write_private_text_atomic


_skipped: list[dict[str, str]] = []
_xfailed: list[dict[str, str]] = []
_marked_xfail: list[dict[str, str]] = []
_selected: set[str] = set()
_reported: set[str] = set()


def _normalize_nodeid(nodeid: str) -> str:
    path, separator, function = str(nodeid).partition("::")
    normalized = path.replace("\\", "/")
    marker = "/cases/"
    if marker in normalized:
        normalized = "cases/" + normalized.split(marker, 1)[1]
    elif normalized.startswith("ui_automation/cases/"):
        normalized = normalized.removeprefix("ui_automation/")
    return normalized + (f"::{function}" if separator else "")


def pytest_sessionstart(session) -> None:
    _skipped.clear()
    _xfailed.clear()
    _marked_xfail.clear()
    _selected.clear()
    _reported.clear()


def pytest_collection_modifyitems(items) -> None:
    for item in items:
        marks = list(item.iter_markers(name="xfail"))
        if marks:
            _marked_xfail.append({"nodeid": item.nodeid, "reason": str(marks[0].kwargs.get("reason", ""))})


def pytest_collection_finish(session) -> None:
    _selected.update(_normalize_nodeid(item.nodeid) for item in session.items)


def pytest_runtest_logreport(report) -> None:
    _reported.add(_normalize_nodeid(report.nodeid))
    if report.skipped:
        _skipped.append({"nodeid": report.nodeid, "phase": report.when, "reason": str(report.longrepr)})
    if getattr(report, "wasxfail", None):
        _xfailed.append({"nodeid": report.nodeid, "phase": report.when, "reason": str(report.wasxfail)})


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus) -> None:
    reasons: list[str] = []
    if session.testscollected == 0:
        reasons.append("no tests were collected")
    if _skipped:
        reasons.append(f"{len(_skipped)} skipped report(s) are forbidden")
    if _xfailed:
        reasons.append(f"{len(_xfailed)} xfail/xpass report(s) are forbidden")
    if _marked_xfail:
        reasons.append(f"{len(_marked_xfail)} xfail-marked test(s) are forbidden")
    expected_raw = os.environ.get("SHERPA_UI_EXPECTED_NODEIDS_JSON", "")
    expected: set[str] = set()
    if expected_raw:
        try:
            loaded = json.loads(expected_raw)
            if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
                raise ValueError("expected nodeids must be a string list")
            expected = {_normalize_nodeid(item) for item in loaded}
        except (json.JSONDecodeError, ValueError) as exc:
            reasons.append(f"expected nodeid contract is invalid: {type(exc).__name__}")
    if expected:
        missing_selected = sorted(expected - _selected)
        unexpected_selected = sorted(_selected - expected)
        missing_reports = sorted(expected - _reported)
        if missing_selected:
            reasons.append(f"{len(missing_selected)} explicitly requested test(s) were deselected")
        if unexpected_selected:
            reasons.append(f"{len(unexpected_selected)} unrequested test(s) were selected")
        if missing_reports:
            reasons.append(f"{len(missing_reports)} selected test(s) produced no execution report")
    if reasons and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    output = os.environ.get("SHERPA_UI_PYTEST_GUARD_RESULT")
    if output:
        path = Path(output)
        write_private_text_atomic(
            path,
            json.dumps(
                {
                    "status": "FAIL" if reasons else "PASS",
                    "tests_collected": session.testscollected,
                    "reasons": reasons,
                    "skipped": _skipped,
                    "xfailed": _xfailed,
                    "marked_xfail": _marked_xfail,
                    "expected_nodeids": sorted(expected),
                    "selected_nodeids": sorted(_selected),
                    "reported_nodeids": sorted(_reported),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )


def pytest_terminal_summary(terminalreporter) -> None:
    if _skipped or _xfailed or _marked_xfail:
        terminalreporter.write_sep("=", "UI automation strict outcome policy")
        for item in _skipped:
            terminalreporter.write_line(f"FAIL forbidden skip: {item['nodeid']} ({item['phase']})")
        for item in _xfailed:
            terminalreporter.write_line(f"FAIL forbidden xfail/xpass: {item['nodeid']} ({item['phase']})")
        for item in _marked_xfail:
            terminalreporter.write_line(f"FAIL forbidden xfail marker: {item['nodeid']}")
