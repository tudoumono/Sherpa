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

    `cid_key` は `children` のノードの canonical_id の識別子部分（省略時は `name`）。主体（`primary`）と `extras` のノードの cid は `name` で組み、
    `cid_key` は使わない。`extra` はノードへ足す追加プロパティ。
    `qualified` は名前解決だけに使う完全修飾名（package／namespace 込み）。ノードの cid・表示名には関与しない。
    共通層は `qualified`（無ければ `cid_key`）を、完全修飾名の参照（`RefCandidate.extra["qualified"]`・`type_ref`）が引く索引へ登録する（どちらも無ければ登録しない）。
    """

    label: str
    name: str
    cid_key: str | None = None
    value: str | None = None
    line: int | None = None
    extra: dict = field(default_factory=dict)
    qualified: str | None = None

    @property
    def key(self) -> str:
        """canonical_id に使う識別子（`cid_key` 省略時は `name`）。"""
        return self.name if self.cid_key is None else self.cid_key

    @property
    def resolve_key(self) -> str | None:
        """完全修飾名の索引に登録する名前（`qualified`、無ければ `cid_key`）。"""
        return self.qualified if self.qualified is not None else self.cid_key


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

    `primary`＝ファイルの主体定義（無ければ `None`）。参照の既定の始点になる（`RefCandidate.source_symbol_id` が省略されたとき）。
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
    "call", "extends", "implements", "inject", "field_type", "include", "import", "copy",
    "mention",
    "bean_class", "mapper_type", "mapper_namespace", "action_class", "config_value", "config_key",
    "exec_sql", "exec_proc", "include_member", "cics_xctl", "cics_link", "mapper_sql", "vba_sql",
})

# 同一 `(src, edge_type, dst)` に複数の `via` 候補が来たときの採用順（先頭ほど優先）。FW 固有の via を汎用の via より優先し、同順位・未収載は初出を採用する。
VIA_PRIORITY = (
    "bean_class", "mapper_type", "mapper_namespace", "action_class", "config_key", "config_value",
    "cics_xctl", "cics_link",
    "call", "extends", "implements", "inject", "field_type", "include", "import", "copy", "exec_sql",
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
    `extra["qualified"]`＝`name` を完全修飾名として解決する指示（エッジには残らない）。完全修飾名が資料フォルダに無ければ辺は張らず未解決（`unresolved_qualifier`）。
    `extra["type_ref"]`＝`name` が型名であり、参照元ファイルの `file_context`（package／namespace・import／using）に従って解決する指示（エッジには残らない）。
    `extra["resolution_rule"]`＝FW プラグインが解決の根拠として付ける規則名（`world_graph.EDGE_RULES` の名前だけ・辺の根拠の `rule` になる・未知の名前は `unknown_rule` を `flags` に出す・エッジには残らない）。
    `extra["path_exact"]`＝`via=include` の `include_path` が相対パスの完全一致でしか解決しない指示（一致しなければ basename の最近傍へ倒さず未解決・エッジには残らない）。
    `reverse`＝True なら「解決先→始点」の向きで張る。
    `source_symbol_id`＝参照元の定義キー `(rel_path, cid_key)`（`cid_key` は定義ノードの cid を組む `DefItem.key` と同じ値）。
    始点をその定義ノードにする。`None`（省略）はファイルの主体（`rel_path` の主体定義）。`collect_defs` が `children` として返した定義だけを指せる
    （定義ノードの無い言語は常に省略）。
    """

    edge_type: str
    kind: str
    name: str
    line: int
    extra: dict = field(default_factory=dict)
    reverse: bool = False
    source_symbol_id: tuple | None = None


@dataclass
class ImportItem:
    """`FileContext.imports` の1件。

    `kind`＝`"single"`（`import a.b.C;`・型名の import）／`"wildcard"`（`import a.b.*;`・C# の `using A.B;`・VB の `Imports A.B`・`name` は修飾部のみ）／`"alias"`（`using A = a.b.C;`・`alias` が `A`）。
    `name` は完全修飾名。`static`＝`static` import／`using static`（型の名前を持ち込まないので型名の解決には使わない）。`line` は宣言の行。`scope`＝namespace ブロックの中で宣言された import の有効範囲 `(開始行, 終了行)`（`None` はファイル全体）。
    """

    kind: str
    name: str
    alias: str | None = None
    static: bool = False
    line: int = 0
    is_global: bool = False
    scope: tuple | None = None


