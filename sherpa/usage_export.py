"""管理者の利用明細エクスポート（ZIP・`GET /admin/usage/export`）。

期間内の回答（`messages.answer`）から、本文（質問・回答・会話タイトル・参照した資料・ツールの引数）を含めず、数字・閉じた語彙の欄だけを
取り出し、`turns.csv`/`agents.csv`/`tools.csv`/`aux_calls.csv`/`daily.csv`/`activity/<会話番号>.txt`/`summary.json`/`README.txt` を
1つの ZIP にまとめる。母集団・期間境界は `store.usage` に委ね、このモジュールは受け取った行を整形するだけ。
activity 由来の文字列の安全化は `turn_activity_format` に従う。
設計: docs/design/usage.md「明細エクスポート」
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import datetime

from fastapi.encoders import jsonable_encoder

from sherpa.csv_safe import csv_safe
from sherpa.store.usage import _JST, _USAGE_LIMIT_FIELD_KINDS
from sherpa.turn_activity_format import _IDENT, _WORD, _safe, _version, format_turn, label_agents, merge_tools

# 打ち切りの内訳（answer.limits）の列見出し。`web/usage.js` の LIMIT_LABEL と同じ表記、列順は `_USAGE_LIMIT_FIELD_KINDS` の定義順
_LIMIT_LABELS = {
    "tool_result_clipped": "1件の読取量を制限",
    "total_budget_hit": "累計の読取量に到達",
    "search_truncated": "検索件数を制限",
    "auto_continues": "続きを自動で実行",
    "duplicate_tool_call": "同じ条件の再検索を省略",
    "tool_calls_exhausted": "調査の回数上限に到達",
    "backend_unavailable_fulltext": "全文検索が使えなかった",
    "backend_unavailable_graph": "グラフが使えなかった",
    "graph_reingest_required": "グラフは再取り込み待ち",
    "context_compactions": "会話履歴を整理",
    "synthesis_truncated": "回答用の情報量を制限",
    "depth_escalated": "自動で深く調べた",
}
_LIMIT_FIELDS = list(_USAGE_LIMIT_FIELD_KINDS.items())
# CSV のモデル名の形。パス区切り・空白・非 ASCII を含むものは中身を出さず「（その他）」にまとめる
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,80}$")

_TURNS_HEADER = [
    "会話番号", "回答番号", "日時(JST)", "利用者ID", "用途", "経路", "モデル",
    "終了理由", "エラーコード", "所要ms",
    "準備ms", "Codex実行ms", "後処理ms",
    "入力トークン", "キャッシュ入力トークン", "出力トークン", "推論トークン",
    "下調べ役の数", "下調べ役トークン合計",
    "本体往復数", "本体最大入力", "圧縮回数",
    "台帳完了", "台帳継続回数",
    "設定mode", "設定depth", "設定review_rounds", "設定reasoning", "版",
] + [_LIMIT_LABELS[f] for f, _ in _LIMIT_FIELDS]

_AGENTS_HEADER = ["会話番号", "回答番号", "役割", "モデル", "入力トークン", "キャッシュ入力トークン",
                 "出力トークン", "推論トークン", "往復数", "最大入力", "圧縮回数"]

_TOOLS_HEADER = ["会話番号", "回答番号", "役割", "ツール名", "回数", "バイト", "最大バイト",
                "合計ms", "切詰", "打切", "失敗", "サンド失敗"]

_AUX_HEADER = ["日時(JST)", "用途", "経路", "モデル", "入力トークン", "キャッシュ入力トークン",
              "出力トークン", "推論トークン", "呼び出し回数", "経過ms", "利用者ID", "会話番号"]

_DAILY_HEADER = ["日付", "利用者数", "ターン数", "入力トークン", "出力トークン"]


def _num(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _str_or_none(v):
    return v if isinstance(v, str) and v else None


def _safe_or_none(v, pattern):
    return _safe(v, pattern) if isinstance(v, str) and v else None


def _setting_value(v):
    """activity.settings の1値を出力用に整形する（bool/int/None はそのまま、それ以外の文字列だけ安全化する）。"""
    if isinstance(v, bool) or v is None or isinstance(v, int):
        return v
    return _safe(v, _WORD)


def _jst_str(dt) -> str | None:
    return dt.astimezone(_JST).strftime("%Y-%m-%d %H:%M:%S") if isinstance(dt, datetime) else None


def _turns_row(row: dict, answer: dict) -> list:
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    limits = answer.get("limits") if isinstance(answer.get("limits"), dict) else {}
    activity = answer.get("activity") if isinstance(answer.get("activity"), dict) else None
    investigation = answer.get("investigation") if isinstance(answer.get("investigation"), dict) else None
    is_codex = isinstance(activity, dict) and activity.get("source") == "codex_rollout"

    phases = activity.get("phases_ms") if is_codex and isinstance(activity.get("phases_ms"), dict) else {}
    settings = activity.get("settings") if is_codex and isinstance(activity.get("settings"), dict) else {}
    app_version = activity.get("app_version") if is_codex else None

    agents = activity.get("agents") if is_codex and isinstance(activity.get("agents"), list) else []
    labeled = label_agents(agents)
    parent = next((a for label, a in labeled if label == "本体"), None)
    children = [a for label, a in labeled if label != "本体"]
    has_agents = bool(labeled)

    def _agent_tok(agent: dict, key: str) -> int:
        tok = agent.get("tokens") if isinstance(agent.get("tokens"), dict) else {}
        return _num(tok.get(key)) or 0

    sub_agent_tokens_total = (
        sum(_agent_tok(a, "input_tokens") + _agent_tok(a, "output_tokens") for a in children)
        if has_agents else None)

    parent_rounds = (parent.get("rounds") if isinstance(parent, dict) and isinstance(parent.get("rounds"), list)
                     else [])
    parent_inputs = [r[0] for r in parent_rounds
                     if isinstance(r, list) and r and isinstance(r[0], int) and not isinstance(r[0], bool)]

    compactions_total = None
    if has_agents:
        compactions_total = sum(
            len(a.get("compactions")) if isinstance(a.get("compactions"), list) else 0
            for _, a in labeled)

    limit_values = []
    for field, kind in _LIMIT_FIELDS:
        v = limits.get(field)
        limit_values.append((_num(v) or 0) if kind == "count" else (v is True))

    return [
        row.get("conversation_id"), row.get("message_id"), _jst_str(row.get("message_created_at")),
        _str_or_none(row.get("uid")), _safe_or_none(row.get("lens"), _IDENT),
        _safe_or_none(usage.get("provider"), _IDENT), _safe_or_none(usage.get("model"), _MODEL),
        _safe_or_none(answer.get("stop_kind"), _IDENT), _safe_or_none(answer.get("codex_error_code"), _IDENT),
        _num(answer.get("duration_ms")),
        _num(phases.get("prepare")), _num(phases.get("agent")), _num(phases.get("post")),
        _num(usage.get("input_tokens")), _num(usage.get("cached_input_tokens")),
        _num(usage.get("output_tokens")), _num(usage.get("reasoning_output_tokens")),
        len(children) if has_agents else None, sub_agent_tokens_total,
        len(parent_rounds) if parent is not None else None,
        (max(parent_inputs) if parent_inputs else None) if parent is not None else None,
        compactions_total,
        investigation.get("complete") if isinstance(investigation, dict) else None,
        _num(investigation.get("continuations")) if isinstance(investigation, dict) else None,
        _setting_value(settings.get("mode")) if "mode" in settings else None,
        _setting_value(settings.get("depth")) if "depth" in settings else None,
        _setting_value(settings.get("review_rounds")) if "review_rounds" in settings else None,
        _setting_value(settings.get("reasoning")) if "reasoning" in settings else None,
        _version(app_version) if isinstance(app_version, str) and app_version else None,
    ] + limit_values


def _agent_row(conv_id, msg_id, label: str, agent: dict) -> list:
    tok = agent.get("tokens") if isinstance(agent.get("tokens"), dict) else {}
    rounds = agent.get("rounds") if isinstance(agent.get("rounds"), list) else []
    inputs = [r[0] for r in rounds
             if isinstance(r, list) and r and isinstance(r[0], int) and not isinstance(r[0], bool)]
    comps = agent.get("compactions") if isinstance(agent.get("compactions"), list) else []
    return [
        conv_id, msg_id, label, _safe(agent.get("model"), _MODEL),
        _num(tok.get("input_tokens")), _num(tok.get("cached_input_tokens")),
        _num(tok.get("output_tokens")), _num(tok.get("reasoning_output_tokens")),
        len(rounds), max(inputs) if inputs else None, len(comps),
    ]


def _tool_row(conv_id, msg_id, label: str, name: str, t: dict) -> list:
    return [
        conv_id, msg_id, label, name,
        _num(t.get("calls")), _num(t.get("bytes")), _num(t.get("max_bytes")), _num(t.get("ms")),
        _num(t.get("clipped")), _num(t.get("truncated")), _num(t.get("errors")), _num(t.get("sandbox_errors")),
    ]


def _aux_row(r: dict) -> list:
    return [
        _jst_str(r.get("ts")), _safe_or_none(r.get("kind"), _IDENT), _safe_or_none(r.get("provider"), _IDENT),
        _safe_or_none(r.get("model"), _MODEL),
        _num(r.get("input_tokens")), _num(r.get("cached_input_tokens")),
        _num(r.get("output_tokens")), _num(r.get("reasoning_output_tokens")),
        _num(r.get("calls")), _num(r.get("elapsed_ms")),
        _str_or_none(r.get("uid")), r.get("conversation_id") if isinstance(r.get("conversation_id"), int) else None,
    ]


def _daily_rows(summary: dict) -> list[list]:
    """`daily` と `tokens.daily` を日付で突き合わせる（片方にしか無い日付も欠側を空欄にして残す）。"""
    daily = {d["date"]: d for d in (summary.get("daily") or []) if isinstance(d, dict) and d.get("date")}
    token_daily = {d["date"]: d for d in ((summary.get("tokens") or {}).get("daily") or [])
                  if isinstance(d, dict) and d.get("date")}
    rows = []
    for date in sorted(set(daily) | set(token_daily)):
        d, t = daily.get(date, {}), token_daily.get(date, {})
        rows.append([date, d.get("active_users"), d.get("turns"), t.get("input"), t.get("output")])
    return rows


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows([csv_safe(v) for v in r] for r in rows)
    return buf.getvalue().encode("utf-8-sig")


def _readme_text(period: dict, retrieved_at: datetime, app_ver: str | None) -> str:
    lines = [
        "Sherpa 利用明細エクスポート",
        "",
        f"期間: {period.get('start')} 〜 {period.get('end')}（JST 暦日・両日を含む）",
        f"実際に使った半開区間: {period.get('from')} 〜 {period.get('to')}",
        f"取得時刻: {retrieved_at.astimezone(_JST).strftime('%Y-%m-%d %H:%M:%S')} JST",
        f"アプリの版: {app_ver or '不明'}",
        "",
        "質問と回答の本文・会話の題名・参照した資料・ツールの引数は含みません。会話番号・回答番号で",
        "DB や `make trace CONV=<会話番号>` と突き合わせられます。",
        "",
        "ファイルと列:",
        "  summary.json    利用統計の集計（GET /admin/usage/stats の応答そのもの。画面は上位10件に絞る一覧も全件）"
        "（上位10件などの絞り込みも画面と同じ）。",
        "  turns.csv       回答（assistant 返答）1件=1行。会話番号・回答番号・日時・利用者ID・"
        "用途・経路・モデル・終了理由・エラーコード・所要時間・準備/Codex/後処理の所要時間・"
        "トークン4種・下調べ役の数とトークン合計・本体の往復数と最大入力・圧縮回数・調査台帳"
        "（完了・継続回数）・主な設定・版・打ち切りの内訳。Codex 経路の詳しい記録が無い回答は"
        "該当欄が空欄です。",
        "  agents.csv      回答×エージェント（本体/下調べ役N）=1行。モデル・トークン4種・"
        "往復数・最大入力・圧縮回数。",
        "  tools.csv       回答×エージェント×ツール=1行。回数・バイト・最大バイト・合計所要"
        "時間・切詰/打切/失敗/サンドボックス失敗の件数。",
        "  aux_calls.csv   期間内のチャット以外のAI呼び出し（usage_events）1件=1行。日時・"
        "用途・経路・モデル・トークン4種・呼び出し回数・経過時間・利用者ID・会話番号。",
        "  daily.csv       日別。日付・利用者数・ターン数・入力/出力トークン。",
        "  activity/<会話番号>.txt   その会話の期間内の回答ごとの活動記録（本体と下調べ役の"
        "トークン・往復・圧縮・ツール別の回数とバイト）を数字だけで表示。",
        "",
        "列が空欄なのは「その経路・その回答では記録されない」ことを表し、0（測って0だった）とは"
        "区別しています。",
    ]
    return "\n".join(lines) + "\n"


def build_export_zip(fileobj, *, summary: dict, turn_rows: list[dict], aux_rows: list[dict],
                     retrieved_at: datetime, app_ver: str | None) -> None:
    """`fileobj`（シーク可能な書き込み先）へ ZIP を書き込む。

    `summary`: `store.usage_stats(...)` の結果（読み取るだけ）。`turn_rows`: `store.usage_export_turns(...)` の行
    （数字・閉じた語彙の欄だけを抜き出し、本文は zip のどのファイルへも書かない）。`aux_rows`: `store.usage_export_aux_calls(...)` の行。
    """
    period = summary.get("period") or {}
    turns_csv_rows: list[list] = []
    agents_csv_rows: list[list] = []
    tools_csv_rows: list[list] = []
    activity_by_conv: dict = {}

    for row in turn_rows:
        answer = row.get("answer") if isinstance(row.get("answer"), dict) else {}
        conv_id = row.get("conversation_id")
        msg_id = row.get("message_id")

        turns_csv_rows.append(_turns_row(row, answer))

        activity = answer.get("activity") if isinstance(answer.get("activity"), dict) else None
        if isinstance(activity, dict) and activity.get("source") == "codex_rollout":
            agents = activity.get("agents") if isinstance(activity.get("agents"), list) else []
            for label, agent in label_agents(agents):
                agents_csv_rows.append(_agent_row(conv_id, msg_id, label, agent))
                for name, t in merge_tools(agent.get("tools")).items():
                    tools_csv_rows.append(_tool_row(conv_id, msg_id, label, name, t))

        activity_by_conv.setdefault(conv_id, []).append({
            "id": msg_id, "created_at": row.get("message_created_at"),
            "activity": activity, "stop_kind": answer.get("stop_kind"),
            "codex_error_code": answer.get("codex_error_code"),
            "duration_ms": answer.get("duration_ms"),
            "investigation": answer.get("investigation"),
        })

    with zipfile.ZipFile(fileobj, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", _readme_text(period, retrieved_at, app_ver))
        zf.writestr("summary.json", json.dumps(jsonable_encoder(summary), ensure_ascii=False, indent=2))
        zf.writestr("turns.csv", _csv_bytes(_TURNS_HEADER, turns_csv_rows))
        zf.writestr("agents.csv", _csv_bytes(_AGENTS_HEADER, agents_csv_rows))
        zf.writestr("tools.csv", _csv_bytes(_TOOLS_HEADER, tools_csv_rows))
        zf.writestr("aux_calls.csv", _csv_bytes(_AUX_HEADER, [_aux_row(r) for r in aux_rows]))
        zf.writestr("daily.csv", _csv_bytes(_DAILY_HEADER, _daily_rows(summary)))
        for conv_id, rows in sorted(activity_by_conv.items(), key=lambda kv: (kv[0] is None, kv[0])):
            text = [f"会話 {conv_id}（回答 {len(rows)} 件）"]
            for r in rows:
                text.extend(format_turn(r))
            zf.writestr(f"activity/{conv_id}.txt", "\n".join(text) + "\n")
