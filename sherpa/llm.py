"""LLM プロバイダ共通層（OpenAI / Gemini / Ollama）。

各呼び出し点で重複していた **エンドポイントURL・ヘッダ生成・HTTP POST・プロバイダ選択**をここに集約する
（agentic_search のツールループ／graph_extract の抽出／embeddings の埋め込み／agents のストリーミング）。
SDK 非依存（urllib）。OpenAI/Gemini へは**本文テキストのみ**送信（ファイル非アップロード・CLAUDE.md）。

注意:
- ストリーミング（agents._stream）は SSE/行ストリームのため `post_json` を通さない＝URL/ヘッダのみ共用。
- `agentic_search._post` / `graph_extract.complete_json` は**テスト差し替えシーム**。これらの薄いラッパ自体は
  各モジュールに残し、本体（URL/ヘッダ/HTTP）だけここへ委譲する。
- `post_json` は HTTP エラー時に `urllib.error.HTTPError` を送出する（呼び元の 429 バックオフ等が依存）。

R2a（SSRF 封じ・2026-07-13 横断レビュー対応）: `ollama_url()` が**単一チョークポイント**＝
embeddings/graph_extract/graph_admin/providers.ollama（agentic＋stream）は全部ここで URL を組み立てる
ため、ここで宛先ポリシーを検証すれば全シンクに効く。既定許可＝loopback のみ（単一ボックス構成の既定
Ollama を保護）。それ以外（RFC1918 含む）は admin が `system_settings` に登録した allowlist
（`ollama_allowlist`・host:port 完全一致）に載っているものだけ許可＝「正当なローカル Ollama」と
「内部 URL」の区別はアドレス種別でなくリスト所属で行う。`SsrfBlocked` は `ValueError` 派生＝各シンクの
既存の broad `except Exception` に自然に乗って degrade する（embeddings→None／intent→None／
ask_graph→failed／agentic 失敗→単発 grep フォールバック等・呼び出し側の変更は不要）。

R2a 追加是正（2026-07-14 横断レビュー対応・Codex RV HIGH×3）:
  #1 ポート省略時、旧実装は無条件で Ollama の正規ポート（11434）を補っていたため、admin が
     `host:11434` を allowlist に登録すると省略ポートの `http://host`（wire port は実際には 80）が
     誤って一致し許可されてしまっていた。`_canonical_host_port` は**scheme の既定ポート**
     （http=80・https=443）を補うよう修正（明示ポートはそのまま）。
  #2 `base`（`ollama_url()`/`assert_ollama_url_allowed()` に渡す接続先）に path/query/fragment が
     混入すると、`ollama_url(base, path)` の `base + path` 連結で呼び出し側が意図した path
     （`"/api/chat"` 等）が上書き/追加され、任意パスへ到達できてしまう（fragment 混入時は urlopen が
     `#` 以降を送信しないため、呼び出し側の path 引数自体が黙って捨てられ base 側の path に固定
     される）。`_canonical_host_port` は path が空/`"/"` 以外・query あり・fragment ありを
     解釈不能（None）として弾く＝`assert_ollama_url_allowed` 経由で全シンク＋書込境界
     （`routers/system.py` の `PUT /settings`・`POST /settings/test`）に一括で効く。
  #3 `post_json`／`providers/ollama.py` の raw ストリーミング urlopen／`health.py` の ollama ping は
     素の `urllib.request.urlopen` を使っており、allowlist 検証後の宛先が 3xx redirect で
     allowlist 外へ誘導されても自動追跡してしまっていた。`urlopen_no_redirect`（本モジュール）へ
     統一し、redirect を追跡しない（3xx はそのまま `HTTPError` として呼び出し側の既存 broad
     except に乗り degrade する）。

OpenAI 互換 API の接続先（Azure OpenAI・その他 OpenAI 互換エンドポイント）は
`system_settings`（DB・管理画面「AIプロバイダ（クラウド）」カードの「接続先」欄）が唯一の真実源
（`openai_endpoint_kind`／`openai_base_url`／`openai_auth_header`／`openai_api_version`）。env
（`OPENAI_BASE_URL` 等）は初回起動時に一度だけ DB へ取り込む（`sherpa.api._seed_openai_endpoint_from_env`）
シード専用で、以後は読まない。`openai_base_url()`/`openai_url()`/`openai_headers()`/
`openai_endpoint_kind()`/`openai_auth_header_style()`/`openai_api_version()` は呼び出し時に毎回
`system_settings` を読む（DB 不達時は組み込み既定「OpenAI 本家・bearer」へ fail-safe）。
`system_settings`（省略可）を渡すと、呼び出し側が既に読んだスナップショットをそのまま使う
（`assert_ollama_url_allowed` 等と同じ流儀）。モジュール定数 `OPENAI_CHAT_URL`/`OPENAI_EMBED_URL` は
互換のため残す（DB を読まない固定の既定値のスナップショット・実際の呼び出しは全て `openai_url()`
を経由しその都度 DB を読む）。
"""
from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import json
import os
import re
import threading
import urllib.request
from urllib.parse import quote, urlparse, urlunparse


class PreflightRejected(RuntimeError, ValueError):
    """権威あるガード（`assert_openai_io_allowed`/`assert_openai_base_url_allowed`/
    `assert_ollama_url_allowed`）が「この I/O は許可されていない」と判定したことを示す共通の
    例外基底（実送信直前の再確認・保存前の事前検証のどちらから呼ばれた場合も同じ型で表す）。

    `RuntimeError`・`ValueError` の両方を継承する＝呼び出し元は `assert_openai_io_allowed` を
    `except RuntimeError`、`assert_openai_base_url_allowed`/`assert_ollama_url_allowed` を
    `except ValueError` で捕捉していても、どちらの型でもそのまま捕捉できる。新しい except 節を
    追加する必要はない。呼び出し元が「未送信（実際の HTTP 送信・Popen 起動等は一切行っていない）」
    ことを型で判定したい場合（`usage_chat.answer_usage_question` 参照）は、この型だけを狙って
    捕捉する。
    """


class SsrfBlocked(PreflightRejected):
    """Ollama 接続先が宛先ポリシー（loopback／admin allowlist）を満たさない。

    `PreflightRejected`（延いては `ValueError`）を継承する＝各呼び出し側の既存の broad
    `except Exception`／`except ValueError`／`except llm.SsrfBlocked`（embeddings._embed_batch・
    graph_extract.extract_one/_probe・graph_admin.ask_graph・providers.base._GenProvider.run・
    routers/system.py・routers/system_extras.py 等）にそのまま乗って安全に degrade する。
    新しい except 節を追加する必要はない。
    """



class SendBudgetExceeded(RuntimeError):
    """`begin_openai_send()` が呼び出し予算の消費に失敗したとき送出する（`assert_openai_io_allowed`
    のブロック起因の `RuntimeError` と区別するための専用型）。"""


def _canonical_host_port(url: str) -> tuple[str, int] | None:
    """`url` を `(host, port)` に正規化する（解釈不能・不正なら None）。

    `urlparse().hostname` は userinfo（`user:pass@`）を除去した上で hostname だけを返すため、
    userinfo 付き URL をそのまま渡すと資格情報が黙って捨てられて `host:port` だけが残る（admin
    allowlist の正規化時に想定外のホストへ丸め込まれる・保存値・監査ログに資格情報が残る、の
    どちらも避けたい）。userinfo（username/password のいずれか）を含む URL は解釈不能として
    None を返す（呼び出し側は不正な接続先として拒否する）。ここでは末尾ドット
    （`example.com.`）の除去だけ追加する。scheme は http/https のみ許可（それ以外は不正として None）。

    R2a #2（2026-07-14）: path が空/`"/"` 以外、または query/fragment を含む URL は解釈不能として
    None を返す（`base` は接続先の起点＝host:port のみを表すべきという契約・詳細はモジュール docstring）。

    R2a #1（2026-07-14）: ポートは明示指定を優先し、無指定なら **scheme の既定ポート**
    （http=80・https=443）を補う（allowlist エントリ・接続先 URL の双方に同じ正規化を適用するため
    両者に同時に効く）。旧実装は無条件で Ollama の正規ポート（11434）を補っており、admin が
    `host:11434` を allowlist に登録すると省略ポートの `http://host`（wire port は実際には 80）が
    誤って一致してしまっていた（詳細はモジュール docstring）。
    """
    try:
        p = urlparse(url or "")
    except ValueError:                       # 例: 不正な IPv6 リテラル（"http://[::1"）
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
    except ValueError:                       # 例: ポートが数値でない/範囲外
        return None
    if port is not None:
        return host, port
    return host, 80 if p.scheme == "http" else 443


