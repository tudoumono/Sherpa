"""LLM プロバイダ共通層（OpenAI / Azure OpenAI / Ollama）。エンドポイント URL・ヘッダ生成・HTTP POST・プロバイダ選択をここに集約する。
設計: docs/design/settings.md「プロバイダ＋接続先」／docs/design/safety.md「外部 AI へ何を送るか」

SDK 非依存（urllib）。OpenAI へは本文テキストのみ送信する（ファイルはアップロードしない）。
- ストリーミング（`agents._stream`）は `post_json` を通さず、URL/ヘッダだけ共用する。
- `agentic_search._post` / `graph_extract.complete_json` はテスト差し替えシーム。薄いラッパは各モジュールに残し、URL/ヘッダ/HTTP はここへ委譲する。
- `post_json` は HTTP エラー時に `urllib.error.HTTPError` を送出する（429 バックオフ等が依存）。

SSRF 封じ: `ollama_url()` が単一チョークポイントで、全シンクがここで URL を組み立てる。許可は loopback と、admin が
`system_settings` に登録した allowlist（`ollama_allowlist`・host:port 完全一致）のみ。`SsrfBlocked` は `ValueError` 派生で、各シンクの broad `except` で degrade する。
- ポート省略時は scheme の既定ポート（http=80・https=443）を補う（`_canonical_host_port`）。
- `base` に path/query/fragment が混入した URL は解釈不能（None）として弾く。
- `post_json`／`providers/ollama.py` のストリーミング／`health.py` の ollama ping は `urlopen_no_redirect` を使い、3xx を追跡しない。

OpenAI 互換 API の接続先（Azure OpenAI 等）は `system_settings`（`openai_endpoint_kind`／`openai_base_url`／`openai_auth_header`／`openai_api_version`）が唯一の真実源。
env（`OPENAI_BASE_URL` 等）は初回起動時の DB へのシード専用（`sherpa.api._seed_openai_endpoint_from_env`）。
`openai_*()` は呼び出し時に毎回 `system_settings` を読み（DB 不達は「OpenAI 本家・bearer」へ fail-safe）、`system_settings` を渡すとそのスナップショットを使う。
`OPENAI_CHAT_URL`/`OPENAI_EMBED_URL` は互換用の固定既定値で、実際の呼び出しは `openai_url()` を経由する。
"""
from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import json
import os
import threading
import urllib.request
from urllib.parse import quote, urlparse, urlunparse


class PreflightRejected(RuntimeError, ValueError):
    """権威あるガード（`assert_openai_io_allowed`/`assert_openai_base_url_allowed`/`assert_ollama_url_allowed`）が「この I/O は許可されていない」と判定したことを示す共通の例外基底。
    `RuntimeError`・`ValueError` の両方を継承するため、既存の except がそのまま捕捉できる。「未送信」を型で判定したい呼び出し元はこの型を狙って捕捉する。
    """


class SsrfBlocked(PreflightRejected):
    """Ollama 接続先が宛先ポリシー（loopback／admin allowlist）を満たさない。`PreflightRejected` 派生で、各呼び出し側の既存 except に乗って degrade する。"""



class SendBudgetExceeded(RuntimeError):
    """`begin_openai_send()` が呼び出し予算の消費に失敗したとき送出する（ガードのブロックによる `RuntimeError` と区別する専用型）。"""


def _canonical_host_port(url: str) -> tuple[str, int] | None:
    """`url` を `(host, port)` に正規化する（解釈不能・不正なら None）。
    - userinfo を含む URL は解釈不能（資格情報を黙って捨てない）。scheme は http/https のみ。末尾ドットは除去する。
    - path が空/`"/"` 以外、または query/fragment を含む URL は解釈不能（`base` は host:port だけを表す）。
    - ポートは明示指定を優先し、無指定なら scheme の既定ポート（http=80・https=443）を補う（allowlist エントリと接続先の両方に同じ正規化を適用）。
    """
    try:
        p = urlparse(url or "")
    except ValueError:  # 例: 不正な IPv6 リテラル
        return None
    if p.scheme not in ("http", "https"):
        return None
    if p.path not in ("", "/") or p.query or p.fragment:
        return None
    if p.username or p.password:
        return None
    host = (p.hostname or "").rstrip(".")
    if not host:
        return None
    try:
        port = p.port
    except ValueError:  # 例: ポートが数値でない/範囲外
        return None
    if port is not None:
        return host, port
    return host, 80 if p.scheme == "http" else 443


