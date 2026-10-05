"""バックエンド健全性チェック（ナビの状態ドット＋管理者のシステム状態画面）。

- 各コンポーネントを短いタイムアウトで ping し、結果を TTL キャッシュ（既定 15 秒）する。
- 全体 status は落ちたときの影響を集約する: Postgres → down／Neo4j・Elasticsearch → degraded／Codex・OpenAI・Ollama → 影響なし（参考情報のみ）。
設計: docs/design/operations.md「起動・停止・状態」
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

from . import keys  # トップレベル import 安全（keys.py はモジュールレベルで sherpa 内を import しない）

_TTL = 15.0
# 既定は短めに固定する（不達時に lock 内で直列 ping する全コンポーネント分の待ち時間上限になるため）
_TIMEOUT = 1.0

_LEVELS = {"ok": 0, "degraded": 1, "down": 2}

_STORE_HINT = "make up（docker compose up -d）でストアを再起動してください"

_logger = logging.getLogger(__name__)


def _ping_postgres() -> None:
    import psycopg

    from . import store
    # statement_timeout でクエリ側の上限も保証する
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
    # urllib の timeout は socket 単位。cluster health は小さい応答なので十分
    from . import es_index
    with urllib.request.urlopen(es_index._url() + "/_cluster/health", timeout=_TIMEOUT) as r:
        json.loads(r.read())


def _ping_codex() -> None:
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI が見つかりません")


def _ping_openai() -> None:
    # 判定は `agent_constructs.is_real_api_key` に揃え、キーは `sherpa.keys.resolve_api_key`（中央設定）経由で読む
    from . import agent_constructs, keys
    key = keys.resolve_api_key("openai", None, strict=True)
    if not agent_constructs.is_real_api_key(key):
        raise RuntimeError("OpenAI の API キーが未設定です（管理画面で設定してください）")


class _NotApplicable(RuntimeError):
    """「対象外（未設定/未選択）」の申告用。`_check_one` はこれを ok=True・detail「対象外（…）」・DEBUG ログとして扱う（失敗とは区別する）。"""


class _OllamaEmbedModelMissing(RuntimeError):
    """埋め込みが Ollama 構成に解決されているのに、その埋め込みモデルが Ollama 未取得。`_classify()` が案内文をそのまま使う。"""

    def __init__(self, model: str):
        self.model = model
        super().__init__(model)


def _ping_ollama() -> None:
    # 接続先は `llm.ollama_url` 経由で組み立て（SSRF 宛先ポリシーを通す）、送信は `llm.urlopen_no_redirect`。
    # ブロック時の `SsrfBlocked` は `_check_one` の broad except に乗る。env `OLLAMA_URL` は直接読まない
    from . import keys, llm, store
    sys_s = store.get_system_settings()
    configured = bool(sys_s.get("ollama_url"))  # 中央設定に接続先があるか（既定 localhost は「未設定」扱い）
    base = keys.resolve_ollama_url(None, system_settings=sys_s)
    try:
        with llm.urlopen_no_redirect(llm.ollama_url(base, "/api/tags"), timeout=_TIMEOUT) as r:
            data = json.loads(r.read())
    except Exception as e:
        if not configured:
            # 接続先を設定していない環境で既定の localhost に応答が無いのは正常＝対象外。設定済みで落ちていれば失敗
            raise _NotApplicable(f"接続先が未設定（既定 {base} にも応答なし）") from e
        raise
    _check_ollama_embed_model(data, sys_s)


def _check_ollama_embed_model(tags_response: object, sys_s: dict) -> None:
    """`/api/tags` の応答から、埋め込みが Ollama 構成に解決されている場合だけ、その埋め込みモデルが導入済みかを確認する。

    未取得のままだとベクトル検索が BM25 のみへ縮退する。タグ無し参照は `:latest` とみなす。
    """
    from . import embeddings
    try:
        ec = embeddings.cfg(None, system_settings=sys_s)
    except Exception:
        return
    if not ec or ec.get("provider") != "ollama":
        return
    model = ec.get("model")
    if not isinstance(model, str) or not model:
        return
    names: set[str] = set()
    if isinstance(tags_response, dict):
        raw_models = tags_response.get("models")
        if isinstance(raw_models, list):
            for entry in raw_models:
                name = entry.get("name") if isinstance(entry, dict) else None
                if isinstance(name, str) and name:
                    names.add(name)
    wanted = model if ":" in model else f"{model}:latest"
    if wanted not in names:
        raise _OllamaEmbedModelMissing(model)


# (id, 表示名, 落ちたときの全体への影響, ping, 対処ヒント)
COMPONENTS = [
    ("postgres", "PostgreSQL（会話・ユーザー・台帳）", "down", _ping_postgres, _STORE_HINT),
    ("neo4j", "Neo4j（ナレッジグラフ・影響分析）", "degraded", _ping_neo4j, _STORE_HINT),
    ("elasticsearch", "Elasticsearch（全文検索）", "degraded", _ping_es, _STORE_HINT),
    ("codex", "Codex CLI（AIエージェント）", "none", _ping_codex,
     "codex CLI の導入とログインを確認してください（使わない構成なら対応不要）"),
    # ヒントは env でなく管理画面へ誘導する（`keys.NO_CENTRAL_KEY_MESSAGE` を共有）
    ("openai", "OpenAI API キー", "none", _ping_openai,
     keys.NO_CENTRAL_KEY_MESSAGE + "（Codex/ローカルLLM 利用なら対応不要）"),
    ("ollama", "ローカルLLM（Ollama）", "none", _ping_ollama,
     "ollama serve の起動を確認してください（使わない構成なら対応不要）"),
]

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}


def _classify(e: BaseException) -> str:
    """例外を短い日本語分類に正規化する。接続情報が `str(e)` に含まれうるため、生の例外文字列は出さず分類ラベルのみ返す。
    `URLError` の `__cause__` / `reason` も1段見る。
    """
    if isinstance(e, _OllamaEmbedModelMissing):
        return (f"埋め込みモデル {e.model} が Ollama にありません（ollama pull {e.model}）。"
                "このままではベクトル検索が使えずキーワード検索だけになります")
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
        ok, detail = True, f"対象外（{e}）"  # 使っていない構成＝正常。WARNING を出さない
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
    """`_check_one` の AI 専用版（`ai_snapshot` からのみ使う）。

    `_ai_check_*` は静的な案内文言か `_safe_detail()` 済みの理由しか例外メッセージに乗せない契約のため、丸めずメッセージをそのまま使う。
    ただし想定外の生の例外が接続情報を含みうるため、`_safe_detail` と同じ redaction（`_mask_secrets`・`_redact_reflected_urls`）を通した文字列を
    detail・ログの両方に使う。生の例外オブジェクト `e` はログへ出さない。
    """
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


# ---- 管理者の「システム状態」画面専用（AI・実接続確認） ----
# 上の `_ping_*` は状態ドット向けの軽量チェック（実際には AI へ繋がない）。ここは管理者の「再チェック」専用に、
# 管理者本人の設定（user_settings）も含めて実際に1回だけ AI へ接続する。状態ドット（summary/snapshot）には混ぜず、
# per-uid の別キャッシュ（既定60秒）で実 API 呼び出しの連発を防ぐ。
_AI_TTL = 60.0
# 各プローブは短いタイムアウトを明示で渡し、`ai_snapshot()` が全プローブを並列実行＋全体 deadline で打ち切る（再チェックの応答性を優先）
_AI_TIMEOUT = float(os.environ.get("SHERPA_HEALTH_AI_TIMEOUT", "8"))
_AI_DEADLINE = _AI_TIMEOUT + 4.0  # 並列実行のスケジューリング余裕
_ai_lock = threading.Lock()
_ai_cache: dict = {}  # uid -> {"at": float, "data": [components]}


def _ai_check_openai(settings: dict, system_settings: dict | None = None) -> None:
    from . import agent_constructs, keys, model_catalog
    from .ingest.graph_extract import _probe
    # strict=True: 実 API 呼び出し（課金）を伴うため、不正な `cloud_provider` のとき既定（openai）へ倒れたキーで実送信しない
    key = keys.resolve_api_key("openai", settings, system_settings=system_settings, strict=True)
    # プレースホルダのままのキーは実 API 呼び出しへ進まず、正直な文言で早期に返す
    if not agent_constructs.is_real_api_key(key):
        raise RuntimeError(keys.NO_CENTRAL_KEY_MESSAGE)
    model = model_catalog.resolve_model("openai", "chat", None, system_settings=system_settings)
    # キー・モデル・接続先は `ai_snapshot()` が入口で読んだ同じ `system_settings` で揃える
    ok, detail = _probe({"provider": "openai", "key": key, "model": model,
                         "openai_endpoint_override": system_settings}, timeout=_AI_TIMEOUT)
    if not ok:
        raise RuntimeError(detail or "接続に失敗しました")


def _ai_check_ollama(settings: dict, system_settings: dict | None = None) -> None:
    # 接続先は `llm.ollama_url` 経由で構築し、送信は `llm.urlopen_no_redirect`。env `OLLAMA_URL` は直接読まない
    from . import keys, llm
    base = keys.resolve_ollama_url(settings, system_settings=system_settings)
    with llm.urlopen_no_redirect(llm.ollama_url(base, "/api/tags"), timeout=_AI_TIMEOUT) as r:
        json.loads(r.read())


def _ai_check_codex(settings: dict, system_settings: dict | None = None) -> None:
    import subprocess
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI が見つかりません")
    # Codex(Ollama) は codex login を使わず Ollama へ直接つなぐ＝Ollama へ届くかを見る
    from . import agent_constructs
    if agent_constructs.codex_model_provider(settings) == "ollama":
        from . import keys as _keys, llm as _llm, model_catalog
        base = _keys.resolve_ollama_url(settings, system_settings=system_settings)
        with _llm.urlopen_no_redirect(_llm.ollama_url(base, "/api/tags"), timeout=_AI_TIMEOUT) as r:
            tags = json.loads(r.read())
        # 届くだけでなく、Codex に使うモデルが Ollama に取得済みかも見る
        model = model_catalog.resolve_model("codex", "codex", None, system_settings=system_settings)
        names = {m.get("name") for m in (tags.get("models") or []) if isinstance(m, dict)} \
            if isinstance(tags, dict) else set()
        if model and model not in names and f"{model}:latest" not in names:
            raise RuntimeError(f"Codex に使うモデル {model} が Ollama にありません（ollama pull で取得するか、"
                               "使えるモデルの Codex の列を Ollama のモデル名にしてください）")
        return
    # Azure/互換接続先の Codex(OpenAI) 構成は ChatGPT ログインでなく `OPENAI_API_KEY`（`keys.resolve_api_key("openai")`）で認証するため、チャットと同じ材料で判定する
    from . import llm as _llm
    keys.selected_cloud_provider(system_settings, strict=True)
    if _llm.openai_endpoint_kind(system_settings) != "openai":
        if keys.resolve_api_key("openai", settings, system_settings=system_settings):
            return  # CLI 導入済み＋キー解決可＝チャットの Codex 実行と同じ材料が揃っている
        raise RuntimeError(keys.NO_CENTRAL_KEY_MESSAGE)
    r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=_AI_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "未ログイン（codex login が必要）").strip()[:200])


# (id, 表示名, 落ちたときの全体への影響, check(settings), 対処ヒント)。ヒント文言は `keys.NO_CENTRAL_KEY_MESSAGE` を共有する
_AI_COMPONENTS = [
    ("openai", "OpenAI API", "none", _ai_check_openai,
     keys.NO_CENTRAL_KEY_MESSAGE + "（または個人設定でキーを入力してください）"),
    ("ollama", "ローカルLLM（Ollama）", "none", _ai_check_ollama,
     "ollama serve の起動を確認してください（使わない構成なら対応不要）"),
    ("codex", "Codex CLI（AIエージェント）", "none", _ai_check_codex,
     "codex CLI の導入とログイン（codex login）を確認してください（使わない構成なら対応不要）"),
]


def ai_snapshot(uid: str, settings: dict, force: bool = False) -> list[dict]:
    """管理者本人の設定を使って AI 各プロバイダへ実接続確認する（システム状態ページの「再チェック」専用）。

    per-uid キャッシュ（既定60秒）。各プローブを ThreadPoolExecutor で並列実行し、全体 deadline（`_AI_DEADLINE`）を超えたものは
    「確認できませんでした（タイムアウト）」として打ち切る。
    """
    with _ai_lock:
        cached = _ai_cache.get(uid)
        if not force and cached is not None and time.monotonic() - cached["at"] < _AI_TTL:
            return cached["data"]
    # 全プローブへ同じ system_settings スナップショットを渡す。DB 不達時は `{}` を渡して全プローブを一様に「キーなし」で停止させる（fail-closed）
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
        for cid, label, impact, fn, hint in _AI_COMPONENTS:  # _AI_COMPONENTS の順を保つ
            remaining = max(0.05, deadline - time.monotonic())
            try:
                components.append(futures[cid].result(timeout=remaining))
            except cf.TimeoutError:
                components.append({"id": cid, "label": label, "impact": impact, "ok": False,
                                   "detail": "確認できませんでした（タイムアウト）",
                                   "latency_ms": int(_AI_DEADLINE * 1000), "hint": hint})
    finally:
        ex.shutdown(wait=False, cancel_futures=True)  # レスポンスをブロックしない
    with _ai_lock:
        _ai_cache[uid] = {"at": time.monotonic(), "data": components}
    return components


# ---- 管理者の「システム状態」画面専用（ES/グラフの実クエリ検索テスト） ----
# 登録 world の索引/グラフへ実際に検索クエリを1発打ち、AI 側と検索基盤側の切り分けに使う。
# 「再チェック」は force=True で最新化し、自動ポーリングは per-uid TTL 内なら再実行しない。
_SEARCH_TTL = 60.0
_SEARCH_TIMEOUT = 5.0
_SEARCH_DEADLINE = _SEARCH_TIMEOUT + 4.0  # 並列実行のスケジューリング余裕
_search_lock = threading.Lock()
_search_cache: dict = {}  # uid -> {"at": float, "data": [components]}

_NO_WORLD_DETAIL = "対象なし（登録 world がありません）"


def _search_probe_world() -> str | None:
    """ES/グラフ検索プローブの対象 world_id を解決する（登録済み world の先頭1件）。未登録は None。

    レジストリ読取自体の失敗は握り潰さず例外を送出する（「対象なし」と「読めなかった」を区別するため）。
    """
    from . import store
    rows = store.list_worlds_db()
    return rows[0]["world_id"] if rows else None


def _search_probe_es(world_id: str | None) -> str:
    """ES 検索プローブ（match_all size=1）。成功時の表示用 detail 文字列を返す（失敗は例外を送出し、`_check_one_search` が分類する）。

    索引が無い（HTTP 404）場合は失敗とせず「索引が空」に倒す。
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
    """Neo4j 検索プローブ（world のノードを1件取得・LIMIT 1）。成功時の detail 文字列を返す（失敗は例外を送出し、呼び出し元が分類する）。"""
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


