"""調査台帳の純関数部分（台帳の read/write/判定）。台帳は 1 ターンの作業台帳で、業務事実の正本はソース、利用者への回答の正本は `messages.answer`。
設計: docs/design/codex.md「調査台帳と回答前の関門」

契約:
- 標準ライブラリのみに依存し、他の sherpa モジュールを import しない（葉ノード）。アトミック書込は tmp→`os.replace` をこのモジュール内で独立実装する。
- item はキー集合が正規形 8 キー（`id`/`kind`/`subject`/`required_checks`/`evidence`/`status`/`reason`/`owner`）と完全一致する。
  `evidence` の各要素は `{"kind", "path", "line"}`（`kind`/`path` は str・`line` は int）と完全一致し、資料本文は台帳に書かない。
  `subject`/`reason` には上限文字数（`SUBJECT_MAX_LEN`/`REASON_MAX_LEN`）がある。manifest は `question_kind`/`created_at`/`items` と完全一致する（`_read_manifest`）。
- 壊れた JSON・型不一致・キー欠落/余分・読めないファイルは例外を投げず、無効/欠落として記録する（判定は `ledger_complete()`）。
  `write_item_atomic`/`write_manifest_atomic` の `id` 安全性チェックだけ `ValueError`（パス逃れ防止）。
- 壊れた・無い・空の manifest からは `complete=True` を作らない（`ledger_complete()` は有効な manifest と非空の `items` を必須にする）。
- 終端 item は理由・根拠なしで確定できない。`REASON_REQUIRED_STATUSES` は `reason.strip()` が空なら無効、`EVIDENCE_REQUIRED_STATUSES` は `evidence` が空配列なら無効。
- `required_checks` は `EVIDENCE_KINDS`（`source`/`spec_doc`/`definition`/`log_config`/`callgraph`）の閉集合で、語彙外・重複・空配列は無効。
  `EVIDENCE_REQUIRED_STATUSES` の 3 状態は、その状態が要求する根拠種別（`source_confirmed`→`source`／`spec_only`→`spec_doc`／`conflict`→両方）が `evidence` に無ければ無効。
  さらに宣言した全種別の `evidence` が揃っていなければ、item は有効でも未充足として `ledger_complete()` が完了を妨げる（`Verdict.unsatisfied`）。
- `ledger_complete()`/`no_progress()` は `required_extra`（キーワード専用・既定は空タプル）を受ける。呼び出し側（provider.py）が追加で必須にする根拠種別で、`required_checks` との和集合として効き、対象は `EVIDENCE_REQUIRED_STATUSES` の item だけ。
- `load_ledger()` は symlink を一切辿らない（台帳は model-shell が書ける領域にあり、リンク先を親権限で読むと漏れる）。`dir`／`manifest.json`／`items/`／各 `items/*.json` は読む前に symlink 判定し、1 つでも symlink ならその要素は無効にする。
- 中間の見直し: `dir/reviews.jsonl` に、完了判定の前に最低 1 回必要な「目的・観点の見直し」を 1 行 1 件で追記する（`append_review_atomic`）。
  1 行の正規形は `validate_review_entry()` が検証する。各欄に上限文字数・配列件数の上限がある。
  `ledger_complete()` は `require_review=True` のときだけ、有効な見直しが無い（`review_missing`）・見直しが `added_items` に挙げた item が終端でない（`review_pending_ids`）なら `complete=False` にする。既定は `False`。
"""
from __future__ import annotations

import json
import errno
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# 非終端＝まだ検証していない／検証中。終端＝検証結果が確定した状態。未知の状態文字列はどちらにも属さず、item は無効になる。
NON_TERMINAL_STATUSES: frozenset[str] = frozenset({"pending", "in_progress"})
# `unverified`: 「確認できなかった」ことを理由付きの終端として明示する状態。Sherpa が coverage.jsonl の記録に基づき機械的に置き換えることがある（`apply_unverified_downgrades`）。モデルが直接使ってもよい（`reason` は `UNVERIFIED_REASON_CODES` の閉じた語彙のみ）。
TERMINAL_STATUSES: frozenset[str] = frozenset({
    "source_confirmed",
    "spec_only",
    "conflict",
    "not_found_in_scope",
    "unreadable",
    "unavailable",
    "unverified",
})

# 「確認不能・範囲外・読取不能」は理由付きの終端でなければならない。`reason.strip()` が空なら無効。`unverified` は `reason` が `UNVERIFIED_REASON_CODES` の語彙でなければならない（`validate_item`）。
REASON_REQUIRED_STATUSES: frozenset[str] = frozenset({
    "not_found_in_scope", "unreadable", "unavailable", "unverified"})
# 「確認済み」系の終端は根拠（evidence）1 件以上が必須。
EVIDENCE_REQUIRED_STATUSES: frozenset[str] = frozenset({"source_confirmed", "spec_only", "conflict"})

# `unverified` の `reason` が取れる値の閉じた語彙（自由記述にすると、provider.py 側の「確認できなかった項目」節が理由を利用者向け文へ変換できない）。
# - search_truncated: 検索がヒット数上限／件数上限で打ち切られた。
# - search_error: 検索・グラフの道具そのものが失敗した。
# - no_hits_only: 0 件の検索しか行っていない（モデルが自己判断で使う理由で、Sherpa 側の機械的な降格はこの理由を選ばない）。
# - timeout: 検索・読取が時間切れになった。
# - unreadable: 検索・読取の対象が読み取れなかった。
# - not_searched: この item に対する item 付きの検索・読取が 1 回も行われていない。
UNVERIFIED_REASON_CODES: frozenset[str] = frozenset({
    "search_truncated", "search_error", "no_hits_only", "timeout", "unreadable", "not_searched"})

# 回答末尾の「確認できなかった項目」の節に集める終端状態の閉集合。`not_found_in_scope` は降格されていなくても利用者からは「確認できなかった」に見えるため常に含める。
UNCONFIRMED_STATUSES: frozenset[str] = frozenset({
    "unverified", "not_found_in_scope", "unreadable", "unavailable"})

# 台帳が扱う根拠種別の閉集合（`investigation_state.EVIDENCE_KINDS` と同じ語彙・葉ノード原則のため独立定義）。`required_checks` の各要素はこの外に出られない。
EVIDENCE_KINDS: tuple[str, ...] = ("source", "spec_doc", "definition", "log_config", "callgraph")
_EVIDENCE_KINDS_SET: frozenset[str] = frozenset(EVIDENCE_KINDS)

