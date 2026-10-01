"""調査台帳の純関数部分（`docs/archive/2026-09-21-調査台帳を文脈の外に置く.md` §3/§7 が正典）。

台帳は「回答の正本」ではなく「1ターンの作業台帳」。業務事実の正本は常にソース、利用者への回答の
正本は `messages.answer`（正典 §3「位置づけ」）。このモジュールは台帳の read/write/判定だけを扱う
**純関数部分**で、`provider.py` 等への統合（完了ゲート・最終回答生成・寿命管理）は別レーンの対象。

契約:
- **標準ライブラリのみに依存する**。他の sherpa モジュールを import しない（`layer.py`／
  `depth_profile.py` と同じ葉ノード原則）。アトミック書込は `sherpa/json_io.py` と同じ
  tmp→`os.replace` 方式をこのモジュール内に独立実装する（DRY より葉ノード原則を優先する）。
- item 最上位もキー集合が正規形8キー（`id`/`kind`/`subject`/`required_checks`/`evidence`/
  `status`/`reason`/`owner`）と**完全一致**する契約——余分なキー（例: `text`）を足して本文を
  紛れ込ませる経路を塞ぐ。item の `evidence` も各要素の**キー集合が `{"kind", "path", "line"}`
  と完全一致**する契約——資料本文（テキスト）は台帳に書かない。余分なキー（`text` を含む）・
  入れ子データは無効として拒否する（`kind`/`path` は str・`line` は int でなければならない・
  本文混入の機械的防止・fail-closed）。`subject`/`reason` にも上限文字数（`SUBJECT_MAX_LEN`/
  `REASON_MAX_LEN`）を設け、超過は無効とする——自由記述欄に本文を書き込む逃げ道を狭める。
  manifest も同じ流儀——キー集合が `question_kind`/`created_at`/`items` と完全一致し、
  `question_kind`/`created_at` は非空 str でなければならない（`_read_manifest` 参照）。
- 壊れた JSON・型不一致・必須キー欠落/余分・読めないファイルは**例外を投げず**無効/欠落として
  記録する（判定は呼び出し側＝`ledger_complete()` に任せる・fail-safe）。`write_item_atomic`/
  `write_manifest_atomic` の `id` 安全性チェックのみ `ValueError` で fail-loud（パス逃れ防止）。
- **壊れた・無い・空の manifest からは `complete=True` を作らない**（正典§4「壊れた台帳から
  `final` を生成しない」）。`ledger_complete()` は「有効な manifest が存在し、その `items` が
  空でない」ことを complete の必須条件にする（詳細は `ledger_complete` の docstring）。
- 終端 item は理由・根拠なしで確定できない（正典§3「確認不能・範囲外・読取不能は理由付きの
  終端」）——`REASON_REQUIRED_STATUSES`（`not_found_in_scope`/`unreadable`/`unavailable`）は
  `reason.strip()` が空なら無効、`EVIDENCE_REQUIRED_STATUSES`（`source_confirmed`/`spec_only`/
  `conflict`）は `evidence` が空配列なら無効（調べずに終端化する・根拠なしで確認済みを名乗る、
  という2つの抜け道を塞ぐ）。
- `required_checks` は `EVIDENCE_KINDS`（`source`/`spec_doc`/`definition`/`log_config`/
  `callgraph`）の閉集合——語彙外・重複・空配列は無効
  （`docs/archive/2026-09-22-Codex経路の精度・網羅性と費用の改善.md` §3.3。何を確認すべきか
  宣言していない item は完了判定できない）。`EVIDENCE_REQUIRED_STATUSES` の3状態はさらに、その
  状態が意味として要求する根拠種別が `evidence` の `kind` に無ければ無効とする（`source_confirmed`
  →`source`／`spec_only`→`spec_doc`／`conflict`→`source`と`spec_doc`の両方。設計書の根拠だけでは
  `source_confirmed` は成立しない）。この3状態はさらに、`required_checks` に宣言した**全種別**に
  ついて対応する `evidence` が揃っていなければ、item 自体は有効でも「未充足」として
  `ledger_complete()` が完了を妨げる（`Verdict.unsatisfied`）——`required_checks` の宣言と実際に
  集めた証跡の乖離を機械的に検知する。
- `ledger_complete()`/`no_progress()` は `required_extra`（キーワード専用・既定は空タプル）を
  受ける。呼び出し側（provider.py）が item の `required_checks` 宣言に関わらず追加で必須にできる
  根拠種別（`EVIDENCE_KINDS` の閉集合を想定・呼び出し側が保証する）——モデル自身が宣言しなかった
  種別でも、範囲にその種別があるといった呼び出し側の事情で完了判定に足せる。`required_checks` との
  **和集合**として効き、対象は `EVIDENCE_REQUIRED_STATUSES` の item だけ（理由付きの終端は影響
  されない）。既定の空タプルでは現行と完全に同じ結果になる。
- **`load_ledger()` は symlink を一切辿らない**（RV 高-1・2026-09-22 9巡目是正）。台帳は
  model-shell が書ける領域にあり、リンク先を親権限（Sherpa 本体）で読むと本文・他会話の情報が
  漏れる——`dir`／`manifest.json`／`items/`／各 `items/*.json` はいずれも読む前に symlink 判定
  し、1つでも symlink ならその要素は読まずに無効にする（詳細は `load_ledger` の docstring）。
- **中間の見直し**（COD-18 ⑤・`docs/proposals/課題管理簿.md` COD-18・利用者2026-10-01指示）:
  `dir/reviews.jsonl`（台帳と同じ置き場）に、台帳が完了と判定される前に最低1回必要な「目的・
  観点の見直し」を1行1件で追記する（`append_review_atomic`・`coverage.jsonl` と同じ追記専用の
  jsonl）。1行の正規形は `validate_review_entry()` が検証する8キーちょうど（本文は `purpose`/
  `summary`/`perspectives`/`extra_perspectives` に書いてよいが、各欄に上限文字数・配列件数の
  上限を設ける——自由記述欄が無制限に肥大化する経路を塞ぐ）。`ledger_complete()` は
  `require_review=True` のときだけ、有効な見直しが1件も無い（`review_missing`）・見直しが
  `added_items` に挙げた item id が終端になっていない（`review_pending_ids`）のいずれかなら
  `complete=False` にする——`require_review` の既定は `False`（渡さない呼び出しは現行と完全に
  同じ結果＝Codex の標準モード以外（素の Codex・台帳を使わない構成）はこの条件を一切見ない）。
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

# 非終端＝まだ検証していない／検証中。終端＝検証結果が確定した状態（正典§3「状態語彙」）。
# 未知の状態文字列はどちらの集合にも属さない＝item は無効として扱われる。
NON_TERMINAL_STATUSES: frozenset[str] = frozenset({"pending", "in_progress"})
# `unverified`（COD-16・2026-09-29「調査の網羅と未確認の明示」提案書 §2）: 「確認できなかった」
# ことそのものを理由付きの終端として明示する状態——`not_found_in_scope` と違い、Sherpa が
# coverage.jsonl（`load_coverage`）の記録に基づき機械的に置き換える（`apply_unverified_downgrades`）
# ことがある。モデル自身が直接 `ledger_item_put` で使ってもよい（両方とも `reason` は
# `UNVERIFIED_REASON_CODES` の閉じた語彙のみ）。
TERMINAL_STATUSES: frozenset[str] = frozenset({
    "source_confirmed",
    "spec_only",
    "conflict",
    "not_found_in_scope",
    "unreadable",
    "unavailable",
    "unverified",
})

# 「確認不能・範囲外・読取不能」は**理由付き**の終端でなければならない（正典§3「状態語彙」）。
# `reason.strip()` が空ならその item は無効——理由の無い確認不能は、調べずに終端化する抜け道になる。
# `unverified` も理由必須——ただしこの状態だけは `reason` 自体が閉じた語彙
# （`UNVERIFIED_REASON_CODES`）でなければならない（`validate_item` の追加チェック）。
REASON_REQUIRED_STATUSES: frozenset[str] = frozenset({
    "not_found_in_scope", "unreadable", "unavailable", "unverified"})
# 「確認済み」系の終端は根拠（evidence）1件以上が必須——根拠の無い「確認済み」も同じ抜け道になる。
EVIDENCE_REQUIRED_STATUSES: frozenset[str] = frozenset({"source_confirmed", "spec_only", "conflict"})

# `unverified` の `reason` が取れる値の閉じた語彙（COD-16 §2・提案書§5 受け入れ）。自由記述にすると
# 「確認できなかった項目」節（provider.py 側）が理由を機械的に利用者向け文へ変換できない——
# 台帳へ本文を書かせない契約（モジュール docstring）とも整合する。
# - search_truncated: 検索がヒット数上限／件数上限で打ち切られた（母集団の一部しか見ていない）。
# - search_error: 検索・グラフの道具そのものが失敗した（`error`＝母集団を確認できていない・
#   切り詰めとは別＝道具が結果を返せなかった側）。
# - no_hits_only: 0件の検索しか行っていない（モデル自身が「これだけでは確信が持てない」と判断
#   したときに使う——Sherpa 側の機械的な降格はこの理由を選ばない・§5「切り詰めの無い0件の検索
#   だけなら not_found_in_scope のまま」）。
# - timeout: 検索・読取が時間切れになった。
# - unreadable: 検索・読取の対象が読み取れなかった。
# - not_searched: この item に対して item 付きの検索・読取が1回も行われていない。
UNVERIFIED_REASON_CODES: frozenset[str] = frozenset({
    "search_truncated", "search_error", "no_hits_only", "timeout", "unreadable", "not_searched"})

# 「確認できなかった項目」（COD-17・提案書§3）として回答末尾の節に集める終端状態の閉集合。
# `not_found_in_scope` は（降格されていなくても）利用者からは「見つからなかった＝確認できな
# かった」に見えるため常に含める——ここでの意味は「陽性確認が無い」であって「調べが甘い」とは
# 限らない（降格された分だけ理由が `unverified`/`UNVERIFIED_REASON_CODES` になる）。
UNCONFIRMED_STATUSES: frozenset[str] = frozenset({
    "unverified", "not_found_in_scope", "unreadable", "unavailable"})

# 台帳が扱う根拠種別の閉集合（`sherpa/investigation_state.py` の `EVIDENCE_KINDS` と同じ語彙
# ——このモジュールは他の sherpa モジュールを import しない契約のため独立定義する・葉ノード
# 原則優先・DRY より優先する）。`required_checks` の各要素はこの外に出られない。
EVIDENCE_KINDS: tuple[str, ...] = ("source", "spec_doc", "definition", "log_config", "callgraph")
_EVIDENCE_KINDS_SET: frozenset[str] = frozenset(EVIDENCE_KINDS)

# `EVIDENCE_REQUIRED_STATUSES` の各状態が意味として要求する根拠種別——`evidence` の `kind` に
# これが無ければ item 自体が無効（`required_checks` の宣言内容とは独立の、状態そのものに内在する
# 最小要件）。`conflict` はソースと設計書の食い違いを言うため両方が要る。
_STATUS_REQUIRED_EVIDENCE_KINDS: dict[str, frozenset[str]] = {
    "source_confirmed": frozenset({"source"}),
    "spec_only": frozenset({"spec_doc"}),
    "conflict": frozenset({"source", "spec_doc"}),
}

# item の正規形（正典§3「item の正規形と質問型ごとの行」）＝キー集合がこの8つと**完全一致**
# （余分なキー・欠落のどちらも無効・本文をよそのキーへ紛れ込ませる経路を塞ぐ）。
# 質問固有の列は kind/subject から表示する。
_ITEM_REQUIRED_KEYS = frozenset({"id", "kind", "subject", "required_checks", "evidence", "status", "reason", "owner"})
# evidence 1要素の正規形＝キー集合がこの3つと**完全一致**（本文は含めない・§5「秘匿」）。
_EVIDENCE_REQUIRED_KEYS = frozenset({"kind", "path", "line"})
# manifest の正規形（正典§3「manifest」）＝キー集合がこの3つと**完全一致**（item と同じ流儀・
# 余分なキーへ本文を書かせない）。
_MANIFEST_REQUIRED_KEYS = frozenset({"question_kind", "created_at", "items"})

# `subject`/`reason` の上限文字数（超過は無効・自由記述欄への本文書き込みを防ぐ）。
SUBJECT_MAX_LEN = 2000
REASON_MAX_LEN = 2000

_MANIFEST_FILENAME = "manifest.json"
_ITEMS_DIRNAME = "items"


@dataclass(frozen=True)
class LedgerSnapshot:
    """`load_ledger()` の戻り値＝読んだだけの状態（判定は行わない）。

    `manifest`: 読めた manifest（`dict`）。無い／壊れている／`items` が文字列の配列でない場合は
    `None`（欠落と同一視・`ledger_complete()` はこの場合 manifest の `items` を空集合として扱う）。
    `items`: 有効な item のみ（id＝ファイル名の stem をキーに持つ辞書）。
    `invalid_ids`: 壊れている/無効な item の id（ファイル stem から拾えた分・昇順）。
    """

    manifest: dict | None
    items: dict[str, dict]
    invalid_ids: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    """`ledger_complete()` の戻り値。

    `manifest_invalid`: manifest が無い／壊れている／`items` が空、のいずれかなら `True`
    （「manifest 無効」の報告欄）。`True` の間は他の条件に関わらず `complete=False`。

    id は原則として `non_terminal_ids`／`invalid_ids`／`missing_ids`／`unregistered_ids` の
    いずれか1つにのみ現れる（**登録集合（manifest の `items`）に無い id は、その item の状態・
    有効性に関わらず `unregistered_ids` にだけ**現れ、`non_terminal_ids`/`invalid_ids` には
    含めない——親が把握していない item の状態は完了判定の対象外という契約を、報告欄の分類でも
    一貫させるため）。例外は `unsatisfied`（下記）の id——構造的には有効・終端だが根拠種別が
    未充足の item は `non_terminal_ids` にも重ねて現れる。

    `unsatisfied`（`docs/archive/2026-09-22-Codex経路の精度・網羅性と費用の改善.md` §3.3が
    正典）: `EVIDENCE_REQUIRED_STATUSES`（`source_confirmed`/`spec_only`/`conflict`）の登録済み
    item のうち、`required_checks` と `ledger_complete()` の `required_extra` の**和集合**の一部が
    `evidence` に無い id → 足りない種別（ソート済み）の対応。item 自体は `validate_item` 上は有効
    （`snapshot.items` に入る）でも、この必須集合を満たしていなければ完了させない——`required_checks`
    を宣言しても対応する `evidence` を集めていない item が「確認済み」を名乗れてしまう穴を塞ぐ（例:
    `required_checks=[spec_doc, source]` の item が spec_doc の evidence だけで `spec_only` に
    なっても、source を確認していない以上は未充足）。継続判定・無進捗判定が既存の
    `non_terminal_ids` 経路でそのまま効くよう、未充足の id は `non_terminal_ids` にも含める
    （`unsatisfied` は内訳の報告専用）。

    `review_missing`/`review_pending_ids`（COD-18 ⑤・`ledger_complete()` に `require_review=True`
    を渡したときだけ意味を持つ・既定 `require_review=False` ではどちらも常に偽/空で `complete` に
    影響しない）: `review_missing` は有効な中間の見直し（`validate_review_entry()` を満たす
    `reviews` の要素）が1件も無いこと、または `require_continuation_resolved=True` のとき
    `pending_continuation_review(reviews)` が非 `None`（＝まだ解決していない「追加の観点」の
    義務が残っている）であることを示す。`review_pending_ids` は、いずれかの見直しが
    `added_items` に挙げた item id のうち、登録集合に無い・item ファイルが無い/無効・終端でない、
    のいずれかに該当するもの（昇順タプル）——見直しで「足した」と申告した項目が実際には終端に
    なっていないことを示す。どちらか一方でも真/非空なら `complete=False`。
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
    """`path.is_symlink()` の fail-closed 版——判定自体が失敗（列挙不能なディレクトリ配下等）
    したら「疑わしい symlink」として `True` を返す（`load_ledger` の symlink 拒否契約を
    列挙エラーが素通りさせないため）。"""
    try:
        return path.is_symlink()
    except OSError:
        return True


