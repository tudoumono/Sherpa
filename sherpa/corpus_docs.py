"""資料フォルダの文書走査（doc_ledger の元）。フォルダ木そのものを走査し、各文書に `top_scope/phase/category`（rel_path の第 1〜3 セグメント）と
`path`(=rel_path) を持たせる。グラフは作らない（読み取り専用）。
設計: docs/design/scope.md「範囲（scope）＝フォルダ部分木のフィルタ」
"""
from __future__ import annotations

import os

import logging
import time
from collections import Counter
from pathlib import Path

from . import json_io, text_encoding
from . import scope_infer as si
from . import worlds
from .ingest import archive_extract, importance, text_kind
from .ingest.analyzers import registry as _analyzer_registry

_log = logging.getLogger("sherpa")

# 拡張子 → doctype（表示用・非コード分の固定表）。コード分はアナライザ登録簿（`registry.candidates`／`resolve_lazy`）で決まる
_NONCODE_DOCTYPE = {".md": "設計書", ".markdown": "設計書", ".txt": "テキスト"}
_MD_EXT = {".md", ".markdown", ".txt"}
# Office／PDF（決定的 MD 化の対象）
_OFFICE_DOCTYPE = {".docx": "Word", ".doc": "Word(旧)", ".xlsx": "Excel", ".xls": "Excel(旧)",
                   ".pptx": "PowerPoint", ".ppt": "PowerPoint(旧)", ".pdf": "PDF"}
# ラスタ画像（視覚読み取りアーム `vision` の対象）。vision 有効かつ VLM 実効可（`office_md.convertible_exts` に画像 ext がある）ときだけ文書として一覧に載せる
_IMAGE_DOCTYPE_LABEL = "画像"
# 内容判定（accepts）が必要だったが読み取れなかった時の明示 doctype
_UNREADABLE_DOCTYPE_LABEL = "読み取り不可"
# アーカイブ（zip/tar(.gz)/tgz）自身の台帳 1 行用 doctype（中のファイルは `archive_extract.sync_world_archives` が展開した木が `also=` で合流し、通常の行になる）
_ARCHIVE_DOCTYPE_LABEL = "アーカイブ(zip/tar)"


def _archive_row(rel: str, world: str) -> dict:
    """アーカイブ自身の台帳 1 行。`archive_extract.sync_world_archives` が `worlds.archive_manifest_path(world)` へ書いたサマリから組み立てる。
    マニフェストに行が無ければ `state="unreadable"`／`reason="archive_pending"`（一覧から消さない）。
    """
    manifest = json_io.read_json(worlds.archive_manifest_path(world), default={})
    summary = manifest.get(rel) if isinstance(manifest, dict) else None
    if not isinstance(summary, dict):
        return {"name": rel, "path": rel, "doctype": _ARCHIVE_DOCTYPE_LABEL, "branch": "archive",
               "analyzer": None, "state": "unreadable", "label": "展開待ち", "reason": "archive_pending",
               "md_path": None, **_scope_meta(rel)}
    status = summary.get("status")
    if status == "ok":
        extracted = summary.get("extracted_count", 0)
        skipped = (summary.get("skipped_sensitive", 0) + summary.get("skipped_nested", 0)
                  + summary.get("skipped_traversal", 0) + summary.get("skipped_symlink", 0))
        label = f"展開済み（{extracted}件" + (f"・対象外{skipped}件" if skipped else "") + "）"
        return {"name": rel, "path": rel, "doctype": _ARCHIVE_DOCTYPE_LABEL, "branch": "archive",
               "analyzer": None, "state": "ready", "label": label, "reason": None,
               "md_path": None, **_scope_meta(rel)}
    from .ingest.failure_reasons import REASON_CATALOG as _RC
    reason = summary.get("reason") or "other"
    label = _RC.get(reason, _RC["other"])["label"]
    return {"name": rel, "path": rel, "doctype": _ARCHIVE_DOCTYPE_LABEL, "branch": "archive",
           "analyzer": None, "state": "unreadable", "label": label, "reason": reason,
           "md_path": None, **_scope_meta(rel)}


class _HeadUnreadable(Exception):
    """`_read_head` が実ファイルを読めなかった（OSError）ことを示す内部シグナル。`classify_document()` が捕まえて `kind="unreadable"` で判定を打ち切る。"""


def classify_document(rel_path: str, ext: str, read_head, *, allow_content_sniff: bool = True,
                      text_quality=None) -> dict:
    """`_classify_document_core()` の判定を返す。
    `text_quality`（省略可・zero-arg callable）: テキストとして読める（`_classify_verdict_reachable()` が True）場合だけ呼ぶ。
    `(encoding, ratio, majority_garbled)`（`text_encoding.detect_fd_quality` と同型）または None を返す。`text_encoding.quality_of` が:
    - `"ok"`: core の結果をそのまま返す。
    - `"partial"`: `result["encoding_partial"] = True` を足す（`kind`／`doctype` は変えない）。
    - `"undetermined"`: `kind`／`doctype` を未対応（`document`／`None`）へ書き換え、`unreadable_reason = "encoding_undetermined"` を足す。
    `text_quality is None`（既定）は補正しない。
    """
    result = _classify_document_core(rel_path, ext, read_head, allow_content_sniff=allow_content_sniff)
    if text_quality is None or not _classify_verdict_reachable(result):
        return result
    quality = text_quality()
    if quality is None:
        return result
    level = text_encoding.quality_of(quality[1], quality[2])
    if level == "ok":
        return result
    out = dict(result)
    out["encoding"] = quality[0]
    if level == "undetermined":
        out["kind"] = "document"
        out["doctype"] = None
        out["unreadable_reason"] = "encoding_undetermined"
    else:
        out["encoding_partial"] = True
    return out


