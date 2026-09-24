"""OpenAI 互換 API の接続先（`llm.openai_base_url`/`openai_url`/`openai_headers`/
`openai_endpoint_kind`/`openai_auth_header_style`/`openai_api_version`）の単体テスト。

SET-2c（接続先の UI 移管）で、接続先の唯一の真実源は env から `system_settings`（DB・管理画面
「AIプロバイダ（クラウド）」カードの「接続先」欄）へ移った。ここでは各関数へ `system_settings`
（省略可の第2引数／キーワード引数）を直接渡すことで、DB・env のどちらにも依存しない純粋な
単体テストにする（通信を伴わない）。

本ファイルは通信を伴わない純粋な単体テストのみを置く（URL 組み立て・末尾スラッシュ正規化・
api-version 付与・ヘッダ方式・`openai_endpoint_kind` の3値・不正 URL の拒否）。実サーバを立てて
「接続できた」と主張するテストは置かない（実際の接続確認は `scripts/azure_smoke.py` で行う）。
"""
from __future__ import annotations

import pytest

from sherpa import llm

# ---------------------------------------------------------------------------
# URL 組み立て（ネットワークなし）
# ---------------------------------------------------------------------------


def test_default_base_url_and_urls():
    """既定（system_settings 未設定）は従来の OpenAI 本家 URL とバイト単位で一致する。"""
    assert llm.openai_base_url({}) == "https://api.openai.com/v1"
    assert llm.openai_url("chat/completions", {}) == "https://api.openai.com/v1/chat/completions"
    assert llm.openai_url("embeddings", {}) == "https://api.openai.com/v1/embeddings"


def test_constant_openai_chat_url_unchanged():
    """モジュール定数 `OPENAI_CHAT_URL`/`OPENAI_EMBED_URL` は互換のため残す＝DB を読まない固定値
    （`tests/unit/test_sub_hybrid.py`・`test_agentic_search.py` が参照）。"""
    assert llm.OPENAI_CHAT_URL == "https://api.openai.com/v1/chat/completions"
    assert llm.OPENAI_EMBED_URL == "https://api.openai.com/v1/embeddings"


def test_azure_base_url_trailing_slash_normalized():
    """Azure の base URL（末尾スラッシュ有り）を渡しても二重スラッシュにならない。"""
    sysset = {"openai_endpoint_kind": "azure",
              "openai_base_url": "https://myres.openai.azure.com/openai/v1/"}
    assert llm.openai_base_url(sysset) == "https://myres.openai.azure.com/openai/v1"
    assert llm.openai_url("chat/completions", sysset) == \
        "https://myres.openai.azure.com/openai/v1/chat/completions"


def test_azure_base_url_without_trailing_slash():
    sysset = {"openai_endpoint_kind": "azure",
              "openai_base_url": "https://myres.openai.azure.com/openai/v1"}
    assert llm.openai_url("embeddings", sysset) == "https://myres.openai.azure.com/openai/v1/embeddings"


def test_api_version_appended_as_query():
    sysset = {"openai_endpoint_kind": "azure",
              "openai_base_url": "https://myres.openai.azure.com/openai/v1/",
              "openai_api_version": "2026-05-01-preview"}
    url = llm.openai_url("chat/completions", sysset)
    assert url == ("https://myres.openai.azure.com/openai/v1/chat/completions"
                   "?api-version=2026-05-01-preview")


def test_api_version_combines_with_existing_query():
    """`path` 自体にクエリが既にあれば `&` で結合する（既定の呼び方は起こらないが、堅牢性として担保）。"""
    sysset = {"openai_endpoint_kind": "custom", "openai_base_url": "https://gw.example.com/v1",
              "openai_api_version": "2026-05-01-preview"}
    url = llm.openai_url("models?limit=10", sysset)
    assert url == "https://gw.example.com/v1/models?limit=10&api-version=2026-05-01-preview"


