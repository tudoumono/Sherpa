"""社内の宛先をプロキシの例外（`NO_PROXY`／`no_proxy`）へ自動で足す。

設定から分かる宛先のホスト（ES・Neo4j・Postgres の接続先・ループバック・Ollama の既定の接続先と許可リスト）を、環境の
`NO_PROXY` に重複なく足して `os.environ` を更新する。urllib はプロキシの例外を要求のたびに環境から読み、
子プロセス（Codex・MCP）は更新後の環境を受け継ぐ。プロキシの設定そのもの（`HTTP_PROXY` など）は触らない。
呼ぶ時機は ① プロセスの起動時（API・取り込みワーカー・MCP の道具のサーバー） ② system_settings の保存時
③ Codex と MCP の子プロセスを起こす直前 ④ 取り込みワーカーが依頼を 1 件拾うたび ⑤ 取り込みワーカーの中のつながりの確認の前。設定を読めなくても例外を出さず、足せた分だけ足す。
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


def _is_plain_host(host: str) -> bool:
    """Ollama の登録から足してよい形か（ゾーン ID なしの IP のリテラル、または英数字・ハイフン・下線・ドットだけの名前）。
    末尾のドット 1 つは許す。`*`・先頭ドット・空白・ゾーン ID・記号を含むものは足さない（ワイルドカード・先頭ドットの指定を入れない）。"""
    h = host.strip("[]")
    try:
        ipaddress.ip_address(h)
        return "%" not in h
    except ValueError:
        pass
    name = h[:-1] if h.endswith(".") else h
    return bool(name) and not name.startswith(".") and not name.endswith(".") and all(
        c.isascii() and (c.isalnum() or c in "-._") for c in name)


def _forms(host: str) -> list[str]:
    """IPv6 のリテラルは角括弧なし・ありの両方（urllib は `[::1]:port` の形で照合する）。"""
    h = host.strip("[]")
    try:
        if ipaddress.ip_address(h).version == 6:
            return [h, f"[{h}]"]
    except ValueError:
        pass
    return [h]


def _collect(name: str, getter) -> list[str]:
    try:
        return [h for h in (host_of(x) for x in getter()) if h]
    except Exception as e:  # 起動を止めない（接続先が分からない分は足さない）
        _log.warning("プロキシの例外へ足す宛先を読めませんでした（%s）: %s", name, type(e).__name__)
        return []


def internal_hosts(system_settings: dict | None = None) -> list[str]:
    """設定から分かる宛先のホスト（重複なし・順序保持）。ES・Neo4j・Postgres・ループバックはそのまま足し、
    Ollama の登録だけ `_is_plain_host` の形のものに絞る。"""
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

    def ollama():
        ss = settings()
        hosts = (host_of(x) for x in (ss.get("ollama_url") or "", *(ss.get("ollama_allowlist") or [])))
        return [f"[{h}]" if ":" in h else h for h in hosts if h and _is_plain_host(h)]

    hosts: list[str] = []
    for name, getter in (("ES", es), ("Neo4j", neo4j), ("Postgres", postgres), ("Ollama", ollama)):
        hosts += _collect(name, getter)
    hosts += LOOPBACK_HOSTS
    return list(dict.fromkeys(f.lower() for h in hosts for f in _forms(h)))


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