# `EVIDENCE_REQUIRED_STATUSES` の各状態が要求する根拠種別。`evidence` の `kind` に無ければ item 自体が無効。`conflict` はソースと設計書の両方が要る。
_STATUS_REQUIRED_EVIDENCE_KINDS: dict[str, frozenset[str]] = {
    "source_confirmed": frozenset({"source"}),
    "spec_only": frozenset({"spec_doc"}),
    "conflict": frozenset({"source", "spec_doc"}),
}

# item の正規形＝キー集合がこの 8 つと完全一致（余分なキー・欠落はどちらも無効）。
_ITEM_REQUIRED_KEYS = frozenset({"id", "kind", "subject", "required_checks", "evidence", "status", "reason", "owner"})
# evidence 1 要素の正規形＝キー集合がこの 3 つと完全一致（本文は含めない）。
_EVIDENCE_REQUIRED_KEYS = frozenset({"kind", "path", "line"})
# manifest の正規形＝キー集合がこの 3 つと完全一致。
_MANIFEST_REQUIRED_KEYS = frozenset({"question_kind", "created_at", "items"})

# `subject`/`reason` の上限文字数（超過は無効）。
SUBJECT_MAX_LEN = 2000
REASON_MAX_LEN = 2000

_MANIFEST_FILENAME = "manifest.json"
_ITEMS_DIRNAME = "items"


@dataclass(frozen=True)
class LedgerSnapshot:
    """`load_ledger()` の戻り値（読んだだけの状態・判定は行わない）。
    `manifest`: 読めた manifest。無い／壊れている／`items` が文字列の配列でない場合は `None`。
    `items`: 有効な item のみ（id＝ファイル名の stem をキーとする辞書）。
    `invalid_ids`: 壊れている/無効な item の id（昇順）。
    """

    manifest: dict | None
    items: dict[str, dict]
    invalid_ids: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    """`ledger_complete()` の戻り値。
    `manifest_invalid`: manifest が無い／壊れている／`items` が空なら `True`。`True` の間は `complete=False`。
    id は原則 `non_terminal_ids`／`invalid_ids`／`missing_ids`／`unregistered_ids` のいずれか 1 つだけに現れる（登録集合に無い id は `unregistered_ids` にだけ現れる）。例外は `unsatisfied` の id で、`non_terminal_ids` にも重ねて現れる。
    `unsatisfied`: `EVIDENCE_REQUIRED_STATUSES` の登録済み item のうち、`required_checks` と `required_extra` の和集合の一部が `evidence` に無い id → 足りない種別（ソート済み）。内訳の報告専用で、継続判定・無進捗判定は `non_terminal_ids` 経由で効く。
    `review_missing`/`review_pending_ids`: `require_review=True` のときだけ意味を持つ。
      `review_missing` は有効な見直しが 1 件も無い、または `require_continuation_resolved=True` で `pending_continuation_review(reviews)` が非 `None`（未解決の「追加の観点」の義務が残る）こと。
      `review_pending_ids` は、見直しが `added_items` に挙げた item id のうち、登録集合に無い・item が無い/無効・終端でないもの（昇順）。どちらか一方でも真/非空なら `complete=False`。
    """

    complete: bool
    non_terminal_ids: tuple[str, ...]
    invalid_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    unregistered_ids: tuple[str, ...]
    unsatisfied: dict[str, tuple[str, ...]]
    terminal_counts: dict[str, int]
    manifest_invalid: bool
    review_missing: bool = False
    review_pending_ids: tuple[str, ...] = ()


def _is_symlink_fail_closed(path: Path) -> bool:
    """`path.is_symlink()` の fail-closed 版。判定自体が失敗したら「疑わしい symlink」として `True` を返す。"""
    try:
        return path.is_symlink()
    except OSError:
        return True


def _read_json_safe(path: Path):
    """JSON を安全に読む。無い/壊れ/IO エラーは `None`。"""
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def validate_manifest(manifest) -> list[str]:
    """manifest の正規形（キー集合が `question_kind`/`created_at`/`items` と完全一致・前 2 者は非空 str・`items` は文字列の配列）を検証し、問題を人が読める理由の文字列のリストで返す（無ければ空）。"""
    if not isinstance(manifest, dict):
        return ["manifest は dict である必要がある"]
    extra_keys = set(manifest.keys()) - _MANIFEST_REQUIRED_KEYS
    missing_keys = _MANIFEST_REQUIRED_KEYS - set(manifest.keys())
    if extra_keys or missing_keys:
        problems: list[str] = []
        if extra_keys:
            problems.append(f"manifest に余分なキーがある: {sorted(extra_keys)}")
        if missing_keys:
            problems.append(f"manifest に必須キーが無い: {sorted(missing_keys)}")
        return problems
    question_kind = manifest.get("question_kind")
    if not isinstance(question_kind, str) or question_kind == "":
        return ["question_kind は非空の str である必要がある"]
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str) or created_at == "":
        return ["created_at は非空の str である必要がある"]
    items = manifest.get("items")
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        return ["items は str の配列である必要がある"]
    return []


def _read_manifest(path: Path) -> dict | None:
    """`validate_manifest()` を通した manifest を返す。読めない・壊れている・正規形に合わない場合は `None`。"""
    data = _read_json_safe(path)
    if validate_manifest(data):
        return None
    return data


def _validate_evidence(evidence) -> list[str]:
    """evidence の各要素を検証する（キー集合が `{"kind", "path", "line"}` と完全一致・`kind`/`path` は str・`line` は int）。余分なキー・入れ子データ・型不一致は無効。`bool` は `line` として拒否する。
    問題を人が読める理由の文字列のリストで返す（無ければ空）。
    """
    if not isinstance(evidence, list):
        return ["evidence は配列である必要がある"]
    problems: list[str] = []
    for idx, ev in enumerate(evidence):
        if not isinstance(ev, dict):
            problems.append(f"evidence[{idx}] は dict である必要がある")
            continue
        if set(ev.keys()) != _EVIDENCE_REQUIRED_KEYS:
            problems.append(f"evidence[{idx}] のキー集合が {sorted(_EVIDENCE_REQUIRED_KEYS)} と一致しない")
            continue
        if not isinstance(ev.get("kind"), str):
            problems.append(f"evidence[{idx}].kind は str が必要")
        if not isinstance(ev.get("path"), str):
            problems.append(f"evidence[{idx}].path は str が必要")
        line = ev.get("line")
        if isinstance(line, bool) or not isinstance(line, int):
            problems.append(f"evidence[{idx}].line は int が必要")
    return problems