def format_host_port(host: str, port: int) -> str:
    """`(host, port)` を再パース可能な `host:port` 文字列へ整形する（IPv6 は角括弧で囲む）。"""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def ollama_url_fingerprint(url: str) -> str | None:
    """`url` を「正規化 host:port」の指紋へ縮約する（`_canonical_host_port` と同じ規則・解釈不能なら None）。
    ポート省略の有無などの表記ゆれがあっても同じ接続先なら同じ指紋になる。VLM の Ollama 接続先シードが中央接続先との一致判定に使う。
    """
    hp = _canonical_host_port(url)
    return format_host_port(hp[0], hp[1]) if hp is not None else None


def is_loopback_host(host: str) -> bool:
    """host が loopback（localhost・127.0.0.0/8・::1）か。"""
    h = (host or "").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:  # IP リテラルでない（非 IP ホスト名は loopback と判定しない）
        return False


def _allowlisted_hosts(system_settings: dict | None = None) -> set[tuple[str, int]]:
    """非 loopback 接続先の許可リスト（`(host, port)` の集合）。唯一の真実源は admin が `system_settings.ollama_allowlist` に登録した値。
    `OLLAMA_URL` の env はこの許可リストへ加算しない（初回シードだけ）。VLM の送信も同じ許可リストで検証する。
    `system_settings` を渡すとそのスナップショットを使う。
    """
    allowed: set[tuple[str, int]] = set()
    try:
        if system_settings is not None:
            entries = system_settings.get("ollama_allowlist") or []
        else:
            from . import store  # 遅延 import（循環回避）
            entries = store.get_system_settings().get("ollama_allowlist") or []
    except Exception:  # DB 未接続等でも fail-closed（allowlist 空扱い＝loopback 以外は拒否）
        entries = []
    for entry in entries:
        hp = _canonical_host_port(f"http://{entry}")
        if hp is not None:
            allowed.add(hp)
    return allowed


def _assert_host_port_allowed(host: str, port: int, allowed: set[tuple[str, int]]) -> None:
    """`(host, port)` が `allowed` に対して許可されるか（loopback は常に許可・それ以外は集合所属）。`assert_ollama_url_allowed`／`assert_ollama_url_allowed_in` の共有判定。"""
    if is_loopback_host(host):
        return
    if (host, port) in allowed:
        return
    raise SsrfBlocked(f"許可されていない接続先です: {host}:{port}（admin allowlist 未登録）")


def assert_ollama_url_allowed(base: str, *, extra_allowed: set[tuple[str, int]] | None = None,
                              system_settings: dict | None = None) -> None:
    """`base`（Ollama のベース URL）が接続許可ポリシーを満たすか検証する（I/O なし）。
    loopback は許可。それ以外は `_allowlisted_hosts()` に host:port が正規化一致するものだけ許可する。
    `extra_allowed` は呼び出し側が個別に許可した追加の宛先集合（一般の allowlist には影響しない）。不正 URL／不許可の宛先は `SsrfBlocked`。
    `system_settings` は `_allowlisted_hosts()` へ渡す。エラー文言に生の `base` は含めない（`_redact_url_for_error` の安全な host 表現か固定文言のみ）。
    """
    hp = _canonical_host_port(base)
    if hp is None:
        safe = _redact_url_for_error(base) or "（解析できません）"
        raise SsrfBlocked(f"不正な接続先 URL です: {safe}")
    allowed = _allowlisted_hosts(system_settings)
    if extra_allowed:
        allowed = allowed | extra_allowed
    _assert_host_port_allowed(hp[0], hp[1], allowed)


def assert_ollama_url_allowed_in(base: str, allowed: set[tuple[str, int]]) -> None:
    """`base` が呼び出し側の用意した `allowed` 集合だけに対して許可されるか検証する（DB の現行 `ollama_allowlist` は読まない・loopback は常に許可）。
    admin が `ollama_url` と `ollama_allowlist` を同一 PUT で更新するとき、置換後の候補一覧を正本として渡すために使う（`routers/system_extras.py::_validate_central_ollama_url`）。
    エラー文言に生の `base` は含めない。
    """
    hp = _canonical_host_port(base)
    if hp is None:
        safe = _redact_url_for_error(base) or "（解析できません）"
        raise SsrfBlocked(f"不正な接続先 URL です: {safe}")
    _assert_host_port_allowed(hp[0], hp[1], allowed)


