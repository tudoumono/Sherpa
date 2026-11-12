"""OCR の検出・推論・入力の上限の設定（環境変数）と、モデルの組。

設計: docs/design/rag.md「OCR（非同期・隔離ワーカー）」
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

from ..env_int import env_int

_log = logging.getLogger("sherpa")

DET_LIMIT_TYPES = frozenset({"max", "min"})


def max_image_side() -> int:
    """推論へ渡す画像の長辺上限（env `SHERPA_OCR_MAX_IMAGE_SIDE`・範囲外/不正は既定）。"""
    return env_int("SHERPA_OCR_MAX_IMAGE_SIDE", 2560, 256, 16384)


def max_image_pixels() -> int:
    """推論へ渡す画像の総画素数上限（env `SHERPA_OCR_MAX_IMAGE_PIXELS`・範囲外/不正は既定）。"""
    return env_int("SHERPA_OCR_MAX_IMAGE_PIXELS", 6_000_000, 262_144, 100_000_000)


@dataclass(frozen=True)
class OCRModelSet:
    detection: str
    recognition: str
    # model 本体だけの tree hash（取得メタデータ `.cache/` は除く）。docker/ocr-models.lock.json と一致させる
    detection_tree_sha256: str
    recognition_tree_sha256: str


DEFAULT_OCR_MODEL = "small"
OCR_MODEL_SETS: dict[str, OCRModelSet] = {
    "medium": OCRModelSet(
        "PP-OCRv6_medium_det", "PP-OCRv6_medium_rec",
        "f74ebd70d463d5fe627dd1d0d235bc5097acda0ff1c05f527c363054bd6975ee",
        "a0e6515f7e4c2745c07de72ffa867836f68a7aec988989754b8afb0d019bf3db",
    ),
    "small": OCRModelSet(
        "PP-OCRv6_small_det", "PP-OCRv6_small_rec",
        "ed4253a10d4eacc4102967902901c732b6326e09a290987a1591c8364bdb9103",
        "36ba134a762676f41fbd47863b43a040a2ed84c2af6058fb6917871d37ad62ba",
    ),
    "mobile": OCRModelSet(
        "PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec",
        "ebf54c9e3df9de9cb0bb724e3dd3319789592c95cd557b2bf65e73da8f20c0d6",
        "2dfaa35e17a5e103c828e33a012050f970ebcaf573df773a1c8214c2c263e989",
    ),
}


@lru_cache(maxsize=16)
def model_set_for(raw: str) -> OCRModelSet:
    key = raw.strip().lower()
    if not key:
        return OCR_MODEL_SETS[DEFAULT_OCR_MODEL]
    if key not in OCR_MODEL_SETS:
        _log.warning("SHERPA_OCR_MODEL=%r は未知の値です（%s のいずれか）。%s を使います",
                     raw, "・".join(OCR_MODEL_SETS), DEFAULT_OCR_MODEL)
        return OCR_MODEL_SETS[DEFAULT_OCR_MODEL]
    return OCR_MODEL_SETS[key]


def model_set() -> OCRModelSet:
    """使うモデルの組（env `SHERPA_OCR_MODEL`・未設定/未知は既定の small）。"""
    return model_set_for(os.environ.get("SHERPA_OCR_MODEL", ""))


def det_limit_type() -> str:
    """検出の上限の向き（env `SHERPA_OCR_DET_LIMIT_TYPE`・max か min・不正は max）。"""
    value = os.environ.get("SHERPA_OCR_DET_LIMIT_TYPE", "").strip().lower()
    return value if value in DET_LIMIT_TYPES else "max"


def det_limit_side_len() -> int:
    """検出の上限の画素数（env `SHERPA_OCR_DET_LIMIT_SIDE_LEN`・既定 1280）。"""
    return env_int("SHERPA_OCR_DET_LIMIT_SIDE_LEN", 1280, 64, 8192)


def rec_batch_size() -> int:
    """認識のバッチ数（env `SHERPA_OCR_REC_BATCH_SIZE`・既定 1）。"""
    return env_int("SHERPA_OCR_REC_BATCH_SIZE", 1, 1, 64)


def max_input_pixels() -> int:
    """入力の画素数の上限（env `SHERPA_OCR_MAX_PIXELS`・既定 4000 万）。"""
    return env_int("SHERPA_OCR_MAX_PIXELS", 40_000_000, 1_000_000, 1_000_000_000)


def max_input_bytes() -> int:
    """入力のバイト数の上限（env `SHERPA_OCR_MAX_INPUT_BYTES`・既定 64MiB）。"""
    return env_int("SHERPA_OCR_MAX_INPUT_BYTES", 64 * 1024 * 1024, 1024 * 1024, 2 * 1024 ** 3)


def onednn() -> bool:
    """oneDNN（PaddleOCR の enable_mkldnn）を使うか（env `SHERPA_OCR_ONEDNN`・0 か 1・不正は 0）。"""
    return env_int("SHERPA_OCR_ONEDNN", 0, 0, 1) == 1


def cpu_threads() -> int:
    """推論スレッド数（env `SHERPA_OCR_THREADS`・0＝未指定で Paddle の既定）。割り振りは mem_budget.sh が決める。"""
    return env_int("SHERPA_OCR_THREADS", 0, 0, 256)