def validate_item(item, *, expected_id: str | None = None) -> list[str]:
    """item の正規形を検証し、問題を人が読める理由の文字列のリストで返す（無ければ空）。
    `expected_id` を渡すと `item["id"]` との一致も検査する（`load_ledger` がファイル名の stem と照合する用途）。
    """
    if not isinstance(item, dict):
        return ["item は dict である必要がある"]
    extra_keys = set(item.keys()) - _ITEM_REQUIRED_KEYS
    missing_keys = _ITEM_REQUIRED_KEYS - set(item.keys())
    if extra_keys or missing_keys:
        problems: list[str] = []
        if extra_keys:
            problems.append(f"item に余分なキーがある: {sorted(extra_keys)}")
        if missing_keys:
            problems.append(f"item に必須キーが無い: {sorted(missing_keys)}")
        return problems
    if expected_id is not None and item.get("id") != expected_id:
        return [f"item.id({item.get('id')!r}) がファイル名({expected_id!r})と一致しない"]
    if not isinstance(item.get("kind"), str):
        return ["kind は str である必要がある"]
    if not isinstance(item.get("owner"), str):
        return ["owner は str である必要がある"]
    subject = item.get("subject")
    if not isinstance(subject, str):
        return ["subject は str である必要がある"]
    if len(subject) > SUBJECT_MAX_LEN:
        return [f"subject が上限({SUBJECT_MAX_LEN}文字)を超えている"]
    reason = item.get("reason")
    if not isinstance(reason, str):
        return ["reason は str である必要がある"]
    if len(reason) > REASON_MAX_LEN:
        return [f"reason が上限({REASON_MAX_LEN}文字)を超えている"]
    required_checks = item.get("required_checks")
    if not isinstance(required_checks, list) or not all(isinstance(c, str) for c in required_checks):
        return ["required_checks は str の配列である必要がある"]
    if len(required_checks) == 0:
        return ["required_checks が空——確認すべき根拠種別を宣言していない"]
    if len(set(required_checks)) != len(required_checks):
        return ["required_checks に重複がある"]
    unknown_checks = sorted(set(required_checks) - _EVIDENCE_KINDS_SET)
    if unknown_checks:
        return [f"required_checks に語彙外の種別がある: {unknown_checks}"]
    evidence = item.get("evidence")
    evidence_problems = _validate_evidence(evidence)
    if evidence_problems:
        return evidence_problems
    status = item.get("status")
    # 非 str は集合照合で TypeError になるため、先に str 型を確定させる（例外を投げない）。
    if not isinstance(status, str):
        return ["status は str である必要がある"]
    if status not in NON_TERMINAL_STATUSES and status not in TERMINAL_STATUSES:
        return [f"status '{status}' は語彙に無い"]
    # 確認不能系の終端は理由必須。
    if status in REASON_REQUIRED_STATUSES and reason.strip() == "":
        return [f"status '{status}' は reason が必須"]
    # `unverified` の理由は閉じた語彙（`UNVERIFIED_REASON_CODES`）でなければならない。
    if status == "unverified" and reason not in UNVERIFIED_REASON_CODES:
        return [f"status 'unverified' の reason は {sorted(UNVERIFIED_REASON_CODES)} のいずれかである必要がある"]
    # 確認済み系の終端は根拠 1 件以上必須。さらに、状態が要求する根拠種別（`_STATUS_REQUIRED_EVIDENCE_KINDS`）が `evidence` の `kind` に実在することも確認する。
    if status in EVIDENCE_REQUIRED_STATUSES:
        if len(evidence) == 0:
            return [f"status '{status}' は evidence が1件以上必須"]
        required_kinds = _STATUS_REQUIRED_EVIDENCE_KINDS[status]
        present_kinds = {ev.get("kind") for ev in evidence}
        missing_kinds = required_kinds - present_kinds
        if missing_kinds:
            return [f"status '{status}' の evidence に必要な種別が無い: {sorted(missing_kinds)}"]
    return []


def _validate_item(item, *, expected_id: str) -> bool:
    """`validate_item` の真偽判定版（`load_ledger` 用）。"""
    return not validate_item(item, expected_id=expected_id)


def load_ledger(dir: Path) -> LedgerSnapshot:
    """`dir` から manifest と items を読む。`dir` が無ければ空のスナップショット。壊れた JSON・読めないファイルは無効として記録し、例外は投げない。
    symlink は一切辿らない（台帳は model-shell が書ける `run_dir/.tmp/investigation/` にあり、リンク先を親権限で読むと本文・他会話の情報が漏れる）:
    - `dir` 自身が symlink なら空スナップショットとして扱う。
    - `manifest.json` が symlink ならリンク先を読まず manifest を無効（`None`）にする。
    - `items/` 自体が symlink なら中を列挙せず items を空にし、manifest も無効にする。
    - 各 `items/*.json` が symlink ならリンク先を読まず、その id を `invalid_ids` に入れる。
    - `items/` の列挙（`glob`）自体が失敗したら manifest を無効にする。
    """
    dir = Path(dir)
    if _is_symlink_fail_closed(dir) or not dir.is_dir():
        return LedgerSnapshot(manifest=None, items={}, invalid_ids=())

    manifest_path = dir / _MANIFEST_FILENAME
    manifest = None if _is_symlink_fail_closed(manifest_path) else _read_manifest(manifest_path)

    items: dict[str, dict] = {}
    invalid: list[str] = []
    items_dir = dir / _ITEMS_DIRNAME
    if _is_symlink_fail_closed(items_dir):
        manifest = None
    elif items_dir.is_dir():
        try:
            entries = sorted(items_dir.glob("*.json"))
        except OSError:
            manifest = None
            entries = ()
        for path in entries:
            stem = path.stem
            if _is_symlink_fail_closed(path):
                invalid.append(stem)
                continue
            data = _read_json_safe(path)
            if _validate_item(data, expected_id=stem):
                items[stem] = data
            else:
                invalid.append(stem)

    return LedgerSnapshot(manifest=manifest, items=items, invalid_ids=tuple(sorted(invalid)))


