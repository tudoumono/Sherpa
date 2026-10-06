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
_STRUCTURED_KEYS_V2_NO_RECON = _STRUCTURED_KEYS | {"claims"}
_STRUCTURED_KEYS_V2 = _STRUCTURED_KEYS_V2_NO_RECON | {"reconciliation"}
# 設計書とソースの照らし合わせ（`reconciliation`）の1行のキー集合と判定の語彙。
_RECON_KEYS = {"item", "spec_text", "spec_ref", "source_text", "source_ref", "verdict"}
_RECON_VERDICTS = {"match", "mismatch", "spec_missing", "source_missing"}
# `evidence_kinds`（7つ目のキー）は主張1件が実際に開いて確認した根拠の種別（`investigation_state.EVIDENCE_KINDS` の閉集合）。`item_ids`（8つ目のキー）は主張が対応する調査台帳の項目 id の配列。
# 7キー形（`_CLAIM_KEYS_NO_ITEM_IDS`）は `item_ids` 未申告（キー無しのまま返す＝台帳突合は従来どおり evidence_refs だけで照合する）、6キー形（`_CLAIM_KEYS_LEGACY`）はさらに `evidence_kinds=None`（未申告＝最終ゲートは格下げも不足の計上もしない）にする。
_CLAIM_KEYS = {"id", "status", "text", "evidence_refs", "reason", "reason_code", "evidence_kinds", "item_ids"}
_CLAIM_KEYS_NO_ITEM_IDS = _CLAIM_KEYS - {"item_ids"}
_CLAIM_KEYS_LEGACY = _CLAIM_KEYS_NO_ITEM_IDS - {"evidence_kinds"}
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
    `evidence_kinds`（閉集合の list）は7キー以上の形でだけ検証する。6キー形は `evidence_kinds=None` を補って返す（最終ゲートは未申告として扱う）。
    `item_ids`（文字列の list・空可）は8キー形でだけ検証する。それ以外の形は `item_ids` を補わない。
    """
    if not isinstance(item, dict):
        return None
    keys = set(item.keys())
    if keys in (_CLAIM_KEYS, _CLAIM_KEYS_NO_ITEM_IDS):
        if "item_ids" in keys:
            ids_raw = item.get("item_ids")
            if not isinstance(ids_raw, list) or not all(isinstance(i, str) for i in ids_raw):
                return None
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


def _parse_recon_row(item) -> dict | None:
    """照らし合わせの1行を検証する（6キーちょうど・全て文字列・`item` は空でない・`verdict` は閉じた語彙）。不正なら None。"""
    if not isinstance(item, dict) or set(item.keys()) != _RECON_KEYS:
        return None
    if not all(isinstance(item[k], str) for k in _RECON_KEYS):
        return None
    if not item["item"].strip() or item["verdict"] not in _RECON_VERDICTS:
        return None
    return item


def _parse_structured_v2(text: str | None) -> dict | None:
    """v2 出力スキーマ（`claims` を持つ4キー・`reconciliation` を加えた5キー）を検証する。v1 の3キー形（`claims` 無し）も `_parse_structured` と同じ検証で読み、返す dict に `claims: []`・`claims_invalid: 0` を補う（照らし合わせは無し）。
    v2 の4キー形は主張配列の各要素も `_parse_claim` で検証し、不正な要素だけを除いて正しい要素を残す。除いた件数を `claims_invalid` に載せる（`status`/`answer`/`next_step` はそのまま返す＝回答本文を巻き添えにしない）。
    5キー形の `reconciliation` も1行ずつ `_parse_recon_row` で検証し、不正な行だけを除いて件数を `reconciliation_invalid` に載せる。4キー形は `reconciliation: []`・`reconciliation_invalid: 0` を補う。
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
    if set(obj.keys()) not in (_STRUCTURED_KEYS_V2, _STRUCTURED_KEYS_V2_NO_RECON):
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
    recon_raw = obj.get("reconciliation", [])
    if not isinstance(recon_raw, list):
        return None
    recon_rows = []
    recon_invalid = 0
    for row in recon_raw:
        parsed_row = _parse_recon_row(row)
        if parsed_row is None:
            recon_invalid += 1
        else:
            recon_rows.append(parsed_row)
    if recon_invalid:
        _log.warning("codex v2: invalid reconciliation rows dropped=%d kept=%d", recon_invalid, len(recon_rows))
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
    return {**obj, "claims": parsed_claims, "claims_invalid": invalid,
            "reconciliation": recon_rows, "reconciliation_invalid": recon_invalid}


