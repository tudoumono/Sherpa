"""原本（登録ディレクトリ）と派生物・観測の置き場が重ならないことの検査。
設計: docs/design/scope.md「資料フォルダの登録・解決・付け替え（registry）」
"""
from __future__ import annotations

from pathlib import Path

from . import worlds


def derived_base_dir() -> Path:
    """派生領域のベース（全資料フォルダの `derived_dir` の親）。"""
    return worlds.derived_dir("_").parent


def source_overlap_reason(root: str | Path) -> str | None:
    """原本の候補 `root` が派生・観測の置き場と同一／祖先／子孫なら、利用者へ返せる理由を返す。重ならなければ None。
    パスは実体（symlink を解いた形）で比べ、判定できないときは重なりありとして扱う。
    """
    for base, name in ((derived_base_dir(), "派生物"), (worlds.observation_base_dir(), "OCR 観測")):
        if worlds._fs_overlap(base, Path(root)):
            return (f"そのフォルダはアプリが{name}を置く場所と重なっているため、登録できません"
                    "（資料の置き場とは別の場所を選んでください）")
    return None


def assert_derived_removable(world_id: str) -> None:
    """派生物を消す前に呼ぶ。消す木が登録済みの原本の置き場と重なるなら ValueError にして何も消させない。"""
    from . import store

    target = worlds.derived_dir(world_id)
    for row in store.list_worlds_db():
        root_path = row.get("root_path") if isinstance(row, dict) else None
        if not isinstance(root_path, str) or not root_path:
            raise ValueError("登録済みWorld参照元を安全に検証できません")
        if worlds._fs_overlap(target, Path(root_path)):
            raise ValueError("派生物の置き場が原本の置き場と重なるため削除できません")
