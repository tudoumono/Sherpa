"""システム系エンドポイント: `/healthz`・`/`（ルート）・`/config`・`/settings*`（ユーザー設定系）。
`healthz_router`/`settings_router`/`root_router` の 3 router を、api.py が元のエンドポイント位置にそれぞれ `app.include_router(...)` する（ルート表 golden の定義順を保つため）。
`sherpa.api` を import しない。
設計: docs/design/settings.md「個人設定に残るもの」
"""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from sherpa import agent_constructs, chat_examples, keys, llm, model_catalog, required_tools, store
from sherpa.agents import (
    AGENT_PROVIDERS,
    _web_search_admin_allowed,
    provider_info,
)
from sherpa.deps import _current_user
from sherpa.schemas import (
    ConfigResponse,
    SettingsResponse,
    SettingsTestResponse,
)

_log = logging.getLogger("sherpa")


# /config・/settings*（設定）。router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
settings_router = APIRouter()


# チャットで閉じた頭脳（保存させない。保存済みの値はチャット時に選び直しを案内する）。
_CLOSED_CHAT_AGENTS = frozenset({"heuristic", "gemini", "bedrock", "openai", "ollama"})


class SettingsReq(BaseModel):
    """個人設定の PUT ボディ。未指定のフィールドは変更しない。
    プロバイダ/モデルの選択・`codex_reasoning`・`codex_web_search`・`search_helper`・`system_prompt`・閉じたプロバイダ（gemini/bedrock）のキー・モデル欄は個人設定に無い。これらを送っても未知フィールドとして無視される（422 にならず保存もされない）。
    """
    agent: str | None = None
    # Codex CLI が接続するモデル提供元（openai / ollama）。Codex 構成のみ有効。
    codex_model_provider: str | None = None
    openai_api_key: str | None = None
    ollama_url: str | None = None


# 接続先（OpenAI／Azure OpenAI／その他 OpenAI 互換）を個人設定画面へ読み取り専用で表示するための補助。判定は `sherpa/llm.py::openai_base_url`/`openai_endpoint_kind` が唯一の真実源。実行時例外（DB 不達等）は安全側（openai・ホスト名なし）に倒す。
_INVALID_SAVED_BASE_URL_LABEL = "(不正な保存値)"


def _openai_base_url_host(system_settings: dict | None = None) -> str:
    """`llm.openai_base_url()` からホスト名だけを取り出す（パス・クエリ・認証情報は含めない）。
    `system_settings`（省略可）は `llm.openai_base_url()` へそのまま渡す（`_public_settings` が 1 応答内の単一スナップショットを渡し、kind と host が別世代にならないようにする）。
    表示前に `llm.assert_openai_base_url_allowed()` で再検証する（空白・バックスラッシュ混入の値は `urlsplit` が `hostname` にそのまま含めるため）。不合格なら固定文字列（`_INVALID_SAVED_BASE_URL_LABEL`）を返す。
    """
    try:
        url = llm.openai_base_url(system_settings)
    except Exception:
        return ""
    if not url:
        return ""
    try:
        llm.assert_openai_base_url_allowed(url)
    except ValueError:
        return _INVALID_SAVED_BASE_URL_LABEL
    except Exception:
        return ""
    try:
        from urllib.parse import urlsplit
        return urlsplit(url).hostname or ""
    except Exception:
        return ""


def _openai_endpoint_kind(system_settings: dict | None = None) -> str:
    """`llm.openai_endpoint_kind()` の値。例外時は "openai"（既定）に倒す。`system_settings`（省略可）は `_openai_base_url_host` と同じ理由でそのまま渡す。"""
    try:
        return llm.openai_endpoint_kind(system_settings) or "openai"
    except Exception:
        return "openai"


