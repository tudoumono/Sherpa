"""M2/M3（proposals/2026-07-07-Marpスライド作成.md）単体テスト。

marp スライド作成スキル本体（SKILL.md＋テーマ）・配備（themes/ 込み）・プロンプト/AGENTS.md の
使い分け文言・marp CLI／Chromium の存在検出ヘルパ（`_marp_bin`/`_detect_chrome_path`）を確認する。
M3 案2で Codex は .md を書くだけになり、実際のレンダ（unshare 隔離下の marp 実行）は
`sherpa/marp_render.py` に分離された（そちらの単体テストは tests/unit/test_marp_render.py）。
実 codex CLI 起動は対象外（E2E はコーディネーターが後で実施）。
"""
from __future__ import annotations

import os
import pathlib
import stat
import tempfile

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents as A          # noqa: E402
from sherpa import codex_skills as S    # noqa: E402
from sherpa import codex_agents_md as M  # noqa: E402


def _mk(d: pathlib.Path) -> pathlib.Path:
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_exec(p: pathlib.Path) -> pathlib.Path:
    """実行ビット付きのダミーファイルを作る（_marp_bin の os.access(X_OK) 用）。"""
    p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


# ===== SKILL.md 本体（体裁・使い分け・レンダコマンド・成果物規約） =====

def test_marp_skill_exists_with_frontmatter():
    p = S.BASE_SKILLS_DIR / "marp" / "SKILL.md"
    assert p.is_file(), "marp/SKILL.md が無い"
    txt = p.read_text(encoding="utf-8")
    assert txt.startswith("---\n"), "frontmatter が無い"
    lines = txt.splitlines()
    assert lines[1] == "name: marp-slides-japanese", "name が想定と違う"
    assert any(l.startswith("description:") for l in lines[:6]), "description が無い"


def test_marp_skill_description_declares_pptx_fallback():
    """description に『見た目重視』『HTML/PDF/PPTX』と『PowerPoint 編集なら pptx スキル』の使い分けがある。"""
    txt = (S.BASE_SKILLS_DIR / "marp" / "SKILL.md").read_text(encoding="utf-8")
    desc = next(l for l in txt.splitlines() if l.startswith("description:"))
    assert "見た目重視" in desc
    assert "PPTX" in desc and "HTML" in desc and "PDF" in desc
    assert "pptx" in desc and "PowerPoint" in desc      # 編集したいなら pptx スキルへ


def test_marp_skill_body_has_frontmatter_type_and_theme():
    txt = (S.BASE_SKILLS_DIR / "marp" / "SKILL.md").read_text(encoding="utf-8")
    for token in ("marp: true", "theme: sherpa", "paginate: true"):
        assert token in txt, f"front-matter の型に {token!r} が無い"
    assert "画像ベース" in txt, "PPTX が画像ベース＝本文編集不可の明記が無い"


def test_marp_skill_render_is_delegated_to_sherpa():
    """M3 案2: SKILL.md は「Codex は .md を書くだけ・レンダは Sherpa 側が自動実行」を明記する
    （Codex 自身がサンドボックス内で marp CLI を呼ぶ旧レンダコマンド節は撤去済み）。"""
    txt = (S.BASE_SKILLS_DIR / "marp" / "SKILL.md").read_text(encoding="utf-8")
    assert "Sherpa" in txt and "自動" in txt
    assert '"$SHERPA_MARP_BIN"' not in txt              # 旧レンダコマンドの直接呼び出しは残さない
    assert "marp が導入されていない場合" in txt or "marp が未導入" in txt or "導入されていない" in txt


def test_marp_skill_keeps_md_source_as_artifact():
    txt = (S.BASE_SKILLS_DIR / "marp" / "SKILL.md").read_text(encoding="utf-8")
    assert "Marp ソース" in txt and "成果物として残す" in txt


def test_marp_skill_common_rules_inherited():
    """既存スキルと同じ共通ルール（authoring 直下・ネットワーク不可・完了報告）を踏襲する。"""
    txt = (S.BASE_SKILLS_DIR / "marp" / "SKILL.md").read_text(encoding="utf-8")
    for phrase in ("authoring 直下", "ネットワーク", "報告する", "推測で埋めない"):
        assert phrase in txt, f"共通ルール文言が無い: {phrase!r}"


# ===== テーマ CSS =====

def test_marp_theme_exists_and_is_marp_theme():
    css = S.BASE_SKILLS_DIR / "marp" / "themes" / "sherpa.css"
    assert css.is_file(), "themes/sherpa.css が無い"
    txt = css.read_text(encoding="utf-8")
    assert txt.startswith("/* @theme sherpa */"), "Marp テーマ宣言（/* @theme sherpa */）が先頭に無い"
    # 日本語フォントのフォールバック指定（RUNTIME-SANDBOX §9 の前提）。
    assert "Noto Sans CJK JP" in txt and "IPAGothic" in txt
    # Sherpa の teal アクセント（app.css 由来）を使っている。
    assert "#0d9488" in txt or "#0f766e" in txt


# ===== deploy_skills が marp と themes/ を配備する =====

def test_deploy_skills_includes_marp_with_themes():
    with tempfile.TemporaryDirectory() as td:
        authoring = _mk(pathlib.Path(td) / "authoring")
        users_dir = _mk(pathlib.Path(td) / "users")
        S.deploy_skills(authoring, "u1", users_dir)
        dest = authoring / ".agents" / "skills" / "marp"
        assert (dest / "SKILL.md").is_file(), "marp SKILL.md が配備されていない"
        assert (dest / "themes" / "sherpa.css").is_file(), "themes/ サブディレクトリが配備されていない"