def _read_json_safe(path: Path):
    """JSON を安全に読む。無い/壊れ/IO エラーは `None`（呼び出し側が欠落/無効として扱う）。"""
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def validate_manifest(manifest) -> list[str]:
    """manifest の正規形（キー集合が `question_kind`/`created_at`/`items` と完全一致・
    `question_kind`/`created_at` は非空 str・`items` は文字列の配列）を検証し、問題を人が読める
    理由の文字列のリストで返す（問題が無ければ空リスト）。`_read_manifest` はこの薄いラッパー。
    """
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
    """`validate_manifest()` を通した manifest を返す。読めない・壊れている・正規形に合わない
    場合はいずれも `None`（`ledger_complete()` が `manifest_invalid` として扱う）。
    """
    data = _read_json_safe(path)
    if validate_manifest(data):
        return None
    return data


def _validate_evidence(evidence) -> list[str]:
    """各要素のキー集合が `{"kind", "path", "line"}` と完全一致し、`kind`/`path` が str・`line` が
    int であることを要求する（余分なキー・入れ子データ・型不一致は全て無効＝本文混入の防止）。
    `bool` は `int` の部分型のため `line` として明示的に拒否する（`True`/`False` が行番号として
    紛れ込むのを防ぐ）。問題を人が読める理由の文字列のリストで返す（問題が無ければ空リスト）。
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
    """item の正規形を検証し、問題を人が読める理由の文字列のリストで返す（問題が無ければ空
    リスト）。`_validate_item` はこの薄いラッパー（真偽判定は `not validate_item(...)`）。
    `expected_id` を渡すと `item["id"]` がそれと一致するかも検査する（`load_ledger` がファイル名の
    stem との一致を確認する用途——省略時はこの検査をしない）。
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
    # `status in <frozenset>` はハッシュ照合のため、非 str（list/dict 等）は hash() 自体が
    # TypeError になる——先に str 型を確定させてから集合照合する（fail-safe・例外を投げない契約）。
    if not isinstance(status, str):
        return ["status は str である必要がある"]
    if status not in NON_TERMINAL_STATUSES and status not in TERMINAL_STATUSES:
        return [f"status '{status}' は語彙に無い"]
    # 確認不能系の終端は理由必須（理由なしで「調べずに終端化」できる抜け道を塞ぐ）。
    if status in REASON_REQUIRED_STATUSES and reason.strip() == "":
        return [f"status '{status}' は reason が必須"]
    # `unverified` は理由が閉じた語彙（`UNVERIFIED_REASON_CODES`）でなければならない——自由記述の
    # 理由文にすると回答末尾の節（provider.py）が機械的に人間向け文へ変換できなくなる。
    if status == "unverified" and reason not in UNVERIFIED_REASON_CODES:
        return [f"status 'unverified' の reason は {sorted(UNVERIFIED_REASON_CODES)} のいずれかである必要がある"]
    # 確認済み系の終端は根拠1件以上必須（根拠なしの「確認済み」も同じ抜け道）。さらに、その状態が
    # 意味として要求する根拠種別（`_STATUS_REQUIRED_EVIDENCE_KINDS`）が `evidence` の `kind` に
    # 実在するかも確認する（設計書の根拠だけで `source_confirmed` は成立しない）。
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
    """`validate_item` の真偽判定版（`load_ledger` 用の薄いラッパー）。"""
    return not validate_item(item, expected_id=expected_id)


