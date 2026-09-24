"""調査台帳の純関数部分（`docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md` §3/§7 が正典）。

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
  （`docs/proposals/2026-09-22-Codex経路の精度・網羅性と費用の改善.md` §3.3。何を確認すべきか
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
"""
from __future__ import annotations

import json
import errno
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

# 非終端＝まだ検証していない／検証中。終端＝検証結果が確定した状態（正典§3「状態語彙」）。
# 未知の状態文字列はどちらの集合にも属さない＝item は無効として扱われる。
NON_TERMINAL_STATUSES: frozenset[str] = frozenset({"pending", "in_progress"})
TERMINAL_STATUSES: frozenset[str] = frozenset({
    "source_confirmed",
    "spec_only",
    "conflict",
    "not_found_in_scope",
    "unreadable",
    "unavailable",
})

# 「確認不能・範囲外・読取不能」は**理由付き**の終端でなければならない（正典§3「状態語彙」）。
# `reason.strip()` が空ならその item は無効——理由の無い確認不能は、調べずに終端化する抜け道になる。
REASON_REQUIRED_STATUSES: frozenset[str] = frozenset({"not_found_in_scope", "unreadable", "unavailable"})
# 「確認済み」系の終端は根拠（evidence）1件以上が必須——根拠の無い「確認済み」も同じ抜け道になる。
EVIDENCE_REQUIRED_STATUSES: frozenset[str] = frozenset({"source_confirmed", "spec_only", "conflict"})

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

    `unsatisfied`（`docs/proposals/2026-09-22-Codex経路の精度・網羅性と費用の改善.md` §3.3が
    正典）: `EVIDENCE_REQUIRED_STATUSES`（`source_confirmed`/`spec_only`/`conflict`）の登録済み
    item のうち、`required_checks` と `ledger_complete()` の `required_extra` の**和集合**の一部が
    `evidence` に無い id → 足りない種別（ソート済み）の対応。item 自体は `validate_item` 上は有効
    （`snapshot.items` に入る）でも、この必須集合を満たしていなければ完了させない——`required_checks`
    を宣言しても対応する `evidence` を集めていない item が「確認済み」を名乗れてしまう穴を塞ぐ（例:
    `required_checks=[spec_doc, source]` の item が spec_doc の evidence だけで `spec_only` に
    なっても、source を確認していない以上は未充足）。継続判定・無進捗判定が既存の
    `non_terminal_ids` 経路でそのまま効くよう、未充足の id は `non_terminal_ids` にも含める
    （`unsatisfied` は内訳の報告専用）。
    """

    complete: bool
    non_terminal_ids: tuple[str, ...]
    invalid_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    unregistered_ids: tuple[str, ...]
    unsatisfied: dict[str, tuple[str, ...]]
    terminal_counts: dict[str, int]
    manifest_invalid: bool


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


def ledger_complete(snapshot: LedgerSnapshot, *, required_extra: tuple[str, ...] = ()) -> Verdict:
    """台帳の完了判定（正典§3「状態語彙」＝`final` の条件は「非終端がゼロ」・正典§4「壊れた台帳
    から `final` を生成しない」）。

    `required_extra`: 呼び出し側が追加で必須にする根拠種別（既定は空タプル＝現行と完全に同じ
    結果）。`EVIDENCE_REQUIRED_STATUSES` の item の必須集合へ `required_checks` との和集合として
    効く——モデル自身が `required_checks` に含めなかった種別でも呼び出し側の事情で完了判定に
    足せる（例: 範囲にソースがある調査は `source` を必須にする。詳細は `_missing_required_kinds`
    docstring）。理由付きの終端（`REASON_REQUIRED_STATUSES`）は対象外。

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

    `unsatisfied`（`docs/proposals/2026-09-22-Codex経路の精度・網羅性と費用の改善.md` §3.3が
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

    complete = (
        not manifest_invalid
        and not non_terminal_ids
        and not invalid_ids
        and not missing_ids
        and not unsatisfied
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