# エンドポイント / ヘッダ
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

# 起動時 env シード（`sherpa.api._seed_openai_endpoint_from_env`）の候補検証が不正で確定できなかったときに立てるプロセス内フラグ（DB には書かない）。
# シード失敗時は system_settings に接続先キーが書かれず、DB だけでは「正当な本家既定」と区別できない。黙って本家へ fail-safe すると本家向けでないキーを本家へ送るため、このフラグで遮断する。
# DB 一時障害（`get_system_settings()` が例外）はこのフラグの対象外。
_openai_endpoint_seed_blocked_reason: str | None = None

# `set_openai_endpoint_seed_blocked()` と `begin_openai_send()` を同一ロックで直列化する。
# `assert_openai_io_allowed()` 単体はこのロックを取らない。対象は agentic ループの 3 送信経路（`_send`・`_run_evaluation`・`attribute_openai_style`）。
_openai_send_gate_lock = threading.Lock()


def set_openai_endpoint_seed_blocked(reason: str | None) -> None:
    """起動時 env シードの候補検証が失敗したときに呼ぶ（`reason` が None 以外＝ブロック開始、None は解除でテスト専用）。
    `_openai_send_gate_lock` の下でフラグを立てる。ブロック成立後は新規送信の開始が一件も確定しない。
    """
    global _openai_endpoint_seed_blocked_reason
    with _openai_send_gate_lock:
        _openai_endpoint_seed_blocked_reason = reason


def openai_endpoint_seed_blocked_reason() -> str | None:
    """ブロック中なら理由文字列、ブロックされていなければ `None`。"""
    return _openai_endpoint_seed_blocked_reason


def assert_openai_io_allowed() -> None:
    """OpenAI 系 I/O（HTTP 送信・Codex(OpenAI) の Popen 起動・auth.json 受け渡し）を今行ってよいか検証する公開ガード。
    `openai_url()`/`openai_headers()` の入口に加え、この関数を経由しない経路にも個別に適用する:
    - Codex(OpenAI) の provider 選択時・各 `subprocess.Popen` 直前（`providers/__init__.py`・`providers/codex/provider.py`）。
    - `providers/codex/sandbox.py::_write_codex_authoring_config` の auth.json 受け渡し直前（Codex(Ollama) は対象外）。
    - agentic ループの 3 送信経路は `begin_openai_send()`（本関数を内包）経由で呼ぶ。
    Ollama 経路は対象外。
    """
    reason = _openai_endpoint_seed_blocked_reason
    if reason is not None:
        raise PreflightRejected(
            "OpenAI 接続先の設定が未確定のため停止しています"
            f"（env の設定を修正して再起動してください）: {reason}")


def begin_openai_send(call_budget=None, usage_acc: dict | None = None) -> None:
    """OpenAI 送信の「開始」を原子的に確定する（agentic ループの 3 送信経路が使う）。
    `set_openai_endpoint_seed_blocked` と同一のロック（`_openai_send_gate_lock`）の下で ① ガード確認 ② 呼び出し予算消費 ③ usage 加算 を隙間なく行う。
    - 例外なしで返った送信は、以後 block が成立しても物理送信まで進めてよい（実際の HTTP 送信はロックの外）。
    - block 成立後に呼ばれた場合は、新規の送信開始を確定させない。
    `call_budget`（`.consume() -> bool`）の消費失敗は `SendBudgetExceeded`（ガードの後に消費するため、弾かれた分は消費しない）。`usage_acc` は開始確定時に `calls` を 1 加算する。
    Ollama 等 OpenAI 以外の宛先には使わない。
    """
    with _openai_send_gate_lock:
        assert_openai_io_allowed()
        if call_budget is not None and not call_budget.consume():
            raise SendBudgetExceeded("call 予算の上限に達しました")
        if usage_acc is not None:
            usage_acc["calls"] += 1


def _openai_endpoint_settings(system_settings: dict | None = None) -> dict:
    """接続先関連 4 キーのスナップショット（DB 不達なら空 dict＝組み込み既定へ fail-safe）。`system_settings` を渡すとそれを使う。"""
    if system_settings is not None:
        return system_settings
    try:
        from . import store
        return store.get_system_settings()
    except Exception:
        return {}


