"""利用統計チャット API（`POST /admin/usage/chat`）テスト。

- admin ゲート（非 admin → 403、未ログイン → 401 は test_auth_snapshot.py で snapshot 済み）
- 正常系（プロバイダをスタブ・応答が返る・admin.usage_chat_asked が監査される）
- プロバイダ未接続/失敗は別プロバイダへフォールバックせず明示エラー（503/502）
- 入力上限超過（質問の長さ・履歴の件数・不正な role）は 400（履歴1件の長さ超過は拒否せず切り詰めて
  受理する＝別テスト）。400・502・503・想定外の例外（500）いずれも admin.usage_chat_asked を監査する。
- 前後の空白パディングで上限チェックを迂回できない（検証・送信ともに trim 後の値を使う）
- 長い正常回答を history に積んでも、連続する次のターンが失敗し続けない
- 本処理（`answer_usage_question`・設定取得）側の `ValueError` は 400（入力検証エラー）に
  誤分類せず 500 になる（`ValueError` の捕捉は検証段階に限定する）
- クライアント側で既に上限まで切り詰められた（省略印付き・上限ちょうどの）履歴も、
  監査（history_truncated）に切り詰めの事実が残る
- リクエスト body は型を固定しないため、件数超過（業務上限20件〜防御的上限10000件）・型不正な
  値・質問フィールド自体の欠落・トップレベルがオブジェクトでない body（配列・文字列・数値・
  `null`・空）もハンドラへ到達して 400（監査あり）になる（防御的上限10000件超・本文サイズ上限
  超過のみ、意図的に監査なしの422/413のまま残す）
- `history` 省略/明示的 `null` は空履歴として受理し、監査の `history_len` には 0 を記録する
  （型不正で不明を示す `null` にしない）
- OpenAI の起動時 env シード未確定・Ollama の SSRF allowlist 外接続先は、実送信を試みる前に
  503（未接続）になる（502＝送信を試みたが失敗、への誤分類・未送信呼び出しの計測を防ぐ）。
  応答が JSON として解析できない（送信は成功した）場合は逆に 502・計測ありにする（503への
  誤分類を防ぐ）
- history 最大蓄積＋大きな統計データでもプロンプト総量上限により古い完全ターンを落として
  成功する（502による再送ループを防ぐ）
- 送信前に pending 監査行を確保してから実送信する（fail-closed・監査の書き込みに失敗したら
  送信しない）。実送信・監査とも同期 DB/LLM 呼び出しのため threadpool 実行になる
  （`admin_usage_chat` docstring 参照・単一 worker プロセスで event loop を塞がないため）
- 本文サイズ上限（1MiB）超過は 413（監査なし）。BOM 付き・UTF-8 として不正・深すぎるネスト
  （`RecursionError`）はいずれも固定文言の 400（監査あり・詳細は反射しない）

LLM 呼び出しは `sherpa.usage_chat._complete`/`_resolve_cfg` をスタブし、実ネットワークへは出ない
（`tests/unit/test_intent_llm.py`・`tests/unit/test_graph_admin.py` と同じ差し替え流儀）。

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store, usage_chat
from sherpa.api import app
from sherpa.routers import audit_usage


from _common import _login, _sfx, _try_init


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@usagechat.local", display_name=f"表示名-{uid}",
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _stub_ok(monkeypatch, answer: str = "テストの回答です") -> None:
    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})
    monkeypatch.setattr(usage_chat, "_complete", lambda system, user, cfg: json.dumps({"answer": answer}))


def test_usage_chat_requires_admin_and_login():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"uchusr{sfx}", f"UcUser{sfx}"
    _mk_user(uid, pw, role="user")

    anon = TestClient(app, raise_server_exceptions=False)
    r = anon.post("/admin/usage/chat", json={"question": "x"})
    assert r.status_code == 401, r.text

    u = _login(uid, pw)
    r2 = u.post("/admin/usage/chat", json={"question": "x"})
    assert r2.status_code == 403, r2.text


def test_usage_chat_success_returns_answer_and_audits(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchadm{sfx}", f"UcAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    _stub_ok(monkeypatch, answer="今月は u1 が最多です")

    r = admin.post("/admin/usage/chat", json={"question": "今月一番使っているユーザーは？"})
    assert r.status_code == 200, r.text
    # 応答に実際に使った provider/接続先種別を含める（画面はこの確定値で「送信先」表示を
    # 更新する・_stub_ok の cfg は openai・endpoint_kind 省略＝None）。
    assert r.json() == {"answer": "今月は u1 が最多です", "provider_used": "openai", "endpoint_kind": None,
                        "notes": []}   # 改善ログ要約の取得に失敗していなければ空

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows, "admin.usage_chat_asked was not recorded"
    assert rows[0]["outcome"] == "success"
    assert rows[0]["detail"]["provider_used"] == "openai"
    assert rows[0]["detail"]["question_len"] == len("今月一番使っているユーザーは？")
    assert rows[0]["detail"]["improvement_log_failed"] is False


def test_usage_chat_improvement_log_failure_still_succeeds_with_notes(monkeypatch):
    """改善ログの要約取得に失敗しても質問応答自体は成功する（fail-open）。ただし黙って
    `{}` を渡さず、応答の notes・監査 detail の両方に失敗を明示する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchilf{sfx}", f"UcIlf{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    _stub_ok(monkeypatch, answer="今月の集計です")
    monkeypatch.setattr("sherpa.improvement_log.compact_summary",
                        lambda *, days: (_ for _ in ()).throw(RuntimeError("boom")))

    r = admin.post("/admin/usage/chat", json={"question": "今月は？"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "今月の集計です"
    assert body["notes"] == [usage_chat.IMPROVEMENT_LOG_UNAVAILABLE_NOTE]

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "success"
    assert rows[0]["detail"]["improvement_log_failed"] is True


def test_usage_chat_llm_call_failure_after_improvement_log_failure_still_flags_audit(monkeypatch):
    """改善ログの要約取得に失敗した直後に実送信（`_complete`）も失敗した場合（502）、
    `answer_usage_question` は例外を投げて戻り値の notes が router に届かないが、
    監査 detail の improvement_log_failed は False へ落ちてはいけない（例外側に載せて運ぶ）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchilf2{sfx}", f"UcIlf2{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    monkeypatch.setattr("sherpa.improvement_log.compact_summary",
                        lambda *, days: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})

    def _boom(system, user, cfg):
        raise TimeoutError("upstream timeout")
    monkeypatch.setattr(usage_chat, "_complete", _boom)

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 502, r.text

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["reason"] == "llm_call_failed"
    assert rows[0]["detail"]["improvement_log_failed"] is True, \
        "改善ログ取得失敗の情報が例外経由で監査 detail に届いていない"


def test_usage_chat_carries_history_into_prompt(monkeypatch):
    """会話履歴がプロンプトへ渡ること（本文がサーバへ丸ごと永続化されるわけではなく、
    その場のプロンプト組み立てにだけ使われることの確認）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchhadm{sfx}", f"UcHAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    seen = {}
    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})

    def _capture(system, user, cfg):
        seen["user"] = user
        return json.dumps({"answer": "続きの回答"})
    monkeypatch.setattr(usage_chat, "_complete", _capture)

    r = admin.post("/admin/usage/chat", json={
        "question": "それを踏まえてどうすべき？",
        "history": [{"role": "user", "content": "先週のトークン量は？"},
                   {"role": "assistant", "content": "先週は1000トークンでした"}],
    })
    assert r.status_code == 200, r.text
    assert "先週のトークン量は？" in seen["user"]
    assert "先週は1000トークンでした" in seen["user"]
    assert "それを踏まえてどうすべき？" in seen["user"]


def test_usage_chat_provider_override_reaches_resolve_cfg_via_router(monkeypatch):
    """リクエストの `provider`（一時上書き）がルーター経由で実際に `_resolve_cfg` の判断に
    届き、専用設定より優先されることを end-to-end で確認する（`_resolve_cfg` 自体はスタブせず、
    実際のプロバイダ選択ロジックを経由させる）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchovradm{sfx}", f"UcOvrAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    # system_settings["usage_chat_provider"] は未設定（A7 も未設定なら既定は ollama になる）
    # だが、リクエストの provider="ollama" が（既定が何であれ）優先されることを確認する。
    monkeypatch.setattr("sherpa.keys.resolve_ollama_url",
                        lambda s, system_settings=None: "http://localhost:11434")
    monkeypatch.setattr("sherpa.llm.assert_ollama_url_allowed", lambda *a, **kw: None)
    monkeypatch.setattr("sherpa.model_catalog.resolve_model",
                        lambda provider, usage, user_settings, system_settings=None: "m")

    def _must_not_call_openai_key(*a, **kw):
        raise AssertionError("provider=ollama を上書き指定したのに openai のキー解決に進んだ")
    monkeypatch.setattr("sherpa.keys.resolve_api_key", _must_not_call_openai_key)

    seen = {}

    def _capture(system, user, cfg):
        seen["provider"] = cfg["provider"]
        return json.dumps({"answer": "回答"})
    monkeypatch.setattr(usage_chat, "_complete", _capture)

    r = admin.post("/admin/usage/chat", json={"question": "質問", "provider": "ollama"})
    assert r.status_code == 200, r.text
    assert seen["provider"] == "ollama"

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "success"
    assert rows[0]["detail"]["provider_override"] == "ollama"
    assert rows[0]["detail"]["provider_used"] == "ollama"


def test_usage_chat_llm_unavailable_returns_503_no_fallback(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchunadm{sfx}", f"UcUnAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _raise(system_settings, provider_override=None):
        raise usage_chat.LLMUnavailableError("未接続です")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _raise)

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 503, r.text

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["reason"] == "llm_unavailable"


def test_usage_chat_provider_call_failure_returns_502_no_fallback(monkeypatch):
    """プロバイダへの送信は行ったが失敗＝別プロバイダへ自動フォールバックせず明示エラー（502）。
    実送信を試みた時点で送信先は確定しているため、応答/監査の provider_used/endpoint_kind には
    その値（cfg の内容）が残る。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchfladm{sfx}", f"UcFlAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    calls = []
    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test", "endpoint_kind": "custom"})

    def _boom(system, user, cfg):
        calls.append(cfg["provider"])
        raise TimeoutError("upstream timeout")
    monkeypatch.setattr(usage_chat, "_complete", _boom)

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 502, r.text
    assert calls == ["openai"], "1系統のみ試行し、別プロバイダへフォールバックしないこと"
    body = r.json()
    assert body["provider_used"] == "openai"
    assert body["endpoint_kind"] == "custom"

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["reason"] == "llm_call_failed"
    assert rows[0]["detail"]["provider_used"] == "openai"
    assert rows[0]["detail"]["endpoint_kind"] == "custom"


def test_usage_chat_response_reflects_azure_endpoint_kind(monkeypatch):
    """openai 使用時、接続先が Azure 等なら応答の `endpoint_kind` にその値を載せる
    （画面が「OpenAI」ではなく「クラウド（OpenAI 互換）」と表示するための情報）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchazadm{sfx}", f"UcAzAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "my-deployment", "endpoint_kind": "azure"})
    monkeypatch.setattr(usage_chat, "_complete", lambda system, user, cfg: json.dumps({"answer": "回答"}))

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 200, r.text
    assert r.json() == {"answer": "回答", "provider_used": "openai", "endpoint_kind": "azure", "notes": []}

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "success"
    assert rows[0]["detail"]["endpoint_kind"] == "azure"


def test_usage_chat_late_guard_rejection_inside_complete_is_503_not_502(monkeypatch):
    """`_resolve_cfg` の事前チェックを通過した後、実送信（`_complete`）自体が
    `llm.PreflightRejected`（`complete_json` 内部の権威あるガードが実送信前に拒否したことを示す
    共通の例外基底）を投げた場合も、502（送信を試みたが失敗）に誤分類せず 503（未接続）にする。
    送信先（cfg）は既に確定しているため、応答/監査の provider_used にはその値が残る
    （`endpoint_kind` は cfg に無いキーなので `None`）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchlateadm{sfx}", f"UcLateAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    from sherpa import llm as _llm

    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "ollama", "url": "http://evil.example:1", "model": "m"})

    def _late_reject(system, user, cfg):
        raise _llm.SsrfBlocked("許可されていない接続先です")
    monkeypatch.setattr(usage_chat, "_complete", _late_reject)

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["provider_used"] == "ollama"
    assert body["endpoint_kind"] is None

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["reason"] == "llm_unavailable"
    assert rows[0]["detail"]["provider_used"] == "ollama"
    assert rows[0]["detail"]["endpoint_kind"] is None


