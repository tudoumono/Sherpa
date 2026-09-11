#!/usr/bin/env python3
"""スライス完了ゲート（`make gate-slice`）。変更ファイルから該当テストスイートを自動選択して
実行する。選択ロジック（`select_tests`）は git を叩かない純粋関数として分離してあり、単体テスト
（`tests/unit/test_gate_slice.py`）が直接呼び出す。

対象範囲: `git merge-base <base> HEAD` から `HEAD` までの差分（コミット済み）＋作業ツリーの
未コミット変更（staged・unstaged・新規未追跡ファイルを含む）。

選択規則の正典は `docs/20-開発ハーネス.md` §5。「対象なしで成功」にはしない（選択できない変更は
全件へ昇格する）。
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --- (a) 対応表: ファイルパスのパターン → テストパスのパターン -------------------------------
_RE_TOP_LEVEL = re.compile(r"^sherpa/([^/]+)\.py$")
_RE_SUBPACKAGE = re.compile(r"^sherpa/([^/]+)/([^/]+)\.py$")
_RE_PROVIDERS_CODEX = re.compile(r"^sherpa/providers/codex/")
_RE_ROUTERS = re.compile(r"^sherpa/routers/([^/]+)\.py$")
_RE_WEB_CHAT = re.compile(r"^web/chat/")
_RE_WEB_PAGE = re.compile(r"^web/([^/]+)\.[^/.]+$")
_RE_TESTS = re.compile(r"^tests/")
_RE_DOCS = re.compile(r"^docs/")
_RE_ANY_MD = re.compile(r"\.md$", re.IGNORECASE)
# conftest・共通ヘルパ・goldens はディレクトリ単位のテスト共通土台（そのディレクトリの
# スイート全件へ）。tests/<dir>/conftest.py・tests/<dir>/_*.py はそのディレクトリのテストから
# 参照されうる共通ヘルパのため dir 候補にする。tests/ 直下の conftest.py と tests/_*.py は
# 全ディレクトリから参照されうるため、個別の対応表候補ではなく select_tests 側で
# 「共通フィクスチャ」として全件昇格を扱う。
_RE_SUBDIR_CONFTEST = re.compile(r"^tests/([^/]+)/conftest\.py$")
_RE_SUBDIR_HELPER = re.compile(r"^tests/([^/]+)/_[^/]+\.py$")
_RE_ROOT_CONFTEST = re.compile(r"^tests/conftest\.py$")
_RE_TOP_HELPER = re.compile(r"^tests/_[^/]+\.py$")
_RE_GOLDENS = re.compile(r"^tests/([^/]+)/goldens/")

# (c) 動的登録・設定キー定義ファイル（registry/__init__.py 以外の分）。
_CONFIG_KEY_FILES = {
    "sherpa/ingest/office_md.py",       # SHERPA_ARMS 変換アームレジストリ
    "sherpa/routers/system.py",         # SettingsReq（個人設定キー定義）
    "sherpa/routers/system_extras.py",  # SystemSettingsReq（全体設定キー定義）
    "sherpa/store/settings.py",         # _SETTINGS_DEFAULT（個人設定の既定値表）
}
_INFRA_FILES = {".env.example", "pyproject.toml", "Makefile"}
_INFRA_GLOB_RE = re.compile(r"^requirements[^/]*\.txt$")

# world 系（取り込み・スコープ・グラフ投入）の変更は integration 領域必須スイートの対象。
_WORLD_AREA_FILES = {
    "sherpa/world_admin_service.py",
    "sherpa/worlds.py",
    "sherpa/scope.py",
    "sherpa/es_index.py",
}
_RE_INGEST = re.compile(r"^sherpa/ingest/")

_RETIREMENT_WORDS_RE = re.compile(r"退役|撤去")


@dataclass
class SelectionResult:
    selected: list[str]
    escalated: bool
    escalation_reasons: list[str]
    areas: set[str]
    retirement_violation: bool = False
    retirement_reason: str | None = None
    # 変更ファイルごとの選択理由タグ（診断出力用。a=対応表・b=import逆引き・c=未知の場所・d=共通フィクスチャ）。
    file_reasons: dict[str, list[str]] = field(default_factory=dict)
    # import 逆引きの走査対象から外れたテストファイル（構文エラー・repo_root 相対）。
    import_scan_syntax_errors: list[str] = field(default_factory=list)


def _relpath(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _structural_candidates(file: str) -> tuple[bool, list[tuple[str, str]]]:
    """`file` が対応表のどれかに構造的に一致するか、と選択候補（種別・値）の一覧を返す。

    候補の種別: "glob"（tests_root 相対の glob パターン）／"dir"（tests_root 相対のディレクトリ名・
    ディレクトリ全体を選ぶ・ファイル単位の当たり判定にも数える）／"area_dir"（"dir" と同じく
    ディレクトリ全体を選ぶが、無条件で足す「領域候補」のためファイル単位の当たり判定には
    数えない・例: router 変更に無条件で足す contract）／"self"（repo_root 相対のパス・
    そのファイル自身を選ぶ）。
    """
    matched = False
    candidates: list[tuple[str, str]] = []

    m = _RE_TOP_LEVEL.match(file)
    if m:
        matched = True
        mod = m.group(1)
        candidates.append(("glob", f"unit/test_{mod}*.py"))
        candidates.append(("glob", f"api/test_{mod}*.py"))

    m = _RE_SUBPACKAGE.match(file)
    if m:
        matched = True
        pkg, mod = m.group(1), m.group(2)
        candidates.append(("glob", f"unit/test_{pkg}_{mod}*.py"))
        candidates.append(("glob", f"unit/test_{mod}*.py"))

    if _RE_PROVIDERS_CODEX.match(file):
        matched = True
        candidates.append(("glob", "unit/test_codex_*.py"))
        candidates.append(("glob", "unit/test_agents_*.py"))

    m = _RE_ROUTERS.match(file)
    if m:
        matched = True
        r = m.group(1)
        candidates.append(("glob", f"api/test_{r}*.py"))
        # "area_dir": 全 router 変更に無条件で contract 全件を実行対象へ加える「領域候補」。
        # "dir"（conftest・共通ヘルパ由来）とは違い、このルータ固有のテストが実在するかの
        # ファイル単位の当たり判定には数えない（無条件のため、当たり判定に混ぜるとテストの
        # 無いルータ変更が「未知の場所」として昇格すべき場面をすり抜けてしまう）。
        candidates.append(("area_dir", "contract"))

    if _RE_WEB_CHAT.match(file):
        matched = True
        candidates.append(("glob", "e2e/test_chat_*.py"))

    m = _RE_WEB_PAGE.match(file)
    if m:
        matched = True
        page = m.group(1)
        candidates.append(("glob", f"e2e/test_{page}*.py"))

    m = _RE_SUBDIR_CONFTEST.match(file)
    if m:
        matched = True
        candidates.append(("dir", m.group(1)))

    m = _RE_SUBDIR_HELPER.match(file)
    if m:
        matched = True
        candidates.append(("dir", m.group(1)))

    m = _RE_GOLDENS.match(file)
    if m:
        matched = True
        candidates.append(("dir", m.group(1)))

    if _RE_TESTS.match(file):
        matched = True
        # 自身だけを選ぶ「self」規則は test_*.py に限る（conftest.py・goldens・共通ヘルパは
        # 上の dir 候補か、select_tests 側の共通フィクスチャ全件昇格で扱う）。
        if Path(file).name.startswith("test_"):
            candidates.append(("self", file))

    if _RE_DOCS.match(file) or _RE_ANY_MD.search(file):
        matched = True
        candidates.append(("glob", "unit/test_docs_gates.py"))

    return matched, candidates


def _expand_candidates(
    candidates: list[tuple[str, str]], tests_root: Path, repo_root: Path
) -> list[str]:
    out: list[str] = []
    for kind, value in candidates:
        if kind == "glob":
            if not tests_root.exists():
                continue
            for p in sorted(tests_root.glob(value)):
                if p.is_file():
                    out.append(_relpath(p, repo_root))
        elif kind in ("dir", "area_dir"):
            d = tests_root / value
            if d.is_dir():
                out.append(_relpath(d, repo_root))
        elif kind == "self":
            p = repo_root / value
            if p.is_file():
                out.append(_relpath(p, repo_root))
    return out


def _load_test_file_asts(
    tests_root: Path, repo_root: Path
) -> tuple[dict[Path, ast.Module], list[str]]:
    """テストファイルを1回だけ ast.parse し、以後の import 逆引きはこのキャッシュを使い回す。
    構文エラーのファイルは走査対象から外し、repo_root 相対パスで理由に出す（黙って落とさない）。
    """
    trees: dict[Path, ast.Module] = {}
    syntax_error_files: list[str] = []
    if not tests_root.exists():
        return trees, syntax_error_files
    for p in sorted(tests_root.rglob("*.py")):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            trees[p] = ast.parse(text)
        except SyntaxError:
            syntax_error_files.append(_relpath(p, repo_root))
    return trees, syntax_error_files


def _module_dotted(file: str) -> str:
    return file[: -len(".py")].replace("/", ".")


def _ast_references_module(tree: ast.Module, dotted: str) -> bool:
    """`import <dotted>` / `from <parent> import <last>` / `from <dotted> import ...` の
    いずれかで `dotted` を参照しているかを AST で判定する（複数行・括弧付き import も対象）。"""
    parent, _, last = dotted.rpartition(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == dotted for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相対 import（`from . import x`）は対象外（dotted は常に絶対パス）
                continue
            mod = node.module or ""
            if mod == dotted:
                return True
            if parent and mod == parent and any(
                alias.name in (last, "*") for alias in node.names
            ):
                return True
    return False


def _import_matches(dotted: str, test_asts: dict[Path, ast.Module]) -> list[Path]:
    return [path for path, tree in test_asts.items() if _ast_references_module(tree, dotted)]


def _compute_areas(changed_files: list[str]) -> set[str]:
    areas: set[str] = set()
    for f in changed_files:
        if f.startswith("web/"):
            areas.add("e2e")
        if f in _WORLD_AREA_FILES or _RE_INGEST.match(f):
            areas.add("integration")
        if f.startswith("sherpa/routers/"):
            areas.add("api")
    return areas


def select_tests(
    changed_files: list[str],
    *,
    deleted_test_files: list[str],
    head_commit_message: str,
    repo_root: Path,
    tests_root: Path | None = None,
    deleted_test_commit_bodies: dict[str, str] | None = None,
    uncommitted_deleted_test_files: list[str] | None = None,
) -> SelectionResult:
    repo_root = Path(repo_root)
    tests_root = Path(tests_root) if tests_root is not None else repo_root / "tests"

    changed_files = sorted(set(changed_files))

    # テスト削除の退役判定。コミット済み削除はパスごとの削除コミット本文（呼び出し側が
    # `git log --diff-filter=D <merge_base>..HEAD -- <path>` で集めた、削除コミットに
    # 限定した本文）を見る。渡されていないパスは
    # 後方互換のため head_commit_message にフォールバックする。未コミットの削除には削除
    # コミット自体が存在しないため、無条件で「理由不明」扱いにする（コミットしてから通す）。
    deleted = sorted({f for f in deleted_test_files if f.startswith("tests/")})
    uncommitted_deleted = sorted(
        {f for f in (uncommitted_deleted_test_files or []) if f.startswith("tests/")}
    )
    bodies = deleted_test_commit_bodies or {}
    no_reason_committed = [
        f for f in deleted
        if not _RETIREMENT_WORDS_RE.search(bodies.get(f, head_commit_message) or "")
    ]

    retirement_violation = bool(no_reason_committed) or bool(uncommitted_deleted)
    retirement_reason: str | None = None
    if retirement_violation:
        reason_parts: list[str] = []
        if no_reason_committed:
            reason_parts.append(
                "削除コミットの本文に「退役」「撤去」のいずれも含まれません: "
                + ", ".join(no_reason_committed)
            )
        if uncommitted_deleted:
            reason_parts.append(
                "未コミットの削除は削除コミットが無く理由を確認できません"
                "（コミットしてから通してください）: " + ", ".join(uncommitted_deleted)
            )
        retirement_reason = " / ".join(reason_parts)

    test_asts, import_scan_syntax_errors = _load_test_file_asts(tests_root, repo_root)

    selected: set[str] = set()
    file_reasons: dict[str, list[str]] = {}
    unknown_files: list[str] = []
    shared_fixture_files: list[str] = []
    escalation_reasons_extra: list[str] = []
    registry_init_files: list[str] = []
    config_key_files: list[str] = []
    infra_files: list[str] = []

    for f in changed_files:
        reasons: list[str] = []

        _structural_match, candidates = _structural_candidates(f)
        expanded = _expand_candidates(candidates, tests_root, repo_root)
        if expanded:
            reasons.append("a:対応表")
            selected.update(expanded)

        # ファイル単位の当たり判定は "area_dir"（無条件の領域候補・例: router→contract）を
        # 除いた候補だけで見る。無条件候補はどの変更ファイルでも常に実在するため、当たり判定に
        # 混ぜるとファイル固有のテストが無い変更（ファイル単位の昇格）を隠してしまう。
        file_hit_candidates = [c for c in candidates if c[0] != "area_dir"]
        file_hit_expanded = _expand_candidates(file_hit_candidates, tests_root, repo_root)

        import_match_paths: list[str] = []
        if f.startswith("sherpa/") and f.endswith(".py"):
            dotted = _module_dotted(f)
            import_match_paths = [
                _relpath(p, repo_root) for p in _import_matches(dotted, test_asts)
            ]
        if import_match_paths:
            reasons.append("b:import逆引き")
            # 逆引きで拾った共通ヘルパ（tests/_*.py・tests/<dir>/_*.py・conftest.py）は単独では
            # 収集 0 件＝終了コード 5 になるので、その利用側（tests/ 直下なら全件・サブディレクトリ
            # ならそのディレクトリ全件）へ広げる。
            for ip in import_match_paths:
                if _RE_TOP_HELPER.match(ip) or _RE_ROOT_CONFTEST.match(ip):
                    selected.update({"tests/unit", "tests/contract"})
                    escalation_reasons_extra.append(f"逆引きが共通フィクスチャ {ip} に当たった")
                elif _RE_SUBDIR_HELPER.match(ip) or (ip.endswith("/conftest.py") and ip.startswith("tests/")):
                    selected.add("/".join(ip.split("/")[:2]))
                else:
                    selected.add(ip)

        # ファイル単位の昇格判定: このファイル自身が対応表（無条件の領域候補を除く）からも
        # import 逆引きからも実在するテストを1件も出していなければ、他の変更ファイルの選択
        # 結果に関わらず「未知の場所」として数える（別ファイルの成功で隠さない）。
        # tests/conftest.py・tests/_*.py（共通フィクスチャ）はここでは扱わず、専用の全件
        # 昇格理由に回す。
        if _RE_ROOT_CONFTEST.match(f) or _RE_TOP_HELPER.match(f):
            shared_fixture_files.append(f)
            reasons.append("d:共通フィクスチャ")
        elif not file_hit_expanded and not import_match_paths:
            unknown_files.append(f)
            reasons.append("c:未知の場所")

        name = Path(f).name
        if name == "__init__.py" or "registry" in name.lower():
            registry_init_files.append(f)
        if f in _CONFIG_KEY_FILES:
            config_key_files.append(f)
        if f in _INFRA_FILES or _INFRA_GLOB_RE.match(name):
            infra_files.append(f)

        file_reasons[f] = reasons or ["(該当なし)"]

    escalation_reasons: list[str] = list(escalation_reasons_extra)
    if unknown_files:
        escalation_reasons.append(
            "対応表にも import 逆引きにも実在するテストが無い変更ファイル: "
            + ", ".join(unknown_files)
        )
    if shared_fixture_files:
        escalation_reasons.append(
            "共通フィクスチャ（tests/conftest.py・tests/_*.py）の変更: "
            + ", ".join(sorted(set(shared_fixture_files)))
        )
    if registry_init_files:
        escalation_reasons.append(
            "registry/__init__.py の変更: " + ", ".join(sorted(set(registry_init_files)))
        )
    if config_key_files:
        escalation_reasons.append(
            "動的登録・設定キー定義ファイルの変更: " + ", ".join(sorted(set(config_key_files)))
        )
    if infra_files:
        escalation_reasons.append(
            ".env.example/pyproject.toml/Makefile/requirements*.txt の変更: "
            + ", ".join(sorted(set(infra_files)))
        )
    # テスト削除は独立した昇格条件（退役の理由が明記されていても、削除の影響範囲を
    # 確認するため全件へ昇格する）。
    all_deleted = sorted(set(deleted) | set(uncommitted_deleted))
    if all_deleted:
        escalation_reasons.append(
            "テストファイルの削除（理由の有無に関わらず全件へ昇格）: " + ", ".join(all_deleted)
        )
    if changed_files and not selected and not escalation_reasons:
        escalation_reasons.append("選択された pytest 対象が0件（変更ファイルは1件以上あります）")

    escalated = bool(escalation_reasons)

    if escalated:
        # 昇格は「単体＋契約の全件」を選択に加えるだけで、対応表・import 逆引きで既に
        # 選ばれたスイート（e2e/api/integration 等）は消さない。
        final_selected = sorted(selected | {"tests/unit", "tests/contract"})
        areas = {"e2e", "integration", "api"}
    else:
        final_selected = sorted(selected)
        areas = _compute_areas(changed_files)

    return SelectionResult(
        selected=final_selected,
        escalated=escalated,
        escalation_reasons=escalation_reasons,
        areas=areas,
        retirement_violation=retirement_violation,
        retirement_reason=retirement_reason,
        file_reasons=file_reasons,
        import_scan_syntax_errors=import_scan_syntax_errors,
    )


# tests/ 直下の第1階層＝スイート。同名テストファイルが異なるスイートに共存すると（例:
# tests/api/test_chat_turns.py と tests/unit/test_chat_turns.py）、1つの pytest プロセスに
# 両方渡した時点で ImportPathMismatchError（収集エラー）で止まる。スイートごとに別プロセスへ
# 分けて順次実行することで回避する。順序は固定（docs/20-開発ハーネス.md の段階表に登場する順）。
_SUITE_ORDER = ["unit", "contract", "api", "integration", "e2e"]


def _suite_of(path: str) -> str:
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "tests":
        return parts[1]
    return "(other)"


def plan_pytest_commands(selected: list[str]) -> list[list[str]]:
    """選択結果（`tests/<suite>/...` のファイル/ディレクトリが混在した一覧）を、スイートごとに
    分けた pytest コマンド列へ変換する純関数（git を叩かない・順序は固定＝`_SUITE_ORDER`）。
    空スイートはコマンドを出さない。`_SUITE_ORDER` に無い未知のスイート名は末尾へ1本にまとめる
    （黙って落とさない）。
    """
    by_suite: dict[str, list[str]] = {}
    for path in selected:
        by_suite.setdefault(_suite_of(path), []).append(path)

    commands: list[list[str]] = []
    # 既知の順 → それ以外のスイート（e2e_live 等）を名前順 → tests/ 外。どのスイートも落とさない
    # （落とすと「選択されたのに実行されず終了コード 0」＝対象なしで成功、になる）。
    ordered = list(_SUITE_ORDER) + sorted(
        k for k in by_suite if k not in _SUITE_ORDER and k != "(other)") + ["(other)"]
    for suite in ordered:
        paths = by_suite.get(suite)
        if paths:
            commands.append([sys.executable, "-m", "pytest", *paths, "-q"])
    return commands


# --- CLI（git 呼び出し・pytest 起動。選択ロジックの外側） ------------------------------------


def _git(repo_root: Path, *args: str) -> str:
    # core.quotePath=false: 非 ASCII ファイル名を八進エスケープの引用形式（"\343\201\202..."）
    # ではなく素の UTF-8 で返させる。引用形式のままだと対応表・import 逆引きの正規表現が
    # 一致せず、日本語ファイル名の変更が軒並み「未知の場所」に化けてしまう。
    return subprocess.check_output(
        ["git", "-c", "core.quotePath=false", *args], cwd=repo_root, text=True
    )


def _parse_porcelain_status_line(line: str) -> tuple[list[str], list[str]]:
    """`git status --porcelain` の1行を (changed に足すパス, deleted に足すパス) へ変換する
    純関数（git を叩かない）。X/Y いずれかが `R`（リネーム）なら新旧パス両方を changed に
    加え、さらに Y 側が `D`（例: `RD` = 索引はリネーム済みだが作業ツリーで移動先が削除された）
    なら移動先パスを未コミット削除として扱う（リネーム自体は削除ではないが、リネーム後に
    移動先が消えているケースを見落とさない）。
    """
    x, y = line[0], line[1]
    rest = line[3:]
    if x == "R" or y == "R":
        old, _, new = rest.partition(" -> ")
        changed = [new, old]
        deleted = [new] if y == "D" else []
        return changed, deleted
    changed = [rest]
    deleted = [rest] if (x == "D" or y == "D") else []
    return changed, deleted


def _gather_git_state(
    repo_root: Path, base: str
) -> tuple[list[str], list[str], dict[str, str], list[str], str]:
    merge_base = _git(repo_root, "merge-base", base, "HEAD").strip()

    committed_changed: list[str] = []
    committed_deleted: list[str] = []
    diff_out = _git(repo_root, "diff", "--name-status", merge_base, "HEAD")
    for line in diff_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") or status.startswith("C"):
            old, new = parts[1], parts[2]
            committed_changed.append(new)
            # リネームは削除ではない（退役判定の対象外・純粋な "D" ステータスのみ数える）。
        else:
            path = parts[1]
            committed_changed.append(path)
            if status == "D":
                committed_deleted.append(path)

    head_commit_message = _git(repo_root, "log", "-1", "--format=%B", "HEAD")

    wt_changed: list[str] = []
    wt_deleted: list[str] = []
    status_out = _git(repo_root, "status", "--porcelain")
    for line in status_out.splitlines():
        if not line:
            continue
        changed, deleted = _parse_porcelain_status_line(line)
        wt_changed.extend(changed)
        wt_deleted.extend(deleted)

    changed_files = sorted(set(committed_changed) | set(wt_changed))
    deleted_test_files = sorted({p for p in committed_deleted if p.startswith("tests/")})
    uncommitted_deleted_test_files = sorted({p for p in wt_deleted if p.startswith("tests/")})

    # 削除パスごとの削除コミット本文（範囲内に複数コミットがあれば連結）。退役判定を
    # 「HEAD の本文」ではなく「実際にそのパスを削除したコミットの本文」に対応付ける。
    # --diff-filter=D で削除コミットだけに絞る（同じパスへの変更コミットの本文が
    # 紛れ込むと、無関係な語で退役判定をすり抜け得るため）。
    deleted_test_commit_bodies: dict[str, str] = {
        p: _git(
            repo_root, "log", "--diff-filter=D", "--format=%B", f"{merge_base}..HEAD", "--", p
        )
        for p in deleted_test_files
    }

    return (
        changed_files,
        deleted_test_files,
        deleted_test_commit_bodies,
        uncommitted_deleted_test_files,
        head_commit_message,
    )


def _print_report(
    result: SelectionResult, changed_files: list[str], commands: list[list[str]]
) -> None:
    print("=== 変更ファイル ===")
    if changed_files:
        for f in changed_files:
            reasons = ",".join(result.file_reasons.get(f, []))
            print(f"  {f}  [{reasons}]")
    else:
        print("  (なし)")
    print()

    if result.escalated:
        print("=== 全件へ昇格（理由） ===")
        for r in result.escalation_reasons:
            print(f"  - {r}")
        print()

    if result.import_scan_syntax_errors:
        print("=== import 逆引き対象外（構文エラー） ===")
        for f in result.import_scan_syntax_errors:
            print(f"  {f}")
        print()

    print("=== 実行する pytest 対象 ===")
    for s in result.selected:
        print(f"  {s}")
    print()

    print("=== 実行予定の pytest コマンド（スイート別・順次実行） ===")
    if commands:
        for cmd in commands:
            print("  " + " ".join(cmd))
    else:
        print("  (なし)")
    print()

    print("=== 変更領域（--areas-only 用） ===")
    for area in sorted(result.areas):
        print(f"AREA:{area}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="main", help="比較対象の ref（既定 main）")
    parser.add_argument(
        "--dry-run", action="store_true", help="選択結果だけ表示して pytest を実行しない"
    )
    parser.add_argument(
        "--areas-only",
        action="store_true",
        help="変更領域ラベル（AREA:xxx 行）だけを出して終了する（pytest は実行しない）",
    )
    args = parser.parse_args(argv)

    repo_root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    (
        changed_files,
        deleted_test_files,
        deleted_test_commit_bodies,
        uncommitted_deleted_test_files,
        head_commit_message,
    ) = _gather_git_state(repo_root, args.base)
    result = select_tests(
        changed_files,
        deleted_test_files=deleted_test_files,
        deleted_test_commit_bodies=deleted_test_commit_bodies,
        uncommitted_deleted_test_files=uncommitted_deleted_test_files,
        head_commit_message=head_commit_message,
        repo_root=repo_root,
    )

    commands = plan_pytest_commands(result.selected)
    _print_report(result, changed_files, commands)

    if result.retirement_violation:
        print()
        print(f"NG: {result.retirement_reason}")
        return 3

    if args.areas_only or args.dry_run:
        return 0

    env = os.environ.copy()
    env["SHERPA_USE_FIXTURES"] = "1"
    # スイートごとに別プロセスで順次実行する（同名テストファイルが異なるスイートに共存すると
    # 1プロセスに混ぜた時点で ImportPathMismatchError になるため）。最初に出た非ゼロを最終
    # 終了コードとして保持しつつ、後続スイートも実行して結果を全部表示する。
    final_returncode = 0
    for cmd in commands:
        proc = subprocess.run(cmd, cwd=repo_root, env=env)
        if proc.returncode != 0 and final_returncode == 0:
            final_returncode = proc.returncode
    return final_returncode


if __name__ == "__main__":
    raise SystemExit(main())
