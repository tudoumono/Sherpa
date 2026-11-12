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

import logging
import os
import shutil
from pathlib import Path

from . import worlds

_log = logging.getLogger("sherpa")
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


def _derived_orphans(valid: set, derived_ok: bool, failed: list | None = None) -> list:
    """`data/derived/{world}` のうち valid に無い dir の world 名。`derived_ok` が False（派生先が原本と重なる）なら空。
    `valid_world` 名のみ対象（`.{world}.rebind-bak` 等は無視）。置き場を読めなければ `failed` に `derived` を足す。
    """
    parent = _derived_parent()
    out = []
    if not derived_ok:
        return out
    try:
        if not parent.is_dir():
            return out
        for d in parent.iterdir():
            if (d.is_dir() and not d.is_symlink() and worlds.valid_world(d.name)
                    and d.name not in valid):
                out.append(d.name)
    except OSError:
        _log.warning("派生物の置き場を読めませんでした: %s", parent, exc_info=True)
        if failed is not None:
            failed.append("derived")
    return sorted(out)


def _remove_derived(world: str) -> None:
    """孤児の派生MD dir を消す（無ければ何もしない・symlink と `valid_world` でない名前は消さない）。消せなければ例外。"""
    if not worlds.valid_world(world):
        return
    target = worlds.derived_dir(world)
    if target.is_symlink():
        return
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        pass


def _documents_orphans(valid: set, failed: list | None = None) -> list:
    """`documents`（文書台帳・PG）に残る、valid に無い world。一覧を読めなければ `failed` に `documents` を足して空を返す。"""
    from . import store
    try:
        present = store.list_document_worlds()
    except Exception:
        _log.warning("文書台帳の world 一覧を読めませんでした", exc_info=True)
        if failed is not None:
            failed.append("documents")
        return []
    return sorted(w for w in present if w not in valid)


def _stale_cleanup_keys() -> list:
    """止まって lock が外れた掃除の行の鍵。読めなければ空（次回に回す）。"""
    from .store import replace_rows
    try:
        return replace_rows.stale_cleanup_worlds()
    except Exception:
        _log.warning("止まった掃除の行を読めませんでした", exc_info=True)
        return []


def _reconcile_es(valid: set, failed: list) -> list:
    """孤児の ES 索引を、索引ごとに掃除の行（`kind='cleanup'`・鍵は `ES_CLEANUP_PREFIX` + エイリアス名）を持って消す。返り値＝消せたエイリアス名。
    止まった ES の掃除の行は、まだ孤児として見えている索引なら続きから消し、もう無い索引なら行だけ消す。
    """
    from . import es_index
    from .ingest import replace_guard
    from .store import replace_rows
    prefix = replace_rows.ES_CLEANUP_PREFIX
    deleted = []
    try:
        orphans = es_index.orphan_indices(valid)
    except Exception:
        _log.warning("ES の孤児の一覧を取れませんでした", exc_info=True)
        failed.append("es")
        return deleted
    stale = [k[len(prefix):] for k in _stale_cleanup_keys() if k.startswith(prefix)]
    for alias in sorted(set(orphans) | set(stale)):
        try:
            with replace_rows.begin_cleanup(prefix + alias) as row:
                if row is None:
                    continue   # 他が掃除中
                if alias in orphans:
                    es_index.delete_orphan(alias, row=row)
                replace_guard.drop_own(row)
            if alias in orphans:
                deleted.append(alias)
        except (Exception, replace_rows.ReplaceAborted):
            _log.warning("孤児の ES 索引を削除できませんでした（次回に続きから消します）: %s", alias, exc_info=True)
            failed.append(f"es:{alias}")
    return deleted


