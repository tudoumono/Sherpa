"""言語アナライザの登録簿（拡張子→アナライザ解決の単一の真実源・§2.4・§7 裁定2/5/10）。

既知アナライザの列挙順＝**優先順**（同じ拡張子を複数のアナライザが要求したら上位が担当）。
CODE-1b（管理画面の有効/無効・並び順）が本モジュールを介して `_ANALYZERS` を差し替える until
それまでは固定の既定リストのみ。`registered_extensions()` が「コード」と見なす拡張子集合の
単一の真実源——`doc_kinds.CODE_EXT`・`corpus_docs._doctype_map()`（コード分）・
`scope._CONTENT_EXT`（コード分）・`agentic_search._READABLE_EXT`（コード分）・
`ext_api._UTF8_DECLARE_EXT`／`_DOC_CONTENT_TYPE`（コード分）はすべてこれを参照する（§2.4）。
`resolve_lazy()` は拡張子だけでなく `accepts()` の内容判定まで見て担当アナライザを確定する
（`corpus_docs.iter_world_documents` が使う・既定 `accepts` のみなら内容を読まない）。
"""
from __future__ import annotations

from pathlib import PurePosixPath

from ._base import KNOWN_VIA, VIA_PRIORITY, Analyzer  # noqa: F401  (世界層が参照する再エクスポート)
from ._base import via_priority_rank  # noqa: F401  (同上)
from .c import CAnalyzer
from .cobol import CobolAnalyzer
from .copybook import CopybookAnalyzer
from .css import CssAnalyzer
from .csharp import CSharpAnalyzer
from .html import HtmlTemplateAnalyzer
from .java import JavaAnalyzer
from .jcl import JclAnalyzer
from .js import JsAnalyzer
from .jsp import JspAnalyzer
from .properties import PropertiesAnalyzer
from .shell import ShellBatchAnalyzer
from .sql import SqlDdlAnalyzer
from .vb import VbAnalyzer
from .xml_config import XmlConfigAnalyzer
from .yaml_config import YamlConfigAnalyzer

# 優先順＝この並び順（§7 裁定2）。JavaAnalyzer は拡張子 `.java` が他アナライザと衝突しない
# ため末尾に追加（CODE-1d＝新言語1つでの手順検証・docs/proposals/2026-08-29 §4.2）。
# Properties/YamlConfig/XmlConfig（アナライザ拡張 S3b・A6/A7）・SqlDdlAnalyzer（S2・A1/A10）も
# 拡張子が他アナライザと衝突しないため末尾に追加——新規アナライザの追加自体は `config_signature()`
# の `_ANALYZERS` タプル材料が自動的に変わるため `CODE_ANALYZERS_SCHEMA_VERSION` の据え置きでよい
# （S3b 前例）。CAnalyzer（`.c`/`.h`）・CSharpAnalyzer（`.cs`）も同じ理由で末尾追加（S6/S7・A1）。
# JspAnalyzer/HtmlTemplateAnalyzer/JsAnalyzer/CssAnalyzer（波3 レーンA）・ShellBatchAnalyzer
# （波3 レーンB）・VbAnalyzer（波3 レーンC）も同じ理由（拡張子非衝突）で末尾追加。ただし `.js`/
# `.sh`/`.bash`/`.zsh`/`.bat`/`.cmd`/`.vb` は従来 `text_kind.CODE_EXT`（軽量テキスト枠）が拾って
# いた拡張子のため、本登録により `classify_document()` の判定結果（branch）が変わる——
# `CODE_ANALYZERS_SCHEMA_VERSION` を上げる理由の一つ（v9 参照）。
_ANALYZERS: tuple[Analyzer, ...] = (CobolAnalyzer(), CopybookAnalyzer(), JclAnalyzer(), JavaAnalyzer(),
                                   PropertiesAnalyzer(), YamlConfigAnalyzer(), XmlConfigAnalyzer(),
                                   SqlDdlAnalyzer(), CAnalyzer(), CSharpAnalyzer(),
                                   JspAnalyzer(), HtmlTemplateAnalyzer(), JsAnalyzer(), CssAnalyzer(),
                                   ShellBatchAnalyzer(), VbAnalyzer())