def load_ledger(dir: Path) -> LedgerSnapshot:
    """`dir` から manifest と items を読む。`dir` が無ければ空のスナップショット（manifest 無し）。

    壊れた JSON・読めないファイルは無効として記録し、例外は投げない（fail-safe・正典§3/§7）。

    契約（RV 高-1・2026-09-22 9巡目是正）: **symlink は一切辿らない**。台帳は model-shell が
    書ける領域（`run_dir/.tmp/investigation/`）にあり、リンク先を親権限（Sherpa 本体）で読むと
    本文・他会話の情報が漏れる——台帳ゲートは毎 attempt この関数を呼んで結果（未完了 id 等）を
    継続プロンプトへそのまま埋め込むため、退避・復元側の symlink 検査（provider.py）だけでは
    このライブ読み込み経路を防げない。
    - `dir` 自身が symlink なら空スナップショット（manifest なし・items なし）として扱う。
    - `manifest.json` が symlink ならリンク先を読まず manifest を無効（`None`）にする。
    - `items/` 自体が symlink なら中を列挙せず items を空にしたうえで manifest も無効にする
      （`LedgerSnapshot` に「items 側だけ疑わしい」を表す欄が無いため、登録集合そのものを
      信用しない側に倒す）。
    - 各 `items/*.json` が symlink ならリンク先を読まずその id を `invalid_ids` に入れる。
    - `items/` の列挙（`glob`）自体が失敗したら fail-safe で manifest を無効にする。
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
    """台帳の完了判定（正典§3「状態語彙」＝`final` の条件は「非終端がゼロ」・正典§4「壊れた台帳
    から `final` を生成しない」）。

    `required_extra`: 呼び出し側が追加で必須にする根拠種別（既定は空タプル＝現行と完全に同じ
    結果）。`EVIDENCE_REQUIRED_STATUSES` の item の必須集合へ `required_checks` との和集合として
    効く——モデル自身が `required_checks` に含めなかった種別でも呼び出し側の事情で完了判定に
    足せる（例: 範囲にソースがある調査は `source` を必須にする。詳細は `_missing_required_kinds`
    docstring）。理由付きの終端（`REASON_REQUIRED_STATUSES`）は対象外。

    `reviews`/`require_review`（COD-18 ⑤・既定は空タプル/`False`＝現行と完全に同じ結果。
    呼び出し側＝Codex の標準モードだけが `require_review=True` を渡す）: `reviews` の各要素は
    `validate_review_entry()` を満たすものだけを「有効な見直し」として数える（`terminal_count`
    欠落・1未満の要素はこの時点で無効——「見直しを書いた時点で終端項目が1件以上あった」ことを
    その場で検証済みの見直ししか有効扱いしない設計）。`require_review=True` のとき、有効な
    見直しが1件も無ければ `Verdict.review_missing=True`。1件以上あれば、`Verdict.review_pending_ids`
    （昇順タプル）に、いずれかの見直しが `added_items` に挙げた item id のうち登録集合に無い・
    `snapshot.items` に無い（欠落/無効）・状態が終端でない、のいずれかに該当するものをまとめる。
    どちらか一方でも真/非空なら `complete=False`（`require_review=False` のときはこの2項目を
    一切見ない＝常に偽/空）。

    `require_continuation_resolved`（COD-18 ⑤ RV是正3巡目・既定 `False`）: `True` のとき、
    `pending_continuation_review(reviews)`（下記・単一の純関数）が非 `None`（＝まだ解決していない
    「追加の観点」の義務が残っている）なら `Verdict.review_missing=True` にする（有効な見直しが
    1件以上あっても）。呼び出し側（provider.py）は「続き」で前ターンの未投資観点を注入した
    ターンだけこれを `True` にする——義務の有無の判定自体は呼び出し側の事情（何件目以降かの
    カウント等）を一切持たず、見直しの列の内容だけから `pending_continuation_review()` が決める
    （「最後の見直し」だけを見ると、間に `insufficient`／`added_items` 空の見直しを挟んだときに
    義務が消えてしまう穴があった・RV是正3巡目）。`require_continuation_resolved=False`（既定・
    「続き」以外の全ターン）では義務の有無を一切見ない——義務が残っていても、そのターン自身の
    完了は妨げない（回答末尾の定型文・退避の判断は別途 `pending_continuation_review()` を直接
    使う・下記）。

    `complete=True` は次の**すべて**を満たすときだけ:
    - 有効な manifest が存在し、その `items`（登録集合）が**空でない**（`manifest_invalid=False`）。
      manifest が無い／壊れている（`load_ledger` が `None` にした場合）／`items` が空配列なら、
      他の条件に関わらず `complete=False`——「未登録の item だけが終端」や「manifest 未作成」で
      complete にはしない（母集団が manifest 側でも確定していないため）。
    - 登録集合内で、非終端 item がゼロ・無効 item がゼロ・欠落（登録集合にあるが item ファイルが
      無い id）がゼロ（「item が1つ以上ある」は上の manifest 非空条件に包含される——登録集合が
      非空かつ欠落ゼロなら、登録された id は全て `snapshot.items` に存在することになる）。

    `unregistered_ids`（item ファイルはあるが manifest の `items`＝登録集合に無い id。状態が
    非終端でも、item 自体が無効でも同様）は complete の判定に**含めない**——親が把握していない
    情報は無視するが、報告はする（正典§3が明言していない点のこのモジュールでの裁定。子が親の
    把握範囲外に shard を作ってしまっても、親が既に把握している分の完了を妨げない。子が作った
    item を親に採らせるには、親が manifest にその id を登録し直す必要がある）。ただし登録集合
    そのものが空（`manifest_invalid=True`）なら、未登録 item だけで complete にはならない——
    上記の manifest 条件が先に効く。

    `non_terminal_ids`/`invalid_ids`（Verdict の報告欄）は**登録集合との積集合**——未登録の
    非終端/無効 item はここに現れず、`unregistered_ids` にのみ現れる（前掲の契約を報告欄でも
    一貫させる）。

    `unsatisfied`（`docs/archive/2026-09-22-Codex経路の精度・網羅性と費用の改善.md` §3.3が
    正典）: 登録集合内・有効（`snapshot.items` に入っている）・状態が `EVIDENCE_REQUIRED_STATUSES`
    の item のうち、`required_checks` に宣言した根拠種別の一部が `evidence` の `kind` に無い id
    → 足りない種別（ソート済みタプル）の対応。item 自体は `validate_item` の意味では有効
    （構造・状態固有の最小要件は満たす）でも、宣言した `required_checks` を満たしていなければ
    完了させない。未充足の id は、継続判定・無進捗判定が既存の経路でそのまま効くよう
    `non_terminal_ids` にも重ねて含める（`unsatisfied` は内訳の報告専用の欄）。`complete` は
    `unsatisfied` が空であることも必須条件にする。
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
            # RV是正（「続き」3巡目）: 義務の判定は `pending_continuation_review()`（単一の純関数・
            # 見直しの列全体を見る）に一本化——件数の基準値（旧 `min_review_count`）を外から渡す
            # 方式は、間に `insufficient`／`added_items` 空の見直しを挟むと基準値がずれて義務を
            # 見失う穴があった。`review_missing` を流用し、継続プロンプトが「見直しを書いて
            # ください」と促す既存の経路へそのまま乗せる。
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
                        pending.add(added_id)   # 欠落 or 無効（`snapshot.invalid_ids` 側）
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
    """根拠が必要な終端状態（`EVIDENCE_REQUIRED_STATUSES`）の item で、`required_checks` に宣言した
    種別と `required_extra`（呼び出し側が追加で必須にする種別）の**和集合**のうち `evidence` に
    無い種別（昇順）。空なら充足。他の状態は常に空（充足を求めない・理由付きの終端は根拠を
    問わない）。"""
    if item.get("status") not in EVIDENCE_REQUIRED_STATUSES:
        return ()
    present_kinds = {ev.get("kind") for ev in (item.get("evidence") or []) if isinstance(ev, dict)}
    required = set(item.get("required_checks") or []) | set(required_extra)
    return tuple(sorted(required - present_kinds))


