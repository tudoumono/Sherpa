"""言語アナライザの共通基底（正典 docs/proposals/2026-08-29-コード解析層のコンポーネント化.md §2.2・§7 裁定1）。

クラス階層は2段固定: 本基底 `Analyzer`（言語非依存の共通処理のみ）＋そこから**直接**派生する言語クラス
（`CobolAnalyzer`/`CopybookAnalyzer`/`JclAnalyzer` 等）。中間の言語別基底は作らない。方言（ベンダー差）の
概念・アナライザごとの設定は持たない——標準で解釈できない構文は**解析せず、`dropped` で記録して落とす**
（黙って誤解釈しない・黙って消さない）。

名前解決（同一 top_scope 内最近傍）・cid 組み立ては `world_graph` の共通層が担う。本基底が担うのは
「1ファイルからの定義/参照候補の抽出」まで——`collect_defs`/`extract_refs`
は候補を返すだけで、任意解決は一切行わない。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DefItem:
    """1件の定義候補（ノード化前）。`label` は docs/05-グラフ語彙.md のノードラベルのみを使う。

    `cid_key` は canonical_id の識別子部分に使う値（省略時は `name`）。修飾名（COBOL コピーブックの
    `GROUP.ITEM` のような同名衝突回避の識別子）が `name`（表示名）と異なる場合に指定する。
    `extra` はノードにそのまま足す追加プロパティ（既存ノードの型・命名は変えない・追加のみ）。
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
    """解析せず落とした構文の1件（`collect_defs`/`extract_refs` が返す）。

    docs/05 の語彙で表現できない、または現行アナライザが未対応の構文（COBOL の動的 CALL＝識別子
    呼び出し、JCL の `PROC`/`INCLUDE` 等）を検知したときに積む。共通層が `flags` へ
    `reason: "dropped_syntax"` として記録する——黙って消さない。解釈（名前解決・ノード化）は
    増やさない＝挙動不変のまま可視化だけする。
    """

    reason: str
    line: int
    snippet: str


@dataclass
class DefGroup:
    """`DefResult.extras` の1件（アナライザ拡張 A10・DDL の複数 `CREATE TABLE` 用・A8 と同型の
    限定改訂）。

    `primary`＝ファイル内の**主体以外**のトップレベル定義（例: 2件目以降の `CREATE TABLE`）。
    共通層のクロスファイル解決索引には登録される（`extract_refs` の dst になり得る）が、
    ファイルの主体（`rel_name`・Pass2 の参照元）にはならない——「1ファイル1主体」という
    既存契約自体は変えず、主体以外のトップレベル定義を追加で持てるようにするだけの拡張。

    `children`＝この `primary` に構造的に含まれる子定義（`DefResult.children` と同じ意味・
    `primary -CONTAINS-> child` を1本ずつ生成する）。
    """

    primary: DefItem
    children: list = field(default_factory=list)


@dataclass
class DefResult:
    """`Analyzer.collect_defs` の戻り値（1ファイル分）。

    `primary`＝このファイルの主体定義（例: COBOL の PROGRAM-ID・コピーブック自身・JCL の JOB 名）。
    共通層のクロスファイル解決索引に登録され、`extract_refs` が返す参照の src（起点）になる。
    構文にマッチせずファイルが主体を持たない場合は `None`（従来どおりノード化しない）。

    `children`＝主体に構造的に含まれる子定義（例: コピーブックの DataItem 項目）。共通層が
    `primary -CONTAINS-> child` のエッジを1本ずつ生成する（`primary` が `None` なら無視される）。

    `extras`＝同一ファイル内の**主体以外**のトップレベル定義（アナライザ拡張 A10・`list[DefGroup]`）。
    DDL は1ファイルに複数 `CREATE TABLE` があるのが普通なため、「1ファイル1主体」の外側に
    追加で持てるようにする——既定は空（既存アナライザの挙動は不変）。共通層は各 `extras` エントリを
    `primary` と同じ規則でノード化・索引登録する（`extract_refs` の src には**ならない**——
    参照の起点は従来どおり `primary` のみ）。

    `dropped`＝定義収集の際に解析せず落とした構文（`list[Dropped]`）。
    """

    primary: DefItem | None = None
    children: list = field(default_factory=list)
    extras: list = field(default_factory=list)
    dropped: list = field(default_factory=list)


