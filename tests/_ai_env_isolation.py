"""AI 系 env（`OPENAI_BASE_URL` 等）をテストから隔離する共通部品（単一の真実源）。

開発機の実シェル環境（Azure 実機接続用に export した値・実 `codex login` 済みの `~/.codex` 等）が
テストへ流入すると、接続先判定・頭脳自動選択・Codex 認証判定の結果が環境依存になり、実行するマシンや
シェルの状態で unit/contract の結果が変わってしまう。同じ理由で、import 時に一度だけ env を読み切る
精度チューニング系の定数（`agentic_search.MAX_HITS` 等）も対象に含める——開発機のシェルに
これらが export されていると、「未設定＝既定値」を前提にしたテストが環境依存で失敗しうる。

`AI_ENV_VARS` を `tests/conftest.py`（セッション開始時に一度だけ・`sherpa.llm` のような import 時に
env を読み切る一度きりの定数の対策）と、`tests/unit`／`tests/contract` の per-test autouse fixture
（この2箇所以外へコピーしない）の両方が参照する。
"""
from __future__ import annotations

import os

AI_ENV_VARS: tuple[str, ...] = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "SHERPA_OPENAI_AUTH_HEADER",
    "SHERPA_OPENAI_API_VERSION",
    "SHERPA_OPENAI_ENDPOINT_KIND",
    "OPENAI_CHAT_MODEL",
    "OPENAI_EMBED_MODEL",
    "SHERPA_AGENT",
    "SHERPA_EXTRA_AGENTS",
    "SHERPA_CODEX_SANDBOX",
    "SHERPA_ALLOW_WEB_SEARCH",
    "CODEX_HOME",
    "OLLAMA_URL",
    "GEMINI_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "SHERPA_GREP_MAX_HITS",
    "SHERPA_READ_WINDOW",
    "SHERPA_ES_CHUNK_LINES",
    "SHERPA_ES_HYBRID_WEIGHT",
    "SHERPA_IMPACT_MAX_DEPTH",
    "SHERPA_TROUBLESHOOT_GRAPH_DEPTH",
    "SHERPA_TOOLS_AVAILABILITY_TTL",
)

# `CODEX_HOME` は他の var と違い、単純削除では隔離にならない: 未設定時のコード側フォールバックは
# `~/.codex` のため、削除するとむしろ開発機の実 `auth.json`（`codex login` 済みなら実在する）を
# 参照してしまう。実在しないパスへ差し替えて「auth.json 無し」を保証する
# （`tests/unit/conftest.py` の `SHERPA_POWERSHELL_BIN` と同じ手法）。
CODEX_HOME_SENTINEL = "/nonexistent/sherpa-test-codex-home"


def strip_ai_env_from_os_environ() -> None:
    """`os.environ` を直接書き換える（`monkeypatch` がまだ使えないモジュールロード時専用の
    一度きりの呼び出し＝`tests/conftest.py` の DB 隔離 `_setup_test_pg_dsn` と同じ位置づけ）。"""
    for name in AI_ENV_VARS:
        os.environ.pop(name, None)
    os.environ["CODEX_HOME"] = CODEX_HOME_SENTINEL