def test_usage_chat_non_json_response_after_send_is_502_not_503(monkeypatch):
    """`_complete` が実際に送信した後、応答本文が JSON として解析できない
    （`JSONDecodeError` は `ValueError` 派生）場合は、実送信直前ガードの拒否（503・未計測）と
    型だけで混同せず 502（送信を試みたが失敗・計測あり）にする。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchnonjsonadm{sfx}", f"UcNonJsonAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})
    monkeypatch.setattr(usage_chat, "_complete", lambda system, user, cfg: "not json at all")

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["provider_used"] == "openai"
    assert body["endpoint_kind"] is None

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["reason"] == "llm_call_failed"


def test_usage_chat_openai_seed_blocked_returns_503_not_502_and_never_sends(monkeypatch):
    """OpenAI 接続先の起動時 env シードが未確定（`llm.assert_openai_io_allowed` が拒否）な場合、
    `_complete`（実送信）まで到達させず 503（未接続）にする——502（送信を試みたが失敗）に誤分類
    しない・未送信の呼び出しを計測しない、の両方を確認する。送信先（openai）は既に確定した
    後の拒否のため、応答/監査の provider_used/endpoint_kind にはその値が残る。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchseedadm{sfx}", f"UcSeedAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    # STAT-2: 既定選択（未設定時）は A7 連動のため、`cloud_provider` の全体設定
    # （共有テーブル・他テストの残留状態の影響を受けうる）に依存せず openai 分岐へ確実に
    # 入るよう、専用設定を明示的に openai へ固定してからテストする。共有テーブルなので、
    # 元の値を GET で退避し、以降のどの assert が失敗しても try/finally で必ず元へ戻す
    # （戻し忘れは他テスト/他セッションの usage_chat_provider を意図せず変えてしまう）。
    # 退避 GET 自体の成否も明示 assert する（黙って読めたと仮定すると、失敗時に不定な値へ
    # 復元してしまう）。
    r_orig = admin.get("/admin/settings")
    assert r_orig.status_code == 200, r_orig.text
    original = r_orig.json()["usage_chat"]["configured"]
    r_cfg = admin.put("/admin/settings", json={"usage_chat_provider": "openai"})
    assert r_cfg.status_code == 200, r_cfg.text
    try:
        monkeypatch.setattr("sherpa.keys.resolve_api_key",
                            lambda provider, s, system_settings=None, strict=False: "sk-real-key")
        # endpoint_kind は sys_s（openai_endpoint_kind/openai_base_url）に依存するため、共有
        # テーブルの残留状態に関わらず決定的にする。
        monkeypatch.setattr("sherpa.llm.openai_endpoint_kind", lambda system_settings=None: "openai")

        from sherpa import llm as _llm

        def _blocked():
            raise _llm.PreflightRejected("OpenAI 接続先の設定が未確定のため停止しています")
        monkeypatch.setattr("sherpa.llm.assert_openai_io_allowed", _blocked)

        def _must_not_call(*a, **kw):
            raise AssertionError("送信前に弾かれるべきで _complete に到達してはいけない")
        monkeypatch.setattr(usage_chat, "_complete", _must_not_call)

        r = admin.post("/admin/usage/chat", json={"question": "質問"})
        assert r.status_code == 503, r.text
        assert r.json() == {
            "detail": r.json()["detail"], "provider_used": "openai", "endpoint_kind": "openai"}

        rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
        assert rows and rows[0]["outcome"] == "failure"
        assert rows[0]["detail"]["reason"] == "llm_unavailable"
        assert rows[0]["detail"]["provider_used"] == "openai"
        assert rows[0]["detail"]["endpoint_kind"] == "openai"
    finally:
        # monkeypatch を明示的にここで戻す——PUT /admin/settings も応答生成の途中で
        # `vision_arm._openai_key()` 経由で `resolve_api_key` を呼ぶため、戻さないまま
        # 復元 PUT を実行すると、上の monkeypatch（フェイクのキー/接続先判定）がそのまま
        # 混入する（今は害の無い値に差し替えているだけで実害はないが、`_must_not_call` の
        # ような監視用スタブに変えた際に事故る・同種の罠は同じ形で塞ぐ）。
        monkeypatch.undo()
        # 共有テーブル（system_settings）を汚さない＝他テスト/他セッションへ影響させない。
        r_restore = admin.put("/admin/settings", json={"usage_chat_provider": original})
        assert r_restore.status_code == 200, r_restore.text


