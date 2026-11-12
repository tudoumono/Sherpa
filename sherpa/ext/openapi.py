"""GET /ext/v1/openapi.json: `/ext/v1` 配下だけを抜いた OpenAPI 文書。
設計: docs/design/external-api.md
"""
from __future__ import annotations

from fastapi import Depends, Request

from sherpa.ext.audit import _AuditScope
from sherpa.ext.auth import require_api_key
from sherpa.ext.common import router


def _ext_openapi_subset(app) -> dict:
    """`app.openapi()` から `/ext/v1` 配下（admin・利用者キー管理を除く）のパスと、そこから到達可能な `components.schemas` だけを抜いた OpenAPI 文書。
    Dify カスタムツールに直接インポート可能。X-API-Key 認証のルート（convert/search/capabilities/doc/answer・Codex ジョブ受付/状態/結果/取消の4本）のみを含める。
    """
    full = app.openapi()
    paths = {p: v for p, v in full.get("paths", {}).items()
             if p.startswith("/ext/v1/") and not p.startswith("/ext/v1/admin")
             and not p.startswith("/ext/v1/keys")
             and p != "/ext/v1/openapi.json"}
    schemas = (full.get("components") or {}).get("schemas") or {}

    def _collect(obj, out: set):
        if isinstance(obj, dict):
            r = obj.get("$ref")
            if isinstance(r, str) and r.startswith("#/components/schemas/"):
                out.add(r.rsplit("/", 1)[1])
            for v in obj.values():
                _collect(v, out)
        elif isinstance(obj, list):
            for v in obj:
                _collect(v, out)

    need: set = set()
    _collect(paths, need)
    while True:
        more: set = set()
        for name in need:
            _collect(schemas.get(name, {}), more)
        if more <= need:
            break
        need |= more
    return {
        "openapi": full["openapi"],
        "info": {"title": "Slouch External API", "version": "1.0.0",
                 "description": "MD変換・RAG検索の外部連携 API（X-API-Key 認証）"},
        "paths": paths,
        "components": {
            "schemas": {n: schemas[n] for n in sorted(need) if n in schemas},
            "securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
        },
        "security": [{"ApiKeyAuth": []}],
    }


@router.get("/openapi.json", include_in_schema=False)
def ext_openapi(request: Request, key: dict = Depends(require_api_key)):
    with _AuditScope(request, "ext_api.openapi", "ext_openapi") as audit:
        doc = _ext_openapi_subset(request.app)
        audit.detail["result_count"] = len(doc.get("paths") or {})
        return doc