#: `INVOKES`/`CONTAINS` 一般化（docs/05-グラフ語彙.md §2・2026-09-03裁定）に伴うエッジ属性 `via` の
#: 既知値（アナライザ基盤が管理・エッジ型は増やさない）。アナライザが未知の値を返しても黙って
#: 新値扱いにはしない——共通層（`world_graph._link`）が `unknown_via` として `flags` に記録し、
#: その `via` 属性だけを落とす（エッジ自体は張る＝構造の事実と分類ラベルは別物）。
#: `"mention"`（S2・2026-09-04-グラフのソース正典化.md §2・K3）: `DOCUMENTS` エッジの細分——
#: 辞書突合（`world_graph._mention_pass`）が資料文書とコード定義名の完全一致から静的に張る言及。
#: `INVOKES`/`CONTAINS` の via 語彙（呼び出し/継承等）とは別軸（`DOCUMENTS` 専用）だが、
#: 「型は閉じ属性で開く」流儀は同じなのでこの frozenset に同居させる。
#: `bean_class`/`mapper_type`/`mapper_namespace`/`action_class`/`config_value`/`config_key`
#: （アナライザ拡張 A6・`Config` ノードの受け皿・docs/05-グラフ語彙.md §1/§2）: 設定ファイル
#: アナライザ（本値を実際に生成するアナライザ本体は別途追加）が使う細分。共通層（`world_graph._link`）
#: の名前解決は既定のまま（同一 top_scope 内最近傍）——`config_key` のみ A9 の特例（同世代の同名
#: `Config` キー全件へ接続）を経由する。
#: `exec_sql`（アナライザ拡張 §4(d)・COBOL `EXEC SQL` 由来の `ACCESSES`→`Table`）: 名前解決は既定
#: （同一 top_scope 内最近傍）のまま——DDL（`Table` の producer）が無い world では unresolved flag に
#: 落ちるだけで新しい解決規則は要らない。
#: `exec_proc`/`include_member`（アナライザ拡張 S5a・JCL の `EXEC PROC=`/カタログド実行・
#: `INCLUDE MEMBER=` 由来の `Batch`→`Batch` INVOKES）: 名前解決は既定（同一 top_scope 内最近傍）の
#: まま——PROC の実体展開はしない（`Batch(JOB)-INVOKES->Batch(PROC)-INVOKES->Module` の2段）。
#: `cics_xctl`/`cics_link`（アナライザ拡張 S5b・COBOL `EXEC CICS XCTL/LINK PROGRAM('X')` 由来の
#: `Module`→`Module` INVOKES）: FW 固有 via（`bean_class` 等）と同格の優先順位。名前解決は既定
#: （同一 top_scope 内最近傍）のまま。
#: `mapper_sql`（アナライザ拡張 S4'・MyBatis Mapper XML の SQL 本文由来 `Module`→`Table`
#: ACCESSES。生成側アナライザは別スライスで追加）: `VIA_PRIORITY` の全順序では `exec_sql` の
#: 次点（順序＝`exec_sql` → `vba_sql` → `mapper_sql`）。
#: `vba_sql`（アナライザ拡張 波3 レーン C・VB6/VBA の SQL 文字列連結由来 `Module`→`Table`
#: ACCESSES）: `mapper_sql` と同格の FW/言語固有 via。
KNOWN_VIA = frozenset({
    "call", "extends", "implements", "field_type", "inject", "include", "import", "copy",
    "mention",
    "bean_class", "mapper_type", "mapper_namespace", "action_class", "config_value", "config_key",
    "exec_sql", "exec_proc", "include_member", "cics_xctl", "cics_link", "mapper_sql", "vba_sql",
})

#: §4(f)（アナライザ拡張・エッジ集約規則）: 同一 `(src, edge_type, dst)` に複数の参照候補が来たとき、
#: どの `via` を採用するかの優先順位（先頭ほど優先）。フレームワーク固有の via（設定ファイル由来）は
#: 汎用の via（宣言型参照等）より優先する——固有の情報を持つ方が影響調査の手がかりとして価値が高い。
#: 同順位（未収載の `via`／`via` 無し）は初出の候補を採用する。全 via の全順序は実装時の実例で
#: 確定する未決事項（docs/proposals/2026-09-05-アナライザ拡張.md §8 未決事項4）——本表は現時点の
#: 既知 via のみを対象にした暫定表。
VIA_PRIORITY = (
    "bean_class", "mapper_type", "mapper_namespace", "action_class", "config_key", "config_value",
    "cics_xctl", "cics_link",
    "call", "extends", "implements", "field_type", "inject", "include", "import", "copy", "exec_sql",
    "vba_sql", "mapper_sql", "exec_proc", "include_member",
)


def via_priority_rank(via: str | None) -> int:
    """`via` の集約優先順位（§4(f)）。小さいほど優先。`VIA_PRIORITY` 未収載／`None` は最下位
    （複数候補が並んだ場合は初出＝最初に見つかった候補を採用する現状維持の挙動になる）。"""
    try:
        return VIA_PRIORITY.index(via)
    except ValueError:
        return len(VIA_PRIORITY)


