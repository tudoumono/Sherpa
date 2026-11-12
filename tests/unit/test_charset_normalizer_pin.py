"""入っている charset-normalizer の版が constraints.txt の固定版と一致することを確かめる。
判定（外字・半角カナ等）は版で変わるため、版を上げるときは test_text_encoding.py・test_text_encoding_euc.py を通すこと。"""
from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _pinned(name: str, text: str) -> str | None:
    m = re.search(rf"^{re.escape(name)}==([^\s#]+)", text, re.M)
    return m.group(1) if m else None


def test_installed_charset_normalizer_matches_constraints_pin():
    pinned = _pinned("charset-normalizer", (_ROOT / "constraints.txt").read_text(encoding="utf-8"))
    assert pinned is not None
    assert metadata.version("charset-normalizer") == pinned


def test_requirements_do_not_allow_older_than_pinned_version():
    pinned = _pinned("charset-normalizer", (_ROOT / "constraints.txt").read_text(encoding="utf-8"))
    m = re.search(r"^charset-normalizer>=([^\s,<#]+)", (_ROOT / "requirements.txt").read_text(encoding="utf-8"), re.M)
    assert m and m.group(1) == pinned
