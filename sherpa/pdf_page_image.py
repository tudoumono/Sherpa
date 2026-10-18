"""PDF の 1 ページ（または一部の領域）を PNG に描いて、その資料の派生物の場所へ置く（pypdfium2・追加の依存なし）。
設計: docs/design/interfaces.md「MCP の道具」

呼び出し元（`parts/read/tools.py`）が doc_id の解決（範囲・秘匿・symlink・層）と、検査済みの原本を開いた fd の受け渡しを済ませる。
このモジュールは範囲・秘匿判定を知らない。描画は子プロセスで行う（時間切れは子を止める）。

- 置き場所: `{derived_rag_dir}/{doc_id}.pages/`。ファイル名は `p{ページ}.{原本 sha256 の先頭16桁}.d{dpi}.{領域}.{描画の版}.png`。
  原本が変われば sha が変わり別の名前になる（古い画像は返さず、次の描画時に消す）。
- 上限: 解像度 `DPI_MAX`・最長辺 `MAX_SIDE`・画素数 `MAX_PIXELS`（超えたら縮小して `downscaled`）・時間 `TIMEOUT_SEC`・メモリ・PNG の大きさ。1 回 1 ページ。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import uuid
import zlib
from pathlib import Path

RENDER_VERSION = "v1"
DPI_DEFAULT = 150
DPI_MIN = 36
DPI_MAX = 200
MAX_SIDE = 4000
MAX_PIXELS = 8_000_000
TIMEOUT_SEC = 30
MAX_PNG_BYTES = 10 * 1024 * 1024
CACHE_KEEP_MAX = 100
_MEM_LIMIT_BYTES = 4 * 1024 ** 3
_TMP_STALE_SEC = 600
_META_KEY = b"sherpa-pdf-page"
_NAME_RE = re.compile(r"^p(\d{4,})\.([0-9a-f]{16})\.d\d+\.[a-z0-9-]+\.v\d+\.png$")
_REPO_ROOT = Path(__file__).resolve().parents[1]


class RenderError(Exception):
    """描画できなかった理由（`code` は固定の短い識別子・`str(e)` は利用者向けの文）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def parse_region(raw) -> tuple[float, float, float, float] | None:
    """ページ内の相対座標（0〜1・左上が原点）の矩形 `[x0, y0, x1, y1]` を検証して返す。None・空は全面。不正は `RenderError`。"""
    if raw is None or raw == [] or raw == "":
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 4 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) for v in raw):
        raise RenderError("bad_region", "region は [x0, y0, x1, y1]（ページ内の相対座標 0〜1）で指定してください")
    x0, y0, x1, y1 = (round(min(1.0, max(0.0, float(v))), 4) for v in raw)
    if x1 - x0 < 0.02 or y1 - y0 < 0.02:
        raise RenderError("bad_region", "region が狭すぎます（x0<x1・y0<y1 で幅・高さとも 0.02 以上）")
    if (x0, y0, x1, y1) == (0.0, 0.0, 1.0, 1.0):
        return None
    return x0, y0, x1, y1


def clamp_dpi(raw) -> tuple[int, bool]:
    """解像度を `DPI_MIN`〜`DPI_MAX` に収める。戻り値 `(dpi, 上限・下限で丸めたか)`。数値でなければ `RenderError`。"""
    if raw is None:
        return DPI_DEFAULT, False
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise RenderError("bad_dpi", "dpi は数値で指定してください")
    dpi = round(raw)
    clamped = min(DPI_MAX, max(DPI_MIN, dpi))
    return clamped, clamped != dpi


def _region_tag(region) -> str:
    if region is None:
        return "full"
    return "r" + "-".join(f"{round(v * 10000):04d}" for v in region)


def cache_name(page: int, sha16: str, dpi: int, region) -> str:
    return f"p{page:04d}.{sha16}.d{dpi}.{_region_tag(region)}.{RENDER_VERSION}.png"


def sha256_of(f) -> str:
    """開いた原本の sha256（16進）。読み取り位置は先頭へ戻す。"""
    h = hashlib.sha256()
    f.seek(0)
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
    f.seek(0)
    return h.hexdigest()


def pages_dir(rag_root: Path, doc_id: str) -> Path:
    """`{rag_root}/{doc_id}.pages/` を返す（無ければ最後の 1 階層だけ作る）。途中の階層が無い（取り込み前）・symlink・派生ルートの外は `RenderError`。"""
    try:
        rr = Path(rag_root).resolve()
    except OSError:
        raise RenderError("derived_missing", "派生領域を開けません") from None
    if not rr.is_dir():
        raise RenderError("derived_missing", "この資料フォルダの派生領域がありません（取り込み前）")
    parts = (doc_id + ".pages").split("/")
    cur = rr
    for i, part in enumerate(parts):
        cur = cur / part
        last = i == len(parts) - 1
        if cur.is_symlink():
            raise RenderError("unsafe_path", "派生領域の置き場に symlink があるため描画しません")
        if not cur.exists():
            if not last:
                raise RenderError("derived_missing", "この資料の派生領域がありません（取り込み前）")
            try:
                cur.mkdir()
            except FileExistsError:
                pass
            except OSError:
                raise RenderError("write_failed", "画像の置き場を作れません") from None
        if not cur.is_dir():
            raise RenderError("unsafe_path", "派生領域の置き場が不正です")
    try:
        real = cur.resolve()
    except OSError:
        raise RenderError("unsafe_path", "派生領域の置き場が不正です") from None
    if real != rr.joinpath(*parts) or not real.is_relative_to(rr):
        raise RenderError("unsafe_path", "派生領域の外には書きません")
    return real


