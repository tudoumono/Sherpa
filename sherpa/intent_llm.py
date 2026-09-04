"""intent（レンズ）の LLM 分類シーム（hybrid の Tier2・§3 "intent も Codex 化"・2026-06-30）。

heuristic（`chat_router`）が確信を持てない**曖昧時だけ** chat_service が呼ぶ＝コスト最小。provider 非依存
（`llm.select_provider`）で**安価モデル**を使う（分類は軽い）。未接続/失敗/不正応答は **None** を返し、
呼び元は clarify（ask_user 確認）へフォールバックする。**本文テキストのみ送信**（CLAUDE.md）。
HTTP/補完は `graph_extract.complete_json`（テスト差し替え seam）を再利用＝`_complete` 経由で本モジュールも差し替え可。
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
    """分類用の安価モデル cfg（complete_json が解釈する provider/key/model/url 形）。鍵/URL 無しは None。

    provider 選択は `graph_extract` と同じ規約（`llm.select_provider`）＝クラウドを一度も選んで
    いない構成でだけ既定 `ollama_url`(localhost) も可用とみなす（Ollama 稼働中なら分類に使う／
    不在なら接続即拒否→ classify は None→clarify にフォールバック・無害）。A7 でクラウドを
    明示選択している場合は、そのプロバイダで解決できなければ Ollama へは倒さず None
    （FBK-1・fail-loud・抽出層と一貫）。

    モデルは管理者の使えるモデル一覧（`model_catalog`・用途 `intent`）から解決する（個人設定の
    プロバイダ/モデル選択は読まない）。bedrock ファクトリは従来どおり無し＝bedrock 選択時は
    None→clarify（既存の縮退と同じ）。

    `system_settings`（省略可）が無ければここで1回だけ読み、プロバイダ選択
    （`llm.select_provider`）・モデル解決（`model_catalog.resolve_model`）・`complete_json` の
    送信時接続先解決（`openai_endpoint_override`）まで同じスナップショットで揃える
    （`graph_extract.available()` と同じ形）。
    """
    from . import model_catalog
    from . import store as _store
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()

    def O(key):
        return {"provider": "openai", "key": key,
                "model": model_catalog.resolve_model("openai", "intent", None, system_settings=sys_s),
                "openai_endpoint_override": sys_s}

    def G(key):
        return {"provider": "gemini", "key": key,
                "model": model_catalog.resolve_model("gemini", "intent", None, system_settings=sys_s)}

    def L(url):
        return {"provider": "ollama", "url": url,
                "model": model_catalog.resolve_model("ollama", "intent", None, system_settings=sys_s)}

    # 意図しない課金の是正: `cloud_provider`（A7）が非空の不正値のときは黙って既定（openai）へ
    # 倒れたキーで分類を送信しない。intent 分類は本来「主AIがローカルでも分類だけ課金され得る」
    # 独立経路のため、失敗は本関数の既存契約どおり None（呼び元は clarify へ縮退）に寄せる。
    from . import keys as _keys
    try:
        return llm.select_provider(settings, openai=O, gemini=G, ollama=L, system_settings=sys_s,
                                   strict=True)
    except _keys.InvalidCloudProviderConfigError as e:
        # 利用者向けにはしない（既存契約どおり None→clarify へ静かに縮退）が、黙って握り潰さず
        # 管理者が診断できるログを残す（strict 例外の黙殺の是正）。
        _log.warning("intent_llm._cfg: cloud_provider が不正なため intent 分類を無効化しました: %s", e)
        return None


def _complete(system: str, user: str, cfg: dict) -> str:
    """補完（JSON 文字列）。`graph_extract.complete_json` に委譲（テストは本関数を差し替え可）。

    intent 分類は軽いので**短い timeout**（SSE を固めない・Codex RV Med）＝既定 90s でなく 15s。
    """
    from .ingest.graph_extract import complete_json
    return complete_json(system, user, cfg, timeout=15)


def classify(message: str, settings: dict | None, *,
            user_id: str | None = None, world: str | None = None,
            system_settings: dict | None = None) -> dict | None:
    """曖昧メッセージ → {"lens","confident"} | None（未接続/失敗/不正は None＝clarify へ）。

    S1（2026-07-15-LLMオーケストレーション実装計画.md §3）: `_complete` 呼び出しを
    `metering.acc_begin()`/`acc_end()` で囲み、`kind='intent'` で1行記録する（計測有効時のみ）。
    JSON パース失敗時も finally で記録される（トークンは消費済み）。`classify` 自体は非raiseのまま
    （`metering.record` は例外を出さない）。

    `system_settings`（省略可）: 呼び出し側が既に読んだスナップショットがあれば渡す（省略時は
    `_cfg` がこの呼び出し内で1回だけ読む）。

    RV1（FBK-1・境界回帰#6・2026-09-01）: `_cfg()` 自体が送出する例外（`InvalidCloudProviderConfigError`
    以外・例えば `store.get_system_settings()` の DB 一時障害）もここで捕捉し None に丸める——
    intent 分類は本来チャット全体を止めてよい経路ではなく（既存契約どおり Tier1/確認カードへ
    継続する）、捕捉し損ねると設定 DB の一時障害がチャット応答全体をクラッシュさせてしまう。
    例外内容は秘密を含みうる詳細（str(e)）を出さず、クラス名だけをログに残す。
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
                if isinstance(conf, str):               # "false"/"true" 文字列も正しく解釈（bool("false")==True の罠・RV MED）
                    conf = conf.strip().lower() in ("true", "1", "yes")
                return {"lens": lens, "confident": bool(conf)}
        except Exception:
            return None
        return None
    finally:
        tokens, n = metering.acc_end()
        if n:
            metering.record("intent", cfg["provider"], cfg["model"], tokens,
                            user_id=user_id, world=world, calls=n)