def format_host_port(host: str, port: int) -> str:
    """`(host, port)` を **再パース可能な** `host:port` 文字列へ整形する。
    IPv6（`:` を含む host）は角括弧で囲む（`[2001:db8::1]:11434`）＝そうしないと `_allowlisted_hosts()` の
    `http://<entry>` 再パースでホスト/ポート境界が曖昧になり round-trip しない（RV Medium・2026-07-14）。
    """
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def ollama_url_fingerprint(url: str) -> str | None:
    """`url` を「正規化 host:port」の指紋へ縮約する（`_canonical_host_port` と同じ規則＝userinfo・
    path（空/"/" 以外）・query・fragment を含む URL は None）。解釈不能なら None。

    `_redact_url_for_error` の「安全な表示用 host 表現」とは**別物**: あちらはポートが URL に
    明示されている場合しか含めない（表示の簡潔さ優先）ため、`http://host` と `http://host:80` は
    異なる表示になる。ここでは scheme の既定ポート（http=80・https=443）を補う `_canonical_host_port`
    をそのまま使う＝表記ゆれ（ポート省略の有無）があっても同じ接続先なら同じ指紋になる。
    `sherpa.store.settings.catchup_ollama_allowlist_for_env_seeded_url_v2` の tamper 検知（env
    シードが挿入した値と現在の DB 値が「同じ接続先を指しているか」の厳密一致）に使う＝監査には
    生 URL でなくこの指紋だけを残す。
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
    except ValueError:                       # IP リテラルでない（loopback とは判定しない＝非 IP ホスト名）
        return False


def _allowlisted_hosts(system_settings: dict | None = None) -> set[tuple[str, int]]:
    """非 loopback 接続先の許可リスト（`(host, port)` の集合）。

    唯一の真実源は admin が `system_settings.ollama_allowlist`（host:port の文字列リスト）に登録
    したもの（UI(DB) が唯一の真実源・env は初回シードのみという所有原則）。`OLLAMA_URL`／
    `SHERPA_VLM_OLLAMA_URL` の env は system_settings への初回シード（`OLLAMA_URL`）や VLM 専用の
    接続先解決（`vision_arm.vlm_config`）にのみ使い、この一般許可リストへは加算しない
    （admin が管理画面で allowlist から削除した接続先が、env 経由で復活し続ける穴を塞ぐ）。
    VLM 自身の送信は `_read_ollama` が自分の接続先だけを明示的に許可する
    （`ollama_url(..., extra_allowed=...)` 引数・`vision_arm._read_ollama` 参照）。

    `system_settings`（省略可）: 呼び出し側が既に読んだスナップショットを渡すとそれを使う
    （省略時は自分で読む）。
    """
    allowed: set[tuple[str, int]] = set()
    try:
        if system_settings is not None:
            entries = system_settings.get("ollama_allowlist") or []
        else:
            from . import store              # 遅延 import（循環回避・health.py の他 ping と同じ流儀）
            entries = store.get_system_settings().get("ollama_allowlist") or []
    except Exception:                         # DB 未接続等でも fail-closed（allowlist 空扱い＝loopback 以外は拒否）
        entries = []
    for entry in entries:
        hp = _canonical_host_port(f"http://{entry}")
        if hp is not None:
            allowed.add(hp)
    return allowed


def _assert_host_port_allowed(host: str, port: int, allowed: set[tuple[str, int]]) -> None:
    """`(host, port)` が `allowed` に対して許可されるか（loopback は常に許可・それ以外は集合所属）。
    `assert_ollama_url_allowed`／`assert_ollama_url_allowed_in` が共有する判定本体。"""
    if is_loopback_host(host):
        return
    if (host, port) in allowed:
        return
    raise SsrfBlocked(f"許可されていない接続先です: {host}:{port}（admin allowlist 未登録）")


def assert_ollama_url_allowed(base: str, *, extra_allowed: set[tuple[str, int]] | None = None,
                              system_settings: dict | None = None) -> None:
    """`base`（Ollama のベース URL）が接続許可ポリシーを満たすか検証する（I/O なし）。

    既定許可＝loopback のみ。非 loopback は `_allowlisted_hosts()`（admin allowlist・DB の現行値）に
    host:port が正規化一致するものだけ許可。`extra_allowed`（省略可）は呼び出し側が自分の判断で個別に
    許可した追加の宛先集合（例: VLM が自分の設定した接続先だけを許可する・`vision_arm._read_ollama`
    参照）。一般の allowlist（`_allowlisted_hosts()`）には影響しない＝呼び出し元だけに閉じたスコープ
    拡張。不正 URL／不許可の宛先は `SsrfBlocked` を送出する。

    `system_settings`（省略可）: `_allowlisted_hosts()` へそのまま渡す（省略時は自分で読む）。
    1リクエスト内で既に読んだスナップショットと同じ allowlist を見るために使う。

    エラー文言には生の `base` を一切含めない（`_redact_url_for_error` の安全な host 表現、または
    パース不能時は固定文言のみを使う＝`assert_openai_base_url_allowed` と同じ流儀。呼び出し元
    （`usage_chat._resolve_cfg` 等）がこの例外の文言をそのまま 503 の detail に含めるため、
    userinfo・path・query に紛れた秘密や内部 URL がそのまま外部応答へ反射されないようにする）。
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
    """`base` が呼び出し側が用意した `allowed` 集合**だけ**に対して許可されるか検証する（DB の現行
    `ollama_allowlist` は読まない・loopback は常に許可）。

    admin が `ollama_url`（中央既定）と `ollama_allowlist` を**同一 PUT で同時に**更新するとき、
    DB はまだ更新前のため `assert_ollama_url_allowed`（`_allowlisted_hosts()` 経由で必ず旧 allowlist を
    含める）では「新しい URL が、置換されて消えるはずの旧エントリで許可されてしまう」判定になる。
    ここでは呼び出し側が組み立てた**置換後の候補一覧そのもの**を正本として渡す
    （`sherpa/routers/system_extras.py::_validate_central_ollama_url` 参照）。

    エラー文言には生の `base` を一切含めない（`assert_ollama_url_allowed` と同じ流儀・
    `_redact_url_for_error` 参照）。
    """
    hp = _canonical_host_port(base)
    if hp is None:
        safe = _redact_url_for_error(base) or "（解析できません）"
        raise SsrfBlocked(f"不正な接続先 URL です: {safe}")
    _assert_host_port_allowed(hp[0], hp[1], allowed)


# ---- エンドポイント / ヘッダ ----
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

# 起動時 env シード（`sherpa.api._seed_openai_endpoint_from_env`）の候補検証が不正で確定できなかった
# 場合に立てるプロセス内フラグ（DB には書かない）。
#
# なぜ DB だけでは判定できないか: シードが失敗すると system_settings には接続先関連キーが一切
# 書き込まれない（`sherpa.api._openai_endpoint_seed_candidate` 参照）。この状態は「admin が一度も
# 接続先をカスタマイズしていない、正当な OpenAI 本家既定」と DB 上まったく区別が付かない。だが
# 実際には env に「本家以外へ向けるつもりの」不正な設定（例: 壊れた OPENAI_BASE_URL）があった
# 可能性があり、その場合に黙って本家既定へ fail-safe すると、本家向けでないキーを本家へ送って
# しまう（キー漏洩・誤送信）。DB 一時障害（`store.get_system_settings()` が例外）はこれとは別状態
# （次回起動/healthz の再試行任せで良い＝一度もフラグは立たない）。
_openai_endpoint_seed_blocked_reason: str | None = None

