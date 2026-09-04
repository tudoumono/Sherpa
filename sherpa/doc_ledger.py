"""文書台帳（鏡モデル・path 基準）。

旧 P3 台帳（version 別・basename キー・layer/scope_path・Postgres seed）は撤去。鏡では world の
**フォルダ木を走査**して文書を表す（doc_id＝rel_path）。scope/preview/DL がここを参照する単一の出所。
原本DL は `documents.resolve`（パス基準・root 限定）。特定テーマの名前は持たない。
"""
from __future__ import annotations

from . import corpus_docs, documents, scope_infer as si, store, worlds
from .ingest import importance
from .store.db import world_lock_shared


def documents_for(world: str, *, root=None, deadline: float | None = None, files=None) -> list:
    """world の文書一覧（rel_path＝doc_id・範囲メタ付き・走査由来）。

    `root`（省略可）: 呼び出し側が既に world root を解決済みなら渡す（`corpus_docs.world_documents`
    へそのまま転送・`worlds.world_dir()` を再度呼ばない）。
    `files`（省略可・キーワード専用）: 呼び出し側が既に `scope_infer.safe_files(root)` を1回
    materialize（`list(...)`）済みなら渡す（`corpus_docs.world_documents` へそのまま転送・二重の
    全木走査を避ける・`root` と同じ理由。§③ 2026-09-01・`preview_service.build_preview` 参照）。
    渡す場合は**同じ list を複数回渡してよい形**（generator ではなく materialize 済み）にすること
    ——下流が複数回イテレートしうる。
    `deadline`（省略可・既定 None＝無期限＝既存呼び出し元は無変更）: `corpus_docs.world_documents`
    （ツリー走査）と `corpus_docs.last_run_blocked_docs`（直近 run の DB 参照）の両方へ転送する
    （PART-4 の `agentic_search.run_tool` が残り時間ベースで渡す・list_docs ツールの打切り契約を
    ツリー走査だけでなく DB 参照にも及ぼす——後者だけ無期限のまま素通りさせない）。

    既定 accepts の言語（cobol/copybook/jcl）は `resolve_lazy` が内容を読まない短絡（分類短絡・
    §7 裁定10）を維持するため、走査（`classify_document`）だけでは実際の読み取り失敗を検知できない。
    直近 ingest run の blocked flag（`corpus_docs.last_run_blocked_docs`＝実際にファイルを開く
    `world_graph.build_world` Pass1 の結果）を突き合わせ、該当する doc だけ `state="unreadable"`
    へ上書きする（doctype/branch は走査結果のまま＝「何のファイルか」は分かる状態を保つ）。

    直近 run 自体が**確認できなかった**場合（DB 例外・打切り期限超過＝`last_run_blocked_docs` が
    `None`）は、「blocked 無し」と混同せず、分類短絡の対象（`branch=="source"`・現在 `state=
    "ready"`）の doc を `state="unknown"`（表示「状態を確認できませんでした」）へ倒す——黙って
    「使えます」のままにしない（fail-closed の表示側版）。分類短絡の対象外（Office/画像/md/txt 等）
    は `classify_document` が内容/派生MDの有無から直接判定済みのため対象外（この経路の不調とは無関係）。
    """
    rows = corpus_docs.world_documents(world, root=root, deadline=deadline, files=files)
    blocked = corpus_docs.last_run_blocked_docs(world, deadline=deadline)
    if blocked is None:
        return [
            {**r, "state": "unknown", "label": "状態を確認できませんでした", "reason": None}
            if r.get("branch") == "source" and r.get("state") == "ready" else r
            for r in rows
        ]
    if not blocked:
        return rows
    out = []
    for r in rows:
        reason = blocked.get(r["name"])
        if reason and r.get("state") != "unreadable":
            r = {**r, "state": "unreadable", "label": "読み取れません", "reason": reason}
        out.append(r)
    return out


def _importance_fields(rel: str, res_map: dict) -> dict:
    """`importance`/`importance_reason`/`importance_source` を、値があれば返す（無ければ空 dict）。

    `importance_source`＝勝者となった `_重要度.txt` の rel_path と行番号（登録者への由来表示用）。
    """
    res = res_map.get(rel)
    if res is None:
        return {}
    out = {"importance": res.value, "importance_source": f"{res.config_path}:{res.rule_line}行目"}
    if res.reason:
        out["importance_reason"] = res.reason
    return out


