"""Elasticsearch 連携（共有KBのみ・world単位インデックス・日本語BM25=kuromoji）。

13-詳細設計引継ぎ.md: ES＝ベクトル＋BM25・**共有KBのみ**・個人文書は隔離（共有indexに書かない）。本モジュールは
まず **BM25（kuromoji 形態素）** を実装（ベクトルは埋め込み手段確定後に追加）。SDK 非依存＝REST(urllib)。
**ES 未起動でも落ちない**（best-effort・graceful）。索引対象は `corpus_docs.world_documents`（設計書/テキスト/
ソース＋Office派生MD）を行チャンク化。doc_id=rel_path、`scopes`（祖先フォルダ prefix 群）で範囲フィルタ。
常時、Office/PDF の `{rel}.rag.md`（Evidence IR 由来・RAG 正本＝D1）をアンカー分割した本文を
索引ソースにする（`{rel}.rag_chunks.jsonl` は citation/locator 等を運ぶ証跡サイドカー・
`rag_es_enabled`／`index_world`／`_validate_rag_chunks` 参照。グローバルな系統切替トグル
`SHERPA_SEARCH_RAG_ES` は TOGGLE-RM・2026-09-03 で撤去済み・rag_chunks 破損時の per-file
legacy 縮退は別契約として残る）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from . import corpus_docs, doc_text, embeddings, json_io, scope_infer, worlds
from . import layer as layer_mod
from . import scope as scope_mod
from .ingest import importance, text_kind
from .ingest.analyzers import registry as analyzer_registry

_log = logging.getLogger("sherpa")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析（`agentic_search._env_int` と同一セマンティクス）。

    `agentic_search` は本モジュールを import する側（`from . import ... es_index ...`）のため、
    ここで `agentic_search` を import すると循環 import になる。同じ検証ロジック（範囲外・非整数・
    負値は既定へ、既定値自体も [lo, hi] にクランプ）を独立実装する（`grep_tool._env_int` 等と同型）。
    """
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    """`_env_int` の float 版（同一セマンティクス）。範囲外・非数値は既定へ fail-safe。"""
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


# チャンク粒度（行・read_around と整合）。legacy チャンク経路（rag_chunks 未使用時）のみ効く。
# 値を変えると索引の中身が変わるため、`needs_reindex` が `_meta` の `chunk_lines` で drift を検知する。
_CHUNK_LINES = _env_int("SHERPA_ES_CHUNK_LINES", 40, 10, 400)
# 旧索引（`chunk_lines` フィールドが無い）は既定 40 として扱う（`needs_reindex` 参照）。
# `index_world` は既定値（40）のときは meta に `chunk_lines` を書かない（索引 meta を最小限に保つ）。
_CHUNK_LINES_DEFAULT = 40
# `search()` が ES へ送る size の上限。`SHERPA_GREP_MAX_HITS`（agentic_search.MAX_HITS と同じ env・
# `.env.example` の行はコメントアウト配布＝既定はコード側）と連動するが、50 未満には下げない
# （50 を超えて広げたときだけ効く・下限は常に 50）。agentic_search 側の既定30とは別の既定値で
# 同じ env を共用するため、この下限がないと env の値次第で ES 側の既定上限が後退しうる。
# 範囲 [1,1000] は ES の既定 max_result_window（10000）を十分下回る安全な値。
_ES_SEARCH_K_MAX = max(50, _env_int("SHERPA_GREP_MAX_HITS", 50, 1, 1000))
_TIMEOUT = 30
ES_MAPPING_VERSION = "7"                            # マッピング/チャンクメタの版。上げると次回 sync で全 world が自動 reindex（needs_reindex 参照）
# v5: `branch` フィールド追加（層フィルタ（`layer.es_filter`）を ext membership から
# classify_document 確定値（`branch=="source"`）へ切替・grep/agentic と揃える）。
# v6: rag チャンクの隣接キー（`previous_chunk_id`/`next_chunk_id`/`parent_id`/`logical_record_id`/
# `section_path`）を追加（B1・出所メタの passthrough。`parent_id` は `agentic_search` の親返し
# （L4c）が読む。文脈拡張として辿って連結する B2 は撤去済み＝2026-09-03・CLEAN-2 item f）。
# v7（I2・2026-09-05・重要度の経路別反映）: `importance`（keyword）/`importance_reason`
# （keyword・index:false＝表示専用で検索対象にしない）を追加。`_重要度.txt` の無い world の文書は
# どちらも索引に持たない（`_doc_chunk_bundle` の meta 組み立てが条件付きで焼き込む）——
# 検索クエリ側の重要度ブースト（`search()`/`search_knn_only()`）はこのフィールドへの `term` filter
# なので、フィールド自体が無い文書は一致せず boost が完全 no-op になる（受け入れ条件＝
# 重要度制御ファイルの無い world でスコア完全不変）。
# rag_chunks.jsonl 読み取りの安全弁（1文書分）。この用途の JSONL は通常でも1チャンクあたり
# 数百〜数千文字程度（`evidence_render.MAX_GROUP_CHARS`＝1800 が生成側のグループ分割閾値）に収まるため、
# 桁違いに超える入力は生成側の不具合・破損・攻撃的な入力とみなし、超過は個別行を切り詰めず
# ファイル全体を無効にして legacy チャンクへ縮退する（`_validate_rag_chunks` 参照）。**この安全弁は
# 1文書単位**——world 全体の bulk 送信量は `_ES_BULK_BATCH_MAX_DOCS`/`_ES_BULK_BATCH_MAX_BYTES`
# （バッチ分割）が別途境界を持つため、ここは「壊れた/巨大すぎる1文書を拾わない」判定に専念する。
_RAG_CHUNKS_FILE_CAP_BYTES = _env_int(                # 1ファイルの読み取り上限バイト（超過は無効）
    "SHERPA_ES_RAG_CHUNKS_FILE_CAP_BYTES", 32 * 1024 * 1024, 1024, 1024 * 1024 * 1024)
# 既定は 20000 から大幅に緩和（2026-09-02）: 旧既定はレコード単位チャンク（xlsx の行・docx の段落等）
# の正常なファイル1本（例: 2.5万行）すら拒否しうる、ファイル単位に誤って掛かった境界だった
# （world 全体の bulk 送信量はバッチ分割が別途守る＝ここを world 単位の上限として使う必要がなくなった）。
_RAG_CHUNKS_MAX_ROWS = _env_int(                      # 1ファイルが持てるチャンク行数の上限（超過は無効）
    "SHERPA_ES_RAG_CHUNKS_MAX_ROWS", 200000, 100, 2_000_000)
_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS = _env_int(          # 1チャンクの索引本文（rag.mdアンカー間）文字数上限（超過は無効）
    "SHERPA_ES_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS", 20000, 100, 1_000_000)
# bulk 送信のバッチ境界（2026-09-02・本レーン）。world 全体を1本の `_bulk` リクエストへ詰めていた
# 旧実装は、world のチャンク総数に比例してリクエストボディ・ES 側のバッチ処理メモリが際限なく
# 増える（大規模 world で実メモリを圧迫しうる）——チャンクサイズがばらつくため件数だけでは
# 有界にならず、件数とバイト数の両方で境界を決める（`_bulk_batches` 参照）。
_ES_BULK_BATCH_MAX_DOCS = _env_int("SHERPA_ES_BULK_BATCH_MAX_DOCS", 2000, 1, 100000)
_ES_BULK_BATCH_MAX_BYTES = _env_int(
    "SHERPA_ES_BULK_BATCH_MAX_BYTES", 8 * 1024 * 1024, 64 * 1024, 512 * 1024 * 1024)
# 埋め込みのフラッシュ単位（2026-09-03・EMBED-2、EMBED-3 で用途を拡張）: `_embed_cached()` 1回分の
# 不足分（`need`）を1本の `embeddings.embed()` 呼び出しへ渡すと、返り値の全ベクトル（各 1536 次元級）を
# 一括でメモリに保持することになり、大規模 world（実測: 15GB RAM 環境で 20万チャンク級）で OOM を
# 起こす。`need` をこの件数単位のバッチへ割り、バッチ成功ごとにシャード（`_embed_cache_dir`）へ
# **即時フラッシュ**することで、①1回に保持するベクトル量を有界化し、②プロセスが OOM/kill で
# 落ちてもフラッシュ済み分は次回 sync がキャッシュヒットで即スキップできる（＝再開性の本体・
# `_embed_cached` docstring 参照）。**EMBED-3（doc単位ストリーミング化）で `index_world()` 自身の
# doc グループ化バッチサイズにも兼用**する（`_doc_chunk_bundle`/`_flush_doc_group` 参照）——
# 「embed の HTTP バッチングは件数単位で有界」という既存契約と、「一度に保持するチャンク量」を
# 同じ1つの env で揃え、チューニングつまみを増やさない。件数のみ（`_ES_BULK_BATCH_MAX_DOCS` と
# 違いバイト数は見ない）＝チャンク本文の長さは埋め込み対象（自然文）でばらつきが小さく、
# 件数だけで十分実用的に有界。
_EMBED_FLUSH_CHUNKS = _env_int("SHERPA_EMBED_FLUSH_CHUNKS", 500, 1, 100000)
_embed_log = logging.getLogger("sherpa.embed")     # 埋め込み進捗はここへ（log_setup.py の embed.log 行き）

# 重要度スコアブースト（I2・J2・2026-09-05）: `高`/`低` それぞれの function_score 係数。
# 既定は控えめ（`高`=1.2倍・`低`=0.85倍）。範囲クランプ・env で調整可（`_env_float` 参照）。
# `中`/未設定はどちらの function にも一致せず等倍（1.0）のまま——`_重要度.txt` の無い world は
# 全文書がこの等倍のまま＝スコア完全不変（受け入れ条件）。
_ES_IMPORTANCE_BOOST_HIGH = _env_float("SHERPA_ES_IMPORTANCE_BOOST_HIGH", 1.2, 1.0, 5.0)
_ES_IMPORTANCE_BOOST_LOW = _env_float("SHERPA_ES_IMPORTANCE_BOOST_LOW", 0.85, 0.05, 1.0)


def rag_es_enabled() -> bool:
    """ES 索引ソースが rag チャンク（`{rel}.rag_chunks.jsonl`）を使うか。

    常時 True（TOGGLE-RM・2026-09-03: グローバルな系統切替トグル `SHERPA_SEARCH_RAG_ES` を撤去し
    常時ONへ固定）。既存の呼び出し形（`_search_chunk_mode`／`index_world`／`needs_reindex` 等）を
    変えない最小変更として関数自体は残す——`_search_chunk_mode()` は旧世代インデックス（`_meta` に
    `search_chunk_mode` フィールド自体が無い・古い legacy 索引）を検知して一度だけ reindex させる
    移行安全弁として引き続き機能する。rag_chunks 破損時の per-file legacy 縮退は別契約として残る。
    """
    return True


# `search()` の kNN＋BM25 ハイブリッドにおける BM25(keyword) 対 kNN(vector) の配分。
# 0.0＝vector 寄り〜1.0＝keyword 寄り。既定 0.5＝両者同着（boost キー自体を書かない＝無指定
# ハイブリッドと同じ本文になる）。他の env 駆動定数と同様、起動時（import 時）に1回だけ読む。
_HYBRID_WEIGHT = _env_float("SHERPA_ES_HYBRID_WEIGHT", 0.5, 0.0, 1.0)


def _search_chunk_mode() -> str:
    """現在の ES 索引ソース方針（`"rag"`／`"legacy"`）。`rag_es_enabled()` は常時 True のため通常は
    `"rag"` だが、`_meta` にこの署名を刻み続けることで `needs_reindex` が旧世代（`search_chunk_mode`
    フィールド自体が無い・legacy チャンクで張られた古い）索引を検知し一度だけ reindex させられる
    （索引の中身が変わる＝内容署名/マッピング版とは別の drift 源・TOGGLE-RM 後もこの移行安全弁は残す）。"""
    return "rag" if rag_es_enabled() else "legacy"


