"""ES 索引の公開（エイリアスの付け替え）と、作り直しの前の空き確認・作りかけの索引の後始末。
取り込みの開始時に埋め込みの接続先を決め、中止すべきかを判定する。`es_index` の下請け。ES への呼び出しは `es_index` の関数を呼び出し時に引く（試験が差し替える先を一つに保つ）。
公開中の索引を消すのは、新しい索引が公開されたと確かめた後だけ。空きが足りなければ古い索引に触れず作り直しを止める。
設計: docs/design/rag.md「ES 索引の構成」
"""
from __future__ import annotations

import logging
import time
import urllib.error

from .store import replace_rows
from .store.replace_rows import ReplaceRow

_log = logging.getLogger("sherpa")

REBUILD_FREE_FACTOR = 2  # 作り直しには、空きが古い索引の大きさのこの倍だけ要る（新旧 2 世代が並ぶ間の余裕を含む）
ACTIVATED = "activated"
NOT_ACTIVATED = "not_activated"
UNKNOWN = "unknown"      # 付け替えが効いたか確かめられない（両方の世代を残す）
_CONFIRM_ATTEMPTS = 3
_CONFIRM_WAIT_SEC = 1.0


def _es():
    from . import es_index
    return es_index


def index_has_vectors(world: str) -> bool:
    """既存の索引がベクトルの素性（`_meta.embed_provider`／`dim`）を持つか。索引が無ければ False。読めないときは True（既存を捨てない側に倒す）。"""
    es = _es()
    try:
        r = es._req("GET", f"/{es._index(world)}/_mapping")
    except urllib.error.HTTPError as exc:
        return exc.code != 404
    except Exception:
        return True
    for v in r.values():
        meta = (v.get("mappings") or {}).get("_meta") or {}
        return bool(meta.get("embed_provider") or meta.get("dim"))
    return False


def no_embedding_reason(ec: dict | None, meta_failed: bool, sys_s: dict | None) -> str:
    """検索でベクトルを使えないときの降格理由。`ec` は `cfg()` の返り値、`meta_failed` は索引のメタの取得が 404 以外で失敗したか。
    接続先は決まったが次元が無い（`ec` あり）: メタを読めなかったなら索引の障害＝`es_query_failed`、読めたなら `embedding_cloud_unavailable`。
    接続先が無い（`ec` なし）: Ollama でない設定の不成立は `embedding_cloud_unavailable`、通常の未設定は `embedding_not_configured`。
    """
    if ec is not None:
        return "es_query_failed" if meta_failed else "embedding_cloud_unavailable"
    return "embedding_cloud_unavailable" if _es().embeddings.cloud_selected_but_unavailable(system_settings=sys_s) else "embedding_not_configured"


def embed_feature_matches(ec: dict | None, meta: dict) -> bool:
    """索引の埋め込み素性が今の設定と同じか。次元が未実測（`ec["dim"]` が None）のときは次元を比べない（不明は「違う」でない）。
    次元が未実測でベクトルの素性を持たない索引は、測れないだけなので同じとみなす（再索引を促さない）。
    """
    es = _es()
    have = (meta.get("embed_provider"), meta.get("embed_model"), meta.get("dim"), meta.get("embed_algo"), meta.get("quant"))
    if ec is None:
        return have == (None,) * 5
    if ec.get("dim") is None:
        return not any(have[:3]) or (have[0], have[1], have[3], have[4]) == (
            ec["provider"], ec["model"], es.embeddings.EMBEDDING_INPUT_ALGORITHM_ID, es.embeddings.ES_VECTOR_QUANT)
    return have == (ec["provider"], ec["model"], ec["dim"], es.embeddings.EMBEDDING_INPUT_ALGORITHM_ID, es.embeddings.ES_VECTOR_QUANT)


def update_embed_target(ec: dict | None, meta: dict) -> tuple[dict | None, str | None]:
    """1 文書の更新で使う埋め込みの接続先と、更新できない理由 `(ec, error)`。
    次元が未実測なら、ベクトルのある索引は `embedding_dim_unknown`（測れるまで触らない）、無い索引は BM25 のまま（ec=None）。素性が違えば `embed_config_changed`。
    """
    if ec is not None and ec.get("dim") is None:
        if any(meta.get(k) for k in ("embed_provider", "embed_model", "dim")):
            return ec, "embedding_dim_unknown"
        ec = None
    return ec, None if embed_feature_matches(ec, meta) else "embed_config_changed"


