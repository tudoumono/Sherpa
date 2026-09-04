"""全体設定 API（2026-07-08-設定分離とUI整備.md S1）テスト。

- GET/PUT /admin/settings: admin 専用（非 admin 403・未ログイン 401）・検証（arms/legacy_backend）。
- 優先順 system_settings > env > 既定 の実効反映（office_md.convertible_exts）。
- 監査（system_settings.updated・severity=warning）・fail-closed（監査失敗で変更を取り消し 500）。
- null で未設定へ戻す（既定/env へフォールバック）。

要 Postgres。DB 不可は SKIP。system_settings は全体（1 世界共有）なので各テスト前後で全消去する。
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app
from sherpa.ingest import arms, office_md


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _clear_system_settings() -> None:
    """全体設定を全消去（テスト分離・全体 KV なので per-test で clean slate にする）。"""
    try:
        with store._connect() as c:
            c.execute("DELETE FROM system_settings")
        store._invalidate_system_settings_cache()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_system_settings():
    _clear_system_settings()
    yield
    _clear_system_settings()


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@sys.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _admin_client():
    sfx = _sfx()
    uid, pw = f"sysadm{sfx}", f"SysAdm{sfx}"
    _mk_user(uid, pw, role="admin")
    return _login(uid, pw), uid


# ===== 認可 =====

def test_admin_settings_gates():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"sysusr{sfx}", f"SysUsr{sfx}"
    _mk_user(uid, pw, role="user")

    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get("/admin/settings").status_code == 401
    assert anon.put("/admin/settings", json={"legacy_backend": "libreoffice"}).status_code == 401

    u = _login(uid, pw)
    assert u.get("/admin/settings").status_code == 403
    assert u.put("/admin/settings", json={"legacy_backend": "libreoffice"}).status_code == 403


def test_admin_settings_get_shape():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.get("/admin/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    # 未設定なら configured は None・実効は env/既定（既知アームは ooxml/pdf_text/markitdown/markitdown_ocr）。
    # tesseract 直の ocr は撤去済み（2026-07-08）。
    assert body["arms"]["known"] == arms.known_arm_names()
    # MD化強化（2026-08-15 移植）: markitdown 系アームは撤去し、画像系は vision へ一本化した。
    assert set(body["arms"]["known"]) == {"ooxml", "pdf_text", "vision"}
    assert body["arms"]["configured"] is None
    assert set(body["arms"]["enabled"]) == set(arms.env_default_arm_names())
    # 各アームの実効可用性（未導入案内用）。ooxml は常時利用可・markitdown/markitdown_ocr は環境依存（bool）。
    avail = body["arms"]["available"]
    assert set(avail) == set(arms.known_arm_names()) and avail["ooxml"] is True
    assert all(isinstance(v, bool) for v in avail.values())
    # 金額系（token_prices/usd_jpy）は撤去済み＝ビューに含まれない。
    assert "token_prices" not in body and "usd_jpy" not in body
    # W0/W1: 旧形式変換バックエンド（未設定＝env/既定・soffice/office_com の検出状態を含む）。
    lb = body["legacy_backend"]
    assert lb["configured"] is None
    assert lb["effective"] in ("none", "libreoffice", "office_com")   # env 未設定なら none
    assert "none" in lb["options"] and "libreoffice" in lb["options"]
    assert "office_com" in lb["options"]                  # W1 で選択肢に追加
    assert isinstance(lb["libreoffice"]["available"], bool)
    # W1/W2': office_com ブロック（URL 未設定 と 不達 を区別する configured_url・動作形態 mode・direct 検出
    # 状態 powershell・到達可否・versions）。conftest が SHERPA_POWERSHELL_BIN を無効パスに固定するため、
    # URL 未設定のこの環境では mode="unavailable"（direct も検出しない）。
    assert isinstance(lb["office_com"]["configured_url"], bool)
    assert lb["office_com"]["mode"] in ("direct", "http", "unavailable")
    assert isinstance(lb["office_com"]["powershell"], bool)
    assert isinstance(lb["office_com"]["available"], bool)
    assert "versions" in lb["office_com"]                 # 不達なら None
    # ⑤（feedback-batch-2026-07-08）: 視覚読み取り（markitdown_ocr）の VLM 設定ブロック。
    vlm = body["vlm"]
    assert vlm["configured"] is None                      # 未設定
    assert vlm["effective"]["provider"] in ("ollama", "openai")
    assert vlm["effective"]["cloud_allowed"] is False     # 既定は必ず false
    assert vlm["default"]["cloud_allowed"] is False       # env/既定も cloud は常に false
    assert vlm["providers"] == ["ollama", "openai"]
    assert isinstance(vlm["available"], bool) and isinstance(vlm["openai_key_present"], bool)
    # R1b（Codex ネイティブ resume・決定5）: 未設定は configured=None・effective=0（無制限）。
    csr = body["codex_session_retention_days"]
    assert csr["configured"] is None
    assert csr["effective"] == 0
    # STAT-2: 利用統計チャット専用の AI 選択。未設定（configured=None）時の既定は A7
    # （`cloud_provider`）連動——A7 が明示的に openai の時だけ openai・それ以外（この環境の
    # ように A7 も未設定の場合を含む）は ollama。
    uc = body["usage_chat"]
    assert uc["configured"] is None
    assert uc["effective"] == "ollama"
    assert uc["default"] == "ollama"
    assert uc["providers"] == ["openai", "ollama"]
    # FBK-1 RV1（2026-09-01・境界回帰#2）: cloud_provider を一度も PUT していない構成では
    # provider_raw が null（`provider` は既定込みの openai）。
    assert body["cloud"]["provider"] == "openai"
    assert body["cloud"]["provider_raw"] is None


def test_admin_settings_cloud_provider_raw_persists_even_when_equal_to_default():
    """FBK-1 RV1（境界回帰#2）: 初期表示と同じ値（openai）を明示的に PUT しても、生の保存値が
    残ることを固定する——`provider`（既定込みの実効値）だけでは「一度も選んでいない」と
    「明示的に openai を選んだ」を区別できず、A7 の fail-loud 判定（`keys.
    cloud_provider_explicitly_selected`）が効かなくなる事故を防ぐ。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    before = admin.get("/admin/settings").json()
    assert before["cloud"]["provider_raw"] is None   # 前提: 未選択
    r = admin.put("/admin/settings", json={"cloud_provider": "openai"})
    assert r.status_code == 200, r.text
    assert r.json()["cloud"]["provider"] == "openai"
    assert r.json()["cloud"]["provider_raw"] == "openai"   # 既定と同値でも raw は残る
    after = admin.get("/admin/settings").json()
    assert after["cloud"]["provider_raw"] == "openai"


# ===== 検証（422）=====

def test_admin_settings_validation_errors():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    # 未知アーム名
    assert admin.put("/admin/settings", json={"arms_enabled": ["bogus-arm"]}).status_code == 422
    # legacy_backend（W0/W1）: 未知値・非文字列は 422（office_com は W1 で許可＝別テストで 200 を確認）。
    assert admin.put("/admin/settings", json={"legacy_backend": "bogus"}).status_code == 422
    assert admin.put("/admin/settings", json={"legacy_backend": 123}).status_code == 422
    # ⑤ vlm: 非オブジェクト・未知 provider・空 model・非 bool cloud・未知キーは 422。
    assert admin.put("/admin/settings", json={"vlm": "notdict"}).status_code == 422
    assert admin.put("/admin/settings", json={"vlm": {"provider": "gemini"}}).status_code == 422
    assert admin.put("/admin/settings", json={"vlm": {"model": ""}}).status_code == 422
    assert admin.put("/admin/settings", json={"vlm": {"cloud_allowed": "yes"}}).status_code == 422
    assert admin.put("/admin/settings", json={"vlm": {"bogus": 1}}).status_code == 422
    # R1b: codex_session_retention_days は0以上の整数のみ（負値・非整数値・非数値文字列は 422）。
    assert admin.put("/admin/settings", json={"codex_session_retention_days": -1}).status_code == 422
    assert admin.put("/admin/settings", json={"codex_session_retention_days": 1.5}).status_code == 422
    assert admin.put("/admin/settings", json={"codex_session_retention_days": "abc"}).status_code == 422
    # RV再検証 LOW-5（2026-07-15）: `StrictInt` で bool/数値文字列の暗黙変換も拒否する
    # （素の `int` フィールドだと pydantic の緩い型強制で "14"→14／True→1 と黙って受理されてしまう）。
    assert admin.put("/admin/settings", json={"codex_session_retention_days": True}).status_code == 422
    assert admin.put("/admin/settings", json={"codex_session_retention_days": False}).status_code == 422
    assert admin.put("/admin/settings", json={"codex_session_retention_days": "14"}).status_code == 422
    # STAT-2: usage_chat_provider は openai/ollama のみ（cloud_provider とは別の選択肢集合＝
    # gemini/bedrock は本機能の対象外）。非文字列も 422。
    assert admin.put("/admin/settings", json={"usage_chat_provider": "gemini"}).status_code == 422
    assert admin.put("/admin/settings", json={"usage_chat_provider": "bedrock"}).status_code == 422
    assert admin.put("/admin/settings", json={"usage_chat_provider": 123}).status_code == 422
    # 空文字は「未設定へ戻す」として黙って受理しない（null のみ受理）。
    assert admin.put("/admin/settings", json={"usage_chat_provider": ""}).status_code == 422
    assert admin.put("/admin/settings", json={"usage_chat_provider": "   "}).status_code == 422


def test_admin_settings_usage_chat_provider_round_trip():
    """STAT-2: 保存（openai|ollama）→ GET 反映 → null で既定（A7 連動＝この環境は未設定なので
    ollama）へ戻る、の一往復を固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()

    r = admin.put("/admin/settings", json={"usage_chat_provider": "ollama"})
    assert r.status_code == 200, r.text
    assert r.json()["usage_chat"] == {
        "configured": "ollama", "effective": "ollama", "default": "ollama",
        "providers": ["openai", "ollama"]}

    r_get = admin.get("/admin/settings")
    assert r_get.json()["usage_chat"]["configured"] == "ollama"
    assert r_get.json()["usage_chat"]["effective"] == "ollama"

    r_reset = admin.put("/admin/settings", json={"usage_chat_provider": None})
    assert r_reset.status_code == 200, r_reset.text
    assert r_reset.json()["usage_chat"] == {
        "configured": None, "effective": "ollama", "default": "ollama",
        "providers": ["openai", "ollama"]}


def test_admin_settings_usage_chat_provider_default_follows_a7_openai_selection():
    """A7（`cloud_provider`）を明示的に openai へ保存すると、未設定の usage_chat_provider
    の既定が openai になる。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()

    r = admin.put("/admin/settings", json={"cloud_provider": "openai"})
    assert r.status_code == 200, r.text
    assert r.json()["usage_chat"] == {
        "configured": None, "effective": "openai", "default": "openai",
        "providers": ["openai", "ollama"]}


def test_admin_settings_usage_chat_provider_invalid_configured_shown_as_corrupted(monkeypatch):
    """`usage_chat_provider` が API の検証を経由せず不正な値になっている場合
    （例: 旧データ・手動編集）、`effective` は既定へ黙って丸めず
    `_INVALID_SAVED_VALUE_LABEL`（"(不正な保存値)"）を返す——正常な既定選択と見分けが付く。
    `_validate_usage_chat_provider` を経由しない書込み経路を模すため `store.set_system_settings`
    を直接呼ぶ（PUT 経由では常に検証されるため再現できない）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import store
    admin, admin_uid = _admin_client()

    # 共有テーブル（system_settings）なので、元の値を GET で退避し try/finally で必ず元へ戻す
    # （`store.set_system_settings` は検証を経由しないため、破損させたのと同じ経路で戻す＝
    # PUT だと "gemini" 等の不正値は最初から書けないが、正常値へ戻すだけなので PUT でもよい
    # ところ、対称性のため同じ関数を使う）。退避 GET・復元後の再読取のどちらも成否を明示 assert
    # する（黙って読めた/戻せたと仮定すると、退避自体が失敗していた場合に別の値へ復元してしまう）。
    r_orig = admin.get("/admin/settings")
    assert r_orig.status_code == 200, r_orig.text
    original = r_orig.json()["usage_chat"]["configured"]
    store.set_system_settings(admin_uid, {"usage_chat_provider": "gemini"})
    try:
        r = admin.get("/admin/settings")
        assert r.status_code == 200, r.text
        uc = r.json()["usage_chat"]
        assert uc["configured"] == "gemini"
        assert uc["effective"] == "(不正な保存値)"
    finally:
        store.set_system_settings(admin_uid, {"usage_chat_provider": original})
        r_restored = admin.get("/admin/settings")
        assert r_restored.status_code == 200, r_restored.text
        assert r_restored.json()["usage_chat"]["configured"] == original


def test_admin_settings_secret_key_rejects_embedded_control_chars():
    """RV6 是正の固定: 中央 API キー（openai/gemini/bedrock）に改行・制御文字が混入したまま
    保存させない（422）。保存を許すと、以後の全リクエストで送信時に urllib/http.client が
    「ヘッダ値に不正な文字を含む」例外を投げ、その例外メッセージへキー値自体がエコーされて
    漏洩しうる（`research_service.py` のマスク処理は最終防衛線であり、根本対策は保存時に弾く
    こと・`sherpa/routers/system_extras.py::_validate_secret_key` 参照）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    for field in ("openai_api_key", "gemini_api_key", "bedrock_api_key"):
        r_lf = admin.put("/admin/settings", json={field: "sk-good-prefix\nAuthorization: evil"})
        assert r_lf.status_code == 422, r_lf.text
        r_crlf = admin.put("/admin/settings", json={field: "sk-good-prefix\r\nX-Injected: 1"})
        assert r_crlf.status_code == 422, r_crlf.text
        r_ctl = admin.put("/admin/settings", json={field: "sk-good-prefix\x00tail"})
        assert r_ctl.status_code == 422, r_ctl.text
    # 通常のキー（前後空白のみ）は従来どおり保存できる（過剰検知の否定）。
    r_ok = admin.put("/admin/settings", json={"openai_api_key": "  sk-perfectly-normal-key  "})
    assert r_ok.status_code == 200, r_ok.text


