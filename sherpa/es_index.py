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
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import corpus_docs, doc_text, embeddings, json_io, scope_infer, worlds
from . import layer as layer_mod
from . import scope as scope_mod
from .env_int import env_int
from .ingest import importance, ocr_router, substage, text_kind
from .ingest.analyzers import registry as analyzer_registry

_log = logging.getLogger("sherpa")


# チャンク粒度（行・read_around と整合）。legacy チャンク経路のみ効く。値を変えると索引の中身が変わるため、`needs_reindex` が `_meta` の `chunk_lines` で drift を検知する
_CHUNK_LINES = 40
# `chunk_lines` が無い旧索引は既定 40 として扱う（`index_world` は既定値のとき meta に書かない）
_CHUNK_LINES_DEFAULT = 40
# `search()` が ES へ送る size の上限（資料検索のヒット数の既定 `parts.read.tools.MAX_HITS` を下回らない固定値）
_ES_SEARCH_K_MAX = 50
_TIMEOUT = 30
ES_MAPPING_VERSION = "14"  # マッピング／チャンクメタの版。上げると次回 sync で全資料フォルダが自動 reindex する（`needs_reindex` 参照）
# reindex のとき埋め込みはキャッシュから再利用される。
# rag_chunks.jsonl 読み取りの安全弁: 1 行（1 チャンク）の大きさで判定する。桁違いに大きい行は破損とみなし、ファイル全体を無効にして legacy チャンクへ縮退する（`_validate_rag_chunks` 参照）。
# ファイル全体の大きさは見ない（大きな表でも行ごとの切れを保つ）。読むときも上限＋1 文字で打ち切る
# 資料フォルダ全体の bulk 送信量は `_ES_BULK_BATCH_MAX_DOCS`／`_ES_BULK_BATCH_MAX_BYTES` が別に境界を持つ
_RAG_CHUNKS_LINE_CAP_BYTES = 1024 * 1024  # 1 行の読み取り上限（超過は無効）
# rag.md はアンカー辞書として本文を全部メモリに持つため、行数上限（下の `_RAG_CHUNKS_MAX_ROWS`）まで通る大きさにメモリとの釣り合いで上限を置く
_RAG_MD_FILE_CAP_BYTES = 256 * 1024 * 1024  # rag.md 1 ファイルの読み取り上限バイト（超過は無効）
# レコード単位チャンク（xlsx の行・docx の段落等）の正常なファイル 1 本を拒否しないよう緩めてある
_RAG_CHUNKS_MAX_ROWS = 200000  # 1 ファイルが持てるチャンク行数の上限（超過は無効）
_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS = 20000  # 1 チャンクの索引本文の文字数上限（超過は無効）
# bulk 送信のバッチ境界（件数とバイト数の両方。`_bulk_batches` 参照）。1 本の `_bulk` に詰めるとメモリが際限なく増えるため
_ES_BULK_BATCH_MAX_DOCS = 2000
_ES_BULK_BATCH_MAX_BYTES = 8 * 1024 * 1024
# 同時に ES へ流す `_bulk` の本数（設計: docs/design/rag.md「ES 索引の構成」）
_ES_BULK_PARALLEL = 4
# 埋め込みのフラッシュ単位: `_embed_cached()` の不足分をこの件数のバッチに割り、成功ごとにキャッシュ DB へ即時フラッシュする
# （保持するベクトル量を有界にし、落ちても次回 sync でキャッシュヒットして再開できる）。`index_world()` の doc グループ化バッチサイズにも兼用する
_EMBED_FLUSH_CHUNKS = 500
_embed_log = logging.getLogger("sherpa.embed")  # 埋め込み進捗はここへ（log_setup.py の embed.log 行き）
# ベクトルを持つ索引の作り直しで ES の大きさを抑える（ES はベクトルの写しを、flush の後のマージで初めて捨てる）。
# 索引の設定（`_mapping`）と、Pass2 の終わりの「詰める」（`_compact_building`）の 2 つで効かせる
_ES_TRANSLOG_FLUSH_SIZE = "256mb"
_ES_MERGE_SEGMENTS_PER_TIER = 4
_ES_COMPACT_MIN_CHUNKS = 100000     # 終わりに詰めるのは、この件数以上を入れた索引だけ
_ES_COMPACT_SETTLE_SEC = 35         # flush の後、ES が写しを捨ててよいと決めるまでの待ち
_ES_COMPACT_TIMEOUT_SEC = 1800      # forcemerge の終わりを待つ上限（過ぎたら待たずに先へ進む）
_ES_COMPACT_POLL_SEC = 5

# 重要度スコアブースト: `高`／`低` の function_score 係数。`中`／未設定は等倍で、重要度制御ファイルの無い資料フォルダはスコア不変
_ES_IMPORTANCE_BOOST_HIGH = 1.2
_ES_IMPORTANCE_BOOST_LOW = 0.85


# 検索の質問の埋め込みの総期限（秒）。env `SHERPA_SEARCH_EMBED_TIMEOUT_SEC`（1〜120・不正は既定）。過ぎたら BM25 だけで続ける（`query_embed_failed`）。取り込みの埋め込みの再送は変えない
_SEARCH_EMBED_TIMEOUT_DEFAULT = 10.0
_SEARCH_EMBED_TIMEOUT_MIN = 1.0
_SEARCH_EMBED_TIMEOUT_MAX = 120.0

# BM25 の対象欄と重み。本文 `text` が最も重く、見出し系（`labels`＝項目名など・`region_context`＝見出し・シート名）は控えめに足す。`tie_breaker` は複数欄に当たった文書をわずかに押し上げる
_BM25_FIELDS = ("text", "labels", "region_context")
_BM25_FIELD_BOOSTS = {"text": 1.0, "labels": 0.3, "region_context": 0.3}
_BM25_TIE_BREAKER = 0.1


# `search()` のハイブリッドにおける BM25(keyword) 対 kNN(vector) の配分（0.0＝vector 寄り〜1.0＝keyword 寄り・0.5＝boost キーを書かない）
_HYBRID_WEIGHT = 0.5


def _mapping(dim, analyzer: str, emeta=None) -> dict:
    """index マッピング。`analyzer`＝kuromoji／standard。`dim` で byte の dense_vector(cosine・`embeddings.vector_to_es_bytes` で縮めた値)、`emeta` で埋め込み素性を `_meta` に記録する。"""
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
        # 構造の欄（rag チャンクのみ）: 識別子の完全一致（`es_search` の `exact`）と、record・領域の文脈を引く欄。
        # `identifier_values`＝チャンクに出る識別子の全役割、`record_identity_values`＝そのレコードが定義している識別子、
        # `labels`＝レコードに同居する日本語の値（項目名など）、`region_context`＝所属領域の見出し・シート名
        "identifier_values": {"type": "keyword"},
        "record_identity_values": {"type": "keyword"},
        "labels": {"type": "text", "analyzer": analyzer},
        "region_context": {"type": "text", "analyzer": analyzer},
        # 登録者が `_重要度.txt` で付けた重要度。`importance` はブースト（function_score の term filter）とフィルタ／表示に使う。`importance_reason` は表示専用（`index: False`）
        "importance": {"type": "keyword"},
        "importance_reason": {"type": "keyword", "index": False},
        # ベクトルを取れなかったチャンクだけ "missing" を持つ（`fill_missing_vectors` が後から埋めて "ok" にする）
        "vector_status": {"type": "keyword"},
        # 表の行・本文のまとまりの先頭だけが持つ（設計: docs/design/rag.md「表の行・本文のまとまり」）。どちらも検索の対象にしない
        "vector_text": {"type": "text", "index": False},
        "row_group": {"type": "object", "enabled": False},
    }
    if dim:
        props["embedding"] = {"type": "dense_vector", "dims": dim, "element_type": "byte", "index": True,
                              "similarity": "cosine"}
    # `index_world()` は新しい物理索引を作って全件入れ、最後にエイリアスを付け替える。作成時は背景 refresh を止め（`-1`）、
    # 最終バッチ（`refresh=true`・`_StreamingBulkSender.finish()`）で一度だけ refresh して全件を可視化し、通常値へ戻す（`_restore_refresh_interval`）
    m = {"settings": {"index": {"refresh_interval": "-1"}}, "mappings": {"properties": props}}
    if dim:
        m["mappings"]["_source"] = {"excludes": ["embedding"]}  # ベクトルは原本の写しに持たない（kNN は索引側で動く・再利用は埋め込みキャッシュから）
        # 取り込み中に flush を増やしてマージを細かく起こし、ES が持ち続けるベクトルの写し（recovery source）を取り込み中に捨てさせる
        m["settings"]["index"].update({"translog.flush_threshold_size": _ES_TRANSLOG_FLUSH_SIZE,
                                       "merge.policy.segments_per_tier": _ES_MERGE_SEGMENTS_PER_TIER,
                                       "merge.policy.max_merge_at_once": _ES_MERGE_SEGMENTS_PER_TIER})
    if emeta:
        m["mappings"]["_meta"] = emeta  # {embed_provider, embed_model, dim, embed_algo, quant}（検索時の素性照合用）
    return m


def _index_meta(world: str, idx: str | None = None) -> dict | None:
    """index に記録した埋め込み素性（`_meta`）。取得失敗（GET 例外・到達不可等）は `None`、index はあるが `_meta` が無い正当な不在は `{}` で区別する。
    既存 `_meta` を読み直して書き戻す PUT 系（`confirm_human_md_meta`／`_confirm_content_sig`／`_wipe_after_bulk_failure`）は `None` のとき PUT をスキップする（`_meta` は丸ごと置換のため、一時失敗で既存値を消さない）。
    """
    try:
        r = _req("GET", f"/{idx or _index(world)}/_mapping")
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


# 索引名の付け方（設計: docs/design/rag.md「ES 索引の構成」）。検索・単一文書の更新が使う名前（`_index(world)`）は**エイリアス**で、
# 実体は `<エイリアス名>-g<世代>` の物理索引。全再構築は新しい世代を作って全件入れ、終わってからエイリアスを原子的に付け替える。
# 0.18 以前の「エイリアス名と同じ名前の実の索引」は、付け替えと同じ 1 回の `_aliases` で消して置き換える。
_PHYSICAL_SUFFIX_RE = re.compile(r"-g\d+$")
_building: dict[str, str] = {}  # 資料フォルダ ID → 作成中の物理索引名（`ensure_index` が入れ、`index_world` が付け替え・破棄のときに取り出す）


def _new_physical_name(world: str) -> str:
    return f"{_index(world)}-g{time.time_ns() // 1_000_000}"


def _physical_base(name: str) -> str:
    """物理索引名からエイリアス名を返す（世代の接尾辞が無ければそのまま）。"""
    return _PHYSICAL_SUFFIX_RE.sub("", name)


def _alias_targets(world: str) -> list[str] | None:
    """エイリアス `_index(world)` が指す物理索引。エイリアスでない（実の索引・無し）は []、取得失敗は None。"""
    alias = _index(world)
    try:
        res = _req("GET", f"/_alias/{alias}")
    except urllib.error.HTTPError as e:
        return [] if e.code == 404 else None
    except Exception:
        return None
    if not isinstance(res, dict):
        return []
    return sorted(k for k, v in res.items() if isinstance(v, dict) and alias in (v.get("aliases") or {}))