def _classify_document_core(rel_path: str, ext: str, read_head, *, allow_content_sniff: bool = True) -> dict:
    """1 ファイルの分類（列挙・集計・状態 API が共有する単一の判定）。`classify_document()` が委譲する（直接は呼ばない）。
    `read_head`（`size` キーワードを受ける callable）は `registry.resolve_lazy` の内容判定にそのまま渡す。既定の accepts だけなら内容を読まない。
    `resolve_lazy` が読んだ head はキャッシュし、`_classify_generic_text` の内容推定で追加 I/O なしに再利用する。
    戻り値:
    - `{"kind": "code", "doctype", "analyzer"}` — `resolve_lazy` が担当を確定。
    - `{"kind": "document", "doctype": str | None, "had_code_candidates": bool}` — 担当なし＝資料の枠へ倒す。`doctype` は `_NONCODE_DOCTYPE` にあればその値、無ければ None（呼び出し側が Office／画像／未対応へ倒す）。
      秘匿名（`text_kind.is_sensitive`）で早期 return した場合だけ `"sensitive": True` を持つ（呼び出し側が Office／画像の拡張子分類へ再度倒して秘匿ファイルを採用しないため）。
    - `{"kind": "unreadable", "had_code_candidates": True}` — 内容判定が必要だったが読めなかった（次点アナライザへ進まず打ち切る）。
    `accepts()` が全滅した場合、候補のいずれかが `fallback_to_text_kind_when_declined=True` なら、早期 return せず通常の内容推定へ進める。
    担当なしの拡張子は、`.md`／`.txt` 等の資料表にも無ければ `_classify_generic_text()`（軽量テキスト枠）へ回す（Office／画像は対象外のまま返す）。軽量テキスト枠が `"code"` と判定したら `kind="code"`（`analyzer=None`）。
    `allow_content_sniff`（既定 True）: False なら第 1 段（拡張子マップ）で判定できない拡張子は `read_head()` を呼ばず `doctype=None` へ倒す（status 経路が資料フォルダ root を再解決しないため）。
    """
    # 秘匿ファイルは担当アナライザの有無によらず分類経路から外す。`sensitive: True` は呼び出し側が Office／画像分類へ再採用しないための旗
    if text_kind.is_sensitive(Path(rel_path).name, ext):
        return {"kind": "document", "doctype": None, "had_code_candidates": False, "sensitive": True}
    candidates = _analyzer_registry.candidates(rel_path)
    if not candidates:
        doctype = _NONCODE_DOCTYPE.get(ext)
        if doctype is not None:
            return {"kind": "document", "doctype": doctype, "had_code_candidates": False}
        return _classify_generic_text(rel_path, ext, read_head, had_code_candidates=False,
                                      allow_content_sniff=allow_content_sniff)
    # `resolve_lazy` が accepts() 判定用に読んだ head を捕らえ、内容推定へ回った場合に再利用する
    cached_head: list = []

    def _capturing_read_head(size: int = 4096) -> str:
        head = read_head(size=size)
        cached_head[:] = [head]
        return head

    try:
        analyzer = _analyzer_registry.resolve_lazy(rel_path, _capturing_read_head)
    except _HeadUnreadable:
        return {"kind": "unreadable", "had_code_candidates": True}
    if analyzer is not None:
        return {"kind": "code", "doctype": analyzer.doctype, "analyzer": analyzer, "had_code_candidates": True}
    doctype = _NONCODE_DOCTYPE.get(ext)
    if doctype is not None:
        return {"kind": "document", "doctype": doctype, "had_code_candidates": True}
    declined_allows_text_kind = any(
        getattr(a, "fallback_to_text_kind_when_declined", False) for a in candidates)
    return _classify_generic_text(rel_path, ext, read_head, had_code_candidates=True,
                                  allow_content_sniff=allow_content_sniff,
                                  declined_allows_text_kind=declined_allows_text_kind,
                                  cached_head=cached_head[0] if cached_head else None)


def _classify_generic_text(rel_path: str, ext: str, read_head, had_code_candidates: bool,
                           allow_content_sniff: bool = True,
                           declined_allows_text_kind: bool = False,
                           cached_head: str | None = None) -> dict:
    """軽量テキスト枠（`ingest.text_kind`）＝登録簿に候補が一つも無い拡張子のテキストファイル判定。
    登録アナライザの候補が居たが `accepts()` が全滅した場合（`had_code_candidates=True`）は対象外（既存の資料種別に該当しなければ未対応のまま `doctype=None`）。
    `declined_allows_text_kind=True`（拒否した候補が `fallback_to_text_kind_when_declined=True` を宣言）のときだけ、通常の内容推定（第 1 段の拡張子マップ→第 2 段の内容 sniff）へ進める。
    `cached_head` があれば第 2 段の内容推定はそれを使い、`allow_content_sniff` のゲートを迂回してよい（追加読み取りが無いため）。
    Office／画像、ノイズ・一時ファイル、秘匿ファイル、`worlds.is_semantic_control_path()` は対象外のまま `doctype=None`（内容も読まない）。
    `allow_content_sniff=False` なら第 2 段は行わず `doctype=None`。`read_head()` が失敗（`_HeadUnreadable`）したら `kind="unreadable"`。サイズ上限（8MiB）は呼び出し側の責務。
    """
    if ext in _OFFICE_DOCTYPE:
        return {"kind": "document", "doctype": None, "had_code_candidates": had_code_candidates}
    from .ingest import office_md
    if ext in office_md.IMAGE_EXT:
        return {"kind": "document", "doctype": None, "had_code_candidates": had_code_candidates}
    if had_code_candidates and not declined_allows_text_kind:
        return {"kind": "document", "doctype": None, "had_code_candidates": True}
    if worlds.is_semantic_control_path(rel_path):
        return {"kind": "document", "doctype": None, "had_code_candidates": False}
    name = Path(rel_path).name
    if text_kind.is_noise(name, ext) or text_kind.is_sensitive(name, ext):
        return {"kind": "document", "doctype": None, "had_code_candidates": False}
    kind = text_kind.classify_ext(ext)
    if kind is None:
        if cached_head is not None:
            head = cached_head  # accepts() 用に既に読んだ head を再利用（追加 I/O なし）
        elif not allow_content_sniff:
            return {"kind": "document", "doctype": None, "had_code_candidates": False}
        else:
            try:
                head = read_head()
            except _HeadUnreadable:
                return {"kind": "unreadable", "had_code_candidates": had_code_candidates}
        sniff = text_kind.sniff_content(head)
        if sniff == "binary":
            # 理由を明示する。`unreadable_reason` は `scan_report()` の理由別内訳専用で、`iter_world_documents()` は見ない（バイナリは一覧に出ない）
            return {"kind": "document", "doctype": None, "had_code_candidates": had_code_candidates,
                    "unreadable_reason": "binary"}
        kind = sniff
    if kind == "code":
        return {"kind": "code", "doctype": text_kind.CODE_DOCTYPE_LABEL, "analyzer": None,
                "had_code_candidates": had_code_candidates}
    return {"kind": "document", "doctype": text_kind.DOCUMENT_DOCTYPE_LABEL,
            "had_code_candidates": had_code_candidates}