def test_admin_settings_secret_key_control_char_check_runs_before_strip():
    """RV7 是正の固定: 制御文字の検査は strip() 前の生値に対して行う。

    先に strip してから検査すると、`"\\r\\nsk-ok\\r\\n"` のように前後だけに制御文字がある値は
    strip 後の中身に制御文字が残らず検査をすり抜けてしまう（黙って trim されて保存される）。
    また改行だけの値（strip すると空文字になる非空文字列）も、黙って「クリア（未設定へ戻す）」
    として受理されてしまっていた——「クリア」は利用者が明示的に空文字を送った場合だけの契約
    とし、ゴミ入力を誤ってクリア操作として受理しない。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r_edge_crlf = admin.put("/admin/settings", json={"openai_api_key": "\r\nsk-ok\r\n"})
    assert r_edge_crlf.status_code == 422, r_edge_crlf.text
    r_only_newline = admin.put("/admin/settings", json={"openai_api_key": "\n"})
    assert r_only_newline.status_code == 422, r_only_newline.text
    # 明示的な空文字は従来どおりクリア（未設定へ戻す）として受理される。
    admin.put("/admin/settings", json={"openai_api_key": "sk-temp-value-before-clear"})
    r_clear = admin.put("/admin/settings", json={"openai_api_key": ""})
    assert r_clear.status_code == 200, r_clear.text


def test_admin_settings_legacy_backend_reflects_and_resets():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    # none|libreoffice は許可され、生値が configured/effective に反映される。
    r = admin.put("/admin/settings", json={"legacy_backend": "libreoffice"})
    assert r.status_code == 200, r.text
    assert r.json()["legacy_backend"]["configured"] == "libreoffice"
    assert r.json()["legacy_backend"]["effective"] == "libreoffice"   # soffice 有無に関わらず名前は反映

    # 明示的な none も有効な選択（未設定へは畳まず生値のまま保存）。
    r2 = admin.put("/admin/settings", json={"legacy_backend": "none"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["legacy_backend"]["configured"] == "none"

    # null で未設定へ戻す → env/既定へフォールバック（configured=None）。
    r3 = admin.put("/admin/settings", json={"legacy_backend": None})
    assert r3.status_code == 200, r3.text
    assert r3.json()["legacy_backend"]["configured"] is None


def test_admin_settings_legacy_backend_office_com_allowed():
    """W1: office_com は許可され保存できる（ワーカー未起動でも設定自体は保持＝起動待ちの間も選択を覚える）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"legacy_backend": "office_com"})
    assert r.status_code == 200, r.text
    lb = r.json()["legacy_backend"]
    assert lb["configured"] == "office_com"               # 生値として保存される
    assert lb["effective"] == "office_com"                # backend 名は反映（到達可否とは独立）
    # ただし URL 未設定かつ direct（powershell）を conftest で無効化したこの環境では変換不可＝unavailable。
    assert lb["office_com"]["mode"] == "unavailable"
    assert lb["office_com"]["available"] is False
    assert lb["office_com"]["configured_url"] is False
    # 後片付け（他テストへ漏らさない）。
    admin.put("/admin/settings", json={"legacy_backend": None})


# ===== 実効反映（system_settings > env > 既定）=====

def test_put_arms_enabled_reflects_in_convertible_exts_and_get():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    # ooxml のみ有効化 → PDF は convertible から外れる（pdf_text を無効化）。
    r = admin.put("/admin/settings", json={"arms_enabled": ["ooxml"]})
    assert r.status_code == 200, r.text
    assert r.json()["arms"]["configured"] == ["ooxml"]
    assert r.json()["arms"]["enabled"] == ["ooxml"]
    # office_md.convertible_exts は system_settings を反映（PUT でキャッシュ無効化済）。
    exts = office_md.convertible_exts()
    assert ".pdf" not in exts       # pdf_text 無効＝PDF は MD 化対象外
    assert ".docx" in exts          # ooxml は有効

    # null で未設定へ戻す → env/既定（ooxml,pdf_text）へフォールバック。
    r2 = admin.put("/admin/settings", json={"arms_enabled": None})
    assert r2.status_code == 200, r2.text
    assert r2.json()["arms"]["configured"] is None
    assert set(office_md.convertible_exts()) >= {".docx"}
    assert set(r2.json()["arms"]["enabled"]) == set(arms.env_default_arm_names())


def test_put_vlm_reflects_and_resets():
    """⑤: vlm 設定は生値が configured に、解決結果が effective に反映され、null で未設定へ戻る。
    provider=openai・cloud_allowed=false でも保存は許可（実効は無効＝画像を送らない・fail-safe）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"vlm": {"provider": "openai", "model": "gpt-4o", "cloud_allowed": False}})
    assert r.status_code == 200, r.text
    vlm = r.json()["vlm"]
    assert vlm["configured"] == {"provider": "openai", "model": "gpt-4o", "cloud_allowed": False}
    assert vlm["effective"]["provider"] == "openai" and vlm["effective"]["model"] == "gpt-4o"
    assert vlm["available"] is False                      # openai×未許可＝実効は使えない（画像を送らない）

    # ローカル＋モデル指定は使える扱い。
    r2 = admin.put("/admin/settings", json={"vlm": {"provider": "ollama", "model": "qwen2.5vl"}})
    assert r2.status_code == 200, r2.text
    assert r2.json()["vlm"]["available"] is True

    # null で未設定へ戻す → env/既定（ローカル・cloud=false）。
    r3 = admin.put("/admin/settings", json={"vlm": None})
    assert r3.status_code == 200, r3.text
    assert r3.json()["vlm"]["configured"] is None
    assert r3.json()["vlm"]["effective"]["cloud_allowed"] is False


def test_put_codex_session_retention_days_reflects_and_resets():
    """R1b（決定5）: 保持日数は生値が configured/effective に反映され、0（無制限）も明示保存できる。
    null で未設定（＝0/無制限）へ戻る。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"codex_session_retention_days": 14})
    assert r.status_code == 200, r.text
    csr = r.json()["codex_session_retention_days"]
    assert csr["configured"] == 14 and csr["effective"] == 14

    # 0 も「明示的に無制限」として保存できる（未設定 null とは別扱い）。
    r2 = admin.put("/admin/settings", json={"codex_session_retention_days": 0})
    assert r2.status_code == 200, r2.text
    csr2 = r2.json()["codex_session_retention_days"]
    assert csr2["configured"] == 0 and csr2["effective"] == 0

    # null で未設定へ戻す。
    r3 = admin.put("/admin/settings", json={"codex_session_retention_days": None})
    assert r3.status_code == 200, r3.text
    csr3 = r3.json()["codex_session_retention_days"]
    assert csr3["configured"] is None and csr3["effective"] == 0


# ===== 使えるモデル（model_catalog） =====

def test_admin_settings_model_catalog_shape_and_default():
    """未設定なら configured は None・effective は組み込み既定（openai/gemini/ollama/codex）を含む。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.get("/admin/settings")
    assert r.status_code == 200, r.text
    mc = r.json()["model_catalog"]
    assert mc["configured"] is None
    assert mc["effective"]["openai"]["chat"]["default"] == "gpt-5.5"
    assert mc["effective"]["ollama"]["chat"]["allowed"] == ["qwen2.5"]
    assert mc["effective"]["codex"]["codex"]["default"] == "gpt-5.5"
    assert "bedrock" not in mc["effective"]   # 実在確認済みモデルの専用機構と重複させない（対象外）
    assert set(mc["providers"]) == {"openai", "gemini", "bedrock", "ollama", "codex"}
    assert set(mc["usages"]) == {"chat", "intent", "embed", "route", "subsearch", "codex", "render"}


def test_admin_settings_model_catalog_put_reflects_and_resets():
    """PUT で1セルだけ差し替えても、他セルは組み込み既定のまま残る（部分セル・全体マージ）。
    null で未設定へ戻る（組み込み既定のみへ）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_catalog": {"openai": {"chat": {"allowed": ["custom-a", "custom-b"], "default": "custom-a"}}}})
    assert r.status_code == 200, r.text
    mc = r.json()["model_catalog"]
    assert mc["configured"] == {"openai": {"chat": {"allowed": ["custom-a", "custom-b"], "default": "custom-a"}}}
    assert mc["effective"]["openai"]["chat"] == {"allowed": ["custom-a", "custom-b"], "default": "custom-a"}
    # 触っていないセルは組み込み既定のまま。
    assert mc["effective"]["ollama"]["chat"]["allowed"] == ["qwen2.5"]

    r2 = admin.put("/admin/settings", json={"model_catalog": None})
    assert r2.status_code == 200, r2.text
    mc2 = r2.json()["model_catalog"]
    assert mc2["configured"] is None
    assert mc2["effective"]["openai"]["chat"]["default"] == "gpt-5.5"


def test_admin_settings_model_catalog_builtin_unaffected_by_configured_overrides():
    """`builtin`（組み込み既定のみ・`model_catalog.get_catalog({})`）は管理者設定を一切重ねない。
    セルを明示的に上書きしても builtin は変わらず、effective とは異なる（差分強調の基準として
    使える＝configured の存在有無ではなく実際の値差分で判定できることの前提）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import model_catalog

    admin, _ = _admin_client()
    builtin_before = admin.get("/admin/settings").json()["model_catalog"]["builtin"]
    assert builtin_before == model_catalog.get_catalog({})

    r = admin.put("/admin/settings", json={
        "model_catalog": {"openai": {"chat": {"allowed": ["custom-a"], "default": "custom-a"}}}})
    assert r.status_code == 200, r.text
    mc = r.json()["model_catalog"]
    assert mc["builtin"] == builtin_before   # configured を変えても builtin は不変
    assert mc["builtin"]["openai"]["chat"]["default"] == "gpt-5.5"   # 組み込み既定のまま
    assert mc["effective"]["openai"]["chat"]["default"] == "custom-a"   # effective は上書き反映
    assert mc["builtin"] != mc["effective"]

    admin.put("/admin/settings", json={"model_catalog": None})   # 後始末


def test_admin_settings_model_catalog_validation_errors():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    assert admin.put("/admin/settings", json={"model_catalog": "notdict"}).status_code == 422
    assert admin.put("/admin/settings", json={"model_catalog": {"openai": "notdict"}}).status_code == 422
    assert admin.put("/admin/settings",
                     json={"model_catalog": {"openai": {"chat": "notdict"}}}).status_code == 422
    assert admin.put("/admin/settings",
                     json={"model_catalog": {"openai": {"chat": {"allowed": "notalist"}}}}).status_code == 422
    assert admin.put("/admin/settings",
                     json={"model_catalog": {"openai": {"chat": {"allowed": [1, 2]}}}}).status_code == 422
    assert admin.put("/admin/settings",
                     json={"model_catalog": {"openai": {"chat": {"allowed": [], "default": 1}}}}).status_code == 422


def test_admin_settings_model_catalog_default_auto_added_to_allowed():
    """default が allowed に含まれていなければ自動的に足す（保存直後に矛盾を作らない）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_catalog": {"openai": {"chat": {"allowed": ["a"], "default": "b"}}}})
    assert r.status_code == 200, r.text
    cell = r.json()["model_catalog"]["configured"]["openai"]["chat"]
    assert cell["default"] == "b" and "b" in cell["allowed"] and "a" in cell["allowed"]


def test_admin_settings_model_catalog_rejects_bedrock_cell():
    """重大バグ是正: 対象外のプロバイダ（bedrock）のセルは admin 直接 API 経由でも 422（実在確認済み
    モデルの専用機構と二重の真実源になる隠れ設定を防ぐ）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_catalog": {"bedrock": {"chat": {"allowed": ["x"], "default": "x"}}}})
    assert r.status_code == 422, r.text


def test_admin_settings_model_catalog_rejects_unknown_provider_and_usage():
    """重大バグ是正: タイプミス（未知 provider/usage）は 422（黙って保存すると UI にも実行にも
    効かない隠れ設定になる）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    assert admin.put("/admin/settings", json={
        "model_catalog": {"opneai": {"chat": {"allowed": ["x"], "default": "x"}}}}).status_code == 422
    assert admin.put("/admin/settings", json={
        "model_catalog": {"openai": {"bogus-usage": {"allowed": ["x"], "default": "x"}}}}).status_code == 422


def test_admin_settings_view_top_level_keys_match_schema():
    """重大バグ是正（スキーマ検証の偽陽性防止）: GET /admin/settings の実応答トップレベルキーが
    `AdminSettingsView` の宣言フィールドと完全一致することを固定する（pydantic は既定で余剰キーを
    無視するため、`model_catalog` のような新規フィールドがスキーマへ追加漏れしても
    `test_response_schemas.py` の TypeAdapter 検証だけでは気付けなかった）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import schemas as sc
    admin, _ = _admin_client()
    r = admin.get("/admin/settings")
    assert r.status_code == 200, r.text
    assert set(r.json().keys()) == set(sc.AdminSettingsView.model_fields.keys())


def test_admin_settings_view_catalog_and_allowlist_share_one_snapshot(monkeypatch):
    """重大バグ是正（RV 7巡目 #7）: `_admin_settings_view()` は `sysset = store.get_system_settings()`
    を取得済みだが、モデルカタログ（`model_catalog.get_catalog()`）と Ollama allowlist
    （`llm._allowlisted_hosts()`）を無引数で再読込しており、`configured`（sysset 由来）と
    `effective`（独自読込）が別時点になり得た。ここでは `_admin_settings_view()` を直接呼び
    （HTTP 経由だと `vision_arm._openai_key()` 等の**このスコープ外**のヘルパーが独自に
    `keys.resolve_api_key`／`selected_cloud_provider` を読み直しており、それらと混線して
    偽陽性/偽陰性の原因になるため）、`get_catalog`／`_allowlisted_hosts` が `_admin_settings_view`
    自身が読んだのと同一の system_settings オブジェクトを受け取ることを固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import llm, model_catalog
    from sherpa.routers import system_extras as sysx

    sentinel = store.get_system_settings()   # _admin_settings_view() 自身の最初の読取と同じ値を使う

    def _fixed():
        return sentinel

    monkeypatch.setattr(store, "get_system_settings", _fixed)

    seen: dict[str, list] = {}

    def _spy(name, real, extract):
        def _wrapped(*args, **kwargs):
            seen.setdefault(name, []).append(extract(args, kwargs))
            return real(*args, **kwargs)
        return _wrapped

    monkeypatch.setattr(model_catalog, "get_catalog", _spy(
        "get_catalog", model_catalog.get_catalog, lambda a, kw: kw.get("system_settings", a[0] if a else None)))
    monkeypatch.setattr(llm, "_allowlisted_hosts", _spy(
        "_allowlisted_hosts", llm._allowlisted_hosts, lambda a, kw: kw.get("system_settings", a[0] if a else None)))

    sysx._admin_settings_view()

    required = {"get_catalog", "_allowlisted_hosts"}
    missing = required - set(seen)
    assert not missing, f"呼ばれなかったヘルパー: {missing}"
    # `model_catalog.get_catalog` は `effective`（sentinel を渡す）に加え、`builtin`（組み込み既定のみ・
    # 意図的に空の system_settings `{}` を渡す）の分も呼ばれる。少なくとも1回は sentinel と
    # 同一オブジェクトを受け取っていることだけを固定する（`_allowlisted_hosts` は1回のみの呼び出し）。
    assert any(arg is sentinel for arg in seen["get_catalog"]), \
        "get_catalog が _admin_settings_view 自身の system_settings（sentinel）を一度も受け取らなかった"
    # `builtin`（組み込み既定のみ・差分強調の基準）を作る呼び出しは、`sentinel`（実際の system_settings）
    # ではなく明示的に空の `{}` を渡していることを固定する（`None` だと get_catalog が自分で
    # `store.get_system_settings()` を読み直してしまい、この関数が最初に読んだスナップショットと
    # 別時点になり得る＝本テストが検出しようとしている実害そのものが builtin 側で再発する）。
    assert any(arg == {} and arg is not sentinel for arg in seen["get_catalog"]), \
        "get_catalog が builtin 用に空の {} を渡していない（None 省略や sentinel 誤用の疑い）"
    for sysset_arg in seen["_allowlisted_hosts"]:
        assert sysset_arg is sentinel, \
            "_allowlisted_hosts が _admin_settings_view 自身の system_settings と異なるオブジェクトを受け取った"


