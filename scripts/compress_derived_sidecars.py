#!/usr/bin/env python3
"""派生物の証跡 2 種（`{rel}.evidence.json`・`{rel}.rag_chunks.jsonl`）を gzip にし直す 1 回きりの道具。

設計: docs/design/rag.md「派生物」。

使い方:
    scripts/compress_derived_sidecars.py <data/derived/資料フォルダ> [--dry-run]

対象は派生領域の `rag/`・`ir/`・`rag.staging/`・`ir.staging/`・`_conv_cache/` の中の上の 2 種だけ（ファイル名・置き場所は変えない）。
すでに gzip のものは飛ばす。1 ファイルずつ「一時ファイルへ書いて差し替え」るので途中で止めても壊れず、再実行できる。
同じ実体へのハードリンク（変換のキャッシュと公開中）は、圧縮した実体へ張り直してリンクを保つ。途中で止めて再実行し別々に圧縮された同じ中身も、最後に 1 つの実体へリンクし直す。読めないフォルダは失敗に数え、終了コードを 1 にする。
守ること: 取り込み（sync・再変換）が動いている間は走らせない。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sherpa import json_io  # noqa: E402

SUFFIXES = (".evidence.json", ".rag_chunks.jsonl")
_SKIP_NAMES = frozenset({"archives", "archives_work"})


def _target_roots(derived: Path) -> list[Path]:
    roots = []
    for child in sorted(derived.iterdir()):
        if not child.is_dir() or child.is_symlink() or child.name in _SKIP_NAMES:
            continue
        if child.name in ("rag", "ir", "_conv_cache") or child.name in ("rag.staging", "ir.staging"):
            roots.append(child)
    return roots


def _iter_targets(derived: Path, walk_errors: list | None = None):
    def _onerror(exc: OSError) -> None:  # 読めないフォルダは黙って飛ばさず失敗に数える
        print(f"失敗: フォルダを読めません: {exc}", file=sys.stderr)
        if walk_errors is not None:
            walk_errors.append(exc)

    for root in _target_roots(derived):
        for dirpath, _dirs, files in os.walk(root, onerror=_onerror):
            for name in sorted(files):
                if name.endswith(SUFFIXES):
                    yield Path(dirpath) / name


def _relink_duplicates(derived: Path, stat: dict) -> None:
    """同じ中身の gzip の証跡（途中で止めて再実行したときに別々に圧縮された写し）を 1 つの実体へハードリンクし直す。
    gzip は決定的（同じ本文なら同じバイト列）なので、中身のハッシュが同じものは同じ証跡。リンクできなければ失敗に数えて、そのまま残す。
    """
    first: dict[tuple[int, str], Path] = {}
    for path in _iter_targets(derived):
        try:
            if path.is_symlink() or not json_io.is_gzip_file(path):
                continue
            st = path.stat()
            h = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            key = (st.st_size, h.hexdigest())
            keep = first.setdefault(key, path)
            if keep == path:
                continue
            kst = keep.stat()
            if (kst.st_dev, kst.st_ino) == (st.st_dev, st.st_ino) or kst.st_dev != st.st_dev:
                continue
            _link_into(keep, path)
            stat["relinked"] += 1
        except OSError as exc:
            stat["failed"] += 1
            print(f"失敗: {path}: {exc}", file=sys.stderr)


def _tmp_name(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _compress_one(path: Path) -> None:
    """`path` の中身を展開後と同じ本文のまま gzip にして差し替える。"""
    with json_io.open_text_maybe_gzip(path) as src, json_io.atomic_gzip_text_writer(path) as dst:
        shutil.copyfileobj(src, dst)


def _link_into(src: Path, path: Path) -> None:
    """`path` を `src` と同じ実体へ差し替える（一時の名前へリンクしてから改名）。"""
    tmp = _tmp_name(path)
    try:
        os.link(src, tmp)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def compress_tree(derived: Path, *, dry_run: bool = False) -> dict:
    """`derived` の対象を圧縮する。戻り値は件数と大きさ（バイト）の集計。
    ① 非圧縮の対象を元の実体（inode）ごとにまとめる ② 実体ごとに 1 つ圧縮し、すぐ同じ実体の残りの名前をすべて圧縮後へ差し替える
    （古い実体を先に手放すので、空きの少ないディスクでも圧縮の途中で溜まらない）③ 再実行で分かれた同じ中身をリンクし直す。
    """
    stat = {"compressed": 0, "linked": 0, "relinked": 0, "skipped_gzip": 0, "failed": 0, "before": 0, "after": 0}
    walk_errors: list = []
    groups: dict[tuple[int, int], list[Path]] = {}
    sizes: dict[tuple[int, int], int] = {}
    for path in _iter_targets(derived, walk_errors):
        try:
            if path.is_symlink():
                continue
            if json_io.is_gzip_file(path):
                stat["skipped_gzip"] += 1
                continue
            st = path.stat()
        except OSError as exc:
            stat["failed"] += 1
            print(f"失敗: {path}: {exc}", file=sys.stderr)
            continue
        key = (st.st_dev, st.st_ino)
        groups.setdefault(key, []).append(path)
        sizes[key] = st.st_size
    for key, paths in groups.items():
        if dry_run:
            stat["compressed"] += 1
            stat["before"] += sizes[key]
            continue
        head, rest = paths[0], paths[1:]
        try:
            _compress_one(head)
        except (OSError, ValueError) as exc:
            stat["failed"] += len(paths)
            print(f"失敗: {head}: {exc}", file=sys.stderr)
            continue
        stat["compressed"] += 1
        stat["before"] += sizes[key]
        stat["after"] += head.stat().st_size
        for path in rest:
            try:
                _link_into(head, path)
                stat["linked"] += 1
            except OSError:
                try:                                  # リンクできなければ、その名前だけ別に圧縮する（非圧縮を残さない）
                    _compress_one(path)
                    stat["compressed"] += 1
                except (OSError, ValueError) as exc:
                    stat["failed"] += 1
                    print(f"失敗: {path}: {exc}", file=sys.stderr)
    if not dry_run:
        _relink_duplicates(derived, stat)  # 途中で止めた後の再実行で分かれた写しを 1 つの実体へ戻す
    stat["failed"] += len(walk_errors)
    return stat

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("derived", help="派生領域のフォルダ（例: data/derived/<資料フォルダ>）")
    ap.add_argument("--dry-run", action="store_true", help="書かずに、圧縮する件数と大きさだけ出す")
    args = ap.parse_args(argv)
    derived = Path(args.derived)
    if not derived.is_dir():
        print(f"フォルダがありません: {derived}", file=sys.stderr)
        return 2
    print("注意: 取り込み（sync・再変換）が動いている間は走らせないでください。", file=sys.stderr)
    stat = compress_tree(derived, dry_run=args.dry_run)
    saved = stat["before"] - stat["after"]
    print(f"圧縮 {stat['compressed']} 件・リンクを保って差し替え {stat['linked']} 件・同じ中身をリンクし直し {stat['relinked']} 件・"
          f"gzip 済みで飛ばした {stat['skipped_gzip']} 件・失敗 {stat['failed']} 件")
    if args.dry_run:
        print(f"（dry-run）圧縮対象の合計 {stat['before']} バイト")
    else:
        print(f"大きさ {stat['before']} → {stat['after']} バイト（{saved} バイト減）")
    return 1 if stat["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
