"""取り込みオーケストレーション（鏡モデル・即反映ライブ鏡・MIRROR-MODEL §4）。

world（登録ディレクトリ）を1回スキャンし、**台帳＋グラフ(Neo4j)** を現状に一致させる単一の経路:
スキャン→台帳書込(`store.replace_documents`)→グラフ構築(`world_graph.build_world`)→
**world 単位のクリーン rebuild**(`world_neo4j.load_world`)→`ingest_runs` 記録。
旧 version 別 src/md・merge(S+L)・filter_edges・auto-scope overrides は撤去（鏡は1木＋パス同一性）。
特定テーマの名前は持たない（語彙は world のフォルダ/ファイル＝データ由来）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone

from .. import corpus_docs, es_index, scope_infer, store, webhooks, worlds
from . import failure_reasons, importance, office_md, world_graph, world_graph_service, world_neo4j
from .analyzers import registry as analyzer_registry

# LOG-2（2026-09-03）: MD 変換（取り込み進行ログ）は専用ログ（sherpa.ingest.convert）へまとめる
# （`sherpa/log_setup.py` の登録表参照・office_md.py と合流させ1系統にする）。
_log = logging.getLogger("sherpa.ingest.convert")

# ING-3（取り込みの背景実行化）: 段の表示名（内部段キー→利用者向け平文）。`ingest_runs.progress`
# へ書き込み `GET /worlds/{wid}/status` が実行中 run の進捗として返す。段構成は実装の主要な
# 待ち時間の境界に合わせたもの（office_md 段＝旧形式変換＋MD化＋検索用データ整形をまとめて1段・
# es_index 段＝全文索引＋ベクトル化をまとめて1段——別々に計測できる境界が実装に無いものは分けない）。
STAGE_LABELS = {
    "accepted": "受け付けました",
    "scanning": "フォルダを確認中",
    "office_md": "旧形式を新形式へ変換し、読める写し（MD）・検索用データを作成中",
    "graph_build": "関係グラフを構築中",
    "es_index": "全文索引に登録し、ベクトル化中",
    "finalize": "仕上げ中",
    "deleting": "検索用データを削除しています",
}

# 逐次進捗の書き込み頻度（office_md 段の per-file 進捗はこの件数ごとに間引く・毎ファイル書くと
# 大規模 world で DB 書き込みが支配的になる）。最初（0件）・最後（総数一致）は間引かず必ず書く。
_PROGRESS_FILE_INTERVAL = 100


def _target_of(url: str) -> str:
    """URL/URI から「ホスト:ポート」だけを取り出す（失敗理由の表示用・userinfo/パスは落とす）。

    取り出せない・空なら "?"。例外は投げない（失敗の記録処理の中で使うため）。
    """
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
    """world の `(nodes, edges, flags)` を構築（有効グラフの単一入口へ委譲・rv-full DRY）。"""
    return world_graph_service.build_effective_world(world)


def _reflect_graph_after_rag_rewrite(world: str) -> None:
    """`.rag.md` の軽量書換え後、Neo4j のグラフ（言及エッジ）を追いつかせる（rv-s2-mention #1）。

    言及エッジ（Pass3・辞書突合）は `{rel}.rag.md` があればそれを本文として読む
    （`corpus_docs.iter_world_documents` の `md_path` 選定・`world_graph._mention_pass` 参照）。
    `_llm_render_pass`／`regenerate_rag_rule_only`／`_refresh_derived_representations`
    （OCR 反映後の catch-up 等）はいずれも rag.md を書き換えるが世代（world 署名）は変えない
    軽量経路のため、`_run_locked`（通常の full sync）の `build_world_graph`→`load_world` を
    経由しない——放置すると ES だけ追随しグラフ（言及エッジ）が陳腐化したまま固定される。

    ES 反映（RAG_ES 無効なら不要）とは独立に**常に**呼ぶ——グラフは RAG_ES の設定に関わらず
    追随させる必要がある。呼び出し元が既に `store.world_lock(world)` を保持している前提の
    lock-free ヘルパー（`_wipe_locked`/`_run_locked` と同じ流儀・世界単位ロックの再入不可を
    避ける）。失敗は例外のまま呼び出し元へ伝播させる（ここで揉み消さない・best-effort にしない
    ——各呼び出し元は既に `sync()`/`regenerate_rag_rule_only()` の例外伝播契約で受け止める）。
    """
    nodes, edges, _flags = build_world_graph(world)
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, world, env["uri"], env["user"], env["pw"])


# `office_md.build_derived()` が持つ per-file 失敗リスト（`[{"doc": rel, "reason": str}]`）のキー。
# ING-1: status 画面の「失敗ファイル一覧＋再変換ボタン」の元データへ一本化する（キー名の末尾
# `_failures` を落とした残りを stage 名として使う）。
_FAILURE_LIST_KEYS = ("unhandled_failures", "legacy_conversion_failures", "conversion_failures",
                     "document_ir_failures", "evidence_ir_failures", "rag_failures")

_FAILED_FILES_LIMIT = 200   # ingest_runs.extraction_snapshot へ保存する量の上限（超過分は total/truncated で示す）

# 「抽出不完全の疑い」一覧（失敗ではない別枠・`failure_reasons.PARTIAL_EXTRACTION_LABEL/_ADVICE` 参照）。
_PARTIAL_EXTRACTION_LIMIT = 200


def _office_md_stage_summary(drep: dict) -> dict:
    """`office_md.build_derived()` の要約3値（`_record`／PG replace 失敗パスの両方で使う共通片）。"""
    return {"converted": drep.get("converted", 0), "failed": drep.get("failed", 0),
            "unsupported": drep.get("unsupported", 0)}


def _failed_files_summary(drep: dict) -> dict:
    """`office_md.build_derived()` の各段 `*_failures` を1つの一覧へまとめる（rel＋stage＋閉じた理由コード）。

    `reason` は `failure_reasons.classify()` で閉じた語彙へ分類済み（`detail`＝分類前の生文字列・
    `other` の内訳表示用）。`by_reason`＝理由コード別件数（**打ち切り前の全件**を対象・ING-1裁定2の
    内訳集計）。`_FAILED_FILES_LIMIT` 件で `items` を打ち切り、`total`（打ち切り前の全件数）・
    `truncated`（打ち切ったか）を併記する——`ingest_runs.extraction_snapshot` への保存量を抑えつつ、
    全体件数・内訳は失わない。
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
    """`office_md.build_derived()` の `partial_extraction_suspected`（失敗ではない「要確認」一覧）を整形する。

    `_failed_files_summary` と同じ打ち切り契約（`total`/`truncated`）。失敗一覧と混ぜない別枠
    （ING-1裁定・静かな部分抽出検知）。
    """
    items = [e for e in (drep.get("partial_extraction_suspected") or []) if isinstance(e.get("doc"), str)]
    return {"items": items[:_PARTIAL_EXTRACTION_LIMIT], "total": len(items),
            "truncated": len(items) > _PARTIAL_EXTRACTION_LIMIT}


def _ledger_rows(world: str, *, sig: str | None = None) -> list:
    """走査文書 → 台帳行（doc_id＝rel_path・原本DLはパス基準で解決するので original_path は持たない）。

    `state="unreadable"`（内容判定に必要なヘッダが読み取れない文書）は台帳の `status` にも
    そのまま反映する——「使える」文書と黙って同列にしない。

    `importance`/`importance_reason`/`importance_source`（RV1是正#2・2026-09-01）: `GET /documents`
    の台帳高速経路（`doc_ledger.public_documents_page`）が実走査せずに重要度を返せるよう、
    ここ（ingest 時・呼び出し元 `_run_locked` は `world_lock` 保持中で rebind と競合しない）で
    `importance.resolve_for_world`（world 全体の走査を伴う）を1回だけ実行し materialize する
    （§2 truth table＝無ければ3キーとも付けない・`doc_ledger._importance_fields` と同じ形）。

    root/files/sig の一本化（RV2是正#b1・2026-09-01）: 以前は `importance.resolve_for_world(world)`
    と `corpus_docs.world_documents(world)` がそれぞれ独立に `worlds.world_dir()` を解決し、
    `_重要度.txt` 探索・文書列挙のために木を別々に歩いていた（`resolve_for_world` 自体も
    `sig` 省略時は自前の署名計算でもう1回歩く）——`_run_locked` の排他 `world_lock` 保持中に
    実測 cold 時4walk・cache hit時も3walk。ここで `root` と `files`（`scope_infer.safe_files(root)`
    の materialize 済み list）を1回だけ確定し、両方へ同じ `root`/`files`/`sig` を渡す
    （`sig` 省略可＝呼び出し元 `_run_locked` が `world_state()` で既に確定済みの署名を渡せば、
    `resolve_for_world` 内の自前署名計算も省ける）。
    """
    root = worlds.world_dir(world)
    if not root:
        return []
    files = list(scope_infer.safe_files(root))
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
    """1 world 分の取り込み（台帳＋グラフ反映）を実行し `ingest_runs` に記録、要約を返す。

    **world 単位 advisory lock で直列化**（同じ world の同時 rebuild でグラフ/台帳が混ざらない・RV High#1）。
    `reflect=False`＝Neo4j 反映を省く（DB 無し検証/テスト用＝台帳書込とグラフ構築だけ）。
    `run_id`（ING-3）＝呼び出し元が受付時に O(1) で確保済みの `ingest_runs` 行を渡す時だけ
    指定する（router は即受付契約のため、`run_id` は必ずここで判明済みの状態でこの関数に入る）。
    省略時（`None`）は従来どおりここで自前に行を確保する（直接呼び出し・テスト用）。
    `on_run_id`＝`run_id` を渡さない代わりに、確保された run_id が判明した瞬間に呼ばれる
    コールバック（旧経路・後方互換）。
    `op`（PART-6・Webhook 通知の情報用途のみ）＝sync/refresh/rebind/rerun のいずれか
    （`rerun()` が "rerun" を渡す・他は既定 "sync"）。
    """
    with store.world_lock(world):
        return _run_locked(world, reflect=reflect, created_by=created_by,
                           scan_root=scan_root, run_id=run_id, on_run_id=on_run_id, op=op)


