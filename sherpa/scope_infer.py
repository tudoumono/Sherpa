"""安全なファイル走査と一意 index（鏡モデルの基盤プリミティブ）。

**旧 auto-scope 推定層は撤去**（MIRROR-MODEL §3/§7 step6＝範囲はフォルダパスそのもの・推定しない）。
本モジュールには `safe_files`（symlink を辿らない・root 限定の走査）と `unique_index`（衝突 fail-closed）
だけを残し、`world_graph`/`documents`/`corpus_docs` が共用する（DL・台帳・グラフ解決の単一の走査）。
"""
from __future__ import annotations

import os
import stat as stat_mod
import time
from pathlib import Path


class ScopeWalkDeadlineExceeded(Exception):
    """`safe_files(deadline=...)` が木走査中にデッドラインを超えたことを示す（呼び出し元が翻訳する・
    PART-4 は 504 にする）。"""


_DEADLINE_CHECK_ENTRIES = 256   # `safe_files(deadline=...)` がディレクトリ内エントリ処理中に
# デッドラインを再確認する間隔（`safe_files` docstring 参照・1ディレクトリに大量のファイルが
# ある場合でも、次のディレクトリ境界を待たずに打ち切れるようにする）。


def rel_scope_meta(rel: str) -> dict:
    """rel_path（POSIX・world root 相対）→ 検索スコープのメタ（dir 成分から top_scope/phase/category）。

    `top_scope`=世代（トップフォルダ）／`phase`=第2階層／`category`=第3階層。root 直下ファイルは全て None。
    world_graph / corpus_docs が共用（同一導出の単一の真実源・rv-full B3）。グラフ所属判定や scope_path 付与は別概念（吸収しない）。
    """
    dirs = rel.split("/")[:-1]
    return {"top_scope": dirs[0] if len(dirs) > 0 else None,
            "phase": dirs[1] if len(dirs) > 1 else None,
            "category": dirs[2] if len(dirs) > 2 else None}


def ancestor_scopes(rel: str) -> list:
    """rel_path → 祖先フォルダ prefix 群（例 `4期/02_設計/x.md`→`["4期","4期/02_設計"]`）。root 直下は `[]`。

    ES の所属 prefix（es_index）と範囲ツリーの prefix 集合（scope）が共用（rv-full B3）。件数/label 生成は呼び出し側。
    """
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _lstat_kind(p: Path) -> str | None:
    """`os.lstat()` を直接呼んで種別を返す（"symlink"/"dir"/"file"/None）。

    `pathlib.Path.is_dir()`/`is_file()`/`is_symlink()` は内部で `OSError` を握って `False` を
    返す（ドキュメント上の仕様）——`safe_files(strict=True)` がそれらをそのまま使うと、途中で
    root/entry が消えた・権限が変わった等の「見えなかった」が黙って「無かった」に潰れてしまう。
    ここでは `os.lstat()` を直接呼び、`OSError` を**呼び出し元へ伝播させる**（strict/非strict の
    分岐は呼び出し元が行う・単一の判定点）。
    """
    st = os.lstat(p)
    if stat_mod.S_ISLNK(st.st_mode):
        return "symlink"
    if stat_mod.S_ISDIR(st.st_mode):
        return "dir"
    if stat_mod.S_ISREG(st.st_mode):
        return "file"
    return None


