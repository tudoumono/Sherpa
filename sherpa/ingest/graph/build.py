"""資料フォルダ（登録ディレクトリ）を `(nodes, edges, flags)` にする入口。段ごとの部品を順に呼ぶだけ。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from pathlib import PurePosixPath

from ... import scope_infer, worlds
from .. import build_link, importance, periods, substage, text_kind
from ..period_text import read_text_via
from .aggregate import finalize
from .analyze import collect_definitions, read_vb_projects
from .build_defs import add_build_definitions
from .flow import add_dataflow
from .mention import mention_pass
from .paths import resolve_path
from .periods_select import PeriodSelect, mark_period_nodes
from .refs import build_type_relations, resolve_references
from .select import build_path_suffix_index
from .state import WorldBuild


def graph_files(world_dir, world_id: str, files=None) -> list:
    """グラフの対象ファイル `[(実体のパス, 相対パス)]`。秘匿・重要度の制御ファイルを除く。"""
    entries = files if files is not None else scope_infer.safe_files(
        world_dir, also=worlds.archives_dir(world_id))
    # 秘匿ファイル（環境変数ファイル系・秘密鍵系・credentials 等）はグラフ取り込みでも読まない。
    # Pass1 は拡張子で候補を引くため、拡張子付きの秘匿名（YAML・shell 等）を別に塞ぐ。
    return [(rp, rel) for rp, rel in entries
            if not importance.is_importance_control_path(rel)
            and not text_kind.is_sensitive(PurePosixPath(rel).name, PurePosixPath(rel).suffix.lower())]


def applied_marks_sig(world_dir, world_id: str, marks: dict | None) -> str:
    """ビルドの定義を読む場所の印を当てはめた結果の値（読むファイルごとの「読む／大きさの上限で外す」）。読むファイルが無ければ空。"""
    return build_link.applied_signature(graph_files(world_dir, world_id), marks)


def _start(world_dir, world_id: str, files, resolve_config) -> WorldBuild:
    """取り込み 1 回分の作業領域を作る。対象ファイルを列挙し、秘匿・重要度の制御ファイルを除く。"""
    copy_paths = list((resolve_config or {}).get("copy_paths") or [])
    path_aliases = dict((resolve_config or {}).get("path_aliases") or {})
    name_rules = list((resolve_config or {}).get("name_rules") or [])
    files = graph_files(world_dir, world_id, files)
    return WorldBuild(world_dir=world_dir, world_id=world_id, files=files,
                      copy_paths=copy_paths, path_aliases=path_aliases, name_rules=name_rules)


def _read_periods(b: WorldBuild) -> None:
    """対象ファイルの相対パスと変更一覧から期を読み、見分けられたときだけ `b.period_select` を作る。"""
    archives = worlds.archives_dir(b.world_id)
    read_text = read_text_via(lambda rel: resolve_path(b.world_dir, rel) or resolve_path(archives, rel))
    found = periods.read_periods([rel for _rp, rel in b.files], read_text)
    if PeriodSelect.active(found):
        b.period_select = PeriodSelect(found)


def build_world(world_dir, world_id: str, *, files=None, resolve_config=None, build_marks=None):
    """資料フォルダ（登録ディレクトリ）を `(nodes, edges, flags)` にする。パス同一性＋同 top_scope 内の最近傍解決。

    骨格（Pass1/Pass2＝COPIES/CONTAINS/INVOKES/ACCESSES）と言及エッジ（Pass3）だけを決定的に構築する。
    `files`（省略可）: 呼び出し側が `scope_infer.safe_files(world_dir)` を materialize 済みなら渡す（再度歩かない）。
    `_重要度.txt` はここで除外する。
    `resolve_config`（省略可）: 資料フォルダの解決範囲の設定 `{copy_paths, path_aliases, name_rules}`（`resolve_settings` で正規化済み）。
    最近傍で決まらない COPY を `copy_paths` で、`#include` の別名を `path_aliases` で、未解決の名前を `name_rules` で解く。無ければ今までの解決だけ。
    `build_marks`（省略可）: ビルドの定義を読む場所の印 `{パス: read|skip}`（`build_marks.load`）。
    """
    from ..analyzers import require_tree_sitter
    require_tree_sitter()   # Tree-sitter の本体と文法が読めなければ、ここで取り込みを失敗させる（ファイルごとの失敗に散らさない）
    b = _start(world_dir, world_id, files, resolve_config)
    b.build_marks = build_marks
    _read_periods(b)
    read_vb_projects(b)
    # ① Pass 1: 定義収集＋ノード
    pass1 = substage.begin("graph_defs", "ソースを解析しています", total=len(b.files))
    collect_definitions(b, pass1)
    b.path_suffix_index = build_path_suffix_index(b.rel_name)   # Pass1 完了直後（`rel_name` 確定後）に 1 回だけ構築する
    build_type_relations(b)
    pass1.end()
    # ② Pass 2: 参照解決（同 top_scope 内 最近傍）＋構造エッジ → データの流れ → ビルドの定義 → 集約
    pass2 = substage.begin("graph_links", "ソース同士の関係を結んでいます", total=len(b.texts))
    resolve_references(b, pass2)
    add_dataflow(b)
    add_build_definitions(b)
    finalize(b)
    b.texts.clear()   # Pass2 完了直後にコード本文の参照を解放する（Pass3 は資料文書の本文を別経路で読み直す）
    pass2.end()
    # ③ Pass 3: 辞書突合→言及エッジ
    mention_pass(b.world_dir, b.world_id, b.defs, b.mention_aliases, b.files, b.nodes, b.edges, b.flags)
    mark_period_nodes(b)
    return list(b.nodes.values()), b.edges, b.flags
