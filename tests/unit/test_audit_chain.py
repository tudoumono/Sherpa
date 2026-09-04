"""監査ログ hash-chain（2026-07-01-監査ログ強化.md §Phase2・改ざん検知）の純関数テスト。

DB 非依存で core を固定する:
  - _audit_canonical が決定的（key 順・created_at の tz 正規化）。
  - _audit_entry_hash が prev_hash＋行内容に依存する（連鎖）。
  - 行内容の改ざん / prev_hash の付け替え / 並べ替えを検出できるロジック。

DB 統合（verify_audit_chain over Postgres）は手動確認済み（insert→tamper→broken_at 検出）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from sherpa import store


def _row(**kw):
    base = {k: None for k in store._AUDIT_CANON_FIELDS}
    base["created_at"] = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    base.update(kw)
    return base


def test_canonical_is_deterministic_and_key_order_independent():
    a = _row(action="x", detail={"b": 1, "a": 2})
    b = _row(detail={"a": 2, "b": 1}, action="x")   # dict の順序違い
    assert store._audit_canonical(a) == store._audit_canonical(b)


def test_canonical_normalizes_created_at_tz():
    from datetime import timedelta
    jst = timezone(timedelta(hours=9))
    utc_row = _row(created_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc))
    jst_row = _row(created_at=datetime(2026, 7, 1, 21, 0, 0, tzinfo=jst))  # 同一時刻
    assert store._audit_canonical(utc_row) == store._audit_canonical(jst_row)


def test_entry_hash_depends_on_prev_and_content():
    r = _row(action="login", actor_user_id="u1")
    h1 = store._audit_entry_hash(None, r)
    h2 = store._audit_entry_hash("PREV", r)          # prev 違い → hash 変わる
    assert h1 != h2
    r2 = _row(action="login", actor_user_id="u2")    # 内容違い → hash 変わる
    assert store._audit_entry_hash(None, r) != store._audit_entry_hash(None, r2)


def _build_chain(rows):
    """rows(list[dict]) を連鎖させて [(row, prev_hash, entry_hash)] を返す。"""
    out, prev = [], None
    for r in rows:
        eh = store._audit_entry_hash(prev, r)
        out.append((r, prev, eh))
        prev = eh
    return out


def _verify(chain):
    """(row, prev_hash, entry_hash) の列を検証（verify_audit_chain と同ロジック）。"""
    prev = None
    for i, (row, ph, eh) in enumerate(chain):
        if ph != prev:
            return {"ok": False, "broken_at": i, "reason": "prev_hash_mismatch"}
        if store._audit_entry_hash(ph, row) != eh:
            return {"ok": False, "broken_at": i, "reason": "entry_hash_mismatch"}
        prev = eh
    return {"ok": True, "broken_at": None}


def test_intact_chain_verifies():
    chain = _build_chain([_row(action=f"a{i}") for i in range(4)])
    assert _verify(chain)["ok"]


def test_content_tamper_detected():
    chain = _build_chain([_row(action=f"a{i}") for i in range(4)])
    row, ph, eh = chain[2]
    tampered = dict(row); tampered["action"] = "HACKED"   # entry_hash はそのまま＝内容だけ改ざん
    chain[2] = (tampered, ph, eh)
    r = _verify(chain)
    assert not r["ok"] and r["broken_at"] == 2 and r["reason"] == "entry_hash_mismatch"


def test_reorder_or_deletion_detected():
    chain = _build_chain([_row(action=f"a{i}") for i in range(4)])
    del chain[1]                                          # 1行削除＝prev_hash 連鎖が切れる
    r = _verify(chain)
    assert not r["ok"] and r["reason"] == "prev_hash_mismatch"


def test_audit_resolves_audit_insert_via_facade(monkeypatch):
    """RV HIGH（Codex 2026-07-13 フェーズ4）: `monkeypatch.setattr(store, "_audit_insert", …)` が
    `store.audit()` 経由の insert にも効くこと（facade シーム）。分割前の monolith では patch 先＝
    モジュールグローバルなので自然に効いていた挙動の保存を pin する。audit.py がローカル束縛の
    `_audit_insert` を直接呼ぶ退行が起きると、patch が素通りして本テストは
    `calls == []`（かつ fake 接続への insert 実行で AttributeError）で落ちる。
    DB 非依存: `_ensure`/`_connect` は audit モジュール側の束縛を差し替える（これらはシームの
    検証対象ではなく、実 DB を触らないための足場）。
    注: パッケージ属性 `sherpa.store.audit` は facade の re-export（関数 `audit`）に隠されるため、
    サブモジュール本体は sys.modules から取る。"""
    import sys

    import sherpa.store.audit  # noqa: F401  (sys.modules に確実に載せる)
    audit_mod = sys.modules["sherpa.store.audit"]

    class _FakeCM:
        def __enter__(self):
            return object()   # conn は patch 済み _audit_insert にしか渡らない

        def __exit__(self, *a):
            return False

    calls = []
    monkeypatch.setattr(audit_mod, "_ensure", lambda: None)
    monkeypatch.setattr(audit_mod, "_connect", lambda: _FakeCM())
    monkeypatch.setattr(
        store, "_audit_insert",
        lambda conn, *args, **kw: calls.append((args, kw)),
    )
    store.audit("u1", "seam.test", "system", "rid-1", {"k": "v"}, severity="warning")
    assert len(calls) == 1
    args, kw = calls[0]
    assert args == ("u1", "seam.test", "system", "rid-1", {"k": "v"})
    assert kw["severity"] == "warning" and kw["outcome"] == "success"


def _db() -> bool:
    try:
        store._ensure()
        return True
    except Exception:
        return False


def _reset_chain_baseline():
    """既存行を legacy 化（entry_hash=NULL）＋head を空に＝self-contained なテスト基点。"""
    with store._connect() as c:
        c.execute("UPDATE audit_log SET entry_hash=NULL, prev_hash=NULL WHERE entry_hash IS NOT NULL")
        c.execute("INSERT INTO audit_chain_head (singleton,last_id,last_hash,cnt,chain_start_id) "
                  "VALUES (TRUE,NULL,NULL,0,NULL) "
                  "ON CONFLICT (singleton) DO UPDATE SET last_id=NULL,last_hash=NULL,cnt=0,chain_start_id=NULL")


def _reanchor_to_current_tail():
    """残っている chained 行の末尾に head を合わせ直す（truncation 後の整合回復）。"""
    with store._connect() as c:
        r = c.execute("SELECT id, entry_hash FROM audit_log WHERE entry_hash IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1").fetchone()
        cnt = c.execute("SELECT count(*) AS n FROM audit_log WHERE entry_hash IS NOT NULL").fetchone()["n"]
        if r:
            c.execute("UPDATE audit_chain_head SET last_id=%s,last_hash=%s,cnt=%s WHERE singleton",
                      (r["id"], r["entry_hash"], cnt))
        else:
            c.execute("UPDATE audit_chain_head SET last_id=NULL,last_hash=NULL,cnt=0 WHERE singleton")


def test_db_chain_detects_tamper_and_truncation():
    """RV 反映（DB 統合）: JSONB round-trip 安定・改ざん・末尾削除(truncation)・NULL偽行注入 検出。
    **破壊的**（実 audit_log を legacy 化/削除する）ため、明示ガード SHERPA_AUDIT_CHAIN_DB_TEST=1 が無ければ skip。"""
    if os.environ.get("SHERPA_AUDIT_CHAIN_DB_TEST") != "1":  # env を先に見る（未設定で DB/DDL に触れない）
        pytest.skip("破壊的な DB 統合テスト（SHERPA_AUDIT_CHAIN_DB_TEST=1 で明示有効化）")
    if not _db():
        pytest.skip("DB down（Postgres 未起動）")
    from psycopg.types.json import Json
    _reset_chain_baseline()
    # nested dict（順不同 key）を含む2件を新スキームで追記 → verify ok（JSONB 往復が安定）。
    store.audit("u_chain", "test.dbchain", "test", "r1", detail={"n": 1, "d": {"z": 9, "a": 0}})
    store.audit("u_chain", "test.dbchain", "test", "r2", detail={"n": 2})
    assert store.verify_audit_chain()["ok"], "fresh chain should verify"
    # NULL-hash 偽行を chain の後ろに注入 → null_row_injected を検出（legacy skip をすり抜けない）。
    with store._connect() as c:
        c.execute("INSERT INTO audit_log (actor_user_id, action, resource_type) "
                  "VALUES ('attacker','fake.injected','test')")
    vi = store.verify_audit_chain()
    assert not vi["ok"] and vi["reason"] == "null_row_injected", f"NULL injection not detected: {vi}"
    with store._connect() as c:            # 掃除
        c.execute("DELETE FROM audit_log WHERE action='fake.injected'")
    assert store.verify_audit_chain()["ok"], "cleanup failed"
    # 末尾行 truncation → 検出されること（head アンカーと不一致）。
    with store._connect() as c:
        lid = c.execute("SELECT last_id FROM audit_chain_head WHERE singleton").fetchone()["last_id"]
        c.execute("DELETE FROM audit_log WHERE id=%s", (lid,))
    v = store.verify_audit_chain()
    assert not v["ok"] and v["reason"] in ("count_mismatch", "head_mismatch"), \
        f"truncation not detected: {v}"
    # head 削除でも hashed 行が残れば missing_head を検出。
    with store._connect() as c:
        c.execute("DELETE FROM audit_chain_head")
    vh = store.verify_audit_chain()
    assert not vh["ok"] and vh["reason"] == "missing_head", f"missing head not detected: {vh}"
    # head を cnt=0 にリセットしても hashed 行があれば count_mismatch。
    with store._connect() as c:
        c.execute("INSERT INTO audit_chain_head (singleton,last_id,last_hash,cnt,chain_start_id) "
                  "VALUES (TRUE,NULL,NULL,0,NULL)")
    vr = store.verify_audit_chain()
    assert not vr["ok"] and vr["reason"] == "count_mismatch", f"cnt=0 bypass not detected: {vr}"
