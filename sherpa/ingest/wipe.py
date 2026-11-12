"""資料フォルダの派生物の完全削除と、登録の失敗の補償。
設計: docs/design/data.md「削除の伝播」
"""
from __future__ import annotations

import logging
import shutil

from .. import es_index, store, worlds
from ..store.replace_rows import ReplaceAborted, ReplaceRow
from . import replace_guard, world_neo4j

_log = logging.getLogger("sherpa")


def wipe_locked(world, *, reflect, row: ReplaceRow) -> dict:
    """資料フォルダの派生物を完全削除する（delete の前段）: グラフ（Neo4j）＋台帳＋OCR＋派生物＋ES。lock 未取得版（呼び出し元が world_lock と world_swap_lock を保持している前提）。

    ① 何かを消す前に OCR 観測ディレクトリの削除対象を検証する（原本・登録 root と重なる設定なら例外で何も消さない）。
    ② `last_sig` を `''` に無効化する（Neo4j delete より前に、ガード無しで・失敗は伝播させる）。
       書けなければ削除を開始せず、書けた後はどこで失敗しても次回 sync が必ず再構築する。
    ③ グラフを削除する（失敗は握りつぶさず例外にする）。④ 成功してから台帳をクリアする（「台帳空・グラフ残」を作らない）。
    ⑤ OCR 観測ディレクトリと OCR の job/cache/run を消す。⑥ 派生物を消す。⑦ ES インデックスを消す。⑧ 全部成功してから失敗の一覧も消す。
    どの段も失敗は伝播させる（呼び出し側は行を残し、資料フォルダの行を消さない）。参照元の外部フォルダは消さない。
    """
    obs_dir = worlds.observation_removal_target(world)     # ①
    store.set_world_sig(world, "")                          # ②
    deleted = 0
    if reflect:                                            # ③
        env = world_neo4j._env()
        deleted = world_neo4j.delete_world(world, env["uri"], env["user"], env["pw"], row=row)
    ledger = store.replace_documents(world, [], row=row)            # ④
    from ..store import ocr_jobs
    if obs_dir is not None:
        shutil.rmtree(obs_dir)                             # ⑤
    ocr_jobs.purge_world(world, row=row)
    try:
        shutil.rmtree(worlds.derived_dir(world))           # ⑥
    except FileNotFoundError:
        pass
    delete_es_index(world, row=row)                        # ⑦
    store.clear_failed_docs(world)                         # ⑧
    return {"world": world, "ledger_cleared": ledger, "graph_deleted": deleted}


def delete_es_index(world: str, *, row: ReplaceRow) -> None:
    """資料フォルダの ES 索引を消す。消せなかった・ES に届かなかったときは例外にする（行を残して、ES が戻ってから消し切る）。"""
    if not es_index.delete_world(world, row=row):
        raise RuntimeError(f"ES の索引を消せませんでした: world={world}")


def compensate_failed_register(world_id: str, run_id: int | None, *, reflect: bool) -> bool:
    """登録の失敗の補償: 行を `kind='delete'` に変えてから全段を消し、全段が成功したときだけ資料フォルダの行と入れ替えの行を同じトランザクションで消す。
    どれかが失敗したら資料フォルダの行と `kind='delete'` の行を残して False を返す（元の失敗の例外は置き換えない）。`world_lock` を持つ呼び出し元から呼ぶ。
    """
    try:
        with replace_guard.held_row(world_id, run_id, kind="delete") as row:
            wipe_locked(world_id, reflect=reflect, row=row)
            store.delete_world_row(world_id)
        return True
    except (Exception, ReplaceAborted):
        _log.warning("登録の失敗の補償削除が最後まで進みませんでした（資料フォルダの行と削除の行は残します）: world_id=%s",
                     world_id, exc_info=True)
        return False
