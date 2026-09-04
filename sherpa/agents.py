"""思考プロバイダ抽象（M9改・04-画面の原則.md §3.1）。**再エクスポート facade**。

チャットの「頭脳」を差し替え可能にする seam。各プロバイダ（heuristic / codex / openai / ollama /
gemini / bedrock）は**同じ思考イベント**（`node` の active→done を任意個 ＋ 最後に `_result`）を
yield するだけ。UI と SSE プロトコルは不変なので、**どのバックエンドでも思考の可視化は同一**に動く。
選択は**ユーザ設定**（store.user_settings）＞環境変数 `SHERPA_AGENT`（既定 heuristic）。

思考イベント:
- `{"type":"node","id","kind":"think|tool","label","detail","status":"active|done"}`  … 動的に何個でも
- `{"type":"_result","env":<answer envelope>,"decision":<route decision>}`               … 内部（chat_service が永続）
特定テーマの名前はコードに持たない（起点語/検索語は会話とデータから）。

**このファイルの実体はリファクタリング計画フェーズ5（S2〜S11・
docs/proposals/2026-07-02-リファクタリング計画.md）で `sherpa/providers/` パッケージへ純移動済み**。
ドメイン別モジュールは:

    providers/prompts.py        システムプロンプト・facts 整形
    providers/base.py           Provider 抽象・Ctx・_gather・_plain_run 等の共通土台
    providers/heuristic.py      HeuristicProvider
    providers/ollama.py         OllamaProvider
    providers/openai.py         OpenAIProvider
    providers/gemini.py         GeminiProvider
    providers/bedrock.py        BedrockProvider・認証/redact/profile 補助
    providers/codex/sandbox.py  Codex authoring サンドボックス（permission profile 方式）
    providers/codex/mcp.py      Codex MCP 連携
    providers/codex/provider.py CodexProvider（実行・プロセス管理）
    providers/__init__.py       registry（get_provider/provider_info/AGENT_PROVIDERS/_select_provider）

このファイル自体は**docstring と re-export のみ**でロジックを持たない（store フェーズ4の
`sherpa/store/__init__.py` と同形式）。**facade＝このモジュールの属性がシーム**: 呼び出し側
（sherpa/ 各所・tests/）は `from sherpa import agents` の上で `agents.get_provider(...)` の
ように毎回モジュール属性を参照するため、monkeypatch は常に `agents.X`（このファイルの属性）に
対して行う（各ドメインモジュール内部の名前束縛を直接差し替えても facade 側には反映されない・
例外は `_gather`・`_select_provider` が組み立てる各 Provider クラス・`_bedrock_auth_available` で、
いずれもパッケージ内から関数内 `from sherpa import agents as _facade` の実行時解決で
facade シームを保っている・詳細は `sherpa/providers/base.py`・`sherpa/providers/__init__.py` の
docstring 参照）。**新規コードは `from sherpa.providers.<mod> import ...`（例:
`from sherpa.providers import get_provider`）の直 import を推奨**する（facade 経由の間接参照は
既存呼び出し側・テスト互換のために維持している）。

`tests/unit/test_agents_surface.py` が `dir(sherpa.agents)` の公開名一覧（フィルタ後）を golden
固定しており、re-export 漏れ・意図しない挙動変化を検知する。
"""
from __future__ import annotations

import urllib.request  # noqa: F401 -- facade 束縛必須（test_usage_capture が A.urllib.request.urlopen を patch）
from dataclasses import dataclass  # noqa: F401 -- agents 公開名 golden 維持用（Ctx 定義は base.py へ移動済み）
from pathlib import Path  # noqa: F401 -- agents 公開名 golden 維持用（実体は providers/ 各所へ移動済み）
from typing import Callable, Iterator  # noqa: F401 -- agents 公開名 golden 維持用（実体は providers/base.py へ移動済み）

from . import codex_agents_md, codex_skills, llm
from .providers.prompts import (  # noqa: F401 -- facade 再エクスポート（フェーズ5 S2・純移動）
    _AUTHOR_FALLBACK_NOTE,
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
    _GenProvider,
    _LENS_INTENT,
    _TOOLS,
    _can_ask,
    _gather,
    _log,
    _node,
    _plain_run,
    _usage_meta,
)
from .providers.heuristic import HeuristicProvider  # noqa: F401 -- facade 再エクスポート（フェーズ5 S4・純移動）
from .providers.ollama import OllamaProvider  # noqa: F401 -- facade 再エクスポート（フェーズ5 S5・純移動）
from .providers.openai import OpenAIProvider, _openai_usage  # noqa: F401 -- facade 再エクスポート（フェーズ5 S6・純移動）
from .providers.gemini import GeminiProvider, _gemini_usage  # noqa: F401 -- facade 再エクスポート（フェーズ5 S6・純移動）
from .providers.bedrock import (  # noqa: F401 -- facade 再エクスポート（フェーズ5 S7・純移動）
    BEDROCK_MODEL_CHOICES,
    BEDROCK_MODEL_ID_RE,
    BedrockProvider,
    _BEARER_RE,
    _BEDROCK_ENV_KEYS,
    _BEDROCK_LIST_TIMEOUT,
    _BEDROCK_MAX_TOKENS,
    _BEDROCK_MODEL,
    _BEDROCK_PROFILE_REGION_LABEL,
    _anthropic_usage_raw,
    _bedrock_auth_available,
    _bedrock_error_detail,
    _bedrock_list_error_message,
    _bedrock_profile_label,
    _bedrock_region,
    _bedrock_text,
    _is_anthropic_profile,
    _redact_bedrock_secret,
    _resolve_bedrock_bearer_key,
    _safe_bedrock_detail,
    list_bedrock_inference_profiles,
)
from .providers.codex.sandbox import (  # noqa: F401 -- facade 再エクスポート（フェーズ5 S8・純移動）
    _codex_clean_env,
    _codex_sandbox_enabled,
    _detect_chrome_path,
    _kb_read_roots,
    _marp_bin,
    _safe_codex_sessions_home,   # R1b（RV再検証 MEDIUM-3・2026-07-15）: 会話ごと CODEX_HOME の symlink 検証
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
    _graph_schema_era_from_item,  # RV是正 rv-periphery #11（2026-09-05）で追加
    _mcp_config_args,
    _mcp_env,
    _mcp_neighbors_from,
    _toml_str,
)
from .providers.codex.provider import (  # noqa: F401 -- facade 再エクスポート（フェーズ5 S10・純移動）
    CodexProvider,
    _AUTHORING_LOCKS,
    _AUTHORING_LOCKS_GUARD,
    _LAST_MESSAGE_MAX_BYTES,
    _PROGRESS_END_RE,
    _PROGRESS_MARKERS,
    _PROGRESS_VERBS,
    _SKILLS_BASE,
    _authoring_lock,
    _humanize_cmd,
    _is_progress_only,
    _killpg,
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
