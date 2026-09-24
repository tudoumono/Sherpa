"""回答 JSON（`messages.answer`）から `turn_metrics`/`turn_tool_stats`（集計専用の細い写像表・
`docs/proposals/2026-09-23-利用統計の刷新.md` §3.1/§4 が正典）への書込。

正本は引き続き `messages.answer`（JSONB・全文）。本モジュールが書く2表はそこから**再生成できる
派生物**——集計クエリを索引付きで速くするためのコピーであり、単一真実源ではない。
`metrics_from_answer()` が唯一の写像元（answer dict → 列値 dict の純関数・DB を触らない）。

読み方（トークンの取り方・limits/claims の語彙）は `store/usage.py` の既存の集計定義に合わせる
——数え方がずれると新旧の集計値が食い違う。`_USAGE_LIMIT_INT_FIELDS`/`_USAGE_LIMIT_BOOL_FIELDS`/
`_CLAIM_STATUS_KEYS` は usage.py から import する（同じ store パッケージ内なので `store は他の
sherpa.* を import しない` 原則には抵触しない・値のドリフトを防ぐ単一の真実源）。
"""
from __future__ import annotations

import logging

from psycopg.types.json import Json

from .db import _connect, _ensure
from .usage import _CLAIM_STATUS_KEYS, _USAGE_LIMIT_BOOL_FIELDS, _USAGE_LIMIT_INT_FIELDS

_log = logging.getLogger("sherpa")

# metrics_from_answer() 自体の写像規則の版。規則を変えたら上げる——`turn_metrics.mapping_version`
# 列に残るため、規則変更前後の行を区別できる（過去行の再解釈可否の判断材料）。
MAPPING_VERSION = 1

# `answer["activity"]`（§3.1）の契約版。これ以外（欠落・不一致）は「activity 無し」として
# 全面的に旧フィールドへフォールバックする（部分的に信用しない＝壊れた envelope を都合よく
# 読み替えない）。
_ACTIVITY_SCHEMA_VERSION = 1

# `codex_usage_children`/`codex_usage_breakdown.children`（既存フィールド）のトークン4種のキー
# （`providers/codex/provider.py::_CHILD_USAGE_KEYS` と同じ語彙）。store は provider 層を
# import しない（leaf 原則・循環 import 回避）ため値を直接持つ——4値は `_usage_meta` の標準形
# として複数箇所に既に複製されている定数で、ここでの複製も同じ扱い。
_TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")

# metrics_from_answer() が返す列名のうち、identity 列（message_id/conversation_id/
# user_message_id/user_id/world/created_at/lens/personal・upsert() が呼び出し元コンテキストや
# 補助 SELECT から直接埋める）を除いた全列。INSERT/UPDATE 文を1箇所（このタプル）から機械的に
# 組み立てる（列の手書き重複を避ける）。
_METRIC_COLUMNS = (
    "provider", "model", "depth_profile", "reasoning", "app_version",
    "stop_kind", "codex_error_code", "duration_ms",
    "phase_prepare_ms", "phase_agent_ms", "phase_post_ms",
    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
    "parent_input_tokens", "parent_cached_input_tokens",
    "parent_output_tokens", "parent_reasoning_output_tokens",
    "child_input_tokens", "child_cached_input_tokens",
    "child_output_tokens", "child_reasoning_output_tokens",
    "children_detected", "children_usage_found", "children_usage_missing",
    "tool_calls_total", "tool_result_bytes_total", "api_rounds_total", "compactions_total",
) + _USAGE_LIMIT_INT_FIELDS + _USAGE_LIMIT_BOOL_FIELDS + (
    "sources_count",
    "investigation_complete", "investigation_continuations", "investigation_counts",
    "claims_confirmed", "claims_inferred", "claims_unknown", "claims_unknown_reasons",
    "gate_missing_codes",
    "activity_json",
    "mapping_source", "mapping_version",
)

# JSONB 列（psycopg へ渡す際 Json(...) で包む必要がある列。他は int/str/bool/None のスカラー）。
_JSONB_COLUMNS = frozenset({"investigation_counts", "claims_unknown_reasons",
                            "gate_missing_codes", "activity_json"})