@pytest.mark.parametrize("body,reason", [
    ({"question": "   "}, "空の質問"),
    ({"question": "あ" * (usage_chat.QUESTION_MAX_LEN + 1)}, "長すぎる質問"),
    ({"question": "x", "history": [{"role": "user", "content": "x"}] * (usage_chat.HISTORY_MAX_ITEMS + 1)},
     "履歴が多すぎる"),
    ({"question": "x", "history": [{"role": "system", "content": "x"}]}, "履歴のroleが不正"),
    ({}, "質問フィールド自体が無い"),
    ({"question": "x", "history": [{"role": 123, "content": "x"}]}, "履歴のroleが数値（型不正）"),
    ({"question": "x", "history": "oops"}, "履歴が配列でない"),
    ({"question": "x", "history": [123]}, "履歴の要素がオブジェクトでない"),
    ({"question": 123}, "質問が数値（型不正）"),
    # 業務上限（20件）は超えるが、防御的上限（10000件）以内＝ハンドラへ到達し、業務上限判定で
    # 400 になる（防御的上限だけを見て自動 422 に落ちない）。
    ({"question": "x", "history": [{"role": "user", "content": "x"}] * 201}, "履歴201件"),
    # STAT-2: 空文字・空白のみの provider は「上書きなし」として黙って受理しない
    # （null/省略だけが「上書きなし」）。
    ({"question": "x", "provider": ""}, "providerが空文字"),
    ({"question": "x", "provider": "   "}, "providerが空白のみ"),
    ({"question": "x", "provider": "gemini"}, "providerが未対応の値"),
    ({"question": "x", "provider": 123}, "providerが数値（型不正）"),
])
def test_usage_chat_rejects_over_limit_input_with_400(monkeypatch, body, reason):
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchlimadm{sfx}", f"UcLimAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    # プロバイダへ到達しないこと（400 は LLM 呼び出し前に弾かれる）を明示するため、
    # 呼ばれたら失敗させるスタブにしておく。
    def _must_not_call(*a, **kw):
        raise AssertionError(f"LLM 呼び出しに到達してはいけない（{reason}）")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _must_not_call)

    r = admin.post("/admin/usage/chat", json=body)
    assert r.status_code == 400, f"{reason}: {r.text}"

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows, f"400（{reason}）でも admin.usage_chat_asked が監査されるべき"
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["status_code"] == 400