def no_progress(prev: LedgerSnapshot, curr: LedgerSnapshot,
                *, required_extra: tuple[str, ...] = ()) -> list[str]:
    """`prev`→`curr` で status/evidence/reason のいずれも変わっていない**非終端** item（根拠種別が
    未充足の終端状態を含む）の id 一覧（決定的順序＝昇順）。「同じ item に状態遷移が無い attempt は無限継続しない」の判定材料
    （正典§3「状態語彙」）。

    両方に存在する id だけが対象（片方にしか無い id は比較不能として除外）。`curr` 側で終端に
    なった item は対象外（終端化そのものが進捗）。`prev`/`curr` のどちらかで無効だった id
    （`items` に載っていない）は比較対象にならない。

    `required_extra`: `ledger_complete()` に渡すものと**同じ値**を渡すこと——未充足判定
    （`_missing_required_kinds`）の基準を揃えないと、片方にだけ追加種別が効いて無変化の item を
    「進捗あり」と誤判定する。
    """
    out = []
    for item_id, cur_item in curr.items.items():
        prev_item = prev.items.get(item_id)
        if prev_item is None:
            continue
        status = cur_item.get("status")
        # 根拠種別が未充足の終端状態も非終端扱い（`ledger_complete` と同じ定義）——ここで除くと
        # 無変化の未充足 item が「進捗あり」と誤認され、継続の上限まで同じ催促を繰り返す。
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
    """書込先ディレクトリの経路に symlink が含まれていれば `PermissionError`。台帳 dir は
    model-shell から書ける場所（run_dir/.tmp）にあり、`items/` や台帳 dir 自体を外部への symlink に
    置き換えると、サンドボックスの外で動く MCP サーバがリンク先へ書いてしまう——読取側が symlink を
    一切辿らないのと対に、書込側も辿らない。呼出側は解決済み（realpath）のパスを渡す契約。"""
    raw = os.path.abspath(directory)
    if os.path.realpath(raw) != raw:
        raise PermissionError(errno.EPERM, "台帳の書込先に symlink が含まれる", raw)


