"""fixtures/corpus/v1 の実ファイル走査から list_docs 系の期待値を算出する共有オラクル（フェーズ7 S1）。

test_agentic_search.py / test_mcp_server.py の list_docs テストが依存していた固定カウント
（count==10/23/7・name_pattern の完全一致集合）は fixtures 編集（第2テーマ追加等・フェーズ7 S2）の
たびに壊れる「golden」だった。本モジュールは pathlib で実際に fixtures/corpus/v1 を走査して
期待件数/期待集合を算出し、fixtures が変わっても自動追随させる（fixtures を直接見に行くだけで
`sherpa.doc_ledger.documents_for` 等は呼ばない＝独立したオラクル）。

「数える対象」は `sherpa.corpus_docs` の `_DOCTYPE`（台帳が対象にする拡張子＝ソース/設計書/テキスト・
唯一の真実源）をそのまま再利用し、対象拡張子の二重管理を避ける。fixtures/corpus/v1 には
Office/画像文書が無いため、その枝（変換要否の判定）は対象外＝将来 Office fixtures を足す場合は
このヘルパも合わせて拡張が必要。
"""
from __future__ import annotations

import fnmatch
import pathlib

from sherpa.corpus_docs import _DOCTYPE   # 拡張子→doctype（台帳の唯一の真実源を再利用）

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS_V1 = ROOT / "fixtures" / "corpus" / "v1"


def rel_paths_under(prefix: str = "") -> set[str]:
    """`prefix` 配下の「文書」rel_path 集合（台帳が数える拡張子のみ・symlink は辿らない）。"""
    base = (CORPUS_V1 / prefix) if prefix else CORPUS_V1
    if not base.is_dir() or base.is_symlink():
        return set()
    out = set()
    for p in base.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        if p.suffix.lower() not in _DOCTYPE:
            continue
        out.add(p.relative_to(CORPUS_V1).as_posix())
    return out


def count_under(prefix: str = "") -> int:
    """`prefix` 配下の文書件数（`rel_paths_under` と同じ規約）。"""
    return len(rel_paths_under(prefix))


def rel_paths_matching(pattern: str) -> set[str]:
    """list_docs の name_pattern（rel_path 全体への部分一致・小文字化）と同じ規約で絞った rel_path 集合。"""
    pat = pattern.lower()
    return {rp for rp in rel_paths_under() if pat in rp.lower()}


def _match_glob_segments(pat_segs: list[str], rel_segs: list[str]) -> bool:
    """glob_search と同じ規約（`*`/`?`/`[seq]` は1セグメント内のみ・`**` だけが複数セグメントを
    跨ぐ）で判定する独立実装（`sherpa.ingest.importance._match_segment_glob` は呼ばない——実装の
    バグをそのまま追認するオラクルにしないため、素朴な再帰で書く。テスト用の小さい入力にしか
    使わないため DP 化はしない）。
    """
    if not pat_segs:
        return not rel_segs
    head, rest = pat_segs[0], pat_segs[1:]
    if head == "**":
        if _match_glob_segments(rest, rel_segs):
            return True
        return bool(rel_segs) and _match_glob_segments(pat_segs, rel_segs[1:])
    if not rel_segs:
        return False
    return fnmatch.fnmatchcase(rel_segs[0], head) and _match_glob_segments(rest, rel_segs[1:])


def rel_paths_glob(pattern: str) -> set[str]:
    """glob_search と同じ規約（大文字小文字無視・スラッシュ無しはどの階層のファイル名にも一致
    ＝`**/` 前置・スラッシュありは world ルートからの絞り込み）で fixtures/corpus/v1 を独立に
    走査した期待集合。"""
    normalized = pattern if "/" in pattern else f"**/{pattern}"
    pat_segs = normalized.lower().split("/")
    return {rp for rp in rel_paths_under() if _match_glob_segments(pat_segs, rp.lower().split("/"))}