def _classify_verdict_reachable(result: dict) -> bool:
    """`classify_document()` の戻り値から「本文をテキストとして読める（grep／精読／ES 索引の対象にしてよい）」かを導く単一の式。
    `kind=="code"`、または `kind=="document"` で `doctype` が付けば True。`unreadable`、`doctype` の付かない `document`（秘匿名・Office／画像・declined 拡張子・内容が実質バイナリ）は False。
    `_safe_doc_path`・`grep_search`・`_safe_original_path`（file_head）・`reachable_as_text` がこの式を共有する（拡張子の許可リストでなく確定判定が可否を決める）。
    """
    return result["kind"] == "code" or (result["kind"] == "document" and result.get("doctype") is not None)


def reachable_as_text(rel_path: str, ext: str, read_head) -> bool:
    """本文を検索・精読・索引の対象として読めるか（grep／`_safe_doc_path`／ES 索引が共有する判定）。
    Office／PDF／画像は対象外（呼び出し側が派生 MD の実在確認へ分岐する）。秘匿ファイルは False。拡張子が登録済みかは問わず、内容がテキストと判定できれば True。
    """
    return _classify_verdict_reachable(classify_document(rel_path, ext, read_head))


def _read_head(rp: Path, size: int = 4096) -> str:
    """先頭 `size` バイトを読み UTF-8／CP932（不正バイトは置換）でデコードする（`registry.resolve_lazy` の内容判定用）。読めなければ `_HeadUnreadable`。
    バイナリで `size` バイトちょうど読んでからデコードする（`Analyzer.head_bytes` の宣言どおりのバイト境界を守るため）。
    """
    try:
        with rp.open("rb") as f:
            # 分類は先頭だけで足りる＝読み込み量を先頭 size バイトに保つ
            raw = f.read(size)
            encoding = text_encoding.detect_bytes(raw, complete=len(raw) < size or os.fstat(f.fileno()).st_size <= size)
            return text_encoding.decode(raw, encoding)
    except OSError as e:
        raise _HeadUnreadable(str(e)) from e


# 符号化の読み取り品質（`text_encoding.detect_fd_quality` の薄いラッパー）。`classify_document(..., text_quality=...)` へ渡す closure と
# ツール結果への注意喚起（`encoding_caution_for*`）の入口。`text_encoding` のキャッシュを共有する
def _text_quality_for(rp: Path) -> tuple[str, float, bool] | None:
    """`rp` の `(encoding, replacement_ratio, majority_garbled)`。開けなければ `None`。"""
    try:
        with rp.open("rb") as f:
            return text_encoding.detect_fd_quality(f.fileno())
    except OSError:
        return None


# ツール結果（read_doc／read_around／ripgrep_search 等）へ添える短い注意文（判定結果 "partial"／"undetermined" だけで決まる）
_ENCODING_CAUTION = {
    "partial": "この資料は文字コードの判別が不確実で、一部の文字が正しく読み取れていない可能性があります（要確認）。",
    "undetermined": "この資料は文字コードを判別できず、内容が正しく読み取れていません。",
}


def encoding_caution_for_ratio(ratio: float, majority_garbled: bool = False) -> str | None:
    """置換文字比率＋行の過半化け判定 → ツール結果への注意文（"ok" なら None）。"""
    return _ENCODING_CAUTION.get(text_encoding.quality_of(ratio, majority_garbled))


def encoding_caution_for(rp: Path) -> str | None:
    """`rp` の符号化品質に応じた注意文（読み取り不能／"ok" なら None）。"""
    tq = _text_quality_for(rp)
    if tq is None:
        return None
    return encoding_caution_for_ratio(tq[1], tq[2])


def read_full_text_and_raw(rp: Path) -> tuple[str, bytes]:
    """ファイルをバイナリで 1 回だけ読み、全文（UTF-8／CP932・不正バイトは置換）と生バイト列の両方を返す。読めなければ `OSError`。
    `ingest.world_graph` の Pass1 専用（構文解析用の全文と、アナライザごとの `head_bytes` でスライスする生バイト列を同一の読み取りから得る）。
    """
    raw = rp.read_bytes()
    # 全文はユニバーサル改行（CRLF／単独 CR → LF）に揃える。head 用の生バイト列は無加工
    encoding = text_encoding.detect_bytes(raw[:text_encoding.DETECT_CAP_BYTES],
                                          complete=len(raw) <= text_encoding.DETECT_CAP_BYTES)
    text = text_encoding.decode(raw, encoding).replace("\r\n", "\n").replace("\r", "\n")
    return text, raw


def _read_head_for_status(world: str, rel_path: str, size: int = 4096) -> str:
    """`status_document_doctype` 系の遅延読み取り（`world` から実体を解決して先頭を読む）。
    `accepts()` を上書きするアナライザの拡張子（`.html` 等）でだけ実際に呼ばれる。status 経路が資料フォルダ root の再解決（DB 往復）へ踏み込まないようにするための入口。
    """
    from . import documents
    rp = documents.resolve(rel_path, world)
    if rp is None:
        raise _HeadUnreadable("not_found")
    return _read_head(rp, size)


def status_document_doctype(rel_path: str, world: str, *, allow_content_sniff: bool = False) -> str | None:
    """文書状態 API が列挙する原本の doctype。対象外の付帯物・重要度設定ファイル自体は `None`。
    Office／PDF／画像は拡張子分類だけで数え、変換可否は判定しない。コード判定は `iter_world_documents`／`scan_report` と同じ `classify_document()` を使う。
    読み取れなかった場合は `_UNREADABLE_DOCTYPE_LABEL`（原本自体は存在するので `None` にしない）。`_重要度.txt` は classify の前に除外する。
    `allow_content_sniff`（既定 False）: 既定はホットパス（`manifest_doctype_count`）が資料フォルダ root を再解決しないため。単発の doc_id 解決（`verify_doc_exists`・`ext_doc` の配信可否）は True を渡す。
    """
    if importance.is_importance_control_path(rel_path):
        return None
    ext = Path(rel_path).suffix.lower()
    result = classify_document(
        rel_path, ext, lambda size=4096: _read_head_for_status(world, rel_path, size),
        allow_content_sniff=allow_content_sniff)
    return _doctype_for_count(result, ext)