@dataclass
class RefCandidate:
    """参照候補の1件。`edge_type`/`kind` は docs/05 のクローズド語彙のみを使う。

    共通層が `kind`/`name` を同一 top_scope 内最近傍で解決し、解決できたときだけ `edge_type` の
    構造エッジを張る。曖昧/世代外/未解決は共通層が `flags` に記録し、任意解決はしない（§2.3）。

    `extra` は解決後のエッジのプロパティへ**加算的に**透過される追加属性（CODE-2・JAVA-1 残課題#4）。
    細分の分類は `extra["via"]`（`KNOWN_VIA` のみ・例: `call`/`extends`/`field_type`/`inject`）に積む
    ——`edge_type`/`kind` 自体を増やさず、エッジ属性だけで細分を表現する（docs/05 §2 一般化）。

    `extra["qualified"]`（真値・アナライザ拡張 RV2-4）: `name` を完全修飾名として扱い、共通層が
    「同一 top_scope 内の `cid_key` 完全一致→無ければ最後のセグメントの単純名で通常の最近傍
    （`flags` に `qualified_fallback` を記録）」の2段で解決する。エッジ自体のプロパティには
    残らない（共通層が解決前に取り除く・解決の指示であってエッジの事実ではないため）。

    `reverse`（アナライザ拡張 A8）: `True` のとき共通層（`world_graph._link`）はエッジを
    「解決先→このアナライザの primary」の向きで張る（`src`/`dst` が入れ替わるだけで解決規則自体は
    変えない）。既定 `False`（現行どおり primary→解決先）。
    """

    edge_type: str
    kind: str
    name: str
    line: int
    extra: dict = field(default_factory=dict)
    reverse: bool = False


@dataclass
class RefResult:
    """`Analyzer.extract_refs` の戻り値（1ファイル分）。

    `refs`＝参照候補（`list[RefCandidate]`）。`dropped`＝参照抽出の際に解析せず落とした構文
    （`list[Dropped]`・COBOL の動的 CALL・JCL の `PROC`/`INCLUDE` 等）。
    """

    refs: list = field(default_factory=list)
    dropped: list = field(default_factory=list)


class Analyzer:
    """言語アナライザの抽象基底（言語非依存）。すべての言語クラスはここから直接派生する（2段固定）。"""

    #: 表示名・レジストリキー（例 "cobol"）。
    name: str = ""
    #: 担当する拡張子（小文字・ドット付き）。
    extensions: frozenset = frozenset()
    #: 台帳・原本 API 表示用の doctype 文字列（`corpus_docs` が単一の真実源として参照する表示名）。
    doctype: str = ""
    #: 手続き型言語向け: 関数/手続きを children（修飾名 `<ファイル>.<名前>`・extra `c_kind` に
    #: definition/declaration）として返し、`via=call` の単純名参照を同一言語の children へ解決
    #: させる（共通層の2段目解決の対象にする）。既定 False＝他言語は従来どおり。
    resolves_calls_by_simple_name: bool = False

    #: 決定的な内容判定（`accepts()` オーバーライド）が拒否したとき、`corpus_docs._classify_
    #: generic_text` の通常の内容推定（`ingest.text_kind`）へ回してよいか。既定 False＝従来どおり
    #: 「登録アナライザの候補は居たが拒否された」扱いのまま資料枠（doctype=None）へ落ちる。
    #: True にすると、拒否されたファイルは他の未登録テキストと同じ内容推定パスを通り、日本語本文
    #: 主体なら資料・コード的な内容ならコードへ振り分けられる（`HtmlTemplateAnalyzer` 専用）。
    fallback_to_text_kind_when_declined: bool = False

    #: `accepts()` の内容判定に必要な head サイズ（バイト・既定4KiB＝従来どおり）。`accepts()` を
    #: オーバーライドするアナライザが既定より広い範囲を見る必要がある場合（例: `HtmlTemplateAnalyzer`
    #: の目印検出は先頭64KiB）だけ大きい値へ上書きする——読取側（`registry.resolve_lazy`・
    #: `world_graph.build_world`）はこの値に従って読む/切り出す量を決める。
    head_bytes: int = 4096

    def accepts(self, rel_path: str, head_text: str = "") -> bool:
        """このアナライザが `rel_path` を担当してよいか（拡張子は既に一致している前提・§7 裁定10）。

        拡張子だけで言語が一意に決まる場合は既定（常に真）のままでよい。複数のアナライザが
        同じ拡張子を要求する場合にだけ、決定的な内容判定（見出し構文の有無等）でオーバーライドする
        （LLM や推測は使わない）。
        """
        return True

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        """定義候補の抽出（1パス目）。名前解決・ノード化・来歴付与・語彙検証は共通層が行う。"""
        raise NotImplementedError

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        """参照候補の抽出（2パス目）。同一 top_scope 内最近傍解決・エッジ化・語彙検証・
        `dropped` の flags 記録は共通層が行う。"""
        raise NotImplementedError