def test_api_version_ignored_when_kind_is_openai():
    """接続先が「本家」なら、`openai_base_url` に他接続先向けの付随値が残っていても
    `openai_api_version`/`openai_auth_header` は無視する（本家へ切り替えた後、古い Azure 設定が
    黙って有効なまま残らない契約・`openai_base_url()` と同じ縮退規則）。"""
    sysset = {"openai_endpoint_kind": "openai", "openai_base_url": "https://myres.openai.azure.com/openai/v1",
              "openai_api_version": "2026-05-01-preview", "openai_auth_header": "api-key"}
    assert llm.openai_api_version(sysset) == ""
    assert llm.openai_auth_header_style(sysset) == "bearer"
    assert "?" not in llm.openai_url("chat/completions", sysset)
    assert llm.openai_headers("sk-x", sysset) == {"Authorization": "Bearer sk-x", "Content-Type": "application/json"}


def test_no_api_version_means_no_query():
    assert "?" not in llm.openai_url("chat/completions", {})


def test_http_base_url_rejected():
    """`http://` は非 loopback だと拒否される（API キーを平文で送らない）。"""
    sysset = {"openai_endpoint_kind": "custom", "openai_base_url": "http://example.internal/v1"}
    with pytest.raises(ValueError):
        llm.openai_url("chat/completions", sysset)


def test_http_localhost_rejected():
    """`http://` はループバック（localhost/127.0.0.1 等）であっても拒否される（`https://` のみを
    許可する契約）。"""
    for base in ("http://127.0.0.1:9/v1", "http://localhost:9/v1"):
        sysset = {"openai_endpoint_kind": "custom", "openai_base_url": base}
        with pytest.raises(ValueError):
            llm.openai_url("chat/completions", sysset)


# ---------------------------------------------------------------------------
# `https://` なら無条件で許可していた従来の判定を強化する
# （ホスト必須／userinfo 禁止／クエリ・フラグメント禁止）。
# ---------------------------------------------------------------------------


def test_https_empty_host_rejected():
    """`https:///v1`（ホスト空）は拒否される。"""
    sysset = {"openai_endpoint_kind": "custom", "openai_base_url": "https:///v1"}
    with pytest.raises(ValueError):
        llm.openai_url("chat/completions", sysset)


def test_https_userinfo_rejected():
    """`https://user:secret@host/v1`（userinfo 付き）は拒否される。"""
    sysset = {"openai_endpoint_kind": "custom",
              "openai_base_url": "https://user:secret@myres.openai.azure.com/v1"}
    with pytest.raises(ValueError):
        llm.openai_url("chat/completions", sysset)


def test_https_userinfo_not_leaked_in_error():
    """userinfo を含む不正 URL のエラー文言に userinfo（パスワード）を出さない。"""
    sysset = {"openai_endpoint_kind": "custom",
              "openai_base_url": "https://user:secret-password@myres.openai.azure.com/v1"}
    with pytest.raises(ValueError) as exc:
        llm.openai_url("chat/completions", sysset)
    assert "secret-password" not in str(exc.value)


def test_https_query_in_base_rejected():
    """`base` 自体にクエリが付くと `openai_url()` の単純連結で path が壊れるため拒否される
    （API バージョンは別欄の openai_api_version に一本化する契約）。"""
    sysset = {"openai_endpoint_kind": "custom",
              "openai_base_url": "https://myres.openai.azure.com/openai/v1?api-version=2024-10-21"}
    with pytest.raises(ValueError):
        llm.openai_url("chat/completions", sysset)


def test_https_fragment_in_base_rejected():
    sysset = {"openai_endpoint_kind": "custom",
              "openai_base_url": "https://myres.openai.azure.com/openai/v1#frag"}
    with pytest.raises(ValueError):
        llm.openai_url("chat/completions", sysset)


def test_https_backslash_in_base_rejected():
    """`https://host.example\\internal\\secret` のような backslash 混入は拒否される
    （`urlparse` は backslash を構造区切りとして扱わないため素通りしていた穴）。"""
    with pytest.raises(ValueError):
        llm.assert_openai_base_url_allowed("https://host.example\\internal\\secret")


def test_https_internal_ascii_space_rejected():
    """`base` の内部（末尾ではない位置）に ASCII 空白が混入する URL は拒否される
    （`_redact_reflected_urls` の「URL は空白を含まない1トークン」という前提を守るため）。"""
    with pytest.raises(ValueError):
        llm.assert_openai_base_url_allowed("https://host.example internal/v1")