def _read_png_meta(path: Path) -> dict | None:
    """自分が書いた PNG の IHDR と埋め込みの描画情報を読む。読めない・別物は None。"""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PNG_BYTES:
            return None
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, out = 8, {}
    while pos + 8 <= len(head):
        ln, typ = struct.unpack(">I4s", head[pos:pos + 8])
        body = head[pos + 8:pos + 8 + ln]
        if len(body) < ln:
            break
        if typ == b"IHDR":
            out["width"], out["height"] = struct.unpack(">II", body[:8])
        elif typ == b"tEXt" and body.startswith(_META_KEY + b"\0"):
            try:
                out["meta"] = json.loads(body[len(_META_KEY) + 1:].decode("ascii"))
            except ValueError:
                return None
        elif typ == b"IDAT":
            break
        pos += 12 + ln
    return out if "width" in out and isinstance(out.get("meta"), dict) else None


def _tidy(directory: Path, keep: Path, sha16: str) -> None:
    """古い原本の画像・取り残した一時ファイル・多すぎる分（古い順）を消す。自分の命名規則に合うものだけを触る。"""
    import time
    try:
        entries = [p for p in directory.iterdir() if not p.is_symlink() and p.is_file()]
    except OSError:
        return
    now = time.time()
    mine = []
    for p in entries:
        try:
            if p.name.startswith(".tmp-"):
                if now - p.stat().st_mtime > _TMP_STALE_SEC:
                    p.unlink()
                continue
            m = _NAME_RE.match(p.name)
            if not m or p == keep:
                continue
            if m.group(2) != sha16:
                p.unlink()
            else:
                mine.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if len(mine) + 1 > CACHE_KEEP_MAX:
        for _mt, p in sorted(mine)[:len(mine) + 1 - CACHE_KEEP_MAX]:
            try:
                p.unlink()
            except OSError:
                pass


def render(f, *, rag_root: Path, doc_id: str, page: int, region, dpi: int) -> dict:
    """検査済みの原本 `f`（開いた binary file）の `page`（物理ページ・1 始まり）を描く。同じ条件の画像があれば使い回す。
    戻り値は `path`（絶対パス）・`width`・`height`・`dpi`（実際）・`rotation`（ページの /Rotate）・`total_pages`・`printed_page`・`region`・`downscaled`・`cache`（hit／miss）・`size_bytes`。
    失敗は `RenderError`。`f` は閉じない。
    """
    sha16 = sha256_of(f)[:16]
    directory = pages_dir(rag_root, doc_id)
    final = directory / cache_name(page, sha16, dpi, region)
    meta = _read_png_meta(final)
    if meta and meta["meta"].get("sha16") == sha16 and meta["meta"].get("page") == page:
        m = meta["meta"]
        return {"path": str(final), "width": meta["width"], "height": meta["height"], "dpi": m.get("dpi", dpi),
                "rotation": m.get("rotation", 0), "total_pages": m.get("total"), "printed_page": m.get("printed"),
                "region": list(region) if region else None, "downscaled": bool(m.get("downscaled")),
                "cache": "hit", "size_bytes": final.stat().st_size}
    tmp = directory / f".tmp-{uuid.uuid4().hex}.png"
    cmd = [sys.executable, "-m", "sherpa.pdf_page_image", "--fd", str(f.fileno()), "--out", str(tmp),
           "--page", str(page), "--dpi", str(dpi), "--sha16", sha16,
           "--region", ",".join(str(v) for v in region) if region else ""]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(_REPO_ROOT),
           "PYTHONDONTWRITEBYTECODE": "1", "LANG": os.environ.get("LANG", "C.UTF-8")}
    try:
        try:
            cp = subprocess.run(cmd, pass_fds=(f.fileno(),), env=env, capture_output=True, timeout=TIMEOUT_SEC, check=False)
        except subprocess.TimeoutExpired:
            raise RenderError("render_timeout", f"描画が {TIMEOUT_SEC} 秒を超えたため止めました（dpi を下げるか region で絞る）") from None
        except OSError:
            raise RenderError("render_failed", "描画の処理を起動できませんでした") from None
        try:
            res = json.loads(cp.stdout.decode("utf-8") or "{}")
        except ValueError:
            res = {}
        if not res.get("ok"):
            if res.get("code"):
                raise RenderError(res["code"], res.get("message") or "描画できませんでした")
            raise RenderError("render_failed", "描画に失敗しました（PDF が壊れているか、描画の資源上限を超えました）")
        try:
            size = tmp.stat().st_size
        except OSError:
            raise RenderError("render_failed", "描画に失敗しました") from None
        if size <= 0 or size > MAX_PNG_BYTES:
            raise RenderError("too_large", "画像が大きすぎます（dpi を下げるか region で絞る）")
        if _read_png_meta(tmp) is None:
            raise RenderError("render_failed", "描画に失敗しました")
        try:
            os.replace(tmp, final)
        except OSError:
            raise RenderError("write_failed", "画像を置けませんでした") from None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    _tidy(directory, final, sha16)
    return {"path": str(final), "width": res["width"], "height": res["height"], "dpi": res["dpi"],
            "rotation": res["rotation"], "total_pages": res["total"], "printed_page": res.get("printed"),
            "region": list(region) if region else None, "downscaled": bool(res.get("downscaled")),
            "cache": "miss", "size_bytes": size}