def _doctype_for_count(result: dict, ext: str) -> str | None:
    """`classify_document()` の戻り値から集計用 doctype を導く単一の判定（`status_document_doctype`／`manifest_doctype_count*`／`scan_report` が共有）。
    `result["sensitive"]` は Office／画像の拡張子分類へ倒さず無条件で `None`（件数に入れない）。
    """
    if result.get("sensitive"):
        return None
    if result["kind"] == "unreadable":
        return _UNREADABLE_DOCTYPE_LABEL
    if result["kind"] == "code" or result["doctype"] is not None:
        return result["doctype"]
    if ext in _OFFICE_DOCTYPE:
        return _OFFICE_DOCTYPE[ext]
    from .ingest import office_md
    if ext in office_md.IMAGE_EXT:
        return _IMAGE_DOCTYPE_LABEL
    return None


def status_document_reachable(rel_path: str, world: str, *, allow_content_sniff: bool = False) -> bool | None:
    """文書として実在し、原本 DL・実在確認で配信／確認してよいかを 3 値で返す（曖昧な状態を True／False に丸めない）。
    - True: 「読める」と確定（コード、doctype 確定の資料、または Office／PDF／画像）。
    - False: 「対象外」と確定（秘匿名・重要度制御ファイル・実質バイナリ・declined 拡張子等）。
    - None: 判定できなかった（内容判定の読み取りが失敗）。
    呼び出し元は `is True` のときだけ配信／実在扱いにする（fail-closed）。`status_document_doctype()` は unreadable を非 None で返すため、配信可否には使わない。
    """
    if importance.is_importance_control_path(rel_path):
        return False
    ext = Path(rel_path).suffix.lower()
    result = classify_document(
        rel_path, ext, lambda size=4096: _read_head_for_status(world, rel_path, size),
        allow_content_sniff=allow_content_sniff)
    if result.get("sensitive"):
        return False
    if result["kind"] == "unreadable":
        return None
    if _classify_verdict_reachable(result):
        return True
    if ext in _OFFICE_DOCTYPE:
        return True
    from .ingest import office_md
    if ext in office_md.IMAGE_EXT:
        return True
    return False


def manifest_doctype_count(manifest: dict, world: str) -> int:
    """`manifest`（`ingest/worker.py` の `world_state()`／`_manifest()` が返す `rel -> [...]`）から doctype 対応の原本件数を数える。
    変換に失敗／未対応の Office／PDF／画像も数え、`status_document_doctype()` が None の付帯物だけ除く。
    `accepts()` を上書きする拡張子は rel ごとに資料フォルダ root を再解決して head を読むため、manifest 件数に比例したコストになる。
    root を握っている経路からは、`scan_report()` の `document_count` か `manifest_doctype_count_from_root()` を使い、本関数は直接呼ばない。
    """
    return sum(1 for rel in manifest if status_document_doctype(rel, world) is not None)


def manifest_doctype_count_from_root(manifest: dict, root) -> int:
    """`manifest_doctype_count()` と同じ判定（`_doctype_for_count`）を、解決済みの資料フォルダ root から数える（バックフィル用集計専用）。
    root から `ingest.world_graph.resolve_path`（lstat のみ）で辿るため DB 往復が無く、`world_lock` 保持中の呼び出しに向く。
    `root` が None なら全件を読み取り不能として扱う。
    """
    from .ingest import world_graph

    def _read_for(rel):
        def _read(size=4096):
            if root is None:
                raise _HeadUnreadable("world_unresolved")
            rp = world_graph.resolve_path(root, rel)
            if rp is None:
                raise _HeadUnreadable("not_found")
            return _read_head(rp, size)
        return _read

    count = 0
    for rel in manifest:
        if importance.is_importance_control_path(rel):
            continue
        ext = Path(rel).suffix.lower()
        result = classify_document(rel, ext, _read_for(rel), allow_content_sniff=False)
        if _doctype_for_count(result, ext) is not None:
            count += 1
    return count


def last_run_flags(world: str, *, deadline: float | None = None) -> list | None:
    """直近の ingest run（`store.get_latest_run_summary`）の `extraction_snapshot.flags`。
    `deadline`（省略可・`time.monotonic()` 系の絶対期限）: 残り時間を接続／SQL の statement timeout として渡す。超過時・DB 例外時は warning を残して `None`（＝確認できなかった。「blocked 無し」と混同させない）。
    run 自体が無い／flags が無ければ空リスト。`source_doc_ids` を持たない狭い SELECT を使う（`public_documents_page` が共有ロック中に呼ぶため、重い列を読まない）。
    """
    from . import store
    kwargs = {}
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _log.warning("last_run_flags: world=%s 呼び出し時点で既に期限切れのため直近 run を確認しません", world)
            return None
        kwargs = {"connect_timeout": remaining, "statement_timeout_ms": max(1, int(remaining * 1000))}
    try:
        last = store.get_latest_run_summary(world, **kwargs)
    except Exception as e:
        _log.warning("last_run_flags: world=%s 直近 ingest run の取得に失敗しました: %s", world, e)
        return None
    snap = (last or {}).get("extraction_snapshot")
    snap = snap if isinstance(snap, dict) else {}
    flags = snap.get("flags")
    return flags if isinstance(flags, list) else []


def last_run_blocked_docs(world: str, *, deadline: float | None = None) -> dict | None:
    """直近 run の blocked flag（doc 付きのみ）を `{doc: reason}` で返す。`None`＝確認できなかった（呼び出し元は空 dict と区別し、黙って「使えます」にしない）。
    既定 accepts の言語（cobol/copybook/jcl）は `resolve_lazy` が内容を読まないため、`classify_document` だけでは実読込の失敗を検知できない。実際にファイルを開いた直近 run の結果を突き合わせる材料。
    """
    flags = last_run_flags(world, deadline=deadline)
    if flags is None:
        return None
    return {f["doc"]: f["reason"] for f in flags
            if isinstance(f, dict) and f.get("action") == "blocked"
            and isinstance(f.get("doc"), str) and f.get("reason")}