def _run_locked(world, *, reflect, created_by, scan_root, run_id=None, on_run_id=None,
                op: str = "sync",
                finalize: bool = True) -> dict:
    # lock-free 版（呼び出し元が既に world_lock を保持している前提）。`worlds.rebind` はここを直接呼ぶ
    # （`run` 経由だと session-level advisory lock は別コネクション再入不可＝自己デッドロックする・R3-S3）。
    # **この run（この `_run_locked` 呼び出し）**の last_sig 無効化・確定はここで完結する（secRV 再RV
    # round-2・2026-07-14／round-3 でコメント精度是正: 「last_sig への書き込みはすべてここ」は誤りで、
    # `_wipe_locked` の pre-invalidate・`sync` の lock 内バックフィル・rebind 復旧の
    # `restore_bind_invalidate_sig` も同じ world_lock 保持中に last_sig を書く別の書き込み者である）:
    #  ① world 未解決（`sig is None`）は last_sig を含む mutation に一切触れず即 failed で終了する。ただし
    #     `_record`（`ingest_runs` への失敗記録＝監査ログ）は書く（HIGH-2 TOCTOU 対策・後続の
    #     `_build_derived`/`build_world_graph` が world を再解決して番兵無しで進む窓を作らない）。
    #  ② world 解決済みなら取り込み開始**前**に無効化（`''`）をガード無しで書く（pre-invalidate・fail-closed・
    #     finding #4）＝PG に書けなければここで例外が伝播し反映を一切開始しない。書けた後はどこで失敗/
    #     クラッシュしても last_sig は既に `''`（実 sig と一致しない番兵）＝次回 sync は必ず再構築する。
    #  ③ 正しい署名への確定は**成功パスのみ**・かつ `_record`（`ingest_runs` 記録）が**成功した後**に行う
    #     （MED-1）＝記録が失敗すれば署名は無効のまま残り、次回 sync が再試行して記録漏れを拾い直す。
    #  ④ `reflect=False`（staging）は確定しない（MED-2）＝グラフ未反映を「同期済み」として誤認させない。
    #  呼び出し元（`worlds.register`/`worlds.rebind`/`worker.sync`）はここより後・lock 外で `set_world_sig`
    #  を後置き確定しない（round-3 で除去済み・register/rebind 双方）: 他プロセスの pre-invalidate/削除を
    #  有効署名で復活させる番兵復活の穴になるため。
    #
    # ING-3（取り込みの背景実行化・中断リカバリー）: `run_id` は呼び出し元が確保済みの
    # `ingest_runs` 行（`store.start_ingest_run` 済み）を渡す時だけ指定する（この関数へ入る**前**の
    # 処理から同じ行へ進捗を積みたい場合）。
    # 省略時（register/refresh/sync の通常経路）はここで自前に行を確保する——完了時の1回 INSERT
    # だった旧契約を「開始時 INSERT→完了時 UPDATE」へ変える本体（プロセス強制死で `finish_ingest_run`
    # まで届かなかった run は `status='extracting'` のまま残り、起動時 lifespan が孤児として拾う）。
    # `world_state()`（ディレクトリ走査・大規模 root では時間がかかりうる）より**前**に行を確保する
    # ことで、呼び出し元は「受付＝run_id 確定」をスキャン開始前の時点で得られる（即受付契約）。
    #
    # ING-3: HTTP 経由（router の各エンドポイント）は必ず `run_id` を確保済みでここへ入る
    # （`sherpa.ingest.background.start_or_join` の `create_run` が受付処理の中で O(1) INSERT する）。
    # `run_id is None` はここでは直接呼び出し（テスト・CLI 等）専用の後方互換パスであり、以前ここに
    # あった「孤児 `extracting` 行の格下げ」は撤去した——孤児格下げは起動時 lifespan の一括処理**のみ**
    # に一本化する（「稼働中に他プロセスが同じ world へ触れることは無い」という前提自体は
    # 元々起動直後の一瞬しか意味を持たない narrow window 向けの band-aid であり、実行の都度この
    # world へ問い合わせる設計はかえって「いつ・どこで孤児が回収されるか」を追いにくくしていた）。
    #
    # `finalize`（既定 True）: False の時、この呼び出しは `ingest_runs` の終端 UPDATE を一切書かず、
    # 代わりに戻り値へ `_pending_finalize`（`finish_ingest_run`/`finish_ingest_run_and_confirm_world`
    # へそのまま渡せる終端引数一式）を積んで返す（例外を投げる経路では `e._sherpa_ingest_run_pending`
    # へ同じ内容を積む）。`worlds.rebind` が新root試行→（失敗時）旧root復旧の**複数回**の
    # `_run_locked` 呼び出しを、受付 run にとっての「非terminalな内部段」として扱うための出口
    # ——各段が個別に terminal 化すると、後段の呼び出しが前段の published_snapshot/source_doc_ids を
    # 上書き消去しうる（世代跨ぎの Graph 件数 0 表示の原因になる）。呼び出し元は複数段の顛末を見た
    # 上で、最終的にどちらか1回だけ `finish_ingest_run*` を呼ぶ。
    if run_id is None:
        run_row = store.start_ingest_run(world, scan_root=scan_root, created_by=created_by)
        run_id = run_row["id"]
    if on_run_id is not None:
        on_run_id(run_id)

    def _progress(stage, done=None, total=None):
        try:
            store.update_ingest_run_progress(run_id, {
                "stage": stage, "stage_label": STAGE_LABELS.get(stage, stage),
                "done": done, "total": total,
                "updated_at": datetime.now(timezone.utc).isoformat()})
        except Exception:
            _log.warning(
                "進捗の記録に失敗しました（取り込み自体は継続）: world=%s stage=%s", world, stage, exc_info=True)

    _progress("scanning")
    # 走査済み件数を逐次報告（総数は走査完了まで不明＝done のみ・UI 側は「N件確認済み」表示）。
    sig, manifest = world_state(world, progress=lambda n: _progress("scanning", done=n, total=None))

    nodes, edges, flags, rows = [], [], [], []              # 台帳行は派生MD（既に作成済）から確定（Office を含める）

    def _record(status, reflected=None, ledger=0, extra_flags=(), drep=None,
               es_summary=None, neo4j_summary=None,
               confirm_sig=None, confirm_manifest=None, confirm_doc_count=None,
               confirm_scan_report=None):
        fl = list(flags) + list(extra_flags)
        snap = {"docs": len(rows), "nodes": len(nodes), "edges": len(edges), "flags": fl}
        if drep is not None:
            # office_md 段の要約（ING-1・status 画面の詳細折りたたみ）。`drep` は derive の段階まで
            # 進んでいれば呼び出し元が渡す（graph/ES/Neo4j の成否に関わらず、その時点までの
            # office_md 内訳は分かっているため・失敗した run でも失敗ファイル一覧は失わない）。
            snap["office_md"] = _office_md_stage_summary(drep)
            snap["failed_files"] = _failed_files_summary(drep)
            snap["partial_extraction_suspected"] = _partial_extraction_summary(drep)
        if es_summary is not None:
            snap["es"] = es_summary
        if neo4j_summary is not None:
            snap["neo4j"] = neo4j_summary
        pending = {"status": status, "extraction_snapshot": snap, "published_snapshot": reflected,
                  "source_doc_ids": [r["name"] for r in rows], "confirm_sig": confirm_sig,
                  "confirm_manifest": confirm_manifest, "confirm_doc_count": confirm_doc_count,
                  "confirm_scan_report": confirm_scan_report}
        if not finalize:
            # `finalize=False`（rebind の新root試行/旧root復旧の内部段・呼び出し元が最終結末を
            # 見てから一度だけ terminal 化する契約）: この行の DB 確定は呼び出し元に委ねる。
            return {"world": world, "status": status, "ledger": ledger,
                    "nodes": len(nodes), "edges": len(edges), "flags": fl, "run": None,
                    "_pending_finalize": pending}
        # ING-3: 開始時に確保済みの行（`run_id`）を完了状態へ UPDATE する（INSERT ではない・
        # `finish_ingest_run`/`finish_ingest_run_and_confirm_world` が `progress` を NULL へ戻す）。
        # `confirm_sig` が渡された（＝この呼び出しが成功確定パス）時だけ、run 完了と
        # world 側の署名/manifest/doc_count/scan_report 確定を**同一トランザクション**で行う
        # （呼び出し元が scan_report をこの呼び出しより前に計算済みであること＝重い処理を
        # トランザクション内に残さない契約）。
        if confirm_sig is not None:
            rec = store.finish_ingest_run_and_confirm_world(
                run_id, world, status=status, extraction_snapshot=snap,
                published_snapshot=reflected, source_doc_ids=pending["source_doc_ids"],
                sig=confirm_sig, manifest=confirm_manifest, doc_count=confirm_doc_count,
                scan_report=confirm_scan_report)
        else:
            rec = store.finish_ingest_run(run_id, status=status, extraction_snapshot=snap,
                                          published_snapshot=reflected,
                                          source_doc_ids=pending["source_doc_ids"])
        # PART-6: terminal 化のこの1点（`finalize=True` のときだけ実際に到達する）から Webhook
        # 通知を best-effort で発火する（例外は握って取り込み成否へ昇格させない・
        # `webhooks.notify_run_terminal` 自身が内部で全て捕捉するため try は防御的な二重）。
        try:
            webhooks.notify_run_terminal(world, run_id, op, status, doc_count=len(rows))
        except Exception:
            _log.warning("Webhook 通知の起動に失敗しました（取り込み自体は継続）: world=%s", world,
                        exc_info=True)
        return {"world": world, "status": status, "ledger": ledger,
                "nodes": len(nodes), "edges": len(edges), "flags": fl, "run": rec}

    def _office_flags(d):                                   # 派生MD のセットアップ失敗を正直に flag（書けないなら検索不可）
        """per-file の詳細（rel/reason）はここへ複製しない——`failed_files`
        （`_failed_files_summary`・200件上限で集約済み）を単一の出所にする。以前は
        `unhandled_failures` の全 rel を `office_md_blocked:{doc}\t{reason}` として無制限に
        `flags`（`extraction_snapshot`/`last_run_blocked` へそのまま伝播・上限なし）へ複製しており、
        失敗件数の多い world で JSONB が際限なく膨らんでいた。集約 warn だけを返す。
        """
        if not d.get("error"):
            return []
        return [{"doc": None, "action": "warn", "reason": f"office_md:{d['error']}"}]

    if sig is None:                                         # world 未解決＝mutation 前に即 failed（HIGH-2）
        flags = [{"doc": None, "action": "blocked", "reason": "world_unresolved"}]
        return _record("failed")

    store.set_world_sig(world, "")                          # pre-invalidate（ガード無し・fail-closed・finding #4）

    # 順序: ①Office→決定的MD を先に作る（corpus_docs / ES が Office 項目定義表も参照できる・RV High）→
    # ②グラフ構築。
    _progress("office_md", done=0, total=None)
    _last_office_progress_done = [None]

    def _office_progress(done, total):
        # Nファイルごとに間引く（先頭0件・末尾＝総数一致は必ず書く）。大規模 world で毎ファイル
        # DB 書き込みしないための頻度制御（`_PROGRESS_FILE_INTERVAL`）。
        last = _last_office_progress_done[0]
        if done == 0 or done == total or last is None or done - last >= _PROGRESS_FILE_INTERVAL:
            _last_office_progress_done[0] = done
            _progress("office_md", done=done, total=total)

    drep = _build_derived(world, world_sig=sig, progress=_office_progress)
    if drep.get("error"):
        # 派生生成が公開Gateで拒否された（不完全な世代）＝派生ディレクトリは旧内容のまま更新されて
        # いない。ここで打ち切らずgraph/台帳/ESへ進むと、旧派生content(A)を基に反映した上で
        # 新しい原本sig(B)を確定してしまい、次回syncが「sigはB・派生は(まだ)A」の不一致に
        # 気付けなくなる（pre-invalidateはmutation開始前に書き済みなので、ここは確定側の分岐を
        # 増やすだけで足りる）。sigを確定させず、冒頭のpre-invalidate（''）のまま終了する。
        return _record("failed", extra_flags=_office_flags(drep), drep=drep)
    # ING-3: `build_world_graph`（アナライザ解析込み・大きい world では無視できない時間が
    # かかりうる）に入る**前**に段を「関係グラフを構築中」へ進める——旧実装はこの呼び出しの間
    # 進捗が「office_md」のまま止まって見えた（Neo4j へのロード開始まで graph_build 表示にならない）。
    _progress("graph_build")
    nodes, edges, flags = build_world_graph(world)

    if any(f.get("action") == "blocked" for f in flags):    # blocked＝world 未解決 or 不可読コード
        # （unreadable_code_file）＝反映も台帳書込もしない（fail-closed・部分グラフを確定しない）
        return _record("failed", extra_flags=_office_flags(drep), drep=drep)

    if not reflect:                                         # staging のみ（DB無し検証/テスト）＝台帳だけ
        rows = _ledger_rows(world, sig=sig)
        written = store.replace_documents(world, rows)
        # 署名は確定しない（MED-2）＝ last_sig は pre-invalidate のまま `''`。staging は Neo4j 未反映なので
        # 「同期済み」とみなさない＝以後の `sync(reflect=True)` が必ず本反映で再構築する。
        return _record("extracting", ledger=written, extra_flags=_office_flags(drep), drep=drep)

    # reflect=True: グラフを atomic 置換（load_world が world_id 単位の delete+load を1 tx）。失敗時は tx ロールバックで
    # 旧グラフが残り、台帳も書き換えない＝旧状態を一貫保持（派生MD は cache のため先行更新済・last_sig は冒頭の
    # pre-invalidate で既に `''`＝ソース内容が不変（設定drift のみ）でも次回 sync が確実に再構築する・RV High#2/R3再RV#4）。
    env = None
    neo4j_t0 = time.monotonic()
    try:
        env = world_neo4j._env()
        n, m = world_neo4j.load_world(nodes, edges, world, env["uri"], env["user"], env["pw"])
    except Exception as e:
        # 失敗理由に**接続先（ホスト:ポート）**を含める（閉域実機・2026-08-18）: NEO4J_URI の例示ホスト名を
        # そのまま有効化して名前解決できず全滅、という事故で画面に例外クラス名しか出ず原因が追えなかった。
        # 認証情報（user/pw・URL の userinfo）は含めない。
        # 失敗した段も stage summary に残す（office_md は完了済みなので `drep` 込み・
        # neo4j は未完了だが所要時間とエラーだけは分かる＝完全な沈黙にしない）。
        return _record("failed", extra_flags=[{"doc": None, "action": "blocked",
                       "reason": f"graph_reflect_failed:{e.__class__.__name__}@{_target_of(env['uri'] if env else '')}"}],
                       drep=drep,
                       neo4j_summary={"error": f"{e.__class__.__name__}@{_target_of(env['uri'] if env else '')}",
                                     "duration_sec": round(time.monotonic() - neo4j_t0, 3)})
    neo4j_duration_sec = time.monotonic() - neo4j_t0
    rows = _ledger_rows(world, sig=sig)                     # 派生 .md ができてから台帳（Office を含める）
    try:
        written = store.replace_documents(world, rows)      # 台帳はグラフ成功後（不一致を残さない）
    except Exception as e:
        # R3-S1: PG 台帳 replace 失敗＝Neo4j は新・台帳は旧のまま残る窓。記録は best-effort
        # （PG 全断なら記録自体も書けない＝元例外の伝播のみで可視。制約違反等のデータ起因失敗なら記録が残る）。
        # last_sig は冒頭の pre-invalidate で既に `''`（fail-closed）＝ここで改めて無効化する必要はない
        # （次回 sync は必ず全再構築で自己修復する・Codex finding #4）。
        # office_md／neo4j は既に完了しているので、その要約は捨てずに残す（pg_replace だけが
        # 失敗した run でも三段の状況が見える・失敗一覧も失わない）。`published_snapshot`
        # も記録する——Neo4j へは既に反映済み（台帳 replace の失敗は Neo4j 側の巻き戻しを
        # 伴わない）ため、run 自体は `failed` のままでも「今実際に Neo4j にある内容」は
        # 新世代（n/m）。省略すると `get_latest_published_run_summary` が旧 run の件数を
        # 返し続け、status の graph_nodes/graph_edges が実態より古いまま止まる。
        pending = {"status": "failed", "source_doc_ids": [r["name"] for r in rows],
                  "extraction_snapshot": {"docs": len(rows), "nodes": len(nodes), "edges": len(edges),
                                          "flags": list(flags), "degraded": True,
                                          "stage": "pg_replace", "error": e.__class__.__name__,
                                          "office_md": _office_md_stage_summary(drep),
                                          "failed_files": _failed_files_summary(drep),
                                          "partial_extraction_suspected":
                                              _partial_extraction_summary(drep),
                                          "neo4j": {"nodes": n, "edges": m,
                                                   "duration_sec": round(neo4j_duration_sec, 3)}},
                  "published_snapshot": {"nodes": n, "edges": m},
                  "confirm_sig": None, "confirm_manifest": None, "confirm_doc_count": None,
                  "confirm_scan_report": None}
        if finalize:
            # ING-3: 開始時に確保済みの行を UPDATE する（INSERT ではない・二重記録を避ける）。
            try:
                store.finish_ingest_run(
                    run_id, status=pending["status"], source_doc_ids=pending["source_doc_ids"],
                    extraction_snapshot=pending["extraction_snapshot"],
                    published_snapshot=pending["published_snapshot"])
                e._sherpa_ingest_run_recorded = True   # 呼び出し元（_run_worker_or_503 等）の二重記録を防ぐ
                # PART-6: `_record` を経由しないこの terminal 化（pg_replace 失敗）でも通知する。
                try:
                    webhooks.notify_run_terminal(world, run_id, op, pending["status"])
                except Exception:
                    _log.warning("Webhook 通知の起動に失敗しました: world=%s", world, exc_info=True)
            except Exception as record_exc:
                _log.warning(
                    "pg_replace 失敗の記録に失敗（best-effort・元の例外はそのまま re-raise）: %s", record_exc)
        else:
            # `finalize=False`（rebind の内部段）: ここでは書かず、呼び出し元が最終結末を
            # 見てから一度だけ terminal 化できるよう、保留分を例外に添えて伝える。
            e._sherpa_ingest_run_pending = pending
        raise
    extra = _office_flags(drep)
    _progress("es_index", done=0, total=None)
    _last_es_progress_done = [None]

    def _es_progress(done, total):
        # `_office_progress` と同じ間引き（`_PROGRESS_FILE_INTERVAL` 件ごと・先頭/末尾は必ず書く）。
        # es_index 側は既に doc グループ（flush）単位で呼ばれるため、ここは二重の安全弁。
        last = _last_es_progress_done[0]
        # RV是正（rv-periphery #3(c)・2026-09-05）: 直前と全く同じ done は無条件に書かない
        # （`done==0`/`done==total` は同値でも毎回書く特例だったため、例えば空 world の Pass2 が
        # `progress(0, 0)` を2回通知するケースで同一内容を重複書込みしていた）。
        if last == done:
            return
        if done == 0 or done == total or last is None or done - last >= _PROGRESS_FILE_INTERVAL:
            _last_es_progress_done[0] = done
            _progress("es_index", done=done, total=total)

    # ES/reconcile は best-effort だが握りつぶさず flag 化＝半壊状態の可視化（監査#5）。取り込み自体は成功扱いのまま。
    try:
        esr = es_index.index_world(world, content_sig=world_signature(world),
                                   progress=_es_progress)   # ES 全文索引（best-effort・署名で鮮度管理・EMBED-3′: doc単位進捗）
        if esr.get("error"):                                # delete/create/bulk 失敗は例外でなく error dict（available=False の未接続は warn しない）
            extra.append({"doc": None, "action": "warn", "reason": f"es_index_failed:{esr['error']}"})
        elif esr.get("available") is True:
            # 全再構築は毎回 human_md も作り直すため render 側は既に追随済みのはず（bulk 成功時だけ
            # 確定・RAG_ES の設定に関わらず評価）。次回 sync が human_md 次元だけで無駄な
            # reindex を繰り返さないよう、ここで `.human_md_es_sig` を確定する。
            wd = worlds.world_dir(world)
            dmd = worlds.derived_md_dir(world)
            if wd and dmd.exists():
                if office_md.confirm_human_md_es_sig(wd, dmd):
                    # マーカー確定に続けて ES 自身の `_meta.human_md_sig` も現行署名へ書き直す
                    # （書き直さないと meta だけ None のまま残り、次回 sync が human_md 次元で
                    # 無駄な reindex を繰り返す・`es_index.confirm_human_md_meta` docstring 参照）。
                    if not es_index.confirm_human_md_meta(world):
                        extra.append({"doc": None, "action": "warn",
                                      "reason": "human_md_es_meta_confirm_failed"})
                else:
                    # confirm を捨てずに flags へ反映する——render 側の drift 残り/マーカー書込
                    # 失敗を「成功（auto_published）」で覆い隠さない。
                    extra.append({"doc": None, "action": "warn",
                                  "reason": "human_md_es_sig_marker_confirm_failed"})
    except Exception as e:
        esr = {"available": None, "error": f"{e.__class__.__name__}@{_target_of(es_index._url())}"}
        extra.append({"doc": None, "action": "warn",
                      "reason": f"es_index_failed:{e.__class__.__name__}@{_target_of(es_index._url())}"})
    _progress("finalize")
    try:
        from .. import reconcile                            # 取込のついでに孤児派生物を自動掃除（不可視・registry 確実時のみ）
        reconcile.reconcile_derivatives(reflect=reflect)
    except Exception as e:
        extra.append({"doc": None, "action": "warn", "reason": f"reconcile_failed:{e.__class__.__name__}"})
    status = "auto_published_with_flags" if (flags or extra) else "auto_published"
    # `esr`（直前の `es_index.index_world()` の戻り値）が既に `chunks`（bulk 対象件数）を持つ——
    # 別途 `es_index.count()` を叩き直さない（この run が実際に送った件数と ES 側の現況が bulk 失敗時に
    # 食い違いうる・無用な ES 往復を増やさない）。
    es_summary = {"available": esr.get("available") if isinstance(esr, dict) else None,
                 "error": esr.get("error") if isinstance(esr, dict) else None,
                 "chunks": esr.get("chunks") if isinstance(esr, dict) else None}
    neo4j_summary = {"nodes": n, "edges": m, "duration_sec": round(neo4j_duration_sec, 3)}
    # 既知の残余（secRV 再RV round-4・2026-07-14・ライブ鏡の本質的 TOCTOU）: この確定は**冒頭スキャン時点**の
    # 署名であり、取り込み各段（派生MD/グラフ/台帳/ES）が実際に読んだ内容と原子的に一致する保証は無い。
    # 取り込み中に外部がファイルを追加→確定前に削除して元へ戻す（ABA）と、反映済み内容(B)と署名(A)＝ディスク(A)
    # が一致せず次回 sync が unchanged と誤判定し得る。恒久変化なら次回 sync の署名不一致で自己修復するが、
    # 一時的な ABA はどの時点の再スキャンでも観測不能＝スキャン方式では閉じられない。完全閉鎖は取り込みが読む
    # 不変スナップショット or 単調な世代ID の導入（別提案・finding #8 partial と同じ設計依存）で行う。
    #
    # ING-3: scan_report は「run 完了＋world 確定」の**同一トランザクション**（`_record` の
    # `confirm_*` 引数 → `finish_ingest_run_and_confirm_world`）へ含めるため、`_record` を呼ぶ
    # **前**にここで計算しておく（重い処理をトランザクション内に残さない・run が「成功」を確定した
    # 直後に world 側の確定が来ない中間状態を作らない）。計算自体の失敗は best-effort
    # （`scan_rep=None` のまま渡す＝該当列は更新されず前回値が残る・次回 sync か明示の
    # `POST /worlds/{id}/recount` が拾う。sig 確定自体は失敗させない）。
    confirm_doc_count = None
    scan_rep = None
    if sig is not None:
        try:
            scan_rep = corpus_docs.scan_report(world)
        except Exception:
            scan_rep = None
            _log.warning(
                "取り込み集計（scan_report）の計算に失敗しました（次回 status は前回値のまま）: "
                "world=%s", world, exc_info=True)
        # doc_count は外部公開 discovery（/ext/v1/capabilities）の事前集計値・ここ（成功確定）でだけ
        # 更新する＝ホットパスでのファイルツリー走査を無くすための唯一の書き込み点。`len(rows)`
        # （変換に成功して検索可能になった件数）ではなく、冒頭の `world_state()` と**同一スキャン**の
        # `manifest` から doctype 対応原本件数を数える（変換失敗/未対応の Office・PDF・画像も
        # 「原本」としては存在し `/doc` の対象なので、検索可能台帳数に矮小化しない。sig と同じ
        # manifest から数えるため世代もずれない）。
        confirm_doc_count = corpus_docs.manifest_doctype_count(manifest, world)
    return _record(status, reflected={"nodes": n, "edges": m}, ledger=written, extra_flags=extra,
                  drep=drep, es_summary=es_summary, neo4j_summary=neo4j_summary,
                  confirm_sig=sig, confirm_manifest=manifest,
                  confirm_doc_count=confirm_doc_count, confirm_scan_report=scan_rep)


