"""Office（OOXML）→ 決定的 Markdown 変換（鏡モデルの派生MD・正典 docs/11-Office変換.md §D5）。

**OOXML を直接パース**（Office/LibreOffice 非依存・Linux-native）。MVP は最小形＝本文/見出し/表（値）。
- .docx: `word/document.xml` を document-ir 経由で読む（段落＋見出し＋結合セル/ネスト表展開済みの表・
  `human_md.render_docx`）。外部ライブラリ不要。
- .pptx: `ppt/slides/slideN.xml` のテキストを slide 順に。外部ライブラリ不要。
- .xlsx: openpyxl（導入済）で document-ir 経由・シート内の表候補（`regions()`）ごとに値の表
  （`human_md.render_xlsx`）。
- .pdf: テキスト層を抽出（バックエンド pypdf・requirements.txt に同梱既定・2026-07-08）。到達不可なら None＝「未対応」。
- .doc/.xls/.ppt（旧バイナリ）: `to_markdown` は直接は扱わない（None）。build_derived が legacy_backend（W0・
  LibreOffice 等）で先に OOXML へ前段変換し、MD 化は①OOXML へ委譲する（`arms/legacy_convert.py`）。

決定的（タイムスタンプ等を出さない・順序安定）。壊れたファイル/非OOXML は例外を握って None。
LLM は使わない（INGEST-MD: 大半は決定的MD化で LLM 不要）。
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

# LOG-2（2026-09-03）: MD 変換（取り込み進行ログ）は専用ログ（sherpa.ingest.convert）へまとめる
# （`sherpa/log_setup.py` の登録表参照・worker.py と合流させ1系統にする）。
_log = logging.getLogger("sherpa.ingest.convert")

# OOXML 名前空間
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

CONVERTIBLE_EXT = {".docx", ".xlsx", ".pptx"}                 # OOXML＝常時 MD化（外部ライブラリ不要）
RASTER_EVIDENCE_EXT = frozenset({".png", ".jpg", ".jpeg"})    # OCRなしで存在・位置・hashをEvidence化
LEGACY_OFFICE_EXT = frozenset({".doc", ".xls", ".ppt"})
EVIDENCE_EXT = frozenset({".xlsx", ".docx", ".pptx", ".pdf"}) | RASTER_EVIDENCE_EXT | LEGACY_OFFICE_EXT
PDF_EXT = {".pdf"}                                            # PDF はテキスト層を抽出（同梱既定バックエンド pypdf）
# ラスタ画像（視覚読み取りアーム `vision`＝VLM の対象・tesseract の `ocr` アームは撤去済 2026-07-08）。
# **vision 有効 かつ VLM 実効可時のみ** MD化候補になる（既定 ooxml,pdf_text では画像は従来どおり
# grep 専用の素ファイル＝挙動不変）。
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}
OFFICE_EXT = CONVERTIBLE_EXT | PDF_EXT | set(LEGACY_OFFICE_EXT)   # Office/PDF 一括（未対応含む）


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析（`grep_tool._env_int` と同型・独立実装）。"""
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


# Office/PDF 取り込みの入口サイズガード（MEM-1・2026-09-03・実環境 OOM 障害対応）。openpyxl は
# read_only=False で全ブックをオブジェクトツリーとして展開するため、原本サイズに対しピークメモリが
# 数倍〜数十倍に達しうる（セル書式・共有文字列・結合セル等の付帯構造が展開されるため）。この上限は
# 「変換を試みる前に諦める」安全弁であり、変換アルゴリズム自体のメモリ効率とは独立（項目2参照）。
_OFFICE_FILE_CAP_BYTES = _env_int(
    "SHERPA_OFFICE_FILE_CAP_BYTES", 100 * 1024 * 1024, 1024 * 1024, 1024 * 1024 * 1024)
_PDF_FILE_CAP_BYTES = _env_int(
    "SHERPA_PDF_FILE_CAP_BYTES", 100 * 1024 * 1024, 1024 * 1024, 1024 * 1024 * 1024)


def _office_size_exceeded(rp: Path, ext: str) -> bool:
    """変換前の入口サイズ超過判定（Office 系＝`.docx/.xlsx/.pptx/.doc/.xls/.ppt`・PDF は別 cap）。

    stat 失敗（消失・権限・race）はサイズ超過として扱わない（`corpus_docs._text_oversize` と同じ
    fail-open＝実読込の失敗は別経路が拾う）。
    """
    cap = _PDF_FILE_CAP_BYTES if ext == ".pdf" else _OFFICE_FILE_CAP_BYTES
    try:
        return rp.stat().st_size > cap
    except OSError:
        return False


# xlsx セル数ガード（MEM-2・2026-09-04・閉域実機の実測対応）: `SHERPA_OFFICE_FILE_CAP_BYTES` は
# 原本の圧縮後（zip）サイズを見るため、xlsx は圧縮率が高く（共有文字列/繰り返し値が多い）実測
# 13MB のファイルが st_size ガードを素通りし、python RSS 6.6GB・20分超に達した。この上限は
# openpyxl を一切開かない段階で効かせる安全弁（1セル≈数百B〜数KBのオブジェクト化で数百万セル→
# 数GB に達する実測ベース）。
_XLSX_CELL_CAP = _env_int("SHERPA_XLSX_CELL_CAP", 2_000_000, 1_000, 100_000_000)