def _concrete_indices(world: str) -> list[str] | None:
    """この資料フォルダの実の索引（エイリアス名と同名の旧索引と、世代付きの物理索引）。取得失敗は None。"""
    alias = _index(world)
    try:
        rows = _req("GET", f"/_cat/indices/{alias}*?format=json&h=index&expand_wildcards=all")
    except urllib.error.HTTPError as e:
        return [] if e.code == 404 else None
    except Exception:
        return None
    names = [r.get("index") for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    return sorted(n for n in names if n == alias or (n.startswith(alias + "-g") and _physical_base(n) == alias))


def _index_kind(world: str) -> str | None:
    """`"alias"`（0.19 の形）／`"index"`（エイリアス名と同名の実の索引＝0.18 以前）／`"none"`／取得失敗 None。"""
    targets = _alias_targets(world)
    if targets is None:
        return None
    if targets:
        return "alias"
    names = _concrete_indices(world)
    if names is None:
        return None
    return "index" if _index(world) in names else "none"


def delete_world(world: str) -> bool:
    """資料フォルダの索引を全部削除する（エイリアスの指す物理索引・作りかけの物理索引・0.18 以前の実の索引。無ければ無視）。wipe の派生物伝播で呼ぶ。"""
    names = _concrete_indices(world)
    if names is None:
        return False
    ok = True
    for n in names:
        try:
            _req("DELETE", "/" + n)
        except urllib.error.HTTPError as e:
            ok = ok and e.code == 404  # 既に無い＝成功扱い
        except Exception:
            ok = False
    return ok


def _discard_building(world: str) -> None:
    """作りかけの物理索引を消す（エイリアスと古い索引には触れない）。失敗は次の作り直しの前の掃除に任せる。"""
    name = _building.pop(world, None)
    if not name or name == _index(world):
        return
    try:
        _req("DELETE", "/" + name)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            _log.warning("es_index: 作りかけの索引を消せませんでした（次回の作り直しで掃除します）: %s", name)
    except Exception:
        _log.warning("es_index: 作りかけの索引を消せませんでした（次回の作り直しで掃除します）: %s", name)


def _drop_stale_physicals(world: str) -> bool:
    """エイリアスが指していない物理索引（前回落ちた作りかけ）を消す。一覧を取れなければ何も消さず False。"""
    targets = _alias_targets(world)
    names = _concrete_indices(world)
    if targets is None or names is None:
        return False
    alias = _index(world)
    for n in names:
        if n == alias or n in targets:
            continue
        try:
            _req("DELETE", "/" + n)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return False
        except Exception:
            return False
    return True


def _activate_index(world: str, new_idx: str) -> bool:
    """エイリアスを `new_idx` へ原子的に付け替え、古い物理索引を消す。0.18 以前の実の索引は同じ `_aliases` の中で消して置き換える。"""
    alias = _index(world)
    if new_idx == alias:
        return True
    targets = _alias_targets(world)
    names = _concrete_indices(world)
    if targets is None or names is None:
        return False
    actions: list = [{"remove": {"index": t, "alias": alias}} for t in targets]
    if not targets and alias in names:
        actions.append({"remove_index": {"index": alias}})
    actions.append({"add": {"index": new_idx, "alias": alias}})
    try:
        _req("POST", "/_aliases", {"actions": actions})
    except Exception:
        return False
    _building.pop(world, None)
    for t in targets:
        try:
            _req("DELETE", "/" + t)
        except Exception:
            _log.warning("es_index: 古い索引を消せませんでした（次回の作り直しで掃除します）: %s", t)
    return True


_REBUILD_FREE_FACTOR = 2  # 空きが古い索引の大きさのこの倍に満たなければ、作り直しの前に古い索引を消す


def _es_free_bytes() -> int | None:
    """ES のデータ置き場の空き（`/_nodes/stats/fs` の available・ノードが複数なら最も少ないもの）。取れなければ None。"""
    try:
        res = _req("GET", "/_nodes/stats/fs")
    except Exception:
        return None
    nodes = res.get("nodes") if isinstance(res, dict) else None
    vals = [((n.get("fs") or {}).get("total") or {}).get("available_in_bytes")
            for n in (nodes.values() if isinstance(nodes, dict) else []) if isinstance(n, dict)]
    vals = [v for v in vals if isinstance(v, int)]
    return min(vals) if vals else None


def _store_bytes(names: list) -> int | None:
    """索引の大きさの合計（`_cat/indices` の store.size）。1 つでも取れなければ None。"""
    try:
        rows = _req("GET", f"/_cat/indices/{','.join(names)}?format=json&h=index,store.size&bytes=b&expand_wildcards=all")
    except Exception:
        return None
    sizes = {r.get("index"): r.get("store.size") for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}
    try:
        return sum(int(sizes[n]) for n in names)
    except (KeyError, TypeError, ValueError):
        return None


_LOW_FREE_WARN_BYTES = 5 * 1024 ** 3  # 作り直しの途中で ES の空きがこれを切ったら WARNING を 1 回だけ出す


def _free_space_note(world: str):
    """作り直しの途中のログに添える ES の空き（`substage.begin` の `note`）。空きが `_LOW_FREE_WARN_BYTES` を切ったら WARNING を 1 回だけ出す（止めない）。"""
    warned = False

    def note() -> str:
        nonlocal warned
        free = _es_free_bytes()
        if free is None:
            return ""
        if free < _LOW_FREE_WARN_BYTES and not warned:
            warned = True
            _log.warning("es_index: 作り直しの途中で ES の空きが 5GB を切りました（取り込みは続けます）: world=%s・空き %.1f GB",
                         world, free / 1024 ** 3)
        return f"ES の空き {free / 1024 ** 3:.1f} GB"

    return note


def _drop_old_for_space(world: str) -> bool:
    """作り直しの前に、ES の空きが古い索引の大きさの `_REBUILD_FREE_FACTOR` 倍に満たなければ古い索引を先に消す。消したら True。
    空き・大きさを取れないときは消さない（今まで通り新旧を持って付け替える）。
    """
    names = _concrete_indices(world)
    if not names:
        return False
    size = _store_bytes(names)
    free = _es_free_bytes()
    if size is None or free is None or free >= size * _REBUILD_FREE_FACTOR:
        return False
    _log.warning("es_index: ES の空きが足りないため、古い索引を消してから作り直します（終わるまでこの資料フォルダは検索できません）"
                 ": world=%s・空き %d バイト・古い索引 %d バイト", world, free, size)
    dropped = False
    for n in names:
        try:
            _req("DELETE", "/" + n)
            dropped = True
        except urllib.error.HTTPError as e:
            dropped = dropped or e.code == 404
            if e.code != 404:
                _log.warning("es_index: 古い索引を消せませんでした: %s", n)
        except Exception:
            _log.warning("es_index: 古い索引を消せませんでした: %s", n)
    return dropped


def _confirm_content_sig(world: str, content_sig, idx: str | None = None) -> None:
    """bulk が全バッチ成功した後（エイリアスの付け替えの前）に、新しい物理索引 `idx` の `_meta.content_sig`・`_meta.doc_count` を 1 回の read-modify-write で書く（Put Mapping API で `_meta` だけ更新）。
    `doc_count` は ES 側の実件数（`count(world)`）で、`needs_reindex` の実件数照合に使う。content_sig と同じ書き込みにまとめ、件数取得か書き込みが失敗すれば content_sig も書かない（次回 sync が fail-closed で 1 回張り直す）。
    """
    if not content_sig:
        return
    n = count(world) if idx is None else count(world, idx)
    if n is None:
        _log.warning("es_index: 実件数の取得に失敗しました（次回 sync が1回だけ張り直す）: world=%s", world)
        return
    existing = _index_meta(world) if idx is None else _index_meta(world, idx)
    if existing is None:  # GET 失敗＝既存 meta を消して PUT しない（次回 sync が再試行）
        _log.warning("es_index: content_sig 確定前の meta 取得に失敗しました（次回 sync が1回だけ張り直す）: world=%s", world)
        return
    try:
        meta = dict(existing)
        meta["content_sig"] = content_sig
        meta["doc_count"] = n
        _req("PUT", f"/{idx or _index(world)}/_mapping", {"_meta": meta})
    except Exception:
        _log.warning("es_index: content_sig/doc_count の確定に失敗しました（次回 sync が1回だけ張り直す）: world=%s", world)


def _restore_refresh_interval(world: str, idx: str | None = None) -> None:
    """`_mapping()` が索引作成時に立てた `refresh_interval:-1` を、bulk 全件成功後にクラスタ既定（"1s"）へ戻す（`null` を渡すと既定に復帰する）。
    失敗しても完全性には影響しない（最終バッチの `refresh=true` で全件可視化済み）。次回の `index_world()` が作り直す。
    """
    try:
        _req("PUT", f"/{idx or _index(world)}/_settings", {"index": {"refresh_interval": None}})
    except Exception:
        _log.warning("es_index: refresh_interval の復帰に失敗しました（次回 reindex で上書きされます）: world=%s", world)


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
        if idx in keep or _physical_base(idx) in keep:  # 世代付きの物理索引は、エイリアス名で所有者を判定する
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
    """新しい世代の物理索引を作る（作った名前は `_building`）。アナライザ不明（400）は standard で再試行する（作ったアナライザは `_created_analyzer`）。`dim`／`emeta` で kNN 用。"""
    name = _new_physical_name(world)
    idx = "/" + name
    _created_analyzer.pop(world, None)
    for analyzer in ("kuromoji", "standard"):
        try:
            _req("PUT", idx, _mapping(dim, analyzer, emeta))
            _created_analyzer[world] = analyzer
            _building[world] = name
            return True
        except urllib.error.HTTPError as e:
            try:
                txt = e.read().decode("utf-8", "replace")
            except Exception:
                txt = ""
            if "resource_already_exists" in txt:  # 世代名の衝突（通常起きない）
                return False
            if e.code == 400 and analyzer == "kuromoji":
                continue  # kuromoji 不明 → standard で再試行
            return False  # それ以外の 400／エラーは失敗
        except Exception:
            return False
    return False


# 埋め込みキャッシュのストア（SQLite の KV 1 ファイル）。資料フォルダ全体のベクトルを dict で持たず、フラッシュに含まれるキーだけを SELECT／INSERT する
# （メモリ有界・1 回のフラッシュの I/O がキー数に比例）。`embed_cache.i8.sqlite3`（表 `kv3`・ES へ渡すのと同じ 1 次元 1 バイトの BLOB）があればそれだけを使う。
# 無ければ 0.19.5 の `embed_cache.f32.sqlite3`（表 `kv2`・float32 の BLOB）、0.19.4 以前の `embed_cache.sqlite3`（表 `kv`・JSON）の順に、残っている方を
# 今まで通り読み書きし（読んだ値は `embeddings.quantize_vector` で縮めて返す）、どれも無ければ i8 を作る。写し替えは `embed_cache_migrate`。
_EMBED_CACHE_DB_NAME = "embed_cache.sqlite3"
_EMBED_CACHE_F32_NAME = "embed_cache.f32.sqlite3"
_EMBED_CACHE_I8_NAME = "embed_cache.i8.sqlite3"
_EMBED_CACHE_TABLES = {"i8": "kv3", "f32": "kv2", "json": "kv"}
_EMBED_CACHE_SQL_CHUNK = 500  # 1 回の IN 節に含める上限（SQLite の変数上限への余裕）


class _EmbedConn(sqlite3.Connection):
    """どの形式の DB を開いたか（`kind`＝i8／f32／json・表名）を持つ接続。"""
    table = "kv3"
    kind = "i8"


def _embed_cache_legacy_path(world: str) -> Path:
    return worlds.semantic_dir(world) / _EMBED_CACHE_DB_NAME


def _embed_cache_f32_path(world: str) -> Path:
    return worlds.semantic_dir(world) / _EMBED_CACHE_F32_NAME


def _embed_cache_i8_path(world: str) -> Path:
    return worlds.semantic_dir(world) / _EMBED_CACHE_I8_NAME


def _embed_cache_kind(p: Path) -> str:
    return {_EMBED_CACHE_I8_NAME: "i8", _EMBED_CACHE_F32_NAME: "f32"}.get(p.name, "json")


def _embed_cache_db_path(world: str) -> Path:
    """資料フォルダの埋め込みキャッシュ（SQLite・1 ファイル）の場所。i8 があればそれ、無ければ残っている f32・JSON の順、どれも無ければ i8。"""
    for p in (_embed_cache_i8_path(world), _embed_cache_f32_path(world), _embed_cache_legacy_path(world)):
        if p.exists():
            return p
    return _embed_cache_i8_path(world)


def _embed_cache_connect(world: str, *, create: bool) -> _EmbedConn | None:
    """埋め込みキャッシュ DB へ接続する。`create=False`（読み取り専用）で DB ファイルが無ければ接続せず None（空 DB を作らない）。WAL を有効化する。接続・初期化の失敗（権限等）は None（呼び出し元は miss／書込失敗として扱う）。"""
    p = _embed_cache_db_path(world)
    kind = _embed_cache_kind(p)
    if not create and not p.exists():
        return None
    for attempt in (0, 1):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(p), timeout=30, factory=_EmbedConn)
            conn.kind = kind
            conn.table = _EMBED_CACHE_TABLES[kind]
            try:
                if kind == "f32":
                    conn.execute("PRAGMA page_size=65536")  # 新しいファイルを作るときだけ効く（1 行が約 6KB のため大きなページにする）
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(f"CREATE TABLE IF NOT EXISTS {conn.table} "
                             f"(key TEXT PRIMARY KEY, vec {'TEXT' if kind == 'json' else 'BLOB'} NOT NULL)")
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
    """`keys` のうちキャッシュ済み・形状検証 OK（次元一致）のものだけ、1 次元 1 バイトに縮めた `array('b')` で返す（壊れ／型ズレは miss）。`_EMBED_CACHE_SQL_CHUNK` 件ずつ `SELECT ... WHERE key IN (...)` で引く。DB 接続・クエリの失敗も miss 扱い（呼び出し元は再 embed へ）。"""
    if not keys:
        return {}
    conn = _embed_cache_connect(world, create=False)
    if conn is None:
        return {}
    out: dict = {}
    try:
        for chunk in _sql_chunks(keys):
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(f"SELECT key, vec FROM {conn.table} WHERE key IN ({placeholders})", chunk).fetchall()
            for k, raw in rows:
                if conn.kind == "i8":
                    v = embeddings.quantized_from_bytes(raw, dim)  # 長さが次元でない BLOB は miss
                elif conn.kind == "f32":
                    v = embeddings.vector_from_bytes(raw, dim)  # 長さが次元×4 でない BLOB は miss
                    v = embeddings.quantize_vector(v) if v is not None else None
                else:
                    try:
                        v = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    v = embeddings.quantize_vector(v) if isinstance(v, list) and len(v) == dim else None
                if v is not None:
                    out[k] = v
    except sqlite3.Error:
        pass  # ここまでに読めた分は活かす
    finally:
        conn.close()
    return out


def _embed_cache_write_batch(world: str, new_vectors: dict, quantized: dict | None = None) -> None:
    """新規ベクトル（API から受けた float）を DB へ upsert する（1 トランザクション・呼び出しごとに commit）。i8 には `embeddings.quantize_vector` で縮めて書く（縮め済みの値は `quantized` で渡す）。フラッシュ済み分が次回キャッシュヒットすることで再開性を担保する（`_embed_cached` 参照）。
    書込失敗（ENOSPC・DB 接続不能を含む OSError）は呼び出し元へ伝播させる（握りつぶすと永続化されていないバッチを成功扱いにしてしまう）。
    """
    if not new_vectors:
        return
    for attempt in (0, 1):
        conn = _embed_cache_connect(world, create=True)
        if conn is None:
            raise OSError(f"embed cache DB へ接続できません（world={world}）")
        if conn.kind == "i8":
            qs = quantized or {}
            rows = [(k, q.tobytes()) for k, q in
                    ((k, qs.get(k) or embeddings.quantize_vector(v)) for k, v in new_vectors.items()) if q is not None]
        elif conn.kind == "f32":
            rows = [(k, embeddings.vector_to_bytes(v)) for k, v in new_vectors.items()]
        else:
            rows = [(k, json.dumps(embeddings.vector_to_json(v))) for k, v in new_vectors.items()]
        try:
            with conn:
                conn.executemany(f"INSERT OR REPLACE INTO {conn.table}(key, vec) VALUES (?, ?)", rows)
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
    for p in (_embed_cache_i8_path(world), _embed_cache_f32_path(world), _embed_cache_legacy_path(world)):
        for f in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
            try:
                f.unlink()
            except OSError:
                pass


def _live_keys_reset(world: str) -> None:
    """今回の索引作りで生きているチャンクの鍵を溜める表（キャッシュ DB 内の `live`）を空にする。鍵を Python のメモリに持たず、剪定を SQL の差で行うための表。"""
    conn = _embed_cache_connect(world, create=True)
    if conn is None:
        raise OSError(f"embed cache DB へ接続できません（world={world}）")
    try:
        with conn:
            conn.execute("DROP TABLE IF EXISTS live")
            conn.execute("CREATE TABLE live (key TEXT PRIMARY KEY) WITHOUT ROWID")
    except sqlite3.Error as exc:
        raise OSError(str(exc)) from exc
    finally:
        conn.close()


def _live_keys_add(world: str, keys) -> None:
    """生きている鍵を `live` 表へ足す（`_EMBED_FLUSH_CHUNKS` 件ずつ呼ぶ）。失敗は呼び出し元へ伝播させる。"""
    rows = [(k,) for k in keys]
    if not rows:
        return
    conn = _embed_cache_connect(world, create=True)
    if conn is None:
        raise OSError(f"embed cache DB へ接続できません（world={world}）")
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS live (key TEXT PRIMARY KEY) WITHOUT ROWID")
            conn.executemany("INSERT OR IGNORE INTO live(key) VALUES (?)", rows)
    except sqlite3.Error as exc:
        raise OSError(str(exc)) from exc
    finally:
        conn.close()


def _prune_embed_cache(world: str, valid_keys=None) -> None:
    """資料フォルダ全体の doc ストリームを一巡し終えた直後にだけ呼ぶ最終剪定。`live` 表に無い鍵を消す（SQL の差で行い、鍵の集合を Python に持たない）。`live` が空なら DB ごと削除する。
    `valid_keys` を渡すとそれを `live` 表に入れ直してから剪定する（呼び出し側が先に `_live_keys_add` で溜めたなら省略）。
    取れなかったチャンクの鍵はキャッシュに無いだけなので不足は異常にしない（後から `fill_missing_vectors` が足す）。
    `index_world()` から 1 回だけ呼ぶ（`_embed_cached()` は doc グループごとに複数回呼ばれるため、呼ぶたびに剪定すると前のグループ分を消す）。削除失敗は呼び出し元へ伝播させる。
    """
    if valid_keys is not None:
        _live_keys_reset(world)
        keys = list(valid_keys)
        for chunk in _sql_chunks(keys):
            _live_keys_add(world, chunk)
    if not _embed_cache_db_path(world).exists():
        return  # キャッシュ自体が無い＝剪定するものが無い
    conn = _embed_cache_connect(world, create=False)
    if conn is None:
        # DB が存在するのに接続できない（権限・ロック・破損）＝障害。OSError で打ち切らせる（黙って戻ると新しい索引の作成へ進んでしまう）
        raise OSError(f"embed cache DB へ接続できません（world={world}）")
    try:
        try:
            has_live = conn.execute("SELECT 1 FROM live LIMIT 1").fetchone() is not None
        except sqlite3.OperationalError:  # `live` 表が無い＝生きている鍵が 1 件も溜まっていない
            has_live = False
        if not has_live:
            conn.close()
            _delete_embed_cache(world)
            return
        with conn:
            conn.execute(f"DELETE FROM {conn.table} WHERE key NOT IN (SELECT key FROM live)")
            conn.execute("DROP TABLE live")
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


