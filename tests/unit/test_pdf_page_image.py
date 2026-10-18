"""PDF ページ画像ツール（pdf_page_image）と pdf_pages の画像確認の案内の回帰テスト。
実害の再現に限る（向き・領域・上限・キャッシュの取り違え・範囲外/秘匿/symlink/層の素通し・派生ルート外への書き込み・指示文の抜け）。DB/ES/Neo4j 不要。
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import zlib
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest

from sherpa import codex_agents_md, mcp_server, store
from sherpa import doc_readers as DR
from sherpa import pdf_page_image as P
from sherpa import tool_dispatch as TD
from sherpa import worlds as worlds_mod

pytestmark = pytest.mark.usefixtures("upstream_only_registry")

BLACK_TOP_LEFT = "0 g 0 150 100 50 re f"  # 400x200pt のページの左上（PDF 座標は左下が原点）


def _pdf(path: Path, pages: list[dict]) -> None:
    """ページごとに `{"rotate": 度, "box": (w, h), "ops": 描画命令, "image": bool, "text": str}` を持つ PDF を直接組む。"""
    objs: list[bytes] = [b"", b""]

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    img = add(b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream")
    kids = []
    for pg in pages:
        ops = [pg.get("ops", "")]
        if pg.get("text"):
            ops.append(f"BT /F1 10 Tf 10 100 Td ({pg['text']}) Tj ET")
        if pg.get("image"):
            ops.append("q 50 0 0 50 100 100 cm /Im1 Do Q")
        data = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
        w, h = pg.get("box", (400, 200))
        rot = f"/Rotate {pg['rotate']} " if pg.get("rotate") else ""
        kids.append(add(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] %s/Contents %d 0 R "
                         "/Resources << /Font << /F1 %d 0 R >> /XObject << /Im1 %d 0 R >> >> >>"
                         % (w, h, rot, content, font, img)).encode()))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(f"{k} 0 R" for k in kids), len(kids))).encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def _decode(path: str) -> tuple[int, int, list[bytes]]:
    """このツールが書く PNG（RGB 8bit・フィルタ 0）を行ごとの RGB バイト列へ戻す。"""
    raw = Path(path).read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(raw):
        ln, typ = struct.unpack(">I4s", raw[pos:pos + 8])
        body = raw[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif typ == b"IDAT":
            idat += body
        pos += 12 + ln
    data = zlib.decompress(idat)
    row = 1 + w * 3
    return w, h, [data[y * row + 1:(y + 1) * row] for y in range(h)]


def _dark(rows: list[bytes], x: int, y: int) -> bool:
    return rows[y][x * 3] < 64


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """tmp の KB（原本）と派生領域。`world` の rag/ は空で作っておく（取り込み済みの形）。"""
    world = "pgimg"
    kb = tmp_path / "kb"
    derived = tmp_path / "derived"
    wd = kb / world
    wd.mkdir(parents=True)
    (derived / world / "rag").mkdir(parents=True)
    monkeypatch.setenv("SHERPA_KB_DIR", str(kb))
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(derived))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    for name in ("SHERPA_MCP_WORLD", "SHERPA_MCP_WORLD_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(store, "get_world", lambda world_id: None)
    monkeypatch.setattr(store, "get_world_root", lambda world_id: None)
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: wd)
    return type("Env", (), {"world": world, "wd": wd, "rag": derived / world / "rag", "tmp": tmp_path})


def _call(env, args: dict, scope=None, layer=None) -> dict:
    r, _docs, _c, _k = TD.run_tool("pdf_page_image", args, env.world, scope, layer=layer)
    return r


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ===== 描画（向き・領域・上限）=====

def test_render_full_page_keeps_orientation_and_rotated_page_is_upright(env):
    _pdf(env.wd / "a.pdf", [{"ops": BLACK_TOP_LEFT}, {"ops": BLACK_TOP_LEFT, "rotate": 90}])
    r = _call(env, {"doc_id": "a.pdf", "page": 1, "dpi": 72})
    assert "error" not in r and (r["width"], r["height"], r["rotation"]) == (400, 200, 0)
    w, h, rows = _decode(r["path"])
    assert _dark(rows, 10, 10) and not _dark(rows, w - 10, h - 10) and not _dark(rows, w - 10, 10)
    r2 = _call(env, {"doc_id": "a.pdf", "page": 2, "dpi": 72})
    assert (r2["width"], r2["height"], r2["rotation"]) == (200, 400, 90)
    w2, h2, rows2 = _decode(r2["path"])
    assert _dark(rows2, w2 - 10, 10) and not _dark(rows2, 10, 10) and not _dark(rows2, w2 - 10, h2 - 10)


def test_render_region_zoom_crops_the_requested_rectangle_even_on_rotated_page(env):
    _pdf(env.wd / "a.pdf", [{"ops": BLACK_TOP_LEFT}, {"ops": BLACK_TOP_LEFT, "rotate": 90}])
    r = _call(env, {"doc_id": "a.pdf", "page": 1, "dpi": 72, "region": [0, 0, 0.25, 0.25]})
    w, h, rows = _decode(r["path"])
    assert (w, h) == (100, 50) and r["region"] == [0.0, 0.0, 0.25, 0.25]
    assert all(_dark(rows, x, y) for x in (1, 50, 98) for y in (1, 25, 48))
    r = _call(env, {"doc_id": "a.pdf", "page": 1, "dpi": 72, "region": [0.5, 0.5, 1, 1]})
    _w, _h, rows = _decode(r["path"])
    assert not any(_dark(rows, x, y) for x in (1, 50, 190) for y in (1, 50, 98))
    # 回転ページでも region は「見えている向き」の相対座標（黒は見た目の右上）
    r = _call(env, {"doc_id": "a.pdf", "page": 2, "dpi": 72, "region": [0.75, 0, 1, 0.25]})
    w, h, rows = _decode(r["path"])
    assert (w, h) == (50, 100) and all(_dark(rows, x, y) for x in (2, 25, 47) for y in (2, 50, 97))
    r = _call(env, {"doc_id": "a.pdf", "page": 2, "dpi": 72, "region": [0, 0, 0.25, 0.25]})
    _w, _h, rows = _decode(r["path"])
    assert not any(_dark(rows, x, y) for x in (2, 25, 47) for y in (2, 50, 97))


def test_render_caps_pixels_side_and_dpi(env):
    _pdf(env.wd / "big.pdf", [{"box": (14400, 14400)}])
    r = _call(env, {"doc_id": "big.pdf", "page": 1, "dpi": 999})
    assert r["dpi_clamped"] is True and r["downscaled"] is True
    assert max(r["width"], r["height"]) <= P.MAX_SIDE and r["width"] * r["height"] <= P.MAX_PIXELS
    assert r["dpi"] < P.DPI_MAX and r["size_bytes"] <= P.MAX_PNG_BYTES


def test_render_timeout_is_explicit_error_and_leaves_no_temp_file(env, monkeypatch):
    _pdf(env.wd / "a.pdf", [{"ops": BLACK_TOP_LEFT}])

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(P.subprocess, "run", boom)
    r = _call(env, {"doc_id": "a.pdf", "page": 1})
    assert r["error_code"] == "pdf_page_image_render_timeout" and "path" not in r
    pages = env.rag / "a.pdf.pages"
    assert pages.is_dir() and list(pages.iterdir()) == []


def test_page_out_of_range_and_bad_arguments_are_explicit_errors(env):
    _pdf(env.wd / "a.pdf", [{"ops": BLACK_TOP_LEFT}])
    assert _call(env, {"doc_id": "a.pdf", "page": 2})["error_code"] == "pdf_page_image_page_out_of_range"
    for bad in (0, -1, "x", True, None, 1.5):
        assert _call(env, {"doc_id": "a.pdf", "page": bad})["error_code"] == "read_invalid_args", bad
    for bad in ([0, 0, 1], [0.5, 0, 0.4, 1], [0, 0, 0.01, 1], "a", [0, 0, 1, "x"]):
        assert _call(env, {"doc_id": "a.pdf", "page": 1, "region": bad})["error_code"] == "read_invalid_args", bad
    assert _call(env, {"doc_id": "a.pdf", "page": 1, "dpi": "high"})["error_code"] == "read_invalid_args"
    broken = env.wd / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot a pdf")
    assert _call(env, {"doc_id": "broken.pdf", "page": 1})["error_code"] == "pdf_page_image_open_failed"


# ===== キャッシュ =====

def test_cache_hit_reuses_file_and_changed_original_gets_new_name_and_drops_old(env):
    _pdf(env.wd / "a.pdf", [{"ops": BLACK_TOP_LEFT}])
    r1 = _call(env, {"doc_id": "a.pdf", "page": 1, "dpi": 72})
    assert r1["cache"] == "miss"
    name = Path(r1["path"]).name
    assert name.startswith("p0001.") and ".d72.full.v1.png" in name
    mtime = os.stat(r1["path"]).st_mtime_ns
    r2 = _call(env, {"doc_id": "a.pdf", "page": 1, "dpi": 72})
    assert r2["cache"] == "hit" and r2["path"] == r1["path"] and os.stat(r2["path"]).st_mtime_ns == mtime
    assert (r2["width"], r2["height"], r2["rotation"], r2["total_pages"]) == (400, 200, 0, 1)
    assert Path(_call(env, {"doc_id": "a.pdf", "page": 1, "dpi": 100})["path"]).name != name  # 条件が違えば別の画像
    _pdf(env.wd / "a.pdf", [{"ops": "0 g 300 0 100 50 re f"}])  # 原本の差し替え（右下が黒）
    r3 = _call(env, {"doc_id": "a.pdf", "page": 1, "dpi": 72})
    assert r3["cache"] == "miss" and r3["path"] != r1["path"] and not os.path.exists(r1["path"])
    w, h, rows = _decode(r3["path"])
    assert _dark(rows, w - 10, h - 10) and not _dark(rows, 10, 10)


def test_cache_entry_from_another_original_is_not_returned(env):
    """名前が同じでも中の sha が別の画像（手で置かれた・衝突）は返さず描き直す。"""
    _pdf(env.wd / "a.pdf", [{"ops": BLACK_TOP_LEFT}])
    r1 = _call(env, {"doc_id": "a.pdf", "page": 1, "dpi": 72})
    stale = Path(r1["path"])
    stale.write_bytes(b"not a png")
    r2 = _call(env, {"doc_id": "a.pdf", "page": 1, "dpi": 72})
    assert r2["cache"] == "miss" and _decode(r2["path"])[0] == 400


# ===== 守り =====

def test_guards_scope_sensitive_symlink_traversal_absolute_layer(env):
    _pdf(env.wd / "4期" / "a.pdf", [{"ops": BLACK_TOP_LEFT}])
    (env.rag / "4期").mkdir()
    _pdf(env.wd / "5期" / "b.pdf", [{"ops": BLACK_TOP_LEFT}])
    _pdf(env.wd / "credentials.pdf", [{"ops": BLACK_TOP_LEFT}])
    outside = env.tmp / "outside.pdf"
    _pdf(outside, [{"ops": BLACK_TOP_LEFT}])
    (env.wd / "link.pdf").symlink_to(outside)
    (env.wd / "linkdir").symlink_to(env.tmp)
    (env.wd / "note.txt").write_text("x")
    denied = {"error": "doc_id が無効、または読み取り対象外です"}
    for bad in ("../outside.pdf", "4期/../../outside.pdf", str(outside), "credentials.pdf", "link.pdf",
                "linkdir/outside.pdf", "missing.pdf", "note.txt", "", "4期\\a.pdf"):
        assert _call(env, {"doc_id": bad, "page": 1}) == denied, bad
    assert _call(env, {"doc_id": "5期/b.pdf", "page": 1}, scope=["4期"]) == denied
    assert "error" not in _call(env, {"doc_id": "4期/a.pdf", "page": 1}, scope=["4期"])
    assert _call(env, {"doc_id": "4期/a.pdf", "page": 1}, layer="code") == {"error": "探す対象がソースに限定されています"}
    assert not any("outside" in s or "credentials" in s for s in _tree(env.rag))
    assert not (env.rag / "5期").exists()


def test_never_writes_outside_derived_root_or_into_the_original_folder(env, monkeypatch):
    _pdf(env.wd / "d" / "a.pdf", [{"ops": BLACK_TOP_LEFT}])
    before = _tree(env.wd)
    # 取り込み前（途中の階層が派生領域に無い）は作らずエラー
    r = _call(env, {"doc_id": "d/a.pdf", "page": 1})
    assert r["error_code"] == "pdf_page_image_derived_missing" and not (env.rag / "d").exists()
    (env.rag / "d").mkdir()
    # 置き場所が派生領域の外への symlink なら書かない
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    (env.rag / "d" / "a.pdf.pages").symlink_to(elsewhere)
    r = _call(env, {"doc_id": "d/a.pdf", "page": 1})
    assert r["error_code"] == "pdf_page_image_unsafe_path" and list(elsewhere.iterdir()) == []
    (env.rag / "d" / "a.pdf.pages").unlink()
    (env.rag / "d").rename(env.rag / "moved")
    (env.rag / "d").symlink_to(elsewhere)  # 途中の階層が symlink
    r = _call(env, {"doc_id": "d/a.pdf", "page": 1})
    assert r["error_code"] == "pdf_page_image_unsafe_path" and list(elsewhere.iterdir()) == []
    (env.rag / "d").unlink()
    (env.rag / "moved").rename(env.rag / "d")
    ok = _call(env, {"doc_id": "d/a.pdf", "page": 1})
    assert Path(ok["path"]).resolve().is_relative_to(env.rag.resolve())
    assert _tree(env.wd) == before
    # 派生領域そのものが無い資料フォルダ
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(env.tmp / "nowhere"))
    assert _call(env, {"doc_id": "d/a.pdf", "page": 1})["error_code"] == "pdf_page_image_derived_missing"


def test_other_world_gets_its_own_images_and_never_the_first_ones(env, monkeypatch):
    _pdf(env.wd / "a.pdf", [{"ops": BLACK_TOP_LEFT}])
    r = _call(env, {"doc_id": "a.pdf", "page": 1})
    other = env.tmp / "derived" / "other" / "rag"
    other.mkdir(parents=True)
    r2, _d, _c, _k = TD.run_tool("pdf_page_image", {"doc_id": "a.pdf", "page": 1}, "other", None)
    assert Path(r["path"]).is_relative_to(env.rag) and Path(r2["path"]).is_relative_to(other)
    assert r2["cache"] == "miss"


def test_tidy_removes_only_stale_originals_and_caps_count(env, monkeypatch):
    _pdf(env.wd / "a.pdf", [{"ops": BLACK_TOP_LEFT}])
    pages = env.rag / "a.pdf.pages"
    pages.mkdir()
    (pages / "p0001.0123456789abcdef.d72.full.v1.png").write_bytes(b"old")
    (pages / "memo.txt").write_text("keep")
    monkeypatch.setattr(P, "CACHE_KEEP_MAX", 3)
    for dpi in (60, 70, 80, 90, 100):
        _call(env, {"doc_id": "a.pdf", "page": 1, "dpi": dpi})
    names = sorted(p.name for p in pages.iterdir())
    assert "memo.txt" in names and not any(n.startswith("p0001.0123456789abcdef") for n in names)
    assert len([n for n in names if n.endswith(".png")]) == 3


# ===== pdf_pages の案内 =====

def test_pdf_pages_points_to_page_image_for_image_drawing_and_no_text_pages(tmp_path):
    lines = "\n".join(f"{10 * i} 10 m {10 * i} 90 l S" for i in range(1, 13))
    p = tmp_path / "t.pdf"
    _pdf(p, [{"text": "plain text page"},                      # 1 文字だけ
             {"text": "with image", "image": True},             # 2 画像あり
             {"text": "flow", "ops": lines},                    # 3 図形が多い
             {"image": True},                                   # 4 文字層なし・画像のみ
             {}])                                               # 5 白紙
    out = DR.pdf_pages(open(p, "rb"), pages="1-5")
    assert out["view_pages"] == [2, 3, 4] and "pdf_page_image" in out["view_hint"]
    assert out["pages"][2]["drawing_count"] >= 10 and out["pages"][4].get("blank") is True
    plain = tmp_path / "p.pdf"
    _pdf(plain, [{"text": "only text"}])
    out2 = DR.pdf_pages(open(plain, "rb"), pages="1")
    assert "view_pages" not in out2 and "view_hint" not in out2


# ===== MCP の公開・指示文 =====

def _tool_names_for(monkeypatch, layer):
    if layer:
        monkeypatch.setenv("SHERPA_MCP_LAYER", layer)
    else:
        monkeypatch.delenv("SHERPA_MCP_LAYER", raising=False)
    return {d["name"]: d for d in mcp_server._tool_defs()}


def test_mcp_exposes_pdf_page_image_except_for_code_layer_and_counts_it_as_a_read(monkeypatch):
    assert "pdf_page_image" in mcp_server.READ_DOC_TOOLS
    for layer in (None, "both", "docs"):
        d = _tool_names_for(monkeypatch, layer)["pdf_page_image"]
        assert d["inputSchema"]["required"] == ["doc_id", "page"]
        assert "view_image" in d["description"]
    assert "pdf_page_image" not in _tool_names_for(monkeypatch, "code")


@pytest.mark.parametrize("style", ["json", "plan_gate", "plan_main"])
def test_agents_md_of_all_three_ledger_styles_carries_the_page_image_and_output_rules(tmp_path, style):
    codex_agents_md.write_agents_md(tmp_path, output_schema=True, output_schema_v2=True, ledger_style=style,
                                    multi_agent=(style != "json"), review_rounds=1)
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "pdf_page_image" in text and "tools.view_image({path})" in text and "image(r.image_url)" in text
    assert "推定で補わない" in text
    assert "必要な欄（件数・パス・ページ・該当箇所）だけ" in text and "next_text_offset" in text


def test_mcp_call_replays_page_image_instead_of_rejecting_duplicate(env, monkeypatch):
    _pdf(env.wd / "a.pdf", [{"ops": BLACK_TOP_LEFT}])
    monkeypatch.setenv("SHERPA_MCP_WORLD", env.world)
    monkeypatch.setattr(mcp_server, "_seen_tool_calls", mcp_server.OrderedDict())
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "pdf_page_image", "arguments": {"doc_id": "a.pdf", "page": 1}}}
    bodies = []
    for _ in range(2):
        resp = mcp_server.handle(req)
        assert not resp["result"]["isError"]
        bodies.append(json.loads(resp["result"]["content"][0]["text"]))
    assert bodies[0]["cache"] == "miss" and bodies[1]["cache"] == "hit" and bodies[0]["path"] == bodies[1]["path"]