# `accepts()`/`classify_document` の分類契約版——分類結果（同じ入力に対する kind/doctype/branch の
# 判定）に影響する意味変更（例: 既定 accepts の扱いを変える・優先順の解決規則を変える）があれば
# 上げる。`config_signature()` の材料（`importance.IMPORTANCE_SCHEMA_VERSION` と同じ流儀）。
# v2: 軽量テキスト枠（`ingest.text_kind`）導入——`classify_document()` の「担当なし」経路が
# 未登録拡張子のテキストファイルを新たに code/document 判定するようになった（従来は未対応の
# まま台帳・ES に載らなかった）。登録簿自体（`_ANALYZERS`/`extensions`）は無変更のため、この
# 版を上げないと `content_sig`/ES `analyzer_config_sig` が drift を検知できず、既存 world が
# 次回 sync/reindex まで新しい分類を反映しない。
CODE_ANALYZERS_SCHEMA_VERSION = 10   # v3（2026-09-05）: _CALL/_COPY の前方語境界是正（偽参照の除去）
# v4（rv-s2-mention #5・2026-09-05）: COPY/CALL 抽出前に引用文字列の中身／行末インラインコメント
# （`*>` 以降）を除去する前処理を追加（COBOL の引用/コメント誤検知の是正）＋ `CALL "PGM"`
# （二重引用符）も INVOKES として受理するよう `_CALL` を拡張。
# v5: `NODE_LABELS` へ `Config` を追加（A6・設定ファイルアナライザの受け皿）。共通層の契約拡張
# （`RefCandidate.reverse`／qualified 名の2段解決／エッジ集約・KNOWN_VIA の Config 系 via 追加）も
# 本版に含む。
# v6（アナライザ拡張 S2・2026-09-06）: 既存 `CobolAnalyzer` の `extract_refs` へ `EXEC SQL`→
# `Table`/`ACCESSES(via=exec_sql)` 抽出を追加（同一構成のまま抽出結果が変わる変更・§6 版管理表）。
# `SqlDdlAnalyzer` の新規登録自体は上記の理由により版を上げない。`DefResult.extras`（A10・DDL の
# 複数 `CREATE TABLE` 用）の共通層契約拡張も本版に含む（既存アナライザは `extras` 既定空で無変更）。
# v7（アナライザ拡張 波2・2026-09-06）: `CAnalyzer`/`CSharpAnalyzer` の新規登録（S6/S7）＋
# 既存 `CobolAnalyzer` の `EXEC CICS XCTL/LINK` 抽出（S5b）をまとめて1回で版上げする——新規登録
# 単独では `config_signature()` が自動的に構成差分を検知するため版据え置きでもよい前例（S3b）が
# あるが、波2は既存アナライザの抽出結果が変わる変更（S5b）を同時に含むため、対象を1つずつ切り
# 分けず波全体で1回に統一する。
# v8（アナライザ拡張 S3' 残課題・2026-09-06）: 既存 `XmlConfigAnalyzer` の `collect_defs` へ
# キー単位 `Config` children（Spring `<bean>`/`<property>`/`<alias>`・MyBatis 文 id/`<resultMap>`・
# Struts `<action>`/`<constant>`）を追加し、既存 `JavaAnalyzer` の設定キー参照抽出へ
# `getBean`/`@Qualifier`/`@Named`/`@Resource(name=...)` を追加した（同一構成のまま抽出結果が
# 変わる変更・§6 版管理表）。
# v9（アナライザ拡張 波3 統合・2026-09-06）: `JspAnalyzer`/`HtmlTemplateAnalyzer`/`JsAnalyzer`/
# `CssAnalyzer`（レーンA・画面テンプレート/JS/CSS）・`ShellBatchAnalyzer`（レーンB・シェル/バッチ）・
# `VbAnalyzer`（レーンC・VB.NET/VB6/VBA/VBScript）を新規登録し、`text_kind.CODE_EXT` から
# `.js`/`.sh`/`.bash`/`.zsh`/`.bat`/`.cmd`/`.vb`（専用アナライザに移管した拡張子）を外した——
# 新規アナライザの追加自体は登録簿の構成差分として自動検知されるが、これらの拡張子が軽量テキスト
# 枠から専用アナライザ判定へ切り替わる `classify_document()` の分類契約変更（v2 と同種）を含むため
# 明示的に版を上げる。既存 `JavaAnalyzer` の URL キー定義側（`@RequestMapping`/`@GetMapping`等→
# `Config` children・`key_kind="url"`）追加、`_base.KNOWN_VIA`/`VIA_PRIORITY` への `vba_sql` 追加
# （VbAnalyzer の SQL 文字列参照の受け皿）も本版に含む。
# v10（アナライザ拡張 波3 統合の是正）: `Config` キーの `cid_key` へ `key_kind` を含めて
# 名前空間を分離（同名でも種別が違えば別ノード＝既存 cid と不一致）、単純名解決（`simple_name_defs`）
# を言語（`c_kind`）内に限定（C/C# 間の誤接続を止める＝既存の跨言語一致が unresolved に変わり得る）、
# `JavaAnalyzer` の `@RequestMapping`（クラス）×`@GetMapping`等（メソッド）の URL キーを配列直積で
# 展開（複数値の組み合わせ抜けを解消）——いずれも登録簿の構成（`_ANALYZERS`/`extensions`）自体は
# 無変更のまま抽出結果・cid が変わるため明示的に版を上げる。

