"""sherpa/required_tools.py: 検出はプロセス/DB 境界だけ差し替えて、導入有無の写し方を確認する。"""
from __future__ import annotations

from sherpa import required_tools
from sherpa.ingest.arms import legacy_convert
from sherpa.providers.codex import sandbox


def _patch(monkeypatch, *, soffice, chrome, marp, ocr_available):
    monkeypatch.setattr(legacy_convert, "soffice_available", lambda: soffice)
    monkeypatch.setattr(legacy_convert, "soffice_version", lambda: "LibreOffice 7.6")
    monkeypatch.setattr(legacy_convert, "office_com_available", lambda: False)
    monkeypatch.setattr(legacy_convert, "office_com_mode", lambda: "unavailable")
    monkeypatch.setattr(sandbox, "_detect_chrome_path", lambda: "/x/chrome" if chrome else None)
    monkeypatch.setattr(sandbox, "_marp_bin", lambda: "/x/marp" if marp else None)
    monkeypatch.setattr(required_tools.shutil, "which", lambda n: None)
    monkeypatch.setattr(required_tools, "_local_tool", lambda *a: None)
    from sherpa.store import ocr_jobs
    monkeypatch.setattr(ocr_jobs, "worker_availability_summary",
                        lambda h: {"available": ocr_available,
                                   "unavailable_reason": None if ocr_available else "worker_not_seen"})


def test_snapshot_reports_missing_and_installed(monkeypatch):
    _patch(monkeypatch, soffice=False, chrome=True, marp=True, ocr_available=False)
    rows = {r["id"]: r for r in required_tools.snapshot(force=True)}
    assert rows["libreoffice"]["installed"] is False and rows["libreoffice"]["version"] is None
    assert "apt-get" in rows["libreoffice"]["how_to_install"]
    assert rows["chromium"]["installed"] is True
    assert rows["ocr_worker"]["installed"] is False and "worker_not_seen" in rows["ocr_worker"]["detail"]
    assert rows["codex"]["installed"] is False and rows["ripgrep"]["installed"] is False

    _patch(monkeypatch, soffice=True, chrome=False, marp=False, ocr_available=True)
    rows = {r["id"]: r for r in required_tools.snapshot(force=True)}
    assert rows["libreoffice"]["installed"] is True and rows["libreoffice"]["version"] == "LibreOffice 7.6"
    assert rows["chromium"]["installed"] is False and rows["marp"]["installed"] is False
    assert rows["ocr_worker"]["installed"] is True


def test_snapshot_is_cached_until_forced(monkeypatch):
    _patch(monkeypatch, soffice=True, chrome=True, marp=True, ocr_available=True)
    required_tools.snapshot(force=True)
    monkeypatch.setattr(legacy_convert, "soffice_available", lambda: False)
    assert {r["id"]: r for r in required_tools.snapshot()}["libreoffice"]["installed"] is True
    assert {r["id"]: r for r in required_tools.snapshot(force=True)}["libreoffice"]["installed"] is False


def test_office_com_installed_only_when_worker_reachable(monkeypatch):
    """PowerShell があるだけ（direct モードだが Office に届かない）は「入っています」にしない。"""
    _patch(monkeypatch, soffice=True, chrome=True, marp=True, ocr_available=True)
    monkeypatch.setattr(legacy_convert, "office_com_mode", lambda: "direct")
    rows = {r["id"]: r for r in required_tools.snapshot(force=True)}
    assert rows["office_com"]["installed"] is False
    monkeypatch.setattr(legacy_convert, "office_com_available", lambda: True)
    rows = {r["id"]: r for r in required_tools.snapshot(force=True)}
    assert rows["office_com"]["installed"] is True