def _stop_point() -> None:
    """区切りで停止要求を確かめる（背景実行のスレッド以外では何もしない）。"""
    from .ingest import background
    background.raise_if_stop_requested()


_MIN_FREE_BYTES = 10 * 1024 ** 3  # 作り直しの途中で ES のデータ置き場か埋め込みキャッシュの置き場の空きがこれを切ったら、次の区切りで取り込みを止める


def _space_point(world: str) -> None:
    """作り直しの区切りで、ES のデータ置き場と埋め込みキャッシュの置き場の空きを確かめる（背景実行のスレッド以外では何もしない）。
    どちらかが `_MIN_FREE_BYTES` を切っていれば `background.LowDiskStop`（停止要求と同じ止め方）。空きを取れないときは止めない。
    """
    from .ingest import background
    if not (background.in_run() or background.in_followup()):
        return
    frees = [_es_free_bytes()]
    try:
        d = worlds.semantic_dir(world)
        while not d.exists() and d != d.parent:
            d = d.parent
        frees.append(shutil.disk_usage(d).free)
    except OSError:
        pass
    low = min((f for f in frees if isinstance(f, int)), default=None)
    if low is not None and low < _MIN_FREE_BYTES:
        msg = background.low_disk_message(low)
        substage.begin("low_disk", msg, logger=_embed_log)
        _log.warning("es_index: %s: world=%s", msg, world)
        raise background.LowDiskStop(low)


_EMBED_GIVE_UP_AFTER = 3   # 埋め込みのまとまりがこの回数続けて失敗したら、その処理の残りでは送り先へ問い合わせない
_EMBED_COOLDOWN_SEC = 600  # 失敗した直後の `fill_missing_vectors` は、送り先を叩き直さない時間
_embed_failed_at: dict[str, float] = {}  # 資料フォルダ ID → 直近に埋め込みのまとまりが失敗した時刻（time.monotonic）


class _EmbedGuard:
    """1 回の処理（索引作り・足し直し）の中で、埋め込みの送り先が使えないときに問い合わせを打ち切る。"""

    def __init__(self, world: str):
        self.world = world
        self.consecutive = 0
        self.failed_groups = 0

    @property
    def gave_up(self) -> bool:
        return self.consecutive >= _EMBED_GIVE_UP_AFTER

    def ok(self) -> None:
        self.consecutive = 0

    def failed(self) -> None:
        self.consecutive += 1
        self.failed_groups += 1
        _embed_failed_at[self.world] = time.monotonic()


def _embed_fill(world: str, texts: list, ec, guard: _EmbedGuard | None = None, *, stop_points: bool = False) -> tuple:
    """チャンク埋め込みを内容ハッシュでキャッシュし、未変更チャンクの再 embed（API コスト）を省く。取れなかったチャンクだけ `None` にして続ける。
    返値 `(vectors, reused, embedded)`。`vectors` は `texts` と同じ長さで、取れなかった位置が `None`（再送を使い切ったまとまり・キャッシュ書込の失敗・`guard` の打ち切り後）。重複チャンクは 1 回だけ embed し、キャッシュ済みベクトルも形状を検証する（壊れは miss）。
    `texts` は呼び出し元が有界なバッチに分けて渡す。本関数はこのバッチのキーを追加／更新するだけで、剪定は呼び出し元が 1 回だけ `_prune_embed_cache()` で行う。`ec` が None・`texts` が空なら全部 `None`。
    キャッシュ本体は SQLite KV（`_embed_cache_lookup_batch`／`_embed_cache_write_batch`）。不足分は `_EMBED_FLUSH_CHUNKS` 件単位のまとまりに割り、成功のたびに DB へ即時フラッシュする（途中で止まっても次回キャッシュヒットで続きから取る）。
    `stop_points` が真なら、まとまりごとの前に停止要求を確かめる。
    """
    if not ec or not texts:
        return [None] * len(texts), 0, 0
    dim = ec["dim"]
    keys = [_chunk_key(ec, t) for t in texts]
    key_text = {}  # distinct チャンク（重複本文は 1 回だけ embed）
    for k, t in zip(keys, texts):
        key_text.setdefault(k, t)
    filled = _embed_cache_lookup_batch(world, list(key_text), dim)  # 形状検証済みの既存ベクトルだけ再利用
    need = [k for k in key_text if k not in filled]
    embedded = 0
    if need:
        total = len(need)
        n_batches = (total + _EMBED_FLUSH_CHUNKS - 1) // _EMBED_FLUSH_CHUNKS
        for bi, start in enumerate(range(0, total, _EMBED_FLUSH_CHUNKS)):
            if stop_points:
                _stop_point()
            if guard is not None and guard.gave_up:
                break
            batch_keys = need[start:start + _EMBED_FLUSH_CHUNKS]
            new = embeddings.embed([key_text[k] for k in batch_keys], ec, world=world)
            new_map = {k: embeddings.to_vector(vec) for k, vec in zip(batch_keys, new)} if new else {}
            q_map = {k: q for k, q in ((k, embeddings.quantize_vector(v)) for k, v in new_map.items()) if q is not None}
            new_map = {k: new_map[k] for k in q_map}
            if new_map:
                try:
                    _embed_cache_write_batch(world, new_map, q_map)  # 成功したバッチだけ即座に永続化
                except OSError:
                    # DB 書込障害（ENOSPC 等）は成功扱いにしない（このまとまりは取れなかった扱い）
                    _embed_log.warning("es_index: embed キャッシュDBの書込に失敗（world=%s・ENOSPC等）"
                                       "——このまとまりは取れなかった扱いにする", world)
                    new_map = {}
            if not new_map:
                if guard is not None:
                    guard.failed()
                continue
            filled.update(q_map)
            embedded += len(new_map)
            if guard is not None:
                guard.ok()
            if bi == 0 or bi == n_batches - 1 or (bi + 1) % 10 == 0:  # 間引いて進捗を残す
                _embed_log.info("es_index: embed 進捗 %d/%d チャンク（world=%s）",
                                 min(start + len(batch_keys), total), total, world)
    return [filled.get(k) for k in keys], len(key_text) - len(need), embedded


def _embed_cached(world: str, texts: list, ec) -> tuple:
    """`_embed_fill` の全部取れた場合だけ版。1 件でも取れなければ `(None, 0, 0)`（`replace_document` 用＝取れなかった文書は書かずにやり直しへ回す）。"""
    vecs, reused, embedded = _embed_fill(world, texts, ec)
    if not ec or not texts or any(v is None for v in vecs):
        return None, 0, 0
    return vecs, reused, embedded


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


# 構造の欄の上限（1 チャンクの欄を有界にする）。識別子の上限は生成側（`IDENTIFIER_MAX_MENTIONS_PER_CHUNK`）が持つ
_LABELS_MAX_PER_CHUNK = 32
_LABEL_MAX_CHARS = 128
_REGION_CONTEXT_MAX_CHARS = 512
_JA_CHAR_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_BODY_FIELD_RE = re.compile(r"^([^:：\n]{1,64}): (.+)$")


def _str_list(value) -> list:
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def _chunk_identifier_values(chunk: dict) -> tuple:
    """rag チャンクの識別子を `(identifier_values, record_identity_values)` で返す（`identifier_mentions` の値と、役割 `record_identity` の値。重複は除く）。"""
    mentions = [m for m in chunk.get("identifier_mentions") or []
                if isinstance(m, dict) and isinstance(m.get("value"), str) and m["value"]]
    identity = _str_list(chunk.get("record_identity_values_exact"))
    identity += [m["value"] for m in mentions if m.get("role") == "record_identity"]
    identity = list(dict.fromkeys(identity))
    return list(dict.fromkeys([m["value"] for m in mentions] + identity)), identity


def _chunk_labels(chunk: dict, identifiers: list) -> list:
    """レコード（`record_keys` を持つチャンク）の本文 `項目名: 値` の行から、識別子でない日本語の値を集める（項目名・状態など）。"""
    if not chunk.get("record_keys") or not isinstance(chunk.get("body"), str):
        return []
    skip = set(identifiers)
    out: list = []
    for line in chunk["body"].split("\n"):
        m = _BODY_FIELD_RE.match(line)
        if m is None or "のExcel" in m.group(1):
            continue
        value = m.group(2).strip()
        if value and len(value) <= _LABEL_MAX_CHARS and value not in skip and _JA_CHAR_RE.search(value):
            out.append(value)
    return list(dict.fromkeys(out))[:_LABELS_MAX_PER_CHUNK]


def _chunk_region_text(chunk: dict) -> str:
    """所属領域の見出しとシート名（`region_context` の title／sheet）を 1 本の文字列にする。無ければ空。"""
    rc = chunk.get("region_context")
    if not isinstance(rc, dict):
        return ""
    parts = [rc.get(k) for k in ("title", "sheet") if isinstance(rc.get(k), str) and rc.get(k)]
    parts += _str_list(rc.get("section_path"))
    return " ".join(dict.fromkeys(parts))[:_REGION_CONTEXT_MAX_CHARS]


def _chunk_structure_meta(chunk: dict) -> dict:
    """rag チャンクの構造の欄（`identifier_values`／`record_identity_values`／`labels`／`region_context`）。値が無い欄はキーを立てない。"""
    identifiers, identity = _chunk_identifier_values(chunk)
    out: dict = {}
    if identifiers:
        out["identifier_values"] = identifiers
    if identity:
        out["record_identity_values"] = identity
    labels = _chunk_labels(chunk, identifiers)
    if labels:
        out["labels"] = labels
    region = _chunk_region_text(chunk)
    if region:
        out["region_context"] = region
    return out


def _load_rag_md_anchors(md_path: Path | None) -> tuple:
    """`{rel}.rag.md`（RAG 正本）を読み、アンカー辞書 `(anchors, reason)` を返す。`reason` が付けば `anchors` は None。"""
    if md_path is None:
        return None, "rag_md_missing"
    try:
        if md_path.stat().st_size > _RAG_MD_FILE_CAP_BYTES:
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


class RagChunkLineTooLarge(Exception):
    """rag_chunks.jsonl／rag.md の 1 行が `_RAG_CHUNKS_LINE_CAP_BYTES` を超えた。"""


def iter_capped_lines(f):
    """開いたテキストファイルを 1 行ずつ yield する。1 行が `_RAG_CHUNKS_LINE_CAP_BYTES` を超えたら `RagChunkLineTooLarge`（上限＋1 文字までしか読まない）。"""
    cap = _RAG_CHUNKS_LINE_CAP_BYTES
    while True:
        line = f.readline(cap + 1)
        if not line:
            return
        if len(line) > cap and not line.endswith("\n"):
            raise RagChunkLineTooLarge()
        yield line


def _rag_chunks_validate(rag_path: Path, anchors: dict, rel: str) -> tuple:
    """`{rel}.rag_chunks.jsonl` を検証のみ行う（本文／メタは保持せず、`chunk_id` の集合だけを持つ。1 文書のチャンク数に比例したメモリを使わない）。
    有効なら `(seen_chunk_ids, None)`（0 件なら空集合）、無効なら `(None, reason)`。`reason` の語彙は `_validate_rag_chunks` 参照（rag.md 側の `rag_md_*` は `_load_rag_md_anchors` が担当）。
    """
    try:
        rag_path.stat()
    except OSError:
        return None, "stat_failed"
    seen_chunk_ids: set = set()
    try:
        with json_io.open_text_maybe_gzip(rag_path) as f:
            for lineno, raw_line in enumerate(iter_capped_lines(f), start=1):
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
    except RagChunkLineTooLarge:
        return None, "line_too_large"
    except UnicodeDecodeError:
        return None, "invalid_utf8"
    except (OSError, *json_io.GZIP_READ_ERRORS):
        return None, "read_failed"
    if set(anchors) - seen_chunk_ids:
        return None, "rag_md_anchor_surplus"
    return seen_chunk_ids, None


# 表の行のまとまり（ベクトルを 1 本だけ付ける単位）。設計: docs/design/rag.md「表の行・本文のまとまり」
_ROW_GROUP_KINDS = frozenset({"table_record", "coordinate_record"})
_ROW_GROUP_MAX_ROWS = 20
_ROW_GROUP_CELL_MAX_CHARS = 32      # 1 つの値の上限（超えた分は切る）
_ROW_GROUP_ROW_MAX_BYTES = 300      # 値を並べた 1 行の上限（UTF-8）
_ROW_GROUP_HEAD_MAX_BYTES = 600     # 出所・列名の行それぞれの上限
_ROW_GROUP_MAX_BYTES = 7600         # まとまり 1 本の上限（埋め込みの 1 窓＝約 8000 バイトに収める）
_ROW_GROUP_SEP = " | "
# 本文のまとまり（表の行でない連続するチャンクにベクトルを 1 本）。設計: docs/design/rag.md「表の行・本文のまとまり」
_BODY_GROUP_MAX_CHARS = 1000        # まとまり 1 本の上限（文字）。UTF-8 で 4000 バイト以下＝埋め込みの 1 窓に収める
_BODY_GROUP_TEXT_KINDS = frozenset({"paragraph", "heading"})   # 見出しと段落は同じ種類として束ねる
_BODY_GROUP_TAIL_DROP = ("出所:", "出所：", "文書:", "見出し:", "## ", "### ")
_A1_RE = re.compile(r"^\$?([A-Z]{1,3})\$?(\d{1,7})$")


def _a1_cells(cell_range) -> list:
    """`"A2"`／`"A2:C5"` を `[(列番号, 行番号), ...]` にする。読めなければ空。"""
    if not isinstance(cell_range, str):
        return []
    out = []
    for part in cell_range.split(":"):
        m = _A1_RE.match(part.strip().upper())
        if m is None:
            return []
        col = 0
        for ch in m.group(1):
            col = col * 26 + (ord(ch) - 64)
        out.append((col, int(m.group(2))))
    return out


def _a1_name(col: int, row: int) -> str:
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def _row_group_locator(rows: list) -> dict | None:
    """まとまりの行の citation のセル範囲の外枠（シート名つき）。セル範囲で表せない行があれば先頭の行の locator。"""
    cells = []
    for row in rows:
        for citation in row.get("citations") or []:
            locator = citation.get("locator") if isinstance(citation, dict) else None
            found = _a1_cells(locator.get("cell_range")) if isinstance(locator, dict) else []
            if not found:
                return _chunk_locator(rows[0])
            cells.extend(found)
    if not cells:
        return _chunk_locator(rows[0])
    cols = [c for c, _ in cells]
    nums = [r for _, r in cells]
    out = {**(_chunk_locator(rows[0]) or {}),
           "cell_range": f"{_a1_name(min(cols), min(nums))}:{_a1_name(max(cols), max(nums))}"}
    sheet = (rows[0].get("region_context") or {}).get("sheet") if isinstance(rows[0].get("region_context"), dict) else None
    if isinstance(sheet, str) and sheet:
        out["sheet"] = sheet
    return out