# Office 非圧縮サイズガード（MEM-2）: docx の巨大 document.xml・pptx の大量スライド等、xlsx 以外にも
# 同型の圧縮爆弾リスクがある。zip の非圧縮サイズ合計（セントラルディレクトリの `ZipInfo.file_size`
# の和のみ・シート/スライド本体は読まない＝安価）に対する上限。素の docx/xlsx/pptx と、旧形式
# （.doc/.xls/.ppt）を①OOXML へ前段変換した後の materialized ファイルの両方に適用する。
_OFFICE_UNCOMPRESSED_CAP_BYTES = _env_int(
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
    """openpyxl でフルロードする前に、zip 内 `xl/worksheets/sheetN.xml` の `<dimension ref="..."/>`
    だけをストリーミングで読み、全シートのセル数（面積）の和を見積もる（MEM-2）。

    シート XML 全体は読まない（先頭 `_XLSX_DIMENSION_SCAN_BYTES` バイトだけ）。dimension が
    欠落/不正な原本が1つでもあれば全体を「見積不能」として None を返す＝fail-open（ガード不可・
    通常の変換経路へそのまま流す）。壊れた zip も同様に None。
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


def _office_uncompressed_total_bytes(rp: Path) -> int | None:
    """zip セントラルディレクトリのみを読み、全エントリの非圧縮サイズ（`ZipInfo.file_size`）の和を
    返す（MEM-2）。エントリ本体（シート/スライド XML 自体）は読まない＝安価。壊れた zip・stat 不能
    は見積不能として None（fail-open）。
    """
    try:
        with zipfile.ZipFile(rp) as zf:
            return sum(info.file_size for info in zf.infolist())
    except (OSError, zipfile.BadZipFile):
        return None


# 抽出不完全の疑い（ING-1・静かな部分抽出検知）: 原本サイズに対し生成MDが極端に小さければ疑いに
# 計上する（docx/pdf 等にも効く粗い網・深いカバレッジ計算はしない）。1MiB 超という下限は「小さい
# 原本はMDも小さくて正常」を除外するための保守的な閾値。
_PARTIAL_SIZE_MIN_SOURCE_BYTES = 1024 * 1024
_PARTIAL_SIZE_MAX_MD_BYTES = 512


def _pdf_backend() -> str | None:
    """利用可能な PDF テキスト抽出バックエンド名（優先順 pypdf > pdfminer.six）。無ければ None。

    `pypdf` は `requirements.txt` に**同梱既定**（2026-07-08）＝箱出しで PDF が読める。pdfminer.six は
    任意の追加バックエンド。PyMuPDF は製品経路に採用しない。いずれも到達不可なら PDF は
    「未対応」と正直表示（fail-safe）。テキスト層のみ＝スキャン画像（視覚読み取りは vision）は対象外。
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
    """今 MD化できる拡張子。**有効アーム（`SHERPA_ARMS`）**が担当し、かつ現時点で変換可能な拡張子の和集合。

    既定（`ooxml,pdf_text`）では従来と同一＝OOXML は常時、PDF はバックエンド導入時のみ（無ければ未対応扱い）。
    任意の vision アームが有効なら、その受理拡張子もここに合流させる（アーム名→拡張子の対応は本関数が集約）。

    旧形式（.doc/.xls/.ppt）は legacy_backend（W0・LibreOffice 等）が旧→新へ前段変換し MD 化は①OOXML に委譲する
    ため、**ooxml 有効 かつ 変換バックエンド到達可**（`legacy_convert.legacy_exts()` が非空）のときだけ合流させる。

    Office（.docx/.xlsx/.pptx）は① OOXML 有効時だけ対象にする。PDF は `pdf_text`（テキスト層）または
    `vision`（テキスト層ゼロのPDFをVLMで視覚読み取り）のいずれかが到達可なら変換候補（担当は
    `pdf_escalation_target` が決める）。ラスタ画像は **vision（VLM 実効可）のときのみ**（`_image_convertible` と一致）
    ＝それ以外は従来どおり未対応（正直表示）。tesseract 直の `ocr` アームは撤去済み（2026-07-08）。
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
    """ラスタ画像を今 MD化できるか（vision＝VLM 実効可のときのみ・tesseract の `ocr` は撤去済）。"""
    return _vision_image_ready(names)


def _pdf_escalation_available(names) -> bool:
    """vision がPDFの視覚読み取りに到達可能か（PDFを変換候補にできるか）。"""
    return _vision_pdf_ready(names)


# ---- アーム構成 drift（旧 `.pdf_backend` を一般化した `.arms_sig` マーカー）----
# 派生MD を作った時の「有効アーム構成の署名」を派生 dir に残し、構成が変わったら署名同一でも作り直す。
_ARMS_SIG_MARKER = ".arms_sig"                               # 派生MD を作った時の有効アーム構成を記録
_OLD_PDF_MARKER = ".pdf_backend"                             # 旧マーカー（PDF バックエンド名のみ・後方互換の読み替え用）


def _arms_sig(arm_names, backend: str | None, legacy: str | None = None,
              vlm: str | None = None) -> str:
    """有効アーム構成の決定的署名（順序安定）: 有効アーム名（ソート）＋ PDF バックエンド＋ legacy 変換
    バックエンド（W0）＋ VLM（vision）の**実効可用性**。

    `vlm` は「アーム有効 かつ この環境で実際に使える」ときだけ非 "none"（未導入→後付け導入
    や設定変更で署名が変わり drift 再ビルドが誘発される・soffice の `legacy_sig_value` と同じ扱い）。`vlm` は
    provider/model／**クラウド許可（cloud_allowed）の切替でも変わる**（ローカル⇔クラウドで抽出源が変わるため
    ＝`vision_arm.sig_value`）。エンジンのバージョンは含めない（版更新での不要な全リビルドを避ける・
    provenance には残す）。既定値付き引数なので、後方互換の再構成呼び出し（旧 `.pdf_backend`/marker皆無の
    A2/A3 前基準）は自動的に vlm=none になる。

    **document-ir（DOC-IR-001）のスキーマ/抽出器版はここに含めない**（DOC-IR-001.5・修正3・契約修正 High#3）:
    `es_index._arms_config_sig()`（es_index.py 内）が本関数（`_current_arms_sig()`）をそのまま ES の
    `needs_reindex` drift 判定に使うため、IR 版をここに混ぜると IR 実装の版更新だけで**全 world の ES 再索引**
    まで誘発してしまう（この `needs_reindex` 経路に限っては IR は無関係でよい）。IR 版の drift は独立した
    マーカー（`_DOCUMENT_IR_SIG_MARKER`／`document_ir_sig_drift`）で判定し、対象を document_ir だけに
    限定せず対象 OOXML 文書を全件再生成する`refresh_document_ir`が担う——ただし IR 単独では完結せず、
    `worker._refresh_derived_representations` が IR 再生成後に evidence/rag（さらに RAG_ES 有効時は
    ES 索引）まで連鎖再生成する契約（`worker.sync` 参照）。

    **`;md=` と `;ocr=` 成分は撤去した**（MarkItDown不採用およびtesseractの `ocr` アーム撤去に伴う署名変更）。
    既存の派生 dir が持つ旧マーカー（`;md=...` / `;ocr=...` を含む形式）は新フォーマットと文字列が一致しなくなる
    ため、次回 sync で `arms_sig_drift` が True になり**派生の再ビルドが1回だけ**走る（想定内・正しい挙動）。
    """
    return ("arms=" + ",".join(sorted(arm_names))
            + ";pdf=" + (backend or "none")
            + ";legacy=" + (legacy or "none")
            + ";vlm=" + (vlm or "none"))


def _current_arms_sig() -> str:
    """**今**の有効アーム構成（`SHERPA_ARMS`）＋各アームの実効可用性（PDF バックエンド／legacy／VLM）
    の署名。document-ir 版は含まない（`_arms_sig` docstring 参照）。"""
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
    """派生MD を作った時と**今**でアーム構成（有効アーム＋PDF バックエンド）が変わったか。

    True なら sync が署名同一でも派生を作り直すべき（アーム後付け導入で PDF/新形式を検索対象化／除去後の
    残骸を一掃・RV High）。旧 `.pdf_backend` マーカーしか無い派生 dir は「既定アーム構成で書かれたもの」と
    **読み替えて**比較する（後方互換）。マーカー皆無は既定アーム＋バックエンド無しを基準に判定する
    （旧 `pdf_backend_drift` の recorded="none" 相当・marker 書込失敗時の無限ループを避ける）。

    RV Med（Codex gpt-5.5/xhigh・2026-07-08 R1）: `.arms_sig` marker が**在る**が今の署名と不一致
    （例: 旧 `;ocr=...` 付きフォーマットの marker が本撤去の署名フォーマット変更で不一致になるケース）でも、
    dir が**書けない**場合は書込不能 probe（`_dir_writable`）で据え置く（no-marker ケースと同じ fail-safe）。
    これが無いと、再ビルドで marker を更新できない dir は毎 sync で drift=True を返し続けフルリビルドの
    ループになる（`build_derived` が rebuild しても `_write_arms_sig_marker` が書けず署名不一致のまま）。
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
    old = d / _OLD_PDF_MARKER
    if old.is_file():                                        # 旧マーカーのみ＝既定アーム構成で書かれたと読み替え
        try:
            backend = old.read_text(encoding="utf-8").strip() or "none"
        except OSError:
            return True
        return _arms_sig(_arms.DEFAULT_ARMS, backend, "none") != cur   # 旧マーカーは W0 前＝legacy=none 基準
    # マーカー皆無＝既定構成・バックエンド無しを基準に判定（旧 pdf_backend_drift の recorded="none" 相当）。
    # RV Med（Codex 2026-07-08）: marker が**書けない** dir で True を返し続けると、再ビルドしても marker を
    # 永続できず毎 sync フルリビルドのループになる（旧実装から潜在）。書込可否を probe し、書けない dir では
    # 警告して据え置く（fail-safe＝dir を直せば次の sync で必要な再ビルドが走る）。
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


# ---- document-ir 版 drift（DOC-IR-001.5・修正3・契約修正 High#3）----
# `.arms_sig`（MD 派生の構成署名）とは**別マーカー**にする（`_arms_sig` docstring の根拠参照＝es_index の
# needs_reindex drift 判定を誤発火させ全 world の ES 再索引を誘発しないため）。IR 版の更新は
# `document_ir_sig_drift`→`refresh_document_ir`の軽量経路（MD自体の全再ビルドはしない）で解決するが、
# `worker.sync`（`_refresh_derived_representations`）がこれに続けて evidence/rag（さらに RAG_ES
# 有効時は ES 索引）まで連鎖再生成する契約になっている＝IR 単独で完結する更新ではない。
_DOCUMENT_IR_SIG_MARKER = ".document_ir_sig"


def _current_document_ir_sig() -> str:
    """**今**の document-ir 版（JSON 形式のスキーマ版＋DOCX/PPTX/XLSX 各抽出処理の版）の署名。

    docx/pptx/xlsx を別々の成分（`docx=`/`pptx=`/`xlsx=`）として持つが、drift 判定
    （`document_ir_sig_drift`）はこの署名文字列**全体**を1つのマーカーとして比較する（`.document_ir_sig`
    は world 単位で1つしか持たない）。**どれか1つの抽出器だけを拡張しても、対象拡張子だけに
    絞り込まれるわけではない**——`_refresh_derived_representations` の `refresh_document_ir` は
    drift を検知したら world 内の全 OOXML 文書（docx/pptx/xlsx すべて）を対象に再生成する
    （`worker.py::_refresh_derived_representations` docstring 参照）。
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
    """`.document_ir_sig` を現行値で確定する公開ヘルパ（`write_rag_sig_marker` と同型）。

    `refresh_document_ir` を `write_document_ir_sig_marker=False` で呼んだ場合、確定は
    連鎖した evidence/rag（さらに RAG_ES 有効時は ES 反映）の成否を確認できる呼び出し元
    （`worker`）へ委ねられる（マーカー保留方式）。呼び出し元は document_ir 自体の生成成功に
    加え、連鎖先の成功も確認できた時だけこれを呼ぶ。
    """
    _write_document_ir_sig_marker(Path(derived_md_dir))


def document_ir_sig_drift(derived_md_dir) -> bool:
    """派生を作った時と**今**で document-ir 版が変わったか（マーカー無し/読めない/不一致で True）。

    `arms_sig_drift` と同型（マーカー方式・無し/不一致は drift）だが、書込不能 dir の据え置き probe
    （`_dir_writable`）は行わない: `arms_sig_drift` がそれを持つのは drift=True が**派生MD 全再ビルド**を
    誘発し、書けない dir で毎 sync ループするコストが高いため。一方こちらの drift は `refresh_document_ir`
    （対象 OOXML 文書を全件再生成し、`worker.sync` 経由で evidence/rag へも連鎖する軽量経路）しか
    誘発せず、書けない dir で据え置かない場合の実害（無駄な再走査）は小さい＝単純さを優先する。
    """
    sig_path = Path(derived_md_dir) / _DOCUMENT_IR_SIG_MARKER
    if not sig_path.is_file():
        return True
    try:
        return sig_path.read_text(encoding="utf-8").strip() != _current_document_ir_sig()
    except OSError:
        return True


# ---- 人間向け MD（human_md）版 drift（H2・単一 asset 版・正典 §10 裁定#1〜#4）----
# `.document_ir_sig`（world 単位の1マーカーファイル）とは異なる設計にする: `{rel}.md` の版は
# `_write_derived_sidecar_manifest` が rel ごとに `{rel}.derived.json` の `asset_versions.human_md` へ
# 書く（RAG-KV の drift 連鎖と同じ「単一 asset だけの選択的再生成」）。world 単位のマーカーにすると
# 1文書だけ再生成すればよい場面でも他の全 docx/xlsx を巻き込む全再構築を誘発してしまう。


def _current_human_md_sig() -> str:
    """**今**の人間向け MD（`human_md`）の版。レンダラ自体の版に加え、レンダラが消費する
    docx/xlsx 抽出器の版（`DOCX_EXTRACTOR_VERSION`/`XLSX_EXTRACTOR_VERSION`）も含める
    （`_current_document_ir_sig` と同じ理由＝抽出器が変われば document-ir 経由の human_md 出力も
    変わりうるため）。
    """
    from . import human_md
    from .arms import ooxml_arm
    return (f"renderer={human_md.HUMAN_MD_RENDERER_VERSION};"
            f"docx={ooxml_arm.DOCX_EXTRACTOR_VERSION};xlsx={ooxml_arm.XLSX_EXTRACTOR_VERSION}")


def human_md_sig_drift(wd, derived) -> bool:
    """素の docx/xlsx のうち、`{rel}.md`（human_md 生成）の版が現在の `_current_human_md_sig()` と
    食い違う rel が1件でもあれば True。

    **`.md` sidecar の有無では絞り込まない**: 空の xlsx 等は `.md` を持たない
    （`human_md.render_xlsx`／`render_docx` が None を返した）のが正当な結果でありうるが、それでも
    `asset_versions.human_md` を評価対象にしないと、レンダラ版が上がった時にその rel だけが
    永久に「未評価」のまま drift 検知から漏れる（`refresh_human_md`／`_write_derived_sidecar_manifest`
    docstring 参照）。マニフェスト自体が読めない rel は「未評価」と同じ扱い（drift あり）にする
    （`rag_sidecars_missing` 側が別途この rel を全再構築の対象にできる）。legacy `.doc`/`.xls`
    （前段変換経由）は対象外（`refresh_human_md` docstring のスコープ限定参照）。

    **有効アーム（`SHERPA_ARMS`）に従う**: docx/xlsx を担当する `ooxml` アームが無効化されている
    間はこの関数の対象から完全に外す（drift 評価そのものをしない）。`ooxml` 無効時は
    `convertible_exts()` がこれらの拡張子を「未対応」のまま扱う契約であり、`.md` sidecar の
    有無に依存しない今の判定方式のままだと、無効アームでも人間向け MD を新規生成してしまう
    迂回になる。
    """
    from . import arms as _arms
    from .. import scope_infer as si
    if "ooxml" not in _arms.enabled_arm_names():
        return False
    wd = Path(wd).resolve()
    dr = Path(derived)
    dr_ir = _sibling_layer_dir(dr, "ir")          # `.derived.json` マニフェストは ir 層（§8.1 三階層）
    current = _current_human_md_sig()
    for rp, rel in si.safe_files(wd):
        if rp.suffix.lower() not in (".docx", ".xlsx"):
            continue
        manifest = json_io.read_json(dr_ir / (rel + _DERIVED_MANIFEST_SUFFIX), default=None)
        versions = manifest.get("asset_versions") if isinstance(manifest, dict) else None
        recorded = versions.get("human_md") if isinstance(versions, dict) else None
        if recorded != current:
            return True
    return False


def refresh_human_md(wd, derived) -> dict:
    """人間向け `{rel}.md` **だけ**の軽量再生成（H2・単一 asset・RAG-KV の drift 連鎖と
    同じ考え方）。`human_md_sig_drift` が対象とする条件と同じ rel だけを選び、document-ir を作り
    直して `human_md.render_docx`/`render_xlsx` へ渡し、`{rel}.md` だけを書き換える。
    `.document.json`/`.evidence.json`/`.rag.md`/`.rag_chunks.jsonl`・ES 索引には一切触れない
    （`.rag_sig`/`.document_ir_sig` も変更しない＝RAG_ES 有効時でも ES を無駄に再索引させない）。

    legacy `{rel}.md` の中身は ES の40行チャンク索引元になりうる（`docs/proposals/2026-08-28-
    人間向けMDの刷新.md` §5/§6）——RAG_ES OFF の world では常に、RAG_ES ON の world でも
    `rag_chunks` が無効/劣化した文書は legacy 縮退でこの経路を使うため、RAG_ES の設定だけでは
    「ここでの `{rel}.md` 更新が ES に無関係」と言い切れない。ES 追随は `es_index.needs_reindex`
    側が `asset_versions.human_md`/`.human_md_es_sig` マーカーを見て判断する（`es_index.py`
    `_human_md_config_sig` 参照）。

    スコープ限定（既知の制約）: 素の `.docx`/`.xlsx` だけを対象にする。legacy `.doc`/`.xls`
    （前段変換経由で human_md 生成される rel）はここでは対象にしない——前段変換の再実行
    （`legacy_convert`）を伴うため単一 asset の軽量再生成という設計から外れる。legacy 系は次回の
    通常 sync/run（原本変化等）が拾う（据え置きの間、レンダラ版が変わっても legacy 系の
    `{rel}.md` は追随が遅れうる＝受容）。

    **`.md` sidecar の有無では絞り込まない**: `human_md_sig_drift` と同じ理由で、空の
    xlsx/docx（IR は構築できるが本文が無く `.md` を書かない rel）も評価対象にする——IR が構築でき
    「今回の版で本文が無いことを確認できた」場合は失敗ではなく、`asset_versions.human_md` を現行版で
    確定させて次回の再評価ループを止める（`.md` は書かない＝存在しない状態を維持する）。IR 構築
    そのものが失敗した場合だけを `human_md_failed` へ計上する（fail-closed・次回 sync で再試行）。

    失敗した rel は `human_md_failed`/`human_md_failures` へ計上し、マニフェストの
    `asset_versions.human_md` を更新しない（次回 sync が再試行する＝fail-safe）。

    **有効アーム（`SHERPA_ARMS`）に従う**: `human_md_sig_drift` と同じ理由で、`ooxml` アームが
    無効化されている間は何もしない（対象拡張子を「未対応」のまま維持する）。
    """
    from . import arms as _arms
    from .. import scope_infer as si
    from . import human_md
    from .arms import ooxml_arm

    if "ooxml" not in _arms.enabled_arm_names():
        return {"human_md_generated": 0, "human_md_failed": 0, "human_md_failures": []}
    wd = Path(wd).resolve()
    dr = Path(derived)
    dr_rag = _sibling_layer_dir(dr, "rag")
    dr_ir = _sibling_layer_dir(dr, "ir")          # `.derived.json` マニフェストは ir 層（§8.1 三階層）
    current = _current_human_md_sig()
    generated = failed = 0
    failures: list[dict] = []
    for rp, rel in si.safe_files(wd):
        ext = rp.suffix.lower()
        if ext not in (".docx", ".xlsx"):
            continue
        manifest = json_io.read_json(dr_ir / (rel + _DERIVED_MANIFEST_SUFFIX), default=None)
        versions = manifest.get("asset_versions") if isinstance(manifest, dict) else None
        recorded = versions.get("human_md") if isinstance(versions, dict) else None
        if recorded == current:
            continue
        try:
            ir = ooxml_arm._build_docx_ir(rp) if ext == ".docx" else ooxml_arm._build_xlsx_ir(rp)
        except Exception as e:
            failed += 1
            failures.append({"doc": rel, "reason": f"ir_build_failed:{e.__class__.__name__}"})
            continue
        if ir is None:
            failed += 1
            failures.append({"doc": rel, "reason": "ir_build_failed"})
            continue
        try:
            md = human_md.render_docx(ir) if ext == ".docx" else human_md.render_xlsx(ir)
            if md is not None:
                json_io.write_text_atomic(dr / (rel + ".md"), md)
            # md is None（docx のみ実質発生・xlsx は human_md.render_xlsx が常に非 None を返す）は
            # 失敗ではなく「今回の版で確認できた正当な空」＝.md は書かず manifest だけ確定させる。
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


# ---- human_md の ES 反映 drift（世界単位・ホールドバック方式・`.rag_sig` と同型）----
# `asset_versions.human_md`（rel 単位・render 済みかどうか）とは別物: こちらは「ES がこの版まで
# 実際に索引反映できたか」を表す。`es_index.index_world` はクリーン再索引（delete→create→bulk）で、
# 索引作成（`ensure_index`）は bulk の成否が判明する前に行われるため、bulk が部分失敗しても index
# 自体は作られてしまう——bulk の成否を確認できる呼び出し元（`worker`）からしか確定できない。
_HUMAN_MD_ES_SIG_MARKER = ".human_md_es_sig"


def _write_human_md_es_sig_marker(dr: Path) -> bool:
    try:
        (dr / _HUMAN_MD_ES_SIG_MARKER).write_text(_current_human_md_sig(), encoding="utf-8")
        return True
    except OSError:
        _log.warning(
            "`.human_md_es_sig` マーカーの書込に失敗しました（次回 sync も pending のまま再試行）: %s", dr)
        return False


def confirm_human_md_es_sig(wd, derived) -> bool:
    """`.human_md_es_sig` を現行値で確定する（ES が bulk 成功でこの版まで追随できたと記録する）。

    呼び出し元（`worker`）は `es_index.index_world()` の戻り値を検査し、失敗（`error` キー）が
    無かった時だけこれを呼ぶ。ただし bulk 自体が成功していても、render 側（`human_md_sig_drift`）が
    まだ現行版に追随できていない rel が残っている間は確定しない——その場合の bulk は古い
    `{rel}.md` を含んだまま索引した可能性があり、「ES が現行版に追いついた」と言い切れないため。
    確定できた（True）か、render 側の drift が残っていて確定を見送った・マーカーの書込自体が
    失敗した（いずれも False）かを返す。呼び出し元は False の場合、失敗として扱う
    （例: `ingest_runs` へ記録する）。
    """
    wd = Path(wd).resolve()
    dr = Path(derived)
    if human_md_sig_drift(wd, dr):
        return False
    return _write_human_md_es_sig_marker(dr)


def drop_human_md_es_sig_marker(derived_md_dir) -> bool:
    """`.human_md_es_sig` を明示的に未確定へ戻す（`drop_rag_sig_marker` と同型・削除失敗を
    検知できる版）。

    **再索引を始める前に必ず呼ぶ**（RAG-KV 提案書の `.rag_sig` と同じ順序）: 呼ばずに
    `es_index.index_world()` を直接呼ぶと、以前の成功で既に確定済みのマーカーが残ったまま
    今回の bulk が部分失敗しても、`_human_md_config_sig` は（pending でなくなっているため）
    現行版を返してしまい、ES 自身の `_meta` には「成功して確定した版」のはずの値が
    `ensure_index()`（bulk 実行**前**）の時点で書かれてしまう——bulk の成否と無関係に meta が
    確定値になる「二段階更新の穴」になる。成功時 True、削除失敗（`OSError`）時は False
    （呼び出し元は失敗をログに残しつつ、それでも再索引自体は続行してよい＝この失敗は
    ベストエフォートの安全弁の劣化であって再索引を止める理由ではない）。
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


# ---- Canonical Evidence IR版 drift（E2a/E2b/E4・XLSX/DOCX/PPTX）----
# 現行 document-ir-v2 は置換せず並行生成する。Evidence IR のschema/parser変更だけでMDや既存IRを
# 作り直さないよう、独立マーカーと軽量refreshを持つ。
_EVIDENCE_IR_SIG_MARKER = ".evidence_ir_sig"


def _current_evidence_ir_sig() -> str:
    """現在のEvidence IR契約、parser profile、通常生成対象の抽出器版の署名。"""
    from . import evidence_ir, evidence_spike, excel_display, legacy_provenance, office_native_display, raster_evidence
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


# ---- Evidence IR由来のpipe-free RAG表現版 drift（E3）----
_RAG_SIG_MARKER = ".rag_sig"


def _resolve_ocr_observation_dir(world: str | None) -> Path | None:
    """`world` の公開中 OCR 観測ディレクトリ（無効/未指定/未公開は None）。

    `worlds.observation_dir` は Canonical `derived_dir` と物理 root を分ける契約（隔離 OCR worker
    への read-only 境界）のため、`world` 文字列でしか辿れない——`office_md.py` の他関数群は
    Path のみで動く慣習だが、ここだけは `worlds` を遅延 import する（`worlds.py` 側の
    `ingest.derived_generation`/`ingest.observation_render` import も遅延のため循環しない）。
    """
    if not world or not ocr_enabled():
        return None
    from .. import worlds
    return worlds.observation_current_dir(world)


def _ocr_observation_marker_for(obs_dir: Path | None) -> str | None:
    """`obs_dir`（`_resolve_ocr_observation_dir` の戻り値）を `.rag_sig` 用の不透明な印にする。

    OCR が新しい観測世代を公開するたび（＝この world の何らかの文書の OCR 完了）値が変わる
    （`observation_render.active_observation_dir` は `{canonical_generation_id}/{observation_generation_id}`
    を返すため）。観測なしは None＝`_current_rag_sig` は `"none"` として扱う。
    """
    return f"{obs_dir.parent.name}/{obs_dir.name}" if obs_dir is not None else None


def current_ocr_observation_marker(world: str | None) -> str | None:
    """`world` の OCR 観測の現在状態を表す印（呼び出し元は世界ごとに1回だけ解決して使い回す）。"""
    return _ocr_observation_marker_for(_resolve_ocr_observation_dir(world))


def _current_rag_sig(*, ocr_observation_marker: str | None = None) -> str:
    """Evidence版を包含するRAG renderer/chunker署名。

    `ocr_observation_marker`（O1）: 公開中 OCR 観測世代の印。rag.md の内容は
    Evidence 自体が不変でも OCR 観測（非同期・後から届く）が新しく公開されると変わりうるため
    （`evidence_render.render(observation_set=...)` へ VLM と合流して渡す・§8.1 一本化）、
    この次元を含めることで OCR 完了後の次回 sync が `refresh_rag` を自然に誘発し、
    既存の `.rag_sig`/holdback/`_reindex_after_rag_rewrite` 連鎖にそのまま乗る
    （新しい再生成・再索引の仕組みは作らない）。
    """
    from . import ai_observation, context_ir, evidence_render

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

    `refresh_evidence_ir`/`refresh_rag` を `write_rag_sig_marker=False` で呼んだ場合、確定は
    ES 反映の成否を確認できる呼び出し元（`worker`）へ委ねられる（マーカー保留方式）。呼び出し元は
    ES 反映が成功した時だけこれを呼ぶ。`world` を渡すと OCR 観測次元（O1）も現行値で確定する
    （渡さなければ「観測なし」として確定＝OCR 無効/世界不明な旧呼び出し元は挙動不変）。
    """
    _write_rag_sig_marker(Path(derived_md_dir), ocr_observation_marker=current_ocr_observation_marker(world))


def drop_rag_sig_marker(derived_md_dir) -> bool:
    """`.rag_sig` を明示的に未確定へ戻す（削除失敗を検知できる版・`_remove_marker` は使わない）。

    `_remove_marker` は `OSError` を握り潰すため、削除に失敗したまま先へ進むと保留方式の
    前提（生成開始時点で必ず未確定）が壊れる。成功時 True、削除失敗（`OSError`）時は False。
    """
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
    """失敗時に stale な版マーカーを消す: 現行値のマーカーが残っていると
    drift=False になり、欠落した派生物（document.json 等）の修復フックが二度と走らない。best-effort。"""
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
    from . import evidence_spike, raster_evidence

    if Path(source_path).suffix.lower() in RASTER_EVIDENCE_EXT:
        return raster_evidence.extract_assets(source_path, extracted, destination)
    return evidence_spike.extract_assets(extraction_path, extracted, destination)


def _build_vlm_observation_set(extracted_evidence, rel: str, assets_dir: Path):
    """`vision` が有効かつ VLM 実効可のときだけ、canonical が読めない画像要素を補足観測する（L8）。

    既定（`vision` が有効アームに無い、または VLM が実効利用不可）は常に None＝コストゼロで
    `evidence_render.render(observation_set=None)` と同じ挙動になる。候補選定は
    `ocr_router.build_manifest`（OCR ルーティングと同じ規則＝picture/image_fill 等 OOXML が画素の
    中身を持たない要素だけを selected とする）をこの場で再計算するだけで、`.ocr_route.json`
    （`_write_ocr_routes`・世代全体をまとめて処理する非同期 OCR 経路向け）とは独立（二重採用ではなく
    同じ決定規則の別呼び出し）。`asset_root` は呼び出し元が直前に抽出済みの `{rel}.assets/` を渡す。
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
        # 補足観測は任意（既定OFF）。VLM/Set構築の想定外失敗でCanonicalのrag.md生成を巻き添えにしない。
        _log.warning(
            "VLM 補足観測の生成に失敗しました（Canonical の生成は継続）: %s", rel, exc_info=True)
        return None


def _load_ocr_observation_sets(extracted_evidence, rel: str, obs_dir: Path | None) -> list:
    """`obs_dir`（公開中 OCR 観測ディレクトリ）から、この `rel` 分の Observation Set 群を読む（O1）。

    OCR は隔離 worker が非同期に書く別成果物（`{rel}.ai_observations.jsonl`・
    `observation_render.artifact_paths` と同じ命名）。1 文書に複数画像があれば複数 Set が
    JSONL の行として並ぶ（`observation_render._StreamingObservationBundle.add` が1 job＝1行）。
    ここで読む Set は `ai_observation.from_json_str(..., ir=extracted_evidence)` により
    `source_content_hash` が**今の** Evidence と一致することを再検証する——世界の他文書だけが
    変わって OCR 観測世代が古くなっていない場合でも、この文書自体が変わっていれば安全に弾かれる。
    読めない/検証失敗は「この文書の OCR 観測なし」へ縮退する（VLM 単独と同じ fail-safe・
    Canonical の rag.md 生成を巻き添えにしない）。
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
    """VLM（同期）と OCR（非同期・公開済み分）の観測 Set を合流し、`evidence_render.render` へ渡す
    1つの Set にする（O1・§8.2 の器へ OCR も乗せる）。

    どちらも無ければ None（従来どおり）。片方だけなら合成 Set を作らずそのまま返す
    （`ai_observation.merge_sets` の契約＝単一由来なら provider/model の本文表記を壊さない）。
    両方あるときだけ `merge_sets` で1つへ畳む——合流自体が失敗した場合は VLM 単独へ縮退する
    （OCR の想定外failure でCanonicalのrag.md生成を止めない・既存の fail-safe 方針を踏襲）。
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


# ---- 安全な差し替え（2026-08-16 決定・簡易版の世代公開） ----
# 旧実装は公開中の派生ディレクトリを**先に全消し**してから1件ずつ作り直していた。数十秒〜分かかる
# 変換の間ずっと検索対象が欠け、途中で失敗するとその中途半端な状態が次の同期まで残っていた。
# 別ディレクトリへ作り切ってから**改名2回で差し替える**（改名は一瞬）。失敗時はステージングを
# 捨てるだけで公開中の内容は無傷のまま残る。
# フル世代管理（世代ID・active ポインタ・世代マニフェスト・coverage台帳）は将来の課題として保留。
_STAGING_SUFFIX = ".staging"
_RETIRED_SUFFIX = ".retired"


# ---- フォルダ三分割（§8.1・2026-09-03 裁定）----------------------------------------------------
# 派生物は md（人間用）／rag（RAG 正本＋証跡）／ir（中間表現）の3層に物理分離する。呼び出し元
# （worker.py 等）は従来どおり `derived_md_dir(world)` だけを渡し続け、rag/ir はここで**その兄弟**
# （同じ derived root 配下）として導出する——呼び出し側のシグネチャを変えずに済む。
# `.arms_sig`/`.document_ir_sig`/`.evidence_ir_sig`/`.rag_sig`/`.human_md_es_sig`/`.world_sig` 等
# の world 単位マーカーは**現行位置のまま**（md 層に刻む・層に属さない world 単位の状態）。
def _sibling_layer_dir(md_variant: Path, layer: str) -> Path:
    """`md_variant`（公開中／ステージング／退避のいずれかの md 層パス）と同じ変種の
    `layer`（'rag'|'ir'）兄弟ディレクトリを返す。md と同じ生成/公開サイクル（ステージング→
    改名2回での差し替え）を共有するため、staging/retired サフィックスも一致させる。
    """
    name = md_variant.name
    for suffix in (_STAGING_SUFFIX, _RETIRED_SUFFIX):
        if name.endswith(suffix):
            return md_variant.parent / (layer + suffix)
    return md_variant.parent / layer


def _stamp_rule_only_rag_markdown(markdown: str) -> str:
    """rag.md 書込直前に `生成手段: 規則` を刻む（L5・§8.3-1・rag.md は必ずこの申告を持つ契約）。

    sync 経路（ここ）は規則版を即時生成するだけで LLM は呼ばない——LLM 成形は取り込み後の
    バックグラウンド後追い（`llm_render.run_world_pass`）が `生成手段: 規則` を目印に拾って行う。
    """
    from . import llm_render
    return llm_render.stamp_rule_only(markdown)


# `.derived.json`（sidecar マニフェスト）が横断的に記録する5種の sidecar 種別（`_MANIFEST_SIDECAR_SUFFIXES`
# 参照）が実際にどの層へ物理配置されるか。中間（ir）: document/evidence/derived/ocr_route の各 json。
# RAG 正本（rag）: rag.md・rag_chunks.jsonl・assets/。人間用（md）: md・md.meta.json。
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
# 公開中の派生物が「どの World 内容から作られたか」を刻む印。簡易版の世代公開では、この署名が
# 上流のフル世代管理でいう generation ID の役割を果たす（`derived_generation` 参照）。
_WORLD_SIG_MARKER = ".world_sig"
_OCR_ENABLED_ENV = "SHERPA_OCR_ENABLED"


def ocr_enabled() -> bool:
    """OCR 観測が有効か。**既定 ON**（決定 2026-08-16「初期から組み込む」）。

    ここで有効なのは取り込み側の**ルート生成**（どのラスタを読むかを決めるだけ・実測4.1秒/52文書）。
    読み取り本体は隔離ワーカーが行うため、ワーカーが動いていなければ指示が溜まるだけで、
    取り込みも検索も従来どおり動く（＝既定 ON にしても壊れない）。

    `SHERPA_OCR_ENABLED=0` で止められる。
    """
    raw = os.environ.get(_OCR_ENABLED_ENV)
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _write_ocr_routes(stage_ir: Path, stage_rag: Path) -> dict:
    """OCR 実行とは独立に、Evidence 内の全ラスタ候補を決定的に分類する（上流 38ffa52 の移植）。

    ここは「読む/読まない」を決めるだけで、画像の意味は推定しないし OCR も走らせない。
    `{rel}.evidence.json`/出力の `{rel}.ocr_route.json` はいずれも ir 層ステージング（`stage_ir`）、
    asset inventory は rag 層ステージング（`stage_rag`）から読む（§8.1 三階層・`.assets/` は
    `rag.md` と同じ層）。公開と同時に切り替わる。
    """
    from . import evidence_ir, ocr_router

    summary = {"documents": 0, "selected": 0, "excluded": 0, "failed_binding": 0}
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
    return summary


def _recover_interrupted_swap(target: Path) -> None:
    """改名2回の間で中断した場合（公開中が無く退避先だけがある）に、退避先を公開中へ戻す。

    状態遷移（呼び出し元 `_build_derived_into_staging` の setup 段階から見た表）:
      - target 有り                              → 中断なし。何もしない。
      - target 無し・retired 無し                → 中断ではない（初回ビルド・まだ何も公開していない）。何もしない。
      - target 無し・retired 有り→rename 成功    → 中断から復旧。target が旧内容で復活。
      - target 無し・retired 有り→rename 失敗    → **二重障害**（前回の公開失敗時のロールバックに続き、
        今回の復旧も失敗）。ここで例外を握り潰して続行すると、後続の `dr.mkdir`/`_publish_staging` へ
        進んでしまい、`_publish_staging` 冒頭の `shutil.rmtree(retired, ...)` が唯一残った旧世代
        （retired）ごと削除する——派生物が全消失する。retired を消さずに例外を送出し、呼び出し元
        （`_build_derived_into_staging`）の `derived_setup_failed` として打ち切らせる（fail-loud）。
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

    後半の改名（staging→target）が失敗すると、target は既に retired へ退避済み＝target が
    存在しない状態になる（派生 root ごと消え、Office 文書が grep 不能になる）。これを避けるため、
    後半が失敗したら retired→target で即時ロールバックしてから例外を再送出する（呼び出し元は
    「公開失敗」を記録するだけでよく、旧内容の復元まで意識しなくてよい）。ロールバック自体が
    失敗した場合、旧内容そのものは失われない（retired へ退避したままレイアウト上は無傷で
    残り続ける）が、target が不在のまま公開できない状態になる——その旨をログへ残す
    （隠蔽しない・fail-loud）。旧内容自体の復旧は次回 build の `_recover_interrupted_swap`
    が試みる（それも失敗した場合の扱いはそちらの docstring 参照）。
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
    """自プロセスの RSS（GiB）。/proc/self/status の VmRSS を読む（Linux 専用・読めなければ None）。
    重い変換の前後で実メモリをログへ出すため（閉域実機の OOM 観測 2026-09-04）。"""
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
    """公開中の派生物を壊さずに作り直す薄いラッパ（2026-08-16）。

    実体は `_build_derived_into_staging`。途中で例外が起きてもステージングを残さない
    （残骸が次回の `rmtree` まで容量を占めるのを避ける）。公開中の内容には触れないため、
    例外がそのまま呼び出し元へ伝播しても検索は従来どおり動き続ける。

    `world_sig` を渡すと、公開する派生物に**その中身を作った World 署名**を刻む
    （`.world_sig`）。OCR のような後段の任意処理が「今公開されている派生物はどの内容から
    作られたか」を、原本を再スキャンせずに確かめるために使う（`derived_generation` 参照）。

    `world`（O1）は `_build_derived_into_staging` docstring 参照。
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
    # OCR は任意観測（既定OFF）。有効時だけ、公開前のステージング上で**どのラスタを読むか**を
    # 決定的に分類しておく（OCR 自体は実行しない＝隔離 worker の仕事）。ここで作れば、公開と
    # 同時にルートも入れ替わり、Evidence とルートが食い違う瞬間が生まれない。
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
    # 完了Gate: 「作り切れた」ときだけ公開中と差し替える。文書ごとの変換失敗は failed notice へ
    # 縮退済みで正常系（S3の契約＝失敗も索引する）。ここで見るのは**縮退すらできなかった失敗**
    # （`_generate_evidence` のコメント「notice自体のrenderに失敗した場合だけ…公開を止める」）。
    # 止めた場合、公開中は旧内容のまま生き続ける（欠けた状態を見せない）。
    # `document_ir_failed` はここに含めない: docx/xlsx の document-ir 構築失敗は
    # `OoxmlArm.convert()` が必ず notes 付き ArmResult を返し、呼び出し元がそのまま
    # evidence-notice（failed notice）へ縮退させる正常系（S3の契約と同じ）——`.document_ir_sig`
    # マーカーを確定しない（次回 sync が再試行する）という形で drift 検知には残るが、
    # 公開全体を止める理由にはならない（壊れた1文書のために案件全体の取り込みを失敗させない）。
    blocking = {key: rep.get(key) or 0 for key in (
        "rag_failed", "evidence_ir_failed", "unhandled_failed") if rep.get(key)}
    if blocking:
        for s in all_staging:
            shutil.rmtree(s, ignore_errors=True)
        rep["error"] = "derived_incomplete:" + ",".join(f"{k}={v}" for k, v in sorted(blocking.items()))
        return rep
    # 3層それぞれ独立に改名2回で差し替える（§8.1・フォルダ分離）。跨ぎでの原子性は無い——
    # 同一ファイルシステムの rename 自体は高信頼だが、途中で1層だけ失敗すると3層が食い違う
    # 瞬間が生まれうる（次回 sync の drift 検知/全再構築が自己修復する・複数ディレクトリを
    # 束ねるフル世代管理は将来課題として保留、`_STAGING_SUFFIX` 冒頭コメントと同じ判断）。
    # **1層の失敗が他層の公開試行を止めない**（`try/except` を層ごとに独立させる）: 各層は
    # 自分の staging/retired だけで完結する独立した差し替えであり、他層の障害の有無に関わらず
    # 自分自身の公開を試みるのが「独立」の実質——1層目で例外を伝播させて残りを試みずに終えると、
    # 障害と無関係な層まで（旧内容のまま／`_publish_staging` 自身の失敗時ロールバックも含め）
    # 一律未実行になってしまう。
    publish_failures: list[str] = []
    for staging_dir, published_dir in (
        (staging_ir, published_ir), (staging_rag, published_rag), (staging, published),
    ):
        try:
            _publish_staging(staging_dir, published_dir)
        except OSError as e:
            # `_publish_staging` は後半 rename 失敗時に旧公開中（retired）を published へ自前で
            # ロールバックを試みる。ロールバックが成功すれば published は旧内容のまま生き続けるが、
            # ロールバック自体も失敗した場合は published が不在のまま（旧内容は retired 側に残る・
            # `_publish_staging` の docstring／`_recover_interrupted_swap` 参照）。
            publish_failures.append(f"{published_dir.name}:{e.__class__.__name__}")
    # 成功した層は staging が rename 済み（既に存在しない）ので no-op。失敗した層だけ、
    # 公開できなかった中途半端な新内容がここで掃除される。
    for s in all_staging:
        shutil.rmtree(s, ignore_errors=True)
    if publish_failures:
        rep["error"] = "derived_publish_failed:" + ",".join(publish_failures)
    return rep


def _check_partial_extraction(rp: Path, md: str, rel: str, document, out: list[dict]) -> None:
    """静かな部分抽出の疑いを検知する（ING-1・安価な整合チェックのみ・新パーサ/深いカバレッジ計算はしない）。

    **失敗にはしない**（呼び出し元の変換は成功のまま継続する）。疑いがあれば `out` へ
    `{"doc": rel, "basis": "size_ratio"|"xlsx_row_ratio", ...根拠の数値}` を1件だけ追記する
    （両方の根拠が同時に成立しても1文書1件——`size_ratio` を先に見る＝拡張子を問わない粗い網）。
    `document`（`ooxml_arm` が返す `DocumentIR | None`）の `sheet` 要素が持つ
    `source_map["partial_extraction_suspected"]`（`_build_xlsx_ir` が cap 打切りと独立に判定済み・
    H2 の自己申告打切りはそちらで除外済み）を読むだけで、ここで再判定はしない。

    H2 の自己申告打切り（`sheet.source_map["truncated"]`）は**文書全体の `size_ratio` 判定だけ**
    省略させる（サイズ比判定より先に確認する）。打切りがあるシートを含む文書は、生成MDが本来の
    内容より小さいことが既に自己申告済み＝正常（打切り注記そのものが短い）なので、`size_ratio`
    判定では「抽出不完全の疑い」にしない。ただし打切りは1シート単位の事情であり、**別のシート**が
    （cap 打切りとは独立に）`partial_extraction_suspected` を立てていることはありうるため、
    以降の xlsx_row_ratio 走査は打切りの有無に関わらず必ず実行する（1枚の自己申告打切りが
    他シートの疑いまで消してしまうと最小入力の出力が空になっていた）。
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


# ---- per-file 変換結果キャッシュ（CONV-CACHE・2026-09-03・実環境障害対応の最終柱）--------------------
# 実環境（10,000ファイル・1件30秒級）では `_build_derived_into_staging` の途中死が再実行を毎回0から
# 始めさせていた（ステージングは全件成功時だけ公開する契約＝正しいが、途中死は進捗を全損する）。
# 旧形式変換だけが持っていた「再ビルドを跨ぐキャッシュ」（`legacy_convert.cache_root_for`）と同じ型を
# 変換本体（①アーム実行＋Evidence/RAG 生成）へ拡張する。キー＝(原本の resolved path・st_size・
# st_mtime_ns・変換パイプライン署名)。ヒットしたら実変換（アーム実行・evidence 生成・LLM/VLM 呼び出し）
# を丸ごとスキップし、キャッシュ済みの派生一式をステージングへコピーするだけで済ませる。
# 失敗ファイル（notice へ縮退したもの）は対象外——次回 sync が必ず再試行する現行契約を維持する。
_CONV_CACHE_DIRNAME = "_conv_cache"
_CONV_CACHE_MAX_BYTES_ENV = "SHERPA_CONV_CACHE_MAX_BYTES"
# per-file キャッシュが実際にミラーする sidecar 種別。`_LAYER_FOR_SIDECAR_SUFFIX` の部分集合——
# `.derived.json`（マニフェスト）はキャッシュせず、復元後に既存の `_write_derived_sidecar_manifest`
# が実際に存在するファイルから毎回そのまま書き直す（人間 md の版は rep_delta 経由で運ぶ・下記）。
# `.ocr_route.json` は `build_derived`（公開直前・ステージング全体を1回で走査）が世界単位で書くもので
# per-file 変換の一部ではないため対象外（「毎回生成・安い」＝やらないこと節）。
_CONV_CACHE_SIDECAR_SUFFIXES = (
    ".md", ".md.meta.json", ".evidence.json", ".document.json", ".rag.md", ".rag_chunks.jsonl",
)


def _conv_cache_root_for(derived_md_dir) -> Path:
    """派生MD dir と同階層の per-file 変換結果キャッシュ dir（`legacy_convert.cache_root_for` と同型）。

    `md`/`rag`/`ir` の兄弟に置くため公開/ステージングの改名2回の差し替えに巻き込まれず、
    再ビルドを跨いで残る。world 削除時は `derived/{world}` 木ごと消える＝鏡と整合。
    """
    return Path(derived_md_dir).parent / _CONV_CACHE_DIRNAME


def _current_conv_cache_pipeline_sig(*, ocr_observation_marker: str | None) -> str:
    """per-file キャッシュのヒット判定に使う変換パイプライン全体の署名。

    新しい版概念は発明せず、既存の各層版マーカー（`_current_arms_sig`／`_current_document_ir_sig`／
    `_current_human_md_sig`／`_current_rag_sig`）をそのまま束ねるだけ。いずれか1つでも変われば
    署名全体が変わり**全ファイル**キャッシュミスになる——現行の drift 全再ビルドと同じ挙動
    （正しさ優先・部分的に古い版が混在する余地を作らない）。`ocr_observation_marker`（O1）は
    `_current_rag_sig` 経由で含める＝OCR 観測が新しく公開されるとこの world の全キャッシュが
    ミスになり、次回 sync の `refresh_rag` 相当が自然に効く（新しい再生成の仕組みは作らない）。
    """
    return (f"arms={_current_arms_sig()};"
            f"document_ir={_current_document_ir_sig()};"
            f"human_md={_current_human_md_sig()};"
            f"rag={_current_rag_sig(ocr_observation_marker=ocr_observation_marker)}")


def _conv_cache_source_key(rp: Path, pipeline_sig: str) -> str | None:
    """キャッシュキー（原本の resolved path・st_size・st_mtime_ns・パイプライン署名）。

    `rp.resolve()`/`rp.stat()` が失敗（消失・権限・race）したらキャッシュ対象外として None
    （呼び出し元は通常の実変換へフォールバックする＝fail-safe）。
    """
    try:
        resolved = str(rp.resolve())
        st = rp.stat()
    except OSError:
        return None
    return f"{resolved}|{st.st_size}|{st.st_mtime_ns}|{pipeline_sig}"


def _conv_cache_slot(cache_root: Path, rel: str) -> tuple[Path, Path]:
    """`rel` のキャッシュスロット（メタ JSON・内容ディレクトリ）。1 rel = 最新1件のみ保持
    （`legacy_convert.ensure_ooxml` の `{rel}{ext}` + `.key` と同型）。"""
    return cache_root / (rel + ".key.json"), cache_root / (rel + ".d")


def _conv_cache_lookup(cache_root: Path, rel: str, want_key: str) -> tuple[dict, Path] | None:
    """キャッシュヒットなら `(rep_delta, content_dir)` を返す。ミス/壊れ/鍵不一致は None。"""
    meta_path, content_dir = _conv_cache_slot(cache_root, rel)
    meta = json_io.read_json(meta_path, default=None)
    if not isinstance(meta, dict) or meta.get("key") != want_key:
        return None
    rep_delta = meta.get("rep_delta")
    if not isinstance(rep_delta, dict) or not content_dir.is_dir():
        return None
    return rep_delta, content_dir


def _conv_cache_restore(content_dir: Path, rel: str, dr: Path, dr_rag: Path, dr_ir: Path) -> bool:
    """キャッシュ内容一式を3層のステージングへコピーする（成功時 True）。

    失敗（OSError）したら呼び出し元は実変換へフォールバックする——この rel について部分的に
    コピー済みのファイルが残っていても、実変換が同名で上書きするため実害は限定的（fail-safe）。
    """
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

    一時 dir へコピーしてから改名する（コピー中断で壊れたキャッシュを次回ヒット扱いしない）。
    メタ JSON（鍵・rep_delta）は内容の改名が終わった**後**に書く——鍵一致だけがヒット判定の唯一の
    根拠のため、内容が揃う前に鍵が読める状態を作らない。失敗しても取り込み自体は継続する
    （次回も実変換するだけ＝キャッシュは最適化であり正しさの前提ではない）。
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
    """今回の原本一覧（`seen_rels`）に無い rel のキャッシュを削除する（鏡・非有界化しない）。

    呼び出し元は per-file ループを最後まで走り切った（＝完走した）時だけこれを呼ぶ——途中死した
    場合は呼ばれず、次回 sync がまだ生きている rel のキャッシュをそのまま再利用できる
    （進捗を保持する・「完走しなかった場合は剪定しない」）。
    """
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


def _conv_cache_enforce_cap(cache_root: Path) -> None:
    """`SHERPA_CONV_CACHE_MAX_BYTES`（既定 0＝無制限）を超えたらキャッシュを古い順に削る。

    実環境の派生物総量（3層×1原本あたり数KB〜数百KB）は原本自体と同オーダーで、原本を既に
    保持できているディスクなら追加分も同オーダーに収まる想定のため既定は無制限——閉域環境で
    ディスクが逼迫する場合だけ env で明示的に絞る安全弁。
    """
    cap = _env_int(_CONV_CACHE_MAX_BYTES_ENV, 0, 0, 1024 * 1024 * 1024 * 1024)
    if cap <= 0 or not cache_root.is_dir():
        return
    entries: list[tuple[int, Path, Path, int]] = []
    total = 0
    for meta_path in cache_root.rglob("*.key.json"):
        rel = meta_path.relative_to(cache_root).as_posix()[: -len(".key.json")]
        content_dir = cache_root / (rel + ".d")
        try:
            size = sum(p.stat().st_size for p in content_dir.rglob("*") if p.is_file())
            mtime = meta_path.stat().st_mtime_ns
        except OSError:
            continue
        entries.append((mtime, meta_path, content_dir, size))
        total += size
    if total <= cap:
        return
    entries.sort(key=lambda e: e[0])          # 古い順（作成/更新が最も昔のものから削る）
    for _mtime, meta_path, content_dir, size in entries:
        if total <= cap:
            break
        try:
            meta_path.unlink(missing_ok=True)
            shutil.rmtree(content_dir, ignore_errors=True)
        except OSError:
            continue
        total -= size


def _build_derived_into_staging(
    wd, derived, *, progress: Callable[[int, int], None] | None = None, world: str | None = None,
) -> dict:
    """`wd` 配下の Office を MD化し `derived/{rel}.md` に書き出す（鏡＝毎回まるごと作り直す）。

    `world`（O1）: 渡すと、直前まで公開中だった Canonical 世代に紐づく OCR 観測（隔離 worker が
    非同期に公開した分）を VLM と合流して rag.md へ含める（`_build_observation_set` 参照）。
    未指定/OCR 無効時は VLM のみ（従来どおり）。この関数はまだ**旧**公開世代（このビルドが
    差し替えるはずの世代）を見ている——`build_derived` が改名2回で差し替える前なので、
    観測は「今の原本から見て文書ごとに古くなっていないか」を `source_content_hash` で
    個別に再検証してから使う（`_load_ocr_observation_sets` 参照）。

    2026-08-16: 書き込み先は公開中ではなく**ステージング**（`{derived}.staging`）で、作り切ってから
    改名2回で差し替える（`_publish_staging`）。取り込み中も公開中の派生物が生き続け、失敗しても
    壊れた状態が残らない。

    CONV-CACHE（2026-09-03）: per-file ループの各 rel は、原本 mtime/size と変換パイプライン署名が
    前回成功時と一致すれば `_conv_cache_root_for(dr)` のキャッシュから復元するだけで済ませ、
    実変換（①アーム実行・Evidence/RAG 生成）をスキップする。ステージング自体は毎回まるごと
    作り直す（鏡モデルは不変）が、**per-file の変換結果はステージングの再作成を跨いで再利用**
    することで、途中死からの再実行で0からやり直さずに済む（`_conv_cache_store`/`_conv_cache_lookup`
    参照）。失敗（notice へ縮退したもの）はキャッシュ対象外——次回 sync が必ず再試行する現行契約を
    保つ。

    返値 `{converted, failed, unsupported, by_ext, legacy_conversion_failures, document_ir_generated,
    document_ir_failed, document_ir_failures, evidence_ir_generated, evidence_ir_failed,
    evidence_ir_failures, rag_generated, rag_failed, rag_failures, error?}`
    （status 表示用）。ソース（wd）には一切書かない。
    `failed`＝変換失敗（壊れ等）、`unsupported`＝PDF/旧バイナリ（MVP 未対応）。`legacy_conversion_failures`
    （`[{"doc": rel, "reason": "legacy_conversion_failed"}]`）＝`failed` のうち旧形式（.doc/.xls/.ppt）の
    前段変換自体が失敗した rel（`document_ir_failures` 等と異なり、原因コードは常に固定文字列——
    前段変換の失敗理由そのものは呼び出し元に返らないため）。**例外は投げない**（best-effort・fail-safe）:
    派生先がソース配下と重なる/セットアップ失敗時は `error` を立てて何も書かずに返す（READ-ONLY source 保護・RV High#2）。
    **1ファイル分の変換処理は try/except で包む**（A2/A3 RV High #1）: PDF/VLM抽出由来の想定外例外で
    1件が失敗しても他ファイルの変換は継続する（failed 計上のみ・全体を止めない）。

    `document_ir_*`（DOC-IR-001.5・修正4）: document.json の書込失敗は握りつぶさず `document_ir_failed`／
    `document_ir_failures`（`[{"doc": rel, "reason": "write_failed" | "build_failed:<Exc>"}]`）に計上する
    （`converted`/`failed` とは独立のカウンタ＝MD 自体は成功しているため）。**`document_ir_failed == 0`
    のときだけ** `.document_ir_sig` マーカーを書く（失敗が残れば次回 sync の `document_ir_sig_drift` が
    True のままになり `refresh_document_ir` が再試行する）。

    `evidence_ir_*`（E2b/E4）: 素のXLSX/DOCX/PPTXをOOXML armで変換できた場合に限り、既存IRと並行して
    `derived/{rel}.evidence.json` を生成する。自然文や業務状態へ早期に畳まず、セル、DrawingML object、
    locator、幾何関係、coverageを保持する。全件成功時だけ `.evidence_ir_sig` を書き、1件でも失敗した
    generationは公開品質gateで拒否できるよう独立カウンタへ記録する。

    `rag_*`（E3）: 成功したEvidence IRからpipe表を使わない`{rel}.rag.md`と、同じrecord集合に由来する
    `{rel}.rag_chunks.jsonl`を同時生成する。画像assetは`{rel}.assets/`へcontent hash名で取り出す。
    旧MDが空になる画像だけのXLSXでも、この新しいRAG表現は生成する。
    """
    from .. import scope_infer as si
    # IRキーは早期 return（overlap/setup 失敗）でも同じ形で返す（RV Low #5: レポート契約の一貫性）。
    rep = {"converted": 0, "published_notice_count": 0, "failed": 0, "unsupported": 0,
           "unhandled_failed": 0, "unhandled_failures": [], "by_ext": {},
           "legacy_conversion_failures": [], "conversion_failures": [],
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
    obs_dir = _resolve_ocr_observation_dir(world)         # 1回だけ解決（O1・文書ごとに解決し直さない）
    ocr_observation_marker = _ocr_observation_marker_for(obs_dir)
    by = Counter()
    enabled = _arms.enabled_arms()                       # 有効アーム（既定 ooxml,pdf_text＝現行と同一）
    conv = convertible_exts()                            # 有効アームが今 MD化できる拡張子集合
    # PNG/JPEGはOCRなしでも画像の存在・hashをEvidence化するため、常に候補に含む。
    candidate = OFFICE_EXT | RASTER_EVIDENCE_EXT | conv
    legacy_cache = legacy_convert.cache_root_for(dr)     # 旧→新変換のキャッシュ（md/ の兄弟・再ビルドをまたいで残す）
    source_failure_notices: set[str] = set()
    conv_cache_root = _conv_cache_root_for(dr)           # per-file 変換結果キャッシュ（CONV-CACHE・md/ の兄弟）
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
            if prebuilt_evidence is None or actual.suffix.lower() not in LEGACY_OFFICE_EXT:
                _extract_evidence_assets(rp, actual, extracted_evidence, assets_dir)
                observation_set = _build_observation_set(extracted_evidence, rel, assets_dir, obs_dir=obs_dir)
            rendered = evidence_render.render(
                extracted_evidence, source_name=rel, observation_set=observation_set)
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
            # Evidence抽出は成功しても、rendererのcoverage検証だけが文書固有の構造で失敗することがある。
            # その1件をgeneration全体の失敗へ昇格させず、原本identityに拘束したsource-level failed
            # noticeへ置き換える。notice自体のrenderに失敗した場合だけrag_failedとして公開を止める。
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
        """このrel処理直前の rep カウンタ・リスト長のスナップショット（CONV-CACHE の差分計算用）。
        `converted` 自体は含めない（キャッシュヒット/成功はどちらも常に+1固定のため呼び出し元が直接足す）。
        """
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

        IR/Evidence/RAG は「MD 自体は成功（`converted`）したが一部だけ書込失敗した」という縁の
        カウンタ増分（OSError→`*_failed`＋`*_failures` 追記だけで `continue` しない経路）を持ちうる
        ため、`converted` 以外の全カウンタ／リスト／dict の増分を丸ごと運ぶ（部分的な決め打ちをしない）。
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
        """直前に処理した rel（ループ変数 `rel`/`conv_cache_key`/`conv_cache_rep_before`/
        `human_md_sig_for_rel` を閉包で参照）を CONV-CACHE へ保存する。両方の成功終着点
        （raster／通常変換）から呼ぶ共通処理（DRY）——`conv_cache_key` が None（`ext not in conv`）
        なら no-op。"""
        if conv_cache_key is None or conv_cache_rep_before is None:
            return
        rep_delta = _conv_cache_rep_delta(conv_cache_rep_before)
        rep_delta["human_md_sig"] = human_md_sig_for_rel
        _conv_cache_store(conv_cache_root, rel, conv_cache_key, rep_delta, dr, dr_rag, dr_ir)

    def _publish_failed_size_notice(rp: Path, rel: str, reason_code: str, detail: dict | None = None) -> None:
        """変換前（openpyxl/pypdf 等のフルロード前）に諦めるサイズ/セル数系ガードの共通処理
        （MEM-1 の `size_exceeded` と MEM-2 の `cell_count_exceeded`/`uncompressed_size_exceeded`）。

        failed notice を発行し `failed`/`published_notice_count`/`conversion_failures` へ計上する
        （呼び出し側はこの後 `continue` する）。`detail` は実測値（測定セル数/バイト数と上限）を
        Evidence の `coverage.detail` へ載せる（`evidence_render._coverage_notice_records` が
        利用者向けの平文メッセージへ実測値を埋め込む）。"""
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

    candidate_total = sum(1 for rp, _rel in si.safe_files(wd) if rp.suffix.lower() in candidate)
    processed_candidates = 0
    if progress is not None:
        progress(0, candidate_total)

    for rp, rel in si.safe_files(wd):
        ext = rp.suffix.lower()
        if ext not in candidate:
            continue
        by[ext] += 1
        conv_cache_seen_rels.add(rel)        # 剪定用「今回の原本一覧」（成否問わず候補に入った rel すべて）
        # RV High #1（belt-and-braces）: `accepts()`/`convert()` は各アーム実装（PDF/vision の
        # 抽出処理）由来の想定外例外を投げうる。ファイル1件の失敗が for ループ全体を止めて他ファイルの変換まで
        # 巻き込むことがないよう、1ファイル分の処理をまるごと try/except で包む（fail-safe・failed 計上のみ）。
        rel_unhandled = False               # 下の except（想定外の例外）で True にする
        # この rel の `.md` が今回 human_md（`OoxmlArm`・docx/xlsx）で生成できたら
        # `_current_human_md_sig()` を入れる。マニフェストの `asset_versions.human_md` として書き、
        # レンダラ/抽出器の版だけが変わった時に **この asset だけ**を選択的に再生成できるようにする
        # （RAG-KV の drift 連鎖と同じ単一 asset 版の考え方・全再構築は誘発しない）。
        human_md_sig_for_rel: str | None = None
        conv_cache_key: str | None = None    # 非None＝この rel はキャッシュ対象（`ext in conv`）
        conv_cache_rep_before: dict | None = None
        _t0 = time.monotonic()
        _rss0 = _proc_rss_gib()
        _log.info("MD化を開始します: %s%s", rel,
                  f"（RSS {_rss0:.1f}G）" if _rss0 is not None else "")
        _done_label = "完了"                     # finally の完了行用（キャッシュ復元なら差し替え）
        try:
            # CONV-CACHE（実変換対象＝`conv`）: 原本 mtime/size と変換パイプライン署名が前回成功時と
            # 一致すれば、①アーム実行・Evidence 生成・LLM/VLM 呼び出しを丸ごとスキップし、キャッシュ済み
            # 派生一式をステージングへコピーするだけで済ませる。ミス（原本変化/署名変化/未キャッシュ）は
            # 下の通常経路へそのまま流れる——キャッシュは最適化であり、正しさはここに依存しない。
            if ext in conv:
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
                # 変換前に諦める＝openpyxl/pypdf 等のフルロードを一切発生させない（MEM-1）。
                _publish_failed_size_notice(rp, rel, "size_exceeded")
                continue
            if ext == ".xlsx":
                # 圧縮爆弾ガード（MEM-2）: st_size は圧縮後サイズのため小さい xlsx でも展開後に
                # 巨大化しうる。openpyxl を一切開かず zip 内 dimension だけ見てセル数を見積もる。
                # 見積不能（dimension欠落/壊れたzip等）は fail-open で通常の変換経路へ流す。
                estimated_cells = _xlsx_estimated_cell_count(rp)
                if estimated_cells is not None and estimated_cells > _XLSX_CELL_CAP:
                    _publish_failed_size_notice(
                        rp, rel, "cell_count_exceeded",
                        detail={"measured_cells": estimated_cells, "cap_cells": _XLSX_CELL_CAP})
                    continue
            if ext in CONVERTIBLE_EXT:
                # 非圧縮サイズガード（MEM-2）: docx/pptx にも同型の圧縮爆弾リスクがある（xlsx は
                # 上のセル数ガードが本命・こちらは3形式共通の粗い網）。
                uncompressed = _office_uncompressed_total_bytes(rp)
                if uncompressed is not None and uncompressed > _OFFICE_UNCOMPRESSED_CAP_BYTES:
                    _publish_failed_size_notice(
                        rp, rel, "uncompressed_size_exceeded",
                        detail={"measured_bytes": uncompressed, "cap_bytes": _OFFICE_UNCOMPRESSED_CAP_BYTES})
                    continue
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
            if ext in legacy_convert.LEGACY_EXT_MAP:         # 旧形式＝先に OOXML へ前段変換（LibreOffice 等・W0）
                materialized = legacy_convert.ensure_ooxml(rp, rel, legacy_cache)
                if materialized is None:
                    # `backend_ready=False`（バックエンド未設定/未到達）は**未対応**（未来失敗ではない・
                    # 軽量再生成パス`refresh_evidence_ir`等と同じ分類——「失敗」と「対象外」を混ぜない・
                    # ING-1閉じた理由語彙）。`backend_ready=True` で実際に変換を試みて失敗した場合だけ
                    # `failed` へ計上し、`take_conversion_failure_reason()` でタイムアウトかどうかを
                    # 区別する（`_run_soffice` の subprocess タイムアウトのみ判別可・他は汎用失敗）。
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
                # 非圧縮サイズガード（MEM-2）: 旧形式変換後の materialized OOXML にも同じ上限を適用
                # する（原本 .doc/.xls/.ppt の入口サイズ判定は上の `_office_size_exceeded` が既に
                # 通した後・小さい旧形式が展開後に巨大な OOXML へ変換されるケースへの備え）。
                uncompressed = _office_uncompressed_total_bytes(conv_path)
                if uncompressed is not None and uncompressed > _OFFICE_UNCOMPRESSED_CAP_BYTES:
                    _publish_failed_size_notice(
                        rp, rel, "uncompressed_size_exceeded",
                        detail={"measured_bytes": uncompressed, "cap_bytes": _OFFICE_UNCOMPRESSED_CAP_BYTES})
                    continue
            arm_name, result = _convert_with_arms(conv_path, enabled)  # A1＝最初に受理したアーム1本
            if result is None or result.md is None:
                # fail-closed: docx/xlsx は document-ir 構築失敗が直接 md=None を招く
                # （`OoxmlArm.convert()`）。ここで見逃すと `document_ir_failed` が 0 のまま据え置かれ、
                # `.document_ir_sig` 等の版マーカーが「全件成功」の嘘をついたまま確定してしまう
                # （次回 sync の drift 検知が働かず再試行の契機を失う）。
                if (result is not None and result.document is None
                        and ext in ooxml_arm._IR_EXTS):
                    document_ir_failed += 1
                    reason = next(
                        (n for n in result.notes if n.startswith("document_ir_failed:")),
                        "document_ir_failed:unknown")
                    document_ir_failures.append({"doc": rel, "reason": reason})
                    # IR 構築の失敗はここで直接 failed notice を発行する（Evidence 側の独立な
                    # 再抽出＝`_generate_evidence(rp, rel)` の素の呼び出しに成否を委ねない）。
                    # 委ねると、構造がより緩い Evidence 抽出だけが「成功」してしまうことがあり
                    # （例: 本文が空の docx）、その場合 notice が一切出ないまま `{rel}.md` が
                    # 欠落し、文書が台帳・grep から消える——document_ir_failed を非 blocking に
                    # した前提（「縮退した failed notice が必ず出る」）が崩れる。
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
                            # 変換arm名を付けるとquality Gateが通常のDocument IR chainまで要求してしまう。
                            # これは変換成功物ではなく、source-level failed coverageを検索可能にするnoticeである。
                            _write_provenance(dst, "evidence_notice", notice_result)
                            published_notice_count += 1
                    failed += 1
                    continue
                if (result is not None and result.document is not None
                        and ext in (".docx", ".xlsx") and arm_name == "ooxml"):
                    # IR は構築できたが本文が無く human_md が意図的に None を返した（実質 docx のみ・
                    # xlsx は human_md.render_xlsx が常に非 None を返す）＝失敗ではない。今回の版で
                    # 「空である」と確認できたことを記録し、次回 sync の human_md_sig_drift による
                    # 無限再評価ループを止める（`.md` 自体は書かない＝存在しない状態を維持する）。
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
                        # 変換arm名を付けるとquality Gateが通常のDocument IR chainまで要求してしまう。
                        # これは変換成功物ではなく、source-level failed coverageを検索可能にするnoticeである。
                        _write_provenance(dst, "evidence_notice", notice_result)
                        published_notice_count += 1
                failed += 1
                # IR を持たない拡張子（PDF/PPTX 等）の一般失敗も rel・理由を残す
                # （件数だけ増えて一覧に載らない穴を塞ぐ）。PDF は暗号化を判別可能な範囲で明示する。
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
            if ext in (".docx", ".xlsx") and arm_name == "ooxml":
                # 素の docx/xlsx だけを対象にする（旧 .doc/.xls の legacy 前段変換経由は対象外＝
                # スコープを絞った既知の制約。legacy 経路は次回の通常 sync/run が拾う）。
                human_md_sig_for_rel = _current_human_md_sig()
            _write_provenance(
                dst, arm_name, result, legacy_conversion=legacy_conversion)  # 来歴サイドカーをESチャンクメタへ搬送
            # B1（RV Med）: IR は**原本が素の .docx/.pptx/.xlsx のときだけ**書く。旧 .doc/.ppt/.xls は前段変換
            # （legacy_convert）で materialized .docx/.pptx/.xlsx になっており、OoxmlArm は渡された OOXML の
            # suffix しか見ないため IR 自体は生成できてしまう。しかしそれを書き出すと doc_id/source.path=rel
            # （例 "old.doc"）なのに file_type="docx"・content_hash=変換後キャッシュファイルの hash という
            # **来歴汚染**が起きる（原本は .doc/.ppt/.xls であり、IR の由来は変換後の一時 OOXML）。
            # DOC-IR-001/003/004 は素の DOCX/PPTX/XLSX のみをスコープとする（旧形式は原本 hash/file_type の
            # 仕様を固めるフェーズ2以降）ため、legacy 経路（`ext` が原本の拡張子＝ループ先頭で確定した
            # rp.suffix）では document.json を書かない。
            if ext in ooxml_arm._IR_EXTS:                     # document-ir-v2 の並行生成（DOC-IR-001/003/004）
                if result.document is not None:
                    result.document.doc_id = rel
                    result.document.source.path = rel        # 絶対パス（環境依存値）を派生物に残さない＝決定的
                    try:                                       # 修正4: 失敗を握りつぶさず計上する（write_text_atomic）
                        json_io.write_text_atomic(dr_ir / (rel + ".document.json"),
                                                  document_ir.to_json_str(result.document))
                        document_ir_generated += 1
                    except OSError:
                        document_ir_failed += 1
                        document_ir_failures.append({"doc": rel, "reason": "write_failed"})
                        _log.warning(
                            "document.json の書込に失敗しました（次回 sync の drift 検知で再試行）: %s", rel)
                else:
                    # アーム側（ooxml_arm.convert）で IR 構築自体が例外で失敗した場合は notes に
                    # `document_ir_failed:<ExcClassName>` が残る（fail-safe で md は継続）。md は成功でも
                    # IR だけ握りつぶさず計上する（修正4）。
                    boom = next((n for n in result.notes if n.startswith("document_ir_failed:")), None)
                    if boom is not None:
                        document_ir_failed += 1
                        document_ir_failures.append(
                            {"doc": rel, "reason": f"build_failed:{boom.split(':', 1)[1]}"})
            if (ext in EVIDENCE_EXT and _evidence_arm_selected(ext, arm_name)) or legacy_conversion is not None:
                # 現行document.json/blocks/chunksを書き終えてから同じDocument IRをEvidenceへ移す。
                # consume_legacyでtable cell listを順次空にし、密表で2組のcell objectを同時保持しない。
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
                    # 通常MD/Document IRは失敗noticeの検索表現と混在させない。文書単位failedとして
                    # notice chainだけを公開し、他文書は同じgenerationで継続する。
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
                # 想定外の例外（belt-and-braces の except）で終わった rel は、sidecar が
                # 不完全な途中状態のままになりうるため、その状態を「実測の正本」として
                # マニフェスト化しない（マニフェスト欠落＝次回 sync が要再生成として拾う）。
                unhandled_failed += 1
            elif not _write_derived_sidecar_manifest(dr, dr_rag, dr_ir, rel, human_md_sig=human_md_sig_for_rel):
                # マニフェスト自体の書込失敗も、不完全な世代を「成功」と偽らないよう
                # unhandled_failed へ計上し公開Gate（build_derived）で止める。
                unhandled_failed += 1
                unhandled_failures.append({"doc": rel, "reason": "manifest_write_failed"})
            _rss1 = _proc_rss_gib()
            _log.info("MD化が%sしました: %s（%.1f秒%s）",
                      "失敗" if rel_unhandled else _done_label, rel, time.monotonic() - _t0,
                      f"・RSS {_rss0:.1f}G→{_rss1:.1f}G" if _rss0 is not None and _rss1 is not None else "")
            processed_candidates += 1
            if progress is not None:
                progress(processed_candidates, candidate_total)
    # per-file ループを最後まで走り切った（＝完走した）ときだけ剪定/上限適用する（`_conv_cache_prune`
    # docstring 参照）。ここへ到達しない途中死（プロセス強制終了等）では呼ばれず、まだ生きている
    # rel のキャッシュは次回 sync でそのまま再利用できる（進捗を保持する）。
    _conv_cache_prune(conv_cache_root, conv_cache_seen_rels)
    _conv_cache_enforce_cap(conv_cache_root)
    _write_arms_sig_marker(dr)                           # この派生を作った時のアーム構成を刻む（後の drift 判定用）
    if document_ir_failed == 0:                          # 全 IR が正常に書けた時だけ IR 版マーカーを刻む（修正3/4）
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


def refresh_document_ir(wd, derived, *, write_document_ir_sig_marker: bool = True) -> dict:
    """**IR だけ**の軽量再生成（DOC-IR-001.5・修正3）。`build_derived` と違い derived を全消去しない
    （MD／meta.json／`.arms_sig` には一切触れない＝IR 版だけの更新で全再ビルドを誘発しない）。

    `wd` を `scope_infer.safe_files` で歩き、原本が素の `.docx`/`.pptx`/`.xlsx`（B1 と同じ理由で旧形式は
    対象外＝`build_derived` の B1 コメント参照）**かつ** 対応する `derived/{rel}.md` が既に在る文書だけを
    拡張子に応じて `ooxml_arm._build_docx_ir`/`_build_pptx_ir`/`_build_xlsx_ir` で再構築し
    `write_text_atomic` で原子書込する（MD 自体が無い＝未対応/未変換な文書は対象外）。対応する原本が無く
    なった stale な
    `*.document.json`（鏡＝原本が消えたら派生も消える）は削除する。**マーカー（`.document_ir_sig`）は
    世界単位で1つ**＝docx/pptx/xlsx のどれか1つの抽出器版だけが上がった場合でも、対象拡張子だけに
    限定せず対象の OOXML 文書を全件（この関数のスコープ内で）再生成する。

    返値 `{document_ir_generated, document_ir_failed, document_ir_failures:[{doc, reason}]}`。
    **全件成功（`document_ir_failed == 0`）の時だけ** `.document_ir_sig` マーカーを更新する（失敗が残れば
    次回 sync も drift のまま＝再試行される）。`write_document_ir_sig_marker=False`
    （`worker.sync` の document_ir→evidence/rag 連鎖の契約・`refresh_evidence_ir` の
    `write_rag_sig_marker` と同型）は、`document_ir_failed == 0` でもこの場では確定せず、
    呼び出し元が連鎖した evidence/rag（さらに RAG_ES 有効時は ES 反映）の成功まで確認してから
    `write_document_ir_sig_marker()` を呼んで確定する（先に確定すると、連鎖が失敗した時に
    再試行の入口＝document_ir drift の検知そのものを失う）。
    1文書ごとの想定内の失敗は本関数内で個別に捕捉し `document_ir_failed` 等へ計上する
    （best-effort）が、それ以外の想定外の例外は捕捉せず呼び出し元（`worker.sync`）へ伝播する
    （`worker.sync` 自身もこれを握り潰さない）。
    """
    from .. import scope_infer as si
    from . import document_ir
    from .arms import ooxml_arm
    wd = Path(wd).resolve()
    try:
        dr = Path(derived).resolve()
    except OSError:
        dr = Path(derived)
    dr_ir = _sibling_layer_dir(dr, "ir")           # `.document.json` は ir 層（§8.1 三階層）
    # `build_derived` と同じ重なりガード（RV High #2）: derived がソース配下と重なる/同一だと、ソース木への
    # `.document.json` 書込と stale 一掃 unlink がそのまま READ-ONLY source の汚染・削除になる。書く前に拒否。
    if _within(dr, wd) or _within(wd, dr) or _within(dr_ir, wd) or _within(wd, dr_ir):
        return {"document_ir_generated": 0, "document_ir_failed": 0, "document_ir_failures": [],
                "error": "derived_overlaps_source"}
    generated = failed = 0
    failures: list[dict] = []
    seen_ir: set[str] = set()
    for rp, rel in si.safe_files(wd):
        ext = rp.suffix.lower()
        if ext not in ooxml_arm._IR_EXTS:
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
# `{rel}.derived.json` は原本ごとに、検索 consumer 側の設定（例: ES への RAG 反映の有無）に
# 関係なく常時生成する（=diskを消費する。無効化しても書かれなくなるものではない）。
# マニフェスト形式のバージョン（`sidecars`/`assets` キーの構成が変わったら上げる）。現行値と
# 一致しないマニフェスト（キー自体を持たない旧世代を含む）は `rag_sidecars_missing` が
# 「欠落」として扱い、全再構築で書き直させる（形式移行の自己修復）。
# 運用上の注意: このバージョンを上げた直後の最初の手動 sync は、原本1件ごとに旧マニフェストが
# 「欠落」判定になるため、検索 consumer のフラグ（ES への RAG 反映の有無等）が OFF の world でも
# 全原本分の全再構築（`run()`）が1回だけ走る＝所要時間は通常の差分更新でなく初回取り込み相当に
# なる（1回で安定する・以後は従来どおり差分のみ）。
_DERIVED_MANIFEST_SCHEMA_VERSION = "derived-sidecar-manifest-v1"
# マニフェストが記録しうる sidecar 種別（この5種のうち実際に書かれたものだけを列挙する）。
_MANIFEST_SIDECAR_SUFFIXES = (".md", ".md.meta.json", ".evidence.json", ".rag.md", ".rag_chunks.jsonl")


def _write_derived_sidecar_manifest(dr: Path, dr_rag: Path, dr_ir: Path, rel: str, *,
                                    human_md_sig: str | None = None) -> bool:
    """`rel` について実際に生成された sidecar 集合を記録する（sidecar 欠落検知の唯一の正本）。

    `dr`/`dr_rag`/`dr_ir`＝md/rag/ir 3層の物理ルート（§8.1 三階層）。マニフェスト自身は ir 層
    （`{rel}.derived.json`）に書くが、対象の sidecar は3層に分散するため存在確認は
    `_LAYER_FOR_SIDECAR_SUFFIX` で層を引いてから行う。

    どの sidecar が書かれるかは実行時条件（空/image-only・有効 arm・legacy backend 到達性・
    raster 等）で決定的に変わり、原本の拡張子だけから静的に導出できない。生成した側がその場で
    「実際に何を書いたか」をそのまま書き残すことで、この不一致を構造的に無くす。候補
    （`_MANIFEST_SIDECAR_SUFFIXES`）のうち現に存在するものだけを記録する（何も書かれなければ
    空リスト＝「この原本には何も期待しない」という正当な記録になる）。`{rel}.assets/`
    ディレクトリが存在する場合は中身のファイル名一覧も `assets` キーへ記録する（存在しなければ
    キー自体を省略する＝assets を一度も持たない rel のマニフェストを変えない）。現行の
    `_DERIVED_MANIFEST_SCHEMA_VERSION` も必ず書く（`rag_sidecars_missing` が形式移行を検知する
    唯一の手がかり）。

    `human_md_sig`: この rel の human_md（`OoxmlArm`・docx/xlsx）出力を**今回評価した**（`{rel}.md` を
    実際に書いた、または IR は構築できたが本文が無く意図的に書かなかったのいずれかを含む）時だけ
    呼び出し元が `_current_human_md_sig()` を渡す。`asset_versions.human_md` として書き、
    `human_md_sig_drift`/`refresh_human_md` がこの asset **だけ**の選択的な差分再生成判定に使う
    （`.rag.md`/`.evidence.json`/ES には一切触れない・RAG-KV の drift 連鎖と同じ単一 asset 版の
    考え方）。**`.md` sidecar の有無とは独立に記録する**（`.md` が無いのは「空だから書かなかった」
    正当な状態でもありうるため・`.md` が無い rel を `asset_versions` から除外すると、その rel は
    レンダラ版が上がるたびに毎回「未評価」として拾われ続け、`human_md_sig_drift` が二度と False に
    収束しなくなる）。省略時（None）はこの呼び出しが human_md を評価していない
    （`refresh_evidence_ir`/`refresh_rag` 等）ことを意味し、**既存マニフェストの
    `asset_versions.human_md` をそのまま引き継ぐ**（読めない/無ければ何も書かない＝キー自体を
    増やさない）。

    呼び出し元（`_build_derived_into_staging`/`refresh_evidence_ir`/`refresh_rag`）は、対象 rel
    の処理が正常に完了した（想定外の例外で終わっていない）場合にだけこれを呼ぶ——想定外の失敗で
    終わった rel は sidecar が不完全な途中状態のままになりうるため、その状態を正本として記録
    しない。戻り値: 書込に成功したら True。失敗（OSError。`.assets/` の列挙中の失敗も含む）した
    ら False——呼び出し元はこの rel を失敗として扱い公開を止める（書けなかったマニフェストを
    「対象外」と取り違えて次回 sidecar 欠落検知から漏らさないため）。
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


def rag_sidecars_missing(wd, derived) -> bool:
    """原本ごとに生成時マニフェスト（`_write_derived_sidecar_manifest`）を読み、そこに列挙された
    sidecar・asset が1つでも欠落していれば True。

    どの sidecar が生成されるかは実行時条件（空/image-only・有効 arm・legacy backend 到達性・
    raster 等）で決定的に変わり、原本の拡張子だけからは静的に導出できない。ここでは生成した側
    （`build_derived`/`refresh_evidence_ir`/`refresh_rag`）が実際に書いた sidecar の記録を
    そのまま照合するだけにする。マニフェスト自体が無い（旧世代・未生成・書込失敗・想定外の
    例外で処理が終わった）rel も欠落扱いとする——1回だけ `worker.sync()` の全再構築
    フォールバックを誘発し、`build_derived` がその rel のマニフェストを新たに書く（自己修復）。

    マニフェストの `schema` が現行の `_DERIVED_MANIFEST_SCHEMA_VERSION` と一致しない（キー自体が
    無い旧世代を含む）場合も欠落扱いとする——マニフェスト形式そのものが変わった（例: `assets`
    キーの追加）場合、旧形式のマニフェストは新しい検査基準に対して「揃っているかどうか判定
    不能」なので、安全側に倒して1回だけ全再構築へ回し、現行形式で書き直させる。

    残余リスク（受容）: 該当原本の Evidence 生成が恒久的に失敗し続ける場合、この検査は毎回
    True を返し続け、sync のたびに全再構築（`run()`）へフォールバックし続ける。閉域LAN・
    単一worker の非機能前提ではこれを受容する（該当原本自体の恒久失敗が要対応の異常であり、
    sidecar 検査の欠陥ではない）。
    """
    from .. import scope_infer as si

    wd = Path(wd).resolve()
    dr = Path(derived)
    dr_rag = _sibling_layer_dir(dr, "rag")
    dr_ir = _sibling_layer_dir(dr, "ir")
    roots = {"md": dr, "rag": dr_rag, "ir": dr_ir}
    # build_derived が候補として実際に処理しうる拡張子とだけ突き合わせる（それ以外の原本＝
    # 設計書/ソースコード等は最初からマニフェストを持たない対象外であり、無いことは欠落ではない）。
    manifest_candidates = OFFICE_EXT | RASTER_EVIDENCE_EXT | convertible_exts()
    for rp, rel in si.safe_files(wd):
        if rp.suffix.lower() not in manifest_candidates:
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
    """XLSX/DOCX/PPTX/PDF Evidence IRをclone済みgeneration上で軽量再生成する（E2b/E4）。

    原本が素の``.xlsx``/``.docx``で、既存MDのprovenanceが``ooxml``の文書だけを対象にする。旧形式から変換した
    一時OOXML出力へ誤ったsource hashを付けない。対応原本が消えた、または担当armが
    変わったstale ``*.evidence.json``は削除する。全件成功時だけ``.evidence_ir_sig``を更新する。

    `write_rag_sig_marker=False`（RAG_ES 有効時の呼び出し契約）は「成功時に自ら `.rag_sig` を
    確定しない」だけでなく、生成処理を始める**前**に既存 `.rag_sig` を明示的に未確定へ戻す
    （マーカー保留方式・呼び出し元が ES 反映の成否を確認してから確定する）。削除に失敗したら
    生成を開始せず `error` を返す。

    `world`（O1）は `_build_derived_into_staging` docstring 参照——公開中の OCR 観測を VLM と
    合流して rag.md へ含める。
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
    obs_dir = _resolve_ocr_observation_dir(world)          # 1回だけ解決（O1）
    ocr_observation_marker = _ocr_observation_marker_for(obs_dir)
    for rp, rel in si.safe_files(wd):
        if rp.suffix.lower() not in EVIDENCE_EXT:
            continue
        ext = rp.suffix.lower()
        md_path = dr / (rel + ".md")
        meta = json_io.read_json(dr / (rel + ".md.meta.json"), default=None)
        source_failure_notice = _is_source_failure_notice(meta)
        if ext == ".pdf":
            # image-only PDFは旧MDが空でもEvidence/RAGを正本画像と構造から再生成する。
            # full generationでparse failure noticeへ縮退済みなら、backendの現在値に左右されず
            # notice chainを現行schemaへ再生成する。
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
            # 空/image-only な正常 OOXML は legacy `.md` を持たない正当なケースがある。`.md` の
            # 有無だけで対象外と決めず、生成時マニフェストが以前 `.evidence.json` を記録して
            # いれば regeneration 対象として扱う（`.md` が本当に無い＝未対応/未処理の場合だけ
            # マニフェストにも `.evidence.json` が無く、正しく対象外のままになる）。
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
                    extracted, source_name=rel, observation_set=observation_set)
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
        # この rel の処理が正常に完了した時だけ、実際に書かれた sidecar をマニフェスト化する
        # （途中で失敗した rel は不完全な途中状態を正本として記録しない）。マニフェスト自体の
        # 書込に失敗した場合も rag_failed へ計上し、`.rag_sig` マーカーの確定を防ぐ。
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
    """既存Evidence世代と同じ原本からpipe-free Markdown/chunk/assetsを再生成する（E3）。

    `write_rag_sig_marker=False` の契約は `refresh_evidence_ir` と同じ（マーカー保留方式・
    生成開始前に `.rag_sig` を明示的に未確定へ戻す）。

    `world`（O1）は `_build_derived_into_staging` docstring 参照——OCR 完了後の「追いつき」
    再生成はこの関数（`rag_sig_drift` の OCR 観測次元が誘発）が担う。
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
        rel: rp for rp, rel in si.safe_files(wd)
        if rp.suffix.lower() in EVIDENCE_EXT
    }
    generated = failed = 0
    failures: list[dict] = []
    seen: set[str] = set()
    legacy_cache = legacy_convert.cache_root_for(dr)
    obs_dir = _resolve_ocr_observation_dir(world)          # 1回だけ解決（O1）
    ocr_observation_marker = _ocr_observation_marker_for(obs_dir)
    for evidence_path in sorted(dr_ir.rglob("*.evidence.json")):
        rel = evidence_path.relative_to(dr_ir).as_posix()[: -len(".evidence.json")]
        source_path = sources.get(rel)
        if source_path is None:
            failed += 1
            failures.append({"doc": rel, "reason": "source_missing"})
            continue
        seen.add(rel)
        rel_ok = True
        try:
            # 巨大Evidence JSONの全量read/json.loadsを避け、同じ原本と固定parserから再構築する。
            # この経路はevidence_sigが一致する時だけworkerから呼ばれるため、既存Evidenceと決定的に等価。
            actual = source_path
            conversion = None
            meta = json_io.read_json(dr / (rel + ".md.meta.json"), default=None)
            if _is_source_failure_notice(meta):
                # Evidence sigは一致済みでRAG rendererだけがdriftした経路。巨大な通常Evidenceは
                # 原本から再構築する一方、このsource-level noticeは小さい既存IRを正本として読む。
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
            if actual.suffix.lower() not in LEGACY_OFFICE_EXT:
                _extract_evidence_assets(source_path, actual, ir, asset_dir)
                observation_set = _build_observation_set(ir, rel, asset_dir, obs_dir=obs_dir)
            rendered = evidence_render.render(ir, source_name=rel, observation_set=observation_set)
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
        # この rel の処理が正常に完了した時だけ、実際に書かれた sidecar をマニフェスト化する
        # （途中で失敗した rel は不完全な途中状態を正本として記録しない）。マニフェスト自体の
        # 書込に失敗した場合も failed へ計上し、`.rag_sig` マーカーの確定を防ぐ。
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
    """有効アームのうち**最初に受理したアーム1本**で変換（A1＝単一アーム選択）。

    返値 `(arm_name, ArmResult|None)`。受理アームが無ければ `(None, None)`。
    """
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

    **決定的**（タイムスタンプを含めない・キー順固定＝`sort_keys`）。derived 全消去→再作成の鏡動作に相乗り
    （毎回作り直す）。es_index がこのサイドカーを読んでチャンクメタ（extraction_method/confidence）へ搬送する。
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
    """DOCX の人間向け MD（H2・`docs/proposals/2026-08-28-人間向けMDの刷新.md` §3.2）。

    document-ir（`arms/ooxml_arm._build_docx_ir`）を共通土台にし、`human_md.render_docx` へ委譲する
    （結合セル・ネスト表の展開は `_docx_table_walk` の解決結果をそのまま使う＝独自の簡易パーサは
    持たない）。IR 構築に失敗すれば未対応（None・fail-safe）。
    """
    from . import human_md
    from .arms import ooxml_arm
    ir = ooxml_arm._build_docx_ir(p)
    return human_md.render_docx(ir) if ir is not None else None


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
    out = []
    with zipfile.ZipFile(p) as z:
        for i, n in enumerate(_slide_order(z), 1):
            root = ET.fromstring(z.read(n))
            texts = _pptx_slide_texts(root)
            if texts:
                out.append(f"## スライド {i}")
                out.extend(texts)
    return "\n\n".join(out) if out else None


