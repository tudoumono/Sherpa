"""原本の UTF-8 / CP932 判定と読み取り境界の回帰テスト。"""
from __future__ import annotations

import os

from sherpa import text_encoding as TE


def test_detect_bytes_truncated_multibyte_boundary_stays_utf8(monkeypatch, tmp_path):
    """判定範囲の上限が多バイト文字の途中で切れても（`complete=False`）、末尾の不完全な列を
    不正と数えず UTF-8 のまま判定する（CP932 に誤って落ちない）。"""
    text = "業務ルールの説明文です。" * 500
    raw = text.encode("utf-8")
    cut = raw[: len(raw) - 1]   # 末尾の3バイト文字を1バイトぶん欠けさせる
    assert TE.detect_bytes(cut, complete=False) == "utf-8"
    path = tmp_path / "source.cbl"
    path.write_bytes(raw)
    monkeypatch.setattr(TE, "DETECT_CAP_BYTES", len(cut))
    with path.open("rb") as f:
        assert TE.detect_fd(f.fileno()) == "utf-8"


def test_detect_bytes_single_corrupted_byte_stays_utf8_not_cp932():
    """UTF-8 の正しい日本語本文に不正バイトが1つ混じっても CP932 と誤らない——同じ範囲を
    UTF-8/CP932 それぞれの置換読みで数え、CP932 のほうが真に少ないときだけ CP932 を選ぶ
    非対称な閾値が効く（実測: この試料は utf-8 側 3 個・cp932 側 38 個）。"""
    text = ("架空の締め処理は毎月末の夜間バッチで実行される。設定ファイルの区分値を確認してから"
            "処理を開始し、異常時はログに詳細を出力して終了する。") * 5
    raw = bytearray(text.encode("utf-8"))
    raw[6] = 0x85
    assert TE.detect_bytes(bytes(raw), complete=True) == "utf-8"


def test_detect_bytes_cp932_source_detected_when_strict_utf8_fails():
    """CP932 のソース（strict UTF-8 では通らない）は cp932 と判定される。"""
    raw = "架空の締め処理の通知メッセージ。".encode("cp932")
    assert TE.detect_bytes(raw, complete=True) == "cp932"


def test_decode_utf8_sig_strips_leading_bom_only():
    """`utf-8-sig` は先頭の BOM だけを落とす。"""
    raw = b"\xef\xbb\xbf# heading\nbody\n"
    assert TE.decode(raw, "utf-8-sig") == "# heading\nbody\n"


def test_decode_utf8_does_not_strip_bom_bytes_mid_document():
    """文書中程に同じ3バイト列（EF BB BF＝U+FEFF）が出ても、`encoding="utf-8"` で decode する限り
    （先頭セグメントでないときの契約・`grep_tool._logical_lines` 参照）は落とさない。"""
    raw = "text ".encode("utf-8") + b"\xef\xbb\xbf" + "more".encode("utf-8")
    assert TE.decode(raw, "utf-8") == "text ﻿more"


def test_detect_fd_finalizes_at_exact_chunk_eof(tmp_path):
    """チャンク境界で終わる半角カナを、未完の UTF-8 列として見逃さない。"""
    path = tmp_path / "source.cbl"
    raw = b"A" * (64 * 1024 - 1) + "ﾂ".encode("cp932")
    path.write_bytes(raw)
    with path.open("rb") as f:
        f.seek(10)
        assert TE.detect_fd(f.fileno()) == TE.detect_bytes(raw, complete=True) == "cp932"
        assert f.tell() == 10


def test_detect_fd_raises_for_closed_descriptor(tmp_path):
    """読めないファイルを UTF-8 の判定成功として返さない。"""
    import pytest

    path = tmp_path / "source.cbl"
    path.write_bytes(b"source")
    with path.open("rb") as f:
        fd = f.fileno()
    with pytest.raises(OSError):
        TE.detect_fd(fd)


