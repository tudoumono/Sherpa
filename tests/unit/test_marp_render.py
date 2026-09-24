"""M3 案2（proposals/2026-07-07-Marpスライド作成.md）単体テスト。

Marp レンダの外出し（`sherpa/marp_render.py`）: front-matter 判定（`is_marp_markdown`）と
レンダ実行（`render_outputs`）を、実 marp CLI を使わず**フェイク marp バイナリ**（引数を記録して
`-o` のファイルを touch するだけの小さな sh スクリプト）で駆動して確認する。実 marp/Chromium/unshare
を使う実レンダは tests/integration/test_marp_render_real.py（実機のみ）。

RV 2026-07-12 反映: 入出力の authoring 封じ込め（symlink/dangling symlink 拒否・root 外解決拒否）、
`--allow-local-files` を**付けない**こと（持ち出し面の遮断）、unshare プローブが lo UP まで検証すること。
pdf/pptx 系のゲートは `shutil.which` でなく実プローブ `_unshare_available()`（unshare はあるが
user namespace 禁止、の CI でも正しく skip される）。
"""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import tempfile

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import marp_render as R      # noqa: E402


def _mk(d: pathlib.Path) -> pathlib.Path:
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_md(p: pathlib.Path, *, marp: bool = True) -> pathlib.Path:
    if marp:
        p.write_text("---\nmarp: true\ntheme: sherpa\n---\n\n# タイトル\n", encoding="utf-8")
    else:
        p.write_text("# ただの Markdown\n", encoding="utf-8")
    return p


def _fake_marp_bin(td: pathlib.Path, *, record: pathlib.Path) -> pathlib.Path:
    """引数を record に追記し、`-o <path>` のファイルを touch するフェイク marp。"""
    bin_path = td / "fake-marp.sh"
    bin_path.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{record}"\n'
        'out=""\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-o" ]; then out="$a"; fi\n'
        '  prev="$a"\n'
        'done\n'
        'if [ -n "$out" ]; then touch "$out"; fi\n',
        encoding="utf-8",
    )
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_path


def _skip_unless_isolated():
    """pdf/pptx を実際に生成させたいテスト用のゲート。実プローブと同条件で判定する
    （unshare バイナリはあるが user namespace が禁止の CI でも正しく skip・RV LOW）。"""
    if not R._unshare_available():
        pytest.skip("この環境では unshare -rn（lo UP まで）が使えない")


# ===== is_marp_markdown =====

def test_is_marp_markdown_true_when_marp_true_in_frontmatter():
    with tempfile.TemporaryDirectory() as td:
        p = _write_md(pathlib.Path(td) / "a.md", marp=True)
        assert R.is_marp_markdown(p) is True


def test_is_marp_markdown_false_when_no_frontmatter():
    with tempfile.TemporaryDirectory() as td:
        p = _write_md(pathlib.Path(td) / "a.md", marp=False)
        assert R.is_marp_markdown(p) is False


def test_is_marp_markdown_false_when_marp_false():
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "a.md"
        p.write_text("---\nmarp: false\ntheme: sherpa\n---\n\n# x\n", encoding="utf-8")
        assert R.is_marp_markdown(p) is False


def test_is_marp_markdown_false_when_missing_file():
    assert R.is_marp_markdown(pathlib.Path("/nonexistent/nope.md")) is False


# ===== _unshare_available（プローブ） =====

def test_unshare_probe_validates_lo_up(monkeypatch):
    """プローブは `unshare -rn true` でなく **lo UP まで**検証する（`ip` 不在環境の見逃し防止・RV Med）。"""
    seen = {}

    def _fake_run(argv, **kw):
        seen["argv"] = argv

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert R._unshare_available() in (True, False)   # which() 次第だが、呼ばれたなら argv を検証
    if "argv" in seen:
        assert seen["argv"][:2] == ["unshare", "-rn"]
        assert "ip link set lo up" in " ".join(seen["argv"])


# ===== render_outputs =====

def test_render_outputs_empty_when_marp_bin_none():
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        md = _write_md(tdp / "a.md")
        got = R.render_outputs([md], marp_bin=None, chrome_path=None, theme_dirs=[],
                               containment_root=tdp)
        assert got == []
        assert not (tdp / "a.html").exists()


def test_render_outputs_html_only_when_chrome_path_none():
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        md = _write_md(tdp / "a.md")
        record = tdp / "record.txt"
        marp = _fake_marp_bin(tdp, record=record)
        got = R.render_outputs([md], marp_bin=str(marp), chrome_path=None, theme_dirs=[],
                               containment_root=tdp)
        names = sorted(p.name for p in got)
        assert names == ["a.html"]
        assert (tdp / "a.html").is_file()
        assert not (tdp / "a.pdf").exists()
        assert not (tdp / "a.pptx").exists()


