"""回答の「調べた範囲」（`answer.investigation_summary`）の中身を、回答に残った事実から組み立てる。
入力は `env["limits"]`（打ち切り）・`env["investigation"]`（調査台帳の件数・未確認・壊れた項目・調べた量・見直しの読み取り）だけ。
本文・資料名は読まない（台帳の項目名と、定型の理由文だけを使う）。`answer_shape.seal` が呼ぶ。
設計: docs/design/chat.md「回答の形」
`sherpa` 内の他モジュールに依存しない葉モジュール。
"""
from __future__ import annotations

# 未確認の項目を一覧にする件数の上限（超えた分は「ほか N 件」）。
UNCONFIRMED_LIST_MAX = 50

_LABEL_LIMIT = "打ち切り"
_LABEL_CONFIRMED = "確認できた項目"
_LABEL_UNCONFIRMED = "確認できなかった項目"
_LABEL_OMITTED = "回答に入っていない項目"
_LABEL_BROKEN = "壊れていた項目"
_LABEL_EFFORT = "調べた量"
_LABEL_REVIEW = "途中の見直し"
_LABEL_RECORD = "調査の記録"

# 打ち切り（件数で数える値は `{n}` に件数を入れる）。真偽の値は True のときだけ出す。
_LIMIT_TEXTS: tuple[tuple[str, str], ...] = (
    ("search_truncated", "検索の結果が多く、途中で打ち切った検索が {n} 回ありました（残りは確認していません）。"),
    ("tool_result_clipped", "読んだ内容が大きく、途中で切り詰めた回数が {n} 回ありました（切った先は読んでいません）。"),
    ("total_budget_hit", "読み取り量の上限に達したため、それ以上は調べていません。"),
    ("tool_calls_exhausted", "道具を使える回数の上限に達したため、それ以上は調べていません。"),
    ("duplicate_tool_call", "同じ条件の検索を繰り返したため、{n} 回は実行せずに断りました。"),
    ("backend_unavailable_fulltext", "全文検索が使えない状態でした。原本を直接探して調べています。"),
    ("wall_clock_hit", "時間の上限に達したため、ここまでの結果で打ち切りました。"),
    ("ledger_incomplete", "調査の確認項目が一部終わらないまま回答しています。"),
    ("claims_unmatched", "調べた記録と合わない主張があったため、「推定」に下げています。"),
)
_CONFIRMED_LABELS = (("source_confirmed", "ソースで確認"), ("spec_only", "設計書だけで確認"),
                     ("conflict", "ソースと設計書が食い違う"))


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _split_names(names) -> tuple[list[str], int]:
    """項目名のうち、秘匿名として判定されるもの（`ingest.text_kind.is_sensitive_doc_id`）を件数に落として `(出せる名前, 伏せた件数)`。"""
    from .ingest import text_kind
    shown = [n for n in names if not text_kind.is_sensitive_doc_id(n)]
    return shown, len(names) - len(shown)