def _mapping(dim, analyzer: str, emeta=None) -> dict:
    """index マッピング。`analyzer`＝kuromoji/standard。`dim` で dense_vector(cosine)、`emeta` で埋め込み素性を _meta に記録。"""
    props = {
        "doc_id": {"type": "keyword"}, "ext": {"type": "keyword"},
        # `corpus_docs.classify_document` の確定判定（"source"=code／それ以外=docs・`layer.es_filter` 参照）。
        "branch": {"type": "keyword"},
        "top_scope": {"type": "keyword"},
        "scopes": {"type": "keyword"},              # 祖先フォルダ prefix 群（範囲フィルタ＝prefix 一致）
        "line": {"type": "integer"},
        "text": {"type": "text", "analyzer": analyzer},
        # 抽出来歴（office_md の meta.json 由来・表示のみ／検索スコアには反映しない）。派生MD 文書のみ付く。
        "extraction_method": {"type": "keyword"},   # ooxml / pdf_text / vision
        "confidence": {"type": "float"},            # アームの確信度（0.0〜1.0）
        "has_conflicts": {"type": "boolean"},       # 決定的マージで conflicts が出た文書か
        # rag_chunks 由来チャンクのみ持つ（§ index_world）。chunk_id は生成側の record 単位キー（検索結果へ
        # passthrough する参照用フィールド。ES の `_id` 自体は別に doc_id と束ねて名前空間化する＝
        # `_rag_chunk_es_id` 参照）。locator は citation 由来の原本位置。Office/PDF ごとに locator の形が
        # 変わり dynamic mapping が型衝突しうるため enabled:false にする（_source には残る＝素の JSON として
        # 取得はできる。検索/絞り込みの対象にはしない）。
        "chunk_id": {"type": "keyword"},
        "locator": {"type": "object", "enabled": False},
        # B1: 隣接キー（v6・rag チャンクのみ持つ）。前後チャンク（`previous_chunk_id`/`next_chunk_id`）・
        # 所属領域（`parent_id`＝context-region・`agentic_search` の親返し L4c が読む）・レコード単位の
        # 識別子（`logical_record_id`）・見出し経路（`section_path`・配列）を持つ。全て keyword
        # （フィルタ/集約用途で十分・全文検索対象ではない）。文脈拡張として辿って連結する B2 は撤去済み
        # （2026-09-03・CLEAN-2 item f）。
        "previous_chunk_id": {"type": "keyword"},
        "next_chunk_id": {"type": "keyword"},
        "parent_id": {"type": "keyword"},
        "logical_record_id": {"type": "keyword"},
        "section_path": {"type": "keyword"},
        # I2（v7）: 登録者が `_重要度.txt` で付けた文書の重要度（`ingest.importance`）。`importance`
        # はスコアブースト（function_score の term filter）とフィルタ/表示の両方に使うため keyword。
        # `importance_reason` は表示専用（`index: False`＝検索対象にしない・理由文の全文検索は不要）。
        "importance": {"type": "keyword"},
        "importance_reason": {"type": "keyword", "index": False},
    }
    if dim:
        props["embedding"] = {"type": "dense_vector", "dims": dim, "index": True, "similarity": "cosine"}
    m = {"mappings": {"properties": props}}
    if emeta:
        m["mappings"]["_meta"] = emeta              # {embed_provider, embed_model, dim}（検索時の素性照合用）
    return m


def _index_meta(world: str) -> dict:
    """index に記録した埋め込み素性（_meta）。無ければ {}。"""
    try:
        r = _req("GET", f"/{_index(world)}/_mapping")
        for v in r.values():
            return (v.get("mappings") or {}).get("_meta") or {}
    except Exception:
        return {}
    return {}


def _settings(s):
    if s is not None:
        return s
    try:
        from . import store
        return store.get_settings()
    except Exception:
        return {}


def _embed_system_settings_snapshot() -> dict | None:
    """`embeddings.cfg()`/`cloud_selected_but_unavailable()` へ渡す system_settings スナップ
    ショットを1回だけ読む（同じ判定に使い回す・RV2 参照）——ただし `SHERPA_DISABLE_EMBED`
    （テスト用 kill-switch）が有効な間は読まずに `None` を返す（RV3・FBK-1・2026-09-01）。
    両 helper とも kill-switch を system_settings 参照より先にチェックして即座に返す契約
    （`embeddings.py` 参照）のため、`None` を渡しても実害は無い一方、ここで無条件に読むと
    kill-switch が有効なテスト環境でも設定 DB 障害でこの読み取り自体が例外を出してしまい、
    「実埋め込み API を叩かない」つもりの kill-switch を回避しきれていなかった。
    """
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return None
    from . import store as _store
    return _store.get_system_settings()


def _url() -> str:
    # ES_URL の明示は別ホスト向けの上書き手段として最優先。無ければ compose の公開ポート変数
    # SHERPA_ES_PORT（docker-compose.yml と共用の 1 変数）に追随する＝ポートを 2 か所で対にしなくてよい。
    url = os.environ.get("ES_URL")
    if not url:
        url = f"http://localhost:{os.environ.get('SHERPA_ES_PORT') or '9200'}"
    return url.rstrip("/")


def _index(world: str) -> str:
    """ES index 名（必ず小文字＝ES 規約）。world_id の**大小文字違いで衝突しない**よう厳密ハッシュを付す（RV High）。"""
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


# 既定1秒・env で上書き可（閉域の遅い ES で誤不達判定になる環境向けの逃し弁・health と同型）。
_AVAILABLE_TIMEOUT = float(os.environ.get("SHERPA_ES_AVAILABLE_TIMEOUT", "1"))


def available() -> bool:
    """ES に到達できるか（未起動なら False＝各処理は no-op/空に）。

    timeout は短め（既定1秒・`SHERPA_ES_AVAILABLE_TIMEOUT` で上書き可）にする（不達時にこの
    呼び出しを待つ全経路——`agentic_search.tool_availability` の single-flight lock 内含む——の
    待ち時間上限になるため。閉域の遅い環境で誤って不達判定（SC-6e の 422・索引の no-op）に
    倒れる場合は env で引き上げる。健全時の挙動には影響しない）。
    """
    try:
        _req("GET", "/", timeout=_AVAILABLE_TIMEOUT)
        return True
    except Exception:
        return False


def _scopes(rel: str) -> list:
    return scope_infer.ancestor_scopes(rel)   # 祖先 prefix（導出は scope_infer に集約・rv-full B3）


def _provenance_meta(d: dict) -> dict:
    """派生MD の来歴サイドカー（`office_md` が書く `{md}.meta.json`）から**検索表示用**のチャンクメタを取り出す。

    返値（あれば）: `extraction_method`（アーム method）・`confidence`（0.0〜1.0）・`has_conflicts`（決定的
    マージで conflicts が出たか＝A4 が走った文書のみ）。**無ければ省略**（キーを立てない）。ソース文書
    （`md_path` 無し）や meta.json 欠落は `{}`＝後方互換（従来どおりのチャンク）。検索スコアには反映しない
    （表示のみ・起案 A4）。best-effort（読取失敗・型不正は無視して `{}`）。
    """
    mp = d.get("md_path")
    if not mp:
        return {}
    raw = json_io.read_json(Path(str(mp) + ".meta.json"))    # 無い/壊れは None
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    method = raw.get("method")
    if isinstance(method, str) and method:
        out["extraction_method"] = method
    conf = raw.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        out["confidence"] = float(conf)
    if "conflicts" in raw:                                   # A4 マージが走った文書だけ has_conflicts を立てる
        out["has_conflicts"] = bool(raw.get("conflicts"))
    return out


def _arms_config_sig() -> str | None:
    """今の office_md アーム構成署名（`office_md._current_arms_sig()`）。取得失敗は None（fail-safe）。

    `needs_reindex` の drift 判定に使う（RV Med）: アーム構成（例 OCR 有効/無効）が変わると派生MD の中身
    （画像 first-class 化の有無・extraction_method 等）が変わりうるが、ソースファイル自体の
    `content_sig`（rel/mtime/ctime/size のみ）は変化しないため、この署名が無いと `needs_reindex` の
    「unchanged」判定（`sync()` の no-op 分岐）が ES 索引の古い中身を見逃す。
    """
    try:
        from .ingest import office_md
        return office_md._current_arms_sig()
    except Exception:
        return None


def _analyzer_config_sig() -> str:
    """コード解析アナライザの有効構成署名（`analyzer_registry.config_signature()`）を文字列化。

    `needs_reindex` の drift 判定に使う: アナライザ構成（登録順・拡張子集合・分類契約版）が
    変わると、既存文書の確定判定（`corpus_docs.classify_document` の `kind`＝ES に保存する
    `branch`）が変わりうる。`ingest/worker.py::_sig()` が同じ `config_signature()` を **world
    署名（`content_sig`）の材料にも**畳み込んでいるため、通常の sync 経路では `content_sig` の
    不一致だけで drift を検知できる（`_arms_config_sig` の office アーム構成とは異なり、こちらは
    `content_sig` からも独立ではない）。この比較は、ES 自身の索引メタが何らかの理由で
    `content_sig` と揃わないまま `branch` だけ古くなった状態（例: 前回の reindex が構成変更の
    途中で失敗し ES 索引だけが取り残された場合）を、`content_sig` 比較に頼らず独立に検知する
    多層防御。`config_signature()` はタプルを返すが、ES `_meta` は JSON 往復でタプル→配列になり
    素の比較が食い違うため、`repr()` で文字列へ固定してから保存/比較する。
    """
    return repr(analyzer_registry.config_signature())


# pending（ES がまだ現行版へ追随できていない）を表す明示センチネル。meta 側のフィールド欠落/
# 旧索引（None・null）と衝突しないための専用の値——`None` を pending の意味で使うと、meta に
# 該当フィールドが無い場合の `None` と区別が付かず `needs_reindex` が「一致（不要）」と誤判定する。
_HUMAN_MD_PENDING_SENTINEL = "pending"


def _human_md_config_sig(world: str) -> str | None:
    """人間向け MD（`human_md`）レンダラ/抽出器の**今の版**（world がその版まで ES 索引に
    反映できているかの判定込み）。

    **RAG_ES の ON/OFF に関わらず常に評価する**: `rag_chunks` が無効/劣化（symlink・破損・上限超過等）
    な文書は40行チャンクへ縮退する（`index_world` の `rag_result is None` フォールバック参照）。
    縮退先の実体は `md_path`＝`corpus_docs.iter_world_documents(include_rag=True)` が返すファイルで、
    **rag.md があればそれ・無ければ legacy `{rel}.md`（`human_md` 生成）**（2026-09-02 §8 D1 の
    「grep/ES/グラフが同じ物理ファイルを見る」に従う）。したがって human_md 版は RAG_ES=OFF の索引本体と、
    ON でも rag.md を持たない文書の縮退先に効き続ける——RAG_ES=ON だからといって無条件に無視してはいけない。

    **pending（世界単位のホールドバック）はセンチネル文字列を返す**: 次の2条件のどちらかが
    True の間は、現行版ではなく `_HUMAN_MD_PENDING_SENTINEL`（"pending"）を返し続ける。
    (a) `office_md.human_md_sig_drift`＝render 側（rel 単位の `asset_versions.human_md`）が
    まだ現行版に追随できていない rel が残っている。
    (b) `office_md.human_md_es_sig_drift`＝ES 自身がまだこの版までの bulk 成功を確定できて
    いない（`.human_md_es_sig` マーカー・RAG-KV の `.rag_sig` と同型のホールドバック方式）。
    `index_world` はクリーン再索引（delete→create→bulk）で、索引作成は bulk の成否が判明する
    前に行われる——bulk が部分失敗しても index 自体は作られてしまうため、bulk の成否を
    確認できる呼び出し元（`worker`）が成功を確認した後にだけ `office_md.confirm_human_md_es_sig`
    でマーカーを確定する。pending 中にセンチネルではなく `None` を返すと、meta にまだ
    `human_md_sig` フィールドが無い（旧索引・None）ケースと区別が付かず `needs_reindex` が
    「一致（不要）」と誤判定してしまう。**ES meta 自体には pending 状態を書かない**——
    `index_world` はこの関数がセンチネルを返した場合、meta の `human_md_sig` を `None` にする
    （「成功して確定した版」だけを meta に書く契約）。

    既知のスコープ限定: `index_world` はクリーン再索引のみを提供し、世界単位より細かい
    per-document reindex は現行アーキテクチャに無い。この署名も世界単位（world 内のどれか
    1文書の human_md 版が変われば world 全体が対象になる）で、「当該文書だけ再索引」という
    理想の粒度ではない（受容）。取得失敗（例外）は pending ではなく fail-safe の `None`
    （`_arms_config_sig` と同じ扱い＝原因不明のエラーで無闇に reindex ループを起こさない）。
    """
    try:
        from .ingest import office_md
        wd = worlds.world_dir(world)
        dmd = worlds.derived_md_dir(world)
        if wd and dmd.exists() and (
                office_md.human_md_sig_drift(wd, dmd) or office_md.human_md_es_sig_drift(dmd)):
            return _HUMAN_MD_PENDING_SENTINEL
        return office_md._current_human_md_sig()
    except Exception:
        return None


