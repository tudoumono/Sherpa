"""構造化された最終応答（`--output-schema` の v1/v2）の検証と、主張の根拠種別・調査台帳との突合。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import json
import re

from ... import investigation_ledger
from ... import investigation_state
from ..base import _KINDS_OUTSIDE_LEDGER, _log, _scope_evidence_kinds


# ---- 出力スキーマ（`--output-schema`）----
# `_OUTPUT_SCHEMA_PATH` の3キーちょうど（strict・additionalProperties: false）と対応させる。
_STRUCTURED_KEYS = {"status", "answer", "next_step"}
_STRUCTURED_STATUSES = {"final", "in_progress"}
# v2（output_schema_v2.json）の4キーちょうど・主張1件の閉じたキー集合と語彙。
_STRUCTURED_KEYS_V2 = _STRUCTURED_KEYS | {"claims"}
# `evidence_kinds`（7つ目のキー）は主張1件が実際に開いて確認した根拠の種別（`investigation_state.EVIDENCE_KINDS` の閉集合）。6キー形（`_CLAIM_KEYS_LEGACY`）も受理し、その場合は `evidence_kinds=None`（未申告＝最終ゲートは格下げも不足の計上もしない）にする。
_CLAIM_KEYS = {"id", "status", "text", "evidence_refs", "reason", "reason_code", "evidence_kinds"}
_CLAIM_KEYS_LEGACY = _CLAIM_KEYS - {"evidence_kinds"}
_CLAIM_STATUSES = {"confirmed", "inferred", "unknown"}
_CLAIM_UNKNOWN_REASON_CODES = {
    "not_found_in_scope", "unexplored", "insufficient", "conflict", "budget", "unreadable"}


def _parse_structured(text: str | None) -> dict | None:
    """`--output-schema` で固定した3キー JSON かどうかを検証する（純関数）。
    キー集合の完全一致・`status` の値・`answer`/`next_step` の型が全て合うときだけ dict を返す。それ以外（構文エラー・途中で切れた JSON・キーの過不足・不正な型・不正な status 値）は None（呼び出し側は「未完了」として扱う）。
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or set(obj.keys()) != _STRUCTURED_KEYS:
        return None
    # 先に str 型を確認してから集合照合する（非 hashable 値で TypeError にしない）。
    _status = obj.get("status")
    if not isinstance(_status, str) or _status not in _STRUCTURED_STATUSES:
        return None
    if not isinstance(obj.get("answer"), str):
        return None
    _next = obj.get("next_step")
    if _next is not None and not isinstance(_next, str):
        return None
    return obj


def _parse_claim(item) -> dict | None:
    """v2 の主張1件を検証する（`investigation_state.parse_claims` と同じ規約: キー集合の完全一致・`status` の閉じた語彙・`unknown` だけ `reason_code` を閉じた語彙から要求）。不正なら None（呼び出し元は主張配列全体を無効とする）。
    confirmed には非空の `evidence_refs` を要求する（参照の実在チェックはしない）。inferred は空白のみでない `reason` を必須にする。
    `evidence_kinds`（閉集合の list）は7キー形でだけ検証する。6キー形は `evidence_kinds=None` を補って返す（最終ゲートは未申告として扱う）。
    """
    if not isinstance(item, dict):
        return None
    keys = set(item.keys())
    if keys == _CLAIM_KEYS:
        kinds_raw = item.get("evidence_kinds")
        if not isinstance(kinds_raw, list) or not all(
                isinstance(k, str) and k in investigation_state.EVIDENCE_KINDS for k in kinds_raw):
            return None
        evidence_kinds = kinds_raw
    elif keys == _CLAIM_KEYS_LEGACY:
        evidence_kinds = None
    else:
        return None
    cid, status, text = item.get("id"), item.get("status"), item.get("text")
    if not isinstance(cid, str) or not cid.strip():
        return None
    if not isinstance(status, str) or status not in _CLAIM_STATUSES:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    refs = item.get("evidence_refs")
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        return None
    reason = item.get("reason")
    if not isinstance(reason, str):
        return None
    reason_code = item.get("reason_code")
    if not isinstance(reason_code, str):
        return None
    if status == "unknown":
        if reason_code not in _CLAIM_UNKNOWN_REASON_CODES:
            return None
    elif reason_code:
        return None
    if status == "confirmed" and not any(r.strip() for r in refs):
        return None
    if status == "inferred" and not reason.strip():
        return None
    return {**item, "evidence_kinds": evidence_kinds}