def resolve_embed_target(world: str, settings: dict, system_settings: dict | None) -> tuple[dict | None, bool, bool]:
    """取り込みの開始時に埋め込みの接続先を決めて embed ログへ 1 行残す。返値 `(ec, abort, keep_cache)`。
    `ec` は次元が確定した接続先（無ければ None＝BM25 のみ）。`abort` は何も作る前に取り込みを中止すべきとき:
    Ollama でない設定が成立しない、または接続先は Ollama に決まったが次元を測れず、ベクトルのある既存索引がある（読めないときも）。
    接続先が決まって次元だけ測れないとき `keep_cache` が真（埋め込みキャッシュを消さない）。中止でも続行でも、接続先を次元なし（dim=None）で残す。
    """
    emb = _es().embeddings
    ec = emb.cfg(settings, system_settings=system_settings)
    if ec is None:
        emb.log_target(None, world)
        return None, emb.cloud_selected_but_unavailable(system_settings=system_settings), False
    emb.log_target(ec, world)
    if ec.get("dim") is None:
        return None, index_has_vectors(world), True
    return ec, False, False


def discard_building(world: str) -> None:
    """作りかけの物理索引を消す（エイリアスと古い索引には触れない）。消す直前にエイリアスの指す先を確かめ、公開中の索引・確かめられない場合は消さない。"""
    es = _es()
    name = es._building.pop(world, None)
    if not name or name == es._index(world):
        return
    targets = es._alias_targets(world)
    if targets is None or name in targets:
        _log.warning("es_index: 公開中か確かめられない索引は消しません: %s", name)
        return
    try:
        es._req("DELETE", "/" + name)
    except Exception as e:
        if getattr(e, "code", None) != 404:
            _log.warning("es_index: 作りかけの索引を消せませんでした（次回の作り直しで掃除します）: %s", name)


def _confirm_activated(world: str, new_idx: str) -> str:
    """付け替えの応答が届かなかったとき、エイリアスの指す先を引き直して効いたかを確かめる（回数を区切る）。"""
    es = _es()
    for attempt in range(_CONFIRM_ATTEMPTS):
        targets = es._alias_targets(world)
        if targets is not None:
            return ACTIVATED if new_idx in targets else NOT_ACTIVATED
        if attempt + 1 < _CONFIRM_ATTEMPTS:
            time.sleep(_CONFIRM_WAIT_SEC)
    return UNKNOWN


def activate_index(world: str, new_idx: str, *, row: ReplaceRow) -> str:
    """エイリアスを `new_idx` へ原子的に付け替え、エイリアスを引き直して効いたと確かめてから古い物理索引を消す。0.18 以前の実の索引は同じ `_aliases` の中で消して置き換える。
    返り値は `ACTIVATED`／`NOT_ACTIVATED`／`UNKNOWN`（応答が届かず効いたか確かめられない＝新旧どちらも消さない）。
    付け替えの前に入れ替えの行へ古い索引の名前と「入れ替えた」を書き、古い索引を消し終えたら「古いものを消した」にする。
    """
    es = _es()
    alias = es._index(world)
    if new_idx == alias:
        return ACTIVATED
    targets = es._alias_targets(world)
    names = es._concrete_indices(world)
    if targets is None or names is None:
        return NOT_ACTIVATED
    actions: list = [{"remove": {"index": t, "alias": alias}} for t in targets]
    if not targets and alias in names:
        actions.append({"remove_index": {"index": alias}})
    actions.append({"add": {"index": new_idx, "alias": alias}})
    old = sorted({*targets, *([alias] if not targets and alias in names else [])})
    replace_rows.before_swap(row, world, "es", old=old)   # 古い索引の名前と「入れ替えた」を、付け替えの前に行へ書く
    try:
        es._req("POST", "/_aliases", {"actions": actions})
    except Exception as e:
        _log.warning("es_index: エイリアスの付け替えの応答を受け取れませんでした（効いたかを確かめます）: %s: %s", world, type(e).__name__)
    # 応答の成否（2xx の `acknowledged:false` を含む）に関わらず、エイリアスが新しい索引を指すと確かめてから古い索引を消す
    state = _confirm_activated(world, new_idx)
    if state != ACTIVATED:
        if state == UNKNOWN:
            es._building.pop(world, None)  # 成否が不明＝作りかけとして消さない
        return state
    es._building.pop(world, None)
    removed = True
    for t in targets:
        if t == new_idx:
            continue
        try:
            es._req("DELETE", "/" + t)
        except Exception:
            removed = False
            _log.warning("es_index: 古い索引を消せませんでした（次回の作り直しで掃除します）: %s", t)
    if removed:
        replace_rows.after_old_removed(row, world, "es")
    return ACTIVATED