def _write_json_atomic(path: Path, data: dict) -> Path:
    """tmp→`os.replace` の原子置換（`sherpa/json_io.write_json_atomic` と同じ方式の独立実装・
    葉ノード原則のため import はしない）。"""
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
    """1 item を `dir/items/{id}.json` へ原子的に書く（子が使う想定の書込関数）。manifest は
    書かない（親が `write_manifest_atomic` で別途管理する・正典§3「親子の分担」）。

    `item["id"]` からファイル名を決める。`id` が非文字列/空/`/`・`..`・先頭 `.` を含む場合は
    `ValueError`（パス逃れ防止）。`item` 自体の正規形（必須キー等）はここでは検証しない
    （書込途中の shard を許すため）——正規形の検証は `load_ledger`/`ledger_complete` 側が行う。
    """
    item_id = _validate_safe_id(item.get("id") if isinstance(item, dict) else item)
    dir = Path(dir)
    return _write_json_atomic(dir / _ITEMS_DIRNAME / f"{item_id}.json", item)


def write_manifest_atomic(dir: Path, manifest: dict) -> Path:
    """manifest を `dir/manifest.json` へ原子的に書く（親が使う想定の書込関数）。"""
    dir = Path(dir)
    return _write_json_atomic(dir / _MANIFEST_FILENAME, manifest)


# ---- 項目ごとの調査カバレッジ記録（COD-16・2026-09-29「調査の網羅と未確認の明示」提案書 §2）----
# `dir/coverage.jsonl`（台帳と同じ置き場）に、item 引数つきの検索・読取ツール呼出し1回ごとの
# 結果区分だけを追記する（`sherpa/mcp_server.py` が呼ぶ・本文/引数の中身は一切書かない）。
# 親・子（`spawn_agent` の worker）は同じ `SHERPA_MCP_LEDGER_DIR` を渡されるため、この
# ファイルも台帳の items/ と同じく親子共有——サイドカー（`.mcp_sidecar.jsonl`）を介さずに
# 子の観測を親と同じ場所へ集める（子の MCP 呼出は親の `--json` に現れないが、ファイル書込は
# プロセスに関係なく同じディレクトリへ届く）。
COVERAGE_OUTCOMES: frozenset[str] = frozenset({
    "hit", "no_hits", "truncated", "limit", "timeout", "unreadable", "error"})
