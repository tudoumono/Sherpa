"""Codex authoring 用サンドボックス機構（リファクタリング計画 フェーズ5 S8・`sherpa/agents.py` から
純移動）。

Feature A（permission profile 方式の読取封じ込め・2026-07-01 実機実証）＋ Marp レンダ用バイナリ検出＋
web_search 既定 OFF ポリシー（Codex 強化計画 Phase0・§5-1）をまとめる。docs/08-実行権限と隔離.md /
memory `codex-sandbox-permission-profile` と対応付け。`sherpa/agents.py` が facade として本モジュール
から再エクスポートする（S10 で `CodexProvider` は `codex/provider.py` へ・S11 で `_select_provider` は
`providers/__init__.py` へ移動済み＝本モジュールの利用者はどちらも providers パッケージ内の兄弟）。

移動した10名: `_codex_sandbox_enabled`・`_kb_read_roots`・`_codex_clean_env`・`_marp_bin`・
`_detect_chrome_path`・`_web_search_admin_allowed`・`_web_search_disabled_value`・
`_web_search_c_args`・`_write_codex_authoring_config`・`_safe_workspace_authoring`。

**明示変更（3箇所・計画書 S8 指示どおり）**: `Path(__file__).resolve().parents[1]`
（`sherpa/agents.py` 基準＝repo root）は、本モジュール（`sherpa/providers/codex/sandbox.py`）が
2階層深い（providers→codex）ため `parents[3]` に修正した（`_kb_read_roots`・`_marp_bin`・
`_write_codex_authoring_config` 内の MCP サブプロセス PYTHONPATH の3箇所）。
`tests/unit/test_agents_surface.py` の pin テストは `pathlib.Path(sherpa.agents.__file__)
.resolve().parents[1]`（facade は常に `sherpa/agents.py` を指す）と比較するため、両者が同じ
実パス（repo root）を指すことで担保される。

**相対 import の深さ調整（純移動の範囲内・S5〜S7 と同じ判断）**: `_kb_read_roots` 内の
`from . import worlds` は、本モジュールが `sherpa` から2階層深い（providers→codex）ため
`from ... import worlds` に変更した（参照先は変わらず `sherpa.worlds`）。

**`_mcp_env`・`_toml_str` は兄弟モジュール `.mcp` から直接 import する**（S9 で移動済み・
一方向 sandbox→mcp なので循環なし）。S8 時点では両名がまだ `sherpa/agents.py` 側に残っていた
ため facade 実行時解決（関数内 `from sherpa import agents as _facade`）で凌いでいたが、
S9 完了後は不要になったので RV（2026-07-14・LOW）の指摘どおり直接 import に戻した。
どちらもテストが facade attribute を **patch する**名前ではない（facade 経由の直接**呼び出し**のみ）
ことを確認済み＝patch 素通りの懸念なし（「危険な継ぎ目」リストは `_gather`／`BedrockProvider`／
`_bedrock_auth_available` のみ）。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .mcp import _mcp_env, _toml_str


# ---- Feature A: Codex authoring の permission-profile サンドボックス（読取封じ込め・2026-07-01 実機実証）----
# `-s workspace-write` は書込を cwd に封じるが**読取が FS 全開**＝他人 workspace・秘密が読める（RV BLOCKER①②）。
# Codex 0.139 の permission profile（default_permissions）で**読取も KB(RO)＋authoring(RW) に封じ込める**。
# 検証・落とし穴は docs/notes/2026-07-01-codex-authoring-sandbox.md / memory codex-sandbox-permission-profile。
def _codex_sandbox_enabled() -> bool:
    """既定 ON。SHERPA_CODEX_SANDBOX=0 で旧 `-s workspace-write` にフォールバック（緊急時の逃げ道）。"""
    return os.environ.get("SHERPA_CODEX_SANDBOX", "1").strip().lower() not in ("0", "false", "no", "off", "")


def _kb_read_roots(world: str) -> list:
    """permission profile に read を許す KB の絶対パス（fixtures か実 world root・無ければ data/kb 全体）。"""
    from ... import worlds
    repo_root = Path(__file__).resolve().parents[3]
    roots: list = []
    try:
        if worlds._fixtures():
            base = repo_root / "fixtures" / "corpus" / world
            if base.exists():
                roots.append(str(base.resolve()))
        else:
            wd = worlds.world_dir(world)
            if wd:
                roots.append(str(Path(wd).resolve()))
    except Exception:
        pass
    if not roots:
        roots.append(str((repo_root / "data" / "kb").resolve()))
    return roots


# 親環境に**設定されているときだけ** Codex へ透過する変数（閉域実機の是正・2026-08-18）。
# プロキシ経由でしか外へ出られない閉域では、これが届かないと Codex（と web 検索）が OpenAI に到達できない。
# MITM 型プロキシなら社内 CA も要る。いずれも**接続経路の設定であって creds（DB/ES/KB/API キー）ではない**
# ＝「creds を渡さない」という上の契約は保たれる（プロキシ URL に認証を埋める運用は利用者の判断で、
# それは OpenAI に送るものではなくプロキシへの接続情報）。大文字・小文字の両方を見る（curl/Node は小文字も読む）。
_CODEX_PASSTHROUGH_ENV: tuple[str, ...] = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
)


def _codex_clean_env(codex_home: Path, authoring: Path, tmpdir: Path,
                     openai_api_key: str | None = None) -> dict:
    """codex exec 用の最小 env（env -i 相当）。**DB/ES/KB creds を渡さない**・PATH 等ランタイムのみ。
    creds が要る MCP サブプロセスへは config ファイル(mcp_servers.sherpa.env)経由で渡す（プロセス env に置かない）。
    例外はプロキシ/CA の経路設定（`_CODEX_PASSTHROUGH_ENV`）で、親環境に**あるときだけ**そのまま渡す。

    `openai_api_key`（S2・Azure OpenAI 対応・2026-08-18）: **既定 None＝従来どおり渡さない**（回帰ゼロ・
    `test_codex_clean_env_has_no_secrets`／`test_codex_clean_env_passes_proxy_and_ca_only_when_set`
    は引数省略で呼び、親環境に `OPENAI_API_KEY` があっても env に出ないことを固定している）。

    非 None を渡すのは `_write_codex_authoring_config` が Codex(OpenAI) 構成で接続先を Azure 等の
    カスタム `model_providers.<id>` へ差し替えた時**だけ**（`_openai_compat_provider_lines` 参照）。
    その独自プロバイダは `env_key = "OPENAI_API_KEY"` で**子プロセスの環境変数**からキーを読む設計
    （Codex 公式ドキュメント確認済み・`auth.json`/ChatGPT ログインは `requires_openai_auth = true` を
    明示した provider だけが使う別経路で、本カスタム provider はそれを設定していない）。一方、既定
    （OpenAI 直結・組込み `openai` provider）は引き続き `auth.json`（実 home からの symlink）経由の
    ままで、この関数に env として渡す必要が無い＝呼び出し元（provider.py）はこの構成の時だけ
    `openai_api_key` を渡す（他の全呼び出しは省略＝この docstring 追記だけでは何も変わらない）。"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(authoring),
        "CODEX_HOME": str(codex_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TMPDIR": str(tmpdir),
    }
    for name in _CODEX_PASSTHROUGH_ENV:
        value = os.environ.get(name)
        if value:                                   # 空文字は「未設定」と同じ＝渡さない
            env[name] = value
    if openai_api_key:                              # 明示的に渡された時だけ（既定 None は従来どおり無し）
        env["OPENAI_API_KEY"] = openai_api_key
    return env


