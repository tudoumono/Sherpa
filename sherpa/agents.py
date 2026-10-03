"""思考プロバイダ抽象の再エクスポート facade。

実体は `sherpa/providers/` パッケージ（prompts / base / simple / codex / registry）。このファイルは docstring と
re-export のみでロジックを持たない。呼び出し側は `agents.X` とモジュール属性を毎回参照するため、
monkeypatch は `agents.X` に対して行う。新規コードは `from sherpa.providers import ...` の直 import を推奨。

思考イベント:
- `{"type":"node","id","kind":"think|tool","label","detail","status":"active|done"}`
- `{"type":"_result","env":<answer envelope>,"decision":<route decision>}`
"""
from __future__ import annotations

import urllib.request  # noqa: F401 -- facade 束縛必須（test_usage_capture が A.urllib.request.urlopen を patch）
from dataclasses import dataclass  # noqa: F401 -- agents 公開名 golden 維持用（Ctx 定義は base.py へ移動済み）
from pathlib import Path  # noqa: F401 -- agents 公開名 golden 維持用（実体は providers/ 各所へ移動済み）
from typing import Callable, Iterator  # noqa: F401 -- agents 公開名 golden 維持用（実体は providers/base.py へ移動済み）

from . import codex_agents_md, codex_skills, llm
from .providers.prompts import (  # noqa: F401 -- facade 再エクスポート（フェーズ5 S2・純移動）
    _PLAIN_PROMPT,
    _PLAIN_PROMPT_WITH_PERSONAL,
    _answer_prompt,
    _facts,
    _kb_hint,
    _kb_hint_abs,
)
from .providers.base import (  # noqa: F401 -- facade 再エクスポート（フェーズ5 S3・純移動）
    Ctx,
    Provider,
    _LENS_INTENT,
    _TOOLS,
    _can_ask,
    _gather,
    _log,
    _node,
    _plain_run,
    _usage_meta,
)
from .providers.simple import SimpleProvider  # noqa: F401 -- facade 再エクスポート（簡易チャット）
from .providers.codex.sandbox import (  # noqa: F401 -- facade 再エクスポート（フェーズ5 S8・純移動）
    _codex_clean_env,
    _codex_sandbox_enabled,
    _detect_chrome_path,
    _kb_read_roots,
    _marp_bin,
    _safe_codex_sessions_home,
    _safe_workspace_authoring,
    _web_search_admin_allowed,
    _web_search_c_args,
    _web_search_disabled_value,
    _write_codex_authoring_config,
)
from .providers.codex.mcp import (  # noqa: F401 -- facade 再エクスポート（フェーズ5 S9・純移動＋以後の追加分）
    _MCP_PASSTHROUGH,
    _abs_kb_or_derived,
    _apply_codex_neighbors,
    _codex_ask_capture,
    _codex_ask_question,
    _codex_mcp_enabled,
    _graph_schema_era_from_item,
    _mcp_config_args,
    _mcp_env,
    _mcp_neighbors_from,
    _toml_str,
)
from .providers.codex.provider import (  # noqa: F401 -- facade 再エクスポート（フェーズ5 S10・純移動）
    CodexProvider,
    _CONTINUE_PROMPT,
    _LAST_MESSAGE_MAX_BYTES,
    _PROGRESS_END_RE,
    _PROGRESS_MARKERS,
    _PROGRESS_VERBS,
    _SKILLS_BASE,
    _accumulate_codex_usage,
    _humanize_cmd,
    _is_progress_only,
    _killpg,
    _needs_continuation,
    _pick_codex_headline,
    _read_last_message_fallback,
    _spawn_stop_watcher,
    _trim_trailing_progress,
    _usage_from_turn_completed,
)
from .providers import (  # noqa: F401 -- facade 再エクスポート（フェーズ5 S11・純移動）
    AGENT_PROVIDERS,
    _UnwiredProvider,
    _select_provider,
    get_provider,
    provider_info,
)