_COVERAGE_FILENAME = "coverage.jsonl"
_COVERAGE_REQUIRED_KEYS = frozenset({"item", "tool", "outcome", "ts"})


def append_coverage_atomic(dir: Path, item_id: str, tool: str, outcome: str) -> None:
    """`dir/coverage.jsonl` へ1行（`item`/`tool`/`outcome`/`ts` の4キーだけ）を追記する。

    `item_id` は `_validate_safe_id` と同じ規則で検証する（`ValueError` で fail-loud・
    write_item_atomic と同じ規律——呼び出し元＝`mcp_server.py` が catch して fail-open にする）。
    `outcome` は `COVERAGE_OUTCOMES` の外なら `ValueError`（プログラミングエラー・呼び出し元は
    サーバ自身でモデル入力ではないため fail-loud でよい）。追記は "a" モードの単発 `write()`
    （`mcp_server._sidecar_append` と同じ流儀）——複数プロセス（親子）が同じファイルへ追記しても
    1行の長さが一般的な PIPE_BUF に収まる限り、行単位で混ざらない。

    `coverage.jsonl` 自体は `O_NOFOLLOW`・`O_NONBLOCK`（FIFO に差し替えられても開くところで
    待たない）で開き、fstat で通常ファイルであることを確かめてから
    書く——`_reject_symlinked_dir` は `dir` の経路だけを検査するため、ファイル自体が後から
    symlink に差し替えられる余地（TOCTOU）は別に塞ぐ必要がある（`load_ledger` が symlink を
    一切辿らないのと対に、書込側も辿らない）。symlink／通常ファイルでない場合は `OSError`
    （呼び出し元は上と同じ fail-open で catch する）。
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
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.ELOOP, "coverage.jsonl is not a regular file", str(path))
        os.write(fd, line)
    finally:
        os.close(fd)


def load_coverage(dir: Path) -> dict[str, tuple[str, ...]]:
    """`dir/coverage.jsonl` を読み、item id → その item に対する `outcome` の記録順タプルへ
    まとめる。壊れた行・型不正な行・語彙外の `outcome`・symlink・読めないファイルはいずれも
    fail-safe（無視するか空を返す・例外を投げない——`load_ledger` と同じ流儀）。
    """
    dir = Path(dir)
    path = dir / _COVERAGE_FILENAME
    if _is_symlink_fail_closed(path) or not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or set(entry.keys()) != _COVERAGE_REQUIRED_KEYS:
            continue
        item_id, outcome = entry.get("item"), entry.get("outcome")
        if not isinstance(item_id, str) or not item_id or outcome not in COVERAGE_OUTCOMES:
            continue
        out.setdefault(item_id, []).append(outcome)
    return {item_id: tuple(outcomes) for item_id, outcomes in out.items()}


# `not_found_in_scope` を `unverified` へ機械的に置き換えるときの理由の優先順位（COD-16 §2・
# 提案書§5「切り詰め・上限・時間切れ・読めない」の列挙順）。`search_truncated` は「truncated」
# （検索母集団の一部しか見ていない）と「limit」（同義・`mcp_server._coverage_outcome` の上限区分）
# の両方を受ける語彙——`UNVERIFIED_REASON_CODES` に「limit」専用の枠は無いためここで合流させる。
# `error`（道具そのものの失敗＝母集団を確認できていない）は「途中までは見た」`search_truncated`
# とは別原因のため、独立した `search_error` へ分ける（合流させない・降格時の理由文も別文にする——
# provider.py 側の `_UNVERIFIED_REASON_PHRASES`）。複数の outcome が混在する item は、このタプルの
# 先頭（最も原因が明確なもの）から順に一致した理由を採用する。
_DOWNGRADE_REASON_PRIORITY: tuple[tuple[str, frozenset[str]], ...] = (
    ("timeout", frozenset({"timeout"})),
    ("unreadable", frozenset({"unreadable"})),
    ("search_error", frozenset({"error"})),
    ("search_truncated", frozenset({"truncated", "limit"})),
)


def apply_unverified_downgrades(dir: Path, snapshot: LedgerSnapshot,
                                *, exclude_ids: frozenset[str] = frozenset()) -> LedgerSnapshot:
    """`not_found_in_scope` の**登録済み** item を、`coverage.jsonl`（`load_coverage`）の記録に
    基づき必要なら `unverified` へ機械的に置き換える（COD-16 §2・提案書§5 受け入れ）。

    置き換え規則:
    - この item に `item` 付きの呼出しが1回も記録されていない → `reason="not_searched"`。
    - 記録はあるが `truncated`／`limit`／`timeout`／`unreadable`／`error` のいずれかが1回でも
      あれば、`_DOWNGRADE_REASON_PRIORITY` の優先順位で reason を選び置き換える。
    - 記録が `hit`／`no_hits` だけ（問題が無い）なら置き換えない——「切り詰めの無い0件の検索
      だけなら `not_found_in_scope` のまま」（提案書§5）。

    `not_found_in_scope` 以外の状態（`unverified` を含む・モデル自身が直接その状態にした item・
    確認済み系の終端）は対象外——確認済みの状態を coverage の記録だけで書き換えない。未登録
    item（manifest の登録集合に無い id）も対象外（`ledger_complete` と同じ「登録集合だけを見る」
    契約）。

    `exclude_ids`（呼び出し側＝provider.py が渡す「このターンの開始時点で既に
    `not_found_in_scope` だった item id」の集合・既定は空）に含まれる id は、記録が1件も無い
    ときだけ対象外にする——前のターン（「続き」）で確定した項目を、記録が無いという理由だけで
    `unverified`/`not_searched` へ降格しない（記録を持ち越せなかった退避台帳でも同じ）。記録が
    あれば（このターンで調べ直した等）通常どおり判定する。

    `dir` 上の対象 item ファイルを実際に書き換える（`write_item_atomic`）。書換えに失敗した
    item（`OSError`）は元の状態のまま返す——fail-open（完了判定・回答生成を止めない）。
    戻り値は置き換え後の新しい `LedgerSnapshot`。
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
                continue   # hit/no_hits だけ＝置き換えない（not_found_in_scope のまま）
        new_item = {**item, "status": "unverified", "reason": reason}
        try:
            write_item_atomic(dir, new_item)
        except (OSError, ValueError):
            continue       # 書換え失敗は元の状態のまま（fail-open）
        new_items[item_id] = new_item
    return LedgerSnapshot(manifest=snapshot.manifest, items=new_items, invalid_ids=snapshot.invalid_ids)


