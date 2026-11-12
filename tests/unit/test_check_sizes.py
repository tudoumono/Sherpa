"""`scripts/check_sizes.py`（ファイル・関数の大きさの上限と部品の依存の向き）の契約テスト。一時ディレクトリの実ファイルで確かめる。"""
from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import check_sizes


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _func(name: str, body_lines: int) -> str:
    return f"def {name}():\n" + "    pass\n" * (body_lines - 1)


def _run(root: Path, *extra: str) -> int:
    return check_sizes.main(["--root", str(root), *extra])


def test_new_over_limit_file_and_function_fail(tmp_path, capsys):
    _write(tmp_path, "sherpa/big.py", "x = 1\n" * 1001)
    _write(tmp_path, "sherpa/fn.py", _func("long_one", 101) + _func("ok", 100))
    _write(tmp_path, "scripts/size_baseline.txt", "")
    assert _run(tmp_path) == 1
    out = capsys.readouterr().out
    assert "sherpa/big.py: 1001 行" in out
    assert "sherpa/fn.py::long_one: 101 行" in out
    assert "sherpa/fn.py::ok" not in out


def test_baseline_is_a_ratchet(tmp_path, capsys):
    _write(tmp_path, "sherpa/big.py", "x = 1\n" * 1100)
    assert _run(tmp_path, "--init") == 0
    assert _run(tmp_path) == 0
    _write(tmp_path, "sherpa/big.py", "x = 1\n" * 1101)
    assert _run(tmp_path) == 1
    assert "基準 1100 より増えた" in capsys.readouterr().out
    assert _run(tmp_path, "--write-baseline") == 0
    assert (tmp_path / "scripts/size_baseline.txt").read_text() == "sherpa/big.py\t1100\n"
    _write(tmp_path, "sherpa/big.py", "x = 1\n" * 1050)
    assert _run(tmp_path) == 1
    assert "減った（基準 1100）" in capsys.readouterr().out
    assert _run(tmp_path, "--write-baseline") == 0
    _write(tmp_path, "sherpa/big.py", "x = 1\n" * 1080)
    assert _run(tmp_path) == 1
    _write(tmp_path, "sherpa/big.py", "x = 1\n")
    assert _run(tmp_path) == 1
    assert "上限内に戻ったか消えた" in capsys.readouterr().out
    assert _run(tmp_path, "--write-baseline") == 0
    assert (tmp_path / "scripts/size_baseline.txt").read_text() == ""


def test_accept_new_only_admits_moves_within_the_old_value(tmp_path, capsys):
    _write(tmp_path, "sherpa/old.py", _func("long_one", 150))
    assert _run(tmp_path, "--init") == 0
    (tmp_path / "sherpa/old.py").unlink()
    _write(tmp_path, "sherpa/moved.py", _func("long_one", 200))
    assert _run(tmp_path, "--write-baseline", "--accept-new") == 1
    assert "対にできない" in capsys.readouterr().out
    _write(tmp_path, "sherpa/moved.py", _func("long_one", 150))
    _write(tmp_path, "sherpa/other.py", _func("another", 120))
    assert _run(tmp_path, "--write-baseline", "--accept-new") == 1
    _write(tmp_path, "sherpa/other.py", "x = 1\n")
    assert _run(tmp_path, "--write-baseline") == 1
    assert _run(tmp_path, "--write-baseline", "--accept-new") == 0
    assert _run(tmp_path) == 0


def test_absorb_raises_only_up_to_the_absorbed_line(tmp_path, capsys):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    _write(tmp_path, "scripts/a.py", "x = 1\n" * 1100)
    git("add", "-A")
    git("commit", "-qm", "theirs")
    _write(tmp_path, "scripts/a.py", "x = 1\n" * 1050)
    assert _run(tmp_path, "--init") == 0
    _write(tmp_path, "scripts/a.py", "x = 1\n" * 1100)
    assert _run(tmp_path, "--write-baseline") == 0
    assert _run(tmp_path) == 1
    assert _run(tmp_path, "--write-baseline", "--absorb", "HEAD") == 0
    assert _run(tmp_path) == 0
    _write(tmp_path, "scripts/a.py", "x = 1\n" * 1101)
    assert _run(tmp_path, "--write-baseline", "--absorb", "HEAD") == 0
    assert _run(tmp_path) == 1
    assert "基準 1100 より増えた" in capsys.readouterr().out


