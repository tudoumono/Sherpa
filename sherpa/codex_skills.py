"""Codex 実行前に authoring/.agents/skills へスキルを配備する。

ベーススキル（`sherpa/skills_base/{xlsx,docx,pptx}/`）＋個人スキル（`users/{uid}/workspace/skills/<name>/`）を、
実行の直前に毎回作り直しでコピーする（同名は個人が置換）。symlink は一切追従しない。
呼び出し側で try/except すること（配備に失敗しても Codex 実行は継続してよい）。
設計: docs/design/codex.md「実行の構成」
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

_log = logging.getLogger("sherpa")

# ベーススキル原本（リポジトリ管理・READ-ONLY）
BASE_SKILLS_DIR = Path(__file__).resolve().parent / "skills_base"


def _has_symlink_inside(root: Path) -> bool:
    """root 配下（root 自身は含まない）に symlink が1つでもあれば True。"""
    try:
        return any(p.is_symlink() for p in root.rglob("*"))
    except OSError:
        return True


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


def deploy_skills(authoring: Path, uid: str, users_dir: Path, *, skip_prefix: str | None = None) -> None:
    """authoring/.agents/skills を毎回作り直し、base→個人オーバーレイの順で配備する。

    `skip_prefix` を指定すると、その名前で始まるスキル（base・個人とも）を配備しない。
    """
    # 親 `.agents` 自体の symlink も拒否する（authoring 外の削除・書込を防ぐ）。symlink なら unlink して作り直す
    agents_dir = authoring / ".agents"
    if agents_dir.is_symlink():
        agents_dir.unlink()
    dest_root = agents_dir / "skills"
    if dest_root.is_symlink():
        dest_root.unlink()
    elif dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    dest_root.mkdir(parents=True, exist_ok=True)

    def _skip(name: str) -> bool:
        return skip_prefix is not None and name.startswith(skip_prefix)

    # ベースを先に配備
    if BASE_SKILLS_DIR.is_dir():
        for base_skill in sorted(p for p in BASE_SKILLS_DIR.iterdir() if p.is_dir()):
            if _skip(base_skill.name):
                continue
            _copy_skill_dir(base_skill, dest_root / base_skill.name)

    # 個人オーバーレイ（同名は個人が置換。無くても正常）
    personal_root = users_dir / uid / "workspace" / "skills"
    if personal_root.is_symlink() or not personal_root.is_dir():
        return
    for personal_skill in sorted(p for p in personal_root.iterdir() if p.is_dir()):
        if _skip(personal_skill.name):
            continue
        dst = dest_root / personal_skill.name
        if dst.exists() or dst.is_symlink():
            shutil.rmtree(dst, ignore_errors=True) if not dst.is_symlink() else dst.unlink()
        _copy_skill_dir(personal_skill, dst)