def test_admin_settings_ollama_url_and_allowlist_can_be_set_together():
    """重大バグ是正: 新しいホストを allowlist に追加しつつ、同じ PUT でそれを中央既定に設定できる
    （以前は DB 未更新の古い allowlist で ollama_url を検証していたため 422 になっていた）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={
        "ollama_allowlist": ["10.9.9.9:11434"], "ollama_url": "http://10.9.9.9:11434"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ollama_allowlist"]["configured"] == ["10.9.9.9:11434"]
    assert body["cloud"]["ollama_url"] == "http://10.9.9.9:11434"


def test_admin_settings_ollama_allowlist_can_remove_current_central_host():
    """allowlist から現在の中央既定ホストを外す操作自体は禁止しない（実行時に到達不可になるだけの
    admin の正当な操作・item 5 の是正で新たな制約を追加しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r1 = admin.put("/admin/settings", json={
        "ollama_allowlist": ["10.9.9.9:11434"], "ollama_url": "http://10.9.9.9:11434"})
    assert r1.status_code == 200, r1.text
    r2 = admin.put("/admin/settings", json={"ollama_allowlist": []})
    assert r2.status_code == 200, r2.text
    assert r2.json()["ollama_allowlist"]["configured"] is None
    assert r2.json()["cloud"]["ollama_url"] == "http://10.9.9.9:11434"   # 中央既定は残る（未検証のまま）


def test_admin_settings_ollama_allowlist_only_change_unaffected():
    """allowlist だけを変更する（ollama_url は触らない）操作は従来どおり動く。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"ollama_allowlist": ["10.9.9.8:11434"]})
    assert r.status_code == 200, r.text
    assert r.json()["ollama_allowlist"]["configured"] == ["10.9.9.8:11434"]


def test_admin_settings_ollama_url_change_must_be_authorized_by_pending_allowlist_not_old():
    """重大バグ是正（RV 2巡目）: 中央URLとallowlistを同一PUTで変更する場合、新URLは「置換後の」
    allowlistで検証する。旧allowlistにだけ残っているホストへ変更する新しいPUTを、旧一覧の権限で
    誤って認可しない（保存直後の実行時には拒否される不整合を保存時に検知する）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r1 = admin.put("/admin/settings", json={"ollama_allowlist": ["10.9.9.9:11434"]})
    assert r1.status_code == 200, r1.text
    # 置換後の新一覧(10.9.9.8)に含まれない 10.9.9.9（旧一覧にだけ残る）へ中央URLを変更しようと
    # する＝旧一覧の権限で通ってはならない。
    r2 = admin.put("/admin/settings", json={
        "ollama_allowlist": ["10.9.9.8:11434"], "ollama_url": "http://10.9.9.9:11434"})
    assert r2.status_code == 422, r2.text


def test_admin_settings_ollama_url_unchanged_allowed_even_if_allowlist_narrowed_same_put():
    """重大バグ是正（RV 2巡目）: 中央URLが実際には変わらない再送（フォームの全項目送信等）は、
    同時に allowlist を狭めてそのホストを含まなくなっても拒否しない（狭い例外＝URL自体を新しく
    選んだわけではないため・allowlist だけからの中央ホスト削除は既に認めている裁定と同じ扱い）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r1 = admin.put("/admin/settings", json={
        "ollama_allowlist": ["10.9.9.9:11434"], "ollama_url": "http://10.9.9.9:11434"})
    assert r1.status_code == 200, r1.text
    r2 = admin.put("/admin/settings", json={
        "ollama_allowlist": [], "ollama_url": "http://10.9.9.9:11434"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["ollama_allowlist"]["configured"] is None
    assert r2.json()["cloud"]["ollama_url"] == "http://10.9.9.9:11434"


# ===== openai_endpoint_kind/openai_base_url のクロス検証（書込み直前の原子性）=====

def test_admin_settings_openai_endpoint_kind_azure_without_base_rejected():
    """基本ケース: 単発の PUT で kind=azure・base 無しは 422（クロス検証そのものの固定・
    `store.set_system_settings` が advisory lock 取得後に同一コネクションから実効値を
    読み直して検証する契約を経由することを確認）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"openai_endpoint_kind": "azure"})
    assert r.status_code == 422, r.text


def test_admin_settings_openai_endpoint_kind_and_base_together_succeeds():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={
        "openai_endpoint_kind": "azure", "openai_base_url": "https://res.openai.azure.com"})
    assert r.status_code == 200, r.text
    assert r.json()["openai_endpoint"]["configured"]["kind"] == "azure"


def test_admin_settings_openai_endpoint_base_url_alone_uses_fresh_kind_not_stale_cache():
    """kind/base のクロス検証は `store.set_system_settings` が advisory lock
    取得後に**同一コネクションから読み直した実効値**で行う（3秒 TTL キャッシュ経由の現在値ではない）。

    ここでは「別の書込み経路（`store.set_system_settings` を直接呼ぶ・admin PUT を経由しない）が
    キャッシュの外で kind を azure に変えた直後」を模す。この PUT リクエストのプロセスが読む
    `get_system_settings()` のキャッシュがまだ古い（kind=openai のまま）としても、書込み直前の
    再読み取りは常に DB の最新値（azure）を見るため、base_url を欠いた状態で確定させることはない
    （＝azure のまま base だけを外そうとする PUT は 422 になる）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r1 = admin.put("/admin/settings", json={
        "openai_endpoint_kind": "azure", "openai_base_url": "https://res.openai.azure.com"})
    assert r1.status_code == 200, r1.text
    # キャッシュを人為的に古い状態（kind=openai）へ巻き戻す（3秒 TTL の外部要因を模す）。facade
    # （`sherpa.store`）の re-export は import 時点のスナップショットで別バインディングのため、
    # 実体（`sherpa.store.settings`）を直接 monkeypatch する（`get_system_settings`/
    # `set_system_settings` が実際に読み書きするのはこちら）。
    from sherpa.store import settings as _store_settings
    _store_settings._system_settings_cache = {"openai_endpoint_kind": "openai"}
    _store_settings._system_settings_cache_ts = time.monotonic()
    try:
        # base_url だけを外そうとする（kind は指定しない＝現状 azure を維持するつもりの PUT）。
        # 古いキャッシュ（kind=openai）を見ていたら検証を素通りしてしまうが、advisory lock 後の
        # 読み直しは DB の実際の値（azure）を見るため 422 になるはず。
        r2 = admin.put("/admin/settings", json={"openai_base_url": None})
        assert r2.status_code == 422, r2.text
        # 拒否されたので実際に azure+base のまま変わっていないことも確認する。
        store._invalidate_system_settings_cache()
        view = admin.get("/admin/settings").json()["openai_endpoint"]["configured"]
        assert view["kind"] == "azure"
        assert view["base_url"] == "https://res.openai.azure.com"
    finally:
        store._invalidate_system_settings_cache()


def test_admin_settings_openai_endpoint_kind_openai_recovers_corrupted_base_url_in_one_put():
    """一操作復旧（実害の回帰固定）: `openai_base_url` が非文字列に破損した状態（通常の PUT
    経路では起こり得ない・直接 DB へ混入した想定）から、`openai_endpoint_kind` を明示的に
    `"openai"` へ保存する単発の PUT だけで復旧できることを固定する。管理画面は「本家」選択時に
    `openai_base_url` を PUT ボディへ含めない（`web/admin-settings.js::collectOpenaiEndpoint`）
    ため、この PUT 自身が破損値を明示 NULL 化しないと、型検査
    （`llm._assert_openai_endpoint_settings_types_valid`）により読み取り側が `ValueError` を
    送出し続け、別途 `openai_base_url` を明示クリアする2回目の PUT が必要になってしまう。"""
    if not _try_init():
        pytest.skip("DB down")
    from psycopg.types.json import Json

    from sherpa import llm

    admin, _ = _admin_client()
    with store._connect() as c:
        c.execute(
            "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, 'test') "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            ("openai_endpoint_kind", Json("azure")))
        c.execute(
            "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, 'test') "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            ("openai_base_url", Json(0)))
    store._invalidate_system_settings_cache()

    r = admin.put("/admin/settings", json={"openai_endpoint_kind": "openai"})
    assert r.status_code == 200, r.text

    with store._connect() as c:
        row = c.execute(
            "SELECT value FROM system_settings WHERE key=%s", ("openai_base_url",)).fetchone()
    assert row is None, f"openai_base_url が復旧（未設定へ削除）されていない: {row}"

    store._invalidate_system_settings_cache()
    sysset = store.get_system_settings()
    assert llm.openai_endpoint_kind(sysset) == "openai"
    assert llm.openai_base_url(sysset) == "https://api.openai.com/v1"


def test_admin_settings_openai_endpoint_kind_openai_recovery_audit_before_shows_corruption():
    """実害の回帰固定: 一操作復旧 PUT の監査 before-state は、破損していた事実
    （`(不正な保存値)`）を残す。`_redact_secret_settings` が非文字列の falsy 値（`{}`/`[]`/`0`/
    `False`）を `<cleared>`（=「元々未設定だった」）に畳んでしまうと、監査記録だけを見たときに
    「一操作復旧が実際に何を直したのか」（破損値の削除）が分からなくなる。"""
    if not _try_init():
        pytest.skip("DB down")
    from psycopg.types.json import Json

    admin, admin_uid = _admin_client()
    with store._connect() as c:
        c.execute(
            "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, 'test') "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            ("openai_endpoint_kind", Json("azure")))
        c.execute(
            "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, 'test') "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            ("openai_base_url", Json({})))
    store._invalidate_system_settings_cache()

    r = admin.put("/admin/settings", json={"openai_endpoint_kind": "openai"})
    assert r.status_code == 200, r.text

    rows = store.list_audit(action="system_settings.updated", actor=admin_uid, limit=1)
    assert rows, "system_settings.updated が監査に残っていない"
    before = rows[0]["before_state"]
    after = rows[0]["after_state"]
    assert before.get("openai_base_url") == "(不正な保存値)", (
        f"破損の事実が before から消えている: {before!r}")
    assert after.get("openai_base_url") == "<cleared>", (
        f"復旧後は未設定へ削除されたことが after に残るべき: {after!r}")


def test_admin_settings_openai_endpoint_kind_openai_does_not_touch_valid_saved_base_url():
    """一操作復旧の対象は「破損（非文字列）した」既存値だけであり、正常な文字列の
    `openai_base_url` が既に保存されている場合は、`openai_endpoint_kind` を `"openai"` へ
    切り替える PUT でも触らずそのまま残す（azure→openai→azure の往復で値が保持される既存の
    契約を壊さない・回帰ゼロ）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import llm

    admin, _ = _admin_client()
    r1 = admin.put("/admin/settings", json={
        "openai_endpoint_kind": "azure", "openai_base_url": "https://res.openai.azure.com"})
    assert r1.status_code == 200, r1.text

    r2 = admin.put("/admin/settings", json={"openai_endpoint_kind": "openai"})
    assert r2.status_code == 200, r2.text

    sysset = store.get_system_settings()
    assert sysset.get("openai_base_url") == "https://res.openai.azure.com"
    assert llm.openai_endpoint_kind(sysset) == "openai"


def test_set_system_settings_raises_conflict_exception_directly():
    """`store.set_system_settings` は不整合な kind/base の組合せを
    `store.OpenAIEndpointSettingsConflict`（`ValueError` 派生）で拒否する
    （`sherpa/routers/system_extras.py::admin_settings_put` がこれを 422 に変換する契約・上の HTTP
    経由のテストとは別に、store 層そのものの例外型も固定する）。"""
    if not _try_init():
        pytest.skip("DB down")
    with pytest.raises(store.OpenAIEndpointSettingsConflict):
        store.set_system_settings("admin-uid", {"openai_endpoint_kind": "custom"})


def test_admin_settings_openai_endpoint_kind_alone_succeeds_when_base_already_saved():
    """実 API（mock ではない）で「保存済み base_url あり＋kind 単独 PUT」が成功することを
    固定する。1回目の PUT で kind=azure＋base を保存した後、2回目の PUT では kind だけを送る
    （実ブラウザの admin-settings.js はこの組合せを送らないが、`store.set_system_settings` の
    クロス検証（`_assert_openai_endpoint_update_consistent`）は現在の実効 base_url を DB から
    読み直して補うため、kind だけの PUT でも保存済み base があれば 422 にならない・
    `tests/e2e/test_admin_settings_ui.py::test_mock_openai_endpoint_pending_inherits_saved_base_url_...`
    の mock 版に対する実 API 版）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r1 = admin.put("/admin/settings", json={
        "openai_endpoint_kind": "azure", "openai_base_url": "https://res.openai.azure.com"})
    assert r1.status_code == 200, r1.text
    r2 = admin.put("/admin/settings", json={"openai_endpoint_kind": "azure"})
    assert r2.status_code == 200, r2.text
    view = r2.json()["openai_endpoint"]["configured"]
    assert view["kind"] == "azure"
    assert view["base_url"] == "https://res.openai.azure.com"   # 保存済み base はそのまま維持される


def test_config_omits_token_pricing():
    """金額系の撤去確認: /config はトークン単価表（token_pricing）を返さない。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    cfg = admin.get("/config").json()
    assert "token_pricing" not in cfg


def test_partial_update_leaves_other_keys_untouched():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    admin.put("/admin/settings", json={"legacy_backend": "libreoffice"})
    # arms_enabled を触らない更新でも legacy_backend は残る。
    r = admin.put("/admin/settings", json={"arms_enabled": ["ooxml", "pdf_text"]})
    assert r.status_code == 200, r.text
    assert r.json()["legacy_backend"]["configured"] == "libreoffice"
    assert r.json()["arms"]["configured"] == ["ooxml", "pdf_text"]


