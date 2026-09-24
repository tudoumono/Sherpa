"""MD化アームのプラグイン基盤（正典 docs/11-Office変換.md §5.6/§5.7・起案 docs/archive/proposals/2026-07-07-MD化多アーム統合.md A1）。

**アーム（arm）** ＝1つの文書を Markdown 化する変換ルート（手法）の1本。複数アームを差し替え可能に
（プラグイン式）する。A1 は**この器だけ**を作る:
プロトコル・結果 dataclass・設定駆動レジストリ（system_settings `arms_enabled`・管理画面「取り込み」）。
既存の OOXML/PDF 変換を挙動不変で包む。

既定（`ooxml,pdf_text`）では従来と完全に同一の挙動（`office_md` の決定的変換へ委譲）。未知のアーム名は
警告して無視する（fail-safe）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# 実 import（TYPE_CHECKING ガードにしない）: `document_ir` は stdlib のみに依存する純データ型モジュールで
# 循環 import が無く、実行時に `typing.get_type_hints(ArmResult)` で注釈を解決する利用者を壊さないため
# （RV2巡目 Low: TYPE_CHECKING ガードだと NameError になる）。
from ..document_ir import DocumentIR

_log = logging.getLogger(__name__)

# 既定の有効アーム（導入しても現行と挙動が変わらない構成・INGEST-MD §5.6 の①OOXML＋④PDFテキスト）。
DEFAULT_ARMS: tuple[str, ...] = ("ooxml", "pdf_text")


@dataclass
class ArmResult:
    """1アームの変換結果。`md=None`＝このアームでは変換できない/失敗（fail-safe・未対応表示に倒す）。

    `method`＝変換手法名（例 "ooxml"/"pdf_text"/"vision"）。`confidence`＝0.0〜1.0。
    `notes`＝来歴の補足（バックエンド名等）。
    `document`＝document-ir-v1（文書標準構造）の並行生成結果（DOC-IR-001）。md が正のまま（検索/ES/grep/台帳/
    削除経路は不変）＝IR は付随情報であり、未対応アーム/形式では `None`（既定・後方互換）。
    """
    md: str | None
    method: str
    confidence: float
    notes: list[str] = field(default_factory=list)
    document: "DocumentIR | None" = None


@runtime_checkable
class Arm(Protocol):
    """変換アームのプロトコル（`name`／`accepts`／`convert`）。ステートレス実装を前提とする。"""
    name: str

    def accepts(self, path) -> bool:
        """このアームがこの入力を担当できる拡張子/形式か（実変換の可否ではない・軽量判定）。"""
        ...

    def convert(self, path) -> ArmResult | None:
        """`path` を Markdown 化して `ArmResult` を返す。変換不能/失敗は `None` または `md=None`。"""
        ...


def _registry() -> dict[str, Arm]:
    """名前 → アーム実装のマップ（アーム追加時にエントリを足す唯一の場所）。

    既定（DEFAULT_ARMS＝ooxml,pdf_text）以外の vision は登録済みだが**既定では無効**
    （有効化は `SHERPA_ARMS` か管理画面のチェックボックス）＝導入しても挙動は変わらない。
    tesseract 直の `ocr` アームは撤去した（2026-07-08・視覚読み取りは `vision`＝VLM に一本化）。
    """
    from . import vision_arm, ooxml_arm, pdf_text_arm
    return {"ooxml": ooxml_arm.OoxmlArm(), "pdf_text": pdf_text_arm.PdfTextArm(),
            "vision": vision_arm.VisionArm()}


def arm_availability() -> dict[str, bool]:
    """既知アームごとの「この環境で実際に使えるか」（未導入アームの UI 案内用・S1 の legacy_backend.available と同型）。

    アームが optional な可用性判定（`available()`）を持てば（pdf_text＝PDF 抽出バックエンド到達性・
    vision＝VLM 設定の実効可用性）それを、無ければ常時利用可（ooxml）とみなす。
    fail-safe: 判定中の例外は「利用不可」に倒す。
    """
    out: dict[str, bool] = {}
    for name, arm in _registry().items():
        avail = getattr(arm, "available", None)
        if not callable(avail):
            out[name] = True
            continue
        try:
            out[name] = bool(avail())
        except Exception:
            out[name] = False
    return out


def _dedup(names) -> list[str]:
    """順序を保ったまま重複除去（二重変換防止）。"""
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _env_configured_names() -> list[str]:
    """env `SHERPA_ARMS`（カンマ区切り）による有効アーム名（順序保持・重複除去）。未設定は既定。

    system_settings を見ない env/既定のみの解決（GET /admin/settings の「既定へ戻すと何になるか」表示や、
    system_settings が読めない/未設定の文脈でのフォールバックに使う）。
    """
    raw = os.environ.get("SHERPA_ARMS")
    names = list(DEFAULT_ARMS) if raw is None else [n.strip() for n in raw.split(",") if n.strip()]
    return _dedup(names)


def _system_arms_enabled() -> list[str] | None:
    """全体設定（system_settings）の `arms_enabled`（有効なら list[str]）。

    優先順 system_settings > env > 既定（docs/archive/proposals/2026-07-08-設定分離とUI整備.md S1）の最上位。
    未設定・空リスト・不正値は None を返し、呼び出し側は env/既定へフォールバックする。
    **fail-safe**: store を読めない文脈（MCP サブプロセスは PG creds を持たない・DB 停止中）では例外を
    握って None（＝env へ倒す）。取り込みホットループでの DB 打鍵は store 側の短TTLキャッシュで平滑化する。
    """
    try:
        from sherpa import store
        val = store.get_system_settings().get("arms_enabled")
    except Exception:
        return None
    if isinstance(val, list):
        names = [str(n).strip() for n in val if str(n).strip()]
        return names or None       # 空リストは「未設定」扱い＝env へ（S1 仕様）
    return None


def _configured_names() -> list[str]:
    """有効アーム名（順序保持・重複除去）。優先順は system_settings > env > 既定。

    system_settings の `arms_enabled`（非空 list）があればそれを最優先し、無ければ env `SHERPA_ARMS`
    （未設定は既定）へフォールバックする。既知/未知の選別は呼び出し側（`enabled_arm_names`）が行う。
    """
    sys_names = _system_arms_enabled()
    names = sys_names if sys_names is not None else _env_configured_names()
    return _dedup(names)


def known_arm_names() -> list[str]:
    """登録済み（既知）の全アーム名（ソート済み）。PUT /admin/settings の `arms_enabled` 検証・
    設定画面のチェックボックス描画に使う。"""
    return sorted(_registry())


def env_default_arm_names() -> list[str]:
    """system_settings を無視した env/既定の実効アーム名（既知名のみ）。設定画面で「未設定に戻すと
    何が有効になるか」を示すために使う（fail-safe: 未知名は除外）。"""
    reg = _registry()
    return [n for n in _env_configured_names() if n in reg]


_warned_unknown: set[str] = set()      # 同じ未知名の警告はプロセス内1回だけ（sync/scan 毎のスパム防止・RV Low）


def enabled_arm_names() -> list[str]:
    """有効かつ既知のアーム名（登録順ではなく `SHERPA_ARMS` の指定順）。未知名は警告して除外（fail-safe）。"""
    reg = _registry()
    out: list[str] = []
    unknown: list[str] = []
    for name in _configured_names():
        if name in reg:
            out.append(name)
        elif name not in _warned_unknown:
            unknown.append(name)
            _warned_unknown.add(name)
    if unknown:
        _log.warning("SHERPA_ARMS の未知アームを無視します: %s（既知: %s）",
                     ",".join(unknown), ",".join(sorted(reg)))
    return out


def enabled_arms() -> list[Arm]:
    """有効なアームのインスタンス列（`SHERPA_ARMS` の指定順）。未知名は警告して無視する（fail-safe）。"""
    reg = _registry()
    return [reg[name] for name in enabled_arm_names()]
