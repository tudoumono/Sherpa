"""調査台帳による完了判定ゲートの定数・継続プロンプト・未確認項目の節と、台帳の退避・復元。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from ... import investigation_ledger
from ..base import _log, _scope_evidence_kinds
from .sandbox import _remove_dir_best_effort


# 調査台帳ゲート: `status=final` をそのまま信じず、台帳（`run_dir/.tmp/investigation/`）が完了しているかを確認してから受理する。
# 既存の自動継続（`SHERPA_CODEX_AUTO_CONTINUE`）とは独立の上限。
_LEDGER_CONTINUE_CAP = 10
_LEDGER_MANIFEST_MISSING_PROMPT = (
    "調査台帳を ledger_manifest_set と ledger_item_put で登録してから続けてください。"
    "ファイルを直接書かないでください。"
)
# manifest.json が存在するが内容が規約に合わない（必須キー欠落・`items` が空等）場合は「未作成」と区別し、通常の台帳継続と同じ枠（`_LEDGER_CONTINUE_CAP`）で修復を促す（壊れた台帳から final を生成しない）。
_LEDGER_MANIFEST_INVALID_PROMPT = (
    "manifest.json が規約に合いません。ledger_manifest_set に question_kind と全 id の items を"
    "渡して修復してください（items は空にしない）。ファイルを直接書かないでください。"
)


def _ledger_tool_detail_manifest(a: dict) -> str:
    items = a.get("items")
    return f"{len(items)}件" if isinstance(items, list) else ""


def _ledger_tool_detail_item(a: dict) -> str:
    status = a.get("status")
    known = investigation_ledger.NON_TERMINAL_STATUSES | investigation_ledger.TERMINAL_STATUSES
    # 引数はモデル生成で型の保証がない。非文字列を集合照合に通すと TypeError でストリーム処理ごと止まるため、先に型で弾く。
    return f"状態: {status}" if isinstance(status, str) and status in known else ""


def _ledger_tool_detail_review(a: dict) -> str:
    """`verdict`（閉じた語彙）だけを出す。purpose/summary/perspectives 等のモデル生成文字列は出さない（`_ledger_tool_detail_item` と同じ契約）。"""
    verdict = a.get("verdict")
    return f"判断: {verdict}" if isinstance(verdict, str) and verdict in investigation_ledger.REVIEW_VERDICTS else ""


# 「思考の流れ」の台帳ツール行の補足。id・subject・reason 等のモデル生成文字列は出さない（件数と状態語彙の閉集合だけ）。
_LEDGER_TOOL_DETAILS = {
    "ledger_manifest_set": _ledger_tool_detail_manifest,
    "ledger_item_put": _ledger_tool_detail_item,
    "ledger_status": lambda a: "",
    "ledger_review_put": _ledger_tool_detail_review,
}


def _ledger_continue_prompt(verdict: investigation_ledger.Verdict) -> str:
    """未完了の台帳へ、idと未充足の根拠種別だけを返す。本文・pathは含めない。
    `verdict.review_missing`/`verdict.review_pending_ids`（`require_review=True` の `ledger_complete()` だけが立てる）が立っていれば、中間の見直しが必要な旨を追記する（item id のみ）。
    """
    def _join(ids: tuple) -> str:
        return "、".join(ids) if ids else "なし"
    unsatisfied = "、".join(f"{item_id}（{'・'.join(kinds)} が未確認）"
                         for item_id, kinds in sorted(verdict.unsatisfied.items())) or "なし"
    _review_note = ""
    if verdict.review_missing:
        _review_note = "回答を確定する前に ledger_review_put で中間の見直しを1件書いてください。"
    elif verdict.review_pending_ids:
        _review_note = (f"見直しで足したと申告した項目が未終端です: "
                        f"{_join(verdict.review_pending_ids)}。終端にしてください。")
    return (
        "調査台帳に未完了の項目があります。"
        f"未完了: {_join(verdict.non_terminal_ids)}。"
        f"無効: {_join(verdict.invalid_ids)}。"
        f"欠落: {_join(verdict.missing_ids)}。"
        f"未充足: {unsatisfied}。"
        f"{_review_note}"
        "これらを終端状態にしてから最終回答を返してください。台帳に無い新しい主張は書かないこと。"
    )


# 「見直しの一巡」。台帳が complete と判定した直後、回答を確定する前に一度だけ Codex へ続きを頼み、計画外の発見・前提との食い違い・中身を調べていない語が残っていないかを点検させる。
# 閉じた定数文で、本文・資料名は含めない（調査中の指示を足して結論を急がせない）。名前は `_ledger_review_*`（見直し役の巡数 `_review_rounds` とは別の概念）。
_LEDGER_REVIEW_PROMPT = (
    "回答を確定する前の点検です（調べ方を変える指示ではありません）。"
    "下書きの回答・根拠について次の2点を確かめてください。"
    "1) 台帳のどの項目にも入らない発見（想定外の区分の値・分岐・呼び出し先など）や、"
    "計画の前提と食い違う事実が無いか。"
    "2) 定義はあるのに中身をまだ調べていない語（参照・分類名）が回答に残っていないか。"
    "1か2に当てはまるものがあれば、ledger_manifest_set で目録にその項目を足してから調べ、"
    "それから答え直してください。当てはまるものが無ければ答え直さなくてかまいません。"
    "答え直すときは、質問の形に合わせ（一覧なら一覧・範囲なら範囲）、分類名や参照で止めず"
    "具体的な値まで展開し、台帳と根拠にある事実以外は新たに足さないでください。"
)
# 見直しを頼む回数の上限（2回まで）。台帳継続の上限（`_LEDGER_CONTINUE_CAP`）とは別枠。目録を増やさずに台帳を未完了へ戻した見直しも1回に数える。
_LEDGER_REVIEW_CAP = 2


# 台帳にまだ解決していない「追加の観点」の義務（`investigation_ledger.pending_continuation_review()`）が残っていたら、回答の末尾に定型文「追加で調べますか？」を付ける（AI の自由記述文はそのまま流さない）。
# `extra_perspectives` の正規化は `investigation_ledger.sanitize_review_text_list` を「続き」の注入文・調査の記録の Markdown 表示と共通で使う。
# 義務の判定は `pending_continuation_review()` に一本化する。
_REVIEW_CONTINUATION_HEADER = "追加で調べられる観点"
_REVIEW_CONTINUATION_FOOTER = "続けて調べる場合は『続き』と送ってください。"


def _review_continuation_note(reviews: tuple[dict, ...]) -> str:
    """`reviews`（`load_reviews()` の戻り値・記録順）に未解決の「追加の観点」の義務があれば、回答末尾に付ける定型文を返す（無ければ空文字・純関数）。"""
    pending = investigation_ledger.pending_continuation_review(reviews)
    if pending is None:
        return ""
    extras_text = investigation_ledger.sanitize_review_text_list(pending.get("extra_perspectives"))
    if not extras_text:
        return ""
    return (f"{_REVIEW_CONTINUATION_HEADER}: " + extras_text + "。"
           + _REVIEW_CONTINUATION_FOOTER)


def _ledger_review_is_worse(pre: dict | None, post: dict | None) -> bool:
    """見直しの一巡の後の回答候補（`_candidate_final()` の戻り値）が、見直し前より悪くなったか（空になった・主張や根拠の数が減った）を判定する純関数。
    `post is pre`（見直しが新しい final を積まなかった場合を含む）は悪化なし、`pre` が無ければ悪化なし。
    """
    if pre is None or post is None or post is pre:
        return False
    if not (post.get("answer") or "").strip():
        return True
    pre_claims = pre.get("claims") or []
    post_claims = post.get("claims") or []
    if len(post_claims) < len(pre_claims):
        return True

    def _evidence_count(claims: list) -> int:
        return sum(len(c.get("evidence_refs") or []) for c in claims if isinstance(c, dict))

    return _evidence_count(post_claims) < _evidence_count(pre_claims)


# 「確認できなかった項目」節。AI は使わず、`item.reason`（モデルの自由記述）を転記せず、状態／閉じた理由語彙（`unverified` の `reason` のみ）から定型文へ機械的に変換する。
_UNCONFIRMED_STATUS_PHRASES = {
    "not_found_in_scope": "登録範囲内では見つかりませんでした",
    "unreadable": "読み取れませんでした",
    "unavailable": "確認できませんでした（利用できません）",
}
_UNVERIFIED_REASON_PHRASES = {
    "search_truncated": "検索が上限に達し、途中までしか確認できませんでした",
    "search_error": "検索が失敗し、確認できませんでした",
    "no_hits_only": "手がかりが見つからず確認できませんでした",
    "timeout": "確認が時間切れになりました",
    "unreadable": "資料を読み取れませんでした",
    # シェル（grep 等）で調べたが台帳へ記録が付かなかった場合もこの理由になり得るため、「未着手」と決めつけない言い回しにする。
    "not_searched": "この項目を調べた記録がありません",
}
_UNCONFIRMED_ITEMS_FALLBACK_PHRASE = "確認できませんでした"
# 台帳が未完了のまま受理されたターンの、登録済みだが非終端（pending/in_progress）の item・item ファイルが無い item（`missing_ids`）向けの定型文。
_UNCONFIRMED_ITEMS_INCOMPLETE_PHRASE = "調べ終わっていません"
_UNCONFIRMED_ITEMS_HEADER = "確認できなかった項目:"


def _unconfirmed_items_list(snapshot: investigation_ledger.LedgerSnapshot) -> list[dict]:
    """台帳（`apply_unverified_downgrades` 適用後）の登録済み item から、「確認できなかった項目」の構造化リストを組み立てる（件名と定型文だけ・item の本文/引用/reason の生テキストは使わない）。要素は `{"item": 件名, "reason": 定型文}`。対象が無ければ空リスト。
    `_unconfirmed_items_section`（headline 末尾の文章版）と Codex ジョブ API の `unconfirmed_items`（構造化版）が、この同じリストを写して共有する。
    対象: ① 確認できなかった終端状態（`UNCONFIRMED_STATUSES`）の item ② 登録済みだが非終端（`NON_TERMINAL_STATUSES`）の item ③ item ファイル自体が無い item（`missing_ids`）。壊れた item（`invalid_ids`）は対象外。
    """
    manifest_ids = set(snapshot.manifest["items"]) if snapshot.manifest is not None else set()
    out: list[dict] = []
    for item_id in sorted(manifest_ids):
        item = snapshot.items.get(item_id)
        if item is None:
            if item_id in snapshot.invalid_ids:
                continue
            out.append({"item": item_id, "reason": _UNCONFIRMED_ITEMS_INCOMPLETE_PHRASE})
            continue
        status = item.get("status")
        subject = (item.get("subject") or "").strip() or item_id
        if status in investigation_ledger.NON_TERMINAL_STATUSES:
            out.append({"item": subject, "reason": _UNCONFIRMED_ITEMS_INCOMPLETE_PHRASE})
            continue
        if status not in investigation_ledger.UNCONFIRMED_STATUSES:
            continue
        if status == "unverified":
            phrase = _UNVERIFIED_REASON_PHRASES.get(item.get("reason"), _UNCONFIRMED_ITEMS_FALLBACK_PHRASE)
        else:
            phrase = _UNCONFIRMED_STATUS_PHRASES.get(status, _UNCONFIRMED_ITEMS_FALLBACK_PHRASE)
        out.append({"item": subject, "reason": phrase})
    return out


def _format_unconfirmed_items_section(items: list[dict]) -> str:
    """`_unconfirmed_items_list` の戻り値を見出し＋箇条書きの文章に整形する（チャットの headline 末尾に付ける版）。対象が無ければ空文字列。"""
    if not items:
        return ""
    lines = [f"- {it['item']}（{it['reason']}）" for it in items]
    return _UNCONFIRMED_ITEMS_HEADER + "\n" + "\n".join(lines)


def _unconfirmed_items_section(snapshot: investigation_ledger.LedgerSnapshot) -> str:
    """`_unconfirmed_items_list(snapshot)` を文章に整形する（`_format_unconfirmed_items_section` の薄いラッパー）。"""
    return _format_unconfirmed_items_section(_unconfirmed_items_list(snapshot))


def _ledger_source_required_extra(world: str, scope_paths, layer) -> tuple[str, ...]:
    """このターンの台帳ゲートへ渡す `required_extra`。範囲にソースがある調査は、item の `required_checks` に source が無くても完了判定へ source を足す（ソースは常に必須）。
    `layer` は MCP へ実際に渡す実効の層（qa 以外は `None`）を渡すこと。
    `layer == "docs"` なら source を必須にしない（MCP のソース読取が層で拒否されるため）。それ以外の層では `_scope_evidence_kinds(world, scope_paths, layer)` の1つ目の要素（登録範囲に実在するか）で判定する。走査が判定不能（`None`）なら source を必須のままにする。
    """
    if layer == "docs":
        return ()
    scope_kinds = _scope_evidence_kinds(world, scope_paths, layer)
    if scope_kinds is None or "source" in scope_kinds[0]:
        return ("source",)
    return ()


def _ledger_progressed(
        prev_snapshot: investigation_ledger.LedgerSnapshot,
        curr_snapshot: investigation_ledger.LedgerSnapshot,
        *, required_extra: tuple[str, ...] = ()) -> bool:
    """`prev_snapshot` から `curr_snapshot` への間に「進捗」があったかを判定する純関数（台帳ゲートの無進捗 streak リセット判定）。
    未解決集合 U = 非終端 ∪ 欠落 ∪ 無効 で比較し、`U_prev − U_curr` が空でなければ進捗あり。
    未解決集合が縮んでいなくても、`no_progress()` の結果を登録済み id に限定してから登録済みの非終端集合と比較する。
    `required_extra`: 呼び出し側の `ledger_complete()` と同じ値を渡すこと（片方だけに足すと `stalled` と `curr_verdict.non_terminal_ids` が食い違う）。
    """
    prev_verdict = investigation_ledger.ledger_complete(prev_snapshot, required_extra=required_extra)
    curr_verdict = investigation_ledger.ledger_complete(curr_snapshot, required_extra=required_extra)
    prev_unresolved = (set(prev_verdict.non_terminal_ids) | set(prev_verdict.missing_ids)
                       | set(prev_verdict.invalid_ids))
    curr_unresolved = (set(curr_verdict.non_terminal_ids) | set(curr_verdict.missing_ids)
                       | set(curr_verdict.invalid_ids))
    if prev_unresolved - curr_unresolved:
        return True
    registered = set(curr_snapshot.manifest["items"]) if curr_snapshot.manifest is not None else set()
    stalled = set(investigation_ledger.no_progress(
        prev_snapshot, curr_snapshot, required_extra=required_extra)) & registered
    return stalled != set(curr_verdict.non_terminal_ids)


def _investigation_tree_has_symlink(root: Path) -> bool:
    """`root` 配下（`manifest.json`・`items/`・配下ファイル）に symlink が1つでもあれば `True`。model-shell は cwd 配下に書けるため、host 側ファイルへの symlink で退避経由に本文が実体化するのを防ぐ。
    `followlinks=False` で辿らず各エントリの symlink 判定だけで検出する。列挙失敗は握りつぶさず（`os.walk` の `onerror` で再送出し）`True` を返す（fail-closed）。
    """
    def _reraise(exc: OSError) -> None:
        raise exc
    try:
        if root.is_symlink():
            return True
        for dirpath, dirnames, filenames in os.walk(root, onerror=_reraise, followlinks=False):
            for name in dirnames + filenames:
                if os.path.islink(os.path.join(dirpath, name)):
                    return True
    except OSError:
        return True
    return False


def _copy_investigation_contract_files(src: Path, dst: Path) -> None:
    """`src` の台帳の正規ファイル（`manifest.json`・`items/*.json`・`coverage.jsonl`・`reviews.jsonl`）だけを `dst` へコピーする。それ以外のファイル・ディレクトリは無視する。
    `coverage.jsonl`（項目ごとの未確認）と `reviews.jsonl`（中間の見直し）も「続き」で消えないよう持ち越す。
    各ファイルをコピーする直前に `os.path.islink()` で個別確認し、symlink を検出したら `OSError` を送出して中止する（`_retire_investigation_ledger` の `except OSError` が fail-closed・警告1行にする）。
    """
    dst.mkdir(parents=True, exist_ok=True)
    manifest_src = src / "manifest.json"
    if manifest_src.is_file():
        if manifest_src.is_symlink():
            raise OSError(f"refusing to copy symlink: {manifest_src.name}")
        shutil.copy2(manifest_src, dst / "manifest.json")
    items_src = src / "items"
    if items_src.is_dir():
        items_dst = dst / "items"
        items_dst.mkdir(parents=True, exist_ok=True)
        for item_path in items_src.glob("*.json"):
            if item_path.is_file():
                if item_path.is_symlink():
                    raise OSError(f"refusing to copy symlink: {item_path.name}")
                shutil.copy2(item_path, items_dst / item_path.name)
    coverage_src = src / "coverage.jsonl"
    if coverage_src.is_file():
        if coverage_src.is_symlink():
            raise OSError(f"refusing to copy symlink: {coverage_src.name}")
        shutil.copy2(coverage_src, dst / "coverage.jsonl")
    reviews_src = src / "reviews.jsonl"
    if reviews_src.is_file():
        if reviews_src.is_symlink():
            raise OSError(f"refusing to copy symlink: {reviews_src.name}")
        shutil.copy2(reviews_src, dst / "reviews.jsonl")


def _restore_investigation_ledger(retired_dir: Path, investigation_dir: Path, tmp_root: Path) -> bool:
    """`retired_dir`（前ターンの退避）を `investigation_dir` へ原子的に復元する。`tmp_root` 配下の一時ディレクトリへ `_copy_investigation_contract_files` で全部コピーしてから `os.replace` で置き換える（途中失敗で部分復元が残り、退避を上書きしないため）。
    失敗したら一時ディレクトリを消し、`investigation_dir` を空に戻す。fail-open（例外を投げず警告1行）。戻り値: 復元できたら `True`。呼び出し側は `False` のとき、このターンでは退避台帳を削除・置換しないこと。
    """
    tmp_dir = tmp_root / f".investigation.restore-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        if tmp_dir.exists():
            _remove_dir_best_effort(tmp_dir)
        _copy_investigation_contract_files(retired_dir, tmp_dir)
        if investigation_dir.exists():
            _remove_dir_best_effort(investigation_dir)
        os.replace(tmp_dir, investigation_dir)
        return True
    except OSError as e:
        _log.warning("investigation ledger restore failed: %s", type(e).__name__)
        _remove_dir_best_effort(tmp_dir)
        if investigation_dir.exists():
            _remove_dir_best_effort(investigation_dir)
        (investigation_dir / "items").mkdir(parents=True, exist_ok=True)
        return False


def _retire_investigation_ledger(investigation_dir: Path, ledger_home: Path,
                                 *, required_extra: tuple[str, ...] = (),
                                 require_review: bool = False) -> bool:
    """未完了（または manifest 破損等で判定不能）の調査台帳を `ledger_home/investigation` へ退避する。complete なら退避先を削除する（ただし未解決の「追加の観点」の義務が残っていれば complete でも退避する）。
    `required_extra`: 台帳ゲートに渡したものと同じ値を渡すこと。`require_review`（既定 `False`）: 台帳ゲートと同じ値を渡すこと（`True` なら `reviews.jsonl` を読んで `ledger_complete(..., reviews=..., require_review=True)` で判定する）。
    退避の要否は `investigation_ledger.pending_continuation_review(reviews)` で内部で決める（回答末尾の定型文・継続プロンプト・完了判定と同じ純関数）。
    通常終了時は戻り値（実際に退避できたか）を `env["investigation"]["retained"]` に使う。
    「未作成の空ディレクトリ」と「作業データのある無効台帳」（manifest または items 配下にファイルがある）を区別し、後者は退避する。台帳ルート配下に symlink があれば退避を拒否する（`_investigation_tree_has_symlink`）。
    fail-open。戻り値: 退避（コピー）を実際に行ったら `True`、delete のみ／何もしない場合は `False`。
    """
    retire_dir = ledger_home / "investigation"
    try:
        reviews = investigation_ledger.load_reviews(investigation_dir) if require_review else ()
        verdict = investigation_ledger.ledger_complete(
            investigation_ledger.load_ledger(investigation_dir), required_extra=required_extra,
            reviews=reviews, require_review=require_review)
        force_retain = investigation_ledger.pending_continuation_review(reviews) is not None
        if verdict.complete and not force_retain:
            if retire_dir.exists():
                _remove_dir_best_effort(retire_dir)
            return False
        manifest_file_exists = (investigation_dir / "manifest.json").is_file()
        items_dir = investigation_dir / "items"
        has_any_item_file = items_dir.is_dir() and any(items_dir.glob("*.json"))
        if not (manifest_file_exists or has_any_item_file) or not investigation_dir.exists():
            return False
        if _investigation_tree_has_symlink(investigation_dir):
            _log.warning("investigation ledger retire skipped: symlink detected under investigation dir")
            return False
        # 原子的な置換: 一時ディレクトリへ丸ごとコピーしてから `os.replace`（同一ファイルシステム上の rename）で置き換える。
        tmp_retire_dir = ledger_home / f".investigation.tmp-{os.getpid()}-{os.urandom(4).hex()}"
        if tmp_retire_dir.exists():
            _remove_dir_best_effort(tmp_retire_dir)
        _copy_investigation_contract_files(investigation_dir, tmp_retire_dir)
        if retire_dir.exists():
            _remove_dir_best_effort(retire_dir)
        os.replace(tmp_retire_dir, retire_dir)
        return True
    except OSError as e:
        _log.warning("investigation ledger retire failed: %s", type(e).__name__)
        return False