def test_usage_chat_invalid_provider_value_not_reflected_in_audit_or_response(monkeypatch):
    """不正な `provider` 値は監査の `provider_override` にも応答本文にも反射しない
    （`validate_provider_override` が例外を投げるより前に代入されないため、監査時点の値は
    常に初期値 `None` のまま・値そのものを反映するとログ/エラーへ利用者入力が混入する）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchbadprovadm{sfx}", f"UcBadProvAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _must_not_call(*a, **kw):
        raise AssertionError("不正な provider は LLM 呼び出しに到達してはいけない")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _must_not_call)

    marker = "秘密の識別子マーカーXYZ"
    r = admin.post("/admin/usage/chat", json={"question": "質問", "provider": marker})
    assert r.status_code == 400, r.text
    assert marker not in r.text, "不正な provider の値がエラー応答に反射している"

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["provider_override"] is None
    assert marker not in json.dumps(rows[0]["detail"]), "不正な provider の値が監査 detail に反射している"


def test_usage_chat_corrupted_stored_provider_returns_503_with_no_provider_used(monkeypatch):
    """専用設定の保存値自体が不正（API の検証を経由しない旧データ/手動編集）な場合、送信先が
    確定する前に拒否される——応答/監査の provider_used/endpoint_kind はどちらも `None` のまま
    （`_unavailable_invalid_provider_value` は送信先を知らないため）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchcorruptadm{sfx}", f"UcCorruptAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    # 退避 GET・復元後の再読取のどちらも成否を明示 assert する（黙って読めた/戻せたと仮定すると、
    # 退避自体が失敗していた場合に別の値へ復元してしまう）。
    r_orig = admin.get("/admin/settings")
    assert r_orig.status_code == 200, r_orig.text
    original = r_orig.json()["usage_chat"]["configured"]
    # `_validate_usage_chat_provider` を経由しない書込み経路を模す（PUT 経由では常に検証される
    # ため再現できない）。
    store.set_system_settings(admin_uid, {"usage_chat_provider": "gemini"})
    try:
        def _must_not_call(*a, **kw):
            raise AssertionError("送信先が決まる前に弾かれるべきで実送信の準備へ進んではいけない")
        monkeypatch.setattr("sherpa.keys.resolve_api_key", _must_not_call)
        monkeypatch.setattr("sherpa.keys.resolve_ollama_url", _must_not_call)

        r = admin.post("/admin/usage/chat", json={"question": "質問"})
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["provider_used"] is None
        assert body["endpoint_kind"] is None

        rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
        assert rows and rows[0]["outcome"] == "failure"
        assert rows[0]["detail"]["reason"] == "llm_unavailable"
        assert rows[0]["detail"]["provider_used"] is None
        assert rows[0]["detail"]["endpoint_kind"] is None
    finally:
        # `resolve_api_key`/`resolve_ollama_url` の monkeypatch は明示的にここで戻す
        # （pytest の自動 undo は test 関数を抜けるまで効かないため、このままだと直後の
        # 復元用 GET /admin/settings が `vision_arm._openai_key()` 経由で
        # `resolve_api_key` を呼んだ時に `_must_not_call` へ衝突し 500 になる）。
        monkeypatch.undo()
        store.set_system_settings(admin_uid, {"usage_chat_provider": original})
        r_restored = admin.get("/admin/settings")
        assert r_restored.status_code == 200, r_restored.text
        assert r_restored.json()["usage_chat"]["configured"] == original


