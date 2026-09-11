"""P1-b（Codex 強化計画 Phase1・自作 Office スキル3本＋配備機構）単体テスト。

§5c 決定＝案A′（ベース＋個人オーバーレイ）: authoring/.agents/skills を毎回作り直し、
base（sherpa/skills_base/）→ 個人（users/{uid}/workspace/skills/）の順で配備し、同名は個人が置換する。
symlink は一切追従しない（fail-closed）。台帳スナップショット（Codex 新規ファイル検出）は
`.agents` 配下を除外し、配備したスキルが誤って personal_workspace_files に登録されないようにする。
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import tempfile

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import codex_skills as S  # noqa: E402

# S3（提案書 2026-09-10-Codex原本直読と調査スキル §2-6）: 調査スキル5本。frontmatter の name は
# ディレクトリ名と一致（investigate-list/investigate-spec/investigate-impact/investigate-cause/
# investigate-compare）。各スキルで「必ず含む1句」を1つ決めて固定する（本文の骨格が崩れていないかの目印）。
_INVESTIGATE_SKILLS = {
    "investigate-list": "list_docs",
    "investigate-spec": "read_only=True",
    "investigate-impact": "graph_neighbors",
    "investigate-cause": "仮説",
    "investigate-compare": "compare_documents",
}


def _mk(d: pathlib.Path) -> pathlib.Path:
    d.mkdir(parents=True, exist_ok=True)
    return d


# ===== 3本のベーススキルが実在し、frontmatter/用途前置きを満たす =====

def test_base_skills_exist_with_frontmatter():
    for name in ("xlsx", "docx", "pptx"):
        p = S.BASE_SKILLS_DIR / name / "SKILL.md"
        assert p.is_file(), f"{name}/SKILL.md が無い"
        txt = p.read_text(encoding="utf-8")
        assert txt.startswith("---\n"), f"{name}/SKILL.md に frontmatter が無い"
        assert "name:" in txt.splitlines()[1], f"{name}/SKILL.md の frontmatter に name が無い"
        assert any(l.startswith("description:") for l in txt.splitlines()[:5]), \
            f"{name}/SKILL.md の frontmatter に description が無い"


def test_base_skills_mention_correct_library_and_common_rules():
    libs = {"xlsx": "openpyxl", "docx": "python-docx", "pptx": "python-pptx"}
    for name, lib in libs.items():
        txt = (S.BASE_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert lib in txt, f"{name}/SKILL.md に {lib} の言及が無い"
        # 共通ルール（カレントディレクトリ直下・日本語ファイル名・ネットワーク不可・完了報告）。
        for phrase in ("カレントディレクトリ", "authoring 直下", "ネットワーク", "報告する"):
            assert phrase in txt, f"{name}/SKILL.md に共通ルール文言が無い: {phrase!r}"


# ===== 調査スキル5本（S3・investigate-*）: frontmatter・必須句・Python コード片の構文 =====

def test_investigate_skills_exist_with_frontmatter_name_matches_dir():
    for name in _INVESTIGATE_SKILLS:
        p = S.BASE_SKILLS_DIR / name / "SKILL.md"
        assert p.is_file(), f"{name}/SKILL.md が無い"
        txt = p.read_text(encoding="utf-8")
        assert txt.startswith("---\n"), f"{name}/SKILL.md に frontmatter が無い"
        lines = txt.splitlines()
        assert lines[1] == f"name: {name}", f"{name}/SKILL.md の frontmatter name がディレクトリ名と不一致"


def test_investigate_skills_description_nonempty_and_ends_with_使う():
    for name in _INVESTIGATE_SKILLS:
        txt = (S.BASE_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        desc_line = next(l for l in txt.splitlines()[:6] if l.startswith("description:"))
        desc = desc_line[len("description:"):].strip()
        assert desc, f"{name}/SKILL.md の description が空"
        assert desc.endswith("使う。"), f"{name}/SKILL.md の description が「使う」で終わっていない: {desc!r}"


def test_investigate_skills_have_required_phrase_and_common_structure():
    for name, required_phrase in _INVESTIGATE_SKILLS.items():
        txt = (S.BASE_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert required_phrase in txt, f"{name}/SKILL.md に必須句が無い: {required_phrase!r}"
        # 共通骨格: 4節（最初に開くもの／中身の確認／完了条件と中断／回答の形）＋回答末尾の固定書式。
        for phrase in ("最初に開くもの", "完了条件と中断", "回答の形", "参照した資料:"):
            assert phrase in txt, f"{name}/SKILL.md に共通骨格の文言が無い: {phrase!r}"
        line_count = len(txt.splitlines())
        assert 60 <= line_count <= 160, f"{name}/SKILL.md の行数が想定範囲外: {line_count}"


def test_investigate_skills_python_code_blocks_are_syntactically_valid():
    """各スキルの ```python コード片は実際に ast.parse できる（API 名の初歩的な誤りを防ぐ）。"""
    fence = re.compile(r"```python\n(.*?)```", re.S)
    for name in _INVESTIGATE_SKILLS:
        txt = (S.BASE_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        blocks = fence.findall(txt)
        assert blocks, f"{name}/SKILL.md に python コードブロックが無い"
        for i, block in enumerate(blocks):
            try:
                ast.parse(block)
            except SyntaxError as e:
                raise AssertionError(f"{name}/SKILL.md のコードブロック{i}が構文エラー: {e}") from e


# ===== deploy_skills: base 配備・個人オーバーレイ・毎回作り直し =====

def test_deploy_skills_copies_all_three_base_skills():
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        S.deploy_skills(authoring, "u1", users_dir)
        dest = authoring / ".agents" / "skills"
        for name in ("xlsx", "docx", "pptx"):
            assert (dest / name / "SKILL.md").is_file(), f"{name} が配備されていない"


def test_deploy_skills_copies_all_nine_base_skills_including_investigate():
    """S3: ベーススキルは9本（既存4＝xlsx/docx/pptx/marp＋新5＝investigate-*）全てが配備される。"""
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        S.deploy_skills(authoring, "u1", users_dir)
        dest = authoring / ".agents" / "skills"
        for name in ("xlsx", "docx", "pptx", "marp", *_INVESTIGATE_SKILLS):
            assert (dest / name / "SKILL.md").is_file(), f"{name} が配備されていない"


def test_deploy_skills_personal_overlay_replaces_same_name():
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        personal = _mk(users_dir / "u1" / "workspace" / "skills" / "xlsx")
        (personal / "SKILL.md").write_text("---\nname: my-xlsx\n---\n個人カスタム版\n", encoding="utf-8")
        S.deploy_skills(authoring, "u1", users_dir)
        dest_skill = authoring / ".agents" / "skills" / "xlsx" / "SKILL.md"
        assert dest_skill.is_file()
        assert "個人カスタム版" in dest_skill.read_text(encoding="utf-8"), \
            "同名の個人スキルが base を置換していない"
        # docx/pptx は個人オーバーレイが無いので base のまま。
        assert "python-docx" in (authoring / ".agents" / "skills" / "docx" / "SKILL.md").read_text(encoding="utf-8")


def test_deploy_skills_personal_skill_without_conflict_is_added():
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        personal = _mk(users_dir / "u1" / "workspace" / "skills" / "my-tool")
        (personal / "SKILL.md").write_text("---\nname: my-tool\n---\n独自スキル\n", encoding="utf-8")
        S.deploy_skills(authoring, "u1", users_dir)
        assert (authoring / ".agents" / "skills" / "my-tool" / "SKILL.md").is_file()
        assert (authoring / ".agents" / "skills" / "xlsx" / "SKILL.md").is_file()   # base も両立


def test_deploy_skills_no_personal_dir_is_fine():
    """個人スキル未作成（既定）でも base スキルだけが配備されて正常終了する。"""
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        S.deploy_skills(authoring, "u1", users_dir)   # 個人 skills/ ディレクトリを一切作らない
        dest = authoring / ".agents" / "skills"
        # 期待は「base スキル一式（marp を含む）と完全一致・余分なし」。base 追加に頑健なよう
        # 期待値は原本ディレクトリから導出する（xlsx/docx/pptx/marp の少なくとも4本を含む）。
        base_names = {p.name for p in S.BASE_SKILLS_DIR.iterdir() if p.is_dir()}
        assert base_names >= {"xlsx", "docx", "pptx", "marp"}
        assert base_names == {p.name for p in dest.iterdir()}


def test_deploy_skills_rebuilds_every_run_idempotently():
    """毎回作り直し: 前回実行で残った余分なファイルが次回配備で消える。"""
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        dest = authoring / ".agents" / "skills"
        _mk(dest / "leftover")
        (dest / "leftover" / "old.txt").write_text("stale", encoding="utf-8")
        S.deploy_skills(authoring, "u1", users_dir)
        assert not (dest / "leftover").exists(), "毎回作り直しになっていない（前回の残骸が残った）"
        assert (dest / "xlsx" / "SKILL.md").is_file()


# ===== symlink 拒否（fail-closed） =====

def test_deploy_skills_rejects_symlinked_personal_skill_dir():
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        outside = _mk(pathlib.Path(td) / "outside")
        (outside / "SKILL.md").write_text("SHOULD NOT BE COPIED", encoding="utf-8")
        skills_dir = _mk(users_dir / "u1" / "workspace" / "skills")
        (skills_dir / "evil").symlink_to(outside)
        S.deploy_skills(authoring, "u1", users_dir)
        assert not (authoring / ".agents" / "skills" / "evil").exists(), \
            "symlink 個人スキルが配備されてしまった（封じ込め崩壊）"
        # base 3本は正常に配備される（個人スキルの拒否が全体を壊さない）。
        assert (authoring / ".agents" / "skills" / "xlsx" / "SKILL.md").is_file()


def test_deploy_skills_rejects_skill_dir_containing_symlink_file():
    """スキルフォルダ自体は通常ディレクトリだが、中に symlink ファイルが1つでもあれば拒否する。"""
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        secret = pathlib.Path(td) / "secret.txt"
        secret.write_text("SECRET", encoding="utf-8")
        skill_dir = _mk(users_dir / "u1" / "workspace" / "skills" / "bad")
        (skill_dir / "SKILL.md").write_text("---\nname: bad\n---\n", encoding="utf-8")
        (skill_dir / "leak").symlink_to(secret)
        S.deploy_skills(authoring, "u1", users_dir)
        assert not (authoring / ".agents" / "skills" / "bad").exists(), \
            "symlink を含む個人スキルフォルダが配備されてしまった"


def test_deploy_skills_dest_root_symlink_is_rejected_not_followed():
    """RV 系の流儀踏襲: 配備先 .agents/skills 自体が symlink でも追従せず作り直す。"""
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        evil_target = _mk(pathlib.Path(td) / "evil_target")
        (authoring / ".agents").mkdir(parents=True, exist_ok=True)
        (authoring / ".agents" / "skills").symlink_to(evil_target)
        S.deploy_skills(authoring, "u1", users_dir)
        dest = authoring / ".agents" / "skills"
        assert not dest.is_symlink(), "配備先が symlink のまま残っている"
        assert (dest / "xlsx" / "SKILL.md").is_file()
        assert not (evil_target / "xlsx").exists(), "symlink の指す先（authoring 外）に書き込んでしまった"


# ===== CodexProvider.run(): 配備呼び出し＋台帳スナップショット除外（ソース検査） =====

def test_codex_run_deploys_skills_before_subprocess_spawn():
    """R1b で Popen 呼出は `_attempt()`（ネスト関数）に切り出された。`inspect.getsource` は
    ネスト関数の**定義**を try の外（テキスト上手前）に出すため、`subprocess.Popen` の文字列位置は
    もう「実行順」を表さない（定義位置と呼出位置がズレる）。`_attempt(` の**呼出**
    （`yield from _attempt(`）と比較する。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    assert "codex_skills.deploy_skills" in src, "run() が codex_skills.deploy_skills を呼んでいない"
    i_deploy = src.index("codex_skills.deploy_skills")
    i_call = src.index("yield from _attempt(")
    assert i_deploy < i_call, "スキル配備が Popen 実行（_attempt 呼出）より後に呼ばれている（実行前に配備されていない）"


def test_ledger_snapshot_excludes_agents_dir():
    """新規ファイル検出（台帳登録スキャン）が .tmp と同様に .agents 配下も除外している（ソース検査）。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    assert src.count('{".tmp", ".agents"}') >= 2, \
        "before/after 両方のスナップショットで .agents 除外が入っていない"


def test_deploy_skills_rejects_symlinked_agents_parent():
    """RV HIGH（Phase1）: 親 `.agents` 自体が symlink（Codex は authoring に書ける＝前回実行で
    残せる）だと、旧実装は symlink 先の実ディレクトリを rmtree/copytree で破壊した。
    symlink は unlink され、外側は無傷のまま実ディレクトリに配備し直されること。"""
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        outside = _mk(pathlib.Path(td) / "outside")
        victim = _mk(outside / "skills") / "victim.txt"
        victim.write_text("user data", encoding="utf-8")
        (authoring / ".agents").symlink_to(outside)

        S.deploy_skills(authoring, "u1", users_dir)

        assert victim.read_text(encoding="utf-8") == "user data", "authoring 外のデータが破壊された"
        agents_dir = authoring / ".agents"
        assert not agents_dir.is_symlink() and agents_dir.is_dir(), ".agents が実ディレクトリで再作成されていない"
        assert (agents_dir / "skills" / "xlsx" / "SKILL.md").is_file(), "配備が完了していない"
