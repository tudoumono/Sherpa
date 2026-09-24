"""sherpa.health の単体テスト（外部サービス不要・偽 ping に差し替えて検証）。

health.COMPONENTS を偽 ping（成功/例外）に差し替えて snapshot()/summary() の集約ロジックと
TTL キャッシュを検証する。実ストアへは一切 ping しない。
"""
from __future__ import annotations

from sherpa import health

_ORIGINAL_COMPONENTS = health.COMPONENTS


def _ok():
    return None


def _fail():
    raise RuntimeError("boom")


def _patch_components(failing_ids: set[str]) -> None:
    """COMPONENTS を偽 ping に差し替える（id/label/impact/hint は元のまま維持）。

    差し替え時に _cache もリセットする（前のテストの TTL キャッシュを次のテストに
    持ち越さないため）。
    """
    health.COMPONENTS = [
        (comp_id, label, impact, (_fail if comp_id in failing_ids else _ok), hint)
        for comp_id, label, impact, _ping, hint in _ORIGINAL_COMPONENTS
    ]
    health._cache = {"at": 0.0, "data": None}


def _restore() -> None:
    health.COMPONENTS = _ORIGINAL_COMPONENTS
    health._cache = {"at": 0.0, "data": None}


def test_all_success_is_ok():
    _patch_components(set())
    try:
        s = health.snapshot(force=True)
        assert s["status"] == "ok"
        assert all(c["ok"] for c in s["components"])
        assert "hint" not in s["components"][0]
    finally:
        _restore()


def test_elasticsearch_failure_is_degraded():
    _patch_components({"elasticsearch"})
    try:
        s = health.snapshot(force=True)
        assert s["status"] == "degraded"
        es = next(c for c in s["components"] if c["id"] == "elasticsearch")
        assert es["ok"] is False
        assert es["hint"]
    finally:
        _restore()


def test_postgres_failure_is_down():
    _patch_components({"postgres"})
    try:
        s = health.snapshot(force=True)
        assert s["status"] == "down"
    finally:
        _restore()


def test_none_impact_failures_stay_ok_but_are_recorded():
    """codex/openai/ollama（impact=none）は失敗しても status には影響しないが、
    components には ok=False と hint が残る（管理画面の参考情報用）。"""
    _patch_components({"codex", "openai", "ollama"})
    try:
        s = health.snapshot(force=True)
        assert s["status"] == "ok"
        failed = {c["id"]: c for c in s["components"] if not c["ok"]}
        assert set(failed) == {"codex", "openai", "ollama"}
        for c in failed.values():
            assert c["hint"]
    finally:
        _restore()


def test_cache_ttl_and_force_refresh():
    _patch_components(set())
    try:
        s1 = health.snapshot(force=True)
        s2 = health.snapshot(force=False)
        assert s2 is s1, "TTL 内は force=False で同一（キャッシュ）結果を返すはず"

        # 差し替え後も TTL 内なら force=False はキャッシュを優先する
        # （_patch_components は _cache をリセットするため、ここでは検証したい「COMPONENTS
        # 変更だけではキャッシュは無効化されない」を壊さないよう直接差し替える）。
        health.COMPONENTS = [
            (comp_id, label, impact, (_fail if comp_id == "postgres" else _ok), hint)
            for comp_id, label, impact, _ping, hint in _ORIGINAL_COMPONENTS
        ]
        s3 = health.snapshot(force=False)
        assert s3 is s1, "TTL 内は差し替え後も force=False はキャッシュを優先するはず"

        s4 = health.snapshot(force=True)
        assert s4 is not s1, "force=True は再計算されるはず"
        assert s4["status"] == "down"
    finally:
        _restore()


def test_summary_returns_only_status_and_checked_at():
    _patch_components(set())
    try:
        s = health.summary(force=True)
        assert set(s.keys()) == {"status", "checked_at"}
        assert s["status"] == "ok"
        assert isinstance(s["checked_at"], str)
    finally:
        _restore()


def test_detail_does_not_leak_raw_exception_text():
    """detail に接続情報（DSN 等）を含む生の例外文字列を出さないことを検証する。

    raw の例外内容はサーバログにのみ出す想定（health._logger.warning）。detail には
    _classify() による短い日本語分類のみが入る。
    """
    def _fail_with_dsn():
        raise RuntimeError("postgresql://user:secretpw@host/db connection failed")

    health.COMPONENTS = [
        (comp_id, label, impact, (_fail_with_dsn if comp_id == "postgres" else _ok), hint)
        for comp_id, label, impact, _ping, hint in _ORIGINAL_COMPONENTS
    ]
    health._cache = {"at": 0.0, "data": None}
    try:
        s = health.snapshot(force=True)
        pg = next(c for c in s["components"] if c["id"] == "postgres")
        assert pg["ok"] is False
        assert "secretpw" not in pg["detail"]
        assert "エラー" in pg["detail"]
        assert pg["hint"]
    finally:
        _restore()


# ---- _ping_bedrock（Codex RV 修正: boto3/IMDS を撤去し純粋な env/ファイル存在チェックへ）----

_AWS_ENV_KEYS = ("AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_PROFILE")


