"""Neo4j/ES 上のテスト用 world 登録簿＋セッション終了時クリーンアップ。

背景（docs/proposals/2026-07-03-テストDB分離.md）: Neo4j は community 版のため物理的な
マルチ DB 分離ができない＝Postgres（`sherpa_test`）と違って専用インスタンスに逃がせない。
そのためテストが作る world_id を本モジュールに登録しておき、`tests/conftest.py` の
session-scoped autouse fixture がセッション終了時にまとめて Neo4j ノード／ES index を削除する
（`tests/_test_users.py` と同じ思想＝登録簿＋teardown 一括削除）。

既存の per-test 自前 cleanup（例: test_world_impact_a.py の `_cleanup`）を置き換えるものではなく、
テストが失敗して自前 cleanup に到達しなかった場合のバックストップとして重ねて効く
（起動時 `reconcile` と同じ多層防御の思想）。

**保護の二層構成**（2026-07-03 インシデント対応 HIGH#2: 固定 world_id 'v1' が実 Neo4j 上の
実登録 world と衝突し、テスト由来の孤児掃除が実データ（world 'test'・76ノード）を巻き込んだ
事故の再発防止）:
  1. 静的 denylist（`_DENYLIST`）＝最低限のフォールバック。base Postgres に接続できない時は
     これだけが頼り。
  2. **base（実・非隔離）Postgres の worlds レジストリへの直接照会**＝将来どんな固定 world_id を
     テストが使っても、それが実登録されていれば自動的に保護される（ハードコードの denylist に
     依存しない一般解）。`tests/conftest.py` がテスト用 DB 分離を有効化した時に設定する
     `SHERPA_ORIG_PG_DSN`（元 DSN）を使う。未設定/接続不可なら 1. の denylist 最低限にフォールバック
     （fail-safe＝「確認できない」を「安全」とは扱わない一方、既存の登録/削除機能自体は止めない）。

register 時・cleanup（削除）直前の**両方**でこのチェックを行う（cleanup 側は毎回その場で
再照会＝登録から時間が経っても最新の実レジストリで判定する最終防波堤）。
"""
from __future__ import annotations

import os

# 実登録 world（本番データ）。base Postgres に照会できない時の最低限のフォールバック。
_DENYLIST = {"test"}

_REGISTRY: list[str] = []


def _query_real_registered_world_ids() -> set | None:
    """base（実・非隔離）Postgres の `worlds` レジストリを直接照会する。

    `SHERPA_ORIG_PG_DSN` 未設定・接続不可・クエリ失敗はすべて None（呼び出し側は
    `_DENYLIST` 最低限にフォールバック）。他 sherpa.* への依存を避けるため psycopg を直接使う
    （store.py と同じ最下層の考え方）。
    """
    dsn = os.environ.get("SHERPA_ORIG_PG_DSN")
    if not dsn:
        return None
    try:
        import psycopg

        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as c:
            rows = c.execute("SELECT world_id FROM worlds").fetchall()
            return {r[0] for r in rows}
    except Exception:
        return None


def _is_protected(world_id: str, real_ids: set | None) -> bool:
    """denylist、または base Postgres に実在する world_id なら True（登録/削除を拒否）。"""
    if world_id in _DENYLIST:
        return True
    return bool(real_ids is not None and world_id in real_ids)


def register_test_world(world_id: str | None) -> None:
    """このプロセスが Neo4j/ES に書き込んだテスト用 world_id を記録する。

    denylist、および base Postgres に実登録済みの world_id は登録自体を無視する
    （多層防御の1層目・上のモジュール docstring 参照）。
    """
    if not world_id:
        return
    if _is_protected(world_id, _query_real_registered_world_ids()):
        return
    _REGISTRY.append(world_id)


def drain_registered_worlds() -> list[str]:
    """登録済み world_id を重複除去して取り出し、レジストリを空にする（session teardown から呼ぶ）。"""
    world_ids = list(dict.fromkeys(_REGISTRY))
    _REGISTRY.clear()
    return world_ids


def cleanup_worlds(world_ids: list[str]) -> dict:
    """指定 world_id の Neo4j ノード＋ES index を削除する。

    denylist・base Postgres 実レジストリは呼び出し経路に関わらず常に除外する
    （多層防御の2層目・register 側のチェックをすり抜けても最終防波堤になる。ここは
    キャッシュせずその場で再照会する＝登録時から時間が経っていても最新の実レジストリで判定）。
    Neo4j/ES 不到達は best-effort で握る（既存の `world_neo4j.delete_world`/`es_index.delete_world`
    と同じ・失敗しても後続の起動時 reconcile が backstop）。
    """
    real_ids = _query_real_registered_world_ids()
    world_ids = [w for w in dict.fromkeys(world_ids) if w and not _is_protected(w, real_ids)]
    if not world_ids:
        return {"deleted_worlds": [], "neo4j_nodes": 0}

    from sherpa import es_index
    from sherpa.ingest import world_neo4j

    total_nodes = 0
    try:
        env = world_neo4j._env()
        for w in world_ids:
            try:
                total_nodes += world_neo4j.delete_world(w, env["uri"], env["user"], env["pw"])
            except Exception:
                pass   # Neo4j 不到達等は best-effort（起動時 reconcile が backstop）
    except Exception:
        pass

    for w in world_ids:
        try:
            es_index.delete_world(w)
        except Exception:
            pass

    return {"deleted_worlds": world_ids, "neo4j_nodes": total_nodes}