def test_usage_chat_extreme_history_count_still_422_unaudited_by_design(monkeypatch):
    """会話履歴の件数上限（`_HISTORY_HARD_CAP`=10000）は、業務上限（20件）とは別の、実務上まず
    起こり得ない極端な乱用だけを弾く防御的な上限として残す（DoS 対策）。この上限を超える送信は、
    認証チェックは通るが `_resolve_cfg`/`answer_usage_question`（実送信の経路）へは到達する前に
    422 になる——業務上限超過（400・監査あり）とは意図的に異なる扱い（監査も経由しない）で
    あることを明示するための固定テスト。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchxtrmadm{sfx}", f"UcXtrmAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _must_not_call(*a, **kw):
        raise AssertionError("極端な件数超過は 422 で止まり、_resolve_cfg 以降へ到達してはいけない")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _must_not_call)

    body = {"question": "x", "history": [{"role": "user", "content": "x"}] * 10_001}
    r = admin.post("/admin/usage/chat", json=body)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("raw_body,reason", [
    (b"[]", "トップレベルが配列"),
    (b'"oops"', "トップレベルが文字列"),
    (b"123", "トップレベルが数値"),
    (b"null", "トップレベルが JSON null"),
    (b"", "本文が空（何も送信しない）"),
])
def test_usage_chat_non_object_top_level_body_is_400_and_audited(monkeypatch, raw_body, reason):
    """リクエスト本文のトップレベルがオブジェクトでない（配列・文字列・数値・`null`・空）場合、
    型を固定しない body（`Any`）で受け、認証チェック後に 400（監査あり）として拒否する——他の
    型不正な入力（`test_usage_chat_rejects_over_limit_input_with_400`）と同じ扱いに揃える。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchnonobjadm{sfx}", f"UcNonObjAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _must_not_call(*a, **kw):
        raise AssertionError(f"トップレベルがオブジェクトでない body（{reason}）は LLM 呼び出しに"
                             "到達してはいけない")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _must_not_call)

    r = admin.post("/admin/usage/chat", content=raw_body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400, f"{reason}: {r.text}"

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows, f"{reason}: トップレベルがオブジェクトでない body でも admin.usage_chat_asked が監査されるべき"
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["status_code"] == 400


def test_usage_chat_oversized_body_is_413_and_unaudited(monkeypatch):
    """`_HISTORY_HARD_CAP`（10000件）判定に到達する前でも、単一要素が極端に長い等で
    本文自体が巨大になりうる（各要素の長さには事前の上限が無い）。本文サイズ上限を超える送信は
    413（固定文言・値は反射しない）で打ち切り、`_HISTORY_HARD_CAP` 超過と同じく監査を経由しない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchbigadm{sfx}", f"UcBigAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _must_not_call(*a, **kw):
        raise AssertionError("本文サイズ超過は LLM 呼び出しに到達してはいけない")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _must_not_call)

    huge = "あ" * usage_chat.QUESTION_MAX_LEN   # 1件あたり utf-8 で約6000バイト
    body = {"question": "x",
            "history": [{"role": "user", "content": huge}] * 200}   # 合計で上限（1MiB）を優に超える
    r = admin.post("/admin/usage/chat", json=body)
    assert r.status_code == 413, r.text

    rows_before = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows_before == [], "本文サイズ超過は監査を経由してはいけない（_HISTORY_HARD_CAP と同じ扱い）"


def test_usage_chat_malformed_json_body_is_400_and_audited(monkeypatch):
    """JSON として解析できない本文も、型不正な入力の一種として 400（監査あり）に
    する（トップレベルがオブジェクトでない場合と同じ扱い）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchbadjsonadm{sfx}", f"UcBadJsonAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _must_not_call(*a, **kw):
        raise AssertionError("不正な JSON 本文は LLM 呼び出しに到達してはいけない")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _must_not_call)

    r = admin.post("/admin/usage/chat", content=b"{not valid json",
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["status_code"] == 400


def test_usage_chat_bom_prefixed_body_is_400_and_audited(monkeypatch):
    """UTF-8 BOM 付き本文は明示的に拒否する（`json.loads(bytes)` の暗黙エンコーディング推測
    （RFC 8259 sniffing）を経由させない・本文は UTF-8・BOM なしとしてのみ受理する契約）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchbomadm{sfx}", f"UcBomAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _must_not_call(*a, **kw):
        raise AssertionError("BOM 付き本文は LLM 呼び出しに到達してはいけない")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _must_not_call)

    body = b"\xef\xbb\xbf" + json.dumps({"question": "x"}).encode("utf-8")
    r = admin.post("/admin/usage/chat", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["status_code"] == 400


def test_usage_chat_invalid_utf8_body_is_400_not_500(monkeypatch):
    """UTF-8 として不正なバイト列を含む本文は、`UnicodeDecodeError`（コーデック内部の詳細な文言）を
    そのまま漏らさず、固定文言の 400（監査あり）にする。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchbadutf8adm{sfx}", f"UcBadUtf8Adm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _must_not_call(*a, **kw):
        raise AssertionError("不正な UTF-8 本文は LLM 呼び出しに到達してはいけない")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _must_not_call)

    body = b'{"question": "\xff\xfe not valid utf-8"}'
    r = admin.post("/admin/usage/chat", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    assert "codec" not in r.text, "UnicodeDecodeError の内部文言をそのまま漏らしている"

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["status_code"] == 400


def test_usage_chat_deeply_nested_json_body_is_400_not_500(monkeypatch):
    """極端に深くネストした JSON（`json.loads` が `RecursionError` を送出しうる深さ）も、
    想定外の 500 にせず固定文言の 400（監査あり）にする。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchdeepadm{sfx}", f"UcDeepAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _must_not_call(*a, **kw):
        raise AssertionError("深すぎるネストの本文は LLM 呼び出しに到達してはいけない")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _must_not_call)

    depth = 10_000   # 既定の再帰上限（1000）に対し、この深さから json.loads が RecursionError
                     # を送出する（本文サイズ上限 1MiB には十分収まる）。
    body = ("[" * depth + "]" * depth).encode("utf-8")
    r = admin.post("/admin/usage/chat", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["status_code"] == 400


def test_usage_chat_success_writes_pending_row_before_send(monkeypatch):
    """fail-closed（外部送信の監査もれ防止）: 実送信（`answer_usage_question`）の前に
    outcome=pending の監査行を確保する。成功時は pending 行と結果行（success）の2行が、
    同じ request_id で対応付いて残る。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchpendadm{sfx}", f"UcPendAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    _stub_ok(monkeypatch, answer="回答")

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 200, r.text

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert len(rows) >= 2, f"pending 行と結果行の2行が残るべき: {rows}"
    assert rows[0]["outcome"] == "success"   # ORDER BY created_at DESC, id DESC＝最新が先頭
    pending_rows = [r for r in rows if r["outcome"] == "pending"]
    assert pending_rows, "送信前の pending 行が見当たらない"
    assert pending_rows[0]["request_id"] == rows[0]["request_id"], (
        "pending 行と結果行は同じ request_id で対応付くべき")


def test_usage_chat_pending_audit_write_failure_blocks_send(monkeypatch):
    """fail-closed（外部送信の監査もれ防止）: 実送信前の pending 監査行の書き込みに失敗したら、
    実送信（`_resolve_cfg`/`_complete`）へは一切到達せず 500 を返す（未監査のまま統計データが
    外部 AI へ渡ることを防ぐ）。

    `store.audit` を無条件に失敗させる構成だと、万一 `_resolve_cfg` が誤って呼ばれてしまっても
    （本来あってはならない）その後の結果監査の書き込みも同じ理由で失敗し、結局 500 が返る
    ため、テストが「正しく送信をブロックできた」ことと「送信してしまったが監査にも失敗した」
    ことを区別できない（見かけ上どちらも 500）。ここでは pending 行の書き込み（1回目の
    `store.audit` 呼び出し）だけを失敗させ、それ以降の呼び出しは成功させる——`_resolve_cfg` が
    誤って呼ばれた場合、その中の `_must_not_call` が送出する素の `AssertionError` は、以降の
    監査書き込みが成功するために `_audit` の fail-closed 500 に飲み込まれず、
    `raise_server_exceptions`（既定 True）の TestClient を通じてテストへそのまま伝播する
    （`admin`＝`_login` が返す `raise_server_exceptions=False` のクライアントは使わない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchpendfailadm{sfx}", f"UcPendFailAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")

    strict = TestClient(app)   # raise_server_exceptions は既定 True（未捕捉の例外はテストへ伝播）
    r_login = strict.post("/auth/login", json={"username": admin_uid, "password": admin_pw})
    assert r_login.status_code == 200, r_login.text

    def _must_not_call(*a, **kw):
        raise AssertionError("pending 監査行の書き込みに失敗したら実送信へ到達してはいけない")
    monkeypatch.setattr(usage_chat, "_resolve_cfg", _must_not_call)

    calls = {"n": 0}

    def _boom_first_call_only(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("DB 一時障害（テスト用・pending 行のみ失敗させる）")
        return None   # 2回目以降（結果行の書き込み）は成功させる
    monkeypatch.setattr(store, "audit", _boom_first_call_only)

    r = strict.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 500, r.text
    assert calls["n"] == 1, "pending 行の1回だけで止まるべき（実送信へ進んで結果監査まで発生していない）"


def test_usage_chat_pending_row_exists_at_complete_call_time(monkeypatch):
    """fail-closed の順序性そのものを直接固定する: `_complete`（実送信）が実際に呼ばれる
    **時点**で、pending 監査行が既に DB にコミット済みで見える（`_complete` 呼び出しの後で
    ようやく pending 行が書かれる、といった順序の崩れが無いことの確認・両者とも成功する
    フローでも固定する）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchpendordadm{sfx}", f"UcPendOrdAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})

    seen = {}

    def _capture(system, user, cfg):
        rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
        seen["pending_rows_at_complete_time"] = [row for row in rows if row["outcome"] == "pending"]
        return json.dumps({"answer": "回答"})
    monkeypatch.setattr(usage_chat, "_complete", _capture)

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 200, r.text
    assert seen.get("pending_rows_at_complete_time"), (
        "_complete 実行時点で pending 行が DB に見えているべき")