_CUT_MARK = "…"


def _cut_bytes(text: str, limit: int) -> str:
    """UTF-8 で `limit` バイトに収まるよう末尾を切る（文字の途中では切らない）。切ったときは末尾に「…」を付ける。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit - len(_CUT_MARK.encode("utf-8"))].decode("utf-8", "ignore") + _CUT_MARK


def _cut_value(v: str) -> str:
    """束の中のセルの値を上限の文字数で切る。切ったときは末尾に「…」を付ける。"""
    return v if len(v) <= _ROW_GROUP_CELL_MAX_CHARS else v[:_ROW_GROUP_CELL_MAX_CHARS - 1] + _CUT_MARK


def _row_fields(row: dict, text: str) -> list:
    """行の本文（`列名: 値` の並び）を `[(列名, 値), ...]` にする。Excel の付記の行は除き、続きの行は前の値へつなぐ。
    `列名: 値` の行が無ければ rag.md の本文を値 1 つとして返す（列名は空）。
    """
    body = row.get("body")
    fields: list = []
    for line in (body if isinstance(body, str) else "").split("\n"):
        m = _BODY_FIELD_RE.match(line)
        if m is not None:
            fields.append(None if "のExcel" in m.group(1) else [m.group(1).strip(), m.group(2)])
        elif fields and fields[-1] is not None and line.strip():
            fields[-1][1] += " " + line
    fields = [(k, " ".join(v.split())) for k, v in (f for f in fields if f is not None)]
    return fields or [("", " ".join(text.split()))]


def _row_group_columns(fields: list) -> list:
    """列の並び（同じ列名が何度も出る表は何番目かで区別する）。"""
    seen: dict = {}
    out = []
    for name, _v in fields:
        seen[name] = seen.get(name, 0) + 1
        out.append((name, seen[name]))
    return out


_COORD_DEFAULT_LABEL = "記載値"   # 項目のラベルが無いセルに取り込みが付ける名前


def _row_position(row: dict) -> str:
    """見出しの無い配置の行（`coordinate_record`）の位置。セル範囲、無ければページと表の中の行・列。"""
    citations = row.get("citations") or []
    loc = citations[0].get("locator") if citations and isinstance(citations[0], dict) else None
    if not isinstance(loc, dict):
        return ""
    if isinstance(loc.get("cell_range"), str) and loc["cell_range"]:
        return loc["cell_range"]
    grid = loc.get("grid") if isinstance(loc.get("grid"), dict) else {}
    parts = [f"p{loc['page']}" if isinstance(loc.get("page"), int) else "",
             f"r{grid['row']}c{grid.get('column')}" if isinstance(grid.get("row"), int) else ""]
    return " ".join(p for p in parts if p)


def _row_group_line(row: dict, fields: list, columns: list | None) -> str:
    """1 行を束の 1 行にする。値・行の長さは上限で切る。
    表の行は値だけを見出しの列の順に区切り文字で並べる（見出しに無い列の値は後ろに足す）。
    見出しの無い配置の行（`columns is None`）は、列名の代わりにセルの位置と項目のラベルを値に添える。
    """
    if columns is None:
        values = [(f"{k}: " if k and k != _COORD_DEFAULT_LABEL else "") + _cut_value(v) for k, v in fields]
        line = " ".join(x for x in (_row_position(row), _ROW_GROUP_SEP.join(values)) if x)
        return _cut_bytes(line, _ROW_GROUP_ROW_MAX_BYTES)
    by_col = dict(zip(_row_group_columns(fields), (v for _k, v in fields)))
    values = [by_col.pop(c, "") for c in columns] + list(by_col.values())
    while values and not values[-1]:
        values.pop()
    line = _ROW_GROUP_SEP.join(_cut_value(v) for v in values)
    return _cut_bytes(line, _ROW_GROUP_ROW_MAX_BYTES)


def _row_group_head_lines(row: dict, text: str, fields: list) -> tuple:
    """まとまりの先頭の行（出所・列名）と列の並び。出所は rag.md の本文の `出所:` の行、無ければ領域の見出しとシート名。
    見出しの無い配置の行（`coordinate_record`）は列名の行を作らず、列の並びは None（各行が位置とラベルを持つ）。
    """
    first = text.split("\n", 1)[0].strip()
    source = first if first.startswith("出所") else _chunk_region_text(row)
    if row.get("content_type") == "coordinate_record":
        columns = None
        header = ""
    else:
        columns = _row_group_columns(fields)
        header = "列: " + _ROW_GROUP_SEP.join(name for name, _n in columns) if any(name for name, _n in columns) else ""
    lines = [_cut_bytes(x, _ROW_GROUP_HEAD_MAX_BYTES) for x in (source, header) if x]
    return lines, columns


def _body_group_kind(kind) -> str | None:
    """本文のまとまりで同じ種類として束ねる区分。表の行・画像は束ねない（None）。"""
    if kind in _ROW_GROUP_KINDS or kind in ocr_router.RASTER_ELEMENT_TYPES:
        return None
    return "text" if kind in _BODY_GROUP_TEXT_KINDS else kind


def _body_group_line(text: str) -> str:
    """本文のまとまりの 2 件目以降の文字。先頭と重なる出所・見出しの行を除く。"""
    return "\n".join(ln for ln in text.split("\n") if ln.strip() and not ln.startswith(_BODY_GROUP_TAIL_DROP))


def _row_group_entries(group: list, head_lines: list):
    """連続する表の行・本文のチャンク `[(es_id, body, text, row, line), ...]` を `(es_id, body, text, skip_embed)` で返す。
    先頭だけがまとまりの文字（`head_lines`＋各件の `line`）でベクトルを取り、`vector_text`・`row_group` を持つ。
    表の行は出所・列名の行＋各行の値だけの行、本文は先頭の本文＋2 件目以降の本文（`_body_group_line`）。
    2 件目以降はベクトルを取らない。1 件だけのまとまりは rag.md の本文のまま埋め込む。
    """
    head_id, head_body, head_text, _head_row, _line = group[0]
    if len(group) == 1:
        yield head_id, head_body, head_text, False
        return
    vector_text = "\n".join(head_lines + [line for *_x, line in group])
    row_group = {"chunk_ids": [b["chunk_id"] for _i, b, _t, _r, _l in group], "rows": len(group)}
    locator = _row_group_locator([row for _i, _b, _t, row, _l in group])
    if locator is not None:
        row_group["locator"] = locator
    yield head_id, {**head_body, "vector_text": vector_text, "row_group": row_group}, vector_text, False
    for es_id, body, text, _row, _l in group[1:]:
        yield es_id, body, text, True


def _iter_rag_chunk_entries(rag_path: Path, rel: str, anchors: dict, base_meta: dict):
    """事前に `_rag_chunks_validate()` が `reason=None` を返した `rag_path` をもう一度走査し、`(id, body, text, skip_embed)` を 1 件ずつ yield する。検証済みの `rag_path`／`anchors` でのみ呼ぶ。
    `text` は埋め込みの文字。表の行は連続する行を、表の行でないチャンクは同じ領域・同じ節・同じ種類の連続するチャンクを
    `_BODY_GROUP_MAX_CHARS` 字までまとまりに束ね、先頭だけがまとまりの文字を持ち、残りは `skip_embed`（`_row_group_entries`）。持つメモリはまとまり 1 つ分まで。
    """
    group: list = []
    head_lines: list = []
    columns: list = []
    group_bytes = 0
    body_group: list = []
    body_chars = 0
    with json_io.open_text_maybe_gzip(rag_path) as f:
        for raw_line in iter_capped_lines(f):
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
            body.update(_chunk_structure_meta(row))  # 構造の欄（無ければ何も足さない）
            es_id = _rag_chunk_es_id(rel, cid)
            kind = row.get("content_type")
            if kind not in _ROW_GROUP_KINDS:
                yield from _row_group_entries(group, head_lines) if group else ()
                group = []
                body_kind = _body_group_kind(kind)
                section = tuple(row.get("section_path") or ())
                if body_group:
                    head = body_group[0][3]
                    body_line = _body_group_line(text)
                    if (body_kind != _body_group_kind(head.get("content_type"))
                            or row.get("parent_id") != head.get("parent_id")
                            or section != tuple(head.get("section_path") or ())
                            or body_chars + len(body_line) + 1 > _BODY_GROUP_MAX_CHARS):
                        yield from _row_group_entries(body_group, [])
                        body_group = []
                if body_kind is None:
                    yield es_id, body, text, False
                    continue
                if not body_group:
                    body_line = text
                    body_chars = len(text)
                else:
                    body_chars += len(body_line) + 1
                body_group.append((es_id, body, text, row, body_line))
                continue
            yield from _row_group_entries(body_group, []) if body_group else ()
            body_group = []
            fields = _row_fields(row, text)
            if group:
                head = group[0][3]
                row_line = _row_group_line(row, fields, columns)
                if (row.get("parent_id") != head.get("parent_id") or kind != head.get("content_type")
                        or len(group) >= _ROW_GROUP_MAX_ROWS
                        or group_bytes + len(row_line.encode("utf-8")) + 1 > _ROW_GROUP_MAX_BYTES):
                    yield from _row_group_entries(group, head_lines)
                    group = []
            if not group:
                head_lines, columns = _row_group_head_lines(row, text, fields)
                row_line = _row_group_line(row, fields, columns)
                group_bytes = len("\n".join(head_lines).encode("utf-8"))
            group.append((es_id, body, text, row, row_line))
            group_bytes += len(row_line.encode("utf-8")) + 1
    if group:
        yield from _row_group_entries(group, head_lines)
    if body_group:
        yield from _row_group_entries(body_group, [])


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
    - `line_too_large`／`too_many_rows`／`search_text_too_long`: `_RAG_CHUNKS_LINE_CAP_BYTES`（1 行）／`_RAG_CHUNKS_MAX_ROWS`／`_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS` 超過。
    - `rag_md_too_large`（`_RAG_MD_FILE_CAP_BYTES` 超過）／`rag_md_stat_failed`／`rag_md_invalid_utf8`／`rag_md_read_failed`: rag.md 側の読み取り失敗。
    jsonl は 1 行ずつ（1 行の上限付き）読み、rag.md は上限付きで一括読みする。索引本文・埋め込み対象は rag.md をアンカー分割した本文で、`line` は立てない。
    """
    anchors, reason = _load_rag_md_anchors(md_path)
    if reason is not None:
        return [], [], [], reason
    seen, reason = _rag_chunks_validate(rag_path, anchors, rel)
    if reason is not None:
        return [], [], [], reason
    ids, bodies, texts = [], [], []
    for cid_key, body, text, _skip in _iter_rag_chunk_entries(rag_path, rel, anchors, base_meta):
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
        v = vec_by_idx.get(i)  # テキスト資料（軽量テキスト枠の第 2 段）など埋め込み対象外のチャンクは None
        if v is not None:
            q = embeddings.vector_to_es_bytes(v)
            body = {**body, "embedding": q} if q is not None else {**body, "vector_status": "missing"}
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


def _is_source_doc(d: dict) -> bool:
    """ES に入れない文書か。登録コード（`branch=="source"`）と汎用コード（`ingest.text_kind` の第 1 段）。第 2 段（csv・log などのテキスト資料）は入れる。"""
    return d.get("branch") == "source" or d.get("doctype") == text_kind.CODE_DOCTYPE_LABEL


def _iter_doc_chunk_records(world: str, d: dict, derived: Path | None, rag_exts: frozenset,
                            res_map: dict | None = None) -> tuple:
    """1 文書分のチャンクを `(id, body, text, no_embed)` を 1 件ずつ返すジェネレータへ組み立てる（巨大な 1 文書でも 1 件ごとに flush 判定できるようにする）。
    rag_chunks 経路は yield を始める前に `_rag_chunks_validate()` で jsonl 全体を検証し終える。失敗なら 1 件も yield せず legacy 40 行チャンクへ縮退する（doc 単位の原子性）。検証成功後にファイルを開き直して `_iter_rag_chunk_entries` で yield する。
    `res_map`（省略可）: `importance.resolve_for_world()` の結果。あれば `importance`／`importance_reason` を全チャンクの body へ条件付きで焼き込む。
    返値 `(chunk_iter, degraded_entry)`。
    - `chunk_iter`＝None: この文書はスキップ（unreadable／ソース（登録コードと汎用コード）＝ES に入れない／本文の読み取り失敗＝reason="text_read_failed"／本文が空白のみ＝reason="empty_text"）。
    - `chunk_iter`＝`(id, body, text, no_embed)` を yield するジェネレータ（0 件もありうる）。
    - `degraded_entry`＝None または {"doc": rel, "reason": ...}（rag_chunks はあるが使えなかった場合のみ。yield を始める前に確定する）。
    """
    if d.get("state") == "unreadable":  # 分類を唯一のゲートにする（再読が成功しても索引しない）
        return None, None
    rel = d["name"]
    ext = Path(rel).suffix.lower()
    if _is_source_doc(d):
        # ソース原文は ES に入れない（同じ識別子が何十回も出てキーワードの順位を占め、設計書を押し出すため）。探すのは ripgrep_search
        return None, None
    # テキスト資料（軽量テキスト枠の第 2 段）は「ベクトル・グラフ・LLM を一切通さない」ため、BM25 だけで索引する（`no_embed`）
    no_embed = d.get("doctype") == text_kind.DOCUMENT_DOCTYPE_LABEL
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
                        for cid_key, body, text, skip in _iter_rag_chunk_entries(rag_path, rel, anchors, meta):
                            yield cid_key, body, text, no_embed or skip
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


_senders: dict[str, _StreamingBulkSender] = {}  # 資料フォルダ ID → 送信中の送り手（`index_world` が終わるときに飛行中の bulk を待つ）