def build_items(env: dict) -> list[dict]:
    """`[{label, text}]`。打ち切り・確認できた／できなかった項目・壊れた項目・調べた量・見直しの読み取りの省略・調査記録の欠落の順。"""
    items: list[dict] = []
    limits = env.get("limits") if isinstance(env.get("limits"), dict) else {}
    for key, template in _LIMIT_TEXTS:
        value = limits.get(key)
        n = _count(value)
        if value is True or n:
            items.append({"label": _LABEL_LIMIT, "text": template.format(n=n)})
    inv = env.get("investigation") if isinstance(env.get("investigation"), dict) else {}
    if inv.get("manifest_invalid"):
        items.append({"label": _LABEL_BROKEN, "text": "調査の目録（確認する項目の一覧）が壊れていて、項目ごとの確認を確かめられません。"})
    counts = inv.get("counts") if isinstance(inv.get("counts"), dict) else {}
    confirmed = [(label, _count(counts.get(key))) for key, label in _CONFIRMED_LABELS]
    total = sum(n for _label, n in confirmed)
    if total:
        parts = "・".join(f"{label} {n} 件" for label, n in confirmed if n)
        items.append({"label": _LABEL_CONFIRMED, "text": f"{total} 件（{parts}）"})
    unconfirmed = [u for u in (inv.get("unconfirmed_items") or [])
                   if isinstance(u, dict) and isinstance(u.get("item"), str) and isinstance(u.get("reason"), str)]
    hidden = 0
    for u in unconfirmed[:UNCONFIRMED_LIST_MAX]:
        if _split_names([u["item"]])[1]:
            hidden += 1
            continue
        items.append({"label": _LABEL_UNCONFIRMED, "text": f"{u['item']}（{u['reason']}）"})
    if len(unconfirmed) > UNCONFIRMED_LIST_MAX:
        items.append({"label": _LABEL_UNCONFIRMED, "text": f"ほか {len(unconfirmed) - UNCONFIRMED_LIST_MAX} 件"})
    if hidden:
        items.append({"label": _LABEL_UNCONFIRMED, "text": f"名前を表示できない項目 {hidden} 件"})
    omitted = [n for n in (inv.get("omitted_items") or []) if isinstance(n, str)]
    omitted_hidden = _count(inv.get("omitted_hidden"))
    if omitted or omitted_hidden:
        shown, masked = _split_names(omitted[:UNCONFIRMED_LIST_MAX])
        more = len(omitted) - len(shown) - masked
        masked += omitted_hidden
        tail = ("、".join(shown) + (f" ほか {more} 件" if more > 0 else "")
                + (f"（名前を表示できない項目 {masked} 件）" if masked else ""))
        items.append({"label": _LABEL_OMITTED,
                      "text": f"{len(omitted) + omitted_hidden} 件（調べて確認できましたが、回答の主張に対応づけられていません: {tail}）"})
    invalid = [i for i in (inv.get("invalid") or []) if isinstance(i, str)]
    invalid_total = max(_count(inv.get("invalid_total")), len(invalid))
    if invalid_total:
        shown, masked = _split_names(invalid[:UNCONFIRMED_LIST_MAX])
        more = invalid_total - len(shown) - masked
        tail = ("、".join(shown) + (f" ほか {more} 件" if more > 0 else "")
                + (f"（名前を表示できない項目 {masked} 件）" if masked else ""))
        items.append({"label": _LABEL_BROKEN,
                      "text": f"{invalid_total} 件（項目の記録が壊れていて確認できませんでした: {tail}）"})
    effort = inv.get("effort") if isinstance(inv.get("effort"), dict) else {}
    searches, docs = _count(effort.get("searches")), _count(effort.get("docs_read"))
    if searches or docs:
        items.append({"label": _LABEL_EFFORT, "text": f"検索 {searches} 回・読んだ資料 {docs} 件（確認できた範囲の数です）"})
    report = inv.get("reviews_report") if isinstance(inv.get("reviews_report"), dict) else {}
    omitted = _count(report.get("over_count")) + _count(report.get("invalid"))
    if omitted or report.get("over_bytes"):
        items.append({"label": _LABEL_REVIEW,
                      "text": f"上限（件数・容量）または形式の不正のため、読み込めなかった見直しがあります（{omitted} 件以上）。"})
    for note in (inv.get("record_notes") or []):
        if isinstance(note, str) and note.strip():
            items.append({"label": _LABEL_RECORD, "text": note.strip()})
    return items


def fill(env: dict) -> None:
    """`env["investigation_summary"]` を組み立てて入れる。呼び出し側が自前で詰めた中身（`auto` 印の無い非空の `items`）は上書きしない。
    組み立てた中身には `auto: true` を付け、次の呼び出しで事実から作り直す（何度呼んでも同じ結果）。"""
    current = env.get("investigation_summary")
    if isinstance(current, dict) and current.get("items") and not current.get("auto"):
        return
    items = build_items(env)
    summary: dict = {"v": 1, "items": items}
    if items:
        summary["auto"] = True
    env["investigation_summary"] = summary