def _ollama_url_choice(s: dict, system_settings: dict | None = None) -> dict:
    """個人設定の Ollama 接続先 `<select>` の選択肢（`{"allowed": [...], "default": "..."}` 形＋`legacy`）。空文字は「管理者の既定を使う」。
    `allowed` には許可ポリシー（loopback／admin allowlist）を満たす完全 URL（scheme 込み）だけを返す。利用者の現在の保存値が許可されていない場合は `allowed` に混ぜず `legacy` に別枠で返す。
    scheme が分かっている実際の URL（中央既定・利用者の現在の保存値）を、admin allowlist からの合成エントリより先に追加する。同一 host:port が既にある場合は、後から来た URL が `https://` で既存が `https://` でないときだけ置換する（合成の `http://` が https を上書きしない）。
    `system_settings`（省略可）: `ollama_url`（中央既定）と `llm._allowlisted_hosts()` の解決に使う（省略時は自分で読む）。
    """
    sysset = system_settings if system_settings is not None else store.get_system_settings()
    central_url = (sysset.get("ollama_url") or "").strip() or keys.DEFAULT_OLLAMA_URL
    allowlisted = llm._allowlisted_hosts(sysset)
    allowed: list[str] = []
    seen: dict[tuple[str, int], int] = {}  # host:port -> allowed 内の index。

    def _policy_valid(hp: tuple[str, int]) -> bool:
        return llm.is_loopback_host(hp[0]) or hp in allowlisted

    def _add(url: str | None):
        if not url:
            return
        hp = llm._canonical_host_port(url)
        if hp is None or not _policy_valid(hp):
            return
        full = url if "://" in url else f"http://{url}"
        idx = seen.get(hp)
        if idx is None:
            seen[hp] = len(allowed)
            allowed.append(full)
            return
        if full.startswith("https://") and not allowed[idx].startswith("https://"):
            allowed[idx] = full  # https が同一 host:port の http を置換する。

    # scheme が分かっている実URL（中央既定・利用者の現在値）を先に確定させ、admin allowlist からの `http://` 合成エントリは最後に未確定分だけ補う。
    _add(central_url)
    current = (s.get("ollama_url") or "").strip()
    _add(current)
    for host, port in sorted(allowlisted):
        _add(f"http://{llm.format_host_port(host, port)}")

    legacy = None
    if current:
        hp = llm._canonical_host_port(current)
        if hp is None or not _policy_valid(hp):
            legacy = current

    return {"allowed": allowed, "default": central_url, "legacy": legacy}


def _public_settings(s: dict) -> dict:
    # key_set は「今この設定で実際に使えるキーがあるか」を `keys.resolve_api_key`（中央/個人・A6/A7 込み）の結果で判定する。この関数内の system_settings 依存の解決（キー・agent 既定選択・cloud_provider・construct_id の A7 判定・モデルカタログ・Ollama allowlist）は同じスナップショットで行う。
    sys_s = store.get_system_settings()
    openai_key = keys.resolve_api_key("openai", s, system_settings=sys_s)
    saved_agent = s["agent"]
    return {"agent": saved_agent or agent_constructs.default_agent(sys_s),
            # web_search の管理者許可は `system_settings.web_search_allowed`（管理画面「プロバイダ＋接続先」タブ）が唯一の真実源で、ここでは調べ方ブロックの Web 検索行の表示条件（web/chat/menus.js）にだけ使う。実行に使うかどうかは `ChatReq.web_search` だけで決まる。
            "web_search_available": _web_search_admin_allowed(sys_s),
            # 判定を provider 選択・health と同じ `agent_constructs.is_real_api_key` に揃える（プレースホルダ文字列でなければ真）。
            "openai_key_set": agent_constructs.is_real_api_key(openai_key),
            # 接続先の種類（openai/azure/custom）とホスト名のみ（キー・パスは出さない）。ユーザーごとには変わらない。
            "openai_endpoint_kind": _openai_endpoint_kind(sys_s),
            "openai_base_url_host": _openai_base_url_host(sys_s),
            "ollama_url": s["ollama_url"],
            # 4 構成（agent_constructs）: 現在の構成と、この環境で選べる構成の一覧。画面はこの一覧だけを描画する。
            "codex_model_provider": s.get("codex_model_provider") or "",
            "construct_id": agent_constructs.construct_id(s, system_settings=sys_s),
            "constructs_available": agent_constructs.available_constructs(system_settings=sys_s),
            "cloud_provider": keys.selected_cloud_provider(sys_s),
            "personal_api_keys_allowed": keys.personal_keys_allowed(sys_s),
            # 個人設定の「外部連携」欄を出し分けるフラグ（既定 false・personal_api_keys_allowed と同型）。
            "user_api_keys_allowed": bool(sys_s.get("user_api_keys_allowed") or False),
            # 自己発行キーの 1 日あたり呼び出し上限（既定/上限・管理者統制）。発行フォームのプレースホルダ表示用。
            "user_api_keys_daily_quota_default": store.resolve_self_issued_daily_quota_cap(sys_s),
            # 個人の Ollama 接続先は許可ホスト一覧から選ぶ（許可ホスト一覧＋中央既定・完全 URL 保持）。
            "ollama_url_choice": _ollama_url_choice(s, system_settings=sys_s),
            # チャット画面のクイック入力例（管理者設定・`sherpa/chat_examples.py`）。None＝未設定（フロントの既定を使う）。配列（空含む）＝管理者が明示設定済み（空配列＝非表示）。
            "chat_examples": chat_examples.public_examples(sys_s)}