# ---- 子プロセス（`python -m sherpa.pdf_page_image`）----

def _png_chunk(typ: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + typ + body + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF)


def _write_png(path: str, bitmap, meta: dict) -> None:
    """pdfium の RGB ビットマップを PNG にして書く（Pillow に依存しない）。"""
    w, h, stride = bitmap.width, bitmap.height, bitmap.stride
    buf = memoryview(bitmap.buffer).cast("B")
    comp = zlib.compressobj(6)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, "wb") as out:
        out.write(b"\x89PNG\r\n\x1a\n")
        out.write(_png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        out.write(_png_chunk(b"tEXt", _META_KEY + b"\0" + json.dumps(meta, ensure_ascii=True).encode("ascii")))
        pending = bytearray()
        for y in range(h):
            pending += b"\x00"
            pending += buf[y * stride:y * stride + w * 3]
            if len(pending) > 1 << 20:
                out.write(_png_chunk(b"IDAT", comp.compress(bytes(pending))))
                pending.clear()
        out.write(_png_chunk(b"IDAT", comp.compress(bytes(pending)) + comp.flush()))
        out.write(_png_chunk(b"IEND", b""))


def _fail(code: str, message: str) -> int:
    sys.stdout.write(json.dumps({"ok": False, "code": code, "message": message}, ensure_ascii=False))
    return 0


def _child(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fd", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--dpi", type=int, required=True)
    ap.add_argument("--sha16", required=True)
    ap.add_argument("--region", default="")
    a = ap.parse_args(argv)
    try:
        import resource
        for lim, val in ((resource.RLIMIT_AS, _MEM_LIMIT_BYTES), (resource.RLIMIT_CPU, TIMEOUT_SEC + 5)):
            try:
                resource.setrlimit(lim, (val, val))
            except (ValueError, OSError):
                pass
    except ImportError:
        pass
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return _fail("render_unavailable", "PDF を描く部品（pypdfium2）がありません")
    region = tuple(float(v) for v in a.region.split(",")) if a.region else None
    f = os.fdopen(a.fd, "rb", closefd=False)
    try:
        try:
            pdf = pdfium.PdfDocument(f)
        except Exception:
            return _fail("open_failed", "PDF を開けませんでした（壊れている、またはパスワード付きです）")
        total = len(pdf)
        if a.page < 1 or a.page > total:
            return _fail("page_out_of_range", f"ページ {a.page} はありません（全 {total} ページ）")
        pg = pdf[a.page - 1]
        try:
            width, height = pg.get_size()
            rotation = pg.get_rotation()
            x0, y0, x1, y1 = region or (0.0, 0.0, 1.0, 1.0)
            scale = a.dpi / 72.0
            px_w, px_h = (x1 - x0) * width * scale, (y1 - y0) * height * scale
            shrink = min(1.0, MAX_SIDE / max(px_w, px_h) * 0.999, math.sqrt(MAX_PIXELS / (px_w * px_h)) * 0.999)
            scale *= shrink
            crop = (x0 * width, (1 - y1) * height, (1 - x1) * width, y0 * height)
            bitmap = pg.render(scale=scale, crop=crop, rev_byteorder=True, fill_color=(255, 255, 255, 255))
            try:
                printed = pdf.get_page_label(a.page - 1) or None
            except Exception:
                printed = None
            eff_dpi = round(a.dpi * shrink, 1)
            meta = {"sha16": a.sha16, "page": a.page, "total": total, "rotation": rotation, "printed": printed,
                    "dpi": eff_dpi, "downscaled": shrink < 1.0, "v": RENDER_VERSION}
            try:
                _write_png(a.out, bitmap, meta)
                out = {"ok": True, "width": bitmap.width, "height": bitmap.height, **{k: meta[k] for k in
                       ("dpi", "rotation", "total", "printed", "downscaled")}}
            finally:
                bitmap.close()
        finally:
            pg.close()
    except MemoryError:
        return _fail("too_large", "メモリ上限を超えました（dpi を下げるか region で絞る）")
    except Exception:
        return _fail("render_failed", "描画に失敗しました")
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_child(sys.argv[1:]))
