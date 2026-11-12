"""資料フォルダの解決範囲の設定（GET・PUT /worlds/{wid}/resolve-settings）の要求・応答スキーマ。`sherpa.schemas` から再公開する。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ResolveSettingsReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    copy_paths: list[str] = []
    path_aliases: dict[str, str] = {}
    name_rules: list[dict[str, str]] = []


class WorldResolveSettingsResponse(BaseModel):
    """資料フォルダの解決範囲の設定（GET/PUT /worlds/{wid}/resolve-settings）。

    `copy_paths`＝COPY の取り込み元の場所（優先順・資料フォルダの root からの相対パス）。`path_aliases`＝パスの別名 → 場所。`name_rules`＝名前の対応（順序付きの `{from, to}`）。
    `warnings` は資料フォルダの中に見つからない場所（保存は止めない）。`changed` は PUT で内容が変わったか
    （変わると資料フォルダ全体を取り込み直す）。`refresh_started` は PUT でその取り込み直しを起こせたか（false で changed なら別の処理が実行中＝更新待ち）。GET では常に false。
    """
    ok: bool
    world_id: str
    copy_paths: list[str]
    path_aliases: dict[str, str]
    name_rules: list[dict[str, str]]
    warnings: list[str]
    changed: bool
    refresh_started: bool
    note: str