def _codex_ollama_probe(s: dict, sys_s: dict, model: str | None) -> dict:
    """Codex(Ollama) の接続テスト。実行時（`providers._select_provider`）と同じ接続先の解決と許可の確認をしてから、Ollama の `/api/tags` でモデルが取得済みかを見る（タグ無しは `:latest` とみなす）。"""
    import json as _json
    base = keys.resolve_ollama_url(s, system_settings=sys_s)
    try:
        llm.assert_ollama_url_allowed(base, system_settings=sys_s)
    except Exception:
        return {"ok": False, "provider": "codex", "model": model,
                "detail": "設定のローカルAIの接続先が許可されていません。設定画面で確認してください"}
    try:
        with llm.urlopen_no_redirect(llm.ollama_url(base, "/api/tags"), timeout=10) as r:
            names = {m.get("name") for m in (_json.loads(r.read()).get("models") or [])
                     if isinstance(m, dict)}
    except Exception as e:
        return {"ok": False, "provider": "codex", "model": model,
                "detail": f"ローカルAI（Ollama）に接続できません（{type(e).__name__}）"}
    if model and model not in names and f"{model}:latest" not in names:
        return {"ok": False, "provider": "codex", "model": model,
                "detail": f"ローカルAI（Ollama）にモデル {model} がありません（ollama pull で取得してください）"}
    return {"ok": True, "provider": "codex", "model": model,
            "detail": "接続OK（ローカルAI・codex login の状態は問いません）"}

@settings_router.get("/config", tags=["設定"], response_model=ConfigResponse)
def config_get(request: Request):
    """利用可能な AI プロバイダ情報（現在の設定を踏まえた provider_info）を返す。"""
    u = _current_user(request)
    return provider_info(store.get_settings(u["uid"]))


@settings_router.get("/settings", tags=["設定"], response_model=SettingsResponse)
def settings_get(request: Request):
    """現在ユーザーの設定を返す（API キーは有無のみ・値は返さない）。"""
    u = _current_user(request)
    return _public_settings(store.get_settings(u["uid"]))