def safe_files(root, *, strict: bool = False, deadline: float | None = None):
    """`root` 配下の実ファイルを安全に列挙（**symlink file/dir を辿らない**・root 限定）。

    各要素は `(resolved_path, rel_posix)`。symlink は file/dir とも prune（脱出/誤参照を防ぐ）。

    `strict=False`（既定・内部 UI 向け）: root/ディレクトリ列挙/各エントリの種別判定で起きる
    `OSError`（権限エラー・途中での消失等）は該当箇所だけ黙って skip する（部分的な結果でも
    取込/表示を止めない寛容な挙動・従来どおり）。
    `strict=True`（外部 API の実在フィルタ等）: 同じ `OSError` を**すべての箇所で**re-raise する
    ——「見えなかった」を「無かった」に取り違えて存在しないかのように返してはいけない経路向け
    （呼び出し側が 503 にする・従来は `iterdir()` 由来の `OSError` しか伝播せず、
    `Path.is_dir()`/`is_file()`/`is_symlink()` 由来（`_lstat_kind` に置き換え済み）は素通りで
    `False` に潰れていた）。

    `deadline`（省略可・`time.monotonic()` 系の絶対期限。既定 None＝無期限＝既存呼び出し元は
    無変更）: 指定時、以下の**すべての境界**で確認し、超えていれば即座に `ScopeWalkDeadlineExceeded`
    を送出して打ち切る（部分結果を黙って返さない・一貫した契約）——
    (1) 関数の開始直後（root の `lstat`/`resolve` より前）、
    (2) **1ディレクトリを処理するごと**（`while` ループの各反復の先頭）、
    (3) **1ディレクトリ内のエントリ列挙中**（`os.scandir` のイテレーション・ソートの**前**・
    `_DEADLINE_CHECK_ENTRIES` 件ごと）、
    (4) **列挙完了時**（列挙件数が間引き間隔未満だと (3) のチェックが一度も発火しないまま
    完了しうるため）、
    (5) **1ディレクトリ内のエントリを処理するごと**（ソート済み集合の処理ループ・
    `_DEADLINE_CHECK_ENTRIES` 件ごと）、
    (6) **1ディレクトリの後処理完了時**（処理件数が間引き間隔未満だと (5) のチェックが
    一度も発火しないまま完了しうるため）。
    `sorted(...)` はイテレータを全件消費してから返るため、ソート**後**の集合だけをチェックしても、
    単一ディレクトリに大量のファイルがある場合はその列挙・ソート自体がデッドラインより先に
    リクエスト全体の予算を食い潰しうる（巨大/深いフォルダ木の走査、単一ディレクトリに大量の
    ファイルがあるケース、間引き間隔未満の小規模ディレクトリが連続するケースのすべてに対する
    防御・PART-4 の scope_paths 検証専用）。

    契約上の限界: **1エントリの `os.scandir()`/`os.lstat()` 呼び出し自体がブロックする**場合
    （マウント先の I/O 停止・ネットワークドライブの断等）は、このチェックの間隔より先にその
    1回の呼び出し自体が止まるため打ち切れない——Python の同期 I/O では個々のシステムコールを
    途中でタイムアウトさせられない（OS レベルで bound できない）。この防御はあくまで「多数の
    軽いディレクトリを辿る合計時間」を抑えるものであり、単一の重い I/O 待ちからは守らない
    （既知の限界として受容する）。
    """
    root = Path(root)
    if deadline is not None and time.monotonic() > deadline:
        raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")
    try:
        root_kind = _lstat_kind(root)
    except OSError:
        if strict:
            raise
        return
    if root_kind != "dir":                             # root 自体が symlink/非ディレクトリなら走査しない（脱出防止）
        return
    rootr = root.resolve()
    stack = [root]
    while stack:
        if deadline is not None and time.monotonic() > deadline:
            raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")
        current_dir = stack.pop()
        try:
            # `sorted(...)` はイテレータを全件消費してから返る——ソート**後**の集合だけを
            # 周期確認しても、単一ディレクトリに大量のファイルがある場合はこの列挙・ソート
            # 自体がデッドラインを超えて完了する。ソートの**前**（生の列挙段階）で
            # `_DEADLINE_CHECK_ENTRIES` 件ごとに確認し、超過していれば打ち切る。
            raw_entries: list[Path] = []
            with os.scandir(current_dir) as it:
                for i, entry in enumerate(it):
                    if (deadline is not None and i > 0
                            and i % _DEADLINE_CHECK_ENTRIES == 0
                            and time.monotonic() > deadline):
                        raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")
                    raw_entries.append(Path(entry.path))
            # 列挙完了時にも確認する——列挙件数が間引き間隔（`_DEADLINE_CHECK_ENTRIES`）未満だと
            # 上のループ内チェックが一度も発火しないまま列挙が完了しうるため。
            if deadline is not None and time.monotonic() > deadline:
                raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")
            entries = sorted(raw_entries)
        except OSError:
            if strict:
                raise
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
                continue
            if kind == "symlink":                      # symlink は file/dir とも辿らない
                continue
            if kind == "dir":
                stack.append(p)
            elif kind == "file":
                try:
                    rp = p.resolve()
                except OSError:
                    if strict:
                        raise
                    continue
                if rp.is_relative_to(rootr):             # root 外への脱出を拒否
                    yield rp, p.relative_to(root).as_posix()
        # 後処理完了時（このディレクトリの各エントリ処理を終えた時点）にも確認する——処理件数が
        # 間引き間隔未満だと上のループ内チェックが一度も発火しないまま完了しうるため
        # （次の `while stack:` 反復先頭のチェックだけに頼ると、複数の小規模ディレクトリが
        # 連続する場合の合計時間を見逃す）。
        if deadline is not None and time.monotonic() > deadline:
            raise ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")


def unique_index(items, keyfn):
    """`items` から `keyfn` 一意の index を作る。**衝突キーは fail-closed で除外**（先勝ち禁止）。

    戻り: `(index{key: item}, collisions{key: [item,...]})`。誤った解決対象を返さないための要。
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