def _assert_openai_endpoint_settings_types_valid(sysset: dict) -> None:
    """`openai_endpoint_kind`/`openai_base_url` の保存値の型を検査する（`None` 以外の非文字列は `ValueError`）。
    両関数とも、判定の分岐に入る前に必ずこれを呼ぶ（falsy な非文字列が「未設定」に見えて素通りするのを防ぐ）。
    """
    for key in ("openai_endpoint_kind", "openai_base_url"):
        raw = sysset.get(key)
        if raw is not None and not isinstance(raw, str):
            raise ValueError(f"接続先設定（{key}）の保存値が不正です（文字列ではありません）")


def openai_endpoint_kind(system_settings: dict | None = None) -> str:
    """接続先の種別（`"openai"` 既定 ／ `"azure"` ／ `"custom"`）。
    `system_settings.openai_endpoint_kind` の明示選択が最優先。未設定なら `openai_base_url` から推定する
    （host が `.openai.azure.com`／`.services.ai.azure.com` で終わる → `"azure"`、既定 URL のまま → `"openai"`、それ以外 → `"custom"`）。DB 不達は `"openai"`。
    ホストの末尾 DNS ルートドットは判定前に正規化する。保存値の型検査（`_assert_openai_endpoint_settings_types_valid`）は判定より先に行う。
    """
    sysset = _openai_endpoint_settings(system_settings)
    _assert_openai_endpoint_settings_types_valid(sysset)
    explicit = (sysset.get("openai_endpoint_kind") or "").strip().lower()
    if explicit in ("openai", "azure", "custom"):
        return explicit
    base = (sysset.get("openai_base_url") or "").strip().rstrip("/")
    if not base:
        return "openai"
    try:
        parsed = urlparse(base)
        host = (parsed.hostname or "").lower()
    except ValueError:
        parsed, host = None, ""
    host_norm = host.rstrip(".")
    base_norm = base
    if parsed is not None and host_norm:
        # ホスト表記の大文字小文字・末尾 DNS ルートドットを正規化してから既定 URL/Azure サフィックスを判定する（正規化済みの `host_norm` から netloc を組み直し、port は保持する）。
        _, sep, portpart = parsed.netloc.rpartition(":")
        netloc = f"{host_norm}:{portpart}" if sep and portpart.isdigit() else host_norm
        base_norm = urlunparse(parsed._replace(netloc=netloc)).rstrip("/")
    if base_norm == _DEFAULT_OPENAI_BASE_URL:
        return "openai"
    if host_norm.endswith(".openai.azure.com") or host_norm.endswith(".services.ai.azure.com"):
        return "azure"
    return "custom"


def openai_base_url(system_settings: dict | None = None) -> str:
    """OpenAI 互換 API の base URL（`system_settings.openai_base_url`・既定は OpenAI 本家）。
    種別が `"openai"` の間は、`openai_base_url` に値が残っていても常に組み込み既定を返す。
    末尾スラッシュは落として返す（`openai_url()` が結合時に付け直す）。保存値の型検査は kind の判定より先に行う。
    """
    sysset = _openai_endpoint_settings(system_settings)
    _assert_openai_endpoint_settings_types_valid(sysset)
    if openai_endpoint_kind(sysset) == "openai":
        return _DEFAULT_OPENAI_BASE_URL
    base = (sysset.get("openai_base_url") or "").strip().rstrip("/")
    return base or _DEFAULT_OPENAI_BASE_URL


# 非公開 TLD の代表例（.local に加え、社内で慣習的な .internal/.lan）。網羅は目指さず、判定不能はクラウド側へ倒す。
_PRIVATE_HOST_TLDS = frozenset({"local", "internal", "lan"})
# CGNAT／Shared Address Space（RFC 6598・100.64.0.0/10）。`is_private` に含まれないため別途判定する。
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def endpoint_locality(base_url: str | None) -> str:
    """`base_url` のホストが私有/ローカル範囲か公開範囲かを判定する（`"on_prem"`／`"cloud"`）。`agent_constructs.is_local()` が `"custom"` のときにここへ委ねる。
    DNS 解決はせず、URL 上のホスト表記だけで判定する。
    on_prem: ① private/loopback/link-local/CGNAT な IP ② ホスト名 `"localhost"` ③ 非公開 TLD（`_PRIVATE_HOST_TLDS`・末尾ドットは正規化）④ ドットを含まない裸のホスト名。
    それ以外（公開 FQDN・グローバル IP）と、ホストを解決できない場合（空・不正）は cloud。
    """
    try:
        host = (urlparse(base_url or "").hostname or "").strip().lower()
    except ValueError:
        host = ""
    host = host.rstrip(".")  # DNS ルートドット（FQDN 末尾の "."）を正規化してから判定する
    if not host:
        return "cloud"
    if host == "localhost":
        return "on_prem"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass  # IP リテラルではない＝ホスト名として下で判定する
    else:
        if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
            return "on_prem"
        return "on_prem" if (ip.is_private or ip.is_loopback or ip.is_link_local) else "cloud"
    labels = host.split(".")
    if len(labels) < 2:
        return "on_prem"  # DNS サフィックス無しの裸のホスト名
    return "on_prem" if labels[-1] in _PRIVATE_HOST_TLDS else "cloud"