@dataclass
class FileContext:
    """1ファイル分の名前解決の文脈（`RefResult.file_context`）。`package`＝宣言された package／namespace（無ければ `None`）、`imports`＝`ImportItem` の宣言順の一覧。

    `namespaces`＝1 ファイルに namespace が複数あるときの `(開始行, 終了行, 名前)` の一覧（型の本体の範囲）。参照の行を含む範囲の名前がその参照の namespace
    （`package_at`）。範囲に入らない行は `package`。
    """

    package: str | None = None
    imports: list = field(default_factory=list)
    namespaces: list = field(default_factory=list)

    def at(self, line: int) -> "FileContext":
        """`line` の参照から見える文脈（その行の package／namespace と、有効範囲に `line` を含む import）。"""
        visible = [i for i in self.imports if i.scope is None or i.scope[0] <= line <= i.scope[1]]
        return FileContext(package=self.package_at(line), imports=visible)

    def package_at(self, line: int) -> str | None:
        """`line` の参照の package／namespace（最も狭い範囲・無ければ `package`）。"""
        best = None
        for start, end, name in self.namespaces:
            if start <= line <= end and (best is None or end - start < best[1] - best[0]):
                best = (start, end, name)
        return self.package if best is None else best[2]


@dataclass
class RefResult:
    """`Analyzer.extract_refs` の戻り値。`refs`＝参照候補、`dropped`＝解析せず落とした構文。

    `file_context`＝ファイル単位の解析の文脈（共通層が Pass 2 の解決器へ渡す）。`requires_file_context` を持つアナライザは常に返す。
    """

    refs: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    file_context: FileContext | None = None


