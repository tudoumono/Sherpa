"""根拠種別の語彙と判定（純関数・I/O なし）。評価（見直し）は根拠の量ではなく、質問の型ごとに必要な種別が揃っているかで判定する。
設計: docs/design/codex.md「調査台帳と回答前の関門」
"""
from __future__ import annotations


# 根拠の種別（閉集合）。評価（見直し）は根拠の量ではなく、質問の型ごとに必要な種別が揃っているかで判定する。
# 語彙は統計（`chat-round` の `missing_codes`）・回答の告知で共通。
EVIDENCE_KINDS = ("source", "spec_doc", "definition", "log_config", "callgraph")

# 告知・プロンプトで使う平文ラベル（画面にそのまま出せる語）。
EVIDENCE_KIND_LABELS = {
    "source": "ソース", "spec_doc": "設計書", "definition": "定義",
    "log_config": "ログ・設定", "callgraph": "呼出関係"}

# 「確定」に要る根拠種別（どの調べ方でもソースだけ）。設計書・呼び出し関係・ログや設定は無いことを理由に推定へ落とさない。
LENS_REQUIRED_EVIDENCE_KINDS = {
    "qa": ("source",),
    "impact": ("source",),
    "troubleshoot": ("source",),
    "author": ("source",),
}

# 定義（DDL・copybook・データ構造の宣言）。コード層の拡張子だが実装そのものではないため `source` と分ける。設定ファイルは `log_config`。
_DEFINITION_EXT = frozenset({".sql", ".cpy", ".copybook", ".json", ".xml", ".toml"})
# ログ・設定（運用ログ・貼り付けた表・アプリの設定ファイル）。トラブルシュートの必須種別は「ソース＋ログ・設定」で、設定値を読んで症状の条件を確かめるため、設定ファイル系は `definition` ではなくこちらに置く。
_LOG_CONFIG_EXT = frozenset({
    ".log", ".csv", ".tsv",
    ".properties", ".yaml", ".yml", ".ini", ".cfg", ".conf"})
# 設計書の原本（決定的 MD の元）。派生 MD は `{rel}.md`＝原本拡張子を含む名前のため、`.md` を剥がした内側の拡張子で判定できる。
_SPEC_ORIGINAL_EXT = frozenset({".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf"})
_MD_EXT = frozenset({".md", ".markdown"})
# 素のテキスト資料。設計書が `.txt`/`.rtf` で運用される world があるため、ログ側ではなく設計書として扱う。
_SPEC_TEXT_EXT = frozenset({".txt", ".rtf"})


def evidence_kind_of_doc(doc_id) -> str | None:
    """doc_id（rel_path）から根拠種別を決める純関数（ファイル本文は読まない）。判定できないときは `None`（不足の判定に数えない）。層の近似（`layer.layer_of`）を最後の分岐に使うため、アナライザ登録簿に言語が増えれば自動で `source` 側に載る。"""
    if not isinstance(doc_id, str) or not doc_id.strip():
        return None
    from pathlib import PurePosixPath
    name = PurePosixPath(doc_id.replace("\\", "/")).name.lower()
    stem, _, ext = name.rpartition(".")
    ext = f".{ext}" if stem else ""
    if ext in _MD_EXT:
        # 派生 MD（`{原本名}.md`）は原本の拡張子で種別が決まる。素の `.md`/`.markdown` は設計書として扱う（`corpus_docs._NONCODE_DOCTYPE` と同じ）。
        inner = PurePosixPath(stem).suffix.lower()
        if inner in _LOG_CONFIG_EXT:
            return "log_config"
        if inner in _DEFINITION_EXT:
            return "definition"
        return "spec_doc"
    if ext in _SPEC_ORIGINAL_EXT or ext in _SPEC_TEXT_EXT:
        return "spec_doc"
    if ext in _LOG_CONFIG_EXT:
        return "log_config"
    if ext in _DEFINITION_EXT:
        return "definition"
    from . import layer as layer_mod  # 葉ノードのまま保つための関数内 import
    return "source" if layer_mod.layer_of(doc_id) == "code" else None


def required_evidence_kinds(lens: str) -> tuple:
    """確定に要る種別（どの調べ方でもソースだけ・未知のレンズも同じ）。"""
    return LENS_REQUIRED_EVIDENCE_KINDS.get(lens, LENS_REQUIRED_EVIDENCE_KINDS["qa"])


def missing_code_for_kind(kind: str) -> str:
    """不足種別 → `missing_codes` の閉じた語彙（本文は持たない）。"""
    return f"{'spec' if kind == 'spec_doc' else 'log' if kind == 'log_config' else kind}_missing"


def evidence_kind_labels(kinds) -> str:
    """種別の集合を平文ラベルの読み下しにする（告知・プロンプト用・順序は `EVIDENCE_KINDS` 固定）。"""
    return "・".join(EVIDENCE_KIND_LABELS[k] for k in EVIDENCE_KINDS if k in set(kinds or ()))


def demote_reason_for_missing_kinds(lacking) -> str:
    """必須種別を欠く確定を推定へ落とすときの理由文言。API・Codex 両経路の最終ゲート（`providers/base.py::_demote_claim_for_missing_kinds`・`providers/codex/provider.py`）が共有する唯一の文言源。"""
    return f"{evidence_kind_labels(lacking)}を確認できていないため確定できません"


# 網羅性の要求検知。「Xごとに Y」のような親子二段の列挙を求める依頼を検知する語彙の唯一の真実源。
# `codex_agents_md.py`・`providers/base.py`（査読プロンプトの `coverage_required`）・`chat_service.py`（クイック時の深さ案内）が同じ集合を使う。
COVERAGE_KEYWORDS = ("ごと", "すべて", "全て", "全部", "各", "それぞれ", "一覧", "漏れなく", "網羅", "全件")


def coverage_requested(question: str) -> bool:
    """質問文が親子二段の網羅（列挙の抜け漏れが起きやすい依頼）を求めているかの純粋な語彙判定。"""
    if not isinstance(question, str) or not question:
        return False
    return any(kw in question for kw in COVERAGE_KEYWORDS)
