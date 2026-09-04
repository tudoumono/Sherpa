"""Codex authoring 実行前に authoring/.agents/skills へ配備するスキル
（Codex 強化計画 Phase1・P1-b・§5c 決定＝案A′ ベース＋個人オーバーレイ）。

ベーススキル（`sherpa/skills_base/{xlsx,docx,pptx}/`・Sherpa 管理・自作・リポジトリ管理の READ-ONLY 原本）
＋ 個人スキル（`users/{uid}/workspace/skills/<name>/`・本人のみ書込）を、Codex 実行の直前に
`authoring/.agents/skills/` へ**毎回作り直し**でコピーする。同名スキルは個人が base を置換する
（"各自でブラッシュアップできないのは問題" という FB を踏まえた設計）。

symlink は一切追従しない（読み取り側で fail-closed・Codex 実行の封じ込め recipe の一部）。
knowledge=ON の Codex 実行**全部**で配備する（author レンズに限定しない＝progressive disclosure で
軽量・description に一致しなければ Codex はスキル本文を読まないため他レンズへの悪影響は無い）。

呼び出し側で try/except すること（AGENTS.md 同様ベストエフォート・fail-open。配備に失敗しても
プロンプト側の指示だけで Codex 実行自体は継続してよい）。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

_log = logging.getLogger("sherpa")

# ベーススキル原本（リポジトリ管理・自作＝外部スキルのコピー禁止）。
BASE_SKILLS_DIR = Path(__file__).resolve().parent / "skills_base"


def _has_symlink_inside(root: Path) -> bool:
    """root 配下（root 自身は含まない）に symlink が1つでもあれば True。"""
    try:
        return any(p.is_symlink() for p in root.rglob("*"))
    except OSError:
        return True   # 走査自体に失敗＝安全側（symlink ありとみなして拒否）


def _copy_skill_dir(src: Path, dst: Path) -> bool:
    """1スキル分を src → dst へコピー。src 自身または配下に symlink があれば拒否（fail-closed）。"""
    if src.is_symlink() or not src.is_dir():
        _log.warning("codex_skills: skip non-dir/symlink skill source: %s", src)
        return False
    if _has_symlink_inside(src):
        _log.warning("codex_skills: symlink inside skill source rejected: %s", src)
        return False
    try:
        shutil.copytree(src, dst)
        return True
    except Exception as e:
        _log.warning("codex_skills: copy failed for %s -> %s: %s", src, dst, e)
        return False


def deploy_skills(authoring: Path, uid: str, users_dir: Path) -> None:
    """authoring/.agents/skills を毎回作り直し、base→個人オーバーレイの順で配備する。

    `authoring` は Codex の cwd（既存の封じ込め recipe そのまま・sandbox profile 変更ゼロ）。
    """
    # RV HIGH: 親 `.agents` 自体の symlink も拒否する。Codex は authoring に書けるため、前回実行で
    # `.agents -> ../files` 等にすり替えられていると、下の rmtree/copytree が authoring 外
    # （ユーザーデータ）を削除・書込してしまう。symlink なら unlink して実ディレクトリで作り直す。
    agents_dir = authoring / ".agents"
    if agents_dir.is_symlink():
        agents_dir.unlink()
    dest_root = agents_dir / "skills"
    if dest_root.is_symlink():
        dest_root.unlink()
    elif dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    dest_root.mkdir(parents=True, exist_ok=True)

    # ベース（リポジトリ管理・自作）を先に配備。
    if BASE_SKILLS_DIR.is_dir():
        for base_skill in sorted(p for p in BASE_SKILLS_DIR.iterdir() if p.is_dir()):
            _copy_skill_dir(base_skill, dest_root / base_skill.name)

    # 個人オーバーレイ（同名スキルは個人が置換）。存在しなくても正常（個人スキル未作成が既定）。
    personal_root = users_dir / uid / "workspace" / "skills"
    if personal_root.is_symlink() or not personal_root.is_dir():
        return
    for personal_skill in sorted(p for p in personal_root.iterdir() if p.is_dir()):
        dst = dest_root / personal_skill.name
        if dst.exists() or dst.is_symlink():
            shutil.rmtree(dst, ignore_errors=True) if not dst.is_symlink() else dst.unlink()
        _copy_skill_dir(personal_skill, dst)
