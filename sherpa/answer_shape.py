"""回答（`messages.answer`）の形: 本文・注記・調べた範囲・完了状態の分離。
`body`（AI の回答本文・注記で書き換えない）／`notices`（`[{kind, text}]`・根拠不足・未確認・時間切れ・停止・失敗・巻き戻しなど）／
`investigation_summary`（調べた範囲の器）／`completion`（complete・partial・stopped・failed）／`answer_schema`（形の版）。
`headline` は旧クライアント向けの投影（notices の文 + body）で、会話の `content`・検索・監査の書き出しはこの投影を読む。
旧形式の行（`body` が無い answer）は `headline` を body として読む（`body_of`）。
設計: docs/design/chat.md「回答の形」
`investigation_summary` の中身は `investigation_summary.fill` が回答の事実（limits・investigation）から組み立てる。
`sherpa` 内の他モジュールは `investigation_summary`（葉）にだけ依存する。
"""
from __future__ import annotations

from . import investigation_summary

ANSWER_SCHEMA = 2
COMPLETIONS = ("complete", "partial", "stopped", "failed")
# 注記の種類（`add_notice` に渡す kind）の閉じた語彙。利用統計は、これ以外を `other` にまとめる。
NOTICE_KINDS = (
    "coverage_write_failed", "created_files_failed", "evidence_gate", "files_discarded", "graph_degraded",
    "investigation_record_trimmed", "marp_failed", "no_sources", "recovered_error", "review_continuation",
    "sources_unverified", "stopped", "stopped_early", "unconfirmed_items", "wall_clock",
    "ledger_unfinished", "partial_followup", "truncated", "invalid_output", "answer_recovery", "review_reverted",
    "investigation_record_failed",
)

STOPPED_NOTICE = "利用者の操作で停止しました。停止までに回収した部分回答です。"

STOPPED_EARLY_NOTICE = ("AI が途中経過を伝えたまま調査を終えたため、途中までの結果です。"
                        "「続きを調べる」を押すと続きから調べられます。")

# 封印前（`body` 未設定）の `headline` は本文そのもの。封印後の `headline` は notices + body の投影。


def body_of(answer) -> str:
    """回答の本文。旧形式の行は `headline` を本文として読む。"""
    if not isinstance(answer, dict):
        return ""
    body = answer.get("body")
    if isinstance(body, str):
        return body
    head = answer.get("headline")
    return head if isinstance(head, str) else ""


def notices_of(answer) -> list[dict]:
    """型の正しい注記（`kind`・`text` が文字列で text が空でない）だけを返す。旧形式の行・壊れた値は空リスト。"""
    items = answer.get("notices") if isinstance(answer, dict) else None
    if not isinstance(items, list):
        return []
    return [{"kind": n["kind"], "text": n["text"]} for n in items
            if isinstance(n, dict) and isinstance(n.get("kind"), str)
            and isinstance(n.get("text"), str) and n["text"].strip()]


def investigation_summary_of(answer) -> dict | None:
    """調べた範囲の器を型の正しい形（`{v, items:[{label, text}]}`）だけで返す。無い・壊れていれば None。"""
    s = answer.get("investigation_summary") if isinstance(answer, dict) else None
    if not isinstance(s, dict) or not isinstance(s.get("items"), list):
        return None
    items = [{"label": i["label"], "text": i["text"]} for i in s["items"]
             if isinstance(i, dict) and isinstance(i.get("label"), str) and isinstance(i.get("text"), str)]
    return {"v": s["v"] if isinstance(s.get("v"), int) and not isinstance(s.get("v"), bool) else 1,
            "items": items}


def project_headline(notices: list[dict], body: str) -> str:
    """notices の文を本文の前に並べた旧クライアント向けの文字列。"""
    parts = [n["text"].strip() for n in notices]
    if body and body.strip():
        parts.append(body)
    return "\n\n".join(parts)


def add_notice(env: dict, kind: str, text: str) -> None:
    """注記を足す（同じ kind と文は 1 件）。本文は書き換えない。封印済みなら headline も投影し直す。"""
    text = (text or "").strip()
    if not text:
        return
    items = env.setdefault("notices", [])
    if any(n.get("kind") == kind and n.get("text") == text for n in items):
        return
    items.append({"kind": kind, "text": text})
    if isinstance(env.get("body"), str):
        env["headline"] = project_headline(notices_of(env), env["body"])


def set_body(env: dict, text: str) -> None:
    """本文を置き換える（本文が AI の回答でない定型文のとき・封印済みなら headline も投影し直す）。"""
    if isinstance(env.get("body"), str):
        env["body"] = text
        env["headline"] = project_headline(notices_of(env), text)
    else:
        env["headline"] = text


def seal(env: dict) -> dict:
    """本文・注記・調べた範囲（`investigation_summary.fill`）・版を確定し、headline を投影にする。何度呼んでも同じ結果（封印後の注記追加は `add_notice` が投影し直す）。"""
    body = env["body"] if isinstance(env.get("body"), str) else (env.get("headline") or "")
    env["body"] = body
    env["notices"] = notices_of(env)
    env["answer_schema"] = ANSWER_SCHEMA
    investigation_summary.fill(env)
    env["headline"] = project_headline(env["notices"], body)
    return env