def _build_derived(world, *, world_sig: str | None = None, progress=None) -> dict:
    """world の Office を決定的MD化して派生領域に materialize（grep 検索対象になる）。world 未解決は no-op。

    `progress`（ING-3・`Callable[[int, int], None] | None`）は `office_md.build_derived` が既に
    持つ per-file 進捗コールバックへそのまま転送する（done, total）。
    """
    wd = worlds.world_dir(world)
    if not wd:
        return {"converted": 0, "failed": 0, "unsupported": 0, "by_ext": {}}
    rep = office_md.build_derived(
        wd, worlds.derived_md_dir(world), world_sig=world_sig, progress=progress, world=world)
    if not rep.get("error"):
        _enqueue_ocr_refresh(world, world_sig)
    return rep


def _enqueue_ocr_refresh(world, world_sig: str | None) -> None:
    """公開できた派生物に対して、OCR の作り直しを1行だけ積む（既定OFF・best-effort）。

    積むのは「この署名の派生物を OCR し直す」という指示だけで、どのラスタを読むかの展開は
    隔離 worker が公開済みのルート（`.ocr_route.json`）を辿って行う。OCR は任意観測なので、
    ここでの失敗を取り込みの失敗へ昇格させない（次回の取り込みか明示実行で拾い直す）。
    """
    if not office_md.ocr_enabled() or not world_sig:
        return
    try:
        from ..store import ocr_jobs
        from . import derived_generation, ocr_worker

        # 世代IDは投入側と照合側で必ず同じ写像を使う（`generation_id_for` のコメント参照）。
        ocr_jobs.enqueue_refresh_run(
            world, derived_generation.generation_id_for(world_sig), ocr_worker.profile_hash())
    except Exception:
        _log.warning(
            "OCR再実行のenqueueに失敗しました（取り込み自体は成功）: world=%s", world, exc_info=True)


