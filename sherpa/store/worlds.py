"""world レジストリ（world_id → 参照元 root_path・鏡モデル）。

`sherpa/store/__init__.py` から純移動（フェーズ4 S5）。`world_lock` は S1 で db.py へ純移動済み
（このモジュールでは扱わない）。
"""
from __future__ import annotations

import math
import time

from psycopg.types.json import Json

from .db import _KB_ID, _connect, _ensure


def upsert_world(world_id, root_path, label=None, storage_mode="external_reference") -> dict:
    """world の参照バインドを登録/更新。`label=None` の更新は既存 label を保持（COALESCE・RV Low）。

    同じ root を別 world に登録すると `worlds_root` UNIQUE 違反（呼出側＝API/worlds が事前に 409 で防ぐ）。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO worlds (kb_id, world_id, root_path, label, storage_mode) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (kb_id, world_id) DO UPDATE SET root_path=EXCLUDED.root_path, "
            "  label=COALESCE(EXCLUDED.label, worlds.label), storage_mode=EXCLUDED.storage_mode, updated_at=now() "
            "RETURNING world_id, root_path, label, storage_mode, created_at, updated_at",
            (_KB_ID, world_id, root_path, label, storage_mode)).fetchone()


def rebind_bind_invalidate_sig(world_id, root_path, label=None,
                               storage_mode="external_reference") -> dict:
    """rebind の**新 root へのバインド更新**と**`last_sig`/`last_doc_count`/`last_scan_report(+at)`
    の無効化**を同一 tx で確定する。

    `upsert_world()`（root だけ更新）を使うと、`worker._run_locked` 冒頭の pre-invalidate
    （`store.set_world_sig(world, "")`）は `world_state()` の**全木スキャン後**にしか実行されない
    ため、root=新・`last_sig`/`last_doc_count`=旧（前の世代の確定値）という中途状態が
    スキャンが終わるまで（大規模 root では長時間）そのまま観測されてしまう
    （`/ext/v1/capabilities` が旧世代の件数・更新時刻を新 root に結び付けて公開する）。
    ここで root 更新と同時に `last_sig=''`（pre-invalidate と同じ番兵）・`last_doc_count=NULL`
    を確定することで、その窓を無くす。`last_scan_report`/`last_scan_report_at` も同時に NULL に
    しないと、新 root の同期が終わるまで**旧 root の集計**が新 root の件数として表示され続ける
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO worlds (kb_id, world_id, root_path, label, storage_mode) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (kb_id, world_id) DO UPDATE SET root_path=EXCLUDED.root_path, "
            "  label=COALESCE(EXCLUDED.label, worlds.label), storage_mode=EXCLUDED.storage_mode, "
            "  last_sig='', last_doc_count=NULL, last_scan_report=NULL, last_scan_report_at=NULL, "
            "  updated_at=now() "
            "RETURNING world_id, root_path, label, storage_mode, created_at, updated_at",
            (_KB_ID, world_id, root_path, label, storage_mode)).fetchone()


def restore_bind_invalidate_sig(world_id, root_path, label=None,
                                storage_mode="external_reference") -> None:
    """rebind 失敗時のロールバック用: **バインドを旧 root へ戻す＋last_sig を無効化**を**同一 tx**で確定する。

    R3-S3 RV（2026-07-14）: この2つを別々に書くと「bind=旧・sig=旧のまま」の中途状態が生じ得て、
    次回 sync が `prev == sig` で `unchanged` になり self-heal しない（Neo4j=新 のまま永続不整合）。
    1 tx で確定することで「bind を旧へ戻したなら sig は必ず無効化済み」を保証し、次回 sync が必ず
    再構築する（`last_sig=''` は実 sig と一致しない番兵）。tx 自体が失敗（PG 断）した時は**どちらも
    適用されない**＝bind=新のまま＝PG 復旧後の sync が新 root へ self-heal（いずれも整合状態へ収束）。
    """
    _ensure()
    with _connect() as c:
        c.execute(
            "INSERT INTO worlds (kb_id, world_id, root_path, label, storage_mode) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (kb_id, world_id) DO UPDATE SET root_path=EXCLUDED.root_path, "
            "  label=COALESCE(EXCLUDED.label, worlds.label), storage_mode=EXCLUDED.storage_mode, "
            "  last_sig='', updated_at=now()",
            (_KB_ID, world_id, root_path, label, storage_mode))


