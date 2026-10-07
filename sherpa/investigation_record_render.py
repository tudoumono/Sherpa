"""調査台帳の記録（`investigation_records`）を人が読める Markdown へ整形する。

入力は `investigation_ledger.py` の正規形（manifest/items/coverage/reviews）のみ。`reviews`（中間の見直し）だけは
本文（purpose/summary/perspectives）を持つ。決定的（同じ入力なら同じ出力・id 昇順・reviews は記録順）。
`investigation_ledger` 以外の sherpa モジュールは import しない。
設計: docs/design/codex.md「調査の記録のダウンロード」
"""
from __future__ import annotations

from . import investigation_ledger as _il

_HEADER = "# 調査の記録"

# 内部の語彙 → 人が読む表示（未知の値は元の値のまま。JSON と突き合わせられるよう元の値も括弧で残す）
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
_ROLE_LABELS = {"parent": "本体", "child": "下調べ役", "undetermined": "判定不能"}
_CALL_STATUS_LABELS = {"ok": "成功", "truncated": "上限で途中まで", "error": "失敗"}
_COVERAGE_LABELS = {
    "hit": "見つかった", "no_hits": "見つからない", "truncated": "上限で途中まで", "limit": "回数の上限",
    "timeout": "時間切れ", "unreadable": "読み取れない", "error": "失敗"}
# 中間の見直しの判断語彙
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
    """`added_items`/`removed_items`（`{"id","reason"}` の配列）を1行テキストへ整形する。"""
    if not refs:
        return "-"
    return "; ".join(f"{_esc_cell(r.get('id'))}: {_esc_cell(r.get('reason'))}"
                     for r in refs if isinstance(r, dict))


def _review_list_text(values: list) -> str:
    """`perspectives`/`extra_perspectives`（str の配列）を1行テキストへ整形する。"""
    if not values:
        return "-"
    return "、".join(_esc_cell(v) for v in values if isinstance(v, str))


def describe_dropped(dropped: dict | None) -> str:
    """保存時に落としたものの内訳（`store.investigation_records.trim_record` の `dropped`）を利用者向けの 1 文にする（落としたものが無ければ空文字）。"""
    parts = []
    for key, label, unit in (("items", "調べた項目", "件"), ("reviews", "途中の見直し", "件"),
                             ("coverage", "検索の結果の記録", "項目分"),
                             ("coverage_detail", "検索語・読んだ範囲の記録", "項目分"),
                             ("found_docs", "見つかった資料の記録", "件"), ("calls", "調べた経路の記録", "件")):
        n = (dropped or {}).get(key)
        if isinstance(n, int) and n > 0:
            parts.append(f"{label} {n} {unit}")
    if (dropped or {}).get("manifest"):
        parts.append("調査の目録の詳細")
    if not parts:
        return ""
    return "調査の記録が大きすぎたため、" + "・".join(parts) + "を保存していません。"


_VIA_LABELS = {"keyword": "言葉の一致", "vector": "ベクトル", "keyword_only_search": "言葉の一致だけで検索"}
_MODE_LABELS = {"hybrid": "言葉の一致＋ベクトル", "keyword": "言葉の一致だけ", "vector": "ベクトルだけ"}


def _hit_rank_note(d: dict) -> str:
    """検索の当たりの順位・当たり方・点数を「（#3・ベクトル・0.8123）」の形で資料名に添える（無ければ空）。"""
    parts = []
    if isinstance(d.get("rank"), int) and not isinstance(d.get("rank"), bool):
        parts.append(f"#{d['rank']}")
    if d.get("via") in _VIA_LABELS:
        parts.append(_VIA_LABELS[d["via"]])
    if isinstance(d.get("score"), (int, float)) and not isinstance(d.get("score"), bool):
        parts.append(f"{d['score']:.4f}")
    return f"（{'・'.join(parts)}）" if parts else ""