# ---- A5: 幾何オクルージョン（覆い図形の座標判定・pptx v1・§5.5 の簡略版）----
#
# 現行契約 docs/11-Office変換.md §5.5／起案経緯 docs/archive/proposals/2026-07-07-MD化多アーム統合.md A5。
# 2026-07-12 ユーザー決定＝簡略版: 人手確認UI（A17）は作らず、覆い図形に隠されたテキストへ
# MD本文に直接マーカー行を自動出力するだけ（メタタグ `status=hidden_candidate` 方式ではない・
# D3=自動公開の原則と整合）。対象は .pptx のみ（座標が EMU で明確）。属性ベースの隠し
# （w:vanish・veryHidden シート・非表示スライド等）は今回は対象外（未実装の残課題）。
_HIDDEN_MARKER = "**［隠し候補：前面の図形に覆われた文字］**"
_OCCLUSION_RATIO = 0.9                                        # 交差面積÷テキストshape面積のしきい値


def _pptx_slide_texts(root) -> list[str]:
    """1スライド分のテキスト行（shape 単位・文書順・A5 の隠し候補マーカー付き）。

    shape 単位の歩行ロジックがどんな理由で失敗しても（未知の構造・想定外の要素等）、例外を握って
    現行と全く同じフラット抽出（`root.iter(f"{_A}t")`）にフォールバックする（テキストの取りこぼし
    を絶対に起こさない・保守的に倒す）。非隠しスライドは shape 単位歩行でも現行実装とバイト単位で
    一致する（spTree の文書順走査は `root.iter` と同じ順序でテキストを拾うため）。
    """
    try:
        return _pptx_slide_texts_by_shape(root)
    except Exception:
        return _pptx_slide_texts_flat(root)


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


