"""個人ファイル（本人の workspace）の grep と、その結果の事実テキスト・出典への整形。
設計: docs/design/chat.md「個人ファイルの参照」
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from . import store, text_encoding
from .ingest import text_kind
from .ingest.analyzers import registry as _analyzer_registry

_log = logging.getLogger("sherpa")

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}"
    r"|-----BEGIN[^-]+PRIVATE KEY-----[\s\S]*?-----END[^-]+PRIVATE KEY-----)"
)
_KV_SECRET_RE = re.compile(r"(?i)\b(pass(?:word|wd)?|secret|api[_-]?key|token|authorization)\b(\s*[=:]\s*)(\S+)")


def _redact(text: str) -> str:
    """外部 LLM/画面へ渡る検索抜粋の明らかな秘密を伏せる（agentic_search と同じ多層防御）。"""
    t = _SECRET_RE.sub("[REDACTED]", text or "")
    return _KV_SECRET_RE.sub(r"\1\2[REDACTED]", t)


# 個人ファイル参照の許可拡張子（workspace upload 許可と同じ集合・`api.py` の `_WORKSPACE_SEARCHABLE_EXT` と同義）。`api.py` に依存しないためここで重複定義する。個人領域は共有 KB のアナライザ登録簿とは別の独立集合で、コード分は和集合に含める。
_PERSONAL_SEARCHABLE_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".sql", ".py", ".sh", ".bat",
} | _analyzer_registry.registered_extensions()


class _PersonalHits(list):
    """`_personal_grep_hits` の戻り値。`unreadable`＝読み取り・復号に失敗して調べられなかった個人ファイルの件数。"""

    unreadable: int = 0


def _personal_grep_hits(user_id: str, query: str, users_dir: str) -> list[dict]:
    """ユーザーの個人 workspace を台帳基準で grep し、ヒット一覧を返す。
    - 検索は `personal_workspace_files` 台帳上の status='uploaded' ファイルのみ（FS 残骸を拒否）。
    - base は users_dir / uid / workspace / files に閉じ込める（symlink・パストラバーサル拒否）。
    - ES/Neo4j・共有 KB には触れない。他ユーザーの uid は引数で分離する。
    - 秘匿名（`text_kind.is_sensitive_doc_id`）のファイルは読まない。
    - 読み取り・復号に失敗したファイルは、戻り値の `unreadable` に件数を残す（一致なしと区別する）。
    """
    if not query or not query.strip():
        return []
    q = query.strip()
    q_lower = q.lower()
    files_dir = (Path(users_dir).resolve() / user_id / "workspace" / "files")
    live_paths = store.live_workspace_rel_paths(user_id)
    hits = _PersonalHits()
    # files/ ディレクトリ自体が symlink・無い場合は拒否する（confinement 破壊防止）。台帳にあるファイルは読めなかった数に入れる
    if files_dir.is_symlink() or not files_dir.is_dir():
        hits.unreadable = len(live_paths)
        return hits
    seen: set[tuple] = set()
    files_dir_resolved = files_dir.resolve()
    for rel_path in sorted(live_paths):
        raw = files_dir / rel_path
        # 台帳にあるのに調べられないもの（symlink・root の外・実体が無い）は、読めなかった数に入れる（「一致なし」に見せない）。
        if raw.is_symlink():
            hits.unreadable += 1
            continue
        target = (files_dir / rel_path).resolve()
        try:
            target.relative_to(files_dir_resolved)
        except ValueError:
            hits.unreadable += 1
            continue
        if not target.is_file():
            hits.unreadable += 1
            continue
        if target.suffix.lower() not in _PERSONAL_SEARCHABLE_EXT:
            continue
        # 秘匿名のファイルは本文を推論へ渡さない（共有 KB と同じ判定）。
        if text_kind.is_sensitive_doc_id(rel_path):
            continue
        try:
            raw_bytes = target.read_bytes()
            enc = text_encoding.detect_bytes(raw_bytes, complete=True)
            lines = text_encoding.decode(raw_bytes, enc).splitlines()
        except Exception as e:
            hits.unreadable += 1
            _log.warning("個人ファイルを読み取れませんでした: %s", type(e).__name__)
            continue
        for i, ln in enumerate(lines):
            if q_lower not in ln.lower():
                continue
            s = max(0, i - 1)
            e = min(len(lines), i + 3)
            key = (rel_path, s, e)
            if key in seen:
                continue
            seen.add(key)
            hits.append({
                "rel_path": rel_path,
                "line": i + 1,
                "text": _redact("\n".join(lines[s:e]).strip()),
                "match": q,
                "source": "個人ファイル内ヒット",
            })
            if len(hits) >= 20:
                break
        if len(hits) >= 20:
            break
    return hits


def _personal_facts(hits: list[dict], query: str) -> str:
    """個人ファイルのヒットを LLM への事実テキストに整形する。このテキストは AI への入力のみで、ES/Neo4j には書かない。"""
    if not hits:
        return ""
    parts = []
    for h in hits[:8]:
        parts.append(f"[個人ファイル: {h['rel_path']} 行{h['line']}] {h['text'][:200]}")
    return "\n【個人ファイル内ヒット（本人のみ参照可・共有不可）】\n" + "\n".join(parts)


def _personal_citations(hits: list[dict]) -> list[dict]:
    """個人ファイルのヒットを citation 形式に変換する。`source` フィールドで共有 KB citation と区別し、DL リンクは付けない（個人 workspace 専用）。"""
    seen_rel: set[str] = set()
    cites: list[dict] = []
    for h in hits:
        rel = h["rel_path"]
        if rel not in seen_rel:
            seen_rel.add(rel)
            cites.append({
                "doc_id": rel,
                "quote": h["text"][:80],
                "source": "個人ファイル内ヒット",
            })
    return cites


def personal_unreadable_notice(hits) -> str:
    """読み取れなかった個人ファイルがあるときの回答の注記（無ければ空）。"""
    n = getattr(hits, "unreadable", 0)
    return f"個人ファイル{n}件を読み取れなかったため、読めたファイルだけを調べています。" if n else ""
