"""`/ext/v1/capabilities` の `document_count`（`worlds.last_doc_count`）を実 worker 実行で固定する
（要 PG+Neo4j・mock 直値ではなく `worlds.register`/`worlds.rebind` の実処理経由）。

- 原本件数は「検索可能になった台帳数」ではなく「doctype 対応**原本**の総数」（変換されない
  legacy Office も含む）であることを固定する。
- rebind 直後は新 root の取り込みが終わるまで `last_doc_count`/`last_sig` が確定しない
  （旧世代の値を新 root に結び付けない）ことを、実際の rebind 経由で固定する。
- `last_doc_count` 列の導入前に成功同期が確定していた既存 world（NULL のまま残る）は、内容が
  不変（unchanged 経路）のまま `worker.sync()` を呼ぶだけで後追い補完される・`last_synced_at`
  は書き換えない。
"""
from __future__ import annotations

import pathlib
import shutil
import tempfile
import threading

from _world_setup import driver

from sherpa import store, worlds

W = "test_ext_doc_count_real"


def _mk_cbl(root, gen, prog):
    d = pathlib.Path(root) / gen / "03_開発" / "01_ソース"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{prog}.cbl").write_text(
        "       IDENTIFICATION DIVISION.\n"
        f"       PROGRAM-ID. {prog}.\n"
        "       PROCEDURE DIVISION.\n           STOP RUN.\n", encoding="utf-8")


def _mk_legacy_doc(root, gen, name):
    """`.doc`（legacy Word）——決定的変換の対象外＝検索可能な台帳（`documents`）には載らないが、
    `corpus_docs.status_document_doctype()` は "Word(旧)" を返す＝原本としては存在する。
    """
    d = pathlib.Path(root) / gen / "03_開発" / "01_ソース"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504)


def _mk_vision_only_image(root, gen, name):
    """`.bmp`（vision（VLM）経路のみで変換される画像・PNG/JPEG と違い決定的metadata経路が無い）。
    既定は vision 無効のためこの実行環境では変換されず台帳（`documents`）には載らないが、
    `corpus_docs.status_document_doctype()` は "画像" を返す＝原本としては存在する
    （`manifest_doctype_count()` と台帳件数が本当に食い違う fixture）。
    """
    d = pathlib.Path(root) / gen / "03_開発" / "01_ソース"
    d.mkdir(parents=True, exist_ok=True)
    # 最小の有効 BMP（1x1 px・24bit）。内容の正しさは問わない（原本として存在すればよい）。
    (d / f"{name}.bmp").write_bytes(
        b"BM" + (54 + 3).to_bytes(4, "little") + b"\x00\x00\x00\x00" + (54).to_bytes(4, "little")
        + (40).to_bytes(4, "little") + (1).to_bytes(4, "little") + (1).to_bytes(4, "little")
        + (1).to_bytes(2, "little") + (24).to_bytes(2, "little") + b"\x00" * 24 + b"\x00\x00\x00")


def _cleanup(wid, roots, drv):
    try:
        worlds.delete(wid)
    except Exception:
        pass
    store.delete_world_row(wid)
    with drv.session() as s:
        s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=wid)
    drv.close()
    for r in roots:
        shutil.rmtree(r, ignore_errors=True)