def confirm_human_md_meta(world: str) -> bool:
    """既存索引の `_meta.human_md_sig` を確定値へ書き直す（索引の再作成・bulk の再実行は不要・
    Put Mapping API で `_meta` だけを更新する）。

    `index_world()` の `ensure_index()`（bulk 実行**前**）は、この呼び出し時点で human_md が
    まだ pending（`.human_md_es_sig` マーカー未確定）なら meta に `None` を書く（pending
    センチネルをそのまま書かない契約・`_human_md_config_sig` docstring 参照）。その後 bulk が
    成功し、呼び出し元（`worker.index_world_with_human_md_holdback`）が
    `office_md.confirm_human_md_es_sig()` でマーカーを確定できたら、ここで meta の値**も**
    現行署名へ書き直す——書き直さないと meta は `None` のまま残り、マーカーは確定済み
    （`_human_md_config_sig` はもう pending を返さない）なのに meta だけが古いという不整合になり、
    次回 `needs_reindex()` が「None ≠ 現行版」を検知して**無駄な再索引を毎 sync 繰り返し続ける**
    （収束しない）。

    既存の `_meta`（`world_id`/`mapping_version`/`arms_sig`/`content_sig` 等）は保持したまま
    `human_md_sig` フィールドだけを上書きする（Put Mapping API の `_meta` は丸ごと置換のため、
    既存値を読み直してから書き戻す——ES 側の部分マージ挙動に依存しない）。索引が無い/到達不可・
    まだ pending（呼び出し元の前提が崩れている防御）は False。
    """
    sig = _human_md_config_sig(world)
    if sig == _HUMAN_MD_PENDING_SENTINEL:
        return False
    try:
        meta = dict(_index_meta(world))
        meta["human_md_sig"] = sig
        _req("PUT", f"/{_index(world)}/_mapping", {"_meta": meta})
        return True
    except Exception:
        return False


def delete_world(world: str) -> bool:
    """world のインデックスを削除（無ければ無視）。wipe の派生物伝播で呼ぶ。"""
    try:
        _req("DELETE", "/" + _index(world))
        return True
    except urllib.error.HTTPError as e:
        return e.code == 404                         # 既に無い＝成功扱い
    except Exception:
        return False


def _confirm_content_sig(world: str, content_sig) -> None:
    """bulk が**全バッチ成功した後**に `_meta.content_sig` を書く（`confirm_human_md_meta` と同じ
    Put Mapping API で `_meta` だけ更新する）。

    先に書かない理由は `index_world` の該当箇所を参照（途中でプロセスが落ちたときに中途半端な索引が
    居座るのを防ぐ）。この書き込み自体が失敗した場合は content_sig が無いまま完全な索引が残る＝
    次回 sync が1回だけ無駄に張り直す（安全側の失敗＝取りこぼしは生まない）。
    """
    if not content_sig:
        return
    try:
        meta = dict(_index_meta(world))
        meta["content_sig"] = content_sig
        _req("PUT", f"/{_index(world)}/_mapping", {"_meta": meta})
    except Exception:
        _log.warning("es_index: content_sig の確定に失敗しました（次回 sync が1回だけ張り直す）: world=%s", world)


def _wipe_after_bulk_failure(world: str) -> None:
    """bulk 途中失敗時に world を空へ戻す（案a＝全部か無しか）。**wipe 自体が失敗しても
    次回 sync が必ず張り直せる状態にする**のが本関数の契約。

    `ensure_index()` は bulk の**前**に `_meta.content_sig` を書く。したがって wipe が失敗すると
    「一部だけ入った索引＋有効な content_sig」が残り、`needs_reindex()` が False を返して
    **その中途半端な索引が居座る**——利用者から見て「検索したのに出てこない」という、本プロジェクトが
    一貫して避けているサイレントな取りこぼしそのものになる。

    そこで wipe に失敗したら `_meta.content_sig` を落として fail-closed にする（`confirm_human_md_meta`
    と同じ Put Mapping API で `_meta` だけ書き換える）。content_sig が無い索引は `needs_reindex()` の
    `meta.get("content_sig") != content_sig` で必ず不一致になり、次回 sync が張り直す。
    meta の書き換えにも失敗する（＝ES 自体が落ちている）場合は、そもそも次回 sync の `available()` が
    False になり索引は使われない。
    """
    if delete_world(world):
        return
    try:
        meta = dict(_index_meta(world))
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
    except urllib.error.HTTPError as e:
        return [] if e.code == 404 else []           # 該当なし=404 も空
    except Exception:
        return []