# ---- Marp（スライド作成スキル）レンダ用のバイナリ検出（M3 案2・2026-07-12・
#      RUNTIME-SANDBOX §9 の M1 実証結果 / §10.3 の未解決問題を踏まえた設計変更）----
# Codex は sandbox 内で .md を書くだけ（marp CLI を直接呼ばない）。レンダ（HTML/PDF/PPTX）は
# Codex 完了後に Sherpa 本体プロセスが marp_render.py 経由でこの marp CLI・Chromium を使って
# 実行する（sandbox の外＝permission profile の read root に marp/Chromium を足す必要が無い）。
def _marp_bin() -> str | None:
    """marp CLI 実行ファイルの絶対パス（存在＆実行可能な時だけ）。env `SHERPA_MARP_BIN` で明示上書き可。
    未解決なら None＝marp_render.render_outputs() は何もしない（.md のみが成果物）。"""
    override = os.environ.get("SHERPA_MARP_BIN")
    if override:
        # RV Med（2026-07-08）: 相対パスのまま子プロセスへ渡すと Popen(cwd=authoring) 側で
        # authoring 相対に誤解釈される。expanduser＋絶対化して渡す（abspath＝symlink は辿らない）。
        p = Path(os.path.abspath(os.path.expanduser(override)))
        return str(p) if (p.is_file() and os.access(str(p), os.X_OK)) else None
    # repo_root は絶対（__file__.resolve()）。`.bin/marp` は npm が張る symlink（→ marp-cli.js）。
    # RUNTIME-SANDBOX §9 / M1 実証がこの `.bin/marp` パスをそのまま使うため resolve せず返す
    # （リポジトリ管理の開発ツールで、authoring 配下の user データではない＝symlink 封じ込め対象外）。
    repo_root = Path(__file__).resolve().parents[3]
    cand = repo_root / "tools" / "marp" / "node_modules" / ".bin" / "marp"
    if cand.is_file() and os.access(str(cand), os.X_OK):
        return str(cand)
    return None