def get_world(world_id, *, connect_timeout: float | None = None,
             statement_timeout_ms: int | None = None) -> dict | None:
    """world 登録行を1件引く。

    `connect_timeout`/`statement_timeout_ms`（両方省略可・既定 None＝無期限＝既存呼び出し元は
    無変更）: PART-4（外部 API のリクエスト全体デッドライン）がこの registry 読み取り自体を
    無期限にブロックさせないために残り時間ベースで渡す。**接続の確立にかかった時間ぶんを
    差し引いた残りだけ**を `statement_timeout` へ渡す（`store.db.world_lock_shared` と同じ
    理由——呼び出し元は `connect_timeout` と `statement_timeout_ms` に同じ残り時間 R を渡すため、
    接続オプションへ両方とも接続**前**の値で焼き込むと、接続自体に R かかった上でさらに
    statement_timeout も R 残っているかのように振る舞い、実時間が最大で約 2R まで伸びうる
    （registry 読み取りは `world_lock_shared` 保持中に行われることがあり、共有ロックがその分
    余分に保持され続ける）。`SET LOCAL statement_timeout` を接続確立**後**に発行することで、
    connect_timeout での待ちと合算で2回消費しない。差引後の残りが0以下になっても0
    （Postgres では `statement_timeout=0` は「無効化＝無制限」を意味し逆効果）にはせず、
    最小1msへクランプしてほぼ即座に打ち切らせる。`connect_timeout` は `world_lock_shared` と
    同じ psycopg 3.3.4 丸め対策（整数秒へ切り上げ・最小1秒でクランプ）。

    未初期化（`_inited=False`）時は `_ensure()` が内部で `init_schema()`（advisory lock 待ち・DDL
    実行）を起動しうる——この分の経過時間も同じ予算から差し引く（差し引かないと、`_ensure()` が
    予算を消費した後も本関数自身の接続へ満額の `connect_timeout` が再付与され、合計の実時間が
    最大で約2倍まで伸びうる・`store.db.init_schema` と同型の是正）。そのため経過計測は
    `_ensure()` 呼び出し**前**から開始する。差し引いた残りが0以下なら、`max(1, ...)` で
    最低1秒へクランプして接続を開始することはせず、`TimeoutError` を送出して接続自体を
    開始しない（`_ensure()` だけで予算を使い切っていても新規接続を試みてしまう抜け穴を塞ぐ）。
    """
    budget_started = time.monotonic()   # `_ensure()` の消費分も差し引くため、その呼び出し前から計測する
    _ensure(connect_timeout=connect_timeout)
    connect_kwargs = {}
    if connect_timeout is not None:
        remaining = connect_timeout - (time.monotonic() - budget_started)
        if remaining <= 0:
            # `_ensure()`（未初期化時の schema 初期化）だけで予算を使い切った——ここで
            # `max(1, math.ceil(remaining))` により最低1秒へクランプして接続を試みると、
            # 既に期限を過ぎているのに新規の接続確立を開始してしまう。接続自体を開始せず、
            # 呼び出し元（`worlds.resolve_external_world`）が拾える例外にする。
            raise TimeoutError(f"get_world({world_id!r}): budget exhausted before connecting")
        connect_kwargs["connect_timeout"] = max(1, math.ceil(remaining))
    with _connect(**connect_kwargs) as c:
        if statement_timeout_ms is not None:
            elapsed_ms = (time.monotonic() - budget_started) * 1000
            remaining_ms = max(1, int(statement_timeout_ms - elapsed_ms))
            # SET LOCAL（session-level ではなく）: プール導入後（性能台帳#17 QW2）、この
            # with ブロック＝単一トランザクションの間だけ有効にし、返却後の接続に
            # statement_timeout が残らないようにする（GUC 汚染防止・commit/rollback で自動消滅）。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        return c.execute(
            "SELECT world_id, root_path, label, storage_mode, last_sig, last_synced_at, "
            "last_manifest, last_doc_count, last_scan_report, last_scan_report_at, created_at, updated_at "
            "FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world_id)).fetchone()


