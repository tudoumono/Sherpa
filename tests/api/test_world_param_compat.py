"""語彙統一（refactoring-plan フェーズ2・第2段・2026-07-13）の契約テスト。

第2段の契約: 旧 `version` パラメータの受理は終了した。`world` が唯一の API パラメータ。

「宣言は復活したが内部で無視する」型の回帰も含め、`version` が API surface に再宣言されて
いないことを固定する。宣言が無ければ FastAPI/Pydantic の既定動作により黙って無視される
（422 にもならない）ため、`version` の挙動側は surface の pin で足りる（この pin と
`tests/contract/test_openapi_contract.py` の openapi golden の二重で復活を検出する）。

`world` 省略時に既定 world（`worlds.default_world()`＝"v1"）へ解決されること自体は `version`
互換とは別の現役契約（`docs/03-鏡モデル.md` §9）のため、pin では代替できず挙動テストを残す。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(auth_disabled):
    from sherpa.api import app
    return TestClient(app, raise_server_exceptions=False)


def _patch_scope_tree(monkeypatch, seen: dict):
    from sherpa import api
    # フェーズ7-1（response_model 付与）: GET /scopes は `sc.ScopesResponse`（world/label/scopes 必須）を
    # 検証するようになったため、スタブも実形状に合わせる（この関数の関心は「どの world が渡ったか」の
    # 捕捉のみ・スタブの中身自体はテストのアサーション対象ではない）。
    monkeypatch.setattr(api.scope_mod, "scope_tree",
                        lambda w: (seen.__setitem__("world", w), {"world": w, "label": None, "scopes": []})[1])


def test_scopes_default_world_when_omitted(client, monkeypatch):
    """`world` 省略時に既定 world（`worlds.default_world()`＝"v1"）へ解決されることを固定する。
    `version` 復活防止の pin（`test_openapi_surface_has_no_version_parameter`）は
    `deps._resolve_world()` を実行しないため、この既定値解決の代替にはならない。"""
    seen: dict = {}
    _patch_scope_tree(monkeypatch, seen)
    r = client.get("/scopes")
    assert r.status_code == 200, r.text
    assert seen["world"] == "v1"             # 既定 world（default_world・挙動不変）


def test_openapi_surface_has_no_version_parameter():
    """OpenAPI スキーマ全体に `version` という query/path パラメータ・リクエストモデルのプロパティが
    存在しないことを pin する。「宣言は復活したが内部で無視する」型の回帰は挙動テストを通って
    しまうため、surface そのものを固定する（フェーズ2第2段 RV 所見）。"""
    from sherpa.api import app
    schema = app.openapi()
    offenders: list[str] = []
    for path, ops in (schema.get("paths") or {}).items():
        for method, op in ops.items():
            if not isinstance(op, dict):
                continue
            for p in (op.get("parameters") or []):
                if p.get("name") == "version":
                    offenders.append(f"{method.upper()} {path} (parameter)")
    for name, comp in ((schema.get("components") or {}).get("schemas") or {}).items():
        if "version" in (comp.get("properties") or {}):
            offenders.append(f"components.schemas.{name}.version")
    assert not offenders, "version が API surface に再宣言されています: " + ", ".join(offenders)