def reconcile(valid_worlds) -> list:
    """**孤児 ES 索引の自動掃除**: `sherpa-kb-*` のうち、登録 world の現行索引名いずれにも一致しないものを削除。

    `valid_worlds`＝**確実に取得できた**登録 world id 集合（呼出側が fail-safe を担保＝不確実なら呼ばない）。
    対象は `sherpa-kb-` 接頭辞のみ・名前一致で判定（命名規則が将来変わっても現行名集合に無ければ孤児＝消える）。
    返り値＝削除した索引名。ES 不可/個別失敗は握って続行（best-effort）。
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
            pass                                      # 個別失敗は次回リコンサイルで再試行
    return deleted


def ensure_index(world: str, dim=None, emeta=None) -> bool:
    """index を作成（既存なら True）。アナライザ不明(400)は standard fallback で再試行。`dim`/`emeta` で kNN 用。"""
    idx = "/" + _index(world)
    for analyzer in ("kuromoji", "standard"):
        try:
            _req("PUT", idx, _mapping(dim, analyzer, emeta))
            return True
        except urllib.error.HTTPError as e:
            try:
                txt = e.read().decode("utf-8", "replace")
            except Exception:
                txt = ""
            if "resource_already_exists" in txt:     # 既存（delete 後は通常起きない）
                return True
            if e.code == 400 and analyzer == "kuromoji":
                continue                             # kuromoji 不明 → standard で再試行
            return False                             # それ以外の 400/エラーは失敗
        except Exception:
            return False
    return False


# 埋め込みキャッシュのシャーディング（2026-09-03・EMBED-3）: 旧形式（`embed_cache.json` 単一ファイル・
# EMBED-2 以前）は world 全体のベクトルを1つの dict へ読み込む設計そのものが、大規模 world
# （実測: 15GB RAM 環境で 20万チャンク級）で OOM の主因だった——Python の float リストはベクトル1本
# あたり dense_vector の理論サイズよりはるかに大きいオブジェクト表現になる。キー（SHA1）の先頭
# `_EMBED_CACHE_SHARD_HEX` 桁でシャーディングし、常に**シャード単位**（world 全体ではなく）で読み書き
# する——ある瞬間に保持するベクトル量が世界規模ではなく「1シャード＋1フラッシュバッチ」に
# 有界化される。旧単一ファイルはもう読まない（キー体系は同じ＝miss 扱いで自然に再 embed される・
# 実害なし・移行コード不要）。
_EMBED_CACHE_SHARD_HEX = 2   # 16^2=256 シャード


def _embed_cache_dir(world: str) -> Path:
    """world の埋め込みキャッシュ（シャード群）ディレクトリ。

    EMBED-3 以前の旧・単一 JSON キャッシュ（`embed_cache.json`・実測で数GB級になり得る）は
    シャード化以降どこからも読まれない＝残しても機能影響ゼロだが、**誰も消さないと閉域機の
    ディスクを恒久占有する**（2026-09-05 横並び精査 R2）。ここで見つけ次第1回だけ削除する
    （失敗は無視＝掃除はベストエフォート）。"""
    d = worlds.semantic_dir(world) / "embed_cache"
    legacy = d.parent / "embed_cache.json"
    if legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass
    return d


def _embed_cache_shard_path(world: str, shard_id: str) -> Path:
    return _embed_cache_dir(world) / f"{shard_id}.json"


def _read_embed_cache_shard(world: str, shard_id: str) -> dict:
    raw = json_io.read_json(_embed_cache_shard_path(world, shard_id))    # 無い/壊れは None
    v = raw.get("vectors") if isinstance(raw, dict) else None
    return v if isinstance(v, dict) else {}


def _write_embed_cache_shard(world: str, shard_id: str, vectors: dict) -> None:
    p = _embed_cache_shard_path(world, shard_id)
    if not vectors:                                    # このシャードに現存キーが無い＝ファイル自体を消す（鏡）
        try:
            p.unlink()
        except OSError:
            pass
        return
    try:
        json_io.write_json_atomic(p, {"vectors": vectors})
    except OSError:
        pass


def _embed_cache_lookup_batch(world: str, keys: list, dim: int) -> dict:
    """`keys` のうちキャッシュ済み・形状検証OK（list かつ次元一致・壊れ/型ズレは miss 扱い）のものだけ
    返す。関与するシャードだけを1つずつ読み（world 全体を一括ロードしない＝有界）、読み終えた
    シャードの dict はその場で手放す。"""
    by_shard: dict = {}
    for k in keys:
        by_shard.setdefault(k[:_EMBED_CACHE_SHARD_HEX], []).append(k)
    out: dict = {}
    for shard_id, shard_keys in by_shard.items():
        cache = _read_embed_cache_shard(world, shard_id)
        for k in shard_keys:
            v = cache.get(k)
            if isinstance(v, list) and len(v) == dim:
                out[k] = v
    return out


def _embed_cache_write_batch(world: str, new_vectors: dict) -> None:
    """新規ベクトルをシャードへマージ書き込みする（シャード単位の read-modify-write・
    有界＝1回に保持するのは1シャード分＋この呼び出しの新規分だけ）。"""
    by_shard: dict = {}
    for k, v in new_vectors.items():
        by_shard.setdefault(k[:_EMBED_CACHE_SHARD_HEX], {})[k] = v
    for shard_id, updates in by_shard.items():
        cache = _read_embed_cache_shard(world, shard_id)
        cache.update(updates)
        _write_embed_cache_shard(world, shard_id, cache)


def _delete_embed_cache(world: str) -> None:
    """埋め込み無効/現存チャンク無し時のキャッシュ全消去（削除残骸を残さない・鏡）。"""
    shutil.rmtree(_embed_cache_dir(world), ignore_errors=True)


def _prune_embed_cache(world: str, valid_keys: set) -> None:
    """**world 全体の doc ストリームを一巡し、全チャンクの embed が完了した直後にだけ**呼ぶ最終剪定:
    現存キー（`valid_keys`）以外を消す（鏡・サイズ有界・削除/プロバイダ変更で旧エントリ消滅）。

    シャードを1つずつ処理する（world 全体を一括ロードしない＝EMBED-3 の主眼）。`valid_keys` が
    空なら（対象チャンクが無い world）ディレクトリごと削除する（`_delete_embed_cache` と同じ・
    RV Med 相当）。`index_world()` から**1回だけ**呼ぶ契約——`_embed_cached()` は EMBED-3 で
    複数回（doc グループごと）呼ばれるため、呼ぶたびに剪定すると前のグループ分を消してしまう。
    """
    if not valid_keys:
        _delete_embed_cache(world)
        return
    d = _embed_cache_dir(world)
    if not d.is_dir():
        return
    valid_by_shard: dict = {}
    for k in valid_keys:
        valid_by_shard.setdefault(k[:_EMBED_CACHE_SHARD_HEX], set()).add(k)
    try:
        shard_files = list(d.glob("*.json"))
    except OSError:
        return
    for shard_file in shard_files:
        shard_id = shard_file.stem
        keep = valid_by_shard.get(shard_id) or set()
        if not keep:
            try:
                shard_file.unlink()
            except OSError:
                pass
            continue
        cache = _read_embed_cache_shard(world, shard_id)
        _write_embed_cache_shard(world, shard_id, {k: v for k, v in cache.items() if k in keep})


def _chunk_key(ec: dict, text: str) -> str:
    """埋め込みキャッシュのキー＝(プロバイダ|モデル|次元|本文) の SHA1。**素性が変われば別キー**＝自動で再 embed。"""
    return hashlib.sha1(f"{ec['provider']}|{ec['model']}|{ec['dim']}|{text}".encode("utf-8")).hexdigest()


def _embed_cached(world: str, texts: list, ec) -> tuple:
    """チャンク埋め込みを**内容ハッシュでキャッシュ**し、未変更チャンクの再 embed（＝API コスト）を省く。

    返値 `(vectors|None, reused, embedded)`。embed 失敗/次元不一致は `(None,0,0)`＝呼び出し元は
    BM25 のみへ降格し、**既存キャッシュは壊さない**（このバッチより前に成功したフラッシュ分は
    そのまま残る・後述）。重複チャンクは1回だけ embed。キャッシュ済ベクトルも形状（list かつ
    次元一致）を検証し、壊れ/型ズレは miss 扱いで再 embed する（毒ベクトルを使わない）。

    **`texts` は呼び出し元が有界なバッチへ分けて渡す契約**（2026-09-03・EMBED-3・doc単位
    ストリーミング化）: `index_world()` はもう world 全体の texts を1回では渡さない——doc 単位で
    `_EMBED_FLUSH_CHUNKS` 件程度ずつ束ねて本関数を**複数回**呼ぶ（`index_world` docstring 参照）。
    そのため本関数は**このバッチのキーだけを ADD/UPDATE するのみ**——旧版（EMBED-2 以前）にあった
    「現存チャンクだけへ剪定する」最終仕上げはここでは行わない（複数回呼ばれるため、呼ぶたびに
    剪定すると前のバッチの分を消してしまう）。剪定は `_prune_embed_cache()` を、world 全体の doc
    ストリームを一巡し終えた**呼び出し元側で1回だけ**呼ぶ契約に切り出した。埋め込み無効
    （`ec` が None）/このバッチが空（`texts` が空）は `(None,0,0)`（キャッシュには一切触れない・
    削除も呼び出し元の責務——`_delete_embed_cache`/`_prune_embed_cache` 参照）。

    キャッシュ本体はシャード化（`_embed_cache_lookup_batch`/`_embed_cache_write_batch`）——
    world 全体のベクトルを1つの dict へロードしない（EMBED-3 の主眼＝メモリ有界化）。
    不足分（`need`）はさらに `_EMBED_FLUSH_CHUNKS` 件単位のバッチへ分割し、バッチが成功するたびに
    シャードへ即座にフラッシュする（EMBED-2 由来の再開性の本体：後続バッチの失敗やプロセス自体の
    OOM/kill で今回の呼び出しが完走できなくても、フラッシュ済み分は次回呼び出しでキャッシュヒットし
    即スキップできる＝再実行が0から始まらない）。
    """
    if not ec or not texts:
        return None, 0, 0
    dim = ec["dim"]
    keys = [_chunk_key(ec, t) for t in texts]
    key_text = {}                                     # distinct チャンク（重複本文は1回だけ embed）
    for k, t in zip(keys, texts):
        key_text.setdefault(k, t)
    filled = _embed_cache_lookup_batch(world, list(key_text), dim)   # 形状検証済みの既存ベクトルだけ再利用
    need = [k for k in key_text if k not in filled]
    if need:
        total = len(need)
        n_batches = (total + _EMBED_FLUSH_CHUNKS - 1) // _EMBED_FLUSH_CHUNKS
        for bi, start in enumerate(range(0, total, _EMBED_FLUSH_CHUNKS)):
            batch_keys = need[start:start + _EMBED_FLUSH_CHUNKS]
            new = embeddings.embed([key_text[k] for k in batch_keys], ec, world=world)
            if not new:                                # 失敗/次元不一致＝BM25 のみ（ここまでの flush 済み分は温存）
                return None, 0, 0
            new_map = {}
            for k, vec in zip(batch_keys, new):
                filled[k] = vec
                new_map[k] = vec
            _embed_cache_write_batch(world, new_map)   # 成功したバッチだけ即座に永続化（シャード単位・アトミック）
            if bi == 0 or bi == n_batches - 1 or (bi + 1) % 10 == 0:   # 間引いて進捗を残す（無言の長時間実行を無くす）
                _embed_log.info("es_index: embed 進捗 %d/%d チャンク（world=%s）",
                                 min(start + len(batch_keys), total), total, world)
    return [filled[k] for k in keys], len(key_text) - len(need), len(need)


def _rag_chunk_source_exts() -> frozenset:
    """rag_chunks.jsonl を持ちうる拡張子（Office/PDF のみ）。`office_md.OFFICE_EXT` を単一の真実源に
    する（ソース/テキスト文書に同名の sidecar が存在しても、拡張子の時点で対象外にし、
    stale/別文書の sidecar を rag チャンク源として取り違えない）。
    """
    from .ingest import office_md
    return office_md.OFFICE_EXT


def _rag_chunk_es_id(doc_id: str, chunk_id: str) -> str:
    """rag チャンクの ES `_id`。生成側の `chunk_id` は理論上 world 内で一意な設計だが、複製文書・
    stale sidecar・生成側の不具合があっても異なる文書間で無警告に上書きし合わないよう、`doc_id` を
    束ねた決定的ハッシュで名前空間化する（`chunk_id` 自体は検索結果へ passthrough する別フィールドと
    してそのまま保持＝参照側の契約は変えない）。
    """
    return "ragchunk:" + hashlib.sha1(f"{doc_id}\x00{chunk_id}".encode("utf-8")).hexdigest()


def _safe_rag_chunks_path(derived: Path, rel: str) -> tuple:
    """`{rel}.rag_chunks.jsonl` の安全な読み取りパスを検証する。

    返値 `(path, reason)`。`(path, None)`＝読んでよい。`(None, None)`＝そもそも存在しない
    （旧 world の未再sync等・通常の縮退＝報告不要）。`(None, reason)`＝存在するが安全に読めない
    （symlink・derived root 外への脱出等・報告対象）。symlink は resolve 前に拒否し、resolve 後は
    `derived`（信頼済みルート）配下に収まることを確認する。
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
    """`{rel}.rag.md`（D1のRAG正本）の安全な読み取りパスを検証する。`_safe_rag_chunks_path`と
    同じ契約（symlink拒否・derived配下への閉じ込め）。返値の意味も同じ:
    `(path, None)`＝読んでよい、`(None, None)`＝存在しない、`(None, reason)`＝存在するが読めない。
    """
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
    """rag.mdをアンカー（`<!-- chunk:{chunk_id} -->`）で分割し、`{chunk_id: 本文}`を返す。

    アンカーが1つも無い（旧形式・アンカー無しrag.mdのまま新コードが読んだ場合）は
    `({}, "rag_md_no_anchors")`＝呼び出し側は文書全体を無効として legacy 40行チャンクへ安全に
    縮退する。アンカーの重複は `({}, "rag_md_duplicate_anchor")`。本文はアンカー行の直後から
    次のアンカー（無ければファイル末尾）までを`strip()`したもの。
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
    """1行が rag.md のチャンクアンカー（`<!-- chunk:{chunk_id} -->`）なら、その chunk_id を返す
    （でなければ None）。`_parse_rag_md_chunks`（全文一括版）と同じアンカー形式を、行単位の
    ストリーミング走査向けに公開する——`agentic_search` の親返し（L4c・P2）が rag.md を
    `_CappedStreamReader` で1行ずつ読みながらアンカーを検出するのに使う。フォーマット定義
    （`<!-- chunk:{chunk_id} -->`）を二重管理しない。
    """
    m = _RAG_MD_CHUNK_ANCHOR_LINE_RE.match(line)
    return m.group(1) if m else None


def chunk_ids_for_parent(world: str, doc_id: str, parent_ids: list, *, limit: int = 5000) -> list:
    """`doc_id` の rag チャンクのうち `parent_id` が `parent_ids` のいずれかに一致する `chunk_id` を
    ES から取得する（親返し・P2＝「ヒットが属する領域」の全チャンク集合・L4c）。

    ES 不達／クエリ失敗は空リスト（best-effort・呼び出し元は少なくともヒット自身の chunk_id を
    ユニオンに含めるため、この関数が空でも P2 が完全に不能にはならない）。
    """
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
    """rag チャンクの代表 locator（先頭 citation の locator）。無ければ None（キーを立てない）。"""
    citations = chunk.get("citations")
    if not isinstance(citations, list) or not citations:
        return None
    first = citations[0]
    locator = first.get("locator") if isinstance(first, dict) else None
    return locator if isinstance(locator, dict) else None


_CHUNK_CONTEXT_STR_KEYS = ("previous_chunk_id", "next_chunk_id", "parent_id", "logical_record_id")


def _chunk_context_meta(chunk: dict) -> dict:
    """rag チャンクの隣接キー（B1・`_mapping` の v6 追加分）。`_chunk_locator` と同じ流儀で、
    欠落・型不正（生成側の不具合等）はキーを立てないだけにする——`_validate_rag_chunks` の
    無効化条件には含めない（このメタが無くても索引・検索自体は従来どおり成立する）。
    """
    out: dict = {}
    for key in _CHUNK_CONTEXT_STR_KEYS:
        v = chunk.get(key)
        if isinstance(v, str) and v:
            out[key] = v
    sp = chunk.get("section_path")
    if isinstance(sp, list) and sp and all(isinstance(x, str) and x for x in sp):
        out["section_path"] = sp
    return out


def _validate_rag_chunks(rag_path: Path, md_path: Path | None, rel: str, base_meta: dict) -> tuple:
    """`{rel}.rag_chunks.jsonl`（証跡サイドカー）＋`{rel}.rag.md`（D1のRAG正本）を突き合わせて検証し、
    ES bulk 用の `(ids, bodies, texts, reason)` を返す。

    `reason` が None なら使ってよい（`ids` が空＝チャンク0件も正常系）。`reason` が付く＝**ファイル全体**を
    無効とみなし、呼び出し側は40行チャンクへ縮退する（1行だけ捨てて残りを使う部分採用はしない＝
    部分破損の黙認防止）。無効になる条件（`reason` の語彙）:

    - `rag_md_missing`: jsonl はあるのに対になる rag.md が無い（不整合な派生状態）。
    - `rag_md_no_anchors`: rag.md にアンカー（`<!-- chunk:{chunk_id} -->`）が1つも無い——
      **D1 以前（v1alpha8以下）の旧形式 rag.md をそのまま読んだ場合を含む**。jsonl 側が旧形式の
      `search_text` フィールドを持っていても関与しない（もう読まない）ため、安全に legacy 40行
      チャンクへ縮退する。
    - `rag_md_duplicate_anchor`: rag.md 内で同じ chunk_id のアンカーが複数ある。
    - `rag_md_anchor_missing`: jsonl の chunk_id に対応するアンカーが rag.md に無い。
    - `rag_md_anchor_surplus`: rag.md に、jsonl のどの chunk_id とも対応しないアンカーが余っている。
    - `invalid_utf8`/`invalid_json`/`row_not_object`: 非空行が UTF-8 として不正、または JSON として
      壊れている・dict でない。
    - `missing_chunk_id`: 必須フィールドが欠落/空。
    - `source_rel_path_mismatch`: 行の `source_rel_path` が呼び出し元の `rel`（原本）と食い違う
      （stale/別文書の sidecar を取り違えて索引しない）。
    - `duplicate_chunk_id`: 同一ファイル内で `chunk_id` が重複（`_rag_chunk_es_id` の名前空間化とは別に、
      同一文書内の重複はここで弾く）。
    - `file_too_large`/`too_many_rows`/`search_text_too_long`: `_RAG_CHUNKS_FILE_CAP_BYTES`/
      `_RAG_CHUNKS_MAX_ROWS`/`_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS` 超過。
    - `rag_md_too_large`/`rag_md_stat_failed`/`rag_md_invalid_utf8`/`rag_md_read_failed`:
      rag.md 側の読み取り失敗（`_RAG_CHUNKS_FILE_CAP_BYTES` 超過含む・jsonl と同じ上限を流用する）。

    jsonl はファイル全体を `Path.read_text()` で一括ロードしない。ファイルサイズを先に確認したうえで
    `open()` の逐次 iteration で1行ずつ読み、必要フィールドだけ取り出す（無制限メモリの回避。1行が
    改行を持たない巨大な内容だと逐次 iteration でもその1行分は読み切る必要があるため、事前のファイル
    サイズ確認が実質的な上限になる）。rag.md は `_RAG_CHUNKS_FILE_CAP_BYTES`（jsonl と同一の上限。
    同じ文書の対になるサイドカーでサイズの桁が大きく異ならない前提・新しい env は増やさない）付きで
    一括読み込みする（アンカー分割にファイル全体が要るため・jsonl 側の逐次読みとは別の設計判断）。

    索引本文／埋め込み対象は rag.md をアンカーで分割した本文（D1・**もう jsonl の `search_text` は
    読まない**）。`line` は立てない（rag チャンクは行番号を持たない。無ければキーを省く既存の流儀に
    合わせる）。
    """
    if md_path is None:
        return [], [], [], "rag_md_missing"
    try:
        if md_path.stat().st_size > _RAG_CHUNKS_FILE_CAP_BYTES:
            return [], [], [], "rag_md_too_large"
    except OSError:
        return [], [], [], "rag_md_stat_failed"
    try:
        markdown = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [], [], [], "rag_md_invalid_utf8"
    except OSError:
        return [], [], [], "rag_md_read_failed"
    anchors, anchor_reason = _parse_rag_md_chunks(markdown)
    if anchor_reason is not None:
        return [], [], [], anchor_reason

    ids, bodies, texts, seen_chunk_ids = [], [], [], set()
    try:
        if rag_path.stat().st_size > _RAG_CHUNKS_FILE_CAP_BYTES:
            return [], [], [], "file_too_large"
    except OSError:
        return [], [], [], "stat_failed"
    try:
        with rag_path.open("r", encoding="utf-8", errors="strict") as f:
            for lineno, raw_line in enumerate(f, start=1):
                if lineno > _RAG_CHUNKS_MAX_ROWS:
                    return [], [], [], "too_many_rows"
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    return [], [], [], "invalid_json"
                if not isinstance(row, dict):
                    return [], [], [], "row_not_object"
                cid = row.get("chunk_id")
                if not (isinstance(cid, str) and cid):
                    return [], [], [], "missing_chunk_id"
                if row.get("source_rel_path") != rel:
                    return [], [], [], "source_rel_path_mismatch"
                if cid in seen_chunk_ids:
                    return [], [], [], "duplicate_chunk_id"
                seen_chunk_ids.add(cid)
                text = anchors.get(cid)
                if not (isinstance(text, str) and text.strip()):
                    return [], [], [], "rag_md_anchor_missing"
                if len(text) > _RAG_CHUNK_SEARCH_TEXT_MAX_CHARS:
                    return [], [], [], "search_text_too_long"
                body = {**base_meta, "chunk_id": cid, "text": text}
                locator = _chunk_locator(row)
                if locator is not None:
                    body["locator"] = locator
                body.update(_chunk_context_meta(row))    # B1: 隣接キー（無ければ何も足さない）
                ids.append(_rag_chunk_es_id(rel, cid))
                bodies.append(body)
                texts.append(text)
    except UnicodeDecodeError:
        return [], [], [], "invalid_utf8"
    except OSError:
        return [], [], [], "read_failed"
    if set(anchors) - seen_chunk_ids:                  # 1:1検証: rag.md側の余剰アンカー
        return [], [], [], "rag_md_anchor_surplus"
    return ids, bodies, texts, None


def _bulk_batches(ids: list, bodies: list, vec_by_idx: dict) -> list:
    """`ids`/`bodies`（＋ `vec_by_idx` の embedding）を ES `_bulk` 用の NDJSON ペイロードへ、
    件数（`_ES_BULK_BATCH_MAX_DOCS`）とバイト数（`_ES_BULK_BATCH_MAX_BYTES`）の両方で有界な
    バッチへ分割する。チャンクサイズ（特に embedding 付きチャンク）がばらつくため、件数だけでは
    1バッチの実バイト量を有界にできない——両方の閾値のどちらかに達したら現行バッチを確定する。

    1チャンク単体が `_ES_BULK_BATCH_MAX_BYTES` を超えても、そのチャンクだけの単独バッチとして
    送る（レコードを分割できないため・現行バッチが空でない場合のみ閾値判定するので単独チャンクは
    必ず1バッチに収まる）。返り値は各バッチの NDJSON 本文（末尾改行込み・そのまま `_req` へ渡せる）。
    """
    out: list = []
    lines: list = []
    n_docs = 0
    n_bytes = 0
    for i, (cid, body) in enumerate(zip(ids, bodies)):
        v = vec_by_idx.get(i)                          # branch=="source"/軽量テキスト枠は埋め込み対象外＝常に None
        if v is not None:
            body = {**body, "embedding": v}
        action = json.dumps({"index": {"_id": cid}})
        doc = json.dumps(body, ensure_ascii=False)
        pair_bytes = len(action.encode("utf-8")) + len(doc.encode("utf-8")) + 2   # +2 = 各行の改行
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


def _doc_chunk_bundle(world: str, d: dict, derived: Path | None, rag_exts: frozenset,
                      res_map: dict | None = None) -> tuple:
    """1文書分のチャンク（rag優先→40行縮退）を組み立てる（2026-09-03・EMBED-3・doc単位ストリーミング化）。

    `index_world()` の元の単一ループ本体（world 全体の ids/bodies/texts を一括蓄積していた箇所）を
    doc 単位へ切り出したもの。呼び出し側はこれを Pass1（embed 対象テキストだけ）・Pass2（実際の
    索引本文一式）で**2回**呼ぶ——2回目は disk I/O の再実行（`_validate_rag_chunks`/
    `doc_text.read_world_doc_text` の再実行）を伴うが、途中のチャンクデータを world 規模で
    メモリに保持しない設計とのトレードオフ（`index_world` docstring 参照）。

    `res_map`（省略可・I2・2026-09-05）: `importance.resolve_for_world()` の結果（world 全体を
    `index_world()` が1回だけ解決して渡す）。あれば `meta`（この文書の全チャンクへ passthrough）へ
    `importance`/`importance_reason` を条件付きで焼き込む（無ければキー自体を持たない・§2 truth
    table）。省略時（Pass1 の埋め込み専用パス等）は従来どおり付けない。

    返値 `(bundle, degraded_entry)`。
    - `bundle`＝None: この文書はスキップ（unreadable／軽量テキスト第2段／テキスト抽出失敗）。
    - `bundle`＝{"ids": [...], "bodies": [...], "texts": [...], "no_embed": [...]}（0件チャンクもありうる）。
    - `degraded_entry`＝None または {"doc": rel, "reason": ...}（rag_chunks はあるが使えなかった場合のみ・
      `bundle` の有無とは独立——rag が壊れていても legacy 縮退が成功すれば bundle は None にならない）。
    """
    if d.get("state") == "unreadable":              # 分類を唯一のゲートに——再読が成功しても索引しない
        return None, None
    rel = d["name"]
    ext = Path(rel).suffix.lower()
    # 軽量テキスト枠（`ingest.text_kind`）の**第2段**（未知拡張子・拡張子なしの内容推定）文書は
    # ES 索引の対象外にする——第1段（拡張子マップ）は通常の文書と同格に扱うが、第2段は
    # read_around（引用検証・精読）/`verify_doc_exists`/`manifest_doctype_count` が元々
    # 対象外にしている（`corpus_docs._classify_generic_text`/`status_document_doctype` の
    # `allow_content_sniff=False` 参照）ため、ES だけが検索可能でも引用・精読できない
    # 非対称（「検索可能集合＝引用可能集合」契約の破れ）が生まれる。`d["doctype"]` が
    # 軽量テキスト枠の2ラベル（`CODE_DOCTYPE_LABEL`/`DOCUMENT_DOCTYPE_LABEL`）で、かつ
    # `text_kind.classify_ext(ext)` が `None`（＝第1段では判定できず第2段が必要だった）なら
    # 第2段と判定できる——`corpus_docs` 側に専用フラグを追加せずに済む安価な再判定。
    is_light_text = d.get("doctype") in (text_kind.CODE_DOCTYPE_LABEL, text_kind.DOCUMENT_DOCTYPE_LABEL)
    if is_light_text and text_kind.classify_ext(ext) is None:
        return None, None
    # 軽量テキスト枠は「ベクトル・グラフ・LLM を一切通さない」契約——登録コード
    # （branch=="source"）だけでなく、軽量テキスト枠の資料側（csv/tsv/log/rtf 等・
    # branch=="office"）も embed 対象から除外する（下の `no_embed` へ搬送）。
    no_embed = d.get("branch") == "source" or is_light_text
    meta = {"doc_id": rel, "ext": ext, "branch": d.get("branch"),
            "top_scope": d.get("top_scope"), "scopes": _scopes(rel)}
    meta.update(_provenance_meta(d))                # 抽出来歴（extraction_method/confidence/has_conflicts）を搬送（無ければ省略）
    if res_map:                                      # I2: importance/importance_reason（無ければ省略）
        meta.update(importance.public_fields(res_map.get(rel)))
    rag_result = None
    degraded_entry = None
    if derived is not None and meta["ext"] in rag_exts:
        rag_path, path_reason = _safe_rag_chunks_path(derived, rel)
        if path_reason is not None:
            degraded_entry = {"doc": rel, "reason": path_reason}
        elif rag_path is not None:
            # D1: rag.md（正本）が jsonl（証跡サイドカー）と対になっているかを先に見る。
            md_path, md_path_reason = _safe_rag_md_path(derived, rel)
            if md_path_reason is not None:
                degraded_entry = {"doc": rel, "reason": md_path_reason}
            elif md_path is None:
                degraded_entry = {"doc": rel, "reason": "rag_md_missing"}
            else:
                r_ids, r_bodies, r_texts, invalid_reason = _validate_rag_chunks(rag_path, md_path, rel, meta)
                if invalid_reason is None and r_ids:
                    rag_result = (r_ids, r_bodies, r_texts)
                elif invalid_reason is not None:
                    degraded_entry = {"doc": rel, "reason": invalid_reason}
    if rag_result is not None:
        c_ids, c_bodies, c_texts = rag_result
        return {"ids": c_ids, "bodies": c_bodies, "texts": c_texts,
                "no_embed": [no_embed] * len(c_ids)}, degraded_entry
    text = doc_text.read_world_doc_text(world, d)   # ソース/テキスト、または rag_chunks の無い/無効な Office/PDF
    if text is None:
        return None, degraded_entry
    ids, bodies, texts, no_embeds = [], [], [], []
    rows = text.splitlines()
    for s in range(0, max(1, len(rows)), _CHUNK_LINES):
        chunk = "\n".join(rows[s:s + _CHUNK_LINES]).strip()
        if not chunk:
            continue
        ids.append(f"{rel}#{s + 1}")
        bodies.append({**meta, "line": s + 1, "text": chunk})
        texts.append(chunk)
        no_embeds.append(no_embed)
    return {"ids": ids, "bodies": bodies, "texts": texts, "no_embed": no_embeds}, degraded_entry


class _StreamingBulkSender:
    """`index_world()` Pass2 が doc グループ単位で積むチャンクを、bulk 送信の境界（`_bulk_batches` と
    同じ件数/バイト数の閾値）に達し次第 ES へ流す（world 全体を先に1本のリストへ溜めない・EMBED-3）。

    `refresh=true` は**最後の送信だけ**に付けたいが、ストリーミングでは「これが最後の送信か」は
    全 doc を処理し終えるまで分からない——直前に確定した1バッチを `_pending` として1つだけ
    持ち越す（lag-by-one）: 次のバッチが確定した時点で「まだ後続がある＝直前の持ち越し分は最後
    ではない」と確定してから送る。`finish()` で最後に残った `_pending` を refresh 付きで送る
    （`_bulk_batches()` を丸ごと事前計算していた旧実装と、doc/バイト数境界での分割・
    refresh のタイミングとも完全に等価——境界がどこにあっても最後の1バッチだけが refresh 付きになる）。
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
            # 案a（全部か無しか）: 途中バッチの失敗は呼び出し元が wipe する——一部だけ入った索引は
            # 利用者から見て「検索したのに出てこない」というサイレントな取りこぼしになるため。
            self.failed, self.error = True, "bulk_failed"
            return False
        if res.get("errors"):                          # item-level の失敗（HTTP200 でも起きる）
            self.failed, self.error = True, "bulk_errors"
            return False
        return True

    def send_group(self, ids: list, bodies: list, vec_by_idx: dict) -> bool:
        """1グループ分（`_EMBED_FLUSH_CHUNKS` 件程度に有界）を bulk 用サブバッチへ分割し、
        持ち越し済みの前グループ分（あれば・後続確定＝refreshなし）→ このグループの内部境界
        （複数サブバッチに割れた場合、最後の1つ以外）の順で送る。このグループ自身の最後の
        サブバッチは新たな `_pending` として持ち越す。"""
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
    """Pass2 の1グループ（doc数件・チャンク`_EMBED_FLUSH_CHUNKS`件程度に有界）を、必要なら
    埋め込みキャッシュから embedding を引いて bulk 送信する（`sender.send_group` へ委譲）。
    キャッシュ参照は `embed_feature_applies` が真の時だけ（Pass1 が全チャンクの embed を
    完了させている前提——グループ内の対象チャンクだけを束ねて1回のシャード参照で引く）。"""
    vec_by_idx: dict = {}
    if embed_feature_applies:
        embed_positions = [i for i, skip in enumerate(no_embed) if not skip]
        if embed_positions:
            keys = [_chunk_key(ec, texts[i]) for i in embed_positions]
            hit = _embed_cache_lookup_batch(world, keys, ec["dim"])
            for i, k in zip(embed_positions, keys):
                v = hit.get(k)
                if v is not None:
                    vec_by_idx[i] = v
    return sender.send_group(ids, bodies, vec_by_idx)


