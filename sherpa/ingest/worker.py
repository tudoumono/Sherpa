"""資料フォルダの取り込みオーケストレーション（ライブ鏡）。

資料フォルダを1回スキャンし、台帳とグラフ（Neo4j）を現状に一致させる単一の経路:
① スキャン→アーカイブの展開先の差分同期→② 派生 MD→③ グラフ構築（`world_graph.build_world`）→④ 資料フォルダ単位のグラフ置換（`world_neo4j.load_world`）→
⑤ 台帳書込（`store.replace_documents`・グラフの置換が成功してから）→⑥ ES 索引→⑦ 仕上げ→ `ingest_runs` の終端記録。
設計: docs/design/rag.md「全体の流れ」
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone

from .. import corpus_docs, es_index, scope_infer, store, webhooks, worlds
from . import (
    archive_extract,
    failure_reasons,
    importance,
    office_md,
    resolve_settings,
    world_graph,
    world_graph_service,
    world_neo4j,
)
from .analyzers import registry as analyzer_registry

# MD 変換（取り込み進行ログ）は専用ログ（sherpa.ingest.convert）へまとめる（`sherpa/log_setup.py`・office_md.py と同じ系統）
_log = logging.getLogger("sherpa.ingest.convert")

# 段の表示名（内部段キー→利用者向け平文）。`ingest_runs.progress` へ書き込み、`GET /worlds/{wid}/status` が実行中 run の進捗として返す
STAGE_LABELS = {
    "accepted": "受け付けました",
    "scanning": "フォルダを確認中",
    "office_md": "旧形式を新形式へ変換し、読める写し（MD）・検索用データを作成中",
    "graph_build": "関係グラフを構築中",
    "es_index": "全文索引に登録し、ベクトル化中",
    "finalize": "仕上げ中",
    "deleting": "検索用データを削除しています",
}

# 逐次進捗の書き込み間隔（office_md 段の per-file 進捗をこの件数ごとに間引く）。最初（0件）と最後（総数一致）は必ず書く
_PROGRESS_FILE_INTERVAL = 100


def _target_of(url: str) -> str:
    """URL/URI から「ホスト:ポート」だけを取り出す（失敗理由の表示用・userinfo/パスは落とす）。取り出せなければ "?"。例外は投げない。"""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url or "")
        host = u.hostname or ""
        if not host:
            return "?"
        return f"{host}:{u.port}" if u.port else host
    except (TypeError, ValueError):
        return "?"


def build_world_graph(world: str):
    """資料フォルダの `(nodes, edges, flags)` を構築する（有効グラフの単一入口へ委譲）。"""
    return world_graph_service.build_effective_world(world)


def _reflect_graph_after_rag_rewrite(world: str) -> None:
    """`.rag.md` の軽量書換え後、Neo4j のグラフ（言及エッジ）を追いつかせる。

    言及エッジは `{rel}.rag.md` があればそれを本文として読む。`_llm_render_pass`／`regenerate_rag_rule_only`／
    `_refresh_derived_representations` は rag.md を書き換えるが世代（world 署名）を変えないため、`_run_locked` の
    `build_world_graph`→`load_world` を経由しない。ES 反映とは独立に常に呼ぶ。
    呼び出し元が `store.world_lock(world)` を保持している前提（ロックは再入不可）。失敗は例外のまま伝播させる。
    """
    nodes, edges, flags = build_world_graph(world)
    blocked = [f for f in flags if f.get("action") == "blocked"]
    if blocked:
        # `_run_locked` と同じく、不可読/途中で変わったコードがあるときは部分グラフで既存グラフを置換せず、例外で run を失敗として記録する
        reasons = sorted({str(f.get("reason")) for f in blocked})
        raise RuntimeError(f"graph reflect blocked: {','.join(reasons)}")
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, world, env["uri"], env["user"], env["pw"],
                           plugin_failures=world_graph.plugin_failures_from_flags(flags))


# `office_md.build_derived()` の per-file 失敗リスト（`[{"doc": rel, "reason": str}]`）のキー。末尾の `_failures` を落とした残りを stage 名にする
_FAILURE_LIST_KEYS = ("unhandled_failures", "legacy_conversion_failures", "conversion_failures",
                     "document_ir_failures", "evidence_ir_failures", "rag_failures")

_FAILED_FILES_LIMIT = 200   # `ingest_runs.extraction_snapshot` へ保存する件数の上限（超過分は total/truncated で示す）

# 「抽出不完全の疑い」一覧（失敗とは別枠・`failure_reasons.PARTIAL_EXTRACTION_LABEL/_ADVICE`）の上限
_PARTIAL_EXTRACTION_LIMIT = 200

def _office_md_stage_summary(drep: dict) -> dict:
    """`office_md.build_derived()` の要約3値（`_record` と PG replace 失敗パスの共通片）。"""
    return {"converted": drep.get("converted", 0), "failed": drep.get("failed", 0),
            "unsupported": drep.get("unsupported", 0)}


def _counts_summary(drep: dict | None, es_summary: dict | None, manifest: dict | None,
                    rows: list | None, scan_rep: dict | None = None) -> dict:
    """`extraction_snapshot["counts"]`（走査/対象/変換/索引/埋め込みの件数・時間）を作る。

    取れない項目はキー自体を付けない（0 と欠落を区別する）。`_record` と PG replace 失敗パスの両方から呼ぶ。
    `legacy_converted`／`legacy_failed` はこの run で実際に前段変換した件数（変換キャッシュから復元した分は含まない）。
    `scan_rep`（`corpus_docs.scan_report()` の戻り値）があれば、本文が読めず対象外にした件数・拡張子別内訳・秘匿除外件数を合流する。
    """
    c: dict = {}
    if manifest is not None:
        c["scanned"] = len(manifest)              # 走査で見つけたファイル数
    if rows is not None:                           # 台帳確定前に終わった失敗 run は欄ごと省略
        c["targeted"] = len(rows)                  # 取り込み対象＝台帳に載せた数
    if drep is not None:
        c["converted"] = drep.get("converted", 0)
        c["failed"] = drep.get("failed", 0)
        c["unsupported"] = drep.get("unsupported", 0)
        c["legacy_converted"] = drep.get("legacy_converted", 0)
        c["legacy_failed"] = len(drep.get("legacy_conversion_failures") or [])
    if es_summary is not None:
        if es_summary.get("indexed") is not None:
            c["es_indexed"] = es_summary["indexed"]
        if es_summary.get("embedded") is not None:
            c["embedded_chunks"] = es_summary["embedded"]
        if es_summary.get("reused") is not None:
            c["reused_chunks"] = es_summary["reused"]
        if es_summary.get("embed_elapsed_ms") is not None:
            c["embed_elapsed_ms"] = es_summary["embed_elapsed_ms"]
    if scan_rep is not None:
        c["unreachable_as_text"] = scan_rep.get("unreachable_as_text", 0)
        c["sensitive_excluded"] = scan_rep.get("sensitive_excluded", 0)
        by_ext = scan_rep.get("unreachable_as_text_by_ext")
        if by_ext:
            c["unreachable_as_text_by_ext"] = by_ext
        # 理由別内訳（判別不能／バイナリ）と、対象外にしない「一部が化けている」件数
        by_reason = scan_rep.get("unreachable_by_reason")
        if by_reason:
            c["unreachable_by_reason"] = by_reason
        encoding_partial = scan_rep.get("encoding_partial_count", 0)
        if encoding_partial:
            c["encoding_partial_count"] = encoding_partial
    return c


def _failed_files_summary(drep: dict) -> dict:
    """`office_md.build_derived()` の各段 `*_failures` を1つの一覧（rel＋stage＋理由コード）にまとめる。

    `reason` は `failure_reasons.classify()` の語彙（`detail` は分類前の生文字列）。`by_reason` は打ち切り前の全件の理由コード別件数。
    `items` は `_FAILED_FILES_LIMIT` 件で打ち切り、`total`・`truncated` を併記する。
    """
    items = []
    by_reason: dict[str, int] = {}
    for key in _FAILURE_LIST_KEYS:
        stage = key[: -len("_failures")]
        for entry in drep.get(key) or []:
            doc, raw_reason = entry.get("doc"), entry.get("reason")
            if not (isinstance(doc, str) and isinstance(raw_reason, str)):
                continue
            desc = failure_reasons.describe(raw_reason)
            by_reason[desc["code"]] = by_reason.get(desc["code"], 0) + 1
            items.append({"doc": doc, "stage": stage, "reason": desc["code"], "detail": desc["detail"]})
    return {"items": items[:_FAILED_FILES_LIMIT], "total": len(items),
            "truncated": len(items) > _FAILED_FILES_LIMIT, "by_reason": by_reason}


def _partial_extraction_summary(drep: dict) -> dict:
    """`office_md.build_derived()` の `partial_extraction_suspected`（失敗ではない「要確認」一覧）を整形する。`_failed_files_summary` と同じ打ち切り契約。"""
    items = [e for e in (drep.get("partial_extraction_suspected") or []) if isinstance(e.get("doc"), str)]
    return {"items": items[:_PARTIAL_EXTRACTION_LIMIT], "total": len(items),
            "truncated": len(items) > _PARTIAL_EXTRACTION_LIMIT}


def _ledger_rows(world: str, *, sig: str | None = None) -> list:
    """走査文書を台帳行にする（doc_id＝rel_path。原本 DL はパス基準で解決するので original_path は持たない）。

    `state="unreadable"` の文書は台帳の `status` にもそのまま反映する。
    `importance`/`importance_reason`/`importance_source` は、`GET /documents` の台帳高速経路が実走査せず返せるよう、
    ここで `importance.resolve_for_world` を1回だけ実行して materialize する（値が無ければ3キーとも付けない）。
    `root` と `files`（`scope_infer.safe_files(root)` の list）はここで1回だけ確定し、重要度解決と文書列挙に同じ `root`/`files`/`sig` を渡す
    （木を何度も歩かないため。`sig` は呼び出し元 `_run_locked` が確定済みの署名を渡せる）。
    """
    root = worlds.world_dir(world)
    if not root:
        return []
    files = list(scope_infer.safe_files(root, also=worlds.archives_dir(world)))
    res_map = importance.resolve_for_world(world, root=root, files=files, sig=sig)
    rows = []
    for d in corpus_docs.world_documents(world, root=root, files=files):
        status = "unreadable" if d.get("state") == "unreadable" else "indexed"
        row = {"name": d["name"], "layer": "version", "scope_path": d.get("top_scope"),
               "doctype": d.get("doctype"), "branch": d.get("branch"),
               "original_path": None, "md_path": d.get("md_path"), "status": status}
        res = res_map.get(d["name"])
        if res is not None:
            row["importance"] = res.value
            row["importance_source"] = f"{res.config_path}:{res.rule_line}行目"
            if res.reason:
                row["importance_reason"] = res.reason
        rows.append(row)
    return rows


def run(world, *, reflect=True, created_by="admin",
        scan_root=None, run_id=None, on_run_id=None, op: str = "sync") -> dict:
    """資料フォルダ1つ分の取り込み（台帳＋グラフ反映）を実行し、`ingest_runs` に記録して要約を返す。

    資料フォルダ単位の advisory lock で直列化する。`reflect=False` は Neo4j 反映を省く（DB 無し検証/テスト用）。
    `run_id` は呼び出し元が受付時に確保済みの `ingest_runs` 行を渡すときだけ指定する（省略時はここで確保する）。
    `on_run_id` は確保された run_id が判明した時点で呼ばれるコールバック。
    `op` は Webhook 通知の情報用途（sync/refresh/rebind/rerun）。
    """
    with store.world_lock(world):
        return _run_locked(world, reflect=reflect, created_by=created_by,
                           scan_root=scan_root, run_id=run_id, on_run_id=on_run_id, op=op)


def _run_locked(world, **kwargs) -> dict:
    """取り込み 1 回の本体。解決範囲の設定は始めに 1 回だけ読んで固定する（署名・グラフ・確定の記録が同じ設定を使う）。"""
    with resolve_settings.pinned(world):
        return _run_locked_body(world, **kwargs)


def _run_locked_body(world, *, reflect, created_by, scan_root, run_id=None, on_run_id=None,
                     op: str = "sync",
                     finalize: bool = True) -> dict:
    # lock 保持下で呼ぶ版。`worlds.rebind` はここを直接呼ぶ（`run` 経由だと同じ advisory lock を再入できず自己デッドロックする）。
    # last_sig はこの run の中で完結させる（他に書くのは `_wipe_locked` の pre-invalidate・`sync` の lock 内バックフィル・rebind 復旧の `restore_bind_invalidate_sig`）:
    #  - world 未解決（`sig is None`）は last_sig に触れず即 failed にする（`_record` の監査記録だけ書く）
    #  - 取り込み開始前に `''` で無効化する（pre-invalidate）。以後どこで失敗しても次回 sync は必ず再構築する
    #  - 正しい署名の確定は成功パスで `_record` が成功した後だけ。`reflect=False`（staging）は確定しない
    #  - 呼び出し元は lock 外で `set_world_sig` を後置き確定しない（他プロセスの無効化を復活させるため）
    # `run_id`: 呼び出し元が確保済みの `ingest_runs` 行を渡す（HTTP 経由は必ず確保済み）。省略時はここで開始時に INSERT する。
    # 行はスキャン開始前に確保する（即受付契約）。強制死で終端に届かなかった run は `extracting` のまま残り、起動時 lifespan が孤児として回収する。
    # `finalize=False`: 終端 UPDATE を書かず、戻り値の `_pending_finalize`（例外時は `e._sherpa_ingest_run_pending`）に終端引数を載せて返す
    # （`worlds.rebind` の複数段の呼び出しを、呼び出し元が最終結末を見て1回だけ terminal 化するため）。
    if run_id is None:
        run_row = store.start_ingest_run(world, scan_root=scan_root, created_by=created_by)
        run_id = run_row["id"]
    if on_run_id is not None:
        on_run_id(run_id)

    # 段ごとの開始・終了時刻。`_progress` の段遷移で確定し、run 終端でも `_close_stage_timing()` で閉じる
    stage_timings: dict = {}
    _stage_mono: dict = {}
    _current_stage = [None]

    def _close_stage_timing() -> None:
        stage = _current_stage[0]
        if stage is None or stage_timings.get(stage, {}).get("finished_at") is not None:
            return
        stage_timings[stage]["finished_at"] = datetime.now(timezone.utc).isoformat()
        stage_timings[stage]["elapsed_ms"] = round((time.monotonic() - _stage_mono[stage]) * 1000)

    def _progress(stage, done=None, total=None):
        if stage != _current_stage[0]:
            _close_stage_timing()
            now_iso = datetime.now(timezone.utc).isoformat()
            stage_timings[stage] = {"started_at": now_iso, "finished_at": None, "elapsed_ms": None}
            _stage_mono[stage] = time.monotonic()
            _current_stage[0] = stage
        try:
            store.update_ingest_run_progress(run_id, {
                "stage": stage, "stage_label": STAGE_LABELS.get(stage, stage),
                "done": done, "total": total,
                "updated_at": datetime.now(timezone.utc).isoformat()})
        except Exception:
            _log.warning(
                "進捗の記録に失敗しました（取り込み自体は継続）: world=%s stage=%s", world, stage, exc_info=True)

    _progress("scanning")
    # ① 走査（走査済み件数を逐次報告する。総数は走査完了まで不明）
    # このグラフが使う設定のハッシュ（署名より先に読む＝保存が割り込んでも「未反映」側へ倒れる）
    # 設定は取り込みの始めに 1 回だけ読んだスナップショット（`run_locked` の `resolve_settings.pinned`）で、署名・グラフの構築・確定の記録が同じ値を使う
    applied_resolve_sig = resolve_settings.signature_of(world)
    sig, manifest = world_state(world, progress=lambda n: _progress("scanning", done=n, total=None))

    nodes, edges, flags, rows = [], [], [], []              # 台帳行は派生 MD の作成後に確定する
    rows_known = [False]                                    # 台帳が確定したか（確定前の失敗 run は counts.targeted を出さない）

    def _record(status, reflected=None, ledger=0, extra_flags=(), drep=None,
               es_summary=None, neo4j_summary=None,
               confirm_sig=None, confirm_manifest=None, confirm_doc_count=None,
               confirm_scan_report=None):
        fl = list(flags) + list(extra_flags)
        snap = {"docs": len(rows), "nodes": len(nodes), "edges": len(edges), "flags": fl}
        if drep is not None:
            # office_md 段の要約（失敗した run でも、derive まで進んでいれば失敗ファイル一覧を残す）
            snap["office_md"] = _office_md_stage_summary(drep)
            snap["failed_files"] = _failed_files_summary(drep)
            snap["partial_extraction_suspected"] = _partial_extraction_summary(drep)
        if es_summary is not None:
            snap["es"] = es_summary
        if neo4j_summary is not None:
            snap["neo4j"] = neo4j_summary
        # run 終端で今開いている段を閉じてから記録する
        _close_stage_timing()
        if stage_timings:
            snap["stage_timings"] = {k: dict(v) for k, v in stage_timings.items()}
        counts = _counts_summary(drep, es_summary, manifest, rows if rows_known[0] else None,
                                 scan_rep=confirm_scan_report)
        if counts:
            snap["counts"] = counts
        pending = {"status": status, "extraction_snapshot": snap, "published_snapshot": reflected,
                  "source_doc_ids": [r["name"] for r in rows], "confirm_sig": confirm_sig,
                  "confirm_manifest": confirm_manifest, "confirm_doc_count": confirm_doc_count,
                  "confirm_scan_report": confirm_scan_report, "confirm_resolve_sig": applied_resolve_sig}
        if not finalize:
            # `finalize=False`: この行の DB 確定は呼び出し元に委ねる
            return {"world": world, "status": status, "ledger": ledger,
                    "nodes": len(nodes), "edges": len(edges), "flags": fl, "run": None,
                    "_pending_finalize": pending}
        # 開始時に確保済みの行（`run_id`）を完了状態へ UPDATE する。`confirm_sig` があるとき（成功確定パス）だけ、
        # run 完了と world 側の署名/manifest/doc_count/scan_report の確定を同一トランザクションで行う（重い計算は呼び出し前に済ませる）
        if confirm_sig is not None:
            rec = store.finish_ingest_run_and_confirm_world(
                run_id, world, status=status, extraction_snapshot=snap,
                published_snapshot=reflected, source_doc_ids=pending["source_doc_ids"],
                sig=confirm_sig, manifest=confirm_manifest, doc_count=confirm_doc_count,
                scan_report=confirm_scan_report, resolve_sig=applied_resolve_sig)
        else:
            rec = store.finish_ingest_run(run_id, status=status, extraction_snapshot=snap,
                                          published_snapshot=reflected,
                                          source_doc_ids=pending["source_doc_ids"])
        # terminal 化のこの1点から Webhook 通知を best-effort で発火する（取り込みの成否には影響させない）
        try:
            webhooks.notify_run_terminal(world, run_id, op, status, doc_count=len(rows))
        except Exception:
            _log.warning("Webhook 通知の起動に失敗しました（取り込み自体は継続）: world=%s", world,
                        exc_info=True)
        return {"world": world, "status": status, "ledger": ledger,
                "nodes": len(nodes), "edges": len(edges), "flags": fl, "run": rec}

    def _office_flags(d):                                   # 派生 MD のセットアップ失敗を flag にする
        """派生 MD の失敗を集約 warn の flag にして返す。per-file の詳細は `failed_files` を単一の出所にし、ここへは複製しない（JSONB の肥大化を避ける）。"""
        if not d.get("error"):
            return []
        return [{"doc": None, "action": "warn", "reason": f"office_md:{d['error']}"}]

    if sig is None:                                         # world 未解決＝mutation 前に即 failed
        flags = [{"doc": None, "action": "blocked", "reason": "world_unresolved"}]
        return _record("failed")

    store.set_world_sig(world, "")                          # pre-invalidate（ガード無し）

    # アーカイブ（zip/tar(.gz)/tgz）は原本に書かず、展開先（`worlds.archives_dir`）を原本側のアーカイブ集合と突き合わせて差分同期する。
    # 以降のグラフ構築・台帳行は展開先を合流させるため、それらより前に展開を終える。個別の失敗は展開結果サマリへ閉じ込め、取り込み全体は止めない。
    try:
        archive_extract.sync_world_archives(world, worlds.world_dir(world))
    except Exception:
        _log.warning("アーカイブの展開に失敗しました（取り込み自体は継続）: world=%s", world, exc_info=True)

    # ② 派生 MD（Office→決定的 MD）を先に作る（corpus_docs / ES が Office 項目定義表も参照できるように）→ ③ グラフ構築
    _progress("office_md", done=0, total=None)
    _last_office_progress_done = [None]

    def _office_progress(done, total):
        # `_PROGRESS_FILE_INTERVAL` 件ごとに間引く（先頭・末尾は必ず書く）
        last = _last_office_progress_done[0]
        if done in (0, total) or last is None or done - last >= _PROGRESS_FILE_INTERVAL:
            _last_office_progress_done[0] = done
            _progress("office_md", done=done, total=total)

    drep = _build_derived(world, world_sig=sig, progress=_office_progress)
    if drep.get("error"):
        # 派生生成が公開 Gate で拒否された（不完全な世代）ときは、ここで打ち切る。進むと旧派生を基に反映した上で新しい署名を確定してしまう。
        # 署名は確定せず、冒頭の pre-invalidate（''）のまま終了する
        return _record("failed", extra_flags=_office_flags(drep), drep=drep)
    # `build_world_graph` は時間がかかりうるので、入る前に段を「関係グラフを構築中」へ進める
    _progress("graph_build")
    nodes, edges, flags = build_world_graph(world)

    if any(f.get("action") == "blocked" for f in flags):    # blocked＝world 未解決 or 不可読コード
        # 反映も台帳書込もしない（部分グラフを確定しない）
        return _record("failed", extra_flags=_office_flags(drep), drep=drep)

    if not reflect:                                         # staging のみ（DB 無し検証/テスト）＝台帳だけ
        rows = _ledger_rows(world, sig=sig)
        rows_known[0] = True
        written = store.replace_documents(world, rows)
        # 署名は確定しない（last_sig は `''` のまま）。Neo4j 未反映を「同期済み」とみなさず、次の `sync(reflect=True)` が再構築する
        return _record("extracting", ledger=written, extra_flags=_office_flags(drep), drep=drep)

    # ④ グラフを資料フォルダ単位で atomic に置換する（`load_world`）。失敗時は tx がロールバックされ旧グラフが残り、台帳も書き換えない
    env = None
    neo4j_t0 = time.monotonic()
    try:
        env = world_neo4j._env()
        n, m = world_neo4j.load_world(nodes, edges, world, env["uri"], env["user"], env["pw"],
                                      plugin_failures=world_graph.plugin_failures_from_flags(flags))
    except Exception as e:
        # 失敗理由に接続先（ホスト:ポート）を含める（認証情報は含めない）。失敗した段も stage summary に所要時間とエラーを残す
        return _record("failed", extra_flags=[{"doc": None, "action": "blocked",
                       "reason": f"graph_reflect_failed:{e.__class__.__name__}@{_target_of(env['uri'] if env else '')}"}],
                       drep=drep,
                       neo4j_summary={"error": f"{e.__class__.__name__}@{_target_of(env['uri'] if env else '')}",
                                     "duration_sec": round(time.monotonic() - neo4j_t0, 3)})
    neo4j_duration_sec = time.monotonic() - neo4j_t0
    rows = _ledger_rows(world, sig=sig)                     # ⑤ 台帳（派生 .md ができた後）
    rows_known[0] = True
    try:
        written = store.replace_documents(world, rows)      # グラフ成功後に書く（不一致を残さない）
    except Exception as e:
        # PG の台帳 replace が失敗すると Neo4j は新・台帳は旧のまま残る。記録は best-effort で、last_sig は pre-invalidate 済みなので次回 sync が全再構築で自己修復する。
        # office_md／neo4j は完了済みなので要約を残し、`published_snapshot` も記録する（Neo4j には既に新世代が入っているため）
        _close_stage_timing()   # pg_replace 段の時刻をここで確定する
        pending = {"status": "failed", "source_doc_ids": [r["name"] for r in rows],
                  "extraction_snapshot": {"docs": len(rows), "nodes": len(nodes), "edges": len(edges),
                                          "flags": list(flags), "degraded": True,
                                          "stage": "pg_replace", "error": e.__class__.__name__,
                                          "office_md": _office_md_stage_summary(drep),
                                          "failed_files": _failed_files_summary(drep),
                                          "partial_extraction_suspected":
                                              _partial_extraction_summary(drep),
                                          "neo4j": {"nodes": n, "edges": m,
                                                   "duration_sec": round(neo4j_duration_sec, 3)},
                                          "stage_timings": {k: dict(v) for k, v in stage_timings.items()},
                                          "counts": _counts_summary(drep, None, manifest,
                                                                    rows if rows_known[0] else None)},
                  "published_snapshot": {"nodes": n, "edges": m},
                  "confirm_sig": None, "confirm_manifest": None, "confirm_doc_count": None,
                  "confirm_scan_report": None}
        if finalize:
            # 開始時に確保済みの行を UPDATE する（INSERT しない）
            try:
                store.finish_ingest_run(
                    run_id, status=pending["status"], source_doc_ids=pending["source_doc_ids"],
                    extraction_snapshot=pending["extraction_snapshot"],
                    published_snapshot=pending["published_snapshot"])
                e._sherpa_ingest_run_recorded = True   # 呼び出し元（_run_worker_or_503 等）の二重記録を防ぐ
                # `_record` を経由しないこの terminal 化でも通知する
                try:
                    webhooks.notify_run_terminal(world, run_id, op, pending["status"])
                except Exception:
                    _log.warning("Webhook 通知の起動に失敗しました: world=%s", world, exc_info=True)
            except Exception as record_exc:
                _log.warning(
                    "pg_replace 失敗の記録に失敗（best-effort・元の例外はそのまま re-raise）: %s", record_exc)
        else:
            # `finalize=False`: ここでは書かず、保留分を例外に添えて呼び出し元へ伝える
            e._sherpa_ingest_run_pending = pending
        raise
    extra = _office_flags(drep)
    _progress("es_index", done=0, total=None)
    _last_es_progress_done = [None]

    def _es_progress(done, total):
        # `_office_progress` と同じ間引き（`_PROGRESS_FILE_INTERVAL` 件ごと・先頭/末尾は必ず書く）。
        # es_index 側は既に doc グループ（flush）単位で呼ばれるため、ここは二重の安全弁。
        last = _last_es_progress_done[0]
        # 直前と同じ done は書かない
        if last == done:
            return
        if done in (0, total) or last is None or done - last >= _PROGRESS_FILE_INTERVAL:
            _last_es_progress_done[0] = done
            _progress("es_index", done=done, total=total)

    # ⑥ ES 索引。ES/reconcile は best-effort で、失敗は握りつぶさず flag 化する（取り込み自体は成功扱いのまま）
    try:
        # `content_sig` は冒頭で確定済みの `sig` をそのまま渡す（再計算すると全木の再走査になり、ABA で PG と ES の署名が食い違う）
        esr = es_index.index_world(world, content_sig=sig,
                                   progress=_es_progress)   # ES 全文索引（署名で鮮度管理・doc 単位進捗）
        if esr.get("error"):                                # delete/create/bulk 失敗は error dict（未接続は warn しない）
            extra.append({"doc": None, "action": "warn", "reason": f"es_index_failed:{esr['error']}"})
        elif esr.get("available") is True:
            # 全再構築は human_md も作り直すので、bulk 成功時だけ `.human_md_es_sig` を確定する（RAG_ES の設定に関わらず評価）
            wd = worlds.world_dir(world)
            dmd = worlds.derived_md_dir(world)
            if wd and dmd.exists():
                if office_md.confirm_human_md_es_sig(wd, dmd, world=world):
                    # ES 自身の `_meta.human_md_sig` も現行署名へ書き直す（`es_index.confirm_human_md_meta`）
                    if not es_index.confirm_human_md_meta(world):
                        extra.append({"doc": None, "action": "warn",
                                      "reason": "human_md_es_meta_confirm_failed"})
                else:
                    # confirm の失敗は flags へ反映する（成功で覆い隠さない）
                    extra.append({"doc": None, "action": "warn",
                                  "reason": "human_md_es_sig_marker_confirm_failed"})
    except Exception as e:
        esr = {"available": None, "error": f"{e.__class__.__name__}@{_target_of(es_index._url())}"}
        extra.append({"doc": None, "action": "warn",
                      "reason": f"es_index_failed:{e.__class__.__name__}@{_target_of(es_index._url())}"})
    _progress("finalize")     # ⑦ 仕上げ
    try:
        from .. import reconcile                            # 孤児派生物の自動掃除（registry が確実なときだけ）
        reconcile.reconcile_derivatives(reflect=reflect)
    except Exception as e:
        extra.append({"doc": None, "action": "warn", "reason": f"reconcile_failed:{e.__class__.__name__}"})
    status = "auto_published_with_flags" if (flags or extra) else "auto_published"
    # `esr` は `chunks`（bulk 対象件数）を既に持つので、`es_index.count()` は叩き直さない
    es_summary = {"available": esr.get("available") if isinstance(esr, dict) else None,
                 "error": esr.get("error") if isinstance(esr, dict) else None,
                 "chunks": esr.get("chunks") if isinstance(esr, dict) else None,
                 # counts の元データ（`esr` に無ければ None のまま）
                 "indexed": esr.get("indexed") if isinstance(esr, dict) else None,
                 "embedded": esr.get("embedded") if isinstance(esr, dict) else None,
                 "reused": esr.get("reused") if isinstance(esr, dict) else None,
                 "embed_elapsed_ms": esr.get("embed_elapsed_ms") if isinstance(esr, dict) else None}
    neo4j_summary = {"nodes": n, "edges": m, "duration_sec": round(neo4j_duration_sec, 3)}
    # 既知の残余: 確定する署名は冒頭スキャン時点のもので、各段が実際に読んだ内容と原子的には一致しない（取り込み中の ABA で次回 sync が unchanged と誤判定しうる）。
    # 恒久変化なら次回 sync の署名不一致で自己修復する。
    # scan_report は run 完了＋world 確定の同一トランザクションに含めるため、`_record` を呼ぶ前に計算する。計算の失敗は best-effort（`scan_rep=None` のまま渡し、前回値が残る）
    confirm_doc_count = None
    scan_rep = None
    if sig is not None:
        try:
            # `expected_rels`（冒頭の manifest の rel 集合）と実集合が食い違えば `scan_rep["document_count"]` は `None` になり、更新保留になる
            scan_rep = corpus_docs.scan_report(world, expected_rels=frozenset(manifest))
        except Exception:
            scan_rep = None
            _log.warning(
                "取り込み集計（scan_report）の計算に失敗しました（次回 status は前回値のまま）: "
                "world=%s", world, exc_info=True)
        # doc_count は外部公開 discovery（/ext/v1/capabilities）の事前集計値で、成功確定でだけ更新する（`None` は前回値を保持する）
        confirm_doc_count = scan_rep.get("document_count") if scan_rep is not None else None
    return _record(status, reflected={"nodes": n, "edges": m}, ledger=written, extra_flags=extra,
                  drep=drep, es_summary=es_summary, neo4j_summary=neo4j_summary,
                  confirm_sig=sig, confirm_manifest=manifest,
                  confirm_doc_count=confirm_doc_count, confirm_scan_report=scan_rep)


def _build_derived(world, *, world_sig: str | None = None, progress=None) -> dict:
    """資料フォルダの Office を決定的 MD にして派生領域へ作る（grep の検索対象になる）。未解決は no-op。`progress` は `office_md.build_derived` の per-file 進捗コールバック（done, total）へ転送する。"""
    wd = worlds.world_dir(world)
    if not wd:
        return {"converted": 0, "failed": 0, "unsupported": 0, "by_ext": {}}
    rep = office_md.build_derived(
        wd, worlds.derived_md_dir(world), world_sig=world_sig, progress=progress, world=world)
    if not rep.get("error") and not rep.get("ocr_routes_error") and _enqueue_ocr_refresh(world, world_sig):
        # ルート生成に成功し、公開済みで、refresh も積めたときだけ確定する（満たさなければ次回 sync が再試行する）
        office_md.write_ocr_route_sig_marker(worlds.derived_md_dir(world))
    return rep


def _enqueue_ocr_refresh(world, world_sig: str | None) -> bool:
    """公開できた派生物に対して、OCR の作り直しを1行だけ積む（既定 OFF・best-effort）。

    積むのは「この署名の派生物を OCR し直す」という指示だけで、読む画像の展開は隔離 worker が公開済みのルート（`.ocr_route.json`）を辿って行う。
    ここでの失敗は取り込みの失敗にしない。積めたら True、積まなかった/失敗したら False（呼び出し元がルート版マーカーの確定に使う）。
    """
    if not office_md.ocr_enabled() or not world_sig:
        return False
    try:
        from ..store import ocr_jobs
        from . import derived_generation, ocr_worker

        # 世代 ID は投入側と照合側で必ず同じ写像（`generation_id_for`）を使う
        ocr_jobs.enqueue_refresh_run(
            world, derived_generation.generation_id_for(world_sig), ocr_worker.profile_hash())
        return True
    except Exception:
        _log.warning(
            "OCR再実行のenqueueに失敗しました（取り込み自体は成功）: world=%s", world, exc_info=True)
        return False


def _derived_stale(world) -> bool:
    """派生 MD の作り直しが要るか（変更検知の無変化判定に併用）。

    派生ディレクトリが無いのに変換可能な Office があれば True。アーム構成（有効アーム＋PDF バックエンド）が変わったときも True。
    一度ビルドすればディレクトリは残るので無限ループしない。
    """
    dmd = worlds.derived_md_dir(world)
    if dmd.exists():
        return office_md.arms_sig_drift(dmd)             # アーム構成/PDF バックエンドの変化で作り直す
    wd = worlds.world_dir(world)
    if not wd:
        return False
    return any(rp.suffix.lower() in office_md.convertible_exts() for rp, _ in scope_infer.safe_files(wd))


def rerun(world, **kw) -> dict:
    """失敗/再取り込みのやり直し。資料フォルダ全体のクリーン rebuild を行う。"""
    kw.setdefault("op", "rerun")   # Webhook 通知の op（呼び出し側が明示すればそちらを優先）
    return run(world, **kw)


_SCAN_PROGRESS_INTERVAL = 500   # 走査進捗の報告間隔（ファイル数）


def _scan_dir(wd, progress=None) -> list:
    """`wd` 配下の対象ファイルを `(rel, mtime_ns, ctime_ns, size)` のソート済みリストで返す（stat のみで中身は読まない）。

    `progress`（省略可）は走査済みファイル数を `_SCAN_PROGRESS_INTERVAL` 件ごとと最後に報告する。"""
    parts = []
    for rp, rel in scope_infer.safe_files(wd):
        try:
            st = rp.stat()
            parts.append((rel, st.st_mtime_ns, st.st_ctime_ns, st.st_size))   # ctime も含める
        except OSError:
            parts.append((rel, None, None, None))
        if progress is not None and len(parts) % _SCAN_PROGRESS_INTERVAL == 0:
            progress(len(parts))
    if progress is not None:
        progress(len(parts))
    parts.sort()
    return parts


def _sig(parts, resolve_sig: str = "") -> str:
    # 重要度のスキーマ版・アナライザの有効構成・言及エッジの仕様版と実効値（`world_graph._mention_min_len`/`_mention_max_per_doc`）を材料に含める。
    # これらが変わると、ソースが不変でも署名が変わり全再構築される。
    # 資料フォルダの解決範囲の設定（`resolve_settings.signature_material`）があるときはそのハッシュも材料にする（設定が無ければ材料に足さない）
    material = (importance.IMPORTANCE_SCHEMA_VERSION, analyzer_registry.config_signature(),
                world_graph.MENTION_SCHEMA_VERSION, world_graph._mention_min_len(),
                world_graph._mention_max_per_doc(), parts)
    if resolve_sig:
        material += (("resolve_settings", resolve_sig),)
    return hashlib.sha1(repr(material).encode("utf-8")).hexdigest()


def _manifest(parts) -> dict:
    """`parts` → `rel -> [mtime_ns, ctime_ns, size]` の dict（差分チェックの基準・JSONB 保存用）。"""
    return {rel: [m, c, s] for (rel, m, c, s) in parts}


def world_signature_of_root(wd, resolve_sig: str = "") -> str:
    """解決済みの root（`Path`）から署名を計算する（`worlds.world_dir()` を再度呼ばない）。

    root 取得済みの呼び出し元はこちらを使う（再解決の間に rebind が起きると、古い root のスキャン結果を新しい root の署名で扱ってしまう）。
    """
    return _sig(_scan_dir(wd), resolve_sig)


def world_signature(world) -> str | None:
    """資料フォルダの安価な署名（各ファイルの rel/mtime/ctime/size の集約＋重要度スキーマ版＋アナライザ構成署名の SHA1・`_sig()`）。変更検知の基準。不在は None。"""
    wd = worlds.world_dir(world)
    return world_signature_of_root(wd, resolve_settings.signature_of(world)) if wd else None


def world_state(world, progress=None):
    """`(署名, ファイル明細)` を1スキャンで返す。資料フォルダ不在は `(None, None)`。`progress` は `_scan_dir` へ転送する。"""
    wd = worlds.world_dir(world)
    if not wd:
        return None, None
    parts = _scan_dir(wd, progress=progress)
    return _sig(parts, resolve_settings.signature_of(world)), _manifest(parts)


def diff_dir(wd, prev_manifest, prev_sig=None, resolve_sig: str = "") -> dict:
    """フォルダ現状と取り込み済み明細の差分を返す（read-only・グラフ/台帳/ES には書かない）。

    返値は `added`/`removed`/`changed`（rel のリスト）と `total`（現在のファイル数）・`indexed`（前回取込のファイル数）。
    `prev_manifest` が None/空なら未取り込み扱い（全ファイルが added）。ただし明細が未保存でも署名（`prev_sig`）が現状と一致すれば差分なし。
    """
    parts = _scan_dir(wd)
    cur = _manifest(parts)
    prev = prev_manifest or {}
    if not prev and prev_sig is not None and _sig(parts, resolve_sig) == prev_sig:
        return {"added": [], "removed": [], "changed": [], "total": len(cur), "indexed": len(cur)}
    added = sorted(r for r in cur if r not in prev)
    removed = sorted(r for r in prev if r not in cur)
    changed = sorted(r for r in cur if r in prev and list(cur[r]) != list(prev[r]))
    return {"added": added, "removed": removed, "changed": changed,
            "total": len(cur), "indexed": len(prev)}


def index_world_with_human_md_holdback(world: str, *, content_sig=None, settings: dict | None = None,
                                       run_id: int | None = None,
                                       progress: Callable[[int, int], None] | None = None) -> dict:
    """`es_index.index_world()` を、human_md の ES 反映ホールドバック込みで呼ぶ共通ヘルパ。

    ① 呼び出し前に `.human_md_es_sig` マーカーを無効化する（確定済みマーカーが残ると、bulk が部分失敗しても meta が確定値になり欠けた索引が固定されるため）。
       無効化に失敗（`OSError`）したら `index_world()` を呼ばず、失敗を記録して終える。
    ② bulk が成功（`available` かつ `error` なし）したときだけ `confirm_human_md_es_sig` でマーカーを再確定し、
       続けて `es_index.confirm_human_md_meta()` で ES 側の `_meta.human_md_sig` も現行署名へ書き直す（書かないと次回 `needs_reindex()` が収束しない）。
    `ingest_runs` へ `status="failed"` で記録するのは (a) マーカー無効化の失敗 (b) `index_world()` の失敗/未接続/例外 (c) マーカー書込の失敗。meta の書き直しの失敗は warning のみ。
    戻り値は `index_world()` の結果（呼ばなかった/例外時は `{"available": False, "error": ...}`）。
    `run_id`（`sync` の unchanged 分岐専用）を指定すると失敗しても新規 `ingest_runs` 行を作らない（呼び出し元が戻り値から受付 run を terminal 化する）。
    `progress` は `es_index.index_world()` へ転送する（`(done_docs, total_docs)`）。
    `_refresh_derived_representations`・`sync`（legacy 自己修復分岐）・`ocr_worker.reindex_observations` が共用する（全再構築の `_run_locked` は含めない）。
    """
    world_dir = worlds.world_dir(world)
    derived_md_dir = worlds.derived_md_dir(world)
    tracked = bool(world_dir and derived_md_dir.exists())
    if tracked and not office_md.drop_human_md_es_sig_marker(derived_md_dir):
        _record_es_index_failure(world, "human_md_es_sig_marker_drop_failed", run_id=run_id)
        return {"available": False, "error": "human_md_es_sig_marker_drop_failed"}
    try:
        esr = es_index.index_world(world, content_sig=content_sig, settings=settings, progress=progress)
    except Exception as e:
        _log.warning(
            "ES 再索引で例外が発生しました（次回 sync で再試行）: world=%s", world, exc_info=True)
        esr = {"available": False, "error": e.__class__.__name__}
    ok = esr.get("available") is True and not esr.get("error")
    failure_reason = None
    if not ok:
        failure_reason = esr.get("error") or "unavailable"
    elif tracked and not office_md.confirm_human_md_es_sig(world_dir, derived_md_dir, world=world):
        failure_reason = "human_md_es_sig_marker_write_failed"
    elif tracked and not es_index.confirm_human_md_meta(world):
        _log.warning(
            "ES `_meta.human_md_sig` の書き直しに失敗しました（次回 sync まで human_md 次元が"
            "収束しない可能性）: world=%s", world)
    if failure_reason is not None:
        _record_es_index_failure(world, failure_reason, run_id=run_id)
    return esr


def _record_es_index_failure(world: str, reason: str, *, run_id: int | None = None) -> None:
    """ES 反映失敗を記録する。`run_id` 指定時は何もしない（呼び出し元が戻り値から受付 run 自身を terminal 化する）。"""
    if run_id is not None:
        return
    try:
        store.add_ingest_run(
            world, status="failed",
            extraction_snapshot={"stage": "es_index", "error": reason,
                                 # 接続できないだけ（unavailable）は失敗の理由ではなく未接続として残す
                                 "es": {"available": False,
                                        "error": None if reason == "unavailable" else reason}},
            created_by="admin")
    except Exception:
        _log.warning(
            "ES 反映失敗の ingest_runs 記録に失敗しました: world=%s", world, exc_info=True)


def _merge_es_runs(prev_summary, prev_timing, new_summary, new_timing):
    """同一 run で ES 再索引が 2 回走ったときの統計を合成する。`prev_*` が None なら `new_*` をそのまま返す。

    所要時間と埋め込み（`embedded`・`reused`・`embed_elapsed_ms`）は累積、`started_at` は初回、`finished_at` は最終、
    索引件数（`indexed`・`chunks`）と状態（`available`・`error`）は最終回の値。"""
    if prev_summary is None and prev_timing is None:
        return new_summary, new_timing
    def _add(a, b):
        if a is None and b is None:
            return None
        return (a or 0) + (b or 0)
    summary = dict(new_summary)
    if prev_summary:
        summary["embedded"] = _add(prev_summary.get("embedded"), new_summary.get("embedded"))
        summary["reused"] = _add(prev_summary.get("reused"), new_summary.get("reused"))
        summary["embed_elapsed_ms"] = _add(prev_summary.get("embed_elapsed_ms"), new_summary.get("embed_elapsed_ms"))
    timing = dict(new_timing)
    if prev_timing:
        timing["started_at"] = prev_timing.get("started_at") or new_timing.get("started_at")
        timing["elapsed_ms"] = _add(prev_timing.get("elapsed_ms"), new_timing.get("elapsed_ms"))
    return summary, timing


def _refresh_derived_representations(world, sig) -> tuple[str | None, dict | None, str | None]:
    """`sync()` の軽量再生成分岐（human_md/document_ir/evidence/rag の drift のみで、arms drift・force・原本変化は無い）。

    呼び出し元は `store.world_lock` の中で呼ぶ（derived への書込を並行の `run()`/`sync()` と競合させない）。
    戻り値は `(status, es_refresh_info, failure_reason)`。
    - `status`: sidecar 欠落を検知したら `"needs_full_run"`（呼び出し元が同じ lock 区間で `_run_locked()` を直接呼ぶ）。
      drift が無ければ `None`。軽量再生成を実行したら `"handled"`（全て成功）か `"rag_failed"`（`failure_reason` に詳細）。
    - `es_refresh_info`: ここで ES 再索引を実行したときだけ `{"summary", "stage_timing"}`（呼び出し元の明示 ES 自己修復と同形）。呼び出し元が `counts`/`stage_timings` へ畳み込む。
    呼び出し元は `"handled"` でも backfill/ES 自己修復をスキップしない（human_md が書き換える `{rel}.md` は legacy 縮退で ES の索引元になりうる）。

    手順:
    ① sidecar（`.md`/`.md.meta.json`/`.evidence.json`）の欠落確認を、drift の有無に関わらず必ず先に行い、欠落なら全再構築へ回す。
    ② human_md drift → `refresh_human_md`（`{rel}.md` だけの軽量再生成）。他の drift とは独立に必ず個別に確認・実行する。
    ③ document_ir drift → `refresh_document_ir`（全 OOXML 文書を対象に再生成）。続けて ④ evidence→rag→（RAG_ES 有効時は）ES 反映まで連鎖させる。
    ⑤ ③を経ない場合の evidence drift → `refresh_evidence_ir`（evidence→rag も）。⑥ ③⑤を経ない場合の rag drift → `refresh_rag`。

    守ること:
    - document_ir を再生成したら、evidence/rag の drift 判定によらず必ず連鎖再生成する。
    - `refresh_document_ir` が1文書失敗しても連鎖は止めない（`error` キー＝構造的な setup 失敗のときだけ止める）。
      `.document_ir_sig` は資料フォルダ単位で1つなので、失敗が残る限り次回も全件を再実行する。
    - `.document_ir_sig` は `write_document_ir_sig_marker=False` で呼び、連鎖した evidence/rag（と ES 反映）の成功を確認してから
      `write_document_ir_sig_marker()` で確定する（先に確定すると再試行の入口が失われる）。
    """
    wd = worlds.world_dir(world)
    dmd = worlds.derived_md_dir(world)
    if not wd or not dmd.exists():                      # text/code のみの資料フォルダは評価対象が無い
        return None, None, None
    if office_md.rag_sidecars_missing(wd, dmd, world=world):   # ① drift の有無によらず常に確認する
        return "needs_full_run", None, None
    # ② human_md drift は他の drift と独立（rag/ES には触れない）。排他分岐の外で必ず確認する
    human_md_handled = False
    human_md_failure_reason = None                       # 失敗しても以降の drift 判定は続行する
    if office_md.human_md_sig_drift(wd, dmd, world=world):
        hm_result = office_md.refresh_human_md(wd, dmd, world=world)
        failed = hm_result.get("human_md_failed", 0)
        if failed:
            human_md_failure_reason = f"human_md_refresh_failed:{failed}"
            _log.warning(
                "human_md の軽量再生成で一部の文書が失敗しました（次回 sync で再試行）: "
                "world=%s detail=%s", world, hm_result)
        human_md_handled = True
    # OCR ルート版の drift は Evidence/rag と独立（ルートだけ書き直し、ES には触れない）。読めない画像形式になった入力の過去の job もここで終端する。
    # マーカーは、全件の書き直し・終端と OCR refresh の enqueue が成功した後にだけ確定する
    if office_md.ocr_route_refresh_needed(dmd):
        try:
            active_sig = (dmd / office_md._WORLD_SIG_MARKER).read_text(encoding="utf-8").strip()
        except OSError:
            active_sig = ""
        if active_sig:
            from . import derived_generation
            route_result = office_md.refresh_ocr_routes(
                dmd, world=world, generation_id=derived_generation.generation_id_for(active_sig))
            if not route_result.get("ocr_routes_failed") and _enqueue_ocr_refresh(world, active_sig):
                office_md.write_ocr_route_sig_marker(dmd)
        human_md_handled = True
    document_ir_drift = office_md.document_ir_sig_drift(dmd)
    evidence_drift = office_md.evidence_ir_sig_drift(dmd)
    # `rag_sig_drift` は OCR 観測次元を含む。OCR が新しい観測世代を公開していれば evidence が不変でも `refresh_rag` を誘発する
    rag_drift = office_md.rag_sig_drift(dmd, world=world)
    if not document_ir_drift and not evidence_drift and not rag_drift:
        if human_md_failure_reason:
            return "rag_failed", None, human_md_failure_reason
        return ("handled" if human_md_handled else None), None, None
    document_ir_ok = True                                # document_ir を経由しない経路では真のまま
    if document_ir_drift:
        doc_result = office_md.refresh_document_ir(wd, dmd, write_document_ir_sig_marker=False, world=world)
        if doc_result.get("error"):                      # 構造的な setup 失敗＝1文書も処理できていない
            _log.warning(
                "document_ir の軽量再生成に失敗しました（次回 sync で再試行）: world=%s detail=%s",
                world, doc_result)
            reason = f"document_ir_refresh_failed:{doc_result.get('error')}"
            if human_md_failure_reason:
                reason = f"{human_md_failure_reason};{reason}"
            return "rag_failed", None, reason
        if doc_result.get("document_ir_failed", 0):
            document_ir_ok = False                       # マーカーは未確定のまま・今回の evidence/rag 連鎖は継続する
            _partial = f"document_ir_refresh_failed:{doc_result.get('document_ir_failed')}"
            human_md_failure_reason = (f"{human_md_failure_reason};{_partial}"
                                       if human_md_failure_reason else _partial)   # 部分失敗も run の終端へ引き継ぐ
            _log.warning(
                "document_ir の軽量再生成で一部の文書が失敗しました（マーカーは world 単位のため"
                "全 OOXML 文書を対象に次回 sync も再実行されます・今回分の evidence/rag への"
                "連鎖は継続します）: world=%s detail=%s", world, doc_result)
    if document_ir_drift or evidence_drift:
        result = office_md.refresh_evidence_ir(wd, dmd, write_rag_sig_marker=False, world=world)
        ok = not result.get("error") and result.get("evidence_ir_failed", 0) == 0 \
            and result.get("rag_failed", 0) == 0
    else:
        result = office_md.refresh_rag(wd, dmd, write_rag_sig_marker=False, world=world)
        ok = not result.get("error") and result.get("rag_failed", 0) == 0
    if not ok:
        _log.warning(
            "RAG/Evidence IR の軽量再生成に失敗しました（次回 sync で再試行）: world=%s detail=%s",
            world, result)
        reason = f"rag_refresh_failed:{result.get('error') or result.get('rag_failed')}"
        if human_md_failure_reason:
            reason = f"{human_md_failure_reason};{reason}"
        return "rag_failed", None, reason
    # rag.md が書き換わったので、ES 反映の成否に関わらずグラフ（言及エッジ）を追いつかせる（`store.world_lock` 保持中の呼び出し元から lock-free ヘルパーを呼ぶ）
    _reflect_graph_after_rag_rewrite(world)
    # human_md は RAG_ES の設定に関わらず ES の索引内容に影響しうるため、共通ヘルパが `.human_md_es_sig` の無効化/確定/失敗記録まで面倒を見る
    _es_t0 = time.monotonic()
    _es_started_at = datetime.now(timezone.utc).isoformat()
    esr = index_world_with_human_md_holdback(world, content_sig=sig)
    _es_finished_at = datetime.now(timezone.utc).isoformat()
    es_ok = esr.get("available") is True and not esr.get("error")
    # 呼び出し元の明示 ES 自己修復と同形（呼び出し元が `counts`/`stage_timings` へ畳み込む）
    es_refresh_info = {
        "summary": {
            "available": esr.get("available") if isinstance(esr, dict) else None,
            "error": esr.get("error") if isinstance(esr, dict) else None,
            "chunks": esr.get("chunks") if isinstance(esr, dict) else None,
            "indexed": esr.get("indexed") if isinstance(esr, dict) else None,
            "embedded": esr.get("embedded") if isinstance(esr, dict) else None,
            "reused": esr.get("reused") if isinstance(esr, dict) else None,
            "embed_elapsed_ms": esr.get("embed_elapsed_ms") if isinstance(esr, dict) else None,
        },
        "stage_timing": {
            "started_at": _es_started_at,
            "finished_at": _es_finished_at,
            "elapsed_ms": round((time.monotonic() - _es_t0) * 1000),
        },
    }
    if es_ok:
        office_md.write_rag_sig_marker(dmd, world=world)
    else:
        _log.warning(
            "RAG refresh後のES再索引が失敗しました（次回 sync で再試行）: world=%s", world)
    # document_ir マーカーは、document_ir 自体が全件成功し、連鎖した evidence/rag（と該当すれば ES 反映）も成功してから確定する
    if document_ir_drift and document_ir_ok and es_ok:
        office_md.write_document_ir_sig_marker(dmd)
    if human_md_failure_reason:                          # evidence/rag/ES は成功したが human_md/document_ir の一部が残った
        return "rag_failed", es_refresh_info, human_md_failure_reason
    return "handled", es_refresh_info, None


def sync(world, *, reflect=True, force=False, run_id=None, on_run_id=None, op: str = "sync") -> dict:
    """`_sync_impl` の薄いラッパー。成功後に rag.md の LLM 成形を背景で後追い起動する（`llm_render.schedule_background`）。

    資料フォルダが解決できなかった（`status="unavailable"`）場合と、`SHERPA_TEST_DB_ISOLATED`（pytest 実行中に立つ内部フラグ）が立っている間は起動しない。
    背景起動の失敗は best-effort で、`sync()` の戻り値・例外には影響させない。
    """
    with resolve_settings.pinned(world):        # 変更なしの分岐（グラフの自己修復）も含め、1 回の sync は同じ設定を使う
        result = _sync_impl(world, reflect=reflect, force=force, run_id=run_id, on_run_id=on_run_id, op=op)
    if result.get("status") != "unavailable" and not os.environ.get("SHERPA_TEST_DB_ISOLATED"):
        try:
            from . import llm_render
            llm_render.schedule_background(world, _llm_render_pass)
        except Exception:
            _log.warning(
                "LLM 成形の背景起動に失敗しました（次回 sync で再試行）: world=%s", world, exc_info=True)
    return result


def _sync_impl(world, *, reflect=True, force=False, run_id=None, on_run_id=None,
               op: str = "sync") -> dict:
    """変更検知つき取り込み（「今すぐ更新」・ポーリング・登録時のリラン用）。変わったときだけ再ビルドする。

    署名が前回と同じ（かつ `force=False`）なら no-op（`changed=False`）、違えば `run` する。
    署名不変でも human_md/document_ir/evidence/rag の版だけが drift した場合は `_refresh_derived_representations` の軽量再生成を経由する。
    その後（`"handled"` でもスキップせず）の ES 自己修復（`es_index.needs_reindex`→`index_world`）が成功したら `.human_md_es_sig` を確定し、
    部分失敗時は確定せず `store.add_ingest_run(status="failed")` で監査に残す（次回 sync が再試行する）。
    グラフも同じ不変分岐で自己修復する（`world_neo4j.check_graph_counts` が世代/件数の食い違いを検知したら `_reflect_graph_after_rag_rewrite` で作り直す）。
    `reflect=False` では Neo4j に触れないため照合・修復も行わない。

    署名の確定は `_run_locked`（`run` 経由・world_lock 保持中）だけが行う。ここでは `run` 復帰後（lock 解放後）に確定を書き足さない（他プロセスの無効化を復活させるため）。
    `run_id` は呼び出し元が受付時に確保済みの `ingest_runs` 行。`_run_locked` を経由する分岐はそのまま転送し、`_run_locked` に到達しない分岐
    （資料フォルダ未解決／完全な unchanged）は `sync` 自身が最後に `run_id` を terminal 化する（`extracting` のまま残さない）。
    `on_run_id` は `_run_locked` 経由の分岐で run_id が判明したときに呼ばれるコールバック。
    """
    def _finalize_if_unused(status: str, reasons: list[str] | None = None,
                            stage_timings: dict | None = None, counts: dict | None = None,
                            es_summary: dict | None = None) -> None:
        # `_run_locked` を経由しない終了点専用（呼び出し元の run_id を未消化のまま残さない）
        if run_id is None:
            return
        try:
            snap = {"changed": False}
            if reasons:
                snap["flags"] = [{"doc": None, "action": "warn", "reason": r} for r in reasons]
            # unchanged でも走査/自己修復した工程があれば、所要時間（`stage_timings`）と取得できた計数（`counts`・取れない項目はキーごと省略）を残す
            if stage_timings:
                snap["stage_timings"] = stage_timings
            if counts:
                snap["counts"] = counts
            # 実際に ES を張り直した run（`es_summary` あり）だけ `extraction_snapshot.es` に残す（`_record` と同じキー・同じ形）。張り直さなかった run には置かない
            if es_summary is not None:
                snap["es"] = es_summary
            store.finish_ingest_run(run_id, status=status, extraction_snapshot=snap)
        except Exception:
            _log.warning(
                "sync の unchanged/unresolved run 確定に失敗しました（best-effort）: world=%s run_id=%s",
                world, run_id, exc_info=True)
            return
        # `_run_locked`（`_record`）を経由しないこの terminal 化でも Webhook 通知を発火する
        try:
            webhooks.notify_run_terminal(world, run_id, op, status)
        except Exception:
            _log.warning("Webhook 通知の起動に失敗しました（sync 自体は継続）: world=%s", world,
                        exc_info=True)

    def _progress(stage, done=None, total=None):
        # `_run_locked` の同名クロージャと同じ形。unchanged 分岐は `_run_locked` を経由しないためここで進捗を配線する。`run_id` が無い分岐は no-op
        if run_id is None:
            return
        try:
            store.update_ingest_run_progress(run_id, {
                "stage": stage, "stage_label": STAGE_LABELS.get(stage, stage),
                "done": done, "total": total,
                "updated_at": datetime.now(timezone.utc).isoformat()})
        except Exception:
            _log.warning(
                "進捗の記録に失敗しました（sync 自体は継続）: world=%s stage=%s", world, stage, exc_info=True)

    # ES 自己修復の progress は、`_run_locked` の `_es_progress` と同じ間引き（`_PROGRESS_FILE_INTERVAL` 件間隔・先頭/末尾は必ず書く）を掛けてから `_progress` へ渡す
    _last_unchanged_es_progress_done = [None]

    def _unchanged_es_progress(done, total):
        last = _last_unchanged_es_progress_done[0]
        if last == done:              # 同値は書かない
            return
        if done in (0, total) or last is None or done - last >= _PROGRESS_FILE_INTERVAL:
            _last_unchanged_es_progress_done[0] = done
            _progress("es_index", done=done, total=total)

    # unchanged 分岐が実行した工程の所要時間を集める（`_finalize_if_unused` へ渡す・実行した段だけ載る）
    _stage_timings: dict = {}
    _progress("scanning")
    _t_scan0 = time.monotonic()
    _scan_started_at = datetime.now(timezone.utc).isoformat()
    sig, manifest = world_state(world, progress=lambda n: _progress("scanning", done=n, total=None))
    _stage_timings["scanning"] = {
        "started_at": _scan_started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": round((time.monotonic() - _t_scan0) * 1000),
    }
    if sig is None:
        _finalize_if_unused("failed", ["world_unresolved"], stage_timings=_stage_timings)
        return {"world": world, "changed": False, "status": "unavailable"}
    row = store.get_world(world)
    prev = row.get("last_sig") if row else None
    if not force and prev == sig and not _derived_stale(world):  # 無変化＝再ビルドしない（派生 MD 欠落時は除く）
        # unchanged 分岐の全て（軽量再生成・バックフィル・グラフ/ES 自己修復・マーカー確定）を単一の `store.world_lock` 区間に収める
        # （区間を分けると並行の sync/rebind/delete が割り込み、反映が参照した派生物と世代が食い違う）。
        # lock は再入できない（別コネクションの自己デッドロック）ので、区間内で `store.world_lock` を取り直さない
        with store.world_lock(world):                    # derived への書込を同一資料フォルダの並行 run/sync と直列化
            _t_refresh0 = time.monotonic()
            _refresh_started_at = datetime.now(timezone.utc).isoformat()
            refresh_outcome, refresh_es_info, refresh_failure_reason = _refresh_derived_representations(world, sig)
            _stage_timings["refresh_derived"] = {
                "started_at": _refresh_started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": round((time.monotonic() - _t_refresh0) * 1000),
            }
            if refresh_outcome == "needs_full_run":
                # 欠落検知→全再構築→`.rag_sig` 削除を同一 lock 区間で行う。`run()` は非再入 lock を取り直すので、lock-free 版の `_run_locked` を直接呼ぶ
                res = _run_locked(world, reflect=reflect, created_by="admin", scan_root=None,
                                  run_id=run_id, on_run_id=on_run_id, op=op)
                if not office_md.drop_rag_sig_marker(worlds.derived_md_dir(world)):
                    _log.warning(
                        "sidecar欠落からの全再構築後、`.rag_sig`の削除に失敗しました"
                        "（ES再索引の再試行契機を逃す可能性）: world=%s", world)
                return {"world": world, "changed": True, "status": res["status"],
                        "ledger": res["ledger"], "flags": list(res.get("flags", []))}
            # `refresh_outcome == "handled"` でもここで早期 return しない（human_md の軽量再生成は legacy `{rel}.md`＝ES の索引元を書き換えるため、同じ呼び出し内で `needs_reindex` 自己修復まで到達させる）。
            # 軽量再生成で失敗した rel は次回 sync の drift 判定が再試行する
            # `last_manifest` は空の資料フォルダでは `{}` が正当な値なので、欠落の判定は `is None` で行う
            needs_manifest_backfill = row is not None and row.get("last_manifest") is None
            # `last_doc_count` が NULL の資料フォルダは、unchanged のままだと `document_count` が null のままになるためバックフィルする
            needs_doc_count_backfill = row is not None and row.get("last_doc_count") is None
            # last_scan_report も同様にバックフィルする（`scan_report()` の項目が不足する形式も `corpus_docs.scan_report_missing_fields` で検出して更新する）
            _cur_scan_report = row.get("last_scan_report") if row is not None else None
            needs_scan_report_backfill = row is not None and (
                _cur_scan_report is None or corpus_docs.scan_report_missing_fields(_cur_scan_report))
            if needs_manifest_backfill or needs_doc_count_backfill or needs_scan_report_backfill:
                # 外側の `with` で lock 保持中なので `store.world_lock` を取り直さない（再入の自己デッドロック）
                cur = store.get_world(world)                    # 他の writer が割り込んでいないか再読する
                # root は1回だけ解決し、以降の doc_count 集計へ渡す（`manifest_doctype_count_from_root`）
                backfill_root = worlds.world_dir(world)
                if cur is not None and cur.get("last_sig") == sig:
                    if cur.get("last_manifest") is None and cur.get("last_doc_count") is None:
                        # 両方 NULL は1回の UPDATE でまとめて補完する（`last_synced_at` は変えない）
                        store.backfill_manifest_and_doc_count(
                            world, manifest,
                            corpus_docs.manifest_doctype_count_from_root(manifest, backfill_root), sig)
                    else:
                        if cur.get("last_manifest") is None:
                            store.set_world_sig(world, sig, manifest=manifest)
                            cur = store.get_world(world)         # `last_manifest` が埋まった最新行を使い直す
                        if cur.get("last_doc_count") is None:
                            saved_manifest = cur.get("last_manifest")
                            if saved_manifest is None:
                                saved_manifest = manifest
                            # `last_synced_at` は更新しない
                            store.backfill_doc_count(
                                world,
                                corpus_docs.manifest_doctype_count_from_root(saved_manifest, backfill_root),
                                sig)
                    if cur.get("last_scan_report") is None or corpus_docs.scan_report_missing_fields(cur.get("last_scan_report")):
                        # sig 一致を確認済みの区間内なので、この世代に対する scan_report として正当（`last_synced_at` は更新しない）
                        try:
                            store.set_scan_report(world, corpus_docs.scan_report(world))
                        except Exception:
                            _log.warning(
                                "取り込み集計（scan_report）のバックフィルに失敗しました: world=%s",
                                world, exc_info=True)
                # sig が不一致（他プロセスが無効化/更新済み）なら何もしない（上書きしない）。次回 sync が実際の状態を判定して収束する
            # グラフ修復: 世代不一致・件数スタンプ欠落・実物との件数不一致（`check_graph_counts`）を検知したら既存の派生物から作り直す（埋め込み/LLM は呼ばない）。
            # 例外は握りつぶさず `_finalize_reasons` へ積んで run を failed にする。`reflect=False` は Neo4j に触れないので照合も修復もしない
            graph_repair_failure = None
            if reflect:
                try:
                    genv = world_neo4j._env()
                    graph_repair_reason = world_neo4j.check_graph_counts(
                        world, genv["uri"], genv["user"], genv["pw"])
                    if graph_repair_reason is not None:
                        _t_graph0 = time.monotonic()
                        _graph_repair_started_at = datetime.now(timezone.utc).isoformat()
                        _reflect_graph_after_rag_rewrite(world)
                        _stage_timings["graph_repair"] = {
                            "started_at": _graph_repair_started_at,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                            "elapsed_ms": round((time.monotonic() - _t_graph0) * 1000),
                        }
                except Exception as e:
                    _log.warning(
                        "グラフ自己修復中に予期しない例外が発生しました: world=%s", world, exc_info=True)
                    graph_repair_failure = e.__class__.__name__
            # ES 修復: 空/署名ズレ/埋め込みプロバイダ変更を検知して張り直す。失敗は別 run を作らず、`run_id` を渡して受付 run 自身の終端へ畳み込む
            es_repair_failure = None
            # `_refresh_derived_representations` が既に ES 再索引を実行していれば、その結果を引き継ぐ（畳み込まないと索引件数・工程時間が欠落する）
            es_summary = refresh_es_info["summary"] if refresh_es_info is not None else None
            if refresh_es_info is not None:
                _stage_timings["es_index"] = refresh_es_info["stage_timing"]
            try:
                if es_index.needs_reindex(world, sig):
                    _progress("es_index", done=0, total=None)
                    _t_es0 = time.monotonic()
                    _es_started_at = datetime.now(timezone.utc).isoformat()
                    esr = index_world_with_human_md_holdback(
                        world, content_sig=sig, run_id=run_id,
                        progress=_unchanged_es_progress)
                    _outer_timing = {
                        "started_at": _es_started_at,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": round((time.monotonic() - _t_es0) * 1000),
                    }
                    if not (esr.get("available") is True and not esr.get("error")):
                        es_repair_failure = esr.get("error") or "unavailable"
                    # `_record` の es_summary と同形（counts の元データ）
                    _outer_summary = {"available": esr.get("available") if isinstance(esr, dict) else None,
                                      "error": esr.get("error") if isinstance(esr, dict) else None,
                                      "chunks": esr.get("chunks") if isinstance(esr, dict) else None,
                                      "indexed": esr.get("indexed") if isinstance(esr, dict) else None,
                                      "embedded": esr.get("embedded") if isinstance(esr, dict) else None,
                                      "reused": esr.get("reused") if isinstance(esr, dict) else None,
                                      "embed_elapsed_ms": esr.get("embed_elapsed_ms") if isinstance(esr, dict) else None}
                    # 内部再索引の後に外側の再索引も走った場合は置換せず合成する（`_merge_es_runs`）
                    es_summary, _stage_timings["es_index"] = _merge_es_runs(
                        es_summary, _stage_timings.get("es_index"), _outer_summary, _outer_timing)
            except Exception as e:
                _log.warning(
                    "ES 自己修復中に予期しない例外が発生しました: world=%s", world, exc_info=True)
                es_repair_failure = e.__class__.__name__
            # drep（office_md 段別要約）／rows（台帳）は unchanged 分岐に無いので `_counts_summary` がキーごと省略する
            _counts = _counts_summary(None, es_summary, manifest, None)
            # `refresh_outcome == "rag_failed"` は ES 自己修復の成否と独立に run を failed にする（未反映のまま `auto_published` と記録しないため）
            _finalize_reasons = []
            if refresh_outcome == "rag_failed":
                _finalize_reasons.append(f"rag_refresh_failed:{refresh_failure_reason}")
            if graph_repair_failure is not None:
                _finalize_reasons.append(f"graph_repair_failed:{graph_repair_failure}")
            if es_repair_failure is not None:
                _finalize_reasons.append(f"es_repair_failed:{es_repair_failure}")
            if _finalize_reasons:
                _finalize_if_unused("failed", _finalize_reasons,
                                    stage_timings=_stage_timings, counts=_counts, es_summary=es_summary)
            else:
                _finalize_if_unused("auto_published", stage_timings=_stage_timings, counts=_counts,
                                    es_summary=es_summary)
            return {"world": world, "changed": False, "status": "unchanged", "ledger": 0}
    # `op` を渡して Webhook payload の `op` を呼び出し元の種別にそろえる
    res = run(world, reflect=reflect, run_id=run_id, on_run_id=on_run_id, op=op)   # 署名の確定/無効化は run 内部（`_run_locked`）が lock 内で行う
    return {"world": world, "changed": True, "status": res["status"],
            "ledger": res["ledger"], "flags": list(res.get("flags", []))}


def _reindex_after_rag_rewrite(world: str) -> bool:
    """rag.md が世代を変えずに書き換わった（LLM 成形の反映・規則版への一掃）後、ES へ載せ直す。

    `store.world_lock` 区間の中で、`_refresh_derived_representations` の holdback 分岐と同じ順序（`.rag_sig` を先に落とし、
    `index_world_with_human_md_holdback` の bulk 成功でだけ確定）で行う。RAG_ES が無効なら ES には触れず成功扱いにする。
    グラフ反映（`_reflect_graph_after_rag_rewrite`）は RAG_ES の有無に関わらず常に行う。
    """
    row = store.get_world(world)
    sig = row.get("last_sig") if row else None
    if not sig:
        return False
    dmd = worlds.derived_md_dir(world)
    with store.world_lock(world):
        # 解決範囲の設定がまだ反映されていない（保存後の全件の取り込み待ち）なら、グラフだけを今の設定で作り直さない
        # （署名・台帳・ES は旧い設定のまま＝グラフだけが先に進むと食い違う）。全件の取り込みに任せる。
        if resolve_settings.pending(store.get_world(world)):
            _log.info("解決範囲の設定が未反映のため、LLM 成形後のグラフの反映を見送ります（次の取り込みで反映）: world=%s", world)
        else:
            _reflect_graph_after_rag_rewrite(world)
        if not office_md.drop_rag_sig_marker(dmd):
            _log.warning(
                "LLM 成形反映後、`.rag_sig` の無効化に失敗しました（ES 再索引を見送ります）: world=%s",
                world)
            return False
        esr = index_world_with_human_md_holdback(world, content_sig=sig)
        ok = esr.get("available") is True and not esr.get("error")
        if ok:
            office_md.write_rag_sig_marker(dmd, world=world)
        else:
            _log.warning(
                "LLM 成形反映後の ES 再索引に失敗しました（次回 sync の自己修復に委ねます）: world=%s",
                world)
        return ok


def _llm_render_pass(world: str) -> None:
    """`sync()` 成功後に背景 thread から呼ばれる LLM 成形の1回分。

    `llm_render.run_world_pass` がファイル書込までを担い、書き換わった rel が1件でもあればここで ES への反映（`_reindex_after_rag_rewrite`）まで行う。
    個々のファイル書込は `store.world_lock` を取らない（LLM 呼び出しで長時間になるため。書込自体は `write_text_atomic` で原子的）。
    """
    from . import llm_render
    result = llm_render.run_world_pass(world)
    if result.changed_rels:
        _reindex_after_rag_rewrite(world)


def regenerate_rag_rule_only(world: str) -> dict:
    """当該資料フォルダの LLM 成形キャッシュを一掃し、rag.md を規則版へ作り直す（管理者の明示操作「規則版で再生成」）。

    `office_md.refresh_rag`（Evidence IR から決定的に再生成する経路）を呼び直し、書込時に必ず `生成手段: 規則` が刻まれる（`_stamp_rule_only_rag_markdown`）。
    `store.world_lock` はここで1区間として確保する。トグルが ON のままなら次の背景パスが再び LLM 成形を試みうる。
    """
    from . import llm_render
    wd = worlds.world_dir(world)
    dmd = worlds.derived_md_dir(world)
    if not wd or not dmd.exists():
        return {"status": "unavailable"}
    llm_render.clear_cache(world)
    with store.world_lock(world):
        result = office_md.refresh_rag(wd, dmd, write_rag_sig_marker=False, world=world)
    if result.get("error") or result.get("rag_failed", 0):
        _log.warning(
            "規則版への再生成が一部失敗しました: world=%s detail=%s", world, result)
        return {"status": "partial_failure", **result}
    es_ok = _reindex_after_rag_rewrite(world)
    return {"status": "ok" if es_ok else "es_reindex_failed", **result}


def wipe_world(world, *, reflect=True) -> dict:
    """資料フォルダの派生物を完全削除する（delete の前段）: グラフ（Neo4j）＋台帳＋ES。

    資料フォルダ単位の advisory lock で run と直列化する薄いラッパー（`_wipe_locked` へ委譲）。
    `worlds.delete` は外側で lock 取得済みなので lock-free の `_wipe_locked` を直接呼ぶ（lock は再入不可）。
    """
    with store.world_lock(world):
        return _wipe_locked(world, reflect=reflect)


def _wipe_locked(world, *, reflect) -> dict:
    """`wipe_world` の lock 未取得版（呼び出し元が world_lock を保持している前提）。

    ① 何かを消す前に OCR 観測ディレクトリの削除対象を検証する（原本・登録 root と重なる設定なら例外で何も消さない）。
    ② `last_sig` を `''` に無効化する（Neo4j delete より前に、ガード無しで・失敗は伝播させる）。
       書けなければ削除を開始せず、書けた後はどこで失敗しても次回 sync が必ず再構築する。
    ③ グラフを削除する（失敗は握りつぶさず例外にする）。④ 成功してから台帳をクリアする（「台帳空・グラフ残」を作らない）。
    ⑤ OCR 観測ディレクトリと OCR の job/cache/run を消す（失敗は伝播）。⑥ 派生物と ES インデックスを削除する。
    参照元の外部フォルダは消さない。
    """
    obs_dir = worlds.observation_removal_target(world)     # ①
    store.set_world_sig(world, "")                          # ② pre-invalidate
    deleted = 0
    if reflect:                                            # ③ Neo4j 失敗は伝播（呼出側は registry を進めない）
        env = world_neo4j._env()
        deleted = world_neo4j.delete_world(world, env["uri"], env["user"], env["pw"])
    ledger = store.replace_documents(world, [])            # ④ グラフ削除成功後に台帳クリア
    import shutil
    from ..store import ocr_jobs
    if obs_dir is not None:
        shutil.rmtree(obs_dir)                             # ⑤ OCR 観測本文。失敗は伝播（OCR の行を残して再試行）
    ocr_jobs.purge_world(world)                            # ⑤ OCR の job/cache/run。失敗は伝播
    shutil.rmtree(worlds.derived_dir(world), ignore_errors=True)   # ⑥ 派生 MD（Office 由来）も消す
    try:
        es_index.delete_world(world)                  # ES インデックスも削除
    except Exception:
        pass
    return {"world": world, "ledger_cleared": ledger, "graph_deleted": deleted}