# ===== プロンプト／AGENTS.md の使い分け文言 =====

def test_agents_md_has_marp_vs_pptx_distinction():
    txt = M.AGENTS_MD
    assert "marp" in txt, "AGENTS.md に marp の言及が無い"
    assert "python-pptx" in txt, "AGENTS.md に pptx フォールバックの言及が無い"
    assert "自動" in txt, "AGENTS.md にレンダが Sherpa 側自動である旨の言及が無い"
    assert "PowerPoint" in txt


def test_prompt_mcp_author_branch_has_marp_distinction():
    p = A.CodexProvider()
    prompt = p._prompt_mcp("消費税率変更の説明スライドを作って", "author", "v1")
    assert "marp" in prompt, "author プロンプトに marp の言及が無い"
    assert "python-pptx" in prompt
    assert "自動" in prompt, "author プロンプトにレンダが Sherpa 側自動である旨の言及が無い"


def test_prompt_mcp_nonauthor_branch_has_no_marp_noise():
    """非 author（qa 等）レンズには marp の作成指示は混ぜない（既存の QA 挙動を汚さない）。"""
    p = A.CodexProvider()
    prompt = p._prompt_mcp("税率マスタは何件ある？", "qa", "v1")
    assert "marp スキル" not in prompt


# ===== env 配線ヘルパ: _marp_bin =====

def test_marp_bin_override_executable(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        exe = _make_exec(pathlib.Path(td) / "marp")
        monkeypatch.setenv("SHERPA_MARP_BIN", str(exe))
        assert A._marp_bin() == str(exe)


def test_marp_bin_override_nonexecutable_is_none(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        plain = pathlib.Path(td) / "marp"
        plain.write_text("x", encoding="utf-8")          # 実行ビット無し
        monkeypatch.setenv("SHERPA_MARP_BIN", str(plain))
        assert A._marp_bin() is None


def test_marp_bin_override_missing_is_none(monkeypatch):
    monkeypatch.setenv("SHERPA_MARP_BIN", "/nonexistent/marp")
    assert A._marp_bin() is None


def test_marp_bin_autodetect_repo_or_none(monkeypatch):
    """override 無し: リポジトリの tools/marp があればそれ、無ければ None（環境非依存に頑健化）。"""
    monkeypatch.delenv("SHERPA_MARP_BIN", raising=False)
    r = A._marp_bin()
    assert r is None or r.endswith("tools/marp/node_modules/.bin/marp")


# ===== env 配線ヘルパ: _detect_chrome_path =====

def test_chrome_path_env_honored(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        chrome = pathlib.Path(td) / "chrome"
        chrome.write_text("x", encoding="utf-8")
        chrome.chmod(0o755)                                       # RV Med: 実行ビット必須
        monkeypatch.setenv("CHROME_PATH", str(chrome))
        assert A._detect_chrome_path() == str(chrome)


def test_chromium_path_env_honored_when_chrome_unset(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        chrome = pathlib.Path(td) / "chrome"
        chrome.write_text("x", encoding="utf-8")
        chrome.chmod(0o755)
        monkeypatch.delenv("CHROME_PATH", raising=False)
        monkeypatch.setenv("CHROMIUM_PATH", str(chrome))
        assert A._detect_chrome_path() == str(chrome)


def test_chrome_path_non_executable_rejected(monkeypatch):
    """RV Med（2026-07-08）: 非実行ファイルの CHROME_PATH は無視（Puppeteer の EACCES 失敗を防ぐ）。"""
    with tempfile.TemporaryDirectory() as td:
        chrome = pathlib.Path(td) / "chrome"
        chrome.write_text("x", encoding="utf-8")
        chrome.chmod(0o644)                                       # 実行不可
        monkeypatch.setenv("CHROME_PATH", str(chrome))
        monkeypatch.setenv("HOME", td)                            # playwright 自動検出も無効化
        assert A._detect_chrome_path() is None


def test_marp_bin_override_relative_path_absolutized(monkeypatch):
    """RV Med（2026-07-08）: 相対パス override は絶対化して返す（Popen(cwd=authoring) の誤解釈防止）。"""
    with tempfile.TemporaryDirectory() as td:
        marp = pathlib.Path(td) / "marp"
        marp.write_text("#!/bin/sh\n", encoding="utf-8")
        marp.chmod(0o755)
        monkeypatch.chdir(td)
        monkeypatch.setenv("SHERPA_MARP_BIN", "marp")             # 相対指定
        r = A._marp_bin()
        assert r is not None and pathlib.Path(r).is_absolute()
        assert pathlib.Path(r).samefile(marp)


def test_chrome_path_autodetect_picks_latest_playwright(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        home = pathlib.Path(td)
        monkeypatch.delenv("CHROME_PATH", raising=False)
        monkeypatch.delenv("CHROMIUM_PATH", raising=False)
        monkeypatch.setenv("HOME", str(home))
        # 数値サフィックスが大きい方（1300）を選ぶ（文字列比較だと 999 が勝ってしまう罠）。
        for ver in ("chromium-999", "chromium-1300"):
            d = _mk(home / ".cache" / "ms-playwright" / ver / "chrome-linux64")
            _make_exec(d / "chrome")
        got = A._detect_chrome_path()
        assert got is not None and got.endswith("chromium-1300/chrome-linux64/chrome")


def test_chrome_path_none_when_nothing(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.delenv("CHROME_PATH", raising=False)
        monkeypatch.delenv("CHROMIUM_PATH", raising=False)
        monkeypatch.setenv("HOME", str(td))              # 空 HOME＝Playwright chromium 無し
        assert A._detect_chrome_path() is None