# ---- 中間の見直し（COD-18 ⑤・`docs/proposals/課題管理簿.md` COD-18・利用者2026-10-01指示）----
# `dir/reviews.jsonl`（台帳・coverage.jsonl と同じ置き場）に、回答を確定する前の「目的・観点の
# 見直し」を1行1件で追記する。本文（purpose/summary/perspectives/extra_perspectives）を持つ点は
# item/manifest と異なる（正典§3の「台帳は本文を持たない」原則の対象外——見直しは人が後から
# 読む「調査の記録」の一部として本文を持つことが要件そのもの・COD-18 ⑤①〜③）。本文を持つ分、
# 各欄に上限文字数・配列件数の上限を設け、自由記述欄が無制限に肥大化する経路だけを塞ぐ。
REVIEW_VERDICTS: frozenset[str] = frozenset({"insufficient", "mostly_answered"})
# `summary` は purpose/perspectives より長い説明文を想定するため別上限（`SUBJECT_MAX_LEN` の2倍）。
REVIEW_SUMMARY_MAX_LEN = SUBJECT_MAX_LEN * 2
# 観点・足した/外した項目の配列件数の上限（1回の見直しでの宣言量を有界にする）。
_REVIEW_LIST_MAX_ITEMS = 50
# review entry の正規形（キー集合完全一致）。`ts`/`terminal_count` はサーバ（mcp_server.py）が
# 設定する（`manifest.created_at` と同じ分担——モデル自身には渡させない）。`terminal_count`
# （RV是正・順番の検証）: この見直しを書いた**時点**で台帳にあった終端 item の件数——`ledger_
# review_put` は受付時にこれを数え、1未満なら書込み自体を拒否する（`mcp_server.py` 側の責務）。
# `validate_review_entry()` が1以上を要求するため、`load_reviews()` が返す「有効な見直し」は
# 必ずこの条件を満たす——`ledger_complete()` 側は改めて現在の item 状態を見ずに、この記録済みの
# 値を信じればよい（見直しの後で item の状態が巻き戻っても、見直しの有効性自体は変わらない）。
_REVIEW_REQUIRED_KEYS = frozenset({
    "purpose", "perspectives", "summary", "added_items", "removed_items",
    "verdict", "extra_perspectives", "ts", "terminal_count"})
# `added_items`/`removed_items` の1要素の正規形（キー集合完全一致）。
_REVIEW_ITEM_REF_KEYS = frozenset({"id", "reason"})
# item id の書式（AGENTS.md の案内と同じ＝英数字・ハイフン・アンダースコアのみ）。
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_REVIEWS_FILENAME = "reviews.jsonl"
# RV是正（上限）: 見直しは本文（purpose/summary/perspectives 等）を持つため、item/coverage と
# 違い無制限の自由記述が何度も積み重なり得る——件数と `reviews.jsonl` の総バイト数の両方に
# 上限を設け、超える追記は拒否する（`append_review_atomic`）。読込（`load_reviews`）も同じ上限で
# 打ち切り、上限を超えて書かれたファイル（上限追加前の遺産・外部からの改ざん等）を丸ごと
# パースしようとしない。
REVIEWS_MAX_COUNT = 20
REVIEWS_MAX_BYTES = 256 * 1024  # 256 KiB


def _validate_text_list(value, field_name: str, *, require_non_empty: bool) -> list[str]:
    """`perspectives`/`extra_perspectives` 共通の検証（配列・件数上限・各要素が非空 str で
    `SUBJECT_MAX_LEN` 以下）。問題を人が読める理由の文字列のリストで返す（無ければ空）。"""
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
    """`added_items`/`removed_items` 共通の検証（配列・件数上限・各要素は `id`/`reason` の2キー
    ちょうど・`id` は item id の書式・`reason` は非空かつ `REASON_MAX_LEN` 以下）。"""
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
    """中間の見直し1件の正規形を検証し、problems を人が読める理由の文字列のリストで返す（問題が
    無ければ空リスト・真偽判定は `not validate_review_entry(...)`）。キー集合が
    `_REVIEW_REQUIRED_KEYS` の9つと完全一致する（item/manifest と同じ流儀）。`verdict` が
    `insufficient` のときは `extra_perspectives` を空配列にする（`mostly_answered` の意味だけが
    持つ「追加で調べられる観点」を、まだ足りないと判断した見直しに混在させない）。`terminal_count`
    は1以上の整数を要求する（RV是正・順番——見直しを書いた時点で終端 item が1件も無い見直しは
    無効。サーバが受付時に数えて埋める値のため、ここでは型・下限だけ検証する）。
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
    """1件の中間の見直しを `dir/reviews.jsonl` へ追記する。`entry` は呼び出し側
    （`mcp_server.py`）が `ts`/`terminal_count` を付けたうえで `validate_review_entry()` を通した
    完全な正規形を渡す契約——本関数自身は正規形を検証しない（`write_item_atomic` と同じ分担）。

    書込方式は `append_coverage_atomic` と同じ（単発 "a" write・`O_NOFOLLOW`・`O_NONBLOCK`・
    fstat で通常ファイルであることを確認してから書く——TOCTOU で symlink に差し替えられた
    ファイルへ書かない。複数プロセス（親子）が同じファイルへ追記しても、1行の長さが一般的な
    PIPE_BUF に収まる限り行単位で混ざらない）。`dir` 自身が symlink／ディレクトリでなければ
    `load_ledger` と同じ流儀で拒否する（`_is_symlink_fail_closed`・`PermissionError`）。`dir` の
    経路に symlink が含まれていれば同じく `PermissionError`（`_reject_symlinked_dir`）。

    RV是正（上限）: 追記後の件数が `REVIEWS_MAX_COUNT` を超える、または追記後のファイル総バイト数
    が `REVIEWS_MAX_BYTES` を超える場合は書き込まず `ValueError`（fail-loud・呼び出し元
    ＝mcp_server.py が `problems` としてモデルへ返す）。既存ファイルの読み直し→判定→追記の間は
    原子的ではない（`append_coverage_atomic` と同じ既存の複数プロセス前提＝1行が PIPE_BUF に
    収まる限り破損しないが、上限ちょうどの際に稀に多少超過する競合はあり得る——件数・バイト数は
    フェイルセーフの目安であり厳密な排他ではない）。
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