# `set_openai_endpoint_seed_blocked()`（block 状態の遷移・healthz の再シードから呼ばれる）と
# `begin_openai_send()`（ガード確認→予算消費→usage 加算＝「送信の開始を確定させる」操作）を
# 同一ロック下で直列化する（下記 `begin_openai_send` docstring 参照）。`assert_openai_io_allowed()`
# 単体（Codex Popen 直前・sandbox の auth.json 受け渡し等）はこのロックを取らない＝対象は
# agentic ループの3送信経路（`_send`・`_run_evaluation`・`attribute_openai_style`）に限る。
_openai_send_gate_lock = threading.Lock()


def set_openai_endpoint_seed_blocked(reason: str | None) -> None:
    """起動時 env シードの候補検証が失敗した時に呼ぶ（`reason` は None 以外＝ブロック開始）。
    `reason=None` は解除（テスト専用・通常運用では env を直して再起動するまで解除しない＝
    プロセス内フラグなのでプロセスを跨いで残らない）。

    `_openai_send_gate_lock` の下でフラグを立てる——`begin_openai_send()` がこのロックを保持して
    「送信の開始を確定させる」最中は、本関数はそのロック解放まで待たされる（＝block はその送信の
    直後にしか成立しない）。逆にこの関数がロックを取って block を成立させた後は、`begin_openai_send`
    は必ずロック取得後に最新の状態を見るため、以後の新規送信は一件も開始が確定しない。
    """
    global _openai_endpoint_seed_blocked_reason
    with _openai_send_gate_lock:
        _openai_endpoint_seed_blocked_reason = reason


def openai_endpoint_seed_blocked_reason() -> str | None:
    """ブロック中なら理由文字列、ブロックされていなければ `None`。"""
    return _openai_endpoint_seed_blocked_reason


def assert_openai_io_allowed() -> None:
    """OpenAI 系 I/O（HTTP 送信・Codex(OpenAI) の Popen 起動・auth.json 受け渡しを含む）を今すぐ
    行ってよいか検証する公開ガード。

    `openai_url()`/`openai_headers()` の入口（唯一のチョークポイント＝これを通れば embeddings・
    graph_extract・providers 全てが自動的に fail-closed になる）に加え、この関数を経由しない
    OpenAI 系 I/O 経路にも個別に適用する:
      - Codex(OpenAI) の provider 選択時・各 `subprocess.Popen` 直前
        （`providers/__init__.py`・`providers/codex/provider.py`）。
      - `providers/codex/sandbox.py::_write_codex_authoring_config` の auth.json 受け渡し直前
        （Codex(Ollama) 構成は対象外）。
      - agentic ループの HTTP 実送信直前（`agentic_search.py` の `_send`・`_run_evaluation`・
        `attribute_openai_style`）: エンドポイント/ヘッダーを入口で1回だけ確定させて使い回す
        設計のため、ブロックが確定/解除されるタイミングを跨いでも各ターンの送信直前に再確認する。
        この3経路はガード確認・予算消費・usage 加算を隙間なく行う必要があるため、本関数を
        直接は呼ばず `begin_openai_send()`（本関数を内包する）経由で呼ぶ。

    Ollama 経路は対象外（呼び出し側が `ollama`/`ollama_base_url` で判定してから呼ぶ）。
    """
    reason = _openai_endpoint_seed_blocked_reason
    if reason is not None:
        raise PreflightRejected(
            "OpenAI 接続先の設定が未確定のため停止しています"
            f"（env の設定を修正して再起動してください）: {reason}")


def begin_openai_send(call_budget=None, usage_acc: dict | None = None) -> None:
    """OpenAI 送信の「開始」を原子的に確定する共通ヘルパー（agentic ループの3送信経路
    `agentic_search.py` の `_send`・`_run_evaluation`・`attribute_openai_style` が使う）。

    `assert_openai_io_allowed()` 単体を「ガード確認 → （呼び出し元が）予算消費 → usage 加算」の
    順で個別に呼ぶ設計は、ガード確認と実際の消費/送信の間に隙間ができる。その隙間で
    `set_openai_endpoint_seed_blocked()`（`/healthz` の再シード等・別スレッドから呼ばれうる）が
    block を成立させると、ガードは通過済みのまま消費/送信が実行されてしまう（強制インターリーブ
    で再現する競合）。本関数は `set_openai_endpoint_seed_blocked` と**同一のロック**
    （`_openai_send_gate_lock`）の下で「ガード確認 → 呼び出し予算消費 → usage 加算」を隙間なく
    行うことで、この競合を線形化する:

    - 契約A（開始確定は完遂してよい）: 本関数が例外を送出せずに返った送信は、以後 block が
      成立しても中断されず物理送信まで進めてよい——**実際の HTTP 送信自体はこのロックの外**
      （呼び出し元が本関数の直後に `_post` する）。ネットワーク I/O でロックを長時間占有しない。
    - 契約B（block 成立後は新規開始ゼロ）: `set_openai_endpoint_seed_blocked` が同じロックを
      取って block を成立させた後に呼ばれる本関数は、ロック取得後に必ず最新の block 状態を見る
      ため、新規の送信開始は一件も確定しない。

    `call_budget`（省略可）: `.consume() -> bool` を持つオブジェクト（`agentic_search._CallBudget`）。
    消費に失敗したら `SendBudgetExceeded` を送出する（ガード確認の**後**・「1物理送信=1消費」
    契約により、ガードで弾かれた分は消費しない）。`usage_acc`（省略可）: `{"calls": int, ...}` の
    dict。送信開始が確定した時点で `calls` を1加算する（実際に発行を試みた回数を数える契約）。

    Ollama 等 OpenAI 以外の宛先には使わない（`assert_openai_io_allowed` と同じ適用範囲・呼び出し元が
    `ollama` フラグで判定してから呼ぶ）。
    """
    with _openai_send_gate_lock:
        assert_openai_io_allowed()
        if call_budget is not None and not call_budget.consume():
            raise SendBudgetExceeded("call 予算の上限に達しました")
        if usage_acc is not None:
            usage_acc["calls"] += 1


def _openai_endpoint_settings(system_settings: dict | None = None) -> dict:
    """接続先関連4キーのスナップショット（DB 不達なら空 dict＝全て組み込み既定へ fail-safe）。
    `system_settings`（省略可）は呼び出し側が既に読んだスナップショットをそのまま使う。"""
    if system_settings is not None:
        return system_settings
    try:
        from . import store
        return store.get_system_settings()
    except Exception:
        return {}


def _assert_openai_endpoint_settings_types_valid(sysset: dict) -> None:
    """`openai_endpoint_kind`/`openai_base_url` の保存値の型を検査する（`None`＝未設定だけを
    許容し、それ以外の非文字列＝`{}`/`[]`/`0`/`False` 等・JSONB は型を強制しない）は `ValueError`
    を送出する）。`openai_endpoint_kind`/`openai_base_url` の両方が、それぞれの判定ロジックへ
    分岐する**前**に必ずこれを呼ぶ契約（呼び出し元の既存 broad except に乗って fail-closed に
    degrade する＝`assert_openai_base_url_allowed` と同じ流儀）。

    kind が `"openai"` または未設定のときに本家既定へ進む分岐がこの型検査より先に来ると、
    base_url が falsy な非文字列でも「未設定」に見えて検査を素通りしてしまう（`value or ""` の
    ような素朴な falsy 潰しの問題と同型＝どちらの分岐を通るかに関わらず、まず型を検査する）。"""
    for key in ("openai_endpoint_kind", "openai_base_url"):
        raw = sysset.get(key)
        if raw is not None and not isinstance(raw, str):
            raise ValueError(f"接続先設定（{key}）の保存値が不正です（文字列ではありません）")