def test_missing_codex_cli_is_not_selectable_and_not_auto_default(monkeypatch):
    """codex 本体が無ければ構成一覧から外れ・自動選択も codex にならない。あれば従来どおり。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(required_tools, "codex_cli_missing", lambda: True)
    monkeypatch.setattr(agent_constructs.shutil, "which", lambda n: "/x/codex")
    monkeypatch.setattr(agent_constructs, "_codex_auth_available", lambda s=None: True)
    ids = {c["id"] for c in agent_constructs.available_constructs({})}
    assert "codex_openai" not in ids and "codex_ollama" not in ids
    assert agent_constructs._auto_default_agent({}) != "codex"

    monkeypatch.setattr(required_tools, "codex_cli_missing", lambda: False)
    ids = {c["id"] for c in agent_constructs.available_constructs({})}
    assert {"codex_openai", "codex_ollama"} <= ids
    assert agent_constructs._auto_default_agent({}) == "codex"


def _fake_npm_codex(tmp_path):
    """npm 版の配置: bin/codex.js ＋ node_modules/@openai/codex-x/vendor/<triple>/{codex-path/rg,codex-resources/bwrap}。"""
    pkg = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    (pkg / "bin").mkdir(parents=True)
    js = pkg / "bin" / "codex.js"
    js.write_text("#!/usr/bin/env node\n")
    js.chmod(0o755)
    triple = required_tools._NPM_TRIPLES[(required_tools.platform.system(), required_tools.platform.machine())]
    vendor = pkg / "node_modules" / "@openai" / "codex-linux-x64" / "vendor" / triple
    for rel in ("codex-path/rg", "codex-resources/bwrap"):
        f = vendor / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")
        f.chmod(0o755)
    link = tmp_path / "bin" / "codex"
    link.parent.mkdir()
    link.symlink_to(js)
    return str(link)


def test_rg_and_bwrap_found_in_npm_vendor_dir(monkeypatch, tmp_path):
    """PATH に rg/bwrap が無くても、npm 版 codex の vendor 配下にあれば「入っています」。"""
    import pytest
    if (required_tools.platform.system(), required_tools.platform.machine()) not in required_tools._NPM_TRIPLES:
        pytest.skip("対象外のプラットフォーム")
    _patch(monkeypatch, soffice=True, chrome=True, marp=True, ocr_available=True)
    link = _fake_npm_codex(tmp_path)
    monkeypatch.setattr(required_tools, "_is_linux", lambda: True)
    monkeypatch.setattr(required_tools.shutil, "which", lambda n: link if n == "codex" else None)
    monkeypatch.setattr(required_tools, "_first_line", lambda cmd: "codex-cli 0.0")
    rows = {r["id"]: r for r in required_tools.snapshot(force=True)}
    assert rows["codex"]["installed"] and rows["ripgrep"]["installed"] and rows["bwrap"]["installed"]
    assert required_tools.codex_cli_missing() is False


def test_missing_rg_bwrap_is_warning_only(monkeypatch, tmp_path):
    """codex 本体はあるが rg/bwrap がどこにも無い: 行に警告は出るが、Codex 構成は外れず保存も拒否しない。"""
    from sherpa import agent_constructs
    _patch(monkeypatch, soffice=True, chrome=True, marp=True, ocr_available=True)
    exe = tmp_path / "bin" / "codex"
    exe.parent.mkdir()
    exe.write_text("")
    exe.chmod(0o755)
    monkeypatch.setattr(required_tools, "_is_linux", lambda: True)
    monkeypatch.setattr(required_tools, "_ROOT", tmp_path)
    monkeypatch.setattr(required_tools.shutil, "which", lambda n: str(exe) if n == "codex" else None)
    monkeypatch.setattr(required_tools, "_first_line", lambda cmd: "codex-cli 0.0")
    rows = {r["id"]: r for r in required_tools.snapshot(force=True)}
    assert rows["ripgrep"]["installed"] is False and "部品が見つかりません" in rows["ripgrep"]["detail"]
    assert rows["bwrap"]["installed"] is False
    assert required_tools.codex_cli_missing_message() is None
    ids = {c["id"] for c in agent_constructs.available_constructs({})}
    assert {"codex_openai", "codex_ollama"} <= ids