def _office_convertible() -> set:
    """今 MD 化できる Office／PDF 拡張子（`office_md` が唯一の真実源）。"""
    from .ingest import office_md
    return office_md.convertible_exts()


def _image_convertible(conv: set) -> set:
    """今 MD 化できる画像拡張子（`office_md.convertible_exts()` と `office_md.IMAGE_EXT` の積）。PNG／JPEG は決定的 metadata 経路、その他は任意の vision 経路。"""
    from .ingest import office_md
    return conv & office_md.IMAGE_EXT


def _scope_meta(rel: str) -> dict:
    """rel_path → {top_scope, phase, category}（導出は scope_infer）。"""
    return si.rel_scope_meta(rel)


def _note_value(notes, key: str) -> str | None:
    """notes（`"key=value"` 文字列のリスト）から `key` の値を取り出す（best-effort・無ければ None）。"""
    if not isinstance(notes, list):
        return None
    prefix = key + "="
    for n in notes:
        if isinstance(n, str) and n.startswith(prefix):
            return n[len(prefix):] or None
    return None


def _coverage_notice_status(md_path: Path) -> str | None:
    """検索可能な partial notice なら coverage status を返す（表示用判定。既存 status API の互換カウンタだけを補う）。"""
    raw = json_io.read_json(Path(str(md_path) + ".meta.json"))
    if not isinstance(raw, dict):
        return None
    status = _note_value(raw.get("notes"), "coverage_status")
    return status if status in {"unsupported", "failed"} else None


def provenance_summary(md_path) -> dict | None:
    """派生 MD の来歴サイドカー（`{md_path}.meta.json`）から画面表示用の要約を作る（「どう読み取ったか」バッジ用・読むだけ）。返値（あれば）:
    - `method`（`ooxml`／`pdf_text`／`vision`）＝主たる読み取り方法。
    - `confidence`（0.0〜1.0）＝アームの確信度。
    - `legacy_backend`（`libreoffice`／`office_com` 等）＝旧形式を前段変換したバックエンド名（notes の `legacy_backend=…`）。
    - `has_conflicts`（True のみ）＝決定的マージで「別の読み方で追加内容」が見つかった文書。
    - `metafile_truncated`（該当時のみ）＝図（WMF/EMF）の文字・画像を上限で落とした記録（`lines_dropped`／`chars_dropped`／`figures_text_capped`／`figures_bitmaps_capped`／`bitmaps_excluded`）。
    - `pdf_pages`（画像で読んだ PDF のみ）＝`{total, over_limit, budget_cut, unread}`（元の総ページ数／上限で読まなかった数／時間予算で読まなかった数／読み取りに失敗・空だったページ数）。0 の項目は載せない。
    `md_path` 無し・サイドカー欠落／型不正・method 欠落・読取失敗は None（バッジを出さない）。
    """
    if not md_path:
        return None
    raw = json_io.read_json(Path(str(md_path) + ".meta.json"))  # 無い／壊れは None
    if not isinstance(raw, dict):
        return None
    method = raw.get("method")
    if not (isinstance(method, str) and method):  # method がアンカー（無ければ表示すべきものが無い）
        return None
    out: dict = {"method": method}
    conf = raw.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        out["confidence"] = float(conf)
    lb = _note_value(raw.get("notes"), "legacy_backend")
    if lb:
        out["legacy_backend"] = lb
    if raw.get("conflicts"):  # マージが差分を出した文書だけ
        out["has_conflicts"] = True
    pages = _pdf_pages_summary(raw.get("notes"))
    if pages:
        out["pdf_pages"] = pages
    mt = raw.get("metafile_truncated")
    if isinstance(mt, dict) and mt:  # 図（WMF/EMF）の文字・画像を上限で落とした記録（件数のみ）
        out["metafile_truncated"] = {k: v for k, v in mt.items() if isinstance(v, int) and not isinstance(v, bool)}
    return out


def _pdf_pages_summary(notes) -> dict | None:
    """画像で読んだ PDF の notes（`vision_arm._read_pdf` の `pdf_pages_*`）から読めなかったページの要約を作る。総ページ数の記録が無ければ None。"""
    def _int(key: str) -> int:
        v = _note_value(notes, key)
        return int(v) if v and v.isdigit() else 0

    total = _int("pdf_pages_total")
    if not total:
        return None
    out = {"total": total}
    for key, name in (("pdf_pages_over_limit", "over_limit"), ("pdf_pages_budget_cut", "budget_cut"),
                      ("pdf_pages_unread_count", "unread")):
        n = _int(key)
        if n:
            out[name] = n
    return out


def _text_oversize(rp: Path) -> bool:
    """`kind=="code"` の文書全般（登録アナライザ・軽量テキスト枠の汎用コード）に適用するサイズ超過判定（`text_kind.MAX_BYTES`＝8MiB・grep 上限と同じ）。
    `.md`／`.txt`・Office／画像には適用しない（呼び出し側が `kind == "code"` のときだけ呼ぶ）。巨大ファイルの全量読み込みによる OOM を防ぐ。
    stat 失敗はサイズ超過として扱わない。
    """
    try:
        return rp.stat().st_size > text_kind.MAX_BYTES
    except OSError:
        return False


def _size_exceeded_row(rel: str, doctype: str, branch: str) -> dict:
    """軽量テキスト枠のサイズ超過を台帳行へ（`failure_reasons` の `size_exceeded` を再利用）。
    `state="unreadable"` にし（`es_index.index_world`／`ingest.worker._ledger_rows` の索引スキップ判定と同じ値）、`doctype`／`branch` は判定済みの値を残す。
    """
    from .ingest.failure_reasons import REASON_CATALOG
    return {"name": rel, "path": rel, "doctype": doctype, "branch": branch, "analyzer": None,
            "state": "unreadable", "label": REASON_CATALOG["size_exceeded"]["label"],
            "reason": "size_exceeded", "md_path": None, **_scope_meta(rel)}


# `scan_report()` のフィールド追加前に保存された `worlds.last_scan_report` が持たないキーの集合。
# `routers.worlds._ingest_summary`・`ingest.worker._sync_impl` が `scan_report_missing_fields()` 経由で共有する
SCAN_REPORT_REQUIRED_KEYS = ("sensitive_excluded", "unreachable_as_text", "unreachable_as_text_by_ext",
                            "unreachable_by_reason", "encoding_partial_count", "walk_skipped")