def openai_endpoint_kind(system_settings: dict | None = None) -> str:
    """接続先の種別（`"openai"` 既定 ／ `"azure"` ／ `"custom"`）。

    管理画面「接続先」欄（`system_settings.openai_endpoint_kind`）の明示選択が最優先。未設定
    （管理画面で一度も選ばれていない・env シード前）なら `openai_base_url` から推定する
    （host が `.openai.azure.com`／`.services.ai.azure.com` で終わる → `"azure"`、既定 URL のまま →
    `"openai"`、それ以外 → `"custom"`）。DB 不達時は `"openai"`。

    ホストの末尾 DNS ルートドット（例: `"api.openai.com."`／`"foo.openai.azure.com."`）は判定前に
    正規化する——正規化しないと、実際には本家/Azure を指す URL がここで誤って `"custom"` に
    分類され、`agent_constructs.is_local()` 経由で「クラウド（OpenAI 互換）」表示になるだけで
    なく、`providers/__init__.py` の接続可否チェック（既定モデルのまま Azure 等へ送るのを防ぐ
    ガード）が誤発火して本来接続できるはずの本家/Azure 構成を「未接続」扱いにしてしまう。

    保存値の型検査は `_assert_openai_endpoint_settings_types_valid` 参照（判定分岐より先に行う）。
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
        # ホスト表記の大文字小文字・末尾 DNS ルートドットを正規化してから既定URL/Azureサフィックス
        # 判定を行う——正規化済みの `host_norm`（小文字・ドット無し）から netloc を組み直す
        # （`parsed.netloc` を条件付きで使うと、大文字ホスト＋ルートドット無しのケース
        # （`host == host_norm` で分岐が素通りする）で大文字のまま比較されて "custom" に
        # 誤判定される・port は保持する）。
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

    接続先の種別が `"openai"`（既定・明示選択・または `openai_base_url` 未設定からの推定）の間は、
    `openai_base_url` に値が残っていても常に組み込み既定を返す（管理画面で「OpenAI 本家」へ
    切り替えた後に、古い base_url が黙って有効なまま残ることを防ぐ）。

    末尾スラッシュは落として返す（`openai_url()` が結合時に付け直す＝Azure の
    `https://<res>.openai.azure.com/openai/v1/` のようなスラッシュ付き設定でも二重スラッシュにならない）。
    呼び出し時に毎回 `system_settings` を読む（モジュール docstring 参照）。

    保存値の型検査（`_assert_openai_endpoint_settings_types_valid`）は、kind の判定（"openai" か
    どうか）より**先**に行う: kind=openai／未設定の分岐を型検査より先に置くと、base_url が
    falsy な非文字列（`{}`/`[]`/`0`/`False`）でも「未設定」に見えて型検査へ到達せず本家既定へ
    黙って縮退してしまう（Azure 向けの資格情報が本家 OpenAI へ誤送信されうる・実害）。
    """
    sysset = _openai_endpoint_settings(system_settings)
    _assert_openai_endpoint_settings_types_valid(sysset)
    if openai_endpoint_kind(sysset) == "openai":
        return _DEFAULT_OPENAI_BASE_URL
    base = (sysset.get("openai_base_url") or "").strip().rstrip("/")
    return base or _DEFAULT_OPENAI_BASE_URL


# 非公開 TLD の代表例（RFC 6762 の .local に加え、社内で慣習的に使われる .internal/.lan）。
# 網羅は目指さない——ここに無い私設 TLD は IP/裸ホスト名判定でカバーされないケースが残り得るが、
# 判定不能をクラウド側（誤って「社内」と言い切らない）に倒す設計なので実害は「社内なのにクラウド
# 表示になる」だけに留まる。
_PRIVATE_HOST_TLDS = frozenset({"local", "internal", "lan"})
# CGNAT／Shared Address Space（RFC 6598・100.64.0.0/10）。ISP/社内 NAT 越しの私設アドレス帯だが
# `ipaddress.IPv4Address.is_private` は対象に含めない（IANA の private-use ではなく
# shared-address-space という別区分のため）——明示的に別途判定する。
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def endpoint_locality(base_url: str | None) -> str:
    """`base_url` のホストが私有/ローカル範囲か公開範囲かを判定する（`"on_prem"`／`"cloud"` の2値）。

    `agent_constructs.is_local()` が `openai_endpoint_kind() == "custom"`（管理画面「その他 OpenAI
    互換」）のときにここへ委ねる——`"custom"` は DGX Spark 等の LAN 内エンドポイントも、単に
    OpenAI 本家・Azure 以外の外部クラウド API（サードパーティの OpenAI 互換サービス）も同じ値に
    なるため、"custom" というだけで一律 on_prem 扱いにすると後者を「社内サーバ」と誤表示する。

    **DNS 解決はしない**——`base_url` の URL 上のホスト表記（IP リテラル／ホスト名の文字列）だけで
    判定する（実際に名前解決した先の IP は見ない）。そのため、私有 IP に解決される公開 FQDN
    （社内 DNS が上書きしている等）は文字列表記どおり「クラウド（OpenAI 互換）」表示になる
    （逆に、判定のためだけに DNS へ問い合わせる副作用・失敗時の不確実性を避ける設計）。

    on_prem（いずれか）:
      - ホストが private/loopback/link-local/CGNAT な IP（RFC1918・127.0.0.0/8・169.254.0.0/16・
        100.64.0.0/10（RFC 6598）・::1・fe80::/10 等・`ipaddress` の
        `is_private`/`is_loopback`/`is_link_local`／`_CGNAT_NET` で判定）。
      - ホスト名が `"localhost"`。
      - ホスト名の TLD が非公開（`_PRIVATE_HOST_TLDS`）。末尾の DNS ルートドット
        （例: `"llm.internal."`）は判定前に正規化して取り除く。
      - ホスト名に DNS サフィックスが無い（ドットを含まない裸のホスト名＝社内 DNS 解決前提）。
    それ以外（公開 FQDN・グローバル IP）は cloud。ホストを解決できない（`base_url` が空・不正）
    場合も cloud——「社内サーバ」と誤って言い切らない方を安全側とする。
    """
    try:
        host = (urlparse(base_url or "").hostname or "").strip().lower()
    except ValueError:
        host = ""
    host = host.rstrip(".")   # DNS ルートドット（FQDN 末尾の "."）を正規化してから判定する
    if not host:
        return "cloud"
    if host == "localhost":
        return "on_prem"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass   # IP リテラルではない＝ホスト名として下で判定する
    else:
        if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
            return "on_prem"
        return "on_prem" if (ip.is_private or ip.is_loopback or ip.is_link_local) else "cloud"
    labels = host.split(".")
    if len(labels) < 2:
        return "on_prem"   # DNS サフィックス無しの裸のホスト名
    return "on_prem" if labels[-1] in _PRIVATE_HOST_TLDS else "cloud"


def assert_openai_endpoint_consistent(kind: str, base_url: str) -> None:
    """`openai_endpoint_kind`/`openai_base_url` の組が矛盾しないか検証する（I/O なし・唯一の
    真実源）。PUT /admin/settings の部分更新後の実効値・env 初回シードの候補・管理画面の接続テスト
    （保存前の pending 値）の3箇所が共有する。

    `kind` が `"openai"` 以外なのに `base_url` が空だと、実際の呼び出しは `openai_base_url()` の
    fail-safe で黙って本家へ縮退し、選んだつもりの接続先と実際の送信先が食い違う（見た目の設定と
    実挙動が乖離する、最も危険な種類のバグ）。不正なら `ValueError`。
    """
    if kind != "openai" and not (base_url or "").strip():
        raise ValueError("接続先が「OpenAI 本家」以外のときは、接続先 URL（openai_base_url）が必要です")