# ===== 監査 =====

def test_put_writes_audit_warning():
    if not _try_init():
        pytest.skip("DB down")
    admin, admin_uid = _admin_client()
    r = admin.put("/admin/settings", json={"legacy_backend": "libreoffice"})
    assert r.status_code == 200, r.text
    rows = store.list_audit(action="system_settings.updated", actor=admin_uid, limit=10)
    assert rows, "system_settings.updated が監査に残っていない"
    assert rows[0]["severity"] == "warning"
    assert rows[0]["resource_type"] == "system_settings"


# ===== fail-closed（設定変更＋監査を同一トランザクションで実行・2026-07-08 RV High 対応）=====
# 旧実装（commit→別接続 audit()→失敗時 compensate）は (a) commit〜復元の間に未監査値が見える
# (b) その間のプロセス停止で未監査変更が残留する (c) 並行更新を補償復元が上書きする、の3穴があった。
# 新実装は set_system_settings 内で before スナップショット→適用→`_audit_insert` を同一トランザクションに
# 収め、監査失敗の例外で全体を rollback する。よってここでは `store.audit`（薄いラッパー・自前接続）ではなく
# `store._audit_insert`（同一トランザクションで呼ばれる内部ヘルパー）を直接壊して検証する。

def _boom(*_a, **_kw):
    raise RuntimeError("simulated audit failure")


def test_put_fail_closed_on_audit_failure(monkeypatch):
    """PUT の監査 INSERT が失敗したら、設定変更ごとロールバックされて 500（before の値がそのまま残る）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    # 先に legacy_backend=libreoffice を正常保存（before 状態）。
    assert admin.put("/admin/settings", json={"legacy_backend": "libreoffice"}).status_code == 200

    monkeypatch.setattr(store, "_audit_insert", _boom)
    r = admin.put("/admin/settings", json={"legacy_backend": "none"})
    assert r.status_code == 500, r.text
    monkeypatch.undo()

    # キャッシュを明示的に捨ててから読み直す＝DB そのものが本当にロールバックされていることを確認する
    # （キャッシュが偶然正しい値を返しているだけ、という誤検知を避ける）。
    store._invalidate_system_settings_cache()
    assert store.get_system_settings().get("legacy_backend") == "libreoffice", \
        "監査失敗時に none が commit されている（同一トランザクション化が効いていない）"

    # テーブル行そのものも none になっていないことを直接確認する（cache 経路を経由しない生の確認）。
    with store._connect() as c:
        row = c.execute("SELECT value FROM system_settings WHERE key=%s", ("legacy_backend",)).fetchone()
    assert row["value"] == "libreoffice"


def test_put_fail_closed_new_key_is_not_partially_committed(monkeypatch):
    """監査失敗時、それまで未設定だったキー（arms_enabled）が中途半端に残らない（テーブルに行が無い）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()

    monkeypatch.setattr(store, "_audit_insert", _boom)
    r = admin.put("/admin/settings", json={"arms_enabled": ["ooxml"]})
    assert r.status_code == 500, r.text
    monkeypatch.undo()

    store._invalidate_system_settings_cache()
    assert store.get_system_settings().get("arms_enabled") is None
    with store._connect() as c:
        row = c.execute("SELECT value FROM system_settings WHERE key=%s", ("arms_enabled",)).fetchone()
    assert row is None, "監査失敗時に新規キーの行がテーブルへ残っている（トランザクション化が効いていない）"


def test_compat_mode_admin(auth_disabled):
    """互換モード（SHERPA_AUTH_DISABLED=1・合成 admin）でも GET/PUT が動く。"""
    if not _try_init():
        pytest.skip("DB down")
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/admin/settings").status_code == 200
    r = c.put("/admin/settings", json={"legacy_backend": "libreoffice"})
    assert r.status_code == 200, r.text
    assert r.json()["legacy_backend"]["configured"] == "libreoffice"


# ===== store.seed_system_settings_once（実 DB での意味論の固定） =====

def test_seed_system_settings_once_inserts_when_absent_and_skips_when_present():
    """未設定のキーは実際に INSERT され `applied` に返る。既に値がある行は上書きせず `conflicts` に
    現在の DB 値を返す（`sherpa.api._seed_settings_from_env` が呼ぶ store 層の不変条件を実 DB で確認）。"""
    if not _try_init():
        pytest.skip("DB down")
    applied1, conflicts1 = store.seed_system_settings_once(
        {"openai_api_key": "sk-first-seed"}, guard_key="env_seed_version",
        secret_keys=frozenset({"openai_api_key"}))
    assert applied1 == {"openai_api_key": "sk-first-seed"}
    assert conflicts1 == {}
    assert store.get_system_settings()["openai_api_key"] == "sk-first-seed"

    # 2回目: 別の値を渡しても上書きされない（ON CONFLICT DO NOTHING）。
    applied2, conflicts2 = store.seed_system_settings_once(
        {"openai_api_key": "sk-would-clobber"}, guard_key="env_seed_version",
        secret_keys=frozenset({"openai_api_key"}))
    assert applied2 == {}
    assert conflicts2 == {"openai_api_key": "sk-first-seed"}   # 現在の DB 値（元の値のまま）
    assert store.get_system_settings()["openai_api_key"] == "sk-first-seed"


def test_seed_system_settings_once_does_not_clobber_concurrent_admin_write():
    """`get_system_settings()` でマーカー不在を確認した直後、実際の `seed_system_settings_once` 呼び出し
    までの間に管理者が別トランザクションで先に値を入れても、その値を上書きしない（実 DB での検証）。"""
    if not _try_init():
        pytest.skip("DB down")
    assert store.get_system_settings().get("openai_api_key") is None   # 未シード状態

    # 「管理者が先に入れる」を模す＝seed とは別の set_system_settings 呼び出しを間に挟む。
    store.set_system_settings("admin-uid", {"openai_api_key": "sk-admin-entered"},
                              secret_keys=frozenset({"openai_api_key"}))

    applied, conflicts = store.seed_system_settings_once(
        {"openai_api_key": "sk-env-value", "env_seed_version": 1}, guard_key="env_seed_version",
        secret_keys=frozenset({"openai_api_key"}))
    assert "openai_api_key" not in applied
    assert conflicts["openai_api_key"] == "sk-admin-entered"
    assert applied == {"env_seed_version": 1}   # マーカーは新規なので書ける
    assert store.get_system_settings()["openai_api_key"] == "sk-admin-entered"   # 上書きされていない


def test_seed_system_settings_once_does_not_reinsert_key_deleted_after_marker_set():
    """マーカー（guard_key）が既に存在する状態で、そのキー自体は行が無い（管理者が削除した状態）
    場合、`seed_system_settings_once` を呼んでも挿入されない（実 DB での検証・マーカーの有無だけを
    見て個々のキーの ON CONFLICT に任せていた旧設計では、マーカーが既にあってもキー単体が復活して
    しまう穴があった）。"""
    if not _try_init():
        pytest.skip("DB down")
    # マーカーだけ先に立てる（= シード完了済みの状態を模す。openai_api_key は無い＝削除済み）。
    store.set_system_settings("admin-uid", {"env_seed_version": 1})
    assert store.get_system_settings().get("openai_api_key") is None

    applied, conflicts = store.seed_system_settings_once(
        {"openai_api_key": "sk-old-env-value"}, guard_key="env_seed_version",
        secret_keys=frozenset({"openai_api_key"}))
    assert applied == {}
    assert conflicts == {"openai_api_key": None}   # マーカーが存在するため書込み自体が起きなかった
    assert store.get_system_settings().get("openai_api_key") is None   # 復活していない


def test_migrate_marker_if_legacy_exists_migrates_without_touching_other_keys():
    """実 DB での `store.migrate_marker_if_legacy_exists` の検証。旧共有マーカー
    （`env_seed_version` 相当）が存在すれば、`guard_key` だけを確定して他のキーには一切触れない
    （admin が削除した値を復活させない・legacy_key を持たない `seed_system_settings_once` と違う
    独立した関数であることを実 DB で固定する）。"""
    if not _try_init():
        pytest.skip("DB down")
    store.set_system_settings("admin-uid", {"legacy-marker-test": 1})
    migrated = store.migrate_marker_if_legacy_exists(
        "new-guard-test", "legacy-marker-test", guard_value=1)
    assert migrated is True
    assert store.get_system_settings().get("new-guard-test") == 1
    assert "ollama_url" not in store.get_system_settings()   # 触れていない


def test_migrate_marker_if_legacy_exists_returns_false_when_legacy_absent():
    """旧マーカーが無ければ何も書かず False を返す（新規導入環境の判定・呼び出し元は
    通常どおりの候補構築・書込みへ進める）。"""
    if not _try_init():
        pytest.skip("DB down")
    migrated = store.migrate_marker_if_legacy_exists(
        "new-guard-test2", "legacy-marker-that-does-not-exist", guard_value=1)
    assert migrated is False
    assert store.get_system_settings().get("new-guard-test2") is None


def test_migrate_marker_if_legacy_exists_is_idempotent_across_repeated_calls():
    """複数回呼んでも guard_key は二重確定しない（`WHERE NOT EXISTS` の単体原子性）。"""
    if not _try_init():
        pytest.skip("DB down")
    store.set_system_settings("admin-uid", {"legacy-marker-test3": 1})
    assert store.migrate_marker_if_legacy_exists("new-guard-test3", "legacy-marker-test3", 1) is True
    assert store.migrate_marker_if_legacy_exists("new-guard-test3", "legacy-marker-test3", 1) is True
    assert store.get_system_settings().get("new-guard-test3") == 1


def test_seed_ollama_url_from_env_does_not_revive_admin_deleted_url_after_legacy_upgrade(monkeypatch):
    """旧統合シード済み環境（`env_seed_version` あり）で
    admin が `ollama_url`／`ollama_allowlist` を削除済みの状態から、残存 `OLLAMA_URL` env のまま
    `api._seed_ollama_url_from_env()` を呼んでも、削除済みの接続先は復活・再認可されない
    （`sherpa.api` 経由・実 DB・モックなしの end-to-end 検証）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import api as api_mod
    store.set_system_settings("admin-uid", {"env_seed_version": 1})   # 旧統合シード済み（admin 削除後）
    monkeypatch.setenv("OLLAMA_URL", "http://revival-should-not-happen.internal:11434")
    api_mod._seed_ollama_url_from_env()
    current = store.get_system_settings()
    assert "ollama_url" not in current
    assert "ollama_allowlist" not in current
    assert current.get(api_mod._OLLAMA_URL_SEED_MARKER_KEY) == api_mod._OLLAMA_URL_SEED_VERSION


def test_seed_system_settings_once_ollama_allowlist_merge_when_url_newly_inserted():
    """重大バグ是正（RV 3巡目 #2）: `ollama_allowlist_merge` を渡すと、URL が実際に新規挿入できた
    場合だけ、既存の allowlist（他の admin 登録ホストを含む）へ host:port を追記する
    （URLとその送信先の認可を常にペアとして確定させる・実 DB での検証）。"""
    if not _try_init():
        pytest.skip("DB down")
    store.set_system_settings("admin-uid", {"ollama_allowlist": ["10.1.1.1:11434"]})
    applied, _conflicts = store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434", "env_seed_version": 1},
        guard_key="env_seed_version",
        ollama_allowlist_merge=("ollama_url", "central.internal:11434"))
    assert applied["ollama_url"] == "http://central.internal:11434"
    assert set(applied["ollama_allowlist"]) == {"10.1.1.1:11434", "central.internal:11434"}
    stored = store.get_system_settings()
    assert set(stored["ollama_allowlist"]) == {"10.1.1.1:11434", "central.internal:11434"}


def test_seed_system_settings_once_ollama_allowlist_not_merged_when_url_conflicts():
    """URL 行が既に存在する（このトランザクションでは新規挿入できなかった）場合、allowlist には
    一切触れない（実 DB での検証）。"""
    if not _try_init():
        pytest.skip("DB down")
    store.set_system_settings("admin-uid", {"ollama_url": "http://already-set.internal:11434"})
    applied, conflicts = store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434", "env_seed_version": 1},
        guard_key="env_seed_version",
        ollama_allowlist_merge=("ollama_url", "central.internal:11434"))
    assert "ollama_url" not in applied
    assert conflicts["ollama_url"] == "http://already-set.internal:11434"
    assert "ollama_allowlist" not in applied
    assert store.get_system_settings().get("ollama_allowlist") is None


def test_seed_system_settings_once_rejects_ollama_allowlist_in_updates_with_merge():
    """`ollama_allowlist_merge` 使用時に `updates` へ `ollama_allowlist` を含めるのは呼び出し側の
    誤り（このマージ経路が唯一の書込み元であるべき契約）＝ `ValueError`。"""
    if not _try_init():
        pytest.skip("DB down")
    with pytest.raises(ValueError):
        store.seed_system_settings_once(
            {"ollama_url": "http://x:11434", "ollama_allowlist": ["x:11434"]},
            guard_key="env_seed_version", ollama_allowlist_merge=("ollama_url", "x:11434"))


def _insert_raw_audit(action: str, after_state: dict, created_at: str | None = None) -> int:
    """テスト専用: `system_settings.env_seeded`/`system_settings.updated` の監査行を、
    `_audit_insert`（常に `now()`）を経由せず直接 INSERT する。`created_at`（トランザクション
    開始時刻）と `id`（実行順・BIGSERIAL）の食い違いを意図的に作るための唯一の方法
    （RV 5巡目 #2 のテスト：advisory lock 待ちで開始順と確定順が入れ替わるケースの再現）。"""
    from psycopg.types.json import Json
    with store._connect() as c:
        if created_at is not None:
            row = c.execute(
                "INSERT INTO audit_log (actor_user_id, action, resource_type, after_state, created_at) "
                "VALUES (%s, %s, 'system_settings', %s, %s) RETURNING id",
                ("system", action, Json(after_state), created_at)).fetchone()
        else:
            row = c.execute(
                "INSERT INTO audit_log (actor_user_id, action, resource_type, after_state) "
                "VALUES (%s, %s, 'system_settings', %s) RETURNING id",
                ("system", action, Json(after_state))).fetchone()
    return row["id"]


# ===== store.catchup_ollama_allowlist_for_env_seeded_url_v2（実 DB での意味論の固定・4巡目簡素化裁定） =====