def _pptx_slide_texts_by_shape(root) -> list[str]:
    """`p:cSld/p:spTree` 直下を文書順（=z順・背面→前面）で歩き、shape 単位でテキストを取る。

    A5: 各テキスト shape について、それより後（前面）にある occluder 候補（無地塗りの空shape／画像）と
    bbox の交差比が閾値以上なら「隠し候補」とマークし、直前に独立行のマーカーを出す。
    `p:grpSp`（グループ）内は幾何判定せず、テキストのみ再帰抽出する。
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
        if not entry["has_text"]:
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
    """XLSX の人間向け MD（H2・`docs/proposals/2026-08-28-人間向けMDの刷新.md` §3.1）。

    document-ir（`arms/ooxml_arm._build_xlsx_ir`）を共通土台にし、`human_md.render_xlsx` へ委譲する
    （シート丸ごと1枚の打切り付きパイプ表ではなく、`ooxml/excel.py::regions()` が検出する表候補
    ごとに見出し＋パイプ表を出す＝独自の簡易パーサは持たない）。IR 構築に失敗すれば未対応
    （None・fail-safe）。
    """
    from . import human_md
    from .arms import ooxml_arm
    ir = ooxml_arm._build_xlsx_ir(p)
    return human_md.render_xlsx(ir) if ir is not None else None


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

    `_pdf_pages` は暗号化 PDF も本文ゼロの `[]` へ丸めてしまい、呼び出し元
    （`_build_derived_into_staging` の一般失敗分岐）が理由を失う。ここは判定専用の軽い呼び出しで、
    バックエンドが `pdfminer`（暗号化判定 API を持たない）や未導入の場合は判定不能＝False
    （fail-safe・「分からない」を「暗号化されている」と決め付けない）。
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


# ---- PDF ティア制（§5「PDF は一律解なし」）＝テキスト層の品質で担当アームを決定的に選ぶ ----
_DEFAULT_PDF_TEXT_MIN_CHARS = 30                             # ページあたり平均抽出文字数のしきい値（保守的）


def _pdf_text_min_chars() -> int:
    """PDF テキスト層「十分」の判定しきい値（env `SHERPA_PDF_TEXT_MIN_CHARS`・ページあたり平均文字数）。

    この値は診断用の good/sparse 分類に使う。テキストが存在するPDFは、sparseでも決定的なpdf_textを継続する。
    不正/未設定は保守的な既定（30）。0 以下は常に good とみなす。
    """
    raw = os.environ.get("SHERPA_PDF_TEXT_MIN_CHARS")
    if not raw:
        return _DEFAULT_PDF_TEXT_MIN_CHARS
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_PDF_TEXT_MIN_CHARS


# 品質判定のメモ化（RV Med #4）: `accepts()` は pdf_text/vision の複数アームから同一 PDF に
# 対して繰り返し呼ばれる（`_convert_with_arms` が最初に受理したアームを探すため最大アーム数回呼ぶ）ため、抽出
# （`_pdf_pages`）を毎回やり直すと PDF 1件あたり複数回の全文抽出が走ってしまう。`(resolved path,size,mtime_ns)`
# キーでプロセス内メモ化し、1ファイル1回に抑える。上限超過は全消し（シンプルさ優先・LRU化はしない）。
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
    """PDF テキスト層の品質（good/sparse/empty）とページ平均抽出文字数を計算する（メモ化・fail-safe）。

    **fail-safe（RV High #1）**: `_pdf_pages`（壊れた PDF 等で例外を投げうる）を例外安全に包み、失敗時は
    `("empty", 0.0)` を返す。ここで例外を握らないと `accepts()` 経由で `build_derived` の1ファイルループ全体が
    中断し、1件の壊れた PDF が他ファイルの変換まで巻き込んで取り込み全体を止めてしまう。
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
            min_chars = _pdf_text_min_chars()
            quality = "sparse" if (min_chars > 0 and avg < min_chars) else "good"
            result = (quality, avg)
    if key is not None:
        if len(_PDF_QUALITY_CACHE) >= _PDF_QUALITY_CACHE_MAX:
            _PDF_QUALITY_CACHE.clear()                        # 上限超過は全消し
        _PDF_QUALITY_CACHE[key] = result
    return result