def _clear_aws_env(monkeypatch) -> None:
    for k in _AWS_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _select_bedrock(monkeypatch) -> None:
    """A7: bedrock を選択中のクラウドプロバイダにする（既定 openai のままだと
    `_ping_bedrock`/`_ai_check_bedrock` が SigV4/認証情報ファイルの確認より前に early return する）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"cloud_provider": "bedrock"})


def test_ping_bedrock_does_not_reference_boto3():
    """boto3 を import/呼び出ししないこと（IMDS 等ネットワークへ出ない約束の静的確認）。

    docstring は経緯説明として "boto3" の語を含むため、生ソース文字列ではなく**コンパイル済み
    バイトコードが参照する名前**（`co_names`＝import/属性アクセス/呼び出しの対象名）で判定する。
    """
    names = health._ping_bedrock.__code__.co_names
    assert "boto3" not in names


def test_ping_bedrock_fails_closed_without_any_credentials(monkeypatch, tmp_path):
    _clear_aws_env(monkeypatch)
    _select_bedrock(monkeypatch)
    monkeypatch.setattr(health, "_aws_credentials_file", lambda: tmp_path / "no-such-file")
    try:
        health._ping_bedrock()
        assert False, "認証情報が何も無いのに例外が出なかった"
    except RuntimeError as e:
        assert "見つかりません" in str(e)


def test_ping_bedrock_ok_via_sigv4_env(monkeypatch, tmp_path):
    """SigV4 の静的な手掛かり（AWS_ACCESS_KEY_ID/AWS_PROFILE）はインフラ管理のまま env を見る。"""
    monkeypatch.setattr(health, "_aws_credentials_file", lambda: tmp_path / "no-such-file")
    for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE"):
        _clear_aws_env(monkeypatch)
        _select_bedrock(monkeypatch)
        monkeypatch.setenv(k, "dummy")
        health._ping_bedrock()                     # 例外が出なければ OK


def test_ping_bedrock_ok_via_central_key(monkeypatch, tmp_path):
    """Bearer キー系（旧 AWS_BEARER_TOKEN_BEDROCK/ANTHROPIC_AWS_API_KEY）
    はもう env を読まない。中央設定（system_settings.bedrock_api_key・cloud_provider=bedrock）経由で
    解決されることを確認する（`sherpa.keys.resolve_api_key`）。"""
    monkeypatch.setattr(health, "_aws_credentials_file", lambda: tmp_path / "no-such-file")
    _clear_aws_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"cloud_provider": "bedrock", "bedrock_api_key": "central-key"})
    health._ping_bedrock()                          # 例外が出なければ OK


def test_ping_bedrock_ok_via_credentials_file(monkeypatch, tmp_path):
    _clear_aws_env(monkeypatch)
    _select_bedrock(monkeypatch)
    cred_file = tmp_path / ".aws" / "credentials"
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text("[default]\naws_access_key_id = x\n", encoding="utf-8")
    monkeypatch.setattr(health, "_aws_credentials_file", lambda: cred_file)
    health._ping_bedrock()                          # 例外が出なければ OK（ファイル存在のみで判定）


def test_ping_bedrock_reads_system_settings_exactly_once(monkeypatch, tmp_path):
    """重大バグ是正（RV 4巡目 #7）: A7 判定（`selected_cloud_provider`）とキー解決
    （`resolve_api_key`）を別々に読み直すと、途中の admin 更新でどちらか片方だけ新しい値を
    見てしまう窓ができる。同じスナップショットを使い回し、DB 読取（`get_system_settings`）は
    1回だけであることを spy で固定する。"""
    _clear_aws_env(monkeypatch)
    monkeypatch.setattr(health, "_aws_credentials_file", lambda: tmp_path / "no-such-file")
    calls = []

    def _spy():
        calls.append(1)
        return {"cloud_provider": "bedrock", "bedrock_api_key": "central-key"}

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy)
    health._ping_bedrock()
    assert len(calls) == 1


def test_ai_check_bedrock_reads_system_settings_exactly_once(monkeypatch):
    """`_ai_check_bedrock` も同様に1回だけ読む（`_ping_bedrock` と同じ理由）。"""
    calls = []

    def _spy():
        calls.append(1)
        return {"cloud_provider": "openai"}   # bedrock 未選択＝早期 return する分岐で十分

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy)
    try:
        health._ai_check_bedrock({})
        assert False, "bedrock 未選択なのに例外が出なかった"
    except RuntimeError:
        pass
    assert len(calls) == 1


# ---- ai_snapshot（UI フィードバック4・2026-07-03: 管理者本人の設定で実接続確認） ----
# _AI_COMPONENTS を偽 check に差し替えて検証する（実 AI へは一切繋がない）。

_ORIGINAL_AI_COMPONENTS = health._AI_COMPONENTS


def _patch_ai_components(checks: dict) -> None:
    """id -> check(settings) 関数の dict で _AI_COMPONENTS を差し替える（未指定 id は成功関数のまま）。
    差し替え時に _ai_cache もリセットする。"""
    def _ok(_settings, _system_settings=None):
        return None
    health._AI_COMPONENTS = [
        (comp_id, label, impact, checks.get(comp_id, _ok), hint)
        for comp_id, label, impact, _check, hint in _ORIGINAL_AI_COMPONENTS
    ]
    health._ai_cache = {}


def _restore_ai() -> None:
    health._AI_COMPONENTS = _ORIGINAL_AI_COMPONENTS
    health._ai_cache = {}


def test_ai_components_include_gemini():
    """RV 相当の実装漏れ修正: 旧来の COMPONENTS（状態ドット用）は gemini を含んでいなかった。
    ai_snapshot の対象には gemini が含まれる。"""
    ids = [c[0] for c in health._AI_COMPONENTS]
    assert "gemini" in ids, "gemini が AI ヘルスチェックの対象に含まれていない"


def test_ai_snapshot_passes_per_user_settings_to_each_check():
    """ai_snapshot が渡された settings dict をそのまま各 check(settings) に渡す
    （管理者本人が設定画面で入れた API キーを使って実接続確認する、という設計の裏付け）。"""
    received = {}

    def _record(name):
        def _check(settings, system_settings=None):
            received[name] = settings
        return _check

    _patch_ai_components({c[0]: _record(c[0]) for c in health._AI_COMPONENTS})
    try:
        sentinel = {"openai_api_key": "sk-test-sentinel", "gemini_api_key": "AIza-test-sentinel"}
        rows = health.ai_snapshot("admin", sentinel, force=True)
        assert all(c["ok"] for c in rows)
        for name, settings in received.items():
            assert settings is sentinel, f"{name} に渡された settings が呼出元と別物になっている"
    finally:
        _restore_ai()


def test_ai_snapshot_failure_recorded_with_hint_but_no_secret_leak():
    """API キー不正等での失敗は ok=False・hint 付きで記録される。detail に渡した設定値
    （偽のキー文字列）そのものが漏れないこと。`_check_one_ai` は `_ai_check_*` の例外メッセージを
    丸めずそのまま使うが（`test_ai_snapshot_shows_detailed_reason_instead_of_generic_classification`
    参照）、`graph_extract._mask_secrets()` を多層防御として通すため、`sk-` 形式のトークンパターンは
    想定外の生の例外（本テストのように `_safe_detail` を経由しない）からでも伏せられる。"""
    def _fail(settings, system_settings=None):
        raise RuntimeError(f"401 unauthorized for key={settings.get('openai_api_key')}")

    _patch_ai_components({"openai": _fail})
    try:
        rows = health.ai_snapshot("admin", {"openai_api_key": "sk-should-not-leak"}, force=True)
        openai_row = next(c for c in rows if c["id"] == "openai")
        assert openai_row["ok"] is False
        assert openai_row["hint"]
        assert "sk-should-not-leak" not in openai_row["detail"], "detail にキー値が漏れている"
    finally:
        _restore_ai()


def test_ai_snapshot_redacts_unexpected_exception_with_url_userinfo_query_and_fragment(caplog):
    """`_ai_check_ollama`（urllib の例外をそのまま使う）・`_ai_check_codex`（subprocess の
    stderr/stdout をそのまま使う）等、`_safe_detail` を経由しない経路が URL の userinfo・
    query token・fragment を含む想定外の例外を投げても、`_check_one_ai` が detail・ログ
    （`health._logger`）の**両方**を `_redact_reflected_urls`/`_mask_secrets` で伏せる
    （生の例外はどちらにも渡さない）。"""
    def _fail(settings, system_settings=None):
        raise RuntimeError(
            "connect to https://user:s3cr3t@internal-gw.example.com:8443/path"
            "?token=leak-me#frag failed")

    _patch_ai_components({"ollama": _fail})
    try:
        with caplog.at_level("WARNING", logger="sherpa.health"):
            rows = health.ai_snapshot("admin", {}, force=True)
        row = next(c for c in rows if c["id"] == "ollama")
        assert row["ok"] is False
        for leaked in ("s3cr3t", "user:", "token=leak-me", "leak-me", "frag"):
            assert leaked not in row["detail"], f"{leaked!r} が detail に漏れている: {row['detail']!r}"
        assert "internal-gw.example.com:8443" in row["detail"]   # host[:port] は残る（安全な表現）

        logged_text = " ".join(r.getMessage() for r in caplog.records)
        for leaked in ("s3cr3t", "user:", "token=leak-me", "leak-me", "frag"):
            assert leaked not in logged_text, f"{leaked!r} がログに漏れている: {logged_text!r}"
    finally:
        _restore_ai()


def test_ai_snapshot_redacts_dsn_style_exceptions_in_detail_and_log(caplog):
    """`graph_extract._redact_reflected_urls` の URL 指標は元々 `http(s)://` 限定だったため、
    `postgresql://admin:db-secret@db.internal/app` のような DSN（http/https 以外の scheme）は
    detail・ログの双方へ全文素通りしていた。汎用 scheme（`_GENERIC_SCHEME_RE`）で検出し、
    http/https 以外は host 縮約せず丸ごと `[URL]` にする（userinfo/パスワードを確実に消す）。"""
    dsn_cases = {
        "postgresql": ("postgresql://admin:db-secret@db.internal/app", ("admin", "db-secret", "app")),
        "redis": ("redis://user:pass@cache.internal:6379/0", ("user", "pass", "6379")),
        "bolt": ("bolt://neo4j:s3cr3t@graph.internal:7687", ("neo4j", "s3cr3t", "7687")),
    }
    for comp_id, (dsn, leaked_fragments) in dsn_cases.items():

        def _fail(settings, system_settings=None, _dsn=dsn):
            raise RuntimeError(f"connect failed: {_dsn} timeout")

        _patch_ai_components({"ollama": _fail})
        try:
            with caplog.at_level("WARNING", logger="sherpa.health"):
                rows = health.ai_snapshot("admin", {}, force=True)
            row = next(c for c in rows if c["id"] == "ollama")
            assert row["ok"] is False
            for leaked in leaked_fragments:
                assert leaked not in row["detail"], (
                    f"[{comp_id}] {leaked!r} が detail に漏れている: {row['detail']!r}")
            assert row["detail"] == "connect failed: [URL] timeout"

            logged_text = " ".join(r.getMessage() for r in caplog.records)
            for leaked in leaked_fragments:
                assert leaked not in logged_text, (
                    f"[{comp_id}] {leaked!r} がログに漏れている: {logged_text!r}")
        finally:
            _restore_ai()
            caplog.clear()


def test_ai_snapshot_shows_detailed_reason_instead_of_generic_classification():
    """`_check_one`（postgres/neo4j/es 等の一般チェック）は例外を `_classify()` で短い分類ラベル
    （「エラー（RuntimeError）」等）へ丸めるが、`ai_snapshot`（admin 専用画面）の AI 各行は
    `_check_one_ai` を使い、`_ai_check_*` が積んだ具体的な理由文字列をそのまま表示する
    （`docs/manual/41-運用Runbook.md` S8「状態ページの「OpenAI API」行にも同じ理由が出る」契約・
    seed-blocked 等の静的で秘密を含まない理由の可読性を優先する）。"""
    def _fail(settings, system_settings=None):
        raise RuntimeError("OpenAI 接続先の設定が未確定のため停止しています"
                          "（env の設定を修正して再起動してください）: kind が不正です")

    _patch_ai_components({"openai": _fail})
    try:
        rows = health.ai_snapshot("admin", {}, force=True)
        openai_row = next(c for c in rows if c["id"] == "openai")
        assert openai_row["ok"] is False
        assert openai_row["detail"] == (
            "OpenAI 接続先の設定が未確定のため停止しています（env の設定を修正して再起動してください）: kind が不正です")
        assert "エラー（RuntimeError）" not in openai_row["detail"], "詳細な理由が汎用分類へ丸められている"
    finally:
        _restore_ai()


def test_ai_snapshot_caches_per_uid_and_force_bypasses():
    """per-uid キャッシュ: 同一 uid の2回目は force=False なら再実行しない。異なる uid は独立に実行される。
    force=True は常に再実行する（自動ポーリングでの連打を防ぎつつ、再チェックボタンでは必ず最新化する）。"""
    calls = {"n": 0}

    def _count(_settings, _system_settings=None):
        calls["n"] += 1

    _patch_ai_components({"openai": _count})
    try:
        health.ai_snapshot("admin", {}, force=True)
        assert calls["n"] == 1
        health.ai_snapshot("admin", {}, force=False)   # キャッシュ内＝再実行しない
        assert calls["n"] == 1
        health.ai_snapshot("other-admin", {}, force=False)   # 別 uid は独立＝実行される
        assert calls["n"] == 2
        health.ai_snapshot("admin", {}, force=True)   # force は常に再実行
        assert calls["n"] == 3
    finally:
        _restore_ai()


# ---- RV HIGH（2026-07-03再検証）: プローブの並列実行＋短い per-probe timeout ----

def test_ai_snapshot_runs_probes_in_parallel_not_sequentially():
    """5件それぞれが 0.3s かかっても、直列なら 1.5s 超だが並列なら 1s 未満で返る
    （遅い provider 1つで /admin/health 全体が最悪数分ブロックしていた問題の再検証）。"""
    import time as _time

    def _slow(_settings, _system_settings=None):
        _time.sleep(0.3)

    _patch_ai_components({c[0]: _slow for c in health._AI_COMPONENTS})
    try:
        t0 = _time.monotonic()
        rows = health.ai_snapshot("admin", {}, force=True)
        elapsed = _time.monotonic() - t0
        assert all(c["ok"] for c in rows)
        assert elapsed < 1.0, f"並列実行されていない疑い（{elapsed:.2f}s）"
    finally:
        _restore_ai()


def test_ai_snapshot_marks_probe_exceeding_deadline_as_timeout():
    """全体 deadline を超えたプローブは ok=False・「タイムアウト」detail で打ち切られ、
    レスポンス自体は deadline 近辺で返る（無応答スレッドを待ち続けない）。"""
    import threading as _threading
    import time as _time

    release = _threading.Event()

    def _hang(_settings, _system_settings=None):
        release.wait(timeout=5)   # テスト終了後にスレッドが残留し続けないよう上限を設ける

    orig_deadline = health._AI_DEADLINE
    _patch_ai_components({"openai": _hang})
    health._AI_DEADLINE = 0.2   # 実時間で長々待たされないよう deadline を短縮
    try:
        t0 = _time.monotonic()
        rows = health.ai_snapshot("admin", {}, force=True)
        elapsed = _time.monotonic() - t0
        assert elapsed < 1.0, f"deadline 超過後もブロックしている（{elapsed:.2f}s）"
        openai_row = next(c for c in rows if c["id"] == "openai")
        assert openai_row["ok"] is False
        assert "タイムアウト" in openai_row["detail"]
    finally:
        release.set()            # 残留スレッドを解放してからテストを抜ける
        health._AI_DEADLINE = orig_deadline
        _restore_ai()


def test_ai_check_openai_passes_short_explicit_timeout_to_probe(monkeypatch):
    """health 用の短い timeout（既定8秒・SHERPA_HEALTH_AI_TIMEOUT）を _probe に明示で渡す
    （渡さないと抽出用の既定90秒まで1プローブがブロックし得る）。"""
    from sherpa.ingest import graph_extract

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    captured = {}
    orig = graph_extract._probe

    def fake_probe(cfg, timeout=None):
        captured["timeout"] = timeout
        return True, ""

    graph_extract._probe = fake_probe
    try:
        health._ai_check_openai({"openai_api_key": "sk-test"})
        assert captured["timeout"] == health._AI_TIMEOUT
    finally:
        graph_extract._probe = orig


def test_ai_check_openai_strict_rejects_invalid_cloud_provider_without_probing(monkeypatch):
    """`cloud_provider`（A7）が非空の不正値のとき、実 API 呼び出し（課金）を伴う「再チェック」
    probe を送信せず正直に失敗する（黙って既定 openai へ倒れたキーで実送信しない・意図しない
    課金の是正）。"""
    import pytest
    from sherpa import keys
    from sherpa.ingest import graph_extract

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "not-a-real-provider"})
    probe_calls = []
    orig = graph_extract._probe
    graph_extract._probe = lambda cfg, timeout=None: (probe_calls.append(cfg) or (True, ""))
    try:
        with pytest.raises(keys.InvalidCloudProviderConfigError, match="not-a-real-provider"):
            health._ai_check_openai({"openai_api_key": "sk-test"})
        assert probe_calls == []
    finally:
        graph_extract._probe = orig


def test_ai_check_gemini_strict_rejects_invalid_cloud_provider_without_probing(monkeypatch):
    import pytest
    from sherpa import keys
    from sherpa.ingest import graph_extract

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "not-a-real-provider"})
    probe_calls = []
    orig = graph_extract._probe
    graph_extract._probe = lambda cfg, timeout=None: (probe_calls.append(cfg) or (True, ""))
    try:
        with pytest.raises(keys.InvalidCloudProviderConfigError, match="not-a-real-provider"):
            health._ai_check_gemini({"gemini_api_key": "gk-test"})
        assert probe_calls == []
    finally:
        graph_extract._probe = orig


def test_ai_check_bedrock_strict_rejects_invalid_cloud_provider_without_probing(monkeypatch):
    import pytest
    from sherpa import keys
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "not-a-real-provider",
        "bedrock_api_key": "bk-test"})
    with pytest.raises(keys.InvalidCloudProviderConfigError, match="not-a-real-provider"):
        health._ai_check_bedrock({"bedrock_api_key": "bk-test"})


def test_ai_check_openai_does_not_leak_reflected_url_into_health_log(monkeypatch, caplog):
    """custom/Azure 上流がエラー本文へ要求 URL を echo しても、health のサーバログ
    （`health._logger.warning`）に admin だけが設定した base URL の path（デプロイ名）・query
    （api-version）が残らないこと。`_ai_check_openai` の RuntimeError は
    `graph_extract._probe`（内部で `_safe_detail` が反射 URL を伏せる）の detail から組み立てる
    ため、ログへ渡る文字列も既に安全なはずだが、`_check_one_ai` 自身も detail・ログの両方へ
    `_mask_secrets`/`_redact_reflected_urls` を重ねて通す（多層防御・生の例外は渡さない）。"""
    import io
    import json
    import urllib.error

    from sherpa.ingest import graph_extract

    sys_s = {"openai_endpoint_kind": "azure",
            "openai_base_url": "https://myres.openai.azure.com/openai/deployments/my-secret-deploy",
            "openai_api_key": "sk-test"}
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: sys_s)

    fp = io.BytesIO(json.dumps({"error": {"message": (
        "bad request to https://myres.openai.azure.com/openai/deployments/my-secret-deploy/"
        "chat/completions?api-version=2024-01-01")}}).encode("utf-8"))
    exc = urllib.error.HTTPError("https://myres.openai.azure.com/openai/v1/chat/completions",
                                 400, "error", {}, fp)
    monkeypatch.setattr(graph_extract, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))

    with caplog.at_level("WARNING", logger="sherpa.health"):
        rows = health.ai_snapshot("admin", {}, force=True)
    openai_row = next(c for c in rows if c["id"] == "openai")
    assert openai_row["ok"] is False
    assert "my-secret-deploy" not in openai_row["detail"]
    logged_text = " ".join(r.getMessage() for r in caplog.records)
    assert "my-secret-deploy" not in logged_text
    assert "api-version" not in logged_text


def _set_central_openai_key(monkeypatch, value):
    """`_ping_openai` はもう env を読まない（`sherpa.keys.resolve_api_key` 経由の中央設定）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"openai_api_key": value} if value is not None else {})