def test_usage_chat_offloads_blocking_work_off_the_event_loop_thread(monkeypatch):
    """認証・監査・本処理（`_run_answer`／`_complete`）は `run_in_threadpool` により event loop の
    スレッドとは別スレッドで実行される（単一 worker 構成で、この呼び出し中に他の API/health が
    応答不能にならないための必須条件）ことを、実行スレッドの比較で直接固定する。認証
    （`_current_user`）・監査（`store.audit`）・本処理（`_complete`）の3系統それぞれについて
    固定する（`_run_answer` 一箇所だけでは、他の箇所が event loop 直実行に戻る回帰を検出できない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchthreadadm{sfx}", f"UcThreadAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    real_read = audit_usage._read_capped_json_body
    event_loop_thread: dict = {}

    async def _wrapped_read(request):
        # 本文読み込み（`_read_capped_json_body`）は唯一 event loop 上で直接動く非同期処理
        # （`await request.stream()` を要するため）＝実行スレッドを確実に観測できる基準点にする。
        event_loop_thread["id"] = threading.get_ident()
        return await real_read(request)
    monkeypatch.setattr(audit_usage, "_read_capped_json_body", _wrapped_read)

    real_current_user = audit_usage._current_user
    authn_threads: set = set()

    def _wrapped_current_user(request, **kw):
        authn_threads.add(threading.get_ident())
        return real_current_user(request, **kw)
    monkeypatch.setattr(audit_usage, "_current_user", _wrapped_current_user)

    real_audit = store.audit
    audit_threads: set = set()

    def _wrapped_audit(*a, **kw):
        audit_threads.add(threading.get_ident())
        return real_audit(*a, **kw)
    monkeypatch.setattr(store, "audit", _wrapped_audit)

    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})

    worker_threads: set = set()

    def _capture(system, user, cfg):
        worker_threads.add(threading.get_ident())
        return json.dumps({"answer": "回答"})
    monkeypatch.setattr(usage_chat, "_complete", _capture)

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 200, r.text
    assert event_loop_thread.get("id") is not None, "_read_capped_json_body が呼ばれていない"
    assert authn_threads, "_current_user が呼ばれていない"
    assert audit_threads, "store.audit が呼ばれていない"
    assert worker_threads, "_complete が呼ばれていない"
    assert event_loop_thread["id"] not in authn_threads, (
        "認証が event loop のスレッドで実行された＝run_in_threadpool を経由していない")
    assert event_loop_thread["id"] not in audit_threads, (
        "監査書き込みが event loop のスレッドで実行された＝run_in_threadpool を経由していない")
    assert event_loop_thread["id"] not in worker_threads, (
        "本処理が event loop のスレッドで実行された＝run_in_threadpool を経由していない")


def test_usage_chat_post_send_audit_failure_uses_duplicate_send_warning_wording(monkeypatch):
    """実送信（pending 記録後）の結果監査だけを書き込み失敗させた場合、送信前の失敗
    （「AI へは送信していません」）とは異なる固定文言（送信は完了している可能性がある・再試行は
    重複送信になり得る）になることを、実際の応答本文で文言レベルに固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchpostfailadm{sfx}", f"UcPostFailAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    _stub_ok(monkeypatch, answer="回答")

    real_audit = store.audit
    calls = {"n": 0}

    def _fail_after_pending(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_audit(*a, **kw)   # pending 行は実際に成功させる
        raise RuntimeError("DB 一時障害（テスト用・結果行のみ失敗させる）")
    monkeypatch.setattr(store, "audit", _fail_after_pending)

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert "送信は完了している可能性があります" in detail
    assert "重複送信になり得ます" in detail
    assert "AI へは送信していません" not in detail, "送信前の失敗と同じ文言になっている"
    assert calls["n"] == 2, "pending 行1回＋結果行1回（失敗）の合計2回のはず"


def test_usage_chat_explicit_null_history_records_zero_length_in_audit(monkeypatch):
    """`history: null` は空履歴として受理する（`validate_request` の既存契約）。
    受理した以上、監査の `history_len` は「型不正で不明」を示す `null` ではなく、実際に受理された
    件数である `0` を記録する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchnullhistadm{sfx}", f"UcNullHistAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    _stub_ok(monkeypatch, answer="回答")

    r = admin.post("/admin/usage/chat", json={"question": "質問", "history": None})
    assert r.status_code == 200, r.text

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "success"
    assert rows[0]["detail"]["history_len"] == 0


def test_usage_chat_overlong_history_item_is_truncated_not_rejected(monkeypatch):
    """履歴1件が長すぎる（例: 前ターンの長い正常な AI 回答）場合は 400 で拒否せず、切り詰めて
    受理する（拒否だと次の質問から毎回 400 になり、利用者の再読込なしには回復できない）。
    切り詰めは無言では行わず末尾に省略の印を付ける。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchtrncadm{sfx}", f"UcTrncAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    seen = {}
    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})

    def _capture(system, user, cfg):
        seen["user"] = user
        return json.dumps({"answer": "続きの回答"})
    monkeypatch.setattr(usage_chat, "_complete", _capture)

    long_answer = "あ" * (usage_chat.HISTORY_ITEM_MAX_LEN + 500)
    r = admin.post("/admin/usage/chat", json={
        "question": "続きは？",
        "history": [{"role": "user", "content": "先週の状況は？"},
                   {"role": "assistant", "content": long_answer}],
    })
    assert r.status_code == 200, r.text
    # 送信されたプロンプトに含まれる履歴は切り詰め済み（元の全長のままではない）・省略の印付き。
    assert long_answer not in seen["user"]
    assert usage_chat._TRUNCATION_SUFFIX in seen["user"]

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "success"
    assert rows[0]["detail"]["history_truncated"] is True


