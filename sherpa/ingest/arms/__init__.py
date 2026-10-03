"""MD化アームのプラグイン基盤。アーム（arm）＝1つの文書を Markdown 化する変換ルート1本。プロトコル（`Arm`）・結果 dataclass（`ArmResult`）・設定駆動レジストリ（`system_settings.arms_enabled`・管理画面「取り込み」）を持つ。

既定（`ooxml,pdf_text`）は `office_md` の決定的変換へ委譲する。未知のアーム名は警告して無視する。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# 実 import にする（`TYPE_CHECKING` ガードにしない）: 実行時に `typing.get_type_hints(ArmResult)` で注釈を解決する利用者のため。`document_ir` は stdlib のみの純データ型で循環しない。
from ..document_ir import DocumentIR

_log = logging.getLogger(__name__)

# 既定の有効アーム（OOXML＋PDF テキスト）。
DEFAULT_ARMS: tuple[str, ...] = ("ooxml", "pdf_text")


@dataclass
class ArmResult:
    """1アームの変換結果。`md=None`＝このアームでは変換できない/失敗（未対応表示に倒す）。

    `method`＝変換手法名（例 "ooxml"/"pdf_text"/"vision"）。`confidence`＝0.0〜1.0。`notes`＝来歴の補足（バックエンド名等）。`document`＝document-ir-v1 の並行生成結果。md が正で、IR は付随情報（未対応アーム/形式では `None`）。
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

    既定以外の `vision` は登録済みだが既定では無効（有効化は管理画面）。
    """
    from . import vision_arm, ooxml_arm, pdf_text_arm
    return {"ooxml": ooxml_arm.OoxmlArm(), "pdf_text": pdf_text_arm.PdfTextArm(),
            "vision": vision_arm.VisionArm()}


def arm_availability() -> dict[str, bool]:
    """既知アームごとの「この環境で実際に使えるか」（未導入アームの UI 案内用）。

    アームが `available()` を持てばそれを、無ければ常時利用可（ooxml）とみなす。判定中の例外は「利用不可」に倒す。
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
    """順序を保ったまま重複除去する。"""
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _env_configured_names() -> list[str]:
    """MCP サブプロセスへ親が渡す実効アームのスナップショット（env `SHERPA_MCP_ARMS`・カンマ区切り）による有効アーム名（順序保持・重複除去）。無ければ既定。

    system_settings を見ない解決（管理画面の「既定へ戻すと何になるか」表示や、system_settings が読めない文脈＝MCP サブプロセスのフォールバック用）。
    """
    raw = os.environ.get("SHERPA_MCP_ARMS")
    names = list(DEFAULT_ARMS) if raw is None else [n.strip() for n in raw.split(",") if n.strip()]
    return _dedup(names)


def _system_arms_enabled() -> list[str] | None:
    """全体設定（system_settings）の `arms_enabled`（有効なら list[str]）。

    優先順 system_settings > MCP スナップショット > 既定の最上位。未設定・空リスト・不正値は None（呼び出し側がスナップショット/既定へフォールバックする）。store を読めない文脈（MCP サブプロセス・DB 停止中）では例外を握って None を返す。
    """
    try:
        from sherpa import store
        val = store.get_system_settings().get("arms_enabled")
    except Exception:
        return None
    if isinstance(val, list):
        names = [str(n).strip() for n in val if str(n).strip()]
        return names or None  # 空リストは「未設定」扱い＝env へ
    return None


def _configured_names() -> list[str]:
    """有効アーム名（順序保持・重複除去）。優先順は system_settings > MCP スナップショット > 既定。既知/未知の選別は呼び出し側（`enabled_arm_names`）が行う。"""
    sys_names = _system_arms_enabled()
    names = sys_names if sys_names is not None else _env_configured_names()
    return _dedup(names)


def known_arm_names() -> list[str]:
    """登録済み（既知）の全アーム名（ソート済み）。`PUT /admin/settings` の `arms_enabled` 検証・設定画面のチェックボックス描画に使う。"""
    return sorted(_registry())


def env_default_arm_names() -> list[str]:
    """system_settings を無視した既定の実効アーム名（既知名のみ）。設定画面で「未設定に戻すと何が有効になるか」を示すために使う。"""
    reg = _registry()
    return [n for n in _env_configured_names() if n in reg]


_warned_unknown: set[str] = set()  # 同じ未知名の警告はプロセス内1回だけ


def enabled_arm_names() -> list[str]:
    """有効かつ既知のアーム名（指定順）。未知名は警告して除外する。"""
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
        _log.warning("未知アームを無視します: %s（既知: %s）",
                     ",".join(unknown), ",".join(sorted(reg)))
    return out


def enabled_arms() -> list[Arm]:
    """有効なアームのインスタンス列（指定順）。未知名は警告して無視する。"""
    reg = _registry()
    return [reg[name] for name in enabled_arm_names()]
