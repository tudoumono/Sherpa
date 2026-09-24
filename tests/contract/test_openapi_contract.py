"""OpenAPI 契約の固定（フェーズ7-1・docs/proposals/2026-07-02-リファクタリング計画.md フェーズ7 作業項目1）。

`sherpa.api.app.openapi()`（`GET /openapi.json` と同じ生成物）を golden 化する。`tests/api/test_route_snapshot.py`
（ルート表の定義順固定）と同じ思想で、こちらは**各ルートの入出力スキーマ**（response_model 付与状況・
リクエストボディの型・必須/任意）まで含めて固定する。response_model 付与（フェーズ7-1）や
リクエスト/応答モデルの変更が golden 差分として必ず可視化される。

決定性: `app.openapi()` は `app.openapi_schema` にキャッシュされるため同一プロセス内では同じ辞書を返す。
別プロセスでも再現することを実測済み（2回連続実行で同一 SHA-256）。

正規化: 生成物をそのまま golden にすると差分が読みにくいため、`json.dumps(..., indent=2, sort_keys=True)`
で整形＝キー順を辞書順に固定してから比較する（dict の insertion 順は意味を持たない＝ソートしても
情報は失われない。実体の差分＝キーの増減・型変更だけが golden diff に出る）。

外部サービス不要（DB 接続なし・ルート定義とモデルの型注釈だけから構築される）。

golden の更新手順（**意図した変更のときだけ**実行し、diff を目視確認してからコミットする）:

    SHERPA_USE_FIXTURES=1 PYTHONPATH=. .venv/bin/python -c \
        "import sys; sys.path.insert(0, 'tests/contract'); \
         import test_openapi_contract as t; t._write_golden()"
"""
from __future__ import annotations

import json
import pathlib

_GOLDEN = pathlib.Path(__file__).resolve().parent / "goldens" / "openapi.json"


def _normalized_schema() -> str:
    from sherpa.api import app

    schema = app.openapi()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_golden() -> None:
    """現状の app.openapi() から golden を書き出す（更新手順は module docstring 参照）。"""
    _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    _GOLDEN.write_text(_normalized_schema(), encoding="utf-8")


def test_openapi_schema_matches_golden():
    """OpenAPI 契約（ルートの入出力スキーマ）が golden と完全一致する。"""
    assert _GOLDEN.exists(), f"golden 未生成: {_GOLDEN}（module docstring の手順で作成）"
    expected = _GOLDEN.read_text(encoding="utf-8")
    actual = _normalized_schema()
    assert actual == expected, (
        "OpenAPI 契約が golden と不一致。意図した変更（response_model 追加・リクエスト/応答モデル変更等）"
        "なら module docstring の手順で golden を更新すること。"
    )


def test_openapi_schema_is_deterministic():
    """同一コードから2回生成しても同一（フェーズ7-1 受け入れ基準6: 2回連続実行で同一）。

    `app.openapi()` はプロセス内キャッシュを持つため、キャッシュを明示的にクリアして
    再生成した結果を突合する（真に2回目の構築が同じになることを確認する）。
    """
    from sherpa.api import app

    first = _normalized_schema()
    app.openapi_schema = None   # FastAPI の内部キャッシュを落として再構築させる
    second = _normalized_schema()
    assert first == second