def ledger_complete(snapshot: LedgerSnapshot, *, required_extra: tuple[str, ...] = (),
                    reviews: tuple[dict, ...] = (), require_review: bool = False,
                    require_continuation_resolved: bool = False) -> Verdict:
    """台帳の完了判定。`complete=True` の条件は、有効な manifest があり登録集合（`items`）が非空で、登録集合内の非終端・無効・欠落の item がゼロで、`unsatisfied` が空であること。
    - manifest が無い／壊れている／`items` が空なら `complete=False`（母集団が確定していない）。
    - `unregistered_ids`（item ファイルはあるが登録集合に無い id）は complete の判定に含めない（報告のみ）。`non_terminal_ids`/`invalid_ids` は登録集合との積集合。
    - `required_extra`: 呼び出し側が追加で必須にする根拠種別（既定は空タプル）。`EVIDENCE_REQUIRED_STATUSES` の item の必須集合へ `required_checks` との和集合として効く（`_missing_required_kinds`）。理由付きの終端は対象外。
    - `unsatisfied`: 登録済み・有効・`EVIDENCE_REQUIRED_STATUSES` の item で、`required_checks` の一部が `evidence` の `kind` に無い id → 足りない種別。未充足の id は `non_terminal_ids` にも重ねて含める。
    - `reviews`/`require_review`（既定は空タプル/`False`）: `reviews` のうち `validate_review_entry()` を満たすものだけを有効な見直しとして数える。`require_review=True` のとき、有効な見直しが無ければ `review_missing=True`、あれば `review_pending_ids` を集める。どちらかが真/非空なら `complete=False`。
    - `require_continuation_resolved`（既定 `False`）: `True` のとき、`pending_continuation_review(reviews)` が非 `None`（未解決の「追加の観点」の義務が残る）なら `review_missing=True` にする。「続き」で前ターンの未投資観点を注入したターンだけ `True` にする。
    """
    manifest_ids = set(snapshot.manifest["items"]) if snapshot.manifest is not None else set()
    manifest_invalid = snapshot.manifest is None or len(manifest_ids) == 0
    present_ids = set(snapshot.items) | set(snapshot.invalid_ids)

    missing_ids = tuple(sorted(manifest_ids - present_ids))
    unregistered_ids = tuple(sorted(present_ids - manifest_ids))

    unsatisfied: dict[str, tuple[str, ...]] = {}
    for item_id, item in snapshot.items.items():
        if item_id not in manifest_ids:
            continue
        missing_kinds = _missing_required_kinds(item, required_extra)
        if missing_kinds:
            unsatisfied[item_id] = missing_kinds

    non_terminal_ids = tuple(sorted(
        {item_id for item_id, item in snapshot.items.items()
         if item_id in manifest_ids and item.get("status") in NON_TERMINAL_STATUSES}
        | set(unsatisfied)
    ))
    invalid_ids = tuple(sorted(set(snapshot.invalid_ids) & manifest_ids))

    terminal_counts: dict[str, int] = {}
    for item in snapshot.items.values():
        status = item.get("status")
        if status in TERMINAL_STATUSES:
            terminal_counts[status] = terminal_counts.get(status, 0) + 1

    review_missing = False
    review_pending_ids: tuple[str, ...] = ()
    if require_review:
        valid_reviews = [r for r in reviews if not validate_review_entry(r)]
        if not valid_reviews:
            review_missing = True
        elif require_continuation_resolved and pending_continuation_review(reviews) is not None:
            # 義務の判定は `pending_continuation_review()`（見直しの列全体を見る純関数）に一本化し、`review_missing` を流用して、継続プロンプトが「見直しを書いてください」と促す経路に乗せる。
            review_missing = True
        else:
            pending: set[str] = set()
            for review in valid_reviews:
                for ref in review.get("added_items") or []:
                    if not isinstance(ref, dict):
                        continue
                    added_id = ref.get("id")
                    if not isinstance(added_id, str):
                        continue
                    if added_id not in manifest_ids:
                        pending.add(added_id)
                        continue
                    added_item = snapshot.items.get(added_id)
                    if added_item is None:
                        pending.add(added_id)  # 欠落 or 無効（`snapshot.invalid_ids` 側）
                        continue
                    if (added_item.get("status") in NON_TERMINAL_STATUSES
                            or added_id in unsatisfied):
                        pending.add(added_id)
            review_pending_ids = tuple(sorted(pending))

    complete = (
        not manifest_invalid
        and not non_terminal_ids
        and not invalid_ids
        and not missing_ids
        and not unsatisfied
        and not review_missing
        and not review_pending_ids
    )

    return Verdict(
        complete=complete,
        non_terminal_ids=non_terminal_ids,
        invalid_ids=invalid_ids,
        missing_ids=missing_ids,
        unregistered_ids=unregistered_ids,
        unsatisfied=unsatisfied,
        terminal_counts=terminal_counts,
        manifest_invalid=manifest_invalid,
        review_missing=review_missing,
        review_pending_ids=review_pending_ids,
    )