def test_https_fullwidth_space_rejected():
    """全角スペース（U+3000）等の Unicode 空白も拒否される（ASCII 空白限定のチェックでは
    素通りしてしまう）。"""
    with pytest.raises(ValueError):
        llm.assert_openai_base_url_allowed("https://host.example\u3000internal/v1")


def test_https_control_character_rejected():
    """制御文字（例: BEL）混入も拒否される。"""
    with pytest.raises(ValueError):
        llm.assert_openai_base_url_allowed("https://host.example\x07internal/v1")


def test_https_non_ascii_character_rejected():
    """ASCII 印字文字以外（例: 全角英数字）も拒否される。"""
    with pytest.raises(ValueError):
        llm.assert_openai_base_url_allowed("https://host.example／internal/v1")


def test_invalid_scheme_error_does_not_leak_path_pseudo_secret():
    """https 以外の scheme を拒否するエラー文言の host 表現（`_redact_url_for_error`
    経由の安全な部分）には scheme を含めず、path 内の疑似秘密も一切含めない（文言そのものが
    "https:// のみ許可されます" と案内するのは既定文言であり対象外・safe な host 表現部分だけを見る）。"""
    sysset = {"openai_endpoint_kind": "custom",
              "openai_base_url": "http://myres.openai.azure.com/openai/deployments/sk-should-not-leak"}
    with pytest.raises(ValueError) as exc:
        llm.openai_url("chat/completions", sysset)
    assert "sk-should-not-leak" not in str(exc.value)
    assert llm._redact_url_for_error(sysset["openai_base_url"]) == "myres.openai.azure.com"
    assert "'myres.openai.azure.com'" in str(exc.value)   # host 表現そのものは repr で出る
    assert "http://myres" not in str(exc.value) and "https://myres" not in str(exc.value)


def test_db_unreachable_falls_back_to_openai_defaults(monkeypatch):
    """DB 到達不可（`store.get_system_settings()` が例外）でも `system_settings` 省略呼び出しは
    クラッシュせず組み込み既定（OpenAI 本家・bearer）へ fail-safe する。"""
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("sherpa.store.get_system_settings", _boom)
    assert llm.openai_base_url() == "https://api.openai.com/v1"
    assert llm.openai_endpoint_kind() == "openai"
    assert llm.openai_api_version() == ""
    assert llm.openai_auth_header_style() == "bearer"
    assert llm.openai_headers("sk-x") == {"Authorization": "Bearer sk-x", "Content-Type": "application/json"}
    assert llm.openai_url("chat/completions") == "https://api.openai.com/v1/chat/completions"


@pytest.mark.parametrize("bad_value", [{}, [], 0, False])
def test_falsy_non_string_saved_base_url_raises_instead_of_degrading_to_openai_default(bad_value):
    """実害の回帰固定: `{}`/`[]`/`0`/`False` のような falsy な非文字列が保存されていても、
    `value or ""` の素朴な falsy 潰しで「未設定」と誤認して本家既定 URL へ黙って縮退しては
    ならない（kind=azure なのに本家 URL が組み立てられ、Azure 向けの資格情報が本家 OpenAI へ
    送信されてしまう）。`None`（真の未設定）とは区別して `ValueError` を送出する。"""
    sysset = {"openai_endpoint_kind": "azure", "openai_base_url": bad_value}
    with pytest.raises(ValueError):
        llm.openai_base_url(sysset)
    with pytest.raises(ValueError):
        llm.openai_url("chat/completions", sysset)


def test_none_saved_base_url_still_falls_back_to_openai_default():
    """`None`（真に未設定）は従来どおり本家既定へ fail-safe する（`ValueError` にはしない・
    falsy 非文字列との区別を固定する対照テスト）。"""
    sysset = {"openai_endpoint_kind": "azure", "openai_base_url": None}
    assert llm.openai_base_url(sysset) == "https://api.openai.com/v1"


@pytest.mark.parametrize("bad_value", [["https://evil.example.com"], {"nested": "v"}, 12345])
def test_truthy_non_string_saved_base_url_also_raises(bad_value):
    """真になる非文字列（list/dict/int 等）も同様に `ValueError`（`str(value)` で偶然パースに
    失敗して弾かれる場合があるだけの偶然の安全ではなく、型そのものを検証する契約であることを
    固定する）。"""
    sysset = {"openai_endpoint_kind": "azure", "openai_base_url": bad_value}
    with pytest.raises(ValueError):
        llm.openai_base_url(sysset)


