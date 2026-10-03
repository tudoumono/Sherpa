"""利用明細エクスポート API（`GET /admin/usage/export`）テスト。

- admin ゲート（非 admin → 403）
- 質問・回答の本文／会話の題名／参照した資料／台帳の id 一覧が ZIP のどのファイルにも含まれない
- turns.csv に会話番号・回答番号が含まれる（DB や `make trace` との突き合わせに使う）
- `days` の明示指定と `from`/`to` の同時指定は 422

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import io
import zipfile

import pytest

from _common import _login, _sfx, _try_init
from _test_users import register_test_uid
from sherpa import auth, store


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@usage-export.local", display_name=f"表示名-{uid}",
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def test_usage_export_requires_admin():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"uexpusr{sfx}", f"UexpUser{sfx}"
    _mk_user(uid, pw, role="user")
    u = _login(uid, pw)
    r = u.get("/admin/usage/export")
    assert r.status_code == 403, r.text


def test_usage_export_days_and_from_to_conflict_is_422():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uexpadm{sfx}", f"UexpAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)
    f, t = "2026-09-18T00:00:00+09:00", "2026-09-19T00:00:00+09:00"
    r = admin.get("/admin/usage/export", params={"days": 7, "from": f, "to": t})
    assert r.status_code == 422, r.text
    r2 = admin.get("/admin/usage/export", params={"from": f})
    assert r2.status_code == 422, r2.text


def test_usage_export_zip_excludes_content_but_includes_conversation_and_message_ids():
    """本文・題名・参照資料・台帳の id 一覧はどのファイルにも出さない。turns.csv には会話番号・
    回答番号が入り、agents.csv/tools.csv も対応する下調べ役・ツールの行を持つ。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uexpview{sfx}", f"UexpView{sfx}"
    user_uid, user_pw = f"uexpown{sfx}", f"UexpOwn{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    world = f"uexpworld{sfx}"
    secret = f"極秘マーカー-{sfx}"
    odd_model = f"モデル名 {sfx}"   # 識別子の形でない値（CSV・活動記録では中身を出さない）

    conv = store.create_conversation(user_id=user_uid, world=world, title=f"{secret}-題名")
    cid = conv["id"]
    store.add_message(cid, "user", f"{secret}-質問")
    answer = {
        "usage": {"provider": "codex", "model": odd_model, "input_tokens": 100,
                  "cached_input_tokens": 10, "output_tokens": 50, "reasoning_output_tokens": 5},
        "stop_kind": "completed",
        "duration_ms": 12345,
        "limits": {"tool_result_clipped": 2, "total_budget_hit": True},
        "sources": [{"title": f"{secret}-資料", "path": f"/mnt/x/{secret}.docx"}],
        "activity": {
            "v": 1, "source": "codex_rollout", "app_version": "0.14.0+deadbeef",
            "settings": {"mode": "standard", "depth": "standard", "review_rounds": 2, "reasoning": "high"},
            "phases_ms": {"prepare": 1000, "agent": 5000, "post": 500, "total": 6500},
            "agents": [
                {"role": "parent", "model": "gpt-5.5",
                 "tokens": {"input_tokens": 80, "cached_input_tokens": 10, "output_tokens": 40,
                           "reasoning_output_tokens": 5},
                 "rounds": [[80, 40, 1, 0]], "compactions": [],
                 "tools": {"ripgrep_search": {"calls": 3, "bytes": 500, "max_bytes": 200, "ms": 10}}},
                {"role": "child", "model": "gpt-5.5",
                 "tokens": {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 10,
                           "reasoning_output_tokens": 0},
                 "rounds": [], "compactions": [], "tools": {}},
            ],
        },
        "investigation": {"complete": True, "continuations": 1, "counts": {"source_confirmed": 2},
                          "non_terminal": [f"{secret}-台帳項目id"]},
    }
    assistant = store.add_message(cid, "assistant", f"{secret}-回答", lens="qa", answer=answer)
    msg_id = assistant["id"]

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/export?days=30")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/zip")
    assert secret not in r.headers.get("content-disposition", "")

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "README.txt" in names
    assert "summary.json" in names
    assert "turns.csv" in names
    assert "agents.csv" in names
    assert "tools.csv" in names
    assert "aux_calls.csv" in names
    assert "daily.csv" in names
    assert f"activity/{cid}.txt" in names

    for name in names:
        raw = zf.read(name)
        text = raw.decode("utf-8-sig", errors="replace")
        assert secret not in text, f"{name} に本文/題名/資料名/台帳idが含まれている"

    for name in names:
        if name.endswith(".csv") or name.startswith("activity/"):
            assert odd_model not in zf.read(name).decode("utf-8-sig"), name

    turns_csv = zf.read("turns.csv").decode("utf-8-sig")
    assert str(cid) in turns_csv
    assert str(msg_id) in turns_csv
    assert "completed" in turns_csv          # 終了理由（閉じた語彙）はそのまま出る

    agents_csv = zf.read("agents.csv").decode("utf-8-sig")
    assert "本体" in agents_csv and "下調べ役1" in agents_csv

    tools_csv = zf.read("tools.csv").decode("utf-8-sig")
    assert "ripgrep_search" in tools_csv

    activity_txt = zf.read(f"activity/{cid}.txt").decode("utf-8-sig")
    assert f"会話 {cid}" in activity_txt
    assert f"#{msg_id}" in activity_txt


def test_usage_export_csv_cells_are_not_formulas():
    """表計算ソフトで開いても、= + - @ で始まる値を数式として実行させない。"""
    from sherpa import usage_export
    lines = usage_export._csv_bytes(["a", "b"], [["=HYPERLINK(1)", 3]]).decode("utf-8-sig").splitlines()
    assert lines[1] == "'=HYPERLINK(1),3"