class _StreamingBulkSender:
    """`index_world()` Pass2 が doc グループ単位で積むチャンクを、bulk 送信の境界（件数／バイト数の閾値）に達し次第 ES へ流す。
    `_ES_BULK_PARALLEL` 本までを同時に流す。`refresh=true` は最後の送信だけに付ける。最後かどうかは全 doc を処理し終えるまで分からないため、直前に確定した 1 バッチを `_pending` として 1 つだけ持ち越し（lag-by-one）、`finish()` で飛行中の bulk が全部終わるのを待ってから、最後の `_pending` を refresh 付きで送る。
    設計: docs/design/rag.md「ES 索引の構成」
    """

    def __init__(self, world: str, idx: str | None = None):
        self.world = world
        self.idx = idx or _index(world)  # 送り先＝作成中の物理索引（付け替え前なので検索は古い索引のまま）
        self._pending: str | None = None
        self._pool = ThreadPoolExecutor(max_workers=_ES_BULK_PARALLEL, thread_name_prefix="es-bulk")
        self._inflight: set = set()
        self._lock = threading.Lock()
        self.failed = False
        self.error: str | None = None
        self.missing = 0  # ベクトル無し（`vector_status: missing`）で入れたチャンクの数
        _senders[world] = self

    def _send(self, payload: str, refresh: bool) -> bool:
        path = f"/{self.idx}/_bulk" + ("?refresh=true" if refresh else "")
        try:
            # 同時に送る本数だけ ES の中で待たされるので、1 本の待ちの上限もその分延ばす
            res = _req("POST", path, payload, ndjson=True, timeout=_TIMEOUT * _ES_BULK_PARALLEL)
        except Exception as e:
            # 途中バッチの失敗は呼び出し元が作りかけの索引を捨てる（全部か無しか。付け替え前なので古い索引は残る）
            # 失敗後は `failed` で以降の送信を止めるので、原因の行は 1 つの送り手につき 1 回だけ出る
            with self._lock:
                first, self.failed, self.error = not self.failed, True, "bulk_failed"
            if first:
                _log.warning("ES への登録に失敗しました（作りかけの索引は捨てます）: world=%s 原因=%s: %s",
                             self.world, e.__class__.__name__, " ".join(str(e).split())[:200])
            return False
        if res.get("errors"):  # item-level の失敗（HTTP 200 でも起きる）
            self.failed, self.error = True, "bulk_errors"
            return False
        return True

    def _submit(self, payload: str) -> bool:
        """refresh 無しの 1 バッチを送る。飛行中が上限なら 1 本終わるまで待つ。"""
        while len(self._inflight) >= _ES_BULK_PARALLEL:
            _, self._inflight = wait(self._inflight, return_when=FIRST_COMPLETED)
        if self.failed:
            return False
        self._inflight.add(self._pool.submit(self._send, payload, False))
        return True

    def drain(self) -> bool:
        """飛行中の bulk が全部終わるのを待つ。ここまでに失敗が無ければ True。"""
        wait(self._inflight)
        self._inflight = set()
        return not self.failed

    def close(self) -> None:
        """飛行中の bulk を待ち、送り手を片づける。作りかけの索引を消す前に必ず呼ぶ。"""
        self.drain()
        self._pool.shutdown(wait=True)
        if _senders.get(self.world) is self:
            del _senders[self.world]

    def send_group(self, ids: list, bodies: list, vec_by_idx: dict) -> bool:
        """1 グループ分（`_EMBED_FLUSH_CHUNKS` 件程度に有界）を bulk 用サブバッチへ分割し、持ち越し済みの前グループ分（refresh なし）→ このグループの内部境界（最後の 1 つ以外）の順で送る。このグループの最後のサブバッチは新たな `_pending` として持ち越す。"""
        if self.failed or not ids:
            return not self.failed
        batches = _bulk_batches(ids, bodies, vec_by_idx)
        if not batches:
            return True
        if self._pending is not None:
            if not self._submit(self._pending):
                return False
            self._pending = None
        for payload in batches[:-1]:
            if not self._submit(payload):
                return False
        self._pending = batches[-1]
        return not self.failed

    def finish(self) -> bool:
        """飛行中の bulk を待ち、最後に残った持ち越し分を refresh 付きで送る（何も送っていなければ no-op）。"""
        if not self.drain():
            return False
        if self._pending is not None:
            ok = self._send(self._pending, refresh=True)
            self._pending = None
            return ok
        return True


def _close_sender(world: str) -> None:
    sender = _senders.get(world)
    if sender is not None:
        sender.close()


def _flush_doc_group(sender: _StreamingBulkSender, world: str, ids: list, bodies: list,
                     texts: list, no_embed: list, ec, embed_feature_applies: bool) -> bool:
    """Pass2 の 1 グループ（doc 数件・チャンク `_EMBED_FLUSH_CHUNKS` 件程度に有界）を、必要なら埋め込みキャッシュから embedding を引いて bulk 送信する（`sender.send_group` へ委譲）。
    キャッシュ参照は `embed_feature_applies` が真の時だけ。キャッシュに無い（Pass1 で取れなかった）埋め込み対象チャンクは、ベクトル無し・`vector_status: "missing"` で入れる（`fill_missing_vectors` が後から足す）。
    送る前に停止要求とディスクの空きを確かめる（止まったら呼び出し元の `index_world` が作りかけの索引を捨てる）。
    """
    _stop_point()
    _space_point(world)
    vec_by_idx: dict = {}
    if embed_feature_applies:
        embed_positions = [i for i, skip in enumerate(no_embed) if not skip]
        if embed_positions:
            keys = [_chunk_key(ec, texts[i]) for i in embed_positions]
            hit = _embed_cache_lookup_batch(world, keys, ec["dim"])
            for i, k in zip(embed_positions, keys):
                v = hit.get(k)
                if v is None:
                    bodies[i] = {**bodies[i], "vector_status": "missing"}
                    sender.missing += 1
                else:
                    vec_by_idx[i] = v
    return sender.send_group(ids, bodies, vec_by_idx)


def _compact_building(world: str, idx: str) -> None:
    """作りかけの物理索引を詰める（ベクトルの写しを捨てたセグメントへ畳み直す）。失敗・時間切れは WARNING 1 行で先へ進む（取り込みは失敗にしない）。
    停止要求は待ちの間も確かめる（`StopRequested` は呼び出し元へ通す）。
    """
    # ① flush ② ES が写しを捨ててよいと決めるまで待つ ③ セグメントを半分に畳む forcemerge を投げ、終わりを待つ
    try:
        _req("POST", f"/{idx}/_flush")
    except Exception as e:
        _log.warning("es_index: 索引を詰める前の flush に失敗しました（詰めずに続けます）: world=%s 原因=%s", world, e.__class__.__name__)
        return
    until = time.monotonic() + _ES_COMPACT_SETTLE_SEC
    while time.monotonic() < until:
        _stop_point()
        time.sleep(min(1.0, max(0.0, until - time.monotonic())))
    try:
        stats = _req("GET", f"/{idx}/_stats/segments")["indices"]
        n = sum(v["primaries"]["segments"]["count"] for v in stats.values())
        if n < 2:
            return
        task = _req("POST", f"/{idx}/_forcemerge?max_num_segments={n // 2}&wait_for_completion=false").get("task")
    except Exception as e:
        _log.warning("es_index: 索引を詰められませんでした（詰めずに続けます）: world=%s 原因=%s", world, e.__class__.__name__)
        return
    deadline = time.monotonic() + _ES_COMPACT_TIMEOUT_SEC
    while task:
        _stop_point()
        try:
            res = _req("GET", f"/_tasks/{task}")
        except Exception as e:
            _log.warning("es_index: 索引を詰める処理の終わりを確かめられませんでした（待たずに続けます）: world=%s 原因=%s",
                         world, e.__class__.__name__)
            return
        if res.get("completed"):
            if res.get("error"):
                _log.warning("es_index: 索引を詰める処理が失敗しました（詰めずに続けます）: world=%s", world)
            return
        if time.monotonic() >= deadline:
            _log.warning("es_index: 索引を詰める処理が %d 秒で終わりませんでした（待たずに続けます）: world=%s",
                         _ES_COMPACT_TIMEOUT_SEC, world)
            return
        time.sleep(_ES_COMPACT_POLL_SEC)


def index_world(world: str, settings: dict | None = None, content_sig: str | None = None,
                progress: Callable[[int, int], None] | None = None) -> dict:
    """資料フォルダを再索引する（新しい物理索引を作って全件入れ、エイリアスを付け替える）。埋め込み設定があればベクトルも付与する（kNN 用）。
    - 作り直しの間もエイリアスは古い索引を指し続け、検索は途切れない。途中の失敗・停止要求では作りかけの物理索引だけを消し、古い索引（とそのメタ）には触れない。
      ただし ES の空きが古い索引の大きさの 2 倍に満たなければ、新しい物理索引を作る直前に古い索引を消す（`_drop_old_for_space`・終わるまで検索は空。途中で止まれば索引が無い状態になり、次の更新が作り直す）。
    - 埋め込みのまとまりが再送を使い切って取れなくても、資料フォルダ全体を降格・打ち切りにはしない。そのチャンクだけ、ベクトル無し・`vector_status: "missing"` で入れる（件数は戻り値の `vectors_missing`・`fill_missing_vectors` が後から足す）。埋め込みを選んだクラウドが設定の段階で解決できないときだけ、何も変えずに打ち切る（`embedding_cloud_unavailable`）。
    - `content_sig`＝索引時のフォルダ署名（`_meta` に保存・古い索引の検知に使う）。埋め込みは内容ハッシュキャッシュ（`_embed_fill`）経由で未変更チャンクの再 embed を省く。
    - Office／PDF（`{rel}.rag_chunks.jsonl` を持つ）はレコード単位チャンク、それ以外（テキスト資料、rag_chunks が無い／検証に失敗した Office／PDF）は 40 行チャンク。rag_chunks があるのに使えなかった文書と本文が空白のみの文書（reason="empty_text"）は、戻り値の `rag_degraded`（件数）・`rag_degraded_docs`（内訳）・`rag_degraded_by_reason`（理由別の件数）で報告する。本文を読めず索引から外した文書は reason="text_read_failed"。
      ベクトルを持つチャンクが 1 件も無い索引になったときは `embed_degraded=True`、日本語アナライザを作れず標準で作り直したときは `analyzer_fallback=True`（どちらも取り込みの状態へ出す）。
    - ソース（`branch=="source"` の登録コードと、軽量テキスト枠の第 1 段の汎用コード）は索引に入れない（`_is_source_doc`・探すのは ripgrep_search）。軽量テキスト枠の第 2 段（csv・log などのテキスト資料）は索引に入れるが、埋め込みはしない（BM25 のみ）。索引の `_meta.source_excluded` に印を残し、印の無い旧索引は `needs_reindex` が張り直す。
    - ベクトルを持つ索引は、`_ES_COMPACT_MIN_CHUNKS` 件以上入れたら Pass2 の終わり（付け替えの前）に詰める（`_compact_building`・失敗・時間切れでも取り込みは続ける）。
      取り込みの途中では詰めない・refresh しない（forcemerge も refresh も、その後のマージで置き換わった古いセグメントを次の refresh まで残させる）。
    - bulk 送信は `_bulk_batches()` で件数・バイト数ともに有界なバッチへ分割し、`refresh=true` は最後のバッチだけに付ける。途中のバッチが失敗したら作りかけの索引を捨てて error を返す（全部か無しか）。
    - 2 パス構成（メモリを資料フォルダの規模に比例させない）:
      - Pass1（埋め込みのみ）: `_iter_doc_chunk_records()` でチャンクを 1 件ずつ受け取り、embed 対象テキストを `_EMBED_FLUSH_CHUNKS` 件程度に束ねて `_embed_fill()` を呼ぶ。生きている鍵はキャッシュ DB の `live` 表へ溜める。
      - Pass2（bulk 送信）: 同じ `docs` をもう一度走査して id／body／text を 1 件ずつ受け取り、`_EMBED_FLUSH_CHUNKS` 件程度のグループに束ね、対象チャンクだけキャッシュから embedding を引いて `_StreamingBulkSender` で新しい物理索引へ流す。
      キャッシュの剪定（現存チャンクだけへ縮める）は Pass1 直後（ES 操作の前）に `_prune_embed_cache()` で 1 回だけ行う。
    - 区切りで停止要求を確かめる（Pass1 の `_EMBED_FLUSH_CHUNKS` 件ごと・Pass2 の bulk ごと）。止まったときは作りかけの物理索引を消して `background.StopRequested` を投げる（マーカーと `content_sig` は確定しない＝次の取り込みで作り直す。埋め込みはキャッシュ済みの分を飛ばして続きから取る）。
    - `progress`（省略可）: Pass1 は文書 1 件処理するたび、Pass2 は文書グループを flush するたびに `progress(done_docs, total_docs)` を呼ぶ（`total_docs` は `docs` の長さ）。Pass2 開始時に `progress(0, total_docs)` を 1 回呼ぶ。Pass2 の `done_docs` は対象外（チャンク 0 件）の文書も 1 件に数えるので、`indexed` とは別の目盛り。Pass1→Pass2 の切替で一度小さく戻るため、単調増加を前提にしない。呼び出しの間引きは呼び出し元の責務。
    設計: docs/design/rag.md「ES 索引の構成」
    """
    _building.pop(world, None)
    try:
        return _index_world(world, settings, content_sig, progress)
    except BaseException:  # 停止要求（StopRequested）・想定外の例外: 作りかけの物理索引を残さない
        _close_sender(world)
        _discard_building(world)
        raise
    finally:
        _close_sender(world)
        _building.pop(world, None)
        substage.end()                       # 早く抜けても小さな段を残さない（「まだ動いています」を出し続けない）


_ES_BULK_LABEL_DROPPED = "容量が足りないため、古い検索の索引を消して作り直しています（終わるまで検索できません）"


