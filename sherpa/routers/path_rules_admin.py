"""管理者の「資料の優先と除外」（`GET`/`PUT /admin/path-rules`・`GET /admin/path-rules/files`）。判定と保存は `sherpa/path_rules.py`。`sherpa.api` を import しない。
設計: docs/design/settings.md「優先と除外」
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from sherpa import build_marks, doc_ledger, path_rules, scope, scope_infer, worlds
from sherpa.ingest import build_link, importance, text_kind
from sherpa.deps import (
    SCOPE_WALK_TIMEOUT_MESSAGE,
    _WORLD_PATTERN,
    _current_user,
    _require_admin,
    _require_world,
    _resolve_world,
)

_log = logging.getLogger("sherpa")

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
path_rules_admin_router = APIRouter()

_FILES_MAX = 1000


class PathRuleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    kind: str
    outside_scope: bool = False
    target_type: str | None = None  # "file"／"folder"。保存のときにサーバが実物で確かめて入れる（申告は使わない）


class PathRulesPutReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    world: str | None = None
    rules: list[PathRuleItem] = []


class PathRuleFolder(BaseModel):
    path: str
    label: str
    depth: int
    count: int


class PathRulesResponse(BaseModel):
    world: str
    label: str
    rules: list[PathRuleItem]
    removed_missing: int
    folders: list[PathRuleFolder]


class PathRulesSaveResponse(BaseModel):
    world: str
    rules: list[PathRuleItem]
    dropped_children: list[PathRuleItem]


class PathRuleFilesResponse(BaseModel):
    folder: str
    files: list[str]
    truncated: bool
    total: int
    next_offset: int | None


def _walk_all_rels(world: str) -> list:
    """台帳が無いときの代わりの列挙。`scope._walk_rels` と同じ歩き方で、拡張子では絞らない。"""
    wd = worlds.world_dir(world)
    if not wd:
        return []
    return [rel for _rp, rel in scope_infer.safe_files(wd, strict=False, deadline=scope_infer.user_op_deadline(),
                                                       also=worlds.archives_dir(world))]


def _listing_rels(world: str) -> list:
    """画面の木・ファイルの一覧に出す資料のパス。台帳の資料を拡張子で落とさず（拡張子の無い資料・登録の無い拡張子の資料も設定できる）、
    検索の対象でないもの（秘匿名・重要度の設定ファイル）は検索と同じく出さない。台帳が無いときだけ資料フォルダを歩く。"""
    names = doc_ledger.ledger_rels(world)
    if names is None:
        names = _walk_all_rels(world)
    return [n for n in names if not text_kind.is_sensitive_doc_id(n) and not importance.is_importance_control_path(n)]


@path_rules_admin_router.get("/admin/path-rules", tags=["管理者:資料の優先と除外"],
                             response_model=PathRulesResponse)
def admin_path_rules_get(request: Request, world: str | None = Query(None, pattern=_WORLD_PATTERN)):
    """資料フォルダの優先・除外の設定とフォルダの木を返す（管理者のみ）。
    開いたときに、もう存在しない場所の設定を取り除いて保存し、取り除いた件数を `removed_missing` で返す（定期の走査はしない）。
    """
    u = _current_user(request)
    _require_admin(u)
    w = _resolve_world(world)
    _require_world(w)
    try:
        kept, gone = path_rules.prune_world(u["uid"], w)  # 最新を読み直して取り除く（同時の保存を旧い値で上書きしない）
    except path_rules.ExistenceUnknown as e:  # 実在を確かめられない場所がある: 取り除かず保存もしない（除外を外さない）
        raise HTTPException(503, "資料フォルダの中を確かめられませんでした。時間をおいてやり直してください") from e
    except Exception as e:
        raise HTTPException(503, "設定を読み書きできませんでした。時間をおいてやり直してください") from e
    try:
        rels = _listing_rels(w)
    except scope_infer.ScopeWalkDeadlineExceeded:
        raise HTTPException(503, SCOPE_WALK_TIMEOUT_MESSAGE) from None
    folders = [{"path": p, "label": label, "depth": depth, "count": count}
               for p, label, depth, count in scope._build_index(rels).scopes]
    return {"world": w, "label": worlds.world_label(w), "rules": [r.as_dict() for r in kept],
            "removed_missing": len(gone), "folders": folders}


@path_rules_admin_router.get("/admin/path-rules/files", tags=["管理者:資料の優先と除外"],
                             response_model=PathRuleFilesResponse)
def admin_path_rules_files(request: Request, world: str | None = Query(None, pattern=_WORLD_PATTERN),
                           folder: str = Query(""), offset: int = Query(0, ge=0),
                           limit: int = Query(_FILES_MAX, ge=1, le=_FILES_MAX)):
    """フォルダ直下のファイルの一覧（管理者のみ・名前順）。`folder` が空なら資料フォルダの直下。
    `offset`／`limit` でページ送りする（続きがあれば `next_offset`・無ければ null）。`total` は直下のファイルの総数。"""
    _require_admin(_current_user(request))
    w = _resolve_world(world)
    _require_world(w)
    try:
        base = path_rules.normalize_path(folder) if folder.strip().strip("/") else ""
    except path_rules.PathRulesError as e:
        raise HTTPException(422, str(e)) from e
    try:
        rels = _listing_rels(w)
    except scope_infer.ScopeWalkDeadlineExceeded:
        raise HTTPException(503, SCOPE_WALK_TIMEOUT_MESSAGE) from None
    pre = base + "/" if base else ""
    direct = sorted(r for r in rels if r.startswith(pre) and "/" not in r[len(pre):])
    page = direct[offset:offset + limit]
    nxt = offset + len(page) if offset + len(page) < len(direct) else None
    return {"folder": base, "files": page, "truncated": nxt is not None, "total": len(direct), "next_offset": nxt}


@path_rules_admin_router.put("/admin/path-rules", tags=["管理者:資料の優先と除外"],
                             response_model=PathRulesSaveResponse)
def admin_path_rules_put(req: PathRulesPutReq, request: Request):
    """資料フォルダ 1 つの優先・除外の設定を置き換えて保存する（管理者のみ・`system_settings` の監査に残る）。
    不正（`..`・絶対パス・種類・「範囲の外でも読む」を除外に付ける等）は 422。親に設定がある場所の配下の設定は保存の時点で落とし、落とした設定を返す。
    """
    u = _current_user(request)
    _require_admin(u)
    w = _resolve_world(req.world)
    _require_world(w)
    try:
        rules = path_rules.validate_entries([r.model_dump() for r in req.rules])
    except path_rules.PathRulesError as e:
        raise HTTPException(422, str(e)) from e
    try:
        path_rules.check_snapshot_size(rules)
    except path_rules.PathRulesError as e:
        raise HTTPException(422, str(e)) from e
    try:
        _, missing = path_rules.prune_missing(w, rules)
    except path_rules.ExistenceUnknown as e:
        raise HTTPException(503, "資料フォルダの中を確かめられませんでした。時間をおいてやり直してください") from e
    if missing:
        raise HTTPException(422, "資料フォルダの中に見つからない場所があります")
    kept, dropped = path_rules.collapse(path_rules.with_target_types(w, rules))
    try:
        path_rules.check_deny_budget(kept)  # Codex の deny が上限を超えて直読の準備が失敗しないように、最悪の件数で断る
    except path_rules.PathRulesError as e:
        raise HTTPException(422, str(e)) from e
    try:
        path_rules.save_world(u["uid"], w, kept)
    except Exception as e:
        _log.warning("path_rules: 保存に失敗しました: %s", type(e).__name__)
        raise HTTPException(503, "設定を保存できませんでした。時間をおいてやり直してください") from e
    return {"world": w, "rules": [r.as_dict() for r in kept], "dropped_children": [r.as_dict() for r in dropped]}


class BuildMarkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    mark: str


class BuildMarksPutReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    world: str | None = None
    marks: list[BuildMarkItem] = []


class BuildMarksResponse(BaseModel):
    world: str
    marks: list[BuildMarkItem]
    layout_folders: list[str]


@path_rules_admin_router.get("/admin/build-marks", tags=["管理者:資料の優先と除外"], response_model=BuildMarksResponse)
def admin_build_marks_get(request: Request, world: str | None = Query(None, pattern=_WORLD_PATTERN)):
    """ビルドの定義を読む場所の印と、置き方から読むフォルダ（ソースの組のフォルダ）を返す（管理者のみ）。"""
    _require_admin(_current_user(request))
    w = _resolve_world(world)
    _require_world(w)
    try:
        rels = _listing_rels(w)
    except scope_infer.ScopeWalkDeadlineExceeded:
        raise HTTPException(503, SCOPE_WALK_TIMEOUT_MESSAGE) from None
    try:
        marks = build_marks.load(w)
    except Exception as e:
        _log.warning("build_marks: 設定を読めません: %s", type(e).__name__)
        raise HTTPException(503, "設定を読み込めませんでした。時間をおいてやり直してください") from e
    return {"world": w, "marks": [{"path": p, "mark": m} for p, m in sorted(marks.items())],
            "layout_folders": build_link.source_group_dirs(rels)}


@path_rules_admin_router.put("/admin/build-marks", tags=["管理者:資料の優先と除外"], response_model=BuildMarksResponse)
def admin_build_marks_put(req: BuildMarksPutReq, request: Request):
    """ビルドの定義を読む場所の印を置き換えて保存する（管理者のみ・`system_settings` の監査に残る）。次の「今すぐ更新」でグラフだけを作り直して効かせる。
    不正な指定・資料フォルダに無い場所は 422。"""
    u = _current_user(request)
    _require_admin(u)
    w = _resolve_world(req.world)
    _require_world(w)
    try:
        marks = build_marks.validate_entries([m.model_dump() for m in req.marks])
    except path_rules.PathRulesError as e:
        raise HTTPException(422, str(e)) from e
    try:
        _, missing = path_rules.prune_missing(w, [path_rules.Rule(p, path_rules.KIND_EXCLUDE) for p in marks])
    except path_rules.ExistenceUnknown as e:
        raise HTTPException(503, "資料フォルダの中を確かめられませんでした。時間をおいてやり直してください") from e
    if missing:
        raise HTTPException(422, "資料フォルダの中に見つからない場所があります")
    try:
        build_marks.save_world(u["uid"], w, marks)
    except Exception as e:
        _log.warning("build_marks: 保存に失敗しました: %s", type(e).__name__)
        raise HTTPException(503, "設定を保存できませんでした。時間をおいてやり直してください") from e
    return {"world": w, "marks": [{"path": p, "mark": m} for p, m in sorted(marks.items())], "layout_folders": []}
