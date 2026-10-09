"""安全なファイル走査（`safe_files`）と一意 index（`unique_index`）。
設計: docs/design/scope.md「鏡モデルの核」
"""
from __future__ import annotations

import os
import stat as stat_mod
import time
from pathlib import Path


class ScopeWalkDeadlineExceeded(Exception):
    """`safe_files(deadline=...)` が走査中に期限を超えたことを示す。"""


_DEADLINE_CHECK_ENTRIES = 256  # ディレクトリ内エントリ処理中に期限を再確認する間隔


def rel_scope_meta(rel: str) -> dict:
    """rel_path（POSIX・world root 相対）→ 検索スコープのメタ（top_scope=世代／phase=第2階層／category=第3階層）。
    root 直下ファイルは全て None。
    """
    dirs = rel.split("/")[:-1]
    return {"top_scope": dirs[0] if len(dirs) > 0 else None,
            "phase": dirs[1] if len(dirs) > 1 else None,
            "category": dirs[2] if len(dirs) > 2 else None}


def ancestor_scopes(rel: str) -> list:
    """rel_path → 祖先フォルダ prefix 群（例 `4期/02_設計/x.md`→`["4期","4期/02_設計"]`）。root 直下は `[]`。"""
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _lstat_kind(p: Path) -> str | None:
    """`os.lstat()` で種別（"symlink"/"dir"/"file"/None）を返す。`OSError` は呼び出し元へ伝播させる。"""
    st = os.lstat(p)
    if stat_mod.S_ISLNK(st.st_mode):
        return "symlink"
    if stat_mod.S_ISDIR(st.st_mode):
        return "dir"
    if stat_mod.S_ISREG(st.st_mode):
        return "file"
    return None


VCS_DIR_NAMES = frozenset({".svn", ".git", ".hg", ".bzr", "CVS"})  # 版管理の記録のフォルダ（取り込みの対象外）


def is_vcs_dir_name(name: str) -> bool:
    """フォルダ名が版管理の記録（`.svn`/`.git`/`.hg`/`.bzr`/`CVS`）そのものか（大文字小文字を区別し、名前の完全一致だけ）。
    設計: docs/03-鏡モデル.md「取り込みの対象外」。原本の木を歩く入口は、このフォルダに入らない。
    """
    return name in VCS_DIR_NAMES


WALK_SKIPPED_KEYS = ("symlink", "unreadable_dir", "unreadable_file", "outside_root")


def safe_files(root, *, strict: bool = False, deadline: float | None = None, also=None,
               skipped: dict | None = None):
    """`root` 配下の実ファイルを列挙する（symlink の file/dir は辿らない・root 外へ出ない）。要素は `(resolved_path, rel_posix)`。

    - `also`: 第2の root（アーカイブ展開先）も同じ規律で列挙して連結する（1 段のみ）。存在しなければ何も足さない。
    - `strict=False`: 権限エラー・消失などの `OSError` は該当箇所だけ skip する。
    - `strict=True`: 同じ `OSError` を re-raise する（「見えなかった」を「無かった」にしない経路向け）。
    - `deadline`: `time.monotonic()` 系の絶対期限。開始時・ディレクトリごと・エントリ列挙中・後処理完了時に確認し、
      超過で `ScopeWalkDeadlineExceeded` を送出する（部分結果は返さない）。
      1 回のシステムコール自体がブロックする場合は打ち切れない。
    - `skipped`: 渡すと、走査で数えられなかったものの件数を `WALK_SKIPPED_KEYS` ごとに足す（`symlink`＝辿らなかったシンボリックリンク／
      `unreadable_dir`＝列挙できなかったフォルダ／`unreadable_file`＝種別・実体を取れなかったエントリ／`outside_root`＝root の外へ出る実体）。
      列挙の結果は変えない。名前は持たない（件数のみ）。
    """
    # `also` が実在するときだけ連結する（無ければ単一 root の走査のまま）。
    if also is not None and Path(also).is_dir():
        yield from safe_files(root, strict=strict, deadline=deadline, skipped=skipped)
        yield from safe_files(also, strict=strict, deadline=deadline, skipped=skipped)
        return
    def _skip(key: str) -> None:
        if skipped is not None:
            skipped[key] = skipped.get(key, 0) + 1

    root = Path(root)
    if deadline is not None and time.monotonic() > deadline:
        raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")
    try:
        root_kind = _lstat_kind(root)
    except OSError:
        if strict:
            raise
        _skip("unreadable_dir")
        return
    if root_kind != "dir":  # root 自体が symlink/非ディレクトリなら走査しない
        return
    rootr = root.resolve()
    stack = [root]
    while stack:
        if deadline is not None and time.monotonic() > deadline:
            raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")
        current_dir = stack.pop()
        try:
            # ソート前の列挙段階で `_DEADLINE_CHECK_ENTRIES` 件ごとに期限を確認する。
            raw_entries: list[Path] = []
            with os.scandir(current_dir) as it:
                for i, entry in enumerate(it):
                    if (deadline is not None and i > 0
                            and i % _DEADLINE_CHECK_ENTRIES == 0
                            and time.monotonic() > deadline):
                        raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")
                    raw_entries.append(Path(entry.path))
            # 列挙完了時にも期限を確認する。
            if deadline is not None and time.monotonic() > deadline:
                raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")
            entries = sorted(raw_entries)
        except OSError:
            if strict:
                raise
            _skip("unreadable_dir")
            continue
        for i, p in enumerate(entries):
            if (deadline is not None and i > 0
                    and i % _DEADLINE_CHECK_ENTRIES == 0
                    and time.monotonic() > deadline):
                raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")
            try:
                kind = _lstat_kind(p)
            except OSError:
                if strict:
                    raise
                _skip("unreadable_file")
                continue
            if kind == "symlink":  # symlink は file/dir とも辿らない
                _skip("symlink")
                continue
            if kind == "dir":
                if not is_vcs_dir_name(p.name):
                    stack.append(p)
            elif kind == "file":
                try:
                    rp = p.resolve()
                except OSError:
                    if strict:
                        raise
                    _skip("unreadable_file")
                    continue
                if rp.is_relative_to(rootr):  # root 外への脱出を拒否
                    yield rp, p.relative_to(root).as_posix()
                else:
                    _skip("outside_root")
        # 後処理完了時にも期限を確認する。
        if deadline is not None and time.monotonic() > deadline:
            raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")


def unique_index(items, keyfn):
    """`items` から `keyfn` 一意の index を作る。衝突キーは fail-closed で除外する（先勝ちにしない）。
    戻り: `(index{key: item}, collisions{key: [item,...]})`。
    """
    index: dict = {}
    seen: dict = {}
    for it in items:
        k = keyfn(it)
        seen.setdefault(k, []).append(it)
    collisions = {k: v for k, v in seen.items() if len(v) > 1}
    for k, v in seen.items():
        if len(v) == 1:
            index[k] = v[0]
    return index, collisions
