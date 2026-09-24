"""symlink 差し替え（TOCTOU）耐性を持つファイル/ディレクトリ open の共通ヘルパ。

sherpa 内のどのモジュールも import しない（stdlib のみに依存する安全な葉ノード）。
`agentic_search.py`（world root からの本文読み取り）と `ext_api.py`（`/ext/v1/doc` の原本DL・
`sherpa.agents`/`sherpa.chat_service`/`sherpa.grep_tool` を import できない契約）の両方が、
検証（realpath/doctype 等）を通った後の実ファイル open にこの関数を使う。
"""
from __future__ import annotations

import os
from pathlib import Path


def open_file_nofollow_walk(anchor: Path, rel_parts: tuple) -> int:
    """信頼済みディレクトリ `anchor`（world root 等）を起点に、`rel_parts`（相対パスの各要素）を
    dir_fd 相対で1段ずつ `open()` し、最終ファイルの fd を返す（secRV FIX-L・2026-07-19・
    read_around の祖先 symlink TOCTOU 是正）。

    中間ディレクトリは `O_DIRECTORY|O_NOFOLLOW`、最終要素（ファイル）は `O_RDONLY|O_NOFOLLOW` で
    開く。`_safe_doc_path()` の検査（realpath 確認・symlink 拒否）から実際の読み取りまでの間に窓が
    あり、検査済みファイルの**中間ディレクトリ**が保護対象（world root 外）への symlink に
    差し替えられると、素朴な単発 `open(str(path))` は追跡してしまう（`O_NOFOLLOW` は最終パス要素
    にしか効かない＝POSIX 仕様、FIX-3 で対処した「最終要素の symlink」の一段上の穴）。本関数は
    `anchor`（呼び出し時点で world root 等として signed-off 済み）から各コンポーネントを個別に
    `O_NOFOLLOW` で辿ることで、途中のどの段が symlink に差し替えられていても `OSError`
    （`ELOOP`）で検出する。`scripts/graph_extract_ab.py::_open_dir_nofollow_walk` と同型のロジック
    （あちらは書き込み先ディレクトリの dir_fd 取得・こちらは読み取り対象ファイルの fd 取得）。

    `_safe_doc_path()` のトラバーサル/種別/封じ込め検証ロジック自体は変わらない。呼び出し元は
    `rel_parts` を、`_safe_doc_path()` が返す**resolve 済みでない lexical_rel**（rag/legacy 優先順位
    込みで解決済み・doc_id 由来）から組む（レビュー是正 FIX-N・secRV・2026-07-19・既存 symlink による
    scope/拡張子迂回＝resolve 済み戻り値から作った rel だと doc_id 経路の途中の既存 symlink を
    一度も見ずに通過してしまっていたため）。lexical パスを渡すことで、doc_id 経路上の symlink は
    本関数のどこかの段で必ず `O_NOFOLLOW` により拒否される。

    レビュー是正（FIX-O・secRV・2026-07-19・FIFO で最終 open がブロック）: 最終要素（ファイル）の
    open に `O_NONBLOCK` を追加する。最終ファイルを writer のいない FIFO に競合差し替えられると、
    素の `O_RDONLY` open は書き手が現れるまで無期限にブロックしスレッドを枯渇させうる
    （POSIX: reader-only の open は `O_NONBLOCK` が無いとブロックしうる）。`O_NONBLOCK` を付けると
    reader-only open は待たずに即座に成功する＝呼び出し元の `os.fstat` チェックが FIFO/デバイス等
    （`S_ISREG` 以外）を安全に拒否できる。通常ファイルは `O_NONBLOCK` を付けても即 open 成功する
    ため正常系は不変（read 自体も通常ファイルには無害）。

    レビュー是正（FIX-Q・secRV・2026-07-19・anchor 祖先 symlink 競合）: `anchor`（world root 等）を
    `os.open(str(anchor), O_NOFOLLOW)` で単発 open すると、`O_NOFOLLOW` は最終パス要素（`anchor`
    自身）にしか効かず、`anchor` の**祖先**（例 `/data/kb` の親である `/data`）が検査後に外部
    ツリーへの symlink へ差し替えられていても素通りしてしまう（`rel_parts` 側の中間ディレクトリを
    保護する FIX-L と同種の穴が、anchor 自身より上の階層に残っていた）。是正: anchor も `/` から
    各パス要素を `O_RDONLY|O_DIRECTORY|O_NOFOLLOW` の dir_fd 相対で1段ずつ walk する
    （`scripts/graph_extract_ab.py::_open_dir_nofollow_walk` と同型のロジック）。`anchor` の
    lexical 正規化には `os.path.abspath()`（FS アクセスなしの純粋な文字列処理）を使い、
    `os.path.resolve()`/`Path.resolve()` は使わない（symlink を追跡してしまうと、この関数が
    検出しようとしている symlink 差し替え自体を見逃すため）。

    注意: anchor パス経路上（world root の祖先）に**正規の** symlink がある展開では、この walk は
    その段で `OSError`（`ELOOP`）となり読み取りが fail-closed になる（呼び出し元は本関数の
    `OSError` を捕捉して「読み取り失敗」として処理する＝本ファイルの `read_around` 実装内、
    本関数呼び出し直後の `except OSError` 節で確認済み）。
    """
    if not rel_parts:
        raise ValueError("rel_parts が空です")
    # レビュー是正（FIX-V・secRV 7巡目・2026-07-19・lexical 正規化による検証/open 対象の分離）:
    # anchor に `..` が含まれると、`os.path.abspath()` が `symlink/..` を**字面で**潰すため、
    # `_safe_doc_path()`（symlink を辿った実体を検証）と本関数の walk（潰したパスを open）で
    # 対象が食い違いうる（例: `/srv/share/pivot/../sensitive` で `pivot` が symlink の場合、
    # 検証は辿った先・open は `/srv/share/sensitive`）。`..` 入りの生 anchor
    # （非 canonical な `SHERPA_KB_DIR`/`SHERPA_DERIVED_DIR` 設定等）は fail-closed で拒否する
    # （呼び出し元は OSError を読み取り失敗として処理する）。
    if ".." in Path(anchor).parts:
        raise OSError(f"anchor に '..' 要素が含まれています（fail-closed・FIX-V）: {anchor}")
    anchor_abs = Path(os.path.abspath(anchor))  # 純 lexical 正規化（FS アクセスなし・resolve は使わない）
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in anchor_abs.parts[1:]:   # parts[0] は os.sep 自体（ルートは既に open 済み）
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        for part in rel_parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(rel_parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        return file_fd
    finally:
        os.close(fd)
