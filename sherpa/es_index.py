"""Elasticsearch 連携（共有 KB のみ・資料フォルダ単位のインデックス・日本語 BM25＝kuromoji）。
ベクトル＋BM25 を REST（urllib）で扱う。ES 未起動でも落ちない（best-effort）。個人文書は共有 index に書かない。
索引対象は `corpus_docs.world_documents` の文書（設計書／テキスト／ソース＋Office 派生 MD）。doc_id＝rel_path、`scopes`（祖先フォルダ prefix 群）で範囲フィルタする。
Office／PDF は `{rel}.rag.md`（RAG 正本）をアンカー分割した本文を索引ソースにする（`{rel}.rag_chunks.jsonl` は citation／locator を運ぶ証跡。`index_world`／`_validate_rag_chunks` 参照）。
設計: docs/design/rag.md「ES 索引の構成」
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from . import corpus_docs, doc_text, embeddings, json_io, scope_infer, worlds
from . import layer as layer_mod
from . import scope as scope_mod
from .env_int import env_int
from .ingest import importance, text_kind
from .ingest.analyzers import registry as analyzer_registry

_log = logging.getLogger("sherpa")


# チャンク粒度（行・read_around と整合）。legacy チャンク経路のみ効く。値を変えると索引の中身が変わるため、`needs_reindex` が `_meta` の `chunk_lines` で drift を検知する
_CHUNK_LINES = 40
# `chunk_lines` が無い旧索引は既定 40 として扱う（`index_world` は既定値のとき meta に書かない）
_CHUNK_LINES_DEFAULT = 40
# `search()` が ES へ送る size の上限（資料検索のヒット数の既定 `parts.read.tools.MAX_HITS` を下回らない固定値）
_ES_SEARCH_K_MAX = 50
_TIMEOUT = 30
ES_MAPPING_VERSION = "8"  # マッピング／チャンクメタの版。上げると次回 sync で全資料フォルダが自動 reindex する（`needs_reindex` 参照）
# reindex のとき埋め込みはキャッシュから再利用される。
# rag_chunks.jsonl 読み取りの安全弁（1 文書分）: 桁違いに超える入力は破損とみなし、ファイル全体を無効にして legacy チャンクへ縮退する（`_validate_rag_chunks` 参照）。
# 資料フォルダ全体の bulk 送信量は `_ES_BULK_BATCH_MAX_DOCS`／`_ES_BULK_BATCH_MAX_BYTES` が別に境界を持つ
_RAG_CHUNKS_FILE_CAP_BYTES = 32 * 1024 * 1024  # 1 ファイルの読み取り上限バイト（超過は無効）
# レコード単位チャンク（xlsx の行・docx の段落等）の正常なファイル 1 本を拒否しないよう緩めてある
_RAG_CHUNKS_MAX_ROWS = 200000  # 1 ファイルが持てるチャンク行数の上限（超過は無効）
_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS = 20000  # 1 チャンクの索引本文の文字数上限（超過は無効）
# bulk 送信のバッチ境界（件数とバイト数の両方。`_bulk_batches` 参照）。1 本の `_bulk` に詰めるとメモリが際限なく増えるため
_ES_BULK_BATCH_MAX_DOCS = 2000
_ES_BULK_BATCH_MAX_BYTES = 8 * 1024 * 1024
# 埋め込みのフラッシュ単位: `_embed_cached()` の不足分をこの件数のバッチに割り、成功ごとにキャッシュ DB へ即時フラッシュする
# （保持するベクトル量を有界にし、落ちても次回 sync でキャッシュヒットして再開できる）。`index_world()` の doc グループ化バッチサイズにも兼用する
_EMBED_FLUSH_CHUNKS = 500
_embed_log = logging.getLogger("sherpa.embed")  # 埋め込み進捗はここへ（log_setup.py の embed.log 行き）

# 重要度スコアブースト: `高`／`低` の function_score 係数。`中`／未設定は等倍で、重要度制御ファイルの無い資料フォルダはスコア不変
_ES_IMPORTANCE_BOOST_HIGH = 1.2
_ES_IMPORTANCE_BOOST_LOW = 0.85


# `search()` のハイブリッドにおける BM25(keyword) 対 kNN(vector) の配分（0.0＝vector 寄り〜1.0＝keyword 寄り・0.5＝boost キーを書かない）
_HYBRID_WEIGHT = 0.5


def _mapping(dim, analyzer: str, emeta=None) -> dict:
    """index マッピング。`analyzer`＝kuromoji／standard。`dim` で dense_vector(cosine)、`emeta` で埋め込み素性を `_meta` に記録する。"""
    props = {
        "doc_id": {"type": "keyword"}, "ext": {"type": "keyword"},
        # `corpus_docs.classify_document` の確定判定（"source"＝code／それ以外＝docs。`layer.es_filter` 参照）
        "branch": {"type": "keyword"},
        "top_scope": {"type": "keyword"},
        "scopes": {"type": "keyword"},  # 祖先フォルダ prefix 群（範囲フィルタ＝prefix 一致）
        "line": {"type": "integer"},
        "text": {"type": "text", "analyzer": analyzer},
        # 抽出来歴（office_md の meta.json 由来・表示のみ）。派生 MD 文書のみ付く
        "extraction_method": {"type": "keyword"},  # ooxml / pdf_text / vision
        "confidence": {"type": "float"},  # アームの確信度（0.0〜1.0）
        "has_conflicts": {"type": "boolean"},  # 決定的マージで conflicts が出た文書か
        # rag_chunks 由来チャンクのみ持つ。`chunk_id` は生成側の record 単位キー（ES の `_id` は `_rag_chunk_es_id` で doc_id と束ねる）。
        # `locator` は citation 由来の原本位置で、形が種別ごとに変わるため `enabled:false`（_source には残るが検索対象にしない）
        "chunk_id": {"type": "keyword"},
        "locator": {"type": "object", "enabled": False},
        # 隣接キー（rag チャンクのみ）: 前後チャンク・所属領域（`parent_id`＝親返しが読む）・レコード識別子・見出し経路。全て keyword
        "previous_chunk_id": {"type": "keyword"},
        "next_chunk_id": {"type": "keyword"},
        "parent_id": {"type": "keyword"},
        "logical_record_id": {"type": "keyword"},
        "section_path": {"type": "keyword"},
        # 登録者が `_重要度.txt` で付けた重要度。`importance` はブースト（function_score の term filter）とフィルタ／表示に使う。`importance_reason` は表示専用（`index: False`）
        "importance": {"type": "keyword"},
        "importance_reason": {"type": "keyword", "index": False},
    }
    if dim:
        props["embedding"] = {"type": "dense_vector", "dims": dim, "index": True, "similarity": "cosine"}
    # `index_world()` はクリーン再索引（delete→create→bulk）。bulk 中に部分的な投入が検索から見えないよう、作成時は背景 refresh を止め（`-1`）、
    # 最終バッチ（`refresh=true`・`_StreamingBulkSender.finish()`）で一度だけ refresh して全件を可視化し、通常値へ戻す（`_restore_refresh_interval`）
    m = {"settings": {"index": {"refresh_interval": "-1"}}, "mappings": {"properties": props}}
    if emeta:
        m["mappings"]["_meta"] = emeta  # {embed_provider, embed_model, dim, embed_algo}（検索時の素性照合用）
    return m


def _index_meta(world: str) -> dict | None:
    """index に記録した埋め込み素性（`_meta`）。取得失敗（GET 例外・到達不可等）は `None`、index はあるが `_meta` が無い正当な不在は `{}` で区別する。
    既存 `_meta` を読み直して書き戻す PUT 系（`confirm_human_md_meta`／`_confirm_content_sig`／`_wipe_after_bulk_failure`）は `None` のとき PUT をスキップする（`_meta` は丸ごと置換のため、一時失敗で既存値を消さない）。
    """
    try:
        r = _req("GET", f"/{_index(world)}/_mapping")
        for v in r.values():
            return (v.get("mappings") or {}).get("_meta") or {}
        return {}
    except Exception:
        return None


def _settings(s):
    if s is not None:
        return s
    try:
        from . import store
        return store.get_settings()
    except Exception:
        return {}


def _embed_system_settings_snapshot() -> dict | None:
    """`embeddings.cfg()`／`cloud_selected_but_unavailable()` へ渡す system_settings スナップショットを 1 回だけ読む。`SHERPA_DISABLE_EMBED` が有効な間は読まず `None` を返す。"""
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return None
    from . import store as _store
    return _store.get_system_settings()


def _url() -> str:
    # `ES_URL` の明示を最優先し、無ければ `SHERPA_ES_PORT`（docker-compose.yml と共用）に追随する
    url = os.environ.get("ES_URL")
    if not url:
        url = f"http://localhost:{os.environ.get('SHERPA_ES_PORT') or '9200'}"
    return url.rstrip("/")


def _index(world: str) -> str:
    """ES index 名（小文字）。資料フォルダ ID の大小文字違いで衝突しないよう厳密ハッシュを付す。"""
    slug = re.sub(r"[^a-z0-9._-]", "-", world.lower())[:40].strip("-._") or "w"
    return f"sherpa-kb-{slug}-{hashlib.sha1(world.encode('utf-8')).hexdigest()[:10]}"


def _req(method: str, path: str, body=None, ndjson: bool = False, timeout: int = _TIMEOUT):
    if ndjson:
        data = body.encode("utf-8")
        ctype = "application/x-ndjson"
    else:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        ctype = "application/json"
    req = urllib.request.Request(_url() + path, data=data, method=method, headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


_AVAILABLE_TIMEOUT = 1.0  # 秒


def available() -> bool:
    """ES に到達できるか（未起動なら False＝各処理は no-op／空になる）。
    timeout は短め（1 秒）。不達時にこの呼び出しを待つ全経路（`agentic_search.tool_availability` の single-flight lock 内を含む）の待ち時間上限になる。
    """
    try:
        _req("GET", "/", timeout=_AVAILABLE_TIMEOUT)
        return True
    except Exception:
        return False


def _scopes(rel: str) -> list:
    return scope_infer.ancestor_scopes(rel)  # 祖先 prefix（導出は scope_infer に集約）


def _provenance_meta(d: dict) -> dict:
    """派生 MD の来歴サイドカー（`{md}.meta.json`）から検索表示用のチャンクメタを取り出す。
    返値（あれば）: `extraction_method`・`confidence`（0.0〜1.0）・`has_conflicts`（決定的マージで conflicts が出た文書）。無ければ省略し、ソース文書（`md_path` 無し）や meta.json 欠落は `{}`。検索スコアには反映しない（表示のみ）。読取失敗・型不正は無視する。
    """
    mp = d.get("md_path")
    if not mp:
        return {}
    raw = json_io.read_json(Path(str(mp) + ".meta.json"))  # 無い／壊れは None
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    method = raw.get("method")
    if isinstance(method, str) and method:
        out["extraction_method"] = method
    conf = raw.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        out["confidence"] = float(conf)
    if "conflicts" in raw:  # マージが走った文書だけ has_conflicts を立てる
        out["has_conflicts"] = bool(raw.get("conflicts"))
    return out


def _arms_config_sig() -> str | None:
    """今の office_md アーム構成署名（`office_md._current_arms_sig()`）。取得失敗は None（fail-safe）。
    `needs_reindex` の drift 判定に使う（アーム構成が変わっても `content_sig` は変わらないため、この署名で派生 MD の中身の古さを検知する）。
    """
    try:
        from .ingest import office_md
        return office_md._current_arms_sig()
    except Exception:
        return None


def _analyzer_config_sig() -> str:
    """コード解析アナライザの有効構成署名（`analyzer_registry.config_signature()`）を文字列化する。
    `needs_reindex` の drift 判定に使い、アナライザ構成の変更で `branch` が古くなった索引を `content_sig` 比較に頼らず独立に検知する。
    `_meta` は JSON 往復でタプルが配列になるため、`repr()` で文字列に固定してから保存・比較する。
    """
    return repr(analyzer_registry.config_signature())


# pending（ES がまだ現行版へ追随できていない）を表す明示センチネル（meta のフィールド欠落・旧索引の None と区別するため）
_HUMAN_MD_PENDING_SENTINEL = "pending"


def _human_md_config_sig(world: str) -> str | None:
    """人間向け MD（`human_md`）レンダラ／抽出器の今の版（世界がその版まで ES 索引に反映できているかの判定込み）。
    RAG_ES に関わらず常に評価する（rag_chunks が無効な文書は 40 行チャンクへ縮退し、その実体は `iter_world_documents(include_rag=True)` が返す `md_path`＝rag.md があればそれ、無ければ legacy `{rel}.md`）。
    pending（資料フォルダ単位のホールドバック）はセンチネル文字列を返す。次のどちらかが True の間は `_HUMAN_MD_PENDING_SENTINEL`（"pending"）を返し続ける。
    (a) `office_md.human_md_sig_drift`＝render 側が現行版に追随できていない rel が残っている。
    (b) `office_md.human_md_es_sig_drift`＝ES がこの版までの bulk 成功をまだ確定できていない（`.human_md_es_sig` マーカー）。
    bulk の成否は `worker` が確認した後に `office_md.confirm_human_md_es_sig` でマーカーを確定する。pending を `None` で返すと meta 未設定の旧索引と区別できないため、センチネルを使う。
    ES meta には pending を書かず、センチネルのとき `index_world` は meta の `human_md_sig` を `None` にする（確定した版だけを meta に書く）。
    再索引は資料フォルダ単位（per-document の reindex は無い）。取得失敗（例外）は pending ではなく `None`（無闇な reindex ループを起こさない）。
    """
    try:
        from .ingest import office_md
        wd = worlds.world_dir(world)
        dmd = worlds.derived_md_dir(world)
        if wd and dmd.exists() and (
                office_md.human_md_sig_drift(wd, dmd, world=world) or office_md.human_md_es_sig_drift(dmd)):
            return _HUMAN_MD_PENDING_SENTINEL
        return office_md._current_human_md_sig()
    except Exception:
        return None


def confirm_human_md_meta(world: str) -> bool:
    """既存索引の `_meta.human_md_sig` を確定値へ書き直す（Put Mapping API で `_meta` だけを更新する）。
    `index_world()` の `ensure_index()` は pending 中なら meta に `None` を書く。bulk 成功後に `worker.index_world_with_human_md_holdback` がマーカーを確定したら、ここで meta も現行署名へ書き直す（直さないと毎 sync 無駄な再索引を繰り返す）。
    既存の `_meta` は読み直して保持し、`human_md_sig` だけ上書きする。索引が無い・到達不可・まだ pending なら False。
    """
    sig = _human_md_config_sig(world)
    if sig == _HUMAN_MD_PENDING_SENTINEL:
        return False
    existing = _index_meta(world)
    if existing is None:  # GET 失敗＝既存 meta を消して PUT しない（次回 sync が再試行）
        _log.warning("es_index: human_md_sig 確定前の meta 取得に失敗しました（次回 sync で再試行）: world=%s", world)
        return False
    try:
        meta = dict(existing)
        meta["human_md_sig"] = sig
        _req("PUT", f"/{_index(world)}/_mapping", {"_meta": meta})
        return True
    except Exception:
        return False


def delete_world(world: str) -> bool:
    """資料フォルダのインデックスを削除する（無ければ無視）。wipe の派生物伝播で呼ぶ。"""
    try:
        _req("DELETE", "/" + _index(world))
        return True
    except urllib.error.HTTPError as e:
        return e.code == 404  # 既に無い＝成功扱い
    except Exception:
        return False


def _confirm_content_sig(world: str, content_sig) -> None:
    """bulk が全バッチ成功した後に `_meta.content_sig`・`_meta.doc_count` を 1 回の read-modify-write で書く（Put Mapping API で `_meta` だけ更新）。
    `doc_count` は ES 側の実件数（`count(world)`）で、`needs_reindex` の実件数照合に使う。content_sig と同じ書き込みにまとめ、件数取得か書き込みが失敗すれば content_sig も書かない（次回 sync が fail-closed で 1 回張り直す）。
    """
    if not content_sig:
        return
    n = count(world)
    if n is None:
        _log.warning("es_index: 実件数の取得に失敗しました（次回 sync が1回だけ張り直す）: world=%s", world)
        return
    existing = _index_meta(world)
    if existing is None:  # GET 失敗＝既存 meta を消して PUT しない（次回 sync が再試行）
        _log.warning("es_index: content_sig 確定前の meta 取得に失敗しました（次回 sync が1回だけ張り直す）: world=%s", world)
        return
    try:
        meta = dict(existing)
        meta["content_sig"] = content_sig
        meta["doc_count"] = n
        _req("PUT", f"/{_index(world)}/_mapping", {"_meta": meta})
    except Exception:
        _log.warning("es_index: content_sig/doc_count の確定に失敗しました（次回 sync が1回だけ張り直す）: world=%s", world)


def _restore_refresh_interval(world: str) -> None:
    """`_mapping()` が索引作成時に立てた `refresh_interval:-1` を、bulk 全件成功後にクラスタ既定（"1s"）へ戻す（`null` を渡すと既定に復帰する）。
    失敗しても完全性には影響しない（最終バッチの `refresh=true` で全件可視化済み）。次回の `index_world()` が作り直す。
    """
    try:
        _req("PUT", f"/{_index(world)}/_settings", {"index": {"refresh_interval": None}})
    except Exception:
        _log.warning("es_index: refresh_interval の復帰に失敗しました（次回 reindex で上書きされます）: world=%s", world)


def _wipe_after_bulk_failure(world: str) -> None:
    """bulk 途中失敗時に資料フォルダの索引を空へ戻す（全部か無しか）。wipe 自体が失敗しても次回 sync が張り直せる状態にする。
    `ensure_index()` は bulk の前に `_meta.content_sig` を書くため、wipe が失敗すると「一部だけ入った索引＋有効な content_sig」が居座る。そこで wipe に失敗したら `_meta.content_sig` を落として fail-closed にする（content_sig が無い索引は `needs_reindex()` で必ず不一致になる）。
    meta の書き換えにも失敗するなら ES 自体が落ちており、次回 sync の `available()` が False で索引は使われない。
    """
    if delete_world(world):
        return
    # 目的は meta の温存でなく索引の無効化。GET 失敗（None）でも `{}` として PUT し、少なくとも `content_sig` を落とす
    existing = _index_meta(world) or {}
    try:
        meta = dict(existing)
        meta.pop("content_sig", None)
        _req("PUT", f"/{_index(world)}/_mapping", {"_meta": meta})
    except Exception:
        _log.warning("es_index: bulk 失敗後の wipe と meta 無効化の両方に失敗しました"
                     "（次回 sync の再索引に委ねる）: world=%s", world)


def list_kb_indices() -> list:
    """現存する Sherpa の KB 索引名（`sherpa-kb-*`）一覧。ES 不可は []（best-effort）。"""
    try:
        rows = _req("GET", "/_cat/indices/sherpa-kb-*?format=json&h=index")
        return [r["index"] for r in (rows or []) if r.get("index")]
    except Exception:
        return []


def reconcile(valid_worlds) -> list:
    """孤児 ES 索引の自動掃除: `sherpa-kb-*` のうち、登録資料フォルダの現行索引名のいずれにも一致しないものを削除する。
    `valid_worlds` は確実に取得できた登録 ID 集合（不確実なら呼ばない）。返り値は削除した索引名。ES 不可・個別失敗は握って続行する。
    """
    keep = {_index(w) for w in (valid_worlds or [])}
    deleted = []
    for idx in list_kb_indices():
        if idx in keep:
            continue
        try:
            _req("DELETE", "/" + idx)
            deleted.append(idx)
        except Exception:
            pass  # 個別失敗は次回リコンサイルで再試行
    return deleted


# 直近の `ensure_index()` が実際に作った索引のアナライザ（資料フォルダ ID → "kuromoji"／"standard"）。`index_world()` が取り出して
# 標準への退避を結果へ載せる（`ensure_index()` の戻り値は bool のまま）。既存索引だった場合は入れない。
_created_analyzer: dict[str, str] = {}


def ensure_index(world: str, dim=None, emeta=None) -> bool:
    """index を作成する（既存なら True）。アナライザ不明（400）は standard で再試行する（作ったアナライザは `_created_analyzer`）。`dim`／`emeta` で kNN 用。"""
    idx = "/" + _index(world)
    _created_analyzer.pop(world, None)
    for analyzer in ("kuromoji", "standard"):
        try:
            _req("PUT", idx, _mapping(dim, analyzer, emeta))
            _created_analyzer[world] = analyzer
            return True
        except urllib.error.HTTPError as e:
            try:
                txt = e.read().decode("utf-8", "replace")
            except Exception:
                txt = ""
            if "resource_already_exists" in txt:  # 既存（delete 後は通常起きない）
                return True
            if e.code == 400 and analyzer == "kuromoji":
                continue  # kuromoji 不明 → standard で再試行
            return False  # それ以外の 400／エラーは失敗
        except Exception:
            return False
    return False


# 埋め込みキャッシュのストア（SQLite の KV 1 ファイル）。資料フォルダ全体のベクトルを dict で持たず、フラッシュに含まれるキーだけを SELECT／INSERT する
# （メモリ有界・1 回のフラッシュの I/O がキー数に比例）
_EMBED_CACHE_DB_NAME = "embed_cache.sqlite3"
_EMBED_CACHE_SQL_CHUNK = 500  # 1 回の IN 節に含める上限（SQLite の変数上限への余裕）


def _embed_cache_db_path(world: str) -> Path:
    """資料フォルダの埋め込みキャッシュ（SQLite・1 ファイル）の場所。"""
    d = worlds.semantic_dir(world)
    return d / _EMBED_CACHE_DB_NAME


def _embed_cache_connect(world: str, *, create: bool) -> sqlite3.Connection | None:
    """埋め込みキャッシュ DB へ接続する。`create=False`（読み取り専用）で DB ファイルが無ければ接続せず None（空 DB を作らない）。WAL を有効化する。接続・初期化の失敗（権限等）は None（呼び出し元は miss／書込失敗として扱う）。"""
    p = _embed_cache_db_path(world)
    if not create and not p.exists():
        return None
    for attempt in (0, 1):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(p), timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, vec TEXT NOT NULL)")
            except sqlite3.Error:
                conn.close()
                raise
            return conn
        except sqlite3.DatabaseError as exc:
            # 壊れた DB ファイル（"file is not a database" 等）は、書込側（`create=True`）だけが本体と WAL／SHM を 1 回だけ捨てて作り直す（キャッシュは再 embed で戻る）。読取側は miss 扱いのまま
            if create and attempt == 0 and not isinstance(exc, sqlite3.OperationalError):
                _embed_log.warning("es_index: embed キャッシュDBが壊れているため作り直す（world=%s・%s）",
                                   world, type(exc).__name__)
                _delete_embed_cache(world)
                continue
            return None
        except sqlite3.Error:
            return None
    return None


def _sql_chunks(items: list, size: int = _EMBED_CACHE_SQL_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _embed_cache_lookup_batch(world: str, keys: list, dim: int) -> dict:
    """`keys` のうちキャッシュ済み・形状検証 OK（list かつ次元一致）のものだけ返す（壊れ／型ズレは miss）。`_EMBED_CACHE_SQL_CHUNK` 件ずつ `SELECT ... WHERE key IN (...)` で引く。DB 接続・クエリの失敗も miss 扱い（呼び出し元は再 embed へ）。"""
    if not keys:
        return {}
    conn = _embed_cache_connect(world, create=False)
    if conn is None:
        return {}
    out: dict = {}
    try:
        for chunk in _sql_chunks(keys):
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(f"SELECT key, vec FROM kv WHERE key IN ({placeholders})", chunk).fetchall()
            for k, raw in rows:
                try:
                    v = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(v, list) and len(v) == dim:
                    out[k] = v
    except sqlite3.Error:
        pass  # ここまでに読めた分は活かす
    finally:
        conn.close()
    return out


def _embed_cache_write_batch(world: str, new_vectors: dict) -> None:
    """新規ベクトルを DB へ upsert する（1 トランザクション・呼び出しごとに commit）。フラッシュ済み分が次回キャッシュヒットすることで再開性を担保する（`_embed_cached` 参照）。
    書込失敗（ENOSPC・DB 接続不能を含む OSError）は呼び出し元へ伝播させる（握りつぶすと永続化されていないバッチを成功扱いにしてしまう）。
    """
    if not new_vectors:
        return
    rows = [(k, json.dumps(v)) for k, v in new_vectors.items()]
    for attempt in (0, 1):
        conn = _embed_cache_connect(world, create=True)
        if conn is None:
            raise OSError(f"embed cache DB へ接続できません（world={world}）")
        try:
            with conn:
                conn.executemany("INSERT OR REPLACE INTO kv(key, vec) VALUES (?, ?)", rows)
            return
        except sqlite3.DatabaseError as exc:
            # ページ破損は書込で初めて発覚する。書込側が 1 回だけ作り直す。OperationalError（ENOSPC・ロック・権限）は破損ではないので消さずに伝播する
            if attempt == 0 and not isinstance(exc, sqlite3.OperationalError):
                conn.close()
                _embed_log.warning("es_index: embed キャッシュDBが壊れているため作り直す（world=%s・%s）",
                                   world, type(exc).__name__)
                _delete_embed_cache(world)
                continue
            raise OSError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise OSError(str(exc)) from exc
        finally:
            conn.close()


def _delete_embed_cache(world: str) -> None:
    """埋め込み無効／現存チャンク無し時のキャッシュ全消去。DB 本体＋WAL／SHM を消す（失敗はベストエフォート）。"""
    p = _embed_cache_db_path(world)
    for f in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
        try:
            f.unlink()
        except OSError:
            pass


def _prune_embed_cache(world: str, valid_keys: set) -> None:
    """資料フォルダ全体の doc ストリームを一巡し、全チャンクの embed が完了した直後にだけ呼ぶ最終剪定。現存キー（`valid_keys`）以外を消す。`valid_keys` が空なら DB ごと削除する。
    `index_world()` から 1 回だけ呼ぶ（`_embed_cached()` は doc グループごとに複数回呼ばれるため、呼ぶたびに剪定すると前のグループ分を消す）。削除失敗は呼び出し元へ伝播させる。
    """
    if not valid_keys:
        _delete_embed_cache(world)
        return
    if not _embed_cache_db_path(world).exists():
        return  # キャッシュ自体が無い＝剪定するものが無い
    conn = _embed_cache_connect(world, create=False)
    if conn is None:
        # DB が存在するのに接続できない（権限・ロック・破損）＝障害。OSError で打ち切らせる（黙って戻ると既存索引の delete へ進んで索引が消えたまま残る）
        raise OSError(f"embed cache DB へ接続できません（world={world}）")
    try:
        valid_key_set = set(valid_keys)  # 1 回だけ set 化して使い回す
        existing = {row[0] for row in conn.execute("SELECT key FROM kv").fetchall()}
        missing = len(valid_key_set - existing)
        if missing:
            # Pass1 完了時点で現存キーは全て DB にあるはず。不足は異常（壊れた DB の作り直し等）なので OSError で打ち切り、既存索引を残す（次回 sync で再 embed される）
            raise OSError(f"embed cache に現存キーが不足しています（world={world}・missing={missing}）")
        to_delete = list(existing - valid_key_set)
        with conn:
            for chunk in _sql_chunks(to_delete):
                placeholders = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM kv WHERE key IN ({placeholders})", chunk)
    except sqlite3.Error as exc:
        raise OSError(str(exc)) from exc
    else:
        # DELETE だけでは空きページが残るため、`freelist_count` が正なら VACUUM で返す。VACUUM の失敗は警告に留め、次回の判定で再試行する
        try:
            if conn.execute("PRAGMA freelist_count").fetchone()[0] > 0:
                conn.execute("VACUUM")
        except sqlite3.Error as exc:
            _embed_log.warning("es_index: embed キャッシュの VACUUM に失敗（world=%s・%s）——次回 sync で再試行",
                               world, type(exc).__name__)
    finally:
        conn.close()


def _chunk_key(ec: dict, text: str) -> str:
    """埋め込みキャッシュのキー＝(プロバイダ|モデル|次元|前処理アルゴリズム版|本文) の SHA1。素性が変われば別キーになり自動で再 embed される。"""
    return hashlib.sha1(
        f"{ec['provider']}|{ec['model']}|{ec['dim']}|{embeddings.EMBEDDING_INPUT_ALGORITHM_ID}|{text}"
        .encode("utf-8")
    ).hexdigest()


def _embed_cached(world: str, texts: list, ec) -> tuple:
    """チャンク埋め込みを内容ハッシュでキャッシュし、未変更チャンクの再 embed（API コスト）を省く。
    返値 `(vectors|None, reused, embedded)`。embed 失敗／次元不一致は `(None,0,0)`（呼び出し元は BM25 のみへ降格し、既存キャッシュは壊さない）。重複チャンクは 1 回だけ embed し、キャッシュ済みベクトルも形状を検証する（壊れは miss）。
    `texts` は呼び出し元が有界なバッチに分けて渡す（`index_world()` は doc 単位に `_EMBED_FLUSH_CHUNKS` 件程度ずつ複数回呼ぶ）。本関数はこのバッチのキーを追加／更新するだけで、剪定は呼び出し元が 1 回だけ `_prune_embed_cache()` で行う。`ec` が None・`texts` が空なら `(None,0,0)`。
    キャッシュ本体は SQLite KV（`_embed_cache_lookup_batch`／`_embed_cache_write_batch`）。不足分は `_EMBED_FLUSH_CHUNKS` 件単位のバッチに割り、成功のたびに DB へ即時フラッシュする（途中で落ちても次回キャッシュヒットで再開できる）。
    """
    if not ec or not texts:
        return None, 0, 0
    dim = ec["dim"]
    keys = [_chunk_key(ec, t) for t in texts]
    key_text = {}  # distinct チャンク（重複本文は 1 回だけ embed）
    for k, t in zip(keys, texts):
        key_text.setdefault(k, t)
    filled = _embed_cache_lookup_batch(world, list(key_text), dim)  # 形状検証済みの既存ベクトルだけ再利用
    need = [k for k in key_text if k not in filled]
    if need:
        total = len(need)
        n_batches = (total + _EMBED_FLUSH_CHUNKS - 1) // _EMBED_FLUSH_CHUNKS
        for bi, start in enumerate(range(0, total, _EMBED_FLUSH_CHUNKS)):
            batch_keys = need[start:start + _EMBED_FLUSH_CHUNKS]
            new = embeddings.embed([key_text[k] for k in batch_keys], ec, world=world)
            if not new:  # 失敗／次元不一致＝BM25 のみ（flush 済み分は温存）
                return None, 0, 0
            new_map = {}
            for k, vec in zip(batch_keys, new):
                filled[k] = vec
                new_map[k] = vec
            try:
                _embed_cache_write_batch(world, new_map)  # 成功したバッチだけ即座に永続化
            except OSError:
                # DB 書込障害（ENOSPC 等）を flush 成功として扱わず、embed 失敗と同じ扱い（BM25 のみへ降格・既存キャッシュは壊さない）にする
                _embed_log.warning(
                    "es_index: embed キャッシュDBの書込に失敗（world=%s・ENOSPC等）"
                    "——このバッチを embed 失敗として扱う", world)
                return None, 0, 0
            if bi == 0 or bi == n_batches - 1 or (bi + 1) % 10 == 0:  # 間引いて進捗を残す
                _embed_log.info("es_index: embed 進捗 %d/%d チャンク（world=%s）",
                                 min(start + len(batch_keys), total), total, world)
    return [filled[k] for k in keys], len(key_text) - len(need), len(need)


def _rag_chunk_source_exts() -> frozenset:
    """rag_chunks.jsonl を持ちうる拡張子（Office／PDF のみ。`office_md.OFFICE_EXT` が唯一の真実源）。ソース／テキスト文書に同名の sidecar があっても対象外にする。"""
    from .ingest import office_md
    return office_md.OFFICE_EXT


def _rag_chunk_es_id(doc_id: str, chunk_id: str) -> str:
    """rag チャンクの ES `_id`。`doc_id` を束ねた決定的ハッシュで名前空間化し、複製文書や stale sidecar があっても異なる文書間で上書きし合わないようにする（`chunk_id` 自体は検索結果へそのまま渡す）。"""
    return "ragchunk:" + hashlib.sha1(f"{doc_id}\x00{chunk_id}".encode("utf-8")).hexdigest()


def _safe_rag_chunks_path(derived: Path, rel: str) -> tuple:
    """`{rel}.rag_chunks.jsonl` の安全な読み取りパスを検証する。
    返値 `(path, reason)`。`(path, None)`＝読んでよい。`(None, None)`＝存在しない（通常の縮退・報告不要）。`(None, reason)`＝存在するが安全に読めない（symlink・derived root 外への脱出等・報告対象）。
    symlink は resolve 前に拒否し、resolve 後は `derived` 配下に収まることを確認する。
    """
    p = derived / (rel + ".rag_chunks.jsonl")
    if p.is_symlink():
        return None, "symlink_rejected"
    if not p.is_file():
        return None, None
    try:
        resolved = p.resolve(strict=True)
        droot = derived.resolve(strict=True)
    except OSError:
        return None, "resolve_failed"
    if not (resolved == droot or resolved.is_relative_to(droot)):
        return None, "path_confinement_failed"
    return resolved, None


def _safe_rag_md_path(derived: Path, rel: str) -> tuple:
    """`{rel}.rag.md`（RAG 正本）の安全な読み取りパスを検証する。契約・返値は `_safe_rag_chunks_path` と同じ（symlink 拒否・derived 配下への閉じ込め）。"""
    p = derived / (rel + ".rag.md")
    if p.is_symlink():
        return None, "symlink_rejected"
    if not p.is_file():
        return None, None
    try:
        resolved = p.resolve(strict=True)
        droot = derived.resolve(strict=True)
    except OSError:
        return None, "resolve_failed"
    if not (resolved == droot or resolved.is_relative_to(droot)):
        return None, "path_confinement_failed"
    return resolved, None


_RAG_MD_CHUNK_ANCHOR_RE = re.compile(r"^<!-- chunk:(\S+) -->\r?\n", re.MULTILINE)


def _parse_rag_md_chunks(markdown: str) -> tuple[dict[str, str], str | None]:
    """rag.md をアンカー（`<!-- chunk:{chunk_id} -->`）で分割し、`{chunk_id: 本文}` を返す。
    アンカーが 1 つも無ければ `({}, "rag_md_no_anchors")`、重複があれば `({}, "rag_md_duplicate_anchor")`（呼び出し側は文書全体を無効として legacy 40 行チャンクへ縮退する）。本文はアンカー行の直後から次のアンカー（無ければ末尾）までを `strip()` したもの。
    """
    matches = list(_RAG_MD_CHUNK_ANCHOR_RE.finditer(markdown))
    if not matches:
        return {}, "rag_md_no_anchors"
    bodies: dict[str, str] = {}
    for i, m in enumerate(matches):
        chunk_id = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        if chunk_id in bodies:
            return {}, "rag_md_duplicate_anchor"
        bodies[chunk_id] = markdown[start:end].strip()
    return bodies, None


_RAG_MD_CHUNK_ANCHOR_LINE_RE = re.compile(r"^<!-- chunk:(\S+) -->$")


def rag_md_anchor_chunk_id(line: str) -> str | None:
    """1 行が rag.md のチャンクアンカーなら、その chunk_id を返す（でなければ None）。`_parse_rag_md_chunks` と同じ形式を行単位の走査向けに公開する（`agentic_search` の親返しが使う）。"""
    m = _RAG_MD_CHUNK_ANCHOR_LINE_RE.match(line)
    return m.group(1) if m else None


def chunk_ids_for_parent(world: str, doc_id: str, parent_ids: list, *, limit: int = 5000) -> list:
    """`doc_id` の rag チャンクのうち `parent_id` が `parent_ids` のいずれかに一致する `chunk_id` を ES から取得する（親返し用の領域内チャンク集合）。ES 不達・クエリ失敗は空リスト（best-effort）。"""
    ids = [p for p in parent_ids if isinstance(p, str) and p]
    if not ids or not available():
        return []
    body = {"size": limit, "_source": ["chunk_id"],
            "query": {"bool": {"filter": [{"term": {"doc_id": doc_id}}, {"terms": {"parent_id": ids}}]}}}
    try:
        res = _req("POST", f"/{_index(world)}/_search", body)
    except Exception:
        return []
    out = []
    for h in res.get("hits", {}).get("hits", []):
        cid = (h.get("_source") or {}).get("chunk_id")
        if isinstance(cid, str) and cid:
            out.append(cid)
    return out


def _chunk_locator(chunk: dict) -> dict | None:
    """rag チャンクの代表 locator（先頭 citation の locator）。無ければ None。"""
    citations = chunk.get("citations")
    if not isinstance(citations, list) or not citations:
        return None
    first = citations[0]
    locator = first.get("locator") if isinstance(first, dict) else None
    return locator if isinstance(locator, dict) else None


_CHUNK_CONTEXT_STR_KEYS = ("previous_chunk_id", "next_chunk_id", "parent_id", "logical_record_id")


def _chunk_context_meta(chunk: dict) -> dict:
    """rag チャンクの隣接キー。欠落・型不正はキーを立てないだけにし、`_validate_rag_chunks` の無効化条件には含めない。"""
    out: dict = {}
    for key in _CHUNK_CONTEXT_STR_KEYS:
        v = chunk.get(key)
        if isinstance(v, str) and v:
            out[key] = v
    sp = chunk.get("section_path")
    if isinstance(sp, list) and sp and all(isinstance(x, str) and x for x in sp):
        out["section_path"] = sp
    return out


def _load_rag_md_anchors(md_path: Path | None) -> tuple:
    """`{rel}.rag.md`（RAG 正本）を読み、アンカー辞書 `(anchors, reason)` を返す。`reason` が付けば `anchors` は None。"""
    if md_path is None:
        return None, "rag_md_missing"
    try:
        if md_path.stat().st_size > _RAG_CHUNKS_FILE_CAP_BYTES:
            return None, "rag_md_too_large"
    except OSError:
        return None, "rag_md_stat_failed"
    try:
        markdown = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None, "rag_md_invalid_utf8"
    except OSError:
        return None, "rag_md_read_failed"
    anchors, anchor_reason = _parse_rag_md_chunks(markdown)
    if anchor_reason is not None:
        return None, anchor_reason
    return anchors, None


def _rag_chunks_validate(rag_path: Path, anchors: dict, rel: str) -> tuple:
    """`{rel}.rag_chunks.jsonl` を検証のみ行う（本文／メタは保持せず、`chunk_id` の集合だけを持つ。1 文書のチャンク数に比例したメモリを使わない）。
    有効なら `(seen_chunk_ids, None)`（0 件なら空集合）、無効なら `(None, reason)`。`reason` の語彙は `_validate_rag_chunks` 参照（rag.md 側の `rag_md_*` は `_load_rag_md_anchors` が担当）。
    """
    try:
        if rag_path.stat().st_size > _RAG_CHUNKS_FILE_CAP_BYTES:
            return None, "file_too_large"
    except OSError:
        return None, "stat_failed"
    seen_chunk_ids: set = set()
    try:
        with rag_path.open("r", encoding="utf-8", errors="strict") as f:
            for lineno, raw_line in enumerate(f, start=1):
                if lineno > _RAG_CHUNKS_MAX_ROWS:
                    return None, "too_many_rows"
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    return None, "invalid_json"
                if not isinstance(row, dict):
                    return None, "row_not_object"
                cid = row.get("chunk_id")
                if not (isinstance(cid, str) and cid):
                    return None, "missing_chunk_id"
                if row.get("source_rel_path") != rel:
                    return None, "source_rel_path_mismatch"
                if cid in seen_chunk_ids:
                    return None, "duplicate_chunk_id"
                seen_chunk_ids.add(cid)
                text = anchors.get(cid)
                if not (isinstance(text, str) and text.strip()):
                    return None, "rag_md_anchor_missing"
                if len(text) > _RAG_CHUNK_SEARCH_TEXT_MAX_CHARS:
                    return None, "search_text_too_long"
    except UnicodeDecodeError:
        return None, "invalid_utf8"
    except OSError:
        return None, "read_failed"
    if set(anchors) - seen_chunk_ids:
        return None, "rag_md_anchor_surplus"
    return seen_chunk_ids, None


def _iter_rag_chunk_entries(rag_path: Path, rel: str, anchors: dict, base_meta: dict):
    """事前に `_rag_chunks_validate()` が `reason=None` を返した `rag_path` をもう一度走査し、`(id, body, text)` を 1 件ずつ yield する（常に 1 件分だけをメモリに持つ）。検証済みの `rag_path`／`anchors` でのみ呼ぶ。"""
    with rag_path.open("r", encoding="utf-8", errors="strict") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            row = json.loads(line)
            cid = row["chunk_id"]
            text = anchors[cid]
            body = {**base_meta, "chunk_id": cid, "text": text}
            locator = _chunk_locator(row)
            if locator is not None:
                body["locator"] = locator
            body.update(_chunk_context_meta(row))  # 隣接キー（無ければ何も足さない）
            yield _rag_chunk_es_id(rel, cid), body, text


def _validate_rag_chunks(rag_path: Path, md_path: Path | None, rel: str, base_meta: dict) -> tuple:
    """`{rel}.rag_chunks.jsonl`（証跡サイドカー）と `{rel}.rag.md`（RAG 正本）を突き合わせて検証し、ES bulk 用の `(ids, bodies, texts, reason)` を返す（一括版。`index_world()` は有界メモリ版の `_load_rag_md_anchors`／`_rag_chunks_validate`／`_iter_rag_chunk_entries` を直接使う）。
    `reason` が None なら使ってよい（チャンク 0 件も正常）。`reason` が付けばファイル全体を無効とみなし、呼び出し側は 40 行チャンクへ縮退する（部分採用はしない）。`reason` の語彙:
    - `rag_md_missing`: jsonl はあるのに対になる rag.md が無い。
    - `rag_md_no_anchors`: rag.md にアンカー（`<!-- chunk:{chunk_id} -->`）が 1 つも無い（旧形式を含む）。
    - `rag_md_duplicate_anchor`: 同じ chunk_id のアンカーが複数ある。
    - `rag_md_anchor_missing`: jsonl の chunk_id に対応するアンカーが rag.md に無い。
    - `rag_md_anchor_surplus`: jsonl のどの chunk_id とも対応しないアンカーが余っている。
    - `invalid_utf8`／`invalid_json`／`row_not_object`: 非空行が UTF-8 として不正、JSON として壊れている、dict でない。
    - `missing_chunk_id`: 必須フィールドが欠落／空。
    - `source_rel_path_mismatch`: 行の `source_rel_path` が `rel`（原本）と食い違う。
    - `duplicate_chunk_id`: 同一ファイル内で `chunk_id` が重複。
    - `file_too_large`／`too_many_rows`／`search_text_too_long`: `_RAG_CHUNKS_FILE_CAP_BYTES`／`_RAG_CHUNKS_MAX_ROWS`／`_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS` 超過。
    - `rag_md_too_large`／`rag_md_stat_failed`／`rag_md_invalid_utf8`／`rag_md_read_failed`: rag.md 側の読み取り失敗。
    jsonl は逐次読み、rag.md は上限（jsonl と同じ）付きで一括読みする。索引本文・埋め込み対象は rag.md をアンカー分割した本文で、`line` は立てない。
    """
    anchors, reason = _load_rag_md_anchors(md_path)
    if reason is not None:
        return [], [], [], reason
    seen, reason = _rag_chunks_validate(rag_path, anchors, rel)
    if reason is not None:
        return [], [], [], reason
    ids, bodies, texts = [], [], []
    for cid_key, body, text in _iter_rag_chunk_entries(rag_path, rel, anchors, base_meta):
        ids.append(cid_key)
        bodies.append(body)
        texts.append(text)
    return ids, bodies, texts, None


def _bulk_batches(ids: list, bodies: list, vec_by_idx: dict) -> list:
    """`ids`／`bodies`（＋ `vec_by_idx` の embedding）を ES `_bulk` 用の NDJSON ペイロードへ、件数（`_ES_BULK_BATCH_MAX_DOCS`）とバイト数（`_ES_BULK_BATCH_MAX_BYTES`）の両方で有界なバッチに分割する。
    1 チャンクが上限を超えても単独バッチとして送る。返り値は各バッチの NDJSON 本文（末尾改行込み）。
    """
    out: list = []
    lines: list = []
    n_docs = 0
    n_bytes = 0
    for i, (cid, body) in enumerate(zip(ids, bodies)):
        v = vec_by_idx.get(i)  # branch=="source"／軽量テキスト枠は埋め込み対象外＝常に None
        if v is not None:
            body = {**body, "embedding": v}
        action = json.dumps({"index": {"_id": cid}})
        doc = json.dumps(body, ensure_ascii=False)
        pair_bytes = len(action.encode("utf-8")) + len(doc.encode("utf-8")) + 2  # +2 = 各行の改行
        if lines and (n_docs >= _ES_BULK_BATCH_MAX_DOCS or n_bytes + pair_bytes > _ES_BULK_BATCH_MAX_BYTES):
            out.append("\n".join(lines) + "\n")
            lines, n_docs, n_bytes = [], 0, 0
        lines.append(action)
        lines.append(doc)
        n_docs += 1
        n_bytes += pair_bytes
    if lines:
        out.append("\n".join(lines) + "\n")
    return out


def _iter_doc_chunk_records(world: str, d: dict, derived: Path | None, rag_exts: frozenset,
                            res_map: dict | None = None) -> tuple:
    """1 文書分のチャンクを `(id, body, text, no_embed)` を 1 件ずつ返すジェネレータへ組み立てる（巨大な 1 文書でも 1 件ごとに flush 判定できるようにする）。
    rag_chunks 経路は yield を始める前に `_rag_chunks_validate()` で jsonl 全体を検証し終える。失敗なら 1 件も yield せず legacy 40 行チャンクへ縮退する（doc 単位の原子性）。検証成功後にファイルを開き直して `_iter_rag_chunk_entries` で yield する。
    `res_map`（省略可）: `importance.resolve_for_world()` の結果。あれば `importance`／`importance_reason` を全チャンクの body へ条件付きで焼き込む。
    返値 `(chunk_iter, degraded_entry)`。
    - `chunk_iter`＝None: この文書はスキップ（unreadable／本文の読み取り失敗＝reason="text_read_failed"／本文が空白のみ＝reason="empty_text"）。
    - `chunk_iter`＝`(id, body, text, no_embed)` を yield するジェネレータ（0 件もありうる）。
    - `degraded_entry`＝None または {"doc": rel, "reason": ...}（rag_chunks はあるが使えなかった場合のみ。yield を始める前に確定する）。
    """
    if d.get("state") == "unreadable":  # 分類を唯一のゲートにする（再読が成功しても索引しない）
        return None, None
    rel = d["name"]
    ext = Path(rel).suffix.lower()
    # 軽量テキスト枠（`ingest.text_kind`）の第 1 段・第 2 段はどちらも ES 索引の対象にする（`reachable_as_text` 契約）
    is_light_text = d.get("doctype") in (text_kind.CODE_DOCTYPE_LABEL, text_kind.DOCUMENT_DOCTYPE_LABEL)
    # 軽量テキスト枠は「ベクトル・グラフ・LLM を一切通さない」ため、登録コード（`branch=="source"`）と同様に embed 対象から除外する（`no_embed`）
    no_embed = d.get("branch") == "source" or is_light_text
    meta = {"doc_id": rel, "ext": ext, "branch": d.get("branch"),
            "top_scope": d.get("top_scope"), "scopes": _scopes(rel)}
    meta.update(_provenance_meta(d))  # 抽出来歴を搬送（無ければ省略）
    if res_map:  # importance／importance_reason（無ければ省略）
        meta.update(importance.public_fields(res_map.get(rel)))
    degraded_entry = None
    if derived is not None and meta["ext"] in rag_exts:
        rag_path, path_reason = _safe_rag_chunks_path(derived, rel)
        if path_reason is not None:
            degraded_entry = {"doc": rel, "reason": path_reason}
        elif rag_path is not None:
            # rag.md（正本）が jsonl（証跡サイドカー）と対になっているかを先に見る
            md_path, md_path_reason = _safe_rag_md_path(derived, rel)
            if md_path_reason is not None:
                degraded_entry = {"doc": rel, "reason": md_path_reason}
            elif md_path is None:
                degraded_entry = {"doc": rel, "reason": "rag_md_missing"}
            else:
                anchors, reason = _load_rag_md_anchors(md_path)
                seen = None
                if reason is None:
                    seen, reason = _rag_chunks_validate(rag_path, anchors, rel)
                if reason is None and seen:  # 有効かつ 1 件以上
                    def _rag_chunk_iter(rag_path=rag_path, anchors=anchors, meta=meta, no_embed=no_embed):
                        for cid_key, body, text in _iter_rag_chunk_entries(rag_path, rel, anchors, meta):
                            yield cid_key, body, text, no_embed
                    return _rag_chunk_iter(), None
                if reason is not None:
                    degraded_entry = {"doc": rel, "reason": reason}
                # 検証成功で 0 件チャンク（正しく空）の場合は degraded にせず legacy 縮退へ続ける
    text = doc_text.read_world_doc_text(world, d)  # ソース／テキスト、または rag_chunks の無い／無効な Office／PDF
    if text is None:
        # 本文を読めず索引から外す文書（rag_chunks が使えなかった理由があっても、外れたことを優先して報告する）
        return None, {"doc": rel, "reason": "text_read_failed"}
    if text.strip() == "":
        # 本文が空白のみ（空ファイル／空派生 MD）は「処理対象外」に分類する（空文書だけの資料フォルダが `index_world()` の no_chunks ガードに恒常的に該当しないように）
        return None, {"doc": rel, "reason": "empty_text"}

    def _legacy_chunk_iter(text=text, meta=meta, no_embed=no_embed):
        rows = text.splitlines()
        for s in range(0, max(1, len(rows)), _CHUNK_LINES):
            chunk = "\n".join(rows[s:s + _CHUNK_LINES]).strip()
            if not chunk:
                continue
            yield f"{rel}#{s + 1}", {**meta, "line": s + 1, "text": chunk}, chunk, no_embed

    return _legacy_chunk_iter(), degraded_entry


class _StreamingBulkSender:
    """`index_world()` Pass2 が doc グループ単位で積むチャンクを、bulk 送信の境界（件数／バイト数の閾値）に達し次第 ES へ流す。
    `refresh=true` は最後の送信だけに付ける。最後かどうかは全 doc を処理し終えるまで分からないため、直前に確定した 1 バッチを `_pending` として 1 つだけ持ち越し（lag-by-one）、`finish()` で最後の `_pending` を refresh 付きで送る。
    """

    def __init__(self, world: str):
        self.world = world
        self._pending: str | None = None
        self.failed = False
        self.error: str | None = None

    def _send(self, payload: str, refresh: bool) -> bool:
        path = f"/{_index(self.world)}/_bulk" + ("?refresh=true" if refresh else "")
        try:
            res = _req("POST", path, payload, ndjson=True)
        except Exception:
            # 途中バッチの失敗は呼び出し元が wipe する（全部か無しか。一部だけ入った索引は取りこぼしになる）
            self.failed, self.error = True, "bulk_failed"
            return False
        if res.get("errors"):  # item-level の失敗（HTTP 200 でも起きる）
            self.failed, self.error = True, "bulk_errors"
            return False
        return True

    def send_group(self, ids: list, bodies: list, vec_by_idx: dict) -> bool:
        """1 グループ分（`_EMBED_FLUSH_CHUNKS` 件程度に有界）を bulk 用サブバッチへ分割し、持ち越し済みの前グループ分（refresh なし）→ このグループの内部境界（最後の 1 つ以外）の順で送る。このグループの最後のサブバッチは新たな `_pending` として持ち越す。"""
        if self.failed or not ids:
            return not self.failed
        batches = _bulk_batches(ids, bodies, vec_by_idx)
        if not batches:
            return True
        if self._pending is not None:
            if not self._send(self._pending, refresh=False):
                return False
            self._pending = None
        for payload in batches[:-1]:
            if not self._send(payload, refresh=False):
                return False
        self._pending = batches[-1]
        return True

    def finish(self) -> bool:
        """最後に残った持ち越し分を refresh 付きで送る（何も送っていなければ no-op）。"""
        if self.failed:
            return False
        if self._pending is not None:
            ok = self._send(self._pending, refresh=True)
            self._pending = None
            return ok
        return True


def _flush_doc_group(sender: _StreamingBulkSender, world: str, ids: list, bodies: list,
                     texts: list, no_embed: list, ec, embed_feature_applies: bool) -> bool:
    """Pass2 の 1 グループ（doc 数件・チャンク `_EMBED_FLUSH_CHUNKS` 件程度に有界）を、必要なら埋め込みキャッシュから embedding を引いて bulk 送信する（`sender.send_group` へ委譲）。
    キャッシュ参照は `embed_feature_applies` が真の時だけ（Pass1 が全チャンクの embed を完了させている前提）。
    埋め込み対象キーの miss は fail-loud にする（DB 破損・並行削除・書込障害等の異常。miss したチャンクだけ embedding 無しで送ると、同じ資料フォルダ内でベクトル付き／無しが混在するため）。miss を検知したら `sender.failed` を立てて bulk 送信ごと中止し、既存の「途中バッチ失敗＝ wipe」経路に乗せる。
    """
    vec_by_idx: dict = {}
    if embed_feature_applies:
        embed_positions = [i for i, skip in enumerate(no_embed) if not skip]
        if embed_positions:
            keys = [_chunk_key(ec, texts[i]) for i in embed_positions]
            hit = _embed_cache_lookup_batch(world, keys, ec["dim"])
            for i, k in zip(embed_positions, keys):
                v = hit.get(k)
                if v is None:
                    sender.failed, sender.error = True, "embed_cache_miss"
                    return False
                vec_by_idx[i] = v
    return sender.send_group(ids, bodies, vec_by_idx)


def index_world(world: str, settings: dict | None = None, content_sig: str | None = None,
                progress: Callable[[int, int], None] | None = None) -> dict:
    """資料フォルダをクリーン再索引する（delete→create→bulk）。埋め込み設定があればベクトルも付与する（kNN 用）。
    - 失敗は古い索引を残さず error を返す。埋め込みを一度も選んでいない構成は BM25 のみで索引する。明示選択したクラウドの埋め込みが解決できない・実際の embed が失敗した場合は、delete の前に打ち切る（既存のベクトル付き索引を BM25 のみで黙って上書きしない）。
    - `content_sig`＝索引時のフォルダ署名（`_meta` に保存・古い索引の検知に使う）。埋め込みは内容ハッシュキャッシュ（`_embed_cached`）経由で未変更チャンクの再 embed を省く。
    - Office／PDF（`{rel}.rag_chunks.jsonl` を持つ）はレコード単位チャンク、それ以外（ソース／テキスト、rag_chunks が無い／検証に失敗した Office／PDF）は 40 行チャンク。rag_chunks があるのに使えなかった文書と本文が空白のみの文書（reason="empty_text"）は、戻り値の `rag_degraded`（件数）・`rag_degraded_docs`（内訳）・`rag_degraded_by_reason`（理由別の件数）で報告する。本文を読めず索引から外した文書は reason="text_read_failed"。
      埋め込みに失敗して BM25 のみで索引したときは `embed_degraded=True`、日本語アナライザを作れず標準で作り直したときは `analyzer_fallback=True`（どちらも取り込みの状態へ出す）。
    - `branch=="source"`（登録コード＋軽量テキスト枠の汎用コード）のチャンクは埋め込み対象から除く（BM25 は全チャンクに効く）。
    - bulk 送信は `_bulk_batches()` で件数・バイト数ともに有界なバッチへ分割し、`refresh=true` は最後のバッチだけに付ける。途中のバッチが失敗したら `delete_world()` して空へ戻し、error を返す（全部か無しか）。
    - 2 パス構成（メモリを資料フォルダの規模に比例させない）:
      - Pass1（埋め込みのみ）: `_iter_doc_chunk_records()` でチャンクを 1 件ずつ受け取り、embed 対象テキストを `_EMBED_FLUSH_CHUNKS` 件程度に束ねて `_embed_cached()` を呼ぶ。
      - Pass2（bulk 送信）: 同じ `docs` をもう一度走査して id／body／text を 1 件ずつ受け取り、`_EMBED_FLUSH_CHUNKS` 件程度のグループに束ね、対象チャンクだけキャッシュから embedding を引いて `_StreamingBulkSender` で ES へ流す。
      Pass1 が資料フォルダ全体の embed の成否を確定してから Pass2 が始まるため、doc ごとにベクトル付き／無しが混在しない。キャッシュの剪定（現存チャンクだけへ縮める）は Pass1 成功直後（ES 操作の前）に `_prune_embed_cache()` で 1 回だけ行う。
    - `progress`（省略可）: Pass1 は文書 1 件処理するたび、Pass2 は文書グループを flush するたびに `progress(done_docs, total_docs)` を呼ぶ（`total_docs` は `docs` の長さ）。Pass2 開始時に `progress(0, total_docs)` を 1 回呼ぶ。Pass2 の `done_docs` は対象外（チャンク 0 件）の文書も 1 件に数えるので、`indexed` とは別の目盛り。Pass1→Pass2 の切替で一度小さく戻るため、単調増加を前提にしない。呼び出しの間引きは呼び出し元の責務。
    設計: docs/design/rag.md「ES 索引の構成」
    """
    if not available():
        return {"available": False, "indexed": 0, "chunks": 0}
    # `cfg()` と `cloud_selected_but_unavailable()` を同じ system_settings スナップショットで呼ぶ（kill-switch 有効時は読まない）
    sys_s = _embed_system_settings_snapshot()
    ec = embeddings.cfg(_settings(settings), system_settings=sys_s)
    if ec is None and embeddings.cloud_selected_but_unavailable(system_settings=sys_s):
        # 削除より前に打ち切る（既存索引を BM25 のみで上書きしない）
        return {"available": True, "indexed": 0, "chunks": 0, "error": "embedding_cloud_unavailable"}

    derived = worlds.derived_rag_dir(world)  # RAG 正本層
    rag_exts = _rag_chunk_source_exts()
    docs = corpus_docs.world_documents(world, include_rag=True)
    total_docs = len(docs)  # 進捗表示の total（materialize 済みの一覧の長さ）
    # 重要度は資料フォルダ全体を 1 回だけ解決し（`res_map`）、各文書のチャンク組み立てへ使い回す
    wd = worlds.world_dir(world)
    res_map = importance.resolve_for_world(world, root=wd) if wd else {}

    # ---- Pass1: 埋め込みのみ（doc 単位でテキストを束ね、`_EMBED_FLUSH_CHUNKS` 件ごとに flush）----
    had_embed_eligible = False
    embed_ok = True
    valid_keys: set = set()
    reused_total = 0
    embedded_total = 0
    embed_elapsed_ms = 0.0  # `_embed_cached()` 呼び出しの合計所要時間
    embed_calls_made = False  # 0（呼んだが瞬時）と未計測（一度も呼んでいない）を区別する
    if ec is not None:
        buf: list = []
        pass1_docs_done = 0
        for d in docs:
            chunk_iter, _degraded = _iter_doc_chunk_records(world, d, derived, rag_exts)
            if chunk_iter is None:
                continue
            for _cid, _body, t, skip in chunk_iter:
                if skip:
                    continue
                had_embed_eligible = True
                valid_keys.add(_chunk_key(ec, t))
                buf.append(t)
                if len(buf) >= _EMBED_FLUSH_CHUNKS:
                    _t0 = time.monotonic()
                    vecs, reused, embedded = _embed_cached(world, buf, ec)
                    embed_elapsed_ms += (time.monotonic() - _t0) * 1000
                    embed_calls_made = True
                    reused_total += reused
                    embedded_total += embedded
                    buf = []
                    if vecs is None:
                        embed_ok = False
                        break
            if not embed_ok:
                break
            pass1_docs_done += 1
            if progress is not None:
                # Pass1 の進捗も呼ぶ（冷キャッシュ時は Pass1 が最長段になりうるため）。Pass2 開始時に done は一度小さく戻る
                progress(pass1_docs_done, total_docs)
        if embed_ok and buf:
            _t0 = time.monotonic()
            vecs, reused, embedded = _embed_cached(world, buf, ec)
            embed_elapsed_ms += (time.monotonic() - _t0) * 1000
            embed_calls_made = True
            reused_total += reused
            embedded_total += embedded
            if vecs is None:
                embed_ok = False

    if ec is not None and had_embed_eligible and not embed_ok:
        # `cfg()` は解決できたが実際の embed 呼び出しが失敗した
        if embeddings.cloud_selected_but_unavailable(system_settings=sys_s):
            # まだ `delete_world()` を呼んでいない＝既存索引はそのまま残る
            out = {"available": True, "indexed": 0, "chunks": 0, "error": "embedding_cloud_unavailable"}
            if embed_calls_made:
                out["embed_elapsed_ms"] = round(embed_elapsed_ms)
            return out
        # クラウドを一度も選んでいない構成の実失敗は、BM25 のみへ降格して続行する

    # 埋め込みキャッシュの最終剪定／削除は、資料フォルダ全体の doc ストリームを一巡し終えた直後（ES 操作の前）に 1 回だけ行う。embed_ok が False の間はキャッシュへ触れない（フラッシュ済み分を温存する）
    if ec is None:
        _delete_embed_cache(world)
    elif embed_ok:
        try:
            _prune_embed_cache(world, valid_keys)
        except OSError:
            # DB 書込障害（ENOSPC 等）は成功扱いせず `delete_world()` の前に打ち切る（既存索引を残す）
            _embed_log.warning("es_index: embed キャッシュ剪定の書込に失敗（world=%s）"
                               "——索引の delete 前に中止する", world)
            return {"available": True, "indexed": 0, "chunks": 0, "error": "embed_cache_write_failed"}

    # `ec` が有効でも埋め込み対象チャンクが無い（`had_embed_eligible` が偽）資料フォルダは、embed が「この構成で最新」なので emeta に埋め込み素性を書く（書かないと `needs_reindex()` が毎 sync 不一致になる）。
    # 真の embed 失敗（`had_embed_eligible` があるのに `embed_ok` が偽）は書かず、次回 sync で再試行させる
    embed_feature_applies = ec is not None and (not had_embed_eligible or embed_ok)

    if not delete_world(world):  # 削除失敗のまま bulk すると stale chunk が残る
        return {"available": True, "indexed": 0, "chunks": 0, "error": "delete_failed"}

    dim = ec["dim"] if embed_feature_applies else None
    human_md_sig = _human_md_config_sig(world)
    if human_md_sig == _HUMAN_MD_PENDING_SENTINEL:
        # pending センチネルは meta に書かず `None` にする（確定した版だけを書く。bulk 成功後に呼び出し元がマーカーを確定する）
        human_md_sig = None
    emeta = {"world_id": world,  # 帰属を索引自身に刻む（孤児リコンサイルの所有者判定）
             "mapping_version": ES_MAPPING_VERSION,  # マッピング／チャンクメタの版（変わったら reindex）
             "arms_sig": _arms_config_sig(),  # アーム構成（変わったら reindex・fail-safe で None もありうる）
             "human_md_sig": human_md_sig,  # human_md 版（pending は書かない）
             "analyzer_config_sig": _analyzer_config_sig()}  # コード解析アナライザの有効構成（変わったら reindex）
    if _CHUNK_LINES != _CHUNK_LINES_DEFAULT:  # 既定(40)時は書かない
        emeta["chunk_lines"] = _CHUNK_LINES  # legacy チャンク粒度（既定と異なるときだけ記録・drift 検知用）
    # `content_sig` は bulk が全バッチ成功した後に書く（`_confirm_content_sig`）。先に書くと、途中でプロセスが落ちたとき「一部だけ入った索引＋有効な content_sig」が居座る。後書きなら、どう落ちても content_sig が無い＝次回 sync が必ず張り直す（fail-closed）
    if embed_feature_applies:  # had_embed_eligible が偽でも ec 由来の素性を書く
        emeta.update({"embed_provider": ec["provider"], "embed_model": ec["model"], "dim": ec["dim"],
                      "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID})  # 前処理アルゴリズム版（`_chunk_key` と同じ材料）
    if not ensure_index(world, dim=dim, emeta=(emeta or None)):
        return {"available": True, "indexed": 0, "chunks": 0, "error": "create_failed"}
    analyzer_fallback = _created_analyzer.pop(world, None) == "standard"  # 日本語アナライザを作れず標準で作り直した

    # ---- Pass2: チャンクを 1 件ずつ受け取り、`_EMBED_FLUSH_CHUNKS` 件ごとにグループ化して bulk 送信する ----
    n_docs = 0
    total_chunks = 0
    rag_degraded = 0
    rag_degraded_docs: list = []
    sender = _StreamingBulkSender(world)
    g_ids: list = []
    g_bodies: list = []
    g_texts: list = []
    g_no_embed: list = []
    # `docs_done` は Pass2 が見終えた文書数（対象外の文書も含む）。`n_docs`（実際に索引した文書数）とは別に持ち、対象外が連続しても `total_docs` へ収束させる
    docs_done = 0
    if progress is not None:
        # Pass2 開始時に 0/total を明示通知する（間引きラッパーが Pass1 完走時の done を覚えているため）
        progress(0, total_docs)
    for d in docs:
        chunk_iter, degraded_entry = _iter_doc_chunk_records(world, d, derived, rag_exts, res_map)
        if degraded_entry is not None:
            rag_degraded += 1
            rag_degraded_docs.append(degraded_entry)
        if chunk_iter is None:
            docs_done += 1
            if progress is not None:
                progress(docs_done, total_docs)
            continue
        n_docs += 1
        docs_done += 1
        for cid, body, text, skip in chunk_iter:
            g_ids.append(cid)
            g_bodies.append(body)
            g_texts.append(text)
            g_no_embed.append(skip)
            total_chunks += 1
            if len(g_ids) >= _EMBED_FLUSH_CHUNKS:
                if not _flush_doc_group(sender, world, g_ids, g_bodies, g_texts, g_no_embed, ec, embed_feature_applies):
                    break
                g_ids, g_bodies, g_texts, g_no_embed = [], [], [], []
                if progress is not None:
                    progress(docs_done, total_docs)
        if sender.failed:
            break
    if not sender.failed and g_ids:
        _flush_doc_group(sender, world, g_ids, g_bodies, g_texts, g_no_embed, ec, embed_feature_applies)
    if progress is not None:
        # 末尾の leftover が 0 件でも最終呼び出し（`done == total`）を必ず 1 回行う
        progress(docs_done, total_docs)

    rag_report = {"rag_degraded": rag_degraded}
    if rag_degraded_docs:
        rag_report["rag_degraded_docs"] = rag_degraded_docs
        by_reason: dict = {}
        for entry in rag_degraded_docs:
            by_reason[entry["reason"]] = by_reason.get(entry["reason"], 0) + 1
        rag_report["rag_degraded_by_reason"] = by_reason
    if ec is not None and had_embed_eligible and not embed_ok:
        rag_report["embed_degraded"] = True    # 埋め込みに失敗し、BM25 のみで索引した（クラウド未選択の構成）
    if analyzer_fallback:
        rag_report["analyzer_fallback"] = True

    if sender.failed or not sender.finish():
        # 途中バッチの失敗は資料フォルダを空へ戻す（全部か無しか）
        _wipe_after_bulk_failure(world)
        out = {"available": True, "indexed": 0, "chunks": 0, "error": sender.error, **rag_report}
        if embed_calls_made:
            out["embed_elapsed_ms"] = round(embed_elapsed_ms)
        return out
    if n_docs > 0 and total_chunks == 0:
        # 文書があるのにチャンク 0 件は索引の異常として扱い、content_sig を確定させない（次回 sync が再索引する）。空の資料フォルダ（`n_docs==0`）は対象外
        _restore_refresh_interval(world)  # 索引自体は作成済み（refresh_interval=-1 のまま）
        out = {"available": True, "indexed": 0, "chunks": total_chunks, "error": "no_chunks", **rag_report}
        if embed_calls_made:
            out["embed_elapsed_ms"] = round(embed_elapsed_ms)
        return out
    _restore_refresh_interval(world)  # 全バッチ成功＝最終 refresh で可視化済み・背景 refresh を通常へ戻す
    _confirm_content_sig(world, content_sig)  # 全バッチ成功後にだけ鮮度署名と実件数を確定する
    out = {"available": True, "indexed": n_docs, "chunks": total_chunks,
          "vectors": bool(embed_feature_applies and had_embed_eligible),
          "embedded": embedded_total, "reused": reused_total, **rag_report}
    if embed_calls_made:
        # `embed_elapsed_ms`: 埋め込み呼び出しの合計所要時間。一度も呼んでいなければキーを付けない（0 と欠落を区別する）
        out["embed_elapsed_ms"] = round(embed_elapsed_ms)
    return out


def count(world: str) -> int | None:
    try:
        return _req("GET", f"/{_index(world)}/_count").get("count")
    except Exception:
        return None


def needs_reindex(world: str, content_sig, settings: dict | None = None) -> bool:
    """ES 索引の張り直しが要るか（ES 稼働時のみ）。次のいずれかで True:
    空／内容署名ズレ／アーム構成ズレ／マッピング版ズレ／チャンク粒度ズレ／人間向け MD 版ズレ／アナライザ構成ズレ／埋め込み素性（provider／model／dim／前処理アルゴリズム版）ズレ／実件数ズレ。
    `content_sig`（ソースファイルの rel／mtime／ctime／size のみ）はアーム構成・マッピング版等の変更を検知できないため、各署名を別途比較する。索引済みメタにフィールド自体が無い旧索引は比較先が None になり、不一致＝1 回だけ再索引される。
    例外: `human_md_sig` は pending 中は比較先がセンチネルになり、pending が解消するまで毎回再索引を試みる（fail-closed）。`chunk_lines` は欠落を旧既定 `_CHUNK_LINES_DEFAULT`（40）として扱う。
    実件数ズレ: `_meta.doc_count`（`_confirm_content_sig` が刻む）と `count(world)` が食い違えば True（資料は不変のまま ES の文書が欠けた状態の自己修復）。`doc_count` が無い旧索引ではこの条件だけ比較しない（スタンプを刻むためだけに有料の再埋め込みを走らせないため）。
    """
    if not available():
        return False
    n = count(world)
    if not n:
        return True
    meta = _index_meta(world) or {}  # GET 失敗は未設定相当＝fail-closed で reindex を促す
    if meta.get("content_sig") != content_sig:
        return True
    if meta.get("mapping_version") != ES_MAPPING_VERSION:
        return True
    if meta.get("chunk_lines", _CHUNK_LINES_DEFAULT) != _CHUNK_LINES:
        return True
    if meta.get("arms_sig") != _arms_config_sig():
        return True
    if meta.get("human_md_sig") != _human_md_config_sig(world):
        return True
    if meta.get("analyzer_config_sig") != _analyzer_config_sig():
        return True
    ec = embeddings.cfg(_settings(settings))
    want = (ec["provider"], ec["model"], ec["dim"], embeddings.EMBEDDING_INPUT_ALGORITHM_ID) if ec else (None, None, None, None)
    have = (meta.get("embed_provider"), meta.get("embed_model"), meta.get("dim"), meta.get("embed_algo"))
    if want != have:
        return True
    doc_count = meta.get("doc_count")
    return doc_count is not None and n != doc_count


# hybrid の match 節に付ける名前。ヒットの `matched_queries` に載れば語が一致した印（kNN だけで出たヒットは載らない）
_KEYWORD_QUERY_NAME = "keyword"


def _mark_keyword_all(hits: list) -> list:
    for h in hits:
        h["keyword_match"] = True
    return hits


def _parse_hits(res: dict) -> list:
    out = []
    for h in res.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        full = src.get("text", "")
        frag = (h.get("highlight", {}).get("text") or [full])[0]
        hit = {"doc_id": src.get("doc_id"), "line": src.get("line"),
               "text": frag, "score": h.get("_score"), "ext": src.get("ext")}
        # 抽出来歴・rag_chunks 由来メタ・重要度を表示用にそのまま渡す（無ければ付けない）。`parent_id` は親返しが読む。`importance_source` は出典には出さないので含めない
        for k in ("extraction_method", "confidence", "has_conflicts", "chunk_id", "locator",
                  "previous_chunk_id", "next_chunk_id", "parent_id", "logical_record_id", "section_path",
                  "importance", "importance_reason"):
            if src.get(k) is not None:
                hit[k] = src[k]
        if "matched_queries" in h:  # 名前付きクエリを持つ hybrid だけ ES が返す
            hit["keyword_match"] = _KEYWORD_QUERY_NAME in (h["matched_queries"] or [])
        out.append(hit)
    return out


def _importance_boost_query(bool_query: dict) -> dict:
    """`bool_query`（`{"bool": {...}}`）を function_score で包み、`importance` に応じてスコアを乗算する（`高`＝`_ES_IMPORTANCE_BOOST_HIGH` 倍・`低`＝`_ES_IMPORTANCE_BOOST_LOW` 倍）。
    `importance` を持たない文書（`中`／未設定・重要度制御ファイルの無い資料フォルダ）はどちらの filter にも一致せず 1 倍＝スコア不変。`score_mode="first"`（`importance` は単一値）・`boost_mode="multiply"`。
    """
    return {"function_score": {
        "query": bool_query,
        "functions": [
            {"filter": {"term": {"importance": "高"}}, "weight": _ES_IMPORTANCE_BOOST_HIGH},
            {"filter": {"term": {"importance": "低"}}, "weight": _ES_IMPORTANCE_BOOST_LOW},
        ],
        "score_mode": "first", "boost_mode": "multiply",
    }}


def _importance_score_multiplier(v) -> float:
    if v == "高":
        return _ES_IMPORTANCE_BOOST_HIGH
    if v == "低":
        return _ES_IMPORTANCE_BOOST_LOW
    return 1.0


def _rerank_knn_by_importance(hits: list) -> list:
    """純 kNN（`search_knn_only`）専用の取得後の再ランク。`knn` 節は function_score で包めないため、返ってきたスコアへ `_importance_boost_query` と同じ乗数を Python 側で掛け、安定ソートで並べ直す。
    `importance` の無いヒット（重要度制御ファイルの無い資料フォルダ）は乗数 1.0 のままでスコア・順序とも不変。
    """
    for h in hits:
        if h.get("score") is not None:
            h["score"] = h["score"] * _importance_score_multiplier(h.get("importance"))
    hits.sort(key=lambda h: -(h.get("score") or 0.0))
    return hits


def _classify_query_exception(exc: Exception) -> str:
    """BM25 クエリ実行時の例外を固定コードへ分類する（呼び出し元が回復可否を区別できるよう、握りつぶす前に 1 回だけ判定する）。例外は再送出しない（`search()`／`search_knn_only()` は例外を投げず `(hits, degrade_reason)` を返す契約）。
    - `HTTPError`: 404（索引未作成）・429・5xx は `es_query_failed`（回復可能）、それ以外の 4xx（クエリの拒否）は `es_query_rejected`（回復不可）。
    - それ以外の `OSError`（接続断・タイムアウト等）は `es_query_failed`。
    - 非通信例外（`JSONDecodeError`／`TypeError`／`KeyError` 等＝想定外の応答形）は `es_query_rejected`。
    """
    if isinstance(exc, urllib.error.HTTPError):
        return ("es_query_failed" if (exc.code in (404, 429) or 500 <= exc.code <= 599)
               else "es_query_rejected")
    if isinstance(exc, OSError):
        return "es_query_failed"
    return "es_query_rejected"


def search(world: str, query: str, scope_paths=None, k: int = 20, settings: dict | None = None,
          vector: bool = True, layer=None, k_ceiling: int | None = None) -> tuple[list, str | None]:
    """検索。`vector=True` かつ埋め込み設定があれば kNN＋BM25 のハイブリッド、無ければ BM25。範囲フィルタ・graceful。
    `vector=False` は BM25 のみ（クエリ埋め込みを呼ばない・reason は常に None）。
    `k_ceiling`（省略可）: 呼び出し元が上限まで検証済みの `k` を渡す場合（`agentic_search.run_tool` の `es_search`）、`_ES_SEARCH_K_MAX` による再クランプを迂回してこちらを使う。
    `layer`（省略可・`"docs"|"code"|"both"`・既定 both）: `scopes` と同じ `filter` 節に `branch` の term／must_not フィルタを積む（`layer.es_filter`）。
    各ヒットに `keyword_match`（語が一致したか）を付ける。hybrid は match 節の名前付きクエリで判定（kNN だけで出たヒットは False）・BM25 のみの経路は全件 True。
    返値 `(hits, degrade_reason|None)`（`search_knn_only()` と同じ形）。degrade 時も BM25 の hits をそのまま返す（reason は「hybrid でなく BM25 だけになった理由」の注記）。`es_unavailable`／クエリ空は `[]`。
    degrade_reason: `es_unavailable`／`embedding_not_configured`（埋め込み未設定）／`embedding_cloud_unavailable`／`vector_feature_mismatch`（索引の埋め込み素性が現在の設定と不一致＝再索引待ち）／`query_embed_failed`／`hybrid_query_failed`（hybrid が失敗し BM25 は成功）／`es_query_failed`（BM25 も失敗・一時的）／`es_query_rejected`（BM25 も失敗・クエリ不備）。`fused_search.DEGRADE_REASONS` と同一集合（増やすときは両方直す）。
    reason は呼び出し元が tool result 経由で思考の流れ（UI）へ表示する。
    """
    q = (query or "").strip()
    if not q:
        return [], None
    if not available():
        return [], "es_unavailable"
    k = max(1, min(k, k_ceiling if k_ceiling is not None else _ES_SEARCH_K_MAX))
    flt = []
    sel = scope_mod.normalize_scope_paths(scope_paths)
    if sel:
        flt.append({"terms": {"scopes": sel}})  # 選択 prefix のいずれかを含む doc に限定
    lfilt = layer_mod.es_filter(layer)
    if lfilt:
        flt.append(lfilt)
    hl = {"fields": {"text": {"fragment_size": 240, "number_of_fragments": 1}}}
    bm25 = {"size": k, "query": _importance_boost_query({"bool": {"must": [{"match": {"text": q}}], "filter": flt}}),
           "highlight": hl}
    # `cfg()` と `cloud_selected_but_unavailable()` を同じ system_settings スナップショットで呼ぶ（kill-switch 有効時は読まない）
    sys_s = _embed_system_settings_snapshot() if vector else None
    ec = embeddings.cfg(_settings(settings), system_settings=sys_s) if vector else None
    reason = None
    if vector and ec is None:
        if embeddings.cloud_selected_but_unavailable(system_settings=sys_s):
            reason = "embedding_cloud_unavailable"
            _log.warning("es_index.search: 選択中クラウドの埋め込みが解決できず world=%s は BM25 のみへ降格しました",
                         world)
        else:
            reason = "embedding_not_configured"  # 埋め込み未設定＝BM25 だけで検索した（注記のみ・失敗ではない）
    meta = (_index_meta(world) or {}) if ec else {}
    same = bool(ec) and (meta.get("embed_provider") == ec["provider"]
                         and meta.get("embed_model") == ec["model"] and meta.get("dim") == ec["dim"]
                         and meta.get("embed_algo") == embeddings.EMBEDDING_INPUT_ALGORITHM_ID)
    if ec and not same:
        # 索引のベクトル素性が現在の埋め込み設定と合わない（再索引待ち）。kNN は打てないので BM25 のみへ縮退し、クエリ埋め込みも呼ばない。理由を返して静かな縮退にしない
        reason = "vector_feature_mismatch"
    if same:  # 索引のベクトル素性が一致する時だけ kNN
        qv = embeddings.embed([q], ec, world=world)
        if qv:
            # 既定配分（w=0.5）のときは boost キーを書かない。0.5 以外のときだけ boost を付ける（合計 2.0 に配分）
            match_clause = {"match": {"text": {"query": q, "_name": _KEYWORD_QUERY_NAME}}}
            knn_clause = {"knn": {"field": "embedding", "query_vector": qv[0], "k": k,
                                  "num_candidates": max(50, k * 5), "filter": flt}}
            if _HYBRID_WEIGHT != 0.5:
                match_clause = {"match": {"text": {"query": q, "boost": _HYBRID_WEIGHT * 2.0,
                                                   "_name": _KEYWORD_QUERY_NAME}}}
                knn_clause["knn"]["boost"] = (1.0 - _HYBRID_WEIGHT) * 2.0
            # 重要度ブーストは合成スコア（BM25＋kNN）全体へ 1 回だけ掛ける。`knn` を top-level で並記すると `query` 側だけを包む function_score が BM25 成分にしか効かないため、
            # ES 8.9+ の query 節内の `knn` を使い、match／knn を同じ `bool.should` に並べて `_importance_boost_query` で包む。
            # union（どちらか一方だけに一致した文書も出る）で、重要度なしの資料フォルダではスコア不変
            combined = {"bool": {"should": [match_clause, knn_clause], "filter": flt}}
            hybrid = {"size": k, "highlight": hl, "query": _importance_boost_query(combined)}
            try:
                return _parse_hits(_req("POST", f"/{_index(world)}/_search", hybrid)), None
            except Exception:
                # hybrid 自体の失敗（次元不一致・未ベクトル索引等）は、BM25 が成功すれば hits が空にならないため、`es_query_failed` とは別の reason にする
                reason = "hybrid_query_failed"  # 実クエリ失敗 → BM25 へ
        else:
            reason = "query_embed_failed"  # クエリ埋め込みの通信失敗 → BM25 へ
    try:
        return _mark_keyword_all(_parse_hits(_req("POST", f"/{_index(world)}/_search", bm25))), reason
    except Exception as exc:
        # BM25 自体も失敗＝hits 空を最優先の理由で説明する
        return [], _classify_query_exception(exc)


def search_knn_only(world: str, query: str, scope_paths=None, k: int = 20,
                    settings: dict | None = None, layer=None, k_ceiling: int = 50) -> tuple[list, str | None]:
    """純 kNN 検索（BM25 を混ぜない・fused_search の vector エンジン用）。`search()` は kNN＋BM25 のハイブリッド固定のため、engines=["vector"] 単独用に top-level `knn` のみのクエリを発行する。
    返値 `(hits, degrade_reason|None)`。hits は `_parse_hits` 形 `{doc_id, line, text, score, ext}`。
    degrade_reason: es_unavailable／embedding_not_configured／embedding_cloud_unavailable／vector_feature_mismatch／query_embed_failed／es_query_failed（一時的）／es_query_rejected（クエリ不備）（`parts/read/fused_search.py` の `DEGRADE_REASONS` と同一・増やすときは両方直す）。
    `embedding_cloud_unavailable`（明示選択したクラウドが解決できない）は、`embedding_not_configured`（クラウドを選んでいない通常の未設定）と区別する。
    `layer`（省略可）: `search()` と同じ探す対象フィルタ。`k_ceiling`（省略可・既定 50）: `k` の上限（`search()` と同じく、上限まで検証済みの呼び出し元が引き上げる）。
    """
    q = (query or "").strip()
    if not q:
        return [], None
    if not available():
        return [], "es_unavailable"
    k = max(1, min(k, k_ceiling))
    # `cfg()` と `cloud_selected_but_unavailable()` を同じ system_settings スナップショットで呼ぶ（`search()` と同じ）
    sys_s = _embed_system_settings_snapshot()
    ec = embeddings.cfg(_settings(settings), system_settings=sys_s)
    if not ec:
        if embeddings.cloud_selected_but_unavailable(system_settings=sys_s):
            return [], "embedding_cloud_unavailable"
        return [], "embedding_not_configured"
    meta = _index_meta(world) or {}  # GET 失敗は未設定相当＝ベクトル素性不一致として扱う
    if not (meta.get("embed_provider") == ec["provider"]
            and meta.get("embed_model") == ec["model"] and meta.get("dim") == ec["dim"]
            and meta.get("embed_algo") == embeddings.EMBEDDING_INPUT_ALGORITHM_ID):
        return [], "vector_feature_mismatch"  # 索引素性ズレ
    qv = embeddings.embed([q], ec, world=world)
    if not qv:
        return [], "query_embed_failed"
    flt = []
    sel = scope_mod.normalize_scope_paths(scope_paths)
    if sel:
        flt.append({"terms": {"scopes": sel}})  # `search()` と同一の範囲フィルタ
    lfilt = layer_mod.es_filter(layer)
    if lfilt:
        flt.append(lfilt)
    # 上位ちょうど `k` 件を取ってから重要度で並べ替えると、「高」のヒットが取得時点で落選して浮上できない。有界の overfetch（`k*3`・上限 `k+50`）で多めに取り、補正・再ソートの後に `k` 件へ切る（重要度なしの資料フォルダは補正が no-op で結果不変）
    fetch_k = min(k * 3, k + 50)
    body = {"size": fetch_k,
            "knn": {"field": "embedding", "query_vector": qv[0], "k": fetch_k,
                    "num_candidates": max(50, fetch_k * 5), "filter": flt}}
    try:
        # 純 kNN は function_score で包めないため、取得後の再ランクで重要度ブーストを適用する
        hits = _rerank_knn_by_importance(_parse_hits(_req("POST", f"/{_index(world)}/_search", body)))
        return hits[:k], None
    except Exception as exc:
        return [], _classify_query_exception(exc)
