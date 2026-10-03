"""クラウド AI プロバイダのキー／接続先の解決。

運用ポリシーと資格情報は system_settings（DB・中央）が唯一の真実源。env は初回起動時のシード（`sherpa.api._seed_settings_from_env`）
にのみ使い、本モジュールは env を読まない。個人キーは `personal_api_keys_allowed`（既定 false）が真で本人の保存キーがある
ときだけ優先。クラウド側の選択肢は openai（Azure OpenAI を含む）だけで、Ollama は対象外（常時併用・`resolve_ollama_url`）。
閉じたプロバイダ（gemini/bedrock）が `cloud_provider` に残っていても例外にせず「未選択」として扱う（`retired_cloud_provider`）。
設計: docs/design/settings.md「プロバイダ＋接続先」
"""
from __future__ import annotations

CLOUD_PROVIDERS = ("openai",)

# 閉じたクラウドプロバイダ（保存値として残りうるだけ・選べない）
RETIRED_CLOUD_PROVIDERS = ("gemini", "bedrock")

# provider 名 → system_settings/user_settings の実キー列名
_KEY_FIELD = {"openai": "openai_api_key"}

DEFAULT_CLOUD_PROVIDER = "openai"
DEFAULT_OLLAMA_URL = "http://localhost:11434"

# honest failure 用の定型メッセージ（黙って env を読まない・他プロバイダへ倒れない）
NO_CENTRAL_KEY_MESSAGE = "管理者が AI プロバイダのキーを設定してください"


class InvalidCloudProviderConfigError(ValueError):
    """`cloud_provider` が非空の不正値のとき、`selected_cloud_provider(strict=True)` が送出する。"""


def _system_settings() -> dict:
    from sherpa import store  # 循環回避のため関数内 import
    return store.get_system_settings()


def selected_cloud_provider(system_settings: dict | None = None, *, strict: bool = False) -> str:
    """現在選択中のクラウドプロバイダ（未設定は既定 `openai`）。

    非空の不正値（廃止済みの gemini/bedrock の保存値を含む）は `strict=True` のときだけ `InvalidCloudProviderConfigError`。
    `strict=False`（既定）は表示/診断/監査向けに既定 `openai` へ倒す。実際に送る入口・接続テストなど、拒否が必要な呼び出しはすべて `strict=True` で呼ぶ。
    """
    s = system_settings if system_settings is not None else _system_settings()
    raw = s.get("cloud_provider")
    # 文字列以外の非 None（設定破損）は truthiness 判定に先立って拒否する
    if raw is not None and not isinstance(raw, str):
        if strict:
            raise InvalidCloudProviderConfigError(
                f"cloud_provider の値が不正です（{raw!r}）。選べる値: {', '.join(CLOUD_PROVIDERS)}。"
                "設定画面で選び直してください。")
        return DEFAULT_CLOUD_PROVIDER
    value = str(raw or "").strip().lower()
    if value in RETIRED_CLOUD_PROVIDERS:
        # 廃止済みプロバイダ: 表示（strict=False）は既定へ倒すが、実行（strict=True）は黙って OpenAI へ切り替えず未接続にする
        if strict:
            raise InvalidCloudProviderConfigError(
                f"cloud_provider に廃止済みのプロバイダ（{value!r}）が保存されています。"
                f"選べる値: {', '.join(CLOUD_PROVIDERS)}。設定画面で選び直してください。")
        return DEFAULT_CLOUD_PROVIDER
    if not value:
        return DEFAULT_CLOUD_PROVIDER
    if value in CLOUD_PROVIDERS:
        return value
    if strict:
        raise InvalidCloudProviderConfigError(
            f"cloud_provider の値が不正です（{value!r}）。選べる値: {', '.join(CLOUD_PROVIDERS)}。"
            "設定画面で選び直してください。")
    return DEFAULT_CLOUD_PROVIDER


def cloud_provider_raw(system_settings: dict | None = None) -> str | None:
    """`cloud_provider` の生の保存値（未選択なら None）。表示専用で妥当性は検証しない（`cloud.provider_raw` の実体）。"""
    s = system_settings if system_settings is not None else _system_settings()
    raw = s.get("cloud_provider")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip() or None
    return str(raw)


def retired_cloud_provider(system_settings: dict | None = None) -> str | None:
    """保存済みの `cloud_provider` が閉じたプロバイダ（gemini/bedrock）ならその名前、それ以外は None（管理画面の警告用）。"""
    raw = cloud_provider_raw(system_settings)
    value = (raw or "").strip().lower()
    return value if value in RETIRED_CLOUD_PROVIDERS else None


def cloud_provider_explicitly_selected(system_settings: dict | None = None) -> bool:
    """`cloud_provider` を admin が明示的に触ったか（`cloud_provider_raw()` が非 None か）。

    真のときだけ、選択が解決できない場合に Ollama へ倒さず未接続（None）を返す（`llm.resolve_auto_provider`/`llm.select_provider`）。
    偽（クラウドを一度も選んでいない・Ollama 専用構成）なら Ollama への自動フォールバックを維持する。
    """
    return cloud_provider_raw(system_settings) is not None


def personal_keys_allowed(system_settings: dict | None = None) -> bool:
    """個人 API キーの利用を管理者が許可しているか（既定 false）。"""
    s = system_settings if system_settings is not None else _system_settings()
    return bool(s.get("personal_api_keys_allowed") or False)


def resolve_api_key(provider: str, user_settings: dict | None,
                    system_settings: dict | None = None, *, strict: bool = False) -> str | None:
    """provider（openai）の実行時キーを解決する（env は読まない）。

    順序: (1) `provider` が選択中のクラウドプロバイダでなければ常に None。(2) `personal_api_keys_allowed` が真で本人の保存キーが
    あればそれ。(3) それ以外は中央（system_settings）の同名キー。(4) どちらも無ければ None（別プロバイダへ倒れない）。
    `strict` は `selected_cloud_provider(strict=...)` へ転送する（実際に送信する経路は `strict=True`）。
    `system_settings` は呼び出し側が読み済みのスナップショットを共有するための引数（省略時は自分で読む）。
    """
    provider = str(provider).strip().lower()
    field = _KEY_FIELD.get(provider)
    if field is None:
        raise ValueError(f"unknown cloud provider: {provider!r}")
    sys_s = system_settings if system_settings is not None else _system_settings()
    if selected_cloud_provider(sys_s, strict=strict) != provider:
        return None
    if retired_cloud_provider(sys_s) is not None:
        # 廃止済みプロバイダの保存値は送信に使える鍵を返さない（管理者が選び直すまで送信できない）
        return None
    us = user_settings or {}
    if personal_keys_allowed(sys_s):
        personal = us.get(field)
        if personal:
            return personal
    central = sys_s.get(field)
    return central or None


def resolve_ollama_url(user_settings: dict | None, system_settings: dict | None = None) -> str:
    """Ollama 接続先（常時併用）。個人保存 → 中央既定 → 組み込み既定の順。env は読まない。"""
    us = user_settings or {}
    personal = us.get("ollama_url")
    if personal:
        return personal
    sys_s = system_settings if system_settings is not None else _system_settings()
    return sys_s.get("ollama_url") or DEFAULT_OLLAMA_URL