def test_ping_openai_rejects_placeholder_exact_match_not_substring(monkeypatch):
    """RV MED（2026-08-18 Codex RV 2巡目 指摘3）: 以前は `"REPLACE_ME" in key`（部分一致）で、
    たまたま "REPLACE_ME" という文字列を含むだけの実キーまで誤って未設定扱いにし得た。判定は
    `agent_constructs.is_real_api_key`（完全一致ベース）に揃え、プレースホルダは弾きつつ、
    それ以外のキー（"REPLACE_ME" を部分文字列に含むだけの値）は弾かないことを固定する。"""
    import pytest

    _set_central_openai_key(monkeypatch, "sk-REPLACE_ME")
    with pytest.raises(RuntimeError, match="設定してください"):
        health._ping_openai()

    _set_central_openai_key(monkeypatch, None)
    with pytest.raises(RuntimeError, match="設定してください"):
        health._ping_openai()

    # "REPLACE_ME" を部分文字列に含むだけの実キー（旧・部分一致判定なら誤って未設定扱いされていた）。
    _set_central_openai_key(monkeypatch, "sk-proj-REPLACE_ME_IS_NOT_MY_WHOLE_KEY-abc123")
    health._ping_openai()   # 例外を投げなければ OK（未設定扱いされていない）


def test_ai_check_openai_rejects_placeholder_without_calling_probe(monkeypatch):
    """RV MED（2026-08-18 指摘3）: env の OPENAI_API_KEY がプレースホルダのままだと、以前は
    `if not key:` の真偽値判定だけを通り抜けて実 API へ probe しに行き、分かりにくい 401 になって
    いた。プレースホルダは probe に到達する前に「未設定」として弾かれることを固定する
    （_probe を差し替えて、呼ばれたら即座に分かるようにする）。"""
    import pytest

    from sherpa.ingest import graph_extract

    called = {"n": 0}

    def _should_not_be_called(cfg, timeout=None):
        called["n"] += 1
        return True, ""

    orig = graph_extract._probe
    graph_extract._probe = _should_not_be_called
    try:
        with pytest.raises(RuntimeError, match="設定してください"):
            health._ai_check_openai({"openai_api_key": "sk-REPLACE_ME"})
        assert called["n"] == 0, "プレースホルダなのに _probe（実API呼び出し）まで進んでいる"
    finally:
        graph_extract._probe = orig