def test_ebcdic_like_bytes_are_not_taken_as_cp932():
    """CP932 はほぼ全バイトを受け入れる＝EBCDIC の英数字レコード（数字は私用領域・制御文字を含む）が
    CP932 として選ばれても「読める資料」にはしない（SRH-05: 採否そのものは真の置換文字数の
    少なさだけで決めるが——CP932 の置換文字は0個なので選ばれる——ありえない文字が比率へ計上され、
    行の過半に及ぶため undetermined になる。以前の「ありえない文字の比率で CP932 を棄却し utf-8
    へ戻す」規則は撤去したが、対象外になるという結論自体は変わらない）。"""
    record = "ORDER0001 SAMPLE CUSTOMER 20260929".encode("cp500") + b"\x05\x15"
    enc, ratio, majority_garbled = TE.detect_bytes_quality(record * 50, complete=True)
    assert enc == "cp932"
    assert TE.quality_of(ratio, majority_garbled) == "undetermined"


def test_read_head_classifies_from_head_only(tmp_path, monkeypatch):
    """内容分類の先頭読みは先頭だけで判定し、全体判定（最大 64MiB 読む）を使わない。"""
    from sherpa import corpus_docs
    p = tmp_path / "data.dat"
    p.write_bytes("架空の締め処理\n".encode("cp932") + b"x = 1\n" * (1024 * 1024))
    monkeypatch.setattr(TE, "detect_fd", lambda fd: (_ for _ in ()).throw(AssertionError("全体判定を呼んだ")))
    assert corpus_docs._read_head(p).startswith("架空の締め処理")


def test_grep_skips_only_the_file_whose_encoding_read_fails(tmp_path, monkeypatch):
    """1 ファイルの文字コード判定で読み取りに失敗しても、検索全体を失敗させずそのファイルだけ飛ばす。"""
    from sherpa import grep_tool
    (tmp_path / "a_ok.c").write_text("int NEEDLE = 1;\n")
    (tmp_path / "b_bad.c").write_text("int other = 2;\n")
    real = TE.detect_fd

    def _boom(fd):
        if os.readlink(f"/proc/self/fd/{fd}").endswith("b_bad.c"):
            raise OSError(5, "Input/output error")
        return real(fd)

    monkeypatch.setattr(TE, "detect_fd", _boom)
    hits = grep_tool.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert [h["path"].split("/")[-1] for h in hits] == ["a_ok.c"]


# ===== SRH-05 是正（敵対レビュー実害3件）: BOM/NUL/CP932ありえない文字の計上と行過半判定 =====

def test_quality_of_requires_ratio_over_threshold_and_line_majority_for_undetermined():
    """`quality_of` は比率が閾値を超えるだけでは undetermined にしない——空でない行の過半にも
    化けが広がっていることを両方要求する（一部だけ化けた原本を対象外にしない契約・SRH-05）。"""
    assert TE.quality_of(0.0, False) == "ok"
    assert TE.quality_of(0.5, False) == "partial"          # 比率超過だけでは undetermined にしない
    assert TE.quality_of(TE.UNDETERMINED_RATIO, True) == "partial"   # 閾値ちょうどは超過ではない
    assert TE.quality_of(TE.UNDETERMINED_RATIO + 0.001, True) == "undetermined"


def test_bom_utf8_body_ratio_is_measured_not_fixed_zero(tmp_path):
    """UTF-8 BOM で始まる原本は、以前は中身を測らず常に `ratio=0.0`（"ok"）だった——BOM の後ろが
    CP932 で丸ごと化けていても undetermined になる（`detect_bytes_quality`/`detect_fd_quality`
    の両方・SRH-05）。BOM の後ろが正しい UTF-8 なら従来どおり `"ok"`。"""
    clean = b"\xef\xbb\xbf" + "# 見出し\n架空の本文です。\n".encode("utf-8")
    enc, ratio, majority = TE.detect_bytes_quality(clean, complete=True)
    assert enc == "utf-8-sig"
    assert TE.quality_of(ratio, majority) == "ok"

    garbled = b"\xef\xbb\xbf" + ("在庫管理の区分を更新する。\n" * 5).encode("cp932")
    enc2, ratio2, majority2 = TE.detect_bytes_quality(garbled, complete=True)
    assert enc2 == "utf-8-sig"
    assert TE.quality_of(ratio2, majority2) == "undetermined"

    # fd 版（`detect_fd_quality`）も同じ結論——BOM の3バイトを読み飛ばして本文だけを判定する。
    path = tmp_path / "memo.txt"
    path.write_bytes(garbled)
    with path.open("rb") as f:
        enc3, ratio3, majority3 = TE.detect_fd_quality(f.fileno())
    assert enc3 == "utf-8-sig"
    assert TE.quality_of(ratio3, majority3) == "undetermined"