def test_catchup_v2_adds_when_env_seeded_audit_proves_no_tampering():
    """`system_settings.env_seeded` 監査が `ollama_url` の挿入を証明し、以後 `ollama_url`／
    `ollama_allowlist` への admin 操作が無い場合だけ、host:port を allowlist へ追記する。"""
    if not _try_init():
        pytest.skip("DB down")
    store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434"}, guard_key="unused-marker-1")
    reason = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-1")
    assert reason == "added"
    stored = store.get_system_settings()
    assert "central.internal:11434" in stored["ollama_allowlist"]
    assert stored["catchup-v2-test-1"] == 1


def test_seed_system_settings_once_writes_ollama_url_fingerprint_and_redacted_url_in_audit():
    """`system_settings.env_seeded` 監査の `after_state["ollama_url"]` は host 表現へ
    畳まれ（生 URL を残さない）、tamper 検知専用の `ollama_url_fingerprint`（正規化 host:port）が
    別フィールドとして残る。"""
    if not _try_init():
        pytest.skip("DB down")
    store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434"}, guard_key="unused-marker-fp")
    with store._connect() as c:
        row = c.execute(
            "SELECT after_state FROM audit_log WHERE action='system_settings.env_seeded' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    after = row["after_state"]
    assert after["ollama_url"] == "central.internal:11434"
    assert after["ollama_url_fingerprint"] == "central.internal:11434"


def test_catchup_v2_fails_closed_when_admin_touched_url_or_allowlist_after_seed():
    """重大バグ是正（RV 4巡目 #2）: env シード後に admin が `ollama_url`／`ollama_allowlist` を
    操作していれば（URL を変えていなくても・allowlist だけの操作でも）、値の一致だけを
    provenance とみなさず fail-closed で何も追加しない（旧v1は値一致だけで判定しており、
    admin が allowlist からそのhostだけ削除した操作を復活させ得た）。"""
    if not _try_init():
        pytest.skip("DB down")
    store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434"}, guard_key="unused-marker-2")
    store.set_system_settings("admin-uid", {"ollama_allowlist": []})   # admin が明示的に空へ
    reason = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-2")
    assert reason == "skipped_unproven"
    assert store.get_system_settings().get("ollama_allowlist") in (None, [])


def test_catchup_v2_fails_closed_when_no_provable_env_seed_evidence():
    """env シードの監査証跡が無い（admin が直接 `ollama_url` を設定した・由来不明）場合は
    fail-closed で何も追加しない。"""
    if not _try_init():
        pytest.skip("DB down")
    store.set_system_settings("admin-uid", {"ollama_url": "http://admin-set-directly:11434"})
    reason = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-3")
    assert reason == "skipped_unproven"
    assert store.get_system_settings().get("ollama_allowlist") is None


def test_catchup_v2_ignores_stale_v1_marker_and_evaluates_independently():
    """重大バグ是正（RV 5巡目 #12）: 旧 v1 の完了マーカー（`ollama_allowlist_env_seed_catchup`・
    値一致だけを provenance とみなしていた旧 guard_key）が既に存在する環境（v1 を一度でも踏んだ
    展開）でも、v2 は別の guard_key（`_v2` サフィックス）で独立に評価する＝旧マーカーの存在に
    引きずられて「証明済み」と誤認しない。旧マーカーが立っているだけで v2 が要求する監査証跡が
    無ければ、通常どおり fail-closed のままであることを固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    store.set_system_settings("admin-uid", {"ollama_url": "http://legacy-v1-deploy.internal:11434"})
    with store._connect() as c:
        c.execute(
            "INSERT INTO system_settings (key, value, updated_by) VALUES "
            "('ollama_allowlist_env_seed_catchup', '1'::jsonb, 'system') ON CONFLICT (key) DO NOTHING")
    reason = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-legacy-marker")
    assert reason == "skipped_unproven"
    assert store.get_system_settings().get("ollama_allowlist") is None
    # 旧マーカー自体は触れられていない（v2 は別キーで動くので削除も上書きもしない）。
    assert store.get_system_settings().get("ollama_allowlist_env_seed_catchup") == 1


def _set_raw_ollama_url(url: str) -> None:
    """テスト専用: `system_settings.ollama_url` を監査を経由せず直接 upsert する（seed の
    after_state と現在値を一致させ、URL 一致チェックを常に満たした状態で id 順の判定だけを
    単独で検証するための下ごしらえ）。"""
    from psycopg.types.json import Json
    with store._connect() as c:
        c.execute(
            "INSERT INTO system_settings (key, value, updated_by) VALUES ('ollama_url', %s, 'test') "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (Json(url),))


def test_catchup_v2_fails_closed_when_admin_update_has_same_created_at_as_seed():
    """重大バグ是正（RV 5巡目 #2・6巡目 #12 で false green を是正）: `created_at` は解像度の
    限界等で seed 監査と同一になり得る。`created_at > seed_ts` という厳密比較では「同一時刻＝
    後ではない」と誤って見逃していた。id（実行順）が seed より後の `system_settings.updated`
    は、created_at が同一でも改ざんとして検出する。

    現在の `ollama_url` を seed 値と一致させておく（RV 6巡目 #12: 一致させていないと、
    id 判定を旧 `created_at` 判定へ戻しても URL 不一致だけで skipped_unproven になり、
    id 判定自体を検証できていない false green だった）。

    seed 監査の判定は `ollama_url_fingerprint`（正規化 host:port）で行うため、生の
    `ollama_url` フィールドと一緒にこの raw 監査へ含める（`seed_system_settings_once` が
    書く実際の形を模す・欠けていると id 判定に到達する前に「証明できない」で早期に
    skipped_unproven になり、この id 判定ロジック自体を検証できなくなる）。"""
    if not _try_init():
        pytest.skip("DB down")
    same_ts = "2031-06-01T00:00:00+00:00"
    _insert_raw_audit("system_settings.env_seeded",
                      {"ollama_url": "central.internal:11434",
                       "ollama_url_fingerprint": "central.internal:11434"}, created_at=same_ts)
    _insert_raw_audit("system_settings.updated",
                      {"ollama_url": "http://tampered.internal:11434"}, created_at=same_ts)
    _set_raw_ollama_url("http://central.internal:11434")
    reason = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-sametime")
    assert reason == "skipped_unproven"
    assert store.get_system_settings().get("ollama_allowlist") is None


def test_catchup_v2_fails_closed_when_lock_order_reverses_created_at_order():
    """重大バグ是正（RV 5巡目 #2・6巡目 #12 で false green を是正）: `created_at` はトランザク
    ション**開始**時刻（advisory lock を待つ前）であって確定（commit）順ではない。先に開始した
    が lock 待ちで後から commit した admin 更新は、created_at が seed より古いまま id（実行順）
    だけが後になる。created_at 基準では「seed より前」と誤認して見逃すが、id 基準では正しく
    「seed より後」と判定して fail-closed になることを固定する。

    現在の `ollama_url` を seed 値と一致させておく（RV 6巡目 #12: URL 不一致だけで
    skipped_unproven になる false green を避け、id 判定そのものを検証する）。

    seed 監査の判定は `ollama_url_fingerprint`（正規化 host:port）で行うため raw
    監査に含める（`test_catchup_v2_fails_closed_when_admin_update_has_same_created_at_as_seed`
    と同じ理由）。"""
    if not _try_init():
        pytest.skip("DB down")
    # seed の方を「開始が遅い」ように見せる（created_at を admin 更新より未来にする）。
    # 実行順（id）は挿入順どおり＝seed が先・admin 更新が後。
    _insert_raw_audit("system_settings.env_seeded",
                      {"ollama_url": "central.internal:11434",
                       "ollama_url_fingerprint": "central.internal:11434"},
                      created_at="2031-01-01T00:00:00+00:00")
    _insert_raw_audit("system_settings.updated",
                      {"ollama_url": "http://tampered.internal:11434"},
                      created_at="2000-01-01T00:00:00+00:00")
    _set_raw_ollama_url("http://central.internal:11434")
    reason = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-lockorder")
    assert reason == "skipped_unproven"
    assert store.get_system_settings().get("ollama_allowlist") is None


def test_catchup_v2_fails_closed_when_current_url_does_not_match_seed_audit_value():
    """重大バグ是正（RV 5巡目 #2）: 「以降に admin 更新が無い」ことを監査で証明できても、
    現在の `ollama_url` の指紋（`llm.ollama_url_fingerprint`）が seed 監査に記録された
    指紋と一致しなければ書かない（監査を経由しない書込み経路が将来増えた場合への二重の安全網）。"""
    if not _try_init():
        pytest.skip("DB down")
    _insert_raw_audit("system_settings.env_seeded",
                      {"ollama_url": "central.internal:11434",
                       "ollama_url_fingerprint": "central.internal:11434"})
    _set_raw_ollama_url("http://different-from-seed.internal:11434")
    reason = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-urlmismatch")
    assert reason == "skipped_unproven"
    assert store.get_system_settings().get("ollama_allowlist") is None


def test_catchup_v2_fails_closed_when_seed_audit_predates_fingerprint_field():
    """`ollama_url_fingerprint` フィールドを持たない旧形式の env_seeded 監査は「証明できない」
    として扱う（生 URL の文字列一致には戻さない・fail-closed）。

    host は本ファイルの他の catchup_v2 テストと重ならない一意な値を使う（`audit_log` は
    per-test で消去されない共有テーブル・seed 証拠のスキャンは guard_key で絞られない全件走査
    のため、同じ host を使う他テストの `env_seeded` 行を誤って拾わないようにする）。"""
    if not _try_init():
        pytest.skip("DB down")
    _insert_raw_audit("system_settings.env_seeded", {"ollama_url": "legacy-fp-missing.internal:11434"})
    _set_raw_ollama_url("http://legacy-fp-missing.internal:11434")
    reason = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-legacy-format")
    assert reason == "skipped_unproven"
    assert "legacy-fp-missing.internal:11434" not in (store.get_system_settings().get("ollama_allowlist") or [])


def test_catchup_v2_is_one_time_only_and_does_not_revive_admin_deletion():
    """一度評価された後（marker が立った後）は、admin がそのホストを allowlist から削除しても
    再評価・復活させない（版付き移行の一度きり性）。"""
    if not _try_init():
        pytest.skip("DB down")
    store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434"}, guard_key="unused-marker-4")
    reason1 = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-4")
    assert reason1 == "added"
    store.set_system_settings("admin-uid", {"ollama_allowlist": []})   # admin が後で削除
    reason2 = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-4")
    assert reason2 == "already_present"   # marker 既存＝再評価しない
    assert store.get_system_settings().get("ollama_allowlist") == []   # 復活しない


def test_catchup_v2_records_reasoned_audit_before_marker():
    """判定理由（`added`/`skipped_unproven`）を持つ監査が、marker 挿入の直前に必ず記録される。"""
    if not _try_init():
        pytest.skip("DB down")
    store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434"}, guard_key="unused-marker-5")
    store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-5")
    rows = store.list_audit(action="system_settings.env_seed_catchup", limit=5)
    assert rows and rows[0]["after_state"]["reason"] == "added"


def test_catchup_v2_audit_insert_failure_rolls_back_marker_and_allowlist(monkeypatch):
    """重大バグ是正（RV 4巡目 #3・#12b）: 監査 INSERT が失敗すると、同一トランザクションの
    marker・allowlist 追記も一緒にロールバックされる（監査記録の無い marker/追記が確定しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434"}, guard_key="unused-marker-7")

    monkeypatch.setattr(store, "_audit_insert", _boom)
    with pytest.raises(RuntimeError):
        store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-7")
    monkeypatch.undo()

    store._invalidate_system_settings_cache()
    stored = store.get_system_settings()
    assert stored.get("catchup-v2-test-7") is None, "監査失敗時に marker が commit されている"
    assert "central.internal:11434" not in (stored.get("ollama_allowlist") or []), \
        "監査失敗時に allowlist への追記が commit されている"
    # ロールバック後に再実行すれば通常どおり評価・追加できる（マーカーが残留していない証拠）。
    reason = store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-7")
    assert reason == "added"


def test_catchup_v2_preserves_concurrent_admin_allowlist_row_via_insert_then_lock():
    """重大バグ是正（RV 4巡目 #3）: `ollama_allowlist` 行を先に確保してから `FOR UPDATE` するため、
    admin が catch-up の**直前**に allowlist を初期作成していても、その値を古い空配列で
    上書きしない（行未作成のまま `FOR UPDATE` すると何もロックできず、後勝ちで消えていた旧穴）。"""
    if not _try_init():
        pytest.skip("DB down")
    store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434"}, guard_key="unused-marker-6")
    store.set_system_settings("admin-uid", {"ollama_allowlist": ["10.9.9.9:11434"]})
    # 上の set_system_settings が「admin 操作」として記録されるため、この後の catch-up は
    # fail-closed で追加しない（#2 の証明契約どおり）が、admin が入れた値自体は消えない。
    store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-6")
    assert store.get_system_settings()["ollama_allowlist"] == ["10.9.9.9:11434"]


def test_catchup_v2_blocks_on_shared_advisory_lock_held_by_concurrent_writer():
    """重大バグ是正（RV 4巡目 #2・#12c）: catch-up と他の system_settings 複数行書き込み
    （admin PUT・env シード）は同じ advisory lock（`_ENV_SEED_LOCK`）を共有するため、片方が
    保持している間はもう片方が実際にロック待ちへ入る（別々の行ロック順序に依存しない・
    デッドロックの芽を構造的に塞ぐ）ことを、真の並行スレッド＋`pg_blocking_pids` で観測して
    固定する（値の一致や逐次実行の代理観測ではなく、実際のブロッキングを直接確認する）。"""
    if not _try_init():
        pytest.skip("DB down")
    store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434"}, guard_key="unused-marker-8")

    holder_conn = store._connect()
    monitor_conn = store._connect()
    try:
        holder_pid = holder_conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        # 他の system_settings 複数行書き込み（`set_system_settings`／`seed_system_settings_once`）が
        # 使うのと同じ advisory lock を、テスト側が先に保持する。
        holder_conn.execute("SELECT pg_advisory_xact_lock(%s)", (store.settings._ENV_SEED_LOCK,))

        thread_done = threading.Event()
        errors: list[Exception] = []
        results: list[str] = []

        def worker():
            try:
                results.append(
                    store.catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key="catchup-v2-test-8"))
            except Exception as e:   # pragma: no cover - 診断用（assert で検出する）
                errors.append(e)
            finally:
                thread_done.set()

        t = threading.Thread(target=worker)
        t.start()

        deadline = time.monotonic() + 5.0
        worker_pid = None
        while time.monotonic() < deadline:
            rows = monitor_conn.execute(
                "SELECT pid FROM pg_stat_activity "
                "WHERE wait_event_type='Lock' AND datname = current_database() "
                "  AND %s = ANY(pg_blocking_pids(pid))",
                (holder_pid,)).fetchall()
            monitor_conn.rollback()
            if len(rows) == 1:
                worker_pid = rows[0]["pid"]
                break
            if len(rows) > 1:
                raise AssertionError(
                    f"holder（pid={holder_pid}）にブロックされているバックエンドが複数見つかった: "
                    f"{[r['pid'] for r in rows]}")
            time.sleep(0.05)

        assert worker_pid is not None, \
            "worker が advisory lock 待ちへ入ったことを観測できなかった（lock 共有が効いていない）"
        assert not thread_done.is_set(), \
            "ロック待ちを観測した直後にスレッドが完了扱いになっている（矛盾・診断ロジック不整合）"

        holder_conn.commit()   # ここでロック解放＝worker が進める

        assert thread_done.wait(timeout=5), "commit 後もスレッドが完了しなかった"
        t.join(timeout=5)
        assert not t.is_alive()
        assert not errors, f"スレッドで例外: {errors}"
        assert results == ["added"]
    finally:
        holder_conn.close()
        monitor_conn.close()


