"""言語アナライザの共通基底。1ファイルから定義候補（`collect_defs`）と参照候補（`extract_refs`）を取り出す。

言語クラスは `Analyzer` から直接派生する（中間基底は作らない）。標準で解釈できない構文は解析せず
`dropped` に記録する。名前解決・cid 組み立ては `world_graph` の共通層が担い、ここは候補を返すだけ。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DefItem:
    """1件の定義候補（ノード化前）。`label` は docs/05-グラフ語彙.md のノードラベルのみ。

    `cid_key` は canonical_id の識別子部分（省略時は `name`）。`extra` はノードへ足す追加プロパティ。
    """

    label: str
    name: str
    cid_key: str | None = None
    value: str | None = None
    line: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """canonical_id に使う識別子（`cid_key` 省略時は `name`）。"""
        return self.name if self.cid_key is None else self.cid_key


@dataclass
class Dropped:
    """解析せず落とした構文の1件。共通層が `flags` へ `dropped_syntax` として記録する。"""

    reason: str
    line: int
    snippet: str


@dataclass
class DefGroup:
    """`DefResult.extras` の1件。

    `primary` はファイルの主体以外のトップレベル定義（例: 2件目以降の `CREATE TABLE`）。解決索引には登録されるが参照の起点にはならない。
    `children` はその子定義（`primary -CONTAINS-> child`）。
    """

    primary: DefItem
    children: list = field(default_factory=list)


@dataclass
class DefResult:
    """`Analyzer.collect_defs` の戻り値（1ファイル分）。

    `primary`＝ファイルの主体定義（無ければ `None`）。参照の起点になる。
    `children`＝主体に含まれる子定義（`primary -CONTAINS-> child`）。
    `extras`＝主体以外のトップレベル定義。参照の起点にはならない。
    `dropped`＝解析せず落とした構文。
    """

    primary: DefItem | None = None
    children: list = field(default_factory=list)
    extras: list = field(default_factory=list)
    dropped: list = field(default_factory=list)


# `INVOKES`/`CONTAINS`/`DOCUMENTS` エッジ属性 `via` の既知値。未知の値は共通層が `unknown_via` として `flags` に記録し、`via` だけを落とす（エッジは張る）。
# `mention` は資料とコード定義名の完全一致から張る `DOCUMENTS`。名前解決はいずれも既定（同一 top_scope 内最近傍）。
KNOWN_VIA = frozenset({
    "call", "extends", "implements", "field_type", "inject", "include", "import", "copy",
    "mention",
    "bean_class", "mapper_type", "mapper_namespace", "action_class", "config_value", "config_key",
    "exec_sql", "exec_proc", "include_member", "cics_xctl", "cics_link", "mapper_sql", "vba_sql",
})

# 同一 `(src, edge_type, dst)` に複数の `via` 候補が来たときの採用順（先頭ほど優先）。FW 固有の via を汎用の via より優先し、同順位・未収載は初出を採用する。
VIA_PRIORITY = (
    "bean_class", "mapper_type", "mapper_namespace", "action_class", "config_key", "config_value",
    "cics_xctl", "cics_link",
    "call", "extends", "implements", "field_type", "inject", "include", "import", "copy", "exec_sql",
    "vba_sql", "mapper_sql", "exec_proc", "include_member",
)


def via_priority_rank(via: str | None) -> int:
    """`via` の集約優先順位（小さいほど優先）。未収載・`None` は最下位。"""
    try:
        return VIA_PRIORITY.index(via)
    except ValueError:
        return len(VIA_PRIORITY)


@dataclass
class RefCandidate:
    """参照候補の1件。`edge_type`/`kind` は docs/05-グラフ語彙.md のクローズド語彙のみ。

    共通層が `kind`/`name` を同一 top_scope 内最近傍で解決し、解決できたときだけ `edge_type` のエッジを張る（曖昧・未解決は `flags` に記録）。
    `extra` はエッジのプロパティへ加算される追加属性。細分は `extra["via"]`（`KNOWN_VIA` のみ）。
    `extra["qualified"]`＝`name` を完全修飾名として解決する指示（エッジには残らない）。
    `reverse`＝True なら「解決先→primary」の向きで張る。
    """

    edge_type: str
    kind: str
    name: str
    line: int
    extra: dict = field(default_factory=dict)
    reverse: bool = False


@dataclass
class RefResult:
    """`Analyzer.extract_refs` の戻り値。`refs`＝参照候補、`dropped`＝解析せず落とした構文。"""

    refs: list = field(default_factory=list)
    dropped: list = field(default_factory=list)


class Analyzer:
    """言語アナライザの抽象基底。言語クラスはここから直接派生する。"""

    name: str = ""
    extensions: frozenset = frozenset()
    # 台帳・原本 API の表示用 doctype 文字列。
    doctype: str = ""
    # 手続き型言語向け: 関数/手続きを children（`<ファイル>.<名前>`・extra `c_kind`）として返し、`via=call` の単純名参照を children へ解決させる。
    resolves_calls_by_simple_name: bool = False

    # `accepts()` が拒否したファイルを `ingest.text_kind` の通常の内容推定へ回してよいか（`HtmlTemplateAnalyzer` 専用）。
    fallback_to_text_kind_when_declined: bool = False

    # `accepts()` の内容判定が読む head サイズ（バイト・既定 4KiB）。
    head_bytes: int = 4096

    # 部品ごとの版番号（1以上）。`registry.config_signature()` に `(name, version, extensions)` として畳み込まれる。
    version: int = 1

    # 拡張アナライザが上流と拡張子を意図的に共有するときだけ明示する拡張子集合。それ以外の衝突は発見時に例外にする。共有拡張子は上流の `accepts()` が拒否したときだけ拡張側へ回る。
    overrides: frozenset = frozenset()

    def accepts(self, rel_path: str, head_text: str = "") -> bool:
        """このアナライザが `rel_path` を担当してよいか（拡張子は一致済み）。

        拡張子だけで決まるなら既定（常に真）のまま。同じ拡張子を複数が要求するときだけ、決定的な内容判定で上書きする。
        """
        return True

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        """定義候補の抽出（1パス目）。名前解決・ノード化・来歴付与・語彙検証は共通層が行う。"""
        raise NotImplementedError

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        """参照候補の抽出（2パス目）。名前解決・エッジ化・`dropped` の記録は共通層が行う。"""
        raise NotImplementedError
