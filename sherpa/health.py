"""バックエンド健全性チェック（ナビの状態ドット＋管理者のシステム状態画面）。

- 各コンポーネントを短いタイムアウトで ping し、結果を TTL キャッシュ（既定 15 秒）する。
- 全体 status は落ちたときの影響を集約する: Postgres → down／Neo4j・Elasticsearch → degraded／AI（回答の AI・Codex・埋め込み）→ 影響なし（参考情報のみ）。
- 軽い確認だけを流す（届くか・モデルが取得済みか・キーがあるか・1 件読めるか）。AI への送信も書き込みもしない。実際に送る確認は `connectivity`。
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

from . import agent_constructs, ai_targets, embeddings, keys, llm  # keys.py はモジュールレベルで sherpa 内を import しない

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


class _NotApplicable(RuntimeError):
    """「対象外（未設定/未選択）」の申告用。`_check_one` はこれを ok=True・detail「対象外（…）」・DEBUG ログとして扱う（失敗とは区別する）。"""


class _NotInUse(_NotApplicable):
    """Ollama を使わない構成の申告（`_check_one` は対象外と同じ扱いでログも出さない）。"""


# (id, 表示名, 落ちたときの全体への影響, ping, 対処ヒント)
COMPONENTS = [
    ("postgres", "PostgreSQL（会話・ユーザー・台帳）", "down", _ping_postgres, _STORE_HINT),
    ("neo4j", "Neo4j（ナレッジグラフ・影響分析）", "degraded", _ping_neo4j, _STORE_HINT),
    ("elasticsearch", "Elasticsearch（全文検索）", "degraded", _ping_es, _STORE_HINT),
]

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}


def _classify(e: BaseException) -> str:
    """例外を短い日本語分類に正規化する。接続情報が `str(e)` に含まれうるため、生の例外文字列は出さず分類ラベルのみ返す。
    `URLError` の `__cause__` / `reason` も1段見る。
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
        ok, detail = True, f"対象外（{e}）"  # 使っていない構成＝正常。WARNING を出さない
        if not isinstance(e, _NotInUse):
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
    except _NotInUse as e:
        ok, detail = True, f"対象外（{e}）"  # 使っていない構成はログに出さない
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


# ---- 管理者の「システム状態」画面専用（AI の軽い確認） ----
# 用途ごと（回答の AI・Codex・埋め込み）が実際に使う接続先・モデル・キーだけを見る。AI へは送らない。
# 管理者本人の設定（user_settings）も含めて見るので、状態ドット（summary/snapshot）には混ぜず、per-uid の別キャッシュ（既定60秒）に持つ。
_AI_TTL = 60.0
# 各確認は短いタイムアウトを明示で渡し、`ai_snapshot()` が全プローブを並列実行＋全体 deadline で打ち切る（再チェックの応答性を優先）
_AI_TIMEOUT = float(os.environ.get("SHERPA_HEALTH_AI_TIMEOUT", "8"))
_AI_DEADLINE = _AI_TIMEOUT + 4.0  # 並列実行のスケジューリング余裕
_ai_lock = threading.Lock()
_ai_cache: dict = {}  # uid -> {"at": float, "data": [components]}


def _tags_or_raise(base: str, system_settings: dict, what: str) -> dict:
    """Ollama の取得済みモデルの一覧を読む。届かなければ用途名つきの理由で失敗にする（接続情報は出さない）。"""
    try:
        return ai_targets.fetch_tags(base, _AI_TIMEOUT, system_settings=system_settings)
    except Exception as e:
        raise RuntimeError(f"{what}の Ollama に届きません（{_classify(e)}）") from None


def _need_model(model: str | None, tags: dict, what: str) -> None:
    if not ai_targets.model_installed(model, ai_targets.installed_names(tags)):
        raise RuntimeError(f"{what}に使うモデル {model} が Ollama にありません（ollama pull {model} で取得してください）")


def _need_openai_key(settings: dict | None, system_settings: dict) -> None:
    key = keys.resolve_api_key("openai", settings, system_settings=system_settings, strict=True)
    if not agent_constructs.is_real_api_key(key):
        raise RuntimeError(keys.NO_CENTRAL_KEY_MESSAGE)


def _ai_check_answer(settings: dict, system_settings: dict | None = None) -> None:
    """回答の AI（簡易の回答）。Ollama なら中央の接続先に届くか＋モデルが取得済みか、OpenAI ならキーが実在するか。"""
    sys_s = system_settings or {}
    try:
        t = ai_targets.answer_target(sys_s)
    except Exception:
        raise RuntimeError("回答の AI の設定を解決できません（既定の AI・使えるモデルを確認）") from None
    if t.provider == "openai":
        _need_openai_key(None, sys_s)
        return
    _need_model(t.model, _tags_or_raise(t.base, sys_s, "回答の AI"), "回答の AI")