def test_utf16_is_undetermined_via_nul_counting_with_or_without_bom():
    """strict UTF-8 は NUL（U+0000）を正しい1バイト文字として通すため、BOM 無しの ASCII 主体
    UTF-16LE は以前 `"ok"` になり grep にも一切当たらなかった——NUL を「化け」として数えることで
    undetermined になる（SRH-05）。UTF-16 の BOM（FF FE）付きも同様——BOM の2バイトだけでは
    比率がわずかで `"partial"` 止まりだったが、本文の NUL も数えることで undetermined になる。"""
    ascii_no_bom = ("MONTHLY CLOSING PROCEDURE STEP ONE\n" * 30).encode("utf-16-le")
    enc, ratio, majority = TE.detect_bytes_quality(ascii_no_bom, complete=True)
    assert enc == "utf-8"          # strict UTF-8 として通る（NUL は正当な1バイト文字）
    assert TE.quality_of(ratio, majority) == "undetermined"

    sql_with_bom = b"\xff\xfe" + ("SELECT * FROM ORDERS WHERE ID = 1;\n" * 30).encode("utf-16-le")
    _enc2, ratio2, majority2 = TE.detect_bytes_quality(sql_with_bom, complete=True)
    assert TE.quality_of(ratio2, majority2) == "undetermined"


def test_cp932_with_sparse_implausible_chars_stays_partial_not_rejected(tmp_path):
    """置換文字が0個の正しい CP932 原本（外字が時々混じる程度）は、以前は『ありえない文字の比率』
    だけで CP932 を棄却し UTF-8 の置換読みへ戻していたため undetermined（対象外）になっていた——
    採否は真の置換文字数の少なさだけで決め、ありえない文字は比率へ計上するにとどめる（SRH-05）。
    外字が1行だけ（20行中1行）なら行の過半には届かず `"partial"` のまま検索・精読の対象に残る。"""
    clean_line = "山田太郎様の請求書 ORDERNO を作成する。区分は１とする。".encode("cp932")
    gaiji_line = ("氏名：山田".encode("cp932") + b"\xf0\x40"
                 + "郎　様の請求書 ORDERNO を作成する。区分は１とする。".encode("cp932"))
    raw = b"\n".join([clean_line] * 19 + [gaiji_line]) + b"\n"
    enc, ratio, majority = TE.detect_bytes_quality(raw, complete=True)
    assert enc == "cp932"
    assert not majority                # 20行中1行だけ＝過半に届かない
    assert TE.quality_of(ratio, majority) == "partial"


def test_partial_garbling_confined_to_a_minority_of_lines_stays_partial_not_undetermined():
    """一部だけ化けている原本（化けた段落・行がごく一部）は対象外にせず要確認のままにする契約
    （SRH-05）。比率だけで判定すると `UNDETERMINED_RATIO` を超えて undetermined になり得るが、
    化けは1行（41行中）に限られる＝行の過半には届かないため `"partial"` のままになる。"""
    para = ("架空の段落です。今日は在庫確認を行い、担当者へ結果を連絡します。"
            "締め処理の後に台帳を更新します。")
    utf8_body = ("\n".join([para] * 40) + "\n").encode("utf-8")
    cp932_tail_line = para.encode("cp932")             # 改行なしで直接続く＝1行として追加される
    raw = utf8_body + cp932_tail_line
    enc, ratio, majority = TE.detect_bytes_quality(raw, complete=True)
    assert enc == "utf-8"
    assert ratio > TE.UNDETERMINED_RATIO                # 比率だけなら undetermined の閾値を超える
    assert not majority
    assert TE.quality_of(ratio, majority) == "partial"


