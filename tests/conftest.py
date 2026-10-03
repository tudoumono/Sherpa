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

import hashlib
import os
import pathlib
import re
import subprocess
import sys
import time

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


# TEST-5（使い捨て per-run DB）: ひな型 DB 名・プロセス間の作り直し排他に使う advisory lock 鍵・
# 使い捨て DB 名のパターン（`sherpa_test_run_<作成時刻base36>_<pid>_<乱数6桁hex>`・先頭の
# base36 を stale 掃除の経過時間判定に使う）。鍵は固定文字列のハッシュ（`sherpa.store.db` の
# 各種 advisory lock と同型・別名前空間にするため DDL の `_SCHEMA_LOCK_KEY` とは別の種を使う）。
_TEMPLATE_DBNAME = "sherpa_test_template"
_TEMPLATE_LOCK_KEY = int.from_bytes(
    hashlib.sha1(b"sherpa_test_db_template_build").digest()[:8], "big", signed=True)
_EPHEMERAL_RE = re.compile(r"^sherpa_test_run_([0-9a-z]+)_(\d+)_[0-9a-f]{6}$")
# 2 時間より短くしない: 掃除は経過時間だけで判定するため、短くすると実行中の別の pytest の DB を消しうる。
_STALE_DB_MAX_AGE_HOURS = max(2.0, float(os.environ.get("SHERPA_TEST_DB_STALE_HOURS", "6")))

# pytest_sessionfinish で使い捨て DB を drop するための状態（`_setup_ephemeral_test_db` が
# 使い捨て経路を取った時だけ埋まる）。
_EPHEMERAL_DB_NAME: str | None = None
_EPHEMERAL_ORIG_DSN: str | None = None


def _setup_test_pg_dsn() -> None:
    """セッション最初期（他 fixture より先・conftest モジュール import 時）に
    `SHERPA_PG_DSN` をテスト専用 DB へ差し替える（テスト用 DB 分離・TEST-5＝既定は流すたびの
    使い捨て DB・docs/20-開発ハーネス.md §6）。

    経路は3つ（優先順）:
      1. `SHERPA_TEST_PG_DSN` が明示されていれば、それをそのままテスト DSN として使う
         （`scripts/gate-lane.sh` 等が既に `sherpa_test_<lane>` を用意して起動した場合＝
         二重に作らない。`_setup_fixed_test_db` へ委譲）。
      2. `SHERPA_TEST_DB_SHARED=1` なら、従来どおり共有 DB `sherpa_test` を使う（逃げ道・
         `_setup_fixed_test_db` へ委譲）。
      3. 既定（上記どちらでもない）: ひな型 DB `sherpa_test_template`（スキーマ適用済み・
         空データ）から `CREATE DATABASE ... TEMPLATE` で複製した使い捨て DB
         （`sherpa_test_run_<...>`）を使う（`_setup_ephemeral_test_db`）。セッション終了時
         （`pytest_sessionfinish`）に drop する。異常終了で残った古い使い捨て DB は次回起動時に
         掃除する（`_sweep_stale_ephemeral_dbs`）。

    どの経路でも、接続不可（Postgres 到達不能）なら**何もしない**（`SHERPA_PG_DSN` は書き換え
    ない＝既存の per-test `pytest.skip` に委ねる。停止時の graceful SKIP を壊さない）。
    """
    from sherpa import store   # store は最下層（他 sherpa.* 非 import）・import 時副作用なし

    orig_dsn = store._dsn()

    explicit_dsn = os.environ.get("SHERPA_TEST_PG_DSN")
    if explicit_dsn:
        _setup_fixed_test_db(orig_dsn, explicit_dsn)
        return
    if os.environ.get("SHERPA_TEST_DB_SHARED") == "1":
        _setup_fixed_test_db(orig_dsn, _swap_dbname(orig_dsn, "sherpa_test"))
        return
    _setup_ephemeral_test_db(orig_dsn)


