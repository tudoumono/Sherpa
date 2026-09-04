"""バックエンド健全性チェック（ナビの状態ドット＋管理者のシステム状態画面）。

- 各コンポーネントを短いタイムアウトで ping し、結果を TTL キャッシュ（既定 15 秒）。
  状態ドットが全ページから定期ポーリングするため、キャッシュでストア負荷を一定に抑える。
- 全体 status の決め方＝コンポーネント毎の「落ちたときの影響」を集約:
    Postgres → down（会話・認証・台帳が使えない＝アプリの根幹）
    Neo4j / Elasticsearch → degraded（影響分析・検索など一部機能の制限）
    Codex / OpenAI / Ollama → 影響なし（環境によって使わない構成が正常のため、
    管理画面に参考情報として出すだけで status には反映しない）
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime
import json
import logging
import os
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import keys   # トップレベル import 安全（keys.py はモジュールレベルで sherpa 内を import しない）

_TTL = float(os.environ.get("SHERPA_HEALTH_TTL", "15"))
# 既定は短めに固定する（不達時に lock 内で直列 ping する全コンポーネント分の待ち時間上限に
# なるため。健全時の挙動には影響しない）。
_TIMEOUT = float(os.environ.get("SHERPA_HEALTH_TIMEOUT", "1"))

_LEVELS = {"ok": 0, "degraded": 1, "down": 2}

_STORE_HINT = "make up（docker compose up -d）でストアを再起動してください"

_logger = logging.getLogger(__name__)


def _ping_postgres() -> None:
    import psycopg

    from . import store
    # statement_timeout でクエリ側の上限も保証する（connect_timeout は接続確立のみをカバー）。
    with psycopg.connect(store._dsn(), connect_timeout=max(1, int(_TIMEOUT)),
                          options=f"-c statement_timeout={int(_TIMEOUT * 1000)}") as conn:
        conn.execute("SELECT 1")


def _ping_neo4j() -> None:
    from neo4j import GraphDatabase

    from .ingest import world_neo4j
    env = world_neo4j._env()
    with GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]),
                              connection_timeout=_TIMEOUT,
                              connection_acquisition_timeout=_TIMEOUT) as driver:
        driver.verify_connectivity()


def _ping_es() -> None:
    # urllib の timeout は socket（接続＋読み取り）単位。read 全体の deadline ではないが、
    # cluster health は小さい応答なので実用上十分。
    from . import es_index
    with urllib.request.urlopen(es_index._url() + "/_cluster/health", timeout=_TIMEOUT) as r:
        json.loads(r.read())


def _ping_codex() -> None:
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI が見つかりません")


def _ping_openai() -> None:
    # RV MED（2026-08-18 Codex RV 2巡目 指摘3）: 以前は `"REPLACE_ME" in key`（部分一致）で、
    # たまたま "REPLACE_ME" という文字列を含む実キーまで誤って未設定扱いにし得た。判定は
    # `agent_constructs.is_real_api_key`（完全一致ベース・provider 選択と共有）に揃える。
    # env を直接読まず `sherpa.keys.resolve_api_key`（中央設定・A7 排他込み）を経由する。
    from . import agent_constructs, keys
    if not agent_constructs.is_real_api_key(keys.resolve_api_key("openai", None)):
        raise RuntimeError("OpenAI の API キーが未設定です（管理画面で設定してください）")


def _aws_credentials_file() -> Path:
    return Path.home() / ".aws" / "credentials"


class _NotApplicable(RuntimeError):
    """「対象外（未設定/未選択）」の申告用（2026-09-04 実利用フィードバック）。

    失敗（WARNING・状態画面で赤）と区別する: 使っていないプロバイダを毎回の health check で
    WARNING ログに流すのはノイズで、実障害の行が埋もれる。`_check_one` はこれを ok=True・
    detail「対象外（…）」・DEBUG ログとして扱う。"""


def _ping_bedrock() -> None:
    """**ネットワークに一切出ない**軽量チェック: 中央キー、または SigV4 の静的な手掛かりの**存在**だけを見る。

    Codex RV 指摘（2026-07-02）: 旧実装は `boto3.session.Session().get_credentials()` を呼んでいたが、
    環境によっては EC2/ECS の instance metadata service（IMDS）への外向き通信を伴いうる＝
    「ネットワークに出ない」の約束を破る。boto3 は一切 import/呼び出しせず、キーの有無と
    `~/.aws/credentials` の存在確認のみに留める（実際に資格情報として有効かどうかまでは確認しない
    ＝軽量チェックの範囲）。per-user 設定の API キーまでは見ない（未設定表示でも問題ない）。
    env の Bearer キーは直接読まない（`sherpa.keys.resolve_api_key` が中央設定・A7 排他を経て解決する）。

    A7（クラウドプロバイダ排他選択）は SigV4 ヒントの確認**前**で確定する＝bedrock を選択していない
    端末に AWS 認証情報が残っているだけで「利用可能」と誤判定しない。
    """
    from . import keys, store
    # RV 4巡目 #7: A7 判定とキー解決を同じスナップショットで行う（別々に読み直すと、途中の
    # admin 更新で「選択中」と「キーあり」の判定が食い違う窓ができる）。
    sys_s = store.get_system_settings()
    if keys.selected_cloud_provider(sys_s) != "bedrock":
        raise _NotApplicable("クラウドプロバイダとして未選択")
    if keys.resolve_api_key("bedrock", None, system_settings=sys_s):
        return
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return
    if _aws_credentials_file().exists():
        return
    raise RuntimeError(
        "Bedrock 認証情報（中央設定のキー、または AWS 認証情報/~/.aws/credentials）が見つかりません")


def _ping_ollama() -> None:
    # urllib の timeout は socket（接続＋読み取り）単位。read 全体の deadline ではないが、
    # tags 一覧は小さい応答なので実用上十分。
    # R2a: 直 urlopen ではなく `llm.ollama_url` 経由で URL を組み立てる＝SSRF 宛先ポリシー
    # （`llm.assert_ollama_url_allowed`）を通す。ブロック時は `SsrfBlocked`（ValueError 派生）が
    # 上がり、`_check_one` の既存 broad except で ok=False の degrade に乗る（例外は落ちない）。
    # R2a #3（2026-07-14）: `llm.urlopen_no_redirect` 経由にする＝allowlist 通過後の応答が 3xx
    # redirect で allowlist 外へ誘導されても追跡しない（llm.py docstring 参照）。
    # env `OLLAMA_URL` は直接読まない（`sherpa.keys.resolve_ollama_url` が中央設定を見る）。
    from . import keys, llm, store
    sys_s = store.get_system_settings()
    configured = bool(sys_s.get("ollama_url"))   # 中央設定に接続先があるか（既定 localhost は「未設定」扱い）
    base = keys.resolve_ollama_url(None, system_settings=sys_s)
    try:
        with llm.urlopen_no_redirect(llm.ollama_url(base, "/api/tags"), timeout=_TIMEOUT) as r:
            json.loads(r.read())
    except Exception as e:
        if not configured:
            # 接続先を設定していない環境（Ollama を使っていない）で、既定の localhost に応答が
            # 無いのは正常＝対象外。設定済みで落ちている場合だけ本物の失敗として上へ。
            raise _NotApplicable(f"接続先が未設定（既定 {base} にも応答なし）") from e
        raise


# (id, 表示名, 落ちたときの全体への影響, ping, 対処ヒント)
COMPONENTS = [
    ("postgres", "PostgreSQL（会話・ユーザー・台帳）", "down", _ping_postgres, _STORE_HINT),
    ("neo4j", "Neo4j（ナレッジグラフ・影響分析）", "degraded", _ping_neo4j, _STORE_HINT),
    ("elasticsearch", "Elasticsearch（全文検索）", "degraded", _ping_es, _STORE_HINT),
    ("codex", "Codex CLI（AIエージェント）", "none", _ping_codex,
     "codex CLI の導入とログインを確認してください（使わない構成なら対応不要）"),
    # env は初回シードのみで実行時には読まない（`keys.py` の設定所有原則）ため、ヒントも env
    # ではなく管理画面へ誘導する。`_AI_COMPONENTS` の openai 行と同じ `keys.NO_CENTRAL_KEY_MESSAGE`
    # を使う（表記ゆれ防止）。
    ("openai", "OpenAI API キー", "none", _ping_openai,
     keys.NO_CENTRAL_KEY_MESSAGE + "（Codex/ローカルLLM 利用なら対応不要）"),
    ("bedrock", "AWS Bedrock（Claude）", "none", _ping_bedrock,
     "サーバ側 env に AWS_BEARER_TOKEN_BEDROCK か AWS 認証情報を設定してください"
     "（設定画面でユーザー毎に API キーを入れている場合は未設定表示でも問題なし／使わない構成なら対応不要）"),
    ("ollama", "ローカルLLM（Ollama）", "none", _ping_ollama,
     "ollama serve の起動を確認してください（使わない構成なら対応不要）"),
]

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}


def _classify(e: BaseException) -> str:
    """例外を短い日本語分類に正規化する（DSN・Neo4j URI 等の接続情報が str(e) に
    含まれうるため、生の例外文字列を detail に出さない・分類ラベルのみを返す）。

    urllib は接続系エラーを URLError でラップする（reason に原因例外を持つ）ので、
    e 自体に加えて __cause__ / reason も1段見る。
    """
    for c in (e, getattr(e, "__cause__", None), getattr(e, "reason", None)):
        if c is None:
            continue
        if isinstance(c, (TimeoutError, socket.timeout)):
            return "タイムアウト"
        if isinstance(c, ConnectionRefusedError):
            return "接続拒否（サービス停止の可能性）"
        if isinstance(c, socket.gaierror):
            return "名前解決に失敗"
    text = str(e).lower()
    if "auth" in text or "password" in text or "unauthorized" in text:
        return "認証失敗"
    return "エラー"


def _check_one(comp_id, label, impact, ping, hint) -> dict:
    t0 = time.monotonic()
    try:
        ping()
        ok, detail = True, None
    except _NotApplicable as e:
        ok, detail = True, f"対象外（{e}）"   # 使っていない構成＝正常。WARNING を出さない
        _logger.debug("health check not applicable: %s: %s", comp_id, e)
    except Exception as e:
        ok = False
        detail = f"{_classify(e)}（{type(e).__name__}）"
        _logger.warning("health check failed: %s: %s", comp_id, e)
    out = {"id": comp_id, "label": label, "impact": impact, "ok": ok,
           "detail": detail, "latency_ms": int((time.monotonic() - t0) * 1000)}
    if not ok:
        out["hint"] = hint
    return out


def _check_one_ai(comp_id, label, impact, ping, hint) -> dict:
    """`_check_one` の AI 専用版（`ai_snapshot` からのみ使う・admin 専用画面）。

    `_check_one` は例外を `_classify()` で短い分類ラベルへ丸める（postgres/neo4j/es 等は
    DSN・接続 URI が `str(e)` に含まれうるため、生の例外文字列を出さない契約）。一方
    `_ai_check_*`（`_AI_COMPONENTS`）は各関数の契約として、例外メッセージに静的な案内文言
    （`keys.NO_CENTRAL_KEY_MESSAGE`・起動時シードの env 検証エラー等）か `_probe`→`_safe_detail()`
    経由で既にマスク・URL 反射伏せ字済みの理由文字列しか乗せない＝丸めず例外メッセージをそのまま
    使い、`docs/manual/41-運用Runbook.md` S8「状態ページの「OpenAI API」行にも同じ理由が出る」
    契約を満たす。

    ただし想定外の生の例外（`_safe_detail` を経由しない `_ai_check_ollama`・`_ai_check_codex`
    ＝それぞれ urllib の例外・subprocess の stderr/stdout をそのまま使う経路等）が URL の
    userinfo・query token・DSN 等を含んだまま上がってくる可能性への多層防御として、
    `_safe_detail` と同じ redaction パイプライン（`graph_extract._mask_secrets()` の
    Bearer/api-key/sk- トークン等の一般パターン・`graph_extract._redact_reflected_urls()` の
    URL 一律伏せ字）を通してから detail・ログの**両方**に使う（`secret` 指定なし＝個別の値の
    完全一致は見ない・パターン一致のみ）。生の例外オブジェクト `e` はログへ一切出さない
    （redaction 後の文字列だけを使う）。"""
    from .ingest.graph_extract import _mask_secrets, _redact_reflected_urls
    t0 = time.monotonic()
    try:
        ping()
        ok, detail = True, None
    except Exception as e:
        ok = False
        detail = _mask_secrets(str(e), None)
        detail = _redact_reflected_urls(detail, None)
        detail = detail or f"{_classify(e)}（{type(e).__name__}）"
        _logger.warning("health check failed: %s: %s", comp_id, detail)
    out = {"id": comp_id, "label": label, "impact": impact, "ok": ok,
           "detail": detail, "latency_ms": int((time.monotonic() - t0) * 1000)}
    if not ok:
        out["hint"] = hint
    return out


def _compute() -> dict:
    components = [_check_one(*c) for c in COMPONENTS]
    level = 0
    for c in components:
        if not c["ok"] and c["impact"] != "none":
            level = max(level, _LEVELS[c["impact"]])
    status = next(k for k, v in _LEVELS.items() if v == level)
    return {
        "status": status,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "ttl_seconds": _TTL,
        "components": components,
    }


def snapshot(force: bool = False) -> dict:
    """全コンポーネントの健全性（TTL キャッシュ・lock で single-flight）。"""
    with _lock:
        if not force and _cache["data"] is not None and time.monotonic() - _cache["at"] < _TTL:
            return _cache["data"]
        data = _compute()
        _cache["at"] = time.monotonic()
        _cache["data"] = data
        return data


def summary(force: bool = False) -> dict:
    """状態ドット用の最小サマリ。"""
    s = snapshot(force)
    return {"status": s["status"], "checked_at": s["checked_at"]}


# ---- 管理者の「システム状態」画面専用（AI・実接続確認・2026-07-03） ----
# 上の _ping_openai/_ping_bedrock/_ping_ollama/_ping_codex は状態ドット（全ページ・全ユーザーから
# 15秒キャッシュでポーリング）向けの**軽量チェック**（env の有無/バイナリ有無だけ・実際には AI へ
# 繋がない・per-user 設定のキーは見ない＝上のコメント参照）。この per-user キー未参照が、ユーザーが
# 設定画面で入れた API キーを使っているのに「システム状態」では未設定/停止に見える原因になっていた
# （UIフィードバック4「AI チェックが走らない／動いているか分からない」）。ここでは管理者の「再チェック」
# ボタン専用に、**ログイン中の管理者本人の設定（user_settings）も含めて実際に1回だけ AI へ接続する**
# （/settings/test と同じ probe を再利用）。gemini は元の COMPONENTS に無かった（実装漏れ）ので追加する。
# 実 API 呼び出しを伴うため、ナビの状態ドット（summary/snapshot）には一切混ぜず、per-uid の別キャッシュ
# （既定60秒）を持たせて自動ポーリング（30秒間隔）中に実 API 呼び出しを連発しないようにする。
_AI_TTL = float(os.environ.get("SHERPA_HEALTH_AI_TTL", "60"))
# RV HIGH（2026-07-03）: 各プローブは実 API 呼び出しのため、複数プロバイダの誰か1つが遅い/無応答だと
# _probe/BedrockProvider.probe の既定タイムアウト（抽出用90s・SDK既定は数分オーダー）まで待たされ、
# 直列実行だと /admin/health 全体が最悪数分ブロックしていた。ここは「再チェック」ボタンの応答性が
# 最優先＝短いタイムアウトを明示で渡し、かつ ai_snapshot() 側で全プローブを並列実行＋全体 deadline を設ける。
_AI_TIMEOUT = float(os.environ.get("SHERPA_HEALTH_AI_TIMEOUT", "8"))
_AI_DEADLINE = _AI_TIMEOUT + 4.0   # 並列実行のスケジューリング余裕（各プローブは _AI_TIMEOUT で自ら終わるはず）
_ai_lock = threading.Lock()
_ai_cache: dict = {}   # uid -> {"at": float, "data": [components]}


def _ai_check_openai(settings: dict, system_settings: dict | None = None) -> None:
    from . import agent_constructs, keys, model_catalog
    from .ingest.graph_extract import _probe
    # strict=True: 実 API 呼び出し（課金）を伴う経路のため、`cloud_provider`（A7）が非空の
    # 不正値のとき黙って既定（openai）へ倒れたキーで実送信しない（意図しない課金の是正）。
    key = keys.resolve_api_key("openai", settings, system_settings=system_settings, strict=True)
    # RV MED（2026-08-18 指摘3）: プレースホルダのまま（.env.example を無編集で有効化した env 等）だと
    # 実 API 呼び出しへ進んで分かりにくい 401 になる。同じ判定を先に通し、正直な文言で早期に返す。
    if not agent_constructs.is_real_api_key(key):
        raise RuntimeError(keys.NO_CENTRAL_KEY_MESSAGE)
    model = model_catalog.resolve_model("openai", "chat", None, system_settings=system_settings)
    # キー・モデル・接続先を `ai_snapshot()` が入口で読んだ同じ `system_settings` で揃える
    # （`_probe`→`complete_json` の送信時再読みを `openai_endpoint_override` で止める）。
    ok, detail = _probe({"provider": "openai", "key": key, "model": model,
                         "openai_endpoint_override": system_settings}, timeout=_AI_TIMEOUT)
    if not ok:
        raise RuntimeError(detail or "接続に失敗しました")


def _ai_check_gemini(settings: dict, system_settings: dict | None = None) -> None:
    from . import keys, model_catalog
    from .ingest.graph_extract import _probe
    # strict=True: 実 API 呼び出し（課金）を伴う経路のため、`cloud_provider`（A7）が非空の
    # 不正値のとき黙って既定（openai）へ倒れて gemini キーが None になる代わりに正直に伝える。
    key = keys.resolve_api_key("gemini", settings, system_settings=system_settings, strict=True)
    if not key:
        raise RuntimeError(keys.NO_CENTRAL_KEY_MESSAGE)
    model = model_catalog.resolve_model("gemini", "chat", None, system_settings=system_settings)
    ok, detail = _probe({"provider": "gemini", "key": key, "model": model}, timeout=_AI_TIMEOUT)
    if not ok:
        raise RuntimeError(detail or "接続に失敗しました")


def _ai_check_ollama(settings: dict, system_settings: dict | None = None) -> None:
    # R2a: per-user 設定の ollama_url も `llm.ollama_url` 経由で構築＝直 urlopen バイパスを閉じる
    # （`_ping_ollama` と同じ理由・degrade も同様に既存の broad except に乗る）。
    # R2a #3（2026-07-14）: `llm.urlopen_no_redirect` 経由にする（`_ping_ollama` と同じ理由）。
    # env `OLLAMA_URL` は直接読まない（`sherpa.keys.resolve_ollama_url` が中央設定を見る）。
    from . import keys, llm
    base = keys.resolve_ollama_url(settings, system_settings=system_settings)
    with llm.urlopen_no_redirect(llm.ollama_url(base, "/api/tags"), timeout=_AI_TIMEOUT) as r:
        json.loads(r.read())


def _ai_check_bedrock(settings: dict, system_settings: dict | None = None, *,
                      max_retries: int | None = None) -> None:
    from . import keys, store
    from .agents import BedrockProvider, _bedrock_auth_available
    # A7（クラウドプロバイダ排他選択）を _bedrock_auth_available の**前**で確定する（bedrock 未選択
    # なら SigV4 等の静的な手掛かりがあっても使わない・_bedrock_auth_available 単体の判定には任せない）。
    # A7 判定・キー解決を `ai_snapshot()` が入口で読んだ同じスナップショットで行う
    # （`_ping_bedrock` と同じ理由・省略時のみ自分で読む）。
    sys_s = system_settings if system_settings is not None else store.get_system_settings()
    # strict=True: 実 API 呼び出し（課金）を伴う経路のため、`cloud_provider` が非空の不正値
    # のとき黙って「未選択」扱い（NO_CENTRAL_KEY_MESSAGE）に丸めず正直な理由を伝える。
    if keys.selected_cloud_provider(sys_s, strict=True) != "bedrock":
        raise RuntimeError(keys.NO_CENTRAL_KEY_MESSAGE)
    api_key = keys.resolve_api_key("bedrock", settings, system_settings=sys_s, strict=True)
    if not _bedrock_auth_available(api_key):
        raise RuntimeError(keys.NO_CENTRAL_KEY_MESSAGE)
    # region は常に東京固定（利用者設定からは読まない・`_bedrock_region` が唯一の真実源）。
    # `max_retries`（省略可）: 呼び出し元（doctor 等）が「1回だけ実際に送る」ことを保証したい
    # 場合に `0` を明示できる（`BedrockProvider` docstring 参照・省略時は SDK 既定のまま）。
    ok, detail = BedrockProvider(None, settings.get("bedrock_model"), api_key,
                                 max_retries=max_retries).probe(timeout=_AI_TIMEOUT)
    if not ok:
        raise RuntimeError(detail or "接続に失敗しました")


def _ai_check_codex(settings: dict, system_settings: dict | None = None) -> None:
    import subprocess
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI が見つかりません")
    # Azure/互換接続先の Codex(OpenAI) 構成は ChatGPT ログイン（auth.json）を使わず、子プロセス env の
    # OPENAI_API_KEY（`keys.resolve_api_key("openai")` で解決したキー）で認証する
    # （`providers/codex/sandbox._write_codex_authoring_config` の独自 provider・env_key 方式）。
    # この構成で `codex login status` を判定に使うと常に「未ログイン」＝チャットでは動くのに
    # テスト画面だけ未接続になる（実環境指摘 2026-09-02）。チャットと同じ認証材料で判定する。
    from . import llm as _llm
    if _llm.openai_endpoint_kind(system_settings) != "openai":
        if keys.resolve_api_key("openai", settings, system_settings=system_settings):
            return   # CLI 導入済み＋キー解決可＝チャットの Codex 実行と同じ材料が揃っている
        raise RuntimeError(keys.NO_CENTRAL_KEY_MESSAGE)
    r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=_AI_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "未ログイン（codex login が必要）").strip()[:200])


# (id, 表示名, 落ちたときの全体への影響, check(settings), 対処ヒント)
# ヒント文言は sherpa.keys.NO_CENTRAL_KEY_MESSAGE を共有する（env はもう初回シードのみのため、
# ".env で設定" 式の案内はしない・管理画面/個人設定画面が唯一の管理場所）。
_AI_COMPONENTS = [
    ("openai", "OpenAI API", "none", _ai_check_openai,
     keys.NO_CENTRAL_KEY_MESSAGE + "（または個人設定でキーを入力してください）"),
    ("gemini", "Gemini（Google）", "none", _ai_check_gemini,
     keys.NO_CENTRAL_KEY_MESSAGE + "（または個人設定でキーを入力してください）"),
    ("bedrock", "AWS Bedrock（Claude）", "none", _ai_check_bedrock,
     keys.NO_CENTRAL_KEY_MESSAGE + "（または個人設定でキーを入力するか、サーバの AWS 認証情報を確認してください）"),
    ("ollama", "ローカルLLM（Ollama）", "none", _ai_check_ollama,
     "ollama serve の起動を確認してください（使わない構成なら対応不要）"),
    ("codex", "Codex CLI（AIエージェント）", "none", _ai_check_codex,
     "codex CLI の導入とログイン（codex login）を確認してください（使わない構成なら対応不要）"),
]


def ai_snapshot(uid: str, settings: dict, force: bool = False) -> list[dict]:
    """管理者本人の設定を使って AI 各プロバイダへ実接続確認する（システム状態ページの
    「再チェック」専用）。per-uid キャッシュ（既定60秒）＝自動ポーリング中は実 API 呼び出しをしない。

    RV HIGH（2026-07-03）: 直列実行だと遅い/無応答なプロバイダ1つで全体が最悪数分ブロックしていた。
    ここでは各プローブを ThreadPoolExecutor で並列実行し、全体 deadline（`_AI_DEADLINE`）を超えた
    プローブは「確認できませんでした（タイムアウト）」として打ち切る（各プローブ自身も `_AI_TIMEOUT`
    で自ら終わるはずなので deadline は二重の安全網＝スレッドがハングしてもレスポンス自体は遅延しない）。
    """
    with _ai_lock:
        cached = _ai_cache.get(uid)
        if not force and cached is not None and time.monotonic() - cached["at"] < _AI_TTL:
            return cached["data"]
    # 全プローブへ同じ system_settings スナップショットを渡す（個別に読み直すと、この一括チェックの
    # 最中に admin 保存が挟まった場合に一部のプローブだけ新しい接続先/キーを見る混在が起こり得る）。
    # DB 不達時は `None`（＝各 `_ai_check_*` が自分で読み直す＝5プローブがバラバラに再試行し、一部
    # だけ TTL キャッシュ経由で成功する等の不揃いを招く）ではなく、明示的な空 dict `{}` を渡す＝
    # 全プローブが「キーなし」で一様に即座に停止する（fail-closed）。再試行は次回の `ai_snapshot()`
    # 呼び出し（60秒 TTL 経過後の次の healthz/再チェック）に委ねる＝この1回の呼び出しの中で
    # 再試行して結果が割れることはない。
    from . import store as _store
    try:
        sys_s = _store.get_system_settings()
    except Exception:
        sys_s = {}
    ex = cf.ThreadPoolExecutor(max_workers=max(1, len(_AI_COMPONENTS)))
    try:
        futures = {cid: ex.submit(_check_one_ai, cid, label, impact,
                                  (lambda fn=fn: fn(settings, sys_s)), hint)
                  for cid, label, impact, fn, hint in _AI_COMPONENTS}
        deadline = time.monotonic() + _AI_DEADLINE
        components = []
        for cid, label, impact, fn, hint in _AI_COMPONENTS:               # _AI_COMPONENTS の順を保つ
            remaining = max(0.05, deadline - time.monotonic())
            try:
                components.append(futures[cid].result(timeout=remaining))
            except cf.TimeoutError:
                components.append({"id": cid, "label": label, "impact": impact, "ok": False,
                                   "detail": "確認できませんでした（タイムアウト）",
                                   "latency_ms": int(_AI_DEADLINE * 1000), "hint": hint})
    finally:
        ex.shutdown(wait=False, cancel_futures=True)   # レスポンスをブロックしない（残存スレッドは自身の timeout で終わる）
    with _ai_lock:
        _ai_cache[uid] = {"at": time.monotonic(), "data": components}
    return components


# ---- 管理者の「システム状態」画面専用（ES/グラフの実クエリ検索テスト） ----
# 上の postgres/neo4j/elasticsearch（COMPONENTS）は疎通確認のみ（cluster health・verify_connectivity）
# で、登録 world の索引/グラフへ実際に検索クエリを1発打つわけではない。AI（openai/gemini/…）の
# 実接続確認が失敗した時に、原因が「AI 側」か「検索基盤（ES/Neo4j）側」かを利用者が切り分けられる
# よう、実 world に対して軽量な実クエリを1発打つ行をここに追加する。ai_snapshot と同じ理由
# （実クエリはコスト/負荷が非ゼロ）で per-uid TTL キャッシュを持たせる——「再チェック」ボタンは
# force=True で必ず最新化し、自動ポーリング（force=False）は TTL 内なら再実行しない
# （＝最大でも1分に1回しか実クエリを打たない）。
_SEARCH_TTL = float(os.environ.get("SHERPA_HEALTH_SEARCH_TTL", "60"))
_SEARCH_TIMEOUT = float(os.environ.get("SHERPA_HEALTH_SEARCH_TIMEOUT", "5"))
_SEARCH_DEADLINE = _SEARCH_TIMEOUT + 4.0   # 並列実行のスケジューリング余裕（各プローブは _SEARCH_TIMEOUT で自ら終わるはず）
_search_lock = threading.Lock()
_search_cache: dict = {}   # uid -> {"at": float, "data": [components]}

_NO_WORLD_DETAIL = "対象なし（登録 world がありません）"


def _search_probe_world() -> str | None:
    """ES/グラフ検索プローブの対象 world_id を解決する。

    KB は全社1つが前提（CLAUDE.md）のため、登録済み world の先頭1件（`list_worlds_db()` は
    world_id 昇順）を代表として使う。空リスト（未登録）は None を返す——呼び出し元
    `search_snapshot` はこれを「対象なし」として扱う。レジストリ読取自体の失敗（Postgres 不達等）は
    ここで握り潰さず例外をそのまま送出する（「対象なし」と「読めなかった」は意味が違う——
    `search_snapshot` 側で区別して正直に失敗させる）。
    """
    from . import store
    rows = store.list_worlds_db()
    return rows[0]["world_id"] if rows else None


def _search_probe_es(world_id: str | None) -> str:
    """ES 検索プローブ（match_all size=1）。成功時の**表示用 detail 文字列**を返す（失敗は例外を
    送出し、呼び出し元 `_check_one_search` が `_classify()` で分類する＝接続情報は出さない）。

    索引が無い（HTTP 404＝その world を1度も取り込んでいない）場合は ES 自体は正常に応答して
    いるため失敗として扱わず、0 ヒットと同じ「索引が空」に倒す。
    """
    if world_id is None:
        return _NO_WORLD_DETAIL
    from . import es_index
    try:
        res = es_index._req("POST", f"/{es_index._index(world_id)}/_search",
                            {"query": {"match_all": {}}, "size": 1}, timeout=_SEARCH_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "索引が空です（未取り込み）"
        raise
    hits = (res.get("hits") or {}).get("hits") or []
    return "ヒットあり" if hits else "索引が空です"


def _search_probe_graph(world_id: str | None) -> str:
    """Neo4j 検索プローブ（world のノードを1件取得・LIMIT 1）。成功時の detail 文字列を返す
    （失敗は例外を送出し、呼び出し元が分類する＝`_ping_neo4j` と同じ接続材料・短い per-query
    timeout は `world_neo4j._run_read_capped` の安全弁付き経路ではなく、疎通確認用の素の
    `session.run` で足りる＝影響分析ほど重い網羅性契約はここには無い）。
    """
    if world_id is None:
        return _NO_WORLD_DETAIL
    from neo4j import GraphDatabase, Query

    from .ingest import world_neo4j
    env = world_neo4j._env()
    with GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]),
                              connection_timeout=_SEARCH_TIMEOUT,
                              connection_acquisition_timeout=_SEARCH_TIMEOUT) as driver:
        with driver.session() as session:
            row = session.run(Query("MATCH (n:Entity {world_id: $world}) RETURN n LIMIT 1",
                                    timeout=_SEARCH_TIMEOUT), world=world_id).single()
    return "ヒットあり" if row else "該当データが空です（未取り込み）"


# (id, 表示名, 落ちたときの全体への影響, probe(world_id), 対処ヒント)
# impact="none"＝AI 各行と同じ「参考情報」扱い（page 全体の status は `_compute()`/COMPONENTS の
# postgres/neo4j/elasticsearch 疎通確認だけで決まる・この診断行の結果では変わらない）。
_SEARCH_COMPONENTS = [
    ("es_search", "ES検索（実クエリ）", "none", _search_probe_es,
     "Elasticsearch の起動・索引の取り込み状況を確認してください"),
    ("graph_search", "グラフ検索（実クエリ）", "none", _search_probe_graph,
     "Neo4j の起動・取り込み状況を確認してください"),
]


def _check_one_search(comp_id, label, impact, probe, hint) -> dict:
    """検索プローブ専用の `_check_one`/`_check_one_ai` 相当。

    `probe()` は成功時に**表示用の detail 文字列**（ヒット有無・「対象なし」等）を返す契約
    （他の `_check_one*` と違い、成功時も detail を捨てずそのまま使う——ヒット有無を見せるのが
    このプローブの目的のため）。失敗（接続不可等）は例外を送出させ、DSN・接続 URI 等の生の
    例外文字列は出さず `_classify()` で短い分類ラベルへ丸める。
    """
    t0 = time.monotonic()
    try:
        detail = probe()
        ok = True
    except Exception as e:
        ok = False
        detail = f"{_classify(e)}（{type(e).__name__}）"
        _logger.warning("health search probe failed: %s: %s", comp_id, e)
    out = {"id": comp_id, "label": label, "impact": impact, "ok": ok,
           "detail": detail, "latency_ms": int((time.monotonic() - t0) * 1000)}
    if not ok:
        out["hint"] = hint
    return out


def search_snapshot(uid: str, force: bool = False) -> list[dict]:
    """ES/Neo4j への実クエリ検索プローブ（AI との切り分け用）。per-uid TTL キャッシュ（既定60秒）で
    実クエリの頻度を抑える仕組みは `ai_snapshot` と同じ——「再チェック」ボタンは `force=True` で
    必ず最新化し、自動ポーリング（`force=False`）は TTL 内なら再実行しない（＝最大でも1分に1回）。

    レジストリ読取自体の失敗（Postgres 不達等）は「対象なし」に丸めず、両行を ok=False＋
    `_classify()` の分類 detail で正直に失敗させる（`_search_probe_world` docstring 参照）。
    解決できた場合は ES・グラフの両プローブへ**同じ world_id スナップショット**を渡す
    （`ai_snapshot` が全プローブへ同じ system_settings を渡すのと同じ理由——別々に解決すると、
    この一括チェックの最中に world の register/rebind が挟まった場合に片方だけ別の world を
    見る食い違いが起こり得る）。

    `ai_snapshot` と同じ ThreadPoolExecutor＋全体 deadline（`_SEARCH_DEADLINE`）で実行する
    （TCP は繋がるが応答しない ES/Neo4j 相手だと、per-probe timeout だけでは再チェックが
    無期限化しスレッドが積み上がりうるため・deadline 超過は「確認できませんでした（タイムアウト）」
    に丸めてレスポンス自体はブロックしない）。
    """
    with _search_lock:
        cached = _search_cache.get(uid)
        if not force and cached is not None and time.monotonic() - cached["at"] < _SEARCH_TTL:
            return cached["data"]
    t0 = time.monotonic()
    try:
        world_id = _search_probe_world()
    except Exception as e:
        detail = f"{_classify(e)}（{type(e).__name__}）"
        _logger.warning("health search probe world resolution failed: %s", e)
        latency_ms = int((time.monotonic() - t0) * 1000)
        components = [{"id": cid, "label": label, "impact": impact, "ok": False,
                       "detail": detail, "latency_ms": latency_ms, "hint": hint}
                     for cid, label, impact, _fn, hint in _SEARCH_COMPONENTS]
        with _search_lock:
            _search_cache[uid] = {"at": time.monotonic(), "data": components}
        return components
    ex = cf.ThreadPoolExecutor(max_workers=max(1, len(_SEARCH_COMPONENTS)))
    try:
        futures = {cid: ex.submit(_check_one_search, cid, label, impact,
                                  (lambda fn=fn: fn(world_id)), hint)
                  for cid, label, impact, fn, hint in _SEARCH_COMPONENTS}
        deadline = time.monotonic() + _SEARCH_DEADLINE
        components = []
        for cid, label, impact, fn, hint in _SEARCH_COMPONENTS:            # _SEARCH_COMPONENTS の順を保つ
            remaining = max(0.05, deadline - time.monotonic())
            try:
                components.append(futures[cid].result(timeout=remaining))
            except cf.TimeoutError:
                components.append({"id": cid, "label": label, "impact": impact, "ok": False,
                                   "detail": "確認できませんでした（タイムアウト）",
                                   "latency_ms": int(_SEARCH_DEADLINE * 1000), "hint": hint})
    finally:
        ex.shutdown(wait=False, cancel_futures=True)   # レスポンスをブロックしない（残存スレッドは自身の timeout で終わる）
    with _search_lock:
        _search_cache[uid] = {"at": time.monotonic(), "data": components}
    return components