def test_usage_chat_consecutive_turns_with_long_answer_do_not_get_stuck(monkeypatch):
    """連続ターンの確認: 1ターン目で長い正常回答を得て、それをそのまま2ターン目の history に
    積んでも、2ターン目が 400 で失敗し続けない（利用者の再読込に頼らない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchseqadm{sfx}", f"UcSeqAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})

    long_answer = "とても長い回答です。" * 500   # HISTORY_ITEM_MAX_LEN（4000）を超える
    assert len(long_answer) > usage_chat.HISTORY_ITEM_MAX_LEN
    answers = iter([long_answer, "2ターン目の回答"])
    monkeypatch.setattr(usage_chat, "_complete",
                        lambda system, user, cfg: json.dumps({"answer": next(answers)}))

    r1 = admin.post("/admin/usage/chat", json={"question": "1ターン目の質問"})
    assert r1.status_code == 200, r1.text
    turn1_answer = r1.json()["answer"]
    assert turn1_answer == long_answer   # 表示側は切り詰めない（切り詰めるのは送信する history だけ）

    r2 = admin.post("/admin/usage/chat", json={
        "question": "2ターン目の質問",
        "history": [{"role": "user", "content": "1ターン目の質問"},
                   {"role": "assistant", "content": turn1_answer}],
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["answer"] == "2ターン目の回答"


def test_usage_chat_client_truncated_history_is_flagged_in_audit(monkeypatch):
    """web/usage.js::ucClip はクライアント側で上限ちょうど（省略印付き）まで切り詰めてから
    次のターンへ送る。この時点の長さはもう上限を超えていないため、サーバの素朴な長さ比較だけ
    では「切り詰めが起きた」ことを見落とす。末尾の省略印でも判定することで、監査
    （history_truncated）にクライアント側の切り詰めが正しく残ることを確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchuiadm{sfx}", f"UcUiAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})
    monkeypatch.setattr(usage_chat, "_complete",
                        lambda system, user, cfg: json.dumps({"answer": "2ターン目の回答"}))

    # web/usage.js::ucClip が実際に生成する形を模す（上限ちょうど・末尾に省略印）。
    client_clipped = ("あ" * (usage_chat.HISTORY_ITEM_MAX_LEN - len(usage_chat._TRUNCATION_SUFFIX))
                      + usage_chat._TRUNCATION_SUFFIX)
    assert len(client_clipped) == usage_chat.HISTORY_ITEM_MAX_LEN

    r = admin.post("/admin/usage/chat", json={
        "question": "2ターン目の質問",
        "history": [{"role": "user", "content": "1ターン目の質問"},
                   {"role": "assistant", "content": client_clipped}],
    })
    assert r.status_code == 200, r.text

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "success"
    assert rows[0]["detail"]["history_truncated"] is True