def cleanup_world(world: str, *, reflect: bool, derived: bool = True) -> dict | None:
    """登録の無い孤児 world 1 つの派生物（Neo4j・文書台帳・派生MD・ES）を、掃除の行（`kind='cleanup'`）を持って消す。
    他が掃除中なら何もせず None。止まって lock が外れた掃除の行は引き継いで続きから消す。全部消せたときだけ行を消す。
    戻り値は `{"done": 消せたストアの名前, "failed": 失敗}`。
    """
    from . import es_index, store
    from .ingest import replace_guard, wipe, world_neo4j
    from .store import replace_rows
    with replace_rows.begin_cleanup(world) as row:
        if row is None:
            return None
        steps = []
        if reflect:
            def _graph():
                env = world_neo4j._env()
                world_neo4j.delete_world(world, env["uri"], env["user"], env["pw"], row=row)
            steps.append(("neo4j", _graph))
        steps.append(("documents", lambda: store.replace_documents(world, [], row=row)))
        if derived:
            steps.append(("derived", lambda: _remove_derived(world)))
        steps.append(("es", lambda: wipe.delete_es_index(world, row=row)))
        done, failed = [], []
        for name, step in steps:
            try:
                step()
                done.append(name)
            except Exception:
                _log.warning("孤児の掃除の一段に失敗しました（次回に続きから消します）: %s world=%s", name, world, exc_info=True)
                failed.append(f"{name}:{world}")
        if not failed:
            replace_guard.drop_own(row)
    return {"done": done, "failed": failed}


def _reconcile_worlds(valid: set, source_roots: list, reflect: bool, failed: list) -> dict:
    """孤児の Neo4j・文書台帳・派生MD と、止まって lock が外れた掃除の行を、world ごとに `cleanup_world` で消す。返り値＝ストアごとの消せた world。"""
    from .ingest import world_neo4j
    from .store import replace_rows
    derived_ok = not any(_overlaps(_derived_parent(), sr) for sr in source_roots)   # 派生先が原本と重なる＝消さない
    listed: dict = {"neo4j": [], "documents": [], "derived": []}
    if reflect:
        try:
            env = world_neo4j._env()
            listed["neo4j"] = world_neo4j.orphan_worlds(valid, env["uri"], env["user"], env["pw"])
        except Exception:
            _log.warning("Neo4j の孤児の一覧を取れませんでした", exc_info=True)
            failed.append("neo4j")
    listed["documents"] = _documents_orphans(valid, failed)
    listed["derived"] = _derived_orphans(valid, derived_ok, failed)
    stale = [w for w in _stale_cleanup_keys() if w not in valid and not w.startswith(replace_rows.ES_CLEANUP_PREFIX)]
    out: dict = {"neo4j": [], "documents": [], "derived": []}
    for world in sorted({w for ws in listed.values() for w in ws} | set(stale)):
        try:
            res = cleanup_world(world, reflect=reflect, derived=derived_ok)
        except (Exception, replace_rows.ReplaceAborted):
            _log.warning("孤児の掃除を続けられませんでした（次回に続きから消します）: %s", world, exc_info=True)
            failed.append(f"cleanup:{world}")
            continue
        if res is None:
            continue   # 他が掃除中
        failed.extend(res["failed"])
        for name, ws in listed.items():
            if world in ws and name in res["done"]:
                out[name].append(world)
    return out


def reconcile_derivatives(reflect: bool = True) -> dict:
    """ES索引・Neo4j・派生MD・文書台帳(documents) の孤児を一括掃除する。不確実なら skip（何も消さない）。

    各ストアは best-effort。削除できなかったものは `failed`（`es`／`es:{索引}`／`neo4j`／`neo4j:{world}`／`derived`／`derived:{world}`／`documents`／`documents:{world}`／`cleanup:{world}`）に載せる（「対象なし」と区別する）。
    孤児は掃除の行（`kind='cleanup'`）を持って消し、止まって lock が外れた行は次の掃除が続きから消す。`SHERPA_TEST_DB_ISOLATED` が立っている間は全面 skip する（レジストリがテスト用 DB を指す一方、
    Neo4j/ES/`data/derived` は実環境と共有のため、実世界を孤児と誤判定して消す事故を防ぐ）。
    world 単体の削除（`wipe.wipe_locked`）はここを経由しない。
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
    failed: list = []
    out: dict = {"es": _reconcile_es(valid, failed), "neo4j": [], "derived": [], "documents": [], "failed": failed}
    out.update(_reconcile_worlds(valid, _source_roots(rows), reflect, failed))
    return out
