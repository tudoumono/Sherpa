"""つながりの確認の共通部品（失敗・流さない・確かめきれなかったの申告、宛先と経路の取り出し、例外の決まった言葉への分類、埋め込みの解決）。
`connectivity` と `connectivity_roundtrip` が使う。
設計: docs/design/operations.md「システム状態とつながりの確認」
"""
from __future__ import annotations

import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

_DIRECT = "直接"
_PROXY = "プロキシ経由"


class _Skip(Exception):
    """「流さない」の申告（reason が表示になる）。"""

    def __init__(self, reason: str, *, paid: bool = False, route=None, host=None):
        super().__init__(reason)
        self.reason, self.paid, self.route, self.host = reason, paid, route, host


class _Incomplete(Exception):
    """確かめきれなかった（設定を読めない等）。"""


class _Ng(Exception):
    """決まった言葉の理由つきの失敗。"""

    def __init__(self, reason: str, *, route=None, host=None, paid=None):
        super().__init__(reason)
        self.reason, self.route, self.host, self.paid = reason, route, host, paid


def host_of(value) -> str | None:
    """URL・`host:port` からホスト名だけを取り出す（資格情報・パス・クエリは捨てる）。"""
    v = str(value or "").strip()
    if not v:
        return None
    try:
        return urllib.parse.urlsplit(v if "://" in v else "//" + v).hostname or None
    except ValueError:
        return None


def route_for(url_or_host, scheme: str = "https") -> str:
    """urllib が実際に選ぶ経路。プロキシが設定され、宛先が例外に当たらなければプロキシ経由。
    例外の照合は urllib の送信と同じ値（URL なら `Request(url).host`＝資格情報があれば含む `host:port`）で行う。
    """
    if not host_of(url_or_host):
        return _DIRECT
    v = str(url_or_host).strip()
    if "://" in v:
        scheme = urllib.parse.urlsplit(v).scheme or scheme
        try:
            target = urllib.request.Request(v).host
        except ValueError:
            return _DIRECT
    else:
        target = v
    if urllib.request.getproxies().get(scheme) and not urllib.request.proxy_bypass(target):
        return _PROXY
    return _DIRECT


def classify(e: BaseException) -> str:
    """例外を決まった言葉の理由にする。例外の文言は出さない。"""
    from . import llm
    seen = set()
    stack = [e]
    while stack:
        c = stack.pop()
        if c is None or id(c) in seen:
            continue
        seen.add(id(c))
        if isinstance(c, llm.SsrfBlocked):
            return "宛先が許可されていない"
        if isinstance(c, urllib.error.HTTPError):
            if c.code in (401, 403):
                return f"認証に失敗（{c.code}）"
            if c.code == 404:
                return "モデルまたは宛先が見つからない（404）"
            if c.code == 429:
                return "利用の上限に達している（429）"
            return f"相手がエラーを返した（{c.code}）"
        if isinstance(c, ssl.SSLCertVerificationError):
            return "証明書の検証に失敗"
        if isinstance(c, (TimeoutError, socket.timeout)):
            return "時間切れ"
        if isinstance(c, ConnectionRefusedError):
            return "接続を拒否された"
        if isinstance(c, socket.gaierror):
            return "名前を引けない"
        text = str(c).lower()
        name = type(c).__name__
        if name in ("AuthError", "Unauthorized") or "password authentication failed" in text \
                or "authentication failed" in text:
            return "認証に失敗"
        if "certificate" in text:
            return "証明書の検証に失敗"
        if "connection refused" in text or "refused" in text:
            return "接続を拒否された"
        if "could not translate host" in text or "name or service not known" in text \
                or "nodename nor servname" in text or "temporary failure in name resolution" in text:
            return "名前を引けない"
        if "timeout" in text or "timed out" in text:
            return "時間切れ"
        stack.extend([getattr(c, "__cause__", None), getattr(c, "__context__", None),
                      getattr(c, "reason", None)])
    return "つながらない"


def _embed_failure_reason(failure, dim, timeout_s=None) -> str:
    if failure is not None and failure.kind == "timeout":
        return (f"時間切れ（1 回の要求の待ち時間 {timeout_s} 秒。管理画面の埋め込みの待ち時間を延ばす）"
                if timeout_s else "時間切れ")
    if failure is None:
        return "応答を得られなかった（時間切れの可能性）"
    if failure.kind == "dimension":
        return f"次元が合わない（設定 {dim}・応答 {failure.got_dim}）"
    if failure.kind == "invalid":
        return "応答の形が正しくない"
    if failure.kind == "deadline":
        return "時間切れ"
    status, etype = failure.status, str(failure.error_type)
    if status in (401, 403):
        return f"認証に失敗（{status}）"
    if status == 404:
        return "モデルが無い（404）"
    if status == 429:
        return "利用の上限に達している（429）"
    if status is not None:
        return f"相手がエラーを返した（{status}）"
    low = etype.lower()
    if "timeout" in low:
        return "時間切れ"
    if "ssl" in low or "certificate" in low:
        return "証明書の検証に失敗"
    if "ssrf" in low or "preflight" in low:
        return "宛先が許可されていない"
    if "connectionrefused" in low:
        return "接続を拒否された"
    if "urlerror" in low or "connection" in low:
        return "つながらない（拒否・名前を引けない・証明書のいずれか。詳しくは API のログ）"
    return "つながらない"


def resolve_embed(sys_s: dict):
    """埋め込みの解決結果（`ai_targets.Target`）。使わない構成は「流さない」、設定が成立しないときは失敗にする。"""
    from . import ai_targets, embeddings
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        raise _Skip("埋め込みを使わない設定になっている")
    try:
        target = ai_targets.embed_target(sys_s, probe=True)
    except Exception:
        raise _Ng("埋め込みの設定を解決できない")
    if target is None:
        if embeddings.cloud_selected_but_unavailable(sys_s):
            raise _Ng("埋め込みの設定を解決できない（選んだ接続先のキー・接続先・モデルを確認）")
        raise _Skip("埋め込みは使っていない")
    return target