# (id, 表示名, 落ちたときの全体への影響, probe(world_id), 対処ヒント)。impact="none" は参考情報（page 全体の status を変えない）
_SEARCH_COMPONENTS = [
    ("es_search", "ES検索（実クエリ）", "none", _search_probe_es,
     "Elasticsearch の起動・索引の取り込み状況を確認してください"),
    ("graph_search", "グラフ検索（実クエリ）", "none", _search_probe_graph,
     "Neo4j の起動・取り込み状況を確認してください"),
]


def _check_one_search(comp_id, label, impact, probe, hint) -> dict:
    """検索プローブ専用の `_check_one`。`probe()` は成功時に表示用 detail 文字列を返す（ヒット有無を見せるため捨てない）。失敗は `_classify()` で短い分類ラベルへ丸める。"""
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
    """ES/Neo4j への実クエリ検索プローブ（AI との切り分け用）。

    per-uid TTL キャッシュ（既定60秒）。「再チェック」は `force=True` で最新化し、自動ポーリングは TTL 内なら再実行しない。
    レジストリ読取自体の失敗は「対象なし」に丸めず、両行を ok=False で失敗させる。両プローブへ同じ world_id スナップショットを渡す。
    `ai_snapshot` と同じ ThreadPoolExecutor＋全体 deadline（`_SEARCH_DEADLINE`）で実行し、超過は「確認できませんでした（タイムアウト）」にする。
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
        for cid, label, impact, fn, hint in _SEARCH_COMPONENTS:  # _SEARCH_COMPONENTS の順を保つ
            remaining = max(0.05, deadline - time.monotonic())
            try:
                components.append(futures[cid].result(timeout=remaining))
            except cf.TimeoutError:
                components.append({"id": cid, "label": label, "impact": impact, "ok": False,
                                   "detail": "確認できませんでした（タイムアウト）",
                                   "latency_ms": int(_SEARCH_DEADLINE * 1000), "hint": hint})
    finally:
        ex.shutdown(wait=False, cancel_futures=True)  # レスポンスをブロックしない
    with _search_lock:
        _search_cache[uid] = {"at": time.monotonic(), "data": components}
    return components
