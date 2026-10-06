"""world レジストリ 受け入れ（鏡モデル・要 Neo4j＋PG）: 別案件＝追加／参照先変更＝全削除＋再ミラー／削除。

参照元フォルダ（任意パス）を world にバインドし、register→rebind→delete を検証する。
rebind は **旧 world の派生物を完全削除して新パスから作り直す**（差分でなく破棄→再作成）。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import time

import pytest
from _corpus_helpers import _mk
from _world_setup import driver

from sherpa import store, worlds
from sherpa.ingest import worker

W = "test_world_admin"
OK_STATUS = ("auto_published", "auto_published_with_flags")


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """このファイルはログインせず直接叩く前提（compat モード）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


class _Sandbox:
    """一時フォルダ・world・Neo4j ノードを追跡し、テスト終了時に派生物まで掃除する。"""

    def __init__(self):
        self.drv = driver()
        self.wids: list[str] = []
        self.dirs: list[str] = []

    def folder(self, project=None, prog=None) -> str:
        d = tempfile.mkdtemp()
        self.dirs.append(d)
        if project:
            _mk(d, project, prog)
        return d

    def world(self, wid: str) -> str:
        self.wids.append(wid)
        return wid

    def names(self, wid=W):
        with self.drv.session() as s:
            return sorted(r["n"] for r in s.run(
                "MATCH (x:Entity {world_id:$w}) RETURN x.name AS n", w=wid))

    def cleanup(self):
        for wid in self.wids:
            for fn in (lambda: worlds.delete(wid), lambda: store.delete_world_row(wid)):
                try:
                    fn()
                except Exception:
                    pass
            try:
                der = worlds.derived_dir(wid)
                shutil.rmtree(der, ignore_errors=True)
                shutil.rmtree(der.with_name("." + der.name + ".rebind-bak"), ignore_errors=True)
            except Exception:
                pass
            with self.drv.session() as s:
                s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=wid)
        self.drv.close()
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def sb():
    sandbox = _Sandbox()
    yield sandbox
    sandbox.cleanup()


def _resolved(path) -> pathlib.Path:
    return pathlib.Path(path).resolve()