def delete_world(world: str, *, row: ReplaceRow) -> bool:
    """資料フォルダの索引を全部削除する（エイリアスの指す物理索引・作りかけの物理索引・0.18 以前の実の索引。無ければ無視）。wipe の派生物伝播で呼ぶ。"""
    es = _es()
    names = es._concrete_indices(world)
    if names is None:
        return False
    replace_rows.before_swap(row, world, "es", old=names)
    ok = True
    for n in names:
        try:
            es._req("DELETE", "/" + n)
        except urllib.error.HTTPError as e:
            ok = ok and e.code == 404  # 既に無い＝成功扱い
        except Exception:
            ok = False
    if ok:
        replace_rows.after_old_removed(row, world, "es")
    return ok


def delete_orphan(alias: str, *, row: ReplaceRow) -> None:
    """孤児の索引（エイリアス名 `alias` とその物理索引）を全部消す（掃除の行 `ES_CLEANUP_PREFIX + alias` を持つ呼び出し専用）。一覧を取れない・消せない索引があれば例外にする。"""
    es = _es()
    try:
        rows = es._req("GET", f"/_cat/indices/{alias}*?format=json&h=index&expand_wildcards=all")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        rows = []
    names = sorted(r["index"] for r in rows or [] if isinstance(r, dict) and es._physical_base(r.get("index", "")) == alias)
    replace_rows.before_swap(row, row.world, "es", old=names)
    for n in names:
        try:
            es._req("DELETE", "/" + n)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    replace_rows.after_old_removed(row, row.world, "es")


def drop_stale_physicals(world: str) -> bool:
    """エイリアスが指していない物理索引（前回落ちた作りかけ）を消す。一覧を取れなければ何も消さず False。"""
    es = _es()
    targets = es._alias_targets(world)
    names = es._concrete_indices(world)
    if targets is None or names is None:
        return False
    alias = es._index(world)
    for n in names:
        if n == alias or n in targets:
            continue
        try:
            es._req("DELETE", "/" + n)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return False
        except Exception:
            return False
    return True


def rebuild_space_shortfall(world: str) -> dict | None:
    """作り直しに要る ES の空きが足りないとき、足りない量を返す（古い索引には触れない）。足りていれば、または空き・大きさを取れなければ None。
    取り込みの実行中なら、ディスクの空きが少ないときと同じ止め方（`background.LowDiskStop`）で止める。
    """
    es = _es()
    names = es._concrete_indices(world)
    if not names:
        return None
    size = es._store_bytes(names)
    free = es._es_free_bytes()
    if size is None or free is None or free >= size * REBUILD_FREE_FACTOR:
        return None
    need = size * REBUILD_FREE_FACTOR
    short = {"free_bytes": free, "need_bytes": need, "shortfall_bytes": need - free}
    from .ingest import background, substage
    msg = (f"検索の索引を作り直す空きが足りないため取り込みを止めました（空き {free / 1024 ** 3:.1f} GB・"
           f"あと約 {(need - free) / 1024 ** 3:.1f} GB 必要）。今の検索の索引はそのまま残しています")
    _log.warning("es_index: %s: world=%s", msg, world)
    if background.in_run() or background.in_followup():
        substage.begin("low_disk", msg, logger=logging.getLogger("sherpa.embed"))
        raise background.LowDiskStop(free)
    return short


def read_ingest_no(world: str) -> int:
    """索引の `_meta.ingest_no`（書かれていない・読めないときは 0）。"""
    return int((_es()._index_meta(world) or {}).get("ingest_no") or 0)
