"""実サービス試験から通信差し替え手段を排除する静的検査。"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from ui_automation.runner.filesystem_safety import assert_no_mount_targets


@dataclass(frozen=True)
class PolicyViolation:
    path: str
    line: int
    rule: str

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "rule": self.rule}


_FORBIDDEN_IMPORTS = {
    "unittest." + "mock",
    "pytest_" + "mock",
    "requests_" + "mock",
    "pytest_httpserver",
    "respx",
    "responses",
}
_FORBIDDEN_NAMES = {
    "M" + "ock",
    "Magic" + "Mock",
    "Async" + "Mock",
    "m" + "ock",
    "p" + "atch",
    "m" + "ocker",
    "monkeypatch",
    "Mock" + "Transport",
}
_FORBIDDEN_REFERENCE_PARTS = {"mock_server", "fake_provider", "stub_provider"}
_FORBIDDEN_DYNAMIC_CALLS = {"__import__", "eval", "exec"}
_NETWORK_INTERCEPTION_METHODS = {
    "abort",
    "continue_",
    "fallback",
    "fulfill",
    "route",
    "route_from_har",
    "route_web_socket",
    "set_offline",
    "unroute",
    "unroute_all",
}
_ALLOWED_BROWSER_PROTOCOL_COMMANDS = {
    # The legacy-navigation case needs JavaScript disabled to exercise the real
    # noscript link. It does not alter requests or responses.
    "Emulation.setScriptExecutionDisabled",
}
_BROWSER_PROTOCOL_INTERCEPTION_PREFIXES = (
    "Fetch.",
    "Network.continueInterceptedRequest",
    "Network.setBlockedURLs",
    "Network.setRequestInterception",
)
_POLICY_TOKEN_RULES = {
    "page.route(": "network_interception",
    "context.route(": "network_interception",
    "route.fulfill(": "network_interception",
    "route.abort(": "network_interception",
    "route_from_har": "network_interception",
    "route_web_socket": "network_interception",
    "set_offline(": "network_interception",
    "unroute": "network_interception",
    "unroute_all": "network_interception",
    "Fetch.enable": "network_interception",
    "Fetch.fulfillRequest": "network_interception",
    "Fetch.continueRequest": "network_interception",
    "Fetch.failRequest": "network_interception",
    "Network.setRequestInterception": "network_interception",
    "Network.continueInterceptedRequest": "network_interception",
    "window.fetch =": "network_interception",
    "globalThis.fetch =": "network_interception",
    "window.EventSource =": "network_interception",
    "globalThis.EventSource =": "network_interception",
    "XMLHttpRequest.prototype": "network_interception",
    "navigator.serviceWorker.register": "network_interception",
    "http.server": "test_double",
    "mock": "test_double",
    "mocker": "test_double",
    "monkeypatch": "test_double",
    "MockTransport": "network_interception",
    "stub": "test_double",
    "fake_provider": "test_double",
    "tests.": "existing_test_import",
    "conftest": "existing_test_import",
}
_JS_FORBIDDEN = (
    re.compile(r"\b(?:page|context|browserContext)\.route\s*\("),
    re.compile(
        r"\b(?:page|context|browserContext)\."
        r"(?:route_from_har|route_web_socket|set_offline|unroute|unroute_all)\s*\("
    ),
    re.compile(r"\broute\.(?:fulfill|abort|continue|fallback)\s*\("),
    re.compile(
        r"\b(?:Fetch\.(?:enable|fulfillRequest|continueRequest|failRequest)|"
        r"Network\.(?:setRequestInterception|continueInterceptedRequest|setBlockedURLs))\b"
    ),
    re.compile(r"\b(?:window|globalThis)(?:\.fetch|\[['\"]fetch['\"]\])\s*="),
    re.compile(r"\b(?:window|globalThis)(?:\.EventSource|\[['\"]EventSource['\"]\])\s*="),
    re.compile(r"\b(?:window|globalThis)(?:\.XMLHttpRequest|\[['\"]XMLHttpRequest['\"]\])\s*="),
    re.compile(r"\bXMLHttpRequest\.prototype\.(?:open|send)\s*="),
    re.compile(r"\bnavigator\.serviceWorker\.register\s*\("),
    re.compile(r"\b(?:sinon|nock|msw)\b", re.IGNORECASE),
)
_COMMAND_DOUBLE_PATTERNS = (
    re.compile(r"\bhttp\.server\b", re.IGNORECASE),
    re.compile(r"\b(?:BaseHTTPRequestHandler|HTTPServer|MockTransport|mitmproxy|mockserver|wiremock)\b", re.IGNORECASE),
    re.compile(r"\b(?:pytest_httpserver|requests_mock|respx|responses)\b", re.IGNORECASE),
)
_COMMAND_SESSION_ESCAPE_PATTERN = re.compile(r"(?:^|[\s/])setsid(?:\s|$)")


def _constant_string(node: ast.AST, bindings: dict[str, str] | None = None) -> str | None:
    """Safely fold only literal strings; never execute source being checked."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left, bindings)
        right = _constant_string(node.right, bindings)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            parts.append(value.value)
        return "".join(parts)
    if isinstance(node, ast.Name) and bindings is not None:
        return bindings.get(node.id)
    return None