def index_world(world: str, settings: dict | None = None, content_sig: str | None = None,
                progress: Callable[[int, int], None] | None = None) -> dict:
    """world を**クリーン再索引**（delete→create→bulk）。埋め込み設定があればベクトルも付与（kNN 用）。

    失敗は古い索引を残さず error を返す（RV Med）。埋め込みを一度も選んでいない構成（BM25 のみで
    良い）は従来どおり graceful に BM25 のみで索引する。**A7 で明示選択したクラウドの埋め込みが
    解決できない場合は削除の前に失敗する**（RV1・FBK-1・2026-09-01）——`embeddings.cfg()` の
    None を通常の埋め込み未設定と区別できないまま索引を作り直すと、既存の（正しい）ベクトル付き
    索引が黙って BM25-only に置き換わり、クラウド側の障害に気付けない。**`cfg()` は成功しても
    実際の embed API 呼び出しが失敗する場合も同様に保護する**（RV2・2026-09-01）——文書列挙と
    埋め込み生成を delete の**前**に完了させ、選択済みクラウドで生成が実際に失敗（`_embed_cached`
    が `None` を返す）したら delete せず打ち切る。
    `content_sig`＝索引時のフォルダ署名（_meta に保存・古い索引の検知＝鮮度修復に使う・RV High）。
    埋め込みは**内容ハッシュキャッシュ**経由（`_embed_cached`）で未変更チャンクの再 embed を省く（コスト最適化）。
    `rag_es_enabled()` 時は Office/PDF（`{rel}.rag_chunks.jsonl` を持つ）をレコード単位チャンクで索引し、
    それ以外（ソース/テキスト文書、rag_chunks が無い/検証に失敗した Office/PDF）は従来どおり40行チャンク。
    rag_chunks が存在するのに使えなかった（symlink・sidecar 取り違え・破損・上限超過等）文書数は、
    戻り値の `rag_degraded`（件数）・`rag_degraded_docs`（内訳、無ければ省略）で報告する（`rag_es_enabled()`
    が False の間はこの2キー自体を返さない＝挙動は完全に不変）。
    `branch=="source"`（登録コード＋軽量テキスト枠の汎用コード）のチャンクは埋め込み対象から
    除外する（コード分の embed コストを避ける・BM25 は全チャンクに効く＝ハイブリッド検索は
    embedding 欠落チャンクでも壊れない）。

    **bulk 送信はバッチ分割する**（2026-09-02）: world 全体を1本の `_bulk` リクエストへ詰めると
    リクエストボディ・ES 側の処理メモリが world のチャンク総数に比例して際限なく増える（旧実装の
    欠陥——`_RAG_CHUNKS_MAX_ROWS` 等の1文書単位の上限を守っていても、複数文書の合算で同じ問題が
    起きる）。`_bulk_batches()` が件数・バイト数の両方で有界なバッチへ分割し、`refresh=true` は
    **最後のバッチだけ**に付ける（毎バッチ refresh すると著しく遅い）。**途中のバッチが失敗したら
    `delete_world()` して world を空へ戻し、error を返す**（既存の「失敗＝空の索引・次回 sync が
    全部やり直す」という全部か無しかのセマンティクスを、複数バッチに分けても保つ——一部だけ入った
    索引は利用者から見て「検索したのに出てこない」というサイレントな取りこぼしになるため）。

    **doc 単位ストリーミング化（2026-09-03・EMBED-3）**: 実環境（1万ファイル・数十万チャンク級）で
    world 全体の ids/bodies/texts をリストへ一括蓄積し、返り値の全ベクトルを filled dict/vec_by_idx
    に保持する旧実装は、メモリが world サイズに比例して OOM を起こしていた（EMBED-2 のバッチ確定
    フラッシュだけでは埋め込みキャッシュ dict 自体が world 規模のまま残っていた）。本関数は**2パス**
    構成にする（ユーザー裁定「doc単位パイプライン化」）:

    - **Pass1（埋め込みのみ）**: `_doc_chunk_bundle()` で doc ごとにチャンクを組み立て、embed 対象
      テキストだけを `_EMBED_FLUSH_CHUNKS` 件程度のバッファへ束ね、閾値に達するたび `_embed_cached()`
      を呼ぶ（HTTP バッチングの単位は維持——doc 単位で1往復にはしない）。bodies/ids はこのパスでは
      作らない（テキストと no_embed フラグだけ）。
    - **Pass2（bulk 送信）**: 同じ `docs` リストをもう一度走査し、`_doc_chunk_bundle()` で実際の
      ids/bodies/texts を組み立て直し（Pass1 との重複 I/O はメモリ有界化とのトレードオフ）、
      `_EMBED_FLUSH_CHUNKS` 件程度のグループへ束ねる。埋め込みが有効なら、グループの対象チャンク
      だけキャッシュから引いて（`_embed_cache_lookup_batch`）body へ付与し、`_StreamingBulkSender`
      で ES へ流す。

    **「embed 一部失敗＝world 全体を BM25 縮退・doc ごとの混在を作らない」不変条件は2パス構成が
    自然に守る**: Pass1 が world 全体の embed 完了（成功/失敗）を確定させてから Pass2 が始まる
    ため、Pass2 の時点で「この world は embed 済みか否か」は既に一様に決まっている
    （`embed_feature_applies` 参照）——doc の処理順序によって前半だけベクトル付きになるような
    早期実行は起きない。埋め込みキャッシュ自体もシャード化（`_embed_cache_dir` 系関数）し、
    一度に保持するベクトル量を「1シャード＋1フラッシュバッチ」へ有界化した（world 全体を
    1つの dict へロードしない）。剪定（現存チャンクだけへ縮める）は Pass1 が成功した直後
    （ES 操作より前）に `_prune_embed_cache()` で1回だけ行う——`_embed_cached()` は複数回
    呼ばれるためもう自前で剪定しない（`_embed_cached` docstring 参照）。

    `progress`（省略可）: Pass2 が文書グループを flush するたび `progress(done_docs, total_docs)`
    を呼ぶ（`total_docs` は Pass1/Pass2 が共有する `docs` リストの長さ＝厳密な事前カウントの
    ための追加走査はしない）。実環境（数時間かかる最長段）で最長時間 done/total が動かない
    まま止まって見える問題への対処（office_md 段の `_office_progress` と同じ役割）。呼び出し
    頻度の間引きは呼び出し元（`ingest/worker.py` の `_progress`）の責務——ここでは flush 単位
    （`_EMBED_FLUSH_CHUNKS` チャンクごと）でそのまま呼ぶ。
    """
    if not available():
        return {"available": False, "indexed": 0, "chunks": 0}
    # RV2/RV3: cfg() と cloud_selected_but_unavailable() を同じ system_settings スナップショット
    # で呼ぶ（別々に読むと、その間の admin 更新で「解決できた（旧鍵）が理由判定は不可用（新状態）」
    # のような食い違いが起こりうる）。埋め込み生成が実際に失敗した場合の再判定（下記）でも同じ
    # スナップショットを使い回す。kill-switch 有効時は読まない（`_embed_system_settings_snapshot`）。
    sys_s = _embed_system_settings_snapshot()
    ec = embeddings.cfg(_settings(settings), system_settings=sys_s)
    if ec is None and embeddings.cloud_selected_but_unavailable(system_settings=sys_s):
        # 削除より前に打ち切る＝既存索引（ベクトル付きかもしれない）を BM25-only で上書きしない。
        return {"available": True, "indexed": 0, "chunks": 0, "error": "embedding_cloud_unavailable"}

    use_rag = rag_es_enabled()
    derived = worlds.derived_rag_dir(world) if use_rag else None   # RAG 正本層（§8.1 三階層）
    rag_exts = _rag_chunk_source_exts() if use_rag else frozenset()
    docs = corpus_docs.world_documents(world, include_rag=True) if use_rag else corpus_docs.world_documents(world)
    total_docs = len(docs)                             # 進捗表示の total（既に materialize 済みの一覧の長さ・追加走査なし）
    # I2（2026-09-05）: 重要度は world 全体を1回だけ解決し（`res_map`）、各文書のチャンク組み立て
    # （`_doc_chunk_bundle`）へ使い回す。`world_documents()` 呼び出しは既存テスト（`world_documents`
    # を単一引数 `lambda w: docs` で差し替える広範な既存スタブ群）と互換な形（`root=` を渡さない）
    # のまま維持し、重要度解決だけ独立に `world_dir()` を呼ぶ（`doc_ledger.public_documents` ほど
    # 厳密な「同一 root 保証」ではないが、rebind は稀な運用イベントであり許容する・最小変更）。
    wd = worlds.world_dir(world)
    res_map = importance.resolve_for_world(world, root=wd) if wd else {}

    # ---- Pass1: 埋め込みのみ（doc単位でテキストを束ね、`_EMBED_FLUSH_CHUNKS` 件ごとに flush）----
    had_embed_eligible = False
    embed_ok = True
    valid_keys: set = set()
    reused_total = 0
    embedded_total = 0
    if ec is not None:
        buf: list = []
        for d in docs:
            bundle, _degraded = _doc_chunk_bundle(world, d, derived, rag_exts)
            if bundle is None:
                continue
            for t, skip in zip(bundle["texts"], bundle["no_embed"]):
                if skip:
                    continue
                had_embed_eligible = True
                valid_keys.add(_chunk_key(ec, t))
                buf.append(t)
                if len(buf) >= _EMBED_FLUSH_CHUNKS:
                    vecs, reused, embedded = _embed_cached(world, buf, ec)
                    reused_total += reused
                    embedded_total += embedded
                    buf = []
                    if vecs is None:
                        embed_ok = False
                        break
            if not embed_ok:
                break
        if embed_ok and buf:
            vecs, reused, embedded = _embed_cached(world, buf, ec)
            reused_total += reused
            embedded_total += embedded
            if vecs is None:
                embed_ok = False

    if ec is not None and had_embed_eligible and not embed_ok:
        # RV2（高1）: `cfg()` は解決できたが実際の embed API 呼び出しが失敗した（`_embed_cached`
        # が `None` を返した＝`not ec or not texts` ではなく実送信の失敗・docstring 参照）。
        if embeddings.cloud_selected_but_unavailable(system_settings=sys_s):
            # まだ `delete_world()` を呼んでいない＝既存索引（ベクトル付きかもしれない）はそのまま残る。
            return {"available": True, "indexed": 0, "chunks": 0, "error": "embedding_cloud_unavailable"}
        # クラウドを一度も選んでいない構成の実失敗は従来どおり graceful に BM25-only へ降格して続行する。

    # 埋め込みキャッシュの最終剪定/削除は**世界全体の doc ストリームを一巡し終えた直後**（ES 操作の前）に
    # 一度だけ行う——`_embed_cached()` は EMBED-3 で複数回（doc グループごと）呼ばれるため、
    # もう自前で剪定しない（`_embed_cached` docstring 参照）。embed_ok が False（真の失敗）の間は
    # キャッシュへ触れない——フラッシュ済みの部分成功分を温存する（旧実装と同じ契約）。
    if ec is None:
        _delete_embed_cache(world)
    elif embed_ok:
        _prune_embed_cache(world, valid_keys)

    # `ec` が有効でも、対象チャンクが全て branch=="source"/軽量テキスト枠（`had_embed_eligible` が
    # 偽）の world では埋め込み対象自体が無いため embed は「この構成で最新（対象チャンクが無い
    # だけ）」——これを「埋め込み未設定/失敗」と区別せず emeta に埋め込み素性を書かないと、
    # `needs_reindex()` の want(`ec` 由来) vs have(`None`) 比較が恒久的に不一致となり、内容が
    # 全く変わらない world でも毎 sync で無限に full reindex が走り続ける（実測）。真の埋め込み
    # 失敗（`had_embed_eligible` はあるのに `embed_ok` が偽）とは区別する——そちらは従来どおり
    # 書かず、次回 sync で再試行させる。
    embed_feature_applies = ec is not None and (not had_embed_eligible or embed_ok)

    if not delete_world(world):                       # 削除失敗のまま bulk すると stale chunk が残る
        return {"available": True, "indexed": 0, "chunks": 0, "error": "delete_failed"}

    dim = ec["dim"] if embed_feature_applies else None
    human_md_sig = _human_md_config_sig(world)
    if human_md_sig == _HUMAN_MD_PENDING_SENTINEL:
        # pending センチネルは meta に書かない（「成功して確定した版」だけを書く契約）——
        # bulk がこれから走る今の呼び出しで pending 中と分かっていても、meta には
        # フィールド欠落と同じ扱いの None を書く（呼び出し元が bulk 成功後に
        # `office_md.confirm_human_md_es_sig` でマーカーを確定するまで pending のまま）。
        human_md_sig = None
    emeta = {"world_id": world,                        # 帰属を索引自身に刻む（孤児リコンサイルの厳密な所有者判定）
             "mapping_version": ES_MAPPING_VERSION,     # RV Low: マッピング/チャンクメタの版（変わったら reindex）
             "search_chunk_mode": _search_chunk_mode(), # rag/legacy の索引ソース方針（旧世代索引の一度きり reindex 検知用）
             "arms_sig": _arms_config_sig(),             # RV Med: アーム構成（変わったら reindex・fail-safe で None もありうる）
             "human_md_sig": human_md_sig,              # H2: human_md 版（RAG_ES の設定に関わらず評価・pending は書かない）
             "analyzer_config_sig": _analyzer_config_sig()}  # コード解析アナライザの有効構成（変わったら reindex）
    if _CHUNK_LINES != _CHUNK_LINES_DEFAULT:            # 既定(40)時は書かない＝索引 meta を最小限に保つ
        emeta["chunk_lines"] = _CHUNK_LINES             # legacy チャンク粒度（既定と異なるときだけ記録・drift 検知用）
    # `content_sig` は **bulk が全バッチ成功した後**に書く（下の `_confirm_content_sig`）。
    # ここで先に書くと、途中でプロセスが落ちた（OOM・kill＝ES エラーではないので
    # `_wipe_after_bulk_failure` が走らない）ときに「一部だけ入った索引＋有効な content_sig」が残り、
    # `needs_reindex()` が False を返して**その中途半端な索引が恒久的に居座る**。バッチ化で
    # 索引中の時間窓が伸びたぶんこの窓は無視できない。後書きなら、途中でどう落ちても
    # content_sig が無い＝次回 sync が必ず張り直す（fail-closed）。
    if embed_feature_applies:                          # had_embed_eligible が偽でも ec 由来の素性を書く（上のコメント参照）
        emeta.update({"embed_provider": ec["provider"], "embed_model": ec["model"], "dim": ec["dim"]})
    if not ensure_index(world, dim=dim, emeta=(emeta or None)):
        return {"available": True, "indexed": 0, "chunks": 0, "error": "create_failed"}

    # ---- Pass2: doc 単位でチャンクを組み立て、`_EMBED_FLUSH_CHUNKS` 件ごとにグループ化して
    # bulk 送信する（world 全体を先に1本のリストへ溜めない・vec_by_idx もグループ内ローカル）----
    n_docs = 0
    total_chunks = 0
    rag_degraded = 0
    rag_degraded_docs: list = []
    sender = _StreamingBulkSender(world)
    g_ids: list = []
    g_bodies: list = []
    g_texts: list = []
    g_no_embed: list = []
    for d in docs:
        bundle, degraded_entry = _doc_chunk_bundle(world, d, derived, rag_exts, res_map)
        if degraded_entry is not None:
            rag_degraded += 1
            rag_degraded_docs.append(degraded_entry)
        if bundle is None:
            continue
        n_docs += 1
        g_ids.extend(bundle["ids"])
        g_bodies.extend(bundle["bodies"])
        g_texts.extend(bundle["texts"])
        g_no_embed.extend(bundle["no_embed"])
        total_chunks += len(bundle["ids"])
        if len(g_ids) >= _EMBED_FLUSH_CHUNKS:
            if not _flush_doc_group(sender, world, g_ids, g_bodies, g_texts, g_no_embed, ec, embed_feature_applies):
                break
            g_ids, g_bodies, g_texts, g_no_embed = [], [], [], []
            if progress is not None:
                progress(n_docs, total_docs)
    if not sender.failed and g_ids:
        _flush_doc_group(sender, world, g_ids, g_bodies, g_texts, g_no_embed, ec, embed_feature_applies)
    if progress is not None:
        # ループ末尾の leftover が0件（直前の mid-loop flush でちょうど割り切れた／末尾が
        # 全てスキップ/0チャンク文書だった）場合でも最終呼び出しを保証する（`done == total`
        # を必ず1回は報告する契約・上の mid-loop 呼び出しに畳み込まず独立させる）。
        progress(n_docs, total_docs)

    rag_report = {}
    if use_rag:                                        # OFF はキー自体を返さない（戻り値の形も完全不変）
        rag_report["rag_degraded"] = rag_degraded
        if rag_degraded_docs:
            rag_report["rag_degraded_docs"] = rag_degraded_docs

    if sender.failed or not sender.finish():
        # 案a（全部か無しか）: 途中バッチの失敗は world を空へ戻す——一部だけ入った索引は
        # 利用者から見て「検索したのに出てこない」というサイレントな取りこぼしになるため。
        _wipe_after_bulk_failure(world)
        return {"available": True, "indexed": 0, "chunks": 0, "error": sender.error, **rag_report}
    _confirm_content_sig(world, content_sig)           # 全バッチ成功後にだけ鮮度署名を確定する
    return {"available": True, "indexed": n_docs, "chunks": total_chunks,
            "vectors": bool(embed_feature_applies and had_embed_eligible),
            "embedded": embedded_total, "reused": reused_total, **rag_report}