# ---- 小さな防御的読み取りヘルパ（answer は外部由来の JSON なので型を信用しない） ----

def _clamp_int(v, default: int = 0) -> int:
    """非負整数として解釈できる値だけを通す（bool は int のサブクラスなので明示的に除外——
    既存の `_usage_tok`/`_usage_limit_int` と同じ防御思想）。それ以外は `default`。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, int) and v >= 0:
        return v
    return default


def _pos_int_or_none(v) -> int | None:
    """非負整数ならその値、そうでなければ None（「0」と「取れない」を区別する）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and v >= 0:
        return v
    return None


def _str_or_none(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _bool_or_none(v) -> bool | None:
    return v if isinstance(v, bool) else None


def _activity_dict(answer: dict) -> dict | None:
    """`answer["activity"]` が §3.1 契約どおりの形（`v==1` かつ `agents` が配列）なら返す。
    そうでなければ None（呼び出し側は全面的に旧フィールドへフォールバックする）。"""
    a = answer.get("activity")
    if not isinstance(a, dict) or a.get("v") != _ACTIVITY_SCHEMA_VERSION:
        return None
    if not isinstance(a.get("agents"), list):
        return None
    return a


def _agent_tokens(agent: dict) -> dict:
    tokens = agent.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    return {k: _clamp_int(tokens.get(k)) for k in _TOKEN_KEYS}


def _sum_tokens(agents: list, *, role: str | None) -> dict:
    """`agents`（activity の配列）のうち role 一致分（role=None なら全件）のトークンを合算する。
    非 dict の要素は無視する。"""
    out = {k: 0 for k in _TOKEN_KEYS}
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if role is not None and agent.get("role") != role:
            continue
        t = _agent_tokens(agent)
        for k in _TOKEN_KEYS:
            out[k] += t[k]
    return out


def _find_parent_agent(agents: list) -> dict | None:
    for agent in agents:
        if isinstance(agent, dict) and agent.get("role") == "parent":
            return agent
    return None


def _activity_aggregates(agents: list) -> tuple[int, int, int, int]:
    """`(tool_calls_total, tool_result_bytes_total, api_rounds_total, compactions_total)`。
    ツール呼出数・結果バイトは全エージェント×全ツールの合算、往復数・圧縮回数は全エージェントの
    `rounds`/`compactions` 配列長の合算。`bytes` キーの無いツール（`_tool_stat_rows` 参照）は
    合算では 0 扱い（`_clamp_int`）——欠落ツールの分だけ合計が過小になるが、値自体を欠くわけ
    ではない（1ツールごとの実際の欠落は `turn_tool_stats.bytes_total` の NULL で見分けられる）。"""
    tool_calls = 0
    tool_bytes = 0
    rounds_total = 0
    compactions_total = 0
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        tools = agent.get("tools")
        if isinstance(tools, dict):
            for t in tools.values():
                if isinstance(t, dict):
                    tool_calls += _clamp_int(t.get("calls"))
                    tool_bytes += _clamp_int(t.get("bytes"))
        rounds = agent.get("rounds")
        if isinstance(rounds, list):
            rounds_total += len(rounds)
        compactions = agent.get("compactions")
        if isinstance(compactions, list):
            compactions_total += len(compactions)
    return tool_calls, tool_bytes, rounds_total, compactions_total


def _tool_stat_rows(agents: list) -> list[tuple]:
    """`(agent_index, role, tool, calls, bytes_total, max_bytes, clipped, truncated, errors, ms)`
    のタプル列。`agent_index` は `agents` 配列の添字（0始まり・parent が通常先頭）。

    `calls` だけは常に測定される（0 既定）。`bytes_total`/`max_bytes`/`clipped`/`truncated`/
    `errors`/`ms` はキーが無ければ NULL——Codex 組み込みツール（function_call/custom_tool_call/
    tool_search/web_search）は測っていないこれらのキーを activity に置かない契約（§3.1）のため、
    欠落を 0 で埋めない（`web_search` は結果の大きさを取れず calls のみのエントリになる）。
    MCP ツールは全キーが揃って来るのでこれまでどおり実測値が入る。"""
    rows: list[tuple] = []
    for idx, agent in enumerate(agents):
        if not isinstance(agent, dict):
            continue
        role = _str_or_none(agent.get("role"))
        tools = agent.get("tools")
        if not isinstance(tools, dict):
            continue
        for tool_name, t in tools.items():
            if not isinstance(tool_name, str) or not tool_name or not isinstance(t, dict):
                continue
            rows.append((
                idx, role, tool_name,
                _clamp_int(t.get("calls")), _pos_int_or_none(t.get("bytes")),
                _pos_int_or_none(t.get("max_bytes")),
                _pos_int_or_none(t.get("clipped")), _pos_int_or_none(t.get("truncated")),
                _pos_int_or_none(t.get("errors")), _pos_int_or_none(t.get("ms")),
            ))
    return rows


def _limit_fields(limits) -> dict:
    """`_USAGE_LIMIT_INT_FIELDS`/`_USAGE_LIMIT_BOOL_FIELDS` の12項目を answer["limits"] から読む。
    `limits` 自体が dict でなければ全項目 None（「この打ち切り計測が無い」＝取れない）。dict が
    存在すれば項目ごとに数値/真偽へクランプ（欠落・非数値は 0/false——`limits` という区画自体は
    観測されている以上、その中の未設定項目は「起きなかった」の確定値として扱う。usage.py の
    `_usage_limit_int`/`_usage_limit_bool` と同じ既定＝集計側とずれない）。"""
    if not isinstance(limits, dict):
        return {f: None for f in (_USAGE_LIMIT_INT_FIELDS + _USAGE_LIMIT_BOOL_FIELDS)}
    out = {f: _clamp_int(limits.get(f)) for f in _USAGE_LIMIT_INT_FIELDS}
    out.update({f: bool(limits.get(f) is True) for f in _USAGE_LIMIT_BOOL_FIELDS})
    return out


def _claim_counts(data) -> dict:
    """`answer["data"]["claims"]`（配列）から `_CLAIM_STATUS_KEYS` 別件数。配列でなければ全 None
    （claims 自体を持たないターン＝旧回答・巡を持たない経路等）。"""
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        return {f"claims_{k}": None for k in _CLAIM_STATUS_KEYS}
    counts = {k: 0 for k in _CLAIM_STATUS_KEYS}
    for claim in claims:
        if isinstance(claim, dict) and claim.get("status") in counts:
            counts[claim["status"]] += 1
    return {f"claims_{k}": v for k, v in counts.items()}


def _claims_unknown_reasons(data) -> dict | None:
    """status='unknown' の主張を reason_code 別に数える（`usage.py::_compute_final_reason_codes`
    と同じ規則: reason_code が空なら 'unknown'、文字列でなければ飛ばす）。data.claims が配列で
    なければ None。閉じた語彙は上流で保証済み（`investigation_state._CLAIM_UNKNOWN_REASON_CODES`）
    ——ここでは検証しない。"""
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        return None
    counts: dict[str, int] = {}
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("status") != "unknown":
            continue
        code = claim.get("reason_code") or "unknown"
        if not isinstance(code, str):
            continue
        counts[code] = counts.get(code, 0) + 1
    return counts


def _gate_missing_codes(data) -> dict | None:
    """`answer["data"]["evidence_gate"]["missing_codes"]` を件数化する
    （`usage.py::_compute_final_missing_codes` と同じ規則: 空でない文字列だけを数える）。配列で
    なければ None。閉じた語彙は上流で保証済み（`providers/base.py::_MISSING_CODES`）——ここでは
    検証しない。"""
    gate = data.get("evidence_gate") if isinstance(data, dict) else None
    codes = gate.get("missing_codes") if isinstance(gate, dict) else None
    if not isinstance(codes, list):
        return None
    counts: dict[str, int] = {}
    for code in codes:
        if isinstance(code, str) and code:
            counts[code] = counts.get(code, 0) + 1
    return counts


def _investigation_fields(investigation) -> dict:
    """`answer["investigation"]`（provider.py が組み立てる辞書）から complete／継続回数／
    `investigation_counts`（**終端状態の件数のみ**・`counts`＝terminal_counts をそのまま）を読む。
    非終端・不正・欠落の id 一覧（`non_terminal`/`invalid`/`missing`）は先頭50件に切り詰められて
    おり全体の項目数を確定できない（未完了の台帳では過小な値になる）ため、項目数の列は持たない。
    """
    if not isinstance(investigation, dict):
        return {"investigation_complete": None, "investigation_continuations": None,
                "investigation_counts": None}
    counts = investigation.get("counts")
    if isinstance(counts, dict):
        counts_out = {k: v for k, v in counts.items()
                      if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
    else:
        counts_out = None
    return {
        "investigation_complete": _bool_or_none(investigation.get("complete")),
        "investigation_continuations": _pos_int_or_none(investigation.get("continuations")),
        "investigation_counts": counts_out,
    }


def _sources_count(answer: dict) -> int:
    """出典件数（`answer["sources"]` の配列長）。非配列/欠落は 0。

    「根拠なし」（usage.py の zero_hit）はここでは判定しない——その定義は assistant メッセージの
    `lens` 列（`messages.lens`・行の識別情報であり `answer` の中身ではない）と本列の組で決まるため、
    turn_metrics には出典件数だけを置き、判定は読み出し側（lens 列と sources_count 列を持つ集計）に委ねる。
    """
    sources = answer.get("sources")
    return len(sources) if isinstance(sources, list) else 0


def metrics_from_answer(answer: dict) -> dict:
    """回答1件（`messages.answer` の中身）→ `turn_metrics`/写像列の辞書への純粋な写像。

    DB を一切触らない（identity 列＝message_id/conversation_id/user_message_id/user_id/world/
    created_at/lens/personal は呼び出し元コンテキストや補助 SELECT 由来であり、本関数の対象外
    ——`upsert()` 側が付与する）。

    **優先順位と NULL の規則**（本関数全体の唯一の規則）:
      `answer["activity"]`（§3.1・`v==1` かつ `agents` が配列の場合のみ「有効」とみなす）が
      有効なら、そこから求まる値（トークン4種の合計・本体/下調べ役の内訳・下調べ役の数・
      ツール呼出数/結果バイト・API往復数・圧縮回数・所要の内訳・アプリの版）を使い、
      `activity_json` に検証済みの activity をそのまま保持する。無効/欠落なら、対応する
      **旧フィールド**（`answer["usage"]`・`answer["usage"]["codex_usage_breakdown"]`・
      `answer["codex_usage_children"]`）へ全面的にフォールバックし、`activity_json` は NULL。
      （部分的な混在はしない＝どちらの世代の値かが列ごとに変わると突合が壊れるため）。
      トークン合計を activity 優先にする理由: 失敗したターン（`context_window_exceeded` 等）は
      `answer["usage"]` が 0 のまま保存されるが、activity はセッション記録から実消費を拾える。

      「取れない値」（該当する区画（`usage`/`limits`/`activity`/`investigation`/`data.claims`）
      自体が丸ごと欠けている）は **NULL**、「区画はあるが個々の項目が無い/不正」は既存
      集計（usage.py）と同じ既定（int=0・bool=false）に倒す——0 と「不明」を区別する
      （区画の有無＝計測されたかどうかの証拠、項目値の有無＝その計測の中身）。
      `turn_tool_stats` の `bytes_total`/`max_bytes`/`clipped`/`truncated`/`errors`/`ms` は
      この既定の例外で、ツール1件ごとにキーが無ければ NULL（Codex 組み込みツールは測っていない
      キーを置かない契約のため・`web_search` は結果の大きさを取れず calls だけのエントリになる）。
      `calls` だけは常に測定されるので 0 既定のまま（`_tool_stat_rows` 参照）。`turn_metrics.
      tool_result_bytes_total`（全ツールの合算）はキーの無いツールを 0 として合算するため
      （`_activity_aggregates` 参照）、合計値自体には影響しない。

      `provider`/`model`/`depth_profile`/`reasoning` は常に `answer["usage"]` から読む
      （新旧どちらの回答でも同じ場所にあり、activity 優先の対象外）。
      `_USAGE_LIMIT_INT_FIELDS`/`_USAGE_LIMIT_BOOL_FIELDS`（打ち切りの内訳12項目）は常に
      `answer["limits"]` から読む（activity のスキーマに含まれないため）。
      `sources_count`/`claims_*`/`claims_unknown_reasons`/`gate_missing_codes`/
      `investigation_*`/`stop_kind`/`codex_error_code`/`duration_ms` も同様に activity を
      経由しない既存の場所（`answer` 直下・`answer["data"]["claims"]`・
      `answer["data"]["evidence_gate"]["missing_codes"]`）から読む。

    本文・タイトル・資料名・ツール引数は列として個別には読まない。`activity_json` だけは
    例外的に activity（§3.1）を丸ごと保持するが、その契約自体に引数・本文・資料名を
    含めないことが上流（S1）の取り決めであり、本関数はそれを信頼して検証しない。
    """
    if not isinstance(answer, dict):
        raise TypeError("metrics_from_answer: answer は dict である必要があります")

    usage = answer.get("usage")
    usage = usage if isinstance(usage, dict) else None
    activity = _activity_dict(answer)
    agents = activity.get("agents") if activity is not None else None

    out: dict = {}

    # ---- 経路・モデル・深さ・推論（常に usage 由来・新旧共通） ----
    out["provider"] = _str_or_none(usage.get("provider")) if usage else None
    out["model"] = _str_or_none(usage.get("model")) if usage else None
    out["depth_profile"] = _str_or_none(usage.get("depth_profile")) if usage else None
    out["reasoning"] = _str_or_none(usage.get("reasoning")) if usage else None

    # ---- アプリの版（activity にしか無い） ----
    out["app_version"] = _str_or_none(activity.get("app_version")) if activity else None

    # ---- 終了理由・エラー・所要 ----
    out["stop_kind"] = _str_or_none(answer.get("stop_kind"))
    out["codex_error_code"] = _str_or_none(answer.get("codex_error_code"))
    out["duration_ms"] = _pos_int_or_none(answer.get("duration_ms"))
    phases = activity.get("phases_ms") if activity else None
    phases = phases if isinstance(phases, dict) else {}
    out["phase_prepare_ms"] = _pos_int_or_none(phases.get("prepare")) if activity else None
    out["phase_agent_ms"] = _pos_int_or_none(phases.get("agent")) if activity else None
    out["phase_post_ms"] = _pos_int_or_none(phases.get("post")) if activity else None

    # ---- トークン4種: 合計・本体（parent）内訳・下調べ役（child）内訳 ----
    breakdown = usage.get("codex_usage_breakdown") if usage else None
    breakdown = breakdown if isinstance(breakdown, dict) else None
    children = answer.get("codex_usage_children")
    children = children if isinstance(children, dict) else None

    if agents is not None:
        totals = _sum_tokens(agents, role=None)
        for k in _TOKEN_KEYS:
            out[k] = totals[k]
        parent_agent = _find_parent_agent(agents)
        if parent_agent is not None:
            pt = _agent_tokens(parent_agent)
            for k in _TOKEN_KEYS:
                out[f"parent_{k}"] = pt[k]
        else:
            for k in _TOKEN_KEYS:
                out[f"parent_{k}"] = None
        ct = _sum_tokens(agents, role="child")
        for k in _TOKEN_KEYS:
            out[f"child_{k}"] = ct[k]
    elif usage is not None:
        for k in _TOKEN_KEYS:
            out[k] = _clamp_int(usage.get(k))
        if breakdown is not None and isinstance(breakdown.get("parent"), dict):
            for k in _TOKEN_KEYS:
                out[f"parent_{k}"] = _clamp_int(breakdown["parent"].get(k))
        else:
            # breakdown が無い＝下調べ役の内訳自体が計測されていない＝usage 全体が本体分。
            for k in _TOKEN_KEYS:
                out[f"parent_{k}"] = out[k]
        if breakdown is not None and isinstance(breakdown.get("children"), dict):
            for k in _TOKEN_KEYS:
                out[f"child_{k}"] = _clamp_int(breakdown["children"].get(k))
        elif children is not None:
            for k in _TOKEN_KEYS:
                out[f"child_{k}"] = _clamp_int(children.get(k))
        else:
            for k in _TOKEN_KEYS:
                out[f"child_{k}"] = None
    else:
        for k in _TOKEN_KEYS:
            out[k] = None
            out[f"parent_{k}"] = None
            out[f"child_{k}"] = None

    # ---- 下調べ役の数（検出・usage 取得・欠落） ----
    # `codex_usage_children`（found/missing の明示的な区別を持つ唯一の場所）を優先する。
    # 無ければ activity の agents 配列から role=='child' の件数を数える——この場合、配列に
    # 載っている＝usage を取得できた分のみなので「欠落」件数は分からず None のまま。
    if children is not None:
        found = _clamp_int(children.get("found"))
        missing = _clamp_int(children.get("missing"))
        out["children_detected"] = found + missing
        out["children_usage_found"] = found
        out["children_usage_missing"] = missing
    elif agents is not None:
        n = sum(1 for a in agents if isinstance(a, dict) and a.get("role") == "child")
        out["children_detected"] = n
        out["children_usage_found"] = n
        out["children_usage_missing"] = None
    else:
        out["children_detected"] = None
        out["children_usage_found"] = None
        out["children_usage_missing"] = None

    # ---- ツール呼出数・結果バイト・API往復数・圧縮回数（activity にしか無い） ----
    if agents is not None:
        tool_calls, tool_bytes, rounds_total, compactions_total = _activity_aggregates(agents)
        out["tool_calls_total"] = tool_calls
        out["tool_result_bytes_total"] = tool_bytes
        out["api_rounds_total"] = rounds_total
        out["compactions_total"] = compactions_total
    else:
        out["tool_calls_total"] = None
        out["tool_result_bytes_total"] = None
        out["api_rounds_total"] = None
        out["compactions_total"] = None

    # ---- 打ち切りの内訳（常に answer["limits"] 由来） ----
    out.update(_limit_fields(answer.get("limits")))

    # ---- 出典件数 ----
    out["sources_count"] = _sources_count(answer)

    # ---- 調査台帳 ----
    out.update(_investigation_fields(answer.get("investigation")))

    # ---- 主張の区分件数・不明の理由コード・最終ゲートの不足コード ----
    data = answer.get("data")
    out.update(_claim_counts(data))
    out["claims_unknown_reasons"] = _claims_unknown_reasons(data)
    out["gate_missing_codes"] = _gate_missing_codes(data)

    # ---- activity 全体（検証済みなら丸ごと保持） ----
    # 詳細画面は本体の往復系列だけでなく下調べ役の系列・エージェント別モデル・圧縮位置・
    # 実際に効いた設定（§2.2）も要るため、個別の列に分解せず activity をそのまま JSONB で持つ
    # （§3.1 の契約により activity 自体に本文・資料名・ツール引数は入らない）。
    out["activity_json"] = activity

    # ---- 写像の由来・版 ----
    out["mapping_source"] = "activity" if activity is not None else "answer_only"
    out["mapping_version"] = MAPPING_VERSION

    return out


def _tool_stats_values_sql(n: int) -> str:
    return ", ".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * n)


def upsert(c, *, message_id: int, conversation_id: int, created_at, lens: str | None,
          personal: bool, answer: dict) -> None:
    """`turn_metrics`/`turn_tool_stats` を1メッセージ分、渡された接続 `c` で書く（呼び出し元の
    トランザクションに乗る・commit/rollback は呼び出し元の責務）。

    `user_message_id`（同じ会話でこの assistant より id が小さい直近の role='user' の id・
    無ければ NULL）はここで1本の SELECT により求める——`store/usage.py::_USAGE_TURN_CTE` と同じ
    「ターン＝user 発言」の対応付けに揃える（1ターンに assistant 行が複数あっても全員が同じ
    user_message_id を指す）。`personal` はこのアシスタント回答自体が個人の資料を使ったか
    （`chat_service._used_personal`）——usage.py の personal_turns（user 発言の personal 列）とは
    別物で、対応する user 発言は user_message_id で引く。

    冪等: `turn_metrics` は `message_id` 主キーの `ON CONFLICT DO UPDATE`。`turn_tool_stats` は
    自然キーが無い（1メッセージに複数行）ため、まず該当 `message_id` を全削除してから
    activity 由来の行を入れ直す（洗い替え＝再実行しても最終状態は同じ）。

    例外はそのまま送出する（呼び出し元が savepoint 等で隔離する契約・`upsert_best_effort` 参照）。
    """
    if not isinstance(answer, dict):
        raise TypeError("turn_metrics.upsert: answer は dict である必要があります")
    conv = c.execute(
        "SELECT user_id, version FROM conversations WHERE id=%s", (conversation_id,)
    ).fetchone()
    if conv is None:
        raise ValueError(f"turn_metrics.upsert: conversation_id={conversation_id} が見つかりません")
    user_row = c.execute(
        "SELECT id FROM messages WHERE conversation_id=%s AND role='user' AND id < %s "
        "ORDER BY id DESC LIMIT 1",
        (conversation_id, message_id),
    ).fetchone()
    user_message_id = user_row["id"] if user_row else None

    metrics = metrics_from_answer(answer)
    row = {
        "message_id": message_id, "conversation_id": conversation_id,
        "user_message_id": user_message_id,
        "user_id": conv["user_id"], "world": conv["version"],
        "created_at": created_at, "lens": lens, "personal": bool(personal),
        **metrics,
    }
    cols = ("message_id", "conversation_id", "user_message_id", "user_id", "world",
            "created_at", "lens", "personal") + _METRIC_COLUMNS
    params = {}
    for col in cols:
        v = row.get(col)
        params[col] = Json(v) if (col in _JSONB_COLUMNS and v is not None) else v
    col_list = ", ".join(cols)
    placeholders = ", ".join(f"%({col})s" for col in cols)
    update_clause = ", ".join(f"{col}=EXCLUDED.{col}" for col in cols if col != "message_id")
    c.execute(
        f"INSERT INTO turn_metrics ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (message_id) DO UPDATE SET {update_clause}",
        params,
    )

    c.execute("DELETE FROM turn_tool_stats WHERE message_id=%s", (message_id,))
    activity = _activity_dict(answer)
    if activity is not None:
        rows = _tool_stat_rows(activity.get("agents") or [])
        if rows:
            flat: list = []
            for r in rows:
                flat.append(message_id)
                flat.extend(r)
            c.execute(
                "INSERT INTO turn_tool_stats (message_id, agent_index, role, tool, calls, "
                "bytes_total, max_bytes, clipped, truncated, errors, ms) "
                f"VALUES {_tool_stats_values_sql(len(rows))}",
                flat,
            )


def upsert_best_effort(c, *, message_id: int, conversation_id: int, created_at, lens: str | None,
                       personal: bool, answer: dict) -> bool:
    """`upsert()` を Postgres SAVEPOINT（`c.transaction()`）で保護して呼ぶ。

    `turn_metrics`/`turn_tool_stats` は回答 JSON から再生成できる派生物であり、書込失敗を
    呼び出し元のトランザクション（本体メッセージの保存等）へ伝播させない——欠けた行は
    `ensure_rows()` が後で埋める。savepoint を使うのは、素の例外握りつぶしだと Postgres 側で
    トランザクションが abort 状態のまま残り、呼び出し元の後続 commit まで失敗するため
    （savepoint への rollback だけがこの汚染を防げる）。

    戻り値: 成功なら True。失敗時は型名・errno のみ warning ログして False（本文・値は出さない）。
    """
    try:
        with c.transaction():
            upsert(c, message_id=message_id, conversation_id=conversation_id,
                  created_at=created_at, lens=lens, personal=personal, answer=answer)
        return True
    except Exception as e:
        _log.warning("turn_metrics 書込に失敗しました（回答の保存は継続・ensure_rows が後で埋めます）"
                    " message_id=%s: %s errno=%s",
                    message_id, type(e).__name__, getattr(e, "errno", None))
        return False


_BACKFILL_BATCH_SIZE = 500   # keyset ページングの1バッチ件数（fetchall で全件を一度に載せない）


def backfill_all() -> int:
    """既存の全 assistant メッセージ（`answer` 有り）を `turn_metrics`/`turn_tool_stats` へ
    一度だけ移す。`id` 昇順の keyset で `_BACKFILL_BATCH_SIZE` 件ずつ処理しバッチごとに commit する
    （大きな messages 表を一括 fetchall/単一トランザクションにしない）。

    冪等: `upsert` が `message_id` 主キーの `ON CONFLICT DO UPDATE`（`turn_tool_stats` は洗い替え）
    のため、複数回実行しても行数・値は変わらない。

    戻り値: 書込に成功した件数（失敗はスキップして次へ進む・`upsert_best_effort` が warning ログ）。
    """
    _ensure()
    processed = 0
    last_id = 0
    with _connect() as c:
        while True:
            rows = c.execute(
                "SELECT id, conversation_id, created_at, lens, personal, answer FROM messages "
                "WHERE role='assistant' AND answer IS NOT NULL AND id > %s "
                "ORDER BY id LIMIT %s",
                (last_id, _BACKFILL_BATCH_SIZE),
            ).fetchall()
            if not rows:
                break
            for r in rows:
                if upsert_best_effort(c, message_id=r["id"], conversation_id=r["conversation_id"],
                                      created_at=r["created_at"], lens=r["lens"],
                                      personal=r["personal"], answer=r["answer"]):
                    processed += 1
                last_id = r["id"]
            c.commit()
    return processed


def ensure_rows(start_ts, end_ts) -> int:
    """`turn_metrics` 行が無い assistant メッセージのうち、`[start_ts, end_ts)` の期間に属する
    ターンを埋める——旧版のアプリで保存された・ライブ書込が失敗した等で欠けたターンを、
    利用統計を開いたときに補う想定（`add_message` は同期で毎回呼ぶ・こちらは補完専用）。

    「期間に属する」は `store/usage.py::_USAGE_TURN_CTE` と同じ基準（**質問＝user 発言の
    created_at**）に揃える契約——回答は必ず質問より後なので、下限（`assistant.created_at >= start`）
    のままで質問が期間内のターンを取りこぼさない。上限は `assistant.created_at < end` **または**
    その assistant の直近の先行 user 発言（`user_message_id` と同じ決め方）の `created_at < end`
    のどちらかが成り立てば対象にする——23:59 の質問に翌日00:01保存された回答のように、質問と
    回答が期間の境をまたぐターンを取りこぼさないため（先行 user が無ければ assistant 自身の
    created_at だけで判定する）。質問が期間よりわずかに前のターンの回答を余分に埋めることは
    あるが、`turn_metrics` は answer から再生成できる派生物なので害はない。

    `backfill_all()` と同じ keyset バッチ方式。戻り値は書込に成功した件数。
    """
    _ensure()
    processed = 0
    last_id = 0
    with _connect() as c:
        while True:
            rows = c.execute(
                "SELECT m.id, m.conversation_id, m.created_at, m.lens, m.personal, m.answer "
                "FROM messages m LEFT JOIN turn_metrics tm ON tm.message_id = m.id "
                "LEFT JOIN LATERAL ("
                "  SELECT u.created_at FROM messages u "
                "  WHERE u.conversation_id = m.conversation_id AND u.role='user' AND u.id < m.id "
                "  ORDER BY u.id DESC LIMIT 1"
                ") um ON true "
                "WHERE m.role='assistant' AND m.answer IS NOT NULL AND tm.message_id IS NULL "
                "  AND m.created_at >= %s AND (m.created_at < %s OR um.created_at < %s) "
                "  AND m.id > %s "
                "ORDER BY m.id LIMIT %s",
                (start_ts, end_ts, end_ts, last_id, _BACKFILL_BATCH_SIZE),
            ).fetchall()
            if not rows:
                break
            for r in rows:
                if upsert_best_effort(c, message_id=r["id"], conversation_id=r["conversation_id"],
                                      created_at=r["created_at"], lens=r["lens"],
                                      personal=r["personal"], answer=r["answer"]):
                    processed += 1
                last_id = r["id"]
            c.commit()
    return processed