def _setup_fixed_test_db(orig_dsn: str, test_dsn: str) -> None:
    """固定名のテスト DB（レーン DB／共有 `sherpa_test`）を使う経路（旧 `_setup_test_pg_dsn` の
    本体・挙動は無変更）。

    - **安全ガード**: テスト DSN の dbname が元 DSN と同一なら `pytest.exit`
      （fail-closed。誤設定で再び dev DB に書く事故を構造的に防ぐ）。
    - 元 DSN の Postgres に接続して `CREATE DATABASE`（重複は握る＝冪等）。
      **接続不可（到達できない）となら何もしない**（`SHERPA_PG_DSN` は書き換えない＝既存の
      per-test `pytest.skip` に委ねる。Postgres 停止時の graceful SKIP を壊さない）。
      **接続はできたが CREATE DATABASE 自体が失敗**（権限不足等・`DuplicateDatabase` 以外）
      した場合は `pytest.exit`（fail-closed。2026-07-03 RV 対応 HIGH#1: 旧実装はこのケースも
      「何もしない」に丸めており、`SHERPA_PG_DSN` 未上書きのまま base DB へテストが書き続ける
      fail-open だった）。
    - `SHERPA_TEST_DB_ISOLATED=1` も同時に立てる。`sherpa.reconcile.reconcile_derivatives()`
      がこれを見て**全面 skip** する（2026-07-03 インシデント再発防止）: Postgres の world
      レジストリはテスト DB（隔離済・別内容）を指すが Neo4j/ES/`data/derived` は実環境と
      **共有**のままのため、レジストリ基準の「孤児」判定が実世界（例: 実登録 `test`）を
      丸ごと孤児削除しうる（実際に発生済＝実 world `test` の Neo4j 76 ノードが消えた事故）。
    - `SHERPA_ORIG_PG_DSN` に元 DSN（base/実 DB）も残す。`tests/_world_registry.py` が
      base の worlds レジストリへ照会するための経路（2026-07-03 RV 対応 HIGH#2 の汎用ガード）。
    """
    import psycopg
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


def _ephemeral_db_name() -> str:
    """使い捨て DB 名を生成する（`sherpa_test_run_<作成時刻base36>_<pid>_<乱数6桁hex>`）。
    先頭の base36 は stale 掃除（`_sweep_stale_ephemeral_dbs`）が経過時間の判定に使う。
    """
    import random

    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    n = int(time.time())
    ts36 = "0" if n == 0 else ""
    while n:
        n, r = divmod(n, 36)
        ts36 = digits[r] + ts36
    rand = f"{random.SystemRandom().getrandbits(24):06x}"
    return f"sherpa_test_run_{ts36}_{os.getpid()}_{rand}"


