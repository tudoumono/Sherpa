"""派生物の孤児を自動掃除する。

登録 world に属さない派生物（ES 索引・Neo4j の `world_id` パーティション・派生MD dir）を、取込/削除/起動の直後に消す。

fail-safe（破ると誤削除になる）:
- 登録レジストリ(PG)を直接当てて成功したときだけ実行する（取得不可なら何もしない。`worlds.list_worlds()` は使わない）。
- ローカル world の列挙が不確実（OSError）なら全削除を止める。
- 派生MD parent がソース root（登録 root_path / KB / fixtures）と重なる誤設定なら派生掃除をしない。
- 対象は Sherpa の派生物のみ（ES は `sherpa-kb-*` 接頭辞・派生MD は `valid_world` 名のみ）。
設計: docs/design/data.md「削除の伝播」
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import worlds

_warned_test_db_isolated = False  # プロセス内で一度だけ warning を出す


def _warn_test_db_isolated_once() -> None:
    """`SHERPA_TEST_DB_ISOLATED` による全面 skip をプロセス内で一度だけログに残す。"""
    global _warned_test_db_isolated
    if _warned_test_db_isolated:
        return
    _warned_test_db_isolated = True
    import logging
    logging.getLogger("sherpa").warning(
        "reconcile_derivatives(): SHERPA_TEST_DB_ISOLATED によりスキップしました"
        "（孤児派生物の自動掃除は無効・テスト用 DB 分離時の想定内動作）。")


def _local_world_ids() -> set:
    """fixtures/corpus・data/kb 直下の world id（filesystem 由来）。

    不在（FileNotFound/NotADirectory）だけスキップし、それ以外の OSError は伝播する（呼び出し側が「不確実」として全削除を止める）。
    """
    out: set = set()
    bases = []
    if worlds._fixtures():
        bases.append(Path("fixtures/corpus"))
    bases.append(worlds._kb())
    for base in bases:
        try:
            it = os.scandir(base)
        except (FileNotFoundError, NotADirectoryError):
            continue  # base 不在＝正常
        # 上記以外の OSError は伝播（不確実→全停止）
        with it:
            for e in it:
                if e.is_dir() and worlds.valid_world(e.name):
                    out.add(e.name)
    return out


def _source_roots(rows) -> list:
    """派生掃除の安全確認に使う「ソース root」群（登録 root_path ＋ KB ＋ fixtures）。"""
    roots = [Path(r["root_path"]) for r in rows if r.get("root_path")]
    roots.append(worlds._kb())
    if worlds._fixtures():
        roots.append(Path("fixtures/corpus"))
    return roots


def _overlaps(a: Path, b: Path) -> bool:
    """a と b が同一/包含関係なら True（解決不能は安全側で True）。"""
    try:
        a, b = a.resolve(), b.resolve()
    except OSError:
        return True
    return a == b or a in b.parents or b in a.parents


def _derived_parent() -> Path:
    # `worlds.derived_dir()` と同じ既定を使う（cwd 非依存）
    v = os.environ.get("SHERPA_DERIVED_DIR")
    return Path(v) if v else worlds._repo_root() / "data" / "derived"


def _reconcile_derived(valid: set, source_roots: list) -> list:
    """`data/derived/{world}` のうち valid に無い dir を削除する。返り値＝削除した world id。

    派生 parent がソース root と重なるなら何もしない。`valid_world` 名のみ対象（`.{world}.rebind-bak` 等は無視）。
    """
    parent = _derived_parent()
    if any(_overlaps(parent, sr) for sr in source_roots):  # 派生先が原本と重なる＝消さない
        return []
    deleted = []
    try:
        if not parent.is_dir():
            return []
        for d in parent.iterdir():
            if (d.is_dir() and not d.is_symlink() and worlds.valid_world(d.name)
                    and d.name not in valid):
                shutil.rmtree(d, ignore_errors=True)
                deleted.append(d.name)
    except OSError:
        pass
    return sorted(deleted)


def _reconcile_documents(valid: set) -> list:
    """`documents`（文書台帳・PG）のうち valid に無い world の行を削除する。

     best-effort（個別失敗は次回に再試行）。削除は `replace_documents(world, [])` を再利用する。返り値＝削除した world id。
    """
    from . import store
    deleted = []
    try:
        present = store.list_document_worlds()
    except Exception:
        return deleted
    for world in present:
        if world in valid:
            continue
        try:
            store.replace_documents(world, [])
            deleted.append(world)
        except Exception:
            continue  # 個別失敗は次回に再試行
    return sorted(deleted)


def reconcile_derivatives(reflect: bool = True) -> dict:
    """ES索引・Neo4j・派生MD・文書台帳(documents) の孤児を一括掃除する。不確実なら skip（何も消さない）。

    各ストアは best-effort。`SHERPA_TEST_DB_ISOLATED` が立っている間は全面 skip する（レジストリがテスト用 DB を指す一方、
    Neo4j/ES/`data/derived` は実環境と共有のため、実世界を孤児と誤判定して消す事故を防ぐ）。
    world 単体の削除（`worker.wipe_world`）はここを経由しない。
    """
    if os.environ.get("SHERPA_TEST_DB_ISOLATED"):
        _warn_test_db_isolated_once()
        return {"skipped": "test_db_isolated"}
    from . import store
    try:
        rows = list(store.list_worlds_db())  # レジストリ直当て（取れなければ全停止）
    except Exception:
        return {"skipped": "registry_unavailable"}
    try:
        local = _local_world_ids()
    except OSError:
        return {"skipped": "local_uncertain"}  # ローカル列挙が不確実なら全停止
    valid = {r["world_id"] for r in rows} | local
    out: dict = {"es": [], "neo4j": [], "derived": [], "documents": []}
    try:
        from . import es_index
        out["es"] = es_index.reconcile(valid)
    except Exception:
        pass
    out["derived"] = _reconcile_derived(valid, _source_roots(rows))
    try:
        out["documents"] = _reconcile_documents(valid)
    except Exception:
        pass
    if reflect:
        try:
            from .ingest import world_neo4j
            env = world_neo4j._env()
            out["neo4j"] = world_neo4j.reconcile(valid, env["uri"], env["user"], env["pw"])
        except Exception:
            pass
    return out
