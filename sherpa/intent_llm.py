"""intent（レンズ）の LLM 分類。

heuristic（`chat_router`）が確信を持てない曖昧時だけ chat_service が呼ぶ。安価モデルを使い、本文テキストのみ送信する。
未接続・失敗・不正応答は None を返し、呼び元は clarify（ask_user 確認）へ縮退する。
HTTP/補完は `graph_extract.complete_json` を再利用する（`_complete` 経由で差し替え可）。
"""
from __future__ import annotations

import json
import logging

from . import llm

_log = logging.getLogger("sherpa")

_LENSES = ("impact", "troubleshoot", "qa", "author")
_SYS = ("あなたは社内ナレッジ検索の意図分類器です。ユーザの発話を次の4つの『調べ方』のどれかに分類し、"
        'JSON だけを返す: {"lens":"impact|troubleshoot|qa|author","confident":true|false}。'
        "impact=変更したときの影響範囲（〜を変えたら何に波及するか）／"
        "troubleshoot=不具合・エラー・異常の原因／qa=仕様・定義・内容の問い合わせ／"
        "author=資料の作成（調べた内容を Excel/Word/PowerPoint 等のファイルにしてほしい依頼）。"
        "どれとも判断しづらい場合は confident=false。")


def _cfg(settings: dict | None, *, system_settings: dict | None = None) -> dict | None:
    """分類用の安価モデル cfg（provider/key/model/url 形）。鍵/URL 無しは None。

    プロバイダ選択は `llm.select_provider`。クラウドを選んでいない構成でだけ既定 `ollama_url` も可用とみなし、
    クラウド明示選択時にそのプロバイダで解決できなければ Ollama へ倒さず None。
    モデルは `model_catalog`（用途 `intent`）から解決する。`system_settings` 省略時はここで1回だけ読む。
    """
    from . import model_catalog
    from . import store as _store
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()

    def O(key):
        return {"provider": "openai", "key": key,
                "model": model_catalog.resolve_model("openai", "intent", None, system_settings=sys_s),
                "openai_endpoint_override": sys_s}

    def L(url):
        return {"provider": "ollama", "url": url,
                "model": model_catalog.resolve_model("ollama", "intent", None, system_settings=sys_s)}

    # `cloud_provider` が不正値のときは既定（openai）へ倒さず None（分類を送信しない）
    from . import keys as _keys
    try:
        return llm.select_provider(settings, openai=O, ollama=L, system_settings=sys_s,
                                   strict=True)
    except _keys.InvalidCloudProviderConfigError as e:
        # None→clarify へ静かに縮退するが、管理者が診断できるようログは残す
        _log.warning("intent_llm._cfg: cloud_provider が不正なため intent 分類を無効化しました: %s", e)
        return None


def _complete(system: str, user: str, cfg: dict) -> str:
    """補完（JSON 文字列）。`graph_extract.complete_json` に委譲する。短い timeout（15s）で SSE を固めない。"""
    from .ingest.graph_extract import complete_json
    return complete_json(system, user, cfg, timeout=15)


def classify(message: str, settings: dict | None, *,
            user_id: str | None = None, world: str | None = None,
            system_settings: dict | None = None,
            conversation_id: int | None = None) -> dict | None:
    """曖昧メッセージ → {"lens","confident"} | None（未接続・失敗・不正は None＝clarify へ）。

    `_complete` 呼び出しを `metering.acc_begin()`/`acc_end()` で囲み `kind='intent'` で記録する（失敗時も記録）。
    `system_settings` は読み済みのスナップショットがあれば渡す。`conversation_id` は会話別集計キー（省略可）。
    `_cfg()` の例外もここで捕捉して None に丸める（クラス名だけをログに残し、秘密を含みうる詳細は出さない）。
    """
    msg = (message or "").strip()
    if not msg:
        return None
    try:
        cfg = _cfg(settings, system_settings=system_settings)
    except Exception as e:
        _log.warning("intent_llm.classify: 設定解決に失敗しました（clarify へ縮退します）: %s",
                     e.__class__.__name__)
        return None
    if not cfg:
        return None
    from . import metering
    metering.acc_begin()
    try:
        try:
            data = json.loads(_complete(_SYS, msg, cfg))
            lens = data.get("lens") if isinstance(data, dict) else None
            if lens in _LENSES:
                conf = data.get("confident", True)
                if isinstance(conf, str):
                    conf = conf.strip().lower() in ("true", "1", "yes")
                return {"lens": lens, "confident": bool(conf)}
        except Exception:
            return None
        return None
    finally:
        tokens, n = metering.acc_end()
        if n:
            metering.record("intent", cfg["provider"], cfg["model"], tokens,
                            user_id=user_id, world=world, calls=n,
                            conversation_id=conversation_id)
