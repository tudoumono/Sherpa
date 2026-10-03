"""運用スクリプトが macOS 標準の bash 3.2 で動く書き方に留まっていることの契約。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

# Linux（Ubuntu/WSL2）専用のスクリプト。macOS では使わない（apt・dpkg・useradd 等を使う）。
LINUX_ONLY = frozenset({
    "install_offline_kit.sh", "make_offline_kit.sh", "verify_offline_kit_apt.sh",
    "setup-runtime-users.sh",
})

# bash 4 以降でしか動かない構文（名前・正規表現）。
BASH4_PATTERNS = (
    ("連想配列（declare/local/typeset -A）", re.compile(r"\b(?:declare|local|typeset)\s+-[a-zA-Z]*A")),
    ("名前参照（declare/local -n）", re.compile(r"\b(?:declare|local|typeset)\s+-[a-zA-Z]*n\b")),
    ("mapfile / readarray", re.compile(r"\b(?:mapfile|readarray)\b")),
    ("大文字小文字変換（${v,,} ${v^^} など）", re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?(?:,,?|\^\^?)\}")),
    ("${v@Q} などの変換", re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*@[QEPAa]\}")),
    ("wait -n", re.compile(r"\bwait\s+-n\b")),
    ("coproc", re.compile(r"\bcoproc\b")),
    ("|& / &>>", re.compile(r"\|&|&>>")),
    ("case の ;& / ;;&", re.compile(r";;&|(?<!;);&")),
    ("負の配列添字（${a[-1]}）", re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\[-[0-9]+\]\}")),
    ("globstar", re.compile(r"shopt\s+-s\s+globstar")),
    ("fd の自動割当（exec {fd}>）", re.compile(r"(?<!\$)\{[A-Za-z_][A-Za-z0-9_]*\}[<>]")),
)


def _targets() -> list[Path]:
    return sorted(p for p in SCRIPTS.rglob("*.sh") if p.name not in LINUX_ONLY)


def _code_lines(path: Path):
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield no, line


def test_scripts_avoid_bash4_only_syntax():
    found = []
    for path in _targets():
        for no, line in _code_lines(path):
            for label, pat in BASH4_PATTERNS:
                if pat.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{no}: {label}: {line.strip()}")
    assert not found, "macOS の bash 3.2 で動かない構文があります:\n" + "\n".join(found)


def test_linux_only_list_names_existing_scripts():
    names = {p.name for p in SCRIPTS.rglob("*.sh")}
    missing = sorted(LINUX_ONLY - names)
    assert not missing, f"対象外の一覧に無いスクリプトがあります: {missing}"


# `$VAR` の直後に全角文字などの非 ASCII が続くと、macOS の bash はロケールによってその先頭バイトを
# 変数名に取り込み、`set -u` で止まる（Linux では起きない）。`${VAR}` と囲めば起きない。Linux 専用も含めて全部見る。
_VAR_BEFORE_NON_ASCII = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]")


def test_scripts_brace_variables_before_non_ascii():
    found = [f"{path.relative_to(ROOT)}:{no}: {line.strip()}"
             for path in sorted(SCRIPTS.rglob("*.sh"))
             for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
             if _VAR_BEFORE_NON_ASCII.search(line)]
    assert not found, "変数の直後に非 ASCII の文字があります（${VAR} と囲んでください）:\n" + "\n".join(found)

