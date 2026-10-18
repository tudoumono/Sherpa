"""1 文書の差し替え（office_md.publish_one）で、回答時に描いたページ画像（{rel}.pages/）を古い原本の分として残さない。"""
import xlsxwriter

from sherpa.ingest import office_md


def test_publish_one_removes_rendered_page_images_of_the_replaced_document(tmp_path):
    src = tmp_path / "kb"
    src.mkdir()
    der = tmp_path / "derived" / "md"
    wb = xlsxwriter.Workbook(src / "a.xlsx")
    wb.add_worksheet("s").write(0, 0, "甲の値")
    wb.close()
    office_md.build_derived(src, der)
    pages = der.parent / "rag" / "a.xlsx.pages"
    pages.mkdir()
    (pages / "p0001.old.png").write_bytes(b"x")
    rep = office_md.derive_one(src, der, (src / "a.xlsx").resolve(), "a.xlsx")
    assert not rep.get("error")
    office_md.publish_one(der, "a.xlsx")
    assert not pages.exists()
    assert "甲の値" in (der.parent / "rag" / "a.xlsx.rag.md").read_text(encoding="utf-8")
