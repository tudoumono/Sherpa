"""vision アームが単独の TIFF（CCITT 圧縮・複数ページを含む）を VLM で読めること。

VLM へ送る画像は PNG・JPEG に限る（TIFF をそのまま送ると読み取りに失敗し、変換の失敗の知らせになる）。
実 VLM は呼ばない（送信の境界 `vision_arm._read_ollama` を差し替え、送られた画像の形式を記録する）。
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from PIL import Image, ImageDraw

from sherpa.ingest.arms import vision_arm

_CFG = {"provider": "ollama", "model": "qwen2.5vl", "ollama_url": "http://localhost:11434", "cloud_allowed": False}


def _page(label: str) -> Image.Image:
    image = Image.new("1", (600, 200), 1)
    ImageDraw.Draw(image).text((40, 80), label, fill=0)
    return image


def _record_sent(monkeypatch) -> list[bytes]:
    sent: list[bytes] = []

    def _fake_read_ollama(image_path, cfg, timeout):
        data = Path(image_path).read_bytes()
        sent.append(data)
        return f"読み取り{len(sent)}"

    monkeypatch.setattr(vision_arm, "resolve_vlm", lambda: dict(_CFG))
    monkeypatch.setattr(vision_arm, "_read_ollama", _fake_read_ollama)
    return sent


def test_ccitt_g4_tiff_is_sent_as_png(tmp_path, monkeypatch):
    sent = _record_sent(monkeypatch)
    src = tmp_path / "scan.tif"
    _page("FAX 1").save(src, compression="group4")

    result = vision_arm.VisionArm().convert(src)

    assert result is not None and "読み取り1" in result.md
    assert len(sent) == 1 and sent[0].startswith(b"\x89PNG\r\n\x1a\n")


def test_multi_page_tiff_reads_each_page(tmp_path, monkeypatch):
    sent = _record_sent(monkeypatch)
    src = tmp_path / "fax.tiff"
    first, *rest = [_page(f"PAGE {n}") for n in (1, 2, 3)]
    first.save(src, compression="group4", save_all=True, append_images=rest)

    result = vision_arm.VisionArm().convert(src)

    assert result is not None
    assert [line for line in result.md.splitlines() if line.startswith("## ページ")] == [
        "## ページ 1", "## ページ 2", "## ページ 3"]
    assert len(sent) == 3 and all(data.startswith(b"\x89PNG") for data in sent)
    assert "pages=3" in result.notes and "image_pages_total=3" in result.notes


def test_png_is_sent_unchanged(tmp_path, monkeypatch):
    sent = _record_sent(monkeypatch)
    src = tmp_path / "a.png"
    _page("PNG").save(src)

    assert vision_arm.VisionArm().convert(src) is not None
    assert sent == [src.read_bytes()]


def test_unreadable_tiff_is_not_sent(tmp_path, monkeypatch):
    sent = _record_sent(monkeypatch)
    src = tmp_path / "broken.tif"
    src.write_bytes(b"II*\x00not a tiff")

    assert vision_arm.VisionArm().convert(src) is None
    assert sent == []