def _calls_section(calls) -> list[str]:
    """調べた経路（全部の道具の呼び出し）と見つかった資料（検索で見つかった資料）の節。記録が無ければ空（今までのダウンロードは変わらない）。"""
    if not isinstance(calls, dict):
        return []
    route = [r for r in (calls.get("route") or []) if isinstance(r, dict)]
    found = [r for r in (calls.get("found") or []) if isinstance(r, dict)]
    head = f"道具の呼び出し {calls.get('calls') or 0} 回"
    if calls.get("missing"):
        head += f"（記録の欠け {calls['missing']} 行）"
    lines = ["", "## 調べた経路", "", head, ""]
    if route:
        lines += ["| 担当 | 道具 | 検索語 | 件数 | 結果 | 時間(ms) | 項目 | 資料と範囲 |", "|---|---|---|---|---|---|---|---|"]
    for r in route:
        docs = "; ".join(_esc_cell(d.get("doc")) + (f" {_esc_cell(d.get('range'))}" if d.get("range") else "")
                         + _hit_rank_note(d) for d in (r.get("docs") or []) if isinstance(d, dict))
        if r.get("docs_omitted"):
            docs += f"（ほか {r['docs_omitted']} 件は上限で省略）"
        if r.get("docs_hidden"):
            docs += f"（名前を出せない資料 {r['docs_hidden']} 件）"
        if r.get("mode") in _MODE_LABELS:
            docs = f"検索の形: {_MODE_LABELS[r['mode']]}。 " + docs
        status = _label(_CALL_STATUS_LABELS, r.get("status")) + (f"・{_esc_cell(r['error'])}" if r.get("error") else "")
        lines.append(f"| {_esc_cell(_label(_ROLE_LABELS, r.get('role')))} | {_esc_cell(r.get('tool'))} | "
                     f"{_esc_cell(r.get('query') or r.get('range')) or '-'} | "
                     f"{r['count'] if isinstance(r.get('count'), int) else '-'} | {status} | "
                     f"{r['ms'] if isinstance(r.get('ms'), int) else '-'} | {_esc_cell(r.get('item')) or '-'} | {docs or '-'} |")
    if calls.get("route_omitted"):
        lines.append(f"\nほか {calls['route_omitted']} 回の呼び出しは、上限のため記録していません。")
    lines += ["", "## 見つかった資料（検索の結果）", ""]
    if not found:
        lines.append("(なし)")
    for r in found:
        extra = []
        if r.get("lines"):
            extra.append("行: " + "、".join(str(x) for x in r["lines"]))
        if r.get("queries"):
            extra.append("検索語: " + "、".join(_esc_cell(q) for q in r["queries"]))
        extra.append("Codex の読み取り記録: " + ("あり" if r.get("opened")
                                                 else "確かめられません（記録が欠けています）" if calls.get("opened_unknown")
                                                 else "なし"))
        lines.append(f"- {_esc_cell(r.get('doc'))}（{' / '.join(extra)}）")
    if calls.get("found_more"):
        lines.append(f"- ほか {calls['found_more']} 件（上限で省略）")
    if calls.get("found_hidden"):
        lines.append(f"- 名前を出せない資料 {calls['found_hidden']} 件")
    if calls.get("found_hits_omitted"):
        lines.append(f"- 1 回の呼び出しの上限で取り込めなかった検索のヒット {calls['found_hits_omitted']} 件")
    return lines


def render_markdown(record: dict) -> str:
    """調査台帳の記録1件を Markdown へ整形する。`record` は `complete`/`truncated`/`manifest`/`items`/`coverage`/`reviews`（省略可）を持つ dict。"""
    manifest = record.get("manifest") or {}
    items = record.get("items") or {}
    question_kind = _label(_QUESTION_KIND_LABELS, manifest.get("question_kind"))
    detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
    if detail.get("ledger") == "none":
        note = describe_dropped(detail.get("dropped")) if record.get("truncated") else ""
        return "\n".join([_HEADER, "", "調査台帳はありません（このターンは台帳を使っていません）。"]
                         + ([f"- 注記: {note}"] if note else [])
                         + _calls_section(detail.get("calls")) + [""])
    lines = [_HEADER, "",
             f"- 質問の種類: {question_kind}",
             f"- 完了: {'はい' if record.get('complete') else 'いいえ'}"]
    if record.get("truncated"):
        lines.append("- 注記: " + (describe_dropped(detail.get("dropped"))
                                  or "件数が多いため、一部を切り詰めて保存しています。"))
    reviews_report = detail.get("reviews_report") if isinstance(detail.get("reviews_report"), dict) else {}
    if reviews_report.get("over_count") or reviews_report.get("over_bytes") or reviews_report.get("invalid"):
        lines.append("- 注記: 途中の見直しは上限（件数・容量）または形式の不正のため、一部を読み込めていません"
                     f"（上限超過 {reviews_report.get('over_count') or 0} 件・不正 {reviews_report.get('invalid') or 0} 件"
                     + ("・容量超過あり" if reviews_report.get("over_bytes") else "") + "）。")
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
    # 中間の見直しの節（見直しごとに目的・観点・分かったこと・足した/外した項目・判断・追加の観点）
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
                # `extra_perspectives` は `investigation_ledger.sanitize_review_text_list` を通す
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
            for call in (detail.get("coverage") or {}).get(item_id) or []:
                if not isinstance(call, dict):
                    continue
                if isinstance(call.get("omitted"), int):
                    lines.append(f"  - ほか {call['omitted']} 件（省略）")
                    continue
                what = "・".join(_esc_cell(call[k]) for k in ("query", "doc", "range") if call.get(k))
                lines.append(f"  - {_esc_cell(call.get('tool'))}（{_label(_COVERAGE_LABELS, call.get('outcome'))}）"
                             + (f": {what}" if what else ""))
    lines += _calls_section(detail.get("calls"))
    lines.append("")
    return "\n".join(lines)