# ---- 設計書とソースの照らし合わせ（`reconciliation`）の検証 ----
# 表に出す行数・1欄の文字数の上限（超えた分は件数だけ伝える）。
_RECON_MAX_ROWS = 40
_RECON_TEXT_MAX = 300
# 表の並び順（食い違い・設計書に記述なし・ソースに見当たらない・未確認を先に、一致を最後に）。
_RECON_ORDER = {"mismatch": 0, "spec_missing": 1, "source_missing": 2, "unverified": 3, "match": 4}
_RECON_REF_LINE = re.compile(r"^(.*?)(?::(\d+)(?:-(\d+))?)?$")
# 行数を数えるときに読む原本の上限（バイト）。超えるものは行番号を確かめられない扱い。
_RECON_LINECOUNT_MAX_BYTES = 20 * 1024 * 1024


def _line_count(doc_id: str, world: str, scope_paths=None) -> int | None:
    """精読ツール（read_doc／read_around）が行番号を振る本文（Office／PDF は変換済みの本文・ソースは原本）の論理行数。数えられない・走査上限で打ち切られたときは None。"""
    from ...parts.read import tools as read_tools
    f, err = read_tools._open_doc_stream(world, doc_id, scope_paths, None)
    if err is not None or f is None:
        return None
    try:
        reader, it, _caution = read_tools._stream_doc_lines(f)
        total = sum(1 for _ in it)
        return None if reader.truncated else total
    except OSError:
        return None
    finally:
        f.close()


def _verify_recon_ref(ref: str, world: str, scope_paths, cache: dict) -> tuple[dict | None, bool]:
    """根拠の参照（`資料のパス:行`）1件を検証する。戻り値 `(参照 {doc_id, line?} か None, 秘匿の資料を指すか)`。空の参照・範囲外・実在しない・秘匿名・重要度設定ファイルは参照 None。`cache` は1ターン内で資料ごとの実在確認・行数を使い回す入れ物。"""
    from ...ingest import importance, text_kind
    ref = ref.strip()
    if not ref:
        return None, False
    m = _RECON_REF_LINE.match(ref)
    path_part, line, line_end = (m.group(1), m.group(2), m.group(3)) if m else (ref, None, None)
    from .citations import normalize_doc_ref, _verify_one
    doc = normalize_doc_ref(path_part, world)
    if doc and text_kind.is_sensitive_doc_id(doc):
        return None, True
    if doc and importance.is_importance_control_path(doc):
        return None, False
    if ("v", path_part) not in cache:
        cache[("v", path_part)] = _verify_one(path_part, world, scope_paths)
    verified = cache[("v", path_part)]
    if not verified:
        return None, False
    out = {"doc_id": verified}
    if line:
        first, last = int(line), int(line_end or line)
        if ("n", verified) not in cache:
            cache[("n", verified)] = _line_count(verified, world, scope_paths)
        total = cache[("n", verified)]
        if total is None or not 1 <= first <= last <= total:
            return None, False
        out["line"] = first
        if last > first:
            out["line_end"] = last
    return out, False


