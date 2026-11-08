"""社内の宛先をプロキシの例外（`NO_PROXY`／`no_proxy`）へ自動で足す。

設定から分かる宛先のホスト（ES・Neo4j・Postgres の接続先・ループバック・社内と分かる Ollama の接続先と許可リスト）を、環境の
`NO_PROXY` に重複なく足して `os.environ` を更新する。urllib はプロキシの例外を要求のたびに環境から読み、
子プロセス（Codex・MCP）は更新後の環境を受け継ぐ。プロキシの設定そのもの（`HTTP_PROXY` など）は触らない。
呼ぶ時機は ① プロセスの起動時（API・取り込みワーカー・MCP の道具のサーバー） ② system_settings の保存時
③ Codex と MCP の子プロセスを起こす直前。設定を読めなくても例外を出さず、足せた分だけ足す。
設計: docs/design/operations.md「起動・停止・状態」
"""
from __future__ import annotations

import ipaddress
import logging
import os
import urllib.parse

_log = logging.getLogger("sherpa")

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

_base_existing: str | None = None  # 最初の `apply()` 時点の利用者の値（足した分を次回へ引き継がない）


def reset_base() -> None:
    """覚えた利用者の元の値を忘れる（テスト用）。"""
    global _base_existing
    _base_existing = None


def host_of(value: str | None) -> str | None:
    """URL・`host:port`・ホスト名からホスト部分だけを取り出す（取れなければ None）。"""
    v = (value or "").strip()
    if not v:
        return None
    try:
        host = urllib.parse.urlsplit(v if "://" in v else "//" + v).hostname
    except ValueError:
        return None
    return host or None


def is_internal_host(host: str) -> bool:
    """社内と分かるホストか（DNS は引かない）。IP はプライベート・ループバック・リンクローカル、名前は
    `localhost`・`.local` 終わり・ドットの無い名前。"""
    h = host.strip().strip("[]").lower().rstrip(".")
    if not h:
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return h == "localhost" or h.endswith(".local") or "." not in h
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _collect(name: str, getter) -> list[str]:
    try:
        return [h for h in (host_of(x) for x in getter()) if h]
    except Exception as e:  # 起動を止めない（接続先が分からない分は足さない）
        _log.warning("プロキシの例外へ足す宛先を読めませんでした（%s）: %s", name, type(e).__name__)
        return []


def internal_hosts(system_settings: dict | None = None) -> list[str]:
    """設定から分かる社内の宛先のホスト（重複なし・順序保持）。"""
    def settings() -> dict:
        if system_settings is not None:
            return system_settings
        from sherpa import store
        return store.get_system_settings()

    def es():
        from sherpa import es_index
        return [es_index._url()]

    def neo4j():
        from sherpa.ingest import world_neo4j
        return [world_neo4j.default_neo4j_uri()]

    def postgres():
        from psycopg.conninfo import conninfo_to_dict

        from sherpa import store
        raw = str(conninfo_to_dict(store._dsn()).get("host") or "")
        return [h for h in raw.split(",") if h and not h.startswith("/")]

    skipped = 0

    def ollama():
        nonlocal skipped
        ss = settings()
        hosts = [host_of(x) for x in (ss.get("ollama_url") or "", *(ss.get("ollama_allowlist") or []))]
        hosts = [h for h in hosts if h]
        internal = [h for h in hosts if is_internal_host(h)]
        skipped = len(hosts) - len(internal)
        return internal

    hosts: list[str] = []
    for name, getter in (("ES", es), ("Neo4j", neo4j), ("Postgres", postgres), ("Ollama", ollama)):
        hosts += _collect(name, getter)
    if skipped:
        _log.info("社内と分からない Ollama の宛先はプロキシの例外へ足しません（%d 件）", skipped)
    hosts += LOOPBACK_HOSTS
    return list(dict.fromkeys(h.lower() for h in hosts))


def merged_no_proxy(extra: list[str], existing: str = "") -> str:
    """既存の `NO_PROXY` の値へ `extra` を重複なく（大文字小文字を区別せず）足した値。"""
    parts: list[str] = []
    seen: set[str] = set()
    for h in [*(p.strip() for p in existing.split(",")), *extra]:
        if h and h.lower() not in seen:
            seen.add(h.lower())
            parts.append(h)
    return ",".join(parts)


def apply(system_settings: dict | None = None) -> str:
    """`os.environ` の `NO_PROXY` と `no_proxy` を同じ値で更新して返す。失敗しても例外は出さない。"""
    global _base_existing
    if _base_existing is None:
        _base_existing = ",".join(p for p in (os.environ.get("NO_PROXY", ""), os.environ.get("no_proxy", "")) if p)
    existing = _base_existing
    try:
        value = merged_no_proxy(internal_hosts(system_settings), existing)
    except Exception as e:
        _log.warning("プロキシの例外の更新に失敗しました: %s", type(e).__name__)
        return existing
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value
    return value
