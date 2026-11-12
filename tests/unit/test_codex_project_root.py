"""Codex の作業領域そのものを根にする設定（上の階層の `.git`・AGENTS.md を探しに行かない）。"""
from __future__ import annotations

import shutil
import tomllib

import pytest

from sherpa.providers.codex import sandbox as SB


@pytest.fixture(autouse=True)
def _codex_cli_present(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)


def test_workspace_itself_is_the_project_root(tmp_path):
    """作業領域が git の中にあっても、Codex は上の階層の `.git`・AGENTS.md を探しに行かない（macOS では読めずに起動が止まり、開発者向けの AGENTS.md も混ざり得る）。"""
    ch = tmp_path / "ch"
    SB._write_codex_authoring_config(ch, ["/kb"], "low", True, "test", None)
    assert tomllib.loads((ch / "config.toml").read_text())["project_root_markers"] == []