def test_ai_check_bedrock_passes_short_explicit_timeout_to_probe(monkeypatch):
    """Bedrock も同様に `BedrockProvider.probe(timeout=...)` へ health 用の短い timeout を渡す。

    A7: bedrock を選択中のプロバイダにする（既定 openai のままだと `_ai_check_bedrock` が
    `sherpa.keys.selected_cloud_provider` のゲートで早期に RuntimeError を出す）。
    """
    from sherpa import agents

    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"cloud_provider": "bedrock", "personal_api_keys_allowed": True})

    captured = {}

    class _FakeBedrockProvider:
        def __init__(self, *_a, **_k):
            pass

        def probe(self, timeout=None):
            captured["timeout"] = timeout
            return True, ""

    orig = agents.BedrockProvider
    agents.BedrockProvider = _FakeBedrockProvider
    try:
        health._ai_check_bedrock({"bedrock_api_key": "test-key"})
        assert captured["timeout"] == health._AI_TIMEOUT
    finally:
        agents.BedrockProvider = orig


def test_ai_check_codex_uses_key_auth_on_azure_endpoint(monkeypatch):
    """Azure/互換接続先の Codex(OpenAI) 構成は env_key 認証＝`codex login status` を見ない。

    実環境指摘（2026-09-02）: チャットの Codex は動くのにテスト画面だけ「未接続」——
    ChatGPT ログイン確認が実際の認証方式（OPENAI_API_KEY）とズレていた。キー解決可なら
    subprocess を一切呼ばず成功、キー無しは NO_CENTRAL_KEY_MESSAGE で失敗する。"""
    import subprocess

    import pytest
    from sherpa import health, keys

    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("sherpa.llm.openai_endpoint_kind", lambda s=None: "azure")

    def _no_subprocess(*a, **kw):
        raise AssertionError("codex login status を呼んではいけない（env_key 認証構成）")
    monkeypatch.setattr(subprocess, "run", _no_subprocess)

    monkeypatch.setattr(health.keys, "resolve_api_key",
                        lambda provider, settings, system_settings=None, **kw: "sk-test")
    health._ai_check_codex({}, {})   # 例外なし＝接続 OK

    monkeypatch.setattr(health.keys, "resolve_api_key",
                        lambda provider, settings, system_settings=None, **kw: None)
    with pytest.raises(RuntimeError, match=keys.NO_CENTRAL_KEY_MESSAGE[:12]):
        health._ai_check_codex({}, {})