@pytest.mark.parametrize("bad_value", [{}, [], 0, False])
def test_falsy_non_string_base_url_raises_even_when_kind_is_openai(bad_value):
    """実害の回帰固定: kind=openai（明示）の分岐は本来 base_url を無視してよいはずだが、
    型検査（`_assert_openai_endpoint_settings_types_valid`）は判定分岐より**先**に行う契約のため、
    kind=openai でも base_url が falsy な非文字列なら `ValueError` になる（型検査を後回しにすると
    「kind=openai だから base_url は空でよい」という早期 return が型検査より先に来てしまい、
    素通りする）。"""
    sysset = {"openai_endpoint_kind": "openai", "openai_base_url": bad_value}
    with pytest.raises(ValueError):
        llm.openai_endpoint_kind(sysset)
    with pytest.raises(ValueError):
        llm.openai_base_url(sysset)
    with pytest.raises(ValueError):
        llm.openai_url("chat/completions", sysset)


@pytest.mark.parametrize("bad_value", [{}, [], 0, False])
def test_falsy_non_string_base_url_raises_when_kind_unset(bad_value):
    """kind 未設定（推定に委ねる）でも base_url が falsy な非文字列なら `ValueError`
    になる（推定ロジックへ入る前に型検査する）。"""
    sysset = {"openai_base_url": bad_value}
    with pytest.raises(ValueError):
        llm.openai_endpoint_kind(sysset)
    with pytest.raises(ValueError):
        llm.openai_base_url(sysset)


@pytest.mark.parametrize("bad_value", [{}, [], 0, False])
def test_non_string_kind_raises_regardless_of_base_url(bad_value):
    """`openai_endpoint_kind` 自体が falsy な非文字列でも `ValueError`
    （kind 側の falsy 潰しも base_url と同様に廃止）。"""
    sysset = {"openai_endpoint_kind": bad_value, "openai_base_url": "https://real.example.com/v1"}
    with pytest.raises(ValueError):
        llm.openai_endpoint_kind(sysset)
    with pytest.raises(ValueError):
        llm.openai_base_url(sysset)


# ---------------------------------------------------------------------------
# ヘッダ
# ---------------------------------------------------------------------------


def test_headers_default_bearer():
    assert llm.openai_headers("sk-x", {}) == {"Authorization": "Bearer sk-x", "Content-Type": "application/json"}


def test_headers_api_key_style():
    sysset = {"openai_endpoint_kind": "azure", "openai_base_url": "https://myres.openai.azure.com/openai/v1",
              "openai_auth_header": "api-key"}
    assert llm.openai_headers("sk-x", sysset) == {"api-key": "sk-x", "Content-Type": "application/json"}


def test_headers_unknown_style_falls_back_to_bearer():
    sysset = {"openai_endpoint_kind": "custom", "openai_base_url": "https://gw.example.com/v1",
              "openai_auth_header": "something-else"}
    assert llm.openai_headers("sk-x", sysset)["Authorization"] == "Bearer sk-x"


def test_headers_rejects_non_string_key_without_sending():
    """RV8 是正の固定: `key` が非文字列（設定破損で JSONB 値が dict/list 等になっている場合）
    だと、`f"Bearer {key}"` の文字列化で dict の repr がそのままヘッダ値に混入し、後から例外
    メッセージへエコーされた場合にマスクの汎用パターンをすり抜けて秘密が漏洩しうる（実際に
    再現）。送信を一切発生させず即座に拒否する（fail-closed）。"""
    corrupted_key = {"unexpected": "AZUREKEY-SHOULD-NEVER-BE-SENT-1234567890"}
    with pytest.raises(RuntimeError):
        llm.openai_headers(corrupted_key, {})
    with pytest.raises(RuntimeError):
        llm.openai_headers(["also", "not", "a", "string"], {})


# ---------------------------------------------------------------------------
# openai_endpoint_kind()
# ---------------------------------------------------------------------------


