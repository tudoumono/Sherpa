"""POST /admin/users/import（ユーザーの CSV 一括追加）。DB 不可は graceful SKIP。"""
from __future__ import annotations

import time

import pytest

IMPORT_ERROR: Exception | None = None
try:
    from fastapi.testclient import TestClient

    from sherpa import auth, store
    from sherpa.api import app
except Exception as e:  # pragma: no cover
    IMPORT_ERROR = e
    TestClient = None  # type: ignore[assignment]

_HEADER = "uid,display_name,email,role,password\n"


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> None:
    if IMPORT_ERROR is not None:
        pytest.skip(f"infra down: {IMPORT_ERROR}")
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")


def _login(uid: str, role: str) -> "TestClient":
    from _test_users import register_test_uid
    pw = f"CsvImp-{_sfx()}!"
    store.upsert_user(uid, email=None, display_name=uid, password_hash=auth.hash_password(pw),
                      role=role, status="active")
    register_test_uid(uid)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return c


def _csv(*lines: str) -> dict:
    return {"file": ("users.csv", (_HEADER + "\n".join(lines) + "\n").encode("utf-8"), "text/csv")}


def test_import_rejects_all_when_any_row_is_bad():
    _try_init()
    from _test_users import register_test_uid
    sfx = _sfx()
    c = _login(f"csvadm{sfx}", "admin")
    good, bad = f"csvgood{sfx}", f"csvbad{sfx}"
    register_test_uid(good)
    register_test_uid(bad)
    r = c.post("/admin/users/import", files=_csv(
        f"{good},良い,,user,Zq7!xW2mK9p", f"{bad},悪い,,user,short"))
    assert r.status_code == 422, r.text
    errs = r.json()["errors"]
    assert [(e["line"], e["uid"]) for e in errs] == [(3, bad)]
    assert "Zq7!xW2mK9p" not in r.text
    assert store.get_user(good) is None


def test_import_creates_everyone_with_must_change_password():
    _try_init()
    from _test_users import register_test_uid
    sfx = _sfx()
    c = _login(f"csvadm{sfx}", "admin")
    u1, u2 = f"csvone{sfx}", f"csvtwo{sfx}"
    register_test_uid(u1)
    register_test_uid(u2)
    r = c.post("/admin/users/import", files=_csv(
        f"{u1},一,,user,Zq7!xW2mK9p", f"{u2},二,{u2}@x.local,admin,Rt5#vB8nL3q"))
    assert r.status_code == 200, r.text
    assert r.json() == {"created": 2, "uids": [u1, u2]}
    for uid, role in ((u1, "user"), (u2, "admin")):
        row = store.get_user(uid)
        assert row["role"] == role and row["must_change_password"] is True


def test_import_forbidden_for_non_admin():
    _try_init()
    c = _login(f"csvusr{_sfx()}", "user")
    r = c.post("/admin/users/import", files=_csv("x,x,,user,Zq7!xW2mK9p"))
    assert r.status_code == 403