def verify_reconciliation(rows: list[dict], world: str, scope_paths) -> tuple[list[dict], dict]:
    """照らし合わせの行ごとに、設計書側・ソース側の根拠が範囲内に実在し秘匿でないかを確かめる（主張の根拠の検証と同じ流儀）。
    確かめられない参照は参照も記述も消し、欠けている側（設計書に記述なし・ソースに見当たらない）は参照・記述とも空に固定する。行番号（範囲）が原本の行数の中にあることも確かめる。その判定が成り立たない行は `unverified`（未確認）に下げる。
    戻り値 `(表に出す行（並べ替え・上限適用済み）, {"unverified": 下げた件数, "more": 上限で省いた件数})`。行は `{item, verdict, spec: {text, doc_id?, line?, line_end?}, source: {...}}`。
    """
    # 上限は検証の前にかける（申告された判定の順に並べて先頭だけ検証する）。検証で未確認に下がった行は、残した行の中で並べ直す。
    ordered = sorted(rows, key=lambda r: _RECON_ORDER[r["verdict"]])
    more = max(0, len(ordered) - _RECON_MAX_ROWS)
    cache: dict = {}
    out = []
    unverified = 0
    for r in ordered[:_RECON_MAX_ROWS]:
        verdict = r["verdict"]
        need = {"match": ("spec", "source"), "mismatch": ("spec", "source"),
                "spec_missing": ("source",), "source_missing": ("spec",)}[verdict]
        sides = {}
        for key in ("spec", "source"):
            if key not in need:
                sides[key] = {"text": ""}
                continue
            ref, _sensitive = _verify_recon_ref(r[f"{key}_ref"], world, scope_paths, cache)
            text = r[f"{key}_text"].strip()[:_RECON_TEXT_MAX]
            # 行番号つきで確かめられ、記述も空でない側だけ採る（それ以外は丸ごと空）。
            sides[key] = {"text": text, **ref} if ref and "line" in ref and text else {"text": ""}
        if any("doc_id" not in sides[k] for k in need):
            verdict = "unverified"
            unverified += 1
        out.append({"item": r["item"].strip()[:_RECON_TEXT_MAX], "verdict": verdict, **sides})
    out.sort(key=lambda x: _RECON_ORDER[x["verdict"]])
    return out, {"unverified": unverified, "more": more}


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


def _ledger_item_locations(snapshot: investigation_ledger.LedgerSnapshot) -> dict[str, set]:
    """台帳の manifest に登録され、確認済みの状態（`EVIDENCE_REQUIRED_STATUSES`）の item だけを、`id → {(正規化 path, line)}`（その item の `evidence`）にまとめる。未登録 item・`unverified`／`unreadable`／`unavailable`／未完了の item は含めない。manifest が無い／`items` が空なら空 dict。"""
    manifest_ids = set(snapshot.manifest["items"]) if snapshot.manifest is not None else set()
    out: dict[str, set] = {}
    for item_id, item in snapshot.items.items():
        if item_id not in manifest_ids or item.get("status") not in investigation_ledger.EVIDENCE_REQUIRED_STATUSES:
            continue
        out[item_id] = {(_normalize_evidence_path(ev["path"]), ev["line"]) for ev in item.get("evidence") or []}
    return out


# `_claims_vs_ledger` が confirmed を推定へ格下げするときの理由文の先頭に付ける固定文（既存の `reason` があれば括弧書きで残す）。
_LEDGER_UNMATCHED_REASON = "根拠が調査台帳に無いため確定できません"
# `item_ids` を持つ主張の格下げ理由（項目の指定なし／確認済みでない・未登録の項目を指す／別の項目の根拠を使っている）。
_LEDGER_NO_ITEM_REASON = "対応する調査項目が示されていないため確定できません"
_LEDGER_ITEM_UNKNOWN_REASON = "対応する調査項目が調査台帳の確認済みの項目に無いため確定できません"
_LEDGER_ITEM_MISMATCH_REASON = "根拠が対応する調査項目の根拠と一致しない（別の項目の根拠）ため確定できません"


def _demote_for_ledger(c: dict, base_reason: str) -> dict:
    """confirmed の主張を推定へ下げる（既存の `reason` があれば括弧書きで残す）。"""
    reason = (c.get("reason") or "").strip()
    return {**c, "status": "inferred", "reason": f"{base_reason}（{reason}）" if reason else base_reason,
            "reason_code": ""}


