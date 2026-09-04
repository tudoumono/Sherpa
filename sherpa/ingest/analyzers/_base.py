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
class DefResult:
    """`Analyzer.collect_defs` の戻り値（1ファイル分）。

    `primary`＝このファイルの主体定義（例: COBOL の PROGRAM-ID・コピーブック自身・JCL の JOB 名）。
    共通層のクロスファイル解決索引に登録され、`extract_refs` が返す参照の src（起点）になる。
    構文にマッチせずファイルが主体を持たない場合は `None`（従来どおりノード化しない）。

    `children`＝主体に構造的に含まれる子定義（例: コピーブックの DataItem 項目）。共通層が
    `primary -CONTAINS-> child` のエッジを1本ずつ生成する（`primary` が `None` なら無視される）。

    `dropped`＝定義収集の際に解析せず落とした構文（`list[Dropped]`）。
    """

    primary: DefItem | None = None
    children: list = field(default_factory=list)
    dropped: list = field(default_factory=list)


#: `INVOKES`/`CONTAINS` 一般化（docs/05-グラフ語彙.md §2・2026-09-03裁定）に伴うエッジ属性 `via` の
#: 既知値（アナライザ基盤が管理・エッジ型は増やさない）。アナライザが未知の値を返しても黙って
#: 新値扱いにはしない——共通層（`world_graph._link`）が `unknown_via` として `flags` に記録し、
#: その `via` 属性だけを落とす（エッジ自体は張る＝構造の事実と分類ラベルは別物）。
#: `"mention"`（S2・2026-09-04-グラフのソース正典化.md §2・K3）: `DOCUMENTS` エッジの細分——
#: 辞書突合（`world_graph._mention_pass`）が資料文書とコード定義名の完全一致から静的に張る言及。
#: `INVOKES`/`CONTAINS` の via 語彙（呼び出し/継承等）とは別軸（`DOCUMENTS` 専用）だが、
#: 「型は閉じ属性で開く」流儀は同じなのでこの frozenset に同居させる。
KNOWN_VIA = frozenset({
    "call", "extends", "implements", "field_type", "inject", "include", "import", "copy",
    "mention",
})


@dataclass
class RefCandidate:
    """参照候補の1件。`edge_type`/`kind` は docs/05 のクローズド語彙のみを使う。

    共通層が `kind`/`name` を同一 top_scope 内最近傍で解決し、解決できたときだけ `edge_type` の
    構造エッジを張る。曖昧/世代外/未解決は共通層が `flags` に記録し、任意解決はしない（§2.3）。

    `extra` は解決後のエッジのプロパティへ**加算的に**透過される追加属性（CODE-2・JAVA-1 残課題#4）。
    細分の分類は `extra["via"]`（`KNOWN_VIA` のみ・例: `call`/`extends`/`field_type`/`inject`）に積む
    ——`edge_type`/`kind` 自体を増やさず、エッジ属性だけで細分を表現する（docs/05 §2 一般化）。
    """

    edge_type: str
    kind: str
    name: str
    line: int
    extra: dict = field(default_factory=dict)


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
