"""調査台帳の記録（`investigation_records`）を人が読める Markdown へ整形する（COD-18 ②「調査台帳を
回答ごとに残す」提案書）。

入力は `investigation_ledger.py` の正規形（manifest/items/coverage）のみ——本文・資料名を新たに
持ち込まない（正規形自体がそれを持たない契約は `investigation_ledger.py` 側で担保済み）。決定的
（同じ入力なら常に同じ出力・id 昇順）に組み立てる。`investigation_ledger` 以外の sherpa モジュールは
import しない（表示専用の独立モジュール・provider.py の私的な日本語定型文には依存しない）。
"""
from __future__ import annotations

from . import investigation_ledger as _il

_HEADER = "# 調査の記録"

# 内部の語彙 → 人が読む表示（未知の値は元の値をそのまま出す）。元の値も括弧で残し、JSON と突き合わせられるようにする。
_QUESTION_KIND_LABELS = {
    "list": "一覧", "compare": "比較", "impact": "影響範囲", "troubleshoot": "トラブルの原因", "other": "その他"}
_STATUS_LABELS = {
    "pending": "未着手", "in_progress": "調査中",
    "source_confirmed": "ソースで確認", "spec_only": "設計書だけで確認", "conflict": "ソースと設計書が食い違う",
    "not_found_in_scope": "範囲内に見つからない", "unreadable": "読み取れない", "unavailable": "利用できない",
    "unverified": "確かめられていない"}
_EVIDENCE_KIND_LABELS = {
    "source": "ソース", "spec_doc": "設計書", "definition": "定義", "log_config": "ログ・設定", "callgraph": "呼び出し関係"}
_OWNER_LABELS = {"main": "本体", "worker": "下調べ役"}
_COVERAGE_LABELS = {
    "hit": "見つかった", "no_hits": "見つからない", "truncated": "上限で途中まで", "limit": "回数の上限",
    "timeout": "時間切れ", "unreadable": "読み取れない", "error": "失敗"}


def _label(table: dict, v) -> str:
    raw = str(v or "").strip()
    if not raw:
        return "-"
    ja = table.get(raw)
    return f"{ja}（{raw}）" if ja else raw


def _esc_cell(v) -> str:
    """Markdown テーブルのセル用エスケープ（`|` と改行をつぶす）。"""
    return str(v or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _evidence_text(evidence: list) -> str:
    if not evidence:
        return "-"
    return "; ".join(f"{_esc_cell(_label(_EVIDENCE_KIND_LABELS, e.get('kind')))}: "
                     f"{_esc_cell(e.get('path'))}:{e.get('line')}"
                     for e in evidence if isinstance(e, dict))


def render_markdown(record: dict) -> str:
    """調査台帳の記録1件（`investigation_records` の行、または Codex ジョブの `investigation`）を
    Markdown へ整形する。`record` は `complete`/`truncated`/`manifest`/`items`/`coverage` を持つ
    dict（`items`/`coverage` は id→値の dict）。"""
    manifest = record.get("manifest") or {}
    items = record.get("items") or {}
    question_kind = _label(_QUESTION_KIND_LABELS, manifest.get("question_kind"))
    lines = [_HEADER, "",
             f"- 質問の種類: {question_kind}",
             f"- 完了: {'はい' if record.get('complete') else 'いいえ'}"]
    if record.get("truncated"):
        lines.append("- 注記: 件数が多いため、一部を切り詰めて保存しています。")
    lines += ["", "## 項目", "",
             "| id | 対象 | 状態 | 必要な根拠の種類 | 根拠（資料のパス・行） | 理由 | 担当 |",
             "|---|---|---|---|---|---|---|"]
    for item_id in sorted(items):
        item = items[item_id] or {}
        checks = ", ".join(_label(_EVIDENCE_KIND_LABELS, c) for c in (item.get("required_checks") or [])) or "-"
        lines.append(
            f"| {_esc_cell(item_id)} | {_esc_cell(item.get('subject'))} | "
            f"{_esc_cell(_label(_STATUS_LABELS, item.get('status')))} | {_esc_cell(checks)} | "
            f"{_evidence_text(item.get('evidence'))} | {_esc_cell(item.get('reason')) or '-'} | "
            f"{_esc_cell(_label(_OWNER_LABELS, item.get('owner')))} |")
    lines += ["", "## 確認できなかった項目", ""]
    unconfirmed = [
        (item_id, item) for item_id, item in sorted(items.items())
        if (item or {}).get("status") in _il.UNCONFIRMED_STATUSES
        or (item or {}).get("status") in _il.NON_TERMINAL_STATUSES
    ]
    if not unconfirmed:
        lines.append("(なし)")
    else:
        for item_id, item in unconfirmed:
            subject = _esc_cell(item.get("subject")) or item_id
            reason = _esc_cell(item.get("reason")) or "-"
            lines.append(f"- {subject}（{_esc_cell(_label(_STATUS_LABELS, item.get('status')))}: {reason}）")
    coverage = record.get("coverage") or {}
    if coverage:
        lines += ["", "## 検索の結果（項目ごと・記録順）", ""]
        for item_id in sorted(coverage):
            outcomes = coverage[item_id] or []
            subject = _esc_cell((items.get(item_id) or {}).get("subject")) or _esc_cell(item_id)
            lines.append(f"- {subject}: " + "、".join(_label(_COVERAGE_LABELS, o) for o in outcomes))
    lines.append("")
    return "\n".join(lines)