def _derived_stale(world) -> bool:
    """**派生MD の作り直しが要るか**（変更検知の無変化判定に併用・RV High#1）。

    派生ディレクトリが無い（＝この機能の後付け導入／`data/derived` 削除／`SHERPA_DERIVED_DIR` 変更）のに
    変換可能 Office が在るなら True。一度ビルドすれば dir は残る（失敗ファイルがあっても）ので無限ループしない。
    **アーム構成（有効アーム＋PDF バックエンド）が変わった時も True**（署名同一でも作り直す・RV High）。
    記録したアーム構成と今が一致＝drift 無しなら再ビルドしない（無限ループしない）。
    """
    dmd = worlds.derived_md_dir(world)
    if dmd.exists():
        return office_md.arms_sig_drift(dmd)             # アーム構成/PDF バックエンドの変化を検知して作り直す
    wd = worlds.world_dir(world)
    if not wd:
        return False
    return any(rp.suffix.lower() in office_md.convertible_exts() for rp, _ in scope_infer.safe_files(wd))


def rerun(world, **kw) -> dict:
    """失敗/再取り込みのやり直し＝**world 全体のクリーン rebuild**（即反映ライブ鏡）。"""
    kw.setdefault("op", "rerun")   # PART-6: Webhook 通知の op（呼び出し側が明示すればそちらを優先）
    return run(world, **kw)


_SCAN_PROGRESS_INTERVAL = 500   # 走査進捗の報告間隔（ファイル数。SMB 越しの1万ファイル級で「無音の走査段」を無くす）


def _scan_dir(wd, progress=None) -> list:
    """wd 配下の対象ファイルを `(rel, mtime_ns, ctime_ns, size)` のソート済みリストで返す（stat のみ・中身は読まない＝軽い）。

    `progress`（省略可・`Callable[[int], None]`）: 走査済みファイル数を `_SCAN_PROGRESS_INTERVAL`
    件ごと＋最後に1回報告する（総数は走査が終わるまで不明＝件数のみ。実環境フィードバック
    2026-09-04「走査段が無音で止まって見える」への対処）。"""
    parts = []
    for rp, rel in scope_infer.safe_files(wd):
        try:
            st = rp.stat()
            parts.append((rel, st.st_mtime_ns, st.st_ctime_ns, st.st_size))   # ctime も（粗い mtime 対策・RV Med#4）
        except OSError:
            parts.append((rel, None, None, None))
        if progress is not None and len(parts) % _SCAN_PROGRESS_INTERVAL == 0:
            progress(len(parts))
    if progress is not None:
        progress(len(parts))
    parts.sort()
    return parts


def _sig(parts) -> str:
    # `importance.IMPORTANCE_SCHEMA_VERSION`／`analyzer_registry.config_signature()`／
    # `world_graph.MENTION_SCHEMA_VERSION`＋実効値（`_mention_min_len()`/`_mention_max_per_doc()`）
    # を材料に含める——重要度機能のスキーマ、コード解析アナライザの有効構成（登録順・拡張子集合・
    # 分類契約版）、辞書突合（言及エッジ）の仕様版、または env（`SHERPA_MENTION_MIN_LEN`/
    # `SHERPA_MENTION_MAX_PER_DOC`）の実効値が変わった world は、ソースファイル自体が不変でも
    # 署名が変わり、標準の「署名不一致→全再構築」経路で自動的に full rebuild される（旧世代の
    # 台帳/Neo4j データの後始末に専用の移行経路を持たない・rv-s2-mention #2＝実効値未対応だと
    # 設定変更後も既存 world の言及エッジが旧しきい値のまま素通りしていた）。
    return hashlib.sha1(repr((importance.IMPORTANCE_SCHEMA_VERSION, analyzer_registry.config_signature(),
                             world_graph.MENTION_SCHEMA_VERSION, world_graph._mention_min_len(),
                             world_graph._mention_max_per_doc(), parts)).encode("utf-8")).hexdigest()