# docs/05-グラフ語彙.md のクローズド語彙（アナライザが返してよいラベル/エッジ型の上限・§7 裁定5）。
# K13（2026-09-04-グラフのソース正典化.md §4）確定リスト＋A6（`Config` 追加）。刈った型は復活させない
# （`ingest.model.NODE_LABELS`/`EDGE_TYPES` と同じ集合＝`world_neo4j.WORLD_EDGE_TYPES` が
# `CORRESPONDS_TO` を別途加算する）。
NODE_LABELS = frozenset({"Module", "Copybook", "Batch", "DataItem", "Table", "Document", "Config"})
EDGE_TYPES = frozenset({"COPIES", "CONTAINS", "INVOKES", "ACCESSES", "DOCUMENTS"})


def known_analyzers() -> tuple[Analyzer, ...]:
    """既知アナライザの一覧（優先順のまま）。"""
    return _ANALYZERS


def registered_extensions() -> frozenset:
    """全アナライザの担当拡張子の和集合（拡張子集合の単一の真実源・§2.4）。"""
    exts: set = set()
    for a in _ANALYZERS:
        exts |= set(a.extensions)
    return frozenset(exts)


def config_signature() -> tuple:
    """現在の有効構成の署名（登録順のアナライザ名＋各 extensions＋分類契約版）。

    world 署名（`ingest/worker.py::_sig`）・ES 設定署名（`es_index.needs_reindex`）の材料に使う——
    新規アナライザの追加・CODE-1b（管理画面）による有効/無効・並び替えのいずれかで構成が変われば
    この署名も変わり、標準の「署名不一致→再構築」経路で台帳・Neo4j・ES の `branch`（`corpus_docs.
    classify_document` 確定値）が自動的に作り直される（専用の移行機構を持たない・`importance.
    IMPORTANCE_SCHEMA_VERSION` と同じ流儀）。呼び出しごとに `_ANALYZERS` から都度計算する
    （`registered_extensions()` と同じくキャッシュしない——CODE-1b でプロセス内に構成が動的に
    変わっても次の呼び出しから追随する）。
    """
    return (CODE_ANALYZERS_SCHEMA_VERSION,
            tuple((a.name, tuple(sorted(a.extensions))) for a in _ANALYZERS))


def candidates(rel_path: str) -> tuple[Analyzer, ...]:
    """`rel_path` の拡張子を担当し得るアナライザ（優先順）。内容判定（`accepts`）は行わない。"""
    ext = _ext(rel_path)
    if not ext:
        return ()
    return tuple(a for a in _ANALYZERS if ext in a.extensions)


def resolve(rel_path: str, head_text: str = "") -> Analyzer | None:
    """`rel_path` の担当アナライザ（優先順で `accepts()` を通った最初のもの・§7 裁定2/10）。

    どのアナライザも通らなければ `None`（資料の枠へ倒す——実際に資料として扱われるのは既存の
    資料種別に該当する場合のみ・該当しなければ未対応＝§7 裁定10）。
    """
    for a in candidates(rel_path):
        if a.accepts(rel_path, head_text):
            return a
    return None


def resolve_lazy(rel_path: str, read_head) -> Analyzer | None:
    """`resolve()` の遅延読み取り版。`accepts()` を上書きしている候補があるときだけ `read_head()` を呼ぶ。

    候補全員が既定の `accepts`（常に真）のままなら先頭候補がそのまま確定し、`read_head` は
    一度も呼ばれない（内容を読まない＝列挙コストを増やさない・§7 裁定10）。`read_head` は
    実際に必要になるまでファイルを開かない callable として呼び出し側が渡す（例: 先頭数 KB を
    読むクロージャ）——`size`（キーワード専用で呼ぶ・省略時の既定は呼び出し側のクロージャに委ねる）
    を受け取れる必要がある。読む量は `accepts()` を上書きしている候補のうち最大の `head_bytes`
    （`Analyzer` 既定4KiB・`HtmlTemplateAnalyzer` は64KiB）に従う——大きい head を要求する候補が
    無ければ従来どおりの量のまま。
    """
    cands = candidates(rel_path)
    if not cands:
        return None
    overriding = [a for a in cands if _overrides_accepts(a)]
    if not overriding:
        return cands[0]
    needed = max(getattr(a, "head_bytes", 4096) for a in overriding)
    head_text = read_head(size=needed)
    for a in cands:
        if a.accepts(rel_path, head_text):
            return a
    return None


def _overrides_accepts(a: Analyzer) -> bool:
    """`a` が基底の既定 `accepts`（常に真）をオーバーライドしているか。"""
    return type(a).accepts is not Analyzer.accepts


def _ext(rel_path: str) -> str:
    """`rel_path` の拡張子（`Path.suffix` と同じ規約・ドットのみのファイル名は拡張子なし扱い）。"""
    return PurePosixPath(rel_path).suffix.lower()
