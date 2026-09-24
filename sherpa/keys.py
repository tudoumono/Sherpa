"""クラウド AI プロバイダのキー／接続先の解決（唯一の真実源）。

設定の所有原則（`docs/proposals/2026-08-22-拡張構想（受領原文）.md` §ユーザー追加方針6）:
運用ポリシーと資格情報は system_settings（DB・中央）が唯一の真実源。env は初回起動時の
シード（`sherpa.api._seed_settings_from_env`）にのみ使い、以後は読まない。本モジュールは
env を一切読まない（`os.environ` を参照しない）。

A6（個人 API キー原則・`docs/08-実行権限と隔離.md` §7）: 個人には発行しない。
`personal_api_keys_allowed`（既定 false）が真、かつ本人の保存キーがある時だけ個人優先。
A7（クラウドプロバイダ排他選択）: `cloud_provider` で1つだけ選ぶ（openai/gemini/bedrock）。
非選択プロバイダの保存キー（個人・中央とも）は消さずに温存するが、解決結果は常に None
（＝使わない）。Ollama はローカル接続のため排他対象外（常時併用・`resolve_ollama_url`）。
"""
from __future__ import annotations

CLOUD_PROVIDERS = ("openai", "gemini", "bedrock")

# provider 名 → system_settings/user_settings の実キー列名（両テーブルで同名）。
_KEY_FIELD = {"openai": "openai_api_key", "gemini": "gemini_api_key", "bedrock": "bedrock_api_key"}

DEFAULT_CLOUD_PROVIDER = "openai"
DEFAULT_OLLAMA_URL = "http://localhost:11434"

# honest failure（黙って env を読まない・黙って他プロバイダへ倒れない）用の定型メッセージ。
NO_CENTRAL_KEY_MESSAGE = "管理者が AI プロバイダのキーを設定してください"


class InvalidCloudProviderConfigError(ValueError):
    """`cloud_provider` が非空の不正値のとき、`selected_cloud_provider(strict=True)` が送出する
    （黙って既定へ倒さない・実行時 honest failure 用）。"""


def _system_settings() -> dict:
    from sherpa import store   # 循環回避のため関数内 import（store もこのモジュールを import しない想定）
    return store.get_system_settings()


def selected_cloud_provider(system_settings: dict | None = None, *, strict: bool = False) -> str:
    """A7: 現在選択中のクラウドプロバイダ（未設定は既定 `openai`）。

    非空の不正値（env 誤記・旧データ等）は `strict=True` のときだけ `InvalidCloudProviderConfigError`
    を送出する（黙って既定へ倒すと、admin が意図した排他選択と異なるプロバイダが実行されうるため）。
    `strict=False`（既定）は表示/診断/監査など読み取り専用の呼び出しを壊れた設定でも動かし続ける
    ために既定 `openai` へ倒す。実行の唯一の入口（`providers/__init__.py::_select_provider` 経由の
    `agent_constructs.effective_agent(strict=True)`）だけが `strict=True` を使う。
    """
    s = system_settings if system_settings is not None else _system_settings()
    raw = s.get("cloud_provider")
    # 文字列以外の非 None（bool/int/list/dict 等・設定破損）は、`or ""` の truthiness 判定に
    # 先立って拒否する（false 等が `not value` で「未設定」に化けると、strict でも黙って既定へ
    # 倒れ、意図しないプロバイダでの課金につながるため）。
    if raw is not None and not isinstance(raw, str):
        if strict:
            raise InvalidCloudProviderConfigError(
                f"cloud_provider の値が不正です（{raw!r}）。選べる値: {', '.join(CLOUD_PROVIDERS)}。"
                "設定画面で選び直してください。")
        return DEFAULT_CLOUD_PROVIDER
    value = str(raw or "").strip().lower()
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
    """`cloud_provider` の生の保存値（未選択＝一度も PUT されていなければ None）。表示専用——値の
    妥当性は検証しない（非文字列の破損値も `str()` で可視化するだけ・妥当性検証は
    `selected_cloud_provider(strict=...)` の責務）。管理画面 `GET /admin/settings` の
    `cloud.provider_raw`（`CloudInfo`）が返す値の実体＝ここが唯一の真実源。
    """
    s = system_settings if system_settings is not None else _system_settings()
    raw = s.get("cloud_provider")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip() or None
    return str(raw)