def public_documents(world: str) -> list:
    """API/画面向け（**物理パスは出さない**＝rel_path とメタのみ）。

    `status` は `documents_for()`（走査結果＋直近 run の blocked flag 突き合わせ済み）の `state`
    をそのまま通す——`unreadable`（内容判定に必要なヘッダが読み取れない・実読込失敗）・
    `unknown`（直近 run の確認自体ができなかった）を黙って `ready` と同列にしない。
    登録者が `_重要度.txt` で付けた重要度（値・理由・由来）も、あれば best-effort で付ける
    （§2 truth table＝無ければ3キーとも付けない）。
    """
    # 文書列挙と重要度解決は同一 root から行う（別々に world_dir() を解決すると、その間隔の
    # rebind で「旧 root の文書一覧」に「新 root の重要度」を付けてしまいうる）。root が解決
    # できなければここで打ち切る（fail-closed）——`root=None` のまま下流へ渡すと、
    # `documents_for`/`resolve_for_world` はそれぞれ「root 省略」と解釈して**独立に**
    # `worlds.world_dir()` を再解決してしまい、閉じたはずの rebind 競合が再び開く。
    wd = worlds.world_dir(world)
    if not wd:
        return []
    res_map = importance.resolve_for_world(world, root=wd)
    return [{"name": r["name"], "top_scope": r.get("top_scope"), "phase": r.get("phase"),
             "category": r.get("category"), "doctype": r.get("doctype"),
             "branch": r.get("branch"), "status": r.get("state", "ready"),
             **_importance_fields(r["name"], res_map)}
            for r in documents_for(world, root=wd)]


# 台帳（`store.documents`）の `status` は ingest 時点のスナップショット（`ingest/worker.py::
# _ledger_rows` が書く）。`public_documents()`（実走査＋直近run再突合）の表示語彙に合わせるための
# 最小マッピング（`indexed`→`ready`）。`unreadable`/`_reconcile_ledger_blocked` が付ける
# `unknown` はそのまま通す。
_LEDGER_STATUS_DISPLAY = {"indexed": "ready"}


def _ledger_row_to_public(r: dict) -> dict:
    """台帳の1行（`store.list_documents_page` 由来）→ 公開形（`public_documents()` と同じ形）。

    `top_scope`/`phase`/`category` は `name`（rel_path）から `scope_infer.rel_scope_meta` で
    その場で導出する（純関数・I/O なし）——台帳には持たせていない列のため。
    `importance`/`importance_reason`/`importance_source`（RV1是正#2）は台帳列（`ingest/worker.py::
    _ledger_rows` が ingest 時に `importance.resolve_for_world` で解決し materialize 済み）を
    そのまま通す——ここで実走査はしない。§2 truth table＝無ければ3キーとも付けない
    （`doc_ledger._importance_fields` と同じ契約）。
    """
    meta = si.rel_scope_meta(r["name"])
    status = r.get("status")
    out = {"name": r["name"], "top_scope": meta["top_scope"], "phase": meta["phase"],
           "category": meta["category"], "doctype": r.get("doctype"), "branch": r.get("branch"),
           "status": _LEDGER_STATUS_DISPLAY.get(status, status or "ready")}
    if r.get("importance") is not None:
        out["importance"] = r["importance"]
        out["importance_source"] = r.get("importance_source")
        if r.get("importance_reason"):
            out["importance_reason"] = r["importance_reason"]
    return out


