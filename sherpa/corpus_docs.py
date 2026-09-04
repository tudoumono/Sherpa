"""world（登録ディレクトリ）の文書走査（鏡モデル・doc_ledger の seed 元）。

旧モデル（version 別 src/md・structure.json・auto-scope の layer/ambiguous）は**撤去**。
鏡では world の**フォルダ木そのもの**を走査し、各文書に `top_scope/phase/category`（rel_path の
第1-3セグメント）と `path`(=rel_path) を持たせる（範囲＝フォルダ・MIRROR §3）。グラフは作らない（read-only）。
特定テーマの名前は持たない（語彙はフォルダ/ファイル名＝データ由来）。
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path

from . import json_io
from . import scope_infer as si
from . import worlds
from .ingest import importance, text_kind
from .ingest.analyzers import registry as _analyzer_registry

_log = logging.getLogger("sherpa")

# 拡張子 → doctype（表示用・非コード分＝固定表）。コード分はアナライザ登録簿から毎回導出する
# （`_doctype_map()`）ので、新規言語をレジストリに足すだけで台帳・scan_report・原本 API に反映される。
_NONCODE_DOCTYPE = {".md": "設計書", ".markdown": "設計書", ".txt": "テキスト"}
_MD_EXT = {".md", ".markdown", ".txt"}
# Office/PDF（決定的MD化の対象・INGEST-MD §D5）。OOXML は常時変換、PDF はバックエンド導入時のみ、旧バイナリは未対応。
_OFFICE_DOCTYPE = {".docx": "Word", ".doc": "Word(旧)", ".xlsx": "Excel", ".xls": "Excel(旧)",
                   ".pptx": "PowerPoint", ".ppt": "PowerPoint(旧)", ".pdf": "PDF"}
# ラスタ画像（視覚読み取りアーム `vision`＝VLM の対象・tesseract の `ocr` アームは撤去済 2026-07-08）。
# doctype は Office と混同しない平文「画像」。**vision 有効 かつ VLM 実効可（＝office_md.convertible_exts
# に画像 ext が含まれる）ときだけ**文書として一覧に載せる（既定は vision 無効では convertible に画像が
# 入らない＝下の image_exts が空＝走査は従来どおり「その他」に落ちる＝scan_report 不変）。
# ⚠ 許容される過渡状態（RV Med #4・コード変更不要）: vision 無効化〜次回 sync（`office_md.arms_sig_drift`
# が検知して派生MD を作り直す）までの間は、既存の画像派生MD がディスクに残ったままなので grep/read_around
# からは引き続き見える（アーム構成変更全般に共通する性質・鏡の即時性は「次回 sync 完了時点」が基準）。
_IMAGE_DOCTYPE_LABEL = "画像"
# 内容判定（accepts）が必要だったが読み取れなかった時の明示 doctype。
_UNREADABLE_DOCTYPE_LABEL = "読み取り不可"


class _HeadUnreadable(Exception):
    """`_read_head` が実ファイルを読めなかった（OSError）ことを示す内部シグナル。

    `accepts()` を「空文字＝内容なし」と誤解させて次点アナライザへ誤配属しないよう、
    `classify_document()` がこれを捕まえて明示の失敗（`kind="unreadable"`）に倒し、そこで
    判定を打ち切る。
    """


def _code_doctype() -> dict:
    """コード分の拡張子→doctype（優先順で先勝ち＝拡張子衝突時はレジストリの優先順に従う）。

    毎回アナライザ登録簿から導出する（新規言語の追加やテストでの差し替えに追随する単一の真実源・
    §2.4）——値を固定 dict にキャッシュしない。
    """
    out: dict = {}
    for a in _analyzer_registry.known_analyzers():
        for ext in a.extensions:
            out.setdefault(ext, a.doctype)
    return out


def _doctype_map() -> dict:
    """表示用 doctype の合成表（非コード分の固定表＋コード分のレジストリ導出）。呼び出しごとに計算する。

    `scan_report`/`status_document_doctype`/`iter_world_documents` は `accepts()` まで見る
    `classify_document()` を共有するため、本関数はもう使わない——後方互換の `_DOCTYPE`
    属性アクセス（`tests/_corpus_expect.py` 等の拡張子メンバーシップ判定）専用に残す。
    衝突時は非コード分（`_NONCODE_DOCTYPE`）を優先する（レジストリ側の不備で `.md`/`.txt` 等の
    確立済み表示名を巻き込んで壊さない安全側の順序）。
    """
    return {**_code_doctype(), **_NONCODE_DOCTYPE}


def __getattr__(name: str):
    """後方互換: `_DOCTYPE`（旧・固定 dict）への属性アクセスを `_doctype_map()` へ委譲する。

    `tests/_corpus_expect.py` 等の外部参照が `from sherpa.corpus_docs import _DOCTYPE` の形で
    使えるよう維持しつつ、値そのものはレジストリ変更に追随させる（モジュール読み込み時には
    レジストリを引かない＝`doc_kinds.__getattr__` と同型）。
    """
    if name == "_DOCTYPE":
        return _doctype_map()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def classify_document(rel_path: str, ext: str, read_head, *, allow_content_sniff: bool = True) -> dict:
    """1ファイルの分類（列挙・集計・状態APIが共有する単一の判定）。

    `read_head`（zero-arg callable）は `registry.resolve_lazy` の内容判定にそのまま渡す——
    実際に必要な時（拡張子を要求する候補の誰かが `accepts()` を上書きしている時）だけ呼ばれる
    （既定 accepts のみなら内容を読まない・§7 裁定10）。

    戻り値:
    - `{"kind": "code", "doctype": ..., "analyzer": ...}` — `resolve_lazy` が担当を確定。
    - `{"kind": "document", "doctype": str | None, "had_code_candidates": bool}` — 担当なし
      （未登録拡張子、または登録済みだが `accepts()` 全滅）＝**資料の枠へ倒す**。実際に資料として
      扱われる（索引・一覧に出る）のは既存の資料種別（`doctype` が `_NONCODE_DOCTYPE` にある、
      または呼び出し側が Office/画像と判定する拡張子）に該当する場合のみ——該当しなければ
      未対応（§7 裁定10「既存の資料種別に該当するものは資料・それ以外は未対応」）。
      `doctype` は `_NONCODE_DOCTYPE` にあればその値、無ければ `None`（呼び出し側が
      Office/画像/未対応へ倒す）。`had_code_candidates`＝この拡張子を要求する登録済み
      アナライザが1つ以上あったか（`accepts()` 全滅で資料の枠へ落ちたことを可視化するのに使う・
      裁定10「その旨を内訳へ」）。
    - `{"kind": "unreadable", "had_code_candidates": True}` — 内容判定が必要だったが
      読み取れなかった。次点アナライザへは進まず（誤配属しない）ここで判定を打ち切る。

    「担当なし」（登録簿にもアナライザにも拾われなかった）拡張子は、`.md`/`.txt` 等の既存の
    資料表にも無ければ `_classify_generic_text()`（軽量テキスト枠・`ingest.text_kind`）へ回す。
    Office/画像（`_OFFICE_DOCTYPE`／`office_md.IMAGE_EXT`）はそちらで既存の資料種別として
    確定するため、ここでは対象外のまま返す（`doctype=None`・呼び出し側の既存分岐に委ねる）。
    軽量テキスト枠が `"code"` と判定した場合は `kind="code"`（`analyzer=None`）で返す——
    `branch=source`／層フィルタが登録アナライザと同じ経路で自然に効く（§ ING-TEXT-1）。

    `allow_content_sniff`（既定 True）: 軽量テキスト枠の第2段（未知拡張子・拡張子なしの
    `read_head()` 内容推定）を許すか。`False` にすると第1段（拡張子マップ）で判定できない
    拡張子は `read_head()` を一切呼ばず即座に `doctype=None` へ倒す——`status_document_doctype`/
    `status_document_requires_coverage`（`manifest_doctype_count` 経由で `_run_locked` の
    ホットパスから毎 sync 呼ばれる・元々「ファイルツリー走査を避ける」ために `manifest` 経由の
    軽量呼び出しとして設計された関数）が使う。ここで `read_head()` を呼ぶと、`status_document_
    doctype` が内部で使う `_read_head_for_status` が `documents.resolve`→`worlds.world_dir` を
    経由して world root を**再解決**（DB 往復もありうる）してしまい、`manifest_doctype_count` の
    「追加の走査/解決をしない」という設計契約を破る（実測: `_run_locked` の world root 解決を
    モックした単体テストで `store.get_world()` の返り値不足により落ちた・2026-09-02）。
    `scan_report`/`iter_world_documents`（どのみち全木を歩く経路）は既定 True のまま。
    """
    candidates = _analyzer_registry.candidates(rel_path)
    if not candidates:
        doctype = _NONCODE_DOCTYPE.get(ext)
        if doctype is not None:
            return {"kind": "document", "doctype": doctype, "had_code_candidates": False}
        return _classify_generic_text(rel_path, ext, read_head, had_code_candidates=False,
                                      allow_content_sniff=allow_content_sniff)
    try:
        analyzer = _analyzer_registry.resolve_lazy(rel_path, read_head)
    except _HeadUnreadable:
        return {"kind": "unreadable", "had_code_candidates": True}
    if analyzer is not None:
        return {"kind": "code", "doctype": analyzer.doctype, "analyzer": analyzer, "had_code_candidates": True}
    doctype = _NONCODE_DOCTYPE.get(ext)
    if doctype is not None:
        return {"kind": "document", "doctype": doctype, "had_code_candidates": True}
    return _classify_generic_text(rel_path, ext, read_head, had_code_candidates=True,
                                  allow_content_sniff=allow_content_sniff)


def _classify_generic_text(rel_path: str, ext: str, read_head, had_code_candidates: bool,
                           allow_content_sniff: bool = True) -> dict:
    """軽量テキスト枠（`ingest.text_kind`）＝**登録簿に候補が一つも無い**（＝真に未登録の）拡張子の
    テキストファイル判定。

    `classify_document()` の「担当なし」経路から呼ばれるが、`had_code_candidates=True`
    （登録アナライザの候補は居たが `accepts()` が全滅した＝§7 裁定10 の対象）は軽量テキスト枠の
    対象**外**——既存の資料種別（`.md`/`.txt`/Office/画像）に該当しなければ従来どおり「未対応」の
    まま `doctype=None` を返す（`test_declined_extension_...`/`test_grep_search_excludes_declined_
    registered_code_extension_...` 系が固定する既存契約。軽量テキスト枠は「未登録拡張子」だけが
    対象で、「登録されているが拒否された」ケースの扱いを緩めない）。

    Office/画像は既存の資料種別として呼び出し側（`scan_report`/`iter_world_documents`/
    `status_document_doctype`）が別途扱うため、ここで対象外のまま `doctype=None` を返し、内容も
    読まない（二重の読み取りをしない）。ノイズ/一時ファイル、秘匿ファイル慣習拡張子
    （`text_kind.SENSITIVE_EXT`＝`.env`/`.key`。`agentic_search.verify_doc_exists()` が
    「doctype 分類に無い付帯物は文書として実在しない」を秘匿ファイル非対応の安全側の性質として
    使っている・`tests/unit/test_ext2_evidence.py::test_verify_doc_exists_false_for_dotenv_and_key_files`
    参照）、`worlds.is_semantic_control_path()`（`semantic/concepts.json`／`semantic/l_extract.json`
    ＝旧・意味層機構〔GRAPH-SRC 2026-09-04 で撤去済み〕の world配下フォールバック位置に置かれた
    内部制御ファイル。`.json` を汎用コード扱いにしたことで偶然「ただの文書」に露出しないように
    する残置ガード）も同様（`doctype=None`＝呼び出し側の既存「未対応」集計へ自然に合流）。

    第1段（拡張子マップ）で判定できなければ、第2段（`read_head()` の先頭数KBの内容推定）へ——
    ただし `allow_content_sniff=False` なら第2段自体を行わず（`read_head()` を呼ばず）
    即座に `doctype=None` へ倒す（`classify_document` の同名引数 docstring 参照）。
    `read_head()` が失敗（`_HeadUnreadable`）したら `kind="unreadable"`（既存の read_failed
    経路と同じ形）。サイズ上限（8MiB）はここでは判定しない——実ファイルの stat を持つ
    呼び出し側（`iter_world_documents`/`scan_report`）の責務。
    """
    if ext in _OFFICE_DOCTYPE:
        return {"kind": "document", "doctype": None, "had_code_candidates": had_code_candidates}
    from .ingest import office_md
    if ext in office_md.IMAGE_EXT:
        return {"kind": "document", "doctype": None, "had_code_candidates": had_code_candidates}
    if had_code_candidates:
        return {"kind": "document", "doctype": None, "had_code_candidates": True}
    if worlds.is_semantic_control_path(rel_path):
        return {"kind": "document", "doctype": None, "had_code_candidates": False}
    name = Path(rel_path).name
    if text_kind.is_noise(name, ext) or text_kind.is_sensitive(name, ext):
        return {"kind": "document", "doctype": None, "had_code_candidates": False}
    kind = text_kind.classify_ext(ext)
    if kind is None:
        if not allow_content_sniff:
            return {"kind": "document", "doctype": None, "had_code_candidates": False}
        try:
            head = read_head()
        except _HeadUnreadable:
            return {"kind": "unreadable", "had_code_candidates": had_code_candidates}
        sniff = text_kind.sniff_content(head)
        if sniff == "binary":
            return {"kind": "document", "doctype": None, "had_code_candidates": had_code_candidates}
        kind = sniff
    if kind == "code":
        return {"kind": "code", "doctype": text_kind.CODE_DOCTYPE_LABEL, "analyzer": None,
                "had_code_candidates": had_code_candidates}
    return {"kind": "document", "doctype": text_kind.DOCUMENT_DOCTYPE_LABEL,
            "had_code_candidates": had_code_candidates}


def _read_head(rp: Path, size: int = 4096) -> str:
    """先頭 `size` 文字を読む（`registry.resolve_lazy` の内容判定専用）。読めなければ `_HeadUnreadable`。"""
    try:
        with rp.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(size)
    except OSError as e:
        raise _HeadUnreadable(str(e)) from e


def _read_head_for_status(world: str, rel_path: str) -> str:
    """`status_document_doctype` 系の遅延読み取り（`world` から実体を解決して先頭を読む）。

    `resolve_lazy` は既定 accepts のみの拡張子では呼ばない（§7 裁定10）ため、登録アナライザ側
    （現行3種はすべて既定 accepts）では実際には呼ばれない。**軽量テキスト枠**（`ingest.text_kind`）
    の第2段（未知拡張子・拡張子なしの内容推定）も、`status_document_doctype`/
    `status_document_requires_coverage` は `classify_document(..., allow_content_sniff=False)` で
    呼ぶため実際には呼ばれない——`manifest_doctype_count`（`ingest/worker.py` がホットパス
    （毎 sync）で「追加の走査/world root 再解決をしない」前提で使う）がこの関数を経由して
    `documents.resolve`→`worlds.world_dir` の再解決（DB 往復を伴いうる）に踏み込まないための
    安全策（2026-09-02・実測: `_run_locked` 系単体テストで world root 解決のモック不足により
    落ちた）。第1段（拡張子マップ）で判定できる拡張子は元々内容を読まない。
    """
    from . import documents
    rp = documents.resolve(rel_path, world)
    if rp is None:
        raise _HeadUnreadable("not_found")
    return _read_head(rp)


def status_document_doctype(rel_path: str, world: str) -> str | None:
    """文書状態APIが列挙する原本のdoctype。対象外の付帯物・重要度設定ファイル自体は ``None``。

    通常の文書台帳は検索可能になった文書だけを返すため、変換に失敗して派生物を持たない
    Office/画像を列挙できない。状態APIは原本側の対象集合を先に作る必要があるので、既存の
    文書種別とOffice/PDF/画像の拡張子分類だけをここで共有する。変換可否は判定しない。

    コード判定は `iter_world_documents`/`scan_report` と同じ `classify_document()` を使い、
    `accepts()` まで見て確定する。読み取れなかった場合は `_UNREADABLE_DOCTYPE_LABEL`
    （明示の失敗状態・原本自体は存在するので `None` にはしない）。
    `_重要度.txt`（`importance.is_importance_control_path`）は文書として扱わない（§5の除外契約・
    `/ext/v1/doc` の配信可否・`document_count` の両方がこの1関数を経由する）——classify_document
    より前に判定する（除外対象の内容は読まない）。

    `classify_document(..., allow_content_sniff=False)`: 軽量テキスト枠（`ingest.text_kind`）の
    第2段（未知拡張子・拡張子なしの内容推定）は行わない——`manifest_doctype_count`（`ingest/
    worker.py` がホットパスで使う）が想定する「追加の走査/world root 再解決をしない」契約を
    守るため（`_read_head_for_status` docstring 参照）。第1段（拡張子マップ）で判定できる
    軽量テキストは対象のまま（`text_kind.classify_ext` は内容を読まない）。
    """
    if importance.is_importance_control_path(rel_path):
        return None
    ext = Path(rel_path).suffix.lower()
    result = classify_document(rel_path, ext, lambda: _read_head_for_status(world, rel_path),
                              allow_content_sniff=False)
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


def manifest_doctype_count(manifest: dict, world: str) -> int:
    """`manifest`（`ingest/worker.py` の `world_state()`/`_manifest()` が返す `rel -> [...]` 辞書）から
    doctype 対応**原本**件数を数える。

    `/ext/v1/capabilities` の `document_count` はこの値を使う（変換に成功して検索可能になった
    件数＝`len(_ledger_rows(world))` ではない）。原本が存在するが変換に失敗/未対応な
    Office/PDF/画像も対象原本として数える——`status_document_doctype()` が None を返す付帯物
    （semantic/*.json 等）だけを除外する。登録アナライザ側（`accepts()` を上書きするものが無い
    現行構成）・軽量テキスト枠（`ingest.text_kind`）とも `status_document_doctype()` が
    `allow_content_sniff=False` で呼ぶため、ファイルツリー走査/world root 再解決は発生しない
    （`_read_head_for_status` docstring 参照）。
    """
    return sum(1 for rel in manifest if status_document_doctype(rel, world) is not None)


def status_document_requires_coverage(rel_path: str, world: str) -> bool:
    """文書状態の根拠として Canonical coverage を要求する形式か（Office/PDF/画像のみ True）。

    `allow_content_sniff=False`（`status_document_doctype` と同じ理由・docstring参照）。
    """
    ext = Path(rel_path).suffix.lower()
    result = classify_document(rel_path, ext, lambda: _read_head_for_status(world, rel_path),
                              allow_content_sniff=False)
    if result["kind"] in ("code", "unreadable") or result["doctype"] is not None:
        return False
    if ext in _OFFICE_DOCTYPE:
        return True
    from .ingest import office_md
    return ext in office_md.IMAGE_EXT


def last_run_flags(world: str, *, deadline: float | None = None) -> list | None:
    """直近の ingest run（`store.get_latest_run_summary`）の `extraction_snapshot.flags`。

    `deadline`（省略可・`time.monotonic()` 系の絶対期限・既定 None＝無期限＝既存呼び出し元は
    無変更）: `agentic_search.run_tool` の list_docs ツール打切り契約（`doc_ledger.documents_for`
    経由）に合わせ、残り時間を接続/SQL の statement timeout として `store.get_latest_run_summary`
    （RV2是正#a3・以前は `store.list_ingest_runs`）へ渡す。超過時・DB 例外時は呼ばず（または
    結果を待たず）打ち切り、warning ログを残して **`None`** を返す——「blocked 無し」と混同しない
    よう、呼び出し元（`last_run_blocked_docs`）に「確認できなかった」ことを明示的に伝える
    （黙って空へ丸めない）。run 自体が無い/flags が無ければ空リスト（＝正常に「blocked 無し」と
    確認できた）。

    `list_ingest_runs(world, limit=1)` は `source_doc_ids`（world の全文書名を持つ JSONB 配列・
    大規模 world では際限なく大きい）まで毎回読む——本関数は `doc_ledger.public_documents_page`
    が `world_lock_shared`（共有ロック）保持中に呼ぶ経路を持つため、この重い列を読むと O(N)
    （文書総数比例）の転送・deserialize を共有ロック区間へ持ち込んでしまう。`get_latest_run_summary`
    （`source_doc_ids` を持たない狭い SELECT・`GET /worlds/{wid}/status` と共用）に置き換える。
    """
    from . import store
    kwargs = {}
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _log.warning("last_run_flags(%s): 呼び出し時点で既に期限切れのため直近 run を確認しません", world)
            return None
        kwargs = {"connect_timeout": remaining, "statement_timeout_ms": max(1, int(remaining * 1000))}
    try:
        last = store.get_latest_run_summary(world, **kwargs)
    except Exception as e:
        _log.warning("last_run_flags(%s): 直近 ingest run の取得に失敗しました: %s", world, e)
        return None
    snap = (last or {}).get("extraction_snapshot")
    snap = snap if isinstance(snap, dict) else {}
    flags = snap.get("flags")
    return flags if isinstance(flags, list) else []


def last_run_blocked_docs(world: str, *, deadline: float | None = None) -> dict | None:
    """直近 run の blocked flag（doc 付きのみ）を `{doc: reason}` で返す。**`None`＝確認できなかった**
    （DB 例外・打切り期限超過）——呼び出し元は「blocked 無し」（空 dict）と区別し、対象文書を
    黙って「使えます」のままにしない（`doc_ledger.documents_for` 参照）。

    既定 accepts の言語（cobol/copybook/jcl）は `resolve_lazy` が内容を読まない短絡（§7 裁定10）
    のため、`classify_document`（列挙・`scan_report`・文書一覧が共有する判定）だけでは実際の
    読み取り失敗（`world_graph.build_world` Pass1 の実読込＋OSError 検知）を検知できない——
    実際にファイルを開く直近 ingest run の結果を突き合わせて上書きするための材料。
    """
    flags = last_run_flags(world, deadline=deadline)
    if flags is None:
        return None
    return {f["doc"]: f["reason"] for f in flags
            if isinstance(f, dict) and f.get("action") == "blocked"
            and isinstance(f.get("doc"), str) and f.get("reason")}


def _office_convertible() -> set:
    """今 MD化できる Office/PDF 拡張子（OOXML＋PDFバックエンド有無で動的）。office_md が唯一の真実源。"""
    from .ingest import office_md
    return office_md.convertible_exts()


def _image_convertible(conv: set) -> set:
    """今MD化できる画像拡張子。PNG/JPEGは決定的metadata経路、その他は任意vision経路。

    真実源は `office_md.convertible_exts()`（既に受け取った `conv`）と `office_md.IMAGE_EXT` の積＝
    ``convertible_exts``との積を真実源にし、OCR/VLMが無くてもPNG/JPEGは対象になる。
    """
    from .ingest import office_md
    return conv & office_md.IMAGE_EXT


def _scope_meta(rel: str) -> dict:
    """rel_path → {top_scope, phase, category}。導出は scope_infer に集約（rv-full B3）。"""
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
    """検索可能なpartial noticeならcoverage statusを返す。

    MDの存在と内容抽出成功を同一視しないための表示用判定。Canonicalな判定は
    Evidence/coverage台帳にあり、ここでは既存status APIの互換カウンタだけを補う。
    """
    raw = json_io.read_json(Path(str(md_path) + ".meta.json"))
    if not isinstance(raw, dict):
        return None
    status = _note_value(raw.get("notes"), "coverage_status")
    return status if status in {"unsupported", "failed"} else None


def provenance_summary(md_path) -> dict | None:
    """派生MD の来歴サイドカー（`office_md` が書く `{md_path}.meta.json`）から**画面表示用**の要約を作る。

    「どう読み取ったか」バッジ（ingest.html・S2）のデータ源。**追加の記録は一切せず**、既にある来歴
    （A1 のサイドカー）を読むだけ（表示のみ）。返値（あれば）:

    - `method`（`ooxml`／`pdf_text`／`vision`）＝主たる読み取り方法（アンカー）。
    - `confidence`（0.0〜1.0）＝アームの確信度。
    - `legacy_backend`（`libreoffice`／`office_com` 等）＝旧形式（.doc/.xls/.ppt）を前段変換した時のバックエンド名
      （notes の `legacy_backend=…` 由来・あれば）。
    - `has_conflicts`（True のみ）＝決定的マージ（A4）で「別の読み方で追加内容」が見つかった文書。

    ソース文書（`md_path` 無し）・サイドカー欠落/型不正・method 欠落は **None**（＝バッジを出さない・後方互換）。
    best-effort（読取失敗は None）。**per-doc の小さな JSON 読み**（`es_index._provenance_meta` と同型）で、
    md_path を持つ Office/画像枝の文書だけが読む（ソース文書は md_path 無し＝この関数を呼ぶ前に None で弾かれる）。
    """
    if not md_path:
        return None
    raw = json_io.read_json(Path(str(md_path) + ".meta.json"))    # 無い/壊れは None
    if not isinstance(raw, dict):
        return None
    method = raw.get("method")
    if not (isinstance(method, str) and method):                  # method がアンカー（無ければ表示すべきものが無い）
        return None
    out: dict = {"method": method}
    conf = raw.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        out["confidence"] = float(conf)
    lb = _note_value(raw.get("notes"), "legacy_backend")
    if lb:
        out["legacy_backend"] = lb
    if raw.get("conflicts"):                                       # A4 マージが差分を出した文書だけ（空/無しは付けない）
        out["has_conflicts"] = True
    return out


def _text_oversize(rp: Path) -> bool:
    """軽量テキスト枠（`ingest.text_kind`）だけに適用するサイズ超過判定（grep 上限と同じ 8MiB）。

    登録アナライザのコード・`.md`/`.txt`・Office/画像には適用しない（既存動作は無変更）——
    呼び出し側が `doctype` が `text_kind.CODE_DOCTYPE_LABEL`/`DOCUMENT_DOCTYPE_LABEL` の
    ときだけ呼ぶ。stat 失敗（消失・権限）はサイズ超過として扱わない（実読込の失敗は別経路
    （`unreadable`）が拾う対象で、本関数の責務ではない）。
    """
    try:
        return rp.stat().st_size > text_kind.MAX_BYTES
    except OSError:
        return False


def _size_exceeded_row(rel: str, doctype: str, branch: str) -> dict:
    """軽量テキスト枠のサイズ超過を台帳行へ（`failure_reasons` の既存語彙 `size_exceeded` を再利用）。

    `state="unreadable"` にする（`es_index.index_world`/`ingest.worker._ledger_rows` の唯一の
    索引スキップ・ステータス判定ゲートと同じ値＝新しい state 値を増やさない）。`doctype`/`branch`
    は判定済みの値をそのまま残す（「何のファイルか」は分かる状態を保つ・`doc_ledger` の
    blocked flag 反映と同じ流儀）。
    """
    from .ingest.failure_reasons import REASON_CATALOG
    return {"name": rel, "path": rel, "doctype": doctype, "branch": branch, "analyzer": None,
            "state": "unreadable", "label": REASON_CATALOG["size_exceeded"]["label"],
            "reason": "size_exceeded", "md_path": None, **_scope_meta(rel)}


def empty_scan_report() -> dict:
    """`scan_report()` の全ゼロ形（world 未解決の返値と同形）。

    `GET /worlds/{wid}/status` が事前集計（`worlds.last_scan_report`）を持たない world に対して
    「未集計」を示すためのプレースホルダとしても使う（呼び出し元はフォルダを歩かない）。
    """
    return {"scanned": 0, "indexed": 0, "by_doctype": {}, "office_md": 0,
            "skipped_office": 0, "office_failed": 0, "skipped_other": 0, "skipped_ext": {},
            "analyzer_declined": 0, "analyzer_declined_as_document": 0, "unreadable": 0}


def scan_report(world: str) -> dict:
    """world 走査の内訳（**取り込み状況の正直化**）。インデックス済み・未対応形式・拡張子別の件数を返す。

    `indexed`＝検索対象になる本文（ソース/設計書/テキスト＋**MD化できた Office**＋（OCR 有効時のみ）**OCR できた画像**）。
    `office_md`＝そのうち検索可能なOffice MD（明示partial noticeを含む。画像は別集計）。
    `office_failed`/`skipped_office`はnoticeが検索可能でも内容抽出に失敗/未対応なら併記するため、
    `indexed`と排他的な件数ではない。`skipped_other`＝その他（json 等）。
    Office/画像の MD化済みは派生領域（`worlds.derived_dir`）に `{rel}.md` があるかで判定（＝実態に一致）。
    **既定（OCR 無効）では画像は従来どおり `skipped_other` に落ちる**（`_image_convertible` が空集合＝この分類は不変）。

    `analyzer_declined`＝担当アナライザは居たが `accepts()` が全滅し、かつ既存の資料種別
    （md/office/txt/画像等）にも該当しない＝**未対応**として残った件数（`skipped_other` と重複してよい
    内訳・§7 裁定10「既存の資料種別に該当するものは資料・それ以外は未対応」）。
    `analyzer_declined_as_document`＝`accepts()` が全滅したが既存の資料種別（md/office/txt/画像）に
    該当し**資料として扱われた**件数（`indexed`／`office_md`／`office_failed`／`skipped_office` の
    いずれかと重複してよい内訳・Office/画像判定を確定してから加算する＝資料扱いを未対応に誤集計
    しない）。現行の登録拡張子構成（cobol/copybook/jcl のみ）では常に0＝コード拡張子と資料拡張子が
    排他のため。将来アナライザが既存の資料拡張子を要求した場合に非0になる。
    `unreadable`＝内容判定が必要だったが読み取れず明示の失敗にした件数（`iter_world_documents`/
    `status_document_doctype` と同じ `classify_document()` を共有）。**軽量テキスト枠**
    （`ingest.text_kind`＝未登録拡張子のテキストファイル）のサイズ超過（8MiB・grep 上限と同じ）
    もここへ合流する（`skipped_ext` にも計上・`failure_reasons.REASON_CATALOG["size_exceeded"]`
    と同じ理由）——新しいカウンタは増やさない。
    """
    wd = worlds.world_dir(world)
    if not wd:
        return empty_scan_report()
    derived = worlds.derived_md_dir(world)
    conv = _office_convertible()                       # OOXML＋（バックエンド有なら）PDF
    image_exts = _image_convertible(conv)              # 画像（OCR 有効時のみ非空・既定は空＝画像は下の else へ）
    (indexed, by, office_md_n, office_skip, office_fail, other, skipped_ext,
     analyzer_declined, analyzer_declined_as_document, unreadable) = (
        0, Counter(), 0, 0, 0, 0, Counter(), 0, 0, 0)
    scanned = 0
    for rp, rel in si.safe_files(wd):
        scanned += 1
        if importance.is_importance_control_path(rel):  # 重要度設定ファイル自体は検索可能数・by_doctype に数えない（§5）
            continue
        ext = rp.suffix.lower()
        result = classify_document(rel, ext, lambda rp=rp: _read_head(rp))
        if result["kind"] == "unreadable":
            unreadable += 1
            continue
        if result["kind"] == "code":
            if result["doctype"] == text_kind.CODE_DOCTYPE_LABEL and _text_oversize(rp):
                unreadable += 1                         # 軽量テキスト枠のみ・サイズ超過は対象外（failure_reasons.size_exceeded）
                skipped_ext[ext] += 1
                continue
            indexed += 1
            by[result["doctype"]] += 1
            continue
        if result["doctype"] is not None:              # 資料表（.md/.txt 等）にある拡張子
            if result["doctype"] == text_kind.DOCUMENT_DOCTYPE_LABEL and _text_oversize(rp):
                unreadable += 1                         # 軽量テキスト枠のみ・サイズ超過は対象外
                skipped_ext[ext] += 1
                continue
            indexed += 1
            by[result["doctype"]] += 1
            if result["had_code_candidates"]:           # 担当アナライザは居たが accepts() 全滅＝資料として扱う
                analyzer_declined_as_document += 1
            continue
        # 「担当なし」の内訳（未対応 vs 資料扱い）は、Office/画像という**既存の資料種別**に
        # 該当するかを確定してから振り分ける（先に analyzer_declined へ倒すと、資料として使える
        # Office/画像まで「未対応」に誤集計する・§7 裁定10）。
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
            elif ext in conv:                          # 変換可能形式だが派生MD無し＝変換失敗（壊れ/暗号化/スキャン=OCR要）
                office_fail += 1
                skipped_ext[ext] += 1
            else:                                      # PDF/旧バイナリ（MVP 未対応）
                office_skip += 1
                skipped_ext[ext] += 1
        elif ext in image_exts:                        # PNG/JPEGはmetadata、その他は任意vision経路
            if result["had_code_candidates"]:
                analyzer_declined_as_document += 1
            if (derived / (rel + ".md")).is_file():    # image_exts ⊆ conv なので変換可否は判定済み
                indexed += 1
                by[_IMAGE_DOCTYPE_LABEL] += 1          # 「画像」を doctype に（office_md_n＝Office/PDF 用なので触らない）
            else:                                      # OCR で文字が取れなかった＝変換失敗
                office_fail += 1
                skipped_ext[ext] += 1
        else:                                          # 既存の資料種別に該当しない＝未対応（§7 裁定10）
            if result["had_code_candidates"]:
                analyzer_declined += 1
            other += 1
            skipped_ext[ext or "(拡張子なし)"] += 1
    return {"scanned": scanned, "indexed": indexed, "by_doctype": dict(by), "office_md": office_md_n,
            "skipped_office": office_skip, "office_failed": office_fail,
            "skipped_other": other, "skipped_ext": dict(skipped_ext),
            "analyzer_declined": analyzer_declined,
            "analyzer_declined_as_document": analyzer_declined_as_document, "unreadable": unreadable}


def iter_world_documents(world: str, include_rag: bool = False, *, root=None, deadline: float | None = None,
                         files=None):
    """world の文書一覧（rel_path＝doc_id・フォルダ由来の範囲メタ付き）。**存在しない world は空**。

    本文（ソース/設計書/テキスト）＋ **MD化できた Office**（派生 `{rel}.md` あり）＋（OCR 有効時のみ）
    **OCR できた画像**を載せる。PDF/旧形式など未変換の Office は台帳に載せない（`scan_report.skipped_office`
    で可視化）。**既定（OCR 無効）では画像は載らない**（`_image_convertible` が空集合＝ES/検索対象にも入らない）。

    `include_rag=True`は**rag 表現を正本とする消費者向け**（ES索引＝`es_index.index_world`、
    グラフの言及エッジ＝辞書突合＝`world_graph.build_world`）。`{rel}.rag.md`があればそれを`md_path`に採り、
    無ければ legacy `{rel}.md`へ落ちる（`grep_tool.preferred_derived_name`と同じ優先順位＝
    grep/ES/グラフが同じ物理ファイルを見る）。通常MDを作れないimage-only PDFもこれで検索対象に入る。
    既定Falseなので台帳・preview・legacy索引の従来挙動は変えない。

    `root`（省略可）: 呼び出し側が既に world root を解決済みなら渡す（`worlds.world_dir()` を
    再度呼ばない）——文書列挙と重要度解決を同一 root から行いたい呼び出し元向け
    （`doc_ledger.public_documents`/`preview_documents` 参照）。
    `files`（省略可・キーワード専用）: 呼び出し側が既に `scope_infer.safe_files(wd)` を1回
    materialize（`list(...)`）済みなら渡す——与えられれば `si.safe_files` を呼ばない（`deadline`
    もこのとき無視される＝列挙自体は呼び出し側の責務。§③ 2026-09-01・`preview_service.build_preview`
    が文書列挙／重要度解決／診断の3消費者へ同じ list を配って二重の全木走査を避けるのに使う）。
    `deadline`（省略可・キーワード専用・既定 None＝無期限＝既存呼び出し元は無変更）:
    `scope_infer.safe_files` へそのまま転送する（PART-4 の `agentic_search.run_tool` が残り時間
    ベースで渡す・超過時は `scope_infer.ScopeWalkDeadlineExceeded` を送出）。

    コード拡張子で `accepts()` 内容判定が必要なのに読み取れなかった文書は `state="unreadable"`
    （`doctype`/`branch`/`analyzer` は `None`・`reason="read_failed"`）で載せる——資料としても除外しない。

    `analyzer`＝担当アナライザの内部名（`Analyzer.name`）。`kind=="code"` の文書だけ非 `None`
    （§7 裁定2の受入条件＝取り込み画面・影響分析の根拠表示で担当アナライザを参照できるようにする）。
    軽量テキスト枠（`ingest.text_kind`＝未登録拡張子のテキストファイル）は登録アナライザを持たない
    ため `analyzer=None`（`doctype` は `text_kind.CODE_DOCTYPE_LABEL`/`DOCUMENT_DOCTYPE_LABEL`）。
    サイズ超過（8MiB・grep 上限と同じ）は `state="unreadable"`／`reason="size_exceeded"`
    （`failure_reasons.REASON_CATALOG` の既存語彙を再利用・派生MD もベクトル/グラフも作らない
    ＝原文をそのまま grep/ES 全文の対象にする）。
    """
    wd = root if root is not None else worlds.world_dir(world)
    if not wd:
        return
    derived = worlds.derived_md_dir(world)
    derived_rag = worlds.derived_rag_dir(world)          # RAG 正本層（§8.1 三階層）
    conv = _office_convertible()                       # OOXML＋（バックエンド有なら）PDF
    image_exts = _image_convertible(conv)              # 画像（OCR 有効時のみ非空・既定は空＝画像は台帳に載らない）
    entries = files if files is not None else si.safe_files(wd, deadline=deadline)
    for rp, rel in entries:
        if importance.is_importance_control_path(rel):  # 重要度設定ファイル自体は文書として扱わない（§5）
            continue
        ext = rp.suffix.lower()
        # コード判定は拡張子だけでなく accepts() まで見て確定する（`resolve_lazy` は既定 accepts
        # （常に真）のアナライザしか候補に無ければ内容を読まない＝列挙コストは増やさない・§7 裁定10）。
        # scan_report/status_document_doctype と同じ classify_document() を共有する。
        result = classify_document(rel, ext, lambda rp=rp: _read_head(rp))
        if result["kind"] == "unreadable":
            # 内容判定が必要だったが読み取れない＝次点アナライザへ誤配属せず判定を打ち切り、
            # 明示の失敗状態として出す。
            yield {"name": rel, "path": rel, "doctype": None, "branch": None, "analyzer": None,
                   "state": "unreadable", "label": "読み取れません", "reason": "read_failed",
                   "md_path": None, **_scope_meta(rel)}
        elif result["kind"] == "code":
            # `analyzer`＝担当アナライザの内部名（`Analyzer.name`）。`doctype` は種別表示用の
            # 平文ラベル素材で、両者は現行構成では同値だが独立した概念（§7 裁定2の受入条件＝
            # 担当アナライザの来歴を一覧応答で参照できるようにする）——画面は `analyzer` を表示する。
            # 軽量テキスト枠の汎用コード（`text_kind`）には登録アナライザが無い＝`analyzer=None`。
            analyzer_obj = result.get("analyzer")
            if result["doctype"] == text_kind.CODE_DOCTYPE_LABEL and _text_oversize(rp):
                yield _size_exceeded_row(rel, result["doctype"], "source")
            else:
                yield {"name": rel, "path": rel, "doctype": result["doctype"], "branch": "source",
                       "analyzer": analyzer_obj.name if analyzer_obj is not None else None,
                       "state": "ready", "label": "使えます", "reason": None,
                       "md_path": None, **_scope_meta(rel)}
        elif result["doctype"] is not None:              # 設計書/テキスト（コード拡張子が accepts() 全滅時もここへ資料落ち）
            if result["doctype"] == text_kind.DOCUMENT_DOCTYPE_LABEL and _text_oversize(rp):
                yield _size_exceeded_row(rel, result["doctype"], "office")
            else:
                yield {"name": rel, "path": rel, "doctype": result["doctype"], "branch": "office", "analyzer": None,
                       "state": "ready", "label": "使えます", "reason": None,
                       "md_path": None, **_scope_meta(rel)}
        elif ext in _OFFICE_DOCTYPE:
            # rag 表現が正本（`grep_tool.preferred_derived_name` と同じ優先順位）: include_rag 有効時は
            # legacy `.md` より `.rag.md` を優先する（grep/ES/グラフが同じ物理ファイルを見る・
            # 2026-09-02-RAG表現の全形式展開と文脈保持.md §8 D1）。image-only PDF 等 legacy を作れない
            # 文書は rag.md だけが存在する。include_rag 無効時は従来どおり legacy のみを見る。
            rag_md = derived_rag / (rel + ".rag.md") if (ext in conv and include_rag) else None
            if rag_md is not None and rag_md.is_file():
                yield {"name": rel, "path": rel, "doctype": _OFFICE_DOCTYPE[ext], "branch": "office",
                       "analyzer": None,
                       "state": "ready", "label": "使えます（RAG MD化）", "reason": None,
                       "md_path": str(rag_md), **_scope_meta(rel)}
            else:
                md = derived / (rel + ".md")
                if md.is_file():                       # 通常MDまたは明示partial noticeを検索対象にする。
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
    """後方互換のmaterialized一覧。大規模索引は``iter_world_documents``を使う。

    `root`/`deadline`/`files`（省略可・キーワード専用・既定 None＝既存呼び出し元は無変更）:
    `iter_world_documents` へそのまま転送する。
    """
    return sorted(iter_world_documents(world, include_rag=include_rag, root=root, deadline=deadline, files=files),
                 key=lambda d: d["name"])
