"""パスワード付き（暗号化）の Office・PDF を、中身を読まずに見分ける。

パスワードは試さない（PDF の空のパスワードだけ例外）。見分けられないものは「付いていない」と扱い、今までの変換に任せる。
判定は原本を読み取り専用で開くだけで、書き込み・横に置くファイルは無い。
設計: docs/design/rag.md「サイズガードと文字コード・対象外」
"""
from __future__ import annotations

import struct
from pathlib import Path

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_TAIL_BYTES = 128 * 1024
_HEAD_BYTES = 64 * 1024
_XLS_RECORD_SCAN_LIMIT = 40
_BIFF_FILEPASS = 0x002F
_BIFF_EOF = 0x000A
_PPT_ENCRYPTED_TOKEN = 0xF3D1C4DF
_DOC_FENCRYPTED = 0x0100


def is_password_protected(path: str | Path, ext: str | None = None) -> bool:
    """パスワードが無いと開けないファイルか。PDF は空のパスワードで開けるもの（所有者パスワードだけ）を含めない。"""
    p = Path(path)
    suffix = (ext if ext is not None else p.suffix).lower()
    try:
        if suffix == ".pdf":
            return _pdf_needs_user_password(p)
        with open(p, "rb") as f:
            if f.read(8) != _OLE_MAGIC:
                return False
        return _ole_is_encrypted(p, suffix)
    except Exception:
        return False


def _pdf_needs_user_password(p: Path) -> bool:
    """暗号化の印が付いた PDF のうち、空のパスワードで開けないもの。印の無い PDF は PDF 全体を解析せずに False。"""
    size = p.stat().st_size
    with open(p, "rb") as f:
        head = f.read(_HEAD_BYTES)
        f.seek(max(0, size - _TAIL_BYTES))
        tail = f.read()
    if b"/Encrypt" not in head and b"/Encrypt" not in tail:
        return False
    from pypdf import PdfReader
    reader = PdfReader(str(p))
    if not getattr(reader, "is_encrypted", False):
        return False
    try:
        return not reader.decrypt("")
    except Exception:
        return True


def _ole_is_encrypted(p: Path, suffix: str) -> bool:
    try:
        import olefile
    except ImportError:
        return False
    if not olefile.isOleFile(str(p)):
        return False
    with olefile.OleFileIO(str(p)) as ole:
        if ole.exists("EncryptionInfo") or ole.exists("EncryptedPackage"):
            return True
        if suffix == ".doc" and ole.exists("WordDocument"):
            head = ole.openstream("WordDocument").read(12)
            return len(head) >= 12 and bool(struct.unpack_from("<H", head, 0x0A)[0] & _DOC_FENCRYPTED)
        if suffix == ".xls":
            name = "Workbook" if ole.exists("Workbook") else ("Book" if ole.exists("Book") else None)
            return name is not None and _xls_has_filepass(ole.openstream(name))
        if suffix == ".ppt":
            if ole.exists("EncryptedSummary"):
                return True
            if ole.exists("Current User"):
                head = ole.openstream("Current User").read(16)
                return len(head) >= 16 and struct.unpack_from("<I", head, 12)[0] == _PPT_ENCRYPTED_TOKEN
    return False


def _xls_has_filepass(stream) -> bool:
    """BIFF の先頭のレコード列に FILEPASS（暗号化の印）があるか。"""
    for _ in range(_XLS_RECORD_SCAN_LIMIT):
        header = stream.read(4)
        if len(header) < 4:
            return False
        record_id, length = struct.unpack("<HH", header)
        if record_id == _BIFF_FILEPASS:
            return True
        if record_id == _BIFF_EOF:
            return False
        stream.read(length)
    return False