def test_short_file_single_bad_byte_stays_partial_despite_high_ratio():
    """短いファイルに不正なバイトが1個だけ混じると比率は大きく跳ねる（`UNDETERMINED_RATIO` 超）が、
    2行中1行だけの化け＝行の過半には届かないため `"partial"` のままになる（SRH-05）。"""
    raw = "締め処理の担当者一覧（".encode("utf-8") + b"\x81" + "）\n管理課\n".encode("utf-8")
    enc, ratio, majority = TE.detect_bytes_quality(raw, complete=True)
    assert enc == "utf-8"
    assert ratio > TE.UNDETERMINED_RATIO
    assert not majority
    assert TE.quality_of(ratio, majority) == "partial"


def test_multiline_euc_jp_with_short_japanese_is_not_readable():
    """行ごとの日本語が短い EUC-JP（CSV など）も、CP932 として「要確認」で使わせず対象外にする。"""
    csv = "コード,名称,区分\n0001,東京支店,1\n0002,大阪支店,2\n" * 10
    enc, ratio, majority = TE.detect_bytes_quality(csv.encode("euc_jp"), complete=True)
    assert TE.quality_of(ratio, majority) == "undetermined"
    enc, ratio, majority = TE.detect_bytes_quality(csv.encode("cp932"), complete=True)
    assert (enc, TE.quality_of(ratio, majority)) == ("cp932", "ok")


def test_cp932_with_gaiji_on_most_lines_stays_readable_with_caution():
    """外字（私用領域）が多くの行にある正しい CP932 は対象外にせず、要確認にとどめる。"""
    line = "氏名：山田".encode("cp932") + b"\xf0\x40" + "郎　様の請求書を作成する。\n".encode("cp932")
    enc, ratio, majority = TE.detect_bytes_quality(line * 20, complete=True)
    assert (enc, TE.quality_of(ratio, majority)) == ("cp932", "partial")


def test_fixed_length_ebcdic_without_control_chars_is_not_readable():
    """制御文字も改行も無い固定長の EBCDIC（英大文字と数字だけ）も対象外にする。"""
    lines = ["000100 IDENTIFICATION DIVISION.", "000200 PROGRAM-ID. SAMPLE01.", "000300 STOP RUN."] * 10
    raw = b"".join(line.ljust(80).encode("cp037") for line in lines)
    enc, ratio, majority = TE.detect_bytes_quality(raw, complete=True)
    assert TE.quality_of(ratio, majority) == "undetermined"


def test_euc_jp_with_nec_special_chars_is_not_readable():
    """Python の euc_jp が拒否する NEC 特殊文字（13 区）を含む EUC-JP も対象外にする。"""
    csv = ("コード,名称,区分\n0001,東京支店,1\n0002,大阪支店,2\n" * 10).encode("euc_jp")
    raw = csv + b"0003," + b"\xad\xea" + "札幌,3\n".encode("euc_jp")
    enc, ratio, majority = TE.detect_bytes_quality(raw, complete=True)
    assert TE.quality_of(ratio, majority) == "undetermined"


def test_cp932_gaiji_table_without_kana_stays_readable():
    """全角のかなを含まない行に外字がある正しい CP932（外字変換表・名簿の CSV）も対象外にしない。"""
    rows = b"".join(b"F0%02X," % i + b"\xf0" + bytes([0x40 + i]) + ",\x8d\x82\n".encode("latin-1") for i in range(20))
    enc, ratio, majority = TE.detect_bytes_quality(rows, complete=True)
    assert (enc, TE.quality_of(ratio, majority)) == ("cp932", "partial")