def scan_report_missing_fields(rep) -> bool:
    """`rep`（`last_scan_report` 列の値）が dict だが `SCAN_REPORT_REQUIRED_KEYS` のいずれかを持たない（旧形式）なら True。dict でない（None 等）は False。"""
    return isinstance(rep, dict) and any(k not in rep for k in SCAN_REPORT_REQUIRED_KEYS)


def empty_scan_report() -> dict:
    """`scan_report()` の全ゼロ形（資料フォルダ未解決の返値と同形）。事前集計を持たない資料フォルダの「未集計」プレースホルダにも使う。"""
    return {"scanned": 0, "indexed": 0, "by_doctype": {}, "office_md": 0,
            "skipped_office": 0, "office_failed": 0, "skipped_other": 0, "skipped_ext": {},
            "analyzer_declined": 0, "analyzer_declined_as_document": 0, "unreadable": 0,
            "sensitive_excluded": 0, "unreachable_as_text": 0, "unreachable_as_text_by_ext": {},
            "unreachable_by_reason": {}, "encoding_partial_count": 0,
            "walk_skipped": {}, "document_count": 0}


def scan_report(world: str, *, expected_rels: frozenset[str] | None = None) -> dict:
    """資料フォルダ走査の内訳（インデックス済み・未対応形式・拡張子別の件数）。

    `expected_rels`（省略可）: 呼び出し元が持つ rel 集合。本走査の rel 集合と一致した場合だけ `document_count` を実値で返し、不一致（走査中に増減＝世代混在）なら `None` にして更新を保留する。省略時は比較しない。

    - `indexed`＝検索対象になる本文（ソース／設計書／テキスト＋MD 化できた Office＋OCR できた画像）。
    - `office_md`＝そのうち検索可能な Office MD（明示 partial notice を含む）。`office_failed`／`skipped_office` は notice があっても内容抽出に失敗／未対応なら併記するので `indexed` と排他ではない。
    - `skipped_other`＝その他（json 等）。Office／画像の MD 化済みは派生領域に `{rel}.md` があるかで判定。既定（画像変換が無効）では画像は `skipped_other` に落ちる。
    - `analyzer_declined`＝担当アナライザは居たが `accepts()` が全滅し、既存の資料種別にも該当しない＝未対応として残った件数（`skipped_other` と重複してよい）。
    - `analyzer_declined_as_document`＝`accepts()` が全滅したが既存の資料種別に該当し資料として扱われた件数。
    - `unreadable`＝内容判定が必要だったが読めず明示の失敗にした件数。`kind=="code"` のサイズ超過（8MiB・`_text_oversize`）もここへ合流する（`skipped_ext` にも計上）。
    - `document_count`＝`manifest_doctype_count()`／`status_document_doctype()` と同一の判定（`/ext/v1/capabilities` の `document_count` の材料）を、本ループが読んだ head を再利用して算出したもの。
    - `sensitive_excluded`＝秘匿名で分類自体をスキップした件数（拡張子内訳は持たない）。
    - `unreachable_as_text`＝grep／read_around／ES 索引のどれからも本文を読めないファイルの総数（`unreadable`＋`skipped_other` の未分類分＋`sensitive_excluded`）。`unreachable_as_text_by_ext`＝そのうち秘匿を除いた拡張子別内訳。
    - `unreachable_by_reason`＝`unreachable_as_text` のうち理由コードが判明しているものの内訳（`"encoding_undetermined"`／`"binary"`）。
    - `encoding_partial_count`＝対象外にはしないが置換文字が残る（`quality_of` が `"partial"`）ファイルの件数（`unreachable_as_text` には含めない）。
    - `walk_skipped`＝木の走査で数えなかったものの件数（`scope_infer.WALK_SKIPPED_KEYS`・0 の項目は載せない）。`scanned` に含まれないので、無いのではなく「見ていない」ことを区別して伝える。
    """
    wd = worlds.world_dir(world)
    if not wd:
        return empty_scan_report()
    derived = worlds.derived_md_dir(world)
    conv = _office_convertible()  # OOXML＋（バックエンド有なら）PDF
    image_exts = _image_convertible(conv)  # 画像（変換が有効なときのみ非空）
    (indexed, by, office_md_n, office_skip, office_fail, other, skipped_ext,
     analyzer_declined, analyzer_declined_as_document, unreadable, doc_count) = (
        0, Counter(), 0, 0, 0, 0, Counter(), 0, 0, 0, 0)
    sensitive_excluded = 0
    unreachable_ext: Counter = Counter()  # `unreadable`＋`other`（本文 readability 起因のみ）の拡張子別内訳
    unreachable_by_reason: Counter = Counter()  # 理由コードが判明している分だけの内訳
    encoding_partial_count = 0  # 対象外にしない「一部が化けている」件数
    scanned = 0
    walk_skipped: dict = {}
    # `expected_rels` 比較用（省略時は集めない）。manifest と同じ母集合にするため、下の `continue` より前で追加する
    actual_rels: set | None = set() if expected_rels is not None else None
    for rp, rel in si.safe_files(wd, skipped=walk_skipped):
        scanned += 1
        if actual_rels is not None:
            actual_rels.add(rel)
        if importance.is_importance_control_path(rel):  # 重要度設定ファイル自体は検索可能数・by_doctype に数えない
            continue
        ext = rp.suffix.lower()
        head_cache: dict = {}

        def _cached_read_head(size=4096, rp=rp, cache=head_cache):
            if size not in cache:
                cache[size] = _read_head(rp, size)
            return cache[size]

        result = classify_document(rel, ext, _cached_read_head,
                                   text_quality=lambda rp=rp: _text_quality_for(rp))
        if result.get("sensitive"):  # 秘匿名: 台帳にも by_doctype／skipped_ext にも入れない
            _log.warning("scan_report: 秘匿名のため対象外にしました（doctype=対象外 ext=%s）", ext)
            sensitive_excluded += 1  # 拡張子内訳は持たない（存在を推測させない）
            continue
        # 理由が判明している「対象外」の内訳（`unreachable_as_text` には含めない）。`encoding_partial_count` は `indexed` を加算する分岐でだけ数える（サイズ超過との二重計上を避ける）
        ur_reason = result.get("unreadable_reason")
        if ur_reason:
            unreachable_by_reason[ur_reason] += 1
        # `document_count`: `manifest_doctype_count`／`status_document_doctype` と同一の判定を、本ループが読んだ head を `_cached_read_head` で再利用して求める
        if _doctype_for_count(classify_document(rel, ext, _cached_read_head, allow_content_sniff=False),
                              ext) is not None:
            doc_count += 1
        if result["kind"] == "unreadable":
            unreadable += 1
            unreachable_ext[ext or "(拡張子なし)"] += 1
            continue
        if result["kind"] == "code":
            if _text_oversize(rp):
                unreadable += 1  # コード全般のサイズ超過は対象外（failure_reasons.size_exceeded）
                skipped_ext[ext] += 1
                unreachable_ext[ext or "(拡張子なし)"] += 1
                continue
            indexed += 1
            by[result["doctype"]] += 1
            if result.get("encoding_partial"):
                encoding_partial_count += 1
            continue
        if result["doctype"] is not None:
            if result["doctype"] == text_kind.DOCUMENT_DOCTYPE_LABEL and _text_oversize(rp):
                unreadable += 1  # 軽量テキスト枠のみ・サイズ超過は対象外
                skipped_ext[ext] += 1
                unreachable_ext[ext or "(拡張子なし)"] += 1
                continue
            indexed += 1
            by[result["doctype"]] += 1
            if result.get("encoding_partial"):
                encoding_partial_count += 1
            if result["had_code_candidates"]:  # 担当アナライザは居たが accepts() 全滅＝資料として扱う
                analyzer_declined_as_document += 1
            continue
        # 「担当なし」の内訳（未対応 vs 資料扱い）は、Office／画像という既存の資料種別に該当するかを確定してから振り分ける
        if ext in _OFFICE_DOCTYPE:
            if result["had_code_candidates"]:
                analyzer_declined_as_document += 1
            md = derived / (rel + ".md")
            if md.is_file():
                indexed += 1
                office_md_n += 1
                by[_OFFICE_DOCTYPE[ext]] += 1
                notice_status = _coverage_notice_status(md)
                if notice_status == "failed":
                    office_fail += 1
                elif notice_status == "unsupported":
                    office_skip += 1
            elif ext in conv:  # 変換可能形式だが派生 MD 無し＝変換失敗
                office_fail += 1
                skipped_ext[ext] += 1
            else:  # PDF／旧バイナリ（未対応）
                office_skip += 1
                skipped_ext[ext] += 1
        elif ext in image_exts:
            if result["had_code_candidates"]:
                analyzer_declined_as_document += 1
            if (derived / (rel + ".md")).is_file():  # image_exts ⊆ conv なので変換可否は判定済み
                indexed += 1
                by[_IMAGE_DOCTYPE_LABEL] += 1
            else:  # 文字が取れなかった＝変換失敗
                office_fail += 1
                skipped_ext[ext] += 1
        else:  # 既存の資料種別に該当しない＝未対応
            # 符号化が理由（`ur_reason` あり）ならアナライザは何も拒否していないので「未対応」には数えない
            if result["had_code_candidates"] and not ur_reason:
                analyzer_declined += 1
            other += 1
            skipped_ext[ext or "(拡張子なし)"] += 1
            unreachable_ext[ext or "(拡張子なし)"] += 1
    if expected_rels is not None and actual_rels != expected_rels:
        doc_count = None  # 世代混在（走査中の増減）＝実値を確定できないので更新保留
    return {"scanned": scanned, "indexed": indexed, "by_doctype": dict(by), "office_md": office_md_n,
            "skipped_office": office_skip, "office_failed": office_fail,
            "skipped_other": other, "skipped_ext": dict(skipped_ext),
            "analyzer_declined": analyzer_declined,
            "analyzer_declined_as_document": analyzer_declined_as_document, "unreadable": unreadable,
            "sensitive_excluded": sensitive_excluded,
            "unreachable_as_text": unreadable + other + sensitive_excluded,
            "unreachable_as_text_by_ext": dict(unreachable_ext),
            "unreachable_by_reason": dict(unreachable_by_reason),
            "encoding_partial_count": encoding_partial_count,
            "walk_skipped": {k: v for k, v in walk_skipped.items() if v},
            "document_count": doc_count}


