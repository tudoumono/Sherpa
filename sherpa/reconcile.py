"""派生物の孤児を自動掃除（ユーザ不可視の自己修復）。

参照先変更(rebind)・world 削除・索引/グラフの命名/キー変更などで、**登録 world に属さない派生物**
（ES 索引・Neo4j の `world_id` パーティション・派生MD dir）が取り残されうる。これを取込/削除/起動の
直後に勝手に消す＝管理ボタンも「削除がエラーで止まる」も無し（ユーザは意識しない）。

**fail-safe（最重要）**:
- 登録レジストリ(PG)を **直接** 当てて成功した時だけ実行（取得不可なら何もしない）。`worlds.list_worlds()` は
  DB エラーを握りつぶして fixtures に落ちるので**使わない**＝登録 world を孤児と誤判定して全消しする事故を防ぐ。
- ローカル world（fixtures/data-kb）の列挙が**不確実(OSError)なら全削除を止める**（valid を部分集合にしない）。
- 派生MD parent が**ソース root（登録 root_path / KB / fixtures）と重なる誤設定**なら派生掃除を**やらない**
  （原本フォルダを消さない・office_md/graph_extract の overlap guard と同じ思想）。
- 対象は Sherpa の派生物のみ（ES は `sherpa-kb-*` 接頭辞・派生MD は `valid_world` 名のみ＝`.`始まりの退避dir等は無視）。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import worlds

_warned_test_db_isolated = False   # 2026-07-03 RV 対応 MEDIUM: プロセス内で一度だけ warning を出す


def _warn_test_db_isolated_once() -> None:
    """`SHERPA_TEST_DB_ISOLATED` による全面 skip をプロセス内で一度だけログに残す（無警告無効化の防止）。

    `reconcile_derivatives()` はテストセッション中に何十回も呼ばれうるため、毎回 warning すると
    ログが埋もれる。起動時ガード（`sherpa.api._warn_test_db_isolated`）が本番誤設定の主防波堤、
    こちらは「テスト実行中も skip が起きていることが追跡可能」であることの補助。
    """
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

    **不在(FileNotFound/NotADirectory)だけスキップ、それ以外の OSError は伝播**＝呼出側で「不確実」として
    全削除を止める（`Path.is_dir()` は OSError を握りつぶして False を返すので使わず、`os.scandir`/`DirEntry.is_dir`
    でエラーを表面化させる＝部分 valid で誤爆しない・RV High）。
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
            continue                                     # base 不在＝正常（その base に world 無し）
        # ↑以外の OSError（Permission/IO 等）は伝播＝不確実→全停止
        with it:
            for e in it:
                if e.is_dir() and worlds.valid_world(e.name):   # DirEntry.is_dir は実 I/O 失敗時 OSError を投げる＝伝播
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
    """a と b が同一/包含関係なら True（解決不能は安全側で True＝重なり扱い）。"""
    try:
        a, b = a.resolve(), b.resolve()
    except OSError:
        return True
    return a == b or a in b.parents or b in a.parents


def _derived_parent() -> Path:
    # RV HIGH（2026-07-03）: worlds.derived_dir() と同じ既定を使う（cwd 非依存・リポジトリ基準）。
    # 既存の `worlds._kb()`/`worlds._fixtures()` 直接参照と同じ流儀でモジュール private を再利用する
    # （reconcile.py は worlds.py に依存済み・DRY・二重管理を避ける）。
    v = os.environ.get("SHERPA_DERIVED_DIR")
    return Path(v) if v else worlds._repo_root() / "data" / "derived"


def _reconcile_derived(valid: set, source_roots: list) -> list:
    """`data/derived/{world}` のうち valid に無い dir を削除。返り値＝削除した world id。

    **派生 parent がソース root と重なるなら何もしない**（誤設定で原本/登録フォルダを消さない・BLOCKER 対策）。
    `valid_world` 名のみ対象＝`.{world}.rebind-bak` 等の退避/隠しは自動で無視。
    """
    parent = _derived_parent()
    if any(_overlaps(parent, sr) for sr in source_roots):        # 派生先が原本と重なる＝fail-closed（消さない）
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
    """`documents`（文書台帳・PG）のうち valid に無い world の行を削除する（バッチ2・3番・2026-07-03）。

    ES/Neo4j/派生MD と同じく best-effort・失敗は個別に無視（次回リコンサイルで再試行）。
    削除は既存の `replace_documents(world, [])` をそのまま再利用する（世界削除の正規経路と同じ
    ロジック・削除ロジックの二重管理を避ける）。返り値＝削除した world id。
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
            continue                                              # 個別失敗は次回リコンサイルで再試行
    return sorted(deleted)


def reconcile_derivatives(reflect: bool = True) -> dict:
    """ES索引・Neo4j・派生MD・文書台帳(documents) の孤児を一括掃除（不可視の自己修復）。
    **不確実なら skip**（何も消さない）。

    各ストアは best-effort（一つの失敗で他を止めない・個別失敗は次回リコンサイルで再試行）。

    **fail-safe③**（2026-07-03 インシデント対応・テスト用 DB 分離）: `SHERPA_TEST_DB_ISOLATED`
    （`tests/conftest.py` がテスト用 Postgres へ切替時に設定）が立っている時は**全面 skip**。
    Postgres の world レジストリは `sherpa_test`（テスト隔離用・空/別内容）を指しているが、
    Neo4j／ES／`data/derived` は**実環境と共有**（community 版制約で分離不可）のため、この状態で
    レジストリを基に「孤児」を判定すると実世界（例: 実登録 `test`）を丸ごと削除してしまう
    （実際に発生: 実 world `test` の Neo4j 76 ノードが誤って孤児削除された）。world 単体の
    削除（`worker.wipe_world`）は world_id を直接指定するスコープ削除でここを経由しないため、
    本 skip は削除機能そのものには影響しない（孤児掃除という補助機能だけを止める）。
    """
    if os.environ.get("SHERPA_TEST_DB_ISOLATED"):
        _warn_test_db_isolated_once()
        return {"skipped": "test_db_isolated"}
    from . import store
    try:
        rows = list(store.list_worlds_db())                      # レジストリ直当て＝取れなければ全停止（fail-safe）
    except Exception:
        return {"skipped": "registry_unavailable"}
    try:
        local = _local_world_ids()
    except OSError:
        return {"skipped": "local_uncertain"}                    # ローカル列挙が不確実＝valid 部分集合を避け全停止
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