@settings_router.put("/settings", tags=["設定"], response_model=SettingsResponse)
def settings_put(req: SettingsReq, request: Request):
    """現在ユーザーの設定を更新する（未指定フィールドは変更しない）。
    `agent` は許可された頭脳のみ（閉じた頭脳は 422）。`ollama_url` は宛先ポリシー（loopback／admin allowlist）に合わないと 422（到達可否の詳細は返さない）。管理画面で個人キーが許可されていない（`personal_api_keys_allowed` が偽）ときは、クラウド AI のキーを保存できず 422。
    """
    u = _current_user(request)
    uid = u["uid"]
    # この関数内の system_settings 依存の判定・解決（A6・A7・Ollama URL/allowlist・モデルカタログ）は同じスナップショットで行う。個人キーの実書込みだけは、`store.update_settings()` が書込み直前に advisory lock 付きで再確認する（`store.PersonalKeysDisallowedError`）。
    sys_s = store.get_system_settings()
    if not keys.personal_keys_allowed(sys_s):
        for _k in ("openai_api_key",):
            if getattr(req, _k) is not None:
                raise HTTPException(422, "個人 API キーは無効化されています（管理者が中央設定でキーを管理します）")
    if req.agent and (req.agent not in AGENT_PROVIDERS or req.agent in _CLOSED_CHAT_AGENTS):
        raise HTTPException(422, "agent は codex / simple のいずれか"
                                 "（Gemini・AWS Bedrock・AI なしの定型応答はチャットでは廃止しました）")
    # 標準 MVP は 4 構成だけを見せる。env で有効化していない外部 AI は保存させない。
    if req.agent and agent_constructs.runtime_blocked(req.agent):
        raise HTTPException(422, "この AI はこの環境では利用できません（管理者が有効化していません）")
    if req.agent == "codex":
        _codex_msg = required_tools.codex_cli_missing_message()
        if _codex_msg:
            raise HTTPException(422, _codex_msg)
    if req.codex_model_provider and req.codex_model_provider not in agent_constructs.CODEX_MODEL_PROVIDERS:
        raise HTTPException(422, "codex_model_provider は openai / ollama のいずれか")
    if req.ollama_url:
        try:
            llm.assert_ollama_url_allowed(req.ollama_url, system_settings=sys_s)
        except llm.SsrfBlocked:
            raise HTTPException(422, "指定された Ollama 接続先は許可されていません"
                                     "（admin が allowlist に登録した host:port のみ保存できます）")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        store.update_settings(uid, **fields)
    except store.PersonalKeysDisallowedError:
        # 事前チェック通過後、書込み直前に admin が無効化した（`store.update_settings` が同一トランザクションで再確認して fail-closed した）。
        raise HTTPException(422, "個人 API キーは無効化されています（管理者が中央設定でキーを管理します）")
    try:
        # API key の before/after は <set>/<unset>/<cleared> のみ記録する（値は保存しない）。
        _audit_settings_update(uid, fields)
    except Exception:
        _log.warning("audit write failed for settings.updated (best-effort)")
    return _public_settings(store.get_settings(uid))


def _audit_settings_update(uid: str, fields: dict) -> None:
    """settings 更新の監査（API key は状態のみ・値は保存しない）。"""
    secret_keys = {"openai_api_key"}
    changed: dict = {}
    for k, v in fields.items():
        if k in secret_keys:
            changed[k] = "<set>" if v else "<cleared>"
        elif k == "ollama_url" and v:
            # 多層防御: userinfo 付き URL は保存前に拒否されるが、監査ログ側でも念のため除去する（`llm._redact_url_for_error` と同じロジック）。
            changed[k] = llm._redact_url_for_error(v) or "<不正なURL>"
        else:
            changed[k] = v
    store.audit(uid, "settings.updated", "settings", f"settings:{uid}",
                detail={"changed_fields": list(fields.keys()), "changes": changed},
                outcome="success")


def _round_ollama_probe_detail(detail: str) -> str:
    """`POST /settings/test`（provider=ollama）の失敗理由を丸める。Connection refused/timeout/reset/DNS 失敗の区別を出さない（内部ホスト/ポートの生死判別に使われないため）。認証失敗（401 相当）だけは区別を残す（`graph_extract._http_detail` の `f"{e.code} ...: ..."` 形式に先頭一致する）。"""
    if detail.startswith("401"):
        return detail
    return "Ollama への接続に失敗しました（詳細は表示しません）"


