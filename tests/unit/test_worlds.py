"""world レジストリ解決の単体テスト（鏡モデル・PG/Neo4j 不要）。

- list_worlds: レジストリは無条件に含む／data/kb・fixtures 直下の**未登録**候補は実ファイルが
  無ければ除外する（S2・2026-07-03: 旧レイアウトの空ディレクトリが世界セレクタを汚染し、
  アルファベット順で先頭に来て既定選択を奪う不具合の再発防止）。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from sherpa import store, worlds


@pytest.fixture
def _isolated_kb(monkeypatch):
    """SHERPA_KB_DIR を空の一時ディレクトリに差し替え、DB レジストリ/fixtures も無効化する。"""
    d = Path(tempfile.mkdtemp())
    monkeypatch.setenv("SHERPA_KB_DIR", str(d))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    monkeypatch.setattr(store, "list_worlds_db", lambda: [])   # DB 不問（レジストリ空を模擬）
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_list_worlds_excludes_empty_unregistered_kb_dirs(_isolated_kb):
    """実ファイルが1つも無い data/kb 直下の未登録ディレクトリ（旧レイアウト残骸）は候補に出さない。"""
    d = _isolated_kb
    (d / "md").mkdir()
    (d / "md" / "v1").mkdir()                        # 空（ファイル無し）＝旧レイアウトの残骸を模擬
    assert worlds.list_worlds() == ["v1"]             # 空候補は無く、最終 fallback の既定のみ


def test_list_worlds_includes_unregistered_kb_dir_with_real_content(_isolated_kb):
    """未登録でも実ファイルがあれば headless world として候補に出す（既存の documented 挙動は維持）。"""
    d = _isolated_kb
    (d / "headless").mkdir()
    (d / "headless" / "a.md").write_text("x", encoding="utf-8")
    assert worlds.list_worlds() == ["headless"]


def test_list_worlds_registered_world_included_even_if_empty(monkeypatch, _isolated_kb):
    """レジストリ登録済みは中身が空でも無条件に含める（登録直後の world を隠さない）。"""
    d = _isolated_kb
    monkeypatch.setattr(store, "list_worlds_db",
                        lambda: [{"world_id": "fresh", "root_path": str(d / "fresh")}])
    (d / "fresh").mkdir()                             # 参照先はあるが中身は空
    assert worlds.list_worlds() == ["fresh"]


def test_list_worlds_mixed_registered_and_content_bearing_unregistered(monkeypatch, _isolated_kb):
    d = _isolated_kb
    monkeypatch.setattr(store, "list_worlds_db",
                        lambda: [{"world_id": "reg", "root_path": str(d / "reg")}])
    (d / "reg").mkdir()
    (d / "md").mkdir(); (d / "md" / "v1").mkdir()     # 空の未登録残骸＝除外される
    (d / "headless").mkdir(); (d / "headless" / "a.txt").write_text("x", encoding="utf-8")
    assert worlds.list_worlds() == ["headless", "reg"]


# ---- _has_any_file（RV MEDIUM・2026-07: safe_files の完全列挙から sort 無し early-exit へ置換）----

def test_has_any_file_empty_and_nested_and_missing():
    d = Path(tempfile.mkdtemp())
    try:
        assert worlds._has_any_file(d) is False                     # 空ディレクトリ
        (d / "a" / "b").mkdir(parents=True)
        assert worlds._has_any_file(d) is False                     # 空フォルダが深く入れ子でも False
        (d / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")
        assert worlds._has_any_file(d) is True                      # 深い階層のファイルも見つける
        assert worlds._has_any_file(d / "does-not-exist") is False  # 存在しないパスは False（例外にしない）
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_has_any_file_skips_symlinks():
    d = Path(tempfile.mkdtemp())
    outside = Path(tempfile.mkdtemp())
    try:
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        try:
            (d / "link.txt").symlink_to(outside / "secret.txt")      # symlink file
            (d / "linkdir").symlink_to(outside)                      # symlink dir
        except OSError as e:
            pytest.skip(f"symlink 非対応の環境: {e}")
        assert worlds._has_any_file(d) is False                      # symlink 経由のファイルは辿らない
        (d / "real.txt").write_text("r", encoding="utf-8")
        assert worlds._has_any_file(d) is True                       # 実ファイルがあれば True
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def test_has_any_file_strict_propagates_permission_error_not_swallows(monkeypatch, tmp_path):
    """`strict=True` は ENOENT 以外の OSError（権限エラー等）を re-raise する——
    `Path.is_dir()`/`os.DirEntry.is_dir()` は内部で任意の OSError を握って False を返すため、
    `_lstat_kind()`（`os.lstat`＋`stat.S_ISDIR`）へ置き換えて初めて伝播する。
    `strict=False`（既定）では同じ状況でも黙って False（従来どおり）。
    """
    d = tmp_path / "world"
    d.mkdir()

    def _boom(p, *a, **kw):
        raise PermissionError(13, "permission denied")

    monkeypatch.setattr(worlds.os, "lstat", _boom)
    with pytest.raises(PermissionError):
        worlds._has_any_file(d, strict=True)
    assert worlds._has_any_file(d, strict=False) is False


class _FailOnSecondNextEntry:
    def __init__(self, path):
        self.path = str(path)


class _FailOnSecondNextScandir:
    """`os.scandir()` の代わりに差し込む fake: 1件目は返すが、2件目以降を取得しようとしたら
    （＝呼び出し側が early return せず全件を読み切ろうとした証拠）AssertionError を投げる。
    `os.scandir()` の戻り値自体が context manager 兼 iterator であることに合わせる。
    """

    def __init__(self, first_path):
        self._first_path = first_path
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
            return _FailOnSecondNextEntry(self._first_path)
        raise AssertionError(
            "2件目以降が取得された＝list(os.scandir(...)) 相当の一括材料化に回帰している"
        )


def test_has_any_file_does_not_eagerly_materialize_scandir_entries(monkeypatch, tmp_path):
    """`_has_any_file` は最初の1件でファイルが見つかったら即 return し、`os.scandir()` の
    残りの entry を取得しようとしない（`list(os.scandir(...))` による全件材料化への回帰を
    検知する・巨大ディレクトリでの無制限メモリ消費の防止）。"""
    d = tmp_path / "world"
    d.mkdir()
    real_file = d / "f.txt"
    real_file.write_text("x", encoding="utf-8")

    def _fake_scandir(path):
        assert str(path) == str(d)
        return _FailOnSecondNextScandir(real_file)

    monkeypatch.setattr(worlds.os, "scandir", _fake_scandir)
    assert worlds._has_any_file(d) is True


def test_discover_fs_world_ids_strict_propagates_permission_error(monkeypatch, tmp_path):
    """base（fixtures/corpus・KB 直下）自体への stat が権限エラーで失敗した場合、
    `discover_fs_world_ids_strict()` は候補から静かに落とさず `ExternalResolverError` にする
    （`Path.is_dir()` ベースだと swallow されて空リストに潰れていた）。"""
    monkeypatch.setenv("SHERPA_KB_DIR", str(tmp_path))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)

    orig_lstat = worlds.os.lstat

    def _boom(p, *a, **kw):
        if str(p) == str(tmp_path):
            raise PermissionError(13, "permission denied")
        return orig_lstat(p, *a, **kw)

    monkeypatch.setattr(worlds.os, "lstat", _boom)
    with pytest.raises(worlds.ExternalResolverError):
        worlds.discover_fs_world_ids_strict()


def test_discover_fs_world_ids_strict_skips_enoent_base(monkeypatch, tmp_path):
    """base 自体が存在しない（ENOENT）場合は例外にせず、単に候補から外す（skip）。"""
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("SHERPA_KB_DIR", str(missing))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    assert worlds.discover_fs_world_ids_strict() == []


# ---- RV HIGH（2026-07-03・4頭脳比較で発覚の実機バグ）----
# MCP サブプロセス（cwd=authoring）で SHERPA_KB_DIR/SHERPA_DERIVED_DIR の相対既定値が cwd 基準に
# 誤解決され、派生MD（Office 文書の本文）ディレクトリが見つからず Office 文書が丸ごと台帳から
# 脱落していた（source/.txt 等は wd 直下を直接見るため影響を受けない非対称な症状）。


@pytest.fixture
def _chdir(tmp_path):
    """一時的に別ディレクトリへ chdir し、テスト終了後に元へ戻す（authoring 相当の別 cwd を模す）。"""
    old = Path.cwd()
    other = tmp_path / "authoring-like"
    other.mkdir()
    os.chdir(other)
    try:
        yield other
    finally:
        os.chdir(old)


def test_kb_and_derived_dir_default_resolve_to_repo_root_regardless_of_cwd(monkeypatch, _chdir):
    """SHERPA_KB_DIR/SHERPA_DERIVED_DIR 未設定時の既定値は cwd を変えてもリポジトリ基準のまま
    （恒久対策・クラスごと潰す）。挙動変更は「未設定時の既定」に限定される。"""
    monkeypatch.delenv("SHERPA_KB_DIR", raising=False)
    monkeypatch.delenv("SHERPA_DERIVED_DIR", raising=False)
    repo_root = Path(__file__).resolve().parents[2]
    assert worlds._kb() == repo_root / "data" / "kb"
    assert worlds.derived_dir("v1") == repo_root / "data" / "derived" / "v1"
    assert worlds._kb().is_absolute() and worlds.derived_dir("v1").is_absolute()


def test_kb_explicit_relative_value_still_resolves_against_cwd(monkeypatch, _chdir):
    """既定と違い、明示的に指定した相対値は従来どおり cwd 基準のまま（挙動変更なし・fail-safe回避）。"""
    monkeypatch.setenv("SHERPA_KB_DIR", "relkb")
    monkeypatch.setenv("SHERPA_DERIVED_DIR", "relderived")
    assert worlds._kb() == Path("relkb")
    assert worlds.derived_dir("v1") == Path("relderived") / "v1"


def test_world_dir_mcp_world_root_override_bypasses_registry_for_matching_world_only(monkeypatch, tmp_path):
    """RV HIGH#2: SHERPA_MCP_WORLD_ROOT は SHERPA_MCP_WORLD と一致する world_id にだけ効き、
    registry 解決を経由せず絶対パスをそのまま使う（他 world には影響しないスコープ限定）。

    `world_dir()` は `store.get_world()` の例外を自前で fail-safe に握る（呼出側の except に伝播
    しない）ため、「呼ばれなかった」の検証は例外送出でなく**呼び出し記録**で行う。
    """
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    calls: list = []

    def _track(world_id):
        calls.append(world_id)
        return None   # registry には無い体（未登録）
    monkeypatch.setattr(store, "get_world", _track)

    monkeypatch.setenv("SHERPA_MCP_WORLD", "target")
    monkeypatch.setenv("SHERPA_MCP_WORLD_ROOT", str(real_root))
    assert worlds.world_dir("target") == real_root          # override が効く（registry を呼ばない）
    assert calls == [], f"override が効くはずなのに registry へ問い合わせが発生: {calls}"

    # 他 world には効かない（通常どおり registry に問い合わせる＝calls に積まれる）。
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    monkeypatch.setenv("SHERPA_KB_DIR", str(tmp_path / "no-such-kb-dir"))   # フォールバックも None になるよう封じる
    assert worlds.world_dir("other") is None
    assert calls == ["other"], "他 world では override は効かず通常どおり registry に問い合わせるはず"


def test_world_dir_mcp_world_root_override_falls_back_when_invalid(monkeypatch):
    """override が壊れている（相対/不在/symlink）場合は override 自体を無視して通常解決へ
    フォールバックする（override 不整合だけを理由に fail-closed にはしない）。"""
    monkeypatch.setattr(store, "get_world", lambda world_id: None)   # registry: 未登録扱い
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    monkeypatch.setenv("SHERPA_MCP_WORLD", "target")
    monkeypatch.setenv("SHERPA_MCP_WORLD_ROOT", "relative/not/absolute")   # 絶対パスでない＝無効
    monkeypatch.setenv("SHERPA_KB_DIR", "/no/such/kb/dir")
    assert worlds.world_dir("target") is None   # override 無効→通常解決（KB フォールバック）→存在せず None


@pytest.fixture
def _fake_repo_root(monkeypatch, tmp_path):
    """`worlds._repo_root()` を tmp_path 配下へ差し替える。

    RV MEDIUM（2026-07-03 再検証）: 以前はここで `worlds.derived_dir()`（未設定時の既定）が
    実体の `data/derived/{world}` を指してしまい、同名の実 world があれば `shutil.rmtree` で
    実データを壊しかねなかった。`_repo_root()` 自体を差し替え、既定値の解決先を tmp_path 配下
    （pytest が自動クリーンアップ）に限定することで、実体には一切触れない形にする。
    """
    fake = tmp_path / "fake-repo-root"
    fake.mkdir()
    monkeypatch.setattr(worlds, "_repo_root", lambda: fake)
    return fake


def test_mcp_env_output_restores_office_docs_from_authoring_like_cwd(monkeypatch, tmp_path, _fake_repo_root):
    """統合回帰（authoring 相当の別 cwd から Office 込み件数が一致）: `agents._mcp_env()` が
    実際に計算する env を使って初めて (a) が成立し、素の相対パス（旧 _MCP_PASSTHROUGH 相当）では
    (b) のとおり Office 文書が脱落することを両方固定する（`_mcp_env()` 自体の絶対化ロジックを
    exercise する＝手動で絶対パスを用意するだけのテストでは本修正を検出できないため）。
    """
    from sherpa import agents, corpus_docs

    world_root = tmp_path / "world-root"
    world_root.mkdir()
    (world_root / "note.txt").write_text("ソース文書", encoding="utf-8")
    (world_root / "report.xlsx").write_bytes(b"")   # 中身は問わない（office_md 変換は別工程・存在と拡張子のみ判定）

    monkeypatch.setattr(store, "get_world",
                        lambda world_id: {"root_path": str(world_root)} if world_id == "testworld" else None)

    old_cwd = Path.cwd()
    authoring_like = tmp_path / "authoring-like"
    authoring_like.mkdir()
    try:
        # SHERPA_DERIVED_DIR は相対既定のまま（サーバプロセス cwd=repo root で計算する想定・
        # _fake_repo_root により実体の data/derived には触れない）。
        monkeypatch.delenv("SHERPA_DERIVED_DIR", raising=False)
        monkeypatch.delenv("SHERPA_KB_DIR", raising=False)
        md_dir = worlds.derived_dir("testworld") / "md"
        md_dir.mkdir(parents=True, exist_ok=True)
        (md_dir / "report.xlsx.md").write_text("## 概要\nダミーの変換済み本文\n", encoding="utf-8")

        # (a) agents._mcp_env() が計算した env（絶対パス化 + SHERPA_MCP_WORLD_ROOT）を適用すると、
        #     authoring 相当の別 cwd でも件数が一致する（source 1 + office 1 = 2）。
        mcp_env = agents._mcp_env("testworld", None)
        assert Path(mcp_env["SHERPA_DERIVED_DIR"]).is_absolute(), "SHERPA_DERIVED_DIR が絶対化されていない"
        assert Path(mcp_env["SHERPA_KB_DIR"]).is_absolute(), "SHERPA_KB_DIR が絶対化されていない"
        for k, v in mcp_env.items():
            monkeypatch.setenv(k, v)
        os.chdir(authoring_like)
        docs_after_fix = corpus_docs.world_documents("testworld")
        assert {d["name"] for d in docs_after_fix} == {"note.txt", "report.xlsx"}, \
            "agents._mcp_env() の絶対化後は authoring 相当の別 cwd でも Office 込みで件数一致するはず"

        # (b) 対照実験（修正前の再現）: 素の相対値をそのまま渡す（旧 _MCP_PASSTHROUGH の素通し相当）
        #     と、authoring 相当の別 cwd では Office 文書が脱落する。
        os.chdir(old_cwd)
        monkeypatch.setenv("SHERPA_DERIVED_DIR", "data/derived")   # 絶対化しない・素の相対既定
        monkeypatch.delenv("SHERPA_MCP_WORLD_ROOT", raising=False)
        os.chdir(authoring_like)
        docs_before_fix = corpus_docs.world_documents("testworld")
        assert [d["name"] for d in docs_before_fix] == ["note.txt"], \
            "相対 SHERPA_DERIVED_DIR を素通しすると authoring 相当の別 cwd で Office 文書が脱落するはず（修正前の再現）"
    finally:
        os.chdir(old_cwd)


def test_mcp_env_world_root_survives_registry_outage_for_world_documents(monkeypatch, tmp_path, _fake_repo_root):
    """RV LOW（2026-07-03 再検証）: `agents._mcp_env()` が実際に SHERPA_MCP_WORLD_ROOT を出すこと、
    かつその override があれば registry 完全不達（`store.get_world` が例外＝MCP サブプロセスの
    サンドボックスでネットワークが遮断される状況を模す）でも `world_documents()` の件数が
    registry 到達時と変わらないことを固定する。override を消すと落ちることも確認する
    （＝このテスト自体が回帰を検出できることの証跡）。
    """
    from sherpa import agents, corpus_docs

    world_root = tmp_path / "world-root"
    world_root.mkdir()
    (world_root / "note.txt").write_text("ソース文書", encoding="utf-8")
    (world_root / "report.xlsx").write_bytes(b"")

    monkeypatch.setattr(store, "get_world",
                        lambda world_id: {"root_path": str(world_root)} if world_id == "testworld" else None)
    monkeypatch.delenv("SHERPA_DERIVED_DIR", raising=False)
    monkeypatch.delenv("SHERPA_KB_DIR", raising=False)
    md_dir = worlds.derived_dir("testworld") / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    (md_dir / "report.xlsx.md").write_text("## 概要\nダミー\n", encoding="utf-8")

    # フェーズ1（サーバプロセス相当・registry 到達可）: _mcp_env() が world root を実際に解決して返す。
    mcp_env = agents._mcp_env("testworld", None)
    assert mcp_env.get("SHERPA_MCP_WORLD_ROOT") == str(world_root.resolve()), \
        "_mcp_env() が SHERPA_MCP_WORLD_ROOT を出していない"
    baseline_count = len(corpus_docs.world_documents("testworld"))
    assert baseline_count == 2

    # フェーズ2（MCP サブプロセス相当・registry 完全不達）: store.get_world を例外化しても、
    # override があれば registry へ一切問い合わせずに同じ件数のまま。
    def _unreachable(world_id):
        raise RuntimeError("simulated: Postgres unreachable (sandboxed network)")
    monkeypatch.setattr(store, "get_world", _unreachable)
    for k, v in mcp_env.items():
        monkeypatch.setenv(k, v)
    count_with_override = len(corpus_docs.world_documents("testworld"))
    assert count_with_override == baseline_count, \
        "registry 不達でも SHERPA_MCP_WORLD_ROOT override があれば件数は変わらないはず"

    # 対照実験（override を消すとこのテストが落ちることの確認）: registry 不達時は 0 件になる。
    monkeypatch.delenv("SHERPA_MCP_WORLD_ROOT", raising=False)
    count_without_override = len(corpus_docs.world_documents("testworld"))
    assert count_without_override == 0, \
        "override が無ければ registry 不達時に world_dir が None になり 0 件になるはず（対照実験）"


def test_resolve_external_world_registered_root_permission_error_raises_external_resolver_error(
        monkeypatch, tmp_path):
    """登録済み world の root が権限エラーで stat できない場合、`_is_dir_strict()`（`os.lstat`
    ベース）経由で `ExternalResolverError` にする——`Path.is_dir()` ベースだと swallow されて
    `not_found`（404 相当）に潰れてしまう。"""
    root = tmp_path / "registered-root"
    root.mkdir()

    orig_lstat = worlds.os.lstat

    def _boom(p, *a, **kw):
        if str(p) == str(root):
            raise PermissionError(13, "permission denied")
        return orig_lstat(p, *a, **kw)

    monkeypatch.setattr(worlds.os, "lstat", _boom)
    row = {"world_id": "regworld", "root_path": str(root), "storage_mode": "external_reference"}
    with pytest.raises(worlds.ExternalResolverError):
        worlds.resolve_external_world("regworld", registry_row=row)


def test_resolve_external_world_unregistered_candidate_permission_error_raises_external_resolver_error(
        monkeypatch, tmp_path):
    """未登録 world_id の候補（fixtures/KB）が権限エラーで stat できない場合も、静かに
    `not_found`（404 相当）へ潰さず `ExternalResolverError` にする（登録済み root と同じ扱いに
    統一）。"""
    monkeypatch.setenv("SHERPA_KB_DIR", str(tmp_path))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    candidate = tmp_path / "someworld"

    orig_lstat = worlds.os.lstat

    def _boom(p, *a, **kw):
        if str(p) == str(candidate):
            raise PermissionError(13, "permission denied")
        return orig_lstat(p, *a, **kw)

    monkeypatch.setattr(worlds.os, "lstat", _boom)
    with pytest.raises(worlds.ExternalResolverError):
        worlds.resolve_external_world("someworld", registry_row=None)


def test_resolve_external_world_forwards_connect_and_statement_timeout_to_store_get_world(monkeypatch):
    """RV6 是正の固定: `registry_row` 省略時（＝ここで `store.get_world()` を1回引く経路）は
    `connect_timeout`/`statement_timeout_ms` をそのまま転送する——PART-4（外部 API のリクエスト
    全体デッドライン）がこの registry 読み取り自体を無期限にブロックさせないために残り時間
    ベースで渡す配線（`store.get_world`/`resolve_external_world` docstring 参照）。"""
    captured = {}

    def _fake_get_world(world_id, *, connect_timeout=None, statement_timeout_ms=None):
        captured["connect_timeout"] = connect_timeout
        captured["statement_timeout_ms"] = statement_timeout_ms
        return None   # 未登録扱い（fixtures/KB フォールバックへ進む・実 DB 到達は不要）

    monkeypatch.setattr(store, "get_world", _fake_get_world)
    worlds.resolve_external_world("no-such-world-for-timeout-test", connect_timeout=2.5,
                                  statement_timeout_ms=1500)
    assert captured == {"connect_timeout": 2.5, "statement_timeout_ms": 1500}


def test_resolve_external_world_registry_row_given_skips_store_get_world_entirely(monkeypatch):
    """`registry_row` を渡した場合は `store.get_world()` を一切呼ばない（N+1 回避の既存契約）——
    `connect_timeout`/`statement_timeout_ms` を同時に渡してもこの経路には関係ない（無視される）。"""
    def _boom(*a, **kw):
        raise AssertionError("registry_row 指定時は store.get_world を呼んではいけない")

    monkeypatch.setattr(store, "get_world", _boom)
    res = worlds.resolve_external_world("whatever-world-id", registry_row=None, connect_timeout=1.0)
    assert res.status == "not_found"


def test_register_cleans_up_registry_row_when_run_locked_raises(monkeypatch, tmp_path):
    """新規登録中に `_run_locked` が（`res["status"]=="failed"` ではなく）例外を bare raise
    した場合も、registry 行・derived_dir 残骸を残さない——try/finally で両方の失敗経路を
    一本化する（`status=="failed"` の既存経路だけをガードしていると、PG/Neo4j 接続断等の
    途中失敗で「registry 行だけ残り取り込みは一度も成功していない」孤児状態になる）。
    """
    import contextlib

    from sherpa import es_index
    from sherpa.ingest import worker, world_neo4j

    monkeypatch.setattr(worlds, "derived_dir", lambda w: tmp_path / "derived_root")

    @contextlib.contextmanager
    def _noop_lock(*a, **kw):
        yield
    monkeypatch.setattr(store, "world_registry_lock", _noop_lock)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(store, "list_worlds_db", lambda: [])
    monkeypatch.setattr(store, "get_world", lambda w: None)
    monkeypatch.setattr(store, "world_by_root", lambda root: None)
    monkeypatch.setattr(store, "upsert_world", lambda *a, **kw: None)
    monkeypatch.setattr(store, "replace_documents", lambda w, rows: 0)   # cleanup の補償削除用（DB非依存）
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    monkeypatch.setattr(world_neo4j, "delete_world", lambda w, uri, user, pw: 0)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)

    delete_calls = []
    monkeypatch.setattr(store, "delete_world_row", lambda w: delete_calls.append(w))

    def _boom(*a, **kw):
        raise RuntimeError("simulated PG/Neo4j failure mid-run")
    monkeypatch.setattr(worker, "_run_locked", _boom)

    with pytest.raises(RuntimeError, match="simulated PG/Neo4j failure mid-run"):
        worlds.register("wtest", str(tmp_path))

    assert delete_calls == ["wtest"], "registry 行が孤児として残らない"


def test_register_cleanup_runs_rmtree_even_when_delete_world_row_raises(monkeypatch, tmp_path):
    """cleanup の DB 行削除（`store.delete_world_row`）自体が失敗（PG 障害等）しても、
    派生ディレクトリ削除（`shutil.rmtree`）は独立に実行され、finally 内の二次例外で
    元の worker 例外が握り潰されない（finally 内で例外を投げると伝播中の元例外を置き換えて
    しまう Python の仕様を踏まえ、cleanup 側の例外はログのみに留める）。
    """
    import contextlib

    from sherpa import es_index
    from sherpa.ingest import worker, world_neo4j

    der = tmp_path / "derived_root"
    der.mkdir()
    (der / "leftover.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(worlds, "derived_dir", lambda w: der)

    @contextlib.contextmanager
    def _noop_lock(*a, **kw):
        yield
    monkeypatch.setattr(store, "world_registry_lock", _noop_lock)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(store, "list_worlds_db", lambda: [])
    monkeypatch.setattr(store, "get_world", lambda w: None)
    monkeypatch.setattr(store, "world_by_root", lambda root: None)
    monkeypatch.setattr(store, "upsert_world", lambda *a, **kw: None)
    monkeypatch.setattr(store, "replace_documents", lambda w, rows: 0)   # cleanup の補償削除用（DB非依存）
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    monkeypatch.setattr(world_neo4j, "delete_world", lambda w, uri, user, pw: 0)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)

    def _boom_delete(w):
        raise RuntimeError("simulated PG failure during cleanup delete_world_row")
    monkeypatch.setattr(store, "delete_world_row", _boom_delete)

    def _boom_run(*a, **kw):
        raise RuntimeError("original worker failure")
    monkeypatch.setattr(worker, "_run_locked", _boom_run)

    with pytest.raises(RuntimeError, match="original worker failure"):
        worlds.register("wtest", str(tmp_path))

    assert not der.exists(), "delete_world_row が失敗しても derived_dir の rmtree は実行される"


def test_register_cleanup_rmtree_ignores_missing_derived_dir_without_warning(monkeypatch, tmp_path, caplog):
    """派生ディレクトリがそもそも作られていない（`_run_locked` が早期に失敗した）register
    失敗時、`shutil.rmtree` の `onexc` は `FileNotFoundError` を握り潰す——「削除すべき
    ものが無かっただけ」の正常系であり、紛らわしい「派生ディレクトリ削除でエラー」という
    warning を残さない（実際の削除失敗＝権限等だけを警告する契約）。
    """
    import contextlib

    from sherpa import es_index
    from sherpa.ingest import worker, world_neo4j

    der = tmp_path / "derived_root"   # mkdir しない＝存在しない
    monkeypatch.setattr(worlds, "derived_dir", lambda w: der)

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

    def _boom(*a, **kw):
        raise RuntimeError("simulated failure before derived dir creation")
    monkeypatch.setattr(worker, "_run_locked", _boom)

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError, match="simulated failure before derived dir creation"):
            worlds.register("wtest", str(tmp_path))

    assert "派生ディレクトリ削除でエラー" not in caplog.text
    assert "派生ディレクトリ削除に失敗しました" not in caplog.text


def test_register_cleanup_compensates_neo4j_documents_es_when_replace_documents_fails(monkeypatch, tmp_path):
    """`_run_locked` は Neo4j load を先に commit してから台帳（`replace_documents`）を
    更新する。台帳更新が失敗すると、registry 行は無いのに Neo4j にはグラフが載ったままの
    孤児状態になりうる——register 失敗時の cleanup は `DELETE /worlds` と同じ削除伝播
    （Neo4j 削除・台帳クリア・ES 削除）を補償的に実行し、かつ元の worker 例外（台帳更新
    失敗の詳細）を書き換えずに伝播させる。`_run_locked` 自体はモックせず、実際に
    Neo4j load→台帳更新の順で進めてから台帳更新だけを失敗させる。
    """
    import contextlib

    from sherpa import es_index
    from sherpa.ingest import worker, world_neo4j

    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "a.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: tmp_path / "derived_md")
    monkeypatch.setattr(worlds, "derived_dir", lambda w: tmp_path / "derived_root")

    @contextlib.contextmanager
    def _noop_lock(*a, **kw):
        yield
    monkeypatch.setattr(store, "world_registry_lock", _noop_lock)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(store, "list_worlds_db", lambda: [])
    monkeypatch.setattr(store, "get_world", lambda w: None)
    monkeypatch.setattr(store, "world_by_root", lambda root: None)
    monkeypatch.setattr(store, "upsert_world", lambda *a, **kw: None)
    monkeypatch.setattr(store, "set_world_sig", lambda *a, **kw: None)
    # ING-3: 開始時 INSERT（`start_ingest_run`）→完了時 UPDATE（`finish_ingest_run`）の2段構成
    # （`add_ingest_run` の単発 INSERT を置換）。
    monkeypatch.setattr(store, "downgrade_orphaned_extracting_runs", lambda world=None: [])
    monkeypatch.setattr(store, "update_ingest_run_progress", lambda run_id, progress: None)
    monkeypatch.setattr(store, "start_ingest_run", lambda w, **kw: {"id": 1, "version": w, "status": "extracting"})
    monkeypatch.setattr(store, "finish_ingest_run", lambda run_id, **kw: {"id": run_id, **kw})
    monkeypatch.setattr(worker, "build_world_graph", lambda w: ([], [], []))
    monkeypatch.setattr(worker, "_build_derived",
                        lambda w, **kw: {"converted": 0, "failed": 0, "unsupported": 0, "by_ext": {}})

    neo4j_load_calls = []
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    monkeypatch.setattr(world_neo4j, "load_world",
                        lambda nodes, edges, w, uri, user, pw: neo4j_load_calls.append(w) or (0, 0))
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