def _literal_bindings(tree: ast.AST) -> dict[str, str]:
    """Collect unambiguous constant script/command bindings for injection checks."""

    values: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = _constant_string(node.value)
        for target in targets:
            if isinstance(target, ast.Name):
                values.setdefault(target.id, set()).add(value if value is not None else "<dynamic>")
    return {name: next(iter(candidates)) for name, candidates in values.items() if len(candidates) == 1 and "<dynamic>" not in candidates}


def _constant_strings(node: ast.AST, bindings: dict[str, str]) -> tuple[str, ...]:
    folded = _constant_string(node, bindings)
    if folded is not None:
        return (folded,)
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return tuple(value for item in node.elts for value in _constant_strings(item, bindings))
    if isinstance(node, ast.Dict):
        values: list[str] = []
        for key, value in zip(node.keys, node.values, strict=True):
            if key is not None:
                values.extend(_constant_strings(key, bindings))
            values.extend(_constant_strings(value, bindings))
        return tuple(values)
    return ()


def _script_uses_network_interception(script: str) -> bool:
    return any(pattern.search(script) for pattern in _JS_FORBIDDEN)


def _command_uses_test_double(command: str) -> bool:
    return any(pattern.search(command) for pattern in _COMMAND_DOUBLE_PATTERNS)