def pdf_text_quality(p) -> str:
    """PDF テキスト層の品質を決定的に判定: "good"（十分）/"sparse"（少ない）/"empty"（テキスト層ゼロ）。

    バックエンド（pypdf/pdfminer）で全ページ抽出→整形後の総文字数とページ平均で分類する。ヒューリスティク
    だが同一入力・同一バックエンドでは安定（決定的）。バックエンド未導入・**抽出例外（壊れた PDF 等）は測定
    不能＝"empty" 扱い**（fail-safe・呼び出し側の `pdf_escalation_target` が上位アームへ委ねる）。
    プロセス内メモ化（`_pdf_quality_and_avg`）を経由する。
    """
    return _pdf_quality_and_avg(p)[0]


def pdf_escalation_target(p) -> str | None:
    """テキスト層ゼロの PDF を任意の vision に回すべきか。回すなら `"vision"`、それ以外は None を返す。

    決定的なティア判定を1箇所に集約し、各 PDF アームの `accepts` はこの結果を参照する（登録順に依存しない）:
    - pdf_text.accepts → target is None
    - vision.accepts（PDF 分岐）→ target == "vision"

    判定表:
    - good（十分）→ None（pdf_text 続投）
    - sparse（少ないが有る）→ None（原本に存在するテキスト層を決定的に抽出し、AI解釈を足さない）
    - empty（テキスト層ゼロまたは抽出不能）→ visionが実効利用可能なら `"vision"`、無ければ None

    既定（`ooxml,pdf_text`）では vision が無効＝常に None を返す（＝pdf_text が全 PDF を担当・従来どおり）。
    tesseract 直の `ocr` アームは撤去した（2026-07-08・視覚読み取りは vision に一本化）。
    """
    if Path(p).suffix.lower() != ".pdf":
        return None
    from . import arms as _arms
    names = set(_arms.enabled_arm_names())
    if not _vision_pdf_ready(names):
        return None                                          # vision無し＝pdf_text（有効なら）が担当
    return "vision" if pdf_text_quality(p) == "empty" else None