def iter_world_documents(world: str, include_rag: bool = False, *, root=None, deadline: float | None = None,
                         files=None):
    """資料フォルダの文書一覧（rel_path＝doc_id・フォルダ由来の範囲メタ付き）。存在しない資料フォルダは空。
    本文（ソース／設計書／テキスト）＋MD 化できた Office（派生 `{rel}.md` あり）＋（画像変換が有効なときのみ）変換できた画像を載せる。
    未変換の Office は載せない（`scan_report.skipped_office` で可視化）。
    `include_rag=True` は rag 表現を正本とする消費者向け（ES 索引・グラフの言及エッジ）。`{rel}.rag.md` があればそれを `md_path` に採り、無ければ `{rel}.md`（`grep_tool.preferred_derived_name` と同じ優先順位）。
    `root`: 解決済みの資料フォルダ root を渡すと `worlds.world_dir()` を再度呼ばない。
    `files`: materialize 済みの `safe_files` の list を渡すと走査しない（`deadline` は無視される）。
    `deadline`: `scope_infer.safe_files` へそのまま渡す（超過時は `scope_infer.ScopeWalkDeadlineExceeded`）。
    `accepts()` の内容判定が必要なのに読めなかったコード拡張子の文書は `state="unreadable"`（`reason="read_failed"`）で載せる。
    `analyzer`＝担当アナライザの内部名（`kind=="code"` の登録アナライザのみ非 None）。軽量テキスト枠は `analyzer=None`。
    サイズ超過（8MiB）は `state="unreadable"`／`reason="size_exceeded"`（派生 MD もベクトル／グラフも作らず原文を grep／ES 全文の対象にする）。
    文字コードを判別できない原本は `state="unreadable"`／`reason="encoding_undetermined"`。バイナリは載せない。
    「一部が化けている」（`encoding_partial`）は `state="ready"` のまま行へ `encoding_partial: True` を足す（コード／テキスト資料の行のみ）。
    """
    wd = root if root is not None else worlds.world_dir(world)
    if not wd:
        return
    derived = worlds.derived_md_dir(world)
    derived_rag = worlds.derived_rag_dir(world)  # RAG 正本層
    conv = _office_convertible()  # OOXML＋（バックエンド有なら）PDF
    image_exts = _image_convertible(conv)  # 画像（変換が有効なときのみ非空）
    # アーカイブ取り込み: `worlds.archives_dir(world)` は zip/tar(.gz)/tgz の展開先。展開木の構成がそのまま doc_id になり、通常の文書と同じ分類・MD 参照が成立する
    entries = files if files is not None else si.safe_files(
        wd, deadline=deadline, also=worlds.archives_dir(world))
    for rp, rel in entries:
        if importance.is_importance_control_path(rel):  # 重要度設定ファイル自体は文書として扱わない
            continue
        if archive_extract.archive_kind(rel) is not None:
            # アーカイブ自身は `classify_document` を経由させず専用の 1 行にする（バイナリ扱いで一覧から消えるのを避ける）。
            # 秘匿判定はこの専用行を作る前に行う（秘匿名のアーカイブは存在も出さない）
            if text_kind.is_sensitive_doc_id(rel):
                _log.warning(
                    "iter_world_documents: 秘匿名のため対象外にしました（アーカイブ・doctype=対象外）")
                continue
            yield _archive_row(rel, world)
            continue
        ext = rp.suffix.lower()
        # コード判定は拡張子だけでなく accepts() まで見て確定する（`scan_report`／`status_document_doctype` と同じ `classify_document()` を共有）
        result = classify_document(rel, ext, lambda rp=rp, size=4096: _read_head(rp, size),
                                   text_quality=lambda rp=rp: _text_quality_for(rp))
        if result.get("sensitive"):  # 秘匿名: 台帳に載せない・Office／画像へ再採用しない
            _log.warning("iter_world_documents: 秘匿名のため対象外にしました（doctype=対象外 ext=%s）", ext)
            continue
        encoding_partial_kw = {"encoding_partial": True} if result.get("encoding_partial") else {}
        if result["kind"] == "unreadable":
            # 内容判定が必要だったが読み取れない＝判定を打ち切り、明示の失敗状態として出す
            yield {"name": rel, "path": rel, "doctype": None, "branch": None, "analyzer": None,
                   "state": "unreadable", "label": "読み取れません", "reason": "read_failed",
                   "md_path": None, **_scope_meta(rel)}
        elif result.get("unreadable_reason") == "encoding_undetermined":
            # `read_failed`／`size_exceeded` と同じ出し方（バイナリはこの行に含めない）
            from .ingest.failure_reasons import REASON_CATALOG as _RC
            yield {"name": rel, "path": rel, "doctype": None, "branch": None, "analyzer": None,
                   "state": "unreadable", "label": _RC["encoding_undetermined"]["label"],
                   "reason": "encoding_undetermined", "md_path": None, **_scope_meta(rel)}
        elif result["kind"] == "code":
            # `analyzer`＝担当アナライザの内部名（画面は `analyzer` を表示する）。軽量テキスト枠の汎用コードは `analyzer=None`
            analyzer_obj = result.get("analyzer")
            if _text_oversize(rp):
                yield _size_exceeded_row(rel, result["doctype"], "source")
            else:
                yield {"name": rel, "path": rel, "doctype": result["doctype"], "branch": "source",
                       "analyzer": analyzer_obj.name if analyzer_obj is not None else None,
                       "state": "ready", "label": "使えます", "reason": None,
                       "md_path": None, **_scope_meta(rel), **encoding_partial_kw}
        elif result["doctype"] is not None:  # 設計書／テキスト（accepts() 全滅のコード拡張子もここへ資料落ち）
            if result["doctype"] == text_kind.DOCUMENT_DOCTYPE_LABEL and _text_oversize(rp):
                yield _size_exceeded_row(rel, result["doctype"], "office")
            else:
                yield {"name": rel, "path": rel, "doctype": result["doctype"], "branch": "office", "analyzer": None,
                       "state": "ready", "label": "使えます", "reason": None,
                       "md_path": None, **_scope_meta(rel), **encoding_partial_kw}
        elif ext in _OFFICE_DOCTYPE:
            # include_rag 有効時は legacy `.md` より `.rag.md` を優先する（grep／ES／グラフが同じ物理ファイルを見る）。無効時は legacy のみ
            rag_md = derived_rag / (rel + ".rag.md") if (ext in conv and include_rag) else None
            if rag_md is not None and rag_md.is_file():
                yield {"name": rel, "path": rel, "doctype": _OFFICE_DOCTYPE[ext], "branch": "office",
                       "analyzer": None,
                       "state": "ready", "label": "使えます（RAG MD化）", "reason": None,
                       "md_path": str(rag_md), **_scope_meta(rel)}
            else:
                md = derived / (rel + ".md")
                if md.is_file():  # 通常 MD または明示 partial notice を検索対象にする
                    notice_status = _coverage_notice_status(md)
                    label = "使えます（MD化）" if notice_status is None else "未抽出箇所を検索できます"
                    yield {"name": rel, "path": rel, "doctype": _OFFICE_DOCTYPE[ext], "branch": "office", "analyzer": None,
                           "state": "ready", "label": label, "reason": notice_status,
                           "md_path": str(md), **_scope_meta(rel)}
        elif ext in image_exts:
            md = derived / (rel + ".md")
            if md.is_file():
                from .ingest import office_md
                label = (
                    "使えます（画像メタデータ・内容未解釈）"
                    if ext in office_md.RASTER_EVIDENCE_EXT
                    else "使えます（画像読み取り）"
                )
                yield {"name": rel, "path": rel, "doctype": _IMAGE_DOCTYPE_LABEL, "branch": "office", "analyzer": None,
                       "state": "ready", "label": label, "reason": None,
                       "md_path": str(md), **_scope_meta(rel)}


def world_documents(world: str, include_rag: bool = False, *, root=None, deadline: float | None = None,
                    files=None) -> list:
    """後方互換の materialized 一覧。大規模索引は `iter_world_documents` を使う。引数は `iter_world_documents` へそのまま渡す。"""
    return sorted(iter_world_documents(world, include_rag=include_rag, root=root, deadline=deadline, files=files),
                 key=lambda d: d["name"])