def _reconcile_ledger_blocked(rows: list, blocked) -> list:
    """台帳行（`status` キー・"indexed"/"unreadable"）に直近 run の blocked flag を突き合わせる
    （RV1是正#2・RV2是正#b2）。`documents_for()`（実走査版・`state` キー）と同じ判断をキー名だけ
    台帳側に合わせたもの——台帳の `status="unreadable"` は ingest 時点で `_ledger_rows` が既に
    反映済みだが、直近 run 自体が**確認できなかった**場合（DB 例外・打切り期限超過＝
    `blocked is None`）は、分類短絡の対象（`branch=="source"`・現在 `status=="indexed"`）の行
    だけを "unknown"（表示「状態を確認できませんでした」相当）へ倒す——黙って「使えます」の
    ままにしない（fail-closed の表示側版・`documents_for` と同じ契約）。`branch=="source"` 限定
    なのは `documents_for()` と同じ理由（既定 accepts の言語＝cobol/copybook/jcl は
    `resolve_lazy` が内容短絡するため実読込失敗を検知できず、直近 run の突き合わせが必要な
    対象がソース枝に限られる）——Office/画像/Markdown（`branch!="source"`）は分類時点で内容確認
    済みのため対象外（誤って "unknown" にしない）。`corpus_docs.last_run_blocked_docs` は DB のみの
    読み取り（フォルダを歩かない）ため、台帳高速経路に足しても定数時間の契約は崩れない。
    """
    if blocked is None:
        return [
            {**r, "status": "unknown"} if r.get("branch") == "source" and r.get("status") == "indexed" else r
            for r in rows
        ]
    if not blocked:
        return rows
    out = []
    for r in rows:
        if r["name"] in blocked and r.get("status") != "unreadable":
            r = {**r, "status": "unreadable"}
        out.append(r)
    return out


def public_documents_page(world: str, *, limit: int | None, offset: int = 0) -> tuple[list, int]:
    """`GET /documents` の応答本体（総件数＋文書一覧・S工事②・RV1是正#1/#5・2026-09-01）。

    `limit=None`（後方互換・既定）: **全件**を返す——旧クライアント（`limit`/`offset` を知らず
    `documents` だけを読む）が既定 200 件で黙って切られないようにする（RV1 finding #1）。
    `limit` を明示指定した時だけ有界（LIMIT/OFFSET）にする。`offset` は `limit` 指定時のみ意味を
    持つ（`limit=None` では無視し常に全件）。

    台帳（`store.documents`）に行があれば**そこだけ**を読む——`store.count_documents`（索引範囲の
    COUNT）＋`store.list_documents`/`list_documents_page`（`name` 順・後者のみ LIMIT/OFFSET）＋
    `corpus_docs.last_run_blocked_docs`（直近 run の blocked flag・DB のみ）だけで、フォルダは
    一切歩かない（ING-2 の `last_scan_report` と同じ「同期時点のスナップショットを読むだけ」の
    考え方）。2TB 級 world でも応答は文書総数に比例しない。

    世代整合（RV1 finding #5）: COUNT〜ページ取得〜blocked 突き合わせの3クエリは別接続だと、
    その間に背景 sync（`worker.run`）が台帳を丸ごと置換（`replace_documents` は DELETE→INSERT）
    すると `total` と実際に返す `documents` が別世代の内容から混ざり得る（空ページなのに
    `has_more=true` 等）。既存の `world_lock_shared`（読み取り専用の共有ロック・`sherpa/routers/
    documents.py::doc_download` と同じ・rebind/delete/sync の排他ロックと直列化）でこの3クエリを
    囲み、単一の世代スナップショットに固定する——新しいロック機構は作らない。sync 実行中は
    この間 `GET /documents` がブロックされうるが、`doc_download` に既にある同じ trade-off
    （`world_lock_shared` は sync/rebind/delete と相互排他）を踏襲する。

    台帳が空（一度も ingest run が成功していない world・未登録の dev fixture world を含む）の時
    だけ、既存の `public_documents`（実走査＋importance 付き・後方互換の唯一の経路）へフォール
    バックしてからメモリ上でスライスする——小規模/未登録 world は従来どおり動く（この経路は
    `world_lock_shared` の対象外＝実走査自体が1回の自己完結したスナップショットのため）。
    """
    with world_lock_shared(world):
        total = store.count_documents(world)
        if total > 0:
            rows = (store.list_documents(world) if limit is None
                   else store.list_documents_page(world, limit=limit, offset=offset))
            rows = _reconcile_ledger_blocked(rows, corpus_docs.last_run_blocked_docs(world))
            docs = [_ledger_row_to_public(r) for r in rows]
            return docs, total
    all_docs = public_documents(world)
    if limit is None:
        return all_docs, len(all_docs)
    return all_docs[offset:offset + limit], len(all_docs)


