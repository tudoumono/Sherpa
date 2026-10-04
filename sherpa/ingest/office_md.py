"""Office（OOXML）/PDF → 決定的 Markdown 変換と、派生物（MD・IR・Evidence・RAG）の生成・drift 判定。

OOXML を直接パースする（Office/LibreOffice 非依存）。
- .docx/.xlsx: document-ir 経由で人間向け MD にする（`human_md.render_docx`/`render_xlsx`）。
- .pptx: スライド順にテキストを出す。
- .pdf: テキスト層を抽出する（バックエンド pypdf）。到達不可なら None＝「未対応」。
- .doc/.xls/.ppt（旧バイナリ）: 直接は扱わず、legacy_backend で OOXML へ前段変換してから OOXML 経路へ委譲する（`arms/legacy_convert.py`）。
決定的（タイムスタンプ等を出さない・順序安定）。壊れたファイル/非 OOXML は例外を握って None。LLM は使わない。
設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import zipfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from .. import json_io
from ..env_int import env_int
from . import text_kind

# MD 変換（取り込み進行ログ）は専用ログ（sherpa.ingest.convert）へまとめる
_log = logging.getLogger("sherpa.ingest.convert")

# OOXML 名前空間
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

CONVERTIBLE_EXT = {".docx", ".xlsx", ".pptx"}                 # OOXML＝常時 MD化（外部ライブラリ不要）
RASTER_EVIDENCE_EXT = frozenset({".png", ".jpg", ".jpeg"})    # OCRなしで存在・位置・hashをEvidence化
LEGACY_OFFICE_EXT = frozenset({".doc", ".xls", ".ppt"})
EVIDENCE_EXT = frozenset({".xlsx", ".docx", ".pptx", ".pdf"}) | RASTER_EVIDENCE_EXT | LEGACY_OFFICE_EXT
PDF_EXT = {".pdf"}                                            # PDF はテキスト層を抽出（同梱既定バックエンド pypdf）
# ラスタ画像（視覚読み取りアーム `vision`＝VLM の対象）。vision 有効かつ VLM 実効可のときだけ MD 化候補になる
# （既定では画像は grep 専用の素ファイル）。
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}
OFFICE_EXT = CONVERTIBLE_EXT | PDF_EXT | set(LEGACY_OFFICE_EXT)   # Office/PDF 一括（未対応含む）


# Office/PDF 取り込みの入口サイズガード。openpyxl は全ブックをオブジェクトツリーに展開するため、
# ピークメモリが原本サイズの数倍〜数十倍になりうる。変換を試みる前に諦める安全弁。
_OFFICE_FILE_CAP_BYTES = env_int(
    "SHERPA_OFFICE_FILE_CAP_BYTES", 100 * 1024 * 1024, 1024 * 1024, 1024 * 1024 * 1024)
_PDF_FILE_CAP_BYTES = env_int(
    "SHERPA_PDF_FILE_CAP_BYTES", 100 * 1024 * 1024, 1024 * 1024, 1024 * 1024 * 1024)


def _office_size_exceeded(rp: Path, ext: str) -> bool:
    """変換前の入口サイズ超過判定（Office 系・PDF は別 cap）。stat 失敗はサイズ超過として扱わない（実読込の失敗は別経路が拾う）。"""
    cap = _PDF_FILE_CAP_BYTES if ext == ".pdf" else _OFFICE_FILE_CAP_BYTES
    try:
        return rp.stat().st_size > cap
    except OSError:
        return False


# xlsx セル数ガード: 圧縮後サイズ（`SHERPA_OFFICE_FILE_CAP_BYTES`）は圧縮率の高い xlsx を素通りさせるため、
# openpyxl を開く前にセル数の上限で止める。
_XLSX_CELL_CAP = env_int("SHERPA_XLSX_CELL_CAP", 2_000_000, 1_000, 100_000_000)

# Office 非圧縮サイズガード: zip の非圧縮サイズ合計（セントラルディレクトリの `ZipInfo.file_size` の和・本体は読まない）の上限。
# 素の docx/xlsx/pptx と、旧形式を OOXML へ前段変換した後のファイルの両方に適用する。
_OFFICE_UNCOMPRESSED_CAP_BYTES = env_int(
    "SHERPA_OFFICE_UNCOMPRESSED_CAP_BYTES", 500 * 1024 * 1024, 1024 * 1024, 4 * 1024 * 1024 * 1024)

_XLSX_DIMENSION_RE = re.compile(rb'<dimension\s+ref="([^"]*)"')
_XLSX_DIMENSION_SCAN_BYTES = 8192   # dimension はシート XML 先頭付近（sheetData 前）にある通例
_XLSX_CELL_REF_RE = re.compile(r'^([A-Za-z]+)(\d+)$')


def _xlsx_col_to_num(col: str) -> int:
    n = 0
    for c in col.upper():
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n


def _xlsx_dimension_area(ref: str) -> int | None:
    """`<dimension ref="A1:XX9999"/>` の座標範囲 → セル数（行×列）。パース不能は None。"""
    ref = ref.strip()
    if not ref:
        return None
    parts = ref.split(":")
    if len(parts) not in (1, 2):
        return None
    cells = []
    for part in parts:
        m = _XLSX_CELL_REF_RE.match(part)
        if not m:
            return None
        cells.append((_xlsx_col_to_num(m.group(1)), int(m.group(2))))
    if len(cells) == 1:
        return 1
    (c1, r1), (c2, r2) = cells
    return (abs(c2 - c1) + 1) * (abs(r2 - r1) + 1)


def _xlsx_estimated_cell_count(rp: Path) -> int | None:
    """openpyxl でロードする前に、`xl/worksheets/sheetN.xml` の `<dimension ref="..."/>` だけを読んで全シートのセル数（面積）の和を見積もる。

    先頭 `_XLSX_DIMENSION_SCAN_BYTES` バイトだけ読む。`<dimension>` は自己申告のため、欠落/不正で None を返したら
    呼び出し側が `_xlsx_actual_cell_count`（実数カウント）へフォールバックする。壊れた zip/シート皆無は None。
    """
    try:
        with zipfile.ZipFile(rp) as zf:
            sheet_names = [n for n in zf.namelist()
                           if n.startswith("xl/worksheets/") and n.lower().endswith(".xml")]
            if not sheet_names:
                return None
            total = 0
            for name in sheet_names:
                with zf.open(name) as fh:
                    head = fh.read(_XLSX_DIMENSION_SCAN_BYTES)
                m = _XLSX_DIMENSION_RE.search(head)
                if not m:
                    return None
                area = _xlsx_dimension_area(m.group(1).decode("ascii", "ignore"))
                if area is None:
                    return None
                total += area
            return total
    except (OSError, zipfile.BadZipFile):
        return None


_XLSX_CELL_TAG_NEEDLE = b"<c "         # OOXML のセル要素は必ず `r="..."` 属性を伴うため空白まで含める
_XLSX_CELL_COUNT_CHUNK_BYTES = 1 << 20  # 1MiB ずつ（シート全体を一度にメモリへ載せない）


def _xlsx_actual_cell_count(rp: Path, cap: int) -> int | None:
    """`<dimension>` が欠落/不正な xlsx 向けに、シート XML の `<c ` 出現数を実際にストリーミングでカウントする。

    1MiB チャンクで読み、チャンク境界をまたぐ `<c ` は直前チャンク末尾（needle 長-1 バイト）を持ち越して拾う。
    `cap` を超えた時点で打ち切る。壊れた zip/シート皆無は None。
    """
    try:
        with zipfile.ZipFile(rp) as zf:
            sheet_names = [n for n in zf.namelist()
                           if n.startswith("xl/worksheets/") and n.lower().endswith(".xml")]
            if not sheet_names:
                return None
            total = 0
            carry_len = len(_XLSX_CELL_TAG_NEEDLE) - 1
            for name in sheet_names:
                with zf.open(name) as fh:
                    carry = b""
                    while True:
                        chunk = fh.read(_XLSX_CELL_COUNT_CHUNK_BYTES)
                        if not chunk:
                            break
                        buf = carry + chunk
                        total += buf.count(_XLSX_CELL_TAG_NEEDLE)
                        if total > cap:
                            return total
                        carry = buf[-carry_len:] if carry_len else b""
            return total
    except (OSError, zipfile.BadZipFile):
        return None


def _office_uncompressed_total_bytes(rp: Path) -> int | None:
    """zip セントラルディレクトリだけを読み、全エントリの非圧縮サイズ（`ZipInfo.file_size`）の和を返す。壊れた zip・stat 不能は None。"""
    try:
        with zipfile.ZipFile(rp) as zf:
            return sum(info.file_size for info in zf.infolist())
    except (OSError, zipfile.BadZipFile):
        return None


# 抽出不完全の疑い（静かな部分抽出の検知）: 原本サイズに対し生成 MD が極端に小さければ疑いに計上する。
# 1MiB 超という下限で、小さい原本を除く。
_PARTIAL_SIZE_MIN_SOURCE_BYTES = 1024 * 1024
_PARTIAL_SIZE_MAX_MD_BYTES = 512


def _pdf_backend() -> str | None:
    """利用可能な PDF テキスト抽出バックエンド名（優先順 pypdf > pdfminer.six）。無ければ None。

    pypdf は requirements.txt に同梱既定。いずれも到達不可なら PDF は「未対応」と表示する。
    テキスト層のみ（スキャン画像の視覚読み取りは vision）。
    """
    for name, mod in (("pypdf", "pypdf"), ("pdfminer", "pdfminer.high_level")):
        try:
            __import__(mod)
            return name
        except Exception:
            continue
    return None


def pdf_available() -> bool:
    return _pdf_backend() is not None


def convertible_exts() -> set:
    """今 MD 化できる拡張子。有効アーム（管理画面の取り込み設定）が担当し、かつ現時点で変換可能な拡張子の和集合。

    - OOXML: ooxml アームが有効なとき。
    - 旧形式（.doc/.xls/.ppt）: ooxml 有効かつ変換バックエンド到達可（`legacy_convert.legacy_exts()` が非空）のとき。
    - PDF: `pdf_text` または `vision` のいずれかが到達可なら変換候補（担当は `pdf_escalation_target` が決める）。
    - ラスタ画像: vision（VLM 実効可）のときのみ（`_image_convertible` と一致）。
    """
    from . import arms as _arms
    from .arms import legacy_convert
    names = set(_arms.enabled_arm_names())
    exts: set = set()
    if "ooxml" in names:
        exts |= CONVERTIBLE_EXT                              # OOXML＝常時 MD化できる（外部ライブラリ不要）
        exts |= legacy_convert.legacy_exts()                # 旧形式は①OOXML 経由で MD化＝ooxml 有効時のみ
    if ("pdf_text" in names and pdf_available()) or _pdf_escalation_available(names):
        exts |= PDF_EXT                                      # PDF はテキスト/VLM のいずれか到達可なら
    exts |= RASTER_EVIDENCE_EXT                              # PNG/JPEGはOCRなしでもEvidence/RAG化できる
    if _image_convertible(names):
        exts |= IMAGE_EXT                                    # その他ラスタ画像は vision（VLM）が到達可なら
    return exts


def _vision_pdf_ready(names) -> bool:
    """vision（VLM 視覚読み取り・⑤）が有効 かつ PDFium で PDF をラスタライズできるか。"""
    if "vision" not in names:
        return False
    from .arms import vision_arm
    return vision_arm.pdf_rasterize_available() and vision_arm.vlm_usable()


def _vision_image_ready(names) -> bool:
    """vision（VLM 視覚読み取り・⑤）が有効 かつ VLM が実効可か（画像はラスタ化不要）。"""
    if "vision" not in names:
        return False
    from .arms import vision_arm
    return vision_arm.vlm_usable()


def _image_convertible(names) -> bool:
    """ラスタ画像を今 MD化できるか（vision＝VLM 実効可のときのみ）。"""
    return _vision_image_ready(names)


def _pdf_escalation_available(names) -> bool:
    """vision がPDFの視覚読み取りに到達可能か（PDFを変換候補にできるか）。"""
    return _vision_pdf_ready(names)


# ---- アーム構成 drift（`.arms_sig` マーカー）----
# 派生 MD を作った時の有効アーム構成の署名を派生 dir に残し、構成が変わったら署名同一でも作り直す。
_ARMS_SIG_MARKER = ".arms_sig"                               # 派生MD を作った時の有効アーム構成を記録


def _arms_sig(arm_names, backend: str | None, legacy: str | None = None,
              vlm: str | None = None) -> str:
    """有効アーム構成の決定的署名（順序安定）: 有効アーム名（ソート）＋ PDF バックエンド＋ legacy 変換バックエンド＋ VLM の実効可用性。

    `vlm` はアーム有効かつ実際に使えるときだけ非 "none"（provider/model/クラウド許可の切替でも変わる）。
    エンジンのバージョンは含めない。
    document-ir のスキーマ/抽出器版はここに含めない: `es_index._arms_config_sig()` がこの署名を ES の `needs_reindex` 判定に使うため、
    IR 版を混ぜると全資料フォルダの ES 再索引を誘発する。IR 版の drift は別マーカー（`_DOCUMENT_IR_SIG_MARKER`／`document_ir_sig_drift`）で判定する。
    `;md=`/`;ocr=` 成分は含めない。
    """
    return ("arms=" + ",".join(sorted(arm_names))
            + ";pdf=" + (backend or "none")
            + ";legacy=" + (legacy or "none")
            + ";vlm=" + (vlm or "none"))


def _current_arms_sig() -> str:
    """今の有効アーム構成＋各アームの実効可用性（PDF バックエンド／legacy／VLM）の署名。document-ir 版は含まない。"""
    from . import arms as _arms
    from .arms import legacy_convert, vision_arm
    names = set(_arms.enabled_arm_names())
    vlm = vision_arm.sig_value(names)                # ⑤ VLM: provider/model/クラウド許可の変化で drift
    return _arms_sig(_arms.enabled_arm_names(), _pdf_backend(),
                     legacy_convert.legacy_sig_value(), vlm)


def _write_arms_sig_marker(dr: Path):
    """派生MD ビルド時の有効アーム構成署名を派生 dir に残す（後の drift 判定用・best-effort）。"""
    try:
        (dr / _ARMS_SIG_MARKER).write_text(_current_arms_sig(), encoding="utf-8")
    except OSError:
        pass


def arms_sig_drift(derived_md_dir) -> bool:
    """派生 MD を作った時と今でアーム構成（有効アーム＋PDF バックエンド）が変わったか。

    True なら sync が署名同一でも派生を作り直すべき。マーカー皆無は既定アーム＋バックエンド無しを基準に判定する。
    marker が在って不一致でも、dir が書けない場合は書込不能 probe（`_dir_writable`）で据え置く
    （再ビルドで marker を更新できず毎 sync フルリビルドになるのを避ける）。
    """
    d = Path(derived_md_dir)
    cur = _current_arms_sig()
    sig_path = d / _ARMS_SIG_MARKER
    if sig_path.is_file():
        try:
            mismatched = sig_path.read_text(encoding="utf-8").strip() != cur
        except OSError:
            return True
        if mismatched and not _dir_writable(d):
            _log.warning(
                "派生 dir に arms_sig marker を書けないため drift 再ビルドを見送ります: %s", d)
            return False
        return mismatched
    from . import arms as _arms
    # マーカー皆無＝既定構成・バックエンド無しを基準に判定。marker が書けない dir は警告して据え置く（dir を直せば次の sync で再ビルドが走る）
    drift = _arms_sig(_arms.DEFAULT_ARMS, "none", "none") != cur
    if drift and not _dir_writable(d):
        _log.warning(
            "派生 dir に arms_sig marker を書けないため drift 再ビルドを見送ります: %s", d)
        return False
    return drift


def _dir_writable(d: Path) -> bool:
    """`d` に小ファイルを作成→削除できるか（arms_sig marker を永続できる dir かの probe）。"""
    probe = d / (_ARMS_SIG_MARKER + ".probe")
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# ---- document-ir 版 drift ----
# `.arms_sig` とは別マーカーにする（IR 版の更新で ES 再索引を誘発しないため）。IR 版の更新は
# `document_ir_sig_drift`→`refresh_document_ir` の軽量経路で解決し、`worker.sync` がこれに続けて
# evidence/rag（RAG_ES 有効時は ES 索引）まで連鎖再生成する。
_DOCUMENT_IR_SIG_MARKER = ".document_ir_sig"


def _current_document_ir_sig() -> str:
    """今の document-ir 版（JSON 形式のスキーマ版＋DOCX/PPTX/XLSX 各抽出処理の版）の署名。

    drift 判定（`document_ir_sig_drift`）はこの署名全体を 1 つのマーカーとして比較する。
    どれか 1 つの抽出器だけの更新でも、`refresh_document_ir` は資料フォルダ内の全 OOXML 文書を再生成する。
    """
    from . import document_ir
    from .arms import ooxml_arm
    return (f"schema={document_ir.DOCUMENT_IR_SCHEMA_VERSION};"
            f"docx={ooxml_arm.DOCX_EXTRACTOR_VERSION};pptx={ooxml_arm.PPTX_EXTRACTOR_VERSION};"
            f"xlsx={ooxml_arm.XLSX_EXTRACTOR_VERSION}")


def _write_document_ir_sig_marker(dr: Path):
    """IR 生成/再生成時の document-ir 版を派生 dir に残す（`_write_arms_sig_marker` と同型・best-effort）。"""
    try:
        (dr / _DOCUMENT_IR_SIG_MARKER).write_text(_current_document_ir_sig(), encoding="utf-8")
    except OSError:
        pass


def write_document_ir_sig_marker(derived_md_dir) -> None:
    """`.document_ir_sig` を現行値で確定する公開ヘルパ。

    `refresh_document_ir` を `write_document_ir_sig_marker=False` で呼んだ場合、確定は連鎖した evidence/rag
    （RAG_ES 有効時は ES 反映）の成否を確認できる呼び出し元（`worker`）が行う。
    """
    _write_document_ir_sig_marker(Path(derived_md_dir))


def document_ir_sig_drift(derived_md_dir) -> bool:
    """派生を作った時と今で document-ir 版が変わったか（マーカー無し/読めない/不一致で True）。

    `arms_sig_drift` と違い、書込不能 dir の据え置き probe は行わない。
    """
    sig_path = Path(derived_md_dir) / _DOCUMENT_IR_SIG_MARKER
    if not sig_path.is_file():
        return True
    try:
        return sig_path.read_text(encoding="utf-8").strip() != _current_document_ir_sig()
    except OSError:
        return True


# ---- 人間向け MD（human_md）版 drift ----
# `{rel}.md` の版は rel ごとに `{rel}.derived.json` の `asset_versions.human_md` へ書く
# （1 文書だけの選択的再生成のため。資料フォルダ単位のマーカーにはしない）。


def _current_human_md_sig() -> str:
    """今の人間向け MD（`human_md`）の版。レンダラの版に加え、レンダラが消費する docx/xlsx 抽出器の版も含める。"""
    from . import human_md
    from .arms import ooxml_arm
    return (f"renderer={human_md.HUMAN_MD_RENDERER_VERSION};"
            f"docx={ooxml_arm.DOCX_EXTRACTOR_VERSION};xlsx={ooxml_arm.XLSX_EXTRACTOR_VERSION}")


_LEGACY_HUMAN_MD_EXT = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}


def _md_is_from_ooxml_arm(dr: Path, rel: str) -> bool:
    """既存の `{rel}.md` が ooxml アーム由来か（失敗の注記などを作り直しで上書きしない）。"""
    try:
        meta = json.loads((dr / (rel + ".md.meta.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(meta, dict) and meta.get("arm") == "ooxml"


def _legacy_human_md_source(rp: Path, rel: str, dr: Path) -> Path | None:
    """旧形式 `.doc`/`.xls` の人間向け MD を作り直すための、キャッシュ済みの変換後 OOXML を返す。

    変換は再実行しない。変換キャッシュ（`legacy_convert.ensure_ooxml` の `{rel}{ext}` と `.key`）が無ければ None。
    既存の `{rel}.md` が ooxml アーム由来でない場合も None。
    """
    from .arms import legacy_convert

    target_ext = _LEGACY_HUMAN_MD_EXT.get(rp.suffix.lower())
    if target_ext is None:
        return None
    cache_path = legacy_convert.cache_root_for(dr) / (rel + target_ext)
    key_path = Path(str(cache_path) + ".key")
    try:
        if (not cache_path.is_file() or key_path.read_text(encoding="utf-8").strip()
                != legacy_convert._source_key(rp)):
            return None
        meta = json.loads((dr / (rel + ".md.meta.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cache_path if isinstance(meta, dict) and meta.get("arm") == "ooxml" else None


def human_md_sig_drift(wd, derived, *, world: str | None = None) -> bool:
    """素の docx/xlsx のうち、`{rel}.md` の版が現在の `_current_human_md_sig()` と食い違う rel が 1 件でもあれば True。

    - `world`（アーカイブ取り込み）を渡すと zip/tar 展開先（`_archive_also_root`）も評価する。
    - `.md` sidecar の有無では絞り込まない（空の xlsx 等は `.md` を持たないのが正当だが、`asset_versions.human_md` は評価する）。
      マニフェストが読めない rel は drift あり。legacy `.doc`/`.xls` は変換キャッシュが残っているものだけが対象。
    - 有効アームに従う: `ooxml` アームが無効の間は対象から外す。
    """
    from . import arms as _arms
    from .. import scope_infer as si
    if "ooxml" not in _arms.enabled_arm_names():
        return False
    wd = Path(wd).resolve()
    dr = Path(derived)
    dr_ir = _sibling_layer_dir(dr, "ir")          # `.derived.json` マニフェストは ir 層
    current = _current_human_md_sig()
    for rp, rel in si.safe_files(wd, also=_archive_also_root(world)):
        if rp.suffix.lower() not in (".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"):
            continue
        if rp.suffix.lower() in _LEGACY_HUMAN_MD_EXT and _legacy_human_md_source(rp, rel, dr) is None:
            continue
        if rp.suffix.lower() == ".pptx" and not _md_is_from_ooxml_arm(dr, rel):
            continue
        if _is_sensitive_original(rp, rp.suffix.lower()):
            # 秘匿名は `{rel}.derived.json` マニフェストを持たない（`_is_sensitive_original` 参照）。除外しないと drift が恒常 True になる
            continue
        manifest = json_io.read_json(dr_ir / (rel + _DERIVED_MANIFEST_SUFFIX), default=None)
        versions = manifest.get("asset_versions") if isinstance(manifest, dict) else None
        recorded = versions.get("human_md") if isinstance(versions, dict) else None
        if recorded != current:
            return True
    return False


def _is_sensitive_original(rp: Path, ext: str) -> bool:
    """秘匿名（`text_kind.is_sensitive`）の原本かどうかを判定する唯一の入口。

    変換ループ・欠落検知（`rag_sidecars_missing`）・軽量再生成（`refresh_human_md`/`refresh_document_ir`/`refresh_evidence_ir`）は
    全てこの関数経由で判定する（判定が分散すると秘匿本文が派生ツリーへ平文で書き出される）。
    秘匿名の原本は派生 MD/IR/Evidence/RAG もマニフェストも持たない。
    """
    return text_kind.is_sensitive(rp.name, ext)


def _render_human_md(ir, path: Path, ext: str) -> str | None:
    """docx/xlsx の document-ir から人間向け MD を作る（WMF/EMF 図の文字は原本パッケージから拾って添える）。"""
    from . import human_md, metafile_text

    if ext == ".docx":
        return human_md.render_docx(ir, figure_texts=metafile_text.docx_figure_texts(path))
    return human_md.render_xlsx(ir, figure_texts=metafile_text.xlsx_figure_texts(path))


def refresh_human_md(wd, derived, *, world: str | None = None) -> dict:
    """人間向け `{rel}.md` だけの軽量再生成。

    `human_md_sig_drift` が対象とする rel だけを選び、document-ir を作り直して `human_md.render_docx`/`render_xlsx` へ渡し、`{rel}.md` だけを書き換える
    （`.document.json`/`.evidence.json`/`.rag.md`/`.rag_chunks.jsonl`・ES 索引・各 sig マーカーには触れない）。
    - legacy `.doc`/`.xls`: 変換キャッシュが残っているものだけ、そのキャッシュから作り直す（無ければ据え置き）。
    - IR が構築でき本文が無いと確認できた rel は `asset_versions.human_md` を現行版で確定し、`.md` は書かない。
    - 失敗した rel は `human_md_failed`/`human_md_failures` へ計上し、`asset_versions.human_md` を更新しない（次回 sync が再試行する）。
    - `world`・有効アームの扱いは `human_md_sig_drift` と同じ（`ooxml` アームが無効の間は何もしない）。
    """
    from . import arms as _arms
    from .. import scope_infer as si
    from .arms import ooxml_arm

    if "ooxml" not in _arms.enabled_arm_names():
        return {"human_md_generated": 0, "human_md_failed": 0, "human_md_failures": []}
    wd = Path(wd).resolve()
    dr = Path(derived)
    dr_rag = _sibling_layer_dir(dr, "rag")
    dr_ir = _sibling_layer_dir(dr, "ir")          # `.derived.json` マニフェストは ir 層
    current = _current_human_md_sig()
    generated = failed = 0
    failures: list[dict] = []
    for rp, rel in si.safe_files(wd, also=_archive_also_root(world)):
        ext = rp.suffix.lower()
        if ext not in (".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"):
            continue
        source = rp
        if ext in _LEGACY_HUMAN_MD_EXT:
            # 旧形式はキャッシュ済みの変換後 OOXML から作り直す（無ければ据え置き・変換は再実行しない）。
            source = _legacy_human_md_source(rp, rel, dr)
            if source is None:
                continue
            ext = _LEGACY_HUMAN_MD_EXT[ext]
        elif ext == ".pptx" and not _md_is_from_ooxml_arm(dr, rel):
            continue
        if _is_sensitive_original(rp, rp.suffix.lower()):
            # 秘匿名は human_md を持たない（`_is_sensitive_original` 参照）。除外しないと秘匿本文が `{rel}.md` へ平文で書き出される
            continue
        manifest = json_io.read_json(dr_ir / (rel + _DERIVED_MANIFEST_SUFFIX), default=None)
        versions = manifest.get("asset_versions") if isinstance(manifest, dict) else None
        recorded = versions.get("human_md") if isinstance(versions, dict) else None
        if recorded == current:
            continue
        ir = None
        if ext != ".pptx":                            # pptx の MD は document-ir を経由しない
            try:
                ir = ooxml_arm._build_docx_ir(source) if ext == ".docx" else ooxml_arm._build_xlsx_ir(source)
            except Exception as e:
                failed += 1
                failures.append({"doc": rel, "reason": f"ir_build_failed:{e.__class__.__name__}"})
                continue
            if ir is None:
                failed += 1
                failures.append({"doc": rel, "reason": "ir_build_failed"})
                continue
        try:
            md = _pptx_md(source) if ext == ".pptx" else _render_human_md(ir, source, ext)
            if md is not None:
                json_io.write_text_atomic(dr / (rel + ".md"), md)
            # md is None（docx のみ）は失敗ではなく正当な空＝.md は書かず manifest だけ確定させる
        except OSError as e:
            failed += 1
            failures.append({"doc": rel, "reason": f"write_failed:{e.__class__.__name__}"})
            continue
        except Exception as e:
            failed += 1
            failures.append({"doc": rel, "reason": f"unexpected:{e.__class__.__name__}"})
            _log.warning(
                "human_md の軽量再生成中に想定外の例外が発生しました: %s", rel, exc_info=True)
            continue
        if not _write_derived_sidecar_manifest(dr, dr_rag, dr_ir, rel, human_md_sig=current):
            failed += 1
            failures.append({"doc": rel, "reason": "manifest_write_failed"})
            continue
        generated += 1
    return {"human_md_generated": generated, "human_md_failed": failed, "human_md_failures": failures}


# ---- human_md の ES 反映 drift（資料フォルダ単位・ホールドバック方式・`.rag_sig` と同型）----
# `asset_versions.human_md`（render 済みか）とは別物で、ES がこの版まで索引反映できたかを表す。
# bulk の成否は `worker` しか確認できないため、マーカーの確定は `worker` が行う。
_HUMAN_MD_ES_SIG_MARKER = ".human_md_es_sig"


def _write_human_md_es_sig_marker(dr: Path) -> bool:
    try:
        (dr / _HUMAN_MD_ES_SIG_MARKER).write_text(_current_human_md_sig(), encoding="utf-8")
        return True
    except OSError:
        _log.warning(
            "`.human_md_es_sig` マーカーの書込に失敗しました（次回 sync も pending のまま再試行）: %s", dr)
        return False


def confirm_human_md_es_sig(wd, derived, *, world: str | None = None) -> bool:
    """`.human_md_es_sig` を現行値で確定する（ES が bulk 成功でこの版まで追随できたと記録する）。

    `worker` が `es_index.index_world()` の戻り値に失敗（`error` キー）が無いときだけ呼ぶ。
    render 側（`human_md_sig_drift`）に未追随の rel が残る間は確定しない。
    確定できたら True、見送り・書込失敗は False（呼び出し元は失敗として扱う）。`world` は `human_md_sig_drift` へ転送する。
    """
    wd = Path(wd).resolve()
    dr = Path(derived)
    if human_md_sig_drift(wd, dr, world=world):
        return False
    return _write_human_md_es_sig_marker(dr)


def drop_human_md_es_sig_marker(derived_md_dir) -> bool:
    """`.human_md_es_sig` を明示的に未確定へ戻す（`drop_rag_sig_marker` と同型）。

    再索引を始める前に必ず呼ぶ（呼ばないと、確定済みマーカーが残ったまま bulk が部分失敗しても meta に確定値が書かれる）。
    成功時 True、削除失敗（`OSError`）時は False（呼び出し元はログに残して再索引を続行してよい）。
    """
    try:
        (Path(derived_md_dir) / _HUMAN_MD_ES_SIG_MARKER).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def human_md_es_sig_drift(derived) -> bool:
    """ES がまだ現行の human_md 版まで追随できていないか（マーカー欠落/不一致で True）。

    `es_index._human_md_config_sig()` が pending 判定に使う（世界単位・`rag_sig_drift` と同型）。
    """
    sig_path = Path(derived) / _HUMAN_MD_ES_SIG_MARKER
    if not sig_path.is_file():
        return True
    try:
        return sig_path.read_text(encoding="utf-8").strip() != _current_human_md_sig()
    except OSError:
        return True


# ---- Canonical Evidence IR 版 drift ----
# Document IR は置換せず並行生成する。Evidence IR の schema/parser 変更だけで MD や既存 IR を作り直さないよう、
# 独立マーカーと軽量 refresh を持つ。
_EVIDENCE_IR_SIG_MARKER = ".evidence_ir_sig"


def _current_evidence_ir_sig() -> str:
    """現在のEvidence IR契約、parser profile、通常生成対象の抽出器版の署名。"""
    from . import (
        evidence_ir, evidence_spike, excel_display, legacy_provenance, office_native_display, raster_evidence,
    )
    from .arms import ooxml_arm

    return (f"schema={evidence_ir.EVIDENCE_IR_SCHEMA_VERSION};"
            f"parser={evidence_ir.EVIDENCE_PARSER_PROFILE};"
            f"xlsx={ooxml_arm.XLSX_EXTRACTOR_VERSION};docx={ooxml_arm.DOCX_EXTRACTOR_VERSION};"
            f"pptx={ooxml_arm.PPTX_EXTRACTOR_VERSION};"
            f"xlsx_adapter={evidence_spike.XLSX_ADAPTER_VERSION};"
            f"docx_adapter={evidence_spike.DOCX_ADAPTER_VERSION};"
            f"pptx_adapter={evidence_spike.PPTX_ADAPTER_VERSION};"
            f"pdf_adapter={evidence_spike.PDF_ADAPTER_VERSION};"
            f"xlsx_display={excel_display.EXCEL_DISPLAY_PROFILE};"
            f"office_native={office_native_display.config_signature()};"
            f"raster_adapter={raster_evidence.RASTER_ADAPTER_VERSION};"
            f"legacy_adapter={legacy_provenance.LEGACY_PROVENANCE_ADAPTER_VERSION}")


def _write_evidence_ir_sig_marker(dr: Path):
    """Evidence IR生成時の版を派生directoryへ残す（best-effort）。"""
    try:
        (dr / _EVIDENCE_IR_SIG_MARKER).write_text(_current_evidence_ir_sig(), encoding="utf-8")
    except OSError:
        pass


def evidence_ir_sig_drift(derived_md_dir) -> bool:
    """派生を作った時と現在でEvidence IR版が変わったか。"""
    sig_path = Path(derived_md_dir) / _EVIDENCE_IR_SIG_MARKER
    if not sig_path.is_file():
        return True
    try:
        return sig_path.read_text(encoding="utf-8").strip() != _current_evidence_ir_sig()
    except OSError:
        return True


# ---- Evidence IR 由来の pipe-free RAG 表現版 drift ----
_RAG_SIG_MARKER = ".rag_sig"


def _archive_also_root(world: str | None) -> Path | None:
    """アーカイブ取り込み（zip/tar(.gz)/tgz）: `world` が分かれば、その展開先（`worlds.archives_dir`）を返す。

    `scope_infer.safe_files(wd, also=...)` へ渡すための唯一の入口で、原本ツリー走査（変換・drift 判定・軽量再生成）の全箇所が共有する。
    展開先は doc_id と同じ相対パス構造を持つため、`wd` と合わせて歩いた `rel` は派生物の置き場キーにそのまま使える
    （原本ツリーへは書かない）。`world` 不明なら None（合流なし）。
    """
    if not world:
        return None
    from .. import worlds
    return worlds.archives_dir(world)


def _resolve_ocr_observation_dir(world: str | None) -> Path | None:
    """`world` の公開中 OCR 観測ディレクトリ（無効/未指定/未公開は None）。

    `worlds.observation_dir` は `derived_dir` と物理 root を分ける（隔離 OCR worker への read-only 境界）ため、
    `world` 文字列でしか辿れない。循環を避けて `worlds` を遅延 import する。
    """
    if not world or not ocr_enabled():
        return None
    from .. import worlds
    return worlds.observation_current_dir(world)


def _ocr_observation_marker_for(obs_dir: Path | None) -> str | None:
    """`obs_dir` を `.rag_sig` 用の不透明な印にする。観測世代が公開されるたび値が変わる。観測なしは None（`_current_rag_sig` は `"none"` として扱う）。"""
    return f"{obs_dir.parent.name}/{obs_dir.name}" if obs_dir is not None else None


def current_ocr_observation_marker(world: str | None) -> str | None:
    """`world` の OCR 観測の現在状態を表す印（呼び出し元は世界ごとに1回だけ解決して使い回す）。"""
    return _ocr_observation_marker_for(_resolve_ocr_observation_dir(world))


def _current_rag_sig(*, ocr_observation_marker: str | None = None) -> str:
    """Evidence 版を包含する RAG renderer/chunker の署名。

    `ocr_observation_marker`: 公開中 OCR 観測世代の印。rag.md の内容は OCR 観測が公開されると変わりうるため、
    この次元を含めて OCR 完了後の次回 sync に `refresh_rag` を誘発させる。
    """
    from . import ai_observation, context_ir, evidence_render, metafile_text

    return (f"renderer={evidence_render.RAG_RENDERER_VERSION};"
            f"chunker={evidence_render.RAG_CHUNKER_VERSION};"
            f"observation={ai_observation.AI_OBSERVATION_SCHEMA_VERSION}/"
            f"{ai_observation.AI_OBSERVATION_RESOLVER_VERSION}/"
            f"{ai_observation.AI_OBSERVATION_MERGE_VERSION};"
            f"context={context_ir.CONTEXT_IR_SCHEMA_VERSION}/{context_ir.CONTEXT_ANALYZER_VERSION};"
            f"docx_context={context_ir.DOCX_CONTEXT_ANALYZER_VERSION};"
            f"pptx_context={context_ir.PPTX_CONTEXT_ANALYZER_VERSION};"
            f"pdf_context={context_ir.PDF_CONTEXT_ANALYZER_VERSION};"
            f"identifier_roles={context_ir.IDENTIFIER_ROLE_ANALYZER_VERSION};"
            f"identifier_metadata={context_ir.IDENTIFIER_METADATA_SCHEMA_VERSION}/"
            f"{context_ir.IDENTIFIER_MAX_MENTIONS_PER_CHUNK};"
            f"metafile_text={metafile_text.METAFILE_EXTRACT_VERSION};"
            f"evidence={_current_evidence_ir_sig()};"
            f"ocr_observation={ocr_observation_marker or 'none'}")


def _write_rag_sig_marker(dr: Path, *, ocr_observation_marker: str | None = None):
    try:
        (dr / _RAG_SIG_MARKER).write_text(
            _current_rag_sig(ocr_observation_marker=ocr_observation_marker), encoding="utf-8")
    except OSError:
        pass


def write_rag_sig_marker(derived_md_dir, *, world: str | None = None) -> None:
    """`.rag_sig` を現行値で確定する公開ヘルパ。

    `refresh_evidence_ir`/`refresh_rag` を `write_rag_sig_marker=False` で呼んだ場合、確定は ES 反映の成否を確認できる
    呼び出し元（`worker`）が、成功時だけ行う。`world` を渡すと OCR 観測次元も現行値で確定する（渡さなければ「観測なし」）。
    """
    _write_rag_sig_marker(Path(derived_md_dir), ocr_observation_marker=current_ocr_observation_marker(world))


def drop_rag_sig_marker(derived_md_dir) -> bool:
    """`.rag_sig` を明示的に未確定へ戻す。削除失敗を検知できる（`_remove_marker` は `OSError` を握り潰す）。成功時 True、削除失敗時は False。"""
    try:
        (Path(derived_md_dir) / _RAG_SIG_MARKER).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def rag_sig_drift(derived_md_dir, *, world: str | None = None) -> bool:
    sig_path = Path(derived_md_dir) / _RAG_SIG_MARKER
    if not sig_path.is_file():
        return True
    try:
        current = _current_rag_sig(ocr_observation_marker=current_ocr_observation_marker(world))
        return sig_path.read_text(encoding="utf-8").strip() != current
    except OSError:
        return True


def _remove_marker(dr: Path, name: str):
    """失敗時に stale な版マーカーを消す（現行値のマーカーが残ると drift=False になり修復フックが走らなくなる）。best-effort。"""
    try:
        (dr / name).unlink(missing_ok=True)
    except OSError:
        pass


def _within(p: Path, parent: Path) -> bool:
    return p == parent or parent in p.parents


def _evidence_arm_selected(ext: str, arm_name: str | None) -> bool:
    """原本hashを保ったCanonical Evidenceを生成できる担当armか。"""
    if ext in CONVERTIBLE_EXT:
        return arm_name == "ooxml"
    if ext == ".pdf":
        return arm_name in {"pdf_text", "vision"}
    return False


def _extract_canonical_evidence(
    source_path: Path,
    *,
    extraction_path: Path | None = None,
    legacy_ir=None,
    consume_legacy: bool = False,
    legacy_conversion: dict | None = None,
    office_display_report: dict | None = None,
):
    """原本identityと実抽出artifactを分離してCanonical Evidenceを構築する。"""
    from . import excel_display, evidence_spike, legacy_provenance, office_native_display, raster_evidence

    source_path = Path(source_path)
    actual = Path(extraction_path) if extraction_path is not None else source_path
    if source_path.suffix.lower() in RASTER_EVIDENCE_EXT:
        extracted = raster_evidence.extract(source_path)
    else:
        extracted = evidence_spike.extract(
            actual, legacy_ir=legacy_ir, consume_legacy=consume_legacy)
        if actual.suffix.lower() == ".xlsx":
            excel_display.enrich_evidence(extracted, actual)
            native_report = office_native_display.enrich_evidence(extracted, source_path)
            if office_display_report is not None:
                office_display_report.update(native_report.to_dict())
        if legacy_conversion is not None:
            legacy_provenance.apply_to_evidence(extracted, legacy_conversion)
    return extracted


def _extract_evidence_assets(
    source_path: Path,
    extraction_path: Path,
    extracted,
    destination: Path,
) -> list[Path]:
    from . import evidence_spike, metafile_text, raster_evidence

    if Path(source_path).suffix.lower() in RASTER_EVIDENCE_EXT:
        return raster_evidence.extract_assets(source_path, extracted, destination)
    written = evidence_spike.extract_assets(extraction_path, extracted, destination)
    # WMF/EMF の中のビットマップは PNG にして子として並べる（OCR ルートが親の図に結び付けて選ぶ）。
    metafile_text.materialize_children(destination)
    return written


def _build_figure_texts(extracted_evidence, assets_dir: Path) -> dict[str, list[list[str]]]:
    """WMF/EMF の図の描画命令に載っている文字を、図（Evidence 要素）ごとに取り出す。

    OCR の有効/無効や完了に依存せず、抽出済み assets だけから決定的に作る。失敗は「この文書の図の
    文字なし」へ縮退し、rag.md の生成は止めない。
    """
    from . import metafile_text, ocr_router

    try:
        paths: dict[str, str] = {}
        for binding in ocr_router.inventory_assets(assets_dir):
            if binding.parent_sha256 is None and not binding.is_readable_raster():
                paths.setdefault(binding.asset_sha256, binding.relative_path)
        if not paths:
            return {}
        cache: dict[str, list[str]] = {}
        result: dict[str, list[list[str]]] = {}
        for element in extracted_evidence.elements:
            for candidate in ocr_router._raster_candidates(element):
                raw = candidate.get("asset_sha256")
                if not isinstance(raw, str):
                    continue
                digest = raw.strip().lower()
                digest = digest if digest.startswith("sha256:") else "sha256:" + digest
                relative = paths.get(digest)
                if relative is None:
                    continue
                if digest not in cache:
                    content = metafile_text.read_asset_content(assets_dir.joinpath(*relative.split("/")))
                    cache[digest] = content.lines if content is not None else []
                if cache[digest]:
                    result.setdefault(element.element_id, []).append(cache[digest])
        return result
    except Exception:
        _log.warning("図の中の文字の抽出に失敗しました（この文書では出さずに継続）", exc_info=True)
        return {}


def _build_vlm_observation_set(extracted_evidence, rel: str, assets_dir: Path):
    """`vision` が有効かつ VLM 実効可のときだけ、canonical が読めない画像要素を補足観測する。

    既定（`vision` が有効アームに無い、または VLM が実効利用不可）は None＝`evidence_render.render(observation_set=None)` と同じ。
    候補選定は `ocr_router.build_manifest` を呼び直すだけで、`.ocr_route.json` とは独立。
    `asset_root` は直前に抽出済みの `{rel}.assets/`。
    """
    from . import arms as _arms
    from . import ocr_router
    from .arms import vision_arm

    names = set(_arms.enabled_arm_names())
    if "vision" not in names:
        return None
    if vision_arm.resolve_vlm() is None:
        return None
    assets = ocr_router.inventory_assets(assets_dir)
    if not assets:
        return None
    try:
        manifest = ocr_router.build_manifest(extracted_evidence, source_rel_path=rel, assets=assets)
    except ValueError:
        return None
    decisions = [
        item for item in manifest.decisions if item.status == "selected" and item.input_kind == "asset"
    ]
    if not decisions:
        return None
    try:
        return vision_arm.build_asset_observations(
            extracted_evidence, decisions=decisions, asset_root=assets_dir)
    except Exception:
        # 補足観測は任意。VLM/Set 構築の想定外失敗で Canonical の rag.md 生成を巻き添えにしない
        _log.warning(
            "VLM 補足観測の生成に失敗しました（Canonical の生成は継続）: %s", rel, exc_info=True)
        return None


def _load_ocr_observation_sets(extracted_evidence, rel: str, obs_dir: Path | None) -> list:
    """`obs_dir`（公開中 OCR 観測ディレクトリ）から、この `rel` 分の Observation Set 群を読む。

    OCR は隔離 worker が非同期に書く別成果物（`{rel}.ai_observations.jsonl`・1 job＝1 行）。
    各 Set は `ai_observation.from_json_str(..., ir=extracted_evidence)` で `source_content_hash` が今の Evidence と一致することを再検証し、
    古い観測は弾く。読めない/検証失敗は「この文書の OCR 観測なし」へ縮退する。
    """
    if obs_dir is None:
        return []
    from . import ai_observation

    rel_posix = PurePosixPath(rel.replace("\\", "/"))
    base = obs_dir.joinpath(*rel_posix.parts)
    jsonl_path = Path(str(base) + ".ai_observations.jsonl")
    if not jsonl_path.is_file():
        return []
    sets = []
    try:
        with jsonl_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                sets.append(ai_observation.from_json_str(line, ir=extracted_evidence))
    except (OSError, ValueError):
        _log.warning(
            "OCR 観測 Set の読込/検証に失敗しました（VLM のみで rag.md を生成します）: %s",
            rel, exc_info=True)
        return []
    return sets


def _build_observation_set(extracted_evidence, rel: str, assets_dir: Path, *, obs_dir: Path | None = None):
    """VLM（同期）と OCR（非同期・公開済み分）の観測 Set を合流し、`evidence_render.render` へ渡す 1 つの Set にする。

    どちらも無ければ None。片方だけなら合成せずそのまま返す。両方あるときだけ `merge_sets` で畳み、合流が失敗したら VLM 単独へ縮退する。
    """
    vlm_set = _build_vlm_observation_set(extracted_evidence, rel, assets_dir)
    ocr_sets = _load_ocr_observation_sets(extracted_evidence, rel, obs_dir)
    candidates = list(ocr_sets)
    if vlm_set is not None:
        candidates.append(vlm_set)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    from . import ai_observation
    try:
        return ai_observation.merge_sets(candidates, ir=extracted_evidence)
    except Exception:
        _log.warning(
            "VLM/OCR 観測 Set の合流に失敗しました（VLM 単独へ縮退します）: %s", rel, exc_info=True)
        return vlm_set


def _is_source_failure_notice(meta: object) -> bool:
    """full generationが公開したsource-level parse failure noticeか。"""
    if not isinstance(meta, dict) or meta.get("arm") != "evidence_notice":
        return False
    notes = meta.get("notes")
    return isinstance(notes, list) and "reason_code=source_parse_failed" in notes


def _source_failure_detail(previous_evidence_path: Path) -> dict:
    """旧Evidenceから抽出失敗の診断値だけを引き継ぐ。

    Evidence schema drift時でもnotice自体を再生成できるよう、型付きIRとしては読まずJSONの
    ``error_class``だけを採用する。原本値や推定内容を持ち込まず、診断値が壊れていれば空へ縮退する。
    """
    payload = json_io.read_json(previous_evidence_path, default=None)
    if not isinstance(payload, dict):
        return {}
    coverage = payload.get("coverage")
    if not isinstance(coverage, list):
        return {}
    for item in coverage:
        if not isinstance(item, dict) or item.get("reason_code") != "source_parse_failed":
            continue
        detail = item.get("detail")
        error_class = detail.get("error_class") if isinstance(detail, dict) else None
        return {"error_class": error_class} if isinstance(error_class, str) and error_class else {}
    return {}


def _build_source_failure_evidence(source_path: Path, *, detail: dict | None = None):
    """壊れた現行Office/PDFを検索可能なsource-level failed coverageへする。"""
    from . import legacy_provenance

    return legacy_provenance.build_unavailable_evidence(
        source_path,
        status="failed",
        reason_code="source_parse_failed",
        detected_kind=f"{source_path.suffix.lower().lstrip('.') or 'unknown'}_source_document",
        object_id="source-parse-failure",
        detail=detail,
    )


# ---- 安全な差し替え（世代公開） ----
# 公開中の派生ディレクトリを先に全消しせず、別ディレクトリへ作り切ってから改名 2 回で差し替える。
# 失敗時はステージングを捨てるだけで公開中の内容は無傷。
_STAGING_SUFFIX = ".staging"
_RETIRED_SUFFIX = ".retired"


# ---- 派生物の 3 層（md／rag／ir）----
# 派生物は md（人間用）／rag（RAG 正本＋証跡）／ir（中間表現）の 3 層に物理分離する。
# 呼び出し元は `derived_md_dir(world)` だけを渡し、rag/ir はその兄弟として導出する。
# `.arms_sig`/`.document_ir_sig`/`.evidence_ir_sig`/`.rag_sig`/`.human_md_es_sig`/`.world_sig` 等の資料フォルダ単位マーカーは md 層に置く。
def _sibling_layer_dir(md_variant: Path, layer: str) -> Path:
    """`md_variant`（公開中／ステージング／退避のいずれかの md 層パス）と同じ変種の `layer`（'rag'|'ir'）兄弟ディレクトリを返す。
    staging/retired サフィックスも一致させる。
    """
    name = md_variant.name
    for suffix in (_STAGING_SUFFIX, _RETIRED_SUFFIX):
        if name.endswith(suffix):
            return md_variant.parent / (layer + suffix)
    return md_variant.parent / layer


def _stamp_rule_only_rag_markdown(markdown: str) -> str:
    """rag.md 書込直前に `生成手段: 規則` を刻む（rag.md は必ずこの申告を持つ）。

    sync 経路は規則版を即時生成するだけで LLM は呼ばない。LLM 成形は取り込み後のバックグラウンド
    （`llm_render.run_world_pass`）が `生成手段: 規則` を目印に拾って行う。
    """
    from . import llm_render
    return llm_render.stamp_rule_only(markdown)


# `.derived.json`（sidecar マニフェスト）が記録する 5 種の sidecar 種別（`_MANIFEST_SIDECAR_SUFFIXES`）がどの層へ置かれるか。
# 中間（ir）: document/evidence/derived/ocr_route の各 json。RAG 正本（rag）: rag.md・rag_chunks.jsonl・assets/。人間用（md）: md・md.meta.json。
_LAYER_FOR_SIDECAR_SUFFIX = {
    ".md": "md",
    ".md.meta.json": "md",
    ".evidence.json": "ir",
    ".document.json": "ir",
    ".derived.json": "ir",
    ".ocr_route.json": "ir",
    ".rag.md": "rag",
    ".rag_chunks.jsonl": "rag",
}
# 公開中の派生物が「どの資料フォルダの内容から作られたか」を刻む印（`derived_generation` 参照）
_WORLD_SIG_MARKER = ".world_sig"
_OCR_ENABLED_ENV = "SHERPA_OCR_ENABLED"


def ocr_enabled() -> bool:
    """OCR 観測が有効か。既定 ON。

    取り込み側で有効なのはルート生成（どのラスタを読むかを決めるだけ）。読み取り本体は隔離ワーカーが行う。
    `SHERPA_OCR_ENABLED=0` で止められる。
    """
    raw = os.environ.get(_OCR_ENABLED_ENV)
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def write_ocr_route_sig_marker(dr) -> None:
    """`.ocr_route_sig` を確定する。OCR refresh の enqueue が成功した後にだけ呼ぶこと（先に確定すると enqueue 失敗時に再試行の入口を失う）。"""
    dr = Path(dr)
    from . import ocr_router
    try:
        (dr / ocr_router.OCR_ROUTE_SIG_MARKER).write_text(ocr_router.ocr_route_sig_value() + "\n", encoding="utf-8")
    except OSError:
        pass


def ocr_route_refresh_needed(derived) -> bool:
    """公開中のルート（`.ocr_route.json`）が現行のルート版で作られていなければ True（OCR 有効時のみ）。"""
    from . import ocr_router
    return ocr_enabled() and ocr_router.ocr_route_sig_drift(derived)


def refresh_ocr_routes(derived, *, world: str, generation_id: str) -> dict:
    """ルート版が古い／欠けている `.ocr_route.json` だけを、公開中の Evidence と assets から作り直す。

    あわせて全ルートについて、「読めない画像形式」として対象外の入力に残っている過去の queued/failed job を cancelled へ終端する。
    Evidence・rag.md・ES には触れない。マーカー（`.ocr_route_sig`）は呼び出し元が全件成功を確認して確定する。
    """
    from . import evidence_ir, metafile_text, ocr_router
    from ..store import ocr_jobs

    dr = Path(derived)
    dr_ir = _sibling_layer_dir(dr, "ir")
    dr_rag = _sibling_layer_dir(dr, "rag")
    rewritten = failed = unavailable = 0
    for evidence_path in sorted(dr_ir.rglob("*.evidence.json")):
        rel = evidence_path.relative_to(dr_ir).as_posix()[: -len(".evidence.json")]
        route_path = dr_ir / f"{rel}.ocr_route.json"
        try:
            raw = route_path.read_text(encoding="utf-8") if route_path.is_file() else None
            if raw is None or json.loads(raw).get("router_profile") != ocr_router.OCR_ROUTER_PROFILE:
                ir = evidence_ir.from_json_str(evidence_path.read_text(encoding="utf-8"))
                metafile_text.materialize_children(dr_rag / f"{rel}.assets")
                assets = ocr_router.inventory_assets(dr_rag / f"{rel}.assets")
                manifest = ocr_router.build_manifest(ir, source_rel_path=rel, assets=assets)
                ocr_router.write_json_atomic(route_path, manifest)
                rewritten += 1
            else:
                manifest = ocr_router.from_json_str(raw)
            unavailable += sum(
                1 for d in manifest.decisions if ocr_router.render_unavailable(d))
            ocr_jobs.cancel_unsupported_routes(
                world, generation_id, rel, ocr_jobs.unsupported_route_ids(manifest))
        except Exception:
            failed += 1
            _log.warning("OCRルートの書き直し/終端に失敗しました（次回 sync で再試行）: %s", rel, exc_info=True)
    return {"ocr_routes_rewritten": rewritten, "ocr_routes_failed": failed,
            "metafile_render_unavailable": unavailable}


def _write_ocr_routes(stage_ir: Path, stage_rag: Path) -> dict:
    """OCR 実行とは独立に、Evidence 内の全ラスタ候補を決定的に分類する。

    「読む/読まない」を決めるだけで、画像の意味は推定せず OCR も走らせない。
    `{rel}.evidence.json` と出力の `{rel}.ocr_route.json` は ir 層ステージング（`stage_ir`）、
    asset inventory は rag 層ステージング（`stage_rag`）から読む。
    """
    from . import evidence_ir, ocr_router

    summary = {"documents": 0, "selected": 0, "excluded": 0, "failed_binding": 0, "metafile_render_unavailable": 0}
    for path in sorted(stage_ir.rglob("*.evidence.json")):
        rel = path.relative_to(stage_ir).as_posix()
        source_rel_path = rel[: -len(".evidence.json")]
        ir = evidence_ir.from_json_str(path.read_text(encoding="utf-8"))
        assets = ocr_router.inventory_assets(stage_rag / f"{source_rel_path}.assets")
        manifest = ocr_router.build_manifest(ir, source_rel_path=source_rel_path, assets=assets)
        ocr_router.write_json_atomic(stage_ir / f"{source_rel_path}.ocr_route.json", manifest)
        summary["documents"] += 1
        for decision in manifest.decisions:
            if decision.status in summary:
                summary[decision.status] += 1
            if ocr_router.render_unavailable(decision):
                summary["metafile_render_unavailable"] += 1     # 「未対応（LibreOffice が入っていません）」の図の数
    return summary


def _recover_interrupted_swap(target: Path) -> None:
    """改名 2 回の間で中断した場合（公開中が無く退避先だけがある）に、退避先を公開中へ戻す。

    - target 有り: 何もしない。
    - target 無し・retired 無し: 初回ビルドのため何もしない。
    - target 無し・retired 有り: retired を target へ rename して復旧する。rename に失敗したら（二重障害）例外を送出する。
      握り潰すと後続の `_publish_staging` が retired（唯一残った旧世代）を削除し、派生物が全消失する。
    """
    retired = target.with_name(target.name + _RETIRED_SUFFIX)
    if not target.exists() and retired.is_dir():
        try:
            retired.rename(target)
        except OSError:
            _log.error(
                "派生物の差し替え中断からの復旧に失敗しました"
                "（retired を保持したまま build を打ち切ります）: %s", target, exc_info=True)
            raise
        _log.warning(
            "派生物の差し替えが中断していたため復旧しました: %s", target)


def _publish_staging(staging: Path, target: Path) -> None:
    """ステージングを公開中へ差し替える（旧公開分は削除）。同一ファイルシステム内の改名のみ。

    後半の改名（staging→target）が失敗したら、retired→target で即時ロールバックしてから例外を再送出する。
    ロールバック自体が失敗した場合は旧内容が retired に残るため、その旨をログに残す（次回 build の `_recover_interrupted_swap` が復旧を試みる）。
    """
    retired = target.with_name(target.name + _RETIRED_SUFFIX)
    shutil.rmtree(retired, ignore_errors=True)
    target_existed = target.exists()
    if target_existed:
        target.rename(retired)          # ここから下の rename までが唯一の窓（ミリ秒）
    try:
        staging.rename(target)
    except OSError:
        if target_existed:
            try:
                retired.rename(target)
            except OSError:
                _log.error(
                    "派生物の差し替えに失敗し、旧公開分の復元にも失敗しました"
                    "（derived root が消失した可能性）: %s", target, exc_info=True)
        raise
    shutil.rmtree(retired, ignore_errors=True)



def _proc_rss_gib() -> float | None:
    """自プロセスの RSS（GiB）。/proc/self/status の VmRSS を読む（Linux 専用・読めなければ None）。重い変換の前後でログへ出す。"""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        pass
    return None

def build_derived(wd, derived, *, progress: Callable[[int, int], None] | None = None,
                  world_sig: str | None = None, world: str | None = None) -> dict:
    """公開中の派生物を壊さずに作り直す薄いラッパ。

    実体は `_build_derived_into_staging`。途中で例外が起きてもステージングを残さず、公開中の内容には触れない。
    `world_sig` を渡すと、公開する派生物にその中身を作った資料フォルダ署名を刻む（`.world_sig`・`derived_generation` 参照）。
    `world` は `_build_derived_into_staging` 参照。
    """
    published = Path(derived)
    published_rag = _sibling_layer_dir(published, "rag")
    published_ir = _sibling_layer_dir(published, "ir")
    staging = published.with_name(published.name + _STAGING_SUFFIX)
    staging_rag = published_rag.with_name(published_rag.name + _STAGING_SUFFIX)
    staging_ir = published_ir.with_name(published_ir.name + _STAGING_SUFFIX)
    all_staging = (staging, staging_rag, staging_ir)
    try:
        rep = _build_derived_into_staging(wd, derived, progress=progress, world=world)
    except BaseException:
        for s in all_staging:
            shutil.rmtree(s, ignore_errors=True)
        raise
    if rep.get("error"):                             # 準備段階で失敗＝ステージングも作られていない
        for s in all_staging:
            shutil.rmtree(s, ignore_errors=True)
        return rep
    # OCR は任意観測。有効時だけ、公開前のステージング上でどのラスタを読むかを決定的に分類する（OCR 自体は隔離 worker の仕事）。
    # 公開と同時にルートも入れ替わるため、Evidence とルートが食い違わない。
    if ocr_enabled():
        try:
            rep["ocr_routes"] = _write_ocr_routes(staging_ir, staging_rag)
        except Exception as e:                       # OCR は任意＝Canonical の公開を巻き添えにしない
            rep["ocr_routes_error"] = f"{e.__class__.__name__}"
            _log.warning(
                "OCRルート生成に失敗しました（Canonicalの公開は継続）", exc_info=True)
    if world_sig:
        # `.world_sig` は現行位置のまま md 層ステージングへ刻む（層に属さない world 単位の状態）。
        try:
            json_io.write_text_atomic(staging / _WORLD_SIG_MARKER, world_sig + "\n")
        except OSError as e:                         # 署名が刻めなければ後段は「不明」として動かない
            rep["world_sig_error"] = f"{e.__class__.__name__}"
    # 完了 Gate: 「作り切れた」ときだけ公開中と差し替える。文書ごとの変換失敗は failed notice へ縮退済みの正常系で、
    # ここで見るのは縮退すらできなかった失敗。止めた場合は公開中の旧内容が生き続ける。
    # `document_ir_failed` はここに含めない（failed notice へ縮退する正常系。`.document_ir_sig` を確定せず次回 sync が再試行する）。
    blocking = {key: rep.get(key) or 0 for key in (
        "rag_failed", "evidence_ir_failed", "unhandled_failed") if rep.get(key)}
    if blocking:
        for s in all_staging:
            shutil.rmtree(s, ignore_errors=True)
        rep["error"] = "derived_incomplete:" + ",".join(f"{k}={v}" for k, v in sorted(blocking.items()))
        return rep
    # 3 層それぞれ独立に改名 2 回で差し替える。跨ぎでの原子性は無く、1 層だけ失敗した場合は次回 sync の drift 検知/全再構築が自己修復する。
    # 1 層の失敗が他層の公開試行を止めない（`try/except` を層ごとに独立させる）。
    publish_failures: list[str] = []
    for staging_dir, published_dir in (
        (staging_ir, published_ir), (staging_rag, published_rag), (staging, published),
    ):
        try:
            _publish_staging(staging_dir, published_dir)
        except OSError as e:
            # `_publish_staging` は後半 rename 失敗時に旧公開中（retired）を published へロールバックする。
            # ロールバックも失敗した場合は published が不在のまま（旧内容は retired に残る・`_recover_interrupted_swap` 参照）。
            publish_failures.append(f"{published_dir.name}:{e.__class__.__name__}")
    # 成功した層は staging が rename 済みで no-op。失敗した層だけ、公開できなかった新内容がここで掃除される。
    for s in all_staging:
        shutil.rmtree(s, ignore_errors=True)
    if publish_failures:
        rep["error"] = "derived_publish_failed:" + ",".join(publish_failures)
    return rep


def _check_partial_extraction(rp: Path, md: str, rel: str, document, out: list[dict]) -> None:
    """静かな部分抽出の疑いを検知する（安価な整合チェックのみ）。

    失敗にはしない。疑いがあれば `out` へ `{"doc": rel, "basis": "size_ratio"|"xlsx_row_ratio", ...根拠の数値}` を
    1 文書 1 件だけ追記する（`size_ratio` を先に見る）。
    `document` の `sheet` 要素の `source_map["partial_extraction_suspected"]`（`_build_xlsx_ir` が判定済み）を読むだけで再判定しない。
    自己申告の打切り（`sheet.source_map["truncated"]`）がある文書は `size_ratio` 判定だけ省略する（生成 MD が小さいのは自己申告済みで正常）。
    xlsx_row_ratio 走査は打切りの有無に関わらず必ず実行する（他シートの疑いを消さないため）。
    """
    has_truncated_sheet = document is not None and any(
        e.type == "sheet" and isinstance(e.source_map, dict) and e.source_map.get("truncated")
        for e in document.elements)
    if not has_truncated_sheet:
        try:
            source_bytes = rp.stat().st_size
        except OSError:
            source_bytes = None
        if source_bytes is not None and source_bytes >= _PARTIAL_SIZE_MIN_SOURCE_BYTES:
            md_bytes = len(md.encode("utf-8"))
            if md_bytes < _PARTIAL_SIZE_MAX_MD_BYTES:
                out.append({"doc": rel, "basis": "size_ratio", "source_bytes": source_bytes, "md_bytes": md_bytes})
                return
    if document is not None:
        for e in document.elements:
            sm = e.source_map
            if e.type == "sheet" and isinstance(sm, dict) and sm.get("partial_extraction_suspected"):
                out.append({"doc": rel, "basis": "xlsx_row_ratio",
                           "declared_rows": sm.get("declared_rows"), "extracted_rows": sm.get("extracted_rows")})
                return


# ---- per-file 変換結果キャッシュ ----
# 途中死からの再実行で 0 から変換し直さないよう、変換本体（アーム実行＋Evidence/RAG 生成）の結果を再ビルドを跨いで保持する
# （旧形式変換の `legacy_convert.cache_root_for` と同型）。キー＝(原本の resolved path・st_size・st_mtime_ns・変換パイプライン署名)。
# ヒットしたら実変換を丸ごとスキップし、キャッシュ済みの派生一式をステージングへコピーする。
# 失敗ファイル（notice へ縮退したもの）は対象外で、次回 sync が必ず再試行する。
_CONV_CACHE_DIRNAME = "_conv_cache"
# per-file キャッシュがミラーする sidecar 種別（`_LAYER_FOR_SIDECAR_SUFFIX` の部分集合）。
# `.derived.json` は復元後に `_write_derived_sidecar_manifest` が毎回書き直し、`.ocr_route.json` は `build_derived` が資料フォルダ単位で書くため対象外。
_CONV_CACHE_SIDECAR_SUFFIXES = (
    ".md", ".md.meta.json", ".evidence.json", ".document.json", ".rag.md", ".rag_chunks.jsonl",
)
# rep_delta のうち「MD 自体は成功したが一部書込が失敗した」ことを示すカウンタ。
# いずれかが非ゼロの結果はキャッシュへ保存せず、既存キャッシュも復元時に拒否する（失敗を焼き付けず、次回 sync で再試行させる）。
_CONV_CACHE_FAILED_DELTA_KEYS = ("document_ir_failed", "evidence_ir_failed", "rag_failed")


def _conv_cache_root_for(derived_md_dir) -> Path:
    """派生 MD dir と同階層の per-file 変換結果キャッシュ dir。`md`/`rag`/`ir` の兄弟に置くため、改名 2 回の差し替えに巻き込まれず再ビルドを跨いで残る。"""
    return Path(derived_md_dir).parent / _CONV_CACHE_DIRNAME


def _current_conv_cache_pipeline_sig(*, ocr_observation_marker: str | None) -> str:
    """per-file キャッシュのヒット判定に使う変換パイプライン全体の署名。

    既存の各層版マーカー（`_current_arms_sig`／`_current_document_ir_sig`／`_current_human_md_sig`／`_current_rag_sig`）を束ねるだけ。
    いずれかが変われば全ファイルがキャッシュミスになる。`ocr_observation_marker` は `_current_rag_sig` 経由で含める。
    """
    return (f"arms={_current_arms_sig()};"
            f"document_ir={_current_document_ir_sig()};"
            f"human_md={_current_human_md_sig()};"
            f"rag={_current_rag_sig(ocr_observation_marker=ocr_observation_marker)}")


def _conv_cache_source_key(rp: Path, pipeline_sig: str) -> str | None:
    """キャッシュキー（原本の resolved path・st_size・st_mtime_ns・st_ctime_ns・パイプライン署名）。

    `st_ctime_ns` を含めるのは、同じ size・mtime のまま中身を上書きされた原本を検知するため（`ingest/worker.py` の資料フォルダ署名と同じ材料）。
    全ファイルハッシュまでは取らない。`rp.resolve()`/`rp.stat()` が失敗したらキャッシュ対象外として None（実変換へフォールバック）。
    """
    try:
        resolved = str(rp.resolve())
        st = rp.stat()
    except OSError:
        return None
    return f"{resolved}|{st.st_size}|{st.st_mtime_ns}|{st.st_ctime_ns}|{pipeline_sig}"


def _conv_cache_slot(cache_root: Path, rel: str) -> tuple[Path, Path]:
    """`rel` のキャッシュスロット（メタ JSON・内容ディレクトリ）。1 rel につき最新 1 件のみ保持する。"""
    return cache_root / (rel + ".key.json"), cache_root / (rel + ".d")


def _conv_cache_lookup(cache_root: Path, rel: str, want_key: str) -> tuple[dict, Path] | None:
    """キャッシュヒットなら `(rep_delta, content_dir)` を返す。ミス/壊れ/鍵不一致は None。

    `rep_delta` に失敗カウンタ（`_CONV_CACHE_FAILED_DELTA_KEYS`）が非ゼロで残っている実体はミス扱いにする。
    """
    meta_path, content_dir = _conv_cache_slot(cache_root, rel)
    meta = json_io.read_json(meta_path, default=None)
    if not isinstance(meta, dict) or meta.get("key") != want_key:
        return None
    rep_delta = meta.get("rep_delta")
    if not isinstance(rep_delta, dict) or not content_dir.is_dir():
        return None
    if any(rep_delta.get(k) for k in _CONV_CACHE_FAILED_DELTA_KEYS):
        return None
    return rep_delta, content_dir


def _conv_cache_restore(content_dir: Path, rel: str, dr: Path, dr_rag: Path, dr_ir: Path) -> bool:
    """キャッシュ内容一式を 3 層のステージングへコピーする（成功時 True）。失敗（OSError）したら呼び出し元は実変換へフォールバックする。"""
    roots = {"md": dr, "rag": dr_rag, "ir": dr_ir}
    try:
        for suffix in _CONV_CACHE_SIDECAR_SUFFIXES:
            src = content_dir / _LAYER_FOR_SIDECAR_SUFFIX[suffix] / (rel + suffix)
            if not src.is_file():
                continue
            dst = roots[_LAYER_FOR_SIDECAR_SUFFIX[suffix]] / (rel + suffix)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        assets_src = content_dir / "rag" / (rel + ".assets")
        if assets_src.is_dir():
            assets_dst = dr_rag / (rel + ".assets")
            shutil.rmtree(assets_dst, ignore_errors=True)
            shutil.copytree(assets_src, assets_dst)
        return True
    except OSError:
        _log.warning(
            "変換結果キャッシュの復元に失敗しました（実変換へフォールバックします）: %s", rel, exc_info=True)
        return False


def _conv_cache_store(cache_root: Path, rel: str, key: str, rep_delta: dict,
                      dr: Path, dr_rag: Path, dr_ir: Path) -> None:
    """この rel の変換結果一式（成功時のみ呼ばれる）をキャッシュへ保存する（best-effort）。

    一時 dir へコピーしてから改名する。メタ JSON（鍵・rep_delta）は内容の改名が終わった後に書く（鍵一致だけがヒット判定の根拠のため）。
    失敗しても取り込みは継続する。
    """
    roots = {"md": dr, "rag": dr_rag, "ir": dr_ir}
    meta_path, content_dir = _conv_cache_slot(cache_root, rel)
    tmp_dir = content_dir.with_name(content_dir.name + ".tmp")
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        wrote_any = False
        for suffix in _CONV_CACHE_SIDECAR_SUFFIXES:
            layer = _LAYER_FOR_SIDECAR_SUFFIX[suffix]
            src = roots[layer] / (rel + suffix)
            if not src.is_file():
                continue
            dst = tmp_dir / layer / (rel + suffix)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            wrote_any = True
        assets_src = dr_rag / (rel + ".assets")
        if assets_src.is_dir():
            shutil.copytree(assets_src, tmp_dir / "rag" / (rel + ".assets"))
            wrote_any = True
        if not wrote_any:                     # 何も書かれなかった rel はキャッシュする意味が無い
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return
        shutil.rmtree(content_dir, ignore_errors=True)
        tmp_dir.rename(content_dir)
        json_io.write_json_atomic(meta_path, {"key": key, "rep_delta": rep_delta})
    except OSError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _log.warning(
            "変換結果キャッシュの保存に失敗しました（次回 sync も実変換します）: %s", rel, exc_info=True)


def _conv_cache_prune(cache_root: Path, seen_rels: set) -> None:
    """今回の原本一覧（`seen_rels`）に無い rel のキャッシュを削除する。per-file ループを完走したときだけ呼ぶ（途中死した場合は剪定しない）。"""
    if not cache_root.is_dir():
        return
    for meta_path in cache_root.rglob("*.key.json"):
        rel = meta_path.relative_to(cache_root).as_posix()[: -len(".key.json")]
        if rel in seen_rels:
            continue
        try:
            meta_path.unlink(missing_ok=True)
            shutil.rmtree(cache_root / (rel + ".d"), ignore_errors=True)
        except OSError:
            pass


def _build_derived_into_staging(
    wd, derived, *, progress: Callable[[int, int], None] | None = None, world: str | None = None,
) -> dict:
    """`wd` 配下の Office を MD 化して派生物を書き出す（毎回まるごと作り直す）。

    書き込み先は公開中ではなくステージング（`{derived}.staging`）で、作り切ってから改名 2 回で差し替える（`_publish_staging`）。
    ソース（wd）には書かない。`world` を渡すと、旧公開世代に紐づく OCR 観測を VLM と合流して rag.md へ含める（`_build_observation_set`）。
    per-file ループの各 rel は、原本 mtime/size と変換パイプライン署名が前回成功時と一致すれば `_conv_cache_root_for(dr)` から復元して実変換をスキップする
    （失敗＝notice へ縮退したものは対象外）。

    返値（status 表示用）: `{converted, failed, unsupported, by_ext, legacy_converted, legacy_conversion_failures,
    document_ir_generated, document_ir_failed, document_ir_failures, evidence_ir_generated, evidence_ir_failed,
    evidence_ir_failures, rag_generated, rag_failed, rag_failures, error?}`。
    - `failed`＝変換失敗、`unsupported`＝PDF/旧バイナリの未対応、`legacy_converted`＝旧形式の前段変換成功件数。
    - `legacy_conversion_failures`・`*_failures` は `[{"doc": rel, "reason": ...}]`。
    - 例外は投げない。派生先がソース配下と重なる/セットアップ失敗時は `error` を立てて何も書かずに返す（READ-ONLY source 保護）。
      1 ファイル分の変換は try/except で包み、1 件の失敗で他ファイルを止めない。
    - `document_ir_failed == 0` のときだけ `.document_ir_sig`、`evidence_ir_failed == 0` のときだけ `.evidence_ir_sig` を書く。
    """
    from .. import scope_infer as si
    # IRキーは早期 return（overlap/setup 失敗）でも同じ形で返す（レポート契約の一貫性）。
    rep = {"converted": 0, "published_notice_count": 0, "failed": 0, "unsupported": 0,
           "unhandled_failed": 0, "unhandled_failures": [], "by_ext": {},
           "legacy_converted": 0, "legacy_conversion_failures": [], "conversion_failures": [],
           "partial_extraction_suspected": [],
           "document_ir_generated": 0, "document_ir_failed": 0, "document_ir_failures": [],
           "evidence_ir_generated": 0, "evidence_ir_failed": 0, "evidence_ir_failures": [],
           "rag_generated": 0, "rag_failed": 0, "rag_failures": [],
           "office_display_requested": 0, "office_display_applied": 0,
           "office_display_fallback_docs": 0, "office_display_profiles": []}
    wd = Path(wd).resolve()
    try:
        dr = Path(derived).resolve()
    except OSError:
        dr = Path(derived)
    if _within(dr, wd) or _within(wd, dr):               # 派生先が READ-ONLY source と重なる＝書かない
        rep["error"] = "derived_overlaps_source"
        return rep
    published = dr                                       # 公開中の派生ディレクトリ（呼び出し側が読む場所＝md層）
    published_rag = _sibling_layer_dir(published, "rag")
    published_ir = _sibling_layer_dir(published, "ir")
    if any(_within(p, wd) or _within(wd, p) for p in (published_rag, published_ir)):
        rep["error"] = "derived_overlaps_source"
        return rep
    try:
        _recover_interrupted_swap(published)             # 前回の差し替えが中断していたら戻す（3層それぞれ）
        _recover_interrupted_swap(published_rag)
        _recover_interrupted_swap(published_ir)
        dr = published.with_name(published.name + _STAGING_SUFFIX)   # 以降 .md/.md.meta.json はここへ
        dr_rag = published_rag.with_name(published_rag.name + _STAGING_SUFFIX)  # .rag.md/.rag_chunks.jsonl/.assets
        dr_ir = published_ir.with_name(published_ir.name + _STAGING_SUFFIX)     # .document.json/.evidence.json/.derived.json/.ocr_route.json
        for staging_dir in (dr, dr_rag, dr_ir):
            shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)   # ビルド実施の印（失敗ファイルがあっても dir はある）
    except OSError as e:
        rep["error"] = f"derived_setup_failed:{e.__class__.__name__}"
        return rep
    from . import arms as _arms
    from . import document_ir
    from . import evidence_ir
    from . import evidence_render
    from . import legacy_provenance
    from .arms import legacy_convert
    from .arms import ooxml_arm
    converted = published_notice_count = failed = unsupported = unhandled_failed = 0
    legacy_converted = 0   # 旧形式（.doc/.xls/.ppt）の前段変換成功数
    unhandled_failures: list[dict] = []
    legacy_conversion_failures: list[dict] = []
    conversion_failures: list[dict] = []
    partial_extraction_suspected: list[dict] = []
    document_ir_generated = document_ir_failed = 0
    document_ir_failures: list[dict] = []
    evidence_ir_generated = evidence_ir_failed = 0
    evidence_ir_failures: list[dict] = []
    rag_generated = rag_failed = 0
    rag_failures: list[dict] = []
    office_display_requested = office_display_applied = office_display_fallback_docs = 0
    office_display_profiles: dict[str, dict] = {}
    obs_dir = _resolve_ocr_observation_dir(world)         # 1回だけ解決（文書ごとに解決し直さない）
    ocr_observation_marker = _ocr_observation_marker_for(obs_dir)
    by = Counter()
    enabled = _arms.enabled_arms()                       # 有効アーム
    conv = convertible_exts()                            # 有効アームが今 MD化できる拡張子集合
    # PNG/JPEGはOCRなしでも画像の存在・hashをEvidence化するため、常に候補に含む。
    candidate = OFFICE_EXT | RASTER_EVIDENCE_EXT | conv
    legacy_cache = legacy_convert.cache_root_for(dr)     # 旧→新変換のキャッシュ（md/ の兄弟・再ビルドをまたいで残す）
    source_failure_notices: set[str] = set()
    conv_cache_root = _conv_cache_root_for(dr)           # per-file 変換結果キャッシュ（md/ の兄弟）
    conv_cache_pipeline_sig = _current_conv_cache_pipeline_sig(ocr_observation_marker=ocr_observation_marker)
    conv_cache_seen_rels: set[str] = set()               # 剪定用「今回の原本一覧」（成否問わず候補に入った rel）

    def _generate_evidence(
        rp: Path,
        rel: str,
        *,
        extraction_path: Path | None = None,
        legacy_ir=None,
        consume_legacy: bool = False,
        legacy_conversion: dict | None = None,
        prebuilt_evidence=None,
    ) -> str | None:
        nonlocal evidence_ir_generated, evidence_ir_failed, rag_generated, rag_failed
        nonlocal office_display_requested, office_display_applied, office_display_fallback_docs
        actual = extraction_path or rp
        display_report: dict = {}
        try:
            extracted_evidence = prebuilt_evidence or _extract_canonical_evidence(
                rp, extraction_path=actual, legacy_ir=legacy_ir, consume_legacy=consume_legacy,
                legacy_conversion=legacy_conversion, office_display_report=display_report)
            evidence_ir.write_json_atomic(dr_ir / (rel + ".evidence.json"), extracted_evidence)
            evidence_ir_generated += 1
        except OSError:
            evidence_ir_failed += 1
            evidence_ir_failures.append({"doc": rel, "reason": "write_failed"})
            _log.warning(
                "evidence.json の書込に失敗しました（次回 sync で再試行）: %s", rel)
            return None
        except Exception as e:
            _log.warning(
                "Evidence IR生成に失敗したためsource-level failed noticeへ縮退します: %s", rel, exc_info=True)
            try:
                extracted_evidence = _build_source_failure_evidence(
                    rp, detail={"error_class": e.__class__.__name__})
                evidence_ir.write_json_atomic(dr_ir / (rel + ".evidence.json"), extracted_evidence)
                evidence_ir_generated += 1
                source_failure_notices.add(rel)
            except Exception as fallback_error:
                evidence_ir_failed += 1
                evidence_ir_failures.append({
                    "doc": rel,
                    "reason": f"fallback_failed:{fallback_error.__class__.__name__}",
                })
                _log.warning(
                    "source-level failed Evidenceの生成にも失敗しました: %s", rel, exc_info=True)
                return None
        if display_report.get("enabled"):
            office_display_requested += int(display_report.get("requested_cells") or 0)
            office_display_applied += int(display_report.get("applied_cells") or 0)
            if display_report.get("status") == "fallback_linux":
                office_display_fallback_docs += 1
            profile = display_report.get("worker_profile")
            if isinstance(profile, dict) and isinstance(profile.get("profile_hash"), str):
                office_display_profiles[profile["profile_hash"]] = profile
            _merge_provenance_metadata(dr / (rel + ".md"), {"office_display": display_report})
        try:
            assets_dir = dr_rag / (rel + ".assets")
            observation_set = None
            figure_texts = None
            if prebuilt_evidence is None or actual.suffix.lower() not in LEGACY_OFFICE_EXT:
                _extract_evidence_assets(rp, actual, extracted_evidence, assets_dir)
                observation_set = _build_observation_set(extracted_evidence, rel, assets_dir, obs_dir=obs_dir)
                figure_texts = _build_figure_texts(extracted_evidence, assets_dir)
            rendered = evidence_render.render(
                extracted_evidence, source_name=rel, observation_set=observation_set,
                figure_texts=figure_texts)
            json_io.write_text_atomic(
                dr_rag / (rel + ".rag.md"), _stamp_rule_only_rag_markdown(rendered.markdown))
            evidence_render.write_chunks_atomic(dr_rag / (rel + ".rag_chunks.jsonl"), rendered.chunks)
            rag_generated += 1
            return rendered.markdown
        except OSError:
            rag_failed += 1
            rag_failures.append({"doc": rel, "reason": "write_failed"})
            _log.warning(
                "pipe-free RAG表現の書込に失敗しました（次回 sync で再試行）: %s", rel)
            return None
        except Exception as e:
            # Evidence 抽出は成功しても renderer の coverage 検証だけが文書固有の構造で失敗することがある。
            # その 1 件を generation 全体の失敗にせず、原本 identity に拘束した source-level failed notice へ置き換える。
            # notice 自体の render に失敗した場合だけ rag_failed として公開を止める。
            if prebuilt_evidence is not None or rel in source_failure_notices:
                rag_failed += 1
                rag_failures.append({"doc": rel, "reason": f"render_failed:{e.__class__.__name__}"})
                _log.warning(
                    "pipe-free RAG noticeの生成に失敗しました: %s", rel, exc_info=True)
                return None
            _log.warning(
                "pipe-free RAG表現の生成に失敗したためsource-level failed noticeへ縮退します: %s",
                rel,
                exc_info=True,
            )
            try:
                failed_evidence = _build_source_failure_evidence(
                    rp, detail={"error_class": e.__class__.__name__})
                evidence_ir.write_json_atomic(dr_ir / (rel + ".evidence.json"), failed_evidence)
                rendered = evidence_render.render(failed_evidence, source_name=rel)
                json_io.write_text_atomic(
                    dr_rag / (rel + ".rag.md"), _stamp_rule_only_rag_markdown(rendered.markdown))
                evidence_render.write_chunks_atomic(dr_rag / (rel + ".rag_chunks.jsonl"), rendered.chunks)
                source_failure_notices.add(rel)
                rag_generated += 1
                return rendered.markdown
            except OSError:
                rag_failed += 1
                rag_failures.append({"doc": rel, "reason": "fallback_write_failed"})
                _log.warning(
                    "source-level failed RAG noticeの書込に失敗しました: %s", rel, exc_info=True)
                return None
            except Exception as fallback_error:
                rag_failed += 1
                rag_failures.append({
                    "doc": rel,
                    "reason": f"fallback_render_failed:{fallback_error.__class__.__name__}",
                })
                _log.warning(
                    "source-level failed RAG noticeの生成にも失敗しました: %s", rel, exc_info=True)
                return None

    def _conv_cache_rep_snapshot() -> dict:
        """このrel処理直前の rep カウンタ・リスト長のスナップショット（キャッシュの差分計算用）。`converted` 自体は含めない。"""
        return {
            "document_ir_generated": document_ir_generated,
            "document_ir_failed": document_ir_failed,
            "document_ir_failures_len": len(document_ir_failures),
            "evidence_ir_generated": evidence_ir_generated,
            "evidence_ir_failed": evidence_ir_failed,
            "evidence_ir_failures_len": len(evidence_ir_failures),
            "rag_generated": rag_generated,
            "rag_failed": rag_failed,
            "rag_failures_len": len(rag_failures),
            "partial_extraction_suspected_len": len(partial_extraction_suspected),
            "office_display_requested": office_display_requested,
            "office_display_applied": office_display_applied,
            "office_display_fallback_docs": office_display_fallback_docs,
            "office_display_profiles_keys": list(office_display_profiles),
        }

    def _conv_cache_rep_delta(before: dict) -> dict:
        """`_conv_cache_rep_snapshot()` からこの rel だけが動かした分の差分（キャッシュへ保存する値）。

        MD 自体は成功したが一部だけ書込失敗した場合のカウンタ増分も運ぶため、`converted` 以外の全カウンタ／リスト／dict の増分を丸ごと運ぶ。
        """
        return {
            "document_ir_generated": document_ir_generated - before["document_ir_generated"],
            "document_ir_failed": document_ir_failed - before["document_ir_failed"],
            "document_ir_failures": document_ir_failures[before["document_ir_failures_len"]:],
            "evidence_ir_generated": evidence_ir_generated - before["evidence_ir_generated"],
            "evidence_ir_failed": evidence_ir_failed - before["evidence_ir_failed"],
            "evidence_ir_failures": evidence_ir_failures[before["evidence_ir_failures_len"]:],
            "rag_generated": rag_generated - before["rag_generated"],
            "rag_failed": rag_failed - before["rag_failed"],
            "rag_failures": rag_failures[before["rag_failures_len"]:],
            "partial_extraction_suspected": partial_extraction_suspected[before["partial_extraction_suspected_len"]:],
            "office_display_requested": office_display_requested - before["office_display_requested"],
            "office_display_applied": office_display_applied - before["office_display_applied"],
            "office_display_fallback_docs": office_display_fallback_docs - before["office_display_fallback_docs"],
            "office_display_profiles": {
                k: v for k, v in office_display_profiles.items()
                if k not in before["office_display_profiles_keys"]
            },
        }

    def _conv_cache_apply_rep_delta(delta: dict) -> None:
        """キャッシュヒット時に保存済み差分を rep カウンタへ再生する（`converted` は呼び出し側が+1する）。"""
        nonlocal document_ir_generated, document_ir_failed
        nonlocal evidence_ir_generated, evidence_ir_failed
        nonlocal rag_generated, rag_failed
        nonlocal office_display_requested, office_display_applied, office_display_fallback_docs
        document_ir_generated += delta["document_ir_generated"]
        document_ir_failed += delta["document_ir_failed"]
        document_ir_failures.extend(delta["document_ir_failures"])
        evidence_ir_generated += delta["evidence_ir_generated"]
        evidence_ir_failed += delta["evidence_ir_failed"]
        evidence_ir_failures.extend(delta["evidence_ir_failures"])
        rag_generated += delta["rag_generated"]
        rag_failed += delta["rag_failed"]
        rag_failures.extend(delta["rag_failures"])
        partial_extraction_suspected.extend(delta["partial_extraction_suspected"])
        office_display_requested += delta["office_display_requested"]
        office_display_applied += delta["office_display_applied"]
        office_display_fallback_docs += delta["office_display_fallback_docs"]
        office_display_profiles.update(delta["office_display_profiles"])

    def _conv_cache_store_if_eligible() -> None:
        """直前に処理した rel（ループ変数を閉包で参照）を per-file キャッシュへ保存する。

        両方の成功終着点（raster／通常変換）から呼ぶ共通処理。`conv_cache_key` が None（`ext not in conv`）なら no-op。
        `converted` として計上した経路でも、Evidence/RAG/IR の一時書込（OSError）だけが失敗して `*_failed` が増えている場合は
        保存しない（保存すると次回 sync がキャッシュヒットで失敗を再生し続けるため）。
        """
        if conv_cache_key is None or conv_cache_rep_before is None:
            return
        rep_delta = _conv_cache_rep_delta(conv_cache_rep_before)
        if any(rep_delta.get(k) for k in _CONV_CACHE_FAILED_DELTA_KEYS):
            return
        rep_delta["human_md_sig"] = human_md_sig_for_rel
        _conv_cache_store(conv_cache_root, rel, conv_cache_key, rep_delta, dr, dr_rag, dr_ir)

    def _publish_failed_size_notice(rp: Path, rel: str, reason_code: str, detail: dict | None = None) -> None:
        """変換前（フルロード前）に諦めるサイズ/セル数系ガード（`size_exceeded`/`cell_count_exceeded`/`uncompressed_size_exceeded`）の共通処理。

        failed notice を発行し `failed`/`published_notice_count`/`conversion_failures` へ計上する（呼び出し側はこの後 `continue` する）。
        `detail` は実測値（測定セル数/バイト数と上限）で、Evidence の `coverage.detail` へ載せる。
        """
        nonlocal failed, published_notice_count
        failed_evidence = legacy_provenance.build_unavailable_evidence(
            rp, status="failed", reason_code=reason_code, detail=detail)
        notice_md = _generate_evidence(rp, rel, prebuilt_evidence=failed_evidence)
        if notice_md is not None:
            dst = dr / (rel + ".md")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(notice_md, encoding="utf-8")
            notice_result = _arms.ArmResult(
                md=notice_md,
                method="source_failure_notice",
                confidence=1.0,
                notes=["coverage_status=failed", f"reason_code={reason_code}"],
            )
            _write_provenance(dst, "evidence_notice", notice_result)
            published_notice_count += 1
        failed += 1
        conversion_failures.append({"doc": rel, "reason": reason_code})

    _also = _archive_also_root(world)   # アーカイブ取り込み: この関数内の全 safe_files 呼び出しで共有
    candidate_total = sum(1 for rp, _rel in si.safe_files(wd, also=_also) if rp.suffix.lower() in candidate)
    processed_candidates = 0
    if progress is not None:
        progress(0, candidate_total)

    for rp, rel in si.safe_files(wd, also=_also):
        ext = rp.suffix.lower()
        if ext not in candidate:
            continue
        if _is_sensitive_original(rp, ext):
            # 秘匿名は拡張子だけの `candidate` 集合では除けないため、変換ループで別途塞ぐ（派生 MD を一切作らない）
            _log.warning("MD化をスキップします（秘匿名のため対象外・ext=%s）", ext)
            continue
        by[ext] += 1
        conv_cache_seen_rels.add(rel)        # 剪定用「今回の原本一覧」（成否問わず候補に入った rel すべて）
        # `accepts()`/`convert()` は各アーム実装由来の想定外例外を投げうるため、1 ファイル分の処理をまるごと try/except で包む
        # （1 件の失敗で他ファイルを止めない・failed 計上のみ）
        rel_unhandled = False               # 下の except（想定外の例外）で True にする
        # この rel の `.md` が今回 human_md（`OoxmlArm`・docx/xlsx）で生成できたら `_current_human_md_sig()` を入れる。
        # マニフェストの `asset_versions.human_md` として書き、レンダラ/抽出器の版だけが変わった時にこの asset だけを選択的に再生成できるようにする。
        human_md_sig_for_rel: str | None = None
        conv_cache_key: str | None = None    # 非None＝この rel はキャッシュ対象（`ext in conv`）
        conv_cache_rep_before: dict | None = None
        _t0 = time.monotonic()
        _rss0 = _proc_rss_gib()
        _log.info("MD化を開始します: %s%s", rel,
                  f"（RSS {_rss0:.1f}G）" if _rss0 is not None else "")
        _done_label = "完了"                     # finally の完了行用（キャッシュ復元なら差し替え）
        try:
            # 入口ガード群（`size_exceeded`・`cell_count_exceeded`/`uncompressed_size_exceeded`）は per-file キャッシュ照合より先に評価する
            # （逆順だとキャッシュヒットでガードが素通りする）。キャッシュ照合は全ガード通過後にのみ行う。
            if ext not in conv:                              # PDF(バックエンド無)/旧バイナリ/該当アーム無効は未対応
                if ext in LEGACY_OFFICE_EXT:
                    unavailable_evidence = legacy_provenance.build_unavailable_evidence(
                        rp, status="unsupported", reason_code="legacy_backend_unavailable")
                    notice_md = _generate_evidence(rp, rel, prebuilt_evidence=unavailable_evidence)
                    if notice_md is not None:
                        dst = dr / (rel + ".md")
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_text(notice_md, encoding="utf-8")
                        notice_result = _arms.ArmResult(
                            md=notice_md,
                            method="legacy_source_notice",
                            confidence=1.0,
                            notes=["coverage_status=unsupported", "reason_code=legacy_backend_unavailable"],
                        )
                        _write_provenance(dst, "legacy", notice_result)
                        published_notice_count += 1
                unsupported += 1
                continue
            if ext in OFFICE_EXT and _office_size_exceeded(rp, ext):
                # 変換前に諦める＝openpyxl/pypdf 等のフルロードを一切発生させない
                _publish_failed_size_notice(rp, rel, "size_exceeded")
                continue
            if ext == ".xlsx":
                # 圧縮爆弾ガード: st_size は圧縮後サイズのため、小さい xlsx でも展開後に巨大化しうる。
                # openpyxl を開かず zip 内 dimension だけ見てセル数を見積もり、dimension が欠落/不正なら `<c ` の実カウントへフォールバックする。
                # 壊れた zip 等で見積不能なら通す。
                estimated_cells = _xlsx_estimated_cell_count(rp)
                if estimated_cells is None:
                    estimated_cells = _xlsx_actual_cell_count(rp, _XLSX_CELL_CAP)
                if estimated_cells is not None and estimated_cells > _XLSX_CELL_CAP:
                    _publish_failed_size_notice(
                        rp, rel, "cell_count_exceeded",
                        detail={"measured_cells": estimated_cells, "cap_cells": _XLSX_CELL_CAP})
                    continue
            if ext in CONVERTIBLE_EXT:
                # 非圧縮サイズガード: docx/pptx にも同型の圧縮爆弾リスクがあるため、3 形式共通の粗い網として適用する
                uncompressed = _office_uncompressed_total_bytes(rp)
                if uncompressed is not None and uncompressed > _OFFICE_UNCOMPRESSED_CAP_BYTES:
                    _publish_failed_size_notice(
                        rp, rel, "uncompressed_size_exceeded",
                        detail={"measured_bytes": uncompressed, "cap_bytes": _OFFICE_UNCOMPRESSED_CAP_BYTES})
                    continue
            # per-file キャッシュ（`ext in conv` は直前の早期 continue で確定済み）: 原本 mtime/size と変換パイプライン署名が前回成功時と一致すれば、
            # アーム実行・Evidence 生成・LLM/VLM 呼び出しを丸ごとスキップし、キャッシュ済み派生一式をステージングへコピーする。
            # ミスは下の通常経路へ流れる。
            conv_cache_key = _conv_cache_source_key(rp, conv_cache_pipeline_sig)
            if conv_cache_key is not None:
                hit = _conv_cache_lookup(conv_cache_root, rel, conv_cache_key)
                if hit is not None:
                    rep_delta, content_dir = hit
                    if _conv_cache_restore(content_dir, rel, dr, dr_rag, dr_ir):
                        _conv_cache_apply_rep_delta(rep_delta)
                        human_md_sig_for_rel = rep_delta.get("human_md_sig")
                        converted += 1
                        _done_label = "完了（キャッシュ復元）"
                        continue
                conv_cache_rep_before = _conv_cache_rep_snapshot()
            # 単体PNG/JPEGはVLM armへ渡さず、OCR非依存のEvidence/RAGを通常成果物として作る。
            if ext in RASTER_EVIDENCE_EXT:
                raster_md = _generate_evidence(rp, rel)
                if raster_md is None:
                    failed += 1
                    conversion_failures.append({"doc": rel, "reason": "raster_evidence_failed"})
                    continue
                result = _arms.ArmResult(
                    md=raster_md,
                    method="raster_metadata",
                    confidence=1.0,
                    notes=["image_content=uninterpreted", "ocr_required=false"],
                )
                dst = dr / (rel + ".md")
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(raster_md, encoding="utf-8")
                _write_provenance(dst, "raster", result)
                converted += 1
                _conv_cache_store_if_eligible()
                continue

            conv_path, extra_notes = rp, []
            legacy_conversion = None
            if ext in legacy_convert.LEGACY_EXT_MAP:         # 旧形式＝先に OOXML へ前段変換（LibreOffice 等）
                materialized = legacy_convert.ensure_ooxml(rp, rel, legacy_cache)
                if materialized is None:
                    # `backend_ready=False`（バックエンド未設定/未到達）は失敗ではなく未対応（「失敗」と「対象外」を混ぜない）。
                    # `backend_ready=True` で変換を試みて失敗した場合だけ `failed` へ計上し、`take_conversion_failure_reason()` でタイムアウトかどうかを区別する。
                    backend_ready = ext in legacy_convert.legacy_exts()
                    if backend_ready:
                        detail = legacy_convert.take_conversion_failure_reason()
                        status, reason = "failed", (
                            "legacy_conversion_timeout" if detail == "timeout" else "legacy_conversion_failed")
                    else:
                        status, reason = "unsupported", "legacy_backend_unavailable"
                    failed_evidence = legacy_provenance.build_unavailable_evidence(
                        rp, status=status, reason_code=reason)
                    notice_md = _generate_evidence(rp, rel, prebuilt_evidence=failed_evidence)
                    if notice_md is not None:
                        dst = dr / (rel + ".md")
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_text(notice_md, encoding="utf-8")
                        notice_result = _arms.ArmResult(
                            md=notice_md,
                            method="legacy_source_notice",
                            confidence=1.0,
                            notes=[f"coverage_status={status}", f"reason_code={reason}"],
                        )
                        _write_provenance(dst, "legacy", notice_result)
                        published_notice_count += 1
                    if backend_ready:
                        failed += 1
                        legacy_conversion_failures.append({"doc": rel, "reason": reason})
                    else:
                        unsupported += 1
                    continue
                conv_path, extra_notes = materialized        # 以降は変換済み OOXML を①アームへ渡す
                legacy_conversion = legacy_provenance.build(rp, conv_path, extra_notes)
                legacy_converted += 1   # 前段変換（旧→新）自体は成功（後続の MD 化成否とは独立に数える）
                # 非圧縮サイズガード: 旧形式変換後の materialized OOXML にも同じ上限を適用する
                uncompressed = _office_uncompressed_total_bytes(conv_path)
                if uncompressed is not None and uncompressed > _OFFICE_UNCOMPRESSED_CAP_BYTES:
                    _publish_failed_size_notice(
                        rp, rel, "uncompressed_size_exceeded",
                        detail={"measured_bytes": uncompressed, "cap_bytes": _OFFICE_UNCOMPRESSED_CAP_BYTES})
                    continue
                if conv_path.suffix.lower() == ".xlsx":
                    # セル数ガード: 旧形式（.xls→.xlsx）の変換後 xlsx にも、`.xlsx` 原本向けと同じガードを適用する
                    materialized_cells = _xlsx_estimated_cell_count(conv_path)
                    if materialized_cells is None:
                        materialized_cells = _xlsx_actual_cell_count(conv_path, _XLSX_CELL_CAP)
                    if materialized_cells is not None and materialized_cells > _XLSX_CELL_CAP:
                        _publish_failed_size_notice(
                            rp, rel, "cell_count_exceeded",
                            detail={"measured_cells": materialized_cells, "cap_cells": _XLSX_CELL_CAP})
                        continue
            arm_name, result = _convert_with_arms(conv_path, enabled)  # 最初に受理したアーム 1 本
            if result is None or result.md is None:
                # fail-closed: docx/xlsx は document-ir 構築失敗が直接 md=None を招く（`OoxmlArm.convert()`）。
                # 見逃すと `document_ir_failed` が 0 のまま、版マーカーが「全件成功」として確定してしまう。
                if (result is not None and result.document is None
                        and ext in ooxml_arm._IR_EXTS):
                    document_ir_failed += 1
                    reason = next(
                        (n for n in result.notes if n.startswith("document_ir_failed:")),
                        "document_ir_failed:unknown")
                    document_ir_failures.append({"doc": rel, "reason": reason})
                    # IR 構築の失敗はここで直接 failed notice を発行する（Evidence 側の独立な再抽出に成否を委ねない）。
                    # 委ねると、本文が空の docx などで notice が出ないまま `{rel}.md` が欠落し、文書が台帳・grep から消える。
                    if ext in EVIDENCE_EXT and _evidence_arm_selected(ext, arm_name):
                        error_class = reason.split(":", 1)[1] if ":" in reason else reason
                        failed_evidence = _build_source_failure_evidence(
                            rp, detail={"error_class": error_class})
                        notice_md = _generate_evidence(rp, rel, prebuilt_evidence=failed_evidence)
                        if notice_md is not None:
                            dst = dr / (rel + ".md")
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            dst.write_text(notice_md, encoding="utf-8")
                            notice_result = _arms.ArmResult(
                                md=notice_md,
                                method="source_failure_notice",
                                confidence=1.0,
                                notes=["coverage_status=failed", "reason_code=source_parse_failed"],
                            )
                            # 変換 arm 名を付けると quality Gate が通常の Document IR chain まで要求するため付けない
                            # （変換成功物ではなく、source-level failed coverage を検索可能にする notice）
                            _write_provenance(dst, "evidence_notice", notice_result)
                            published_notice_count += 1
                    failed += 1
                    continue
                if (result is not None and result.document is not None
                        and ext in (".docx", ".xlsx") and arm_name == "ooxml"):
                    # IR は構築できたが本文が無く human_md が意図的に None を返した（実質 docx のみ）＝失敗ではない。
                    # 今回の版で「空である」と記録し、human_md_sig_drift の無限再評価ループを止める（`.md` は書かない）。
                    human_md_sig_for_rel = _current_human_md_sig()
                # 画像だけのXLSXは旧rendererがNoneでもEvidence/RAGを先に残す。
                if ext in EVIDENCE_EXT and _evidence_arm_selected(ext, arm_name):
                    notice_md = _generate_evidence(rp, rel)
                    if rel in source_failure_notices and notice_md is not None:
                        dst = dr / (rel + ".md")
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_text(notice_md, encoding="utf-8")
                        notice_result = _arms.ArmResult(
                            md=notice_md,
                            method="source_failure_notice",
                            confidence=1.0,
                            notes=["coverage_status=failed", "reason_code=source_parse_failed"],
                        )
                        # 変換 arm 名を付けると quality Gate が通常の Document IR chain まで要求するため付けない
                        # （変換成功物ではなく、source-level failed coverage を検索可能にする notice）
                        _write_provenance(dst, "evidence_notice", notice_result)
                        published_notice_count += 1
                failed += 1
                # IR を持たない拡張子（PDF/PPTX 等）の一般失敗も rel・理由を残す。PDF は暗号化を判別可能な範囲で明示する。
                conversion_failures.append({
                    "doc": rel,
                    "reason": "password_protected" if (ext == ".pdf" and _pdf_is_encrypted(rp)) else "conversion_failed",
                })
                continue
            if extra_notes:                                  # 来歴に legacy_backend/soffice バージョンを追記
                result.notes = list(result.notes) + extra_notes
            dst = dr / (rel + ".md")                          # 出力名は必ず**原本 rel**（台帳/grep が一致）
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(result.md, encoding="utf-8")      # 出力MD は委譲変換のまま＝バイト一致（決定的）
            _check_partial_extraction(rp, result.md, rel, result.document, partial_extraction_suspected)
            if ext in (".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt") and arm_name == "ooxml":
                human_md_sig_for_rel = _current_human_md_sig()
            _write_provenance(
                dst, arm_name, result, legacy_conversion=legacy_conversion)  # 来歴サイドカーをESチャンクメタへ搬送
            # IR は原本が素の .docx/.pptx/.xlsx のときだけ書く。旧 .doc/.ppt/.xls は前段変換後の一時 OOXML から IR を作れてしまうが、
            # doc_id/source.path は原本 rel なのに file_type・content_hash が変換後ファイルの値になる（来歴汚染）ため、
            # legacy 経路（`ext` が原本の拡張子）では document.json を書かない。
            if ext in ooxml_arm._IR_EXTS:                     # document-ir-v2 の並行生成
                if result.document is not None:
                    result.document.doc_id = rel
                    result.document.source.path = rel        # 絶対パス（環境依存値）を派生物に残さない＝決定的
                    try:                                       # 失敗を握りつぶさず計上する（write_text_atomic）
                        json_io.write_text_atomic(dr_ir / (rel + ".document.json"),
                                                  document_ir.to_json_str(result.document))
                        document_ir_generated += 1
                    except OSError:
                        document_ir_failed += 1
                        document_ir_failures.append({"doc": rel, "reason": "write_failed"})
                        _log.warning(
                            "document.json の書込に失敗しました（次回 sync の drift 検知で再試行）: %s", rel)
                else:
                    # アーム側で IR 構築自体が例外で失敗した場合は notes に `document_ir_failed:<ExcClassName>` が残る（md は継続）。IR の失敗も握りつぶさず計上する。
                    boom = next((n for n in result.notes if n.startswith("document_ir_failed:")), None)
                    if boom is not None:
                        document_ir_failed += 1
                        document_ir_failures.append(
                            {"doc": rel, "reason": f"build_failed:{boom.split(':', 1)[1]}"})
            if (ext in EVIDENCE_EXT and _evidence_arm_selected(ext, arm_name)) or legacy_conversion is not None:
                # 現行 document.json/blocks/chunks を書き終えてから同じ Document IR を Evidence へ移す
                # （consume_legacy で table cell list を順次空にし、密表で 2 組の cell object を同時保持しない）
                evidence_md = _generate_evidence(
                    rp,
                    rel,
                    extraction_path=conv_path,
                    legacy_ir=result.document,
                    consume_legacy=result.document is not None,
                    legacy_conversion=legacy_conversion,
                )
                result.document = None
                if rel in source_failure_notices and evidence_md is not None:
                    # 通常 MD/Document IR は失敗 notice の検索表現と混在させない。文書単位 failed として notice chain だけを公開し、
                    # 他文書は同じ generation で継続する。
                    artifact = dr_ir / (rel + ".document.json")
                    if artifact.exists():
                        artifact.unlink()
                        document_ir_generated = max(0, document_ir_generated - 1)
                    human_md_sig_for_rel = None       # .md を失敗noticeで上書き＝human_md版の記録は無効
                    dst.write_text(evidence_md, encoding="utf-8")
                    notice_result = _arms.ArmResult(
                        md=evidence_md,
                        method="source_failure_notice",
                        confidence=1.0,
                        notes=["coverage_status=failed", "reason_code=source_parse_failed"],
                    )
                    _write_provenance(dst, "evidence_notice", notice_result)
                    published_notice_count += 1
                    failed += 1
                    continue
            converted += 1
            _conv_cache_store_if_eligible()
        except OSError as exc:
            _log.warning(
                "MD化中に想定外のOSErrorが発生しました（failed として継続）: %s", rp, exc_info=True)
            failed += 1
            rel_unhandled = True
            # reason はクラス名のみ（str(exc)は原本パス/内容断片を含みうるためUI/DBへは載せない）。
            unhandled_failures.append({"doc": rel, "reason": f"unhandled_os_error:{exc.__class__.__name__}"})
        except Exception as exc:
            _log.warning(
                "MD化中に想定外の例外が発生しました（failed として継続）: %s", rp, exc_info=True)
            failed += 1
            rel_unhandled = True
            unhandled_failures.append({"doc": rel, "reason": f"unhandled_exception:{exc.__class__.__name__}"})
        finally:
            if rel_unhandled:
                # 想定外の例外で終わった rel は sidecar が不完全な途中状態になりうるため、マニフェスト化しない
                # （マニフェスト欠落＝次回 sync が要再生成として拾う）
                unhandled_failed += 1
            elif not _write_derived_sidecar_manifest(dr, dr_rag, dr_ir, rel, human_md_sig=human_md_sig_for_rel):
                # マニフェスト自体の書込失敗も unhandled_failed へ計上し、公開 Gate（build_derived）で止める（不完全な世代を「成功」と偽らない）
                unhandled_failed += 1
                unhandled_failures.append({"doc": rel, "reason": "manifest_write_failed"})
            _rss1 = _proc_rss_gib()
            _log.info("MD化が%sしました: %s（%.1f秒%s）",
                      "失敗" if rel_unhandled else _done_label, rel, time.monotonic() - _t0,
                      f"・RSS {_rss0:.1f}G→{_rss1:.1f}G" if _rss0 is not None and _rss1 is not None else "")
            processed_candidates += 1
            if progress is not None:
                progress(processed_candidates, candidate_total)
    # per-file ループを完走したときだけ剪定する（`_conv_cache_prune` 参照）。
    # 途中死では呼ばれず、生きている rel のキャッシュは次回 sync で再利用できる。
    _conv_cache_prune(conv_cache_root, conv_cache_seen_rels)
    _write_arms_sig_marker(dr)                           # この派生を作った時のアーム構成を刻む（後の drift 判定用）
    if document_ir_failed == 0:                          # 全 IR が正常に書けた時だけ IR 版マーカーを刻む
        _write_document_ir_sig_marker(dr)
    else:
        _remove_marker(dr, _DOCUMENT_IR_SIG_MARKER)      # 失敗を現行値マーカーで隠さない（次回 sync が必ず drift）
    if evidence_ir_failed == 0:
        _write_evidence_ir_sig_marker(dr)
    else:
        _remove_marker(dr, _EVIDENCE_IR_SIG_MARKER)
    if evidence_ir_failed == 0 and rag_failed == 0:
        _write_rag_sig_marker(dr, ocr_observation_marker=ocr_observation_marker)
    else:
        _remove_marker(dr, _RAG_SIG_MARKER)
    rep.update(converted=converted, published_notice_count=published_notice_count,
               failed=failed, unsupported=unsupported, unhandled_failed=unhandled_failed,
               unhandled_failures=unhandled_failures, by_ext=dict(by),
               legacy_converted=legacy_converted,
               legacy_conversion_failures=legacy_conversion_failures,
               conversion_failures=conversion_failures,
               partial_extraction_suspected=partial_extraction_suspected,
               document_ir_generated=document_ir_generated, document_ir_failed=document_ir_failed,
               document_ir_failures=document_ir_failures,
               evidence_ir_generated=evidence_ir_generated, evidence_ir_failed=evidence_ir_failed,
               evidence_ir_failures=evidence_ir_failures,
               rag_generated=rag_generated, rag_failed=rag_failed, rag_failures=rag_failures,
               office_display_requested=office_display_requested,
               office_display_applied=office_display_applied,
               office_display_fallback_docs=office_display_fallback_docs,
               office_display_profiles=[office_display_profiles[key] for key in sorted(office_display_profiles)])
    return rep


def refresh_document_ir(wd, derived, *, write_document_ir_sig_marker: bool = True,
                        world: str | None = None) -> dict:
    """IR だけの軽量再生成。`build_derived` と違い derived を全消去しない（MD／meta.json／`.arms_sig` には触れない）。

    `wd` を `scope_infer.safe_files` で歩き、原本が素の `.docx`/`.pptx`/`.xlsx`（旧形式は対象外）かつ対応する `derived/{rel}.md` が既に在る文書だけを
    `ooxml_arm._build_docx_ir`/`_build_pptx_ir`/`_build_xlsx_ir` で再構築して原子書込し、原本が無くなった stale な `*.document.json` は削除する。
    `world` を渡すと zip/tar 展開先（`_archive_also_root`）も歩く（省くと展開先の `.document.json` が stale として削除される）。
    マーカー `.document_ir_sig` は資料フォルダ単位で 1 つのため、対象の OOXML 文書は全件再生成する。

    返値 `{document_ir_generated, document_ir_failed, document_ir_failures:[{doc, reason}]}`。
    全件成功のときだけ `.document_ir_sig` を更新する。`write_document_ir_sig_marker=False` のときは確定せず、
    呼び出し元が連鎖した evidence/rag（RAG_ES 有効時は ES 反映）の成功を確認してから `write_document_ir_sig_marker()` で確定する。
    1 文書ごとの想定内の失敗は個別に計上し、想定外の例外は呼び出し元（`worker.sync`）へ伝播する。
    """
    from .. import scope_infer as si
    from . import document_ir
    from .arms import ooxml_arm
    wd = Path(wd).resolve()
    try:
        dr = Path(derived).resolve()
    except OSError:
        dr = Path(derived)
    dr_ir = _sibling_layer_dir(dr, "ir")           # `.document.json` は ir 層
    # `build_derived` と同じ重なりガード: derived がソース配下と重なる/同一だと、`.document.json` 書込と stale 一掃 unlink が
    # READ-ONLY source の汚染・削除になる。書く前に拒否する。
    if _within(dr, wd) or _within(wd, dr) or _within(dr_ir, wd) or _within(wd, dr_ir):
        return {"document_ir_generated": 0, "document_ir_failed": 0, "document_ir_failures": [],
                "error": "derived_overlaps_source"}
    generated = failed = 0
    failures: list[dict] = []
    seen_ir: set[str] = set()
    for rp, rel in si.safe_files(wd, also=_archive_also_root(world)):
        ext = rp.suffix.lower()
        if ext not in ooxml_arm._IR_EXTS:
            continue
        if _is_sensitive_original(rp, ext):
            # 秘匿名は document_ir を持たない（`_is_sensitive_original` 参照）。stale な旧 `.md` が残っていた場合の再生成を塞ぐため明示的に除外する。
            continue
        md_path = dr / (rel + ".md")
        if not md_path.is_file():                            # MD 自体が無い（未対応/未変換）は対象外
            continue
        seen_ir.add(rel)
        try:
            if ext == ".docx":
                ir = ooxml_arm._build_docx_ir(rp)
            elif ext == ".pptx":
                ir = ooxml_arm._build_pptx_ir(rp)
            else:
                ir = ooxml_arm._build_xlsx_ir(rp)
        except Exception as e:
            failed += 1
            failures.append({"doc": rel, "reason": f"build_failed:{e.__class__.__name__}"})
            continue
        if ir is None:
            failed += 1
            failures.append({"doc": rel, "reason": "build_failed:None"})
            continue
        ir.doc_id = rel
        ir.source.path = rel                                  # 絶対パス（環境依存値）を派生物に残さない＝決定的
        try:
            json_io.write_text_atomic(dr_ir / (rel + ".document.json"), document_ir.to_json_str(ir))
            generated += 1
        except OSError:
            failed += 1
            failures.append({"doc": rel, "reason": "write_failed"})
    for doc_path in dr_ir.rglob("*.document.json"):           # stale な document.json（原本が消えた）を一掃
        rel = doc_path.relative_to(dr_ir).as_posix()[: -len(".document.json")]
        if rel not in seen_ir:
            try:
                doc_path.unlink()
            except OSError:
                pass
    if failed == 0:
        if write_document_ir_sig_marker:                  # False＝呼び出し元が連鎖成功を確認後に確定する
            _write_document_ir_sig_marker(dr)
    else:
        _remove_marker(dr, _DOCUMENT_IR_SIG_MARKER)
    return {"document_ir_generated": generated, "document_ir_failed": failed, "document_ir_failures": failures}


_DERIVED_MANIFEST_SUFFIX = ".derived.json"
# `{rel}.derived.json` は原本ごとに、検索 consumer 側の設定（ES への RAG 反映の有無等）に関係なく常時生成する。
# マニフェスト形式のバージョン（`sidecars`/`assets` キーの構成が変わったら上げる）。現行値と一致しないマニフェスト
# （キー自体を持たない旧世代を含む）は `rag_sidecars_missing` が「欠落」として扱い、全再構築で書き直させる。
# このバージョンを上げた直後の最初の手動 sync は、全原本分の全再構築（`run()`）が 1 回だけ走る。
_DERIVED_MANIFEST_SCHEMA_VERSION = "derived-sidecar-manifest-v1"
# マニフェストが記録しうる sidecar 種別（この5種のうち実際に書かれたものだけを列挙する）。
_MANIFEST_SIDECAR_SUFFIXES = (".md", ".md.meta.json", ".evidence.json", ".rag.md", ".rag_chunks.jsonl")


def _write_derived_sidecar_manifest(dr: Path, dr_rag: Path, dr_ir: Path, rel: str, *,
                                    human_md_sig: str | None = None) -> bool:
    """`rel` について実際に生成された sidecar 集合を記録する（sidecar 欠落検知の唯一の正本）。

    `dr`/`dr_rag`/`dr_ir` は md/rag/ir 3 層の物理ルート。マニフェストは ir 層（`{rel}.derived.json`）に書き、sidecar の存在確認は
    `_LAYER_FOR_SIDECAR_SUFFIX` で層を引いて行う。候補（`_MANIFEST_SIDECAR_SUFFIXES`）のうち存在するものだけを記録する（無ければ空リスト）。
    `{rel}.assets/` があれば中身のファイル名一覧を `assets` キーへ、現行の `_DERIVED_MANIFEST_SCHEMA_VERSION` も必ず書く。
    `human_md_sig`: human_md（`OoxmlArm`・docx/xlsx）出力を今回評価したときだけ `_current_human_md_sig()` を渡し、`asset_versions.human_md` として書く
    （`.md` の有無とは独立）。None なら既存マニフェストの値を引き継ぐ。
    呼び出し元は、対象 rel の処理が正常に完了した場合にだけ呼ぶ。戻り値は書込成功なら True、失敗（OSError）なら False
    （呼び出し元はその rel を失敗として扱い公開を止める）。
    """
    roots = {"md": dr, "rag": dr_rag, "ir": dr_ir}
    present = [suffix for suffix in _MANIFEST_SIDECAR_SUFFIXES
              if (roots[_LAYER_FOR_SIDECAR_SUFFIX[suffix]] / (rel + suffix)).is_file()]
    manifest = {"schema": _DERIVED_MANIFEST_SCHEMA_VERSION, "sidecars": present}
    try:
        asset_dir = dr_rag / (rel + ".assets")
        if asset_dir.is_dir():
            manifest["assets"] = sorted(p.name for p in asset_dir.iterdir() if p.is_file())
        if human_md_sig is not None:
            manifest["asset_versions"] = {"human_md": human_md_sig}
        else:
            old = json_io.read_json(dr_ir / (rel + _DERIVED_MANIFEST_SUFFIX), default=None)
            old_versions = old.get("asset_versions") if isinstance(old, dict) else None
            old_human_md = old_versions.get("human_md") if isinstance(old_versions, dict) else None
            if old_human_md is not None:
                manifest["asset_versions"] = {"human_md": old_human_md}
        json_io.write_text_atomic(
            dr_ir / (rel + _DERIVED_MANIFEST_SUFFIX), json.dumps(manifest, ensure_ascii=False))
        return True
    except OSError:
        _log.warning(
            "sidecarマニフェストの書込に失敗しました（次回 sync が欠落として検知し再生成する）: %s", rel)
        return False


def rag_sidecars_missing(wd, derived, *, world: str | None = None) -> bool:
    """原本ごとの生成時マニフェスト（`_write_derived_sidecar_manifest`）を読み、そこに列挙された sidecar・asset が 1 つでも欠落していれば True。

    - `world`（アーカイブ取り込み）: 渡すと zip/tar 展開先も合流して評価する（省略時は `wd` だけ）。
    - 生成した側（`build_derived`/`refresh_evidence_ir`/`refresh_rag`）が実際に書いた sidecar の記録をそのまま照合する。
      マニフェスト自体が無い rel も欠落扱いとして、1 回だけ `worker.sync()` の全再構築フォールバックを誘発する（自己修復）。
    - マニフェストの `schema` が現行の `_DERIVED_MANIFEST_SCHEMA_VERSION` と一致しない場合も欠落扱いとして、現行形式で書き直させる。
    Evidence 生成が恒久的に失敗し続ける原本があると毎回 True を返し、sync のたびに全再構築へフォールバックする。
    """
    from .. import scope_infer as si

    wd = Path(wd).resolve()
    dr = Path(derived)
    dr_rag = _sibling_layer_dir(dr, "rag")
    dr_ir = _sibling_layer_dir(dr, "ir")
    roots = {"md": dr, "rag": dr_rag, "ir": dr_ir}
    # build_derived が候補として処理しうる拡張子とだけ突き合わせる（それ以外の原本はマニフェストを持たない対象外）
    manifest_candidates = OFFICE_EXT | RASTER_EVIDENCE_EXT | convertible_exts()
    for rp, rel in si.safe_files(wd, also=_archive_also_root(world)):
        ext = rp.suffix.lower()
        if ext not in manifest_candidates:
            continue
        if _is_sensitive_original(rp, ext):
            # 秘匿名は変換ループが MD/派生/マニフェストを作らない＝「欠落」ではなく「対象外」（`_is_sensitive_original` 参照）。
            # 塞がないと毎 sync で欠落判定→全再構築ループが続く。
            continue
        manifest = json_io.read_json(dr_ir / (rel + _DERIVED_MANIFEST_SUFFIX), default=None)
        if (not isinstance(manifest, dict)
                or manifest.get("schema") != _DERIVED_MANIFEST_SCHEMA_VERSION
                or not isinstance(manifest.get("sidecars"), list)):
            return True
        for suffix in manifest["sidecars"]:
            if not isinstance(suffix, str):
                return True
            layer = _LAYER_FOR_SIDECAR_SUFFIX.get(suffix)
            if layer is None or not (roots[layer] / (rel + suffix)).is_file():
                return True
        assets = manifest.get("assets")
        if assets is not None:
            if not isinstance(assets, list):
                return True
            asset_dir = dr_rag / (rel + ".assets")
            if not asset_dir.is_dir():
                return True
            for name in assets:
                if not isinstance(name, str) or not (asset_dir / name).is_file():
                    return True
    return False


def refresh_evidence_ir(wd, derived, *, write_rag_sig_marker: bool = True, world: str | None = None) -> dict:
    """XLSX/DOCX/PPTX/PDF の Evidence IR を clone 済み generation 上で軽量再生成する。

    原本が素の ``.xlsx``/``.docx`` で既存 MD の provenance が ``ooxml`` の文書だけを対象にする（旧形式の一時 OOXML に誤った source hash を付けない）。
    対応原本が消えた、または担当 arm が変わった stale ``*.evidence.json`` は削除し、全件成功時だけ ``.evidence_ir_sig`` を更新する。
    `write_rag_sig_marker=False`（RAG_ES 有効時）は、生成を始める前に既存 `.rag_sig` を未確定へ戻し（削除に失敗したら `error` を返す）、
    成功時にも確定しない（呼び出し元が ES 反映の成否を確認してから確定する）。
    `world`: 公開中の OCR 観測を VLM と合流して rag.md へ含め、zip/tar 展開先（`_archive_also_root`）も歩く。
    """
    from .. import scope_infer as si
    from . import evidence_ir
    from . import evidence_render
    from . import legacy_provenance
    from .arms import legacy_convert

    wd = Path(wd).resolve()
    try:
        dr = Path(derived).resolve()
    except OSError:
        dr = Path(derived)
    dr_rag = _sibling_layer_dir(dr, "rag")
    dr_ir = _sibling_layer_dir(dr, "ir")
    if (_within(dr, wd) or _within(wd, dr)
            or _within(dr_rag, wd) or _within(wd, dr_rag)
            or _within(dr_ir, wd) or _within(wd, dr_ir)):
        return {
            "evidence_ir_generated": 0,
            "evidence_ir_failed": 0,
            "evidence_ir_failures": [],
            "rag_generated": 0,
            "rag_failed": 0,
            "rag_failures": [],
            "error": "derived_overlaps_source",
        }
    if not write_rag_sig_marker and not drop_rag_sig_marker(dr):
        return {
            "evidence_ir_generated": 0,
            "evidence_ir_failed": 0,
            "evidence_ir_failures": [],
            "rag_generated": 0,
            "rag_failed": 0,
            "rag_failures": [],
            "error": "rag_sig_unlink_failed",
        }

    generated = failed = 0
    failures: list[dict] = []
    rag_generated = rag_failed = 0
    rag_failures: list[dict] = []
    seen: set[str] = set()
    legacy_cache = legacy_convert.cache_root_for(dr)
    obs_dir = _resolve_ocr_observation_dir(world)          # 1回だけ解決
    ocr_observation_marker = _ocr_observation_marker_for(obs_dir)
    for rp, rel in si.safe_files(wd, also=_archive_also_root(world)):
        if rp.suffix.lower() not in EVIDENCE_EXT:
            continue
        ext = rp.suffix.lower()
        if _is_sensitive_original(rp, ext):
            # 秘匿名は evidence_ir/rag を持たない（`_is_sensitive_original` 参照）。PDF は image-only で `.md` 欠落だけでは対象外にならない経路があるため、
            # 明示的に塞がないと秘匿本文が `.evidence.json`/`.rag.md` へ平文で書き出される。
            continue
        md_path = dr / (rel + ".md")
        meta = json_io.read_json(dr / (rel + ".md.meta.json"), default=None)
        source_failure_notice = _is_source_failure_notice(meta)
        if ext == ".pdf":
            # image-only PDF は旧 MD が空でも Evidence/RAG を正本画像と構造から再生成する。
            # full generation で parse failure notice へ縮退済みなら、backend の現在値に左右されず notice chain を現行 schema へ再生成する。
            if not source_failure_notice and not pdf_available():
                continue
        elif ext in RASTER_EVIDENCE_EXT:
            if not md_path.is_file() or not isinstance(meta, dict) or meta.get("arm") != "raster":
                continue
        elif ext in LEGACY_OFFICE_EXT:
            if not md_path.is_file() or not isinstance(meta, dict) or meta.get("arm") not in {"ooxml", "legacy"}:
                continue
        elif md_path.is_file():
            if not isinstance(meta, dict) or (meta.get("arm") != "ooxml" and not source_failure_notice):
                continue
        else:
            # 空/image-only な正常 OOXML は `.md` を持たない場合がある。`.md` の有無だけで対象外とせず、
            # 生成時マニフェストが以前 `.evidence.json` を記録していれば regeneration 対象にする。
            prior_manifest = json_io.read_json(dr_ir / (rel + _DERIVED_MANIFEST_SUFFIX), default=None)
            prior_sidecars = prior_manifest.get("sidecars") if isinstance(prior_manifest, dict) else None
            if not (isinstance(prior_sidecars, list) and ".evidence.json" in prior_sidecars):
                continue
        seen.add(rel)
        actual = rp
        conversion = None
        rel_ok = True
        try:
            if source_failure_notice:
                extracted = _build_source_failure_evidence(
                    rp, detail=_source_failure_detail(dr_ir / (rel + ".evidence.json")))
            elif ext in legacy_convert.LEGACY_EXT_MAP:
                materialized = legacy_convert.ensure_ooxml(rp, rel, legacy_cache)
                if materialized is None:
                    backend_ready = ext in legacy_convert.legacy_exts()
                    extracted = legacy_provenance.build_unavailable_evidence(
                        rp,
                        status="failed" if backend_ready else "unsupported",
                        reason_code="legacy_conversion_failed" if backend_ready else "legacy_backend_unavailable",
                    )
                else:
                    actual, notes = materialized
                    conversion = legacy_provenance.build(rp, actual, notes)
                    extracted = _extract_canonical_evidence(
                        rp, extraction_path=actual, consume_legacy=True, legacy_conversion=conversion)
            else:
                extracted = _extract_canonical_evidence(
                    rp, extraction_path=actual, consume_legacy=True, legacy_conversion=conversion)
            evidence_ir.write_json_atomic(dr_ir / (rel + ".evidence.json"), extracted)
            generated += 1
        except OSError:
            failed += 1
            failures.append({"doc": rel, "reason": "write_failed"})
            rel_ok = False
        except Exception as exc:
            failed += 1
            failures.append({"doc": rel, "reason": f"build_failed:{exc.__class__.__name__}"})
            rel_ok = False
        else:
            try:
                asset_dir = dr_rag / (rel + ".assets")
                shutil.rmtree(asset_dir, ignore_errors=True)
                _extract_evidence_assets(rp, actual, extracted, asset_dir)
                observation_set = _build_observation_set(extracted, rel, asset_dir, obs_dir=obs_dir)
                rendered = evidence_render.render(
                    extracted, source_name=rel, observation_set=observation_set,
                    figure_texts=_build_figure_texts(extracted, asset_dir))
                json_io.write_text_atomic(
                    dr_rag / (rel + ".rag.md"), _stamp_rule_only_rag_markdown(rendered.markdown))
                evidence_render.write_chunks_atomic(dr_rag / (rel + ".rag_chunks.jsonl"), rendered.chunks)
                rag_generated += 1
            except OSError:
                rag_failed += 1
                rag_failures.append({"doc": rel, "reason": "write_failed"})
                rel_ok = False
            except Exception as exc:
                rag_failed += 1
                rag_failures.append({"doc": rel, "reason": f"render_failed:{exc.__class__.__name__}"})
                rel_ok = False
        # この rel の処理が正常に完了した時だけ、実際に書かれた sidecar をマニフェスト化する。
        # マニフェスト自体の書込に失敗した場合も rag_failed へ計上し、`.rag_sig` マーカーの確定を防ぐ。
        if rel_ok and not _write_derived_sidecar_manifest(dr, dr_rag, dr_ir, rel):
            rag_failed += 1
            rag_failures.append({"doc": rel, "reason": "manifest_write_failed"})

    for evidence_path in dr_ir.rglob("*.evidence.json"):
        rel = evidence_path.relative_to(dr_ir).as_posix()[: -len(".evidence.json")]
        if rel not in seen:
            try:
                evidence_path.unlink()
            except OSError:
                pass
    for suffix in (".rag.md", ".rag_chunks.jsonl"):
        for rag_path in dr_rag.rglob(f"*{suffix}"):
            rel = rag_path.relative_to(dr_rag).as_posix()[: -len(suffix)]
            if rel not in seen:
                try:
                    rag_path.unlink()
                except OSError:
                    pass
    for asset_dir in list(dr_rag.rglob("*.assets")):
        if not asset_dir.is_dir():
            continue
        rel = asset_dir.relative_to(dr_rag).as_posix()[: -len(".assets")]
        if rel not in seen:
            shutil.rmtree(asset_dir, ignore_errors=True)
    if failed == 0:
        _write_evidence_ir_sig_marker(dr)
    else:
        _remove_marker(dr, _EVIDENCE_IR_SIG_MARKER)
    if failed == 0 and rag_failed == 0:
        if write_rag_sig_marker:
            _write_rag_sig_marker(dr, ocr_observation_marker=ocr_observation_marker)
    else:
        _remove_marker(dr, _RAG_SIG_MARKER)
    return {
        "evidence_ir_generated": generated,
        "evidence_ir_failed": failed,
        "evidence_ir_failures": failures,
        "rag_generated": rag_generated,
        "rag_failed": rag_failed,
        "rag_failures": rag_failures,
    }


def refresh_rag(wd, derived, *, write_rag_sig_marker: bool = True, world: str | None = None) -> dict:
    """既存 Evidence 世代と同じ原本から pipe-free な Markdown/chunk/assets を再生成する。

    `write_rag_sig_marker=False` の契約は `refresh_evidence_ir` と同じ。
    `world`: OCR 完了後の再生成はこの関数（`rag_sig_drift` の OCR 観測次元が誘発）が担う。
    zip/tar 展開先（`_archive_also_root`）も `sources` に合流する（省くと `source_path is None` 判定が展開先由来の `rel` を `failed` へ誤計上する）。
    """
    from .. import scope_infer as si
    from . import evidence_ir
    from . import evidence_render
    from . import legacy_provenance
    from .arms import legacy_convert

    wd = Path(wd).resolve()
    try:
        dr = Path(derived).resolve()
    except OSError:
        dr = Path(derived)
    dr_rag = _sibling_layer_dir(dr, "rag")
    dr_ir = _sibling_layer_dir(dr, "ir")
    if (_within(dr, wd) or _within(wd, dr)
            or _within(dr_rag, wd) or _within(wd, dr_rag)
            or _within(dr_ir, wd) or _within(wd, dr_ir)):
        return {
            "rag_generated": 0,
            "rag_failed": 0,
            "rag_failures": [],
            "error": "derived_overlaps_source",
        }
    if not write_rag_sig_marker and not drop_rag_sig_marker(dr):
        return {
            "rag_generated": 0,
            "rag_failed": 0,
            "rag_failures": [],
            "error": "rag_sig_unlink_failed",
        }
    sources = {
        rel: rp for rp, rel in si.safe_files(wd, also=_archive_also_root(world))
        if rp.suffix.lower() in EVIDENCE_EXT
    }
    generated = failed = 0
    failures: list[dict] = []
    seen: set[str] = set()
    legacy_cache = legacy_convert.cache_root_for(dr)
    obs_dir = _resolve_ocr_observation_dir(world)          # 1回だけ解決
    ocr_observation_marker = _ocr_observation_marker_for(obs_dir)
    for evidence_path in sorted(dr_ir.rglob("*.evidence.json")):
        rel = evidence_path.relative_to(dr_ir).as_posix()[: -len(".evidence.json")]
        source_path = sources.get(rel)
        if source_path is None:
            failed += 1
            failures.append({"doc": rel, "reason": "source_missing"})
            continue
        if _is_sensitive_original(source_path, source_path.suffix.lower()):
            # 秘匿名は rag を持たない（`_is_sensitive_original` 参照）。`seen` へ加えず、下の cleanup ループで既存の生成物を削除させる。
            continue
        seen.add(rel)
        rel_ok = True
        try:
            # 巨大な Evidence JSON の全量 read/json.loads を避け、同じ原本と固定 parser から再構築する
            # （`evidence_sig` が一致する時だけ worker から呼ばれるため、既存 Evidence と決定的に等価）。
            actual = source_path
            conversion = None
            meta = json_io.read_json(dr / (rel + ".md.meta.json"), default=None)
            if _is_source_failure_notice(meta):
                # Evidence sig は一致済みで RAG renderer だけが drift した経路。巨大な通常 Evidence は原本から再構築し、
                # この source-level notice は小さい既存 IR を正本として読む。
                ir = evidence_ir.from_json_str(evidence_path.read_text(encoding="utf-8"))
            elif source_path.suffix.lower() in legacy_convert.LEGACY_EXT_MAP:
                materialized = legacy_convert.ensure_ooxml(source_path, rel, legacy_cache)
                if materialized is None:
                    backend_ready = source_path.suffix.lower() in legacy_convert.legacy_exts()
                    ir = legacy_provenance.build_unavailable_evidence(
                        source_path,
                        status="failed" if backend_ready else "unsupported",
                        reason_code="legacy_conversion_failed" if backend_ready else "legacy_backend_unavailable",
                    )
                else:
                    actual, notes = materialized
                    conversion = legacy_provenance.build(source_path, actual, notes)
                    ir = _extract_canonical_evidence(
                        source_path, extraction_path=actual, consume_legacy=True, legacy_conversion=conversion)
            else:
                ir = _extract_canonical_evidence(
                    source_path, extraction_path=actual, consume_legacy=True, legacy_conversion=conversion)
            asset_dir = dr_rag / (rel + ".assets")
            shutil.rmtree(asset_dir, ignore_errors=True)
            observation_set = None
            figure_texts = None
            if actual.suffix.lower() not in LEGACY_OFFICE_EXT:
                _extract_evidence_assets(source_path, actual, ir, asset_dir)
                observation_set = _build_observation_set(ir, rel, asset_dir, obs_dir=obs_dir)
                figure_texts = _build_figure_texts(ir, asset_dir)
            rendered = evidence_render.render(
                ir, source_name=rel, observation_set=observation_set, figure_texts=figure_texts)
            json_io.write_text_atomic(
                dr_rag / (rel + ".rag.md"), _stamp_rule_only_rag_markdown(rendered.markdown))
            evidence_render.write_chunks_atomic(dr_rag / (rel + ".rag_chunks.jsonl"), rendered.chunks)
            generated += 1
        except OSError:
            failed += 1
            failures.append({"doc": rel, "reason": "write_failed"})
            rel_ok = False
        except (ValueError, KeyError, TypeError) as exc:
            failed += 1
            failures.append({"doc": rel, "reason": f"render_failed:{exc.__class__.__name__}"})
            rel_ok = False
        # この rel の処理が正常に完了した時だけ、実際に書かれた sidecar をマニフェスト化する。
        # マニフェスト自体の書込に失敗した場合も failed へ計上し、`.rag_sig` マーカーの確定を防ぐ。
        if rel_ok and not _write_derived_sidecar_manifest(dr, dr_rag, dr_ir, rel):
            failed += 1
            failures.append({"doc": rel, "reason": "manifest_write_failed"})
    for suffix in (".rag.md", ".rag_chunks.jsonl"):
        for rag_path in dr_rag.rglob(f"*{suffix}"):
            rel = rag_path.relative_to(dr_rag).as_posix()[: -len(suffix)]
            if rel not in seen:
                try:
                    rag_path.unlink()
                except OSError:
                    pass
    for asset_dir in list(dr_rag.rglob("*.assets")):
        if not asset_dir.is_dir():
            continue
        rel = asset_dir.relative_to(dr_rag).as_posix()[: -len(".assets")]
        if rel not in seen:
            shutil.rmtree(asset_dir, ignore_errors=True)
    if failed == 0:
        if write_rag_sig_marker:
            _write_rag_sig_marker(dr, ocr_observation_marker=ocr_observation_marker)
    else:
        _remove_marker(dr, _RAG_SIG_MARKER)
    return {"rag_generated": generated, "rag_failed": failed, "rag_failures": failures}


def _convert_with_arms(path, enabled):
    """有効アームのうち最初に受理したアーム 1 本で変換する。返値 `(arm_name, ArmResult|None)`。受理アームが無ければ `(None, None)`。"""
    for arm in enabled:
        if arm.accepts(path):
            return arm.name, arm.convert(path)
    return None, None


def _write_provenance(
    md_path: Path,
    arm_name: str,
    result,
    *,
    legacy_conversion: dict | None = None,
) -> None:
    """変換来歴サイドカー `{md_path}.meta.json`（`{arm, method, confidence, notes}`）を書く（best-effort）。

    決定的（タイムスタンプなし・`sort_keys`）。es_index がこのサイドカーを読んでチャンクメタ（extraction_method/confidence）へ搬送する。
    """
    meta = {"arm": arm_name, "method": result.method,
            "confidence": result.confidence, "notes": list(result.notes)}
    if legacy_conversion is not None:
        meta["legacy_conversion"] = legacy_conversion
    try:
        (md_path.parent / (md_path.name + ".meta.json")).write_text(
            json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _merge_provenance_metadata(md_path: Path, values: dict) -> None:
    """既存MD来歴へ任意補完profileを追記する。sidecar未生成の経路はEvidence内profileだけを正本にする。"""
    meta_path = md_path.parent / (md_path.name + ".meta.json")
    if not meta_path.is_file():
        return
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        raw.update(values)
        json_io.write_text_atomic(
            meta_path, json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    except (OSError, ValueError):
        pass


def to_markdown(path) -> str | None:
    """Office ファイル → 決定的 Markdown 文字列。未対応形式・変換失敗は None。"""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext == ".docx":
            return _docx_md(p)
        if ext == ".pptx":
            return _pptx_md(p)
        if ext == ".xlsx":
            return _xlsx_md(p)
        if ext == ".pdf":
            return _pdf_md(p)
        if ext in RASTER_EVIDENCE_EXT:
            from . import evidence_render, raster_evidence
            return evidence_render.render(raster_evidence.extract(p), source_name=p.name).markdown
    except Exception:
        return None                                          # 壊れ/非OOXML/想定外は未対応扱い
    return None


# ---- .docx（word/document.xml 直読み）----

def _para_text(p_el) -> str:
    return "".join(t.text or "" for t in p_el.iter(f"{_W}t"))


def _heading_level(p_el) -> int | None:
    """段落スタイルが見出しなら 1-6 を返す（Heading1.. / 見出し1.. 両対応）。本文は None。"""
    style = p_el.find(f"{_W}pPr/{_W}pStyle")
    val = style.get(f"{_W}val") if style is not None else None
    if not val:
        return None
    m = re.search(r"(\d+)", val)
    lvl = int(m.group(1)) if m else 1
    if "Heading" in val or "見出し" in val or val.lower().startswith("h"):
        return max(1, min(6, lvl))
    if val.lower() in ("title", "subtitle"):
        return 1
    return None


def _table_md(tbl_el) -> str:
    rows = []
    for tr in tbl_el.findall(f"{_W}tr"):
        cells = []
        for tc in tr.findall(f"{_W}tc"):
            txt = " ".join(_para_text(p).strip() for p in tc.findall(f"{_W}p")).strip()
            cells.append(txt.replace("|", "\\|"))
        if cells:
            rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _docx_md(p: Path) -> str | None:
    """DOCX の人間向け MD。document-ir（`arms/ooxml_arm._build_docx_ir`）を土台に `human_md.render_docx` へ委譲する。IR 構築に失敗すれば未対応（None）。"""
    from .arms import ooxml_arm
    ir = ooxml_arm._build_docx_ir(p)
    return _render_human_md(ir, p, ".docx") if ir is not None else None


# ---- .pptx（ppt/slides/slideN.xml 直読み）----

_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PR = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _slide_order(z) -> list[str]:
    """表示順のスライド名（`ppt/presentation.xml` の sldIdLst → rels で解決）。失敗時は番号順。"""
    names = [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)]
    numeric = sorted(names, key=lambda n: int(re.search(r"(\d+)", n).group(1)))
    try:
        pres = ET.fromstring(z.read("ppt/presentation.xml"))
        rids = [s.get(f"{_R}id") for s in pres.iter(f"{_PR}sldId")]
        rels = ET.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
        tgt = {r.get("Id"): r.get("Target") for r in rels.iter(f"{_RELS}Relationship")}
        ordered = []
        for rid in rids:
            t = tgt.get(rid)
            if not t:
                continue
            name = ("ppt/" + t[3:]) if t.startswith("../") else ("ppt/" + t.lstrip("/")
                    if not t.startswith("ppt/") else t)
            if name in names:
                ordered.append(name)
        # presentation に出ない（削除残り等）スライドは番号順で後ろに付ける
        ordered += [n for n in numeric if n not in ordered]
        return ordered or numeric
    except Exception:
        return numeric


def _pptx_md(p: Path) -> str | None:
    from . import metafile_text

    out = []
    cache: dict[str, list[str]] = {}
    with zipfile.ZipFile(p) as z:
        for i, n in enumerate(_slide_order(z), 1):
            root = ET.fromstring(z.read(n))
            figures = metafile_text.pptx_slide_figures(p, n, cache)
            texts = _pptx_slide_texts(root, figures)
            if texts:
                out.append(f"## スライド {i}")
                out.extend(texts)
    return "\n\n".join(out) if out else None


# ---- 幾何オクルージョン（覆い図形の座標判定・pptx）----
# 覆い図形に隠されたテキストへ、MD 本文に直接マーカー行を自動出力する（対象は .pptx のみ・座標が EMU で明確）。
# 属性ベースの隠し（w:vanish・veryHidden シート・非表示スライド等）は対象外。
_HIDDEN_MARKER = "**［隠し候補：前面の図形に覆われた文字］**"
_OCCLUSION_RATIO = 0.9                                        # 交差面積÷テキストshape面積のしきい値


def _figure_text_line(lines: list[str]) -> str:
    """メタファイル図の描画命令にある文字（元の値）を 1 つのブロックにする（人間向け MD 用）。"""
    return "図の中の文字（元の値）\n" + "\n".join("- " + line for line in lines)


def _pptx_slide_texts(root, figures: dict[int, list[list[str]]] | None = None) -> list[str]:
    """1 スライド分のテキスト行（shape 単位・文書順・隠し候補マーカー付き）。

    shape 単位の歩行が失敗したら例外を握って、フラット抽出（`root.iter(f"{_A}t")`）にフォールバックする（テキストの取りこぼしを起こさない）。
    非隠しスライドは shape 単位でもフラット抽出とバイト単位で一致する。
    """
    try:
        return _pptx_slide_texts_by_shape(root, figures)
    except Exception:
        flat = _pptx_slide_texts_flat(root)
        # 位置を決められないときは、図の文字をスライド末尾へまとめる。
        return flat + [_figure_text_line(lines) for blocks in (figures or {}).values() for lines in blocks]


def _pptx_slide_texts_flat(root) -> list[str]:
    """現行のフラット抽出（フォールバック・挙動不変）。"""
    return [t.text.strip() for t in root.iter(f"{_A}t") if t.text and t.text.strip()]


def _pptx_shape_texts_list(sp) -> list[str]:
    """`p:txBody` 内の非空テキスト行（run 単位・順序保持）。"""
    txbody = sp.find(f"{_PR}txBody")
    if txbody is None:
        return []
    return [t.text.strip() for t in txbody.iter(f"{_A}t") if t.text and t.text.strip()]


def _pptx_bbox(sp) -> tuple[int, int, int, int] | None:
    """`p:spPr/a:xfrm` から bbox（x0,y0,x1,y1・EMU）。rot 付き/欠落は None（幾何判定に参加しない）。"""
    spPr = sp.find(f"{_PR}spPr")
    if spPr is None:
        return None
    xfrm = spPr.find(f"{_A}xfrm")
    if xfrm is None:
        return None
    rot = xfrm.get("rot")
    if rot is not None and rot.strip() not in ("", "0"):
        return None                                            # 回転あり＝幾何判定に参加しない（保守的）
    off = xfrm.find(f"{_A}off")
    ext = xfrm.find(f"{_A}ext")
    if off is None or ext is None:
        return None
    try:
        x, y = int(off.get("x")), int(off.get("y"))
        cx, cy = int(ext.get("cx")), int(ext.get("cy"))
    except (TypeError, ValueError):
        return None
    return (x, y, x + cx, y + cy)


def _pptx_has_solid_fill(sp) -> bool:
    """`p:spPr/a:solidFill` を持つか（`a:noFill` の場合は False）。"""
    spPr = sp.find(f"{_PR}spPr")
    if spPr is None:
        return False
    if spPr.find(f"{_A}noFill") is not None:
        return False
    return spPr.find(f"{_A}solidFill") is not None


def _bbox_intersection_ratio(inner: tuple, outer: tuple) -> float:
    """`inner` 矩形が `outer` 矩形とどれだけ重なるか（交差面積 ÷ inner 面積・0.0-1.0）。"""
    ix0, iy0, ix1, iy1 = inner
    ox0, oy0, ox1, oy1 = outer
    inner_area = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    if inner_area <= 0:
        return 0.0
    dx = min(ix1, ox1) - max(ix0, ox0)
    dy = min(iy1, oy1) - max(iy0, oy0)
    if dx <= 0 or dy <= 0:
        return 0.0
    return (dx * dy) / inner_area


def _pptx_slide_texts_by_shape(root, figures: dict[int, list[list[str]]] | None = None) -> list[str]:
    """`p:cSld/p:spTree` 直下を文書順（=z順・背面→前面）で歩き、shape 単位でテキストを取る。

    各テキスト shape について、後（前面）にある occluder 候補（無地塗りの空 shape／画像）と bbox の交差比が閾値以上なら
    「隠し候補」とマークし、直前に独立行のマーカーを出す。`p:grpSp`（グループ）内は幾何判定せず、テキストのみ再帰抽出する。
    """
    cSld = root.find(f"{_PR}cSld")
    if cSld is None:
        raise ValueError("p:cSld が見つからない")               # フォールバックへ
    spTree = cSld.find(f"{_PR}spTree")
    if spTree is None:
        raise ValueError("p:spTree が見つからない")

    # 文書順の子要素を分類: テキストshape候補（p:sp）／occluder候補（p:sp 無地塗り or p:pic）／グループ
    children = list(spTree)
    entries = []                                                # [{kind, sp, texts, bbox, occluder}]
    for el in children:
        tag = el.tag
        if tag == f"{_PR}sp":
            texts = _pptx_shape_texts_list(el)
            bbox = _pptx_bbox(el)
            has_text = bool(texts)
            is_occluder = (not has_text) and _pptx_has_solid_fill(el) and bbox is not None
            entries.append({"kind": "sp", "texts": texts, "bbox": bbox,
                             "occluder": is_occluder, "has_text": has_text})
        elif tag == f"{_PR}pic":
            bbox = _pptx_bbox(el)
            entries.append({"kind": "pic", "texts": [], "bbox": bbox,
                             "occluder": bbox is not None, "has_text": False})
        elif tag == f"{_PR}grpSp":
            group_texts = _pptx_group_texts(el)
            entries.append({"kind": "grpSp", "texts": group_texts, "bbox": None,
                             "occluder": False, "has_text": bool(group_texts)})
        elif tag == f"{_PR}graphicFrame":
            texts = [t.text.strip() for t in el.iter(f"{_A}t") if t.text and t.text.strip()]
            entries.append({"kind": "graphicFrame", "texts": texts, "bbox": None,
                             "occluder": False, "has_text": bool(texts)})
        else:                                                   # p:cxnSp／未知要素等: テキストのみ拾う
            texts = [t.text.strip() for t in el.iter(f"{_A}t") if t.text and t.text.strip()]
            entries.append({"kind": "other", "texts": texts, "bbox": None,
                             "occluder": False, "has_text": bool(texts)})

    out: list[str] = []
    n = len(entries)
    for i, entry in enumerate(entries):
        figure_blocks = [_figure_text_line(lines) for lines in (figures or {}).get(i, [])]
        if not entry["has_text"]:
            out.extend(figure_blocks)                            # 図（pic・graphicFrame）の直後に出す
            continue
        hidden = False
        if entry["kind"] == "sp" and entry["bbox"] is not None:
            for j in range(i + 1, n):                            # 後続（前面）の occluder 候補のみ
                other = entries[j]
                if not other["occluder"] or other["bbox"] is None:
                    continue
                if _bbox_intersection_ratio(entry["bbox"], other["bbox"]) >= _OCCLUSION_RATIO:
                    hidden = True
                    break
        if hidden:
            out.append(_HIDDEN_MARKER)
        out.extend(entry["texts"])
        out.extend(figure_blocks)
    return out


def _pptx_group_texts(grp) -> list[str]:
    """`p:grpSp` 内のテキストのみ再帰抽出（幾何判定は行わない＝グループ内 shape は occluder 候補にも
    被occlude対象にもしない）。ネストした `p:grpSp` も辿る。"""
    out: list[str] = []
    for el in grp:
        tag = el.tag
        if tag in (f"{_PR}sp", f"{_PR}pic", f"{_PR}graphicFrame"):
            out.extend([t.text.strip() for t in el.iter(f"{_A}t") if t.text and t.text.strip()])
        elif tag == f"{_PR}grpSp":
            out.extend(_pptx_group_texts(el))
        else:
            out.extend([t.text.strip() for t in el.iter(f"{_A}t") if t.text and t.text.strip()])
    return out


# ---- .xlsx（openpyxl・値ベース）----

def _xlsx_md(p: Path) -> str | None:
    """XLSX の人間向け MD。document-ir（`arms/ooxml_arm._build_xlsx_ir`）を土台に `human_md.render_xlsx` へ委譲し、
    `ooxml/excel.py::regions()` が検出する表候補ごとに見出し＋パイプ表を出す。IR 構築に失敗すれば未対応（None）。
    """
    from .arms import ooxml_arm
    ir = ooxml_arm._build_xlsx_ir(p)
    return _render_human_md(ir, p, ".xlsx") if ir is not None else None


# ---- .pdf（テキスト層・バックエンド：pypdf＝同梱既定 / pdfminer.six＝任意）----

def _normalize_pdf_text(txt: str) -> str:
    """PDF 抽出テキストを決定的に整形（改行正規化・行末空白除去・連続空行を1つに）。OCR はしない。"""
    lines = (txt or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, blank = [], 0
    for ln in lines:
        ln = ln.rstrip()
        if ln.strip():
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1:                                   # 連続空行は1つに畳む（順序安定）
                out.append("")
    return "\n".join(out).strip()


def _pdf_pages(p: Path) -> list[str]:
    """PDF をページ毎テキストのリストに（バックエンド差を吸収）。バックエンド無/失敗は []。"""
    backend = _pdf_backend()
    if backend == "pypdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        if getattr(reader, "is_encrypted", False):
            try:
                if not reader.decrypt(""):                   # 空パスワードで開けない＝復号不可→未対応([])（破らない）
                    return []
            except Exception:
                return []
        return [(pg.extract_text() or "") for pg in reader.pages]
    if backend == "pdfminer":
        from pdfminer.high_level import extract_text
        return (extract_text(str(p)) or "").split("\f")      # pdfminer は改ページを \f で区切る
    return []


def _pdf_is_encrypted(p: Path) -> bool:
    """PDF が暗号化されているか（空パスワードで復号できるかは問わない・`pypdf` で判定）。

    `_pdf_pages` は暗号化 PDF も `[]` に丸めるため、一般失敗分岐が理由を失う。
    ここは判定専用で、バックエンドが `pdfminer` や未導入の場合は判定不能＝False。
    """
    if _pdf_backend() != "pypdf":
        return False
    try:
        from pypdf import PdfReader
        return bool(getattr(PdfReader(str(p)), "is_encrypted", False))
    except Exception:
        return False


def _pdf_md(p: Path) -> str | None:
    """PDF のテキスト層 → 決定的 Markdown（`## ページ N` 見出し＋本文）。

    本文ゼロ（スキャン画像＝OCR要 / 暗号化 / 非テキスト）は None＝「未対応(変換失敗)」として可視化。
    """
    pages = _pdf_pages(p)
    out = []
    for i, raw in enumerate(pages, 1):
        body = _normalize_pdf_text(raw)
        if body:
            out.append(f"## ページ {i}\n\n{body}")
    return "\n\n".join(out) if out else None


# ---- PDF ティア制＝テキスト層の品質で担当アームを決定的に選ぶ ----
_PDF_TEXT_MIN_CHARS = 30  # PDF テキスト層「十分」の判定しきい値（ページあたり平均抽出文字数）。診断用の good/sparse 分類に使い、テキストが存在する PDF は sparse でも決定的な pdf_text を継続する。0 以下は常に good


# 品質判定のメモ化: `accepts()` は pdf_text/vision の複数アームから同一 PDF に繰り返し呼ばれるため、
# `(resolved path,size,mtime_ns)` キーでプロセス内メモ化し、1 ファイル 1 回の抽出に抑える。上限超過は全消し。
_PDF_QUALITY_CACHE: dict[tuple[str, int, int], tuple[str, float]] = {}
_PDF_QUALITY_CACHE_MAX = 256


def _pdf_quality_cache_key(p: Path) -> tuple[str, int, int] | None:
    """`(resolved path, size, mtime_ns)` キー。stat 不可（存在しない/権限無し等）は None＝キャッシュせず都度計算。"""
    try:
        rp = p.resolve()
        st = rp.stat()
        return (str(rp), st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _pdf_quality_and_avg(p) -> tuple[str, float]:
    """PDF テキスト層の品質（good/sparse/empty）とページ平均抽出文字数を計算する（メモ化）。

    `_pdf_pages` の例外は握り、失敗時は `("empty", 0.0)` を返す（1 件の壊れた PDF が `build_derived` の 1 ファイルループ全体を止めないため）。
    """
    path = Path(p)
    key = _pdf_quality_cache_key(path)
    if key is not None and key in _PDF_QUALITY_CACHE:
        return _PDF_QUALITY_CACHE[key]
    try:
        pages = _pdf_pages(path)
        texts = [_normalize_pdf_text(raw) for raw in pages]
        total = sum(len(t) for t in texts)
    except Exception:
        result = ("empty", 0.0)                              # 抽出失敗＝テキスト層ゼロと同様に扱う（fail-safe）
    else:
        if total == 0:
            result = ("empty", 0.0)
        else:
            avg = round(total / (len(texts) or 1), 1)
            min_chars = _PDF_TEXT_MIN_CHARS
            quality = "sparse" if (min_chars > 0 and avg < min_chars) else "good"
            result = (quality, avg)
    if key is not None:
        if len(_PDF_QUALITY_CACHE) >= _PDF_QUALITY_CACHE_MAX:
            _PDF_QUALITY_CACHE.clear()                        # 上限超過は全消し
        _PDF_QUALITY_CACHE[key] = result
    return result


def pdf_text_quality(p) -> str:
    """PDF テキスト層の品質を決定的に判定: "good"（十分）/"sparse"（少ない）/"empty"（テキスト層ゼロ）。

    全ページ抽出→整形後の総文字数とページ平均で分類する（`_pdf_quality_and_avg` でメモ化）。
    バックエンド未導入・抽出例外は "empty" 扱い（`pdf_escalation_target` が上位アームへ委ねる）。
    """
    return _pdf_quality_and_avg(p)[0]


def pdf_escalation_target(p) -> str | None:
    """テキスト層ゼロの PDF を vision に回すべきか。回すなら `"vision"`、それ以外は None。

    ティア判定をここに集約し、各 PDF アームの `accepts` がこの結果を参照する（pdf_text.accepts → target is None、vision.accepts → target == "vision"）。
    - good → None（pdf_text 続投）
    - sparse → None（存在するテキスト層を決定的に抽出し、AI 解釈を足さない）
    - empty → vision が実効利用可能なら `"vision"`、無ければ None
    """
    if Path(p).suffix.lower() != ".pdf":
        return None
    from . import arms as _arms
    names = set(_arms.enabled_arm_names())
    if not _vision_pdf_ready(names):
        return None                                          # vision無し＝pdf_text（有効なら）が担当
    return "vision" if pdf_text_quality(p) == "empty" else None
