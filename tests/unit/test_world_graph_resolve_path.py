"""`world_graph.resolve_path` の単体テスト（root から `rel` へ直接 `os.lstat` して降りる解決・
world 全体を走査しないことが要件）。

`safe_files`（全木走査）に依存していた旧実装と等価な contract を、走査コストを払わずに保つ:
symlink はどの階層でも拒否（脱出だけでなく root 内へのエイリアスも「実在する document」と
しない）・`..`/絶対/`\\`/NUL/空・`.` 要素は FS アクセス前に文字列だけで拒否・root 配下に収まらない
解決は拒否・存在しない/ファイルでない rel は None。
"""
from __future__ import annotations

import pytest

from sherpa import scope_infer
from sherpa.ingest import world_graph


def _mk_world(tmp_path):
    wd = tmp_path / "world"
    (wd / "4期更改" / "03_開発").mkdir(parents=True)
    (wd / "4期更改" / "03_開発" / "ORDER-MAIN.cbl").write_text("ORIG\n", encoding="utf-8")
    return wd


def test_resolve_path_returns_file_for_valid_nested_rel(tmp_path):
    wd = _mk_world(tmp_path)
    p = world_graph.resolve_path(wd, "4期更改/03_開発/ORDER-MAIN.cbl")
    assert p is not None and p.is_file()
    assert p.read_text(encoding="utf-8") == "ORIG\n"


def test_resolve_path_accepts_world_dir_as_str(tmp_path):
    """呼び出し元が `world_dir` を str で渡す既存の使い方（`worlds.world_dir` の一部戻り値互換）も解決できる。"""
    wd = _mk_world(tmp_path)
    p = world_graph.resolve_path(str(wd), "4期更改/03_開発/ORDER-MAIN.cbl")
    assert p is not None and p.is_file()


def test_resolve_path_rejects_dotdot_traversal(tmp_path):
    wd = _mk_world(tmp_path)
    (tmp_path / "secret.txt").write_text("out\n", encoding="utf-8")
    assert world_graph.resolve_path(wd, "../secret.txt") is None
    assert world_graph.resolve_path(wd, "4期更改/../../secret.txt") is None


def test_resolve_path_rejects_absolute_rel(tmp_path):
    wd = _mk_world(tmp_path)
    assert world_graph.resolve_path(wd, "/etc/passwd") is None


def test_resolve_path_rejects_nonexistent_rel(tmp_path):
    wd = _mk_world(tmp_path)
    assert world_graph.resolve_path(wd, "4期更改/03_開発/NOPE.cbl") is None
    assert world_graph.resolve_path(wd, "does/not/exist.md") is None


def test_resolve_path_rejects_directory_rel(tmp_path):
    """rel がディレクトリを指す場合は文書ではない＝None（旧 safe_files はファイルしか yield しない）。"""
    wd = _mk_world(tmp_path)
    assert world_graph.resolve_path(wd, "4期更改/03_開発") is None


def test_resolve_path_rejects_symlink_escape_outside_root(tmp_path):
    wd = _mk_world(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.cbl").write_text("SECRET\n", encoding="utf-8")
    try:
        (wd / "escape.cbl").symlink_to(outside / "secret.cbl")
    except OSError as e:
        pytest.skip(f"symlink 非対応の環境: {e}")
    assert world_graph.resolve_path(wd, "escape.cbl") is None


def test_resolve_path_rejects_symlink_leaf_within_root(tmp_path):
    """symlink の先が root 内でも、leaf 自体が symlink なら実在扱いしない
    （`safe_files` が symlink file を辿らない/yield しないのと同じ contract）。"""
    wd = _mk_world(tmp_path)
    real = wd / "4期更改" / "03_開発" / "ORDER-MAIN.cbl"
    try:
        (wd / "alias.cbl").symlink_to(real)
    except OSError as e:
        pytest.skip(f"symlink 非対応の環境: {e}")
    assert world_graph.resolve_path(wd, "alias.cbl") is None


def test_resolve_path_rejects_path_through_symlinked_directory(tmp_path):
    """途中階層が symlink（root 内を指していても）なら拒否する——`safe_files` は symlink dir を
    辿らないため、その配下は元から実在集合に現れない。"""
    wd = _mk_world(tmp_path)
    real_dir = wd / "4期更改" / "03_開発"
    try:
        (wd / "linkdir").symlink_to(real_dir)
    except OSError as e:
        pytest.skip(f"symlink 非対応の環境: {e}")
    assert world_graph.resolve_path(wd, "linkdir/ORDER-MAIN.cbl") is None
    # symlink を経由しない直接パスは従来どおり解決できる（symlink の存在自体が全体を壊さない）。
    assert world_graph.resolve_path(wd, "4期更改/03_開発/ORDER-MAIN.cbl") is not None


def test_resolve_path_rejects_empty_and_trailing_slash(tmp_path):
    wd = _mk_world(tmp_path)
    assert world_graph.resolve_path(wd, "") is None
    assert world_graph.resolve_path(wd, "4期更改/03_開発/") is None


def test_resolve_path_returns_none_when_world_dir_missing(tmp_path):
    assert world_graph.resolve_path(tmp_path / "no-such-world", "a.md") is None


def test_resolve_path_rejects_dot_component(tmp_path):
    """`.` は意図的な縮小として明示拒否する（RV1 是正・単純結合では実害が無くても正準表記から外れる）。"""
    wd = _mk_world(tmp_path)
    assert world_graph.resolve_path(wd, "./4期更改/03_開発/ORDER-MAIN.cbl") is None
    assert world_graph.resolve_path(wd, "4期更改/./03_開発/ORDER-MAIN.cbl") is None
    assert world_graph.resolve_path(wd, "4期更改/03_開発/ORDER-MAIN.cbl/.") is None


def test_resolve_path_rejects_backslash(tmp_path):
    """`\\` は FS アクセス前に文字列だけで拒否する（POSIX rel 契約を優先する意図的な制限）。"""
    wd = _mk_world(tmp_path)
    assert world_graph.resolve_path(wd, "4期更改\\03_開発\\ORDER-MAIN.cbl") is None


def test_resolve_path_rejects_embedded_nul(tmp_path):
    """NUL は `os.lstat` 等に渡すと `ValueError`（500 相当）になり得るため、FS アクセス前に拒否する。"""
    wd = _mk_world(tmp_path)
    assert world_graph.resolve_path(wd, "4期更改/03_開発/ORDER-MAIN.cbl\x00.txt") is None


def test_resolve_path_does_not_walk_world_tree(tmp_path, monkeypatch):
    """`scope_infer.safe_files`（world 全体の走査）を一切呼ばずに解決できることを固定する
    （DOC-1＝root/rel の直接結合＋実在確認へ置換・全木走査は撤去）。"""
    wd = _mk_world(tmp_path)

    def _must_not_walk(*a, **kw):
        raise AssertionError("resolve_path は safe_files（全木走査）を呼んではいけない")

    monkeypatch.setattr(scope_infer, "safe_files", _must_not_walk)
    p = world_graph.resolve_path(wd, "4期更改/03_開発/ORDER-MAIN.cbl")
    assert p is not None and p.is_file()
    # 拒否系（存在しない/トラバーサル）も走査を経由せず判定できる。
    assert world_graph.resolve_path(wd, "4期更改/03_開発/NOPE.cbl") is None
    assert world_graph.resolve_path(wd, "../etc/passwd") is None