def openai_endpoint_seed_candidate() -> dict:
    """env から起動時シード候補（`openai_base_url`／`openai_endpoint_kind`／`openai_auth_header`／
    `openai_api_version`）を組み立てる（I/O なし・DB へは触れない・env のみ読む）。

    4項目を**1つの候補として検証**し、不正なら `ValueError` を送出して**候補全体**を返さない
    （base URL だけ無視して残りは確定する、という部分的な取り込みはしない＝base 抜きで
    auth_header/api_version だけが永久確定する不整合を防ぐ）。

    `SHERPA_OPENAI_ENDPOINT_KIND` は `openai`/`azure`/`custom` のいずれかを**明示的に**指定できる
    （未指定時だけ `openai_base_url` の host から推定＝`openai_endpoint_kind()` の読み取り時
    フォールバックにそのまま委ねる・ここで先回りして host 推定した値を書き込まない＝推定ロジックの
    重複を避け、将来ホスト判定が改善されても既存デプロイへ遡って効く）。

    `sherpa/api.py`（起動時シード本体）・`scripts/azure_smoke.py`（実機疎通確認）・
    `scripts/check_production_openai_probe.py`（`check-production.sh` の env 候補モード）の3箇所が
    同じ検証をここから呼ぶ（自前で再実装しない）。`sherpa/api.py` には FastAPI アプリ全体
    （`fastapi`/`anthropic` 等）を import する重い依存があるため、`sherpa.llm`（軽量・stdlib のみ）
    側に置く＝依存未導入の preflight スクリプトからも安全に import できる。

    戻り値は空 dict もあり得る（env に何も設定されていない＝既定のまま）。
    """
    candidate: dict[str, object] = {}
    raw_base = (os.environ.get("OPENAI_BASE_URL") or "").strip().rstrip("/")
    if raw_base and raw_base != _DEFAULT_OPENAI_BASE_URL:
        assert_openai_base_url_allowed(raw_base)   # 不正なら ValueError（候補全体を無効にする）
        candidate["openai_base_url"] = raw_base
    raw_kind = (os.environ.get("SHERPA_OPENAI_ENDPOINT_KIND") or "").strip().lower()
    if raw_kind:
        if raw_kind not in ("openai", "azure", "custom"):
            # エラー文言に生の env 値を含めない（固定 reason code のみ・
            # `set_openai_endpoint_seed_blocked` の理由文字列としてプロセス内に残り続け、
            # healthz/接続テストのエラー詳細経由で外へ出うるため）。
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
    # クロス検証: 明示 kind が openai 以外なのに base_url が候補に無ければ、この候補全体を拒否する
    # （`assert_openai_endpoint_consistent` は PUT /admin/settings・接続テストとも共有する唯一の
    # 真実源）。kind 未指定（host 推定に委ねる）場合はここでは検証しない＝推定結果は読み取り時に
    # 決まるため、シード時点では判定できない。
    if "openai_endpoint_kind" in candidate:
        assert_openai_endpoint_consistent(
            candidate["openai_endpoint_kind"], candidate.get("openai_base_url", ""))
    return candidate


def _redact_url_for_error(base: str) -> str | None:
    """エラー文言・ログ・監査に埋め込む前の URL を「安全な host 表現」（`host[:port]`）へ切り詰める。

    `ParseResult`（`p._replace(...).geturl()`）は再利用せず、`hostname`／`port` だけから文字列を
    **組み立てる**（scheme は含めない・IPv6 は `format_host_port()` で角括弧を復元）。
    `path`/`query`/`fragment` を空にしてから `geturl()` で再構築する方法は使わない:
    `ParseResult.params`（`;` 区切りの旧式 path パラメータ・例 `/path;sk-secret`）は `_replace` では
    空にならず、`geturl()` が `;params` をそのまま再結合してしまう（scheme/netloc 経由でもれなく
    落としたつもりの秘密が params 経由で漏れる）。`ParseResult` を一切経由しない組み立てなら、
    この種の「見落とした未知フィールド」が構造的に発生しない。

    userinfo（`user:pass@`）は `hostname` に含まれない（`urlparse` が既に除去する）。パース不能
    なら呼び出し側が固定文言を使う契約＝ここでは `None` を返す。host が空でも同様に `None`。
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
    r"""`base`（接続先 URL・管理画面「接続先」欄の `openai_base_url`）が妥当か検証する（I/O なし）。

    `_select_provider`（`providers/__init__.py`）の codex 分岐と `sandbox.py::_openai_compat_base_url()`
    の両方が、Codex CLI 側の config.toml へ書く/子プロセス env にキーを渡す**前に**同じ検証を
    通せるようにする（迂回経路を塞ぐ）。

    検証内容:
      - ホスト名は必須（`https:///v1` のようなホスト空の URL を拒否）。
      - userinfo（`https://user:pass@host/...`）は禁止（意図しない資格情報の埋め込みを弾く）。
      - ポートは明示指定時のみ検証する（`https://host:notaport/v1` のような非数値/範囲外は拒否・
        `urlparse` 自身は `.port` を遅延評価するため、ここで明示的にアクセスしないと素通りする）。
      - クエリ・フラグメントは禁止: `openai_url()` は `f"{base}/{path}"` で単純連結するため、
        `base` にクエリが付くと `.../v1?api-version=x/chat/completions` のように `path` がクエリの
        後ろへ紛れ込み、意図した URL に組み立たない。API バージョンは `openai_api_version`
        （管理画面の別欄）に一本化する契約＝`base` 自体には持たせない。

    scheme は `https://` のみ許可する（API キーを平文 HTTP で送らないため）。不正なら
    `PreflightRejected`（`RuntimeError`/`ValueError` 両方を継承・モジュール docstring 参照）を
    送出する（呼び出し元の既存 broad except に乗って degrade する）。エラー文言には
    生の `base` を一切含めない（`_redact_url_for_error` の安全な host 表現、またはパース不能時は
    固定文言のみを使う＝malformed userinfo に紛れた秘密がログへ残らないようにする）。

    ASCII の印字文字（空白・制御文字を除く）のみを許可し、空白（全角スペース等 Unicode 空白も
    含む）・バックスラッシュ・制御文字は拒否する: `urlparse` はこれらを構造区切りとして扱わない
    ため、ホスト名の妥当性チェックだけではこれらを含む値を弾けない。
    `graph_extract._redact_reflected_urls`（空白区切りの単語単位でマスクする方式）は「URL は
    空白を含まない1トークン」を前提にしており、内部に空白を含む URL が保存されると
    stdlib の `InvalidURL` 経由で scheme/host を伴わない生の request-target を反射しうる
    （`_PLAIN_MULTI_SEGMENT_PATH_RE` が別途これを検出するが、保存時に弾く方が確実で安全）。
    """
    if any(c.isspace() or c == "\\" or ord(c) < 0x20 or ord(c) > 0x7E for c in base):
        raise PreflightRejected("接続先 URL に空白・バックスラッシュ・制御文字・ASCII 印字文字以外の"
                                "文字を含められません")
    try:
        p = urlparse(base)
    except ValueError:                       # 例: 不正な IPv6 リテラル（stdlib 自身の ValueError）
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
    """`system_settings.openai_api_version`（Azure OpenAI の API バージョン等・空文字＝未使用）。
    接続先が `"openai"`（本家）なら常に空文字（本家切替後に他接続先向けの付随値が黙って有効な
    まま残らないよう、`openai_base_url()` と同じ「kind=openai は他の付随値も無視する」契約を揃える）。
    """
    sysset = _openai_endpoint_settings(system_settings)
    if openai_endpoint_kind(sysset) == "openai":
        return ""
    return str(sysset.get("openai_api_version") or "").strip()


def openai_url(path: str, system_settings: dict | None = None) -> str:
    """OpenAI 互換 API の URL（`path` は `"chat/completions"`/`"embeddings"`/`"responses"`/`"models"` 等の相対パス）。

    Azure OpenAI（v1 API）は base URL 配下に OpenAI と同じ相対パスがそのままぶら下がるため、
    base の組み立て＋任意の `api-version` クエリ付与だけで両対応になる（モジュール docstring 参照）。
    `openai_api_version()` が非空を返せば `?api-version=<値>` を付ける（v1 GA では不要・旧方式の
    エンドポイントを使うときだけ要る）。base_url の検証（`assert_openai_base_url_allowed`）は
    ここで**呼び出し時に**行う。

    起動時 env シードが未確定（`assert_openai_io_allowed` 参照）の間は `RuntimeError` で拒否する
    （唯一のチョークポイント＝全 OpenAI 系 I/O をここで一括 fail-closed）。
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
    """`system_settings.openai_auth_header`（`"bearer"` 既定 ／ `"api-key"`）。未知値は `"bearer"`。
    接続先が `"openai"`（本家）なら常に `"bearer"`（`openai_api_version()` と同じ「kind=openai は
    他の付随値も無視する」契約）。"""
    sysset = _openai_endpoint_settings(system_settings)
    if openai_endpoint_kind(sysset) == "openai":
        return "bearer"
    style = str(sysset.get("openai_auth_header") or "bearer").strip().lower()
    return style if style in ("bearer", "api-key") else "bearer"