def test_catchup_v2_and_set_system_settings_concurrent_real_writers_preserve_values():
    """重大バグ是正（RV 5巡目 #12・6巡目 #12 で false green を是正）: 最初の版はテスト自身が
    advisory lock を直接保持する代理観測だった。次の版は実 writer を `threading.Barrier` だけで
    「ほぼ同時」に走らせていたが、(a) 無関係な `sub_planner` だけを更新しており
    （`ollama_url`／`ollama_allowlist` 行には一切触れない＝共有 lock を削除しても行ロック競合が
    発生せず通る）、(b) `threading.Barrier` は開始タイミングを揃えるだけで、実際にロック待ちへ
    入ったこと自体は保証しない＝スレッド起動の実行速度差で競合そのものが再現されないことがある
    （`bedrock_verified_models` の並行テストが同じ理由で Barrier 方式から `pg_blocking_pids`
    方式へ是正した教訓と同型・実行のたびに再現するとは限らない非決定的な false green の余地）、
    "最終値の一致"も両者が無関係な値を書く以上は無意味だった。

    ここでは catchup と**同じ2行を逆順**で触る admin 書込み（`set_system_settings` は
    `ollama_allowlist`→`ollama_url` の順・catchup は `ollama_url`→`ollama_allowlist` の順・
    `_ENV_SEED_LOCK` の定義コメント参照）を、テスト側が先に共有 lock を保持した状態で**両方とも**
    実際に起動し、`pg_blocking_pids` で「実 writer 2つが両方ともこの1つの lock で本当に
    ブロックされている」ことを直接観測してから解放する（値の一致や Barrier の代理観測ではない）。
    解放後どちらが先に確定しても（`set_system_settings` の `ollama_allowlist` 更新は完全上書き・
    catchup は seed 後の admin 更新を改ざんとして検知して何も書かない）最終値は admin の書込みに
    収束する＝決定的に固定できる。"""
    if not _try_init():
        pytest.skip("DB down")
    store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434"}, guard_key="unused-marker-9")

    holder_conn = store._connect()
    monitor_conn = store._connect()
    try:
        holder_pid = holder_conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        holder_conn.execute("SELECT pg_advisory_xact_lock(%s)", (store.settings._ENV_SEED_LOCK,))

        errors: list[Exception] = []
        results: dict = {}

        def writer():
            try:
                # ollama_allowlist→ollama_url の順（admin PUT と同じ・catchup とは逆順・
                # `_ENV_SEED_LOCK` 定義コメント参照）。
                store.set_system_settings("admin-uid", {
                    "ollama_allowlist": ["10.9.9.9:11434"],
                    "ollama_url": "http://writer-set.internal:11434",
                })
            except Exception as e:   # pragma: no cover - 診断用（assert で検出する）
                errors.append(e)

        def catcher():
            try:
                results["reason"] = store.catchup_ollama_allowlist_for_env_seeded_url_v2(
                    guard_key="catchup-v2-test-9")
            except Exception as e:   # pragma: no cover - 診断用（assert で検出する）
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=catcher)
        t1.start()
        t2.start()

        deadline = time.monotonic() + 5.0
        blocked_pids: set[int] = set()
        while time.monotonic() < deadline and len(blocked_pids) < 2:
            rows = monitor_conn.execute(
                "SELECT pid FROM pg_stat_activity "
                "WHERE wait_event_type='Lock' AND datname = current_database() "
                "  AND %s = ANY(pg_blocking_pids(pid))",
                (holder_pid,)).fetchall()
            monitor_conn.rollback()
            blocked_pids = {r["pid"] for r in rows}
            if len(blocked_pids) < 2:
                time.sleep(0.05)

        assert len(blocked_pids) == 2, \
            (f"実 writer 2つが holder（pid={holder_pid}）に両方ともブロックされていることを"
             f"観測できなかった（観測できた分: {blocked_pids}・lock 共有が効いていない）")

        holder_conn.commit()   # ここでロック解放＝2つの writer が進める

        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive(), "解放後もスレッドが完了しなかった"
        assert not errors, f"スレッドで例外: {errors}"
        assert results.get("reason") in ("added", "skipped_unproven")
        # admin の書込みは、どちらが先に確定してもここに残る（上記docstring参照）。値まで確認する
        # （Barrier 版は allowlist の中身を一切見ていなかった）。
        stored = store.get_system_settings()
        assert stored.get("ollama_url") == "http://writer-set.internal:11434"
        assert stored.get("ollama_allowlist") == ["10.9.9.9:11434"]
    finally:
        holder_conn.close()
        monitor_conn.close()


# ===== WEB-1: Codex の Web 検索を管理者が許可する（system_settings.web_search_allowed）=====

def test_admin_settings_web_search_allowed_round_trip_and_gates():
    """GET は既定 false・PUT で true/false を往復・null で既定へ戻る・StrictBool 検証・admin 限定。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin, _ = _admin_client()

    r0 = admin.get("/admin/settings")
    assert r0.status_code == 200, r0.text
    assert r0.json()["cloud"]["web_search_allowed"] is False   # 未設定＝既定 false

    r1 = admin.put("/admin/settings", json={"web_search_allowed": True})
    assert r1.status_code == 200, r1.text
    assert r1.json()["cloud"]["web_search_allowed"] is True

    r_get = admin.get("/admin/settings")
    assert r_get.json()["cloud"]["web_search_allowed"] is True

    r2 = admin.put("/admin/settings", json={"web_search_allowed": None})
    assert r2.status_code == 200, r2.text
    assert r2.json()["cloud"]["web_search_allowed"] is False   # 未設定へ戻る（既定 false）

    # StrictBool: 非 bool（文字列 "yes" 等）は pydantic 自身が 422 にする。
    assert admin.put("/admin/settings", json={"web_search_allowed": "yes"}).status_code == 422
    assert admin.put("/admin/settings", json={"web_search_allowed": 1}).status_code == 422
    assert admin.put("/admin/settings", json={"web_search_allowed": "true"}).status_code == 422

    # 非 admin は 403（既存の _require_admin 契約）。未ログインは 401。
    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get("/admin/settings").status_code == 401
    assert anon.put("/admin/settings", json={"web_search_allowed": True}).status_code == 401
    uid, pw = f"websrchusr{sfx}", f"WebSrchUsr{sfx}"
    _mk_user(uid, pw, role="user")
    user = _login(uid, pw)
    assert user.get("/admin/settings").status_code == 403
    assert user.put("/admin/settings", json={"web_search_allowed": True}).status_code == 403


# ===== A6: personal_api_keys_allowed=false → 個人キーの一括削除 =====
# OFF のとき個人キーは保存されない状態を保つ。

def test_put_personal_keys_off_purges_all_users_and_audits_count():
    """personal_api_keys_allowed を false で保存すると、全ユーザーの個人秘密キー（openai/gemini/
    bedrock）が NULL になり、監査ログに削除件数が記録される。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    sfx = _sfx()
    u1, u2 = f"pkoff1{sfx}", f"pkoff2{sfx}"
    for uid in (u1, u2):
        _mk_user(uid, f"pw-{uid}")
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    store.update_settings(u1, openai_api_key="sk-u1", gemini_api_key="gk-u1")
    store.update_settings(u2, bedrock_api_key="bk-u2")
    assert store.get_settings(u1)["openai_api_key"] == "sk-u1"

    r = admin.put("/admin/settings", json={"personal_api_keys_allowed": False})
    assert r.status_code == 200, r.text

    assert store.get_settings(u1)["openai_api_key"] is None
    assert store.get_settings(u1)["gemini_api_key"] is None
    assert store.get_settings(u2)["bedrock_api_key"] is None

    rows = store.list_audit(action="user_settings.personal_keys_purged", limit=5)
    assert rows, "監査行が記録されていない"
    assert rows[0]["detail"]["count"] >= 2


def test_put_personal_keys_stays_true_does_not_purge():
    """personal_api_keys_allowed を true のまま（または true へ）保存しても、個人キーは削除されない。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    sfx = _sfx()
    u1 = f"pkon1{sfx}"
    _mk_user(u1, f"pw-{u1}")
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    store.update_settings(u1, openai_api_key="sk-keep")

    r = admin.put("/admin/settings", json={"personal_api_keys_allowed": True})
    assert r.status_code == 200, r.text
    assert store.get_settings(u1)["openai_api_key"] == "sk-keep"


def test_put_personal_keys_off_is_idempotent_no_audit_on_repeat_with_nothing_to_purge():
    """既に全員キーが無い状態で false を再度保存しても、監査行は増えない（0件は記録しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r1 = admin.put("/admin/settings", json={"personal_api_keys_allowed": False})
    assert r1.status_code == 200
    after1 = len(store.list_audit(action="user_settings.personal_keys_purged", limit=1000))
    r2 = admin.put("/admin/settings", json={"personal_api_keys_allowed": False})
    assert r2.status_code == 200
    after2 = len(store.list_audit(action="user_settings.personal_keys_purged", limit=1000))
    assert after2 == after1   # 2回目は削除対象0件＝監査行が増えない


def test_admin_settings_get_includes_personal_keys_in_use_count():
    """GET /admin/settings の cloud.personal_keys_in_use_count は個人キー保有ユーザー数を返す
    （保存前の確認ダイアログ用プレビュー）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    sfx = _sfx()
    u1 = f"pkcnt1{sfx}"
    _mk_user(u1, f"pw-{u1}")
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    store.update_settings(u1, openai_api_key="sk-count-me")

    r = admin.get("/admin/settings")
    assert r.status_code == 200
    assert r.json()["cloud"]["personal_keys_in_use_count"] >= 1


def test_purge_personal_api_keys_idempotent():
    """`store.purge_personal_api_keys` を連続で呼んでも、2回目は0件（既に消えているため対象が無い・
    監査行も増えない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    u1 = f"pkidem1{sfx}"
    _mk_user(u1, f"pw-{u1}")
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    store.update_settings(u1, openai_api_key="sk-idem")

    n1 = store.purge_personal_api_keys(actor="test")
    assert n1 >= 1
    n2 = store.purge_personal_api_keys(actor="test")
    assert n2 == 0


def test_startup_purge_deletes_when_flag_false_and_skips_when_true():
    """`api._purge_personal_keys_if_disabled_on_startup` は実際に false のときだけ個人キーを削除し、
    true なら残す（既存 DB へのアップグレード直後を想定した起動時の後方互換パス・実 DB での検証）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import api
    sfx = _sfx()
    u1, u2 = f"pkstart1{sfx}", f"pkstart2{sfx}"
    _mk_user(u1, f"pw-{u1}")
    _mk_user(u2, f"pw-{u2}")
    store.set_system_settings("test", {"personal_api_keys_allowed": True})
    store.update_settings(u1, openai_api_key="sk-start-false")

    store.set_system_settings("test", {"personal_api_keys_allowed": False})
    api._purge_personal_keys_if_disabled_on_startup()
    assert store.get_settings(u1)["openai_api_key"] is None

    store.set_system_settings("test", {"personal_api_keys_allowed": True})
    store.update_settings(u2, openai_api_key="sk-start-true")
    api._purge_personal_keys_if_disabled_on_startup()
    assert store.get_settings(u2)["openai_api_key"] == "sk-start-true"


def test_update_settings_personal_key_write_serializes_with_purge_via_shared_lock():
    """個人キーを含む `store.update_settings` の書き込みと、管理者の A6 無効化に伴う purge は
    同じ advisory lock（`_PERSONAL_KEY_LOCK`）を共有する。先に purge 側がロックを保持している間、
    後続の個人キー書き込みは実際にロック待ちへ入り（`pg_blocking_pids` で直接観測）、purge の
    コミット後にロックを取得してから改めて `personal_api_keys_allowed` を読み直すため、呼び出し
    時点では true だったとしても、purge 後に書き込みへ進むと `PersonalKeysDisallowedError` で
    拒否される（古い true を掴んだ書き込みが purge の後にすり抜けて個人キーが残ってしまう
    競合の直接検証・値の一致や逐次実行の代理観測ではなく実際のブロッキングを確認する）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    u1 = f"pklockrace{sfx}"
    _mk_user(u1, f"pw-{u1}")
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})

    holder_conn = store._connect()
    monitor_conn = store._connect()
    try:
        holder_pid = holder_conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        holder_conn.execute("SELECT pg_advisory_xact_lock(%s)", (store.settings._PERSONAL_KEY_LOCK,))
        # purge はロック保持中に A6 を無効化した後で走る想定（後続スレッドは依然 true を
        # 前提に update_settings を呼ぶ＝「古い true を掴んだ」状態を再現する）。
        store.set_system_settings("admin-uid", {"personal_api_keys_allowed": False})

        thread_done = threading.Event()
        errors: list[Exception] = []
        raised: list[Exception] = []

        def worker():
            try:
                store.update_settings(u1, openai_api_key="sk-stale-race")
            except store.PersonalKeysDisallowedError as e:
                raised.append(e)
            except Exception as e:   # pragma: no cover - 診断用（assert で検出する）
                errors.append(e)
            finally:
                thread_done.set()

        t = threading.Thread(target=worker)
        t.start()

        deadline = time.monotonic() + 5.0
        worker_pid = None
        while time.monotonic() < deadline:
            rows = monitor_conn.execute(
                "SELECT pid FROM pg_stat_activity "
                "WHERE wait_event_type='Lock' AND datname = current_database() "
                "  AND %s = ANY(pg_blocking_pids(pid))",
                (holder_pid,)).fetchall()
            monitor_conn.rollback()
            if len(rows) == 1:
                worker_pid = rows[0]["pid"]
                break
            if len(rows) > 1:
                raise AssertionError(
                    f"holder（pid={holder_pid}）にブロックされているバックエンドが複数見つかった: "
                    f"{[r['pid'] for r in rows]}")
            time.sleep(0.05)

        assert worker_pid is not None, \
            "worker が advisory lock 待ちへ入ったことを観測できなかった（lock 共有が効いていない）"
        assert not thread_done.is_set(), \
            "ロック待ちを観測した直後にスレッドが完了扱いになっている（矛盾・診断ロジック不整合）"

        holder_conn.commit()   # ここでロック解放＋A6=false が確定＝worker が進める

        assert thread_done.wait(timeout=5), "commit 後もスレッドが完了しなかった"
        t.join(timeout=5)
        assert not t.is_alive()
        assert not errors, f"スレッドで例外: {errors}"
        assert len(raised) == 1, f"PersonalKeysDisallowedError が発生しなかった: raised={raised}"
        assert store.get_settings(u1)["openai_api_key"] is None, \
            "purge 後に個人キーが書き込まれてしまっている（競合が塞がれていない）"
    finally:
        holder_conn.close()
        monitor_conn.close()