def _index_world(world: str, settings, content_sig, progress) -> dict:
    if not available():
        return {"available": False, "indexed": 0, "chunks": 0}
    # `cfg()` と `cloud_selected_but_unavailable()` を同じ system_settings スナップショットで呼ぶ（kill-switch 有効時は読まない）
    sys_s = _embed_system_settings_snapshot()
    ec = embeddings.cfg(_settings(settings), system_settings=sys_s)
    if ec is None and embeddings.cloud_selected_but_unavailable(system_settings=sys_s):
        # 何も作る前に打ち切る（既存索引を BM25 のみで置き換えない）
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
    eligible_chunks = 0
    reused_total = 0
    embedded_total = 0
    embed_elapsed_ms = 0.0  # `_embed_fill()` 呼び出しの合計所要時間
    embed_calls_made = False  # 0（呼んだが瞬時）と未計測（一度も呼んでいない）を区別する
    guard = _EmbedGuard(world)
    if ec is not None:
        try:
            _live_keys_reset(world)
        except OSError:
            _embed_log.warning("es_index: embed キャッシュの準備に失敗（world=%s）——索引の作成前に中止する", world)
            return {"available": True, "indexed": 0, "chunks": 0, "error": "embed_cache_write_failed"}

        def _flush_embed(buf: list) -> bool:
            """束ねたテキストを埋め込む（取れなかった分は飛ばす）。キャッシュ DB に書けなければ False。"""
            nonlocal reused_total, embedded_total, embed_elapsed_ms, embed_calls_made
            _stop_point()
            _space_point(world)
            _t0 = time.monotonic()
            _vecs, reused, embedded = _embed_fill(world, buf, ec, guard, stop_points=True)
            embed_elapsed_ms += (time.monotonic() - _t0) * 1000
            embed_calls_made = True
            reused_total += reused
            embedded_total += embedded
            try:
                _live_keys_add(world, [_chunk_key(ec, t) for t in buf])
            except OSError:
                return False
            return True

        buf: list = []
        pass1_docs_done = 0
        pass1_step = substage.begin("embed_vectors", "埋め込みを取っています", total=total_docs, logger=_embed_log)
        for d in docs:
            chunk_iter, _degraded = _iter_doc_chunk_records(world, d, derived, rag_exts)
            if chunk_iter is None:
                continue
            for _cid, _body, t, skip in chunk_iter:
                if skip:
                    continue
                had_embed_eligible = True
                eligible_chunks += 1
                buf.append(t)
                if len(buf) >= _EMBED_FLUSH_CHUNKS:
                    if not _flush_embed(buf):
                        _embed_log.warning("es_index: embed キャッシュの書込に失敗（world=%s）"
                                           "——索引の作成前に中止する", world)
                        return {"available": True, "indexed": 0, "chunks": 0, "error": "embed_cache_write_failed"}
                    buf = []
            pass1_docs_done += 1
            pass1_step.tick(pass1_docs_done)
            if progress is not None:
                # Pass1 の進捗も呼ぶ（冷キャッシュ時は Pass1 が最長段になりうるため）。Pass2 開始時に done は一度小さく戻る
                progress(pass1_docs_done, total_docs)
        if buf:
            if not _flush_embed(buf):
                _embed_log.warning("es_index: embed キャッシュの書込に失敗（world=%s）"
                                   "——索引の作成前に中止する", world)
                return {"available": True, "indexed": 0, "chunks": 0, "error": "embed_cache_write_failed"}

    # 埋め込みキャッシュの最終剪定／削除は、資料フォルダ全体の doc ストリームを一巡し終えた直後（ES 操作の前）に 1 回だけ行う
    substage.begin("embed_cache", "埋め込みのキャッシュを整理しています", logger=_embed_log)
    if ec is None:
        _delete_embed_cache(world)
    else:
        try:
            _prune_embed_cache(world)
        except OSError:
            # DB 書込障害（ENOSPC 等）は成功扱いせず、索引の作成前に打ち切る（既存索引を残す）
            _embed_log.warning("es_index: embed キャッシュ剪定の書込に失敗（world=%s）"
                               "——索引の作成前に中止する", world)
            return {"available": True, "indexed": 0, "chunks": 0, "error": "embed_cache_write_failed"}

    # `ec` が有効なら、埋め込み対象チャンクが無い・取れなかったチャンクがある資料フォルダも「この構成」の索引なので emeta に埋め込み素性を書く
    # （書かないと `needs_reindex()` が毎 sync 不一致になる）。取れなかったチャンクは `fill_missing_vectors` が後から足す
    embed_feature_applies = ec is not None

    if not _drop_stale_physicals(world):  # 前回落ちた作りかけを片付けられないまま新しく作らない
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
    emeta["source_excluded"] = True  # ソース（登録コードと汎用コード）のチャンクを含まない索引（印の無い旧索引は `needs_reindex` が張り直す）
    if _CHUNK_LINES != _CHUNK_LINES_DEFAULT:  # 既定(40)時は書かない
        emeta["chunk_lines"] = _CHUNK_LINES  # legacy チャンク粒度（既定と異なるときだけ記録・drift 検知用）
    # `content_sig` は bulk が全バッチ成功した後に書く（`_confirm_content_sig`）。先に書くと、途中でプロセスが落ちたとき「一部だけ入った索引＋有効な content_sig」が残りうる。後書きなら、どう落ちても content_sig が無い＝付け替えもされない（fail-closed）
    if embed_feature_applies:
        emeta.update({"embed_provider": ec["provider"], "embed_model": ec["model"], "dim": ec["dim"],
                      "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID,  # 前処理アルゴリズム版（`_chunk_key` と同じ材料）
                      "quant": embeddings.ES_VECTOR_QUANT})  # ES へ渡すベクトルの縮め方
    _space_point(world)  # 古い索引を消す前に空きを確かめる（対象が 0 件でも止める）
    old_dropped = _drop_old_for_space(world)  # 空きが足りなければ古い索引を先に消す（終わるまで検索できない）
    if not ensure_index(world, dim=dim, emeta=(emeta or None)):
        return {"available": True, "indexed": 0, "chunks": 0, "error": "create_failed"}
    new_idx = _building.get(world)  # ensure_index が作った物理索引（差し替えられた場合は None＝エイリアス名のまま）
    analyzer_fallback = _created_analyzer.pop(world, None) == "standard"  # 日本語アナライザを作れず標準で作り直した

    # ---- Pass2: チャンクを 1 件ずつ受け取り、`_EMBED_FLUSH_CHUNKS` 件ごとにグループ化して bulk 送信する ----
    n_docs = 0
    total_chunks = 0
    rag_degraded = 0
    rag_degraded_docs: list = []
    sender = _StreamingBulkSender(world, new_idx)
    g_ids: list = []
    g_bodies: list = []
    g_texts: list = []
    g_no_embed: list = []
    # `docs_done` は Pass2 が見終えた文書数（対象外の文書も含む）。`n_docs`（実際に索引した文書数）とは別に持ち、対象外が連続しても `total_docs` へ収束させる
    docs_done = 0
    pass2_step = substage.begin("es_bulk", _ES_BULK_LABEL_DROPPED if old_dropped else "全文索引に書き込んでいます",
                                total=total_docs, logger=_embed_log, note=_free_space_note(world))
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
            pass2_step.tick(docs_done)
            if progress is not None:
                progress(docs_done, total_docs)
            continue
        n_docs += 1
        docs_done += 1
        pass2_step.tick(docs_done)
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

    substage.begin("es_swap", "全文索引を確定し、付け替えています", logger=_embed_log)
    rag_report = {"rag_degraded": rag_degraded}
    if rag_degraded_docs:
        rag_report["rag_degraded_docs"] = rag_degraded_docs
        by_reason: dict = {}
        for entry in rag_degraded_docs:
            by_reason[entry["reason"]] = by_reason.get(entry["reason"], 0) + 1
        rag_report["rag_degraded_by_reason"] = by_reason
    if sender.missing:
        rag_report["vectors_missing"] = sender.missing  # ベクトル無しで入れたチャンク（次の同期で足す）
    if ec is not None and had_embed_eligible and sender.missing >= eligible_chunks:
        rag_report["embed_degraded"] = True    # ベクトルを持つチャンクが 1 件も無い（全文検索のみ）
    if analyzer_fallback:
        rag_report["analyzer_fallback"] = True

    def _failure(error: str) -> dict:
        sender.close()
        _discard_building(world)  # 作りかけの物理索引だけ捨てる（エイリアスの指す古い索引はそのまま）
        out = {"available": True, "indexed": 0, "chunks": 0, "error": error, **rag_report}
        if embed_calls_made:
            out["embed_elapsed_ms"] = round(embed_elapsed_ms)
        return out

    if sender.failed or not sender.finish():
        return _failure(sender.error)
    if n_docs > 0 and total_chunks == 0:
        # 文書があるのにチャンク 0 件は索引の異常として扱い、付け替えない（次回 sync が再索引する）。空の資料フォルダ（`n_docs==0`）は対象外
        return _failure("no_chunks")
    if dim and total_chunks >= _ES_COMPACT_MIN_CHUNKS:  # 詰めるのはベクトルを持つ索引だけ
        substage.begin("es_compact", "検索の索引を詰めています", logger=_embed_log, note=_free_space_note(world))
        _compact_building(world, sender.idx)
        substage.begin("es_swap", "全文索引を確定し、付け替えています", logger=_embed_log)
    _restore_refresh_interval(world, new_idx)  # 全バッチ成功＝最終 refresh で可視化済み・背景 refresh を通常へ戻す
    _confirm_content_sig(world, content_sig, new_idx)  # 全バッチ成功後にだけ鮮度署名と実件数を確定する
    if new_idx is not None and not _activate_index(world, new_idx):
        return _failure("alias_swap_failed")
    out = {"available": True, "indexed": n_docs, "chunks": total_chunks,
          "vectors": bool(embed_feature_applies and had_embed_eligible and sender.missing < eligible_chunks),
          "embedded": embedded_total, "reused": reused_total, **rag_report}
    if embed_calls_made:
        # `embed_elapsed_ms`: 埋め込み呼び出しの合計所要時間。一度も呼んでいなければキーを付けない（0 と欠落を区別する）
        out["embed_elapsed_ms"] = round(embed_elapsed_ms)
    _embed_log.info("es_index: 全文検索の登録が終わりました: world=%s・文書 %d 件・チャンク %d 件・埋め込みを取った %d 件"
                    "・キャッシュから %d 件・ベクトル無し %d 件",
                    world, n_docs, total_chunks, embedded_total, reused_total, sender.missing)
    return out


def count(world: str, idx: str | None = None) -> int | None:
    try:
        return _req("GET", f"/{idx or _index(world)}/_count").get("count")
    except Exception:
        return None


def store_bytes(world: str) -> int | None:
    """この資料フォルダの索引（世代の物理索引を含む）が ES のディスクに占める大きさ（バイト）。取得失敗は None。"""
    names = _concrete_indices(world)
    if names is None:
        return None
    if not names:
        return 0
    try:
        rows = _req("GET", f"/_cat/indices/{','.join(names)}?format=json&bytes=b&h=index,pri.store.size")
        return sum(int(r.get("pri.store.size") or 0) for r in rows)
    except Exception:
        return None


def fs_stats() -> dict | None:
    """ES がデータを置いているファイルシステムの `{"total": バイト, "free": バイト}`（ES 自身の報告・複数ノードは最小の空き）。取得失敗は None。"""
    try:
        res = _req("GET", "/_nodes/stats/fs", timeout=_AVAILABLE_TIMEOUT * 3)
        best = None
        for node in (res.get("nodes") or {}).values():
            tot = ((node.get("fs") or {}).get("total") or {})
            t, f = int(tot["total_in_bytes"]), int(tot["available_in_bytes"])
            if best is None or f < best["free"]:
                best = {"total": t, "free": f}
        return best
    except Exception:
        return None


_SCROLL_PAGE = 1000


def _scroll_hits(world: str, query: dict, source) -> list:
    """`query` に当たる切れ端を全部（scroll で）返す。各要素は ES の hit。"""
    idx = _index(world)
    res = _req("POST", f"/{idx}/_search?scroll=1m",
               {"size": _SCROLL_PAGE, "query": query, "_source": source, "sort": ["_doc"]})
    hits: list = []
    scroll_id = res.get("_scroll_id")
    try:
        while True:
            page = (res.get("hits") or {}).get("hits") or []
            if not page:
                return hits
            hits.extend(page)
            res = _req("POST", "/_search/scroll", {"scroll": "1m", "scroll_id": scroll_id})
            scroll_id = res.get("_scroll_id") or scroll_id
    finally:
        if scroll_id:
            try:
                _req("DELETE", "/_search/scroll", {"scroll_id": scroll_id})
            except Exception:
                pass


def _text_used_elsewhere(world: str, rel: str, text: str) -> bool:
    """`rel` 以外の埋め込み付きの切れ端に、本文がまったく同じものがあるか。確かめられなければ True（キーを残す側）。"""
    try:
        res = _req("POST", f"/{_index(world)}/_search", {
            "size": 100, "_source": ["text"],
            "query": {"bool": {"filter": [{"exists": {"field": "embedding"}}],
                               "must_not": [{"term": {"doc_id": rel}}],
                               "must": [{"match_phrase": {"text": text}}]}}})
    except Exception:
        return True
    return any((h.get("_source") or {}).get("text") == text for h in (res.get("hits") or {}).get("hits") or [])


def _embed_cache_delete_keys(world: str, keys: list) -> int:
    """埋め込みのキャッシュから `keys` を消す（best-effort・消した件数）。"""
    if not keys:
        return 0
    conn = _embed_cache_connect(world, create=False)
    if conn is None:
        return 0
    n = 0
    try:
        with conn:
            for chunk in _sql_chunks(list(keys)):
                placeholders = ",".join("?" * len(chunk))
                n += conn.execute(f"DELETE FROM {conn.table} WHERE key IN ({placeholders})", chunk).rowcount
    except sqlite3.Error:
        _embed_log.warning("es_index: 埋め込みのキャッシュのキーを消せませんでした（world=%s）", world)
    finally:
        conn.close()
    return n


def _delete_payloads(ids: list) -> list:
    """`ids` の切れ端を消す bulk 用の NDJSON（`_ES_BULK_BATCH_MAX_DOCS` 件ずつ）。"""
    return ["".join(json.dumps({"delete": {"_id": i}}) + "\n" for i in ids[s:s + _ES_BULK_BATCH_MAX_DOCS])
            for s in range(0, len(ids), _ES_BULK_BATCH_MAX_DOCS)]


def _old_chunk_restore_payloads(world: str, ec, old_hits: list, vec_ids: set) -> list:
    """`replace_document` の巻き戻し用に、古い切れ端を入れ直す NDJSON を作る。ベクトルは `_source` に無いので埋め込みのキャッシュから取り（API は呼ばない）、
    ベクトルを持っていたのにキャッシュに無い切れ端は `vector_status: "missing"` で入れる（`fill_missing_vectors` が後から足す）。
    """
    ids = [h["_id"] for h in old_hits]
    bodies = [dict(h.get("_source") or {}) for h in old_hits]
    vec_by_idx: dict = {}
    if ec and vec_ids:
        keys = {i: _chunk_key(ec, b.get("vector_text") or b["text"]) for i, b in enumerate(bodies)
                if ids[i] in vec_ids and isinstance(b.get("vector_text") or b.get("text"), str)}
        cached = _embed_cache_lookup_batch(world, list(set(keys.values())), ec["dim"])
        for i, k in keys.items():
            if k in cached:
                vec_by_idx[i] = cached[k]
        for i, cid in enumerate(ids):
            if cid in vec_ids and i not in vec_by_idx:
                bodies[i]["vector_status"] = "missing"
    return _bulk_batches(ids, bodies, vec_by_idx)


def _send_bulk(world: str, payloads: list) -> str | None:
    """bulk 用の NDJSON を順に送り、最後だけ refresh する。失敗なら理由（`bulk_failed`／`bulk_errors`）。"""
    for i, payload in enumerate(payloads):
        path = f"/{_index(world)}/_bulk" + ("?refresh=true" if i == len(payloads) - 1 else "")
        try:
            res = _req("POST", path, payload, ndjson=True)
        except Exception:
            return "bulk_failed"
        if res.get("errors"):
            return "bulk_errors"
    return None