def _detect_chrome_path() -> str | None:
    """CHROME_PATH（PDF/PPTX レンダに必須の Chromium）。既存 env（CHROME_PATH/CHROMIUM_PATH）を
    尊重し、無ければ Playwright の既存 chromium を自動検出（新規 DL なし・M1 実証で流用実績）。
    見つからなければ None＝marp_render.render_outputs() は HTML のみ生成する。"""
    for k in ("CHROME_PATH", "CHROMIUM_PATH"):
        v = os.environ.get(k)
        if v:
            # RV Med（2026-07-08）: 絶対化＋実行ビット確認（非実行ファイルを渡すと Puppeteer が
            # EACCES でレンダ失敗）。相対パスは Popen(cwd=authoring) で誤解釈されるため絶対化。
            p = Path(os.path.abspath(os.path.expanduser(v)))
            if p.is_file() and os.access(str(p), os.X_OK):
                return str(p)
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    cands = list(home.glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    if not cands:
        return None

    def _ver(p: Path) -> int:                       # chromium-1228 の数値部で最新を選ぶ（文字列比較だと桁数で誤る）
        m = re.search(r"chromium-(\d+)", str(p))
        return int(m.group(1)) if m else -1

    latest = max((c for c in cands if c.is_file() and os.access(str(c), os.X_OK)), key=_ver, default=None)
    return str(latest) if latest else None


# ---- WEB-1: web_search は既定 OFF。Codex CLI は web_search が既定 ON（OpenAI 管理インデックスの
# キャッシュ）で、社内資料接地の原則（04-画面の原則.md §4）と不整合のため、管理者が管理画面
# （system_settings.web_search_allowed）で明示許可した場合のみ、チャットごとの希望を尊重する。----
def _web_search_admin_allowed(system_settings: dict | None = None) -> bool:
    """管理者フラグ（`system_settings.web_search_allowed`・既定 false・管理画面「プロバイダ＋接続先」
    タブで設定）。env `SHERPA_ALLOW_WEB_SEARCH` は初回シードのみ（`sherpa.api._seed_settings_from_env`）
    で、実行時にはもう読まない（設定所有台帳の原則）。DB 不達（`system_settings` 省略時の取得失敗）は
    安全側 `False`（env フォールバックはしない）。`system_settings`（省略可）は呼び出し側が既に読んだ
    スナップショットをそのまま使う（`sherpa.llm._openai_endpoint_settings` と同じ形）。"""
    if system_settings is None:
        try:
            from ... import store
            system_settings = store.get_system_settings()
        except Exception:
            return False
    return bool(system_settings.get("web_search_allowed"))


def _web_search_disabled_value(user_enabled: bool, endpoint_kind: str = "openai",
                               system_settings: dict | None = None) -> str | None:
    """config/argv へ渡す web_search の値。管理者が許可し、かつこのチャットで希望した時だけ
    `None`（＝config へ何も書かない・Codex 既定の ON に委ねる）。それ以外は常に `"disabled"`。
    管理者未許可の間は、`user_enabled=True`（このチャットで希望）が渡されても無視する。

    `endpoint_kind`（S2・Azure OpenAI 対応・2026-08-18）: Codex(OpenAI) 構成の実際の接続先
    （`sherpa.llm.openai_endpoint_kind()` の値）。`"openai"`（既定・省略時もこれ）以外＝Azure 等の
    代替エンドポイントのときは、admin 許可・ユーザー設定に**関わらず常に無効化**する（Codex の
    web_search は OpenAI がホストする管理インデックスの機能。Azure OpenAI Responses API 自体は
    Web 検索ツールに対応しているが、現在の Sherpa＋Codex CLI カスタムプロバイダー経由でこの
    代替エンドポイントでも動くかは未検証のため、確認できるまで一律無効のままにする）。省略時は
    従来どおりの判定のみ（回帰ゼロ）。`_web_search_c_args`（emergency
    fallback＝`SHERPA_CODEX_SANDBOX=0` 経路）はこの引数を渡さない＝この経路は Azure 等への
    リダイレクト自体が未対応（`_write_codex_authoring_config` 参照。Codex(Ollama) 構成もこの経路
    では独自 model_provider を書けないため、`_select_provider` がサンドボックス無効時は honest
    failure を返し Codex を起動しない＝そもそもこの経路まで到達しない）ため、web_search だけ独自に
    強制 OFF すると「Azure は使えないのに web_search だけ気にする」というちぐはぐな挙動になる。

    `system_settings`（省略可）は `_web_search_admin_allowed` へそのまま転送する（呼び出し側が
    既に読んだスナップショットを使い回す・省略時は都度読み直す）。"""
    if endpoint_kind != "openai":
        return "disabled"
    if user_enabled and _web_search_admin_allowed(system_settings):
        return None
    return "disabled"


def _web_search_endpoint_note(user_enabled: bool, endpoint_kind: str,
                              system_settings: dict | None = None) -> str | None:
    """S2: 接続先が既定(OpenAI)以外（Azure 等）のせいで web_search が強制 OFF になっている時だけ、
    ユーザー向けの一言を返す（それ以外は None＝何も表示しない）。

    `_web_search_disabled_value` と条件を二重管理しない: admin 許可 or このチャットでの希望の
    どちらかが欠けている場合は、Azure と無関係にそもそも既定で OFF なので「Azure が理由」という
    説明は不要（過剰な注記を出さない）。`system_settings`（省略可）は `_web_search_admin_allowed`
    と同じ理由（呼び出し側のスナップショットをそのまま使う）。"""
    if endpoint_kind != "openai" and bool(user_enabled) and _web_search_admin_allowed(system_settings):
        return ("接続先が Azure OpenAI（または OpenAI 以外の互換エンドポイント）のため、"
                "現在の Sherpa＋Codex 構成では Web 検索は未検証として無効にしています。")
    return None


def _web_search_c_args(user_enabled: bool, system_settings: dict | None = None) -> list:
    """fallback 経路（`--strict-config` 無し・config.toml でなく `-c`）用の argv 追加分。
    `_write_codex_authoring_config` の web_search 行と同じ判定を `-c` 引数の形で返す
    （単一の真実源は `_web_search_disabled_value`・sandbox/fallback 間の判定ロジック重複を防ぐ）。
    disabled 相当なら `["-c", 'web_search="disabled"']`・有効相当なら `[]`（Codex 既定 ON に委ねる）。

    S2（Azure OpenAI 対応）: `endpoint_kind` を渡さない＝常に既定 "openai" 扱い。この emergency
    fallback 経路（`SHERPA_CODEX_SANDBOX=0`）はそもそも Azure 等へのリダイレクト自体が未対応
    （`_write_codex_authoring_config` の `ollama_base_url`/`_openai_compat_provider_lines` 分岐は
    sandbox モードのみ。Codex(Ollama) 構成は `_select_provider` がサンドボックス無効時に honest
    failure を返しこの経路まで到達しない）ので、この経路では実際に既定の api.openai.com へ繋がり
    web_search も従来どおり使える＝ここだけ強制 OFF にする理由が無い。`system_settings`（省略可）は
    呼び出し元（`CodexProvider`）が保持するスナップショットをそのまま使う。"""
    v = _web_search_disabled_value(user_enabled, system_settings=system_settings)
    return ["-c", f"web_search={_toml_str(v)}"] if v is not None else []


def _ollama_provider_lines(ollama_base_url: str) -> list[str]:
    """Codex CLI を Ollama へ向ける設定行（`model_provider` ＋ 独自プロバイダ定義）。

    実測（codex-cli 0.144.1・2026-08-15）:
      - `wire_api = "chat"` は廃止済み。Codex は OpenAI **Responses API**（`POST /v1/responses`）を使う。
        Ollama は 0.13.3 以降これに対応している（非stateful のみ）。
      - 組み込みプロバイダ id `ollama` は予約語で上書きできず、接続先が `localhost:11434` 固定になる
        （`OLLAMA_HOST` も効かない）。そのため**独自 id で定義**し、Sherpa の `ollama_url` 設定を
        常に効かせる（設定項目があるのに一部構成だけ無視される、という不整合を作らない）。

    `base`（`ollama_url`）は呼び出し側が `llm.assert_ollama_url_allowed` を通したものを渡す。
    """
    base = ollama_base_url.rstrip("/") + "/v1"
    return [
        f'model_provider = {_toml_str(_OLLAMA_PROVIDER_ID)}',
        '',
        f'[model_providers.{_OLLAMA_PROVIDER_ID}]',
        'name = "Ollama"',
        f'base_url = {_toml_str(base)}',
        'wire_api = "responses"',      # chat 方言は codex 0.144 で廃止（実測）
    ]


# 組み込み id（`ollama`）は予約語のため衝突しない名前を使う（実測で 400 相当のエラーになる）。
_OLLAMA_PROVIDER_ID = "sherpa-ollama"

# 組み込み id（`openai`/`ollama`/`lmstudio`）は予約語のため衝突しない名前を使う（Codex 公式ドキュメント
# 「Custom providers can't reuse the reserved built-in provider IDs」＝実装前に確認済み・2026-08-18）。
_OPENAI_COMPAT_PROVIDER_ID = "sherpa-openai-compat"


# S2（Azure OpenAI 対応・2026-08-18）: `sherpa.llm` の `openai_endpoint_kind()`/`openai_base_url()`
# を呼ぶ単一の真実源。作業開始当初は S1（`sherpa.llm`）と並行実装中だったため
# `getattr(..., None)` で欠落を防御していたが、S1 は着地済み＝LOW-1（2026-08-18 Codex RV）で
# 直接呼びに戻した（欠落を隠す防御は「関数が消えても気づかない」逆効果になるため撤去）。
def _openai_endpoint_kind(system_settings: dict | None = None) -> str:
    """`sherpa.llm.openai_endpoint_kind()` を呼ぶ（"openai" | "azure" | "custom"）。
    `system_settings`（省略可）は `CodexProvider` が保持するスナップショットをそのまま渡す
    （省略時は `llm.py` が都度読み直す）。"""
    from ... import llm as _llm
    return _llm.openai_endpoint_kind(system_settings)


def _openai_compat_base_url(system_settings: dict | None = None) -> str:
    """`sherpa.llm.openai_base_url()` を呼び、HIGH-1（2026-08-18 Codex RV）として base URL の
    妥当性（`llm.assert_openai_base_url_allowed`）も検証する。

    呼ばれるのは呼び出し側（`_write_codex_authoring_config`）が既に `_openai_endpoint_kind() !=
    "openai"` と確認した後だけ。`_select_provider`（`providers/__init__.py`）が既に同じ検証を通した
    上で `CodexProvider` を組み立てる契約だが、ここでも検証する＝config.toml へ書く／子プロセス env
    にキーを渡す**直前**の最終防衛線（`_select_provider` の判定を迂回する経路があっても、不正な
    base URL がそのまま書かれてキーが誤った宛先へ渡ることを防ぐ）。不正なら `ValueError` を送出し、
    呼び出し元（`provider.py` の実行ループ）の既存 broad except に乗って安全に degrade する
    （S1 docstring 参照）。`system_settings`（省略可）は `_openai_endpoint_kind` と同じ理由。"""
    from ... import llm as _llm
    base = _llm.openai_base_url(system_settings)
    _llm.assert_openai_base_url_allowed(base)
    return base


def _openai_compat_provider_lines(base_url: str, *, api_version: str | None, auth_header: str) -> list[str]:
    """Codex CLI を OpenAI 互換エンドポイント（主用途は Azure OpenAI）へ向ける設定行。
    `_ollama_provider_lines` と同型（`model_provider` ＋ 独自プロバイダ定義）。呼ばれるのは
    `_write_codex_authoring_config` が「Codex(OpenAI) 構成で、接続先が既定(api.openai.com)以外」と
    判定した時だけ＝既定のときは**この関数自体が呼ばれない**＝回帰ゼロ。

    実装根拠（Codex `config-advanced` 公式ドキュメント確認済み・2026-08-18・codex-cli 0.144.1）:
      - Azure 公式サンプルはそのまま `[model_providers.azure]` に `env_key`＋`query_params`
        （`api-version`）＋`wire_api = "responses"` を書く。`openai_base_url`（トップレベル・組込み
        `openai` provider の base_url だけを差し替える簡易版）は `wire_api`/`query_params`/`env_key`
        を変えられないため Azure（v1 API 以外・旧方式）や独自ヘッダが要る構成には使えない
        （ビルトイン `openai` provider 自体は上書き不可＝予約語）。
      - `env_key` は Codex **子プロセスの環境変数**からキーを読む（`auth.json`/ChatGPT ログインは
        provider 側で `requires_openai_auth = true` を明示した時だけ使われる別経路で、本カスタム
        provider はそれを設定しない＝env_key 一本）。そのため Sherpa 側は、この構成の時**だけ**
        `_codex_clean_env` に `OPENAI_API_KEY` を渡す必要がある（`provider.py` 呼び出し側・
        `_codex_clean_env` の `openai_api_key` 引数を参照。既定(OpenAI 直結)は auth.json 経由の
        ままで変更なし＝env にキーを置かない現行方針を維持）。
      - `env_key` だけなら Codex は既定で `Authorization: Bearer <値>` を送る。Microsoft 公式の
        REST 例は Azure API キーを `api-key` ヘッダ、Entra ID トークンを `Authorization: Bearer`
        ヘッダで案内しており、Azure API キーの Bearer 送出そのものを公式に保証したものではない。
        ただし実機の Azure v1 エンドポイントで疎通確認済みのため、既定はこのまま Bearer とする
        （`auth_header="bearer"` はこの既定のまま何も追加しない）。
      - 旧来の `api-key: <値>` ヘッダ形式が要る環境だけ `env_http_headers`（env 変数名を書く・
        **値そのものは書かない**）で追加する。`env_key` はそのまま残す（Bearer と api-key を同時に
        送る構成＝この組み合わせ自体は未検証。Azure 側がどちらを優先する／片方を無視するかは
        確認していない）。`http_headers`（静的値を書く方）は使わない＝キーの値が 0600 の
        config.toml とはいえ literal で残ってしまう理由が無いため。
    """
    lines = [
        f'model_provider = {_toml_str(_OPENAI_COMPAT_PROVIDER_ID)}',
        '',
        f'[model_providers.{_OPENAI_COMPAT_PROVIDER_ID}]',
        'name = "OpenAI 互換エンドポイント"',
        f'base_url = {_toml_str(base_url)}',
        'env_key = "OPENAI_API_KEY"',
        'wire_api = "responses"',
    ]
    if api_version:
        lines.append('query_params = { "api-version" = ' + _toml_str(api_version) + ' }')
    if auth_header == "api-key":
        lines.append('env_http_headers = { "api-key" = "OPENAI_API_KEY" }')
    return lines


def _write_codex_authoring_config(codex_home: Path, kb_roots: list, reason: str,
                                  mcp: bool, world: str, scope_paths,
                                  web_search_enabled: bool = False,
                                  ask_disabled: bool = False,
                                  ollama_base_url: str | None = None,
                                  system_settings: dict | None = None,
                                  layer=None) -> None:
    """per-request CODEX_HOME に permission profile（＋任意で MCP 設定）を書く。
    **creds は config ファイル内に閉じる**（`:root=deny` 下では model-shell から CODEX_HOME 不可視・
    コマンドライン `-c` に creds を出さない＝`/proc/<pid>/cmdline` 漏洩も無い）。auth.json は実 home から symlink。

    `layer`（省略可・既定 `None`＝both）: `_mcp_env` へそのまま転送する（探す対象・MCP サーバ側の
    フィルタ）と同時に、`mcp=True` かつ `"docs"/"code"` に限定されているときは、解決済み KB ルート
    それぞれへ permission profile 上で明示的な `deny` を書く（下記参照・正典 §3.4「範囲と同じ
    硬いフィルタ」）——`":minimal"`（/usr,/bin,libs 等）配下に KB root が来る配置でも読めないよう、
    読取許可の省略ではなく明示 deny にする。呼び出し元は qa レンズのときだけ実値を渡し、それ以外は
    `None`（both）のまま呼ぶ契約。"""
    codex_home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(codex_home, 0o700)                 # RV HIGH: creds を含む CODEX_HOME を同ホスト他プロセス/ユーザから守る
    except OSError:
        pass
    # Codex(OpenAI) 構成（`ollama_base_url` なし）だけ、auth.json（実 home の OpenAI 資格情報）を
    # 受け渡す**直前**に再確認する。Popen 直前（provider.py）より手前のチョークポイント＝ここで
    # 止まれば auth.json の symlink 自体を作らない（Codex(Ollama) は OpenAI 系 I/O ではないため
    # 対象外）。呼び出し元（provider.py）の既存 broad except に乗り、「profile config 書込失敗→
    # answer=None→決定的回答」という既存の fail-closed 経路へそのまま合流する。
    if ollama_base_url is None:
        from ... import llm as _llm
        _llm.assert_openai_io_allowed()
    real_home = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    src = real_home / "auth.json"
    dst = codex_home / "auth.json"
    try:
        if src.exists() and not dst.exists():
            dst.symlink_to(src.resolve())
    except Exception:
        pass
    lines = [
        'default_permissions = "sherpa-authoring"',
        'approval_policy = "never"',
    ]
    # Codex(OpenAI) 構成のときだけ、実際の接続先（`sherpa.llm.openai_endpoint_kind()`）が既定
    # (api.openai.com) 以外かを見る＝既定なら "openai" が返り、以降の判定・分岐は全部素通り
    # （S1 未実装/未マージの間の防御的フォールバックも同じ "openai" を返す＝回帰ゼロ）。Azure/custom
    # 分岐（下の `elif`）は `ollama_base_url` が無い時だけ通るため、Ollama 構成側の値には無関係。
    _endpoint_kind = "openai" if ollama_base_url else _openai_endpoint_kind(system_settings)
    # WEB-1: web_search（OpenAI がホストする管理インデックス）は Codex(Ollama) 構成では原理的に
    # 使えない——`_endpoint_kind` を Azure/custom 判定用に "openai" のまま保つのとは別に、
    # web_search の可否判定にだけ "ollama"（openai 以外）を渡し、管理者許可・ユーザー希望に
    # 関わらず常に無効化する（`_web_search_disabled_value` の endpoint_kind != "openai" 分岐）。
    _web_search_endpoint_kind = "ollama" if ollama_base_url else _endpoint_kind
    _ws_value = _web_search_disabled_value(web_search_enabled, _web_search_endpoint_kind, system_settings)
    if _ws_value is not None:                        # Phase0・§5-1: 既定は必ず disabled を明示的に書く
        lines.append(f'web_search = {_toml_str(_ws_value)}')
    if ollama_base_url:                              # Codex(Ollama) 構成のときだけ接続先を差し替える
        lines += _ollama_provider_lines(ollama_base_url)
    elif _endpoint_kind != "openai":                 # Codex(OpenAI) 構成で接続先が Azure 等のときだけ
        from ... import llm as _llm
        # kind・base_url・auth_header・api_version をすべて同じ `system_settings` から読む
        # （呼び出しごとに個別へ都度読み直すと、この1回の config.toml 生成の中で admin 保存が
        # 挟まった場合に組が食い違い得る）。
        lines += _openai_compat_provider_lines(
            _openai_compat_base_url(system_settings),
            api_version=_llm.openai_api_version(system_settings) or None,
            auth_header=_llm.openai_auth_header_style(system_settings))
    lines += [
        '',
        '[permissions.sherpa-authoring]',
        'extends = ":workspace"',
        '',
        '[permissions.sherpa-authoring.filesystem]',
        '":root" = "deny"',       # FS 全体の読取を遮断（他人領域・秘密が見えない）
        '":minimal" = "read"',    # /usr,/bin,libs 等 実行最小限
    ]
    # 正典 §3.4「範囲と同じ硬いフィルタ」: 層（探す対象）が限定されたターンは、KB への直接
    # ファイル読み取りを許可せず MCP ツール経由の検索だけに構造的に限定する（MCP サーバ
    # ＝`sherpa/mcp_server.py` の `run_tool` が層を実際にフィルタする）。both／未指定は
    # 従来どおり KB を直接読取許可する（scope_paths と同じくプロンプト指示止まりで足りる）。
    if mcp and layer not in (None, "both"):
        # 読取許可を省略するだけでは足りない——world_admin_service は KB root の配置場所を
        # 制限しないため、KB root が `":minimal"`（/usr,/bin,libs 等・実行最小限として無条件で
        # read 許可）の配下に来る構成があり得る。省略はより広い許可の下で読めてしまうので、
        # 解決済みの KB root ごとに明示的な deny 行を足す（具体パスほど優先される profile 解決に
        # 頼らず、意図を明文化する）。
        for r in kb_roots:
            lines.append(f'{_toml_str(r)} = "deny"')
    else:
        for r in kb_roots:            # KB は読取専用
            lines.append(f'{_toml_str(r)} = "read"')
    lines += [
        '',
        '[permissions.sherpa-authoring.filesystem.":workspace_roots"]',
        '"." = "write"',          # authoring（cwd）だけ読書
        '',
        '[permissions.sherpa-authoring.network]',
        'enabled = false',        # model-shell の egress 遮断（codex 自身の API/MCP は codex 機構側で通る）
    ]
    if mcp:
        py = sys.executable or "python3"
        menv = _mcp_env(world, scope_paths, ask_disabled, layer=layer)
        # クリーン env 下でも MCP サブプロセス（python -m sherpa.mcp_server）が動くよう PATH/PYTHONPATH を補う。
        menv.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
        menv.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
        env_toml = "{" + ", ".join(f"{k} = {_toml_str(v)}" for k, v in menv.items()) + "}"
        lines += [
            '',
            '[mcp_servers.sherpa]',
            f'command = {_toml_str(py)}',
            'args = ["-m", "sherpa.mcp_server"]',
            'default_tools_approval_mode = "approve"',
            f'env = {env_toml}',
        ]
    cfg = codex_home / "config.toml"
    # RV HIGH: creds(mcp env) を含むため symlink/race を避けて 0600 で書く（O_CREAT|O_EXCL|O_NOFOLLOW）。
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    # RV MEDIUM: 既存 config が居たら **fail-closed**（握り潰さず raise）＝古い/細工された config での起動を防ぐ。
    fd = os.open(str(cfg), flags, 0o600)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _safe_workspace_authoring(users_dir: Path, uid: str):
    """RV BLOCKER: `workspace`/`authoring` に symlink が混入していると cwd/書込 root が個人 files 等へずれ、
    読取封じ込めが崩れる。各コンポーネントを symlink 拒否＋実体が workspace 配下に収まることを確認して返す。
    異常時は None＝fail-closed（Codex を起動しない）。uid slug も再検証（パス注入防御）。"""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", uid or ""):
        return None
    base = users_dir / uid
    ws = base / "workspace"
    authoring = ws / "authoring"
    for comp in (base, ws, authoring):
        if comp.is_symlink():                       # symlink 混入＝封じ込め崩壊 → fail-closed
            return None
        if comp.exists() and not comp.is_dir():     # dir 以外が居る → fail-closed
            return None
    try:
        authoring.mkdir(parents=True, exist_ok=True)
        authoring.resolve().relative_to(ws.resolve())   # 最終確認: 実体が workspace 配下
    except (OSError, ValueError):
        return None
    return authoring


def _safe_codex_sessions_home(users_dir: Path, uid: str, conversation_id) -> "Path | None":
    """R1b（会話継続・Codex ネイティブ resume・RV再検証 MEDIUM-3）: 会話ごとの永続 CODEX_HOME
    （`workspace/.codex-sessions/{cid}`）の安全確認。`_safe_workspace_authoring` と同じ契約
    （symlink混入・非ディレクトリ・workspace 外逸脱は fail-closed で None を返す＝呼び出し側は
    Codex を起動しない）。

    `{cid}` は conversation_id 由来の**固定パス**（毎ターン同じ場所を再利用する）ため、
    per-request 乱数名の旧 CODEX_HOME（`.codexhome-<rand>`）以上に symlink 事前設置（write-what-
    where）の標的になりやすい＝本関数で個別に検証する。uid 自体の形式検証・`workspace` の
    symlink 拒否は呼び出し側が既に `_safe_workspace_authoring` で済ませている前提
    （本関数は `.codex-sessions` とその下の `{cid}` だけを追加検証する）。
    """
    ws = users_dir / uid / "workspace"
    try:
        cid_str = str(int(conversation_id))
    except (TypeError, ValueError):
        return None
    sessions_root = ws / ".codex-sessions"
    codex_home = sessions_root / cid_str
    for comp in (sessions_root, codex_home):
        if comp.is_symlink():                       # symlink 混入＝封じ込め崩壊 → fail-closed
            return None
        if comp.exists() and not comp.is_dir():      # dir 以外が居る → fail-closed
            return None
    try:
        codex_home.mkdir(parents=True, exist_ok=True)
        codex_home.resolve().relative_to(ws.resolve())   # 最終確認: 実体が workspace 配下
    except (OSError, ValueError):
        return None
    return codex_home
