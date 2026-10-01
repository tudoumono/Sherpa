"""調査台帳の記録（`investigation_records`）を人が読める Markdown へ整形する（COD-18 ②「調査台帳を
回答ごとに残す」提案書・⑤「調査の途中で台帳を確かめ、目的や観点を見直す」利用者2026-10-01指示）。

入力は `investigation_ledger.py` の正規形（manifest/items/coverage/reviews）のみ——資料名を新たに
持ち込まない（正規形自体がそれを持たない契約は `investigation_ledger.py` 側で担保済み）。`reviews`
（中間の見直し）だけは item/evidence と異なり本文（purpose/summary/perspectives）を持つ——これは
正典（`investigation_ledger.validate_review_entry()`）が許す唯一の例外で、人が後から読む「調査の
記録」の一部として見直しの本文を残すことが要件そのもの（本モジュールが新たに本文を作り出すわけ
ではない）。決定的（同じ入力なら常に同じ出力・id 昇順・reviews は記録順）に組み立てる。
`investigation_ledger` 以外の sherpa モジュールは import しない（表示専用の独立モジュール・
provider.py の私的な日本語定型文には依存しない）。
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
# COD-18 ⑤（利用者2026-10-01指示）: 中間の見直し（`ledger_review_put`・`investigation_ledger.
# validate_review_entry` の正規形）の判断語彙。
_REVIEW_VERDICT_LABELS = {"insufficient": "まだ足りない", "mostly_answered": "おおむね出た"}


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


def _review_refs_text(refs: list) -> str:
    """`added_items`/`removed_items`（`{"id","reason"}` の配列）を1行テキストへ整形する
    （COD-18 ⑤）。"""
    if not refs:
        return "-"
    return "; ".join(f"{_esc_cell(r.get('id'))}: {_esc_cell(r.get('reason'))}"
                     for r in refs if isinstance(r, dict))


def _review_list_text(values: list) -> str:
    """`perspectives`/`extra_perspectives`（str の配列）を1行テキストへ整形する（COD-18 ⑤）。"""
    if not values:
        return "-"
    return "、".join(_esc_cell(v) for v in values if isinstance(v, str))


def render_markdown(record: dict) -> str:
    """調査台帳の記録1件（`investigation_records` の行、または Codex ジョブの `investigation`）を
    Markdown へ整形する。`record` は `complete`/`truncated`/`manifest`/`items`/`coverage`/`reviews`
    （COD-18 ⑤・中間の見直しの配列・無ければ省略可）を持つ dict（`items`/`coverage` は id→値の
    dict）。"""
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
    # COD-18 ⑤（利用者2026-10-01指示）: 中間の見直し（目的・観点の再確認）の節。見直しごとに
    # 目的・観点・分かったこと・足した/外した項目と理由・判断・追加で調べられる観点を出す
    # （`record.get("reviews")` は `investigation_ledger.validate_review_entry()` 済みの正規形の
    # 配列・記録順）。
    reviews = record.get("reviews") or []
    lines += ["", "## 途中の見直し", ""]
    if not reviews:
        lines.append("(なし)")
    else:
        for idx, review in enumerate(reviews, start=1):
            review = review or {}
            lines += [
                f"### 見直し{idx}",
                f"- 目的: {_esc_cell(review.get('purpose')) or '-'}",
                f"- 観点: {_review_list_text(review.get('perspectives'))}",
                f"- 分かったこと: {_esc_cell(review.get('summary')) or '-'}",
                f"- 足した項目: {_review_refs_text(review.get('added_items'))}",
                f"- 外した項目: {_review_refs_text(review.get('removed_items'))}",
                f"- 判断: {_esc_cell(_label(_REVIEW_VERDICT_LABELS, review.get('verdict')))}",
                # RV是正（正規化）: `extra_perspectives` は回答末尾の定型文・「続き」の注入文と
                # 同じ `investigation_ledger.sanitize_review_text_list` を通す（3箇所で正規化の
                # 実装を持たない）。
                f"- 追加で調べられる観点: "
                f"{_il.sanitize_review_text_list(review.get('extra_perspectives')) or '-'}",
                "",
            ]
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
