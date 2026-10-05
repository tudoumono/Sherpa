"""`scripts/gate_slice.py::select_tests`（変更ファイル→該当テストの自動選択）の契約テスト。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import scripts.gate_slice as gate_slice
from scripts.gate_slice import plan_pytest_commands, select_tests


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git_run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _select(changed, root: Path, *, deleted=None, msg="通常のコミット", **kw):
    return select_tests(changed, deleted_test_files=deleted or [], head_commit_message=msg, repo_root=root, **kw)


def _make(root: Path, files):
    for f in files:
        path, content = (f, "") if isinstance(f, str) else f
        _touch(root / path, content)


# 対応表の規則と import 逆引き（昇格せず、期待のテストが選ばれる）
@pytest.mark.parametrize("files,changed,expected", [
    (["tests/unit/test_worlds.py", "tests/api/test_worlds_api.py"], ["sherpa/worlds.py"],
     ["tests/unit/test_worlds.py", "tests/api/test_worlds_api.py"]),
    (["tests/unit/test_world_graph.py"], ["sherpa/ingest/world_graph.py"], ["tests/unit/test_world_graph.py"]),
    (["tests/unit/test_codex_mcp.py", "tests/unit/test_agents_seams.py"], ["sherpa/providers/codex/mcp.py"],
     ["tests/unit/test_codex_mcp.py", "tests/unit/test_agents_seams.py"]),
    (["tests/api/test_chat_api.py", "tests/contract/test_mirror_contract.py"], ["sherpa/routers/chat.py"],
     ["tests/api/test_chat_api.py", "tests/contract"]),
    (["tests/e2e/test_settings_ui.py"], ["web/settings.js"], ["tests/e2e/test_settings_ui.py"]),
    (["tests/e2e/test_chat_ui.py"], ["web/chat/render.js"], ["tests/e2e/test_chat_ui.py"]),
    (["tests/unit/test_something.py"], ["tests/unit/test_something.py"], ["tests/unit/test_something.py"]),
    (["tests/unit/test_docs_gates.py"], ["docs/09-実装の現在地.md", "CLAUDE.md"], ["tests/unit/test_docs_gates.py"]),
    ([("tests/unit/test_uses_helper.py", "import sherpa.foo.bar\n")], ["sherpa/foo/bar.py"],
     ["tests/unit/test_uses_helper.py"]),
    ([("tests/unit/test_uses_helper2.py", "from sherpa.ingest import world_graph\n")], ["sherpa/ingest/world_graph.py"],
     ["tests/unit/test_uses_helper2.py"]),
    ([("tests/unit/test_uses_multiline.py", "from sherpa.foo import (\n    bar,\n    baz,\n)\n")], ["sherpa/foo/bar.py"],
     ["tests/unit/test_uses_multiline.py"]),   # 複数行の括弧付き import も AST で拾う
    (["tests/api/test_x.py"], ["tests/api/conftest.py"], ["tests/api"]),
    (["tests/unit/test_x.py"], ["tests/unit/goldens/some_fixture.json"], ["tests/unit"]),
    (["tests/api/test_x.py", "tests/api/_common.py"], ["tests/api/_common.py"], ["tests/api"]),
])
def test_selection_rules(tmp_path, files, changed, expected):
    _make(tmp_path, files)
    result = _select(changed, tmp_path)
    assert not result.escalated
    assert set(expected) <= set(result.selected)


# 昇格（全件へ広げる）条件
@pytest.mark.parametrize("changed,reason", [
    (["scripts/some_new_tool.py"], None),
    (["sherpa/providers/__init__.py"], None),
    (["sherpa/ingest/analyzers/registry.py"], None),
    (["sherpa/ingest/office_md.py"], None),
    (["sherpa/routers/system.py"], None),
    (["sherpa/routers/system_extras.py"], None),
    (["sherpa/store/settings.py"], "sherpa/store/settings.py"),
    ([".env.example"], None), (["pyproject.toml"], None), (["Makefile"], None), (["requirements-dev.txt"], None),
    (["tests/conftest.py"], "共通フィクスチャ"),
    (["tests/_world_setup.py"], "共通フィクスチャ"),
    (["sherpa/worlds.py"], None),   # 対応表に一致しても対応する実テストが無ければ昇格
])
def test_escalation_conditions(tmp_path, changed, reason):
    result = _select(changed, tmp_path)
    assert result.escalated
    if reason:
        assert any(reason in r for r in result.escalation_reasons)
    if changed == ["scripts/some_new_tool.py"]:
        assert set(result.selected) == {"tests/unit", "tests/contract"} and result.areas == {"e2e", "integration", "api"}


def test_escalation_keeps_selection_and_is_decided_per_file(tmp_path):
    _make(tmp_path, ["tests/api/test_chat_api.py", "tests/contract/test_mirror_contract.py", "tests/unit/test_bar.py"])
    result = _select(["sherpa/routers/chat.py", "scripts/unknown_tool.py"], tmp_path)
    assert result.escalated and {"tests/api/test_chat_api.py", "tests/contract", "tests/unit"} <= set(result.selected)
    # 別ファイルの選択成功が、実テストの無いファイルの昇格を隠さない
    result = _select(["sherpa/foo/bar.py", "sherpa/worlds.py"], tmp_path)
    assert result.escalated and any("sherpa/worlds.py" in r for r in result.escalation_reasons)
    assert "tests/unit/test_bar.py" in result.selected
    # router 固有のテストが無ければ、無条件で足す contract 候補があっても昇格する（selected には残す）
    result = _select(["sherpa/routers/documents.py"], tmp_path)
    assert result.escalated and any("sherpa/routers/documents.py" in r for r in result.escalation_reasons)
    assert "tests/contract" in result.selected


def test_areas_and_structural_candidates(tmp_path):
    _make(tmp_path, ["tests/e2e/test_home_ui.py", "tests/api/test_worlds_api.py",
                     "tests/unit/test_world_admin_service.py", "tests/unit/test_scope.py"])
    result = _select(["web/home.js", "sherpa/ingest/world_graph.py", "sherpa/routers/worlds.py"], tmp_path)
    assert {"e2e", "integration", "api"} <= result.areas
    result = _select(["sherpa/world_admin_service.py", "sherpa/scope.py"], tmp_path)
    assert not result.escalated and "integration" in result.areas
    matched, candidates = gate_slice._structural_candidates("tests/api/conftest.py")
    assert matched and ("self", "tests/api/conftest.py") not in candidates and ("dir", "api") in candidates


def test_import_scan_skips_syntax_error_file_and_reports_it(tmp_path):
    _touch(tmp_path / "tests" / "unit" / "test_broken.py", "def test_x(:\n    pass\n")
    result = _select(["sherpa/foo/bar.py"], tmp_path)
    assert "tests/unit/test_broken.py" in result.import_scan_syntax_errors
    assert "tests/unit/test_broken.py" not in result.selected


def test_import_lookup_hit_on_shared_helper_expands_to_suite_not_bare_file(tmp_path):
    """逆引きが tests/_*.py（共通ヘルパ）に当たったら単独で渡さず全件へ広げる（収集 0 件で落ちるため）。"""
    _touch(tmp_path / "sherpa" / "es_index.py", "X = 1\n")
    _touch(tmp_path / "tests" / "_world_registry.py", "from sherpa import es_index\n")
    _touch(tmp_path / "tests" / "unit" / "test_other.py", "def test_a():\n    pass\n")
    (tmp_path / "tests" / "contract").mkdir()
    result = select_tests(["sherpa/es_index.py"], head_commit_message="", deleted_test_files=[], repo_root=tmp_path)
    assert "tests/_world_registry.py" not in result.selected
    assert {"tests/unit", "tests/contract"} <= set(result.selected)


# テスト削除の退役判定（削除コミット本文に退役語があること・未コミット削除は免除されない）
_OLD = "tests/unit/test_old_thing.py"


@pytest.mark.parametrize("kw,violation,escalated", [
    (dict(deleted=[_OLD], msg="旧テストを削除"), True, True),
    (dict(deleted=[_OLD], msg="旧テストを退役"), False, True),
    (dict(deleted=[_OLD], msg="旧テストを撤去"), False, True),
    (dict(deleted=["scripts/not_a_test.py"], msg="コミット本文に理由なし"), False, None),   # tests 外は対象外
    (dict(deleted=[_OLD], msg="通常のコミット（退役語なし）", bodies={_OLD: "旧テストを撤去"}), False, None),
    (dict(deleted=[_OLD], msg="旧テストを退役", bodies={_OLD: "整理のみ"}), True, None),   # HEAD 本文へ横流れさせない
    (dict(uncommitted=["tests/unit/test_wip.py"], msg="退役 理由あり"), True, True),
])
def test_deleted_tests_retirement_rule(tmp_path, kw, violation, escalated):
    extra = {}
    if "bodies" in kw:
        extra["deleted_test_commit_bodies"] = kw["bodies"]
    if "uncommitted" in kw:
        extra["uncommitted_deleted_test_files"] = kw["uncommitted"]
    result = _select(["sherpa/worlds.py"], tmp_path, deleted=kw.get("deleted"), msg=kw["msg"], **extra)
    assert result.retirement_violation == violation
    assert bool(result.retirement_reason) == violation
    if escalated:
        assert result.escalated and any("削除" in r for r in result.escalation_reasons)
    if kw.get("bodies") and violation:
        assert _OLD in result.retirement_reason
    if kw.get("uncommitted"):
        assert "未コミット" in result.retirement_reason


def test_deleted_commit_body_excludes_bodies_of_non_deleting_commits(tmp_path):
    repo = tmp_path
    _git_run(repo, "init", "-q", "-b", "main")
    _git_run(repo, "config", "user.email", "test@example.com")
    _git_run(repo, "config", "user.name", "Test")
    _touch(repo / "README.md", "base\n")
    _touch(repo / _OLD, "def test_x():\n    pass\n")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "initial")
    _git_run(repo, "checkout", "-q", "-b", "feature")
    _touch(repo / _OLD, "def test_x():\n    pass\n\n# 変更\n")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "退役概念の回帰テスト追加")   # 追加側の本文に退役語があっても数えない
    (repo / _OLD).unlink()
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "テスト整理")
    changed, deleted, bodies, uncommitted, head_msg = gate_slice._gather_git_state(repo, "main")
    assert deleted == [_OLD] and "退役" not in bodies[_OLD] and "整理" in bodies[_OLD]
    result = select_tests(changed, deleted_test_files=deleted, deleted_test_commit_bodies=bodies,
                          uncommitted_deleted_test_files=uncommitted, head_commit_message=head_msg, repo_root=repo)
    assert result.retirement_violation and _OLD in (result.retirement_reason or "")


@pytest.mark.parametrize("line,changed,deleted", [
    ("RD tests/unit/test_old.py -> tests/unit/test_new.py", ["tests/unit/test_new.py", "tests/unit/test_old.py"],
     ["tests/unit/test_new.py"]),
    ("R  tests/unit/test_old.py -> tests/unit/test_new.py", ["tests/unit/test_new.py", "tests/unit/test_old.py"], []),
    (" D tests/unit/test_old.py", ["tests/unit/test_old.py"], ["tests/unit/test_old.py"]),
])
def test_parse_porcelain_status_line(line, changed, deleted):
    assert gate_slice._parse_porcelain_status_line(line) == (changed, deleted)


def test_git_helper_passes_core_quote_path_false(monkeypatch, tmp_path):
    captured = {}

    def fake_check_output(cmd, cwd, text):
        captured.update(cmd=cmd, cwd=cwd)
        return ""
    monkeypatch.setattr(gate_slice.subprocess, "check_output", fake_check_output)
    gate_slice._git(tmp_path, "status", "--porcelain")
    assert captured["cmd"][:3] == ["git", "-c", "core.quotePath=false"] and captured["cwd"] == tmp_path


def test_gate_budget_shows_result_line_and_nonzero_exit_when_pytest_fails(tmp_path):
    fake_py = tmp_path / "fake_python"
    fake_py.write_text("#!/usr/bin/env bash\nexit 2\n")
    fake_py.chmod(0o755)
    gate_budget_sh = Path(gate_slice.__file__).resolve().parent / "lib" / "gate_budget.sh"
    r = subprocess.run(["bash", "-c", f"set -euo pipefail\n. {gate_budget_sh}\ngate_run_unit_contract_budgeted '{fake_py}'\n"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "単体+契約:" in r.stdout and "pytest 終了コード 2" in r.stdout


def test_plan_pytest_commands():
    py = [sys.executable, "-m", "pytest"]
    # 同名テストは別スイートごとに別プロセスへ分ける（ImportPathMismatchError の回避）・unit が先
    assert plan_pytest_commands(["tests/api/test_chat_turns.py", "tests/unit/test_chat_turns.py"]) == [
        [*py, "tests/unit/test_chat_turns.py", "-q"], [*py, "tests/api/test_chat_turns.py", "-q"]]
    assert plan_pytest_commands(["tests/unit/test_a.py", "tests/unit/test_b.py"]) == [
        [*py, "tests/unit/test_a.py", "tests/unit/test_b.py", "-q"]]
    assert plan_pytest_commands(["tests/unit", "tests/contract"]) == [[*py, "tests/unit", "-q"], [*py, "tests/contract", "-q"]]
    assert plan_pytest_commands([]) == []
    cmds = plan_pytest_commands(["tests/e2e/test_z.py", "tests/integration/test_y.py", "tests/api/test_x.py",
                                 "tests/contract/test_w.py", "tests/unit/test_v.py"])
    assert [c[3].split("/")[1] for c in cmds] == ["unit", "contract", "api", "integration", "e2e"]
    # 既知の順に無いスイートも落とさない
    joined = [" ".join(c) for c in plan_pytest_commands(["tests/e2e_live/test_live_flows.py", "tests/unit/test_x.py"])]
    assert "tests/unit/" in joined[0] and "tests/e2e_live/" in joined[-1]