def test_put_settings_personal_key_race_returns_422_with_detail_via_real_http(monkeypatch):
    """上のテストは store 層で `PersonalKeysDisallowedError` の送出そのものを直接確認しているが、
    `PUT /settings` の実際の利用者はその例外を見ない＝`settings_put` が 422 へ変換した HTTP 応答
    しか見えない。ここでは「`settings_put` 自身の事前チェックは古い（真の）A6=true のスナップ
    ショットを掴んだが、`update_settings()` の書込み直前の実 DB 再確認では既に A6=false へ
    変わっていた」という競合を、事前チェック側だけ monkeypatch で古いスナップショットを固定し
    （実際の競合の型そのものは上のテストが `pg_blocking_pids` で直接証明済み）、実 DB は
    最初から false のままにして、実際の HTTP 経路（TestClient・ログイン済みセッション）で
    再現する。store 例外が正しく HTTPException(422, 利用者向け文言) へ変換されて返ることを固定する
    （store 例外→HTTP 422/detail 文言の変換契約そのものへの回帰防止）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    u1, pw1 = f"pkhttprace{sfx}", f"pw-{sfx}"
    _mk_user(u1, pw1)
    c1 = _login(u1, pw1)
    # 実 DB は最初から A6=false（`update_settings()` の再確認が実際に見る値）。
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": False})
    # settings_put 自身の事前チェック（`sys_s = store.get_system_settings()`）だけを、
    # 古い（本来はもう無効な）true のスナップショットに固定する。
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"personal_api_keys_allowed": True})

    r = c1.put("/settings", json={"openai_api_key": "sk-http-race"})

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "個人 API キーは無効化されています（管理者が中央設定でキーを管理します）", \
        f"利用者向け文言が想定と異なる: {r.text}"
    assert store.get_settings(u1)["openai_api_key"] is None, \
        "古い true を掴んだ PUT が個人キーを書き込んでしまっている（実 DB 再確認が効いていない）"


def test_keyless_update_settings_does_not_resurrect_key_after_real_purge(monkeypatch):
    """キーを一切含まない `store.update_settings`（例: `codex_reasoning` だけの保存）は、実行開始時に
    読んだ現在値のスナップショット（`cur`）を全列 UPSERT で書き戻すのではなく、個人キー3列を SQL の
    SET から丸ごと除外する（部分更新・`update_settings` docstring 参照）。この呼び出しが古いキーを
    読んでから実際に書き込むまでの間に、管理者の本物の `store.purge_personal_api_keys()` が割り込んでも、
    このキー無し保存はキー列を一切触らない＝purge が消したキーを書き戻せない（タイミングに依存しない
    構造的な保証であることを、実際にその interleave を強制して固定する）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa.store import settings as settings_mod

    sfx = _sfx()
    u1 = f"pkkeyless{sfx}"
    _mk_user(u1, f"pw-{u1}")
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    store.update_settings(u1, openai_api_key="sk-will-be-purged")
    assert store.get_settings(u1)["openai_api_key"] == "sk-will-be-purged"

    real_get_settings = settings_mod.get_settings
    reader_entered = threading.Event()
    proceed = threading.Event()

    def _blocking_get_settings(user_id="admin"):
        result = real_get_settings(user_id)
        if user_id == u1:
            reader_entered.set()
            assert proceed.wait(timeout=5), "テスト側が purge を先に進めなかった"
        return result

    # `update_settings` はモジュール内で `get_settings(user_id)` を直接呼ぶ（同一モジュールの
    # グローバル名参照＝関数属性の monkeypatch が効く）。これで「キーを読んだ直後・書込み前」の
    # 窓を意図的に広げ、その間に本物の purge を挟めるようにする。
    monkeypatch.setattr(settings_mod, "get_settings", _blocking_get_settings)

    errors: list[Exception] = []

    def worker():
        try:
            store.update_settings(u1, codex_reasoning="medium")
        except Exception as e:   # pragma: no cover - 診断用（assert で検出する）
            errors.append(e)

    t = threading.Thread(target=worker)
    t.start()
    assert reader_entered.wait(timeout=5), \
        "update_settings が cur の読取（キーがまだ purge されていない状態）へ入らなかった"

    n = store.purge_personal_api_keys(actor="test")
    assert n >= 1, "purge が対象を見つけられなかった（テスト前提が崩れている）"

    proceed.set()   # worker の update_settings を、purge 完了後の状態で書込みへ進ませる
    t.join(timeout=5)
    assert not t.is_alive()
    assert not errors, f"worker で例外: {errors}"

    fetched = store.get_settings(u1)
    assert fetched["openai_api_key"] is None, \
        "キー無し保存が purge 後に古いキーを書き戻した（部分更新が効いていない）"
    assert fetched["codex_reasoning"] == "medium", "本来の保存対象（codex_reasoning）が反映されていない"


def _raw_key_columns(uid: str) -> dict:
    """`user_settings` の個人キー3列を DB から直接読む（`get_settings()` の既定値マージを経由
    しない・行そのものの実値を見る）。"""
    with store._connect() as c:
        return c.execute(
            "SELECT openai_api_key, gemini_api_key, bedrock_api_key FROM user_settings WHERE user_id=%s",
            (uid,)).fetchone()


@pytest.mark.parametrize("target_field", ["openai_api_key", "gemini_api_key", "bedrock_api_key"])
def test_update_settings_key_columns_partial_update_boundary(target_field):
    """個人キー3列の部分更新の境界を DB 直接読取で固定する（`update_settings` docstring 参照）。
    (1) 新規行（初回 INSERT・キーを一切指定しない保存）は3列とも NULL のまま。
    (2) 3列とも実キーで埋めた後、対象1列だけを明示クリア（""）すると、その列だけ NULL になり、
    他2列は SQL 上一切 SET されない＝無関係な保存で消えたり書き換わったりしない。"""
    if not _try_init():
        pytest.skip("DB down")
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    sfx = _sfx()
    u1 = f"pkboundary{target_field[:3]}{sfx}"
    _mk_user(u1, f"pw-{u1}")

    # (1) 新規行（キー一切未指定）は3列とも NULL。
    store.update_settings(u1, codex_reasoning="medium")
    row = _raw_key_columns(u1)
    assert row is not None, "新規行が作成されなかった"
    assert row["openai_api_key"] is None
    assert row["gemini_api_key"] is None
    assert row["bedrock_api_key"] is None

    # 3列とも実キーで埋める。
    expected = {"openai_api_key": "sk-o", "gemini_api_key": "sk-g", "bedrock_api_key": "sk-b"}
    store.update_settings(u1, **expected)
    row = _raw_key_columns(u1)
    assert row["openai_api_key"] == "sk-o"
    assert row["gemini_api_key"] == "sk-g"
    assert row["bedrock_api_key"] == "sk-b"

    # (2) target_field だけを明示クリア（""）→ その列だけ NULL、他2列は元の値と完全一致のまま
    # （SQL 上一切 SET されない＝無関係な保存で消えたり書き換わったりしない）。
    store.update_settings(u1, **{target_field: ""})
    row = _raw_key_columns(u1)
    other_fields = [f for f in ("openai_api_key", "gemini_api_key", "bedrock_api_key") if f != target_field]
    assert row[target_field] is None, f"{target_field} が明示クリアされていない"
    for f in other_fields:
        assert row[f] == expected[f], \
            f"{f} が無関係な保存で元の値（{expected[f]!r}）から変わってしまった: {row[f]!r}"


# ===== model_catalog.seed_catalog_once（実 DB での初回シードの意味論） =====

def test_seed_catalog_once_inserts_when_absent_and_skips_on_second_call():
    """未設定なら `model_catalog`＋完了マーカーが1回だけ書き込まれ、2回目は何もしない
    （`sherpa.api._seed_settings_from_env` と同じ「一度だけ」方式・実 DB での検証）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import model_catalog
    assert store.get_system_settings().get(model_catalog._CATALOG_SEED_MARKER_KEY) is None
    model_catalog.seed_catalog_once()
    sysset = store.get_system_settings()
    assert sysset.get(model_catalog._CATALOG_SEED_MARKER_KEY) == model_catalog._CATALOG_SEED_VERSION
    assert sysset["model_catalog"]["openai"]["chat"]["default"] == "gpt-5.5"

    # 2回目: 管理者が既にカタログを編集していても上書きしない。
    store.set_system_settings("admin-uid", {"model_catalog": {"openai": {"chat": {
        "allowed": ["admin-edited"], "default": "admin-edited"}}}})
    model_catalog.seed_catalog_once()
    assert store.get_system_settings()["model_catalog"]["openai"]["chat"]["default"] == "admin-edited"


def test_seed_catalog_once_reads_openai_embed_model_env(monkeypatch):
    """`OPENAI_EMBED_MODEL` env は初回シード時にのみ openai/embed の既定へ取り込まれる
    （以後 env は読まない＝`sherpa/embeddings.py` は `model_catalog.resolve_model` 経由で解決する）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import model_catalog
    monkeypatch.setenv("OPENAI_EMBED_MODEL", "my-embed-deployment")
    model_catalog.seed_catalog_once()
    cell = store.get_system_settings()["model_catalog"]["openai"]["embed"]
    assert cell["default"] == "my-embed-deployment"
    assert "my-embed-deployment" in cell["allowed"]


def test_settings_put_shares_one_system_settings_snapshot(monkeypatch):
    """`settings_put` は `sys_s = store.get_system_settings()` を入口で1回取得し、A6
    （`personal_keys_allowed`）判定・A7（`agent_requires_unselected_cloud`→`selected_cloud_provider`）
    判定・`ollama_url` の SSRF/allowlist 検証（`assert_ollama_url_allowed`→`_allowlisted_hosts`）
    へそれを渡す契約になっている（個別に読み直すと、1リクエストの検証中に admin 更新が挟まった
    場合に判定が新旧混在しうる）。ここでは agent=openai（A7 経路）と ollama_url=loopback
    （SSRF/allowlist 経路）を同一 PUT に含め、両方が `settings_put` 自身の system_settings と
    同一オブジェクトで判定されることを固定する。

    `settings_put` は保存後に `_public_settings()`（レスポンス構築・保存直後の実態を反映する
    ため意図的に別スナップショットを読み直す・別スコープ＝`test_public_settings_snapshot.py` で
    固定済み）を呼ぶため、各ヘルパーの**最初の**呼び出し（＝保存前の検証フェーズ）だけを比較する
    （後続の呼び出しは `_public_settings` 側の別スナップショット由来で一致しなくてよい）。
    最初の呼び出しは `system_settings=None`（未伝播の回帰）であっても記録する＝スキップして
    後続の正しく伝播した呼び出しに取って代わられることを許すと、先頭呼び出しの未伝播という
    回帰そのものを見逃す false green になる。`store.get_system_settings()` が呼ばれるたびに
    別オブジェクトを返すよう仕込むことで、「たまたま同じ値」ではなく「本当に同じオブジェクトを
    受け取ったか」を検出可能にする。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import keys, llm

    sfx = _sfx()
    uid, pw = f"snapput{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    real_get_system_settings = store.get_system_settings
    call_id = {"n": 0}

    def _tagged_each_call():
        call_id["n"] += 1
        d = dict(real_get_system_settings())
        d["_call_id"] = call_id["n"]
        return d

    monkeypatch.setattr(store, "get_system_settings", _tagged_each_call)

    first_call_value: dict[str, dict | None] = {}

    def _spy(name, real, extract):
        def _wrapped(*args, **kwargs):
            # 最初の呼び出しは system_settings=None（未伝播）でも記録する。None を読み飛ばして
            # 後続の正しく伝播した呼び出しで上書きすると、先頭呼び出しの未伝播という回帰自体を
            # 見逃してしまう（false green）。
            if name not in first_call_value:
                first_call_value[name] = extract(args, kwargs)
            return real(*args, **kwargs)
        return _wrapped

    monkeypatch.setattr(keys, "personal_keys_allowed", _spy(
        "personal_keys_allowed", keys.personal_keys_allowed,
        lambda a, kw: kw.get("system_settings", a[0] if a else None)))
    monkeypatch.setattr(keys, "selected_cloud_provider", _spy(
        "selected_cloud_provider", keys.selected_cloud_provider,
        lambda a, kw: kw.get("system_settings", a[0] if a else None)))
    monkeypatch.setattr(llm, "_allowlisted_hosts", _spy(
        "_allowlisted_hosts", llm._allowlisted_hosts,
        lambda a, kw: kw.get("system_settings", a[0] if a else None)))

    r = c.put("/settings", json={"agent": "openai", "ollama_url": "http://localhost:11434"})
    assert r.status_code == 200, r.text

    required = {"personal_keys_allowed", "selected_cloud_provider", "_allowlisted_hosts"}
    missing = required - set(first_call_value)
    assert not missing, f"呼ばれなかったヘルパー: {missing}"
    for name, v in first_call_value.items():
        assert v is not None, f"{name} の最初の呼び出しが system_settings=None だった（未伝播の回帰）"
    ids = {v["_call_id"] for v in first_call_value.values()}
    assert len(ids) == 1, f"検証フェーズのヘルパーが異なる system_settings を受け取った: {first_call_value}"


# ===== PART-4a: research_default_provider（外部連携タブ「AI 下調べ検索の既定 AI」）=====

def test_admin_settings_research_default_provider_accepts_ollama_without_preflight():
    """ollama は実送信可能性の preflight を通す必要が無い——常に受理される。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"research_default_provider": "ollama"})
    assert r.status_code == 200, r.text
    rdp = r.json()["ext_keys"]["research_default_provider"]
    assert rdp == {"configured": "ollama", "effective": "ollama", "default": "ollama"}