def test_missing_baseline_fails_and_init_refuses_to_overwrite(tmp_path, capsys):
    _write(tmp_path, "sherpa/a.py", "x = 1\n")
    assert _run(tmp_path) == 1
    assert _run(tmp_path, "--write-baseline") == 1
    assert "--init" in capsys.readouterr().out
    assert _run(tmp_path, "--init") == 0
    assert _run(tmp_path, "--init") == 1


def test_function_length_counts_nested_methods_and_duplicates(tmp_path):
    src = (
        "@deco\nclass K:\n"
        "    @deco\n    def m(self):\n" + "        pass\n" * 100 +   # 101 行（デコレータは数えない）
        "\n" + _func("dup", 101) + _func("dup", 102)
    )
    _write(tmp_path, "sherpa/m.py", src)
    over = check_sizes.measure_over_limit(tmp_path)
    assert over["sherpa/m.py::K.m"] == 101
    assert over["sherpa/m.py::dup"] == 101
    assert over["sherpa/m.py::dup#2"] == 102


def test_excluded_paths_are_not_measured(tmp_path):
    big = "x = 1\n" * 1500
    for rel in ("fixtures/a.py", "tests/unit/goldens/g.py", "web/vendor/v.js", "web/app.min.js", "sherpa/node_modules/n.js"):
        _write(tmp_path, rel, big)
    assert check_sizes.measure_over_limit(tmp_path) == {}


def _arch_tree(root: Path, rules: str) -> None:
    _write(root, "scripts/arch_rules.toml", rules)
    _write(root, "sherpa/graph/__init__.py", "from .api import build\n")
    _write(root, "sherpa/graph/api.py", "def build(): ...\n_secret = 1\n")
    _write(root, "sherpa/graph/hidden.py", "def h(): ...\n")
    _write(root, "sherpa/names/__init__.py", "from .core import lookup\n")
    _write(root, "sherpa/names/core.py", "def lookup(): ...\n")


_RULES = '[components."sherpa.graph"]\nallow = ["sherpa.names"]\n[components."sherpa.names"]\nallow = []\n'


def test_arch_allows_public_use_and_declared_direction(tmp_path):
    _arch_tree(tmp_path, _RULES)
    _write(tmp_path, "sherpa/graph/user.py", "from sherpa.names import lookup\nfrom .hidden import h\n")
    _write(tmp_path, "sherpa/other.py", "from sherpa.graph import build\nimport sherpa.names\n")
    assert check_sizes.check_arch(tmp_path, tmp_path / "scripts/arch_rules.toml") == []


def test_arch_detects_reverse_direction_and_private_access(tmp_path):
    _arch_tree(tmp_path, _RULES)
    _write(tmp_path, "sherpa/names/rev.py", "from sherpa.graph import build\n")
    _write(tmp_path, "sherpa/other.py", "from sherpa.graph.hidden import h\nfrom sherpa.graph.api import build\nfrom sherpa.graph import _secret\n")
    got = "\n".join(check_sizes.check_arch(tmp_path, tmp_path / "scripts/arch_rules.toml"))
    assert "sherpa/names/rev.py:1: 部品 sherpa.names は sherpa.graph を import してよい一覧に無い" in got
    assert "sherpa/other.py:1:" in got and "sherpa.graph.hidden" in got
    assert "sherpa/other.py:2:" in got and "sherpa.graph.api" in got
    assert "sherpa/other.py:3:" in got and "_secret" in got


def test_arch_shims_may_reexport_private_names_but_others_may_not(tmp_path):
    _arch_tree(tmp_path, 'shims = ["sherpa.old_graph"]\n' + _RULES)
    _write(tmp_path, "sherpa/old_graph.py", "from sherpa.graph.hidden import h\nfrom sherpa.graph.api import _secret\n")
    _write(tmp_path, "sherpa/other.py", "from sherpa.graph.api import _secret\n")
    got = check_sizes.check_arch(tmp_path, tmp_path / "scripts/arch_rules.toml")
    assert got and all(g.startswith("sherpa/other.py:") for g in got)


def test_empty_rules_are_green(tmp_path):
    _write(tmp_path, "scripts/arch_rules.toml", "# 空\n")
    _write(tmp_path, "sherpa/a.py", "import os\n")
    _write(tmp_path, "scripts/size_baseline.txt", "")
    assert _run(tmp_path) == 0