def test_document_count_matches_manifest_doctype_count_not_hardcoded():
    """`last_doc_count` は実 worker 実行（`worlds.register`→`_run_locked`）が書いた値で、
    その場で独立に再スキャンした manifest から `corpus_docs.manifest_doctype_count()` を
    計算した値と一致する（mock 直値ではなく実処理の結果であることの確認）。

    `.bmp`（vision 経路のみで変換される画像・既定は vision 無効のためこの世界では**決して**
    台帳に載らない）を混ぜることで、台帳件数（`len(rows)`）と `manifest_doctype_count()` が
    **実際に食い違う** fixture にする——`.doc` だけだと環境（LibreOffice 導入済み）次第で
    変換されて台帳にも載ってしまい、「`len(rows)` に回帰しても偶然一致して検出できない」
    弱いテストになっていた。
    """
    from sherpa import corpus_docs
    from sherpa.ingest import worker

    a = tempfile.mkdtemp()
    _mk_cbl(a, "案件X", "REALCOUNT1")
    _mk_legacy_doc(a, "案件X", "REALCOUNT2")
    _mk_vision_only_image(a, "案件X", "REALCOUNT3")
    wid = W + "_originals"
    drv = driver()
    try:
        res = worlds.register(wid, str(pathlib.Path(a).resolve()))
        assert res["status"] in ("auto_published", "auto_published_with_flags")

        row = store.get_world(wid)
        assert row["last_sig"], "取り込み成功確定済みのはず"

        # 独立した再スキャン（register 内部の manifest とは別に、この場でもう一度 world_state を
        # 引き直す）。中身は変えていないので同じ集合になるはず。
        _, manifest = worker.world_state(wid)
        expected = corpus_docs.manifest_doctype_count(manifest, wid)
        ledger_count = len(store.list_documents(wid))
        assert expected >= 3, "少なくとも .cbl・.doc・.bmp の3件は doctype 対応原本のはず"
        assert ledger_count < expected, (
            f"fixture が台帳件数（{ledger_count}）と doctype 対応原本件数（{expected}）で"
            "食い違っていない＝ .bmp が意図せず変換されている可能性（vision が既定 ON になった等）")
        assert row["last_doc_count"] == expected, (
            f"last_doc_count={row['last_doc_count']} が manifest_doctype_count()={expected} と不一致"
            "（worker.py の確定呼び出しが len(rows) 等へ回帰していないか確認）")
    finally:
        _cleanup(wid, [a], drv)


def test_rebind_does_not_leak_old_generation_count_to_new_root():
    """rebind 直後、新 root の取り込みが完了するまで `last_doc_count`/`last_sig` は
    旧世代の確定値のまま新 root に結び付けて見せない（`store.rebind_bind_invalidate_sig` が
    root 更新と同一 tx で無効化する）。取り込み完了後は新 root の実件数に一致する。"""
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    _mk_cbl(a, "案件X", "OLDGEN1")
    _mk_cbl(a, "案件X", "OLDGEN2")   # 旧 root: .cbl 2件
    _mk_cbl(b, "案件Y", "NEWGEN1")   # 新 root: .cbl 1件だけ（旧と異なる件数にして取り違えを検出可能にする）
    wid = W + "_rebind"
    drv = driver()
    try:
        worlds.register(wid, str(pathlib.Path(a).resolve()))
        assert store.get_world(wid)["last_doc_count"] == 2

        worlds.rebind(wid, str(pathlib.Path(b).resolve()))   # 内部で新 root の取り込みまで完了する
        row = store.get_world(wid)
        assert row["root_path"] == str(pathlib.Path(b).resolve())
        assert row["last_sig"], "rebind 成功後は確定済みのはず"
        assert row["last_doc_count"] == 1, (
            f"新 root（.cbl 1件）の件数に一致するはずが last_doc_count={row['last_doc_count']}"
            "（旧世代の値2が漏れていないか確認）")
    finally:
        _cleanup(wid, [a, b], drv)