def preview_documents(world: str, *, root=None, files=None, sig: str | None = None) -> list:
    """取り込みプレビュー用の文書一覧（走査 canonical → 画面の形）。物理パスは出さない。

    派生MD を持つ Office/画像枝の文書には「どう読み取ったか」の要約（`provenance`＝method/confidence/
    legacy_backend?/has_conflicts?）を best-effort で付ける（S2・表示のみ・データ源は既存の来歴サイドカー）。
    ソース文書（`md_path` 無し）や meta.json 欠落は付けない＝後方互換。`md_path`（物理パス）自体は出さず、
    サイドカーを読むためだけに使う。登録者が `_重要度.txt` で付けた重要度（値・理由・由来）も、
    あれば同様に best-effort で付ける（§2 truth table＝無ければ3キーとも付けない）。

    `state`/`label`/`reason` は `documents_for()`（走査結果＋直近 run の blocked flag 突き合わせ済み）
    の同名フィールドをそのまま通す——Office の未抽出箇所通知・`unreadable`（読み取り不可）・
    `unknown`（直近 run を確認できなかった）を「使えます」で一律に上書きしない。

    `analyzer`＝担当アナライザの内部名（`corpus_docs.iter_world_documents` 参照・コード文書のみ
    非 `None`）。`doctype`（種別表示用）とは独立した値——§7 裁定2 の受入条件（取り込み画面で
    担当アナライザの来歴を参照できるようにする）のため一覧応答に含める。

    `root`/`files`（省略可・キーワード専用）: 呼び出し側が既に world root を解決し・ファイル列挙
    （`scope_infer.safe_files`・materialize 済み list）を済ませているなら渡す（`documents_for`／
    `importance.resolve_for_world` へそのまま転送・二重の全木走査を避ける・§③ 2026-09-01・
    `preview_service.build_preview` 参照）。省略時は従来どおりここで `world_dir()` を解決する。
    `sig`（省略可・キーワード専用）: 呼び出し側が既に world の `last_sig`（登録済み世界のみ・空でない
    値）を把握しているなら渡す——`importance.resolve_for_world` はこれが `None` だと
    `worker.world_signature_of_root(wd)` で**もう1回**全木走査してキャッシュキー用の署名を作る
    （`files` を渡していても避けられない別の走査）。渡せばそれを回避する。未登録 world（`sig` が
    空文字）は渡さない——`resolve_for_world` 側の署名キャッシュ契約（メタデータ変化での自動失効）を
    空文字で固定してしまわないため（呼び出し元 `preview_service._compute_preview` 参照）。
    """
    # 文書列挙と重要度解決は同一 root から行う（public_documents と同じ理由・fail-closed も同じ）。
    wd = root if root is not None else worlds.world_dir(world)
    if not wd:
        return []
    res_map = importance.resolve_for_world(world, root=wd, files=files, sig=sig)
    out = []
    for r in documents_for(world, root=wd, files=files):
        doc = {"name": r["name"], "doctype": r.get("doctype"), "branch": r.get("branch"),
               "analyzer": r.get("analyzer"),
               "top_scope": r.get("top_scope"), "phase": r.get("phase"),
               "category": r.get("category"), "folder": "/".join(r["name"].split("/")[:-1]),
               "state": r.get("state", "ready"), "label": r.get("label", "使えます"),
               "reason": r.get("reason"),
               **_importance_fields(r["name"], res_map)}
        prov = corpus_docs.provenance_summary(r.get("md_path"))   # md_path 無し/欠落は None＝付けない
        if prov:
            doc["provenance"] = prov
        out.append(doc)
    return out


def control_diagnostics(world: str, *, root=None, files=None) -> list:
    """`_重要度.txt` の構文診断（台帳・取り込みプレビューの警告バナー用・§6）。

    `root`/`files`（省略可）: `importance.diagnostics_for_world` へそのまま転送（§③ 2026-09-01・
    `preview_service.build_preview` が既に列挙済みのファイル一覧を渡し、二重の全木走査を避ける）。
    """
    return importance.diagnostics_for_world(world, root=root, files=files)


def original_path(rel: str, world: str):
    """根拠DL の原本 Path（パス基準・root 限定・無ければ None）。"""
    return documents.resolve(rel, world)