def test_endpoint_kind_default_is_openai():
    assert llm.openai_endpoint_kind({}) == "openai"


def test_endpoint_kind_azure_by_host_suffix_when_not_explicit():
    """明示 `openai_endpoint_kind` が未設定なら `openai_base_url` の host から推定する
    （env シード直後・管理画面で一度も選ばれていない状態の後方互換）。"""
    assert llm.openai_endpoint_kind(
        {"openai_base_url": "https://myres.openai.azure.com/openai/v1/"}) == "azure"
    assert llm.openai_endpoint_kind(
        {"openai_base_url": "https://myres.services.ai.azure.com/openai/v1/"}) == "azure"


def test_endpoint_kind_custom_when_not_azure_host_and_not_explicit():
    assert llm.openai_endpoint_kind(
        {"openai_base_url": "https://openai-compatible.example.com/v1"}) == "custom"


def test_endpoint_kind_trailing_dns_root_dot_is_normalized_before_classification():
    """`openai_base_url` のホストに DNS ルートドット（`"api.openai.com."`等）が付いていても、
    正規化せずに判定すると本家/Azure が誤って "custom" に分類され、これが
    `agent_constructs.is_local()` 経由で「クラウド（OpenAI 互換）」表示になるだけでなく、
    `providers/__init__.py` の接続可否チェックで本来使えるはずの構成が「未接続」扱いになって
    いた。ここでは分類そのものが正しく戻ることを固定する。"""
    assert llm.openai_endpoint_kind({"openai_base_url": "https://api.openai.com./v1"}) == "openai"
    assert llm.openai_endpoint_kind(
        {"openai_base_url": "https://myres.openai.azure.com./openai/v1"}) == "azure"
    assert llm.openai_endpoint_kind(
        {"openai_base_url": "https://myres.services.ai.azure.com./openai/v1"}) == "azure"
    # 公開ホストでも本家/Azure のサフィックスに一致しなければ引き続き custom のまま。
    assert llm.openai_endpoint_kind({"openai_base_url": "https://api.example.com./v1"}) == "custom"


def test_endpoint_kind_uppercase_host_with_and_without_trailing_dot_is_normalized():
    """ホストが大文字（`"API.OPENAI.COM"`）でも、ルートドットの有無に関わらず本家/Azure に
    正しく分類される——`hostname` の小文字化だけでは不十分で、既定URL比較に使う `base_norm` の
    再構築自体も正規化済みホストから組み立てる必要がある（`netloc` は urlparse で小文字化されない
    ため、大文字ホスト＋ルートドット無しの組み合わせで `base_norm` が大文字のまま残り、既定URL
    （小文字）との比較に失敗して "custom" に誤分類されていた）。"""
    assert llm.openai_endpoint_kind({"openai_base_url": "https://API.OPENAI.COM/v1"}) == "openai"
    assert llm.openai_endpoint_kind({"openai_base_url": "https://API.OPENAI.COM./v1"}) == "openai"
    assert llm.openai_endpoint_kind(
        {"openai_base_url": "https://MYRES.OPENAI.AZURE.COM/openai/v1"}) == "azure"
    assert llm.openai_endpoint_kind(
        {"openai_base_url": "https://MYRES.OPENAI.AZURE.COM./openai/v1"}) == "azure"
    assert llm.openai_endpoint_kind({"openai_base_url": "https://API.EXAMPLE.COM./v1"}) == "custom"


def test_endpoint_kind_explicit_overrides_host_heuristic():
    """host だけでは判別できない構成（社内プロキシ越しの Azure 等）向けに、明示保存された
    `openai_endpoint_kind` が host 推定より優先される。"""
    sysset = {"openai_base_url": "https://azure-proxy.internal/openai/v1",
              "openai_endpoint_kind": "azure"}
    assert llm.openai_endpoint_kind(sysset) == "azure"


def test_endpoint_kind_explicit_openai_ignores_leftover_base_url():
    """「OpenAI 本家」へ明示的に切り替えたら、古い base_url が system_settings に残っていても
    常に本家として扱う（切替後に古い接続先が黙って有効なまま残らない）。"""
    sysset = {"openai_endpoint_kind": "openai",
              "openai_base_url": "https://myres.openai.azure.com/openai/v1"}
    assert llm.openai_endpoint_kind(sysset) == "openai"
    assert llm.openai_base_url(sysset) == "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# assert_openai_endpoint_consistent()（PUT・env シード・接続テストが共有するクロス検証）
