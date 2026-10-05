"""1ターンの活動記録（`answer["activity"]`）を安全な文字列表現へ変換する共通ロジック。

利用明細エクスポート（`usage_export.py`）と会話トレース（`scripts/conversation_trace.py`）が共有する。識別子の形の文字列だけを出し、
それ以外は「（その他）」に畳み込む。`source` が `"codex_rollout"` のときだけ agents/tools/settings を持つ。
"""
from __future__ import annotations

import re

_TOP_TOOLS = 8
# 表示してよい文字列の形。合わないものは中身を出さず「その他」にまとめる
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


def merge_tools(tools) -> dict:
    """1エージェント分の `tools` を、名前の安全化で畳み込みながら合算する（`max_bytes` は最大値、他の整数は合算）。"""
    merged: dict = {}
    if not isinstance(tools, dict):
        return merged
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
    return merged


def label_agents(agents) -> list[tuple[str, dict]]:
    """`activity.agents` へ表示用ラベル（本体／下調べ役N）を振る。非 dict は読み飛ばす。"""
    labeled: list[tuple[str, dict]] = []
    if not isinstance(agents, list):
        return labeled
    child_no = 0
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if agent.get("role") == "parent":
            label = "本体"
        else:
            child_no += 1
            label = f"下調べ役{child_no}"
        labeled.append((label, agent))
    return labeled


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
    merged = merge_tools(agent.get("tools"))
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
    """assistant メッセージ 1 件分の表示行。`row` は id・created_at と answer から選んだ欄だけを持つ。"""
    act = row.get("activity") if isinstance(row.get("activity"), dict) else None
    head = (f"── #{row.get('id')} {row.get('created_at')} 終了: {_safe(row.get('stop_kind'), _IDENT) if row.get('stop_kind') else '-'}"
            + (f" エラー: {_safe(row['codex_error_code'], _IDENT)}" if row.get("codex_error_code") else "")
            + f" 所要 {_sec(row.get('duration_ms'))}")
    if act is None:
        return [head, "  活動記録なし（0.13.1 より前の版で保存）"]
    if act.get("source") != "codex_rollout":
        # Codex 以外の経路や Codex を起動しなかったターンは、版と合計の所要だけを持つ
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
    for label, agent in label_agents(agents):
        lines.extend("  " + ln for ln in _agent_lines(agent, label))
    if not agents:
        lines.append("  エージェントの記録なし（セッション記録を読めなかった）")
    return lines
