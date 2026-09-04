"""語彙統一（refactoring-plan フェーズ2・第2段・2026-07-13）の契約テスト。

第2段の契約: 旧 `version` パラメータの受理は終了した。`world` が唯一の API パラメータ。

代表エンドポイントで次を確認する:
- 旧 `version=` を送っても**選択されない**（黙って無視される＝FastAPI/Pydantic の既定動作。422 にも
  ならない）。値を省略したのと同じ扱いになり、既定 world（`worlds.default_world()`＝"v1"）に解決される。
- `world=` は従来どおり効く（単独でも、`version` と同時指定でも `world` が使われる）。

第1段時代にあった deprecation 警告（logger "sherpa"）はもう出ない＝メカニズム自体を撤去済みなので、
本ファイルは警告の有無を検査しない。

依存を持ち込まないため、解決先（scope_tree / handle_message / run_impact）は monkeypatch で捕捉する。
"""
from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(auth_disabled):
    from sherpa.api import app
    return TestClient(app, raise_server_exceptions=False)


# ---- /scopes（クエリパラメータ）----

def _patch_scope_tree(monkeypatch, seen: dict):
    from sherpa import api
    # フェーズ7-1（response_model 付与）: GET /scopes は `sc.ScopesResponse`（world/label/scopes 必須）を
    # 検証するようになったため、スタブも実形状に合わせる（この関数の関心は「どの world が渡ったか」の
    # 捕捉のみ・スタブの中身自体はテストのアサーション対象ではない）。
    monkeypatch.setattr(api.scope_mod, "scope_tree",
                        lambda w: (seen.__setitem__("world", w), {"world": w, "label": None, "scopes": []})[1])


def test_scopes_ignores_legacy_version_uses_default(client, monkeypatch):
    seen: dict = {}
    _patch_scope_tree(monkeypatch, seen)
    # version=v9 は既定 world "v1" と異なる値。選択されればここに現れるはずだが、受理は終了済みなので
    # 無視され既定 world に解決される（挙動不変・v1 は worlds.default_world()）。
    r = client.get("/scopes", params={"version": "v9"})
    assert r.status_code == 200, r.text
    assert seen["world"] == "v1"             # version は無視され既定 world に解決される


def test_scopes_world_param_still_works(client, monkeypatch):
    seen: dict = {}
    _patch_scope_tree(monkeypatch, seen)
    r = client.get("/scopes", params={"world": "wA"})
    assert r.status_code == 200, r.text
    assert seen["world"] == "wA"             # world は従来どおり効く


def test_scopes_world_priority_ignoring_version(client, monkeypatch):
    seen: dict = {}
    _patch_scope_tree(monkeypatch, seen)
    r = client.get("/scopes", params={"world": "wA", "version": "wB"})
    assert r.status_code == 200, r.text
    assert seen["world"] == "wA"             # version は黙って無視される（wB は選択されない）


def test_scopes_default_world_when_omitted(client, monkeypatch):
    seen: dict = {}
    _patch_scope_tree(monkeypatch, seen)
    r = client.get("/scopes")
    assert r.status_code == 200, r.text
    assert seen["world"] == "v1"             # 既定 world（default_world・挙動不変）


# ---- /chat（リクエストボディ・knowledge=False は handle_message のみ）----

def test_chat_body_ignores_legacy_version_uses_default(client, monkeypatch):
    from sherpa.routers import chat as chat_routes
    seen: dict = {}

    def _fake_handle(session, message, world, **kw):
        seen["world"] = world
        return {"conversation_id": 1, "message": {"answer": {"lens": "chat"}}}

    # /chat は sherpa/routers/chat.py（フェーズ3スライス7）へ移動済み＝ハンドラは router 束縛の
    # handle_message を参照する（monkeypatch.setattr(api, ...) はもう届かない）。
    monkeypatch.setattr(chat_routes, "handle_message", _fake_handle)
    r = client.post("/chat", json={"message": "hello", "version": "v9"})
    assert r.status_code == 200, r.text
    assert seen["world"] == "v1"             # version は body の未宣言フィールドとして無視される


def test_chat_body_world_param_still_works(client, monkeypatch):
    from sherpa.routers import chat as chat_routes
    seen: dict = {}

    def _fake_handle(session, message, world, **kw):
        seen["world"] = world
        return {"conversation_id": 1, "message": {"answer": {"lens": "chat"}}}

    monkeypatch.setattr(chat_routes, "handle_message", _fake_handle)
    r = client.post("/chat", json={"message": "hello", "world": "wA", "version": "wB"})
    assert r.status_code == 200, r.text
    assert seen["world"] == "wA"             # world が使われ version は無視される


# ---- /impact/run（リクエストボディ・neo4j/run_impact は monkeypatch）----

def test_impact_run_ignores_legacy_version_uses_default(client, monkeypatch):
    from sherpa import api
    from sherpa.routers import impact
    seen: dict = {}

    @contextlib.contextmanager
    def _fake_session():
        yield None

    # impact_run は sherpa/routers/impact.py へ移動済み（フェーズ3スライス5）。api.py の再エクスポートは
    # `_analyses` 等の状態のみで validated_scope/neo4j_session/run_impact は router 束縛のため、
    # ここも router モジュール側を monkeypatch する（api 側を patch しても no-op になる）。
    monkeypatch.setattr(impact, "validated_scope", lambda world, sp: None)
    monkeypatch.setattr(impact, "neo4j_session", _fake_session)

    def _fake_impact(session, start, world, **kw):
        seen["world"] = world
        return {"items": [], "presumed": []}

    monkeypatch.setattr(impact, "run_impact", _fake_impact)
    r = client.post("/impact/run", json={"start": "X", "version": "v9"})
    assert r.status_code == 200, r.text
    assert seen["world"] == "v1"             # version は無視され既定 world に解決される


def test_impact_run_world_param_still_works(client, monkeypatch):
    from sherpa import api
    from sherpa.routers import impact
    seen: dict = {}

    @contextlib.contextmanager
    def _fake_session():
        yield None

    # impact_run は sherpa/routers/impact.py へ移動済み（フェーズ3スライス5）。router 束縛を patch する
    # （api 側の再エクスポートは _analyses 等の状態のみ・上のテストと同じ理由）。
    monkeypatch.setattr(impact, "validated_scope", lambda world, sp: None)
    monkeypatch.setattr(impact, "neo4j_session", _fake_session)

    def _fake_impact(session, start, world, **kw):
        seen["world"] = world
        return {"items": [], "presumed": []}

    monkeypatch.setattr(impact, "run_impact", _fake_impact)
    r = client.post("/impact/run", json={"start": "X", "world": "wA", "version": "wB"})
    assert r.status_code == 200, r.text
    assert seen["world"] == "wA"             # world が使われ version は無視される


# ---- API surface の pin（RV LOW: version の再宣言回帰を検出）----

def test_openapi_surface_has_no_version_parameter():
    """OpenAPI スキーマ全体に `version` という query/path パラメータ・リクエストモデルのプロパティが
    存在しないことを pin する。「宣言は復活したが内部で無視する」型の回帰は上の挙動テストを通って
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