def _python_violations(path: Path, root: Path) -> list[PolicyViolation]:
    relative = str(path.relative_to(root))
    process_escape_restricted = Path(relative).parts[0] in {"cases", "support"}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return [PolicyViolation(relative, getattr(exc, "lineno", 0) or 0, "source_not_parseable")]
    violations: list[PolicyViolation] = []
    bindings = _literal_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_IMPORTS or alias.name.startswith("unittest." + "mock"):
                    violations.append(PolicyViolation(relative, node.lineno, "test_double_import"))
                if alias.name == "tests" or alias.name.startswith("tests.") or alias.name == "conftest":
                    violations.append(PolicyViolation(relative, node.lineno, "existing_test_import"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in _FORBIDDEN_IMPORTS or module.startswith("unittest." + "mock"):
                violations.append(PolicyViolation(relative, node.lineno, "test_double_import"))
            if module == "unittest" and any(alias.name in {"mock", "patch", "Mock", "MagicMock", "AsyncMock"} for alias in node.names):
                violations.append(PolicyViolation(relative, node.lineno, "test_double_import"))
            if module == "tests" or module.startswith("tests.") or module == "conftest":
                violations.append(PolicyViolation(relative, node.lineno, "existing_test_import"))
            if process_escape_restricted and any(alias.name == "setsid" for alias in node.names):
                violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
            if (
                process_escape_restricted
                and module == "subprocess"
                and any(alias.name in {"Popen", "call", "check_call", "check_output", "run"} for alias in node.names)
            ):
                violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
            if process_escape_restricted and module == "os" and any(alias.name in {"posix_spawn", "posix_spawnp"} for alias in node.names):
                violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES:
                violations.append(PolicyViolation(relative, node.lineno, "test_double_symbol"))
            if node.id in _FORBIDDEN_DYNAMIC_CALLS:
                violations.append(PolicyViolation(relative, node.lineno, "dynamic_code_loading"))
            if any(part in node.id.lower() for part in _FORBIDDEN_REFERENCE_PARTS):
                violations.append(PolicyViolation(relative, node.lineno, "test_double_reference"))
        elif isinstance(node, (ast.Name, ast.Attribute)):
            identifier = node.id if isinstance(node, ast.Name) else node.attr
            if identifier in _FORBIDDEN_NAMES:
                violations.append(PolicyViolation(relative, node.lineno, "test_double_symbol"))
            if any(part in identifier.lower() for part in _FORBIDDEN_REFERENCE_PARTS):
                violations.append(PolicyViolation(relative, node.lineno, "test_double_reference"))
            if process_escape_restricted and isinstance(node, ast.Attribute) and identifier == "setsid":
                violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
            if isinstance(node, ast.Attribute) and identifier in _NETWORK_INTERCEPTION_METHODS:
                # An interception method captured into an alias is still an
                # interception (``handler = page.route; handler(...)``).
                violations.append(PolicyViolation(relative, node.lineno, "network_interception"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # ownerの変数名を限定すると ``p = page; p.route(...)`` で回避できるため、
            # UI automation配下では通信差し替えAPIそのものを全面禁止する。
            if node.func.attr in _NETWORK_INTERCEPTION_METHODS:
                violations.append(PolicyViolation(relative, node.lineno, "network_interception"))
            if node.func.attr == "send":
                command = _constant_string(node.args[0], bindings) if node.args else None
                if command not in _ALLOWED_BROWSER_PROTOCOL_COMMANDS:
                    violations.append(PolicyViolation(relative, node.lineno, "unapproved_browser_protocol_command"))
                if command and command.startswith(_BROWSER_PROTOCOL_INTERCEPTION_PREFIXES):
                    violations.append(PolicyViolation(relative, node.lineno, "network_interception"))
            if node.func.attr in {"execute_cdp_cmd", "send_command"}:
                violations.append(PolicyViolation(relative, node.lineno, "unapproved_browser_protocol_command"))
            if node.func.attr in {"evaluate", "evaluate_handle", "add_init_script"}:
                arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
                if any(
                    _script_uses_network_interception(script) for argument in arguments for script in _constant_strings(argument, bindings)
                ):
                    violations.append(PolicyViolation(relative, node.lineno, "browser_script_network_interception"))
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in {"Popen", "call", "check_call", "check_output", "run"}
            ):
                arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
                if any(_command_uses_test_double(command) for argument in arguments for command in _constant_strings(argument, bindings)):
                    violations.append(PolicyViolation(relative, node.lineno, "test_double_process"))
                if process_escape_restricted:
                    commands = tuple(command for argument in arguments for command in _constant_strings(argument, bindings))
                    if any(_COMMAND_SESSION_ESCAPE_PATTERN.search(command) for command in commands):
                        violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
                    for keyword in node.keywords:
                        if keyword.arg is None:
                            violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
                        if keyword.arg == "start_new_session" and not (
                            isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                        ):
                            violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
                        if keyword.arg == "preexec_fn" and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None):
                            violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
            if process_escape_restricted and node.func.attr == "setsid":
                violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
            if process_escape_restricted and node.func.attr in {"posix_spawn", "posix_spawnp"}:
                if any(
                    keyword.arg == "setsid" and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
                    for keyword in node.keywords
                ):
                    violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
            if node.func.attr in {"setenv", "setitem", "setdefault", "__setitem__"}:
                if any(isinstance(argument, ast.Constant) and argument.value == "SHERPA_USE_FIXTURES" for argument in node.args[:2]):
                    violations.append(PolicyViolation(relative, node.lineno, "fixture_mode_enabled"))
            if node.func.attr == "setattr" and any(
                isinstance(argument, ast.Attribute) and argument.attr in {"urlopen", "open", "request"} for argument in node.args[:2]
            ):
                violations.append(PolicyViolation(relative, node.lineno, "network_monkeypatch"))
            if node.func.attr == "import_module" and node.args and isinstance(node.args[0], ast.Constant):
                imported = str(node.args[0].value)
                if imported in _FORBIDDEN_IMPORTS or imported.startswith("unittest." + "mock"):
                    violations.append(PolicyViolation(relative, node.lineno, "test_double_import"))
                if imported == "tests" or imported.startswith("tests.") or imported == "conftest":
                    violations.append(PolicyViolation(relative, node.lineno, "existing_test_import"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_DYNAMIC_CALLS:
                violations.append(PolicyViolation(relative, node.lineno, "dynamic_code_loading"))
            if node.func.id == "getattr" and len(node.args) >= 2:
                attribute = _constant_string(node.args[1], bindings)
                if attribute in _NETWORK_INTERCEPTION_METHODS:
                    violations.append(PolicyViolation(relative, node.lineno, "network_interception"))
                elif process_escape_restricted and attribute == "setsid":
                    violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
                elif attribute is None:
                    target = node.args[0]
                    allowed_signal_lookup = isinstance(target, ast.Name) and target.id == "signal"
                    if not allowed_signal_lookup:
                        violations.append(PolicyViolation(relative, node.lineno, "dynamic_attribute_lookup"))
            if process_escape_restricted and node.func.id == "setsid":
                violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "SHERPA_USE_FIXTURES"
                ):
                    violations.append(PolicyViolation(relative, node.lineno, "fixture_mode_enabled"))
        elif isinstance(node, ast.Dict):
            if any(isinstance(key, ast.Constant) and key.value == "SHERPA_USE_FIXTURES" for key in node.keys):
                violations.append(PolicyViolation(relative, node.lineno, "fixture_mode_enabled"))
            if process_escape_restricted and any(
                isinstance(key, ast.Constant) and key.value in {"start_new_session", "preexec_fn", "setsid"} for key in node.keys
            ):
                violations.append(PolicyViolation(relative, node.lineno, "pytest_supervision_escape"))
    return violations


def scan_source_policy(root: Path) -> list[PolicyViolation]:
    """実行ソースだけを検査し、説明文や生成artifactは検査対象から外す。"""
    violations: list[PolicyViolation] = []
    ignored_parts = {"artifacts", "baselines", "__pycache__", ".pytest_cache"}
    assert_no_mount_targets(root)
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            if not ignored_parts.intersection(path.parts):
                violations.append(PolicyViolation(str(path.relative_to(root)), 0, "source_symlink"))
            continue
        if not path.is_file() or ignored_parts.intersection(path.parts):
            continue
        if path.suffix == ".py":
            violations.extend(_python_violations(path, root))
        elif path.suffix in {".html", ".js", ".mjs", ".cjs", ".ts"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), 1):
                if any(pattern.search(line) for pattern in _JS_FORBIDDEN):
                    violations.append(PolicyViolation(str(path.relative_to(root)), line_number, "network_interception"))
    unique = {(item.path, item.line, item.rule): item for item in violations}
    return sorted(unique.values(), key=lambda item: (item.path, item.line, item.rule))


def validate_forbidden_tokens(raw_tokens: object) -> list[str]:
    """capability台帳とAST検査規則が同じ禁止境界を宣言していることを確認する。"""
    if not isinstance(raw_tokens, list):
        return ["source policy forbidden_tokens must be a list"]
    configured = {str(item) for item in raw_tokens}
    required = set(_POLICY_TOKEN_RULES)
    missing = sorted(required - configured)
    unsupported = sorted(configured - required)
    errors: list[str] = []
    if missing:
        errors.append("source policy token contract is missing: " + ", ".join(missing))
    if unsupported:
        errors.append("source policy token contract is unsupported: " + ", ".join(unsupported))
    return errors