def count(world: str) -> int | None:
    try:
        return _req("GET", f"/{_index(world)}/_count").get("count")
    except Exception:
        return None


def indexed_sig(world: str) -> str | None:
    """ES index に記録した content_sig（鮮度判定用）。無ければ None。"""
    return _index_meta(world).get("content_sig")


def needs_reindex(world: str, content_sig, settings: dict | None = None) -> bool:
    """ES 索引の張り直しが要るか（ES 稼働時のみ）。空 / 内容署名ズレ / **アーム構成ズレ**（RV Med） /
    **マッピング版ズレ**（RV Low） / **索引ソース方針(rag/legacy)ズレ** / **チャンク粒度ズレ** /
    **人間向け MD 版ズレ**（H2・RAG_ES の設定に関わらず評価） / **アナライザ構成ズレ** /
    **埋め込み素性(provider/model/dim)ズレ** で True。

    ＝内容（ソースファイル自体）が変わらなくても、(a) 取り込みアーム構成（例 OCR 有効/無効・vision
    有効/無効）を切り替えた、(b) このプロセスのマッピング/チャンクメタ仕様（`ES_MAPPING_VERSION`）が
    デプロイで上がった、(c) 索引ソース（rag_chunks か40行チャンクか）が変わった（旧世代 legacy 索引
    からの一度きりの移行検知・現行はグローバル切替トグルではなく常時 rag）、(d) `SHERPA_ES_CHUNK_LINES`
    （legacy チャンク粒度）を切り替えた、(e) 人間向け
    `{rel}.md`（`human_md`）のレンダラ/抽出器版が変わった、または当該 world がまだその版に
    追随できていない（`_human_md_config_sig` 参照。RAG_ES 無効時は `{rel}.md` が索引の実体そのもの、
    有効時も rag.md を持たない文書の縮退先であり続けるため常に評価する。**RAG_ES 有効かつ rag.md を
    持つ文書の縮退先は rag.md へ変わった**（`corpus_docs.iter_world_documents(include_rag=True)`）ので、
    その経路の鮮度は human_md_sig ではなく worker の `.rag_sig` holdback が担保する）、(f) コード解析アナライザの有効構成（登録順・拡張子集合・
    分類契約版＝`analyzer_registry.config_signature()`）を変えた（新規アナライザ追加・CODE-1b の
    有効/無効・並び替え）、(g) 埋め込みプロバイダ/モデルを切り替えた、のいずれかが
    あれば次回 `sync()` で確実に張り直す（管理UI 不要・更新で修復）。`content_sig`（ソースファイルの
    rel/mtime/ctime/size のみ）はこれらを検知できないため、この署名を別途比較する。索引済みメタに
    該当フィールド自体が無い旧索引は、mapping_version/search_chunk_mode/arms_sig/analyzer_config_sig
    は比較先が None になり不一致＝1回だけ再索引される。**human_md_sig だけは違う**: pending 中は
    比較先が明示のセンチネル文字列（`_HUMAN_MD_PENDING_SENTINEL`）になるため、meta 側の値
    （欠落＝None でも旧確定値でも）と絶対に一致せず、pending が解消するまで**毎回**再索引を試みる
    （fail-closed の代償として意図的・`_human_md_config_sig` docstring 参照）。chunk_lines だけは
    例外: 本 env 導入前の索引は全て旧既定40行チャンクで作られているため、欠落は `None` ではなく
    旧既定 `_CHUNK_LINES_DEFAULT`（40）として扱う（さもないと env 未使用の既存 world まで一律
    reindex される）。
    """
    if not available():
        return False
    if not count(world):
        return True
    meta = _index_meta(world)
    if meta.get("content_sig") != content_sig:
        return True
    if meta.get("mapping_version") != ES_MAPPING_VERSION:
        return True
    if meta.get("search_chunk_mode") != _search_chunk_mode():
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
    want = (ec["provider"], ec["model"], ec["dim"]) if ec else (None, None, None)
    have = (meta.get("embed_provider"), meta.get("embed_model"), meta.get("dim"))
    return want != have


