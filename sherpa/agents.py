"""思考プロバイダ抽象の再エクスポート facade。

実体は `sherpa/providers/` パッケージ（prompts / base / simple / codex / registry）。このファイルは docstring と
re-export のみでロジックを持たない。製品コードが `sherpa.agents` から使う名前と、providers が実行時に
`agents.X`（`_gather`・`SimpleProvider`・`CodexProvider`・`_UnwiredProvider`）として引く差し替えの口だけを残す。
それ以外は `from sherpa.providers... import ...` の定義元から直 import する（`tests/unit/test_agents_surface.py` が公開名を固定）。

思考イベント:
- `{"type":"node","id","kind":"think|tool","label","detail","status":"active|done"}`
- `{"type":"_result","env":<answer envelope>,"decision":<route decision>}`
"""
from __future__ import annotations

from .providers.base import Ctx, _gather  # noqa: F401 -- facade 再エクスポート（`_gather` は providers が実行時にここから引く）
from .providers.simple import SimpleProvider  # noqa: F401 -- facade 再エクスポート（providers が実行時にここから引く）
from .providers.codex.sandbox import (  # noqa: F401 -- facade 再エクスポート
    _codex_sandbox_enabled,
    _web_search_admin_allowed,
)
from .providers.codex.provider import CodexProvider  # noqa: F401 -- facade 再エクスポート
from .providers import (  # noqa: F401 -- facade 再エクスポート
    AGENT_PROVIDERS,
    _UnwiredProvider,
    get_provider,
    provider_info,
)