def test_ai_check_codex_direct_openai_still_uses_login_status(monkeypatch):
    """OpenAI 直結構成は従来どおり `codex login status`（auth.json 経由）で判定する。"""
    import subprocess
    from sherpa import health

    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("sherpa.llm.openai_endpoint_kind", lambda s=None: "openai")
    calls = []

    class _R:
        returncode = 0
        stdout = "Logged in"
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (calls.append(a), _R())[1])
    health._ai_check_codex({}, {})
    assert calls, "直結構成では login status を確認する"


# ---- _ping_openai のヒント文言 ----

def test_ping_openai_hint_points_to_admin_not_env():
    """運用ポリシー/資格情報は管理画面（system_settings）が唯一の真実源で、env は初回シードのみ
    （`sherpa.keys` docstring）。COMPONENTS の openai 行のヒントも `_AI_COMPONENTS` の openai 行と
    同じ `keys.NO_CENTRAL_KEY_MESSAGE`（管理画面誘導）を使うことを固定する。"""
    from sherpa import keys

    hint = next(c[4] for c in health.COMPONENTS if c[0] == "openai")
    assert ".env" not in hint
    assert keys.NO_CENTRAL_KEY_MESSAGE in hint


# ---- ES/グラフ 実クエリ検索プローブ（AI との切り分け用）----