def body_end_line(sanitized: str, decl_end: int, scan_limit: int = 4000) -> int:
    """型宣言の本体 `{ ... }` の閉じ括弧の行（1 始まり）を返す。

    `sanitized` はコメント・文字列を空白化した本文。`decl_end` 以降 `scan_limit` 文字の内で最初の `{` を本体の開きとし、対応する `}` の行を返す。
    開きの前に `;` がある（本体の無い宣言）・開きが見つからない場合は宣言行、閉じない場合は最終行を返す。
    """
    window_end = min(len(sanitized), decl_end + scan_limit)
    brace = sanitized.find("{", decl_end, window_end)
    semi = sanitized.find(";", decl_end, window_end)
    if brace == -1 or (semi != -1 and semi < brace):
        return sanitized.count("\n", 0, decl_end) + 1
    depth = 0
    for i in range(brace, len(sanitized)):
        ch = sanitized[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return sanitized.count("\n", 0, i) + 1
    return sanitized.count("\n") + 1


class Analyzer:
    """言語アナライザの抽象基底。言語クラスはここから直接派生する。"""

    name: str = ""
    extensions: frozenset = frozenset()
    # 台帳・原本 API の表示用 doctype 文字列。
    doctype: str = ""
    # True のアナライザは、名前空間が親へさかのぼって見える言語（C#・VB.NET）。型名の解決で、参照元の namespace の外側（親→グローバル）も順に探す。
    resolves_parent_namespaces: bool = False

    # True のアナライザは `extract_refs` が常に `file_context` を返す（欠けたら共通層が `file_context_missing` を `flags` へ申告する）。
    requires_file_context: bool = False

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

    def config_kind(self, text: str, rel_path: str) -> str | None:
        """設定ファイルの種別（XML のルート要素・名前空間など。FW プラグインの適用条件 `FwPlugin.config_kinds` が照合する）。判定しないアナライザは `None`。"""
        return None

    def global_imports(self, text: str, rel_path: str) -> list:
        """同じ最上位フォルダ（世代）の全ファイルへ効く import（C# の `global using`）の `ImportItem` の一覧。共通層が Pass 1 で集め、同じ世代の各ファイルの `file_context.imports` へ足す。"""
        return []

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        """定義候補の抽出（1パス目）。名前解決・ノード化・来歴付与・語彙検証は共通層が行う。"""
        raise NotImplementedError

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        """参照候補の抽出（2パス目）。名前解決・エッジ化・`dropped` の記録は共通層が行う。"""
        raise NotImplementedError


@dataclass(frozen=True)
class TypeCandidate:
    """型の定義の候補 1 件。`path`＝所属ファイル・`name`＝定義の識別子（canonical_id を組む名前）・`qualified`＝完全修飾名（`RefCandidate.name` に `extra["qualified"]` を付けると、この定義へ一意に解決される）。

    `exact`＝この候補を指した参照が他の候補と区別できていたか（`TypeLookup.subtypes` の要素で、継承・実装の宣言自体の解決が曖昧だったときは False）。
    """

    path: str
    name: str
    qualified: str
    exact: bool = True


@dataclass(frozen=True)
class TypeLookup:
    """`TypeRelations.subtypes` の結果。`status`＝`resolved`（型が 1 つに決まった）／`ambiguous`（複数の候補）／`unresolved`／`cross_scope`。

    `targets`＝問い合わせた型名の定義の候補（`ambiguous` は複数のまま）。`subtypes`＝`targets` のどれかを継承・実装する型の候補の列（パス・名前の昇順・複数あれば複数のまま）。
    """

    status: str
    targets: tuple = ()
    subtypes: tuple = ()


class TypeRelations:
    """共通の層が引く「型の継承・実装の関係」の読み取り専用の口（`FwPlugin.extract_refs` の `types`）。

    同じ資料フォルダ・同じ世代（最上位フォルダ）の中だけを引き、型名の解決は共通層の規則（package／namespace・import／using・最近傍）と同じ。
    プラグインは名前を自分で解決しない。定義の収集（1 パス目）が終わった後でないと引けないため、`FwPlugin.uses_type_relations = True` のプラグインにだけ 2 パス目で渡す。
    """

    def subtypes(self, type_name: str, from_rel: str, file_context=None, line: int | None = None, *,
                 kind: str = "Module") -> TypeLookup:
        """`from_rel` のファイルから見た型名 `type_name`（`file_context`・`line` はその参照の文脈）を継承・実装する型の候補を返す。"""
        raise RuntimeError("型の関係は使えません（FwPlugin.uses_type_relations = True を宣言したプラグインの 2 パス目でだけ使えます）")


@dataclass
class PluginAmbiguity:
    """プラグインが「複数の候補から 1 つに決まらなかった」ことを申告する 1 件（任意に選ばない）。

    共通層が取り込みの記録（`flags`）と未解決の申告（`unresolved`・`reason=ambiguous`・`candidates`＝候補の数・`candidate_paths`＝候補のパス）に載せる。辺は張らない。
    `kind`・`name`・`line`・`via` は決めたかった参照のもの、`source_symbol_id` は `RefCandidate` と同じ（省略＝ファイルの主体）。
    `why`＝決められなかった理由の短い名前（任意・あれば `flags` と未解決の申告に `why` として載る）。
    """

    kind: str
    name: str
    line: int
    candidates: list = field(default_factory=list)
    via: str | None = None
    source_symbol_id: tuple | None = None
    why: str = ""


@dataclass
class PluginDefs:
    """`FwPlugin.collect_defs` の戻り値。本体の `DefResult` へ足す定義だけ（`children`＝主体の子・`extras`＝主体以外のトップレベル）と `dropped`。"""

    children: list = field(default_factory=list)
    extras: list = field(default_factory=list)
    dropped: list = field(default_factory=list)


@dataclass
class PluginRefs:
    """`FwPlugin.extract_refs` の戻り値。本体の `RefResult` へ足す参照候補・決まらなかった申告（`PluginAmbiguity`）・`dropped`。"""

    refs: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    ambiguous: list = field(default_factory=list)


class FwPlugin:
    """FW プラグインの基底（言語アナライザの結果へ FW 固有の定義・参照を足す部品）。

    実装は 3 層: ①構文を拾う（本体のアナライザ）②名前・型の解決（共通層・`world_graph`）③FW の意味（プラグイン）。プラグインは名前を自分で解決せず、参照の候補
    （名前・種別・via・行・`type_ref` などの解決の指示）を返すだけ。型の継承・実装の関係が要るときは `types`（`TypeRelations`）を引き、候補が複数なら複数のまま扱う
    （1 つに決められなければ `PluginRefs.ambiguous` で申告し、任意に選ばない）。

    登録名 `"<prefix>:<fw>"`・`languages`（対象のアナライザ名）・適用条件（`languages` ＋ `config_kinds`）・`version`・`order` を持つ。
    入力は本体が返した `DefResult`／`RefResult`（コピー）とファイルの本文・パス。出力は追加の定義・参照と `Dropped` だけで、本体の出力を消せず、
    他のプラグインの出力も見ない。グラフへは直接書かない。契約: docs/21-拡張の契約.md §3a。
    """

    name: str = ""
    # 対象のアナライザ名（`Analyzer.name`）。
    languages: frozenset = frozenset()
    # 空＝設定の種別で絞らない。非空なら対象アナライザの `config_kind()` がこの集合に入るファイルにだけ適用する。
    config_kinds: frozenset = frozenset()
    version: int = 1
    # 小さいほど先に適用する（同値は登録名の昇順）。
    order: int = 100
    # True のプラグインは `extract_refs` で `types`（型の継承・実装の関係）を引ける。宣言があると共通層が 2 パス目の前に全ファイルの継承・実装の宣言を集める追加の段を走らせる。
    uses_type_relations: bool = False
    # 空でなければ `config_signature()` のこのプラグインのタプルの末尾に足す署名の材料（宣言的なルールファイルの内容のハッシュ・ルール ID と版）。
    signature_extra: tuple = ()
    # True のプラグインは `collect_defs`／`extract_refs` が `ctx`（取り込み 1 回ごとに作られる dict・そのプラグイン専用）を受け取り、1 パス目の事実を 2 パス目へ渡せる。
    # 共有のインスタンスに資料フォルダの事実を持たせない（並行・連続の取り込みで混ざる）ための口。
    uses_build_context: bool = False

    def collect_defs(self, text: str, rel_path: str, base: DefResult) -> PluginDefs:
        """追加の定義候補（1パス目・型の関係はまだ引けない）。"""
        return PluginDefs()

    def extract_refs(self, text: str, rel_path: str, base_defs: DefResult, base_refs: RefResult,
                     types: TypeRelations) -> PluginRefs:
        """追加の参照候補（2パス目）。`base_defs` は本体の定義（プラグインの追加分を含まない）。"""
        return PluginRefs()