def openai_headers(key: str, system_settings: dict | None = None) -> dict:
    """OpenAI 互換 API の認証ヘッダ（`openai_auth_header_style()` で切り替え）:
    - `bearer`（既定）: `Authorization: Bearer <key>`（OpenAI 本家の既定。Azure OpenAI v1 API でも
      実機で疎通確認済みのため既定のまま使う。Microsoft 公式の REST 例は、Azure API キーは
      `api-key` ヘッダ、Microsoft Entra ID トークンは `Authorization: Bearer` ヘッダで案内している
      ＝Azure API キーの Bearer 送出そのものを公式に保証したものではない）。
    - `api-key`: `api-key: <key>`（Azure OpenAI の従来ヘッダ・環境によってはこちらしか許可されないことがある）

    `openai_url()` と同じゲート（`assert_openai_io_allowed`）を通す（キーを埋め込んだヘッダを
    組み立てる直前の二重チェック＝`openai_url()` を経由しない直接呼び出しでもキー送信を止める）。

    `key` は文字列でなければならない——設定破損（system_settings の JSONB 値が想定外の型に
    なっている場合）で dict/list 等が渡ってくると、`f"Bearer {key}"` の文字列化でその repr が
    そのままヘッダ値に混入する。この状態で例外メッセージ等へ後からエコーされると、マスク処理の
    汎用パターン（`_BEARER_RE` 等はクォート文字・空白で打ち切られる正規表現）をすり抜けて秘密が
    ログ/応答へ漏洩しうる（実際に再現）。ここで即座に拒否して**送信自体を一切発生させない**
    （fail-closed・呼び出し元の broad except に乗って安全なエラーへ縮退する）。
    """
    if not isinstance(key, str):
        raise RuntimeError(
            "中央 API キーの形式が不正です（設定破損の可能性があります・管理者に確認してください）")
    assert_openai_io_allowed()
    if openai_auth_header_style(system_settings) == "api-key":
        return {"api-key": key, "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


# 既定の接続先（互換のため残す・DB を読まない固定値のスナップショット）。実際の HTTP 呼び出しは
# 全て `openai_url()` を経由し、その都度 `system_settings` を読む（本モジュール定数は使わない）。
OPENAI_CHAT_URL = f"{_DEFAULT_OPENAI_BASE_URL}/chat/completions"
OPENAI_EMBED_URL = f"{_DEFAULT_OPENAI_BASE_URL}/embeddings"

JSON_HEADERS = {"Content-Type": "application/json"}   # Ollama（ローカル・認証なし）

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def gemini_url(model: str, method: str = "generateContent", *, sse: bool = False) -> str:
    """Gemini REST の URL。`method` は generateContent / streamGenerateContent / batchEmbedContents。"""
    url = f"{_GEMINI_BASE}/{model}:{method}"
    return f"{url}?alt=sse" if sse else url


def gemini_headers(key: str) -> dict:
    return {"Content-Type": "application/json", "x-goog-api-key": key}


# Ollama chat のパス。リテラルは llm.py（チョークポイント・SSRF 契約テストの走査除外対象）に
# 集約する——`model_windows.derive_ollama_base_url` が組み立て済み chat URL からの**逆変換**
# （suffix 除去）にも使うため、構築（ollama_url）と逆変換が同じ1定数を共有する。
OLLAMA_CHAT_PATH = "/api/chat"


def ollama_url(base: str, path: str, *, extra_allowed: set[tuple[str, int]] | None = None,
               system_settings: dict | None = None) -> str:
    """`base` の末尾スラッシュを正規化して `path`（例 "/api/chat"）を連結。

    R2a: URL 構築前に `assert_ollama_url_allowed(base, extra_allowed=extra_allowed,
    system_settings=system_settings)` で宛先ポリシーを検証する（全シンク共通の単一チョークポイント＝
    ここを通らない直 urlopen が無いことが SSRF 対策の前提）。`extra_allowed` は呼び出し元だけに閉じた
    追加許可（`assert_ollama_url_allowed` 参照）。

    `system_settings`（省略可）: `assert_ollama_url_allowed()`（延いては `_allowlisted_hosts()`）へ
    そのまま渡す。省略時は `_allowlisted_hosts()` 自身が `sherpa.store.get_system_settings()`
    （未初期化なら `_ensure()`→`init_schema()` を実行する高水準 API）で読みに行くため、既に
    system_settings を読み取り専用 SELECT 等で取得済みの呼び出し元（`scripts/doctor_checks.py` 等）
    は明示的に渡し、DB への書き込み経路（DDL）に入らないようにする。
    """
    assert_ollama_url_allowed(base, extra_allowed=extra_allowed, system_settings=system_settings)
    return base.rstrip("/") + path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx（redirect）を追跡しない（R2a #3・2026-07-14 横断レビュー対応・HIGH）。

    既定の `urlopen` は `Location` ヘッダを自動追跡するため、`assert_ollama_url_allowed` を通過した
    宛先からの応答が redirect 経由で allowlist 外／任意パスへ誘導されうる（allowlist は接続開始時の
    宛先しか見ない）。`redirect_request` が None を返すと urllib は追跡せず、呼び出し元には元の
    3xx レスポンスがそのまま `urllib.error.HTTPError` として届く（各シンクの既存 broad except に
    そのまま乗って安全に degrade する＝新しい except 節は不要）。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# `build_opener(_NoRedirect)` は既定の `HTTPRedirectHandler` を（サブクラスなので）自動的に
# 差し替える（`HTTPDefaultErrorHandler` 等の他の既定ハンドラはそのまま残る＝`ProxyHandler` も既定の
# まま・環境変数 HTTP(S)_PROXY を尊重する＝OpenAI/Gemini 等、通常の企業 proxy 利用を維持したい
# シンクはこのオープナーを使う）。モジュール読み込み時に1回だけ構築する（呼び出しごとの構築は
# 不要・状態を持たない）。
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)

def _build_no_proxy_opener() -> urllib.request.OpenerDirector:
    """`_NO_REDIRECT_NO_PROXY_OPENER` の構築処理をファクトリとして切り出したもの。`ProxyHandler` は
    構築時にしか env を読まないため、任意の時点の env スナップショットから同じ実装で opener を
    再構築できるようにする（呼び出し元は下の singleton 構築、および任意の env で再構築を確認したい
    テスト）。

    `ProxyHandler({})`（空 dict）は環境変数 HTTP(S)_PROXY を一切読まない明示的な「proxy 無効」
    ハンドラ（`urllib.request.ProxyHandler` は引数省略時のみ env を読む・空 dict を渡すと env を
    無視した「常に直結」になる）。redirect 非追跡は `_NO_REDIRECT_OPENER` と同じ。
    """
    return urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