def test_search_probe_es_no_world():
    assert health._search_probe_es(None) == health._NO_WORLD_DETAIL


def test_search_probe_es_hit(monkeypatch):
    monkeypatch.setattr("sherpa.es_index._req",
                        lambda *a, **k: {"hits": {"hits": [{"_id": "1"}]}})
    assert health._search_probe_es("test") == "ヒットあり"


def test_search_probe_es_empty_index_reports_empty_not_failure(monkeypatch):
    monkeypatch.setattr("sherpa.es_index._req", lambda *a, **k: {"hits": {"hits": []}})
    assert health._search_probe_es("test") == "索引が空です"


def test_search_probe_es_missing_index_404_reports_empty_not_failure(monkeypatch):
    """索引が存在しない（その world を1度も取り込んでいない）は ES 自体は正常に応答している
    ため、失敗（ok=False）ではなく「索引が空」に倒す。"""
    import io
    import urllib.error

    def _raise_404(*a, **k):
        raise urllib.error.HTTPError("http://es/x/_search", 404, "not found", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr("sherpa.es_index._req", _raise_404)
    assert health._search_probe_es("test") == "索引が空です（未取り込み）"


def test_search_probe_es_connection_failure_raises(monkeypatch):
    """接続不可等の実失敗は例外を送出する（呼び出し元 `_check_one_search` が分類する）。"""
    import pytest

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("sherpa.es_index._req", _boom)
    with pytest.raises(RuntimeError):
        health._search_probe_es("test")


class _FakeGraphResult:
    def __init__(self, row):
        self._row = row

    def single(self):
        return self._row


class _FakeGraphSession:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **params):
        return _FakeGraphResult(self._row)