def cloud_provider_explicitly_selected(system_settings: dict | None = None) -> bool:
    """`cloud_provider` を admin が明示的に触ったか（`cloud_provider_raw()` が非 None かどうか・
    `selected_cloud_provider()` の既定 openai への読み替えより前の状態）。

    FBK-1（2026-09-01）: 「選択中クラウド→だめなら Ollama」という auto 解決の黙った縮退を撤去し、
    fail-loud にする境界として使う——**明示選択済み**（この関数が真）のときだけ、その選択が解決
    できない場合に Ollama へ倒さず未接続（None）を返す（`llm.resolve_auto_provider`／
    `llm.select_provider` 参照）。生の保存値が無い（＝クラウドを一度も選んでいない・Ollama 専用
    構成）ときは、この関数は偽を返し、Ollama への自動フォールバックは従来どおり維持する。
    """
    return cloud_provider_raw(system_settings) is not None


def personal_keys_allowed(system_settings: dict | None = None) -> bool:
    """A6: 個人 API キーの利用を管理者が許可しているか（既定 false）。"""
    s = system_settings if system_settings is not None else _system_settings()
    return bool(s.get("personal_api_keys_allowed") or False)


def resolve_api_key(provider: str, user_settings: dict | None,
                    system_settings: dict | None = None, *, strict: bool = False) -> str | None:
    """provider（openai/gemini/bedrock）の実行時キーを解決する（env は読まない）。

    順序: (1) A7 排他 — `provider` が選択中のクラウドプロバイダでなければ常に None（保存キーは
    温存するが使わない）。(2) `personal_api_keys_allowed` が真で本人の保存キーがあればそれを使う。
    (3) それ以外は中央（system_settings）の同名キー。(4) どちらも無ければ None（honest failure は
    呼び出し側の責務・本関数は黙って別プロバイダへ倒れない）。

    `strict`（既定 False）: `selected_cloud_provider(strict=...)` へそのまま転送する。True のとき
    `cloud_provider` が非空の不正値なら `InvalidCloudProviderConfigError` を送出し、黙って既定
    （openai）へ倒れたキーで実送信することを防ぐ（意図しない課金の是正）。実際に送信する経路
    （`llm.select_provider`/`resolve_auto_provider`・graph 抽出/埋め込み/intent 分類/VLM 等）は
    `strict=True` を渡す。表示/監査など読み取り専用の呼び出しは既定のまま（壊れた設定でも
    画面/監査を止めない）。

    `system_settings`（省略可）: 呼び出し側が既に読んだ system_settings スナップショットを渡すと、
    それを使う（省略時は自分で読む）。`sherpa.llm.select_provider()` のように同一呼び出し内で
    複数回 provider/key/URL を解決する場合、各回で個別に読み直すと（TTL キャッシュはあるものの）
    途中の admin 更新で読み取り結果が食い違う窓が生まれるため、呼び出し側が1回読んだスナップ
    ショットを使い回せるようにする（RV 是正）。
    """
    provider = str(provider).strip().lower()
    field = _KEY_FIELD.get(provider)
    if field is None:
        raise ValueError(f"unknown cloud provider: {provider!r}")
    sys_s = system_settings if system_settings is not None else _system_settings()
    if selected_cloud_provider(sys_s, strict=strict) != provider:
        return None
    us = user_settings or {}
    if personal_keys_allowed(sys_s):
        personal = us.get(field)
        if personal:
            return personal
    central = sys_s.get(field)
    return central or None


def resolve_ollama_url(user_settings: dict | None, system_settings: dict | None = None) -> str:
    """Ollama 接続先（A7 排他の対象外・常時併用）。個人保存 → 中央既定 → 組み込み既定の順。env は
    読まない。`system_settings`（省略可）は `resolve_api_key` と同じ理由でスナップショット共有用。"""
    us = user_settings or {}
    personal = us.get("ollama_url")
    if personal:
        return personal
    sys_s = system_settings if system_settings is not None else _system_settings()
    return sys_s.get("ollama_url") or DEFAULT_OLLAMA_URL