_NO_REDIRECT_NO_PROXY_OPENER = _build_no_proxy_opener()

# `no_proxy_requests()` の有効範囲を表す（`contextvars.ContextVar` は `with` の**外側**や
# 別スレッド・独立した並行タスクへは波及しない＝モジュール全体で1個の可変フラグを使う
# グローバル状態と違い、無関係な並行呼び出しを巻き込まない）。ただし `with` ブロックの**内側**で
# 新規に spawn した async task（`asyncio.create_task` 等）へは、Python の contextvars の仕様どおり
# 現在のコンテキストがコピーされて引き継がれる＝子タスクの中でも True のまま見える。現状の唯一の
# 呼び出し元（`embeddings._embed_batch` の同期的な埋め込みリクエスト）はこの `with` 内で新規タスクを
# spawn しないため実害は無いが、将来 `with` 内から非同期タスクを起こす呼び出し元を追加する場合は
# この継承を踏まえること。
_no_proxy_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar("_llm_no_proxy_ctx", default=False)


@contextlib.contextmanager
def no_proxy_requests():
    """`with` ブロック内の `urlopen_no_redirect()`（延いては `post_json()`）呼び出しを、環境変数の
    HTTP(S)_PROXY を無視する専用 opener（`_NO_REDIRECT_NO_PROXY_OPENER`）で行う（redirect 非追跡は
    どちらの opener でも維持）。

    ローカル/allowlist 済みの Ollama 宛リクエスト専用（現在の唯一の呼び出し元は
    `embeddings._embed_batch` の ollama 分岐・`graph_extract.complete_json` の ollama 分岐はまだ
    使っていない）。企業 proxy 経由でしか外部（OpenAI/Gemini）へ出られない環境で、同じ ambient
    HTTP(S)_PROXY を LAN/localhost 上の Ollama へも適用すると、プロキシが自社ネットワーク宛の
    トラフィックを中継できず接続に失敗する（プロキシ設定は外部向けが前提のため）。OpenAI/Gemini は
    この関数を経由しない既定の `_NO_REDIRECT_OPENER`（env の HTTP(S)_PROXY を尊重）のまま。
    """
    token = _no_proxy_ctx.set(True)
    try:
        yield
    finally:
        _no_proxy_ctx.reset(token)


def urlopen_no_redirect(req, timeout=None):
    """Ollama 宛の `urllib.request.urlopen` 相当（3xx を追跡しない・R2a #3 の単一チョークポイント）。

    `post_json`・`providers/ollama.py` の raw ストリーミング urlopen・`health.py` の ollama ping
    （状態ドットの軽量チェック／管理者の AI 再チェック）から共通で使う。`req` は URL 文字列/
    `urllib.request.Request` のどちらでもよい（`urlopen` と同じ）。

    `no_proxy_requests()` の `with` ブロック内から呼ばれている間は、proxy 無効の専用 opener
    （`_NO_REDIRECT_NO_PROXY_OPENER`）を使う（`_no_proxy_ctx` 参照）。
    """
    opener = _NO_REDIRECT_NO_PROXY_OPENER if _no_proxy_ctx.get() else _NO_REDIRECT_OPENER
    if timeout is None:
        return opener.open(req)
    return opener.open(req, timeout=timeout)


def post_json(url: str, headers: dict, body: dict, timeout: int = 90) -> dict:
    """HTTP POST(JSON)→JSON。HTTP エラーは `urllib.error.HTTPError` を送出（429 等の判定が依存）。
    R2a #3: redirect は追跡しない（`urlopen_no_redirect` 参照）。redirect 非追跡は全プロバイダ共通で
    意図的（公式固定 URL は redirect しない前提・SSRF 対策の単純化優先）。

    Gemini/Ollama とも共用する層＝ここに `assert_openai_io_allowed()` は入れない（OpenAI 専用の
    ブロックで Gemini/Ollama 送信まで一律遮断してしまう）。OpenAI 宛の送信は `openai_post_json()`
    を使うこと。"""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    with urlopen_no_redirect(req, timeout=timeout) as r:
        return json.loads(r.read())


def openai_post_json(url: str, headers: dict, body: dict, timeout: int = 90) -> dict:
    """OpenAI 系 HTTP 送信専用の `post_json`（embeddings/graph_extract/intent/vision の各シンクが
    使う・agentic ループは `agentic_search._send`/インライン確認で別途カバー済み）。

    `openai_url()`/`openai_headers()` は呼び出し時点で `assert_openai_io_allowed()` を確認するが、
    その後・実際にソケットを開くまでの間（Request 構築等）に、並行する `healthz()` 再試行が
    新たに block を成立させる窓がある。ここで実送信（`post_json`）の**直前**にもう一度確認する
    ことで、未確定中に本文・秘密ヘッダーが実際に送出されるのを防ぐ（多層防御の最終段）。
    `post_json` は Gemini/Ollama とも共用のためこのチェックを持たせない＝OpenAI 専用にここへ置く。

    `post_json(...)` へ委譲する（urlopen を再実装しない・`tests/unit/test_metering_sites.py` 等の
    既存の `monkeypatch.setattr(llm, "post_json", ...)` シームがそのまま効く＝モジュール内の
    ベアネーム呼び出しは呼び出し時点でモジュール名前空間を再解決するため、patch 後の関数が呼ばれる）。
    """
    assert_openai_io_allowed()
    return post_json(url, headers, body, timeout)


# ---- プロバイダ選択（抽出/埋め込み共通: extract_provider 駆動）----

# 明示選択として認識する値（この4つ以外は「auto」として扱う＝空文字/未設定/文字列 "auto"/未知の値、
# いずれも同じ auto 解決経路に落ちる）。
_EXPLICIT_PROVIDER_NAMES = frozenset({"openai", "gemini", "ollama", "bedrock"})


def pick_provider_selector(*values: str | None) -> str:
    """`values` を順に見て、最初の非空文字列（strip・小文字化）を返す。全て空なら `"auto"`。

    重大バグ是正（RV 3巡目 #2）: 「機能別 provider（例 intent_provider）が明示的に `"auto"`」と
    「機能別 provider が未設定（空文字/None）で `extract_provider` へ継承する」は**別の状態**だが、
    どちらも「このセルの値」だけを見て次点（`extract_provider`）へフォールバックするかどうかを
    決める、という一段のロジックである点は同じ＝`values` に **既に決まった優先順位の生値**
    （例: `(intent_provider の生値, extract_provider の生値)`）を渡す。明示的な `"auto"` は
    それ自体が非空文字列なので**ここで確定し**、次の値（`extract_provider`）へは進まない
    （`select_provider()` の元々の `x or y or "auto"` 連鎖と同じ規則）。保存時検証
    （`sherpa/routers/system.py::_effective_provider_for_field`）・実行時解決（`select_provider`）・
    UI（`web/settings.js`）は全てこの関数（または JS 側の同一規則の実装）を使い、
    「明示 auto は自動解決・空は継承」という区別を共有する。
    """
    for v in values:
        v = str(v or "").strip().lower()
        if v:
            return v
    return "auto"