def assert_openai_endpoint_consistent(kind: str, base_url: str) -> None:
    """`openai_endpoint_kind`/`openai_base_url` の組が矛盾しないか検証する（I/O なし・唯一の真実源）。PUT /admin/settings の部分更新後の実効値・env 初回シード候補・接続テスト（保存前の値）が共有する。
    `kind` が `"openai"` 以外で `base_url` が空だと、実際の送信が黙って本家へ縮退して設定と実挙動が食い違う。不正なら `ValueError`。
    """
    if kind != "openai" and not (base_url or "").strip():
        raise ValueError("接続先が「OpenAI 本家」以外のときは、接続先 URL（openai_base_url）が必要です")


def openai_endpoint_seed_candidate() -> dict:
    """env から起動時シード候補（`openai_base_url`／`openai_endpoint_kind`／`openai_auth_header`／`openai_api_version`）を組み立てる（I/O なし・env のみ読む）。
    4 項目を 1 つの候補として検証し、不正なら `ValueError` を送出して候補全体を返さない（部分的な取り込みはしない）。
    `SHERPA_OPENAI_ENDPOINT_KIND` は明示指定でき、未指定なら `openai_endpoint_kind()` の読み取り時推定に委ねる（ここで host 推定した値は書かない）。
    `sherpa/api.py`（起動時シード）・`scripts/azure_smoke.py`・`scripts/check_production_openai_probe.py` が共有する（stdlib のみのため preflight スクリプトからも import できる）。
    戻り値は空 dict もあり得る（env 未設定）。
    """
    candidate: dict[str, object] = {}
    raw_base = (os.environ.get("OPENAI_BASE_URL") or "").strip().rstrip("/")
    if raw_base and raw_base != _DEFAULT_OPENAI_BASE_URL:
        assert_openai_base_url_allowed(raw_base)  # 不正なら ValueError（候補全体を無効にする）
        candidate["openai_base_url"] = raw_base
    raw_kind = (os.environ.get("SHERPA_OPENAI_ENDPOINT_KIND") or "").strip().lower()
    if raw_kind:
        if raw_kind not in ("openai", "azure", "custom"):
            # エラー文言に生の env 値を含めない（固定 reason code のみ）。
            raise ValueError(
                "invalid_endpoint_kind: SHERPA_OPENAI_ENDPOINT_KIND の値が不正です"
                "（openai/azure/custom のいずれか）")
        candidate["openai_endpoint_kind"] = raw_kind
    raw_auth = (os.environ.get("SHERPA_OPENAI_AUTH_HEADER") or "").strip().lower()
    if raw_auth:
        if raw_auth not in ("bearer", "api-key"):
            raise ValueError(
                "invalid_auth_header: SHERPA_OPENAI_AUTH_HEADER の値が不正です"
                "（bearer/api-key のいずれか）")
        candidate["openai_auth_header"] = raw_auth
    raw_version = (os.environ.get("SHERPA_OPENAI_API_VERSION") or "").strip()
    if raw_version:
        candidate["openai_api_version"] = raw_version
    # クロス検証: 明示 kind が openai 以外なのに base_url が候補に無ければ候補全体を拒否する。kind 未指定（host 推定に委ねる）ならここでは検証しない。
    if "openai_endpoint_kind" in candidate:
        assert_openai_endpoint_consistent(
            candidate["openai_endpoint_kind"], candidate.get("openai_base_url", ""))
    return candidate