def test_rebind_exposes_invalidated_transient_state_and_preserves_storage_mode():
    """rebind の bind 更新＋`last_sig`/`last_doc_count` 無効化は `_run_locked` の再構築より
    **先に・同一 tx で**確定する（`rebind_bind_invalidate_sig`）。他プロセスからは一瞬
    root=新・last_sig=''・last_doc_count=NULL という中間状態が観測できることを、実際に
    バックグラウンドでポーリングして固定する（前段の test は「rebind 直後」の終状態しか
    見ておらず、この中間状態そのものは検証していなかった）。

    あわせて `storage_mode`（`managed_copy`）が rebind を通じて保持されることも確認する
    （`rebind_bind_invalidate_sig` へ `storage_mode=old.get("storage_mode")` を明示的に
    引き継がないと既定の "external_reference" に化ける・`sherpa/worlds.py::rebind` docstring
    参照）。
    """
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    _mk_cbl(a, "案件X", "TRANSOLD1")
    _mk_cbl(a, "案件X", "TRANSOLD2")   # 旧 root: .cbl 2件
    _mk_cbl(b, "案件Y", "TRANSNEW1")   # 新 root: .cbl 1件
    wid = W + "_rebind_transient"
    drv = driver()
    try:
        worlds.register(wid, str(pathlib.Path(a).resolve()), storage_mode="managed_copy")
        before = store.get_world(wid)
        assert before["storage_mode"] == "managed_copy", "前提: managed_copy で登録できていない"

        new_root = str(pathlib.Path(b).resolve())
        observed: list[tuple] = []
        stop_poll = threading.Event()

        def _poll():
            while not stop_poll.is_set():
                row = store.get_world(wid)
                if row is not None:
                    observed.append((row["root_path"], row["last_sig"], row["last_doc_count"]))

        poller = threading.Thread(target=_poll)
        poller.start()
        try:
            worlds.rebind(wid, new_root)   # 内部で新 root の取り込みまで完了する
        finally:
            stop_poll.set()
            poller.join(timeout=5)

        transient_hits = [o for o in observed if o[0] == new_root and o[1] == "" and o[2] is None]
        assert transient_hits, (
            "rebind 中に root=新・last_sig=''・last_doc_count=NULL という中間状態を"
            "一度も観測できなかった（bind 更新＋無効化が _run_locked の再構築完了後にずれて"
            "確定している回帰の可能性・ポーリング間隔が粗すぎる場合も要確認）")

        after = store.get_world(wid)
        assert after["root_path"] == new_root
        assert after["last_sig"], "rebind 成功後は確定済みのはず"
        assert after["last_doc_count"] == 1
        assert after["storage_mode"] == "managed_copy", (
            f"storage_mode が保持されていない: {after['storage_mode']}"
            "（rebind_bind_invalidate_sig への引き継ぎ漏れ）")
    finally:
        _cleanup(wid, [a, b], drv)


def test_sync_backfills_last_doc_count_for_existing_null_row():
    """`last_doc_count` 列の導入前に成功同期が確定していた（NULL のまま残っている）既存 world は、
    内容不変（unchanged 経路）のまま `worker.sync()` を呼ぶだけで後追い補完される。
    `last_synced_at` は書き換えない（「いつ確定したか」の事実を後追い補完で偽らない）。
    Neo4j 不要（unchanged 経路は Neo4j に触れない・`worlds.register` を経由しない）。
    """
    from sherpa.ingest import worker

    root = pathlib.Path(tempfile.mkdtemp())
    wid = W + "_backfill_null"
    try:
        _mk_cbl(root, "案件X", "BACKFILL1")
        _mk_cbl(root, "案件X", "BACKFILL2")   # .cbl 2件

        # `worlds.register()`（Neo4j 込みの本反映）を経由せず、「last_doc_count 列の導入前に
        # 成功同期が確定していた」状態を直接組み立てる（sig/manifest は確定済み・doc_count だけ無い）。
        store.upsert_world(wid, str(root.resolve()))
        sig, manifest = worker.world_state(wid)
        assert sig is not None
        store.set_world_sig(wid, sig, manifest=manifest)   # doc_count 省略＝旧仕様を模擬
        before = store.get_world(wid)
        assert before["last_doc_count"] is None
        assert before["last_synced_at"] is not None
        synced_at_before = before["last_synced_at"]

        res = worker.sync(wid)   # 内容不変＝unchanged 経路（Neo4j に触れない）

        assert res["changed"] is False
        assert res["status"] == "unchanged"
        after = store.get_world(wid)
        assert after["last_doc_count"] == 2, (
            f"last_doc_count が補完されていない: {after['last_doc_count']}")
        assert after["last_synced_at"] == synced_at_before, (
            "last_synced_at が書き換わっている（後追い補完は confirmed 時刻を偽らないはず）")
    finally:
        store.delete_world_row(wid)
        shutil.rmtree(root, ignore_errors=True)