class TestReq(BaseModel):
    provider: str  # openai / ollama / codex。
    openai_api_key: str | None = None  # 未入力なら保存済みキーで試す（入力時はそれで試す）。
    ollama_url: str | None = None
    # モデル名と接続先（kind/base_url/auth_header/api_version）の override はここに置かない。`/settings/test` はログイン済みなら誰でも呼べるため、任意の値を受けると実 probe への到達や中央キーの送信先指定に使われる。モデルは常に管理者の使えるモデル一覧の解決値で、接続先は保存済みの system_settings で probe する（入力中の接続先で試す機能は admin 専用の `POST /admin/settings/openai-endpoint-test`）。


@settings_router.post("/settings/test", tags=["設定"], response_model=SettingsTestResponse)
def settings_test(req: TestReq, request: Request):
    """API キー/モデルの接続テスト（1 回だけ最小リクエスト）。保存はしない。入力中のキーで試せる。
    返値 `{ok, provider, model, detail}`。ok=False の detail に実エラー（401=認証/429=クォータ/モデル不明 等）を載せる。
    provider=ollama は宛先ポリシー（loopback／admin allowlist）に合わないと接続せず 422 を返す。接続後の失敗は、到達可否の区別を出さず丸める（401 相当の認証失敗だけ区別を残す）。
    """
    from sherpa.ingest import graph_extract
    u = _current_user(request)
    s = store.get_settings(u["uid"])
    # system_settings 依存の解決（キー・モデル・URL・宛先許可）はこの 1 回のスナップショットで行う。
    sys_s = store.get_system_settings()
    # 前後の空白を除去してから比較する（実行時の判定と同じ）。
    prov = str(req.provider or "").strip().lower()
    pending = dict(s)
    # `ollama_url` は個人設定として残る欄で、送られてこなければ保存済みの値を使う。明示的な `""`（クリア＝中央既定に従う）はそのまま重ねる（`keys.resolve_ollama_url()` が空文字を既定へフォールバックする）。
    if req.ollama_url is not None:
        pending["ollama_url"] = req.ollama_url
    # モデル名は個人設定にも TestReq にも無く、管理者の使えるモデル一覧の解決値だけで probe する。
    if prov == "codex":  # Codex は CLI＝キー不要。CLI の有無とログイン状態を確認する（フル exec はしない）。
        import shutil
        import subprocess
        model = model_catalog.resolve_model("codex", "codex", None, system_settings=sys_s)
        # `codex login status` はモデル名を見ないため、文法として不正なモデル名（`CodexProvider` が実行時に拒否する値）を先に共通文法で確認する。
        if model and not model_catalog.CODEX_MODEL_NAME_RE.fullmatch(model):
            return {"ok": False, "provider": "codex", "model": model,
                    "detail": "モデル名の形式が不正です（使える文字: 英数字 . _ : / - ・64文字以内）"}
        if not shutil.which("codex"):
            return {"ok": False, "provider": "codex", "model": model, "detail": "codex CLI が見つかりません（インストール/PATH を確認）"}
        # 接続先が Azure 等（`openai_endpoint_kind() != "openai"`）の Codex(OpenAI) 構成は、`codex login status` ではなく env のキーで接続する（`sandbox.py::_codex_clean_env`）。`_select_provider` と判定を共有するため `providers._codex_openai_compat_block_reason` を呼び、入力中の未保存キー（`req.openai_api_key`）も明示 override として渡す。
        # Codex(Ollama) 構成: サンドボックス無効時は `_select_provider` と同じく fail-closed にする。それ以外は `login status` を見る。
        from sherpa import llm as _llm
        # 実行時（`_select_provider`）と同じ共通 resolver（`agent_constructs.codex_model_provider`）を通す。
        try:
            codex_provider_choice = agent_constructs.codex_model_provider(s)
        except agent_constructs.InvalidCodexModelProviderError as e:
            return {"ok": False, "provider": "codex", "model": model, "detail": str(e)}
        # Ollama 分岐を先に見る（`_select_provider` と同じ順序）。先に `openai_endpoint_kind` を評価すると、OpenAI 系設定の型破損で ValueError になり接続テストが偽陰性になる。
        if codex_provider_choice == "ollama":
            from sherpa.providers import _codex_ollama_sandbox_disabled_reason
            sandbox_reason = _codex_ollama_sandbox_disabled_reason()
            if sandbox_reason is not None:
                return {"ok": False, "provider": "codex", "model": model, "detail": sandbox_reason}
            # Codex(Ollama) は codex login を使わないため、実行時と同じ接続先へ届くか・モデルがあるかを確かめる。
            return _codex_ollama_probe(s, sys_s, model)
        else:
            # `sys_s` の openai_endpoint_kind/openai_base_url は JSONB で非文字列の破損値がありうる。`openai_endpoint_kind()` は型検査で ValueError を出しうるため、ここで捕捉して正直な失敗にする。
            try:
                _codex_kind = _llm.openai_endpoint_kind(sys_s)
            except ValueError:
                return {"ok": False, "provider": "codex", "model": model,
                        "detail": "接続先の設定が不正です。管理者に確認してください"}
        if _codex_kind != "openai":
            from sherpa.providers import _codex_openai_compat_block_reason
            probe_settings = {**s, "openai_api_key": req.openai_api_key or s.get("openai_api_key")}
            # 入力中の未保存キー（req.openai_api_key）は A6（personal_api_keys_allowed）の対象外で試せるよう、明示 override として渡す（保存・ログ出力はしない）。
            reason = _codex_openai_compat_block_reason(probe_settings, explicit_openai_api_key=req.openai_api_key,
                                                        system_settings=sys_s)
            if reason is not None:
                return {"ok": False, "provider": "codex", "model": model, "detail": reason}
            # 形式確認だけでは実失敗（キー無効・デプロイ名不在・権限不足・DNS 不到達）を「接続OK」と誤表示するため、実際に 1 回だけ最小リクエストする（Codex CLI は起動せず、Codex が使うのと同じ base_url/キー/デプロイ名への直接プローブ。`graph_extract._probe` が他プロバイダと共有する唯一の実 HTTP 経路）。Codex CLI 経由の生死判定は `make azure-smoke ARGS="--codex"`。
            try:
                keys.selected_cloud_provider(sys_s, strict=True)  # 入力中のキーでも廃止済み保存値では送信しない。
                resolved_key = probe_settings["openai_api_key"] or keys.resolve_api_key(
                    "openai", s, system_settings=sys_s, strict=True)
            except keys.InvalidCloudProviderConfigError as e:
                return {"ok": False, "provider": "codex", "model": model, "detail": str(e)}
            ok, detail = graph_extract._probe({"provider": "openai", "key": resolved_key, "model": model,
                                              "openai_endpoint_override": sys_s})
            return {"ok": ok, "provider": "codex", "model": model,
                    "detail": ("接続OK（Azure OpenAI 等: 実際に接続して確認済み・codex login の状態は問いません）"
                               if ok else detail)}
        try:
            r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=20)
            ok = r.returncode == 0
            detail = "接続OK" if ok else ((r.stderr or r.stdout or "未ログイン（codex login が必要）").strip()[:200])
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}"[:200]
        return {"ok": ok, "provider": "codex", "model": model, "detail": detail}
    if prov == "openai":
        model = model_catalog.resolve_model("openai", "chat", None, system_settings=sys_s)
        # この直後の probe（実 API 呼び出し）に使うため strict=True で解決する（課金を伴う接続テストを寛容なキー解決で実送信しない）。
        try:
            keys.selected_cloud_provider(sys_s, strict=True)  # 入力中のキーでも廃止済み保存値では送信しない。
            openai_key = req.openai_api_key or keys.resolve_api_key("openai", s, system_settings=sys_s, strict=True)
        except keys.InvalidCloudProviderConfigError as e:
            return {"ok": False, "provider": "openai", "model": model, "detail": str(e)}
        # env の OPENAI_API_KEY がプレースホルダのままだと分かりにくい 401 になるため、他の消費箇所と同じ `is_real_api_key` で早期に弾く（利用者が入力した実キーの扱いは変えない）。
        cfg = {"provider": "openai",
               "key": openai_key if agent_constructs.is_real_api_key(openai_key) else None,
               "model": model,
               # 接続先も含め、この probe 全体を入口で読んだ 1 つの `sys_s` だけで完結させる（`complete_json` が別途読み直すと旧キーを新接続先へ送る混在が起こりうる）。一般ユーザーの接続先 override は受け付けない。
               "openai_endpoint_override": sys_s}
    elif prov == "ollama":
        cfg = {"provider": "ollama", "url": keys.resolve_ollama_url(pending, system_settings=sys_s),
               "model": model_catalog.resolve_model("ollama", "chat", None, system_settings=sys_s)}
        # probe（実 I/O）の前に宛先ポリシーを検証する。ブロック時は probe せず汎用メッセージの 422 のみ返す。
        try:
            llm.assert_ollama_url_allowed(cfg["url"], system_settings=sys_s)
        except llm.SsrfBlocked:
            raise HTTPException(422, "指定された Ollama 接続先は許可されていません"
                                     "（admin が allowlist に登録した host:port のみ確認できます）")
    else:
        raise HTTPException(422, "provider は openai / ollama / codex のいずれか")
    if prov == "openai" and not cfg.get("key"):
        return {"ok": False, "provider": prov, "model": cfg["model"], "detail": keys.NO_CENTRAL_KEY_MESSAGE}
    ok, detail = graph_extract._probe(cfg)
    if prov == "ollama" and not ok:
        # Connection refused/timeout/reset/DNS の区別を出さない（401 相当の認証失敗だけ区別を残す）。
        detail = _round_ollama_probe_detail(detail)
    return {"ok": ok, "provider": prov, "model": cfg["model"],
            "detail": "接続OK" if ok else detail}