# ---------------------------------------------------------------------------


def test_endpoint_consistent_openai_kind_never_requires_base_url():
    llm.assert_openai_endpoint_consistent("openai", "")
    llm.assert_openai_endpoint_consistent("openai", "https://leftover.example.com/v1")


def test_endpoint_consistent_non_openai_kind_requires_base_url():
    with pytest.raises(ValueError):
        llm.assert_openai_endpoint_consistent("azure", "")
    with pytest.raises(ValueError):
        llm.assert_openai_endpoint_consistent("custom", "   ")
    llm.assert_openai_endpoint_consistent("azure", "https://myres.openai.azure.com/openai/v1")


# ---------------------------------------------------------------------------
# ポート検証・エラー/ログへの生値非混入（項目8）
# ---------------------------------------------------------------------------


def test_invalid_port_rejected():
    sysset = {"openai_endpoint_kind": "custom", "openai_base_url": "https://host:notaport/v1"}
    with pytest.raises(ValueError):
        llm.openai_url("chat/completions", sysset)


def test_out_of_range_port_rejected():
    sysset = {"openai_endpoint_kind": "custom", "openai_base_url": "https://host:999999/v1"}
    with pytest.raises(ValueError):
        llm.openai_url("chat/completions", sysset)


def test_valid_explicit_port_accepted():
    sysset = {"openai_endpoint_kind": "custom", "openai_base_url": "https://host:8443/v1"}
    assert llm.openai_url("chat/completions", sysset) == "https://host:8443/v1/chat/completions"


def test_parse_failure_error_has_no_raw_value_embedded():
    """urlparse 自体が失敗する入力（例: 閉じられていない IPv6 リテラル）でも、エラー文言に生の
    base をそのまま埋め込まない（malformed userinfo に紛れた秘密がログへ残らないようにする）。"""
    bad = "https://user:s3cr3t@[::1/v1"
    with pytest.raises(ValueError) as exc:
        llm.assert_openai_base_url_allowed(bad)
    assert "s3cr3t" not in str(exc.value)
    assert bad not in str(exc.value)


def test_query_fragment_error_does_not_leak_query_content():
    """クエリ/フラグメントを含む URL の拒否メッセージは、クエリの中身（誤って混入した秘密等）を
    含めない安全な host 表現のみを示す。"""
    bad = "https://myres.openai.azure.com/openai/v1?leaked_key=sk-should-not-appear"
    with pytest.raises(ValueError) as exc:
        llm.assert_openai_base_url_allowed(bad)
    assert "sk-should-not-appear" not in str(exc.value)


def test_redact_url_for_error_strips_query_and_fragment():
    """scheme を含めず host（＋port）のみへ切り詰める（`_redact_url_for_error` の契約）。"""
    assert llm._redact_url_for_error("https://host/v1?a=1#frag") == "host"


def test_redact_url_for_error_strips_path_with_pseudo_secret():
    """path 内に秘密らしき文字列が混入していても（クエリ同様の想定外混入
    経路）、host 表現には一切残らない。"""
    leaked = llm._redact_url_for_error("https://host/openai/deployments/sk-should-not-appear-in-path")
    assert leaked == "host"
    assert "sk-should-not-appear-in-path" not in leaked


def test_redact_url_for_error_strips_legacy_path_params_with_pseudo_secret():
    """`ParseResult.params`（`;` 区切りの旧式 path パラメータ）経由の混入も残らない
    （`_redact_url_for_error` は `ParseResult`/`geturl()` を経由せず `hostname`/`port` だけから
    組み立てる契約のため、`params` 等の未知フィールド経由の混入が構造的に起こらない）。"""
    leaked = llm._redact_url_for_error("https://host/path;sk-should-not-leak-via-params?q=1#f")
    assert leaked == "host"
    assert "sk-should-not-leak-via-params" not in leaked


def test_redact_url_for_error_keeps_port():
    assert llm._redact_url_for_error("https://host:8443/v1") == "host:8443"