def test_usage_chat_max_accumulated_history_and_large_context_still_succeeds(monkeypatch):
    """history を最大蓄積（HISTORY_MAX_ITEMS件・各要素ほぼ上限の長さ）＋統計データも大きめの
    状態で送っても、プロンプト総量上限により古い完全ターンを落として成功する（502 にならない）。
    502 になると、失敗した質問は history に積まれない一方で既に蓄積された history はそのまま
    残るため、次の質問からも毎回同じ理由で失敗し続ける（利用者が再送ループに陥る）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchbudadm{sfx}", f"UcBudAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})
    # 統計データ自体も大きめにしておく（現実的な最大蓄積の想定・DB の実データ量に依存しない）。
    monkeypatch.setattr(usage_chat, "_compact_stats_context", lambda stats: ("x" * 40_000, False))

    seen = {}
    def _capture(system, user, cfg):
        seen["user"] = user
        return json.dumps({"answer": "回答"})
    monkeypatch.setattr(usage_chat, "_complete", _capture)

    # 最大蓄積: HISTORY_MAX_ITEMS 件（10ターン）、各要素はほぼ上限（4000字）まで積む。
    history = []
    for i in range(usage_chat.HISTORY_MAX_ITEMS // 2):
        history.append({"role": "user",
                        "content": f"turn{i}:" + ("あ" * (usage_chat.HISTORY_ITEM_MAX_LEN - 10))})
        history.append({"role": "assistant",
                        "content": f"turn{i}:" + ("い" * (usage_chat.HISTORY_ITEM_MAX_LEN - 10))})

    r = admin.post("/admin/usage/chat", json={"question": "最後の質問", "history": history})
    assert r.status_code == 200, r.text
    assert len(seen["user"]) <= usage_chat._PROMPT_MAX_CHARS
    assert "最後の質問" in seen["user"]
    assert "turn0:" not in seen["user"], "最も古いターンは落ちているはず"


def test_usage_chat_question_padding_bypass_is_trimmed_before_send(monkeypatch):
    """上限チェックは trim 後の文字列に対して行われるが、送信も trim 後の値でなければ、
    前後の空白だけで巨大化させた質問が上限チェックを迂回して丸ごと送信されてしまう。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchpadadm{sfx}", f"UcPadAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    seen = {}
    monkeypatch.setattr(usage_chat, "_resolve_cfg", lambda system_settings, provider_override=None: {
        "provider": "openai", "key": "x", "model": "gpt-test"})

    def _capture(system, user, cfg):
        seen["user"] = user
        return json.dumps({"answer": "回答"})
    monkeypatch.setattr(usage_chat, "_complete", _capture)

    padded_question = (" " * 100_000) + "短い質問" + (" " * 100_000)
    r = admin.post("/admin/usage/chat", json={"question": padded_question})
    assert r.status_code == 200, r.text
    assert len(seen["user"]) < 100_000, "パディングを含む生の質問がそのまま送信されている"
    assert "短い質問" in seen["user"]


def test_usage_chat_unexpected_error_in_answer_returns_500_and_audits(monkeypatch):
    """LLMUnavailableError/LLMCallFailedError 以外の想定外の例外（本処理側）も、
    admin.usage_chat_asked の監査を欠かさない（結果コード付き・盲点を作らない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchboomadm{sfx}", f"UcBoomAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _boom(question, history, *, system_settings, user_id=None, provider_override=None):
        raise RuntimeError("想定外のバグ")
    monkeypatch.setattr(usage_chat, "answer_usage_question", _boom)

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 500, r.text

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["reason"] == "unexpected_error"
    assert rows[0]["detail"]["status_code"] == 500


def test_usage_chat_unexpected_error_in_validation_returns_500_and_audits(monkeypatch):
    """本処理側だけでなく、検証側（`validate_request`）が `ValueError` 以外の想定外の例外を
    投げた場合も 500 になり、かつ監査される（検証用・本処理用それぞれの try が
    `except Exception:` を持つため、どちらの段で起きた想定外の例外も監査してから 500 になる）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchvboomadm{sfx}", f"UcVBoomAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _boom(question, history):
        raise RuntimeError("検証側の想定外のバグ")
    monkeypatch.setattr(usage_chat, "validate_request", _boom)

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 500, r.text

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["reason"] == "unexpected_error"
    assert rows[0]["detail"]["status_code"] == 500


def test_usage_chat_value_error_from_main_processing_is_500_not_400(monkeypatch):
    """`ValueError` の捕捉は検証段階（`validate_request`）専用にする。本処理
    （`answer_usage_question`・設定取得）の内部で何らかの理由で `ValueError` が発生しても、
    「入力検証エラー」（400・例外文をそのままクライアントへ返す）に誤分類せず 500 にする
    （本処理側の内部エラーはクライアントの入力の問題ではない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"uchveadm{sfx}", f"UcVeAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    def _boom(question, history, *, system_settings, user_id=None, provider_override=None):
        raise ValueError("内部の想定外のバグ（本処理側）")
    monkeypatch.setattr(usage_chat, "answer_usage_question", _boom)

    r = admin.post("/admin/usage/chat", json={"question": "質問"})
    assert r.status_code == 500, r.text
    assert "内部の想定外のバグ" not in r.text, "本処理側の内部例外文がクライアントへ漏れている"

    rows = store.list_audit(actor=admin_uid, action="admin.usage_chat_asked", limit=5)
    assert rows and rows[0]["outcome"] == "failure"
    assert rows[0]["detail"]["reason"] == "unexpected_error"
    assert rows[0]["detail"]["status_code"] == 500