def _parse_structured_v2(text: str | None) -> dict | None:
    """v2 出力スキーマ（`claims` を持つ4キー）を検証する。v1 の3キー形（`claims` 無し）も `_parse_structured` と同じ検証で読み、返す dict に `claims: []`・`claims_invalid: 0` を補う。
    v2 の4キー形は主張配列の各要素も `_parse_claim` で検証し、不正な要素だけを除いて正しい要素を残す。除いた件数を `claims_invalid` に載せる（`status`/`answer`/`next_step` はそのまま返す＝回答本文を巻き添えにしない）。
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if set(obj.keys()) == _STRUCTURED_KEYS:
        v1 = _parse_structured(text)
        if v1 is None:
            return None
        return {**v1, "claims": [], "claims_invalid": 0}
    if set(obj.keys()) != _STRUCTURED_KEYS_V2:
        return None
    _status = obj.get("status")
    if not isinstance(_status, str) or _status not in _STRUCTURED_STATUSES:
        return None
    if not isinstance(obj.get("answer"), str):
        return None
    _next = obj.get("next_step")
    if _next is not None and not isinstance(_next, str):
        return None
    claims = obj.get("claims")
    if not isinstance(claims, list):
        return None
    parsed_claims = []
    invalid = 0
    for item in claims:
        parsed = _parse_claim(item)
        if parsed is None:
            invalid += 1
            continue
        parsed_claims.append(parsed)
    if invalid:
        _log.warning("codex v2: invalid claims dropped=%d kept=%d (answer kept)", invalid, len(parsed_claims))
    return {**obj, "claims": parsed_claims, "claims_invalid": invalid}


# Codex は自分の MCP/直読の履歴を残さないため、主張自身が申告する `evidence_kinds`（`_parse_claim` が検証）を根拠種別の唯一の入力にする。
# レンズ別必須種別・「範囲に無い」の除外・確定の格下げ文言は API 側と同じ語彙・文言（`investigation_state.demote_reason_for_missing_kinds`）を使う。
def _apply_codex_evidence_gate(claims: list[dict], *, lens: str, world: str, scope_paths,
                               layer, personal_facts: str) -> tuple[list[dict], dict, tuple, tuple]:
    """確定主張のうち必須の根拠種別を欠くものを推定へ格下げし、ターン単位の不足も併せて返す。
    戻り値: `(格下げ済みの claims コピー, envelope 用 evidence_gate meta, ターン単位の不足種別, 範囲に無い種別)`。後2つは headline 注記（`_evidence_gate_note`）用の生の種別名。
    `unavailable`: 登録範囲にその種別が存在しない（該当なし＝不足に数えない）。
    `personal_facts` があれば `log_config` は主張単位・ターン単位の両方で充足済みとして扱う。
    meta の `applied`: 未申告（`evidence_kinds is None`）の主張が無く、ターン単位の判定を確定的に行えたか（1件でも未申告があれば `missing_codes` は空のまま `applied=False`）。
    meta の `demoted`: 格下げした主張の件数。呼び出し側はこの件数で「一部の主張は推定に留めた」注記を前置する。
    """
    required_all = investigation_state.required_evidence_kinds(lens)
    scope_kinds = _scope_evidence_kinds(world, scope_paths, layer)
    unavailable = tuple(
        k for k in required_all
        if scope_kinds is not None and k not in scope_kinds[0]
        and not (k in _KINDS_OUTSIDE_LEDGER and personal_facts))
    required = tuple(k for k in required_all if k not in unavailable)
    outside_ledger_seen = set(_KINDS_OUTSIDE_LEDGER) if personal_facts else set()
    declared_union: set = set(outside_ledger_seen)
    any_undeclared = False
    demoted = 0
    out = []
    for c in claims:
        kinds = c.get("evidence_kinds")
        if kinds is None:
            any_undeclared = True
            out.append(c)
            continue
        declared_union |= set(kinds)
        lacking = [k for k in required if k not in kinds and k not in outside_ledger_seen]
        if c.get("status") == "confirmed" and lacking:
            c = {**c, "status": "inferred",
                 "reason": investigation_state.demote_reason_for_missing_kinds(lacking),
                 "reason_code": ""}
            demoted += 1
        out.append(c)
    turn_missing = (() if any_undeclared
                    else tuple(k for k in required if k not in declared_union))
    meta = {"missing_codes": [investigation_state.missing_code_for_kind(k) for k in turn_missing],
            "unavailable": list(unavailable), "applied": not any_undeclared, "demoted": demoted}
    return out, meta, turn_missing, unavailable


def _normalize_evidence_path(path: str) -> str:
    """区切りを `/` に統一し先頭の `./` を除く（台帳 item の `evidence.path` と claim の `evidence_refs` の表記ゆれを吸収する）。"""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _parse_evidence_ref(ref) -> tuple[str, int] | None:
    """claim の `evidence_refs` 1件（`"path:line"` 形式）を `(正規化 path, line)` に分解する。形式に合わなければ `None`（台帳のどの `evidence` とも一致しない扱い・例外は投げない）。"""
    if not isinstance(ref, str):
        return None
    idx = ref.rfind(":")
    if idx <= 0 or idx == len(ref) - 1:
        return None
    path_part, line_part = ref[:idx], ref[idx + 1:]
    try:
        line = int(line_part)
    except ValueError:
        return None
    return (_normalize_evidence_path(path_part), line)


def _ledger_evidence_locations(snapshot: investigation_ledger.LedgerSnapshot) -> set:
    """台帳の manifest に登録され、確認済みの状態（`EVIDENCE_REQUIRED_STATUSES`）の item だけの `evidence` を `(正規化 path, line)` の集合にまとめる。未登録 item・`unverified`／`unreadable`／`unavailable`／未完了の item の evidence は裏付けとして採用しない。manifest が無い／`items` が空なら集合は空（全 confirmed が格下げされる）。"""
    manifest_ids = set(snapshot.manifest["items"]) if snapshot.manifest is not None else set()
    locations = set()
    for item_id, item in snapshot.items.items():
        if item_id not in manifest_ids or item.get("status") not in investigation_ledger.EVIDENCE_REQUIRED_STATUSES:
            continue
        for ev in item.get("evidence") or []:
            locations.add((_normalize_evidence_path(ev["path"]), ev["line"]))
    return locations


# `_claims_vs_ledger` が confirmed を推定へ格下げするときの理由文の先頭に付ける固定文（既存の `reason` があれば括弧書きで残す）。
_LEDGER_UNMATCHED_REASON = "根拠が調査台帳に無いため確定できません"


def _claims_vs_ledger(claims: list[dict], snapshot: investigation_ledger.LedgerSnapshot, *,
                      manifest_file_exists: bool) -> tuple[list[dict], dict]:
    """最終回答の `claims` を調査台帳と突き合わせる（claims は台帳からの投影）。
    `evidence_refs` が空、または1件でも台帳の確認済み item の `evidence`（`path`/`line`）に一致しない confirmed 主張は、推定（inferred）へ格下げする（`status`/`reason`/`reason_code` を書き換える）。一部だけ一致する confirmed も格下げする（全 refs が一致したときだけ維持）。`inferred`／`unknown` は対象外。
    `manifest_file_exists`（`(investigation_dir / "manifest.json").is_file()`）で `snapshot.manifest is None` の原因を区別する:
    - ファイルが無い＝台帳を作らなかった依頼: 対応関係を適用せず `claims` を無変更で返す。
    - ファイルはあるが内容不正／symlink＝壊れた台帳: 登録集合を空として扱い、confirmed を全て格下げする。
    戻り値: `(格下げ済みの claims コピー, 統計 dict)`。`manifest_state` は `"absent"`／`"invalid"`／`"valid"`。`absent` は `{"ledger": False, "manifest_state": "absent"}`、それ以外は `{"ledger": True, "checked": 検査した confirmed 件数, "downgraded": 格下げ件数, "unmatched_refs": 台帳に一致しなかった evidence_refs の延べ件数, "manifest_state": ...}`。
    """
    if not manifest_file_exists:
        return claims, {"ledger": False, "manifest_state": "absent"}
    manifest_state = "valid" if snapshot.manifest is not None else "invalid"
    locations = _ledger_evidence_locations(snapshot)
    checked = downgraded = unmatched_refs = 0
    out = []
    for c in claims:
        if c.get("status") != "confirmed":
            out.append(c)
            continue
        checked += 1
        refs = c.get("evidence_refs") or []
        unmatched = sum(1 for ref in refs if _parse_evidence_ref(ref) not in locations)
        unmatched_refs += unmatched
        if not refs or unmatched:
            reason = (c.get("reason") or "").strip()
            new_reason = (f"{_LEDGER_UNMATCHED_REASON}（{reason}）" if reason
                         else _LEDGER_UNMATCHED_REASON)
            c = {**c, "status": "inferred", "reason": new_reason, "reason_code": ""}
            downgraded += 1
        out.append(c)
    return out, {"ledger": True, "checked": checked, "downgraded": downgraded,
                "unmatched_refs": unmatched_refs, "manifest_state": manifest_state}


# 格下げは起きたがターン全体では種別が揃っている（不足の注記が出ない）ときの前置文（本文は書き換えない）。
_DEMOTED_CLAIMS_NOTE = "一部の内容は必要な根拠の種別が揃っていないため、確定ではなく推定として扱っています。"

# 主張の一部が形式不正で除かれたターンの前置文（残った主張は通常どおり検証される）。
_INVALID_CLAIMS_NOTE = "根拠の一部を確認できませんでした。"


# 最終回答の先頭段落が「点検・答え直しの経緯」だけの前置きのとき、その段落を取り除く（指示文での抑止が破れたときの決定的な守り）。
_PREAMBLE_MARKERS = ("答え直し", "前回の回答", "前回回答")
_PREAMBLE_MAX_CHARS = 400
_PREAMBLE_STRUCTURE = re.compile(
    r"^\s*(?:[-*・•]\s|\d+[.)）]\s|\|)|```|参照した資料|\S:\d+", re.MULTILINE)


def _is_review_preamble(paragraph: str) -> bool:
    """点検で始まり、答え直し・前回の回答の語を含む短い 1 段落で、表・箇条書き・コード・ファイル:行・参照資料の記載を含まないときだけ真。"""
    text = paragraph.strip()
    if not text or len(text) > _PREAMBLE_MAX_CHARS or not text.startswith("点検"):
        return False
    if _PREAMBLE_STRUCTURE.search(text):
        return False
    return any(m in text for m in _PREAMBLE_MARKERS)


def split_review_preamble(answer: str) -> tuple[str, str]:
    """回答の先頭 1 段落（最初の空行まで）が点検の前置きで、取り除いても本文が残るときだけ (残りの本文, 前置き) に分ける。それ以外・判断できないときは (原文, 空文字)。"""
    if not isinstance(answer, str):
        return answer, ""
    head, sep, rest = answer.lstrip().partition("\n\n")
    if not sep or not _is_review_preamble(head):
        return answer, ""
    # 残りが「参照した資料」のブロックだけ（実際の本文が空）なら除かない。
    if not rest.split("参照した資料", 1)[0].strip():
        return answer, ""
    _log.info("codex answer: leading review preamble removed chars=%d", len(answer) - len(rest.lstrip()))
    return rest.lstrip("\n"), head


def strip_review_preamble(answer: str) -> str:
    """点検の前置きを除いた本文（除いた段落は `split_review_preamble` が返す）。"""
    return split_review_preamble(answer)[0]