def _redact_url_for_error(base: str) -> str | None:
    """エラー文言・ログ・監査に埋め込む前の URL を「安全な host 表現」（`host[:port]`）へ切り詰める。
    `ParseResult` は経由せず、`hostname`／`port` から文字列を組み立てる（scheme は含めない・IPv6 は `format_host_port()` で角括弧を復元）。`;params` 経由の漏洩を避けるため。
    パース不能・host が空なら `None`（呼び出し側が固定文言を使う）。
    """
    try:
        p = urlparse(base)
    except ValueError:
        return None
    host = p.hostname or ""
    if not host:
        return None
    try:
        port = p.port
    except ValueError:
        port = None
    if port is not None:
        return format_host_port(host, port)
    return f"[{host}]" if ":" in host else host


def assert_openai_base_url_allowed(base: str) -> None:
    """`base`（管理画面「接続先」欄の `openai_base_url`）が妥当か検証する（I/O なし）。`_select_provider` の codex 分岐と `sandbox.py::_openai_compat_base_url()` が、Codex 側へ書く/キーを渡す前に同じ検証を通す。
    検証内容:
    - ホスト名は必須。userinfo は禁止。ポートは明示指定時のみ検証する（非数値/範囲外は拒否）。
    - クエリ・フラグメントは禁止（`openai_url()` が `f"{base}/{path}"` で単純連結するため。API バージョンは `openai_api_version` に一本化する）。
    - scheme は `https://` のみ（API キーを平文 HTTP で送らない）。
    - ASCII の印字文字のみ許可し、空白（Unicode 空白含む）・バックスラッシュ・制御文字は拒否する（`urlparse` がこれらを構造区切りとして扱わないため）。
    不正なら `PreflightRejected`。エラー文言に生の `base` は含めない（`_redact_url_for_error` の host 表現か固定文言のみ）。
    """
    if any(c.isspace() or c == "\\" or ord(c) < 0x20 or ord(c) > 0x7E for c in base):
        raise PreflightRejected("接続先 URL に空白・バックスラッシュ・制御文字・ASCII 印字文字以外の"
                                "文字を含められません")
    try:
        p = urlparse(base)
    except ValueError:  # 例: 不正な IPv6 リテラル
        raise PreflightRejected("不正な接続先 URL です（解析できません）") from None
    host = p.hostname or ""
    safe = _redact_url_for_error(base) or "（解析できません）"
    if not host:
        raise PreflightRejected(f"接続先 URL にホスト名がありません: {safe!r}")
    if p.username or p.password:
        raise PreflightRejected(f"接続先 URL にユーザー情報（user:pass@）を含められません: {safe!r}")
    try:
        p.port
    except ValueError:
        raise PreflightRejected(f"接続先 URL のポート番号が不正です: {safe!r}") from None
    if p.query or p.fragment:
        raise PreflightRejected(
            "接続先 URL にクエリ/フラグメントを含められません"
            f"（API バージョンは別欄の openai_api_version で設定してください）: {safe!r}")
    if p.scheme == "https":
        return
    raise PreflightRejected(
        f"接続先 URL は https:// のみ許可されます（API キーを平文 HTTP で送らないため）: {safe!r}")


def openai_api_version(system_settings: dict | None = None) -> str:
    """`system_settings.openai_api_version`（Azure OpenAI の API バージョン等・空文字＝未使用）。種別が `"openai"` なら常に空文字。"""
    sysset = _openai_endpoint_settings(system_settings)
    if openai_endpoint_kind(sysset) == "openai":
        return ""
    return str(sysset.get("openai_api_version") or "").strip()


def openai_url(path: str, system_settings: dict | None = None) -> str:
    """OpenAI 互換 API の URL（`path` は `"chat/completions"`/`"embeddings"`/`"responses"`/`"models"` 等の相対パス）。
    base URL の組み立てに加え、`openai_api_version()` が非空なら `?api-version=<値>` を付ける。base_url の検証（`assert_openai_base_url_allowed`）は呼び出し時に行う。
    起動時 env シードが未確定の間は `RuntimeError` で拒否する（全 OpenAI 系 I/O の fail-closed の入口）。
    """
    assert_openai_io_allowed()
    sysset = _openai_endpoint_settings(system_settings)
    base = openai_base_url(sysset)
    assert_openai_base_url_allowed(base)
    url = f"{base}/{path.lstrip('/')}"
    version = openai_api_version(sysset)
    if version:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}api-version={quote(version, safe='')}"
    return url