def test_admin_settings_research_default_provider_accepts_openai_when_key_present_in_same_put():
    """同一 PUT で openai_api_key も一緒に指定すれば、この PUT 適用後の実効設定
    （現在値へ updates を重ねたもの）で preflight が通り 200 になる。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={
        "openai_api_key": "sk-test-real-key-1234567890",
        "research_default_provider": "openai"})
    assert r.status_code == 200, r.text
    rdp = r.json()["ext_keys"]["research_default_provider"]
    assert rdp["configured"] == "openai"
    assert rdp["effective"] == "openai"


def test_admin_settings_research_default_provider_rejects_unknown_value():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"research_default_provider": "gemini"})
    assert r.status_code == 422, r.text


def test_admin_settings_research_default_provider_null_resets_to_unset():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"research_default_provider": "ollama"})
    assert r.status_code == 200, r.text
    r2 = admin.put("/admin/settings", json={"research_default_provider": None})
    assert r2.status_code == 200, r2.text
    rdp = r2.json()["ext_keys"]["research_default_provider"]
    assert rdp["configured"] is None
    assert rdp["effective"] == "ollama"


def test_admin_settings_research_default_provider_audit_records_change():
    if not _try_init():
        pytest.skip("DB down")
    admin, admin_uid = _admin_client()
    r = admin.put("/admin/settings", json={"research_default_provider": "ollama"})
    assert r.status_code == 200, r.text
    rows = store.list_audit(action="system_settings.updated", actor=admin_uid, limit=10)
    assert rows, "system_settings.updated が監査に残っていない"
    assert rows[0]["after_state"].get("research_default_provider") == "ollama"


def test_admin_settings_research_default_provider_openai_without_key_rejected():
    """openai_api_key 未設定のまま research_default_provider=openai は保存できない
    （保存時点で送信不可能な組み合わせを作らせない・422・保存もされない）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"research_default_provider": "openai"})
    assert r.status_code == 422, r.text
    get_r = admin.get("/admin/settings")
    assert get_r.json()["ext_keys"]["research_default_provider"]["configured"] is None


def test_admin_settings_research_default_provider_openai_with_placeholder_key_rejected():
    """`.env.example` のプレースホルダキーは「キーあり」と誤認しない
    （`agent_constructs.is_real_api_key` 経由の判定を保存時 preflight でも使う）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r0 = admin.put("/admin/settings", json={"openai_api_key": "sk-REPLACE_ME"})
    assert r0.status_code == 200, r0.text
    r = admin.put("/admin/settings", json={"research_default_provider": "openai"})
    assert r.status_code == 422, r.text


def test_admin_settings_research_default_provider_openai_rejected_when_only_chat_deployment_configured():
    """保存時 preflight は実際に送信する用途（subsearch）のデプロイ名を検査する——chat 用途に
    だけデプロイ名を設定し subsearch 用途を未設定のままにした Azure 接続先では、下調べ検索は
    subsearch セルの組み込み既定（未解決）のまま送信されてしまうため 422 で拒否する
    （chat セルだけを見て通してしまう誤判定の固定）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r0 = admin.put("/admin/settings", json={
        "openai_api_key": "sk-test-real-key-1234567890",
        "openai_endpoint_kind": "azure",
        "openai_base_url": "https://example.openai.azure.com/openai/v1",
        "model_catalog": {"openai": {"chat": {"allowed": ["my-chat-deployment"],
                                              "default": "my-chat-deployment"}}}})
    assert r0.status_code == 200, r0.text
    r = admin.put("/admin/settings", json={"research_default_provider": "openai"})
    assert r.status_code == 422, r.text


def test_admin_settings_view_research_default_provider_shape():
    """GET /admin/settings の ext_keys.research_default_provider は
    {configured, effective, default} の3キー（差分ハイライト・タブ描画の基準値）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.get("/admin/settings")
    assert r.status_code == 200, r.text
    rdp = r.json()["ext_keys"]["research_default_provider"]
    assert set(rdp.keys()) == {"configured", "effective", "default"}
    assert rdp == {"configured": None, "effective": "ollama", "default": "ollama"}


# ===== SC-6c: 調べる深さの基準値（調べ方ブロック §3.2・admin-settings.html 基準値編集セクション）=====

def test_admin_settings_view_depth_profile_shape_unset():
    """GET /admin/settings の depth_profile は7項目（整数6＋codex_reasoning）。未設定なら
    configured=None・effective=default=各モジュールの env 既定値。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import agentic_search, impact_service, lens_service, chat_service, depth_profile
    admin, _ = _admin_client()
    r = admin.get("/admin/settings")
    assert r.status_code == 200, r.text
    dp = r.json()["depth_profile"]
    assert set(dp.keys()) == set(depth_profile.BASE_SETTINGS_KEYS)
    for key, default in (
        ("max_turns", agentic_search.MAX_TURNS), ("grep_max_hits", agentic_search.MAX_HITS),
        ("qa_max_hits", chat_service.QA_MAX_HITS_DEFAULT), ("read_window", agentic_search.READ_WINDOW),
        ("impact_depth", impact_service.IMPACT_MAX_DEPTH),
        ("troubleshoot_depth", lens_service.TROUBLESHOOT_GRAPH_DEPTH),
    ):
        assert dp[key] == {"configured": None, "effective": default, "default": default}, key
    assert dp["codex_reasoning"]["configured"] is None
    assert dp["codex_reasoning"]["effective"] == dp["codex_reasoning"]["default"]
    assert set(dp["codex_reasoning"]["options"]) == set(depth_profile.CODEX_REASONING_LEVELS)


@pytest.mark.parametrize("field,value", [
    ("depth_base_max_turns", 20), ("depth_base_grep_max_hits", 50), ("depth_base_qa_max_hits", 25),
    ("depth_base_read_window", 80), ("depth_base_impact_depth", 12), ("depth_base_troubleshoot_depth", 6),
])
def test_admin_settings_depth_base_int_put_and_get_roundtrip(field, value):
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={field: value})
    assert r.status_code == 200, r.text
    key = {"depth_base_max_turns": "max_turns", "depth_base_grep_max_hits": "grep_max_hits",
          "depth_base_qa_max_hits": "qa_max_hits", "depth_base_read_window": "read_window",
          "depth_base_impact_depth": "impact_depth",
          "depth_base_troubleshoot_depth": "troubleshoot_depth"}[field]
    dp = r.json()["depth_profile"][key]
    assert dp["configured"] == value and dp["effective"] == value

    r2 = admin.put("/admin/settings", json={field: None})
    assert r2.status_code == 200, r2.text
    dp2 = r2.json()["depth_profile"][key]
    assert dp2["configured"] is None and dp2["effective"] == dp2["default"]


@pytest.mark.parametrize("field,bad", [
    ("depth_base_max_turns", 0), ("depth_base_max_turns", 500),
    ("depth_base_grep_max_hits", 0), ("depth_base_read_window", 5),
    ("depth_base_impact_depth", 100), ("depth_base_troubleshoot_depth", 0),
])
def test_admin_settings_depth_base_int_rejects_out_of_range(field, bad):
    """StrictInt+Field(ge,le) の範囲外は 422（保存もされない）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={field: bad})
    assert r.status_code == 422, r.text


def test_admin_settings_depth_base_int_rejects_non_integer():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"depth_base_max_turns": "twelve"})
    assert r.status_code == 422, r.text
    r2 = admin.put("/admin/settings", json={"depth_base_max_turns": True})   # StrictInt は bool を拒否
    assert r2.status_code == 422, r2.text


def test_admin_settings_depth_base_codex_reasoning_put_and_get_roundtrip():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"depth_base_codex_reasoning": "high"})
    assert r.status_code == 200, r.text
    cr = r.json()["depth_profile"]["codex_reasoning"]
    assert cr["configured"] == "high" and cr["effective"] == "high"

    r2 = admin.put("/admin/settings", json={"depth_base_codex_reasoning": None})
    assert r2.status_code == 200, r2.text
    cr2 = r2.json()["depth_profile"]["codex_reasoning"]
    assert cr2["configured"] is None and cr2["effective"] == cr2["default"]


def test_admin_settings_depth_base_codex_reasoning_rejects_unknown_value():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"depth_base_codex_reasoning": "ultra"})
    assert r.status_code == 422, r.text


def test_admin_settings_depth_base_audit_records_change():
    if not _try_init():
        pytest.skip("DB down")
    admin, admin_uid = _admin_client()
    r = admin.put("/admin/settings", json={"depth_base_max_turns": 30})
    assert r.status_code == 200, r.text
    rows = store.list_audit(action="system_settings.updated", actor=admin_uid, limit=10)
    assert rows, "system_settings.updated が監査に残っていない"
    assert rows[0]["after_state"].get("depth_base_max_turns") == 30


# ===== BUDGET-1（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4・env→管理者設定への昇格）=====

def test_admin_settings_view_agentic_budget_shape_unset():
    """GET /admin/settings の agentic_budget は未設定なら configured=None・
    effective=default=コード既定（精度優先・262144/4194304）。BUDGET-2（§3.4）: 新設の DB 未設定
    （fresh DB・system_settings 全消去）なら「現在のモデル」（既定は ollama/qwen2.5）の窓は
    登録値/シード表のどちらにも無い＝unknown に落ち、effective は BUDGET-1 のみの値のまま
    （退行しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import agentic_search
    admin, _ = _admin_client()
    r = admin.get("/admin/settings")
    assert r.status_code == 200, r.text
    ab = r.json()["agentic_budget"]
    assert set(ab.keys()) == {"per_result", "total", "window", "model_windows"}
    assert ab["per_result"] == {"configured": None, "effective": agentic_search.TOOL_RESULT_MAX_BYTES,
                                "default": agentic_search.TOOL_RESULT_MAX_BYTES}
    assert ab["total"] == {"configured": None, "effective": agentic_search.TOOL_RESULT_MAX_TOTAL_BYTES,
                           "default": agentic_search.TOOL_RESULT_MAX_TOTAL_BYTES}
    assert ab["window"]["source"] == "unknown"
    assert ab["window"]["window_tokens"] is None
    assert ab["window"]["derived_cap_bytes"] is None
    assert ab["model_windows"] == {"configured": None}


@pytest.mark.parametrize("field,key,value", [
    ("agentic_budget_per_result", "per_result", 100_000),
    ("agentic_budget_total", "total", 2_000_000),
])
def test_admin_settings_agentic_budget_put_and_get_roundtrip(field, key, value):
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={field: value})
    assert r.status_code == 200, r.text
    ab = r.json()["agentic_budget"][key]
    assert ab["configured"] == value and ab["effective"] == value

    r2 = admin.put("/admin/settings", json={field: None})
    assert r2.status_code == 200, r2.text
    ab2 = r2.json()["agentic_budget"][key]
    assert ab2["configured"] is None and ab2["effective"] == ab2["default"]


@pytest.mark.parametrize("field,bad", [
    ("agentic_budget_per_result", 0), ("agentic_budget_per_result", -1),
    ("agentic_budget_per_result", 1023), ("agentic_budget_per_result", 8 * 1024 * 1024 + 1),
    ("agentic_budget_total", 0), ("agentic_budget_total", -1),
    ("agentic_budget_total", 4095), ("agentic_budget_total", 64 * 1024 * 1024 + 1),
])
def test_admin_settings_agentic_budget_rejects_out_of_range(field, bad):
    """StrictInt+Field(ge,le) の範囲外（負値・過大値）は 422（保存もされない）——
    §3.4 の受け入れ条件「範囲検証（負値・過大値→400）」。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={field: bad})
    assert r.status_code == 422, r.text


def test_admin_settings_agentic_budget_rejects_non_integer():
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"agentic_budget_per_result": "big"})
    assert r.status_code == 422, r.text
    r2 = admin.put("/admin/settings", json={"agentic_budget_total": True})   # StrictInt は bool を拒否
    assert r2.status_code == 422, r2.text


def test_admin_settings_agentic_budget_audit_records_change():
    if not _try_init():
        pytest.skip("DB down")
    admin, admin_uid = _admin_client()
    r = admin.put("/admin/settings", json={"agentic_budget_per_result": 300_000})
    assert r.status_code == 200, r.text
    rows = store.list_audit(action="system_settings.updated", actor=admin_uid, limit=10)
    assert rows, "system_settings.updated が監査に残っていない"
    assert rows[0]["after_state"].get("agentic_budget_per_result") == 300_000


# ===== BUDGET-2（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4・2026-09-03 裁定・
# モデル窓連動・min() 方式）=====

def test_admin_settings_model_windows_put_and_get_roundtrip():
    """`model_context_windows` の追加・取得・削除（null で未設定へ戻す）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_context_windows": {"openai:my-test-model": 50_000, "ollama:qwen2.5": 32_768}})
    assert r.status_code == 200, r.text
    mw = r.json()["agentic_budget"]["model_windows"]
    assert mw == {"configured": {"openai:my-test-model": 50_000, "ollama:qwen2.5": 32_768}}

    r2 = admin.put("/admin/settings", json={"model_context_windows": None})
    assert r2.status_code == 200, r2.text
    assert r2.json()["agentic_budget"]["model_windows"] == {"configured": None}


@pytest.mark.parametrize("bad", [
    "not-a-dict",
    {"no-colon-key": 100},
    {"unknownprovider:m": 100},
    {"openai:m": 0},
    {"openai:m": -1},
    {"openai:m": True},
    {"openai:m": "50000"},
])
def test_admin_settings_model_windows_rejects_bad_shapes(bad):
    if not _try_init():
        pytest.skip("DB down")
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={"model_context_windows": bad})
    assert r.status_code == 422, r.text


def test_admin_settings_model_windows_registered_value_narrows_effective_budget(monkeypatch):
    """登録値（管理画面の「モデル窓」欄）が現在のモデルに一致すると、min() で `effective` が
    窓由来の上限まで縮む（§3.4 min() 方式の end-to-end 固定）。「現在のモデル」の解決
    （`_current_chat_provider_model`）はこのテストの主眼ではない（実行環境の `SHERPA_AGENT`
    既定値に依存させない）ため ollama/qwen2.5 に固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import model_windows
    from sherpa.routers import system_extras
    monkeypatch.setattr(system_extras, "_current_chat_provider_model", lambda sysset: ("ollama", "qwen2.5"))
    admin, _ = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_context_windows": {"ollama:qwen2.5": 40_000}})
    assert r.status_code == 200, r.text
    view = r.json()
    ab = view["agentic_budget"]
    assert ab["window"] == {"provider": "ollama", "model": "qwen2.5", "window_tokens": 40_000,
                            "source": "registered",
                            "derived_cap_bytes": model_windows.derive_window_bytes(40_000)}
    expected_cap = model_windows.derive_window_bytes(40_000)
    assert ab["per_result"]["effective"] == expected_cap
    assert ab["total"]["effective"] == expected_cap