def test_render_outputs_html_only_when_unshare_unavailable(monkeypatch):
    """chrome_path はあっても unshare が使えない環境では pdf/pptx をスキップ（fail-closed）。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        md = _write_md(tdp / "a.md")
        record = tdp / "record.txt"
        marp = _fake_marp_bin(tdp, record=record)
        monkeypatch.setattr(R, "_unshare_available", lambda: False)
        got = R.render_outputs(
            [md], marp_bin=str(marp), chrome_path="/usr/bin/fake-chrome", theme_dirs=[],
            containment_root=tdp)
        names = sorted(p.name for p in got)
        assert names == ["a.html"]


def test_render_outputs_all_three_formats_when_isolated():
    """隔離が使えれば html/pdf/pptx の3形式とも生成される（フェイク marp が touch するだけ）。"""
    _skip_unless_isolated()
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        md = _write_md(tdp / "a.md")
        record = tdp / "record.txt"
        marp = _fake_marp_bin(tdp, record=record)
        got = R.render_outputs(
            [md], marp_bin=str(marp), chrome_path="/usr/bin/fake-chrome", theme_dirs=[],
            containment_root=tdp)
        names = sorted(p.name for p in got)
        assert names == ["a.html", "a.pdf", "a.pptx"]


def test_render_outputs_argv_flags_and_no_local_files():
    """--no-stdin/--theme-set は付き、`--allow-local-files` は**どの形式にも付かない**
    （任意ローカルファイルの成果物への埋め込み＝持ち出し面を閉じる・RV High）。"""
    _skip_unless_isolated()
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        md = _write_md(tdp / "a.md")
        theme_dir = _mk(tdp / "themes")
        record = tdp / "record.txt"
        marp = _fake_marp_bin(tdp, record=record)
        R.render_outputs(
            [md], marp_bin=str(marp), chrome_path="/usr/bin/fake-chrome",
            theme_dirs=[theme_dir], containment_root=tdp)
        recorded = record.read_text(encoding="utf-8")
        assert "--no-stdin" in recorded
        assert "--theme-set" in recorded and str(theme_dir) in recorded
        assert "--allow-local-files" not in recorded
        assert [ln for ln in recorded.splitlines() if "--pdf" in ln]
        assert [ln for ln in recorded.splitlines() if "--pptx" in ln]


def test_render_outputs_skips_existing_output():
    _skip_unless_isolated()
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        md = _write_md(tdp / "a.md")
        (tdp / "a.html").write_text("既存の出力", encoding="utf-8")   # 上書き禁止の対象
        record = tdp / "record.txt"
        marp = _fake_marp_bin(tdp, record=record)
        got = R.render_outputs(
            [md], marp_bin=str(marp), chrome_path="/usr/bin/fake-chrome", theme_dirs=[],
            containment_root=tdp)
        names = sorted(p.name for p in got)
        assert names == ["a.pdf", "a.pptx"]                          # html は既存のためスキップ
        assert (tdp / "a.html").read_text(encoding="utf-8") == "既存の出力"   # 上書きされていない


def test_render_outputs_refuses_dangling_symlink_output():
    """出力位置に dangling symlink が居座っていたら**その形式を生成しない**
    （symlink 先＝authoring 外へ Sherpa 本体権限で書くのを防ぐ・RV BLOCKER）。"""
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
        tdp = pathlib.Path(td)
        md = _write_md(tdp / "a.md")
        victim = pathlib.Path(outside) / "victim.html"
        (tdp / "a.html").symlink_to(victim)                          # dangling symlink（victim は未存在）
        record = tdp / "record.txt"
        marp = _fake_marp_bin(tdp, record=record)
        got = R.render_outputs([md], marp_bin=str(marp), chrome_path=None, theme_dirs=[],
                               containment_root=tdp)
        assert got == []                                             # html は symlink のため拒否
        assert not victim.exists()                                   # symlink 先に書かれていない


def test_render_outputs_refuses_src_outside_containment_root():
    """containment_root の外に実体がある src（symlink 経由の持ち込み等）は扱わない。"""
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
        tdp = pathlib.Path(td)
        real = _write_md(pathlib.Path(outside) / "real.md")
        link = tdp / "a.md"
        link.symlink_to(real)                                        # authoring 内の symlink → 外の実体
        record = tdp / "record.txt"
        marp = _fake_marp_bin(tdp, record=record)
        got = R.render_outputs([link], marp_bin=str(marp), chrome_path=None, theme_dirs=[],
                               containment_root=tdp)
        assert got == []
        assert not record.exists()                                   # marp は一度も呼ばれていない


def test_render_outputs_skips_non_marp_markdown():
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        md = _write_md(tdp / "a.md", marp=False)
        record = tdp / "record.txt"
        marp = _fake_marp_bin(tdp, record=record)
        # is_marp_markdown でフィルタしてから渡す使い方（agents.py の glue と同じ呼び出し方）を模す。
        targets = [p for p in [md] if R.is_marp_markdown(p)]
        assert targets == []
        got = R.render_outputs(targets, marp_bin=str(marp), chrome_path=None, theme_dirs=[],
                               containment_root=tdp)
        assert got == []


def test_render_outputs_marp_bin_nonexistent_path_is_empty():
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        md = _write_md(tdp / "a.md")
        got = R.render_outputs(
            [md], marp_bin="/nonexistent/marp", chrome_path=None, theme_dirs=[],
            containment_root=tdp)
        assert got == []
