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

from ._base import KNOWN_VIA, Analyzer  # noqa: F401  (KNOWN_VIA は世界層が参照する再エクスポート)
from .cobol import CobolAnalyzer
from .copybook import CopybookAnalyzer
from .java import JavaAnalyzer
from .jcl import JclAnalyzer

# 優先順＝この並び順（§7 裁定2）。JavaAnalyzer は拡張子 `.java` が他アナライザと衝突しない
# ため末尾に追加（CODE-1d＝新言語1つでの手順検証・docs/proposals/2026-08-29 §4.2）。
_ANALYZERS: tuple[Analyzer, ...] = (CobolAnalyzer(), CopybookAnalyzer(), JclAnalyzer(), JavaAnalyzer())

# `accepts()`/`classify_document` の分類契約版——分類結果（同じ入力に対する kind/doctype/branch の
# 判定）に影響する意味変更（例: 既定 accepts の扱いを変える・優先順の解決規則を変える）があれば
# 上げる。`config_signature()` の材料（`importance.IMPORTANCE_SCHEMA_VERSION` と同じ流儀）。
# v2: 軽量テキスト枠（`ingest.text_kind`）導入——`classify_document()` の「担当なし」経路が
# 未登録拡張子のテキストファイルを新たに code/document 判定するようになった（従来は未対応の
# まま台帳・ES に載らなかった）。登録簿自体（`_ANALYZERS`/`extensions`）は無変更のため、この
# 版を上げないと `content_sig`/ES `analyzer_config_sig` が drift を検知できず、既存 world が
# 次回 sync/reindex まで新しい分類を反映しない。
CODE_ANALYZERS_SCHEMA_VERSION = 3   # v3（2026-09-05）: _CALL/_COPY の前方語境界是正（偽参照の除去）

# docs/05-グラフ語彙.md のクローズド語彙（アナライザが返してよいラベル/エッジ型の上限・§7 裁定5）。
# K13（2026-09-04-グラフのソース正典化.md §4）確定リスト。刈った型は復活させない
# （`ingest.model.NODE_LABELS`/`EDGE_TYPES` と同じ集合＝`world_neo4j.WORLD_EDGE_TYPES` が
# `CORRESPONDS_TO` を別途加算する）。
NODE_LABELS = frozenset({"Module", "Copybook", "Batch", "DataItem", "Table", "Document"})
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
    実際に必要になるまでファイルを開かない zero-arg callable として呼び出し側が渡す
    （例: 先頭数 KB を読むクロージャ）。
    """
    cands = candidates(rel_path)
    if not cands:
        return None
    if not any(_overrides_accepts(a) for a in cands):
        return cands[0]
    head_text = read_head()
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