def _parse_hits(res: dict) -> list:
    out = []
    for h in res.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        full = src.get("text", "")
        frag = (h.get("highlight", {}).get("text") or [full])[0]
        hit = {"doc_id": src.get("doc_id"), "line": src.get("line"),
               "text": frag, "score": h.get("_score"), "ext": src.get("ext")}
        # 抽出来歴・rag_chunks 由来メタ（chunk_id・locator・B1 の隣接キー）・重要度（I2）を表示用に
        # passthrough（無ければ付けない＝後方互換）。`parent_id` は `agentic_search` の親返し（L4c）
        # が読む。`importance_source` は含めない（J4・出典には出さない）。
        for k in ("extraction_method", "confidence", "has_conflicts", "chunk_id", "locator",
                  "previous_chunk_id", "next_chunk_id", "parent_id", "logical_record_id", "section_path",
                  "importance", "importance_reason"):
            if src.get(k) is not None:
                hit[k] = src[k]
        out.append(hit)
    return out


def _importance_boost_query(bool_query: dict) -> dict:
    """`bool_query`（`{"bool": {...}}`）を function_score で包み、`importance` フィールドに応じて
    スコアを乗算する（I2・J2・2026-09-05）。

    `高`＝`_ES_IMPORTANCE_BOOST_HIGH` 倍・`低`＝`_ES_IMPORTANCE_BOOST_LOW` 倍。`importance` フィールド
    自体を持たない文書（`_重要度.txt` の無い world・`中`/未設定）はどちらの `term` filter にも
    一致せず、function_score の既定挙動（一致する function が無ければ 1 倍）によりスコアは完全に
    不変（`x * 1.0 == x`）——受け入れ条件（重要度制御ファイルの無い world でスコア完全不変）を
    ES クエリの構造自体で満たす。`score_mode="first"`＝1文書につき `importance` は単一値なので
    高々1つの function しか一致しない（複数一致を想定した合算は不要）。`boost_mode="multiply"`＝
    元のクエリスコアへの乗算（BM25/knn 側の相対順位を保ったまま重要度で押し上げ/押し下げる）。
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
    """純 kNN（`search_knn_only`）専用の**取得後の再ランク**（I2・J2）。

    ES の `knn` 節は BM25 の `query` と違い function_score で直接包めない（function_score が
    受け付けるのは `query` 節のみ）——`_importance_boost_query` と等価な効果を得るため、返って
    来た kNN スコアへ Python 側で同じ乗数を掛け、安定ソート（`list.sort` は同値の相対順序を保つ）
    で並べ直す。`importance` フィールドが無い（＝`_重要度.txt` の無い world）ヒットは乗数 1.0 のまま
    ＝スコア・順序とも完全不変（`x*1.0==x` かつ全ヒットが同じ乗数なら並び替えても順序は変わらない）。
    """
    for h in hits:
        if h.get("score") is not None:
            h["score"] = h["score"] * _importance_score_multiplier(h.get("importance"))
    hits.sort(key=lambda h: -(h.get("score") or 0.0))
    return hits


def search(world: str, query: str, scope_paths=None, k: int = 20, settings: dict | None = None,
          vector: bool = True, layer=None, k_ceiling: int | None = None) -> tuple[list, str | None]:
    """検索。`vector=True` かつ埋め込み設定があれば **kNN＋BM25 ハイブリッド**、無ければ BM25。範囲フィルタ・graceful。

    `k_ceiling`（省略可・既定 `None`＝モジュール既定 `_ES_SEARCH_K_MAX` を使う＝既存呼び出し元は
    無変更）: 呼び出し元が既に「倍率適用後の絶対上限」まで検証済みの `k` を渡す場合
    （`agentic_search.run_tool` の `es_search` 分岐＝調べる深さが計算した実効値）、`_ES_SEARCH_K_MAX`
    （env `SHERPA_GREP_MAX_HITS` 由来・既定 50 の床）による再クランプを迂回してこちらを使う——
    `_ES_SEARCH_K_MAX` の既定 50 は grep 側の既定 30 よりヒット数を広めに取る設計のための床であり、
    調べる深さ「最大」（既定 ×2＝60）のような意図的に大きい値まで潰してしまう。
    `_ES_SEARCH_K_MAX` 自体の既存契約（`SHERPA_GREP_MAX_HITS` 未設定時は 50 を下回らない等・
    `tests/unit/test_es_index_meta.py` 参照）はこの引数を渡さない既存呼び出し元でそのまま残る。

    返値 `(hits, degrade_reason|None)`（RV2・FBK-1・2026-09-01・`search_knn_only()` と同じ形へ統一）。
    `search_knn_only()` と違い、本関数は degrade 時も **BM25 の hits をそのまま返す**（reason は
    「hybrid でなく BM25 だけになった理由」の注記であって、hits を空にする合図ではない）。
    `es_unavailable`／クエリ空はこれまでどおり `[]` を返す。
    degrade_reason 語彙: `es_unavailable`／`embedding_cloud_unavailable`／`query_embed_failed`／
    `hybrid_query_failed`（hybrid 自体が失敗し BM25 は成功＝hits は空でない）／
    `es_query_failed`（BM25 自体も失敗＝hits は空。`search_service.DEGRADE_REASONS` と同一集合・
    増やすときは両方直す）。
    `vector=False`＝BM25 のみ（クエリ埋め込みを呼ばない＝コスト/レイテンシ回避・facts 統合用・
    reason は常に None）。
    `layer`（省略可・`"docs"|"code"|"both"`・既定 `None`＝`"both"`＝フィルタなし＝既存呼び出し元は
    無変更）: 探す対象（調べ方ブロック §3.4）。`scopes` と同じ `filter` 節に `branch`（`classify_document`
    確定値）の term/must_not フィルタを積む（`layer.es_filter` 参照・grep/agentic と同じ判定）。

    呼び出し元（`agentic_search.py` の `es_search` ツール）はこの reason を tool result 経由で
    trace ノードへ搬送し、UI（思考の流れ）へ表示する——サーバログの warning だけでは利用者/
    運用者以外の窓口（チャット画面）に届かず、静かな縮退のままになる（RV2 是正）。
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
        flt.append({"terms": {"scopes": sel}})       # 選択 prefix のいずれかを含む doc に限定
    lfilt = layer_mod.es_filter(layer)
    if lfilt:
        flt.append(lfilt)
    hl = {"fields": {"text": {"fragment_size": 240, "number_of_fragments": 1}}}
    bm25 = {"size": k, "query": _importance_boost_query({"bool": {"must": [{"match": {"text": q}}], "filter": flt}}),
           "highlight": hl}
    # RV2/RV3: cfg() と cloud_selected_but_unavailable() を同じ system_settings スナップショットで
    # 呼ぶ（別々に読むと、その間の admin 更新で判定が食い違いうる）。kill-switch 有効時は読まない
    # （`_embed_system_settings_snapshot`）。
    sys_s = _embed_system_settings_snapshot() if vector else None
    ec = embeddings.cfg(_settings(settings), system_settings=sys_s) if vector else None
    reason = None
    if vector and ec is None and embeddings.cloud_selected_but_unavailable(system_settings=sys_s):
        reason = "embedding_cloud_unavailable"
        _log.warning("es_index.search: 選択中クラウドの埋め込みが解決できず world=%s は BM25 のみへ降格しました",
                     world)
    meta = _index_meta(world) if ec else {}
    same = bool(ec) and (meta.get("embed_provider") == ec["provider"]
                         and meta.get("embed_model") == ec["model"] and meta.get("dim") == ec["dim"])
    if same:                                          # 索引のベクトル素性が一致する時だけ kNN（不一致は BM25・無駄な embed もしない）
        qv = embeddings.embed([q], ec, world=world)
        if qv:
            # 既定配分（w=0.5）のときは boost キー自体を書かない（無指定と同じ本文にする）。
            # 0.5 以外のときだけ boost を付与する（合計2.0に配分）。
            match_clause = {"match": {"text": q}}
            knn_extra: dict = {}
            if _HYBRID_WEIGHT != 0.5:
                match_clause = {"match": {"text": {"query": q, "boost": _HYBRID_WEIGHT * 2.0}}}
                knn_extra = {"boost": (1.0 - _HYBRID_WEIGHT) * 2.0}
            # I2（J2）: 重要度ブーストは `query`（BM25/keyword 側）だけへ掛ける——ES はこの top-level
            # `query`+`knn` 併記を「両者のスコアを加算」で合成するため（`_HYBRID_WEIGHT` の
            # boost 配分と同じ仕組み）、`knn` 側は素のベクトル類似度のまま残る（純kNN側の重要度
            # ブーストは `search_knn_only`/`_rerank_knn_by_importance` が別途担う・実装形の選択理由は
            # 提案書 I2 の報告を参照）。
            hybrid = {"size": k, "highlight": hl,
                      "query": _importance_boost_query({"bool": {"must": [match_clause], "filter": flt}}),
                      "knn": {"field": "embedding", "query_vector": qv[0], "k": k,
                              "num_candidates": max(50, k * 5), "filter": flt, **knn_extra}}
            try:
                return _parse_hits(_req("POST", f"/{_index(world)}/_search", hybrid)), None
            except Exception:
                # RV3（FBK-1・2026-09-01）: hybrid 自体の失敗（次元不一致/未ベクトル索引等）は
                # BM25 が成功すれば hits が空にならない＝`es_query_failed`（hits が空になる
                # BM25 自体の失敗）とは別の reason にする。`_degrade_result_node()` はこの区別で
                # 「BM25 は返っているが hybrid だけ失敗した」ケースも思考ノード対象にできる。
                reason = "hybrid_query_failed"        # 次元不一致/未ベクトル索引等の実クエリ失敗 → BM25 へ
        else:
            reason = "query_embed_failed"            # クエリ埋め込みの実通信失敗 → BM25 へ
    try:
        return _parse_hits(_req("POST", f"/{_index(world)}/_search", bm25)), reason
    except Exception:
        return [], "es_query_failed"                 # BM25 自体も失敗＝hits 空を最優先の理由で説明する


