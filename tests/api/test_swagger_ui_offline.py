"""Swagger UI（/docs）が閉域LAN（外部ネットワーク到達不可）でも描画できることを固定する。

`sherpa/api.py` の /docs は web/vendor/ 同梱の資産だけで描画し、外部ホストへの参照を一切持たない
（swagger-ui-bundle.js・swagger-ui.css・favicon はすべて自ホスト配信・検証バッジも無効化）。
ReDoc（/redoc）は提供しない＝ルート自体が存在しない（利用者向け契約は Swagger UI のみ）。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from sherpa.api import app

client = TestClient(app)

# 応答 HTML に含まれてはならない外部ホスト（CDN・検証バッジ・既定 favicon）。
_FORBIDDEN_HOSTS = ("cdn.jsdelivr.net", "fastapi.tiangolo.com", "validator.swagger.io")


def test_docs_html_has_no_external_urls():
    r = client.get("/docs")
    assert r.status_code == 200
    body = r.text
    assert "http://" not in body and "https://" not in body, (
        "GET /docs の HTML に絶対URL（外部参照の疑い）が含まれている: "
        + body
    )
    for host in _FORBIDDEN_HOSTS:
        assert host not in body, f"GET /docs の HTML に外部ホストへの参照が残っている: {host}"
    # 自前資産（/ui/vendor 配下）を指していること。
    assert "/ui/vendor/swagger-ui-bundle.js" in body
    assert "/ui/vendor/swagger-ui.css" in body
    assert "/ui/vendor/swagger-ui-favicon.png" in body
    # 検証バッジ（validator.swagger.io への外部問い合わせ）を明示的に無効化していること。
    assert '"validatorUrl": null' in body


def test_docs_vendored_assets_are_served():
    js = client.get("/ui/vendor/swagger-ui-bundle.js")
    assert js.status_code == 200
    assert "SwaggerUIBundle" in js.text
    css = client.get("/ui/vendor/swagger-ui.css")
    assert css.status_code == 200
    favicon = client.get("/ui/vendor/swagger-ui-favicon.png")
    assert favicon.status_code == 200


def test_docs_oauth2_redirect_is_self_contained():
    """/docs/oauth2-redirect はコード埋め込みの HTML で、外部URLを持たない。"""
    r = client.get("/docs/oauth2-redirect")
    assert r.status_code == 200
    assert "http://" not in r.text and "https://" not in r.text


def test_redoc_route_does_not_exist():
    """ReDoc（/redoc）は提供しない（利用者向け API 仕様書の導線は Swagger UI のみ）。"""
    r = client.get("/redoc")
    assert r.status_code == 404
