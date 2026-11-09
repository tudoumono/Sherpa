"""資料フォルダの中のフォルダ・ファイルに付ける「ビルドの定義を読む場所」の印（管理者の設定）。
保存先は `system_settings` の 1 キー（`build_read_marks`）で、`{資料フォルダ: [{"path", "mark"}]}` を JSON で持つ。
検索の「優先と除外」とは別の設定で、次の「今すぐ更新」でグラフだけを作り直して効かせる。守ること: 資料フォルダの解決範囲の設定（`resolve_settings`）に置かない。
設計: docs/design/settings.md「ビルドの定義を読む場所」
"""
from __future__ import annotations

from .ingest.build_link import MARK_READ, MARK_SKIP, MARKS, marks_signature
from .path_rules import MAX_RULES_PER_WORLD, PathRulesError, normalize_path

SETTINGS_KEY = "build_read_marks"


def validate_entries(entries) -> dict:
    """保存する印の検証（不正は `PathRulesError`）。戻りは `{パス: 印}`。"""
    if not isinstance(entries, (list, tuple)):
        raise PathRulesError("設定の形が不正です")
    if len(entries) > MAX_RULES_PER_WORLD:
        raise PathRulesError(f"設定は {MAX_RULES_PER_WORLD} 件までです")
    out: dict = {}
    for e in entries:
        if not isinstance(e, dict):
            raise PathRulesError("設定の形が不正です")
        path = normalize_path(e.get("path"))
        if e.get("mark") not in MARKS:
            raise PathRulesError("印の種類は「読む場所にする」か「読む場所にしない」です")
        if path in out:
            raise PathRulesError("同じ場所に設定が重複しています")
        out[path] = e["mark"]
    return out


def _parse(raw) -> dict:
    out: dict = {}
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict) and isinstance(e.get("path"), str) and e.get("mark") in MARKS:
            out[e["path"]] = e["mark"]
    return out


def load(world: str) -> dict:
    """資料フォルダの印 `{パス: 印}`。守ること: 読めないときは例外のまま伝える（印なしと区別する）。"""
    from . import store
    raw = store.get_system_settings().get(SETTINGS_KEY)
    return _parse(raw.get(world)) if isinstance(raw, dict) else {}


def save_world(uid, world: str, marks: dict) -> dict:
    """資料フォルダ 1 つぶんの印を保存する（読み取り・マージ・書き込みは 1 つのトランザクション）。"""
    from . import store

    def _merge(cur):
        allv = dict(cur) if isinstance(cur, dict) else {}
        if marks:
            allv[world] = [{"path": p, "mark": m} for p, m in sorted(marks.items())]
        else:
            allv.pop(world, None)
        return allv or None

    store.modify_system_setting(uid, SETTINGS_KEY, _merge)
    return dict(marks)
