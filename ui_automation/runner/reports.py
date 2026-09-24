"""全profileの機械可読・人間可読レポートを生成する。"""

from __future__ import annotations

import html
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ui_automation.runner.artifacts import SecretRedactor, write_private_text_atomic
from ui_automation.runner.models import ProfileResult


def collect_usage_summary(run_root: Path) -> dict[str, Any]:
    """各caseの正規化済み1-turn証跡だけを重複排除して集計する。"""
    paths = sorted(run_root.glob("*/*/*/state/provider-correlation.json*"))
    records_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    errors: list[str] = []
    duplicate_records = 0
    for path in paths:
        relative = path.relative_to(run_root)
        profile = relative.parts[0]
        case = "/".join(relative.parts[1:3])
        try:
            text = path.read_text(encoding="utf-8")
            payloads = [json.loads(line) for line in text.splitlines() if line.strip()] if path.suffix == ".jsonl" else [json.loads(text)]
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{relative}: {type(exc).__name__}")
            continue
        for index, payload in enumerate(payloads, 1):
            if not isinstance(payload, dict):
                errors.append(f"{relative}:{index}: usage record must be an object")
                continue
            turn_id = str(payload.get("turn_id") or "").strip()
            operation = str(payload.get("operation") or "chat.turn").strip()
            provider = str(payload.get("usage_provider") or payload.get("provider") or "").strip().lower()
            model = str(payload.get("usage_model") or payload.get("model") or "").strip()
            if not turn_id or not provider:
                errors.append(f"{relative}:{index}: turn_id/provider is required")
                continue
            try:
                input_tokens = int(payload.get("input_tokens") or 0)
                output_tokens = int(payload.get("output_tokens") or 0)
            except (TypeError, ValueError):
                errors.append(f"{relative}:{index}: token counts must be integers")
                continue
            if input_tokens < 0 or output_tokens < 0 or input_tokens + output_tokens <= 0:
                errors.append(f"{relative}:{index}: token total must be positive")
                continue
            record = {
                "profile": profile,
                "case": case,
                "turn_sha256": hashlib.sha256(turn_id.encode("utf-8")).hexdigest(),
                "operation": operation,
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
            key = (profile, case, turn_id)
            existing = records_by_key.get(key)
            if existing is not None:
                duplicate_records += 1
                if existing != record:
                    errors.append(f"{relative}:{index}: conflicting duplicate turn usage")
                continue
            records_by_key[key] = record
    unreported_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in sorted(run_root.glob("*/*/*/state/provider-usage-unreported.jsonl")):
        relative = path.relative_to(run_root)
        try:
            payloads = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{relative}: {type(exc).__name__}")
            continue
        for index, payload in enumerate(payloads, 1):
            if not isinstance(payload, dict):
                errors.append(f"{relative}:{index}: unreported usage record must be an object")
                continue
            provider = str(payload.get("provider") or "").strip().lower()
            reason = str(payload.get("reason") or "").strip()
            turn_id = str(payload.get("turn_id") or "").strip()
            try:
                calls = int(payload.get("calls") or 0)
            except (TypeError, ValueError):
                calls = 0
            if not provider or not reason or not turn_id or calls <= 0:
                errors.append(f"{relative}:{index}: turn_id/provider/reason/positive calls are required")
                continue
            case = "/".join(relative.parts[1:3])
            record = {
                "profile": relative.parts[0],
                "case": case,
                "turn_sha256": hashlib.sha256(turn_id.encode("utf-8")).hexdigest(),
                "operation": str(payload.get("operation") or "provider.call"),
                "provider": provider,
                "model": str(payload.get("model") or ""),
                "calls": calls,
                "reason": reason,
            }
            key = (relative.parts[0], case, turn_id)
            existing = unreported_by_key.get(key)
            if existing is not None:
                duplicate_records += 1
                if existing != record:
                    errors.append(f"{relative}:{index}: conflicting duplicate unreported usage")
                continue
            unreported_by_key[key] = record
    records = sorted(records_by_key.values(), key=lambda item: (item["profile"], item["case"], item["turn_sha256"]))
    providers: dict[str, dict[str, int]] = {}
    for record in records:
        bucket = providers.setdefault(
            str(record["provider"]),
            {"turns": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        bucket["turns"] += 1
        bucket["input_tokens"] += int(record["input_tokens"])
        bucket["output_tokens"] += int(record["output_tokens"])
        bucket["total_tokens"] += int(record["total_tokens"])
    totals = {
        "turns": len(records),
        "input_tokens": sum(int(item["input_tokens"]) for item in records),
        "output_tokens": sum(int(item["output_tokens"]) for item in records),
        "total_tokens": sum(int(item["total_tokens"]) for item in records),
    }
    unreported_records = sorted(
        unreported_by_key.values(),
        key=lambda item: (item["profile"], item["case"], item["turn_sha256"]),
    )
    return {
        "status": "FAIL" if errors else "PASS",
        "totals": totals,
        "providers": providers,
        "records": records,
        "source_files": len(paths),
        "duplicate_records_ignored": duplicate_records,
        "unreported_usage": {
            "calls": sum(int(item["calls"]) for item in unreported_records),
            "records": unreported_records,
        },
        "errors": errors,
    }


def write_summary(
    *,
    run_root: Path,
    run_id: str,
    suite: str,
    started_at: str,
    finished_at: str,
    results: list[ProfileResult],
    global_failures: list[str],
    usage_summary: dict[str, Any],
    redactor: SecretRedactor,
) -> dict[str, Any]:
    passed = sum(item.status == "PASS" for item in results)
    failed = sum(item.status != "PASS" for item in results)
    status = "PASS" if not global_failures and failed == 0 and results else "FAIL"
    summary = {
        "run_id": run_id,
        "suite": suite,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "profile_counts": {"total": len(results), "passed": passed, "failed": failed},
        "global_failures": global_failures,
        "usage": usage_summary,
        "profiles": [item.as_dict() for item in results],
    }
    redactor.write_json(run_root / "summary.json", summary)
    failure_lines = [f"GLOBAL: {message}" for message in global_failures]
    for result in results:
        for message in result.failures:
            failure_lines.append(f"{result.profile} [{result.stage}]: {message}")
    write_private_text_atomic(
        run_root / "failures.txt",
        redactor.redact_text("\n".join(failure_lines) + ("\n" if failure_lines else "no failures\n")),
    )
    _write_html(run_root / "report.html", summary, redactor)
    _write_junit(run_root / "junit.xml", results, global_failures, redactor)
    return summary


def _write_html(path: Path, summary: dict[str, Any], redactor: SecretRedactor) -> None:
    rows = []
    for profile in summary["profiles"]:
        failures = "<br>".join(html.escape(str(item)) for item in profile["failures"]) or "-"
        counts = ", ".join(f"{key}={value}" for key, value in profile.get("tests", {}).items()) or "-"
        rows.append(
            "<tr>"
            f"<td>{html.escape(profile['profile'])}</td>"
            f'<td class="{profile["status"].lower()}">{html.escape(profile["status"])}</td>'
            f"<td>{html.escape(profile['stage'])}</td>"
            f"<td>{html.escape(counts)}</td>"
            f"<td>{failures}</td>"
            "</tr>"
        )
    global_items = "".join(f"<li>{html.escape(item)}</li>" for item in summary["global_failures"]) or "<li>なし</li>"
    usage = summary.get("usage") or {}
    usage_totals = usage.get("totals") or {}
    document = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>Sherpa UI automation {html.escape(summary["run_id"])}</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;color:#202124}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:.55rem;text-align:left;vertical-align:top}}th{{background:#f1f3f4}}
.pass{{color:#137333;font-weight:700}}.fail{{color:#b3261e;font-weight:700}}code{{background:#f4f4f4;padding:.1rem .3rem}}</style></head>
<body><h1>Sherpa 実サービス UI 自動試験</h1>
<p>Run <code>{html.escape(summary["run_id"])}</code> / suite <code>{html.escape(summary["suite"])}</code> / 
status <strong class="{summary["status"].lower()}">{html.escape(summary["status"])}</strong></p>
<h2>全体失敗</h2><ul>{global_items}</ul>
<h2>実AI利用量</h2><p>turns={int(usage_totals.get("turns") or 0)}, input={int(usage_totals.get("input_tokens") or 0)},
output={int(usage_totals.get("output_tokens") or 0)}, total={int(usage_totals.get("total_tokens") or 0)}</p>
<h2>Profile結果</h2><table><thead><tr><th>Profile</th><th>結果</th><th>段階</th><th>件数</th><th>失敗理由</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></body></html>"""
    write_private_text_atomic(path, redactor.redact_text(document))


def _write_junit(
    path: Path,
    results: list[ProfileResult],
    global_failures: list[str],
    redactor: SecretRedactor,
) -> None:
    root = ET.Element("testsuites")
    for result in results:
        source = path.parent / _profile_part(result.profile) / "reports" / "junit.xml"
        appended = False
        if source.is_file():
            try:
                parsed = ET.parse(source).getroot()
                suites = [parsed] if parsed.tag == "testsuite" else list(parsed.findall("testsuite"))
                for suite in suites:
                    suite.set("name", f"{result.profile}:{suite.attrib.get('name', 'pytest')}")
                    root.append(suite)
                    appended = True
            except (ET.ParseError, OSError):
                appended = False
        if not appended:
            suite = ET.SubElement(root, "testsuite", name=result.profile, tests="1", failures="1" if result.status != "PASS" else "0")
            case = ET.SubElement(suite, "testcase", classname="ui_automation.profile", name=result.profile)
            if result.status != "PASS":
                failure = ET.SubElement(case, "failure", message=result.stage)
                failure.text = "\n".join(result.failures) or "profile failed before pytest"
        elif result.status != "PASS":
            suite = ET.SubElement(
                root,
                "testsuite",
                name=f"{result.profile}:runner",
                tests="1",
                failures="1",
            )
            case = ET.SubElement(suite, "testcase", classname="ui_automation.runner", name=result.profile)
            failure = ET.SubElement(case, "failure", message=result.stage)
            failure.text = "\n".join(result.failures) or "profile failed outside pytest"
    if global_failures:
        suite = ET.SubElement(
            root, "testsuite", name="ui_automation.manifests", tests=str(len(global_failures)), failures=str(len(global_failures))
        )
        for index, message in enumerate(global_failures, 1):
            case = ET.SubElement(suite, "testcase", classname="ui_automation.manifest", name=f"global-{index}")
            failure = ET.SubElement(case, "failure", message="global validation failure")
            failure.text = message
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in root.findall("testsuite"):
        for key in totals:
            try:
                totals[key] += int(suite.attrib.get(key, 0))
            except ValueError:
                pass
    root.attrib.update({key: str(value) for key, value in totals.items()})
    raw = ET.tostring(root, encoding="unicode", xml_declaration=True)
    write_private_text_atomic(path, redactor.redact_text(raw))


def _profile_part(value: str) -> str:
    import re

    clean = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
    return (clean or "profile")[:80]