def _missing_required_kinds(item: dict, required_extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """根拠が必要な終端状態の item で、`required_checks` と `required_extra` の和集合のうち `evidence` に無い種別（昇順）。空なら充足。他の状態は常に空。"""
    if item.get("status") not in EVIDENCE_REQUIRED_STATUSES:
        return ()
    present_kinds = {ev.get("kind") for ev in (item.get("evidence") or []) if isinstance(ev, dict)}
    required = set(item.get("required_checks") or []) | set(required_extra)
    return tuple(sorted(required - present_kinds))


def no_progress(prev: LedgerSnapshot, curr: LedgerSnapshot,
                *, required_extra: tuple[str, ...] = ()) -> list[str]:
    """`prev`→`curr` で status/evidence/reason のいずれも変わっていない非終端 item（根拠種別が未充足の終端状態を含む）の id 一覧（昇順）。同じ item に状態遷移が無い attempt を無限継続させない判定材料。
    両方に存在する id だけが対象。`curr` で終端になった item、どちらかで無効だった id は対象外。
    `required_extra` は `ledger_complete()` と同じ値を渡すこと（未充足判定の基準を揃える）。
    """
    out = []
    for item_id, cur_item in curr.items.items():
        prev_item = prev.items.get(item_id)
        if prev_item is None:
            continue
        status = cur_item.get("status")
        # 根拠種別が未充足の終端状態も非終端扱い（`ledger_complete` と同じ定義）。
        if status not in NON_TERMINAL_STATUSES and not _missing_required_kinds(cur_item, required_extra):
            continue
        if prev_item.get("status") != status:
            continue
        if prev_item.get("evidence") != cur_item.get("evidence"):
            continue
        if prev_item.get("reason") != cur_item.get("reason"):
            continue
        out.append(item_id)
    return sorted(out)


def _validate_safe_id(item_id) -> str:
    """`id` がファイル名として安全か検証する（パス逃れ防止）。不正なら `ValueError`。"""
    if not isinstance(item_id, str) or item_id == "":
        raise ValueError(f"item id must be a non-empty str: {item_id!r}")
    if "/" in item_id or ".." in item_id or item_id.startswith("."):
        raise ValueError(f"unsafe item id (path traversal risk): {item_id!r}")
    return item_id


def _reject_symlinked_dir(directory: Path) -> None:
    """書込先ディレクトリの経路に symlink が含まれていれば `PermissionError`。台帳 dir は model-shell から書ける場所にあり、symlink に置き換えられるとサンドボックス外の MCP サーバがリンク先へ書いてしまう。
    呼出側は解決済み（realpath）のパスを渡す。
    """
    raw = os.path.abspath(directory)
    if os.path.realpath(raw) != raw:
        raise PermissionError(errno.EPERM, "台帳の書込先に symlink が含まれる", raw)


def _write_json_atomic(path: Path, data: dict) -> Path:
    """tmp→`os.replace` の原子置換（`json_io.write_json_atomic` と同じ方式の独立実装）。"""
    _reject_symlinked_dir(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def write_item_atomic(dir: Path, item: dict) -> Path:
    """1 item を `dir/items/{id}.json` へ原子的に書く（子が使う）。manifest は書かない（親が `write_manifest_atomic` で管理する）。
    `id` が非文字列/空/`/`・`..`・先頭 `.` を含む場合は `ValueError`。item の正規形はここでは検証しない（書込途中の shard を許す・検証は `load_ledger`/`ledger_complete`）。
    """
    item_id = _validate_safe_id(item.get("id") if isinstance(item, dict) else item)
    dir = Path(dir)
    return _write_json_atomic(dir / _ITEMS_DIRNAME / f"{item_id}.json", item)


def write_manifest_atomic(dir: Path, manifest: dict) -> Path:
    """manifest を `dir/manifest.json` へ原子的に書く（親が使う）。"""
    dir = Path(dir)
    return _write_json_atomic(dir / _MANIFEST_FILENAME, manifest)


# 項目ごとの調査カバレッジ記録。`dir/coverage.jsonl`（台帳と同じ置き場）に、item 引数つきの検索・読取ツール呼出し 1 回ごとの結果区分だけを追記する（`mcp_server.py` が呼ぶ・本文/引数の中身は書かない）。
# 親と子（`spawn_agent` の worker）は同じ `SHERPA_MCP_LEDGER_DIR` を共有するため、このファイルも親子共有になる。
COVERAGE_OUTCOMES: frozenset[str] = frozenset({
    "hit", "no_hits", "truncated", "limit", "timeout", "unreadable", "error"})
_COVERAGE_FILENAME = "coverage.jsonl"
_COVERAGE_REQUIRED_KEYS = frozenset({"item", "tool", "outcome", "ts"})
# 呼出しの内容（検索語・資料・読んだ範囲）。任意で、文字列だけ・`COVERAGE_DETAIL_MAX_LEN` 文字まで。
COVERAGE_DETAIL_KEYS: tuple[str, ...] = ("query", "doc", "range")
COVERAGE_DETAIL_MAX_LEN = 200


def append_coverage_atomic(dir: Path, item_id: str, tool: str, outcome: str,
                           detail: dict | None = None) -> None:
    """`dir/coverage.jsonl` へ 1 行（`item`/`tool`/`outcome`/`ts` の 4 キーと、任意の `COVERAGE_DETAIL_KEYS`）を追記する。
    `detail` は検索語（query）・資料（doc）・読んだ範囲（range）。文字列以外・空は捨て、長いものは `COVERAGE_DETAIL_MAX_LEN` 文字で切る（本文は渡さない）。
    `item_id` は `_validate_safe_id` で検証し、`outcome` は `COVERAGE_OUTCOMES` の外なら `ValueError`（呼び出し元の `mcp_server.py` が catch して fail-open にする）。
    追記は "a" モードの単発 `write()`（1 行が PIPE_BUF に収まる限り親子の追記が混ざらない）。
    ファイルは `O_NOFOLLOW`・`O_NONBLOCK` で開き、fstat で通常ファイルであることを確かめてから書く（symlink／FIFO への差し替えを防ぐ）。通常ファイルでなければ `OSError`。
    """
    item_id = _validate_safe_id(item_id)
    if not isinstance(tool, str) or not tool:
        raise ValueError(f"tool must be a non-empty str: {tool!r}")
    if outcome not in COVERAGE_OUTCOMES:
        raise ValueError(f"unknown coverage outcome: {outcome!r}")
    dir = Path(dir)
    path = dir / _COVERAGE_FILENAME
    _reject_symlinked_dir(dir)
    dir.mkdir(parents=True, exist_ok=True)
    entry = {"item": item_id, "tool": tool, "outcome": outcome, "ts": time.time()}
    for key in COVERAGE_DETAIL_KEYS:
        value = (detail or {}).get(key)
        if isinstance(value, str) and value.strip():
            entry[key] = value.strip()[:COVERAGE_DETAIL_MAX_LEN]
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.ELOOP, "coverage.jsonl is not a regular file", str(path))
        os.write(fd, line)
    finally:
        os.close(fd)


def _read_coverage_entries(dir: Path) -> list[dict]:
    """`dir/coverage.jsonl` の正規の行（必須 4 キー＋任意の `COVERAGE_DETAIL_KEYS` だけ・語彙内の `outcome`）を記録順で返す。壊れた行・型不正・symlink・読めないファイルは無視する。"""
    dir = Path(dir)
    path = dir / _COVERAGE_FILENAME
    if _is_symlink_fail_closed(path) or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    allowed = _COVERAGE_REQUIRED_KEYS | frozenset(COVERAGE_DETAIL_KEYS)
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        keys = set(entry.keys())
        if not _COVERAGE_REQUIRED_KEYS <= keys <= allowed:
            continue
        item_id, outcome = entry.get("item"), entry.get("outcome")
        if not isinstance(item_id, str) or not item_id or outcome not in COVERAGE_OUTCOMES:
            continue
        if any(not isinstance(entry[k], str) for k in keys & set(COVERAGE_DETAIL_KEYS)):
            continue
        out.append(entry)
    return out


def load_coverage(dir: Path) -> dict[str, tuple[str, ...]]:
    """`dir/coverage.jsonl` を読み、item id → `outcome` の記録順タプルにまとめる。壊れた行・型不正・語彙外の `outcome`・symlink・読めないファイルは無視する（例外を投げない）。"""
    out: dict[str, list[str]] = {}
    for entry in _read_coverage_entries(dir):
        out.setdefault(entry["item"], []).append(entry["outcome"])
    return {item_id: tuple(outcomes) for item_id, outcomes in out.items()}


def load_coverage_detail(dir: Path, *, per_item_max: int = 20) -> dict[str, list[dict]]:
    """item id → 呼出しの内容の列（`tool`・`outcome`・任意の `query`/`doc`/`range`・記録順）。1 項目あたり先頭 `per_item_max` 件までで、超えた件数は最後の要素 `{"omitted": N}` に入れる。"""
    out: dict[str, list[dict]] = {}
    omitted: dict[str, int] = {}
    for entry in _read_coverage_entries(dir):
        rows = out.setdefault(entry["item"], [])
        if len(rows) >= per_item_max:
            omitted[entry["item"]] = omitted.get(entry["item"], 0) + 1
            continue
        rows.append({k: entry[k] for k in ("tool", "outcome", *COVERAGE_DETAIL_KEYS) if k in entry})
    for item_id, n in omitted.items():
        out[item_id].append({"omitted": n})
    return out


# `not_found_in_scope` を `unverified` へ機械的に置き換えるときの理由の優先順位。複数の outcome が混在する item は、先頭から順に一致した理由を採用する。
# `search_truncated` は truncated と limit（`mcp_server._coverage_outcome` の上限区分）の両方を受ける。`error`（道具の失敗）は独立した `search_error` へ分ける。
_DOWNGRADE_REASON_PRIORITY: tuple[tuple[str, frozenset[str]], ...] = (
    ("timeout", frozenset({"timeout"})),
    ("unreadable", frozenset({"unreadable"})),
    ("search_error", frozenset({"error"})),
    ("search_truncated", frozenset({"truncated", "limit"})),
)


def apply_unverified_downgrades(dir: Path, snapshot: LedgerSnapshot,
                                *, exclude_ids: frozenset[str] = frozenset()) -> LedgerSnapshot:
    """`not_found_in_scope` の登録済み item を、`coverage.jsonl`（`load_coverage`）の記録に基づき必要なら `unverified` へ機械的に置き換える。
    - この item に `item` 付きの呼出しが 1 回も記録されていない → `reason="not_searched"`。
    - `truncated`／`limit`／`timeout`／`unreadable`／`error` が 1 回でもあれば、`_DOWNGRADE_REASON_PRIORITY` の順で reason を選んで置き換える。
    - 記録が `hit`／`no_hits` だけなら置き換えない（切り詰めの無い 0 件のままなら `not_found_in_scope`）。
    `not_found_in_scope` 以外の状態と未登録 item は対象外。
    `exclude_ids`（このターン開始時点で既に `not_found_in_scope` だった id の集合・既定は空）は、記録が 1 件も無いときだけ対象外にする（前ターンで確定した項目を `not_searched` へ降格しない）。
    対象 item ファイルを `write_item_atomic` で書き換える。書換えに失敗（`OSError`）した item は元の状態のまま返す（fail-open）。戻り値は置き換え後の `LedgerSnapshot`。
    """
    coverage = load_coverage(dir)
    manifest_ids = set(snapshot.manifest["items"]) if snapshot.manifest is not None else set()
    new_items = dict(snapshot.items)
    for item_id, item in snapshot.items.items():
        if item_id not in manifest_ids or item.get("status") != "not_found_in_scope":
            continue
        outcomes = coverage.get(item_id)
        if not outcomes and item_id in exclude_ids:
            continue
        if not outcomes:
            reason = "not_searched"
        else:
            outcome_set = set(outcomes)
            reason = next((code for code, bucket in _DOWNGRADE_REASON_PRIORITY if bucket & outcome_set),
                         None)
            if reason is None:
                continue  # hit/no_hits だけ＝置き換えない
        new_item = {**item, "status": "unverified", "reason": reason}
        try:
            write_item_atomic(dir, new_item)
        except (OSError, ValueError):
            continue  # 書換え失敗は元の状態のまま（fail-open）
        new_items[item_id] = new_item
    return LedgerSnapshot(manifest=snapshot.manifest, items=new_items, invalid_ids=snapshot.invalid_ids)


# 中間の見直し。`dir/reviews.jsonl`（台帳・coverage.jsonl と同じ置き場）に、回答を確定する前の「目的・観点の見直し」を 1 行 1 件で追記する。
# 見直しは本文（purpose/summary/perspectives/extra_perspectives）を持つ点が item/manifest と異なる（人が後から読む調査の記録の一部）。各欄に上限文字数・配列件数の上限を設ける。
REVIEW_VERDICTS: frozenset[str] = frozenset({"insufficient", "mostly_answered"})
# `summary` の上限文字数（`SUBJECT_MAX_LEN` の 2 倍）。
REVIEW_SUMMARY_MAX_LEN = SUBJECT_MAX_LEN * 2
# 観点・足した/外した項目の配列件数の上限。
_REVIEW_LIST_MAX_ITEMS = 50
# review entry の正規形（キー集合完全一致）。`ts`/`terminal_count` はサーバ（`mcp_server.py`）が設定する。
# `terminal_count` はその見直しを書いた時点の終端 item の件数で、`ledger_review_put` が受付時に数え、1 未満なら書込みを拒否する。
# `validate_review_entry()` が 1 以上を要求するため、`ledger_complete()` は記録済みの値を信じてよい。
_REVIEW_REQUIRED_KEYS = frozenset({
    "purpose", "perspectives", "summary", "added_items", "removed_items",
    "verdict", "extra_perspectives", "ts", "terminal_count"})
# `added_items`/`removed_items` の 1 要素の正規形（キー集合完全一致）。
_REVIEW_ITEM_REF_KEYS = frozenset({"id", "reason"})
# item id の書式（英数字・ハイフン・アンダースコアのみ）。
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_REVIEWS_FILENAME = "reviews.jsonl"
# 見直しの件数と `reviews.jsonl` の総バイト数の上限。超える追記は拒否し（`append_review_atomic`）、読込（`load_reviews`）も同じ上限で打ち切る。
REVIEWS_MAX_COUNT = 20
REVIEWS_MAX_BYTES = 256 * 1024  # 256 KiB


def _validate_text_list(value, field_name: str, *, require_non_empty: bool) -> list[str]:
    """`perspectives`/`extra_perspectives` 共通の検証（配列・件数上限・各要素が非空 str で `SUBJECT_MAX_LEN` 以下）。問題を理由の文字列のリストで返す。"""
    if not isinstance(value, list):
        return [f"{field_name} は配列である必要がある"]
    if len(value) > _REVIEW_LIST_MAX_ITEMS:
        return [f"{field_name} が上限({_REVIEW_LIST_MAX_ITEMS}件)を超えている"]
    if require_non_empty and len(value) == 0:
        return [f"{field_name} が空——観点を1件以上挙げる"]
    for idx, v in enumerate(value):
        if not isinstance(v, str) or v.strip() == "":
            return [f"{field_name}[{idx}] は非空の str である必要がある"]
        if len(v) > SUBJECT_MAX_LEN:
            return [f"{field_name}[{idx}] が上限({SUBJECT_MAX_LEN}文字)を超えている"]
    return []


def _validate_item_refs(value, field_name: str) -> list[str]:
    """`added_items`/`removed_items` 共通の検証（配列・件数上限・各要素は `id`/`reason` の 2 キー・`id` は item id の書式・`reason` は非空で `REASON_MAX_LEN` 以下）。"""
    if not isinstance(value, list):
        return [f"{field_name} は配列である必要がある"]
    if len(value) > _REVIEW_LIST_MAX_ITEMS:
        return [f"{field_name} が上限({_REVIEW_LIST_MAX_ITEMS}件)を超えている"]
    for idx, ref in enumerate(value):
        if not isinstance(ref, dict) or set(ref.keys()) != _REVIEW_ITEM_REF_KEYS:
            return [f"{field_name}[{idx}] のキー集合が {sorted(_REVIEW_ITEM_REF_KEYS)} と一致しない"]
        rid = ref.get("id")
        if not isinstance(rid, str) or not _ITEM_ID_RE.match(rid):
            return [f"{field_name}[{idx}].id は item id の書式（英数字・ハイフン・アンダースコア）"
                   " である必要がある"]
        reason = ref.get("reason")
        if not isinstance(reason, str) or reason.strip() == "":
            return [f"{field_name}[{idx}].reason は非空の str である必要がある"]
        if len(reason) > REASON_MAX_LEN:
            return [f"{field_name}[{idx}].reason が上限({REASON_MAX_LEN}文字)を超えている"]
    return []


def validate_review_entry(entry) -> list[str]:
    """中間の見直し 1 件の正規形を検証し、問題を理由の文字列のリストで返す（無ければ空）。キー集合は `_REVIEW_REQUIRED_KEYS` の 9 つと完全一致する。
    `verdict` が `insufficient` のときは `extra_perspectives` を空配列にする。`terminal_count` は 1 以上の整数を要求する（型・下限だけ検証）。
    """
    if not isinstance(entry, dict):
        return ["review は dict である必要がある"]
    extra_keys = set(entry.keys()) - _REVIEW_REQUIRED_KEYS
    missing_keys = _REVIEW_REQUIRED_KEYS - set(entry.keys())
    if extra_keys or missing_keys:
        problems: list[str] = []
        if extra_keys:
            problems.append(f"review に余分なキーがある: {sorted(extra_keys)}")
        if missing_keys:
            problems.append(f"review に必須キーが無い: {sorted(missing_keys)}")
        return problems
    purpose = entry.get("purpose")
    if not isinstance(purpose, str) or purpose.strip() == "":
        return ["purpose は非空の str である必要がある"]
    if len(purpose) > SUBJECT_MAX_LEN:
        return [f"purpose が上限({SUBJECT_MAX_LEN}文字)を超えている"]
    summary = entry.get("summary")
    if not isinstance(summary, str) or summary.strip() == "":
        return ["summary は非空の str である必要がある"]
    if len(summary) > REVIEW_SUMMARY_MAX_LEN:
        return [f"summary が上限({REVIEW_SUMMARY_MAX_LEN}文字)を超えている"]
    problems = _validate_text_list(entry.get("perspectives"), "perspectives", require_non_empty=True)
    if problems:
        return problems
    problems = _validate_item_refs(entry.get("added_items"), "added_items")
    if problems:
        return problems
    problems = _validate_item_refs(entry.get("removed_items"), "removed_items")
    if problems:
        return problems
    verdict = entry.get("verdict")
    if not isinstance(verdict, str) or verdict not in REVIEW_VERDICTS:
        return [f"verdict は {sorted(REVIEW_VERDICTS)} のいずれかである必要がある"]
    problems = _validate_text_list(entry.get("extra_perspectives"), "extra_perspectives",
                                   require_non_empty=False)
    if problems:
        return problems
    if verdict != "mostly_answered" and entry.get("extra_perspectives"):
        return ["verdict が insufficient のときは extra_perspectives を空配列にする"]
    ts = entry.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return ["ts は数値である必要がある"]
    terminal_count = entry.get("terminal_count")
    if isinstance(terminal_count, bool) or not isinstance(terminal_count, int) or terminal_count < 1:
        return ["terminal_count は1以上の整数である必要がある"]
    return []


def append_review_atomic(dir: Path, entry: dict) -> None:
    """1 件の中間の見直しを `dir/reviews.jsonl` へ追記する。`entry` は呼び出し側（`mcp_server.py`）が `ts`/`terminal_count` を付けて `validate_review_entry()` を通した正規形を渡す（本関数は正規形を検証しない）。
    書込方式は `append_coverage_atomic` と同じ（単発 "a" write・`O_NOFOLLOW`・`O_NONBLOCK`・fstat で通常ファイル確認）。
    `dir` 自身が symlink／ディレクトリでなければ拒否する（`_is_symlink_fail_closed`・`PermissionError`）。経路に symlink があれば同じく `PermissionError`（`_reject_symlinked_dir`）。
    追記後の件数が `REVIEWS_MAX_COUNT`、またはファイル総バイト数が `REVIEWS_MAX_BYTES` を超える場合は書き込まず `ValueError`。件数・バイト数の判定は厳密な排他ではなく、上限ちょうどでは稀に多少超過しうる。
    """
    dir = Path(dir)
    if _is_symlink_fail_closed(dir) or not dir.is_dir():
        raise PermissionError(errno.EPERM, "台帳ディレクトリが symlink またはディレクトリでない", str(dir))
    path = dir / _REVIEWS_FILENAME
    _reject_symlinked_dir(dir)
    dir.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    existing_size = 0
    existing_count = 0
    if path.is_file() and not path.is_symlink():
        try:
            existing = path.read_bytes()
        except OSError:
            existing = b""
        existing_size = len(existing)
        existing_count = existing.count(b"\n")
    if existing_count >= REVIEWS_MAX_COUNT:
        raise ValueError(f"reviews が上限({REVIEWS_MAX_COUNT}件)に達しています")
    if existing_size + len(line) > REVIEWS_MAX_BYTES:
        raise ValueError(f"reviews.jsonl が上限({REVIEWS_MAX_BYTES}バイト)を超えます")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.ELOOP, "reviews.jsonl is not a regular file", str(path))
        os.write(fd, line)
    finally:
        os.close(fd)