def search_knn_only(world: str, query: str, scope_paths=None, k: int = 20,
                    settings: dict | None = None, layer=None) -> tuple[list, str | None]:
    """**純 kNN 検索**（BM25 を混ぜない・search_service の vector エンジン用・外部連携API E2a）。

    既存 `search()` は kNN＋BM25 を同一 bool query に入れたハイブリッド固定のため、
    engines=["vector"] 単独を表現できない。本関数は top-level `knn` のみのクエリを発行する。
    返値 `(hits, degrade_reason|None)`。hits は `_parse_hits` 形 `{doc_id, line, text, score, ext}`。
    degrade_reason 語彙: es_unavailable / embedding_not_configured / embedding_cloud_unavailable /
    vector_feature_mismatch / query_embed_failed / es_query_failed（search_service.py の
    DEGRADE_REASONS と同一・増やすときは両方直す）。`embedding_cloud_unavailable`（RV1・FBK-1）は
    A7 で明示選択したクラウドの埋め込みが解決できない場合＝`embedding_not_configured`（クラウドを
    一度も選んでいない通常の未設定）と区別し、呼び出し側が「設定すれば直る」のか「選択済みクラウド
    が壊れている」のかを利用者へ正直に伝えられるようにする。
    `layer`（省略可・既定 `None`＝`"both"`）: `search()` と同じ探す対象フィルタ（§3.4）。
    """
    q = (query or "").strip()
    if not q:
        return [], None
    if not available():
        return [], "es_unavailable"
    k = max(1, min(k, 50))
    # RV2/RV3: cfg() と cloud_selected_but_unavailable() を同じ system_settings スナップショットで
    # 呼ぶ（別々に読むと、その間の admin 更新で判定が食い違いうる・`search()` と同じ理由）。
    # kill-switch 有効時は読まない（`_embed_system_settings_snapshot`）。
    sys_s = _embed_system_settings_snapshot()
    ec = embeddings.cfg(_settings(settings), system_settings=sys_s)
    if not ec:
        if embeddings.cloud_selected_but_unavailable(system_settings=sys_s):
            return [], "embedding_cloud_unavailable"
        return [], "embedding_not_configured"
    meta = _index_meta(world)
    if not (meta.get("embed_provider") == ec["provider"]
            and meta.get("embed_model") == ec["model"] and meta.get("dim") == ec["dim"]):
        return [], "vector_feature_mismatch"      # 索引素性ズレ（provider/model/dim いずれか）
    qv = embeddings.embed([q], ec, world=world)
    if not qv:
        return [], "query_embed_failed"
    flt = []
    sel = scope_mod.normalize_scope_paths(scope_paths)
    if sel:
        flt.append({"terms": {"scopes": sel}})    # 既存 search() と同一の範囲フィルタ（全エンジン共通）
    lfilt = layer_mod.es_filter(layer)
    if lfilt:
        flt.append(lfilt)
    body = {"size": k,
            "knn": {"field": "embedding", "query_vector": qv[0], "k": k,
                    "num_candidates": max(50, k * 5), "filter": flt}}
    try:
        # I2（J2）: 純 kNN は function_score で `query` を包めない（`knn` 節は対象外）ため、
        # 取得後の再ランクで重要度ブーストを適用する（`_rerank_knn_by_importance` 参照）。
        return _rerank_knn_by_importance(_parse_hits(_req("POST", f"/{_index(world)}/_search", body))), None
    except Exception:
        return [], "es_query_failed"