def get_world_status_row(world_id, *, connect_timeout: float | None = None,
                         statement_timeout_ms: int | None = None) -> dict | None:
    """`GET /worlds/{wid}/status` 専用の狭い SELECT——`get_world()` が持つ `last_manifest`
    （world 全ファイル分の rel→[mtime_ns,ctime_ns,size] を持つ JSONB・大規模 world では際限なく
    大きい）を含めない。status はこの列を一切使わない（差分検知は sync 側の専管）ため、
    行1件あたりの読取量を `last_manifest` の大きさに比例させない（O(1) 契約）。
    `last_sig` は GRA-1（`preview_service.graph_view` のプロセス内キャッシュ）も同じ狭い行を
    プローブに使う——`last_sig` は固定長の TEXT で `last_manifest` のような比例コストが無い。

    `connect_timeout`/`statement_timeout_ms`（両方省略可・既定 None＝無期限＝既存呼び出し元は
    無変更）は `get_world()` と**同じ方式**（`_ensure()` の消費分も予算から差し引く・接続確立
    **後**に残り時間で `SET LOCAL statement_timeout` を発行）。GRA-1是正RV2#2: `graph_view` が
    `_GRAPH_VIEW_LOCK` 保持中にこのプローブを呼ぶ経路は有限 timeout を渡す——DB が詰まっていても
    このプロセス内ロックを無期限に握ったまま（＝他の world の miss も含めて全滞留）にしない。
    """
    budget_started = time.monotonic()
    _ensure(connect_timeout=connect_timeout)
    connect_kwargs = {}
    if connect_timeout is not None:
        remaining = connect_timeout - (time.monotonic() - budget_started)
        if remaining <= 0:
            raise TimeoutError(f"get_world_status_row({world_id!r}): budget exhausted before connecting")
        connect_kwargs["connect_timeout"] = max(1, math.ceil(remaining))
    with _connect(**connect_kwargs) as c:
        if statement_timeout_ms is not None:
            elapsed_ms = (time.monotonic() - budget_started) * 1000
            remaining_ms = max(1, int(statement_timeout_ms - elapsed_ms))
            # SET LOCAL（session-level ではなく）: プール導入後（性能台帳#17 QW2）、この
            # with ブロック＝単一トランザクションの間だけ有効にし、返却後の接続に
            # statement_timeout が残らないようにする（GUC 汚染防止・commit/rollback で自動消滅）。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        return c.execute(
            "SELECT world_id, root_path, label, last_sig, last_synced_at, last_scan_report, "
            "last_scan_report_at FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world_id)).fetchone()