def _ai_check_embed(settings: dict, system_settings: dict | None = None) -> None:
    """埋め込み。Ollama なら埋め込み用の接続先に届くか＋埋め込みモデルが取得済みか、OpenAI ならキーが実在するか。使っていなければ対象外。"""
    sys_s = system_settings or {}
    try:
        t = ai_targets.embed_target(sys_s)
    except Exception:
        raise RuntimeError("埋め込みの設定を解決できません") from None
    if t is None:
        if embeddings.cloud_selected_but_unavailable(sys_s):
            raise RuntimeError("埋め込みの設定が成立していません（選んだ接続先のキー・接続先・モデルを確認）")
        raise _NotInUse("使っていません")
    if t.provider == "openai":
        if not agent_constructs.is_real_api_key(t.cfg.get("key")):
            raise RuntimeError(keys.NO_CENTRAL_KEY_MESSAGE)
        return
    tags = _tags_or_raise(t.base, sys_s, "埋め込み用")
    if not ai_targets.model_installed(t.model, ai_targets.installed_names(tags)):
        raise RuntimeError(f"埋め込みモデル {t.model} が Ollama にありません（ollama pull {t.model}）。"
                           "このままでは意味での検索が使えず、語の一致の検索だけになります")


def _ai_check_codex(settings: dict, system_settings: dict | None = None) -> None:
    """Codex 調査。CLI の有無と、実効の接続先ごとの判定（Ollama は届くか＋モデル、互換接続先はキー、OpenAI 本家はログイン状態）。"""
    import subprocess
    sys_s = system_settings or {}
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI が見つかりません")
    t = ai_targets.codex_target(settings, sys_s)
    if t.provider == "ollama":
        tags = _tags_or_raise(t.base, sys_s, "Codex 用")
        if t.model and not ai_targets.model_installed(t.model, ai_targets.installed_names(tags)):
            raise RuntimeError(f"Codex に使うモデル {t.model} が Ollama にありません（ollama pull で取得するか、"
                               "使えるモデルの Codex の列を Ollama のモデル名にしてください）")
        return
    keys.selected_cloud_provider(sys_s, strict=True)
    if llm.openai_endpoint_kind(sys_s) != "openai":
        if keys.resolve_api_key("openai", settings, system_settings=sys_s):
            return  # Azure/互換接続先の Codex は ChatGPT ログインでなくキーで認証する
        raise RuntimeError(keys.NO_CENTRAL_KEY_MESSAGE)
    r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=_AI_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "未ログイン（codex login が必要）").strip()[:200])


# (id, 表示名, 落ちたときの全体への影響, check(settings, system_settings), 対処ヒント)。ヒント文言は `keys.NO_CENTRAL_KEY_MESSAGE` を共有する
_AI_COMPONENTS = [
    ("answer_ai", "回答の AI（簡易の回答）", "none", _ai_check_answer,
     "管理画面の既定の AI・使えるモデル・接続先（またはキー）を確認してください"),
    ("codex", "Codex 調査", "none", _ai_check_codex,
     "codex CLI の導入とログイン（codex login）、または Ollama の接続先とモデルを確認してください（使わない構成なら対応不要）"),
    ("embed", "埋め込み（意味での検索）", "none", _ai_check_embed,
     "埋め込みの接続先とモデル（またはキー）を確認してください（使わない構成なら対応不要）"),
]


def ai_snapshot(uid: str, settings: dict, force: bool = False) -> list[dict]:
    """管理者本人の設定を使って、用途ごとの AI（回答の AI・Codex・埋め込み）の軽い確認を流す（システム状態ページ用・AI へは送らない）。

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


# ---- 管理者の「システム状態」画面専用（資料の中身: ES/グラフの 1 件読み） ----
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
            row = session.run(Query(
                "OPTIONAL MATCH (gm:SherpaMeta {world_id:$world}) WITH gm "
                "MATCH (n:Entity {world_id:$world}) "
                f"WHERE {world_neo4j._generation_pred('n')} RETURN n LIMIT 1",
                timeout=_SEARCH_TIMEOUT), world=world_id).single()
    return "ヒットあり" if row else "該当データが空です（未取り込み）"


# (id, 表示名, 落ちたときの全体への影響, probe(world_id), 対処ヒント)。impact="none" は参考情報（page 全体の status を変えない）
_SEARCH_COMPONENTS = [
    ("es_search", "全文検索（1 件読めるか）", "none", _search_probe_es,
     "Elasticsearch の起動・索引の取り込み状況を確認してください"),
    ("graph_search", "つながりの検索（1 件読めるか）", "none", _search_probe_graph,
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