def replace_document(world: str, rel: str, rp: Path, *, content_sig: str) -> dict:
    """1 文書の切れ端だけを入れ替える（索引は消さない・ほかの文書の切れ端には触れない）。
    設計: docs/design/rag.md「失敗した資料のやり直し」
    ① 索引があり（切れ端が 0 件でもよい）、今の署名の世代で、埋め込みの素性が今の設定と同じか確かめる（違えば何も書かずに error）
    ② その文書の新しい切れ端を `index_world` と同じ組み立てで作り、埋め込みはキャッシュから引く（無いものだけ API を呼ぶ）
    ③ 新しい切れ端を全部書き込んでから、新しい側に無い古い切れ端を消す。途中で失敗したら、新しく入れた切れ端を消し
       控えた古い切れ端を入れ直して古い切れ端だけの状態へ戻す（戻せなければ error に `_rollback_failed` を付ける）
    ④ `content_sig` が今の署名と同じ索引なら `_meta.doc_count` を実件数に直す
    ⑤ 古い切れ端の埋め込みのキャッシュのキーを、ほかの切れ端が同じ本文を使っていなければ消す
    返値 `{available, error?, chunks, deleted, embedded, reused, doc_count}`（`chunks` はこの文書の切れ端の数・`doc_count` は索引全体）。
    """
    if not available():
        return {"available": False, "error": "unavailable"}
    meta = _index_meta(world)
    if meta is None:
        return {"available": True, "error": "index_missing"}
    if meta.get("content_sig") != content_sig:  # 索引が今の署名の世代でない＝1 文書だけ混ぜない（全体の更新に任せる）
        return {"available": True, "error": "index_stale"}
    if count(world) is None:
        return {"available": True, "error": "count_failed"}
    sys_s = _embed_system_settings_snapshot()
    ec = embeddings.cfg(_settings(None), system_settings=sys_s)
    want = ((ec["provider"], ec["model"], ec["dim"], embeddings.EMBEDDING_INPUT_ALGORITHM_ID, embeddings.ES_VECTOR_QUANT)
            if ec else (None,) * 5)
    have = (meta.get("embed_provider"), meta.get("embed_model"), meta.get("dim"), meta.get("embed_algo"), meta.get("quant"))
    if want != have:
        return {"available": True, "error": "embed_config_changed"}
    with_vectors = ec is not None
    # ② 新しい切れ端
    wd = worlds.world_dir(world)
    res_map = importance.resolve_for_world(world, root=wd, sig=content_sig) if wd else {}
    ids: list = []
    bodies: list = []
    texts: list = []
    embed_idx: list = []
    for d in corpus_docs.iter_world_documents(world, include_rag=True, root=wd, files=[(Path(rp), rel)]):
        chunk_iter, _degraded = _iter_doc_chunk_records(world, d, worlds.derived_rag_dir(world),
                                                        _rag_chunk_source_exts(), res_map)
        if chunk_iter is None:
            if not _is_source_doc(d) and (_degraded or {}).get("reason") != "empty_text":  # 読めない＝ES を変えずに止める（古い切れ端を消さない）。ソースは元から ES に無い（残りがあれば消す）
                return {"available": True, "error": "chunk_read_failed"}
            continue
        for cid, body, text, skip in chunk_iter:
            if not skip:
                embed_idx.append(len(ids))
            ids.append(cid)
            bodies.append(body)
            texts.append(text)
    vec_by_idx: dict = {}
    reused = embedded = 0
    if with_vectors and embed_idx:
        vecs, reused, embedded = _embed_cached(world, [texts[i] for i in embed_idx], ec)
        if vecs is None:
            return {"available": True, "error": "embedding_failed"}
        vec_by_idx = dict(zip(embed_idx, vecs))
    # ③ 書き込みと古い切れ端の削除（古い切れ端は全部を控え、途中で失敗したら古い切れ端だけの状態へ戻す）
    try:
        old_hits = _scroll_hits(world, {"term": {"doc_id": rel}}, True)
        # `_source` にベクトルは無い。ベクトルを持つ切れ端の ID は `exists` で引く（巻き戻しと⑤で使う）
        vec_ids = ({h["_id"] for h in _scroll_hits(world, {"bool": {"filter": [
            {"term": {"doc_id": rel}}, {"exists": {"field": "embedding"}}]}}, False)} if with_vectors else set())
    except Exception:
        return {"available": True, "error": "search_failed"}
    old_ids = [h["_id"] for h in old_hits]
    new_ids = set(ids)
    stale = [i for i in old_ids if i not in new_ids]
    err = _send_bulk(world, _bulk_batches(ids, bodies, vec_by_idx)) if ids else None
    if err is None and stale:
        err = _send_bulk(world, _delete_payloads(stale))
    if err is not None:
        restore = (_delete_payloads(sorted(new_ids - set(old_ids)))
                   + _old_chunk_restore_payloads(world, ec, old_hits, vec_ids))
        if restore and _send_bulk(world, restore) is not None:
            return {"available": True, "error": f"{err}_rollback_failed"}
        return {"available": True, "error": err}
    old_embedded = [h for h in old_hits if h["_id"] in vec_ids]
    # ④ 件数の印
    n = count(world)
    meta = _index_meta(world)
    if n is None or meta is None:
        return {"available": True, "error": "meta_update_failed"}
    if meta.get("content_sig") == content_sig and meta.get("doc_count") != n:  # 件数が無い・ずれていれば実件数を刻む
        try:
            _req("PUT", f"/{_index(world)}/_mapping", {"_meta": {**meta, "doc_count": n}})
        except Exception:
            return {"available": True, "error": "meta_update_failed"}
    # ⑤ 使われなくなった埋め込みのキャッシュのキー
    if with_vectors and old_embedded:
        new_keys = {_chunk_key(ec, texts[i]) for i in embed_idx}
        candidates: dict = {}
        for h in old_embedded:
            if (h.get("_source") or {}).get("vector_text"):
                continue  # まとまりの文字は本文の索引で同じものを確かめられない＝キーを残す（次の全体の作り直しで剪定される）
            t = (h.get("_source") or {}).get("text")
            if isinstance(t, str):
                k = _chunk_key(ec, t)
                if k not in new_keys:
                    candidates.setdefault(k, t)
        _embed_cache_delete_keys(world, [k for k, t in candidates.items()
                                         if not _text_used_elsewhere(world, rel, t)])
    return {"available": True, "chunks": len(ids), "deleted": len(stale),
            "embedded": embedded, "reused": reused, "doc_count": n}


def needs_reindex(world: str, content_sig, settings: dict | None = None) -> bool:
    """ES 索引の張り直しが要るか（ES 稼働時のみ）。次のいずれかで True:
    空／内容署名ズレ／ソース除外の印なし／アーム構成ズレ／マッピング版ズレ／チャンク粒度ズレ／人間向け MD 版ズレ／アナライザ構成ズレ／埋め込み素性（provider／model／dim／前処理アルゴリズム版／ES へ渡す縮め方）ズレ／実件数ズレ。
    `content_sig`（ソースファイルの rel／mtime／ctime／size のみ）はアーム構成・マッピング版等の変更を検知できないため、各署名を別途比較する。索引済みメタにフィールド自体が無い旧索引は比較先が None になり、不一致＝1 回だけ再索引される。
    例外: `human_md_sig` は pending 中は比較先がセンチネルになり、pending が解消するまで毎回再索引を試みる（fail-closed）。`chunk_lines` は欠落を旧既定 `_CHUNK_LINES_DEFAULT`（40）として扱う。
    エイリアス: 索引名がエイリアスでなく実の索引（0.18 以前の形）なら True（0.19 の最初の更新で 1 回だけ作り直す）。ベクトル無しのチャンクがあることは理由にしない（`fill_missing_vectors` が足す）。
    実件数ズレ: `_meta.doc_count`（`_confirm_content_sig` が刻む）と `count(world)` が食い違えば True（資料は不変のまま ES の文書が欠けた状態の自己修復）。`doc_count` が無い旧索引ではこの条件だけ比較しない（スタンプを刻むためだけに有料の再埋め込みを走らせないため）。
    """
    if not available():
        return False
    n = count(world)
    if n is None:
        return True
    if _index_kind(world) in ("index", "none"):  # エイリアスでない（0.18 以前の実の索引）＝新しい形へ 1 回作り直す
        return True
    meta = _index_meta(world) or {}  # GET 失敗は未設定相当＝fail-closed で reindex を促す
    if meta.get("content_sig") != content_sig:
        return True
    if not n and meta.get("doc_count") != 0:  # 空は張り直す。ただしソースだけの資料フォルダは正当に 0 件なので、張り直し済み（件数 0 を刻んだ）なら毎回は張り直さない
        return True
    if meta.get("source_excluded") is not True:  # ソースのチャンクが残っている旧索引
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
    want = ((ec["provider"], ec["model"], ec["dim"], embeddings.EMBEDDING_INPUT_ALGORITHM_ID, embeddings.ES_VECTOR_QUANT)
            if ec else (None,) * 5)
    have = (meta.get("embed_provider"), meta.get("embed_model"), meta.get("dim"), meta.get("embed_algo"), meta.get("quant"))
    if want != have:
        return True
    doc_count = meta.get("doc_count")
    return doc_count is not None and n != doc_count



_MISSING_QUERY = {"term": {"vector_status": "missing"}}
_FILL_SCROLL = "10m"  # 1 ページ（`_EMBED_FLUSH_CHUNKS` 件）の埋め込みが再送で長引いても切れない長さ


def count_missing_vectors(world: str) -> int | None:
    """ベクトル無し（`vector_status: missing`）のチャンク数。取れなければ None。"""
    try:
        return _req("POST", f"/{_index(world)}/_count", {"query": _MISSING_QUERY}).get("count")
    except Exception:
        return None


def _vector_update_batches(ids: list, vecs: list) -> list:
    """`_update` 用の NDJSON（ベクトルと `vector_status` だけを書く＝本文は触らない）。件数・バイト数で有界に分ける。"""
    out: list = []
    lines: list = []
    n_bytes = 0
    for cid, vec in zip(ids, vecs):
        q = embeddings.vector_to_es_bytes(vec)
        if q is None:
            continue
        action = json.dumps({"update": {"_id": cid}})
        doc = json.dumps({"doc": {"embedding": q, "vector_status": "ok"}})
        pair = len(action) + len(doc) + 2
        if lines and (len(lines) >= 2 * _ES_BULK_BATCH_MAX_DOCS or n_bytes + pair > _ES_BULK_BATCH_MAX_BYTES):
            out.append("\n".join(lines) + "\n")
            lines, n_bytes = [], 0
        lines += [action, doc]
        n_bytes += pair
    if lines:
        out.append("\n".join(lines) + "\n")
    return out


def fill_missing_vectors(world: str, settings: dict | None = None) -> dict:
    """ベクトル無しで入れたチャンク（`vector_status: missing`）の埋め込みを取り直し、ベクトルと `vector_status` だけを書き足す。取り込み（変更あり・なし）の最後に呼ぶ。
    設計: docs/design/rag.md「ベクトルの無いチャンク」
    ① 索引の埋め込み素性が今の設定と同じか確かめる（違えば何もしない＝再索引が作り直す） ② scroll で一度に `_EMBED_FLUSH_CHUNKS` 件ずつ読み、埋め込みを取る（キャッシュに入れる）
    ③ 取れたものだけ `_update` で書く（同じ ID への部分更新なので二重にならない）。取れなかったものは missing のまま次回へ。区切り（ページごと）で停止要求を確かめる。
    直近に埋め込みのまとまりが失敗していれば（`_EMBED_COOLDOWN_SEC`）送り先を叩き直さない。
    返値 `{available, filled, missing, error?, skipped?}`（`missing`＝終了後に残っている件数・取れなければ None）。
    """
    if not available():
        return {"available": False, "filled": 0, "missing": None}
    before = count_missing_vectors(world)
    if not before:
        return {"available": True, "filled": 0, "missing": before}
    sys_s = _embed_system_settings_snapshot()
    ec = embeddings.cfg(_settings(settings), system_settings=sys_s)
    if ec is None:
        return {"available": True, "filled": 0, "missing": before, "skipped": "embedding_not_configured"}
    meta = _index_meta(world)
    if meta is None:
        return {"available": True, "filled": 0, "missing": before, "error": "index_missing"}
    want = (ec["provider"], ec["model"], ec["dim"], embeddings.EMBEDDING_INPUT_ALGORITHM_ID, embeddings.ES_VECTOR_QUANT)
    have = (meta.get("embed_provider"), meta.get("embed_model"), meta.get("dim"), meta.get("embed_algo"), meta.get("quant"))
    if want != have:  # 設定が変わった索引は再索引が作り直す
        return {"available": True, "filled": 0, "missing": before, "skipped": "embed_config_changed"}
    last_fail = _embed_failed_at.get(world)
    if last_fail is not None and time.monotonic() - last_fail < _EMBED_COOLDOWN_SEC:
        return {"available": True, "filled": 0, "missing": before, "skipped": "embed_recently_failed"}
    guard = _EmbedGuard(world)
    filled = 0
    idx = _index(world)
    scroll_id = None
    fill_step = substage.begin("fill_vectors", "ベクトルを足し直しています", total=before, logger=_embed_log)
    try:
        res = _req("POST", f"/{idx}/_search?scroll={_FILL_SCROLL}",
                   {"size": _EMBED_FLUSH_CHUNKS, "query": _MISSING_QUERY, "_source": ["text", "vector_text"],
                    "sort": ["_doc"]})
        scroll_id = res.get("_scroll_id")
        while True:
            page = (res.get("hits") or {}).get("hits") or []
            if not page:
                break
            _stop_point()
            rows = [(h["_id"], (h.get("_source") or {}).get("vector_text") or (h.get("_source") or {}).get("text"))
                    for h in page]
            rows = [(i, t) for i, t in rows if isinstance(t, str) and t]
            vecs, _reused, _embedded = _embed_fill(world, [t for _i, t in rows], ec, guard, stop_points=True)
            got = [(i, v) for (i, _t), v in zip(rows, vecs) if v is not None]
            for payload in _vector_update_batches([i for i, _v in got], [v for _i, v in got]):
                r = _req("POST", f"/{idx}/_bulk", payload, ndjson=True)
                if r.get("errors"):  # 読んだ後に消えた・置き換わった文書（404）は無視する。それ以外は止める
                    bad = [it for it in (r.get("items") or []) if (it.get("update") or {}).get("error")
                           and (it.get("update") or {}).get("status") != 404]
                    if bad:
                        raise RuntimeError("bulk_errors")
            filled += len(got)
            fill_step.tick(filled)
            if guard.gave_up:
                break
            res = _req("POST", "/_search/scroll", {"scroll": _FILL_SCROLL, "scroll_id": scroll_id})
            scroll_id = res.get("_scroll_id") or scroll_id
    except Exception as exc:
        from .ingest import background
        if isinstance(exc, background.StopRequested):
            raise
        _log.warning("es_index: ベクトルの足し直しに失敗しました（次回 sync で再試行）: world=%s %s", world, type(exc).__name__)
        return {"available": True, "filled": filled, "missing": count_missing_vectors(world), "error": "fill_failed"}
    finally:
        if scroll_id:
            try:
                _req("DELETE", "/_search/scroll", {"scroll_id": scroll_id})
            except Exception:
                pass
        if filled:
            try:
                _req("POST", f"/{idx}/_refresh")
            except Exception:
                pass
        fill_step.end()
    return {"available": True, "filled": filled, "missing": count_missing_vectors(world)}


def _search_embed_timeout_s() -> float:
    """検索の質問の埋め込みの総期限（秒）。env `SHERPA_SEARCH_EMBED_TIMEOUT_SEC` が範囲外・数値でなければ既定。"""
    raw = os.environ.get("SHERPA_SEARCH_EMBED_TIMEOUT_SEC", "").strip()
    try:
        v = float(raw)
    except ValueError:
        return _SEARCH_EMBED_TIMEOUT_DEFAULT
    return v if _SEARCH_EMBED_TIMEOUT_MIN <= v <= _SEARCH_EMBED_TIMEOUT_MAX else _SEARCH_EMBED_TIMEOUT_DEFAULT


def _embed_query(q: str, ec: dict, world: str):
    """検索の質問 1 件の埋め込み。総期限（`_search_embed_timeout_s`）内に取れなければ None。"""
    return embeddings.embed([q], ec, world=world, deadline_s=_search_embed_timeout_s())


def _keyword_clause(q: str) -> dict:
    """本文＋見出し系の欄を引く BM25 の節（本文が最も重い）。"""
    return {"multi_match": {"query": q, "type": "best_fields", "tie_breaker": _BM25_TIE_BREAKER,
                            "fields": [f"{f}^{_BM25_FIELD_BOOSTS[f]}" for f in _BM25_FIELDS]}}


