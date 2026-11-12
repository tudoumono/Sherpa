"""取り込み 1 回分の作業領域（`WorldBuild`）と、ノード・識別子（cid）の作り方。ファイル由来はパス同一性（複製は別ノード）。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ... import scope_infer


def scope_meta(rel: str) -> dict:
    """rel_path（POSIX）→ 検索スコープのメタ（top_scope/phase/category）。導出は scope_infer に集約。"""
    return scope_infer.rel_scope_meta(rel)



def make_cid(label: str, world: str, rel: str, name: str) -> str:
    """ファイル由来＝パス同一性の canonical_id（複製は別ノード）。"""
    return f"{label.lower()}:{world}:{rel}#{name}"


def make_node(label, world, rel, name, value=None):
    return {"cid": make_cid(label, world, rel, name), "label": label, "name": name,
            "world_id": world, "path": rel, "value": value,
            "status": "active", **scope_meta(rel)}


class QualifiedIndex(dict):
    """完全修飾名の索引。`project_of`（ソース rel → 属する VB.NET プロジェクト）を持ち、同じ完全修飾名の候補が複数あるとき参照元と同じプロジェクトの候補を先にする。"""

    project_of: dict = {}


def document_cid(world_id: str, rel: str) -> str:
    """Document ノードの同一性＝パス。言及元 Document ノードを rel_path で同一ノードに収束させるための cid。"""
    return f"document:{world_id}:{rel}"



@dataclass
class WorldBuild:
    """取り込み 1 回分の作業領域。Pass 1（定義）・Pass 2（参照の解決）・データの流れ・集約・言及の各段が同じものを読み書きする。"""

    world_dir: object
    world_id: str
    files: list                # [(原本 Path, rel)]（秘匿・重要度の制御ファイルは除外済み）
    copy_paths: list           # 最近傍で決まらない COPY を試す prefix（資料フォルダの解決設定）
    path_aliases: dict         # `#include` の別名 -> prefix（資料フォルダの解決設定）
    name_rules: list           # 未解決の名前を置き換えて引き直す `[{from, to}]`（資料フォルダの解決設定・順序付き）
    defs: dict = field(default_factory=dict)             # (label, NAME) -> [rel, ...]
    name_index: dict = field(default_factory=dict)       # label -> [定義の NAME, ...]（`name_rules` が種類ごとに 1 回だけ作る）
    qualified_defs: QualifiedIndex = field(default_factory=QualifiedIndex)   # (label, cid_key) -> [(rel, 実名), ...]（cid_key が付く定義は常時登録）
    rel_name: dict = field(default_factory=dict)         # rel -> (label, NAME)  ＝ファイルの主体名
    def_qkey: dict = field(default_factory=dict)         # (rel, 子の識別子|None=主体) -> 完全修飾名（型の関係の候補が持つ）
    symbol_cids: dict = field(default_factory=dict)      # (rel, cid_key) -> 定義ノード（children）の cid  ＝`RefCandidate.source_symbol_id` の解決先
    texts: dict = field(default_factory=dict)            # rel -> (原本 Path, analyzer, 本文の sha1)
    nodes: dict = field(default_factory=dict)            # cid -> node
    edges: list = field(default_factory=list)
    link_edges: list = field(default_factory=list)       # Pass2 の解決済みエッジ（集約前のステージング）
    flags: list = field(default_factory=list)
    fw_build_ctx: dict = field(default_factory=dict)     # FW プラグイン名 -> その取り込み 1 回だけの作業領域（`uses_build_context`）
    fw_ctx: dict = field(default_factory=dict)           # rel -> ([適用する FW プラグイン], 本体の DefResult)（Pass2 でも同じ本体の定義を渡す）
    plugin_failed: dict = field(default_factory=dict)    # FW プラグイン名 -> {"files": {rel}, "from": 最初の rel, "why": 理由}
    # 言及突合の単純名エイリアス: (label, DefItem.name) -> [(rel, DefItem.key), ...]。
    # `key`（cid_key）が `name`（表示名）と異なる子定義（コピーブックの `GROUP.ITEM` 等）だけを登録する。
    mention_aliases: dict = field(default_factory=dict)
    # 手続き型言語の関数呼び出し解決専用の索引: (analyzer_name, label, 単純名) -> [(rel, cid_key, c_kind|None), ...]。
    # children は `defs` に cid_key（修飾名）でしか登録されないため、`link` が通常解決で unresolved のときだけ
    # 2 段目として参照する。索引キーに analyzer_name を含め、他言語の同名と誤接続しない。
    simple_name_defs: dict = field(default_factory=dict)
    # `Table` 参照の解決専用: NAME -> [(rel, cid_key, schema|None), ...]（同名の表が複数 schema にある場合を区別する）。
    table_defs: dict = field(default_factory=dict)
    # `via=config_key` 専用の索引: (label, key_kind, 裸キー) -> [(rel, cid_key), ...]。
    # 設定ファイルのキー child（`label=="Config"` かつ `cid_key` が `"key:"` 接頭辞）だけを登録し、primary（`defs`）候補を混ぜない。
    # `key_kind`（"property"/"bean"/"action"/"mapper"/"url"/"env"）を索引キーに含め、同じ裸キーの別種別を同一視しない。
    config_key_index: dict = field(default_factory=dict)
    global_imports: dict = field(default_factory=dict)   # (アナライザ名, 最上位フォルダ) -> [ImportItem, ...]（`global using` など世代内の同じ言語の全ファイルへ効く import）
    # VB.NET のプロジェクト設定（`.vbproj`・`Directory.Build.props`）。ソース rel -> 当てたプロジェクト。
    # 当てたプロジェクトのソースの型・手続きは、完全修飾名の索引に本当の名前（`<RootNamespace>.…`）だけで載せる（ノードの識別子は変えない）。
    vb_bound: dict = field(default_factory=dict)
    vb_projects: list = field(default_factory=list)
    vb_props: dict = field(default_factory=dict)
    vb_index: object = None
    path_suffix_index: dict = field(default_factory=dict)   # Pass1 完了直後に 1 回だけ構築する（`link` が参照する）
    item_names: set = field(default_factory=set)         # DataItem ノードの名前（データの流れの段の入力）
    type_relations: object = None                        # 型の継承・実装の読み取り専用の口（使う FW プラグインがあるときだけ）
    unresolved_by_rel: dict = field(default_factory=dict)    # rel -> 未解決の申告（ファイルの主体ノードへ保存する）
    unresolved_subject: dict = field(default_factory=dict)   # rel -> 主体ノードの cid
    io_by_rel: dict = field(default_factory=dict)        # COBOL の rel -> (Module の cid, [FileIo])（データの流れの段の入力）
    dd_by_rel: dict = field(default_factory=dict)        # JCL の rel -> [DdBinding]
    names_by_rel: dict = field(default_factory=dict)     # COBOL の rel -> (Module の cid, 手続き部に出るコピーブックの項目名 -> 最初の行)
    build_marks: dict | None = None                      # ビルドの定義を読む場所の印 `{パス: read|skip}`（`build_marks.load`）
    exec_calls_resolved: list = field(default_factory=list)   # 実行物の呼び出しのうち、宣言名で解けたもの
    exec_calls: list = field(default_factory=list)       # 実行物の呼び出しのうち、今つながらないもの（ビルドの定義の段の入力）
    period_select: object = None                         # 期を見分けられたときだけ `PeriodSelect`（候補の選び方・エッジの確かさ）。None なら今までの選び方だけ

    def __post_init__(self):
        self.qualified_defs.project_of = self.vb_bound
