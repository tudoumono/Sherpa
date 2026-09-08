"""チャット履歴の検索（H1・docs/proposals/2026-09-07-履歴検索と下調べ並列化.md §1）テスト。

`test_auth_sharing.py`/`test_sanitized_share.py`/`test_share_fork_refresh.py` と同じ流儀:
store 層の関数（`store.search_conversations`）を直接呼んでデータ・可視集合の契約を固定し、
HTTP ステータス（422）の対応づけだけルータ経由（TestClient）で確認する。要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from _common import _login, _try_init
from _test_users import register_test_uid
from sherpa import auth, store

_PW = "HistSearch-Pw!9"


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _mk_users(sfx: str, *names: str):
    uids = [f"{n}{sfx}" for n in names]
    for u in uids:
        store.upsert_user(u, email=f"{u}@ex.local", display_name=u.upper(),
                          password_hash=auth.hash_password(_PW), role="user")
        register_test_uid(u)
    return uids


def _future(days=7):
    return datetime.now(timezone.utc) + timedelta(days=days)


def _past(days=1):
    return datetime.now(timezone.utc) - timedelta(days=days)


def _mk_share(cid, owner, invitee, *, expires_at=None, sfx=""):
    th = hashlib.sha256(f"tok-{sfx}-{cid}-{invitee}".encode()).hexdigest()
    return store.create_share(cid, owner, th, expires_at if expires_at is not None else _future(), [invitee])


# ===================================================================================
# 可視集合（本人の会話＋受領共有だけ・他人の会話は混ざらない・snapshot は出ない）
# ===================================================================================

def test_search_scoped_to_own_visible_set_excludes_other_users():
    if not _try_init():
        return
    sfx = _sfx()
    a, b = _mk_users(sfx, "hsa", "hsb")
    conv_a = store.create_conversation(user_id=a, world="v1", title="バッチ処理の相談A")
    store.add_message(conv_a["id"], "user", "バッチ処理について教えて")
    conv_b = store.create_conversation(user_id=b, world="v1", title="バッチ処理の相談B")
    store.add_message(conv_b["id"], "user", "バッチ処理について教えて")

    hits = store.search_conversations(a, "バッチ処理")
    ids = {r["id"] for r in hits}
    assert conv_a["id"] in ids, "本人の会話が検索結果に出ていない"
    assert conv_b["id"] not in ids, "他人の会話が検索結果に混ざっている"


def test_search_excludes_sanitized_snapshot_itself():
    if not _try_init():
        return
    sfx = _sfx()
    (uid,) = _mk_users(sfx, "hss")
    conv = store.create_conversation(user_id=uid, world="v1", title="個人ファイルの要約")
    cid = conv["id"]
    store.add_message(cid, "user", content="my_salary.xlsx を要約して", personal=True)
    store.add_message(cid, "assistant", content="個人ファイルによると年収は900万です",
                      answer={"headline": "個人ファイルによると年収は900万です",
                              "personal_sources": [{"doc_id": "my_salary.xlsx"}]}, personal=True)
    store.set_contains_personal_workspace(cid)
    snap = store.create_sanitized_snapshot(uid, cid)
    assert snap is not None

    # snapshot 自身の本文（伏字後プレースホルダ）に一致する語で検索しても、snapshot 自体は
    # 一覧に出ない（`_visible_conversations_rows` の origin<>'sanitized_snapshot' を継承）。
    hits = store.search_conversations(uid, store._REDACTED_TEXT[:8])
    ids = {r["id"] for r in hits}
    assert snap not in ids, "sanitized_snapshot が検索結果に出ている（内部成果物のはず）"
    assert cid not in ids   # 元会話の本文はこのプレースホルダ文言を含まないので不一致（当然） も確認


# ===================================================================================
# q のエスケープ（ILIKE の %/_ をリテラル化）
# ===================================================================================

def test_search_escapes_ilike_wildcards():
    if not _try_init():
        return
    sfx = _sfx()
    (uid,) = _mk_users(sfx, "hew")
    # タイトルはどれも検索語を含まない汎用文言にし、本文（ILIKE 経由）だけを狙う。
    conv_percent_lit = store.create_conversation(user_id=uid, world="v1", title="経理相談P1")
    store.add_message(conv_percent_lit["id"], "assistant", content="本年の利率は100%達成の見込みです")
    conv_percent_decoy = store.create_conversation(user_id=uid, world="v1", title="経理相談P2")
    store.add_message(conv_percent_decoy["id"], "assistant", content="本年の利率は100X達成の見込みです")

    conv_underscore_lit = store.create_conversation(user_id=uid, world="v1", title="在庫相談U1")
    store.add_message(conv_underscore_lit["id"], "assistant", content="在庫コード_123を確認しました")
    conv_underscore_decoy = store.create_conversation(user_id=uid, world="v1", title="在庫相談U2")
    store.add_message(conv_underscore_decoy["id"], "assistant", content="在庫コードX123を確認しました")

    # `%` がエスケープされていなければ「100X達成」も任意文字列一致でヒットしてしまう。
    percent_hits = {r["id"] for r in store.search_conversations(uid, "100%達成")}
    assert conv_percent_lit["id"] in percent_hits
    assert conv_percent_decoy["id"] not in percent_hits, "% がエスケープされておらずワイルドカード一致した"

    # `_` がエスケープされていなければ「在庫コードX123」も任意1文字一致でヒットしてしまう。
    underscore_hits = {r["id"] for r in store.search_conversations(uid, "在庫コード_123")}
    assert conv_underscore_lit["id"] in underscore_hits
    assert conv_underscore_decoy["id"] not in underscore_hits, "_ がエスケープされておらずワイルドカード一致した"


# ===================================================================================
# 受領共有: 有効なら本文一致・取消/期限切れ/個人ブロックならタイトルのみ
# ===================================================================================

def test_search_received_share_content_requires_valid_share():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "hro", "hri")

    def _mk_shared_conv(title):
        conv = store.create_conversation(user_id=owner, world="v1", title=title)
        cid = conv["id"]
        store.add_message(cid, "user", "質問")
        store.add_message(cid, "assistant", "夜間バッチが異常終了しました")
        return cid

    cid_active = _mk_shared_conv("調査ログA")
    wid_active = store.accept_share(_mk_share(cid_active, owner, invitee, sfx=sfx), invitee)

    cid_revoked = _mk_shared_conv("調査ログB")
    sid_revoked = _mk_share(cid_revoked, owner, invitee, sfx=sfx)
    wid_revoked = store.accept_share(sid_revoked, invitee)
    assert store.revoke_share(sid_revoked, owner) is True

    cid_expired = _mk_shared_conv("調査ログC")
    wid_expired = store.accept_share(
        _mk_share(cid_expired, owner, invitee, expires_at=_past(), sfx=sfx), invitee)

    cid_personal = _mk_shared_conv("調査ログD")
    wid_personal = store.accept_share(_mk_share(cid_personal, owner, invitee, sfx=sfx), invitee)
    store.set_contains_personal_workspace(cid_personal)

    # 本文一致（"夜間バッチ"）は有効な共有のワッパーだけがヒットする。
    content_hits = {r["id"]: r for r in store.search_conversations(invitee, "夜間バッチ")}
    assert wid_active in content_hits and content_hits[wid_active]["match"]["where"] == "message"
    assert wid_revoked not in content_hits, "取消済み共有が本文検索でヒットしている"
    assert wid_expired not in content_hits, "期限切れ共有が本文検索でヒットしている"
    assert wid_personal not in content_hits, "個人ブロック共有が本文検索でヒットしている"

    # タイトル一致は共有の有効性に関わらずヒットする（ワッパー自身の title 列を見るだけのため）。
    for title, wid in (("調査ログB", wid_revoked), ("調査ログC", wid_expired), ("調査ログD", wid_personal)):
        title_hits = {r["id"]: r for r in store.search_conversations(invitee, title)}
        assert wid in title_hits, f"{title} のタイトル検索がヒットしない"
        assert title_hits[wid]["match"]["where"] == "title"


# ===================================================================================
# sanitized 共有: 伏字後のスナップショット本文だけが対象（伏字化前の秘密は漏れない）
# ===================================================================================

def test_search_sanitized_share_targets_redacted_snapshot_only():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "hsso", "hssi")
    conv = store.create_conversation(user_id=owner, world="v1", title="個人ファイルの調査")
    cid = conv["id"]
    store.add_message(cid, "user", content="my_secret.xlsx の年収を教えて", personal=True)
    store.add_message(cid, "assistant", content="個人ファイルによると年収は900万です",
                      answer={"headline": "個人ファイルによると年収は900万です",
                              "personal_sources": [{"doc_id": "my_secret.xlsx"}]}, personal=True)
    store.add_message(cid, "user", content="夜間バッチの件も教えて")
    store.add_message(cid, "assistant", content="共有KBによると夜間バッチは正常終了です",
                      answer={"headline": "共有KBによると夜間バッチは正常終了です", "lens": "qa", "sources": []})
    store.set_contains_personal_workspace(cid)

    snap = store.create_sanitized_snapshot(owner, cid)
    assert snap is not None
    wid = store.accept_share(_mk_share(snap, owner, invitee, sfx=sfx), invitee)

    # 伏字化前の秘密（本来の本文）は検索してもヒットしない。
    leaked = {r["id"] for r in store.search_conversations(invitee, "900万")}
    assert wid not in leaked, "伏字化前の個人情報が受領共有の検索でヒットしている"

    # 伏字後のプレースホルダ文言（スナップショットに実際に保存されている本文）はヒットする。
    redacted_hits = {r["id"]: r for r in store.search_conversations(invitee, store._REDACTED_TEXT[:8])}
    assert wid in redacted_hits, "伏字後の本文がヒットしない"
    assert redacted_hits[wid]["match"]["where"] == "message"

    # 非個人ターンはスナップショットでも本文が保持されるので通常どおりヒットする。
    kb_hits = {r["id"] for r in store.search_conversations(invitee, "夜間バッチ")}
    assert wid in kb_hits, "非個人ターンの本文がヒットしない"


# ===================================================================================
# q の長さ検証（HTTP ステータス対応づけ・trim 後 1〜100 字）
# ===================================================================================

def test_search_query_length_validation_http_status():
    if not _try_init():
        return
    sfx = _sfx()
    (uid,) = _mk_users(sfx, "hqv")
    c = _login(uid, _PW)

    r = c.get("/conversations", params={"q": ""})
    assert r.status_code == 422, r.text

    r = c.get("/conversations", params={"q": "   "})   # trim 後 0 字
    assert r.status_code == 422, r.text

    r = c.get("/conversations", params={"q": "x" * 101})
    assert r.status_code == 422, r.text

    r = c.get("/conversations", params={"q": "x" * 100})   # 境界（100字）は許可
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)

    r = c.get("/conversations")   # 省略時は従来どおり全件・応答形も不変
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
