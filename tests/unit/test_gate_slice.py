"""`scripts/gate_slice.py::select_tests`（変更ファイル→該当テストの自動選択）の契約テスト。

選択規則の正典は docs/20-開発ハーネス.md §5。大半は `select_tests` を純粋関数として直接呼び、
git は一切叩かない（tmp_path 上に疑似 `tests/` ツリーを作り、`repo_root`/`tests_root` を差し替える）。
例外は `_gather_git_state`（削除パスごとの削除コミット本文の収集）を対象にした一群で、実際の
git リポジトリを tmp_path に作って検証する。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import scripts.gate_slice as gate_slice
from scripts.gate_slice import plan_pytest_commands, select_tests


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git_run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _select(changed, root: Path, *, deleted=None, msg="通常のコミット"):
    return select_tests(
        changed,
        deleted_test_files=deleted or [],
        head_commit_message=msg,
        repo_root=root,
    )


# --- (a) 対応表: 6パターン ------------------------------------------------------------------


def test_rule_top_level_module(tmp_path):
    _touch(tmp_path / "tests" / "unit" / "test_worlds.py")
    _touch(tmp_path / "tests" / "api" / "test_worlds_api.py")
    result = _select(["sherpa/worlds.py"], tmp_path)
    assert not result.escalated
    assert "tests/unit/test_worlds.py" in result.selected
    assert "tests/api/test_worlds_api.py" in result.selected


def test_rule_subpackage_module(tmp_path):
    _touch(tmp_path / "tests" / "unit" / "test_world_graph.py")
    result = _select(["sherpa/ingest/world_graph.py"], tmp_path)
    assert not result.escalated
    assert "tests/unit/test_world_graph.py" in result.selected


def test_rule_providers_codex(tmp_path):
    _touch(tmp_path / "tests" / "unit" / "test_codex_mcp.py")
    _touch(tmp_path / "tests" / "unit" / "test_agents_seams.py")
    result = _select(["sherpa/providers/codex/mcp.py"], tmp_path)
    assert not result.escalated
    assert "tests/unit/test_codex_mcp.py" in result.selected
    assert "tests/unit/test_agents_seams.py" in result.selected


def test_rule_routers(tmp_path):
    _touch(tmp_path / "tests" / "api" / "test_chat_api.py")
    _touch(tmp_path / "tests" / "contract" / "test_mirror_contract.py")
    result = _select(["sherpa/routers/chat.py"], tmp_path)
    assert not result.escalated
    assert "tests/api/test_chat_api.py" in result.selected
    assert "tests/contract" in result.selected


def test_rule_web_page(tmp_path):
    _touch(tmp_path / "tests" / "e2e" / "test_settings_ui.py")
    result = _select(["web/settings.js"], tmp_path)
    assert not result.escalated
    assert "tests/e2e/test_settings_ui.py" in result.selected


def test_rule_web_chat_always_test_chat_prefix(tmp_path):
    _touch(tmp_path / "tests" / "e2e" / "test_chat_ui.py")
    result = _select(["web/chat/render.js"], tmp_path)
    assert not result.escalated
    assert "tests/e2e/test_chat_ui.py" in result.selected


def test_rule_tests_self_select(tmp_path):
    _touch(tmp_path / "tests" / "unit" / "test_something.py")
    result = _select(["tests/unit/test_something.py"], tmp_path)
    assert not result.escalated
    assert "tests/unit/test_something.py" in result.selected


def test_rule_docs_any_md(tmp_path):
    _touch(tmp_path / "tests" / "unit" / "test_docs_gates.py")
    result = _select(["docs/09-実装の現在地.md"], tmp_path)
    assert not result.escalated
    assert "tests/unit/test_docs_gates.py" in result.selected

    result2 = _select(["CLAUDE.md"], tmp_path)
    assert not result2.escalated
    assert "tests/unit/test_docs_gates.py" in result2.selected


# --- (b) import 逆引き -----------------------------------------------------------------------


def test_import_reverse_lookup(tmp_path):
    _touch(
        tmp_path / "tests" / "unit" / "test_uses_helper.py",
        "import sherpa.foo.bar\n\n\ndef test_x():\n    pass\n",
    )
    result = _select(["sherpa/foo/bar.py"], tmp_path)
    assert not result.escalated
    assert "tests/unit/test_uses_helper.py" in result.selected


def test_import_reverse_lookup_from_import_of_module_itself(tmp_path):
    _touch(
        tmp_path / "tests" / "unit" / "test_uses_helper2.py",
        "from sherpa.ingest import world_graph\n",
    )
    result = _select(["sherpa/ingest/world_graph.py"], tmp_path)
    assert not result.escalated
    assert "tests/unit/test_uses_helper2.py" in result.selected


# --- (c) 昇格 4条件 ---------------------------------------------------------------------------


def test_escalate_unknown_location(tmp_path):
    result = _select(["scripts/some_new_tool.py"], tmp_path)
    assert result.escalated
    assert set(result.selected) == {"tests/unit", "tests/contract"}
    assert result.areas == {"e2e", "integration", "api"}


def test_escalate_registry_or_init(tmp_path):
    result = _select(["sherpa/providers/__init__.py"], tmp_path)
    assert result.escalated

    result2 = _select(["sherpa/ingest/analyzers/registry.py"], tmp_path)
    assert result2.escalated


def test_escalate_config_key_file(tmp_path):
    result = _select(["sherpa/ingest/office_md.py"], tmp_path)
    assert result.escalated

    result2 = _select(["sherpa/routers/system.py"], tmp_path)
    assert result2.escalated

    result3 = _select(["sherpa/routers/system_extras.py"], tmp_path)
    assert result3.escalated


def test_escalate_infra_files(tmp_path):
    for f in (".env.example", "pyproject.toml", "Makefile", "requirements-dev.txt"):
        result = _select([f], tmp_path)
        assert result.escalated, f


def test_escalate_zero_selected_despite_structural_match(tmp_path):
    # sherpa/worlds.py は対応表に構造的に一致するが、対応する実テストファイルが無い。
    result = _select(["sherpa/worlds.py"], tmp_path)
    assert result.escalated
    assert gate_slice._structural_candidates("sherpa/worlds.py")[1] != []


# --- テスト削除の退役判定 ----------------------------------------------------------------------


def test_deleted_tests_without_retirement_word_fails(tmp_path):
    result = _select(
        ["sherpa/worlds.py"],
        tmp_path,
        deleted=["tests/unit/test_old_thing.py"],
        msg="旧テストを削除",
    )
    assert result.retirement_violation
    assert result.retirement_reason


def test_deleted_tests_with_retirement_word_ok():
    for word in ("退役", "撤去"):
        result = select_tests(
            ["sherpa/worlds.py"],
            deleted_test_files=["tests/unit/test_old_thing.py"],
            head_commit_message=f"旧テストを{word}",
            repo_root=Path("/nonexistent-repo-root-for-this-check"),
        )
        assert not result.retirement_violation


def test_deleted_tests_outside_tests_dir_ignored(tmp_path):
    result = _select(
        ["sherpa/worlds.py"],
        tmp_path,
        deleted=["scripts/not_a_test.py"],
        msg="コミット本文に理由なし",
    )
    assert not result.retirement_violation


# --- areas（--areas-only の裏付け） -----------------------------------------------------------


def test_areas_from_changed_files(tmp_path):
    _touch(tmp_path / "tests" / "e2e" / "test_home_ui.py")
    _touch(tmp_path / "tests" / "api" / "test_worlds_api.py")
    result = _select(
        ["web/home.js", "sherpa/ingest/world_graph.py", "sherpa/routers/worlds.py"],
        tmp_path,
    )
    assert "e2e" in result.areas
    assert "integration" in result.areas
    assert "api" in result.areas


def test_areas_world_admin_service_and_scope_are_integration(tmp_path):
    _touch(tmp_path / "tests" / "unit" / "test_world_admin_service.py")
    _touch(tmp_path / "tests" / "unit" / "test_scope.py")
    result = _select(["sherpa/world_admin_service.py", "sherpa/scope.py"], tmp_path)
    assert not result.escalated
    assert "integration" in result.areas


# --- (1) 昇格しても選択済みを消さない ----------------------------------------------------------


def test_escalation_keeps_previously_selected_suites(tmp_path):
    _touch(tmp_path / "tests" / "api" / "test_chat_api.py")
    _touch(tmp_path / "tests" / "contract" / "test_mirror_contract.py")
    # 2つ目の変更ファイル（scripts/unknown_tool.py）は対応表にも import 逆引きにも当たらず
    # 全件昇格を引き起こす——その際に1つ目の変更ファイルが選んだスイートが消えないことを確認する。
    result = _select(["sherpa/routers/chat.py", "scripts/unknown_tool.py"], tmp_path)
    assert result.escalated
    assert "tests/api/test_chat_api.py" in result.selected
    assert "tests/contract" in result.selected
    assert "tests/unit" in result.selected


# --- (2) ファイル単位の昇格判定（別ファイルの選択結果で隠さない） -------------------------------


def test_escalate_file_level_not_masked_by_other_file_selection(tmp_path):
    _touch(tmp_path / "tests" / "unit" / "test_bar.py")
    # sherpa/foo/bar.py は実在するテストを見つけるが、sherpa/worlds.py は対応表候補はあっても
    # 対応する実テストファイルが無い——前者の成功が後者の昇格を隠さないことを確認する。
    result = _select(["sherpa/foo/bar.py", "sherpa/worlds.py"], tmp_path)
    assert result.escalated
    assert any("sherpa/worlds.py" in r for r in result.escalation_reasons)
    assert "tests/unit/test_bar.py" in result.selected


# --- (3) conftest・共通ヘルパ・goldens ---------------------------------------------------------


def test_subdir_conftest_change_selects_whole_directory(tmp_path):
    _touch(tmp_path / "tests" / "api" / "test_x.py")
    result = _select(["tests/api/conftest.py"], tmp_path)
    assert not result.escalated
    assert "tests/api" in result.selected


def test_goldens_change_selects_whole_directory(tmp_path):
    _touch(tmp_path / "tests" / "unit" / "test_x.py")
    result = _select(["tests/unit/goldens/some_fixture.json"], tmp_path)
    assert not result.escalated
    assert "tests/unit" in result.selected


def test_root_conftest_change_escalates_fully(tmp_path):
    result = _select(["tests/conftest.py"], tmp_path)
    assert result.escalated
    assert any("共通フィクスチャ" in r for r in result.escalation_reasons)


def test_top_level_test_helper_change_escalates_fully(tmp_path):
    result = _select(["tests/_world_setup.py"], tmp_path)
    assert result.escalated
    assert any("共通フィクスチャ" in r for r in result.escalation_reasons)


def test_self_select_restricted_to_test_prefixed_files():
    # conftest.py 自身は self select（自身だけを選ぶ規則）の対象にならない（test_*.py に限る）。
    matched, candidates = gate_slice._structural_candidates("tests/api/conftest.py")
    assert matched
    assert ("self", "tests/api/conftest.py") not in candidates
    assert ("dir", "api") in candidates


# --- (4)(5)(6) テスト削除の独立昇格・未コミット削除・パス別の削除コミット本文 -------------------


def test_deleted_tests_escalate_even_with_valid_retirement_reason(tmp_path):
    result = _select(
        ["sherpa/worlds.py"],
        tmp_path,
        deleted=["tests/unit/test_old_thing.py"],
        msg="旧テストを退役",
    )
    assert not result.retirement_violation
    assert result.escalated
    assert any("削除" in r for r in result.escalation_reasons)


def test_uncommitted_deleted_test_always_violates_and_escalates(tmp_path):
    result = select_tests(
        ["sherpa/worlds.py"],
        deleted_test_files=[],
        uncommitted_deleted_test_files=["tests/unit/test_wip.py"],
        head_commit_message="退役 理由あり",  # 退役語があっても未コミットは免除されない
        repo_root=tmp_path,
    )
    assert result.retirement_violation
    assert "未コミット" in result.retirement_reason
    assert result.escalated
    assert any("削除" in r for r in result.escalation_reasons)


def test_retirement_check_uses_per_path_commit_body_not_head_message(tmp_path):
    # HEAD の本文には退役語が無いが、当該パスの削除コミット本文にはある → 合格。
    result = select_tests(
        ["sherpa/worlds.py"],
        deleted_test_files=["tests/unit/test_old_thing.py"],
        deleted_test_commit_bodies={"tests/unit/test_old_thing.py": "旧テストを撤去"},
        head_commit_message="通常のコミット（退役語なし）",
        repo_root=tmp_path,
    )
    assert not result.retirement_violation


def test_retirement_check_per_path_fails_when_own_commit_body_lacks_word(tmp_path):
    # HEAD の本文には退役語があるが、当該パスの削除コミット本文には無い → 不合格
    # （削除コミットの本文と対応付ける・HEAD の本文への横流れを許さない）。
    result = select_tests(
        ["sherpa/worlds.py"],
        deleted_test_files=["tests/unit/test_old_thing.py"],
        deleted_test_commit_bodies={"tests/unit/test_old_thing.py": "整理のみ"},
        head_commit_message="旧テストを退役",
        repo_root=tmp_path,
    )
    assert result.retirement_violation
    assert result.retirement_reason
    assert "tests/unit/test_old_thing.py" in result.retirement_reason


# --- (7) import 逆引きは AST（複数行・括弧付き import も対象） ----------------------------------


def test_import_reverse_lookup_multiline_parenthesized_import(tmp_path):
    _touch(
        tmp_path / "tests" / "unit" / "test_uses_multiline.py",
        "from sherpa.foo import (\n    bar,\n    baz,\n)\n\n\ndef test_x():\n    pass\n",
    )
    result = _select(["sherpa/foo/bar.py"], tmp_path)
    assert not result.escalated
    assert "tests/unit/test_uses_multiline.py" in result.selected


def test_import_scan_skips_syntax_error_file_and_reports_it(tmp_path):
    _touch(
        tmp_path / "tests" / "unit" / "test_broken.py",
        "def test_x(:\n    pass\n",  # 構文エラー
    )
    result = _select(["sherpa/foo/bar.py"], tmp_path)
    assert "tests/unit/test_broken.py" in result.import_scan_syntax_errors
    # 構文エラーのファイルは import 逆引きの対象外なので、それだけを理由に選ばれない。
    assert "tests/unit/test_broken.py" not in result.selected


# --- (8) 設定キー定義ファイル ------------------------------------------------------------------


def test_escalate_config_key_file_settings_store(tmp_path):
    result = _select(["sherpa/store/settings.py"], tmp_path)
    assert result.escalated
    assert any("sherpa/store/settings.py" in r for r in result.escalation_reasons)


# --- (10) core.quotePath=false ------------------------------------------------------------------


def test_git_helper_passes_core_quote_path_false(monkeypatch, tmp_path):
    captured = {}

    def fake_check_output(cmd, cwd, text):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return ""

    monkeypatch.setattr(gate_slice.subprocess, "check_output", fake_check_output)
    gate_slice._git(tmp_path, "status", "--porcelain")
    assert captured["cmd"][:3] == ["git", "-c", "core.quotePath=false"]
    assert captured["cwd"] == tmp_path


# --- (14) gate_budget.sh: errexit 下でも結果行を表示してから非ゼロで終える ----------------------


def test_gate_budget_shows_result_line_and_nonzero_exit_when_pytest_fails(tmp_path):
    fake_py = tmp_path / "fake_python"
    fake_py.write_text("#!/usr/bin/env bash\nexit 2\n")
    fake_py.chmod(0o755)
    gate_budget_sh = Path(gate_slice.__file__).resolve().parent / "lib" / "gate_budget.sh"
    script = (
        "set -euo pipefail\n"
        f". {gate_budget_sh}\n"
        f"gate_run_unit_contract_budgeted '{fake_py}'\n"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "単体+契約:" in r.stdout, r.stdout + r.stderr
    assert "pytest 終了コード 2" in r.stdout, r.stdout + r.stderr


# --- (15) 退役判定は削除コミットの本文に限定（変更コミットの本文を混ぜない） ---------------------


def test_deleted_commit_body_excludes_bodies_of_non_deleting_commits(tmp_path):
    # 追加コミットの本文に「退役」語があっても、実際に削除したコミットの本文に無ければ
    # 判定は「本文に語なし」でなければならない（削除コミットに限定する契約）。
    repo = tmp_path
    _git_run(repo, "init", "-q", "-b", "main")
    _git_run(repo, "config", "user.email", "test@example.com")
    _git_run(repo, "config", "user.name", "Test")
    _touch(repo / "README.md", "base\n")
    _touch(repo / "tests" / "unit" / "test_old_thing.py", "def test_x():\n    pass\n")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "initial")

    # merge_base（main）の時点で既にファイルが存在している必要がある（差分の D 判定は
    # merge_base と HEAD のツリー比較のため、範囲内で追加→削除しても net では現れない）。
    _git_run(repo, "checkout", "-q", "-b", "feature")
    _touch(repo / "tests" / "unit" / "test_old_thing.py", "def test_x():\n    pass\n\n# 変更\n")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "退役概念の回帰テスト追加")

    (repo / "tests" / "unit" / "test_old_thing.py").unlink()
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "テスト整理")

    (
        changed_files,
        deleted_test_files,
        deleted_test_commit_bodies,
        uncommitted_deleted_test_files,
        head_commit_message,
    ) = gate_slice._gather_git_state(repo, "main")

    assert deleted_test_files == ["tests/unit/test_old_thing.py"]
    body = deleted_test_commit_bodies["tests/unit/test_old_thing.py"]
    assert "退役" not in body
    assert "整理" in body

    result = select_tests(
        changed_files,
        deleted_test_files=deleted_test_files,
        deleted_test_commit_bodies=deleted_test_commit_bodies,
        uncommitted_deleted_test_files=uncommitted_deleted_test_files,
        head_commit_message=head_commit_message,
        repo_root=repo,
    )
    assert result.retirement_violation
    assert "tests/unit/test_old_thing.py" in (result.retirement_reason or "")


# --- (16) サブディレクトリの共通ヘルパ（tests/<dir>/_*.py）はディレクトリ全件へ -------------------


def test_subdir_helper_change_selects_whole_directory(tmp_path):
    _touch(tmp_path / "tests" / "api" / "test_x.py")
    _touch(tmp_path / "tests" / "api" / "_common.py")
    result = _select(["tests/api/_common.py"], tmp_path)
    assert not result.escalated
    assert "tests/api" in result.selected


# --- (17) リネーム後に移動先が未コミットで削除された場合の porcelain 行解析 ----------------------


def test_parse_porcelain_rename_then_worktree_delete_records_new_path_as_deleted():
    changed, deleted = gate_slice._parse_porcelain_status_line(
        "RD tests/unit/test_old.py -> tests/unit/test_new.py"
    )
    assert changed == ["tests/unit/test_new.py", "tests/unit/test_old.py"]
    assert deleted == ["tests/unit/test_new.py"]


def test_parse_porcelain_plain_rename_without_worktree_delete():
    changed, deleted = gate_slice._parse_porcelain_status_line(
        "R  tests/unit/test_old.py -> tests/unit/test_new.py"
    )
    assert changed == ["tests/unit/test_new.py", "tests/unit/test_old.py"]
    assert deleted == []


def test_parse_porcelain_plain_delete():
    changed, deleted = gate_slice._parse_porcelain_status_line(" D tests/unit/test_old.py")
    assert changed == ["tests/unit/test_old.py"]
    assert deleted == ["tests/unit/test_old.py"]


# --- (18) router の無条件 contract 候補はファイル単位の当たり判定に数えない ----------------------


def test_router_without_dedicated_test_escalates_despite_area_dir_contract(tmp_path):
    # tests/contract は存在する（router 変更は無条件でここへ足す領域候補）が、
    # sherpa/routers/documents.py 固有のテスト（tests/api/test_documents*.py）が無い。
    # 領域候補をファイル単位の当たり判定に数えると、この不在が隠れて escalated=False に
    # なってしまう——それを固定する。
    _touch(tmp_path / "tests" / "contract" / "test_mirror_contract.py")
    result = _select(["sherpa/routers/documents.py"], tmp_path)
    assert result.escalated
    assert any("sherpa/routers/documents.py" in r for r in result.escalation_reasons)
    # 領域候補としては引き続き selected に contract が入る（実行対象からは消さない）。
    assert "tests/contract" in result.selected


# --- (19) plan_pytest_commands: 選択結果 → スイート別コマンド列（同名テストの衝突回避） ---------
# 異なるスイートに同名テストファイル（例: tests/api/test_chat_turns.py と
# tests/unit/test_chat_turns.py）が共存すると、1つの pytest プロセスに両方渡した時点で
# ImportPathMismatchError（収集エラー）で止まる（sherpa/chat_turns.py の変更で実際に再現した）。
# スイートごとに別プロセスへ分けることで回避する。


def test_plan_pytest_commands_splits_same_named_tests_into_different_suite_commands():
    selected = ["tests/api/test_chat_turns.py", "tests/unit/test_chat_turns.py"]
    commands = plan_pytest_commands(selected)
    assert len(commands) == 2
    # unit が先（固定順）・それぞれのコマンドは自スイートのパスだけを持つ。
    assert commands[0] == [sys.executable, "-m", "pytest", "tests/unit/test_chat_turns.py", "-q"]
    assert commands[1] == [sys.executable, "-m", "pytest", "tests/api/test_chat_turns.py", "-q"]


def test_plan_pytest_commands_no_empty_suite_commands():
    commands = plan_pytest_commands(["tests/unit/test_a.py", "tests/unit/test_b.py"])
    assert len(commands) == 1
    assert commands[0] == [
        sys.executable, "-m", "pytest", "tests/unit/test_a.py", "tests/unit/test_b.py", "-q",
    ]


def test_plan_pytest_commands_empty_selection_returns_no_commands():
    assert plan_pytest_commands([]) == []


def test_plan_pytest_commands_deterministic_order_unit_contract_api_integration_e2e():
    selected = [
        "tests/e2e/test_z.py",
        "tests/integration/test_y.py",
        "tests/api/test_x.py",
        "tests/contract/test_w.py",
        "tests/unit/test_v.py",
    ]
    commands = plan_pytest_commands(selected)
    assert len(commands) == 5
    suites_in_order = [cmd[3].split("/")[1] for cmd in commands]
    assert suites_in_order == ["unit", "contract", "api", "integration", "e2e"]


def test_plan_pytest_commands_directory_selection_grouped_by_suite():
    commands = plan_pytest_commands(["tests/unit", "tests/contract"])
    assert len(commands) == 2
    assert commands[0] == [sys.executable, "-m", "pytest", "tests/unit", "-q"]
    assert commands[1] == [sys.executable, "-m", "pytest", "tests/contract", "-q"]


def test_plan_pytest_commands_keeps_unknown_suites_like_e2e_live():
    """既知の順に無いスイート（e2e_live 等）も落とさずコマンドにする（選ばれたのに未実行で成功しない）。"""
    cmds = plan_pytest_commands(["tests/e2e_live/test_live_flows.py", "tests/unit/test_x.py"])
    joined = [" ".join(c) for c in cmds]
    assert any("tests/e2e_live/test_live_flows.py" in c for c in joined)
    assert joined[0].find("tests/unit/") >= 0 and joined[-1].find("tests/e2e_live/") >= 0


def test_import_lookup_hit_on_shared_helper_expands_to_suite_not_bare_file(tmp_path):
    """逆引きが tests/_*.py（共通ヘルパ）に当たったら、そのファイルを単独で pytest に渡さず
    全件へ広げる（ヘルパ単独は収集 0 件＝終了コード 5 で gate-slice が落ちる）。"""
    repo = tmp_path
    (repo / "sherpa").mkdir()
    (repo / "sherpa" / "es_index.py").write_text("X = 1\n", encoding="utf-8")
    tests = repo / "tests"
    (tests / "unit").mkdir(parents=True)
    (tests / "contract").mkdir()
    (tests / "_world_registry.py").write_text("from sherpa import es_index\n", encoding="utf-8")
    (tests / "unit" / "test_other.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    result = select_tests(["sherpa/es_index.py"], head_commit_message="", deleted_test_files=[],
                          repo_root=repo)
    assert "tests/_world_registry.py" not in result.selected
    assert {"tests/unit", "tests/contract"} <= set(result.selected)