def resolve_auto_provider(settings: dict | None, *, bedrock_capable: bool = False,
                          system_settings: dict | None = None, strict: bool = False) -> str | None:
    """`extract_provider`（または機能別 provider）が `auto` のときに実際どのプロバイダへ解決されるか
    （名前だけ）。`select_provider()` の auto 分岐と**同じ優先順位・同じキー解決**（`sherpa.keys`）を
    使う唯一の実装＝ここを共有することで、実行時解決（`select_provider`）と保存時検証
    （`sherpa/routers/system.py::_effective_provider_for_field`）が食い違わない。

    優先順位: 選択中のクラウドプロバイダ（A7）が bedrock かつ `bedrock_capable`（呼び出し元が
    bedrock 対応済み）なら bedrock → openai（選択中かつキーあり）→ gemini（同）→ どれも解決
    できなければ None。bedrock はキー有無に関わらず「試す」対象に含める（認証解決可否の判定自体は
    呼び出し側の factory に委ねる）。

    FBK-1（2026-09-01・fail-loud）: `cloud_provider`（A7）を admin が**明示的に選んでいる**
    （`_keys.cloud_provider_explicitly_selected`）ときは、その選択が解決できなくても Ollama へは
    倒さない（黙って別プロバイダへ縮退すると、選んだクラウド側の障害なのか切り分けられなくなる
    ため）＝ None のまま呼び出し元へ返し、`llm_unavailable`／ベクトル無効等の既存の未接続扱いに
    委ねる。クラウドを一度も選んでいない（生の保存値が無く既定 openai へ読み替えているだけ）
    ときだけ、従来どおり Ollama（接続先が解決できれば）へフォールバックする。

    `system_settings`（省略可）: 呼び出し側が既に読んだ system_settings スナップショットを渡すと、
    A7 選択・各キー解決を**すべて同じスナップショット**で行う（省略時は自分で読む）。
    `select_provider()` はこれを渡してキー解決（`_keys.resolve_api_key` 等）と provider 名決定が
    別々のタイミングで system_settings を読み直し、途中の admin 更新で食い違う窓を作らないように
    する（RV 是正）。

    `strict`（既定 False）: `_keys.resolve_api_key(strict=...)` へそのまま転送する（意図しない
    課金の是正）。実際に送信する経路（`select_provider()` 経由の実行時解決）は `strict=True` を
    渡す。設定検証/表示（`resolve_provider_selection`・`_effective_provider_for_field`）は既定の
    まま＝壊れた設定でも設定画面を止めない。
    """
    from sherpa import keys as _keys, store as _store
    s = settings or {}
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()
    if bedrock_capable and _keys.selected_cloud_provider(sys_s) == "bedrock":
        return "bedrock"
    if _keys.resolve_api_key("openai", s, system_settings=sys_s, strict=strict):
        return "openai"
    if _keys.resolve_api_key("gemini", s, system_settings=sys_s, strict=strict):
        return "gemini"
    if not _keys.cloud_provider_explicitly_selected(sys_s) and _keys.resolve_ollama_url(s, system_settings=sys_s):
        return "ollama"
    return None


def resolve_provider_selection(*values: str | None, settings: dict | None = None,
                               bedrock_capable: bool = False,
                               system_settings: dict | None = None) -> str | None:
    """`values`（優先順位順の生値・例: `(intent_provider, extract_provider)`）から実効プロバイダ名を
    1つに決める。`pick_provider_selector()` で値を選び、それが明示的なプロバイダ名
    （openai/gemini/ollama/bedrock）ならそのまま返す。それ以外（明示 `"auto"`／全て空で
    フォールバックした `"auto"`／未知の値）は `resolve_auto_provider()` で実際に使われる
    プロバイダへ解決する。`select_provider()` の provider 名決定と同じ規則の、設定検証・UI 側
    （`sherpa/routers/system.py::_effective_provider_for_field`）向けラッパー。

    `system_settings`（省略可）: auto 解決（`resolve_auto_provider`）へそのまま渡す（省略時は
    自分で読む）。
    """
    prov = pick_provider_selector(*values)
    if prov in _EXPLICIT_PROVIDER_NAMES:
        return prov
    return resolve_auto_provider(settings, bedrock_capable=bedrock_capable, system_settings=system_settings)


def select_provider(settings: dict | None, *, openai, gemini, ollama, bedrock=None,
                    system_settings: dict | None = None, strict: bool = False):
    """プロバイダ設定を選ぶ（該当なしは None）。個人設定の機能別プロバイダ選択
    （`extract_provider`／`graph_provider`／`intent_provider`／`embed_provider`）は読まない＝
    モデル/プロバイダの選択は管理者の設定（カタログ・選択中のクラウドプロバイダ）だけで決まる。

    常に auto 解決＝管理者が `cloud_provider`（A7）を明示的に選んでいれば、そのプロバイダで解決
    できなければ他へは倒さない（FBK-1・fail-loud）。クラウドを一度も選んでいない構成だけ、
    従来どおり Ollama を試す（`resolve_auto_provider()` 参照・`keys.resolve_api_key` が非選択
    プロバイダを常に None へ倒すため、openai/gemini のどちらが実際に試されるかは A7 の選択が
    決める・env は読まない＝`sherpa/keys.py` 参照）。`openai(key)` / `gemini(key)` / `ollama(url)`
    は選ばれたプロバイダの設定 dict を作るファクトリ。

    `bedrock` は任意（省略可・既存呼び出し元は無指定のままでよい）。他の3つと違い**引数無しの factory**
    （呼べば cfg dict か None を返す＝region/model/api_key を settings からまとめて組み立て、認証解決可否の
    判定も呼び出し側の factory に持たせる）。選択中のクラウドプロバイダが bedrock なら auto の対象に
    含む（factory 未指定・または factory が None を返す＝認証未解決のときは None＝未接続扱い。
    bedrock は `DEFAULT_CLOUD_PROVIDER` ではないため「選択中が bedrock」は常に admin の明示選択＝
    Ollama へは倒さない）。

    `system_settings`（省略可）: 呼び出し側が既に読んだスナップショットを渡すと、ここでの
    読み直しを省く（省略時は自分で読む）。`bedrock` factory は呼び出し側が
    自前で組み立てるため、この関数からは渡せない＝呼び出し側が同じスナップショットを factory の
    クロージャに直接持たせて揃える（`sherpa/ingest/graph_extract.py::available` 参照）。

    `strict`（既定 False）: `_keys.resolve_api_key`/`resolve_auto_provider` へそのまま転送する
    （意図しない課金の是正）。本関数は実際に LLM へ送信する構成（cfg）を組み立てる関数のため、
    呼び出し元（graph 抽出/埋め込み/intent 分類等の実行時解決）は `strict=True` を渡す。
    """
    from sherpa import keys as _keys, store as _store
    s = settings or {}
    # provider 名の決定（A7 選択含む）とキー/URL 解決を**同じスナップショット**で行う
    # （別々に system_settings を読み直すと、途中の admin 更新で食い違う窓ができる）。
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()
    gkey = _keys.resolve_api_key("gemini", s, system_settings=sys_s, strict=strict)
    okey = _keys.resolve_api_key("openai", s, system_settings=sys_s, strict=strict)
    ourl = _keys.resolve_ollama_url(s, system_settings=sys_s)
    # プロバイダ名の決定自体は `resolve_auto_provider()` に委ねる（保存時検証と実行時解決が
    # 同じ優先順位で決まることを構造的に保証する・モジュールレベルの docstring 参照）。ここでは
    # 同じスナップショットを渡し、決まった名前に対応する factory を呼ぶだけ。
    resolved = resolve_auto_provider(s, bedrock_capable=bedrock is not None, system_settings=sys_s,
                                     strict=strict)
    if resolved == "bedrock":
        cfg = bedrock() if bedrock else None
        if cfg is not None:
            return cfg
        # FBK-1（fail-loud）: bedrock が選択中（＝常に admin の明示選択）で factory が None
        # （未対応の消費者、または region 不正等で認証未解決）のときは Ollama へ倒さず未接続
        # のまま返す（呼び出し元の既存の `llm_unavailable`／ベクトル無効等の扱いに委ねる）。
        return None
    if resolved == "openai":
        return openai(okey)
    if resolved == "gemini":
        return gemini(gkey)
    if resolved == "ollama":
        return ollama(ourl)
    return None
