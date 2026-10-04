"""資料フォルダごとの解決範囲の設定（COPY の取り込み元の場所・パスの別名）。

設計: docs/03-鏡モデル.md §2.2・§6b、docs/proposals/2026-10-04-アナライザとグラフの改善.md 段階 2「案件ごとの解決範囲の設定」。

- 保存場所は `worlds.resolve_settings`（JSON・1 つの資料フォルダに 1 つ）。資料フォルダの中にファイルは置かない。
- 項目は `copy_paths`（優先順の prefix のリスト）と `path_aliases`（別名 → prefix）。prefix は資料フォルダの root からの相対パス。
- prefix は最上位フォルダ（世代）の下だけ。参照元と別の世代にある prefix は解決に使わない（世代跨ぎなし）。
- 解決での使い方（`world_graph._link`）: `copy_paths` は COPY が最近傍で決まらないときだけ先頭から試す。
  `path_aliases` は `#include "別名/x.h"` のようにパスの先頭が別名のとき、別名を prefix に置き換えて実在の相対パスを引く。
- `signature_material()` を `worker._sig` の材料に足す（設定を変えると全件の取り込み）。設定が空なら材料を足さず署名は今のまま。
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import re
from pathlib import Path

MAX_ENTRIES = 50
MAX_LEN = 512
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class ResolveSettingsError(ValueError):
    """設定の検証エラー（利用者に見せる平文の理由）。"""


def empty() -> dict:
    return {"copy_paths": [], "path_aliases": {}}


def normalize_prefix(raw) -> str:
    """prefix を資料フォルダの root からの相対パスに正規化する。`\\`→`/`・先頭末尾と重複の `/`・`.` を除く。

    空（root 全体＝世代をまたぐ）・`..`・ドライブ表記・制御文字は拒否する。
    """
    if not isinstance(raw, str):
        raise ResolveSettingsError("場所は文字列で指定してください")
    s = raw.strip().replace("\\", "/")
    if len(s) > MAX_LEN or _CTRL.search(s):
        raise ResolveSettingsError(f"場所が長すぎるか、使えない文字を含みます: {raw[:40]!r}")
    if re.match(r"[A-Za-z]:", s):                      # `C:/x` も `C:x`（ドライブの相対表記）も拒否
        raise ResolveSettingsError(f"場所は資料フォルダの中からの相対パスで指定してください: {raw!r}")
    segs = [p for p in s.split("/") if p not in ("", ".")]
    if any(p == ".." for p in segs):
        raise ResolveSettingsError(f"場所に「..」は使えません: {raw!r}")
    if not segs:
        raise ResolveSettingsError("場所が空です。資料フォルダの最上位フォルダの下を指定してください（全体は指定できません）")
    return "/".join(segs)


def normalize_alias_key(raw) -> str:
    if not isinstance(raw, str):
        raise ResolveSettingsError("別名は文字列で指定してください")
    k = raw.strip()
    if not k or "/" in k or "\\" in k or k in (".", "..") or len(k) > 128 or _CTRL.search(k):
        raise ResolveSettingsError(f"別名には「/」を含まない 1 語を指定してください: {raw!r}")
    return k


def expand_alias(path: str, aliases: dict) -> str | None:
    """`path` の先頭の 1 語が別名なら prefix に置き換える（別名どうしの連鎖は先頭が別名でなくなるまで展開）。別名でなければ None。"""
    segs = path.replace("\\", "/").split("/")
    if segs[0] not in aliases:
        return None
    for _ in range(len(aliases) + 1):
        head = segs[0]
        if head not in aliases:
            return "/".join(segs)
        segs = aliases[head].split("/") + segs[1:]
    return None


def _check_cycle(aliases: dict) -> None:
    for start in aliases:
        seen = {start}
        cur = aliases[start].split("/")[0]
        while cur in aliases:
            if cur in seen:
                raise ResolveSettingsError(f"別名が循環しています: {start}")
            seen.add(cur)
            cur = aliases[cur].split("/")[0]


def normalize(raw) -> dict:
    """利用者の入力（`{copy_paths?, path_aliases?}`）を正規化して返す。不正は `ResolveSettingsError`。"""
    if raw is None:
        return empty()
    if not isinstance(raw, dict):
        raise ResolveSettingsError("設定の形が正しくありません")
    unknown = set(raw) - {"copy_paths", "path_aliases"}
    if unknown:
        raise ResolveSettingsError(f"未知の項目です: {sorted(unknown)}")
    cp_raw = raw.get("copy_paths") or []
    pa_raw = raw.get("path_aliases") or {}
    if not isinstance(cp_raw, list) or not isinstance(pa_raw, dict):
        raise ResolveSettingsError("設定の形が正しくありません")
    if len(cp_raw) > MAX_ENTRIES or len(pa_raw) > MAX_ENTRIES:
        raise ResolveSettingsError(f"項目は {MAX_ENTRIES} 件までです")
    copy_paths: list[str] = []
    for item in cp_raw:
        p = normalize_prefix(item)
        if p not in copy_paths:
            copy_paths.append(p)
    aliases: dict[str, str] = {}
    for k, v in pa_raw.items():
        key = normalize_alias_key(k)
        if key in aliases:
            raise ResolveSettingsError(f"別名が重複しています: {key}")
        aliases[key] = normalize_prefix(v)
    _check_cycle(aliases)
    return {"copy_paths": copy_paths, "path_aliases": dict(sorted(aliases.items()))}


def warnings_for(settings: dict, *dirs: Path | None) -> list[str]:
    """実在しない prefix の警告（保存は止めない）。`dirs` は資料フォルダの root と、アーカイブ展開先。いずれかにフォルダがあれば実在。"""
    roots = [d for d in dirs if d is not None]
    out: list[str] = []
    for label, prefixes in (("COPY の取り込み元の場所", settings["copy_paths"]),
                            ("別名の指す場所", list(settings["path_aliases"].values()))):
        for p in prefixes:
            actual = expand_alias(p, settings["path_aliases"]) or p      # 別名の連鎖は展開した後の実際の場所で確かめる
            if not any((r / actual).is_dir() for r in roots):
                out.append(f"{label}「{p}」は資料フォルダの中に見つかりません")
    return out


def is_empty(settings) -> bool:
    return not settings or (not settings.get("copy_paths") and not settings.get("path_aliases"))


def signature_material(settings) -> str:
    """正規化した設定の内容のハッシュ。設定が無ければ空文字（署名の材料に足さない）。"""
    if is_empty(settings):
        return ""
    body = json.dumps({"copy_paths": list(settings.get("copy_paths") or []),
                       "path_aliases": dict(sorted((settings.get("path_aliases") or {}).items()))},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


_PINNED: contextvars.ContextVar = contextvars.ContextVar("resolve_settings_pinned", default=None)


@contextlib.contextmanager
def pinned(world_id: str):
    """取り込み 1 回の間、設定を始めに読んだ 1 つの値に固定する（`load` が同じ値を返す＝署名・グラフ・確定の記録が同じ設定を使う）。"""
    token = _PINNED.set((world_id, load(world_id)))
    try:
        yield
    finally:
        _PINNED.reset(token)


def load(world_id: str) -> dict:
    """保存済みの設定を返す（無ければ空）。保存時に正規化済みなので再検証しない。`pinned` の間は固定した値。"""
    pin = _PINNED.get()
    if pin is not None and pin[0] == world_id:
        return {"copy_paths": list(pin[1]["copy_paths"]), "path_aliases": dict(pin[1]["path_aliases"])}
    from .. import store
    row = store.get_world(world_id)
    saved = (row or {}).get("resolve_settings") or {}
    return {"copy_paths": list(saved.get("copy_paths") or []),
            "path_aliases": dict(saved.get("path_aliases") or {})}


def signature_of(world_id: str) -> str:
    return signature_material(load(world_id))


def pending(row) -> bool:
    """保存済みの設定が、最後に確定したグラフに反映されていないか（`worlds` の行から判定・設定が無く未記録も一致とみなす）。"""
    row = row or {}
    return signature_material(row.get("resolve_settings")) != (row.get("resolve_applied_sig") or "")
