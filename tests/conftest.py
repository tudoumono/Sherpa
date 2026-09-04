"""pytest 共通設定（refactoring-plan フェーズ0／テスト用 DB 分離）。

- リポジトリルートと tests/ を sys.path に載せる（各テスト冒頭の `sys.path.insert`
  ボイラープレートを将来撤去できる土台。撤去自体はスライス2）。
- テストのパス（tests/unit/… 等）に応じて対応マーカーを自動付与し、ファイルを
  書き換えずに `-m unit` などの選別を可能にする。
- 共有ヘルパ `tests/_world_setup.py` の `ensure_v1` を session スコープ fixture として提供する。
- **テスト用 DB 分離**（docs/proposals/2026-07-03-テストDB分離.md）: モジュール import 時（どの
  テストファイルよりも先＝tests/api/conftest.py 等サブ conftest より前）に `SHERPA_PG_DSN` を
  専用 DB `sherpa_test` へ差し替える。dev DB（`sherpa`）にテストが直接書く事故（残骸蓄積の
  根本原因）を構造的に防ぐ。Neo4j/ES は community 版の制約で物理分離できないため、
  `tests/_world_registry.py` の登録簿方式（論理分離）でセッション終了時に一括削除する。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
for _p in (ROOT, TESTS):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from _ai_env_isolation import strip_ai_env_from_os_environ  # noqa: E402 (sys.path 準備の後で import する)

# 他のどのテストモジュールよりも先に AI 系 env を隔離する（`sherpa.llm.OPENAI_CHAT_URL` 等は
# import 時に一度だけ env を読む定数のため、個々のテストの monkeypatch では手遅れ＝
# `_ai_env_isolation.py` 参照）。
strip_ai_env_from_os_environ()


def _swap_dbname(dsn: str, dbname: str) -> str:
    """DSN（keyword=value 形式・URI 形式のどちらでも）の dbname だけ差し替える。

    psycopg 自身のパーサ（`conninfo_to_dict`/`make_conninfo`）を使い、DSN 形式の
    独自パースを避ける（DATABASE_URL は URI 形式・`store._dsn()` のフォールバックは
    keyword=value 形式＝両対応が必要）。
    """
    from psycopg import conninfo as _ci

    d = _ci.conninfo_to_dict(dsn)
    d["dbname"] = dbname
    return _ci.make_conninfo(**d)


def _setup_test_pg_dsn() -> None:
    """セッション最初期（他 fixture より先・conftest モジュール import 時）に
    `SHERPA_PG_DSN` を専用 DB `sherpa_test` へ差し替える（テスト用 DB 分離）。

    - `SHERPA_TEST_PG_DSN` が明示されていればそれをテスト DSN としてそのまま使う。
      無ければ元 DSN（`store._dsn()` と同じ解決順＝SHERPA_PG_DSN→DATABASE_URL→PG*）
      から dbname だけ `sherpa_test` に差し替えて構成する。
    - **安全ガード**: テスト DSN の dbname が元 DSN と同一なら `pytest.exit`
      （fail-closed。誤設定で再び dev DB に書く事故を構造的に防ぐ）。
    - 元 DSN の Postgres に接続して `CREATE DATABASE sherpa_test`（重複は握る＝冪等）。
      **接続不可（到達できない）となら何もしない**（`SHERPA_PG_DSN` は書き換えない＝既存の
      per-test `pytest.skip` に委ねる。Postgres 停止時の graceful SKIP を壊さない）。
      **接続はできたが CREATE DATABASE 自体が失敗**（権限不足等・`DuplicateDatabase` 以外）
      した場合は `pytest.exit`（fail-closed。2026-07-03 RV 対応 HIGH#1: 旧実装はこのケースも
      「何もしない」に丸めており、`SHERPA_PG_DSN` 未上書きのまま base DB へテストが書き続ける
      fail-open だった）。
    - `SHERPA_TEST_DB_ISOLATED=1` も同時に立てる。`sherpa.reconcile.reconcile_derivatives()`
      がこれを見て**全面 skip** する（2026-07-03 インシデント再発防止）: Postgres の world
      レジストリは `sherpa_test`（隔離済・別内容）を指すが Neo4j/ES/`data/derived` は実環境と
      **共有**のままのため、レジストリ基準の「孤児」判定が実世界（例: 実登録 `test`）を
      丸ごと孤児削除しうる（実際に発生済＝実 world `test` の Neo4j 76 ノードが消えた事故）。
    - `SHERPA_ORIG_PG_DSN` に元 DSN（base/実 DB）も残す。`tests/_world_registry.py` が
      base の worlds レジストリへ照会するための経路（2026-07-03 RV 対応 HIGH#2 の汎用ガード）。
    """
    import psycopg

    from sherpa import store   # store は最下層（他 sherpa.* 非 import）・import 時副作用なし

    orig_dsn = store._dsn()
    test_dsn = os.environ.get("SHERPA_TEST_PG_DSN") or _swap_dbname(orig_dsn, "sherpa_test")

    from psycopg import conninfo as _ci

    orig_dbname = _ci.conninfo_to_dict(orig_dsn).get("dbname")
    test_dbname = _ci.conninfo_to_dict(test_dsn).get("dbname")
    if not test_dbname or test_dbname == orig_dbname:
        pytest.exit(
            "テスト用 DB 分離の安全ガード: テスト DSN の dbname"
            f"（{test_dbname!r}）が元 DSN の dbname（{orig_dbname!r}）と同一です。"
            "SHERPA_TEST_PG_DSN の設定を確認してください"
            "（誤って dev DB を使う事故を防ぐため起動を拒否します）。"
        )

    try:
        conn = psycopg.connect(orig_dsn, autocommit=True, connect_timeout=5)
    except Exception:
        return   # Postgres 不到達（接続自体が不可）＝何もしない（既存の per-test SKIP に委ねる）

    try:
        try:
            conn.execute(f'CREATE DATABASE "{test_dbname}"')
        except psycopg.errors.DuplicateDatabase:
            pass                                      # 既存＝冪等（想定内）
        except Exception as e:
            # 接続はできた＝Postgres 到達可＝以後のテストも base DB に接続できてしまう。
            # ここで黙って return すると SHERPA_PG_DSN が未上書きのまま base DB に書き続ける
            # fail-open になるため、DuplicateDatabase 以外は起動拒否する（RV HIGH#1）。
            pytest.exit(
                f"テスト用 DB 分離: {test_dbname!r} への接続はできましたが作成に失敗しました"
                f"（{e.__class__.__name__}: {e}）。Postgres の権限等を確認してください"
                "（誤って dev DB を使い続ける事故を防ぐため起動を拒否します）。"
            )
    finally:
        conn.close()

    os.environ["SHERPA_PG_DSN"] = test_dsn
    os.environ["SHERPA_TEST_DB_ISOLATED"] = "1"   # reconcile_derivatives() の全面 skip 用（上記 docstring 参照）
    os.environ["SHERPA_ORIG_PG_DSN"] = orig_dsn   # _world_registry.py の実レジストリ照会用


_setup_test_pg_dsn()

# tests/ 直下のサブディレクトリ名＝マーカー名。
_MARKER_DIRS = {"unit", "api", "contract", "integration", "e2e", "e2e_live"}


def pytest_collection_modifyitems(config, items):
    """収集したテストに、所在ディレクトリ由来のマーカーを自動付与する。

    例: tests/unit/test_x.py::test_y → `unit` マーカー。ファイル側の変更なしで
    `-m unit` / `-m "unit or contract"` の選別を可能にする。
    """
    for item in items:
        try:
            rel = item.path.relative_to(TESTS)   # tests/ からの相対で先頭ディレクトリだけ見る
        except (ValueError, AttributeError):     # tests/ 外・path 無しは対象外
            continue
        top = rel.parts[0] if rel.parts else ""
        if top in _MARKER_DIRS:
            item.add_marker(getattr(pytest.mark, top))


@pytest.fixture(scope="session")
def ensure_v1():
    """v1 world を Neo4j にロードして返す（要 Neo4j・SHERPA_USE_FIXTURES=1 前提）。

    新規テスト用の土台。既存テストは各自 `_world_setup` を直 import しており、この
    fixture を使わなくてよい。fixture は遅延評価のため、要求するテストが無い限り
    `_world_setup` の import 副作用（設定スナップショット等）も走らない。
    """
    from _world_setup import ensure_v1 as _ensure_v1

    _ensure_v1()
    return _ensure_v1


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_worlds():
    """セッション終了時、テストが作成した Neo4j world / ES index をまとめて削除する
    （tests/_world_registry.py 参照。テスト用 DB 分離の Neo4j/ES 版＝論理分離のバックストップ。
    実 world 'test' は同モジュールの denylist で保護される）。
    """
    yield
    from _world_registry import cleanup_worlds, drain_registered_worlds

    world_ids = drain_registered_worlds()
    if not world_ids:
        return
    try:
        cleanup_worlds(world_ids)
    except Exception as e:  # pragma: no cover - Neo4j/ES 障害時のみ
        import warnings

        warnings.warn(f"test world cleanup failed for {world_ids}: {e}", stacklevel=2)