def load_reviews(dir: Path) -> tuple[dict, ...]:
    """`dir/reviews.jsonl` を読み、`validate_review_entry()` を満たす見直しだけを記録順の
    タプルで返す（壊れた行・型不正・語彙外・symlink・読めないファイルは fail-safe で無視—
    `load_coverage` と同じ流儀。例外は投げない）。

    `dir` 自身が symlink／ディレクトリでなければ空タプル（`load_ledger` と同じ流儀——`dir` だけを
    対象に symlink 判定すると、`dir` 自身が別ディレクトリへの symlink の場合に通り抜けて
    リンク先の `reviews.jsonl` を読んでしまう）。

    RV是正（上限）: 先頭 `REVIEWS_MAX_BYTES` バイト・`REVIEWS_MAX_COUNT` 件までしか読まない
    （超えた分は読み捨てる・`append_review_atomic` が書き込み時に同じ上限を課すため正常に書かれた
    ファイルはこの上限内に収まるが、上限追加前の遺産ファイルや外部からの改ざんで超過していても
    無制限にメモリへ展開しない）。
    """
    dir = Path(dir)
    if _is_symlink_fail_closed(dir) or not dir.is_dir():
        return ()
    path = dir / _REVIEWS_FILENAME
    if _is_symlink_fail_closed(path) or not path.is_file():
        return ()
    try:
        with path.open("rb") as f:
            raw = f.read(REVIEWS_MAX_BYTES + 1)
    except OSError:
        return ()
    try:
        text = raw[:REVIEWS_MAX_BYTES].decode("utf-8", errors="ignore")
    except ValueError:
        return ()
    out: list[dict] = []
    for line in text.splitlines():
        if len(out) >= REVIEWS_MAX_COUNT:
            break
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if validate_review_entry(entry):
            continue
        out.append(entry)
    return tuple(out)


def pending_continuation_review(reviews: tuple[dict, ...]) -> dict | None:
    """見直しの列（`load_reviews()` の戻り値・記録順。`validate_review_entry()` を満たさない
    要素は無視する）から、まだ「追加の観点」を調べる義務が残っている見直しを返す（無ければ
    `None`・純関数）。

    義務の定義: `verdict == "mostly_answered"` かつ `extra_perspectives` が非空の見直しのうち、
    その**後**（記録順で後）に `added_items` を1件以上持つ見直しがまだ1件も書かれていないもの。
    該当が複数あれば直近（最新）のものを返す——新しい義務が立った時点で、それより古い義務は
    役目を終えたとみなす（1つの台帳が複数の未解決の「追加観点」を並行して抱える設計にしない）。

    RV是正3巡目（COD-18 ⑤・`docs/proposals/課題管理簿.md` COD-18）: 回答末尾の定型文・「続き」の
    継続プロンプトへの注入・`ledger_complete()` の完了判定（`require_continuation_resolved=True`
    のとき）・退避判断（`force_retain`）の4箇所は、義務の有無・内容をこの関数**だけ**を根拠に
    判定する契約——以前は「最後の見直し」だけを見ていたため、間に `insufficient`／`added_items`
    が空の見直しを1回挟むと、直近の義務が（まだ解決していないのに）見えなくなる穴があった。
    """
    valid = [r for r in reviews if not validate_review_entry(r)]
    pending: dict | None = None
    for review in valid:
        if pending is not None and review.get("added_items"):
            pending = None
        if review.get("verdict") == "mostly_answered" and review.get("extra_perspectives"):
            pending = review
    return pending


# ---- 見直しの自由記述を表示・プロンプト注入へ出す前の正規化（RV是正）----
# `extra_perspectives` 等は見直しの自由記述（`validate_review_entry` は長さ・件数の上限だけを
# 課し、内容の文字種は制約しない）——回答末尾の定型文・「続き」の継続プロンプトへの注入・
# 調査の記録の Markdown 表示のいずれも、この1つの関数を通してから使う（3箇所で別々の正規化を
# 持たない＝一箇所を直せば全箇所に効く）。改行・制御文字で表示/プロンプトの構造を乱したり、
# Markdown の記号で表示崩れを起こしたりする経路を塞ぐ（本文を一切書かない契約を補強する最終防御
# ではなく、あくまで表示崩れ防止——内容そのものの正当性は `validate_review_entry` が担う）。
REVIEW_TEXT_PER_ITEM_MAX = 200
REVIEW_TEXT_TOTAL_MAX = 1000
# Markdown で特別な意味を持つ記号のうち、表示崩れの原因になりやすいものだけをエスケープする
# （`-`/`.`/`#` 等の行頭でだけ意味を持つ記号は、この関数の出力が常に文中・ラベルの後ろに置かれる
# 用途（「- 観点: …」「追加で調べられる観点: …」）のため対象にしない——過剰なエスケープで
# 可読性を落とさない）。`\` を最初に処理しないと後続のエスケープで二重にバックスラッシュが付く。
_REVIEW_TEXT_ESCAPE_CHARS = ("\\", "`", "*", "_", "[", "]", "|")


def sanitize_review_text(text) -> str:
    """見直しの自由記述1件を正規化する。改行・制御文字を取り除き（`str.isprintable()`。ASCII
    空白は printable 扱いのため残る）、`_REVIEW_TEXT_ESCAPE_CHARS` をエスケープし、
    `REVIEW_TEXT_PER_ITEM_MAX` 文字で切る。非 str・空文字は空文字を返す（純関数・例外を投げない）。
    """
    if not isinstance(text, str):
        return ""
    cleaned = "".join(ch for ch in text if ch.isprintable()).strip()
    for ch in _REVIEW_TEXT_ESCAPE_CHARS:
        cleaned = cleaned.replace(ch, "\\" + ch)
    return cleaned[:REVIEW_TEXT_PER_ITEM_MAX]


def sanitize_review_text_list(values, *, separator: str = "、") -> str:
    """`extra_perspectives` 等（str の配列）の各要素を `sanitize_review_text` で正規化してから
    `separator` で結合し、結合結果全体を `REVIEW_TEXT_TOTAL_MAX` 文字で切る（純関数）。空・非 str
    の要素は読み飛ばす。回答末尾の定型文・「続き」の継続プロンプトへの注入・調査の記録の
    Markdown 表示のいずれもこの関数を通す契約（RV是正）。
    """
    cleaned = [c for c in (sanitize_review_text(v) for v in (values or [])) if c]
    return separator.join(cleaned)[:REVIEW_TEXT_TOTAL_MAX]