def load_reviews_report(dir: Path) -> tuple[tuple[dict, ...], dict]:
    """`load_reviews` と同じ読み取りに、省略の内訳を添えて返す。`report` は `{"invalid": 不正行で除いた件数, "over_count": 件数の上限 `REVIEWS_MAX_COUNT` で読まなかった件数, "over_bytes": 容量の上限 `REVIEWS_MAX_BYTES` で読まなかったか}`（総数が分からない読み捨ては件数でなく真偽）。"""
    report = {"invalid": 0, "over_count": 0, "over_bytes": False}
    dir = Path(dir)
    if _is_symlink_fail_closed(dir) or not dir.is_dir():
        return (), report
    path = dir / _REVIEWS_FILENAME
    if _is_symlink_fail_closed(path) or not path.is_file():
        return (), report
    try:
        with path.open("rb") as f:
            raw = f.read(REVIEWS_MAX_BYTES + 1)
    except OSError:
        return (), report
    report["over_bytes"] = len(raw) > REVIEWS_MAX_BYTES
    try:
        text = raw[:REVIEWS_MAX_BYTES].decode("utf-8", errors="ignore")
    except ValueError:
        return (), report
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            report["invalid"] += 1
            continue
        if validate_review_entry(entry):
            report["invalid"] += 1
            continue
        if len(out) >= REVIEWS_MAX_COUNT:
            report["over_count"] += 1
            continue
        out.append(entry)
    return tuple(out), report