def openai_auth_header_style(system_settings: dict | None = None) -> str:
    """`system_settings.openai_auth_header`（`"bearer"` 既定 ／ `"api-key"`）。未知値と、種別が `"openai"` のときは `"bearer"`。"""
    sysset = _openai_endpoint_settings(system_settings)
    if openai_endpoint_kind(sysset) == "openai":
        return "bearer"
    style = str(sysset.get("openai_auth_header") or "bearer").strip().lower()
    return style if style in ("bearer", "api-key") else "bearer"


def openai_headers(key: str, system_settings: dict | None = None) -> dict:
    """OpenAI 互換 API の認証ヘッダ（`openai_auth_header_style()` で切り替え）:
    - `bearer`（既定）: `Authorization: Bearer <key>`。
    - `api-key`: `api-key: <key>`（Azure OpenAI の従来ヘッダ）。
    `openai_url()` と同じゲート（`assert_openai_io_allowed`）を通す。
    `key` は文字列でなければならない（dict/list 等が混ざるとヘッダ値に repr が入り、マスクをすり抜けて漏れうる）。非文字列は即座に拒否して送信を発生させない（fail-closed）。
    """
    if not isinstance(key, str):
        raise RuntimeError(
            "中央 API キーの形式が不正です（設定破損の可能性があります・管理者に確認してください）")
    assert_openai_io_allowed()
    if openai_auth_header_style(system_settings) == "api-key":
        return {"api-key": key, "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


# 既定の接続先（互換用の固定スナップショット）。実際の HTTP 呼び出しは `openai_url()` を経由する。
OPENAI_CHAT_URL = f"{_DEFAULT_OPENAI_BASE_URL}/chat/completions"
OPENAI_EMBED_URL = f"{_DEFAULT_OPENAI_BASE_URL}/embeddings"

JSON_HEADERS = {"Content-Type": "application/json"}  # Ollama（ローカル・認証なし）


def ollama_url(base: str, path: str, *, extra_allowed: set[tuple[str, int]] | None = None,
               system_settings: dict | None = None) -> str:
    """`base` の末尾スラッシュを正規化して `path`（例 "/api/chat"）を連結する。
    URL 構築前に `assert_ollama_url_allowed(base, extra_allowed=..., system_settings=...)` で宛先ポリシーを検証する（全シンク共通の単一チョークポイント）。
    `system_settings` を渡すと `_allowlisted_hosts()` がそれを使う（省略時は自分で読む・DB 書き込み経路に入らないよう、取得済みの呼び出し元は明示的に渡す）。
    """
    assert_ollama_url_allowed(base, extra_allowed=extra_allowed, system_settings=system_settings)
    return base.rstrip("/") + path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx（redirect）を追跡しない。`redirect_request` が None を返すと、元の 3xx が `urllib.error.HTTPError` として呼び出し元に届き、既存の broad except で degrade する。
    （allowlist は接続開始時の宛先しか見ないため、redirect で allowlist 外へ誘導されるのを防ぐ。）
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# redirect を追跡しない opener（既定の ProxyHandler を残し、環境変数 HTTP(S)_PROXY を尊重する）。モジュール読み込み時に 1 回だけ構築する。
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)

def _build_no_proxy_opener() -> urllib.request.OpenerDirector:
    """redirect 非追跡かつ proxy 無効の opener を構築する。`ProxyHandler({})` は環境変数 HTTP(S)_PROXY を読まず常に直結する。
    `ProxyHandler` は構築時にしか env を読まないため、ファクトリとして切り出している。
    """
    return urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


_NO_REDIRECT_NO_PROXY_OPENER = _build_no_proxy_opener()

# `no_proxy_requests()` の有効範囲を表す ContextVar（`with` の外側・別スレッドへは波及しない）。`with` 内で spawn した async task にはコンテキストがコピーされて引き継がれる。
_no_proxy_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar("_llm_no_proxy_ctx", default=False)


@contextlib.contextmanager
def no_proxy_requests():
    """`with` ブロック内の `urlopen_no_redirect()`（延いては `post_json()`）を、環境変数 HTTP(S)_PROXY を無視する専用 opener で行う。
    ローカル/allowlist 済みの Ollama 宛リクエスト専用（現在の呼び出し元は `embeddings._embed_batch` の ollama 分岐）。OpenAI 宛は経由しない。
    """
    token = _no_proxy_ctx.set(True)
    try:
        yield
    finally:
        _no_proxy_ctx.reset(token)