def _template_is_current(template_dsn: str, expected_hash: str) -> bool:
    """ひな型 DB が存在し、かつ現在のコード側スキーマと一致するか（`schema_version` の最新行で判定）。"""
    import psycopg

    try:
        with psycopg.connect(template_dsn, autocommit=True, connect_timeout=5) as c:
            row = c.execute(
                "SELECT schema_hash FROM schema_version ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except Exception:
        return False
    return bool(row) and row[0] == expected_hash


def _run_init_schema_subprocess(dsn: str) -> None:
    """`sherpa.store.db.init_schema()` を別プロセスで実行し、ひな型 DB にスキーマを適用する。

    現在のプロセス内で `SHERPA_PG_DSN` を一時的に差し替えて直接呼ぶ方式は避ける——
    `init_schema()` はプロセス全体のグローバル状態（`store.db._inited`・バックグラウンド索引
    構築スレッドが `_dsn()` を遅延評価で読む）を書き換えるため、本体プロセスの env を
    その後すぐに使い捨て DB 側へ戻しても、その書き換えの影響がどちらの DB に対して効くかが
    タイミング依存になる。別プロセスなら、プロセス終了と同時にその状態も消える。
    """
    code = "from sherpa.store import db as store_db\nstore_db.init_schema()\n"
    env = dict(os.environ)
    env["SHERPA_PG_DSN"] = dsn
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        pytest.exit(
            "テスト用 DB 分離（使い捨て）: ひな型 DB へのスキーマ適用に失敗しました。\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def _ensure_template_db(conn, orig_dsn: str) -> str:
    """ひな型 DB（`sherpa_test_template`）が無い・スキーマが古ければ作り直し、常に最新の
    ひな型 DSN を返す。`conn` は `orig_dsn` への既存 autocommit 接続（呼び出し元が管理・
    ここでは閉じない）。**呼び出し元が advisory lock（`_TEMPLATE_LOCK_KEY`）を持った状態で呼ぶ**——
    鮮度の確認・作り直し・複製を同じロックの中で終えないと、スキーマの違う別の作業場所が間に作り直した
    ひな型を複製してしまう。
    """
    from sherpa.store import db as store_db

    template_dsn = _swap_dbname(orig_dsn, _TEMPLATE_DBNAME)
    if _template_is_current(template_dsn, store_db._SCHEMA_HASH):
        return template_dsn
    conn.execute(f'DROP DATABASE IF EXISTS "{_TEMPLATE_DBNAME}" WITH (FORCE)')
    conn.execute(f'CREATE DATABASE "{_TEMPLATE_DBNAME}"')
    _run_init_schema_subprocess(template_dsn)
    return template_dsn


def _sweep_stale_ephemeral_dbs(conn) -> None:
    """前回までの異常終了で残った使い捨て DB（`_STALE_DB_MAX_AGE_HOURS` 時間より古いもの）を
    掃除する（best-effort・失敗は無視＝他プロセスが同時に使用/掃除中の可能性がある）。
    """
    cutoff = time.time() - _STALE_DB_MAX_AGE_HOURS * 3600
    try:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'sherpa_test_run_%'"
        ).fetchall()
    except Exception:
        return
    for (name,) in rows:
        m = _EPHEMERAL_RE.match(name)
        if not m:
            continue
        try:
            created = int(m.group(1), 36)
        except ValueError:
            continue
        if created < cutoff:
            try:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except Exception:
                pass   # 他プロセスが使用中／同時に掃除中＝握り潰して続行


def _setup_ephemeral_test_db(orig_dsn: str) -> None:
    """既定経路（TEST-5）: ひな型 DB から複製した使い捨て DB を使う。

    Postgres 不達なら何もしない（`_setup_fixed_test_db` と同じ fail-open 方針）。ひな型/使い捨て
    DB の作成自体が失敗した場合（権限不足等）は `pytest.exit`（fail-closed・base DB へ書き続ける
    事故を防ぐ・`_setup_fixed_test_db` と同じ方針）。
    """
    global _EPHEMERAL_DB_NAME, _EPHEMERAL_ORIG_DSN
    import psycopg

    try:
        conn = psycopg.connect(orig_dsn, autocommit=True, connect_timeout=5)
    except Exception:
        return   # Postgres 不到達＝何もしない（既存の per-test SKIP に委ねる）

    try:
        _sweep_stale_ephemeral_dbs(conn)
        eph_name = _ephemeral_db_name()
        last_err: Exception | None = None
        conn.execute("SELECT pg_advisory_lock(%s)", (_TEMPLATE_LOCK_KEY,))
        try:
            _ensure_template_db(conn, orig_dsn)
            for attempt in range(5):
                try:
                    conn.execute(f'CREATE DATABASE "{eph_name}" TEMPLATE "{_TEMPLATE_DBNAME}"')
                    last_err = None
                    break
                except psycopg.errors.ObjectInUse as e:
                    # 直前の鮮度確認で開いた接続がサーバ側でまだ閉じ切っていない間だけ起きる
                    # （「ひな型に他の接続がある」）。それ以外の失敗はやり直さずに止める。
                    last_err = e
                    time.sleep(0.2 * (attempt + 1))
                except Exception as e:
                    last_err = e
                    break
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_TEMPLATE_LOCK_KEY,))
            except Exception:
                pass
        if last_err is not None:
            pytest.exit(
                f"テスト用 DB 分離（使い捨て）: {eph_name!r} の作成に失敗しました"
                f"（{last_err.__class__.__name__}: {last_err}）。Postgres の権限・"
                "ひな型 DB の状態を確認してください。"
            )
    finally:
        conn.close()

    test_dsn = _swap_dbname(orig_dsn, eph_name)
    os.environ["SHERPA_PG_DSN"] = test_dsn
    os.environ["SHERPA_TEST_DB_ISOLATED"] = "1"
    os.environ["SHERPA_ORIG_PG_DSN"] = orig_dsn
    _EPHEMERAL_DB_NAME = eph_name
    _EPHEMERAL_ORIG_DSN = orig_dsn