def test_redact_url_for_error_restores_ipv6_brackets_with_port():
    assert llm._redact_url_for_error("https://[2001:db8::1]:8443/v1") == "[2001:db8::1]:8443"


def test_redact_url_for_error_restores_ipv6_brackets_without_port():
    assert llm._redact_url_for_error("https://[2001:db8::1]/v1") == "[2001:db8::1]"


def test_redact_url_for_error_returns_none_on_parse_failure():
    assert llm._redact_url_for_error("https://[::1/v1") is None


def test_redact_url_for_error_returns_none_on_empty_host():
    assert llm._redact_url_for_error("https:///v1") is None


# ---------------------------------------------------------------------------
# `ollama_url_fingerprint`（catchup_v2 の tamper 検知が使う正規化 host:port 指紋）
# ---------------------------------------------------------------------------


def test_ollama_url_fingerprint_normalizes_port_omission():
    """ポート省略の表記ゆれがあっても同じ接続先なら同じ指紋になる
    （`_redact_url_for_error` は逆にポート省略を保つ＝別物であることの確認）。"""
    assert llm.ollama_url_fingerprint("http://10.0.0.5") == llm.ollama_url_fingerprint("http://10.0.0.5:80")
    assert llm._redact_url_for_error("http://10.0.0.5") != llm._redact_url_for_error("http://10.0.0.5:80")


def test_ollama_url_fingerprint_differs_on_different_port():
    assert (llm.ollama_url_fingerprint("http://10.0.0.5:11434")
           != llm.ollama_url_fingerprint("http://10.0.0.5:11435"))


def test_ollama_url_fingerprint_none_on_userinfo():
    assert llm.ollama_url_fingerprint("http://user:secret@10.0.0.5:11434") is None


def test_ollama_url_fingerprint_none_on_query():
    assert llm.ollama_url_fingerprint("http://10.0.0.5:11434?token=x") is None


def test_ollama_url_fingerprint_brackets_ipv6():
    assert llm.ollama_url_fingerprint("http://[::1]:11434") == "[::1]:11434"


# ---------------------------------------------------------------------------
# `openai_post_json`（Request 構築後/opener 呼出直前の再確認・OpenAI 専用）
# ---------------------------------------------------------------------------


def test_openai_post_json_raises_before_socket_open_when_blocked(monkeypatch):
    """`assert_openai_io_allowed` が block を報告する間は、`urlopen_no_redirect`
    （実際にソケットを開く関数）が一切呼ばれないことを固定する。"""
    called = []
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda *a, **kw: called.append(1))
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", "test-blocked")
    with pytest.raises(RuntimeError):
        llm.openai_post_json("https://api.openai.com/v1/x", {}, {"a": 1})
    assert called == []


def test_post_json_not_gated_by_openai_block(monkeypatch):
    """Gemini/Ollama とも共用する `post_json` は OpenAI の block と無関係に動く
    （一律遮断しないことの固定）。"""
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda *a, **kw: _Resp())
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", "test-blocked")
    assert llm.post_json("http://localhost:11434/api/chat", {}, {}) == {}


# ---------------------------------------------------------------------------
# `llm.PreflightRejected`（実送信前ガードの共通の例外基底・型による「未送信」判定契約）
# ---------------------------------------------------------------------------
# 呼び出し元（`usage_chat.answer_usage_question`）は「実送信前に拒否された」ことを
# `llm.PreflightRejected` という型だけで判定する。`assert_openai_io_allowed` は
# `RuntimeError`、`assert_openai_base_url_allowed`/`assert_ollama_url_allowed` は `ValueError`
# （`SsrfBlocked`）を送出するが、共通型は両方を継承するため、既存の broad
# `except RuntimeError`/`except ValueError`/`except llm.SsrfBlocked` 呼び出し元もそのまま拾える。

def test_openai_io_blocked_raises_preflight_rejected(monkeypatch):
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", "boom")
    with pytest.raises(llm.PreflightRejected):
        llm.assert_openai_io_allowed()


def test_openai_base_url_rejected_raises_preflight_rejected():
    with pytest.raises(llm.PreflightRejected):
        llm.assert_openai_base_url_allowed("http://example.internal/v1")