def _manifest(parts) -> dict:
    """`parts` → `rel -> [mtime_ns, ctime_ns, size]` の dict（差分チェックの基準・JSONB 保存用）。"""
    return {rel: [m, c, s] for (rel, m, c, s) in parts}


def world_signature_of_root(wd) -> str:
    """既に解決済みの root（`Path`）から署名を計算する（`worlds.world_dir()` を再度呼ばない）。

    呼び出し元が root を取得済みの場合はこちらを使う——`world_signature(world_id)` のように
    world_id から毎回 `world_dir()` を呼び直すと、その2回の呼び出しの間に rebind（root 差し替え）
    が起きた場合、古い root を実際にスキャンしていながら新しい root の署名を返してしまい、
    呼び出し元が「古い root の結果」を「新しい root の署名」でキャッシュする不整合が起きうる
    （`ingest.importance.resolve_for_world` 参照）。
    """
    return _sig(_scan_dir(wd))


def world_signature(world) -> str | None:
    """world の安価な署名（各ファイルの rel/mtime/ctime/size の集約＋重要度スキーマ版＋アナライザ
    構成署名の SHA1・`_sig()` 参照）。変更検知の基準。world 不在は None。"""
    wd = worlds.world_dir(world)
    return world_signature_of_root(wd) if wd else None


def world_state(world, progress=None):
    """`(署名, ファイル明細)` を**1スキャン**で返す（取り込み時に両方を保存する用）。world 不在は `(None, None)`。
    `progress` は `_scan_dir` へそのまま転送（走査済み件数の報告・省略可）。"""
    wd = worlds.world_dir(world)
    if not wd:
        return None, None
    parts = _scan_dir(wd, progress=progress)
    return _sig(parts), _manifest(parts)


def diff_dir(wd, prev_manifest, prev_sig=None) -> dict:
    """フォルダ現状 vs 取り込み済み明細の**差分**（read-only・グラフ/台帳/ES に一切書かない）。

    返値: `added`/`removed`/`changed`（rel のリスト）＋ `total`（現在のファイル数）/`indexed`（前回取込のファイル数）。
    `prev_manifest`=None/空＝未取り込み扱い＝全ファイルが added（＝登録したら入る件数のプレビュー）。
    ただし**明細が未保存でも署名（`prev_sig`）が現状と一致**するなら取り込み済みと同一内容＝差分なし
    （旧データ/バックフィル前に「全件 added」と誤表示しない・RV High）。
    """
    parts = _scan_dir(wd)
    cur = _manifest(parts)
    prev = prev_manifest or {}
    if not prev and prev_sig is not None and _sig(parts) == prev_sig:
        return {"added": [], "removed": [], "changed": [], "total": len(cur), "indexed": len(cur)}
    added = sorted(r for r in cur if r not in prev)
    removed = sorted(r for r in prev if r not in cur)
    changed = sorted(r for r in cur if r in prev and list(cur[r]) != list(prev[r]))
    return {"added": added, "removed": removed, "changed": changed,
            "total": len(cur), "indexed": len(prev)}