def pytest_sessionfinish(session, exitstatus):   # noqa: ARG001 (pytest hook シグネチャ固定)
    """使い捨て DB（`_setup_ephemeral_test_db` が作った場合のみ）をセッション終了時に drop する
    （best-effort。異常終了で drop できなかった分は次回起動時の `_sweep_stale_ephemeral_dbs` が
    拾う）。"""
    if not _EPHEMERAL_DB_NAME:
        return
    try:
        from sherpa.store import db as store_db
        store_db.close_pg_pool(timeout=5.0)   # DROP 前にプールの接続を閉じる（WITH (FORCE) が最終防御）
    except Exception:
        pass
    import psycopg

    try:
        with psycopg.connect(_EPHEMERAL_ORIG_DSN, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_EPHEMERAL_DB_NAME}" WITH (FORCE)')
    except Exception:
        pass   # best-effort（次回起動時の stale sweep が拾う）


_setup_test_pg_dsn()

import _det_provider  # noqa: E402 (DSN 確定後に sherpa を import する)

_det_provider.install()

# tests/ 直下のサブディレクトリ名＝マーカー名。
_MARKER_DIRS = {"unit", "api", "contract", "integration", "e2e"}


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


@pytest.fixture
def upstream_only_registry(monkeypatch):
    """`registry._ANALYZERS` を上流の固定既定リスト（`_UPSTREAM_ANALYZERS`）だけに据える
    （開発ハーネス S4・拡張の契約・敵対 RV 是正・docs/21-拡張の契約.md）。tests/unit・tests/contract
    の両方から使える最上位 conftest に置く（`test_mirror_contract.py` 等 tests/contract 側のテストも
    実 fixture コーパス＝`.md`/`.cbl` 等の分類固定値を前提にするため）。

    上流の単体テストは「上流の構成」を検証するもの——フォークが正規の拡張アナライザ
    （`<prefix>_*.py`）を `sherpa/ingest/analyzers/` 配下に登録しても（`discover_extension_
    analyzers()` が名前順で `_ANALYZERS` の末尾に足す）、上流構成の固定値を前提にしたテストが
    赤にならないよう、明示的に opt-in するテストモジュールへ適用する（module-level
    `pytestmark = [pytest.mark.usefixtures("upstream_only_registry")]`、または個別テストへの
    `@pytest.mark.usefixtures("upstream_only_registry")`）。

    `registered_extensions()`/`known_analyzers()`/`candidates()`/`resolve()`/`resolve_lazy()`/
    `config_signature()`（ひいては `corpus_docs.classify_document`・`world_graph.build_world`
    経由の doctype/branch/グラフ判定全般）はいずれも `_ANALYZERS` を都度参照する実装（キャッシュ
    なし）のため、これらは `_ANALYZERS` の差し替えだけで追随する。**ただし `sherpa.layer.CODE_EXT`
    は例外**（`from .doc_kinds import CODE_EXT` でモジュール import 時に1回だけ束縛される
    「実質キャッシュ」・`doc_kinds.CODE_EXT` 自体は `__getattr__` 経由の都度計算だが、`layer.py` 側の
    名前は import 時の値のまま固定される）——`_ANALYZERS` の差し替えだけでは `layer.layer_of`/
    `in_layer` の判定に反映されないため、本 fixture で明示的に上書きし直す。他の同型キャッシュ
    （`grep_tool._TEXT_EXT`・`scope._CONTENT_EXT`・`agentic_search._READABLE_EXT` 等）は上流拡張子の
    **上位互換の和集合**（`text_kind`/固定リストとの OR）を作るだけの事前フィルタで、最終判定は
    `classify_document`（ライブ）に委ねているため、フォークの拡張子が混ざっていても実害のある
    テスト失敗は今のところ確認されていない——新たに問題になるテストが出てきたら、同じ要領でこの
    フィクスチャに追加する。

    フォークの拡張が実際に読み込まれることを検証するテスト（`tests/unit/test_analyzer_
    extensions.py`・`tests/unit/analyzers/test_registry.py` の「上流順＋拡張の名前順」と
    「サンプル拡張が載る」）はこの fixture を使わない——実登録簿（`discover_extension_
    analyzers()` が実際に見つけたもの込み）を見る必要があるため。
    """
    from sherpa import layer as layer_mod
    from sherpa.ingest.analyzers import registry
    monkeypatch.setattr(registry, "_ANALYZERS", registry._UPSTREAM_ANALYZERS)
    upstream_ext = frozenset().union(*(a.extensions for a in registry._UPSTREAM_ANALYZERS))
    monkeypatch.setattr(layer_mod, "CODE_EXT", upstream_ext)