# /healthz。router に tags を持たせない（settings_router と同じ）。
healthz_router = APIRouter()

# 未 ready 中に未認証 /healthz が重なると、全リクエストが advisory lock に並んで各自 DDL 全文を実行し滞留しうる。再初期化はプロセス内 single-flight（非ブロッキング）にし、進行中なら試行せず即 503 を返す（sync endpoint は threadpool で並行実行されるため lock が要る）。
_schema_init_inflight = threading.Lock()

# env→system_settings シード再試行の single-flight。schema が ready のままシードだけ一時失敗した場合も、次の healthz で再試行できるようにする。
_seed_retry_inflight = threading.Lock()


@healthz_router.get("/healthz", tags=["システム"])
def healthz():
    """死活監視用エンドポイント（schema readiness 連動）。DB のスキーマが未適用なら一度だけ初期化を試み、それでも ready でなければ 503、ready なら 200 を返す。
    """
    if not store.schema_ready() and _schema_init_inflight.acquire(blocking=False):
        try:
            store.init_schema()
        except Exception:
            pass
        finally:
            _schema_init_inflight.release()
    # schema が ready のたびに env→system_settings のシードを再試行する（冪等・single-flight）
    if store.schema_ready() and _seed_retry_inflight.acquire(blocking=False):
        try:
            from sherpa import api as _api  # 循環回避のため関数内 import
            _api._seed_settings_from_env()
            _api._seed_ollama_url_from_env()
            _api._warn_central_ollama_not_allowed()
            _api._seed_openai_endpoint_from_env()
            _api._seed_depth_profile_from_env()
            _api._seed_screen_settings_from_env()
            _api._seed_user_agent_from_env()
            _api._seed_vlm_ollama_url_from_env()
            model_catalog.seed_catalog_once()
        except Exception:
            pass
        finally:
            _seed_retry_inflight.release()
    if not store.schema_ready():
        return JSONResponse(status_code=503, content={"ok": False, "detail": "schema not ready"})
    return {"ok": True}


# /（ルート）。

root_router = APIRouter()


@root_router.get("/", tags=["システム"])
def _root():
    """ルートアクセスをトップ画面（/ui/home.html・運営掲示板）へ redirect する。"""
    return RedirectResponse("/ui/home.html")