def load_reviews(dir: Path) -> tuple[dict, ...]:
    """`dir/reviews.jsonl` を読み、`validate_review_entry()` を満たす見直しだけを記録順のタプルで返す。壊れた行・型不正・語彙外・symlink・読めないファイルは無視する（例外を投げない）。
    `dir` 自身が symlink／ディレクトリでなければ空タプル。先頭 `REVIEWS_MAX_BYTES` バイト・`REVIEWS_MAX_COUNT` 件までしか読まない（超過分は読み捨てる・内訳は `load_reviews_report`）。
    """
    return load_reviews_report(dir)[0]


def pending_continuation_review(reviews: tuple[dict, ...]) -> dict | None:
    """見直しの列（`load_reviews()` の戻り値・記録順）から、まだ「追加の観点」を調べる義務が残っている見直しを返す（無ければ `None`・純関数）。
    義務: `verdict == "mostly_answered"` かつ `extra_perspectives` が非空の見直しのうち、その後に `added_items` を 1 件以上持つ見直しがまだ書かれていないもの。複数あれば直近のものを返す。
    回答末尾の定型文・「続き」の継続プロンプトへの注入・`ledger_complete()`（`require_continuation_resolved=True`）・退避判断（`force_retain`）の 4 箇所は、義務の有無・内容をこの関数だけを根拠に判定する。
    """
    valid = [r for r in reviews if not validate_review_entry(r)]
    pending: dict | None = None
    for review in valid:
        if pending is not None and review.get("added_items"):
            pending = None
        if review.get("verdict") == "mostly_answered" and review.get("extra_perspectives"):
            pending = review
    return pending


