"""画面・API・case・環境変数台帳の未登録driftを検出する。"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import shlex
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ui_automation.runner.config import generate_environment_profiles, generate_pairwise_profiles
from ui_automation.runner.filesystem_safety import assert_no_mount_targets
from ui_automation.runner.models import VALID_REJECTION_ERROR_SOURCES
from ui_automation.support.environment_probes import (
    OBSERVABLE_ADAPTER_IDS,
    declared_observable_ids,
    probe_adapter_name,
    registry_summary,
)


_EXPECTED_OUTCOMES = frozenset({"reject", "explicit-error", "accepted-boundary"})
_ERROR_SOURCES = frozenset({"ui", "api", "service-log"})

_INTERACTIVE_TAGS = frozenset({"button", "select", "textarea", "summary"})
_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "switch",
        "tab",
        "textbox",
    }
)
_NON_IDENTITY_CLASSES = frozenset(
    {
        "act-btn",
        "btn-ghost",
        "btn-primary",
        "btn-secondary",
        "danger",
        "filterchip",
        "iconbtn",
        "tab-link",
        "mini",
        "on",
        "small",
    }
)
_HREF_PREFERRED_CLASSES = frozenset({"crumb", "help-link", "tab-link"})
_SHARED_NAV_CONTROL_IDS = frozenset({"healthdot", "themebtn", "topbar-user", "turnnotice", "um-changepw", "um-logout"})
_CONTROL_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*")
_CONTROL_ID_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{1,119}[-_.:]")
_CONTROL_CLASS = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_CONTROL_DATA_ATTRIBUTE = re.compile(r"data-[a-z0-9_-]+")
_SAFE_RELATIVE_HREF = re.compile(r"(?:/|[A-Za-z0-9])[A-Za-z0-9._~!&'()*+,;=:@%/+-]*")


_ENV_PATTERNS = (
    re.compile(r"(?:os\.)?(?:environ\.(?:get|setdefault|pop)|getenv)\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"(?:os\.)?environ\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"),
    re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)"),
)


def _yaml(path: Path) -> dict[str, Any]:
    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"manifest root must be a mapping: {path}")
    return loaded


def _canonical_path(path: str) -> str:
    value = path.strip().split("?", 1)[0]
    value = re.sub(r"\$\{[^}]+\}", "{}", value)
    value = re.sub(r"\{[^}/]+\}", "{}", value)
    value = re.sub(r"//+", "/", value)
    return value.rstrip("/") or "/"


def _case_exists(ui_root: Path, nodeid: str) -> tuple[bool, str]:
    relative, separator, function = nodeid.partition("::")
    path = ui_root / relative
    if path.is_symlink() or not path.is_file():
        return False, f"case file is missing: {nodeid}"
    if not separator:
        return False, f"case nodeid must name a test function: {nodeid}"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return False, f"case cannot be parsed: {nodeid}: {exc}"
    names = {item.name for item in ast.walk(tree) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if function not in names:
        return False, f"case function is missing: {nodeid}"
    return True, ""


def _discover_case_nodeids(ui_root: Path) -> set[str]:
    discovered: set[str] = set()
    cases_root = ui_root / "cases"
    assert_no_mount_targets(cases_root)
    for path in sorted(cases_root.rglob("test_*.py")):
        if path.is_symlink():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        relative = path.relative_to(ui_root)
        for item in tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                discovered.add(f"{relative}::{item.name}")
    return discovered


def _case_function_source(ui_root: Path, nodeid: str) -> str:
    relative, separator, function = nodeid.partition("::")
    if not separator:
        return ""
    path = ui_root / relative
    if path.is_symlink():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return ""
    lines = text.splitlines()
    for item in ast.walk(tree):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function:
            return "\n".join(lines[item.lineno - 1 : item.end_lineno])
    return ""


def _normalize_control_href(raw_value: str) -> str | None:
    """Return a non-secret, exact href identity shared by discovery and runtime.

    Query strings and fragments are deliberately not part of a control key: both
    may contain conversation IDs, share tokens, or manual anchors generated from
    content.  Dynamic/template hrefs cannot be an exact identity and therefore
    remain an explicitly unkeyed discovery result.
    """

    value = raw_value.strip()
    if not value or len(value) > 140 or any(character in value for character in "\r\n\t${}"):
        return None
    value = value.split("?", 1)[0].split("#", 1)[0]
    if not value:
        return None
    if value.startswith("./"):
        value = value[2:]
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname.lower()
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        authority = host if port is None else f"{host}:{port}"
        path = parsed.path or "/"
        if not _SAFE_RELATIVE_HREF.fullmatch(path):
            return None
        return f"{parsed.scheme.lower()}://{authority}{path}"
    if parsed.netloc or ":" in value.split("/", 1)[0]:
        return None
    if any(part == ".." for part in value.split("/")) or not _SAFE_RELATIVE_HREF.fullmatch(value):
        return None
    return value


def _dynamic_control_id_prefix(raw_value: str) -> str | None:
    marker = raw_value.find("${")
    if marker <= 0:
        return None
    prefix = raw_value[:marker]
    return prefix if _CONTROL_ID_PREFIX.fullmatch(prefix) else None


def _source_line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _mask_javascript_comments(text: str) -> str:
    """Mask JS comments without changing offsets or newlines."""

    output = list(text)
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote = character
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            if end < 0:
                end = len(text)
            for position in range(index, end):
                output[position] = " "
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = len(text) if end < 0 else end + 2
            for position in range(index, end):
                if output[position] != "\n":
                    output[position] = " "
            index = end
            continue
        index += 1
    return "".join(output)


def _record_discovered_control(
    controls: dict[str, dict[str, Any]],
    key: str,
    *,
    tag: str,
    input_type: str,
    role: str,
    source: str,
    line: int,
    excludable: bool = False,
    ambiguity: str = "",
) -> None:
    reference = f"{source}:{line}"
    row = controls.setdefault(
        key,
        {
            "tag": tag,
            "type": input_type,
            "role": role,
            "sources": [],
            "occurrences": 0,
            "excludable": excludable,
            "ambiguity": ambiguity,
        },
    )
    sources = row.setdefault("sources", [])
    if reference not in sources:
        sources.append(reference)
    row["occurrences"] = len(sources)
    row["excludable"] = bool(row.get("excludable")) or excludable
    if ambiguity and not row.get("ambiguity"):
        row["ambiguity"] = ambiguity


def _semantic_classes(raw_value: str) -> list[str]:
    return [item for item in raw_value.split() if _CONTROL_CLASS.fullmatch(item) and item.lower() not in _NON_IDENTITY_CLASSES]


def _unkeyed_control_key(source: str, line: int, tag: str) -> str:
    return f"@unkeyed:{source}:{line}:{tag.lower()}"


def _record_control_element(
    controls: dict[str, dict[str, Any]],
    *,
    tag: str,
    attrs: list[tuple[str, str]],
    source: str,
    line: int,
    force_interactive: bool = False,
) -> None:
    normalized_tag = tag.lower()
    values = {key.lower(): value for key, value in attrs}
    input_type = values.get("type", "").lower()
    role = values.get("role", "").lower()
    interactive = (
        force_interactive
        or normalized_tag in _INTERACTIVE_TAGS
        or normalized_tag == "details"
        or (normalized_tag == "input" and input_type != "hidden")
        or (normalized_tag == "a" and ("href" in values or bool(values.get("id"))))
        or role in _INTERACTIVE_ROLES
        or "tabindex" in values
        or "contenteditable" in values
        or "onclick" in values
    )
    if not interactive:
        return

    control_id = values.get("id", "").strip()
    if _CONTROL_ID.fullmatch(control_id):
        _record_discovered_control(
            controls,
            control_id,
            tag=normalized_tag,
            input_type=input_type,
            role=role,
            source=source,
            line=line,
        )
        return
    id_prefix = _dynamic_control_id_prefix(control_id)
    if id_prefix:
        _record_discovered_control(
            controls,
            f"@id-prefix:{id_prefix}",
            tag=normalized_tag,
            input_type=input_type,
            role=role,
            source=source,
            line=line,
        )
        return

    data_attributes = [key for key, _ in attrs if _CONTROL_DATA_ATTRIBUTE.fullmatch(key.lower())]
    if data_attributes:
        attribute = data_attributes[0].lower()
        _record_discovered_control(
            controls,
            f"@selector:[{attribute}]",
            tag=normalized_tag,
            input_type=input_type,
            role=role,
            source=source,
            line=line,
        )
        return

    classes = _semantic_classes(values.get("class", ""))
    href_preferred = bool(set(classes) & _HREF_PREFERRED_CLASSES)
    if classes and not href_preferred:
        _record_discovered_control(
            controls,
            f"@selector:.{classes[-1]}",
            tag=normalized_tag,
            input_type=input_type,
            role=role,
            source=source,
            line=line,
        )
        return

    href = _normalize_control_href(values.get("href", "")) if normalized_tag == "a" else None
    if href:
        _record_discovered_control(
            controls,
            f"@href:{href}",
            tag=normalized_tag,
            input_type=input_type,
            role=role,
            source=source,
            line=line,
        )
        return

    marker = _unkeyed_control_key(source, line, normalized_tag)
    _record_discovered_control(
        controls,
        marker,
        tag=normalized_tag,
        input_type=input_type,
        role=role,
        source=source,
        line=line,
        excludable=True,
        ambiguity="interactive element has no stable id, data attribute, semantic class, or exact safe href",
    )


class _InteractiveControlParser(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source
        self.controls: dict[str, dict[str, Any]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _record_control_element(
            self.controls,
            tag=tag,
            attrs=[(key, value or "") for key, value in attrs],
            source=self.source,
            line=self.getpos()[0],
        )


def _source_attributes(raw: str) -> list[tuple[str, str]]:
    pattern = re.compile(
        r"(?P<name>[A-Za-z_:][A-Za-z0-9_:.-]*)"
        r"(?:\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+)))?"
    )
    attributes: list[tuple[str, str]] = []
    for match in pattern.finditer(raw):
        value = next(
            (item for item in (match.group("double"), match.group("single"), match.group("bare")) if item is not None),
            "",
        )
        attributes.append((match.group("name").lower(), value))
    return attributes


def _record_event_selector(
    controls: dict[str, dict[str, Any]],
    selector: str,
    *,
    source: str,
    line: int,
) -> None:
    data_attributes = re.findall(r"\[(data-[A-Za-z0-9_-]+)(?:[^\]]*)\]", selector)
    if data_attributes:
        _record_discovered_control(
            controls,
            f"@selector:[{data_attributes[-1].lower()}]",
            tag="event-target",
            input_type="",
            role="",
            source=source,
            line=line,
        )
        return
    raw_classes = re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", selector)
    target_class = raw_classes[-1] if raw_classes else ""
    if target_class and target_class.lower() not in _NON_IDENTITY_CLASSES:
        _record_discovered_control(
            controls,
            f"@selector:.{target_class}",
            tag="event-target",
            input_type="",
            role="",
            source=source,
            line=line,
        )
        return
    if target_class:
        # A utility class such as .filterchip cannot safely identify the event
        # target.  Static markup may still expose a narrower data-* identity.
        return
    ids = re.findall(r"#([A-Za-z][A-Za-z0-9_.:-]*)", selector)
    if ids:
        _record_discovered_control(
            controls,
            ids[-1],
            tag="event-target",
            input_type="",
            role="",
            source=source,
            line=line,
        )
        return
    _record_discovered_control(
        controls,
        _unkeyed_control_key(source, line, "event-target"),
        tag="event-target",
        input_type="",
        role="",
        source=source,
        line=line,
        excludable=True,
        ambiguity=f"event selector has no narrow stable identity: {selector[:80]}",
    )


def _discover_interactive_controls(repository: Path, discovery: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for pattern in discovery.get("html_globs") or ["web/*.html"]:
        for path in sorted(repository.glob(str(pattern))):
            if not path.is_file():
                continue
            source = path.relative_to(repository).as_posix()
            parser = _InteractiveControlParser(source)
            parser.feed(path.read_text(encoding="utf-8", errors="replace"))
            result[f"/ui/{path.name}"] = parser.controls
    dynamic_tag_pattern = re.compile(
        r"<(?P<tag>[A-Za-z][A-Za-z0-9-]*)\b(?P<attributes>[^>]*)>",
        re.IGNORECASE,
    )
    assigned_event_selector_pattern = re.compile(
        r"(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*"
        r"(?:[A-Za-z_$][\w$]*\.)?target\.closest\(\s*['\"](?P<selector>[^'\"]+)['\"]\s*\)",
        re.IGNORECASE,
    )
    query_listener_pattern = re.compile(
        r"(?:document\.)?querySelectorAll\(\s*['\"](?P<selector>[^'\"]+)['\"]\s*\)"
        r"\.forEach\(\s*\((?P<variable>[A-Za-z_$][\w$]*)\)\s*=>\s*\{?"
        r"(?P<body>.{0,400}?)\b(?P=variable)\.addEventListener\(",
        re.IGNORECASE | re.DOTALL,
    )
    direct_id_click_pattern = re.compile(
        r"\$\(\s*['\"](?P<id>[A-Za-z][A-Za-z0-9_.:-]*)['\"]\s*\)\.addEventListener\("
        r"\s*['\"]click['\"]\s*,\s*\(\s*\)\s*=>",
        re.IGNORECASE,
    )
    bound_id_pattern = re.compile(
        r"(?:const|let|var)\s+(?P<variable>[A-Za-z_$][\w$]*)\s*=\s*\$\(\s*['\"]"
        r"(?P<id>[A-Za-z][A-Za-z0-9_.:-]*)['\"]\s*\)",
        re.IGNORECASE,
    )
    nav_href_pattern = re.compile(r"\[\s*['\"](?P<href>[^'\"]+\.html)['\"]\s*,\s*['\"]", re.IGNORECASE)
    for pattern in discovery.get("javascript_globs") or ["web/*.js", "web/chat/*.js"]:
        for path in sorted(repository.glob(str(pattern))):
            if not path.is_file():
                continue
            source = path.relative_to(repository).as_posix()
            if path.name in {"common.js", "nav.js"}:
                # nav.js is one shared light-DOM component.  Inventory it once on
                # the application-shell surface.  common.js is likewise scanned
                # once; its temporary download anchor must be explicitly excluded.
                page = "/ui/home.html"
            elif path.parent.name == "chat" or path.name == "chat.js":
                page = "/ui/chat.html"
            else:
                page = f"/ui/{path.stem}.html"
            if page not in result:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            scan_text = _mask_javascript_comments(text)
            for tag_match in dynamic_tag_pattern.finditer(scan_text):
                attributes = _source_attributes(tag_match.group("attributes"))
                values = dict(attributes)
                if path.name == "nav.js" and tag_match.group("tag").lower() == "a" and "${href}" in values.get("href", ""):
                    # The exact href values are taken from the three fixed nav arrays
                    # below.  Treating this template placeholder as an unkeyed anchor
                    # would double-count the same shared controls.
                    continue
                _record_control_element(
                    result[page],
                    tag=tag_match.group("tag"),
                    attrs=attributes,
                    source=source,
                    line=_source_line(text, tag_match.start()),
                )

            for selector_match in assigned_event_selector_pattern.finditer(scan_text):
                _record_event_selector(
                    result[page],
                    selector_match.group("selector"),
                    source=source,
                    line=_source_line(text, selector_match.start()),
                )
            for selector_match in query_listener_pattern.finditer(scan_text):
                _record_event_selector(
                    result[page],
                    selector_match.group("selector"),
                    source=source,
                    line=_source_line(text, selector_match.start()),
                )
            for listener_match in direct_id_click_pattern.finditer(scan_text):
                if path.name != "nav.js" and listener_match.group("id") in _SHARED_NAV_CONTROL_IDS:
                    continue
                _record_discovered_control(
                    result[page],
                    listener_match.group("id"),
                    tag="event-target",
                    input_type="",
                    role="",
                    source=source,
                    line=_source_line(text, listener_match.start()),
                )
            for binding_match in bound_id_pattern.finditer(scan_text):
                variable = binding_match.group("variable")
                tail = scan_text[binding_match.end() : binding_match.end() + 4000]
                redeclared = re.search(rf"(?:const|let|var)\s+{re.escape(variable)}\s*=", tail)
                if redeclared:
                    tail = tail[: redeclared.start()]
                if not re.search(
                    rf"\b{re.escape(variable)}\.addEventListener\(\s*['\"]click['\"]\s*,\s*\(\s*\)\s*=>",
                    tail,
                ):
                    continue
                if path.name != "nav.js" and binding_match.group("id") in _SHARED_NAV_CONTROL_IDS:
                    continue
                _record_discovered_control(
                    result[page],
                    binding_match.group("id"),
                    tag="event-target",
                    input_type="",
                    role="",
                    source=source,
                    line=_source_line(text, binding_match.start()),
                )

            if path.name == "nav.js":
                for href_match in nav_href_pattern.finditer(scan_text):
                    href = _normalize_control_href(href_match.group("href"))
                    if href:
                        _record_discovered_control(
                            result[page],
                            f"@href:{href}",
                            tag="a",
                            input_type="",
                            role="link",
                            source=source,
                            line=_source_line(text, href_match.start()),
                        )

            create_pattern = re.compile(
                r"(?:const|let|var)\s+(?P<variable>[A-Za-z_$][\w$]*)\s*=\s*document\.createElement\(\s*['\"]"
                r"(?P<tag>[A-Za-z][A-Za-z0-9-]*)['\"]\s*\)",
                re.IGNORECASE,
            )
            for created in create_pattern.finditer(scan_text):
                variable, tag = created.group("variable"), created.group("tag").lower()
                if path.name == "nav.js" and tag == "a":
                    continue
                tail = scan_text[created.end() : created.end() + 2400]
                boundary = re.search(rf"(?:const|let|var)\s+{re.escape(variable)}\s*=", tail)
                if boundary:
                    tail = tail[: boundary.start()]
                attrs: list[tuple[str, str]] = []
                for name, expression in re.findall(
                    rf"\b{re.escape(variable)}\.(id|className|href|type)\s*=\s*([^;\n]+)",
                    tail,
                ):
                    literal = _js_literal(expression.strip())
                    attrs.append(("class" if name == "className" else name.lower(), literal if literal is not None else "${dynamic}"))
                for name in re.findall(rf"\b{re.escape(variable)}\.dataset\.([A-Za-z][A-Za-z0-9_]*)\s*=", tail):
                    attribute = "data-" + re.sub(r"(?<!^)([A-Z])", r"-\1", name).lower()
                    attrs.append((attribute, ""))
                for name, raw_value in re.findall(
                    rf"\b{re.escape(variable)}\.setAttribute\(\s*['\"]([^'\"]+)['\"]\s*,\s*([^\)]+)\)",
                    tail,
                ):
                    literal = _js_literal(raw_value.strip())
                    attrs.append((name.lower(), literal if literal is not None else "${dynamic}"))
                class_additions = re.findall(
                    rf"\b{re.escape(variable)}\.classList\.add\(\s*['\"]([A-Za-z][A-Za-z0-9_-]*)['\"]",
                    tail,
                )
                if class_additions and not any(name == "class" for name, _ in attrs):
                    attrs.append(("class", " ".join(class_additions)))
                _record_control_element(
                    result[page],
                    tag=tag,
                    attrs=attrs,
                    source=source,
                    line=_source_line(text, created.start()),
                    force_interactive=bool(re.search(rf"\b{re.escape(variable)}\.addEventListener\(", tail)),
                )

            dynamic_create_pattern = re.compile(
                r"(?:const|let|var)\s+(?P<variable>[A-Za-z_$][\w$]*)\s*=\s*document\.createElement\(\s*"
                r"(?P<tag_variable>[A-Za-z_$][\w$]*)\s*\)",
                re.IGNORECASE,
            )
            for created in dynamic_create_pattern.finditer(scan_text):
                variable = created.group("variable")
                tag_variable = created.group("tag_variable")
                tail = scan_text[created.end() : created.end() + 1800]
                if not re.search(rf"\b{re.escape(tag_variable)}\s*===?\s*['\"]A['\"]", tail, re.IGNORECASE):
                    continue
                if not re.search(rf"\b{re.escape(variable)}\.setAttribute\(\s*['\"]href['\"]", tail, re.IGNORECASE):
                    continue
                _record_discovered_control(
                    result[page],
                    _unkeyed_control_key(source, _source_line(text, created.start()), "a"),
                    tag="a",
                    input_type="",
                    role="link",
                    source=source,
                    line=_source_line(text, created.start()),
                    excludable=True,
                    ambiguity="content-derived sanitized anchor has no finite exact href inventory",
                )

            svg_control_pattern = re.compile(
                r"_svgEl\(\s*['\"](?P<tag>[A-Za-z][A-Za-z0-9-]*)['\"]\s*,\s*\{"
                r"(?P<attributes>[^{}]*\btabindex\s*:\s*['\"][^'\"]+['\"][^{}]*)\}\s*\)",
                re.IGNORECASE | re.DOTALL,
            )
            for created in svg_control_pattern.finditer(scan_text):
                attrs = [
                    (name.lower(), value)
                    for name, value in re.findall(
                        r"['\"]?([A-Za-z][A-Za-z0-9_-]*)['\"]?\s*:\s*['\"]([^'\"]*)['\"]",
                        created.group("attributes"),
                    )
                ]
                _record_control_element(
                    result[page],
                    tag=created.group("tag"),
                    attrs=attrs,
                    source=source,
                    line=_source_line(text, created.start()),
                )
    return result


def _split_js_top_level(value: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    start = 0
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, character in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"', "`"}:
            quote = character
            continue
        if character in "([{":
            stack.append(character)
            continue
        if character in ")]}" and stack and stack[-1] == pairs[character]:
            stack.pop()
            continue
        if character == delimiter and not stack:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _iter_js_api_calls(text: str):
    call_pattern = re.compile(r"(?<![A-Za-z0-9_$])(?P<name>(?:Sherpa\.)?api|fetch|getJSON|EventSource)\s*\(")
    pairs = {")": "(", "]": "[", "}": "{"}
    for match in call_pattern.finditer(text):
        opening = match.end() - 1
        stack = ["("]
        quote: str | None = None
        escaped = False
        argument_start = opening + 1
        arguments: list[str] = []
        for index in range(opening + 1, len(text)):
            character = text[index]
            if quote is not None:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
                continue
            if character in {"'", '"', "`"}:
                quote = character
                continue
            if character in "([{":
                stack.append(character)
                continue
            if character in ")]}" and stack and stack[-1] == pairs[character]:
                stack.pop()
                if not stack:
                    arguments.append(text[argument_start:index].strip())
                    yield match.group("name"), arguments, match.start()
                    break
                continue
            if character == "," and len(stack) == 1:
                arguments.append(text[argument_start:index].strip())
                argument_start = index + 1


def _js_literal(part: str) -> str | None:
    value = part.strip()
    if len(value) < 2 or value[0] not in {"'", '"', "`"} or value[-1] != value[0]:
        return None
    if value[0] == "`":
        body = value[1:-1].replace("\\`", "`")
        return re.sub(r"\$\{[^}]*\}", "{}", body)
    try:
        loaded = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value[1:-1]
    return str(loaded) if isinstance(loaded, str) else None


def _js_bindings(text: str) -> dict[str, list[tuple[int, str]]]:
    """Return simple, ordered JS variable assignments.

    This is deliberately narrower than a JavaScript evaluator.  A name is only
    resolved when it has one preceding assignment at the call site; ambiguous
    names remain unresolved and must be classified explicitly by the manifest.
    Counting later reassignments is important: treating only a declaration as
    authoritative could turn ``let url = "/safe"; url = user; fetch(url)`` into
    a false static discovery.
    """

    declaration = re.compile(r"(?m)^\s*(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(.+?)\s*;\s*(?://.*)?$")
    reassignment = re.compile(r"(?m)^\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?!=)(.+?)\s*;\s*(?://.*)?$")
    bindings: dict[str, list[tuple[int, str]]] = {}
    for pattern in (declaration, reassignment):
        for match in pattern.finditer(text):
            bindings.setdefault(match.group(1), []).append((match.start(), match.group(2).strip()))
    for rows in bindings.values():
        rows.sort(key=lambda row: row[0])
    return bindings


def _js_url_expression(
    expression: str,
    *,
    bindings: dict[str, list[tuple[int, str]]] | None = None,
    before: int | None = None,
    seen: frozenset[str] = frozenset(),
    relative_base: str | None = None,
) -> str | None:
    stripped = expression.strip()
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", stripped) and bindings is not None:
        candidates = [row for row in bindings.get(stripped, ()) if before is None or row[0] < before]
        if len(candidates) != 1 or stripped in seen:
            return None
        return _js_url_expression(
            candidates[0][1],
            bindings=bindings,
            before=candidates[0][0],
            seen=seen | {stripped},
            relative_base=relative_base,
        )
    parts = _split_js_top_level(expression, "+")
    output: list[str] = []
    for part in parts:
        literal = _js_literal(part)
        nested = None
        identifier = part.strip()
        if literal is None and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", identifier) and bindings is not None:
            candidates = [row for row in bindings.get(identifier, ()) if before is None or row[0] < before]
            if len(candidates) == 1 and identifier not in seen:
                nested = _js_literal(candidates[0][1])
                if nested is None:
                    nested = _js_url_expression(
                        identifier,
                        bindings=bindings,
                        before=before,
                        seen=seen,
                        relative_base=relative_base,
                    )
        value = literal if literal is not None else nested if nested is not None else "{}"
        if value == "{}" and re.search(r"['\"]\?", part):
            value = "?{}"
        if value == "{}" and output and output[-1] == "{}":
            continue
        output.append(value)
    path = "".join(output)
    if not path.replace("{}", "").replace("?", ""):
        return None
    if not path.startswith("/") and relative_base and not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", path):
        path = relative_base.rstrip("/") + "/" + path.lstrip("/")
    if not path.startswith("/"):
        return None
    return _canonical_path(path)


def _control_key_referenced(control_key: str, sources: list[str], *, page: str | None = None) -> bool:
    if control_key.startswith("@unkeyed:"):
        # An unkeyed product control can only be referenced by the exact source
        # identity passed to ArtifactCase.arm_unkeyed_control().  Partial source
        # paths or line-only matches are deliberately insufficient.
        return any(control_key in source for source in sources)
    if control_key.startswith("@id-prefix:"):
        prefix = control_key.removeprefix("@id-prefix:")
        return any(f"#{prefix}" in source or f'id^="{prefix}"' in source or f"id^='{prefix}'" in source for source in sources)
    if control_key.startswith("@href:"):
        href = control_key.removeprefix("@href:")
        escaped_href = re.escape(href)
        exact_selector = re.compile(rf"\[href\s*=\s*['\"]{escaped_href}(?:[?#][^'\"]*)?['\"]\]", re.IGNORECASE)
        data_driven_nav = re.compile(r"#sherpa-nav\s+a", re.IGNORECASE)
        page_visit = None
        if page and page.startswith("/ui/"):
            page_visit = re.compile(
                rf"\.goto\([^\n]*['\"]{re.escape(page)}(?:[?#][^'\"]*)?['\"]",
                re.IGNORECASE,
            )
        for source in sources:
            # nav.js is intentionally represented by one application-shell
            # surface.  Do not let its data-driven test prove a same-href link
            # rendered on an unrelated page.
            if (
                page == "/ui/home.html"
                and re.search(rf"['\"]{escaped_href}['\"]", source)
                and data_driven_nav.search(source)
                and re.search(r"\blink\.click\(", source)
            ):
                return True
            for selector_match in exact_selector.finditer(source):
                if page_visit is not None:
                    visits = list(page_visit.finditer(source, 0, selector_match.start()))
                    if not visits:
                        continue
                    last_other_visit = max(
                        (match.start() for match in re.finditer(r"\.goto\([^\n]*['\"]/ui/", source[: selector_match.start()])),
                        default=-1,
                    )
                    if visits[-1].start() < last_other_visit:
                        continue
                if re.search(r"\.click\(", source[selector_match.start() : selector_match.start() + 1200]):
                    return True
        return False
    escaped = re.escape(control_key)
    patterns = (
        re.compile(rf"#{escaped}(?![A-Za-z0-9_-])"),
        re.compile(rf"get_element_by_id\(\s*['\"]{escaped}['\"]\s*\)", re.IGNORECASE),
        re.compile(rf"getElementById\(\s*['\"]{escaped}['\"]\s*\)"),
    )
    return any(pattern.search(source) for source in sources for pattern in patterns)


def _dynamic_selector_referenced(selector: str, sources: list[str]) -> bool:
    attribute = re.fullmatch(r"\[([A-Za-z0-9_-]+)\]", selector)
    if attribute:
        # Require the exact attribute selector.  A substring lookup would, for
        # example, incorrectly accept [data-toggle-pub] as evidence that a case
        # actually operates [data-toggle].
        exact_attribute = re.compile(
            rf"\[\s*{re.escape(attribute.group(1))}(?:\s*(?:[~|^$*]?=)[^\]]+)?\s*\]",
            re.IGNORECASE,
        )
        return any(exact_attribute.search(source) for source in sources)
    css_class = re.fullmatch(r"\.([A-Za-z][A-Za-z0-9_-]*)", selector)
    if css_class:
        exact_class = re.compile(rf"\.{re.escape(css_class.group(1))}(?![A-Za-z0-9_-])")
        return any(exact_class.search(source) for source in sources)
    return any(selector in source for source in sources)


def _discover_browser_contract(
    repository: Path,
    discovery: dict[str, Any],
) -> tuple[set[str], set[tuple[str, str]], set[str], list[str], list[dict[str, Any]]]:
    html_files: set[Path] = set()
    js_files: set[Path] = set()
    for pattern in discovery.get("html_globs") or ["web/*.html"]:
        html_files.update(path for path in repository.glob(str(pattern)) if path.is_file())
    for pattern in discovery.get("javascript_globs") or ["web/*.js", "web/chat/*.js"]:
        js_files.update(path for path in repository.glob(str(pattern)) if path.is_file())

    pages = {f"/ui/{path.name}" for path in html_files}
    endpoints: set[tuple[str, str]] = set()
    navigation: set[str] = set()
    unresolved_calls: list[dict[str, Any]] = []
    href_pattern = re.compile(r"href\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
    javascript_navigation_literal = re.compile(
        r"\[\s*['\"]([^'\"]+\.html(?:[?#][^'\"]*)?)['\"]\s*,\s*['\"]",
        re.IGNORECASE,
    )
    for path in sorted(html_files | js_files):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = str(path.relative_to(repository))
        bindings = _js_bindings(text) if path.suffix.lower() == ".js" else {}
        if path.suffix.lower() == ".js":
            for match in javascript_navigation_literal.finditer(text):
                href = match.group(1).split("#", 1)[0].split("?", 1)[0]
                navigation.add("/ui/" + Path(href).name)
        for match in href_pattern.finditer(text):
            href = match.group(1).split("#", 1)[0].split("?", 1)[0]
            if not href or href.startswith(("http:", "https:", "mailto:", "#")):
                continue
            if href.endswith(".html"):
                if href.startswith("/"):
                    navigation.add(href)
                else:
                    navigation.add("/ui/" + Path(href).name)
            elif href == "/docs":
                navigation.add(href)
        for call_name, arguments, call_offset in _iter_js_api_calls(text):
            if call_name.endswith("api"):
                if len(arguments) < 2:
                    continue
                method = _js_literal(arguments[0])
                endpoint = _js_url_expression(arguments[1], bindings=bindings, before=call_offset, relative_base="/ui/")
            else:
                if not arguments:
                    continue
                endpoint = _js_url_expression(arguments[0], bindings=bindings, before=call_offset, relative_base="/ui/")
                if call_name in {"getJSON", "EventSource"}:
                    method = "GET"
                else:
                    options = arguments[1] if len(arguments) > 1 else ""
                    method_match = re.search(
                        r"\bmethod\s*:\s*['\"]([A-Z]+)['\"]",
                        options,
                    )
                    method = method_match.group(1) if method_match else "GET"
            if endpoint and method:
                endpoints.add((str(method).upper(), endpoint))
            elif arguments:
                unresolved_calls.append(
                    {
                        "source": relative,
                        "line": _source_line(text, call_offset),
                        "call": call_name,
                        "expression": arguments[1] if call_name.endswith("api") and len(arguments) > 1 else arguments[0],
                    }
                )

    raw_allowances = discovery.get("allow_unresolved_browser_calls") or []
    errors: list[str] = []
    if not isinstance(raw_allowances, list):
        errors.append("discovery.allow_unresolved_browser_calls must be a list")
        raw_allowances = []
    allowed: set[int] = set()
    allowance_rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_allowances, 1):
        if not isinstance(raw, dict):
            errors.append(f"unresolved browser call allowance #{index} must be a mapping")
            continue
        source = str(raw.get("source") or "")
        call = str(raw.get("call") or "")
        expression = str(raw.get("expression") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if not source or not call or not expression or len(reason) < 24:
            errors.append(f"unresolved browser call allowance #{index} requires source, call, exact expression, and a detailed reason")
            continue
        matches = {
            row_index
            for row_index, row in enumerate(unresolved_calls)
            if row["source"] == source and row["call"] == call and row["expression"].strip() == expression
        }
        if len(matches) != 1:
            errors.append(f"unresolved browser call allowance must match exactly one current call: {source}:{call}:{expression}")
            continue
        allowed.update(matches)
        allowance_rows.append({"source": source, "call": call, "expression": expression, "reason": reason})
    for index, row in enumerate(unresolved_calls):
        if index not in allowed:
            errors.append(f"unresolved browser API URL: {row['source']}:{row['line']}:{row['call']}({row['expression']})")
    return pages, endpoints, navigation, errors, allowance_rows


_ROUTE_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head"})
_ROUTE_DECORATORS = _ROUTE_METHODS | {"api_route", "route", "websocket", "websocket_route"}


def _static_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        values: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            values.append(value.value)
        return "".join(values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _discover_backend_routes(
    repository: Path,
    discovery: dict[str, Any],
) -> tuple[set[tuple[str, str]], dict[str, list[str]], list[str]]:
    """Discover literal FastAPI/APIRouter routes declared by product modules.

    Browser-call discovery alone can silently miss a newly added endpoint.  The
    route inventory therefore comes from the product decorators themselves and
    every route must be either mapped to a UI surface or explicitly classified
    as a non-UI route with a detailed reason.
    """

    route_files: set[Path] = set()
    for pattern in discovery.get("route_globs") or ():
        route_files.update(path for path in repository.glob(str(pattern)) if path.is_file())
    routes: set[tuple[str, str]] = set()
    sources: dict[str, list[str]] = {}
    errors: list[str] = []
    trees: dict[str, tuple[str, ast.Module]] = {}
    for path in sorted(route_files):
        relative = str(path.relative_to(repository))
        module = relative.removesuffix(".py").replace("/", ".")
        if module.endswith(".__init__"):
            module = module.removesuffix(".__init__")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, UnicodeError, SyntaxError) as exc:
            errors.append(f"backend route source cannot be parsed: {relative}: {type(exc).__name__}")
            continue
        trees[module] = (relative, tree)

    router_defs: dict[tuple[str, str], dict[str, str]] = {}
    for module, (relative, tree) in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if not isinstance(value, ast.Call):
                continue
            function = value.func
            constructor = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
            if constructor not in {"APIRouter", "FastAPI"}:
                continue
            prefix = ""
            for keyword in value.keywords:
                if keyword.arg == "prefix":
                    literal = _static_string(keyword.value)
                    if literal is None:
                        errors.append(f"backend router prefix must be static: {relative}:{getattr(node, 'lineno', 0)}")
                    else:
                        prefix = literal
            for target in targets:
                if isinstance(target, ast.Name):
                    router_defs[(module, target.id)] = {"prefix": prefix, "kind": constructor}

    module_names = set(trees)
    module_imports: dict[str, dict[str, str]] = {}
    object_imports: dict[str, dict[str, tuple[str, str]]] = {}
    for module, (_, tree) in trees.items():
        modules: dict[str, str] = {}
        objects: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    local = item.asname or item.name.split(".", 1)[0]
                    modules[local] = item.name if item.asname else local
            elif isinstance(node, ast.ImportFrom) and node.module:
                for item in node.names:
                    if item.name == "*":
                        continue
                    local = item.asname or item.name
                    candidate_module = f"{node.module}.{item.name}"
                    if candidate_module in module_names:
                        modules[local] = candidate_module
                    else:
                        objects[local] = (node.module, item.name)
        module_imports[module] = modules
        object_imports[module] = objects

    aliases: dict[tuple[str, str], tuple[str, str]] = {}

    def dotted(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.Attribute):
            return [*dotted(node.value), node.attr]
        return []

    def resolve_router(module: str, node: ast.AST) -> tuple[str, str] | None:
        if isinstance(node, ast.Name):
            local_key = (module, node.id)
            if local_key in router_defs:
                return local_key
            if local_key in aliases:
                return aliases[local_key]
            imported = object_imports.get(module, {}).get(node.id)
            if imported in router_defs:
                return imported
            return None
        parts = dotted(node)
        if len(parts) < 2:
            return None
        imported_module = module_imports.get(module, {}).get(parts[0])
        if imported_module:
            candidate = (".".join([imported_module, *parts[1:-1]]), parts[-1])
        else:
            candidate = (".".join(parts[:-1]), parts[-1])
        if candidate in aliases:
            return aliases[candidate]
        return candidate if candidate in router_defs else None

    changed = True
    while changed:
        changed = False
        for module, (_, tree) in trees.items():
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                resolved = resolve_router(module, node.value)
                if resolved is None:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and (module, target.id) not in aliases:
                        aliases[(module, target.id)] = resolved
                        changed = True

    route_rows: list[tuple[tuple[str, str], str, set[str], str]] = []
    include_edges: list[tuple[tuple[str, str], tuple[str, str], str, str]] = []

    def route_methods(name: str, keywords: list[ast.keyword], relative: str, line: int) -> set[str]:
        if name in _ROUTE_METHODS:
            return {name.upper()}
        if name in {"websocket", "websocket_route", "add_websocket_route"}:
            return {"WEBSOCKET"}
        method_keyword = next((item.value for item in keywords if item.arg == "methods"), None)
        if method_keyword is None:
            return {"GET"}
        if not isinstance(method_keyword, (ast.List, ast.Tuple, ast.Set)):
            errors.append(f"backend route methods must be a static collection: {relative}:{line}")
            return set()
        values = [_static_string(item) for item in method_keyword.elts]
        if any(value is None for value in values):
            errors.append(f"backend route methods must be static: {relative}:{line}")
            return set()
        return {str(value).upper() for value in values}

    for module, (relative, tree) in trees.items():
        for node in ast.walk(tree):
            decorators = node.decorator_list if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else ()
            for decorator in decorators:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                decorator_name = decorator.func.attr.lower()
                if decorator_name not in _ROUTE_DECORATORS:
                    continue
                owner_key = resolve_router(module, decorator.func.value)
                if owner_key is None:
                    errors.append(f"backend route decorator owner cannot be resolved: {relative}:{decorator.lineno}")
                    continue
                if not decorator.args:
                    errors.append(f"backend route has no path argument: {relative}:{decorator.lineno}")
                    continue
                route_path = _static_string(decorator.args[0])
                if route_path is None:
                    errors.append(f"backend route path must be static: {relative}:{decorator.lineno}")
                    continue
                methods = route_methods(decorator_name, decorator.keywords, relative, decorator.lineno)
                route_rows.append((owner_key, route_path, methods, f"{relative}:{decorator.lineno}"))

            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            call_name = node.func.attr.lower()
            owner_key = resolve_router(module, node.func.value)
            if call_name == "include_router":
                if owner_key is None or not node.args:
                    errors.append(f"backend include_router owner or target cannot be resolved: {relative}:{node.lineno}")
                    continue
                target_key = resolve_router(module, node.args[0])
                if target_key is None:
                    errors.append(f"backend include_router target cannot be resolved: {relative}:{node.lineno}")
                    continue
                prefix_node = next((item.value for item in node.keywords if item.arg == "prefix"), None)
                include_prefix = _static_string(prefix_node) if prefix_node is not None else ""
                if include_prefix is None:
                    errors.append(f"backend include_router prefix must be static: {relative}:{node.lineno}")
                    continue
                include_edges.append((owner_key, target_key, include_prefix, f"{relative}:{node.lineno}"))
            elif call_name in {"add_api_route", "add_route", "add_websocket_route"}:
                if owner_key is None:
                    errors.append(f"backend imperative route owner cannot be resolved: {relative}:{node.lineno}")
                    continue
                if not node.args:
                    errors.append(f"backend imperative route has no path argument: {relative}:{node.lineno}")
                    continue
                route_path = _static_string(node.args[0])
                if route_path is None:
                    errors.append(f"backend imperative route path must be static: {relative}:{node.lineno}")
                    continue
                methods = route_methods(call_name, node.keywords, relative, node.lineno)
                route_rows.append((owner_key, route_path, methods, f"{relative}:{node.lineno}"))

    mounted: dict[tuple[str, str], set[str]] = {key: {""} for key, definition in router_defs.items() if definition["kind"] == "FastAPI"}
    changed = True
    while changed:
        changed = False
        for owner, target, include_prefix, _ in include_edges:
            for owner_mount in mounted.get(owner, ()):
                target_mount = _canonical_path(owner_mount + router_defs[owner]["prefix"] + include_prefix)
                if target_mount == "/":
                    target_mount = ""
                if target_mount not in mounted.setdefault(target, set()):
                    mounted[target].add(target_mount)
                    changed = True
    routed_owners = {owner for owner, _, _, _ in route_rows}
    for owner in sorted(routed_owners - set(mounted)):
        errors.append(f"backend router with declared routes is never included: {owner[0]}.{owner[1]}")
    for owner, route_path, methods, source in route_rows:
        definition = router_defs[owner]
        for mount_prefix in mounted.get(owner, ()):
            full_path = _canonical_path(mount_prefix + definition["prefix"] + route_path)
            for method in methods:
                contract = (method, full_path)
                routes.add(contract)
                sources.setdefault(f"{contract[0]} {contract[1]}", []).append(source)
    return routes, {key: sorted(set(value)) for key, value in sorted(sources.items())}, errors


def _route_allowance_matches(method: str, path: str, raw_route: str) -> bool:
    declaration = raw_route.strip()
    candidate_method = ""
    candidate_path = declaration
    first, separator, remainder = declaration.partition(" ")
    if separator and first.upper() in {*(value.upper() for value in _ROUTE_METHODS), "WEBSOCKET"}:
        candidate_method = first.upper()
        candidate_path = remainder.strip()
    if not candidate_path.startswith("/"):
        return False
    if candidate_method and candidate_method != method:
        return False
    normalized_pattern = re.sub(r"//+", "/", candidate_path.rstrip("/") or "/")
    return fnmatch.fnmatchcase(_canonical_path(path), normalized_pattern)


def validate_coverage(repository: Path, config_root: Path) -> dict[str, Any]:
    manifest = _yaml(config_root / "coverage.yaml")
    surfaces = manifest.get("surfaces") or []
    discovery = manifest.get("discovery") or {}
    errors: list[str] = []
    if not isinstance(surfaces, list):
        return {"status": "FAIL", "errors": ["coverage surfaces must be a list"]}
    registered_pages: set[str] = set()
    registered_endpoints: set[tuple[str, str]] = set()
    registered_features: set[str] = set()
    registered_cases: set[str] = set()
    surface_contracts: list[dict[str, Any]] = []
    ui_root = repository / "ui_automation"
    discovered_controls_by_page = _discover_interactive_controls(repository, discovery)
    registered_controls: set[tuple[str, str]] = set()
    excluded_discovered_controls: set[tuple[str, str]] = set()
    discovery_control_exclusions: list[dict[str, Any]] = []
    control_coverage: list[dict[str, Any]] = []
    for surface in surfaces:
        if not isinstance(surface, dict):
            errors.append("coverage surface must be a mapping")
            continue
        page = surface.get("page")
        if page:
            registered_pages.add(str(page))
        for raw in surface.get("endpoints") or ():
            method, separator, endpoint = str(raw).partition(" ")
            if not separator:
                errors.append(f"endpoint must include an HTTP method: {raw}")
                continue
            registered_endpoints.add((method.upper(), _canonical_path(endpoint)))
        registered_features.update(str(item) for item in surface.get("features") or ())
        surface_cases = [str(item) for item in surface.get("cases") or ()]
        surface_contract_cases = set(surface_cases)
        for nodeid in surface_cases:
            registered_cases.add(str(nodeid))
        if page in discovered_controls_by_page and "controls" not in surface:
            errors.append(f"surface {surface.get('id')}: controls inventory is required")
        raw_discovery_exclusions = surface.get("control_exclusions") or []
        if not isinstance(raw_discovery_exclusions, list):
            errors.append(f"surface {surface.get('id')}: control_exclusions must be a list")
            raw_discovery_exclusions = []
        for exclusion_index, raw_exclusion in enumerate(raw_discovery_exclusions, 1):
            if not isinstance(raw_exclusion, dict):
                errors.append(f"surface {surface.get('id')} control exclusion #{exclusion_index}: entry must be a mapping")
                continue
            exclusion_key = str(raw_exclusion.get("key") or "").strip()
            exclusion_reason = str(raw_exclusion.get("reason") or "").strip()
            exclusion_contract = (str(page), exclusion_key)
            discovered = discovered_controls_by_page.get(str(page), {}).get(exclusion_key)
            if not exclusion_key or len(exclusion_reason) < 24:
                errors.append(
                    f"surface {surface.get('id')} control exclusion #{exclusion_index}: exact key and a detailed reason are required"
                )
                continue
            if exclusion_contract in excluded_discovered_controls:
                errors.append(f"duplicate interactive control exclusion: {page}#{exclusion_key}")
                continue
            if not discovered:
                errors.append(f"interactive control exclusion is stale or unknown: {page}#{exclusion_key}")
                continue
            if discovered.get("excludable") is not True:
                errors.append(f"concrete interactive control cannot be excluded from runtime coverage: {page}#{exclusion_key}")
                continue
            excluded_discovered_controls.add(exclusion_contract)
            discovery_control_exclusions.append(
                {
                    "surface": str(surface.get("id") or ""),
                    "page": page,
                    "control_key": exclusion_key,
                    "reason": exclusion_reason,
                    "ambiguity": str(discovered.get("ambiguity") or ""),
                    "sources": list(discovered.get("sources") or ()),
                    "runtime_required": False,
                }
            )
        raw_controls = surface.get("controls", [])
        if not isinstance(raw_controls, list):
            errors.append(f"surface {surface.get('id')}: controls must be a list")
            raw_controls = []
        for entry_index, raw_control in enumerate(raw_controls, 1):
            if not isinstance(raw_control, dict):
                errors.append(f"surface {surface.get('id')} control #{entry_index}: entry must be a mapping")
                continue
            ids_raw = raw_control.get("ids")
            if ids_raw is None:
                ids_raw = [raw_control.get("id")] if raw_control.get("id") else []
            if isinstance(ids_raw, str):
                ids_raw = [ids_raw]
            control_ids: list[str] = []
            for item in ids_raw or ():
                control_id = str(item)
                if not control_id:
                    continue
                if not _CONTROL_ID.fullmatch(control_id):
                    errors.append(f"literal control id is unsafe or malformed: {page}#{control_id}")
                    continue
                control_ids.append(control_id)
            id_prefixes_raw = raw_control.get("id_prefixes") or []
            if isinstance(id_prefixes_raw, str):
                id_prefixes_raw = [id_prefixes_raw]
            id_prefixes = [str(item) for item in id_prefixes_raw if str(item)]
            hrefs_raw = raw_control.get("hrefs") or []
            if isinstance(hrefs_raw, str):
                hrefs_raw = [hrefs_raw]
            hrefs = [str(item) for item in hrefs_raw if str(item)]
            unkeyed_raw = raw_control.get("unkeyed") or []
            if isinstance(unkeyed_raw, str):
                unkeyed_raw = [unkeyed_raw]
            unkeyed_controls = [str(item) for item in unkeyed_raw if str(item)]
            special_control_keys: list[str] = []
            for prefix in id_prefixes:
                if not _CONTROL_ID_PREFIX.fullmatch(prefix):
                    errors.append(f"dynamic control id prefix is unsafe or too broad: {page}#{prefix}")
                    continue
                special_control_keys.append(f"@id-prefix:{prefix}")
            for href in hrefs:
                normalized_href = _normalize_control_href(href)
                if normalized_href != href:
                    errors.append(f"control href must be an exact normalized non-secret path: {page}#{href}")
                    continue
                special_control_keys.append(f"@href:{href}")
            for control_key in unkeyed_controls:
                discovered = discovered_controls_by_page.get(str(page), {}).get(control_key)
                if not control_key.startswith("@unkeyed:") or not discovered or discovered.get("excludable") is not True:
                    errors.append(f"unkeyed control must exactly match an ambiguous discovered control: {page}#{control_key}")
                    continue
                special_control_keys.append(control_key)
            selectors_raw = raw_control.get("selectors") or []
            if isinstance(selectors_raw, str):
                selectors_raw = [selectors_raw]
            control_selectors = [str(item) for item in selectors_raw if str(item)]
            action = str(raw_control.get("action") or "").strip()
            authorization_mode = str(raw_control.get("authorization_mode") or "correlated-action-role")
            roles = [str(item) for item in raw_control.get("roles") or ()]
            states = raw_control.get("states") or {}
            control_cases = [str(item) for item in raw_control.get("cases") or ()]
            exclusions = raw_control.get("selector_exclusions") or {}
            if not isinstance(exclusions, dict):
                errors.append(f"surface {surface.get('id')} control #{entry_index}: selector_exclusions must map control IDs to reasons")
                exclusions = {}
            if not (control_ids or special_control_keys or control_selectors) or not action:
                errors.append(
                    f"surface {surface.get('id')} control #{entry_index}: ids/id_prefixes/hrefs/unkeyed/selectors and action are required"
                )
            if authorization_mode not in {"correlated-action-role", "awaited-pre-action"}:
                errors.append(
                    f"surface {surface.get('id')} control #{entry_index}: authorization_mode must be "
                    "correlated-action-role or awaited-pre-action"
                )
            surface_roles = {str(item) for item in surface.get("roles") or ()}
            if not roles or not set(roles).issubset(surface_roles):
                errors.append(f"surface {surface.get('id')} control #{entry_index}: roles must be a non-empty subset of the surface roles")
            if not isinstance(states, dict) or not str(states.get("normal") or "").strip() or not str(states.get("abnormal") or "").strip():
                errors.append(f"surface {surface.get('id')} control #{entry_index}: states.normal and states.abnormal are required")
            if not control_cases:
                errors.append(f"surface {surface.get('id')} control #{entry_index}: cases are required")
            selector_sources = [_case_function_source(ui_root, nodeid) for nodeid in control_cases]
            source_labels = list(control_cases)
            for source_name in raw_control.get("selector_sources") or ():
                source_path = (repository / str(source_name)).resolve()
                try:
                    source_path.relative_to(ui_root.resolve())
                except ValueError:
                    errors.append(
                        f"surface {surface.get('id')} control #{entry_index}: selector source is outside ui_automation: {source_name}"
                    )
                    continue
                if not source_path.is_file():
                    errors.append(f"surface {surface.get('id')} control #{entry_index}: selector source is missing: {source_name}")
                    continue
                selector_sources.append(source_path.read_text(encoding="utf-8", errors="replace"))
                source_labels.append(str(source_name))
            for nodeid in control_cases:
                registered_cases.add(nodeid)
                surface_contract_cases.add(nodeid)
                ok, detail = _case_exists(ui_root, nodeid)
                if not ok:
                    errors.append(detail)
            for control_id in [*control_ids, *special_control_keys]:
                key = (str(page), control_id)
                if key in registered_controls:
                    errors.append(f"duplicate interactive control registration: {page}#{control_id}")
                if key in excluded_discovered_controls:
                    errors.append(f"interactive control is both registered and excluded: {page}#{control_id}")
                registered_controls.add(key)
                referenced = _control_key_referenced(control_id, selector_sources, page=str(page) if page else None)
                exclusion_reason = str(exclusions.get(control_id) or "").strip()
                if exclusion_reason and len(exclusion_reason) < 16:
                    errors.append(f"interactive control exclusion reason is too short: {page}#{control_id}")
                status = "REFERENCED" if referenced else ("EXCLUDED" if exclusion_reason else "UNREFERENCED")
                control_coverage.append(
                    {
                        "surface": str(surface.get("id") or ""),
                        "page": page,
                        "control_id": control_id,
                        "action": action,
                        "authorization_mode": authorization_mode,
                        "roles": roles,
                        "states": states,
                        "cases": control_cases,
                        "selector_sources": source_labels,
                        "selector_status": status,
                        "exclusion_reason": exclusion_reason or None,
                        "runtime_required": True,
                    }
                )
            for selector in control_selectors:
                if not re.fullmatch(r"(?:\[data-[a-z0-9_-]+\]|\.[a-z][a-z0-9_-]*)", selector):
                    errors.append(f"dynamic control selector must be a single data-* attribute or class: {page}{selector}")
                    continue
                if selector.startswith(".") and selector[1:].lower() in _NON_IDENTITY_CLASSES:
                    errors.append(f"broad styling class cannot identify a control: {page}{selector}")
                    continue
                control_key = f"@selector:{selector}"
                key = (str(page), control_key)
                if key in registered_controls:
                    errors.append(f"duplicate interactive control registration: {page}{selector}")
                if key in excluded_discovered_controls:
                    errors.append(f"interactive control is both registered and excluded: {page}{selector}")
                registered_controls.add(key)
                referenced = _dynamic_selector_referenced(selector, selector_sources)
                status = "REFERENCED" if referenced else "UNREFERENCED"
                control_coverage.append(
                    {
                        "surface": str(surface.get("id") or ""),
                        "page": page,
                        "control_id": None,
                        "selector": selector,
                        "action": action,
                        "authorization_mode": authorization_mode,
                        "roles": roles,
                        "states": states,
                        "cases": control_cases,
                        "selector_sources": source_labels,
                        "selector_status": status,
                        "exclusion_reason": None,
                        "runtime_required": True,
                    }
                )
        evidence_pages = [str(item) for item in surface.get("evidence_pages") or ()]
        if page and not evidence_pages:
            evidence_pages = [str(page)]
        if any(not item.startswith("/") for item in evidence_pages):
            errors.append(f"surface {surface.get('id')}: evidence_pages must be absolute browser paths")
        role_aliases = surface.get("role_aliases") or {}
        if not isinstance(role_aliases, dict):
            errors.append(f"surface {surface.get('id')}: role_aliases must be a mapping")
            role_aliases = {}
        surface_roles = {str(item) for item in surface.get("roles") or ()}
        unknown_role_aliases = sorted(set(map(str, role_aliases)) - surface_roles)
        if unknown_role_aliases:
            errors.append(f"surface {surface.get('id')}: role_aliases keys are absent from roles: " + ", ".join(unknown_role_aliases))
        invalid_alias_values = sorted(
            {
                str(item)
                for aliases in role_aliases.values()
                if isinstance(aliases, list)
                for item in aliases
                if str(item) not in {"admin", "user", "anonymous"}
            }
        )
        if invalid_alias_values:
            errors.append(
                f"surface {surface.get('id')}: role_aliases contain invalid authorization roles: " + ", ".join(invalid_alias_values)
            )
        surface_viewports = [str(item) for item in surface.get("viewports") or ()]
        known_viewports = set(map(str, (discovery.get("required_viewports") or {})))
        unknown_viewports = sorted(set(surface_viewports) - known_viewports)
        if unknown_viewports:
            errors.append(f"surface {surface.get('id')}: unknown viewport contracts: " + ", ".join(unknown_viewports))
        surface_contracts.append(
            {
                "id": str(surface.get("id") or ""),
                "page": str(page) if page else None,
                "evidence_pages": evidence_pages,
                "roles": [str(item) for item in surface.get("roles") or ()],
                "role_aliases": {str(role): [str(item) for item in aliases or ()] for role, aliases in role_aliases.items()},
                "viewports": surface_viewports,
                "cases": sorted(surface_contract_cases),
            }
        )
    runtime_evidence_contracts: list[dict[str, Any]] = []
    seen_runtime_evidence_ids: set[str] = set()
    raw_runtime_evidence = manifest.get("runtime_evidence_contracts") or []
    if not isinstance(raw_runtime_evidence, list):
        errors.append("runtime_evidence_contracts must be a list")
        raw_runtime_evidence = []
    for index, contract in enumerate(raw_runtime_evidence, 1):
        if not isinstance(contract, dict):
            errors.append(f"runtime evidence contract #{index} must be a mapping")
            continue
        contract_id = str(contract.get("id") or "").strip()
        feature = str(contract.get("feature") or "").strip()
        description = str(contract.get("description") or "").strip()
        cases = [str(item) for item in contract.get("cases") or ()]
        artifact = str(contract.get("artifact") or "").strip()
        required_values = contract.get("required_values")
        artifact_path = Path(artifact)
        if not contract_id or contract_id in seen_runtime_evidence_ids:
            errors.append(f"runtime evidence contract #{index} has a missing or duplicate id")
            continue
        seen_runtime_evidence_ids.add(contract_id)
        if not feature or feature not in registered_features:
            errors.append(f"runtime evidence contract {contract_id}: feature is not registered by a surface")
        if len(description) < 16:
            errors.append(f"runtime evidence contract {contract_id}: description is too short")
        if not cases:
            errors.append(f"runtime evidence contract {contract_id}: cases are required")
        if not artifact or artifact_path.is_absolute() or ".." in artifact_path.parts:
            errors.append(f"runtime evidence contract {contract_id}: artifact must be a safe case-relative path")
        if not isinstance(required_values, dict) or not required_values:
            errors.append(f"runtime evidence contract {contract_id}: required_values must be a non-empty mapping")
            required_values = {}
        for nodeid in cases:
            registered_cases.add(nodeid)
            ok, detail = _case_exists(ui_root, nodeid)
            if not ok:
                errors.append(detail)
        runtime_evidence_contracts.append(
            {
                "id": contract_id,
                "feature": feature,
                "description": description,
                "cases": cases,
                "artifact": artifact,
                "required_values": required_values,
            }
        )
    for nodeid in sorted(registered_cases):
        ok, detail = _case_exists(ui_root, nodeid)
        if not ok:
            errors.append(detail)
    discovered_cases = _discover_case_nodeids(ui_root)
    unregistered_cases = sorted(discovered_cases - registered_cases)
    if unregistered_cases:
        errors.append("unregistered UI automation cases: " + ", ".join(unregistered_cases))

    required_features = {str(item) for item in manifest.get("required_feature_categories") or ()}
    missing_features = sorted(required_features - registered_features)
    if missing_features:
        errors.append("required feature categories are unused: " + ", ".join(missing_features))

    discovered_pages, discovered_endpoints, navigation, browser_discovery_errors, unresolved_browser_allowances = (
        _discover_browser_contract(repository, discovery)
    )
    errors.extend(browser_discovery_errors)
    backend_routes, backend_route_sources, backend_route_errors = _discover_backend_routes(repository, discovery)
    errors.extend(backend_route_errors)
    missing_pages = sorted(discovered_pages - registered_pages)
    if missing_pages:
        errors.append("unregistered HTML pages: " + ", ".join(missing_pages))
    missing_navigation = sorted(page for page in navigation if page != "/docs" and page not in registered_pages)
    if missing_navigation:
        errors.append("unregistered navigation destinations: " + ", ".join(missing_navigation))
    discovered_controls = {(page, control_id) for page, controls in discovered_controls_by_page.items() for control_id in controls}
    missing_controls = sorted(discovered_controls - registered_controls - excluded_discovered_controls)
    unknown_controls = sorted(registered_controls - discovered_controls)
    if missing_controls:
        errors.append("unregistered interactive controls: " + ", ".join(f"{page}#{control_id}" for page, control_id in missing_controls))
    if unknown_controls:
        errors.append(
            "registered interactive control IDs are absent from HTML: "
            + ", ".join(f"{page}#{control_id}" for page, control_id in unknown_controls)
        )
    unreferenced_controls = [row for row in control_coverage if row["selector_status"] == "UNREFERENCED"]
    if unreferenced_controls:
        errors.append("interactive controls lack a case selector reference or reasoned exclusion: " + str(len(unreferenced_controls)))

    def endpoint_registered(discovered: tuple[str, str]) -> bool:
        method, path = discovered
        if (method, path) in registered_endpoints:
            return True
        shape = _canonical_path(path)
        return any(method == candidate_method and shape == candidate for candidate_method, candidate in registered_endpoints)

    raw_route_allowances = discovery.get("allow_unmapped_routes") or []
    if not isinstance(raw_route_allowances, list):
        errors.append("discovery.allow_unmapped_routes must be a list")
        raw_route_allowances = []
    route_allowances: list[dict[str, str]] = []
    seen_allowances: set[str] = set()
    for index, raw_allowance in enumerate(raw_route_allowances, 1):
        if not isinstance(raw_allowance, dict):
            errors.append(f"unmapped route allowance #{index} must be a mapping")
            continue
        route = str(raw_allowance.get("route") or "").strip()
        reason = str(raw_allowance.get("reason") or "").strip()
        first, separator, remainder = route.partition(" ")
        route_methods = {*(value.upper() for value in _ROUTE_METHODS), "WEBSOCKET"}
        path_part = remainder.strip() if separator and first.upper() in route_methods else route
        if not route or not path_part.startswith("/") or len(reason) < 24:
            errors.append(f"unmapped route allowance #{index} requires a route pattern and a detailed reason")
            continue
        if route in seen_allowances:
            errors.append(f"duplicate unmapped route allowance: {route}")
            continue
        seen_allowances.add(route)
        matching = sorted(f"{method} {path}" for method, path in backend_routes if _route_allowance_matches(method, path, route))
        if not matching:
            errors.append(f"unmapped route allowance is stale or matches no backend route: {route}")
            continue
        route_allowances.append({"route": route, "reason": reason, "matches": matching})

    mapped_backend_routes = {route for route in backend_routes if endpoint_registered(route)}
    allowed_backend_routes = {
        route
        for route in backend_routes - mapped_backend_routes
        if any(_route_allowance_matches(route[0], route[1], allowance["route"]) for allowance in route_allowances)
    }
    unclassified_backend_routes = sorted(
        f"{method} {path}" for method, path in backend_routes - mapped_backend_routes - allowed_backend_routes
    )
    if unclassified_backend_routes:
        errors.append("backend routes lack a UI surface or reasoned non-UI classification: " + ", ".join(unclassified_backend_routes))

    missing_endpoints = sorted(f"{method} {path}" for method, path in discovered_endpoints if not endpoint_registered((method, path)))
    if missing_endpoints:
        errors.append("unregistered browser API calls: " + ", ".join(missing_endpoints))
    return {
        "status": "FAIL" if errors else "PASS",
        "errors": errors,
        "counts": {
            "discovered_pages": len(discovered_pages),
            "registered_pages": len(registered_pages),
            "discovered_endpoints": len(discovered_endpoints),
            "registered_endpoints": len(registered_endpoints),
            "discovered_backend_routes": len(backend_routes),
            "mapped_backend_routes": len(mapped_backend_routes),
            "allowed_unmapped_backend_routes": len(allowed_backend_routes),
            "unclassified_backend_routes": len(unclassified_backend_routes),
            "registered_cases": len(registered_cases),
            "discovered_cases": len(discovered_cases),
            "required_features": len(required_features),
            "discovered_controls": len(discovered_controls),
            "registered_controls": len(registered_controls),
            "referenced_controls": sum(row["selector_status"] == "REFERENCED" for row in control_coverage),
            "excluded_controls": len(excluded_discovered_controls) + sum(row["selector_status"] == "EXCLUDED" for row in control_coverage),
            "discovery_excluded_controls": len(excluded_discovered_controls),
            "unreferenced_controls": len(unreferenced_controls),
        },
        "unregistered": {
            "pages": missing_pages,
            "navigation": missing_navigation,
            "endpoints": missing_endpoints,
            "cases": unregistered_cases,
            "controls": [f"{page}#{control_id}" for page, control_id in missing_controls],
            "unknown_controls": [f"{page}#{control_id}" for page, control_id in unknown_controls],
            "backend_routes": unclassified_backend_routes,
        },
        "control_coverage": control_coverage,
        "discovery_control_exclusions": discovery_control_exclusions,
        "discovered_control_inventory": [
            {
                "page": page,
                "control_key": control_key,
                **metadata,
            }
            for page, controls in sorted(discovered_controls_by_page.items())
            for control_key, metadata in sorted(controls.items())
        ],
        "registered_endpoint_contracts": [f"{method} {path}" for method, path in sorted(registered_endpoints)],
        "backend_route_inventory": [
            {
                "route": f"{method} {path}",
                "classification": ("UI_SURFACE" if (method, path) in mapped_backend_routes else "REASONED_NON_UI"),
                "sources": backend_route_sources.get(f"{method} {path}", []),
            }
            for method, path in sorted(mapped_backend_routes | allowed_backend_routes)
        ],
        "route_allowances": route_allowances,
        "unresolved_browser_call_allowances": unresolved_browser_allowances,
        "viewport_contracts": discovery.get("required_viewports") or {},
        "surface_contracts": surface_contracts,
        "runtime_evidence_contracts": runtime_evidence_contracts,
        "case_coverage": [{"nodeid": nodeid, "status": "DECLARED", "executions": []} for nodeid in sorted(registered_cases)],
    }


def _runtime_endpoint_matches(method: str, path: str, contracts: set[tuple[str, str]]) -> bool:
    canonical = _canonical_path(path)
    for candidate_method, candidate_path in contracts:
        if method != candidate_method:
            continue
        expression = re.escape(candidate_path)
        expression = re.sub(r"\\\{[^}]*\\\}", r"[^/]+", expression)
        if re.fullmatch(expression, canonical):
            return True
    return False


def _surface_path_matches(path: str, contract: str) -> bool:
    return _runtime_endpoint_matches(
        "GET",
        _canonical_path(path),
        {("GET", _canonical_path(contract))},
    )


def _viewport_matches(
    observed: dict[str, Any],
    name: str,
    contracts: dict[str, Any],
) -> bool:
    expected = contracts.get(name)
    if not isinstance(expected, dict):
        return False
    try:
        width = int(observed.get("width"))
        height = int(observed.get("height"))
        expected_width = int(expected.get("width"))
        expected_height = int(expected.get("height"))
    except (TypeError, ValueError):
        return False
    if min(width, height, expected_width, expected_height) <= 0:
        return False
    if expected_width <= 500:
        return width <= 500 and height >= min(600, expected_height)
    if expected_width >= 1024:
        return width >= 1024 and height >= 700
    return width == expected_width and height == expected_height


def finalize_feature_coverage(report: dict[str, Any], *, run_root: Path) -> dict[str, Any]:
    """登録caseを実際のcase resultへ突合し、未実行や証跡欠落を網羅扱いしない。"""
    observed: dict[str, list[dict[str, str]]] = {}
    for path in sorted(run_root.glob("*/*/*/result.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or not payload.get("nodeid"):
            continue
        nodeid = str(payload["nodeid"])
        observed.setdefault(nodeid, []).append(
            {
                "profile": path.relative_to(run_root).parts[0],
                "status": "PASS" if payload.get("outcome") == "passed" else "FAIL",
                "evidence": str(path.relative_to(run_root)),
            }
        )
    counts = {"pass": 0, "fail": 0, "not_run": 0}
    for row in report.get("case_coverage") or ():
        executions = observed.get(str(row.get("nodeid")), [])
        row["executions"] = executions
        if executions and all(item["status"] == "PASS" for item in executions):
            row["status"] = "PASS"
            counts["pass"] += 1
        elif executions:
            row["status"] = "FAIL"
            counts["fail"] += 1
        else:
            row["status"] = "NOT_RUN"
            counts["not_run"] += 1
    report["execution_counts"] = counts
    if counts["fail"] or counts["not_run"]:
        report["status"] = "FAIL"
        message = "registered UI automation case execution coverage is incomplete"
        if message not in report.setdefault("errors", []):
            report["errors"].append(message)

    runtime_evidence_rows: list[dict[str, Any]] = []
    for contract in report.get("runtime_evidence_contracts") or ():
        if not isinstance(contract, dict):
            continue
        declared_cases = {str(item) for item in contract.get("cases") or ()}
        relative_artifact = str(contract.get("artifact") or "")
        required_values = contract.get("required_values") or {}
        observations: list[dict[str, Any]] = []
        invalid = False
        for result_path in sorted(run_root.glob("*/*/*/result.json")):
            try:
                result_payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(result_payload, dict) or str(result_payload.get("nodeid") or "") not in declared_cases:
                continue
            profile = result_path.relative_to(run_root).parts[0]
            case_passed = result_payload.get("outcome") == "passed"
            evidence_path = result_path.parent / relative_artifact
            observation: dict[str, Any] = {
                "case": str(result_payload.get("nodeid") or ""),
                "profile": profile,
                "case_status": "PASS" if case_passed else "FAIL",
                "evidence": str(evidence_path.relative_to(run_root)),
                "required_values": required_values,
            }
            if not case_passed:
                observation["status"] = "CASE_FAILED"
                observations.append(observation)
                continue
            try:
                evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                observation["status"] = "INVALID"
                observation["reason"] = f"runtime evidence is missing or malformed: {type(exc).__name__}"
                invalid = True
                observations.append(observation)
                continue
            if not isinstance(evidence_payload, dict):
                observation["status"] = "INVALID"
                observation["reason"] = "runtime evidence root is not an object"
                invalid = True
                observations.append(observation)
                continue
            observed_values = {str(key): evidence_payload.get(key) for key in required_values}
            matched = all(
                key in evidence_payload and type(evidence_payload[key]) is type(expected) and evidence_payload[key] == expected
                for key, expected in required_values.items()
            )
            observation["observed_values"] = observed_values
            observation["status"] = "MATCH" if matched else "NOT_PROVEN"
            observations.append(observation)
        matched_observations = [row for row in observations if row.get("status") == "MATCH"]
        contract_status = "FAIL" if invalid else "PASS" if matched_observations else "NOT_PROVEN"
        runtime_evidence_rows.append(
            {
                **contract,
                "status": contract_status,
                "matching_evidence": [row["evidence"] for row in matched_observations],
                "observations": observations,
            }
        )
    unproven_runtime_evidence = [row for row in runtime_evidence_rows if row.get("status") != "PASS"]
    report["runtime_evidence_coverage"] = {
        "status": "FAIL" if unproven_runtime_evidence else "PASS",
        "contract_count": len(runtime_evidence_rows),
        "passing_contracts": sum(row.get("status") == "PASS" for row in runtime_evidence_rows),
        "unproven_contracts": [str(row.get("id") or "") for row in unproven_runtime_evidence],
        "contracts": runtime_evidence_rows,
    }
    if unproven_runtime_evidence:
        report["status"] = "FAIL"
        message = "runtime feature evidence is incomplete or not proven by a passing real-service execution"
        if message not in report.setdefault("errors", []):
            report["errors"].append(message)

    contracts: set[tuple[str, str]] = set()
    for raw in report.get("registered_endpoint_contracts") or ():
        method, separator, path = str(raw).partition(" ")
        if separator:
            contracts.add((method.upper(), _canonical_path(path)))
    api_resource_types = {"fetch", "xhr", "eventsource"}
    attempted_endpoints: set[tuple[str, str]] = set()
    successful_endpoints: set[tuple[str, str]] = set()
    endpoint_evidence: list[dict[str, Any]] = []
    endpoint_failures: list[dict[str, Any]] = []
    incomplete_transactions: list[dict[str, Any]] = []
    correlation_errors: list[dict[str, Any]] = []
    for path in sorted(run_root.glob("*/*/*/network/http.jsonl")):
        evidence_path = str(path.relative_to(run_root))
        try:
            rows = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        browser_transactions: dict[str, dict[str, Any]] = {}
        for line_number, raw in enumerate(rows, 1):
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                correlation_errors.append(
                    {
                        "evidence": evidence_path,
                        "line": line_number,
                        "reason": "invalid JSONL record",
                    }
                )
                continue
            if not isinstance(row, dict):
                correlation_errors.append(
                    {
                        "evidence": evidence_path,
                        "line": line_number,
                        "reason": "HTTP evidence record is not an object",
                    }
                )
                continue
            phase = str(row.get("phase") or "")
            if phase == "independent-client":
                try:
                    runtime_path = urlsplit(str(row.get("url") or "")).path
                except ValueError:
                    runtime_path = ""
                method = str(row.get("method") or "GET").upper()
                status = row.get("status")
                if not runtime_path.startswith("/") or not isinstance(status, int) or isinstance(status, bool):
                    correlation_errors.append(
                        {
                            "evidence": evidence_path,
                            "line": line_number,
                            "reason": "independent-client record lacks an absolute path or integer status",
                        }
                    )
                    continue
                endpoint = (method, _canonical_path(runtime_path))
                attempted_endpoints.add(endpoint)
                observation = {
                    "method": method,
                    "path": endpoint[1],
                    "status": status,
                    "correlation": "atomic-independent-client-record",
                    "evidence": evidence_path,
                    "line": line_number,
                }
                if 200 <= status < 400:
                    successful_endpoints.add(endpoint)
                    endpoint_evidence.append(observation)
                else:
                    endpoint_failures.append({**observation, "reason": "non-success HTTP status"})
                continue

            if phase not in {"request", "response", "requestfailed"}:
                continue
            request_id = str(row.get("request_id") or "")
            resource_type = str(row.get("resource_type") or "")
            if phase == "request" and resource_type not in api_resource_types:
                continue
            if phase != "request" and resource_type not in api_resource_types and request_id not in browser_transactions:
                continue
            if not request_id:
                correlation_errors.append(
                    {
                        "evidence": evidence_path,
                        "line": line_number,
                        "phase": phase,
                        "reason": "browser HTTP record has no request_id",
                    }
                )
                continue
            transaction = browser_transactions.setdefault(
                request_id,
                {"request": None, "responses": [], "failures": []},
            )
            record = {**row, "line": line_number}
            if phase == "request":
                if transaction["request"] is not None:
                    correlation_errors.append(
                        {
                            "evidence": evidence_path,
                            "line": line_number,
                            "request_id": request_id,
                            "reason": "duplicate browser request record",
                        }
                    )
                else:
                    transaction["request"] = record
            elif phase == "response":
                transaction["responses"].append(record)
            else:
                transaction["failures"].append(record)

        for request_id, transaction in browser_transactions.items():
            request = transaction["request"]
            responses = transaction["responses"]
            failures = transaction["failures"]
            if not isinstance(request, dict):
                correlation_errors.append(
                    {
                        "evidence": evidence_path,
                        "request_id": request_id,
                        "reason": "browser response/requestfailed has no matching request record",
                    }
                )
                continue
            try:
                runtime_path = urlsplit(str(request.get("url") or "")).path
            except ValueError:
                runtime_path = ""
            method = str(request.get("method") or "GET").upper()
            if not runtime_path.startswith("/"):
                correlation_errors.append(
                    {
                        "evidence": evidence_path,
                        "line": request.get("line"),
                        "request_id": request_id,
                        "reason": "browser request record lacks an absolute path",
                    }
                )
                continue
            endpoint = (method, _canonical_path(runtime_path))
            attempted_endpoints.add(endpoint)
            base = {
                "request_id": request_id,
                "method": method,
                "path": endpoint[1],
                "evidence": evidence_path,
                "request_line": request.get("line"),
            }
            terminal_records = [*responses, *failures]
            mismatched = []
            for terminal in terminal_records:
                try:
                    terminal_path = urlsplit(str(terminal.get("url") or "")).path
                except ValueError:
                    terminal_path = ""
                terminal_method = str(terminal.get("method") or "GET").upper()
                if terminal_method != method or _canonical_path(terminal_path) != endpoint[1]:
                    mismatched.append(
                        {
                            "phase": terminal.get("phase"),
                            "line": terminal.get("line"),
                            "method": terminal_method,
                            "path": _canonical_path(terminal_path) if terminal_path else "",
                        }
                    )
            if mismatched:
                correlation_errors.append(
                    {
                        **base,
                        "reason": "request_id correlated records disagree on method or exact path",
                        "mismatched_records": mismatched,
                    }
                )
                continue
            if failures:
                endpoint_failures.append(
                    {
                        **base,
                        "reason": "browser requestfailed terminal record",
                        "failures": [
                            {
                                "line": item.get("line"),
                                "failure": str(item.get("failure") or "request failed"),
                                "expected": item.get("expected") is True,
                            }
                            for item in failures
                        ],
                    }
                )
                continue
            if len(responses) != 1:
                target = incomplete_transactions if not responses else correlation_errors
                target.append(
                    {
                        **base,
                        "reason": "browser request has no correlated response"
                        if not responses
                        else "browser request has multiple correlated responses",
                        "response_count": len(responses),
                    }
                )
                continue
            response = responses[0]
            status = response.get("status")
            if not isinstance(status, int) or isinstance(status, bool):
                correlation_errors.append(
                    {
                        **base,
                        "line": response.get("line"),
                        "reason": "correlated browser response lacks an integer status",
                    }
                )
                continue
            observation = {
                **base,
                "response_line": response.get("line"),
                "status": status,
                "correlation": "browser-request-id",
            }
            if 200 <= status < 400:
                successful_endpoints.add(endpoint)
                endpoint_evidence.append(observation)
            else:
                endpoint_failures.append({**observation, "reason": "non-success HTTP status"})
    unregistered_runtime = sorted(
        f"{method} {path}" for method, path in attempted_endpoints if not _runtime_endpoint_matches(method, path, contracts)
    )
    unobserved_contracts = sorted(
        f"{method} {path}"
        for method, path in contracts
        if not any(
            _runtime_endpoint_matches(observed_method, observed_path, {(method, path)})
            for observed_method, observed_path in successful_endpoints
        )
    )
    report["runtime_endpoint_coverage"] = {
        "status": "FAIL" if unregistered_runtime or unobserved_contracts or incomplete_transactions or correlation_errors else "PASS",
        "success_status_contract": "200-399",
        "attempted_count": len(attempted_endpoints),
        "attempted": sorted(f"{method} {path}" for method, path in attempted_endpoints),
        "observed_count": len(successful_endpoints),
        "observed_successful": sorted(f"{method} {path}" for method, path in successful_endpoints),
        "observed": sorted(f"{method} {path}" for method, path in successful_endpoints),
        "unregistered": unregistered_runtime,
        "unobserved_registered": unobserved_contracts,
        "successful_evidence": endpoint_evidence,
        "evidence": endpoint_evidence,
        "failed_transactions": endpoint_failures,
        "incomplete_transactions": incomplete_transactions,
        "correlation_errors": correlation_errors,
    }
    if unregistered_runtime:
        report["status"] = "FAIL"
        message = "runtime browser/API requests are absent from coverage.yaml: " + ", ".join(unregistered_runtime)
        if message not in report.setdefault("errors", []):
            report["errors"].append(message)
    if unobserved_contracts:
        report["status"] = "FAIL"
        message = "registered browser/API contracts lack a correlated successful response: " + ", ".join(unobserved_contracts)
        if message not in report.setdefault("errors", []):
            report["errors"].append(message)
    if incomplete_transactions or correlation_errors:
        report["status"] = "FAIL"
        message = "runtime browser/API request-response correlation evidence is incomplete or malformed"
        if message not in report.setdefault("errors", []):
            report["errors"].append(message)

    screenshot_observations: list[dict[str, Any]] = []
    screenshot_evidence_errors: list[dict[str, Any]] = []
    for result_path in sorted(run_root.glob("*/*/*/result.json")):
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(result, dict) or result.get("outcome") != "passed":
            continue
        nodeid = str(result.get("nodeid") or "")
        attestation_path = result_path.parent / "security" / "screenshot-attestations.jsonl"
        http_path = result_path.parent / "network" / "http.jsonl"
        if not nodeid or not attestation_path.is_file():
            continue
        screenshot_http: dict[str, list[dict[str, Any]]] = {}
        try:
            for http_line_number, raw_http in enumerate(http_path.read_text(encoding="utf-8").splitlines(), 1):
                http_row = json.loads(raw_http)
                if not isinstance(http_row, dict) or http_row.get("evidence_probe") != "screenshot-role-v1":
                    continue
                correlation_id = str(http_row.get("evidence_correlation_id") or "")
                if correlation_id:
                    screenshot_http.setdefault(correlation_id, []).append({**http_row, "line": http_line_number})
        except (OSError, json.JSONDecodeError):
            screenshot_http = {}
        try:
            rows = attestation_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in rows:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            authorization = row.get("authorization_observation")
            pre_capture = row.get("pre_capture_context")
            post_capture = row.get("post_capture_context")
            viewport = row.get("viewport")
            page_path = str(row.get("page_path") or "")
            page_id = str(row.get("page_id") or "")
            control_action_ids = row.get("control_action_ids")
            captured_at = row.get("captured_at_epoch_seconds")
            if (
                not page_path.startswith("/")
                or not isinstance(authorization, dict)
                or not isinstance(pre_capture, dict)
                or not isinstance(post_capture, dict)
                or not isinstance(viewport, dict)
                or not isinstance(control_action_ids, list)
                or len(control_action_ids) != len(set(control_action_ids))
                or not all(isinstance(item, str) and re.fullmatch(r"control-action-\d{6,}", item) for item in control_action_ids)
                or not isinstance(captured_at, (int, float))
                or isinstance(captured_at, bool)
            ):
                screenshot_evidence_errors.append(
                    {
                        "case": nodeid,
                        "profile": result_path.relative_to(run_root).parts[0],
                        "evidence": str(attestation_path.relative_to(run_root)),
                        "reason": "screenshot attestation lacks structured capture context",
                    }
                )
                continue
            contexts = [pre_capture, post_capture]
            correlated_contexts: list[dict[str, Any]] = []
            correlations: set[str] = set()
            for context in contexts:
                context_authorization = context.get("authorization") if isinstance(context, dict) else None
                correlation_id = (
                    str(context_authorization.get("evidence_correlation_id") or "") if isinstance(context_authorization, dict) else ""
                )
                rows_for_correlation = screenshot_http.get(correlation_id, [])
                requests = [item for item in rows_for_correlation if item.get("phase") == "request"]
                responses = [item for item in rows_for_correlation if item.get("phase") == "response"]
                failures = [item for item in rows_for_correlation if item.get("phase") == "requestfailed"]
                request_id = str(requests[0].get("request_id") or "") if len(requests) == 1 else ""
                observed_at = context_authorization.get("observed_at_epoch_seconds") if isinstance(context_authorization, dict) else None
                valid_context = (
                    re.fullmatch(r"screenshot-role-\d{10,14}-\d+", correlation_id) is not None
                    and len(requests) == 1
                    and len(responses) == 1
                    and not failures
                    and bool(request_id)
                    and responses[0].get("request_id") == request_id
                    and requests[0].get("page_id") == page_id == responses[0].get("page_id") == context.get("page_id")
                    and context.get("page_path") == page_path
                    and context.get("viewport") == viewport
                    and isinstance(context_authorization, dict)
                    and context_authorization.get("role") in {"admin", "user", "anonymous"}
                    and isinstance(context_authorization.get("status"), int)
                    and not isinstance(context_authorization.get("status"), bool)
                    and isinstance(context_authorization.get("auth_disabled"), bool)
                    and responses[0].get("status") == context_authorization.get("status")
                    and isinstance(observed_at, (int, float))
                    and not isinstance(observed_at, bool)
                    and isinstance(responses[0].get("ts"), (int, float))
                    and not isinstance(responses[0].get("ts"), bool)
                    and abs(float(responses[0]["ts"]) - float(observed_at)) <= 5
                )
                if valid_context:
                    correlations.add(correlation_id)
                    correlated_contexts.append(
                        {
                            "correlation_id": correlation_id,
                            "request_id": request_id,
                            "request_line": requests[0].get("line"),
                            "response_line": responses[0].get("line"),
                        }
                    )
            authorization_status = authorization.get("status")
            valid_role_status = (
                isinstance(authorization_status, int)
                and not isinstance(authorization_status, bool)
                and (
                    (authorization.get("role") in {"admin", "user"} and 200 <= authorization_status < 300)
                    or (authorization.get("role") == "anonymous" and authorization_status in {401, 403})
                )
            )
            pre_authorization = pre_capture.get("authorization") if isinstance(pre_capture, dict) else None
            post_authorization = post_capture.get("authorization") if isinstance(post_capture, dict) else None
            stable_authorization = (
                isinstance(pre_authorization, dict)
                and isinstance(post_authorization, dict)
                and all(pre_authorization.get(key) == post_authorization.get(key) for key in ("status", "role", "auth_disabled"))
            )
            if (
                len(correlated_contexts) != 2
                or len(correlations) != 2
                or post_capture.get("authorization") != authorization
                or not stable_authorization
                or not valid_role_status
            ):
                screenshot_evidence_errors.append(
                    {
                        "case": nodeid,
                        "profile": result_path.relative_to(run_root).parts[0],
                        "evidence": str(attestation_path.relative_to(run_root)),
                        "screenshot": str(row.get("path") or row.get("screenshot") or ""),
                        "reason": "screenshot role evidence is not uniquely correlated to pre/post /auth/me HTTP transactions",
                    }
                )
                continue
            screenshot_observations.append(
                {
                    "case": nodeid,
                    "profile": result_path.relative_to(run_root).parts[0],
                    "page_path": page_path,
                    "role": str(authorization.get("role") or "unknown"),
                    "auth_disabled": authorization.get("auth_disabled") is True,
                    "viewport": viewport,
                    "viewport_class": str(row.get("viewport_class") or ""),
                    "captured_at_epoch_seconds": float(captured_at),
                    "evidence": str(attestation_path.relative_to(run_root)),
                    "screenshot": str(row.get("path") or row.get("screenshot") or ""),
                    "page_id": page_id,
                    "control_action_ids": control_action_ids,
                    "authorization_http": correlated_contexts,
                }
            )

    report["runtime_screenshot_evidence"] = {
        "status": "FAIL" if screenshot_evidence_errors else "PASS",
        "observation_count": len(screenshot_observations),
        "errors": screenshot_evidence_errors,
    }
    if screenshot_evidence_errors:
        report["status"] = "FAIL"
        message = "screenshot role evidence is not uniquely correlated to browser HTTP evidence"
        if message not in report.setdefault("errors", []):
            report["errors"].append(message)

    viewport_contracts = report.get("viewport_contracts") or {}
    trusted_control_actions: list[dict[str, Any]] = []
    control_evidence_errors: list[dict[str, Any]] = []
    for result_path in sorted(run_root.glob("*/*/*/result.json")):
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(result, dict) or result.get("outcome") != "passed":
            continue
        nodeid = str(result.get("nodeid") or "")
        action_path = result_path.parent / "browser" / "control-actions.jsonl"
        http_path = result_path.parent / "network" / "http.jsonl"
        authorization_http: dict[str, list[dict[str, Any]]] = {}
        try:
            for http_line_number, raw_http in enumerate(http_path.read_text(encoding="utf-8").splitlines(), 1):
                http_row = json.loads(raw_http)
                if not isinstance(http_row, dict) or http_row.get("evidence_probe") != "control-action-role-v1":
                    continue
                correlation_id = str(http_row.get("evidence_correlation_id") or "")
                if not correlation_id:
                    continue
                authorization_http.setdefault(correlation_id, []).append({**http_row, "line": http_line_number})
        except (OSError, json.JSONDecodeError):
            authorization_http = {}
        if not nodeid or not action_path.is_file():
            control_evidence_errors.append(
                {
                    "case": nodeid,
                    "profile": result_path.relative_to(run_root).parts[0],
                    "evidence": str(action_path.relative_to(run_root)),
                    "reason": "passing case has no trusted control-action evidence file",
                }
            )
            continue
        try:
            action_lines = action_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            control_evidence_errors.append(
                {
                    "case": nodeid,
                    "profile": result_path.relative_to(run_root).parts[0],
                    "evidence": str(action_path.relative_to(run_root)),
                    "reason": "trusted control-action evidence is unreadable",
                }
            )
            continue
        seen_action_ids: set[str] = set()
        for line_number, raw in enumerate(action_lines, 1):
            try:
                action = json.loads(raw)
            except json.JSONDecodeError:
                control_evidence_errors.append(
                    {
                        "case": nodeid,
                        "profile": result_path.relative_to(run_root).parts[0],
                        "evidence": str(action_path.relative_to(run_root)),
                        "line": line_number,
                        "reason": "invalid control-action JSONL record",
                    }
                )
                continue
            if not isinstance(action, dict):
                control_evidence_errors.append(
                    {
                        "case": nodeid,
                        "profile": result_path.relative_to(run_root).parts[0],
                        "evidence": str(action_path.relative_to(run_root)),
                        "line": line_number,
                        "reason": "control-action record is not an object",
                    }
                )
                continue
            action_id = str(action.get("action_id") or "")
            action_nodeid = str(action.get("nodeid") or "")
            page_path = str(action.get("page_path") or "")
            keys = action.get("control_keys")
            viewport = action.get("viewport")
            authorization = action.get("authorization_observation")
            authorization_observed_at = action.get("authorization_observed_at_epoch_seconds")
            authorization_correlation_id = str(action.get("authorization_evidence_correlation_id") or "")
            authorization_mode = str(action.get("authorization_mode") or "")
            action_page_id = str(action.get("page_id") or "")
            recorded_at = action.get("recorded_at_epoch_seconds")
            occurred_at = action.get("occurred_at_epoch_seconds")
            state_attestations = action.get("state_attestations")
            state_contract = action.get("state_contract")
            auth_http_rows = authorization_http.get(authorization_correlation_id, [])
            auth_requests = [row for row in auth_http_rows if row.get("phase") == "request"]
            auth_responses = [row for row in auth_http_rows if row.get("phase") == "response"]
            auth_failures = [row for row in auth_http_rows if row.get("phase") == "requestfailed"]
            auth_request_id = str(auth_requests[0].get("request_id") or "") if len(auth_requests) == 1 else ""
            valid_authorization_order = (
                len(auth_responses) == 1
                and isinstance(auth_responses[0].get("ts"), (int, float))
                and not isinstance(auth_responses[0].get("ts"), bool)
                and isinstance(authorization_observed_at, (int, float))
                and not isinstance(authorization_observed_at, bool)
                and isinstance(occurred_at, (int, float))
                and not isinstance(occurred_at, bool)
                and (
                    (
                        authorization_mode == "awaited-pre-action"
                        and 0 <= float(occurred_at) - float(authorization_observed_at) <= 5
                        and abs(float(auth_responses[0]["ts"]) - float(authorization_observed_at)) <= 5
                    )
                    or (
                        authorization_mode == "concurrent-action-probe"
                        and float(occurred_at) <= float(auth_responses[0]["ts"]) <= float(authorization_observed_at) + 5
                    )
                )
            )
            valid_auth_http_correlation = (
                len(auth_requests) == 1
                and len(auth_responses) == 1
                and not auth_failures
                and bool(auth_request_id)
                and str(auth_responses[0].get("request_id") or "") == auth_request_id
                and auth_requests[0].get("page_id") == action_page_id
                and auth_responses[0].get("page_id") == action_page_id
                and isinstance(authorization, dict)
                and auth_responses[0].get("status") == authorization.get("status")
                and valid_authorization_order
            )
            attested_states = {str(item.get("state")) for item in state_attestations or () if isinstance(item, dict) and item.get("state")}
            valid_authorization = (
                isinstance(authorization, dict)
                and authorization.get("role") in {"admin", "user", "anonymous"}
                and isinstance(authorization.get("status"), int)
                and not isinstance(authorization.get("status"), bool)
                and isinstance(authorization.get("auth_disabled"), bool)
                and (
                    (authorization.get("role") in {"admin", "user"} and 200 <= authorization["status"] < 300)
                    or (authorization.get("role") == "anonymous" and authorization["status"] in {401, 403})
                )
                and (authorization.get("auth_disabled") is False or authorization.get("role") == "admin")
            )
            valid = (
                action.get("schema_version") == 1
                and action.get("is_trusted") is True
                and action.get("capture_source") == "playwright-exposed-binding-with-page-nonce"
                and action_nodeid == nodeid
                and bool(action_id)
                and action_id not in seen_action_ids
                and page_path.startswith("/")
                and "?" not in page_path
                and "#" not in page_path
                and isinstance(keys, list)
                and bool(keys)
                and all(isinstance(key, str) and key for key in keys)
                and isinstance(viewport, dict)
                and _viewport_matches(viewport, str(action.get("viewport_class") or ""), viewport_contracts)
                and isinstance(recorded_at, (int, float))
                and not isinstance(recorded_at, bool)
                and isinstance(occurred_at, (int, float))
                and not isinstance(occurred_at, bool)
                and abs(float(recorded_at) - float(occurred_at)) <= 60
                and re.fullmatch(r"control-action-role-\d{10,14}-\d+", authorization_correlation_id) is not None
                and action_page_id.startswith("browser-page-")
                and isinstance(authorization_observed_at, (int, float))
                and not isinstance(authorization_observed_at, bool)
                and authorization_mode in {"awaited-pre-action", "concurrent-action-probe"}
                and (
                    0 <= float(occurred_at) - float(authorization_observed_at) <= 5
                    if authorization_mode == "awaited-pre-action"
                    else float(occurred_at) <= float(authorization_observed_at) <= float(recorded_at) + 1
                )
                and valid_auth_http_correlation
                and valid_authorization
                and action.get("post_action_screenshot_required") is True
                and isinstance(state_attestations, list)
                and len(state_attestations) <= 1
                and isinstance(state_contract, dict)
                and state_contract.get("inference_allowed") is False
                and set(state_contract.get("required") or ()) == {"normal", "abnormal"}
                and set(state_contract.get("observed") or ()) == attested_states
            )
            if not valid:
                control_evidence_errors.append(
                    {
                        "case": nodeid,
                        "profile": result_path.relative_to(run_root).parts[0],
                        "evidence": str(action_path.relative_to(run_root)),
                        "line": line_number,
                        "action_id": action_id,
                        "reason": "trusted control-action record violates the evidence schema",
                    }
                )
                continue
            seen_action_ids.add(action_id)
            trusted_control_actions.append(
                {
                    **action,
                    "case": nodeid,
                    "profile": result_path.relative_to(run_root).parts[0],
                    "evidence": str(action_path.relative_to(run_root)),
                    "line": line_number,
                    "role": str(authorization["role"]),
                    "recorded_at_epoch_seconds": float(recorded_at),
                    "occurred_at_epoch_seconds": float(occurred_at),
                    "authorization_observed_at_epoch_seconds": float(authorization_observed_at),
                    "authorization_request_id": auth_request_id,
                }
            )

    for screenshot in screenshot_observations:
        for action_id in screenshot.get("control_action_ids", ()):
            matches = [
                action
                for action in trusted_control_actions
                if action.get("action_id") == action_id
                and action.get("case") == screenshot.get("case")
                and action.get("profile") == screenshot.get("profile")
            ]
            if len(matches) != 1:
                control_evidence_errors.append(
                    {
                        "case": screenshot.get("case"),
                        "profile": screenshot.get("profile"),
                        "evidence": screenshot.get("evidence"),
                        "screenshot": screenshot.get("screenshot"),
                        "action_id": action_id,
                        "reason": "screenshot action binding has no unique trusted action in the same case and profile",
                    }
                )

    control_rows: list[dict[str, Any]] = []
    control_failures: list[str] = []
    for contract in report.get("control_coverage") or ():
        if not isinstance(contract, dict):
            continue
        control_key = str(contract.get("control_id") or "")
        if not control_key:
            selector = str(contract.get("selector") or "")
            control_key = f"@selector:{selector}" if selector else ""
        page = _canonical_path(str(contract.get("page") or ""))
        declared_cases = {str(item) for item in contract.get("cases") or ()}
        declared_roles = [str(item) for item in contract.get("roles") or ()]
        required_authorization_mode = str(contract.get("authorization_mode") or "correlated-action-role")
        key_actions = [item for item in trusted_control_actions if control_key in item.get("control_keys", ())]
        exact_actions = [
            item for item in key_actions if _canonical_path(str(item.get("page_path") or "")) == page and item.get("case") in declared_cases
        ]
        action_observations: list[dict[str, Any]] = []
        qualifying_actions: list[dict[str, Any]] = []
        for action in exact_actions:
            post_screenshots = [
                screenshot
                for screenshot in screenshot_observations
                if screenshot["case"] == action["case"]
                and screenshot["profile"] == action["profile"]
                and action["action_id"] in screenshot.get("control_action_ids", ())
                and screenshot["captured_at_epoch_seconds"] >= action["occurred_at_epoch_seconds"]
                and _viewport_matches(screenshot["viewport"], "desktop", viewport_contracts)
            ]
            gaps = []
            if action["role"] not in declared_roles:
                gaps.append(f"observed role {action['role']} is not declared")
            if required_authorization_mode == "awaited-pre-action" and action.get("authorization_mode") != "awaited-pre-action":
                gaps.append("authentication-changing control lacks an awaited pre-action role probe")
            if not _viewport_matches(action["viewport"], "desktop", viewport_contracts):
                gaps.append("trusted action was not performed at desktop viewport")
            if not post_screenshots:
                gaps.append("no desktop screenshot is explicitly bound to the trusted action ID")
            observation = {
                "action_id": action["action_id"],
                "case": action["case"],
                "profile": action["profile"],
                "page_path": action["page_path"],
                "control_key": control_key,
                "role": action["role"],
                "viewport": action["viewport"],
                "event_type": action.get("event_type"),
                "evidence": action["evidence"],
                "line": action["line"],
                "post_action_screenshots": [
                    {
                        "evidence": item["evidence"],
                        "screenshot": item["screenshot"],
                        "page_path": item["page_path"],
                        "control_action_ids": item["control_action_ids"],
                        "captured_at_epoch_seconds": item["captured_at_epoch_seconds"],
                    }
                    for item in post_screenshots
                ],
                "status": "PASS" if not gaps else "FAIL",
                "gaps": gaps,
            }
            action_observations.append(observation)
            if not gaps:
                qualifying_actions.append(action)

        role_rows = []
        for role in declared_roles:
            matches = [item for item in qualifying_actions if item["role"] == role]
            role_rows.append(
                {
                    "role": role,
                    "status": "PASS" if matches else "NOT_PROVEN",
                    "action_ids": sorted({str(item["action_id"]) for item in matches}),
                    "evidence": sorted({str(item["evidence"]) for item in matches}),
                }
            )

        state_rows = []
        for state in ("normal", "abnormal"):
            state_evidence = []
            invalid_state_evidence = []
            for action in qualifying_actions:
                for attestation in action.get("state_attestations") or ():
                    valid_attestation = (
                        isinstance(attestation, dict)
                        and attestation.get("state") == state
                        and attestation.get("outcome") == "passed"
                        and attestation.get("source") == "explicit-test-assertion"
                        and attestation.get("control_key") == control_key
                        and attestation.get("action_id") == action["action_id"]
                        and _canonical_path(str(attestation.get("page_path") or "")) == page
                        and isinstance(attestation.get("assertion"), str)
                        and len(attestation["assertion"].strip()) >= 12
                        and isinstance(attestation.get("attested_at_epoch_seconds"), (int, float))
                        and not isinstance(attestation.get("attested_at_epoch_seconds"), bool)
                        and attestation["attested_at_epoch_seconds"] >= action["occurred_at_epoch_seconds"]
                    )
                    evidence_row = {
                        "action_id": action["action_id"],
                        "case": action["case"],
                        "profile": action["profile"],
                        "evidence": action["evidence"],
                        "line": action["line"],
                    }
                    if valid_attestation:
                        state_evidence.append(evidence_row)
                    elif isinstance(attestation, dict) and attestation.get("state") == state:
                        invalid_state_evidence.append(evidence_row)
            state_rows.append(
                {
                    "state": state,
                    "expected_behavior": str((contract.get("states") or {}).get(state) or ""),
                    "inference_allowed": False,
                    "status": "PASS" if state_evidence else "NOT_PROVEN",
                    "evidence": state_evidence,
                    "invalid_evidence": invalid_state_evidence,
                }
            )

        gaps = []
        if not exact_actions:
            if key_actions:
                gaps.append("trusted actions exist, but none match the exact declared page and case")
            else:
                gaps.append("no trusted action matches the exact declared control key")
        gaps.extend(f"declared role {row['role']} has no qualifying desktop action" for row in role_rows if row["status"] != "PASS")
        gaps.extend(f"state {row['state']} has no explicit passing assertion" for row in state_rows if row["status"] != "PASS")
        if exact_actions and not qualifying_actions:
            gaps.append("no exact trusted action has a later action-ID-bound desktop screenshot")
        status = "FAIL" if gaps else "PASS"
        if gaps:
            control_failures.append(f"{page}#{control_key}: " + "; ".join(gaps))
        control_rows.append(
            {
                "surface": str(contract.get("surface") or ""),
                "page": page,
                "control_key": control_key,
                "declared_action": str(contract.get("action") or ""),
                "declared_cases": sorted(declared_cases),
                "declared_roles": declared_roles,
                "authorization_mode": required_authorization_mode,
                "requirements": {
                    "passing_case": True,
                    "exact_page": page,
                    "exact_control_key": control_key,
                    "declared_roles": declared_roles,
                    "authorization_mode": required_authorization_mode,
                    "viewport": "desktop",
                    "post_action_screenshot": "explicit action-ID binding",
                    "explicit_states": ["normal", "abnormal"],
                    "state_inference_allowed": False,
                },
                "status": status,
                "roles": role_rows,
                "states": state_rows,
                "observations": action_observations,
                "gaps": gaps,
            }
        )
    report["runtime_control_coverage"] = {
        "status": "FAIL" if control_failures or control_evidence_errors else "PASS",
        "control_count": len(control_rows),
        "passing_controls": sum(row["status"] == "PASS" for row in control_rows),
        "failing_controls": sum(row["status"] != "PASS" for row in control_rows),
        "trusted_action_count": len(trusted_control_actions),
        "evidence_errors": control_evidence_errors,
        "failures": control_failures,
        "controls": control_rows,
    }
    if control_failures or control_evidence_errors:
        report["status"] = "FAIL"
        message = "UI control execution/state coverage is incomplete"
        if message not in report.setdefault("errors", []):
            report["errors"].append(message)

    surface_rows: list[dict[str, Any]] = []
    surface_failures: list[str] = []
    for contract in report.get("surface_contracts") or ():
        if not isinstance(contract, dict):
            continue
        surface_id = str(contract.get("id") or "")
        page = contract.get("page")
        if not page:
            surface_rows.append(
                {
                    "surface": surface_id,
                    "status": "NOT_APPLICABLE",
                    "reason": "surface has no browser page",
                    "observations": [],
                }
            )
            continue
        evidence_pages = [str(item) for item in contract.get("evidence_pages") or (page,)]
        declared_cases = {str(item) for item in contract.get("cases") or ()}
        observations = [
            item
            for item in screenshot_observations
            if item["case"] in declared_cases
            and any(_surface_path_matches(str(item["page_path"]), candidate) for candidate in evidence_pages)
        ]
        role_aliases = contract.get("role_aliases") or {}
        role_rows = []
        for expected_role in [str(item) for item in contract.get("roles") or ()]:
            accepted = {expected_role}
            aliases = role_aliases.get(expected_role) if isinstance(role_aliases, dict) else None
            if isinstance(aliases, list):
                accepted.update(str(item) for item in aliases)
            matches = [item for item in observations if item["role"] in accepted]
            role_rows.append(
                {
                    "role": expected_role,
                    "accepted_authorization_roles": sorted(accepted),
                    "status": "PASS" if matches else "NOT_OBSERVED",
                    "evidence": [item["evidence"] for item in matches],
                }
            )
        viewport_rows = []
        for expected_viewport in [str(item) for item in contract.get("viewports") or ()]:
            matches = [item for item in observations if _viewport_matches(item["viewport"], expected_viewport, viewport_contracts)]
            viewport_rows.append(
                {
                    "viewport": expected_viewport,
                    "contract": viewport_contracts.get(expected_viewport),
                    "status": "PASS" if matches else "NOT_OBSERVED",
                    "observed_dimensions": sorted({(int(item["viewport"]["width"]), int(item["viewport"]["height"])) for item in matches}),
                    "evidence": [item["evidence"] for item in matches],
                }
            )
        role_viewport_rows = []
        for role_row in role_rows:
            accepted_roles = set(role_row["accepted_authorization_roles"])
            for viewport_row in viewport_rows:
                expected_viewport = str(viewport_row["viewport"])
                matches = [
                    item
                    for item in observations
                    if item["role"] in accepted_roles and _viewport_matches(item["viewport"], expected_viewport, viewport_contracts)
                ]
                role_viewport_rows.append(
                    {
                        "role": role_row["role"],
                        "accepted_authorization_roles": sorted(accepted_roles),
                        "viewport": expected_viewport,
                        "status": "PASS" if matches else "NOT_OBSERVED",
                        "evidence": [item["evidence"] for item in matches],
                    }
                )
        gaps = []
        if not observations:
            gaps.append("no passing screenshot attestation")
        gaps.extend(f"role {row['role']} was not observed" for row in role_rows if row["status"] != "PASS")
        gaps.extend(f"viewport {row['viewport']} was not observed" for row in viewport_rows if row["status"] != "PASS")
        gaps.extend(
            f"role {row['role']} at viewport {row['viewport']} was not observed" for row in role_viewport_rows if row["status"] != "PASS"
        )
        status = "FAIL" if gaps else "PASS"
        if gaps:
            surface_failures.append(f"{surface_id}: " + "; ".join(gaps))
        surface_rows.append(
            {
                "surface": surface_id,
                "page": page,
                "evidence_pages": evidence_pages,
                "status": status,
                "roles": role_rows,
                "viewports": viewport_rows,
                "role_viewport_matrix": role_viewport_rows,
                "observation_count": len(observations),
                "observations": observations,
                "gaps": gaps,
            }
        )
    report["surface_execution_coverage"] = {
        "status": "FAIL" if surface_failures else "PASS",
        "surface_count": len(surface_rows),
        "failures": surface_failures,
        "surfaces": surface_rows,
    }
    if surface_failures:
        report["status"] = "FAIL"
        message = "UI surface role/viewport execution coverage is incomplete"
        if message not in report.setdefault("errors", []):
            report["errors"].append(message)
    return report


def _included_sources(repository: Path, sources: dict[str, Any]) -> list[Path]:
    excluded = [str(item) for item in sources.get("exclude_from_discovery") or ()]
    result: set[Path] = set()
    for pattern in sources.get("include") or ():
        for path in repository.glob(str(pattern)):
            if not path.is_file():
                continue
            relative = str(path.relative_to(repository))
            if any(fnmatch.fnmatch(relative, pattern) for pattern in excluded):
                continue
            result.add(path)
    return sorted(result)


_LEXICAL_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _lexical_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    current: ast.AST | None = node
    while current is not None and not isinstance(current, _LEXICAL_SCOPES):
        current = parents.get(current)
    if current is None:
        raise ValueError("AST node has no lexical scope")
    return current


def _assignment_nodes(
    tree: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> dict[ast.AST, dict[str, list[tuple[int, ast.AST]]]]:
    assignments: dict[ast.AST, dict[str, list[tuple[int, ast.AST]]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        scope = _lexical_scope(parents.get(node, tree), parents)
        for target in targets:
            if isinstance(target, ast.Name):
                assignments.setdefault(scope, {}).setdefault(target.id, []).append((getattr(node, "lineno", 0), value))
    return assignments


def _static_string_rows(
    node: ast.AST,
    assignments: dict[ast.AST, dict[str, list[tuple[int, ast.AST]]]],
    parents: dict[ast.AST, ast.AST],
    *,
    owner: ast.AST,
    seen: frozenset[tuple[int, str]] = frozenset(),
) -> list[tuple[str, ...]]:
    literal = _static_string(node)
    if literal is not None:
        return [(literal,)]
    if isinstance(node, ast.Name):
        immediate_scope = _lexical_scope(owner, parents)
        scope: ast.AST | None = immediate_scope
        while scope is not None:
            marker = (id(scope), node.id)
            if marker in seen:
                return []
            candidates = assignments.get(scope, {}).get(node.id, ())
            if scope is immediate_scope:
                line = getattr(owner, "lineno", 0)
                candidates = [row for row in candidates if row[0] < line]
            if candidates:
                rows: list[tuple[str, ...]] = []
                for _, value in candidates:
                    rows.extend(
                        _static_string_rows(
                            value,
                            assignments,
                            parents,
                            owner=value,
                            seen=seen | {marker},
                        )
                    )
                return rows
            parent = parents.get(scope)
            scope = _lexical_scope(parent, parents) if parent is not None else None
        return []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        rows: list[tuple[str, ...]] = []
        for item in node.elts:
            if isinstance(item, (ast.Tuple, ast.List)):
                columns: list[str] = []
                for column in item.elts:
                    values = _static_string_rows(column, assignments, parents, owner=owner, seen=seen)
                    if len(values) != 1 or len(values[0]) != 1:
                        columns = []
                        break
                    columns.append(values[0][0])
                if columns:
                    rows.append(tuple(columns))
            else:
                rows.extend(_static_string_rows(item, assignments, parents, owner=owner, seen=seen))
        return rows
    return []


def _loop_bindings(
    target: ast.AST,
    iterator: ast.AST,
    assignments: dict[ast.AST, dict[str, list[tuple[int, ast.AST]]]],
    parents: dict[ast.AST, ast.AST],
    *,
    owner: ast.AST,
) -> dict[str, set[str]]:
    rows = _static_string_rows(iterator, assignments, parents, owner=owner)
    if isinstance(target, ast.Name):
        return {target.id: {value for row in rows for value in row}}
    if isinstance(target, (ast.Tuple, ast.List)):
        result: dict[str, set[str]] = {}
        for index, target_item in enumerate(target.elts):
            if not isinstance(target_item, ast.Name):
                continue
            result[target_item.id] = {row[index] for row in rows if len(row) > index}
        return result
    return {}


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _python_env_references(path: Path, relative: str) -> tuple[set[str], dict[str, list[str]], list[dict[str, Any]]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError as exc:
        return set(), {}, [{"path": relative, "line": exc.lineno or 0, "function": "", "kind": "parse", "expression": ""}]
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    assignments = _assignment_nodes(tree, parents)
    os_aliases = {"os"}
    environ_aliases: set[str] = set()
    getenv_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "os":
                    os_aliases.add(item.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for item in node.names:
                if item.name == "environ":
                    environ_aliases.add(item.asname or item.name)
                elif item.name == "getenv":
                    getenv_aliases.add(item.asname or item.name)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            is_environment = (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id in os_aliases
                and value.attr == "environ"
            ) or (isinstance(value, ast.Name) and value.id in environ_aliases)
            if not is_environment:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in environ_aliases:
                    environ_aliases.add(target.id)
                    changed = True

    def is_environment(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in os_aliases and node.attr == "environ"
        ) or (isinstance(node, ast.Name) and node.id in environ_aliases)

    def enclosing_function(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current
            current = parents.get(current)
        return None

    helper_values: dict[tuple[int, str], set[str]] = {}
    helper_dynamic: set[tuple[int, str]] = set()
    function_rows: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_rows.setdefault(function.name, []).append(function)
    functions = {name: rows[0] for name, rows in function_rows.items() if len(rows) == 1}
    for function_name, function in functions.items():
        parameters = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
        for index, parameter in enumerate(parameters):
            values: set[str] = set()
            saw_call = False
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != function_name:
                    continue
                saw_call = True
                argument: ast.AST | None = call.args[index] if index < len(call.args) else None
                if argument is None:
                    argument = next((item.value for item in call.keywords if item.arg == parameter.arg), None)
                if argument is not None:
                    rows = _static_string_rows(argument, assignments, parents, owner=call)
                    if rows:
                        values.update(value for row in rows for value in row)
                    else:
                        helper_dynamic.add((id(function), parameter.arg))
                else:
                    positional = [*function.args.posonlyargs, *function.args.args]
                    defaults = [None] * (len(positional) - len(function.args.defaults)) + list(function.args.defaults)
                    default: ast.AST | None = None
                    if index < len(positional):
                        default = defaults[index]
                    else:
                        default = function.args.kw_defaults[index - len(positional)]
                    if default is not None:
                        rows = _static_string_rows(default, assignments, parents, owner=function)
                        values.update(value for row in rows for value in row)
                    else:
                        helper_dynamic.add((id(function), parameter.arg))
            if values:
                helper_values[(id(function), parameter.arg)] = values
            if not saw_call:
                helper_dynamic.add((id(function), parameter.arg))

    def resolve_argument(node: ast.AST, owner: ast.AST) -> tuple[set[str], bool]:
        if not isinstance(node, ast.Name):
            rows = _static_string_rows(node, assignments, parents, owner=owner)
            return ({value for row in rows for value in row}, not bool(rows))
        current = parents.get(owner)
        while current is not None:
            if isinstance(current, (ast.For, ast.AsyncFor)):
                values = _loop_bindings(current.target, current.iter, assignments, parents, owner=current).get(node.id)
                if values:
                    return values, False
            if isinstance(current, (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
                for generator in current.generators:
                    values = _loop_bindings(
                        generator.target,
                        generator.iter,
                        assignments,
                        parents,
                        owner=current,
                    ).get(node.id)
                    if values:
                        return values, False
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parameters = {parameter.arg for parameter in (*current.args.posonlyargs, *current.args.args, *current.args.kwonlyargs)}
                if node.id in parameters:
                    key = (id(current), node.id)
                    return helper_values.get(key, set()), key in helper_dynamic
                break
            current = parents.get(current)
        rows = _static_string_rows(node, assignments, parents, owner=owner)
        return ({value for row in rows for value in row}, not bool(rows))

    found: set[str] = set()
    locations: dict[str, list[str]] = {}
    dynamic: list[dict[str, Any]] = []

    def record(argument: ast.AST, owner: ast.AST, kind: str) -> None:
        values, unresolved = resolve_argument(argument, owner)
        valid = sorted(value for value in values if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))
        if unresolved or not valid:
            expression = ast.unparse(argument) if hasattr(ast, "unparse") else type(argument).__name__
            function = enclosing_function(owner)
            dynamic.append(
                {
                    "path": relative,
                    "line": getattr(owner, "lineno", 0),
                    "function": function.name if function is not None else "<module>",
                    "kind": kind,
                    "expression": expression,
                }
            )
        for name in valid:
            found.add(name)
            locations.setdefault(name, []).append(f"{relative}:{getattr(owner, 'lineno', 0)}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            argument: ast.AST | None = None
            if (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id in os_aliases
                and function.attr == "getenv"
            ):
                argument = node.args[0] if node.args else None
            elif isinstance(function, ast.Name) and function.id in getenv_aliases:
                argument = node.args[0] if node.args else None
            elif isinstance(function, ast.Attribute) and function.attr in {"get", "setdefault", "pop"} and is_environment(function.value):
                argument = node.args[0] if node.args else None
            if argument is not None:
                call_kind = function.attr if isinstance(function, ast.Attribute) else function.id
                record(argument, node, f"call {call_kind}")
        elif isinstance(node, ast.Subscript) and is_environment(node.value):
            record(node.slice, node, "subscript")
        elif isinstance(node, ast.Compare) and any(is_environment(comparator) for comparator in node.comparators):
            if any(isinstance(operator, (ast.In, ast.NotIn)) for operator in node.ops):
                record(node.left, node, "membership")
    unique_dynamic = {
        (str(row["path"]), int(row["line"]), str(row["function"]), str(row["kind"]), str(row["expression"])): row for row in dynamic
    }
    return (
        found,
        {name: sorted(set(values)) for name, values in sorted(locations.items())},
        [unique_dynamic[key] for key in sorted(unique_dynamic)],
    )


def _discover_env_names(
    repository: Path,
    sources: dict[str, Any],
) -> tuple[set[str], dict[str, list[str]], list[str]]:
    prefixes = tuple(str(item) for item in sources.get("prefixes") or ())
    explicit = {str(item) for item in sources.get("explicit_standard_names") or ()}
    discovered: set[str] = set()
    locations: dict[str, list[str]] = {}
    errors: list[str] = []
    dynamic_references: list[dict[str, Any]] = []
    for path in _included_sources(repository, sources):
        relative = str(path.relative_to(repository))
        text = path.read_text(encoding="utf-8", errors="replace")
        names: set[str] = set()
        if path.name.startswith(".env"):
            names.update(re.findall(r"(?m)^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", text))
        elif path.suffix == ".py":
            python_names, python_locations, python_errors = _python_env_references(path, relative)
            names.update(python_names)
            dynamic_references.extend(python_errors)
            for name, values in python_locations.items():
                locations.setdefault(name, []).extend(values)
        elif path.suffix in {".sh", ".yaml", ".yml"} or path.name in {"Makefile", "makefile"}:
            expansion_text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            reference_matches = [
                *re.finditer(r"\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)", expansion_text),
                *re.finditer(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", expansion_text),
            ]
            assignment_ends: dict[str, list[int]] = {}
            assignment = re.compile(
                r"(?m)^\s*(?:(?:local|readonly|declare(?:\s+-[A-Za-z]+)?|export)\s+)?"
                r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
            )
            for assignment_match in assignment.finditer(expansion_text):
                assignment_ends.setdefault(assignment_match.group(1), []).append(assignment_match.end())
            for match in reference_matches:
                name = match.group(1)
                external_namespace = name in explicit or name.startswith(prefixes)
                conventional_environment_name = name.upper() == name and len(name) > 1
                # Product namespaces and explicitly declared standard names are
                # always environment candidates, even when the script assigns
                # the same name later.  The former set-wide subtraction hid an
                # earlier inherited read in that common shell pattern.
                assigned_before = any(end < match.start() for end in assignment_ends.get(name, ()))
                if (external_namespace or conventional_environment_name) and not assigned_before:
                    names.add(name)
            indirect = sorted(set(re.findall(r"\$\{!([A-Za-z_][A-Za-z0-9_]*)", expansion_text)))
            for variable in indirect:
                dynamic_references.append(
                    {
                        "path": relative,
                        "line": next(
                            (
                                index
                                for index, line in enumerate(expansion_text.splitlines(), 1)
                                if re.search(rf"\$\{{!{re.escape(variable)}(?:\b|[:}}])", line)
                            ),
                            0,
                        ),
                        "function": "<shell>",
                        "kind": "shell-indirect",
                        "expression": variable,
                    }
                )
        for name in names:
            discovered.add(name)
            locations.setdefault(name, []).append(relative)
    raw_exclusions = sources.get("dynamic_reference_exclusions") or []
    if not isinstance(raw_exclusions, list):
        errors.append("sources.dynamic_reference_exclusions must be a list")
        raw_exclusions = []
    allowed_dynamic: set[int] = set()
    for exclusion_index, raw in enumerate(raw_exclusions, 1):
        if not isinstance(raw, dict):
            errors.append(f"dynamic environment exclusion #{exclusion_index} must be a mapping")
            continue
        expected_path = str(raw.get("path") or "")
        expected_function = str(raw.get("function") or "")
        expected_kind = str(raw.get("kind") or "")
        expected_expression = str(raw.get("expression") or "")
        reason = str(raw.get("reason") or "").strip()
        if not expected_path or not expected_function or not expected_kind or not expected_expression or len(reason) < 24:
            errors.append(
                f"dynamic environment exclusion #{exclusion_index} requires path, function, kind, exact expression, and a detailed reason"
            )
            continue
        matched = {
            index
            for index, record in enumerate(dynamic_references)
            if record.get("path") == expected_path
            and record.get("function") == expected_function
            and record.get("kind") == expected_kind
            and record.get("expression") == expected_expression
        }
        if len(matched) != 1:
            errors.append(
                "dynamic environment exclusion must match exactly one current reference: "
                f"{expected_path}:{expected_function}:{expected_kind}:{expected_expression}"
            )
            continue
        allowed_dynamic.update(matched)
    for index, record in enumerate(dynamic_references):
        if index in allowed_dynamic:
            continue
        errors.append(
            "unresolved dynamic environment "
            f"{record.get('kind')}: {record.get('path')}:{record.get('line')}:"
            f"{record.get('function')}:{record.get('expression')}"
        )
    return (
        discovered,
        {name: sorted(set(paths)) for name, paths in sorted(locations.items())},
        sorted(set(errors)),
    )


def _outcome_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("outcome") or "")
    return ""


def _env_example_literal_defaults(path: Path) -> dict[str, str]:
    """Return only active, static KEY=value defaults from ``.env.example``."""
    if not path.is_file():
        return {}
    defaults: dict[str, str] = {}
    assignment = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = assignment.match(line)
        if match is None:
            continue
        raw_value = match.group(2).strip()
        if any(token in raw_value for token in ("${", "$(", "`")):
            continue
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError:
            continue
        if len(parts) > 1:
            continue
        defaults[match.group(1)] = parts[0] if parts else ""
    return defaults


def _literal_default_valid_collisions(
    repository: Path,
    variables: dict[str, Any],
) -> list[str]:
    """Reject a test value that cannot distinguish an active example default."""
    defaults = _env_example_literal_defaults(repository / ".env.example")
    errors: list[str] = []
    for name, raw in variables.items():
        if not isinstance(raw, dict) or raw.get("classification") != "tested":
            continue
        if raw.get("secret") is True or raw.get("scenario_set") == "path":
            continue
        test_values = raw.get("test_values")
        if not isinstance(test_values, dict):
            continue
        valid = test_values.get("valid")
        if not isinstance(valid, (str, int, float, bool)):
            continue
        default = defaults.get(str(name))
        if default is not None and default == str(valid):
            equivalence_reason = str(raw.get("valid_matches_default_reason") or "").strip()
            if len(equivalence_reason) < 24:
                errors.append(f"{name}: tested literal valid duplicates .env.example default: {default!r}")
        elif raw.get("valid_matches_default_reason"):
            errors.append(f"{name}: valid_matches_default_reason is stale because valid differs from the example default")
    return errors


def _validate_tested_contracts(
    variables: dict[str, Any],
    scenario_catalog: dict[str, Any],
) -> tuple[list[str], dict[str, int], set[str]]:
    errors: list[str] = []
    tested = 0
    explicit_value_contracts = 0
    required_explicit_value_contracts = 0
    outcome_contracts = 0
    declared_observables: set[str] = set()
    for name, raw in variables.items():
        if not isinstance(raw, dict) or raw.get("classification") != "tested":
            continue
        tested += 1
        scenario_set = str(raw.get("scenario_set") or "")
        catalog = scenario_catalog.get(scenario_set)
        if not isinstance(catalog, dict):
            errors.append(f"{name}: scenario_set is absent from scenario_catalog: {scenario_set}")
        observables = raw.get("observable") or ()
        if not isinstance(observables, list) or not observables:
            errors.append(f"{name}: observable must be a non-empty list")
            observables = ()
        for observable in observables:
            probe_id = str(observable)
            declared_observables.add(probe_id)
            if probe_id not in OBSERVABLE_ADAPTER_IDS:
                errors.append(f"{name}: observable has no registered real adapter: {probe_id}")
            elif probe_adapter_name(probe_id) == "explicit-failure":
                errors.append(f"{name}: observable resolves only to explicit failure: {probe_id}")

        test_values = raw.get("test_values")
        if isinstance(test_values, dict) and set(test_values) >= {"default", "valid", "boundary", "invalid", "precedence", "restart"}:
            explicit_value_contracts += 1
        required_explicit_value_contracts += 1
        if not isinstance(test_values, dict):
            errors.append(f"{name}: tested variable requires an explicit test_values mapping")
        else:
            missing = sorted({"default", "valid", "boundary", "invalid", "precedence", "restart"} - set(test_values))
            if missing:
                errors.append(f"{name}: explicit test_values is missing scenarios: {', '.join(missing)}")

        outcomes = raw.get("expected_outcomes")
        for scenario in ("boundary", "invalid"):
            if not isinstance(outcomes, dict) or scenario not in outcomes:
                errors.append(f"{name}: expected_outcomes.{scenario} is required")
                continue
            declaration = outcomes[scenario]
            outcome = _outcome_name(declaration)
            if outcome not in _EXPECTED_OUTCOMES:
                errors.append(f"{name}: expected_outcomes.{scenario} has invalid outcome: {outcome or '<empty>'}")
                continue
            outcome_contracts += 1
            if isinstance(declaration, dict):
                stage = str(declaration.get("failure_stage") or "")
                patterns = declaration.get("error_patterns") or ()
                error_sources = declaration.get("error_sources") or ()
            else:
                stage = ""
                patterns = ()
                error_sources = ()
            if outcome == "reject":
                if not stage or not isinstance(patterns, list) or not patterns:
                    errors.append(f"{name}: reject outcome for {scenario} requires failure_stage and error_patterns")
                if not isinstance(error_sources, list) or not error_sources or set(map(str, error_sources)) - VALID_REJECTION_ERROR_SOURCES:
                    errors.append(
                        f"{name}: reject outcome for {scenario} requires error_sources from "
                        + ", ".join(sorted(VALID_REJECTION_ERROR_SOURCES))
                    )
            if outcome != "reject" and stage:
                errors.append(f"{name}: failure_stage is only valid for a reject outcome ({scenario})")
            if outcome == "explicit-error":
                if not isinstance(patterns, list) or not patterns:
                    errors.append(f"{name}: explicit-error for {scenario} requires error_patterns")
                if not isinstance(error_sources, list) or not error_sources or set(map(str, error_sources)) - _ERROR_SOURCES:
                    errors.append(f"{name}: explicit-error for {scenario} requires error_sources from " + ", ".join(sorted(_ERROR_SOURCES)))
    return (
        errors,
        {
            "tested_variables": tested,
            "explicit_test_value_contracts": explicit_value_contracts,
            "required_explicit_test_value_contracts": required_explicit_value_contracts,
            "outcome_contracts": outcome_contracts,
            "required_outcome_contracts": tested * 2,
            "registered_observables": len(declared_observables & OBSERVABLE_ADAPTER_IDS),
            "declared_observables": len(declared_observables),
        },
        declared_observables,
    )


def validate_environment_manifest(repository: Path, config_root: Path) -> dict[str, Any]:
    manifest = _yaml(config_root / "env_matrix.yaml")
    variables = manifest.get("variables") or {}
    errors: list[str] = []
    if not isinstance(variables, dict):
        return {"status": "FAIL", "errors": ["env variables must be a mapping"]}
    scenario_catalog = manifest.get("scenario_catalog") or {}
    contract_errors, contract_counts, declared_observables = _validate_tested_contracts(variables, scenario_catalog)
    errors.extend(contract_errors)
    literal_default_collisions = _literal_default_valid_collisions(repository, variables)
    errors.extend(literal_default_collisions)
    contract_counts["literal_default_valid_collisions"] = len(literal_default_collisions)
    manifest_observables = declared_observable_ids(config_root / "env_matrix.yaml")
    missing_manifest_adapters = sorted(manifest_observables - OBSERVABLE_ADAPTER_IDS)
    for probe_id in missing_manifest_adapters:
        errors.append(f"manifest observable has no registered real adapter: {probe_id}")
    explicit_failure_adapters = sorted(probe_id for probe_id in manifest_observables if probe_adapter_name(probe_id) == "explicit-failure")
    for probe_id in explicit_failure_adapters:
        errors.append(f"manifest observable resolves only to explicit failure: {probe_id}")
    semantic_registry = registry_summary(config_root / "env_matrix.yaml")
    semantic_gaps = sorted(str(value) for value in semantic_registry.get("explicit_failure") or ())
    for probe_id in semantic_gaps:
        if probe_id not in explicit_failure_adapters:
            errors.append(f"manifest observable has no completed semantic adapter: {probe_id}")
    pair_semantic_gaps = semantic_registry.get("pair_semantic_gaps") or ()
    for gap in pair_semantic_gaps:
        if not isinstance(gap, dict):
            errors.append(f"manifest has an invalid semantic pair gap record: {gap!r}")
            continue
        variable = str(gap.get("variable") or "")
        probe_id = str(gap.get("probe_id") or "")
        legacy_adapter = str(gap.get("legacy_adapter") or "explicit-failure")
        errors.append(f"{variable}: observable {probe_id} has no variable-specific semantic adapter (legacy fallback: {legacy_adapter})")
    contract_counts["manifest_observables"] = len(manifest_observables)
    contract_counts["manifest_observables_registered"] = len(manifest_observables & OBSERVABLE_ADAPTER_IDS)
    contract_counts["manifest_observables_semantic_gaps"] = len(semantic_gaps)
    contract_counts["manifest_observable_pairs"] = int(semantic_registry.get("declared_pair_count") or 0)
    contract_counts["manifest_observable_pairs_resolved"] = int(semantic_registry.get("resolved_pair_count") or 0)
    contract_counts["manifest_observable_pair_semantic_gaps"] = len(pair_semantic_gaps)
    execution_coverage = manifest.get("execution_coverage") or {}
    execution = manifest.get("execution") or {}
    coverage_case = str(execution_coverage.get("case_nodeid") or "")
    coverage_variables = execution_coverage.get("variables") or {}
    profile_definitions = manifest.get("profiles") or {}
    pairwise_rows: list[dict[str, Any]] = []
    generated_profiles = []
    if execution.get("generate_profiles_from_variables") is True:
        try:
            generated_profiles = generate_environment_profiles(manifest, repository)
        except ValueError as exc:
            errors.append(f"generated environment profile contract is invalid: {exc}")
    else:
        errors.append("execution.generate_profiles_from_variables must be true for tested variables")
    generated_by_key = {
        (profile.generated_scenario.variable, profile.generated_scenario.scenario): profile
        for profile in generated_profiles
        if profile.generated_scenario is not None
    }
    pairwise_profiles = []
    try:
        pairwise_profiles = generate_pairwise_profiles(manifest)
    except ValueError as exc:
        errors.append(str(exc))
    all_profile_definitions = dict(profile_definitions)
    for profile in generated_profiles:
        all_profile_definitions[profile.name] = {"suites": ["env"], "generated": True}
    for profile in pairwise_profiles:
        all_profile_definitions[profile.name] = {
            "suites": list(profile.suites),
            "generated": True,
            "case_nodeids": list(profile.case_nodeids),
        }
    if coverage_case:
        ok, detail = _case_exists(repository / "ui_automation", coverage_case)
        if not ok:
            errors.append(detail)
    if not isinstance(coverage_variables, dict):
        errors.append("execution_coverage.variables must be a mapping")
        coverage_variables = {}
    pairwise_groups = execution.get("pairwise_groups") or ()
    pairwise_group_reports: list[dict[str, Any]] = []
    if not isinstance(pairwise_groups, list) or not pairwise_groups:
        errors.append("execution.pairwise_groups must define executable covering arrays")
        pairwise_groups = []
    profiles_by_group: dict[str, list[Any]] = {}
    for profile in pairwise_profiles:
        scenario = profile.pairwise_scenario
        if scenario is not None:
            profiles_by_group.setdefault(scenario.group_id, []).append(profile)
    for raw_group in pairwise_groups:
        if not isinstance(raw_group, dict):
            errors.append("each pairwise group must be a mapping")
            continue
        group_id = str(raw_group.get("id") or "")
        group_variables = [str(item) for item in raw_group.get("variables") or ()]
        raw_factors = raw_group.get("factors") or {}
        case_nodeid = str(raw_group.get("case_nodeid") or "")
        group_profiles = profiles_by_group.get(group_id, [])
        required_pairs: set[tuple[str, str, str, str]] = set()
        if isinstance(raw_factors, dict):
            for left_index, left in enumerate(group_variables):
                left_levels = [str(item.get("id")) for item in raw_factors.get(left) or () if isinstance(item, dict) and item.get("id")]
                for right in group_variables[left_index + 1 :]:
                    right_levels = [
                        str(item.get("id")) for item in raw_factors.get(right) or () if isinstance(item, dict) and item.get("id")
                    ]
                    required_pairs.update(
                        (left, left_level, right, right_level) for left_level in left_levels for right_level in right_levels
                    )
        covered_pairs: set[tuple[str, str, str, str]] = set()
        for profile in group_profiles:
            scenario = profile.pairwise_scenario
            if scenario is None:
                continue
            suites = set(profile.suites)
            if not {"full", "env"}.issubset(suites):
                errors.append(f"pairwise {group_id}/{scenario.row_id}: profile must run in full and env suites")
            if case_nodeid not in set(profile.case_nodeids):
                errors.append(f"pairwise {group_id}/{scenario.row_id}: coverage case is not assigned")
            factor_levels = {factor.key: factor.level for factor in scenario.factors}
            if set(factor_levels) != set(group_variables):
                errors.append(f"pairwise {group_id}/{scenario.row_id}: factor keys do not match group")
            for left_index, left in enumerate(group_variables):
                for right in group_variables[left_index + 1 :]:
                    covered_pairs.add((left, factor_levels.get(left, ""), right, factor_levels.get(right, "")))
            case_path, _, case_name = case_nodeid.partition("::")
            parts = Path(case_path).parts
            feature = parts[parts.index("cases") + 1] if "cases" in parts and parts.index("cases") + 1 < len(parts) else "environment"
            pairwise_rows.append(
                {
                    "id": group_id,
                    "row_id": scenario.row_id,
                    "variables": group_variables,
                    "levels": factor_levels,
                    "profile": profile.name,
                    "case": case_nodeid,
                    "evidence": f"{profile.name}/{feature}/{case_name}/result.json",
                    "observation_evidence": (f"{profile.name}/{feature}/{case_name}/state/pairwise-observed.json"),
                    "contract_evidence": f"{profile.name}/state/pairwise-effective.json",
                    "status": "DECLARED",
                }
            )
        missing_pairs = sorted(required_pairs - covered_pairs)
        if missing_pairs:
            errors.append(f"pairwise {group_id}: covering array misses {len(missing_pairs)} factor-value pairs")
        if not group_profiles:
            errors.append(f"pairwise {group_id}: no executable rows were generated")
        ok, detail = _case_exists(repository / "ui_automation", case_nodeid)
        if not ok:
            errors.append(f"pairwise {group_id}: {detail}")
        pairwise_group_reports.append(
            {
                "id": group_id,
                "factor_count": len(group_variables),
                "row_count": len(group_profiles),
                "required_value_pairs": len(required_pairs),
                "covered_value_pairs": len(covered_pairs & required_pairs),
                "missing_value_pairs": len(missing_pairs),
                "status": "PASS" if group_profiles and not missing_pairs else "FAIL",
            }
        )
    scenario_coverage: dict[str, Any] = {}
    unassigned_count = 0
    incomplete_assignments = 0
    for name, raw in variables.items():
        if not isinstance(raw, dict):
            errors.append(f"{name}: classification must be a mapping")
            continue
        classification = raw.get("classification")
        if classification not in {"tested", "excluded"}:
            errors.append(f"{name}: classification must be tested or excluded")
        elif classification == "excluded" and not str(raw.get("exclude_reason") or "").strip():
            errors.append(f"{name}: excluded variable requires exclude_reason")
        elif classification == "tested":
            for key in ("scenario_set", "restart", "observable"):
                if not raw.get(key):
                    errors.append(f"{name}: tested variable requires {key}")
            scenario_set = str(raw.get("scenario_set") or "")
            catalog = scenario_catalog.get(scenario_set) or {}
            required_scenarios = [str(item) for item in (execution_coverage.get("required_scenarios") or catalog.get("required") or ())]
            assignments: dict[str, list[dict[str, str]]] = {}
            for scenario in required_scenarios:
                generated = generated_by_key.get((str(name), scenario))
                if generated is None:
                    continue
                if generated.expect_startup_failure:
                    generated_case = "runner::expected_startup_failure"
                    evidence = f"{generated.name}/result.json"
                    outcome_evidence = evidence
                else:
                    generated_case = coverage_case
                    case_path, _, case_name = coverage_case.partition("::")
                    parts = Path(case_path).parts
                    feature = (
                        parts[parts.index("cases") + 1] if "cases" in parts and parts.index("cases") + 1 < len(parts) else "environment"
                    )
                    evidence = f"{generated.name}/{feature}/{case_name}/result.json"
                    outcome_evidence = f"{generated.name}/{feature}/{case_name}/state/environment-outcome.json"
                assignments.setdefault(scenario, []).append(
                    {
                        "profile": generated.name,
                        "case": generated_case,
                        "evidence": evidence,
                        "outcome_evidence": outcome_evidence,
                        "expected_outcome": generated.generated_scenario.expected_outcome,
                        "generated": "true",
                    }
                )
            coverage_mapping = coverage_variables.get(str(name)) or {}
            if isinstance(coverage_mapping, dict):
                for scenario, profile_raw in coverage_mapping.items():
                    if isinstance(profile_raw, dict):
                        assignment = {str(key): str(value) for key, value in profile_raw.items() if value is not None}
                    else:
                        assignment = {"profile": str(profile_raw)}
                    if coverage_case:
                        assignment.setdefault("case", coverage_case)
                    if assignment.get("profile") and coverage_case:
                        mapped_profile = all_profile_definitions.get(assignment["profile"]) or {}
                        if isinstance(mapped_profile, dict) and mapped_profile.get("expect_startup_failure"):
                            assignment["case"] = "runner::expected_startup_failure"
                            assignment.setdefault("evidence", f"{assignment['profile']}/result.json")
                        else:
                            case_path, _, case_name = coverage_case.partition("::")
                            parts = Path(case_path).parts
                            feature = (
                                parts[parts.index("cases") + 1]
                                if "cases" in parts and parts.index("cases") + 1 < len(parts)
                                else "environment"
                            )
                            assignment.setdefault("evidence", f"{assignment['profile']}/{feature}/{case_name}/result.json")
                    assignments.setdefault(str(scenario), []).append(assignment)
            declared = raw.get("execution_cases") or {}
            if isinstance(declared, dict):
                for scenario, assignment_raw in declared.items():
                    rows = assignment_raw if isinstance(assignment_raw, list) else [assignment_raw]
                    for row in rows:
                        if isinstance(row, str):
                            assignment = {"case": row}
                        elif isinstance(row, dict):
                            assignment = {str(key): str(value) for key, value in row.items() if value is not None}
                        else:
                            assignment = {}
                        assignments.setdefault(str(scenario), []).append(assignment)
            scenarios_list = raw.get("scenarios") or ()
            if isinstance(scenarios_list, list):
                for row in scenarios_list:
                    if not isinstance(row, dict) or not row.get("id"):
                        continue
                    scenario = str(row["id"])
                    assignments.setdefault(scenario, []).append(
                        {str(key): str(value) for key, value in row.items() if key != "id" and value is not None}
                    )
            rows: list[dict[str, Any]] = []
            for scenario in required_scenarios:
                assigned = assignments.get(scenario, [])
                complete = [item for item in assigned if item.get("profile") and item.get("case") and item.get("evidence")]
                for item in complete:
                    profile_name = item["profile"]
                    profile_raw = all_profile_definitions.get(profile_name)
                    if not isinstance(profile_raw, dict):
                        errors.append(f"{name}/{scenario}: execution profile does not exist: {profile_name}")
                    elif "env" not in {str(value) for value in profile_raw.get("suites") or ()}:
                        errors.append(f"{name}/{scenario}: execution profile is not in env suite: {profile_name}")
                    case_nodeid = item["case"]
                    if case_nodeid not in {
                        "runner::expected_startup_failure",
                        "runner::generated_environment_scenario",
                        "runner::precondition_failure",
                    }:
                        ok, detail = _case_exists(repository / "ui_automation", case_nodeid)
                        if not ok:
                            errors.append(f"{name}/{scenario}: {detail}")
                status = "DECLARED" if complete else "UNASSIGNED"
                if not assigned:
                    unassigned_count += 1
                elif not complete:
                    incomplete_assignments += 1
                rows.append({"scenario": scenario, "assignments": assigned, "status": status})
            scenario_coverage[str(name)] = {
                "scenario_set": scenario_set,
                "required": rows,
            }
    discovered, locations, discovery_errors = _discover_env_names(repository, manifest.get("sources") or {})
    errors.extend(discovery_errors)
    classified = {str(name) for name in variables}
    unknown = sorted(discovered - classified)
    if unknown:
        errors.append("unclassified environment variables: " + ", ".join(unknown))
    if unassigned_count:
        errors.append(f"environment scenario assignments are missing: {unassigned_count}")
    if incomplete_assignments:
        errors.append(f"environment scenario assignments are incomplete: {incomplete_assignments}")
    return {
        "status": "FAIL" if errors else "PASS",
        "errors": errors,
        "counts": {
            "discovered": len(discovered),
            "classified": len(classified),
            "unknown": len(unknown),
            "dynamic_reference_errors": len(discovery_errors),
        },
        "contract_counts": contract_counts,
        "observable_registry": {
            "declared": sorted(manifest_observables),
            "registered": sorted(manifest_observables & OBSERVABLE_ADAPTER_IDS),
            "missing": missing_manifest_adapters,
            "explicit_failure": explicit_failure_adapters,
            "tested_variable_declared": sorted(declared_observables),
        },
        "unknown": [{"name": name, "sources": locations.get(name, [])} for name in unknown],
        "dynamic_reference_errors": discovery_errors,
        "classified_not_currently_discovered": sorted(classified - discovered),
        "scenario_coverage": scenario_coverage,
        "scenario_counts": {
            "unassigned": unassigned_count,
            "incomplete": incomplete_assignments,
        },
        "auto_generation_used": bool(generated_profiles),
        "generated_profile_count": len(generated_profiles),
        "pairwise_generated_profile_count": len(pairwise_profiles),
        "pairwise_groups": pairwise_group_reports,
        "pairwise_coverage": pairwise_rows,
    }


def finalize_environment_coverage(
    report: dict[str, Any],
    *,
    run_root: Path,
    profile_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """宣言済みscenarioを実行結果と証跡へ突合し、未実行を失敗のまま残す。"""
    status_by_profile = {str(item.get("profile")): str(item.get("status")) for item in profile_results}
    counts = {"pass": 0, "fail": 0, "not_run": 0, "unassigned": 0, "evidence_missing": 0}
    for variable in (report.get("scenario_coverage") or {}).values():
        for scenario in variable.get("required") or ():
            assignments = scenario.get("assignments") or []
            complete = [item for item in assignments if item.get("profile") and item.get("case") and item.get("evidence")]
            if not complete:
                scenario["status"] = "UNASSIGNED"
                counts["unassigned"] += 1
                continue
            outcomes: list[dict[str, str]] = []
            for assignment in complete:
                profile = str(assignment["profile"])
                profile_status = status_by_profile.get(profile, "NOT_RUN")
                evidence_pattern = str(assignment["evidence"])
                evidence = sorted(str(path.relative_to(run_root)) for path in run_root.glob(evidence_pattern))
                expected_outcome = str(assignment.get("expected_outcome") or "")
                outcome_evidence_pattern = str(assignment.get("outcome_evidence") or "")
                outcome_evidence = sorted(run_root.glob(outcome_evidence_pattern)) if outcome_evidence_pattern else []
                outcome_errors: list[str] = []
                if expected_outcome == "reject":
                    if not outcome_evidence:
                        outcome_errors.append("runner rejection evidence is missing")
                    else:
                        try:
                            payload = json.loads(outcome_evidence[0].read_text(encoding="utf-8"))
                            if (
                                not isinstance(payload, dict)
                                or payload.get("status") != "PASS"
                                or payload.get("stage") != "expected_rejection"
                            ):
                                outcome_errors.append("runner did not attest the declared startup rejection")
                        except (OSError, ValueError):
                            outcome_errors.append("runner rejection evidence is malformed")
                elif expected_outcome in {"explicit-error", "accepted-boundary"}:
                    if not outcome_evidence:
                        outcome_errors.append("environment actual-outcome evidence is missing")
                    else:
                        try:
                            payload = json.loads(outcome_evidence[0].read_text(encoding="utf-8"))
                            evidence_sources = payload.get("evidence_refs") if isinstance(payload, dict) else None
                            if (
                                not isinstance(payload, dict)
                                or payload.get("expected_outcome") != expected_outcome
                                or payload.get("actual_outcome") != expected_outcome
                                or payload.get("matched") is not True
                                or not evidence_sources
                            ):
                                outcome_errors.append("actual product outcome does not match the declared outcome")
                        except (OSError, ValueError):
                            outcome_errors.append("environment actual-outcome evidence is malformed")
                if profile_status == "NOT_RUN":
                    outcome = "NOT_RUN"
                elif profile_status != "PASS":
                    outcome = "FAIL"
                elif not evidence:
                    outcome = "EVIDENCE_MISSING"
                elif outcome_errors:
                    outcome = "EVIDENCE_MISSING" if any("missing" in message for message in outcome_errors) else "FAIL"
                else:
                    outcome = "PASS"
                outcomes.append(
                    {
                        "profile": profile,
                        "status": outcome,
                        "evidence": ",".join(evidence),
                        "outcome_evidence": ",".join(str(path.relative_to(run_root)) for path in outcome_evidence),
                        "outcome_errors": "; ".join(outcome_errors),
                    }
                )
            scenario["executions"] = outcomes
            statuses = {item["status"] for item in outcomes}
            scenario["status"] = (
                "PASS" if statuses == {"PASS"} else next(status for status in ("FAIL", "NOT_RUN", "EVIDENCE_MISSING") if status in statuses)
            )
            key = scenario["status"].lower()
            counts[key if key in counts else "fail"] += 1
    report["execution_counts"] = counts
    pairwise_counts = {"pass": 0, "fail": 0, "not_run": 0, "evidence_missing": 0}
    for row in report.get("pairwise_coverage") or ():
        profile = str(row.get("profile") or "")
        profile_status = status_by_profile.get(profile, "NOT_RUN")
        evidence_paths = sorted(run_root.glob(str(row.get("evidence") or "")))
        observation_paths = sorted(run_root.glob(str(row.get("observation_evidence") or "")))
        contract_paths = sorted(run_root.glob(str(row.get("contract_evidence") or "")))
        evidence = [str(path.relative_to(run_root)) for path in (*evidence_paths, *observation_paths, *contract_paths)]
        validation_errors: list[str] = []
        if evidence_paths:
            try:
                case_payload = json.loads(evidence_paths[0].read_text(encoding="utf-8"))
                if (
                    not isinstance(case_payload, dict)
                    or case_payload.get("nodeid") != row.get("case")
                    or case_payload.get("outcome") != "passed"
                ):
                    validation_errors.append("case result is not a passing result for the declared nodeid")
            except (OSError, ValueError):
                validation_errors.append("case result evidence is malformed")
        if observation_paths:
            try:
                observation = json.loads(observation_paths[0].read_text(encoding="utf-8"))
                if not isinstance(observation, dict) or observation.get("id") != row.get("id"):
                    validation_errors.append("browser observation does not match the pairwise group")
            except (OSError, ValueError):
                validation_errors.append("browser observation evidence is malformed")
        if contract_paths:
            try:
                contract = json.loads(contract_paths[0].read_text(encoding="utf-8"))
                contract_factors = contract.get("factors") if isinstance(contract, dict) else None
                if (
                    not isinstance(contract, dict)
                    or contract.get("status") != "PASS"
                    or contract.get("profile") != profile
                    or contract.get("group_id") != row.get("id")
                    or contract.get("row_id") != row.get("row_id")
                    or contract.get("all_effective_values_matched") is not True
                    or not isinstance(contract_factors, list)
                    or not contract_factors
                    or any(not isinstance(factor, dict) or factor.get("matched") is not True for factor in contract_factors)
                ):
                    validation_errors.append("effective-value contract does not attest this passing row")
                elif any(factor.get("sensitive") is True and ("expected" in factor or "actual" in factor) for factor in contract_factors):
                    validation_errors.append("secret factor evidence contains a value field")
            except (OSError, ValueError):
                validation_errors.append("effective-value contract evidence is malformed")
        if profile_status == "NOT_RUN":
            status = "NOT_RUN"
        elif profile_status != "PASS":
            status = "FAIL"
        elif not evidence_paths or not observation_paths or not contract_paths:
            status = "EVIDENCE_MISSING"
        elif validation_errors:
            status = "FAIL"
        else:
            status = "PASS"
        row["status"] = status
        row["execution_evidence"] = evidence
        row["evidence_validation_errors"] = validation_errors
        pairwise_counts[status.lower()] += 1
    report["pairwise_execution_counts"] = pairwise_counts
    if any(counts[key] for key in ("fail", "not_run", "unassigned", "evidence_missing")):
        report["status"] = "FAIL"
        if "environment scenario execution coverage is incomplete" not in report.setdefault("errors", []):
            report["errors"].append("environment scenario execution coverage is incomplete")
    if any(pairwise_counts[key] for key in ("fail", "not_run", "evidence_missing")):
        report["status"] = "FAIL"
        if "pairwise environment execution coverage is incomplete" not in report.setdefault("errors", []):
            report["errors"].append("pairwise environment execution coverage is incomplete")
    return report