def _claims_vs_ledger(claims: list[dict], snapshot: investigation_ledger.LedgerSnapshot, *,
                      manifest_file_exists: bool) -> tuple[list[dict], dict]:
    """最終回答の `claims` を調査台帳と突き合わせる（claims は台帳からの投影）。
    `evidence_refs` が空、または1件でも台帳の確認済み item の `evidence`（`path`/`line`）に一致しない confirmed 主張は、推定（inferred）へ格下げする（`status`/`reason`/`reason_code` を書き換える）。一部だけ一致する confirmed も格下げする（全 refs が一致したときだけ維持）。`inferred`／`unknown` は対象外。
    主張が `item_ids`（対応する台帳項目の id・8キー形）を持つときは、confirmed を次の条件で維持する: `item_ids` が非空で、全てが確認済みの登録済み item を指し、全 `evidence_refs` がそれらの item の `evidence` に一致する。`item_ids` が空・確認済みでない／未登録の item を指す・refs が別の item の根拠にだけ一致する（取り違え）主張は推定へ下げ、理由を `reason` に付ける。`item_ids` を持たない主張（7キー以前の形）は従来どおり全 item の evidence 集合で照合する。
    すべての主張が `item_ids` を持つときだけ、確認済みの登録済み item のうちどの主張の `item_ids` にも現れないものを `omitted_items`（項目名＝件名か id の配列・秘匿名は除き `omitted_hidden` に件数）に載せる。
    `manifest_file_exists`（`(investigation_dir / "manifest.json").is_file()`）で `snapshot.manifest is None` の原因を区別する:
    - ファイルが無い＝台帳を作らなかった依頼: 対応関係を適用せず `claims` を無変更で返す。
    - ファイルはあるが内容不正／symlink＝壊れた台帳: 登録集合を空として扱い、confirmed を全て格下げする。
    戻り値: `(格下げ済みの claims コピー, 統計 dict)`。`manifest_state` は `"absent"`／`"invalid"`／`"valid"`。`absent` は `{"ledger": False, "manifest_state": "absent"}`、それ以外は `{"ledger": True, "checked": 検査した confirmed 件数, "downgraded": 格下げ件数, "unmatched_refs": 台帳に一致しなかった evidence_refs の延べ件数, "manifest_state": ...}`（すべての主張が `item_ids` を持つときだけ `omitted_items`／`omitted_hidden` を足す）。
    """
    if not manifest_file_exists:
        return claims, {"ledger": False, "manifest_state": "absent"}
    manifest_state = "valid" if snapshot.manifest is not None else "invalid"
    per_item = _ledger_item_locations(snapshot)
    locations = set().union(*per_item.values()) if per_item else set()
    checked = downgraded = unmatched_refs = 0
    referenced: set[str] = set()
    uses_item_ids = False
    out = []
    for c in claims:
        ids_raw = c.get("item_ids")
        ids = [i.strip() for i in ids_raw if i.strip()] if ids_raw is not None else None
        if ids is not None:
            uses_item_ids = True
            referenced.update(ids)
        if c.get("status") != "confirmed":
            out.append(c)
            continue
        checked += 1
        refs = c.get("evidence_refs") or []
        if ids is None:
            allowed = locations
        else:
            allowed = set().union(*(per_item[i] for i in ids if i in per_item)) if ids else set()
        unmatched = sum(1 for ref in refs if _parse_evidence_ref(ref) not in allowed)
        unmatched_refs += unmatched
        base = None
        if ids is not None and not ids:
            base = _LEDGER_NO_ITEM_REASON
        elif ids is not None and any(i not in per_item for i in ids):
            base = _LEDGER_ITEM_UNKNOWN_REASON
        elif not refs or unmatched:
            mixed_up = ids is not None and all(_parse_evidence_ref(ref) in locations for ref in refs)
            base = _LEDGER_ITEM_MISMATCH_REASON if mixed_up and refs else _LEDGER_UNMATCHED_REASON
        if base is not None:
            c = _demote_for_ledger(c, base)
            downgraded += 1
        out.append(c)
    stats = {"ledger": True, "checked": checked, "downgraded": downgraded,
             "unmatched_refs": unmatched_refs, "manifest_state": manifest_state}
    # ① 全主張が item_ids を持つときだけ数える ② 秘匿名は名前を残さず件数にする。
    if uses_item_ids and all(c.get("item_ids") is not None for c in claims):
        from ...ingest import text_kind
        names = [(snapshot.items[i].get("subject") or "").strip() or i for i in sorted(per_item) if i not in referenced]
        shown = [n for n in names if not text_kind.is_sensitive_doc_id(n)]
        stats["omitted_items"] = shown
        if len(names) > len(shown):
            stats["omitted_hidden"] = len(names) - len(shown)
    return out, stats


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
