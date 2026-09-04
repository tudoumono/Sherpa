"""取り込み・抽出プレビュー（read-only・鏡モデル）。

world グラフ（`world_graph.build_world`）をそのまま画面が描ける形に整形する（Neo4j 不要）。
旧 merge(S+L)/worker.build_snapshot/structure.json は撤去。エンティティ/関係/名寄せ/状態＋件数を返す。
書き込みは一切しない。特定テーマの名前は持たない（語彙はデータ＝world/concepts 由来）。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path

from . import doc_ledger, scope_infer as si, store, worlds
from .ingest import world_graph_service

# 種別ラベル → 表示の日本語（非エンジニア向け・04-画面の原則.md §6）。K13 で供給源を失ったラベルは刈った。
_TYPE_JA = {
    "Module": "プログラム", "Copybook": "コピーブック", "DataItem": "項目",
    "Document": "文書", "Batch": "バッチ", "Table": "テーブル",
}


def _build(world: str, *, files=None):
    """有効グラフ構築は共通入口へ委譲（worker と同一・rv-full DRY）。preview は未解決を空 flags で返す従来挙動を維持。

    `files`（省略可・キーワード専用・既定 None＝既存呼び出し元は無変更）: `world_graph_service.
    build_effective_world`（→`world_graph.build_world`）へそのまま転送する。渡さない限り
    `build_world` 内で従来どおり `scope_infer.safe_files` を直接歩く（`graph_view` 経由の呼び出しは
    無変更）。渡す場合は `_build_full_view` が pin 済み root から1回だけ materialize した list
    （RV1是正#3・2026-09-01・`build_preview` 参照）。
    """
    if not worlds.world_dir(world):
        return [], [], []
    return world_graph_service.build_effective_world(world, files=files)


def _counts(nodes, edges, world, *, doc_count: int | None = None) -> dict:
    """件数サマリ（raw nodes/edges から直接算出＝build を増やさず preview/graph_view で共用・rv-full2 #5）。

    `doc_count`（省略可）: 呼び出し側が既に文書一覧を算出済みなら件数だけ渡す（`build_preview` が
    `preview_documents` の結果件数を渡し、`doc_ledger.documents_for(world)` の**別の**全木走査を
    もう一度発生させないために使う・§③ 2026-09-01）。省略時は従来どおりここで算出する
    （`graph_view` 経由の呼び出しは無変更）。
    """
    def _em(items, val):
        return sum(1 for x in items if x.get("extraction_method", "static") == val)
    return {
        "entities": len(nodes), "entities_static": _em(nodes, "static"),
        "relations": len(edges), "relations_static": _em(edges, "static"),
        "deprecated": sum(1 for n in nodes if n.get("status", "active") == "deprecated"),
        "hidden": sum(1 for n in nodes if n.get("status", "active") == "hidden_candidate"),
        "documents": doc_count if doc_count is not None else len(doc_ledger.documents_for(world)),
    }


def _preview_entities_relations(nodes, edges) -> tuple[list, list]:
    """`build_preview` の entities/relations 整形（署名・キャッシュとは無関係の純粋な整形部分）。

    S3（K12）以降、全ノード/エッジが常に static（意味層/REALIZES 由来は撤去済み）のため
    `extraction_method` は表示/ソートの意味を持たない——フィールド・ソート双方から撤去。
    """
    by_cid = {n["cid"]: n for n in nodes}

    entities = [{
        "name": n["name"], "label": n["label"],
        "status": n.get("status", "active"), "value": n.get("value"),
        "top_scope": n.get("top_scope"), "phase": n.get("phase"), "path": n.get("path"),
        "analyzer": n.get("analyzer"),                # 担当アナライザの来歴（コード以外は None）
    } for n in nodes]
    entities.sort(key=lambda e: (e["label"], e["name"]))

    relations = []
    for e in edges:
        src, dst = by_cid.get(e["src"]), by_cid.get(e["dst"])
        relations.append({
            "type": e["type"], "src": src["name"] if src else e["src"].rsplit("#", 1)[-1],
            "dst": dst["name"] if dst else e["dst"].rsplit("#", 1)[-1],
            "src_label": src["label"] if src else "?", "dst_label": dst["label"] if dst else "?",
            "status": e.get("status", "active"), "doc": e.get("doc", ""),
        })
    relations.sort(key=lambda r: (r["type"], r["src"]))
    return entities, relations


def _graph_signature(payload: dict) -> str:
    """応答本体（`signature` を除く全フィールド）を**丸ごと**署名する決定的値（ETag 用・②graph 軽量化）。

    RV是正（2026-07-08・Med#1）: 以前は nodes/edges の一部フィールドのみを署名対象にしており、
    `counts`（文書数等）だけが変化した場合に署名が変わらず 304 が古い `counts` を返しうた
    （グラフ内容と独立に変化しうる項目の drift を見逃す）。`graph_view` が組み立てた辞書
    （world/counts/total_nodes/total_edges/truncated/nodes/edges）を丸ごと署名対象にすることで、
    将来フィールドが増えても個別に足し忘れない。nodes/edges は各要素を正規化 JSON 文字列にしてから
    sorted（並び順・ビルド順・dict キー順に依存しない決定性）。"""
    canon = dict(payload)
    for key in ("nodes", "edges"):
        if key in canon:
            canon[key] = sorted(json.dumps(x, sort_keys=True, ensure_ascii=True, default=str) for x in canon[key])
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _select_top_nodes(nodes, edges, limit):
    """次数（degree）上位 `limit` ノードと、その間の辺だけに絞る（段階読み込みの主要ノード選択）。

    決定的: degree 降順→同数は名前昇順→id 昇順で安定に選ぶ。`limit` が None/0 以下、または全件が
    収まる場合は truncated=False で素通し（近傍展開/検索の既存挙動は別 API＝ここは初期の全体像のみ）。
    返値 `(nodes, edges, truncated)`。ノード列は元の順序を保持（決定的・レイアウト差を生まない）。"""
    total = len(nodes)
    if not limit or limit <= 0 or total <= limit:
        return nodes, edges, False
    deg = {n["id"]: 0 for n in nodes}
    for e in edges:
        if e["source"] in deg:
            deg[e["source"]] += 1
        if e["target"] in deg:
            deg[e["target"]] += 1
    ranked = sorted(nodes, key=lambda n: (-deg[n["id"]], n.get("name") or "", n["id"]))
    keep = {n["id"] for n in ranked[:limit]}
    kept_nodes = [n for n in nodes if n["id"] in keep]
    kept_edges = [e for e in edges if e["source"] in keep and e["target"] in keep]
    return kept_nodes, kept_edges, True


# GRA-1: world → limit 適用前の全体 view（プロセス内キャッシュ・単一 worker 前提）。
# 鍵は world のみ（値に確定時点の last_sig/last_synced_at を持たせて比較）——世界数分の小さな辞書。
# RV1是正#4（2026-09-01）: `build_preview` もこの**同じ**キャッシュを共有する（グラフ構築のみを
# キャッシュ対象にし、文書一覧／重要度／診断は毎回フレッシュに計算する・下記 `build_preview` 参照）。
# 以前は preview 専用の別キャッシュ（`_PREVIEW_CACHE`）に応答全体を入れていたが、重要度・診断は
# `last_sig` より細かい世代（制御ファイル内容 hash・直近 run の DB 状態）で失効する契約を持つため、
# 外側キャッシュがヒットするとその失効判定に一切到達できず、重要度変更・診断復旧・一時的な
# `unknown` が次回 sync まで固定されてしまっていた（RV1 finding #4）。
_GRAPH_VIEW_CACHE: dict[str, dict] = {}

# miss 時の構築（重い）を1本のロックで直列化する（single-flight・GRA-1是正#5）。世界横断で1本
# ＝異なる world 同士の miss も直列化されるが、管理者がグラフを覗く単発操作であり、並行 miss を
# 束ねて `_build` の重複実行（CPU・Neo4j・I/O が要求数倍）を防ぐ方が優先される。
_GRAPH_VIEW_LOCK = threading.Lock()

# `_GRAPH_VIEW_LOCK` 保持中の DB プローブに強制する有限 timeout（GRA-1是正RV2#2）。無期限だと
# DB が詰まった瞬間にこのプロセス内ロックを握ったまま止まり、他 world の miss まで巻き添えで
# 全滞留する。`get_world()`/`get_world_status_row()` 既存の connect/statement timeout 機構
# （残り時間ベース）をそのまま再利用する・値は他箇所の同種プローブ（`_METERING_DB_TIMEOUT_S` 等）
# と同じ 5 秒に揃える。
_LOCK_PROBE_TIMEOUT_S = 5


def _current_world_status(world: str, *, connect_timeout: float | None = None,
                          statement_timeout_ms: int | None = None) -> dict:
    """現在の world 世代プローブ（`last_sig`／`last_synced_at`／`root_path`）。狭い1行 SELECT
    （`store.get_world_status_row`）を1回読むだけ。未登録 world（行なし・dev fixture）は
    世代なし（`sig=""`）として扱う——`worlds.world_dir()` の未登録フォールバックと同じ前提で
    異常ではない。DB 例外はここでは握り潰さない——silent degradation なしの家風どおり、
    呼び出し元（router）が明示的にログ付き 503 へ変換する契約（GRA-1是正#3・preview は
    `routers/worlds.py::ingest_preview` が同型で変換する・RV1是正#6）。

    `connect_timeout`/`statement_timeout_ms`（省略可）は `store.get_world_status_row` へそのまま
    転送するだけ（既定 None＝無期限＝fast-path の pre-lock プローブは無変更）。`_GRAPH_VIEW_LOCK`
    保持中の呼び出しだけが `_LOCK_PROBE_TIMEOUT_S` を渡す（GRA-1是正RV2#2）。
    """
    row = store.get_world_status_row(world, connect_timeout=connect_timeout,
                                     statement_timeout_ms=statement_timeout_ms) or {}
    return {"sig": row.get("last_sig") or "", "synced_at": row.get("last_synced_at"),
            "root_path": row.get("root_path")}


def _resolved_root(root_path) -> bool:
    """`root_path` が今なお到達可能な実ディレクトリか（`routers/worlds.py::world_status` と同じ
    `stat(follow_symlinks=False)` + `S_ISDIR` 判定・symlink 化けも拒否）。"""
    if not root_path:
        return False
    try:
        st = Path(root_path).stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)


def _cached_view(world: str, sig: str, synced_at) -> dict | None:
    """`sig`/`synced_at` の両方が一致するキャッシュがあれば bundle dict を返す（無ければ None）。

    `sig` 単独の一致では ABA を見逃す——concepts/extract 等の再実行は成功時に原本由来の同じ
    `last_sig` へ戻りうる（`A→""→A`）。空文字を挟む pre-invalidate の間に一度も `graph_view()`/
    `build_preview()` が呼ばれないと、`sig` だけを見た旧実装は再構築後も同じ `sig` で恒久的に
    ヒットし続けてしまう（GRA-1是正#1）。`last_synced_at` は `set_world_sig` が呼ばれるたびに
    必ず更新される（pre-invalidate も確定も）ため、`A→""→A` でも往復後の確定時刻は必ず変わり、
    複合キーなら別世代として扱える。

    bundle の `files` は常に `None`（キャッシュヒット時は歩いていない＝呼び出し元が文書一覧用に
    必要ならここで別途 materialize する・`build_preview` 参照）。
    """
    cached = _GRAPH_VIEW_CACHE.get(world)
    if sig and cached is not None and cached["sig"] == sig and cached["synced_at"] == synced_at:
        return {"out_nodes": cached["out_nodes"], "out_edges": cached["out_edges"], "counts": cached["counts"],
                "total_nodes": cached["total_nodes"], "total_edges": cached["total_edges"],
                "signature": cached["signature"], "raw_nodes": cached["raw_nodes"],
                "raw_edges": cached["raw_edges"], "raw_flags": cached["raw_flags"],
                "files": None, "sig": sig, "synced_at": synced_at}
    return None


def _build_full_view(world: str, status: dict, *, need_files: bool = False) -> dict:
    """limit 適用前の全体 view を1回構築する。

    `_build`（`world_graph_service.build_effective_world`）は原本木＋意味層（concepts/auto橋等）を
    毎回読み直して再構築する処理であり、Neo4j を読み返すわけではない（Neo4j は worker が書き込む
    先・グラフ検索/近傍展開だけがそれを読む・GRA-1是正#8）。

    レジストリ済みで `root_path` が到達可能な world は、構築全体をこの検証済み `root_path` へ
    `pin_world_root` で固定する——構築の途中で参照先が別 root（rebind）へ切り替わらないように
    する（GRA-1是正#2）。戻り値の `resolved` が False は一時的な参照先未解決／未登録 world の
    フォールバックであることを示し、呼び出し元はこの結果をキャッシュへ公開しない。

    `need_files=True`（`build_preview` 専用・RV1是正#3）: pin 済みの root から `scope_infer.
    safe_files` を1回だけ materialize し、`_build`（→`world_graph.build_world`）へ**その同じ
    list** を渡す——`build_preview` はこの戻り値の `files` を文書列挙・重要度解決・重要度診断にも
    使い回すため、cache miss（実構築）時は world 木を通しで1回しか歩かない（以前は `_build` の
    内部歩行と preview 側の歩行が独立で計2回になっていた＝false green だった旧テストの是正）。
    `need_files=False`（`graph_view` の既存呼び出し）は `files=None` のまま `_build(world)` を
    呼ぶ——挙動・呼び出しシグネチャとも無変更。
    """
    resolved = _resolved_root(status["root_path"])
    files = None
    wd_used = None

    def _do_build():
        nonlocal files, wd_used
        if need_files:
            wd_used = worlds.world_dir(world)
            files = list(si.safe_files(wd_used)) if wd_used else []
            return _build(world, files=files)
        return _build(world)

    if resolved:
        with worlds.pin_world_root(world, status["root_path"]):
            nodes, edges, flags = _do_build()
    else:
        nodes, edges, flags = _do_build()
    by_cid = {n["cid"]: n for n in nodes}
    out_nodes = [{"id": n["cid"], "name": n["name"], "type": n["label"],
                  "type_ja": _TYPE_JA.get(n["label"], n["label"]),
                  "status": n.get("status", "active"),
                  "value": n.get("value"), "top_scope": n.get("top_scope"), "path": n.get("path")}
                 for n in nodes]
    ids = set(by_cid)
    out_edges = [{"source": e["src"], "target": e["dst"], "type": e["type"],
                  "status": e.get("status", "active")}
                 for e in edges if e["src"] in ids and e["dst"] in ids]
    # `need_files=True` で既に `files` を materialize 済みなら、それを再利用して documents 件数を
    # 数える（`doc_ledger.documents_for` へ渡す＝もう一度歩かない）。この `counts` は
    # `_GRAPH_VIEW_CACHE` へ公開され `graph_view()` からも読まれる共有値のため、正確な件数が要る
    # （`build_preview` 側で使い捨てるからといって手抜きの概算値を入れない・RV1是正#3 の副作用是正）。
    doc_count = len(doc_ledger.documents_for(world, root=wd_used, files=files)) if files is not None else None
    counts = _counts(nodes, edges, world, doc_count=doc_count)   # 同じ build を使い回す（rv-full2 #5）
    total_nodes, total_edges = len(out_nodes), len(out_edges)
    # 署名は「limit 適用前（全体）」の応答本体を丸ごと対象にする（world/counts 込み・Med#1 是正）。
    full_payload = {"world": world, "counts": counts, "nodes": out_nodes, "edges": out_edges,
                    "total_nodes": total_nodes, "total_edges": total_edges, "truncated": False}
    signature = _graph_signature(full_payload)
    return {"out_nodes": out_nodes, "out_edges": out_edges, "counts": counts,
            "total_nodes": total_nodes, "total_edges": total_edges, "signature": signature,
            "raw_nodes": nodes, "raw_edges": edges, "raw_flags": flags,
            "files": files, "resolved": resolved}


def _build_and_publish(world: str, status: dict, *, need_files: bool = False) -> dict:
    """未キャッシュ時の構築＋（安全なら）公開。呼び出し元が `_GRAPH_VIEW_LOCK` を保持し、待機中に
    他スレッドが先に公開していないかの二重チェック（`_cached_view`）を済ませている前提
    （single-flight・GRA-1是正#5）。

    公開前に世代を再確認する（GRA-1是正#2）: 一時的に参照先未解決／フォールバックで構築した
    結果（`resolved=False`）は公開しない——復旧後も誤った view を返し続けることを防ぐ。`sig` が
    空（pre-invalidate 中）も公開しない。構築には時間がかかりうるため、公開直前に
    `_current_world_status` を取り直し、構築開始時に読んだ世代（`sig`＋`synced_at`）から
    動いていないことを確認してから初めて `_GRAPH_VIEW_CACHE` へ書く——構築中に世代が進んでいたら、
    その構築結果はもう古い（あるいは無関係な）世代の記述であり、キャッシュへ焼き付けない。
    """
    sig, synced_at = status["sig"], status["synced_at"]
    built = _build_full_view(world, status, need_files=need_files)
    resolved = built.pop("resolved")
    if sig and resolved:
        post = _current_world_status(world, connect_timeout=_LOCK_PROBE_TIMEOUT_S,
                                     statement_timeout_ms=_LOCK_PROBE_TIMEOUT_S * 1000)
        if post["sig"] == sig and post["synced_at"] == synced_at:
            _GRAPH_VIEW_CACHE[world] = {"sig": sig, "synced_at": synced_at,
                                        "out_nodes": built["out_nodes"], "out_edges": built["out_edges"],
                                        "counts": built["counts"], "total_nodes": built["total_nodes"],
                                        "total_edges": built["total_edges"], "signature": built["signature"],
                                        "raw_nodes": built["raw_nodes"], "raw_edges": built["raw_edges"],
                                        "raw_flags": built["raw_flags"]}
        else:
            _GRAPH_VIEW_CACHE.pop(world, None)
    built["sig"], built["synced_at"] = sig, synced_at
    return built


def _get_graph_bundle(world: str, *, need_files: bool = False) -> dict:
    """`_GRAPH_VIEW_CACHE`（GRA-1）のヒット確認〜single-flight 構築を1箇所に集約（`graph_view`・
    `build_preview` 共有）。戻り値は `out_nodes`/`out_edges`/`counts`/`total_nodes`/`total_edges`/
    `signature`（変換済みグラフ view）＋`raw_nodes`/`raw_edges`/`raw_flags`（`_build` の生出力・
    entities/relations 整形用）＋`files`（`need_files=True` かつ cache miss で実構築した時だけ
    非 None）＋`sig`/`synced_at`（この呼び出しが確定した世代）。

    GRA-1是正RV2#2: `_GRAPH_VIEW_LOCK` を取ったら status を**取り直してから**二重チェック→構築を
    行う（lock 待ちに入る前の status は使い回さない）。取り直しには有限 timeout
    （`_LOCK_PROBE_TIMEOUT_S`）を強制する——DB が一時的に詰まっているだけなら、この再プローブが
    速く失敗して待機列の後続スレッドへ回る（構築に到達しない＝重い `_build` を無駄に繰り返さない）。
    """
    status = _current_world_status(world)
    sig, synced_at = status["sig"], status["synced_at"]
    if not sig:
        _GRAPH_VIEW_CACHE.pop(world, None)

    hit = _cached_view(world, sig, synced_at)
    if hit is not None:
        return hit
    with _GRAPH_VIEW_LOCK:
        status = _current_world_status(world, connect_timeout=_LOCK_PROBE_TIMEOUT_S,
                                       statement_timeout_ms=_LOCK_PROBE_TIMEOUT_S * 1000)
        sig, synced_at = status["sig"], status["synced_at"]
        if not sig:
            _GRAPH_VIEW_CACHE.pop(world, None)
        hit = _cached_view(world, sig, synced_at)      # 待機中に他スレッドが公開したかもしれない
        if hit is not None:
            return hit
        return _build_and_publish(world, status, need_files=need_files)


def graph_view(world=None, limit=None) -> dict:
    """ナレッジグラフを可視化用（nodes/edges）に整形（read-only）。id＝canonical_id（世代込みで一意）。

    `limit`（None/0 以下＝全件）を指定すると次数上位の主要ノードのみに絞り（段階読み込み・②2026-07-08）、
    `total_nodes`/`total_edges`/`truncated` を返す。`signature` は**limit 適用前の応答本体（world/counts/
    nodes/edges 等）を丸ごと**対象にした決定的内容署名（ETag 用・limit に依存しない＝内容が同じなら同じ・
    counts のみの変化でも drift する＝RV是正2026-07-08 Med#1）。件数サマリ `counts` は常に全体を表す
    （絞り込みで減らさない）。

    重い構築（`_build_full_view`）は world の世代（`last_sig`＋`last_synced_at`）が前回公開時から
    変わっていない限りスキップする（`_get_graph_bundle`・GRA-1）。`limit` はキャッシュの外＝毎回
    `_select_top_nodes` で都度絞る。
    """
    world = world or os.environ.get("SHERPA_VERSION") or worlds.default_world()
    bundle = _get_graph_bundle(world)
    view_nodes, view_edges, truncated = _select_top_nodes(bundle["out_nodes"], bundle["out_edges"], limit)
    return {"world": world, "nodes": view_nodes, "edges": view_edges, "counts": bundle["counts"],
            "total_nodes": bundle["total_nodes"], "total_edges": bundle["total_edges"],
            "truncated": truncated, "signature": bundle["signature"]}


def build_preview(world: str | None = None) -> dict:
    """抽出プレビュー（read-only）。エンティティ/関係/名寄せ/状態と件数サマリを返す。

    グラフ部分（entities/relations の元になる nodes/edges と `issues`＝flags）は `graph_view` と
    **同じ** `_GRAPH_VIEW_CACHE`（GRA-1）を共有する（`_get_graph_bundle(world, need_files=True)`）
    ——world の世代が変わらない限り `_build`（グラフ構築）を再実行しない。

    文書一覧（`documents`）・重要度解決・重要度診断（`importance_diagnostics`）は**キャッシュしない**
    ——**毎回フレッシュに計算する**（RV1是正#4）。理由: これらは `last_sig`（メタデータ由来の世代）
    より細かい失効契約を持つ（`_重要度.txt` の内容 hash・直近 run の DB 状態＝
    `ingest.importance.resolve_for_world`/`corpus_docs.last_run_blocked_docs` 参照）。外側を
    `last_sig` だけでキャッシュすると、`_重要度.txt` の編集・診断の復旧・一時的な `unknown` が
    次回 sync まで固定されてしまう（旧実装の欠陥）。

    走査回数: cache miss（世代が変わった直後の最初の呼び出し）は `_get_graph_bundle` が bundle の
    `files` を返す（`_build_full_view` が pin 済み root から1回 materialize し、`_build`→
    `world_graph.build_world` まで貫通させたもの・RV1是正#3）ため、それを文書列挙・重要度解決・
    診断へ使い回して**合計1回**しか歩かない。cache hit（世代不変）は `_build` 自体をスキップする
    代わりに、文書一覧のためだけに `scope_infer.safe_files` を**1回**歩く（グラフを再構築しない
    ぶん cache miss より軽いが、0回にはならない——上記の理由でここは意図的にキャッシュしない）。
    """
    world = world or os.environ.get("SHERPA_VERSION") or worlds.default_world()
    bundle = _get_graph_bundle(world, need_files=True)
    entities, relations = _preview_entities_relations(bundle["raw_nodes"], bundle["raw_edges"])

    wd = worlds.world_dir(world)
    if wd:
        files = bundle["files"] if bundle["files"] is not None else list(si.safe_files(wd))
        # `sig`（登録済み world の last_sig・未登録は空文字）を重要度解決へ渡す: `resolve_for_world`
        # は `sig=None` だと `worker.world_signature_of_root(wd)` でメタデータ署名を**もう1回**
        # 全木走査して作ってしまう（`files` を渡していても避けられない別の走査）。`bundle["sig"]`
        # は既にこの呼び出しの世代プローブで得た値の使い回し——重要度側の**内容 hash**による細かい
        # 失効判定（`_control_content_signature`・毎回フレッシュに読む）はこれとは独立に働くため、
        # 渡しても「即時反映」の契約は壊れない（空文字は渡さない＝`doc_ledger.preview_documents`
        # 参照）。
        preview_docs = doc_ledger.preview_documents(world, root=wd, files=files, sig=bundle["sig"] or None)
        diagnostics = doc_ledger.control_diagnostics(world, root=wd, files=files)
    else:
        preview_docs, diagnostics = [], []

    return {"world": world, "label": worlds.world_label(world),
            "counts": _counts(bundle["raw_nodes"], bundle["raw_edges"], world, doc_count=len(preview_docs)),
            "documents": preview_docs,
            "issues": bundle["raw_flags"], "entities": entities, "relations": relations,
            # `_重要度.txt` の構文診断（issues とは別キー＝issues は世界構築の警告、こちらは
            # 重要度設定ファイル固有の構文診断で意味が異なる）。
            "importance_diagnostics": diagnostics}