def world_by_root(root_path) -> dict | None:
    """その root_path にバインド済みの world（1:1 検証用・無ければ None）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT world_id, root_path, label, storage_mode FROM worlds WHERE kb_id=%s AND root_path=%s",
            (_KB_ID, root_path)).fetchone()


def set_world_sig(world_id, sig, manifest=None, doc_count=None, scan_report=None) -> None:
    """world の内容署名・ファイル明細・最終同期時刻を記録（変更検知/差分の基準）。

    呼び出し元は4用途で使う: ① **pre-invalidate**（`sig=''`・取り込み/削除開始前のガード無し番兵書き込み。
    `_run_locked`/`_wipe_locked` 冒頭）、② **wipe**（削除時の無効化。①と同じ pre-invalidate 経路に統合済み）、
    ③ **確定**（取り込み成功後に正しい署名＋明細へ更新。`_run_locked` の成功パスのみ）、
    ④ **manifest バックフィル**（`sync` の unchanged パス・lock 内で再読・署名一致を再確認した時のみ）。
    `manifest`＝rel→[mtime_ns,ctime_ns,size] の dict（差分チェック用）。None なら明細は更新しない。
    `doc_count`（外部公開 discovery の事前集計・`/ext/v1/capabilities` 用）は**③確定でのみ**渡す
    （`_run_locked` の成功パス・`manifest` から `corpus_docs.manifest_doctype_count()` で算出した
    doctype 対応原本件数——検索可能になった台帳行数〔`documents` テーブル〕とは別物で、変換され
    ない legacy Office 等も含む）。`scan_report`（`corpus_docs.scan_report()` の結果・ING-2）も
    **③確定でのみ**渡す——sig 確定と**同一 UPDATE 文**へ含めることで、`GET /worlds/{wid}/status`
    が読むキャッシュ（`last_scan_report`/`_at`）を sig/doc_count と同じトランザクションで一斉に
    確定させる（「成功確定した世代の sig と集計が食い違う瞬間」を作らない）。
    いずれも None なら該当列は更新しない（①②④で意図せず古い値を消さない＝pre-invalidate 中は
    「不明」ではなく前回確定値を列に残したまま、読み手側〔`resolve_external_world` 等〕が
    `last_sig` の真偽で信頼性を判断する）。

    不変条件（secRV 再RV round-2・2026-07-14／round-3 でコメント精度是正）: 呼び出し元は**必ず**
    world_lock 保持中に呼ぶこと（「原則」ではなく設計不変条件）。ロック外の後置き書き込みは、他プロセスが
    同じ world に対して行った pre-invalidate（`''`）や削除時の無効化を、古い有効署名で上書き＝復活させて
    しまう（番兵復活の穴）。`worlds.register`/`worlds.rebind` は round-3 でロック外の後置き呼び出しを
    除去済み＝現状の呼び出し元は `_run_locked`/`_wipe_locked`/`sync` の lock 内バックフィルのみ
    （`restore_bind_invalidate_sig` は bind 復元と同一 tx で last_sig を直接 SQL で書く別経路・この
    関数は経由しない）。
    """
    _ensure()
    sets = ["last_sig=%s", "last_synced_at=now()"]
    params: list = [sig]
    if manifest is not None:
        sets.append("last_manifest=%s")
        params.append(Json(manifest))
    if doc_count is not None:
        sets.append("last_doc_count=%s")
        params.append(doc_count)
    if scan_report is not None:
        sets.append("last_scan_report=%s")
        sets.append("last_scan_report_at=now()")
        params.append(Json(scan_report))
    params += [_KB_ID, world_id]
    with _connect() as c:
        c.execute(f"UPDATE worlds SET {', '.join(sets)} WHERE kb_id=%s AND world_id=%s", params)


def set_scan_report(world_id, report) -> None:
    """`corpus_docs.scan_report()` の結果を単独でキャッシュ（`set_world_sig` の sig 確定と同時に
    書けない箇所専用）。

    書き手: `ingest.worker.sync()` の unchanged 経路（旧 world に対する 1 回きりのバックフィル・
    sig は既に一致しているため書き直さない・lock 内で再確認済み）。`POST /worlds/{wid}/recount`
    は無条件のこの関数ではなく `set_scan_report_if_unchanged`（binding/世代のガード付き）を使う——
    走査自体を world_lock の外で行うため、書き戻し時に他の sync/rebind が割り込んでいないかの
    確認が要る。sig 確定を伴う通常の成功パスは `set_world_sig(..., scan_report=...)` を使う
    （同一 UPDATE 文へ折り込む）。`last_sig`/`last_doc_count` とは独立の列——`ingest_runs` の
    最新1件に紐付けない（直後の run が失敗しても、直前に成功した集計をそのまま表示し続けるため）。
    """
    _ensure()
    with _connect() as c:
        c.execute(
            "UPDATE worlds SET last_scan_report=%s, last_scan_report_at=now() "
            "WHERE kb_id=%s AND world_id=%s",
            (Json(report), _KB_ID, world_id))


def set_scan_report_if_unchanged(world_id, report, *, expected_root_path, expected_sig,
                                 expected_created_at, expected_updated_at,
                                 expected_last_synced_at, expected_last_scan_report_at) -> bool:
    """`POST /worlds/{wid}/recount` 専用: 走査を world_lock の**外**で行うため（2TB 級の root
    を排他ロック保持のまま走査すると、検索用共有ロックが timeout で 503 になり、timeout 指定の
    無い sync/rebind/delete は走査終了までブロックされる）、書き戻し時に binding（`root_path`）と
    行の各種世代マーカーが呼び出し元の読み取り時点から変わっていない場合だけ UPDATE する
    （`backfill_doc_count`/`backfill_manifest_and_doc_count` と同じ TOCTOU 対策の型）。

    `last_sig` だけを CAS 条件にすると ABA を見逃す——走査中に別の sync が pre-invalidate
    （`''`）を経て同じ内容（同じ sig）へ再確定した場合、`last_sig` は読み取り時点と一致して
    しまうが、その間に世代は実際には更新されている。`updated_at`（bind/rebind で更新）・
    `last_synced_at`（sig 確定で更新）・`last_scan_report_at`（前回集計時刻）・`created_at`
    （delete→同じ world_id で再登録されると変わる）も合わせて比較することで、いずれかが
    変化していれば「その間に何かが起きた」を検知できる。全列 `IS NOT DISTINCT FROM` で
    比較する（`last_sig`/タイムスタンプ列はいずれも NULL がありうる——未登録直後の
    初回同期前や、集計・同期が一度も成功していない world・素の `=` は NULL 同士を偽と評価し
    実際は変化していなくても毎回不一致になる）。

    戻り値: 実際に更新した行があれば True（binding/世代が不一致なら False＝呼び出し元は 409 で
    終了し、古い走査結果を新しい世代へ誤って結び付けない・再試行しない）。
    """
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE worlds SET last_scan_report=%s, last_scan_report_at=now() "
            "WHERE kb_id=%s AND world_id=%s AND root_path=%s AND last_sig IS NOT DISTINCT FROM %s "
            "AND created_at IS NOT DISTINCT FROM %s AND updated_at IS NOT DISTINCT FROM %s "
            "AND last_synced_at IS NOT DISTINCT FROM %s AND last_scan_report_at IS NOT DISTINCT FROM %s",
            (Json(report), _KB_ID, world_id, expected_root_path, expected_sig,
             expected_created_at, expected_updated_at,
             expected_last_synced_at, expected_last_scan_report_at)).rowcount
    return n > 0


def backfill_doc_count(world_id, doc_count, expected_sig) -> bool:
    """`last_doc_count` が NULL のまま残っている既存 world（`last_doc_count` 列の導入前に
    成功同期が確定していた行等）へ、保存済み manifest から算出した件数だけを補完する。

    `last_synced_at` は**変更しない**（`set_world_sig()` は呼び出しのたびに `now()` で更新するが、
    ここは「いつ確定したか」という事実を書き換えない・count の後追い補完はその世代の中身が
    最終的に反映された時刻を偽らない）。`expected_sig` と現在の `last_sig` が一致する場合のみ
    更新する（呼び出し元が world_lock 内で再確認した sig をそのまま渡すこと・TOCTOU 対策：
    この関数の実行中に他プロセスが sync/rebind で内容を書き換えていたら、古い manifest 由来の
    件数を新しい世代へ誤って結び付けない）。

    戻り値: 実際に更新した行があれば True（`expected_sig` が現在値と不一致なら False＝no-op）。
    """
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE worlds SET last_doc_count=%s WHERE kb_id=%s AND world_id=%s AND last_sig=%s",
            (doc_count, _KB_ID, world_id, expected_sig)).rowcount
    return n > 0


def backfill_manifest_and_doc_count(world_id, manifest, doc_count, expected_sig) -> bool:
    """`last_manifest`・`last_doc_count` が**両方** NULL のまま残っている既存 world（両列の導入前に
    成功同期が確定していた行等）へ、その場で再スキャンした manifest とそこから算出した件数を
    1回の UPDATE でまとめて補完する。

    manifest 補完だけを `set_world_sig()` で行い、件数補完を `backfill_doc_count()` で別に行う
    2ステップ構成だと、前者が `last_synced_at=now()` を書いてしまう（`set_world_sig()` の
    manifest-only 分岐の契約どおり）——バックフィルは「いつ確定したか」という事実を書き換えては
    いけない、という `backfill_doc_count()` と同じ不変条件に反する。ここでは1回の UPDATE に
    まとめることで `last_synced_at` を一切触れないようにする。

    `expected_sig` と現在の `last_sig` が一致する場合のみ更新する（`backfill_doc_count()` と同じ
    TOCTOU 対策・呼び出し元は world_lock 内で再確認した sig を渡すこと）。

    戻り値: 実際に更新した行があれば True（`expected_sig` が現在値と不一致なら False＝no-op）。
    """
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE worlds SET last_manifest=%s, last_doc_count=%s "
            "WHERE kb_id=%s AND world_id=%s AND last_sig=%s",
            (Json(manifest), doc_count, _KB_ID, world_id, expected_sig)).rowcount
    return n > 0


def list_worlds_db() -> list:
    """登録 world の一括取得（`last_sig`/`last_synced_at`/`last_doc_count` も含む）。

    呼び出し元が world 単位に `get_world()` を N 回引く代わりに、この1回の結果から
    `{world_id: row}` を組み立てて再利用できるようにするため（外部公開 discovery 等・
    per-world の追加ラウンドトリップを避ける）。`world_admin_service.public_world()` が
    公開フィールドを明示的に絞るため、ここで列を増やしても外部応答への漏洩は無い。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT world_id, root_path, label, storage_mode, last_sig, last_synced_at, "
            "  last_doc_count, created_at, updated_at "
            "FROM worlds WHERE kb_id=%s ORDER BY world_id", (_KB_ID,)).fetchall()


def delete_world_row(world_id) -> bool:
    """world レジストリ行を削除（派生物の wipe は呼出側＝worlds.delete が先に実行）。"""
    _ensure()
    with _connect() as c:
        n = c.execute("DELETE FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world_id)).rowcount
    return n > 0