class _FakeGraphDriver:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def session(self):
        return _FakeGraphSession(self._row)


def _patch_neo4j_env(monkeypatch) -> None:
    monkeypatch.setattr("sherpa.ingest.world_neo4j._env",
                        lambda: {"uri": "bolt://localhost:7687", "user": "neo4j", "pw": "x"})


def test_search_probe_graph_no_world():
    assert health._search_probe_graph(None) == health._NO_WORLD_DETAIL


def test_search_probe_graph_hit(monkeypatch):
    import neo4j

    _patch_neo4j_env(monkeypatch)
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **k: _FakeGraphDriver({"n": "x"}))
    assert health._search_probe_graph("test") == "ヒットあり"


def test_search_probe_graph_empty_reports_empty_not_failure(monkeypatch):
    import neo4j

    _patch_neo4j_env(monkeypatch)
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **k: _FakeGraphDriver(None))
    assert health._search_probe_graph("test") == "該当データが空です（未取り込み）"


def test_search_probe_graph_connection_failure_raises(monkeypatch):
    import neo4j
    import pytest

    _patch_neo4j_env(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", _boom)
    with pytest.raises(RuntimeError):
        health._search_probe_graph("test")


_ORIGINAL_SEARCH_COMPONENTS = health._SEARCH_COMPONENTS


def _restore_search() -> None:
    health._SEARCH_COMPONENTS = _ORIGINAL_SEARCH_COMPONENTS
    health._search_cache = {}


def test_search_snapshot_no_world_shows_no_target_and_ok(monkeypatch):
    """登録 world が無い時は実クエリを打たず、両行とも「対象なし」＋ok=True（失敗ではない）。"""
    monkeypatch.setattr("sherpa.store.list_worlds_db", lambda: [])
    health._search_cache = {}
    rows = health.search_snapshot("admin", force=True)
    assert len(rows) == 2
    for row in rows:
        assert row["ok"] is True
        assert row["detail"] == health._NO_WORLD_DETAIL
        assert "hint" not in row


def test_search_snapshot_passes_same_world_to_both_probes(monkeypatch):
    """ES・グラフの両プローブへ同じ world_id スナップショットを渡す（`ai_snapshot` が全プローブへ
    同じ system_settings を渡すのと同じ理由）。"""
    seen = []

    def _probe(world_id):
        seen.append(world_id)
        return "ヒットあり"

    health._SEARCH_COMPONENTS = [
        (comp_id, label, impact, _probe, hint)
        for comp_id, label, impact, _fn, hint in _ORIGINAL_SEARCH_COMPONENTS
    ]
    monkeypatch.setattr(health, "_search_probe_world", lambda: "world-x")
    health._search_cache = {}
    try:
        health.search_snapshot("admin", force=True)
        assert seen == ["world-x", "world-x"]
    finally:
        _restore_search()


def test_search_snapshot_caches_per_uid_and_force_bypasses(monkeypatch):
    """per-uid キャッシュ: 同一 uid の2回目は force=False なら再実行しない。異なる uid は独立に
    実行される。force=True は常に再実行する（`ai_snapshot` と同じ仕組み・
    `test_ai_snapshot_caches_per_uid_and_force_bypasses` 参照）。"""
    calls = {"n": 0}

    def _probe(_world_id):
        calls["n"] += 1
        return "ヒットあり"

    health._SEARCH_COMPONENTS = [
        (comp_id, label, impact, _probe, hint)
        for comp_id, label, impact, _fn, hint in _ORIGINAL_SEARCH_COMPONENTS
    ]
    monkeypatch.setattr(health, "_search_probe_world", lambda: "test")
    health._search_cache = {}
    try:
        health.search_snapshot("admin", force=True)
        assert calls["n"] == 2   # es_search + graph_search
        health.search_snapshot("admin", force=False)   # キャッシュ内＝再実行しない
        assert calls["n"] == 2
        health.search_snapshot("other-admin", force=False)   # 別 uid は独立＝実行される
        assert calls["n"] == 4
        health.search_snapshot("admin", force=True)   # force は常に再実行
        assert calls["n"] == 6
    finally:
        _restore_search()


def test_search_snapshot_failure_recorded_with_hint(monkeypatch):
    """接続不可等の失敗は ok=False・hint 付きで記録される（impact=none なので状態ドット全体には
    影響しない・`test_none_impact_failures_stay_ok_but_are_recorded` と同じ考え方）。"""
    def _fail(_world_id):
        raise RuntimeError("boom")

    health._SEARCH_COMPONENTS = [
        (comp_id, label, impact, _fail, hint)
        for comp_id, label, impact, _fn, hint in _ORIGINAL_SEARCH_COMPONENTS
    ]
    monkeypatch.setattr(health, "_search_probe_world", lambda: "test")
    health._search_cache = {}
    try:
        rows = health.search_snapshot("admin", force=True)
        assert all(not r["ok"] and r["hint"] for r in rows)
    finally:
        _restore_search()


def test_search_snapshot_registry_failure_reported_as_failure_not_no_target(monkeypatch):
    """レジストリ読取自体の失敗（Postgres 不達等）は「登録 world がありません」（空リスト）に
    丸めない。空リストと読取失敗は区別し、失敗時は両行 ok=False＋`_classify()` の分類 detail で
    正直に失敗させる。detail に DSN 等の生の例外文字列を出さないのは `_check_one`（postgres 等）
    と同じ契約（サーバログには raw の例外を残す・`test_detail_does_not_leak_raw_exception_text`
    参照）。"""
    def _boom():
        raise RuntimeError("postgresql://user:secretpw@host/db connection failed")

    monkeypatch.setattr("sherpa.store.list_worlds_db", _boom)
    health._search_cache = {}
    rows = health.search_snapshot("admin", force=True)
    assert len(rows) == 2
    for row in rows:
        assert row["ok"] is False
        assert row["detail"] != health._NO_WORLD_DETAIL
        assert "エラー" in row["detail"]
        assert "secretpw" not in row["detail"]
        assert row["hint"]


def test_search_snapshot_runs_probes_in_parallel_not_sequentially(monkeypatch):
    """2件それぞれが 0.3s かかっても、直列なら 0.6s 超だが並列なら短く返る
    （`test_ai_snapshot_runs_probes_in_parallel_not_sequentially` と同じ検証）。"""
    import time as _time

    def _slow(_world_id):
        _time.sleep(0.3)
        return "ヒットあり"

    health._SEARCH_COMPONENTS = [
        (comp_id, label, impact, _slow, hint)
        for comp_id, label, impact, _fn, hint in _ORIGINAL_SEARCH_COMPONENTS
    ]
    monkeypatch.setattr(health, "_search_probe_world", lambda: "test")
    health._search_cache = {}
    try:
        t0 = _time.monotonic()
        rows = health.search_snapshot("admin", force=True)
        elapsed = _time.monotonic() - t0
        assert all(r["ok"] for r in rows)
        assert elapsed < 0.6, f"並列実行されていない疑い（{elapsed:.2f}s）"
    finally:
        _restore_search()


def test_search_snapshot_marks_probe_exceeding_deadline_as_timeout(monkeypatch):
    """全体 deadline を超えたプローブは ok=False・「タイムアウト」detail で打ち切られ、
    レスポンス自体は deadline 近辺で返る（TCP は繋がるが応答しない ES/Neo4j での無期限化・
    スレッド積み上がりを防ぐ・`test_ai_snapshot_marks_probe_exceeding_deadline_as_timeout` と同種）。"""
    import threading as _threading
    import time as _time

    release = _threading.Event()

    def _hang(_world_id):
        release.wait(timeout=5)   # テスト終了後にスレッドが残留し続けないよう上限を設ける
        return "ヒットあり"

    def _ok(_world_id):
        return "ヒットあり"

    health._SEARCH_COMPONENTS = [
        ("es_search", "ES検索（実クエリ）", "none", _hang, "h"),
        ("graph_search", "グラフ検索（実クエリ）", "none", _ok, "h"),
    ]
    monkeypatch.setattr(health, "_search_probe_world", lambda: "test")
    orig_deadline = health._SEARCH_DEADLINE
    health._SEARCH_DEADLINE = 0.2   # 実時間で長々待たされないよう deadline を短縮
    health._search_cache = {}
    try:
        t0 = _time.monotonic()
        rows = health.search_snapshot("admin", force=True)
        elapsed = _time.monotonic() - t0
        assert elapsed < 1.0, f"deadline 超過後もブロックしている（{elapsed:.2f}s）"
        es_row = next(r for r in rows if r["id"] == "es_search")
        assert es_row["ok"] is False
        assert "タイムアウト" in es_row["detail"]
    finally:
        release.set()            # 残留スレッドを解放してからテストを抜ける
        health._SEARCH_DEADLINE = orig_deadline
        _restore_search()


# ===== 対象外（_NotApplicable）: 未設定/未選択のプロバイダを WARNING にしない（2026-09-04） =====

def test_bedrock_unselected_is_not_applicable_not_warning(monkeypatch, caplog):
    import logging
    from sherpa import health, store
    monkeypatch.setattr(store, "get_system_settings", lambda: {})   # cloud 未選択
    with caplog.at_level(logging.DEBUG, logger="sherpa.health"):
        out = health._check_one("bedrock", "b", "none", health._ping_bedrock, "hint")
    assert out["ok"] is True and "対象外" in out["detail"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_ollama_unconfigured_connection_refused_is_not_applicable(monkeypatch, caplog):
    import logging
    from sherpa import health, store, llm
    monkeypatch.setattr(store, "get_system_settings", lambda: {})   # ollama_url 未設定
    def _boom(*a, **kw):
        raise OSError("Connection refused")
    monkeypatch.setattr(llm, "urlopen_no_redirect", _boom)
    with caplog.at_level(logging.DEBUG, logger="sherpa.health"):
        out = health._check_one("ollama", "o", "none", health._ping_ollama, "hint")
    assert out["ok"] is True and "対象外" in out["detail"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_ollama_configured_failure_still_warns(monkeypatch, caplog):
    import logging
    from sherpa import health, store, llm
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {"ollama_url": "http://127.0.0.1:11434"})   # 明示設定あり
    def _boom(*a, **kw):
        raise OSError("Connection refused")
    monkeypatch.setattr(llm, "urlopen_no_redirect", _boom)
    with caplog.at_level(logging.DEBUG, logger="sherpa.health"):
        out = health._check_one("ollama", "o", "none", health._ping_ollama, "hint")
    assert out["ok"] is False
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]
