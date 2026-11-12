"""外部連携 API（`/ext/v1`）: APIキー認証基盤・決定的変換・エンジン分離検索＋RRF融合・discovery・原本取得・キーの world スコープ。

キー認証系（convert・search・answer・Codex ジョブ・capabilities・doc・openapi）は `router` に集約し、`api.py` から include する。
admin キー発行/失効はセッション Cookie 認証が必要なため `sherpa/routers/system_extras.py` に置き、監査は `start_audit()` を通じて
`ExtRequestMiddleware` の request-level 監査へ統合する。キー単位のレート制限は `auth._verify_key_sync` 内の `ratelimit.check_ext_api_rate_limit` が行う。
このパッケージは `sherpa.api`・`sherpa.agents`・`sherpa.chat_service`・`sherpa.chat_router`・`sherpa.grep_tool` を import しない
（循環回避＋共有KBのみの契約）。

- convert は stateless（`office_md.to_markdown` の決定的変換のみ）。KB・台帳・ES・Neo4j へ書き込まず、一時ファイルは必ず削除する。LLM は呼ばない。
- search は `sherpa.parts.read.fused_search`（keyword=ES BM25 / vector=ES 純kNN / graph=Neo4j 影響たどり）へ委譲する。エンジン単位の不可は
  200＋`degraded[]`。world 解決は `worlds.resolve_external_world` で1回だけ行い、その `root` を引き回す（preflight 後の再解決禁止）。
- doc（原本DL）は world root を起点に `sherpa.safe_open` の symlink 差し替え耐性 open で得た fd 1本だけを使い、検証から配信まで行う。
  個人 workspace は world root の外で解決対象にならない。legacy Office（.doc/.xls/.ppt）は CFB ヘッダの健全性のみ検証する。
- `ExtRequestMiddleware`（生 ASGI ミドルウェア・`/ext/v1/*` のみ）が X-Request-Id の解決と応答ヘッダ付与、アプリログへの request_id 束縛、
  認証成功後の1リクエスト=1行監査（専用 writer スレッド経由）を行う。

ルートの登録順＝OpenAPI のパスの並び。下の import の順序（convert → search → answer → codex_jobs → capabilities → doc → openapi）を変えない。
設計: docs/design/external-api.md「共通の約束」
"""
from __future__ import annotations

from sherpa.ext.audit import AUDIT_OUTBOX_KIND, start_audit
from sherpa.ext.auth import require_api_key
from sherpa.ext.common import router
from sherpa.ext.middleware import ExtRequestMiddleware

# ルートの登録（import の副作用）。順序は OpenAPI の並びを決める
from sherpa.ext import convert, search, answer, codex_jobs, capabilities, doc, openapi  # noqa: E402,F401,I001

__all__ = ["AUDIT_OUTBOX_KIND", "ExtRequestMiddleware", "require_api_key", "router", "start_audit"]
