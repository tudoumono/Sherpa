"""world レジストリ解決の単体テスト（鏡モデル・PG/Neo4j 不要）。

- list_worlds: レジストリは無条件に含む／data/kb・fixtures 直下の**未登録**候補は実ファイルが
  無ければ除外する（旧レイアウトの空ディレクトリが世界セレクタを汚染し既定選択を奪わない）。
- 既定ディレクトリの解決・MCP の world root override・外部解決の権限エラー・register 失敗時の補償。
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from sherpa import store, worlds


@pytest.fixture
def _isolated_kb(monkeypatch, tmp_path):
    """SHERPA_KB_DIR を空の一時ディレクトリに差し替え、DB レジストリ/fixtures も無効化する。"""
    d = tmp_path / "kb"
    d.mkdir()
    monkeypatch.setenv("SHERPA_KB_DIR", str(d))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    monkeypatch.setattr(store, "list_worlds_db", lambda: [])
    return d


@pytest.mark.parametrize("registered,dirs,files,expected", [
    # 実ファイルが無い未登録ディレクトリ（旧レイアウト残骸）は候補に出さず、最終 fallback の既定のみ
    ([], ["md/v1"], [], ["v1"]),
    # 未登録でも実ファイルがあれば headless world として候補に出す
    ([], ["headless"], ["headless/a.md"], ["headless"]),
    # 登録済みは中身が空でも無条件に含める（登録直後の world を隠さない）
    (["fresh"], ["fresh"], [], ["fresh"]),
    (["reg"], ["reg", "md/v1"], ["headless/a.txt"], ["headless", "reg"]),
])
def test_list_worlds(monkeypatch, _isolated_kb, registered, dirs, files, expected):
    d = _isolated_kb
    for rel in dirs:
        (d / rel).mkdir(parents=True, exist_ok=True)
    for rel in files:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("x", encoding="utf-8")
    monkeypatch.setattr(store, "list_worlds_db",
                        lambda: [{"world_id": w, "root_path": str(d / w)} for w in registered])
    assert worlds.list_worlds() == expected


# ---- _has_any_file（sort 無し early-exit・symlink を辿らない） ----

def test_has_any_file_empty_and_nested_and_missing(tmp_path):
    assert worlds._has_any_file(tmp_path) is False                     # 空ディレクトリ
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert worlds._has_any_file(tmp_path) is False                     # 空フォルダが深く入れ子でも False
    (tmp_path / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")
    assert worlds._has_any_file(tmp_path) is True                      # 深い階層のファイルも見つける
    assert worlds._has_any_file(tmp_path / "does-not-exist") is False  # 存在しないパスは例外にしない


def test_has_any_file_skips_symlinks(tmp_path):
    d = tmp_path / "d"
    outside = tmp_path / "outside"
    d.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    try:
        (d / "link.txt").symlink_to(outside / "secret.txt")      # symlink file
        (d / "linkdir").symlink_to(outside)                      # symlink dir
    except OSError as e:
        pytest.skip(f"symlink 非対応の環境: {e}")
    assert worlds._has_any_file(d) is False                      # symlink 経由のファイルは辿らない
    (d / "real.txt").write_text("r", encoding="utf-8")
    assert worlds._has_any_file(d) is True


def _deny_lstat(monkeypatch, target):
    """`target` への lstat だけ権限エラーにする（`Path.is_dir()` は OSError を握って False を返すため、
    lstat ベースの判定に置き換えて初めて伝播する）。"""
    orig_lstat = worlds.os.lstat

    def _boom(p, *a, **kw):
        if str(p) == str(target):
            raise PermissionError(13, "permission denied")
        return orig_lstat(p, *a, **kw)

    monkeypatch.setattr(worlds.os, "lstat", _boom)


def test_has_any_file_strict_propagates_permission_error_not_swallows(monkeypatch, tmp_path):
    """`strict=True` は ENOENT 以外の OSError を re-raise し、`strict=False`（既定）は黙って False。"""
    d = tmp_path / "world"
    d.mkdir()

    def _boom(p, *a, **kw):
        raise PermissionError(13, "permission denied")

    monkeypatch.setattr(worlds.os, "lstat", _boom)
    with pytest.raises(PermissionError):
        worlds._has_any_file(d, strict=True)
    assert worlds._has_any_file(d, strict=False) is False


def test_has_any_file_does_not_eagerly_materialize_scandir_entries(monkeypatch, tmp_path):
    """最初の1件でファイルが見つかったら即 return し、残りの entry を取得しない
    （`list(os.scandir(...))` の全件材料化＝巨大ディレクトリでの無制限メモリ消費への回帰を検知）。"""
    d = tmp_path / "world"
    d.mkdir()
    real_file = d / "f.txt"
    real_file.write_text("x", encoding="utf-8")

    class _Entry:
        path = str(real_file)

    class _FailOnSecondNext:
        def __init__(self):
            self._served = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            if not self._served:
                self._served = True
                return _Entry()
            raise AssertionError("2件目以降が取得された＝一括材料化に回帰している")

    def _fake_scandir(path):
        assert str(path) == str(d)
        return _FailOnSecondNext()

    monkeypatch.setattr(worlds.os, "scandir", _fake_scandir)
    assert worlds._has_any_file(d) is True


def test_discover_fs_world_ids_strict_skips_enoent_base(monkeypatch, tmp_path):
    """base 自体が存在しない（ENOENT）場合は例外にせず候補から外す。"""
    monkeypatch.setenv("SHERPA_KB_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    assert worlds.discover_fs_world_ids_strict() == []


@pytest.mark.parametrize("case", ["discover_base", "registered_root", "unregistered_candidate"])
def test_permission_error_on_stat_is_external_resolver_error_not_swallowed(monkeypatch, tmp_path, case):
    """権限エラーで stat できない base／登録済み root／未登録候補は、候補から静かに落とさず・
    not_found（404 相当）に潰さず `ExternalResolverError` にする。"""
    monkeypatch.setenv("SHERPA_KB_DIR", str(tmp_path))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    if case == "discover_base":
        _deny_lstat(monkeypatch, tmp_path)
        call = worlds.discover_fs_world_ids_strict
    elif case == "registered_root":
        root = tmp_path / "registered-root"
        root.mkdir()
        _deny_lstat(monkeypatch, root)
        row = {"world_id": "regworld", "root_path": str(root), "storage_mode": "external_reference"}

        def call():
            return worlds.resolve_external_world("regworld", registry_row=row)
    else:
        _deny_lstat(monkeypatch, tmp_path / "someworld")

        def call():
            return worlds.resolve_external_world("someworld", registry_row=None)
    with pytest.raises(worlds.ExternalResolverError):
        call()


# ---- 既定ディレクトリは cwd に依らずリポジトリ基準（MCP サブプロセス cwd=authoring 対策） ----
# 相対既定値が cwd 基準に誤解決され、派生MD（Office 文書の本文）が見つからず Office 文書が
# 台帳から丸ごと脱落していた（source/.txt 等は wd 直下を直接見るため影響を受けない非対称な症状）。

@pytest.fixture
def _chdir(tmp_path):
    """別ディレクトリへ chdir し、終了後に元へ戻す（authoring 相当の別 cwd を模す）。"""
    old = Path.cwd()
    other = tmp_path / "authoring-like"
    other.mkdir()
    os.chdir(other)
    try:
        yield other
    finally:
        os.chdir(old)


def test_kb_and_derived_dir_default_resolve_to_repo_root_regardless_of_cwd(monkeypatch, _chdir):
    monkeypatch.delenv("SHERPA_KB_DIR", raising=False)
    monkeypatch.delenv("SHERPA_DERIVED_DIR", raising=False)
    repo_root = Path(__file__).resolve().parents[2]
    assert worlds._kb() == repo_root / "data" / "kb"
    assert worlds.derived_dir("v1") == repo_root / "data" / "derived" / "v1"
    assert worlds._kb().is_absolute() and worlds.derived_dir("v1").is_absolute()


def test_kb_explicit_relative_value_still_resolves_against_cwd(monkeypatch, _chdir):
    """既定と違い、明示的に指定した相対値は従来どおり cwd 基準のまま。"""
    monkeypatch.setenv("SHERPA_KB_DIR", "relkb")
    monkeypatch.setenv("SHERPA_DERIVED_DIR", "relderived")
    assert worlds._kb() == Path("relkb")
    assert worlds.derived_dir("v1") == Path("relderived") / "v1"


def test_world_dir_mcp_world_root_override_bypasses_registry_for_matching_world_only(monkeypatch, tmp_path):
    """SHERPA_MCP_WORLD_ROOT は SHERPA_MCP_WORLD と一致する world_id にだけ効き、registry 解決を
    経由せず絶対パスをそのまま使う。`world_dir()` は `store.get_world()` の例外を自前で握るため、
    「呼ばれなかった」は例外でなく呼び出し記録で検証する。"""
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    calls: list = []

    def _track(world_id):
        calls.append(world_id)
        return None   # registry には無い体（未登録）
    monkeypatch.setattr(store, "get_world", _track)

    monkeypatch.setenv("SHERPA_MCP_WORLD", "target")
    monkeypatch.setenv("SHERPA_MCP_WORLD_ROOT", str(real_root))
    assert worlds.world_dir("target") == real_root
    assert calls == [], f"override が効くはずなのに registry へ問い合わせが発生: {calls}"

    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    monkeypatch.setenv("SHERPA_KB_DIR", str(tmp_path / "no-such-kb-dir"))   # フォールバックも None になるよう封じる
    assert worlds.world_dir("other") is None
    assert calls == ["other"], "他 world では override は効かず通常どおり registry に問い合わせるはず"


def test_world_dir_mcp_world_root_override_falls_back_when_invalid(monkeypatch):
    """override が壊れている（相対/不在/symlink）場合は無視して通常解決へフォールバックする。"""
    monkeypatch.setattr(store, "get_world", lambda world_id: None)
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    monkeypatch.setenv("SHERPA_MCP_WORLD", "target")
    monkeypatch.setenv("SHERPA_MCP_WORLD_ROOT", "relative/not/absolute")   # 絶対パスでない＝無効
    monkeypatch.setenv("SHERPA_KB_DIR", "/no/such/kb/dir")
    assert worlds.world_dir("target") is None


@pytest.fixture
def _fake_repo_root(monkeypatch, tmp_path):
    """`worlds._repo_root()` を tmp_path 配下へ差し替え、`derived_dir()` の既定が実体の
    `data/derived/{world}` を指して `rmtree` で実データを壊す事故を防ぐ。"""
    fake = tmp_path / "fake-repo-root"
    fake.mkdir()
    monkeypatch.setattr(worlds, "_repo_root", lambda: fake)
    return fake


@pytest.fixture
def _office_world(monkeypatch, tmp_path, _fake_repo_root):
    """note.txt（ソース）＋ report.xlsx（Office・派生MD あり）を持つ world `testworld` を registry に載せる。"""
    world_root = tmp_path / "world-root"
    world_root.mkdir()
    (world_root / "note.txt").write_text("ソース文書", encoding="utf-8")
    (world_root / "report.xlsx").write_bytes(b"")   # 中身は問わない（変換は別工程・存在と拡張子のみ判定）
    monkeypatch.setattr(store, "get_world",
                        lambda world_id: {"root_path": str(world_root)} if world_id == "testworld" else None)
    monkeypatch.delenv("SHERPA_DERIVED_DIR", raising=False)
    monkeypatch.delenv("SHERPA_KB_DIR", raising=False)
    md_dir = worlds.derived_dir("testworld") / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    (md_dir / "report.xlsx.md").write_text("## 概要\nダミーの変換済み本文\n", encoding="utf-8")
    return world_root


def test_mcp_env_output_restores_office_docs_from_authoring_like_cwd(monkeypatch, tmp_path, _office_world):
    """(a) `mcp._mcp_env()` が計算した env（絶対パス化＋SHERPA_MCP_WORLD_ROOT）を適用すると
    別 cwd でも Office 込みで件数が一致し、(b) 素の相対値を素通しすると Office 文書が脱落する。"""
    from sherpa import corpus_docs
    from sherpa.providers.codex import mcp

    old_cwd = Path.cwd()
    authoring_like = tmp_path / "authoring-like"
    authoring_like.mkdir()
    try:
        mcp_env = mcp._mcp_env("testworld", None)
        assert Path(mcp_env["SHERPA_DERIVED_DIR"]).is_absolute(), "SHERPA_DERIVED_DIR が絶対化されていない"
        assert Path(mcp_env["SHERPA_KB_DIR"]).is_absolute(), "SHERPA_KB_DIR が絶対化されていない"
        for k, v in mcp_env.items():
            monkeypatch.setenv(k, v)
        os.chdir(authoring_like)
        docs_after_fix = corpus_docs.world_documents("testworld")
        assert {d["name"] for d in docs_after_fix} == {"note.txt", "report.xlsx"}

        os.chdir(old_cwd)
        monkeypatch.setenv("SHERPA_DERIVED_DIR", "data/derived")   # 絶対化しない・素の相対既定
        monkeypatch.delenv("SHERPA_MCP_WORLD_ROOT", raising=False)
        os.chdir(authoring_like)
        docs_before_fix = corpus_docs.world_documents("testworld")
        assert [d["name"] for d in docs_before_fix] == ["note.txt"], \
            "相対 SHERPA_DERIVED_DIR を素通しすると別 cwd で Office 文書が脱落するはず（修正前の再現）"
    finally:
        os.chdir(old_cwd)


def test_mcp_env_world_root_survives_registry_outage_for_world_documents(monkeypatch, _office_world):
    """`_mcp_env()` が SHERPA_MCP_WORLD_ROOT を出し、その override があれば registry 完全不達
    （MCP サブプロセスのサンドボックスでネットワーク遮断）でも件数が変わらない。override を消すと
    0 件になる対照も固定する。"""
    from sherpa import corpus_docs
    from sherpa.providers.codex import mcp

    mcp_env = mcp._mcp_env("testworld", None)
    assert mcp_env.get("SHERPA_MCP_WORLD_ROOT") == str(_office_world.resolve())
    baseline_count = len(corpus_docs.world_documents("testworld"))
    assert baseline_count == 2

    def _unreachable(world_id):
        raise RuntimeError("simulated: Postgres unreachable (sandboxed network)")
    monkeypatch.setattr(store, "get_world", _unreachable)
    for k, v in mcp_env.items():
        monkeypatch.setenv(k, v)
    assert len(corpus_docs.world_documents("testworld")) == baseline_count

    monkeypatch.delenv("SHERPA_MCP_WORLD_ROOT", raising=False)
    assert len(corpus_docs.world_documents("testworld")) == 0, \
        "override が無ければ registry 不達時に world_dir が None になり 0 件になるはず（対照）"


# ---- resolve_external_world の registry 引き ----

def test_resolve_external_world_forwards_connect_and_statement_timeout_to_store_get_world(monkeypatch):
    """`registry_row` 省略時は `connect_timeout`/`statement_timeout_ms` を `store.get_world()` へ
    そのまま転送する（外部 API のリクエスト全体デッドラインが registry 読み取りを無期限にブロックさせない）。"""
    captured = {}

    def _fake_get_world(world_id, *, connect_timeout=None, statement_timeout_ms=None):
        captured["connect_timeout"] = connect_timeout
        captured["statement_timeout_ms"] = statement_timeout_ms
        return None   # 未登録扱い（実 DB 到達は不要）

    monkeypatch.setattr(store, "get_world", _fake_get_world)
    worlds.resolve_external_world("no-such-world-for-timeout-test", connect_timeout=2.5,
                                  statement_timeout_ms=1500)
    assert captured == {"connect_timeout": 2.5, "statement_timeout_ms": 1500}


def test_resolve_external_world_registry_row_given_skips_store_get_world_entirely(monkeypatch):
    """`registry_row` を渡したら `store.get_world()` を一切呼ばない（N+1 回避）。"""
    def _boom(*a, **kw):
        raise AssertionError("registry_row 指定時は store.get_world を呼んではいけない")

    monkeypatch.setattr(store, "get_world", _boom)
    res = worlds.resolve_external_world("whatever-world-id", registry_row=None, connect_timeout=1.0)
    assert res.status == "not_found"


# ---- register 失敗時の補償（registry 行・derived_dir・Neo4j・台帳・ES の孤児を残さない） ----

@pytest.fixture
def _register_stubs(monkeypatch):
    """`worlds.register` の外部境界（ロック・DB・Neo4j・ES）を無害化する。個別テストが上書きする。"""
    from sherpa import es_index
    from sherpa.ingest import world_neo4j

    @contextlib.contextmanager
    def _noop_lock(*a, **kw):
        yield
    monkeypatch.setattr(store, "world_registry_lock", _noop_lock)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(store, "list_worlds_db", lambda: [])
    monkeypatch.setattr(store, "get_world", lambda w: None)
    monkeypatch.setattr(store, "world_by_root", lambda root: None)
    monkeypatch.setattr(store, "upsert_world", lambda *a, **kw: None)
    monkeypatch.setattr(store, "replace_documents", lambda w, rows: 0)
    monkeypatch.setattr(store, "delete_world_row", lambda w: None)
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    monkeypatch.setattr(world_neo4j, "delete_world", lambda w, uri, user, pw: 0)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)


def _fail_run_locked(monkeypatch, message):
    from sherpa.ingest import worker

    def _boom(*a, **kw):
        raise RuntimeError(message)
    monkeypatch.setattr(worker, "_run_locked", _boom)


def test_register_cleans_up_registry_row_when_run_locked_raises(monkeypatch, tmp_path, _register_stubs):
    """`_run_locked` が（status=="failed" でなく）例外を bare raise した場合も registry 行・
    derived_dir 残骸を残さない（PG/Neo4j 接続断等の途中失敗で registry 行だけ残る孤児を防ぐ）。"""
    monkeypatch.setattr(worlds, "derived_dir", lambda w: tmp_path / "derived_root")
    delete_calls = []
    monkeypatch.setattr(store, "delete_world_row", lambda w: delete_calls.append(w))
    _fail_run_locked(monkeypatch, "simulated PG/Neo4j failure mid-run")

    with pytest.raises(RuntimeError, match="simulated PG/Neo4j failure mid-run"):
        worlds.register("wtest", str(tmp_path))

    assert delete_calls == ["wtest"], "registry 行が孤児として残らない"


def test_register_cleanup_runs_rmtree_even_when_delete_world_row_raises(monkeypatch, tmp_path, _register_stubs):
    """cleanup の `delete_world_row` 自体が失敗しても derived_dir の rmtree は独立に実行され、
    元の worker 例外が握り潰されない（cleanup 側の例外はログのみ）。"""
    der = tmp_path / "derived_root"
    der.mkdir()
    (der / "leftover.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(worlds, "derived_dir", lambda w: der)

    def _boom_delete(w):
        raise RuntimeError("simulated PG failure during cleanup delete_world_row")
    monkeypatch.setattr(store, "delete_world_row", _boom_delete)
    _fail_run_locked(monkeypatch, "original worker failure")

    with pytest.raises(RuntimeError, match="original worker failure"):
        worlds.register("wtest", str(tmp_path))

    assert not der.exists(), "delete_world_row が失敗しても derived_dir の rmtree は実行される"


def test_register_cleanup_rmtree_ignores_missing_derived_dir_without_warning(
        monkeypatch, tmp_path, caplog, _register_stubs):
    """派生ディレクトリが作られる前に失敗した register は、「削除すべきものが無かっただけ」の
    正常系として warning を残さない（権限等の実際の削除失敗だけを警告する）。"""
    monkeypatch.setattr(worlds, "derived_dir", lambda w: tmp_path / "derived_root")   # mkdir しない
    _fail_run_locked(monkeypatch, "simulated failure before derived dir creation")

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError, match="simulated failure before derived dir creation"):
            worlds.register("wtest", str(tmp_path))

    assert "派生ディレクトリ削除でエラー" not in caplog.text
    assert "派生ディレクトリ削除に失敗しました" not in caplog.text


def test_register_cleanup_compensates_neo4j_documents_es_when_replace_documents_fails(
        monkeypatch, tmp_path, _register_stubs):
    """`_run_locked` は Neo4j load を先に commit してから台帳を更新する。台帳更新が失敗すると
    registry 行は無いのに Neo4j にはグラフが載ったままの孤児になりうる——cleanup は `DELETE /worlds`
    と同じ削除伝播（Neo4j 削除・台帳クリア・ES 削除）を補償的に実行し、元の worker 例外を書き換えない。
    `_run_locked` 自体はモックせず、Neo4j load→台帳更新の順で進めて台帳更新だけを失敗させる。"""
    from sherpa import es_index
    from sherpa.ingest import worker, world_neo4j

    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "a.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: tmp_path / "derived_md")
    monkeypatch.setattr(worlds, "derived_dir", lambda w: tmp_path / "derived_root")
    monkeypatch.setattr(store, "set_world_sig", lambda *a, **kw: None)
    monkeypatch.setattr(store, "downgrade_orphaned_extracting_runs", lambda world=None: [])
    monkeypatch.setattr(store, "update_ingest_run_progress", lambda run_id, progress: None)
    monkeypatch.setattr(store, "start_ingest_run", lambda w, **kw: {"id": 1, "version": w, "status": "extracting"})
    monkeypatch.setattr(store, "finish_ingest_run", lambda run_id, **kw: {"id": run_id, **kw})
    monkeypatch.setattr(worker, "build_world_graph", lambda w: ([], [], []))
    monkeypatch.setattr(worker, "_build_derived",
                        lambda w, **kw: {"converted": 0, "failed": 0, "unsupported": 0, "by_ext": {}})

    neo4j_load_calls = []
    monkeypatch.setattr(world_neo4j, "load_world",
                        lambda nodes, edges, w, uri, user, pw, plugin_failures=None: neo4j_load_calls.append(w) or (0, 0))
    neo4j_delete_calls = []
    monkeypatch.setattr(world_neo4j, "delete_world",
                        lambda w, uri, user, pw: neo4j_delete_calls.append(w))

    replace_calls = []

    def _fake_replace(w, rows):
        replace_calls.append((w, list(rows)))
        if rows:   # `_run_locked` 自身の呼び出し（台帳を書こうとする）だけ失敗させる
            raise RuntimeError("original worker failure: pg_replace")
        return 0   # cleanup の補償クリア（空リスト）は成功させる
    monkeypatch.setattr(store, "replace_documents", _fake_replace)

    es_delete_calls = []
    monkeypatch.setattr(es_index, "delete_world", lambda w: es_delete_calls.append(w))
    delete_row_calls = []
    monkeypatch.setattr(store, "delete_world_row", lambda w: delete_row_calls.append(w))

    with pytest.raises(RuntimeError, match="original worker failure: pg_replace"):
        worlds.register("wtest", str(tmp_path))

    assert neo4j_load_calls == ["wtest"], "前提: Neo4j load が実行されてから台帳更新が失敗した"
    assert neo4j_delete_calls == ["wtest"], "Neo4j へ commit 済みのグラフを補償削除する"
    assert replace_calls[-1] == ("wtest", []), "台帳を空へ補償クリアする"
    assert es_delete_calls == ["wtest"], "ES 索引も補償削除する"
    assert delete_row_calls == ["wtest"], "registry 行も最後に削除する"


def test_rag_md_path_confines_and_rejects_symlinks(monkeypatch, tmp_path):
    """`rag_md_path` は実在する通常の rag.md だけを返し、不正な doc_id・秘匿名・経路上の symlink は None。"""
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    rag = worlds.derived_rag_dir("w")
    (rag / "a").mkdir(parents=True)
    (rag / "a" / "x.xlsx.rag.md").write_text("本文", encoding="utf-8")
    (rag / "secret.pem.rag.md").write_text("鍵", encoding="utf-8")
    (rag / "link.xlsx.rag.md").symlink_to(rag / "a" / "x.xlsx.rag.md")
    (rag / "dirlink").symlink_to(rag / "a", target_is_directory=True)

    assert worlds.rag_md_path("w", "a/x.xlsx") == (rag / "a" / "x.xlsx.rag.md").resolve()
    for bad in ("", "/a/x.xlsx", "a\\x.xlsx", "../a/x.xlsx", "a//x.xlsx", "a/none.xlsx", "secret.pem",
                "link.xlsx", "dirlink/x.xlsx"):
        assert worlds.rag_md_path("w", bad) is None, bad
    assert worlds.rag_md_path("w", None) is None