def _wait_ingest_idle(c, wid, *, timeout=60.0) -> dict:
    """即受付・背景実行のため、完了（または失敗）まで status をポーリングする（テスト専用）。
    登録直後は world 行がまだ無く 404 になりうる＝「作成中」として継続する。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = c.get(f"/worlds/{wid}/status")
        if r.status_code == 404:
            time.sleep(0.2)
            continue
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("running_progress") is None and last.get("last_run_status") is not None:
            return last
        time.sleep(0.2)
    raise AssertionError(f"取り込みが {timeout}s 以内に完了しませんでした: world={wid} last={last}")


def _wait_world_deleted(c, wid, *, timeout=60.0) -> None:
    """DELETE /worlds/{wid} は背景実行のため、`GET .../status` が 404 になるまで待つ（テスト専用）。"""
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        r = c.get(f"/worlds/{wid}/status")
        last_status = r.status_code
        if r.status_code == 404:
            return
        time.sleep(0.2)
    raise AssertionError(f"削除が {timeout}s 以内に完了しませんでした: world={wid} last_status={last_status}")


def _client():
    from fastapi.testclient import TestClient
    from sherpa.api import app
    return TestClient(app)


def _flag_reasons(rec) -> list:
    return [f.get("reason") for f in (rec["extraction_snapshot"] or {}).get("flags", [])]


# ===== register / rebind / delete =====

def test_register_rebind_delete(sb):
    a = sb.folder("案件X", "FOOPROG")
    b = sb.folder("案件Y", "BARPROG")
    sb.world(W)
    rel_a = "案件X/03_開発/01_ソース/FOOPROG.cbl"
    rel_b = "案件Y/03_開発/01_ソース/BARPROG.cbl"
    # 別案件＝新 world を参照元 a にバインドして取り込む（追加）
    r = worlds.register(W, str(_resolved(a)))
    assert r["status"] in OK_STATUS
    assert worlds.world_dir(W) == _resolved(a)                    # レジストリ binding が効く
    assert sb.names() == ["FOOPROG"]
    assert {d["name"] for d in store.list_documents(W)} == {rel_a}

    # 参照先変更＝旧を全削除して b から作り直し（FOOPROG 消え BARPROG だけ・台帳も）
    worlds.rebind(W, str(_resolved(b)))
    assert worlds.world_dir(W) == _resolved(b)
    assert sb.names() == ["BARPROG"], sb.names()                  # 破棄→再作成（差分でない）
    assert {d["name"] for d in store.list_documents(W)} == {rel_b}

    # world 完全削除（グラフ空・台帳空・レジストリ行なし）。参照元フォルダは残る。
    assert worlds.delete(W) is True
    assert sb.names() == [] and store.get_world(W) is None
    assert store.list_documents(W) == []
    assert pathlib.Path(b).is_dir()


def test_delete_neo4j_success_then_pg_failure_self_heals(sb, monkeypatch):
    # delete が Neo4j 削除成功後に PG replace で失敗した窓: 例外は registry 行削除に届かず行が残る。
    # `_wipe_locked` 冒頭の last_sig 無効化（pre-invalidate）により、次回 sync が必ず再構築して自己修復する。
    a = sb.folder("案件X", "DELFOO")
    wid = sb.world(W + "_delete_pg_fail")
    worlds.register(wid, str(_resolved(a)))
    assert sb.names(wid) == ["DELFOO"]
    sig0 = store.get_world(wid)["last_sig"]
    assert sig0

    def _boom(world, rows):
        raise RuntimeError("pg replace fault (after neo4j delete commit)")

    with monkeypatch.context() as m:
        m.setattr(store, "replace_documents", _boom)
        with pytest.raises(RuntimeError, match="pg replace fault"):
            worlds.delete(wid)

    row = store.get_world(wid)
    assert row is not None
    assert row["last_sig"] == ""                                   # 旧実装は sig0 のまま残り穴になった
    assert sb.names(wid) == []                                     # Neo4j は削除済み

    r = worker.sync(wid)                                           # 次回 sync＝自己修復
    assert r["changed"] is True
    assert r["status"] in OK_STATUS
    assert sb.names(wid) == ["DELFOO"]
    assert store.get_world(wid)["last_sig"] == sig0


def test_wipe_invalidates_sig_before_neo4j_delete_order_pin(sb, monkeypatch):
    from sherpa.ingest import world_neo4j
    a = sb.folder("案件X", "ORDERFOO")
    wid = sb.world(W + "_wipe_order")
    worlds.register(wid, str(_resolved(a)))
    assert sb.names(wid) == ["ORDERFOO"]

    calls = []
    orig_set_sig = store.set_world_sig
    orig_delete = world_neo4j.delete_world

    def _set_sig(world, sig, manifest=None):
        calls.append("set_world_sig")
        return orig_set_sig(world, sig, manifest=manifest)

    def _delete(*a, **k):
        calls.append("delete_world")
        return orig_delete(*a, **k)

    with monkeypatch.context() as m:
        m.setattr(store, "set_world_sig", _set_sig)
        m.setattr(world_neo4j, "delete_world", _delete)
        assert worlds.delete(wid) is True

    assert calls == ["set_world_sig", "delete_world"]


def test_wipe_pre_invalidate_failure_is_fail_closed_before_neo4j_delete(sb, monkeypatch):
    # pre-invalidate（`set_world_sig(world, "")`）が失敗したら Neo4j delete に到達せず例外が伝播し、
    # registry 行・last_sig・グラフは無傷のまま残る。
    from sherpa.ingest import world_neo4j
    a = sb.folder("案件X", "WIPEFAILFOO")
    wid = sb.world(W + "_wipe_pre_invalidate_fail")
    worlds.register(wid, str(_resolved(a)))
    assert sb.names(wid) == ["WIPEFAILFOO"]
    sig0 = store.get_world(wid)["last_sig"]
    assert sig0

    orig_delete = world_neo4j.delete_world
    delete_calls = {"n": 0}

    def _track_delete(*a, **k):
        delete_calls["n"] += 1
        return orig_delete(*a, **k)

    def _boom_set_sig(world, sig, manifest=None):
        raise RuntimeError("set_world_sig fault (pre-invalidate, PG断)")

    with monkeypatch.context() as m:
        m.setattr(world_neo4j, "delete_world", _track_delete)
        m.setattr(store, "set_world_sig", _boom_set_sig)
        with pytest.raises(RuntimeError, match="pre-invalidate"):
            worlds.delete(wid)

    assert delete_calls["n"] == 0
    row = store.get_world(wid)
    assert row is not None
    assert row["last_sig"] == sig0
    assert sb.names(wid) == ["WIPEFAILFOO"]


# ===== rebind の失敗時ロールバック =====

def test_rebind_failure_preserves_derived_and_binding(sb, monkeypatch):
    # 失敗するとバインドは旧 root へ戻り、旧派生物も保持される（fail-closed）。
    a = sb.folder("案件X", "FOOPROG")
    b = sb.folder("案件Y", "BARPROG")
    sb.world(W)
    worlds.register(W, str(_resolved(a)))
    marker = worlds.derived_dir(W) / "semantic" / "_marker.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("keep-me", encoding="utf-8")

    def _boom(*aa, **kk):                                          # rebind は lock-free の `_run_locked` を直接呼ぶ
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "_run_locked", _boom)
    with pytest.raises(RuntimeError):
        worlds.rebind(W, str(_resolved(b)))

    assert worlds.world_dir(W) == _resolved(a)
    assert marker.is_file() and marker.read_text(encoding="utf-8") == "keep-me"
    assert sb.names() == ["FOOPROG"]


def test_rebind_neo4j_committed_then_pg_replace_fails_restores_old_graph(sb, monkeypatch):
    # Neo4j load 成功（新 root コミット済）後に PG replace で失敗した場合、即時再構築で Neo4j が旧 root へ戻る
    # （修正前は Neo4j＝新 root のまま・last_sig 不変で self-heal せず、registry/台帳/派生＝旧・Neo4j＝新の永続不整合）。
    a = sb.folder("案件X", "FOOPROG")
    b = sb.folder("案件Y", "BARPROG")
    wid = sb.world("test_world_admin_neo4j_rollback")
    worlds.register(wid, str(_resolved(a)))
    assert sb.names(wid) == ["FOOPROG"]

    real_replace = store.replace_documents
    calls = {"n": 0}

    def _fail_first(world, rows):
        calls["n"] += 1
        if calls["n"] == 1:                                        # rebind の B に対する replace だけ失敗
            raise RuntimeError("pg replace fault (after neo4j commit)")
        return real_replace(world, rows)                           # rollback の A 即時再構築は成功させる

    with monkeypatch.context() as m:
        m.setattr(store, "replace_documents", _fail_first)
        with pytest.raises(RuntimeError, match="pg replace fault"):
            worlds.rebind(wid, str(_resolved(b)))

    assert calls["n"] == 2                                         # rebind(B) 失敗 → rollback(A) 再構築
    assert worlds.world_dir(wid) == _resolved(a)
    assert sb.names(wid) == ["FOOPROG"]                            # 修正前は ["BARPROG"] が残った
    assert store.get_world(wid)["last_sig"]


def test_rebind_failure_preserves_original_exception_even_if_restore_fails(sb, monkeypatch):
    # 復元経路の二次例外で元例外を握り潰さない＋bind 復元（同一 tx の bind＋sig 無効化）が失敗（PG 断継続）
    # したら復旧を一切足さない（bind=新のまま次回 sync が新へ収束）。
    # (a) 元例外が伝播 (b) 復旧の _run_locked が呼ばれない (c) bind が新 root のまま (d) 旧派生は backup 側に残る
    a = sb.folder("案件X", "FOOPROG")
    b = sb.folder("案件Y", "BARPROG")
    wid = sb.world("test_world_admin_exc_preserve")
    worlds.register(wid, str(_resolved(a)))
    der = worlds.derived_dir(wid)
    marker = der / "semantic" / "_marker_A.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("A-derived", encoding="utf-8")
    backup_path = der.with_name("." + der.name + ".rebind-bak")
    calls = {"n": 0}

    def _primary(*aa, **kk):
        calls["n"] += 1
        raise ValueError("rebind-primary (取り込み失敗)")

    def _secondary(*aa, **kk):
        raise RuntimeError("restore-secondary (これが元例外を隠したら NG)")

    monkeypatch.setattr(worker, "_run_locked", _primary)
    monkeypatch.setattr(store, "restore_bind_invalidate_sig", _secondary)
    with pytest.raises(ValueError, match="rebind-primary"):        # (a)
        worlds.rebind(wid, str(_resolved(b)))
    assert calls["n"] == 1                                         # (b)
    assert worlds.world_dir(wid) == _resolved(b)                   # (c)
    assert backup_path.exists() and (backup_path / "semantic" / "_marker_A.txt").is_file()   # (d)
    assert not marker.exists()                                     # (d)


def test_rebind_recovery_rebuild_failure_still_propagates_original_exception(sb, monkeypatch):
    # bind 復元が成功して復旧経路（旧 root 再構築）に入り、そこで別種例外が起きても伝播するのは元例外。
    a = sb.folder("案件X", "FOOPROG")
    b = sb.folder("案件Y", "BARPROG")
    wid = sb.world("test_world_admin_recovery_exc")
    worlds.register(wid, str(_resolved(a)))
    calls = {"n": 0}

    def _run(*aa, **kk):
        calls["n"] += 1
        if calls["n"] == 1:                                        # rebind 本体（B）＝元例外
            raise ValueError("rebind-primary (取り込み失敗)")
        raise RuntimeError("recovery-secondary (復旧の再構築失敗・元例外を隠したら NG)")   # 復旧（A）

    monkeypatch.setattr(worker, "_run_locked", _run)               # restore は本物＝bind 復元成功で復旧経路へ
    with pytest.raises(ValueError, match="rebind-primary"):
        worlds.rebind(wid, str(_resolved(b)))
    assert calls["n"] == 2


def test_rebind_run_id_recovery_success_finalizes_once_with_rolled_back_reason(sb, monkeypatch):
    # run_id 指定時、新 root 試行・旧 root 復旧は非 terminal な内部段で、受付 run の確定は最後に一度だけ。
    # 復旧が成功していれば published_snapshot/source_doc_ids を温存したまま failed・rebind_failed_rolled_back で確定する。
    a = sb.folder("案件X", "FOOPROG")
    b = sb.folder("案件Y", "BARPROG")
    wid = sb.world("test_world_admin_rv2_recovery_ok")
    worlds.register(wid, str(_resolved(a)))
    orig_run = worker._run_locked
    calls = {"n": 0}

    def _run(*aa, **kk):
        calls["n"] += 1
        if calls["n"] == 1:                                        # 新 root（B）試行＝失敗
            raise ValueError("rebind-primary (取り込み失敗)")
        return orig_run(*aa, **kk)                                 # 復旧（旧 root A）は本物を通す

    run_id = store.start_ingest_run(wid, scan_root=None, created_by="admin")["id"]
    monkeypatch.setattr(worker, "_run_locked", _run)
    with pytest.raises(ValueError, match="rebind-primary"):
        worlds.rebind(wid, str(_resolved(b)), run_id=run_id)
    assert calls["n"] == 2

    rec = store.get_latest_run_summary(wid)
    assert rec["id"] == run_id and rec["status"] == "failed"
    assert "rebind_failed_rolled_back" in _flag_reasons(rec)
    pub = store.get_latest_published_run_summary(wid)
    assert pub is not None and pub["published_snapshot"]           # NULL 上書きなら None/空
    full = store.list_ingest_runs(wid, limit=1)[0]
    assert full["id"] == run_id and full["source_doc_ids"]         # 空 [] へ上書きされていない


def test_rebind_run_id_recovery_failure_uses_rollback_failed_reason(sb, monkeypatch):
    # 復旧の再構築自体が失敗/例外なら「戻せた」とは言えず `rebind_rollback_failed` で確定する。
    a = sb.folder("案件X", "FOOPROG")
    b = sb.folder("案件Y", "BARPROG")
    wid = sb.world("test_world_admin_rv2_recovery_fail")
    worlds.register(wid, str(_resolved(a)))
    calls = {"n": 0}

    def _run(*aa, **kk):
        calls["n"] += 1
        raise RuntimeError(f"boom-{calls['n']}")                   # 新 root 試行・復旧再構築ともに失敗

    run_id = store.start_ingest_run(wid, scan_root=None, created_by="admin")["id"]
    monkeypatch.setattr(worker, "_run_locked", _run)
    with pytest.raises(RuntimeError, match="boom-1"):
        worlds.rebind(wid, str(_resolved(b)), run_id=run_id)
    assert calls["n"] == 2

    rec = store.get_latest_run_summary(wid)
    assert rec["id"] == run_id and rec["status"] == "failed"
    reasons = _flag_reasons(rec)
    assert "rebind_rollback_failed" in reasons
    assert "rebind_failed_rolled_back" not in reasons


@pytest.mark.parametrize("restore, expect, forbid", [
    ("ok", "旧状態を保持しました", "戻せませんでした"),
    ("fail", "旧状態に戻せませんでした", "旧状態を保持しました"),
    ("es_failed", "旧状態に戻せませんでした", "旧状態を保持しました"),
])
def test_rebind_admin_error_message_follows_restore_outcome(sb, monkeypatch, restore, expect, forbid):
    # 付け替え失敗の文は旧状態への復元の成否で分ける（復元に失敗したのに「保持した」と言わない）。
    from sherpa import world_admin_service
    a = sb.folder("案件X", "FOOPROG")
    b = sb.folder("案件Y", "BARPROG")
    wid = sb.world("test_world_admin_rebind_msg_%s" % restore)
    worlds.register(wid, str(_resolved(a)))
    orig_run = worker._run_locked
    calls = {"n": 0}

    def _run(*aa, **kk):
        calls["n"] += 1
        if calls["n"] == 1 or restore == "fail":
            raise RuntimeError(f"boom-{calls['n']}")
        res = orig_run(*aa, **kk)
        if restore == "es_failed":                                 # 復旧で全文検索の索引を作り直せなかった
            res = {**res, "status": "auto_published_with_flags",
                   "flags": list(res.get("flags") or []) + [
                       {"doc": None, "action": "warn", "reason": "es_index_failed:ConnectionError@es"}]}
        return res

    monkeypatch.setattr(worker, "_run_locked", _run)
    monkeypatch.setattr(world_admin_service, "resolve_root", lambda p: str(_resolved(p)))
    with pytest.raises(world_admin_service.WorldAdminUnavailableError) as ei:
        world_admin_service.rebind(wid, str(_resolved(b)))
    assert expect in str(ei.value) and forbid not in str(ei.value)


def test_rebind_backup_move_failure_rolls_back_bind_and_self_heals(sb, monkeypatch):
    # 退避（旧派生を `.rebind-bak` へ `os.replace`）の失敗も他の rebind 失敗と同じロールバック経路
    # （bind を旧へ戻し last_sig を無効化 → 旧 root から即時再構築）に入る。
    a = sb.folder("案件X", "FOOPROG")
    b = sb.folder("案件Y", "BARPROG")
    wid = sb.world("test_world_admin_backup_escape")
    worlds.register(wid, str(_resolved(a)))
    assert sb.names(wid) == ["FOOPROG"]
    real_replace = os.replace

    def _boom_replace(src, dst):
        if str(dst).endswith(".rebind-bak"):                       # 退避の os.replace(der, backup) だけ狙い撃ち
            raise OSError("boom: backup escape simulated (fault injection)")
        return real_replace(src, dst)

    with monkeypatch.context() as m:
        m.setattr(os, "replace", _boom_replace)
        with pytest.raises(OSError, match="boom"):
            worlds.rebind(wid, str(_resolved(b)))

    assert worlds.world_dir(wid) == _resolved(a)
    assert sb.names(wid) == ["FOOPROG"]
    assert store.get_world(wid)["last_sig"]


# ===== API =====

def test_api_world_lifecycle(sb):
    """POST /worlds（登録）→ 一覧 → rebind → DELETE。同一フォルダ再登録は冪等（202）・別パスへの同名登録は 409・
    不在パスは 422・未知 rebind は 404。"""
    a = sb.folder("案件X", "APIFOO")
    b = sb.folder("案件Y", "APIBAR")
    sb.world(W)
    c = _client()
    ok = c.post("/worlds", json={"world_id": W, "path": a, "label": "案件X"})
    assert ok.status_code == 202, ok.text
    assert ok.json()["run_id"] is not None
    _wait_ingest_idle(c, W)
    assert any(x["world_id"] == W for x in c.get("/worlds").json()["worlds"])
    assert sb.names() == ["APIFOO"]
    dup = c.post("/worlds", json={"world_id": W, "path": a})       # 同一フォルダの再登録は冪等（チェック→リラン）
    assert dup.status_code == 202, dup.text
    assert dup.json()["world_id"] == W
    _wait_ingest_idle(c, W)
    assert sb.names() == ["APIFOO"]
    assert c.post("/worlds", json={"world_id": W, "path": b}).status_code == 409    # 別パスは rebind を使う
    assert c.post("/worlds", json={"world_id": "nope2", "path": "/no/such/dir"}).status_code == 422
    rb = c.post(f"/worlds/{W}/rebind", json={"path": b})
    assert rb.status_code == 202, rb.text
    assert rb.json()["run_id"] is not None
    _wait_ingest_idle(c, W)
    assert sb.names() == ["APIBAR"]
    assert c.post("/worlds/unknownw/rebind", json={"path": b}).status_code == 404
    del_res = c.delete(f"/worlds/{W}")
    assert del_res.status_code == 202, del_res.text
    assert del_res.json()["run_id"] is not None
    _wait_world_deleted(c, W)
    assert sb.names() == [] and store.get_world(W) is None


def test_conflicts_and_fail_closed(sb):
    """登録済みがあれば2本目は拒否（単一登録契約）／参照元が消えたら world_dir は fail-closed（None）。"""
    a = sb.folder("案件X", "DUPFOO")
    c = sb.folder("案件Z", "GONE")
    w1, w2 = sb.world(W + "_1"), sb.world(W + "_2")
    worlds.register(w1, str(_resolved(a)))
    with pytest.raises(worlds.WorldConflict):                      # 同じ world_id（内容更新は refresh・参照先変更は rebind）
        worlds.register(w1, str(_resolved(c)))
    with pytest.raises(worlds.WorldConflict):                      # 同一参照元を別 world_id で二重登録
        worlds.register(w2, str(_resolved(a)))
    with pytest.raises(worlds.WorldConflict):                      # 別 world_id・別 root でも2本目は拒否
        worlds.register(w2, str(_resolved(c)))
    assert worlds.world_dir(w1) == _resolved(a)
    shutil.rmtree(a, ignore_errors=True)
    assert worlds.world_dir(w1) is None                            # 参照元消失＝unavailable（fixtures/KB に落とさない）


def test_fs_browser_confined(monkeypatch):
    """フォルダ選択 API: 許可ルート配下のサブフォルダだけ返し、範囲外・`..` は 403。"""
    base = tempfile.mkdtemp()
    try:
        (pathlib.Path(base) / "proj" / "sub").mkdir(parents=True)
        monkeypatch.setenv("SHERPA_BROWSE_ROOTS", base)
        c = _client()
        top = c.get("/fs/list").json()                             # path 空＝ルート直下
        assert any(e["name"] == "proj" for e in top["entries"]) and top["parent"] is None
        proj = c.get("/fs/list", params={"path": str(pathlib.Path(base) / "proj")}).json()
        assert any(e["name"] == "sub" for e in proj["entries"])
        assert proj["parent"] == str(_resolved(base))              # 親はルート（その上へは行けない）
        assert c.get("/fs/list", params={"path": "/etc"}).status_code == 403
        assert c.get("/fs/list", params={"path": str(pathlib.Path(base) / "..")}).status_code == 403
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_status_reports_indexed_and_skipped_office(sb):
    """取り込み状況の正直化: txt はインデックス／壊れ Office は変換失敗／PDF は未対応／コード無しでグラフ0。"""
    a = sb.folder()
    base = pathlib.Path(a) / "案件X" / "01_資料"
    base.mkdir(parents=True)
    (base / "メモ.txt").write_text("調査メモ\n", encoding="utf-8")
    (base / "資料.docx").write_bytes(b"PK\x03\x04dummy")            # 壊れ OOXML→変換失敗
    (base / "表.xlsx").write_bytes(b"PK\x03\x04dummy")
    (base / "仕様.pdf").write_bytes(b"%PDF-1.4 dummy")              # PDF→未対応
    wid = sb.world("test_world_status")
    c = _client()
    assert c.post("/worlds", json={"path": str(_resolved(a)), "world_id": wid}).status_code == 202
    s = _wait_ingest_idle(c, wid)
    from sherpa.ingest import office_md
    pdf_on = ".pdf" in office_md.convertible_exts()                # PDF はバックエンド導入時のみ変換対象
    failed_notices = 3 if pdf_on else 2
    # 内容抽出に失敗した文書も locator/理由つきの「失敗の記録」として索引する（追跡できなくなるのを防ぐ）
    assert s["indexed"] == 1 + failed_notices
    assert s["by_doctype"].get("テキスト") == 1
    assert s["office_md"] == failed_notices
    assert s["office_failed"] == failed_notices
    assert s["skipped_office"] == (0 if pdf_on else 1)
    assert s["graph_nodes"] == 0


def test_office_indexed_and_searchable(sb):
    """Office（xlsx）が取り込みで決定的MD化され、検索でヒット＋DLは原本に解決する。"""
    import openpyxl
    from sherpa import documents
    from sherpa.grep_tool import grep_search
    a = sb.folder()
    d = pathlib.Path(a) / "案件X" / "01_資料"
    d.mkdir(parents=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["A2"] = "項目", "消費税率テスト値ZZZ"
    wb.save(d / "表.xlsx")
    rel = "案件X/01_資料/表.xlsx"
    wid = sb.world("test_world_office")
    c = _client()
    assert c.post("/worlds", json={"path": str(_resolved(a)), "world_id": wid}).status_code == 202
    s = _wait_ingest_idle(c, wid)
    assert s["office_md"] == 1 and s["indexed"] == 1 and s["skipped_office"] == 0
    hits = grep_search("消費税率テスト値ZZZ", wid, max_hits=10)
    assert hits and hits[0]["doc_id"] == rel and hits[0]["ext"] == ".xlsx"   # 派生MD→元 xlsx の rel
    p = documents.resolve(rel, wid)                                          # DL は原本（.xlsx）に解決
    assert p and p.is_file() and p.suffix == ".xlsx"


def test_sync_rebuilds_when_derived_missing(sb):
    """ソース無変更でも派生MD が消えていれば sync は再ビルドする（後付け導入/派生削除の自己修復）。"""
    import openpyxl
    a = sb.folder()
    d = pathlib.Path(a) / "案件X"
    d.mkdir(parents=True)
    wb = openpyxl.Workbook()
    wb.active["A1"] = "派生再生成テスト"
    wb.save(d / "x.xlsx")
    wid = sb.world("test_world_derived")
    worlds.register(wid, str(_resolved(a)))
    der = worlds.derived_dir(wid)
    assert der.is_dir() and any(der.rglob("*.md"))
    shutil.rmtree(der)                                             # 派生だけ消す（ソースは無変更）
    r = worker.sync(wid)
    assert r["changed"] is True
    assert der.is_dir() and any(der.rglob("*.md"))


def test_fs_subdirs_skips_inaccessible(monkeypatch):
    """フォルダ一覧は 1 件の権限エラー（Windows システムファイル等）で全体が止まらない（per-entry skip）。"""
    import sherpa.api as api
    base = tempfile.mkdtemp()
    try:
        for n in ("aaa", "mmm", "zzz"):
            (pathlib.Path(base) / n).mkdir()
        (pathlib.Path(base) / "bbb_bad.tmp").write_text("x")       # "aaa" と "mmm" の間で is_dir が例外を投げる
        real_is_dir = pathlib.Path.is_dir

        def fake_is_dir(self):
            if self.name == "bbb_bad.tmp":
                raise PermissionError(13, "Permission denied")
            return real_is_dir(self)

        monkeypatch.setattr(pathlib.Path, "is_dir", fake_is_dir)
        names = [e["name"] for e in api._subdirs(pathlib.Path(base))]
        assert names == ["aaa", "mmm", "zzz"], names
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_post_world_without_id_autogenerates(sb):
    """POST /worlds は world_id 省略可＝自動採番。日本語名は label に。"""
    a = sb.folder("案件X", "AUTOFOO")
    c = _client()
    r = c.post("/worlds", json={"path": str(_resolved(a)), "label": "自動採番案件"})
    assert r.status_code == 202, r.text
    wid = sb.world(r.json()["world_id"])
    assert wid
    _wait_ingest_idle(c, wid)
    match = next((x for x in c.get("/worlds").json()["worlds"] if x["world_id"] == wid), None)
    assert match is not None and match["label"] == "自動採番案件"


def test_refresh_change_detection(sb):
    """変更検知: 変わってなければ no-op、フォルダにファイルを足すと sync が再取り込み。手動 refresh も同様。"""
    a = sb.folder("案件X", "SYNCA")
    wid = sb.world(W + "_sync")
    worlds.register(wid, str(_resolved(a)))
    assert sb.names(wid) == ["SYNCA"]
    assert worker.sync(wid)["changed"] is False                    # 変更なし＝no-op
    _mk(a, "案件X", "SYNCB")                                       # フォルダに直接ファイル追加
    r = worker.sync(wid)
    assert r["changed"] is True and set(sb.names(wid)) == {"SYNCA", "SYNCB"}
    # 手動 refresh API: 受付処理自身が run 行を確保するため run_id は必ず非 null（無変化でも terminal 化・joined=False）
    c = _client()
    rr = c.post(f"/worlds/{wid}/refresh")
    assert rr.status_code == 202, rr.text
    payload = rr.json()
    assert payload["run_id"] is not None and payload["joined"] is False
    _wait_ingest_idle(c, wid)
    assert set(sb.names(wid)) == {"SYNCA", "SYNCB"}


def test_diff_and_idempotent_register(sb):
    """差分チェック（read-only・書き込みなし）と「このフォルダを登録」の冪等性（未登録→登録／登録済み→リラン）。"""
    a = sb.folder("案件X", "DIFFA")
    root = str(_resolved(a))
    wid = sb.world("test_world_diff")
    c = _client()
    # 1) 未登録フォルダの差分＝全ファイルが added・registered False、かつ登録されない（read-only）
    d0 = c.post("/worlds/diff", json={"path": root}).json()
    assert d0["registered"] is False and d0["indexed"] == 0 and len(d0["added"]) >= 1
    assert not any(x["root_path"] == root for x in c.get("/worlds").json()["worlds"])
    # 2) 登録（即受付・背景実行の完了を待つ）
    r = c.post("/worlds", json={"path": root, "world_id": wid, "label": "差分案件"})
    assert r.status_code == 202 and r.json()["world_id"] == wid, r.text
    _wait_ingest_idle(c, wid)
    # 3) 登録直後の差分＝なし
    d1 = c.post("/worlds/diff", json={"path": root}).json()
    assert d1["registered"] is True and d1["added"] == [] and d1["changed"] == [] and d1["removed"] == []
    # 4) ファイルを足すと差分に出る（まだ反映しない＝グラフは DIFFA のまま）
    _mk(a, "案件X", "DIFFB")
    d2 = c.post("/worlds/diff", json={"path": root}).json()
    assert len(d2["added"]) == 1 and any("DIFFB" in x for x in d2["added"])
    assert sb.names(wid) == ["DIFFA"]
    # 5) 「このフォルダを登録」再押下＝冪等リラン
    r2 = c.post("/worlds", json={"path": root})
    assert r2.status_code == 202 and r2.json()["world_id"] == wid, r2.text
    _wait_ingest_idle(c, wid)
    assert set(sb.names(wid)) == {"DIFFA", "DIFFB"}
    # 6) もう差分なし
    d3 = c.post("/worlds/diff", json={"path": root}).json()
    assert d3["added"] == [] and d3["changed"] == [] and d3["removed"] == []


def test_diff_robust_and_id_mismatch(sb):
    """明細未保存でも署名一致なら差分なし＋次回 sync でバックフィル／既存 root に別 world_id 登録は 409。"""
    a = sb.folder("案件X", "ROBUSTA")
    root = str(_resolved(a))
    wid = sb.world("test_world_robust")
    c = _client()
    r = c.post("/worlds", json={"path": root, "world_id": wid, "label": "堅牢案件"})
    assert r.status_code == 202, r.text
    _wait_ingest_idle(c, wid)
    assert c.post("/worlds", json={"path": root, "world_id": "other_id"}).status_code == 409   # すり替え拒否
    with store._connect() as cc:
        cc.execute("UPDATE worlds SET last_manifest=NULL WHERE world_id=%s", (wid,))
    d = c.post("/worlds/diff", json={"path": root}).json()
    assert d["added"] == [] and d["changed"] == [] and d["removed"] == []      # 全件 added にならない
    worker.sync(wid)
    assert store.get_world(wid).get("last_manifest")


def test_fixtures_not_referenced_without_flag(monkeypatch):
    """本番非参照: SHERPA_USE_FIXTURES が無いと world_dir('v1') は架空 golden(fixtures/corpus) に落ちない
    （フラグでのみ参照・レジストリ未登録の v1 を前提）。"""
    from fastapi import HTTPException
    from sherpa.providers.prompts import _kb_hint_abs              # Codex のプロンプトに渡す資料の場所も固定
    from sherpa.api import _require_world
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    monkeypatch.setattr(store, "get_world", lambda *a, **k: None)  # v1 は未登録
    assert not worlds._fixtures()
    assert worlds.world_dir("v1") is None                          # data/kb/v1 も無い→None（fixtures に落ちない）
    assert "fixtures/corpus" not in _kb_hint_abs("v1") and "data/kb" in _kb_hint_abs("v1")
    with pytest.raises(HTTPException) as exc:                      # 未解決 world は 404 で弾く（Neo4j 直読み防止）
        _require_world("v1")
    assert exc.value.status_code == 404
    monkeypatch.setenv("SHERPA_USE_FIXTURES", "1")                 # フラグ有り → fixtures に解決
    d2 = worlds.world_dir("v1")
    assert d2 is not None and "fixtures/corpus/v1" in str(d2)
    assert "fixtures/corpus/v1" in _kb_hint_abs("v1")
    _require_world("v1")                                           # 例外を投げない