def urlopen_no_redirect(req, timeout=None):
    """Ollama 宛の `urllib.request.urlopen` 相当（3xx を追跡しない）。`post_json`・`providers/ollama.py` のストリーミング・`health.py` の ollama ping が共通で使う。
    `req` は URL 文字列/`Request` のどちらでもよい。`no_proxy_requests()` の `with` 内では proxy 無効の opener を使う。
    """
    opener = _NO_REDIRECT_NO_PROXY_OPENER if _no_proxy_ctx.get() else _NO_REDIRECT_OPENER
    if timeout is None:
        return opener.open(req)
    return opener.open(req, timeout=timeout)


def post_json(url: str, headers: dict, body: dict, timeout: int = 90) -> dict:
    """HTTP POST(JSON)→JSON。HTTP エラーは `urllib.error.HTTPError` を送出する。redirect は追跡しない（`urlopen_no_redirect`）。
    Ollama とも共用するため `assert_openai_io_allowed()` は入れない。OpenAI 宛は `openai_post_json()` を使うこと。
    """
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    with urlopen_no_redirect(req, timeout=timeout) as r:
        return json.loads(r.read())


def openai_post_json(url: str, headers: dict, body: dict, timeout: int = 90) -> dict:
    """OpenAI 系 HTTP 送信専用の `post_json`（embeddings/graph_extract/intent/vision の各シンクが使う）。
    実送信の直前にもう一度 `assert_openai_io_allowed()` を確認し、block 成立後に本文・秘密ヘッダーが送出されるのを防ぐ。
    `post_json(...)` へ委譲する（`monkeypatch.setattr(llm, "post_json", ...)` のシームが効く）。
    """
    assert_openai_io_allowed()
    return post_json(url, headers, body, timeout)


# プロバイダ選択（抽出/埋め込み共通）

def resolve_auto_provider(settings: dict | None, *, system_settings: dict | None = None,
                          strict: bool = False) -> str | None:
    """実際に解決されるプロバイダ名（`select_provider()` の解決の唯一の実装）。
    優先順位: openai（選択中かつキーあり）→ ollama（クラウドを明示選択していないときだけ）→ None。
    `cloud_provider` を admin が明示選択している（`_keys.cloud_provider_explicitly_selected`）場合は、解決できなくても Ollama へ倒さず None を返す。
    `system_settings` を渡すと、選択・キー解決をすべて同じスナップショットで行う。
    `strict` は `_keys.resolve_api_key(strict=...)` へ転送する。実送信の経路（`select_provider()`）は `strict=True`、設定検証/表示は既定のまま。
    """
    from sherpa import keys as _keys, store as _store
    s = settings or {}
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()
    if _keys.resolve_api_key("openai", s, system_settings=sys_s, strict=strict):
        return "openai"
    if not _keys.cloud_provider_explicitly_selected(sys_s) and _keys.resolve_ollama_url(s, system_settings=sys_s):
        return "ollama"
    return None


def select_provider(settings: dict | None, *, openai, ollama,
                    system_settings: dict | None = None, strict: bool = False):
    """プロバイダ設定を選ぶ（該当なしは None）。個人設定の機能別プロバイダ選択は読まず、管理者の設定（カタログ・選択中のクラウドプロバイダ）だけで決まる。
    常に auto 解決。`cloud_provider` を明示選択していれば、解決できなくても他へは倒さない（クラウドを一度も選んでいない構成だけ Ollama を試す）。`openai(key)` / `ollama(url)` は選ばれたプロバイダの設定 dict を作るファクトリ。
    `system_settings` を渡すと読み直しを省く。`strict` は `_keys.resolve_api_key`/`resolve_auto_provider` へ転送する（実際に LLM へ送信する構成を組み立てるため、実行時解決の呼び出し元は `strict=True`）。
    """
    from sherpa import keys as _keys, store as _store
    s = settings or {}
    # provider 名の決定（A7 選択含む）とキー/URL 解決を同じスナップショットで行う。
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()
    okey = _keys.resolve_api_key("openai", s, system_settings=sys_s, strict=strict)
    ourl = _keys.resolve_ollama_url(s, system_settings=sys_s)
    # プロバイダ名の決定は `resolve_auto_provider()` に委ねる。
    resolved = resolve_auto_provider(s, system_settings=sys_s, strict=strict)
    if resolved == "openai":
        return openai(okey)
    if resolved == "ollama":
        return ollama(ourl)
    return None