def index_world_with_human_md_holdback(world: str, *, content_sig=None, settings: dict | None = None,
                                       run_id: int | None = None,
                                       progress: Callable[[int, int], None] | None = None) -> dict:
    """`es_index.index_world()` を「human_md の ES 反映ホールドバック」込みで呼ぶ共通ヘルパ。

    `run_id`（受付 run の内部・`sync` の unchanged 分岐専用）指定時、失敗しても新規
    `ingest_runs` 行は作らない——呼び出し元（`sync`）が戻り値（`available`/`error`）を見て
    その受付 run 自身の terminal 化に畳み込む（別 run が生まれると、受付側の run_id を
    ポーリングしているクライアントからは失敗が一切見えなくなる）。

    `progress`（省略可・rv-oom-resume item6・2026-09-05）: そのまま `es_index.index_world()` へ
    転送する（`(done_docs, total_docs)` を文書グループ flush ごとに受け取る）。呼び出し元が
    `ingest_runs` への進捗記録を配線したい場合に使う（`_sync_impl` の unchanged 分岐の ES
    自己修復は実環境で数時間かかりうる最長段になりえたが、従来ここは進捗が一切配線されて
    いなかった）。

    `_refresh_derived_representations`（RAG_ES 有効時の holdback 分岐）・`sync`（legacy 自己修復
    分岐）・`ocr_worker.reindex_observations` の3経路が個別に持っていた確定/失敗記録のロジックを
    一元化する（`_run_locked`＝全再構築経路は、ES 失敗を他の失敗と合わせて1回の `ingest_runs`
    レコードへ畳み込む既存の flag ベース契約のままここには含めない）。

    **呼び出し前に必ず `.human_md_es_sig` マーカーを無効化する**（RAG-KV 提案書の `.rag_sig` と
    同じ「再索引前に既存マーカーを落とす」順序）: 無効化せずに `index_world()` を直接呼ぶと、
    以前の成功で既に確定済みのマーカーが残ったまま、今回の bulk が部分失敗しても
    （`_human_md_config_sig` が pending でなくなっているため）ES 自身の `_meta` には
    `ensure_index()`（bulk 実行**前**）の時点で「成功して確定した版」が書かれてしまう——
    bulk の成否と無関係に meta が確定値になり、欠けた索引が固定される「二段階更新の穴」になる。
    **この無効化自体が失敗（`OSError`）した場合は index_world() を呼ばず fail-closed で
    終える**（RAG-KV 提案書 `.rag_sig` の契約と同型）——削除できたか分からない古いマーカーを
    残したまま再索引すると、上記と同じ「meta が確定値のまま固定される」穴を再現しかねないため、
    索引を開始せず失敗記録だけ残して次回 sync に委ねる。

    bulk が成功（`available` かつ `error` キー無し）した時だけ `confirm_human_md_es_sig` で
    マーカーを再確定し、**続けて `es_index.confirm_human_md_meta()` で ES 自身の
    `_meta.human_md_sig` も現行署名へ書き直す**——`ensure_index()` は bulk 実行前の時点で
    （このマーカー無効化直後は必ず pending のため）meta へ `None` を書いており、bulk 成功後に
    meta を書き直さないと、マーカーは確定済みなのに meta だけ `None` のまま残り、次回
    `needs_reindex()` が「None ≠ 現行版」を検知して**収束せず毎 sync 再索引し続ける**。

    次のいずれかが起きた場合は `ingest_runs` へ `status="failed"` で記録する: (a) マーカーの
    無効化自体が失敗、(b) `index_world()` 自体が失敗/未接続/例外、(c) confirm 自体の書込が
    失敗した（`.human_md_es_sig` マーカーの書込エラー）。meta の書き直し（`confirm_human_md_meta`）
    の失敗は実害が索引内容ではなく次回の自己修復ループの収束速度に留まるため、ここでは
    warning に留め ingest_runs へは記録しない。戻り値は `index_world()` の結果そのもの
    （呼ばなかった/例外時は `{"available": False, "error": ...}` を合成して返す＝呼び出し元は
    常に dict を受け取れる）。
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
    elif tracked and not office_md.confirm_human_md_es_sig(world_dir, derived_md_dir):
        failure_reason = "human_md_es_sig_marker_write_failed"
    elif tracked and not es_index.confirm_human_md_meta(world):
        _log.warning(
            "ES `_meta.human_md_sig` の書き直しに失敗しました（次回 sync まで human_md 次元が"
            "収束しない可能性）: world=%s", world)
    if failure_reason is not None:
        _record_es_index_failure(world, failure_reason, run_id=run_id)
    return esr


def _record_es_index_failure(world: str, reason: str, *, run_id: int | None = None) -> None:
    """ES 反映失敗を記録する。`run_id` 指定時は新規行を作らない——呼び出し元
    （`index_world_with_human_md_holdback` の `run_id` 引数 docstring 参照）が戻り値から
    自分で判断し、受付 run 自身の terminal 化に畳み込む契約のため、ここでは何もしない。"""
    if run_id is not None:
        return
    try:
        store.add_ingest_run(
            world, status="failed",
            extraction_snapshot={"stage": "es_index", "error": reason},
            created_by="admin")
    except Exception:
        _log.warning(
            "ES 反映失敗の ingest_runs 記録に失敗しました: world=%s", world, exc_info=True)


def _refresh_derived_representations(world, sig) -> str | None:
    """`sync()` の軽量再生成分岐（document_ir/evidence/rag drift のみで、arms drift・force・
    原本変化は無し）。呼び出し元は `store.world_lock` の中でこれを呼ぶ（derived ディレクトリへの
    書込を同一 world の並行 `run()`/`sync()` と競合させないため）。

    戻り値: sidecar 欠落を検知したら `"needs_full_run"`（この関数自身は `run()`/`_run_locked()`
    を呼ばない＝呼び出し元が同じ `store.world_lock` 区間の中で lock-free 版の `_run_locked()`
    を直接呼ぶ）。drift が無ければ `None`。それ以外（human_md/document_ir/evidence/rag のいずれか
    の軽量再生成を実行した・成否問わず）は `"handled"`——ただし呼び出し元（`sync()`）は
    `"handled"` でも backfill/ES 自己修復をスキップしない（human_md の軽量再生成が書き換える
    legacy `{rel}.md` は ES の索引元になりうる——RAG_ES OFF なら常に、RAG_ES ON でも
    `rag_chunks` が無効/劣化した文書は legacy 縮退で `{rel}.md` を読むため——同じ sync 呼び出し内で
    `needs_reindex` 自己修復まで到達させる必要がある・`sync()` docstring 参照）。ES 自体が
    この human_md 版まで実際に bulk 反映できたかは別途 `.human_md_es_sig` マーカー
    （`office_md.confirm_human_md_es_sig`・ホールドバック方式）で確定する——render 側
    （`asset_versions.human_md`）とは独立に、ES の bulk 成否を確認できる呼び出し元だけが
    確定できるため（`es_index._human_md_config_sig` docstring 参照）。

    sidecar 欠落の確認は drift の有無に関わらず**必ず先に**行う——バージョン定数は不変
    （drift 無し）のまま `.md`/`.md.meta.json`/`.evidence.json` が外部要因で欠落した場合、
    drift 判定だけを見ていると恒久的な no-op になり検知できない（sig マーカー自体は現行値と
    一致したままのため）。

    優先順位: ①sidecar 欠落→全再構築（他の全経路より先に確認・排他）。②human_md drift
    （`office_md.human_md_sig_drift`・rel ごとの `asset_versions.human_md` 版）→
    `refresh_human_md`（`{rel}.md` **だけ**の軽量再生成・単一 asset）。document_ir/evidence/rag
    のいずれの drift/連鎖とも独立（人間向け MD の版だけが変わっても rag.md/ES を巻き込まない・
    H2・正典 §10 裁定#4関連）ため②〜⑤とは排他ではなく**必ず個別に**確認・実行する。③document_ir
    drift→`refresh_document_ir`（document.json/blocks/chunks の軽量再生成・**全 OOXML 文書を対象に
    再生成する**＝docx/pptx/xlsx のどれか1つの抽出器版だけが上がった場合でも他の拡張子も
    含めて世界全体を作り直す。世界単位の1つのマーカーで判定するため）。④（③を実行した場合は
    それに続けて）evidence→rag→（RAG_ES 有効時は）ES 反映まで連鎖して再生成する。⑤（③を経ない
    場合）evidence drift→`refresh_evidence_ir`（evidence→rag も同時に面倒を見る）。⑥（③⑤の
    いずれも経ない場合）rag drift→`refresh_rag`。

    document_ir と evidence/rag は独立した抽出パイプラインだが、どちらも同じ原本抽出器
    （`ooxml_arm` 等）由来の値を版に含む——document_ir 版を上げる抽出器変更は evidence/rag の
    抽出結果にも影響しうるため、**document_ir を再生成した場合は evidence/rag 自身の drift
    判定結果によらず必ず evidence/rag（→ RAG_ES 有効時は ES）も連鎖再生成する**（取りこぼしを
    避ける）。document_ir/evidence の再生成自体は検索 consumer の設定（RAG の ES 反映有効/無効等）
    に左右されない既定の契約——`.rag_sig` の確定を ES 反映の成否まで保留するかどうかだけが
    その設定で変わる（マーカー保留方式）。

    document_ir 側の失敗分離（1文書の失敗で World 全体を止めない）: `refresh_document_ir` が
    1文書でも失敗しても、それだけで evidence→rag への連鎖を打ち切らない——打ち切ると、その
    失敗文書が直らない限り、成功した他の全文書分の evidence/RAG/ES まで永久に旧世代のまま
    固定されてしまう。連鎖を打ち切るのは `error` キー（overlap 等の構造的な setup 失敗＝
    1文書も処理できていない）が返った時だけにする。**`.document_ir_sig` は world 単位で1つの
    マーカーしか持たない**ため、1文書でも失敗が残る限り次回 sync の `document_ir_sig_drift`
    は world 全体として True のままになり、`refresh_document_ir` は対象 OOXML 文書を毎回
    全件再実行する（成功済みの文書だけ・失敗文書だけを選んで再試行する仕組みは無い）。
    連鎖先の evidence/rag（RAG_ES 有効時は ES 索引も）も、document_ir drift が続く限り
    毎回再実行される。sync 自体は既定でポーリング駆動ではない（手動更新/登録時のリラン等が
    契機）ため、この全件再実行の冗長さを個別最適化する必要は無いと判断している。

    document_ir マーカーの確定順（上流→下流の逆順で確定する）: document_ir 自体の生成が
    成功しても、その場で `.document_ir_sig` を確定しない（`refresh_document_ir` を
    `write_document_ir_sig_marker=False` で呼ぶ）。**連鎖した evidence/rag（さらに RAG_ES
    有効時は ES 反映）まで成功したことを確認できてから**、初めて `write_document_ir_sig_marker()`
    で確定する。先に確定してしまうと、evidence/rag/ES 側の連鎖が（例えば `.rag_sig` の
    ホールドバック削除失敗で）その場で失敗しても、次回 sync では document_ir drift が既に
    False（かつ evidence/rag 自身の drift も、連鎖の起点になった時点で既にそれぞれ False
    だったケースでは変化しない）になり、**再試行の入口そのものが失われる**（恒久的に
    Evidence/RAG/ES が旧世代のまま固定される）。
    """
    wd = worlds.world_dir(world)
    dmd = worlds.derived_md_dir(world)
    if not wd or not dmd.exists():                      # text/code のみの world は評価対象が無い
        return None
    if office_md.rag_sidecars_missing(wd, dmd):          # drift の有無によらず常に確認する
        return "needs_full_run"
    # human_md drift は document_ir/evidence/rag のいずれとも独立（②のみ・rag/ES には触れない）。
    # 排他分岐の外で必ず確認する＝document_ir 等に drift が無くても human_md だけ古ければ拾う。
    human_md_handled = False
    if office_md.human_md_sig_drift(wd, dmd):
        hm_result = office_md.refresh_human_md(wd, dmd)
        if hm_result.get("human_md_failed", 0):
            _log.warning(
                "human_md の軽量再生成で一部の文書が失敗しました（次回 sync で再試行）: "
                "world=%s detail=%s", world, hm_result)
        human_md_handled = True
    document_ir_drift = office_md.document_ir_sig_drift(dmd)
    evidence_drift = office_md.evidence_ir_sig_drift(dmd)
    # `rag_sig_drift` の OCR 観測次元（O1）: 直近 sync 以降に OCR が新しい観測世代を公開していれば
    # ここが True になり、evidence が不変でも rag drift 経由で `refresh_rag` を誘発する
    # （OCR 完了後の rag.md/ES への「追いつき」は、この既存 drift 連鎖に乗せる・新しい仕組みは作らない）。
    rag_drift = office_md.rag_sig_drift(dmd, world=world)
    if not document_ir_drift and not evidence_drift and not rag_drift:
        return "handled" if human_md_handled else None
    document_ir_ok = True                                # document_ir を経由しない経路では常に真のまま
    if document_ir_drift:
        doc_result = office_md.refresh_document_ir(wd, dmd, write_document_ir_sig_marker=False)
        if doc_result.get("error"):                      # 構造的な setup 失敗＝1文書も処理できていない
            _log.warning(
                "document_ir の軽量再生成に失敗しました（次回 sync で再試行）: world=%s detail=%s",
                world, doc_result)
            return "handled"
        if doc_result.get("document_ir_failed", 0):
            document_ir_ok = False                       # world単位マーカー未確定のまま＝今回もevidence/rag連鎖は継続
            _log.warning(
                "document_ir の軽量再生成で一部の文書が失敗しました（マーカーは world 単位のため"
                "全 OOXML 文書を対象に次回 sync も再実行されます・今回分の evidence/rag への"
                "連鎖は継続します）: world=%s detail=%s", world, doc_result)
    defer = es_index.rag_es_enabled()                    # RAG_ES有効時だけマーカー保留方式（ES成否込みで確定）
    if document_ir_drift or evidence_drift:
        result = office_md.refresh_evidence_ir(wd, dmd, write_rag_sig_marker=not defer, world=world)
        ok = not result.get("error") and result.get("evidence_ir_failed", 0) == 0 \
            and result.get("rag_failed", 0) == 0
    else:
        result = office_md.refresh_rag(wd, dmd, write_rag_sig_marker=not defer, world=world)
        ok = not result.get("error") and result.get("rag_failed", 0) == 0
    if not ok:
        _log.warning(
            "RAG/Evidence IR の軽量再生成に失敗しました（次回 sync で再試行）: world=%s detail=%s",
            world, result)
        return "handled"
    # rv-s2-mention #1: rag.md が実際に書き換わった（`ok`）ので、ES 反映の成否に関わらずグラフ
    # （言及エッジ）を追いつかせる。呼び出し元（`_sync_impl`）が既に `store.world_lock` を保持中
    # ＝lock-free ヘルパーをそのまま呼ぶ（`_reflect_graph_after_rag_rewrite` docstring 参照）。
    _reflect_graph_after_rag_rewrite(world)
    es_ok = True                                         # holdback対象外（defer=False）なら確定済み扱い
    if defer:
        # human_md は RAG_ES の設定に関わらず ES の索引内容に影響しうる（rag_chunks 無効時の
        # legacy 縮退経路）ため、共通ヘルパが `.human_md_es_sig` の無効化/確定/失敗記録まで
        # 一元的に面倒を見る（`index_world_with_human_md_holdback` docstring 参照）。
        esr = index_world_with_human_md_holdback(world, content_sig=sig)
        es_ok = esr.get("available") is True and not esr.get("error")
        if es_ok:
            office_md.write_rag_sig_marker(dmd, world=world)
        else:
            _log.warning(
                "RAG refresh後のES再索引が失敗しました（次回 sync で再試行）: world=%s", world)
    # document_ir マーカーは、①document_ir自体が全件成功し（document_ir_ok）、②連鎖した
    # evidence/rag（と該当すれば ES 反映）も成功した（es_ok）ことを確認できてから確定する
    # （上の docstring 参照＝先に確定すると再試行の入口を失う）。
    if document_ir_drift and document_ir_ok and es_ok:
        office_md.write_document_ir_sig_marker(dmd)
    return "handled"


def sync(world, *, reflect=True, force=False, run_id=None, on_run_id=None, op: str = "sync") -> dict:
    """`_sync_impl` の薄いラッパー（L5・§8.6-4）。sync 本体は無変更のまま、成功後に rag.md の LLM
    成形をバックグラウンドで後追い起動する（`llm_render.schedule_background`・world 単位で多重起動
    しない・取りこぼしても次回 sync が再度契機になり収束する）。world が解決できなかった
    （`status="unavailable"`）場合は起動しない——派生物自体が存在しない/古いままの可能性があるため。
    背景起動自体の失敗は best-effort（`sync()` の戻り値・例外伝播には影響させない）。

    `SHERPA_TEST_DB_ISOLATED`（隔離テスト DB・`tests/conftest.py` が pytest 実行中は常に立てる内部
    フラグ）が立っている間は起動しない——`sherpa.reconcile.reconcile_derivatives()` の全面 skip と
    同じ fail-safe。`sync()` は数百のテストから直接呼ばれるため、無条件で daemon thread を
    起動すると (a) LLM 呼び出しを伴わないテスト経路にまで実 DB 読み取り（`system_settings`）を
    無警告に追加する、(b) `graph_extract.available`/`complete_json` を独自に monkeypatch している
    別のテスト経由で、意図しないタイミングで `.rag.md` を書き換えて他テストのアサーションと
    競合する、の2つの実害を生む。本番はこのフラグを立てないため既定 ON のまま影響しない。
    """
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
    """変更検知つき取り込み（手動「今すぐ更新」/ ポーリング/ 登録ボタンのリラン用）。**変わった時だけ**再ビルドする。

    署名が前回と同じ（かつ `force=False`）なら no-op（`changed=False`）。違えば `run` する。
    署名不変でも human_md/document_ir/evidence/rag のいずれかの版だけが drift した場合は
    `_refresh_derived_representations` による軽量再生成（分岐②③④⑤）を経由する
    （RAG-KV-001・§3.2）。その後（`"handled"` でもスキップしない）の ES 自己修復
    （`es_index.needs_reindex`→`index_world`）が成功したら `office_md.confirm_human_md_es_sig`
    で `.human_md_es_sig` マーカーを確定し、bulk_errors 等の部分失敗時は確定せず
    `store.add_ingest_run(status="failed")` で監査に残す（次回 sync が自動で再試行する）。

    secRV 再RV round-2（HIGH-1）: 署名の確定は `_run_locked`（`run` 経由・world_lock 保持中）だけが行う。
    ここ（`sync` 自身）は `run` 復帰**後**（＝lock 解放後）に確定を書き足さない＝他プロセスの
    pre-invalidate/削除が確定していた場合にそれを有効署名で上書き（番兵復活）する穴を作らない。

    `run_id`（ING-3）＝呼び出し元が受付時に O(1) で確保済みの `ingest_runs` 行。全域
    分岐（`_run_locked` を経由する `needs_full_run`/`run` 呼び出し）はそのまま `run_id` を転送する
    ——`_run_locked` が完了時に terminal 化する。**`_run_locked` に到達しない分岐**
    （world 未解決／完全な unchanged）は `sync` 自身がこの関数の最後で `run_id` を terminal 化
    する（「unchanged も同じ run を terminal 化」する契約——未消化のまま `status='extracting'` の
    行を残さない）。`on_run_id`＝`run_id` を渡さない代わりに、`_run_locked` 経由の分岐でのみ
    run_id 判明時に呼ばれるコールバック（旧経路・後方互換）。
    """
    def _finalize_if_unused(status: str, reasons: list[str] | None = None) -> None:
        # `_run_locked` を経由しない終了点専用（呼び出し元 run_id が未消化のまま残らないようにする）。
        if run_id is None:
            return
        try:
            snap = {"changed": False}
            if reasons:
                snap["flags"] = [{"doc": None, "action": "warn", "reason": r} for r in reasons]
            store.finish_ingest_run(run_id, status=status, extraction_snapshot=snap)
        except Exception:
            _log.warning(
                "sync の unchanged/unresolved run 確定に失敗しました（best-effort）: world=%s run_id=%s",
                world, run_id, exc_info=True)
            return
        # PART-6: `_run_locked`（延いては `_record`）を経由しないこの terminal 化専用の
        # 分岐でも Webhook 通知を発火する（`_run_locked` の world 未解決分岐＝`_record("failed")`
        # と同じ状況をここでも terminal 化するため）。
        try:
            webhooks.notify_run_terminal(world, run_id, op, status)
        except Exception:
            _log.warning("Webhook 通知の起動に失敗しました（sync 自体は継続）: world=%s", world,
                        exc_info=True)

    def _progress(stage, done=None, total=None):
        # rv-oom-resume item6（2026-09-05）: `_run_locked` の同名クロージャ（上部）と同じ形——
        # `sync()` の unchanged 分岐は `_run_locked` を経由しないため、進捗記録が一切配線されて
        # いなかった（最初の world_state 走査・ES 自己修復とも「実環境で数時間動かないまま」に
        # なりうる）。`run_id` が無い（CLI 直接呼び出し等）分岐は no-op。
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

    # RV是正（rv-periphery #4・2026-09-05）: unchanged 自己修復（下の ES 修復分岐）の
    # `index_world_with_human_md_holdback` progress コールバックは、従来 doc グループ（flush）
    # 単位の呼び出しをそのまま `_progress`（DB 書込み）へ転送しており、`_run_locked` 側の
    # `_es_progress`（`_PROGRESS_FILE_INTERVAL`＝100件間隔・先頭/末尾のみ必ず書く）と同じ間引きが
    # 掛かっていなかった（大規模 world の自己修復で毎 flush ごとに DB 書込みが積み重なる）。
    _last_unchanged_es_progress_done = [None]

    def _unchanged_es_progress(done, total):
        last = _last_unchanged_es_progress_done[0]
        if last == done:              # #3(c) と同じ同値抑止
            return
        if done == 0 or done == total or last is None or done - last >= _PROGRESS_FILE_INTERVAL:
            _last_unchanged_es_progress_done[0] = done
            _progress("es_index", done=done, total=total)

    _progress("scanning")
    sig, manifest = world_state(world, progress=lambda n: _progress("scanning", done=n, total=None))
    if sig is None:
        _finalize_if_unused("failed", ["world_unresolved"])
        return {"world": world, "changed": False, "status": "unavailable"}
    row = store.get_world(world)
    prev = row.get("last_sig") if row else None
    if not force and prev == sig and not _derived_stale(world):  # 無変化＝再ビルドしない（派生MD欠落時は除く・RV High#1）
        # rv-oom-resume item4（2026-09-05）: unchanged 経路の ES 自己修復（`needs_reindex`→
        # `index_world_with_human_md_holdback`）を含め、この分岐の**全て**を単一の
        # `store.world_lock` 区間に収める（従来は `_refresh_derived_representations` 呼び出し
        # 直後で `with` を閉じてしまい、バックフィル・ES 自己修復・マーカー確定が lock 外で
        # 走っていた——その間に他プロセスの並行 sync/rebind/delete が割り込むと、ES 反映が
        # 参照した派生物と実際の world 世代が食い違いうる）。バックフィル用の第2の
        # `store.world_lock` 呼び出しは同一ロックの**再入**になり自己デッドロックしうるため
        # 削除し、同じ lock 区間へ直接畳み込む（`store.world_lock` は session-level advisory
        # lock＝別コネクションでの再入不可・`wipe_world` docstring 参照）。
        with store.world_lock(world):                    # derived への書込を同一worldの並行run/syncと直列化
            refresh_outcome = _refresh_derived_representations(world, sig)
            if refresh_outcome == "needs_full_run":
                # 欠落検知→全再構築→`.rag_sig`削除を同一lock区間で行う（lockを一度解放して公開
                # `run()`を呼ぶと、その間に他のsync/registerが割り込んで全再構築が重複したり、
                # `.rag_sig`削除だけがlock外に取り残されたりする非原子性を生む）。`run()`自身が
                # 取り直す非再入lockと衝突しないよう、ここでは lock-free 版の `_run_locked` を
                # 直接呼ぶ（`worlds.rebind`/`_wipe_locked` と同じ流儀）。この「同一lock区間で
                # 原子的に実行する」が正の契約（RAG-KV-001・§9.3はこの契約に更新済み）。
                res = _run_locked(world, reflect=reflect, created_by="admin", scan_root=None,
                                  run_id=run_id, on_run_id=on_run_id, op=op)
                if es_index.rag_es_enabled() and not office_md.drop_rag_sig_marker(worlds.derived_md_dir(world)):
                    _log.warning(
                        "sidecar欠落からの全再構築後、`.rag_sig`の削除に失敗しました"
                        "（ES再索引の再試行契機を逃す可能性）: world=%s", world)
                return {"world": world, "changed": True, "status": res["status"],
                        "ledger": res["ledger"], "flags": list(res.get("flags", []))}
            # `refresh_outcome == "handled"`（軽量再生成を実行済み）でもここで早期 return しない: human_md
            # の軽量再生成（`refresh_human_md`）は RAG_ES OFF の world で ES の索引元そのもの
            # （legacy `{rel}.md` の40行チャンク）を書き換えるため、直後の needs_reindex 自己修復まで
            # 同じ sync 呼び出し内で到達しないと、ES が古いままの世代が次回 sync まで残ってしまう。
            # document_ir/evidence/rag 側の軽量再生成は `{rel}.md` 自体に触れないため、ここを通っても
            # `needs_reindex` は通常 False のまま（無害な追加チェック1回で済む）。軽量再生成で失敗した
            # rel が残っていても、それは次回 sync の drift 判定が再試行する＝ここで後続処理を止める
            # 理由にはならない。
            # `last_manifest` は JSONB 列＝内容が空の world（本文0件）なら正当な値として `{}` が
            # 入りうる。「欠落」の判定は `is None` で行う（`not {}` は真になるため、空 dict を
            # 「未設定」と取り違えて空 world を同期のたびに毎回バックフィル対象にしてしまう）。
            needs_manifest_backfill = row is not None and row.get("last_manifest") is None
            # last_doc_count 列の導入前に成功同期が確定していた既存 world は、内容が不変（unchanged
            # 経路）のままだと二度と _run_locked を通らず、document_count が永久に null のままになる。
            needs_doc_count_backfill = row is not None and row.get("last_doc_count") is None
            # last_scan_report 列の導入前に成功同期が確定していた既存 world も同様:
            # 内容不変のままだと `GET /worlds/{wid}/status` がずっと「未集計」を返し続ける。
            needs_scan_report_backfill = row is not None and row.get("last_scan_report") is None
            if needs_manifest_backfill or needs_doc_count_backfill or needs_scan_report_backfill:
                # 既に本関数の外側 `with` で lock 保持中——ここで再度 `store.world_lock` は
                # 呼ばない（呼べば同一 lock の再入＝別コネクションでの自己デッドロック）。
                cur = store.get_world(world)                    # 他 writer が割り込んでいないか再読
                if cur is not None and cur.get("last_sig") == sig:
                    if cur.get("last_manifest") is None and cur.get("last_doc_count") is None:
                        # 両方 NULL＝1回の UPDATE でまとめて補完する（last_synced_at は変更しない・
                        # 2ステップに分けて先に set_world_sig() で manifest だけ書くと、そちらが
                        # last_synced_at=now() を書いてしまい「いつ確定したか」を偽ってしまう）。
                        store.backfill_manifest_and_doc_count(
                            world, manifest, corpus_docs.manifest_doctype_count(manifest, world), sig)
                    else:
                        if cur.get("last_manifest") is None:
                            store.set_world_sig(world, sig, manifest=manifest)
                            cur = store.get_world(world)         # last_manifest が埋まった最新行を使い直す
                        if cur.get("last_doc_count") is None:
                            saved_manifest = cur.get("last_manifest")
                            if saved_manifest is None:
                                saved_manifest = manifest
                            # last_synced_at は更新しない（`backfill_doc_count` 自体がそういう契約・
                            # 「いつ確定したか」の事実を後追い補完で書き換えない）。
                            store.backfill_doc_count(
                                world, corpus_docs.manifest_doctype_count(saved_manifest, world), sig)
                    if cur.get("last_scan_report") is None:
                        # sig 一致を確認済みの区間内＝この世代の内容に対する scan_report として正当。
                        # `set_scan_report` は `last_synced_at` を更新しない（sig 確定の事実を書き換えない）。
                        try:
                            store.set_scan_report(world, corpus_docs.scan_report(world))
                        except Exception:
                            _log.warning(
                                "取り込み集計（scan_report）のバックフィルに失敗しました: world=%s",
                                world, exc_info=True)
                # 不一致（他プロセスが無効化/更新済み）なら何もしない＝上書きしない。この skip 時も
                # 呼び出し元へは（下の return で）status="unchanged" を返す＝バックフィルできなかった
                # 今回の表示は保守的（実際には他プロセスが変更中/無効化した可能性がある）だが安全性の
                # 問題は無い＝次回 sync が実際の状態を正しく判定して収束する（Codex LOW・2026-07-14）。
            # ES 修復: 空/署名ズレ/埋め込みプロバイダ変更を検知して張り直す（管理UI不要）。失敗は
            # 別 run を作らず受付 run（`run_id`）自身の終端へ畳み込む——`index_world_with_human_md_holdback`
            # へ `run_id` を渡すことで内部の失敗記録を抑止し、ここで一度だけ terminal 化する。
            es_repair_failure = None
            try:
                if es_index.needs_reindex(world, sig):
                    _progress("es_index", done=0, total=None)
                    esr = index_world_with_human_md_holdback(
                        world, content_sig=sig, run_id=run_id,
                        progress=_unchanged_es_progress)
                    if not (esr.get("available") is True and not esr.get("error")):
                        es_repair_failure = esr.get("error") or "unavailable"
            except Exception as e:
                _log.warning(
                    "ES 自己修復中に予期しない例外が発生しました: world=%s", world, exc_info=True)
                es_repair_failure = e.__class__.__name__
            if es_repair_failure is not None:
                _finalize_if_unused("failed", [f"es_repair_failed:{es_repair_failure}"])
            else:
                _finalize_if_unused("auto_published")
            return {"world": world, "changed": False, "status": "unchanged", "ledger": 0}
    # RV是正#7: `op` を渡し忘れると `run()` の既定 "sync" に固定され、この呼び出し元が実際には
    # refresh/rerun 等でも Webhook payload の `op` が常に "sync" になってしまう——`op` を配線する。
    res = run(world, reflect=reflect, run_id=run_id, on_run_id=on_run_id, op=op)   # 署名の確定/無効化は run 内部（_run_locked）が lock 内で行う
    return {"world": world, "changed": True, "status": res["status"],
            "ledger": res["ledger"], "flags": list(res.get("flags", []))}


def _reindex_after_rag_rewrite(world: str) -> bool:
    """rag.md が世代を変えずに書き換わった（LLM 成形の反映・規則版への一掃）後、ES へ載せ直す。

    `_refresh_derived_representations` の holdback 分岐（`.rag_sig` を先に落としてから
    `index_world_with_human_md_holdback` を呼び、bulk 成功でだけ確定する）と同じ順序を、
    `store.world_lock` 区間の中で行う（derived への書込を並行 sync と直列化する・
    `_refresh_derived_representations` docstring 参照）。**RAG_ES が無効な world では ES に
    触れる必要が無い**ため、その場合は無条件で成功扱いにする（マーカー操作自体をスキップ）。
    グラフ反映（`_reflect_graph_after_rag_rewrite`・rv-s2-mention #1）は RAG_ES の有無に
    関わらず常に行う——言及エッジは ES とは独立に陳腐化しうる。
    """
    row = store.get_world(world)
    sig = row.get("last_sig") if row else None
    if not sig:
        return False
    dmd = worlds.derived_md_dir(world)
    with store.world_lock(world):
        _reflect_graph_after_rag_rewrite(world)
        if not es_index.rag_es_enabled():
            return True
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
    """`sync()` 成功後にバックグラウンド thread から呼ばれる LLM 成形の1回分（L5・§8.6-4）。

    `llm_render.run_world_pass` はファイル書込までを担い、ここでは書き換わった rel が
    1件でもあれば ES への反映（`_reindex_after_rag_rewrite`）まで面倒を見る。個々のファイル
    書込は `store.world_lock` を取らない（LLM 呼び出しを含み長時間になりうるため、その間
    正規の sync/削除等を長くブロックしないトレードオフ——`write_text_atomic` により個々の
    ファイル書込自体は原子的で、競合時の最悪ケースは「次回パスで再度処理される」だけ）。
    """
    from . import llm_render
    result = llm_render.run_world_pass(world)
    if result.changed_rels:
        _reindex_after_rag_rewrite(world)


def regenerate_rag_rule_only(world: str) -> dict:
    """当該 world の LLM 成形キャッシュを一掃し、rag.md を規則版へ作り直す（管理者の明示操作・
    §8.6-2「規則版で再生成」）。トグルの無効化（残す）とは独立——監査要件等で LLM 出力を今すぐ
    一掃したいケース専用。トグルが ON のままなら、次の背景パスが改めて LLM 成形を試みうる
    （一掃は時点操作であり、恒久的に LLM 成形を止めたいなら合わせてトグルを OFF にすること）。

    既存の `office_md.refresh_rag`（Evidence IR から決定的に再生成する経路）をそのまま再利用する
    ——「LLM 版を規則版へ逆変換する」のではなく、確立済みの生成経路を呼び直すことで rag.md が
    常に規則版であることを保証する（`_stamp_rule_only_rag_markdown` により書込時に必ず
    `生成手段: 規則` が刻まれる）。`store.world_lock` は `refresh_rag` 呼び出し元の既存契約に合わせ、
    ここで1区間として確保する。
    """
    from . import llm_render
    wd = worlds.world_dir(world)
    dmd = worlds.derived_md_dir(world)
    if not wd or not dmd.exists():
        return {"status": "unavailable"}
    llm_render.clear_cache(world)
    defer = es_index.rag_es_enabled()          # RAG_ES有効時はマーカー保留方式（ES成否込みで確定・sync()と同じ流儀）
    with store.world_lock(world):
        result = office_md.refresh_rag(wd, dmd, write_rag_sig_marker=not defer, world=world)
    if result.get("error") or result.get("rag_failed", 0):
        _log.warning(
            "規則版への再生成が一部失敗しました: world=%s detail=%s", world, result)
        return {"status": "partial_failure", **result}
    es_ok = _reindex_after_rag_rewrite(world)
    return {"status": "ok" if es_ok else "es_reindex_failed", **result}


def wipe_world(world, *, reflect=True) -> dict:
    """world の派生物を**完全削除**（delete の前段）: グラフ（Neo4j）＋台帳＋ES。

    **world 単位 advisory lock で run と直列化**（同時 rebuild/delete の競合防止・RV High#1）。
    ロック取得はここだけの薄いラッパー（`_wipe_locked` へ委譲・R3-S3）。`worlds.delete` は既に
    外側で lock を取っているため lock-free の `_wipe_locked` を直接呼ぶ（session-level advisory lock は
    別コネクション再入不可＝ここを経由すると自己デッドロックする）。
    """
    with store.world_lock(world):
        return _wipe_locked(world, reflect=reflect)


def _wipe_locked(world, *, reflect) -> dict:
    """`wipe_world` の lock 未取得版（呼び出し元が既に world_lock を保持している前提・R3-S3）。

    **fail-closed**: グラフ削除に失敗したら例外を投げる（握りつぶさない・RV BLOCKER）。グラフを先に消し、
    成功してから台帳をクリアする（途中失敗で「台帳空・グラフ残」を作らない）。参照元の外部フォルダは消さない。

    pre-invalidate 設計（secRV 再RV・Codex finding #5・2026-07-14）: `last_sig` の無効化（`''`）は
    **関数冒頭**（Neo4j delete より前）で**ガード無し**に行う。旧実装は Neo4j delete 成功**後**に
    best-effort（例外握り潰し）で無効化していたが、これは (a) delete commit 直後〜無効化書き込みの間の
    クラッシュ窓、(b) 無効化自体が PG 断で失敗しても握り潰されて気付かれない、の2点で「グラフは空・
    registry 行は残存・last_sig は旧の（現ソースと一致する）値のまま＝sync が unchanged と誤判定する
    恒久サイレント不整合」を防ぎきれなかった。ここで先に無効化し、かつ失敗を伝播させることで:
    無効化が書けない（PG 断）なら削除自体を開始しない＝まだ何も壊れていない時点で失敗が可視化される。
    無効化が書けた後は、delete/replace/rmtree のどこで失敗・クラッシュしても last_sig は既に `''`
    （実 sig と一致しない番兵）＝次回 sync が必ず「変更あり」判定で再構築し、自己修復に収束する。
    """
    store.set_world_sig(world, "")                          # pre-invalidate（fail-closed・#5）
    deleted = 0
    if reflect:                                            # Neo4j 失敗は伝播（呼出側は registry を進めない）
        env = world_neo4j._env()
        deleted = world_neo4j.delete_world(world, env["uri"], env["user"], env["pw"])
    ledger = store.replace_documents(world, [])            # グラフ削除成功後に台帳クリア
    import shutil
    shutil.rmtree(worlds.derived_dir(world), ignore_errors=True)   # 派生MD（Office由来）も消す
    try:
        es_index.delete_world(world)                  # ES インデックスも削除（派生物の一括伝播）
    except Exception:
        pass
    return {"world": world, "ledger_cleared": ledger, "graph_deleted": deleted}