def _keyword_highlight() -> dict:
    """当たった欄を知るためのハイライト（一致した欄だけが応答に出る）。"""
    return {"fields": {f: {"fragment_size": 240 if f == "text" else 120, "number_of_fragments": 1}
                       for f in _BM25_FIELDS}}


def _matched_fields(highlight) -> list:
    """ハイライトの応答から、語が当たった欄を `_BM25_FIELDS` の順で返す。"""
    if not isinstance(highlight, dict):
        return []
    return [f for f in _BM25_FIELDS if highlight.get(f)]


def _mark_keyword_all(hits: list) -> list:
    for h in hits:
        h["keyword_match"] = True
    return hits


def _as_row_group_hit(hit: dict, src: dict) -> dict:
    """意味検索で当たった表の行・本文のまとまりの先頭を、まとまりの本文・範囲に差し替える（`chunk_id` は先頭のまま）。"""
    group = src.get("row_group")
    vector_text = src.get("vector_text")
    if not isinstance(group, dict) or not isinstance(vector_text, str) or not vector_text:
        return hit
    hit["text"] = vector_text
    if isinstance(group.get("locator"), dict):
        hit["locator"] = group["locator"]
    if isinstance(group.get("chunk_ids"), list):
        hit["row_group_chunk_ids"] = group["chunk_ids"]
    return hit


def _parse_hits(res: dict, vector: bool = False) -> list:
    """ES の応答をヒットの形にする。`vector=True`（kNN の応答）は表の行・本文のまとまりをまとまりの本文で返す。"""
    out = []
    for h in res.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        full = src.get("text", "")
        frag = (h.get("highlight", {}).get("text") or [full])[0]
        hit = {"doc_id": src.get("doc_id"), "line": src.get("line"),
               "text": frag, "score": h.get("_score"), "ext": src.get("ext")}
        matched = _matched_fields(h.get("highlight"))
        if matched:
            hit["matched_fields"] = matched  # 語が当たった欄（text／labels／region_context）
        # 抽出来歴・rag_chunks 由来メタ・重要度を表示用にそのまま渡す（無ければ付けない）。`parent_id` は親返しが読む。`importance_source` は出典には出さないので含めない
        for k in ("extraction_method", "confidence", "has_conflicts", "chunk_id", "locator",
                  "previous_chunk_id", "next_chunk_id", "parent_id", "logical_record_id", "section_path",
                  "importance", "importance_reason"):
            if src.get(k) is not None:
                hit[k] = src[k]
        out.append(_as_row_group_hit(hit, src) if vector else hit)
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


def _fuse_hybrid(bm25_res: dict, knn_res: dict, k: int) -> list:
    """BM25 と kNN の ES 応答を RRF（`fused_search.fuse_rrf`）で融合し、`_parse_hits` の形で上位 k 件を返す。
    重みは `_HYBRID_WEIGHT`（keyword）と残り（vector）を合計 2.0 に配分（0.5＝対等＝各 1.0）。score は RRF の点数。
    `keyword_match` はここでは BM25 の上位に出たか（kNN だけのヒットは `_mark_keyword_matches` が語の一致で付け直す）。本文・ハイライトは BM25 側を優先し、kNN だけのヒットは kNN 側の本文（ハイライトなし）。`engine_ranks` に融合前の順位を残す。
    """
    from .parts.read.fused_search import fuse_rrf  # fused_search は es_index を import する＝呼び出し時に読む

    raw: dict = {}
    per_engine: dict = {"keyword": [], "vector": []}
    for engine, res in (("keyword", bm25_res), ("vector", knn_res)):
        for h in res.get("hits", {}).get("hits", []):
            raw.setdefault(h["_id"], h)
            per_engine[engine].append({"key": h["_id"], "doc_id": h["_id"], "path": "", "snippet": ""})
    weights = {"keyword": _HYBRID_WEIGHT * 2.0, "vector": (1.0 - _HYBRID_WEIGHT) * 2.0}
    out = []
    for m in fuse_rrf(per_engine, weights, k):
        hit = _parse_hits({"hits": {"hits": [raw[m["doc_id"]]]}}, vector="vector" in m["sources"])[0]
        hit["score"] = m["score"]
        hit["keyword_match"] = "keyword" in m["sources"]
        hit["engine_ranks"] = dict(m["sources"])
        hit["_es_id"] = m["doc_id"]          # `_mark_keyword_matches` が使って取り除く
        out.append(hit)
    return out


def _mark_keyword_matches(world: str, query: str, hits: list) -> list:
    """融合の結果のうち BM25 の上位に出なかったヒットについて、語が一致するか（本文・見出し系の欄のどれかに当たるか）を ES に問い合わせて `keyword_match`・`matched_fields` を付け直す。
    問い合わせに失敗したら付け直さない（False のまま）。
    """
    es_ids = {id(h): h.pop("_es_id", None) for h in hits}
    ids = [es_ids[id(h)] for h in hits if not h.get("keyword_match") and es_ids[id(h)]]
    if not ids:
        return hits
    body = {"size": len(ids), "_source": False, "highlight": _keyword_highlight(),
            "query": {"bool": {"filter": [{"ids": {"values": ids}}], "must": [_keyword_clause(query)]}}}
    try:
        matched = {h["_id"]: _matched_fields(h.get("highlight"))
                   for h in _req("POST", f"/{_index(world)}/_search", body).get("hits", {}).get("hits", [])}
    except Exception:
        return hits
    for h in hits:
        if not h.get("keyword_match") and es_ids[id(h)] in matched:
            h["keyword_match"] = True
            if matched[es_ids[id(h)]]:
                h["matched_fields"] = matched[es_ids[id(h)]]
    return hits


_EXACT_MAX_VALUES = 20
_EXACT_VALUE_MAX_CHARS = 256


def normalize_exact(exact) -> list:
    """`es_search` の `exact`（文字列または配列）を、空白を除いた重複の無い文字列の並びにする（個数・長さの上限を超えた分は捨てる）。"""
    items = [exact] if isinstance(exact, str) else list(exact) if isinstance(exact, (list, tuple)) else []
    out = [v.strip() for v in items if isinstance(v, str)]
    return list(dict.fromkeys(v for v in out if v and len(v) <= _EXACT_VALUE_MAX_CHARS))[:_EXACT_MAX_VALUES]


def _exact_filter(exact: list) -> dict:
    """識別子の完全一致（keyword の terms）。チャンクに出る識別子の全役割、またはそのレコードが定義している識別子のどちらかに一致すれば当たる。"""
    return {"bool": {"should": [{"terms": {"identifier_values": exact}},
                                {"terms": {"record_identity_values": exact}}],
                     "minimum_should_match": 1}}


def search_exact(world: str, exact: list, scope_paths=None, k: int = 20, layer=None,
                 k_ceiling: int | None = None) -> tuple[list, str | None]:
    """識別子の完全一致だけで引く（本文検索を使わない）。`exact` は `normalize_exact` 済みの値。
    そのレコードが定義している識別子（`record_identity_values`）に当たるチャンクを先に並べる。範囲・層のフィルタは `search()` と同じ。
    返値 `(hits, degrade_reason|None)`（`search()` と同じ形・全ヒットに `keyword_match` を付ける）。
    """
    if not exact:
        return [], None
    if not available():
        return [], "es_unavailable"
    k = max(1, min(k, k_ceiling if k_ceiling is not None else _ES_SEARCH_K_MAX))
    flt = [_exact_filter(exact)]
    sel = scope_mod.normalize_scope_paths(scope_paths)
    if sel:
        flt.append({"terms": {"scopes": sel}})
    lfilt = layer_mod.es_filter(layer)
    if lfilt:
        flt.append(lfilt)
    body = {"size": k, "query": _importance_boost_query(
        {"bool": {"filter": flt, "should": [{"terms": {"record_identity_values": exact}}]}})}
    try:
        return _mark_keyword_all(_parse_hits(_req("POST", f"/{_index(world)}/_search", body))), None
    except Exception as exc:
        return [], _classify_query_exception(exc)


def search(world: str, query: str, scope_paths=None, k: int = 20, settings: dict | None = None,
          vector: bool = True, layer=None, k_ceiling: int | None = None, exact: list | None = None) -> tuple[list, str | None]:
    """検索。`vector=True` かつ埋め込み設定があれば kNN＋BM25 のハイブリッド、無ければ BM25。範囲フィルタ・graceful。
    `vector=False` は BM25 のみ（クエリ埋め込みを呼ばない・reason は常に None）。
    `k_ceiling`（省略可）: 呼び出し元が上限まで検証済みの `k` を渡す場合（`agentic_search.run_tool` の `es_search`）、`_ES_SEARCH_K_MAX` による再クランプを迂回してこちらを使う。
    `layer`（省略可・`"docs"|"code"|"both"`・既定 both）: `scopes` と同じ `filter` 節に `branch` の term／must_not フィルタを積む（`layer.es_filter`）。
    `exact`（省略可・`normalize_exact` 済み）: 識別子の完全一致で先に絞ってから本文で順位付けする（BM25・kNN の両方に同じフィルタを掛ける）。
    BM25 は本文に加え、見出し系の欄（`labels`・`region_context`）も本文より軽い重みで引き、語が当たった欄を `matched_fields` に示す。質問の埋め込みは総期限（`_search_embed_timeout_s`）内に取れなければ `query_embed_failed` で BM25 だけにする。
    各ヒットに `keyword_match`（語が一致したか）を付ける。hybrid は BM25 の上位に出たヒットは True、kNN だけで出たヒットは語の一致を問い合わせて付ける（問い合わせの失敗は False）・BM25 のみの経路は全件 True。
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
    if exact:
        flt.append(_exact_filter(exact))
    bm25 = {"size": k, "query": _importance_boost_query({"bool": {"must": [_keyword_clause(q)], "filter": flt}}),
           "highlight": _keyword_highlight()}
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
                         and meta.get("embed_algo") == embeddings.EMBEDDING_INPUT_ALGORITHM_ID
                         and meta.get("quant") == embeddings.ES_VECTOR_QUANT)
    if ec and not same:
        # 索引のベクトル素性が現在の埋め込み設定と合わない（再索引待ち）。kNN は打てないので BM25 のみへ縮退し、クエリ埋め込みも呼ばない。理由を返して静かな縮退にしない
        reason = "vector_feature_mismatch"
    if same:  # 索引のベクトル素性が一致する時だけ kNN
        qv = _embed_query(q, ec, world)
        qb = embeddings.vector_to_es_bytes(qv[0]) if qv else None
        if qb is not None:
            # ① BM25（ハイライト付き）を先に引く。失敗は kNN を打たず BM25 失敗の reason で返す
            try:
                bm25_res = _req("POST", f"/{_index(world)}/_search", bm25)
            except Exception as exc:
                return [], _classify_query_exception(exc)
            # ② kNN（同じ filter・k・num_candidates）。重要度ブーストは function_score で kNN 側へ掛ける（重要度なしの資料フォルダでは不変）
            knn_clause = {"knn": {"field": "embedding", "query_vector": qb, "k": k,
                                  "num_candidates": max(50, k * 5), "filter": flt}}
            try:
                knn_res = _req("POST", f"/{_index(world)}/_search",
                               {"size": k, "query": _importance_boost_query(knn_clause)})
            except Exception:
                # kNN だけの失敗は BM25 の結果をそのまま返す（hits は空にならない＝`es_query_failed` とは別の reason）
                return _mark_keyword_all(_parse_hits(bm25_res)), "hybrid_query_failed"
            # ③ アプリ側で RRF 融合（設計: docs/design/rag.md「融合検索（RRF）」）④ kNN だけで出たヒットにも語が一致するかを付け直す
            return _mark_keyword_matches(world, q, _fuse_hybrid(bm25_res, knn_res, k)), None
        else:
            reason = "query_embed_failed"  # クエリ埋め込みの通信失敗 → BM25 へ
    try:
        return _mark_keyword_all(_parse_hits(_req("POST", f"/{_index(world)}/_search", bm25))), reason
    except Exception as exc:
        # BM25 自体も失敗＝hits 空を最優先の理由で説明する
        return [], _classify_query_exception(exc)


def search_knn_only(world: str, query: str, scope_paths=None, k: int = 20,
                    settings: dict | None = None, layer=None, k_ceiling: int = 50,
                    exact: list | None = None) -> tuple[list, str | None]:
    """純 kNN 検索（BM25 を混ぜない・fused_search の vector エンジン用）。`search()` は kNN＋BM25 のハイブリッド固定のため、engines=["vector"] 単独用に top-level `knn` のみのクエリを発行する。
    返値 `(hits, degrade_reason|None)`。hits は `_parse_hits` 形 `{doc_id, line, text, score, ext}`。
    degrade_reason: es_unavailable／embedding_not_configured／embedding_cloud_unavailable／vector_feature_mismatch／query_embed_failed／es_query_failed（一時的）／es_query_rejected（クエリ不備）（`parts/read/fused_search.py` の `DEGRADE_REASONS` と同一・増やすときは両方直す）。
    `embedding_cloud_unavailable`（明示選択したクラウドが解決できない）は、`embedding_not_configured`（クラウドを選んでいない通常の未設定）と区別する。
    `layer`（省略可）: `search()` と同じ探す対象フィルタ。`k_ceiling`（省略可・既定 50）: `k` の上限（`search()` と同じく、上限まで検証済みの呼び出し元が引き上げる）。`exact`（省略可）: `search()` と同じ識別子の完全一致フィルタ。
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
            and meta.get("embed_algo") == embeddings.EMBEDDING_INPUT_ALGORITHM_ID
            and meta.get("quant") == embeddings.ES_VECTOR_QUANT):
        return [], "vector_feature_mismatch"  # 索引素性ズレ
    qv = _embed_query(q, ec, world)
    qb = embeddings.vector_to_es_bytes(qv[0]) if qv else None
    if qb is None:
        return [], "query_embed_failed"
    flt = []
    sel = scope_mod.normalize_scope_paths(scope_paths)
    if sel:
        flt.append({"terms": {"scopes": sel}})  # `search()` と同一の範囲フィルタ
    lfilt = layer_mod.es_filter(layer)
    if lfilt:
        flt.append(lfilt)
    if exact:
        flt.append(_exact_filter(exact))
    # 上位ちょうど `k` 件を取ってから重要度で並べ替えると、「高」のヒットが取得時点で落選して浮上できない。有界の overfetch（`k*3`・上限 `k+50`）で多めに取り、補正・再ソートの後に `k` 件へ切る（重要度なしの資料フォルダは補正が no-op で結果不変）
    fetch_k = min(k * 3, k + 50)
    body = {"size": fetch_k,
            "knn": {"field": "embedding", "query_vector": qb, "k": fetch_k,
                    "num_candidates": max(50, fetch_k * 5), "filter": flt}}
    try:
        # 純 kNN は function_score で包めないため、取得後の再ランクで重要度ブーストを適用する
        hits = _rerank_knn_by_importance(_parse_hits(_req("POST", f"/{_index(world)}/_search", body), vector=True))
        return hits[:k], None
    except Exception as exc:
        return [], _classify_query_exception(exc)
