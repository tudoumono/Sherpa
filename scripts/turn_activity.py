"""1 つの会話の各ターンの活動記録（`answer["activity"]`）を数字だけで表示する（`make turn-activity CONV=<番号>`）。

質問・回答の本文・会話の題名・参照した資料・ツールの引数は DB から取得しない（SQL で activity と、
終了理由・所要・台帳の件数などの数字と閉じた語彙の欄だけを選んで読む。activity 自体も本文や資料名を
持たない契約）。台帳は完了・継続回数・状態別の件数だけを読み、項目の id（モデルが付けた名前）は読まない。
activity の文字列（ツール名・未解析の種類・モデル名・設定値・エラーコード）は識別子の形のものだけを表示し、
それ以外は中身を出さず「（その他）」にまとめる。版（VERSION＋git の短い SHA・利用者の本文ではない）は形が想定と
違っても見当が付くよう、英数字と . + _ - 以外を ? に置き換えて出す。activity は Codex 経路で 0.13.1 以降に保存した
ターンにだけある。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))   # scripts/ から直接起動しても sherpa を読めるように（world_admin.py と同じ）

_TOP_TOOLS = 8
# 表示してよい文字列の形。activity の文字列（ツール名・未解析の種類・モデル名・設定値・エラーコード）は
# Codex CLI 側の語彙だが閉集合ではないので、この形に合わないものは中身を出さず「その他」にまとめる。
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_UNPARSED_KEY = re.compile(r"^[a-z_]{1,40}(:[a-z_?]{1,40})?$")
_WORD = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")
_VERSION_UNSAFE = re.compile(r"[^0-9A-Za-z.+_-]")
_OTHER = "（その他）"


def _safe(value, pattern) -> str:
    return value if isinstance(value, str) and pattern.match(value) else _OTHER


def _version(v) -> str:
    return _VERSION_UNSAFE.sub("?", v)[:40] if isinstance(v, str) and v else "-"


def _n(v) -> str:
    return f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else "-"


def _kib(v) -> str:
    return f"{v / 1024:,.0f}KiB" if isinstance(v, int) and not isinstance(v, bool) else "-"


def _sec(ms) -> str:
    return f"{ms / 1000:,.0f}s" if isinstance(ms, int) and not isinstance(ms, bool) else "-"


def _agent_lines(agent: dict, label: str) -> list[str]:
    tok = agent.get("tokens") if isinstance(agent.get("tokens"), dict) else {}
    rounds = agent.get("rounds") if isinstance(agent.get("rounds"), list) else []
    inputs = [r[0] for r in rounds if isinstance(r, list) and r and isinstance(r[0], int)]
    comps = agent.get("compactions") if isinstance(agent.get("compactions"), list) else []
    lines = [
        f"[{label} {_safe(agent.get('model'), _WORD)}] 入力 {_n(tok.get('input_tokens'))}"
        f"（キャッシュ {_n(tok.get('cached_input_tokens'))}） 出力 {_n(tok.get('output_tokens'))}"
        f" 推論 {_n(tok.get('reasoning_output_tokens'))} 往復 {len(rounds)}"
        f" 最大入力 {_n(max(inputs) if inputs else None)} 圧縮 {len(comps)}回"
        + (f" @{','.join(str(c) for c in comps[:8])}" if comps else "")
    ]
    tools = agent.get("tools") if isinstance(agent.get("tools"), dict) else {}
    merged: dict = {}
    for name, t in tools.items():
        if not isinstance(t, dict):
            continue
        key = _safe(name, _IDENT)
        if key not in merged:
            merged[key] = dict(t)
            continue
        acc = merged[key]
        for k, v in t.items():
            if isinstance(v, int) and not isinstance(v, bool):
                prev = acc.get(k) if isinstance(acc.get(k), int) and not isinstance(acc.get(k), bool) else 0
                acc[k] = max(prev, v) if k == "max_bytes" else prev + v
    ranked = sorted(
        ((name, t) for name, t in merged.items()),
        key=lambda kv: -(kv[1].get("bytes") if isinstance(kv[1].get("bytes"), int) else 0))
    for name, t in ranked[:_TOP_TOOLS]:
        extra = []
        for key, word in (("clipped", "切詰"), ("truncated", "打切"), ("errors", "失敗"),
                          ("sandbox_errors", "サンド失敗")):
            if isinstance(t.get(key), int) and t[key]:
                extra.append(f"{word}{t[key]}")
        took = f" 計{_sec(t['ms'])}" if isinstance(t.get("ms"), int) and not isinstance(t.get("ms"), bool) else ""
        lines.append(f"    {name} {_n(t.get('calls'))}回 {_kib(t.get('bytes'))}"
                     f" 最大{_kib(t.get('max_bytes'))}{took}" + (" " + " ".join(extra) if extra else ""))
    if len(ranked) > _TOP_TOOLS:
        lines.append(f"    ほか {len(ranked) - _TOP_TOOLS} 種")
    unparsed = agent.get("unparsed") if isinstance(agent.get("unparsed"), dict) else {}
    if unparsed:
        shown: dict = {}
        for k, v in unparsed.items():
            if isinstance(v, int) and not isinstance(v, bool):
                key = _safe(k, _UNPARSED_KEY)
                shown[key] = shown.get(key, 0) + v
        lines.append("    未解析: " + " ".join(f"{k}={v}" for k, v in sorted(shown.items())))
    return lines


def format_turn(row: dict) -> list[str]:
    """assistant メッセージ 1 件分の表示行。`row` は id・created_at と、answer から選んだ欄だけを持つ。"""
    act = row.get("activity") if isinstance(row.get("activity"), dict) else None
    head = (f"── #{row.get('id')} {row.get('created_at')} 終了: {_safe(row.get('stop_kind'), _IDENT) if row.get('stop_kind') else '-'}"
            + (f" エラー: {_safe(row['codex_error_code'], _IDENT)}" if row.get("codex_error_code") else "")
            + f" 所要 {_sec(row.get('duration_ms'))}")
    if act is None:
        return [head, "  活動記録なし（0.13.1 より前の版で保存）"]
    if act.get("source") != "codex_rollout":
        # Codex 以外の経路（API／Ollama）や Codex を起動しなかったターンは、版と合計の所要だけを持つ。
        return [head, "  Codex の詳しい記録なし（Codex 以外の経路・または Codex を起動しなかったターン）"
                      f" 版={_version(act.get('app_version'))}"]
    ph = act.get("phases_ms") if isinstance(act.get("phases_ms"), dict) else {}
    lines = [head + f"（準備 {_sec(ph.get('prepare'))}／Codex {_sec(ph.get('agent'))}／後処理 {_sec(ph.get('post'))}）"]
    st = act.get("settings") if isinstance(act.get("settings"), dict) else {}
    if st:
        def _setting(v):
            if isinstance(v, (bool, int)) or v is None:
                return str(v)
            return _safe(v, _WORD)
        lines.append("  設定: " + " ".join(f"{k}={_setting(st[k])}" for k in (
            "provider", "mode", "config", "model", "reasoning", "depth", "review_rounds",
            "schema_level", "multi_agent", "budget_per_result", "max_hits", "window_cap") if k in st)
                     + f" 版={_version(act.get('app_version'))}")
    inv = row.get("investigation") if isinstance(row.get("investigation"), dict) else None
    if inv is not None and any(inv.get(k) is not None for k in ("complete", "continuations", "counts")):
        counts = inv.get("counts") if isinstance(inv.get("counts"), dict) else {}
        lines.append(f"  台帳: 完了={inv.get('complete')} 継続={inv.get('continuations')} 終端: "
                     + (" ".join(f"{_safe(k, _IDENT)}={v}" for k, v in sorted(counts.items())
                                 if isinstance(v, int) and not isinstance(v, bool)) or "-"))
    agents = act.get("agents") if isinstance(act.get("agents"), list) else []
    child_no = 0
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if agent.get("role") == "parent":
            label = "本体"
        else:
            child_no += 1
            label = f"下調べ役{child_no}"
        lines.extend("  " + ln for ln in _agent_lines(agent, label))
    if not agents:
        lines.append("  エージェントの記録なし（セッション記録を読めなかった）")
    return lines


def _rows(conversation_id: int) -> list[dict]:
    # 読むだけの道具なので、スキーマを整える _ensure()（DDL）は呼ばない。
    from sherpa.store.db import _connect
    with _connect() as c:
        return c.execute(
            "SELECT id, created_at, answer->'activity' AS activity, answer->>'stop_kind' AS stop_kind, "
            "answer->>'codex_error_code' AS codex_error_code, "
            "CASE WHEN (answer->>'duration_ms') ~ '^[0-9]+$' THEN (answer->>'duration_ms')::bigint END "
            "AS duration_ms, jsonb_build_object("
            "  'complete', answer->'investigation'->'complete', "
            "  'continuations', answer->'investigation'->'continuations', "
            "  'counts', answer->'investigation'->'counts') AS investigation "
            "FROM messages WHERE conversation_id=%s AND role='assistant' AND answer IS NOT NULL ORDER BY id",
            (conversation_id,),
        ).fetchall()


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print("使い方: make turn-activity CONV=<会話番号>", file=sys.stderr)
        return 2
    conv = int(argv[1])
    rows = _rows(conv)
    if not rows:
        print(f"会話 {conv}: 回答の記録がありません")
        return 1
    print(f"会話 {conv}（回答 {len(rows)} 件）")
    for row in rows:
        for line in format_turn(dict(row)):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