def test_preflight_rejected_is_both_runtime_error_and_value_error():
    """既存呼び出し元は `RuntimeError` 捕捉・`ValueError` 捕捉のどちらでも互換的に拾えること
    （どちらの型で捕捉していたかは呼び出し元ごとに異なる・モジュール docstring 参照）。"""
    assert issubclass(llm.PreflightRejected, RuntimeError)
    assert issubclass(llm.PreflightRejected, ValueError)
    exc = llm.PreflightRejected("test")
    assert isinstance(exc, RuntimeError)
    assert isinstance(exc, ValueError)


def test_ssrf_blocked_is_a_preflight_rejected():
    """`SsrfBlocked`（Ollama 宛先ポリシー違反）も `PreflightRejected` の一種＝usage_chat 側の
    型判定（`except llm.PreflightRejected`）が ollama 経路の拒否も一律に拾える。既存の
    `except llm.SsrfBlocked`／`except ValueError` 呼び出し元との互換も保つ。"""
    assert issubclass(llm.SsrfBlocked, llm.PreflightRejected)
    exc = llm.SsrfBlocked("blocked")
    assert isinstance(exc, llm.PreflightRejected)
    assert isinstance(exc, ValueError)


# ---------------------------------------------------------------------------
# endpoint_locality（"custom" 接続先の私有/公開判定・agent_constructs.is_local が委ねる）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("base_url", [
    "http://10.0.0.5:8000/v1",
    "http://192.168.1.20:8000/v1",
    "http://localhost:8000/v1",
    "http://llm.lan:8000/v1",
])
def test_endpoint_locality_private_hosts_are_on_prem(base_url):
    assert llm.endpoint_locality(base_url) == "on_prem"


def test_endpoint_locality_public_fqdn_is_cloud():
    assert llm.endpoint_locality("https://api.example.com/v1") == "cloud"


def test_endpoint_locality_loopback_and_link_local_ips_are_on_prem():
    assert llm.endpoint_locality("http://127.0.0.1:8000/v1") == "on_prem"
    assert llm.endpoint_locality("http://169.254.1.1:8000/v1") == "on_prem"
    assert llm.endpoint_locality("http://[::1]:8000/v1") == "on_prem"


def test_endpoint_locality_bare_hostname_without_dns_suffix_is_on_prem():
    """ドットを含まない裸のホスト名（社内 DNS 解決前提）は on_prem 扱い。"""
    assert llm.endpoint_locality("http://llmhost:8000/v1") == "on_prem"


def test_endpoint_locality_global_ip_is_cloud():
    assert llm.endpoint_locality("http://8.8.8.8/v1") == "cloud"


def test_endpoint_locality_unparseable_or_empty_defaults_to_cloud():
    """ホストを解決できない場合は「社内サーバ」と誤って言い切らない方（cloud）へ倒す。"""
    assert llm.endpoint_locality("") == "cloud"
    assert llm.endpoint_locality(None) == "cloud"


def test_endpoint_locality_cgnat_shared_address_space_is_on_prem():
    """CGNAT／Shared Address Space（RFC 6598・100.64.0.0/10）は `ipaddress.is_private` の対象外
    だが on_prem 扱いにする（ISP/社内 NAT 越しの私設アドレス帯）。"""
    assert llm.endpoint_locality("http://100.64.0.5:8000/v1") == "on_prem"
    assert llm.endpoint_locality("http://100.127.255.254:8000/v1") == "on_prem"
    assert llm.endpoint_locality("http://100.63.255.255:8000/v1") == "cloud"   # 帯域の外＝対象外
    assert llm.endpoint_locality("http://100.128.0.0:8000/v1") == "cloud"     # 帯域の外＝対象外


def test_endpoint_locality_trailing_dns_root_dot_is_normalized():
    """FQDN 末尾の DNS ルートドット（`"llm.internal."`）を正規化してから判定する
    （正規化せずに末尾ラベルを取ると空文字列になり非公開 TLD 判定をすり抜けてしまう）。"""
    assert llm.endpoint_locality("http://llm.internal.:8000/v1") == "on_prem"
    assert llm.endpoint_locality("https://api.example.com.:443/v1") == "cloud"