# 見直しの自由記述を表示・プロンプト注入へ出す前の正規化。回答末尾の定型文・「続き」の継続プロンプト・調査の記録の Markdown 表示は、この 1 つの関数を通してから使う。
# 改行・制御文字で構造を乱したり、Markdown の記号で表示崩れを起こしたりする経路を塞ぐ（表示崩れ防止であり、内容の正当性は `validate_review_entry` が担う）。
REVIEW_TEXT_PER_ITEM_MAX = 200
REVIEW_TEXT_TOTAL_MAX = 1000
# 上限で切ったことを示す印（切った文字列の末尾に付ける）。
TRUNCATION_MARK = "…"
# Markdown で表示崩れの原因になりやすい記号だけをエスケープする（行頭でだけ意味を持つ記号は、出力が常に文中・ラベルの後ろに置かれるため対象にしない）。`\` を最初に処理する。
_REVIEW_TEXT_ESCAPE_CHARS = ("\\", "`", "*", "_", "[", "]", "|")


def sanitize_review_text(text) -> str:
    """見直しの自由記述 1 件を正規化する。改行・制御文字を取り除き（`str.isprintable()`）、`_REVIEW_TEXT_ESCAPE_CHARS` をエスケープし、`REVIEW_TEXT_PER_ITEM_MAX` 文字で切り、切ったら末尾に `TRUNCATION_MARK` を付ける。非 str・空文字は空文字を返す。"""
    if not isinstance(text, str):
        return ""
    cleaned = "".join(ch for ch in text if ch.isprintable()).strip()
    for ch in _REVIEW_TEXT_ESCAPE_CHARS:
        cleaned = cleaned.replace(ch, "\\" + ch)
    if len(cleaned) > REVIEW_TEXT_PER_ITEM_MAX:
        return cleaned[:REVIEW_TEXT_PER_ITEM_MAX] + TRUNCATION_MARK
    return cleaned


def sanitize_review_text_list(values, *, separator: str = "、") -> str:
    """`extra_perspectives` 等（str の配列）の各要素を `sanitize_review_text` で正規化して `separator` で結合し、結合結果全体を `REVIEW_TEXT_TOTAL_MAX` 文字で切り、切ったら末尾に `TRUNCATION_MARK` を付ける。空・非 str の要素は読み飛ばす。"""
    cleaned = [c for c in (sanitize_review_text(v) for v in (values or [])) if c]
    joined = separator.join(cleaned)
    if len(joined) > REVIEW_TEXT_TOTAL_MAX:
        return joined[:REVIEW_TEXT_TOTAL_MAX] + TRUNCATION_MARK
    return joined