def test_sync_backfills_manifest_and_doc_count_together_when_both_null():
    """`last_manifest`・`last_doc_count` が**両方** NULL のまま残っている、より古い既存 world
    （両列の導入前に成功同期が確定していた行）は、内容不変（unchanged 経路）のまま
    `worker.sync()` を呼ぶだけで両方まとめて補完される。`last_synced_at` は書き換えない
    （`store.backfill_manifest_and_doc_count()` が1回の UPDATE でまとめて書くため・2ステップに
    分けて先に `set_world_sig()` で manifest だけ書くと、そちらが `last_synced_at=now()` を
    書いてしまい「いつ確定したか」を偽ってしまう）。Neo4j 不要。
    """
    from sherpa.ingest import worker

    root = pathlib.Path(tempfile.mkdtemp())
    wid = W + "_backfill_both_null"
    try:
        _mk_cbl(root, "案件X", "BOTHNULL1")
        _mk_cbl(root, "案件X", "BOTHNULL2")   # .cbl 2件

        store.upsert_world(wid, str(root.resolve()))
        sig, _manifest = worker.world_state(wid)
        assert sig is not None
        store.set_world_sig(wid, sig)   # manifest/doc_count 両方省略＝より古い旧仕様を模擬
        before = store.get_world(wid)
        assert before["last_manifest"] is None
        assert before["last_doc_count"] is None
        assert before["last_synced_at"] is not None
        synced_at_before = before["last_synced_at"]

        res = worker.sync(wid)   # 内容不変＝unchanged 経路（Neo4j に触れない）

        assert res["changed"] is False
        assert res["status"] == "unchanged"
        after = store.get_world(wid)
        assert after["last_manifest"], "last_manifest が補完されていない"
        assert after["last_doc_count"] == 2, (
            f"last_doc_count が補完されていない: {after['last_doc_count']}")
        assert after["last_synced_at"] == synced_at_before, (
            "last_synced_at が書き換わっている（後追い補完は confirmed 時刻を偽らないはず）")
    finally:
        store.delete_world_row(wid)
        shutil.rmtree(root, ignore_errors=True)


def test_sync_of_empty_world_does_not_bump_last_synced_at_on_repeated_calls():
    """本文ファイルが1件も無い world（`last_manifest` が正当な空 dict `{}` で確定済み）は、
    `worker.sync()` を連続で呼んでも `last_synced_at` が書き換わらない。

    `last_manifest`/`last_doc_count` の「欠落」判定は `is None` で行うべきところ、
    `not cur.get("last_manifest")` のような truthy 判定だと `{}`（正当な空値）を「未設定」と
    取り違え、確定済みの空 world を同期のたびに毎回バックフィル対象と誤認して
    `last_synced_at` を書き換えてしまう。Neo4j 不要（unchanged 経路）。
    """
    from sherpa.ingest import worker

    root = pathlib.Path(tempfile.mkdtemp())   # 本文ファイルは1件も置かない＝意図的に空
    wid = W + "_empty_world"
    try:
        store.upsert_world(wid, str(root.resolve()))
        sig, manifest = worker.world_state(wid)
        assert sig is not None
        assert manifest == {}, "前提: 空ディレクトリの manifest は空 dict のはず"
        # `_run_locked` の確定パスと同じ形（manifest・doc_count を両方渡す）で確定済み状態を作る。
        store.set_world_sig(wid, sig, manifest=manifest, doc_count=0)
        before = store.get_world(wid)
        assert before["last_manifest"] == {}
        assert before["last_doc_count"] == 0
        assert before["last_synced_at"] is not None
        synced_at_before = before["last_synced_at"]

        for _ in range(3):   # 連続同期（毎回 unchanged のはず）
            res = worker.sync(wid)
            assert res["changed"] is False
            assert res["status"] == "unchanged"

        after = store.get_world(wid)
        assert after["last_manifest"] == {}
        assert after["last_doc_count"] == 0
        assert after["last_synced_at"] == synced_at_before, (
            "last_synced_at が書き換わっている（空 manifest を『未設定』と誤認して"
            "不要なバックフィルを繰り返している可能性）")
    finally:
        store.delete_world_row(wid)
        shutil.rmtree(root, ignore_errors=True)
